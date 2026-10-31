"""Run lifecycle: frozen transitions + the persisted event cursor (C05 rule 2/4).

The facade-owned half of the run state machine (contracts.md **C05**;
`05-run-lifecycle`). The engine adapter (``mai.jobs.engine``) keeps DBOS
system tables as the canonical owner of engine-derived states; this module
owns the domain half:

- **The frozen transition table** — exactly the C05 rule 2 diagram edges.
  Application-level states (``waiting_approval``/``waiting_children``/
  ``reconciling``) and ``queued`` are facade-recorded; terminal states
  (``succeeded``/``failed``/``cancelled``) have no out-edges, so a stale
  result can never overwrite them (acceptance 1). ``cancel_requested`` is
  deliberately not a state: the facade records the request (``job_runs``
  ``cancel_requested_at``) while the run state moves only when the engine
  lands CANCELLED (guard rule 3: requested ≠ verified).

- **The persisted event cursor** — every accepted transition is one
  ``run.transition`` event in ``job_events`` (the frozen C05 rule 4
  envelope, ``v`` = :data:`mai.contracts.jobs.EVENT_PAYLOAD_VERSION`),
  allocated the next per-run ``seq`` under the ``job_runs`` row lock.
  The per-run cursor is the WHOLE stream cursor: step events and other
  producers share it, so allocation reads ``MAX(seq)`` over all events of
  the run and the storage-level ``UNIQUE (run_id, seq)`` (migration
  ``0004_jobs``) is the backstop that keeps it monotonic (acceptance 1).

- **Replay** — the current run state is never cached in the process: it is
  derived from the persisted transition events, in ``seq`` order, with
  chain-contiguity validation (each event's ``from`` must equal the
  previous event's ``to``; the first event anchors at the submit edge).
  A restarted process therefore replays to the identical ordered state
  from the database alone (acceptance 2) — replay is fail-closed, never
  guessed from a partial or tampered log.

Writers outside this module: the ``job_runs`` row itself is created by the
submit owner (``05-jobs-api``); step events are appended by ``05-steps``.
Both MUST allocate their ``seq`` the same way (next stream seq under the
run row lock); the UNIQUE constraint is the shared backstop.

Rejections are one value-free :class:`RunLifecycleError` (code + fixed
reason, C01 rule 3) — safe to log, store and return to a browser.

The module reads no ambient state beyond its arguments; it never touches
engine tables and never writes anything but ``job_events`` rows.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.jobs import (
    EVENT_PAYLOAD_VERSION,
    TERMINAL_RUN_STATES,
    JobEvent,
    RunState,
    parse_job_event,
)
from mai.jobs.engine import EngineError, validate_run_id
from mai.tag_store import _aclassify_terminal_job_locked

#: The event type this module owns in the shared per-run stream. Other
#: producers (steps, schedules) append their own types; replay filters by
#: this slug and ignores the rest. Lowercase dotted slug per the frozen
#: envelope grammar.
TRANSITION_EVENT_TYPE: Final[str] = "run.transition"

#: The frozen C05 rule 2 diagram as a transition table. ``None`` is the
#: pre-submit anchor: the submit edge is the only transition out of it.
#: Terminal states map to the empty set — the table is the stale-result
#: guard (acceptance 1), not just a diagram restatement.
LEGAL_RUN_TRANSITIONS: Final[Mapping[RunState | None, frozenset[RunState]]] = (
    MappingProxyType(
        {
            None: frozenset({RunState.QUEUED}),
            RunState.QUEUED: frozenset(
                {RunState.RUNNING, RunState.FAILED, RunState.CANCELLED}
            ),
            RunState.RUNNING: frozenset(
                {
                    RunState.WAITING_APPROVAL,
                    RunState.WAITING_CHILDREN,
                    RunState.RECONCILING,
                    RunState.SUCCEEDED,
                    RunState.FAILED,
                    RunState.CANCELLED,
                }
            ),
            RunState.WAITING_APPROVAL: frozenset({RunState.RUNNING}),
            RunState.WAITING_CHILDREN: frozenset({RunState.RUNNING}),
            RunState.RECONCILING: frozenset({RunState.RUNNING, RunState.FAILED}),
            RunState.SUCCEEDED: frozenset(),
            RunState.FAILED: frozenset(),
            RunState.CANCELLED: frozenset(),
        }
    )
)


class RunLifecycleErrorCode(StrEnum):
    """Value-free rejection codes (C01 envelope vocabulary)."""

    INVALID_RUN_ID = "invalid_run_id"
    INVALID_TRANSITION = "invalid_transition"
    TERMINAL_IMMUTABLE = "terminal_immutable"
    INVALID_TIMESTAMP = "invalid_timestamp"
    INVALID_EVENT_DATA = "invalid_event_data"
    RUN_NOT_FOUND = "run_not_found"


class RunLifecycleError(ValueError):
    """One value-free lifecycle rejection: code + fixed reason.

    Messages carry codes, field names and opaque identifiers only — never
    payload values — so they are safe to log, store and surface (C01
    rule 3; the "safe error envelope" of acceptance 2).
    """

    def __init__(self, code: RunLifecycleErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def is_legal_transition(current: RunState | None, target: RunState) -> bool:
    """True iff ``current → target`` is an edge of the frozen diagram."""
    if not isinstance(target, RunState):
        return False
    return target in LEGAL_RUN_TRANSITIONS[current]


def validate_transition(current: RunState | None, target: RunState) -> None:
    """Require ``current → target`` to be a legal diagram edge.

    A terminal ``current`` raises ``TERMINAL_IMMUTABLE`` for EVERY target —
    the explicit stale-result guard (acceptance 1: a terminal run can never
    be overwritten, including by the same terminal value again). Any other
    illegal edge raises ``INVALID_TRANSITION``.
    """
    if not isinstance(target, RunState):
        raise RunLifecycleError(
            RunLifecycleErrorCode.INVALID_TRANSITION,
            "target must be a RunState value",
        )
    if current in TERMINAL_RUN_STATES:
        raise RunLifecycleError(
            RunLifecycleErrorCode.TERMINAL_IMMUTABLE,
            f"run is terminal ({current.value}); no transition can overwrite it",
        )
    if not is_legal_transition(current, target):
        raise RunLifecycleError(
            RunLifecycleErrorCode.INVALID_TRANSITION,
            "transition is not an edge of the frozen C05 rule 2 diagram",
        )


def parse_transition_data(
    data: Mapping[str, object],
) -> tuple[RunState | None, RunState]:
    """Validate one ``run.transition`` payload; return ``(from, to)``.

    Strict shape: exactly the keys ``{"from", "to"}``; ``from`` is a state
    value or ``None`` (the submit anchor); ``to`` is a state value. Anything
    else fails closed with ``INVALID_EVENT_DATA`` — replay never guesses.
    """
    if not isinstance(data, Mapping) or set(data) != {"from", "to"}:
        raise RunLifecycleError(
            RunLifecycleErrorCode.INVALID_EVENT_DATA,
            "transition payload must have exactly the keys 'from' and 'to'",
        )
    raw_from, raw_to = data["from"], data["to"]
    if raw_from is not None and not isinstance(raw_from, str):
        raise RunLifecycleError(
            RunLifecycleErrorCode.INVALID_EVENT_DATA,
            "'from' must be a state value or null",
        )
    if not isinstance(raw_to, str):
        raise RunLifecycleError(
            RunLifecycleErrorCode.INVALID_EVENT_DATA, "'to' must be a state value"
        )
    try:
        from_state = None if raw_from is None else RunState(raw_from)
        to_state = RunState(raw_to)
    except ValueError:
        raise RunLifecycleError(
            RunLifecycleErrorCode.INVALID_EVENT_DATA,
            "transition payload carries an unknown state value",
        ) from None
    return from_state, to_state


def make_transition_event(
    run_id: str, seq: int, ts: datetime, from_state: RunState | None, to_state: RunState
) -> JobEvent:
    """Build the frozen-envelope event for one accepted transition."""
    return JobEvent(
        seq=seq,
        run_id=uuid.UUID(run_id),
        ts=ts,
        type=TRANSITION_EVENT_TYPE,
        v=EVENT_PAYLOAD_VERSION,
        data={
            "from": None if from_state is None else from_state.value,
            "to": to_state.value,
        },
    )


def states_in_order(events: Iterable[JobEvent]) -> tuple[RunState, ...]:
    """Replay transition events into the ordered state history (acceptance 2).

    Non-lifecycle events (steps, other producers) are ignored; lifecycle
    events are ordered by ``seq`` and validated: the first anchors at the
    submit edge (``from is None``) and every subsequent ``from`` must equal
    the previous ``to``. Any violation fails closed — a partial, reordered
    or tampered log can never silently yield a wrong state.
    """
    transitions = sorted(
        (event for event in events if event.type == TRANSITION_EVENT_TYPE),
        key=lambda event: event.seq,
    )
    states: list[RunState] = []
    previous: RunState | None = None
    first = True
    for event in transitions:
        from_state, to_state = parse_transition_data(event.data)
        if first:
            if from_state is not None:
                raise RunLifecycleError(
                    RunLifecycleErrorCode.INVALID_EVENT_DATA,
                    "transition log does not anchor at the submit edge",
                )
            first = False
        elif from_state is not previous:
            raise RunLifecycleError(
                RunLifecycleErrorCode.INVALID_EVENT_DATA,
                "transition log is not contiguous at its cursor",
            )
        states.append(to_state)
        previous = to_state
    return tuple(states)


def current_state(events: Iterable[JobEvent]) -> RunState | None:
    """The run's current state per the replayed log; ``None`` before submit."""
    states = states_in_order(events)
    return states[-1] if states else None


