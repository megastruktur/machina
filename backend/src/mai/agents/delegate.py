"""Delegated child runs: yielding agent parents over the one job engine.

Card ``06-child-runs`` against contracts.md **C05** (rules 2, 3, 5, 7: the
frozen ``waiting_children`` lifecycle edges, ``parent_run_id``/``depth``
linkage, deterministic workflow IDs, child waits that never deadlock worker
slots) and **C06** rule 5 (the dispatcher constructs the child task from
explicit scope/context — never from a privileged serialized agent object;
checkpointed engine child edges) plus the GRANT-INTERSECT scenario
(``docs/architecture/runtime-scenarios.md``: child grants are the
intersection with the parent, rechecked immediately before any privileged
effect). This module is the delegation producer over the reviewed
``06-agent-run`` surface; it owns:

- **The delegating-parent intent** — a strict, value-free envelope
  (``parse_delegation_intent``): the nested, frozen agent-run payload for
  the parent (validated by the ONE producer, :func:`mai.agents.run.
  parse_agent_run_intent` — never re-implemented) plus the explicit child
  task specifications and the parent's bounded children-wait budgets.
- **Child plan construction** (pure, :func:`plan_children`) — for every
  child: a fresh canonical run identity, the PARENT's server (a child
  cannot reassign its target: the request vocabulary has no server field
  to express one), an :class:`~mai.policy.evaluate.ExecutionContext`
  whose ``parent_scope`` names the parent run + server and whose
  ``allowed_capabilities`` are the INTERSECTION of the parent's granted
  set with the child's explicit request (omitted request ⇒ no
  capabilities; a request can never ADD anything the parent lacks —
  clamped, never unioned), and a child payload that re-validates through
  the agent-run producer before anything persists.
- **The durable delegation loop** — the registered ``agent.delegate``
  handler executes inside the engine trampoline: child ``job_runs`` rows
  are inserted with ``parent_run_id`` + ``depth`` linkage (the facade row
  precedes every child workflow, so a recovered child always finds its
  row), children start as ``DBOS.start_workflow`` children of THIS
  workflow with their deterministic run ID (checkpointed engine child
  edges; ``SetWorkflowID`` + reject policy), the parent parks
  ``waiting_children`` with a persisted absolute deadline and then polls
  child terminality through checkpointed sleeps — children are NOT queue
  entries, so a yielding parent never holds worker capacity hostage
  (C05 rule 7). Each terminal child state is read from the engine truth
  (never invented); the wake records ``waiting_children → running`` and
  the safe ``agent.children_resolved`` event.

Cancellation (guard rules 3–4): the facade's explicit tree cancellation
flows through the engine child edges (``cancel_children=True`` cancels
every delegation child natively). The handler's own defensive belt: when
the cancellation exception surfaces inside the parent workflow (at a
checkpoint boundary), the parent requests cancellation for every child
(with their subtrees) before re-raising — a cancelled parent never
orphanes its children. Deadline expiry fails the parent honestly
(``children_wait_deadline``) and stops the tree the same way.

Deliberate boundaries (recorded limitations, card evidence): the parent
handler delegates and wakes — it does not continue its own conversation
after the wake (the model-requested mid-conversation delegation tool is
assembled by the chat/delegation surface on this primitive, C06 rule 5);
the aggregate budget vocabulary is :mod:`mai.agents.budgets` — this
handler applies the budget-narrowed plan gates and drives the root
ledger's reserve/charge/release at its plan/start/settle points (the
per-model token attribution stays the agent-run surface's recorded
boundary; the delegation charges producer-reported durable rounds);
children carry no mutation-lock
leases (the declared-steps surface owns lock-fenced effects, sibling to
the agent-run card's recorded boundary). Child terminal states are facts
of the child's own event stream; the parent records them value-free.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from dbos import DBOS, SetWorkflowID
from dbos import error as dbos_error
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents import run as _run
from mai.agents.run import (
    AgentRunError,
    AgentRunIntent,
    AgentStageError,
    canonical_json,
    parse_agent_run_intent,
)
from mai.contracts.jobs import TERMINAL_RUN_STATES, RunState, run_state_from_engine
from mai.jobs import engine as _engine
from mai.jobs.engine import EngineClient
from mai.jobs.steps import StepsError
from mai.policy.evaluate import ExecutionContext, ParentScope, PolicyError

if TYPE_CHECKING:
    # Type-only: the budgets module imports this module's bounds, so the
    # runtime name is resolved lazily (see ``_budget_vocabulary``).
    from mai.agents.budgets import BudgetLedger

__all__ = [
    "CHILDREN_WAIT_DEADLINE_REASON",
    "CHILDREN_RESOLVED_EVENT_TYPE",
    "DELEGATION_COMPLETED_EVENT_TYPE",
    "DELEGATION_PLANNED_EVENT_TYPE",
    "DELEGATION_WAIT_EVENT_TYPE",
    "DELEGATION_PAYLOAD_VERSION",
    "DEFAULT_CHILDREN_POLL_SECONDS",
    "DEFAULT_CHILDREN_WAIT_SECONDS",
    "HANDLER_NAME",
    "MAX_CHILDREN_PER_RUN",
    "MAX_CHILDREN_POLL_SECONDS",
    "MAX_CHILDREN_WAIT_SECONDS",
    "MAX_DELEGATION_DEPTH",
    "ChildPlan",
    "ChildSpec",
    "DelegationError",
    "DelegationErrorCode",
    "DelegationExecutor",
    "DelegationIntent",
    "DelegationParentFailed",
    "DelegationPlan",
    "ParentRunRow",
    "configure_delegation",
    "parse_delegation_intent",
    "plan_children",
    "reset_delegation",
    "run_delegating_parent",
]

# ---------------------------------------------------------------------------
# frozen vocabulary + bounds
# ---------------------------------------------------------------------------

#: The run-handler name this module registers (dotted slug; the engine
#: trampoline resolves it at execution time). A parent run claimed under
#: this handler delegates and yields; its children execute under
#: :data:`mai.agents.run.HANDLER_NAME`.
HANDLER_NAME: Final[str] = "agent.delegate"

#: Payload document version produced by this freeze.
DELEGATION_PAYLOAD_VERSION: Final[int] = 1

#: Explicit delegation bounds (C05 rule 7: explicit, application-level;
#: the full budget-ledger vocabulary is owned by ``06-delegation-budgets``
#: and may only narrow these). A parent may start at most this many
#: children per delegation and only below this tree depth.
MAX_CHILDREN_PER_RUN: Final[int] = 8
MAX_DELEGATION_DEPTH: Final[int] = 4

#: The parent's bounded children-wait budgets (mirroring the agent run's
#: reconciliation-wait shape; the deadline is persisted on the wait event).
DEFAULT_CHILDREN_WAIT_SECONDS: Final[float] = 300.0
MAX_CHILDREN_WAIT_SECONDS: Final[float] = 3600.0
DEFAULT_CHILDREN_POLL_SECONDS: Final[float] = 5.0
MAX_CHILDREN_POLL_SECONDS: Final[float] = 300.0
MIN_CHILDREN_POLL_SECONDS: Final[float] = 0.5

#: Stable failure reason (value-free; envelope-style).
CHILDREN_WAIT_DEADLINE_REASON: Final[str] = "children_wait_deadline"

#: Event types this module owns in the shared per-run stream (C05 rule 4:
#: frozen envelope shape, per-producer type vocabulary; payloads are
#: value-free — child identities and states, never task content).
DELEGATION_PLANNED_EVENT_TYPE: Final[str] = "agent.delegation_planned"
DELEGATION_WAIT_EVENT_TYPE: Final[str] = "agent.delegation_wait"
CHILDREN_RESOLVED_EVENT_TYPE: Final[str] = "agent.children_resolved"
DELEGATION_COMPLETED_EVENT_TYPE: Final[str] = "agent.delegation_completed"

#: Capability-name bound for an explicit child request (references only).
MAX_CAPABILITY_NAME_CHARS: Final[int] = 200

#: The engine reuse policy for child workflow IDs (guard rule 5): a child
#: run ID already bound to an existing engine workflow is a hard failure,
#: never a silent attach. Parent-recovery replays never reach this check
#: (the recorded child edge short-circuits the start).
_CHILD_WORKFLOW_ID_REUSE_POLICY: Final[str] = "reject"


class DelegationErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_PAYLOAD = "invalid_payload"
    INVALID_WIRING = "invalid_wiring"
    EXECUTOR_NOT_CONFIGURED = "executor_not_configured"
    ALREADY_CONFIGURED = "already_configured"
    RUN_NOT_FOUND = "run_not_found"
    INVALID_RUN_STATE = "invalid_run_state"
    DELEGATION_DEPTH = "delegation_depth_exhausted"
    CHILD_UNKNOWN = "child_run_unknown"


class DelegationError(ValueError):
    """One structured, value-free delegation rejection (fail closed)."""

    def __init__(self, code: DelegationErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class DelegationParentFailed(RuntimeError):
    """Raised AFTER the parent was recorded ``failed`` (engine ERROR)."""


# ---------------------------------------------------------------------------
# intent (strict, value-free; the child scope is explicit or absent)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChildSpec:
    """One explicitly scoped child task (C06 rule 5: no arbitrary scope).

    ``capabilities`` is the child's REQUEST; the effective child grants
    are its intersection with the parent's granted set (GRANT-INTERSECT).
    There is deliberately no server/target field: a child runs on its
    parent's server, full stop.
    """

    task: str
    role: str | None
    connection: str | None
    profile: str | None
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DelegationIntent:
    """The validated payload for :data:`HANDLER_NAME`.

    ``run`` is the parent's frozen agent-run intent (the ONE producer
    validated it, identity binding included); ``children`` are the
    explicit child task specifications; the wait budgets bound how long
    the yielding parent may park on ``waiting_children``.
    """

    run: AgentRunIntent
    children: tuple[ChildSpec, ...]
    children_wait_seconds: float
    children_poll_seconds: float


@dataclass(frozen=True, slots=True)
class ParentRunRow:
    """The linkage facts the facade row holds for the parent (C05 rule 3)."""

    run_id: str
    server_id: str
    depth: int
    profile_revision: int
    prompt_revision: int
    model_profile: str
    root_budget: Mapping[str, object] | None


def _reject_payload(detail: str) -> DelegationError:
    return DelegationError(DelegationErrorCode.INVALID_PAYLOAD, detail)


def _budget_vocabulary():
    """The aggregate budget vocabulary, imported at call time.

    :mod:`mai.agents.budgets` imports THIS module's delegation bounds
    (a budget may only narrow them), so a module-level import would
    cycle; both modules are fully loaded by the time any delegation
    plans or runs.
    """
    from mai.agents import budgets

    return budgets


def _bounded_wait(
    value: object,
    field: str,
    default: float,
    maximum: float,
) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _reject_payload(f"{field} must be a number of seconds")
    number = float(value)
    if not 0 < number <= maximum:
        raise _reject_payload(f"{field} outside its bounded range")
    return number


def _parse_child_spec(raw: object, index: int) -> ChildSpec:
    if not isinstance(raw, Mapping):
        raise _reject_payload(f"child {index} must be a JSON object")
    allowed = {"task", "role", "connection", "profile", "capabilities"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise _reject_payload(f"child {index} has unknown field {unknown[0]!r}")
    if "task" not in raw:
        raise _reject_payload(f"child {index} is missing its task")
    task = raw["task"]
    if not isinstance(task, str) or not task.strip() or len(task) > _run.MAX_TASK_CHARS:
        raise _reject_payload(
            f"child {index} task must be a non-empty string of at most "
            f"{_run.MAX_TASK_CHARS} characters"
        )
    for field in ("role", "connection", "profile"):
        value = raw.get(field)
        if value is None:
            continue
        if field == "role":
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > _run.MAX_ROLE_LEN
                or "\n" in value
            ):
                raise _reject_payload(
                    f"child {index} role must be a single line of 1.."
                    f"{_run.MAX_ROLE_LEN} characters"
                )
            continue
        if not isinstance(value, str) or not value or len(value) > 200:
            raise _reject_payload(
                f"child {index} {field} must be a non-empty name of at "
                "most 200 characters"
            )
    capabilities_raw = raw.get("capabilities", ())
    if not isinstance(capabilities_raw, (list, tuple)):
        raise _reject_payload(f"child {index} capabilities must be an array")
    capabilities: list[str] = []
    for capability in capabilities_raw:
        if not isinstance(capability, str) or not capability.strip():
            raise _reject_payload(
                f"child {index} capabilities must be non-empty strings"
            )
        if len(capability) > MAX_CAPABILITY_NAME_CHARS:
            raise _reject_payload(
                f"child {index} capability names are bounded at "
                f"{MAX_CAPABILITY_NAME_CHARS} characters"
            )
        capabilities.append(capability)
    return ChildSpec(
        task=task,
        role=raw.get("role"),
        connection=raw.get("connection"),
        profile=raw.get("profile"),
        capabilities=tuple(capabilities),
    )


def parse_delegation_intent(payload_json: str) -> DelegationIntent:
    """Validate one delegating-parent payload; return it typed (fail closed).

    Strict shape: exactly the frozen field set (wait budgets optional);
    ``v`` is this freeze's version; ``run`` is validated by the ONE
    agent-run producer (identity binding: the embedded context names THIS
    run and THIS server); ``children`` is a non-empty bounded array of
    explicit child specifications.
    """
    if not isinstance(payload_json, str):
        raise _reject_payload("delegation payload must be a JSON string")
    try:
        document = json.loads(payload_json)
    except ValueError:
        raise _reject_payload("delegation payload is not valid JSON") from None
    if not isinstance(document, Mapping):
        raise _reject_payload("delegation payload must be a JSON object")
    allowed = {
        "v",
        "run",
        "children",
        "children_wait_seconds",
        "children_poll_seconds",
    }
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise _reject_payload(f"unknown field {unknown[0]!r}")
    missing = {"v", "run", "children"} - set(document)
    if missing:
        raise _reject_payload(f"missing field {sorted(missing)[0]!r}")
    if document["v"] != DELEGATION_PAYLOAD_VERSION:
        raise _reject_payload(f"v must be {DELEGATION_PAYLOAD_VERSION}")
    try:
        parent = parse_agent_run_intent(canonical_json(document["run"]))
    except AgentRunError:
        raise _reject_payload("run is not a valid agent-run payload") from None
    children_raw = document["children"]
    if not isinstance(children_raw, (list, tuple)):
        raise _reject_payload("children must be an array")
    if not 1 <= len(children_raw) <= MAX_CHILDREN_PER_RUN:
        raise _reject_payload(
            f"children must hold 1..{MAX_CHILDREN_PER_RUN} specifications"
        )
    children = tuple(
        _parse_child_spec(raw, index) for index, raw in enumerate(children_raw)
    )
    return DelegationIntent(
        run=parent,
        children=children,
        children_wait_seconds=_bounded_wait(
            document.get("children_wait_seconds"),
            "children_wait_seconds",
            DEFAULT_CHILDREN_WAIT_SECONDS,
            MAX_CHILDREN_WAIT_SECONDS,
        ),
        children_poll_seconds=_bounded_wait(
            document.get("children_poll_seconds"),
            "children_poll_seconds",
            DEFAULT_CHILDREN_POLL_SECONDS,
            MAX_CHILDREN_POLL_SECONDS,
        ),
    )


# ---------------------------------------------------------------------------
# child plan (pure): intersected grants, pinned server, parent scope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChildPlan:
    """One planned child: identity, re-validated payload, grant references."""

    run_id: str
    payload_json: str
    grants: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DelegationPlan:
    """The whole plan: one payload per child, in specification order."""

    children: tuple[ChildPlan, ...]

    @property
    def child_run_ids(self) -> tuple[str, ...]:
        return tuple(child.run_id for child in self.children)


def _child_context(
    parent: AgentRunIntent, child_run_id: str
) -> ExecutionContext:
    """The child's execution context: pinned server, named parent scope.

    C03 rule 2 + GRANT-INTERSECT: the child inherits the parent's
    principal, server binding, revision pins and credential REFERENCES;
    its ``run_id`` is its own identity and its ``parent_scope`` names the
    parent run + server (C10 cross-server tabletop: the parent's server
    binding stays visible in the child).
    """
    return ExecutionContext(
        principal=parent.context.principal,
        server_id=parent.context.server_id,
        run_id=uuid.UUID(child_run_id),
        parent_scope=ParentScope(
            run_id=parent.context.run_id, server_id=parent.context.server_id
        ),
        profile_revision=parent.context.profile_revision,
        prompt_revision=parent.context.prompt_revision,
        credential_references=parent.context.credential_references,
        allowed_capabilities=(),  # replaced by plan_children (intersection)
        mode=parent.context.mode,
    )


def _intersect(parent_allowed: Sequence[str], requested: Sequence[str]) -> tuple[str, ...]:
    """The child's effective grants: the parent's set, filtered by request.

    Preserve the parent's grant order (its context order is the reviewed
    vocabulary); a request can only SUBSET the parent — never add, never
    union with defaults; duplicates collapse (the context rejects them).
    """
    requested_set = set(requested)
    seen: set[str] = set()
    effective: list[str] = []
    for capability in parent_allowed:
        if capability in requested_set and capability not in seen:
            seen.add(capability)
            effective.append(capability)
    return tuple(effective)


def plan_children(
    intent: DelegationIntent,
    parent_row: ParentRunRow,
    child_ids: Sequence[str],
) -> DelegationPlan:
    """Construct the child payloads from explicit scope (pure; fail closed).

    Every child: a fresh identity supplied by the caller (generated
    inside the checkpointed planning step, so engine-recovery replays
    reuse the SAME identities), the parent's server (no retargeting), the
    intersected grants, and a payload that re-validates through the ONE
    agent-run producer before returning. The parent row's ``depth`` is
    the nesting budget: a parent at :data:`MAX_DELEGATION_DEPTH` may not
    delegate further, and a ``root_budget`` on the parent row narrows
    both the depth and the fan-out bound (C05 rule 7) before anything
    persists.
    """
    if len(child_ids) != len(intent.children):
        raise _reject_payload("child identity count does not match the request")
    if parent_row.server_id != intent.run.server_id:
        raise DelegationError(
            DelegationErrorCode.INVALID_RUN_STATE,
            "parent row server does not match the payload identity",
        )
    if parent_row.depth >= MAX_DELEGATION_DEPTH:
        raise DelegationError(
            DelegationErrorCode.DELEGATION_DEPTH,
            f"parent depth {parent_row.depth} exhausted the delegation "
            f"nesting budget ({MAX_DELEGATION_DEPTH})",
        )
    budgets = _budget_vocabulary()
    budget = None
    if parent_row.root_budget is not None:
        try:
            budget = budgets.parse_root_budget(dict(parent_row.root_budget))
        except budgets.BudgetError as error:
            raise DelegationError(
                DelegationErrorCode.INVALID_RUN_STATE,
                f"the stored root budget does not parse: {error.detail}",
            ) from None
    if budget is not None:
        # Budget-narrowed gates (C05 rule 7): the document can only
        # NARROW the module bounds, so these run after them and reject
        # whole (nothing persists, no identity is ever generated again).
        budgets.check_nesting(parent_row.depth, budget)
        budgets.check_fanout(len(intent.children), budget)
    children: list[ChildPlan] = []
    for spec, child_run_id in zip(intent.children, child_ids, strict=True):
        validate_run_id_or_reject(child_run_id)
        grants = _intersect(intent.run.context.allowed_capabilities, spec.capabilities)
        context = _child_context(intent.run, child_run_id)
        try:
            context = ExecutionContext(
                principal=context.principal,
                server_id=context.server_id,
                run_id=context.run_id,
                parent_scope=context.parent_scope,
                profile_revision=context.profile_revision,
                prompt_revision=context.prompt_revision,
                credential_references=context.credential_references,
                allowed_capabilities=grants,
                mode=context.mode,
            )
        except PolicyError as error:
            raise _reject_payload(f"child context rejected: {error.detail}") from None
        document: dict[str, object] = {
            "v": _run.RUN_PAYLOAD_VERSION,
            "run_id": child_run_id,
            "server_id": intent.run.server_id,
            "context": context.to_json(),
            "role": spec.role if spec.role is not None else intent.run.role,
            "task": spec.task,
            "connection": (
                spec.connection if spec.connection is not None else intent.run.connection
            ),
            "profile": spec.profile if spec.profile is not None else intent.run.profile,
            "rounds": intent.run.max_rounds,
            "reconcile_wait_seconds": intent.run.reconcile_wait_seconds,
            "wait_poll_seconds": intent.run.wait_poll_seconds,
        }
        payload_json = canonical_json(document)
        try:
            # The ONE producer re-validates our construction before
            # anything persists (identity binding included).
            parse_agent_run_intent(payload_json)
        except AgentRunError as error:
            raise _reject_payload(
                f"constructed child payload rejected: {error.detail}"
            ) from None
        children.append(
            ChildPlan(
                run_id=child_run_id, payload_json=payload_json, grants=grants
            )
        )
    return DelegationPlan(children=tuple(children))


def validate_run_id_or_reject(run_id: object) -> str:
    """Canonical UUID check for a caller-supplied child identity."""
    if not isinstance(run_id, str):
        raise _reject_payload("child run_id must be a string")
    try:
        parsed = uuid.UUID(run_id)
    except ValueError:
        raise _reject_payload("child run_id must be a canonical UUID") from None
    if str(parsed) != run_id:
        raise _reject_payload("child run_id must be canonical form")
    return run_id


# ---------------------------------------------------------------------------
# executor composition (the reviewed agent-run machinery, shared)
# ---------------------------------------------------------------------------


class DelegationExecutor:
    """Delegation seams over the composed agent-run executor.

    Wiring is constructor-checked (fail-closed, the sibling pattern).
    There is deliberately NO second machinery here: transitions go through
    the reviewed :class:`~mai.jobs.lifecycle.RunLifecycle` behind
    ``executor.steps.lifecycle`` (the frozen ``waiting_children`` edges
    are diagram edges), events go through the ONE whole-stream event
    writer (``executor.steps.aemit_run_event``), async seams run on the
    executor's ONE persistent loop, the child ``job_runs`` rows are plain
    facade-row inserts over the SAME session factory the agent run uses,
    and tree stops go through the ONE engine adapter
    (:class:`~mai.jobs.engine.EngineClient`). The worker composes one
    executor and configures both handlers.

    ``client`` exists because a cancellation exception poisons every
    checkpointed call in the cancelling thread: ``DBOS.cancel_workflow``
    executes as a STEP, so a cancelled parent calling it would re-raise
    the cancellation instead of stopping its children. The adapter's
    ``request_cancel`` is a plain control-plane write — the belt works
    exactly when it must (guard rules 3–4).
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        executor: _run.AgentRunExecutor,
        client: EngineClient,
        ledger: BudgetLedger,
    ) -> None:
        if not isinstance(sessions, async_sessionmaker):
            raise DelegationError(
                DelegationErrorCode.INVALID_WIRING,
                "sessions must be an async_sessionmaker on a NullPool engine",
            )
        if not isinstance(executor, _run.AgentRunExecutor):
            raise DelegationError(
                DelegationErrorCode.INVALID_WIRING,
                "executor must be the composed AgentRunExecutor",
            )
        if not isinstance(client, EngineClient):
            raise DelegationError(
                DelegationErrorCode.INVALID_WIRING,
                "client must be the EngineClient engine adapter",
            )
        if not isinstance(ledger, _budget_vocabulary().BudgetLedger):
            raise DelegationError(
                DelegationErrorCode.INVALID_WIRING,
                "ledger must be the BudgetLedger root-budget ledger",
            )
        self._sessions = sessions
        self._executor = executor
        self._client = client
        self._ledger = ledger

    def cancel_children_quietly(self, child_ids: Sequence[str]) -> None:
        """Best-effort explicit tree stop of the children (guard rule 4).

        Every child is cancelled WITH its own subtree through the engine
        adapter (never a checkpoint, so the call works while the
        cancellation exception is in flight). Failures are swallowed:
        this is the defensive belt — the facade's ``cancel_children=True``
        path reaches the same result through the native child edges.
        """
        for child_id in child_ids:
            try:
                self._client.request_cancel(child_id, cancel_children=True)
            except Exception:
                continue

    @property
    def executor(self) -> _run.AgentRunExecutor:
        """The composed reviewed agent-run executor (shared machinery)."""
        return self._executor

    def run(self, coro: object) -> object:
        """Drive one coroutine on the executor's persistent loop."""
        return self._executor.run(coro)

    # -- facade rows (the delegation linkage; C05 rules 3/5) ---------------

    async def _aparent_row(self, run_id: str) -> ParentRunRow:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT server_id, depth, profile_revision, "
                        "prompt_revision, model_profile, root_budget "
                        "FROM job_runs WHERE run_id = :r"
                    ),
                    {"r": uuid.UUID(run_id)},
                )
            ).mappings().first()
        if row is None:
            raise DelegationError(
                DelegationErrorCode.RUN_NOT_FOUND,
                "no job_runs row for the delegating parent",
            )
        root_budget = row["root_budget"]
        return ParentRunRow(
            run_id=run_id,
            server_id=str(row["server_id"]),
            depth=int(row["depth"]),
            profile_revision=int(row["profile_revision"]),
            prompt_revision=int(row["prompt_revision"]),
            model_profile=str(row["model_profile"]),
            root_budget=dict(root_budget) if isinstance(root_budget, Mapping) else None,
        )

    async def _ainsert_child_rows(
        self, plan: DelegationPlan, parent: DelegationIntent, parent_row: ParentRunRow
    ) -> None:
        """Insert every child's ``job_runs`` row (the linkage; idempotent).

        ``ON CONFLICT DO NOTHING``: a step re-executed inside its crash
        window observes the committed rows instead of duplicating them;
        the engine-level children carry the same IDs (reject policy), so
        a duplicate child run is structurally impossible (C05 rule 5).
        """
        depth = parent_row.depth + 1
        async with self._sessions() as session:
            async with session.begin():
                for child in plan.children:
                    await session.execute(
                        text(
                            """
                            INSERT INTO job_runs
                                (run_id, server_id, parent_run_id, depth,
                                 profile_revision, prompt_revision,
                                 model_profile, requested_grants, root_budget,
                                 submitted_at, updated_at)
                            VALUES
                                (:run_id, :server_id, :parent_run_id, :depth,
                                 :profile_revision, :prompt_revision,
                                 :model_profile, :requested_grants, :root_budget,
                                 now(), now())
                            ON CONFLICT (run_id) DO NOTHING
                            """
                        ),
                        {
                            "run_id": uuid.UUID(child.run_id),
                            "server_id": uuid.UUID(parent.run.server_id),
                            "parent_run_id": uuid.UUID(parent.run.run_id),
                            "depth": depth,
                            "profile_revision": parent_row.profile_revision,
                            "prompt_revision": parent_row.prompt_revision,
                            "model_profile": parent_row.model_profile,
                            "requested_grants": json.dumps(list(child.grants)),
                            "root_budget": (
                                json.dumps(parent_row.root_budget)
                                if parent_row.root_budget is not None
                                else None
                            ),
                        },
                    )

    async def _aemit(self, run_id: str, event_type: str, data: Mapping[str, object]) -> int:
        return await self._executor.steps.aemit_run_event(run_id, event_type, data)

    # -- the root-budget ledger (C05 rule 7; 06-delegation-budgets) --------

    async def _aroot_run_id(self, run_id: str) -> str:
        """Walk ``parent_run_id`` up to the tree root (the ledger's run).

        The budget is a root concept: every row carries the verbatim
        document, but the ledger's events live on the ROOT run's stream.
        Depth is module-bounded, so the walk is bounded; a missing row or
        a chain longer than the depth bound is an integrity rejection,
        never a guessed root.
        """
        current = run_id
        for _ in range(MAX_DELEGATION_DEPTH + 1):
            async with self._sessions() as session:
                row = (
                    await session.execute(
                        text("SELECT parent_run_id FROM job_runs WHERE run_id = :r"),
                        {"r": uuid.UUID(current)},
                    )
                ).mappings().first()
            if row is None:
                raise DelegationError(
                    DelegationErrorCode.RUN_NOT_FOUND,
                    "no job_runs row while resolving the budget root",
                )
            parent = row["parent_run_id"]
            if parent is None:
                return current
            current = str(parent)
        raise DelegationError(
            DelegationErrorCode.INVALID_RUN_STATE,
            "the parent_run_id chain exceeds the delegation depth bound",
        )

    async def _areserve_children(
        self, run_id: str, child_ids: Sequence[str]
    ) -> Mapping[str, object]:
        """Reserve the delegation's active slots on the root ledger.

        ``None``-shaped no-op (``{"unbudgeted": True}``) for an
        unbudgeted tree — the module bounds stay the only enforcement
        there and no transaction touches the root row. A budgeted
        rejection propagates the ledger's structured
        :class:`~mai.agents.budgets.BudgetError` (fail closed, whole:
        no child workflow was ever started). The ledger's own
        same-batch replay is the recovery-window idempotency.
        """
        parent_row = await self._aparent_row(run_id)
        if parent_row.root_budget is None:
            return {"unbudgeted": True}
        root_id = await self._aroot_run_id(run_id)
        state = await self._ledger.areserve(root_id, run_id, child_ids)
        if state.is_unbudgeted:
            return {"unbudgeted": True}
        return {"active_reserved": state.active_reserved}

    async def _acharge_children(
        self, run_id: str, child_ids: Sequence[str]
    ) -> Mapping[str, object]:
        """Charge each child's durable spend to the root budget.

        The producer-reported aggregate: one durable step per
        checkpointed model round on the child's own stream (token
        attribution stays the agent-run surface's recorded boundary, so
        tokens are not claimed here). Exactly-once under recovery: a
        child already charged on the root stream (the crash-inside-step
        window) is skipped — the read-before-insert discipline of the
        reviewed ``_emit_once``. Zero-round children (never reached the
        model) charge nothing, and a zero charge is never written.
        """
        parent_row = await self._aparent_row(run_id)
        if parent_row.root_budget is None:
            return {"unbudgeted": True}
        root_id = await self._aroot_run_id(run_id)
        charged = await self._abudget_charged_runs(root_id)
        lifecycle = self._executor.steps.lifecycle
        count = 0
        for child_id in child_ids:
            if child_id in charged:
                continue
            rounds = sum(
                1
                for event in await lifecycle.event_stream(child_id)
                if event.type == _run.AGENT_ROUND_EVENT_TYPE
            )
            if rounds <= 0:
                continue
            await self._ledger.aconsume(root_id, child_id, steps=rounds)
            count += 1
        return {"charged_runs": count}

    async def _abudget_charged_runs(self, root_id: str) -> set[str]:
        """The run identities already charged on the root's stream."""
        charged = _budget_vocabulary().BUDGET_CHARGED_EVENT_TYPE
        async with self._sessions() as session:
            rows = await session.execute(
                text("SELECT data FROM job_events WHERE run_id = :r AND type = :t"),
                {"r": uuid.UUID(root_id), "t": charged},
            )
            runs: set[str] = set()
            for row in rows.mappings():
                document = row["data"]
                if isinstance(document, str):
                    document = json.loads(document)
                if not isinstance(document, Mapping):
                    continue
                run = document.get("run")
                if isinstance(run, str):
                    runs.add(run)
            return runs

    async def _arelease_children(
        self, run_id: str, child_ids: Sequence[str]
    ) -> Mapping[str, object]:
        """Release the delegation's reserved slots (the cascade half).

        The frozen policy's ledger half: a delegating run that is
        cancelled, deadline-expired or failed refunds its unused
        reservations so the tree's remaining active budget stays
        truthful. Exactly-once under recovery: a released marker for
        THIS owner on the root stream short-circuits (the
        crash-inside-step window); a ledger rejection naming an
        already-freed batch is verified against the fold (none of these
        children outstanding any more, e.g. a failure path that runs
        before the reservation ever committed) before it is re-raised.
        """
        parent_row = await self._aparent_row(run_id)
        if parent_row.root_budget is None:
            return {"unbudgeted": True}
        budgets = _budget_vocabulary()
        root_id = await self._aroot_run_id(run_id)
        if await self._abudget_released(root_id, run_id):
            return {"already_released": True}
        try:
            await self._ledger.arelease(root_id, run_id, child_ids)
        except budgets.BudgetError as error:
            if error.code is not budgets.BudgetErrorCode.LEDGER_STATE:
                raise
            state = await self._ledger.asnapshot(root_id)
            if set(state.outstanding) & set(child_ids):
                raise
            return {"already_released": True}
        return {"released": len(child_ids)}

    async def _abudget_released(self, root_id: str, owner_run_id: str) -> bool:
        """Whether THIS owner's batch is already released on the root."""
        released = _budget_vocabulary().BUDGET_RELEASED_EVENT_TYPE
        async with self._sessions() as session:
            rows = await session.execute(
                text("SELECT data FROM job_events WHERE run_id = :r AND type = :t"),
                {"r": uuid.UUID(root_id), "t": released},
            )
            for row in rows.mappings():
                document = row["data"]
                if isinstance(document, str):
                    document = json.loads(document)
                if not isinstance(document, Mapping):
                    continue
                if document.get("owner") == owner_run_id:
                    return True
            return False

    async def _await_deadline(
        self, run_id: str, wait_seconds: float, child_ids: tuple[str, ...]
    ) -> str:
        """Enter ``waiting_children`` ONCE; return the ABSOLUTE deadline.

        Idempotent under recovery replay: an already-parked parent
        observes its persisted deadline (budgets never silently extend);
        any other non-``running`` state is a value-free rejection.
        """
        lifecycle = self._executor.steps.lifecycle
        current = await lifecycle.current_state(run_id)
        if current is RunState.WAITING_CHILDREN:
            for event in reversed(await lifecycle.event_stream(run_id)):
                if event.type == DELEGATION_WAIT_EVENT_TYPE:
                    deadline = event.data.get("deadline")
                    if isinstance(deadline, str):
                        return deadline
            raise DelegationError(
                DelegationErrorCode.INVALID_RUN_STATE,
                "parent is parked but no persisted wait deadline exists",
            )
        if current is not RunState.RUNNING:
            raise DelegationError(
                DelegationErrorCode.INVALID_RUN_STATE,
                "parent is not in a state that may enter the children wait",
            )
        deadline = datetime.now(timezone.utc) + timedelta(seconds=float(wait_seconds))
        await lifecycle.record_transition(run_id, RunState.WAITING_CHILDREN)
        await self._aemit(
            run_id,
            DELEGATION_WAIT_EVENT_TYPE,
            {"children": list(child_ids), "deadline": deadline.isoformat()},
        )
        return deadline.isoformat()

    async def _achildren_resumed(
        self,
        run_id: str,
        resolutions: Sequence[Mapping[str, object]],
    ) -> str:
        """Record ``waiting_children → running`` + the safe wake event.

        Terminal child states are the engine truth read by the wait loop;
        the event carries identities and states only (C05 rule 4).
        """
        lifecycle = self._executor.steps.lifecycle
        current = await lifecycle.current_state(run_id)
        if current is not RunState.WAITING_CHILDREN:
            raise DelegationError(
                DelegationErrorCode.INVALID_RUN_STATE,
                "parent is not parked on waiting_children",
            )
        await lifecycle.record_transition(run_id, RunState.RUNNING)
        await self._aemit(
            run_id,
            CHILDREN_RESOLVED_EVENT_TYPE,
            {"children": [dict(item) for item in resolutions]},
        )
        return RunState.RUNNING.value

    async def _aresume_from_wait(self, run_id: str) -> str:
        """Record ``waiting_children → running`` (deadline/cancel resume)."""
        lifecycle = self._executor.steps.lifecycle
        current = await lifecycle.current_state(run_id)
        if current is RunState.RUNNING:
            return RunState.RUNNING.value
        if current is RunState.WAITING_CHILDREN:
            await lifecycle.record_transition(run_id, RunState.RUNNING)
            return RunState.RUNNING.value
        raise DelegationError(
            DelegationErrorCode.INVALID_RUN_STATE,
            "parent is not parked on waiting_children",
        )

    async def _afinish(self, run_id: str) -> str:
        """Record ``running → succeeded`` (observe; terminal guard holds)."""
        lifecycle = self._executor.steps.lifecycle
        current = await lifecycle.current_state(run_id)
        if current is RunState.RUNNING:
            await lifecycle.record_transition(run_id, RunState.SUCCEEDED)
            return RunState.SUCCEEDED.value
        if current is RunState.SUCCEEDED:
            return RunState.SUCCEEDED.value
        raise DelegationError(
            DelegationErrorCode.INVALID_RUN_STATE,
            "parent is not in a state that may succeed",
        )


