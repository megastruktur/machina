#!/usr/bin/env bash
# MAI explicit host-authorized SSH bootstrap installer (card 12-bootstrap, P12).
#
# Run AS ROOT on the fresh target host by its administrator. The installer:
#   1. presents the host's actual SSH identity (host-key fingerprint) and the
#      exact requested authority (account, authorized key, sudo grant scope,
#      scoped firewall rule, stack profiles) before anything is applied;
#   2. provisions the chosen service account + controller key + connectivity
#      — the firewall rule is always scoped to the controller address and the
#      SSH port, NEVER a blanket allow;
#   3. starts the selected stack (docker compose; core set + optional
#      worker/search/memory profiles) with secrets generated straight into a
#      mode-0600 .env that is never echoed;
#   4. records the canonical server pair through the stable operator CLI
#      (`python -m mai.cli bootstrap`, card 12-cli-ops) — never a guessed
#      endpoint or a second implementation.
#
# Rerun safety (acceptance 2): every step is idempotent and resume-safe.
# Existing accounts/keys/config are preserved or explicitly reconciled
# (--reconcile-grants); a grant-scope change without that flag is refused;
# foreign sudoers files, foreign authorized_keys lines and neighboring
# firewall rules are never touched; sshd/host-key configuration is never
# modified; no password or token is ever printed.
#
# Exit codes: 0 success/plan printed · 1 refusal or step failure · 2 usage.
#
# Test seams (documented, unset in production):
#   MAI_INSTALL_SKIP_ROOT_CHECK=1  allow running unprivileged (sandbox tests).
#   MAI_INSTALL_SANDBOX=DIR        prefix /etc/sudoers.d writes with DIR.
#   MAI_INSTALL_CLI="CMD..."       explicit mai.cli command prefix (default:
#                                  `uv run --frozen --project <repo>/backend
#                                  python -m mai.cli`, falling back to
#                                  <repo>/backend/.venv/bin/python).

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# constants and frozen vocabulary
# ---------------------------------------------------------------------------

SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(dirname "$SCRIPT_PATH")
DEFAULT_REPO=$(dirname "$SCRIPT_DIR")
MANAGED_MARKER="# managed by scripts/install-host.sh"
GRANT_NONE="none"
GRANT_NARROW="narrow"
GRANT_BROAD="broad"
STACK_CORE="core"

usage() {
	cat <<'USAGE'
usage: install-host.sh --server-id UUID --data-root DIR --public-key FILE
                       --grant-scope {none|narrow|broad} --controller-address ADDR
                       [options]

required:
  --server-id UUID          canonical opaque server id (RFC-4122 UUID).
  --data-root DIR           controller data root for `mai bootstrap` (C02
                            rule 1: explicit, never inferred).
  --public-key FILE         controller SSH public key to authorize for the
                            service account (single OpenSSH public-key line).
  --grant-scope SCOPE       none | narrow (explicit systemctl docker.service
                            allowlist) | broad (NOPASSWD: ALL, trusted-admin).
                            The chosen scope is presented before applying.
  --controller-address ADDR controller IPv4 address or CIDR the firewall rule
                            is scoped to. Blanket targets (0.0.0.0/0, ::/0,
                            any) and broader-than-/8 prefixes (/0–/7) are
                            refused.

options:
  --ssh-user NAME           service account (default: mai).
  --ssh-port PORT           SSH port (default: 22; used in the firewall rule).
  --ssh-host ADDR           address written into the canonical profile
                            (default: 127.0.0.1; set the address a REMOTE
                            controller uses to reach this host).
  --name NAME               canonical profile display name (default: hostname).
  --host-key-pub FILE       SSH host public key to fingerprint
                            (default: /etc/ssh/ssh_host_ed25519_key.pub, then
                            ecdsa, then rsa).
  --repo DIR                repository root holding compose.yaml and backend/
                            (default: the directory above this script).
  --env-file FILE           compose .env path (default: <repo>/.env).
  --stack-profiles LIST     comma list of core,worker,search,memory
                            (default: core = the default start set).
  --print-plan              print the exact plan (grant scope, rule, steps)
                            and exit 0 WITHOUT applying anything.
  --yes                     non-interactive consent (a tty otherwise asks).
  --reconcile-grants        explicitly replace/remove a previously
                            installer-managed sudoers file when the requested
                            scope differs. Never applies to foreign files.
  -h | --help               this help.

The stack starts in auth setup state: complete the one-shot browser
bootstrap yourself; this installer generates no admin password and never
prints a password or token.
USAGE
}

