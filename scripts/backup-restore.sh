#!/usr/bin/env bash
# MAI full-stack backup / verify / restore (card 12-backup-restore, P12).
#
# Deterministic, checksum-verified whole-instance backup and restore:
#   1. one pg_dump per database of the stack Postgres (C05 guard rule 9):
#      `mai` (application schema chain 0001..0007 + the engine-owned `dbos`
#      schema) and the dedicated `hindsight` database (C07 rule 3: memory
#      banks are backed up, never silently rebuilt);
#   2. the canonical file half (profiles, prompts, documents, durable
#      reindex outbox) through the REAL stable producer
#      `python -m mai.cli backup` (card 12-cli-ops) — never a second
#      archive implementation;
#   3. credential REFERENCES only: the configured .env is reduced to sorted
#      NAMES through the reviewed `mai.credentials.env` parser. No secret
#      VALUE ever enters the archive (C03 rule 6); re-provisioning .env
#      values is a deliberate operator act, not a backup copy;
#   4. manifest.json + SHA256SUMS + a deterministic .tar.gz bundle (sorted
#      members, fixed mtimes/owner, gzip -n). `verify` re-checks every
#      digest with zero side effects; `restore` verifies BEFORE any target
#      effect and refuses tampered or traversal-carrying archives.
#
# Consistency boundary (full procedure: docs/operations/recovery.md): the
# pg_dumps and the file snapshot are sequential, not atomic. The script
# captures the DATABASE first and the FILES second, so every file-side
# delta versus the DB snapshot carries its durable reindex intent
# (C02 W2/W4) and the post-restore scope reconcile converges the derived
# index from the canonical files. Restores never auto-start a worker or
# resume runs: PENDING workflow rows ride the dump untouched, ERROR rows
# stay ERROR until an explicit resume (engine rule 8) — the operator
# inspects pending runs through the reviewed jobs surface BEFORE
# relaunching any worker.
#
# The script never creates, starts, stops or removes containers. Restore
# loads into a Postgres the operator provisioned fresh; freshness is
# verified (empty `mai` database, no `dbos` schema, absent-or-empty
# `hindsight`) and anything else is refused before one restored byte.
#
# Exit codes: 0 success (one JSON line on stdout) · 1 structured refusal
# or step failure (one {"error":{"code","message"}} line on stderr) ·
# 2 usage error.
#
# Test/ops seams (documented, unset in production):
#   MAI_BACKUP_CLI="CMD..."  mai.cli invocation prefix (default:
#                            <repo>/backend/.venv/bin/python, falling back
#                            to `uv run --frozen --project <repo>/backend
#                            python`).
#   MAI_BACKUP_PG_CONTAINER  explicit postgres container name; the default
#                            resolves the `postgres` service of the given
#                            compose project (fixtures use bare disposable
#                            containers with unique mai-fxt- names).

set -Eeuo pipefail

