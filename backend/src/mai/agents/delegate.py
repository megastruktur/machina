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
import re
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
from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID, SYSTEM_ASSIGNMENT_SOURCE

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
    "DELEGATION_PAYLOAD_VERSION_V2",
    "DEFAULT_CHILDREN_POLL_SECONDS",
    "DEFAULT_CHILDREN_WAIT_SECONDS",
    "HANDLER_NAME",
    "MAX_CHILDREN_PER_RUN",
    "MAX_CHILDREN_POLL_SECONDS",
    "MAX_CHILDREN_WAIT_SECONDS",
    "MAX_DELEGATION_DEPTH",
    "V2_CHILD_HANDLER_NAME",
    "ChildBudgetBounds",
    "ChildPlan",
    "ChildResolution",
    "ChildSpec",
    "ChildSpecV2",
    "DelegationError",
    "DelegationErrorCode",
    "DelegationExecutor",
    "DelegationIntent",
    "DelegationIntentV2",
    "DelegationParentFailed",
    "DelegationPlan",
    "ParentRunRow",
    "V2ChildPayload",
    "configure_delegation",
    "configure_v2_child_handler",
    "parse_delegation_intent",
    "parse_delegation_intent_v2",
    "parse_v2_child_payload",
    "plan_children",
    "plan_children_v2",
    "reset_delegation",
    "reset_v2_child_handler",
    "run_delegating_parent",
    "v2_child_handler_name",
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

#: Payload document version of v2 multi-server delegation (runtime-v2.md
#: §7.2; design target ``ChildSpecV2``). The v1 payload above stays
#: FROZEN forever for legacy replay (S2): a v1 parent keeps producing
#: legacy ``agent.run`` children, byte-identical semantics.
DELEGATION_PAYLOAD_VERSION_V2: Final[int] = 2

#: The run handler v2 children select (runtime-v2.md §7.4). Worker-side
#: configurable (``configure_v2_child_handler``) so the action-runtime
#: slice owns the exact name without touching this module's freeze; the
#: default is the frozen design-target identity.
V2_CHILD_HANDLER_NAME: Final[str] = "agent.run_v2"

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

#: Process-wide v2 child handler name (worker-side seam; see
#: :func:`configure_v2_child_handler`).
_V2_CHILD_HANDLER: str = V2_CHILD_HANDLER_NAME

#: The dotted-slug grammar of engine handler names (mirrored shape; the
#: engine's own grammar stays authoritative at registration time).
_HANDLER_SLUG_RE: Final[str] = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]+)*$"


def configure_v2_child_handler(name: str) -> None:
    """Configure which run handler v2 children select (worker-side seam).

    Defaults to :data:`V2_CHILD_HANDLER_NAME` (the frozen design-target
    identity). A v1 parent ALWAYS selects ``agent.run`` regardless of
    this setting — the v1 replay surface stays frozen (§7.4).
    """
    global _V2_CHILD_HANDLER
    if not isinstance(name, str) or not re.fullmatch(_HANDLER_SLUG_RE, name):
        raise DelegationError(
            DelegationErrorCode.INVALID_WIRING,
            "the v2 child handler name must be a dotted-slug handler name",
        )
    _V2_CHILD_HANDLER = name


def reset_v2_child_handler() -> None:
    """Restore the default v2 child handler name (idempotent; tests)."""
    global _V2_CHILD_HANDLER
    _V2_CHILD_HANDLER = V2_CHILD_HANDLER_NAME


def v2_child_handler_name() -> str:
    """The run handler v2 children currently select."""
    return _V2_CHILD_HANDLER


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
    #: v2: a child named a server outside the parent's objective scope.
    SCOPE_VIOLATION = "scope_violation"
    #: v2: a child requested a capability outside the parent's grants.
    CAPABILITY_EXPANSION = "capability_expansion"
    #: v2: a child budget bound exceeds the parent's remaining budget.
    BUDGET_EXPANSION = "budget_expansion"
    #: v2: the delegating parent run is not bound to a v2 objective.
    OBJECTIVE_UNBOUND = "objective_unbound"


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


