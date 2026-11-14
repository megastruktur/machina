"""Durable capability steps: ordered, resumable, exactly-once per step.

Card ``05-steps`` against contracts.md **C03** (rules 3–5: the gates),
**C04** (rules 3–5: receipts, intent-before-effect, honest uncertainty)
and **C05** (rules 1–2, 5–6: one engine, the frozen run lifecycle, submit
dedup, shared locks). This module is the run facade's step executor: a
run's steps are policy-bound capability calls executed in order through
the ONE common dispatcher
(:class:`~mai.capabilities.dispatch.CapabilityDispatcher`) — there is no
inline route execution and no second effect path (acceptance 1; SERVICES
"End-to-end execution route"). The module owns:

- **Step intent** — one ``job_steps`` row per step (migration
  ``0004_jobs``), inserted ``pending`` BEFORE any effect and advanced
  through the frozen step state machine (below). The durable run owns the
  intent: rows hang off ``run_id`` and ``(run_id, seq)`` is UNIQUE, so
  per-run step order is durable and total.
- **The common dispatcher** — every effect is one
  :meth:`CapabilityDispatcher.dispatch` call with the run's
  :class:`~mai.policy.evaluate.ExecutionContext`; the grant/approval/
  policy/credential gates are the reviewed producer's, never
  re-implemented here.
- **The resource lock** — a step that declares a ``resource`` holds an
  exclusive ``(server_id, resource)`` lease
  (:class:`~mai.jobs.resource_locks.ResourceLockService`) across the
  effect, heartbeated while it runs. A proven terminal outcome releases
  the lease; an ``outcome_unknown`` step KEEPS it (the prior effect may
  still be live — C04 rule 7) and records the lease in the receipt so the
  reconciliation owner can operate it.
- **The persisted safe receipt** — ``job_steps.receipt`` is value-free by
  construction (C04 rule 4): decision facts, stable reasons, opaque op
  id, payload hash, policy revision, evidence references. No command
  text, no output text, no credential material.

Durability and exactly-once semantics (job-engine guard rules 1–2)
------------------------------------------------------------------

The handler :func:`run_capability_steps` is registered as the
``capability.steps`` run handler
(:func:`mai.jobs.engine.register_job_handler`) and executes inside the one
durable trampoline workflow, so every ``DBOS.step`` call below is
checkpointed: a completed attempt replays its recorded result on recovery
and never re-executes. The crash window INSIDE an attempt is covered by
the domain rows themselves (verify-then-mutate, guard rule 1):

1. intent row ``pending`` committed before the attempt starts;
2. the attempt marks the row ``running`` (and emits ``step.started``)
   committed BEFORE dispatch — the pre-effect fence marker;
3. dispatch runs; the outcome + receipt commit after it.

A recovered attempt that finds ``running`` WITHOUT a receipt knows a
prior attempt died with its effect unproven: it persists
``outcome_unknown`` (guard rule 2: missing receipt ⇒ ``outcome_unknown``,
never auto-finalized) and NEVER re-dispatches. An attempt that finds a
terminal receipt returns it — the step ran exactly once.
Over-conservative translation after a crash (``unknown`` for an effect
that provably never started) is safe by design: reconciliation decides,
never retry.

Run-state translation (acceptance 2)
------------------------------------

Every step answer translates onto the frozen C05 rule 2 run diagram via
the reviewed :class:`~mai.jobs.lifecycle.RunLifecycle` producer (the same
row-locked, cursor-allocating transition writer) — never a second state
authority:

- ``approval_required_but_absent`` → run ``waiting_approval`` (legal
  edge); the step row stays ``pending`` with no receipt (nothing was
  attempted; the storage CHECK enforces receipt-less pending). The
  handler parks on a durable ``DBOS.recv`` poll. Resume = a PENDING
  approval decision bound to ``(capability_id, server_id, payload_hash)``
  (C03 rule 4) appears; the runner presents it through the dispatcher's
  approval seam. A rejected decision fails the step honestly; a granted
  one executes. The wait deadline is persisted in the
  ``step.awaiting_approval`` event so recovery keeps the ORIGINAL
  deadline; expiry resumes the run and fails it
  (``waiting_approval → running → failed``, both legal edges).
- Unproven effects (``effect="unknown"``, host receipts that never went
  terminal, capability documents that could not be resolved) → step
  ``outcome_unknown`` + run ``reconciling``. The handler parks reading
  the step row: the reconciliation owner (``04-reconcile`` /
  ``05-cancel-recovery``) resolves the step to a terminal state and the
  runner resumes the run with the resolved outcome. Deadline expiry
  resumes and fails the run (``reconciling → running → failed``).
  Success is never claimed for an unproven effect and the effect is
  never retried.
- Policy/seam denials and lock rejections (``held``/``fenced``) → step
  ``failed`` with the stable reason on the safe receipt; the run fails.
  No effect was attempted.
- Host-verified ``cancelled`` → step ``cancelled``, run ``cancelled``.
- All steps terminal-success → run ``succeeded``; the first failed step
  fails the run (ordered execution, fail fast).

Engine/facade layering is the designed one (C05 rule 2): while parked the
engine stays PENDING and the facade refines it to ``waiting_approval`` /
``reconciling``; terminal endings make both sides agree (the handler
raises :class:`StepsRunFailed` after recording ``failed`` so the engine
lands ERROR, and re-raises the engine's own cancellation exception after
recording ``cancelled``).

The frozen step state machine (``job_steps.state``; vocabulary pinned by
migration ``0004_jobs``, edges pinned HERE):

- ``None → pending`` (the intent insert);
- ``pending → running`` (the pre-effect fence marker) and
  ``pending → failed`` (rejections that provably attempted nothing:
  lock contention, malformed request);
- ``running → pending`` (the ORDERLY no-effect retract: the dispatcher
  proved the attempt ended before any effect — the approval wait; a
  recovered ``running`` row with no receipt is ALWAYS translated as
  unproven instead);
- ``running → succeeded | failed | outcome_unknown | cancelled`` (only
  the attempt holding the fence marker may resolve it — guarded UPDATE);
- ``outcome_unknown → succeeded | failed | cancelled`` — these edges are
  OWNED BY THE RECONCILIATION WRITER; this module's runner only reads
  them;
- ``cancel_requested`` has no edges here: recording it is the
  cancellation owner's surface (guard rule 3: requested ≠ verified);
- terminal states have no out-edges.

Process discipline (mirror of :mod:`mai.jobs.engine` /
:mod:`mai.jobs.schedules`): the worker process composes the executor with
:func:`configure_steps` BEFORE runs are claimed (the real provider
wiring — registry, grants, policy, adoption, credentials, executor — is
the worker-composition card's surface and is never invented here); a
handler that fires without composition fails its run value-free
(``executor_not_configured``) instead of executing anything inline. The
handler is registered at import; worker entrypoints import this module
via ``MAI_WORKER_HANDLERS``.

Sessions sit on a dedicated ``NullPool`` async engine on the single
``mai.database`` URL contract (worker path). ALL async work — sessions,
the lifecycle producer, the dispatcher's approval/credential stores —
runs on ONE persistent background loop owned by the executor
(:class:`_LoopRunner`): SQLAlchemy binds asyncio primitives to the first
loop that touches an engine, so per-call ``asyncio.run`` loops are
measured-broken ("bound to a different event loop"); the one-loop runner
is the composition fix, mirroring the password-channel card's
no-cross-loop rule.

Wake seam: control-plane processes send
``DBOSClient.send(run_id, <any JSON message>, topic=RESUME_TOPIC)`` to
end a park early; any message merely triggers a durable state re-check,
and the poll works with no sender at all. Waiting is durable: a crash
during a park resumes the identical wait on recovery.

Limitations (deliberate, recorded in the card evidence): waiting holds
one queue slot per parked run (DBOS ``recv`` sleeps in the worker
thread) — bounded by the wait budgets and worker concurrency; a database
outage during an attempt surfaces as a workflow error (the adapter's
outage-pause rule is owned by the engine seam, not re-implemented here);
cancellation landing mid-wait is translated by
:meth:`StepExecutor.acancel_run` exactly when the engine raises.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from dbos import DBOS
from dbos import error as dbos_error
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mai.capabilities.dispatch import (
    EFFECT_EXECUTED,
    EFFECT_NOT_ATTEMPTED,
    EFFECT_UNKNOWN,
    CapabilityDispatcher,
    DispatchError,
    DispatchOutcome,
    DispatchReason,
    canonical_payload_hash,
)
from mai.contracts.jobs import (
    EVENT_PAYLOAD_VERSION,
    TERMINAL_RUN_STATES,
    TERMINAL_STEP_STATES,
    RunState,
    StepState,
    step_state_from_command_status,
)
from mai.execution.commands import CommandStatus
from mai.jobs import engine as _engine
from mai.jobs.engine import EngineError, validate_run_id
from mai.jobs.lifecycle import RunLifecycle
from mai.jobs.resource_locks import (
    MAX_RESOURCE_CHARS,
    LockLease,
    LockSnapshot,
    ResourceLockError,
    ResourceLockService,
)
from mai.policy.evaluate import ExecutionContext, execution_context_from_json

__all__ = [
    "APPROVAL_REQUIRED_REASON",
    "CRASH_RECOVERY_REASON",
    "DEFAULT_APPROVAL_WAIT_SECONDS",
    "DEFAULT_RECONCILE_WAIT_SECONDS",
    "DEFAULT_WAIT_POLL_SECONDS",
    "HANDLER_NAME",
    "LEGAL_STEP_TRANSITIONS",
    "MAX_APPROVAL_WAIT_SECONDS",
    "MAX_RECONCILE_WAIT_SECONDS",
    "MAX_STEPS_PER_RUN",
    "MAX_STEP_PAYLOAD_CHARS",
    "MAX_WAIT_POLL_SECONDS",
    "RESUME_TOPIC",
    "RUN_PAYLOAD_VERSION",
    "STATUS_UNRESOLVED_REASON",
    "STEP_AWAITING_APPROVAL_EVENT_TYPE",
    "STEP_COMPLETED_EVENT_TYPE",
    "STEP_RECONCILING_EVENT_TYPE",
    "STEP_STARTED_EVENT_TYPE",
    "WAIT_FOR_APPROVAL",
    "RunIntent",
    "StepExecutor",
    "StepIntent",
    "StepsError",
    "StepsErrorCode",
    "StepsRunFailed",
    "configure_steps",
    "is_legal_step_transition",
    "parse_run_intent",
    "reset_steps",
    "run_capability_steps",
    "validate_step_transition",
]

# ---------------------------------------------------------------------------
# frozen vocabulary + bounds
# ---------------------------------------------------------------------------

#: The run-handler name this module registers (dotted slug; the engine
#: trampoline resolves it at execution time).
HANDLER_NAME: Final[str] = "capability.steps"

#: Payload document version produced by this freeze.
RUN_PAYLOAD_VERSION: Final[int] = 1

#: Step event types this module owns in the shared per-run stream (C05
#: rule 4: lowercase dotted slugs; the envelope SHAPE is frozen in
#: ``mai.contracts.jobs``, the type vocabulary stays open).
STEP_STARTED_EVENT_TYPE: Final[str] = "step.started"
STEP_COMPLETED_EVENT_TYPE: Final[str] = "step.completed"
STEP_AWAITING_APPROVAL_EVENT_TYPE: Final[str] = "step.awaiting_approval"
STEP_RECONCILING_EVENT_TYPE: Final[str] = "step.reconciling"

#: The durable park topic. Control-plane senders wake a parked run by
#: ``DBOS.send(run_id, <any JSON>, topic=RESUME_TOPIC)``; the message is
#: only a re-check trigger — the parked loop always re-reads durable
#: state, so stale or unknown messages are harmless.
RESUME_TOPIC: Final[str] = "mai:steps:resume"

#: The dispatcher reason that means "the policy gate wants a decision
#: nobody has presented" — the approval-wait trigger (acceptance 2).
APPROVAL_REQUIRED_REASON: Final[str] = DispatchReason.APPROVAL_REQUIRED_BUT_ABSENT.value

#: The outcome-dict sentinel for "waiting for an approval decision". It
#: is deliberately NOT a :class:`StepState` — a waiting step row stays
#: ``pending`` with no receipt (nothing was attempted).
WAIT_FOR_APPROVAL: Final[str] = "waiting_approval"

#: Stable receipt reasons owned by this module.
CRASH_RECOVERY_REASON: Final[str] = "crash_recovery"
STATUS_UNRESOLVED_REASON: Final[str] = "status_unresolved"
EXECUTION_TIMEOUT_REASON: Final[str] = "execution_timeout_exceeded"

#: Stable reasons for wait-budget expiry (translate to ``failed`` runs).
APPROVAL_WAIT_DEADLINE_REASON: Final[str] = "approval_wait_deadline"
RECONCILE_WAIT_DEADLINE_REASON: Final[str] = "reconcile_wait_deadline"

#: Wait budgets and poll cadence (per-run payload fields, bounded).
DEFAULT_APPROVAL_WAIT_SECONDS: Final[float] = 24 * 3600.0
DEFAULT_RECONCILE_WAIT_SECONDS: Final[float] = 24 * 3600.0
MAX_APPROVAL_WAIT_SECONDS: Final[float] = 7 * 24 * 3600.0
MAX_RECONCILE_WAIT_SECONDS: Final[float] = 7 * 24 * 3600.0
DEFAULT_WAIT_POLL_SECONDS: Final[float] = 5.0
MAX_WAIT_POLL_SECONDS: Final[float] = 300.0
MIN_RECV_TIMEOUT_SECONDS: Final[float] = 0.5

#: Run-intent bounds.
MAX_STEPS_PER_RUN: Final[int] = 100
MAX_STEP_PAYLOAD_CHARS: Final[int] = 64 * 1024

_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

#: The frozen step state machine (see module docstring for ownership).
LEGAL_STEP_TRANSITIONS: Final[Mapping[StepState | None, frozenset[StepState]]] = (
    MappingProxyType(
        {
            None: frozenset({StepState.PENDING}),
            StepState.PENDING: frozenset({StepState.RUNNING, StepState.FAILED}),
            StepState.RUNNING: frozenset(
                {
                    # running → pending: the ORDERLY no-effect retract of the
                    # fence marker (the dispatcher proved the attempt ended
                    # before any effect — approval wait). NOT the crash
                    # window: a recovered running row with no receipt is
                    # always translated as unproven.
                    StepState.PENDING,
                    StepState.SUCCEEDED,
                    StepState.FAILED,
                    StepState.OUTCOME_UNKNOWN,
                    StepState.CANCELLED,
                }
            ),
            StepState.OUTCOME_UNKNOWN: frozenset(
                {StepState.SUCCEEDED, StepState.FAILED, StepState.CANCELLED}
            ),
            StepState.CANCEL_REQUESTED: frozenset(),
            StepState.SUCCEEDED: frozenset(),
            StepState.FAILED: frozenset(),
            StepState.CANCELLED: frozenset(),
        }
    )
)

#: Run states this module's waits may be entered from / resumed to.
_WAITING_RUN_STATES: Final[frozenset[RunState]] = frozenset(
    {RunState.WAITING_APPROVAL, RunState.RECONCILING}
)


class StepsErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_PAYLOAD = "invalid_payload"
    INVALID_WIRING = "invalid_wiring"
    INVALID_STEP_TRANSITION = "invalid_step_transition"
    RUN_NOT_FOUND = "run_not_found"
    INVALID_RUN_STATE = "invalid_run_state"
    INVALID_WAIT_STATE = "invalid_wait_state"
    STEP_STATE_GUARD = "step_state_guard"
    EXECUTOR_NOT_CONFIGURED = "executor_not_configured"
    ALREADY_CONFIGURED = "already_configured"


class StepsError(ValueError):
    """One value-free step-facade rejection: code + fixed reason.

    Messages carry codes, field names and opaque identifiers only — never
    payload values, commands or output text (C01 rule 3) — so they are
    safe to log, store and surface.
    """

    def __init__(self, code: StepsErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class StepsRunFailed(RuntimeError):
    """Lands the ENGINE at ERROR after the run was recorded ``failed``
    domain-side, so engine and facade agree on the ending.

    The reason is a stable code string, never payload content.
    """


def is_legal_step_transition(current: StepState | None, target: StepState) -> bool:
    """True iff ``current → target`` is an edge of the step machine."""
    if not isinstance(target, StepState):
        return False
    return target in LEGAL_STEP_TRANSITIONS[current]


def validate_step_transition(current: StepState | None, target: StepState) -> None:
    """Require a legal step edge; value-free rejection otherwise."""
    if not isinstance(target, StepState):
        raise StepsError(
            StepsErrorCode.INVALID_STEP_TRANSITION, "target must be a StepState value"
        )
    if not is_legal_step_transition(current, target):
        raise StepsError(
            StepsErrorCode.INVALID_STEP_TRANSITION,
            "transition is not an edge of the frozen step state machine",
        )


# ---------------------------------------------------------------------------
# run-intent payload contract (frozen by this card; references only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepIntent:
    """One step of a run intent (references and identifiers only).

    ``payload`` is the capability operation payload — the exact object the
    approval binds by hash and the dispatcher gates (C03 rule 4).
    ``resource`` optionally declares the mutation-lock identity for the
    step (C05 rule 6): when present the step holds the exclusive
    ``(server_id, resource)`` lease across its effect.
    """

    seq: int
    capability_id: str
    payload: dict[str, object]
    resource: str | None = None

    def to_json(self) -> dict[str, object]:
        document: dict[str, object] = {
            "seq": self.seq,
            "capability_id": self.capability_id,
            "payload": self.payload,
        }
        if self.resource is not None:
            document["resource"] = self.resource
        return document


@dataclass(frozen=True, slots=True)
class RunIntent:
    """The validated run payload for :data:`HANDLER_NAME`.

    ``context`` is the run's :class:`~mai.policy.evaluate.ExecutionContext`
    (references only, C03 rule 2); its ``run_id``/``server_id`` must equal
    the envelope identity — a context can never retarget the run.
    """

    run_id: str
    server_id: str
    context: ExecutionContext
    steps: tuple[StepIntent, ...]
    approval_wait_seconds: float
    reconcile_wait_seconds: float
    wait_poll_seconds: float


def _reject_payload(detail: str) -> StepsError:
    return StepsError(StepsErrorCode.INVALID_PAYLOAD, detail)


def _canonical_uuid_field(value: object, field: str) -> str:
    try:
        return validate_run_id(value)
    except EngineError:
        raise _reject_payload(f"{field} must be a canonical UUID string") from None


def canonical_json(payload: object) -> str:
    """Deterministic JSON text (sort_keys, tight separators, ASCII)."""
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise _reject_payload(
            f"not JSON-canonicalizable ({type(error).__name__})"
        ) from None


def _bounded_wait(value: object, field: str, default: float, maximum: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _reject_payload(f"{field} must be a number of seconds")
    number = float(value)
    if not 0 < number <= maximum:
        raise _reject_payload(f"{field} outside its bounded range")
    return number


def _parse_step_spec(raw: object, index: int) -> StepIntent:
    if not isinstance(raw, Mapping):
        raise _reject_payload(f"steps[{index}] must be an object")
    allowed = {"seq", "capability_id", "payload", "resource"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise _reject_payload(f"steps[{index}] carries unknown field {unknown[0]!r}")
    missing = {"seq", "capability_id", "payload"} - set(raw)
    if missing:
        raise _reject_payload(f"steps[{index}] is missing field {sorted(missing)[0]!r}")
    seq = raw["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= 2**31 - 1:
        raise _reject_payload(f"steps[{index}].seq must be a positive integer")
    capability_id = raw["capability_id"]
    if not isinstance(capability_id, str) or not _IDENTIFIER_RE.fullmatch(
        capability_id
    ):
        raise _reject_payload(
            f"steps[{index}].capability_id must match the C03 rule 1 slug shape"
        )
    payload = raw["payload"]
    if not isinstance(payload, Mapping):
        raise _reject_payload(f"steps[{index}].payload must be an object")
    if len(canonical_json(payload)) > MAX_STEP_PAYLOAD_CHARS:
        raise _reject_payload(
            f"steps[{index}].payload exceeds {MAX_STEP_PAYLOAD_CHARS} canonical bytes"
        )
    resource = raw.get("resource")
    if resource is not None:
        if not isinstance(resource, str) or not resource:
            raise _reject_payload(f"steps[{index}].resource must be a non-empty string")
        if len(resource) > MAX_RESOURCE_CHARS:
            raise _reject_payload(f"steps[{index}].resource exceeds its length bound")
        if any(ch < " " or ch == "\x7f" for ch in resource):
            raise _reject_payload(
                f"steps[{index}].resource must not contain control characters"
            )
    return StepIntent(
        seq=seq,
        capability_id=capability_id,
        payload=dict(payload),
        resource=resource,
    )


def parse_run_intent(payload_json: str) -> RunIntent:
    """Validate one run payload; return it typed (fail closed, value-free).

    Strict shape: exactly the frozen field set (wait budgets optional);
    ``v`` is this freeze's version; identities are canonical UUIDs; the
    embedded execution context must name THIS run and THIS server; steps
    are non-empty, bounded, with ``seq`` ascending strictly from 1 (the
    durable total order; the storage UNIQUE ``(run_id, seq)`` is the
    backstop).
    """
    if not isinstance(payload_json, str):
        raise _reject_payload("run payload must be a JSON string")
    try:
        document = json.loads(payload_json)
    except ValueError:
        raise _reject_payload("run payload is not valid JSON") from None
    if not isinstance(document, Mapping):
        raise _reject_payload("run payload must be a JSON object")
    allowed = {
        "v",
        "run_id",
        "server_id",
        "context",
        "steps",
        "approval_wait_seconds",
        "reconcile_wait_seconds",
        "wait_poll_seconds",
    }
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise _reject_payload(f"unknown field {unknown[0]!r}")
    missing = {"v", "run_id", "server_id", "context", "steps"} - set(document)
    if missing:
        raise _reject_payload(f"missing field {sorted(missing)[0]!r}")
    if document["v"] != RUN_PAYLOAD_VERSION:
        raise _reject_payload(f"v must be {RUN_PAYLOAD_VERSION}")
    run_id = _canonical_uuid_field(document["run_id"], "run_id")
    server_id = _canonical_uuid_field(document["server_id"], "server_id")
    try:
        context = execution_context_from_json(document["context"])
    except Exception:
        raise _reject_payload("context is not a valid ExecutionContext") from None
    if str(context.run_id) != run_id or str(context.server_id) != server_id:
        raise _reject_payload(
            "context identity does not match the run envelope (no retargeting)"
        )
    raw_steps = document["steps"]
    if not isinstance(raw_steps, list) or not raw_steps:
        raise _reject_payload("steps must be a non-empty array")
    if len(raw_steps) > MAX_STEPS_PER_RUN:
        raise _reject_payload(f"steps exceeds {MAX_STEPS_PER_RUN} entries")
    steps = tuple(_parse_step_spec(raw, index) for index, raw in enumerate(raw_steps))
    expected = 1
    for step in steps:
        if step.seq != expected:
            raise _reject_payload("steps.seq must ascend strictly from 1")
        expected += 1
    return RunIntent(
        run_id=run_id,
        server_id=server_id,
        context=context,
        steps=steps,
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
    )


# ---------------------------------------------------------------------------
# outcome translation (pure)
# ---------------------------------------------------------------------------


def _capability_document(outcome: DispatchOutcome) -> dict[str, object] | None:
    """Leniently parse the executor's result document.

    The dispatcher's output is redacted then bounded (C03 rule 6, C06
    rule 3); redaction preserves JSON, bounding can cut it. A document
    that does not parse is treated as UNRESOLVED — never guessed.
    """
    if outcome.output is None:
        return None
    try:
        document = json.loads(outcome.output.text)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _translate_outcome(
    outcome: DispatchOutcome,
) -> tuple[str, str | None, dict[str, object]]:
    """Map one dispatch receipt onto ``(state, reason, safe_receipt)``.

    ``state`` is a :class:`StepState` value or the
    :data:`WAIT_FOR_APPROVAL` sentinel. The receipt is value-free by
    construction: decision facts, stable reasons, opaque references —
    never the command, never output text, never credential material.
    """
    document = _capability_document(outcome)
    receipt: dict[str, object] = {
        "v": 1,
        "op_id": str(outcome.op_id),
        "capability_id": outcome.capability_id,
        "allowed": outcome.allowed,
        "reason": outcome.reason,
        "effect": outcome.effect,
        "policy_revision": outcome.policy_revision,
        "approval_id": None
        if outcome.approval_id is None
        else str(outcome.approval_id),
        "approval_spent": outcome.approval_spent,
        "approval_code": outcome.approval_code,
        "executor_ok": outcome.executor_ok,
        "payload_hash": outcome.payload_hash,
        "error_type": outcome.error_type,
        "error_code": outcome.error_code,
        "output_truncated": None
        if outcome.output is None
        else outcome.output.truncated,
        "omitted_bytes": None
        if outcome.output is None
        else outcome.output.omitted_bytes,
    }
    if isinstance(document, dict):
        for field in ("status", "exit_code", "receipt_dir", "intent_path"):
            if field in document:
                receipt[field] = document[field]

    if outcome.effect == EFFECT_UNKNOWN:
        # The invocation started; the outcome is unproven (host rule 3
        # applied at the dispatch seam). Never retried, never finalized.
        return StepState.OUTCOME_UNKNOWN.value, outcome.reason, receipt
    if not outcome.allowed:
        if outcome.reason == APPROVAL_REQUIRED_REASON:
            return WAIT_FOR_APPROVAL, None, receipt
        return StepState.FAILED.value, outcome.reason, receipt
    if outcome.effect != EFFECT_EXECUTED:
        return StepState.OUTCOME_UNKNOWN.value, STATUS_UNRESOLVED_REASON, receipt
    if (
        isinstance(document, dict)
        and outcome.capability_id == "session.read"
        and isinstance(document.get("session_id"), str)
        and outcome.executor_ok is True
    ):
        receipt["status"] = CommandStatus.SUCCEEDED.value
        return StepState.SUCCEEDED.value, None, receipt
    if not isinstance(document, dict) or not isinstance(document.get("status"), str):
        # An executed effect whose terminal status cannot be resolved from
        # the (bounded) document: honest uncertainty, reconciliation decides.
        receipt["reason"] = STATUS_UNRESOLVED_REASON
        return StepState.OUTCOME_UNKNOWN.value, STATUS_UNRESOLVED_REASON, receipt
    doc_status = document["status"]
    if doc_status == "ok" and outcome.executor_ok is True:
        return StepState.SUCCEEDED.value, None, receipt
    if doc_status in ("unavailable", "refused") and outcome.executor_ok is False:
        fail_reason = (
            str(document["code"])
            if isinstance(document.get("code"), str) and document["code"]
            else doc_status
        )
        receipt["reason"] = fail_reason
        return StepState.FAILED.value, fail_reason, receipt
    try:
        state = step_state_from_command_status(CommandStatus(doc_status))
    except ValueError:
        receipt["reason"] = STATUS_UNRESOLVED_REASON
        return StepState.OUTCOME_UNKNOWN.value, STATUS_UNRESOLVED_REASON, receipt
    if state in (
        StepState.RUNNING,
        StepState.OUTCOME_UNKNOWN,
        StepState.CANCEL_REQUESTED,
    ):
        # The host receipt has not gone terminal (and a requested
        # cancellation is not a verified one — guard rule 3).
        receipt["reason"] = STATUS_UNRESOLVED_REASON
        return StepState.OUTCOME_UNKNOWN.value, STATUS_UNRESOLVED_REASON, receipt
    return state.value, None, receipt


def _outcome_from_receipt(
    state: str, receipt: Mapping[str, object]
) -> dict[str, object]:
    """Rebuild the outcome dict from a persisted receipt (resume path)."""
    return {
        "state": state,
        "reason": receipt.get("reason"),
        "op_id": receipt.get("op_id"),
        "receipt": dict(receipt),
    }


def _lock_owner_ref(run_id: str, seq: int) -> str:
    """The opaque owner label for one step's lease (bounded, value-free)."""
    return f"run:{run_id}:step:{seq}"