#: EXIT trap runs after function scopes are discarded, so the temp path is
#: kept in a global (locals are unbound inside the trap under `set -u`).
CLEANUP_DIR=''
cleanup() { [ -n "$CLEANUP_DIR" ] && rm -rf -- "$CLEANUP_DIR" || true; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# constants and frozen vocabulary
# ---------------------------------------------------------------------------

SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(dirname "$SCRIPT_PATH")
REPO_ROOT=$(dirname "$SCRIPT_DIR")
BACKUP_FORMAT=1
BUNDLE_ROOT_NAME="mai-backup"
MAIN_DATABASE="mai"
MEMORY_DATABASE="hindsight"
PG_USER="mai"

ERR_USAGE=2

usage() {
	cat <<'USAGE'
usage: backup-restore.sh backup --data-root DIR --env-file FILE --output ARCHIVE.tar.gz
                                [--compose-file FILE] [--project NAME]
       backup-restore.sh verify ARCHIVE.tar.gz
       backup-restore.sh restore ARCHIVE.tar.gz --data-root DIR
                                [--compose-file FILE] [--project NAME]

backup  snapshot the whole instance: Postgres (mai + hindsight, one pg_dump
        each), canonical files (via `mai.cli backup`), credential NAMES.
        Writes a deterministic .tar.gz bundle; refuses an existing output.
verify  re-check manifest and every member digest; no effects.
restore verify first (tamper/traversal refusal before any effect), verify
        the Postgres target is fresh (empty `mai`, no `dbos` schema,
        absent-or-empty `hindsight`), then restore files via `mai.cli
        restore` and load the dumps. Never starts or stops services; the
        summary names the post-restore operator checklist.
USAGE
}

json_quote() { # json_quote STRING -> JSON string literal on stdout
	LC_ALL=C python3 - "$1" <<'PYEOF'
import json, sys

sys.stdout.write(json.dumps(sys.argv[1]))
PYEOF
}

fail() { # fail CODE MESSAGE  ->  exit 1 with one structured error line
	printf '%s\n' "{\"error\":{\"code\":\"$1\",\"message\":$(json_quote "$2")}}" >&2
	exit 1
}

# ---------------------------------------------------------------------------
# producer plumbing (the reviewed seam, never a second implementation)
# ---------------------------------------------------------------------------

if [ -n "${MAI_BACKUP_CLI:-}" ]; then
	# shellcheck disable=SC2086  # explicit multi-word override
	mai_cli() { $MAI_BACKUP_CLI "$@"; }
elif [ -x "$REPO_ROOT/backend/.venv/bin/python" ]; then
	mai_cli() { "$REPO_ROOT/backend/.venv/bin/python" "$@"; }
else
	mai_cli() {
		"$HOME/.local/bin/uv" run --frozen --project "$REPO_ROOT/backend" python "$@"
	}
fi

compose_container() { # compose_container <file> <project> -> container id
	local id
	id=$(docker compose -f "$1" -p "$2" ps -q postgres 2>/dev/null || true)
	[ -n "$id" ] ||
		fail pg_unreachable "no running postgres container in compose project $2; start the stack or set MAI_BACKUP_PG_CONTAINER"
	printf '%s' "$id"
}

resolve_pg_container() { # resolve_pg_container <file> <project>
	local container=${MAI_BACKUP_PG_CONTAINER:-}
	if [ -z "$container" ]; then
		container=$(compose_container "$1" "$2")
	else
		docker inspect "$container" >/dev/null 2>&1 ||
			fail pg_unreachable "MAI_BACKUP_PG_CONTAINER names no running container: $container"
	fi
	printf '%s' "$container"
}

pg_exec() { # pg_exec <docker exec args...>; container-local, value-free
	docker exec "$@"
}

pg_psql() { # pg_psql <container> <database> <psql args...>
	local container=$1 database=$2
	shift 2
	pg_exec "$container" psql --username="$PG_USER" --set=ON_ERROR_STOP=1 \
		--dbname="$database" "$@"
}

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

# ---------------------------------------------------------------------------
# bundle format: manifest.json -> SHA256SUMS -> members
# ---------------------------------------------------------------------------

write_bundle_manifest() { # write_bundle_manifest <staging-root> <created-at-utc>
	local staging=$1 created_at=$2 sums_hash tmp
	sums_hash=$(sha256_of "$staging/$BUNDLE_ROOT_NAME/SHA256SUMS")
	tmp=$(mktemp "$staging/$BUNDLE_ROOT_NAME/.manifest.XXXXXX")
	{
		printf '{\n'
		printf '  "format": %d,\n' "$BACKUP_FORMAT"
		printf '  "created_at": %s,\n' "$(json_quote "$created_at")"
		printf '  "bundle": %s,\n' "$(json_quote "$BUNDLE_ROOT_NAME")"
		printf '  "producer": %s,\n' "$(json_quote "scripts/backup-restore.sh")"
		printf '  "sha256sums": %s,\n' "$(json_quote "$sums_hash")"
		printf '  "components": [\n'
		local first=1 line path bytes
		while IFS= read -r line; do
			path=${line#*  }
			bytes=$(stat -c '%s' "$staging/$BUNDLE_ROOT_NAME/$path")
			[ "$first" = 1 ] || printf ',\n'
			first=0
			printf '    {"path": %s, "bytes": %d, "sha256": %s}' \
				"$(json_quote "$path")" "$bytes" "$(json_quote "${line%%  *}")"
		done <"$staging/$BUNDLE_ROOT_NAME/SHA256SUMS"
		printf '\n  ]\n}\n'
	} >"$tmp"
	mv "$tmp" "$staging/$BUNDLE_ROOT_NAME/manifest.json"
}

build_bundle() { # build_bundle <staging-root> <output-archive>
	local staging=$1 output=$2
	(
		cd "$staging"
		tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
			--format=gnu --mode='u+rwX,go-rwx' -cf - "$BUNDLE_ROOT_NAME"
	) | gzip -n -9 >"$output"
}

require_safe_relative_path() { # refuse absolute/.. archive member paths
	local path=$1
	case "/$path/" in
	'//*' | *'//*' | *'/../'* | *'/..')
		fail unsafe_path "archive member path is not a safe relative path"
		;;
	esac
}

# verify_bundle <archive> <extract-dir>; prints "members=<n>".
# Refusals BEFORE any target effect: unreadable/malformed archive, missing
# or format-mismatched manifest, digest-of-SHA256SUMS mismatch, unsafe
# member paths, any member digest mismatch (truncated or tampered archive).
verify_bundle() {
	local archive=$1 extract=$2 line expected actual path members=0
	[ -f "$archive" ] || fail archive_missing "backup archive does not exist: $archive"
	mkdir -m 700 -p "$extract"
	if ! tar -xzf "$archive" -C "$extract" 2>/dev/null; then
		fail archive_invalid "archive is not a readable gzip tar backup"
	fi
	local bundle="$extract/$BUNDLE_ROOT_NAME"
	[ -f "$bundle/manifest.json" ] || fail archive_invalid "archive does not contain the backup manifest"
	[ -f "$bundle/SHA256SUMS" ] || fail archive_invalid "archive does not contain SHA256SUMS"
	# GNU tar already refuses absolute and `..` member names on extraction;
	# the checksum sheet is validated independently so a doctored sheet
	# cannot redirect any digest check outside the bundle.
	while IFS= read -r line || [ -n "$line" ]; do
		[ -n "$line" ] || continue
		case "$line" in
		[0-9a-f]*"  "*) ;;
		*) fail archive_invalid "SHA256SUMS line is malformed" ;;
		esac
		expected=${line%%  *}
		path=${line#*  }
		require_safe_relative_path "$path"
		[ -f "$bundle/$path" ] || fail archive_invalid "SHA256SUMS lists a missing member: $path"
		actual=$(sha256_of "$bundle/$path")
		[ "$actual" = "$expected" ] ||
			fail checksum_mismatch "member digest differs from SHA256SUMS: $path"
		members=$((members + 1))
	done <"$bundle/SHA256SUMS"
	local manifest_fields
	manifest_fields=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("format", -1)); print(d["sha256sums"])' \
		"$bundle/manifest.json" 2>/dev/null) ||
		fail archive_invalid "backup manifest is not readable JSON"
	[ "$(head -n1 <<<"$manifest_fields")" = "$BACKUP_FORMAT" ] ||
		fail archive_invalid "backup manifest format is unsupported"
	expected=$(sha256_of "$bundle/SHA256SUMS")
	[ "$(tail -n1 <<<"$manifest_fields")" = "$expected" ] ||
		fail checksum_mismatch "backup manifest does not match SHA256SUMS (tampered manifest)"
	printf 'members=%d' "$members"
}

# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------

dump_database() { # dump_database <container> <database> <output.sql.gz>
	local container=$1 database=$2 output=$3
	# pg_dump (PG18) wraps the plain dump in `\restrict <random>` …
	# `\unrestrict <random>` psql guards. The random token breaks
	# byte-determinism, so both lines are normalized to one fixed token
	# (literal rules — an optional group would collapse in a backref);
	# the guard itself is preserved, and these are checksum-pinned dumps.
	if ! pg_exec "$container" pg_dump --username="$PG_USER" --format=plain \
		--no-owner --no-privileges --dbname="$database" |
		sed -E -e 's/^\\restrict [A-Za-z0-9]+/\\restrict mai-backup-restore/' \
			-e 's/^\\unrestrict [A-Za-z0-9]+/\\unrestrict mai-backup-restore/' |
		gzip -n -9 >"$output"; then
		rm -f "$output"
		fail pg_unreachable "pg_dump of database $database failed"
	fi
}

write_env_names() { # write_env_names <env-file> <output.txt> — NAMES only, never values
	local env_file=$1 output=$2
	if ! mai_cli -c '
import sys
from pathlib import Path
from mai.credentials.env import EnvFileError, parse_env_text
try:
    entries = parse_env_text(Path(sys.argv[1]).read_text(encoding="utf-8"), source=sys.argv[1])
except EnvFileError as exc:
    print(f"env_file_{exc.code.value}: {exc.detail}", file=sys.stderr)
    raise SystemExit(3)
for name in sorted(entries):
    print(name)
' "$env_file" >"$output"; then
		fail env_file_invalid "the .env source was rejected by the credential resolver (only names are read; the refusal line names the reason)"
	fi
}

