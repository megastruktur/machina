"""Focused bootstrap-installer tests for the P12 host bootstrap (card 12-bootstrap).

Anchors: brief ``mai_12-bootstrap.md`` acceptance 1–2; contracts.md C02 rule 1
(explicit data root), C03 rule 6 (no secret material on any stream), C04 rule 1
(host key pinned, never re-trusted), C09 rule 5 (explicit intents); the
12-cli-ops contract that ``scripts/install-host.sh`` calls the actual stable
``python -m mai.cli`` entrypoint instead of a second implementation.

Lanes (test policy: focused checks, deterministic fakes disclosed):

- Static/plan lane: ``bash -n`` plus the installer's ``--print-plan`` mode —
  the exact grant scope, sudoers content and the SCOPED firewall rule are
  presented without executing anything.
- Sandbox lane: every host-mutating command (``useradd``, ``install``,
  ``visudo``, ``ufw``, ``firewall-cmd``, ``docker``) is a shim on PATH that
  journals its argv and emulates minimal semantics inside a sandbox root
  (``MAI_INSTALL_SANDBOX`` prefixes ``/etc`` paths). The canonical pair step
  runs the REAL ``mai.cli`` producer against the sandbox data root — profile
  schema validation is the reviewed producer's, never re-implemented here.

LIMITS (disclosed): the sandbox proves the installer's decision logic,
ordering, rerun safety and value-free output. It cannot prove real host
effects (real sshd, real ufw/firewalld, real docker) — that is the exclusive
``runtime-target:unique-owned-arch-fixture`` scenario owned by ``12-verify``.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "scripts" / "install-host.sh"
SERVER_ID = "7e6c1a34-9a2b-4c3d-8e5f-1029384756af"
CONTROLLER_ADDR = "203.0.113.10"

CONTROLLER_PUBKEY = (
	"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIControllerFixturePublicKey0123456789 "
	"controller@mai-fxt"
)
FOREIGN_PUBKEY = (
	"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIForeignAdminKeyFixture09876543210a "
	"admin@elsewhere"
)
NARROW_SUDOERS_LINE = (
	"mai ALL=(root) NOPASSWD: /usr/bin/systemctl start docker.service, "
	"/usr/bin/systemctl stop docker.service, "
	"/usr/bin/systemctl restart docker.service, "
	"/usr/bin/systemctl status docker.service"
)
BROAD_SUDOERS_LINE = "mai ALL=(ALL) NOPASSWD: ALL"
MANAGED_MARKER = "# managed by scripts/install-host.sh"

#: journal needles in mandatory provisioning order (fresh install)
PROVISION_ORDER = (
	"useradd",
	"install -d",
	"visudo",
	"install -m 440",
	"ufw allow",
	"docker compose",
)


# ---------------------------------------------------------------------------
# backend venv (the real mai.cli producer used by the sandbox lane)
# ---------------------------------------------------------------------------


def _cli_command(module: str, *args: str) -> list[str]:
	venv_python = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
	if venv_python.is_file():
		return [str(venv_python), "-m", module, *args]
	uv = shutil.which("uv")
	if uv is None:
		pytest.skip("no backend venv or uv available for the real mai.cli producer")
	return [uv, "run", "--frozen", "--project", str(REPO_ROOT / "backend"),
	        "python", "-m", module, *args]


# ---------------------------------------------------------------------------
# sandbox: shims + seeds
# ---------------------------------------------------------------------------

SHIMS: dict[str, str] = {
	"getent": r"""#!/usr/bin/env bash
set -euo pipefail
[[ -n ${MAI_INSTALL_JOURNAL:-} ]] && printf 'getent %s\n' "$*" >>"$MAI_INSTALL_JOURNAL"
case ${1:-} in
  passwd) grep "^${2:?}:" "$MAI_INSTALL_SANDBOX/passwd.db" || exit 2 ;;
  *) exit 2 ;;
esac
""",
	"useradd": r"""#!/usr/bin/env bash
set -euo pipefail
[[ -n ${MAI_INSTALL_JOURNAL:-} ]] && printf 'useradd %s\n' "$*" >>"$MAI_INSTALL_JOURNAL"
name=${*: -1}
home="$MAI_INSTALL_SANDBOX/home/$name"
printf '%s:x:4242:4242:mai-controller:%s:/bin/bash\n' "$name" "$home" >>"$MAI_INSTALL_SANDBOX/passwd.db"
mkdir -p "$home"
chmod 700 "$home"
""",
	"install": r"""#!/usr/bin/env bash