class _LoopRunner:
    """ONE persistent background event loop for every async seam.

    SQLAlchemy binds asyncio primitives (its exec-once connect mutex and
    any pool plumbing) to the FIRST loop that touches an engine; per-call
    ``asyncio.run`` loops then fail with "bound to a different event
    loop" — measured against this module's sync DBOS steps. Every async
    operation (sessions, the lifecycle producer, the dispatcher's
    approval/credential stores) therefore runs on ONE loop owned by a
    daemon thread, and the sync step bodies submit coroutines with
    ``run_coroutine_threadsafe`` and block on the result.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever, name="mai-steps-loop", daemon=True
                )
                thread.start()
                self._loop = loop
                self._thread = thread
            return self._loop

    def run(self, coro: object) -> object:
        """Run one coroutine to completion on the persistent loop."""
        return asyncio.run_coroutine_threadsafe(coro, self._ensure()).result()

    def close(self) -> None:
        """Stop the loop thread (idempotent; teardown convenience)."""
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop, self._thread = None, None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=10.0)
            loop.close()


def _denial_receipt(
    *, capability_id: str, payload_hash: str, reason: str, op_id: str | None
) -> dict[str, object]:
    """Value-free receipt for an attempt that proved NO effect."""
    return {
        "v": 1,
        "op_id": op_id,
        "capability_id": capability_id,
        "allowed": False,
        "reason": reason,
        "effect": EFFECT_NOT_ATTEMPTED,
        "payload_hash": payload_hash,
    }


def _lock_receipt_fields(lease: LockLease, *, released: bool, lapsed: bool):
    return {
        "resource": lease.resource,
        "owner_token": lease.owner_token,
        "lease_ttl": lease.lease_ttl,
        "released": released,
        "lapsed": lapsed,
    }


class _Heartbeat:
    """Keep one lease alive while its effect runs; record a lapse.

    A lost heartbeat is the caller's signal to stop issuing conflicting
    work (C04 rule 7); the lapse is surfaced onto the receipt and the
    lease is fenced by the service's own touch.
    """

    def __init__(self, service: ResourceLockService, lease: LockLease) -> None:
        self._service = service
        self._lease = lease
        self._stop = threading.Event()
        self.lapsed = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        interval = max(1.0, self._lease.lease_ttl / 3.0)

        def _beat() -> None:
            while not self._stop.wait(interval):
                try:
                    if not self._service.heartbeat(self._lease):
                        self.lapsed = True
                        return
                except ResourceLockError:
                    self.lapsed = True
                    return

        self._thread = threading.Thread(target=_beat, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)


# ---------------------------------------------------------------------------
# composition
# ---------------------------------------------------------------------------


class StepExecutor:
    """Composed step execution: sessions + dispatcher + lock service.

    Wiring is constructor-checked (fail-closed, the sibling pattern). The
    sessions factory should sit on a ``NullPool`` engine, and EVERY async
    operation is driven through this executor's ONE persistent loop
    (:meth:`run`) — SQLAlchemy binds asyncio primitives to the first loop
    that touches an engine, so the per-call-loop pattern is measured-
    broken and the one-loop runner replaces it (see :class:`_LoopRunner`).
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        dispatcher: CapabilityDispatcher,
        locks: ResourceLockService,
    ) -> None:
        if not isinstance(sessions, async_sessionmaker):
            raise StepsError(
                StepsErrorCode.INVALID_WIRING,
                "sessions must be an async_sessionmaker on a NullPool engine",
            )
        if not isinstance(dispatcher, CapabilityDispatcher):
            raise StepsError(
                StepsErrorCode.INVALID_WIRING,
                "dispatcher must be the common CapabilityDispatcher",
            )
        if not isinstance(locks, ResourceLockService):
            raise StepsError(
                StepsErrorCode.INVALID_WIRING,
                "locks must be the ResourceLockService",
            )
        self._sessions = sessions
        self._dispatcher = dispatcher
        self._locks = locks
        self._lifecycle = RunLifecycle(sessions)
        self._runner = _LoopRunner()

    def run(self, coro: object) -> object:
        """Drive one coroutine on this executor's persistent loop."""
        return self._runner.run(coro)

    def close(self) -> None:
        """Stop the executor's loop thread (worker shutdown convenience)."""
        self._runner.close()

    @property
    def lifecycle(self) -> RunLifecycle:
        """The reviewed run-lifecycle producer (all run transitions)."""
        return self._lifecycle

    @property
    def locks(self) -> ResourceLockService:
        return self._locks

    # -- run-state observation (acceptance 2: legal transitions only) ----

    async def aobserve_run_started(self, run_id: str) -> str:
        """Anchor the submit edge if absent, then record ``running``.

        The handler executes only inside a claimed run, so an unanchored
        log means the submit owner has not written its edge yet
        (standalone composition); the facade records what it observes —
        idempotently, and never through a non-legal edge. Terminal runs
        are never restarted by a stale observation (the lifecycle's
        terminal guard rejects).
        """
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text("SELECT run_id FROM job_runs WHERE run_id = :r"),
                    {"r": uuid.UUID(run_id)},
                )
            ).first()
        if row is None:
            raise StepsError(
                StepsErrorCode.RUN_NOT_FOUND, "no job_runs row for this run_id"
            )
        current = await self._lifecycle.current_state(run_id)
        if current is None:
            await self._lifecycle.record_transition(run_id, RunState.QUEUED)
            current = await self._lifecycle.current_state(run_id)
        if current in TERMINAL_RUN_STATES:
            raise StepsError(
                StepsErrorCode.INVALID_RUN_STATE,
                "run is already terminal; no step may execute on it",
            )
        if current is RunState.QUEUED:
            await self._lifecycle.record_transition(run_id, RunState.RUNNING)
            current = await self._lifecycle.current_state(run_id)
        return current.value

    async def afail_run(self, run_id: str, reason: str) -> str:
        """Record the run ``failed`` (observe; only from legal states).

        Legal entries: an unanchored log anchors at the submit edge first
        (the handler observes what it executes); ``queued``/``running``
        fail directly; a waiting state resumes through ``running`` first
        (both edges of the frozen diagram). Terminal states are left
        untouched by the lifecycle's stale-result guard.
        """
        current = await self._lifecycle.current_state(run_id)
        if current is None:
            await self._lifecycle.record_transition(run_id, RunState.QUEUED)
            current = await self._lifecycle.current_state(run_id)
        if current in (RunState.QUEUED, RunState.RUNNING):
            await self._lifecycle.record_transition(run_id, RunState.FAILED)
        elif current in _WAITING_RUN_STATES or current is RunState.WAITING_CHILDREN:
            await self._lifecycle.record_transition(run_id, RunState.RUNNING)
            await self._lifecycle.record_transition(run_id, RunState.FAILED)
        return reason

    async def acancel_run(self, run_id: str) -> str:
        """Translate an engine cancellation onto the frozen diagram.

        Waiting/reconciling states resume through ``running`` (the wait
        ended), then land ``cancelled``. Terminal states are untouched.
        Best-effort by contract: the caller re-raises the engine's own
        cancellation exception either way.
        """
        current = await self._lifecycle.current_state(run_id)
        if current in _WAITING_RUN_STATES or current is RunState.WAITING_CHILDREN:
            await self._lifecycle.record_transition(run_id, RunState.RUNNING)
            current = RunState.RUNNING
        if current is RunState.RUNNING:
            await self._lifecycle.record_transition(run_id, RunState.CANCELLED)
        return run_id

    async def aobserve_enter_wait(
        self,
        run_id: str,
        *,
        target: RunState,
        event_type: str,
        event_data: Mapping[str, object],
        wait_seconds: float,
        step_seq: int,
    ) -> str:
        """Enter a facade wait ONCE and persist its deadline (acceptance 2).

        ``running → waiting_approval`` / ``running → reconciling`` is
        recorded (legal edges) together with the wait event carrying the
        ABSOLUTE deadline. A repeated entry (recovery replay, a repeated
        wait loop) observes the state and returns the ORIGINAL deadline
        from the persisted event — budgets never silently extend. Any
        other current state is a value-free rejection.
        """
        if target not in _WAITING_RUN_STATES:
            raise StepsError(
                StepsErrorCode.INVALID_WAIT_STATE,
                "target must be a facade wait state",
            )
        current = await self._lifecycle.current_state(run_id)
        if current is target:
            return await self._wait_deadline(run_id, event_type, step_seq)
        if current is not RunState.RUNNING:
            raise StepsError(
                StepsErrorCode.INVALID_WAIT_STATE,
                "run is not in a state that may enter this wait",
            )
        deadline = datetime.now(timezone.utc) + timedelta(seconds=float(wait_seconds))
        await self._lifecycle.record_transition(run_id, target)
        await self.aemit_run_event(
            run_id,
            event_type,
            {**event_data, "step_seq": step_seq, "deadline": deadline.isoformat()},
        )
        return deadline.isoformat()

    async def _wait_deadline(self, run_id: str, event_type: str, step_seq: int) -> str:
        """The persisted deadline of the run's latest wait event."""
        for event in reversed(await self._lifecycle.event_stream(run_id)):
            if event.type == event_type and event.data.get("step_seq") == step_seq:
                deadline = event.data.get("deadline")
                if isinstance(deadline, str):
                    return deadline
        raise StepsError(
            StepsErrorCode.INVALID_WAIT_STATE,
            "wait state present but no persisted wait deadline exists",
        )

    async def aobserve_wait_resumed(self, run_id: str, from_state: str) -> str:
        """Record ``<wait> → running`` when the wait ends (observe).

        Idempotent under replay: an already-``running`` log is returned
        as-is; a different state is a value-free rejection.
        """
        current = await self._lifecycle.current_state(run_id)
        if current is RunState.RUNNING:
            return RunState.RUNNING.value
        if current is not None and current.value == from_state:
            await self._lifecycle.record_transition(run_id, RunState.RUNNING)
            return RunState.RUNNING.value
        raise StepsError(
            StepsErrorCode.INVALID_WAIT_STATE,
            "run is not in the expected wait state",
        )

    # -- run events (the shared whole-stream cursor) -----------------------

    async def aemit_run_event(
        self, run_id: str, event_type: str, data: Mapping[str, object]
    ) -> int:
        """Append one event with the next whole-stream cursor value.

        Same allocation contract as the lifecycle producer: next
        ``MAX(seq) + 1`` under the ``job_runs`` row lock, in one
        transaction (C05 rule 4; the storage UNIQUE is the backstop).
        """
        async with self._sessions() as session:
            async with session.begin():
                locked = (
                    await session.execute(
                        text(
                            "SELECT run_id FROM job_runs WHERE run_id = :r FOR UPDATE"
                        ),
                        {"r": uuid.UUID(run_id)},
                    )
                ).scalar()
                if locked is None:
                    raise StepsError(
                        StepsErrorCode.RUN_NOT_FOUND,
                        "no job_runs row for this run_id",
                    )
                next_seq = (
                    await session.execute(
                        text(
                            "SELECT COALESCE(MAX(seq), 0) + 1 FROM job_events "
                            "WHERE run_id = :r"
                        ),
                        {"r": uuid.UUID(run_id)},
                    )
                ).scalar_one()
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
                        "seq": next_seq,
                        "ts": datetime.now(timezone.utc),
                        "type": event_type,
                        "v": EVENT_PAYLOAD_VERSION,
                        "data": json.dumps(dict(data)),
                    },
                )
        return next_seq

    # -- step rows (job_steps: the durable intent + safe receipt) ----------

    async def aensure_step_intent(
        self, run_id: str, spec: StepIntent
    ) -> dict[str, object]:
        """Insert the ``pending`` intent row; return the observed row.

        Idempotent: ``ON CONFLICT (run_id, seq) DO NOTHING`` — a replayed
        attempt observes the existing row instead of duplicating it. The
        row IS the exactly-once resume ledger.
        """
        async with self._sessions() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO job_steps (step_id, run_id, seq, capability_id)
                        VALUES (:step_id, :run_id, :seq, :capability_id)
                        ON CONFLICT (run_id, seq) DO NOTHING
                        """
                    ),
                    {
                        "step_id": uuid.uuid4(),
                        "run_id": uuid.UUID(run_id),
                        "seq": spec.seq,
                        "capability_id": spec.capability_id,
                    },
                )
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT step_id, state, receipt FROM job_steps "
                                "WHERE run_id = :run AND seq = :seq"
                            ),
                            {"run": uuid.UUID(run_id), "seq": spec.seq},
                        )
                    )
                    .mappings()
                    .one()
                )
        return {
            "step_id": str(row["step_id"]),
            "state": row["state"],
            "receipt": row["receipt"],
        }

    async def aread_step(self, run_id: str, seq: int) -> dict[str, object] | None:
        """Read one step row (the reconcile poll; never a mutation)."""
        async with self._sessions() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT step_id, capability_id, state, receipt FROM job_steps "
                            "WHERE run_id = :run AND seq = :seq"
                        ),
                        {"run": uuid.UUID(run_id), "seq": seq},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return {
            "step_id": str(row["step_id"]),
            "capability_id": row["capability_id"],
            "state": row["state"],
            "receipt": row["receipt"],
        }

    async def amark_running(
        self, run_id: str, seq: int, capability_id: str
    ) -> dict[str, object]:
        """``pending → running`` + ``step.started`` — the pre-effect fence.

        Committed BEFORE dispatch: an attempt that dies after this commit
        leaves ``running`` without a receipt, which is exactly the
        recoverable "effect unproven" fact the recovery path translates.
        The state guard makes the marker single-owner.
        """
        return await self._persist_step_transition(
            run_id=run_id,
            seq=seq,
            expected=StepState.PENDING,
            target=StepState.RUNNING,
            receipt=None,
            event_type=STEP_STARTED_EVENT_TYPE,
            event_data={"capability_id": capability_id},
        )

    async def arevert_running_to_pending(
        self, run_id: str, seq: int, capability_id: str
    ) -> None:
        """Orderly ``running → pending`` retract of the fence marker.

        Used ONLY on the approval-wait sentinel: the dispatcher proved the
        attempt ended before any effect, so the pre-effect marker is
        retracted and the step returns to receipt-less ``pending`` (the
        storage CHECK enforces receipt-less pending). The
        ``step.awaiting_approval`` event narrates the pause; no extra
        event is emitted here. Guarded: only the attempt holding the
        marker may retract it.
        """
        await self._persist_step_transition(
            run_id=run_id,
            seq=seq,
            expected=StepState.RUNNING,
            target=StepState.PENDING,
            receipt=None,
            event_type=None,
            event_data={},
        )

    async def apersist_outcome(
        self,
        run_id: str,
        seq: int,
        capability_id: str,
        state: StepState,
        receipt: Mapping[str, object],
        *,
        reason: str | None,
        op_id: str | None,
    ) -> None:
        """Persist the outcome + safe receipt + ``step.completed``.

        Guarded on ``state = 'running'``: only the attempt that set the
        fence marker may resolve it (single-owner write).
        """
        await self._persist_step_transition(
            run_id=run_id,
            seq=seq,
            expected=StepState.RUNNING,
            target=state,
            receipt=dict(receipt),
            event_type=STEP_COMPLETED_EVENT_TYPE,
            event_data={
                "capability_id": capability_id,
                "reason": reason,
                "op_id": op_id,
            },
        )

    async def apersist_pending_denial(
        self,
        run_id: str,
        seq: int,
        capability_id: str,
        receipt: Mapping[str, object],
        *,
        reason: str,
    ) -> None:
        """``pending → failed`` for a rejection before the fence marker.

        The attempt provably never started an effect (lock contention or
        a malformed request), so the honest terminal answer lands without
        a ``running`` episode; the safe receipt carries the stable reason.
        """
        await self._persist_step_transition(
            run_id=run_id,
            seq=seq,
            expected=StepState.PENDING,
            target=StepState.FAILED,
            receipt=dict(receipt),
            event_type=STEP_COMPLETED_EVENT_TYPE,
            event_data={
                "capability_id": capability_id,
                "reason": reason,
                "op_id": receipt.get("op_id"),
            },
        )

    async def acrash_window_outcome(
        self, run_id: str, server_id: str, seq: int, spec: StepIntent
    ) -> dict[str, object]:
        """Translate a recovered ``running``-without-receipt attempt.

        A prior attempt died after the fence marker with its effect
        unproven (guard rule 2). Persist ``outcome_unknown`` + a receipt
        that references the still-held lease (if any) — never re-dispatch,
        never finalize. The reconciliation owner decides the outcome; the
        lease stays until a terminal verdict clears it (C04 rule 7).
        """
        receipt: dict[str, object] = {
            "v": 1,
            "op_id": None,
            "capability_id": spec.capability_id,
            "allowed": None,
            "reason": CRASH_RECOVERY_REASON,
            "effect": EFFECT_UNKNOWN,
            "payload_hash": canonical_payload_hash(spec.payload),
        }
        if spec.resource is not None:
            snapshot: LockSnapshot | None = None
            try:
                snapshot = await asyncio.to_thread(
                    self._locks.inspect, server_id, spec.resource
                )
            except ResourceLockError:
                snapshot = None
            if snapshot is not None and snapshot.owner_ref == _lock_owner_ref(
                run_id, seq
            ):
                receipt["lock"] = _lock_receipt_fields(
                    _lease_of_snapshot(snapshot),
                    released=False,
                    lapsed=snapshot.expired,
                )
        await self._persist_step_transition(
            run_id=run_id,
            seq=seq,
            expected=StepState.RUNNING,
            target=StepState.OUTCOME_UNKNOWN,
            receipt=receipt,
            event_type=STEP_COMPLETED_EVENT_TYPE,
            event_data={
                "capability_id": spec.capability_id,
                "reason": CRASH_RECOVERY_REASON,
                "op_id": None,
            },
        )
        return _outcome_from_receipt(StepState.OUTCOME_UNKNOWN.value, receipt)

    async def _persist_step_transition(
        self,
        *,
        run_id: str,
        seq: int,
        expected: StepState,
        target: StepState,
        receipt: dict[str, object] | None,
        event_type: str | None,
        event_data: Mapping[str, object],
    ) -> dict[str, object]:
        """One transaction: guarded step UPDATE + optional whole-stream event.

        The event cursor is allocated the lifecycle's way — next
        whole-stream ``seq`` under the ``job_runs`` row lock (the shared
        producer contract; the storage UNIQUE is the backstop). The state
        guard (``WHERE state = expected``) makes each transition
        single-owner; a lost guard is a loud integrity failure, never a
        silent overwrite. ``event_type=None`` persists the transition
        without an event (the orderly fence-marker retract; the awaiting
        event narrates that pause).
        """
        validate_step_transition(expected, target)
        async with self._sessions() as session:
            async with session.begin():
                locked = (
                    await session.execute(
                        text(
                            "SELECT run_id FROM job_runs WHERE run_id = :r FOR UPDATE"
                        ),
                        {"r": uuid.UUID(run_id)},
                    )
                ).scalar()
                if locked is None:
                    raise StepsError(
                        StepsErrorCode.RUN_NOT_FOUND,
                        "no job_runs row for this run_id",
                    )
                next_seq: int | None = None
                if event_type is not None:
                    next_seq = (
                        await session.execute(
                            text(
                                "SELECT COALESCE(MAX(seq), 0) + 1 FROM job_events "
                                "WHERE run_id = :r"
                            ),
                            {"r": uuid.UUID(run_id)},
                        )
                    ).scalar_one()
                if receipt is None:
                    updated = await session.execute(
                        text(
                            "UPDATE job_steps SET state = :target "
                            "WHERE run_id = :run AND seq = :seq AND state = :expected "
                            "RETURNING step_id"
                        ),
                        {
                            "target": target.value,
                            "run": uuid.UUID(run_id),
                            "seq": seq,
                            "expected": expected.value,
                        },
                    )
                else:
                    updated = await session.execute(
                        text(
                            "UPDATE job_steps SET state = :target, receipt = :receipt "
                            "WHERE run_id = :run AND seq = :seq AND state = :expected "
                            "RETURNING step_id"
                        ),
                        {
                            "target": target.value,
                            "receipt": json.dumps(receipt),
                            "run": uuid.UUID(run_id),
                            "seq": seq,
                            "expected": expected.value,
                        },
                    )
                row = updated.first()
                if row is None:
                    raise StepsError(
                        StepsErrorCode.STEP_STATE_GUARD,
                        "step row is not in the expected pre-transition state",
                    )
                step_id = str(row[0])
                if event_type is not None:
                    data = {
                        "step_id": step_id,
                        "seq": seq,
                        **{
                            key: value
                            for key, value in event_data.items()
                            if value is not None
                        },
                    }
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
                            "seq": next_seq,
                            "ts": datetime.now(timezone.utc),
                            "type": event_type,
                            "v": EVENT_PAYLOAD_VERSION,
                            "data": json.dumps(data),
                        },
                    )
        return {"step_id": step_id, "state": target.value}

    # -- approval lookup (read-only over the frozen 0003 schema) ----------

    async def alookup_pending_approval(
        self, capability_id: str, server_id: str, payload_hash: str
    ) -> str | None:
        """Newest PENDING decision bound to this exact operation (C03 rule 4).

        Read-only SQL over the frozen ``approvals`` schema — consumption
        stays exclusively inside the dispatcher's approval seam. The
        binding match is exact: capability, target, canonical payload
        hash. A decision under a different policy revision fails at
        consume time and fails the step honestly (never silently retried).
        """
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT approval_id FROM approvals "
                        "WHERE capability_id = :cap AND server_id = :srv "
                        "AND payload_hash = :hash AND consumed_at IS NULL "
                        "ORDER BY created_at DESC, approval_id LIMIT 1"
                    ),
                    {
                        "cap": capability_id,
                        "srv": uuid.UUID(server_id),
                        "hash": payload_hash,
                    },
                )
            ).first()
        return None if row is None else str(row[0])

    # -- one guarded attempt (the DBOS step body) --------------------------

    async def aexecute_attempt(
        self,
        intent: RunIntent,
        spec: StepIntent,
        approval_id: str | None,
    ) -> dict[str, object]:
        """One exactly-once attempt of one step (see module docstring).

        Resume-first: a persisted terminal/unknown row IS the answer. The
        fence marker commits before dispatch; the outcome commits after;
        the lock spans the effect with heartbeats and is released only on
        a proven terminal outcome (kept on ``outcome_unknown``).
        """
        payload_hash = canonical_payload_hash(spec.payload)
        row = await self.aensure_step_intent(intent.run_id, spec)
        state = str(row["state"])
        if state == StepState.RUNNING.value:
            return await self.acrash_window_outcome(
                intent.run_id, intent.server_id, spec.seq, spec
            )
        if state != StepState.PENDING.value:
            return _outcome_from_receipt(state, row["receipt"] or {})

        lease: LockLease | None = None
        if spec.resource is not None:
            try:
                lease = await asyncio.to_thread(
                    self._locks.acquire,
                    intent.server_id,
                    spec.resource,
                    owner_ref=_lock_owner_ref(intent.run_id, spec.seq),
                )
            except ResourceLockError as error:
                reason = f"lock_{error.code.value}"
                receipt = _denial_receipt(
                    capability_id=spec.capability_id,
                    payload_hash=payload_hash,
                    reason=reason,
                    op_id=None,
                )
                await self.apersist_pending_denial(
                    intent.run_id,
                    spec.seq,
                    spec.capability_id,
                    receipt,
                    reason=reason,
                )
                return _outcome_from_receipt(StepState.FAILED.value, receipt)

        await self.amark_running(intent.run_id, spec.seq, spec.capability_id)

        heartbeat = _Heartbeat(self._locks, lease) if lease is not None else None
        if heartbeat is not None:
            heartbeat.start()
        dispatch_outcome: DispatchOutcome | None = None
        step_state: StepState | None = None
        reason: str | None = None
        receipt: dict[str, object] | None = None
        try:
            try:
                dispatch_outcome = await self._dispatcher.dispatch(
                    context=intent.context,
                    capability_id=spec.capability_id,
                    payload=spec.payload,
                    approval_id=(
                        uuid.UUID(approval_id) if approval_id is not None else None
                    ),
                )
            except DispatchError as error:
                # Malformed request: rejected before any state was read and
                # before any effect was planned (the dispatcher's contract).
                reason = f"dispatch_{error.code.value}"
                receipt = _denial_receipt(
                    capability_id=spec.capability_id,
                    payload_hash=payload_hash,
                    reason=reason,
                    op_id=None,
                )
                step_state = StepState.FAILED
            except Exception:
                # Infrastructure failure inside the dispatcher's gates
                # propagates only BEFORE the effect (executor exceptions are
                # captured by the dispatcher); the attempt dies honestly as
                # failed-never-attempted rather than as any effect claim.
                reason = "dispatch_infra_failed"
                receipt = _denial_receipt(
                    capability_id=spec.capability_id,
                    payload_hash=payload_hash,
                    reason=reason,
                    op_id=None,
                )
                step_state = StepState.FAILED
            else:
                assert dispatch_outcome is not None
                state_value, reason, receipt = _translate_outcome(dispatch_outcome)
                if state_value != WAIT_FOR_APPROVAL:
                    step_state = StepState(state_value)
        finally:
            if heartbeat is not None:
                heartbeat.stop()

        if step_state is None:
            # The approval-wait sentinel: the dispatcher proved the attempt
            # ended BEFORE any effect, so the fence marker is retracted
            # (orderly ``running → pending``), the lease is released, and
            # the row stays ``pending`` with NO receipt (the storage CHECK
            # enforces receipt-less pending).
            await self.arevert_running_to_pending(
                intent.run_id, spec.seq, spec.capability_id
            )
            if lease is not None:
                await asyncio.to_thread(self._release_quietly, lease)
            assert dispatch_outcome is not None  # the wait implies a receipt
            return {
                "state": WAIT_FOR_APPROVAL,
                "reason": None,
                "op_id": str(dispatch_outcome.op_id),
                "capability_id": spec.capability_id,
                "payload_hash": payload_hash,
                "policy_revision": dispatch_outcome.policy_revision,
            }

        assert receipt is not None
        terminal_proven = step_state in TERMINAL_STEP_STATES
        receipt["lock"] = None
        if lease is not None:
            if terminal_proven:
                released, lapsed = await asyncio.to_thread(
                    self._release_measured, lease
                )
            else:
                released, lapsed = (
                    False,
                    (heartbeat.lapsed if heartbeat is not None else False),
                )
            receipt["lock"] = _lock_receipt_fields(
                lease, released=released, lapsed=lapsed
            )
        await self.apersist_outcome(
            intent.run_id,
            spec.seq,
            spec.capability_id,
            step_state,
            receipt,
            reason=reason,
            op_id=None if dispatch_outcome is None else str(dispatch_outcome.op_id),
        )
        return {
            "state": step_state.value,
            "reason": reason,
            "op_id": None if dispatch_outcome is None else str(dispatch_outcome.op_id),
            "receipt": receipt,
        }

    # -- lock helpers -------------------------------------------------------

    def _release_quietly(self, lease: LockLease) -> None:
        try:
            self._locks.release(lease)
        except ResourceLockError:
            pass  # nothing was attempted; a lapse is fenced and recorded

    def _release_measured(self, lease: LockLease) -> tuple[bool, bool]:
        """Release; return ``(released, lapsed_or_lost)`` for the receipt."""
        try:
            self._locks.release(lease)
            return True, False
        except ResourceLockError:
            return False, True


def _lease_of_snapshot(snapshot: LockSnapshot) -> LockLease:
    """Rebuild lease identity fields from an observed row (reference only)."""
    return LockLease(
        server_id=snapshot.server_id,
        resource=snapshot.resource,
        owner_token=snapshot.owner_token,
        boot_id=snapshot.boot_id,
        owner_ref=snapshot.owner_ref,
        lease_ttl=snapshot.lease_ttl,
        expires_at=snapshot.lease_expires_at,
        acquired_at=snapshot.acquired_at,
    )


# ---------------------------------------------------------------------------
# process-level composition (worker path / test injection)
# ---------------------------------------------------------------------------

_EXECUTOR: StepExecutor | None = None


def configure_steps(executor: StepExecutor) -> None:
    """Inject the composed executor (worker-compose/test path)."""
    global _EXECUTOR
    if not isinstance(executor, StepExecutor):
        raise StepsError(
            StepsErrorCode.INVALID_WIRING, "executor must be a StepExecutor"
        )
    if _EXECUTOR is not None:
        raise StepsError(
            StepsErrorCode.ALREADY_CONFIGURED,
            "step execution is already configured in this process",
        )
    _EXECUTOR = executor


def _resolve_steps() -> StepExecutor:
    if _EXECUTOR is None:
        raise StepsError(
            StepsErrorCode.EXECUTOR_NOT_CONFIGURED,
            "no StepExecutor composed in this process; call configure_steps() "
            "before runs execute",
        )
    return _EXECUTOR


def reset_steps(engine: AsyncEngine | None = None) -> None:
    """Forget the injected executor (test isolation only)."""
    global _EXECUTOR
    _EXECUTOR = None
    if engine is not None:
        engine.sync.dispose()


# ---------------------------------------------------------------------------
# the DBOS steps (sync; every async seam runs on the executor's one loop)
# ---------------------------------------------------------------------------


@DBOS.step(name="steps.run_started")
def _step_run_started(payload_json: str) -> str:
    """Observe-and-record the run's ``queued`` anchor + ``running``."""
    executor = _resolve_steps()
    intent = parse_run_intent(payload_json)
    return executor.run(executor.aobserve_run_started(intent.run_id))


