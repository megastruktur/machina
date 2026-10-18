#!/usr/bin/env bash
# MAI scoped key rotation + host deprovisioning (card 12-uninstall, P12).
#
# The exact inverse of scripts/install-host.sh (card 12-bootstrap): it removes
# ONLY artifacts that installer created and never touches pre-existing host
# state. Run AS ROOT on the target host by its administrator. Two explicit
# modes:
#
#   ROTATE  (--rotate-key FILE --public-key FILE)
#     Replace the controller's authorized key line with a new one in place:
#     the old controller key line is removed, the new line appended, every
#     other line (unrelated admins, break-glass keys) preserved byte-for-byte.
#     Host trust is verifiable and unchanged: sshd/host keys are never read
#     for modification, the host fingerprint is printed for comparison and the
#     canonical profile (host_key, credential_ref) needs no edit.
#
#   UNINSTALL  (--keep-data | --delete-data)
#     Consent-gated teardown of exactly the installer's artifacts:
#       1. stack: docker compose down (volumes purged ONLY with --delete-data)
#       2. firewall: the ONE scoped rule (exact address+port match); neighbor
#          rules are never touched; blanket/broader-than-/8 inputs refused
#       3. sudoers: /etc/sudoers.d/90-mai-<user> ONLY when its first line is
#          the installer's managed marker; a foreign file at that path refuses
#          the whole run before anything is applied
#       4. authorized_keys: ONLY the exact controller key line; foreign lines
#          preserved byte-for-byte; the file/directory is never deleted
#       5. controller data: through the stable operator CLI
#          (`python -m mai.cli uninstall`, card 12-cli-ops) with the explicit
#          keep/delete intent — an inferred absent flag must never become
#          delete (C09 rule 5). --delete-data refuses foreign top-level
#          entries in the data root; managed application data is never
#          removed implicitly.
#       6. service account: KEPT unless --remove-user is given, and then only
#          when the home directory holds no files beyond .ssh (key-only
#          account as provisioned). Pre-existing accounts are preserved.
#
# Retention: --keep-data retains the controller data root and the compose
# .env; see docs/operations/uninstall.md for the retention explanation.
#
# Exit codes: 0 success/plan printed · 1 refusal or step failure · 2 usage.
#
# Test seams (documented, unset in production):
#   MAI_UNINSTALL_SKIP_ROOT_CHECK=1  allow running unprivileged (sandbox tests).
#   MAI_UNINSTALL_SANDBOX=DIR        prefix /etc/sudoers.d paths with DIR.
#   MAI_UNINSTALL_CLI="CMD..."       explicit mai.cli command prefix (default:
#                                    `uv run --frozen --project <repo>/backend
#                                    python -m mai.cli`, falling back to
#                                    <repo>/backend/.venv/bin/python).

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# constants and frozen vocabulary (must mirror scripts/install-host.sh)
# ---------------------------------------------------------------------------

SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(dirname "$SCRIPT_PATH")
DEFAULT_REPO=$(dirname "$SCRIPT_DIR")
MANAGED_MARKER="# managed by scripts/install-host.sh"
STACK_CORE="core"

