"""Focused tests for restart reconciliation + safe cancellation (05-cancel-recovery).

Acceptance (BRIEF):

1. Outstanding supervised steps inspected after worker restart; uncertain
   actions pause/reconcile before retry. Inspection classifies every
   ``job_steps`` row against facade + engine truth; the reconciliation
   owner resolves ``outcome_unknown`` rows ONLY from final host verdicts
   (``running``/``uncertain`` verdicts are negative-controlled: the row
   and its fence stay untouched); a crash window the engine will never
   replay is certified to ``outcome_unknown`` — never finalized, never
   re-dispatched.
2. Cancel request does not report cancelled until the engine outcome is
   known (a budget expiry raises without claiming any outcome); the
   waiting-approval park survives a kill -9 worker restart and resumes
   exactly once; a cancel landing during the approval wait leaves the
   step receipt-less ``pending`` with NO effect and no lease.

Boundary discipline (test policy): every state-machine, lock-disposition,
cancel and restart claim runs against a real disposable PostgreSQL
fixture (``MAI_TEST_DATABASE_URL``) with the real Alembic chain, the real
engine adapter, the real lock table and REAL worker subprocesses (kill -9
mid-effect and mid-approval-wait + fresh-worker launch recovery). The
host-verdict source is an injected stub in the resolution/reconcile tests
— DISCLOSED LIMITATION: the stub cannot prove SSH/host truth; the real
verdict producer is ``04-reconcile``'s reviewed API (host-verified
boundaries proven there and at the 05-verify assembled journey). The
cancel-timeout test uses a deterministic stub engine client (protocol
failure semantics only). Fixtures carry only tmp-namespace paths and
marker words — never secret values.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
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
from sqlalchemy.ext.asyncio import async_sessionmaker

from mai.capabilities.dispatch import canonical_payload_hash
from mai.contracts.jobs import RunState, StepState
from mai.database import create_app_engine
from mai.execution.reconcile import ReconciliationClass
from mai.jobs import engine, recovery, steps
from mai.jobs.engine import EngineClient
from mai.jobs.lifecycle import RunLifecycle
from mai.jobs.resource_locks import (
    LockErrorCode,
    ResourceLockError,
    ResourceLockService,
)
from mai.policy.approvals import ApprovalRequest, ApprovalStore
from mai.policy.evaluate import ExecutionContext, PolicyMode
from mai.tag_store import acreate_tag
from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID, SYSTEM_ASSIGNMENT_SOURCE

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# ONE persistent test loop (the sibling discipline: SQLAlchemy binds
# asyncio primitives to the first loop that touches an engine).
_TEST_LOOP: asyncio.AbstractEventLoop | None = None
_TEST_LOOP_LOCK = threading.Lock()


def _run(coro: object) -> object:
    """Run one coroutine on the persistent test loop; block for the result."""
    global _TEST_LOOP
    with _TEST_LOOP_LOCK:
        if _TEST_LOOP is None or _TEST_LOOP.is_closed():
            _TEST_LOOP = asyncio.new_event_loop()
            thread = threading.Thread(target=_TEST_LOOP.run_forever, daemon=True)
            thread.start()
    return asyncio.run_coroutine_threadsafe(coro, _TEST_LOOP).result()


LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

FIXTURE_ROOT = Path("/tmp/mai-fxt-05-cancel-recovery")

READY_TIMEOUT = 90.0
RUN_TIMEOUT = 120.0

# Frozen fixture identities (canonical lowercase UUID strings, C01 rule 1).
SERVER_ID = "5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60"

READ_CAP = "fixture.read"
MUTATE_CAP = "fixture.mutate"

HANDLER_MODULE_NAME = "recovery_scenario_handlers"


# ---------------------------------------------------------------------------
# the worker-side composition: real dispatcher steps + plain engine probes
# ---------------------------------------------------------------------------

HANDLER_MODULE_SOURCE = '''
"""Worker-side composition for 05-cancel-recovery scenarios."""

import json
import os
import time
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import mai.jobs.steps as steps
from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
from mai.credentials.env import EnvResolver
from mai.jobs import engine
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
    """Journaled capability executor (the 05-steps fixture shape)."""

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
        status = "outcome_unknown" if behavior == "unknown_status" else "succeeded"
        document = {
            "status": status,
            "exit_code": 0 if status == "succeeded" else None,
            "stdout": "",
            "stderr": "",
        }
        return ExecutorResult(ok=status == "succeeded", output=json.dumps(document, sort_keys=True))


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
            os.environ.get("MAI_FX_ENV", "/tmp/mai-fxt-05-cancel-recovery/absent.env")
        ),
        executor=FixtureExecutor(),
    )
    locks = ResourceLockService(url)
    steps.configure_steps(
        steps.StepExecutor(sessions=sessions, dispatcher=dispatcher, locks=locks)
    )


def _mark(path: str, marker: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(marker + "\\n")


def gated(payload_json: str) -> str:
    """Plain engine probe: mark started, wait for the gate file, mark done."""
    payload = json.loads(payload_json)
    _mark(payload["journal"], payload["started"])
    deadline = time.time() + float(payload["timeout"])
    while not os.path.exists(payload["gate"]):
        if time.time() > deadline:
            raise RuntimeError("gate timeout")
        time.sleep(0.05)
    _mark(payload["journal"], payload["done"])
    return "done"


def boom(payload_json: str) -> str:
    raise RuntimeError("boom")


_build()
engine.register_job_handler("gated", gated)
engine.register_job_handler("boom", boom)
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
    if wait_poll_seconds is not None:
        document["wait_poll_seconds"] = wait_poll_seconds
    return document


def _journal_path(scenario: Path) -> Path:
    return scenario / "journal.log"


def _journal_lines(scenario: Path) -> list[str]:
    try:
        return _journal_path(scenario).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _journal_count(scenario: Path, marker: str) -> int:
    return _journal_lines(scenario).count(marker)


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
    runs that would otherwise occupy the fresh worker's queue slots.
    """
    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS tags, job_tags, job_resource_locks, "
            "job_schedule_fires, job_schedules, job_events, job_steps, "
            "job_runs, approvals, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, notification_alerts, observation_findings, "
            "server_observations, knowledge_doc_chunks, knowledge_documents, "
            "application_projection_journal, application_projection, "
            "conversation_steering, conversation_runs, conversation_tags, "
            "conversation_messages, conversations, "
            "agent_task_completions, agent_task_revisions, "
            "agent_task_evidence, agent_task_criteria, "
            "agent_task_constraints, agent_task_steps, agent_tasks, "
            "alembic_version"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
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
        return conn.execute(
            "SELECT seq, ts, type, v, data FROM job_events "
            "WHERE run_id = %s ORDER BY seq",
            (uuid.UUID(run_id),),
        ).fetchall()


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


