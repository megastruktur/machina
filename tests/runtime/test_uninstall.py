"""Focused uninstall/rotation tests for the P12 host deprovisioning (card 12-uninstall).

Anchors: brief ``mai_12-uninstall.md`` acceptance 1–2; contracts.md C02 rule 1
(explicit data root), C03 rule 6 (no secret material on any stream), C04 rule 1
(host key pinned — rotation/deprovisioning never touches host trust), C09 rule
5 (explicit keep/delete intent; managed data never removed implicitly), C10
rule 2 (fixture namespace discipline); the 12-bootstrap artifact vocabulary
(``# managed by scripts/install-host.sh`` marker, scoped ufw/firewalld rule
shape, ``/etc/sudoers.d/90-mai-<user>``) and the 12-cli-ops contract that the
data half flows through the actual ``python -m mai.cli uninstall`` producer.

Lanes (test policy: focused checks, deterministic fakes disclosed):

- Static/plan lane: ``bash -n`` plus ``--print-plan`` — the exact inventory of
  owned artifacts (managed sudoers, scoped rule, controller key line, data
  intent) is presented without executing anything.
- Round-trip lane: the REAL ``scripts/install-host.sh`` provisions each
  sandbox first (its own documented seams + shims), then the uninstaller must
  remove exactly those artifacts. Foreign state — neighbor firewall rules,
  foreign authorized_keys lines, foreign accounts — is seeded alongside and
  proven byte-identical afterwards.
- Negative lane: blanket/broader-than-/8 addresses, foreign sudoers at the
  managed path, foreign home under ``--remove-user``, missing consent,
  unprivileged runs without the documented root-check seam, rotation blind
  adds, CLI-refused foreign data-root entries.

LIMITS (disclosed): the sandbox proves the uninstaller's decision logic,
exact-artifact matching, ordering and idempotency. It cannot prove real host
effects (real sshd login with the old/new key, real ufw/firewalld, real docker
volumes) — the exclusive ``runtime-target:unique-owned-arch-fixture`` scenario
(rotate then old-key denied/new-key works, uninstall with keep-data, unrelated
app/SSH still functional) is owned by ``12-verify``.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UNINSTALLER = REPO_ROOT / "scripts" / "uninstall-host.sh"
INSTALLER = REPO_ROOT / "scripts" / "install-host.sh"
SERVER_ID = "7e6c1a34-9a2b-4c3d-8e5f-1029384756af"
CONTROLLER_ADDR = "203.0.113.10"

CONTROLLER_PUBKEY = (
	"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIControllerFixturePublicKey0123456789 "
	"controller@mai-fxt"
)
NEW_CONTROLLER_PUBKEY = (
	"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIRotatedControllerKeyFedcba9876543210 "
	"controller@mai-fxt-rotated"
)
THIRD_PUBKEY = (
	"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIThirdControllerKey0a1b2c3d4e5f6a7b "
	"controller@mai-fxt-third"
)
FOREIGN_PUBKEY = (
	"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIForeignAdminKeyFixture09876543210a "
	"admin@elsewhere"
)
MANAGED_MARKER = "# managed by scripts/install-host.sh"
NARROW_SUDOERS_LINE = (
	"mai ALL=(root) NOPASSWD: /usr/bin/systemctl start docker.service, "
	"/usr/bin/systemctl stop docker.service, "
	"/usr/bin/systemctl restart docker.service, "
	"/usr/bin/systemctl status docker.service"
)

#: the exact scoped rule the installer writes (ufw ``show added`` shape)
SCOPED_RULE = f"ufw allow from {CONTROLLER_ADDR} to any port 22 proto tcp"
UFW_NEIGHBORS = [
	"ufw allow from 192.168.100.0/24 to any port 80 proto tcp",
	"ufw allow from 198.51.100.7 to any port 443 proto tcp",
]
SCOPED_RICH_RULE = (
	f'rule family="ipv4" source address="{CONTROLLER_ADDR}" '
	'port port="22" protocol="tcp" accept'
)
FIREWALLD_NEIGHBOR = (
	'rule family="ipv4" source address="198.51.100.7" '
	'port port="443" protocol="tcp" accept'
)


# ---------------------------------------------------------------------------
# backend venv (the real mai.cli producer used by the sandbox lane)
# ---------------------------------------------------------------------------


def _cli_command() -> str:
	venv_python = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
	if venv_python.is_file():
		return f"{venv_python} -m mai.cli"
	uv = shutil.which("uv")
	if uv is None:
		pytest.skip("no backend venv or uv available for the real mai.cli producer")
	return f"{uv} run --frozen --project {REPO_ROOT / 'backend'} python -m mai.cli"


# ---------------------------------------------------------------------------
# sandbox: shims + seeds (union of the installer's/uninstaller's surface)
# ---------------------------------------------------------------------------

SHIMS: dict[str, str] = {
	"getent": r"""#!/usr/bin/env bash