_DELEGATION: DelegationExecutor | None = None


def configure_delegation(executor: DelegationExecutor) -> None:
    """Configure the process-wide delegation executor (worker-side seam)."""
    global _DELEGATION
    if not isinstance(executor, DelegationExecutor):
        raise DelegationError(
            DelegationErrorCode.INVALID_WIRING,
            "executor must be the composed DelegationExecutor",
        )
    if _DELEGATION is not None:
        raise DelegationError(
            DelegationErrorCode.ALREADY_CONFIGURED,
            "the delegation executor is already configured",
        )
    _DELEGATION = executor


def reset_delegation() -> None:
    """Reset the delegation executor (idempotent; test convenience)."""
    global _DELEGATION
    _DELEGATION = None


def _resolve_delegation() -> DelegationExecutor:
    if _DELEGATION is None:
        raise DelegationError(
            DelegationErrorCode.EXECUTOR_NOT_CONFIGURED,
            "no delegation executor configured in this process",
        )
    return _DELEGATION


# ---------------------------------------------------------------------------
# checkpointed steps (workflow-side; the run.py discipline)
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _deadline_of(iso: str) -> datetime:
    deadline = datetime.fromisoformat(iso)
    if deadline.tzinfo is None:
        raise DelegationError(
            DelegationErrorCode.INVALID_RUN_STATE,
            "persisted deadline lost its timezone",
        )
    return deadline