def _utc_or_reject(ts: datetime | None) -> datetime:
    """Normalize an explicit event timestamp to UTC; ``None`` means now."""
    if ts is None:
        return datetime.now(timezone.utc)
    if not isinstance(ts, datetime) or ts.tzinfo is None:
        raise RunLifecycleError(
            RunLifecycleErrorCode.INVALID_TIMESTAMP,
            "event timestamp must be timezone-aware",
        )
    return ts.astimezone(timezone.utc)


class RunLifecycle:
    """Facade-side lifecycle over one run's persisted event stream.

    Constructed with the application session factory (``mai.database``
    engine); holds no process state — every read replays from the database,
    so a restarted caller observes the identical ordered state (acceptance 2).
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record_transition(
        self,
        run_id: str,
        target: RunState,
        *,
        occurred_at: datetime | None = None,
    ) -> JobEvent:
        """Validate and durably record one transition; return its event.

        Atomic per run: the ``job_runs`` row is locked ``FOR UPDATE`` (the
        per-run writer serializer), the current state is replayed from the
        persisted log, the transition validated, and the event appended with
        the next whole-stream ``seq`` — all in one transaction. A terminal
        target additionally swaps the system ``unclassified`` job tag for the
        matching terminal tag in that same transaction; a missing terminal
        tag aborts the whole transition atomically. Two
        concurrent transitions of one run therefore serialize: the loser
        replays the winner's committed state and is rejected by the frozen
        table (a terminal race ends with the FIRST terminal value stuck —
        the stale-result guard, acceptance 1). Requires the ``job_runs`` row
        to exist (submit owner creates it); unknown runs are rejected.
        """
        try:
            validate_run_id(run_id)
        except EngineError as exc:
            raise RunLifecycleError(
                RunLifecycleErrorCode.INVALID_RUN_ID,
                "run_id must be a canonical UUID string",
            ) from exc
        if not isinstance(target, RunState):
            raise RunLifecycleError(
                RunLifecycleErrorCode.INVALID_TRANSITION,
                "target must be a RunState value",
            )
        ts = _utc_or_reject(occurred_at)
        async with self._sessions() as session:
            async with session.begin():
                locked = (
                    await session.execute(
                        text(
                            "SELECT run_id, server_id FROM job_runs "
                            "WHERE run_id = :r FOR UPDATE"
                        ),
                        {"r": uuid.UUID(run_id)},
                    )
                ).first()
                if locked is None:
                    raise RunLifecycleError(
                        RunLifecycleErrorCode.RUN_NOT_FOUND,
                        "no job_runs row for this run_id",
                    )
                current = current_state(await self._load_events(session, run_id))
                validate_transition(current, target)
                next_seq = (
                    await session.execute(
                        text(
                            "SELECT COALESCE(MAX(seq), 0) + 1 FROM job_events "
                            "WHERE run_id = :r"
                        ),
                        {"r": uuid.UUID(run_id)},
                    )
                ).scalar_one()
                event = make_transition_event(run_id, next_seq, ts, current, target)
                await session.execute(
                    text(
                        """
                        INSERT INTO job_events
                            (event_id, run_id, seq, ts, type, v, data)
                        VALUES
                            (:event_id, :run_id, :seq, :ts, :type, :v, :data)
                        """
                    ),
                    {
                        "event_id": uuid.uuid4(),
                        "run_id": uuid.UUID(run_id),
                        "seq": event.seq,
                        "ts": event.ts,
                        "type": event.type,
                        "v": event.v,
                        "data": json.dumps(
                            {
                                "from": None if current is None else current.value,
                                "to": target.value,
                            }
                        ),
                    },
                )
                if target in TERMINAL_RUN_STATES:
                    await _aclassify_terminal_job_locked(
                        session,
                        uuid.UUID(run_id),
                        server_id=locked[1],
                        terminal_tag_name=target.value,
                    )
        return event

    async def event_stream(self, run_id: str) -> tuple[JobEvent, ...]:
        """The run's whole persisted event stream, in cursor order.

        Every row is re-validated against the frozen envelope (C05 rule 4)
        — shape drift from any producer fails closed instead of leaking
        into a replay.
        """
        try:
            validate_run_id(run_id)
        except EngineError as exc:
            raise RunLifecycleError(
                RunLifecycleErrorCode.INVALID_RUN_ID,
                "run_id must be a canonical UUID string",
            ) from exc
        async with self._sessions() as session:
            return tuple(await self._load_events(session, run_id))

    async def replay_states(self, run_id: str) -> tuple[RunState, ...]:
        """The ordered state history replayed from the persisted log."""
        return states_in_order(await self.event_stream(run_id))

    async def current_state(self, run_id: str) -> RunState | None:
        """The run's current state per the persisted log (no engine call)."""
        return current_state(await self.event_stream(run_id))

    @staticmethod
    async def _load_events(session: AsyncSession, run_id: str) -> tuple[JobEvent, ...]:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT seq, ts, type, v, data FROM job_events
                    WHERE run_id = :r ORDER BY seq
                    """
                ),
                {"r": uuid.UUID(run_id)},
            )
        ).mappings()
        return tuple(
            parse_job_event(
                {
                    "seq": row["seq"],
                    "run_id": run_id,
                    "ts": row["ts"].isoformat(),
                    "type": row["type"],
                    "v": row["v"],
                    "data": row["data"],
                }
            )
            for row in rows
        )
