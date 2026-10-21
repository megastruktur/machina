"""The ``chat.read_session`` capability (campaign ``mechanicus-ui`` card 02).

The agent-facing read seam over the durable chat conversation record
(contracts.md **C05** facade tables): one ``session_id`` (the
conversation's ``run_id``) in, the conversation's replayed state and
recorded event summary out. The capability is deliberately read-only and
controller-local:

- The target is exclusively the payload's canonical ``session_id`` — no
  host vocabulary of its own (C03 rule 2): a granted call cannot be
  retargeted at an arbitrary host or conversation outside the persisted
  record.
- State is REPLAYED from the persisted ``run.transition`` events through
  the reviewed lifecycle producer — never guessed, never engine-called.
- The answer is a bounded JSON summary (identities, state, tags, event
  counts); task text, prompts and artifact bodies never enter the output
  (C05 rule 4).
- Immutable ``service_tags`` are read back from the run's recorded
  ``chat.submitted`` event — the same recorded source the HTTP
  ConversationView replays.

The executor follows the reviewed dispatcher convention — one
:class:`~mai.capabilities.dispatch.CapabilityCall` in, one honest
:class:`~mai.capabilities.dispatch.ExecutorResult` out; every
grant/approval/policy gate upstream of the executor is the reviewed
producers', never re-implemented here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.capabilities.dispatch import CapabilityCall, ExecutorResult
from mai.contracts.jobs import parse_job_event
from mai.jobs.lifecycle import current_state as replay_current_state
from mai.plugins.registry import CapabilityDescriptor, parse_descriptor
from mai.policy.evaluate import ExecutionContext

__all__ = [
    "CAPABILITY_ID",
    "ChatReadError",
    "ChatReadErrorCode",
    "ChatReadSessionCapability",
    "MAX_EVENTS_SUMMARY",
    "chat_read_descriptor",
]

#: The capability identifier (the executor vocabulary frozen here).
CAPABILITY_ID: Final[str] = "chat.read_session"

#: The chat submission event this capability reads ``service_tags`` from
#: (the HTTP chat surface's own vocabulary, recorded seam).
CHAT_SUBMITTED_EVENT_TYPE: Final[str] = "chat.submitted"

#: The run-state transition event replayed by the reviewed lifecycle
#: producer.
RUN_TRANSITION_EVENT_TYPE: Final[str] = "run.transition"

#: Bound on the events summary (a summary, not a dump; the full stream is
#: the SSE route's vocabulary).
MAX_EVENTS_SUMMARY: Final[int] = 100


class ChatReadErrorCode(StrEnum):
    """Stable machine-readable capability rejection codes."""

    INVALID_PAYLOAD = "invalid_payload"
    NOT_FOUND = "not_found"


class ChatReadError(ValueError):
    """One structured, value-free capability rejection."""

    def __init__(self, code: ChatReadErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def chat_read_descriptor() -> CapabilityDescriptor:
    """The validated ``chat.read_session`` descriptor (C03 rule 1).

    ``effect_class="read"`` is the load-bearing field: replaying a
    persisted conversation changes no observable state, so the policy
    layer requires no approval decision — while every grant/scope/policy
    gate still applies unchanged. (The card vocabulary names a
    ``risk_tier="read_only"``; the frozen descriptor registry owns no
    such field — the effect class carries the routing decision, a
    recorded contract adaptation.)
    """
    return parse_descriptor(
        {
            "identifier": CAPABILITY_ID,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "description": (
                    "Read a persisted chat conversation session by its canonical "
                    "run UUID and return its state, model profile, service tags, "
                    "and bounded event summary."
                ),
                "required": ["session_id"],
                "properties": {
                    "session_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "The conversation run id (canonical UUID) to "
                            "read; no host vocabulary exists."
                        ),
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "session_id",
                    "server_id",
                    "state",
                    "model_profile",
                    "service_tags",
                    "event_count",
                    "events_summary",
                ],
                "properties": {
                    "session_id": {"type": "string", "format": "uuid"},
                    "server_id": {"type": "string", "format": "uuid"},
                    "state": {"type": ["string", "null"]},
                    "model_profile": {"type": "string"},
                    "service_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "event_count": {"type": "integer", "minimum": 0},
                    "events_summary": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["seq", "type"],
                            "properties": {
                                "seq": {"type": "integer", "minimum": 1},
                                "type": {"type": "string"},
                            },
                        },
                    },
                },
            },
            "execution_environments": ["controller"],
            "effect_class": "read",
            "resource_scope": ["chat.session"],
            "required_grants": [CAPABILITY_ID],
        }
    )


@dataclass(frozen=True, slots=True)
class _ReplayedSession:
    """One replayed conversation record (identities + state + summary)."""

    session_id: str
    server_id: str
    state: str | None
    model_profile: str
    service_tags: tuple[str, ...]
    event_count: int
    events_summary: tuple[tuple[int, str], ...]


class ChatReadSessionCapability:
    """``chat.read_session`` executor: one gated call → one honest summary.

    Constructor-checked and fail-closed (the dispatch pattern): the
    session factory must be the app's reviewed ``async_sessionmaker``
    over the ONE facade engine. Performs no policy decision of its own —
    :class:`ExecutionContext` identity is validated as the dispatcher's
    contract requires, and unknown sessions are structured ``not_found``
    rejections, never silent empties.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        if not isinstance(sessions, async_sessionmaker):
            raise ChatReadError(
                ChatReadErrorCode.INVALID_PAYLOAD,
                "sessions must be an async_sessionmaker",
            )
        self._sessions = sessions

    def __repr__(self) -> str:
        return "<ChatReadSessionCapability>"

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        """Execute one gated read (see module docstring for the shape)."""
        if not isinstance(call, CapabilityCall):
            raise ChatReadError(
                ChatReadErrorCode.INVALID_PAYLOAD,
                "executor input must be a dispatcher CapabilityCall",
            )
        if not isinstance(call.context, ExecutionContext):
            raise ChatReadError(
                ChatReadErrorCode.INVALID_PAYLOAD,
                "call context must be an ExecutionContext",
            )
        payload = call.payload
        if not isinstance(payload, Mapping):
            raise ChatReadError(
                ChatReadErrorCode.INVALID_PAYLOAD, "payload must be a JSON object"
            )
        raw_session = payload.get("session_id")
        try:
            session_id = str(uuid.UUID(str(raw_session)))
        except (ValueError, AttributeError):
            raise ChatReadError(
                ChatReadErrorCode.INVALID_PAYLOAD,
                "session_id must be a canonical UUID string",
            ) from None

        replayed = await self._replay(session_id)
        output = {
            "session_id": replayed.session_id,
            "server_id": replayed.server_id,
            "state": replayed.state,
            "model_profile": replayed.model_profile,
            "service_tags": list(replayed.service_tags),
            "event_count": replayed.event_count,
            "events_summary": [
                {"seq": seq, "type": event_type}
                for seq, event_type in replayed.events_summary
            ],
        }
        return ExecutorResult(
            ok=True, output=json.dumps(output, sort_keys=True)
        )

    async def _replay(self, session_id: str) -> _ReplayedSession:
        """The confined read + replay: one run row, its whole event stream."""
        async with self._sessions() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT run_id::text AS run_id, "
                            "server_id::text AS server_id, model_profile "
                            "FROM job_runs WHERE run_id = :r"
                        ),
                        {"r": uuid.UUID(session_id)},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ChatReadError(
                    ChatReadErrorCode.NOT_FOUND,
                    "no conversation stored for this session id",
                )
            event_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT seq, ts, type, v, data FROM job_events "
                            "WHERE run_id = :r ORDER BY seq"
                        ),
                        {"r": uuid.UUID(session_id)},
                    )
                )
                .mappings()
                .all()
            )

        transitions = []
        service_tags: list[str] = []
        submitted_seen = False
        summary: list[tuple[int, str]] = []
        for index, event_row in enumerate(event_rows):
            seq = int(event_row["seq"])
            event_type = str(event_row["type"])
            if index < MAX_EVENTS_SUMMARY:
                summary.append((seq, event_type))
            if event_type == RUN_TRANSITION_EVENT_TYPE:
                transitions.append(
                    parse_job_event(
                        {
                            "seq": seq,
                            "run_id": session_id,
                            "ts": event_row["ts"].isoformat(),
                            "type": event_type,
                            "v": event_row["v"],
                            "data": event_row["data"],
                        }
                    )
                )
            elif event_type == CHAT_SUBMITTED_EVENT_TYPE and not submitted_seen:
                submitted_seen = True
                data = event_row["data"]
                if isinstance(data, Mapping):
                    raw_tags = data.get("service_tags") or []
                    if isinstance(raw_tags, list):
                        service_tags = [
                            str(tag) for tag in raw_tags if isinstance(tag, str)
                        ]
        state = replay_current_state(sorted(transitions, key=lambda e: e.seq))
        return _ReplayedSession(
            session_id=session_id,
            server_id=str(row["server_id"]),
            state=None if state is None else state.value,
            model_profile=str(row["model_profile"]),
            service_tags=tuple(service_tags),
            event_count=len(event_rows),
            events_summary=tuple(summary),
        )