def _terminal_engine_state(client: EngineClient, run_id: str) -> str | None:
    state = client.run_state(run_id)
    return None if state is None else state.value


def _seed_step(
    url: str,
    run_id: str,
    seq: int,
    state: str,
    receipt: dict | None,
    *,
    capability_id: str = READ_CAP,
) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO job_steps (step_id, run_id, seq, capability_id, state, receipt)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (run_id, seq) DO UPDATE"
            " SET state = EXCLUDED.state, receipt = EXCLUDED.receipt",
            (
                uuid.uuid4(),
                uuid.UUID(run_id),
                seq,
                capability_id,
                state,
                None if receipt is None else json.dumps(receipt),
            ),
        )
        conn.commit()


TERMINAL_TAG_NAMES = ("succeeded", "failed", "cancelled")


def _seed_terminal_catalog(factory, names=TERMINAL_TAG_NAMES) -> None:
    """Idempotently create the terminal catalog tags (sync seam)."""
    async def _seed() -> None:
        for name in names:
            await acreate_tag(factory, name=name)

    asyncio.run(_seed())


def _read_job_tag_names(url: str, run_id: str) -> list[str]:
    """The run's assigned tag names, ordered (assertion seam)."""
    with psycopg.connect(url, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT t.name FROM job_tags jt JOIN tags t ON t.tag_id = jt.tag_id "
            "WHERE jt.job_id = %s ORDER BY t.name",
            (uuid.UUID(run_id),),
        ).fetchall()
    return [row["name"] for row in rows]


def _anchor_running(factory, insert_run, run_id: str) -> RunLifecycle:
    _run(insert_run(run_id))
    lifecycle = RunLifecycle(factory)
    _run(lifecycle.record_transition(run_id, RunState.QUEUED))
    _run(lifecycle.record_transition(run_id, RunState.RUNNING))
    return lifecycle


def _acquire_step_lease(
    url: str,
    run_id: str,
    seq: int,
    *,
    resource: str = "fx-resource",
    ttl: float | None = None,
    owner_ref: str | None = None,
):
    service = ResourceLockService(url)
    return service.acquire(
        SERVER_ID,
        resource,
        owner_ref=owner_ref if owner_ref is not None else f"run:{run_id}:step:{seq}",
        ttl=ttl,
    )


def _lock_receipt(
    release_lease, *, released: bool = False, lapsed: bool = False
) -> dict:
    """The executor's ``_lock_receipt_fields`` shape (receipt contract)."""
    return {
        "resource": release_lease.resource,
        "owner_token": release_lease.owner_token,
        "lease_ttl": release_lease.lease_ttl,
        "released": released,
        "lapsed": lapsed,
    }


class _StubEngineClient:
    """Deterministic protocol-failure stub (disclosed limitation).

    Never claims an outcome: ``run_state`` always reports ``running`` so
    the cancel wait can be proven to TIME OUT instead of reporting
    ``cancelled`` from an assumption.
    """

    def __init__(self) -> None:
        self.cancel_requests: list[tuple[str, bool]] = []

    def run_state(self, run_id: str) -> RunState | None:
        return RunState.RUNNING

    def request_cancel(self, run_id: str, *, cancel_children: bool = False) -> None:
        self.cancel_requests.append((run_id, cancel_children))


class _StubVerdictSource:
    """Verdicts keyed by seq; ``None`` and non-final classes included."""

    def __init__(self, verdicts: dict[int, ReconciliationClass | None]) -> None:
        self._verdicts = verdicts

    def verdict_for(self, *, run_id: str, seq: int, receipt: object):
        return self._verdicts.get(seq)


# ---------------------------------------------------------------------------
# live fixtures: real disposable Postgres + the real engine shape
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def recovery_url() -> str:
    """Disposable fixture database: Alembic chain + engine-owned schema."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the recovery claims need a real "
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
def factory(recovery_url: str) -> async_sessionmaker:
    """NullPool session factory for in-process facade assertions (sync)."""
    eng, sessions = create_app_engine(recovery_url)

    async def _clean() -> None:
        async with sessions() as session:
            for table in (
                "job_resource_locks",
                "job_schedule_fires",
                "job_steps",
                "job_events",
                "job_tags",
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
def insert_run(factory):
    async def _insert(run_id: str) -> None:
        async with factory() as session:
            # Production submit shape: the run row AND its system
            # ``unclassified`` assignment land in the SAME transaction.
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
            await session.execute(
                text(
                    """
                    INSERT INTO job_tags (job_id, tag_id, assigned_by,
                                          assignment_source)
                    VALUES (:j, :t, '', :src)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "j": uuid.UUID(run_id),
                    "t": uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
                    "src": SYSTEM_ASSIGNMENT_SOURCE,
                },
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


@pytest.fixture
def sender():
    """Recording wake seam: every (run_id, seq) wake request lands here."""
    sent: list[tuple[str, int]] = []

    def _send(run_id: str, seq: int) -> bool:
        sent.append((run_id, seq))
        return True

    _send.sent = sent  # type: ignore[attr-defined]
    return _send


@pytest.fixture
def wired(recovery_url: str, factory, sender):
    """One RecoveryService per test (injected wake seam; owned clients)."""
    service = recovery.RecoveryService(recovery_url, factory, sender=sender)
    yield service
    service.close()


# ---------------------------------------------------------------------------
# unit half: the verdict contract (negative controls, value-free errors)
# ---------------------------------------------------------------------------