cmd_backup() {
	local data_root='' env_file='' output='' compose_file="$REPO_ROOT/compose.yaml" project=machina
	while [ $# -gt 0 ]; do
		case "$1" in
		--data-root) data_root=$2; shift 2 ;;
		--env-file) env_file=$2; shift 2 ;;
		--output) output=$2; shift 2 ;;
		--compose-file) compose_file=$2; shift 2 ;;
		--project) project=$2; shift 2 ;;
		*) usage >&2; return "$ERR_USAGE" ;;
		esac
	done
	[ -n "$data_root" ] && [ -n "$env_file" ] && [ -n "$output" ] ||
		{ usage >&2; return "$ERR_USAGE"; }
	[ -d "$data_root" ] || fail data_root_missing "data root does not exist: $data_root"
	[ -f "$env_file" ] || fail env_file_missing "credential source does not exist: $env_file"
	[ ! -e "$output" ] || fail output_exists "archive output path already exists: $output"

	# Fail fast on the Postgres seam before writing anything.
	local container
	container=$(resolve_pg_container "$compose_file" "$project")

	local staging created_at
	staging=$(mktemp -d "${TMPDIR:-/tmp}/mai-backup-XXXXXX")
	chmod 700 "$staging"
	created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
	CLEANUP_DIR=$staging

	local bundle="$staging/$BUNDLE_ROOT_NAME"
	mkdir -m 700 -p "$bundle/postgres" "$bundle/files" "$bundle/config"

	# 1. Database first: every later file-side delta keeps its durable
	#    reindex intent in the (younger) file snapshot (recovery.md).
	dump_database "$container" "$MAIN_DATABASE" "$bundle/postgres/$MAIN_DATABASE.sql.gz"
	dump_database "$container" "$MEMORY_DATABASE" "$bundle/postgres/$MEMORY_DATABASE.sql.gz"

	# 2. File half through the reviewed producer (it refuses symlink
	#    escapes and non-regular entries; C02 rule 3). Its own JSON line
	#    is internal detail; this script keeps the one-summary contract.
	if ! mai_cli -m mai.cli backup --data-root "$data_root" --output "$bundle/files/data-root.tar.gz" >/dev/null; then
		fail cli_failed "mai.cli backup rejected the data root"
	fi

	# 3. Credential NAMES only (C03 rule 6).
	write_env_names "$env_file" "$bundle/config/env-names.txt"

	# 4. Value-free stack provenance for the restore-time sanity checks.
	{
		printf '{\n'
		printf '  "project": %s,\n' "$(json_quote "$project")"
		printf '  "compose_file": %s,\n' "$(json_quote "$(basename "$compose_file")")"
		printf '  "pg_user": %s,\n' "$(json_quote "$PG_USER")"
		printf '  "databases": [%s, %s],\n' "$(json_quote "$MAIN_DATABASE")" "$(json_quote "$MEMORY_DATABASE")"
		printf '  "created_at": %s\n' "$(json_quote "$created_at")"
		printf '}\n'
	} >"$bundle/config/stack.json"

	# 5. SHA256SUMS (sorted, relative paths, no self-reference) then the
	#    manifest that pins the sheet's own digest.
	(
		cd "$bundle"
		find postgres files config -type f -print0 | sort -z |
			xargs -0 sha256sum >SHA256SUMS
	)
	write_bundle_manifest "$staging" "$created_at"
	build_bundle "$staging" "$output"

	local digest bytes
	digest=$(sha256_of "$output")
	bytes=$(stat -c '%s' "$output")
	emit_summary "{\"backup\": true, \"archive\": $(json_quote "$output"), \"bytes\": $bytes, \"sha256\": $(json_quote "$digest"), \"databases\": [$(json_quote "$MAIN_DATABASE"), $(json_quote "$MEMORY_DATABASE")]}"
}

