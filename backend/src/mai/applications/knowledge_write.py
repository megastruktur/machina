"""Provenance-bound fact CAS, merge/conflict and retraction (ask_12,
campaign D3/D4, C4/C5).

The SINGLE mutation seam for learned facts (D4: every write goes through
this store — no second path). Every mutation is one transaction that:

1. validates provenance BEFORE any write: each proposed reference must
   resolve to a PERSISTED, SAME-SCOPE, SUCCESSFUL session entry
   (``session_entries`` ⋈ ``session_runs``) — a foreign server's tool
   result can never prove a fact here, and the DB owns ``verified_at``
   (``now()``), never the model;
2. enforces the identity generation compare-and-set (stale work NEVER
   applies) and the per-fact revision compare-and-set (corrections name
   the exact expected revision; never last-writer-wins);
3. merges unrelated keys, treats identical same-key proposals as
   IDEMPOTENT (no duplicate active or history row) and divergent
   same-key proposals as a CONFLICT (never silently applied) — both
   within one request and against stored ACTIVE facts;
4. refuses to exceed :data:`MAX_ACTIVE_FACTS_PER_SERVICE` active facts —
   never silently drops;
5. NEVER touches operator notes, the catalog runbook or the canonical
   map (that is the ``adopt`` seam's business);
6. stores only the bounded ``source_ref`` (the proposal's normalized
   references joined deterministically, clipped to the frozen 256-char
   column) and the bounded ``statement`` — no transcript/raw-output
   column exists or is written.

Retraction marks a fact ``retracted`` (never DELETE), bumps its
revision, and appends the history row. Every successful write appends
exactly one row to the append-only ``service_learned_fact_revision``
history.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Final

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.applications.knowledge_contracts import (
    FactCas,
    FactSourceKind,
    FactStatus,
    LearnRequest,
    MAX_ACTIVE_FACTS_PER_SERVICE,
    MAX_SOURCE_REF_CHARS,
    OperationStatus,
    ProposedFactDelta,
    SubEffectStatus,
    TrustedSourceContext,
    derive_operation_status,
    enforce_envelope_bound,
    validate_same_server_scope,
)

__all__ = [
    "FactApplyResult",
    "KnowledgeWriteStore",
    "ProvenanceRefusalError",
    "RetractionResult",
]

#: The sub-effect key this seam reports on.
_FACTS_EFFECT: Final[str] = "facts"

#: The persisted session-entry kind accepted as provenance per source
#: kind (the trusted runtime context decides which kind a claim is).
_PROVENANCE_ENTRY_KIND: Final[dict[str, str]] = {
    FactSourceKind.TOOL_RESULT.value: "tool_result",
    FactSourceKind.USER_STATEMENT.value: "user_message",
}

#: The successful delivery status a tool-result provenance row must carry.
_SUCCESS_STATUS: Final[str] = "executed"

#: Rejection codes (stable, value-free).
_CODE_REVISION_CONFLICT: Final[str] = "revision_conflict"
_CODE_CAPACITY_EXCEEDED: Final[str] = "capacity_exceeded"
_CODE_INVALID_PROVENANCE: Final[str] = "invalid_provenance"
_CODE_CONFLICT: Final[str] = "conflict"
_CODE_INVALID_REQUEST: Final[str] = "invalid_request"


class ProvenanceRefusalError(ValueError):
    """A provenance reference belongs to a run on a DIFFERENT server —
    the whole write is refused before ANY row is written (fail-closed,
    no partial write; a foreign tool result can never prove a fact
    here)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.code = "scope_mismatch"
        self.detail = detail


def _normalized_statement(statement: str) -> str:
    """Whitespace-normalized statement (the idempotence/conflict
    comparison form)."""
    return " ".join(statement.split())


def _normalized_refs(source_refs: list[str]) -> list[str]:
    """Deterministically ordered, de-duplicated references."""
    return sorted({ref.strip() for ref in source_refs})


def _bounded_source_ref(source_refs: list[str]) -> str:
    """The bounded stored provenance reference: the proposal's
    normalized references joined deterministically; when the join would
    exceed the frozen 256-char column, only the first (sorted)
    reference is stored. Never a payload — always the persisted
    reference identity."""
    joined = ",".join(_normalized_refs(source_refs))
    if len(joined) <= MAX_SOURCE_REF_CHARS:
        return joined
    return _normalized_refs(source_refs)[0][:MAX_SOURCE_REF_CHARS]


