"""Durable agent conversation runs: PydanticAI sessions over the one job engine.

Card ``06-agent-run`` against contracts.md **C06** (rules 1–4, 6: separated
provider config/selection, call-time credential resolution, bounded context,
the agent-to-tool bridge through the ONE dispatcher, fake-LLM-only protocol
tests) and **C05** (rules 1–5: one engine, the frozen run lifecycle, submit
dedup, the shared event stream) plus the SERVICES "End-to-end execution
route" (agent proposes tool calls; the common dispatcher gates every
effect). This module is the run facade's agent executor: a run whose handler
is :data:`HANDLER_NAME` executes a bounded PydanticAI conversation whose
tool calls are gated capability executions through the reviewed
:class:`~mai.agents.tools.CapabilityToolBridge` — the same boundary the
model-facing tools and the deterministic steps path share. The module owns:

- **The agent run intent** — a strict, value-free payload document
  (``parse_agent_run_intent``): run/server identity, the run's
  :class:`~mai.policy.evaluate.ExecutionContext` (identity-matched, never
  retargetable), optional explicit model selection, role + bounded task,
  conversation budgets and the reconciliation wait budgets.
- **Context + model composition** (checkpointed steps) — the model
  (connection, profile) pair is pinned by the reviewed
  :class:`~mai.agents.model_store.ModelConfigStore.bind` BEFORE the
  conversation starts; credentials still resolve at call time through the
  composed ``.env`` source (C03 rule 6, C06 rule 1). The server prompt +
  profile are pinned by :func:`~mai.agents.context.load_server_context` and
  the bounded model payload assembled by :func:`~mai.agents.context.assemble`
  (C06 rules 2–3); a safe summary event (budget report, revision pins, no
  content) lands in the shared per-run stream. The conversation
  ``message_history`` seed is ONE checkpointed step
  (:meth:`AgentRunExecutor.abuild_conversation_seed`): continued runs load
  the prior role-preserving turns from the canonical conversation store as
  native PydanticAI messages (06-context-continuity — never synthetic
  wrappers, never authority elevation); the canonical assistant reply is
  recorded through the idempotent producer step
  (:meth:`AgentRunExecutor.arecord_assistant_reply`).
- **The durable conversation loop** — each model round is ONE checkpointed
  ``DBOS.step`` that ends either in the assistant's final text or in a
  deferred tool-request list; each requested tool call is ONE exactly-once
  guarded attempt (below). The conversation (PydanticAI message history) is
  the payload of checkpointed steps, so the chosen engine adapter (DBOS,
  C05 rule 1) is the canonical persistence for conversation/run state:
  a recovered run replays completed rounds from engine checkpoints and
  never re-asks the model about settled rounds.

Durability and exactly-once semantics (job-engine guard rules 1–3)
------------------------------------------------------------------

The handler :func:`run_agent_conversation` is registered as the
``agent.run`` run handler and executes inside the durable trampoline
workflow. The verify-then-mutate order is applied at the agent level:

1. a model round is checkpointed BEFORE any effect: the deferred request
   list (tool name, arguments, tool call ids) is durable before anything
   dispatches;
2. each requested call executes as ONE attempt of one ``job_steps`` row
   written by the reviewed :class:`~mai.jobs.steps.StepExecutor` machinery
   (intent row ``pending`` → the ``running`` fence marker committed BEFORE
   dispatch → outcome + safe receipt committed after). The step sequence
   number is DERIVED from the checkpointed round record
   (``(round, index) → seq``, :func:`tool_step_seq`), so a recovered
   attempt observes the same row instead of allocating a second one —
   the storage ``UNIQUE (run_id, seq)`` is the backstop;
3. a recovered ``running`` row WITHOUT a receipt is translated by the
   reviewed crash-window path into ``outcome_unknown`` and NEVER
   re-dispatched (guard rule 2): the model receives an honest
   ``unknown_effect`` tool result, the run parks ``reconciling`` and only
   the reconciliation owner (04-reconcile / 05-cancel-recovery) resolves
   the step — via the same terminal edges the steps card pins. Resolution
   feeds the conversation and the loop continues; the wait deadline is
   persisted in the ``agent.awaiting_reconciliation`` event, expiry
   resumes and fails the run.

Model failures classify through the reviewed
:func:`~mai.agents.models.classify_provider_error`
(``model_reachability`` / ``model_auth`` / ``model_rejected_request`` /
``model_provider`` / ``model_unknown``): the run records ``failed`` and the
engine lands ERROR — a deliberate facade resume re-executes only the
failed round step (checkpoints before it replay; executed tool calls
replay their receipts, never their effects). Cancellation: requested ≠
verified (guard rule 3) — the facade ``cancelled`` translation belongs to
the reviewed recovery owner (``RecoveryService.acancel_run`` applies the
engine truth to the diagram after the in-flight checkpointed round
settles); the handler's own cancellation catch is the defensive belt for
a cancellation exception surfacing inside the workflow function.

Approval semantics of the agent path: a privileged call without a bound
decision comes back from the dispatcher as
``approval_required_but_absent``; the model receives the bridge's
``approval_required`` tool result (the durable binding: payload hash,
policy revision, capability, target) and may finish. The row records the
denial honestly (``failed``, ``effect=not_attempted`` — nothing ran); the
approval + one-shot resume of that exact operation is the chat/resume
surface's (06-chat-api), built on the bridge's ``dispatch_once``
approval seam.

Deliberate boundaries (recorded limitations, card evidence): tool calls
carry no mutation-lock lease — resource identity is capability-specific
and the lock-fenced surface stays the declared-steps path; agent runs do
not park ``waiting_approval`` (the model answers inline) nor
``waiting_children`` (delegation is 06-child-runs); retrieval-backed
evidence assembly plugs in at the context seam (empty by default here).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Final

from dbos import DBOS
from dbos import error as dbos_error
from pydantic_ai import Agent
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    SystemPromptPart,
    UserPromptPart,
)
from pydantic_ai.tools import (
    DeferredToolRequests,
    DeferredToolResults,
    Tool,
)
from pydantic_ai_harness import (
    ClampOversizedMessages,
    ClearToolResults,
    SummarizingCompaction,
    TieredCompaction,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents import conversation_context
from mai.agents import conversation_store
from mai.agents import task_completion
from mai.agents import task_progress
from mai.agents import task_store
from mai.agents.intake import IntakeRejection, arun_intake_turn
from mai.agents.context import (
    MAX_ROLE_CHARS,
    ContextError,
    assemble,
    load_server_context,
)
from mai.agents.models import (
    ModelConfigError,
    ProviderFailureKind,
    ResolvedModel,
    build_model,
    classify_provider_error,
    parse_connection,
    parse_profile,
)
from mai.agents.model_store import (
    ModelBinding,
    ModelConfigStore,
    ModelStoreError,
)
from mai.agents.output_safety import (
    REDACTED,
    project_document,
    project_text,
    redact_values,
)
from mai.agents.tools import (
    TOOL_RESULT_SCHEMA_VERSION,
    BridgeError,
    CapabilityToolBridge,
    ToolResult,
    ToolStatus,
    tool_name_for,
)
from mai.capabilities.dispatch import (
    EFFECT_NOT_ATTEMPTED,
    EFFECT_UNKNOWN,
    CapabilityDispatcher,
    canonical_payload_hash,
)
from mai.capabilities.task_state import (
    TASK_PROPOSE_UPDATE_CAPABILITY,
    TASK_READ_CAPABILITY,
    TASK_UPDATE_ITEM_CAPABILITY,
)
from mai.contracts.conversations import (
    ConversationContractError,
    SteerAction,
)
from mai.contracts.jobs import (
    EVENT_PAYLOAD_VERSION,
    TERMINAL_STEP_STATES,
    RunState,
    StepState,
)
from mai.contracts.tasks import (
    Phase,
    PhaseAction,
    TaskContractError,
    TaskOutcome,
    validate_phase,
)
from mai.credentials.env import EnvFileError, EnvResolver
from mai.execution.commands import CommandRequest, CommandStatus
from mai.jobs import engine as _engine
from mai.jobs.engine import EngineError, validate_run_id
from mai.jobs.resource_locks import ResourceLockService
from mai.jobs.steps import (
    EXECUTION_TIMEOUT_REASON,
    RESUME_TOPIC,
    STEP_COMPLETED_EVENT_TYPE,
    WAIT_FOR_APPROVAL,
    StepExecutor,
    StepIntent,
    StepsError,
    StepsErrorCode,
    _translate_outcome,
)
from mai.plugins.registry import CapabilityRegistry
from mai.policy.evaluate import ExecutionContext, execution_context_from_json
from mai.skills.builtin import collect_skills_evidence

__all__ = [
    "AGENT_TOOL_MAX_PER_ROUND",
    "AGENT_TOOL_MAX_PER_RUN",
    "AGENT_TOOL_ROUND_SPAN",
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_RECONCILE_WAIT_SECONDS",
    "DEFAULT_WAIT_POLL_SECONDS",
    "HANDLER_NAME",
    "AGENT_INTAKE_EVENT_TYPE",
    "INTAKE_NO_EXECUTOR_KINDS",
    "MAX_HISTORY_BYTES",
    "MAX_MAX_ROUNDS",
    "MAX_RECONCILE_WAIT_SECONDS",
    "MAX_ROLE_LEN",
    "MAX_TASK_CHARS",
    "MAX_WAIT_POLL_SECONDS",
    "MIN_RECV_TIMEOUT_SECONDS",
    "RUN_PAYLOAD_VERSION",
    "STEER_TOPIC",
    "TOOL_STEP_SEQ_BASE",
    "AgentRunError",
    "AgentRunErrorCode",
    "AgentRunExecutor",
    "AgentRunFailed",
    "AgentRunIntent",
    "AgentStageError",
    "configure_agent_run",
    "initial_history_json",
    "parse_agent_run_intent",
    "reset_agent_run",
    "run_agent_conversation",
    "tool_step_seq",
]

# ---------------------------------------------------------------------------
# step-receipt secret hygiene (C2: zero secret persistence in receipts)
# ---------------------------------------------------------------------------

#: Capability-id prefix whose payloads may carry raw secret material
#: (the built-in native plugin's store surface).
_SECRET_CAPABILITY_PREFIX = "secret."


def _resolved_credential_values(steps: object, references: tuple[str, ...]) -> list[str]:
    """Best-effort resolved values of one dispatch's credential references.

    Reads the composed dispatcher's credential seam exactly like the
    approval reach-in above reads its approval store: the values are
    needed *here* only to keep them out of the durable receipt. Every
    failure (provider absent, reference unresolved) degrades to no
    scrubbing for that name — the dispatcher's own output boundary is
    the primary redaction layer.
    """
    if not references:
        return []
    dispatcher = getattr(steps, "_dispatcher", None)
    provider = getattr(dispatcher, "_credentials", None)
    if not callable(provider):
        return []
    try:
        resolver = provider()
    except Exception:
        return []
    values: list[str] = []
    for name in references:
        try:
            resolved = resolver.resolve(name)
        except Exception:
            continue
        raw = getattr(resolved, "value", None)
        if isinstance(raw, str) and raw:
            values.append(raw)
    return values


def _scrub_secret_text(text: str, secrets: list[str]) -> str:
    """Replace every resolved secret value in one receipt string.

    Raw and JSON-escaped forms both scrub: receipts embed
    JSON-serialized strings (double-encoded payloads, echoed tool
    output), where only the escaped form of the secret appears (C2).
    The value-form producer is shared with the dispatcher's own output
    boundary via :mod:`mai.agents.output_safety`.
    """
    return redact_values(text, secrets)


def _scrub_secret_document(value: object, secrets: list[str]) -> object:
    """Recursively scrub resolved secret values out of a receipt document."""
    if isinstance(value, str):
        return _scrub_secret_text(value, secrets)
    if isinstance(value, Mapping):
        return {
            key: _scrub_secret_document(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub_secret_document(item, secrets) for item in value]
    return value


def _scrubbed_step_params(
    capability_id: str,
    params: dict[str, object],
    resolved_values: list[str],
) -> dict[str, object]:
    """The durable ``receipt["params"]`` document, secret-free (C2).

    ``secret.*`` calls scrub their ``value`` field unconditionally — the
    plaintext the model supplied never becomes persisted audit surface —
    and every string field is scrubbed of resolved credential material.
    """
    scrubbed = dict(params)
    if capability_id.startswith(_SECRET_CAPABILITY_PREFIX) and "value" in scrubbed:
        scrubbed["value"] = REDACTED
    return {
        key: _scrub_secret_document(item, resolved_values)
        if isinstance(item, str)
        else item
        for key, item in scrubbed.items()
    }


# ---------------------------------------------------------------------------
# frozen vocabulary + bounds
# ---------------------------------------------------------------------------

#: The run-handler name this module registers (dotted slug; the engine
#: trampoline resolves it at execution time).
HANDLER_NAME: Final[str] = "agent.run"

#: Payload document version produced by this freeze.
RUN_PAYLOAD_VERSION: Final[int] = 1

#: Conversation budgets (C06 rule 3): bounded rounds, bounded parallel
#: tool calls per round, and a bounded total of durable tool-call rows per
#: run. The per-run tool budget mirrors the steps module's
#: ``MAX_STEPS_PER_RUN`` bound.
DEFAULT_MAX_ROUNDS: Final[int] = 16
MAX_MAX_ROUNDS: Final[int] = 32
AGENT_TOOL_MAX_PER_ROUND: Final[int] = 8
AGENT_TOOL_ROUND_SPAN: Final[int] = AGENT_TOOL_MAX_PER_ROUND
TOOL_STEP_SEQ_BASE: Final[int] = AGENT_TOOL_ROUND_SPAN
AGENT_TOOL_MAX_PER_RUN: Final[int] = 100

#: The bounded task text (the real per-model budget is the reviewed
#: context assembly's char arithmetic; this parse bound only fails closed
#: before any store read).
MAX_TASK_CHARS: Final[int] = 1024 * 1024

#: Role bound re-exported for the payload parser (validated again by the
#: reviewed assembly).
MAX_ROLE_LEN: Final[int] = MAX_ROLE_CHARS

#: Checkpointed conversation-history bound (C06 rule 3: explicit bound,
#: honest failure — never a silent truncation of run state). This fires
#: only as the LAST resort: the TieredCompaction capability below runs
#: first inside every model request, so a history that still exceeds
#: this bound after compaction is an honest structural failure.
MAX_HISTORY_BYTES: Final[int] = 2 * 1024 * 1024

#: Conversation-compaction budget (fraction of the model's context
#: window) the TieredCompaction escalation drives toward: cheap tiers
#: run first; the LLM summarizer only when they cannot reclaim enough.
COMPACTION_TARGET_FRACTION: Final[float] = 0.8
#: Per-part clamp ceiling (tokens) — the first, zero-LLM tier.
COMPACTION_CLAMP_MAX_PART_TOKENS: Final[int] = 50_000
#: How many most-recent tool-call/tool-return pairs keep their results
#: when the second, zero-LLM tier blanks older ones (pairing always kept).
COMPACTION_KEEP_TOOL_PAIRS: Final[int] = 3
#: Tail messages the summarizing tier preserves verbatim (it summarizes
#: everything older, with the run's own model).
COMPACTION_SUMMARY_KEEP_MESSAGES: Final[int] = 20

#: Reconciliation wait budgets (mirroring the steps module's shape; the
#: agent run parks on the same durable ``recv`` seam).
DEFAULT_RECONCILE_WAIT_SECONDS: Final[float] = 300.0
MAX_RECONCILE_WAIT_SECONDS: Final[float] = 3600.0
DEFAULT_WAIT_POLL_SECONDS: Final[float] = 5.0
MAX_WAIT_POLL_SECONDS: Final[float] = 300.0
MIN_RECV_TIMEOUT_SECONDS: Final[float] = 0.5

#: Stable failure reasons (value-free; envelope-style).
RECONCILE_WAIT_DEADLINE_REASON: Final[str] = "reconcile_wait_deadline"

#: Durable DBOS topic for mid-run user steering messages.
STEER_TOPIC: Final[str] = "mai.chat.steer"

#: Bounded page of pending conversation-steering rows consumed per safe
#: boundary (AGENTS.md mandatory-pagination invariant; deterministic
#: ordering, leftover rows stay pending for the next boundary).
STEERING_POLL_PAGE_LIMIT: Final[int] = 50

#: Stable ``agent.completed`` event key carrying a deterministic
#: boundary stop (``pause``/``cancel`` steering); absent on runs that
#: end normally so existing event shapes stay byte-identical.
STOPPED_BY_EVENT_KEY: Final[str] = "stopped_by"
ROUND_BUDGET_REASON: Final[str] = "round_budget_exhausted"
HISTORY_BUDGET_REASON: Final[str] = "history_budget_exhausted"
TOOL_BUDGET_REASON: Final[str] = "tool_call_budget_exhausted"
CRASH_RECOVERY_RESULT_REASON: Final[str] = "crash_recovery"

#: Stable additive ``agent.completed``/summary key qualifying a terminal
#: success whose episode is still open (W2): present ONLY when the real
#: final-seam task-outcome document reports an open domain outcome, next
#: to the honest document itself. Verified/failed/cancelled verdicts and
#: taskless runs keep every existing shape byte-identical.
TASK_OPEN_EVENT_KEY: Final[str] = "task_open_reason"

#: The one open-episode reason this runner emits: durable task criteria
#: or steps remain unverified (the completion review keeps the episode
#: open), so the run's success is qualified, never unqualified.
TASK_OPEN_UNVERIFIED_REASON: Final[str] = "unverified_task_outcome"

#: Domain outcomes that END the episode (§9); every other ``TaskOutcome``
#: value is open and leaves the task exactly as resumable as it was.
_TASK_CLOSED_OUTCOMES: Final[frozenset[str]] = frozenset(
    item.value
    for item in (
        TaskOutcome.VERIFIED,
        TaskOutcome.FAILED,
        TaskOutcome.CANCELLED,
    )
)

#: Event types this module owns in the shared per-run stream (C05 rule 4:
#: the envelope SHAPE is frozen in ``mai.contracts.jobs``, the type
#: vocabulary stays open — each producer owns its types). Row transitions
#: of tool calls emit the shared ``step.started``/``step.completed`` types
#: through the reviewed steps machinery, so the whole-run stream stays
#: coherent for the UI.
AGENT_STARTED_EVENT_TYPE: Final[str] = "agent.started"
AGENT_CONTEXT_EVENT_TYPE: Final[str] = "agent.context_assembled"
AGENT_HISTORY_EVENT_TYPE: Final[str] = "agent.conversation_history"
AGENT_INTAKE_EVENT_TYPE: Final[str] = "agent.intake_interpreted"
AGENT_ROUND_EVENT_TYPE: Final[str] = "agent.model_round"
AGENT_RECONCILING_EVENT_TYPE: Final[str] = "agent.awaiting_reconciliation"
AGENT_COMPLETED_EVENT_TYPE: Final[str] = "agent.completed"
AGENT_FAILED_EVENT_TYPE: Final[str] = "agent.failed"

#: Classified stage reason when the structured intake fails closed (P4):
#: the run records ``failed`` with ZERO model rounds and ZERO tool
#: effects — invalid intake can never enable execution (AC 1).
INTAKE_REJECTED_REASON: Final[str] = "intake_rejected"

#: A3 (agent-interaction-recovery): THE canonical assistant-visible
#: outcome recorded ONCE when an intake turn exhausts its bounded repair
#: (``intake_rejected``). Fixed safe text — it admits the interpretation
#: failed, states this attempt performed no effects and that any task
#: stays open; it never carries raw model output, field values, safe
#: codes or internal exception details (those live only in the
#: ``agent.intake_interpreted`` event). Recorded through the checkpointed
#: :meth:`AgentRunExecutor.arecord_assistant_reply` producer BEFORE the
#: run fails and releases its lease, so replays/restarts never duplicate
#: the message and legacy unlinked runs record nothing.
INTAKE_REJECTED_VISIBLE_REPLY: Final[str] = (
    "I could not reliably interpret that message, so this attempt "
    "performed no effects. Any task in this conversation remains open "
    "and unchanged; nothing was executed or verified. Please rephrase "
    "your message and try again."
)

#: Stable denial reason for a phase-gated tool effect (§6 phase gate):
#: a task-bound run executes steps only in ``act`` and reads only in
#: ``inspect``/``act`` — the ratified ``validate_phase`` vocabulary.
PHASE_NOT_ALLOWED_REASON: Final[str] = "phase_not_allowed"

#: Intake variants that finish the accepted turn WITHOUT launching the
#: executor loop (P1: no second action-model call for a plain answer;
#: ``clarify``/``blocked`` produce no task machinery and no effects).
INTAKE_NO_EXECUTOR_KINDS: Final[frozenset[str]] = frozenset(
    {"answer", "clarify", "blocked"}
)


class AgentRunErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_PAYLOAD = "invalid_payload"
    INVALID_WIRING = "invalid_wiring"
    EXECUTOR_NOT_CONFIGURED = "executor_not_configured"
    ALREADY_CONFIGURED = "already_configured"
    INVALID_WAIT_STATE = "invalid_wait_state"
    UNEXPECTED_DEFERRAL = "unexpected_deferral"


class AgentRunError(ValueError):
    """One structured, value-free agent-run rejection (fail closed)."""

    def __init__(self, code: AgentRunErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class AgentStageError(RuntimeError):
    """A classified conversation failure (bind/context/model stage).

    ``reason`` is the stable run-failure reason (value-free). Raised out
    of checkpointed steps; DBOS records the step error, the handler
    records the run ``failed`` and re-raises :class:`AgentRunFailed` so
    the engine lands ERROR — both sides agree (C05 rule 2).
    """

    def __init__(self, reason: str, error_type: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error_type = error_type


class AgentRunFailed(RuntimeError):
    """Raised AFTER the run was recorded ``failed`` (engine ERROR)."""


# ---------------------------------------------------------------------------
# run intent (strict, value-free, identity-matched)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentRunIntent:
    """The validated payload for :data:`HANDLER_NAME`.

    ``context`` is the run's :class:`~mai.policy.evaluate.ExecutionContext`
    (references only, C03 rule 2); its ``run_id``/``server_id`` must equal
    the envelope identity — a context can never retarget the run.
    ``connection``/``profile`` name an explicit model selection; when
    ``None`` the model store's stored selection is bound (C06 rule 1).
    """

    run_id: str
    server_id: str
    context: ExecutionContext
    role: str
    task: str
    connection: str | None
    profile: str | None
    max_rounds: int
    reconcile_wait_seconds: float
    wait_poll_seconds: float


def _reject_payload(detail: str) -> AgentRunError:
    return AgentRunError(AgentRunErrorCode.INVALID_PAYLOAD, detail)


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


def _bounded_rounds(value: object) -> int:
    if value is None:
        return DEFAULT_MAX_ROUNDS
    if isinstance(value, bool) or not isinstance(value, int):
        raise _reject_payload("rounds must be an integer")
    if not 1 <= value <= MAX_MAX_ROUNDS:
        raise _reject_payload(f"rounds must be within 1..{MAX_MAX_ROUNDS}")
    return value


def parse_agent_run_intent(payload_json: str) -> AgentRunIntent:
    """Validate one agent-run payload; return it typed (fail closed).

    Strict shape: exactly the frozen field set (selection, rounds and
    wait budgets optional); ``v`` is this freeze's version; identities
    are canonical UUIDs; the embedded execution context must name THIS
    run and THIS server; the task is non-empty and bounded (the real
    per-model budget is the reviewed assembly's char arithmetic).
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
        "connection",
        "profile",
        "role",
        "task",
        "rounds",
        "reconcile_wait_seconds",
        "wait_poll_seconds",
    }
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise _reject_payload(f"unknown field {unknown[0]!r}")
    missing = {"v", "run_id", "server_id", "context", "role", "task"} - set(document)
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
    role = document["role"]
    if (
        not isinstance(role, str)
        or not role.strip()
        or len(role) > MAX_ROLE_LEN
        or "\n" in role
    ):
        raise _reject_payload(
            f"role must be a single line of 1..{MAX_ROLE_LEN} characters"
        )
    task = document["task"]
    if not isinstance(task, str) or not task.strip() or len(task) > MAX_TASK_CHARS:
        raise _reject_payload(
            f"task must be a non-empty string of at most {MAX_TASK_CHARS} characters"
        )
    for field in ("connection", "profile"):
        value = document.get(field)
        if value is not None and (
            not isinstance(value, str) or not value or len(value) > 200
        ):
            raise _reject_payload(
                f"{field} must be a non-empty name of at most 200 characters"
            )
    return AgentRunIntent(
        run_id=run_id,
        server_id=server_id,
        context=context,
        role=role,
        task=task,
        connection=document.get("connection"),
        profile=document.get("profile"),
        max_rounds=_bounded_rounds(document.get("rounds")),
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
# pure helpers
# ---------------------------------------------------------------------------


def tool_step_seq(round_index: int, call_index: int) -> int:
    """The deterministic ``job_steps.seq`` of one tool call.

    Round-major encoding over the bounded per-round span: a recovered
    attempt of the same checkpointed call derives the SAME row (the
    exactly-once ledger key), and distinct (round, index) pairs never
    collide. The parse-level bounds keep the value far below 2**31.
    """
    return round_index * TOOL_STEP_SEQ_BASE + call_index + 1


def initial_history_json(system_prompt: str, user_message: str) -> str:
    """The conversation seed: system prompt + user message (one request).

    Built explicitly instead of through agent-side prompt injection so
    the checkpointed history is fully deterministic and the model sees
    exactly the reviewed assembly's payload (C06 rules 2–3).
    """
    seed = [
        ModelRequest(parts=[SystemPromptPart(content=system_prompt)]),
        ModelRequest(parts=[UserPromptPart(content=user_message)]),
    ]
    return ModelMessagesTypeAdapter.dump_json(seed).decode("utf-8")


def _progress_note_text(progress_hint_json: str | None) -> str | None:
    """The bounded rendered progress hint of one round (22-progress).

    Fail-closed both ways: an absent/``null``/malformed document renders
    NO note (the default keeps every legacy round byte-identical), and
    the rendered sentence is the derivation's own bounded text — value-
    free advisory guidance, never a tool result and never an effect.
    """
    if not progress_hint_json or progress_hint_json == "null":
        return None
    try:
        document = json.loads(progress_hint_json)
    except ValueError:
        return None
    hint = task_progress.render_hint_text(document)
    return f"[Progress hint]: {hint}" if hint else None


def compose_round_prompt(
    steered_prompt: str | None, progress_note: str | None
) -> str | None:
    """The next model round's prompt text (22-progress composition).

    User steering keeps its reviewed ``[User steering message]`` framing
    and authority; the runtime hint follows AFTER it, explicitly labeled
    ``[Progress hint]`` so it can never be mistaken for user input. Both
    absent → ``None`` (the legacy prompt argument for a plain deferred-
    result round, byte-identical).
    """
    if progress_note is None:
        return steered_prompt
    if steered_prompt is None:
        return progress_note
    return f"{steered_prompt}\n\n{progress_note}"


def _phase_action_for_capability(capability_id: str, effect_class: str) -> PhaseAction:
    """The §6 phase-gate action of one capability (pure, deterministic).

    Task-state tools are run control over the task record itself, never
    target-server effects: ``task.propose_update`` proposes plan content
    (:attr:`PhaseAction.PLAN_PROPOSAL`) and ``task.read`` /
    ``task.update_item`` are bookkeeping on the same record
    (:attr:`PhaseAction.METADATA_UPDATE`) — both ratified as allowed in
    every phase. Every OTHER capability maps by its descriptor's effect
    class: ``read`` collects evidence, anything else executes steps.
    """
    if capability_id == TASK_PROPOSE_UPDATE_CAPABILITY:
        return PhaseAction.PLAN_PROPOSAL
    if capability_id in (TASK_READ_CAPABILITY, TASK_UPDATE_ITEM_CAPABILITY):
        return PhaseAction.METADATA_UPDATE
    if effect_class == "read":
        return PhaseAction.COLLECT_EVIDENCE
    return PhaseAction.EXECUTE_STEPS


# ---------------------------------------------------------------------------
# structured intake (13-intake-runner): pure projections
# ---------------------------------------------------------------------------


def _intake_output_text(turn: object) -> str:
    """The bounded user-facing text of a NO-EXECUTOR intake variant.

    ``answer`` carries its own text; ``clarify`` renders the bounded open
    questions; ``blocked`` states the safe reason. Deterministic and
    structurally bounded (the contract caps every input field) — never a
    fabricated completion, never an invented effect claim.
    """
    kind = str(getattr(turn, "turn_kind", ""))
    if kind == "answer":
        return str(getattr(turn, "answer_text", "")).strip()
    understanding = str(getattr(turn, "understanding", "")).strip()
    if kind == "clarify":
        questions = getattr(turn, "open_questions", ()) or ()
        lines = [understanding or "The turn needs your input before it can proceed."]
        for question in questions:
            proposal_id = str(getattr(question, "proposal_id", "")).strip()
            body = str(getattr(question, "question", "")).strip()
            label = f"{proposal_id}: " if proposal_id else ""
            lines.append(f"- {label}{body}")
        return "\n".join(lines)
    if kind == "blocked":
        reason = str(getattr(turn, "reason", "")).strip()
        return f"{understanding}\nBlocked: {reason}".strip()
    return understanding  # pragma: no cover - caller gates the variant


def _intake_summary_document(intake: Mapping[str, object]) -> dict[str, object]:
    """The value-free intake projection for the run summary (C05 rule 4).

    Counts, dispositions and identities only — never snapshot content,
    model prose or repair feedback (those stay in the checkpointed step
    document and the bounded event fields, both non-content by contract).
    """
    usage = (
        intake.get("planner_usage")
        if isinstance(intake.get("planner_usage"), Mapping)
        else {}
    )
    return {
        "interpreted": bool(intake.get("interpreted")),
        "turn_kind": intake.get("turn_kind"),
        "applied": bool(intake.get("applied")),
        "task_id": intake.get("task_id"),
        "disposition": intake.get("disposition"),
        "planner_model_calls": usage.get("model_calls"),
        "planner_repaired": usage.get("repaired"),
    }


#: The frozen first and last lines of the rendered task block (the
#: surgical replacement markers of :func:`_history_with_refreshed_task_block`).
TASK_BLOCK_HEADER_LINE: Final[str] = (
    "# Active task episode (validated by structured intake)"
)
TASK_BLOCK_CLOSER: Final[str] = (
    "Work toward the plan under these constraints. The phase above "
    "gates target effects at every tool boundary (plan: none, "
    "inspect: read-only, act: granted capabilities)."
)


def _task_open_reason(summary: Mapping[str, object]) -> str | None:
    """The open-episode qualifier for one finishing run (pure, value-free).

    Returns the stable reason ONLY when the real final-seam task-outcome
    document (:func:`task_completion.aobserve_task_outcome`) reports an
    OPEN domain outcome (``in_progress`` / ``needs_input`` /
    ``partially_verified``): the run's own attempts may have succeeded
    technically, but durable criteria or steps remain unverified, so the
    terminal success must be explicitly qualified instead of presented
    as an unqualified completion. Closed verdicts (``verified`` /
    ``failed`` / ``cancelled``) and an absent/malformed document
    (taskless runs) keep every existing surface byte-identical.
    """
    document = summary.get(task_completion.TASK_OUTCOME_SUMMARY_KEY)
    if not isinstance(document, Mapping) or not document:
        return None
    outcome = document.get("outcome")
    if not isinstance(outcome, str) or outcome in _TASK_CLOSED_OUTCOMES:
        return None
    return TASK_OPEN_UNVERIFIED_REASON


def _task_context_block(header: Mapping[str, object]) -> str:
    """The deterministic bounded task-state block for the seed's system
    prompt (the task/phase binding of AC 2).

    Riding the LEADING ``SystemPromptPart`` is deliberate: the pinned
    compaction stack re-emits leading system parts verbatim ahead of any
    summary, so task id, phase, completed steps and constraints survive
    steering rounds AND compaction events (AC 2) — they cannot be
    silently summarized away mid-run. Every collection is the store's
    bounded page with the omission STATED, never hidden; constraint
    statements ride verbatim within that page bound (critical constraints
    are never truncated here).
    """
    lines = [
        "",
        TASK_BLOCK_HEADER_LINE,
        (
            f"task_id: {header.get('task_id')} "
            f"(revision {header.get('revision')}, "
            f"phase {header.get('requested_phase')})"
        ),
    ]
    objective = str(header.get("summary") or header.get("title") or "").strip()
    if objective:
        lines.append(f"objective: {objective}")
    steps = header.get("steps") if isinstance(header.get("steps"), list) else []
    if steps:
        lines.append("# Plan steps (step_id | status | verification | title)")
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            lines.append(
                f"- {step.get('step_id')} | {step.get('status')} | "
                f"{step.get('verification')} | {step.get('title')}"
            )
    if header.get("steps_omitted"):
        lines.append(f"(plan shows the first {len(steps)} steps; more exist)")
    constraints = (
        header.get("constraints")
        if isinstance(header.get("constraints"), list)
        else []
    )
    if constraints:
        lines.append("# Active constraints (these MUST hold; origin anchors the source)")
        for constraint in constraints:
            if not isinstance(constraint, Mapping):
                continue
            lines.append(
                f"- [{constraint.get('origin')}] {constraint.get('statement')}"
            )
    if header.get("constraints_omitted"):
        lines.append(
            f"(constraints show the first {len(constraints)}; more exist)"
        )
    lines.append(TASK_BLOCK_CLOSER)
    return "\n".join(lines)


def _history_with_refreshed_task_block(
    history_json: str, header: Mapping[str, object] | None
) -> str:
    """Re-bind the CURRENT task block into the seed's leading system part.

    The in-run refresh seam 13-intake-runner left open: after a
    successful ``task.update_item`` / ``task.propose_update`` the NEXT
    model round must see the current task state without restarting the
    run and without a second task episode. The fresh header renders
    through the SAME deterministic block renderer (same page bounds,
    same stated omissions) and is surgically swapped between the frozen
    markers inside the LEADING ``SystemPromptPart`` — the part the
    pinned compaction stack re-emits verbatim, so steering and
    compaction keep carrying the refreshed state. Idempotent: the same
    task state renders the same block, so a refresh adds no effect
    beyond the caller's own mutation. ``None``/empty header or a
    history whose system part never carried a bound block (legacy
    unbound run) changes NOTHING — fail-closed inert, never a second
    binding.
    """
    if not isinstance(header, Mapping) or not header:
        return history_json
    messages = ModelMessagesTypeAdapter.validate_json(
        history_json.encode("utf-8")
    )
    for message in messages:
        if type(message).__name__ != "ModelRequest":
            continue
        for part in message.parts:
            if getattr(part, "part_kind", None) != "system-prompt":
                continue
            content = str(part.content)
            start = content.find("\n" + TASK_BLOCK_HEADER_LINE)
            if start < 0:
                return history_json  # never bound: inert
            end_at = content.find(TASK_BLOCK_CLOSER, start)
            if end_at < 0:
                return history_json  # truncated/markerless: inert
            part.content = (
                content[:start]
                + _task_context_block(header)
                + content[end_at + len(TASK_BLOCK_CLOSER) :]
            )
            return ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")
    return history_json


def _result_document(
    *,
    intent: AgentRunIntent,
    capability_id: str,
    status: ToolStatus,
    reason: str,
    payload_hash: str | None,
    effect: str,
    policy_revision: int | None = None,
    output_text: str | None = None,
    output_truncated: bool | None = None,
    output_omitted_bytes: int | None = None,
) -> str:
    """The frozen-shape ToolResult JSON the model receives (value-free)."""

    def _hash(value: str | None) -> str:
        return value if value is not None else "non_canonical"

    document = ToolResult(
        schema_version=TOOL_RESULT_SCHEMA_VERSION,
        capability_id=capability_id,
        server_id=intent.context.server_id,
        run_id=intent.context.run_id,
        status=status,
        allowed=status is ToolStatus.EXECUTED,
        reason=reason,
        effect=effect,
        payload_hash=_hash(payload_hash),
        policy_revision=policy_revision,
        effect_class=None,
        approval_id=None,
        approval_spent=False,
        approval_code=None,
        executor_ok=(
            None if status is ToolStatus.UNKNOWN_EFFECT else status is ToolStatus.EXECUTED
        ),
        output_text=output_text,
        output_truncated=output_truncated,
        output_omitted_bytes=output_omitted_bytes,
        error_code=None,
        error_detail=None,
    )
    return document.to_json_str()


def _receipt_result_document(
    intent: AgentRunIntent, state: str, receipt: Mapping[str, object], *, resolved: bool
) -> str:
    """Rebuild the model-facing result from a persisted safe receipt.

    The replay/reconciliation path: output text is NOT part of the
    value-free receipt, so the rebuilt document carries the decision
    facts only (capability, status, reason, payload hash) — honest and
    sufficient for the model to continue without re-requesting the
    effect.
    """
    if state == StepState.SUCCEEDED.value:
        status: ToolStatus = ToolStatus.EXECUTED
    elif state == StepState.OUTCOME_UNKNOWN.value:
        status = ToolStatus.UNKNOWN_EFFECT
    else:
        status = ToolStatus.DENIED
    reason_value = receipt.get("reason")
    if resolved and reason_value:
        reason = f"reconciled:{reason_value}"
    elif resolved:
        reason = "reconciled"
    else:
        reason = str(reason_value or state)
    hash_value = receipt.get("payload_hash")
    return _result_document(
        intent=intent,
        capability_id=str(receipt.get("capability_id") or "unknown"),
        status=status,
        reason=reason,
        payload_hash=str(hash_value) if isinstance(hash_value, str) else None,
        effect=str(receipt.get("effect") or EFFECT_NOT_ATTEMPTED),
        policy_revision=(
            int(receipt["policy_revision"])  # type: ignore[arg-type]
            if isinstance(receipt.get("policy_revision"), int)
            else None
        ),
    )


def _timeout_result_document(
    intent: AgentRunIntent,
    receipt: Mapping[str, object],
) -> str:
    """The honest ToolResult at the reconciliation deadline (value-free).

    The wait budget expired while the effect's outcome was still
    unknown: ``status=unknown_effect`` + ``reason=execution_timeout_exceeded``.
    Never a claim of confirmed termination (no termination receipt
    exists) and never a retry instruction: ``allowed`` is false,
    ``executor_ok`` stays unknown and the settled step row is terminal —
    the runtime never re-dispatches it. (Recorded boundary: agent-path
    calls carry no mutation-lock lease; the resource-lock fence is the
    declared-steps surface.)
    """
    hash_value = receipt.get("payload_hash")
    return _result_document(
        intent=intent,
        capability_id=str(receipt.get("capability_id") or "unknown"),
        status=ToolStatus.UNKNOWN_EFFECT,
        reason=EXECUTION_TIMEOUT_REASON,
        payload_hash=str(hash_value) if isinstance(hash_value, str) else None,
        effect=str(receipt.get("effect") or EFFECT_UNKNOWN),
        policy_revision=(
            int(receipt["policy_revision"])  # type: ignore[arg-type]
            if isinstance(receipt.get("policy_revision"), int)
            else None
        ),
    )


def _parse_deferred_results(document: Mapping[str, object]) -> DeferredToolResults:
    """Rebuild the deferred-result map from its checkpointed document."""
    calls_value = document.get("calls")
    calls: dict[str, str] = {}
    if isinstance(calls_value, Mapping):
        for key, value in calls_value.items():
            if isinstance(key, str) and isinstance(value, str):
                calls[key] = value
    return DeferredToolResults(calls=calls)


def _binding_from_pins(pins: Mapping[str, object]) -> ModelBinding:
    """Re-parse the checkpointed binding pins (deterministic).

    The full :class:`~mai.agents.model_store.ModelBinding` — the parsed
    documents AND both version triples — so the reviewed context
    assembly can pin its payload to the exact bound revisions (C02
    rule 4, C06 rule 1).
    """
    connection = parse_connection(json.loads(str(pins["connection_text"])))
    profile = parse_profile(json.loads(str(pins["profile_text"])))
    return ModelBinding(
        connection=connection,
        profile=profile,
        connection_revision=int(pins["connection_revision"]),  # type: ignore[arg-type]
        profile_revision=int(pins["profile_revision"]),  # type: ignore[arg-type]
        connection_sha256=str(pins["connection_sha256"]),
        profile_sha256=str(pins["profile_sha256"]),
    )


def _tool_step_intent(seq: int, capability_id: str, call: Mapping[str, object]) -> StepIntent:
    """The steps-machinery intent row spec for one tool call.

    The payload document is the model's arguments exactly — the object
    the dispatcher gates and the crash-window receipt hashes.
    """
    payload = call.get("payload")
    return StepIntent(
        seq=seq,
        capability_id=capability_id,
        payload=dict(payload) if isinstance(payload, Mapping) else {},
    )


def _status_of_state(state: StepState) -> ToolStatus:
    if state is StepState.SUCCEEDED:
        return ToolStatus.EXECUTED
    if state is StepState.OUTCOME_UNKNOWN:
        return ToolStatus.UNKNOWN_EFFECT
    return ToolStatus.DENIED


def _deferring_tool(tool: Tool) -> Tool:
    """Rebuild one bridge tool as a DEFERRING tool (no inline execution).

    The tool SURFACE stays the reviewed bridge's (name, description,
    published input schema); the execution boundary moves to the durable
    seam: the run ends with the deferred request list BEFORE anything
    dispatches, the executor executes through ``dispatch_once`` in its
    own checkpointed step, and the continuation feeds the recorded
    result back as the tool return. PydanticAI's deferral is a transport
    mechanism here — never an authorization boundary (C06 rule 4).
    """

    def _defer(**_payload: Any) -> Any:
        raise CallDeferred

    definition = tool.tool_def
    return Tool.from_schema(
        _defer,
        name=definition.name,
        description=definition.description or "",
        json_schema=json.loads(json.dumps(definition.parameters_json_schema)),
    )


def _compaction_capability(context_limit: int | None) -> TieredCompaction[None]:
    """The per-run conversation-compaction capability (harness Tiered).

    Cheap-to-expensive escalation under one budget — ``target_fraction``
    of the model's context window: clamp any single oversized part,
    blank older tool results (the most recent ``keep_pairs`` pairs stay;
    call/return pairing is never broken), then LLM-summarize the oldest
    history with the run's own model (the tier's model inherits the
    request's). The processor runs INSIDE the model request
    (``before_model_request``), so the compacted history is exactly what
    ``result.all_messages()`` returns — the round's existing
    ``history_json`` checkpoint carries it; no separate durable step.

    Machina's system prompt — the leading :class:`SystemPromptPart` of
    the checkpointed seed — survives by construction: the harness
    extracts leading system parts and re-emits them verbatim ahead of
    any summary message; the zero-LLM tiers keep every message. The
    bound profile's declared ``context_limit`` overrides window
    resolution (an OpenAI-compatible gateway's model id is not in any
    registry); ``None`` falls back to the harness's conservative
    default window.
    """
    return TieredCompaction(
        tiers=[
            # Zero-LLM first: one oversized part clamped head+tail.
            ClampOversizedMessages(
                max_part_tokens=COMPACTION_CLAMP_MAX_PART_TOKENS
            ),
            # Zero-LLM second: older tool results blanked in place.
            # The tier's own trigger is bypassed inside TieredCompaction
            # (the orchestrator drives it) — ``max_tokens=1`` is the
            # documented "any valid value" placeholder.
            ClearToolResults(
                max_tokens=1, keep_pairs=COMPACTION_KEEP_TOOL_PAIRS
            ),
            # The expensive last tier: summarize everything older than
            # the kept tail with the run's model.
            SummarizingCompaction(
                max_messages=1,
                keep_messages=COMPACTION_SUMMARY_KEEP_MESSAGES,
            ),
        ],
        target_fraction=COMPACTION_TARGET_FRACTION,
        context_window=context_limit,
    )

# ---------------------------------------------------------------------------
# the shared whole-stream event producer (agent-side seam)
# ---------------------------------------------------------------------------


async def _astep_completed_event(
    session: AsyncSession, run_id: str, data: Mapping[str, object]
) -> int:
    """Append one ``step.completed`` event at the next whole-stream cursor.

    The shared producer contract (C05 rule 4, the lifecycle's allocation):
    the CALLER holds the ``job_runs`` row lock in its own transaction, the
    cursor is ``MAX(seq) + 1`` and the row lands with the frozen envelope
    columns (``event_id``/``ts``/``type``/``v``/``data``) — the storage
    ``UNIQUE (run_id, seq)`` is the backstop. Failure raises: a lost event
    is never swallowed as a recorded success.
    """
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
            "type": STEP_COMPLETED_EVENT_TYPE,
            "v": EVENT_PAYLOAD_VERSION,
            "data": json.dumps(dict(data)),
        },
    )
    return next_seq


