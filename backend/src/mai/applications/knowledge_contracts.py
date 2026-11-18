"""Service-knowledge domain contracts (ask_09, campaign D2/D3/D5, C1-C5).

The ONE pure, bounded, versioned schema surface for the service-knowledge
feature: identities, learned-fact deltas with provenance, compare-and-set
envelopes, durable operation receipts, paging metadata, selector unions,
scope guards and the serialized-envelope bound. Downstream slices
(10-migration, 11-read, 12-write, 13/14-operations, 20/21-tools) import
FROM this module; the exact names here are the frozen cross-slice
vocabulary.

Pure by construction: no filesystem, no database, no network, no clock,
no singletons — only types, frozen limits, validators and small pure
helper functions. Trusted runtime identity (actor, grants, run/session/
tool-call ids, timestamps) is a SEPARATE model family
(:class:`TrustedSourceContext`) that server code fills from runtime —
NO model field here accepts handler names, actor identity or grant
lists from the model.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "EnvelopeOverflowError",
    "FactCas",
    "FactPage",
    "FactRecord",
    "FactSource",
    "FactSourceKind",
    "FactStatus",
    "MAX_ACTIVE_FACTS_PER_SERVICE",
    "MAX_FACTS_PER_LEARN_REQUEST",
    "MAX_FACT_KEY_CHARS",
    "MAX_FACT_STATEMENT_CHARS",
    "MAX_PAGE_LIMIT",
    "MAX_SERIALIZED_ENVELOPE_BYTES",
    "MAX_SOURCE_REFS_PER_FACT",
    "MAX_SOURCE_REF_CHARS",
    "LearnRequest",
    "OperationPage",
    "OperationReceipt",
    "OperationStatus",
    "ProposedFactDelta",
    "ScopeMismatchError",
    "ServiceGenerationCas",
    "ServiceSelector",
    "SubEffectStatus",
    "TrustedSourceContext",
    "derive_operation_status",
    "deterministic_order_key",
    "enforce_envelope_bound",
    "validate_same_server_scope",
]

# ---------------------------------------------------------------------------
# frozen limits (campaign C1/D2/D3/D5 — never invented downstream)
# ---------------------------------------------------------------------------

#: Default page size for every knowledge collection read (AGENTS.md
#: pagination invariant).
DEFAULT_PAGE_LIMIT: Final[int] = 10

#: Hard page-size ceiling for every knowledge collection read.
MAX_PAGE_LIMIT: Final[int] = 50

#: Active learned facts retained per service identity.
MAX_ACTIVE_FACTS_PER_SERVICE: Final[int] = 64

#: Learned-fact statement character ceiling.
MAX_FACT_STATEMENT_CHARS: Final[int] = 800

#: Learned-fact semantic key character ceiling.
MAX_FACT_KEY_CHARS: Final[int] = 80

#: Proposed fact deltas accepted per learn request.
MAX_FACTS_PER_LEARN_REQUEST: Final[int] = 8

#: Bounded provenance references per proposed fact.
MAX_SOURCE_REFS_PER_FACT: Final[int] = 4

#: Per-reference character ceiling (persisted provenance references and
#: proposed source_refs entries share the same bound).
MAX_SOURCE_REF_CHARS: Final[int] = 256

#: The CapabilityDispatcher serialized-envelope cap: payloads must stay
#: STRICTLY BELOW this byte count (equal is already overflow).
MAX_SERIALIZED_ENVELOPE_BYTES: Final[int] = 64 * 1024

# ---------------------------------------------------------------------------
# model plumbing (fail-closed capture everywhere)
# ---------------------------------------------------------------------------

#: Every model in this module forbids extra fields.
_FORBID_EXTRA: Final[ConfigDict] = ConfigDict(extra="forbid")


class FactStatus(StrEnum):
    """Learned-fact lifecycle status (D3): exactly these four values."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    CONFLICT = "conflict"


class FactSourceKind(StrEnum):
    """Accepted provenance kinds for a learned fact (D3)."""

    TOOL_RESULT = "tool_result"
    USER_STATEMENT = "user_statement"


