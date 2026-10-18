"""Restart reconciliation + safe cancellation (card ``05-cancel-recovery``).

The application-side reconciliation owner for runs whose durable workflow
can no longer answer for itself (contracts.md **C04** rules 5–7, **C05**
rule 2, job-engine guard rules 2–4 and 8; the siblings this composes:
``05-run-lifecycle`` transitions, ``05-steps`` durable rows, ``05-resource-
locks`` leases, ``05-engine-adapter`` engine truth):

- **Restart inspection** — after a worker restart every outstanding
  ``job_steps`` row of a non-terminal run is classified against the
  facade state (the replayed event log) and the engine state: terminal
  rows are resolved history; receipt-less ``pending`` rows on a parked
  ``waiting_approval`` run are the approval wait SURVIVING the restart
  (acceptance 2 — nothing disturbs them, the recovered workflow re-parks
  and re-reads the durable decision); ``running`` rows whose engine
  workflow can no longer translate them are the crash window (guard rule
  2); ``outcome_unknown`` rows are unresolved effects (they pause the run
  before any retry — the workflow parks itself in ``reconciling``, the
  facade sweep parks an engine-orphaned facade); ``cancel_requested``
  rows await host-verified cancellation (guard rule 3).

- **Certification** — a crash-window row the engine will never replay
  (engine workflow terminal or absent) is translated to
  ``outcome_unknown`` with a crash receipt that references the still-held
  lease, exactly the executor's own recovered-attempt translation
  (:meth:`mai.jobs.steps.StepExecutor.acrash_window_outcome` semantics at
  facade level). While the engine workflow may still claim the step the
  certification REFUSES — restart replay owns that window, and a facade
  park there would desync the replayed wait bookkeeping.

- **Resolution** — the ONLY writer of the ``outcome_unknown → terminal``
  step edges (the runner only reads them). A resolution requires a FINAL
  host verdict (:class:`mai.execution.reconcile.ReconciliationClass`):
  ``running`` and every uncertain class are rejected value-free and the
  row stays open (C04 rule 7: lease expiry never authorizes a duplicate
  mutation, and an unresolved effect is never finalized). On a final
  verdict the receipt-recorded lease is disposed: a live lease of the
  recorded owner is released; a lapsed lease is fenced by its first touch
  and the fence cleared UNDER the terminal verdict
  (:meth:`mai.jobs.resource_locks.ResourceLockService.clear_fence`). The
  parked run is woken on ``mai.jobs.steps.RESUME_TOPIC`` (best effort —
  the parked loop always re-reads durable state on its poll, so a lost
  wake degrades to the poll cadence, never to a wrong outcome).

- **Safe cancellation** — a cancel request is facade-recorded ONCE (a
  ``run.cancel_requested`` event carrying the requested timestamp;
  ``cancel_requested`` is NOT a run state, C05 rule 2), then the engine
  cancel is requested (requested ≠ verified: in-flight non-preemptible
  steps complete and their effects happen — engine adapter guard rules
  3–4) and the caller BLOCKS until the engine outcome is terminal. The
  outcome is only ever reported from the engine's own terminal status —
  a timeout raises without claiming ``cancelled`` (acceptance 2). After
  the engine lands, the engine truth is applied to the facade diagram
  (idempotently; terminal is immutable), open step rows are certified or
  resolved through the host-verdict source, and every disposition is
  reported: resolved rows with their lock outcome, and — honestly kept
  open — steps whose host truth is still missing together with the locks
  that stay fenced until that verdict exists.

- **A run is never re-dispatched here.** Retry is the engine's deliberate
  resume (guard rule 8) over rows that are resume-first (a persisted
  terminal/unknown row IS the answer); this module only makes the rows
  truthful and wakes what parks on them.

Composition follows the sibling services: the engine client and lock
service are constructed from the one production URL contract (or
injected for tests); every DBOS-free async seam is plain SQLAlchemy over
Machina's Postgres; every rejection is one value-free
:class:`RecoveryError` safe to log, store and surface (C01 rule 3); no
secret or payload value ever enters receipts, events, notes or
exceptions. The ``dbos`` engine is never launched here — the module is
safe in control-plane processes (worker composition injects its own
executor separately).

Host truth enters through :class:`VerdictSource` — the reviewed
``04-reconcile`` public API produces verdicts on real targets; the jobs
facade never invents one and never speaks SSH itself. An absent source
leaves rows open and REPORTED, never guessed.
"""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Final, Protocol

import psycopg
from dbos import DBOSClient
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.jobs import (
    EVENT_PAYLOAD_VERSION,
    TERMINAL_RUN_STATES,
    TERMINAL_STEP_STATES,
    RunState,
    StepState,
)
from mai.execution.reconcile import ReconciliationClass
from mai.jobs.engine import (
    APPLICATION_NAME,
    EngineClient,
    EngineError,
    validate_run_id,
)
from mai.jobs.lifecycle import RunLifecycle, RunLifecycleError
from mai.jobs.resource_locks import (
    LOCK_TABLE,
    LockLease,
    LockSnapshot,
    ResourceLockError,
    ResourceLockService,
)
from mai.jobs.steps import (
    CRASH_RECOVERY_REASON,
    RESUME_TOPIC,
    STEP_COMPLETED_EVENT_TYPE,
)

__all__ = [
    "CANCEL_REQUESTED_EVENT_TYPE",
    "DEFAULT_CANCEL_OUTCOME_TIMEOUT",
    "DEFAULT_CANCEL_POLL_SECONDS",
    "LOCK_RESOURCE_UNPROVEN",
    "MAX_CANCEL_OUTCOME_TIMEOUT",
    "MAX_CANCEL_POLL_SECONDS",
    "MAX_NOTE_CHARS",
    "MAX_SOURCE_CHARS",
    "MAX_SWEEP_RUNS",
    "MIN_CANCEL_OUTCOME_TIMEOUT",
    "MIN_CANCEL_POLL_SECONDS",
    "RECOVERY_PARKED_EVENT_TYPE",
    "RESOLUTION_REASON",
    "VERDICT_STEP_STATES",
    "CancelResult",
    "FindingClass",
    "LockDisposition",
    "RecoveryError",
    "RecoveryErrorCode",
    "RecoveryService",
    "ResolveResult",
    "RunInspection",
    "StepFinding",
    "VerdictSource",
]


