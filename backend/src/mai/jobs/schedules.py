"""Facade schedule management + the one durable fire-dispatch workflow.

Frozen boundary for contracts.md **C05 rule 8** and job-engine ADR guard
rules **6–7** (DBOS ``dbos==3.2.0``, the one engine — no second scheduler,
no cron logic outside DBOS schedules in this module or anywhere else).

Responsibility split (ADR "Responsibility split", continued by
:mod:`mai.jobs.engine`):

- **Facade-owned here** — the ``job_schedules`` record (created through
  :class:`ScheduleManager`; ``cron_timezone`` explicit, default UTC,
  ``misfire_policy`` pinned to the single frozen value
  ``skip_missed``), the ``job_schedule_fires`` dispatch ledger with
  PRIMARY KEY ``(schedule_id, planned_fire_time)`` — the storage-level
  dedup C05 rule 8 requires — and the run intent template (``run_spec``)
  applied per fire (references and identifiers, never secret values —
  C05 rule 3; value-level screening stays with the submission boundary).
- **Engine-owned below** — the cron trigger itself lives in the DBOS
  ``workflow_schedules`` system table and is derived from the domain row
  at create time (one trigger per domain schedule, named after its
  schedule ID, bound to the Machina queue and application). DBOS
  evaluates the cron, skips missed fires and resumes from now
  (``automatic_backfill=False`` pinned here — exp5 semantics; the
  unmeasured backfill path stays off).

Frozen misfire policy (C05 rule 8, engine rule 6): **skip missed fires,
resume from now, no catch-up.** A fire that came due while no worker ran
is skipped (no ledger row, no run); the next recurrence after the engine
returns is the first dispatch. There is no backfill and no catch-up.

Fire discipline (engine rule 7 + acceptance 1):

- The DBOS scheduler invokes :func:`schedule_fire_workflow` with the
  **planned fire time as an argument**; time-of-day logic belongs inside
  this workflow, never in the cron expression beyond the recurrence.
- The **single planned-fire identity** is
  :func:`planned_run_id` — a deterministic UUID derived from
  ``(schedule_id, planned_fire_time)``. It is the domain run ID of the
  fire's run, stable across restarts, duplicate pollers and replays; the
  engine additionally dedups the fire workflow itself by its
  deterministic ``sched-<schedule_id>-<fire time>`` workflow ID.
- Dispatch is **idempotent by construction** (guard rule 1): one durable
  step inserts the ledger row (``ON CONFLICT DO NOTHING``) and enqueues
  the run through :meth:`mai.jobs.engine.EngineClient.enqueue_run` with
  the deterministic run ID; the engine's ID-reuse rejection is the
  exactly-once submit guarantee, so a replayed or duplicated dispatch can
  never create a second run intent.

Persistence independence (acceptance 2): schedule records and the fire
ledger live in Machina's Postgres; the trigger lives in the engine's
system tables. Neither the browser nor a model takes part in firing:
dispatch enqueues a run intent for a registered handler name, and LLM
unavailability cannot block it. Conditional scripting
(``ConditionalTrigger``) is deliberately NOT implemented.

Process discipline (mirror of :mod:`mai.jobs.engine`):

- Control-plane processes (HTTP API, tests acting as the API) use
  :class:`ScheduleManager` only. It holds a bare ``DBOSClient`` — never a
  full engine, which would steal pending workflows via launch recovery.
- Worker processes execute :func:`schedule_fire_workflow` (registered at
  import; import it via ``MAI_WORKER_HANDLERS`` beside the handler
  modules). The workflow's dispatch resources are resolved lazily from
  the single ``mai.database`` URL contract, or injected with
  :func:`configure_dispatch` where a process already owns them.

Error discipline: every rejection is one value-free
:class:`ScheduleError` (code + fixed reason — no payload values, no
connection material, safe to log and surface, C01 rule 3).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final, Mapping

from dbos import DBOS, DBOSClient
from dbos import error as dbos_error
from dbos._croniter import croniter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mai.contracts.jobs import (
    DEFAULT_CRON_TIMEZONE,
    MISFIRE_POLICY_SKIP_MISSED,
    JobsContractError,
    validate_cron_expression,
    validate_cron_timezone,
)
from mai.database import create_app_engine, resolve_database_url
from mai.jobs.engine import (
    APPLICATION_NAME,
    QUEUE_NAME,
    EngineClient,
    EngineError,
    RunIdInUseError,
    validate_database_url,
    validate_handler_name,
    validate_run_id,
)

#: Name of the one scheduled facade workflow (DBOS records the function
#: name; the engine trigger created by :class:`ScheduleManager` points at
#: it, so the worker process must have imported this module).
SCHEDULE_FIRE_WORKFLOW_NAME: Final[str] = "schedule_fire_workflow"

#: Name of the durable dispatch step inside the fire workflow. Step names
#: are checkpoint identities — renaming invalidates recorded steps.
DISPATCH_FIRE_STEP_NAME: Final[str] = "schedule.dispatch_fire"

#: Namespace of the planned-fire run identity. A fixed, documented UUID
#: derived from a URN — never random — so the identity is stable forever.
FIRE_RUN_NAMESPACE: Final[str] = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "urn:mai:jobs:schedule-fire:v1")
)

#: The engine's own cron validator (``dbos==3.2.0`` exact pin — the same
#: parser ``DBOSClient.create_schedule`` applies). Importing it keeps
#: facade syntax acceptance identical to engine acceptance instead of
#: duplicating a second cron grammar.


class ScheduleErrorCode(StrEnum):
    """Value-free rejection codes (C01 envelope vocabulary)."""

    INVALID_SERVER_ID = "invalid_server_id"
    INVALID_SCHEDULE_ID = "invalid_schedule_id"
    INVALID_FIRE_TIME = "invalid_fire_time"
    INVALID_CRON = "invalid_cron"
    INVALID_TIMEZONE = "invalid_timezone"
    INVALID_RUN_SPEC = "invalid_run_spec"
    DISPATCH_ALREADY_CONFIGURED = "dispatch_already_configured"
    DISPATCH_NOT_CONFIGURED = "dispatch_not_configured"
    SCHEDULE_NOT_FOUND = "schedule_not_found"
    ENGINE_REJECTED = "engine_rejected"
    MISFIRE_POLICY_DRIFT = "misfire_policy_drift"


class ScheduleError(ValueError):
    """One value-free schedule rejection: code + fixed reason.

    Messages carry codes, field names and opaque identifiers only — never
    payload values or connection material — so they are safe to log,
    store and surface (C01 rule 3).
    """

    def __init__(self, code: ScheduleErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# validation (pure, value-free)
# ---------------------------------------------------------------------------


def _canonical_uuid(value: object, code: ScheduleErrorCode, field: str) -> str:
    """Require a canonical lowercase hyphenated UUID string (C01 rule 1)."""
    try:
        return validate_run_id(value)
    except EngineError:
        raise ScheduleError(code, f"{field} must be a canonical UUID string") from None


def validate_schedule_cron(expression: object) -> str:
    """Validate one cron expression against the frozen field contract.

    Two layers, both authoritative: the shared durable field contract
    (non-blank printable string, bounded length — ``mai.contracts.jobs``)
    and the engine's own grammar (6-field seconds-allowed cron, the exact
    validator ``create_schedule`` applies). Anything the engine would
    reject never reaches it.
    """
    try:
        expression = validate_cron_expression(expression)
    except JobsContractError:
        raise ScheduleError(
            ScheduleErrorCode.INVALID_CRON, "cron must be a bounded printable string"
        ) from None
    if not croniter.is_valid(expression, second_at_beginning=True):
        raise ScheduleError(
            ScheduleErrorCode.INVALID_CRON,
            "cron must be a valid DBOS cron expression (6 fields, seconds allowed)",
        )
    return expression


def validate_schedule_timezone(name: object) -> str:
    """Require an explicit IANA zone (engine rule 7); never default here.

    Callers that want the frozen default apply
    :data:`mai.contracts.jobs.DEFAULT_CRON_TIMEZONE` explicitly, so an
    absent timezone can never silently differ between API and storage.
    """
    try:
        return validate_cron_timezone(name)
    except JobsContractError:
        raise ScheduleError(
            ScheduleErrorCode.INVALID_TIMEZONE,
            "cron_timezone must be an explicit IANA timezone name",
        ) from None


@dataclass(frozen=True, slots=True)
class RunSpec:
    """The run intent template applied per fire (references only).

    Exactly ``{"handler_name", "payload"}``: the handler registered with
    :func:`mai.jobs.engine.register_job_handler` that executes the fire's
    run, and its payload object. The payload carries references and
    identifiers, never secret values (C05 rule 3); it is persisted
    canonically (``sort_keys`` + tight separators) so the same schedule
    row always dispatches byte-identical run intents.
    """

    handler_name: str
    payload: Mapping[str, object]

    def payload_json(self) -> str:
        """Canonical JSON text of the payload (deterministic across fires)."""
        return _canonical_payload_json(self.payload)


def _canonical_payload_json(payload: Mapping[str, object]) -> str:
    """Deterministic JSON text for one run payload.

    Same canonical form the approval path binds (``mai.capabilities.
    dispatch.canonical_payload_json``): ``sort_keys``, tight separators,
    ``ensure_ascii``, ``allow_nan=False`` — identical content in any key
    order yields identical bytes.
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
        raise ScheduleError(
            ScheduleErrorCode.INVALID_RUN_SPEC,
            f"payload is not JSON-canonicalizable ({type(error).__name__})",
        ) from None