emit_summary() { printf '%s\n' "$1"; }

# ---------------------------------------------------------------------------
# verify (checksum-only, zero effects)
# ---------------------------------------------------------------------------

cmd_verify() {
	[ $# -eq 1 ] || { usage >&2; return "$ERR_USAGE"; }
	local archive=$1 extract out bytes
	extract=$(mktemp -d "${TMPDIR:-/tmp}/mai-verify-XXXXXX")
	CLEANUP_DIR=$extract
	out=$(verify_bundle "$archive" "$extract")
	bytes=$(stat -c '%s' "$archive")
	emit_summary "{\"verify\": true, \"archive\": $(json_quote "$archive"), \"members\": ${out#members=}, \"bytes\": $bytes}"
}

# ---------------------------------------------------------------------------
# restore (verify FIRST, freshness guard, then producer-driven effects)
# ---------------------------------------------------------------------------

pg_freshness_guard() { # pg_freshness_guard <container>
	local container=$1 tables dbos_schema hindsight_exists hindsight_tables
	tables=$(pg_psql "$container" "$MAIN_DATABASE" --tuples-only --no-align --command \
		"SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')") ||
		fail pg_unreachable "target Postgres is not reachable (database $MAIN_DATABASE)"
	[ "$tables" = "0" ] ||
		fail database_not_fresh "target $MAIN_DATABASE database already holds tables; restore needs a freshly provisioned Postgres"
	dbos_schema=$(pg_psql "$container" "$MAIN_DATABASE" --tuples-only --no-align --command \
		"SELECT count(*) FROM pg_catalog.pg_namespace WHERE nspname = 'dbos'")
	[ "$dbos_schema" = "0" ] ||
		fail database_not_fresh "target already carries a dbos engine schema; restore needs a freshly provisioned Postgres"
	hindsight_exists=$(pg_psql "$container" postgres --tuples-only --no-align --command \
		"SELECT count(*) FROM pg_catalog.pg_database WHERE datname = '$MEMORY_DATABASE'")
	if [ "$hindsight_exists" != "0" ]; then
		hindsight_tables=$(pg_psql "$container" "$MEMORY_DATABASE" --tuples-only --no-align --command \
			"SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')")
		[ "$hindsight_tables" = "0" ] ||
			fail database_not_fresh "target $MEMORY_DATABASE database already holds tables; restore needs a freshly provisioned Postgres"
	fi
}

load_dump() { # load_dump <container> <database> <dump.sql.gz>
	local container=$1 database=$2 dump=$3
	if ! gunzip -c "$dump" | pg_exec -i "$container" psql --username="$PG_USER" \
		--set=ON_ERROR_STOP=1 --quiet --dbname="$database" >/dev/null; then
		fail restore_load_failed "loading database $database failed; the target Postgres is disposable — reprovision it and rerun"
	fi
}

cmd_restore() {
	local archive='' data_root='' compose_file="$REPO_ROOT/compose.yaml" project=machina
	[ $# -ge 1 ] || { usage >&2; return "$ERR_USAGE"; }
	archive=$1; shift
	while [ $# -gt 0 ]; do
		case "$1" in
		--data-root) data_root=$2; shift 2 ;;
		--compose-file) compose_file=$2; shift 2 ;;
		--project) project=$2; shift 2 ;;
		*) usage >&2; return "$ERR_USAGE" ;;
		esac
	done
	[ -n "$archive" ] && [ -n "$data_root" ] || { usage >&2; return "$ERR_USAGE"; }

	# 1. Verify everything BEFORE any target effect (and before the
	#    Postgres seam: a tampered archive is refused even without docker).
	local extract out members
	extract=$(mktemp -d "${TMPDIR:-/tmp}/mai-restore-XXXXXX")
	CLEANUP_DIR=$extract
	out=$(verify_bundle "$archive" "$extract")
	members=${out#members=}

	local container
	container=$(resolve_pg_container "$compose_file" "$project")

	# 2. Freshness guard before the first restored byte.
	pg_freshness_guard "$container"

	local bundle="$extract/$BUNDLE_ROOT_NAME"

	# 3. File half through the reviewed producer: it re-verifies
	#    internally (traversal/symlink members), refuses a non-clean
	#    target and lands every entry 0o600/0o700. Runs BEFORE the
	#    database load so a rejected file half leaves the (fresh)
	#    database untouched.
	if ! mai_cli -m mai.cli restore --from "$bundle/files/data-root.tar.gz" --data-root "$data_root" >/dev/null; then
		fail cli_failed "mai.cli restore rejected the archive or target"
	fi

	# 4. Database load into the verified-fresh Postgres.
	if [ "$(pg_psql "$container" postgres --tuples-only --no-align --command \
		"SELECT count(*) FROM pg_catalog.pg_database WHERE datname = '$MEMORY_DATABASE'")" = "0" ]; then
		pg_exec "$container" createdb --username="$PG_USER" --owner="$PG_USER" "$MEMORY_DATABASE" ||
			fail restore_load_failed "creating the $MEMORY_DATABASE database failed"
	fi
	load_dump "$container" "$MAIN_DATABASE" "$bundle/postgres/$MAIN_DATABASE.sql.gz"
	load_dump "$container" "$MEMORY_DATABASE" "$bundle/postgres/$MEMORY_DATABASE.sql.gz"

	# 5. Never auto-rerun anything: no worker, no engine, no resume. The
	#    dumped PENDING/ERROR rows surface for deliberate operator review
	#    before any worker relaunch (docs/operations/recovery.md).
	emit_summary "{\"restore\": true, \"data_root\": $(json_quote "$data_root"), \"members\": $members, \"databases\": [$(json_quote "$MAIN_DATABASE"), $(json_quote "$MEMORY_DATABASE")], \"engine_auto_start\": false, \"operator_next_steps\": [\"inspect pending/error runs via the reviewed jobs surface BEFORE relaunching any worker (a stale dangerous run is cancelled deliberately, never resumed by this script)\", \"reconcile every scope so the derived index converges from the canonical files (POST /api/v1/docs/index/reconcile per scope)\", \"provision the .env credential values deliberately; the archive only preserves credential NAMES\"]}"
}

# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

main() {
	[ $# -ge 1 ] || { usage >&2; return "$ERR_USAGE"; }
	case "$1" in
	backup) shift; cmd_backup "$@" ;;
	verify) shift; cmd_verify "$@" ;;
	restore) shift; cmd_restore "$@" ;;
	-h | --help | help) usage ;;
	*) usage >&2; return "$ERR_USAGE" ;;
	esac
}

main "$@"