# ---------------------------------------------------------------------------
# composition
# ---------------------------------------------------------------------------


class AgentRunExecutor:
    """Composed agent-run execution over the reviewed steps machinery.

    Wiring is constructor-checked (fail-closed, the sibling pattern). The
    row/event/crash-window machinery is the reviewed
    :class:`~mai.jobs.steps.StepExecutor`'s — ONE guarded-transition
    writer, ONE whole-stream event cursor, ONE loop runner; this executor
    adds the agent seams: model binding, context assembly, the
    PydanticAI conversation rounds and the bridge as the ONE dispatch
    seam for model-requested tool calls (C06 rule 4). Resource-lock
    leases stay the declared-steps surface (see module docstring).

    ``model_builder`` composes the provider seam: the production default
    is the reviewed :func:`~mai.agents.models.build_model`; protocol
    fixtures inject a deterministic fake model (C06 rule 6 — fake LLMs
    prove MAI plumbing only). It receives the validated selection and
    the executor's resolver, mirroring ``build_model``'s call-time
    credential resolution.
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        dispatcher: CapabilityDispatcher,
        registry: CapabilityRegistry,
        model_store: ModelConfigStore,
        data_root: Any,
        locks: ResourceLockService,
        resolver: Any = None,
        model_builder: Any = None,
        plugin_sync: Any = None,
        host_supervisor: Any = None,
    ) -> None:
        if not isinstance(sessions, async_sessionmaker):
            raise AgentRunError(
                AgentRunErrorCode.INVALID_WIRING,
                "sessions must be an async_sessionmaker on a NullPool engine",
            )
        if not isinstance(dispatcher, CapabilityDispatcher):
            raise AgentRunError(
                AgentRunErrorCode.INVALID_WIRING,
                "dispatcher must be the common CapabilityDispatcher",
            )
        if not isinstance(registry, CapabilityRegistry):
            raise AgentRunError(
                AgentRunErrorCode.INVALID_WIRING,
                "registry must be a CapabilityRegistry",
            )
        if not isinstance(model_store, ModelConfigStore):
            raise AgentRunError(
                AgentRunErrorCode.INVALID_WIRING,
                "model_store must be a ModelConfigStore",
            )
        if not isinstance(data_root, (str, bytes)) and not hasattr(
            data_root, "__fspath__"
        ):
            raise AgentRunError(
                AgentRunErrorCode.INVALID_WIRING,
                "data_root must be a filesystem path",
            )
        if not isinstance(locks, ResourceLockService):
            raise AgentRunError(
                AgentRunErrorCode.INVALID_WIRING,
                "locks must be the ResourceLockService",
            )
        if model_builder is not None and not callable(model_builder):
            raise AgentRunError(
                AgentRunErrorCode.INVALID_WIRING,
                "model_builder must be callable (selection, resolver) -> model",
            )
        self._registry = registry
        self._model_store = model_store
        self._data_root = data_root
        self._resolver = resolver
        self._model_builder = model_builder
        self._plugin_sync = plugin_sync
        self._host_supervisor = host_supervisor
        self._sessions = sessions
        #: The reviewed steps machinery: guarded transitions, the shared
        #: whole-stream event cursor, the crash-window translation and
        #: the ONE persistent loop for every async seam.
        self._steps = StepExecutor(
            sessions=sessions, dispatcher=dispatcher, locks=locks
        )

    @property
    def steps(self) -> StepExecutor:
        """The composed reviewed step executor (row/event machinery)."""
        return self._steps

    def run(self, coro: object) -> object:
        """Drive one coroutine on the executor's persistent loop."""
        return self._steps.run(coro)

    def close(self) -> None:
        """Stop the executor's loop thread (worker shutdown convenience)."""
        self._steps.close()

    # -- host reconciliation (self-healing unknown steps) ----------------

    async def areconcile_step(self, run_id: str, seq: int) -> bool:
        """Inspect one ``outcome_unknown`` step's remote receipt; resolve it.

        Returns ``True`` when the step row was updated to a terminal state.
        """
        row = await self._steps.aread_step(run_id, seq)
        if row is None:
            return False
        receipt = row.get("receipt")
        if not isinstance(receipt, Mapping):
            return False
        op_id_value = receipt.get("op_id")
        if not isinstance(op_id_value, str) or not op_id_value.strip():
            return False
        capability_id = str(row.get("capability_id", ""))
        if capability_id != "host.shell":
            return False
        if self._host_supervisor is None:
            return False
        try:
            op_id = uuid.UUID(op_id_value)
        except ValueError:
            return False
        try:
            payload = json.loads(str(row.get("payload", "{}")))
            timeout = float(payload.get("timeout", 120.0))
        except Exception:
            timeout = 120.0
        supervisor = self._host_supervisor
        try:
            if callable(self._resolver):
                resolver = self._resolver()
            else:
                resolver = self._resolver
            report = await asyncio.to_thread(
                supervisor.inspect_operation,
                uuid.UUID(run_id),
                op_id,
                resolver,
                timeout,
            )
        except Exception:
            return False
        if report.status not in (
            CommandStatus.SUCCEEDED,
            CommandStatus.FAILED,
            CommandStatus.CANCELLED,
        ):
            return False
        step_state = (
            StepState.SUCCEEDED
            if report.status is CommandStatus.SUCCEEDED
            else StepState.FAILED
        )
        reason = "reconciled_succeeded" if step_state is StepState.SUCCEEDED else "reconciled_failed"
        receipt_patch = dict(receipt)
        receipt_patch["resolution"] = "reconciled"
        if report.stdout is not None:
            receipt_patch["response"] = report.stdout
        if report.stderr is not None:
            receipt_patch["stderr"] = report.stderr
        if report.exit_code is not None:
            receipt_patch["exit_code"] = report.exit_code
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
                updated = await session.execute(
                    text(
                        "UPDATE job_steps SET state = :target, receipt = :receipt "
                        "WHERE run_id = :run AND seq = :seq AND state = :expected "
                        "RETURNING step_id"
                    ),
                    {
                        "target": step_state.value,
                        "receipt": json.dumps(receipt_patch),
                        "run": uuid.UUID(run_id),
                        "seq": seq,
                        "expected": StepState.OUTCOME_UNKNOWN.value,
                    },
                )
                step_id = updated.scalar()
                if step_id is None:
                    return False
                await _astep_completed_event(
                    session,
                    run_id,
                    {
                        "step_id": str(step_id),
                        "seq": seq,
                        "capability_id": str(row.get("capability_id", "unknown")),
                        "reason": reason,
                        "op_id": op_id_value,
                    },
                )
        return True

    async def aexpire_unknown_step(self, run_id: str, seq: int) -> dict[str, object]:
        """Expire one ``outcome_unknown`` step at the reconciliation deadline.

        ONE transaction, the reviewed machinery's way: the ``job_runs``
        row lock, the guarded ``outcome_unknown → failed`` transition with
        the timeout receipt (single-owner; a lost guard means a
        reconciler resolution landed concurrently — its durable truth is
        returned instead of the timeout) and the schema-valid
        ``step.completed`` timeout event at the next whole-stream cursor.

        The receipt keeps the attempt's own ``effect``/op identity and
        records ``reason=execution_timeout_exceeded`` +
        ``resolution=timeout``: a WAIT-BUDGET expiry — never a claim that
        the effect was terminated (no termination receipt exists here)
        and never a re-execution authorization. Any persistence failure
        raises (checkpointed step ⇒ the run fails loudly; a lost event is
        never swallowed as a recorded success).
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
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT step_id, capability_id, state, receipt "
                                "FROM job_steps WHERE run_id = :r AND seq = :s "
                                "FOR UPDATE"
                            ),
                            {"r": uuid.UUID(run_id), "s": seq},
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise StepsError(
                        StepsErrorCode.STEP_STATE_GUARD,
                        "no job_steps row for this seq",
                    )
                state = str(row["state"])
                if state != StepState.OUTCOME_UNKNOWN.value:
                    receipt: dict[str, object] = (
                        dict(row["receipt"])
                        if isinstance(row["receipt"], Mapping)
                        else {}
                    )
                    return {"state": state, "receipt": receipt}
                receipt = (
                    dict(row["receipt"])
                    if isinstance(row["receipt"], Mapping)
                    else {}
                )
                receipt["reason"] = EXECUTION_TIMEOUT_REASON
                receipt["resolution"] = "timeout"
                updated = await session.execute(
                    text(
                        "UPDATE job_steps SET state = :target, receipt = :receipt "
                        "WHERE run_id = :run AND seq = :seq AND state = :expected "
                        "RETURNING step_id"
                    ),
                    {
                        "target": StepState.FAILED.value,
                        "receipt": json.dumps(receipt),
                        "run": uuid.UUID(run_id),
                        "seq": seq,
                        "expected": StepState.OUTCOME_UNKNOWN.value,
                    },
                )
                step_id = updated.scalar()
                if step_id is None:
                    raise StepsError(
                        StepsErrorCode.STEP_STATE_GUARD,
                        "step row is not in the expected pre-transition state",
                    )
                event_data: dict[str, object] = {
                    "step_id": str(step_id),
                    "seq": seq,
                    "capability_id": str(row["capability_id"]),
                    "reason": EXECUTION_TIMEOUT_REASON,
                }
                if isinstance(receipt.get("op_id"), str) and receipt["op_id"]:
                    event_data["op_id"] = receipt["op_id"]
                await _astep_completed_event(session, run_id, event_data)
        return {"state": StepState.FAILED.value, "receipt": receipt}

    # -- run-state observation (delegated to the reviewed machinery) ------

    def observe_run_started(self, intent: AgentRunIntent) -> str:
        return self.run(self._steps.aobserve_run_started(intent.run_id))

    def fail_run(self, intent: AgentRunIntent, reason: str) -> str:
        return self.run(self._steps.afail_run(intent.run_id, reason))

    def cancel_run(self, intent: AgentRunIntent) -> str:
        return self.run(self._steps.acancel_run(intent.run_id))

    async def tool_row_count(self, intent: AgentRunIntent) -> int:
        """The run's durable tool-call rows so far (the per-run budget)."""
        async with self._sessions() as session:
            value = await session.execute(
                text("SELECT COUNT(*) FROM job_steps WHERE run_id = :r"),
                {"r": uuid.UUID(intent.run_id)},
            )
            return int(value.scalar_one())

    # -- model binding (checkpointed; C06 rule 1) --------------------------

    async def abind_model(self, intent: AgentRunIntent) -> dict[str, object]:
        """Pin the (connection, profile) pair for this run; return its pins.

        The reviewed ``ModelConfigStore.bind`` validates the pairing and
        endpoint-capability gating eagerly. The checkpointed pins carry
        the RAW stored documents plus both version triples: rounds
        re-parse them deterministically, so an administrator edit can
        never move a bound run (acceptance 2) while credentials still
        resolve at call time (C03 rule 6).
        """
        try:
            binding = self._model_store.bind(intent.connection, intent.profile)
            connection_doc = self._model_store.read_connection(binding.connection.name)
            profile_doc = self._model_store.read_profile(binding.profile.name)
        except (ModelStoreError, ModelConfigError) as error:
            code = getattr(error, "code", None)
            code_value = getattr(code, "value", str(code))
            raise AgentStageError(
                f"model_bind_{code_value or 'failed'}",
                error_type=type(error).__name__,
            ) from None
        await self._emit_once(
            intent.run_id,
            AGENT_STARTED_EVENT_TYPE,
            {
                "connection": binding.connection.name,
                "profile": binding.profile.name,
                "model": binding.profile.model,
                "role": intent.role,
                "rounds_budget": intent.max_rounds,
            },
        )
        return {
            "connection_name": binding.connection.name,
            "profile_name": binding.profile.name,
            "model": binding.profile.model,
            "connection_text": connection_doc.text,
            "profile_text": profile_doc.text,
            "connection_revision": binding.connection_revision,
            "profile_revision": binding.profile_revision,
            "connection_sha256": binding.connection_sha256,
            "profile_sha256": binding.profile_sha256,
        }

    # -- context assembly (checkpointed; C06 rules 2–3) --------------------

    async def aassemble_context(
        self, intent: AgentRunIntent, pins: Mapping[str, object]
    ) -> dict[str, object]:
        """Pin the bounded model payload; emit its safe summary event.

        The event carries the budget report and revision pins — never
        prompt or task content (C05 rule 4); the content lives in the
        checkpointed step result the conversation itself is built from.
        """
        binding = _binding_from_pins(pins)
        try:
            materials = load_server_context(self._data_root, intent.context.server_id)
            skill_items = collect_skills_evidence(
                self._data_root,
                intent.context.allowed_capabilities,
                max_items=8,
            )
            try:
                assembled = assemble(
                    binding,
                    materials,
                    role=intent.role,
                    task=intent.task,
                    evidence=skill_items,
                )
            except ContextError:
                if not skill_items:
                    raise
                assembled = assemble(
                    binding,
                    materials,
                    role=intent.role,
                    task=intent.task,
                )
        except ContextError as error:
            raise AgentStageError(
                f"context_{error.code.value}",
                error_type=type(error).__name__,
            ) from None
        await self._emit_once(
            intent.run_id,
            AGENT_CONTEXT_EVENT_TYPE,
            {
                "prompt_revision": assembled.prompt_revision,
                "prompt_sha256": assembled.prompt_sha256,
                "profile_revision": assembled.profile_revision,
                "profile_sha256": assembled.profile_sha256,
                "connection_revision": assembled.model_connection_revision,
                "profile_model_revision": assembled.model_profile_revision,
                "truncated": assembled.truncated,
                "budget": {
                    "context_limit_tokens": assembled.budget.context_limit_tokens,
                    "output_reserve_tokens": assembled.budget.output_reserve_tokens,
                    "char_budget": assembled.budget.char_budget,
                    "system_chars": assembled.budget.system_chars,
                    "task_chars": assembled.budget.task_chars,
                    "evidence_chars": assembled.budget.evidence_chars,
                    "used_chars": assembled.budget.used_chars,
                },
                "evidence_items": len(assembled.receipts),
            },
        )
        return {
            "system_prompt": assembled.system_prompt,
            "user_message": assembled.user_message,
        }

    # -- conversation seed + producer boundary (06-context-continuity) -----

    async def abuild_conversation_seed(
        self, intent: AgentRunIntent, assembled: Mapping[str, object]
    ) -> dict[str, object]:
        """Build the bounded native multi-turn ``message_history`` seed.

        When the run links to a canonical conversation (05-turn-submit's
        ``conversation_runs`` attachment), the seed carries the prior
        role-preserving turns as native ``ModelRequest(UserPromptPart)`` /
        ``ModelResponse(TextPart)`` messages (never synthetic wrappers,
        never authority elevation — see
        :mod:`mai.agents.conversation_context`). A legacy run without a
        link keeps the plain system+user seed AND the legacy event stream
        shape (no ``conversation_history`` event). The value-free
        assembly report lands once in the run stream for linked
        conversations (counts and completeness markers only, never
        content).
        """
        conversation_id = await conversation_context.aconversation_for_run(
            self._sessions, run_id=intent.run_id
        )
        system_prompt = str(assembled["system_prompt"])
        user_message = str(assembled["user_message"])
        if conversation_id is None:
            seed_json = initial_history_json(system_prompt, user_message)
            report: dict[str, object] = {
                "conversation_linked": False,
                "conversation_id": None,
                "history_completeness": "unavailable",
            }
            # No canonical conversation: the legacy event stream shape is
            # preserved EXACTLY (standalone agent.run runs — no prior
            # turns and no legacy history were loaded — emit no
            # conversation_history event).
        else:
            seed = await conversation_context.abuild_conversation_seed(
                self._sessions,
                conversation_id=str(conversation_id),
                server_id=intent.context.server_id,
                system_prompt=system_prompt,
                user_message=user_message,
                run_id=intent.run_id,
            )
            seed_json = seed.history_json
            report = {
                "conversation_linked": seed.report.conversation_linked,
                "conversation_id": seed.report.conversation_id,
                "history_completeness": seed.report.history_completeness,
                "missing_from_sequence": seed.report.missing_from_sequence,
                "missing_to_sequence": seed.report.missing_to_sequence,
                "prior_turns_total": seed.report.prior_turns_total,
                "turns_included_verbatim": seed.report.turns_included_verbatim,
                "turns_compacted": seed.report.turns_compacted,
                "excerpts_elided": seed.report.excerpts_elided,
                "non_chat_rows_skipped": seed.report.non_chat_rows_skipped,
                "current_turn_rows_excluded": (
                    seed.report.current_turn_rows_excluded
                ),
                "legacy_proven_submissions": (
                    seed.report.legacy_proven_submissions
                ),
                "seed_messages": seed.report.seed_messages,
                "seed_bytes": seed.report.seed_bytes,
                "budget_bytes": seed.report.budget_bytes,
            }
            await self._emit_once(
                intent.run_id, AGENT_HISTORY_EVENT_TYPE, dict(report)
            )
        return {"history_json": seed_json, "report": report}

    async def arecord_assistant_reply(
        self, intent: AgentRunIntent, output: str
    ) -> dict[str, object]:
        """Record ONE canonical assistant reply for this run (producer).

        Trusted producer boundary: idempotent per ``run_id`` (the safe
        ``agent-run:<run_id>`` ``content_ref`` marker is checked before
        the append, so replays/restarts never duplicate the row), C2
        projected content, provenance ``system`` (platform model
        producer per the frozen role/source matrix). Legacy runs without
        a conversation link and blank final outputs record nothing —
        honestly, never fabricated.
        """
        conversation_id = await conversation_context.aconversation_for_run(
            self._sessions, run_id=intent.run_id
        )
        if conversation_id is None:
            return {"recorded": False, "reason": "no_conversation_link"}
        row = await conversation_context.arecord_assistant_reply(
            self._sessions,
            conversation_id=str(conversation_id),
            server_id=intent.context.server_id,
            run_id=intent.run_id,
            content=output,
        )
        if row is None:
            return {"recorded": False, "reason": "empty_output"}
        return {
            "recorded": True,
            "message_id": str(row.message_id),
            "sequence": row.sequence,
        }

    # -- structured intake checkpoint + task binding (13-intake-runner) ----

    async def asource_message_of_turn(
        self, intent: AgentRunIntent
    ) -> uuid.UUID | None:
        """The accepted user turn THIS run interprets, when one exists.

        The canonical submit seam (05) persists the user message with
        ``client_request_id == run_id`` INSIDE the admission window and
        attaches the run BEFORE enqueue, so a started worker always
        observes it. A linked run without that row (defensive) or a
        legacy run without a link returns ``None`` — no intake, honest
        reason, never a guessed source message.
        """
        conversation_id = await conversation_context.aconversation_for_run(
            self._sessions, run_id=intent.run_id
        )
        if conversation_id is None:
            return None
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT message_id FROM conversation_messages "
                        "WHERE conversation_id = :cid "
                        "AND client_request_id = :rid "
                        "AND role = 'user' "
                        "ORDER BY seq, message_id LIMIT 1"
                    ),
                    {"cid": conversation_id, "rid": uuid.UUID(intent.run_id)},
                )
            ).first()
        return None if row is None else uuid.UUID(str(row[0]))

    async def ainterpret_intake_turn(
        self, intent: AgentRunIntent, pins: Mapping[str, object]
    ) -> dict[str, object]:
        """ONE checkpointed tool-free interpretation of the accepted turn.

        C4 idempotency is layered: the DBOS step checkpoint replays the
        SAME accepted result without re-running the intake model, and the
        intake seam's stable per-turn key deduplicates onto the SAME task
        in the store (a replayed turn can never create a second task).
        Usage accounting is honest and replay-invariant: the bounded
        planner record rides the checkpointed document and the value-free
        ``agent.intake_interpreted`` event. An unrepairable rejection
        (P4) raises :class:`AgentStageError` (``intake_rejected``) after
        emitting the value-free rejection fields — the run has ZERO
        rounds and ZERO tool effects.

        Legacy runs without a conversation link return
        ``{"interpreted": False}`` and keep today's behavior unchanged.
        """
        conversation_id = await conversation_context.aconversation_for_run(
            self._sessions, run_id=intent.run_id
        )
        if conversation_id is None:
            return {"interpreted": False, "reason": "no_conversation_link"}
        source_message_id = await self.asource_message_of_turn(intent)
        if source_message_id is None:
            return {"interpreted": False, "reason": "source_message_not_found"}

        binding = _binding_from_pins(pins)
        selection = binding.selection()
        active_resolver: Any = (
            self._resolver() if callable(self._resolver) else self._resolver
        )
        try:
            if self._model_builder is not None:
                resolved = self._model_builder(
                    selection=selection, resolver=active_resolver
                )
            else:
                resolved = build_model(selection, resolver=active_resolver)
        except (ModelConfigError, EnvFileError) as error:
            code = getattr(error, "code", None)
            code_value = getattr(code, "value", str(code))
            raise AgentStageError(
                f"model_credential_{code_value or 'failed'}",
                error_type=type(error).__name__,
            ) from None
        planner_model = (
            resolved.model if isinstance(resolved, ResolvedModel) else resolved
        )
        # Pinned-server context for the FIRST intake call: the SAME
        # canonical producer as executor assembly. An unavailable
        # profile/prompt is represented EXPLICITLY in the intake
        # snapshot prompt ("canonical profile unavailable"); the pinned
        # server_id stays the scope either way (never inferred away).
        try:
            server_materials = load_server_context(
                self._data_root, uuid.UUID(intent.server_id)
            )
        except ContextError:
            server_materials = None

        try:
            outcome = await arun_intake_turn(
                planner_model,
                self._sessions,
                server_id=uuid.UUID(intent.server_id),
                conversation_id=conversation_id,
                source_message_id=source_message_id,
                server_materials=server_materials,
            )
        except IntakeRejection as exc:
            await self._emit_once(
                intent.run_id,
                AGENT_INTAKE_EVENT_TYPE,
                {
                    "interpreted": False,
                    "rejected": True,
                    "fields": dict(exc.fields),
                    "model_calls": exc.model_calls,
                    "attempts": exc.attempts,
                    "repaired": exc.repaired,
                },
            )
            raise AgentStageError(
                INTAKE_REJECTED_REASON, error_type="IntakeRejection"
            ) from None
        except TaskContractError as exc:
            # Defensive belt: the seam converts store rejections into the
            # bounded repair loop itself; a rejection escaping here is
            # still fail-closed with value-free fields.
            await self._emit_once(
                intent.run_id,
                AGENT_INTAKE_EVENT_TYPE,
                {
                    "interpreted": False,
                    "rejected": True,
                    "fields": dict(exc.fields),
                    "model_calls": 0,
                    "attempts": 0,
                    "repaired": False,
                },
            )
            raise AgentStageError(
                INTAKE_REJECTED_REASON, error_type="TaskContractError"
            ) from None

        turn = outcome.turn
        record = outcome.record
        turn_kind = str(turn.turn_kind)
        requested_phase = (
            str(turn.requested_phase.value)
            if getattr(turn, "requested_phase", None) is not None
            else None
        )
        task_header: dict[str, object] | None = None
        if outcome.applied and outcome.task_id is not None:
            # Bind the validated episode to THIS run's link row (§1: the
            # run contributes to the task; the idempotent update makes a
            # replayed bind observe the same row).
            await self._abind_run_task(intent, conversation_id, outcome.task_id)
            task_header = await self._atask_header(
                outcome.task_id, server_id=uuid.UUID(intent.server_id)
            )
        await self._emit_once(
            intent.run_id,
            AGENT_INTAKE_EVENT_TYPE,
            {
                "interpreted": True,
                "rejected": False,
                "turn_kind": turn_kind,
                "applied": outcome.applied,
                "task_id": str(outcome.task_id) if outcome.task_id else None,
                "revision": outcome.revision,
                "disposition": outcome.disposition,
                "requested_phase": requested_phase,
                "planner_model_calls": record.model_calls,
                "planner_attempts": record.attempts,
                "planner_repaired": record.repaired,
                "planner_function_tools": record.function_tools,
            },
        )
        output_text = (
            _intake_output_text(turn)
            if turn_kind in INTAKE_NO_EXECUTOR_KINDS
            else None
        )
        return {
            "interpreted": True,
            "conversation_id": str(conversation_id),
            "source_message_id": str(source_message_id),
            "turn_kind": turn_kind,
            "applied": outcome.applied,
            "task_id": str(outcome.task_id) if outcome.task_id else None,
            "revision": outcome.revision,
            "disposition": outcome.disposition,
            "requested_phase": requested_phase,
            "planner_usage": {
                "model_calls": record.model_calls,
                "attempts": record.attempts,
                "repaired": record.repaired,
                "function_tools": record.function_tools,
            },
            "task_header": task_header,
            "output_text": output_text,
        }

    async def _abind_run_task(
        self,
        intent: AgentRunIntent,
        conversation_id: uuid.UUID,
        task_id: uuid.UUID,
    ) -> None:
        """Attach the intake-validated task to THIS run's link (idempotent).

        The link row exists (attached pre-enqueue) with a NULL task id;
        the intake result names the episode this run contributes to.
        ONE transaction under the conversation row lock with the scope
        re-check (C8 belt); a replayed bind updates the same row to the
        same value, never a second link.
        """
        async with self._sessions() as session, session.begin():
            locked = (
                await session.execute(
                    text(
                        "SELECT server_id FROM conversations "
                        "WHERE conversation_id = :cid FOR UPDATE"
                    ),
                    {"cid": conversation_id},
                )
            ).scalar()
            if locked is None:
                raise AgentStageError(
                    INTAKE_REJECTED_REASON, error_type="ConversationMissing"
                )
            if str(locked) != str(intent.context.server_id):
                # A scope-mismatched link is unreachable through the
                # scope-checked probe above; fail closed regardless.
                raise AgentStageError(
                    INTAKE_REJECTED_REASON, error_type="ConversationScopeMismatch"
                )
            await session.execute(
                text(
                    "UPDATE conversation_runs SET task_id = :task "
                    "WHERE conversation_id = :cid AND run_id = :rid"
                ),
                {
                    "task": task_id,
                    "cid": conversation_id,
                    "rid": uuid.UUID(intent.run_id),
                },
            )

    async def _atask_header(
        self, task_id: uuid.UUID, *, server_id: uuid.UUID
    ) -> dict[str, object] | None:
        """The bounded task-state header bound to execution (AC 1/2).

        Same page bounds as the intake snapshot (store defaults, omission
        stated). ``None`` = the task vanished between apply and read
        (unreachable under the writer lease; honestly unbound rather
        than fabricated).
        """
        try:
            task = await task_store.aget_task(
                self._sessions, task_id, server_id=server_id
            )
        except TaskContractError:
            return None
        steps_page = await task_store.alist_steps(
            self._sessions, task_id, server_id=server_id
        )
        constraints_page = await task_store.alist_constraints(
            self._sessions, task_id, server_id=server_id
        )
        return {
            "task_id": str(task.task_id),
            "revision": task.revision,
            "requested_phase": task.requested_phase.value,
            "title": task.title,
            "summary": task.summary,
            "steps": [
                {
                    "step_id": row.step_id,
                    "status": row.status.value,
                    "verification": row.verification.value,
                    "title": row.title,
                }
                for row in steps_page.items
            ],
            "steps_omitted": steps_page.has_more,
            "constraints": [
                {
                    "constraint_id": row.constraint_id,
                    "origin": row.origin.value,
                    "statement": row.statement,
                }
                for row in constraints_page.items
            ],
            "constraints_omitted": constraints_page.has_more,
        }

    async def arefresh_task_header(
        self, intent: AgentRunIntent
    ) -> dict[str, object] | None:
        """The CURRENT header of THIS run's bound task (refresh seam).

        Read after a successful in-run ``task.*`` mutation so the next
        model round's leading system part can carry the current state
        (the seam 13-intake-runner left open). The run link is the
        single source of the bound task (§1 — the same row
        :meth:`abound_task_phase` reads). ``None`` = no bound task
        (nothing to refresh; the legacy unbound shape) or the task
        vanished between mutation and read (unreachable under the
        writer lease; honestly unbound rather than fabricated).
        Read-only: no effect beyond the caller's own task mutation.
        """
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT r.task_id FROM conversation_runs r "
                        "WHERE r.run_id = :rid AND r.task_id IS NOT NULL "
                        "ORDER BY r.created_at DESC, r.run_id LIMIT 1"
                    ),
                    {"rid": uuid.UUID(intent.run_id)},
                )
            ).first()
        if row is None or row[0] is None:
            return None
        return await self._atask_header(
            uuid.UUID(str(row[0])), server_id=uuid.UUID(intent.server_id)
        )

    async def abound_task_phase(self, intent: AgentRunIntent) -> Phase | None:
        """The phase of the task THIS run is bound to, when any.

        Read at the effect boundary (not intake-time): a phase correction
        recorded between rounds applies at the NEXT tool boundary. No
        link, a link without a task, or a task without a phase →
        ``None`` (the legacy unbound behavior — the gate is inert).
        """
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT t.requested_phase FROM conversation_runs r "
                        "JOIN agent_tasks t ON t.task_id = r.task_id "
                        "WHERE r.run_id = :rid "
                        "ORDER BY r.created_at DESC, r.run_id LIMIT 1"
                    ),
                    {"rid": uuid.UUID(intent.run_id)},
                )
            ).first()
        if row is None or row[0] is None:
            return None
        try:
            return Phase(str(row[0]))
        except ValueError:
            return None  # unknown value: fail closed at the caller

    # -- durable conversation steering queue (07-steering) ------------------

    async def apoll_steering(self, intent: AgentRunIntent) -> dict[str, object]:
        """Read the conversation's PENDING steering rows (checkpointed read).

        One bounded deterministic page (``ORDER BY created_at,
        steering_id LIMIT`` :data:`STEERING_POLL_PAGE_LIMIT`) of
        unconsumed rows for the linked conversation; scope fails closed
        (C8) and legacy runs without a conversation link poll nothing.
        READ-ONLY by contract: the checkpointed poll output is the
        durable claim a recovered workflow re-derives its decisions
        from — the idempotent applied-marking is the SEPARATE ack step,
        so a crash can never lose an already-polled control (the
        checkpoint replays the same rows).
        """
        conversation_id = await conversation_context.aconversation_for_run(
            self._sessions, run_id=intent.run_id
        )
        if conversation_id is None:
            return {"conversation_linked": False, "pending": []}
        try:
            await conversation_store.aget_conversation(
                self._sessions, conversation_id, server_id=intent.context.server_id
            )
        except ConversationContractError:
            return {"conversation_linked": False, "pending": []}
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT steering_id, action, content
                        FROM conversation_steering
                        WHERE conversation_id = :cid AND delivery_status = 'pending'
                        ORDER BY created_at, steering_id
                        LIMIT :l
                        """
                    ),
                    {"cid": conversation_id, "l": STEERING_POLL_PAGE_LIMIT},
                )
            ).mappings().all()
        pending = [
            {
                "steering_id": str(row["steering_id"]),
                "action": str(row["action"]),
                "content": row["content"],
            }
            for row in rows
        ]
        return {"conversation_linked": True, "pending": pending}

    async def aack_steering(
        self, intent: AgentRunIntent, claimed: list[dict[str, object]]
    ) -> dict[str, object]:
        """Mark the processed prefix of pending controls applied (ack).

        Idempotent (``aapply_steering`` replays onto the same durable
        row): a crash before the ack step's own checkpoint replays onto
        already-applied rows, never a second consumption. Rows are
        acknowledged in the claimed order, so a stop control ends the
        prefix — controls behind it stay pending for the next runner.
        """
        if not claimed:
            return {"acked": []}
        conversation_id = await conversation_context.aconversation_for_run(
            self._sessions, run_id=intent.run_id
        )
        if conversation_id is None:
            return {"acked": []}
        acked: list[str] = []
        for item in claimed:
            row = await conversation_store.aapply_steering(
                self._sessions,
                conversation_id=conversation_id,
                server_id=intent.context.server_id,
                steering_id=uuid.UUID(str(item["steering_id"])),
            )
            acked.append(str(row.steering_id))
        return {"acked": acked}

    async def arelease_conversation_lease(
        self, intent: AgentRunIntent
    ) -> dict[str, object]:
        """Release the conversation writer lease at a terminal state (§1).

        The ``conversation_runs`` link attached pre-enqueue (05) IS the
        lease; a terminal run must never keep follow-up turns stuck
        behind a stale ``active_run_id``. Idempotent
        (``arelease_run``): the boolean reports the POST-CALL state
        (``lease_free``), so a replayed release observes the same free
        lease. Legacy runs without a link release nothing. Best-effort
        on the failure/cancel paths: the submit-side terminal-lease
        recheck (chat seam) stays the durable backstop, so a
        pathological release failure never rewrites an honest run
        outcome.
        """
        conversation_id = await conversation_context.aconversation_for_run(
            self._sessions, run_id=intent.run_id
        )
        if conversation_id is None:
            return {"conversation_linked": False, "lease_free": False}
        try:
            await conversation_store.arelease_run(
                self._sessions,
                conversation_id=conversation_id,
                server_id=intent.context.server_id,
                run_id=uuid.UUID(intent.run_id),
            )
        except (ConversationContractError, ValueError):
            return {"conversation_linked": True, "lease_free": False}
        return {"conversation_linked": True, "lease_free": True}

    # -- one conversation round (checkpointed) ------------------------------

    async def amodel_round(
        self,
        intent: AgentRunIntent,
        pins: Mapping[str, object],
        history_json: str,
        deferred_json: str | None,
        round_index: int,
        steered_json: str | None = None,
        progress_hint_json: str | None = None,
    ) -> dict[str, object]:
        """One durable model round (see module docstring).

        Returns the checkpointed round record: either the assistant's
        final text or the deferred tool-request list with each call's
        deterministic step sequence. Classified failures raise
        :class:`AgentStageError` (bind/credential/provider classes).
        ``progress_hint_json`` (22-progress) is the checkpointed bounded
        no-progress derivation's document (or ``None``/``"null"``); it
        renders as a labeled advisory round prefix after any user
        steering and changes no tool effect anywhere.
        """
        binding = _binding_from_pins(pins)
        selection = binding.selection()
        if self._plugin_sync is not None:
            try:
                await self._plugin_sync()
            except Exception:
                pass
        active_resolver: Any = (
            self._resolver()
            if callable(self._resolver)
            else self._resolver
        )
        try:
            if self._model_builder is not None:
                resolved = self._model_builder(
                    selection=selection, resolver=active_resolver
                )
            else:
                resolved = build_model(selection, resolver=active_resolver)
        except (ModelConfigError, EnvFileError) as error:
            code = getattr(error, "code", None)
            code_value = getattr(code, "value", str(code))
            raise AgentStageError(
                f"model_credential_{code_value or 'failed'}",
                error_type=type(error).__name__,
            ) from None
        # The production builder returns the reviewed ResolvedModel
        # wrapper; the composed fake returns a model instance directly.
        resolved_model = (
            resolved.model if isinstance(resolved, ResolvedModel) else resolved
        )
        try:
            bridge = self._bridge_of(intent)
        except BridgeError as error:
            raise AgentStageError(
                f"tool_surface_{error.code.value}",
                error_type=type(error).__name__,
            ) from None
        # W3 (§6 guidance): a task-bound ``inspect`` round is not OFFERED
        # target effects the phase gate necessarily refuses (e.g.
        # ``host.shell``); permitted reads and task metadata/plan tools
        # stay offered. Guidance only: unbound, plan and act rounds keep
        # the full enabled surface, and the execution boundary re-derives
        # the phase and stays fail-closed at every tool call.
        phase = await self.abound_task_phase(intent)
        model_tools = (
            bridge.tools_for_phase(phase, _phase_action_for_capability)
            if phase is Phase.INSPECT
            else bridge.tools
        )

        agent: Agent[None, str | DeferredToolRequests] = Agent(
            model=resolved_model,
            tools=[_deferring_tool(tool) for tool in model_tools],
            output_type=[str, DeferredToolRequests],
            capabilities=[
                _compaction_capability(binding.profile.context_limit)
            ],
        )
        history = ModelMessagesTypeAdapter.validate_json(history_json.encode("utf-8"))
        deferred = (
            _parse_deferred_results(json.loads(deferred_json))
            if deferred_json is not None
            else None
        )
        steered_prompt: str | None = None
        if steered_json is not None:
            raw_steered = json.loads(steered_json)
            if isinstance(raw_steered, list):
                cleaned = [
                    str(item).strip()
                    for item in raw_steered
                    if isinstance(item, str) and str(item).strip()
                ]
                if cleaned:
                    steered_prompt = "\n\n".join(
                        f"[User steering message]: {msg}" for msg in cleaned
                    )
        prompt_text = compose_round_prompt(
            steered_prompt, _progress_note_text(progress_hint_json)
        )
        try:
            result = await agent.run(
                prompt_text,
                message_history=history,
                deferred_tool_results=deferred,
            )
        except Exception as error:
            kind: ProviderFailureKind = classify_provider_error(error)
            raise AgentStageError(
                f"model_{kind.value}",
                error_type=type(error).__name__,
            ) from None

        messages_json = ModelMessagesTypeAdapter.dump_json(
            result.all_messages()
        ).decode("utf-8")
        if len(messages_json.encode("utf-8")) > MAX_HISTORY_BYTES:
            raise AgentStageError(HISTORY_BUDGET_REASON, error_type="HistoryBudget")
        output = result.output
        thinking_parts: list[str] = []
        try:
            for msg in result.new_messages():
                for part in getattr(msg, "parts", ()):
                    if (
                        getattr(part, "part_kind", None) == "thinking"
                        or type(part).__name__ == "ThinkingPart"
                    ):
                        content = getattr(part, "content", None)
                        if isinstance(content, str) and content.strip():
                            thinking_parts.append(content.strip())
        except Exception:
            pass
        thinking_text = "\n\n".join(thinking_parts)

        if isinstance(output, DeferredToolRequests):
            if output.approvals:
                # The bridge never marks tools ``requires_approval``; a
                # deferred approval means the composed surface changed —
                # fail honestly instead of guessing a decision.
                raise AgentStageError(
                    AgentRunErrorCode.UNEXPECTED_DEFERRAL.value,
                    error_type="UnexpectedApprovalDeferral",
                )
            calls = self._deferred_calls(bridge, output, round_index)
            round_payload: dict[str, object] = {
                "status": "tool_calls",
                "tool_calls": [
                    {
                        "seq": call["seq"],
                        "capability_id": call["capability_id"],
                        "payload_hash": call["payload_hash"],
                    }
                    for call in calls
                ],
            }
            if thinking_text:
                round_payload["thinking"] = thinking_text
            await self._emit_round_event(
                intent.run_id,
                round_index,
                round_payload,
            )
            return {
                "status": "tool_calls",
                "history_json": messages_json,
                "calls": calls,
            }
        raw_output = str(output)
        if "<think>" in raw_output and "</think>" in raw_output:
            import re

            for match in re.finditer(r"<think>(.*?)</think>", raw_output, flags=re.DOTALL):
                extracted = match.group(1).strip()
                if extracted:
                    thinking_text = (
                        f"{thinking_text}\n\n{extracted}".strip()
                        if thinking_text
                        else extracted
                    )
            raw_output = re.sub(r"<think>.*?</think>", "", raw_output, flags=re.DOTALL).strip()

        final_payload: dict[str, object] = {
            "status": "final",
            "output_chars": len(raw_output),
            "output": raw_output,
        }
        if thinking_text:
            final_payload["thinking"] = thinking_text
        await self._emit_round_event(
            intent.run_id,
            round_index,
            final_payload,
        )
        return {
            "status": "final",
            "history_json": messages_json,
            "output": raw_output,
            "output_chars": len(raw_output),
        }

    def _deferred_calls(
        self,
        bridge: CapabilityToolBridge,
        requests: DeferredToolRequests,
        round_index: int,
    ) -> list[dict[str, object]]:
        """Validate the round's deferred calls; derive their step seqs.

        The tool name must map to an enabled capability (exposure follows
        the bridge, C06 rule 4); arguments must be a canonicalizable JSON
        object. Invalid calls KEEP their deterministic seq and become
        honestly denied rows at execution time — nothing is silently
        dropped from the model's request.
        """
        if len(requests.calls) > AGENT_TOOL_MAX_PER_ROUND:
            raise AgentStageError(TOOL_BUDGET_REASON, error_type="ToolCallBudget")
        names = {tool_name_for(cid): cid for cid in bridge.enabled_capabilities}
        calls: list[dict[str, object]] = []
        for index, part in enumerate(requests.calls):
            payload: dict[str, object] | None = None
            payload_hash: str | None = None
            raw = part.args
            if isinstance(raw, Mapping):
                candidate: dict[str, object] | None = dict(raw)
            elif isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = None
                candidate = dict(parsed) if isinstance(parsed, Mapping) else None
            else:
                candidate = None
            if candidate is not None:
                try:
                    payload_hash = canonical_payload_hash(candidate)
                    payload = candidate
                except Exception:
                    payload_hash = None
            calls.append(
                {
                    "tool_call_id": part.tool_call_id,
                    "tool_name": part.tool_name,
                    "capability_id": names.get(part.tool_name),
                    "payload": payload,
                    "payload_hash": payload_hash,
                    "seq": tool_step_seq(round_index, index),
                }
            )
        if not calls:
            raise AgentStageError("empty_deferral", error_type="EmptyDeferral")
        return calls

    # -- one exactly-once tool attempt (the bridge seam) --------------------

    async def aexecute_tool(
        self, intent: AgentRunIntent, call: Mapping[str, object]
    ) -> dict[str, object]:
        """One exactly-once attempt of one model-requested call.

        Resume-first: a persisted terminal/unknown row IS the answer
        (replays rebuild the model-facing result from the value-free
        receipt — never a re-dispatch). The fence marker commits before
        :meth:`CapabilityToolBridge.dispatch_once`, the one seam into the
        common dispatcher (C06 rule 4); the outcome + safe receipt commit
        after, through the reviewed guarded-transition writer.
        """
        seq = int(call["seq"])
        capability_id = call.get("capability_id")
        row_capability = (
            str(capability_id)
            if isinstance(capability_id, str) and capability_id
            else str(call.get("tool_name") or "unknown")
        )
        spec = _tool_step_intent(seq, row_capability, call)
        row = await self._steps.aensure_step_intent(intent.run_id, spec)
        state = str(row["state"])
        if state == StepState.RUNNING.value:
            return await self._crash_window_outcome(intent, call, spec)
        if state != StepState.PENDING.value:
            receipt = row["receipt"] if isinstance(row["receipt"], Mapping) else {}
            resolved = isinstance(receipt, Mapping) and bool(receipt.get("resolution"))
            return {
                "seq": seq,
                "state": state,
                "reason": receipt.get("reason"),
                "op_id": receipt.get("op_id"),
                "result_text": _receipt_result_document(
                    intent, state, receipt, resolved=bool(resolved)
                ),
            }

        payload_hash = call.get("payload_hash")
        payload = call.get("payload")
        if capability_id is None or not isinstance(payload, Mapping) or not isinstance(
            payload_hash, str
        ):
            # The model requested something that is not an enabled,
            # canonicalizable operation: nothing was read, consumed or run.
            reason = (
                "capability_not_enabled" if capability_id is None else "invalid_request"
            )
            return await self._persist_denial(
                intent,
                call,
                row_capability,
                pending=True,
                reason=reason,
                detail=None,
                payload_hash=payload_hash if isinstance(payload_hash, str) else None,
            )

        # Phase gate (§6; task-phase-gate resource): a task-bound run
        # executes target effects only in ``act`` and reads only in
        # ``inspect``/``act`` — the ratified ``validate_phase`` mapping.
        # Task-state tools are run control, not target effects:
        # ``task.propose_update`` maps to PLAN_PROPOSAL and ``task.read``
        # / ``task.update_item`` to METADATA_UPDATE (allowed in every
        # phase); target read tools map to COLLECT_EVIDENCE and target
        # non-read tools to EXECUTE_STEPS. The denial lands BEFORE the
        # fence marker: no effect was even planned, and the honest failed
        # row tells the model why.
        phase = await self.abound_task_phase(intent)
        if phase is not None:
            descriptor = self._bridge_of(intent).descriptor_for(str(capability_id))
            effect_class = str(getattr(descriptor, "effect_class", "") or "")
            action = _phase_action_for_capability(str(capability_id), effect_class)
            try:
                validate_phase(phase, action)
            except TaskContractError:
                return await self._persist_denial(
                    intent,
                    call,
                    row_capability,
                    pending=True,
                    reason=PHASE_NOT_ALLOWED_REASON,
                    detail=f"{action.value}_in_{phase.value}",
                    payload_hash=payload_hash,
                )

        await self._steps.amark_running(intent.run_id, seq, row_capability)
        try:
            outcome = await self._bridge_of(intent).dispatch_once(
                str(capability_id),
                dict(payload),
            )
        except BridgeError as error:
            # Schema mismatch / disabled capability at the bridge gate:
            # rejected before any effect was planned. The attempt had the
            # fence marker, so the honest answer lands ``running → failed``.
            return await self._persist_denial(
                intent,
                call,
                row_capability,
                pending=False,
                reason=f"bridge_{error.code.value}",
                detail=error.detail,
                payload_hash=payload_hash,
            )
        except Exception as error:  # infrastructure before the effect
            return await self._persist_denial(
                intent,
                call,
                row_capability,
                pending=False,
                reason="dispatch_infra_failed",
                detail=type(error).__name__,
                payload_hash=payload_hash,
            )

        # The reviewed translation (single authority for outcome → step
        # state + safe receipt; deliberately imported from the steps
        # module instead of re-implemented here).
        state_value, reason, receipt = _translate_outcome(outcome)
        if state_value == WAIT_FOR_APPROVAL:
            # The agent path answers the model inline: the denial IS the
            # durable binding the resume surface consumes later. The row
            # records the honest terminal answer (nothing ran).
            state_value = StepState.FAILED.value
            reason = outcome.reason
            if (
                receipt.get("approval_id") is None
                and isinstance(receipt.get("payload_hash"), str)
                and isinstance(receipt.get("policy_revision"), int)
            ):
                try:
                    approvals_store = getattr(
                        self._steps._dispatcher, "_approvals", None
                    )
                    if approvals_store is not None:
                        from mai.policy.approvals import ApprovalRequest

                        payload_hash_str = str(receipt["payload_hash"])
                        policy_rev_int = int(str(receipt["policy_revision"]))
                        created_approval = await approvals_store.create(
                            ApprovalRequest(
                                capability_id=outcome.capability_id,
                                server_id=uuid.UUID(intent.server_id),
                                payload_hash=payload_hash_str,
                                policy_revision=policy_rev_int,
                                created_by=f"agent.run:{intent.run_id}",
                                expires_at=_utcnow() + timedelta(hours=1),
                            )
                        )
                        receipt["approval_id"] = str(created_approval.approval_id)
                except Exception:
                    pass
        # The shared output-safety projection (C2 / S1): redact resolved
        # values AND secret-shaped tokens BEFORE any size bound, traverse
        # nested / JSON-encoded payloads, bound the receipt copy last.
        resolved_values = _resolved_credential_values(
            self._steps, outcome.credential_references
        )
        if outcome.output is not None and outcome.output.text:
            receipt["response"] = project_document(
                outcome.output.text, resolved_values
            )
        if isinstance(payload, Mapping):
            receipt["params"] = _scrubbed_step_params(
                str(capability_id),
                dict(payload),
                resolved_values,
            )
        step_state = StepState(state_value)
        await self._steps.apersist_outcome(
            intent.run_id,
            seq,
            row_capability,
            step_state,
            receipt,
            reason=reason,
            op_id=str(outcome.op_id),
        )
        result_text = _result_document(
            intent=intent,
            capability_id=outcome.capability_id,
            status=_status_of_state(step_state),
            reason=str(reason or outcome.reason),
            payload_hash=outcome.payload_hash,
            effect=outcome.effect,
            policy_revision=outcome.policy_revision,
            output_text=(
                None
                if outcome.output is None
                else project_text(outcome.output.text, resolved_values)
            ),
            output_truncated=(
                None if outcome.output is None else outcome.output.truncated
            ),
            output_omitted_bytes=(
                None if outcome.output is None else outcome.output.omitted_bytes
            ),
        )
        return {
            "seq": seq,
            "state": step_state.value,
            "reason": reason,
            "op_id": str(outcome.op_id),
            "result_text": result_text,
        }

    async def _crash_window_outcome(
        self,
        intent: AgentRunIntent,
        call: Mapping[str, object],
        spec: StepIntent,
    ) -> dict[str, object]:
        """Translate a recovered ``running``-without-receipt attempt.

        Delegated to the reviewed crash-window writer: ``outcome_unknown``
        + a value-free receipt, NEVER a re-dispatch (guard rule 2). The
        model receives the honest unknown-effect result; the run parks.
        """
        outcome = await self._steps.acrash_window_outcome(
            intent.run_id, intent.server_id, int(call["seq"]), spec
        )
        receipt = (
            outcome["receipt"] if isinstance(outcome.get("receipt"), Mapping) else {}
        )
        return {
            "seq": int(call["seq"]),
            "state": StepState.OUTCOME_UNKNOWN.value,
            "reason": CRASH_RECOVERY_RESULT_REASON,
            "op_id": receipt.get("op_id"),
            "result_text": _receipt_result_document(
                intent,
                StepState.OUTCOME_UNKNOWN.value,
                receipt,
                resolved=False,
            ),
        }

    async def _persist_denial(
        self,
        intent: AgentRunIntent,
        call: Mapping[str, object],
        row_capability: str,
        *,
        pending: bool,
        reason: str,
        detail: str | None,
        payload_hash: str | None,
    ) -> dict[str, object]:
        """Persist a proven no-effect denial (``pending``/``running → failed``).

        ``pending=True`` is a rejection before the fence marker (the
        request was not even a well-formed enabled operation);
        ``pending=False`` is a rejection after the marker (the attempt
        started, the bridge/dispatcher proved nothing ran). Both land
        ``failed`` with the stable reason on the value-free receipt.
        """
        seq = int(call["seq"])
        receipt: dict[str, object] = {
            "v": 1,
            "op_id": None,
            "capability_id": row_capability,
            "allowed": False,
            "reason": reason,
            "effect": EFFECT_NOT_ATTEMPTED,
            "payload_hash": payload_hash or "non_canonical",
            "tool_call_id": str(call.get("tool_call_id")),
        }
        if detail is not None:
            receipt["error_type"] = detail
        if pending:
            await self._steps.apersist_pending_denial(
                intent.run_id,
                seq,
                row_capability,
                receipt,
                reason=reason,
            )
        else:
            await self._steps.apersist_outcome(
                intent.run_id,
                seq,
                row_capability,
                StepState.FAILED,
                receipt,
                reason=reason,
                op_id=None,
            )
        return {
            "seq": seq,
            "state": StepState.FAILED.value,
            "reason": reason,
            "op_id": None,
            "result_text": _result_document(
                intent=intent,
                capability_id=row_capability,
                status=ToolStatus.DENIED,
                reason=reason,
                payload_hash=payload_hash,
                effect=EFFECT_NOT_ATTEMPTED,
            ),
        }

    def _bridge_of(self, intent: AgentRunIntent) -> CapabilityToolBridge:
        """The one bridge surface for this run's context (C06 rule 4)."""
        return CapabilityToolBridge(
            registry=self._registry,
            dispatcher=self._steps._dispatcher,  # noqa: SLF001 - composed seam
            context=intent.context,
        )

    # -- reconciliation park (mirror of the steps module's seam) -----------

    async def await_reconciliation(
        self,
        intent: AgentRunIntent,
        call: Mapping[str, object],
        outcome: Mapping[str, object],
    ) -> str:
        """Enter ``reconciling`` ONCE; return the persisted deadline."""
        return await self._steps.aobserve_enter_wait(
            intent.run_id,
            target=RunState.RECONCILING,
            event_type=AGENT_RECONCILING_EVENT_TYPE,
            event_data={
                "capability_id": call.get("capability_id") or call.get("tool_name"),
                "op_id": outcome.get("op_id"),
                "reason": outcome.get("reason"),
            },
            wait_seconds=intent.reconcile_wait_seconds,
            step_seq=int(call["seq"]),
        )

    def read_step(self, intent: AgentRunIntent, seq: int) -> dict[str, object] | None:
        return self.run(self._steps.aread_step(intent.run_id, seq))

    async def await_wait_resumed(self, intent: AgentRunIntent, from_state: str) -> str:
        return await self._steps.aobserve_wait_resumed(intent.run_id, from_state)

    # -- endings -------------------------------------------------------------

    async def afinalize_task_outcome(
        self, intent: AgentRunIntent
    ) -> dict[str, object] | None:
        """The additive domain-outcome observation at the final seam (§9).

        Read-only derivation over the task bound to THIS run's link:
        the observation document reports every criterion explicitly and
        records NOTHING — a finished attempt never closes, verifies or
        advances an episode by itself. ``verified`` can only ever mirror
        a previously validated evidence-backed verdict (act
        ``20-completion``); the technical ``RunState`` machine is
        untouched (this runs after the workflow loop, before the
        terminal translation, additively). ``None`` = unbound or
        out-of-scope run: taskless runs keep every legacy surface
        byte-identical.
        """
        return await task_completion.aobserve_task_outcome(
            self._sessions,
            server_id=uuid.UUID(intent.server_id),
            run_id=intent.run_id,
        )

    async def afinish_run(
        self, intent: AgentRunIntent, summary: Mapping[str, object]
    ) -> str:
        """Record ``running → succeeded``; emit the safe summary event.

        Terminal state ⇒ the conversation writer lease is released
        (§1): a completed run must never keep follow-up turns stuck
        behind a stale ``active_run_id``.
        """
        current = await self._steps.lifecycle.current_state(intent.run_id)
        if current is RunState.RUNNING:
            await self._steps.lifecycle.record_transition(
                intent.run_id, RunState.SUCCEEDED
            )
            current = RunState.SUCCEEDED
        assert current is not None
        completed_data: dict[str, object] = {
            "rounds": summary.get("rounds"),
            "tool_calls": summary.get("tool_calls"),
            "output_chars": summary.get("output_chars"),
        }
        # Additive (21b): project the value-free task-outcome document into
        # the emitted event so the real path delivers it (R-21 wire gap).
        # Absent for taskless runs (the document is None) -> the key is
        # omitted and every legacy surface stays byte-identical; a
        # non-mapping or empty value is never emitted as-is.
        outcome_document = summary.get(task_completion.TASK_OUTCOME_SUMMARY_KEY)
        if isinstance(outcome_document, Mapping) and outcome_document:
            completed_data[task_completion.TASK_OUTCOME_SUMMARY_KEY] = dict(
                outcome_document
            )
        stopped_by = summary.get(STOPPED_BY_EVENT_KEY)
        if stopped_by is not None:
            # Deterministic boundary stop (pause/cancel steering): the
            # event carries the honest disposition; runs that end
            # normally keep the existing event shape byte-identical.
            completed_data[STOPPED_BY_EVENT_KEY] = stopped_by
        task_open_reason = _task_open_reason(summary)
        if task_open_reason is not None:
            # W2: an open episode qualifies the terminal success — the
            # event carries the stable reason next to the honest
            # task_outcome document (never a fabricated completion).
            completed_data[TASK_OPEN_EVENT_KEY] = task_open_reason
        await self._emit_once(intent.run_id, AGENT_COMPLETED_EVENT_TYPE, completed_data)
        await self.arelease_conversation_lease(intent)
        return current.value

    # -- idempotent-by-observation event emission ---------------------------

    async def aemit_failure_event(self, run_id: str, reason: str) -> None:
        """The classified failure reason on the stream (observable AC2)."""
        if await self._event_seen(run_id, AGENT_FAILED_EVENT_TYPE, ("reason", reason)):
            return
        await self._steps.aemit_run_event(
            run_id, AGENT_FAILED_EVENT_TYPE, {"reason": reason}
        )

    async def _emit_once(
        self, run_id: str, event_type: str, data: Mapping[str, object]
    ) -> None:
        """Append an event once per run (idempotent under recovery).

        A crash between the event commit and the engine checkpoint would
        replay the step; the read-before-insert check keeps the stream
        free of duplicates. Only one attempt of a step executes at a
        time (the engine's single-active-attempt invariant), so the
        check is race-free in the only window that matters.
        """
        if await self._event_seen(run_id, event_type, None):
            return
        await self._steps.aemit_run_event(run_id, event_type, dict(data))

    async def _emit_round_event(
        self, run_id: str, round_index: int, data: Mapping[str, object]
    ) -> None:
        if await self._event_seen(
            run_id, AGENT_ROUND_EVENT_TYPE, ("round", round_index)
        ):
            return
        await self._steps.aemit_run_event(
            run_id,
            AGENT_ROUND_EVENT_TYPE,
            {"round": round_index, **dict(data)},
        )

    async def _event_seen(
        self,
        run_id: str,
        event_type: str,
        match: tuple[str, object] | None,
    ) -> bool:
        events = await self._steps.lifecycle.event_stream(run_id)
        for event in events:
            if event.type != event_type:
                continue
            if match is None or event.data.get(match[0]) == match[1]:
                return True
        return False