def parse_run_spec(raw: object) -> RunSpec:
    """Validate one ``run_spec`` document; fail closed on any drift.

    Strict shape: exactly the keys ``{"handler_name", "payload"}``; the
    handler a registered-name slug (engine vocabulary); the payload a
    JSON-canonicalizable object. Unknown fields are a rejection, never a
    rider (the dispatch of a schedule must not change shape silently).
    """
    if not isinstance(raw, Mapping) or set(raw) != {"handler_name", "payload"}:
        raise ScheduleError(
            ScheduleErrorCode.INVALID_RUN_SPEC,
            "run_spec must have exactly the keys 'handler_name' and 'payload'",
        )
    try:
        handler_name = validate_handler_name(raw["handler_name"])
    except EngineError:
        raise ScheduleError(
            ScheduleErrorCode.INVALID_RUN_SPEC,
            "run_spec handler_name must be a lowercase dotted slug",
        ) from None
    payload = raw["payload"]
    if not isinstance(payload, Mapping):
        raise ScheduleError(
            ScheduleErrorCode.INVALID_RUN_SPEC, "run_spec payload must be an object"
        )
    _canonical_payload_json(payload)  # canonicalizable, else rejection
    return RunSpec(handler_name=handler_name, payload=dict(payload))


# ---------------------------------------------------------------------------
# the planned-fire identity (acceptance 1)
# ---------------------------------------------------------------------------


