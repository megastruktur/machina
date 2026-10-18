"""Dependency lane guard for the P11 optional integration set (card 11-dependencies).

Acceptance criteria (card brief):
1. Optional Hindsight/1Password adapter requirements match supported APIs and
   licenses; .env-only mode remains installable.
2. Missing optional external service/tool produces explicit capability
   unavailable instead of core import failure.

Selection provenance — both optional integrations pin ZERO Python
distributions, by reviewed decision, and this guard freezes that decision:

- Operational memory (IDEA.md §11, contracts.md C07 rules 6/7, ADR
  knowledge-packaging.md §2): Hindsight enters as the Compose-internal
  SERVICE (ghcr.io/vectorize-io/hindsight, version pinned by the Compose
  card, MIT upstream); the 11-memory-adapter speaks its bank-scoped HTTP API
  through the httpx2 client already pinned by 07-dependencies (the frozen
  one-HTTP-stack knowledge lane). 07-dependencies' reviewed negative control
  (set B) already holds the in-process Hindsight packages
  (hindsight-api/all/client/litellm) out of manifest, lock and environment —
  pip-installing the engine would drag Postgres drivers and ONNX extras into
  the FastAPI set (ADR §2.1) and duplicate a process the stack already
  isolates. This card re-asserts that set and extends it with unselected
  operational-memory frameworks (cognee, letta, mem0ai, zep-cloud).
- 1Password (IDEA.md §14, contracts.md C03 rule 6: .env is the mandatory
  first resolver, 1Password an optional later plugin): the supported
  interface is the 1Password CLI (``op``) driven via subprocess — the same
  selection rule as the system OpenSSH client (04-dependencies fix round:
  system tool over third-party library). Service accounts, the plugin's auth
  mode, require CLI >= 2.18.0 (developer.1password.com, "Use service
  accounts with 1Password CLI"). The official onepassword-sdk distribution
  is NOT selected: its wheels stop at CPython 3.13 (Rust sdist beyond), so
  it cannot install into the python:3.14-slim runtime set (backend/
  Dockerfile), and the CLI it wraps is the documented interface anyway.
  The ``op`` binary is OPTIONAL on any host: this guard asserts the version
  floor when it is present and records explicit unavailability when it is
  not — the 11-onepassword plugin card owns classifying that unavailability
  without a fallback secret scan.

The structural form of acceptance criterion 2 at this lane: the optional
distributions are absent from the installed environment AND the core package
imports anyway — a missing service/tool can therefore only degrade a
capability at its own discovery boundary, never break a core import.
"""

import re
import shutil
import subprocess
import sys
import tomllib
from importlib.util import find_spec
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]

# In-process Hindsight packages — 07-dependencies negative-control set B,
# preserved verbatim (ADR §2.1: Compose image is the selected packaging).
IN_PROCESS_HINDSIGHT_DISTRIBUTIONS = (
    "hindsight-api",
    "hindsight-all",
    "hindsight-client",
    "hindsight-litellm",
)
IN_PROCESS_HINDSIGHT_IMPORTS = (
    "hindsight_api",
    "hindsight_client",
    "hindsight_litellm",
)

# Unselected operational-memory frameworks: the memory capability is the
# pinned Hindsight service + the httpx2 adapter lane; no alternate in-process
# memory framework may enter the controller instead.
UNSELECTED_MEMORY_FRAMEWORK_DISTRIBUTIONS = ("cognee", "letta", "mem0ai", "zep-cloud")
UNSELECTED_MEMORY_FRAMEWORK_IMPORTS = ("cognee", "letta", "mem0", "zep_cloud")

# Unselected secret-store SDKs: .env is the mandatory first resolver and the
# 1Password plugin drives the `op` CLI; no third-party secret-store client
# library may enter the dependency set (C03 rule 6 — names resolve only from
# the configured source).
SECRET_STORE_SDK_DISTRIBUTIONS = ("hvac", "keyring", "onepassword-sdk")
SECRET_STORE_SDK_IMPORTS = ("hvac", "keyring", "onepassword")

# 1Password CLI: optional tool, service-account capability floor.
OP_BINARY = "op"
OP_SERVICE_ACCOUNT_FLOOR = (2, 18, 0)


