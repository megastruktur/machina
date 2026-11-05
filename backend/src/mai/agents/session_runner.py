"""Single-loop durable session runner on DBOS (``agent.session_run``).

Campaign ``machina-session-first-chat`` D04 (sfc_PLAN.md D04; COORD-D02-01:
wired alongside the retained ``agent.run``/``agent.run_v2`` until D09
deletes them). ONE production loop per session run — conversation, tools,
approvals and completion go through this handler, reusing the reviewed
execution infrastructure: ``mai.jobs.steps`` guarded transitions and crash
windows, ``RunLifecycle``, ``CapabilityToolBridge`` as the ONE dispatch
seam, the D03 native history projector and the D02 transactional store.
The run IS the deterministic DBOS workflow id; every model round and every
tool execution is a checkpointed ``@DBOS.step``.

Governing rules:

- **One run-state authority (dual write, one transaction seam)** — every
  transition records BOTH the facade (``RunLifecycle`` transitions on
  ``job_events``) and the session domain (``session_runs.state`` + a
  ``RunStateEntry`` + ``sessions.status``) inside deterministic steps. The
  browser never derives run state from a connection.
- **Crash honesty** — a recovered ``running``-without-receipt tool fence is
  NEVER re-dispatched: it translates to ``outcome_unknown``, the session
  log records ``unknown_effect``, and the run parks on reconciliation
  truth (never an invented success).
- **Approvals park durably** — ``waiting_approval`` rides
  ``DBOS.recv_async`` against the persisted deadline and survives worker
  restarts; the resume presents the payload-bound ``approval_id`` through
  the SAME dispatcher seam, so an altered payload, wrong capability or
  stale policy revision is refused fail-closed before any effect.
- **Controls act at safe boundaries** — steering and stop controls are
  durable ``ControlEntry`` rows consumed exactly once by an idempotent
  acknowledgment entry; the model sees injected notes as bounded, labelled
  user prompts; a stop settles ``cancelled`` in both authorities.
- **Idempotent capture** — assistant exchanges persist under
  ``round:{n}:...`` durable ids, so a crash inside a model round re-runs it
  with zero duplicate entries, and a crash after the checkpoint never
  re-invokes the model.

The module is free of the deleted-at-D09 conversation/task/objective
imports and the harness: the session log is the only history.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Final

from dbos import DBOS
from dbos import error as dbos_error
from pydantic_ai import Agent
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.tools import Tool as PydanticTool
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents.session_history import (
    abuild_session_model_history,
    apersist_run_exchange,
    assert_tool_result_honesty,
)
from mai.agents.tools import BridgeError, CapabilityToolBridge, ToolResult
from mai.contracts.sessions import (
    MAX_ENTRY_TEXT_CHARS,
    AssistantMessageEntry,
    ControlActionKind,
    DocumentAttachRef,
    EntryKind,
    RunbookRef,
    SessionContractError,
    SessionControlRequest,
    SessionControlResponse,
    SessionErrorCode,
    SessionFieldError,
    SessionRunState,
    SessionSourceKind,
    SessionStatus,
    ToolCallItem,
    ToolResultEntry,
    assert_session_not_run,
)
from mai.contracts.tool_results import DomainOutcome, EffectCertainty, ExecutionStatus
from mai.jobs import engine as _engine
from mai.jobs.lifecycle import RunLifecycle, RunState
from mai.jobs.steps import (
    RESUME_TOPIC,
    TERMINAL_STEP_STATES,
    StepExecutor,
    StepIntent,
    StepState,
)
from mai.policy.evaluate import ExecutionContext, execution_context_from_json

__all__ = [
    "DEFAULT_APPROVAL_WAIT_SECONDS",
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_RECONCILE_WAIT_SECONDS",
    "DEFAULT_WAIT_POLL_SECONDS",
    "HANDLER_NAME",
    "MAX_MAX_ROUNDS",
    "RUN_PAYLOAD_VERSION_SESSION",
    "SessionRunError",
    "SessionRunExecutor",
    "SessionRunFailed",
    "SessionRunIntent",
    "asubmit_session_control",
    "configure_session_run",
    "parse_session_run_intent",
    "reset_session_run",
    "run_agent_session",
]

# --- frozen constants ------------------------------------------------------------

HANDLER_NAME: Final[str] = "agent.session_run"

#: Payload schema version; consumers fail closed on any other value.
RUN_PAYLOAD_VERSION_SESSION: Final[int] = 1

DEFAULT_MAX_ROUNDS: Final[int] = 16
MAX_MAX_ROUNDS: Final[int] = 32

DEFAULT_APPROVAL_WAIT_SECONDS: Final[float] = 3600.0
MAX_APPROVAL_WAIT_SECONDS: Final[float] = 86_400.0
DEFAULT_RECONCILE_WAIT_SECONDS: Final[float] = 300.0
MAX_RECONCILE_WAIT_SECONDS: Final[float] = 3_600.0
DEFAULT_WAIT_POLL_SECONDS: Final[float] = 5.0
MIN_WAIT_POLL_SECONDS: Final[float] = 0.5
MAX_WAIT_POLL_SECONDS: Final[float] = 60.0

#: Deterministic tool-step sequence base (per-run ``job_steps`` rows).
TOOL_STEP_SEQ_BASE: Final[int] = 100

#: Tool calls per round bound (the D01 per-message tool-call bound).
MAX_TOOL_CALLS_PER_ROUND: Final[int] = 16

#: Tool calls per RUN budget (facade parity: the bounded tool budget).
MAX_TOOL_CALLS_PER_RUN: Final[int] = 64

#: Steering note bound (the D01 control-note bound).
_STEER_NOTE_MAX: Final[int] = 500

#: Stable prefix labelling injected steering prompts.
_STEER_PROMPT_PREFIX: Final[str] = "[operator steering] "

#: Capture-time placeholder timestamp (UTC epoch); the store's committed
#: ``created_at`` (database ``now()``) is the authoritative value.
_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=timezone.utc)

_ALLOWED_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "payload_version",
        "session_id",
        "run_id",
        "server_id",
        "parent_run_id",
        "client_request_id",
        "source",
        "prompt",
        "context",
        "connection",
        "profile",
        "max_rounds",
        "approval_wait_seconds",
        "reconcile_wait_seconds",
        "wait_poll_seconds",
        "document_ref",
        "runbook_ref",
    }
)


class SessionRunError(Exception):
    """Classified, value-free session-runner failure (wiring or stage)."""

    def __init__(self, reason: str, *, error_type: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error_type = error_type


class SessionStageError(SessionRunError):
    """One classified stage failure (the run records ``failed``)."""


class SessionRunFailed(Exception):
    """The engine-visible classified failure (both sides agree)."""


# --- intent -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionRunIntent:
    """One validated session-run intent (the durable workflow payload)."""

    payload_version: int
    session_id: str
    run_id: str
    server_id: str
    client_request_id: str
    context: ExecutionContext
    prompt: str
    parent_run_id: str | None
    source: SessionSourceKind
    connection: str | None
    profile: str | None
    max_rounds: int
    approval_wait_seconds: float
    reconcile_wait_seconds: float
    wait_poll_seconds: float
    document_ref: DocumentAttachRef | None
    runbook_ref: RunbookRef | None

    def to_json(self) -> dict[str, object]:
        """The canonical intent document (round-trips through ``parse``)."""
        return {
            "payload_version": self.payload_version,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "server_id": self.server_id,
            "client_request_id": self.client_request_id,
            "context": self.context.to_json(),
            "prompt": self.prompt,
            "parent_run_id": self.parent_run_id,
            "source": self.source.value,
            "connection": self.connection,
            "profile": self.profile,
            "max_rounds": self.max_rounds,
            "approval_wait_seconds": self.approval_wait_seconds,
            "reconcile_wait_seconds": self.reconcile_wait_seconds,
            "wait_poll_seconds": self.wait_poll_seconds,
            "document_ref": (
                self.document_ref.model_dump(mode="json")
                if self.document_ref is not None
                else None
            ),
            "runbook_ref": (
                self.runbook_ref.model_dump(mode="json")
                if self.runbook_ref is not None
                else None
            ),
        }


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise SessionStageError(f"invalid_{field}")
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise SessionStageError(f"invalid_{field}") from error


def _bounded_wait(value: object, field: str, default: float, maximum: float) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SessionStageError(f"invalid_{field}")
    number = float(value)
    if not 0.0 < number <= maximum:
        raise SessionStageError(f"invalid_{field}")
    return number


def parse_session_run_intent(payload_json: str) -> SessionRunIntent:
    """Parse and validate one session-run payload (strict, fail-closed).

    Unknown fields are rejected, UUID fields are canonicalized, the embedded
    execution context must name THIS run and server, and the D01 identity
    conflicts are rejected before any effect.
    """
    try:
        document = json.loads(payload_json)
    except ValueError as error:
        raise SessionStageError("invalid_payload") from error
    if not isinstance(document, dict):
        raise SessionStageError("invalid_payload")
    unknown = set(document) - _ALLOWED_PAYLOAD_KEYS
    if unknown:
        raise SessionStageError("invalid_payload")
    if document.get("payload_version") != RUN_PAYLOAD_VERSION_SESSION:
        raise SessionStageError("invalid_payload_version")

    session_id = _canonical_uuid(document.get("session_id"), "session_id")
    run_id = _canonical_uuid(document.get("run_id"), "run_id")
    server_id = _canonical_uuid(document.get("server_id"), "server_id")
    client_request_id = _canonical_uuid(
        document.get("client_request_id"), "client_request_id"
    )
    assert_session_not_run(uuid.UUID(session_id), uuid.UUID(run_id))
    parent_raw = document.get("parent_run_id")
    parent_run_id = (
        None if parent_raw is None else _canonical_uuid(parent_raw, "parent_run_id")
    )
    if parent_run_id is not None and parent_run_id in (run_id, session_id):
        raise SessionStageError("invalid_parent_run_id")

    context_doc = document.get("context")
    if not isinstance(context_doc, dict):
        raise SessionStageError("invalid_context")
    try:
        context = execution_context_from_json(context_doc)
    except Exception as error:  # noqa: BLE001 - classified payload rejection
        raise SessionStageError(
            "invalid_context", error_type=type(error).__name__
        ) from None
    if str(context.run_id) != run_id or str(context.server_id) != server_id:
        raise SessionStageError("context_identity_mismatch")

    prompt = document.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise SessionStageError("invalid_prompt")

    try:
        source = SessionSourceKind(
            str(document.get("source", SessionSourceKind.INTERACTIVE.value))
        )
    except ValueError as error:
        raise SessionStageError("invalid_source") from error

    connection = document.get("connection")
    profile = document.get("profile")
    if connection is not None and not isinstance(connection, str):
        raise SessionStageError("invalid_connection")
    if profile is not None and not isinstance(profile, str):
        raise SessionStageError("invalid_profile")

    parsed_document_ref: DocumentAttachRef | None = None
    parsed_runbook_ref: RunbookRef | None = None
    if document.get("document_ref") is not None:
        try:
            parsed_document_ref = DocumentAttachRef.model_validate(
                document["document_ref"]
            )
        except Exception as error:
            raise SessionStageError("invalid_document_ref") from error
    if document.get("runbook_ref") is not None:
        try:
            parsed_runbook_ref = RunbookRef.model_validate(document["runbook_ref"])
        except Exception as error:
            raise SessionStageError("invalid_runbook_ref") from error

    rounds_raw = document.get("max_rounds")
    max_rounds = DEFAULT_MAX_ROUNDS
    if rounds_raw is not None:
        if not isinstance(rounds_raw, int) or isinstance(rounds_raw, bool):
            raise SessionStageError("invalid_max_rounds")
        if not 1 <= rounds_raw <= MAX_MAX_ROUNDS:
            raise SessionStageError("invalid_max_rounds")
        max_rounds = rounds_raw

    return SessionRunIntent(
        payload_version=RUN_PAYLOAD_VERSION_SESSION,
        session_id=session_id,
        run_id=run_id,
        server_id=server_id,
        client_request_id=client_request_id,
        context=context,
        prompt=prompt[:MAX_ENTRY_TEXT_CHARS],
        parent_run_id=parent_run_id,
        source=source,
        connection=connection,
        profile=profile,
        max_rounds=max_rounds,
        approval_wait_seconds=_bounded_wait(
            document.get("approval_wait_seconds"),
            "approval_wait_seconds",
            DEFAULT_APPROVAL_WAIT_SECONDS,
            MAX_APPROVAL_WAIT_SECONDS,
        ),
        reconcile_wait_seconds=_bounded_wait(
            document.get("reconcile_wait_seconds"),
            "reconcile_wait_seconds",
            DEFAULT_RECONCILE_WAIT_SECONDS,
            MAX_RECONCILE_WAIT_SECONDS,
        ),
        wait_poll_seconds=_bounded_wait(
            document.get("wait_poll_seconds"),
            "wait_poll_seconds",
            DEFAULT_WAIT_POLL_SECONDS,
            MAX_WAIT_POLL_SECONDS,
        ),
        document_ref=parsed_document_ref,
        runbook_ref=parsed_runbook_ref,
    )


def _value_of(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


# --- executor composition ----------------------------------------------------------


class SessionRunExecutor:
    """Composed session-run execution over the reviewed steps machinery.

    Wiring is constructor-checked (fail-closed sibling pattern). The
    row/event/crash-window machinery is the reviewed
    :class:`~mai.jobs.steps.StepExecutor`; this executor adds the session
    seams: native history assembly (D03), the bridge as the ONE dispatch
    seam, and the dual-authority state recording.
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        dispatcher: Any,
        registry: Any,
        model_store: Any,
        data_root: Any,
        locks: Any,
        resolver: Any = None,
        model_builder: Any = None,
    ) -> None:
        from mai.agents.model_store import ModelConfigStore
        from mai.capabilities.dispatch import CapabilityDispatcher
        from mai.jobs.resource_locks import ResourceLockService
        from mai.plugins.registry import CapabilityRegistry

        if not isinstance(sessions, async_sessionmaker):
            raise SessionRunError("invalid_wiring", error_type=TypeError.__name__)
        for name, value, expected in (
            ("dispatcher", dispatcher, CapabilityDispatcher),
            ("registry", registry, CapabilityRegistry),
            ("model_store", model_store, ModelConfigStore),
            ("locks", locks, ResourceLockService),
        ):
            if not isinstance(value, expected):
                raise SessionRunError("invalid_wiring", error_type=TypeError.__name__)
        self._registry = registry
        self._model_store = model_store
        self._data_root = data_root
        self._resolver = resolver
        self._model_builder = model_builder
        self._sessions = sessions
        self._steps = StepExecutor(sessions=sessions, dispatcher=dispatcher, locks=locks)
        self._lifecycle = RunLifecycle(sessions)

    @property
    def steps(self) -> StepExecutor:
        return self._steps

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        return self._sessions

    @property
    def lifecycle(self) -> RunLifecycle:
        return self._lifecycle

    def run(self, coro: object) -> object:
        """Drive one coroutine on the executor's persistent loop."""
        return self._steps.run(coro)

    def close(self) -> None:
        self._steps.close()

    def bridge_of(self, context: ExecutionContext) -> CapabilityToolBridge:
        """The one bridge surface for this run's context (C06 rule 4)."""
        return CapabilityToolBridge(
            registry=self._registry,
            dispatcher=self._steps._dispatcher,  # noqa: SLF001 - composed seam
            context=context,
        )

    def capability_for_tool(self, bridge: CapabilityToolBridge, tool_name: str) -> str:
        """The capability id behind one provider-safe tool name.

        The capture records the provider tool name (the model's surface);
        the dispatcher speaks capability ids. The bridge's enabled set is
        the authoritative mapping (``tool_name_for`` is deterministic); an
        unknown tool name falls through and the dispatcher refuses it
        honestly.
        """
        from mai.agents.tools import tool_name_for

        for capability_id in bridge.enabled_capabilities:
            if tool_name_for(capability_id) == tool_name:
                return capability_id
        return tool_name

    def bind_model(self, intent: SessionRunIntent) -> dict[str, object]:
        """Pin the (connection, profile) pair for this run (checkpointed)."""
        binding = self._model_store.bind(intent.connection, intent.profile)
        return {
            "connection": binding.connection.name,
            "profile": binding.profile.name,
            "model": binding.profile.model,
        }

    def build_model_for(self, pins: Mapping[str, object]) -> Any:
        """Resolve the model for the pinned selection (call-time credentials)."""
        from mai.agents.models import build_model

        selection = self._model_store.bind(
            pins.get("connection"),  # type: ignore[arg-type]
            pins.get("profile"),  # type: ignore[arg-type]
        )
        resolver = self._resolver() if callable(self._resolver) else self._resolver
        if self._model_builder is not None:
            resolved = self._model_builder(selection=selection, resolver=resolver)
        else:
            resolved = build_model(selection, resolver=resolver)
        return resolved.model if hasattr(resolved, "model") else resolved