set -euo pipefail
[[ -n ${MAI_INSTALL_JOURNAL:-} ]] && printf 'install %s\n' "$*" >>"$MAI_INSTALL_JOURNAL"
mode=""; dirs=0; targets=()
while [[ $# -gt 0 ]]; do
  case $1 in
    -m) mode=$2; shift 2 ;;
    -o|-g) shift 2 ;;
    -d) dirs=1; shift ;;
    -v|-p) shift ;;
    --) shift; while (($#)); do targets+=("$1"); shift; done ;;
    -*) printf 'install-shim: unsupported flag %s\n' "$1" >&2; exit 64 ;;
    *) targets+=("$1"); shift ;;
  esac
done
sb=${MAI_INSTALL_SANDBOX:-}
remap() { local p=$1; if [[ -n $sb && $p == /* && $p != "$sb"/* ]]; then printf '%s%s' "$sb" "$p"; else printf '%s' "$p"; fi; }
if ((dirs)); then
  for t in "${targets[@]}"; do
    d=$(remap "$t"); mkdir -p "$d"
    [[ -n $mode ]] && chmod "$mode" "$d"
  done
else
  src=${targets[0]?}; dst=$(remap "${targets[1]?}")
  mkdir -p "$(dirname "$dst")"
  # coreutils `install` creates/replaces the destination (unlink first), so a
  # mode-440 sudoers file can be reconciled in place.
  rm -f -- "$dst"
  cp -- "$src" "$dst"
  [[ -n $mode ]] && chmod "$mode" "$dst"
fi
exit 0
""",
	"visudo": r"""#!/usr/bin/env bash
set -euo pipefail
[[ -n ${MAI_INSTALL_JOURNAL:-} ]] && printf 'visudo %s\n' "$*" >>"$MAI_INSTALL_JOURNAL"
if [[ ${MAI_FXT_VISUDO_FAIL:-0} == 1 ]]; then echo "visudo: simulated syntax error" >&2; exit 1; fi
file=""
while [[ $# -gt 0 ]]; do
  case $1 in
    -cf|-f) file=$2; shift 2 ;;
    *) shift ;;
  esac
done
[[ -n $file ]] || exit 64
if grep -q "VISUDO-REJECT" "$file"; then echo "visudo: syntax error" >&2; exit 1; fi
exit 0
""",
	"ufw": r"""#!/usr/bin/env bash
set -euo pipefail
[[ -n ${MAI_INSTALL_JOURNAL:-} ]] && printf 'ufw %s\n' "$*" >>"$MAI_INSTALL_JOURNAL"
fw="$MAI_INSTALL_SANDBOX/ufw-rules.txt"
case ${1:-} in
  status) printf 'Status: active\n'; [[ -f $fw ]] && cat "$fw"; exit 0 ;;
  show) shift; [[ ${1:-} == added ]] || exit 2; [[ -f $fw ]] && cat "$fw"; exit 0 ;;
  allow) printf 'ufw allow %s\n' "${*:2}" >>"$fw"; printf 'Rule added\n'; exit 0 ;;
  *) exit 0 ;;
esac
""",
	"firewall-cmd": r"""#!/usr/bin/env bash
set -euo pipefail
[[ -n ${MAI_INSTALL_JOURNAL:-} ]] && printf 'firewall-cmd %s\n' "$*" >>"$MAI_INSTALL_JOURNAL"
fw="$MAI_INSTALL_SANDBOX/firewalld-rules.txt"
if [[ ${1:-} == --state ]]; then printf 'running\n'; exit 0; fi
if [[ ${1:-} == --permanent && ${2:-} == --list-rich-rules ]]; then [[ -f $fw ]] && cat "$fw"; exit 0; fi
if [[ ${1:-} == --permanent && ${2:-} == --add-rich-rule=* ]]; then printf '%s\n' "${2#--add-rich-rule=}" >>"$fw"; printf 'success\n'; exit 0; fi
if [[ ${1:-} == --reload ]]; then exit 0; fi
exit 1
""",
	"docker": r"""#!/usr/bin/env bash
set -euo pipefail
[[ -n ${MAI_INSTALL_JOURNAL:-} ]] && printf 'docker %s\n' "$*" >>"$MAI_INSTALL_JOURNAL"
has_up=false
for arg in "$@"; do
  if [[ $arg == up ]]; then has_up=true; fi
done
if [[ ${1:-} == compose && $has_up == true && ${MAI_FXT_FAIL_UP:-0} == 1 ]]; then
  echo "docker: simulated stack-start failure" >&2
  exit 1
fi
case ${1:-} in
  compose)
    case ${2:-} in
      version) printf 'Docker Compose version v5.5.0\n' ;;
      up) printf 'Container mai-running\n' ;;
      ps) printf '[]\n' ;;
    esac ;;
esac
exit 0
""",
}

UFW_NEIGHBORS = [
	"ufw allow from 192.168.100.0/24 to any port 80 proto tcp",
	"ufw allow from 198.51.100.7 to any port 443 proto tcp",
]


def _write_shims(root: Path, names: tuple[str, ...] = tuple(SHIMS)) -> Path:
	bin_dir = root / "bin"
	bin_dir.mkdir(parents=True, exist_ok=True)
	for name in names:
		shim = bin_dir / name
		shim.write_text(SHIMS[name], encoding="utf-8")
		shim.chmod(0o755)
	return bin_dir


@pytest.fixture()
def sb(tmp_path: Path) -> Path:
	"""One disposable sandbox: shims, fake account db, seeded firewall."""
	root = tmp_path / "mai-fxt-sb"
	(root / "etc" / "sudoers.d").mkdir(parents=True)
	(root / "home").mkdir()
	(root / "passwd.db").write_text("", encoding="utf-8")
	_write_shims(root)
	# Real fixture host identity + controller key (both generated per-test).
	subprocess.run(
		["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / "hostkey")],
		check=True, capture_output=True,
	)
	(root / "controller.pub").write_text(CONTROLLER_PUBKEY + "\n", encoding="utf-8")
	(root / "ufw-rules.txt").write_text(
		"".join(line + "\n" for line in UFW_NEIGHBORS), encoding="utf-8"
	)
	return root


def seed_existing_account(root: Path, keys: list[str]) -> None:
	"""Pre-existing provisioned account (interrupted/partial install state)."""
	home = root / "home" / "mai"
	(home / ".ssh").mkdir(parents=True, exist_ok=True)
	auth_keys = home / ".ssh" / "authorized_keys"
	auth_keys.write_text("".join(key + "\n" for key in keys), encoding="utf-8")
	auth_keys.chmod(0o600)
	(root / "passwd.db").write_text(
		f"mai:x:4242:4242:mai-controller:{home}:/bin/bash\n", encoding="utf-8"
	)


def install_args(root: Path, *, scope: str = "narrow", profiles: str = "core",
                 extra: tuple[str, ...] = ()) -> list[str]:
	return [
		"--server-id", SERVER_ID,
		"--data-root", str(root / "data-root"),
		"--public-key", str(root / "controller.pub"),
		"--grant-scope", scope,
		"--controller-address", CONTROLLER_ADDR,
		"--host-key-pub", str(root / "hostkey.pub"),
		"--repo", str(REPO_ROOT),
		"--env-file", str(root / "stack.env"),
		"--stack-profiles", profiles,
		*extra,
	]


def run_install(root: Path, *args: str, bin_names: tuple[str, ...] = tuple(SHIMS),
                extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	env = {
		**os.environ,
		"PATH": f"{_write_shims(root, bin_names)}:{os.environ['PATH']}",
		"MAI_INSTALL_SANDBOX": str(root),
		"MAI_INSTALL_SKIP_ROOT_CHECK": "1",
		"MAI_INSTALL_JOURNAL": str(root / "journal.log"),
		"TMPDIR": str(root),  # even the installer's mktemp files stay inside
	}
	# A None value REMOVES the variable (e.g. unsetting the root-check seam).
	for key, value in (extra_env or {}).items():
		if value is None:
			env.pop(key, None)
		else:
			env[key] = value
	return subprocess.run(
		["bash", str(INSTALLER), *args],
		capture_output=True, text=True, env=env, cwd=str(root), timeout=300,
	)


def journal(root: Path) -> list[str]:
	log = root / "journal.log"
	return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def jindex(root: Path, needle: str) -> int:
	for i, line in enumerate(journal(root)):
		if needle in line:
			return i
	raise AssertionError(f"journal has no line containing {needle!r}: {journal(root)}")


def journal_count(root: Path, predicate) -> int:
	return sum(1 for line in journal(root) if predicate(line))


def read_mode(path: Path) -> int:
	return stat.S_IMODE(path.stat().st_mode)


def fingerprint_of(root: Path) -> str:
	out = subprocess.run(
		["ssh-keygen", "-lf", str(root / "hostkey.pub")],
		check=True, capture_output=True, text=True,
	).stdout
	return out.split()[1]


def stored_profile(root: Path) -> Path:
	return root / "data-root" / "servers" / SERVER_ID / "profile.yaml"


def validate_profile_with_producer(profile: Path) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		_cli_command("mai.contracts.server", str(profile)),
		capture_output=True, text=True, timeout=120,
	)


# ---------------------------------------------------------------------------
# static + plan lane
# ---------------------------------------------------------------------------


def test_installer_syntax_and_help_surface() -> None:
	syntax = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True, text=True)
	assert syntax.returncode == 0, syntax.stderr
	helped = subprocess.run(
		["bash", str(INSTALLER), "--help"], capture_output=True, text=True, timeout=60,
	)
	assert helped.returncode == 0
	for option in (
		"--server-id", "--data-root", "--public-key", "--grant-scope",
		"--controller-address", "--stack-profiles", "--print-plan", "--yes",
		"--reconcile-grants", "--ssh-user", "--ssh-port", "--env-file",
	):
		assert option in helped.stdout, option
	assert "prints a password or token" in helped.stdout


def test_plan_presents_exact_grant_and_scoped_rule_without_executing(sb: Path) -> None:
	plan = run_install(sb, *install_args(sb), "--print-plan")
	assert plan.returncode == 0, plan.stderr
	assert "== mai install plan (nothing has been applied) ==" in plan.stdout
	assert fingerprint_of(sb) in plan.stdout
	assert "grant scope          : narrow" in plan.stdout
	assert NARROW_SUDOERS_LINE in plan.stdout
	assert (
		f"ufw — will ensure ONLY: ufw allow from {CONTROLLER_ADDR} to any port 22 proto tcp"
	) in plan.stdout
	assert "capabilities: []" in plan.stdout
	# Read-only: no provisioning command was executed.
	mutating = [
		line for line in journal(sb)
		if line.startswith(("useradd", "install ", "visudo", "docker", "firewall-cmd", "ufw allow"))
	]
	assert mutating == [], mutating
	assert not (sb / "home" / "mai").exists()
	assert not (sb / "stack.env").exists()
	assert not stored_profile(sb).exists()


def test_plan_broad_scope_is_explicitly_labeled_broad(sb: Path) -> None:
	plan = run_install(sb, *install_args(sb, scope="broad"), "--print-plan")
	assert plan.returncode == 0, plan.stderr
	assert BROAD_SUDOERS_LINE in plan.stdout
	assert "BROAD" in plan.stdout
	assert "WARNING" in plan.stdout


def test_plan_none_scope_announces_no_sudoers(sb: Path) -> None:
	plan = run_install(sb, *install_args(sb, scope="none"), "--print-plan")
	assert plan.returncode == 0, plan.stderr
	assert "no sudo grant written" in plan.stdout
	assert "sudoers content" not in plan.stdout


def test_negative_blanket_controller_address_refused(sb: Path) -> None:
	# Exact blanket targets AND broader-than-/8 prefixes (/0–/7): a /0 or /1
	# mask scopes the world, never one controller — refused before any command.
	for blanket in ("0.0.0.0/0", "any", "203.0.113.9/0", "10.1.2.3/1"):
		proc = run_install(
			sb, *install_args(sb), "--controller-address", blanket, "--print-plan",
		)
		assert proc.returncode == 2, (blanket, proc.stdout + proc.stderr)
		# Either refusal is fail-closed: explicit blanket rejection, or the
		# non-IPv4 grammar rejection for words like `any`.
		assert "blanket" in proc.stderr or "not an IPv4 address" in proc.stderr
		assert journal(sb) == [], "a refusal must happen before any command runs"
	# The boundary itself stays legal: /8 is the broadest accepted scope.
	plan = run_install(
		sb, *install_args(sb), "--controller-address", "203.0.113.10/8", "--print-plan",
	)
	assert plan.returncode == 0, plan.stdout + plan.stderr
	assert "ufw allow from 203.0.113.10/8 to any port 22 proto tcp" in plan.stdout
	assert not any(
		line.startswith(("useradd", "install ", "visudo", "docker", "ufw allow"))
		for line in journal(sb)
	), "the /8 plan must stay read-only"


# ---------------------------------------------------------------------------
# fresh install lane
# ---------------------------------------------------------------------------


def test_fresh_install_provisions_in_order_then_starts_stack_and_cli(sb: Path) -> None:
	proc = run_install(sb, *install_args(sb, profiles="core,worker"), "--yes")
	assert proc.returncode == 0, proc.stdout + proc.stderr

	# Provisioning order: account -> .ssh -> sudoers validation -> sudoers
	# write -> scoped firewall rule -> stack start; the canonical pair (real
	# CLI, unjournaled) runs last — its file effect is asserted below.
	indexes = [jindex(sb, needle) for needle in PROVISION_ORDER]
	assert indexes == sorted(indexes), list(zip(PROVISION_ORDER, indexes))

	# Account + key.
	assert "mai:x:4242:4242" in (sb / "passwd.db").read_text()
	auth_keys = sb / "home" / "mai" / ".ssh" / "authorized_keys"
	assert read_mode(auth_keys) == 0o600
	assert read_mode(auth_keys.parent) == 0o700
	assert auth_keys.read_text().splitlines() == [CONTROLLER_PUBKEY]

	# Grants: exact narrow scope, visudo-validated, mode 440.
	sudoers = sb / "etc" / "sudoers.d" / "90-mai-mai"
	assert read_mode(sudoers) == 0o440
	assert sudoers.read_text().splitlines()[0].startswith(MANAGED_MARKER)
	assert NARROW_SUDOERS_LINE in sudoers.read_text()

	# Firewall: only the scoped rule; neighbors untouched; never blanket.
	rules = (sb / "ufw-rules.txt").read_text().splitlines()
	assert rules == [
		*UFW_NEIGHBORS,
		f"ufw allow from {CONTROLLER_ADDR} to any port 22 proto tcp",
	]
	up_line = next(line for line in journal(sb) if line.startswith("docker") and " up" in line)
	assert "--profile worker" in up_line

	# Canonical pair: recorded by the REAL mai.cli against the sandbox root.
	profile = stored_profile(sb)
	assert profile.is_file()
	document = yaml.safe_load(profile.read_text())
	assert document["schema_version"] == 1
	assert document["server_id"] == SERVER_ID
	assert document["environment"] == "linux_ssh"
	assert document["privilege"] == "sudo"  # narrow scope
	assert document["ssh"]["host_key"] == fingerprint_of(sb)
	assert document["ssh"]["user"] == "mai"
	assert document["ssh"]["port"] == 22
	assert document["ssh"]["credential_ref"] == "MAI_SSH_KEY_MAI"
	assert document["capabilities"] == []
	checked = validate_profile_with_producer(profile)
	assert checked.returncode == 0, checked.stdout + checked.stderr
	assert '"bootstrapped": true' in proc.stdout


def test_generated_secrets_never_reach_any_output_stream(sb: Path) -> None:
	proc = run_install(sb, *install_args(sb, profiles="core,search"), "--yes")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	stack_env = (sb / "stack.env").read_text()
	pg = re.search(r"^MAI_POSTGRES_PASSWORD=(\S+)$", stack_env, re.M)
	secret = re.search(r"^SEARXNG_SECRET=(\S+)$", stack_env, re.M)
	assert pg and len(pg.group(1)) >= 32
	assert secret and len(secret.group(1)) >= 32
	combined = proc.stdout + proc.stderr
	assert pg.group(1) not in combined
	assert secret.group(1) not in combined
	assert "MAI_POSTGRES_PASSWORD=" not in combined
	assert "SEARXNG_SECRET=" not in combined
	assert read_mode(sb / "stack.env") == 0o600
	# Rerun output is value-free too.
	again = run_install(sb, *install_args(sb, profiles="core,search"), "--yes")
	assert again.returncode == 0
	combined2 = again.stdout + again.stderr
	assert pg.group(1) not in combined2 and secret.group(1) not in combined2
	assert "preserved" in again.stdout


# ---------------------------------------------------------------------------
# rerun-safety lane
# ---------------------------------------------------------------------------


def test_rerun_preserves_existing_keys_config_and_skips_bootstrap(sb: Path) -> None:
	# Pre-existing account with a FOREIGN admin key: the installer appends the
	# controller key and preserves the foreign line byte-for-byte.
	seed_existing_account(sb, [FOREIGN_PUBKEY])
	first = run_install(sb, *install_args(sb), "--yes")
	assert first.returncode == 0, first.stdout + first.stderr
	auth_keys = sb / "home" / "mai" / ".ssh" / "authorized_keys"
	assert auth_keys.read_text().splitlines() == [FOREIGN_PUBKEY, CONTROLLER_PUBKEY]
	assert "existing line(s) preserved" in first.stdout

	# Second run: identical inputs ⇒ identical state, no re-provisioning.
	sudoers = sb / "etc" / "sudoers.d" / "90-mai-mai"
	auth_before = auth_keys.read_bytes()
	sudoers_before = sudoers.read_bytes()
	env_before = (sb / "stack.env").read_bytes()
	profile_before = stored_profile(sb).read_bytes()

	second = run_install(sb, *install_args(sb), "--yes")
	assert second.returncode == 0, second.stdout + second.stderr
	assert auth_keys.read_bytes() == auth_before
	assert sudoers.read_bytes() == sudoers_before
	assert (sb / "stack.env").read_bytes() == env_before
	assert stored_profile(sb).read_bytes() == profile_before
	assert "already recorded" in second.stdout
	assert "preserved" in second.stdout
	# Nothing was re-provisioned: the pre-existing account was reused (no
	# useradd), no key/grant writes; the stack start IS rerun-safe (resume
	# brings it back).
	assert journal_count(sb, lambda line: line.startswith("useradd")) == 0
	# .ssh pre-existed from the seed; the sudoers grant was written exactly
	# once; the scoped firewall rule was added exactly once.
	assert journal_count(sb, lambda line: line.startswith("install -d")) == 0
	assert journal_count(sb, lambda line: line.startswith("install -m 440")) == 1
	assert journal_count(sb, lambda line: line.startswith("ufw allow")) == 1
	assert journal_count(sb, lambda line: line.startswith("docker") and " up" in line) == 2


def test_interrupted_stack_start_resumes_without_reprovisioning(sb: Path) -> None:
	fail = run_install(sb, *install_args(sb), "--yes", extra_env={"MAI_FXT_FAIL_UP": "1"})
	assert fail.returncode != 0
	assert "simulated stack-start failure" in fail.stderr
	# Everything before the stack step persisted (resume state)…
	assert (sb / "home" / "mai" / ".ssh" / "authorized_keys").exists()
	assert (sb / "etc" / "sudoers.d" / "90-mai-mai").exists()
	assert (sb / "stack.env").exists()
	# …and the resume run completes without redoing any earlier step.
	resumed = run_install(sb, *install_args(sb), "--yes")
	assert resumed.returncode == 0, resumed.stdout + resumed.stderr
	assert journal_count(sb, lambda line: line.startswith("useradd")) == 1
	assert journal_count(sb, lambda line: line.startswith("install -m 440")) == 1
	assert journal_count(sb, lambda line: line.startswith("ufw allow")) == 1
	auth_keys = (sb / "home" / "mai" / ".ssh" / "authorized_keys").read_text()
	assert auth_keys.count(CONTROLLER_PUBKEY) == 1
	assert stored_profile(sb).is_file()


def test_firewalld_path_adds_only_a_scoped_rich_rule(sb: Path) -> None:
	neighbors = [
		'rule family="ipv4" source address="192.168.100.0/24" port port="80" protocol="tcp" accept',
	]
	(sb / "firewalld-rules.txt").write_text(
		"".join(line + "\n" for line in neighbors), encoding="utf-8"
	)
	# Remove the ufw shim so detection falls through to firewalld.
	(sb / "bin" / "ufw").unlink()
	no_ufw = tuple(name for name in SHIMS if name != "ufw")
	first = run_install(sb, *install_args(sb), "--yes", bin_names=no_ufw)
	assert first.returncode == 0, first.stdout + first.stderr
	second = run_install(sb, *install_args(sb), "--yes", bin_names=no_ufw)
	assert second.returncode == 0, second.stdout + second.stderr
	rules = (sb / "firewalld-rules.txt").read_text().splitlines()
	expected = (
		f'rule family="ipv4" source address="{CONTROLLER_ADDR}" '
		'port port="22" protocol="tcp" accept'
	)
	assert rules == [*neighbors, expected]
	assert journal_count(sb, lambda line: "--add-rich-rule=" in line) == 1

	# Presence matches source AND port: a same-source/different-port rerun
	# provisions the requested port instead of reading the :22 rule as enough.
	third = run_install(
		sb, *install_args(sb, extra=("--ssh-port", "2222")), "--yes", bin_names=no_ufw,
	)
	assert third.returncode == 0, third.stdout + third.stderr
	expected_2222 = (
		f'rule family="ipv4" source address="{CONTROLLER_ADDR}" '
		'port port="2222" protocol="tcp" accept'
	)
	assert (sb / "firewalld-rules.txt").read_text().splitlines() == [
		*neighbors, expected, expected_2222,
	]
	assert journal_count(sb, lambda line: "--add-rich-rule=" in line) == 2
	# The identical rerun stays a no-op: no duplicate rule for either port.
	fourth = run_install(
		sb, *install_args(sb, extra=("--ssh-port", "2222")), "--yes", bin_names=no_ufw,
	)
	assert fourth.returncode == 0, fourth.stdout + fourth.stderr
	assert "already present" in fourth.stdout
	assert (sb / "firewalld-rules.txt").read_text().splitlines() == [
		*neighbors, expected, expected_2222,
	]
	assert journal_count(sb, lambda line: "--add-rich-rule=" in line) == 2


# ---------------------------------------------------------------------------
# grant lifecycle lane
# ---------------------------------------------------------------------------


def test_negative_grant_scope_change_refused_without_reconciliation(sb: Path) -> None:
	first = run_install(sb, *install_args(sb, scope="narrow"), "--yes")
	assert first.returncode == 0
	sudoers = sb / "etc" / "sudoers.d" / "90-mai-mai"
	before = sudoers.read_bytes()
	second = run_install(sb, *install_args(sb, scope="broad"), "--yes")
	assert second.returncode == 1
	assert sudoers.read_bytes() == before, "authority changed without explicit reconciliation"
	assert "--reconcile-grants" in second.stderr
	assert journal_count(sb, lambda line: line.startswith("install -m 440")) == 1


def test_grant_reconciliation_is_explicit_and_recorded(sb: Path) -> None:
	first = run_install(sb, *install_args(sb, scope="narrow"), "--yes")
	assert first.returncode == 0
	second = run_install(sb, *install_args(sb, scope="broad"), "--yes", "--reconcile-grants")
	assert second.returncode == 0, second.stdout + second.stderr
	sudoers = sb / "etc" / "sudoers.d" / "90-mai-mai"
	assert read_mode(sudoers) == 0o440
	assert BROAD_SUDOERS_LINE in sudoers.read_text()
	assert NARROW_SUDOERS_LINE not in sudoers.read_text()
	assert "reconciled" in second.stdout
	# Narrowing back is equally explicit.
	third = run_install(sb, *install_args(sb, scope="narrow"), "--yes", "--reconcile-grants")
	assert third.returncode == 0
	assert NARROW_SUDOERS_LINE in sudoers.read_text()


def test_negative_none_scope_over_managed_grant_requires_reconciliation(sb: Path) -> None:
	first = run_install(sb, *install_args(sb, scope="narrow"), "--yes")
	assert first.returncode == 0
	sudoers = sb / "etc" / "sudoers.d" / "90-mai-mai"
	refused = run_install(sb, *install_args(sb, scope="none"), "--yes")
	assert refused.returncode == 1
	assert sudoers.exists(), "a grant was silently removed"
	assert "--reconcile-grants" in refused.stderr
	removed = run_install(sb, *install_args(sb, scope="none"), "--yes", "--reconcile-grants")
	assert removed.returncode == 0
	assert not sudoers.exists()
	assert "reconciled-removed" in removed.stdout


def test_foreign_sudoers_files_are_never_touched(sb: Path) -> None:
	foreign = sb / "etc" / "sudoers.d" / "zzz-unrelated"
	foreign.write_text("admin2 ALL=(ALL) ALL\n", encoding="utf-8")
	before = foreign.read_bytes()
	proc = run_install(sb, *install_args(sb), "--yes")
	assert proc.returncode == 0
	assert foreign.read_bytes() == before


def test_negative_foreign_file_at_the_managed_path_refuses(sb: Path) -> None:
	managed = sb / "etc" / "sudoers.d" / "90-mai-mai"
	managed.write_text("admin2 ALL=(ALL) ALL\n", encoding="utf-8")
	before = managed.read_bytes()
	proc = run_install(sb, *install_args(sb), "--yes")
	assert proc.returncode == 1
	assert "foreign file" in proc.stderr
	assert managed.read_bytes() == before
	assert not (sb / "stack.env").exists()
	assert not any(line.startswith("docker") for line in journal(sb))


# ---------------------------------------------------------------------------
# negative-control lane (fail-closed, no mutation before validation)
# ---------------------------------------------------------------------------


def test_negative_missing_required_arguments_fail_without_mutation(sb: Path) -> None:
	for drop in ("--grant-scope", "--controller-address", "--server-id", "--data-root", "--public-key"):
		args = install_args(sb)
		index = args.index(drop)
		del args[index:index + 2]
		proc = run_install(sb, *args, "--yes")
		assert proc.returncode == 2, (drop, proc.stdout + proc.stderr)
	assert journal(sb) == []
	assert not (sb / "stack.env").exists()
	assert not (sb / "data-root").exists()


def test_negative_noninteractive_run_without_consent_refuses(sb: Path) -> None:
	proc = run_install(sb, *install_args(sb))  # no --yes, stdin is a pipe
	assert proc.returncode == 2
	assert "--yes" in proc.stderr
	# Read-only probes (fingerprint/firewall detection) may run, but nothing
	# may be provisioned or changed.
	mutating = [
		line for line in journal(sb)
		if line.startswith(("useradd", "install ", "visudo", "docker", "firewall-cmd --permanent", "ufw allow"))
	]
	assert mutating == [], mutating
	assert not (sb / "stack.env").exists()
	assert not stored_profile(sb).exists()


def test_negative_invalid_public_key_refused(sb: Path) -> None:
	(sb / "controller.pub").write_text("not an openssh public key\n", encoding="utf-8")
	proc = run_install(sb, *install_args(sb), "--yes")
	assert proc.returncode == 2
	assert "OpenSSH public key" in proc.stderr
	assert journal(sb) == []
	assert not (sb / "home" / "mai").exists()


def test_negative_unprivileged_run_refused_without_root_bypass(sb: Path) -> None:
	# The suite itself runs unprivileged (EUID != 0); without the documented
	# MAI_INSTALL_SKIP_ROOT_CHECK=1 seam — unset OR 0 — the installer must
	# refuse before plan/consent/apply: exit 1, no command, no host effect.
	for skip in (None, "0"):
		proc = run_install(
			sb, *install_args(sb), "--yes",
			extra_env={"MAI_INSTALL_SKIP_ROOT_CHECK": skip},
		)
		assert proc.returncode == 1, (skip, proc.stdout + proc.stderr)
		assert "root" in proc.stderr and "MAI_INSTALL_SKIP_ROOT_CHECK" in proc.stderr
		assert journal(sb) == [], "the root refusal must precede every command"
		assert not (sb / "home" / "mai").exists()
		assert not (sb / "etc" / "sudoers.d" / "90-mai-mai").exists()
		assert not (sb / "stack.env").exists()
		assert not stored_profile(sb).exists()
	# `--help` stays usable unprivileged: the gate sits after option parsing.
	helped = subprocess.run(
		["bash", str(INSTALLER), "--help"], capture_output=True, text=True, timeout=60,
	)
	assert helped.returncode == 0, helped.stderr


def test_negative_visudo_failure_aborts_before_grants_or_stack(sb: Path) -> None:
	proc = run_install(sb, *install_args(sb), "--yes", extra_env={"MAI_FXT_VISUDO_FAIL": "1"})
	assert proc.returncode == 1
	assert "visudo" in proc.stderr
	# Fail-closed ordering: identity steps persisted (resume-safe), but no
	# grant file, no env secrets and no stack start happened.
	assert "mai:x:4242:4242" in (sb / "passwd.db").read_text()
	assert (sb / "home" / "mai" / ".ssh" / "authorized_keys").exists()
	assert not (sb / "etc" / "sudoers.d" / "90-mai-mai").exists()
	assert not (sb / "stack.env").exists()
	assert not any(line.startswith("docker") for line in journal(sb))