@DBOS.step(name="steps.execute")
def _step_execute(
    payload_json: str, spec_json: str, approval_id: str | None = None
) -> dict[str, object]:
    """One exactly-once guarded attempt of one step (checkpointed)."""
    intent = parse_run_intent(payload_json)
    spec = _parse_step_spec(json.loads(spec_json), 0)
    executor = _resolve_steps()
    return executor.run(executor.aexecute_attempt(intent, spec, approval_id))


@DBOS.step(name="steps.awaiting_approval")
def _step_awaiting_approval(
    payload_json: str, spec_json: str, wait_info_json: str
) -> str:
    """Record ``waiting_approval`` + the wait event; return the deadline."""
    executor = _resolve_steps()
    intent = parse_run_intent(payload_json)
    spec = _parse_step_spec(json.loads(spec_json), 0)
    wait_info = json.loads(wait_info_json)
    return executor.run(
        executor.aobserve_enter_wait(
            intent.run_id,
            target=RunState.WAITING_APPROVAL,
            event_type=STEP_AWAITING_APPROVAL_EVENT_TYPE,
            event_data={
                "capability_id": spec.capability_id,
                "payload_hash": wait_info.get("payload_hash"),
                "policy_revision": wait_info.get("policy_revision"),
            },
            wait_seconds=intent.approval_wait_seconds,
            step_seq=spec.seq,
        )
    )


