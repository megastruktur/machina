"""Canonical ``objective.*`` activity ledger store (journal-store slice).

ONE writer/reader for the objective-scoped activity ledger of
runtime-v2.md §5.1/§14 — the same envelope discipline the per-run
``job_events`` log applies (C05 rule 4), extended with the §4 actor-kind
provenance field:

- **Transactional consistency (AC2):** event writes happen INSIDE the
  authoritative transition's transaction. The session-level
  :func:`aappend_events` runs under the caller's transaction while the
  caller holds ``SELECT ... FOR UPDATE`` on the objective row (the same
  serialization discipline the conversation store applies for message
  ``seq`` allocation); the sequence cursor is ``MAX(seq) + 1`` and the
  ``UNIQUE (objective_id, seq)`` constraint is the backstop. A transition
  that fails rolls its events back with it — the ledger can never
  contradict store state.
- **Redaction before persistence (§5.3, AC2):** every string value is
  projected through :func:`mai.agents.output_safety.project_text` with the
  frozen 4096-char per-text bound, and the serialized payload must fit the
  frozen 32768-char whole-payload bound or the append fails closed
  (oversized raw content belongs behind a safe ``content_ref``, not in a
  silently corrupted typed payload). Secrets never enter payloads.
- **Deterministic pagination (§5.2, AGENTS.md):** every read is a bounded
  page — ``limit``/``offset``/``total``/``has_more`` with 50/200 bounds,
  ``ORDER BY seq`` under the identical filter predicate, ``COUNT(*)`` under
  the same predicate, and an ``after_seq`` replay cursor for SSE-reconnect
  deduplication (a replay is read-only).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents.output_safety import REDACTED, project_text
from mai.contracts.conversations import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from mai.contracts.objectives import (
    EVENT_PAYLOAD_MAX_CHARS,
    EVENT_TEXT_MAX_CHARS,
    ACTOR_KINDS,
    OBJECTIVE_EVENT_PAYLOAD_VERSION,
    OBJECTIVE_EVENT_PREFIX,
    ActorKind,
    ObjectiveContractError,
    ObjectiveErrorCode,
    ObjectiveEvent,
    ObjectiveEventPage,
    ObjectiveFieldError,
    assert_objective_scope,
    compute_has_more,
)

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "EVENT_TEXT_MAX_CHARS",
    "EVENT_PAYLOAD_MAX_CHARS",
    "ObjectiveEventDraft",
    "aappend_event",
    "aappend_events",
    "alatest_seq",
    "alist_events",
    "project_event_text",
]


@dataclass(frozen=True, slots=True)
class ObjectiveEventDraft:
    """One event to append: type, provenance and its value-free payload."""

    type: str
    actor_kind: ActorKind
    data: Mapping[str, object]
    run_id: uuid.UUID | None = None
    v: int = OBJECTIVE_EVENT_PAYLOAD_VERSION


_OBJECTIVE_COLUMNS: Final[str] = (
    "objective_id, server_id, client_request_id, content_fingerprint, "
    "title, original_request, constraints, status, revision, budget, "
    "created_at, updated_at"
)


def _not_found() -> ObjectiveContractError:
    """The single closed error for a missing/out-of-scope objective."""
    return ObjectiveContractError(
        [
            ObjectiveFieldError(
                ObjectiveErrorCode.OBJECTIVE_NOT_FOUND.value,
                "objective_id",
                "no objective in the requesting server scope",
            )
        ]
    )


def project_event_text(value: str) -> tuple[str, bool]:
    """Project one raw text to its persistable event form (§5.3).

    Redaction runs BEFORE the bound (a cut can never split a secret);
    returns the safe text and whether the redaction marker is present.
    """
    safe = project_text(value, max_chars=EVENT_TEXT_MAX_CHARS)
    return safe, REDACTED in safe


def project_event_data(data: Mapping[str, object]) -> dict[str, object]:
    """Deeply project one event payload (§5.3; AC2).

    Every string value is redacted then bounded at
    :data:`EVENT_TEXT_MAX_CHARS`; mappings/lists recurse (depth-bounded);
    everything else must be JSON-scalar shaped. The serialized result must
    fit :data:`EVENT_PAYLOAD_MAX_CHARS` — exceeding it is the structured
    ``event_too_large`` rejection, never a silent cut.
    """
    projected = _project_value(data, depth=0)
    serialized = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > EVENT_PAYLOAD_MAX_CHARS:
        raise ObjectiveContractError(
            [
                ObjectiveFieldError(
                    ObjectiveErrorCode.EVENT_TOO_LARGE.value,
                    "data",
                    f"event payload exceeds {EVENT_PAYLOAD_MAX_CHARS} "
                    "projected chars; store raw content behind a safe "
                    "content_ref instead",
                )
            ]
        )
    return projected


def _project_value(value: object, depth: int) -> object:
    if depth > 8:
        raise ValueError("event payload nesting exceeds the depth bound")
    if isinstance(value, str):
        return project_text(value, max_chars=EVENT_TEXT_MAX_CHARS)
    if isinstance(value, bool) or value is None or isinstance(value, int | float):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _project_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_project_value(item, depth + 1) for item in value]
    raise ValueError(f"event payload carries non-scalar value of {type(value)!r}")


def _validate_draft(draft: ObjectiveEventDraft) -> None:
    if not isinstance(draft.type, str) or not draft.type.startswith(
        OBJECTIVE_EVENT_PREFIX
    ):
        raise ValueError(
            f"event type must stay inside the {OBJECTIVE_EVENT_PREFIX!r} family"
        )
    if draft.type != draft.type.strip() or ".." in draft.type:
        raise ValueError("event type must be a clean dotted slug")
    if draft.actor_kind not in ACTOR_KINDS:
        raise ValueError("actor_kind must be an ActorKind value")
    if not isinstance(draft.v, int) or draft.v < 1:
        raise ValueError("v must be an integer >= 1")
    if not isinstance(draft.data, Mapping):
        raise ValueError("data must be a mapping")
    if draft.run_id is not None and not isinstance(draft.run_id, uuid.UUID):
        raise ValueError("run_id must be a UUID when set")


async def alocked_objective(
    session: AsyncSession, objective_id: uuid.UUID
) -> Mapping[str, object]:
    """Fetch one objective row under ``FOR UPDATE`` (seq serialization).

    Raises the closed ``objective_not_found`` error when the row does not
    exist; scope is checked by the caller against the request's server.
    """
    row = (
        await session.execute(
            text(
                f"SELECT {_OBJECTIVE_COLUMNS} FROM objectives "
                "WHERE objective_id = :oid FOR UPDATE"
            ),
            {"oid": objective_id},
        )
    ).mappings().first()
    if row is None:
        raise _not_found()
    return row


async def ascope_server_rows(
    session: AsyncSession, objective_id: uuid.UUID
) -> list[Mapping[str, object]]:
    """The objective's explicit in-scope membership rows (ordered)."""
    rows = (
        await session.execute(
            text(
                "SELECT server_id, added_by_actor_kind, added_at "
                "FROM objective_servers WHERE objective_id = :oid "
                "ORDER BY added_at, server_id"
            ),
            {"oid": objective_id},
        )
    ).mappings().all()
    return list(rows)


