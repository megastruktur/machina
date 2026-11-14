"""The ``session.read`` capability (campaign ``mechanicus-ui`` card 02;
D07 rewires the read onto the session-first tables — sfc_PLAN D07;
D07-fix1 F1 renames the identifier from ``chat.read_session``).

The agent-facing read seam over the durable session-first history root:
one ``session_id`` in, the session's projected status and bounded entry
summary out. The capability is deliberately read-only and
controller-local:

- The target is exclusively the payload's canonical ``session_id`` — no
  host vocabulary of its own (C03 rule 2): a granted call cannot be
  retargeted at an arbitrary host or session outside the persisted record.
- State is the session row's projected ``status`` (D01 SessionStatus
  vocabulary, projected from its runs by the store) — never guessed,
  never engine-called; the legacy ``run.transition`` replay is deleted.
- The answer is a bounded JSON summary (identities, status, tags, entry
  counts); task text, prompts and artifact bodies never enter the output
  (C05 rule 4).
- ``service_tags`` are read back from the session's ``session_tags``
  associations joined to the global tag catalog.

One gated call reads the ONE session-first history root: the ``sessions``
row (identity + server scope + projected status), its latest run's model
profile, its durable ``session_entries`` summary (bounded), and its
``session_tags`` names. The legacy ``job_runs``/``job_events`` replay is
deleted with this cutover: the session store is the only conversation
authority (COORD-D02-01; the old chat surface dies at D09, not here).
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
from mai.capabilities.errors import PreEffectError
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

#: The capability identifier (the executor vocabulary frozen here; D07-fix1
#: F1 renames the read seam to ``session.read`` — the session-first tables
#: are the only authority).
CAPABILITY_ID: Final[str] = "session.read"

#: Bound on the entries summary (a summary, not a dump; the full history is
#: the D06 bounded ``/history`` route's vocabulary).
MAX_EVENTS_SUMMARY: Final[int] = 50


class ChatReadErrorCode(StrEnum):
    """Stable machine-readable capability rejection codes."""

    INVALID_PAYLOAD = "invalid_payload"
    NOT_FOUND = "not_found"


class ChatReadError(PreEffectError):
    """One structured, value-free pre-effect capability rejection."""

    def __init__(self, code: ChatReadErrorCode, detail: str) -> None:
        super().__init__(code.value, detail)
        self.code = code
        self.detail = detail


def chat_read_descriptor() -> CapabilityDescriptor:
    """The validated ``session.read`` descriptor (C03 rule 1).

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
                    "Read a persisted agent session by its canonical session "
                    "UUID and return its projected status, latest model "
                    "profile, service tags, and a bounded entry summary."
                ),
                "required": ["session_id"],
                "properties": {
                    "session_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "The session id (canonical UUID) to read; no "
                            "host vocabulary exists."
                        ),
                    },
                    "server_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "Optional explicit server scope; a session "
                            "stored under a different server is a "
                            "scope-blind not_found (C01 rule 2)."
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
    """``session.read`` executor: one gated call → one honest summary.

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
        # Bounded server-scope validation (C01 rule 2): an explicit payload
        # scope or the context's own scope must MATCH the stored session's
        # server — anything else is the SAME scope-blind not_found (no
        # existence leak across servers). ``None`` context scope (General
        # Consult sentinel / host-agnostic callers) imposes no constraint.
        requested_server = payload.get("server_id")
        if requested_server is not None:
            try:
                requested_server = str(uuid.UUID(str(requested_server)))
            except (ValueError, AttributeError):
                raise ChatReadError(
                    ChatReadErrorCode.INVALID_PAYLOAD,
                    "server_id must be a canonical UUID string",
                ) from None

        replayed = await self._replay(session_id)
        context_server = getattr(call.context, "server_id", None)
        if context_server is not None:
            context_server = str(context_server)  # context carries a UUID
        for scope in (requested_server, context_server):
            if scope is not None and scope != replayed.server_id:
                raise ChatReadError(
                    ChatReadErrorCode.NOT_FOUND,
                    "no session stored for this identity in this scope",
                )
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
        """The confined read: one session row, its latest run's profile, its
        bounded entry summary, and its tag names — the session-first tables
        (``sessions`` / ``session_runs`` / ``session_entries`` /
        ``session_tags``) are the only authority."""
        async with self._sessions() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT session_id::text AS session_id, "
                            "server_id::text AS server_id, status "
                            "FROM sessions WHERE session_id = :s"
                        ),
                        {"s": uuid.UUID(session_id)},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ChatReadError(
                    ChatReadErrorCode.NOT_FOUND,
                    "no session stored for this session id",
                )
            # The latest run's model profile (the run is subordinate to the
            # session; a session with no runs yet reads the default profile).
            profile_row = (
                (
                    await session.execute(
                        text(
                            "SELECT model_profile FROM session_runs "
                            "WHERE session_id = :s "
                            "ORDER BY updated_at DESC, run_id LIMIT 1"
                        ),
                        {"s": uuid.UUID(session_id)},
                    )
                )
                .mappings()
                .first()
            )
            entry_count = (
                await session.execute(
                    text("SELECT COUNT(*) FROM session_entries WHERE session_id = :s"),
                    {"s": uuid.UUID(session_id)},
                )
            ).scalar_one()
            # Bounded summary: the OLDEST 50 entries ascending (the head of
            # the durable log; the newest tail is the /history route's page).
            entry_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT seq, kind FROM session_entries "
                            "WHERE session_id = :s ORDER BY seq "
                            "LIMIT :bound"
                        ),
                        {"s": uuid.UUID(session_id), "bound": MAX_EVENTS_SUMMARY},
                    )
                )
                .mappings()
                .all()
            )
            tag_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT t.name FROM session_tags st "
                            "JOIN tags t ON t.tag_id = st.tag_id "
                            "WHERE st.session_id = :s "
                            "ORDER BY st.created_at, t.name"
                        ),
                        {"s": uuid.UUID(session_id)},
                    )
                )
                .mappings()
                .all()
            )

        summary: list[tuple[int, str]] = [
            (int(entry_row["seq"]), str(entry_row["kind"])) for entry_row in entry_rows
        ]
        service_tags = [str(tag_row["name"]) for tag_row in tag_rows]
        return _ReplayedSession(
            session_id=str(row["session_id"]),
            server_id=str(row["server_id"]),
            state=str(row["status"]),
            model_profile=(
                "default"
                if profile_row is None or profile_row["model_profile"] is None
                else str(profile_row["model_profile"])
            ),
            service_tags=tuple(service_tags),
            event_count=int(entry_count),
            events_summary=tuple(summary),
        )