set -euo pipefail
J=${MAI_INSTALL_JOURNAL:-${MAI_UNINSTALL_JOURNAL:-}}
[[ -n $J ]] && printf 'getent %s\n' "$*" >>"$J"
case ${1:-} in
  passwd) grep "^${2:?}:" "$MAI_UNINSTALL_SANDBOX/passwd.db" || exit 2 ;;
  *) exit 2 ;;
esac
""",
	"useradd": r"""#!/usr/bin/env bash
set -euo pipefail
J=${MAI_INSTALL_JOURNAL:-${MAI_UNINSTALL_JOURNAL:-}}
[[ -n $J ]] && printf 'useradd %s\n' "$*" >>"$J"
name=${*: -1}
home="$MAI_UNINSTALL_SANDBOX/home/$name"
printf '%s:x:4242:4242:mai-controller:%s:/bin/bash\n' "$name" "$home" >>"$MAI_UNINSTALL_SANDBOX/passwd.db"
mkdir -p "$home"
chmod 700 "$home"
""",
	"userdel": r"""#!/usr/bin/env bash
set -euo pipefail
J=${MAI_INSTALL_JOURNAL:-${MAI_UNINSTALL_JOURNAL:-}}
[[ -n $J ]] && printf 'userdel %s\n' "$*" >>"$J"
remove_home=0
while [[ $# -gt 0 ]]; do
  case $1 in
    -r) remove_home=1; shift ;;
    --) shift ;;
    -*) printf 'userdel-shim: unsupported flag %s\n' "$1" >&2; exit 64 ;;
    *) name=$1; shift ;;
  esac
done
sb=$MAI_UNINSTALL_SANDBOX
grep -v "^${name:?}:" "$sb/passwd.db" >"$sb/passwd.db.tmp" || true
mv "$sb/passwd.db.tmp" "$sb/passwd.db"
if ((remove_home)); then rm -rf "$sb/home/$name"; fi
""",
	"install": r"""#!/usr/bin/env bash