_EXECUTOR: SessionRunExecutor | None = None


def configure_session_run(executor: SessionRunExecutor) -> None:
    """Inject the composed executor (worker-compose/test path)."""
    global _EXECUTOR
    if not isinstance(executor, SessionRunExecutor):
        raise SessionRunError("invalid_wiring", error_type=TypeError.__name__)
    if _EXECUTOR is not None:
        raise SessionRunError("already_configured")
    _EXECUTOR = executor


def _resolve_session_run() -> SessionRunExecutor:
    if _EXECUTOR is None:
        raise SessionRunError("executor_not_configured")
    return _EXECUTOR


def reset_session_run() -> None:
    """Forget the injected executor (test isolation only)."""
    global _EXECUTOR
    _EXECUTOR = None


# --- session-domain seams (async store writes used by the steps) ---------------------


async def _ensure_run_row(executor: SessionRunExecutor, intent: SessionRunIntent) -> None:
    """Ensure the ``session_runs`` row exists (idempotent; admission usually
    created it — the unique run id is the key)."""
    from mai.session_store import acreate_run, aget_session

    await aget_session(
        executor.sessions,
        uuid.UUID(intent.session_id),
        server_id=uuid.UUID(intent.server_id),
    )
    try:
        await acreate_run(
            executor.sessions,
            session_id=uuid.UUID(intent.session_id),
            server_id=uuid.UUID(intent.server_id),
            run_id=uuid.UUID(intent.run_id),
            model_profile=intent.profile or "default",
            client_request_id=uuid.UUID(intent.client_request_id),
            parent_run_id=(
                uuid.UUID(intent.parent_run_id) if intent.parent_run_id else None
            ),
        )
    except Exception as error:
        # Already created at admission: the run id IS the idempotency key.
        # Any OTHER failure is a defect — logged loudly, never silently
        # swallowed (the run-state write downstream would fail mysteriously).
        if not isinstance(error, SessionContractError) and "unique" not in str(
            error
        ).lower() and "duplicate" not in str(error).lower():
            print(
                f"session runner: ensure_run_row failed: "
                f"{type(error).__name__}: {error}",
                flush=True,
            )
            raise


