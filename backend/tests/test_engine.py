"""Focused tests for the DBOS engine adapter (card 05-engine-adapter).

Acceptance (BRIEF):

1. The selected engine (DBOS 3.2.0, job-engine ADR `bc8d264`) is the only
   scheduler: durable enqueue and claim go through the application facade
   seam :class:`mai.jobs.engine.EngineClient` (a bare DBOSClient — never a
   second full engine in a control-plane process), the ``run_id`` IS the
   deterministic workflow ID with the reject policy (C05 rule 5), and every
   engine status is mapped onto the frozen :class:`RunState` vocabulary or
   fails closed (C05 rule 2).
2. The worker (:mod:`mai.jobs.worker`) starts, claims and restarts fully
   independently of the HTTP server: real ``python -m mai.jobs.worker``
   subprocesses run against a real disposable Postgres, an accepted run
   completes exactly once, and a kill -9 mid-run is recovered by a FRESH
   worker process (completed steps replayed from checkpoints, the in-flight
   step executed once more — the measured exp1 semantics, proven here at
   domain level through the adapter).

Boundary discipline (test policy): every durable claim runs against a real
disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``) with REAL worker
processes (kill/restart harness); mocks are used only for the pure
fail-closed mapping boundary, which no fixture can produce cheaply
(``DELAYED``/``MAX_RECOVERY_ATTEMPTS_EXCEEDED`` engine statuses). Fixtures
carry only TEST-NET-style tmp-namespace paths — never secret values.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from mai.contracts.jobs import EngineStateError, RunState
from mai.jobs import engine
from mai.jobs.engine import (
    EngineClient,
    EngineError,
    EngineErrorCode,
    RunIdInUseError,
    UnknownHandlerError,
    UnknownRunError,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Scratch namespace per runtime-environment ADR D2/D4 (recorded + cleaned in
# the card evidence). Each test gets a fresh subdirectory.
FIXTURE_ROOT = Path("/tmp/mai-fxt-05-engine-adapter")

# Worker waits are bounded; the whole live suite stays well under a minute.
READY_TIMEOUT = 90.0
STEP_TIMEOUT = 60.0

# Canonical fixture identity (opaque engine ID; C01 rule 1).
RUN_ID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# scenario handler module (written to the scratch namespace; imported ONLY by
# worker subprocesses — never inside pytest, so no registration races)
# ---------------------------------------------------------------------------

HANDLER_MODULE_NAME = "engine_scenario_handlers"

HANDLER_MODULE_SOURCE = '''
"""Scenario handlers for 05-engine-adapter: harmless, journaled effects.

