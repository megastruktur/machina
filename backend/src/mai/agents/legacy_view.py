"""Truthful legacy read adapter (Runtime v2 §10.3; AC4).

READ-ONLY compatibility view over the legacy conversation/task/run
domains: legacy rows remain facts, history is never rewritten, and no
legacy achievement is fabricated into a v2 shape (runtime-v2.md §10.3;
agent-task-continuity.md §8 ``legacy_import``/``history_completeness``
semantics unchanged).

The adapter exists so v2 consumers can look at legacy history WITHOUT
touching the legacy write surfaces: every fact it returns is read from
the app-owned legacy tables (0008 conversations, 0009 agent_tasks,
0004 conversation/job runs) exactly as stored, and the view states
plainly that the episode is ``runtime="legacy"`` with the objective
bindings probed read-only from the v2 linkage table (empty for a legacy
conversation unless someone explicitly bound one of its runs — which the
``objective_runs`` handler CHECK makes impossible for legacy runs).

This module NEVER writes. The "no in-flight legacy run becomes v2"
guarantee is enforced durably at the ONLY v2 binding seam
(:func:`mai.agents.objective_store.abind_run` + the ``objective_runs``
handler CHECK); this adapter makes the legacy truthfulness observable.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents.conversation_store import (
    ConversationRow,
    _not_found,
    _row_to_conversation,
)
from mai.contracts.conversations import assert_conversation_scope

__all__ = [
    "LegacyConversationView",
    "LegacyRunFacts",
    "LegacyTaskFacts",
    "alegacy_conversation_view",
]

_RUNTIME_LEGACY: Final[str] = "legacy"


@dataclass(frozen=True, slots=True)
class LegacyTaskFacts:
    """The legacy task episode exactly as the 0009 row stores it."""

    task_id: uuid.UUID
    conversation_id: uuid.UUID
    server_id: uuid.UUID
    title: str
    summary: str | None
    requested_phase: str
    status: str
    revision: int
    created_at: object
    updated_at: object


@dataclass(frozen=True, slots=True)
class LegacyRunFacts:
    """One legacy run link exactly as the 0008 linkage stores it."""

    run_id: uuid.UUID
    task_id: uuid.UUID | None
    created_at: object
    released_at: object | None
    #: True while the run still holds the conversation writer lease.
    lease_holding: bool


@dataclass(frozen=True, slots=True)
class LegacyConversationView:
    """The truthful legacy view of one conversation.

    ``runtime`` is always ``"legacy"``: nothing in this read path
    reinterprets legacy history as v2. ``objective_ids`` is the read-only
    probe of the v2 linkage table — empty for every legacy conversation
    whose runs were never v2-bound (legacy runs cannot be).
    """

    runtime: str
    conversation: ConversationRow
    task: LegacyTaskFacts | None
    runs: tuple[LegacyRunFacts, ...]
    objective_ids: tuple[uuid.UUID, ...]


async def _alegacy_task(
    session: AsyncSession,
    conversation_id: uuid.UUID,
) -> LegacyTaskFacts | None:
    row = (
        await session.execute(
            text(
                """
                SELECT task_id, conversation_id, server_id, title, summary,
                       requested_phase, status, revision, created_at,
                       updated_at
                FROM agent_tasks
                WHERE conversation_id = :cid
                ORDER BY created_at DESC, task_id
                LIMIT 1
                """
            ),
            {"cid": conversation_id},
        )
    ).mappings().first()
    if row is None:
        return None
    return LegacyTaskFacts(
        task_id=row["task_id"],  # type: ignore[arg-type]
        conversation_id=row["conversation_id"],  # type: ignore[arg-type]
        server_id=row["server_id"],  # type: ignore[arg-type]
        title=str(row["title"]),
        summary=row["summary"],  # type: ignore[arg-type]
        requested_phase=str(row["requested_phase"]),
        status=str(row["status"]),
        revision=int(row["revision"]),  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
    )


async def _alegacy_runs(
    session: AsyncSession,
    conversation_id: uuid.UUID,
) -> tuple[LegacyRunFacts, ...]:
    rows = (
        await session.execute(
            text(
                """
                SELECT run_id, task_id, created_at, released_at
                FROM conversation_runs
                WHERE conversation_id = :cid
                ORDER BY created_at, run_id
                """
            ),
            {"cid": conversation_id},
        )
    ).mappings().all()
    return tuple(
        LegacyRunFacts(
            run_id=row["run_id"],  # type: ignore[arg-type]
            task_id=row["task_id"],  # type: ignore[arg-type]
            created_at=row["created_at"],  # type: ignore[arg-type]
            released_at=row["released_at"],  # type: ignore[arg-type]
            lease_holding=row["released_at"] is None,
        )
        for row in rows
    )


async def _aobjective_probe(
    session: AsyncSession,
    run_ids: Sequence[uuid.UUID],
) -> tuple[uuid.UUID, ...]:
    """Read-only probe: which v2 objectives (if any) these runs bind to."""
    if not run_ids:
        return ()
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT objective_id FROM objective_runs "
                "WHERE run_id = ANY(CAST(:rids AS uuid[])) "
                "ORDER BY objective_id"
            ),
            {"rids": [str(value) for value in run_ids]},
        )
    ).scalars().all()
    return tuple(rows)


async def alegacy_conversation_view(
    sessions: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> LegacyConversationView:
    """The truthful legacy read of one conversation (§10.3; AC4).

    Scope guard identical to the legacy surface: a foreign-scope request
    fails closed with ``conversation_not_found``. Everything returned is
    read from the legacy rows verbatim — completeness markers, task
    facts, run leases — plus the read-only v2-linkage probe.
    """
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT conversation_id, server_id, title, "
                    "history_completeness, missing_from_seq, missing_to_seq, "
                    "active_task_id, active_run_id, created_at, updated_at "
                    "FROM conversations WHERE conversation_id = :cid"
                ),
                {"cid": conversation_id},
            )
        ).mappings().first()
        if row is None:
            raise _not_found()
        conversation = _row_to_conversation(row)
        assert_conversation_scope(server_id, conversation.server_id)
        task = await _alegacy_task(session, conversation_id)
        runs = await _alegacy_runs(session, conversation_id)
        objective_ids = await _aobjective_probe(
            session, [link.run_id for link in runs]
        )
    return LegacyConversationView(
        runtime=_RUNTIME_LEGACY,
        conversation=conversation,
        task=task,
        runs=runs,
        objective_ids=objective_ids,
    )