die() { # die EXIT "message..."
	local code=$1
	shift
	printf 'install-host: %s\n' "$*" >&2
	exit "$code"
}
usage_error() { usage >&2; printf '\ninstall-host: %s\n' "$*" >&2; exit 2; }

# ---------------------------------------------------------------------------
# argument parsing and validation (no side effects before this completes)
# ---------------------------------------------------------------------------

SERVER_ID="" DATA_ROOT="" PUBKEY_FILE="" GRANT_SCOPE="" CONTROLLER_ADDR=""
SSH_USER="mai" SSH_PORT="22" SSH_HOST="127.0.0.1" PROFILE_NAME="$(uname -n 2>/dev/null || echo mai-host)"
HOST_KEY_PUB="" REPO="$DEFAULT_REPO" ENV_FILE=""
STACK_PROFILES="core" PRINT_PLAN=0 ASSUME_YES=0 RECONCILE_GRANTS=0

is_uuid() { [[ $1 =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; }
is_port() { [[ $1 =~ ^[0-9]+$ ]] && ((1 <= $1 && $1 <= 65535)); }
is_printable() { [[ -n $1 && ! $1 =~ [[:space:]\"\'] ]]; }

# IPv4 with optional /prefix; refuses blanket targets outright (acceptance 1).
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
			die 2 "blanket controller address '$addr' refused: the firewall rule must scope the controller, never the world" ;;
	esac
	# A prefix below 8 bits (/0–/7) masks whole blocks of the internet — it is
	# a broad blanket, not a controller scope: refused exactly like 0.0.0.0/0.
	if [[ -n ${BASH_REMATCH[6]} ]] && ((10#${BASH_REMATCH[6]} < 8)); then
		die 2 "blanket controller address '$addr' refused: /${BASH_REMATCH[6]} covers far more than the controller — use a /8 or narrower prefix"
	fi
}

while [[ $# -gt 0 ]]; do
	case $1 in
		--server-id) SERVER_ID=${2:-}; shift 2 ;;
		--data-root) DATA_ROOT=${2:-}; shift 2 ;;
		--public-key) PUBKEY_FILE=${2:-}; shift 2 ;;
		--grant-scope) GRANT_SCOPE=${2:-}; shift 2 ;;
		--controller-address) CONTROLLER_ADDR=${2:-}; shift 2 ;;
		--ssh-user) SSH_USER=${2:-}; shift 2 ;;
		--ssh-port) SSH_PORT=${2:-}; shift 2 ;;
		--ssh-host) SSH_HOST=${2:-}; shift 2 ;;
		--name) PROFILE_NAME=${2:-}; shift 2 ;;
		--host-key-pub) HOST_KEY_PUB=${2:-}; shift 2 ;;
		--repo) REPO=${2:-}; shift 2 ;;
		--env-file) ENV_FILE=${2:-}; shift 2 ;;
		--stack-profiles) STACK_PROFILES=${2:-}; shift 2 ;;
		--print-plan) PRINT_PLAN=1; shift ;;
		--yes) ASSUME_YES=1; shift ;;
		--reconcile-grants) RECONCILE_GRANTS=1; shift ;;
		-h | --help) usage; exit 0 ;;
		*) usage_error "unknown option: $1" ;;
	esac
done

