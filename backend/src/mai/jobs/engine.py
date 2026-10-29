"""The one durable-engine adapter: DBOS vocabulary stops here (C05 rule 1).

Frozen boundary for contracts.md **C05** and the job-engine ADR (`bc8d264`,
DBOS `dbos==3.2.0`). There is no second scheduler and no queue outside the
DBOS queue owned by this module: the facade submits runs through
:class:`EngineClient` (a bare :class:`dbos.DBOSClient`), workers execute them
through :func:`launch_worker` (a full engine that runs launch recovery), and
every engine status leaving this module is already mapped onto the frozen
:class:`mai.contracts.jobs.RunState` vocabulary or raises.

Responsibility split (ADR "Responsibility split" + guard rules):

- **Engine-owned here** — checkpoint/replay, workflow-step state, queue
  claim, launch recovery, schedule storage. DBOS system tables (the ``dbos``
  schema of Machina's own Postgres) are the canonical run-state owner
  (C05 rule 1); this module is the only code path that reads them.
- **Facade-owned elsewhere** (05-run-lifecycle, 05-steps, 05-jobs-api,
  05-schedules) — domain ``job_runs``/``job_steps``/``job_events`` rows, the
  event projection, schedule records and the fire ledger, approval gating,
  reconciliation of uncertain outcomes. This module never writes domain
  tables and never invents domain states.

Guard rules with code-visible consequences:

- **Rule 5 (submit dedup):** ``run_id`` IS the DBOS workflow ID; every
  enqueue uses ``workflow_id_reuse_policy="reject"`` so a lost ID race
  surfaces as :class:`RunIdInUseError` (a structured conflict), never a
  silent attach and never a second run.
- **Rules 3–4 (cancellation):** :meth:`EngineClient.request_cancel` only
  *requests* cancellation — DBOS lands it at the next step boundary and
  in-flight non-preemptible steps run to completion. The method returns the
  moment the request is durably recorded; callers must re-check
  :meth:`EngineClient.run_state` instead of assuming ``cancelled``.
- **Rule 8 (recovery):** relaunching a worker recovers PENDING workflows;
  ERROR runs stay ERROR until an explicit :meth:`EngineClient.resume_run`.
  There is no automatic blind replay in this module.
- **Out-of-process control:** control-plane callers (HTTP API, tests acting
  as the API) MUST use :class:`EngineClient`. A second full engine in the
  API process would steal pending workflows via launch recovery (observed
  during the P00 probe) — :func:`launch_worker` is for worker processes only.

Recovery/app-version pinning: DBOS 3.2.0 launch recovery only re-executes
PENDING workflows whose recorded ``application_version`` equals the
recovering engine's version (``get_pending_workflows`` filters
``application_version == app_version`` exactly). A bare client and a worker
compute different implicit versions, so this adapter pins ONE explicit
version — :data:`APPLICATION_VERSION` — on both sides (enqueue options and
the worker's ``DBOS__APPVERSION``). Bump it deliberately when the durable
workflow surface of this module changes; it is the recovery compatibility
boundary across deployments.

Payload discipline (C05 rule 3): ``payload_json`` is persisted verbatim by
the engine (workflow inputs live in Machina's Postgres). The facade owns its
content — references and identifiers, never secret values; this module only
checks it is a JSON string-shaped value.

The module registers exactly one durable workflow: the :func:`run_workflow`
trampoline, which resolves a registered handler by name and executes it in
workflow context (handlers then use durable DBOS steps). Real handlers are
registered by their owner cards; worker entrypoints import the modules that
call :func:`register_job_handler` before launch.

This module is worker/SSR safe: it reads no ambient state beyond the
arguments it is given and never logs connection material.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import threading
import uuid
from enum import StrEnum
from typing import Final

from dbos import DBOS, DBOSClient, EnqueueOptions
from dbos import error as dbos_error

from mai.contracts.jobs import RunState, run_state_from_engine

#: DBOS application name for Machina (worker config + enqueue ownership).
APPLICATION_NAME: Final[str] = "mai"

#: Explicit recovery-compatibility version (see module docstring). Recorded on
#: every enqueue and pinned at worker launch so launch recovery finds pending
#: workflows across processes.
APPLICATION_VERSION: Final[str] = "mai-1"

#: The single DBOS queue for Machina runs (C05 rule 1: no hidden queue).
QUEUE_NAME: Final[str] = "mai-jobs"

#: Default per-worker queue concurrency. Child delegation workflows are
#: independent ``DBOS.start_workflow`` children, NOT queue entries, so a
#: parent yielding on child handles never deadlocks worker slots (C05 rule 7).
DEFAULT_QUEUE_CONCURRENCY: Final[int] = 8

#: Name of the durable trampoline workflow (DBOS records the function name).
RUN_WORKFLOW_NAME: Final[str] = "run_workflow"

#: Handler names share the frozen dotted-slug discipline of event types
#: (lowercase, ASCII, dot-separated segments) — one vocabulary style for the
#: jobs module, rejected value-free otherwise.
_HANDLER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]+)*$")

#: Only the plain psycopg URL form is accepted: it is the measured
#: configuration (job-engine ADR). Driver-qualified variants are a deliberate
#: reconciliation, not a silent accept.
_ALLOWED_URL_SCHEMES: Final[frozenset[str]] = frozenset({"postgresql"})


class EngineErrorCode(StrEnum):
    """Adapter-boundary rejection codes (value-free, C01 envelope style)."""

    INVALID_DATABASE_URL = "invalid_database_url"
    INVALID_RUN_ID = "invalid_run_id"
    INVALID_HANDLER_NAME = "invalid_handler_name"
    INVALID_PAYLOAD = "invalid_payload"
    INVALID_CONCURRENCY = "invalid_concurrency"
    ENGINE_ALREADY_LAUNCHED = "engine_already_launched"
    ENGINE_NOT_LAUNCHED = "engine_not_launched"
    RUN_ID_IN_USE = "run_id_in_use"
    RUN_NOT_FOUND = "run_not_found"
    HANDLER_NOT_FOUND = "handler_not_found"


class EngineError(RuntimeError):
    """One value-free adapter rejection: code + fixed reason.

    Messages carry codes, field names and opaque identifiers only — never
    payload values or connection material — so they are safe to log, store
    and surface (C01 rule 3).
    """

    def __init__(self, code: EngineErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class RunIdInUseError(EngineError):
    """Structured conflict for a resubmitted run ID (C05 rule 5, guard 5)."""

    def __init__(self, run_id: str) -> None:
        super().__init__(
            EngineErrorCode.RUN_ID_IN_USE,
            f"run_id {run_id} already belongs to an existing engine workflow",
        )
        self.run_id = run_id


class UnknownRunError(EngineError):
    """The engine has no workflow under this run ID (cancel/resume/await)."""

    def __init__(self, run_id: str) -> None:
        super().__init__(
            EngineErrorCode.RUN_NOT_FOUND,
            f"engine has no workflow for run_id {run_id}",
        )
        self.run_id = run_id


class UnknownHandlerError(EngineError):
    """Raised inside the trampoline when the handler is not registered.

    Surfaces as an ERROR (domain ``failed``) run — never a silent no-op.
    """

    def __init__(self, handler_name: str) -> None:
        super().__init__(
            EngineErrorCode.HANDLER_NOT_FOUND,
            f"no run handler registered under name '{handler_name}'",
        )
        self.handler_name = handler_name


# ---------------------------------------------------------------------------
# validation (pure, value-free)
# ---------------------------------------------------------------------------


def validate_run_id(run_id: object) -> str:
    """Require a canonical lowercase hyphenated UUID string (C01 rule 1)."""
    if not isinstance(run_id, str):
        raise EngineError(EngineErrorCode.INVALID_RUN_ID, "run_id must be a string")
    try:
        parsed = uuid.UUID(run_id)
    except ValueError:
        raise EngineError(
            EngineErrorCode.INVALID_RUN_ID, "run_id must be a canonical UUID string"
        ) from None
    if str(parsed) != run_id:
        raise EngineError(
            EngineErrorCode.INVALID_RUN_ID,
            "run_id must be canonical (lowercase, hyphenated) form",
        )
    return run_id


def validate_handler_name(name: object) -> str:
    """Require a non-empty dotted lowercase slug."""
    if not isinstance(name, str) or not _HANDLER_NAME_RE.fullmatch(name):
        raise EngineError(
            EngineErrorCode.INVALID_HANDLER_NAME,
            "handler name must be a non-empty lowercase dotted slug",
        )
    return name


def validate_payload_json(payload_json: object) -> str:
    """Require a string (the persisted run-intent JSON document)."""
    if not isinstance(payload_json, str):
        raise EngineError(
            EngineErrorCode.INVALID_PAYLOAD, "payload_json must be a string"
        )
    return payload_json


def validate_database_url(url: object) -> str:
    """Require the measured plain ``postgresql://`` URL form."""
    if not isinstance(url, str) or not url.startswith("postgresql://"):
        raise EngineError(
            EngineErrorCode.INVALID_DATABASE_URL,
            "system database URL must use the plain postgresql:// form",
        )
    return url