async def _append_user_turn(
    executor: SessionRunExecutor, intent: SessionRunIntent
) -> None:
    """Append the turn's ``UserMessageEntry`` idempotently (+ admission)."""
    from mai.session_store import aappend_entry, arecord_admission

    payload: dict[str, object] = {
        "kind": EntryKind.USER_MESSAGE.value,
        "text": intent.prompt,
        "client_request_id": intent.client_request_id,
        "source": intent.source.value,
    }
    if intent.document_ref is not None:
        payload["document_ref"] = intent.document_ref.model_dump(mode="json")
    if intent.runbook_ref is not None:
        payload["runbook_ref"] = intent.runbook_ref.model_dump(mode="json")
    await arecord_admission(
        executor.sessions,
        session_id=uuid.UUID(intent.session_id),
        server_id=uuid.UUID(intent.server_id),
        client_request_id=uuid.UUID(intent.client_request_id),
        source=intent.source,
        run_id=uuid.UUID(intent.run_id),
    )
    await aappend_entry(
        executor.sessions,
        session_id=uuid.UUID(intent.session_id),
        server_id=uuid.UUID(intent.server_id),
        kind=EntryKind.USER_MESSAGE,
        payload=payload,
        run_id=uuid.UUID(intent.run_id),
        durable_source_id=f"turn:{intent.client_request_id}",
    )


async def _record_session_state(
    executor: SessionRunExecutor,
    intent: SessionRunIntent,
    state: SessionRunState,
    *,
    reason_code: str | None = None,
    session_status: SessionStatus | None = None,
) -> None:
    """The dual-authority state write: session domain + session header."""
    from mai.session_store import arecord_run_state

    await arecord_run_state(
        executor.sessions,
        session_id=uuid.UUID(intent.session_id),
        server_id=uuid.UUID(intent.server_id),
        run_id=uuid.UUID(intent.run_id),
        state=state,
        reason_code=reason_code,
    )
    if session_status is not None:
        await _set_session_status(executor, intent, session_status)


async def _set_session_status(
    executor: SessionRunExecutor, intent: SessionRunIntent, status: SessionStatus
) -> None:
    async with executor.sessions() as session:
        async with session.begin():
            await session.execute(
                sql_text(
                    "UPDATE sessions SET status = CAST(:status AS varchar), "
                    "updated_at = now() "
                    "WHERE session_id = :sid AND server_id = :server_id"
                ),
                {
                    "status": status.value,
                    "sid": uuid.UUID(intent.session_id),
                    "server_id": uuid.UUID(intent.server_id),
                },
            )


async def _persist_tool_result(
    executor: SessionRunExecutor,
    intent: SessionRunIntent,
    *,
    round_index: int,
    call: Mapping[str, object],
    suffix: str,
    status: str,
    execution_status: ExecutionStatus,
    effect_certainty: EffectCertainty,
    error_code: str | None,
    error_detail: str | None,
    approval_id: str | None,
    payload_hash: str,
    output_preview: str | None,
) -> None:
    """Append one honest ``ToolResultEntry`` for a round's tool call."""
    from mai.session_store import aappend_entry

    payload = ToolResultEntry(
        kind=EntryKind.TOOL_RESULT,
        call_id=str(call["tool_call_id"]),
        tool_name=str(call["tool_name"]),
        capability_id=str(call["capability_id"]),
        status=status,
        execution_status=execution_status,
        domain_outcome=_domain_outcome_for(status),
        effect_certainty=effect_certainty,
        payload_hash=payload_hash,
        output_preview=output_preview,
        error_code=error_code,
        error_detail=error_detail,
        approval_id=uuid.UUID(approval_id) if approval_id else None,
    )
    await aappend_entry(
        executor.sessions,
        session_id=uuid.UUID(intent.session_id),
        server_id=uuid.UUID(intent.server_id),
        kind=EntryKind.TOOL_RESULT,
        payload=payload.model_dump(mode="json"),
        run_id=uuid.UUID(intent.run_id),
        durable_source_id=f"round:{round_index}:tool:{call['tool_call_id']}{suffix}",
    )


def _domain_outcome_for(status: str) -> DomainOutcome:
    """The honest domain outcome for a frozen tool status."""
    if status == "executed":
        return DomainOutcome.SUCCESS
    if status == "denied":
        return DomainOutcome.FAILURE
    return DomainOutcome.UNKNOWN


async def amark_tool_group_state(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    entry_id: uuid.UUID,
    tool_group_state: str,
) -> bool:
    """Flip one assistant entry's ``tool_group_state`` in the durable log.

    The frozen vocabulary is enforced value-free; the row must belong to the
    scoped session (fail-closed join on the server scope). Idempotent:
    marking an already-marked group is a no-op returning ``True``.
    """
    if tool_group_state not in ("none", "pending", "complete", "unresolved"):
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "tool_group_state",
                    "tool_group_state must be one of: none, pending, "
                    "complete, unresolved",
                )
            ]
        )
    async with sessions() as session:
        async with session.begin():
            updated = (
                await session.execute(
                    sql_text(
                        """
                        UPDATE session_entries e
                        SET payload = jsonb_set(
                                e.payload,
                                '{tool_group_state}',
                                to_jsonb(CAST(:state AS varchar))
                            )
                        FROM sessions s
                        WHERE s.session_id = e.session_id
                          AND s.server_id = :server_id
                          AND e.session_id = :sid
                          AND e.entry_id = :eid
                          AND e.kind = 'assistant_message'
                        """
                    ),
                    {
                        "state": tool_group_state,
                        "server_id": server_id,
                        "sid": session_id,
                        "eid": entry_id,
                    },
                )
            ).rowcount
            return bool(updated and updated > 0)


async def aupdate_tool_result_payload(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    run_id: uuid.UUID,
    durable_source_id: str,
    payload: dict[str, object],
) -> bool:
    """Update one keyed ``ToolResultEntry``'s payload in place.

    The crash-window protocol writes the honest interim ``unknown_effect``
    result under the idempotency key and UPDATES the SAME row when the
    reconciliation truth resolves — the atomic tool group keeps exactly one
    result per call (a second entry would poison native replay), and the
    durable log never claims an effect that stayed unproven.
    """
    async with sessions() as session:
        async with session.begin():
            updated = (
                await session.execute(
                    sql_text(
                        """
                        UPDATE session_entries e
                        SET payload = CAST(:payload AS jsonb)
                        FROM sessions s
                        WHERE s.session_id = e.session_id
                          AND s.server_id = :server_id
                          AND e.session_id = :sid
                          AND e.run_id = :rid
                          AND e.durable_source_id = :src
                          AND e.kind = 'tool_result'
                        """
                    ),
                    {
                        "payload": json.dumps(payload),
                        "server_id": server_id,
                        "sid": session_id,
                        "rid": run_id,
                        "src": durable_source_id,
                    },
                )
            ).rowcount
            return bool(updated and updated > 0)


async def _drain_controls(
    executor: SessionRunExecutor, intent: SessionRunIntent
) -> tuple[list[str], str | None]:
    """Consume durable control entries targeted at this run.

    Returns ``(notes, stop)``. STEER notes are injected as labelled user
    prompts (the ack entry IS the injected prompt); PAUSE and CANCEL are
    deterministic boundary stops (acknowledged with a CONTROL-kind entry —
    an operator stop is never synthetic user prose). Consumption is
    recorded by an idempotent acknowledgment entry keyed by the control
    entry's own durable id, so a replayed drain never injects or stops
    twice.
    """
    from mai.session_store import aappend_entry

    query = sql_text(
        """
        SELECT e.entry_id, e.durable_source_id, e.payload
        FROM session_entries e
        WHERE e.session_id = :sid
          AND e.kind = 'control'
          AND e.payload->>'action' IN ('steer', 'pause', 'cancel')
          AND (e.payload->>'target_run_id' = :rid
               OR e.payload->>'target_run_id' IS NULL)
          AND NOT EXISTS (
              SELECT 1 FROM session_entries ack
              WHERE ack.session_id = e.session_id
                AND ack.durable_source_id = 'steer:' || e.durable_source_id
          )
        ORDER BY e.seq
        LIMIT 16
        """
    )
    async with executor.sessions() as session:
        rows = (
            (
                await session.execute(
                    query,
                    {"sid": uuid.UUID(intent.session_id), "rid": intent.run_id},
                )
            )
            .mappings()
            .all()
        )
    notes: list[str] = []
    stop: str | None = None
    for row in rows:
        payload = row["payload"]
        action = payload.get("action") if isinstance(payload, dict) else None
        if action in ("pause", "cancel"):
            # Deterministic boundary stop (m2: acknowledged as a CONTROL
            # entry, never turned into synthetic user prose).
            stop = str(action)
            note = str(payload.get("note") or action)
            await aappend_entry(
                executor.sessions,
                session_id=uuid.UUID(intent.session_id),
                server_id=uuid.UUID(intent.server_id),
                kind=EntryKind.CONTROL,
                payload={
                    "kind": EntryKind.CONTROL.value,
                    "action": action,
                    "client_request_id": str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL, f"ack:{row['durable_source_id']}"
                        )
                    ),
                    "target_run_id": intent.run_id,
                    "approval_id": None,
                    "note": f"acknowledged: {note[:200]}",
                },
                run_id=uuid.UUID(intent.run_id),
                durable_source_id=f"steer:{row['durable_source_id']}",
            )
            continue
        note = payload.get("note") if isinstance(payload, dict) else None
        note = (
            note[:_STEER_NOTE_MAX]
            if isinstance(note, str) and note.strip()
            else "steering note"
        )
        notes.append(note)
        await aappend_entry(
            executor.sessions,
            session_id=uuid.UUID(intent.session_id),
            server_id=uuid.UUID(intent.server_id),
            kind=EntryKind.USER_MESSAGE,
            payload={
                "kind": EntryKind.USER_MESSAGE.value,
                "text": f"{_STEER_PROMPT_PREFIX}{note}",
                "client_request_id": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, str(row["durable_source_id"]))
                ),
                "source": SessionSourceKind.API.value,
            },
            run_id=uuid.UUID(intent.run_id),
            durable_source_id=f"steer:{row['durable_source_id']}",
        )
    return notes, stop


