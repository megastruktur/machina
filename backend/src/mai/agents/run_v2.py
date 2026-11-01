"""Durable v2 working-agent runs: ONE action-first loop (runtime-v2.md).

Card ``rv-action-runtime`` against runtime-v2.md §2/§3/§5/§8/§9 (the frozen
Runtime v2 contract) and the ``rv-action-runtime`` acceptance criteria. The
module owns the distinct ``agent.run_v2`` handler:

- **No intake (§2/§S1):** a v2 submission goes straight to the working model.
  There is no ``TurnInterpretation`` planner call, no structured turn union,
  no mandatory todo, and none of the four legacy ``agent.intake_*`` events is
  ever emitted on a v2 run. Natural-language response, clarification,
  planning, tools and completion share ONE bounded conversation loop.
- **Durable rounds (§9):** each model round is ONE checkpointed ``DBOS.step``
  (the same :class:`~mai.agents.run.AgentRunExecutor` seams the legacy
  handler uses — reused, never bypassed); each requested tool call is ONE
  exactly-once guarded attempt through
  :class:`~mai.agents.tools.CapabilityToolBridge` →
  :class:`~mai.capabilities.dispatch.CapabilityDispatcher` (C06 rule 4). A
  malformed/invalid tool payload is denied BEFORE any effect and the honest
  denial is returned to the SAME loop as a bounded structured correction —
  the run does not fail.
- **Objective binding (§3):** the run binds durably to its ``objective_id``
  through the ONE binding seam
  (:func:`mai.agents.objective_store.abind_run`); the objective's verbatim
  goal/constraints ride the leading system part of the seed (bounded,
  checkpointed read).
- **Journal projection (§8):** every tool outcome is projected into the
  ``objective.*`` ledger through :func:`mai.agents.journal_store.ajournal_apply`
  (action attempt + dispatcher decision, receipt, unknown note). A journal or
  checklist write failure is bookkeeping trouble: an honest
  ``agent.journal_degraded`` observation lands on the run stream and the run
  continues — it can never turn successful effects into "nothing happened"
  (§8.4) and can never fail the run.
- **Approval deferral (§9):** a privileged/mutating call in ask mode comes
  back ``approval_required_but_absent``; the v2 loop parks the run durably on
  ``waiting_approval`` (``agent.awaiting_approval`` + persisted deadline).
  Resume presents the ONE payload-bound approval through the bridge's
  ``dispatch_once(approval_id=…)`` seam — worker-side, never generic HTTP
  re-execution; the presented payload is the checkpointed round's exact
  arguments (C03 rule 4).
- **Crash semantics (§9, guard rule 2):** a recovered ``running``-without-
  receipt attempt translates to ``outcome_unknown`` and the run parks
  ``reconciling`` (``agent.awaiting_reconciliation``); settled rounds are
  never repeated, the uncertain effect is never blindly replayed, and the
  unknown is journaled honestly (noted, then marked reconciled by explicit
  classification only).
- **Evidence-bound completion (§8.3):** an optional ``machina-complete``
  projection in the final assistant output becomes an
  :class:`~mai.contracts.objectives.ObjectiveCompletionVerdict` only when its
  criteria are bound to THIS run's executed evidence (succeeded step rows by
  ``step:<seq>`` or ``op:<uuid>`` references). An unevidenced ``verified``
  claim is rejected with ``completion_missing_evidence``, journaled with
  ``missing_evidence=True`` and the objective stays active — the honest
  state. A malformed advisory ``machina-checklist`` block is bookkeeping
  trouble, never a conversation rejection (§8.2).

Deliberate boundaries (recorded limitations): the model-facing projection
convention (the two optional fenced blocks) is this slice's runtime contract;
the conversation-api slice owns the HTTP submission/approval surfaces and may
carry the same decisions over its own API. Steering controls ride the same
durable conversation queue as legacy; child delegation payload v2 is the
policy-delegation slice's surface.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text as sql_text

from dbos import DBOS
from dbos import error as dbos_error

from mai.agents import run as agent_run
from mai.agents.journal_store import (
    ajournal_apply,
    aput_checklist,
    aread_checklist,
)
from mai.agents.objective_store import (
    V2_HANDLER_NAME,
    abind_run,
    acomplete_objective,
    aget_objective,
)
from mai.agents.output_safety import project_document, project_text
from mai.agents.run import (
    AGENT_TOOL_MAX_PER_RUN,
    AGENT_TOOL_RESULT_EVENT_TYPE,
    CRASH_RECOVERY_RESULT_REASON,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_RECONCILE_WAIT_SECONDS,
    DEFAULT_WAIT_POLL_SECONDS,
    MAX_MAX_ROUNDS,
    MAX_RECONCILE_WAIT_SECONDS,
    MAX_ROLE_LEN,
    MAX_TASK_CHARS,
    MAX_WAIT_POLL_SECONDS,
    MIN_RECV_TIMEOUT_SECONDS,
    ROUND_BUDGET_REASON,
    STOPPED_BY_EVENT_KEY,
    TOOL_BUDGET_REASON,
    AgentRunError,
    AgentRunErrorCode,
    AgentRunFailed,
    AgentRunIntent,
    AgentStageError,
    _deadline_of,
    _decide_steering_boundary,
    _drain_steering_messages,
    _receipt_result_document,
    _record_quietly,
    _release_lease_quietly,
    _resolved_credential_values,
    _result_document,
    _scrubbed_step_params,
    _status_of_state,
    _steering_stop_result_text,
    _timeout_result_document,
    _tool_step_intent,
    _utcnow,
    canonical_json,
)
from mai.agents.tools import BridgeError, CapabilityToolBridge
from mai.capabilities.dispatch import DispatchError, canonical_payload_hash
from mai.contracts.jobs import RunState, StepState, TERMINAL_STEP_STATES
from mai.contracts.objectives import (
    ActorKind,
    JournalActionApplied,
    JournalReceiptRecorded,
    JournalUnknownNoted,
    JournalVerificationRecorded,
    ObjectiveCompletionVerdict,
    ObjectiveContractError,
    ObjectiveDetail,
    validate_objective_completion,
)
from mai.contracts.tasks import (
    CriterionResult,
    DomainOutcome,
    EffectCertainty,
    EvidenceKind,
    EvidenceRef,
    ExecutionStatus,
    TaskOutcome,
    VerificationStatus,
)
from mai.jobs import engine as _engine
from mai.jobs.engine import EngineError, validate_run_id
from mai.jobs.steps import (
    APPROVAL_WAIT_DEADLINE_REASON,
    EXECUTION_TIMEOUT_REASON,
    RESUME_TOPIC,
    WAIT_FOR_APPROVAL,
    _translate_outcome,
)
from mai.policy.approvals import ApprovalError
from mai.policy.evaluate import ExecutionContext, execution_context_from_json

HANDLER_NAME: Final[str] = V2_HANDLER_NAME
assert HANDLER_NAME == "agent.run_v2"

#: The v2 run payload version (coordinator brief for this slice; distinct
#: from the frozen legacy ``v: 1`` payload, which keeps replaying as-is).
RUN_PAYLOAD_VERSION_V2: Final[int] = 2

#: Approval-wait budget (the durable ``waiting_approval`` park deadline).
DEFAULT_APPROVAL_WAIT_SECONDS: Final[float] = 3600.0
MAX_APPROVAL_WAIT_SECONDS: Final[float] = 86400.0

#: Deterministic ``job_steps.seq`` stride for approval-resume attempts (the
#: refusals-before-resume land terminal on the ORIGINAL seq, so the resume
#: attempt gets its own exactly-once row far above the normal encoding span).
_APPROVAL_RESUME_SEQ_STRIDE: Final[int] = 1024

#: New v2-only ``agent.*`` event types (additive per runtime-v2.md §5.1).
AGENT_AWAITING_APPROVAL_EVENT_TYPE: Final[str] = "agent.awaiting_approval"
AGENT_JOURNAL_DEGRADED_EVENT_TYPE: Final[str] = "agent.journal_degraded"

#: The dispatcher reason that means "durable approval wait" (the receipt the
#: legacy executor seam records for the refused pre-approval attempt).
_APPROVAL_REQUIRED_REASON: Final[str] = "approval_required_but_absent"

#: Optional model-output projection blocks (runtime contract of this slice).
_CHECKLIST_FENCE: Final[str] = "machina-checklist"
_COMPLETE_FENCE: Final[str] = "machina-complete"

#: Bounded objective-context block (verbatim goal/constraints, §3.1).
_OBJECTIVE_GOAL_MAX_CHARS: Final[int] = 4000
_OBJECTIVE_BLOCK_MAX_CHARS: Final[int] = 8000

#: Stable value-free vocabulary for the journal ``decision`` field.
_DECISION_ALLOWED: Final[str] = "allowed"
_DECISION_DENIED: Final[str] = "denied"
_DECISION_UNKNOWN: Final[str] = "unknown"

_COMPLETE_MAX_CRITERIA: Final[int] = 12


# ---------------------------------------------------------------------------
# v2 run intent (strict, value-free, identity-matched)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentRunIntentV2:
    """The validated payload for :data:`HANDLER_NAME`.

    Everything from the legacy :class:`AgentRunIntent` discipline carries
    over (identity-matched :class:`~mai.policy.evaluate.ExecutionContext`,
    optional explicit model selection, bounded budgets) plus the
    ``objective_id`` binding (§3.3) and the approval-wait budget (§9).
    :attr:`legacy` is the executor-facing shim: the composed
    :class:`~mai.agents.run.AgentRunExecutor` seams read exactly these
    fields, so a v2 run drives the reviewed machinery unmodified.
    """

    run_id: str
    server_id: str
    objective_id: str
    context: ExecutionContext
    role: str
    task: str
    connection: str | None
    profile: str | None
    max_rounds: int
    reconcile_wait_seconds: float
    wait_poll_seconds: float
    approval_wait_seconds: float

    def legacy(self) -> AgentRunIntent:
        """The executor-facing intent (every executor seam's parameter)."""
        return AgentRunIntent(
            run_id=self.run_id,
            server_id=self.server_id,
            context=self.context,
            role=self.role,
            task=self.task,
            connection=self.connection,
            profile=self.profile,
            max_rounds=self.max_rounds,
            reconcile_wait_seconds=self.reconcile_wait_seconds,
            wait_poll_seconds=self.wait_poll_seconds,
        )


def _reject_payload(detail: str) -> AgentRunError:
    return AgentRunError(AgentRunErrorCode.INVALID_PAYLOAD, detail)


def _canonical_uuid_field(value: object, field: str) -> str:
    try:
        return validate_run_id(value)
    except EngineError:
        raise _reject_payload(f"{field} must be a canonical UUID string") from None


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


def parse_agent_run_intent_v2(payload_json: str) -> AgentRunIntentV2:
    """Validate one ``agent.run_v2`` payload; return it typed (fail closed).

    Strict shape: exactly the frozen field set; ``payload_version`` must be
    exactly :data:`RUN_PAYLOAD_VERSION_V2` (an unrecognized version fails
    closed, value-free — never a narrower guess); identities are canonical
    UUIDs; the embedded execution context must name THIS run and THIS
    server.
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
        "payload_version",
        "run_id",
        "server_id",
        "objective_id",
        "context",
        "connection",
        "profile",
        "role",
        "task",
        "rounds",
        "reconcile_wait_seconds",
        "wait_poll_seconds",
        "approval_wait_seconds",
    }
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise _reject_payload(f"unknown field {unknown[0]!r}")
    missing = {
        "payload_version",
        "run_id",
        "server_id",
        "objective_id",
        "context",
        "role",
        "task",
    } - set(document)
    if missing:
        raise _reject_payload(f"missing field {sorted(missing)[0]!r}")
    if document["payload_version"] != RUN_PAYLOAD_VERSION_V2:
        raise _reject_payload(
            f"payload_version must be {RUN_PAYLOAD_VERSION_V2}"
        )
    run_id = _canonical_uuid_field(document["run_id"], "run_id")
    server_id = _canonical_uuid_field(document["server_id"], "server_id")
    objective_id = _canonical_uuid_field(document["objective_id"], "objective_id")
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
    return AgentRunIntentV2(
        run_id=run_id,
        server_id=server_id,
        objective_id=objective_id,
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
        approval_wait_seconds=_bounded_wait(
            document.get("approval_wait_seconds"),
            "approval_wait_seconds",
            DEFAULT_APPROVAL_WAIT_SECONDS,
            MAX_APPROVAL_WAIT_SECONDS,
        ),
    )


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def approval_resume_seq(seq: int) -> int:
    """The deterministic ``job_steps.seq`` of one approval-resume attempt.

    Derived from the ORIGINAL refused call's seq, so a recovered attempt of
    the same resume derives the SAME row (the exactly-once ledger key) and
    normal round-major seqs (bounded far below the stride) never collide.
    """
    return int(seq) + _APPROVAL_RESUME_SEQ_STRIDE


def _action_id(run_id: str, seq: int) -> uuid.UUID:
    """The stable per-attempt journal action identity (dedup on replay)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"agent.run_v2:{run_id}:step:{seq}")