# ---------------------------------------------------------------------------
# process-level composition (worker path / test injection)
# ---------------------------------------------------------------------------

_EXECUTOR: AgentRunExecutor | None = None


def configure_agent_run(executor: AgentRunExecutor) -> None:
    """Inject the composed executor (worker-compose/test path)."""
    global _EXECUTOR
    if not isinstance(executor, AgentRunExecutor):
        raise AgentRunError(
            AgentRunErrorCode.INVALID_WIRING, "executor must be an AgentRunExecutor"
        )
    if _EXECUTOR is not None:
        raise AgentRunError(
            AgentRunErrorCode.ALREADY_CONFIGURED,
            "agent-run execution is already configured in this process",
        )
    _EXECUTOR = executor


def _resolve_agent_run() -> AgentRunExecutor:
    if _EXECUTOR is None:
        raise AgentRunError(
            AgentRunErrorCode.EXECUTOR_NOT_CONFIGURED,
            "no AgentRunExecutor composed in this process; call "
            "configure_agent_run() before agent runs execute",
        )
    return _EXECUTOR


def reset_agent_run() -> None:
    """Forget the injected executor (test isolation only)."""
    global _EXECUTOR
    _EXECUTOR = None


# ---------------------------------------------------------------------------
# the DBOS steps (sync; every async seam runs on the executor's one loop)
# ---------------------------------------------------------------------------