def validate_queue_concurrency(concurrency: object) -> int:
    """Require a positive integer worker concurrency."""
    if (
        not isinstance(concurrency, int)
        or isinstance(concurrency, bool)
        or concurrency < 1
    ):
        raise EngineError(
            EngineErrorCode.INVALID_CONCURRENCY,
            "queue concurrency must be a positive integer",
        )
    return concurrency


# ---------------------------------------------------------------------------
# handler registry + the one durable workflow
# ---------------------------------------------------------------------------

#: Run handlers registered before worker launch. A handler receives the run's
#: ``payload_json`` and executes in workflow context (its own ``DBOS.step``
#: calls are durable). Registration is a worker-process concern; control-plane
#: processes only pass the name.
_RUN_HANDLERS: dict[str, "object"] = {}


def register_job_handler(name: str, handler: object) -> None:
    """Register a run handler under a stable dotted-slug name.

    Must happen before :func:`launch_worker` in the worker process. Missing
    handlers fail the run at execution time (ERROR — domain ``failed``).
    """
    validate_handler_name(name)
    if not callable(handler):
        raise EngineError(
            EngineErrorCode.INVALID_HANDLER_NAME, "handler must be callable"
        )
    _RUN_HANDLERS[name] = handler

#: The one persistent loop async run handlers execute on (W3). Unlike a
#: per-run ``asyncio.run`` loop it is NEVER closed mid-process: pooled
#: async DB connections and loop-registered waiters survive every run.
#: Sync handlers never touch it — they run on their own workflow thread
#: exactly as before, so ``DBOS.start_workflow`` children (which the
#: sync child-start path executes on workflow threads) are unchanged.
_ASYNC_HANDLER_LOOP: asyncio.AbstractEventLoop | None = None
_ASYNC_HANDLER_LOOP_LOCK = threading.Lock()


