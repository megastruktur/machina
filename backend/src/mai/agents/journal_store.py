"""Work-journal projection + advisory checklist store (v2, §8).

ONE projection seam for the Runtime v2 work journal (runtime-v2.md §8,
journal-store slice):

- **Projection, not an engine (§8.1):** the journal is the deterministic
  fold of the canonical ``objective.*`` ledger — :func:`fold_journal` is a
  pure function, so replaying the same events from an empty state yields
  exactly the same document, and ``derived_seq`` names the exact ledger
  cursor the cached row was derived through. The ledger is the authority;
  the ``objective_journal`` row is a read cache that can always be rebuilt.
- **Trusted records only (AC3):** sections move ONLY through typed
  ``objective.journal_updated`` payloads (action attempts, receipts,
  verification verdicts, unknown effects, delegation links). A malformed
  advisory checklist update degrades to an honest ``malformed`` marker
  with the last valid items kept — it can never erase recorded effects and
  never fails a run (§8.2). Unknown in-family types pass through as
  visible bookkeeping markers.
- **Transactional consistency (AC2):** :func:`ajournal_apply` appends the
  ``objective.journal_updated`` event and folds the cached projection
  forward INSIDE one transaction under the objective row lock — the cache
  can never contradict the ledger. A journal write failure raises
  (bookkeeping trouble for the caller); the effect's durable records stay
  the authority and the projection catches up honestly (§8.4) via
  :func:`aread_journal` / :func:`ajournal_catchup` /
  :func:`async_journal_rebuild`.
- **Advisory checklist (§8.2, §11):** bounded, CAS-revisioned PUT with the
  ``objective.checklist_updated`` event in the same transaction. No tick
  grants authority or completes anything.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents.activity_store import (
    ObjectiveEventDraft,
    aappend_events,
    achecked_objective,
)
from mai.agents.output_safety import project_text
from mai.contracts.objectives import (
    ACTOR_KINDS,
    CHECKLIST_ITEM_MAX_CHARS,
    MAX_CHECKLIST_ITEMS,
    OBJECTIVE_CHECKLIST_UPDATED_EVENT_TYPE,
    OBJECTIVE_JOURNAL_UPDATED_EVENT_TYPE,
    ActorKind,
    ChecklistDocument,
    ChecklistItem,
    JournalActionApplied,
    JournalChildLinked,
    JournalMutation,
    JournalReceiptRecorded,
    JournalUnknownNoted,
    JournalVerificationRecorded,
    ObjectiveChecklistUpdatedPayload,
    ObjectiveContractError,
    ObjectiveErrorCode,
    ObjectiveEvent,
    ObjectiveFieldError,
    ObjectiveJournalUpdatedPayload,
    WorkJournal,
    fold_journal,
)

__all__ = [
    "aderive_journal",
    "ajournal_apply",
    "ajournal_catchup",
    "aput_checklist",
    "aread_checklist",
    "aread_journal",
    "async_journal_rebuild",
    "fold_journal",
]

#: Replay page size (the frozen ledger page bound; AGENTS.md §1).
_REPLAY_PAGE: Final[int] = 200

_EVENT_COLUMNS: Final[str] = (
    "event_id, objective_id, seq, run_id, ts, type, actor_kind, v, data"
)


def _not_found() -> ObjectiveContractError:
    return ObjectiveContractError(
        [
            ObjectiveFieldError(
                ObjectiveErrorCode.OBJECTIVE_NOT_FOUND.value,
                "objective_id",
                "no objective in the requesting server scope",
            )
        ]
    )


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


async def _apage_events(
    session: AsyncSession,
    objective_id: uuid.UUID,
    *,
    after_seq: int,
) -> list[ObjectiveEvent]:
    """Every ledger event after ``after_seq``, in bounded seq-order pages."""
    events: list[ObjectiveEvent] = []
    cursor = after_seq
    while True:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT {_EVENT_COLUMNS}
                    FROM objective_events
                    WHERE objective_id = :oid AND seq > :cursor
                    ORDER BY seq
                    LIMIT :page
                    """
                ),
                {"oid": objective_id, "cursor": cursor, "page": _REPLAY_PAGE},
            )
        ).mappings().all()
        if not rows:
            return events
        events.extend(_row_to_event(row) for row in rows)
        cursor = int(rows[-1]["seq"])
        if len(rows) < _REPLAY_PAGE:
            return events