def planned_run_id(schedule_id: str, planned_fire_utc: datetime) -> str:
    """The single planned-fire identity: deterministic, restart-stable.

    ``uuid5(FIRE_RUN_NAMESPACE, "<schedule_id>|<UTC fire time>")`` — the
    same ``(schedule_id, planned_fire_time)`` pair always yields the same
    opaque domain run ID (C01 rule 1), so a fire replayed after a crash,
    dispatched by a duplicate poller or re-derived from the ledger
    identifies ONE run intent, never a second one. The instant is
    normalized to UTC first: the same moment expressed in any timezone
    yields the same identity.
    """
    schedule_id = _canonical_uuid(
        schedule_id, ScheduleErrorCode.INVALID_SCHEDULE_ID, "schedule_id"
    )
    planned_fire_utc = validate_fire_time(planned_fire_utc)
    moment = planned_fire_utc.astimezone(timezone.utc).isoformat()
    return str(uuid.uuid5(uuid.UUID(FIRE_RUN_NAMESPACE), f"{schedule_id}|{moment}"))


def validate_fire_time(value: object) -> datetime:
    """Require a timezone-aware datetime (the planned fire instant)."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ScheduleError(
            ScheduleErrorCode.INVALID_FIRE_TIME,
            "planned fire time must be a timezone-aware datetime",
        )
    return value


# ---------------------------------------------------------------------------
# schedule records (the facade-owned ``job_schedules`` row)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    """One persisted schedule record (the ``job_schedules`` row, typed)."""

    schedule_id: str
    server_id: str
    cron: str
    cron_timezone: str
    misfire_policy: str
    enabled: bool
    run_spec: RunSpec
    created_at: datetime
    updated_at: datetime


_SELECT_SCHEDULE_COLUMNS = (
    "schedule_id, server_id, cron, cron_timezone, misfire_policy, enabled, "
    "run_spec, created_at, updated_at"
)


def _record_from_row(row: Mapping[str, object]) -> ScheduleRecord:
    """Type one ``job_schedules`` row; frozen-policy drift fails closed."""
    misfire_policy = row["misfire_policy"]
    if misfire_policy != MISFIRE_POLICY_SKIP_MISSED:
        raise ScheduleError(
            ScheduleErrorCode.MISFIRE_POLICY_DRIFT,
            "job_schedules row carries a policy outside the frozen vocabulary",
        )
    timezone_name = row["cron_timezone"]
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ScheduleError(
            ScheduleErrorCode.INVALID_TIMEZONE,
            "job_schedules row carries no explicit timezone",
        )
    return ScheduleRecord(
        schedule_id=str(row["schedule_id"]),
        server_id=str(row["server_id"]),
        cron=str(row["cron"]),
        cron_timezone=timezone_name,
        misfire_policy=str(misfire_policy),
        enabled=bool(row["enabled"]),
        run_spec=parse_run_spec(row["run_spec"]),
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
    )


class ScheduleManager:
    """Facade surface over ``job_schedules`` + the derived engine trigger.

    Constructed with the system database URL (the single ``mai.database``
    URL contract) and the application session factory. Control-plane
    safe: it holds a bare ``DBOSClient`` (engine vocabulary stops at the
    :mod:`mai.jobs.engine` constants it reuses — one application, one
    queue) and never launches an engine.

    Creation order is the failure-safe one: the domain row is inserted
    and the engine trigger derived inside ONE application transaction;
    if the engine rejects the definition the transaction rolls back and
    nothing persists. A crash in the narrow window between engine
    acceptance and commit leaves an inert engine trigger (its fire
    workflow skips value-free when the domain row is absent —
    :func:`dispatch_fire`), never a firing orphan.
    """

    def __init__(
        self,
        system_database_url: str,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        validate_database_url(system_database_url)
        self._url = system_database_url
        self._sessions = sessions
        self._client = DBOSClient(
            system_database_url=system_database_url,
            application_name=APPLICATION_NAME,
            lazy=True,
        )

    def close(self) -> None:
        """Release the control client's system-database pool (idempotent)."""
        self._client.destroy()

    def __enter__(self) -> "ScheduleManager":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- write path ----------------------------------------------------------

    async def create(
        self,
        *,
        server_id: str,
        cron: str,
        run_spec: RunSpec | Mapping[str, object],
        cron_timezone: str | None = None,
    ) -> ScheduleRecord:
        """Persist one schedule record and derive its engine trigger.

        ``cron_timezone=None`` applies the frozen default UTC explicitly
        (engine rule 7). The misfire policy is not an argument: the single
        frozen value ``skip_missed`` is stored, and the trigger is created
        with ``automatic_backfill=False`` — the skip-missed / resume-from-
        now policy of C05 rule 8, pinned at both layers.
        """
        server_uuid = uuid.UUID(
            _canonical_uuid(server_id, ScheduleErrorCode.INVALID_SERVER_ID, "server_id")
        )
        validate_schedule_cron(cron)
        zone = (
            validate_schedule_timezone(cron_timezone)
            if cron_timezone is not None
            else DEFAULT_CRON_TIMEZONE
        )
        spec = run_spec if isinstance(run_spec, RunSpec) else parse_run_spec(run_spec)
        schedule_id = str(uuid.uuid4())
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            """
                            INSERT INTO job_schedules
                                (schedule_id, server_id, cron, cron_timezone,
                                 run_spec)
                            VALUES
                                (:schedule_id, :server_id, :cron, :cron_timezone,
                                 :run_spec)
                            """
                        ),
                        {
                            "schedule_id": uuid.UUID(schedule_id),
                            "server_id": server_uuid,
                            "cron": cron,
                            "cron_timezone": zone,
                            "run_spec": json.dumps(
                                {
                                    "handler_name": spec.handler_name,
                                    "payload": spec.payload,
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=True,
                                allow_nan=False,
                            ),
                        },
                    )
                    # The engine trigger is derived from the row: one DBOS
                    # schedule named after the domain ID, bound to the one
                    # Machina queue/application, backfill OFF (frozen
                    # misfire policy). Failure here rolls the row back.
                    await asyncio.to_thread(
                        self._client.create_schedule,
                        schedule_name=schedule_id,
                        workflow_name=SCHEDULE_FIRE_WORKFLOW_NAME,
                        schedule=cron,
                        cron_timezone=zone,
                        queue_name=QUEUE_NAME,
                        application_name=APPLICATION_NAME,
                        automatic_backfill=False,
                        context=schedule_id,
                    )
        except dbos_error.DBOSException as error:
            raise ScheduleError(
                ScheduleErrorCode.ENGINE_REJECTED,
                "the engine rejected the schedule definition",
            ) from error
        record = await self.get(schedule_id)
        if record is None:  # the row was just committed in this transaction
            raise ScheduleError(
                ScheduleErrorCode.ENGINE_REJECTED,
                "the schedule row vanished inside its creation transaction",
            )
        return record

    async def set_enabled(self, schedule_id: str, *, enabled: bool) -> ScheduleRecord:
        """Pause/resume the engine trigger and update the domain row.

        The engine trigger is toggled FIRST (fail fast, nothing written on
        rejection); the domain ``enabled`` flag stays the dispatch-time
        authority — :func:`dispatch_fire` re-checks it inside the
        dispatch transaction, so a paused schedule never dispatches even
        if its trigger fires once more before the engine notices.
        """
        schedule_id = _canonical_uuid(
            schedule_id, ScheduleErrorCode.INVALID_SCHEDULE_ID, "schedule_id"
        )
        engine_schedule = await asyncio.to_thread(
            self._client.get_schedule, schedule_id
        )
        if engine_schedule is None:
            raise ScheduleError(
                ScheduleErrorCode.SCHEDULE_NOT_FOUND,
                "no schedule record for this schedule_id",
            )
        toggle = (
            self._client.resume_schedule if enabled else self._client.pause_schedule
        )
        await asyncio.to_thread(toggle, schedule_id)
        async with self._sessions() as session:
            async with session.begin():
                row = (
                    (
                        await session.execute(
                            text(
                                "UPDATE job_schedules SET enabled = :enabled, "
                                "updated_at = now() WHERE schedule_id = :s "
                                f"RETURNING {_SELECT_SCHEDULE_COLUMNS}"
                            ),
                            {
                                "enabled": enabled,
                                "s": uuid.UUID(schedule_id),
                            },
                        )
                    )
                    .mappings()
                    .first()
                )
        if row is None:
            raise ScheduleError(
                ScheduleErrorCode.SCHEDULE_NOT_FOUND,
                "no schedule record for this schedule_id",
            )
        return _record_from_row(row)

    async def delete(self, schedule_id: str) -> None:
        """Remove the domain row (fires cascade) and the engine trigger.

        The row goes first: a schedule that dispatches in the window
        before the trigger is removed finds no row and skips value-free,
        so the schedule can never fire after its record is gone.
        Unknown IDs are rejected (mirrors the adapter's structured
        unknown-run rejection).
        """
        schedule_id = _canonical_uuid(
            schedule_id, ScheduleErrorCode.INVALID_SCHEDULE_ID, "schedule_id"
        )
        async with self._sessions() as session:
            async with session.begin():
                deleted = (
                    await session.execute(
                        text(
                            "DELETE FROM job_schedules WHERE schedule_id = :s "
                            "RETURNING schedule_id"
                        ),
                        {"s": uuid.UUID(schedule_id)},
                    )
                ).first()
        if deleted is None:
            raise ScheduleError(
                ScheduleErrorCode.SCHEDULE_NOT_FOUND,
                "no schedule record for this schedule_id",
            )
        # Engine delete is a no-op on an unknown name (observed); the
        # domain rejection above is the structured boundary.
        await asyncio.to_thread(self._client.delete_schedule, schedule_id)

    # -- read path -----------------------------------------------------------

    async def get(self, schedule_id: str) -> ScheduleRecord | None:
        """One persisted schedule record, or ``None``."""
        schedule_id = _canonical_uuid(
            schedule_id, ScheduleErrorCode.INVALID_SCHEDULE_ID, "schedule_id"
        )
        async with self._sessions() as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"SELECT {_SELECT_SCHEDULE_COLUMNS} FROM job_schedules "
                            "WHERE schedule_id = :s"
                        ),
                        {"s": uuid.UUID(schedule_id)},
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _record_from_row(row)

    async def list(self, server_id: str | None = None) -> tuple[ScheduleRecord, ...]:
        """All persisted schedules, oldest first; optionally one server's."""
        where = ""
        params: dict[str, object] = {}
        if server_id is not None:
            where = " WHERE server_id = :server"
            params["server"] = uuid.UUID(
                _canonical_uuid(
                    server_id, ScheduleErrorCode.INVALID_SERVER_ID, "server_id"
                )
            )
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {_SELECT_SCHEDULE_COLUMNS} FROM job_schedules"
                        f"{where} ORDER BY created_at, schedule_id"
                    ),
                    params,
                )
            ).mappings()
        return tuple(_record_from_row(row) for row in rows)