# ---------------------------------------------------------------------------
# v2 intent (multi-server children; runtime-v2.md §7.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChildBudgetBounds:
    """A v2 child's requested reservation bounds (ledger vocabulary).

    Both bounds are NARROWING requests against the parent's REMAINING
    root budget (:meth:`mai.agents.budgets.BudgetState.remaining_steps` /
    ``remaining_tokens``): a child can never reserve more than the root
    still holds (C05 rule 7), and an unbudgeted root has no remaining
    budget to bound — child bounds are rejected there outright.
    """

    max_steps: int
    max_total_tokens: int

    def to_json(self) -> dict[str, int]:
        """Frozen-shape document (the v2 child payload's ``budget``)."""
        return {
            "max_steps": self.max_steps,
            "max_total_tokens": self.max_total_tokens,
        }


@dataclass(frozen=True, slots=True)
class ChildSpecV2:
    """One explicitly scoped v2 child task (runtime-v2.md §7.2).

    Differences from the frozen v1 :class:`ChildSpec` (which stays
    byte-frozen for legacy replay):

    - ``server_ids`` — a non-empty explicit subset of the parent's
      effective objective server scope (a child's set ⊆ the parent's
      effective set; C03 rule 3 intersection unchanged). The FIRST
      member is the child's initiating server (its context pin and its
      ``job_runs.server_id``); the whole set is the child's delegated
      scope, durable in the v2 child payload.
    - ``budget`` — optional reservation bounds in the ledger vocabulary;
      planning rejects bounds exceeding the parent's remaining budget.
    - ``capabilities`` stays a REQUEST: the effective child grants are
      the intersection with the parent's granted set, and naming a
      capability outside the parent's grants is a classified
      ``capability_expansion`` rejection (v2 fails closed where v1
      clamped silently — multi-server scope must never widen).
    """

    task: str
    server_ids: tuple[str, ...]
    role: str | None
    connection: str | None
    profile: str | None
    capabilities: tuple[str, ...]
    budget: ChildBudgetBounds | None


@dataclass(frozen=True, slots=True)
class DelegationIntentV2:
    """The validated payload for a v2 delegating parent (payload v2).

    ``run`` is the parent's run intent, validated by the ONE producer
    (:func:`mai.agents.run.parse_agent_run_intent`); the parent run must
    be bound to a v2 objective (``objective_runs``) — planning resolves
    the objective scope from that binding, never from payload prose.
    """

    run: AgentRunIntent
    children: tuple[ChildSpecV2, ...]
    children_wait_seconds: float
    children_poll_seconds: float


@dataclass(frozen=True, slots=True)
class V2ChildPayload:
    """The validated payload of one v2 child run (the ONE parse).

    Document shape (owned by THIS module; the v2 child handler consumes
    it through :func:`parse_v2_child_payload` — no second parser):
    ``{"v": 2, "run": <agent-run document>, "server_ids": [...],
    "budget": {...}|null}``. ``run`` is the frozen agent-run vocabulary
    (context/task/role/rounds) with the child's OWN identity, its
    initiating-server context pin and the INTERSECTED grants;
    ``server_ids`` is the delegated scope (⊆ the objective scope, checked
    at plan time); ``budget`` the narrowed reservation bounds.
    """

    run: AgentRunIntent
    server_ids: tuple[str, ...]
    budget: ChildBudgetBounds | None


@dataclass(frozen=True, slots=True)
class ChildResolution:
    """One settled child's RECEIPT-level resolution (§7.5; AC5).

    The parent aggregates RECEIPTS, never child claims: ``state`` is the
    engine truth for the child's terminal state, ``receipt`` the child's
    own returned summary document (SUCCEEDED children only; the child's
    prose lives in its stream, never here). ``failed``/``cancelled``
    children keep their honest state with no receipt; a child the engine
    cannot map is a classified ``child_run_unknown`` parent failure —
    never a guessed success.
    """

    run_id: str
    state: str
    receipt: Mapping[str, object] | None = None

    def to_record(self) -> dict[str, object]:
        """Value-free event document (identities, states, receipts).

        An absent receipt is OMITTED (the v1 record shape stays
        byte-identical); a failed/cancelled child carries no receipt key.
        """
        record: dict[str, object] = {"run_id": self.run_id, "state": self.state}
        if self.receipt is not None:
            record["receipt"] = dict(self.receipt)
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> ChildResolution:
        run_id = record.get("run_id")
        state = record.get("state")
        if not isinstance(run_id, str) or not isinstance(state, str):
            raise _reject_payload("child resolution needs run_id and state")
        receipt = record.get("receipt")
        if receipt is not None and not isinstance(receipt, Mapping):
            raise _reject_payload("child resolution receipt must be an object")
        return cls(
            run_id=run_id, state=state, receipt=dict(receipt) if receipt else None
        )


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
# v2 parsing (strict; the ONE producer discipline, payload version 2)
# ---------------------------------------------------------------------------


