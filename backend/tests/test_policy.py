"""Focused tests for grant/mode policy evaluation.

Card/contracts: ``03-policy`` against contracts.md **C03 rules 2–5 and 7**
and auth ADR ``3e79449`` §8 G2 (enrollment-before-root). Covers the
policy half of the GRANT-INTERSECT scenario row
(``runtime-scenarios.md``): frozen good/bad contexts — parent
broad/child narrow, changed server, revoked capability — plus the brief
acceptance: deny precedence, modes cannot increase grants, explicit
server/adoption scope, decisions carrying a stable reason and the
current policy revision, and **no OS-isolation claim for raw root**
(the only containment string is the C03 rule 5 forfeiture advisory).

Fail-closed rule: every ambiguity denies — the deny matrix below pins
one exact stable reason per ambiguity, in the frozen precedence order,
under both ``ask`` and ``delegated`` modes.

Secret hygiene: execution contexts carry credential reference *names*
only (C03 rule 2); assertions pin that decision output embeds neither
context, principal nor reference names. Identifiers/UUIDs here are
inert declarative values.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.evaluate import (
    PRIVILEGED_FORFEITS_OS_CONTAINMENT,
    DecisionReason,
    ExecutionContext,
    ParentScope,
    Policy,
    PolicyDecision,
    PolicyError,
    PolicyErrorCode,
    PolicyMode,
    effective_child_grants,
    evaluate,
    execution_context_from_json,
    grants_from_json,
    policy_from_json,
)

SERVER_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
SERVER_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
SERVER_C = uuid.UUID("00000000-0000-0000-0000-00000000000c")
RUN_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
RUN_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")

#: Frozen decision-document key order (AC2: stable, context-free).
DECISION_KEYS = (
    "allowed",
    "reason",
    "policy_revision",
    "capability_id",
    "server_id",
    "effect_class",
    "mode",
    "requires_approval",
    "advisories",
)

CONTEXT_KEYS = (
    "principal",
    "server_id",
    "run_id",
    "parent_scope",
    "profile_revision",
    "prompt_revision",
    "credential_references",
    "allowed_capabilities",
    "mode",
)

CLI = [sys.executable, "-m", "mai.policy.evaluate"]


def _context(**overrides: object) -> ExecutionContext:
    """One valid delegated context on SERVER_A, overridden per test."""
    values: dict[str, object] = {
        "principal": "admin-session-1",
        "server_id": SERVER_A,
        "run_id": RUN_1,
        "parent_scope": None,
        "profile_revision": "profile-rev-1",
        "prompt_revision": "prompt-rev-1",
        "credential_references": ("ENV:DEPLOY_TOKEN",),
        "allowed_capabilities": ("deploy.app",),
        "mode": PolicyMode.DELEGATED,
    }
    values.update(overrides)
    return ExecutionContext(**values)  # type: ignore[arg-type]


def _policy(**overrides: object) -> Policy:
    values: dict[str, object] = {"revision": 7, "denied_capabilities": frozenset()}
    values.update(overrides)
    return Policy(**values)  # type: ignore[arg-type]


def _grants(**overrides: object) -> dict[str, frozenset[uuid.UUID]]:
    values: dict[str, object] = {"deploy.app": frozenset({SERVER_A})}
    values.update(overrides)
    return values  # type: ignore[return-value]


def _descriptor(**overrides: object):
    """A registry-validated descriptor document for ``deploy.app``."""
    document: dict[str, object] = {
        "identifier": "deploy.app",
        "version": "1.0.0",
        "config_schema": None,
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "execution_environments": ["linux_ssh"],
        "effect_class": "mutate",
        "resource_scope": ["target.fs"],
        "required_grants": ["deploy.app"],
    }
    document.update(overrides)
    return parse_descriptor(document)


def _decide(**overrides: object) -> PolicyDecision:
    """One well-formed allowed-by-default evaluation, overridden per test."""
    values: dict[str, object] = {
        "context": _context(),
        "capability_id": "deploy.app",
        "descriptor": _descriptor(),
        "grants": _grants(),
        "policy": _policy(),
        "adopted": True,
    }
    values.update(overrides)
    return evaluate(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ExecutionContext: C03 rule 2 frozen vocabulary, construction-time checks
# ---------------------------------------------------------------------------


def test_context_roundtrip_exact_frozen_vocabulary() -> None:
    context = _context(
        parent_scope=ParentScope(run_id=RUN_2, server_id=SERVER_B),
        mode=PolicyMode.ASK,
    )
    document = context.to_json()
    assert tuple(document) == CONTEXT_KEYS
    assert document["principal"] == "admin-session-1"
    assert document["server_id"] == str(SERVER_A)
    assert document["parent_scope"] == {
        "run_id": str(RUN_2),
        "server_id": str(SERVER_B),
    }
    assert document["mode"] == "ask"
    assert execution_context_from_json(document) == context


def test_context_rejects_non_uuid_identity() -> None:
    with pytest.raises(PolicyError) as raised:
        _context(server_id=str(SERVER_A))
    assert raised.value.code is PolicyErrorCode.INVALID_CONTEXT
    with pytest.raises(PolicyError) as raised:
        _context(run_id="not-a-uuid")
    assert raised.value.code is PolicyErrorCode.INVALID_CONTEXT
    with pytest.raises(PolicyError) as raised:
        _context(parent_scope="parent-run")
    assert raised.value.code is PolicyErrorCode.INVALID_CONTEXT
    with pytest.raises(PolicyError) as raised:
        ParentScope(run_id="x", server_id=SERVER_B)
    assert raised.value.code is PolicyErrorCode.INVALID_CONTEXT


def test_context_rejects_empty_principal_and_revisions() -> None:
    for field in ("principal", "profile_revision", "prompt_revision"):
        with pytest.raises(PolicyError) as raised:
            _context(**{field: ""})
        assert raised.value.code is PolicyErrorCode.INVALID_CONTEXT
        with pytest.raises(PolicyError):
            _context(**{field: 7})


def test_context_rejects_invalid_capability_slug_in_allowed() -> None:
    for bad in ("Deploy.app", "", "deploy app", "deploy/app", 7, None):
        with pytest.raises(PolicyError) as raised:
            _context(allowed_capabilities=(bad,))
        assert raised.value.code is PolicyErrorCode.INVALID_CONTEXT


def test_context_rejects_bad_or_duplicate_credential_references() -> None:
    for references in (("",), (7,), ("ENV:A", "ENV:A")):
        with pytest.raises(PolicyError) as raised:
            _context(credential_references=references)
        assert raised.value.code is PolicyErrorCode.INVALID_CONTEXT


def test_context_rejects_duplicate_or_unknown_capabilities() -> None:
    with pytest.raises(PolicyError) as raised:
        _context(allowed_capabilities=("deploy.app", "deploy.app"))
    assert raised.value.code is PolicyErrorCode.INVALID_CONTEXT
    with pytest.raises(PolicyError) as raised:
        _context(mode="delegated")  # raw string: explicit PolicyMode required
    assert raised.value.code is PolicyErrorCode.INVALID_CONTEXT


def test_context_repr_carries_ids_only() -> None:
    text = repr(_context())
    assert "ENV:DEPLOY_TOKEN" not in text
    assert str(RUN_1) in text
    assert "admin-session-1" in text


def test_context_from_json_accepts_uuid_strings_and_null_parent() -> None:
    document = _context().to_json()
    context = execution_context_from_json(document)
    assert context.server_id == SERVER_A
    assert context.parent_scope is None
    assert context.mode is PolicyMode.DELEGATED


def test_context_from_json_rejects_structural_deviations() -> None:
    base = _context().to_json()
    deviations: list[dict[str, object]] = [
        {**base, "extra": 1},
        {k: v for k, v in base.items() if k != "principal"},
        {**base, "server_id": "zzz"},
        {**base, "parent_scope": {"run_id": str(RUN_2)}},
        {**base, "parent_scope": {"run_id": "x", "server_id": "y"}},
        {**base, "mode": "auto"},
        {**base, "credential_references": "ENV:A"},
        {**base, "allowed_capabilities": "deploy.app"},
        "not-a-mapping",
    ]
    for document in deviations:
        with pytest.raises(PolicyError) as raised:
            execution_context_from_json(document)
        assert raised.value.code is PolicyErrorCode.INVALID_CONTEXT


# ---------------------------------------------------------------------------
# Policy: revision + deny list (C03 rules 3–5)
# ---------------------------------------------------------------------------


def test_policy_revision_strictly_positive_int_not_bool() -> None:
    assert _policy(revision=1).revision == 1
    for bad in (0, -3, "7", 7.5, True):
        with pytest.raises(PolicyError) as raised:
            _policy(revision=bad)
        assert raised.value.code is PolicyErrorCode.INVALID_POLICY


def test_policy_deny_list_entries_are_capability_slugs() -> None:
    assert _policy(denied_capabilities=frozenset({"shell.raw"})).denied_capabilities
    with pytest.raises(PolicyError) as raised:
        _policy(denied_capabilities=frozenset({"Shell.Raw"}))
    assert raised.value.code is PolicyErrorCode.INVALID_POLICY


def test_policy_from_json_roundtrip_and_deviations() -> None:
    policy = policy_from_json({"revision": 3, "denied_capabilities": ["shell.raw"]})
    assert policy.revision == 3
    assert policy.denied_capabilities == frozenset({"shell.raw"})
    assert policy_from_json({"revision": 3}).denied_capabilities == frozenset()
    for document in (
        {"denied_capabilities": []},
        {"revision": 3, "extra": True},
        {"revision": 3, "denied_capabilities": "shell.raw"},
        {"revision": "3"},
        "nope",
    ):
        with pytest.raises(PolicyError) as raised:
            policy_from_json(document)
        assert raised.value.code is PolicyErrorCode.INVALID_POLICY


# ---------------------------------------------------------------------------
# Grants: validation + child intersection (C03 rule 3, GRANT-INTERSECT)
# ---------------------------------------------------------------------------


def test_effective_child_grants_intersect_and_narrow() -> None:
    parent = {"deploy.app": frozenset({SERVER_A, SERVER_B})}
    child = {"deploy.app": frozenset({SERVER_B})}
    assert effective_child_grants(parent, child) == {
        "deploy.app": frozenset({SERVER_B})
    }


def test_intersection_is_never_a_union() -> None:
    parent = {"deploy.app": frozenset({SERVER_A})}
    child = {"deploy.app": frozenset({SERVER_B})}
    assert effective_child_grants(parent, child) == {}


def test_child_only_and_parent_only_capabilities_dropped() -> None:
    parent = {"deploy.app": frozenset({SERVER_A}), "shell.raw": frozenset({SERVER_A})}
    child = {"deploy.app": frozenset({SERVER_A}), "docs.search": frozenset({SERVER_A})}
    assert effective_child_grants(parent, child) == {
        "deploy.app": frozenset({SERVER_A})
    }


def test_empty_intersection_drops_grant_instead_of_narrowing_to_nothing() -> None:
    parent = {"deploy.app": frozenset({SERVER_A})}
    child = {"deploy.app": frozenset[uuid.UUID]()}
    assert effective_child_grants(parent, child) == {}


def test_effective_grants_keys_sorted_deterministic() -> None:
    parent = {"b.cap": frozenset({SERVER_A}), "a.cap": frozenset({SERVER_A})}
    child = {"a.cap": frozenset({SERVER_A}), "b.cap": frozenset({SERVER_A})}
    assert list(effective_child_grants(parent, child)) == ["a.cap", "b.cap"]


def test_grant_sets_structurally_validated() -> None:
    for bad in (
        {"Deploy.app": frozenset({SERVER_A})},
        {"deploy.app": {SERVER_A, "zz"}},
        {"deploy.app": SERVER_A},  # scope itself is not a collection
        {"deploy.app": "server-a"},  # a string is not a server collection
        [("deploy.app", frozenset({SERVER_A}))],  # not a mapping
    ):
        with pytest.raises(PolicyError) as raised:
            effective_child_grants(bad, {})
        assert raised.value.code is PolicyErrorCode.INVALID_GRANTS
        with pytest.raises(PolicyError) as raised:
            effective_child_grants({}, bad)
        assert raised.value.code is PolicyErrorCode.INVALID_GRANTS


def test_grants_from_json_roundtrip_and_deviations() -> None:
    document = [
        {"capability_id": "deploy.app", "server_ids": [str(SERVER_A), str(SERVER_B)]}
    ]
    grants = grants_from_json(document)
    assert grants == {"deploy.app": frozenset({SERVER_A, SERVER_B})}
    for bad in (
        [{"capability_id": "deploy.app"}],
        [{"capability_id": "deploy.app", "server_ids": ["zz"]}],
        [{"capability_id": "Deploy.app", "server_ids": []}],
        [{"capability_id": "deploy.app", "server_ids": [], "extra": 1}],
        [
            {"capability_id": "deploy.app", "server_ids": []},
            {"capability_id": "deploy.app", "server_ids": []},
        ],
        {"deploy.app": []},
    ):
        with pytest.raises(PolicyError) as raised:
            grants_from_json(bad)
        assert raised.value.code is PolicyErrorCode.INVALID_GRANTS


# ---------------------------------------------------------------------------
# evaluate: allowed paths, prompting verdict, decision shape (C03 rule 5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(PolicyMode))
def test_allowed_read_never_requires_approval(mode: PolicyMode) -> None:
    decision = _decide(
        descriptor=_descriptor(effect_class="read", resource_scope=[]),
        context=_context(mode=mode),
    )
    assert decision.allowed is True
    assert decision.reason is DecisionReason.ALLOWED
    assert decision.requires_approval is False
    assert decision.advisories == ()
    assert decision.effect_class == "read"


def test_allowed_mutate_requires_approval_only_in_ask_mode() -> None:
    delegated = _decide(context=_context(mode=PolicyMode.DELEGATED))
    assert delegated.allowed is True
    assert delegated.requires_approval is False
    ask = _decide(context=_context(mode=PolicyMode.ASK))
    assert ask.allowed is True
    assert ask.requires_approval is True
    assert ask.advisories == ()


@pytest.mark.parametrize("mode", list(PolicyMode))
def test_allowed_privileged_always_requires_approval_with_forfeiture_advisory(
    mode: PolicyMode,
) -> None:
    decision = _decide(
        descriptor=_descriptor(
            effect_class="privileged", resource_scope=["target.fs"]
        ),
        context=_context(mode=mode),
    )
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.advisories == (PRIVILEGED_FORFEITS_OS_CONTAINMENT,)


def test_decision_json_frozen_shape_and_current_revision() -> None:
    decision = _decide(policy=_policy(revision=41))
    document = decision.to_json()
    assert tuple(document) == DECISION_KEYS
    assert document["policy_revision"] == 41
    assert document["server_id"] == str(SERVER_A)
    assert document["mode"] == "delegated"
    assert document["effect_class"] == "mutate"
    # a different current policy revision is reflected verbatim (AC2)
    assert _decide(policy=_policy(revision=42)).to_json()["policy_revision"] == 42


def test_decision_never_embeds_the_context() -> None:
    context = _context(
        principal="secret-principal-name",
        credential_references=("ENV:TOKEN_A", "ENV:TOKEN_B"),
    )
    document = _decide(context=context).to_json()
    text = json.dumps(document)
    assert "principal" not in document
    assert "run_id" not in document
    assert "credential" not in text
    assert "secret-principal-name" not in text
    assert "ENV:TOKEN_A" not in text


# ---------------------------------------------------------------------------
# evaluate: fail-closed deny matrix (frozen precedence), under both modes
# ---------------------------------------------------------------------------

DENY_CASES: list[tuple[str, str, Callable[[PolicyMode], dict[str, object]]]] = [
    (
        "adoption not exactly true",
        "server_not_adopted",
        lambda _mode: {"adopted": False},
    ),
    (
        "policy deny list wins over grant",
        "capability_denied_by_policy",
        lambda _mode: {
            "policy": _policy(denied_capabilities=frozenset({"deploy.app"}))
        },
    ),
    (
        "descriptor absent (revoked/unknown)",
        "capability_not_registered",
        lambda _mode: {"descriptor": None},
    ),
    (
        "descriptor for a different identifier",
        "capability_not_registered",
        lambda _mode: {"descriptor": _descriptor(identifier="other.cap")},
    ),
    (
        "capability absent from context allowed list",
        "capability_not_granted",
        lambda mode: {"context": _context(mode=mode, allowed_capabilities=())},
    ),
    (
        "capability absent from grant set",
        "capability_not_granted",
        lambda _mode: {"grants": {}},
    ),
    (
        "grant scope excludes the context server",
        "server_scope_mismatch",
        lambda _mode: {"grants": {"deploy.app": frozenset({SERVER_B})}},
    ),
    (
        "grant scope empty (nowhere)",
        "server_scope_mismatch",
        lambda _mode: {"grants": {"deploy.app": frozenset[uuid.UUID]()}},
    ),
    (
        "requested id not a capability slug",
        "invalid_capability_identifier",
        lambda _mode: {"capability_id": "Deploy.app"},
    ),
    (
        "requested id not a string",
        "invalid_capability_identifier",
        lambda _mode: {"capability_id": 7},
    ),
    (
        "grant set structurally invalid",
        "invalid_grants",
        lambda _mode: {"grants": {"deploy.app": frozenset({SERVER_A, "zz"})}},
    ),
]


@pytest.mark.parametrize("mode", list(PolicyMode))
@pytest.mark.parametrize(
    "label,expected_reason,override", DENY_CASES, ids=[c[0] for c in DENY_CASES]
)
def test_every_ambiguity_denies_with_stable_reason(
    label: str,
    expected_reason: str,
    override: Callable[[PolicyMode], dict[str, object]],
    mode: PolicyMode,
) -> None:
    """Fail-closed matrix: same deny under ask and delegated (AC1)."""
    values: dict[str, object] = {
        "context": _context(mode=mode),
        "capability_id": "deploy.app",
        "descriptor": _descriptor(),
        "grants": _grants(),
        "policy": _policy(),
        "adopted": True,
    }
    values.update(override(mode))
    decision = evaluate(**values)  # type: ignore[arg-type]
    assert decision.allowed is False
    assert decision.reason.value == expected_reason
    assert decision.requires_approval is False
    assert decision.advisories == ()
    assert decision.policy_revision == values["policy"].revision  # type: ignore[attr-defined]
    # a mode can never turn a deny into an allow
    other_mode = (
        PolicyMode.ASK if mode is PolicyMode.DELEGATED else PolicyMode.DELEGATED
    )
    values["context"] = _context(mode=other_mode)
    values.update(override(other_mode))
    flipped = evaluate(**values)  # type: ignore[arg-type]
    assert flipped.allowed is False
    assert flipped.reason is decision.reason


def test_invalid_identifier_deny_reports_raw_value_free_detail() -> None:
    decision = _decide(capability_id="Deploy.app")
    assert decision.capability_id == "Deploy.app"  # inert slug attempt echoed
    decision = _decide(capability_id=7)
    assert decision.capability_id == ""  # non-string cannot echo


def test_deny_precedence_is_frozen_first_match_wins() -> None:
    # adoption (G2 step 1) beats policy deny
    decision = _decide(
        adopted=False,
        policy=_policy(denied_capabilities=frozenset({"deploy.app"})),
    )
    assert decision.reason is DecisionReason.SERVER_NOT_ADOPTED
    # policy deny beats revoked descriptor and missing grant
    decision = _decide(
        policy=_policy(denied_capabilities=frozenset({"deploy.app"})),
        descriptor=None,
        grants={},
    )
    assert decision.reason is DecisionReason.CAPABILITY_DENIED_BY_POLICY
    # registration beats grant presence
    decision = _decide(descriptor=None)
    assert decision.reason is DecisionReason.CAPABILITY_NOT_REGISTERED
    # the context allowed-list check runs before the server-scope check
    decision = _decide(
        context=_context(allowed_capabilities=()),
        grants={"deploy.app": frozenset({SERVER_B})},
    )
    assert decision.reason is DecisionReason.CAPABILITY_NOT_GRANTED


def test_deny_reports_effect_class_only_when_descriptor_exists() -> None:
    denied = _decide(policy=_policy(denied_capabilities=frozenset({"deploy.app"})))
    assert denied.effect_class == "mutate"
    revoked = _decide(descriptor=None)
    assert revoked.effect_class is None


def test_adoption_must_be_exactly_true() -> None:
    for not_true in (False, 1, "true", None):
        decision = _decide(adopted=not_true)  # type: ignore[arg-type]
        assert decision.allowed is False
        assert decision.reason is DecisionReason.SERVER_NOT_ADOPTED


# ---------------------------------------------------------------------------
# GRANT-INTERSECT scenario (policy half): frozen good/bad contexts
# ---------------------------------------------------------------------------


def test_parent_broad_child_narrow_full_flow() -> None:
    parent_grants = {
        "deploy.app": frozenset({SERVER_A, SERVER_B}),
        "shell.raw": frozenset({SERVER_A, SERVER_B}),
    }
    child_grants = {
        "deploy.app": frozenset({SERVER_B}),
        "docs.search": frozenset({SERVER_B}),
    }
    effective = effective_child_grants(parent_grants, child_grants)
    assert effective == {"deploy.app": frozenset({SERVER_B})}

    child_context = _context(
        run_id=RUN_2,
        server_id=SERVER_B,
        parent_scope=ParentScope(run_id=RUN_1, server_id=SERVER_A),
        allowed_capabilities=("deploy.app", "docs.search", "shell.raw"),
    )
    # good context: child applies deploy.app on the shared server
    allowed = evaluate(
        context=child_context,
        capability_id="deploy.app",
        descriptor=_descriptor(),
        grants=effective,
        policy=_policy(),
        adopted=True,
    )
    assert allowed.allowed is True
    assert allowed.server_id == SERVER_B
    # bad context: the child claims the parent-only server A
    denied = evaluate(
        context=_context(
            run_id=RUN_2,
            server_id=SERVER_A,
            parent_scope=ParentScope(run_id=RUN_1, server_id=SERVER_A),
        ),
        capability_id="deploy.app",
        descriptor=_descriptor(),
        grants=effective,
        policy=_policy(),
        adopted=True,
    )
    assert denied.allowed is False
    assert denied.reason is DecisionReason.SERVER_SCOPE_MISMATCH
    # a parent-only capability is not resurrected by the child context
    dropped = evaluate(
        context=child_context,
        capability_id="shell.raw",
        descriptor=_descriptor(
            identifier="shell.raw",
            effect_class="privileged",
            required_grants=["shell.raw"],
        ),
        grants=effective,
        policy=_policy(),
        adopted=True,
    )
    assert dropped.reason is DecisionReason.CAPABILITY_NOT_GRANTED


def test_changed_server_context_is_denied() -> None:
    """Same grants, context re-pointed at another server: deny."""
    for moved_server in (SERVER_B, SERVER_C):
        decision = _decide(context=_context(server_id=moved_server))
        assert decision.allowed is False
        assert decision.reason is DecisionReason.SERVER_SCOPE_MISMATCH
        assert decision.server_id == moved_server


def test_revoked_capability_denied_despite_stale_grant() -> None:
    """Registry revocation (descriptor unresolvable) defeats a live grant."""
    registry = CapabilityRegistry()
    descriptor = registry.register(_descriptor())
    live = _decide(descriptor=registry.get(descriptor.identifier, descriptor.version))
    assert live.allowed is True
    # revoked: the requested version no longer resolves from the registry
    # (never registered / withdrawn) while the grant record survives stale
    assert registry.get("deploy.app", "9.9.9") is None
    revoked = _decide(descriptor=registry.get("deploy.app", "9.9.9"))
    assert revoked.allowed is False
    assert revoked.reason is DecisionReason.CAPABILITY_NOT_REGISTERED


# ---------------------------------------------------------------------------
# C03 rule 5 honesty: a decision never claims OS isolation for raw root
# ---------------------------------------------------------------------------


def test_privileged_decision_carries_no_isolation_claim() -> None:
    decision = _decide(
        descriptor=_descriptor(
            effect_class="privileged", resource_scope=["target.fs"]
        ),
    )
    document = decision.to_json()
    for key in document:
        assert "isolat" not in key and "sandbox" not in key
    assert "isolat" not in json.dumps(document)
    assert "sandbox" not in json.dumps(document)
    # the one containment string is the forfeiture advisory (a loss note)
    assert document["advisories"] == [PRIVILEGED_FORFEITS_OS_CONTAINMENT]


def test_privileged_deny_makes_no_claim_either() -> None:
    decision = _decide(
        descriptor=_descriptor(
            effect_class="privileged", resource_scope=["target.fs"]
        ),
        adopted=False,
    )
    document = decision.to_json()
    assert decision.allowed is False
    assert document["advisories"] == []
    assert "isolat" not in json.dumps(document)
    assert "containment" not in json.dumps(document)


# ---------------------------------------------------------------------------
# Production CLI: one request file -> one decision line (rc 0/1/2)
# ---------------------------------------------------------------------------


def _request_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "policy": {"revision": 7},
        "context": _context().to_json(),
        "grants": [{"capability_id": "deploy.app", "server_ids": [str(SERVER_A)]}],
        "descriptor": _descriptor().to_json(),
        "capability_id": "deploy.app",
        "adopted": True,
    }
    document.update(overrides)
    return document


def _run_cli(document: object, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "request.json"
    if isinstance(document, str):
        path.write_text(document, encoding="utf-8")
    else:
        path.write_text(json.dumps(document), encoding="utf-8")
    return subprocess.run(
        [*CLI, str(path)], capture_output=True, text=True, check=False
    )


def test_cli_allowed_request_exits_zero(tmp_path: Path) -> None:
    done = _run_cli(_request_document(), tmp_path)
    assert done.returncode == 0, done.stderr
    decision = json.loads(done.stdout)
    assert decision["allowed"] is True
    assert decision["reason"] == "allowed"
    assert decision["policy_revision"] == 7
    assert decision["server_id"] == str(SERVER_A)


def test_cli_denied_scenarios_exit_one(tmp_path: Path) -> None:
    bad_server = _request_document(
        context=_context(server_id=SERVER_B).to_json(),
    )
    done = _run_cli(bad_server, tmp_path)
    assert done.returncode == 1
    assert json.loads(done.stdout)["reason"] == "server_scope_mismatch"

    revoked = _request_document(descriptor=None)
    done = _run_cli(revoked, tmp_path)
    assert done.returncode == 1
    assert json.loads(done.stdout)["reason"] == "capability_not_registered"

    denied = _request_document(policy={"revision": 7, "denied_capabilities": ["deploy.app"]})
    done = _run_cli(denied, tmp_path)
    assert done.returncode == 1
    assert json.loads(done.stdout)["reason"] == "capability_denied_by_policy"


def test_cli_malformed_requests_fail_closed_exit_one(tmp_path: Path) -> None:
    malformed: list[object] = [
        "{not json",
        {},
        {"policy": {"revision": 7}},
        _request_document(extra_field=1),
        _request_document(context={**_context().to_json(), "mode": "auto"}),
        _request_document(descriptor={"identifier": "deploy.app"}),
        _request_document(adopted="yes"),
        _request_document(grants=[{"capability_id": "deploy.app", "server_ids": ["zz"]}]),
    ]
    for document in malformed:
        done = _run_cli(document, tmp_path)
        assert done.returncode == 1, document
        assert json.loads(done.stdout)["reason"] == "invalid_request"
    (tmp_path / "request.json").unlink(missing_ok=True)
    done = subprocess.run(
        [*CLI, str(tmp_path / "request.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1
    assert json.loads(done.stdout)["reason"] == "invalid_request"


def test_cli_usage_error_exits_two() -> None:
    done = subprocess.run(CLI, capture_output=True, text=True, check=False)
    assert done.returncode == 2
