"""End-to-end acceptance for the act-fixture isolated runtime (00-runtime).

Drives ``scripts/agent_runtime_fixture.py`` through its recorded command
interface (``start`` / ``status`` / ``reset`` / ``stop``) and exercises the
real stack it owns — real FastAPI HTTP surface, real DBOS worker, disposable
marked Postgres container — through three runtime proofs:

- a synthetic chat turn end-to-end: HTTP submit → worker execution →
  persisted assistant output (run state + events in Postgres);
- scenario C4: the worker is killed at a deterministic effect checkpoint
  (gate), restarted, the same client request retried — one message, one
  intake run, exactly one completed effect, honest dedup/conflict verdicts;
- scenario X1: the representative task run repeated across two fresh
  instances with graded deterministic outcomes compared.

Every fixture resource carries the ``act-fixture-`` ownership marker; the
final cleanup proof asserts zero orphan containers, processes and ports.
Evidence JSON is written outside the repo under ``MAI_ACT_EVIDENCE_DIR``
(default: the campaign evidence directory). The fixture password is a
throwaway synthetic value and never enters evidence.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "agent_runtime_fixture.py"
BACKEND_ROOT = REPO_ROOT / "backend"
STATE_SCHEMA = "act-fixture-state/1"
SESSION_COOKIE = "mai_session"
CSRF_COOKIE = "mai_csrf"
MARKER = "act-fixture-"
INSTANCE_ENV_VAR = "MAI_FIXTURE_INSTANCE"

DEFAULT_EVIDENCE_DIR = Path(
    "/home/megastruktur/projects/dev/machina/plans/agent-continuity/"
    "act_evidence/00-runtime"
)
EVIDENCE_DIR = Path(
    os.environ.get("MAI_ACT_EVIDENCE_DIR", str(DEFAULT_EVIDENCE_DIR))
)

SUBMIT_TIMEOUT = 120.0
#: C4: the engine parks the recovered run on its persisted reconciliation
#: deadline (DEFAULT_RECONCILE_WAIT_SECONDS = 300s) before expiring it.
C4_TERMINAL_TIMEOUT = 420.0
CLI_TIMEOUT = 300.0
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


# ---------------------------------------------------------------------------
# skip / environment gates
# ---------------------------------------------------------------------------


def _docker_alive() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


requires_docker = pytest.mark.skipif(
    not _docker_alive(), reason="docker daemon unavailable; live fixture needs it"
)


# ---------------------------------------------------------------------------
# CLI driver
# ---------------------------------------------------------------------------


def _cli(
    *args: str, state_file: Path, timeout: float = CLI_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--state-file",
            str(state_file),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _cli_json(*args: str, state_file: Path) -> dict[str, Any]:
    proc = _cli(*args, "--json", state_file=state_file)
    assert proc.returncode == 0, (
        f"`{args[0]} --json` failed rc={proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# real HTTP client (loopback uvicorn surface)
# ---------------------------------------------------------------------------


def _http(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> tuple[int, list[tuple[str, str]], bytes]:
    payload = json.dumps(body).encode() if body is not None else None
    flat = dict(headers or {})
    if payload is not None:
        flat["content-type"] = "application/json"
    request = urllib.request.Request(url, data=payload, method=method, headers=flat)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                response.status,
                [(k.lower(), v) for k, v in response.headers.items()],
                response.read(),
            )
    except urllib.error.HTTPError as exc:
        return exc.code, [(k.lower(), v) for k, v in exc.headers.items()], exc.read()


def _cookies(header_pairs: list[tuple[str, str]]) -> dict[str, str]:
    jar: dict[str, str] = {}
    for name, value in header_pairs:
        if name != "set-cookie":
            continue
        pair = value.split(";", 1)[0]
        if "=" in pair:
            key, _, val = pair.partition("=")
            jar[key.strip()] = val.strip()
    return jar


def _login(origin: str, password: str) -> dict[str, str]:
    status, headers, body = _http(
        "POST",
        f"{origin}/api/v1/auth/login",
        body={"password": password},
        headers={"origin": origin},
    )
    assert status == 204, f"login failed ({status}): {body[:300]!r}"
    jar = _cookies(headers)
    assert SESSION_COOKIE in jar and CSRF_COOKIE in jar
    return jar


def _mutation_headers(origin: str, jar: dict[str, str]) -> dict[str, str]:
    return {
        "origin": origin,
        "x-csrf-token": jar[CSRF_COOKIE],
        "cookie": f"{SESSION_COOKIE}={jar[SESSION_COOKIE]}; "
        f"{CSRF_COOKIE}={jar[CSRF_COOKIE]}",
    }


def _submit_chat(
    origin: str,
    jar: dict[str, str],
    server_id: str,
    *,
    run_id: uuid.UUID,
    message: str = "investigate the act-fixture service",
) -> tuple[int, dict[str, Any]]:
    status, _, body = _http(
        "POST",
        f"{origin}/api/v1/chat/messages",
        body={
            "run_id": str(run_id),
            "server_id": server_id,
            "message": message,
            "profile": "fast",
        },
        headers=_mutation_headers(origin, jar),
    )
    return status, json.loads(body)


def _get_run(origin: str, jar: dict[str, str], run_id: uuid.UUID) -> dict[str, Any]:
    status, _, body = _http(
        "GET",
        f"{origin}/api/v1/chat/runs/{run_id}",
        headers={"cookie": f"{SESSION_COOKIE}={jar[SESSION_COOKIE]}"},
    )
    assert status == 200, f"run view failed ({status}): {body[:300]!r}"
    return json.loads(body)


def _await_terminal_state(
    origin: str, jar: dict[str, str], run_id: uuid.UUID, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    view: dict[str, Any] = {}
    while time.monotonic() < deadline:
        view = _get_run(origin, jar, run_id)
        if view.get("state") in TERMINAL_STATES:
            return view
        time.sleep(0.5)
    raise AssertionError(
        f"run {run_id} never reached a terminal state; last view: {view}"
    )


def _await_run_state(
    origin: str, jar: dict[str, str], run_id: uuid.UUID, target: str, timeout: float
) -> None:
    deadline = time.monotonic() + timeout
    view: dict[str, Any] = {}
    while time.monotonic() < deadline:
        view = _get_run(origin, jar, run_id)
        if view.get("state") == target:
            return
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} never reached {target!r}; last: {view}")


# ---------------------------------------------------------------------------
# fixture-state / journal / database helpers
# ---------------------------------------------------------------------------


def _read_state(state_file: Path) -> dict[str, Any]:
    document = json.loads(state_file.read_text(encoding="utf-8"))
    assert document["schema"] == STATE_SCHEMA
    return document


def _journal(home: Path) -> list[str]:
    path = home / "journal.log"
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _journal_count(home: Path, marker: str) -> int:
    return sum(1 for line in _journal(home) if line == marker)


def _wait_journal(home: Path, marker: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker in _journal(home):
            return
        time.sleep(0.25)
    raise AssertionError(
        f"journal never recorded {marker!r}; tail: {_journal(home)[-10:]}"
    )


def _psql_scalar(state: dict[str, Any], sql: str) -> str:
    proc = subprocess.run(
        [
            "docker",
            "exec",
            state["container"],
            "psql",
            "-U",
            "mai",
            "-d",
            "mai",
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _write_evidence(name: str, document: dict[str, Any]) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / name
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return target


def _restart_worker(state: dict[str, Any]) -> int:
    """Relaunch the worker through its RECORDED launch interface (the exact
    argv/env the fixture start wrote); returns the new worker pid."""
    launch = json.loads(Path(state["launch"]["worker"]).read_text(encoding="utf-8"))
    log = open(  # noqa: SIM115 - held for the process lifetime
        Path(state["home"]) / "logs" / "worker-restart.log", "ab"
    )
    try:
        proc = subprocess.Popen(
            launch["argv"],
            cwd=launch["cwd"],
            env=launch["env"],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log.close()
    return proc.pid


def _marked_pids() -> list[int]:
    needle = f"{INSTANCE_ENV_VAR}=".encode()
    mine = os.getpid()
    found: list[int] = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == mine:
            continue
        try:
            raw = Path(f"/proc/{entry.name}/environ").read_bytes()
        except OSError:
            continue
        for chunk in raw.split(b"\0"):
            if chunk.startswith(needle) and MARKER in chunk:
                found.append(int(entry.name))
                break
    return sorted(found)


def _fixture_containers() -> list[str]:
    proc = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"name={MARKER}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _cleanup_proof(state: dict[str, Any], state_file: Path) -> dict[str, Any]:
    return {
        "instance": state["instance"],
        "leftover_containers": _fixture_containers(),
        "leftover_processes": _marked_pids(),
        "state_file_present": state_file.exists(),
        "ports_free": {
            "postgres": not _port_busy(state["database"]["port"]),
            "api": not _port_busy(state["api"]["port"]),
        },
        "source": state.get("source"),
    }


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_help_lists_the_full_recorded_command_interface() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    for subcommand in ("start", "status", "reset", "stop"):
        assert subcommand in proc.stdout, f"--help missing {subcommand}"
    # The isolation contract is part of the interface contract (argparse
    # breaks long tokens after hyphens, so compare newline-stripped text).
    squashed = proc.stdout.replace("\n", "")
    assert "machina-postgres-1" in squashed
    assert "mai-wire-test-pg" in squashed


def state_path_for(state: dict[str, Any]) -> Path:  # noqa: D103 - see _cleanup_proof
    return Path(state["__state_file"])


@requires_docker
def test_lifecycle_synthetic_chat_and_c4_worker_restart(tmp_path: Path) -> None:
    """start → status → synthetic chat E2E → C4 kill/restart/retry → stop."""
    state_file = tmp_path / "act-fixture-state.json"
    state = _cli_json("start", state_file=state_file)
    try:
        # -- honest status right after start -------------------------------
        status = _cli_json("status", state_file=state_file)
        assert status["healthy"] is True, status
        assert status["container_running"] and status["postgres_ready"]

        origin = state["api"]["origin"]
        jar = _login(origin, state["database"]["password"])
        # `start` minted the fixture server through the real HTTP surface and
        # composed the worker-side grants for exactly that identity.
        server_id = state["server_id"]
        home = Path(state["home"])

        # == synthetic chat turn: HTTP → worker → persisted output =========
        run_id = uuid.uuid4()
        submit_status, submit_body = _submit_chat(
            origin, jar, server_id, run_id=run_id
        )
        assert submit_status == 201, submit_body
        assert submit_body["deduplicated"] is False
        view = _await_terminal_state(origin, jar, run_id, SUBMIT_TIMEOUT)
        assert view["state"] == "succeeded", view
        assert "fixture.read" in view["requested_grants"]

        # persisted effects: journaled executor markers
        assert _journal_count(home, "effect:fixture.read:m1") == 1
        assert _journal_count(home, "model_call") >= 2
        # persisted output: the run's events live in the fixture Postgres and
        # carry the scripted assistant text
        events = int(
            _psql_scalar(
                state,
                f"SELECT count(*) FROM job_events WHERE run_id = '{run_id}'",
            )
        )
        assert events > 0
        assistant_rows = _psql_scalar(
            state,
            f"SELECT count(*) FROM job_events WHERE run_id = '{run_id}' "
            f"AND data::text LIKE '%done%'",
        )
        assert int(assistant_rows) >= 1
        # paginated conversation list envelope (AGENTS.md pagination invariant)
        status_code, _, body = _http(
            "GET",
            f"{origin}/api/v1/chat/runs?server_id={server_id}&limit=10&offset=0",
            headers={"cookie": f"{SESSION_COOKIE}={jar[SESSION_COOKIE]}"},
        )
        assert status_code == 200
        page = json.loads(body)
        assert {"conversations", "limit", "offset", "total"} <= set(page)
        assert page["total"] >= 1
        assert any(c["run_id"] == str(run_id) for c in page["conversations"])

        _write_evidence(
            "chat-e2e.json",
            {
                "scenario": "synthetic chat turn end-to-end",
                "run_id": str(run_id),
                "submit_status": submit_status,
                "final_state": view["state"],
                "requested_grants": view["requested_grants"],
                "event_count": events,
                "journal_effect_markers": _journal_count(
                    home, "effect:fixture.read:m1"
                ),
                "source": state["source"],
            },
        )

        # == C4: kill at the effect checkpoint, restart, retry =============
        # The executor's fence marker (mark_running) is committed BEFORE the
        # dispatch; recovery answers a RUNNING row with the crash-window
        # outcome and NEVER re-dispatches the uncertain effect. The gate
        # file makes the kill window deterministic (executor parked inside
        # the effect) and is deliberately NEVER released: a re-dispatch
        # would surface as a duplicate effect marker and fail the test.
        (home / "gates" / "m1.gate").write_text("armed\n", encoding="utf-8")
        c4_run = uuid.uuid4()
        pre_begin = _journal_count(home, "begin:fixture.read:m1")
        pre_effect = _journal_count(home, "effect:fixture.read:m1")
        status_code, c4_body = _submit_chat(
            origin,
            jar,
            server_id,
            run_id=c4_run,
            message="investigate the act-fixture service (c4)",
        )
        assert status_code == 201, c4_body
        _wait_journal(home, "gate_wait:fixture.read:m1")
        old_worker_pid = state["worker"]["pid"]
        os.kill(old_worker_pid, signal.SIGKILL)
        deadline = time.monotonic() + 30.0
        while Path(f"/proc/{old_worker_pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not Path(f"/proc/{old_worker_pid}").exists()

        new_worker_pid = _restart_worker(state)

        # same client request id retried while the run is parked: the
        # identical submission ATTACHES (200, intent-level resume), a
        # different payload on the same id is a structured 409
        retry_status, retry_body = _submit_chat(
            origin,
            jar,
            server_id,
            run_id=c4_run,
            message="investigate the act-fixture service (c4)",
        )
        assert retry_status == 200, retry_body
        assert retry_body["deduplicated"] is True
        assert retry_body["conversation"]["run_id"] == str(c4_run)
        conflict_status, conflict_body = _submit_chat(
            origin, jar, server_id, run_id=c4_run, message="a different submission"
        )
        assert conflict_status == 409, conflict_body
        assert conflict_body["error"]["code"] == "conflict"
        # one intake result: exactly one run row for the retried request
        run_rows = int(
            _psql_scalar(
                state,
                f"SELECT count(*) FROM job_runs WHERE run_id = '{c4_run}'",
            )
        )
        assert run_rows == 1

        # the recovered run parks in `reconciling` (honest uncertain-effect
        # status), then the persisted deadline expires and the step fails
        # honestly — no invented success, no duplicate effect
        _await_run_state(origin, jar, c4_run, "reconciling", 60.0)
        c4_view = _await_terminal_state(origin, jar, c4_run, C4_TERMINAL_TIMEOUT)
        assert c4_view["state"] == "succeeded", c4_view
        assert _journal_count(home, "begin:fixture.read:m1") == pre_begin + 1, (
            "recovery must not re-dispatch the interrupted effect"
        )
        assert _journal_count(home, "effect:fixture.read:m1") == pre_effect, (
            "the interrupted effect must complete at most once"
        )
        reconciliation = _psql_scalar(
            state,
            f"SELECT count(*) FROM job_events WHERE run_id = '{c4_run}' "
            f"AND type = 'agent.awaiting_reconciliation'",
        )
        assert int(reconciliation) == 1
        completed = _psql_scalar(
            state,
            f"SELECT data::text FROM job_events WHERE run_id = '{c4_run}' "
            f"AND type = 'agent.completed'",
        )
        assert "execution_timeout_exceeded" in completed

        status_after = _cli_json("status", state_file=state_file)
        assert status_after["healthy"] is True, status_after
        assert status_after["worker"]["process_alive"] is True

        _write_evidence(
            "scenario-c4.json",
            {
                "scenario": "C4 worker interruption/restart + idempotent retry",
                "run_id": str(c4_run),
                "killed_worker_pid": old_worker_pid,
                "restarted_worker_pid": new_worker_pid,
                "final_state": c4_view["state"],
                "effect_markers_after": _journal_count(home, "effect:fixture.read:m1"),
                "effect_markers_before": pre_effect,
                "begin_markers_after": _journal_count(home, "begin:fixture.read:m1"),
                "begin_markers_before": pre_begin,
                "retry_status": retry_status,
                "retry_deduplicated": retry_body["deduplicated"],
                "conflict_status": conflict_status,
                "conflict_code": conflict_body["error"]["code"],
                "run_rows": run_rows,
                "reconciled_step_reason": "execution_timeout_exceeded",
                "source": state["source"],
            },
        )
    finally:
        stopped = _cli_json("stop", state_file=state_file)
    assert stopped["clean"] is True, stopped
    proof = _cleanup_proof(state, state_file)
    assert proof["leftover_containers"] == [], proof
    assert proof["leftover_processes"] == [], proof
    assert proof["ports_free"] == {"postgres": True, "api": True}, proof
    _write_evidence("cleanup.json", proof)


@requires_docker
def test_x1_repeatable_representative_task_runs(tmp_path: Path) -> None:
    """X1: the representative task run repeats across two fresh instances
    (start → run → stop, reset → run) with equal graded outcomes."""
    state_file = tmp_path / "act-fixture-state.json"
    outcomes: list[dict[str, Any]] = []
    instance_ids: list[str] = []

    def representative_run(state: dict[str, Any]) -> dict[str, Any]:
        origin = state["api"]["origin"]
        jar = _login(origin, state["database"]["password"])
        server_id = state["server_id"]
        run_id = uuid.uuid4()
        status_code, body = _submit_chat(
            origin,
            jar,
            server_id,
            run_id=run_id,
            message="investigate the act-fixture service (x1)",
        )
        assert status_code == 201, body
        view = _await_terminal_state(origin, jar, run_id, SUBMIT_TIMEOUT)
        return {
            "final_state": view["state"],
            "requested_grants": view["requested_grants"],
            "assistant_text_present": bool(
                int(
                    _psql_scalar(
                        state,
                        f"SELECT count(*) FROM job_events WHERE run_id = "
                        f"'{run_id}' AND data::text LIKE '%done%'",
                    )
                )
                >= 1
            ),
            "effect_markers": _journal_count(
                Path(state["home"]), "effect:fixture.read:m1"
            ),
            "run_rows": int(
                _psql_scalar(
                    state,
                    f"SELECT count(*) FROM job_runs WHERE run_id = '{run_id}'",
                )
            ),
        }

    state = _cli_json("start", state_file=state_file)
    instance_ids.append(state["instance"])
    try:
        outcomes.append(representative_run(state))
    finally:
        stopped = _cli_json("stop", state_file=state_file)
    assert stopped["clean"] is True, stopped

    # `reset` with no live state == plain start (deterministic fresh instance)
    state = _cli_json("reset", state_file=state_file)
    instance_ids.append(state["instance"])
    assert instance_ids[0] != instance_ids[1], "reset must mint a fresh instance"
    try:
        outcomes.append(representative_run(state))
    finally:
        stopped = _cli_json("stop", state_file=state_file)
    assert stopped["clean"] is True, stopped

    graded_keys = (
        "final_state",
        "requested_grants",
        "assistant_text_present",
        "effect_markers",
        "run_rows",
    )
    assert outcomes[0]["final_state"] == "succeeded"
    assert outcomes[0] == outcomes[1] or all(
        outcomes[0][key] == outcomes[1][key] for key in graded_keys
    )

    _write_evidence(
        "scenario-x1.json",
        {
            "scenario": "X1 repeatable representative task fixture run",
            "runs": outcomes,
            "graded_keys": list(graded_keys),
            "source": state["source"],
        },
    )