class TestVerdictContract:
    def test_only_final_verdicts_resolve(self) -> None:
        assert set(recovery.VERDICT_STEP_STATES) == {
            ReconciliationClass.FINAL_SUCCEEDED,
            ReconciliationClass.FINAL_FAILED,
            ReconciliationClass.FINAL_CANCELLED,
        }
        assert (
            recovery.VERDICT_STEP_STATES[ReconciliationClass.FINAL_CANCELLED]
            is StepState.CANCELLED
        )

    def test_non_class_value_rejected_value_free(self) -> None:
        with pytest.raises(recovery.RecoveryError) as excinfo:
            recovery._validate_verdict("final_succeeded")
        assert excinfo.value.code is recovery.RecoveryErrorCode.INVALID_VERDICT
        assert "final_succeeded" not in str(excinfo.value)

    @pytest.mark.parametrize(
        "verdict",
        [
            ReconciliationClass.RUNNING,
            ReconciliationClass.UNCERTAIN,
            ReconciliationClass.UNCERTAIN_AFTER_RESTART,
        ],
    )
    def test_unresolved_verdicts_keep_rows_open(self, verdict) -> None:
        with pytest.raises(recovery.RecoveryError) as excinfo:
            recovery._validate_verdict(verdict)
        assert excinfo.value.code is recovery.RecoveryErrorCode.VERDICT_UNRESOLVED

    def test_field_validation_is_value_free(self) -> None:
        with pytest.raises(recovery.RecoveryError) as excinfo:
            recovery._canonical_run_id("not-a-uuid")
        assert excinfo.value.code is recovery.RecoveryErrorCode.INVALID_RUN_ID
        with pytest.raises(recovery.RecoveryError):
            recovery._validate_seq(0)
        with pytest.raises(recovery.RecoveryError):
            recovery._validate_seq(True)
        with pytest.raises(recovery.RecoveryError):
            recovery._validate_source("")
        with pytest.raises(recovery.RecoveryError):
            recovery._validate_source("x" * (recovery.MAX_SOURCE_CHARS + 1))
        with pytest.raises(recovery.RecoveryError):
            recovery._validate_note("   ")
        with pytest.raises(recovery.RecoveryError):
            recovery._validate_timeout(0.0, 1.0, 0.5, 100.0)
        with pytest.raises(recovery.RecoveryError):
            recovery._validate_timeout(
                recovery.MAX_CANCEL_OUTCOME_TIMEOUT * 2,
                1.0,
                recovery.MIN_CANCEL_OUTCOME_TIMEOUT,
                recovery.MAX_CANCEL_OUTCOME_TIMEOUT,
            )


# ---------------------------------------------------------------------------
# inspection (acceptance 1: outstanding steps classified)
# ---------------------------------------------------------------------------


class TestInspection:
    def test_unknown_run_rejected(self, wired) -> None:
        with pytest.raises(recovery.RecoveryError) as excinfo:
            _run(wired.ainspect_run(str(uuid.uuid4())))
        assert excinfo.value.code is recovery.RecoveryErrorCode.RUN_NOT_FOUND

    def test_rows_are_classified_against_facade_and_engine(
        self, recovery_url, wired, factory, insert_run
    ) -> None:
        run_id = str(uuid.uuid4())
        lifecycle = _anchor_running(factory, insert_run, run_id)
        _seed_step(recovery_url, run_id, 1, "succeeded", {"v": 1})
        _seed_step(recovery_url, run_id, 2, "pending", None)
        _seed_step(recovery_url, run_id, 3, "outcome_unknown", {"v": 1})
        _seed_step(recovery_url, run_id, 4, "cancel_requested", None)
        _seed_step(recovery_url, run_id, 5, "running", None)
        _run(lifecycle.record_transition(run_id, RunState.WAITING_APPROVAL))
        inspection = _run(wired.ainspect_run(run_id))
        assert inspection.run_state is RunState.WAITING_APPROVAL
        assert inspection.engine_state is None  # no engine workflow exists
        findings = {f.seq: f.finding for f in inspection.findings}
        assert findings == {
            1: recovery.FindingClass.RESOLVED,
            2: recovery.FindingClass.AWAITING_APPROVAL,
            3: recovery.FindingClass.UNCERTAIN,
            4: recovery.FindingClass.CANCEL_PENDING,
            # The engine has NO workflow here: nobody can claim the step.
            5: recovery.FindingClass.CRASH_WINDOW,
        }
        assert [f.seq for f in inspection.uncertain] == [2, 3, 4, 5]

    def test_running_row_is_active_while_engine_workflow_lives(
        self, recovery_url, wired, factory, insert_run, scenario
    ) -> None:
        run_id = str(uuid.uuid4())
        _anchor_running(factory, insert_run, run_id)
        _seed_step(recovery_url, run_id, 1, "running", None)
        client = EngineClient(recovery_url)
        try:
            client.enqueue_run(
                run_id,
                "gated",
                _canonical(
                    {
                        "journal": str(_journal_path(scenario)),
                        "started": "eng",
                        "gate": str(scenario / "never-gate"),
                        "timeout": 2,
                        "done": "eng-done",
                    }
                ),
            )
            inspection = _run(wired.ainspect_run(run_id))
            assert inspection.engine_state is RunState.QUEUED
            assert inspection.findings[0].finding is recovery.FindingClass.ACTIVE
        finally:
            # Never leave the stale ENQUEUED workflow for later workers.
            client.request_cancel(run_id)
            client.close()

    def test_running_row_is_crash_window_without_engine(
        self, recovery_url, wired, factory, insert_run
    ) -> None:
        run_id = str(uuid.uuid4())
        _anchor_running(factory, insert_run, run_id)
        _seed_step(recovery_url, run_id, 1, "running", None)
        inspection = _run(wired.ainspect_run(run_id))
        assert inspection.findings[0].finding is recovery.FindingClass.CRASH_WINDOW

    def test_sweep_reports_nonterminal_runs_only(
        self, recovery_url, wired, factory, insert_run
    ) -> None:
        open_run = str(uuid.uuid4())
        done_run = str(uuid.uuid4())
        _seed_terminal_catalog(factory)
        _anchor_running(factory, insert_run, open_run)
        _seed_step(recovery_url, open_run, 1, "outcome_unknown", {"v": 1})
        lifecycle = _anchor_running(factory, insert_run, done_run)
        _run(lifecycle.record_transition(done_run, RunState.CANCELLED))
        inspections = _run(wired.ainspect_outstanding())
        by_id = {i.run_id: i for i in inspections}
        assert open_run in by_id and done_run not in by_id
        assert by_id[open_run].findings[0].finding is recovery.FindingClass.UNCERTAIN


# ---------------------------------------------------------------------------
# resolution: the ONLY uncertain-edge writer (real locks, real rows)
# ---------------------------------------------------------------------------


