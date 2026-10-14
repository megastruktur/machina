"""Focused tests for the versioned grant-configuration store (03-policy-config).

Acceptance (BRIEF):
1. Canonical grant configuration survives restart with revision conflict
   handling and explicit target.
2. Current policy revision can be reloaded before each execution so
   revocation takes effect; generated prompts cannot write grants.

Boundary discipline (test policy, risk-critical slice): restart survival,
atomicity and crash-preservation claims are exercised on a real temporary
filesystem. The pre-replace interruption is proven twice — deterministically
in-process (``os.replace`` interrupted) and with a **real SIGKILL** delivered
to a child process instrumented to die exactly before the replace call; the
disk state after the kill is inspected by a fresh store instance (restart
semantics, AC1). Revocation is proven through the production
``mai.policy.evaluate`` decision function against freshly reloaded stores —
no cached state, so a revoke committed by any process denies on the next
reload (AC2). The prompt-prose negative control is a real safety boundary
(C02 rule 6 / C07 rule 5): generated prompt content is rejected before any
filesystem effect and can never enter the grants namespace. No DB, network
or ambient environment is touched; error payloads are checked value-free.
"""

from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from mai.plugins.registry import parse_descriptor
from mai.policy.config import (
    ABSENT_REVISION,
    CONFIG_META_FILENAME,
    CONFIG_FILENAME,
    CONFIG_SCHEMA_VERSION,
    MAX_DOCUMENT_BYTES,
    POLICY_DIR,
    GrantConfigError,
    GrantConfigErrorCode,
    GrantConfigStore,
    canonical_config_meta_relative_path,
    canonical_config_relative_path,
    main,
)
from mai.policy.evaluate import (
    DecisionReason,
    ExecutionContext,
    PolicyMode,
    evaluate,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

SERVER_A = uuid.UUID("0a0a0a0a-0000-0000-0000-00000000000a")
SERVER_B = uuid.UUID("0b0b0b0b-0000-0000-0000-00000000000b")
RUN_1 = uuid.UUID("01010101-0000-0000-0000-000000000001")

CAP = "deploy.app"

PROSE_SENTINEL = "GENERATED-RUNBOOK-SENTINEL-7f3a"

CLI = [sys.executable, "-m", "mai.policy.config"]


def _document(
    revision: int = 1,
    grants: list[dict[str, object]] | None = None,
    denied: list[str] | None = None,
) -> dict[str, object]:
    """One schema-valid candidate document (fixture grants on SERVER_A)."""
    if grants is None:
        grants = [{"capability_id": CAP, "server_ids": [str(SERVER_A)]}]
    return {
        "policy": {"revision": revision, "denied_capabilities": denied or []},
        "grants": grants,
    }


def _config_file(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "config-candidate.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _codes_of(exc: GrantConfigError) -> GrantConfigErrorCode:
    return exc.code


def _context(**overrides: object) -> ExecutionContext:
    values: dict[str, object] = {
        "principal": "admin-session-1",
        "server_id": SERVER_A,
        "run_id": RUN_1,
        "parent_scope": None,
        "profile_revision": "profile-rev-1",
        "prompt_revision": "prompt-rev-1",
        "credential_references": ("ENV:DEPLOY_TOKEN",),
        "allowed_capabilities": (CAP,),
        "mode": PolicyMode.DELEGATED,
    }
    values.update(overrides)
    return ExecutionContext(**values)  # type: ignore[arg-type]


def _descriptor():
    """A registry-validated descriptor document for ``deploy.app``."""
    return parse_descriptor(
        {
            "identifier": CAP,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "execution_environments": ["linux_ssh"],
            "effect_class": "mutate",
            "resource_scope": ["target.fs"],
            "required_grants": [CAP],
        }
    )


def _decide(stored, **overrides: object):
    """One production evaluation against a freshly reloaded configuration."""
    values: dict[str, object] = {
        "context": _context(),
        "capability_id": CAP,
        "descriptor": _descriptor(),
        "grants": stored.grants,
        "policy": stored.policy,
        "adopted": True,
    }
    values.update(overrides)
    return evaluate(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC1 — write/read by explicit root, revision triple, atomic replace
# ---------------------------------------------------------------------------


def test_create_write_read_roundtrip_revision_one(tmp_path: Path) -> None:
    store = GrantConfigStore(tmp_path / "data-root")
    stored = store.write(_document(revision=1), expected_revision=0)
    assert stored.revision == 1
    assert stored.schema_revision == CONFIG_SCHEMA_VERSION
    assert stored.policy.revision == 1
    assert stored.grants == {CAP: frozenset({SERVER_A})}
    assert store.read() == stored


def test_update_bumps_revision_and_replaces_content(tmp_path: Path) -> None:
    store = GrantConfigStore(tmp_path / "data-root")
    first = store.write(_document(revision=1), expected_revision=0)
    second = store.write(
        _document(
            revision=2,
            grants=[
                {"capability_id": CAP, "server_ids": [str(SERVER_A), str(SERVER_B)]}
            ],
        ),
        expected_revision=1,
    )
    assert (first.revision, second.revision) == (1, 2)
    reread = store.read()
    assert reread.revision == 2
    assert reread.policy.revision == 2
    assert reread.grants == {CAP: frozenset({SERVER_A, SERVER_B})}


def test_stale_write_rejected_and_previous_config_intact(tmp_path: Path) -> None:
    """Racing writers: A commits revision 2; B (last saw revision 1) loses."""
    root = tmp_path / "data-root"
    store = GrantConfigStore(root)
    store.write(_document(revision=1), expected_revision=0)
    writer_a = _document(revision=2, grants=[])
    writer_b = _document(
        revision=2,
        grants=[{"capability_id": CAP, "server_ids": [str(SERVER_B)]}],
    )
    store.write(writer_a, expected_revision=1)
    # B's write is based on a superseded observation of the configuration
    with pytest.raises(GrantConfigError) as excinfo:
        store.write(writer_b, expected_revision=1)
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.STALE_REVISION
    reread = store.read()
    assert reread.revision == 2
    assert reread.grants == {}


def test_document_revision_must_equal_expected_plus_one(tmp_path: Path) -> None:
    """The caller states the next revision explicitly; the store never invents it."""
    store = GrantConfigStore(tmp_path / "data-root")
    store.write(_document(revision=1), expected_revision=0)
    # replaying the current revision instead of naming the next one
    with pytest.raises(GrantConfigError) as excinfo:
        store.write(_document(revision=1), expected_revision=1)
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.REVISION_CONFLICT
    # naming a revision further ahead than exactly one step
    with pytest.raises(GrantConfigError) as excinfo:
        store.write(_document(revision=3), expected_revision=1)
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.REVISION_CONFLICT
    assert store.read().revision == 1


@pytest.mark.parametrize("bad_revision", [-1, True, "1", 1.5, None])
def test_invalid_expected_revision_rejected(
    tmp_path: Path, bad_revision: object
) -> None:
    store = GrantConfigStore(tmp_path / "data-root")
    with pytest.raises(GrantConfigError) as excinfo:
        store.write(_document(), expected_revision=bad_revision)  # type: ignore[arg-type]
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.INVALID_REVISION
    with pytest.raises(GrantConfigError):
        store.read()


def test_revision_state_survives_store_reopen(tmp_path: Path) -> None:
    """AC1 restart semantics: a fresh store instance is the only state carrier."""
    root = tmp_path / "data-root"
    first = GrantConfigStore(root)
    first.write(_document(revision=1), expected_revision=0)
    reopened = GrantConfigStore(root)
    reread = reopened.read()
    assert reread.revision == 1
    assert reread.grants == {CAP: frozenset({SERVER_A})}
    nxt = reopened.write(
        _document(revision=2, grants=[]), expected_revision=reread.revision
    )
    assert nxt.revision == 2
    assert GrantConfigStore(root).read().grants == {}


def test_canonical_bytes_are_content_determined(tmp_path: Path) -> None:
    """Input key/member order cannot diverge the canonical hash."""
    store = GrantConfigStore(tmp_path / "data-root")
    ordered = _document(
        revision=1,
        grants=[
            {"capability_id": CAP, "server_ids": [str(SERVER_A), str(SERVER_B)]}
        ],
    )
    shuffled = {
        "grants": [
            {"server_ids": [str(SERVER_B), str(SERVER_A)], "capability_id": CAP}
        ],
        "policy": {"denied_capabilities": [], "revision": 1},
    }
    first = store.write(ordered, expected_revision=0)
    canonical_path = store.root / canonical_config_relative_path()
    canonical_bytes = canonical_path.read_bytes()
    # logically identical document (different construction order) hashes equal
    assert hashlib.sha256(canonical_bytes).hexdigest() == first.sha256
    assert json.loads(canonical_bytes) == json.loads(json.dumps(ordered))
    # a second store at a different root stores byte-identical canonical text
    other = GrantConfigStore(tmp_path / "other-root")
    twin = other.write(shuffled, expected_revision=0)
    assert twin.sha256 == first.sha256
    assert (
        (other.root / canonical_config_relative_path()).read_bytes()
        == canonical_bytes
    )


def test_denied_capabilities_and_multi_grants_roundtrip(tmp_path: Path) -> None:
    store = GrantConfigStore(tmp_path / "data-root")
    document = _document(
        revision=1,
        grants=[
            {"capability_id": "zzz.last", "server_ids": [str(SERVER_B)]},
            {"capability_id": "aaa.first", "server_ids": [str(SERVER_A), str(SERVER_B)]},
        ],
        denied=["raw.shell"],
    )
    stored = store.write(document, expected_revision=0)
    assert stored.grants == {
        "aaa.first": frozenset({SERVER_A, SERVER_B}),
        "zzz.last": frozenset({SERVER_B}),
    }
    assert stored.policy.denied_capabilities == frozenset({"raw.shell"})
    view = store.read().to_json()
    assert view["policy"] == {"revision": 1, "denied_capabilities": ["raw.shell"]}
    assert [entry["capability_id"] for entry in view["grants"]] == [
        "aaa.first",
        "zzz.last",
    ]


def test_empty_grants_is_a_valid_total_revocation(tmp_path: Path) -> None:
    store = GrantConfigStore(tmp_path / "data-root")
    store.write(_document(revision=1), expected_revision=0)
    revoked = store.write(_document(revision=2, grants=[]), expected_revision=1)
    assert revoked.grants == {}
    reread = store.read()
    assert reread.revision == 2
    assert reread.grants == {}


def test_absent_revision_convention_is_zero(tmp_path: Path) -> None:
    assert ABSENT_REVISION == 0
    store = GrantConfigStore(tmp_path / "data-root")
    with pytest.raises(GrantConfigError) as excinfo:
        store.read()
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.NOT_FOUND


def test_stored_view_has_frozen_shape(tmp_path: Path) -> None:
    store = GrantConfigStore(tmp_path / "data-root")
    store.write(_document(revision=1, denied=["raw.shell"]), expected_revision=0)
    view = store.read().to_json()
    assert tuple(view) == (
        "revision",
        "schema_revision",
        "sha256",
        "policy",
        "grants",
    )
    assert tuple(view["policy"]) == ("revision", "denied_capabilities")  # type: ignore[arg-type]
    for entry in view["grants"]:  # type: ignore[union-attr]
        assert tuple(entry) == ("capability_id", "server_ids")


def test_metadata_carries_frozen_version_triple(tmp_path: Path) -> None:
    store = GrantConfigStore(tmp_path / "data-root")
    stored = store.write(_document(revision=1), expected_revision=0)
    meta = json.loads(
        (store.root / canonical_config_meta_relative_path()).read_bytes()
    )
    assert meta == {
        "format": 1,
        "revision": stored.revision,
        "schema_revision": CONFIG_SCHEMA_VERSION,
        "sha256": stored.sha256,
    }


def test_permissions_are_restrictive(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    store = GrantConfigStore(root)
    store.write(_document(revision=1), expected_revision=0)
    assert (root.stat().st_mode & 0o777) == 0o700
    assert ((root / POLICY_DIR).stat().st_mode & 0o777) == 0o700
    assert ((root / canonical_config_relative_path()).stat().st_mode & 0o777) == 0o600
    assert (
        (root / canonical_config_meta_relative_path()).stat().st_mode & 0o777
    ) == 0o600


# ---------------------------------------------------------------------------
# AC2 — reload sees revocation; the revision is the policy revision
# ---------------------------------------------------------------------------


def test_revocation_takes_effect_on_next_reload(tmp_path: Path) -> None:
    store = GrantConfigStore(tmp_path / "data-root")
    store.write(_document(revision=1), expected_revision=0)
    reloaded = GrantConfigStore(tmp_path / "data-root").read()
    allowed = _decide(reloaded)
    assert allowed.allowed is True
    assert allowed.policy_revision == 1

    # revoke while the operation waits: any process commits revision 2
    GrantConfigStore(tmp_path / "data-root").write(
        _document(revision=2, grants=[]), expected_revision=1
    )
    # the dispatcher's later recheck reloads fresh and denies
    rechecked = GrantConfigStore(tmp_path / "data-root").read()
    assert rechecked.policy.revision == 2
    denied = _decide(rechecked)
    assert denied.allowed is False
    assert denied.reason is DecisionReason.CAPABILITY_NOT_GRANTED
    assert denied.policy_revision == 2


def test_deny_list_write_denies_with_policy_reason(tmp_path: Path) -> None:
    store = GrantConfigStore(tmp_path / "data-root")
    store.write(_document(revision=1), expected_revision=0)
    assert _decide(store.read()).allowed is True
    store.write(
        _document(revision=2, denied=[CAP]),
        expected_revision=1,
    )
    denied = _decide(store.read())
    assert denied.allowed is False
    assert denied.reason is DecisionReason.CAPABILITY_DENIED_BY_POLICY
    assert denied.policy_revision == 2


def test_store_holds_no_cache_across_instances(tmp_path: Path) -> None:
    """Every read hits the filesystem: writes from one instance are
    immediately visible to any other instance (reload currency)."""
    root = tmp_path / "data-root"
    writer = GrantConfigStore(root)
    observer = GrantConfigStore(root)
    writer.write(_document(revision=1), expected_revision=0)
    assert observer.read().revision == 1
    observer.write(_document(revision=2, grants=[]), expected_revision=1)
    assert writer.read().revision == 2
    assert writer.read().grants == {}


def test_fixture_grant_full_flow_write_restart_evaluate_revoke_reload(
    tmp_path: Path,
) -> None:
    """Writer-owned half of the P03 gate scenario: write fixture grant through
    the production config service, restart, evaluate, revoke while the
    operation waits, prove the later recheck denies."""
    root = tmp_path / "data-root"
    # 1. admin writes the fixture grant through the production service
    GrantConfigStore(root).write(
        _document(
            revision=1,
            grants=[{"capability_id": CAP, "server_ids": [str(SERVER_A)]}],
        ),
        expected_revision=0,
    )
    # 2. restart: a fresh store instance reloads the durable configuration
    after_restart = GrantConfigStore(root).read()
    assert after_restart.revision == 1
    decision_before = _decide(after_restart)
    assert decision_before.allowed is True
    assert decision_before.policy_revision == 1
    # 3. revoke while the operation waits (second process/instance)
    GrantConfigStore(root).write(_document(revision=2, grants=[]), expected_revision=1)
    # 4. later dispatcher recheck: reload before the effect → deny
    recheck = GrantConfigStore(root).read()
    decision_after = _decide(recheck)
    assert decision_after.allowed is False
    assert decision_after.reason is DecisionReason.CAPABILITY_NOT_GRANTED
    assert decision_after.policy_revision == 2
    # the earlier allowed decision is bound to a superseded revision
    assert decision_before.policy_revision != recheck.policy.revision


# ---------------------------------------------------------------------------
# AC2 — generated prompts cannot write grants (C02 rule 6 / C07 rule 5)
# ---------------------------------------------------------------------------


def test_prompt_prose_document_rejected_before_any_effect(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    store = GrantConfigStore(root)
    prose = (
        "# Generated runbook\n\n"
        f"Successfully deployed. Notes: {PROSE_SENTINEL}. "
        "Next steps: restart the service and verify health."
    )
    with pytest.raises(GrantConfigError) as excinfo:
        store.write(prose, expected_revision=0)  # type: ignore[arg-type]
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.DOCUMENT_SHAPE_INVALID
    assert not (root / POLICY_DIR).exists()
    with pytest.raises(GrantConfigError) as excinfo:
        store.read()
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.NOT_FOUND


def test_document_with_extra_annotation_field_rejected(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    store = GrantConfigStore(root)
    annotated = _document(revision=1)
    annotated["generated_notes"] = f"operator summary {PROSE_SENTINEL}"
    with pytest.raises(GrantConfigError) as excinfo:
        store.write(annotated, expected_revision=0)
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.DOCUMENT_SHAPE_INVALID
    assert PROSE_SENTINEL not in excinfo.value.detail
    assert not (root / POLICY_DIR).exists()


@pytest.mark.parametrize(
    "document", [["grants"], "policy text", 5, None, {"policy": {"revision": 1}}]
)
def test_non_conforming_documents_rejected(
    tmp_path: Path, document: object
) -> None:
    root = tmp_path / "data-root"
    store = GrantConfigStore(root)
    with pytest.raises(GrantConfigError) as excinfo:
        store.write(document, expected_revision=0)
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.DOCUMENT_SHAPE_INVALID
    assert not (root / POLICY_DIR).exists()


def test_grant_entry_with_extra_prose_field_rejected(tmp_path: Path) -> None:
    store = GrantConfigStore(tmp_path / "data-root")
    document = _document(
        revision=1,
        grants=[
            {
                "capability_id": CAP,
                "server_ids": [str(SERVER_A)],
                "justification": "the agent said it needed this",
            }
        ],
    )
    with pytest.raises(GrantConfigError) as excinfo:
        store.write(document, expected_revision=0)
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.GRANTS_INVALID
    with pytest.raises(GrantConfigError):
        store.read()


def test_fixed_namespace_is_not_the_prompt_namespace() -> None:
    """The grants namespace is module-fixed; no caller path component exists."""
    assert canonical_config_relative_path() == f"{POLICY_DIR}/{CONFIG_FILENAME}"
    assert canonical_config_relative_path() == "policy/config.json"
    assert canonical_config_meta_relative_path() == "policy/config.meta.json"
    # a server prompt file (C02 rule 2 scheme) is a different namespace and
    # is never addressable through this module's paths
    prompt_relative = f"servers/{SERVER_A}/prompt.md"
    assert prompt_relative not in (
        canonical_config_relative_path(),
        canonical_config_meta_relative_path(),
    )


# ---------------------------------------------------------------------------
# Producer validation before any filesystem effect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy_section",
    [{"revision": 0, "denied_capabilities": []}, {"revision": True}, {"revision": "1"}],
)
def test_invalid_policy_section_rejected(
    tmp_path: Path, policy_section: object
) -> None:
    store = GrantConfigStore(tmp_path / "data-root")
    document = {"policy": policy_section, "grants": []}
    with pytest.raises(GrantConfigError) as excinfo:
        store.write(document, expected_revision=0)
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.POLICY_INVALID
    with pytest.raises(GrantConfigError):
        store.read()


@pytest.mark.parametrize(
    "grants",
    [
        [{"capability_id": "Bad Slug", "server_ids": [str(SERVER_A)]}],
        [{"capability_id": CAP, "server_ids": ["not-a-uuid"]}],
        [{"capability_id": CAP}],
        "grants text",
    ],
)
def test_invalid_grants_section_rejected(tmp_path: Path, grants: object) -> None:
    store = GrantConfigStore(tmp_path / "data-root")
    with pytest.raises(GrantConfigError) as excinfo:
        store.write({"policy": {"revision": 1}, "grants": grants}, expected_revision=0)
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.GRANTS_INVALID
    with pytest.raises(GrantConfigError):
        store.read()


def test_oversized_document_rejected_before_any_effect(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    store = GrantConfigStore(root)
    document = _document(
        revision=1,
        grants=[
            {
                "capability_id": CAP,
                "server_ids": [str(uuid.UUID(int=i)) for i in range(2000)],
            }
        ],
    )
    canonical = json.dumps(document, sort_keys=True)
    assert len(canonical.encode()) > MAX_DOCUMENT_BYTES
    with pytest.raises(GrantConfigError) as excinfo:
        store.write(document, expected_revision=0)
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.DOCUMENT_TOO_LARGE
    assert not (root / POLICY_DIR).exists()


def test_error_details_never_carry_document_prose(tmp_path: Path) -> None:
    store = GrantConfigStore(tmp_path / "data-root")
    annotated = _document(revision=1)
    annotated["notes"] = f"secret summary {PROSE_SENTINEL}"
    with pytest.raises(GrantConfigError) as excinfo:
        store.write(annotated, expected_revision=0)
    rendered = f"{excinfo.value} {excinfo.value.detail}"
    assert PROSE_SENTINEL not in rendered


# ---------------------------------------------------------------------------
# Confinement (C02 rule 3)
# ---------------------------------------------------------------------------


def _prepared_store(tmp_path: Path) -> GrantConfigStore:
    store = GrantConfigStore(tmp_path / "data-root")
    store.write(_document(revision=1), expected_revision=0)
    return store


def test_symlinked_policy_dir_rejected(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    real = store.root / POLICY_DIR
    real.rename(tmp_path / "real-policy")
    (real).symlink_to(tmp_path / "real-policy", target_is_directory=True)
    (outside / "config.json").write_text("escaped", encoding="utf-8")
    for operation in (
        lambda: store.read(),
        lambda: store.write(_document(revision=2), expected_revision=1),
    ):
        with pytest.raises(GrantConfigError) as excinfo:
            operation()
        assert _codes_of(excinfo.value) is GrantConfigErrorCode.PATH_ESCAPE


def test_symlinked_canonical_file_rejected(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    outside_target = tmp_path / "outside-config.json"
    outside_bytes = b"do not touch"
    outside_target.write_bytes(outside_bytes)
    canonical = store.root / canonical_config_relative_path()
    canonical.unlink()
    canonical.symlink_to(outside_target)
    with pytest.raises(GrantConfigError) as excinfo:
        store.read()
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.PATH_ESCAPE
    with pytest.raises(GrantConfigError) as excinfo:
        store.write(_document(revision=2), expected_revision=1)
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.PATH_ESCAPE
    assert outside_target.read_bytes() == outside_bytes


def test_symlinked_metadata_file_rejected(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    meta = store.root / canonical_config_meta_relative_path()
    meta.unlink()
    meta.symlink_to(tmp_path / "elsewhere.meta.json")
    for operation in (
        lambda: store.read(),
        lambda: store.write(_document(revision=2), expected_revision=1),
    ):
        with pytest.raises(GrantConfigError) as excinfo:
            operation()
        assert _codes_of(excinfo.value) is GrantConfigErrorCode.PATH_ESCAPE


def test_symlinked_data_root_config_is_resolved_and_confined(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    link_root = tmp_path / "link-root"
    link_root.symlink_to(real_root, target_is_directory=True)
    store = GrantConfigStore(link_root)
    assert store.root == real_root.resolve()
    store.write(_document(revision=1), expected_revision=0)
    assert store.read().revision == 1


# ---------------------------------------------------------------------------
# Integrity honestly reported, never invented (C02 rule 5)
# ---------------------------------------------------------------------------


def test_external_tamper_reported_as_hash_mismatch(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    canonical = store.root / canonical_config_relative_path()
    canonical.write_bytes(canonical.read_bytes() + b'{"tampered": true}')
    with pytest.raises(GrantConfigError) as excinfo:
        store.read()
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.HASH_MISMATCH
    rendered = f"{excinfo.value} {excinfo.value.detail}"
    assert CAP not in rendered


def test_missing_metadata_reported_not_reconstructed(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    (store.root / canonical_config_meta_relative_path()).unlink()
    with pytest.raises(GrantConfigError) as excinfo:
        store.read()
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.METADATA_MISSING


def test_invalid_metadata_shape_reported(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    meta_path = store.root / canonical_config_meta_relative_path()
    meta_path.write_bytes(b"{not json")
    with pytest.raises(GrantConfigError) as excinfo:
        store.read()
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.METADATA_INVALID
    meta_path.write_bytes(json.dumps({"format": 99, "revision": 1}).encode())
    with pytest.raises(GrantConfigError) as excinfo:
        store.read()
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.METADATA_INVALID


def test_orphan_metadata_blocks_write(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    (store.root / canonical_config_relative_path()).unlink()
    with pytest.raises(GrantConfigError) as excinfo:
        store.write(_document(revision=2), expected_revision=1)
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.ORPHAN_METADATA


def test_stored_schema_deviation_reported_not_repaired(tmp_path: Path) -> None:
    """Defense in depth: even hash-consistent bytes must satisfy the schema."""
    store = _prepared_store(tmp_path)
    canonical = store.root / canonical_config_relative_path()
    forged = b'{"policy": {"revision": 1, "denied_capabilities": []}}\n'
    canonical.write_bytes(forged)
    digest = hashlib.sha256(forged).hexdigest()
    meta_path = store.root / canonical_config_meta_relative_path()
    meta_path.write_bytes(
        json.dumps(
            {"format": 1, "revision": 1, "schema_revision": 1, "sha256": digest}
        ).encode()
    )
    with pytest.raises(GrantConfigError) as excinfo:
        store.read()
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.DOCUMENT_SHAPE_INVALID


# ---------------------------------------------------------------------------
# AC1 — crash before replace preserves previous valid configuration (W1 half)
# ---------------------------------------------------------------------------


def _policy_dir(store: GrantConfigStore) -> Path:
    return store.root / POLICY_DIR


def test_interrupted_before_canonical_replace_preserves_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _prepared_store(tmp_path)

    def _interrupted(src: object, dst: object) -> None:
        raise OSError("interrupted before replace")

    monkeypatch.setattr("mai.policy.config.os.replace", _interrupted)
    with pytest.raises(OSError):
        store.write(
            _document(revision=2, grants=[]), expected_revision=1
        )
    monkeypatch.undo()

    reread = store.read()
    assert (reread.revision, reread.grants) == (1, {CAP: frozenset({SERVER_A})})
    recovered = store.write(_document(revision=2, grants=[]), expected_revision=1)
    assert recovered.revision == 2
    assert not list(_policy_dir(store).glob("*.tmp"))


def test_interrupted_after_canonical_replace_is_detectable_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # canonical replaced, metadata not yet — read() reports the divergence
    # (canonical authoritative) instead of inventing a successful sync.
    store = _prepared_store(tmp_path)
    real_replace = __import__("os").replace
    calls = {"count": 0}

    def _half_committed(src: object, dst: object) -> None:
        calls["count"] += 1
        if calls["count"] > 1:
            raise OSError("interrupted between replaces")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr("mai.policy.config.os.replace", _half_committed)
    with pytest.raises(OSError):
        store.write(_document(revision=2, grants=[]), expected_revision=1)
    monkeypatch.undo()

    canonical = store.root / canonical_config_relative_path()
    assert b'"revision": 2' in canonical.read_bytes()  # canonical is authoritative
    with pytest.raises(GrantConfigError) as excinfo:
        store.read()
    assert _codes_of(excinfo.value) is GrantConfigErrorCode.HASH_MISMATCH


def test_real_sigkill_before_replace_preserves_previous_config(
    tmp_path: Path,
) -> None:
    """Real-boundary proof: SIGKILL at the replace boundary, then restart."""
    store = _prepared_store(tmp_path)

    child_script = """
import os, signal, sys
from mai.policy.config import GrantConfigStore
root = sys.argv[1]
document = sys.stdin.read()
def _die(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)
os.replace = _die
import json
GrantConfigStore(root).write(json.loads(document), expected_revision=1)
raise SystemExit("write returned; kill hook did not fire")
"""
    completed = subprocess.run(
        [sys.executable, "-c", child_script, str(store.root)],
        input=json.dumps(_document(revision=2, grants=[])),
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        timeout=60,
        check=False,
    )
    assert completed.returncode == -signal.SIGKILL, completed.stderr

    # Restart semantics: a fresh store observes the untouched previous revision.
    restarted = GrantConfigStore(store.root)
    reread = restarted.read()
    assert (reread.revision, reread.grants) == (1, {CAP: frozenset({SERVER_A})})
    # Recovery: the next successful write cleans the orphaned temp file.
    recovered = restarted.write(_document(revision=2, grants=[]), expected_revision=1)
    assert recovered.revision == 2
    assert not list(_policy_dir(restarted).glob("*.tmp"))


def test_stale_temp_files_cleaned_on_next_write(tmp_path: Path) -> None:
    store = GrantConfigStore(tmp_path / "data-root")
    store.write(_document(revision=1), expected_revision=0)
    policy_dir = _policy_dir(store)
    (policy_dir / f"{CONFIG_FILENAME}.crash.tmp").write_bytes(b"stale")
    (policy_dir / f"{CONFIG_META_FILENAME}.crash.tmp").write_bytes(b"stale")
    (policy_dir / "unrelated.tmp").write_bytes(b"keep")
    store.write(_document(revision=2), expected_revision=1)
    assert not list(policy_dir.glob(f"{CONFIG_FILENAME}.*.tmp"))
    assert not list(policy_dir.glob(f"{CONFIG_META_FILENAME}.*.tmp"))
    assert (policy_dir / "unrelated.tmp").exists()


# ---------------------------------------------------------------------------
# Production entrypoint (exit contract 0 / 1 / 2)
# ---------------------------------------------------------------------------


def test_cli_write_read_roundtrip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "data-root"
    document_file = _config_file(tmp_path, _document(revision=1))
    assert main(["write", str(root), str(document_file), "--expect-revision", "0"]) == 0
    written = json.loads(capsys.readouterr().out)
    assert written["revision"] == 1
    assert main(["read", str(root)]) == 0
    assert json.loads(capsys.readouterr().out) == written


def test_cli_read_absent_exits_one_not_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["read", str(tmp_path / "data-root")]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == GrantConfigErrorCode.NOT_FOUND.value


def test_cli_stale_write_exits_one_with_stable_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "data-root"
    document_file = _config_file(tmp_path, _document(revision=1))
    assert main(["write", str(root), str(document_file), "--expect-revision", "0"]) == 0
    capsys.readouterr()
    assert main(["write", str(root), str(document_file), "--expect-revision", "0"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == GrantConfigErrorCode.STALE_REVISION.value


def test_cli_revision_conflict_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "data-root"
    ahead = _config_file(tmp_path, _document(revision=5))
    assert main(["write", str(root), str(ahead), "--expect-revision", "0"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == GrantConfigErrorCode.REVISION_CONFLICT.value


def test_cli_prose_document_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Raw generated prompt/prose text is not JSON — rejected, nothing stored."""
    prose_file = tmp_path / "prompt.md"
    prose_file.write_text(f"# generated runbook\n{PROSE_SENTINEL}\n", encoding="utf-8")
    assert main(["write", str(tmp_path / "data-root"), str(prose_file), "--expect-revision", "0"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == GrantConfigErrorCode.DOCUMENT_NOT_JSON.value
    with pytest.raises(GrantConfigError):
        GrantConfigStore(tmp_path / "data-root").read()


def test_cli_malformed_json_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not json", encoding="utf-8")
    assert main(["write", str(tmp_path / "data-root"), str(bad_file), "--expect-revision", "0"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == GrantConfigErrorCode.DOCUMENT_NOT_JSON.value


def test_cli_deep_json_exits_one_document_too_deep(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deep_file = tmp_path / "deep.json"
    deep_file.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    assert main(["write", str(tmp_path / "data-root"), str(deep_file), "--expect-revision", "0"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == GrantConfigErrorCode.DOCUMENT_TOO_DEEP.value


def test_cli_missing_document_file_exits_two(tmp_path: Path) -> None:
    assert (
        main(
            [
                "write",
                str(tmp_path / "data-root"),
                str(tmp_path / "absent.json"),
                "--expect-revision",
                "0",
            ]
        )
        == 2
    )


def test_cli_no_args_exits_two(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["mai.policy.config"])
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2