@DBOS.step(name="agents.run_started")
def _agent_run_started(payload_json: str) -> str:
    """Observe-and-record the run's ``queued`` anchor + ``running``."""
    intent = parse_agent_run_intent(payload_json)
    return _resolve_agent_run().observe_run_started(intent)


@DBOS.step(name="agents.model_bound")
def _agent_model_bound(payload_json: str) -> dict[str, object]:
    """Pin the (connection, profile) pair (checkpointed)."""
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(executor.abind_model(intent))


@DBOS.step(name="agents.context_assembled")
def _agent_context_assembled(payload_json: str, pins_json: str) -> dict[str, object]:
    """Assemble the bounded model payload (checkpointed)."""
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(executor.aassemble_context(intent, json.loads(pins_json)))


@DBOS.step(name="agents.conversation_seed")
def _agent_conversation_seed(
    payload_json: str, assembled_json: str
) -> dict[str, object]:
    """Build the conversation ``message_history`` seed (checkpointed).

    The seed (prior native turns + the new user request) and its
    value-free assembly report are ONE durable step result: a recovered
    run replays the recorded seed instead of re-reading a transcript
    that only ever grows (deterministic across restarts, C2/C4).
    """
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(
        executor.abuild_conversation_seed(intent, json.loads(assembled_json))
    )