class TestResolution:
    def _seed_uncertain(self, url: str, insert_run, run_id: str, seq: int, lease):
        _run(insert_run(run_id))
        receipt = {
            "v": 1,
            "op_id": str(uuid.uuid4()),
            "capability_id": READ_CAP,
            "allowed": True,
            "reason": "status_unresolved",
            "effect": "unknown",
            "payload_hash": canonical_payload_hash({"marker": "m1"}),
            "lock": _lock_receipt(lease),
        }
        _seed_step(url, run_id, seq, "outcome_unknown", receipt)
        return receipt

    def test_final_verdict_resolves_and_releases_live_lease(
        self, recovery_url, factory, insert_run, wired, sender
    ) -> None:
        run_id = str(uuid.uuid4())
        lease = _acquire_step_lease(recovery_url, run_id, 1)
        self._seed_uncertain(recovery_url, insert_run, run_id, 1, lease)
        result = _run(
            wired.aresolve_step(run_id, 1, ReconciliationClass.FINAL_SUCCEEDED)
        )
        assert result.state is StepState.SUCCEEDED
        assert result.lock is recovery.LockDisposition.RELEASED
        assert result.wake_sent is True
        assert sender.sent == [(run_id, 1)]

        row = _read_step_row(recovery_url, run_id, 1)
        assert row["state"] == "succeeded"
        receipt = row["receipt"]
        assert receipt["lock"]["released"] is True
        assert receipt["resolution"]["class"] == "final_succeeded"
        assert receipt["resolution"]["source"] == "reconciliation"
        # The lease is gone: the key is free for a fresh acquisition.
        locks = ResourceLockService(recovery_url)
        fresh = locks.acquire(SERVER_ID, "fx-resource", owner_ref="successor")
        locks.release(fresh)

    def test_fenced_lease_cleared_only_under_final_verdict(
        self, recovery_url, factory, insert_run, wired
    ) -> None:
        run_id = str(uuid.uuid4())
        lease = _acquire_step_lease(recovery_url, run_id, 1, ttl=1.0)
        self._seed_uncertain(recovery_url, insert_run, run_id, 1, lease)
        # The holder dies; the lease lapses past its TTL on the database
        # clock, and the first touch of it fences the row.
        time.sleep(1.3)
        with pytest.raises(ResourceLockError) as excinfo:
            ResourceLockService(recovery_url).release(lease)
        assert excinfo.value.code is LockErrorCode.LEASE_LOST

        # A non-final verdict never clears the fence.
        with pytest.raises(recovery.RecoveryError) as excinfo:
            _run(wired.aresolve_step(run_id, 1, ReconciliationClass.RUNNING))
        assert excinfo.value.code is recovery.RecoveryErrorCode.VERDICT_UNRESOLVED
        snapshot = ResourceLockService(recovery_url).inspect(SERVER_ID, "fx-resource")
        assert snapshot is not None and snapshot.state == "fenced"

        result = _run(wired.aresolve_step(run_id, 1, ReconciliationClass.FINAL_FAILED))
        assert result.state is StepState.FAILED
        assert result.lock is recovery.LockDisposition.FENCE_CLEARED
        # The key is free again — the fence fell under the verdict.
        fresh = ResourceLockService(recovery_url).acquire(
            SERVER_ID, "fx-resource", owner_ref="successor"
        )
        ResourceLockService(recovery_url).release(fresh)

    def test_resolution_is_single_owner(
        self, recovery_url, factory, insert_run, wired
    ) -> None:
        run_id = str(uuid.uuid4())
        lease = _acquire_step_lease(recovery_url, run_id, 2)
        self._seed_uncertain(recovery_url, insert_run, run_id, 2, lease)
        first = _run(
            wired.aresolve_step(run_id, 2, ReconciliationClass.FINAL_SUCCEEDED)
        )
        assert first.state is StepState.SUCCEEDED
        with pytest.raises(recovery.RecoveryError) as excinfo:
            _run(wired.aresolve_step(run_id, 2, ReconciliationClass.FINAL_FAILED))
        assert excinfo.value.code is recovery.RecoveryErrorCode.STEP_NOT_UNCERTAIN

    def test_foreign_lease_reported_never_touched(
        self, recovery_url, factory, insert_run, wired
    ) -> None:
        run_id = str(uuid.uuid4())
        # The receipt recorded the ORIGINAL holder's token; a successor
        # owns the key by resolution time (lease released, key re-taken).
        stale = _acquire_step_lease(recovery_url, run_id, 1)
        receipt_lock = _lock_receipt(stale)
        ResourceLockService(recovery_url).release(stale)
        successor = ResourceLockService(recovery_url).acquire(
            SERVER_ID, "fx-resource", owner_ref="successor"
        )
        _run(insert_run(run_id))
        receipt = {
            "v": 1,
            "op_id": str(uuid.uuid4()),
            "capability_id": READ_CAP,
            "allowed": True,
            "reason": "status_unresolved",
            "effect": "unknown",
            "payload_hash": canonical_payload_hash({"marker": "m1"}),
            "lock": receipt_lock,
        }
        _seed_step(recovery_url, run_id, 1, "outcome_unknown", receipt)
        result = _run(
            wired.aresolve_step(run_id, 1, ReconciliationClass.FINAL_SUCCEEDED)
        )
        assert result.lock is recovery.LockDisposition.FOREIGN
        snapshot = ResourceLockService(recovery_url).inspect(SERVER_ID, "fx-resource")
        assert snapshot is not None and snapshot.owner_token == successor.owner_token
        ResourceLockService(recovery_url).release(successor)

    def test_missing_step_and_unknown_run_rejected(
        self, recovery_url, factory, insert_run, wired
    ) -> None:
        run_id = str(uuid.uuid4())
        _run(insert_run(run_id))
        with pytest.raises(recovery.RecoveryError) as excinfo:
            _run(wired.aresolve_step(run_id, 9, ReconciliationClass.FINAL_SUCCEEDED))
        assert excinfo.value.code is recovery.RecoveryErrorCode.STEP_NOT_FOUND
        ghost = str(uuid.uuid4())
        with pytest.raises(recovery.RecoveryError) as excinfo:
            _run(wired.aresolve_step(ghost, 1, ReconciliationClass.FINAL_SUCCEEDED))
        assert excinfo.value.code is recovery.RecoveryErrorCode.RUN_NOT_FOUND


# ---------------------------------------------------------------------------
# certification + the facade park (engine-orphaned uncertainty)
# ---------------------------------------------------------------------------