[[ -n $SERVER_ID ]] || usage_error "--server-id is required"
[[ -n $DATA_ROOT ]] || usage_error "--data-root is required (C02 rule 1: the data root is explicit, never inferred)"
[[ -n $PUBKEY_FILE ]] || usage_error "--public-key is required"
[[ -n $GRANT_SCOPE ]] || usage_error "--grant-scope is required: the authority this installer provisions must be chosen explicitly"
[[ -n $CONTROLLER_ADDR ]] || usage_error "--controller-address is required (the firewall rule is always scoped to the controller)"
is_uuid "$SERVER_ID" || die 2 "--server-id must be a UUID, got '$SERVER_ID'"
is_port "$SSH_PORT" || die 2 "--ssh-port must be 1-65535, got '$SSH_PORT'"
validate_controller_address "$CONTROLLER_ADDR"
is_printable "$SSH_USER" || die 2 "--ssh-user must be a plain name"
is_printable "$SSH_HOST" || die 2 "--ssh-host must be a plain address"
is_printable "$PROFILE_NAME" || die 2 "--name must be printable text without quotes"
[[ $SSH_USER =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die 2 "--ssh-user '$SSH_USER' is not a valid account name"
case $GRANT_SCOPE in
	"$GRANT_NONE" | "$GRANT_NARROW" | "$GRANT_BROAD") ;;
	*) die 2 "--grant-scope must be none|narrow|broad, got '$GRANT_SCOPE'" ;;
esac
PROFILE_TOKENS=()
IFS=',' read -ra _requested_profiles <<<"$STACK_PROFILES"
for _p in "${_requested_profiles[@]}"; do
	_p=$(printf '%s' "$_p" | tr -d '[:space:]')
	[[ -n $_p ]] || continue
	case $_p in
		"$STACK_CORE") : ;; # the core set is compose's default start set
		worker | search | memory) PROFILE_TOKENS+=("$_p") ;;
		*) die 2 "--stack-profiles token '$_p' is not one of core,worker,search,memory" ;;
	esac