# ---------------------------------------------------------------------------
# dispatch resources (worker process, resolved lazily or injected)
# ---------------------------------------------------------------------------


class DispatchResources:
    """What the dispatch step needs: app sessions + the engine submit seam.

    One instance per process. The worker path resolves both from the
    single ``mai.database`` URL contract on first fire; tests inject
    explicitly with :func:`configure_dispatch` so no engine or loop
    state is ever shared across loops.
    """

    __slots__ = ("app_engine", "sessions", "client")

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        client: EngineClient,
        app_engine: AsyncEngine | None = None,
    ) -> None:
        self.sessions = sessions
        self.client = client
        self.app_engine = app_engine  # owned only on the lazy worker path


_DISPATCH: DispatchResources | None = None


def configure_dispatch(
    sessions: async_sessionmaker[AsyncSession], client: EngineClient
) -> None:
    """Inject dispatch resources (control-plane/test path).

    The lazy worker path never runs in such a process, so injected
    resources keep every fire-adjacent call on explicit, loop-local
    handles.
    """
    global _DISPATCH
    if _DISPATCH is not None:
        raise ScheduleError(
            ScheduleErrorCode.DISPATCH_ALREADY_CONFIGURED,
            "dispatch resources are already configured in this process",
        )
    _DISPATCH = DispatchResources(sessions=sessions, client=client)


