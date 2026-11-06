"""S4 composed queued-chat regression (queued-chat hotfix plan).

One focused file proving the REPAIRED boundary end to end on a real
disposable PostgreSQL fixture:

- a REAL authenticated HTTP chat turn (the actual ``submit_turn`` route,
  no route/store/completion mocks) creates the run AND the retained
  execution facade itself — ``job_runs`` is NEVER pre-seeded here — then
  an actual DBOS worker process executes it against the deterministic
  scripted local model (the reviewed D04 harness composition) and the
  session settles terminal ``succeeded`` with a real assistant entry;
- the runbook execution route (same root seam, S3) reaches the same
  local stub model and settles terminal without host tools;
- an ordinary model error (the scripted model raises on first call)
  settles the run ``failed`` on BOTH authorities — never stranded
  ``queued``.

Reuse mandate: the worker composition, spawn, scripted model, journal,
policy and await-terminal helpers are imported from the reviewed D04
suite (``test_session_runner`` / ``agent_runtime_harness``); only the
HTTP front end (real ``create_app`` + TestClient) is composed here.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from starlette.testclient import TestClient

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.jobs.engine import EngineClient

# The reviewed D04 harness (worker composition, spawn protocol, scripted
# model wiring, readers) — reused verbatim, not rebuilt.
from test_session_runner import (
    _await_terminal,
    _read_session_entries,
    _read_session_runs,
    _read_sessions_row,
    _script,
    _spawn_worker,
    _write_model_root,
    _write_data_root,
    _write_policy_files,
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"
ORIGIN = "http://testserver"
FIXTURE_PASSWORD = "fixture-password-s4-composed"

_CHAT_REPLY = "composed assistant reply"
_RUNBOOK_REPLY = "runbook procedure completed"


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_all(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def composed_url() -> Iterator[str]:
    """Migrate the disposable DB + bootstrap the dbos schema, then reset."""
    from agent_runtime_harness import SERVER_ID  # noqa: F401 - profile target
    from mai.jobs import engine as jobs_engine

    url = os.environ.get(LIVE_URL_VAR, "")
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the composed claims need a real "
            "disposable Postgres fixture (pgvector/pgvector:pg16)"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_all(url)
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    jobs_engine.launch_worker(url)  # engine-owned dbos schema
    jobs_engine.shutdown()
    yield url
    jobs_engine.shutdown()
    _drop_all(url)


def _clean_domain(url: str) -> None:
    """Fresh session-first + jobs domain per test (FK-safe order)."""
    with psycopg.connect(url) as conn:
        for table in (
            "session_tags",
            "session_provider_artifacts",
            "session_admissions",
            "session_runs",
            "session_entries",
            "sessions",
            "job_resource_locks",
            "job_schedule_fires",
            "job_steps",
            "job_events",
            "job_schedules",
            "job_runs",
            "job_tags",
            "approvals",
        ):
            present = conn.execute(
                "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)
            ).fetchone()
            if present and present[0]:
                conn.execute(f"DELETE FROM {table}")
        conn.commit()


@pytest.fixture
def scenario(tmp_path: pathlib.Path) -> pathlib.Path:
    """One fresh scratch scenario directory (journal, gates, handlers)."""
    from test_session_runner import (
        SESSION_HANDLER_MODULE_NAME,
        SESSION_HANDLER_MODULE_SOURCE,
    )

    directory = tmp_path / "scenario"
    (directory / "gates").mkdir(parents=True)
    handlers = directory / "handlers"
    handlers.mkdir()
    (handlers / f"{SESSION_HANDLER_MODULE_NAME}.py").write_text(
        SESSION_HANDLER_MODULE_SOURCE, encoding="utf-8"
    )
    return directory


def _compose_http_front(
    url: str, scenario: pathlib.Path
) -> tuple[TestClient, dict[str, str]]:
    """The REAL app over the SAME fixture DB + scenario data root, with
    the authenticated cookie jar extracted from the login response (the
    reviewed D06/D08 suite pattern)."""
    _clean_domain(url)
    app = create_app(
        env={
            "MAI_DATABASE_URL": url,
            "MAI_PUBLIC_ORIGINS": ORIGIN,
            "MAI_DATA_ROOT": str(scenario / "data-root"),
        }
    )
    client = TestClient(app)
    client.__enter__()
    bootstrap = client.post(
        "/api/v1/auth/bootstrap",
        json={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert bootstrap.status_code in (201, 409), bootstrap.text
    login = client.post(
        "/api/v1/auth/login",
        json={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert login.status_code == 204, login.text
    jar = {name: value for name, value in login.cookies.items()}
    assert SESSION_COOKIE in jar and CSRF_COOKIE in jar
    return client, jar


def _mutation_headers(jar: dict[str, str]) -> dict[str, str]:
    return {"origin": ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]}


def _turn(
    client: TestClient,
    jar: dict[str, str],
    server_id: str,
    session_id: str,
    key: str,
) -> dict:
    response = client.post(
        f"/api/v1/agent-sessions/{session_id}/turns",
        params={"server_id": server_id},
        json={
            "session_id": session_id,
            "text": "composed regression turn",
            "client_request_id": key,
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status_code in (200, 201), response.text
    return response.json()



def _facade_footprint(url: str, run_id: str) -> dict[str, object]:
    """The retained execution footprint of one admitted run id."""
    with psycopg.connect(url) as conn:
        facade = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(depth), -1) AS depth "
            "FROM job_runs WHERE run_id = %s",
            (uuid.UUID(run_id),),
        ).fetchone()
        tags = conn.execute(
            "SELECT COUNT(*) AS n FROM job_tags WHERE job_id = %s",
            (uuid.UUID(run_id),),
        ).fetchone()
        run = conn.execute(
            "SELECT state FROM session_runs WHERE run_id = %s",
            (uuid.UUID(run_id),),
        ).fetchone()
    return {"facades": facade[0], "depth": facade[1], "tags": tags[0], "run": run[0]}


def _assistant_texts(url: str, session_id: str) -> list[str]:
    entries = _read_session_entries(url, session_id)
    return [
        entry["payload"]["text"]
        for entry in entries
        if entry["kind"] == "assistant_message"
        and isinstance(entry["payload"], dict)
        and "text" in entry["payload"]
    ]


def _require_worker_alive(worker: object, scenario: pathlib.Path, tag: str) -> None:
    """Fail fast with the worker log when the process dies at startup
    (import/wiring errors surface in seconds, not as a 120 s wait)."""
    import time as _time

    log_path = scenario / f"{tag}.log"
    deadline = _time.monotonic() + 10.0
    while _time.monotonic() < deadline and worker.poll() is None:
        try:
            log = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log = ""
        if "mai worker: ready" in log or "dbos" in log.lower():
            break
        _time.sleep(0.2)
    assert worker.poll() is None, (
        f"worker {tag} exited rc={worker.returncode} at startup; log tail:\n"
        f"{log_path.read_text(encoding='utf-8', errors='replace')[-2000:]}"
    )

def _compose_backend(scenario: pathlib.Path, *, capability: str) -> None:
    """The reviewed D04 ``composition`` step, bounded to what S4 needs:
    model root, data root (shared with the HTTP app) and the fixture
    credential env file the worker's resolver loads at import time."""
    from agent_runtime_harness import SECRET_NAME, SECRET_VALUE

    _write_model_root(scenario / "model-root", base_url="http://127.0.0.1:9/v1")
    _write_data_root(scenario / "data-root", capability=capability)
    (scenario / "fixture.env").write_text(
        f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8"
    )
    os.chmod(scenario / "fixture.env", 0o600)