done
[[ -n ${_requested_profiles[*]// /} ]] || die 2 "--stack-profiles must not be empty"
[[ -f $REPO/compose.yaml ]] || die 2 "compose.yaml not found under --repo '$REPO'"
[[ -d $REPO/backend ]] || die 2 "backend/ not found under --repo '$REPO'"
[[ -n $ENV_FILE ]] || ENV_FILE="$REPO/.env"

# The controller public key: one OpenSSH public-key line, read once.
[[ -f $PUBKEY_FILE && -r $PUBKEY_FILE ]] || die 2 "public key file '$PUBKEY_FILE' is not a readable file"
PUBKEY_LINE=$(tr -d '\r' <"$PUBKEY_FILE")
[[ $(printf '%s\n' "$PUBKEY_LINE" | grep -c '[^[:space:]]') -eq 1 ]] ||
	die 2 "public key file must contain exactly one key line"
PUBKEY_LINE=$(printf '%s\n' "$PUBKEY_LINE" | grep '[^[:space:]]' | head -n1)
[[ $PUBKEY_LINE =~ ^(ssh-(ed25519|rsa)|ecdsa-sha2-[a-z0-9-]+|sk-(ssh-ed25519|ecdsa-sha2-[a-z0-9-]+)@openssh\.com)\ [A-Za-z0-9+/=]+([[:space:]][A-Za-z0-9@._-]+)?$ ]] ||
	die 2 "public key file does not contain a recognized OpenSSH public key line"

# Root precondition: the installer provisions a service account, sudo grants
# and firewall rules; an unprivileged run would silently half-provision the
# host. MAI_INSTALL_SKIP_ROOT_CHECK=1 is the documented test-only seam (see
# header) and is never set in production. Every option — including `--help` —
# has already been handled above, so that path never reaches this gate.
if ((EUID != 0)) && [[ ${MAI_INSTALL_SKIP_ROOT_CHECK:-0} != 1 ]]; then
	die 1 "must run as root (EUID 0): this installer provisions the service account, its sudo grant and the scoped firewall rule — MAI_INSTALL_SKIP_ROOT_CHECK=1 is the sandbox-test seam only"
fi

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# Sandbox seam: /etc paths are the only absolute /etc state this installer
# writes; MAI_INSTALL_SANDBOX prefixes them for deterministic sandbox tests.
etc_path() { printf '%s%s' "${MAI_INSTALL_SANDBOX:-}" "$1"; }
SUDOERS_FILE=$(etc_path "/etc/sudoers.d/90-mai-${SSH_USER}")

gen_secret() { od -An -N24 -tx1 /dev/urandom | tr -d ' \n'; }

yaml_quote() { local s=$1; s=${s//\\/\\\\}; s=${s//\"/\\\"}; printf '"%s"' "$s"; }

credential_ref_of() {
	local ref
	ref=$(printf '%s' "$SSH_USER" | tr '[:lower:]-/' '[:upper:]__')
	printf 'MAI_SSH_KEY_%s' "$ref"
}

# Stable CLI invocation (12-cli-ops contract: scripts call the actual CLI).
mai_cli() {
	if [[ -n ${MAI_INSTALL_CLI:-} ]]; then
		local -a parts
		read -ra parts <<<"$MAI_INSTALL_CLI"
		"${parts[@]}" "$@"
	elif command -v uv >/dev/null 2>&1; then
		(cd "$REPO/backend" && uv run --frozen python -m mai.cli "$@")
	elif [[ -x $REPO/backend/.venv/bin/python ]]; then
		"$REPO/backend/.venv/bin/python" -m mai.cli "$@"
	else
		die 1 "no way to run the mai CLI: install uv or create $REPO/backend/.venv (uv sync)"
	fi
}

# Host SSH identity: fingerprint presented, NEVER changed (C04 rule 1: the
# host key is pinned; re-enrollment is an explicit admin action elsewhere).
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

FINGERPRINT=$(resolve_host_fingerprint)

grant_profile_privilege() {
	case $GRANT_SCOPE in
		"$GRANT_NONE") printf 'none' ;;
		"$GRANT_NARROW") printf 'sudo' ;;
		"$GRANT_BROAD") printf 'root' ;;
	esac
}

grant_sudoers_content() {
	if [[ $GRANT_SCOPE == "$GRANT_NARROW" ]]; then
		cat <<EOF
$MANAGED_MARKER — service account sudo grant (scope: narrow)
# change only by rerunning with --reconcile-grants; never hand-edit
$SSH_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start docker.service, /usr/bin/systemctl stop docker.service, /usr/bin/systemctl restart docker.service, /usr/bin/systemctl status docker.service
EOF
	elif [[ $GRANT_SCOPE == "$GRANT_BROAD" ]]; then
		cat <<EOF
$MANAGED_MARKER — service account sudo grant (scope: BROAD, trusted-admin)
# change only by rerunning with --reconcile-grants; never hand-edit
$SSH_USER ALL=(ALL) NOPASSWD: ALL
EOF
	fi
	# scope none intentionally produces no file
}

# ---------------------------------------------------------------------------
# plan (read-only) and consent
# ---------------------------------------------------------------------------

account_home() { getent passwd "$SSH_USER" | cut -d: -f6; }
account_exists() { getent passwd "$SSH_USER" >/dev/null 2>&1; }
account_state_label() {
	if account_exists; then
		printf '(exists, home: %s)' "$(account_home)"
	else
		printf '(will be created, key-only login, no password)'
	fi
}

firewall_manager() {
	# NOTE: capture-then-match, never `cmd | grep -q`: with pipefail, grep -q
	# closing the pipe early turns the producer's SIGPIPE into a false "no".
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

grant_exists() { [[ -f $SUDOERS_FILE ]]; }
key_installed() {
	local home=$1
	[[ -f $home/.ssh/authorized_keys ]] && grep -qxF "$PUBKEY_LINE" "$home/.ssh/authorized_keys"
}
env_file_complete() {
	grep -q '^MAI_POSTGRES_PASSWORD=.\+' "$ENV_FILE" 2>/dev/null || return 1
	if [[ ${#PROFILE_TOKENS[@]} -gt 0 && ${PROFILE_TOKENS[*]} == *search* ]]; then
		grep -q '^SEARXNG_SECRET=.\+' "$ENV_FILE" 2>/dev/null || return 1
	fi
	return 0
}

profile_step_state() {
	mai_cli status --data-root "$DATA_ROOT" --server-id "$SERVER_ID" >/dev/null 2>&1 && printf 'already-bootstrapped' || printf 'fresh'
}

print_plan() {
	cat <<EOF
== mai install plan (nothing has been applied) ==
host ssh fingerprint : $FINGERPRINT
account              : $SSH_USER $(account_state_label)
authorized key       : ${PUBKEY_LINE%% *} … into \$HOME/.ssh/authorized_keys (append-only; existing lines preserved)
grant scope          : $GRANT_SCOPE
sudoers file         : $(if [[ $GRANT_SCOPE == "$GRANT_NONE" ]]; then echo "(none — no sudo grant written)"; else printf '%s' "$SUDOERS_FILE"; fi)
$(if [[ $GRANT_SCOPE == "$GRANT_NARROW" || $GRANT_SCOPE == "$GRANT_BROAD" ]]; then grant_sudoers_content | sed 's/^/sudoers content      : /'; fi)
$(if [[ $GRANT_SCOPE == "$GRANT_BROAD" ]]; then echo 'WARNING             : broad = root-equivalent NOPASSWD authority for the service account; choose only for a trusted-admin configuration'; fi)
firewall             : $(case $FW in
	ufw) printf 'ufw — will ensure ONLY: ufw allow from %s to any port %s proto tcp' "$CONTROLLER_ADDR" "$SSH_PORT" ;;
	firewalld) printf 'firewalld — will ensure ONLY the rich rule scoped to source %s, port %s/tcp' "$CONTROLLER_ADDR" "$SSH_PORT" ;;
	none) printf 'no active firewall manager detected — no rule will be added or changed' ;;
esac)
env file             : $ENV_FILE (generates MAI_POSTGRES_PASSWORD$([[ ${PROFILE_TOKENS[*]} == *search* ]] && printf ' + SEARXNG_SECRET'); values are written mode 0600 and never printed)
stack                : core$([[ ${#PROFILE_TOKENS[@]} -gt 0 ]] && printf ' + %s' "${PROFILE_TOKENS[*]}") via docker compose up -d --wait
canonical pair       : mai.cli bootstrap --data-root $DATA_ROOT --server-id $SERVER_ID
                       (privilege: $(grant_profile_privilege), capabilities: [] — the installer records identity, it grants no runtime capability)
rerun safety         : existing account/key/env are preserved; grant changes require --reconcile-grants; no secret is ever printed
EOF
}

if ((PRINT_PLAN)); then
	print_plan
	exit 0
fi

# Read-only prerequisite for the rerun-safety decision; requires the CLI, so
# it stays AFTER the plan-only exit above.
BOOTSTRAP_STATE=$(profile_step_state)

if (( ! ASSUME_YES )); then
	if [[ ! -t 0 ]]; then
		usage_error "refusing to apply without consent: pass --yes for non-interactive runs (stdin is not a tty)"
	fi
	print_plan
	printf 'Type exactly "install" to apply the plan above: '
	read -r reply
	[[ $reply == install ]] || die 1 "aborted: no consent given (nothing was changed)"
fi

# ---------------------------------------------------------------------------
# step 1: service account (idempotent; key-only, no password is ever set)
# ---------------------------------------------------------------------------

if account_exists; then
	ACCOUNT_HOME=$(account_home)
	ACCOUNT_STATE="existing"
	printf 'account: %s already exists (home %s) — preserved\n' "$SSH_USER" "$ACCOUNT_HOME"
else
	useradd --create-home --shell /bin/bash --comment 'mai-controller' "$SSH_USER"
	ACCOUNT_HOME=$(account_home)
	[[ -n $ACCOUNT_HOME ]] || die 1 "account created but no home directory resolved"
	ACCOUNT_STATE="created"
	printf 'account: %s created (home %s, key-only login, no password)\n' "$SSH_USER" "$ACCOUNT_HOME"
fi
if command -v usermod >/dev/null 2>&1; then
	for grp in docker systemd-journal adm; do
		if getent group "$grp" >/dev/null 2>&1; then
			usermod -aG "$grp" "$SSH_USER" 2>/dev/null || true
		fi
	done
fi

# ---------------------------------------------------------------------------
# step 2: controller key into authorized_keys (append-only, never replace)
# ---------------------------------------------------------------------------

AUTH_KEYS="$ACCOUNT_HOME/.ssh/authorized_keys"
SSH_DIR="$ACCOUNT_HOME/.ssh"
if [[ -d $SSH_DIR ]]; then
	# Create-once, tighten-only: an existing .ssh directory keeps its owner
	# and any stricter mode; world access or group-write never survive.
	ssh_dir_mode=$(stat -c '%a' "$SSH_DIR")
	if (((8#$ssh_dir_mode & 8#077) != 0)); then
		chmod 700 "$SSH_DIR"
	fi
else
	install -d -m 700 -o "$SSH_USER" -g "$SSH_USER" "$SSH_DIR"
fi
if [[ -f $AUTH_KEYS ]]; then
	if grep -qxF "$PUBKEY_LINE" "$AUTH_KEYS"; then
		KEY_STATE="present"
		printf 'authorized_keys: controller key already present — preserved (%s lines untouched)\n' \
			"$(grep -c '[^[:space:]]' "$AUTH_KEYS")"
	else
		# Append without clobbering: keep every existing line, exact byte
		# content plus this one; only tighten the mode if it was looser.
		if [[ $(tail -c 1 -- "$AUTH_KEYS" | wc -l) -eq 0 ]]; then
			printf '\n' >>"$AUTH_KEYS"
		fi
		printf '%s\n' "$PUBKEY_LINE" >>"$AUTH_KEYS"
		KEY_STATE="appended"
		printf 'authorized_keys: controller key appended — %s existing line(s) preserved\n' \
			"$(grep -c '[^[:space:]]' "$AUTH_KEYS")"
	fi
	# Tighten only: world access or group-write never survive a rerun; an
	# already-stricter mode is left exactly as found.
	current_mode=$(stat -c '%a' "$AUTH_KEYS")
	if (((8#$current_mode & 8#022) != 0)); then
		chmod 600 "$AUTH_KEYS"
	fi
else
	umask 077
	printf '%s\n' "$PUBKEY_LINE" >"$AUTH_KEYS"
	chmod 600 "$AUTH_KEYS"
	umask 022
	KEY_STATE="created"
	printf 'authorized_keys: created with the controller key (mode 600)\n'
fi
chown "$SSH_USER":"$SSH_USER" "$AUTH_KEYS" 2>/dev/null || true

# ---------------------------------------------------------------------------
# step 3: sudo grant scope (explicit choice; explicit reconciliation only)
# ---------------------------------------------------------------------------

apply_grant_scope() {
	local intended tmp
	if [[ $GRANT_SCOPE == "$GRANT_NONE" ]]; then
		if grant_exists; then
			if [[ $(head -n1 "$SUDOERS_FILE") == "$MANAGED_MARKER"* ]]; then
				((RECONCILE_GRANTS)) || die 1 "grant scope 'none' but a managed sudoers file exists at $SUDOERS_FILE — removing a grant is an explicit change: rerun with --reconcile-grants"
				rm -f -- "$SUDOERS_FILE"
				GRANT_STATE="reconciled-removed"
				printf 'grants: managed sudoers file removed (scope none, explicit reconciliation)\n'
			else
				die 1 "refusing to touch $SUDOERS_FILE: it was not written by this installer (foreign file)"
			fi
		else
			GRANT_STATE="none"
			printf 'grants: scope none — no sudoers file written\n'
		fi
		return 0
	fi
	intended=$(grant_sudoers_content)
	tmp=$(mktemp)
	# Validate FIRST: an unusable grant file never reaches /etc, whatever the
	# current state is (fresh write, rerun or explicit reconciliation).
	printf '%s\n' "$intended" >"$tmp"
	if ! visudo -cf "$tmp" >/dev/null; then
		rm -f "$tmp"
		die 1 "requested sudoers content failed visudo validation — nothing written or changed"
	fi
	if grant_exists; then
		if [[ $(head -n1 "$SUDOERS_FILE") != "$MANAGED_MARKER"* ]]; then
			rm -f "$tmp"
			die 1 "refusing to touch $SUDOERS_FILE: it was not written by this installer (foreign file)"
		fi
		if cmp -s "$tmp" "$SUDOERS_FILE"; then
			GRANT_STATE="unchanged"
			printf 'grants: sudoers file already matches scope %s — unchanged\n' "$GRANT_SCOPE"
		else
			((RECONCILE_GRANTS)) || {
				rm -f "$tmp"
				die 1 "existing sudoers file differs from the requested scope $GRANT_SCOPE — silently changing authority is never done: rerun with --reconcile-grants to reconcile explicitly"
			}
			install -m 440 -o root -g root "$tmp" "$SUDOERS_FILE"
			GRANT_STATE="reconciled-replaced"
			printf 'grants: sudoers file reconciled to scope %s (explicit)\n' "$GRANT_SCOPE"
		fi
	else
		install -m 440 -o root -g root "$tmp" "$SUDOERS_FILE"
		GRANT_STATE="written"
		printf 'grants: sudoers file written for scope %s (mode 440, visudo-validated)\n' "$GRANT_SCOPE"
	fi
	rm -f "$tmp"
}
apply_grant_scope

# ---------------------------------------------------------------------------
# step 4: connectivity — scoped rule only, never a blanket allow
# ---------------------------------------------------------------------------

ensure_firewall_rule() {
	# Capture-then-match (pipefail-safe): a presence check must never read as
	# absent because grep -q closed the pipe early — that would duplicate the
	# rule on every rerun.
	local added rule present=0
	case $FW in
		ufw)
			added=$(ufw show added 2>/dev/null || true)
			while IFS= read -r rule; do
				if [[ $rule == *"from $CONTROLLER_ADDR "* && $rule == *"to any port $SSH_PORT proto tcp"* ]]; then
					present=1
					break
				fi
			done <<<"$added"
			if ((present)); then
				FW_STATE="present"
				printf 'firewall: scoped ufw rule already present — unchanged\n'
				return 0
			fi
			ufw allow from "$CONTROLLER_ADDR" to any port "$SSH_PORT" proto tcp >/dev/null
			FW_STATE="added"
			printf 'firewall: scoped ufw rule added (from %s to port %s/tcp only)\n' "$CONTROLLER_ADDR" "$SSH_PORT"
			;;
		firewalld)
			# Presence matches the FULL rule — source AND port — so a
			# same-source/different-port rerun provisions the requested port
			# instead of reading the old port's rule as sufficient.
			rule="rule family=\"ipv4\" source address=\"$CONTROLLER_ADDR\" port port=\"$SSH_PORT\" protocol=\"tcp\" accept"
			added=$(firewall-cmd --permanent --list-rich-rules 2>/dev/null || true)
			if [[ $added == *"$rule"* ]]; then
				present=1
			fi
			if ((present)); then
				FW_STATE="present"
				printf 'firewall: scoped firewalld rich rule already present — unchanged\n'
				return 0
			fi
			firewall-cmd --permanent --add-rich-rule="$rule" >/dev/null
			firewall-cmd --reload >/dev/null
			FW_STATE="added"
			printf 'firewall: scoped firewalld rich rule added (source %s, port %s/tcp only)\n' "$CONTROLLER_ADDR" "$SSH_PORT"
			;;
		none)
			FW_STATE="none-detected"
			printf 'firewall: no active firewall manager detected — nothing changed (sshd must already be reachable)\n'
			;;
	esac
}
ensure_firewall_rule

# ---------------------------------------------------------------------------
# step 5: compose .env (create-once; never regenerated, never echoed)
# ---------------------------------------------------------------------------

if [[ -f $ENV_FILE ]]; then
	if env_file_complete; then
		ENV_STATE="preserved"
		printf 'env file: %s exists with the required credentials — preserved byte-for-byte (rotation is a deliberate operator act)\n' "$ENV_FILE"
	else
		die 1 "$ENV_FILE exists but lacks required credential variables (MAI_POSTGRES_PASSWORD$([[ ${PROFILE_TOKENS[*]} == *search* ]] && printf ' or SEARXNG_SECRET')) — complete it manually; the installer never merges or overwrites credentials"
	fi
else
	mkdir -p "$(dirname "$ENV_FILE")"
	umask 077
	{
		printf 'MAI_POSTGRES_PASSWORD=%s\n' "$(gen_secret)"
		if [[ ${PROFILE_TOKENS[*]} == *search* ]]; then
			printf 'SEARXNG_SECRET=%s\n' "$(gen_secret)"
		fi
	} >"$ENV_FILE"
	chmod 600 "$ENV_FILE"
	umask 022
	ENV_STATE="created"
	printf 'env file: %s created (mode 600; values generated in place, never printed)\n' "$ENV_FILE"
fi

# ---------------------------------------------------------------------------
# step 6: selected stack (core default set + explicit optional profiles)
# ---------------------------------------------------------------------------

compose_up() {
	local -a args=(docker compose -f "$REPO/compose.yaml" --env-file "$ENV_FILE")
	local p
	for p in "${PROFILE_TOKENS[@]}"; do
		args+=(--profile "$p")
	done
	args+=(up -d --wait --wait-timeout 300)
	"${args[@]}"
}

if (( ${#PROFILE_TOKENS[@]} )); then
	STACK_DESC="core+${PROFILE_TOKENS[*]}"
else
	STACK_DESC="core"
fi
printf 'stack: starting %s via docker compose (up -d --wait)...\n' "$STACK_DESC"
compose_up
printf 'stack: %s running\n' "$STACK_DESC"

# ---------------------------------------------------------------------------
# step 7: canonical pair through the stable CLI (rerun-safe)
# ---------------------------------------------------------------------------

record_canonical_pair() {
	if [[ $BOOTSTRAP_STATE == "already-bootstrapped" ]]; then
		printf 'bootstrap: canonical pair already recorded for %s — preserved (rerun safety; changes go through mai.cli admin write)\n' "$SERVER_ID"
		mai_cli status --data-root "$DATA_ROOT" --server-id "$SERVER_ID"
		return 0
	fi
	local tmp_profile
	tmp_profile=$(mktemp)
	trap 'rm -f "$tmp_profile"' EXIT
	umask 077
	cat >"$tmp_profile" <<EOF
schema_version: 1
server_id: $(yaml_quote "$SERVER_ID")
name: $(yaml_quote "$PROFILE_NAME")
prompt_revision: 1
environment: linux_ssh
ssh:
  host: $(yaml_quote "$SSH_HOST")
  port: $SSH_PORT
  user: $(yaml_quote "$SSH_USER")
  host_key: $(yaml_quote "$FINGERPRINT")
  credential_ref: $(yaml_quote "$(credential_ref_of)")
privilege: $(grant_profile_privilege)
capabilities: []
EOF
	if ! mai_cli bootstrap --data-root "$DATA_ROOT" --server-id "$SERVER_ID" --profile "$tmp_profile"; then
		die 1 "mai.cli bootstrap failed — the canonical pair was not recorded (see the structured error above; nothing else was weakened)"
	fi
	rm -f "$tmp_profile"
	trap - EXIT
	BOOTSTRAP_STATE="recorded"
}
record_canonical_pair

# ---------------------------------------------------------------------------
# report (value-free: no secret material ever reaches these lines)
# ---------------------------------------------------------------------------

cat <<EOF
== install complete ==
fingerprint   : $FINGERPRINT
account       : $SSH_USER ($ACCOUNT_STATE)
key           : $KEY_STATE
grants        : $GRANT_STATE (scope $GRANT_SCOPE)
firewall      : $FW ($FW_STATE)
env file      : $ENV_STATE $ENV_FILE
stack         : $STACK_DESC running
bootstrap     : $BOOTSTRAP_STATE (data root $DATA_ROOT)
setup state   : the API starts unauthenticated-in-setup — complete the one-shot browser bootstrap yourself
EOF