class SubEffectStatus(StrEnum):
    """Per-effect state inside one durable knowledge operation.

    ``partial`` and ``unknown`` are DISTINCT never-collapsed states:
    ``partial`` = some sub-effects applied, others failed; ``unknown`` =
    authoritative state not provable and needing reconciliation.
    """

    APPLIED = "applied"
    FAILED = "failed"
    UNKNOWN = "unknown"
    PENDING = "pending"


class OperationStatus(StrEnum):
    """Durable operation status progression (D5): accepted -> queued ->
    applying -> applied | failed | partial | unknown. ``partial`` and
    ``unknown`` are distinct states and are NEVER collapsed into each
    other or into ``failed``."""

    ACCEPTED = "accepted"
    QUEUED = "queued"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# pure helpers (deterministic ordering, scope guard, status derivation,
# envelope bound)
# ---------------------------------------------------------------------------


def deterministic_order_key(
    created_at: datetime, entity_id: uuid.UUID
) -> tuple[datetime, str]:
    """The frozen deterministic ordering key: ``(created_at, id)``
    ascending, the id rendered as its canonical string so the tuple is a
    stable tie-breaker for facts, operations and history rows."""
    return (created_at, str(entity_id))


class ScopeMismatchError(ValueError):
    """An explicit foreign ``server_id`` was supplied where the current
    authorized scope applies (C3): rejected, never substituted."""

    def __init__(
        self,
        payload_server_id: uuid.UUID,
        context_server_id: uuid.UUID,
    ) -> None:
        super().__init__(
            f"payload server_id {payload_server_id} is outside the "
            f"authorized scope {context_server_id}"
        )
        self.payload_server_id = payload_server_id
        self.context_server_id = context_server_id


def validate_same_server_scope(
    payload_server_id: uuid.UUID | None, context_server_id: uuid.UUID
) -> uuid.UUID:
    """Resolve the effective server scope for one payload (C3).

    An omitted payload server (``None``) means "current authorized
    scope" and resolves to ``context_server_id``; an explicit payload
    server that DIFFERS from the context is rejected with
    :class:`ScopeMismatchError` — never silently substituted.
    """
    if payload_server_id is None:
        return context_server_id
    if payload_server_id != context_server_id:
        raise ScopeMismatchError(payload_server_id, context_server_id)
    return payload_server_id


class EnvelopeOverflowError(ValueError):
    """The serialized envelope reached the dispatcher cap
    (:data:`MAX_SERIALIZED_ENVELOPE_BYTES`): fail-closed with a bounded
    reference to the oversized entry — never a fake empty answer."""

    def __init__(self, actual_bytes: int, entry_reference: str) -> None:
        bounded_ref = entry_reference[:MAX_FACT_KEY_CHARS]
        super().__init__(
            f"serialized envelope is {actual_bytes} bytes, at or above "
            f"the {MAX_SERIALIZED_ENVELOPE_BYTES}-byte dispatcher cap "
            f"(oversized entry: {bounded_ref})"
        )
        self.actual_bytes = actual_bytes
        self.entry_reference = bounded_ref


def _bounded_entry_reference(model: BaseModel) -> str:
    """A bounded reference naming the oversized entry (an identity field
    when the model carries one, else the model type) — never payload
    content."""
    for field_name in ("operation_id", "fact_id", "call_id"):
        value = getattr(model, field_name, None)
        if value is not None:
            return f"{type(model).__name__}:{field_name}={value}"
    return type(model).__name__


def enforce_envelope_bound(payload: BaseModel | bytes) -> bytes:
    """Serialize ``payload`` (Pydantic model via ``model_dump_json`` or
    pre-encoded bytes) and fail closed with
    :class:`EnvelopeOverflowError` when the byte count is at or above
    :data:`MAX_SERIALIZED_ENVELOPE_BYTES`. Returns the serialized bytes
    on success."""
    if isinstance(payload, BaseModel):
        entry_reference = _bounded_entry_reference(payload)
        encoded = payload.model_dump_json().encode("utf-8")
    else:
        entry_reference = "raw-bytes"
        encoded = bytes(payload)
    if len(encoded) >= MAX_SERIALIZED_ENVELOPE_BYTES:
        raise EnvelopeOverflowError(len(encoded), entry_reference)
    return encoded