def _async_handler_loop() -> asyncio.AbstractEventLoop:
    """The lazily-started persistent loop (worker process concern)."""
    global _ASYNC_HANDLER_LOOP
    with _ASYNC_HANDLER_LOOP_LOCK:
        if _ASYNC_HANDLER_LOOP is None:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="mai-async-handler-loop", daemon=True
            )
            thread.start()
            _ASYNC_HANDLER_LOOP = loop
    return _ASYNC_HANDLER_LOOP


@DBOS.workflow(name=RUN_WORKFLOW_NAME)
def run_workflow(handler_name: str, payload_json: str) -> object:
    """The one durable Machina run workflow (registered at import).

    W3: an async handler (the agent run — its ONE bounded intake call
    must be an awaited, deadline-supervised step) is driven to
    completion on the persistent async-handler loop, right here on this
    workflow thread; sync handlers (steps/delegation/schedules) keep
    today's direct call on today's thread substrate. The durable
    surface (name, inputs, step records) is unchanged — recorded runs
    replay unchanged, so :data:`APPLICATION_VERSION` stays put.
    """
    validate_handler_name(handler_name)
    validate_payload_json(payload_json)
    handler = _RUN_HANDLERS.get(handler_name)
    if handler is None:
        raise UnknownHandlerError(handler_name)
    if inspect.iscoroutinefunction(handler):
        return asyncio.run_coroutine_threadsafe(
            handler(payload_json), _async_handler_loop()
        ).result()
    return handler(payload_json)


# ---------------------------------------------------------------------------
# worker side: the full engine
# ---------------------------------------------------------------------------

_LAUNCHED: "DBOS | None" = None


def launch_worker(
    system_database_url: str,
    *,
    queue_concurrency: int = DEFAULT_QUEUE_CONCURRENCY,
) -> None:
    """Configure and launch the full engine in a WORKER process.

    Runs DBOS launch recovery (pending workflows of the pinned application
    version are re-executed) and registers the Machina queue. Exactly one
    engine per process: a second launch raises. Control-plane processes must
    NOT call this (a second full engine steals pending workflows).
    """
    global _LAUNCHED
    if _LAUNCHED is not None:
        raise EngineError(
            EngineErrorCode.ENGINE_ALREADY_LAUNCHED,
            "the engine is already launched in this process",
        )
    validate_database_url(system_database_url)
    validate_queue_concurrency(queue_concurrency)
    # Pin the recovery version BEFORE engine init (DBOS reads it there); both
    # sides of an enqueue/recover pair must agree (see module docstring).
    os.environ["DBOS__APPVERSION"] = APPLICATION_VERSION
    dbos = DBOS(
        config={
            "name": APPLICATION_NAME,
            "system_database_url": system_database_url,
            "log_level": "INFO",
        }
    )
    dbos.launch()
    dbos.register_queue(QUEUE_NAME, worker_concurrency=queue_concurrency)
    dbos.retrieve_queue(QUEUE_NAME)  # claim surface exists before we report ready
    _LAUNCHED = dbos


def shutdown() -> None:
    """Stop the engine launched by this process (idempotent, safe if absent)."""
    global _LAUNCHED
    if _LAUNCHED is not None:
        _LAUNCHED.destroy()
        _LAUNCHED = None


# ---------------------------------------------------------------------------
# control plane: the bare client the facade uses
# ---------------------------------------------------------------------------