def _unknown_effect_ref(run_id: str, seq: int) -> str:
    """The stable per-attempt unknown-effect reference (§8.1)."""
    return f"run:{run_id}:step:{seq}"


def _completion_idempotency_key(run_id: str) -> uuid.UUID:
    """The per-run completion dedup token (retransmission-safe)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"agent.run_v2:{run_id}:complete")


def _extract_projection_blocks(
    output: str,
) -> tuple[dict[str, object] | None, dict[str, object] | None, str | None]:
    """Extract the optional advisory projections from ONE final output.

    Returns ``(checklist_document_or_None, complete_document_or_None,
    trouble_reason)``. A fenced block that does not parse as a JSON object
    yields ``None`` plus a bounded value-free trouble reason — bookkeeping
    trouble, never a conversation rejection (§8.2). Plain prose (the
    overwhelmingly common case) carries no blocks at all.
    """
    checklist: dict[str, object] | None = None
    complete: dict[str, object] | None = None
    trouble: str | None = None
    for fence, sink in (
        (_CHECKLIST_FENCE, "checklist"),
        (_COMPLETE_FENCE, "complete"),
    ):
        marker = f"```{fence}"
        start = output.find(marker)
        if start < 0:
            continue
        body_start = output.find("\n", start)
        end = output.find("```", body_start + 1)
        if body_start < 0 or end < 0:
            trouble = f"{fence}_unterminated"
            continue
        try:
            parsed = json.loads(output[body_start + 1 : end])
        except ValueError:
            trouble = f"{fence}_unparsable"
            continue
        if not isinstance(parsed, dict):
            trouble = f"{fence}_not_object"
            continue
        if sink == "checklist":
            checklist = parsed
        else:
            complete = parsed
    return checklist, complete, trouble


def _objective_context_block(detail: ObjectiveDetail | None) -> str | None:
    """The bounded leading system block carrying the objective episode.

    Verbatim provenance (the operator's original request + constraints,
    already projected at creation) — never model-reworded (§3.1). Absent
    when the objective carries nothing usable.
    """
    if detail is None:
        return None
    goal = project_text(detail.original_request, max_chars=_OBJECTIVE_GOAL_MAX_CHARS)
    if not goal.strip():
        return None
    lines = [f"[Objective] goal: {goal}"]
    for constraint in list(detail.constraints)[:12]:
        safe = project_text(constraint, max_chars=500)
        if safe.strip():
            lines.append(f"[Objective] constraint: {safe}")
    block = "\n".join(lines)
    return project_text(block, max_chars=_OBJECTIVE_BLOCK_MAX_CHARS)


def _journal_decision(state: str) -> str:
    """The bounded dispatcher-decision vocabulary value for one outcome."""
    if state == StepState.SUCCEEDED.value:
        return _DECISION_ALLOWED
    if state == StepState.OUTCOME_UNKNOWN.value:
        return _DECISION_UNKNOWN
    return _DECISION_DENIED


def _descriptor_effect_class(executor: Any, capability_id: str) -> str:
    """The registered effect class of one capability (bounded fallback)."""
    try:
        versions = executor._registry.versions(capability_id)  # noqa: SLF001
        if not versions:
            return "unknown"
        descriptor = executor._registry.get(capability_id, versions[-1])  # noqa: SLF001
        value = str(getattr(descriptor, "effect_class", "") or "")
        return value or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# the v2 DBOS steps (same discipline as the legacy handler: sync steps run
# off the engine's persistent loop via asyncio.to_thread; every async seam
# runs on the executor's one loop)
# ---------------------------------------------------------------------------


@DBOS.step(name="agents.v2_run_started")
def _v2_run_started(payload_json: str) -> str:
    """Observe-and-record the run's ``queued`` anchor + ``running``."""
    intent = parse_agent_run_intent_v2(payload_json)
    return agent_run._resolve_agent_run().observe_run_started(intent.legacy())