async def _round_model(
    executor: SessionRunExecutor,
    intent: SessionRunIntent,
    pins: Mapping[str, object],
    round_index: int,
) -> dict[str, object]:
    """The async model-round body: native history → agent → durable capture."""
    history = await abuild_session_model_history(
        executor.sessions,
        session_id=uuid.UUID(intent.session_id),
        server_id=uuid.UUID(intent.server_id),
        provider=str(pins.get("connection", "")),
        model_profile=str(pins.get("profile", "")),
    )
    bridge = executor.bridge_of(intent.context)
    resolved_model = executor.build_model_for(pins)

    def _deferring(tool: PydanticTool) -> PydanticTool:
        # The reviewed deferral wrapper (run.py pattern): the tool SURFACE
        # stays the bridge's; execution moves to the durable seam.
        definition = tool.tool_def
        from pydantic_ai.exceptions import CallDeferred

        def _defer(**_payload: Any) -> Any:
            raise CallDeferred

        return PydanticTool.from_schema(
            _defer,
            name=definition.name,
            description=definition.description or "",
            json_schema=json.loads(json.dumps(definition.parameters_json_schema)),
        )

    agent: Agent[None, Any] = Agent(
        model=resolved_model,
        tools=[_deferring(tool) for tool in bridge.tools],
        output_type=[str, DeferredToolRequests],
    )
    try:
        result = await agent.run(message_history=list(history.messages))
    except Exception as error:
        # Fail loudly: the classified stage error is durable, but the root
        # cause must be immediately visible in the worker log too.
        import sys as _sys

        print(
            f"session runner: model round failed: "
            f"{type(error).__name__}: {error}",
            file=_sys.stderr,
            flush=True,
        )
        raise SessionStageError(
            "model_round_failed", error_type=type(error).__name__
        ) from None

    persisted = await apersist_run_exchange(
        executor.sessions,
        new_messages=list(result.new_messages()),
        session_id=uuid.UUID(intent.session_id),
        server_id=uuid.UUID(intent.server_id),
        run_id=uuid.UUID(intent.run_id),
        model_profile=str(pins.get("profile", "")),
        provider=str(pins.get("connection", "")),
        durable_prefix=f"round:{round_index}",
    )
    calls: list[dict[str, object]] = []
    assistant_entry_id: str | None = None
    for entry in persisted:
        payload = entry.payload
        if isinstance(payload, AssistantMessageEntry) and payload.tool_calls:
            assistant_entry_id = str(entry.entry_id)
            for call_index, call in enumerate(
                payload.tool_calls[:MAX_TOOL_CALLS_PER_ROUND]
            ):
                calls.append(
                    {
                        "tool_call_id": call.call_id,
                        "tool_name": call.tool_name,
                        "capability_id": executor.capability_for_tool(
                            bridge, call.tool_name
                        ),
                        "args": call.args,
                        "entry_id": str(entry.entry_id),
                        "seq": TOOL_STEP_SEQ_BASE
                        + round_index * MAX_TOOL_CALLS_PER_ROUND
                        + call_index,
                    }
                )
    output_text = result.output if isinstance(result.output, str) else ""
    return {
        "status": "tool_calls" if calls else "final",
        "assistant_entry_id": assistant_entry_id,
        "calls": calls,
        "output_text": output_text,
        "round": round_index,
    }


# --- the DBOS steps (sync; async seams run on the executor's one loop) ---------------


@DBOS.step(name="agents.session.started")
def _session_started(payload_json: str) -> str:
    """Anchor ``queued→running`` (facade), record the dual-authority
    ``running`` state and append the idempotent user turn + admission."""
    intent = parse_session_run_intent(payload_json)
    executor = _resolve_session_run()
    executor.run(_ensure_run_row(executor, intent))
    executor.run(executor.steps.aobserve_run_started(intent.run_id))
    executor.run(
        _record_session_state(
            executor,
            intent,
            SessionRunState.RUNNING,
            session_status=SessionStatus.RUNNING,
        )
    )
    executor.run(_append_user_turn(executor, intent))
    return json.dumps({"session_id": intent.session_id, "run_id": intent.run_id})


@DBOS.step(name="agents.session.model_bound")
def _session_model_bound(payload_json: str) -> str:
    """Pin the (connection, profile) pair (checkpointed)."""
    intent = parse_session_run_intent(payload_json)
    return json.dumps(_resolve_session_run().bind_model(intent))


@DBOS.step(name="agents.session.controls_drained")
def _session_controls_drained(payload_json: str) -> str:
    """Consume durable steering/pause/cancel controls at a safe boundary.

    Returns ``{"notes": [...], "stop": "cancel"|"pause"|None}``
    (checkpointed).
    """
    intent = parse_session_run_intent(payload_json)
    executor = _resolve_session_run()
    notes, stop = executor.run(_drain_controls(executor, intent))
    return json.dumps({"notes": notes, "stop": stop})


@DBOS.step(name="agents.session.model_round")
def _session_model_round(payload_json: str, pins_json: str, round_index: int) -> str:
    """One durable model round (checkpointed).

    A crash BEFORE the checkpoint safely re-runs the round; the store's
    ``(run_id, durable_source_id, kind)`` key guarantees zero duplicate
    assistant entries. A crash AFTER the checkpoint never re-invokes the
    model.
    """
    intent = parse_session_run_intent(payload_json)
    executor = _resolve_session_run()
    record = executor.run(
        _round_model(executor, intent, json.loads(pins_json), round_index)
    )
    return json.dumps(record)


@DBOS.step(name="agents.session.tool_execute")
def _session_tool_execute(payload_json: str, call_json: str, round_index: int) -> str:
    """One durable guarded tool execution (checkpointed).

    The reviewed fence protocol: intent row → ``running`` fence → dispatch.
    A recovered ``running``-without-receipt fence is NEVER re-dispatched: it
    translates to ``outcome_unknown`` and the session log records the honest
    ``unknown_effect`` result (reconciliation decides the truth).
    """
    intent = parse_session_run_intent(payload_json)
    executor = _resolve_session_run()
    call = json.loads(call_json)
    seq = int(call["seq"])
    capability_id = str(call["capability_id"])
    spec = StepIntent(seq=seq, capability_id=capability_id, payload=dict(call["args"]))

    row = executor.run(executor.steps.aensure_step_intent(intent.run_id, spec))
    state = str(row.get("state", StepState.PENDING.value))

    if state in {item.value for item in TERMINAL_STEP_STATES}:
        # A replayed attempt observes the committed row instead of a second
        # effect (the row IS the exactly-once ledger). A SIGKILL between the
        # outcome commit and the session append leaves the call WITHOUT a
        # session result: repair it from the committed receipt, idempotently
        # keyed (M2). An approval-refusal receipt is skipped — its resume
        # path owns the result entry.
        outcome = executor.run(
            _repair_replayed_terminal_result(
                executor, intent=intent, round_index=round_index, call=call, row=row
            )
        )
        return json.dumps(outcome)
    if state == StepState.RUNNING.value:
        # Crash window: the previous worker died mid-effect holding the
        # fence. NEVER re-dispatch; translate honestly.
        outcome = executor.run(
            executor.steps.acrash_window_outcome(
                intent.run_id, intent.server_id, seq, spec
            )
        )
        receipt = outcome.get("receipt") or {}
        executor.run(
            _persist_tool_result(
                executor,
                intent,
                round_index=round_index,
                call=call,
                suffix="",
                status="unknown_effect",
                execution_status=ExecutionStatus.OUTCOME_UNKNOWN,
                effect_certainty=EffectCertainty.UNKNOWN,
                error_code="crash_window",
                error_detail="effect unproven after worker crash",
                approval_id=None,
                payload_hash=str(receipt.get("payload_hash", "")),
                output_preview=None,
            )
        )
        return json.dumps(
            {
                "seq": seq,
                "state": StepState.OUTCOME_UNKNOWN.value,
                "status": "unknown_effect",
                "approval_id": None,
                "payload_hash": str(receipt.get("payload_hash", "")),
            }
        )

    executor.run(executor.steps.amark_running(intent.run_id, seq, capability_id))
    bridge = executor.bridge_of(intent.context)
    from mai.capabilities.dispatch import canonical_payload_hash

    try:
        outcome = executor.run(
            bridge.dispatch_once(capability_id, dict(call["args"]))
        )
    except BridgeError as error:
        # A bridge refusal AFTER the fence (schema/target gates): the
        # honest running->failed denial, durable in both authorities.
        executor.run(
            executor.steps.apersist_outcome(
                intent.run_id,
                seq,
                capability_id,
                StepState.FAILED,
                {
                    "v": 1,
                    "reason": f"bridge_{error.code.value}",
                    "effect": "not_attempted",
                    "capability_id": capability_id,
                    "payload_hash": canonical_payload_hash(spec.payload),
                },
                reason=f"bridge_{error.code.value}",
                op_id=None,
            )
        )
        executor.run(
            _persist_tool_result(
                executor,
                intent,
                round_index=round_index,
                call=call,
                suffix="",
                status="denied",
                execution_status=ExecutionStatus.FAILED,
                effect_certainty=EffectCertainty.CERTAIN,
                error_code=error.code.value,
                error_detail=None,
                approval_id=None,
                payload_hash=canonical_payload_hash(spec.payload),
                output_preview=None,
            )
        )
        return json.dumps(
            {
                "seq": seq,
                "state": StepState.FAILED.value,
                "status": "denied",
                "approval_id": None,
                "payload_hash": canonical_payload_hash(spec.payload),
            }
        )

    result = _entry_from_bridge_outcome(outcome, str(call["tool_call_id"]))
    from mai.capabilities.dispatch import canonical_payload_hash

    receipt = {
        "v": 1,
        "op_id": (
            str(outcome.op_id) if getattr(outcome, "op_id", None) is not None else None
        ),
        "capability_id": capability_id,
        "allowed": bool(outcome.allowed),
        "reason": str(outcome.reason),
        "effect": str(outcome.effect),
        "payload_hash": canonical_payload_hash(spec.payload),
        "policy_revision": outcome.policy_revision,
        "executor_ok": outcome.executor_ok,
        "approval_id": (
            str(outcome.approval_id) if outcome.approval_id is not None else None
        ),
        "approval_spent": bool(outcome.approval_spent),
    }
    if result.status == "approval_required" and receipt["approval_id"] is None:
        # The reviewed runner seam (C03 rule 4): the ask-mode refusal IS the
        # durable binding surface — create the payload-bound approval
        # request so the park/resume has ONE id to present. The dispatcher
        # binds the consumption to (server, capability, payload hash,
        # policy revision) at resume time.
        created_id = executor.run(
            _create_approval_request(
                executor,
                intent=intent,
                capability_id=capability_id,
                payload_hash=str(receipt["payload_hash"]),
                policy_revision=receipt["policy_revision"],
            )
        )
        receipt["approval_id"] = created_id
    if result.status == "approval_required":
        # The refused attempt closed WITHOUT running (nothing to verify):
        # the honest step row is failed, exactly like the reviewed runner's
        # approval-refusal receipt.
        target_state = StepState.FAILED
    elif result.status == "executed":
        target_state = StepState.SUCCEEDED
    elif result.status == "denied":
        target_state = StepState.FAILED
    else:
        target_state = StepState.OUTCOME_UNKNOWN
    executor.run(
        executor.steps.apersist_outcome(
            intent.run_id,
            seq,
            capability_id,
            target_state,
            receipt,
            reason=str(outcome.reason) or None,
            op_id=receipt["op_id"],
        )
    )
    if result.status != "approval_required":
        # The approval-refused attempt records its binding on the step row
        # (+ the waiting run state); the RESUME outcome lands the call's
        # single result entry, so the atomic group stays exactly-once.
        executor.run(
            _persist_projected_result(
                executor,
                intent,
                round_index=round_index,
                call=call,
                entry=result,
                suffix="",
            )
        )
    return json.dumps(
        {
            "seq": seq,
            "state": target_state.value,
            "status": result.status,
            "approval_id": (
                receipt["approval_id"]
                if result.status == "approval_required"
                else (
                    str(result.approval_id)
                    if result.approval_id is not None
                    else None
                )
            ),
            "payload_hash": result.payload_hash,
        }
    )