@DBOS.step(name="agents.delegate_parent_started")
def _delegation_parent_started(payload_json: str) -> str:
    """Observe-and-record the parent's ``queued`` anchor + ``running``."""
    intent = parse_delegation_intent(payload_json)
    executor = _resolve_delegation()
    return str(
        executor.run(executor.executor.steps.aobserve_run_started(intent.run.run_id))
    )


@DBOS.step(name="agents.delegate_child_ids")
def _delegation_child_ids(payload_json: str) -> str:
    """Generate the child identities ONCE (checkpointed; replay reuses)."""
    intent = parse_delegation_intent(payload_json)
    return canonical_json(
        [str(uuid.uuid4()) for _ in intent.children]
    )


@DBOS.step(name="agents.delegate_children_planned")
def _delegation_children_planned(payload_json: str, ids_json: str) -> str:
    """Plan + persist the linkage; emit the safe planned event.

    One checkpoint: the pure planner intersects the grants and builds the
    child payloads (each re-validated by the agent-run producer), the
    child ``job_runs`` rows commit with ``parent_run_id``/``depth``
    linkage, and the parent stream records the planned child identities
    (no task content). A recovery replay observes the committed rows and
    returns the recorded plan.
    """
    intent = parse_delegation_intent(payload_json)
    executor = _resolve_delegation()
    child_ids = tuple(str(item) for item in json.loads(ids_json))
    parent_row = executor.run(executor._aparent_row(intent.run.run_id))
    assert isinstance(parent_row, ParentRunRow)
    plan = plan_children(intent, parent_row, child_ids)
    executor.run(executor._ainsert_child_rows(plan, intent, parent_row))
    executor.run(
        executor._aemit(
            intent.run.run_id,
            DELEGATION_PLANNED_EVENT_TYPE,
            {
                "children": list(plan.child_run_ids),
                "depth": parent_row.depth + 1,
            },
        )
    )
    return canonical_json(
        {
            "child_ids": list(plan.child_run_ids),
            "child_payloads": [child.payload_json for child in plan.children],
        }
    )