class TestCertifyAndPark:
    def test_crash_window_certified_with_lease_reference(
        self, recovery_url, factory, insert_run, wired
    ) -> None:
        run_id = str(uuid.uuid4())
        _anchor_running(factory, insert_run, run_id)
        _acquire_step_lease(recovery_url, run_id, 1)
        _seed_step(recovery_url, run_id, 1, "running", None)
        finding = _run(wired.acertify_running_step(run_id, 1))
        assert finding.finding is recovery.FindingClass.UNCERTAIN
        row = _read_step_row(recovery_url, run_id, 1)
        assert row["state"] == "outcome_unknown"
        assert row["receipt"]["reason"] == "crash_recovery"
        assert row["receipt"]["effect"] == "unknown"
        # The still-held lease is referenced for the later disposition.
        assert row["receipt"]["lock"]["resource"] == "fx-resource"
        assert row["receipt"]["lock"]["owner_token"]
        events = _read_events(recovery_url, run_id)
        assert any(e["type"] == "step.completed" for e in events)

    def test_certify_refuses_while_engine_may_claim(
        self, recovery_url, factory, insert_run, wired, scenario
    ) -> None:
        run_id = str(uuid.uuid4())
        _anchor_running(factory, insert_run, run_id)
        _seed_step(recovery_url, run_id, 1, "running", None)
        client = EngineClient(recovery_url)
        try:
            client.enqueue_run(
                run_id,
                "gated",
                _canonical(
                    {
                        "journal": str(_journal_path(scenario)),
                        "started": "eng",
                        "gate": str(scenario / "never-gate"),
                        "timeout": 2,
                        "done": "eng-done",
                    }
                ),
            )
            with pytest.raises(recovery.RecoveryError) as excinfo:
                _run(wired.acertify_running_step(run_id, 1))
            assert excinfo.value.code is recovery.RecoveryErrorCode.INVALID_RUN_STATE
            with pytest.raises(recovery.RecoveryError) as excinfo:
                _run(wired.apark_uncertain(run_id))
            assert excinfo.value.code is recovery.RecoveryErrorCode.INVALID_RUN_STATE
            row = _read_step_row(recovery_url, run_id, 1)
            assert row["state"] == "running"  # untouched
        finally:
            client.close()

    def test_certify_on_terminal_row_returns_finding(
        self, recovery_url, factory, insert_run, wired
    ) -> None:
        run_id = str(uuid.uuid4())
        _anchor_running(factory, insert_run, run_id)
        _seed_step(recovery_url, run_id, 3, "succeeded", {"v": 1})
        finding = _run(wired.acertify_running_step(run_id, 3))
        assert finding.finding is recovery.FindingClass.RESOLVED

    def test_park_lands_reconciling_and_lists_open_steps(
        self, recovery_url, factory, insert_run, wired
    ) -> None:
        run_id = str(uuid.uuid4())
        _anchor_running(factory, insert_run, run_id)
        _acquire_step_lease(recovery_url, run_id, 1)
        _seed_step(recovery_url, run_id, 1, "running", None)  # crash window
        _seed_step(recovery_url, run_id, 2, "outcome_unknown", {"v": 1})
        inspection = _run(wired.apark_uncertain(run_id))
        assert inspection.run_state is RunState.RECONCILING
        # RECONCILING is non-terminal: the system unclassified tag stays
        # and no terminal tag appears.
        assert _read_job_tag_names(recovery_url, run_id) == ["unclassified"]
        rows = _read_step_rows(recovery_url, run_id)
        assert [r["state"] for r in rows] == ["outcome_unknown", "outcome_unknown"]
        events = _read_events(recovery_url, run_id)
        parked = [e for e in events if e["type"] == "run.recovery_parked"]
        assert len(parked) == 1
        assert parked[0]["data"]["steps"] == [1, 2]
        transitions = [e["data"] for e in events if e["type"] == "run.transition"]
        assert {"from": "running", "to": "reconciling"} in transitions
        # Idempotent: a second park re-inspects without new events.
        again = _run(wired.apark_uncertain(run_id))
        assert again.run_state is RunState.RECONCILING
        events = _read_events(recovery_url, run_id)
        assert len([e for e in events if e["type"] == "run.recovery_parked"]) == 1
        # The second park leaves the tag assignment unchanged.
        assert _read_job_tag_names(recovery_url, run_id) == ["unclassified"]

    def test_park_on_terminal_run_is_a_no_op(
        self, recovery_url, factory, insert_run, wired
    ) -> None:
        run_id = str(uuid.uuid4())
        lifecycle = _anchor_running(factory, insert_run, run_id)
        _seed_step(recovery_url, run_id, 1, "outcome_unknown", {"v": 1})
        _seed_terminal_catalog(factory)
        _run(lifecycle.record_transition(run_id, RunState.CANCELLED))
        assert _read_job_tag_names(recovery_url, run_id) == ["cancelled"]
        inspection = _run(wired.apark_uncertain(run_id))
        assert inspection.run_state is RunState.CANCELLED
        row = _read_step_row(recovery_url, run_id, 1)
        assert row["state"] == "outcome_unknown"  # terminal stays immutable
        # The orphan park is a no-op: cancelled stays the exact terminal tag.
        assert _read_job_tag_names(recovery_url, run_id) == ["cancelled"]


# ---------------------------------------------------------------------------
# engine-outcome translation + safe cancellation semantics
# ---------------------------------------------------------------------------