class FactApplyResult(BaseModel):
    """The frozen-shape result of one seam mutation (fail-closed
    envelope, < 64 KiB, honest derived status)."""

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(
        description="Authorized server identity the mutation applied to",
    )
    application_id: uuid.UUID = Field(
        description="Canonical application identity the mutation applied to",
    )
    generation: int = Field(
        description="Identity generation the mutation was set against "
        "(-1 when no identity exists yet)",
    )
    applied_fact_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Fact identities written by this mutation",
    )
    superseded_fact_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Prior fact identities superseded by corrections",
    )
    idempotent_fact_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Proposed facts already persisted identically (no-op)",
    )
    conflict_keys: list[str] = Field(
        default_factory=list,
        description="Semantic keys refused as divergent conflicts",
    )
    retracted_fact_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Fact identities retracted by this mutation",
    )
    revisions_written: int = Field(
        default=0,
        description="Append-only history rows written by this mutation",
    )
    status: OperationStatus = Field(
        description="Honest derived status (partial and unknown stay distinct)",
    )
    error_code: str | None = Field(
        default=None,
        description="Stable machine-readable refusal code when refused",
    )
    error_detail: str | None = Field(
        default=None,
        description="Value-free bounded refusal detail when refused",
    )


class RetractionResult(BaseModel):
    """The frozen-shape result of one retraction through the seam."""

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(
        description="Authorized server identity the retraction applied to",
    )
    application_id: uuid.UUID = Field(
        description="Canonical application identity the retraction applied to",
    )
    generation: int = Field(
        description="Identity generation the retraction was set against "
        "(-1 when no identity exists yet)",
    )
    fact_id: uuid.UUID = Field(
        description="Retracted fact identity",
    )
    new_revision: int = Field(
        description="Revision the fact carries after the retraction "
        "(0 when the retraction was refused)",
    )
    status: OperationStatus = Field(
        description="Honest derived status of the retraction",
    )
    error_code: str | None = Field(
        default=None,
        description="Stable machine-readable refusal code when refused",
    )
    error_detail: str | None = Field(
        default=None,
        description="Value-free bounded refusal detail when refused",
    )