def derive_operation_status(
    sub_effects: Mapping[str, SubEffectStatus | str],
) -> OperationStatus:
    """Derive the durable operation status from its sub-effects (D5,
    frozen rules, evaluated in order):

    1. any ``unknown`` -> ``unknown`` (authoritative state unprovable —
       never collapsed into ``failed``);
    2. else any ``failed`` AND any ``applied`` -> ``partial``;
    3. else all ``failed`` -> ``failed``;
    4. else all ``applied`` -> ``applied``;
    5. else (no sub-effects, or mixes with ``pending``) -> ``applying``.
    """
    statuses = [SubEffectStatus(str(value)) for value in sub_effects.values()]
    if not statuses:
        return OperationStatus.APPLYING
    if SubEffectStatus.UNKNOWN in statuses:
        return OperationStatus.UNKNOWN
    has_applied = SubEffectStatus.APPLIED in statuses
    has_failed = SubEffectStatus.FAILED in statuses
    if has_failed and has_applied:
        return OperationStatus.PARTIAL
    if all(status is SubEffectStatus.FAILED for status in statuses):
        return OperationStatus.FAILED
    if all(status is SubEffectStatus.APPLIED for status in statuses):
        return OperationStatus.APPLIED
    return OperationStatus.APPLYING


# ---------------------------------------------------------------------------
# fact domain (D3)
# ---------------------------------------------------------------------------


class FactSource(BaseModel):
    """Provenance of one learned fact: the accepted source kind, a
    bounded persisted reference to the source artifact, and the
    verification timestamp (D3: provenance must resolve to a persisted
    successful tool result or an explicit user statement in the same
    authorized scope)."""

    model_config = _FORBID_EXTRA

    source_kind: FactSourceKind = Field(
        description="Accepted provenance kind of the learned fact",
    )
    source_ref: str = Field(
        min_length=1,
        max_length=MAX_SOURCE_REF_CHARS,
        description="Bounded persisted reference to the proving source artifact",
    )
    verified_at: datetime = Field(
        description="Timestamp when the source evidence was verified",
    )


class FactRecord(BaseModel):
    """One durable learned fact (D3): identity, semantic key, bounded
    statement, revision, provenance, supersession link and lifecycle
    status."""

    model_config = _FORBID_EXTRA

    fact_id: uuid.UUID = Field(
        description="Canonical UUID identity of this learned fact",
    )
    category: str = Field(
        min_length=1,
        max_length=64,
        description="Bounded category grouping this fact with its peers",
    )
    key: str = Field(
        min_length=1,
        max_length=MAX_FACT_KEY_CHARS,
        description="Bounded semantic key identifying the fact inside its category",
    )
    statement: str = Field(
        min_length=1,
        max_length=MAX_FACT_STATEMENT_CHARS,
        description="Bounded learned statement asserted for this service",
    )
    revision: int = Field(
        ge=0,
        description="Non-negative compare-and-set revision of this fact",
    )
    created_at: datetime = Field(
        description="Creation timestamp used for deterministic ordering",
    )
    source: FactSource = Field(
        description="Provenance of the learned fact (kind, reference, verification)",
    )
    supersedes_fact_id: uuid.UUID | None = Field(
        default=None,
        description="Prior fact identity this correction supersedes, when correcting",
    )
    status: FactStatus = Field(
        description="Lifecycle status of this learned fact",
    )