@DBOS.step(name="agents.v2_model_bound")
def _v2_model_bound(payload_json: str) -> dict[str, object]:
    """Pin the (connection, profile) pair (checkpointed; C06 rule 1)."""
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    return executor.run(executor.abind_model(intent.legacy()))


@DBOS.step(name="agents.v2_objective_bound")
def _v2_objective_bound(payload_json: str) -> str:
    """Bind the run to its objective (§3.3; ONE durable binding seam).

    Idempotent per (objective_id, run_id); the binding scope-checks the
    initiating server against the objective scope AT BIND TIME and refuses
    any handler other than ``agent.run_v2`` — the durable §10 backstop that
    an in-flight legacy run can never become v2. Failures propagate: a run
    that cannot bind its objective has no authority to act on it.
    """
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    link = executor.run(
        abind_run(
            executor._sessions,  # noqa: SLF001 - composed seam (mirrors _bridge_of)
            uuid.UUID(intent.objective_id),
            server_id=uuid.UUID(intent.server_id),
            run_id=uuid.UUID(intent.run_id),
            initiating_server_id=uuid.UUID(intent.server_id),
            actor_kind=ActorKind.AGENT,
            handler=HANDLER_NAME,
        )
    )
    return canonical_json(
        {
            "objective_id": str(link.objective_id),
            "handler": link.handler,
            "actor_kind": link.actor_kind.value,
        }
    )


@DBOS.step(name="agents.v2_context_assembled")
def _v2_context_assembled(payload_json: str, pins_json: str) -> dict[str, object]:
    """Assemble the bounded model payload (checkpointed; C06 rules 2–3)."""
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    return executor.run(
        executor.aassemble_context(intent.legacy(), json.loads(pins_json))
    )


@DBOS.step(name="agents.v2_objective_context")
def _v2_objective_context(payload_json: str) -> str:
    """Read the bound objective's verbatim episode (checkpointed read).

    The block text lives ONLY in this step's checkpoint (model context,
    C06 rules 2–3); the run stream never carries objective content.
    """
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    detail = executor.run(
        aget_objective(
            executor._sessions,  # noqa: SLF001 - composed seam
            uuid.UUID(intent.objective_id),
            server_id=uuid.UUID(intent.server_id),
        )
    )
    block = _objective_context_block(detail)
    return canonical_json({"block": block})


@DBOS.step(name="agents.v2_conversation_seed")
def _v2_conversation_seed(
    payload_json: str, assembled_json: str, objective_block_json: str
) -> dict[str, object]:
    """Build the conversation ``message_history`` seed (checkpointed).

    The objective block rides the LEADING system part of the seed, so the
    goal/constraints survive steering and compaction by construction (the
    legacy task-header discipline). Both inputs are checkpointed step
    results, so the seed is a deterministic function of durable state (the
    C4 replay anchor).
    """
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    assembled = json.loads(assembled_json)
    block_document = json.loads(objective_block_json)
    block = block_document.get("block")
    if isinstance(block, str) and block.strip():
        assembled = {
            **assembled,
            "system_prompt": str(assembled["system_prompt"]) + "\n" + block,
        }
    return executor.run(
        executor.abuild_conversation_seed(intent.legacy(), assembled)
    )


@DBOS.step(name="agents.v2_model_round")
def _v2_model_round(
    payload_json: str,
    pins_json: str,
    history_json: str,
    deferred_json: str | None,
    round_index: int,
    steered_json: str | None = None,
    progress_hint_json: str | None = None,
) -> dict[str, object]:
    """One durable conversation round (checkpointed; the working agent).

    Emits ``agent.model_round`` (and the streamed ``agent.output_delta`` /
    ``agent.tool_requested`` observations) through the executor's
    ``ModelEventStreamObserver`` — the canonical ``agent.*`` family (§5.1).
    """
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    return executor.run(
        executor.amodel_round(
            intent.legacy(),
            json.loads(pins_json),
            history_json,
            deferred_json,
            round_index,
            steered_json,
            progress_hint_json,
        )
    )


@DBOS.step(name="agents.v2_tool_execute")
def _v2_tool_execute(payload_json: str, call_json: str) -> dict[str, object]:
    """One exactly-once guarded attempt of one tool call (checkpointed).

    The reviewed executor seam: intent row → fence marker →
    ``CapabilityToolBridge.dispatch_once`` → outcome + safe receipt. A
    malformed payload is denied before any effect and returns the bounded
    structured denial to the SAME loop (AC2).
    """
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    return executor.run(executor.aexecute_tool(intent.legacy(), json.loads(call_json)))


@DBOS.step(name="agents.v2_steering_polled")
def _v2_steering_poll(payload_json: str) -> dict[str, object]:
    """Poll the durable steering queue at a safe boundary (checkpointed)."""
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    return executor.run(executor.apoll_steering(intent.legacy()))


@DBOS.step(name="agents.v2_steering_acked")
def _v2_steering_ack(payload_json: str, claimed_json: str) -> dict[str, object]:
    """Mark the processed steering prefix applied (checkpointed ack)."""
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    return executor.run(
        executor.aack_steering(intent.legacy(), json.loads(claimed_json))
    )


def _claim_steering_boundary_v2(payload_json: str) -> tuple[list[str], str | None]:
    """One safe-boundary steering consumption (durable, exactly once).

    Checkpointed poll → the shared pure boundary decision → checkpointed
    idempotent ack (the legacy discipline with the v2 intent).
    """
    polled = _v2_steering_poll(payload_json)
    claimed = [dict(item) for item in polled.get("pending") or []]
    if not claimed:
        return [], None
    texts, stop, ack_prefix = _decide_steering_boundary(claimed)
    _v2_steering_ack(payload_json, canonical_json(ack_prefix))
    return texts, stop


@DBOS.step(name="agents.v2_assistant_recorded")
def _v2_assistant_recorded(payload_json: str, output: str) -> dict[str, object]:
    """Record the canonical assistant reply (idempotent producer)."""
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    return executor.run(executor.arecord_assistant_reply(intent.legacy(), output))