@DBOS.step(name="agents.intake_interpreted")
def _agent_intake_interpreted(
    payload_json: str, pins_json: str
) -> dict[str, object]:
    """ONE checkpointed tool-free intake interpretation (13-intake-runner).

    The step result IS the C4 replay anchor: a recovered/replayed
    workflow re-reads the SAME accepted interpretation (same turn kind,
    same task binding, same planner usage) without a second model call,
    and the store's stable per-turn key keeps a replay from creating a
    second task even if the checkpoint were lost. An unrepairable
    rejection raises the classified ``intake_rejected`` stage failure
    after emitting the value-free fields (P4: no effects on invalid
    intake).
    """
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(
        executor.ainterpret_intake_turn(intent, json.loads(pins_json))
    )


@DBOS.step(name="agents.assistant_recorded")
def _agent_assistant_recorded(payload_json: str, output: str) -> dict[str, object]:
    """Record the canonical assistant reply (checkpointed producer).

    Idempotent per ``run_id``: the step checkpoint plus the safe
    ``content_ref`` marker make replays/restarts never duplicate the
    transcript row (C4/C7 producer honesty).
    """
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(executor.arecord_assistant_reply(intent, output))


@DBOS.step(name="agents.steering_polled")
def _agent_steering_poll(payload_json: str) -> dict[str, object]:
    """Poll the durable steering queue at a safe boundary (read-only).

    The CHECKPOINTED poll output is the durable claim: a recovered
    workflow re-derives its boundary decisions from this checkpoint,
    never from live queue state (which the idempotent ack mutates).
    """
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(executor.apoll_steering(intent))