class TestEngineOutcome:
    def _enqueue_gated(
        self,
        client: EngineClient,
        insert_run,
        factory,
        scenario: Path,
        run_id: str,
        *,
        timeout: float = 30.0,
        gate_name: str = "eng-gate",
    ) -> Path:
        gate = scenario / "gates" / gate_name
        _run(insert_run(run_id))
        lifecycle = RunLifecycle(factory)
        _run(lifecycle.record_transition(run_id, RunState.QUEUED))
        _run(lifecycle.record_transition(run_id, RunState.RUNNING))
        client.enqueue_run(
            run_id,
            "gated",
            _canonical(
                {
                    "journal": str(_journal_path(scenario)),
                    "started": "gated:begin",
                    "gate": str(gate),
                    "timeout": timeout,
                    "done": "gated:done",
                }
            ),
        )
        return gate

    def test_success_lag_is_repaired(
        self, recovery_url, factory, insert_run, wired, scenario
    ) -> None:
        client = EngineClient(recovery_url)
        worker = None
        run_id = str(uuid.uuid4())
        try:
            worker = _spawn_worker(recovery_url, scenario / "handlers", scenario, "w1")
            gate = self._enqueue_gated(
                client, insert_run, factory, scenario, run_id, timeout=60.0
            )
            _wait_until(
                lambda: _journal_count(scenario, "gated:begin") >= 1,
                RUN_TIMEOUT,
                "workflow start",
            )
            gate.touch()
            _wait_until(
                lambda: _terminal_engine_state(client, run_id) == "succeeded",
                RUN_TIMEOUT,
                "engine success",
            )
            _seed_terminal_catalog(factory)
            applied = _run(wired.aapply_engine_outcome(run_id))
            assert applied is RunState.SUCCEEDED
            assert _run(_current_state(factory, run_id)) == "succeeded"
            # The lag repair swapped unclassified for the exact terminal tag.
            assert _read_job_tag_names(recovery_url, run_id) == ["succeeded"]
            # Idempotent: terminal is immutable, the second apply no-ops.
            assert _run(wired.aapply_engine_outcome(run_id)) is RunState.SUCCEEDED
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

    def test_failure_lag_is_repaired(
        self, recovery_url, factory, insert_run, wired, scenario
    ) -> None:
        client = EngineClient(recovery_url)
        worker = None
        run_id = str(uuid.uuid4())
        try:
            worker = _spawn_worker(recovery_url, scenario / "handlers", scenario, "w1")
            _run(insert_run(run_id))
            _seed_terminal_catalog(factory)
            lifecycle = RunLifecycle(factory)
            _run(lifecycle.record_transition(run_id, RunState.QUEUED))
            _run(lifecycle.record_transition(run_id, RunState.RUNNING))
            client.enqueue_run(run_id, "boom", _canonical({}))
            _wait_until(
                lambda: _terminal_engine_state(client, run_id) == "failed",
                RUN_TIMEOUT,
                "engine failure",
            )
            _seed_terminal_catalog(factory)
            applied = _run(wired.aapply_engine_outcome(run_id))
            assert applied is RunState.FAILED
            assert _run(_current_state(factory, run_id)) == "failed"
            # The lag repair swapped unclassified for the exact terminal tag.
            assert _read_job_tag_names(recovery_url, run_id) == ["failed"]
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

    def test_cancelled_lag_is_repaired(
        self, recovery_url, factory, insert_run, wired, scenario
    ) -> None:
        client = EngineClient(recovery_url)
        worker = None
        run_id = str(uuid.uuid4())
        try:
            worker = _spawn_worker(recovery_url, scenario / "handlers", scenario, "w1")
            gate = self._enqueue_gated(
                client, insert_run, factory, scenario, run_id, timeout=60.0
            )
            _wait_until(
                lambda: _journal_count(scenario, "gated:begin") >= 1,
                RUN_TIMEOUT,
                "workflow start",
            )
            client.request_cancel(run_id)
            _wait_until(
                lambda: _terminal_engine_state(client, run_id) == "cancelled",
                RUN_TIMEOUT,
                "engine cancel",
            )
            _seed_terminal_catalog(factory)
            applied = _run(wired.aapply_engine_outcome(run_id))
            assert applied is RunState.CANCELLED
            assert _run(_current_state(factory, run_id)) == "cancelled"
            del gate
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

    def test_engine_absent_and_unlanded_are_rejected(
        self, recovery_url, factory, insert_run, wired
    ) -> None:
        run_id = str(uuid.uuid4())
        _run(insert_run(run_id))
        with pytest.raises(recovery.RecoveryError) as excinfo:
            _run(wired.aapply_engine_outcome(run_id))
        assert excinfo.value.code is recovery.RecoveryErrorCode.ENGINE_UNKNOWN_RUN


async def _current_state(factory, run_id: str) -> str | None:
    state = await RunLifecycle(factory).current_state(run_id)
    return None if state is None else state.value


# ---------------------------------------------------------------------------
# cancellation: known outcomes only + the recorded request
# ---------------------------------------------------------------------------


class TestCancelSafety:
    def test_timeout_never_claims_cancelled(
        self, recovery_url, factory, insert_run
    ) -> None:
        run_id = str(uuid.uuid4())
        _run(insert_run(run_id))
        stub = _StubEngineClient()
        service = recovery.RecoveryService(
            recovery_url,
            factory,
            client=stub,  # type: ignore[arg-type]
            sender=lambda run_id, seq: True,
            cancel_poll_seconds=0.05,
        )
        try:
            with pytest.raises(recovery.RecoveryError) as excinfo:
                _run(service.acancel_run(run_id, outcome_timeout=0.5))
            assert (
                excinfo.value.code is recovery.RecoveryErrorCode.CANCEL_OUTCOME_TIMEOUT
            )
            # The request WAS recorded — engine call and facade event.
            assert stub.cancel_requests == [(run_id, False)]
            events = _read_events(recovery_url, run_id)
            requested = [e for e in events if e["type"] == "run.cancel_requested"]
            assert len(requested) == 1
            # The facade diagram NEVER recorded a cancelled outcome.
            transitions = [e["data"] for e in events if e["type"] == "run.transition"]
            assert all(t.get("to") != "cancelled" for t in transitions)
        finally:
            service.close()

    def test_cancel_request_recorded_once_and_reentry_is_idempotent(
        self, recovery_url, factory, insert_run, wired, scenario
    ) -> None:
        client = EngineClient(recovery_url)
        worker = None
        run_id = str(uuid.uuid4())
        try:
            worker = _spawn_worker(recovery_url, scenario / "handlers", scenario, "w1")
            gate = scenario / "gates" / "cancel-gate"
            _run(insert_run(run_id))
            _seed_terminal_catalog(factory)
            lifecycle = RunLifecycle(factory)
            _run(lifecycle.record_transition(run_id, RunState.QUEUED))
            _run(lifecycle.record_transition(run_id, RunState.RUNNING))
            client.enqueue_run(
                run_id,
                "gated",
                _canonical(
                    {
                        "journal": str(_journal_path(scenario)),
                        "started": "gated:begin",
                        "gate": str(gate),
                        "timeout": 60.0,
                        "done": "gated:done",
                    }
                ),
            )
            _wait_until(
                lambda: _journal_count(scenario, "gated:begin") >= 1,
                RUN_TIMEOUT,
                "workflow start",
            )
            result = _run(wired.acancel_run(run_id, outcome_timeout=30.0))
            assert result.outcome is RunState.CANCELLED
            assert result.requested_now is True
            events = _read_events(recovery_url, run_id)
            requested = [e for e in events if e["type"] == "run.cancel_requested"]
            assert len(requested) == 1
            # Re-entry: already terminal — same recorded request, no dup.
            again = _run(wired.acancel_run(run_id))
            assert again.outcome is RunState.CANCELLED
            assert again.requested_now is False
            assert again.requested_at == result.requested_at
            events = _read_events(recovery_url, run_id)
            requested = [e for e in events if e["type"] == "run.cancel_requested"]
            assert len(requested) == 1
            assert _run(_current_state(factory, run_id)) == "cancelled"
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

    def test_unknown_and_ghost_runs_rejected(
        self, recovery_url, factory, insert_run, wired
    ) -> None:
        ghost = str(uuid.uuid4())
        with pytest.raises(recovery.RecoveryError) as excinfo:
            _run(wired.acancel_run(ghost))
        assert excinfo.value.code is recovery.RecoveryErrorCode.RUN_NOT_FOUND
        row_only = str(uuid.uuid4())
        _run(insert_run(row_only))
        with pytest.raises(recovery.RecoveryError) as excinfo:
            _run(wired.acancel_run(row_only))
        assert excinfo.value.code is recovery.RecoveryErrorCode.ENGINE_UNKNOWN_RUN


