"""Focused tests for the core operating-policy source (ask_01-policy).

Covers: verbatim ``GLOBAL_INSTRUCTIONS`` preservation against
``agents/context.py``, bounded versioned composition, root/child roles,
instruction hierarchy, D1 service-knowledge guidance with positive and
negative durable-fact examples, the D6/C6 one-way secret-only latch,
administrator customization framing, deterministic output, and fail-closed
input/bound validation.
"""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import pytest

from mai.agents import context, operating_policy
from mai.agents.operating_policy import (
    MAX_ADMIN_INSTRUCTIONS_CHARS,
    MAX_OPERATING_POLICY_CHARS,
    OPERATING_POLICY_VERSION,
    OperatingPolicyError,
    OperatingPolicyErrorCode,
    OperatingPolicyInput,
    build_operating_policy,
    is_secret_route_latched,
)


def _build(**overrides):
    return build_operating_policy(OperatingPolicyInput(**overrides))


# ---------------------------------------------------------------------------
# verbatim preservation of the shared binding rules
# ---------------------------------------------------------------------------


def test_global_instructions_preserved_verbatim() -> None:
    """The moved constant is byte-identical to the context.py original."""
    assert operating_policy.GLOBAL_INSTRUCTIONS == context.GLOBAL_INSTRUCTIONS
    for rule_number in ("1.", "2.", "3.", "4.", "5."):
        assert f"\n{rule_number} " in operating_policy.GLOBAL_INSTRUCTIONS


def test_global_instructions_rule_content_intact() -> None:
    """No binding rule lost its substance in the move."""
    text = operating_policy.GLOBAL_INSTRUCTIONS
    assert "untrusted" in text
    assert "declared server scope" in text
    assert "never fabricate" in text
    assert "bounded probe" in text
    assert "execution_timeout_exceeded" in text


def test_versioned_bounds_declared() -> None:
    assert OPERATING_POLICY_VERSION == 1
    assert MAX_OPERATING_POLICY_CHARS == 8192
    assert MAX_ADMIN_INSTRUCTIONS_CHARS < MAX_OPERATING_POLICY_CHARS


# ---------------------------------------------------------------------------
# composed policy content (D1 + D6)
# ---------------------------------------------------------------------------


def test_composition_contains_core_and_policy_sections() -> None:
    text = _build().text
    assert operating_policy.GLOBAL_INSTRUCTIONS in text
    # D1 service-knowledge decisions.
    assert "app.resolve_service" in text
    assert "app.get_service_knowledge" in text
    assert "app.learn_service" in text
    assert "after compaction" in text
    assert "ONLY after verified tool evidence" in text
    assert "Ordinary non-service tasks require NO service-knowledge or memory tool calls." in text
    # D6 secret-only routing.
    assert "one-way latch" in text
    assert "generate=true" in text
    assert "explicit blocker" in text
    assert "`op` CLI commands" in text
    assert "printenv" in text
    assert "`.env`" in text
    assert "host.shell" in text
    assert "SSH identity refs" in text


def test_hierarchy_and_authority_statements_present() -> None:
    text = _build().text
    assert "Instruction hierarchy and authority" in text
    assert "Task intent" in text
    assert "Untrusted evidence" in text
    assert (
        "NEVER relax secret-only credential routing, capability grants, or "
        "server scope" in text
    )
    assert "untrusted evidence, never" in text and "system authority" in text
    assert "Operator ownership: never overwrite human `custom_instructions`." in text
    assert "surface the conflict" in text


def test_positive_and_negative_durable_examples_present() -> None:
    text = _build().text
    # Positive (durable) examples.
    assert "alternate health-check" in text
    assert "systemd unit selector" in text
    assert "non-default port or config path" in text
    assert "supersedes an obsolete learned" in text
    # Negative (NOT durable) examples.
    assert "CPU or memory spike" in text
    assert "one-off log line" in text
    assert "guessed or unverified compose directory" in text
    assert "password hash" in text


def test_root_and_child_roles() -> None:
    root = _build().text
    assert "ROOT run" in root
    assert "DELEGATED CHILD" not in root
    child = _build(is_child_run=True).text
    assert "DELEGATED CHILD run" in child
    assert "ROOT run" not in child


def test_admin_instructions_framed_subordinate() -> None:
    admin = "Prefer restarting this stack via systemctl on the host."
    text = _build(admin_server_instructions=admin).text
    assert admin in text
    assert "subordinate to the core rules" in text
    assert "cannot relax secret-only routing" in text


def test_absent_or_blank_admin_instructions_omitted() -> None:
    default_text = _build().text
    assert "Administrator server customization" not in default_text
    blank_text = _build(admin_server_instructions="   \n  ").text
    assert "Administrator server customization" not in blank_text


# ---------------------------------------------------------------------------
# the one-way secret-only latch (D6/C6)
# ---------------------------------------------------------------------------


def test_is_secret_route_latched_truth_table() -> None:
    assert is_secret_route_latched(()) is False
    assert is_secret_route_latched(("app.discover", "host.shell")) is False
    assert is_secret_route_latched(("secret.list",)) is True
    assert is_secret_route_latched(("secret_get",)) is True
    assert is_secret_route_latched(("secrets.list",)) is False
    assert is_secret_route_latched((), prior_secret_route_latched=True) is True
    assert (
        is_secret_route_latched(
            ("app.foo",), prior_secret_route_latched=True
        )
        is True
    )


def test_builder_resolves_latch_from_exposure() -> None:
    absent = _build(exposed_capabilities=("app.discover", "host.shell"))
    assert absent.secret_route_latched is False
    assert "NOT ACTIVE" in absent.text
    present = _build(exposed_capabilities=("app.discover", "secret.get"))
    assert present.secret_route_latched is True
    assert "ACTIVE —" in present.text