Real handler modules register themselves with the adapter at import time —
the same contract owner cards will use.
"""

import json
import os
import time

from dbos import DBOS

from mai.jobs import engine


@DBOS.step()
def _mark(path: str, marker: str) -> None:
    with open(path, "a") as f:
        f.write(marker + "\\n")


@DBOS.step()
def _gate(path: str, timeout: float) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return "released"
        time.sleep(0.1)
    raise RuntimeError("gate timeout")


def echo(payload_json: str) -> str:
    payload = json.loads(payload_json)
    _mark(payload["journal"], payload["marker"])
    return payload["marker"]


def gated(payload_json: str) -> str:
    payload = json.loads(payload_json)
    _mark(payload["journal"], payload["started"])
    _gate(payload["gate"], payload["timeout"])
    _mark(payload["journal"], payload["done"])
    return "done"


engine.register_job_handler("echo", echo)
engine.register_job_handler("gated", gated)
'''


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _wait_until(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _journal(scenario: Path) -> Path:
    return scenario / "journal.log"


def _journal_count(scenario: Path, marker: str) -> int:
    try:
        lines = _journal(scenario).read_text().splitlines()
    except FileNotFoundError:
        return 0
    return sum(1 for line in lines if line == marker)


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
    log = open(scenario / f"{tag}.log", "w")
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
    proc.send_signal(signal.SIGTERM)
    return proc.wait(timeout=30)


@pytest.fixture(scope="session")
def engine_url() -> str:
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the durable-engine claims need a real "
            "disposable Postgres fixture"
        )
    return url


@pytest.fixture(scope="session")
def bootstrapped_url(engine_url: str):
    """Create the engine-owned ``dbos`` schema the way production does.

    The DBOS system schema is created by a full engine launch, never by the
    bare client and never by Alembic. One launch + clean shutdown in the test
    session plays the worker's bootstrap role (recovery finds nothing: the
    fixture starts empty); control-plane tests then run client-only, exactly
    like the API process.
    """
    engine.launch_worker(engine_url)
    engine.shutdown()
    return engine_url


@pytest.fixture(scope="session")
def client(bootstrapped_url: str):
    with EngineClient(bootstrapped_url) as facade:
        yield facade


@pytest.fixture
def scenario() -> Path:
    """Fresh scratch namespace per test; always cleaned up."""
    root = FIXTURE_ROOT / uuid.uuid4().hex
    (root / "handlers").mkdir(parents=True)
    (root / "handlers" / f"{HANDLER_MODULE_NAME}.py").write_text(HANDLER_MODULE_SOURCE)
    _journal(root).touch()
    yield root
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# unit half: pure validation + the fail-closed boundary (no engine, no DB)
# ---------------------------------------------------------------------------


class TestValueFreeValidation:
    def test_run_id_must_be_canonical_uuid(self) -> None:
        for bad in (None, 42, "", "not-a-uuid", RUN_ID.upper(), f"{{{RUN_ID}}}"):
            with pytest.raises(EngineError) as excinfo:
                engine.validate_run_id(bad)
            assert excinfo.value.code is EngineErrorCode.INVALID_RUN_ID
            assert str(RUN_ID) not in str(excinfo.value)
        assert engine.validate_run_id(RUN_ID) == RUN_ID

    def test_handler_name_is_a_dotted_lowercase_slug(self) -> None:
        for bad in (None, "", "Echo", "a b", "a..b", "-lead", "dot.", "ünïcode"):
            with pytest.raises(EngineError) as excinfo:
                engine.validate_handler_name(bad)
            assert excinfo.value.code is EngineErrorCode.INVALID_HANDLER_NAME
        assert engine.validate_handler_name("echo") == "echo"
        assert engine.validate_handler_name("steps.shell.exec") == "steps.shell.exec"

    def test_payload_must_be_a_string(self) -> None:
        with pytest.raises(EngineError) as excinfo:
            engine.validate_payload_json({"payload": "object"})
        assert excinfo.value.code is EngineErrorCode.INVALID_PAYLOAD

    def test_database_url_requires_the_measured_form(self) -> None:
        for bad in (
            None,
            "postgresql+psycopg://localhost/db",
            "postgres://localhost/db",
            "sqlite:///file.db",
        ):
            with pytest.raises(EngineError) as excinfo:
                engine.validate_database_url(bad)
            assert excinfo.value.code is EngineErrorCode.INVALID_DATABASE_URL
            assert str(bad) not in str(excinfo.value)
        with pytest.raises(EngineError) as excinfo:
            engine.validate_database_url("")
        assert excinfo.value.code is EngineErrorCode.INVALID_DATABASE_URL
        assert engine.validate_database_url("postgresql://localhost:5432/db")

    def test_queue_concurrency_must_be_positive_int(self) -> None:
        for bad in (None, 0, -1, 1.5, True, "4"):
            with pytest.raises(EngineError) as excinfo:
                engine.validate_queue_concurrency(bad)
            assert excinfo.value.code is EngineErrorCode.INVALID_CONCURRENCY
        assert engine.validate_queue_concurrency(4) == 4

    def test_launch_worker_validates_before_any_engine_init(self) -> None:
        with pytest.raises(EngineError):
            engine.launch_worker("postgres://localhost/db")
        with pytest.raises(EngineError):
            engine.launch_worker("postgresql://localhost/db", queue_concurrency=0)
        # No engine was launched by the rejected calls.
        assert engine._LAUNCHED is None


class TestFailClosedEngineMapping:
    """Unmapped DBOS statuses must raise, never guess (C05 rule 2).

    No cheap fixture produces DELAYED/MAX_RECOVERY_ATTEMPTS_EXCEEDED, so the
    boundary is proven against a stubbed engine status object; the mapping
    itself is the frozen contract function.
    """

    class _StubStatus:
        def __init__(self, status: str) -> None:
            self.status = status

    class _StubHandle:
        def __init__(self, status: str) -> None:
            self._status = status

        def get_status(self) -> "TestFailClosedEngineMapping._StubStatus":
            return TestFailClosedEngineMapping._StubStatus(self._status)

    class _StubClient:
        def __init__(self, status: str) -> None:
            self._status = status

        def destroy(self) -> None:
            pass

        def retrieve_workflow(
            self, run_id: str
        ) -> "TestFailClosedEngineMapping._StubHandle":  # noqa: ANN001
            return TestFailClosedEngineMapping._StubHandle(self._status)

    @pytest.mark.parametrize(
        "unmapped", ["DELAYED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED", "FUTURE"]
    )
    def test_unmapped_engine_status_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, unmapped: str
    ) -> None:
        monkeypatch.setattr(
            engine, "DBOSClient", lambda **_: self._StubClient(unmapped)
        )
        with EngineClient("postgresql://localhost:5432/db") as facade:
            with pytest.raises(EngineStateError):
                facade.run_state(RUN_ID)

    def test_mapped_status_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            engine, "DBOSClient", lambda **_: self._StubClient("SUCCESS")
        )
        with EngineClient("postgresql://localhost:5432/db") as facade:
            assert facade.run_state(RUN_ID) is RunState.SUCCEEDED


# ---------------------------------------------------------------------------
# live half: real Postgres + REAL worker processes
# ---------------------------------------------------------------------------


class TestDurableEnqueueAndClaim:
    def test_reused_run_id_is_a_structured_conflict(
        self, client: EngineClient, scenario: Path, engine_url: str
    ) -> None:
        """C05 rule 5: a lost ID race never attaches and never re-runs."""
        run_id = str(uuid.uuid4())
        payload = json.dumps(
            {
                "journal": str(_journal(scenario)),
                "marker": "ECHO-EFFECT",
            }
        )
        worker = _spawn_worker(
            engine_url, scenario / "handlers", scenario, "w-conflict"
        )
        try:
            client.enqueue_run(run_id, "echo", payload)
            _wait_until(
                lambda: client.run_state(run_id) is RunState.SUCCEEDED,
                READY_TIMEOUT,
                "run success",
            )
            assert _journal_count(scenario, "ECHO-EFFECT") == 1
            with pytest.raises(RunIdInUseError) as excinfo:
                client.enqueue_run(run_id, "echo", payload)
            assert excinfo.value.code is EngineErrorCode.RUN_ID_IN_USE
            # The conflict raised, nothing re-executed, state unchanged.
            _wait_until(
                lambda: _journal_count(scenario, "ECHO-EFFECT") == 1,
                10.0,
                "no duplicate effect",
            )
            assert client.run_state(run_id) is RunState.SUCCEEDED
        finally:
            _kill9(worker)

    def test_unregistered_handler_fails_the_run_value_free(
        self, client: EngineClient, scenario: Path, engine_url: str
    ) -> None:
        """No silent fallback: a missing handler is a FAILED run, value-free."""
        run_id = str(uuid.uuid4())
        client.enqueue_run(run_id, "ghost.handler", "{}")
        worker = _spawn_worker(engine_url, scenario / "handlers", scenario, "w-ghost")
        try:
            _wait_until(
                lambda: client.run_state(run_id) is RunState.FAILED,
                READY_TIMEOUT,
                "run failure",
            )
            assert _journal_count(scenario, "ECHO-EFFECT") == 0
            # The workflow exception is the adapter's value-free rejection.
            with pytest.raises(UnknownHandlerError) as excinfo:
                client.await_run(run_id)
            assert excinfo.value.code is EngineErrorCode.HANDLER_NOT_FOUND
        finally:
            _kill9(worker)

    def test_accepted_run_completes_once_and_state_is_engine_owned(
        self, client: EngineClient, scenario: Path, engine_url: str
    ) -> None:
        run_id = str(uuid.uuid4())
        payload = json.dumps(
            {"journal": str(_journal(scenario)), "marker": "ONCE-EFFECT"}
        )
        # Accepted but unclaimed: ENQUEUED maps to the frozen queued state.
        client.enqueue_run(run_id, "echo", payload)
        assert client.run_state(run_id) is RunState.QUEUED

        worker = _spawn_worker(engine_url, scenario / "handlers", scenario, "w-once")
        try:
            _wait_until(
                lambda: client.run_state(run_id) is RunState.SUCCEEDED,
                READY_TIMEOUT,
                "run success",
            )
            assert _journal_count(scenario, "ONCE-EFFECT") == 1

            # The API process goes away (the worker never depended on it); a
            # NEW client still sees engine truth (DBOS system tables are the
            # run-state owner — no facade cache).
            with EngineClient(engine_url) as fresh:
                assert fresh.run_state(run_id) is RunState.SUCCEEDED
        finally:
            _kill9(worker)


class TestWorkerRestartRecovery:
    def test_restart_completes_accepted_intent_once(
        self, client: EngineClient, scenario: Path, engine_url: str
    ) -> None:
        """The card scenario: kill -9 mid-run, a FRESH worker finishes it once."""
        run_id = str(uuid.uuid4())
        payload = json.dumps(
            {
                "journal": str(_journal(scenario)),
                "started": "STARTED",
                "done": "DONE",
                "gate": str(scenario / "gate"),
                "timeout": 300.0,
            }
        )
        client.enqueue_run(run_id, "gated", payload)
        assert client.run_state(run_id) is RunState.QUEUED

        worker1 = _spawn_worker(engine_url, scenario / "handlers", scenario, "w-r1")
        try:
            _wait_until(
                lambda: _journal_count(scenario, "STARTED") == 1,
                STEP_TIMEOUT,
                "run claim + first step",
            )
        finally:
            _kill9(worker1)

        # Engine truth while nobody runs it: still PENDING → running.
        assert client.run_state(run_id) is RunState.RUNNING

        # Fresh process, fresh launch recovery (guard rule 8, exp1).
        (scenario / "gate").touch()
        worker2 = _spawn_worker(engine_url, scenario / "handlers", scenario, "w-r2")
        try:
            _wait_until(
                lambda: client.run_state(run_id) is RunState.SUCCEEDED,
                READY_TIMEOUT,
                "recovered run success",
            )
        finally:
            _stop(worker2)
        # Completed steps replayed from checkpoints (no duplicate start marker);
        # the in-flight step executed once more and its effect happened once.
        assert _journal_count(scenario, "STARTED") == 1
        assert _journal_count(scenario, "DONE") == 1

    def test_cancel_is_requested_and_resume_is_deliberate(
        self, client: EngineClient, scenario: Path, engine_url: str
    ) -> None:
        """Guard rules 3/8: queued→cancelled edge; recovery is explicit."""
        run_id = str(uuid.uuid4())
        payload = json.dumps(
            {
                "journal": str(_journal(scenario)),
                "started": "RESUME-STARTED",
                "done": "RESUME-DONE",
                "gate": str(scenario / "resume-gate"),
                "timeout": 300.0,
            }
        )
        client.enqueue_run(run_id, "gated", payload)
        assert client.run_state(run_id) is RunState.QUEUED

        # Requested ≠ verified: the request is durable, then the engine lands
        # CANCELLED (an ENQUEUED run never starts a step).
        client.request_cancel(run_id)
        _wait_until(
            lambda: client.run_state(run_id) is RunState.CANCELLED,
            STEP_TIMEOUT,
            "engine CANCELLED",
        )

        # ERROR/CANCELLED runs stay put until an explicit resume (guard 8) —
        # no worker is running, so nothing may move on its own.
        time.sleep(2.0)
        assert client.run_state(run_id) is RunState.CANCELLED

        (scenario / "resume-gate").touch()
        client.resume_run(run_id)
        worker = _spawn_worker(engine_url, scenario / "handlers", scenario, "w-resume")
        try:
            _wait_until(
                lambda: client.run_state(run_id) is RunState.SUCCEEDED,
                READY_TIMEOUT,
                "resumed run success",
            )
            assert _journal_count(scenario, "RESUME-STARTED") == 1
            assert _journal_count(scenario, "RESUME-DONE") == 1
        finally:
            _stop(worker)

        # Unknown-run rejections are structured and value-free.
        ghost = str(uuid.uuid4())
        with pytest.raises(UnknownRunError) as excinfo:
            client.request_cancel(ghost)
        assert excinfo.value.code is EngineErrorCode.RUN_NOT_FOUND
        with pytest.raises(UnknownRunError):
            client.resume_run(ghost)
        assert client.run_state(ghost) is None