@DBOS.step(name="agents.delegate_budget_reserved")
def _delegation_budget_reserved(payload_json: str, ids_json: str) -> str:
    """Reserve the delegation's active slots on the root ledger (C05 r7).

    One checkpoint BEFORE any child starts: a budgeted delegation that
    cannot fit the root's remaining active budget is rejected whole
    (the ledger's structured ``BudgetError`` — nothing was ever
    started), and the ledger's own same-batch replay is the recovery
    idempotency. Unbudgeted trees skip the ledger entirely.
    """
    intent = parse_delegation_intent(payload_json)
    executor = _resolve_delegation()
    child_ids = tuple(str(item) for item in json.loads(ids_json))
    result = executor.run(executor._areserve_children(intent.run.run_id, child_ids))
    return canonical_json(dict(result))


@DBOS.step(name="agents.delegate_budget_settled")
def _delegation_budget_settled(payload_json: str, plan_json: str) -> str:
    """Charge each child's durable spend, then release the batch.

    The success path's ledger close-out (one checkpoint): per-child
    consumption is charged to the root's aggregate budget and the
    delegation's reserved slots are released — charged consumption is
    never refunded, released slots free the tree's active budget for
    the next delegation. Both halves are exactly-once under recovery
    (charged-run / released-owner markers on the root stream).
    """
    intent = parse_delegation_intent(payload_json)
    executor = _resolve_delegation()
    plan = json.loads(plan_json)
    child_ids = tuple(str(item) for item in plan["child_ids"])
    charged = executor.run(executor._acharge_children(intent.run.run_id, child_ids))
    released = executor.run(executor._arelease_children(intent.run.run_id, child_ids))
    return canonical_json({"charged": dict(charged), "released": dict(released)})