class EngineClient:
    """Durable enqueue/claim/state control surface over a bare DBOS client.

    This is the seam the application facade builds on (C05 rule 1). It never
    launches an engine, so it can live in the API process safely. The system
    database connection is opened lazily on first use; failures surface from
    the driver as-is (operational, not a contract rejection).

    Ordering: the engine-owned ``dbos`` schema is created by a full engine
    launch (any worker start), not by the client and not by Alembic. A client
    against a database no engine ever launched fails on first use — start a
    worker first (the Compose stack does: ``mai-worker`` runs beside
    ``mai-api``).
    """

    def __init__(self, system_database_url: str) -> None:
        validate_database_url(system_database_url)
        self._url = system_database_url
        self._client = DBOSClient(
            system_database_url=system_database_url,
            application_name=APPLICATION_NAME,
            lazy=True,
        )

    def close(self) -> None:
        """Release the client's system-database pool (idempotent)."""
        self._client.destroy()

    def __enter__(self) -> "EngineClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def enqueue_run(self, run_id: str, handler_name: str, payload_json: str) -> None:
        """Durably enqueue a run: ``run_id`` is the deterministic workflow ID.

        Duplicate submits with the same ``run_id`` NEVER attach to the
        existing run: the reject policy surfaces :class:`RunIdInUseError`
        (C05 rule 5). The run is claimed from the Machina queue by a worker.
        """
        validate_run_id(run_id)
        validate_handler_name(handler_name)
        validate_payload_json(payload_json)
        options: EnqueueOptions = {
            "workflow_name": RUN_WORKFLOW_NAME,
            "queue_name": QUEUE_NAME,
            "workflow_id": run_id,
            "workflow_id_reuse_policy": "reject",
            "app_version": APPLICATION_VERSION,
            "application_name": APPLICATION_NAME,
        }
        try:
            self._client.enqueue(options, handler_name, payload_json)
        except dbos_error.DBOSWorkflowIDInUseError:
            raise RunIdInUseError(run_id) from None

    def run_state(self, run_id: str) -> RunState | None:
        """Map the engine's workflow status onto the frozen run state.

        ``None`` when the engine has no such workflow. Any unmapped engine
        value fails closed via :class:`mai.contracts.jobs.EngineStateError`
        (C05 rule 2) — never guessed.
        """
        validate_run_id(run_id)
        try:
            status = self._client.retrieve_workflow(run_id).get_status()
        except dbos_error.DBOSNonExistentWorkflowError:
            return None
        return run_state_from_engine(status.status)

    def request_cancel(self, run_id: str, *, cancel_children: bool = False) -> None:
        """Request cancellation — requested, never verified (guard rules 3–4).

        DBOS lands the cancellation at the next step boundary; in-flight
        non-preemptible steps complete and their effects happen. Callers must
        re-check :meth:`run_state` instead of assuming ``cancelled``. Tree
        cancellation is explicit (guard rule 4): children keep running unless
        ``cancel_children`` is set.
        """
        validate_run_id(run_id)
        try:
            # DBOS cancel/resume are bulk updates that no-op silently on an
            # unknown ID; the facade must get a structured rejection instead.
            self._client.retrieve_workflow(run_id)
            self._client.cancel_workflow(run_id, cancel_children=cancel_children)
        except dbos_error.DBOSNonExistentWorkflowError:
            raise UnknownRunError(run_id) from None

    def resume_run(self, run_id: str) -> None:
        """Deliberately resume an ERROR/CANCELLED run (guard rule 8).

        Recovery is never automatic and never blind: the facade decides.
        """
        validate_run_id(run_id)
        try:
            # Same silent-no-op caveat as request_cancel.
            self._client.retrieve_workflow(run_id)
            self._client.resume_workflow(run_id)
        except dbos_error.DBOSNonExistentWorkflowError:
            raise UnknownRunError(run_id) from None

    def await_run(self, run_id: str) -> object:
        """Block until the run finishes; returns the handler's return value.

        A cancelled run raises ``DBOSAwaitedWorkflowCancelledError``; a failed
        run re-raises its (value-free) workflow exception. Unknown IDs raise
        :class:`UnknownRunError`.
        """
        validate_run_id(run_id)
        try:
            return self._client.retrieve_workflow(run_id).get_result()
        except dbos_error.DBOSNonExistentWorkflowError:
            raise UnknownRunError(run_id) from None

    def send_message(
        self,
        run_id: str,
        message: object,
        *,
        topic: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        """Send a durable workflow message to ``run_id`` on ``topic``."""
        validate_run_id(run_id)
        try:
            self._client.retrieve_workflow(run_id)
            self._client.send(
                run_id,
                message,
                topic=topic,
                idempotency_key=idempotency_key,
            )
        except dbos_error.DBOSNonExistentWorkflowError:
            raise UnknownRunError(run_id) from None