# ---------------------------------------------------------------------------
# frozen vocabulary + bounds
# ---------------------------------------------------------------------------

#: The event type this module owns for a cancellation request (C05 rule 4
#: lowercase dotted slug; the envelope SHAPE is frozen, the type vocabulary
#: stays open). The event IS the facade-recorded ``cancel_requested``
#: timestamp — a run-state value does not exist for it (C05 rule 2).
CANCEL_REQUESTED_EVENT_TYPE: Final[str] = "run.cancel_requested"

#: The event type recording a facade-side recovery park of an engine-
#: orphaned run (the workflow's own ``reconciling`` park is narrated by the
#: step executor's wait events; this one has no workflow behind it).
RECOVERY_PARKED_EVENT_TYPE: Final[str] = "run.recovery_parked"

#: The receipt ``reason`` recorded on a reconciliation resolution (the
#: resolution receipt keys are the executor's safe-vocabulary style).
RESOLUTION_REASON: Final[str] = "reconciliation"

#: Receipt ``effect`` value for a certified crash window (matches the
#: dispatcher's unknown-effect vocabulary).
LOCK_RESOURCE_UNPROVEN: Final[str] = "unknown"

#: The verdict→step-state table. ONLY the frozen terminal host verdicts
#: may drive an uncertain row to its terminal answer; every other class is
#: rejected by :meth:`RecoveryService.aresolve_step` (C04 rule 7).
VERDICT_STEP_STATES: Final[Mapping[ReconciliationClass, StepState]] = {
    ReconciliationClass.FINAL_SUCCEEDED: StepState.SUCCEEDED,
    ReconciliationClass.FINAL_FAILED: StepState.FAILED,
    ReconciliationClass.FINAL_CANCELLED: StepState.CANCELLED,
}

#: Default wall-clock budget for :meth:`RecoveryService.acancel_run` to
#: observe a terminal engine outcome after the request (the engine lands
#: the cancellation at the next step boundary; an in-flight step may take
#: its own effect time — the budget is a caller knob, not a guess).
DEFAULT_CANCEL_OUTCOME_TIMEOUT: Final[float] = 300.0
MIN_CANCEL_OUTCOME_TIMEOUT: Final[float] = 0.5
MAX_CANCEL_OUTCOME_TIMEOUT: Final[float] = 24 * 3600.0

#: Engine-outcome poll cadence for the cancel wait (bounded like the
#: sibling wait budgets).
DEFAULT_CANCEL_POLL_SECONDS: Final[float] = 1.0
MIN_CANCEL_POLL_SECONDS: Final[float] = 0.05
MAX_CANCEL_POLL_SECONDS: Final[float] = 60.0

#: Upper bound for one whole-fleet inspection sweep (the sweep is an
#: explicit operator/restart operation, not an unbounded cursor).
MAX_SWEEP_RUNS: Final[int] = 500

MAX_SOURCE_CHARS: Final[int] = 100
MAX_NOTE_CHARS: Final[int] = 500

_CONNECT_TIMEOUT_S: Final[int] = 5


class RecoveryErrorCode(StrEnum):
    """Value-free rejection codes (C01 envelope vocabulary)."""

    INVALID_RUN_ID = "invalid_run_id"
    RUN_NOT_FOUND = "run_not_found"
    #: The engine has no workflow under this run ID (cancel/inspect lag).
    ENGINE_UNKNOWN_RUN = "engine_unknown_run"
    STEP_NOT_FOUND = "step_not_found"
    #: The step row is not open for reconciliation (already resolved,
    #: never attempted, or still owned by a live attempt).
    STEP_NOT_UNCERTAIN = "step_not_uncertain"
    #: The verdict value is not a ReconciliationClass.
    INVALID_VERDICT = "invalid_verdict"
    #: The verdict does not prove the prior effect terminal (C04 rule 7).
    VERDICT_UNRESOLVED = "verdict_unresolved"
    #: The run/facade state does not permit the requested recovery action.
    INVALID_RUN_STATE = "invalid_run_state"
    #: A guarded step transition lost its single-owner race to a live
    #: writer — re-read instead of overwriting (integrity, never silent).
    STEP_STATE_GUARD = "step_state_guard"
    INVALID_SOURCE = "invalid_source"
    INVALID_NOTE = "invalid_note"
    INVALID_TIMEOUT = "invalid_timeout"
    #: The engine outcome was not observed within the budget; the request
    #: stays recorded and re-entry resumes the wait. ``cancelled`` is
    #: NEVER reported by a timeout (guard rule 3).
    CANCEL_OUTCOME_TIMEOUT = "cancel_outcome_timeout"