def _entry_from_bridge_outcome(outcome: Any, call_id: str) -> ToolResultEntry:
    """Project one bridge receipt onto the D01 contract (runtime path).

    The bridge receipt IS the proof of execution (``effect == "executed"`` +
    ``executor_ok``): the D04 runner maps it directly instead of re-parsing
    untrusted output text for structured evidence (the D03 projector's
    evidence downgrade applies to replay-window projections, not to the
    runtime receipt whose authority is the dispatcher's own guarded
    outcome). The anti-forgery guard still validates the entry before it is
    persisted.
    """
    from mai.agents.tools import ToolStatus as BridgeToolStatus

    tool_result = ToolResult.from_outcome(outcome)
    if tool_result.status is BridgeToolStatus.EXECUTED and tool_result.executor_ok is True:
        execution_status = ExecutionStatus.SUCCEEDED
        effect_certainty = EffectCertainty.CERTAIN
        domain = DomainOutcome.SUCCESS
        status = "executed"
    elif tool_result.status is BridgeToolStatus.APPROVAL_REQUIRED:
        status = "approval_required"
        execution_status = ExecutionStatus.FAILED
        effect_certainty = EffectCertainty.UNKNOWN
        domain = DomainOutcome.UNKNOWN
    elif tool_result.status is BridgeToolStatus.UNKNOWN_EFFECT:
        status = "unknown_effect"
        execution_status = ExecutionStatus.OUTCOME_UNKNOWN
        effect_certainty = EffectCertainty.UNKNOWN
        domain = DomainOutcome.UNKNOWN
    else:
        status = "denied"
        execution_status = ExecutionStatus.FAILED
        effect_certainty = EffectCertainty.CERTAIN
        domain = DomainOutcome.FAILURE
    entry = ToolResultEntry(
        kind=EntryKind.TOOL_RESULT,
        call_id=call_id,
        tool_name=tool_result.capability_id,
        capability_id=tool_result.capability_id,
        status=status,
        execution_status=execution_status,
        domain_outcome=domain,
        effect_certainty=effect_certainty,
        payload_hash=tool_result.payload_hash,
        output_preview=tool_result.output_text,
        error_code=tool_result.error_code,
        error_detail=tool_result.error_detail,
        approval_id=tool_result.approval_id,
    )
    assert_tool_result_honesty(entry)
    return entry