@DBOS.step(name="steps.approval_lookup")
def _step_approval_lookup(payload_json: str, spec_json: str) -> str | None:
    """Find the pending decision bound to this exact operation, if any."""
    executor = _resolve_steps()
    intent = parse_run_intent(payload_json)
    spec = _parse_step_spec(json.loads(spec_json), 0)
    return executor.run(
        executor.alookup_pending_approval(
            spec.capability_id,
            intent.server_id,
            canonical_payload_hash(spec.payload),
        )
    )


@DBOS.step(name="steps.wait_resumed")
def _step_wait_resumed(payload_json: str, from_state: str) -> str:
    """Record ``<wait> → running`` when a park ends (observe)."""
    executor = _resolve_steps()
    intent = parse_run_intent(payload_json)
    return executor.run(executor.aobserve_wait_resumed(intent.run_id, from_state))


@DBOS.step(name="steps.reconciling")
def _step_reconciling(payload_json: str, spec_json: str, outcome_json: str) -> str:
    """Record ``reconciling`` + the wait event; return the deadline."""
    executor = _resolve_steps()
    intent = parse_run_intent(payload_json)
    spec = _parse_step_spec(json.loads(spec_json), 0)
    outcome = json.loads(outcome_json)
    return executor.run(
        executor.aobserve_enter_wait(
            intent.run_id,
            target=RunState.RECONCILING,
            event_type=STEP_RECONCILING_EVENT_TYPE,
            event_data={
                "capability_id": spec.capability_id,
                "op_id": outcome.get("op_id"),
                "reason": outcome.get("reason"),
            },
            wait_seconds=intent.reconcile_wait_seconds,
            step_seq=spec.seq,
        )
    )