class FactPage(BaseModel):
    """One bounded page of learned facts (D2): sliced window plus the
    frozen derivation metadata. ``has_more = total > offset + len(items)``
    (C1 frozen rule)."""

    model_config = _FORBID_EXTRA

    items: list[FactRecord] = Field(
        description="The ordered fact records inside this page window",
    )
    limit: int = Field(
        default=DEFAULT_PAGE_LIMIT,
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Requested maximum number of records in this page",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Zero-based offset of this page into the ordered collection",
    )
    total: int = Field(
        default=0,
        ge=0,
        description="Total number of records in the full matching collection",
    )
    has_more: bool = Field(
        description="True when records exist beyond this page window",
    )

    @classmethod
    def build(
        cls,
        items: Sequence[FactRecord],
        *,
        total: int,
        offset: int = 0,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> FactPage:
        """Derive one page from an already-ordered slice (frozen rule:
        ``has_more = total > offset + len(items)``)."""
        window = list(items)[:limit]
        return cls(
            items=window,
            limit=limit,
            offset=offset,
            total=total,
            has_more=total > offset + len(window),
        )


# ---------------------------------------------------------------------------
# durable operations (D5)
# ---------------------------------------------------------------------------


class OperationReceipt(BaseModel):
    """The durable receipt of one knowledge operation (D5): identity,
    honest status progression, the bounded payload digest, per-effect
    sub-statuses (partial vs unknown stay distinct) and the deterministic
    workflow identity."""

    model_config = _FORBID_EXTRA

    operation_id: uuid.UUID = Field(
        description="Canonical UUID identity of the durable operation",
    )
    status: OperationStatus = Field(
        description="Honest durable status of the operation (partial and "
        "unknown are distinct states)",
    )
    payload_digest: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 hex digest of the accepted operation payload",
    )
    sub_effects: dict[str, SubEffectStatus] = Field(
        description="Per-effect durable status by effect name (adoption, map, "
        "docs, facts)",
    )
    accepted_at: datetime = Field(
        description="Trusted timestamp when the operation was accepted",
    )
    workflow_id: str = Field(
        min_length=1,
        max_length=256,
        description="Deterministic workflow identity executing the operation",
    )