async def _create_approval_request(
    executor: SessionRunExecutor,
    *,
    intent: SessionRunIntent,
    capability_id: str,
    payload_hash: str,
    policy_revision: object,
) -> str | None:
    """Create the payload-bound approval for one ask-mode refusal.

    Returns the approval id, or ``None`` when creation failed/is impossible
    (no store composed, malformed binding) — the honest refusal already
    stands and no park happens without a presentable binding.
    """
    from datetime import timedelta

    from mai.policy.approvals import ApprovalRequest

    approvals_store = getattr(executor.steps._dispatcher, "_approvals", None)
    if approvals_store is None:
        return None
    try:
        created = await approvals_store.create(
            ApprovalRequest(
                capability_id=capability_id,
                server_id=uuid.UUID(intent.server_id),
                payload_hash=payload_hash,
                policy_revision=int(str(policy_revision)),
                created_by=f"agent.session_run:{intent.run_id}",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        return str(created.approval_id)
    except Exception as error:  # noqa: BLE001 - no park without a binding
        import sys as _sys

        print(
            f"session runner: approval creation failed: "
            f"{type(error).__name__}: {error}",
            file=_sys.stderr,
            flush=True,
        )
        return None


async def _persist_projected_result(
    executor: SessionRunExecutor,
    intent: SessionRunIntent,
    *,
    round_index: int,
    call: Mapping[str, object],
    entry: ToolResultEntry,
    suffix: str,
) -> None:
    """Append the projected ``ToolResultEntry`` (idempotently keyed)."""
    from mai.session_store import aappend_entry

    await aappend_entry(
        executor.sessions,
        session_id=uuid.UUID(intent.session_id),
        server_id=uuid.UUID(intent.server_id),
        kind=EntryKind.TOOL_RESULT,
        payload=entry.model_dump(mode="json"),
        run_id=uuid.UUID(intent.run_id),
        durable_source_id=f"round:{round_index}:tool:{entry.call_id}{suffix}",
    )


@DBOS.step(name="agents.session.reconciling")
def _session_reconciling(payload_json: str, call_json: str) -> str:
    """Enter ``reconciling`` ONCE (dual authority + persisted deadline)."""
    intent = parse_session_run_intent(payload_json)
    executor = _resolve_session_run()
    call = json.loads(call_json)
    seq = int(call["seq"])
    deadline = executor.run(
        executor.steps.aobserve_enter_wait(
            intent.run_id,
            target=RunState.RECONCILING,
            event_type="agent.awaiting_reconciliation",
            event_data={"capability_id": str(call["capability_id"])},
            wait_seconds=intent.reconcile_wait_seconds,
            step_seq=seq,
        )
    )
    executor.run(
        _record_session_state(
            executor,
            intent,
            SessionRunState.OUTCOME_UNKNOWN,
            reason_code="reconciling",
        )
    )
    return deadline


@DBOS.step(name="agents.session.reconcile_resolved")
def _session_reconcile_resolved(
    payload_json: str, call_json: str, round_index: int, timed_out: bool
) -> str:
    """Update the keyed ``unknown_effect`` entry to the resolved truth.

    The reconciler's terminal row (or the honest expiry) decides; the SAME
    durable entry is updated in place so the atomic tool group keeps exactly
    one result per call and the model window replays the truth, never an
    invented success.
    """
    intent = parse_session_run_intent(payload_json)
    executor = _resolve_session_run()
    call = json.loads(call_json)
    seq = int(call["seq"])
    row = executor.run(executor.steps.aread_step(intent.run_id, seq))
    receipt: dict[str, object] = {}
    row_state = "failed"
    if row is not None:
        row_state = str(row["state"])
        raw = row.get("receipt")
        if isinstance(raw, Mapping):
            receipt = dict(raw)
    if not timed_out and row_state == StepState.SUCCEEDED.value:
        # The D03 honesty contract: "executed" requires a proven successful
        # execution (executor_ok is True). A crash-window row resolved by
        # the reconciler WITHOUT a remote execution receipt stays an honest
        # unknown_effect (execution succeeded, effect evidence unproven).
        proven = receipt.get("executor_ok") is True and receipt.get("effect") == "executed"
        status = "executed" if proven else "unknown_effect"
        execution_status = ExecutionStatus.SUCCEEDED
        certainty = EffectCertainty.CERTAIN if proven else EffectCertainty.UNCERTAIN
        error_code = None
    else:
        status = "unknown_effect"
        execution_status = (
            ExecutionStatus.FAILED if row_state == StepState.FAILED.value else ExecutionStatus.OUTCOME_UNKNOWN
        )
        certainty = (
            EffectCertainty.CERTAIN
            if row_state == StepState.FAILED.value and receipt.get("effect") == "not_attempted"
            else EffectCertainty.UNKNOWN
        )
        error_code = "reconcile_timeout" if timed_out else None
    payload_entry = ToolResultEntry(
        kind=EntryKind.TOOL_RESULT,
        call_id=str(call["tool_call_id"]),
        tool_name=str(call["tool_name"]),
        capability_id=str(call["capability_id"]),
        status=status,
        execution_status=execution_status,
        domain_outcome=_domain_outcome_for(status) if status == "executed" else DomainOutcome.UNKNOWN,
        effect_certainty=certainty,
        payload_hash=str(receipt.get("payload_hash", "")),
        output_preview=None,
        error_code=error_code,
        error_detail=(
            "reconciliation expired before the effect truth resolved"
            if timed_out
            else None
        ),
        approval_id=None,
    )
    executor.run(
        aupdate_tool_result_payload(
            executor.sessions,
            session_id=uuid.UUID(intent.session_id),
            server_id=uuid.UUID(intent.server_id),
            run_id=uuid.UUID(intent.run_id),
            durable_source_id=f"round:{round_index}:tool:{call['tool_call_id']}",
            payload=payload_entry.model_dump(mode="json"),
        )
    )
    return json.dumps(
        {
            "row_state": row_state,
            "status": status,
            "timed_out": timed_out,
            "cancelled": row_state == StepState.CANCELLED.value,
        }
    )


@DBOS.step(name="agents.session.reconcile_expired")
def _session_reconcile_expired(
    payload_json: str, call_json: str, round_index: int
) -> str:
    """The honest expiry observation (checkpointed; never a re-dispatch)."""
    return _session_reconcile_resolved(payload_json, call_json, round_index, True)


async def _settle_unknown_session(
    intent: SessionRunIntent,
    call_json: str,
    round_index: int,
) -> dict[str, object]:
    """Park the durable workflow until the unknown effect's truth resolves.

    Mirrors the reviewed runner's reconciliation park: the persisted
    deadline bounds the wait, the durable step row decides, and a resolved
    truth updates the SAME session entry (never a retry, never an invented
    success). A ``cancelled`` resolution settles the run cancelled.
    """
    payload_json = json.dumps(intent.to_json())
    deadline_iso = _session_reconciling(payload_json, call_json)
    from datetime import datetime as _dt

    deadline = _dt.fromisoformat(deadline_iso)
    while True:
        row_json = _session_read_step(payload_json, intent.run_id, call_json)
        row = json.loads(row_json)
        if row is not None and row.get("state") in (
            StepState.SUCCEEDED.value,
            StepState.FAILED.value,
            StepState.CANCELLED.value,
        ):
            _session_wait_resumed(payload_json, RunState.RECONCILING.value)
            settled = json.loads(
                _session_reconcile_resolved(
                    payload_json, call_json, round_index, False
                )
            )
            if settled.get("cancelled"):
                _session_settled(
                    payload_json,
                    json.dumps({"state": "cancelled", "reason": "step_cancelled"}),
                )
                raise SessionRunFailed("step_cancelled")
            return settled
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            settled = json.loads(
                _session_reconcile_expired(payload_json, call_json, round_index)
            )
            _session_wait_resumed(payload_json, RunState.RECONCILING.value)
            return settled
        await DBOS.recv_async(
            topic=RESUME_TOPIC,
            timeout_seconds=min(
                intent.wait_poll_seconds,
                max(MIN_WAIT_POLL_SECONDS, remaining),
            ),
        )


@DBOS.step(name="agents.session.read_step")
def _session_read_step(payload_json: str, run_id: str, call_json: str) -> str:
    intent = parse_session_run_intent(payload_json)
    executor = _resolve_session_run()
    call = json.loads(call_json)
    row = executor.run(executor.steps.aread_step(run_id, int(call["seq"])))
    if row is None:
        return "null"
    receipt = row.get("receipt")
    return json.dumps(
        {
            "state": row.get("state"),
            "receipt": dict(receipt) if isinstance(receipt, Mapping) else {},
        }
    )


async def _repair_replayed_terminal_result(
    executor: SessionRunExecutor,
    *,
    intent: SessionRunIntent,
    round_index: int,
    call: Mapping[str, object],
    row: Mapping[str, object],
) -> dict[str, object]:
    """Re-append the session result for an already-terminal step row (M2).

    A SIGKILL between ``apersist_outcome`` and ``_persist_projected_result``
    commits the effect truth in ``job_steps`` but leaves ``session_entries``
    without the call's result — the session-append seam. This repair
    reconstructs the honest ``ToolResultEntry`` from the COMMITTED receipt
    and appends it under the idempotency key (the store dedupes a replay),
    so the executed exchange is never silently absent from subsequent native
    history. An approval-refusal receipt is skipped: its RESUME path owns
    the call's single result entry (base/denied/approved suffixes).
    """
    seq = int(call["seq"])
    capability_id = str(call["capability_id"])
    receipt = row.get("receipt") if isinstance(row.get("receipt"), Mapping) else {}
    row_state = str(row.get("state", StepState.FAILED.value))
    reason = str(receipt.get("reason", "")) if receipt else ""
    if reason == "approval_required_but_absent":
        return {
            "seq": seq,
            "state": row_state,
            "status": "approval_required",
            "approval_id": (
                str(receipt.get("approval_id"))
                if receipt.get("approval_id")
                else None
            ),
            "payload_hash": str(receipt.get("payload_hash", "")),
        }

    proven = (
        receipt.get("executor_ok") is True and receipt.get("effect") == "executed"
    )
    if row_state == StepState.SUCCEEDED.value and proven:
        status = "executed"
        execution_status = ExecutionStatus.SUCCEEDED
        certainty = EffectCertainty.CERTAIN
        domain = DomainOutcome.SUCCESS
    elif receipt.get("effect") == "not_attempted" or row_state == StepState.FAILED.value:
        status = "denied"
        execution_status = ExecutionStatus.FAILED
        certainty = EffectCertainty.CERTAIN
        domain = DomainOutcome.FAILURE
    else:
        status = "unknown_effect"
        execution_status = ExecutionStatus.OUTCOME_UNKNOWN
        certainty = EffectCertainty.UNKNOWN
        domain = DomainOutcome.UNKNOWN
    entry = ToolResultEntry(
        kind=EntryKind.TOOL_RESULT,
        call_id=str(call["tool_call_id"]),
        tool_name=capability_id,
        capability_id=capability_id,
        status=status,
        execution_status=execution_status,
        domain_outcome=domain,
        effect_certainty=certainty,
        payload_hash=str(receipt.get("payload_hash", "")),
        output_preview=None,
        error_code=reason or None,
        error_detail=None,
        approval_id=(
            uuid.UUID(str(receipt["approval_id"]))
            if receipt.get("approval_id")
            else None
        ),
    )
    assert_tool_result_honesty(entry)
    from mai.session_store import aappend_entry

    await aappend_entry(
        executor.sessions,
        session_id=uuid.UUID(intent.session_id),
        server_id=uuid.UUID(intent.server_id),
        kind=EntryKind.TOOL_RESULT,
        payload=entry.model_dump(mode="json"),
        run_id=uuid.UUID(intent.run_id),
        durable_source_id=f"round:{round_index}:tool:{entry.call_id}",
    )
    return {
        "seq": seq,
        "state": row_state,
        "status": status,
        "approval_id": receipt.get("approval_id"),
        "payload_hash": str(receipt.get("payload_hash", "")),
    }


@DBOS.step(name="agents.session.approval_wait_entered")
def _session_approval_wait(payload_json: str, call_json: str, approval_id: str) -> str:
    """Enter ``waiting_approval`` ONCE (dual authority + persisted deadline)."""
    intent = parse_session_run_intent(payload_json)
    executor = _resolve_session_run()
    call = json.loads(call_json)
    seq = int(call["seq"])
    deadline = executor.run(
        executor.steps.aobserve_enter_wait(
            intent.run_id,
            target=RunState.WAITING_APPROVAL,
            event_type="agent.awaiting_approval",
            event_data={
                "capability_id": str(call["capability_id"]),
                "approval_id": approval_id,
            },
            wait_seconds=intent.approval_wait_seconds,
            step_seq=seq,
        )
    )
    executor.run(
        _record_session_state(
            executor,
            intent,
            SessionRunState.WAITING_APPROVAL,
            reason_code=f"approval:{approval_id}",
            session_status=SessionStatus.WAITING_APPROVAL,
        )
    )
    return deadline


@DBOS.step(name="agents.session.wait_resumed")
def _session_wait_resumed(payload_json: str, from_state: str) -> str:
    intent = parse_session_run_intent(payload_json)
    executor = _resolve_session_run()
    state = executor.run(
        executor.steps.aobserve_wait_resumed(intent.run_id, from_state)
    )
    executor.run(
        _record_session_state(
            executor,
            intent,
            SessionRunState.RUNNING,
            session_status=SessionStatus.RUNNING,
        )
    )
    return state


#: Deterministic resume-attempt sequence base (approval executions run on
#: their own ``job_steps`` rows; the refused attempt row stays FAILED).
RESUME_SEQ_BASE: Final[int] = 1000


def _resume_seq(call: Mapping[str, object], round_index: int) -> int:
    original = int(call["seq"])
    ordinal = original - TOOL_STEP_SEQ_BASE - round_index * MAX_TOOL_CALLS_PER_ROUND
    return RESUME_SEQ_BASE + round_index * MAX_TOOL_CALLS_PER_ROUND + max(0, ordinal)


@DBOS.step(name="agents.session.approval_executed")
def _session_approval_execute(
    payload_json: str, call_json: str, approval_id: str, round_index: int
) -> str:
    """Resume ONE payload-bound approval through the dispatcher seam.

    The dispatcher binds ``approval_id`` to (server, capability, payload
    hash, policy revision): an altered payload, different scope or stale
    revision is refused fail-closed BEFORE any effect. The resume attempt
    runs on its OWN step row (the refused attempt row stays FAILED).
    """
    intent = parse_session_run_intent(payload_json)
    executor = _resolve_session_run()
    call = json.loads(call_json)
    bridge = executor.bridge_of(intent.context)
    spec = StepIntent(
        seq=_resume_seq(call, round_index),
        capability_id=str(call["capability_id"]),
        payload=dict(call["args"]),
    )
    executor.run(executor.steps.aensure_step_intent(intent.run_id, spec))
    executor.run(
        executor.steps.amark_running(intent.run_id, spec.seq, spec.capability_id)
    )
    try:
        outcome = executor.run(
            bridge.dispatch_once(
                str(call["capability_id"]),
                dict(call["args"]),
                approval_id=uuid.UUID(approval_id),
            )
        )
    except BridgeError as error:
        from mai.capabilities.dispatch import canonical_payload_hash

        executor.run(
            executor.steps.apersist_outcome(
                intent.run_id,
                spec.seq,
                spec.capability_id,
                StepState.FAILED,
                {
                    "v": 1,
                    "reason": f"bridge_{error.code.value}",
                    "effect": "not_attempted",
                    "capability_id": spec.capability_id,
                    "payload_hash": canonical_payload_hash(spec.payload),
                },
                reason=f"bridge_{error.code.value}",
                op_id=None,
            )
        )
        executor.run(
            _persist_tool_result(
                executor,
                intent,
                round_index=round_index,
                call=call,
                suffix=":approved",
                status="denied",
                execution_status=ExecutionStatus.FAILED,
                effect_certainty=EffectCertainty.CERTAIN,
                error_code=error.code.value,
                error_detail=None,
                approval_id=approval_id,
                payload_hash=canonical_payload_hash(spec.payload),
                output_preview=None,
            )
        )
        return json.dumps(
            {"state": "failed", "status": "denied", "reason": f"bridge_{error.code.value}"}
        )
    result = _entry_from_bridge_outcome(outcome, str(call["tool_call_id"]))
    from mai.capabilities.dispatch import canonical_payload_hash

    receipt = {
        "v": 1,
        "op_id": (
            str(outcome.op_id) if getattr(outcome, "op_id", None) is not None else None
        ),
        "capability_id": spec.capability_id,
        "allowed": bool(outcome.allowed),
        "reason": str(outcome.reason),
        "effect": str(outcome.effect),
        "payload_hash": canonical_payload_hash(spec.payload),
        "policy_revision": outcome.policy_revision,
        "executor_ok": outcome.executor_ok,
        "approval_id": approval_id,
        "approval_spent": bool(outcome.approval_spent),
    }
    if result.status == "approval_required":
        # The refused attempt closed WITHOUT running (nothing to verify):
        # the honest step row is failed, exactly like the reviewed runner's
        # approval-refusal receipt.
        target_state = StepState.FAILED
    elif result.status == "executed":
        target_state = StepState.SUCCEEDED
    elif result.status == "denied":
        target_state = StepState.FAILED
    else:
        target_state = StepState.OUTCOME_UNKNOWN
    executor.run(
        executor.steps.apersist_outcome(
            intent.run_id,
            spec.seq,
            spec.capability_id,
            target_state,
            receipt,
            reason=str(outcome.reason) or None,
            op_id=receipt["op_id"],
        )
    )
    executor.run(
        _persist_projected_result(
            executor,
            intent,
            round_index=round_index,
            call=call,
            entry=result,
            suffix=":approved",
        )
    )
    return json.dumps(
        {
            "state": "succeeded" if result.status == "executed" else "failed",
            "status": result.status,
        }
    )


@DBOS.step(name="agents.session.denied_after_wait")
def _session_denied_after_wait(
    payload_json: str, call_json: str, approval_id: str, round_index: int
) -> str:
    """Persist the honest denied result after an explicit deny."""
    intent = parse_session_run_intent(payload_json)
    executor = _resolve_session_run()
    call = json.loads(call_json)
    executor.run(
        _persist_tool_result(
            executor,
            intent,
            round_index=round_index,
            call=call,
            suffix=":denied",
            status="denied",
            execution_status=ExecutionStatus.FAILED,
            effect_certainty=EffectCertainty.CERTAIN,
            error_code="approval_denied",
            error_detail=None,
            approval_id=approval_id,
            payload_hash="",
            output_preview=None,
        )
    )
    return json.dumps({"persisted": "denied"})


@DBOS.step(name="agents.session.tool_group_marked")
def _session_tool_group_marked(payload_json: str, entry_id: str, state: str) -> str:
    intent = parse_session_run_intent(payload_json)
    executor = _resolve_session_run()
    marked = executor.run(
        amark_tool_group_state(
            executor.sessions,
            session_id=uuid.UUID(intent.session_id),
            server_id=uuid.UUID(intent.server_id),
            entry_id=uuid.UUID(entry_id),
            tool_group_state=state,
        )
    )
    return json.dumps({"marked": bool(marked)})


@DBOS.step(name="agents.session.settled")
def _session_settled(payload_json: str, summary_json: str) -> str:
    """The dual-authority terminal settlement (checkpointed, idempotent)."""
    intent = parse_session_run_intent(payload_json)
    executor = _resolve_session_run()
    summary = json.loads(summary_json)
    state_value = str(summary.get("state", "succeeded"))
    mapping: Final[dict[str, tuple[RunState, SessionRunState, SessionStatus]]] = {
        "succeeded": (RunState.SUCCEEDED, SessionRunState.SUCCEEDED, SessionStatus.IDLE),
        "failed": (RunState.FAILED, SessionRunState.FAILED, SessionStatus.FAILED),
        "cancelled": (
            RunState.CANCELLED,
            SessionRunState.CANCELLED,
            SessionStatus.CANCELLED,
        ),
        "outcome_unknown": (
            RunState.RECONCILING,
            SessionRunState.OUTCOME_UNKNOWN,
            SessionStatus.FAILED,
        ),
    }
    facade_state, session_state, header_status = mapping.get(
        state_value, (RunState.FAILED, SessionRunState.FAILED, SessionStatus.FAILED)
    )
    current = executor.run(executor.lifecycle.current_state(intent.run_id))
    if current is not None and current in (
        RunState.SUCCEEDED,
        RunState.FAILED,
        RunState.CANCELLED,
    ):
        # The facade is already terminal (e.g. an engine-side cancellation
        # landed while a step was in flight): the facade is the authority —
        # align the session domain to it instead of overwriting it.
        if current is RunState.CANCELLED:
            facade_state, session_state, header_status = (
                RunState.CANCELLED,
                SessionRunState.CANCELLED,
                SessionStatus.CANCELLED,
            )
        elif current is RunState.SUCCEEDED:
            facade_state, session_state, header_status = (
                RunState.SUCCEEDED,
                SessionRunState.SUCCEEDED,
                SessionStatus.IDLE,
            )
    if current is not facade_state:
        executor.run(
            executor.lifecycle.record_transition(intent.run_id, facade_state)
        )
    executor.run(
        _record_session_state(
            executor,
            intent,
            session_state,
            reason_code=summary.get("reason"),
            session_status=header_status,
        )
    )
    # Terminal classification event (reviewed parity: agent.completed /
    # agent.failed carry the value-free outcome + reason).
    event_type = (
        "agent.completed"
        if session_state is SessionRunState.SUCCEEDED
        else "agent.failed"
        if session_state is SessionRunState.FAILED
        else None
    )
    if event_type is not None:
        executor.run(
            executor.steps.aemit_run_event(
                intent.run_id,
                event_type,
                {
                    "rounds": summary.get("rounds"),
                    "tool_calls": summary.get("tool_calls"),
                    "reason": summary.get("reason"),
                },
            )
        )
    return json.dumps({"settled": state_value})


# --- the workflow ---------------------------------------------------------------------


async def run_agent_session(payload_json: str) -> dict[str, object]:
    """Execute one durable session run (the single production loop).

    Rounds: drain steering → model round (checkpointed) → guarded tool
    calls (checkpointed fences; approvals park on the durable wait) →
    settle. Classified failures record ``failed`` in both authorities and
    raise :class:`SessionRunFailed` (engine ERROR); cancellation settles
    ``cancelled``. Success returns the value-free run summary.
    """
    intent = parse_session_run_intent(payload_json)
    try:
        _session_started(payload_json)
        pins_json = _session_model_bound(payload_json)
        rounds = 0
        stopped_by: str | None = None
        output_text = ""
        tool_records: list[dict[str, object]] = []
        while True:
            boundary = json.loads(_session_controls_drained(payload_json))
            if boundary.get("stop"):
                stopped_by = (
                    "paused"
                    if boundary["stop"] == "pause"
                    else str(boundary["stop"])
                )
                break
            round_record = json.loads(
                await asyncio.to_thread(
                    _session_model_round, payload_json, pins_json, rounds
                )
            )
            if str(round_record["status"]) == "final":
                output_text = str(round_record.get("output_text", ""))
                break
            calls = list(round_record["calls"])
            entry_id = str(round_record["assistant_entry_id"])
            for call in calls:
                call_json = json.dumps(call)
                # Safe boundary BEFORE each new tool effect: a drained stop
                # control gates the next command (settled calls keep their
                # honest recorded outcomes either way).
                boundary = json.loads(_session_controls_drained(payload_json))
                if boundary.get("stop"):
                    # Control vocabulary: the entry action is 'pause'; the
                    # settled state is 'cancelled' with reason 'paused'.
                    stopped_by = (
                        "paused"
                        if boundary["stop"] == "pause"
                        else str(boundary["stop"])
                    )
                    break
                for note in boundary.get("notes", []):
                    tool_records.append({"steering_injected": note})
                outcome = json.loads(
                    await asyncio.to_thread(
                        _session_tool_execute, payload_json, call_json, rounds
                    )
                )
                state = str(outcome["state"])
                tool_records.append(
                    {
                        "seq": outcome.get("seq"),
                        "state": state,
                        "status": outcome.get("status"),
                    }
                )
                if state == StepState.OUTCOME_UNKNOWN.value:
                    # Crash window: the honest unknown result is already in
                    # the log; the durable truth decides what it becomes.
                    settled = await _settle_unknown_session(
                        intent, call_json, rounds
                    )
                    tool_records[-1] = {
                        "seq": outcome.get("seq"),
                        "state": settled.get("row_state"),
                        "status": settled.get("status"),
                    }
                    if settled.get("cancelled"):
                        stopped_by = "cancelled"
                        break
                if str(outcome.get("status")) == "approval_required":
                    approval_id = str(outcome["approval_id"])
                    deadline_iso = _session_approval_wait(
                        payload_json, call_json, approval_id
                    )
                    decision, presented_id = await _park_for_decision(
                        intent, deadline_iso, approval_id
                    )
                    if decision == "timeout":
                        # The approval wait expired (run_v2 parity: expiry
                        # FAILS the run — waiting_approval → running →
                        # failed). No result entry exists for the refused
                        # binding, so the group is marked unresolved (never
                        # a false 'complete') and the run settles failed
                        # with the classified reason.
                        _session_wait_resumed(
                            payload_json, RunState.WAITING_APPROVAL.value
                        )
                        _session_tool_group_marked(
                            payload_json, entry_id, "unresolved"
                        )
                        raise SessionStageError("approval_expired")
                    _session_wait_resumed(
                        payload_json, RunState.WAITING_APPROVAL.value
                    )
                    if decision == "approve":
                        _session_approval_execute(
                            payload_json, call_json, presented_id, rounds
                        )
                    else:
                        _session_denied_after_wait(
                            payload_json, call_json, presented_id, rounds
                        )
            if stopped_by is not None:
                break
            # All round results landed: mark the atomic group complete.
            _session_tool_group_marked(payload_json, entry_id, "complete")
            rounds += 1
            if rounds >= intent.max_rounds:
                raise SessionStageError("round_budget_exhausted")
        final_state = (
            "cancelled"
            if stopped_by in ("cancelled", "paused")
            else "outcome_unknown"
            if stopped_by == "outcome_unknown"
            else "succeeded"
        )
        summary = {
            "state": final_state,
            "rounds": rounds,
            "tool_calls": tool_records,
            "output_chars": len(output_text),
            "stopped_by": stopped_by,
            "reason": stopped_by,
        }
        settled = json.loads(_session_settled(payload_json, json.dumps(summary)))
        if final_state == "cancelled":
            # The engine must land CANCELLED (the DBOS workflow status is
            # the facade truth): settle the domain above, then surface the
            # reviewed cancellation exception so the engine agrees.
            raise dbos_error.DBOSWorkflowCancelledError(
                "session run cancelled at a safe boundary"
            )
        return {
            "run_id": intent.run_id,
            "session_id": intent.session_id,
            "state": str(settled.get("settled", final_state)),
            **summary,
        }
    except SessionStageError as stage:
        _session_failed(payload_json, stage.reason)
        raise SessionRunFailed(stage.reason) from None
    except SessionContractError:
        _session_failed(payload_json, "contract_rejected")
        raise SessionRunFailed("contract_rejected") from None
    except (dbos_error.DBOSAwaitedWorkflowCancelledError, dbos_error.DBOSException) as cancelled:
        # Engine-side cancellation landed (facade request_cancel): the
        # DBOSAwaited subclass is what the workflow actually observes.
        import sys as _sys

        observed = getattr(cancelled, "reason", None) or str(cancelled)
        is_cancel = "cancel" in observed.lower() or isinstance(
            cancelled, dbos_error.DBOSAwaitedWorkflowCancelledError
        )
        print(
            f"session runner: workflow interrupted: {observed} "
            f"(cancel={is_cancel})",
            file=_sys.stderr,
            flush=True,
        )
        if is_cancel:
            _session_cancelled(payload_json)
        else:
            _session_failed(payload_json, "engine_interrupted")
        raise
    except dbos_error.DBOSWorkflowCancelledError:
        _session_cancelled(payload_json)
        raise
    except SessionRunError:
        _session_failed(payload_json, "runner_error")
        raise
    except Exception as error:
        # An unexpected failure (a defect, not a classified stage): the
        # engine lands ERROR either way — record the honest facade
        # translation so both sides agree, then let the original exception
        # surface as-is.
        import sys as _sys

        print(
            f"session runner: unexpected workflow failure: "
            f"{type(error).__name__}: {error}",
            file=_sys.stderr,
            flush=True,
        )
        _session_failed(payload_json, "unexpected_failure")
        raise


def _session_failed(payload_json: str, reason: str) -> str:
    return _session_settled(
        payload_json, json.dumps({"state": "failed", "reason": reason})
    )


def _session_cancelled(payload_json: str) -> str:
    return _session_settled(
        payload_json, json.dumps({"state": "cancelled", "reason": "cancelled"})
    )


async def _park_for_decision(
    intent: SessionRunIntent, deadline_iso: str, parked_approval_id: str
) -> tuple[str, str]:
    """Park the durable workflow on the resume topic until a decision.

    The park survives worker restarts (DBOS recv state is durable); the
    control plane wakes it with a durable message on ``RESUME_TOPIC``. The
    FIRST approval-kind decision wakes the park and is returned as
    ``(decision, presented_approval_id)`` — the dispatcher's payload binding
    refuses any presented approval that does not match THIS call's payload
    hash, so a forged/altered binding lands as an honest denial. Non-approval
    wakes are consumed and the park continues; the persisted deadline bounds
    the wait.
    """
    deadline = datetime.fromisoformat(deadline_iso)
    while True:
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return "timeout", parked_approval_id
        message = await DBOS.recv_async(
            topic=RESUME_TOPIC,
            timeout_seconds=min(
                intent.wait_poll_seconds,
                max(MIN_WAIT_POLL_SECONDS, remaining),
            ),
        )
        if isinstance(message, dict) and message.get("kind") == "approval":
            decision = str(message.get("decision", "deny"))
            if decision not in ("approve", "deny"):
                decision = "deny"
            return decision, str(message.get("approval_id") or parked_approval_id)
        # A non-approval wake on the shared topic (e.g. recovery.resolve or
        # a stray wake) is a durable re-check trigger only: keep waiting
        # within the budget (run_v2.py:1652-1656 pattern) — never a denial.


# --- the control facade (durable steering/cancel for any process) ---------------------


async def asubmit_session_control(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    request: SessionControlRequest,
    active_run_id: uuid.UUID | None = None,
    database_url: str | None = None,
) -> SessionControlResponse:
    """Record one durable control action (steer/pause/cancel/...).

    Scope-checked and idempotent: the ``ControlEntry`` lands under the
    request's client idempotency key (the admission ledger deduplicates),
    and the runner consumes it at the next safe boundary. ``RECONCILE``/
    ``APPROVE``/``DENY_APPROVAL`` are recorded durably; their execution
    bindings ride the approval seam (C03 rule 4), not this facade.
    """
    from mai.session_store import aappend_entry, arecord_admission, arecord_run_state

    target = request.target_run_id or active_run_id
    await arecord_admission(
        sessions,
        session_id=session_id,
        server_id=server_id,
        client_request_id=request.client_request_id,
        source=SessionSourceKind.API,
        run_id=target,
    )
    payload: dict[str, object] = {
        "kind": EntryKind.CONTROL.value,
        "action": request.action.value,
        "client_request_id": str(request.client_request_id),
        "target_run_id": str(target) if target is not None else None,
        "approval_id": (
            str(request.approval_id) if request.approval_id is not None else None
        ),
        "note": request.note,
    }
    if request.action not in (ControlActionKind.STEER, ControlActionKind.CANCEL):
        # Non-delivery controls are recorded but need no runner boundary.
        await aappend_entry(
            sessions,
            session_id=session_id,
            server_id=server_id,
            kind=EntryKind.CONTROL,
            payload=payload,
            run_id=target,
            durable_source_id=f"control:{request.client_request_id}",
        )
        return SessionControlResponse(
            session_id=session_id,
            action=request.action,
            client_request_id=request.client_request_id,
            target_run_id=target,
            accepted=True,
            run_state=None,
        )
    # Delivery controls target a run; steering acknowledges immediately by
    # recording the entry the runner will drain at its next boundary.
    await aappend_entry(
        sessions,
        session_id=session_id,
        server_id=server_id,
        kind=EntryKind.CONTROL,
        payload=payload,
        run_id=target,
        durable_source_id=f"control:{request.client_request_id}",
    )
    run_state: SessionRunState | None = None
    if (
        request.action
        in (ControlActionKind.CANCEL, ControlActionKind.PAUSE)
        and target is not None
        and request.action is ControlActionKind.CANCEL
    ):
        # The engine-side cancellation: DBOS lands it at the workflow's
        # next step/recv boundary as DBOSWorkflowCancelledError; the
        # runner's handler settles the domain cancelled and re-raises so
        # the engine status is CANCELLED (C05 rule 2: the durable Control
        # entry is the operator request; the engine lands the truth).
        if database_url:
            from mai.jobs.engine import EngineClient
            from mai.session_store import arecord_run_state

            client = EngineClient(database_url)
            try:
                client.request_cancel(str(target))
            finally:
                client.close()
            # DBOS hard-cancels the child workflow (its handler may never
            # observe the interruption), so the FACADE settles the session
            # domain here — the request_cancel is durable engine truth.
            # The job_events transitions are recorded the same way
            # StepsService.acancel_run does (waiting/reconciling resume
            # through running, then land cancelled; terminal untouched).
            lifecycle = RunLifecycle(sessions)
            current = await lifecycle.current_state(str(target))
            if current in (RunState.WAITING_APPROVAL, RunState.RECONCILING):
                await lifecycle.record_transition(str(target), RunState.RUNNING)
                current = RunState.RUNNING
            if current is RunState.RUNNING:
                await lifecycle.record_transition(str(target), RunState.CANCELLED)
            await arecord_run_state(
                sessions,
                session_id=session_id,
                server_id=server_id,
                run_id=target,
                state=SessionRunState.CANCELLED,
                reason_code="cancelled",
            )
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        sql_text(
                            "UPDATE sessions SET status = 'cancelled', "
                            "updated_at = now() "
                            "WHERE session_id = :sid AND server_id = :server_id"
                        ),
                        {"sid": session_id, "server_id": server_id},
                    )
        # The runner's boundary drain also observes the durable entry and
        # settles the session domain (belt and braces for a parked run).
        run_state = SessionRunState.CANCELLED
    return SessionControlResponse(
        session_id=session_id,
        action=request.action,
        client_request_id=request.client_request_id,
        target_run_id=target,
        accepted=True,
        run_state=run_state,
    )


# Registration at import (the engine trampoline resolves the handler by
# name; worker entrypoints import this module via MAI_WORKER_HANDLERS).
_engine.register_job_handler(HANDLER_NAME, run_agent_session)