async def achecked_objective(
    session: AsyncSession,
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    for_update: bool = False,
) -> tuple[Mapping[str, object], list[Mapping[str, object]]]:
    """Scope-checked objective row (+ membership rows) for all v2 stores.

    The C8 discipline (runtime-v2.md §5.2 reuse of
    ``assert_objective_scope``): a request whose server is not an explicit
    scope member fails CLOSED with ``objective_not_found`` — existence
    never leaks. ``for_update`` takes the row lock that serializes ledger
    sequence allocation and revision CAS.
    """
    if for_update:
        row: Mapping[str, object] | None = await alocked_objective(
            session, objective_id
        )
    else:
        row = (
            await session.execute(
                text(
                    f"SELECT {_OBJECTIVE_COLUMNS} FROM objectives "
                    "WHERE objective_id = :oid"
                ),
                {"oid": objective_id},
            )
        ).mappings().first()
        if row is None:
            raise _not_found()
    scope_rows = await ascope_server_rows(session, objective_id)
    scope_ids = {uuid.UUID(str(r["server_id"])) for r in scope_rows}
    assert_objective_scope(server_id, scope_ids)
    assert row is not None  # alocked_objective already raised otherwise
    return row, scope_rows


async def aappend_events(
    session: AsyncSession,
    objective_id: uuid.UUID,
    drafts: Sequence[ObjectiveEventDraft],
) -> list[int]:
    """Append events at the next whole-objective cursors (caller's txn).

    The CALLER holds the ``job_runs``-style objective row lock in its own
    transaction (the lifecycle's shared-producer contract): the cursor is
    ``MAX(seq) + 1`` and rows land with the frozen envelope columns.
    Failure raises: a lost event is never swallowed as a recorded success,
    and a failed transition rolls its events back (AC2). Returns the
    allocated sequences in order.
    """
    sequences: list[int] = []
    for draft in drafts:
        _validate_draft(draft)
        projected = project_event_data(draft.data)
        next_seq = int(
            (
                await session.execute(
                    text(
                        "SELECT COALESCE(MAX(seq), 0) + 1 FROM objective_events "
                        "WHERE objective_id = :oid"
                    ),
                    {"oid": objective_id},
                )
            ).scalar_one()
        )
        await session.execute(
            text(
                """
                INSERT INTO objective_events
                    (event_id, objective_id, seq, run_id, ts, type,
                     actor_kind, v, data)
                VALUES
                    (:eid, :oid, :seq, :run, now(), :type, :actor, :v,
                     CAST(:data AS jsonb))
                """
            ),
            {
                "eid": uuid.uuid4(),
                "oid": objective_id,
                "seq": next_seq,
                "run": draft.run_id,
                "type": draft.type,
                "actor": draft.actor_kind.value,
                "v": draft.v,
                "data": json.dumps(projected, ensure_ascii=False),
            },
        )
        sequences.append(next_seq)
    return sequences


