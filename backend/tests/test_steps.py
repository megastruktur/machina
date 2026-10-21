"""Focused tests for durable capability steps (card 05-steps).

Acceptance (BRIEF):

1. The durable run owns step intent (``job_steps``), the common
   dispatcher (every effect is one gated ``CapabilityDispatcher.dispatch``
   call — no inline route execution), the resource lock
   (``ResourceLockService`` lease spanning the effect, released only on a
   proven terminal outcome) and the persisted safe receipt (value-free
   ``job_steps.receipt``).
2. Approval wait and uncertain results translate onto LEGAL run states
   through the reviewed lifecycle producer — ``waiting_approval`` and
   ``reconciling`` — and are never retried into success: the parked run
   resumes only on durable state (a bound pending decision, a
   reconciler-resolved step row).

Boundary discipline (test policy): the exactly-once, crash-window and
park/resume claims run against a real disposable PostgreSQL fixture
(``MAI_TEST_DATABASE_URL``) with the real Alembic chain, the real engine
adapter and REAL worker subprocesses (kill -9 mid-effect + fresh-worker
recovery). The dispatcher composition is the REAL one (real registry,
real policy evaluation, real approval store over the fixture database,
real lock service); the capability executor is a journaled fixture whose
effect markers make "the effect happened exactly once" observable — the
SSH transport lane itself is 04-shell-capability's proven surface and is
not re-proven here (recorded limitation). Fixtures carry only
tmp-namespace paths and marker words — never secret values.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from dbos import DBOSClient
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.capabilities.dispatch import (
    EFFECT_EXECUTED,
    EFFECT_NOT_ATTEMPTED,
    EFFECT_UNKNOWN,
    BoundedOutput,
    CapabilityDispatcher,
    DispatchOutcome,
    ExecutorResult,
    canonical_payload_hash,
)
from mai.contracts.jobs import (
    RunState,
    StepState,
    TERMINAL_STEP_STATES,
    parse_job_event,
)
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.jobs import engine, steps
from mai.jobs.engine import EngineClient
from mai.jobs.lifecycle import RunLifecycle
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry
from mai.policy.approvals import ApprovalRequest, ApprovalStore
from mai.policy.evaluate import ExecutionContext, Policy, PolicyMode

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# ONE persistent test-loop: same discipline the production executor applies
# (SQLAlchemy binds asyncio primitives to the first loop touching an engine;
# per-call asyncio.run loops are measured-broken).
_TEST_LOOP: asyncio.AbstractEventLoop | None = None
_TEST_LOOP_LOCK = __import__("threading").Lock()


def _run(coro: object) -> object:
    """Run one coroutine on the persistent test loop; block for the result."""
    global _TEST_LOOP
    with _TEST_LOOP_LOCK:
        if _TEST_LOOP is None:
            _TEST_LOOP = asyncio.new_event_loop()
            __import__("threading").Thread(
                target=_TEST_LOOP.run_forever, daemon=True
            ).start()
    assert _TEST_LOOP is not None
    return asyncio.run_coroutine_threadsafe(coro, _TEST_LOOP).result()


LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Scratch namespace per runtime-environment ADR D2/D4 (recorded + cleaned
# in the card evidence). Each live scenario gets a fresh subdirectory.
FIXTURE_ROOT = Path("/tmp/mai-fxt-05-steps")

READY_TIMEOUT = 90.0
RUN_TIMEOUT = 120.0

# Frozen fixture identities (canonical lowercase UUID strings, C01 rule 1).
SERVER_ID = "5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60"

READ_CAP = "fixture.read"
MUTATE_CAP = "fixture.mutate"

HANDLER_MODULE_NAME = "steps_scenario_handlers"

# ---------------------------------------------------------------------------
# the worker-side composition + journaled fixture executor
# ---------------------------------------------------------------------------

HANDLER_MODULE_SOURCE = '''
"""Worker-side composition for 05-steps: real dispatcher, journaled effects."""

import json
import os
import time
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import mai.jobs.steps as steps
from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
from mai.credentials.env import EnvResolver
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import Policy


def _descriptor(capability_id: str, effect_class: str):
    return parse_descriptor(
        {
            "identifier": capability_id,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["marker"],
                "properties": {"marker": {"type": "string", "minLength": 1}},
            },
            "output_schema": {
                "type": "object",
                "required": ["status", "exit_code", "stdout", "stderr"],
                "properties": {
                    "status": {"type": "string"},
                    "exit_code": {"type": ["integer", "null"]},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                },
            },
            "execution_environments": ["linux_ssh"],
            "effect_class": effect_class,
            "resource_scope": ["target.exec"],
            "required_grants": [capability_id],
        },
        source=f"<{capability_id} descriptor>",
    )


class FixtureExecutor:
    """Journaled capability executor: effect markers make exactly-once
    observable. Behaviors (``MAI_FX_BEHAVIOR`` JSON, keyed by
    ``capability:marker`` then ``capability``):

    - ``ok`` (default): effect marker + terminal ``succeeded`` document;
    - ``gate``: ``begin`` marker, then wait for the per-operation gate
      file (``<gate dir>/gate-<capability>-<marker>``) before the effect
      (a kill -9 in that window is the crash-window scenario);
    - ``raise_after_effect``: the effect happens, THEN the executor
      raises — the dispatcher must report an unproven effect;
    - ``unknown_status``: terminal-shaped document whose status is
      ``outcome_unknown`` (a host receipt that never went terminal).
    """

    def __init__(self) -> None:
        self.journal = os.environ["MAI_FX_JOURNAL"]
        self.gate_dir = os.environ["MAI_FX_GATE_DIR"]
        self.behaviors = json.loads(os.environ.get("MAI_FX_BEHAVIOR", "{}"))

    def _mark(self, marker: str) -> None:
        with open(self.journal, "a", encoding="utf-8") as handle:
            handle.write(marker + "\\n")

    def _behavior(self, capability_id: str, marker: str) -> str:
        keyed = self.behaviors.get(f"{capability_id}:{marker}")
        if keyed is not None:
            return keyed
        return self.behaviors.get(capability_id, "ok")

    async def __call__(self, call):
        capability_id = call.descriptor.identifier
        marker = call.payload.get("marker", "?")
        behavior = self._behavior(capability_id, str(marker))
        self._mark(f"begin:{capability_id}:{marker}")
        if behavior == "gate":
            gate = os.path.join(self.gate_dir, f"gate-{capability_id}-{marker}")
            deadline = time.time() + 300
            while not os.path.exists(gate):
                if time.time() > deadline:
                    raise RuntimeError("gate timeout")
                time.sleep(0.1)
        self._mark(f"effect:{capability_id}:{marker}")
        if behavior == "raise_after_effect":
            raise RuntimeError("executor boom after effect")
        status = (
            "outcome_unknown" if behavior == "unknown_status" else "succeeded"
        )
        document = {
            "status": status,
            "exit_code": 0 if status == "succeeded" else None,
            "stdout": "",
            "stderr": "",
            "receipt_dir": "/tmp/mai-fxt-05-steps/receipts/example-op",
            "intent_path": "/tmp/mai-fxt-05-steps/intents/example-op.json",
        }
        return ExecutorResult(
            ok=status == "succeeded", output=json.dumps(document, sort_keys=True)
        )


def _build() -> None:
    url = os.environ["MAI_DATABASE_URL"]
    app_engine = create_async_engine(url, poolclass=NullPool)
    sessions = async_sessionmaker(app_engine, expire_on_commit=False)
    with open(os.environ["MAI_FX_GRANTS"], encoding="utf-8") as handle:
        grants_doc = json.load(handle)
    with open(os.environ["MAI_FX_POLICY"], encoding="utf-8") as handle:
        policy_doc = json.load(handle)
    grants = {
        entry["capability_id"]: frozenset(
            uuid.UUID(value) for value in entry["server_ids"]
        )
        for entry in grants_doc
    }
    registry = CapabilityRegistry()
    registry.register(_descriptor("fixture.read", "read"))
    registry.register(_descriptor("fixture.mutate", "mutate"))
    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(sessions),
        grants=lambda: grants,
        policy=lambda: Policy(
            revision=int(policy_doc["revision"]),
            denied_capabilities=frozenset(policy_doc.get("denied", [])),
        ),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(
            os.environ.get("MAI_FX_ENV", "/tmp/mai-fxt-05-steps/absent.env")
        ),
        executor=FixtureExecutor(),
    )
    locks = ResourceLockService(url)
    steps.configure_steps(
        steps.StepExecutor(sessions=sessions, dispatcher=dispatcher, locks=locks)
    )


_build()
'''

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _canonical(document: object) -> str:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _context_doc(run_id: str, *, mode: str, allowed: tuple[str, ...]) -> dict:
    context = ExecutionContext(
        principal="hash:fixture-principal",
        server_id=uuid.UUID(SERVER_ID),
        run_id=uuid.UUID(run_id),
        parent_scope=None,
        profile_revision="profile-1",
        prompt_revision="prompt-1",
        credential_references=(),
        allowed_capabilities=allowed,
        mode=PolicyMode(mode),
    )
    return context.to_json()


def _intent_doc(
    run_id: str,
    specs: list[tuple[str, str | None]],
    *,
    mode: str = "delegated",
    approval_wait_seconds: float | None = None,
    wait_poll_seconds: float | None = None,
) -> dict:
    """One run-intent document: ``(capability_id, resource)`` per step."""
    allowed = tuple(dict.fromkeys(cap for cap, _ in specs))
    document: dict[str, object] = {
        "v": steps.RUN_PAYLOAD_VERSION,
        "run_id": run_id,
        "server_id": SERVER_ID,
        "context": _context_doc(run_id, mode=mode, allowed=allowed),
        "steps": [
            {
                "seq": index + 1,
                "capability_id": capability,
                "payload": {"marker": f"m{index + 1}"},
                **({"resource": resource} if resource else {}),
            }
            for index, (capability, resource) in enumerate(specs)
        ],
    }
    if approval_wait_seconds is not None:
        document["approval_wait_seconds"] = approval_wait_seconds
    if wait_poll_seconds is not None:
        document["wait_poll_seconds"] = wait_poll_seconds
    return document


def _dispatch_outcome(**overrides: object) -> DispatchOutcome:
    defaults: dict[str, object] = {
        "op_id": uuid.uuid4(),
        "ts": datetime.now(timezone.utc),
        "capability_id": READ_CAP,
        "server_id": uuid.UUID(SERVER_ID),
        "run_id": uuid.uuid4(),
        "principal": "hash:fixture-principal",
        "mode": "delegated",
        "allowed": True,
        "reason": "allowed",
        "policy_revision": 1,
        "effect_class": "read",
        "payload_hash": canonical_payload_hash({"marker": "m1"}),
        "effect": EFFECT_EXECUTED,
        "approval_id": None,
        "approval_spent": False,
        "approval_code": None,
        "executor_ok": True,
        "output": BoundedOutput(
            text=json.dumps(
                {
                    "status": "succeeded",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "receipt_dir": "/tmp/receipts/op",
                    "intent_path": "/tmp/intents/op.json",
                }
            ),
            truncated=False,
            omitted_bytes=0,
        ),
        "error_type": None,
        "credential_references": (),
    }
    defaults.update(overrides)
    return DispatchOutcome(**defaults)  # type: ignore[arg-type]


def _journal_path(scenario: Path) -> Path:
    return scenario / "journal.log"


def _journal_count(scenario: Path, marker: str) -> int:
    try:
        lines = _journal_path(scenario).read_text().splitlines()
    except FileNotFoundError:
        return 0
    return sum(1 for line in lines if line == marker)


def _journal_lines(scenario: Path) -> list[str]:
    try:
        return _journal_path(scenario).read_text().splitlines()
    except FileNotFoundError:
        return []


def _wait_until(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _spawn_worker(
    url: str, handler_dir: Path, scenario: Path, tag: str
) -> subprocess.Popen:
    """Launch a REAL worker process against the fixture database."""
    env = dict(os.environ)
    env.pop(LIVE_URL_VAR, None)  # workers speak the production env contract
    env["MAI_DATABASE_URL"] = url
    env["MAI_WORKER_HANDLERS"] = HANDLER_MODULE_NAME
    env["MAI_FX_JOURNAL"] = str(_journal_path(scenario))
    env["MAI_FX_GATE_DIR"] = str(scenario / "gates")
    env["MAI_FX_GRANTS"] = str(scenario / "grants.json")
    env["MAI_FX_POLICY"] = str(scenario / "policy.json")
    behavior_path = scenario / "behavior.json"
    if behavior_path.exists():
        env["MAI_FX_BEHAVIOR"] = behavior_path.read_text(encoding="utf-8")
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


def _kill9(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)


def _stop(proc: subprocess.Popen) -> int:
    """SIGTERM, then SIGKILL if the engine's graceful shutdown stalls."""
    proc.send_signal(signal.SIGTERM)
    try:
        return proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=30)


