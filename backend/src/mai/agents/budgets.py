"""Aggregate delegation budgets: the root-budget ledger over one job engine.

Card ``06-delegation-budgets`` against contracts.md **C05** rule 7 (max
nesting/concurrency/aggregate budget are explicit and owned HERE;
reservation/consumption is concurrency-safe; a child cap can never exceed
the root's remaining budget) and **C06** rule 5 (budget caps are
application-level). This module owns:

- **The frozen root-budget vocabulary** (:func:`parse_root_budget`) —
  the strict, value-free budget document frozen at submit in the
  ``job_runs.root_budget`` column (schema comment: "caps vocabulary owned
  by 06-delegation-budgets"). It bounds the delegation tree's NESTING
  (depth), FAN-OUT (children per delegation), CONCURRENCY (non-terminal
  reserved slots), and aggregate SPEND (durable steps and model tokens).
  A budget may only NARROW the module bounds declared HERE
  (``MAX_DELEGATION_DEPTH``, ``MAX_CHILDREN_PER_RUN``) — it can never
  widen them. A ``NULL`` root
  budget is a valid unbudgeted root: the module bounds still apply, no
  ledger events are written.
- **Pure enforcement gates** (:func:`check_nesting`,
  :func:`check_fanout`) — the fail-closed checks a delegation planner
  applies BEFORE any child identity is generated; a rejected gate
  persists nothing.
- **The atomic budget ledger** (:class:`BudgetLedger`) — reservations,
  releases and consumption are recorded as value-free events on the
  ROOT run's ``job_events`` stream (``agent.budget_reserved`` /
  ``agent.budget_released`` / ``agent.budget_charged``), written under
  the ONE whole-stream contract the reviewed steps/lifecycle producers
  use: next ``MAX(seq) + 1`` inside one transaction that first takes the
  root ``job_runs`` row lock. That lock is the aggregate-root
  serialization point: sibling sub-parents reserving concurrently
  (different processes, different connections) block on the same row, so
  the check-then-append pair is atomic — the tree can never hold more
  active slots, steps or tokens than its root budget allows, no matter
  how the delegation tree races. The ledger state is a fold over the
  root's replayable event stream (C05 rule 4), so every snapshot is
  derived from persisted truth, never from in-process memory.

Deliberate boundaries (recorded limitations, card evidence): the
delegation planner wiring lives with the consuming surface — this module
is the enforcement primitive + vocabulary the planner calls at its
plan/start/release points (the delegation planner propagates the
root budget verbatim, references only, exactly as its freeze declared).
Charges are value-free counts the calling producer reports (the
per-model token attribution is the agent-run surface's duty); the ledger
never reads task content. Unbudgeted roots are an explicit, observable
ledger state — not a silent pass-through. A release refunds only
OUTSTANDING (unconsumed) reservations; consumption already charged stays
charged — a cancelled tree frees its slots but never un-spends its
budget, and the charged events remain on the stream as the preserved,
value-free child evidence.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.jobs import EVENT_PAYLOAD_VERSION

#: Explicit delegation module bounds (C05 rule 7: explicit,
#: application-level; the budget vocabulary may only narrow these). A
#: parent may start at most this many children per delegation and only
#: below this tree depth. Owned HERE since the D09 deletion moved the
#: old ``delegate`` module bounds into their enforcement primitive; the
#: session-first delegation layer (``session_delegate``) narrows these
#: further with its own session bounds (depth 1, 3 children/parent).
MAX_CHILDREN_PER_RUN: Final[int] = 8
MAX_DELEGATION_DEPTH: Final[int] = 4

__all__ = [
    "BUDGET_PAYLOAD_VERSION",
    "BUDGET_CHARGED_EVENT_TYPE",
    "BUDGET_RELEASED_EVENT_TYPE",
    "BUDGET_RESERVED_EVENT_TYPE",
    "BUDGET_EVENT_TYPES",
    "MAX_ACTIVE_RUNS",
    "MAX_TOTAL_STEPS",
    "MAX_TOTAL_TOKENS",
    "BudgetError",
    "BudgetErrorCode",
    "BudgetLedger",
    "BudgetState",
    "MAX_CHILDREN_PER_RUN",
    "MAX_DELEGATION_DEPTH",
    "RootBudget",
    "canonical_json",
    "check_fanout",
    "check_nesting",
    "ledger_state",
    "parse_root_budget",
]

# ---------------------------------------------------------------------------
# frozen vocabulary + bounds
# ---------------------------------------------------------------------------

#: The budget document version this freeze parses (``job_runs.root_budget``
#: is frozen at submit; a document with a foreign ``v`` is rejected).
BUDGET_PAYLOAD_VERSION: Final[int] = 1

#: Event types this module owns in the shared per-run stream (C05 rule 4:
#: frozen envelope shape, per-producer type vocabulary; payloads are
#: value-free — run identities and integer counts, never task content).
#: All three live on the ROOT run's stream: the budget is a root concept
#: and the root's stream is the ledger's append-only log.
BUDGET_RESERVED_EVENT_TYPE: Final[str] = "agent.budget_reserved"
BUDGET_RELEASED_EVENT_TYPE: Final[str] = "agent.budget_released"
BUDGET_CHARGED_EVENT_TYPE: Final[str] = "agent.budget_charged"

#: The ledger's event-type vocabulary (the snapshot query's filter).
BUDGET_EVENT_TYPES: Final[tuple[str, ...]] = (
    BUDGET_RESERVED_EVENT_TYPE,
    BUDGET_RELEASED_EVENT_TYPE,
    BUDGET_CHARGED_EVENT_TYPE,
)

#: Absolute bound for the aggregate active-slot cap (a budget may pin any
#: value in ``1..MAX_ACTIVE_RUNS``; the delegation module's per-run bounds
#: stay the narrowers for depth/fan-out).
MAX_ACTIVE_RUNS: Final[int] = 32

#: Absolute bound for the aggregate durable-step budget (mirrors the
#: scale of the steps/agent-run per-run bounds, aggregated per tree).
MAX_TOTAL_STEPS: Final[int] = 10_000

#: Absolute bound for the aggregate model-token budget (C06 rule 3:
#: size/token budgets are explicit; the count is producer-reported).
MAX_TOTAL_TOKENS: Final[int] = 10_000_000


class BudgetErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_PAYLOAD = "invalid_payload"
    INVALID_WIRING = "invalid_wiring"
    RUN_NOT_FOUND = "run_not_found"
    DEPTH_EXHAUSTED = "delegation_depth_exhausted"
    FANOUT_EXHAUSTED = "fanout_exhausted"
    CHILD_CAP_EXCEEDS_BUDGET = "child_cap_exceeds_budget"
    ACTIVE_BUDGET_EXHAUSTED = "active_budget_exhausted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INVALID_RESERVATION = "invalid_reservation"
    LEDGER_STATE = "ledger_state"


class BudgetError(ValueError):
    """One structured, value-free budget rejection (fail closed)."""

    def __init__(self, code: BudgetErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def _reject_payload(detail: str) -> BudgetError:
    return BudgetError(BudgetErrorCode.INVALID_PAYLOAD, detail)


# ---------------------------------------------------------------------------
# the frozen budget document (strict, value-free, narrowing only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RootBudget:
    """The validated ``job_runs.root_budget`` document (C05 rules 3/7).

    Every cap NARROWS a module bound: ``max_depth`` the delegation
    module's :data:`MAX_DELEGATION_DEPTH`, ``max_children`` its
    :data:`MAX_CHILDREN_PER_RUN`; ``max_active``/``max_steps``/
    ``max_total_tokens`` are tree-aggregate caps bounded by this module's
    absolute bounds.
    """

    max_depth: int
    max_children: int
    max_active: int
    max_steps: int
    max_total_tokens: int

    @property
    def allowed_depth(self) -> int:
        """The effective nesting limit (never above the module bound)."""
        return min(MAX_DELEGATION_DEPTH, self.max_depth)

    @property
    def allowed_children(self) -> int:
        """The effective per-delegation fan-out limit."""
        return min(MAX_CHILDREN_PER_RUN, self.max_children)


def _bounded_cap(raw: object, field: str, maximum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _reject_payload(f"{field} must be an integer")
    if not 1 <= raw <= maximum:
        raise _reject_payload(f"{field} outside its bounded range 1..{maximum}")
    return raw


def parse_root_budget(document: object) -> RootBudget | None:
    """Validate one root-budget document; ``None`` = unbudgeted root.

    Strict shape: a ``None`` document (the column's NULL) is the explicit
    unbudgeted root; anything present must be exactly the frozen field
    set at this freeze's version, with every cap an in-bound integer.
    """
    if document is None:
        return None
    if not isinstance(document, Mapping):
        raise _reject_payload("root budget must be a JSON object or null")
    allowed = {
        "v",
        "max_depth",
        "max_children",
        "max_active",
        "max_steps",
        "max_total_tokens",
    }
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise _reject_payload(f"unknown field {unknown[0]!r}")
    missing = allowed - set(document)
    if missing:
        raise _reject_payload(f"missing field {sorted(missing)[0]!r}")
    if document["v"] != BUDGET_PAYLOAD_VERSION:
        raise _reject_payload(f"v must be {BUDGET_PAYLOAD_VERSION}")
    return RootBudget(
        max_depth=_bounded_cap(document["max_depth"], "max_depth", MAX_DELEGATION_DEPTH),
        max_children=_bounded_cap(
            document["max_children"], "max_children", MAX_CHILDREN_PER_RUN
        ),
        max_active=_bounded_cap(document["max_active"], "max_active", MAX_ACTIVE_RUNS),
        max_steps=_bounded_cap(document["max_steps"], "max_steps", MAX_TOTAL_STEPS),
        max_total_tokens=_bounded_cap(
            document["max_total_tokens"], "max_total_tokens", MAX_TOTAL_TOKENS
        ),
    )


def canonical_json(payload: object) -> str:
    """Deterministic JSON text (sort_keys, tight separators, ASCII).

    Same canonical form as ``mai.jobs.steps.canonical_json``; owned here
    since the D09 deletion removed the old ``agents.run`` definition this
    module imported.
    """
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"not JSON-canonicalizable ({type(error).__name__})"
        ) from None


def check_nesting(parent_depth: int, budget: RootBudget | None) -> int:
    """Fail closed on excessive nesting; return the child depth.

    The child would live at ``parent_depth + 1``; the effective limit is
    the budget's ``max_depth`` narrowed by the delegation module's
    :data:`MAX_DELEGATION_DEPTH` (an unbudgeted tree keeps the module
    bound alone). Rejections persist nothing.
    """
    if isinstance(parent_depth, bool) or not isinstance(parent_depth, int):
        raise _reject_payload("parent_depth must be an integer")
    if parent_depth < 0:
        raise _reject_payload("parent_depth must be >= 0")
    limit = budget.allowed_depth if budget is not None else MAX_DELEGATION_DEPTH
    child_depth = parent_depth + 1
    if child_depth > limit:
        raise BudgetError(
            BudgetErrorCode.DEPTH_EXHAUSTED,
            f"child depth {child_depth} exceeds the delegation nesting "
            f"budget ({limit})",
        )
    return child_depth


def check_fanout(count: int, budget: RootBudget | None) -> int:
    """Fail closed on excessive fan-out; return the validated count.

    One delegation may start at most the budget's ``max_children``
    narrowed by :data:`mai.agents.budgets.MAX_CHILDREN_PER_RUN`.
    """
    if isinstance(count, bool) or not isinstance(count, int):
        raise _reject_payload("child count must be an integer")
    limit = budget.allowed_children if budget is not None else MAX_CHILDREN_PER_RUN
    if not 1 <= count <= limit:
        raise BudgetError(
            BudgetErrorCode.FANOUT_EXHAUSTED,
            f"a delegation starts 1..{limit} children, not {count}",
        )
    return count


# ---------------------------------------------------------------------------
# the ledger fold (pure; the replayable stream state)
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """UTC now (the event ``ts``; stored UTC per C01 rule 5)."""
    return datetime.now(timezone.utc)


def _canonical_run_id(value: object, detail: str) -> str:
    if not isinstance(value, str):
        raise BudgetError(BudgetErrorCode.LEDGER_STATE, detail)
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise BudgetError(BudgetErrorCode.LEDGER_STATE, detail) from None
    if str(parsed) != value:
        raise BudgetError(BudgetErrorCode.LEDGER_STATE, detail)
    return value


@dataclass(frozen=True, slots=True)
class BudgetState:
    """The folded ledger state of one root budget (C05 rule 7 truth).

    ``budget is None`` marks the explicit unbudgeted root. ``outstanding``
    holds the child run identities with an UNRELEASED reservation (the
    tree's active-slot occupancy); ``consumed_*`` accumulate the charged
    spend (never refunded — see the release policy in the module
    docstring).
    """

    budget: RootBudget | None
    outstanding: tuple[str, ...]
    consumed_steps: int
    consumed_tokens: int

    @property
    def is_unbudgeted(self) -> bool:
        return self.budget is None

    @property
    def active_reserved(self) -> int:
        return len(self.outstanding)

    @property
    def remaining_active(self) -> int | None:
        if self.budget is None:
            return None
        return self.budget.max_active - len(self.outstanding)

    @property
    def remaining_steps(self) -> int | None:
        if self.budget is None:
            return None
        return self.budget.max_steps - self.consumed_steps

    @property
    def remaining_tokens(self) -> int | None:
        if self.budget is None:
            return None
        return self.budget.max_total_tokens - self.consumed_tokens


#: The one unbudgeted-root truth (an explicit, observable state — never a
#: silent pass-through).
_UNBUDGETED: Final[BudgetState] = BudgetState(
    budget=None, outstanding=(), consumed_steps=0, consumed_tokens=0
)


def _fold_children(raw: object, detail: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise BudgetError(BudgetErrorCode.LEDGER_STATE, detail)
    children: list[str] = []
    for item in raw:
        child = _canonical_run_id(item, detail)
        if child in children:
            raise BudgetError(BudgetErrorCode.LEDGER_STATE, detail)
        children.append(child)
    return tuple(children)


def _fold_reserved(data: Mapping[str, object]) -> tuple[str, ...]:
    detail = "reserved event payload does not match the frozen shape"
    if not isinstance(data, Mapping) or set(data) != {"owner", "children"}:
        raise BudgetError(BudgetErrorCode.LEDGER_STATE, detail)
    _canonical_run_id(data["owner"], detail)
    return _fold_children(data["children"], detail)


def _fold_released(data: Mapping[str, object]) -> tuple[str, ...]:
    detail = "released event payload does not match the frozen shape"
    if not isinstance(data, Mapping) or set(data) != {"owner", "children"}:
        raise BudgetError(BudgetErrorCode.LEDGER_STATE, detail)
    _canonical_run_id(data["owner"], detail)
    return _fold_children(data["children"], detail)


def _fold_charged(data: Mapping[str, object]) -> tuple[int, int]:
    detail = "charged event payload does not match the frozen shape"
    if not isinstance(data, Mapping) or set(data) != {"run", "steps", "tokens"}:
        raise BudgetError(BudgetErrorCode.LEDGER_STATE, detail)
    _canonical_run_id(data["run"], detail)
    steps, tokens = data["steps"], data["tokens"]
    if isinstance(steps, bool) or not isinstance(steps, int) or steps < 0:
        raise BudgetError(BudgetErrorCode.LEDGER_STATE, detail)
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise BudgetError(BudgetErrorCode.LEDGER_STATE, detail)
    if steps == 0 and tokens == 0:
        raise BudgetError(BudgetErrorCode.LEDGER_STATE, detail)
    return steps, tokens


def ledger_state(
    budget: RootBudget | None, data_items: Iterable[object]
) -> BudgetState:
    """Fold the root's budget events (in stream ``seq`` order) to truth.

    The pure core of every snapshot: reservations add their children to
    the outstanding set, releases remove them (a release of a child that
    is not outstanding is a corrupt/lying stream — rejected, never a
    negative count), charges accumulate. Unrecognized payloads are a
    hard error: the ledger never guesses across a tampered stream.
    """
    outstanding: list[str] = []
    consumed_steps = 0
    consumed_tokens = 0
    for item in data_items:
        if not isinstance(item, Mapping) or "type" not in item or "data" not in item:
            raise BudgetError(
                BudgetErrorCode.LEDGER_STATE,
                "ledger fold needs typed event payloads",
            )
        event_type, data = item["type"], item["data"]
        if event_type == BUDGET_RESERVED_EVENT_TYPE:
            for child in _fold_reserved(data):
                if child in outstanding:
                    raise BudgetError(
                        BudgetErrorCode.LEDGER_STATE,
                        f"child {child} reserved while already outstanding",
                    )
                outstanding.append(child)
        elif event_type == BUDGET_RELEASED_EVENT_TYPE:
            for child in _fold_released(data):
                if child not in outstanding:
                    raise BudgetError(
                        BudgetErrorCode.LEDGER_STATE,
                        f"release of {child} with no outstanding reservation",
                    )
                outstanding.remove(child)
        elif event_type == BUDGET_CHARGED_EVENT_TYPE:
            steps, tokens = _fold_charged(data)
            consumed_steps += steps
            consumed_tokens += tokens
        else:
            raise BudgetError(
                BudgetErrorCode.LEDGER_STATE,
                f"unrecognized budget event type {event_type!r}",
            )
    return BudgetState(
        budget=budget,
        outstanding=tuple(sorted(outstanding)),
        consumed_steps=consumed_steps,
        consumed_tokens=consumed_tokens,
    )


# ---------------------------------------------------------------------------
# the atomic ledger (one transaction per operation, root row lock)
# ---------------------------------------------------------------------------


def _validate_run_ids(run_ids: Sequence[str], field: str) -> tuple[str, ...]:
    if not isinstance(run_ids, (list, tuple)) or not run_ids:
        raise _reject_payload(f"{field} must be a non-empty sequence of run ids")
    canonical: list[str] = []
    for item in run_ids:
        if not isinstance(item, str):
            raise _reject_payload(f"{field} entries must be canonical UUID strings")
        try:
            parsed = uuid.UUID(item)
        except ValueError:
            raise _reject_payload(
                f"{field} entries must be canonical UUID strings"
            ) from None
        if str(parsed) != item:
            raise _reject_payload(f"{field} entries must be canonical UUID strings")
        if item in canonical:
            raise _reject_payload(f"{field} entries must be distinct")
        canonical.append(item)
    return tuple(canonical)


def _validate_charge(steps: int, tokens: int) -> tuple[int, int]:
    for name, value, maximum in (
        ("steps", steps, MAX_TOTAL_STEPS),
        ("tokens", tokens, MAX_TOTAL_TOKENS),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _reject_payload(f"charge {name} must be a non-negative integer")
        if value > maximum:
            raise _reject_payload(f"charge {name} exceeds its absolute bound")
    if steps == 0 and tokens == 0:
        raise _reject_payload("a charge carries steps or tokens, not both zero")
    return steps, tokens


class BudgetLedger:
    """The concurrency-safe root-budget ledger over facade-owned state.

    Every operation runs as ONE transaction that first takes the ROOT
    run's ``job_runs`` row lock (``SELECT ... FOR UPDATE``) — the same
    allocation contract the reviewed event writers use — then folds the
    root's existing budget events, validates the operation against the
    folded truth, and appends exactly one event with the next whole-stream
    ``MAX(seq) + 1`` cursor. Racing siblings serialize on the root row:
    the check-then-append pair is atomic across processes, connections
    and workers, so the aggregate caps cannot be oversubscribed (C05
    rule 7). No new tables; the ledger lives entirely in the two
    facade-owned tables (``job_runs.root_budget`` + the root's
    ``job_events`` stream) and replays from them.

    Wiring is constructor-checked (fail-closed, the sibling pattern). An
    unbudgeted root (``root_budget IS NULL``) is an explicit no-op state:
    every operation returns the unbudgeted :class:`BudgetState` without
    writing an event — the delegation module's own bounds remain the
    only enforcement for such trees.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        if not isinstance(sessions, async_sessionmaker):
            raise BudgetError(
                BudgetErrorCode.INVALID_WIRING,
                "sessions must be an async_sessionmaker",
            )
        self._sessions = sessions

    # -- shared transaction core -------------------------------------------

    async def _alocked_root(
        self, session: AsyncSession, run_id: str
    ) -> RootBudget | None:
        """Lock the root row; return its budget (None = unbudgeted)."""
        row = (
            await session.execute(
                text("SELECT root_budget FROM job_runs WHERE run_id = :r FOR UPDATE"),
                {"r": uuid.UUID(run_id)},
            )
        ).mappings().first()
        if row is None:
            raise BudgetError(
                BudgetErrorCode.RUN_NOT_FOUND,
                "no job_runs row for the budget root",
            )
        try:
            return parse_root_budget(row["root_budget"])
        except BudgetError as error:
            raise BudgetError(
                BudgetErrorCode.LEDGER_STATE,
                f"the frozen root budget does not parse: {error.detail}",
            ) from None

    async def _afold(self, session: AsyncSession, run_id: str) -> list[dict[str, object]]:
        reserved, released, charged = BUDGET_EVENT_TYPES
        rows = await session.execute(
            text(
                "SELECT type, data FROM job_events "
                "WHERE run_id = :r AND type IN (:t0, :t1, :t2) ORDER BY seq"
            ),
            {
                "r": uuid.UUID(run_id),
                "t0": reserved,
                "t1": released,
                "t2": charged,
            },
        )
        return [dict(row) for row in rows.mappings()]

    async def _aappend(
        self,
        session: AsyncSession,
        run_id: str,
        event_type: str,
        data: Mapping[str, object],
    ) -> int:
        """Append one event at the next whole-stream cursor (C05 rule 4)."""
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
                "ts": _utcnow(),
                "type": event_type,
                "v": EVENT_PAYLOAD_VERSION,
                "data": canonical_json(dict(data)),
            },
        )
        return next_seq

    # -- reservation (atomic; the active-slot gate) ------------------------

    async def areserve(
        self,
        run_id: str,
        owner_run_id: str,
        child_ids: Sequence[str],
    ) -> BudgetState:
        """Reserve active slots for one delegation; return the new truth.

        ``owner_run_id`` is the delegating run; ``child_ids`` are the
        planned child identities (distinct, canonical). Fail closed on:
        fan-out beyond the narrowed cap, a delegation that could never
        fit the budget at all, and any size that would oversubscribe the
        root's remaining active slots. A reservation replayed with the
        SAME owner and children (a recovery window) is an idempotent
        no-op; a partial overlap is a hard inconsistency. Nothing is
        written on any rejection.
        """
        children = _validate_run_ids(child_ids, "child_ids")
        (owner,) = _validate_run_ids([owner_run_id], "owner_run_id")
        async with self._sessions() as session:
            async with session.begin():
                budget = await self._alocked_root(session, run_id)
                if budget is None:
                    return _UNBUDGETED
                check_fanout(len(children), budget)
                state = ledger_state(budget, await self._afold(session, run_id))
                outstanding = set(state.outstanding)
                overlap = outstanding.intersection(children)
                if overlap:
                    if outstanding == set(children):
                        return state  # idempotent replay of the same reservation
                    raise BudgetError(
                        BudgetErrorCode.INVALID_RESERVATION,
                        "the reservation partially overlaps an outstanding one",
                    )
                if len(children) > budget.max_active:
                    raise BudgetError(
                        BudgetErrorCode.CHILD_CAP_EXCEEDS_BUDGET,
                        f"a delegation of {len(children)} children can never "
                        f"fit the root's active budget ({budget.max_active})",
                    )
                if len(outstanding) + len(children) > budget.max_active:
                    raise BudgetError(
                        BudgetErrorCode.ACTIVE_BUDGET_EXHAUSTED,
                        f"remaining active budget "
                        f"({budget.max_active - len(outstanding)}) cannot "
                        f"reserve {len(children)} children",
                    )
                await self._aappend(
                    session,
                    run_id,
                    BUDGET_RESERVED_EVENT_TYPE,
                    {"owner": owner, "children": sorted(children)},
                )
                return ledger_state(budget, await self._afold(session, run_id))

    # -- release (the cancellation/refund policy) --------------------------

    async def arelease(
        self,
        run_id: str,
        owner_run_id: str,
        child_ids: Sequence[str],
    ) -> BudgetState:
        """Release outstanding reservations (cancelled/failed branch).

        The frozen cascade policy's ledger half: when a delegating run is
        cancelled or fails, its unused reservations are refunded so the
        tree's remaining budget stays truthful. Only OUTSTANDING children
        of the fold may be released — a release referencing an unknown or
        already-released child is a hard ``ledger_state`` rejection (the
        stream never lies, never goes negative). Charged consumption is
        NOT refunded: its events remain on the stream as the preserved
        value-free child evidence.
        """
        children = _validate_run_ids(child_ids, "child_ids")
        (owner,) = _validate_run_ids([owner_run_id], "owner_run_id")
        async with self._sessions() as session:
            async with session.begin():
                budget = await self._alocked_root(session, run_id)
                if budget is None:
                    return _UNBUDGETED
                state = ledger_state(budget, await self._afold(session, run_id))
                outstanding = set(state.outstanding)
                unknown = [child for child in children if child not in outstanding]
                if unknown:
                    raise BudgetError(
                        BudgetErrorCode.LEDGER_STATE,
                        f"release of {unknown[0]} with no outstanding reservation",
                    )
                await self._aappend(
                    session,
                    run_id,
                    BUDGET_RELEASED_EVENT_TYPE,
                    {"owner": owner, "children": sorted(children)},
                )
                return ledger_state(budget, await self._afold(session, run_id))

    # -- consumption (atomic; the aggregate spend gate) --------------------

    async def aconsume(
        self,
        run_id: str,
        charging_run_id: str,
        *,
        steps: int = 0,
        tokens: int = 0,
    ) -> BudgetState:
        """Charge durable steps / model tokens against the root budget.

        Fail closed: a charge that would push the tree's aggregate spend
        past its caps is rejected whole (no partial charging), leaving
        the fold untouched for the caller's honest failure path. Charges
        are permanent (never refunded) and value-free: run identity plus
        integer counts.
        """
        step_count, token_count = _validate_charge(steps, tokens)
        (charging,) = _validate_run_ids([charging_run_id], "charging_run_id")
        async with self._sessions() as session:
            async with session.begin():
                budget = await self._alocked_root(session, run_id)
                if budget is None:
                    return _UNBUDGETED
                state = ledger_state(budget, await self._afold(session, run_id))
                if state.consumed_steps + step_count > budget.max_steps:
                    raise BudgetError(
                        BudgetErrorCode.BUDGET_EXHAUSTED,
                        f"tree step budget exhausted "
                        f"({state.consumed_steps} consumed of "
                        f"{budget.max_steps}; refusing {step_count})",
                    )
                if state.consumed_tokens + token_count > budget.max_total_tokens:
                    raise BudgetError(
                        BudgetErrorCode.BUDGET_EXHAUSTED,
                        f"tree token budget exhausted "
                        f"({state.consumed_tokens} consumed of "
                        f"{budget.max_total_tokens}; refusing {token_count})",
                    )
                await self._aappend(
                    session,
                    run_id,
                    BUDGET_CHARGED_EVENT_TYPE,
                    {
                        "run": charging,
                        "steps": step_count,
                        "tokens": token_count,
                    },
                )
                return ledger_state(budget, await self._afold(session, run_id))

    # -- snapshot (read-only fold) -----------------------------------------

    async def asnapshot(self, run_id: str) -> BudgetState:
        """Fold the root's budget truth (read-only; no row lock)."""
        if not isinstance(run_id, str):
            raise _reject_payload("run_id must be a string")
        try:
            parsed = uuid.UUID(run_id)
        except ValueError:
            raise _reject_payload("run_id must be a canonical UUID") from None
        if str(parsed) != run_id:
            raise _reject_payload("run_id must be canonical form")
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text("SELECT root_budget FROM job_runs WHERE run_id = :r"),
                    {"r": parsed},
                )
            ).mappings().first()
            if row is None:
                raise BudgetError(
                    BudgetErrorCode.RUN_NOT_FOUND,
                    "no job_runs row for the budget root",
                )
            budget = parse_root_budget(row["root_budget"])
            return ledger_state(budget, await self._afold(session, run_id))