async def aappend_event(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    draft: ObjectiveEventDraft,
) -> ObjectiveEvent:
    """Standalone event append in its own transaction (scope-checked).

    Locks the objective row, verifies the requesting server is in the
    objective's scope (fail closed), appends the projected event, and
    returns the parsed ledger row. For events that belong to an
    authoritative transition use the transition's store function instead —
    this seam is for producer-side appends that ARE their own transition.
    """
    _validate_draft(draft)
    projected = project_event_data(draft.data)
    event_id = uuid.uuid4()
    async with sessions() as session, session.begin():
        row = await alocked_objective(session, objective_id)
        scope = await _ascope_ids(session, objective_id)
        assert_objective_scope(server_id, scope | {row["server_id"]})  # type: ignore[arg-type]
        next_seq = int(
            (
                await session.execute(
                    text(
                        "SELECT COALESCE(MAX(seq), 0) + 1 FROM objective_events "
                        "WHERE objective_id = :oid"
                    ),
                    {"oid": objective_id},
                )
            ).scalar_one()
        )
        inserted = (
            await session.execute(
                text(
                    """
                    INSERT INTO objective_events
                        (event_id, objective_id, seq, run_id, ts, type,
                         actor_kind, v, data)
                    VALUES
                        (:eid, :oid, :seq, :run, now(), :type, :actor, :v,
                         CAST(:data AS jsonb))
                    RETURNING event_id, objective_id, seq, run_id, ts, type,
                              actor_kind, v, data
                    """
                ),
                {
                    "eid": event_id,
                    "oid": objective_id,
                    "seq": next_seq,
                    "run": draft.run_id,
                    "type": draft.type,
                    "actor": draft.actor_kind.value,
                    "v": draft.v,
                    "data": json.dumps(projected, ensure_ascii=False),
                },
            )
        ).mappings().one()
        await session.execute(
            text(
                "UPDATE objectives SET updated_at = now() "
                "WHERE objective_id = :oid"
            ),
            {"oid": objective_id},
        )
    return _row_to_event(inserted)


async def _ascope_ids(
    session: AsyncSession, objective_id: uuid.UUID
) -> set[uuid.UUID]:
    rows = (
        await session.execute(
            text(
                "SELECT server_id FROM objective_servers "
                "WHERE objective_id = :oid"
            ),
            {"oid": objective_id},
        )
    ).scalars().all()
    return {uuid.UUID(str(value)) for value in rows}