def _alembic_config() -> Config:
    return Config(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state.

    Domain tables AND the engine-owned ``dbos`` schema: an interrupted
    earlier attempt can leave PARKED workflows (recv-looping) or ENQUEUED
    runs that would otherwise occupy the fresh worker's queue slots. The
    engine recreates its schema at launch, exactly as in production
    bootstrap.
    """
    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS job_resource_locks, job_schedule_fires, "
            "job_schedules, job_events, job_steps, job_runs, approvals, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, alembic_version"
        )
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


def _gate_file(scenario: Path, capability_id: str, marker: str) -> Path:
    return scenario / "gates" / f"gate-{capability_id}-{marker}"


def _write_policy_files(
    scenario: Path, *, grants: list[dict], denied: list[str] | None = None
) -> None:
    document = [
        {"capability_id": capability, "server_ids": [SERVER_ID] if present else []}
        for entry in grants
        for capability, present in entry.items()
    ]
    (scenario / "grants.json").write_text(json.dumps(document), encoding="utf-8")
    (scenario / "policy.json").write_text(
        json.dumps({"revision": 1, "denied": denied or []}), encoding="utf-8"
    )


def _set_behavior(scenario: Path, behavior: dict[str, str]) -> None:
    (scenario / "behavior.json").write_text(json.dumps(behavior), encoding="utf-8")


def _read_step_rows(url: str, run_id: str) -> list[dict]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT step_id, seq, capability_id, state, receipt FROM job_steps "
            "WHERE run_id = %s ORDER BY seq",
            (uuid.UUID(run_id),),
        ).fetchall()


def _read_step_row(url: str, run_id: str, seq: int) -> dict | None:
    for row in _read_step_rows(url, run_id):
        if row["seq"] == seq:
            return row
    return None


def _read_events(url: str, run_id: str) -> list:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT seq, ts, type, v, data FROM job_events "
            "WHERE run_id = %s ORDER BY seq",
            (uuid.UUID(run_id),),
        ).fetchall()
    return [
        parse_job_event(
            {
                "seq": row["seq"],
                "run_id": run_id,
                "ts": row["ts"].isoformat(),
                "type": row["type"],
                "v": row["v"],
                "data": row["data"],
            }
        )
        for row in rows
    ]


async def _run_states(factory: async_sessionmaker, run_id: str) -> tuple:
    return await RunLifecycle(factory).replay_states(run_id)


async def _current_state(factory: async_sessionmaker, run_id: str) -> str | None:
    state = await RunLifecycle(factory).current_state(run_id)
    return None if state is None else state.value


async def _enqueue_run(
    client: EngineClient,
    insert_run: Callable[[str], object],
    run_id: str,
    document: dict,
) -> None:
    await insert_run(run_id)
    client.enqueue_run(run_id, steps.HANDLER_NAME, _canonical(document))


def _wake(run_id: str, url: str) -> None:
    """Control-plane park wake: any message triggers a durable re-check."""
    client = DBOSClient(
        system_database_url=url,
        application_name=engine.APPLICATION_NAME,
        lazy=True,
    )
    try:
        client.send(run_id, {"kind": "resume"}, topic=steps.RESUME_TOPIC)
    finally:
        client.destroy()


def _resolve_step(url: str, run_id: str, seq: int, state: str) -> None:
    """Reconciliation-owner stand-in: resolve one unknown step terminally.

    The REAL resolution path is owned by 04-reconcile/05-cancel-recovery
    (not integrated at this card's base); this helper writes ONLY what
    that owner may write — a guarded terminal step transition with the
    verdict recorded on the receipt — so the runner's resume semantics
    are exercised against the real rows.
    """
    with psycopg.connect(url) as conn:
        updated = conn.execute(
            "UPDATE job_steps SET state = %s, "
            'receipt = receipt || \'{"resolution": "terminal"}\' '
            "WHERE run_id = %s AND seq = %s AND state = 'outcome_unknown'",
            (state, uuid.UUID(run_id), seq),
        ).rowcount
        conn.commit()
    assert updated == 1


def _terminal_engine_state(client: EngineClient, run_id: str) -> str | None:
    for _ in range(2):
        state = client.run_state(run_id)
        if state is None:
            return None
        return state.value
    return None


# ---------------------------------------------------------------------------
# live fixtures: real disposable Postgres + the real engine shape
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def steps_url() -> str:
    """Disposable fixture database: Alembic chain + engine-owned schema.

    The DBOS ``dbos`` schema is created by a full engine launch (never by
    the client and never by Alembic), so the session bootstraps exactly
    the way production does.
    """
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the durable step claims need a real "
            "disposable Postgres fixture"
        )
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
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
    engine.launch_worker(url)  # creates the engine-owned dbos schema
    engine.shutdown()
    return url


@pytest.fixture
def factory(steps_url: str) -> async_sessionmaker[AsyncSession]:
    """NullPool session factory for in-process facade assertions.

    Deliberately a SYNC fixture: every consumer drives it through its own
    ``asyncio.run`` call (no event loop is shared with the fixture), the
    same discipline the step executor itself applies.
    """
    eng, sessions = create_app_engine(steps_url)

    async def _clean() -> None:
        # Each data test starts from an empty jobs domain (session-scoped
        # fixture database): children before parents; the lock table too.
        # The lock table exists only after a first acquire (lazy creation),
        # so absent tables are skipped.
        async with sessions() as session:
            for table in (
                "job_resource_locks",
                "job_schedule_fires",
                "job_steps",
                "job_events",
                "job_schedules",
                "job_runs",
                "approvals",
            ):
                present = await session.execute(
                    text("SELECT to_regclass(:t) IS NOT NULL"),
                    {"t": f"public.{table}"},
                )
                if not present.scalar():
                    continue
                await session.execute(text(f"DELETE FROM {table}"))
            await session.commit()
        await eng.dispose()

    yield sessions
    _run(_clean())


@pytest.fixture
def insert_run(factory: async_sessionmaker[AsyncSession]):
    """Insert a ``job_runs`` row (the submit owner's row, by SQL helper)."""

    async def _insert(run_id: str) -> None:
        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO job_runs
                        (run_id, server_id, depth, profile_revision,
                         prompt_revision, model_profile, requested_grants,
                         submitted_at, updated_at)
                    VALUES
                        (:run_id, :server_id, 0, 3, 7, 'build-agent',
                         '["fixture.read"]', now(), now())
                    """
                ),
                {"run_id": uuid.UUID(run_id), "server_id": uuid.UUID(SERVER_ID)},
            )
            await session.commit()

    return _insert


@pytest.fixture
def scenario(tmp_path: Path) -> Path:
    """Fresh scenario namespace: handler module + journal + policy files."""
    root = tmp_path / "scenario"
    (root / "handlers").mkdir(parents=True)
    (root / "gates").mkdir(parents=True)
    (root / "handlers" / f"{HANDLER_MODULE_NAME}.py").write_text(
        HANDLER_MODULE_SOURCE, encoding="utf-8"
    )
    _journal_path(root).touch()
    _write_policy_files(root, grants=[{READ_CAP: True, MUTATE_CAP: True}])
    return root


# ---------------------------------------------------------------------------
# unit half: the run-intent contract
# ---------------------------------------------------------------------------


class TestParseIntent:
    def valid_document(self) -> dict:
        return _intent_doc(
            str(uuid.uuid4()), [(READ_CAP, None), (MUTATE_CAP, "fx-res")]
        )

    def test_valid_round_trip(self) -> None:
        document = self.valid_document()
        intent = steps.parse_run_intent(_canonical(document))
        assert intent.run_id == document["run_id"]
        assert intent.server_id == SERVER_ID
        assert [step.seq for step in intent.steps] == [1, 2]
        assert intent.steps[1].resource == "fx-res"
        assert intent.steps[0].resource is None
        assert intent.context.mode is PolicyMode.DELEGATED
        assert intent.approval_wait_seconds == steps.DEFAULT_APPROVAL_WAIT_SECONDS
        assert intent.reconcile_wait_seconds == steps.DEFAULT_RECONCILE_WAIT_SECONDS
        assert intent.wait_poll_seconds == steps.DEFAULT_WAIT_POLL_SECONDS

    def test_step_json_round_trip(self) -> None:
        document = self.valid_document()
        intent = steps.parse_run_intent(_canonical(document))
        assert json.loads(_canonical(intent.steps[1].to_json())) == {
            "seq": 2,
            "capability_id": MUTATE_CAP,
            "payload": {"marker": "m2"},
            "resource": "fx-res",
        }

    @pytest.mark.parametrize(
        ("mutate", "detail"),
        [
            (lambda doc: doc.update(extra=1), "unknown"),
            (lambda doc: doc.pop("v"), "missing"),
            (lambda doc: doc.update(v=2), "v must be"),
            (lambda doc: doc.update(run_id="not-a-uuid"), "run_id"),
            (lambda doc: doc.update(run_id=str(uuid.uuid4())), "context identity"),
            (
                lambda doc: doc["context"].update(server_id=str(uuid.uuid4())),
                "context identity",
            ),
            (lambda doc: doc.update(steps=[]), "non-empty"),
            (lambda doc: doc["steps"][1].update(seq=3), "ascend strictly"),
            (
                lambda doc: doc["steps"].append(dict(doc["steps"][0])),
                "ascend strictly",
            ),
            (lambda doc: doc["steps"][0].update(payload=[1, 2]), "payload"),
            (
                lambda doc: doc["steps"][0].update(capability_id="BAD CAP"),
                "capability_id",
            ),
            (
                lambda doc: doc["steps"][0].update(resource="bad\x1fresource"),
                "control characters",
            ),
            (
                lambda doc: doc["steps"][0].update(resource="x" * 500),
                "length bound",
            ),
            (lambda doc: doc.update(wait_poll_seconds=0), "bounded range"),
            (
                lambda doc: doc.update(approval_wait_seconds=400 * 24 * 3600),
                "bounded range",
            ),
            (
                lambda doc: doc.update(wait_poll_seconds="soon"),
                "number of seconds",
            ),
        ],
    )
    def test_rejections(self, mutate, detail: str) -> None:
        document = self.valid_document()
        mutate(document)
        with pytest.raises(steps.StepsError) as excinfo:
            steps.parse_run_intent(_canonical(document))
        assert detail in str(excinfo.value)
        assert excinfo.value.code is steps.StepsErrorCode.INVALID_PAYLOAD

    def test_oversized_step_payload_rejected(self) -> None:
        document = self.valid_document()
        document["steps"][0]["payload"] = {
            "marker": "x" * (steps.MAX_STEP_PAYLOAD_CHARS + 1)
        }
        with pytest.raises(steps.StepsError) as excinfo:
            steps.parse_run_intent(_canonical(document))
        assert "canonical bytes" in str(excinfo.value)

    def test_not_json_is_rejected_value_free(self) -> None:
        with pytest.raises(steps.StepsError) as excinfo:
            steps.parse_run_intent("{not json SECRET-MARKER")
        assert excinfo.value.code is steps.StepsErrorCode.INVALID_PAYLOAD
        assert "SECRET-MARKER" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# unit half: the frozen step state machine
# ---------------------------------------------------------------------------


class TestStepTransitions:
    LEGAL_EDGES = [
        (None, StepState.PENDING),
        (StepState.PENDING, StepState.RUNNING),
        (StepState.PENDING, StepState.FAILED),
        (StepState.RUNNING, StepState.PENDING),  # orderly no-effect revert
        (StepState.RUNNING, StepState.SUCCEEDED),
        (StepState.RUNNING, StepState.FAILED),
        (StepState.RUNNING, StepState.OUTCOME_UNKNOWN),
        (StepState.RUNNING, StepState.CANCELLED),
        (StepState.OUTCOME_UNKNOWN, StepState.SUCCEEDED),
        (StepState.OUTCOME_UNKNOWN, StepState.FAILED),
        (StepState.OUTCOME_UNKNOWN, StepState.CANCELLED),
    ]

    def test_diagram_edges_are_legal(self) -> None:
        for current, target in self.LEGAL_EDGES:
            assert steps.is_legal_step_transition(current, target) is True
            steps.validate_step_transition(current, target)  # must not raise

    def test_every_other_pair_is_rejected(self) -> None:
        legal = set(self.LEGAL_EDGES)
        for current in [None, *StepState]:
            for target in StepState:
                if (current, target) in legal:
                    continue
                assert steps.is_legal_step_transition(current, target) is False
                with pytest.raises(steps.StepsError) as excinfo:
                    steps.validate_step_transition(current, target)
                assert (
                    excinfo.value.code is steps.StepsErrorCode.INVALID_STEP_TRANSITION
                )

    def test_terminal_states_have_no_out_edges(self) -> None:
        for terminal in TERMINAL_STEP_STATES:
            assert steps.LEGAL_STEP_TRANSITIONS[terminal] == frozenset()

    def test_cancel_requested_has_no_edges(self) -> None:
        """Recording it is the cancellation owner's surface (guard rule 3)."""
        assert steps.LEGAL_STEP_TRANSITIONS[StepState.CANCEL_REQUESTED] == frozenset()
        for current in [None, *StepState]:
            assert (
                StepState.CANCEL_REQUESTED not in steps.LEGAL_STEP_TRANSITIONS[current]
            )

    def test_machine_is_total_over_the_frozen_vocabulary(self) -> None:
        assert set(steps.LEGAL_STEP_TRANSITIONS) == {None, *StepState}

    def test_rejection_is_value_free(self) -> None:
        with pytest.raises(steps.StepsError) as excinfo:
            steps.validate_step_transition(StepState.SUCCEEDED, StepState.RUNNING)
        assert str(excinfo.value).startswith("invalid_step_transition: ")
        with pytest.raises(steps.StepsError) as excinfo:
            steps.validate_step_transition(None, "running")  # type: ignore[arg-type]
        assert excinfo.value.code is steps.StepsErrorCode.INVALID_STEP_TRANSITION


# ---------------------------------------------------------------------------
# unit half: outcome translation (pure)
# ---------------------------------------------------------------------------


class TestOutcomeTranslation:
    def _document(self, status: str | None = "succeeded") -> BoundedOutput:
        body: dict[str, object] = {
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "receipt_dir": "/tmp/receipts/op",
            "intent_path": "/tmp/intents/op.json",
        }
        if status is not None:
            body["status"] = status
        return BoundedOutput(text=json.dumps(body), truncated=False, omitted_bytes=0)

    def test_executed_success_resolves(self) -> None:
        state, reason, receipt = steps._translate_outcome(
            _dispatch_outcome(output=self._document("succeeded"))
        )
        assert state == StepState.SUCCEEDED.value
        assert reason is None
        assert receipt["status"] == "succeeded"
        assert receipt["exit_code"] == 0
        assert receipt["receipt_dir"] == "/tmp/receipts/op"

    def test_executed_failure_resolves(self) -> None:
        state, _, _ = steps._translate_outcome(
            _dispatch_outcome(executor_ok=False, output=self._document("failed"))
        )
        assert state == StepState.FAILED.value

    def test_host_verified_cancel_resolves(self) -> None:
        state, reason, _ = steps._translate_outcome(
            _dispatch_outcome(output=self._document("cancelled"))
        )
        assert state == StepState.CANCELLED.value
        assert reason is None

    @pytest.mark.parametrize(
        "status", ["running", "outcome_unknown", "cancel_requested"]
    )
    def test_non_terminal_host_status_is_uncertain(self, status: str) -> None:
        state, reason, receipt = steps._translate_outcome(
            _dispatch_outcome(output=self._document(status))
        )
        assert state == StepState.OUTCOME_UNKNOWN.value
        assert reason == steps.STATUS_UNRESOLVED_REASON
        assert receipt["status"] == status

    def test_executor_crash_is_uncertain_never_an_effect_claim(self) -> None:
        state, reason, receipt = steps._translate_outcome(
            _dispatch_outcome(
                allowed=False,
                reason="executor_failed",
                effect=EFFECT_UNKNOWN,
                executor_ok=None,
                output=None,
                error_type="RuntimeError",
            )
        )
        assert state == StepState.OUTCOME_UNKNOWN.value
        assert reason == "executor_failed"
        assert receipt["effect"] == EFFECT_UNKNOWN
        assert receipt["error_type"] == "RuntimeError"

    def test_truncated_document_is_uncertain(self) -> None:
        text = json.dumps({"status": "succeeded", "exit_code": 0, "stdout": "x" * 500})
        state, reason, receipt = steps._translate_outcome(
            _dispatch_outcome(
                output=BoundedOutput(text=text[:40], truncated=True, omitted_bytes=99)
            )
        )
        assert state == StepState.OUTCOME_UNKNOWN.value
        assert reason == steps.STATUS_UNRESOLVED_REASON
        assert receipt["output_truncated"] is True
        assert receipt["omitted_bytes"] == 99

    def test_unknown_status_word_is_uncertain(self) -> None:
        state, _, _ = steps._translate_outcome(
            _dispatch_outcome(output=self._document("mysterious"))
        )
        assert state == StepState.OUTCOME_UNKNOWN.value

    def test_approval_required_is_the_wait_sentinel(self) -> None:
        state, reason, _ = steps._translate_outcome(
            _dispatch_outcome(
                allowed=False,
                reason="approval_required_but_absent",
                effect=EFFECT_NOT_ATTEMPTED,
                executor_ok=None,
            )
        )
        assert state == steps.WAIT_FOR_APPROVAL
        assert reason is None

    def test_policy_denial_is_failed_not_attempted(self) -> None:
        state, reason, receipt = steps._translate_outcome(
            _dispatch_outcome(
                allowed=False,
                reason="capability_not_granted",
                effect=EFFECT_NOT_ATTEMPTED,
                executor_ok=None,
                output=None,
            )
        )
        assert state == StepState.FAILED.value
        assert reason == "capability_not_granted"
        assert receipt["effect"] == EFFECT_NOT_ATTEMPTED

    def test_approval_rejected_is_failed(self) -> None:
        state, reason, receipt = steps._translate_outcome(
            _dispatch_outcome(
                allowed=False,
                reason="approval_rejected",
                effect=EFFECT_NOT_ATTEMPTED,
                executor_ok=None,
                approval_spent=True,
                approval_code="payload_mismatch",
                output=None,
            )
        )
        assert state == StepState.FAILED.value
        assert reason == "approval_rejected"
        assert receipt["approval_spent"] is True
        assert receipt["approval_code"] == "payload_mismatch"

    def test_receipt_is_value_free_by_construction(self) -> None:
        """No command text, no output text, no credential material."""
        _, _, receipt = steps._translate_outcome(
            _dispatch_outcome(output=self._document("succeeded"))
        )
        assert "marker" not in receipt
        for forbidden in ("command", "stdout", "stderr", "credential"):
            assert forbidden not in receipt
        assert isinstance(receipt["payload_hash"], str)
        assert len(receipt["payload_hash"]) == 64
        assert receipt["op_id"]

    def test_resume_from_receipt_round_trip(self) -> None:
        _, _, receipt = steps._translate_outcome(
            _dispatch_outcome(output=self._document("succeeded"))
        )
        outcome = steps._outcome_from_receipt("succeeded", receipt)
        assert outcome["state"] == "succeeded"
        assert outcome["op_id"] == receipt["op_id"]
        assert outcome["receipt"] == receipt

    def test_non_command_capability_statuses_translate_cleanly(self) -> None:
        """docs.retrieve, service.http, and chat.read_session outputs resolve
        without falling into status_unresolved / outcome_unknown."""
        state_ok, reason_ok, _ = steps._translate_outcome(
            _dispatch_outcome(
                capability_id="docs.retrieve",
                executor_ok=True,
                output=BoundedOutput(
                    text=json.dumps({"status": "ok", "results": []}),
                    truncated=False,
                    omitted_bytes=0,
                ),
            )
        )
        assert state_ok == StepState.SUCCEEDED.value
        assert reason_ok is None

        state_unavail, reason_unavail, receipt_unavail = steps._translate_outcome(
            _dispatch_outcome(
                capability_id="docs.retrieve",
                executor_ok=False,
                output=BoundedOutput(
                    text=json.dumps({"status": "unavailable", "code": "index_missing"}),
                    truncated=False,
                    omitted_bytes=0,
                ),
            )
        )
        assert state_unavail == StepState.FAILED.value
        assert reason_unavail == "index_missing"
        assert receipt_unavail["reason"] == "index_missing"

        state_chat, reason_chat, _ = steps._translate_outcome(
            _dispatch_outcome(
                capability_id="chat.read_session",
                executor_ok=True,
                output=BoundedOutput(
                    text=json.dumps({"session_id": str(uuid.uuid4()), "event_count": 3}),
                    truncated=False,
                    omitted_bytes=0,
                ),
            )
        )
        assert state_chat == StepState.SUCCEEDED.value
        assert reason_chat is None


# ---------------------------------------------------------------------------
# unit half: composition wiring
# ---------------------------------------------------------------------------


def _wired_dispatcher(factory: async_sessionmaker) -> CapabilityDispatcher:
    """A minimal VALID dispatcher composition (executor never reached)."""

    async def _never(call: object) -> ExecutorResult:  # pragma: no cover
        raise AssertionError("the wiring-test executor must never be reached")

    return CapabilityDispatcher(
        registry=CapabilityRegistry(),
        approvals=ApprovalStore(factory),
        grants=lambda: {},
        policy=lambda: Policy(revision=1),
        adopted=lambda server_id: False,
        credentials=lambda: EnvResolver("/tmp/mai-fxt-05-steps/absent.env"),
        executor=_never,
    )


class TestWiring:
    def test_missing_dispatcher_rejected(self, factory) -> None:
        with pytest.raises(steps.StepsError) as excinfo:
            steps.StepExecutor(
                sessions=factory,
                dispatcher=None,  # type: ignore[arg-type]
                locks=ResourceLockService("postgresql://placeholder"),
            )
        assert excinfo.value.code is steps.StepsErrorCode.INVALID_WIRING

    def test_missing_locks_rejected(self, factory) -> None:
        with pytest.raises(steps.StepsError) as excinfo:
            steps.StepExecutor(
                sessions=factory,
                dispatcher=_wired_dispatcher(factory),
                locks=None,  # type: ignore[arg-type]
            )
        assert excinfo.value.code is steps.StepsErrorCode.INVALID_WIRING

    def test_unconfigured_process_fails_value_free(self) -> None:
        steps.reset_steps()
        with pytest.raises(steps.StepsError) as excinfo:
            steps._resolve_steps()
        assert excinfo.value.code is steps.StepsErrorCode.EXECUTOR_NOT_CONFIGURED


# ---------------------------------------------------------------------------
# live half: durable step execution (real engine, real database, real workers)
# ---------------------------------------------------------------------------


class TestDurableSteps:
    def test_ordered_steps_execute_once_with_receipts_and_lock(
        self, steps_url, factory, insert_run, scenario
    ) -> None:
        run_id = str(uuid.uuid4())
        document = _intent_doc(
            run_id,
            [(READ_CAP, None), (READ_CAP, "fx-res")],
            wait_poll_seconds=1.0,
        )
        client = EngineClient(steps_url)
        worker = None
        engine_final = None
        try:
            worker = _spawn_worker(steps_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "run success",
            )
            engine_final = _terminal_engine_state(client, run_id)
        finally:
            if worker is not None:
                _stop(worker)
            client.close()
        assert engine_final == "succeeded"

        # Exactly-once ordered effects.
        assert _journal_lines(scenario) == [
            "begin:fixture.read:m1",
            "effect:fixture.read:m1",
            "begin:fixture.read:m2",
            "effect:fixture.read:m2",
        ]

        # Persisted intent + safe receipts.
        rows = _read_step_rows(steps_url, run_id)
        assert [row["seq"] for row in rows] == [1, 2]
        assert [row["state"] for row in rows] == ["succeeded", "succeeded"]
        for row in rows:
            receipt = row["receipt"]
            assert receipt["op_id"]
            assert receipt["allowed"] is True
            assert receipt["effect"] == EFFECT_EXECUTED
            assert receipt["status"] == "succeeded"
            assert "stdout" not in receipt and "command" not in receipt
        assert rows[0]["receipt"]["lock"] is None  # no resource declared
        assert rows[1]["receipt"]["lock"]["resource"] == "fx-res"
        assert rows[1]["receipt"]["lock"]["released"] is True

        # The lock was released after the proven terminal outcome.
        locks = ResourceLockService(steps_url)
        assert locks.inspect(SERVER_ID, "fx-res") is None

        # The whole-stream event cursor is monotonic and shared.
        events = _read_events(steps_url, run_id)
        assert [event.seq for event in events] == list(range(1, len(events) + 1))
        assert [event.type for event in events] == [
            "run.transition",
            "run.transition",
            "step.started",
            "step.completed",
            "step.started",
            "step.completed",
            "run.transition",
        ]
        started = [event for event in events if event.type == "step.started"]
        assert [event.data["seq"] for event in started] == [1, 2]

        # Legal run states.
        assert _run(_run_states(factory, run_id)) == (
            "queued",
            "running",
            "succeeded",
        )

    def test_policy_denied_step_fails_run_without_effect(
        self, steps_url, factory, insert_run, scenario
    ) -> None:
        run_id = str(uuid.uuid4())
        document = _intent_doc(run_id, [(READ_CAP, None)])
        _write_policy_files(scenario, grants=[])  # no grants at all
        client = EngineClient(steps_url)
        worker = None
        try:
            worker = _spawn_worker(steps_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "failed",
                RUN_TIMEOUT,
                "run failure",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        # Negative control: NO effect was attempted (no journal line at all).
        assert _journal_lines(scenario) == []

        row = _read_step_row(steps_url, run_id, 1)
        assert row["state"] == "failed"
        assert row["receipt"]["reason"] == "capability_not_granted"
        assert row["receipt"]["effect"] == EFFECT_NOT_ATTEMPTED
        assert row["receipt"]["allowed"] is False

        assert _run(_run_states(factory, run_id)) == (
            "queued",
            "running",
            "failed",
        )
        events = _read_events(steps_url, run_id)
        completed = [e for e in events if e.type == "step.completed"]
        assert completed
        assert completed[0].data["reason"] == "capability_not_granted"

    def test_approval_wait_then_resume_executes_exactly_once(
        self, steps_url, factory, insert_run, scenario
    ) -> None:
        """Acceptance 2: the ask-decision pauses the run LEGALLY."""
        run_id = str(uuid.uuid4())
        document = _intent_doc(
            run_id, [(MUTATE_CAP, None)], mode="ask", wait_poll_seconds=1.0
        )
        payload_hash = canonical_payload_hash({"marker": "m1"})
        client = EngineClient(steps_url)
        worker = None
        decision = None
        try:
            worker = _spawn_worker(steps_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: _run(_current_state(factory, run_id)) == "waiting_approval",
                RUN_TIMEOUT,
                "waiting_approval",
            )

            # Parked: the step row is still pending with NO receipt and the
            # effect never happened.
            row = _read_step_row(steps_url, run_id, 1)
            assert row["state"] == "pending"
            assert row["receipt"] is None
            assert _journal_lines(scenario) == []
            # The engine stays PENDING; the facade refines it (C05 rule 2).
            assert _terminal_engine_state(client, run_id) == "running"

            # The wait event carries the exact approval binding + deadline.
            events = _read_events(steps_url, run_id)
            awaiting = [e for e in events if e.type == "step.awaiting_approval"]
            assert len(awaiting) == 1
            assert awaiting[0].data["payload_hash"] == payload_hash
            assert awaiting[0].data["policy_revision"] == 1
            deadline = datetime.fromisoformat(awaiting[0].data["deadline"])
            assert deadline > datetime.now(timezone.utc)

            # The admin decision: bound to the exact operation (C03 rule 4).
            approvals = ApprovalStore(factory)
            decision = _run(
                approvals.create(
                    ApprovalRequest(
                        capability_id=MUTATE_CAP,
                        server_id=uuid.UUID(SERVER_ID),
                        payload_hash=payload_hash,
                        policy_revision=1,
                        created_by="hash:fixture-admin",
                        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    )
                )
            )
            _wake(run_id, steps_url)

            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "run success",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()
        assert decision is not None

        # The effect happened EXACTLY ONCE across the pause/resume.
        assert _journal_lines(scenario) == [
            "begin:fixture.mutate:m1",
            "effect:fixture.mutate:m1",
        ]

        row = _read_step_row(steps_url, run_id, 1)
        assert row["state"] == "succeeded"
        assert row["receipt"]["approval_spent"] is True
        assert row["receipt"]["approval_id"] == str(decision.approval_id)

        approval = _run(ApprovalStore(factory).get(decision.approval_id))
        assert approval is not None and approval.status == "consumed"

        assert _run(_run_states(factory, run_id)) == (
            "queued",
            "running",
            "waiting_approval",
            "running",
            "succeeded",
        )

    def test_uncertain_result_lands_reconciling_and_never_retries(
        self, steps_url, factory, insert_run, scenario
    ) -> None:
        """Acceptance 2: an unproven effect is reconciling, never success."""
        run_id = str(uuid.uuid4())
        document = _intent_doc(run_id, [(READ_CAP, None)], wait_poll_seconds=1.0)
        _set_behavior(scenario, {f"{READ_CAP}:m1": "raise_after_effect"})
        client = EngineClient(steps_url)
        worker = None
        try:
            worker = _spawn_worker(steps_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: _run(_current_state(factory, run_id)) == "reconciling",
                RUN_TIMEOUT,
                "reconciling",
            )
            # The effect happened once and is NOT retried while parked.
            assert _journal_count(scenario, "effect:fixture.read:m1") == 1
            assert _journal_count(scenario, "begin:fixture.read:m1") == 1

            row = _read_step_row(steps_url, run_id, 1)
            assert row["state"] == "outcome_unknown"
            assert row["receipt"]["reason"] == "executor_failed"
            assert row["receipt"]["effect"] == EFFECT_UNKNOWN

            # Engine PENDING, facade reconciling — the designed layering.
            assert _terminal_engine_state(client, run_id) == "running"

            _resolve_step(steps_url, run_id, 1, "failed")
            _wake(run_id, steps_url)

            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "failed",
                RUN_TIMEOUT,
                "run failure",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        # Still exactly one effect — the uncertain outcome was NEVER retried.
        assert _journal_count(scenario, "effect:fixture.read:m1") == 1
        assert _run(_run_states(factory, run_id)) == (
            "queued",
            "running",
            "reconciling",
            "running",
            "failed",
        )
        row = _read_step_row(steps_url, run_id, 1)
        assert row["state"] == "failed"
        assert row["receipt"]["resolution"] == "terminal"

    def test_kill_mid_effect_recovers_to_outcome_unknown_without_redispatch(
        self, steps_url, factory, insert_run, scenario
    ) -> None:
        """Guard rule 1/2: crash after the fence marker ⇒ outcome_unknown."""
        run_id = str(uuid.uuid4())
        document = _intent_doc(run_id, [(READ_CAP, None)], wait_poll_seconds=1.0)
        _set_behavior(scenario, {f"{READ_CAP}:m1": "gate"})
        client = EngineClient(steps_url)
        worker = None
        restarted = None
        try:
            worker = _spawn_worker(steps_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: (
                    _journal_count(scenario, "begin:fixture.read:m1") == 1
                    and (_read_step_row(steps_url, run_id, 1) or {}).get("state")
                    == "running"
                ),
                RUN_TIMEOUT,
                "in-flight gated effect",
            )
            _kill9(worker)
            worker = None

            # A fresh worker recovers; the gate never opens, so the prior
            # attempt's effect is unproven. The recovered attempt must NOT
            # re-dispatch: it translates the crash window.
            restarted = _spawn_worker(steps_url, scenario / "handlers", scenario, "w2")
            _wait_until(
                lambda: _run(_current_state(factory, run_id)) == "reconciling",
                RUN_TIMEOUT,
                "reconciling after recovery",
            )

            # Negative control: no second attempt ever reached the executor.
            assert _journal_count(scenario, "begin:fixture.read:m1") == 1
            assert _journal_count(scenario, "effect:fixture.read:m1") == 0

            row = _read_step_row(steps_url, run_id, 1)
            assert row["state"] == "outcome_unknown"
            assert row["receipt"]["reason"] == steps.CRASH_RECOVERY_REASON
            assert row["receipt"]["effect"] == EFFECT_UNKNOWN

            _resolve_step(steps_url, run_id, 1, "failed")
            _wake(run_id, steps_url)
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "failed",
                RUN_TIMEOUT,
                "run failure",
            )
        finally:
            if worker is not None:
                _kill9(worker)
            if restarted is not None:
                _stop(restarted)
            client.close()

        assert _run(_run_states(factory, run_id)) == (
            "queued",
            "running",
            "reconciling",
            "running",
            "failed",
        )

    def test_completed_step_replays_without_reexecution_after_restart(
        self, steps_url, factory, insert_run, scenario
    ) -> None:
        """Exactly-once: a checkpointed terminal step never re-executes."""
        run_id = str(uuid.uuid4())
        document = _intent_doc(run_id, [(READ_CAP, None), (READ_CAP, None)])
        _set_behavior(scenario, {f"{READ_CAP}:m1": "gate", f"{READ_CAP}:m2": "gate"})
        _gate_file(scenario, READ_CAP, "m1").touch()  # step 1 may proceed
        # step 2's gate stays closed: the kill lands while it is in flight.
        client = EngineClient(steps_url)
        worker = None
        restarted = None
        try:
            worker = _spawn_worker(steps_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: (
                    _journal_count(scenario, "begin:fixture.read:m2") == 1
                    and (_read_step_row(steps_url, run_id, 2) or {}).get("state")
                    == "running"
                ),
                RUN_TIMEOUT,
                "second step in flight",
            )
            _kill9(worker)
            worker = None

            # Step 1 completed BEFORE the kill: its DBOS step checkpoint and
            # its terminal receipt both survive. A fresh worker resumes the
            # run; step 1 replays its checkpoint (NO new begin/effect) while
            # the in-flight step 2 recovers to the crash-window translation.
            restarted = _spawn_worker(steps_url, scenario / "handlers", scenario, "w2")
            _wait_until(
                lambda: _run(_current_state(factory, run_id)) == "reconciling",
                RUN_TIMEOUT,
                "reconciling after recovery",
            )
        finally:
            if worker is not None:
                _kill9(worker)
            if restarted is not None:
                _stop(restarted)
            client.close()

        lines = _journal_lines(scenario)
        assert lines.count("begin:fixture.read:m1") == 1
        assert lines.count("effect:fixture.read:m1") == 1
        assert lines.count("begin:fixture.read:m2") == 1  # never re-dispatched
        assert lines.count("effect:fixture.read:m2") == 0

        rows = _read_step_rows(steps_url, run_id)
        assert rows[0]["state"] == "succeeded"
        assert rows[1]["state"] == "outcome_unknown"
        assert rows[1]["receipt"]["reason"] == steps.CRASH_RECOVERY_REASON

    def test_lock_contention_fails_step_without_touching_the_holder(
        self, steps_url, factory, insert_run, scenario
    ) -> None:
        run_id = str(uuid.uuid4())
        document = _intent_doc(run_id, [(READ_CAP, "fx-res")])
        locks = ResourceLockService(steps_url)
        lease = locks.acquire(SERVER_ID, "fx-res", owner_ref="other-holder", ttl=120.0)
        client = EngineClient(steps_url)
        worker = None
        try:
            worker = _spawn_worker(steps_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "failed",
                RUN_TIMEOUT,
                "run failure",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        assert _journal_lines(scenario) == []  # no effect was attempted
        row = _read_step_row(steps_url, run_id, 1)
        assert row["state"] == "failed"
        assert row["receipt"]["reason"] == "lock_held"
        snapshot = locks.inspect(SERVER_ID, "fx-res")
        assert snapshot is not None and snapshot.owner_ref == "other-holder"
        locks.release(lease)


# ---------------------------------------------------------------------------
# live half: facade run-state translation (real lifecycle, no worker)
# ---------------------------------------------------------------------------


async def _anchor_running(factory, insert_run, run_id: str) -> RunLifecycle:
    await insert_run(run_id)
    lifecycle = RunLifecycle(factory)
    await lifecycle.record_transition(run_id, RunState.QUEUED)
    await lifecycle.record_transition(run_id, RunState.RUNNING)
    return lifecycle


class TestRunTranslation:
    def test_fail_run_from_queued(self, steps_url, factory, insert_run) -> None:
        run_id = str(uuid.uuid4())
        _run(insert_run(run_id))
        executor = steps.StepExecutor(
            sessions=factory,
            dispatcher=_wired_dispatcher(factory),
            locks=ResourceLockService(steps_url),
        )
        assert _run(executor.afail_run(run_id, "handler_error")) == ("handler_error")
        assert _run(_run_states(factory, run_id)) == ("queued", "failed")

    def test_fail_run_from_waiting_resumes_through_running(
        self, steps_url, factory, insert_run
    ) -> None:
        run_id = str(uuid.uuid4())
        lifecycle = _run(_anchor_running(factory, insert_run, run_id))
        _run(lifecycle.record_transition(run_id, RunState.WAITING_APPROVAL))
        executor = steps.StepExecutor(
            sessions=factory,
            dispatcher=_wired_dispatcher(factory),
            locks=ResourceLockService(steps_url),
        )
        _run(executor.afail_run(run_id, "approval_wait_deadline"))
        assert _run(_run_states(factory, run_id)) == (
            "queued",
            "running",
            "waiting_approval",
            "running",
            "failed",
        )

    def test_cancel_run_from_reconciling(self, steps_url, factory, insert_run) -> None:
        run_id = str(uuid.uuid4())
        lifecycle = _run(_anchor_running(factory, insert_run, run_id))
        _run(lifecycle.record_transition(run_id, RunState.RECONCILING))
        executor = steps.StepExecutor(
            sessions=factory,
            dispatcher=_wired_dispatcher(factory),
            locks=ResourceLockService(steps_url),
        )
        _run(executor.acancel_run(run_id))
        assert _run(_run_states(factory, run_id)) == (
            "queued",
            "running",
            "reconciling",
            "running",
            "cancelled",
        )

    def test_wait_deadline_is_stable_across_re_entry(
        self, steps_url, factory, insert_run
    ) -> None:
        """Re-entering a wait returns the ORIGINAL persisted deadline."""
        run_id = str(uuid.uuid4())
        _run(_anchor_running(factory, insert_run, run_id))
        executor = steps.StepExecutor(
            sessions=factory,
            dispatcher=_wired_dispatcher(factory),
            locks=ResourceLockService(steps_url),
        )
        wait_kwargs = dict(
            target=RunState.WAITING_APPROVAL,
            event_type=steps.STEP_AWAITING_APPROVAL_EVENT_TYPE,
            event_data={"payload_hash": "a" * 64, "policy_revision": 1},
            wait_seconds=3600.0,
            step_seq=1,
        )
        first = _run(executor.aobserve_enter_wait(run_id, **wait_kwargs))
        second = _run(executor.aobserve_enter_wait(run_id, **wait_kwargs))
        assert first == second
        assert _run(_run_states(factory, run_id)) == (
            "queued",
            "running",
            "waiting_approval",
        )

    def test_wait_entry_from_non_running_is_rejected(
        self, steps_url, factory, insert_run
    ) -> None:
        run_id = str(uuid.uuid4())
        _run(insert_run(run_id))
        executor = steps.StepExecutor(
            sessions=factory,
            dispatcher=_wired_dispatcher(factory),
            locks=ResourceLockService(steps_url),
        )
        with pytest.raises(steps.StepsError) as excinfo:
            _run(
                executor.aobserve_enter_wait(
                    run_id,
                    target=RunState.RECONCILING,
                    event_type=steps.STEP_RECONCILING_EVENT_TYPE,
                    event_data={},
                    wait_seconds=60.0,
                    step_seq=1,
                )
            )
        assert excinfo.value.code is steps.StepsErrorCode.INVALID_WAIT_STATE