class OperationPage(BaseModel):
    """One bounded page of durable operation receipts (D5), same frozen
    paging derivation as :class:`FactPage`."""

    model_config = _FORBID_EXTRA

    items: list[OperationReceipt] = Field(
        description="The ordered operation receipts inside this page window",
    )
    limit: int = Field(
        default=DEFAULT_PAGE_LIMIT,
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Requested maximum number of receipts in this page",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Zero-based offset of this page into the ordered collection",
    )
    total: int = Field(
        default=0,
        ge=0,
        description="Total number of receipts in the full matching collection",
    )
    has_more: bool = Field(
        description="True when receipts exist beyond this page window",
    )

    @classmethod
    def build(
        cls,
        items: Sequence[OperationReceipt],
        *,
        total: int,
        offset: int = 0,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> OperationPage:
        """Derive one page from an already-ordered slice (frozen rule)."""
        window = list(items)[:limit]
        return cls(
            items=window,
            limit=limit,
            offset=offset,
            total=total,
            has_more=total > offset + len(window),
        )


# ---------------------------------------------------------------------------
# learn delta + trusted runtime context (D3/D5)
# ---------------------------------------------------------------------------


class ProposedFactDelta(BaseModel):
    """One proposed learned fact inside a learn request (D3): bounded
    key/statement, accepted source kind, and at most
    :data:`MAX_SOURCE_REFS_PER_FACT` provenance references. No handler
    names, actor identity or grant lists are accepted from the model."""

    model_config = _FORBID_EXTRA

    category: str = Field(
        min_length=1,
        max_length=64,
        description="Bounded category grouping the proposed fact",
    )
    key: str = Field(
        min_length=1,
        max_length=MAX_FACT_KEY_CHARS,
        description="Bounded semantic key identifying the proposed fact",
    )
    statement: str = Field(
        min_length=1,
        max_length=MAX_FACT_STATEMENT_CHARS,
        description="Bounded learned statement proposed for persistence",
    )
    source_kind: FactSourceKind = Field(
        description="Accepted provenance kind claimed for the proposal",
    )
    source_refs: list[str] = Field(
        min_length=1,
        max_length=MAX_SOURCE_REFS_PER_FACT,
        description="Bounded persisted provenance references proving this "
        "proposal (at most four per fact)",
    )

    @field_validator("source_refs")
    @classmethod
    def _refs_non_empty(cls, value: list[str]) -> list[str]:
        for ref in value:
            if not ref.strip():
                raise ValueError("source_refs entries must be non-empty")
            if len(ref) > MAX_SOURCE_REF_CHARS:
                raise ValueError(
                    f"source_refs entries must be at most "
                    f"{MAX_SOURCE_REF_CHARS} characters"
                )
        return value


class TrustedSourceContext(BaseModel):
    """Trusted runtime identity attached to a learn request SERVER-SIDE
    (D5): filled exclusively from trusted runtime state (run, session,
    tool-call ids, actor, grants, timestamps) — never from model
    payload. Kept separate from the delta exactly so the model cannot
    assert its own authority."""

    model_config = _FORBID_EXTRA

    server_id: uuid.UUID = Field(
        description="Authorized server identity from the trusted runtime scope",
    )
    run_id: uuid.UUID = Field(
        description="Durable run identity from the trusted runtime context",
    )
    session_id: uuid.UUID | None = Field(
        default=None,
        description="Owning session identity when the run belongs to a session",
    )
    tool_call_id: str = Field(
        min_length=1,
        max_length=256,
        description="Durable tool-call identity whose result proves the delta",
    )
    actor: str = Field(
        min_length=1,
        max_length=256,
        description="Trusted actor identity recorded by the runtime boundary",
    )
    grants: list[str] = Field(
        description="Effective capability grant ids from the trusted runtime",
    )
    context_at: datetime = Field(
        description="Trusted runtime timestamp of the capture context",
    )


class LearnRequest(BaseModel):
    """One bounded learn request (D3/D5): at most
    :data:`MAX_FACTS_PER_LEARN_REQUEST` proposed deltas plus the required
    identity generation compare-and-set value. No handler names, actor
    identity or grant lists are accepted from the model."""

    model_config = _FORBID_EXTRA

    facts: list[ProposedFactDelta] = Field(
        description="Proposed fact deltas to persist for this service",
    )
    expected_generation: int = Field(
        ge=0,
        description="Required observed service generation for the identity CAS",
    )

    @field_validator("facts")
    @classmethod
    def _bounded_fact_count(
        cls, value: list[ProposedFactDelta]
    ) -> list[ProposedFactDelta]:
        if len(value) > MAX_FACTS_PER_LEARN_REQUEST:
            raise ValueError(
                f"a learn request carries at most "
                f"{MAX_FACTS_PER_LEARN_REQUEST} fact deltas"
            )
        return value


# ---------------------------------------------------------------------------
# compare-and-set envelopes (D3/D5)
# ---------------------------------------------------------------------------


class FactCas(BaseModel):
    """Per-fact compare-and-set correction envelope (D3): the correction
    names the exact fact revision it replaces — an omitted expected
    revision can never address a fact."""

    model_config = _FORBID_EXTRA

    fact_id: uuid.UUID = Field(
        description="Canonical UUID identity of the fact being corrected",
    )
    expected_revision: int = Field(
        ge=0,
        description="Exact current revision the correction is set against",
    )
    new_statement: str = Field(
        min_length=1,
        max_length=MAX_FACT_STATEMENT_CHARS,
        description="Bounded corrected statement replacing the fact statement",
    )


class ServiceGenerationCas(BaseModel):
    """Identity-level generation compare-and-set (D3/D5): the service
    generation fence that stale work must match before applying."""

    model_config = _FORBID_EXTRA

    expected_generation: int = Field(
        ge=0,
        description="Required observed service generation before effects apply",
    )


# ---------------------------------------------------------------------------
# selector union (D2)
# ---------------------------------------------------------------------------


class ServiceSelector(BaseModel):
    """Exactly-one-of service selector (D2): an application UUID, a
    compose project+directory pair, or a systemd unit name — never zero
    or two arms at once, and the compose arm requires BOTH its fields."""

    model_config = _FORBID_EXTRA

    application_id: uuid.UUID | None = Field(
        default=None,
        description="Canonical application identity when selecting by id",
    )
    compose_project: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Compose project name when selecting by compose deployment",
    )
    compose_directory: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Compose project directory backing the compose selection",
    )
    systemd_unit: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Systemd unit name when selecting by systemd unit",
    )

    @model_validator(mode="after")
    def _exactly_one_arm(self) -> ServiceSelector:
        arms = 0
        if self.application_id is not None:
            arms += 1
        compose_arm = (
            self.compose_project is not None or self.compose_directory is not None
        )
        if compose_arm:
            arms += 1
        if self.systemd_unit is not None:
            arms += 1
        if arms == 0:
            raise ValueError(
                "a service selector requires exactly one of: application_id, "
                "compose project+directory, or systemd_unit"
            )
        if compose_arm and (
            self.compose_project is None or self.compose_directory is None
        ):
            raise ValueError(
                "the compose selector requires both compose_project and "
                "compose_directory"
            )
        if arms > 1:
            raise ValueError(
                "a service selector accepts exactly one selection arm; "
                "multiple arms were provided"
            )
        return self