usage() {
	cat <<'USAGE'
usage (rotate):  uninstall-host.sh --rotate-key NEW_PUBKEY --public-key OLD_PUBKEY [options]
usage (remove):  uninstall-host.sh --data-root DIR --keep-data|--delete-data
                                 --public-key KEY --controller-address ADDR
                                 [options]

rotation mode:
  --rotate-key FILE         new controller public key (single OpenSSH line);
                            replaces the OLD key's exact authorized_keys line.
  --public-key FILE         the OLD controller public key currently
                            authorized (exact line to revoke).

uninstall mode (exactly one data intent, never inferred):
  --keep-data               retain the controller data root and .env.
  --delete-data             delete the controller data root through
                            `mai.cli uninstall --delete-data` and purge
                            MAI-owned compose volumes; refuses foreign
                            top-level entries in the data root.

shared options:
  --data-root DIR           controller data root (uninstall mode only;
                            C02 rule 1: explicit, never inferred).
  --controller-address ADDR scoped firewall rule to remove (uninstall mode
                            only). Blanket targets (0.0.0.0/0, ::/0, any) and
                            broader-than-/8 prefixes (/0–/7) are refused.
  --ssh-port PORT           port of the scoped rule (default 22).
  --ssh-user NAME           service account (default: mai).
  --repo DIR                repository root holding compose.yaml and backend/
                            (default: the directory above this script).
  --env-file FILE           compose .env path (default: <repo>/.env); never
                            read for secrets and never modified or deleted.
  --stack-profiles LIST     comma list of core,worker,search,memory to mirror
                            the stack that was started (default: core).
  --host-key-pub FILE       SSH host public key to fingerprint in rotate mode
                            (default: /etc/ssh/ssh_host_ed25519_key.pub, then
                            ecdsa, then rsa).
  --remove-user             (uninstall mode) also delete the service account
                            — ONLY when its home holds nothing beyond .ssh.
  --print-plan              print the exact plan/inventory (read-only) and
                            exit 0 WITHOUT applying anything.
  --yes                     non-interactive consent (a tty otherwise asks).
  -h | --help               this help.

Test seams (documented, unset in production):
  MAI_UNINSTALL_SKIP_ROOT_CHECK=1  allow running unprivileged (sandbox tests).
  MAI_UNINSTALL_SANDBOX=DIR        prefix /etc/sudoers.d paths with DIR.
  MAI_UNINSTALL_CLI="CMD..."       explicit mai.cli command prefix.
USAGE
}

die() { # die EXIT "message..."
	local code=$1
	shift
	printf 'uninstall-host: %s\n' "$*" >&2
	exit "$code"
}
usage_error() { usage >&2; printf '\nuninstall-host: %s\n' "$*" >&2; exit 2; }

# ---------------------------------------------------------------------------
# argument parsing and validation (no side effects before this completes)
# ---------------------------------------------------------------------------

MODE="" ROTATE_FILE="" PUBKEY_FILE="" DATA_ROOT="" KEEP_DATA=0 DELETE_DATA=0
CONTROLLER_ADDR="" SSH_PORT="22" SSH_USER="mai"
REPO="$DEFAULT_REPO" ENV_FILE="" STACK_PROFILES="core" HOST_KEY_PUB=""
REMOVE_USER=0 PRINT_PLAN=0 ASSUME_YES=0

is_port() { [[ $1 =~ ^[0-9]+$ ]] && ((1 <= $1 && $1 <= 65535)); }
is_printable() { [[ -n $1 && ! $1 =~ [[:space:]\"\'] ]]; }

# Same refusal grammar as the installer: the rule we remove is scoped to ONE
# controller; a blanket/broader-than-/8 target was never installer-written.
validate_controller_address() {
	local addr=$1 octet
	[[ $addr =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})(/([0-9]|[12][0-9]|3[0-2]))?$ ]] ||
		die 2 "controller address '$addr' is not an IPv4 address or CIDR"
	local i
	for i in 1 2 3 4; do
		octet=${BASH_REMATCH[i]}
		((octet <= 255)) || die 2 "controller address '$addr' has an invalid octet"
	done
	case $addr in
		0.0.0.0 | 0.0.0.0/0 | any | any/0)
			die 2 "blanket controller address '$addr' refused: only the exact scoped rule is ever removed, never a blanket" ;;
	esac
	if [[ -n ${BASH_REMATCH[6]} ]] && ((10#${BASH_REMATCH[6]} < 8)); then
		die 2 "blanket controller address '$addr' refused: /${BASH_REMATCH[6]} covers far more than one controller — use a /8 or narrower prefix"
	fi
}

while [[ $# -gt 0 ]]; do
	case $1 in
		--rotate-key) ROTATE_FILE=${2:-}; shift 2 ;;
		--public-key) PUBKEY_FILE=${2:-}; shift 2 ;;
		--data-root) DATA_ROOT=${2:-}; shift 2 ;;
		--keep-data) KEEP_DATA=1; shift ;;
		--delete-data) DELETE_DATA=1; shift ;;
		--controller-address) CONTROLLER_ADDR=${2:-}; shift 2 ;;
		--ssh-port) SSH_PORT=${2:-}; shift 2 ;;
		--ssh-user) SSH_USER=${2:-}; shift 2 ;;
		--repo) REPO=${2:-}; shift 2 ;;
		--env-file) ENV_FILE=${2:-}; shift 2 ;;
		--stack-profiles) STACK_PROFILES=${2:-}; shift 2 ;;
		--host-key-pub) HOST_KEY_PUB=${2:-}; shift 2 ;;
		--remove-user) REMOVE_USER=1; shift ;;
		--print-plan) PRINT_PLAN=1; shift ;;
		--yes) ASSUME_YES=1; shift ;;
		-h | --help) usage; exit 0 ;;
		*) usage_error "unknown option: $1" ;;
	esac