set -euo pipefail
J=${MAI_INSTALL_JOURNAL:-${MAI_UNINSTALL_JOURNAL:-}}
[[ -n $J ]] && printf 'install %s\n' "$*" >>"$J"
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
sb=${MAI_UNINSTALL_SANDBOX:-}
remap() { local p=$1; if [[ -n $sb && $p == /* && $p != "$sb"/* ]]; then printf '%s%s' "$sb" "$p"; else printf '%s' "$p"; fi; }
if ((dirs)); then
  for t in "${targets[@]}"; do
    d=$(remap "$t"); mkdir -p "$d"
    [[ -n $mode ]] && chmod "$mode" "$d"
  done
else
  src=${targets[0]?}; dst=$(remap "${targets[1]?}")
  mkdir -p "$(dirname "$dst")"
  rm -f -- "$dst"
  cp -- "$src" "$dst"
  [[ -n $mode ]] && chmod "$mode" "$dst"
fi
exit 0
""",
	"visudo": r"""#!/usr/bin/env bash
set -euo pipefail
J=${MAI_INSTALL_JOURNAL:-${MAI_UNINSTALL_JOURNAL:-}}
[[ -n $J ]] && printf 'visudo %s\n' "$*" >>"$J"
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
J=${MAI_INSTALL_JOURNAL:-${MAI_UNINSTALL_JOURNAL:-}}
[[ -n $J ]] && printf 'ufw %s\n' "$*" >>"$J"
fw="$MAI_UNINSTALL_SANDBOX/ufw-rules.txt"
case ${1:-} in
  status) if [[ ${MAI_FXT_UFW_INACTIVE:-0} == 1 ]]; then printf 'Status: inactive\n'; exit 0; fi; printf 'Status: active\n'; [[ -f $fw ]] && cat "$fw"; exit 0 ;;
  show) shift; [[ ${1:-} == added ]] || exit 2; [[ -f $fw ]] && cat "$fw"; exit 0 ;;
  allow) printf 'ufw allow %s\n' "${*:2}" >>"$fw"; printf 'Rule added\n'; exit 0 ;;
  delete) line="ufw allow ${*:3}"; [[ -f $fw ]] || exit 0; grep -vxF "$line" "$fw" >"$fw.tmp" || true; mv "$fw.tmp" "$fw"; printf 'Rule deleted\n'; exit 0 ;;
  *) exit 0 ;;
esac
""",
	"firewall-cmd": r"""#!/usr/bin/env bash
set -euo pipefail
J=${MAI_INSTALL_JOURNAL:-${MAI_UNINSTALL_JOURNAL:-}}
[[ -n $J ]] && printf 'firewall-cmd %s\n' "$*" >>"$J"
fw="$MAI_UNINSTALL_SANDBOX/firewalld-rules.txt"
if [[ ${1:-} == --state ]]; then printf 'running\n'; exit 0; fi
if [[ ${1:-} == --permanent && ${2:-} == --list-rich-rules ]]; then [[ -f $fw ]] && cat "$fw"; exit 0; fi
if [[ ${1:-} == --permanent && ${2:-} == --add-rich-rule=* ]]; then printf '%s\n' "${2#--add-rich-rule=}" >>"$fw"; printf 'success\n'; exit 0; fi
if [[ ${1:-} == --permanent && ${2:-} == --remove-rich-rule=* ]]; then
  line="${2#--remove-rich-rule=}"
  [[ -f $fw ]] || exit 0
  grep -vxF "$line" "$fw" >"$fw.tmp" || true
  mv "$fw.tmp" "$fw"
  printf 'success\n'; exit 0
fi
if [[ ${1:-} == --reload ]]; then exit 0; fi
exit 1
""",
	"docker": r"""#!/usr/bin/env bash
set -euo pipefail
J=${MAI_INSTALL_JOURNAL:-${MAI_UNINSTALL_JOURNAL:-}}
[[ -n $J ]] && printf 'docker %s\n' "$*" >>"$J"
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
      down) printf 'Container mai-removed\n' ;;
      ps) printf '[]\n' ;;
    esac ;;
esac
exit 0
""",
}


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
	"""One disposable sandbox: shims, fake account db, seeded neighbor rules."""
	root = tmp_path / "mai-fxt-sb"
	(root / "etc" / "sudoers.d").mkdir(parents=True)
	(root / "home").mkdir()
	(root / "passwd.db").write_text("", encoding="utf-8")
	_write_shims(root)
	# Real fixture host identity for fingerprint verification in rotate mode.
	subprocess.run(
		["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / "hostkey")],
		check=True, capture_output=True,
	)
	(root / "controller.pub").write_text(CONTROLLER_PUBKEY + "\n", encoding="utf-8")
	(root / "rotated.pub").write_text(NEW_CONTROLLER_PUBKEY + "\n", encoding="utf-8")
	(root / "third.pub").write_text(THIRD_PUBKEY + "\n", encoding="utf-8")
	(root / "foreign.pub").write_text(FOREIGN_PUBKEY + "\n", encoding="utf-8")
	(root / "ufw-rules.txt").write_text(
		"".join(line + "\n" for line in UFW_NEIGHBORS), encoding="utf-8"
	)
	(root / "firewalld-rules.txt").write_text(
		FIREWALLD_NEIGHBOR + "\n", encoding="utf-8"
	)
	return root


def install_args(root: Path, *, profiles: str = "core", extra: tuple[str, ...] = ()) -> list[str]:
	return [
		"--server-id", SERVER_ID,
		"--data-root", str(root / "data-root"),
		"--public-key", str(root / "controller.pub"),
		"--grant-scope", "narrow",
		"--controller-address", CONTROLLER_ADDR,
		"--host-key-pub", str(root / "hostkey.pub"),
		"--repo", str(REPO_ROOT),
		"--env-file", str(root / "stack.env"),
		"--stack-profiles", profiles,
		*extra,
	]


def uninstall_args(root: Path, *, intent: str = "--keep-data",
                   extra: tuple[str, ...] = ()) -> list[str]:
	return [
		"--data-root", str(root / "data-root"),
		"--public-key", str(root / "controller.pub"),
		"--controller-address", CONTROLLER_ADDR,
		"--repo", str(REPO_ROOT),
		"--env-file", str(root / "stack.env"),
		"--stack-profiles", "core",
		intent,
		*extra,
	]


def rotate_args(root: Path, *, old: str = "controller.pub", new: str = "rotated.pub",
                extra: tuple[str, ...] = ()) -> list[str]:
	return [
		"--rotate-key", str(root / new),
		"--public-key", str(root / old),
		"--ssh-user", "mai",
		"--host-key-pub", str(root / "hostkey.pub"),
		*extra,
	]


def _env(root: Path) -> dict[str, str]:
	return {
		**os.environ,
		"PATH": f"{_write_shims(root)}:{os.environ['PATH']}",
		"MAI_UNINSTALL_SANDBOX": str(root),
		"MAI_INSTALL_SANDBOX": str(root),
		"MAI_UNINSTALL_SKIP_ROOT_CHECK": "1",
		"MAI_INSTALL_SKIP_ROOT_CHECK": "1",
		# Separate journals: assertions on the uninstall phase must never see
		# the provisioning commands of the round-trip setup.
		"MAI_UNINSTALL_JOURNAL": str(root / "journal-uninstall.log"),
		"MAI_INSTALL_JOURNAL": str(root / "journal-install.log"),
		"TMPDIR": str(root),  # even mktemp files stay inside the sandbox
	}


def run_install(root: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	env = _env(root)
	env.pop("MAI_UNINSTALL_JOURNAL")  # shims prefer the install journal
	env["MAI_INSTALL_CLI"] = _cli_command()
	for key, value in (extra_env or {}).items():
		if value is None:
			env.pop(key, None)
		else:
			env[key] = value
	return subprocess.run(
		["bash", str(INSTALLER), *args],
		capture_output=True, text=True, env=env, cwd=str(root), timeout=300,
	)


def run_uninstall(root: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	env = _env(root)
	env.pop("MAI_INSTALL_JOURNAL")  # shims prefer the uninstall journal
	env["MAI_UNINSTALL_CLI"] = _cli_command()
	for key, value in (extra_env or {}).items():
		if value is None:
			env.pop(key, None)
		else:
			env[key] = value
	return subprocess.run(
		["bash", str(UNINSTALLER), *args],
		capture_output=True, text=True, env=env, cwd=str(root), timeout=300,
	)


@pytest.fixture()
def installed(sb: Path) -> Path:
	"""A sandbox provisioned by the REAL installer (round-trip baseline).

	The service account pre-exists with an unrelated admin key, exactly the
	rerun-safety state the installer documents: the controller key is appended
	next to it, and the uninstaller must later revoke ONLY its own line.
	"""
	home = sb / "home" / "mai"
	(home / ".ssh").mkdir(parents=True, exist_ok=True)
	auth = home / ".ssh" / "authorized_keys"
	auth.write_text(FOREIGN_PUBKEY + "\n", encoding="utf-8")
	auth.chmod(0o600)
	(sb / "passwd.db").write_text(
		f"mai:x:4242:4242:mai-controller:{home}:/bin/bash\n", encoding="utf-8"
	)
	proc = run_install(sb, *install_args(sb), "--yes")
	assert proc.returncode == 0, proc.stderr + proc.stdout
	content = sudoers(sb).read_text(encoding="utf-8")
	assert content.startswith(MANAGED_MARKER), content
	assert NARROW_SUDOERS_LINE in content, "the baseline is the installer's exact managed grant"
	return sb


def journal(root: Path) -> list[str]:
	log = root / "journal-uninstall.log"
	return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def journal_count(root: Path, predicate) -> int:
	return sum(1 for line in journal(root) if predicate(line))


def only_readonly_probes(root: Path) -> bool:
	"""True when the uninstaller ran nothing but read-only state probes."""
	return all(
		line.startswith(("getent", "ufw status", "firewall-cmd"))
		for line in journal(root)
	)


def auth_keys(sb: Path) -> Path:
	return sb / "home" / "mai" / ".ssh" / "authorized_keys"


def sudoers(sb: Path) -> Path:
	return sb / "etc" / "sudoers.d" / "90-mai-mai"


def data_root(sb: Path) -> Path:
	return sb / "data-root"


def stored_profile(sb: Path) -> Path:
	return data_root(sb) / "servers" / SERVER_ID / "profile.yaml"


def fingerprint_of(sb: Path) -> str:
	out = subprocess.run(
		["ssh-keygen", "-lf", str(sb / "hostkey.pub")],
		check=True, capture_output=True, text=True,
	).stdout
	return out.split()[1]


def read_mode(path: Path) -> int:
	return stat.S_IMODE(path.stat().st_mode)


def assert_foreign_lines_preserved(sb: Path) -> None:
	text = auth_keys(sb).read_text(encoding="utf-8")
	assert FOREIGN_PUBKEY in text, "the unrelated admin key must survive byte-for-byte"


# ---------------------------------------------------------------------------
# static + plan lane
# ---------------------------------------------------------------------------


def test_uninstaller_syntax_and_help_surface() -> None:
	syntax = subprocess.run(["bash", "-n", str(UNINSTALLER)], capture_output=True, text=True)
	assert syntax.returncode == 0, syntax.stderr
	helped = subprocess.run(["bash", str(UNINSTALLER), "--help"], capture_output=True, text=True)
	assert helped.returncode == 0
	# Both modes documented, plus the documented test seams (never hidden).
	assert "--rotate-key" in helped.stdout and "--keep-data" in helped.stdout
	assert "MAI_UNINSTALL_SKIP_ROOT_CHECK" in helped.stdout
	assert "MAI_UNINSTALL_SANDBOX" in helped.stdout


def test_negative_missing_required_arguments_fail_without_mutation(sb: Path) -> None:
	# Each drop leaves the invocation incomplete: usage error, no command run.
	for drop in ("--keep-data", "--controller-address", "--data-root", "--public-key"):
		args = uninstall_args(sb, extra=("--yes",))
		pos = args.index(drop)
		incomplete = args[:pos] + args[pos + 2:]
		proc = run_uninstall(sb, *incomplete)
		assert proc.returncode == 2, (drop, proc.stdout, proc.stderr)
	assert journal(sb) == [], "a usage refusal must run no command at all"


def test_negative_blanket_and_broader_than_8_addresses_refused(sb: Path) -> None:
	# The uninstaller only ever removes the ONE scoped rule; blanket or /0–/7
	# inputs can never name it. Refused at validation time (exit 2, before the
	# root gate), so this also proves refusal happens without any effect.
	for blanket in ("0.0.0.0/0", "any", "10.1.2.3/1", "203.0.113.9/0"):
		proc = run_uninstall(
			sb, "--data-root", str(data_root(sb)),
			"--public-key", str(sb / "controller.pub"),
			"--controller-address", blanket, "--keep-data", "--yes",
		)
		assert proc.returncode == 2, (blanket, proc.stdout, proc.stderr)
		# "any"/non-CIDR die at the grammar check, CIDR blankets at the scope
		# check — both are refusals before any effect.
		assert (
			"blanket controller address" in proc.stderr
			or "is not an IPv4 address or CIDR" in proc.stderr
		), blanket
	assert journal(sb) == []
	assert not data_root(sb).exists()


def test_plan_uninstall_inventory_is_exact_and_read_only(installed: Path) -> None:
	sb = installed
	before_keys = auth_keys(sb).read_bytes()
	before_rules = (sb / "ufw-rules.txt").read_bytes()
	plan = run_uninstall(sb, *uninstall_args(sb), "--print-plan")
	assert plan.returncode == 0, plan.stderr
	# The plan names every owned artifact and its current state...
	assert "== mai uninstall plan / inventory (nothing has been applied) ==" in plan.stdout
	assert f"{sudoers(sb)} — managed file, will be removed" in plan.stdout
	assert SCOPED_RULE in plan.stdout
	assert "--keep-data" in plan.stdout and "docs/operations/uninstall.md" in plan.stdout
	assert "mai — KEPT (pass --remove-user" in plan.stdout
	assert "KEPT" in plan.stdout  # the .env retention explanation
	# ...and running it changed nothing but read-only probes.
	assert only_readonly_probes(sb), journal(sb)
	assert auth_keys(sb).read_bytes() == before_keys
	assert (sb / "ufw-rules.txt").read_bytes() == before_rules
	assert sudoers(sb).is_file()


def test_plan_rotation_is_read_only_and_presents_trust(installed: Path) -> None:
	sb = installed
	before = auth_keys(sb).read_bytes()
	plan = run_uninstall(sb, *rotate_args(sb), "--print-plan")
	assert plan.returncode == 0, plan.stderr
	assert "== mai rotate plan (nothing has been applied) ==" in plan.stdout
	assert fingerprint_of(sb) in plan.stdout, "the plan presents the host fingerprint for verification"
	assert "NEVER changed" in plan.stdout
	assert auth_keys(sb).read_bytes() == before
	assert sudoers(sb).is_file(), "rotation must not touch grants"
	assert only_readonly_probes(sb), journal(sb)


# ---------------------------------------------------------------------------
# rotation lane (acceptance 2: verifiable host trust, unrelated access kept)
# ---------------------------------------------------------------------------


def test_rotate_replaces_exact_line_and_preserves_unrelated_state(installed: Path) -> None:
	sb = installed
	proc = run_uninstall(sb, *rotate_args(sb), "--yes")
	assert proc.returncode == 0, proc.stderr + proc.stdout
	text = auth_keys(sb).read_text(encoding="utf-8")
	assert CONTROLLER_PUBKEY not in text, "the old controller key is revoked"
	assert text.count(NEW_CONTROLLER_PUBKEY) == 1, "the new key is present exactly once"
	assert_foreign_lines_preserved(sb)
	assert read_mode(auth_keys(sb)) == 0o600
	# Verifiable host trust: the same fingerprint is echoed, and nothing else
	# (grants, firewall, stack, data) was executed or changed.
	assert fingerprint_of(sb) in proc.stdout
	assert sudoers(sb).is_file()
	assert SCOPED_RULE in (sb / "ufw-rules.txt").read_text(encoding="utf-8")
	assert stored_profile(sb).is_file()
	assert "host_key:" in stored_profile(sb).read_text(encoding="utf-8")
	assert journal_count(sb, lambda line: line.startswith(("docker", "visudo", "userdel"))) == 0
	assert journal_count(sb, lambda line: line.startswith("ufw delete")) == 0


def test_rotation_chains_with_rerun_safety(installed: Path) -> None:
	sb = installed
	first = run_uninstall(sb, *rotate_args(sb), "--yes")
	assert first.returncode == 0, first.stderr
	second = run_uninstall(sb, *rotate_args(sb, old="rotated.pub", new="third.pub"), "--yes")
	assert second.returncode == 0, second.stderr
	text = auth_keys(sb).read_text(encoding="utf-8")
	assert NEW_CONTROLLER_PUBKEY not in text and text.count(THIRD_PUBKEY) == 1
	assert_foreign_lines_preserved(sb)


def test_negative_rotate_without_old_key_refuses(installed: Path) -> None:
	sb = installed
	keys = auth_keys(sb)
	keys.write_text(FOREIGN_PUBKEY + "\n", encoding="utf-8")  # controller line gone
	proc = run_uninstall(sb, *rotate_args(sb), "--yes")
	assert proc.returncode == 1, proc.stdout
	assert "nothing to rotate" in proc.stderr
	assert NEW_CONTROLLER_PUBKEY not in keys.read_text(), "no authority is added blind"
	assert_foreign_lines_preserved(sb)


def test_negative_rotate_with_invalid_or_identical_key_refuses(installed: Path) -> None:
	sb = installed
	before = auth_keys(sb).read_bytes()
	(sb / "bad.pub").write_text("not an openssh public key\n", encoding="utf-8")
	invalid = run_uninstall(sb, *rotate_args(sb, new="bad.pub"), "--yes")
	assert invalid.returncode == 2, invalid.stdout
	identical = run_uninstall(sb, *rotate_args(sb, new="controller.pub"), "--yes")
	assert identical.returncode == 2, identical.stdout
	assert auth_keys(sb).read_bytes() == before


def test_negative_rotate_cannot_smuggle_uninstall_flags(sb: Path) -> None:
	proc = run_uninstall(sb, *rotate_args(sb, extra=("--keep-data",)), "--yes")
	assert proc.returncode == 2
	assert "rotation only" in proc.stderr


# ---------------------------------------------------------------------------
# uninstall lane (acceptance 1: revoke ONLY owned artifacts; explicit data intent)
# ---------------------------------------------------------------------------


def test_uninstall_keep_data_removes_only_owned_artifacts(installed: Path) -> None:
	sb = installed
	proc = run_uninstall(sb, *uninstall_args(sb), "--yes")
	assert proc.returncode == 0, proc.stderr + proc.stdout
	# grants: the managed sudoers file is gone
	assert not sudoers(sb).exists()
	# firewall: only the scoped rule; the seeded neighbors are byte-identical
	rules = (sb / "ufw-rules.txt").read_text(encoding="utf-8")
	assert SCOPED_RULE not in rules
	assert rules.splitlines() == UFW_NEIGHBORS, rules
	assert journal_count(sb, lambda line: line.startswith("ufw delete")) == 1
	# authorized_keys: exact controller line only; the foreign line survives
	text = auth_keys(sb).read_text(encoding="utf-8")
	assert CONTROLLER_PUBKEY not in text
	assert_foreign_lines_preserved(sb)
	assert read_mode(auth_keys(sb)) == 0o600
	# stack: compose down WITHOUT volume purge on keep-data
	down_calls = [line for line in journal(sb) if line.startswith("docker compose") and " down" in line]
	assert len(down_calls) == 1 and " -v" not in down_calls[0], down_calls
	# account: kept by default
	assert "mai:" in (sb / "passwd.db").read_text(encoding="utf-8")
	# data half: the REAL CLI retained the data root (explicit keep intent)
	assert data_root(sb).is_dir() and stored_profile(sb).is_file()
	assert '"deleted": false' in proc.stdout and '"retained_entries"' in proc.stdout
	# the inventory is exact about what was and was not touched
	assert "grants        : removed" in proc.stdout
	assert "firewall      : ufw (removed)" in proc.stdout
	assert "key           : removed (1 line(s))" in proc.stdout
	assert "stack         : down (profiles: core)" in proc.stdout
	assert f"data root     : retained {data_root(sb)}" in proc.stdout
	assert "account       : mai (kept" in proc.stdout
	assert "env file      : KEPT" in proc.stdout


def test_uninstall_delete_data_purges_volumes_and_data_root(installed: Path) -> None:
	sb = installed
	proc = run_uninstall(sb, *uninstall_args(sb, intent="--delete-data"), "--yes")
	assert proc.returncode == 0, proc.stderr + proc.stdout
	assert not data_root(sb).exists(), "the CLI deleted the MAI-owned data root"
	down_calls = [line for line in journal(sb) if line.startswith("docker compose") and " down" in line]
	assert len(down_calls) == 1 and " -v" in down_calls[0] and "--remove-orphans" in down_calls[0]
	assert f"data root     : deleted {data_root(sb)}" in proc.stdout
	assert "volumes purged" in proc.stdout


def test_uninstall_firewalld_variant_removes_exact_rich_rule(sb: Path) -> None:
	# Same round trip on a firewalld-only host: the ufw shim reports inactive
	# (documented MAI_FXT_UFW_INACTIVE fixture seam) so the installer takes
	# the firewalld path next to the seeded neighbor; the uninstaller removes
	# exactly that one rich rule.
	inactive = {"MAI_FXT_UFW_INACTIVE": "1"}
	proc = run_install(sb, *install_args(sb), "--yes", extra_env=inactive)
	assert proc.returncode == 0, proc.stderr + proc.stdout
	rules = sb / "firewalld-rules.txt"
	assert SCOPED_RICH_RULE in rules.read_text(encoding="utf-8")
	assert SCOPED_RULE not in (sb / "ufw-rules.txt").read_text(encoding="utf-8"), "an inactive ufw must stay untouched"
	out = run_uninstall(sb, *uninstall_args(sb), "--yes", extra_env=inactive)
	assert out.returncode == 0, out.stderr + out.stdout
	assert rules.read_text(encoding="utf-8").splitlines() == [FIREWALLD_NEIGHBOR]
	assert "firewalld (removed)" in out.stdout


def test_uninstall_is_rerun_safe(installed: Path) -> None:
	sb = installed
	first = run_uninstall(sb, *uninstall_args(sb), "--yes")
	assert first.returncode == 0, first.stderr
	second = run_uninstall(sb, *uninstall_args(sb), "--yes")
	assert second.returncode == 0, second.stderr + second.stdout
	# Second pass: everything already absent/not-present, no duplicate effects.
	assert "already absent" in second.stdout
	assert "not present" in second.stdout
	assert journal_count(sb, lambda line: line.startswith("ufw delete")) == 1
	assert not sudoers(sb).exists()
	assert stored_profile(sb).is_file()  # keep-data retained it again, unchanged


def test_uninstall_remove_user_only_for_key_only_accounts(installed: Path) -> None:
	sb = installed
	proc = run_uninstall(sb, *uninstall_args(sb, extra=("--remove-user",)), "--yes")
	assert proc.returncode == 0, proc.stderr + proc.stdout
	assert "mai:" not in (sb / "passwd.db").read_text(encoding="utf-8")
	assert not (sb / "home" / "mai").exists()
	assert "account       : mai (removed)" in proc.stdout
	# the data half still ran with the explicit keep intent
	assert stored_profile(sb).is_file()


def test_negative_remove_user_with_foreign_home_refuses_early(installed: Path) -> None:
	sb = installed
	(sb / "home" / "mai" / "operator-notes.txt").write_text("not MAI's\n", encoding="utf-8")
	proc = run_uninstall(sb, *uninstall_args(sb, extra=("--remove-user",)), "--yes")
	assert proc.returncode == 1, proc.stdout
	assert "holds entries beyond .ssh" in proc.stderr
	# refused in the preflight: nothing beyond the read-only probes ran
	assert only_readonly_probes(sb), journal(sb)
	assert "mai:" in (sb / "passwd.db").read_text(encoding="utf-8")
	assert (sb / "home" / "mai" / "operator-notes.txt").is_file()


def test_negative_foreign_sudoers_at_managed_path_refuses_early(installed: Path) -> None:
	sb = installed
	foreign = sudoers(sb)
	foreign.chmod(0o644)  # the managed file is mode 440; relax to overwrite as fixture
	foreign.write_text(
		"# locally crafted by the previous admin\nadm ALL=(ALL) ALL\n",
		encoding="utf-8",
	)
	proc = run_uninstall(sb, *uninstall_args(sb), "--yes")
	assert proc.returncode == 1, proc.stdout
	assert "not written by scripts/install-host.sh" in proc.stderr
	# the refusal happens in the preflight, before any state change
	assert only_readonly_probes(sb), journal(sb)
	assert foreign.read_text(encoding="utf-8").startswith("# locally crafted")
	assert stored_profile(sb).is_file()  # data untouched too


def test_negative_delete_data_with_foreign_entries_is_refused_by_the_cli(installed: Path) -> None:
	sb = installed
	(data_root(sb) / "operator-archive").mkdir()
	proc = run_uninstall(sb, *uninstall_args(sb, intent="--delete-data"), "--yes")
	assert proc.returncode == 1, proc.stdout
	assert "foreign_entries" in proc.stderr, proc.stderr
	assert "operator-archive" in proc.stderr
	# the data root is intact: only the CLI producer may delete managed data
	assert stored_profile(sb).is_file()
	assert (data_root(sb) / "operator-archive").is_dir()


def test_negative_noninteractive_run_without_consent_refuses(installed: Path) -> None:
	sb = installed
	before = auth_keys(sb).read_bytes()
	proc = run_uninstall(sb, *uninstall_args(sb))  # no --yes, stdin is a pipe
	assert proc.returncode == 2, proc.stdout
	assert "without consent" in proc.stderr
	assert only_readonly_probes(sb), journal(sb)
	assert auth_keys(sb).read_bytes() == before
	assert sudoers(sb).is_file()


def test_negative_unprivileged_run_refused_without_root_bypass(sb: Path) -> None:
	# The suite runs unprivileged (EUID != 0); without the documented
	# MAI_UNINSTALL_SKIP_ROOT_CHECK=1 seam — unset OR 0 — the uninstaller must
	# refuse BEFORE plan/consent/apply: exit 1, no command, no host effect.
	for skip in (None, "0"):
		proc = run_uninstall(
			sb, *uninstall_args(sb, extra=("--print-plan",)),
			extra_env={"MAI_UNINSTALL_SKIP_ROOT_CHECK": skip},
		)
		assert proc.returncode == 1, (skip, proc.stdout)
		assert "must run as root (EUID 0)" in proc.stderr
		assert "MAI_UNINSTALL_SKIP_ROOT_CHECK=1" in proc.stderr
		assert not data_root(sb).exists()
	# --help is handled before the gate and stays available.
	helped = run_uninstall(sb, "--help")
	assert helped.returncode == 0, helped.stderr


def test_negative_cli_failure_surfaces_and_keeps_data(installed: Path) -> None:
	sb = installed
	# A failing CLI producer proves the script surfaces the failure and never
	# deletes data itself: the data half belongs to mai.cli alone.
	proc = run_uninstall(sb, *uninstall_args(sb), "--yes", extra_env={"MAI_UNINSTALL_CLI": "false"})
	assert proc.returncode == 1, proc.stdout
	assert data_root(sb).is_dir(), "the script never deletes the data root on its own"