async def aclose_dispatch() -> None:
    """Release injected/resolved dispatch resources (idempotent)."""
    global _DISPATCH
    if _DISPATCH is not None:
        resources, _DISPATCH = _DISPATCH, None
        resources.client.close()  # sync engine-client pool release
        if resources.app_engine is not None:
            await resources.app_engine.dispose()


def _resolve_dispatch() -> DispatchResources:
    """Worker path: resolve resources from the production URL contract."""
    global _DISPATCH
    if _DISPATCH is None:
        url = resolve_database_url()
        app_engine, sessions = create_app_engine(url)
        _DISPATCH = DispatchResources(
            sessions=sessions, client=EngineClient(url), app_engine=app_engine
        )
    return _DISPATCH


# ---------------------------------------------------------------------------
# the one durable fire-dispatch path
# ---------------------------------------------------------------------------


async def dispatch_fire(
    resources: DispatchResources, schedule_id: str, planned_fire_utc: datetime
) -> str | None:
    """Dispatch one planned fire exactly once; return the run identity.

    One transaction (the C05 rule 8 dedup is the ledger's primary key):

    1. The schedule row is read; a missing or disabled row means the
       engine trigger fired without a live dispatch intent — skip
       value-free (``None``, no ledger row, no run).
    2. The ledger row ``(schedule_id, planned_fire_time)`` is inserted
       ``ON CONFLICT DO NOTHING``. A conflict means this fire was already
       dispatched (replay, duplicate poller, recovery) — return the
       deterministic identity WITHOUT re-enqueueing.
    3. Fresh insert: the run intent is enqueued through
       :meth:`mai.jobs.engine.EngineClient.enqueue_run` under the
       deterministic :func:`planned_run_id` (the engine's reject policy
       turns a duplicate submit into a structured conflict, which IS the
       exactly-once guarantee — the run intent already exists). The
       enqueue happens BEFORE the commit: a crash anywhere leaves either
       a committed dispatch or a clean replay that converges on the same
       identity and ledger row.

    Returns the run ID, or ``None`` when the fire was skipped.
    """
    schedule_id = _canonical_uuid(
        schedule_id, ScheduleErrorCode.INVALID_SCHEDULE_ID, "schedule_id"
    )
    planned_fire_utc = validate_fire_time(planned_fire_utc).astimezone(timezone.utc)
    run_id = planned_run_id(schedule_id, planned_fire_utc)
    async with resources.sessions() as session:
        async with session.begin():
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT enabled, misfire_policy, run_spec FROM job_schedules "
                            "WHERE schedule_id = :s"
                        ),
                        {"s": uuid.UUID(schedule_id)},
                    )
                )
                .mappings()
                .first()
            )
            if row is None or not row["enabled"]:
                return None  # no live dispatch intent behind the trigger
            spec = parse_run_spec(row["run_spec"])
            if row["misfire_policy"] != MISFIRE_POLICY_SKIP_MISSED:
                raise ScheduleError(
                    ScheduleErrorCode.MISFIRE_POLICY_DRIFT,
                    "job_schedules row carries a policy outside the frozen vocabulary",
                )
            inserted = (
                await session.execute(
                    text(
                        "INSERT INTO job_schedule_fires (schedule_id, "
                        "planned_fire_time) VALUES (:s, :t) "
                        "ON CONFLICT DO NOTHING RETURNING schedule_id"
                    ),
                    {"s": uuid.UUID(schedule_id), "t": planned_fire_utc},
                )
            ).first()
            if not inserted:
                return run_id  # already dispatched: the identity IS the answer
            try:
                resources.client.enqueue_run(
                    run_id, spec.handler_name, spec.payload_json()
                )
            except RunIdInUseError:
                pass  # the planned run intent already exists — exactly once
            return run_id