class RecoveryError(RuntimeError):
    """One value-free recovery rejection: code + fixed reason.

    Messages carry codes, field names and opaque identifiers only — never
    payload values — so they are safe to log, store and surface (C01
    rule 3, the sibling envelope discipline).
    """

    def __init__(self, code: RecoveryErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class FindingClass(StrEnum):
    """What one persisted step row means for recovery (inspection view)."""

    #: Terminal row — resolved history; nothing to recover.
    RESOLVED = "resolved"
    #: Receipt-less ``pending`` row not parked on an approval — not yet
    #: attempted (or orderly reverted); nothing to recover.
    PENDING = "pending"
    #: Receipt-less ``pending`` row on a ``waiting_approval`` run — the
    #: approval wait SURVIVES restart (acceptance 2); never disturbed.
    AWAITING_APPROVAL = "awaiting_approval"
    #: ``running`` fence marker with a LIVE engine workflow — an attempt
    #: in flight; its window belongs to that workflow (restart replay
    #: translates a crash, the attempt itself lands the outcome).
    ACTIVE = "active"
    #: ``running`` fence marker whose engine workflow can no longer claim
    #: the step (terminal or absent) — certifiable to ``outcome_unknown``.
    CRASH_WINDOW = "crash_window"
    #: ``outcome_unknown`` row — an unproven effect that pauses the run
    #: before any retry until a terminal host verdict resolves it.
    UNCERTAIN = "uncertain"
    #: ``cancel_requested`` row — awaiting host-verified cancellation.
    CANCEL_PENDING = "cancel_pending"


class LockDisposition(StrEnum):
    """What resolution did with the receipt-recorded lease."""

    #: No lease was recorded on the receipt.
    NONE = "none"
    #: The live lease of the recorded owner was released.
    RELEASED = "released"
    #: The lapsed (fenced) lease was cleared under the terminal verdict.
    FENCE_CLEARED = "fence_cleared"
    #: The key is held by a different owner — not ours to release.
    FOREIGN = "foreign"


class VerdictSource(Protocol):
    """Where host truth comes from (the ``04-reconcile`` boundary).

    Implemented by the worker/API composition over the reviewed
    reconciliation API; called synchronously (SSH/IO work) off the event
    loop. Returning ``None`` means "no verdict available" — the row stays
    open and is reported, never guessed.
    """

    def verdict_for(
        self, *, run_id: str, seq: int, receipt: Mapping[str, object]
    ) -> ReconciliationClass | None: ...


@dataclass(frozen=True, slots=True)
class StepFinding:
    """One classified step row (inspection; never a mutation)."""

    seq: int
    state: StepState
    finding: FindingClass
    receipt: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class RunInspection:
    """One run's recovery view: facade state, engine state, findings."""

    run_id: str
    run_state: RunState | None
    engine_state: RunState | None
    findings: tuple[StepFinding, ...]

    @property
    def uncertain(self) -> tuple[StepFinding, ...]:
        """The open findings recovery must answer before any retry."""
        return tuple(f for f in self.findings if f.finding is not FindingClass.RESOLVED)


@dataclass(frozen=True, slots=True)
class ResolveResult:
    """One reconciliation resolution (audit view)."""

    run_id: str
    seq: int
    state: StepState
    verdict: ReconciliationClass
    lock: LockDisposition
    wake_sent: bool


@dataclass(frozen=True, slots=True)
class CancelResult:
    """The KNOWN outcome of one cancellation (never an assumption).

    ``outcome`` is the engine's own terminal status, applied to the facade
    diagram. ``resolved`` carries the reconciliation dispositions; steps
    whose host truth is still missing stay in ``unresolved_seqs`` with
    their locks fenced until a verdict exists (C04 rule 7) — the run's
    ``cancelled`` is the ENGINE truth and never a claim about those
    effects.
    """

    run_id: str
    outcome: RunState
    requested_at: datetime
    outcome_at: datetime
    requested_now: bool
    resolved: tuple[ResolveResult, ...]
    unresolved_seqs: tuple[int, ...]


# ---------------------------------------------------------------------------
# validation (pure, value-free)
# ---------------------------------------------------------------------------


def _reject(code: RecoveryErrorCode, detail: str) -> None:
    raise RecoveryError(code, detail)


def _canonical_run_id(value: object) -> str:
    try:
        return validate_run_id(value)
    except EngineError as exc:
        raise RecoveryError(
            RecoveryErrorCode.INVALID_RUN_ID,
            "run_id must be a canonical UUID string",
        ) from exc


def _validate_seq(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _reject(RecoveryErrorCode.INVALID_RUN_ID, "step seq must be a positive integer")
    return value


def _validate_verdict(verdict: object) -> ReconciliationClass:
    if not isinstance(verdict, ReconciliationClass):
        _reject(
            RecoveryErrorCode.INVALID_VERDICT,
            "verdict must be a ReconciliationClass value",
        )
    if verdict not in VERDICT_STEP_STATES:
        _reject(
            RecoveryErrorCode.VERDICT_UNRESOLVED,
            "verdict does not prove the prior effect is terminal; the row "
            "stays open until a final host verdict exists",
        )
    return verdict


def _validate_source(source: object) -> str:
    if not isinstance(source, str) or not source.strip():
        _reject(RecoveryErrorCode.INVALID_SOURCE, "source must be a non-empty string")
    if len(source) > MAX_SOURCE_CHARS:
        _reject(RecoveryErrorCode.INVALID_SOURCE, "source exceeds its length bound")
    if any(ch < " " or ch == "\x7f" for ch in source):
        _reject(RecoveryErrorCode.INVALID_SOURCE, "source must not contain controls")
    return source


def _validate_note(note: object) -> str:
    if not isinstance(note, str) or not note.strip():
        _reject(RecoveryErrorCode.INVALID_NOTE, "note must be a non-empty string")
    if len(note) > MAX_NOTE_CHARS:
        _reject(RecoveryErrorCode.INVALID_NOTE, "note exceeds its length bound")
    return note


def _validate_timeout(value: object, default: float, low: float, high: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject(
            RecoveryErrorCode.INVALID_TIMEOUT, "timeout must be a number of seconds"
        )
    number = float(value)
    if not math.isfinite(number) or number < low or number > high:
        _reject(
            RecoveryErrorCode.INVALID_TIMEOUT,
            f"timeout must be finite within [{low}, {high}]",
        )
    return number


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _lock_owner_ref(run_id: str, seq: int) -> str:
    """The opaque owner label for one step's lease — the exact format the
    step executor records (``run:<run_id>:step:<seq>``); the match against
    ``owner_ref`` is what makes a receipt-recorded lease OURS to release.
    """
    return f"run:{run_id}:step:{seq}"


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


#: The lock service's row projection for :class:`LockSnapshot` (mirrors the
#: private ``_ROW_COLUMNS`` of ``mai.jobs.resource_locks``; a drift fails
#: loudly on the first missing key instead of silently misreading).
_LOCK_ROW_COLUMNS: Final[str] = """
    server_id::text AS server_id, resource, state, owner_token::text AS owner_token,
    boot_id::text AS boot_id, owner_ref, lease_ttl_seconds,
    lease_expires_at, acquired_at, heartbeat_at,
    fenced_at, fence_reason, fenced_boot_id::text AS fenced_boot_id
"""


def _snapshot_of(row: Mapping[str, object]) -> LockSnapshot:
    """Type one lock-table row exactly the lock service's way."""
    return LockSnapshot(
        server_id=str(row["server_id"]),
        resource=str(row["resource"]),
        state=str(row["state"]),
        owner_token=str(row["owner_token"]),
        boot_id=str(row["boot_id"]),
        owner_ref=str(row["owner_ref"]),
        lease_ttl=float(row["lease_ttl_seconds"]),  # type: ignore[arg-type]
        lease_expires_at=row["lease_expires_at"],  # type: ignore[arg-type]
        acquired_at=row["acquired_at"],  # type: ignore[arg-type]
        heartbeat_at=row["heartbeat_at"],  # type: ignore[arg-type]
        fenced_at=row["fenced_at"],  # type: ignore[arg-type]
        fence_reason=(
            None if row["fence_reason"] is None else str(row["fence_reason"])
        ),
        fenced_boot_id=(
            None if row["fenced_boot_id"] is None else str(row["fenced_boot_id"])
        ),
        observed_at=row["observed_at"],  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------


class RecoveryService:
    """Reconciliation owner + safe-cancel facade over the jobs domain.

    One instance is one controller identity: it composes the engine client
    and the lock service from the production URL contract (each may be
    injected for tests); its lock service carries a fresh ``boot_id`` per
    construction, so a restarted controller never re-presents stale owner
    identities — receipt-recorded owner tokens authorize exactly the
    recorded row's release, and lapsed rows go through the fence.
    """

    def __init__(
        self,
        system_database_url: str,
        sessions: async_sessionmaker[AsyncSession],
        *,
        locks: ResourceLockService | None = None,
        client: EngineClient | None = None,
        verdict_source: VerdictSource | None = None,
        sender: Callable[[str, int], bool] | None = None,
        cancel_outcome_timeout: float | None = None,
        cancel_poll_seconds: float | None = None,
    ) -> None:
        self._url = system_database_url
        self._sessions = sessions
        self._lifecycle = RunLifecycle(sessions)
        self._locks = (
            locks if locks is not None else ResourceLockService(system_database_url)
        )
        self._owns_client = client is None
        self._client = (
            client if client is not None else EngineClient(system_database_url)
        )
        self._verdict_source = verdict_source
        self._sender = (
            sender
            if sender is not None
            else (lambda run_id, seq: self._default_wake(run_id, seq))
        )
        self._cancel_outcome_timeout = _validate_timeout(
            cancel_outcome_timeout,
            DEFAULT_CANCEL_OUTCOME_TIMEOUT,
            MIN_CANCEL_OUTCOME_TIMEOUT,
            MAX_CANCEL_OUTCOME_TIMEOUT,
        )
        self._cancel_poll_seconds = _validate_timeout(
            cancel_poll_seconds,
            DEFAULT_CANCEL_POLL_SECONDS,
            MIN_CANCEL_POLL_SECONDS,
            MAX_CANCEL_POLL_SECONDS,
        )
        self._wake_client: DBOSClient | None = None

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Release owned clients (idempotent; injected ones are not closed)."""
        if self._wake_client is not None:
            self._wake_client.destroy()
            self._wake_client = None
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "RecoveryService":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- pure queries over the composed services -----------------------------

    @property
    def locks(self) -> ResourceLockService:
        """The composed lock service (its ``boot_id`` is this recovery boot)."""
        return self._locks

    @property
    def client(self) -> EngineClient:
        """The composed engine control client (state/cancel/resume seams)."""
        return self._client

    # -- inspection (acceptance 1) -------------------------------------------

    async def ainspect_run(self, run_id: str) -> RunInspection:
        """Classify one run's step rows for recovery (read-only).

        The facade state is replayed from the persisted event log and the
        engine state read from the engine adapter; unknown runs (no
        ``job_runs`` row) are rejected value-free.
        """
        run_id = _canonical_run_id(run_id)
        facade = await self._require_run_state(run_id)
        engine_state = await asyncio.to_thread(self._client.run_state, run_id)
        rows = await self._read_step_rows(run_id)
        findings = tuple(self._classify(facade, engine_state, row) for row in rows)
        return RunInspection(
            run_id=run_id,
            run_state=facade,
            engine_state=engine_state,
            findings=findings,
        )

    async def ainspect_outstanding(
        self, limit: int = MAX_SWEEP_RUNS
    ) -> tuple[RunInspection, ...]:
        """Inspect every non-terminal run (the restart sweep, bounded).

        Terminal facade states are skipped (nothing outstanding); runs
        whose rows vanish mid-sweep are skipped (a concurrent terminal
        transition won the race — re-inspection is the caller's decision).
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            _reject(
                RecoveryErrorCode.INVALID_TIMEOUT, "limit must be a positive integer"
            )
        run_ids = await self._nonterminal_run_ids(limit)
        inspections: list[RunInspection] = []
        for run_id in run_ids:
            try:
                inspection = await self.ainspect_run(run_id)
            except RecoveryError as error:
                if error.code in (
                    RecoveryErrorCode.RUN_NOT_FOUND,
                    RecoveryErrorCode.ENGINE_UNKNOWN_RUN,
                ):
                    continue
                raise
            if inspection.run_state in TERMINAL_RUN_STATES:
                continue  # resolved history; nothing outstanding
            inspections.append(inspection)
        return tuple(inspections)

    # -- certification (the crash window nobody will replay) -----------------

    async def acertify_running_step(self, run_id: str, seq: int) -> StepFinding:
        """Translate a crash-window row to ``outcome_unknown`` (guard rule 2).

        Allowed ONLY when the engine workflow can no longer claim the step
        (engine terminal or absent): restart replay owns the window
        otherwise, and racing it would fight the live attempt's guarded
        final write. The certified receipt references the still-held lease
        (if the recorded owner still holds it) so the reconciliation owner
        can dispose it after the host verdict (C04 rule 7).
        """
        run_id = _canonical_run_id(run_id)
        seq = _validate_seq(seq)
        facade = await self._require_run_state(run_id)
        if facade in TERMINAL_RUN_STATES:
            _reject(
                RecoveryErrorCode.INVALID_RUN_STATE,
                "run is terminal; its rows are history, not recovery input",
            )
        engine_state = await asyncio.to_thread(self._client.run_state, run_id)
        if engine_state is not None and engine_state not in TERMINAL_RUN_STATES:
            _reject(
                RecoveryErrorCode.INVALID_RUN_STATE,
                "engine workflow may still claim this step; restart replay "
                "owns the crash-window translation",
            )
        row = await self._read_step_row(run_id, seq)
        if row is None:
            _reject(RecoveryErrorCode.STEP_NOT_FOUND, "no step row for this seq")
        state = StepState(str(row["state"]))
        if state is not StepState.RUNNING:
            return self._classify(facade, engine_state, row)
        receipt = await self._certified_receipt(run_id, seq, str(row["capability_id"]))
        await self._persist_step_transition(
            run_id=run_id,
            seq=seq,
            expected=StepState.RUNNING,
            target=StepState.OUTCOME_UNKNOWN,
            receipt=receipt,
            event_data={
                "capability_id": str(row["capability_id"]),
                "reason": CRASH_RECOVERY_REASON,
            },
        )
        return StepFinding(
            seq=seq,
            state=StepState.OUTCOME_UNKNOWN,
            finding=FindingClass.UNCERTAIN,
            receipt=receipt,
        )

    # -- the facade park (engine-orphaned uncertainty) -----------------------

    async def apark_uncertain(self, run_id: str) -> RunInspection:
        """Park an engine-orphaned run's uncertainty (``reconciling``).

        Records ``running → reconciling`` plus a ``run.recovery_parked``
        event listing the open steps, after certifying crash-window rows.
        Refuses while the engine workflow is alive (restart replay owns
        the park there — a facade park would desync the replayed wait
        bookkeeping) and no-ops idempotently on an already-parked or
        terminal run. The parked run has NO workflow behind it and NO wait
        deadline: it proceeds only by deliberate operator decision after
        resolution, never by an automatic retry.
        """
        run_id = _canonical_run_id(run_id)
        facade = await self._require_run_state(run_id)
        if facade in TERMINAL_RUN_STATES or facade is RunState.RECONCILING:
            # Already parked (or history): a second sweep re-inspects and
            # never duplicates the transition or the park event.
            return await self.ainspect_run(run_id)
        engine_state = await asyncio.to_thread(self._client.run_state, run_id)
        if engine_state is not None and engine_state not in TERMINAL_RUN_STATES:
            _reject(
                RecoveryErrorCode.INVALID_RUN_STATE,
                "engine workflow is alive; its own wait parking owns the run",
            )
        if engine_state is not None:
            await self.aapply_engine_outcome(run_id)
            facade = await self._require_run_state(run_id)
            if facade in TERMINAL_RUN_STATES or facade is RunState.RECONCILING:
                return await self.ainspect_run(run_id)
        if facade is not RunState.RUNNING:
            _reject(
                RecoveryErrorCode.INVALID_RUN_STATE,
                "run is not in a state that may enter the recovery park",
            )
        inspection = await self.ainspect_run(run_id)
        open_seqs: list[int] = []
        for finding in inspection.findings:
            if finding.finding is FindingClass.CRASH_WINDOW:
                await self.acertify_running_step(run_id, finding.seq)
                open_seqs.append(finding.seq)
            elif finding.finding is FindingClass.UNCERTAIN:
                open_seqs.append(finding.seq)
        if open_seqs and facade is RunState.RUNNING:
            await self._lifecycle.record_transition(run_id, RunState.RECONCILING)
            await self._aemit_event(
                run_id,
                RECOVERY_PARKED_EVENT_TYPE,
                {"reason": "restart_reconciliation", "steps": sorted(open_seqs)},
            )
        return await self.ainspect_run(run_id)

    # -- resolution (the ONLY uncertain-edge writer) -------------------------

    async def aresolve_step(
        self,
        run_id: str,
        seq: int,
        verdict: ReconciliationClass,
        *,
        source: str = RESOLUTION_REASON,
        note: str | None = None,
    ) -> ResolveResult:
        """Resolve one uncertain row from a FINAL host verdict.

        The guarded single-owner update lands ``outcome_unknown → terminal``
        with the verdict recorded value-free on the receipt; the
        receipt-recorded lease is disposed (release live, clear fence
        under the verdict); the parked run is woken (best effort). A
        non-final verdict is rejected and the row stays exactly as it was.
        """
        run_id = _canonical_run_id(run_id)
        seq = _validate_seq(seq)
        _validate_verdict(verdict)
        source = _validate_source(source)
        if note is not None:
            _validate_note(note)
        else:
            note = (
                f"recovery resolve run {run_id} step {seq} "
                f"verdict {verdict.value} source {source}"
            )
        await self._require_run_row(run_id)
        row = await self._read_step_row(run_id, seq)
        if row is None:
            _reject(RecoveryErrorCode.STEP_NOT_FOUND, "no step row for this seq")
        state = StepState(str(row["state"]))
        if state is not StepState.OUTCOME_UNKNOWN:
            _reject(
                RecoveryErrorCode.STEP_NOT_UNCERTAIN,
                "only an outcome_unknown row is open for reconciliation",
            )
        receipt = dict(row["receipt"] or {})
        lock_disposition, lock_fields = await self._dispose_recorded_lock(
            run_id, seq, receipt, verdict=verdict, note=note
        )
        receipt["lock"] = lock_fields
        receipt["reason"] = RESOLUTION_REASON
        receipt["resolution"] = {
            "class": verdict.value,
            "source": source,
            "resolved_at": _utc_now().isoformat(),
        }
        target = VERDICT_STEP_STATES[verdict]
        await self._persist_step_transition(
            run_id=run_id,
            seq=seq,
            expected=StepState.OUTCOME_UNKNOWN,
            target=target,
            receipt=receipt,
            event_data={
                "capability_id": str(row["capability_id"]),
                "reason": RESOLUTION_REASON,
            },
        )
        wake_sent = await asyncio.to_thread(self._sender, run_id, seq)
        return ResolveResult(
            run_id=run_id,
            seq=seq,
            state=target,
            verdict=verdict,
            lock=lock_disposition,
            wake_sent=bool(wake_sent),
        )

    async def areconcile_run(
        self,
        run_id: str,
        *,
        source: str = RESOLUTION_REASON,
        note: str | None = None,
    ) -> tuple[ResolveResult | StepFinding, ...]:
        """Resolve every uncertain row through the host-verdict source.

        Rows the source cannot answer stay open and are reported as their
        finding (never guessed, never finalized). Without a source the
        sweep is a pure re-inspection. Returns one entry per open step in
        ``seq`` order (resolution results interleaved with the findings
        that remain open).
        """
        run_id = _canonical_run_id(run_id)
        inspection = await self.ainspect_run(run_id)
        results: list[ResolveResult | StepFinding] = []
        for finding in inspection.findings:
            if finding.finding is not FindingClass.UNCERTAIN:
                continue
            verdict = (
                await asyncio.to_thread(
                    self._verdict_source.verdict_for,
                    run_id=run_id,
                    seq=finding.seq,
                    receipt=finding.receipt or {},
                )
                if self._verdict_source is not None
                else None
            )
            if verdict is None:
                results.append(finding)
                continue
            try:
                results.append(
                    await self.aresolve_step(
                        run_id, finding.seq, verdict, source=source, note=note
                    )
                )
            except RecoveryError as error:
                if error.code is RecoveryErrorCode.VERDICT_UNRESOLVED:
                    results.append(finding)
                    continue
                raise
        return tuple(results)

    # -- safe cancellation (acceptance 2) ------------------------------------

    async def acancel_run(
        self,
        run_id: str,
        *,
        cancel_children: bool = False,
        outcome_timeout: float | None = None,
    ) -> CancelResult:
        """Cancel a run and return only a KNOWN engine outcome.

        Records the request once (the facade ``cancel_requested``
        timestamp), requests the engine cancellation (in-flight steps
        complete their effects — guard rules 3–4), then polls until the
        engine reports a TERMINAL status; on budget expiry raises
        ``cancel_outcome_timeout`` WITHOUT claiming any outcome. After the
        engine lands: the engine truth is applied to the facade diagram,
        crash-window rows are certified, uncertain rows are resolved
        through the host-verdict source (when composed), and every
        disposition is reported. Re-entry after a timeout or on an
        already-terminal run is safe and idempotent.
        """
        run_id = _canonical_run_id(run_id)
        timeout = _validate_timeout(
            outcome_timeout,
            self._cancel_outcome_timeout,
            MIN_CANCEL_OUTCOME_TIMEOUT,
            MAX_CANCEL_OUTCOME_TIMEOUT,
        )
        await self._require_run_row(run_id)
        engine_state = await asyncio.to_thread(self._client.run_state, run_id)
        if engine_state is None:
            _reject(
                RecoveryErrorCode.ENGINE_UNKNOWN_RUN,
                "the engine has no workflow under this run_id",
            )
        requested_now = False
        requested_at = await self._record_cancel_request(run_id, cancel_children)
        if engine_state not in TERMINAL_RUN_STATES:
            requested_now = True
            await asyncio.to_thread(
                self._client.request_cancel, run_id, cancel_children=cancel_children
            )
            deadline = _utc_now() + timedelta(seconds=timeout)
            while True:
                state = await asyncio.to_thread(self._client.run_state, run_id)
                if state is not None and state in TERMINAL_RUN_STATES:
                    engine_state = state
                    break
                if _utc_now() >= deadline:
                    _reject(
                        RecoveryErrorCode.CANCEL_OUTCOME_TIMEOUT,
                        "engine outcome not observed within the budget; the "
                        "request stays recorded — re-enter to resume the wait",
                    )
                await asyncio.sleep(self._cancel_poll_seconds)
        assert engine_state is not None and engine_state in TERMINAL_RUN_STATES
        outcome_at = _utc_now()
        await self.aapply_engine_outcome(run_id)

        resolved: list[ResolveResult] = []
        unresolved: list[int] = []
        inspection = await self.ainspect_run(run_id)
        for finding in inspection.findings:
            if finding.finding is FindingClass.CRASH_WINDOW:
                finding = await self.acertify_running_step(run_id, finding.seq)
            if finding.finding not in (
                FindingClass.UNCERTAIN,
                FindingClass.CANCEL_PENDING,
            ):
                continue
            verdict = (
                await asyncio.to_thread(
                    self._verdict_source.verdict_for,
                    run_id=run_id,
                    seq=finding.seq,
                    receipt=finding.receipt or {},
                )
                if self._verdict_source is not None
                else None
            )
            if verdict is None:
                unresolved.append(finding.seq)
                continue
            try:
                resolved.append(
                    await self.aresolve_step(
                        run_id,
                        finding.seq,
                        verdict,
                        source="cancellation",
                        note=f"run {run_id} cancelled; host verdict for step "
                        f"{finding.seq}",
                    )
                )
            except RecoveryError as error:
                if error.code is RecoveryErrorCode.VERDICT_UNRESOLVED:
                    unresolved.append(finding.seq)
                    continue
                raise
        return CancelResult(
            run_id=run_id,
            outcome=engine_state,
            requested_at=requested_at,
            outcome_at=outcome_at,
            requested_now=requested_now,
            resolved=tuple(resolved),
            unresolved_seqs=tuple(sorted(unresolved)),
        )

    async def aapply_engine_outcome(self, run_id: str) -> RunState | None:
        """Apply the engine's terminal truth to the facade diagram.

        Idempotent observation: terminal facade states are never touched
        (the lifecycle's stale-result guard), waiting/reconciling states
        resume through ``running`` first, and an unanchored log anchors at
        the submit edge exactly like the executor's own translations. A
        lagging facade (the worker died between the engine landing and the
        workflow's own recording) is repaired HERE — the engine terminal
        value can never change again.
        """
        run_id = _canonical_run_id(run_id)
        engine_state = await asyncio.to_thread(self._client.run_state, run_id)
        if engine_state is None:
            _reject(
                RecoveryErrorCode.ENGINE_UNKNOWN_RUN,
                "the engine has no workflow under this run_id",
            )
        if engine_state not in TERMINAL_RUN_STATES:
            _reject(
                RecoveryErrorCode.INVALID_RUN_STATE,
                "engine workflow has not landed; there is no outcome to apply",
            )
        facade = await self._facade_state(run_id)
        if facade is None:
            await self._lifecycle.record_transition(run_id, RunState.QUEUED)
            facade = RunState.QUEUED
        if facade in TERMINAL_RUN_STATES:
            return facade
        if facade is RunState.QUEUED:
            if engine_state is RunState.SUCCEEDED:
                _reject(
                    RecoveryErrorCode.INVALID_RUN_STATE,
                    "engine reports success but the facade log never ran; "
                    "the event log is not re-writable",
                )
            await self._lifecycle.record_transition(run_id, engine_state)
            return engine_state
        if facade is RunState.RUNNING:
            await self._lifecycle.record_transition(run_id, engine_state)
            return engine_state
        # waiting_approval / waiting_children / reconciling resume first.
        await self._lifecycle.record_transition(run_id, RunState.RUNNING)
        await self._lifecycle.record_transition(run_id, engine_state)
        return engine_state

    # -- internals: reads ------------------------------------------------------

    async def _facade_state(self, run_id: str) -> RunState | None:
        try:
            return await self._lifecycle.current_state(run_id)
        except RunLifecycleError as error:
            if error.code.value == "invalid_run_id":
                raise RecoveryError(
                    RecoveryErrorCode.INVALID_RUN_ID, error.detail
                ) from None
            raise

    async def _require_run_state(self, run_id: str) -> RunState | None:
        """The facade state, requiring the ``job_runs`` row to exist."""
        await self._require_run_row(run_id)
        return await self._facade_state(run_id)

    async def _require_run_row(self, run_id: str) -> None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text("SELECT run_id FROM job_runs WHERE run_id = :r"),
                    {"r": uuid.UUID(run_id)},
                )
            ).first()
        if row is None:
            _reject(RecoveryErrorCode.RUN_NOT_FOUND, "no job_runs row for this run_id")

    async def _nonterminal_run_ids(self, limit: int) -> tuple[str, ...]:
        async with self._sessions() as session:
            result = await session.execute(
                text("SELECT run_id FROM job_runs ORDER BY run_id LIMIT :n"),
                {"n": limit},
            )
            return tuple(str(row[0]) for row in result)

    async def _read_step_rows(self, run_id: str) -> list[dict[str, object]]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT seq, capability_id, state, receipt FROM job_steps "
                        "WHERE run_id = :r ORDER BY seq"
                    ),
                    {"r": uuid.UUID(run_id)},
                )
            ).mappings()
            return [dict(row) for row in rows]

    async def _read_step_row(self, run_id: str, seq: int) -> dict[str, object] | None:
        async with self._sessions() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT seq, capability_id, state, receipt FROM job_steps "
                            "WHERE run_id = :r AND seq = :s"
                        ),
                        {"r": uuid.UUID(run_id), "s": seq},
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else dict(row)

    def _classify(
        self,
        facade: RunState | None,
        engine_state: RunState | None,
        row: Mapping[str, object],
    ) -> StepFinding:
        """One row's recovery meaning (pure; see :class:`FindingClass`)."""
        state = StepState(str(row["state"]))
        receipt = row["receipt"]
        finding: FindingClass
        if state in TERMINAL_STEP_STATES:
            finding = FindingClass.RESOLVED
        elif state is StepState.PENDING:
            finding = (
                FindingClass.AWAITING_APPROVAL
                if facade is RunState.WAITING_APPROVAL
                else FindingClass.PENDING
            )
        elif state is StepState.RUNNING:
            engine_alive = (
                engine_state is not None and engine_state not in TERMINAL_RUN_STATES
            )
            finding = FindingClass.ACTIVE if engine_alive else FindingClass.CRASH_WINDOW
        elif state is StepState.OUTCOME_UNKNOWN:
            finding = FindingClass.UNCERTAIN
        else:  # cancel_requested: no edges; awaiting host-verified outcome
            finding = FindingClass.CANCEL_PENDING
        return StepFinding(
            seq=int(row["seq"]),
            state=state,
            finding=finding,
            receipt=receipt if isinstance(receipt, Mapping) else None,
        )

    # -- internals: the certified crash receipt -------------------------------

    async def _certified_receipt(
        self, run_id: str, seq: int, capability_id: str
    ) -> dict[str, object]:
        """The crash receipt: effect unproven, lease reference preserved.

        The lease's resource string lives only in the dead attempt's spec
        (a ``running`` row has no receipt yet), so the reference is found
        by the documented ``owner_ref`` format on the lock service's own
        table — a read-only lookup, never a mutation. Without the
        reference the eventual fence would have no in-domain path to its
        verdict-governed clearing (C04 rule 7).
        """
        receipt: dict[str, object] = {
            "v": 1,
            "op_id": None,
            "capability_id": capability_id,
            "allowed": None,
            "reason": CRASH_RECOVERY_REASON,
            "effect": LOCK_RESOURCE_UNPROVEN,
            "payload_hash": None,
        }
        server_id = await self._run_server_id(run_id)
        snapshot = await asyncio.to_thread(
            self._held_lease_snapshot, server_id, run_id, seq
        )
        if snapshot is not None:
            receipt["lock"] = {
                "resource": snapshot.resource,
                "owner_token": snapshot.owner_token,
                "lease_ttl": snapshot.lease_ttl,
                "released": False,
                "lapsed": snapshot.expired,
            }
        return receipt

    def _locks_connect(self) -> psycopg.Connection:
        """One short lock-table connection (the lock service's own pattern)."""
        return psycopg.connect(
            self._url, connect_timeout=_CONNECT_TIMEOUT_S, row_factory=dict_row
        )

    def _held_lease_snapshot(
        self, server_id: str, run_id: str, seq: int
    ) -> LockSnapshot | None:
        """Read the step's lease row by its owner_ref (read-only).

        The ``(server_id, resource)`` key is unknown at facade level (it
        lives in the dead attempt's spec), but ``owner_ref`` is the
        executor's documented cross-module label for exactly this lease
        (``run:<run_id>:step:<seq>``); the lookup reconstructs the same
        observation :meth:`ResourceLockService.inspect` returns, keyed by
        the row's PRIMARY KEY once the resource is known.
        """
        with self._locks_connect() as conn:
            row = conn.execute(
                f"""
                SELECT {_LOCK_ROW_COLUMNS},
                       (lease_expires_at <= now()) AS expired,
                       now() AS observed_at
                FROM {LOCK_TABLE}
                WHERE server_id = %s AND owner_ref = %s
                ORDER BY acquired_at DESC
                """,
                (uuid.UUID(server_id), _lock_owner_ref(run_id, seq)),
            ).fetchone()
        return None if row is None else _snapshot_of(row)

    async def _run_server_id(self, run_id: str) -> str:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text("SELECT server_id FROM job_runs WHERE run_id = :r"),
                    {"r": uuid.UUID(run_id)},
                )
            ).first()
        if row is None:
            _reject(RecoveryErrorCode.RUN_NOT_FOUND, "no job_runs row for this run_id")
        return str(row[0])

    # -- internals: lock disposition on resolution ----------------------------

    async def _dispose_recorded_lock(
        self,
        run_id: str,
        seq: int,
        receipt: Mapping[str, object],
        *,
        verdict: ReconciliationClass,
        note: str | None,
    ) -> tuple[LockDisposition, dict[str, object] | None]:
        """Release or fence-clear the receipt-recorded lease (C04 rule 7).

        A live lease of the recorded owner is released (the token is the
        ONLY release authority and the receipt recorded it); a lapsed
        lease is fenced by its first touch and cleared ONLY under the
        final verdict, with the caller's audit note (or the value-free
        derived note of the resolution itself). A key held by anyone else
        is reported, never touched.
        """
        recorded = receipt.get("lock")
        if not isinstance(recorded, Mapping) or not recorded.get("resource"):
            return LockDisposition.NONE, None
        fields = dict(recorded)
        resource = str(recorded["resource"])
        owner_token = recorded.get("owner_token")
        server_id = await self._run_server_id(run_id)
        snapshot = await asyncio.to_thread(self._locks.inspect, server_id, resource)
        if snapshot is None:
            # The row is gone (released elsewhere after recording); the
            # recorded fields stay exactly as the attempt wrote them.
            return LockDisposition.NONE, fields
        if snapshot.state == "fenced":
            await asyncio.to_thread(
                self._locks.clear_fence,
                server_id,
                resource,
                verdict=verdict,
                note=note or "",
            )
            fields.update({"released": False, "lapsed": True, "cleared": True})
            return LockDisposition.FENCE_CLEARED, fields
        if snapshot.owner_token != owner_token:
            return LockDisposition.FOREIGN, fields
        try:
            await asyncio.to_thread(self._locks.release, _lease_of_snapshot(snapshot))
        except ResourceLockError:
            # The lease lapsed between the inspect and the release: the
            # touch fenced it; the terminal verdict clears the fence.
            await asyncio.to_thread(
                self._locks.clear_fence,
                server_id,
                resource,
                verdict=verdict,
                note=note or "",
            )
            fields.update({"released": False, "lapsed": True, "cleared": True})
            return LockDisposition.FENCE_CLEARED, fields
        fields.update({"released": True, "lapsed": False})
        return LockDisposition.RELEASED, fields

    # -- internals: events + guarded transitions ------------------------------

    async def _record_cancel_request(
        self, run_id: str, cancel_children: bool
    ) -> datetime:
        """Record the cancel request ONCE; return its timestamp.

        Idempotent by observation (the ``aobserve_enter_wait`` discipline):
        an existing ``run.cancel_requested`` event keeps its original
        timestamp — budgets and audit never silently move.
        """
        for event in await self._lifecycle.event_stream(run_id):
            if event.type == CANCEL_REQUESTED_EVENT_TYPE:
                return event.ts
        requested_at = _utc_now()
        await self._aemit_event(
            run_id,
            CANCEL_REQUESTED_EVENT_TYPE,
            {
                "requested_at": requested_at.isoformat(),
                "cancel_children": bool(cancel_children),
            },
            ts=requested_at,
        )
        return requested_at

    async def _aemit_event(
        self,
        run_id: str,
        event_type: str,
        data: Mapping[str, object],
        *,
        ts: datetime | None = None,
    ) -> int:
        """Append one event with the next whole-stream cursor value.

        The shared producer contract (C05 rule 4): the cursor is allocated
        as next ``MAX(seq) + 1`` under the ``job_runs`` row lock, in one
        transaction; the storage UNIQUE is the backstop. An explicit
        ``ts`` keeps the returned record and the stored event on ONE
        clock reading (the recorded request timestamp IS the event's).
        """
        event_ts = ts if ts is not None else _utc_now()
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
                    _reject(
                        RecoveryErrorCode.RUN_NOT_FOUND,
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
                        "ts": event_ts,
                        "type": event_type,
                        "v": EVENT_PAYLOAD_VERSION,
                        "data": json.dumps(dict(data)),
                    },
                )
        return next_seq

    async def _persist_step_transition(
        self,
        *,
        run_id: str,
        seq: int,
        expected: StepState,
        target: StepState,
        receipt: dict[str, object] | None,
        event_data: Mapping[str, object],
    ) -> None:
        """One transaction: guarded step UPDATE + ``step.completed`` event.

        The ``WHERE state = expected`` guard makes each transition
        single-owner — the same discipline the executor's writer applies;
        a lost guard is a loud integrity failure (the live writer won),
        never a silent overwrite.
        """
        from mai.jobs.steps import validate_step_transition  # noqa: PLC0415

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
                    _reject(
                        RecoveryErrorCode.RUN_NOT_FOUND,
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
                    _reject(
                        RecoveryErrorCode.STEP_STATE_GUARD,
                        "step row is not in the expected pre-transition state",
                    )
                step_id = str(row[0])
                data = {
                    "step_id": step_id,
                    "seq": seq,
                    **{k: v for k, v in event_data.items() if v is not None},
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
                        "ts": _utc_now(),
                        "type": STEP_COMPLETED_EVENT_TYPE,
                        "v": EVENT_PAYLOAD_VERSION,
                        "data": json.dumps(data),
                    },
                )

    # -- internals: the park wake ----------------------------------------------

    def _default_wake(self, run_id: str, seq: int) -> bool:
        """Control-plane wake of a parked run (best effort by contract).

        The message is only a re-check trigger on ``RESUME_TOPIC``; the
        parked loop always re-reads durable state on its poll, so a lost
        or unknown wake degrades to the poll cadence, never to a wrong
        outcome. A wake client is opened lazily and destroyed on close.
        """
        if self._wake_client is None:
            self._wake_client = DBOSClient(
                system_database_url=self._url,
                application_name=APPLICATION_NAME,
                lazy=True,
            )
        try:
            self._wake_client.send(
                run_id,
                {"kind": "recovery.resolve", "seq": seq},
                topic=RESUME_TOPIC,
            )
        except Exception:
            return False
        return True