@DBOS.step(name="agents.steering_acked")
def _agent_steering_ack(payload_json: str, claimed_json: str) -> dict[str, object]:
    """Mark the processed steering prefix applied (checkpointed ack).

    Idempotent: replay after a crash re-acks already-applied rows
    unchanged, so a duplicate signal or a step replay never re-applies
    an already-consumed control (C4/C5).
    """
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(
        executor.aack_steering(intent, json.loads(claimed_json))
    )


@DBOS.step(name="agents.model_round")
def _agent_model_round(
    payload_json: str,
    pins_json: str,
    history_json: str,
    deferred_json: str | None,
    round_index: int,
    steered_json: str | None = None,
    progress_hint_json: str | None = None,
) -> dict[str, object]:
    """One durable conversation round (checkpointed)."""
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(
        executor.amodel_round(
            intent,
            json.loads(pins_json),
            history_json,
            deferred_json,
            round_index,
            steered_json,
            progress_hint_json,
        )
    )


@DBOS.step(name="agents.tool_execute")
def _agent_tool_execute(payload_json: str, call_json: str) -> dict[str, object]:
    """One exactly-once guarded attempt of one tool call (checkpointed)."""
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(executor.aexecute_tool(intent, json.loads(call_json)))


@DBOS.step(name="agents.task_header_refreshed")
def _agent_task_header_refreshed(
    payload_json: str,
) -> dict[str, object] | None:
    """Read the CURRENT bound task header after an in-run mutation.

    Checkpointed so a recovered run replays the recorded read instead of
    re-reading task state that later rounds may have mutated again
    (deterministic across restarts, C2/C4). Read-only against the task
    store: the refresh adds no effect beyond the caller's own mutation.
    """
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(executor.arefresh_task_header(intent))