def _row_to_event(row: Mapping[str, object]) -> ObjectiveEvent:
    return ObjectiveEvent(
        event_id=row["event_id"],  # type: ignore[arg-type]
        objective_id=row["objective_id"],  # type: ignore[arg-type]
        sequence=int(row["seq"]),  # type: ignore[arg-type]
        ts=row["ts"],  # type: ignore[arg-type]
        type=str(row["type"]),
        actor_kind=ActorKind(str(row["actor_kind"])),
        v=int(row["v"]),  # type: ignore[arg-type]
        data=dict(row["data"]),  # type: ignore[arg-type]
        run_id=row["run_id"],  # type: ignore[arg-type]
    )


async def alatest_seq(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> int:
    """The objective's current ledger cursor (SSE reconnect baseline)."""
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT server_id FROM objectives "
                    "WHERE objective_id = :oid"
                ),
                {"oid": objective_id},
            )
        ).mappings().first()
        if row is None:
            raise _not_found()
        scope = await _ascope_ids(session, objective_id)
        assert_objective_scope(server_id, scope | {row["server_id"]})
        latest = (
            await session.execute(
                text(
                    "SELECT COALESCE(MAX(seq), 0) FROM objective_events "
                    "WHERE objective_id = :oid"
                ),
                {"oid": objective_id},
            )
        ).scalar_one()
    return int(latest)


async def alist_events(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    type: str | None = None,
    after_seq: int | None = None,
) -> ObjectiveEventPage:
    """Deterministic ledger page (§5.2; AGENTS.md pagination invariant).

    Ordered by ``seq``; ``total`` is the ``COUNT(*)`` under the IDENTICAL
    filter predicate (scope + optional type + optional ``after_seq``
    replay cursor). ``after_seq`` returns strictly-newer events so an SSE
    reconnect after a dropped stream deduplicates by cursor instead of
    re-reading history.
    """
    _check_page_bounds(limit, offset)
    if after_seq is not None and (not isinstance(after_seq, int) or after_seq < 0):
        raise ValueError("after_seq must be an integer >= 0 when set")
    predicate = "WHERE objective_id = :oid"
    params: dict[str, object] = {"oid": objective_id, "l": limit, "o": offset}
    if type is not None:
        if not type.startswith(OBJECTIVE_EVENT_PREFIX):
            raise ValueError("type filter must stay inside the objective family")
        predicate += " AND type = :etype"
        params["etype"] = type
    if after_seq is not None:
        predicate += " AND seq > :after"
        params["after"] = after_seq
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT server_id FROM objectives "
                    "WHERE objective_id = :oid"
                ),
                {"oid": objective_id},
            )
        ).mappings().first()
        if row is None:
            raise _not_found()
        scope = await _ascope_ids(session, objective_id)
        assert_objective_scope(server_id, scope | {row["server_id"]})
        total = int(
            (
                await session.execute(
                    text(f"SELECT COUNT(*) FROM objective_events {predicate}"),
                    params,
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT event_id, objective_id, seq, run_id, ts, type,
                           actor_kind, v, data
                    FROM objective_events {predicate}
                    ORDER BY seq
                    LIMIT :l OFFSET :o
                    """
                ),
                params,
            )
        ).mappings().all()
    items = [_row_to_event(row) for row in rows]
    return ObjectiveEventPage(
        objective_id=objective_id,
        limit=limit,
        offset=offset,
        total=total,
        has_more=compute_has_more(offset, len(items), total),
        items=items,
    )


def _check_page_bounds(limit: int, offset: int) -> None:
    """Bounded-page validation (AGENTS.md §1): fail closed, never clamp."""
    limit_ok = isinstance(limit, int) and not isinstance(limit, bool)
    if not limit_ok or not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ValueError(f"limit must be an integer in [1, {MAX_PAGE_LIMIT}]")
    offset_ok = isinstance(offset, int) and not isinstance(offset, bool)
    if not offset_ok or offset < 0:
        raise ValueError("offset must be an integer >= 0")