async def _alatest_seq(session: AsyncSession, objective_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                text(
                    "SELECT COALESCE(MAX(seq), 0) FROM objective_events "
                    "WHERE objective_id = :oid"
                ),
                {"oid": objective_id},
            )
        ).scalar_one()
    )


async def _aload_cache(
    session: AsyncSession, objective_id: uuid.UUID
) -> tuple[WorkJournal | None, int]:
    row = (
        await session.execute(
            text(
                "SELECT projection, derived_seq FROM objective_journal "
                "WHERE objective_id = :oid"
            ),
            {"oid": objective_id},
        )
    ).mappings().first()
    if row is None:
        return None, 0
    return (
        WorkJournal.model_validate(dict(row["projection"])),
        int(row["derived_seq"]),
    )


async def _aupsert_cache(
    session: AsyncSession, objective_id: uuid.UUID, state: WorkJournal
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO objective_journal
                (objective_id, projection, derived_seq, created_at,
                 updated_at)
            VALUES
                (:oid, CAST(:projection AS jsonb), :derived, now(), now())
            ON CONFLICT (objective_id) DO UPDATE
            SET projection = EXCLUDED.projection,
                derived_seq = EXCLUDED.derived_seq,
                updated_at = now()
            """
        ),
        {
            "oid": objective_id,
            "projection": state.model_dump_json(),
            "derived": state.derived_seq,
        },
    )


async def _areplay_from_zero(
    session: AsyncSession, objective_id: uuid.UUID
) -> WorkJournal:
    """Fold the whole ledger from an empty state (the derived truth)."""
    events = await _apage_events(session, objective_id, after_seq=0)
    if not events:
        return WorkJournal(objective_id=objective_id)
    return fold_journal(None, events).journal


# ---------------------------------------------------------------------------
# reads with honest catch-up (§8.4)
# ---------------------------------------------------------------------------


async def aread_journal(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> WorkJournal:
    """The current work journal (§8 read path).

    Folds the cached projection forward over any ledger events past its
    ``derived_seq`` (crash between producers and projection writes — the
    cache catches up honestly and the effects are never lost, §8.4); a
    missing cache derives the full document from the ledger. The document
    is bounded with explicit truncation flags, so this is a fixed-shape
    read, not an unbounded collection.
    """
    async with sessions() as session, session.begin():
        await achecked_objective(session, objective_id, server_id=server_id)
        state, derived_seq = await _aload_cache(session, objective_id)
        if state is None:
            derived = await _areplay_from_zero(session, objective_id)
            await _aupsert_cache(session, objective_id, derived)
            return derived
        if state.derived_seq >= await _alatest_seq(session, objective_id):
            return state
        tail = await _apage_events(
            session, objective_id, after_seq=state.derived_seq
        )
        result = fold_journal(state, tail)
        await _aupsert_cache(session, objective_id, result.journal)
        return result.journal


async def ajournal_catchup(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> WorkJournal:
    """Persist the honest catch-up (recovery seam; §8.4).

    Folds the cached projection forward to the current ledger cursor — or
    derives from zero when the cache is missing — and writes the cache
    WITHOUT appending events (a recovery, not a transition).
    """
    async with sessions() as session, session.begin():
        await achecked_objective(session, objective_id, server_id=server_id)
        state, derived_seq = await _aload_cache(session, objective_id)
        if state is None:
            derived = await _areplay_from_zero(session, objective_id)
            await _aupsert_cache(session, objective_id, derived)
            return derived
        if derived_seq >= await _alatest_seq(session, objective_id):
            return state
        tail = await _apage_events(session, objective_id, after_seq=derived_seq)
        result = fold_journal(state, tail)
        await _aupsert_cache(session, objective_id, result.journal)
        return result.journal


async def aderive_journal(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> WorkJournal:
    """Derive the journal purely from the ledger (audit path).

    Ignores the cache entirely and replays every event from sequence 1 —
    the deterministic function the cache must always equal.
    """
    async with sessions() as session:
        await achecked_objective(session, objective_id, server_id=server_id)
        return await _areplay_from_zero(session, objective_id)


async def async_journal_rebuild(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> WorkJournal:
    """Rebuild the cache from a full ledger replay (recovery seam).

    The rebuilt document is written back so ``aread_journal`` and the
    derived truth agree exactly afterwards.
    """
    async with sessions() as session, session.begin():
        await achecked_objective(session, objective_id, server_id=server_id)
        derived = await _areplay_from_zero(session, objective_id)
        await _aupsert_cache(session, objective_id, derived)
        return derived


# ---------------------------------------------------------------------------
# trusted-record application (§8.1; AC2/AC3)
# ---------------------------------------------------------------------------


def _mutation_payload(
    mutation: JournalMutation,
) -> ObjectiveJournalUpdatedPayload:
    if isinstance(mutation, JournalActionApplied):
        return ObjectiveJournalUpdatedPayload(kind="action", action=mutation)
    if isinstance(mutation, JournalReceiptRecorded):
        return ObjectiveJournalUpdatedPayload(kind="receipt", receipt=mutation)
    if isinstance(mutation, JournalVerificationRecorded):
        return ObjectiveJournalUpdatedPayload(
            kind="verification", verification=mutation
        )
    if isinstance(mutation, JournalUnknownNoted):
        return ObjectiveJournalUpdatedPayload(kind="unknown", unknown=mutation)
    if isinstance(mutation, JournalChildLinked):
        return ObjectiveJournalUpdatedPayload(kind="child", child=mutation)
    raise ValueError("mutation must be a trusted journal record")


async def ajournal_apply(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    mutation: JournalMutation,
    actor_kind: ActorKind,
    run_id: uuid.UUID | None = None,
) -> WorkJournal:
    """Apply one trusted-record mutation transactionally (§8.1; AC2).

    ONE transaction: lock the objective row, scope-check, append the
    ``objective.journal_updated`` event with the typed payload, re-read
    the persisted event row and fold the cached projection forward to the
    event's cursor. A failure raises and rolls BOTH back — the ledger and
    the cache can never disagree; the caller (the run writer) treats the
    raise as bookkeeping trouble: the effect's durable records stay
    authoritative (§8.4).
    """
    payload = _mutation_payload(mutation)
    async with sessions() as session, session.begin():
        await achecked_objective(
            session, objective_id, server_id=server_id, for_update=True
        )
        (seq,) = await aappend_events(
            session,
            objective_id,
            [
                ObjectiveEventDraft(
                    type=OBJECTIVE_JOURNAL_UPDATED_EVENT_TYPE,
                    actor_kind=actor_kind,
                    data=payload.model_dump(mode="json"),
                    run_id=run_id,
                )
            ],
        )
        row = (
            await session.execute(
                text(
                    f"SELECT {_EVENT_COLUMNS} FROM objective_events "
                    "WHERE objective_id = :oid AND seq = :seq"
                ),
                {"oid": objective_id, "seq": seq},
            )
        ).mappings().one()
        persisted = _row_to_event(row)
        state, _derived_seq = await _aload_cache(session, objective_id)
        if state is None or state.derived_seq < seq - 1:
            # Missing/lagging cache: derive the truth, then fold the new
            # event so the write lands exactly at the new cursor.
            base = await _areplay_from_zero(session, objective_id)
        else:
            base = state
        result = fold_journal(base, [persisted])
        await _aupsert_cache(session, objective_id, result.journal)
        await session.execute(
            text(
                "UPDATE objectives SET updated_at = now() "
                "WHERE objective_id = :oid"
            ),
            {"oid": objective_id},
        )
    return result.journal


# ---------------------------------------------------------------------------
# advisory checklist (§8.2; §11 revision-CAS PUT)
# ---------------------------------------------------------------------------


def _project_items(
    items: list[ChecklistItem] | list[Mapping[str, object]],
) -> list[ChecklistItem]:
    """Typed, projected checklist items (rejection = bookkeeping trouble).

    Text is projected (redaction BEFORE the bound — a cut can never split
    a secret); counts and blanks fail closed at the store boundary.
    """
    projected: list[ChecklistItem] = []
    for item in items:
        if isinstance(item, ChecklistItem):
            text_value, done = item.text, item.done
        elif isinstance(item, Mapping):
            text_value = item.get("text")
            done = bool(item.get("done", False))
        else:
            raise ValueError("checklist items must be ChecklistItem values")
        if not isinstance(text_value, str) or not text_value.strip():
            raise ValueError("checklist item text must be a non-blank string")
        projected.append(
            ChecklistItem(
                text=project_text(text_value, max_chars=CHECKLIST_ITEM_MAX_CHARS),
                done=done,
            )
        )
    if len(projected) > MAX_CHECKLIST_ITEMS:
        raise ValueError(
            f"the advisory checklist is bounded at {MAX_CHECKLIST_ITEMS} items"
        )
    return projected


async def aread_checklist(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> ChecklistDocument | None:
    """The advisory checklist projection; ``None`` when never maintained."""
    async with sessions() as session:
        await achecked_objective(session, objective_id, server_id=server_id)
        row = (
            await session.execute(
                text(
                    "SELECT objective_id, items, revision, updated_at "
                    "FROM objective_checklists WHERE objective_id = :oid"
                ),
                {"oid": objective_id},
            )
        ).mappings().first()
    if row is None:
        return None
    return ChecklistDocument(
        objective_id=row["objective_id"],  # type: ignore[arg-type]
        items=[ChecklistItem.model_validate(item) for item in (row["items"] or [])],
        revision=int(row["revision"]),  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
    )


async def aput_checklist(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    items: list[ChecklistItem] | list[Mapping[str, object]],
    expected_revision: int,
    actor_kind: ActorKind,
    run_id: uuid.UUID | None = None,
) -> ChecklistDocument:
    """Replace the advisory checklist under its CAS revision (§8.2, §11).

    ONE transaction: lock the objective row, scope-check, project the
    typed items, bump the checklist revision, persist the row and append
    ``objective.checklist_updated`` with the full bounded list. A
    rejected write (stale revision, malformed items) changes nothing.
    """
    if actor_kind not in ACTOR_KINDS:
        raise ValueError("actor_kind must be an ActorKind value")
    projected = _project_items(items)
    async with sessions() as session, session.begin():
        await achecked_objective(
            session, objective_id, server_id=server_id, for_update=True
        )
        row = (
            await session.execute(
                text(
                    "SELECT revision FROM objective_checklists "
                    "WHERE objective_id = :oid FOR UPDATE"
                ),
                {"oid": objective_id},
            )
        ).mappings().first()
        current_revision = 0 if row is None else int(row["revision"])
        if expected_revision != current_revision:
            raise ObjectiveContractError(
                [
                    ObjectiveFieldError(
                        ObjectiveErrorCode.REVISION_CONFLICT.value,
                        "expected_revision",
                        "expected revision does not match the current "
                        "checklist revision",
                    )
                ]
            )
        new_revision = current_revision + 1
        await session.execute(
            text(
                """
                INSERT INTO objective_checklists
                    (objective_id, items, revision, created_at, updated_at)
                VALUES
                    (:oid, CAST(:items AS jsonb), :rev, now(), now())
                ON CONFLICT (objective_id) DO UPDATE
                SET items = EXCLUDED.items,
                    revision = EXCLUDED.revision,
                    updated_at = now()
                """
            ),
            {
                "oid": objective_id,
                "items": json.dumps(
                    [item.model_dump() for item in projected], ensure_ascii=False
                ),
                "rev": new_revision,
            },
        )
        payload = ObjectiveChecklistUpdatedPayload(
            items=projected,
            checklist_revision=new_revision,
        )
        await aappend_events(
            session,
            objective_id,
            [
                ObjectiveEventDraft(
                    type=OBJECTIVE_CHECKLIST_UPDATED_EVENT_TYPE,
                    actor_kind=actor_kind,
                    data=payload.model_dump(mode="json"),
                    run_id=run_id,
                )
            ],
        )
        await session.execute(
            text(
                "UPDATE objectives SET updated_at = now() "
                "WHERE objective_id = :oid"
            ),
            {"oid": objective_id},
        )
    document = await aread_checklist(sessions, objective_id, server_id=server_id)
    assert document is not None  # written above in this same call
    return document