@DBOS.step(name="agents.delegate_budget_released")
def _delegation_budget_released(payload_json: str, ids_json: str) -> str:
    """Release the delegation's slots (deadline-expiry path's refund).

    The tree is being stopped honestly: the batch's outstanding
    reservations are refunded so the root's remaining active budget
    stays truthful for the tree's remaining branches. Charged
    consumption is never refunded; the release is exactly-once under
    recovery (the released-owner marker).
    """
    intent = parse_delegation_intent(payload_json)
    executor = _resolve_delegation()
    child_ids = tuple(str(item) for item in json.loads(ids_json))
    result = executor.run(executor._arelease_children(intent.run.run_id, child_ids))
    return canonical_json(dict(result))


@DBOS.step(name="agents.delegate_wait_entered")
def _delegation_wait_entered(payload_json: str, plan_json: str) -> str:
    """Park ``waiting_children`` once; persist + return the deadline."""
    intent = parse_delegation_intent(payload_json)
    executor = _resolve_delegation()
    plan = json.loads(plan_json)
    return str(
        executor.run(
            executor._await_deadline(
                intent.run.run_id,
                intent.children_wait_seconds,
                tuple(str(item) for item in plan["child_ids"]),
            )
        )
    )


@DBOS.step(name="agents.delegate_children_resolved")
def _delegation_children_resolved(
    payload_json: str, resolutions_json: str
) -> str:
    """Wake: ``waiting_children → running`` + the safe resolved event."""
    intent = parse_delegation_intent(payload_json)
    executor = _resolve_delegation()
    resolutions = json.loads(resolutions_json)
    return str(executor.run(executor._achildren_resumed(intent.run.run_id, resolutions)))