@DBOS.step(name="steps.read_step")
def _step_read_step(payload_json: str, seq: int) -> dict[str, object] | None:
    """Poll one step row for a reconciliation resolution."""
    intent = parse_run_intent(payload_json)
    executor = _resolve_steps()
    return executor.run(executor.aread_step(intent.run_id, seq))


@DBOS.step(name="steps.run_finished")
def _step_run_finished(payload_json: str) -> str:
    """Record ``running → succeeded`` after the last step (observe)."""
    intent = parse_run_intent(payload_json)
    executor = _resolve_steps()

    async def _finish() -> str:
        current = await executor.lifecycle.current_state(intent.run_id)
        if current is RunState.RUNNING:
            await executor.lifecycle.record_transition(
                intent.run_id, RunState.SUCCEEDED
            )
            current = RunState.SUCCEEDED
        assert current is not None
        return current.value

    return executor.run(_finish())


@DBOS.step(name="steps.run_failed")
def _step_run_failed(payload_json: str, reason: str) -> str:
    """Record the run ``failed`` through the legal transition path."""
    intent = parse_run_intent(payload_json)
    executor = _resolve_steps()
    return executor.run(executor.afail_run(intent.run_id, reason))


def _record_quietly(intent: RunIntent, action: str) -> None:
    """Best-effort plain (non-checkpointed) run translation.

    Used on the cancellation path: DBOS raises its cancellation exception
    AT checkpoint boundaries, so any ``DBOS.step`` call here would raise
    before executing — the translation must be plain calls. Idempotent by
    observation; failures leave the engine's own exception as the truth.
    """
    executor: StepExecutor | None = None
    try:
        executor = _resolve_steps()
    except StepsError:
        return
    try:
        if action == "cancel":
            executor.run(executor.acancel_run(intent.run_id))
        else:
            executor.run(executor.afail_run(intent.run_id, "handler_error"))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# the one registered run handler
