"""Focused tests for the service-knowledge domain contracts (ask_09).

Pure-module coverage: frozen limits/enum vocabulary, fail-closed model
validation (extra=forbid, bounds, CAS requiredness), frozen page
derivation and deterministic ordering, scope guard, exactly-one-of
selector, envelope bound at real serialized sizes, per-effect status
derivation (partial vs unknown stay distinct), and a recursive
Field-description gate mirroring the agent-readiness walker. No
database, no filesystem, no I/O — Pydantic serializers only.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from mai.applications import knowledge_contracts as kc
from mai.applications.knowledge_contracts import (
    DEFAULT_PAGE_LIMIT,
    EnvelopeOverflowError,
    FactCas,
    FactPage,
    FactRecord,
    FactSource,
    FactSourceKind,
    FactStatus,
    MAX_ACTIVE_FACTS_PER_SERVICE,
    MAX_FACTS_PER_LEARN_REQUEST,
    MAX_FACT_KEY_CHARS,
    MAX_FACT_STATEMENT_CHARS,
    MAX_PAGE_LIMIT,
    MAX_SERIALIZED_ENVELOPE_BYTES,
    MAX_SOURCE_REFS_PER_FACT,
    LearnRequest,
    OperationPage,
    OperationReceipt,
    OperationStatus,
    ProposedFactDelta,
    ScopeMismatchError,
    ServiceGenerationCas,
    ServiceSelector,
    SubEffectStatus,
    TrustedSourceContext,
    derive_operation_status,
    deterministic_order_key,
    enforce_envelope_bound,
    validate_same_server_scope,
)

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SERVER_ID = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
_CONTEXT_SERVER_ID = uuid.UUID("3f2e9a10-5b6c-4d7e-8f90-1a2b3c4d5e6f")


# ---------------------------------------------------------------------------
# frozen constants and vocabulary
# ---------------------------------------------------------------------------


def test_frozen_limits_are_exact() -> None:
    assert DEFAULT_PAGE_LIMIT == 10
    assert MAX_PAGE_LIMIT == 50
    assert MAX_ACTIVE_FACTS_PER_SERVICE == 64
    assert MAX_FACT_STATEMENT_CHARS == 800
    assert MAX_FACT_KEY_CHARS == 80
    assert MAX_FACTS_PER_LEARN_REQUEST == 8
    assert MAX_SOURCE_REFS_PER_FACT == 4
    assert MAX_SERIALIZED_ENVELOPE_BYTES == 64 * 1024
    assert isinstance(MAX_SERIALIZED_ENVELOPE_BYTES, int)


def test_fact_and_operation_vocabulary_is_exact() -> None:
    assert {status.value for status in FactStatus} == {
        "active",
        "superseded",
        "retracted",
        "conflict",
    }
    assert {kind.value for kind in FactSourceKind} == {
        "tool_result",
        "user_statement",
    }
    progression = [status.value for status in OperationStatus]
    assert progression == [
        "accepted",
        "queued",
        "applying",
        "applied",
        "failed",
        "partial",
        "unknown",
    ]
    # partial and unknown are distinct frozen states.
    assert OperationStatus.PARTIAL is not OperationStatus.UNKNOWN
    assert OperationStatus.PARTIAL.value != OperationStatus.UNKNOWN.value


# ---------------------------------------------------------------------------
# minimal valid instances + fail-closed validation matrix
# ---------------------------------------------------------------------------


def _fact_record(**overrides) -> FactRecord:
    base: dict = {
        "fact_id": uuid.UUID(int=1),
        "category": "deployment",
        "key": "compose-directory",
        "statement": "the stack lives under /srv/media",
        "revision": 3,
        "created_at": _EPOCH,
        "source": FactSource(
            source_kind=FactSourceKind.TOOL_RESULT,
            source_ref="run:1:step:2",
            verified_at=_EPOCH,
        ),
        "supersedes_fact_id": None,
        "status": FactStatus.ACTIVE,
    }
    base.update(overrides)
    return FactRecord(**base)


def _fact_record_json(**overrides) -> dict:
    return json.loads(_fact_record(**overrides).model_dump_json())


def test_fact_record_accepts_minimal_valid_instance() -> None:
    record = _fact_record()
    assert record.fact_id == uuid.UUID(int=1)
    assert record.status is FactStatus.ACTIVE
    assert record.source.source_kind is FactSourceKind.TOOL_RESULT
    assert record.supersedes_fact_id is None
    assert len(record.statement) <= MAX_FACT_STATEMENT_CHARS


def test_fact_record_rejects_extra_and_missing_fields() -> None:
    with pytest.raises(ValidationError):
        _fact_record(unexpected_field="nope")
    payload = _fact_record_json()
    del payload["statement"]
    with pytest.raises(ValidationError):
        FactRecord(**payload)


def test_fact_record_rejects_wrong_types_and_bad_uuid() -> None:
    with pytest.raises(ValidationError):
        _fact_record(revision="not-an-int")
    with pytest.raises(ValidationError):
        _fact_record(fact_id="not-a-uuid")
    with pytest.raises(ValidationError):
        _fact_record(status="vanished")


@pytest.mark.parametrize("length", [0, MAX_FACT_STATEMENT_CHARS + 1])
def test_fact_statement_bounds(length: int) -> None:
    with pytest.raises(ValidationError):
        _fact_record(statement="x" * length)


def test_fact_statement_exact_bound_ok_and_overflow_rejected() -> None:
    ok = _fact_record(statement="x" * MAX_FACT_STATEMENT_CHARS)
    assert len(ok.statement) == MAX_FACT_STATEMENT_CHARS
    with pytest.raises(ValidationError):
        _fact_record(statement="x" * (MAX_FACT_STATEMENT_CHARS + 1))


def test_fact_key_exact_bound_ok_and_overflow_rejected() -> None:
    ok = _fact_record(key="k" * MAX_FACT_KEY_CHARS)
    assert len(ok.key) == MAX_FACT_KEY_CHARS
    with pytest.raises(ValidationError):
        _fact_record(key="k" * (MAX_FACT_KEY_CHARS + 1))


def test_fact_revision_and_supersession_validation() -> None:
    with pytest.raises(ValidationError):
        _fact_record(revision=-1)
    prior = uuid.UUID(int=9)
    corrected = _fact_record(
        supersedes_fact_id=prior, status=FactStatus.SUPERSEDED
    )
    assert corrected.supersedes_fact_id == prior


def test_fact_source_bounds_and_kinds() -> None:
    source = FactSource(
        source_kind=FactSourceKind.USER_STATEMENT,
        source_ref="r" * 256,
        verified_at=_EPOCH,
    )
    assert len(source.source_ref) == 256
    with pytest.raises(ValidationError):
        FactSource(
            source_kind="shouting", source_ref="r", verified_at=_EPOCH
        )
    with pytest.raises(ValidationError):
        FactSource(
            source_kind=FactSourceKind.TOOL_RESULT, source_ref="", verified_at=_EPOCH
        )


def test_learn_request_bounds() -> None:
    delta = {
        "category": "deployment",
        "key": "health-path",
        "statement": "health lives at /healthz",
        "source_kind": FactSourceKind.TOOL_RESULT,
        "source_refs": ["run:1:step:2"],
    }
    request = LearnRequest(facts=[ProposedFactDelta(**delta)], expected_generation=4)
    assert request.expected_generation == 4
    with pytest.raises(ValidationError):
        LearnRequest(
            facts=[ProposedFactDelta(**delta) for _ in range(MAX_FACTS_PER_LEARN_REQUEST + 1)],
            expected_generation=4,
        )
    exactly_max = LearnRequest(
        facts=[ProposedFactDelta(**delta) for _ in range(MAX_FACTS_PER_LEARN_REQUEST)],
        expected_generation=0,
    )
    assert len(exactly_max.facts) == MAX_FACTS_PER_LEARN_REQUEST


def test_learn_request_cas_fields_are_required_not_defaulted() -> None:
    delta = ProposedFactDelta(
        category="deployment",
        key="health-path",
        statement="health lives at /healthz",
        source_kind=FactSourceKind.TOOL_RESULT,
        source_refs=["run:1:step:2"],
    )
    payload = {
        "facts": [delta.model_dump(mode="json")],
        "expected_generation": 2,
    }
    parsed = LearnRequest(**payload)
    assert parsed.expected_generation == 2
    del payload["expected_generation"]
    with pytest.raises(ValidationError):
        LearnRequest(**payload)
    with pytest.raises(ValidationError):
        LearnRequest(**{**payload, "expected_generation": -1})
    cas_payload = {
        "fact_id": str(uuid.UUID(int=3)),
        "expected_revision": 1,
        "new_statement": "corrected",
    }
    parsed_cas = FactCas(**cas_payload)
    assert parsed_cas.expected_revision == 1
    del cas_payload["expected_revision"]
    with pytest.raises(ValidationError):
        FactCas(**cas_payload)
    with pytest.raises(ValidationError):
        FactCas(**{**cas_payload, "expected_revision": -1})
    with pytest.raises(ValidationError):
        ServiceGenerationCas(expected_generation=-1)


def test_proposed_delta_source_refs_and_bounds() -> None:
    delta = ProposedFactDelta(
        category="deployment",
        key="health-path",
        statement="health lives at /healthz",
        source_kind=FactSourceKind.TOOL_RESULT,
        source_refs=["run:1:step:2", "run:1:step:9"],
    )
    assert len(delta.source_refs) == 2
    with pytest.raises(ValidationError):
        ProposedFactDelta(
            category="deployment",
            key="health-path",
            statement="s",
            source_kind=FactSourceKind.TOOL_RESULT,
            source_refs=[
                "a",
                "b",
                "c",
                "d",
                "e",
            ][: MAX_SOURCE_REFS_PER_FACT + 1],
        )
    with pytest.raises(ValidationError):
        ProposedFactDelta(
            category="deployment",
            key="health-path",
            statement="s",
            source_kind=FactSourceKind.TOOL_RESULT,
            source_refs=["   "],
        )
    with pytest.raises(ValidationError):
        ProposedFactDelta(
            category="deployment",
            key="k" * (MAX_FACT_KEY_CHARS + 1),
            statement="s",
            source_kind=FactSourceKind.TOOL_RESULT,
            source_refs=["a"],
        )
    # Each provenance reference shares the persisted 256-char ceiling.
    exact_ref = ProposedFactDelta(
        category="deployment",
        key="health-path",
        statement="s",
        source_kind=FactSourceKind.TOOL_RESULT,
        source_refs=["r" * 256],
    )
    assert len(exact_ref.source_refs[0]) == 256
    with pytest.raises(ValidationError):
        ProposedFactDelta(
            category="deployment",
            key="health-path",
            statement="s",
            source_kind=FactSourceKind.TOOL_RESULT,
            source_refs=["r" * 257],
        )


def test_trusted_source_context_is_separate_from_payload() -> None:
    context = TrustedSourceContext(
        server_id=_SERVER_ID,
        run_id=uuid.uuid4(),
        tool_call_id="call-1",
        actor="runtime",
        grants=["secret.store"],
        context_at=_EPOCH,
    )
    assert context.session_id is None
    assert context.grants == ["secret.store"]
    with pytest.raises(ValidationError):
        TrustedSourceContext(
            server_id=_SERVER_ID,
            run_id=uuid.uuid4(),
            tool_call_id="call-1",
            actor="runtime",
            grants=["secret.store"],
            context_at=_EPOCH,
            handler_name="nope",
        )


# ---------------------------------------------------------------------------
# page derivation + deterministic ordering
# ---------------------------------------------------------------------------


def _records(count: int, statement_size: int = 40) -> list[FactRecord]:
    return [
        _fact_record(
            fact_id=uuid.UUID(int=index),
            key=f"key-{index:03d}",
            statement="x" * statement_size,
            created_at=_EPOCH,
        )
        for index in range(1, count + 1)
    ]


def test_page_derivation_has_more_math_at_boundaries() -> None:
    records = _records(5)
    # Exact fill: 5 items, page size 5, offset 0 -> no more.
    page = FactPage.build(records, total=5, offset=0, limit=5)
    assert len(page.items) == 5 and page.has_more is False
    # Last page: offset 3, 2 items, total 5 -> no more.
    page = FactPage.build(records[3:], total=5, offset=3, limit=5)
    assert len(page.items) == 2 and page.has_more is False
    # Middle page: offset 0, limit 2, total 5 -> more.
    page = FactPage.build(records[:2], total=5, offset=0, limit=2)
    assert page.has_more is True
    # Offset beyond total: empty items, has_more False.
    page = FactPage.build([], total=5, offset=9, limit=5)
    assert page.items == [] and page.has_more is False
    assert page.limit == 5 and page.offset == 9
    # Page-size bounds mirror the frozen limits.
    with pytest.raises(ValidationError):
        FactPage.build(records, total=5, limit=MAX_PAGE_LIMIT + 1)
    with pytest.raises(ValidationError):
        FactPage.build(records, total=5, limit=0)


def test_page_ordering_is_deterministic_created_at_then_id() -> None:
    same_time_low_id = _fact_record(
        fact_id=uuid.UUID(int=1), created_at=_EPOCH
    )
    same_time_high_id = _fact_record(
        fact_id=uuid.UUID(int=2), created_at=_EPOCH
    )
    later_time = _fact_record(
        fact_id=uuid.UUID(int=3),
        created_at=_EPOCH.replace(year=_EPOCH.year + 1),
    )
    ordered = sorted(
        [same_time_high_id, later_time, same_time_low_id],
        key=lambda record: deterministic_order_key(
            record.created_at, record.fact_id
        ),
    )
    # Same created_at ties break on the canonical id string ascending;
    # a later created_at always sorts after, whatever the id.
    assert [record.fact_id for record in ordered] == [
        uuid.UUID(int=1),
        uuid.UUID(int=2),
        uuid.UUID(int=3),
    ]
    assert deterministic_order_key(_EPOCH, uuid.UUID(int=7)) == (
        _EPOCH,
        str(uuid.UUID(int=7)),
    )


def test_operation_page_derivation_matches_fact_page() -> None:
    receipt = OperationReceipt(
        operation_id=uuid.UUID(int=4),
        status=OperationStatus.ACCEPTED,
        payload_digest="a" * 64,
        sub_effects={"facts": SubEffectStatus.PENDING},
        accepted_at=_EPOCH,
        workflow_id="wf-1",
    )
    page = OperationPage.build([receipt], total=1, offset=0, limit=10)
    assert page.items == [receipt] and page.has_more is False
    assert page.limit == DEFAULT_PAGE_LIMIT


# ---------------------------------------------------------------------------
# operation status derivation: partial vs unknown stay distinct
# ---------------------------------------------------------------------------


def test_derive_operation_status_frozen_rules() -> None:
    assert derive_operation_status({}) is OperationStatus.APPLYING
    assert (
        derive_operation_status(
            {"adoption": SubEffectStatus.PENDING, "facts": "applied"}
        )
        is OperationStatus.APPLYING
    )
    assert (
        derive_operation_status({"facts": SubEffectStatus.APPLIED})
        is OperationStatus.APPLIED
    )
    assert (
        derive_operation_status(
            {"facts": SubEffectStatus.APPLIED, "map": SubEffectStatus.APPLIED}
        )
        is OperationStatus.APPLIED
    )
    assert (
        derive_operation_status(
            {"facts": SubEffectStatus.FAILED, "map": SubEffectStatus.FAILED}
        )
        is OperationStatus.FAILED
    )
    assert (
        derive_operation_status(
            {"facts": SubEffectStatus.APPLIED, "map": SubEffectStatus.FAILED}
        )
        is OperationStatus.PARTIAL
    )
    assert (
        derive_operation_status({"facts": SubEffectStatus.UNKNOWN})
        is OperationStatus.UNKNOWN
    )
    assert (
        derive_operation_status(
            {"facts": SubEffectStatus.APPLIED, "map": SubEffectStatus.UNKNOWN}
        )
        is OperationStatus.UNKNOWN
    )


def test_partial_and_unknown_never_collapse() -> None:
    all_states = {status.value for status in SubEffectStatus}
    combos: list[dict[str, SubEffectStatus]] = [
        {"adoption": SubEffectStatus(value), "facts": SubEffectStatus(value)}
        for value in all_states
    ]
    combos.extend(
        [
            {"adoption": SubEffectStatus.APPLIED, "facts": SubEffectStatus.FAILED},
            {"adoption": SubEffectStatus.FAILED, "facts": SubEffectStatus.UNKNOWN},
            {"adoption": SubEffectStatus.PENDING, "facts": SubEffectStatus.UNKNOWN},
        ]
    )
    derived = [derive_operation_status(combo) for combo in combos]
    assert OperationStatus.UNKNOWN in derived
    assert OperationStatus.PARTIAL in derived
    assert OperationStatus.UNKNOWN is not OperationStatus.PARTIAL
    # unknown wins even alongside failures (never collapsed into failed).
    mixed_unknown = derive_operation_status(
        {"a": SubEffectStatus.FAILED, "b": SubEffectStatus.UNKNOWN}
    )
    assert mixed_unknown is OperationStatus.UNKNOWN


def test_derive_operation_status_accepts_plain_strings() -> None:
    assert (
        derive_operation_status({"facts": "applied", "map": "failed"})
        is OperationStatus.PARTIAL
    )
    assert (
        derive_operation_status({"facts": "unknown"}) is OperationStatus.UNKNOWN
    )


def test_operation_receipt_validates_digest_and_effects() -> None:
    receipt = OperationReceipt(
        operation_id=uuid.UUID(int=5),
        status=OperationStatus.APPLIED,
        payload_digest="b" * 64,
        sub_effects={"adoption": SubEffectStatus.APPLIED},
        accepted_at=_EPOCH,
        workflow_id="wf-check",
    )
    assert receipt.payload_digest == "b" * 64
    with pytest.raises(ValidationError):
        OperationReceipt(
            operation_id=uuid.UUID(int=5),
            status=OperationStatus.APPLIED,
            payload_digest="Z" * 64,
            sub_effects={},
            accepted_at=_EPOCH,
            workflow_id="wf-check",
        )
    with pytest.raises(ValidationError):
        OperationReceipt(
            operation_id=uuid.UUID(int=5),
            status=OperationStatus.APPLIED,
            payload_digest="b" * 63,
            sub_effects={"adoption": SubEffectStatus.APPLIED},
            accepted_at=_EPOCH,
            workflow_id="wf-check",
        )


# ---------------------------------------------------------------------------
# scope guard (C3)
# ---------------------------------------------------------------------------


def test_scope_guard_same_and_omitted() -> None:
    resolved = validate_same_server_scope(_SERVER_ID, _SERVER_ID)
    assert resolved == _SERVER_ID
    omitted = validate_same_server_scope(None, _SERVER_ID)
    assert omitted == _SERVER_ID


def test_scope_guard_rejects_foreign_server_with_bounded_message() -> None:
    with pytest.raises(ScopeMismatchError) as excinfo:
        validate_same_server_scope(_CONTEXT_SERVER_ID, _SERVER_ID)
    message = str(excinfo.value)
    assert len(message) <= 200
    assert str(_CONTEXT_SERVER_ID) in message


# ---------------------------------------------------------------------------
# selector exactly-one-of (D2)
# ---------------------------------------------------------------------------


def test_selector_accepts_exactly_one_arm() -> None:
    by_id = ServiceSelector(application_id=uuid.UUID(int=6))
    assert by_id.application_id is not None
    by_compose = ServiceSelector(
        compose_project="media", compose_directory="/srv/media"
    )
    assert by_compose.compose_directory == "/srv/media"
    by_unit = ServiceSelector(systemd_unit="jellyfin.service")
    assert by_unit.systemd_unit == "jellyfin.service"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"application_id": str(uuid.UUID(int=6)), "systemd_unit": "a.service"},
        {
            "compose_project": "media",
            "compose_directory": "/srv/media",
            "application_id": str(uuid.UUID(int=6)),
        },
        {"compose_project": "media"},
        {"compose_directory": "/srv/media"},
        {
            "compose_project": "media",
            "compose_directory": "/srv/media",
            "systemd_unit": "a.service",
        },
    ],
)
def test_selector_rejects_zero_or_multiple_arms(payload: dict) -> None:
    with pytest.raises(ValidationError):
        ServiceSelector(**payload)


# ---------------------------------------------------------------------------
# envelope bound (C1: strictly below 64 KiB)
# ---------------------------------------------------------------------------


def test_bounded_page_serializes_strictly_below_cap() -> None:
    records = _records(DEFAULT_PAGE_LIMIT, statement_size=MAX_FACT_STATEMENT_CHARS)
    page = FactPage.build(records, total=DEFAULT_PAGE_LIMIT, offset=0, limit=DEFAULT_PAGE_LIMIT)
    serialized = page.model_dump_json()
    assert len(serialized.encode("utf-8")) < MAX_SERIALIZED_ENVELOPE_BYTES
    bounded = enforce_envelope_bound(page)
    assert isinstance(bounded, bytes)
    assert len(bounded) < MAX_SERIALIZED_ENVELOPE_BYTES


def test_enforce_envelope_bound_accepts_bytes_below_cap() -> None:
    payload = b'{"ok": true}'
    assert enforce_envelope_bound(payload) == payload


def test_envelope_overflow_raises_with_bounded_reference() -> None:
    # A full page (50 = MAX_PAGE_LIMIT) of maximal facts (800-char
    # statements + 256-char provenance refs) serializes far above the
    # 64 KiB dispatcher cap. (The service-level fact cap is 64, but a
    # single PAGE never exceeds 50 rows.)
    oversized = _records(
        MAX_PAGE_LIMIT, statement_size=MAX_FACT_STATEMENT_CHARS
    )
    oversized = [
        record.model_copy(
            update={
                "source": FactSource(
                    source_kind=FactSourceKind.TOOL_RESULT,
                    source_ref="r" * 256,
                    verified_at=_EPOCH,
                ),
                "category": "c" * 64,
                "key": "k" * MAX_FACT_KEY_CHARS,
            }
        )
        for record in oversized
    ]
    page = FactPage.build(
        oversized,
        total=MAX_ACTIVE_FACTS_PER_SERVICE,
        offset=0,
        limit=MAX_PAGE_LIMIT,
    )
    raw = page.model_dump_json().encode("utf-8")
    assert len(raw) >= MAX_SERIALIZED_ENVELOPE_BYTES
    with pytest.raises(EnvelopeOverflowError) as excinfo:
        enforce_envelope_bound(page)
    assert excinfo.value.actual_bytes >= MAX_SERIALIZED_ENVELOPE_BYTES
    assert "FactPage" in excinfo.value.entry_reference
    # Bounded reference names the entry type, never the payload content.
    assert "xxxx" not in str(excinfo.value)
    with pytest.raises(EnvelopeOverflowError):
        enforce_envelope_bound(b"x" * MAX_SERIALIZED_ENVELOPE_BYTES)


def test_envelope_bound_is_strict_below_not_equal() -> None:
    payload = b"x" * (MAX_SERIALIZED_ENVELOPE_BYTES - 1)
    assert enforce_envelope_bound(payload) == payload
    with pytest.raises(EnvelopeOverflowError):
        enforce_envelope_bound(b"x" * MAX_SERIALIZED_ENVELOPE_BYTES)


def test_envelope_overflow_error_is_typed_value_error() -> None:
    error = EnvelopeOverflowError(70000, "FactPage:fact_id=1")
    assert isinstance(error, ValueError)
    assert error.actual_bytes == 70000
    assert len(error.entry_reference) <= MAX_FACT_KEY_CHARS


# ---------------------------------------------------------------------------
# agent-readiness mirror: Field descriptions >= 12 chars recursively
# ---------------------------------------------------------------------------


def test_module_is_pure_no_io_imports() -> None:
    """The contracts module imports nothing beyond stdlib + pydantic:
    no filesystem, database, network or clock surface."""
    import ast
    from pathlib import Path

    source = Path(kc.__file__ or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "uuid",
        "collections",
        "datetime",
        "enum",
        "typing",
        "pydantic",
        "__future__",
    }


def test_every_field_description_is_at_least_twelve_chars() -> None:
    """Mirror of ``assert_agent_ready_descriptor``'s Field-description
    gate, applied recursively to every contract model (projection to
    descriptors is slice 20-21; this pins the description discipline
    now)."""
    import typing

    import pydantic

    model_classes = [
        value
        for value in vars(kc).values()
        if isinstance(value, type)
        and issubclass(value, pydantic.BaseModel)
        and value.__module__ == kc.__name__
    ]
    assert model_classes, "contract models must be discoverable"
    failures: list[str] = []

    def _walk(model: type, seen: set[type]) -> None:
        if model in seen:
            return
        seen.add(model)
        for name, field in model.model_fields.items():
            description = field.description or ""
            if len(description) < 12:
                failures.append(f"{model.__name__}.{name}: {description!r}")
            annotation = field.annotation
            nested: list[type] = []
            origin = typing.get_origin(annotation)
            if origin is not None:
                nested.extend(
                    arg
                    for arg in typing.get_args(annotation)
                    if isinstance(arg, type)
                    and issubclass(arg, pydantic.BaseModel)
                )
            elif isinstance(annotation, type) and issubclass(
                annotation, pydantic.BaseModel
            ):
                nested.append(annotation)
            for nested_model in nested:
                _walk(nested_model, seen)

    for model in model_classes:
        _walk(model, set())
    assert failures == [], failures


def test_all_models_forbid_extra_fields() -> None:
    import pydantic

    for value in vars(kc).values():
        if (
            isinstance(value, type)
            and issubclass(value, pydantic.BaseModel)
            and value.__module__ == kc.__name__
        ):
            assert value.model_config.get("extra") == "forbid", value.__name__