def _peek_delegation_version(payload_json: object) -> int:
    """Best-effort payload-version peek (never a validation).

    Any malformed input reads as the frozen v1 so the ONE v1 parser
    produces its own detailed rejection; a well-formed document with a
    foreign version routes to the matching parser, which rejects it
    value-free. The delegation handler dispatches on this peek only.
    """
    if not isinstance(payload_json, str):
        return DELEGATION_PAYLOAD_VERSION
    try:
        document = json.loads(payload_json)
    except ValueError:
        return DELEGATION_PAYLOAD_VERSION
    if not isinstance(document, Mapping):
        return DELEGATION_PAYLOAD_VERSION
    version = document.get("v", DELEGATION_PAYLOAD_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        return DELEGATION_PAYLOAD_VERSION
    return version


def _parse_intent(payload_json: str) -> DelegationIntent | DelegationIntentV2:
    """Parse either delegation payload version (shared checkpoint steps)."""
    if _peek_delegation_version(payload_json) == DELEGATION_PAYLOAD_VERSION_V2:
        return parse_delegation_intent_v2(payload_json)
    return parse_delegation_intent(payload_json)


def _parse_child_budget_bounds(raw: object) -> ChildBudgetBounds | None:
    """Validate one child budget-bounds document (``None`` = unbounded)."""
    if raw is None:
        return None
    budgets = _budget_vocabulary()
    if not isinstance(raw, Mapping):
        raise _reject_payload("child budget must be a JSON object or null")
    unknown = sorted(set(raw) - {"max_steps", "max_total_tokens"})
    if unknown:
        raise _reject_payload(f"child budget has unknown field {unknown[0]!r}")
    missing = {"max_steps", "max_total_tokens"} - set(raw)
    if missing:
        raise _reject_payload(f"child budget is missing {sorted(missing)[0]!r}")
    bounds: dict[str, int] = {}
    for field, maximum in (
        ("max_steps", budgets.MAX_TOTAL_STEPS),
        ("max_total_tokens", budgets.MAX_TOTAL_TOKENS),
    ):
        value = raw[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise _reject_payload(f"child budget {field} must be a positive int")
        if value > maximum:
            raise _reject_payload(
                f"child budget {field} outside its bounded range 1..{maximum}"
            )
        bounds[field] = value
    return ChildBudgetBounds(
        max_steps=bounds["max_steps"],
        max_total_tokens=bounds["max_total_tokens"],
    )


def _parse_server_ids(raw: object, field: str) -> tuple[str, ...]:
    """Canonical UUID strings, non-empty, unique, objective-bounded."""
    from mai.contracts.objectives import MAX_SCOPE_SERVERS

    if not isinstance(raw, (list, tuple)):
        raise _reject_payload(f"child {field} must be an array of server UUIDs")
    if not raw:
        raise _reject_payload(f"child {field} cannot be empty")
    if len(raw) > MAX_SCOPE_SERVERS:
        raise _reject_payload(
            f"child {field} is bounded at {MAX_SCOPE_SERVERS} servers"
        )
    servers: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise _reject_payload(f"child {field} entries must be UUID strings")
        try:
            parsed = uuid.UUID(item)
        except ValueError:
            raise _reject_payload(
                f"child {field} entries must be canonical UUID strings"
            ) from None
        if str(parsed) != item:
            raise _reject_payload(
                f"child {field} entries must be canonical UUID strings"
            )
        servers.append(item)
    if len(set(servers)) != len(servers):
        raise _reject_payload(f"child {field} carries duplicate servers")
    return tuple(servers)


def _parse_child_spec_v2(raw: object, index: int) -> ChildSpecV2:
    """One strict v2 child specification (see :class:`ChildSpecV2`)."""
    if not isinstance(raw, Mapping):
        raise _reject_payload(f"child {index} must be a JSON object")
    allowed = {
        "task",
        "role",
        "connection",
        "profile",
        "capabilities",
        "server_ids",
        "budget",
    }
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
    if "server_ids" not in raw:
        raise _reject_payload(f"child {index} is missing its server_ids")
    server_ids = _parse_server_ids(raw["server_ids"], f"server_ids (child {index})")
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
    try:
        budget = _parse_child_budget_bounds(raw.get("budget"))
    except DelegationError as error:
        raise _reject_payload(f"child {index} budget rejected: {error.detail}") from None
    return ChildSpecV2(
        task=task,
        server_ids=server_ids,
        role=raw.get("role"),
        connection=raw.get("connection"),
        profile=raw.get("profile"),
        capabilities=tuple(capabilities),
        budget=budget,
    )


def parse_delegation_intent_v2(payload_json: str) -> DelegationIntentV2:
    """Validate one v2 delegating-parent payload; return it typed.

    Strict shape: exactly the frozen field set (wait budgets optional);
    ``v`` is :data:`DELEGATION_PAYLOAD_VERSION_V2`; ``run`` is validated
    by the ONE agent-run producer; ``children`` is a non-empty bounded
    array of :class:`ChildSpecV2` specifications.
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
    if document["v"] != DELEGATION_PAYLOAD_VERSION_V2:
        raise _reject_payload(f"v must be {DELEGATION_PAYLOAD_VERSION_V2}")
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
        _parse_child_spec_v2(raw, index) for index, raw in enumerate(children_raw)
    )
    return DelegationIntentV2(
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


def parse_v2_child_payload(payload_json: str) -> V2ChildPayload:
    """Validate one v2 child-run payload; return it typed (ONE parser).

    The v2 child handler (default :data:`V2_CHILD_HANDLER_NAME`)
    consumes its queue payload through THIS parser — the delegation
    module owns the v2 child document, so no second validation
    vocabulary can drift from the planner's construction.
    """
    if not isinstance(payload_json, str):
        raise _reject_payload("v2 child payload must be a JSON string")
    try:
        document = json.loads(payload_json)
    except ValueError:
        raise _reject_payload("v2 child payload is not valid JSON") from None
    if not isinstance(document, Mapping):
        raise _reject_payload("v2 child payload must be a JSON object")
    allowed = {"v", "run", "server_ids", "budget"}
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise _reject_payload(f"unknown field {unknown[0]!r}")
    missing = allowed - set(document)
    if missing:
        raise _reject_payload(f"missing field {sorted(missing)[0]!r}")
    if document["v"] != DELEGATION_PAYLOAD_VERSION_V2:
        raise _reject_payload(f"v must be {DELEGATION_PAYLOAD_VERSION_V2}")
    try:
        run = parse_agent_run_intent(canonical_json(document["run"]))
    except AgentRunError:
        raise _reject_payload("run is not a valid agent-run payload") from None
    return V2ChildPayload(
        run=run,
        server_ids=_parse_server_ids(document["server_ids"], "server_ids"),
        budget=_parse_child_budget_bounds(document["budget"]),
    )


# ---------------------------------------------------------------------------
# child plan (pure): intersected grants, pinned server, parent scope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChildPlan:
    """One planned child: identity, re-validated payload, grant references.

    ``initiating_server_id`` is the v2 child's context pin (its
    ``job_runs.server_id``); ``None`` for v1 children, which always
    inherit the parent's server.
    """

    run_id: str
    payload_json: str
    grants: tuple[str, ...]
    initiating_server_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class DelegationPlan:
    """The whole plan: one payload per child, in specification order."""

    children: tuple[ChildPlan, ...]

    @property
    def child_run_ids(self) -> tuple[str, ...]:
        return tuple(child.run_id for child in self.children)


def _child_context(
    parent: AgentRunIntent,
    child_run_id: str,
    server_id: uuid.UUID | None = None,
) -> ExecutionContext:
    """The child's execution context: pinned server, named parent scope.

    C03 rule 2 + GRANT-INTERSECT: the child inherits the parent's
    principal, server binding, revision pins and credential REFERENCES;
    its ``run_id`` is its own identity and its ``parent_scope`` names the
    parent run + server (C10 cross-server tabletop: the parent's server
    binding stays visible in the child). v1 children pin the PARENT's
    server (no retargeting vocabulary); a v2 child pins its initiating
    server (``server_ids[0]``, validated inside the objective scope by
    :func:`plan_children_v2`).
    """
    pinned = parent.context.server_id if server_id is None else server_id
    return ExecutionContext(
        principal=parent.context.principal,
        server_id=pinned,
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
# v2 child plan (pure): scoped servers, intersected grants, narrowed budget
# ---------------------------------------------------------------------------


def plan_children_v2(
    intent: DelegationIntentV2,
    parent_row: ParentRunRow,
    child_ids: Sequence[str],
    *,
    scope_server_ids: Sequence[uuid.UUID],
    remaining_steps: int | None,
    remaining_tokens: int | None,
) -> DelegationPlan:
    """Construct the v2 child payloads (pure; fail closed; §7.2).

    Every child: a fresh caller-supplied identity, an initiating server
    that MUST sit inside the parent's effective objective server scope
    (a child's ``server_ids`` set ⊆ the parent's effective set — naming
    an out-of-scope server is the classified ``scope_violation``
    rejection, never a silent clamp), grants that are the INTERSECTION
    of the parent's granted set with the child's request (naming an
    ungranted capability is ``capability_expansion``; a request can
    never ADD), and budget bounds that cannot exceed the parent's
    REMAINING root budget (``budget_expansion``; an unbudgeted root has
    no remaining budget, so child bounds are rejected there). The v1
    gates are unchanged: depth (:data:`MAX_DELEGATION_DEPTH`), fan-out
    (:data:`MAX_CHILDREN_PER_RUN`) and the stored root budget's
    narrowing checks all apply before anything persists.
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
        budgets.check_nesting(parent_row.depth, budget)
        budgets.check_fanout(len(intent.children), budget)
    scope = frozenset(scope_server_ids)
    if not scope:
        raise DelegationError(
            DelegationErrorCode.SCOPE_VIOLATION,
            "the parent's effective objective scope is empty",
        )
    children: list[ChildPlan] = []
    for spec, child_run_id in zip(intent.children, child_ids, strict=True):
        validate_run_id_or_reject(child_run_id)
        child_servers = tuple(validate_run_id_or_reject(s) for s in spec.server_ids)
        scoped = frozenset(uuid.UUID(s) for s in child_servers)
        if not scoped <= scope:
            outside = sorted(
                (str(s) for s in scoped - scope),
            )
            raise DelegationError(
                DelegationErrorCode.SCOPE_VIOLATION,
                "child names a server outside the parent's objective "
                f"scope ({outside[0]})",
            )
        granted_set = set(intent.run.context.allowed_capabilities)
        for capability in spec.capabilities:
            if capability not in granted_set:
                raise DelegationError(
                    DelegationErrorCode.CAPABILITY_EXPANSION,
                    "child requests a capability outside the parent's "
                    f"grants ({capability})",
                )
        grants = _intersect(intent.run.context.allowed_capabilities, spec.capabilities)
        if spec.budget is not None:
            if remaining_steps is None or remaining_tokens is None:
                raise DelegationError(
                    DelegationErrorCode.BUDGET_EXPANSION,
                    "child carries budget bounds but the tree root is "
                    "unbudgeted (no remaining budget exists to bound)",
                )
            if (
                spec.budget.max_steps > remaining_steps
                or spec.budget.max_total_tokens > remaining_tokens
            ):
                raise DelegationError(
                    DelegationErrorCode.BUDGET_EXPANSION,
                    "child budget bounds exceed the parent's remaining "
                    "budget",
                )
        initiating = uuid.UUID(child_servers[0])
        context = _child_context(intent.run, child_run_id, initiating)
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
            "server_id": str(initiating),
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
        payload = {
            "v": DELEGATION_PAYLOAD_VERSION_V2,
            "run": document,
            "server_ids": list(child_servers),
            "budget": None if spec.budget is None else spec.budget.to_json(),
        }
        payload_json = canonical_json(payload)
        try:
            # The ONE v2 child parser re-validates our construction
            # before anything persists (identity binding included).
            parse_v2_child_payload(payload_json)
        except DelegationError as error:
            raise _reject_payload(
                f"constructed child payload rejected: {error.detail}"
            ) from None
        children.append(
            ChildPlan(
                run_id=child_run_id,
                payload_json=payload_json,
                grants=grants,
                initiating_server_id=initiating,
            )
        )
    return DelegationPlan(children=tuple(children))


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
                    # 0011 mandatory-tag invariant: every child is classified
                    # in the SAME transaction as its job_runs row;
                    # ``ON CONFLICT DO NOTHING`` keeps the crash-window replay
                    # idempotent (history-tags W1-FIX1).
                    await session.execute(
                        text(
                            """
                            INSERT INTO job_tags (job_id, tag_id,
                                                  assignment_source)
                            VALUES (:run_id, :tag, :source)
                            ON CONFLICT DO NOTHING
                            """
                        ),
                        {
                            "run_id": uuid.UUID(child.run_id),
                            "tag": uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
                            "source": SYSTEM_ASSIGNMENT_SOURCE,
                        },
                    )

    async def _aobjective_scope(
        self, parent_run_id: str
    ) -> tuple[str, frozenset[uuid.UUID]]:
        """Resolve the parent's v2 objective binding + scope (§3.2/§7.2).

        The scope authority is the STORE, never payload prose: the
        parent run's ``objective_runs`` binding names the episode and the
        episode's ``objective_servers`` rows ARE the effective scope. An
        unbound parent is the classified ``objective_unbound`` rejection
        (v2 delegation without an objective episode is a wiring bug).
        """
        async with self._sessions() as session:
            binding = (
                await session.execute(
                    text(
                        "SELECT objective_id FROM objective_runs "
                        "WHERE run_id = :r"
                    ),
                    {"r": uuid.UUID(parent_run_id)},
                )
            ).first()
            if binding is None:
                raise DelegationError(
                    DelegationErrorCode.OBJECTIVE_UNBOUND,
                    "the delegating parent is not bound to a v2 objective",
                )
            objective_id = str(binding[0])
            rows = (
                await session.execute(
                    text(
                        "SELECT server_id FROM objective_servers "
                        "WHERE objective_id = :o"
                    ),
                    {"o": uuid.UUID(objective_id)},
                )
            ).fetchall()
        if not rows:
            raise DelegationError(
                DelegationErrorCode.SCOPE_VIOLATION,
                "the bound objective has no in-scope servers",
            )
        scope = frozenset(uuid.UUID(str(row[0])) for row in rows)
        return objective_id, scope

    async def _aremaining_budget(self, run_id: str) -> tuple[int | None, int | None]:
        """The parent's remaining root budget ``(steps, tokens)``.

        ``(None, None)`` for an unbudgeted tree (no remaining budget
        exists, so v2 child budget bounds are unstatable there); the
        ledger's folded snapshot is the truth, never in-process memory.
        """
        parent_row = await self._aparent_row(run_id)
        if parent_row.root_budget is None:
            return (None, None)
        root_id = await self._aroot_run_id(run_id)
        state = await self._ledger.asnapshot(root_id)
        if state.is_unbudgeted:
            return (None, None)
        return (
            max(0, int(state.remaining_steps or 0)),
            max(0, int(state.remaining_tokens or 0)),
        )

    async def _ainsert_child_rows_v2(
        self,
        plan: DelegationPlan,
        objective_id: str,
        parent: DelegationIntentV2,
        parent_row: ParentRunRow,
    ) -> None:
        """Insert v2 child facade rows + objective bindings (§3.3/§7).

        One transaction, idempotent under recovery replay
        (``ON CONFLICT DO NOTHING`` everywhere): each child's
        ``job_runs`` row carries ITS OWN initiating server
        (``server_ids[0]``) with the same parent linkage the v1 rows
        hold, and each child is BOUND to the parent's objective
        (``objective_runs``: handler = the configured v2 child handler —
        the 0013 CHECK admits the frozen ``agent.run_v2`` identity;
        ``parent_run_id`` = the bound parent run; ``actor_kind`` =
        ``agent`` — the delegating parent agent performs the binding).
        """
        from mai.contracts.objectives import ActorKind

        depth = parent_row.depth + 1
        handler_name = v2_child_handler_name()
        async with self._sessions() as session:
            async with session.begin():
                for child in plan.children:
                    initiating = child.initiating_server_id
                    if initiating is None:
                        raise DelegationError(
                            DelegationErrorCode.INVALID_PAYLOAD,
                            "a v2 child plan carries no initiating server",
                        )
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
                            "server_id": initiating,
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
                    await session.execute(
                        text(
                            """
                            INSERT INTO job_tags (job_id, tag_id,
                                                  assignment_source)
                            VALUES (:run_id, :tag, :source)
                            ON CONFLICT DO NOTHING
                            """
                        ),
                        {
                            "run_id": uuid.UUID(child.run_id),
                            "tag": uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
                            "source": SYSTEM_ASSIGNMENT_SOURCE,
                        },
                    )
                    await session.execute(
                        text(
                            """
                            INSERT INTO objective_runs
                                (objective_id, run_id, parent_run_id,
                                 initiating_server_id, handler, actor_kind)
                            VALUES (:oid, :rid, :parent, :isid, :handler, :actor)
                            ON CONFLICT (objective_id, run_id) DO NOTHING
                            """
                        ),
                        {
                            "oid": uuid.UUID(objective_id),
                            "rid": uuid.UUID(child.run_id),
                            "parent": uuid.UUID(parent.run.run_id),
                            "isid": initiating,
                            "handler": handler_name,
                            "actor": ActorKind.AGENT.value,
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
    intent = _parse_intent(payload_json)
    executor = _resolve_delegation()
    return str(
        executor.run(executor.executor.steps.aobserve_run_started(intent.run.run_id))
    )


@DBOS.step(name="agents.delegate_child_ids")
def _delegation_child_ids(payload_json: str) -> str:
    """Generate the child identities ONCE (checkpointed; replay reuses)."""
    intent = _parse_intent(payload_json)
    return canonical_json(
        [str(uuid.uuid4()) for _ in intent.children]
    )


@DBOS.step(name="agents.delegate_children_planned")
def _delegation_children_planned(payload_json: str, ids_json: str) -> str:
    """Plan + persist the linkage; emit the safe planned event.

    One checkpoint: the pure planner intersects the grants and builds the
    child payloads (each re-validated by its ONE parser), the child
    ``job_runs`` rows commit with ``parent_run_id``/``depth`` linkage,
    and the parent stream records the planned child identities (no task
    content). A recovery replay observes the committed rows and returns
    the recorded plan.

    v1 payloads plan through :func:`plan_children` (the parent's server,
    no retargeting). v2 payloads resolve the parent's objective binding
    FIRST (the store is the scope authority) and plan through
    :func:`plan_children_v2` — scoped servers, intersected grants,
    narrowed budgets — then persist the per-child facade rows AND the
    ``objective_runs`` bindings in the same transaction.
    """
    intent = _parse_intent(payload_json)
    executor = _resolve_delegation()
    child_ids = tuple(str(item) for item in json.loads(ids_json))
    parent_row = executor.run(executor._aparent_row(intent.run.run_id))
    assert isinstance(parent_row, ParentRunRow)
    objective_id: str | None = None
    if isinstance(intent, DelegationIntentV2):
        objective_id, scope = executor.run(
            executor._aobjective_scope(intent.run.run_id)
        )
        remaining_steps, remaining_tokens = executor.run(
            executor._aremaining_budget(intent.run.run_id)
        )
        plan = plan_children_v2(
            intent,
            parent_row,
            child_ids,
            scope_server_ids=scope,
            remaining_steps=remaining_steps,
            remaining_tokens=remaining_tokens,
        )
        executor.run(
            executor._ainsert_child_rows_v2(plan, objective_id, intent, parent_row)
        )
    else:
        plan = plan_children(intent, parent_row, child_ids)
        executor.run(executor._ainsert_child_rows(plan, intent, parent_row))
    planned_data: dict[str, object] = {
        "children": list(plan.child_run_ids),
        "depth": parent_row.depth + 1,
    }
    if objective_id is not None:
        planned_data["objective_id"] = objective_id
    executor.run(
        executor._aemit(intent.run.run_id, DELEGATION_PLANNED_EVENT_TYPE, planned_data)
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
    intent = _parse_intent(payload_json)
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
    intent = _parse_intent(payload_json)
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
    intent = _parse_intent(payload_json)
    executor = _resolve_delegation()
    child_ids = tuple(str(item) for item in json.loads(ids_json))
    result = executor.run(executor._arelease_children(intent.run.run_id, child_ids))
    return canonical_json(dict(result))


@DBOS.step(name="agents.delegate_wait_entered")
def _delegation_wait_entered(payload_json: str, plan_json: str) -> str:
    """Park ``waiting_children`` once; persist + return the deadline."""
    intent = _parse_intent(payload_json)
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
    intent = _parse_intent(payload_json)
    executor = _resolve_delegation()
    resolutions = json.loads(resolutions_json)
    return str(executor.run(executor._achildren_resumed(intent.run.run_id, resolutions)))


@DBOS.step(name="agents.delegate_wait_resumed")
def _delegation_wait_resumed(payload_json: str) -> str:
    """Record ``waiting_children → running`` (deadline/cancel resume)."""
    intent = _parse_intent(payload_json)
    executor = _resolve_delegation()
    return str(executor.run(executor._aresume_from_wait(intent.run.run_id)))


@DBOS.step(name="agents.delegate_run_finished")
def _delegation_run_finished(payload_json: str, summary_json: str) -> str:
    """Record ``running → succeeded``; emit the safe summary event."""
    intent = _parse_intent(payload_json)
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
    intent = _parse_intent(payload_json)
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
    intent: DelegationIntent | DelegationIntentV2,
    child_ids: tuple[str, ...],
    deadline_iso: str,
    *,
    capture_receipts: bool = False,
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
    (its own stream holds the truth). With ``capture_receipts`` (the v2
    aggregation, §7.5) the SUCCEEDED child's returned summary document
    rides along as its ``receipt`` — the parent aggregates RECEIPTS,
    never child claims; failed/cancelled children carry no receipt.
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
                result = DBOS.retrieve_workflow(child_id).get_result()
                if capture_receipts and isinstance(result, Mapping):
                    record["receipt"] = dict(result)
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

    ONE handler, versioned payloads (runtime-v2.md §7.1: no second
    delegation engine): a payload ``v: 1`` parent keeps the FROZEN v1
    semantics byte-identical — children select :data:`mai.agents.run.
    HANDLER_NAME`, resolutions stay bare identity+state records. A
    payload ``v: 2`` parent (:class:`DelegationIntentV2`) resolves its
    objective scope at plan time, starts children under the configured
    v2 child handler (:func:`v2_child_handler_name`, default
    :data:`V2_CHILD_HANDLER_NAME`), and aggregates child RECEIPTS into
    its summary (§7.5).
    """
    is_v2 = _peek_delegation_version(payload_json) == DELEGATION_PAYLOAD_VERSION_V2
    intent: DelegationIntent | DelegationIntentV2 = (
        parse_delegation_intent_v2(payload_json)
        if is_v2
        else parse_delegation_intent(payload_json)
    )
    child_handler = v2_child_handler_name() if is_v2 else _run.HANDLER_NAME
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
                    _engine.run_workflow, child_handler, str(child_payload)
                )
        deadline = _delegation_wait_entered(payload_json, plan_json)
        try:
            resolutions = _await_children(
                intent, child_ids, deadline, capture_receipts=is_v2
            )
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
        # Receipt aggregation (§7.5; AC5): typed, value-free CHILD
        # RESOLUTIONS — engine-truth states plus the child's own summary
        # document when it succeeded. Child prose is never verification:
        # a failed/cancelled child keeps its honest state with no
        # receipt, and the counts ride the states, never the prose.
        settled: tuple[ChildResolution, ...] = tuple(
            ChildResolution.from_record(record) for record in resolutions
        )
        counts: dict[str, int] = {"succeeded": 0, "failed": 0, "cancelled": 0}
        for resolution in settled:
            state = resolution.state
            counts[state] = counts.get(state, 0) + 1
        summary: dict[str, object] = {
            "children": [resolution.to_record() for resolution in settled],
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