# ---------------------------------------------------------------------------
# reconcile sweep through the (stub) host-verdict source
# ---------------------------------------------------------------------------


class TestReconcileRun:
    def _seed_three(self, recovery_url, factory, insert_run) -> str:
        run_id = str(uuid.uuid4())
        _anchor_running(factory, insert_run, run_id)
        _seed_step(recovery_url, run_id, 1, "outcome_unknown", {"v": 1})
        _seed_step(recovery_url, run_id, 2, "outcome_unknown", {"v": 1})
        _seed_step(recovery_url, run_id, 3, "outcome_unknown", {"v": 1})
        return run_id

    def test_source_verdicts_resolve_and_gaps_stay_open(
        self, recovery_url, factory, insert_run
    ) -> None:
        run_id = self._seed_three(recovery_url, factory, insert_run)
        source = _StubVerdictSource(
            {
                1: ReconciliationClass.FINAL_SUCCEEDED,
                2: None,
                3: ReconciliationClass.RUNNING,
            }
        )
        service = recovery.RecoveryService(
            recovery_url, factory, verdict_source=source, sender=lambda r, s: True
        )
        try:
            results = _run(service.areconcile_run(run_id))
        finally:
            service.close()
        assert len(results) == 3
        resolved = [r for r in results if isinstance(r, recovery.ResolveResult)]
        open_rows = [f for f in results if isinstance(f, recovery.StepFinding)]
        assert [r.seq for r in resolved] == [1]
        assert [f.seq for f in open_rows] == [2, 3]
        states = {r["seq"]: r["state"] for r in _read_step_rows(recovery_url, run_id)}
        assert states == {
            1: "succeeded",
            2: "outcome_unknown",
            3: "outcome_unknown",
        }

    def test_without_source_the_sweep_is_a_pure_reinspection(
        self, recovery_url, factory, insert_run
    ) -> None:
        run_id = self._seed_three(recovery_url, factory, insert_run)
        service = recovery.RecoveryService(
            recovery_url, factory, sender=lambda r, s: True
        )
        try:
            results = _run(service.areconcile_run(run_id))
        finally:
            service.close()
        assert all(isinstance(f, recovery.StepFinding) for f in results)
        states = {r["seq"]: r["state"] for r in _read_step_rows(recovery_url, run_id)}
        assert set(states.values()) == {"outcome_unknown"}


# ---------------------------------------------------------------------------
# live scenario: cancel during the approval wait (acceptance 2)
# ---------------------------------------------------------------------------


class TestCancelDuringApprovalWait:
    def test_cancel_lands_without_effect_and_the_step_stays_pending(
        self, recovery_url, factory, insert_run, wired, scenario
    ) -> None:
        run_id = str(uuid.uuid4())
        document = _intent_doc(
            run_id, [(MUTATE_CAP, None)], mode="ask", wait_poll_seconds=1.0
        )
        payload_hash = canonical_payload_hash({"marker": "m1"})
        client = EngineClient(recovery_url)
        worker = None
        try:
            worker = _spawn_worker(recovery_url, scenario / "handlers", scenario, "w1")
            _run(insert_run(run_id))
            _seed_terminal_catalog(factory)
            client.enqueue_run(run_id, steps.HANDLER_NAME, _canonical(document))
            _wait_until(
                lambda: _run(_current_state(factory, run_id)) == "waiting_approval",
                RUN_TIMEOUT,
                "waiting_approval",
            )
            assert _journal_lines(scenario) == []  # nothing attempted
            row = _read_step_row(recovery_url, run_id, 1)
            assert row["state"] == "pending" and row["receipt"] is None

            result = _run(wired.acancel_run(run_id, outcome_timeout=60.0))
            assert result.outcome is RunState.CANCELLED
            assert result.requested_now is True
            assert result.unresolved_seqs == ()
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        # No orphan effect, no lease, no resolution debt.
        assert _journal_lines(scenario) == []
        row = _read_step_row(recovery_url, run_id, 1)
        assert row["state"] == "pending" and row["receipt"] is None
        assert (
            ResourceLockService(recovery_url).inspect(SERVER_ID, "fx-resource") is None
        )
        assert _run(_current_state(factory, run_id)) == "cancelled"
        requested = [
            e
            for e in _read_events(recovery_url, run_id)
            if e["type"] == "run.cancel_requested"
        ]
        assert len(requested) == 1

        # A decision arriving AFTER the cancel executes NOTHING (negative
        # control: a terminal run is never resumed by a late decision).
        approvals = ApprovalStore(factory)
        _run(
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
        _wake(run_id, recovery_url)
        time.sleep(3.0)
        assert _journal_lines(scenario) == []
        row = _read_step_row(recovery_url, run_id, 1)
        assert row["state"] == "pending"


# ---------------------------------------------------------------------------
# live scenario: the approval wait SURVIVES a worker restart (acceptance 2)
# ---------------------------------------------------------------------------


class TestApprovalWaitSurvivesRestart:
    def test_kill_during_wait_then_restart_resumes_exactly_once(
        self, recovery_url, factory, insert_run, wired, scenario
    ) -> None:
        run_id = str(uuid.uuid4())
        document = _intent_doc(
            run_id, [(MUTATE_CAP, None)], mode="ask", wait_poll_seconds=1.0
        )
        payload_hash = canonical_payload_hash({"marker": "m1"})
        client = EngineClient(recovery_url)
        worker = None
        try:
            worker = _spawn_worker(recovery_url, scenario / "handlers", scenario, "w1")
            _run(insert_run(run_id))
            client.enqueue_run(run_id, steps.HANDLER_NAME, _canonical(document))
            _wait_until(
                lambda: _run(_current_state(factory, run_id)) == "waiting_approval",
                RUN_TIMEOUT,
                "waiting_approval",
            )
        finally:
            if worker is not None:
                _kill9(worker)  # the worker dies MID-WAIT
            client.close()

        # During the dead window: the facade state and the durable
        # park are intact; inspection classifies the approval wait.
        inspection = _run(wired.ainspect_run(run_id))
        assert inspection.run_state is RunState.WAITING_APPROVAL
        assert inspection.engine_state is RunState.RUNNING  # parked workflow
        assert inspection.findings[0].finding is (
            recovery.FindingClass.AWAITING_APPROVAL
        )
        assert _journal_lines(scenario) == []

        # A fresh worker's launch recovery re-executes the parked
        # workflow; the decision then resolves it — exactly once.
        client = EngineClient(recovery_url)
        worker2 = None
        try:
            worker2 = _spawn_worker(recovery_url, scenario / "handlers", scenario, "w2")
            _wait_until(
                lambda: _run(_current_state(factory, run_id)) == "waiting_approval",
                RUN_TIMEOUT,
                "recovered park",
            )
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
            _wake(run_id, recovery_url)
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "run success after restart",
            )
            del decision
        finally:
            if worker2 is not None:
                _stop(worker2)
            client.close()

        assert _journal_lines(scenario) == [
            "begin:fixture.mutate:m1",
            "effect:fixture.mutate:m1",
        ]
        row = _read_step_row(recovery_url, run_id, 1)
        assert row["state"] == "succeeded"
        assert row["receipt"]["approval_spent"] is True
        assert _run(_current_state(factory, run_id)) == "succeeded"