@DBOS.step(name="agents.delegate_wait_resumed")
def _delegation_wait_resumed(payload_json: str) -> str:
    """Record ``waiting_children → running`` (deadline/cancel resume)."""
    intent = parse_delegation_intent(payload_json)
    executor = _resolve_delegation()
    return str(executor.run(executor._aresume_from_wait(intent.run.run_id)))


@DBOS.step(name="agents.delegate_run_finished")
def _delegation_run_finished(payload_json: str, summary_json: str) -> str:
    """Record ``running → succeeded``; emit the safe summary event."""
    intent = parse_delegation_intent(payload_json)
    executor = _resolve_delegation()
    summary = json.loads(summary_json)
    executor.run(executor._afinish(intent.run.run_id))
    return str(
        executor.run(
            executor._aemit(
                intent.run.run_id,
                DELEGATION_COMPLETED_EVENT_TYPE,
                {
                    "children": summary.get("children"),
                    "succeeded": summary.get("succeeded"),
                    "failed": summary.get("failed"),
                    "cancelled": summary.get("cancelled"),
                },
            )
        )
    )


@DBOS.step(name="agents.delegate_run_failed")
def _delegation_run_failed(payload_json: str, reason: str) -> str:
    """Record the parent ``failed`` + the classified reason event."""
    intent = parse_delegation_intent(payload_json)
    executor = _resolve_delegation()
    result = executor.run(executor.executor.steps.afail_run(intent.run.run_id, reason))
    executor.run(
        executor._aemit(
            intent.run.run_id,
            _run.AGENT_FAILED_EVENT_TYPE,
            {"reason": reason},
        )
    )
    return str(result)