done

# Mode selection: rotation and uninstall are exclusive; the data intent is
# never inferred (C09 rule 5).
if [[ -n $ROTATE_FILE ]]; then
	MODE="rotate"
	((KEEP_DATA == 0 && DELETE_DATA == 0)) || usage_error "--rotate-key is rotation only: it cannot be combined with --keep-data/--delete-data"
	[[ -z $CONTROLLER_ADDR ]] || usage_error "--rotate-key is rotation only: it cannot be combined with --controller-address"
	[[ -z $DATA_ROOT ]] || usage_error "--rotate-key is rotation only: it cannot be combined with --data-root"
	((REMOVE_USER == 0)) || usage_error "--rotate-key is rotation only: it cannot be combined with --remove-user"
	[[ -n $PUBKEY_FILE ]] || usage_error "rotation requires --public-key: the OLD controller key whose exact line is replaced"
else
	MODE="uninstall"
	((KEEP_DATA + DELETE_DATA == 1)) || usage_error "exactly one of --keep-data / --delete-data is required (an inferred data intent must never become delete)"
	[[ -n $DATA_ROOT ]] || usage_error "--data-root is required (C02 rule 1: the data root is explicit, never inferred)"
	[[ -n $PUBKEY_FILE ]] || usage_error "--public-key is required: the controller key whose exact authorized_keys line is revoked"
	[[ -n $CONTROLLER_ADDR ]] || usage_error "--controller-address is required: only the exact scoped rule is ever removed"
fi

is_port "$SSH_PORT" || die 2 "--ssh-port must be 1-65535, got '$SSH_PORT'"
if [[ $MODE == uninstall ]]; then
	validate_controller_address "$CONTROLLER_ADDR"
fi
is_printable "$SSH_USER" || die 2 "--ssh-user must be a plain name"
[[ $SSH_USER =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die 2 "--ssh-user '$SSH_USER' is not a valid account name"
[[ -f $PUBKEY_FILE && -r $PUBKEY_FILE ]] || die 2 "public key file '$PUBKEY_FILE' is not a readable file"
PUBKEY_LINE=$(tr -d '\r' <"$PUBKEY_FILE")
[[ $(printf '%s\n' "$PUBKEY_LINE" | grep -c '[^[:space:]]') -eq 1 ]] ||
	die 2 "public key file must contain exactly one key line"
PUBKEY_LINE=$(printf '%s\n' "$PUBKEY_LINE" | grep '[^[:space:]]' | head -n1)
[[ $PUBKEY_LINE =~ ^(ssh-(ed25519|rsa)|ecdsa-sha2-[a-z0-9-]+|sk-(ssh-ed25519|ecdsa-sha2-[a-z0-9-]+)@openssh\.com)\ [A-Za-z0-9+/=]+([[:space:]][A-Za-z0-9@._-]+)?$ ]] ||
	die 2 "public key file does not contain a recognized OpenSSH public key line"

if [[ $MODE == rotate ]]; then
	[[ -f $ROTATE_FILE && -r $ROTATE_FILE ]] || die 2 "new key file '$ROTATE_FILE' is not a readable file"
	NEW_LINE=$(tr -d '\r' <"$ROTATE_FILE")
	[[ $(printf '%s\n' "$NEW_LINE" | grep -c '[^[:space:]]') -eq 1 ]] ||
		die 2 "new key file must contain exactly one key line"
	NEW_LINE=$(printf '%s\n' "$NEW_LINE" | grep '[^[:space:]]' | head -n1)
	[[ $NEW_LINE =~ ^(ssh-(ed25519|rsa)|ecdsa-sha2-[a-z0-9-]+|sk-(ssh-ed25519|ecdsa-sha2-[a-z0-9-]+)@openssh\.com)\ [A-Za-z0-9+/=]+([[:space:]][A-Za-z0-9@._-]+)?$ ]] ||
		die 2 "new key file does not contain a recognized OpenSSH public key line"
	[[ $NEW_LINE != "$PUBKEY_LINE" ]] || die 2 "rotation with the identical key is a no-op: pass a different --rotate-key"
fi

# Uninstall-mode shared paths (validated like the installer; compose.yaml is
# OPTIONAL here — a removed repo must not block deprovisioning, the stack
# step then reports "compose-file-absent").
[[ -n $ENV_FILE ]] || ENV_FILE="$REPO/.env"
PROFILE_TOKENS=()
IFS=',' read -ra _requested_profiles <<<"$STACK_PROFILES"
for _p in "${_requested_profiles[@]}"; do
	_p=$(printf '%s' "$_p" | tr -d '[:space:]')
	[[ -n $_p ]] || continue
	case $_p in
		"$STACK_CORE") : ;; # the core set needs no --profile token
		worker | search | memory) PROFILE_TOKENS+=("$_p") ;;
		*) die 2 "--stack-profiles token '$_p' is not one of core,worker,search,memory" ;;
	esac