class KnowledgeWriteStore:
    """The single mutation seam for learned facts over the 0018 tables
    (async session factory injected; one transaction per mutation)."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # -- provenance validation (inside the write transaction, before
    #    ANY insert) ----------------------------------------------------------

    async def _validate_provenance(
        self,
        session: AsyncSession,
        trusted: TrustedSourceContext,
        provenance: Mapping[str, list[str]],
    ) -> str | None:
        """Validate every proposed provenance reference against the
        persisted session log. Returns ``None`` when every reference
        resolves to a persisted, same-scope, successful entry of the
        required kind; otherwise a stable refusal code."""
        wanted: dict[str, set[str]] = {}
        for source_kind, refs in provenance.items():
            required_kind = _PROVENANCE_ENTRY_KIND[source_kind]
            for ref in refs:
                try:
                    canonical = str(uuid.UUID(ref.strip()))
                except ValueError:
                    return _CODE_INVALID_PROVENANCE
                wanted.setdefault(canonical, set()).add(required_kind)
        if not wanted:
            return _CODE_INVALID_PROVENANCE
        # One persisted entry has exactly ONE kind: a reference claimed
        # under two different source kinds can never satisfy both.
        for required_kinds in wanted.values():
            if len(required_kinds) > 1:
                return _CODE_INVALID_PROVENANCE
        rows = (
            await session.execute(
                sa.text(
                    "SELECT e.entry_id, e.kind, e.payload->>'status' AS "
                    "status, r.server_id FROM session_entries e "
                    "JOIN session_runs r ON e.run_id = r.run_id "
                    "WHERE e.entry_id = ANY(:refs)"
                ),
                {"refs": sorted(wanted)},
            )
        ).mappings().all()
        found = {str(row["entry_id"]): row for row in rows}
        for ref, required_kinds in wanted.items():
            row = found.get(ref)
            if row is None:
                return _CODE_INVALID_PROVENANCE
            required_kind = next(iter(required_kinds))
            if str(row["kind"]) != required_kind:
                return _CODE_INVALID_PROVENANCE
            if uuid.UUID(str(row["server_id"])) != trusted.server_id:
                return "scope_mismatch"
            if (
                required_kind == "tool_result"
                and str(row["status"]) != _SUCCESS_STATUS
            ):
                return _CODE_INVALID_PROVENANCE
        return None

    # -- identity fence -------------------------------------------------------

    async def _identity_generation(
        self,
        session: AsyncSession,
        *,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
    ) -> int | None:
        """The locked current generation (``FOR UPDATE`` serializes
        every mutation for one identity)."""
        row = (
            await session.execute(
                sa.text(
                    "SELECT generation FROM service_knowledge_identity "
                    "WHERE server_id = :s AND application_id = :a "
                    "FOR UPDATE"
                ),
                {"s": server_id, "a": application_id},
            )
        ).first()
        return None if row is None else int(row[0])

    @staticmethod
    def _refusal_kwargs(code: str, detail: str) -> dict[str, object]:
        return {
            "status": OperationStatus.FAILED,
            "error_code": code,
            "error_detail": detail[:400],
        }

    # -- apply (merge/conflict + generation CAS) ------------------------------

    async def apply_facts(
        self,
        *,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        request: LearnRequest,
        trusted: TrustedSourceContext,
        expected_generation: int,
    ) -> FactApplyResult:
        """Apply one bounded learn request through the single mutation
        seam. Provenance is validated before any write; the generation
        CAS must match exactly (an absent identity is created at
        generation 1 — stale work never applies and never fabricates an
        identity row); unrelated (category, key) identities merge,
        identical same-key proposals are idempotent, divergent same-key
        proposals (within the request or against a stored ACTIVE fact)
        are refused as conflicts; capacity is explicit. Operator notes
        and the catalog runbook are never touched."""
        validate_same_server_scope(server_id, trusted.server_id)
        if request.expected_generation != expected_generation:
            raise ValueError(
                "request.expected_generation does not match the "
                "expected_generation CAS argument"
            )
        base: dict[str, object] = {
            "server_id": server_id,
            "application_id": application_id,
            "generation": -1,
            "retracted_fact_ids": [],
        }

        async with self._sessions() as session:
            async with session.begin():
                # 1. Provenance BEFORE any write (fail-closed).
                provenance: dict[str, list[str]] = {}
                for delta in request.facts:
                    refs = _normalized_refs(delta.source_refs)
                    existing = provenance.get(delta.source_kind.value, [])
                    provenance[delta.source_kind.value] = sorted(
                        {*existing, *refs}
                    )
                provenance_failure = await self._validate_provenance(
                    session, trusted, provenance
                )
                if provenance_failure == "scope_mismatch":
                    raise ProvenanceRefusalError(
                        "a provenance reference belongs to a run on a "
                        "different server; a foreign tool result can never "
                        "prove a fact here"
                    )
                if provenance_failure is not None:
                    kwargs = base | self._refusal_kwargs(
                        provenance_failure,
                        "a provenance reference did not resolve to a "
                        "persisted, same-scope, successful session entry",
                    )
                    result = FactApplyResult(**kwargs)
                    enforce_envelope_bound(result)
                    return result

                # 2. Generation CAS (row locked). An absent identity is
                #    created at generation 1 — but ONLY when the caller
                #    observed exactly that generation: stale work never
                #    applies and never fabricates an identity row.
                current = await self._identity_generation(
                    session,
                    server_id=server_id,
                    application_id=application_id,
                )
                if current is None:
                    if expected_generation != 1:
                        kwargs = base | self._refusal_kwargs(
                            _CODE_REVISION_CONFLICT,
                            f"expected generation {expected_generation} does "
                            "not match the absent identity's first "
                            "generation 1; stale work never applies",
                        )
                        result = FactApplyResult(**kwargs)
                        enforce_envelope_bound(result)
                        return result
                    await session.execute(
                        sa.text(
                            "INSERT INTO service_knowledge_identity "
                            "(server_id, application_id, generation) VALUES "
                            "(:s, :a, 1)"
                        ),
                        {"s": server_id, "a": application_id},
                    )
                    current = 1
                generation = current
                if expected_generation != generation:
                    kwargs = base | {
                        "generation": generation
                    } | self._refusal_kwargs(
                        _CODE_REVISION_CONFLICT,
                        f"expected generation {expected_generation} does not "
                        f"match the current generation {generation}; stale "
                        "work never applies",
                    )
                    result = FactApplyResult(**kwargs)
                    enforce_envelope_bound(result)
                    return result

                # 3. Same-(category, key) analysis within the request:
                #    the active-key identity is (category, key) — the
                #    same key under a different category is a DISTINCT
                #    fact (matching uq_service_learned_fact_active_key).
                grouped: dict[tuple[str, str], list[ProposedFactDelta]] = {}
                for delta in request.facts:
                    grouped.setdefault(
                        (delta.category, delta.key), []
                    ).append(delta)
                conflict_keys: list[str] = []
                conflicted_cat_keys: set[tuple[str, str]] = set()
                plan: list[ProposedFactDelta] = []
                request_deduped_keys: set[tuple[str, str]] = set()
                for cat_key in sorted(grouped):
                    deltas = grouped[cat_key]
                    first = deltas[0]
                    identical = all(
                        _normalized_statement(other.statement)
                        == _normalized_statement(first.statement)
                        and other.source_kind is first.source_kind
                        and _normalized_refs(other.source_refs)
                        == _normalized_refs(first.source_refs)
                        for other in deltas[1:]
                    )
                    if not identical:
                        conflicted_cat_keys.add(cat_key)
                        continue
                    if len(deltas) > 1:
                        request_deduped_keys.add(cat_key)
                    plan.append(first)

                # 4. Per-(category, key) CAS against stored ACTIVE facts.
                active_rows = (
                    await session.execute(
                        sa.text(
                            "SELECT fact_id, category, key, statement FROM "
                            "service_learned_fact WHERE server_id = :s AND "
                            "application_id = :a AND generation = :g AND "
                            "status = 'active' FOR UPDATE"
                        ),
                        {"s": server_id, "a": application_id, "g": generation},
                    )
                ).mappings().all()
                active_by_cat_key = {
                    (str(row["category"]), str(row["key"])): row
                    for row in active_rows
                }

                applied: list[uuid.UUID] = []
                idempotent: list[uuid.UUID] = []
                new_rows: list[dict[str, object]] = []
                for delta in sorted(
                    plan, key=lambda item: (item.category, item.key)
                ):
                    cat_key = (delta.category, delta.key)
                    if cat_key in conflicted_cat_keys:  # pragma: no cover
                        continue
                    stored = active_by_cat_key.get(cat_key)
                    if stored is not None:
                        if _normalized_statement(
                            str(stored["statement"])
                        ) == _normalized_statement(delta.statement):
                            idempotent.append(
                                uuid.UUID(str(stored["fact_id"]))
                            )
                            continue
                        # Divergent same-key value against a stored ACTIVE
                        # fact: a CONFLICT — corrections go through the
                        # per-fact CAS, never last-writer-wins.
                        conflicted_cat_keys.add(cat_key)
                        continue
                    new_id = uuid.uuid4()
                    new_rows.append(
                        {
                            "fact_id": new_id,
                            "s": server_id,
                            "a": application_id,
                            "g": generation,
                            "c": delta.category,
                            "k": delta.key,
                            "st": _normalized_statement(delta.statement),
                            "ref": _bounded_source_ref(delta.source_refs),
                            "sk": delta.source_kind.value,
                            "sess": trusted.session_id,
                            "call": trusted.tool_call_id,
                            "key": delta.key,
                        }
                    )
                    applied.append(new_id)

                # 5. Capacity is explicit (never silently dropped).
                if new_rows:
                    active_count = len(active_by_cat_key)
                    if (
                        active_count + len(new_rows)
                        > MAX_ACTIVE_FACTS_PER_SERVICE
                    ):
                        kwargs = base | {
                            "generation": generation
                        } | self._refusal_kwargs(
                            _CODE_CAPACITY_EXCEEDED,
                            f"applying this request would exceed "
                            f"{MAX_ACTIVE_FACTS_PER_SERVICE} active facts; "
                            "facts are never silently dropped",
                        )
                        result = FactApplyResult(**kwargs)
                        enforce_envelope_bound(result)
                        return result

                # 6. Writes: one fact row + one append-only history row
                #    per applied fact (DB owns verified_at = now()).
                for row in new_rows:
                    await session.execute(
                        sa.text(
                            "INSERT INTO service_learned_fact (fact_id, "
                            "server_id, application_id, generation, "
                            "category, key, statement, revision, status, "
                            "source_kind, source_ref, source_run_id, "
                            "source_session_id, source_tool_call_id, "
                            "verified_at) VALUES (:fact_id, :s, :a, :g, "
                            ":c, :k, :st, 1, 'active', :sk, :ref, :run, "
                            ":sess, :call, now())"
                        ),
                        row | {"run": trusted.run_id},
                    )
                    await session.execute(
                        sa.text(
                            "INSERT INTO service_learned_fact_revision "
                            "(revision_id, fact_id, server_id, "
                            "application_id, revision, statement, status, "
                            "source_kind, source_ref, changed_at) VALUES "
                            "(:rid, :fact_id, :s, :a, 1, :st, 'active', "
                            ":sk, :ref, now())"
                        ),
                        row | {"rid": uuid.uuid4()},
                    )
                    if (row["c"], row["k"]) in request_deduped_keys:
                        idempotent.append(uuid.UUID(str(row["fact_id"])))

                # 7. Honest derived status (partial stays distinct);
                #    conflict_keys report the semantic key (deduped).
                conflict_keys = sorted(
                    {key for _cat, key in conflicted_cat_keys}
                )
                sub_effects: dict[str, SubEffectStatus] = {}
                if applied or idempotent:
                    sub_effects[_FACTS_EFFECT] = SubEffectStatus.APPLIED
                if conflict_keys:
                    sub_effects["conflicts"] = SubEffectStatus.FAILED
                kwargs = base | {
                    "generation": generation,
                    "applied_fact_ids": applied,
                    "idempotent_fact_ids": idempotent,
                    "conflict_keys": conflict_keys,
                    "revisions_written": len(new_rows),
                    "status": derive_operation_status(sub_effects)
                    if sub_effects
                    else OperationStatus.APPLYING,
                }
                if conflict_keys and not applied:
                    kwargs |= self._refusal_kwargs(
                        _CODE_CONFLICT,
                        "divergent same-key proposals were refused; correct "
                        "through the per-fact CAS with the expected revision",
                    )
                result = FactApplyResult(**kwargs)
                enforce_envelope_bound(result)
                return result

    # -- per-fact CAS correction (FactCas envelope) ---------------------------

    async def correct_fact(
        self,
        *,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        cas: FactCas,
        trusted: TrustedSourceContext,
        expected_generation: int,
    ) -> FactApplyResult:
        """Per-fact compare-and-set correction (the ``FactCas``
        envelope): the stored revision must equal the expected revision —
        the old row becomes ``superseded``, a NEW row with
        ``revision = N+1`` and ``supersedes_fact_id`` is inserted, and
        the append-only history gains one row. Never last-writer-wins."""
        validate_same_server_scope(server_id, trusted.server_id)
        base: dict[str, object] = {
            "server_id": server_id,
            "application_id": application_id,
            "generation": -1,
            "retracted_fact_ids": [],
        }
        async with self._sessions() as session:
            async with session.begin():
                current = await self._identity_generation(
                    session,
                    server_id=server_id,
                    application_id=application_id,
                )
                if current is None:
                    kwargs = base | self._refusal_kwargs(
                        _CODE_REVISION_CONFLICT,
                        "no knowledge identity exists for this service yet",
                    )
                    result = FactApplyResult(**kwargs)
                    enforce_envelope_bound(result)
                    return result
                generation = current
                if expected_generation != generation:
                    kwargs = base | {
                        "generation": generation
                    } | self._refusal_kwargs(
                        _CODE_REVISION_CONFLICT,
                        f"expected generation {expected_generation} does not "
                        f"match the current generation {generation}",
                    )
                    result = FactApplyResult(**kwargs)
                    enforce_envelope_bound(result)
                    return result
                row = (
                    await session.execute(
                        sa.text(
                            "SELECT revision, status, category, key, "
                            "source_kind, source_ref FROM "
                            "service_learned_fact WHERE fact_id = :f AND "
                            "server_id = :s AND application_id = :a AND "
                            "generation = :g FOR UPDATE"
                        ),
                        {
                            "f": cas.fact_id,
                            "s": server_id,
                            "a": application_id,
                            "g": generation,
                        },
                    )
                ).mappings().first()
                if row is None:
                    kwargs = base | {
                        "generation": generation
                    } | self._refusal_kwargs(
                        _CODE_INVALID_REQUEST,
                        "the corrected fact does not exist in this generation",
                    )
                    result = FactApplyResult(**kwargs)
                    enforce_envelope_bound(result)
                    return result
                stored_revision = int(row["revision"])
                if stored_revision != cas.expected_revision:
                    kwargs = base | {
                        "generation": generation
                    } | self._refusal_kwargs(
                        _CODE_REVISION_CONFLICT,
                        f"expected revision {cas.expected_revision} does not "
                        f"match the stored revision {stored_revision}; never "
                        "last-writer-wins",
                    )
                    result = FactApplyResult(**kwargs)
                    enforce_envelope_bound(result)
                    return result
                if str(row["status"]) != FactStatus.ACTIVE.value:
                    kwargs = base | {
                        "generation": generation
                    } | self._refusal_kwargs(
                        _CODE_INVALID_REQUEST,
                        "only ACTIVE facts can be corrected",
                    )
                    result = FactApplyResult(**kwargs)
                    enforce_envelope_bound(result)
                    return result
                new_id = uuid.uuid4()
                new_revision = stored_revision + 1
                statement = _normalized_statement(cas.new_statement)
                # Supersede FIRST: the partial unique active-key index
                # admits the replacement row only once the old row has
                # left the active state.
                await session.execute(
                    sa.text(
                        "UPDATE service_learned_fact SET status = "
                        "'superseded', updated_at = now() WHERE fact_id = :f"
                    ),
                    {"f": cas.fact_id},
                )
                await session.execute(
                    sa.text(
                        "INSERT INTO service_learned_fact (fact_id, "
                        "server_id, application_id, generation, category, "
                        "key, statement, revision, status, source_kind, "
                        "source_ref, source_run_id, source_session_id, "
                        "source_tool_call_id, supersedes_fact_id, "
                        "verified_at) SELECT :nf, server_id, "
                        "application_id, generation, category, key, :st, "
                        ":r, 'active', source_kind, source_ref, :run, "
                        "source_session_id, :call, fact_id, now() FROM "
                        "service_learned_fact WHERE fact_id = :f"
                    ),
                    {
                        "nf": new_id,
                        "f": cas.fact_id,
                        "st": statement,
                        "r": new_revision,
                        "run": trusted.run_id,
                        "call": trusted.tool_call_id,
                    },
                )
                await session.execute(
                    sa.text(
                        "INSERT INTO service_learned_fact_revision "
                        "(revision_id, fact_id, server_id, application_id, "
                        "revision, statement, status, source_kind, "
                        "source_ref, changed_at) SELECT :rid, fact_id, "
                        "server_id, application_id, :r, :st, 'active', "
                        "source_kind, source_ref, now() FROM "
                        "service_learned_fact WHERE fact_id = :nf"
                    ),
                    {
                        "rid": uuid.uuid4(),
                        "nf": new_id,
                        "r": new_revision,
                        "st": statement,
                    },
                )
                result = FactApplyResult(
                    server_id=server_id,
                    application_id=application_id,
                    generation=generation,
                    applied_fact_ids=[new_id],
                    superseded_fact_ids=[cas.fact_id],
                    conflict_keys=[],
                    retracted_fact_ids=[],
                    revisions_written=1,
                    status=derive_operation_status(
                        {_FACTS_EFFECT: SubEffectStatus.APPLIED}
                    ),
                )
                enforce_envelope_bound(result)
                return result

    # -- retraction ------------------------------------------------------------

    async def retract_fact(
        self,
        *,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        fact_id: uuid.UUID,
        expected_revision: int,
        expected_generation: int,
        reason: str | None = None,
    ) -> RetractionResult:
        """Retract one fact by compare-and-set: the fact is marked
        ``retracted`` (NEVER deleted) with its revision bumped, and one
        ``retracted`` history row is appended. Stale revisions or
        generations are refused. ``reason`` is accepted and bounded
        (<= 400 chars, value-free) but intentionally NOT persisted: the
        0018 schema reserves ``conflict_reason`` for the conflict path,
        and no transcript-adjacent column exists on purpose — the
        retraction is fully described by the status change plus the
        appended history row."""
        if reason is not None and len(reason) > 400:
            raise ValueError("the retraction reason is bounded to 400 chars")
        base: dict[str, object] = {
            "server_id": server_id,
            "application_id": application_id,
            "generation": -1,
            "fact_id": fact_id,
            "new_revision": 0,
        }
        async with self._sessions() as session:
            async with session.begin():
                current = await self._identity_generation(
                    session,
                    server_id=server_id,
                    application_id=application_id,
                )
                if current is None:
                    kwargs = base | self._refusal_kwargs(
                        _CODE_REVISION_CONFLICT,
                        "no knowledge identity exists for this service yet",
                    )
                    result = RetractionResult(**kwargs)
                    enforce_envelope_bound(result)
                    return result
                generation = current
                if expected_generation != generation:
                    kwargs = base | {
                        "generation": generation
                    } | self._refusal_kwargs(
                        _CODE_REVISION_CONFLICT,
                        f"expected generation {expected_generation} does not "
                        f"match the current generation {generation}",
                    )
                    result = RetractionResult(**kwargs)
                    enforce_envelope_bound(result)
                    return result
                row = (
                    await session.execute(
                        sa.text(
                            "SELECT revision FROM service_learned_fact "
                            "WHERE fact_id = :f AND server_id = :s AND "
                            "application_id = :a AND generation = :g "
                            "FOR UPDATE"
                        ),
                        {
                            "f": fact_id,
                            "s": server_id,
                            "a": application_id,
                            "g": generation,
                        },
                    )
                ).first()
                if row is None:
                    kwargs = base | {
                        "generation": generation
                    } | self._refusal_kwargs(
                        _CODE_INVALID_REQUEST,
                        "the retracted fact does not exist in this generation",
                    )
                    result = RetractionResult(**kwargs)
                    enforce_envelope_bound(result)
                    return result
                stored_revision = int(row[0])
                if stored_revision != expected_revision:
                    kwargs = base | {
                        "generation": generation
                    } | self._refusal_kwargs(
                        _CODE_REVISION_CONFLICT,
                        f"expected revision {expected_revision} does not "
                        f"match the stored revision {stored_revision}",
                    )
                    result = RetractionResult(**kwargs)
                    enforce_envelope_bound(result)
                    return result
                new_revision = stored_revision + 1
                await session.execute(
                    sa.text(
                        "UPDATE service_learned_fact SET status = "
                        "'retracted', revision = :r, updated_at = now() "
                        "WHERE fact_id = :f"
                    ),
                    {"r": new_revision, "f": fact_id},
                )
                await session.execute(
                    sa.text(
                        "INSERT INTO service_learned_fact_revision "
                        "(revision_id, fact_id, server_id, application_id, "
                        "revision, statement, status, source_kind, "
                        "source_ref, changed_at) SELECT :rid, fact_id, "
                        "server_id, application_id, :r, statement, "
                        "'retracted', source_kind, source_ref, now() FROM "
                        "service_learned_fact WHERE fact_id = :f"
                    ),
                    {"rid": uuid.uuid4(), "r": new_revision, "f": fact_id},
                )
                result = RetractionResult(
                    server_id=server_id,
                    application_id=application_id,
                    generation=generation,
                    fact_id=fact_id,
                    new_revision=new_revision,
                    status=derive_operation_status(
                        {_FACTS_EFFECT: SubEffectStatus.APPLIED}
                    ),
                )
                enforce_envelope_bound(result)
                return result