def _budget_release_quietly(intent: DelegationIntent, child_ids: Sequence[str]) -> None:
    """Best-effort plain (non-checkpointed) reservation refund.

    The cancellation/failure path's ledger half (mirror of
    ``_record_quietly``): DBOS raises its cancellation exception AT
    checkpoint boundaries, so the release must be plain calls. The
    executor's release is exactly-once by observation (released-owner
    markers), so a replay or a racing double release is a no-op, not a
    corruption; failures leave the fold untouched (the stream never
    lies).
    """
    try:
        executor = _resolve_delegation()
    except DelegationError:
        return
    try:
        executor.run(executor._arelease_children(intent.run.run_id, tuple(child_ids)))
    except Exception:
        pass


def _record_quietly(intent: DelegationIntent, action: str) -> None:
    """Best-effort plain (non-checkpointed) facade translation.

    The cancellation path mirrors ``mai.agents.run._record_quietly``:
    DBOS raises its cancellation exception AT checkpoint boundaries, so
    the translation must be plain calls. Idempotent by observation;
    failures leave the engine's own exception as the truth.
    """
    try:
        executor = _resolve_delegation()
    except DelegationError:
        return
    try:
        if action == "cancel":
            executor.run(executor.executor.steps.acancel_run(intent.run.run_id))
        else:
            executor.run(executor.executor.steps.afail_run(intent.run.run_id, "handler_error"))
    except Exception:
        pass


def _cancel_children_quietly(child_ids: Sequence[str]) -> None:
    """Best-effort tree stop of the delegation children (defensive belt).

    A cancelled or deadline-expired parent never orphans its children.
    The stop goes through the configured executor's engine adapter —
    NEVER through ``DBOS.cancel_workflow``, which executes as a
    checkpointed step and would re-raise the in-flight cancellation
    exception instead of stopping the tree (guard rule 4's explicit tree
    cancellation, applied at the delegation boundary; the facade's
    ``cancel_children=True`` path reaches the same result through the
    native child edges).
    """
    try:
        executor = _resolve_delegation()
    except DelegationError:
        return
    executor.cancel_children_quietly(child_ids)


# ---------------------------------------------------------------------------
# the wait loop (workflow context; engine truth, never invention)
# ---------------------------------------------------------------------------


def _child_engine_state(child_id: str) -> RunState:
    """Map one child's engine status onto the frozen run state."""
    try:
        status = DBOS.retrieve_workflow(child_id).get_status()
    except dbos_error.DBOSNonExistentWorkflowError:
        raise DelegationError(
            DelegationErrorCode.CHILD_UNKNOWN,
            f"the engine has no workflow for child run {child_id}",
        ) from None
    if status is None:
        raise DelegationError(
            DelegationErrorCode.CHILD_UNKNOWN,
            f"the engine reported no status for child run {child_id}",
        )
    try:
        return run_state_from_engine(status.status)
    except Exception as error:
        raise DelegationError(
            DelegationErrorCode.CHILD_UNKNOWN,
            f"child run {child_id} reported an unmappable engine status",
        ) from error