@DBOS.step(name="agents.progress_hint_checked")
def _agent_progress_hint(
    payload_json: str,
    history_json: str,
    round_results_json: str,
) -> str:
    """One checkpointed bounded no-progress derivation (22-progress).

    A PURE function of THIS run's own conversation artifacts — the
    checkpointed history (prior rounds' frozen ``ToolResult`` documents)
    plus the current round's result map — so a recovered workflow replays
    the recorded derivation deterministically instead of re-deriving from
    process-local state (C4). Read-only and advisory: the returned
    bounded document (``"null"`` when silent) steers the NEXT round via
    ``amodel_round`` and never changes any tool effect, any receipt, or
    the durable lifecycle. Task-local by construction (no store, no
    cross-task memory); it stores no chain-of-thought (only counts,
    short digests/fingerprints, capability ids — see ``task_progress``).
    """
    parse_agent_run_intent(payload_json)
    document = task_progress.progress_hint_document(history_json, round_results_json)
    return canonical_json(document) if document is not None else "null"


@DBOS.step(name="agents.read_step")
def _agent_read_step(payload_json: str, seq: int) -> dict[str, object] | None:
    """Poll one tool-call row for a reconciliation resolution."""
    intent = parse_agent_run_intent(payload_json)
    return _resolve_agent_run().read_step(intent, seq)


@DBOS.step(name="agents.reconciling")
def _agent_reconciling(payload_json: str, call_json: str, outcome_json: str) -> str:
    """Record ``reconciling`` + the wait event; return the deadline."""
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(
        executor.await_reconciliation(
            intent, json.loads(call_json), json.loads(outcome_json)
        )
    )


@DBOS.step(name="agents.wait_resumed")
def _agent_wait_resumed(payload_json: str, from_state: str) -> str:
    """Record ``<wait> → running`` when a park ends (observe)."""
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(executor.await_wait_resumed(intent, from_state))


@DBOS.step(name="agents.reconcile_expired")
def _agent_reconcile_expired(payload_json: str, call_json: str) -> str:
    """Expire one unknown-effect step at the deadline (checkpointed).

    ONE checkpointed step ⇒ the guarded transition + the timeout event
    commit at most once per attempt (a recovered attempt replays the
    checkpoint instead of re-appending). Persistence failures propagate:
    the run fails loudly instead of claiming a recorded event.
    """
    intent = parse_agent_run_intent(payload_json)
    call = json.loads(call_json)
    executor = _resolve_agent_run()
    return canonical_json(
        executor.run(executor.aexpire_unknown_step(intent.run_id, int(call["seq"])))
    )


@DBOS.step(name="agents.run_finished")
def _agent_run_finished(payload_json: str, summary_json: str) -> str:
    """Record ``running → succeeded`` after the conversation (observe)."""
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(executor.afinish_run(intent, json.loads(summary_json)))


@DBOS.step(name="agents.task_outcome_finalized")
def _agent_task_outcome_finalized(payload_json: str) -> str:
    """The additive evidence-backed task-outcome observation (20-completion).

    Checkpointed read-only derivation over the task bound to this run's
    link (§9): the returned document rides the run summary additively —
    a taskless run yields ``"null"`` and every legacy surface stays
    byte-identical. A completed ATTEMPT is never a verified task: the
    observation records nothing, so without an evidence-backed criterion
    proposal the episode stays open and resumable (E1/P3). The step is
    strict (failures propagate to the engine's classified recovery): an
    observation error must never be silently hidden behind a missing
    key, and the replay re-reads the same deterministic state.
    """
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    document = executor.run(executor.afinalize_task_outcome(intent))
    return canonical_json(document) if document is not None else "null"


@DBOS.step(name="agents.run_failed")
def _agent_run_failed(payload_json: str, reason: str) -> str:
    """Record the run ``failed`` + the classified reason event."""
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    result = executor.fail_run(intent, reason)
    executor.run(executor.aemit_failure_event(intent.run_id, reason))
    return result


def _record_quietly(intent: AgentRunIntent, action: str) -> None:
    """Best-effort plain (non-checkpointed) run translation.

    Used on the cancellation path: DBOS raises its cancellation exception
    AT checkpoint boundaries, so any ``DBOS.step`` call here would raise
    before executing — the translation must be plain calls. Idempotent by
    observation; failures leave the engine's own exception as the truth.
    """
    try:
        executor = _resolve_agent_run()
    except AgentRunError:
        return
    try:
        if action == "cancel":
            executor.cancel_run(intent)
        else:
            executor.fail_run(intent, "handler_error")
    except Exception:
        pass


def _deadline_of(iso: str) -> datetime:
    deadline = datetime.fromisoformat(iso)
    if deadline.tzinfo is None:
        raise AgentRunError(
            AgentRunErrorCode.INVALID_WAIT_STATE,
            "persisted deadline lost its timezone",
        )
    return deadline


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# the one registered run handler
# ---------------------------------------------------------------------------


def _drain_steering_messages() -> list[str]:
    """Drain any mid-run user steering messages queued on ``STEER_TOPIC``.

    Legacy channel only (runs before the canonical conversation seam).
    Deterministic controls (``action: pause|cancel``) are NEVER model
    input: they ride the durable conversation-steering queue and are
    consumed at safe boundaries, so a control payload is filtered out
    of the text drain even if it is signaled here.
    """
    messages: list[str] = []
    while True:
        try:
            raw = DBOS.recv(topic=STEER_TOPIC, timeout_seconds=0)
        except Exception:
            break
        if raw is None:
            break
        if isinstance(raw, Mapping):
            action = raw.get("action")
            if action is not None and action != SteerAction.STEER.value:
                continue
            text = raw.get("message")
            if isinstance(text, str) and text.strip():
                messages.append(text.strip())
        elif isinstance(raw, str) and raw.strip():
            messages.append(raw.strip())
    return messages


def _decide_steering_boundary(
    claimed: list[dict[str, object]],
) -> tuple[list[str], str | None, list[dict[str, object]]]:
    """The deterministic boundary decision over one claimed page (pure).

    Returns ``(steer_texts, stop_action, ack_prefix)``. Natural-language
    steering is collected for the NEXT model round (``UserPromptPart``
    injection via ``steered_json``). A pause/cancel ALWAYS wins its
    boundary and ends the run: natural-language rows claimed at the
    same boundary stay PENDING (outside the ack prefix) so the next
    runner interprets them — nothing is dropped, nothing is applied
    twice (C4/C5). Because the caller derives the decision from the
    checkpointed poll, a recovered run re-derives the identical stop (a
    replay can never resume effects past a consumed stop control).
    """
    texts: list[str] = []
    for item in claimed:
        action = str(item.get("action"))
        if action in (SteerAction.PAUSE.value, SteerAction.CANCEL.value):
            # Deterministic control: ack ONLY the stop row; natural-
            # language rows stay pending for the next runner, so their
            # interpretation is never lost.
            return [], str(action), [dict(item)]
        if action == SteerAction.STEER.value:
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                texts.append(content.strip())
    return texts, None, [dict(item) for item in claimed]


def _claim_steering_boundary(
    payload_json: str,
) -> tuple[list[str], str | None]:
    """One safe-boundary steering consumption (durable, exactly once).

    Checkpointed poll (the durable claim) → :func:`_decide_steering_boundary`
    → checkpointed idempotent ack of the decided prefix.
    """
    polled = _agent_steering_poll(payload_json)
    claimed = [dict(item) for item in polled.get("pending") or []]
    if not claimed:
        return [], None
    texts, stop, ack_prefix = _decide_steering_boundary(claimed)
    _agent_steering_ack(payload_json, canonical_json(ack_prefix))
    return texts, stop


def _steering_stop_result_text(reason: str) -> str:
    """Honest tool result for a deferred call a boundary never dispatched.

    A batch interrupted at a safe boundary leaves the not-yet-executed
    calls with an explicit ``not_executed`` document (wire honesty, C08
    rule 6): no effect occurred, never an invented outcome.
    """
    return canonical_json(
        {
            "status": "not_executed",
            "reason": reason,
            "detail": (
                "the batch was interrupted at a safe steering boundary "
                "before this call was dispatched; no effect occurred"
            ),
        }
    )