@DBOS.step(name="agents.v2_journal_tool")
def _v2_journal_tool(
    payload_json: str, call_json: str, outcome_json: str
) -> str:
    """Project one settled tool outcome into the work journal (§8.1; AC4).

    ONE checkpointed step per outcome; every mutation deduplicates by its
    stable key (action id / operation id / effect ref), so a replayed step
    never doubles a row. Per §8.4 a journal write failure is bookkeeping
    trouble: the step emits ONE honest ``agent.journal_degraded``
    observation and returns a marker — it NEVER fails the run, because the
    effect's durable records (step rows, checkpoints, receipts) remain the
    authority and the projection catches up honestly.
    """
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    call = json.loads(call_json)
    outcome = json.loads(outcome_json)
    # The DURABLE attempt row this outcome belongs to: the original call's
    # seq, or the resume attempt's own row (an approval resume is its own
    # exactly-once attempt with its own receipt — both attempts stay in
    # the journal honestly).
    seq = int(outcome.get("resume_seq") or outcome["seq"])
    state = str(outcome["state"])
    capability_id = str(
        call.get("capability_id") or call.get("tool_name") or "unknown"
    )
    sections: list[str] = []

    def _degraded(reason: str) -> str:
        try:
            executor.run(
                executor._emit_once(  # noqa: SLF001 - composed seam
                    intent.run_id,
                    AGENT_JOURNAL_DEGRADED_EVENT_TYPE,
                    {"section": "journal", "reason": reason[:200]},
                )
            )
        except Exception:
            pass
        return canonical_json({"journaled": False, "reason": reason[:200]})

    try:
        payload = call.get("payload")
        payload_text = (
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            if isinstance(payload, Mapping)
            else "{}"
        )
        projected = project_document(payload_text, ())
        detail = (
            projected
            if isinstance(projected, str)
            else json.dumps(
                projected, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
        )
        executor.run(
            ajournal_apply(
                executor._sessions,  # noqa: SLF001 - composed seam
                uuid.UUID(intent.objective_id),
                server_id=uuid.UUID(intent.server_id),
                mutation=JournalActionApplied(
                    action_id=_action_id(intent.run_id, seq),
                    capability_id=capability_id[:200],
                    server_id=uuid.UUID(intent.server_id),
                    decision=_journal_decision(state),
                    operation_id=(
                        str(outcome["op_id"])[:128]
                        if outcome.get("op_id")
                        else None
                    ),
                    detail=detail[:4096] if detail else None,
                ),
                actor_kind=ActorKind.AGENT,
                run_id=uuid.UUID(intent.run_id),
            )
        )
        sections.append("action")
        op_id = outcome.get("op_id")
        if isinstance(op_id, str) and op_id and state != StepState.OUTCOME_UNKNOWN.value:
            executor.run(
                ajournal_apply(
                    executor._sessions,  # noqa: SLF001 - composed seam
                    uuid.UUID(intent.objective_id),
                    server_id=uuid.UUID(intent.server_id),
                    mutation=JournalReceiptRecorded(
                        operation_id=op_id[:128],
                        outcome=state,
                        effect_class=_descriptor_effect_class(executor, capability_id),
                        server_id=uuid.UUID(intent.server_id),
                    ),
                    actor_kind=ActorKind.AGENT,
                    run_id=uuid.UUID(intent.run_id),
                )
            )
            sections.append("receipt")
        if state == StepState.OUTCOME_UNKNOWN.value or outcome.get("resolved"):
            executor.run(
                ajournal_apply(
                    executor._sessions,  # noqa: SLF001 - composed seam
                    uuid.UUID(intent.objective_id),
                    server_id=uuid.UUID(intent.server_id),
                    mutation=JournalUnknownNoted(
                        effect_ref=_unknown_effect_ref(intent.run_id, seq),
                        server_id=uuid.UUID(intent.server_id),
                        detail=str(outcome.get("reason") or state)[:4096],
                        reconciled=bool(outcome.get("resolved")),
                    ),
                    actor_kind=ActorKind.AGENT,
                    run_id=uuid.UUID(intent.run_id),
                )
            )
            sections.append("unknown")
    except (ObjectiveContractError, ValueError) as error:
        return _degraded(type(error).__name__)
    except Exception as error:  # infrastructure: value-free only
        return _degraded(type(error).__name__)
    return canonical_json({"journaled": True, "sections": sections})


@DBOS.step(name="agents.v2_checklist_apply")
def _v2_checklist_apply(payload_json: str, document_json: str) -> str:
    """Apply the OPTIONAL advisory checklist projection (§8.2; AC6).

    A malformed document (or a rejected write) is bookkeeping trouble: ONE
    honest ``agent.journal_degraded`` observation, nothing erased, the run
    continues. No tick grants authority or completes anything.
    """
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()

    def _degraded(reason: str) -> str:
        try:
            executor.run(
                executor._emit_once(  # noqa: SLF001 - composed seam
                    intent.run_id,
                    AGENT_JOURNAL_DEGRADED_EVENT_TYPE,
                    {"section": "checklist", "reason": reason[:200]},
                )
            )
        except Exception:
            pass
        return canonical_json({"applied": False, "reason": reason[:200]})

    document = json.loads(document_json)
    items = document.get("items") if isinstance(document, Mapping) else None
    if not isinstance(items, list):
        return _degraded("malformed_document")
    try:
        current = executor.run(
            aread_checklist(
                executor._sessions,  # noqa: SLF001 - composed seam
                uuid.UUID(intent.objective_id),
                server_id=uuid.UUID(intent.server_id),
            )
        )
        revision = 0 if current is None else int(current.revision)
        updated = executor.run(
            aput_checklist(
                executor._sessions,  # noqa: SLF001 - composed seam
                uuid.UUID(intent.objective_id),
                server_id=uuid.UUID(intent.server_id),
                items=items,
                expected_revision=revision,
                actor_kind=ActorKind.AGENT,
                run_id=uuid.UUID(intent.run_id),
            )
        )
    except (ObjectiveContractError, ValueError) as error:
        return _degraded(type(error).__name__)
    except Exception as error:  # infrastructure: value-free only
        return _degraded(type(error).__name__)
    return canonical_json({"applied": True, "revision": int(updated.revision)})


def _verdict_evidence_ref(
    intent: AgentRunIntentV2, reference: str
) -> EvidenceRef | None:
    """Resolve one model-provided evidence reference (bounded, honest).

    ``step:<seq>`` and ``op:<uuid>`` references point at THIS run's
    executed records (the caller checked them against the durable rows);
    the returned :class:`EvidenceRef` anchors the run stream, which
    carries the full value-free receipts. Anything else is not evidence.
    """
    note = reference[:200]
    return EvidenceRef(
        kind=EvidenceKind.RUN_EVENT,
        evidence_id=uuid.UUID(intent.run_id),
        note=f"evidence {note}",
    )


@DBOS.step(name="agents.v2_completion_apply")
def _v2_completion_apply(payload_json: str, document_json: str) -> str:
    """Apply the OPTIONAL evidence-bound completion projection (§8.3; AC6).

    The model's ``machina-complete`` block becomes an
    :class:`ObjectiveCompletionVerdict` ONLY with criteria bound to THIS
    run's executed evidence: every ``step:<seq>``/``op:<uuid>`` reference
    must match one of THIS run's SUCCEEDED step rows (durable receipts —
    never prose). A ``verified`` claim without such evidence is rejected
    with the structured ``completion_missing_evidence`` and journaled with
    ``missing_evidence=True``; the objective stays active and the run
    still finishes honestly. Other completion-contention rejections are
    bookkeeping trouble (degraded marker), never evidence inflation.
    """
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()

    def _degraded(reason: str, *, extra: dict[str, object] | None = None) -> str:
        try:
            executor.run(
                executor._emit_once(  # noqa: SLF001 - composed seam
                    intent.run_id,
                    AGENT_JOURNAL_DEGRADED_EVENT_TYPE,
                    {"section": "completion", "reason": reason[:200]},
                )
            )
        except Exception:
            pass
        payload: dict[str, object] = {"completed": False, "reason": reason[:200]}
        if extra:
            payload.update(extra)
        return canonical_json(payload)

    document = json.loads(document_json)
    if not isinstance(document, Mapping):
        return _degraded("malformed_document")

    # The durable evidence set of THIS run: succeeded step rows (receipts
    # are the authority — model prose never inflates evidence, §8.3).
    async def _succeeded_rows() -> list[dict[str, object]]:
        async with executor._sessions() as session:  # noqa: SLF001 - composed seam
            rows = (
                await session.execute(
                    sql_text(
                        "SELECT seq, state, receipt FROM job_steps "
                        "WHERE run_id = :r"
                    ),
                    {"r": uuid.UUID(intent.run_id)},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    try:
        rows = executor.run(_succeeded_rows())
    except Exception as error:
        return _degraded(type(error).__name__)
    succeeded_seqs: set[int] = set()
    succeeded_op_ids: set[str] = set()
    for row in rows or ():
        if str(row.get("state")) != StepState.SUCCEEDED.value:
            continue
        succeeded_seqs.add(int(row["seq"]))
        receipt = row.get("receipt")
        op_id = receipt.get("op_id") if isinstance(receipt, Mapping) else None
        if isinstance(op_id, str) and op_id:
            succeeded_op_ids.add(op_id)

    def _resolve_reference(reference: object) -> EvidenceRef | None:
        if isinstance(reference, str) and reference.startswith("step:"):
            try:
                seq = int(reference[5:])
            except ValueError:
                return None
            return (
                _verdict_evidence_ref(intent, reference)
                if seq in succeeded_seqs
                else None
            )
        if isinstance(reference, str) and reference.startswith("op:"):
            return (
                _verdict_evidence_ref(intent, reference)
                if reference[3:] in succeeded_op_ids
                else None
            )
        return None

    raw_criteria = document.get("criteria")
    criteria: list[CriterionResult] = []
    criterion_evidence: list[EvidenceRef] = []
    if isinstance(raw_criteria, list):
        for entry in raw_criteria[:_COMPLETE_MAX_CRITERIA]:
            if not isinstance(entry, Mapping):
                continue
            refs = [
                resolved
                for reference in (entry.get("evidence") or [])
                if (resolved := _resolve_reference(reference)) is not None
            ]
            criterion_evidence.extend(refs)
            try:
                criteria.append(
                    CriterionResult(
                        criterion_id=str(entry.get("criterion_id") or "criterion"),
                        result=VerificationStatus(str(entry.get("result") or "")),
                        evidence_refs=refs,
                    )
                )
            except ValueError:
                continue

    # The aggregate evidence: the document's explicit references, or —
    # absent those — the union of the criteria's resolved references
    # (still ONLY durable, succeeded records of this run; prose never
    # inflates evidence).
    explicit_refs = [
        ref
        for reference in (document.get("evidence") or [])
        if (ref := _resolve_reference(reference)) is not None
    ]
    aggregate_refs = explicit_refs or list(
        {ref.evidence_id.__str__() + "|" + (ref.note or ""): ref for ref in criterion_evidence}.values()
    )

    detail = executor.run(
        aget_objective(
            executor._sessions,  # noqa: SLF001 - composed seam
            uuid.UUID(intent.objective_id),
            server_id=uuid.UUID(intent.server_id),
        )
    )
    try:
        verdict = ObjectiveCompletionVerdict(
            objective_id=uuid.UUID(intent.objective_id),
            base_revision=int(detail.revision),
            idempotency_key=_completion_idempotency_key(intent.run_id),
            task_outcome=TaskOutcome(str(document.get("task_outcome") or "")),
            execution_status=ExecutionStatus.SUCCEEDED
            if succeeded_seqs
            else ExecutionStatus.FAILED,
            domain_outcome=DomainOutcome(
                str(document.get("domain_outcome") or DomainOutcome.UNKNOWN.value)
            ),
            effect_certainty=EffectCertainty(
                str(document.get("effect_certainty") or EffectCertainty.UNKNOWN.value)
            ),
            error_code=document.get("error_code")
            if isinstance(document.get("error_code"), str)
            else None,
            evidence_refs=aggregate_refs,
            criterion_results=criteria,
        )
    except ValueError:
        return _degraded("malformed_verdict")

    try:
        validate_objective_completion(verdict)
    except ObjectiveContractError:
        # AC6: the unevidenced verified claim is REJECTED with the stable
        # code, journaled honestly (missing_evidence=True) and the
        # objective stays active — never a fabricated completion.
        executor.run(
            ajournal_apply(
                executor._sessions,  # noqa: SLF001 - composed seam
                uuid.UUID(intent.objective_id),
                server_id=uuid.UUID(intent.server_id),
                mutation=JournalVerificationRecorded(
                    verdict=verdict, missing_evidence=True
                ),
                actor_kind=ActorKind.AGENT,
                run_id=uuid.UUID(intent.run_id),
            )
        )
        return canonical_json(
            {
                "completed": False,
                "missing_evidence": True,
                "reason": "completion_missing_evidence",
            }
        )

    try:
        settled = executor.run(
            acomplete_objective(
                executor._sessions,  # noqa: SLF001 - composed seam
                uuid.UUID(intent.objective_id),
                server_id=uuid.UUID(intent.server_id),
                expected_revision=int(detail.revision),
                verdict=verdict,
                actor_kind=ActorKind.AGENT,
                run_id=uuid.UUID(intent.run_id),
            )
        )
    except ObjectiveContractError as error:
        return _degraded(
            error.fields.get("to_status", "completion_rejected"),
        )
    executor.run(
        ajournal_apply(
            executor._sessions,  # noqa: SLF001 - composed seam
            uuid.UUID(intent.objective_id),
            server_id=uuid.UUID(intent.server_id),
            mutation=JournalVerificationRecorded(
                verdict=verdict, missing_evidence=False
            ),
            actor_kind=ActorKind.AGENT,
            run_id=uuid.UUID(intent.run_id),
        )
    )
    return canonical_json(
        {
            "completed": True,
            "missing_evidence": False,
            "status": settled.status.value,
        }
    )


@DBOS.step(name="agents.v2_approval_wait")
def _v2_approval_wait(payload_json: str, call_json: str, approval_id: str) -> str:
    """Enter the durable ``waiting_approval`` park ONCE (§9; AC3).

    ``running → waiting_approval`` (legal edge) with the
    ``agent.awaiting_approval`` event carrying the ABSOLUTE deadline; a
    repeated entry (recovery replay) returns the ORIGINAL deadline —
    budgets never silently extend.
    """
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    call = json.loads(call_json)
    seq = int(call["seq"])
    capability_id = str(
        call.get("capability_id") or call.get("tool_name") or "unknown"
    )
    deadline = executor.run(
        executor.steps.aobserve_enter_wait(
            intent.run_id,
            target=RunState.WAITING_APPROVAL,
            event_type=AGENT_AWAITING_APPROVAL_EVENT_TYPE,
            event_data={
                "approval_id": approval_id,
                "capability_id": capability_id,
                "reason": _APPROVAL_REQUIRED_REASON,
            },
            wait_seconds=intent.approval_wait_seconds,
            step_seq=seq,
        )
    )
    return deadline


@DBOS.step(name="agents.v2_wait_resumed")
def _v2_wait_resumed(payload_json: str, from_state: str) -> str:
    """Record ``<wait> → running`` when a park ends (observe)."""
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    return executor.run(executor.await_wait_resumed(intent.legacy(), from_state))


@DBOS.step(name="agents.v2_approval_execute")
def _v2_approval_execute(
    payload_json: str, call_json: str, approval_id: str
) -> dict[str, object]:
    """The ONE payload-bound approval resume (§9; AC3; C03 rule 4).

    The presented payload is the checkpointed round's exact arguments — the
    object the approval bound by hash. The attempt is its own exactly-once
    guarded step row (``pending`` → fence marker →
    ``CapabilityToolBridge.dispatch_once(approval_id=…)`` → outcome + safe
    receipt), so a recovered attempt observes the row instead of
    re-presenting the approval. Worker-side only; never generic HTTP
    re-execution.
    """
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    call = json.loads(call_json)
    original_seq = int(call["seq"])
    seq = approval_resume_seq(original_seq)
    capability_id = str(
        call.get("capability_id") or call.get("tool_name") or "unknown"
    )
    payload = call.get("payload")
    spec = _tool_step_intent(
        seq, capability_id, {**call, "payload": payload if isinstance(payload, Mapping) else {}}
    )
    row = executor.run(executor.steps.aensure_step_intent(intent.run_id, spec))
    state = str(row["state"])
    if state == StepState.RUNNING.value:
        # A prior attempt died holding the fence marker: the crash-window
        # translation, never a blind re-presentation of the approval.
        outcome = executor.run(
            executor.steps.acrash_window_outcome(
                intent.run_id, intent.server_id, seq, spec
            )
        )
        return {
            "seq": original_seq,
            "resume_seq": seq,
            "state": str(outcome["state"]),
            "reason": str(outcome.get("reason") or CRASH_RECOVERY_RESULT_REASON),
            "op_id": outcome.get("op_id"),
            "result_text": _receipt_result_document(
                intent.legacy(),
                str(outcome["state"]),
                outcome.get("receipt") if isinstance(outcome.get("receipt"), Mapping) else {},
                resolved=False,
            ),
        }
    if state != StepState.PENDING.value:
        receipt = row["receipt"] if isinstance(row["receipt"], Mapping) else {}
        resolved = isinstance(receipt, Mapping) and bool(receipt.get("resolution"))
        return {
            "seq": original_seq,
            "resume_seq": seq,
            "state": state,
            "reason": receipt.get("reason"),
            "op_id": receipt.get("op_id"),
            "result_text": _receipt_result_document(
                intent.legacy(), state, receipt, resolved=bool(resolved)
            ),
        }

    executor.run(executor.steps.amark_running(intent.run_id, seq, capability_id))
    bridge = CapabilityToolBridge(
        registry=executor._registry,  # noqa: SLF001 - composed seam (mirrors _bridge_of)
        dispatcher=executor.steps._dispatcher,  # noqa: SLF001 - composed seam
        context=intent.context,
    )
    try:
        outcome = executor.run(
            bridge.dispatch_once(
                capability_id,
                dict(payload) if isinstance(payload, Mapping) else {},
                approval_id=uuid.UUID(approval_id),
            )
        )
    except BridgeError as error:
        return _approval_denial(
            intent, executor, call, seq, capability_id,
            reason=f"bridge_{error.code.value}", detail=error.detail,
        )
    except ApprovalError as error:
        return _approval_denial(
            intent, executor, call, seq, capability_id,
            reason=f"approval_{error.code.value}", detail=error.detail,
        )
    except Exception as error:  # infrastructure before the effect
        return _approval_denial(
            intent, executor, call, seq, capability_id,
            reason="dispatch_infra_failed", detail=type(error).__name__,
        )

    state_value, reason, receipt = _translate_outcome(outcome)
    if state_value == WAIT_FOR_APPROVAL:  # a bound approval cannot re-wait
        state_value = StepState.FAILED.value
        reason = str(outcome.reason)
    resolved_values = _resolved_credential_values(
        executor.steps, outcome.credential_references
    )
    if outcome.output is not None and outcome.output.text:
        receipt["response"] = project_document(outcome.output.text, resolved_values)
    if isinstance(payload, Mapping):
        receipt["params"] = _scrubbed_step_params(
            capability_id, dict(payload), resolved_values
        )
    step_state = StepState(state_value)
    executor.run(
        executor.steps.apersist_outcome(
            intent.run_id,
            seq,
            capability_id,
            step_state,
            receipt,
            reason=reason,
            op_id=str(outcome.op_id),
        )
    )
    executor.run(
        executor._aemit_tool_observation(  # noqa: SLF001 - composed seam
            intent.legacy(),
            AGENT_TOOL_RESULT_EVENT_TYPE,
            {**call, "seq": seq},
            {**receipt, "state": step_state.value, "reason": reason},
        )
    )
    result_text = _result_document(
        intent=intent.legacy(),
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
        "seq": original_seq,
        "resume_seq": seq,
        "state": step_state.value,
        "reason": reason,
        "op_id": str(outcome.op_id),
        "result_text": result_text,
    }


def _approval_denial(
    intent: AgentRunIntentV2,
    executor: Any,
    call: Mapping[str, object],
    seq: int,
    capability_id: str,
    *,
    reason: str,
    detail: str,
) -> dict[str, object]:
    """Persist a proven no-effect resume denial (``running → failed``)."""
    payload = call.get("payload")
    try:
        payload_hash = canonical_payload_hash(
            payload if isinstance(payload, Mapping) else {}
        )
    except DispatchError:
        payload_hash = "non_canonical"
    receipt: dict[str, object] = {
        "v": 1,
        "op_id": None,
        "capability_id": capability_id,
        "allowed": False,
        "reason": reason,
        "effect": "not_attempted",
        "payload_hash": payload_hash,
        "error_type": detail,
        "tool_call_id": str(call.get("tool_call_id")),
    }
    executor.run(
        executor.steps.apersist_outcome(
            intent.run_id,
            seq,
            capability_id,
            StepState.FAILED,
            receipt,
            reason=reason,
            op_id=None,
        )
    )
    return {
        "seq": int(call["seq"]),
        "resume_seq": seq,
        "state": StepState.FAILED.value,
        "reason": reason,
        "op_id": None,
        "result_text": _result_document(
            intent=intent.legacy(),
            capability_id=capability_id,
            status=_status_of_state(StepState.FAILED),
            reason=reason,
            payload_hash=payload_hash,
            effect="not_attempted",
        ),
    }


@DBOS.step(name="agents.v2_read_step")
def _v2_read_step(payload_json: str, seq: int) -> dict[str, object] | None:
    """Poll one tool-call row for a reconciliation resolution."""
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    return executor.read_step(intent.legacy(), seq)


@DBOS.step(name="agents.v2_reconciling")
def _v2_reconciling(payload_json: str, call_json: str, outcome_json: str) -> str:
    """Record ``reconciling`` + the wait event; return the deadline."""
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    return executor.run(
        executor.await_reconciliation(
            intent.legacy(), json.loads(call_json), json.loads(outcome_json)
        )
    )


@DBOS.step(name="agents.v2_reconcile_expired")
def _v2_reconcile_expired(payload_json: str, call_json: str) -> str:
    """Expire one unknown-effect step at the deadline (checkpointed)."""
    intent = parse_agent_run_intent_v2(payload_json)
    call = json.loads(call_json)
    executor = agent_run._resolve_agent_run()
    return canonical_json(
        executor.run(
            executor.aexpire_unknown_step(intent.run_id, int(call["seq"]))
        )
    )


@DBOS.step(name="agents.v2_run_finished")
def _v2_run_finished(payload_json: str, summary_json: str) -> str:
    """Record ``running → succeeded`` + the safe ``agent.completed`` event."""
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    return executor.run(
        executor.afinish_run(intent.legacy(), json.loads(summary_json))
    )


@DBOS.step(name="agents.v2_run_failed")
def _v2_run_failed(payload_json: str, reason: str) -> str:
    """Record the run ``failed`` + the classified reason event."""
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    result = executor.fail_run(intent.legacy(), reason)
    executor.run(executor.aemit_failure_event(intent.run_id, reason))
    return result


# ---------------------------------------------------------------------------
# durable wait resolution (approval / unknown-effect) — workflow level
# ---------------------------------------------------------------------------


async def _settle_approval(
    payload_json: str,
    call_json: str,
    call: Mapping[str, object],
    outcome: Mapping[str, object],
) -> dict[str, object]:
    """Drive one approval-deferred call to a resumed outcome (workflow).

    Parks the durable workflow on ``DBOS.recv`` polls against the persisted
    deadline (the approval wait SURVIVES restart: a recovered workflow
    re-parks and the ORIGINAL deadline is re-derived from the persisted
    event). The decision rides the durable resume message; an approval
    presents the ONE payload-bound operation through
    ``dispatch_once(approval_id=…)`` (a NEW exactly-once step row — the
    refused attempt stays terminal history). A denial/expiry resumes and
    answers honestly; expiry fails the run (``waiting_approval → running →
    failed``, both legal edges).
    """
    intent = parse_agent_run_intent_v2(payload_json)
    executor = agent_run._resolve_agent_run()
    seq = int(call["seq"])
    row = executor.run(executor.steps.aread_step(intent.run_id, seq))
    receipt = row.get("receipt") if isinstance(row, Mapping) else {}
    approval_id = (
        str(receipt.get("approval_id"))
        if isinstance(receipt, Mapping) and receipt.get("approval_id")
        else None
    )
    if approval_id is None:
        # The durable approval binding never landed (creation failed): the
        # honest refusal already recorded stands; no park, no invented wait.
        return dict(outcome)
    deadline = _deadline_of(
        _v2_approval_wait(payload_json, call_json, approval_id)
    )
    while True:
        message = await DBOS.recv_async(
            topic=RESUME_TOPIC,
            timeout_seconds=min(
                intent.wait_poll_seconds,
                max(MIN_RECV_TIMEOUT_SECONDS, (deadline - _utcnow()).total_seconds()),
            ),
        )
        remaining = (deadline - _utcnow()).total_seconds()
        decision: str | None = None
        if isinstance(message, Mapping):
            value = message.get("decision")
            if isinstance(value, str):
                decision = value
        if decision == "deny":
            _v2_wait_resumed(payload_json, RunState.WAITING_APPROVAL.value)
            denial = dict(outcome)
            denial["reason"] = "approval_denied"
            denial["result_text"] = _result_document(
                intent=intent.legacy(),
                capability_id=str(
                    call.get("capability_id") or call.get("tool_name") or "unknown"
                ),
                status=_status_of_state(StepState.FAILED),
                reason="approval_denied",
                payload_hash=None,
                effect="not_attempted",
            )
            return denial
        if decision == "approve":
            _v2_wait_resumed(payload_json, RunState.WAITING_APPROVAL.value)
            return dict(
                await asyncio.to_thread(
                    _v2_approval_execute, payload_json, call_json, approval_id
                )
            )
        if remaining <= 0:
            # Budget expiry: resume, then fail the run classified — never
            # a silent drop of the deferred operation.
            _v2_wait_resumed(payload_json, RunState.WAITING_APPROVAL.value)
            raise AgentStageError(
                APPROVAL_WAIT_DEADLINE_REASON, error_type="ApprovalWaitDeadline"
            )
        # A wake without a decision (or an unknown message kind) is a
        # durable re-check trigger only: keep waiting within the budget.


async def _settle_unknown_v2(
    payload_json: str,
    call_json: str,
    call: Mapping[str, object],
    outcome: Mapping[str, object],
) -> dict[str, object]:
    """Drive one unknown-effect call to a resolved outcome (workflow).

    Mirror of the reviewed legacy settlement: the durable workflow parks on
    ``DBOS.recv`` polls against the persisted deadline; the durable step
    row (a reconciler-resolved terminal state) decides — never a retry and
    never an invented success. On resolution the receipt is journaled and
    the unknown note flips to ``reconciled`` (§8.1: unknowns stay explicit).
    """
    intent = parse_agent_run_intent_v2(payload_json)
    deadline = _deadline_of(
        _v2_reconciling(payload_json, call_json, canonical_json(dict(outcome)))
    )
    reconcile_attempts = 0
    while True:
        row = _v2_read_step(payload_json, int(call["seq"]))
        if row is not None and str(row["state"]) in {
            item.value for item in TERMINAL_STEP_STATES
        }:
            _v2_wait_resumed(payload_json, RunState.RECONCILING.value)
            state = str(row["state"])
            if state == StepState.CANCELLED.value:
                _record_quietly(intent.legacy(), "cancel")
                raise AgentRunFailed("step_cancelled")
            receipt = row["receipt"] if isinstance(row["receipt"], Mapping) else {}
            resolved_outcome = {
                "seq": int(call["seq"]),
                "state": state,
                "reason": receipt.get("reason"),
                "op_id": receipt.get("op_id"),
                "resolved": True,
                "result_text": _receipt_result_document(
                    intent.legacy(), state, receipt, resolved=True
                ),
            }
            _v2_journal_tool(
                payload_json, call_json, canonical_json(resolved_outcome)
            )
            return resolved_outcome
        remaining = (deadline - _utcnow()).total_seconds()
        if remaining <= 0:
            _v2_wait_resumed(payload_json, RunState.RECONCILING.value)
            settled = json.loads(
                _v2_reconcile_expired(payload_json, call_json)
            )
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
                    _record_quietly(intent.legacy(), "cancel")
                    raise AgentRunFailed("step_cancelled")
                resolved_outcome = {
                    "seq": int(call["seq"]),
                    "state": row_state,
                    "reason": receipt.get("reason"),
                    "op_id": receipt.get("op_id"),
                    "resolved": True,
                    "result_text": _receipt_result_document(
                        intent.legacy(), row_state, receipt, resolved=True
                    ),
                }
                _v2_journal_tool(
                    payload_json, call_json, canonical_json(resolved_outcome)
                )
                return resolved_outcome
            expired_outcome = {
                "seq": int(call["seq"]),
                "state": StepState.FAILED.value,
                "reason": EXECUTION_TIMEOUT_REASON,
                "op_id": receipt.get("op_id"),
                "resolved": True,
                "result_text": _timeout_result_document(intent.legacy(), receipt),
            }
            _v2_journal_tool(
                payload_json, call_json, canonical_json(expired_outcome)
            )
            return expired_outcome
        await DBOS.recv_async(
            topic=RESUME_TOPIC,
            timeout_seconds=min(
                intent.wait_poll_seconds,
                max(MIN_RECV_TIMEOUT_SECONDS, remaining),
            ),
        )
        reconcile_attempts += 1
        if reconcile_attempts % 6 == 0:
            executor_ref = agent_run._resolve_agent_run()
            try:
                resolved = executor_ref.run(
                    executor_ref.areconcile_step(
                        intent.run_id, int(call["seq"])
                    )
                )
                if resolved:
                    continue
            except Exception:
                pass


def _is_approval_wait_outcome(outcome: Mapping[str, object]) -> bool:
    """True when one tool outcome is the durable approval wait (§9)."""
    return (
        str(outcome.get("state")) == StepState.FAILED.value
        and str(outcome.get("reason") or "") == _APPROVAL_REQUIRED_REASON
    )


# ---------------------------------------------------------------------------
# the ONE registered v2 handler
# ---------------------------------------------------------------------------


async def run_agent_conversation_v2(payload_json: str) -> dict[str, object]:
    """Execute one durable v2 working-agent conversation (see module doc).

    Registered as :data:`HANDLER_NAME` (module import) and driven on the
    engine's persistent async-handler loop by the durable trampoline
    workflow. The FIRST model call is the working agent: no intake planner,
    no intake events, no mandatory todo. Classified stage failures record
    ``failed`` and raise :class:`AgentRunFailed` (engine ERROR — both sides
    agree); cancellation re-raises the engine's own exception after
    recording ``cancelled``; integrity rejections from the steps machinery
    propagate as-is. Success returns the safe conversation summary.
    """
    intent = parse_agent_run_intent_v2(payload_json)
    try:
        _v2_run_started(payload_json)
        pins = _v2_model_bound(payload_json)
        pins_json = canonical_json(pins)
        # §3.3: the durable objective binding BEFORE any model round (the
        # checkpoint replays it; the seam is idempotent under recovery).
        _v2_objective_bound(payload_json)
        assembled = _v2_context_assembled(payload_json, pins_json)
        objective_block_json = _v2_objective_context(payload_json)
        seed = _v2_conversation_seed(
            payload_json, canonical_json(assembled), objective_block_json
        )
        history_json = str(seed["history_json"])
        deferred_json: str | None = None
        pending_steered: list[str] = []
        stopped_by: str | None = None
        round_record: dict[str, object] = {}
        rounds = 0
        tool_records: list[dict[str, object]] = []
        output_chars = 0
        completion: dict[str, object] | None = None
        while True:
            # Safe boundary BEFORE every model round: the durable
            # steering queue is consumed exactly once here, and a
            # deterministic pause/cancel stops the attempt without a
            # further model call or effect.
            if rounds > 0:
                pending_steered.extend(_drain_steering_messages())
            boundary_texts, stop = _claim_steering_boundary_v2(payload_json)
            pending_steered.extend(boundary_texts)
            if stop is not None:
                stopped_by = stop
                break
            steered_json = (
                canonical_json(pending_steered) if pending_steered else None
            )
            pending_steered = []
            round_record = await asyncio.to_thread(
                _v2_model_round,
                payload_json,
                pins_json,
                history_json,
                deferred_json,
                rounds,
                steered_json,
                None,
            )
            history_json = str(round_record["history_json"])
            if str(round_record["status"]) == "final":
                output_chars = int(str(round_record["output_chars"]))
                late_steered = _drain_steering_messages()
                boundary_texts, stop = _claim_steering_boundary_v2(payload_json)
                if stop is not None:
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
            executor = agent_run._resolve_agent_run()
            existing_rows = int(
                executor.run(executor.tool_row_count(intent.legacy()))
            )
            if existing_rows + len(calls) > AGENT_TOOL_MAX_PER_RUN:
                raise AgentStageError(TOOL_BUDGET_REASON)
            results: dict[str, str] = {}
            steering_interrupt = False
            for call in calls:
                # Safe boundary BEFORE EACH NEW tool effect: steering or a
                # deterministic stop gates the next command; already-settled
                # calls keep their honest recorded outcomes either way.
                boundary_texts, stop = _claim_steering_boundary_v2(payload_json)
                if stop is not None:
                    stopped_by = stop
                    break
                if boundary_texts:
                    pending_steered.extend(boundary_texts)
                    steering_interrupt = True
                    break
                call_json = canonical_json(call)
                outcome = await asyncio.to_thread(
                    _v2_tool_execute, payload_json, call_json
                )
                if str(outcome["state"]) == StepState.OUTCOME_UNKNOWN.value:
                    # The unproven effect is journaled FIRST (the unknown is
                    # never hidden), then the durable reconciliation park.
                    _v2_journal_tool(
                        payload_json, call_json, canonical_json(outcome)
                    )
                    outcome = await _settle_unknown_v2(
                        payload_json, call_json, call, outcome
                    )
                elif _is_approval_wait_outcome(outcome):
                    # The refused pre-approval attempt stays terminal
                    # history; the durable park decides what happens next.
                    _v2_journal_tool(
                        payload_json, call_json, canonical_json(outcome)
                    )
                    outcome = await _settle_approval(
                        payload_json, call_json, call, outcome
                    )
                _v2_journal_tool(
                    payload_json, call_json, canonical_json(outcome)
                )
                results[str(call["tool_call_id"])] = str(outcome["result_text"])
                tool_records.append(
                    {
                        "seq": outcome.get("resume_seq", outcome["seq"]),
                        "state": outcome["state"],
                        "reason": outcome.get("reason"),
                    }
                )
            if stopped_by is not None:
                break
            if steering_interrupt:
                # The model must SEE the steering before any further
                # effect: the not-yet-dispatched deferred calls carry
                # explicit not_executed documents (wire honesty) and the
                # next round re-decides under the injected UserPromptPart.
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
        final_output = str(round_record.get("output") or "")
        recorded = _v2_assistant_recorded(payload_json, final_output)
        # §8.2/§8.3: the OPTIONAL advisory projections of the final output.
        # Both are checkpointed, both degrade to honest markers, and NEITHER
        # can reject the conversation or erase recorded effects (AC6).
        checklist_document, complete_document, projection_trouble = (
            _extract_projection_blocks(final_output)
        )
        if projection_trouble is not None:
            executor_ref = agent_run._resolve_agent_run()
            executor_ref.run(
                executor_ref._emit_once(  # noqa: SLF001 - composed seam
                    intent.run_id,
                    AGENT_JOURNAL_DEGRADED_EVENT_TYPE,
                    {"section": "projection", "reason": projection_trouble},
                )
            )
        if checklist_document is not None:
            _v2_checklist_apply(
                payload_json, canonical_json(checklist_document)
            )
        if complete_document is not None:
            completion = json.loads(
                _v2_completion_apply(
                    payload_json, canonical_json(complete_document)
                )
            )
        summary = {
            "rounds": rounds + 1 if round_record else 0,
            "tool_calls": tool_records,
            "output_chars": output_chars,
            "assistant_recorded": bool(recorded.get("recorded")),
            "assistant_message_id": recorded.get("message_id"),
        }
        objective_summary: dict[str, object] = {"objective_id": intent.objective_id}
        if completion is not None:
            objective_summary["completion"] = completion
        summary["objective"] = objective_summary
        if stopped_by is not None:
            summary[STOPPED_BY_EVENT_KEY] = stopped_by
        _v2_run_finished(payload_json, canonical_json(summary))
        return {"run_id": intent.run_id, "state": "succeeded", **summary}
    except AgentStageError as stage:
        _v2_run_failed(payload_json, stage.reason)
        _release_lease_quietly(intent.legacy())
        raise AgentRunFailed(stage.reason) from None
    except dbos_error.StepsError:
        _record_quietly(intent.legacy(), "fail")
        _release_lease_quietly(intent.legacy())
        raise
    except dbos_error.DBOSWorkflowCancelledError:
        _record_quietly(intent.legacy(), "cancel")
        _release_lease_quietly(intent.legacy())
        raise
    except AgentRunError:
        _record_quietly(intent.legacy(), "fail")
        _release_lease_quietly(intent.legacy())
        raise
    except Exception:
        # An unexpected failure (a defect, not a classified stage): the
        # engine lands ERROR either way — record the honest facade
        # translation so both sides agree, then let the original
        # exception surface as-is.
        _record_quietly(intent.legacy(), "fail")
        _release_lease_quietly(intent.legacy())
        raise


# Registration at import (the engine trampoline resolves the handler by
# name; worker entrypoints import this module via MAI_WORKER_HANDLERS —
# runtime-v2.md §2: the ONE registration seam, no second engine).
_engine.register_job_handler(HANDLER_NAME, run_agent_conversation_v2)


__all__ = [
    "AGENT_AWAITING_APPROVAL_EVENT_TYPE",
    "AGENT_JOURNAL_DEGRADED_EVENT_TYPE",
    "DEFAULT_APPROVAL_WAIT_SECONDS",
    "HANDLER_NAME",
    "MAX_APPROVAL_WAIT_SECONDS",
    "RUN_PAYLOAD_VERSION_V2",
    "AgentRunIntentV2",
    "approval_resume_seq",
    "parse_agent_run_intent_v2",
    "run_agent_conversation_v2",
]