def _await_children(
    intent: DelegationIntent, child_ids: tuple[str, ...], deadline_iso: str
) -> list[dict[str, object]]:
    """Poll child terminality; return the value-free resolution list.

    The parent thread sleeps in CHECKPOINTED ``DBOS.sleep`` bounds: the
    poll budget is explicit, the deadline is enforced, and every sleep
    boundary is a cancellation checkpoint (requested ≠ verified, guard
    rule 3). Children are independent engine workflows — never queue
    entries — so this wait never gates child execution on worker-slot
    availability (C05 rule 7). A terminal child's state is read from the
    engine; a SUCCEEDED child's result is awaited once (the engine child
    edge records it), a failed/cancelled child's state alone is recorded
    (its own stream holds the truth).
    """
    deadline = _deadline_of(deadline_iso)
    resolutions: dict[str, dict[str, object]] = {}
    while True:
        for child_id in child_ids:
            if child_id in resolutions:
                continue
            state = _child_engine_state(child_id)
            if state not in TERMINAL_RUN_STATES:
                continue
            record: dict[str, object] = {
                "run_id": child_id,
                "state": state.value,
            }
            if state is RunState.SUCCEEDED:
                # Await once through the child edge; the engine records
                # the child's returned summary on the parent history.
                DBOS.retrieve_workflow(child_id).get_result()
            resolutions[child_id] = record
        if len(resolutions) == len(child_ids):
            return [resolutions[child_id] for child_id in child_ids]
        remaining = (deadline - _utcnow()).total_seconds()
        if remaining <= 0:
            raise AgentStageError(CHILDREN_WAIT_DEADLINE_REASON)
        DBOS.sleep(
            min(
                intent.children_poll_seconds,
                max(MIN_CHILDREN_POLL_SECONDS, remaining),
            )
        )


# ---------------------------------------------------------------------------
# the one registered run handler
# ---------------------------------------------------------------------------


def run_delegating_parent(payload_json: str) -> dict[str, object]:
    """Execute one durable delegating parent (see module docstring).

    Registered as :data:`HANDLER_NAME` (module import) and executed
    inside the engine's durable trampoline workflow. Classified failures
    record ``failed`` and raise :class:`DelegationParentFailed` (engine
    ERROR — both sides agree); cancellation stops the tree (best-effort
    belt) and re-raises the engine's own exception after the facade
    translation; integrity rejections from the reviewed machinery
    propagate as-is. A budgeted tree (``root_budget`` on the parent row)
    additionally reserves its active slots BEFORE any child starts,
    charges each child's durable spend and releases the batch when the
    children resolve, and refunds unused slots on every non-success
    exit (:mod:`mai.agents.budgets`, C05 rule 7). Success returns the
    safe delegation summary.
    """
    intent = parse_delegation_intent(payload_json)
    ids_json: str | None = None
    child_ids: tuple[str, ...] = ()
    try:
        _delegation_parent_started(payload_json)
        ids_json = _delegation_child_ids(payload_json)
        child_ids = tuple(str(item) for item in json.loads(ids_json))
        plan_json = _delegation_children_planned(payload_json, ids_json)
        # The aggregate gate (C05 rule 7): the batch reserves its active
        # slots on the root ledger BEFORE any child starts, so a
        # delegation that cannot fit the remaining budget is rejected
        # whole (classified ``budget_*`` failure, nothing started) and
        # racing sibling delegations serialize on the root row.
        _delegation_budget_reserved(payload_json, ids_json)
        plan: dict[str, object] = json.loads(plan_json)
        # Children start as checkpointed engine children of THIS workflow
        # (deterministic run IDs, reject policy); the facade rows already
        # committed, so a recovered child always finds its row.
        for child_id, child_payload in zip(
            child_ids, plan["child_payloads"], strict=True
        ):
            with SetWorkflowID(
                child_id, workflow_id_reuse_policy=_CHILD_WORKFLOW_ID_REUSE_POLICY
            ):
                DBOS.start_workflow(
                    _engine.run_workflow, _run.HANDLER_NAME, str(child_payload)
                )
        deadline = _delegation_wait_entered(payload_json, plan_json)
        try:
            resolutions = _await_children(intent, child_ids, deadline)
        except AgentStageError as stage:
            # Deadline expiry: stop the tree honestly, refund the batch's
            # unused slots, then fail.
            _cancel_children_quietly(child_ids)
            _delegation_budget_released(payload_json, ids_json)
            _delegation_wait_resumed(payload_json)
            _delegation_run_failed(payload_json, stage.reason)
            raise DelegationParentFailed(stage.reason) from None
        _delegation_children_resolved(payload_json, canonical_json(resolutions))
        # The ledger close-out: per-child durable spend charged, the
        # batch's slots released (both exactly-once under recovery).
        _delegation_budget_settled(payload_json, plan_json)
        counts: dict[str, int] = {"succeeded": 0, "failed": 0, "cancelled": 0}
        for resolution in resolutions:
            state = str(resolution["state"])
            counts[state] = counts.get(state, 0) + 1
        summary: dict[str, object] = {
            "children": resolutions,
            "succeeded": counts.get("succeeded", 0),
            "failed": counts.get("failed", 0),
            "cancelled": counts.get("cancelled", 0),
        }
        _delegation_run_finished(payload_json, canonical_json(summary))
        return {"run_id": intent.run.run_id, "state": "succeeded", **summary}
    except AgentRunError:
        _budget_release_quietly(intent, child_ids)
        _record_quietly(intent, "fail")
        raise
    except StepsError:
        _budget_release_quietly(intent, child_ids)
        _record_quietly(intent, "fail")
        raise
    except (
        dbos_error.DBOSWorkflowCancelledError,
        dbos_error.DBOSAwaitedWorkflowCancelledError,
    ):
        _cancel_children_quietly(child_ids)
        # Plain (non-checkpointed): the cancellation exception is in
        # flight — a checkpoint here would re-raise it (guard rule 4).
        _budget_release_quietly(intent, child_ids)
        _record_quietly(intent, "cancel")
        raise
    except DelegationError:
        _budget_release_quietly(intent, child_ids)
        _record_quietly(intent, "fail")
        raise
    except _budget_vocabulary().BudgetError as error:
        # A classified budget rejection (a narrowed plan gate or the
        # ledger's whole-batch refusal): fail the parent honestly with
        # the value-free budget reason — the engine ERROR and the
        # facade agree, like the deadline path.
        reason = f"budget_{error.code.value}"
        _delegation_run_failed(payload_json, reason)
        raise DelegationParentFailed(reason) from None
    except Exception:
        # An unexpected failure (a defect, not a classified stage): the
        # engine lands ERROR either way — record the honest facade
        # translation so both sides agree, then let the original
        # exception surface as-is (never swallowed, never classified).
        _budget_release_quietly(intent, child_ids)
        _record_quietly(intent, "fail")
        raise


#: The parent's defensive belt and the facade translation above mirror the
#: agent-run handler's cancellation discipline (mai.agents.run).
_engine.register_job_handler(HANDLER_NAME, run_delegating_parent)