def _manifest_tables() -> dict[str, list[str]]:
    """Every PEP 508 requirement table of the manifest keyed by table name."""
    with (BACKEND_DIR / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    tables: dict[str, list[str]] = {
        "project.dependencies": list(data["project"]["dependencies"]),
        "project.optional-dependencies": [
            entry
            for entries in data["project"].get("optional-dependencies", {}).values()
            for entry in entries
        ],
        "dependency-groups": [
            entry
            for entries in data.get("dependency-groups", {}).values()
            for entry in entries
        ],
    }
    return tables


def _distribution_names(requirements: list[str]) -> list[str]:
    """PEP 508 entry -> lowercase distribution name (markers/versions stripped)."""
    return [
        re.split(r"[<>=!~\[\s]", entry.split(";")[0].strip(), maxsplit=1)[0].lower()
        for entry in requirements
    ]


def _locked_packages() -> dict[str, str]:
    with (BACKEND_DIR / "uv.lock").open("rb") as handle:
        data = tomllib.load(handle)
    return {
        package["name"].lower(): package["version"]
        for package in data["package"]
    }


def _importable(name: str) -> bool:
    return find_spec(name) is not None


def test_manifest_declares_no_optional_dependency_table() -> None:
    # The .env-only mode IS the only mode: no PEP 508 extra exists, so the
    # default locked install (uv sync --frozen) is the complete runtime set.
    # Adding extras later is a deliberate reconciliation with this guard.
    tables = _manifest_tables()
    assert tables["project.optional-dependencies"] == [], (
        f"optional-dependencies entries appeared: "
        f"{tables['project.optional-dependencies']}"
    )


def test_manifest_declares_no_in_process_hindsight_distribution() -> None:
    declared = _distribution_names(
        [e for entries in _manifest_tables().values() for e in entries]
    )
    offenders = sorted(set(declared) & set(IN_PROCESS_HINDSIGHT_DISTRIBUTIONS))
    assert offenders == [], f"in-process Hindsight packages in pyproject: {offenders}"


def test_lock_contains_no_in_process_hindsight_distribution() -> None:
    locked = _locked_packages()
    offenders = sorted(locked.keys() & set(IN_PROCESS_HINDSIGHT_DISTRIBUTIONS))
    assert offenders == [], f"in-process Hindsight packages in uv.lock: {offenders}"


def test_locked_environment_has_no_in_process_hindsight_importable() -> None:
    present = sorted(name for name in IN_PROCESS_HINDSIGHT_IMPORTS if _importable(name))
    assert present == [], f"in-process Hindsight packages importable in locked env: {present}"


def test_manifest_declares_no_unselected_memory_framework() -> None:
    declared = _distribution_names(
        [e for entries in _manifest_tables().values() for e in entries]
    )
    offenders = sorted(set(declared) & set(UNSELECTED_MEMORY_FRAMEWORK_DISTRIBUTIONS))
    assert offenders == [], f"unselected memory frameworks in pyproject: {offenders}"


def test_lock_contains_no_unselected_memory_framework() -> None:
    locked = _locked_packages()
    offenders = sorted(locked.keys() & set(UNSELECTED_MEMORY_FRAMEWORK_DISTRIBUTIONS))
    assert offenders == [], f"unselected memory frameworks in uv.lock: {offenders}"


def test_locked_environment_has_no_unselected_memory_framework_importable() -> None:
    present = sorted(
        name for name in UNSELECTED_MEMORY_FRAMEWORK_IMPORTS if _importable(name)
    )
    assert present == [], f"unselected memory frameworks importable in locked env: {present}"


def test_manifest_declares_no_secret_store_sdk() -> None:
    declared = _distribution_names(
        [e for entries in _manifest_tables().values() for e in entries]
    )
    offenders = sorted(set(declared) & set(SECRET_STORE_SDK_DISTRIBUTIONS))
    assert offenders == [], f"secret-store SDKs in pyproject: {offenders}"


def test_lock_contains_no_secret_store_sdk() -> None:
    locked = _locked_packages()
    offenders = sorted(locked.keys() & set(SECRET_STORE_SDK_DISTRIBUTIONS))
    assert offenders == [], f"secret-store SDKs in uv.lock: {offenders}"


def test_locked_environment_has_no_secret_store_sdk_importable() -> None:
    present = sorted(name for name in SECRET_STORE_SDK_IMPORTS if _importable(name))
    assert present == [], f"secret-store SDKs importable in locked env: {present}"


def test_lock_is_consistent_with_manifest() -> None:
    proc = subprocess.run(
        ["uv", "lock", "--check"], cwd=BACKEND_DIR, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"uv.lock out of sync with pyproject: {proc.stdout}{proc.stderr}"


def test_core_imports_without_optional_integrations() -> None:
    # Acceptance criterion 2, structural form: with every optional
    # distribution absent (guards above), the core package and its
    # credential seam still import — a missing Hindsight service or `op`
    # tool can surface only as an explicit capability-unavailable outcome at
    # its own discovery boundary, never as a core import failure.
    import mai  # noqa: F401  (import is the check)
    import mai.credentials.env  # noqa: F401

    leaked = sorted(
        set(IN_PROCESS_HINDSIGHT_IMPORTS)
        | set(UNSELECTED_MEMORY_FRAMEWORK_IMPORTS)
        | set(SECRET_STORE_SDK_IMPORTS)
    )
    loaded = [name for name in leaked if name in sys.modules]
    assert loaded == [], f"optional integration modules leaked into core imports: {loaded}"


def test_op_cli_meets_service_account_floor_when_present() -> None:
    # Optional tool: absence is the expected state on hosts without the
    # 1Password integration (recorded, not fatal — criterion 2); presence is
    # held to the service-account floor (>= 2.18.0) the plugin's documented
    # auth mode requires.
    if shutil.which(OP_BINARY) is None:
        import pytest

        pytest.skip(
            f"{OP_BINARY} CLI not installed on this host — the optional 1Password "
            "capability is unavailable here (recorded); floor check not exercised"
        )
    proc = subprocess.run(
        [OP_BINARY, "--version"], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"{OP_BINARY} --version failed: {proc.stderr}"
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", proc.stdout.strip())
    assert match is not None, f"unparsable {OP_BINARY} version: {proc.stdout!r}"
    version_tuple = tuple(int(part) for part in match.groups())
    assert version_tuple >= OP_SERVICE_ACCOUNT_FLOOR, (
        f"{OP_BINARY} {'.'.join(map(str, version_tuple))} below service-account floor "
        f"{'.'.join(map(str, OP_SERVICE_ACCOUNT_FLOOR))}"
    )