@DBOS.step(name=DISPATCH_FIRE_STEP_NAME)
async def dispatch_fire_step(schedule_id: str, planned_fire_utc_iso: str) -> str | None:
    """The durable dispatch step; its checkpoint makes firing idempotent.

    Completed executions replay from the checkpoint without re-running;
    an in-flight execution re-runs into the ledger's primary key, which
    converges on the same identity (guard rule 1: idempotent by
    construction). The fire time crosses the boundary as its canonical
    UTC ISO text so the checkpointed inputs stay explicit.
    """
    planned = datetime.fromisoformat(planned_fire_utc_iso)
    return await dispatch_fire(_resolve_dispatch(), schedule_id, planned)


@DBOS.workflow(name=SCHEDULE_FIRE_WORKFLOW_NAME)
async def schedule_fire_workflow(scheduled_at: datetime, context: object) -> str | None:
    """The one scheduled facade workflow (engine rule 7).

    Invoked by the DBOS scheduler with the planned fire time as the
    first argument and the owning schedule ID as the context (set at
    :meth:`ScheduleManager.create`). Fire identity, dedup and dispatch
    are delegated to :func:`dispatch_fire_step`.
    """
    validate_fire_time(scheduled_at)
    if not isinstance(context, str):
        raise ScheduleError(
            ScheduleErrorCode.INVALID_SCHEDULE_ID, "fire context must be a schedule id"
        )
    planned_utc = scheduled_at.astimezone(timezone.utc)
    return await dispatch_fire_step(context, planned_utc.isoformat())
