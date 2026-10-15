"""Focused tests for the schedule facade (card 05-schedules).

Acceptance (BRIEF):

1. Timezone/misfire behavior follows the frozen policy (C05 rule 8,
   engine rules 6–7): an explicit ``cron_timezone`` (default UTC) rides
   on the record and the derived engine trigger, the trigger is created
   with ``automatic_backfill=False`` (skip missed fires, resume from
   now, no catch-up), and the planned-fire identity —
   :func:`mai.jobs.schedules.planned_run_id` — is one deterministic run
   ID per ``(schedule_id, planned_fire_time)`` across restarts, replay
   and duplicate pollers.
2. Schedules persist independently of browser and model availability:
   records live in ``job_schedules`` (Machina Postgres), triggers in the
   engine's system tables, and a real DBOS scheduler dispatches a
   near-term benign run through the one queue — no browser, no model,
   no second scheduler anywhere. Conditional scripting is NOT
   implemented (negative boundary: the run spec has exactly two keys and
   nothing scriptable).

Boundary discipline (test policy): every durable claim runs against a
real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``) with REAL
worker processes — the feature-gate scenario ("real scheduler fires a
near-term benign task, restart across due time and run duplicate
pollers; inspect one planned-fire intent and chosen misfire result") is
proven by :class:`TestScheduledFire` (two concurrent workers, one fire)
and :class:`TestMisfireSkipMissed` (fires due while no worker runs are
skipped; the next fire after restart dispatches exactly once). The pure
validation and identity boundaries are unit-tested without a fixture.
Mocks re-implement nothing under test. Fixtures carry only tmp-namespace
paths and marker words — never secret values.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.jobs import RunState
from mai.database import create_app_engine
from mai.jobs.engine import RUN_WORKFLOW_NAME, EngineClient
from mai.jobs.schedules import (
    DISPATCH_FIRE_STEP_NAME,
    FIRE_RUN_NAMESPACE,
    SCHEDULE_FIRE_WORKFLOW_NAME,
    DispatchResources,
    RunSpec,
    ScheduleError,
    ScheduleErrorCode,
    ScheduleManager,
    dispatch_fire,
    parse_run_spec,
    planned_run_id,
    validate_schedule_cron,
    validate_schedule_timezone,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Scratch namespace per runtime-environment ADR D2/D4 (recorded + cleaned
# in the card evidence). Each live test gets a fresh subdirectory.
FIXTURE_ROOT = Path("/tmp/mai-fxt-05-schedules")

# Worker waits are bounded; the restart scenario crosses minute-granularity
# fires, so the live suite runs in minutes, not seconds.
FIRE_TIMEOUT = 150.0

# Canonical fixture identities (opaque engine IDs; C01 rule 1).
SERVER_ID = "5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60"
OTHER_SERVER_ID = "0f6e4d3c-2b1a-49f0-8e5f-6a7b8c9d0e1f"

_MISFIRE_SKIPPED_CRON = "*/7 * * * * *"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _cron_for_seconds(seconds: set[int]) -> str:
    """A seconds-allowed cron firing at the given seconds of every minute."""
    if not seconds or any(not 0 <= s <= 59 for s in seconds):
        raise AssertionError("fire seconds must be within one minute")
    return f"{','.join(str(s) for s in sorted(seconds))} * * * * *"


def _next_fire(at_seconds: set[int], after: datetime) -> datetime:
    """The next whole second after ``after`` whose second-of-minute fires."""
    moment = (after + timedelta(seconds=1)).replace(microsecond=0)
    for _ in range(120):
        if moment.second in at_seconds:
            return moment
        moment += timedelta(seconds=1)
    raise AssertionError("no fire second within 120s of the probe")


def _journal(scenario: Path) -> Path:
    return scenario / "journal.log"


def _journal_count(scenario: Path, marker: str) -> int:
    try:
        lines = _journal(scenario).read_text().splitlines()
    except FileNotFoundError:
        return 0
    return sum(1 for line in lines if line == marker)


def _wait_until(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _pg_rows(url: str, sql: str, params: dict[str, object]) -> list[tuple]:
    """Fixture-side engine-table reads (harness inspection, not API)."""
    with psycopg.connect(url) as conn:
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
    return rows


def _pg_scalar(url: str, sql: str, params: dict[str, object]) -> object:
    """Fixture-side single-value read (harness inspection, not API)."""
    with psycopg.connect(url) as conn:
        value = conn.execute(sql, params).fetchone()[0]
        conn.commit()
    return value


def _engine_run_row(url: str, run_id: str, status: str) -> object:
    """Count the engine's run workflows under one run ID and status.

    The engine run workflow UUID is the planned-fire identity: this is
    where a fire's run intent is durably inspected (the ledger's
    ``run_id`` link stays open until the run-row owner card writes
    ``job_runs`` rows).
    """
    return _pg_scalar(
        url,
        "SELECT count(*) FROM dbos.workflow_status "
        "WHERE workflow_uuid = %(r)s AND name = %(n)s AND status = %(st)s",
        {"r": run_id, "n": RUN_WORKFLOW_NAME, "st": status},
    )


# ---------------------------------------------------------------------------
# scenario handler module (written to the scratch namespace; imported ONLY
# by worker subprocesses — never inside pytest, so no registration races)
# ---------------------------------------------------------------------------

HANDLER_MODULE_NAME = "schedules_scenario_handlers"

HANDLER_MODULE_SOURCE = '''
"""Scenario handlers for 05-schedules: harmless, journaled effects.