# ---------------------------------------------------------------------------


def run_capability_steps(payload_json: str) -> dict[str, object]:
    """Execute a run's capability steps: ordered, resumable, exactly-once.

    Registered as :data:`HANDLER_NAME` (module import) and executed inside
    the engine's durable trampoline workflow. Raises :class:`StepsRunFailed`
    after recording a ``failed`` run (engine ERROR — both sides agree) and
    re-raises the engine's cancellation exception after recording
    ``cancelled``. Success returns the per-step summary document.
    """
    intent = parse_run_intent(payload_json)
    try:
        _step_run_started(payload_json)
        results: list[dict[str, object]] = []
        for spec in intent.steps:
            spec_json = canonical_json(spec.to_json())
            outcome = _settle_step(payload_json, spec_json, spec)
            results.append(
                {
                    "seq": spec.seq,
                    "state": outcome["state"],
                    "reason": outcome.get("reason"),
                }
            )
            state = str(outcome["state"])
            if state == StepState.SUCCEEDED.value:
                continue
            if state == StepState.FAILED.value:
                reason = str(outcome.get("reason"))
                _step_run_failed(payload_json, reason)
                raise StepsRunFailed(reason)
            if state == StepState.CANCELLED.value:
                _record_quietly(intent, "cancel")
                raise StepsRunFailed("step_cancelled")
            # Unreachable post-settle (parks resolve or deadline-fail);
            # defensive: fail honestly instead of claiming anything.
            _step_run_failed(payload_json, RECONCILE_WAIT_DEADLINE_REASON)
            raise StepsRunFailed(RECONCILE_WAIT_DEADLINE_REASON)
        _step_run_finished(payload_json)
        return {"run_id": intent.run_id, "state": "succeeded", "steps": results}
    except dbos_error.DBOSWorkflowCancelledError:
        _record_quietly(intent, "cancel")
        raise
    except StepsError:
        _record_quietly(intent, "fail")
        raise


