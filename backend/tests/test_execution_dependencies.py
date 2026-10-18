"""Dependency lane guard for the P04 SSH/host-execution set (card 04-dependencies).

Acceptance criteria (card brief):
1. Only P00-selected supported SSH/host execution libraries are added; license
   and API version recorded.
2. Clean locked install imports the required production adapters; no invented
   third-party API.

P00 selection (docs/architecture/host-supervision.md; probe
experiments/ssh_supervision_probe.py): command ownership travels the SYSTEM
OpenSSH client driven via subprocess — deliberately NO third-party SSH
library. The runtime dependency is therefore the host ``ssh`` / ``ssh-keygen``
/ ``ssh-keyscan`` binaries, which uv.lock cannot pin; this guard asserts their
presence, version and the frozen probe option surface at runtime, and holds
the negative control that no unselected SSH distribution enters the manifest,
the lock or the installed environment. The production adapters
(``mai.execution.*``, downstream cards 04-ssh-connect …) build on exactly this
surface: stdlib subprocess plus the client options verified below — nothing
invented.
"""

import re
import shutil
import subprocess
import tomllib
from importlib.util import find_spec
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]

# Distributions that would silently add an unselected SSH/execution stack.
# Selection authority: P00 ssh spike (host-supervision.md) measured the
# wrapper/receipt scheme over the system OpenSSH client and selected NO
# third-party SSH library; asyncssh joined this list when the failed first
# round was reverted (review r-04-dependencies).
UNSELECTED_SSH_DISTRIBUTIONS = (
    "asyncssh",
    "paramiko",
    "fabric",
    "plumbum",
    "pexpect",
    "libssh",
)

# Frozen probe option surface (experiments/ssh_supervision_probe.py
# ssh_opts()): the only ssh(1) options P04 adapters may rely on.
PROBE_SSH_OPTIONS = (
    "IdentitiesOnly=yes",
    "BatchMode=yes",
    "StrictHostKeyChecking=yes",
    "UserKnownHostsFile=/tmp/mai-pinned-known-hosts",
    "ConnectTimeout=5",
)

# Documented coordinator baseline (docs/architecture/runtime-environment.md
# capability table: "client OpenSSH 9.2p1 (Debian)").
MINIMUM_OPENSSH = (9, 2)


def _declared_requirements() -> list[str]:
    with (BACKEND_DIR / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return list(data["project"]["dependencies"])


def _distribution_names(requirements: list[str]) -> list[str]:
    """PEP 508 entry -> lowercase distribution name (markers/versions stripped)."""
    return [re.split(r"[<>=!~\[\s]", entry.split(";")[0].strip(), maxsplit=1)[0].lower()
            for entry in requirements]


def test_manifest_declares_no_ssh_library() -> None:
    declared = _distribution_names(_declared_requirements())
    offenders = sorted(set(declared) & set(UNSELECTED_SSH_DISTRIBUTIONS))
    assert offenders == [], f"unselected SSH libraries in pyproject: {offenders}"


def test_lock_contains_no_ssh_distribution() -> None:
    with (BACKEND_DIR / "uv.lock").open("rb") as handle:
        locked = {package["name"].lower() for package in tomllib.load(handle)["package"]}
    offenders = sorted(locked & set(UNSELECTED_SSH_DISTRIBUTIONS))
    assert offenders == [], f"unselected SSH distributions in uv.lock: {offenders}"


def test_locked_environment_has_no_ssh_distribution_installed() -> None:
    present = sorted(name for name in UNSELECTED_SSH_DISTRIBUTIONS if find_spec(name) is not None)
    assert present == [], f"unselected SSH distributions importable in locked env: {present}"


def test_lock_is_consistent_with_manifest() -> None:
    proc = subprocess.run(
        ["uv", "lock", "--check"], cwd=BACKEND_DIR, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"uv.lock out of sync with pyproject: {proc.stdout}{proc.stderr}"


def test_system_ssh_binaries_present() -> None:
    # Probe roles need all three: ssh (transport), ssh-keygen (ed25519 key),
    # ssh-keyscan (host-key pinning) — preflight role of the P00 probe.
    missing = [binary for binary in ("ssh", "ssh-keygen", "ssh-keyscan") if shutil.which(binary) is None]
    assert missing == [], f"system SSH binaries missing from PATH: {missing}"


def test_system_ssh_version_meets_documented_floor() -> None:
    proc = subprocess.run(["ssh", "-V"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    match = re.search(r"OpenSSH_(\d+)\.(\d+)", proc.stderr)
    assert match is not None, f"unparsable ssh version banner: {proc.stderr!r}"
    version = (int(match[1]), int(match[2]))
    assert version >= MINIMUM_OPENSSH, f"OpenSSH {version} below documented floor {MINIMUM_OPENSSH}"


def test_system_ssh_accepts_frozen_probe_option_surface() -> None:
    # ssh -G renders the effective client configuration offline (never dials);
    # rc 0 plus the echoed values prove this client understands every option
    # the P00 probe froze — the adapters' API is real, not invented.
    cmd = ["ssh", "-G"]
    for option in PROBE_SSH_OPTIONS:
        cmd += ["-o", option]
    # .invalid destination: -G renders config offline, it never dials (D5).
    cmd.append("mai-p04-option-probe.invalid")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    rendered = proc.stdout.lower()
    assert "userknownhostsfile /tmp/mai-pinned-known-hosts" in rendered
    # Rendering varies by release: 9.2 canonicalizes yes -> true.
    strict = re.search(r"stricthostkeychecking (\S+)", rendered)
    assert strict is not None, f"stricthostkeychecking missing from -G output: {rendered!r}"
    assert strict[1] in {"yes", "true"}, strict[1]
    assert "identitiesonly yes" in rendered
    assert "batchmode yes" in rendered
    assert "connecttimeout 5" in rendered