Importing ``mai.jobs.schedules`` registers the durable fire-dispatch
workflow exactly the way production worker wiring will.
"""

import json

from dbos import DBOS

import mai.jobs.schedules  # noqa: F401 - registers the fire workflow
from mai.jobs import engine


@DBOS.step()
def _mark(path: str, marker: str) -> None:
    with open(path, "a") as f:
        f.write(marker + "\\n")


def echo(payload_json: str) -> str:
    payload = json.loads(payload_json)
    _mark(payload["journal"], payload["marker"])
    return payload["marker"]


engine.register_job_handler("schedule.echo", echo)
'''


def _spawn_worker(
    url: str, handler_dir: Path, scenario: Path, tag: str
) -> subprocess.Popen:
    """Launch a REAL worker process against the fixture database."""
    env = dict(os.environ)
    env.pop(LIVE_URL_VAR, None)  # workers speak the production env contract
    env["MAI_DATABASE_URL"] = url
    env["MAI_WORKER_HANDLERS"] = HANDLER_MODULE_NAME
    env["PYTHONPATH"] = os.pathsep.join(
        [str(handler_dir), *(p for p in [env.get("PYTHONPATH")] if p)]
    )
    log = open(scenario / f"{tag}.log", "w")  # noqa: SIM115 - closed with proc
    return subprocess.Popen(
        [sys.executable, "-m", "mai.jobs.worker", "--concurrency", "2"],
        cwd=str(BACKEND_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _stop(proc: subprocess.Popen) -> int:
    proc.send_signal(signal.SIGTERM)
    return proc.wait(timeout=30)


# ---------------------------------------------------------------------------
# unit half: pure validation + the planned-fire identity (no engine, no DB)
# ---------------------------------------------------------------------------


class TestRunSpecContract:
    def test_valid_spec_round_trips(self) -> None:
        spec = parse_run_spec(
            {"handler_name": "schedule.echo", "payload": {"journal": "/tmp/j"}}
        )
        assert spec.handler_name == "schedule.echo"
        assert spec.payload == {"journal": "/tmp/j"}
        assert (
            spec.payload_json() == '{"journal":"/tmp/j"}'
        )  # canonical: sorted, tight, ASCII

    def test_payload_json_is_key_order_insensitive(self) -> None:
        one = parse_run_spec(
            {"handler_name": "echo", "payload": {"a": 1, "b": {"c": 2}}}
        ).payload_json()
        two = parse_run_spec(
            {"handler_name": "echo", "payload": {"b": {"c": 2}, "a": 1}}
        ).payload_json()
        assert one == two == '{"a":1,"b":{"c":2}}'

    def test_shape_must_be_exactly_two_keys(self) -> None:
        for bad in (
            None,
            [],
            {},
            {"handler_name": "echo"},
            {"payload": {}},
            {"handler_name": "echo", "payload": {}, "when": "later"},
        ):
            with pytest.raises(ScheduleError) as excinfo:
                parse_run_spec(bad)
            assert excinfo.value.code is ScheduleErrorCode.INVALID_RUN_SPEC

    def test_handler_must_be_a_registered_name_slug(self) -> None:
        for bad in ("", "Echo", "a b", "ünïcode", 42):
            with pytest.raises(ScheduleError) as excinfo:
                parse_run_spec({"handler_name": bad, "payload": {}})
            assert excinfo.value.code is ScheduleErrorCode.INVALID_RUN_SPEC

    def test_payload_must_be_a_canonicalizable_object(self) -> None:
        with pytest.raises(ScheduleError) as excinfo:
            parse_run_spec({"handler_name": "echo", "payload": [1, 2]})
        assert excinfo.value.code is ScheduleErrorCode.INVALID_RUN_SPEC
        with pytest.raises(ScheduleError) as excinfo:
            parse_run_spec({"handler_name": "echo", "payload": {"x": float("nan")}})
        assert excinfo.value.code is ScheduleErrorCode.INVALID_RUN_SPEC

    def test_conditional_scripting_is_not_a_shape(self) -> None:
        """Negative boundary: no scriptable/conditional key is accepted."""
        for scripted in (
            {"handler_name": "echo", "payload": {}, "condition": "x > 1"},
            {"handler_name": "echo", "payload": {}, "script": "return true"},
        ):
            with pytest.raises(ScheduleError):
                parse_run_spec(scripted)


class TestCronAndTimezoneValidation:
    def test_field_contract_is_enforced(self) -> None:
        for bad in (None, 42, "", " " * 201, "0123456789" * 21, "*/5\x00 * * * * *"):
            with pytest.raises(ScheduleError) as excinfo:
                validate_schedule_cron(bad)
            assert excinfo.value.code is ScheduleErrorCode.INVALID_CRON

    def test_grammar_is_the_engine_grammar(self) -> None:
        for bad in ("not a cron", "* * * *", "61 * * * * *", "* * * */7"):
            with pytest.raises(ScheduleError) as excinfo:
                validate_schedule_cron(bad)
            assert excinfo.value.code is ScheduleErrorCode.INVALID_CRON
        assert validate_schedule_cron("* * * * *") == "* * * * *"
        assert validate_schedule_cron("*/5 * * * * *") == "*/5 * * * * *"
        assert validate_schedule_cron(_MISFIRE_SKIPPED_CRON) == _MISFIRE_SKIPPED_CRON

    def test_timezone_must_be_explicit_iana(self) -> None:
        for bad in (None, "", "Mars/Olympus", "Europe/Berln"):
            with pytest.raises(ScheduleError) as excinfo:
                validate_schedule_timezone(bad)
            assert excinfo.value.code is ScheduleErrorCode.INVALID_TIMEZONE
        assert validate_schedule_timezone("Europe/Berlin") == "Europe/Berlin"
        assert validate_schedule_timezone("UTC") == "UTC"


class TestPlannedFireIdentity:
    SCHEDULE = "7e6c1a34-9a2b-4c3d-8e5f-1029384756af"

    def test_identity_is_deterministic(self) -> None:
        moment = datetime(2026, 10, 15, 12, 30, 45, tzinfo=timezone.utc)
        assert planned_run_id(self.SCHEDULE, moment) == planned_run_id(
            self.SCHEDULE, moment
        )

    def test_identity_is_timezone_invariant(self) -> None:
        utc = datetime(2026, 10, 15, 12, 30, 45, tzinfo=timezone.utc)
        athens = datetime(2026, 10, 15, 15, 30, 45, tzinfo=ZoneInfo("Europe/Athens"))
        assert utc == athens  # the same instant, expressed in another zone
        assert planned_run_id(self.SCHEDULE, utc) == planned_run_id(
            self.SCHEDULE, athens
        )

    def test_identity_separates_fires_and_schedules(self) -> None:
        first = planned_run_id(
            self.SCHEDULE, datetime(2026, 10, 15, 12, 30, 45, tzinfo=timezone.utc)
        )
        next_second = planned_run_id(
            self.SCHEDULE, datetime(2026, 10, 15, 12, 30, 46, tzinfo=timezone.utc)
        )
        other_schedule = planned_run_id(
            "0f6e4d3c-2b1a-49f0-8e5f-6a7b8c9d0e1f",
            datetime(2026, 10, 15, 12, 30, 45, tzinfo=timezone.utc),
        )
        assert len({first, next_second, other_schedule}) == 3
        for value in (first, next_second, other_schedule):
            assert str(uuid.UUID(value)) == value  # canonical opaque IDs

    def test_identity_inputs_fail_value_free(self) -> None:
        moment = datetime(2026, 10, 15, 12, 30, 45, tzinfo=timezone.utc)
        for bad_schedule in (None, 42, "not-a-uuid", self.SCHEDULE.upper()):
            with pytest.raises(ScheduleError) as excinfo:
                planned_run_id(bad_schedule, moment)
            assert excinfo.value.code is ScheduleErrorCode.INVALID_SCHEDULE_ID
        for bad_time in (None, 42, "2026-10-15T12:30:45Z", moment.replace(tzinfo=None)):
            with pytest.raises(ScheduleError) as excinfo:
                planned_run_id(self.SCHEDULE, bad_time)
            assert excinfo.value.code is ScheduleErrorCode.INVALID_FIRE_TIME

    def test_identity_namespace_is_fixed(self) -> None:
        assert str(uuid.UUID(FIRE_RUN_NAMESPACE)) == FIRE_RUN_NAMESPACE


class TestValidationRunsBeforeStorage:
    """Creation rejects before touching any database (no fixture needed)."""

    class _ExplodingFactory:
        def __call__(self) -> None:
            raise AssertionError("validation must fail before any session is opened")

    def _manager(self) -> ScheduleManager:
        return ScheduleManager(
            "postgresql://localhost:5432/unused",  # type: ignore[arg-type]
            self._ExplodingFactory(),  # type: ignore[arg-type]
        )

    @pytest.mark.anyio
    async def test_create_rejects_before_storage(self) -> None:
        manager = self._manager()
        spec = {"handler_name": "echo", "payload": {}}
        with pytest.raises(ScheduleError) as excinfo:
            await manager.create(server_id=SERVER_ID, cron="not a cron", run_spec=spec)
        assert excinfo.value.code is ScheduleErrorCode.INVALID_CRON
        with pytest.raises(ScheduleError) as excinfo:
            await manager.create(
                server_id=SERVER_ID,
                cron="* * * * *",
                run_spec=spec,
                cron_timezone="Mars/Olympus",
            )
        assert excinfo.value.code is ScheduleErrorCode.INVALID_TIMEZONE
        with pytest.raises(ScheduleError) as excinfo:
            await manager.create(
                server_id="not-a-uuid", cron="* * * * *", run_spec=spec
            )
        assert excinfo.value.code is ScheduleErrorCode.INVALID_SERVER_ID
        with pytest.raises(ScheduleError) as excinfo:
            await manager.create(server_id=SERVER_ID, cron="* * * * *", run_spec={})
        assert excinfo.value.code is ScheduleErrorCode.INVALID_RUN_SPEC

    @pytest.mark.anyio
    async def test_dispatch_rejects_value_free(self) -> None:
        resources = DispatchResources(
            sessions=self._ExplodingFactory(),  # type: ignore[arg-type]
            client=EngineClient("postgresql://localhost:5432/unused"),
        )
        moment = datetime(2026, 10, 15, 12, 30, 45, tzinfo=timezone.utc)
        with pytest.raises(ScheduleError) as excinfo:
            await dispatch_fire(resources, "not-a-uuid", moment)
        assert excinfo.value.code is ScheduleErrorCode.INVALID_SCHEDULE_ID
        with pytest.raises(ScheduleError) as excinfo:
            await dispatch_fire(
                resources,
                SERVER_ID,
                moment.replace(tzinfo=None),  # type: ignore[arg-type]
            )
        assert excinfo.value.code is ScheduleErrorCode.INVALID_FIRE_TIME


# ---------------------------------------------------------------------------
# live half: real disposable Postgres, real engine trigger, REAL workers
# ---------------------------------------------------------------------------


def _alembic_config() -> Config:
    return Config(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS job_schedule_fires, job_schedules, "
            "job_events, job_steps, job_runs, approvals, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, alembic_version"
        )
        conn.commit()


@pytest.fixture(scope="session")
def schedules_url() -> str:
    """Disposable fixture database: Alembic chain + engine-owned schema.

    The DBOS ``dbos`` schema (and the registered application version the
    scheduler needs to fire workflows) is created by a full engine
    launch — the session bootstraps exactly the way production does.
    """
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the schedule claims need a real "
            "disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_app_tables(url)
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    from mai.jobs import engine

    engine.launch_worker(url)  # creates the engine-owned dbos schema
    engine.shutdown()
    return url


@pytest.fixture(scope="session")
def client(schedules_url: str) -> EngineClient:
    """The control-plane seam (what the API process uses)."""
    with EngineClient(schedules_url) as facade:
        yield facade


@pytest.fixture
async def factory(
    schedules_url: str,
) -> async_sessionmaker[AsyncSession]:
    eng, sessions = create_app_engine(schedules_url)
    yield sessions
    # Each test starts from an empty jobs domain (session-scoped fixture
    # database): delete children before parents, then the engine's
    # trigger rows so no leftover schedule can fire in a later test.
    async with sessions() as session:
        for statement in (
            "DELETE FROM job_schedule_fires",
            "DELETE FROM job_steps",
            "DELETE FROM job_events",
            "DELETE FROM job_schedules",
            "DELETE FROM job_runs",
            "DELETE FROM dbos.workflow_schedules",
        ):
            await session.execute(text(statement))
        await session.commit()
    await eng.dispose()


@pytest.fixture
async def manager(
    schedules_url: str, factory: async_sessionmaker[AsyncSession]
) -> ScheduleManager:
    with ScheduleManager(schedules_url, factory) as facade:
        yield facade


@pytest.fixture
def scenario() -> Path:
    """Fresh scratch namespace per live test; always cleaned up."""
    root = FIXTURE_ROOT / uuid.uuid4().hex
    (root / "handlers").mkdir(parents=True)
    (root / "handlers" / f"{HANDLER_MODULE_NAME}.py").write_text(HANDLER_MODULE_SOURCE)
    _journal(root).touch()
    yield root
    shutil.rmtree(root, ignore_errors=True)


# -- record + trigger discipline (no worker needed) --------------------------


class TestScheduleRecords:
    @pytest.mark.anyio
    async def test_created_record_and_engine_trigger_follow_frozen_policy(
        self,
        schedules_url: str,
        factory: async_sessionmaker[AsyncSession],
        manager: ScheduleManager,
    ) -> None:
        record = await manager.create(
            server_id=SERVER_ID,
            cron=_MISFIRE_SKIPPED_CRON,
            run_spec={"handler_name": "schedule.echo", "payload": {}},
            cron_timezone="Europe/Berlin",
        )
        assert str(uuid.UUID(record.schedule_id)) == record.schedule_id
        assert record.server_id == SERVER_ID
        assert record.cron == _MISFIRE_SKIPPED_CRON
        assert record.cron_timezone == "Europe/Berlin"  # explicit, preserved
        assert record.misfire_policy == "skip_missed"  # the frozen single value
        assert record.enabled is True
        assert record.run_spec == RunSpec(handler_name="schedule.echo", payload={})
        assert record.created_at.tzinfo is not None

        # The derived engine trigger carries the same frozen policy: the
        # schedule timezone, the Machina queue/application, and backfill
        # OFF (the skip-missed / no-catch-up policy, engine rule 6).
        (trigger,) = _pg_rows(
            schedules_url,
            "SELECT workflow_name, schedule, cron_timezone, automatic_backfill, "
            "queue_name, application_name, status FROM dbos.workflow_schedules "
            "WHERE schedule_name = %(name)s",
            {"name": record.schedule_id},
        )
        assert trigger[0] == SCHEDULE_FIRE_WORKFLOW_NAME
        assert trigger[1] == _MISFIRE_SKIPPED_CRON
        assert trigger[2] == "Europe/Berlin"
        assert trigger[3] is False  # never backfill: skip missed fires
        assert trigger[4] == "mai-jobs"  # the one Machina queue
        assert trigger[5] == "mai"  # the one Machina application
        assert trigger[6] == "ACTIVE"

    @pytest.mark.anyio
    async def test_get_and_list_filter_by_server(
        self,
        factory: async_sessionmaker[AsyncSession],
        manager: ScheduleManager,
    ) -> None:
        first = await manager.create(
            server_id=SERVER_ID,
            cron="* * * * *",
            run_spec={"handler_name": "echo", "payload": {}},
        )
        second = await manager.create(
            server_id=OTHER_SERVER_ID,
            cron="* * * * *",
            run_spec={"handler_name": "echo", "payload": {}},
        )
        assert (await manager.get(first.schedule_id)) == first
        assert await manager.get(str(uuid.uuid4())) is None
        mine = await manager.list(SERVER_ID)
        assert [r.schedule_id for r in mine] == [first.schedule_id]
        everyone = await manager.list()
        assert {r.schedule_id for r in everyone} == {
            first.schedule_id,
            second.schedule_id,
        }

    @pytest.mark.anyio
    async def test_set_enabled_toggles_record_and_trigger(
        self,
        schedules_url: str,
        factory: async_sessionmaker[AsyncSession],
        manager: ScheduleManager,
    ) -> None:
        record = await manager.create(
            server_id=SERVER_ID,
            cron="* * * * *",
            run_spec={"handler_name": "echo", "payload": {}},
        )
        paused = await manager.set_enabled(record.schedule_id, enabled=False)
        assert paused.enabled is False
        (status,) = _pg_rows(
            schedules_url,
            "SELECT status FROM dbos.workflow_schedules WHERE schedule_name = %(name)s",
            {"name": record.schedule_id},
        )
        assert status[0] == "PAUSED"
        resumed = await manager.set_enabled(record.schedule_id, enabled=True)
        assert resumed.enabled is True
        (status,) = _pg_rows(
            schedules_url,
            "SELECT status FROM dbos.workflow_schedules WHERE schedule_name = %(name)s",
            {"name": record.schedule_id},
        )
        assert status[0] == "ACTIVE"

    @pytest.mark.anyio
    async def test_unknown_schedule_ids_are_value_free(
        self,
        factory: async_sessionmaker[AsyncSession],
        manager: ScheduleManager,
    ) -> None:
        unknown = str(uuid.uuid4())
        with pytest.raises(ScheduleError) as excinfo:
            await manager.set_enabled(unknown, enabled=False)
        assert excinfo.value.code is ScheduleErrorCode.SCHEDULE_NOT_FOUND
        with pytest.raises(ScheduleError) as excinfo:
            await manager.delete(unknown)
        assert excinfo.value.code is ScheduleErrorCode.SCHEDULE_NOT_FOUND

    @pytest.mark.anyio
    async def test_rejected_create_leaves_nothing_behind(
        self,
        schedules_url: str,
        factory: async_sessionmaker[AsyncSession],
        manager: ScheduleManager,
    ) -> None:
        before = _pg_scalar(
            schedules_url,
            "SELECT count(*) FROM dbos.workflow_schedules",
            {},
        )
        with pytest.raises(ScheduleError) as excinfo:
            await manager.create(
                server_id=SERVER_ID,
                cron="definitely not a cron",
                run_spec={"handler_name": "echo", "payload": {}},
            )
        assert excinfo.value.code is ScheduleErrorCode.INVALID_CRON
        assert await manager.list() == ()  # no domain row survived
        after = _pg_scalar(
            schedules_url,
            "SELECT count(*) FROM dbos.workflow_schedules",
            {},
        )
        assert after == before  # no engine trigger survived

    @pytest.mark.anyio
    async def test_delete_removes_record_and_trigger(
        self,
        schedules_url: str,
        factory: async_sessionmaker[AsyncSession],
        manager: ScheduleManager,
    ) -> None:
        record = await manager.create(
            server_id=SERVER_ID,
            cron="* * * * *",
            run_spec={"handler_name": "echo", "payload": {}},
        )
        await manager.delete(record.schedule_id)
        assert await manager.get(record.schedule_id) is None
        assert (
            _pg_scalar(
                schedules_url,
                "SELECT count(*) FROM dbos.workflow_schedules WHERE schedule_name = %(name)s",
                {"name": record.schedule_id},
            )
            == 0
        )


# -- dispatch path: dedup ledger + deterministic submit (no worker) ----------


class TestDispatchFire:
    @pytest.mark.anyio
    async def test_fire_dispatches_once_and_replays_to_one_identity(
        self,
        schedules_url: str,
        factory: async_sessionmaker[AsyncSession],
        client: EngineClient,
        manager: ScheduleManager,
    ) -> None:
        record = await manager.create(
            server_id=SERVER_ID,
            cron="* * * * *",
            run_spec={
                "handler_name": "schedule.echo",
                "payload": {"journal": "/tmp/mai-fxt-05-schedules/unused"},
            },
        )
        fire_time = datetime(2026, 10, 15, 12, 30, 45, tzinfo=timezone.utc)
        resources = DispatchResources(sessions=factory, client=client)

        run_id = await dispatch_fire(resources, record.schedule_id, fire_time)
        assert run_id == planned_run_id(record.schedule_id, fire_time)
        # The submit intent is durable at the engine: queued, engine-owned.
        assert client.run_state(run_id) is RunState.QUEUED

        (fire_row,) = _pg_rows(
            schedules_url,
            "SELECT planned_fire_time, run_id FROM job_schedule_fires "
            "WHERE schedule_id = %(s)s",
            {"s": uuid.UUID(record.schedule_id)},
        )
        assert fire_row[0] == fire_time
        # The ledger's run link stays open (NULL) until the run-row owner
        # writes the job_runs row; the fire's run intent is identified at
        # the engine by the deterministic workflow UUID:
        assert fire_row[1] is None
        assert _engine_run_row(schedules_url, run_id, "ENQUEUED") == 1

        # A replay of the SAME planned fire converges on the identity and
        # never creates a second intent (ledger PK + engine reject policy).
        replayed = await dispatch_fire(resources, record.schedule_id, fire_time)
        assert replayed == run_id
        assert (
            _pg_scalar(
                schedules_url,
                "SELECT count(*) FROM job_schedule_fires WHERE schedule_id = %(s)s",
                {"s": uuid.UUID(record.schedule_id)},
            )
            == 1
        )

        # A different planned fire is a different intent.
        other = await dispatch_fire(
            resources, record.schedule_id, fire_time + timedelta(seconds=1)
        )
        assert other != run_id
        assert (
            _pg_scalar(
                schedules_url,
                "SELECT count(*) FROM job_schedule_fires WHERE schedule_id = %(s)s",
                {"s": uuid.UUID(record.schedule_id)},
            )
            == 2
        )

        # Cleanup: nothing left for a later worker to recover (guard 8:
        # cancellation is the explicit boundary; queued runs otherwise
        # stay queued forever).
        client.request_cancel(run_id)
        client.request_cancel(other)
        assert client.run_state(run_id) is RunState.CANCELLED

    @pytest.mark.anyio
    async def test_disabled_or_missing_schedule_skips_value_free(
        self,
        schedules_url: str,
        factory: async_sessionmaker[AsyncSession],
        client: EngineClient,
        manager: ScheduleManager,
    ) -> None:
        record = await manager.create(
            server_id=SERVER_ID,
            cron="* * * * *",
            run_spec={"handler_name": "schedule.echo", "payload": {}},
        )
        await manager.set_enabled(record.schedule_id, enabled=False)
        fire_time = datetime(2026, 10, 15, 12, 30, 45, tzinfo=timezone.utc)
        resources = DispatchResources(sessions=factory, client=client)

        # Negative control: the trigger may fire once more before the
        # engine notices the pause, but a disabled record dispatches
        # nothing and writes no ledger row.
        assert await dispatch_fire(resources, record.schedule_id, fire_time) is None
        assert (
            _pg_scalar(
                schedules_url,
                "SELECT count(*) FROM job_schedule_fires WHERE schedule_id = %(s)s",
                {"s": uuid.UUID(record.schedule_id)},
            )
            == 0
        )

        # A trigger without a live record (crash between engine
        # acceptance and domain commit) is equally inert.
        assert await dispatch_fire(resources, str(uuid.uuid4()), fire_time) is None


# -- the feature-gate scenario: real scheduler, duplicate pollers, restart ---


class TestScheduledFire:
    @pytest.mark.anyio
    async def test_real_scheduler_fires_once_with_duplicate_pollers(
        self,
        schedules_url: str,
        factory: async_sessionmaker[AsyncSession],
        client: EngineClient,
        manager: ScheduleManager,
        scenario: Path,
    ) -> None:
        # A near-term benign task: the fire lands 25–85s out, far enough
        # for two real workers to boot and both poll the schedule.
        first_second = (datetime.now(timezone.utc) + timedelta(seconds=25)).second
        seconds = {first_second, (first_second + 20) % 60}
        record = await manager.create(
            server_id=SERVER_ID,
            cron=_cron_for_seconds(seconds),
            run_spec={
                "handler_name": "schedule.echo",
                "payload": {
                    "journal": str(_journal(scenario)),
                    "marker": "scheduled-fire",
                },
            },
        )
        expected_fire = _next_fire(seconds, datetime.now(timezone.utc))

        # Duplicate pollers: two REAL worker processes, both running the
        # DBOS scheduler loop over the same trigger.
        worker_one = _spawn_worker(schedules_url, scenario / "handlers", scenario, "w1")
        worker_two = _spawn_worker(schedules_url, scenario / "handlers", scenario, "w2")
        try:
            _wait_until(
                lambda: _journal_count(scenario, "scheduled-fire") >= 1,
                FIRE_TIMEOUT,
                "the real scheduler to fire the near-term task",
            )
        finally:
            _stop(worker_one)
            _stop(worker_two)

        # One planned-fire intent, inspected at every layer:
        assert _journal_count(scenario, "scheduled-fire") == 1  # effect once
        (fire_row,) = _pg_rows(
            schedules_url,
            "SELECT planned_fire_time, run_id FROM job_schedule_fires "
            "WHERE schedule_id = %(s)s",
            {"s": uuid.UUID(record.schedule_id)},
        )
        assert fire_row[0] == expected_fire  # the planned fire time, verbatim
        # The ledger link stays open (NULL) until the run-row owner card
        # fills it; the planned-fire identity is the engine run workflow:
        assert fire_row[1] is None
        run_id = planned_run_id(record.schedule_id, expected_fire)
        assert _engine_run_row(schedules_url, run_id, "SUCCESS") == 1
        assert (
            _pg_scalar(
                schedules_url,
                "SELECT count(*) FROM job_schedule_fires WHERE schedule_id = %(s)s",
                {"s": uuid.UUID(record.schedule_id)},
            )
            == 1
        )  # no duplicate dispatch
        assert (
            _pg_scalar(
                schedules_url,
                "SELECT count(*) FROM dbos.workflow_status WHERE name = %(name)s "
                "AND workflow_uuid LIKE %(prefix)s AND status = 'SUCCESS'",
                {
                    "name": SCHEDULE_FIRE_WORKFLOW_NAME,
                    "prefix": f"sched-{record.schedule_id}-%",
                },
            )
            == 1
        )  # one fire workflow for the whole minute
        assert client.run_state(run_id) is RunState.SUCCEEDED

        # Pause (the enabled gate) so the next recurrence cannot fire.
        paused = await manager.set_enabled(record.schedule_id, enabled=False)
        assert paused.enabled is False


class TestMisfireSkipMissed:
    @pytest.mark.anyio
    async def test_fires_due_while_down_are_skipped_and_dispatch_resumes(
        self,
        schedules_url: str,
        factory: async_sessionmaker[AsyncSession],
        client: EngineClient,
        manager: ScheduleManager,
        scenario: Path,
    ) -> None:
        # Three fire opportunities 20s apart; the first two come due while
        # NO worker exists (the only scheduler is down).
        first_second = (datetime.now(timezone.utc) + timedelta(seconds=15)).second
        seconds = {
            first_second,
            (first_second + 20) % 60,
            (first_second + 40) % 60,
        }
        record = await manager.create(
            server_id=SERVER_ID,
            cron=_cron_for_seconds(seconds),
            run_spec={
                "handler_name": "schedule.echo",
                "payload": {
                    "journal": str(_journal(scenario)),
                    "marker": "resumed-fire",
                },
            },
        )
        missed_one = _next_fire(seconds, datetime.now(timezone.utc))
        missed_two = _next_fire(seconds, missed_one)
        resume_fire = _next_fire(seconds, missed_two)

        # The two misses actually pass while the engine is down.
        _wait_until(
            lambda: datetime.now(timezone.utc) >= missed_two + timedelta(seconds=2),
            FIRE_TIMEOUT,
            "the two missed fire times to pass with no worker running",
        )

        # Chosen misfire result: SKIPPED — no ledger row, no fire
        # workflow, no run for either missed fire (no catch-up).
        assert (await manager.get(record.schedule_id)) is not None
        assert (
            _pg_scalar(
                schedules_url,
                "SELECT count(*) FROM job_schedule_fires WHERE schedule_id = %(s)s",
                {"s": uuid.UUID(record.schedule_id)},
            )
            == 0
        )
        assert (
            _pg_scalar(
                schedules_url,
                "SELECT count(*) FROM dbos.workflow_status WHERE workflow_uuid LIKE %(prefix)s",
                {"prefix": f"sched-{record.schedule_id}-%"},
            )
            == 0
        )

        # Restart across the due time: a fresh worker resumes from now.
        worker = _spawn_worker(schedules_url, scenario / "handlers", scenario, "w1")
        try:
            _wait_until(
                lambda: _journal_count(scenario, "resumed-fire") >= 1,
                FIRE_TIMEOUT,
                "the post-restart fire to dispatch",
            )
        finally:
            _stop(worker)

        # Exactly ONE planned-fire intent — the post-restart fire, with
        # its deterministic identity; the misses stay skipped.
        assert _journal_count(scenario, "resumed-fire") == 1
        (fire_row,) = _pg_rows(
            schedules_url,
            "SELECT planned_fire_time, run_id FROM job_schedule_fires "
            "WHERE schedule_id = %(s)s",
            {"s": uuid.UUID(record.schedule_id)},
        )
        assert fire_row[0] == resume_fire
        # Ledger link open (NULL, run-row owner pending); the identity is
        # the engine run workflow:
        assert fire_row[1] is None
        run_id = planned_run_id(record.schedule_id, resume_fire)
        assert _engine_run_row(schedules_url, run_id, "SUCCESS") == 1
        assert (
            _pg_scalar(
                schedules_url,
                "SELECT count(*) FROM job_schedule_fires WHERE schedule_id = %(s)s",
                {"s": uuid.UUID(record.schedule_id)},
            )
            == 1
        )
        assert client.run_state(run_id) is RunState.SUCCEEDED
        assert (
            _pg_scalar(
                schedules_url,
                "SELECT count(*) FROM dbos.workflow_status WHERE name = %(name)s "
                "AND workflow_uuid LIKE %(prefix)s AND status = 'SUCCESS'",
                {
                    "name": SCHEDULE_FIRE_WORKFLOW_NAME,
                    "prefix": f"sched-{record.schedule_id}-%",
                },
            )
            == 1
        )


# -- import/entrypoint receipt -----------------------------------------------


def test_worker_visible_registration() -> None:
    """The fire workflow and step are registered at import (worker path).

    DBOS marks registered functions with the ``dbos_function_name``
    attribute; the worker imports this module via ``MAI_WORKER_HANDLERS``
    before launch, which is what makes the trigger executable.
    """
    from mai.jobs.schedules import dispatch_fire_step, schedule_fire_workflow

    assert getattr(schedule_fire_workflow, "dbos_function_name", None) == (
        SCHEDULE_FIRE_WORKFLOW_NAME
    )
    assert getattr(dispatch_fire_step, "dbos_function_name", None) == (
        DISPATCH_FIRE_STEP_NAME
    )
