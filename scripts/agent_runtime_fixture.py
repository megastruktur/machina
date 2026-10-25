#!/usr/bin/env python3
"""Isolated app-under-test lifecycle for the agent-continuity campaign (00-runtime).

One fixture instance owns EXACTLY these resources, every one carrying the
``act-fixture-`` ownership marker:

- one disposable Docker Postgres container named ``act-fixture-<id>``
  (``pgvector/pgvector:0.8.1-pg18``, loopback-only ephemeral port, anonymous
  volume, throwaway random password);
- one disposable state dir ``/tmp/act-fixture-<id>`` (data root, artifacts
  root, model root, worker handler module, journal, gate dir, logs);
- one real FastAPI API process (``uvicorn mai.api.app:create_app --factory``,
  the production entrypoint) on a loopback ephemeral port;
- one real DBOS worker process (``python -m mai.jobs.worker``) with a
  scripted deterministic FunctionModel and a journaled fixture executor;
- one disposable state file (default ``/tmp/act-fixture-state.json``)
  recording the instance and the exact launch commands.

Processes additionally carry the ``MAI_FIXTURE_INSTANCE`` environment marker
so cleanup can prove zero orphans even after a worker restart changed the
recorded PID. The fixture NEVER touches ``machina-postgres-1``,
``mai-wire-test-pg``, any other discovered database/container, host ports
outside its own ephemeral bindings, or any state outside its marker
namespace.

Subcommands: ``start`` | ``status`` | ``reset`` | ``stop``. Every command is
deterministic with bounded waits and prints one JSON report (``--json``) or a
short human summary. Exit codes: 0 ok, 1 degraded/failed, 2 no state/usage.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"
DEFAULT_STATE_FILE = Path("/tmp/act-fixture-state.json")
MARKER = "act-fixture-"
INSTANCE_ENV_VAR = "MAI_FIXTURE_INSTANCE"
STATE_SCHEMA = "act-fixture-state/1"
PG_IMAGE = "pgvector/pgvector:0.8.1-pg18"
#: Exact production API server pin (backend/Dockerfile installs it outside
#: the locked project deps; this fixture mirrors that line).
UVICORN_PIN = "uvicorn==0.54.0"

PG_READY_TIMEOUT = 90.0
API_READY_TIMEOUT = 120.0
WORKER_READY_TIMEOUT = 90.0
STOP_TERM_TIMEOUT = 30.0
PORT_BIND_ATTEMPTS = 3

READ_CAP = "fixture.read"
PRIV_CAP = "fixture.priv"
HANDLER_MODULE_NAME = "act_fixture_handlers"
SESSION_COOKIE = "mai_session"
CSRF_COOKIE = "mai_csrf"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NO_STATE = 2


# ---------------------------------------------------------------------------
# small process / io helpers
# ---------------------------------------------------------------------------


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 120.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"command failed ({proc.returncode}): {argv}\n{detail}")
    return proc


def _docker(*args: str, timeout: float = 120.0, check: bool = True) -> str:
    return _run(["docker", *args], timeout=timeout, check=check).stdout.strip()


def _backend_python() -> list[str]:
    """Backend interpreter: the warmed project venv, else ``uv run``."""
    venv_python = BACKEND_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return [str(venv_python)]
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if not Path(uv).exists():
        raise RuntimeError(
            "no backend venv and no uv on PATH; run `uv sync` in backend/ first"
        )
    return [uv, "run", "--project", str(BACKEND_ROOT), "--frozen", "python"]


def _ensure_uvicorn() -> None:
    """Preflight the API server dependency (Dockerfile line 22 parity)."""
    probe = subprocess.run(
        [*_backend_python(), "-c", "import uvicorn"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        return
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if not Path(uv).exists():
        raise RuntimeError(
            f"backend python lacks uvicorn and uv is unavailable; install {UVICORN_PIN}"
        )
    venv_python = BACKEND_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        _run([uv, "pip", "install", "--python", str(venv_python), UVICORN_PIN])
    else:
        _run([uv, "pip", "install", UVICORN_PIN], cwd=BACKEND_ROOT)


def _probe_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _wait_until(predicate, timeout: float, what: str, interval: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise RuntimeError(f"timed out after {timeout:.0f}s waiting for {what}")


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    return Path(f"/proc/{pid}").exists()


def _http(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """One real HTTP request; returns (status, raw header pairs, body)."""
    payload = json.dumps(body).encode() if body is not None else None
    flat = dict(headers or {})
    if payload is not None:
        flat["content-type"] = "application/json"
    request = urllib.request.Request(url, data=payload, method=method, headers=flat)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            pairs = [
                (key.lower(), value) for key, value in response.headers.items()
            ]
            return response.status, pairs, response.read()
    except urllib.error.HTTPError as exc:
        pairs = [(key.lower(), value) for key, value in exc.headers.items()]
        return exc.code, pairs, exc.read()


def _set_cookie_jar(header_pairs: list[tuple[str, str]]) -> dict[str, str]:
    jar: dict[str, str] = {}
    for name, value in header_pairs:
        if name != "set-cookie":
            continue
        pair = value.split(";", 1)[0]
        if "=" in pair:
            cookie_name, _, cookie_value = pair.partition("=")
            jar[cookie_name.strip()] = cookie_value.strip()
    return jar


def source_identity() -> dict[str, Any]:
    """Exact commit/tree of the tree this fixture was launched from."""
    identity: dict[str, Any] = {"commit": None, "tree": None, "dirty": None}
    try:
        identity["commit"] = _run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, timeout=15
        ).stdout.strip()
        identity["tree"] = _run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=REPO_ROOT, timeout=15
        ).stdout.strip()
        status = _run(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT, timeout=15
        ).stdout.strip()
        identity["dirty"] = bool(status)
    except (OSError, RuntimeError):
        pass
    return identity


# ---------------------------------------------------------------------------
# state file (the fixture's ownership record)
# ---------------------------------------------------------------------------


def _load_state(state_file: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(state_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(document, dict) or document.get("schema") != STATE_SCHEMA:
        raise RuntimeError(f"unrecognized state file: {state_file}")
    return document


def _write_state(state_file: Path, state: dict[str, Any]) -> None:
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.chmod(state_file, 0o600)


def _db_url(state: dict[str, Any]) -> str:
    database = state["database"]
    return (
        f"postgresql://{database['user']}:{database['password']}"
        f"@127.0.0.1:{database['port']}/{database['name']}"
    )


# ---------------------------------------------------------------------------
# generated worker-side composition (written into the instance state dir)
# ---------------------------------------------------------------------------

HANDLER_MODULE_SOURCE = '''\
"""Worker-side composition for the act-fixture runtime: real
dispatcher/policy/approval/model-store machinery, journaled gate-aware
fixture effects, deterministic FunctionModel script (external model
fixtures prove plumbing only)."""

import asyncio
import json
import os
import time
import uuid

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import mai.agents.run as agent_run
import mai.agents.delegate as agent_delegate
from mai.agents.budgets import BudgetLedger
from mai.agents.model_store import ModelConfigStore
from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
from mai.credentials.env import EnvResolver
from mai.jobs.engine import EngineClient
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import Policy

READ_CAP = "fixture.read"
PRIV_CAP = "fixture.priv"


def _descriptor(capability_id, effect_class):
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
            "resource_scope": [] if effect_class == "read" else ["target.exec"],
            "required_grants": [],
        },
        source=f"<{capability_id} descriptor>",
    )


class FixtureExecutor:
    """Journaled capability executor.

    When a gate file ``<marker>.gate`` exists at begin time the executor
    blocks until ``<marker>.release`` appears (bounded): the deterministic
    crash window for the C4 interruption/restart scenario.
    """

    def __init__(self):
        self.journal = os.environ["MAI_FX_JOURNAL"]
        self.gate_dir = os.environ.get("MAI_FX_GATE_DIR", "")

    def _mark(self, marker):
        with open(self.journal, "a", encoding="utf-8") as handle:
            handle.write(marker + "\\n")

    async def __call__(self, call):
        capability_id = call.descriptor.identifier
        marker = call.payload.get("marker", "?")
        self._mark(f"begin:{capability_id}:{marker}")
        if self.gate_dir:
            gate = os.path.join(self.gate_dir, f"{marker}.gate")
            if os.path.exists(gate):
                release = os.path.join(self.gate_dir, f"{marker}.release")
                timeout = float(os.environ.get("MAI_FX_GATE_TIMEOUT", "150"))
                self._mark(f"gate_wait:{capability_id}:{marker}")
                deadline = time.monotonic() + timeout
                while not os.path.exists(release):
                    if time.monotonic() > deadline:
                        self._mark(f"gate_timeout:{capability_id}:{marker}")
                        break
                    await asyncio.sleep(0.25)
                self._mark(f"gate_released:{capability_id}:{marker}")
        self._mark(f"effect:{capability_id}:{marker}")
        document = {"status": "succeeded", "exit_code": 0, "stdout": "", "stderr": ""}
        return ExecutorResult(ok=True, output=json.dumps(document, sort_keys=True))


def _script_model():
    script = json.loads(os.environ["MAI_FX_MODEL_SCRIPT"])
    journal = os.environ["MAI_FX_JOURNAL"]

    def _mark(line):
        with open(journal, "a", encoding="utf-8") as handle:
            handle.write(line + "\\n")

    def _entry_payload(entry):
        if "tool_call" in entry:
            call = entry["tool_call"]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=call["name"],
                        args=call.get("args", {"marker": "m1"}),
                        tool_call_id=call.get("id", "c1"),
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content=entry.get("text", "done"))])

    def _model_fn(messages, info):
        _mark("model_call")
        index = (len(messages) - 1) // 2
        if index >= len(script):
            raise AssertionError("model script exhausted")
        return _entry_payload(script[index])

    return FunctionModel(_model_fn)


def _build():
    url = os.environ["MAI_DATABASE_URL"]
    app_engine = create_async_engine(url, poolclass=NullPool)
    sessions = async_sessionmaker(app_engine, expire_on_commit=False)
    with open(os.environ["MAI_FX_GRANTS"], encoding="utf-8") as handle:
        grants_doc = json.load(handle)
    with open(os.environ["MAI_FX_POLICY"], encoding="utf-8") as handle:
        policy_doc = json.load(handle)
    grants = {
        entry["capability_id"]: frozenset(uuid.UUID(v) for v in entry["server_ids"])
        for entry in grants_doc
    }
    registry = CapabilityRegistry()
    registry.register(_descriptor(READ_CAP, "read"))
    registry.register(_descriptor(PRIV_CAP, "privileged"))
    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(sessions),
        grants=lambda: grants,
        policy=lambda: Policy(
            revision=int(policy_doc["revision"]),
            denied_capabilities=frozenset(policy_doc.get("denied", [])),
        ),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(os.environ["MAI_FX_ENV"]),
        executor=FixtureExecutor(),
    )
    model_store = ModelConfigStore(os.environ["MAI_FX_MODEL_ROOT"])
    fake_model = _script_model()
    executor = agent_run.AgentRunExecutor(
        sessions=sessions,
        dispatcher=dispatcher,
        registry=registry,
        model_store=model_store,
        data_root=os.environ["MAI_FX_DATA_ROOT"],
        locks=ResourceLockService(url),
        resolver=EnvResolver(os.environ["MAI_FX_ENV"]),
        model_builder=lambda **kwargs: fake_model,
    )
    agent_run.configure_agent_run(executor)
    agent_delegate.configure_delegation(
        agent_delegate.DelegationExecutor(
            sessions=sessions,
            executor=executor,
            client=EngineClient(url),
            ledger=BudgetLedger(sessions),
        )
    )


_build()
'''

COMPOSE_MODEL_ROOT_SOURCE = '''\
"""Compose the worker-side deterministic model store (real store classes)."""

import json
import sys

from mai.agents.models import SCHEMA_VERSION as MODEL_SCHEMA_VERSION
from mai.agents.model_store import ModelConfigStore

root = sys.argv[1]
store = ModelConfigStore(root)
connection = {
    "schema_version": MODEL_SCHEMA_VERSION,
    "name": "gateway",
    "base_url": "http://127.0.0.1:9/v1",
    "timeout_seconds": 5,
    "capabilities": {"tool_calls": True},
}
store.write_connection("gateway", json.dumps(connection) + "\\n", expected_revision=0)
profile = {
    "schema_version": MODEL_SCHEMA_VERSION,
    "name": "fast",
    "connection": "gateway",
    "model": "fixture-model",
    "max_tokens": 256,
}
store.write_profile("fast", json.dumps(profile) + "\\n", expected_revision=0)
store.set_selection("gateway", "fast", expected_revision=0)
print("model-root ok")
'''

DEFAULT_MODEL_SCRIPT = [
    {"tool_call": {"name": "fixture_read", "args": {"marker": "m1"}, "id": "c1"}},
    {"text": "done"},
]


# ---------------------------------------------------------------------------
# spawn / teardown
# ---------------------------------------------------------------------------


def _spawn(
    argv: list[str], *, cwd: Path, env: dict[str, str], log_path: Path
) -> tuple[int, dict[str, Any]]:
    """Start one long-lived fixture process in its own session; record the
    exact launch command as the fixture's command interface."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")  # noqa: SIM115 - held for the process lifetime
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log.close()
    launch = {"argv": argv, "cwd": str(cwd), "env": env}
    return proc.pid, launch