done
[[ -n ${_requested_profiles[*]// /} ]] || die 2 "--stack-profiles must not be empty"

# Root precondition: deprovisioning removes /etc/sudoers.d state, firewall
# rules and accounts; an unprivileged run would silently half-revoke and
# report success. MAI_UNINSTALL_SKIP_ROOT_CHECK=1 is the documented test-only
# seam (see header) and is never set in production. Every option — including
# `--help` — has already been handled above, so that path never reaches this
# gate: the refusal happens BEFORE plan/consent/apply.
if ((EUID != 0)) && [[ ${MAI_UNINSTALL_SKIP_ROOT_CHECK:-0} != 1 ]]; then
	die 1 "must run as root (EUID 0): deprovisioning removes the managed sudoers grant, the scoped firewall rule and (optionally) the service account — MAI_UNINSTALL_SKIP_ROOT_CHECK=1 is the sandbox-test seam only"
fi

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

etc_path() { printf '%s%s' "${MAI_UNINSTALL_SANDBOX:-}" "$1"; }
SUDOERS_FILE=$(etc_path "/etc/sudoers.d/90-mai-${SSH_USER}")

# Stable CLI invocation (12-cli-ops contract: scripts call the actual CLI).
mai_cli() {
	if [[ -n ${MAI_UNINSTALL_CLI:-} ]]; then
		local -a parts
		read -ra parts <<<"$MAI_UNINSTALL_CLI"
		"${parts[@]}" "$@"
	elif command -v uv >/dev/null 2>&1; then
		(cd "$REPO/backend" && uv run --frozen python -m mai.cli "$@")
	elif [[ -x $REPO/backend/.venv/bin/python ]]; then
		"$REPO/backend/.venv/bin/python" -m mai.cli "$@"
	else
		die 1 "no way to run the mai CLI: install uv or create $REPO/backend/.venv (uv sync)"
	fi
}

# Host SSH identity: presented for verification, NEVER changed (C04 rule 1:
# rotation/deprovisioning keeps host trust; re-enrollment is an explicit admin
# action elsewhere). Read-only.
resolve_host_fingerprint() {
	local -a candidates=()
	if [[ -n $HOST_KEY_PUB ]]; then
		candidates+=("$HOST_KEY_PUB")
	else
		candidates+=(/etc/ssh/ssh_host_ed25519_key.pub /etc/ssh/ssh_host_ecdsa_key.pub /etc/ssh/ssh_host_rsa_key.pub)
	fi
	local pub line fp
	for pub in "${candidates[@]}"; do
		[[ -r $pub ]] || continue
		line=$(ssh-keygen -lf "$pub" 2>/dev/null) || continue
		fp=$(printf '%s\n' "$line" | awk '{print $2}')
		[[ $fp =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]] || continue
		printf '%s\n' "$fp"
		return 0
	done
	die 1 "no SSH host public key with a SHA256 fingerprint found (looked at: ${candidates[*]})"
}

account_home() { getent passwd "$SSH_USER" | cut -d: -f6; }
account_exists() { getent passwd "$SSH_USER" >/dev/null 2>&1; }

firewall_manager() {
	# Capture-then-match, never `cmd | grep -q` (pipefail SIGPIPE discipline).
	local status
	if command -v ufw >/dev/null 2>&1; then
		status=$(ufw status 2>/dev/null || true)
		if [[ $status == *"Status: active"* ]]; then
			printf 'ufw'
			return 0
		fi
	fi
	if command -v firewall-cmd >/dev/null 2>&1; then
		status=$(firewall-cmd --state 2>/dev/null || true)
		if [[ $status == *running* ]]; then
			printf 'firewalld'
			return 0
		fi
	fi
	printf 'none'
}
FW=$(firewall_manager)

# Read-only preflight: the CURRENT state of every installer artifact, so the
# plan is an honest inventory and refusals (foreign files, foreign homes)
# happen BEFORE anything is applied.
SUDOERS_STATE="absent"
if [[ -f $SUDOERS_FILE ]]; then
	if [[ $(head -n1 "$SUDOERS_FILE") == "$MANAGED_MARKER"* ]]; then
		SUDOERS_STATE="managed"
	else
		SUDOERS_STATE="foreign"
		die 1 "refusing to uninstall: $SUDOERS_FILE exists but was not written by scripts/install-host.sh (foreign file) — this uninstaller never deletes files it did not write; remove it explicitly yourself if it is truly obsolete"
	fi
fi

# ---
# Rotation-mode preflight: the OLD key must currently be present; otherwise
# "rotate" would silently ADD authority it cannot tie to the controller.
ROTATE_OLD_PRESENT=0
if [[ $MODE == rotate ]]; then
	if account_exists; then
		home=$(account_home)
		if [[ -f $home/.ssh/authorized_keys ]] && grep -qxF "$PUBKEY_LINE" "$home/.ssh/authorized_keys"; then
			ROTATE_OLD_PRESENT=1
		fi
	fi
	if ((ROTATE_OLD_PRESENT == 0)); then
		die 1 "old controller key is not present in $SSH_USER's authorized_keys — nothing to rotate (refusing to add new authority blind); install the new key explicitly or check --ssh-user"
	fi
fi

# Uninstall-mode preflight: a foreign home blocks --remove-user before any
# other step runs.
HOME_FOREIGN_ENTRIES=()
if [[ $MODE == uninstall && $REMOVE_USER == 1 ]] && account_exists; then
	home=$(account_home)
	if [[ -n $home && -d $home ]]; then
		while IFS= read -r entry; do
			[[ $entry == .ssh ]] || HOME_FOREIGN_ENTRIES+=("$entry")
		done < <(find "$home" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort)
	fi
fi
if ((${#HOME_FOREIGN_ENTRIES[@]} > 0)); then
	die 1 "refusing --remove-user: home $home holds entries beyond .ssh (${HOME_FOREIGN_ENTRIES[*]}) — this is not the key-only account this installer provisions; revoke the key/grants instead and handle the home manually"
fi

# ---
# read-only plan / inventory

print_plan() {
	if [[ $MODE == rotate ]]; then
		cat <<EOF
== mai rotate plan (nothing has been applied) ==
host ssh fingerprint : $(resolve_host_fingerprint)  (verified, NEVER changed — the canonical profile needs no edit)
account              : $SSH_USER
revoke line          : ${PUBKEY_LINE%% *} … (exact match only; all other authorized_keys lines preserved byte-for-byte)
install line         : ${NEW_LINE%% *} … (append-only; no-op if already present)
untouched            : sudoers, firewall, stack, data root, .env, sshd/host keys
EOF
		return 0
	fi
	cat <<EOF
== mai uninstall plan / inventory (nothing has been applied) ==
data intent          : $(if ((DELETE_DATA)); then printf 'DELETE data root + MAI-owned compose volumes'; else printf 'KEEP data root and .env (retention: docs/operations/uninstall.md)'; fi)
data root            : $DATA_ROOT
stack                : $(if [[ -f $REPO/compose.yaml ]]; then printf 'docker compose down (%s)' "$(if ((DELETE_DATA)); then printf -- '-v --remove-orphans, '; fi)profiles: core$([[ ${#PROFILE_TOKENS[@]} -gt 0 ]] && printf ' %s' "${PROFILE_TOKENS[*]}")"; else printf 'compose.yaml absent under %s — nothing to tear down' "$REPO"; fi)
sudoers              : $(case $SUDOERS_STATE in
	managed) printf '%s — managed file, will be removed' "$SUDOERS_FILE" ;;
	foreign) printf '%s — FOREIGN file (not installer-written): the run REFUSES before applying anything' "$SUDOERS_FILE" ;;
	absent) printf '%s — already absent' "$SUDOERS_FILE" ;;
esac)
firewall             : $(case $FW in
	ufw) printf 'ufw — will remove ONLY the rule "ufw allow from %s to any port %s proto tcp" (neighbors untouched)' "$CONTROLLER_ADDR" "$SSH_PORT" ;;
	firewalld) printf 'firewalld — will remove ONLY the rich rule scoped to source %s, port %s/tcp' "$CONTROLLER_ADDR" "$SSH_PORT" ;;
	none) printf 'no active firewall manager detected — no rule to remove' ;;
esac)
authorized_keys      : revoke ONLY the exact line ${PUBKEY_LINE%% *} … (foreign lines preserved; file/directory never deleted)
service account      : $(if ((REMOVE_USER)); then printf '%s — will be removed ONLY if its home holds nothing beyond .ssh (preflight passed)' "$SSH_USER"; else printf '%s — KEPT (pass --remove-user to remove a key-only provisioned account)' "$SSH_USER"; fi)
data CLI             : mai.cli uninstall --data-root $DATA_ROOT $(if ((DELETE_DATA)); then printf -- '--delete-data'; else printf -- '--keep-data'; fi)
untouched            : .env ($ENV_FILE), sshd/host keys, unrelated accounts/keys/rules
EOF
}