def _settle_step(
    payload_json: str, spec_json: str, spec: StepIntent
) -> dict[str, object]:
    """Drive one step to a resolved outcome (workflow context).

    Approval waits and unproven effects park the durable workflow on
    :func:`DBOS.recv` polls against the persisted deadline; the durable
    state (a bound approval decision, a reconciler-resolved step row)
    decides — never a retry and never an invented success.
    """
    intent = parse_run_intent(payload_json)
    outcome = _step_execute(payload_json, spec_json)
    if outcome["state"] == WAIT_FOR_APPROVAL:
        deadline = _deadline_of(
            _step_awaiting_approval(payload_json, spec_json, json.dumps(outcome))
        )
        while True:
            approval_id = _step_approval_lookup(payload_json, spec_json)
            if approval_id is not None:
                _step_wait_resumed(payload_json, RunState.WAITING_APPROVAL.value)
                outcome = _step_execute(payload_json, spec_json, approval_id)
                if outcome["state"] != WAIT_FOR_APPROVAL:
                    break
                # Still waiting (e.g. the policy revision moved): the
                # observe-skip keeps the ORIGINAL persisted deadline.
                deadline = _deadline_of(
                    _step_awaiting_approval(
                        payload_json, spec_json, json.dumps(outcome)
                    )
                )
                continue
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                _step_wait_resumed(payload_json, RunState.WAITING_APPROVAL.value)
                return {
                    "state": StepState.FAILED.value,
                    "reason": APPROVAL_WAIT_DEADLINE_REASON,
                }
            DBOS.recv(
                topic=RESUME_TOPIC,
                timeout_seconds=min(
                    intent.wait_poll_seconds,
                    max(MIN_RECV_TIMEOUT_SECONDS, remaining),
                ),
            )
    if outcome["state"] == StepState.OUTCOME_UNKNOWN.value:
        deadline = _deadline_of(
            _step_reconciling(payload_json, spec_json, json.dumps(outcome))
        )
        while True:
            row = _step_read_step(payload_json, spec.seq)
            if row is not None and row["state"] in {
                item.value for item in TERMINAL_STEP_STATES
            }:
                _step_wait_resumed(payload_json, RunState.RECONCILING.value)
                return _outcome_from_receipt(str(row["state"]), row["receipt"] or {})
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                _step_wait_resumed(payload_json, RunState.RECONCILING.value)
                return {
                    "state": StepState.FAILED.value,
                    "reason": RECONCILE_WAIT_DEADLINE_REASON,
                }
            DBOS.recv(
                topic=RESUME_TOPIC,
                timeout_seconds=min(
                    intent.wait_poll_seconds,
                    max(MIN_RECV_TIMEOUT_SECONDS, remaining),
                ),
            )
    return outcome


def _deadline_of(iso: str) -> datetime:
    deadline = datetime.fromisoformat(iso)
    if deadline.tzinfo is None:
        raise StepsError(
            StepsErrorCode.INVALID_WAIT_STATE, "persisted deadline lost its timezone"
        )
    return deadline


# Registration at import (the engine trampoline resolves the handler by
# name; worker entrypoints import this module via MAI_WORKER_HANDLERS).
_engine.register_job_handler(HANDLER_NAME, run_capability_steps)