def _release_lease_quietly(intent: AgentRunIntent) -> None:
    """Best-effort conversation lease release on failure/cancel paths.

    Mirrors :func:`_record_quietly`: plain (non-checkpointed) idempotent
    calls, so a terminal run still frees the writer lease even when the
    workflow is unwinding through an exception.
    """
    try:
        executor = _resolve_agent_run()
    except AgentRunError:
        return
    try:
        executor.run(executor.arelease_conversation_lease(intent))
    except Exception:
        pass


def run_agent_conversation(payload_json: str) -> dict[str, object]:
    """Execute one durable agent conversation (see module docstring).

    Registered as :data:`HANDLER_NAME` (module import) and executed
    inside the engine's durable trampoline workflow. Classified stage
    failures record ``failed`` and raise :class:`AgentRunFailed` (engine
    ERROR — both sides agree); cancellation re-raises the engine's own
    exception after recording ``cancelled``; integrity rejections from
    the steps machinery propagate as-is (the reviewed writer's own
    contract). Success returns the safe conversation summary document.
    """
    intent = parse_agent_run_intent(payload_json)
    try:
        _agent_run_started(payload_json)
        pins = _agent_model_bound(payload_json)
        pins_json = canonical_json(pins)
        # 13-intake-runner: ONE checkpointed tool-free interpretation of
        # the accepted turn BEFORE any executor work (C4 anchor; planner
        # usage accounted; P4 rejection fails the run with zero effects).
        intake = _agent_intake_interpreted(payload_json, pins_json)
        assembled = _agent_context_assembled(payload_json, pins_json)
        intake_header = (
            intake.get("task_header")
            if isinstance(intake.get("task_header"), Mapping)
            else None
        )
        if intake.get("interpreted") and intake_header is not None:
            # Task/phase binding (AC 1/2): the validated header rides the
            # LEADING system part of the seed, so task id, phase, steps
            # and constraints survive steering and compaction by
            # construction. The context step's own checkpoint is
            # untouched (06 seam undisturbed); the seed step receives
            # the deterministic workflow-computed payload.
            assembled = {
                **assembled,
                "system_prompt": (
                    str(assembled["system_prompt"])
                    + _task_context_block(intake_header)
                ),
            }
        seed = _agent_conversation_seed(
            payload_json, canonical_json(assembled)
        )
        history_json = str(seed["history_json"])
        deferred_json: str | None = None
        pending_steered: list[str] = []
        stopped_by: str | None = None
        round_record: dict[str, object] = {}
        rounds = 0
        tool_records: list[dict[str, object]] = []
        output_chars = 0
        # 22-progress: the bounded no-progress hint derived AFTER a tool
        # round rides the NEXT model round (consumed once, then reset —
        # a steering continuation of a final round never re-receives a
        # stale hint). ``None`` keeps every legacy round byte-identical.
        progress_hint_json: str | None = None
        progress_hint_emissions = 0
        progress_hint_last: dict[str, object] | None = None
        # 13-intake-runner: the plain-answer variants END the accepted
        # turn here (P1: no second action-model call, no executor, no
        # effects). A deterministic stop control still wins its boundary;
        # claimed natural-language steering is never dropped — the run
        # falls through into the executor loop under it, and the intake
        # answer is superseded by the steered continuation.
        intake_kind = (
            str(intake["turn_kind"]) if intake.get("interpreted") else None
        )
        intake_answered = False
        if intake_kind in INTAKE_NO_EXECUTOR_KINDS:
            boundary_texts, stop = _claim_steering_boundary(payload_json)
            if stop is not None:
                stopped_by = stop
            elif not boundary_texts:
                intake_answered = True
            else:
                pending_steered.extend(boundary_texts)
        if intake_answered:
            answer_text = str(intake.get("output_text") or "")
            recorded = _agent_assistant_recorded(payload_json, answer_text)
            summary = {
                "rounds": 0,
                "tool_calls": tool_records,
                "output_chars": len(answer_text),
                "assistant_recorded": bool(recorded.get("recorded")),
                "assistant_message_id": recorded.get("message_id"),
                "intake": _intake_summary_document(intake),
            }
            # 20-completion: the additive evidence-backed task-outcome
            # observation (absent for taskless runs — legacy shape
            # byte-identical; never closes or verifies an episode).
            outcome_json = _agent_task_outcome_finalized(payload_json)
            if outcome_json != "null":
                summary["task_outcome"] = json.loads(outcome_json)
            task_open_reason = _task_open_reason(summary)
            if task_open_reason is not None:
                # W2: the durable episode is still open — the summary (and
                # the completed event projected from it) qualifies the
                # terminal success instead of presenting an unqualified
                # completion while criteria remain unverified.
                summary[TASK_OPEN_EVENT_KEY] = task_open_reason
            if stopped_by is not None:
                summary[STOPPED_BY_EVENT_KEY] = stopped_by
            _agent_run_finished(payload_json, canonical_json(summary))
            return {"run_id": intent.run_id, "state": "succeeded", **summary}
        while True:
            # Safe boundary BEFORE every model round: the durable
            # steering queue is consumed exactly once here, and a
            # deterministic pause/cancel stops the attempt without a
            # further model call or effect (C4/C5).
            if rounds > 0:
                pending_steered.extend(_drain_steering_messages())
            boundary_texts, stop = _claim_steering_boundary(payload_json)
            pending_steered.extend(boundary_texts)
            if stop is not None:
                stopped_by = stop
                break
            steered_json = (
                canonical_json(pending_steered) if pending_steered else None
            )
            pending_steered = []
            round_record = _agent_model_round(
                payload_json,
                pins_json,
                history_json,
                deferred_json,
                rounds,
                steered_json,
                progress_hint_json,
            )
            progress_hint_json = None
            history_json = str(round_record["history_json"])
            if str(round_record["status"]) == "final":
                output_chars = int(str(round_record["output_chars"]))
                late_steered = _drain_steering_messages()
                boundary_texts, stop = _claim_steering_boundary(payload_json)
                if stop is not None:
                    # The attempt ends at the boundary; late
                    # natural-language steering stays queued for the
                    # next runner (never dropped, never re-applied).
                    stopped_by = stop
                    break
                if not late_steered and not boundary_texts:
                    break
                pending_steered.extend(late_steered)
                pending_steered.extend(boundary_texts)
                deferred_json = None
                rounds += 1
                if rounds >= intent.max_rounds:
                    raise AgentStageError(ROUND_BUDGET_REASON)
                continue
            calls = list(round_record["calls"])
            existing_rows = int(
                _resolve_agent_run().run(
                    _resolve_agent_run().tool_row_count(intent)
                )
            )
            if existing_rows + len(calls) > AGENT_TOOL_MAX_PER_RUN:
                raise AgentStageError(TOOL_BUDGET_REASON)
            results: dict[str, str] = {}
            steering_interrupt = False
            task_mutated = False
            for call in calls:
                # Safe boundary BEFORE EACH NEW tool effect (C5/C6):
                # steering or a deterministic stop gates the next
                # command; the already-settled calls keep their
                # honest recorded outcomes either way.
                boundary_texts, stop = _claim_steering_boundary(payload_json)
                if stop is not None:
                    stopped_by = stop
                    break
                if boundary_texts:
                    pending_steered.extend(boundary_texts)
                    steering_interrupt = True
                    break
                call_json = canonical_json(call)
                outcome = _agent_tool_execute(payload_json, call_json)
                if str(outcome["state"]) == StepState.OUTCOME_UNKNOWN.value:
                    outcome = _settle_unknown(payload_json, call_json, call, outcome)
                results[str(call["tool_call_id"])] = str(outcome["result_text"])
                tool_records.append(
                    {
                        "seq": outcome["seq"],
                        "state": outcome["state"],
                        "reason": outcome.get("reason"),
                    }
                )
                if (
                    str(call.get("capability_id") or "")
                    in (
                        TASK_PROPOSE_UPDATE_CAPABILITY,
                        TASK_UPDATE_ITEM_CAPABILITY,
                    )
                    and str(outcome["state"]) == StepState.SUCCEEDED.value
                ):
                    task_mutated = True
            if stopped_by is not None:
                break
            if task_mutated:
                # In-run task-header refresh (the seam 13-intake-runner
                # left open): the NEXT model round re-reads the current
                # task state through the SAME leading system part — no
                # run restart, no second task episode. Checkpointed read
                # + deterministic re-bind; idempotent, so a refresh adds
                # no effect beyond the mutation the model itself made.
                history_json = _history_with_refreshed_task_block(
                    history_json,
                    _agent_task_header_refreshed(payload_json),
                )
            if results:
                # 22-progress: ONE checkpointed bounded no-progress
                # derivation over THIS run's own artifacts (history +
                # the round's real outcomes; the steering branch's
                # not_executed wire notes are appended only below, so
                # they are never observations). Advisory only: the
                # document, when not "null", renders as the labeled
                # ``[Progress hint]`` prefix of the NEXT round — it
                # changes no effect, no receipt, no lifecycle state.
                hint_json = _agent_progress_hint(
                    payload_json,
                    history_json,
                    canonical_json({"calls": results}),
                )
                if hint_json != "null":
                    progress_hint_json = hint_json
                    progress_hint_emissions += 1
                    document = json.loads(hint_json)
                    if isinstance(document, dict):
                        progress_hint_last = document
            if steering_interrupt:
                # The model must SEE the steering before any further
                # effect: the not-yet-dispatched deferred calls carry
                # explicit not_executed documents (wire honesty —
                # nothing ran, nothing invented) and the next round
                # re-decides under the injected UserPromptPart.
                for call in calls:
                    call_id = str(call["tool_call_id"])
                    if call_id not in results:
                        results[call_id] = _steering_stop_result_text(
                            "steering_pending"
                        )
                deferred_json = canonical_json({"calls": results})
                rounds += 1
                if rounds >= intent.max_rounds:
                    raise AgentStageError(ROUND_BUDGET_REASON)
                continue
            deferred_json = canonical_json({"calls": results})
            rounds += 1
            if rounds >= intent.max_rounds:
                raise AgentStageError(ROUND_BUDGET_REASON)
        recorded = _agent_assistant_recorded(
            payload_json, str(round_record.get("output") or "")
        )
        summary = {
            "rounds": rounds + 1 if round_record else 0,
            "tool_calls": tool_records,
            "output_chars": output_chars,
            "assistant_recorded": bool(recorded.get("recorded")),
            "assistant_message_id": recorded.get("message_id"),
        }
        if intake.get("interpreted"):
            # The task/phase binding this run executed under (AC 1/2),
            # value-free and observable next to the conversation outcome.
            summary["intake"] = _intake_summary_document(intake)
        if progress_hint_emissions:
            # 22-progress: additive observability of the bounded
            # no-progress hints this attempt emitted (count + the LAST
            # document). Absent for hint-free runs — legacy shape
            # byte-identical; advisory data, never a lifecycle change.
            summary["progress_hints"] = progress_hint_emissions
            summary["progress_hint"] = progress_hint_last
        # 20-completion: the additive evidence-backed task-outcome
        # observation (absent for taskless runs — legacy shape
        # byte-identical; a finished attempt never closes or verifies
        # an episode by itself, E1/P3).
        outcome_json = _agent_task_outcome_finalized(payload_json)
        if outcome_json != "null":
            summary["task_outcome"] = json.loads(outcome_json)
        task_open_reason = _task_open_reason(summary)
        if task_open_reason is not None:
            # W2: the durable episode is still open — the summary (and
            # the completed event projected from it) qualifies the
            # terminal success instead of presenting an unqualified
            # completion while criteria remain unverified.
            summary[TASK_OPEN_EVENT_KEY] = task_open_reason
        if stopped_by is not None:
            summary[STOPPED_BY_EVENT_KEY] = stopped_by
        _agent_run_finished(payload_json, canonical_json(summary))
        return {"run_id": intent.run_id, "state": "succeeded", **summary}
    except AgentStageError as stage:
        if stage.reason == INTAKE_REJECTED_REASON:
            # A3: an exhausted intake rejection is assistant-VISIBLE.
            # ONE canonical safe reply is recorded through the
            # checkpointed producer BEFORE the run fails and releases
            # its lease: the conversation shows an honest terminal
            # outcome (interpretation failed, no effects, task stays
            # open) instead of silent termination. The producer is
            # idempotent per run_id (content_ref ``agent-run:<run_id>``
            # belt + DBOS step checkpoint), so a replayed/restarted
            # workflow never duplicates the row; legacy runs without a
            # conversation link record nothing. Safe field codes stay
            # ONLY in the ``agent.intake_interpreted`` event (emitted by
            # the intake seam); the visible text is the fixed constant.
            _agent_assistant_recorded(
                payload_json, INTAKE_REJECTED_VISIBLE_REPLY
            )
        _agent_run_failed(payload_json, stage.reason)
        _release_lease_quietly(intent)
        raise AgentRunFailed(stage.reason) from None
    except StepsError:
        _record_quietly(intent, "fail")
        _release_lease_quietly(intent)
        raise
    except dbos_error.DBOSWorkflowCancelledError:
        _record_quietly(intent, "cancel")
        _release_lease_quietly(intent)
        raise
    except AgentRunError:
        _record_quietly(intent, "fail")
        _release_lease_quietly(intent)
        raise
    except Exception:
        # An unexpected failure (a defect, not a classified stage): the
        # engine lands ERROR either way — record the honest facade
        # translation so both sides agree, then let the original
        # exception surface as-is (never swallowed, never classified).
        _record_quietly(intent, "fail")
        _release_lease_quietly(intent)
        raise


def _settle_unknown(
    payload_json: str,
    call_json: str,
    call: Mapping[str, object],
    outcome: Mapping[str, object],
) -> dict[str, object]:
    """Drive one unknown-effect call to a resolved outcome (workflow).

    Parks the durable workflow on ``DBOS.recv`` polls against the
    persisted deadline; the durable state (a reconciler-resolved step
    row) decides — never a retry and never an invented success. A
    ``cancelled`` resolution translates the run onto the frozen diagram.
    """
    intent = parse_agent_run_intent(payload_json)
    deadline = _deadline_of(
        _agent_reconciling(payload_json, call_json, canonical_json(dict(outcome)))
    )
    reconcile_attempts = 0
    while True:
        row = _agent_read_step(payload_json, int(call["seq"]))
        if row is not None and str(row["state"]) in {
            item.value for item in TERMINAL_STEP_STATES
        }:
            _agent_wait_resumed(payload_json, RunState.RECONCILING.value)
            state = str(row["state"])
            if state == StepState.CANCELLED.value:
                _record_quietly(intent, "cancel")
                raise AgentRunFailed("step_cancelled")
            receipt = row["receipt"] if isinstance(row["receipt"], Mapping) else {}
            return {
                "seq": int(call["seq"]),
                "state": state,
                "reason": receipt.get("reason"),
                "op_id": receipt.get("op_id"),
                "result_text": _receipt_result_document(
                    intent, state, receipt, resolved=True
                ),
            }
        remaining = (deadline - _utcnow()).total_seconds()
        if remaining <= 0:
            _agent_wait_resumed(payload_json, RunState.RECONCILING.value)
            settled = json.loads(_agent_reconcile_expired(payload_json, call_json))
            row_state = str(settled.get("state"))
            receipt = (
                settled["receipt"]
                if isinstance(settled.get("receipt"), Mapping)
                else {}
            )
            if row_state != StepState.FAILED.value:
                # A reconciler resolution landed before the expiry could:
                # the durable truth decides, exactly like a mid-wait
                # resolution (never the timeout, never a re-dispatch).
                if row_state == StepState.CANCELLED.value:
                    _record_quietly(intent, "cancel")
                    raise AgentRunFailed("step_cancelled")
                return {
                    "seq": int(call["seq"]),
                    "state": row_state,
                    "reason": receipt.get("reason"),
                    "op_id": receipt.get("op_id"),
                    "result_text": _receipt_result_document(
                        intent, row_state, receipt, resolved=True
                    ),
                }
            return {
                "seq": int(call["seq"]),
                "state": StepState.FAILED.value,
                "reason": EXECUTION_TIMEOUT_REASON,
                "op_id": receipt.get("op_id"),
                "result_text": _timeout_result_document(intent, receipt),
            }
        DBOS.recv(
            topic=RESUME_TOPIC,
            timeout_seconds=min(
                intent.wait_poll_seconds,
                max(MIN_RECV_TIMEOUT_SECONDS, remaining),
            ),
        )
        reconcile_attempts += 1
        if reconcile_attempts % 6 == 0:
            executor = _resolve_agent_run()
            try:
                resolved = executor.run(
                    executor.areconcile_step(intent.run_id, int(call["seq"]))
                )
                if resolved:
                    continue
            except Exception:
                pass


# Registration at import (the engine trampoline resolves the handler by
# name; worker entrypoints import this module via MAI_WORKER_HANDLERS).
_engine.register_job_handler(HANDLER_NAME, run_agent_conversation)