# ---------------------------------------------------------------------------
# live scenario: kill mid-effect, recover, resolve, no duplicate effect
# ---------------------------------------------------------------------------


class TestKillMidEffectRecovery:
    def test_kill_replay_parks_reconciling_and_resolution_completes(
        self, recovery_url, factory, insert_run, wired, scenario
    ) -> None:
        run_id = str(uuid.uuid4())
        document = _intent_doc(
            run_id, [(READ_CAP, "fx-resource")], wait_poll_seconds=1.0
        )
        _set_behavior(scenario, {f"{READ_CAP}:m1": "gate"})
        client = EngineClient(recovery_url)
        worker = None
        try:
            worker = _spawn_worker(recovery_url, scenario / "handlers", scenario, "w1")
            _run(insert_run(run_id))
            client.enqueue_run(run_id, steps.HANDLER_NAME, _canonical(document))
            _wait_until(
                lambda: _journal_count(scenario, "begin:fixture.read:m1") == 1,
                RUN_TIMEOUT,
                "step attempt start",
            )
        finally:
            if worker is not None:
                _kill9(worker)  # SIGKILL mid-effect (inside the gate)
            client.close()

        # Pre-restart: the attempt owns the window; recovery must NOT
        # certify it (the engine workflow may still claim the step).
        inspection = _run(wired.ainspect_run(run_id))
        assert inspection.engine_state is RunState.RUNNING
        assert inspection.findings[0].finding is recovery.FindingClass.ACTIVE
        with pytest.raises(recovery.RecoveryError) as excinfo:
            _run(wired.acertify_running_step(run_id, 1))
        assert excinfo.value.code is recovery.RecoveryErrorCode.INVALID_RUN_STATE

        # A fresh worker's launch recovery replays the attempt: the
        # crash window lands outcome_unknown and the run parks.
        client = EngineClient(recovery_url)
        worker2 = None
        try:
            worker2 = _spawn_worker(recovery_url, scenario / "handlers", scenario, "w2")
            _wait_until(
                lambda: _run(_current_state(factory, run_id)) == "reconciling",
                RUN_TIMEOUT,
                "reconciling park after replay",
            )
            row = _read_step_row(recovery_url, run_id, 1)
            assert row["state"] == "outcome_unknown"
            assert row["receipt"]["reason"] == "crash_recovery"
            assert row["receipt"]["lock"]["resource"] == "fx-resource"

            # NO duplicate side effect: the crash window was never
            # re-dispatched.
            assert _journal_count(scenario, "begin:fixture.read:m1") == 1
            assert _journal_count(scenario, "effect:fixture.read:m1") == 0

            # The fleet sweep still lists the open uncertainty.
            sweep = _run(wired.ainspect_outstanding())
            assert any(i.run_id == run_id for i in sweep)

            # The host verdict (stub producer — see module docstring)
            # resolves the row; the parked run resumes with it.
            result = _run(
                wired.aresolve_step(run_id, 1, ReconciliationClass.FINAL_SUCCEEDED)
            )
            assert result.state is StepState.SUCCEEDED
            # The lease's TTL outlives the test only sometimes (the dead
            # holder's heartbeats stopped at the kill): both dispositions
            # prove the SAME end state — the key is freed under the
            # verdict, by release (still live) or fence-clear (lapsed).
            assert result.lock in (
                recovery.LockDisposition.RELEASED,
                recovery.LockDisposition.FENCE_CLEARED,
            )
            _wake(run_id, recovery_url)
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "run success after resolution",
            )
        finally:
            if worker2 is not None:
                _stop(worker2)
            client.close()

        # Exactly one DISPATCH across crash + recovery + resolution: the
        # crash window was never re-dispatched, so there is exactly one
        # ``begin`` and no duplicate side effect. The gated fixture effect
        # itself never completed before the kill — the FINAL_SUCCEEDED
        # stub verdict stands in for the host truth that it did (the real
        # producer is 04-reconcile on the live target; disclosed).
        assert _journal_lines(scenario) == [
            "begin:fixture.read:m1",
        ]
        assert _journal_count(scenario, "begin:fixture.read:m1") == 1
        row = _read_step_row(recovery_url, run_id, 1)
        assert row["state"] == "succeeded"
        lock = row["receipt"]["lock"]
        assert lock is None or lock["released"] is True
        assert row["receipt"]["resolution"]["class"] == "final_succeeded"
        # The key is free either way: no orphan lease survives the run.
        snapshot = ResourceLockService(recovery_url).inspect(SERVER_ID, "fx-resource")
        assert snapshot is None or snapshot.state == "fenced" and snapshot.expired
        assert _run(_current_state(factory, run_id)) == "succeeded"
        # Terminal runs drop out of the outstanding sweep.
        sweep = _run(wired.ainspect_outstanding())
        assert all(i.run_id != run_id for i in sweep)