def _write_launch_file(path: Path, launch: dict[str, Any]) -> None:
    path.write_text(json.dumps(launch, indent=2, sort_keys=True) + "\n")
    os.chmod(path, 0o600)


def _marked_pids(instance: str | None = None) -> list[int]:
    """Live processes carrying the fixture environment marker.

    With ``instance`` only that instance's processes; otherwise every
    process of ANY act-fixture instance (the whole owned namespace).
    """
    if instance:
        needle = f"{INSTANCE_ENV_VAR}={instance}".encode()
    else:
        needle = f"{INSTANCE_ENV_VAR}={MARKER}".encode()
    mine = os.getpid()
    found: list[int] = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == mine:
            continue
        try:
            raw = Path(f"/proc/{entry.name}/environ").read_bytes()
        except OSError:
            continue
        if needle in raw.split(b"\0"):
            found.append(int(entry.name))
    return sorted(found)


def _kill_group(pid: int) -> None:
    try:
        group = os.getpgid(pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + (
            STOP_TERM_TIMEOUT if sig == signal.SIGTERM else 10.0
        )
        while time.monotonic() < deadline:
            if not Path(f"/proc/{pid}").exists():
                return
            time.sleep(0.25)


def _fixture_containers(instance: str | None = None) -> list[str]:
    pattern = instance if instance else f"{MARKER}"
    out = _docker(
        "ps", "-aq", "--filter", f"name={pattern}", "--format", "{{.ID}}"
    )
    return [line for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def _wait_pg_ready(container: str, password: str) -> None:
    """Wait until the REAL postgres server accepts TCP connections.

    ``pg_isready`` alone races the official entrypoint: during initdb the
    temporary bootstrap server answers on the unix socket. The fixture
    consumers (migrations, API, worker) all dial TCP, so readiness is
    proven over exactly that path.
    """

    def ready() -> bool:
        proc = _run(
            [
                "docker",
                "exec",
                container,
                "psql",
                f"postgresql://mai:{password}@127.0.0.1:5432/mai",
                "-tAc",
                "SELECT 1",
            ],
            timeout=15,
            check=False,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "1"

    _wait_until(ready, PG_READY_TIMEOUT, "fixture postgres readiness (TCP)")


def _wait_api_ready(port: int) -> None:
    origin = f"http://127.0.0.1:{port}"

    def live() -> bool:
        try:
            status, _, _ = _http("GET", f"{origin}/api/v1/health", timeout=3)
        except (OSError, urllib.error.URLError):
            return False
        return status == 200

    _wait_until(live, API_READY_TIMEOUT, "fixture API liveness")

    def ready() -> bool:
        try:
            status, _, _ = _http("GET", f"{origin}/api/v1/health/ready", timeout=5)
        except (OSError, urllib.error.URLError):
            return False
        return status == 200

    _wait_until(ready, API_READY_TIMEOUT, "fixture API readiness (database probe)")


def _bootstrap_admin_and_server(state: dict[str, Any]) -> str:
    """Login through the real HTTP surface and mint the fixture server."""
    origin = state["api"]["origin"]
    password = state["database"]["password"]
    status, headers, body = _http(
        "POST",
        f"{origin}/api/v1/auth/login",
        body={"password": password},
        headers={"origin": origin},
    )
    if status != 204:
        raise RuntimeError(f"fixture login failed ({status}): {body[:200]!r}")
    jar = _set_cookie_jar(headers)
    if SESSION_COOKIE not in jar or CSRF_COOKIE not in jar:
        raise RuntimeError("fixture login did not issue session+CSRF cookies")
    status, _, body = _http(
        "POST",
        f"{origin}/api/v1/servers",
        body={
            "name": "act-fixture-target",
            "prompt_revision": 3,
            "environment": "linux_ssh",
            "ssh": {
                "host": "198.51.100.10",
                "port": 22,
                "user": "deploy",
                "host_key": "SHA256:6VhOyUC6fvXW0aQ9WNe7Kx3nZZf+zxgupEWLoZXoTMU",
                "credential_ref": "MAI_SSH_KEY_FIXTURE",
            },
            "privilege": "none",
            "capabilities": [READ_CAP],
        },
        headers={
            "origin": origin,
            "x-csrf-token": jar[CSRF_COOKIE],
            "cookie": f"{SESSION_COOKIE}={jar[SESSION_COOKIE]}; "
            f"{CSRF_COOKIE}={jar[CSRF_COOKIE]}",
        },
    )
    if status != 201:
        raise RuntimeError(f"fixture server creation failed ({status}): {body[:200]!r}")
    server_id = json.loads(body).get("server_id")
    if not server_id:
        raise RuntimeError("fixture server creation returned no server_id")
    return str(server_id)


def _wait_worker_ready(container: str) -> None:
    def schema_present() -> bool:
        proc = _run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                "mai",
                "-d",
                "mai",
                "-tAc",
                "SELECT to_regclass('dbos.workflow_status') IS NOT NULL",
            ],
            timeout=15,
            check=False,
        )
        return proc.stdout.strip().endswith("t")

    _wait_until(schema_present, WORKER_READY_TIMEOUT, "engine dbos schema creation")


def cmd_start(state_file: Path, json_output: bool) -> int:
    if _load_state(state_file) is not None:
        report = {
            "error": "state_exists",
            "detail": f"{state_file} exists; run `stop` or `reset` first",
        }
        print(json.dumps(report) if json_output else report["detail"], file=sys.stderr)
        return EXIT_FAIL

    _run(["docker", "info"], timeout=30)  # daemon preflight, fail closed
    _ensure_uvicorn()
    backend_python = _backend_python()

    instance = f"{MARKER}{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
    home = Path(tempfile.mkdtemp(prefix=instance))
    os.chmod(home, 0o700)
    for section in ("data-root", "artifacts", "handlers", "model-root", "gates", "logs"):
        (home / section).mkdir(parents=True, exist_ok=True)
        os.chmod(home / section, 0o700)

    (home / "handlers" / f"{HANDLER_MODULE_NAME}.py").write_text(
        HANDLER_MODULE_SOURCE, encoding="utf-8"
    )
    (home / "model-script.json").write_text(
        json.dumps(DEFAULT_MODEL_SCRIPT) + "\n", encoding="utf-8"
    )
    (home / "fixture.env").write_text(
        "MAI_SSH_KEY_FIXTURE=act-fixture-synthetic-credential-not-a-secret\n",
        encoding="utf-8",
    )
    os.chmod(home / "fixture.env", 0o600)

    spawned: list[tuple[int, str]] = []
    container: str | None = None
    state: dict[str, Any] | None = None
    try:
        # --- disposable Postgres (loopback ephemeral port, no named volume)
        password = secrets.token_hex(16)
        last_error: Exception | None = None
        for _ in range(PORT_BIND_ATTEMPTS):
            pg_port = _probe_free_port()
            container = instance
            try:
                _docker(
                    "run",
                    "-d",
                    "--name",
                    container,
                    "--label",
                    "owner=act-fixture",
                    "-e",
                    "POSTGRES_USER=mai",
                    "-e",
                    f"POSTGRES_PASSWORD={password}",
                    "-e",
                    "POSTGRES_DB=mai",
                    "-p",
                    f"127.0.0.1:{pg_port}:5432",
                    PG_IMAGE,
                    timeout=180,
                )
                break
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                last_error = exc
                container = None
        else:
            raise RuntimeError(f"could not bind a loopback port for postgres: {last_error}")
        assert container is not None
        _wait_pg_ready(container, password)
        db_url = f"postgresql://mai:{password}@127.0.0.1:{pg_port}/mai"

        # --- schema: real migrations from zero through head
        migration_env = dict(os.environ)
        migration_env["MAI_DATABASE_URL"] = db_url
        migration_argv = [
            *backend_python,
            "-m",
            "alembic",
            "upgrade",
            "head",
        ]
        try:
            _run(migration_argv, cwd=BACKEND_ROOT, env=migration_env, timeout=180)
        except RuntimeError:
            # one retry: a first connection can still lose a server that is
            # seconds past its init window on a loaded host
            time.sleep(5.0)
            _wait_pg_ready(container, password)
            _run(migration_argv, cwd=BACKEND_ROOT, env=migration_env, timeout=180)

        # --- real API process (production entrypoint: uvicorn --factory)
        api_port = _probe_free_port()
        origin = f"http://127.0.0.1:{api_port}"
        api_env = {
            **os.environ,
            "MAI_DATABASE_URL": db_url,
            "MAI_PUBLIC_ORIGINS": origin,
            "MAI_DATA_ROOT": str(home / "data-root"),
            "MAI_ARTIFACTS_ROOT": str(home / "artifacts"),
            "MAI_ADMIN_PASSWORD": password,
            INSTANCE_ENV_VAR: instance,
        }
        api_argv = [
            *backend_python,
            "-m",
            "uvicorn",
            "mai.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(api_port),
        ]
        api_pid, api_launch = _spawn(
            api_argv,
            cwd=BACKEND_ROOT,
            env=api_env,
            log_path=home / "logs" / "api.log",
        )
        spawned.append((api_pid, "api"))
        _write_launch_file(home / "launch-api.json", api_launch)
        _wait_api_ready(api_port)

        # --- real HTTP bootstrap: login + fixture server profile
        provisional: dict[str, Any] = {
            "api": {"origin": origin},
            "database": {"password": password},
        }
        server_id = _bootstrap_admin_and_server(provisional)

        # --- worker-side roots: deterministic model store + prompt + policy
        _run(
            [*backend_python, "-c", COMPOSE_MODEL_ROOT_SOURCE, str(home / "model-root")],
            cwd=BACKEND_ROOT,
            timeout=60,
        )
        prompt_dir = home / "data-root" / "servers" / server_id
        prompt_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(prompt_dir, 0o700)
        prompt_path = prompt_dir / "prompt.md"
        prompt_path.write_text("Answer briefly.\n", encoding="utf-8")
        os.chmod(prompt_path, 0o600)
        (home / "grants.json").write_text(
            json.dumps(
                [
                    {"capability_id": READ_CAP, "server_ids": [server_id]},
                    {"capability_id": PRIV_CAP, "server_ids": []},
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (home / "policy.json").write_text(
            json.dumps({"revision": 1, "denied": []}) + "\n", encoding="utf-8"
        )

        # --- real DBOS worker process (the ONLY full engine)
        worker_env = {
            **os.environ,
            "MAI_DATABASE_URL": db_url,
            "MAI_WORKER_HANDLERS": HANDLER_MODULE_NAME,
            "MAI_FX_JOURNAL": str(home / "journal.log"),
            "MAI_FX_GATE_DIR": str(home / "gates"),
            "MAI_FX_GRANTS": str(home / "grants.json"),
            "MAI_FX_POLICY": str(home / "policy.json"),
            "MAI_FX_ENV": str(home / "fixture.env"),
            "MAI_FX_MODEL_ROOT": str(home / "model-root"),
            "MAI_FX_DATA_ROOT": str(home / "data-root"),
            "MAI_FX_MODEL_SCRIPT": (home / "model-script.json").read_text(
                encoding="utf-8"
            ),
            "PYTHONPATH": str(home / "handlers"),
            INSTANCE_ENV_VAR: instance,
        }
        worker_argv = [*backend_python, "-m", "mai.jobs.worker", "--concurrency", "2"]
        worker_pid, worker_launch = _spawn(
            worker_argv,
            cwd=BACKEND_ROOT,
            env=worker_env,
            log_path=home / "logs" / "worker.log",
        )
        spawned.append((worker_pid, "worker"))
        _write_launch_file(home / "launch-worker.json", worker_launch)
        _wait_worker_ready(container)

        state = {
            "schema": STATE_SCHEMA,
            "instance": instance,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "home": str(home),
            "container": container,
            "database": {
                "port": pg_port,
                "user": "mai",
                "password": password,
                "name": "mai",
            },
            "api": {"port": api_port, "origin": origin, "pid": api_pid},
            "worker": {"pid": worker_pid},
            "server_id": server_id,
            "source": source_identity(),
            "launch": {
                "api": str(home / "launch-api.json"),
                "worker": str(home / "launch-worker.json"),
            },
        }
        _write_state(state_file, state)
    except Exception:
        # best-effort teardown so a failed start leaves no orphan behind
        for pid, _kind in reversed(spawned):
            try:
                _kill_group(pid)
            except OSError:
                pass
        for pid in _marked_pids(instance):
            try:
                _kill_group(pid)
            except OSError:
                pass
        if container:
            subprocess.run(
                ["docker", "rm", "-f", "-v", container],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        shutil.rmtree(home, ignore_errors=True)
        raise

    if json_output:
        # The machine contract for start/reset is the ownership state itself.
        print(json.dumps(state, indent=2, sort_keys=True))
    else:
        print(
            f"started {instance}\n"
            f"  api:    {state['api']['origin']} (pid {api_pid})\n"
            f"  worker: pid {worker_pid}\n"
            f"  state:  {state_file}"
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _collect_status(state: dict[str, Any]) -> dict[str, Any]:
    instance = state["instance"]
    container = state["container"]
    running = False
    pg_ready = False
    proc = _run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        timeout=15,
        check=False,
    )
    running = proc.stdout.strip() == "true"
    if running:
        proc = _run(
            ["docker", "exec", container, "pg_isready", "-U", "mai", "-d", "mai"],
            timeout=15,
            check=False,
        )
        pg_ready = proc.returncode == 0

    health_ok = False
    ready_ok = False
    health_error: str | None = None
    try:
        api_origin = state["api"]["origin"]
        status, _, _ = _http("GET", f"{api_origin}/api/v1/health", timeout=3)
        health_ok = status == 200
        status, _, _ = _http("GET", f"{api_origin}/api/v1/health/ready", timeout=5)
        ready_ok = status == 200
    except (OSError, urllib.error.URLError) as exc:
        health_error = type(exc).__name__

    dbos_schema = False
    proc = _run(
        [
            "docker", "exec", container, "psql", "-U", "mai", "-d", "mai",
            "-tAc", "SELECT to_regclass('dbos.workflow_status') IS NOT NULL",
        ],
        timeout=15,
        check=False,
    )
    dbos_schema = proc.stdout.strip().endswith("t")

    marked = _marked_pids(instance)
    api = state["api"]
    worker = state["worker"]
    api_alive = _pid_alive(api.get("pid")) or (
        api.get("pid") in marked if api.get("pid") else False
    )
    # A scenario may have replaced the worker through the recorded launch
    # interface; the new process is recognizable ONLY by its environment
    # marker, so any marked pid beyond the recorded api counts as the
    # worker being alive.
    replacement_pids = [pid for pid in marked if pid != api.get("pid")]
    worker_alive = _pid_alive(worker.get("pid")) or bool(replacement_pids)

    return {
        "instance": instance,
        "state_file_present": True,
        "container_running": running,
        "postgres_ready": pg_ready,
        "api": {
            "pid": api.get("pid"),
            "process_alive": api_alive,
            "health": health_ok,
            "ready": ready_ok,
            "health_error": health_error,
            "origin": api["origin"],
        },
        "worker": {
            "pid": worker.get("pid"),
            "process_alive": worker_alive,
            "marked_processes": marked,
            "replacement_pids": replacement_pids,
            "dbos_schema": dbos_schema,
        },
        "source": state.get("source"),
    }


def cmd_status(state_file: Path, json_output: bool) -> int:
    state = _load_state(state_file)
    if state is None:
        report = {"error": "no_state", "detail": f"{state_file} does not exist"}
        print(json.dumps(report) if json_output else report["detail"], file=sys.stderr)
        return EXIT_NO_STATE
    report = _collect_status(state)
    healthy = (
        report["container_running"]
        and report["postgres_ready"]
        and report["api"]["process_alive"]
        and report["api"]["health"]
        and report["api"]["ready"]
        and report["worker"]["process_alive"]
        and report["worker"]["dbos_schema"]
    )
    report["healthy"] = bool(healthy)
    if json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        verdict = "healthy" if healthy else "DEGRADED"
        print(f"{report['instance']}: {verdict}")
        for key in ("container_running", "postgres_ready"):
            print(f"  {key}: {report[key]}")
        print(f"  api health/ready: {report['api']['health']}/{report['api']['ready']}")
        print(f"  worker alive/dbos: {report['worker']['process_alive']}/{report['worker']['dbos_schema']}")
    return EXIT_OK if healthy else EXIT_FAIL


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def cmd_stop(state_file: Path, json_output: bool) -> int:
    state = _load_state(state_file)
    instance = state["instance"] if state else None
    container = state["container"] if state else None
    home = Path(state["home"]) if state else None
    stopped: dict[str, Any] = {}

    # 1. processes: recorded pids first, then the environment-marker sweep
    # (covers a worker restarted by a scenario, whose new pid the state
    # file never recorded).
    killed: list[int] = []
    if state:
        for pid in (state["worker"].get("pid"), state["api"].get("pid")):
            if pid and _pid_alive(pid) and pid not in killed:
                _kill_group(pid)
                killed.append(pid)
    for pid in _marked_pids(instance):
        if pid not in killed:
            _kill_group(pid)
            killed.append(pid)
    stopped["processes_killed"] = killed
    remaining = _marked_pids(instance)
    stopped["orphan_processes"] = remaining

    # 2. container(s) of this instance, then verify the whole owned namespace
    if container:
        _docker("rm", "-f", "-v", container, timeout=60, check=False)
    namespace_containers = _fixture_containers()
    for stray in namespace_containers:
        _docker("rm", "-f", "-v", stray, timeout=60, check=False)
    stopped["containers_removed"] = ([container] if container else []) + namespace_containers
    stopped["leftover_containers"] = _fixture_containers()

    # 3. ports: recorded bindings must refuse connections now
    freed: dict[str, bool] = {}
    if state:
        freed[f"pg/{state['database']['port']}"] = not _port_in_use(
            state["database"]["port"]
        )
        freed[f"api/{state['api']['port']}"] = not _port_in_use(state["api"]["port"])
    stopped["ports_free"] = freed

    # 4. disposable state
    if home is not None:
        subprocess.run(
            ["rm", "-rf", "--", str(home)], timeout=60, check=False
        )
        stopped["state_dir_removed"] = not home.exists()
    removed_state = False
    if state is not None:
        state_file.unlink(missing_ok=True)
        removed_state = not state_file.exists()
    stopped["state_file_removed"] = removed_state

    leftovers = (
        stopped["orphan_processes"]
        + stopped["leftover_containers"]
        + [name for name, free in freed.items() if not free]
    )
    stopped["clean"] = not leftovers
    stopped["leftovers"] = leftovers
    if json_output:
        print(json.dumps(stopped, indent=2, sort_keys=True))
    else:
        if state is None:
            print("no state file; namespace sweep performed")
        print(
            "stopped cleanly" if stopped["clean"] else f"LEFTOVERS: {leftovers}"
        )
    return EXIT_OK if stopped["clean"] else EXIT_FAIL


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_runtime_fixture.py",
        description=(
            "Isolated app-under-test lifecycle (real API + DBOS worker + "
            "disposable Postgres) owning ONLY act-fixture-marked resources. "
            "Never touches machina-postgres-1, mai-wire-test-pg or any "
            "non-fixture container, database or port."
        ),
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(
            os.environ.get("MAI_FIXTURE_STATE_FILE", str(DEFAULT_STATE_FILE))
        ),
        help="ownership state file (default: %(default)s)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("start", "start one isolated fixture instance (postgres+api+worker)"),
        ("status", "report live resource status; exit 1 when degraded"),
        ("reset", "stop the current instance and start a fresh one"),
        ("stop", "stop everything and verify zero orphans"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "--json", action="store_true", help="print the full JSON report"
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_file: Path = args.state_file
    if args.command == "start":
        return cmd_start(state_file, args.json)
    if args.command == "status":
        return cmd_status(state_file, args.json)
    if args.command == "stop":
        return cmd_stop(state_file, args.json)
    if args.command == "reset":
        if _load_state(state_file) is not None:
            code = cmd_stop(state_file, args.json)
            if code != EXIT_OK:
                return code
        return cmd_start(state_file, args.json)
    raise AssertionError(f"unhandled command {args.command}")  # pragma: no cover


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