if ((PRINT_PLAN)); then
	print_plan
	exit 0
fi

if (( ! ASSUME_YES )); then
	if [[ ! -t 0 ]]; then
		usage_error "refusing to apply without consent: pass --yes for non-interactive runs (stdin is not a tty)"
	fi
	print_plan
	printf 'Type exactly "%s" to apply the plan above: ' "$MODE"
	read -r reply
	[[ $reply == "$MODE" ]] || die 1 "aborted: no consent given (nothing was changed)"
fi

# ---------------------------------------------------------------------------
# apply — uninstall mode: revoke access first, data decision last
# ---------------------------------------------------------------------------

if [[ $MODE == uninstall ]]; then
	# step 1: stack teardown (never implicitly purges volumes)
	if [[ -f $REPO/compose.yaml ]]; then
		compose_down() {
			local -a args=(docker compose -f "$REPO/compose.yaml" --env-file "$ENV_FILE")
			local p
			for p in "${PROFILE_TOKENS[@]}"; do
				args+=(--profile "$p")
			done
			args+=(down)
			if ((DELETE_DATA)); then
				args+=(-v --remove-orphans) # explicit purge: MAI-owned named volumes only
			fi
			"${args[@]}"
		}
		printf 'stack: stopping via docker compose down%s...\n' "$(if ((DELETE_DATA)); then printf ' (-v --remove-orphans: explicit purge)'; fi)"
		compose_down
		STACK_STATE="down"
		printf 'stack: stopped\n'
	else
		STACK_STATE="compose-file-absent"
		printf 'stack: compose.yaml absent under %s — nothing to tear down\n' "$REPO"
	fi

	# step 2: firewall — remove ONLY the exact scoped rule (never neighbors)
	remove_firewall_rule() {
		local removed rule present=0
		case $FW in
			ufw)
				added=$(ufw show added 2>/dev/null || true)
				while IFS= read -r rule; do
					if [[ $rule == *"from $CONTROLLER_ADDR "* && $rule == *"to any port $SSH_PORT proto tcp"* ]]; then
						present=1
						break
					fi
				done <<<"$added"
				if (( ! present)); then
					FW_STATE="not-present"
					printf 'firewall: no scoped ufw rule for %s port %s — nothing removed\n' "$CONTROLLER_ADDR" "$SSH_PORT"
					return 0
				fi
				ufw delete allow from "$CONTROLLER_ADDR" to any port "$SSH_PORT" proto tcp >/dev/null
				FW_STATE="removed"
				printf 'firewall: scoped ufw rule removed (from %s to port %s/tcp only); all other rules untouched\n' "$CONTROLLER_ADDR" "$SSH_PORT"
				;;
			firewalld)
				rule="rule family=\"ipv4\" source address=\"$CONTROLLER_ADDR\" port port=\"$SSH_PORT\" protocol=\"tcp\" accept"
				added=$(firewall-cmd --permanent --list-rich-rules 2>/dev/null || true)
				if [[ $added == *"$rule"* ]]; then
					present=1
				fi
				if (( ! present)); then
					FW_STATE="not-present"
					printf 'firewall: no scoped firewalld rich rule for %s port %s — nothing removed\n' "$CONTROLLER_ADDR" "$SSH_PORT"
					return 0
				fi
				firewall-cmd --permanent --remove-rich-rule="$rule" >/dev/null
				firewall-cmd --reload >/dev/null
				FW_STATE="removed"
				printf 'firewall: scoped firewalld rich rule removed (source %s, port %s/tcp only); all other rules untouched\n' "$CONTROLLER_ADDR" "$SSH_PORT"
				;;
			none)
				FW_STATE="none-detected"
				printf 'firewall: no active firewall manager detected — nothing to remove\n'
				;;
		esac
	}
	remove_firewall_rule

	# step 3: sudoers — managed file only; a foreign file was refused in the
	# preflight above, so reaching this point means managed-or-absent.
	if [[ $SUDOERS_STATE == managed ]]; then
		rm -f -- "$SUDOERS_FILE"
		GRANT_STATE="removed"
		printf 'grants: managed sudoers file removed (%s)\n' "$SUDOERS_FILE"
	else
		GRANT_STATE="absent"
		printf 'grants: sudoers file already absent (%s)\n' "$SUDOERS_FILE"
	fi

	# step 4: authorized_keys — exact-line revocation, foreign lines preserved
	if account_exists; then
		AUTH_KEYS="$(account_home)/.ssh/authorized_keys"
		if [[ -f $AUTH_KEYS ]]; then
			matched=$(grep -cxF "$PUBKEY_LINE" "$AUTH_KEYS" || true)
			if ((matched == 0)); then
				KEY_STATE="not-present"
				printf 'authorized_keys: controller key line not present — file untouched (%s other line(s) preserved)\n' \
					"$(grep -c '[^[:space:]]' "$AUTH_KEYS")"
			else
				tmp=$(mktemp)
				grep -vxF "$PUBKEY_LINE" "$AUTH_KEYS" >"$tmp" || true
				current_mode=$(stat -c '%a' "$AUTH_KEYS")
				# Tighten only, same discipline as the installer.
				if ((8#$current_mode & 8#022)); then
					current_mode=600
				fi
				install -m "$current_mode" "$tmp" "$AUTH_KEYS"
				rm -f "$tmp"
				chown "$SSH_USER":"$SSH_USER" "$AUTH_KEYS" 2>/dev/null || true
				KEY_STATE="removed ($matched line(s))"
				printf 'authorized_keys: %s controller key line(s) removed — %s foreign line(s) preserved byte-for-byte\n' \
					"$matched" "$(grep -c '[^[:space:]]' "$AUTH_KEYS" || true)"
			fi
		else
			KEY_STATE="file-absent"
			printf 'authorized_keys: no authorized_keys file for %s — nothing revoked\n' "$SSH_USER"
		fi
	else
		KEY_STATE="account-absent"
		printf 'authorized_keys: account %s does not exist — nothing revoked\n' "$SSH_USER"
	fi

	# step 5: controller data root — through the stable CLI with the explicit
	# intent (the CLI refuses foreign entries on --delete-data: managed
	# application data is never removed implicitly).
	if [[ -d $DATA_ROOT ]]; then
		intent=--keep-data
		if ((DELETE_DATA)); then
			intent=--delete-data
		fi
		if ! DATA_CLI_OUT=$(mai_cli uninstall --data-root "$DATA_ROOT" "$intent"); then
			die 1 "mai.cli uninstall failed — the data root was left as-is (see the structured error above)"
		fi
		if ((DELETE_DATA)); then
			DATA_STATE="deleted"
		else
			DATA_STATE="retained"
		fi
		printf 'data root: %s (via mai.cli uninstall %s)\n' "$DATA_STATE" "$intent"
		printf '%s\n' "$DATA_CLI_OUT"
	elif ((DELETE_DATA)); then
		DATA_STATE="absent"
		printf 'data root: %s already absent — nothing to delete\n' "$DATA_ROOT"
	else
		DATA_STATE="absent"
		printf 'data root: %s already absent — nothing to retain\n' "$DATA_ROOT"
	fi

	# step 6: service account — kept unless explicitly requested (preflight
	# already refused foreign homes).
	if ((REMOVE_USER)); then
		if account_exists; then
			userdel -r "$SSH_USER"
			ACCOUNT_STATE="removed"
			printf 'account: %s removed (home and .ssh with it — preflight verified nothing else lived there)\n' "$SSH_USER"
		else
			ACCOUNT_STATE="absent"
			printf 'account: %s already absent\n' "$SSH_USER"
		fi
	else
		ACCOUNT_STATE="kept"
	fi

	# report — the exact inventory of what was and was not touched
	cat <<EOF
== uninstall complete ==
account       : $SSH_USER ($ACCOUNT_STATE$(if (( ! REMOVE_USER )); then printf ' — revoke with --remove-user'; fi))
key           : $KEY_STATE
grants        : $GRANT_STATE ($SUDOERS_FILE)
firewall      : $FW ($FW_STATE) — scoped rule only; neighbors untouched
stack         : $STACK_STATE (profiles: core$([[ ${#PROFILE_TOKENS[@]} -gt 0 ]] && printf ' %s' "${PROFILE_TOKENS[*]}")$(if ((DELETE_DATA)); then printf ', volumes purged'; fi))
data root     : $DATA_STATE $DATA_ROOT
env file      : KEPT $ENV_FILE (never read for secrets, never modified — delete manually if truly done: it may pre-date the install)
unrelated     : sshd/host keys, foreign authorized_keys lines, foreign firewall rules and sudoers files: untouched
EOF
	exit 0
fi

# ---------------------------------------------------------------------------
# apply — rotate mode: exact-line key swap, nothing else
# ---------------------------------------------------------------------------

printf 'host fingerprint: %s (unchanged — trust is verifiable, the canonical profile needs no edit)\n' "$(resolve_host_fingerprint)"

AUTH_KEYS="$(account_home)/.ssh/authorized_keys"
tmp=$(mktemp)
# Remove every exact OLD line, preserve the rest byte-for-byte.
grep -vxF "$PUBKEY_LINE" "$AUTH_KEYS" >"$tmp" || true
# Append the NEW line unless already present (rerun safety).
if ! grep -qxF "$NEW_LINE" "$tmp"; then
	if [[ -s $tmp && $(tail -c 1 -- "$tmp" | wc -l) -eq 0 ]]; then
		printf '\n' >>"$tmp"
	fi
	printf '%s\n' "$NEW_LINE" >>"$tmp"
fi
current_mode=$(stat -c '%a' "$AUTH_KEYS")
if ((8#$current_mode & 8#022)); then
	current_mode=600
fi
install -m "$current_mode" "$tmp" "$AUTH_KEYS"
rm -f "$tmp"
chown "$SSH_USER":"$SSH_USER" "$AUTH_KEYS" 2>/dev/null || true

cat <<EOF
== rotate complete ==
account       : $SSH_USER
old key       : revoked (exact line match)
new key       : authorized (single line, no duplicates)
preserved     : $(grep -c '[^[:space:]]' "$AUTH_KEYS") authorized_keys line(s) total — every unrelated line byte-identical
untouched     : sudoers, firewall, stack, data root, .env, sshd/host keys
EOF