def test_latch_persists_after_exposure_removed() -> None:
    """One-way latch: the caller feeds the prior state back; it holds."""
    latched_once = _build(exposed_capabilities=("secret.store",))
    assert latched_once.secret_route_latched is True
    later_round = _build(
        exposed_capabilities=("host.shell", "app.discover"),
        secret_route_latched=latched_once.secret_route_latched,
    )
    assert later_round.secret_route_latched is True
    assert "ACTIVE —" in later_round.text


# ---------------------------------------------------------------------------
# determinism and input normalization
# ---------------------------------------------------------------------------


def test_composition_is_deterministic() -> None:
    a = build_operating_policy(
        OperatingPolicyInput(exposed_capabilities=("b.cap", "a.cap", "secret.get"))
    )
    b = build_operating_policy(
        OperatingPolicyInput(exposed_capabilities=("a.cap", "secret.get", "b.cap"))
    )
    c = build_operating_policy(
        OperatingPolicyInput(exposed_capabilities=("b.cap", "a.cap", "secret.get"))
    )
    assert a == b == c


def test_server_id_normalized_to_canonical_form() -> None:
    server_id = uuid4()
    via_uuid = _build(server_id=server_id)
    via_str = _build(server_id=str(server_id))
    assert via_uuid.server_id == via_str.server_id == str(server_id)
    assert via_uuid.text == via_str.text
    upper = _build(server_id=str(server_id).upper())
    assert upper.server_id == str(server_id)


def test_server_id_none_is_explicit() -> None:
    snapshot = _build()
    assert snapshot.server_id is None


def test_snapshot_fields_carry_version_and_state() -> None:
    snapshot = _build(exposed_capabilities=("secret.list",))
    assert snapshot.version == OPERATING_POLICY_VERSION == 1
    assert snapshot.secret_route_latched is True
    assert isinstance(snapshot.text, str) and snapshot.text


def test_snapshot_is_frozen() -> None:
    snapshot = _build()
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.version = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# fail-closed bounds and input validation
# ---------------------------------------------------------------------------


def test_admin_instructions_over_limit_rejected() -> None:
    with pytest.raises(OperatingPolicyError) as excinfo:
        _build(admin_server_instructions="x" * (MAX_ADMIN_INSTRUCTIONS_CHARS + 1))
    assert excinfo.value.code is OperatingPolicyErrorCode.ADMIN_INSTRUCTIONS_TOO_LARGE


def test_max_admin_instructions_still_fits_policy_ceiling() -> None:
    snapshot = _build(
        is_child_run=True,
        secret_route_latched=True,
        admin_server_instructions="x" * MAX_ADMIN_INSTRUCTIONS_CHARS,
    )
    assert len(snapshot.text) <= MAX_OPERATING_POLICY_CHARS


def test_policy_char_ceiling_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(operating_policy, "MAX_OPERATING_POLICY_CHARS", 64)
    with pytest.raises(OperatingPolicyError) as excinfo:
        _build()
    assert excinfo.value.code is OperatingPolicyErrorCode.POLICY_TOO_LARGE


def test_invalid_capability_entries_rejected() -> None:
    with pytest.raises(OperatingPolicyError) as blank:
        _build(exposed_capabilities=("app.discover", "   "))
    assert blank.value.code is OperatingPolicyErrorCode.INVALID_INPUT
    with pytest.raises(OperatingPolicyError) as non_string:
        _build(exposed_capabilities=("app.discover", 7))
    assert non_string.value.code is OperatingPolicyErrorCode.INVALID_INPUT


def test_bare_string_capabilities_rejected_by_builder() -> None:
    """`str` satisfies Iterable[str]; a bare string must fail closed."""
    with pytest.raises(OperatingPolicyError) as str_case:
        _build(exposed_capabilities="secret.list")
    assert str_case.value.code is OperatingPolicyErrorCode.INVALID_INPUT
    with pytest.raises(OperatingPolicyError) as bytes_case:
        _build(exposed_capabilities=b"secret.list")
    assert bytes_case.value.code is OperatingPolicyErrorCode.INVALID_INPUT


def test_bare_string_capabilities_rejected_by_latch_helper() -> None:
    """The latch helper rejects bare str/bytes even when already latched."""
    for prior in (False, True):
        with pytest.raises(OperatingPolicyError) as str_case:
            is_secret_route_latched("secret.list", prior_secret_route_latched=prior)
        assert str_case.value.code is OperatingPolicyErrorCode.INVALID_INPUT
        with pytest.raises(OperatingPolicyError) as bytes_case:
            is_secret_route_latched(b"secret.list", prior_secret_route_latched=prior)
        assert bytes_case.value.code is OperatingPolicyErrorCode.INVALID_INPUT


def test_invalid_server_id_rejected() -> None:
    with pytest.raises(OperatingPolicyError) as malformed:
        _build(server_id="not-a-uuid")
    assert malformed.value.code is OperatingPolicyErrorCode.INVALID_INPUT
    with pytest.raises(OperatingPolicyError) as wrong_type:
        _build(server_id=12345)
    assert wrong_type.value.code is OperatingPolicyErrorCode.INVALID_INPUT


def test_non_bool_flags_rejected() -> None:
    with pytest.raises(OperatingPolicyError) as child:
        _build(is_child_run=1)
    assert child.value.code is OperatingPolicyErrorCode.INVALID_INPUT
    with pytest.raises(OperatingPolicyError) as latch:
        _build(secret_route_latched="yes")
    assert latch.value.code is OperatingPolicyErrorCode.INVALID_INPUT


def test_default_composition_respects_char_ceiling() -> None:
    snapshot = _build()
    assert len(snapshot.text) <= MAX_OPERATING_POLICY_CHARS