def test_http_turn_executes_on_real_worker_to_terminal_success(
    composed_url: str, scenario: pathlib.Path
) -> None:
    """HTTP admission (which itself creates the facade — nothing is
    pre-seeded) → real DBOS worker → scripted local model → assistant
    entry and terminal agreement between engine and session authority."""
    from agent_runtime_harness import (
        READ_CAP,
        RUN_TIMEOUT,
        SERVER_ID,
        _journal_lines,
        _stop,
    )

    _compose_backend(scenario, capability=READ_CAP)
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(scenario, [{"text": _CHAT_REPLY}])

    client, jar = _compose_http_front(composed_url, scenario)
    worker = None
    engine_client = EngineClient(composed_url)
    try:
        created = client.post(
            "/api/v1/agent-sessions",
            json={
                "server_id": SERVER_ID,
                "source": "interactive",
                "client_request_id": str(uuid.uuid4()),
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["session_id"]
        key = str(uuid.uuid4())
        body = _turn(client, jar, SERVER_ID, session_id, key)
        assert body["run_state"] == "queued"
        run_id = body["run_id"]

        # The HTTP admission created the ENTIRE retained footprint — the
        # anti-preseed assertion: no test code inserted job_runs/job_tags.
        footprint = _facade_footprint(composed_url, run_id)
        assert footprint["facades"] == 1, "HTTP admission must create job_runs"
        assert footprint["depth"] == 0
        assert footprint["tags"] == 1  # mandatory classification
        assert footprint["run"] == "queued"

        worker = _spawn_worker(
            composed_url, scenario / "handlers", scenario, "s4-chat"
        )
        _require_worker_alive(worker, scenario, "s4-chat")
        _await_terminal(
            engine_client, run_id, "succeeded", RUN_TIMEOUT, "composed chat run"
        )
    finally:
        if worker is not None:
            _stop(worker)
        engine_client.close()
        client.__exit__(None, None, None)

    # The model was actually called with the real user turn. (Single-round
    # run: the scripted reply itself is proven by the durable assistant
    # entry below — a saw_text marker would need a second model call
    # receiving the reply back in history, as in the D04 two-run test.)
    lines = _journal_lines(scenario)
    assert "model_call" in lines, lines
    assert any(line.startswith("saw_user:") for line in lines), lines
    # Engine and session authorities AGREE on terminal success.
    runs = _read_session_runs(composed_url, session_id)
    assert len(runs) == 1 and runs[0]["run_id"] == uuid.UUID(run_id)
    assert runs[0]["state"] == "succeeded"
    assert _read_sessions_row(composed_url, session_id)["status"] == "idle"
    texts = _assistant_texts(composed_url, session_id)
    assert _CHAT_REPLY in texts
    kinds = [
        entry["kind"] for entry in _read_session_entries(composed_url, session_id)
    ]
    assert kinds.count("user_message") == 1
    assert "assistant_message" in kinds


def test_http_runbook_executes_on_real_worker_to_terminal_success(
    composed_url: str, scenario: pathlib.Path
) -> None:
    """The runbook root (same seam, S3) reaches the same local stub model
    through a real HTTP execution and settles terminal — no host tools."""
    from agent_runtime_harness import (
        READ_CAP,
        RUN_TIMEOUT,
        SERVER_ID,
        _journal_lines,
        _stop,
    )

    _compose_backend(scenario, capability=READ_CAP)
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(scenario, [{"text": _RUNBOOK_REPLY}])

    client, jar = _compose_http_front(composed_url, scenario)
    worker = None
    engine_client = EngineClient(composed_url)
    try:
        application_id = str(uuid.uuid4())
        adopted = client.post(
            f"/api/v1/servers/{SERVER_ID}/applications",
            json={
                "application_id": application_id,
                "name": "s4-fixture-app",
                "origin": "manual",
                "expected_revision": 0,
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert adopted.status_code == 201, adopted.text
        executed = client.post(
            f"/api/v1/servers/{SERVER_ID}/applications"
            f"/{application_id}/execute",
            json={"action": "verify"},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert executed.status_code == 201, executed.text
        view = executed.json()
        assert view["state"] == "queued"
        run_id = view["run_id"]

        # The runbook HTTP admission created the full footprint itself.
        footprint = _facade_footprint(composed_url, run_id)
        assert footprint["facades"] == 1
        assert footprint["depth"] == 0
        assert footprint["tags"] == 1
        assert footprint["run"] == "queued"

        worker = _spawn_worker(
            composed_url, scenario / "handlers", scenario, "s4-runbook"
        )
        _require_worker_alive(worker, scenario, "s4-runbook")
        _await_terminal(
            engine_client, run_id, "succeeded", RUN_TIMEOUT, "runbook run"
        )
    finally:
        if worker is not None:
            _stop(worker)
        engine_client.close()
        client.__exit__(None, None, None)

    assert "model_call" in _journal_lines(scenario)
    runs = _read_session_runs(composed_url, view["session_id"])
    assert len(runs) == 1 and runs[0]["state"] == "succeeded"
    assert _RUNBOOK_REPLY in _assistant_texts(composed_url, view["session_id"])


def test_model_error_settles_failed_not_queued(
    composed_url: str, scenario: pathlib.Path
) -> None:
    """An ordinary model failure (the scripted model raises on its first
    call) must settle ``failed`` on BOTH authorities — the durable session
    state may never stay ``queued``."""
    from agent_runtime_harness import RUN_TIMEOUT, SERVER_ID, _journal_lines, _stop

    _compose_backend(scenario, capability="fixture.read")
    _write_policy_files(scenario, grants=[{"fixture.read": True}])
    _script(scenario, [])  # first call exhausts the script -> model raises

    client, jar = _compose_http_front(composed_url, scenario)
    worker = None
    engine_client = EngineClient(composed_url)
    try:
        created = client.post(
            "/api/v1/agent-sessions",
            json={
                "server_id": SERVER_ID,
                "source": "interactive",
                "client_request_id": str(uuid.uuid4()),
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["session_id"]
        body = _turn(client, jar, SERVER_ID, session_id, str(uuid.uuid4()))
        run_id = body["run_id"]
        assert _facade_footprint(composed_url, run_id)["facades"] == 1
        worker = _spawn_worker(
            composed_url, scenario / "handlers", scenario, "s4-error"
        )
        _require_worker_alive(worker, scenario, "s4-error")
        _await_terminal(engine_client, run_id, "failed", RUN_TIMEOUT, "failing model")
    finally:
        if worker is not None:
            _stop(worker)
        engine_client.close()
        client.__exit__(None, None, None)

    assert "model_call" in _journal_lines(scenario)
    runs = _read_session_runs(composed_url, session_id)
    assert len(runs) == 1
    assert runs[0]["state"] == "failed", (
        f"session authority stranded in {runs[0]['state']!r} after a model "
        "error (must settle failed, never queued)"
    )
    kinds = [
        entry["kind"] for entry in _read_session_entries(composed_url, session_id)
    ]
    assert "user_message" in kinds
    assert "assistant_message" not in kinds  # no fabricated assistant output

