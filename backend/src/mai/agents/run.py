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
  content) lands in the shared per-run stream.
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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    REDACTED,
    CapabilityDispatcher,
    _secret_output_forms,
    canonical_payload_hash,
)
from mai.contracts.jobs import TERMINAL_STEP_STATES, RunState, StepState
from mai.credentials.env import EnvFileError, EnvResolver
from mai.jobs import engine as _engine
from mai.jobs.engine import EngineError, validate_run_id
from mai.jobs.resource_locks import ResourceLockService
from mai.jobs.steps import (
    RESUME_TOPIC,
    WAIT_FOR_APPROVAL,
    StepExecutor,
    StepIntent,
    StepsError,
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
    """
    for value in _secret_output_forms(secrets):
        text = text.replace(value, REDACTED)
    return text


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
#: honest failure — never a silent truncation of run state).
MAX_HISTORY_BYTES: Final[int] = 2 * 1024 * 1024

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
ROUND_BUDGET_REASON: Final[str] = "round_budget_exhausted"
HISTORY_BUDGET_REASON: Final[str] = "history_budget_exhausted"
TOOL_BUDGET_REASON: Final[str] = "tool_call_budget_exhausted"
CRASH_RECOVERY_RESULT_REASON: Final[str] = "crash_recovery"

#: Event types this module owns in the shared per-run stream (C05 rule 4:
#: the envelope SHAPE is frozen in ``mai.contracts.jobs``, the type
#: vocabulary stays open — each producer owns its types). Row transitions
#: of tool calls emit the shared ``step.started``/``step.completed`` types
#: through the reviewed steps machinery, so the whole-run stream stays
#: coherent for the UI.
AGENT_STARTED_EVENT_TYPE: Final[str] = "agent.started"
AGENT_CONTEXT_EVENT_TYPE: Final[str] = "agent.context_assembled"
AGENT_ROUND_EVENT_TYPE: Final[str] = "agent.model_round"
AGENT_RECONCILING_EVENT_TYPE: Final[str] = "agent.awaiting_reconciliation"
AGENT_COMPLETED_EVENT_TYPE: Final[str] = "agent.completed"
AGENT_FAILED_EVENT_TYPE: Final[str] = "agent.failed"


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

    # -- one conversation round (checkpointed) ------------------------------

    async def amodel_round(
        self,
        intent: AgentRunIntent,
        pins: Mapping[str, object],
        history_json: str,
        deferred_json: str | None,
        round_index: int,
        steered_json: str | None = None,
    ) -> dict[str, object]:
        """One durable model round (see module docstring).

        Returns the checkpointed round record: either the assistant's
        final text or the deferred tool-request list with each call's
        deterministic step sequence. Classified failures raise
        :class:`AgentStageError` (bind/credential/provider classes).
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

        agent: Agent[None, str | DeferredToolRequests] = Agent(
            model=resolved_model,
            tools=[_deferring_tool(tool) for tool in bridge.tools],
            output_type=[str, DeferredToolRequests],
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
        try:
            result = await agent.run(
                steered_prompt,
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
        if outcome.output is not None and outcome.output.text:
            resolved_values = _resolved_credential_values(
                self._steps, outcome.credential_references
            )
            try:
                parsed_output = json.loads(outcome.output.text)
                receipt["response"] = (
                    _scrub_secret_document(parsed_output, resolved_values)
                    if len(outcome.output.text) <= 32768
                    else outcome.output.text[:32768] + "… [truncated]"
                )
            except Exception:
                receipt["response"] = _scrub_secret_text(
                    outcome.output.text[:32768], resolved_values
                )
        if isinstance(payload, Mapping):
            receipt["params"] = _scrubbed_step_params(
                str(capability_id),
                dict(payload),
                _resolved_credential_values(
                    self._steps, outcome.credential_references
                ),
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
            output_text=None if outcome.output is None else outcome.output.text,
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

    async def afinish_run(
        self, intent: AgentRunIntent, summary: Mapping[str, object]
    ) -> str:
        """Record ``running → succeeded``; emit the safe summary event."""
        current = await self._steps.lifecycle.current_state(intent.run_id)
        if current is RunState.RUNNING:
            await self._steps.lifecycle.record_transition(
                intent.run_id, RunState.SUCCEEDED
            )
            current = RunState.SUCCEEDED
        assert current is not None
        await self._emit_once(
            intent.run_id,
            AGENT_COMPLETED_EVENT_TYPE,
            {
                "rounds": summary.get("rounds"),
                "tool_calls": summary.get("tool_calls"),
                "output_chars": summary.get("output_chars"),
            },
        )
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


@DBOS.step(name="agents.model_round")
def _agent_model_round(
    payload_json: str,
    pins_json: str,
    history_json: str,
    deferred_json: str | None,
    round_index: int,
    steered_json: str | None = None,
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
        )
    )


@DBOS.step(name="agents.tool_execute")
def _agent_tool_execute(payload_json: str, call_json: str) -> dict[str, object]:
    """One exactly-once guarded attempt of one tool call (checkpointed)."""
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(executor.aexecute_tool(intent, json.loads(call_json)))


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


@DBOS.step(name="agents.run_finished")
def _agent_run_finished(payload_json: str, summary_json: str) -> str:
    """Record ``running → succeeded`` after the conversation (observe)."""
    intent = parse_agent_run_intent(payload_json)
    executor = _resolve_agent_run()
    return executor.run(executor.afinish_run(intent, json.loads(summary_json)))


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
    """Drain any mid-run user steering messages queued on ``STEER_TOPIC``."""
    messages: list[str] = []
    while True:
        try:
            raw = DBOS.recv(topic=STEER_TOPIC, timeout_seconds=0)
        except Exception:
            break
        if raw is None:
            break
        if isinstance(raw, Mapping):
            text = raw.get("message")
            if isinstance(text, str) and text.strip():
                messages.append(text.strip())
        elif isinstance(raw, str) and raw.strip():
            messages.append(raw.strip())
    return messages


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
        assembled = _agent_context_assembled(payload_json, pins_json)
        history_json = initial_history_json(
            str(assembled["system_prompt"]), str(assembled["user_message"])
        )
        deferred_json: str | None = None
        pending_steered: list[str] = []
        rounds = 0
        tool_records: list[dict[str, object]] = []
        output_chars = 0
        while True:
            if rounds > 0:
                pending_steered.extend(_drain_steering_messages())
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
            )
            history_json = str(round_record["history_json"])
            if str(round_record["status"]) == "final":
                output_chars = int(str(round_record["output_chars"]))
                late_steered = _drain_steering_messages()
                if not late_steered:
                    break
                pending_steered.extend(late_steered)
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
            for call in calls:
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
            deferred_json = canonical_json({"calls": results})
            rounds += 1
            if rounds >= intent.max_rounds:
                raise AgentStageError(ROUND_BUDGET_REASON)
        summary = {
            "rounds": rounds + 1,
            "tool_calls": tool_records,
            "output_chars": output_chars,
        }
        _agent_run_finished(payload_json, canonical_json(summary))
        return {"run_id": intent.run_id, "state": "succeeded", **summary}
    except AgentStageError as stage:
        _agent_run_failed(payload_json, stage.reason)
        raise AgentRunFailed(stage.reason) from None
    except StepsError:
        _record_quietly(intent, "fail")
        raise
    except dbos_error.DBOSWorkflowCancelledError:
        _record_quietly(intent, "cancel")
        raise
    except AgentRunError:
        _record_quietly(intent, "fail")
        raise
    except Exception:
        # An unexpected failure (a defect, not a classified stage): the
        # engine lands ERROR either way — record the honest facade
        # translation so both sides agree, then let the original
        # exception surface as-is (never swallowed, never classified).
        _record_quietly(intent, "fail")
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
            raise AgentStageError(RECONCILE_WAIT_DEADLINE_REASON)
        DBOS.recv(
            topic=RESUME_TOPIC,
            timeout_seconds=min(
                intent.wait_poll_seconds,
                max(MIN_RECV_TIMEOUT_SECONDS, remaining),
            ),
        )


# Registration at import (the engine trampoline resolves the handler by
# name; worker entrypoints import this module via MAI_WORKER_HANDLERS).
_engine.register_job_handler(HANDLER_NAME, run_agent_conversation)
