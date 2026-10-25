"""Cross-domain acceptance + bounded live-provider trials (23-evals).

Drives the act_RUNTIME.md scenario registry (``agent_campaign_cases.json``)
through the REAL isolated stack: a disposable marked ``pgvector`` container,
the real linear Alembic chain, the real ASGI app (``create_app`` + lifespan +
middleware + handlers, the proven 06/08 shape), real DBOS worker subprocesses
and the real stores. Evidence is written OUTSIDE the repo under
``MAI_ACT_EVIDENCE_DIR``.

Fixture discipline (C06 rule 6, the V2/V4 seam-proof pattern): the ONLY
substitution is the model — the harness's OWN deterministic scripted-model
workers. The ``00-runtime`` fixture's scripted model cannot produce a
structured ``TurnInterpretation`` (it fails closed at the intake seam), so
this harness composes its own worker-side handler: a sequential scripted
model whose entries emit contract-shaped ``final_result`` output-tool calls
(intake) plus executor tool calls, a journaled effect executor with
deterministic behaviors, and a ``needs``-gated context file so multi-turn
scenarios can reference identities that only exist after the previous HTTP
submission. Every scripted result is labelled scripted — never live.

Live-provider legs: the ratified budget (60 requests / 30 minutes / USD 5,
first bound wins) bounds every live leg. The authorized endpoint is the
LOCAL LiteLLM proxy (``litellm:4000`` on the compose infra network). The
production credential resolves only through the EnvResolver, so a live leg
runs INSIDE a DISPOSABLE marked container on that network: the credential
env-file is bind-mounted read-only at ``/run/secrets/mai-credentials.env``
and resolved through the production ``ModelConfigStore``/``EnvResolver``
seam — the value is never read by the harness, never printed, never
written to any file. The live model drives the REAL intake seam (the
``final_result``/``TurnInterpretation`` output-tool contract via
``mai.agents.intake.ainterpret_turn``), never a raw provider call. Legs
outside the ratified subset (or a leg whose plumbing cannot run) are
reported not_measured with that exact reason — never as a pass; a live
model failing a judged observable is a REPORTED provider-quality failure.

No destructive replay: only disposable fixture resources marked
``act-evals-``; no production DB, no ``machina-postgres-1``, no
``mai-wire-test-pg``, no running compose container is touched.

Acceptance semantics: every grader is deterministic and judges the
OUTCOME/CONSTRAINT a user would judge. No grader inspects an exact
tool-call sequence (X1 demonstrates this positively: repeats with
different markers grade identically). Deterministic failures and actual
provider quality are reported in separate buckets by
``scripts/agent_campaign_scorecard.py``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from tests import test_conversation_http as H  # noqa: E402 (backend harness)

CASES_PATH = REPO_ROOT / "tests" / "runtime" / "agent_campaign_cases.json"
DEFAULT_EVIDENCE_DIR = Path(
    "/home/megastruktur/projects/dev/machina/plans/agent-continuity/"
    "act_evidence/23-evals"
)
EVIDENCE_DIR = Path(
    os.environ.get("MAI_ACT_EVIDENCE_DIR", str(DEFAULT_EVIDENCE_DIR))
)
FIXTURE_ROOT = Path(
    os.environ.get("MAI_ACT_EVAL_ROOT", "/tmp/act-evals-23")
)

PG_IMAGE = "pgvector/pgvector:0.8.1-pg18"
CONTAINER_MARK = "act-evals-"
PG_USER = "mai"
PG_PASSWORD = "act-evals-disposable-only"
PG_DB = "mai"

ORIGIN = H.ORIGIN
SESSION_COOKIE = H.SESSION_COOKIE
CSRF_COOKIE = H.CSRF_COOKIE

TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
RUN_TIMEOUT = 90.0
WORKER_STOP_TIMEOUT = 30.0
CONTEXT_WAIT = 90.0

CASES: dict[str, dict[str, Any]] = {
    case["id"]: case
    for case in json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
}
RECORDS: dict[str, dict[str, Any]] = {}

#: run_id -> conversation_id for every run this harness submitted. The
#: teardown drain uses it so a test that aborts mid-run never leaves a
#: pending workflow behind: the NEXT test's worker would otherwise recover
#: that stale workflow and consume THIS worker's script entries (the
#: cross-test desync half of the scripted-entry defect class).
_SUBMITTED_RUNS: dict[uuid.UUID, uuid.UUID] = {}

#: every scenario directory created this session; journal-tail forensics
#: archive these on test failure (the repair-extra-entry evidence chain
#: must not rest on transient logs alone).
_ALL_SCENARIO_DIRS: list[Path] = []

_DRAIN_WAIT_SECONDS = 45.0
_DRAIN_CANCEL_WAIT_SECONDS = 20.0


# ---------------------------------------------------------------------------
# persistent event loop (proven 06 shape): sync tests drive the async app
# ---------------------------------------------------------------------------

_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_LOCK = threading.Lock()


def _run(coro: Any) -> Any:
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            _LOOP = asyncio.new_event_loop()
            thread = threading.Thread(target=_LOOP.run_forever, daemon=True)
            thread.start()
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result()


# ---------------------------------------------------------------------------
# disposable container (removed afterward — cleanup is asserted)
# ---------------------------------------------------------------------------


def _docker(argv: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *argv], capture_output=True, text=True, timeout=timeout, check=False
    )


def _container_name() -> str:
    return f"{CONTAINER_MARK}{uuid.uuid4().hex[:12]}"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_pg_ready(container: str, url: str, timeout: float = 120.0) -> None:
    """Wait for REAL host-side connectivity; the postgres image restarts
    the server during first-boot init, so require two stable probes."""
    import psycopg

    deadline = time.monotonic() + timeout
    stable = 0
    while time.monotonic() < deadline:
        proc = _docker(
            ["exec", container, "pg_isready", "-U", PG_USER, "-d", PG_DB],
            timeout=30,
        )
        if proc.returncode == 0:
            try:
                with psycopg.connect(url, connect_timeout=5) as conn:
                    conn.execute("SELECT 1").fetchone()
                stable += 1
                if stable >= 2:
                    time.sleep(1.0)
                    return
            except psycopg.OperationalError:
                stable = 0
        else:
            stable = 0
        time.sleep(1.0)
    raise AssertionError(f"fixture postgres never became ready: {proc.stderr}")


def _marked_containers() -> list[str]:
    proc = _docker(
        ["ps", "-a", "--filter", f"name={CONTAINER_MARK}", "--format", "{{.Names}}"]
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _source_identity() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
        "dirty": bool(git("status", "--porcelain").strip()),
        "branch": git("branch", "--show-current"),
    }


@pytest.fixture(scope="session")
def eval_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Disposable pgvector container, migrated through head, then REMOVED."""
    container = _container_name()
    port = _free_port()
    proc = _docker(
        [
            "run",
            "-d",
            "--name",
            container,
            "-e",
            f"POSTGRES_USER={PG_USER}",
            "-e",
            f"POSTGRES_PASSWORD={PG_PASSWORD}",
            "-e",
            f"POSTGRES_DB={PG_DB}",
            "-p",
            f"127.0.0.1:{port}:5432",
            PG_IMAGE,
        ]
    )
    assert proc.returncode == 0, proc.stderr
    url = f"postgresql://{PG_USER}:{PG_PASSWORD}@127.0.0.1:{port}/{PG_DB}"
    _wait_pg_ready(container, url)
    try:
        previous = os.environ.get("MAI_DATABASE_URL")
        os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
        try:
            H._drop_app_tables(url)
            from alembic import command

            command.upgrade(H._alembic_config(), "head")
        finally:
            if previous is None:
                os.environ.pop("MAI_DATABASE_URL", None)
            else:
                os.environ["MAI_DATABASE_URL"] = previous
        from mai.jobs import engine as jobs_engine

        jobs_engine.launch_worker(url)
        jobs_engine.shutdown()
    except Exception:
        _docker(["rm", "-f", container])
        raise
    yield url
    # -- teardown: remove the container and PROVE removal -------------------
    _docker(["rm", "-f", container])
    proof = {
        "container": container,
        "image": PG_IMAGE,
        "leftover_containers": _marked_containers(),
        "removed": _docker(["inspect", container]).returncode != 0,
        "source": _source_identity(),
    }
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "cleanup.json").write_text(
        json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert proof["removed"] is True, proof
    assert proof["leftover_containers"] == [], proof


@pytest.fixture(scope="session")
def harness(eval_url: str, tmp_path_factory: pytest.TempPathFactory):
    """One real ASGI app on the fixture DB plus the worker-side fixture
    files (grants/policy/env/model root) shared by all scenario workers."""
    root = tmp_path_factory.mktemp("act-evals-root")
    data_root = root / "data-root"
    data_root.mkdir(exist_ok=True)
    H._write_model_root(data_root)

    app = H._build_app(eval_url, data_root)
    lifecycle_cm = H._running(app)
    _run(lifecycle_cm.__aenter__())
    try:
        jar = _run(H.login_session(app))
        server_id = _create_server(app, jar, "act-evals-fixture-target")
        env = _Harness(
            root=root,
            app=app,
            jar=jar,
            url=eval_url,
            server_id=server_id,
            data_root=data_root,
        )
        _write_worker_files(env)
        yield env
    finally:
        _stop_all_workers()
        _run(lifecycle_cm.__aexit__(None, None, None))
        if getattr(app.state, "engine", None) is not None:
            _run(app.state.engine.dispose())


@dataclass
class _Harness:
    root: Path
    app: Any
    jar: dict[str, str]
    url: str
    server_id: uuid.UUID
    data_root: Path


def _create_server(app: Any, jar: dict[str, str], name: str) -> uuid.UUID:
    """The fixture target through the REAL server surface, with both fixture
    capabilities enabled so the modern submit resolves their grants."""
    body = {
        "name": name,
        "prompt_revision": 7,
        "environment": "linux_ssh",
        "ssh": {
            "host": "198.51.100.10",
            "port": 22,
            "user": "deploy",
            "host_key": "SHA256:6VhOyUC6fvXW0aQ9WNe7Kx3nZZf+zxgupEWLoZXoTMU",
            "credential_ref": "MAI_SSH_KEY_FIXTURE",
        },
        "privilege": "none",
        "capabilities": ["fixture.read", "fixture.priv"],
    }
    response = _run(
        H.call_app(
            app,
            "POST",
            "/api/v1/servers",
            json_body=body,
            headers=H._mutation_headers(jar),
            cookies=jar,
        )
    )
    assert response.status == 201, response.body
    return uuid.UUID(response.json()["server_id"])


def _write_worker_files(env: _Harness) -> None:
    """Worker-side grants/policy/env/model-root/data-root shared by all
    scenario workers (one canonical server profile on the data root)."""
    (env.root / "grants.json").write_text(
        json.dumps(
            [
                {"capability_id": "fixture.read", "server_ids": [str(env.server_id)]},
                {"capability_id": "fixture.priv", "server_ids": [str(env.server_id)]},
            ]
        ),
        encoding="utf-8",
    )
    (env.root / "policy.json").write_text(
        json.dumps({"revision": 1, "denied": []}), encoding="utf-8"
    )
    (env.root / "fixture.env").write_text(
        "MAI_SSH_KEY_FIXTURE=act-evals-synthetic-key-not-a-secret\n"
        f"MAI_FXT_TOKEN={CANARY}\n",
        encoding="utf-8",
    )
    os.chmod(env.root / "fixture.env", 0o600)
    H._write_model_root(env.root / "model-root")
    _write_server_profile(env.root / "data-root", env.server_id)
    handlers = env.root / "handlers"
    handlers.mkdir(exist_ok=True)
    (handlers / f"{HANDLER_MODULE_NAME}.py").write_text(
        HANDLER_MODULE_SOURCE, encoding="utf-8"
    )


def _write_server_profile(root: Path, server_id: uuid.UUID) -> None:
    """The canonical server profile the worker-side context assembly reads."""
    import os as _os

    from mai.contracts.server import SCHEMA_VERSION as SERVER_SCHEMA_VERSION
    from mai.profiles.files import ProfileFileError, ProfileFileStore

    store = ProfileFileStore(root)
    profile_text = (
        f"schema_version: {SERVER_SCHEMA_VERSION}\n"
        f'server_id: "{server_id}"\n'
        'name: "act-evals-fixture-target"\n'
        "prompt_revision: 7\n"
        "environment: linux_ssh\n"
        "ssh:\n"
        '  host: "198.51.100.10"\n'
        "  port: 22\n"
        '  user: "deploy"\n'
        '  host_key: "SHA256:6VhOyUC6fvXW0aQ9WNe7Kx3nZZf+zxgupEWLoZXoTMU"\n'
        '  credential_ref: "MAI_SSH_KEY_FIXTURE"\n'
        "privilege: none\n"
        "capabilities:\n"
        "  - fixture.read\n"
        "  - fixture.priv\n"
    )
    try:
        store.write(server_id, profile_text, expected_revision=0)
    except ProfileFileError as error:
        # The real servers API already persisted the canonical profile on
        # this data root (same root serves the app under test) — keep it.
        if "stale_revision" not in str(error):
            raise
    server_dir = root / "servers" / str(server_id)
    server_dir.mkdir(parents=True, exist_ok=True)
    server_dir.chmod(0o700)
    prompt_path = server_dir / "prompt.md"
    prompt_path.write_text(
        "You administer the act-evals fixture target. Keep changes announced.\n",
        encoding="utf-8",
    )
    _os.chmod(prompt_path, 0o600)


CANARY = "fxt-canary-3f9d1b-synthetic-credential-value"


# ---------------------------------------------------------------------------
# the harness's OWN scripted-model worker handler (V2/V4 seam-proof pattern)
# ---------------------------------------------------------------------------
#
# SCRIPTED-MODEL SCRIPT SIZING CONTRACT (read before authoring a script):
#
# Every script lists each turn's entries exactly ONCE. The model function
# RESERVES for the intake bounded-repair attempt automatically: intake runs
# with ``retries=0`` and ``MAX_INTAKE_REPAIR_ATTEMPTS=1`` (intake.py), so a
# rejected interpretation triggers at most ONE repair call, which is a fresh
# prompt carrying the ``# REPAIR REQUIRED`` banner. The scripted model
# detects that banner (and pydantic-ai ``retry-prompt`` parts on executor
# turns) and RE-SERVES the last consumed entry WITHOUT advancing the script
# position — callers (00-runtime, 12, 13 scripted-fixture consumers) may
# rely on: N planned calls consume exactly the first N entries; repair and
# retry attempts are position-neutral. An UNPLANNED call (e.g. a recovered
# stale workflow landing on this worker) can no longer desync every later
# position: instead of raising ``AssertionError('model script exhausted')``
# it serves a benign text spare and journals a ``script_spare:`` marker so
# the row fails honestly with a named cause instead of poisoning the whole
# worker script.
#
# NOTE: this contract lives OUTSIDE HANDLER_MODULE_SOURCE on purpose — the
# literal is Python SOURCE executed by the worker; prose inside it is a
# SyntaxError that only surfaces on a real worker (~90 s per row).

HANDLER_MODULE_NAME = "act_eval_handlers"

HANDLER_MODULE_SOURCE = '''\
"""Worker-side composition for 23-evals: the REAL registry, dispatcher,
approvals, policy, locks, model store and durable agent-run workflow run in
a REAL worker subprocess. Fixture substitutions only: a sequential
scripted model (intake ``final_result`` output-tool calls + executor tool
calls, with ``$key`` template resolution against a context file gated by
``needs``) and a journaled effect executor with deterministic behaviors."""

import json
import os
import time
import uuid

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import mai.agents.run as agent_run
from mai.agents.model_store import ModelConfigStore
from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
from mai.credentials.env import EnvResolver
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import Policy


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

    Behaviors (``MAI_FX_BEHAVIOR`` JSON keyed ``capability:marker`` then
    ``capability``): ``ok`` (default succeeded document), ``gate`` (park
    after ``begin`` until ``<gate dir>/gate-<capability>-<marker>`` exists),
    ``unknown_status`` (terminal-shaped ``outcome_unknown`` receipt), or a
    literal JSON object used as the output document verbatim.
    """

    def __init__(self):
        self.journal = os.environ["MAI_FX_JOURNAL"]
        self.gate_dir = os.environ.get("MAI_FX_GATE_DIR", "")
        self.behaviors = json.loads(os.environ.get("MAI_FX_BEHAVIOR", "{}"))

    def _mark(self, marker):
        with open(self.journal, "a", encoding="utf-8") as handle:
            handle.write(marker + "\\n")

    def _behavior(self, capability_id, marker):
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
            self._mark(f"gate_wait:{capability_id}:{marker}")
            while not os.path.exists(gate):
                if time.time() > deadline:
                    raise RuntimeError("gate timeout")
                time.sleep(0.1)
        self._mark(f"effect:{capability_id}:{marker}")
        if isinstance(behavior, dict):
            document = dict(behavior)
        elif behavior == "unknown_status":
            document = {
                "status": "outcome_unknown",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
            }
        else:
            document = {
                "status": "succeeded",
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
            }
        return ExecutorResult(
            ok=document.get("status") == "succeeded",
            output=json.dumps(document, sort_keys=True),
        )


def _context():
    path = os.environ["MAI_FX_CONTEXT"]
    deadline = time.time() + 120
    while not os.path.exists(path):
        if time.time() > deadline:
            raise RuntimeError("context file never appeared")
        time.sleep(0.1)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _resolve(value, context):
    if isinstance(value, str) and value.startswith("$"):
        key = value[1:]
        if key not in context:
            raise RuntimeError(f"unresolved context key {key!r}")
        return context[key]
    if isinstance(value, dict):
        return {k: _resolve(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, context) for v in value]
    return value


def _script_model():
    script = json.loads(os.environ["MAI_FX_MODEL_SCRIPT"])
    journal = os.environ["MAI_FX_JOURNAL"]
    state = {"index": 0}  # entries consumed by FRESH (non-reserved) calls
    served = {"intake": None, "exec": None}  # last consumed entry per shape

    def _mark(line):
        with open(journal, "a", encoding="utf-8") as handle:
            handle.write(line + "\\n")

    def _last_parts(messages):
        return list(getattr(messages[-1], "parts", ()) or ()) if messages else []

    def _is_repair(messages):
        """The bounded intake repair re-asks with the REPAIR REQUIRED banner."""
        return any(
            isinstance(getattr(part, "content", None), str)
            and "# REPAIR REQUIRED" in part.content
            for part in _last_parts(messages)
        )

    def _is_retry(messages):
        """pydantic-ai tool-validation retry round (executor turns)."""
        return any(
            getattr(part, "part_kind", None) == "retry-prompt"
            for part in _last_parts(messages)
        )

    def _spare(shape):
        _mark(f"script_spare:{shape}")
        # A spare consumed no entry: a repair/retry triggered by it must
        # NOT re-serve a stale earlier entry — keep it position-neutral.
        served[shape] = None
        return ModelResponse(parts=[TextPart(content="script spare (unplanned call)")])

    def _entry_payload(entry, context):
        needs = entry.get("needs") or []
        deadline = time.time() + 120
        while any(key not in context for key in needs):
            if time.time() > deadline:
                raise RuntimeError(f"context never provided {needs}")
            time.sleep(0.1)
            context = _context()
        if "final_result" in entry:
            payload = _resolve(entry["final_result"], context)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="final_result",
                        args={"response": payload},
                        tool_call_id=f"c{state['index']}",
                    )
                ]
            )
        if "tool_call" in entry:
            call = entry["tool_call"]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=call["name"],
                        args=call.get("args", {"marker": "m1"}),
                        tool_call_id=call.get("id", f"c{state['index']}"),
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content=entry.get("text", "done"))])

    def _model_fn(messages, info):
        _mark("model_call")
        names = [
            getattr(tool, "name", str(tool))
            for tool in (getattr(info, "function_tools", ()) or ())
        ]
        shape = "exec" if names else "intake"
        _mark("tools:" + ",".join(sorted(names)))
        _mark(
            "PROMPT\\t"
            + json.dumps(
                [
                    {
                        "kind": getattr(message, "kind", None),
                        "parts": [
                            {
                                "part_kind": getattr(part, "part_kind", None),
                                "tool_name": getattr(part, "tool_name", None),
                                "content": getattr(part, "content", None),
                            }
                            for part in getattr(message, "parts", ()) or ()
                        ],
                    }
                    for message in messages
                ],
                default=str,
            )
        )
        if _is_repair(messages) or _is_retry(messages):
            # RESERVED attempt (documented contract above): re-serve the
            # last consumed entry of this call shape WITHOUT advancing the
            # script position. A repair before any entry was consumed (or
            # after a spare) stays position-neutral and fails closed.
            last = served[shape]
            if last is not None:
                _mark(f"{'repair' if shape == 'intake' else 'retry'}_reserve:{shape}:{state['index'] - 1}")
                return _entry_payload(last, _context())
            return _spare(shape)
        index = state["index"]
        if index >= len(script):
            # An unplanned call must not desync later positions (and must
            # not kill the worker workflow with a bare AssertionError):
            # serve a benign spare, journal the anomaly, keep the position.
            return _spare(shape)
        entry = script[index]
        state["index"] = index + 1
        served[shape] = entry
        return _entry_payload(entry, _context())

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
    registry.register(_descriptor("fixture.read", "read"))
    registry.register(_descriptor("fixture.priv", "privileged"))
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
    agent_run.configure_agent_run(
        agent_run.AgentRunExecutor(
            sessions=sessions,
            dispatcher=dispatcher,
            registry=registry,
            model_store=model_store,
            data_root=os.environ["MAI_FX_DATA_ROOT"],
            locks=ResourceLockService(url),
            resolver=EnvResolver(os.environ["MAI_FX_ENV"]),
            model_builder=lambda **kwargs: fake_model,
        )
    )


_build()
'''

# Fail-fast guard (import time, zero cost): HANDLER_MODULE_SOURCE is Python
# SOURCE — any prose inside the literal is a SyntaxError that otherwise
# only surfaces on a real worker subprocess, ~90 s per wasted row.
import ast as _ast

_ast.parse(HANDLER_MODULE_SOURCE, mode="exec")


# ---------------------------------------------------------------------------
# scenario worker manager
# ---------------------------------------------------------------------------


_ACTIVE_WORKERS: list[subprocess.Popen] = []


def _stop_all_workers() -> None:
    for proc in _ACTIVE_WORKERS:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            try:
                proc.wait(timeout=WORKER_STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass


class Scenario:
    """One scripted-model worker scenario: own directory, own script,
    own journal, own worker subprocess (sequential script consumption)."""

    def __init__(self, env: _Harness, name: str, *, denied: list[str] | None = None):
        self.env = env
        self.name = name
        self.dir = env.root / "scenarios" / name
        self.dir.mkdir(parents=True, exist_ok=True)
        _ALL_SCENARIO_DIRS.append(self.dir)
        (self.dir / "gates").mkdir(exist_ok=True)
        self.journal_path = self.dir / "journal.log"
        self.context_path = self.dir / "context.json"
        self.context_path.write_text("{}", encoding="utf-8")
        grants = [
            {"capability_id": "fixture.read", "server_ids": [str(env.server_id)]},
            {"capability_id": "fixture.priv", "server_ids": [str(env.server_id)]},
        ]
        (self.dir / "grants.json").write_text(json.dumps(grants), encoding="utf-8")
        (self.dir / "policy.json").write_text(
            json.dumps({"revision": 1, "denied": denied or []}), encoding="utf-8"
        )
        (self.dir / "fixture.env").write_text(
            "MAI_SSH_KEY_FIXTURE=act-evals-synthetic-key-not-a-secret\n"
            f"MAI_FXT_TOKEN={CANARY}\n",
            encoding="utf-8",
        )
        os.chmod(self.dir / "fixture.env", 0o600)
        H._write_model_root(self.dir / "model-root")
        self.worker: subprocess.Popen | None = None

    def script(self, entries: list[dict[str, Any]]) -> None:
        (self.dir / "model-script.json").write_text(
            json.dumps(entries), encoding="utf-8"
        )

    def behavior(self, document: dict[str, Any]) -> None:
        (self.dir / "behavior.json").write_text(
            json.dumps(document), encoding="utf-8"
        )

    def context(self, **values: Any) -> None:
        document = json.loads(self.context_path.read_text(encoding="utf-8"))
        document.update(values)
        self.context_path.write_text(json.dumps(document), encoding="utf-8")

    def spawn(self) -> None:
        env = dict(os.environ)
        env.pop("MAI_TEST_DATABASE_URL", None)
        env["MAI_DATABASE_URL"] = self.env.url
        env["MAI_WORKER_HANDLERS"] = HANDLER_MODULE_NAME
        env["MAI_FX_JOURNAL"] = str(self.journal_path)
        env["MAI_FX_GATE_DIR"] = str(self.dir / "gates")
        env["MAI_FX_GRANTS"] = str(self.dir / "grants.json")
        env["MAI_FX_POLICY"] = str(self.dir / "policy.json")
        env["MAI_FX_ENV"] = str(self.dir / "fixture.env")
        env["MAI_FX_MODEL_ROOT"] = str(self.dir / "model-root")
        env["MAI_FX_DATA_ROOT"] = str(self.env.data_root)
        env["MAI_FX_CONTEXT"] = str(self.context_path)
        env["MAI_FX_MODEL_SCRIPT"] = (self.dir / "model-script.json").read_text(
            encoding="utf-8"
        )
        behavior = self.dir / "behavior.json"
        if behavior.exists():
            env["MAI_FX_BEHAVIOR"] = behavior.read_text(encoding="utf-8")
        else:
            env.pop("MAI_FX_BEHAVIOR", None)
        env["PYTHONPATH"] = str(self.env.root / "handlers")
        log = open(self.dir / "worker.log", "w")  # noqa: SIM115 - proc lifetime
        self.worker = subprocess.Popen(
            [sys.executable, "-m", "mai.jobs.worker", "--concurrency", "2"],
            cwd=str(BACKEND_ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        _ACTIVE_WORKERS.append(self.worker)

    def stop(self) -> None:
        if self.worker is None or self.worker.poll() is not None:
            return
        # Drain BEFORE stopping: every run this harness submitted must be
        # terminal while its OWN worker (own script, own context) is still
        # alive. An aborted test would otherwise leave a pending workflow
        # that the NEXT test's worker recovers and executes against the
        # wrong script — the cross-test desync channel.
        for run_id, conversation_id in list(_SUBMITTED_RUNS.items()):
            try:
                view = _run_view(self.env, run_id)
                if view.get("state") in TERMINAL_STATES:
                    continue
                deadline = time.monotonic() + _DRAIN_WAIT_SECONDS
                while time.monotonic() < deadline:
                    time.sleep(0.4)
                    view = _run_view(self.env, run_id)
                    if view.get("state") in TERMINAL_STATES:
                        break
                if view.get("state") not in TERMINAL_STATES:
                    # Parked-by-design runs (e.g. S2 reconciling): explicit
                    # tree cancellation, then a bounded wait; anything still
                    # non-terminal is disclosed in the journal, never silent.
                    status, body = _api(
                        self.env,
                        "POST",
                        f"/api/v1/chat/runs/{run_id}/cancel",
                        body={"cancel_children": True},
                        mutate=True,
                    )
                    deadline = time.monotonic() + _DRAIN_CANCEL_WAIT_SECONDS
                    while time.monotonic() < deadline:
                        time.sleep(0.4)
                        view = _run_view(self.env, run_id)
                        if view.get("state") in TERMINAL_STATES:
                            break
                final_state = view.get("state")
            except Exception as exc:  # noqa: BLE001 - drain is best-effort
                # A run can terminal BETWEEN the state read and the cancel
                # POST; the engine then answers with a recovery error
                # (run rows are history). The drain is teardown hygiene:
                # disclose and move on, never mask a graded verdict.
                self.journal_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.journal_path, "a", encoding="utf-8") as handle:
                    handle.write(
                        f"drain:error:{run_id}:{type(exc).__name__}\n"
                    )
                continue
            if final_state not in TERMINAL_STATES:
                self.journal_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.journal_path, "a", encoding="utf-8") as handle:
                    handle.write(
                        f"drain:run:{run_id}:cancel_status:{status}"
                        f":final_state:{final_state}\n"
                    )
        self.worker.send_signal(signal.SIGTERM)
        try:
            self.worker.wait(timeout=WORKER_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.worker.send_signal(signal.SIGKILL)
            self.worker.wait(timeout=WORKER_STOP_TIMEOUT)

    # -- journal / observables ---------------------------------------------
    def journal(self) -> list[str]:
        try:
            return self.journal_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def count(self, marker: str) -> int:
        return sum(1 for line in self.journal() if line == marker)

    def wait_journal(self, marker: str, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker in self.journal():
                return
            time.sleep(0.2)
        raise AssertionError(
            f"[{self.name}] journal never recorded {marker!r}; "
            f"tail: {self.journal()[-10:]}"
        )

    def prompts(self) -> list[list[dict[str, Any]]]:
        dumps = []
        for line in self.journal():
            if line.startswith("PROMPT\t"):
                dumps.append(json.loads(line[len("PROMPT\t") :]))
        return dumps

    def release_gate(self, capability: str, marker: str) -> None:
        (self.dir / "gates" / f"gate-{capability}-{marker}").write_text(
            "release\n", encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# HTTP surface helpers (sync wrappers over the real ASGI app)
# ---------------------------------------------------------------------------


def _api(
    env: _Harness,
    method: str,
    path: str,
    *,
    body: Any = None,
    jar: dict[str, str] | None = None,
    mutate: bool = False,
) -> tuple[int, Any]:
    headers = H._mutation_headers(env.jar) if mutate else {}
    response = _run(
        H.call_app(
            env.app,
            method,
            path,
            json_body=body,
            headers=headers,
            cookies=jar if jar is not None else env.jar,
        )
    )
    try:
        parsed = response.json()
    except ValueError:
        parsed = {"raw": response.body.decode("utf-8", "replace")}
    return response.status, parsed


def _submit_turn(
    env: _Harness,
    conversation_id: uuid.UUID | None,
    message: str,
    *,
    profile: str = "fast",
    requested_phase: str | None = None,
    task_action: str | None = None,
    jar: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {
        "server_id": str(env.server_id),
        "client_request_id": str(uuid.uuid4()),
        "message": message,
        "profile": profile,
    }
    if requested_phase is not None:
        body["requested_phase"] = requested_phase
    if task_action is not None:
        body["task_action"] = task_action
    path = (
        "/api/v1/chat/conversations"
        if conversation_id is None
        else f"/api/v1/chat/conversations/{conversation_id}/messages"
    )
    status, parsed = _api(env, "POST", path, body=body, mutate=True, jar=jar)
    if isinstance(parsed, dict):
        # echo the generated idempotency key onto the view for graders
        parsed["client_request_id"] = body["client_request_id"]
        run_id = parsed.get("run_id")
        if run_id:
            _SUBMITTED_RUNS[uuid.UUID(str(run_id))] = conversation_id
    return status, parsed


def _resubmit_turn(
    env: _Harness,
    conversation_id: uuid.UUID,
    crid: str,
    message: str,
    *,
    jar: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    body = {
        "server_id": str(env.server_id),
        "client_request_id": crid,
        "message": message,
        "profile": "fast",
    }
    status, parsed = _api(
        env,
        "POST",
        f"/api/v1/chat/conversations/{conversation_id}/messages",
        body=body,
        mutate=True,
        jar=jar,
    )
    if isinstance(parsed, dict) and parsed.get("run_id"):
        _SUBMITTED_RUNS[uuid.UUID(str(parsed["run_id"]))] = conversation_id
    return status, parsed


def _fresh_session(env: _Harness) -> dict[str, str]:
    """A SECOND authenticated session.

    ``H.login_session`` bootstraps the administrator credential, which is
    single-shot per app instance (a second bootstrap answers structured
    409). A fresh session is login-only against the existing credential.
    """
    login = _run(
        H.call_app(
            env.app,
            "POST",
            "/api/v1/auth/login",
            json_body={"password": H.FIXTURE_PASSWORD},
            headers={"origin": H.ORIGIN},
        )
    )
    assert login.status == 204, login.body
    jar = H._set_cookies(login)
    assert H.SESSION_COOKIE in jar and H.CSRF_COOKIE in jar
    return jar


def _conversation(env: _Harness, conversation_id: uuid.UUID, *, jar=None):
    return _api(
        env,
        "GET",
        f"/api/v1/chat/conversations/{conversation_id}?server_id={env.server_id}",
        jar=jar,
    )


def _messages(
    env: _Harness,
    conversation_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
    jar=None,
):
    return _api(
        env,
        "GET",
        f"/api/v1/chat/conversations/{conversation_id}/messages"
        f"?server_id={env.server_id}&limit={limit}&offset={offset}",
        jar=jar,
    )


def _run_view(env: _Harness, run_id: uuid.UUID) -> dict[str, Any]:
    status, view = _api(env, "GET", f"/api/v1/chat/runs/{run_id}")
    assert status == 200, view
    return view


def _await_terminal(
    env: _Harness, run_id: uuid.UUID, timeout: float, *, case_id: str | None = None
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    view: dict[str, Any] = {}
    while time.monotonic() < deadline:
        view = _run_view(env, run_id)
        if view.get("state") in TERMINAL_STATES:
            return view
        time.sleep(0.4)
    message = f"run {run_id} never reached terminal inside {timeout:.0f}s; last: {view}"
    if case_id is not None:
        # record BEFORE aborting so the row keeps its named cause (a red
        # row with a cause; never a blanked verdict)
        _check(case_id, False, message)
    raise AssertionError(message)


def _await_state(
    env: _Harness, run_id: uuid.UUID, target: str, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    view: dict[str, Any] = {}
    while time.monotonic() < deadline:
        view = _run_view(env, run_id)
        if view.get("state") == target:
            return view
        time.sleep(0.4)
    raise AssertionError(f"run {run_id} never reached {target!r}; last: {view}")


def _task_header(env: _Harness, conversation_id: uuid.UUID):
    """The compact task header (§8) for one conversation."""
    status, view = _api(
        env,
        "GET",
        f"/api/v1/chat/conversations/{conversation_id}/task"
        f"?server_id={env.server_id}",
    )
    assert status == 200, (status, view)
    return view


def _psql(url: str, sql: str) -> str:
    import psycopg

    with psycopg.connect(url) as conn:
        rows = conn.execute(sql).fetchall()
    return rows


def _psql_scalar(url: str, sql: str) -> Any:
    rows = _psql(url, sql)
    return None if not rows else rows[0][0]


# ---------------------------------------------------------------------------
# in-process store seam (real producers; deterministic seeds without workers)
# ---------------------------------------------------------------------------


class _SeedModel:
    """Minimal scripted intake model for in-process store-seam seeds."""

    def __init__(self, payload: dict[str, Any]):
        self._payload = payload
        self.calls = 0

    @property
    def model(self):  # noqa: ANN201 - FunctionModel
        from pydantic_ai.models.function import FunctionModel
        from pydantic_ai.messages import ModelResponse, ToolCallPart

        def _respond(messages, info):
            self.calls += 1
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="final_result",
                        args={"response": self._payload},
                        tool_call_id=f"s{self.calls}",
                    )
                ]
            )

        return FunctionModel(_respond)


def _seed_task_via_intake(env: _Harness, message_text: str) -> dict[str, Any]:
    """Create one conversation + user message + task through the REAL
    intake → validate → persist chain (in-process, no worker needed)."""
    from mai.agents.conversation_store import acreate_conversation, asubmit_turn
    from mai.agents.intake import arun_intake_turn

    factory = env.app.state.session_factory
    server_id = env.server_id

    async def _seed() -> dict[str, Any]:
        conversation = await acreate_conversation(factory, server_id=server_id)
        cid = conversation.conversation_id
        turn = await asubmit_turn(
            factory,
            conversation_id=cid,
            server_id=server_id,
            client_request_id=uuid.uuid4(),
            message=message_text,
        )
        message_id = turn.message.message_id
        payload = _plan_payload(
            message_id,
            objective=f"Eval seed task for {message_text[:40]}",
            constraints=(
                {
                    "constraint_id": "no_file_changes",
                    "category": "scope",
                    "statement": "Do not change any files on the target",
                    "origin": "user",
                    "source_message_refs": [str(message_id)],
                    "suggested": "add",
                },
            ),
            operations=(
                {"op": "add", "step": {"step_id": "prepare", "title": "Prepare"}},
                {"op": "add", "step": {"step_id": "apply", "title": "Apply"}},
            ),
        )
        outcome = await arun_intake_turn(
            _SeedModel(payload).model,
            factory,
            server_id=server_id,
            conversation_id=cid,
            source_message_id=message_id,
        )
        return {
            "conversation_id": cid,
            "message_id": message_id,
            "task_id": outcome.task_id,
            "revision": outcome.revision,
        }

    return _run(_seed())


def _plan_payload(
    source_message_id: uuid.UUID,
    *,
    objective: str = "provision the fixture service",
    constraints: tuple[dict, ...] = (),
    criteria: list[dict] | None = None,
    operations: tuple[dict, ...] = (),
    phase: str = "plan",
    assumptions: tuple[dict, ...] = (),
    questions: tuple[dict, ...] = (),
    scope_change: dict | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "source_message_id": str(source_message_id),
        "turn_kind": "plan",
        "understanding": "the user requests a plan-only task",
        "objective": objective,
        "acceptance_criteria": criteria
        if criteria is not None
        else [{"criterion_id": "done", "statement": "the objective is met"}],
        "constraints_proposed": list(constraints),
        "assumptions": list(assumptions),
        "open_questions": list(questions),
        "requested_phase": phase,
        "todo_patch": {
            "schema_version": 1,
            "source_message_id": str(source_message_id),
            "base_revision": 1,
            "idempotency_key": str(uuid.uuid4()),
            "operations": list(operations),
        },
    }
    if scope_change is not None:
        payload["scope_change_proposal"] = scope_change
    return payload


def _record(case_id: str, **fields: Any) -> None:
    record = RECORDS.setdefault(case_id, {"case": CASES[case_id], "checks": []})
    record.update(fields)


def _check(case_id: str, condition: bool, message: str) -> None:
    RECORDS.setdefault(case_id, {"case": CASES[case_id], "checks": []})[
        "checks"
    ].append({"ok": bool(condition), "check": message})
    assert condition, f"[{case_id}] {message}"


@pytest.fixture(scope="session", autouse=True)
def _persist_scenarios():
    yield
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    cases_out: dict[str, Any] = {}
    for case_id, record in RECORDS.items():
        measured, skip_reason = LIVE_FLAGS.get(
            case_id, (False, LIVE_SKIP_DEFAULT)
        )
        cases_out[case_id] = {
            **record,
            "live_measured": measured,
            "live_skip_reason": None if measured else skip_reason,
        }
    document = {
        "schema": "act-evals-scenarios/1",
        "mode": "scripted + budgeted live-provider legs (scripted legs run "
        "deterministic fixtures through the real app/worker/store and are "
        "labelled scripted, never live; live legs ran through the REAL "
        "intake seam against the local LiteLLM proxy inside a disposable "
        "marked container, bounded by the ratified budget)",
        "source": _source_identity(),
        "cases": cases_out,
        "provider_legs": PROVIDER_LEGS,
    }
    (EVIDENCE_DIR / "scenarios.json").write_text(
        json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _archive_journal_tails(
    harness: _Harness, test_name: str, scenario_dirs: list[Path]
) -> None:
    """Archive per-scenario worker journal + worker-log tails on failure.

    The forensic chain for unplanned extra model calls (the repair-entry
    defect class) rests on the journaled ``repair_reserve:`` /
    ``script_spare:`` markers — transient console logs are not evidence.
    """
    out_dir = EVIDENCE_DIR / "journal-tails" / test_name
    out_dir.mkdir(parents=True, exist_ok=True)
    for scenario_dir in scenario_dirs:
        for log_name, keep in (("journal.log", 400), ("worker.log", 200)):
            path = scenario_dir / log_name
            if not path.exists():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            (out_dir / f"{scenario_dir.name}-{log_name}").write_text(
                "\n".join(lines[-keep:]) + "\n", encoding="utf-8"
            )


def _journal_tails_on_failure(fn):
    """Test decorator: on ANY failure, archive the journal tails of the
    scenarios this test created, then re-raise (the row stays honestly red
    WITH its forensic evidence instead of blanking silently)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        before = set(_ALL_SCENARIO_DIRS)
        try:
            return fn(*args, **kwargs)
        except BaseException:
            harness = next(
                (a for a in (*args, *kwargs.values()) if isinstance(a, _Harness)),
                None,
            )
            if harness is not None:
                created = [d for d in _ALL_SCENARIO_DIRS if d not in before]
                _archive_journal_tails(harness, fn.__name__, created)
            raise

    return wrapper


# ---------------------------------------------------------------------------
# C1 / C2 / C3 — one continuity arc, DE-BATCHED (fix 5): each registry row
# owns its own test, so one row's abort can no longer blank its siblings'
# verdicts. Task identity is read from the POST-TERMINAL task header, never
# from the submit response (fix 1, defect class A: the submit contract
# carries task_id|null — the episode binds worker-side at intake).
# ---------------------------------------------------------------------------

CONSTRAINT_TEXT = "Do not change any files on the target"


def _continuity_script(
    *, turn2_continue: bool, fillers: int = 0
) -> list[dict[str, Any]]:
    """The shared continuity-arc script (C1/C2/C3 variants)."""
    script: list[dict[str, Any]] = [
        # turn 1: plan-only intake (needs msg1/crid1)
        {
            "needs": ["msg1", "crid1"],
            "final_result": {
                "schema_version": 1,
                "source_message_id": "$msg1",
                "turn_kind": "plan",
                "understanding": "plan-only inspection request",
                "objective": "Inspect the act-fixture service (plan only)",
                "acceptance_criteria": [
                    {
                        "criterion_id": "status_reported",
                        "statement": "the service status is reported",
                    }
                ],
                "constraints_proposed": [
                    {
                        "constraint_id": "no_file_changes",
                        "category": "scope",
                        "statement": CONSTRAINT_TEXT,
                        "origin": "user",
                        "source_message_refs": ["$msg1"],
                        "suggested": "add",
                    }
                ],
                "requested_phase": "plan",
                "todo_patch": {
                    "schema_version": 1,
                    "source_message_id": "$msg1",
                    "base_revision": 1,
                    "idempotency_key": "$crid1",
                    "operations": [
                        {
                            "op": "add",
                            "step": {
                                "step_id": "inspect_service",
                                "title": "Inspect the service",
                            },
                        }
                    ],
                },
            },
        },
        {"text": "plan recorded; no tools used"},
    ]
    if turn2_continue:
        script.extend(
            [
                # turn 2: continue (needs msg2/rev1)
                {
                    "needs": ["msg2", "rev1"],
                    "final_result": {
                        "schema_version": 1,
                        "source_message_id": "$msg2",
                        "turn_kind": "continue_task",
                        "understanding": "the user continues the current task",
                        "todo_patch": {
                            "schema_version": 1,
                            "source_message_id": "$msg2",
                            "base_revision": "$rev1",
                            "idempotency_key": "$crid2",
                            "operations": [
                                {
                                    "op": "add",
                                    "step": {
                                        "step_id": "report_status",
                                        "title": "Report the status",
                                    },
                                }
                            ],
                        },
                    },
                },
                {"text": "continued; still no tools used"},
            ]
        )
    if fillers:
        script.extend(
            [
                # fillers: compact answers (1 model call each). The
                # transcript window is COUNT-bounded (20 messages,
                # DEFAULT_TRANSCRIPT_LIMIT) — the arc must exceed it for
                # the omission marker to be the honest observable.
                *[
                    {
                        "needs": [f"msgf{i}"],
                        "final_result": {
                            "schema_version": 1,
                            "source_message_id": f"$msgf{i}",
                            "turn_kind": "answer",
                            "understanding": "filler acknowledgement",
                            "answer_text": f"acknowledged filler {i}",
                        },
                    }
                    for i in range(fillers)
                ],
                # turn after fillers: continue again (needs msg3/revf)
                {
                    "needs": ["msg3", "revf"],
                    "final_result": {
                        "schema_version": 1,
                        "source_message_id": "$msg3",
                        "turn_kind": "continue_task",
                        "understanding": "the user continues after a long transcript",
                        "todo_patch": {
                            "schema_version": 1,
                            "source_message_id": "$msg3",
                            "base_revision": "$revf",
                            "idempotency_key": "$crid3",
                            "operations": [
                                {
                                    "op": "update",
                                    "step": {
                                        "step_id": "report_status",
                                        "title": "Report the status",
                                        "status": "in_progress",
                                    },
                                }
                            ],
                        },
                    },
                },
                {"text": "final continue recorded"},
            ]
        )
    return script


@_journal_tails_on_failure
def test_c1_plan_and_continue_share_episode(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "c1")
    scenario.script(_continuity_script(turn2_continue=True, fillers=False))
    scenario.spawn()
    try:
        # -- turn 1 ---------------------------------------------------------
        turn1_text = (
            "Plan only; do not change any files. Inspect the act-fixture "
            "service and report its status."
        )
        status1, body1 = _submit_turn(
            env, None, turn1_text, requested_phase="plan"
        )
        _check("C1", status1 == 201, f"turn1 admitted (201): got {status1} {body1}")
        conversation_id = uuid.UUID(body1["conversation_id"])
        scenario.context(
            msg1=str(body1["message_id"]),
            crid1=body1["client_request_id"]
            if "client_request_id" in body1
            else str(uuid.uuid4()),
        )
        # Defect-class-A fix: task_id|null at submit is the CONTRACT (§3);
        # the episode binds worker-side at intake. The graded observable is
        # the post-terminal task header.
        _check(
            "C1",
            body1.get("task_id") is None,
            f"submit response carries task_id|null (contract §3): {body1.get('task_id')!r}",
        )
        run1 = uuid.UUID(body1["run_id"])
        view1 = _await_terminal(env, run1, RUN_TIMEOUT, case_id="C1")
        _check("C1", view1["state"] == "succeeded", f"turn1 run succeeded: {view1}")
        header1 = _task_header(env, conversation_id)
        _check(
            "C1",
            header1.get("task_id") is not None,
            f"task episode bound at intake (post-terminal header): {header1!r}",
        )
        task_id = uuid.UUID(str(header1["task_id"]))
        _check(
            "C1",
            scenario.count("effect:fixture.priv") == 0
            and scenario.count("effect:fixture.read") == 0,
            "plan-only turn produced zero privileged/read effect markers",
        )
        _check("C1", scenario.count("model_call") == 2, "turn1 made intake+final calls")

        # -- turn 2: continue -------------------------------------------------
        status2, body2 = _submit_turn(env, conversation_id, "continue")
        _check("C1", status2 == 201, f"turn2 admitted: {status2} {body2}")
        _check(
            "C1",
            body2["conversation_id"] == body1["conversation_id"],
            "same conversation identity across turns",
        )
        scenario.context(
            msg2=str(body2["message_id"]),
            crid2=body2.get("client_request_id", str(uuid.uuid4())),
        )
        header = _task_header(env, conversation_id)
        scenario.context(rev1=header["revision"])
        run2 = uuid.UUID(body2["run_id"])
        view2 = _await_terminal(env, run2, RUN_TIMEOUT, case_id="C1")
        _check("C1", view2["state"] == "succeeded", f"turn2 run succeeded: {view2}")
        header2 = _task_header(env, conversation_id)
        _check(
            "C1",
            header2.get("task_id") == str(task_id),
            "same task episode across turns (post-terminal header)",
        )
        _record(
            "C1",
            verdict="pass",
            conversation_id=str(conversation_id),
            task_id=str(task_id),
            runs=[str(run1), str(run2)],
        )
    finally:
        scenario.stop()


@_journal_tails_on_failure
def test_c2_fresh_session_reads_true_transcript(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "c2")
    scenario.script(_continuity_script(turn2_continue=False, fillers=False))
    scenario.spawn()
    try:
        turn1_text = (
            "Plan only; do not change any files. Inspect the act-fixture "
            "service and report its status."
        )
        status1, body1 = _submit_turn(
            env, None, turn1_text, requested_phase="plan"
        )
        assert status1 == 201, body1
        conversation_id = uuid.UUID(body1["conversation_id"])
        scenario.context(
            msg1=str(body1["message_id"]),
            crid1=body1.get("client_request_id", str(uuid.uuid4())),
        )
        view1 = _await_terminal(
            env, uuid.UUID(body1["run_id"]), RUN_TIMEOUT, case_id="C2"
        )
        _check("C2", view1["state"] == "succeeded", f"turn1 run succeeded: {view1}")

        # -- C2: fresh session reads the true transcript ---------------------
        fresh_jar = _fresh_session(env)
        status_c2, detail = _conversation(env, conversation_id, jar=fresh_jar)
        _check("C2", status_c2 == 200, "fresh session reads the conversation")
        _check(
            "C2",
            detail.get("history_completeness") == "complete",
            f"history completeness complete: {detail.get('history_completeness')}",
        )
        status_m2, page2 = _messages(env, conversation_id, jar=fresh_jar)
        _check("C2", status_m2 == 200 and page2["total"] >= 2, "transcript page served")
        roles = [row["role"] for row in page2["items"]]
        _check(
            "C2",
            "user" in roles and "assistant" in roles,
            f"true roles restored: {roles}",
        )
        bodies = [row.get("content") or row.get("body") or "" for row in page2["items"]]
        _check("C2", turn1_text in bodies, "original user text preserved verbatim")
        _record("C2", verdict="pass", fresh_session=True)
    finally:
        scenario.stop()


@_journal_tails_on_failure
def test_c3_long_transcript_bounded_window(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "c3")
    filler_marker = "FILLER-C3-unique-9f31b7"
    scenario.script(_continuity_script(turn2_continue=True, fillers=10))
    scenario.spawn()
    try:
        turn1_text = (
            "Plan only; do not change any files. Inspect the act-fixture "
            "service and report its status."
        )
        status1, body1 = _submit_turn(
            env, None, turn1_text, requested_phase="plan"
        )
        assert status1 == 201, body1
        conversation_id = uuid.UUID(body1["conversation_id"])
        scenario.context(
            msg1=str(body1["message_id"]),
            crid1=body1.get("client_request_id", str(uuid.uuid4())),
        )
        view1 = _await_terminal(
            env, uuid.UUID(body1["run_id"]), RUN_TIMEOUT, case_id="C3"
        )
        _check("C3", view1["state"] == "succeeded", f"turn1 run succeeded: {view1}")
        # Task identity from the post-terminal header (defect-class-A fix).
        task_id = uuid.UUID(str(_task_header(env, conversation_id)["task_id"]))

        # turn 2: continue (the full original arc shape)
        status2, body2 = _submit_turn(env, conversation_id, "continue")
        assert status2 == 201, body2
        scenario.context(
            msg2=str(body2["message_id"]),
            crid2=body2.get("client_request_id", str(uuid.uuid4())),
        )
        scenario.context(rev1=_task_header(env, conversation_id)["revision"])
        view2 = _await_terminal(
            env, uuid.UUID(body2["run_id"]), RUN_TIMEOUT, case_id="C3"
        )
        assert view2["state"] == "succeeded", view2

        # -- C3: fill the transcript past the 20-message window --------------
        total_before = _messages(env, conversation_id)[1]["total"]
        filler_msg_ids: list[str] = []
        for i in range(10):
            filler = (
                f"{filler_marker} {i}: " + "x" * 3400 + f" tail-{filler_marker}-{i}"
            )
            status_f, body_f = _submit_turn(env, conversation_id, filler)
            assert status_f == 201, body_f
            filler_msg_ids.append(str(body_f["message_id"]))
            scenario.context(**{f"msgf{i}": str(body_f["message_id"])})
            view_f = _await_terminal(
                env, uuid.UUID(body_f["run_id"]), RUN_TIMEOUT, case_id="C3"
            )
            assert view_f["state"] == "succeeded", view_f
        total_after = _messages(env, conversation_id)[1]["total"]
        _check(
            "C3",
            total_after == total_before + 20,
            f"no silent history drop: {total_before} -> {total_after}",
        )
        status_c3, body3 = _submit_turn(env, conversation_id, "continue")
        assert status_c3 == 201, body3
        scenario.context(
            msg3=str(body3["message_id"]),
            crid3=body3.get("client_request_id", str(uuid.uuid4())),
        )
        scenario.context(revf=_task_header(env, conversation_id)["revision"])
        run3 = uuid.UUID(body3["run_id"])
        _await_terminal(env, run3, RUN_TIMEOUT, case_id="C3")

        # constraint retained + explicit truncation
        status_cons, constraints = _api(
            env, "GET", f"/api/v1/chat/tasks/{task_id}/constraints"
            f"?server_id={env.server_id}"
        )
        statements = [
            row.get("statement", "") for row in constraints.get("items", [])
        ]
        _check(
            "C3",
            status_cons == 200 and CONSTRAINT_TEXT in statements,
            f"constraint retained after long transcript: {statements}",
        )
        from mai.agents.intake import aload_intake_snapshot

        async def _snapshot():
            return await aload_intake_snapshot(
                env.app.state.session_factory,
                server_id=env.server_id,
                conversation_id=conversation_id,
                source_message_id=uuid.UUID(str(body3["message_id"])),
            )

        snapshot = _run(_snapshot())
        _check(
            "C3",
            snapshot.transcript_omitted_older is True,
            "truncation is explicit: omitted-older marker set",
        )
        _check(
            "C3",
            len(snapshot.transcript) < total_after
            and snapshot.transcript[-1][1] == "continue",
            "bounded recent window ends at the interpreted turn",
        )
        last_prompt = json.dumps(scenario.prompts()[-1])
        _check(
            "C3",
            CONSTRAINT_TEXT in last_prompt,
            "assembled prompt carries the constraint despite truncation",
        )
        # The window keeps the LAST DEFAULT_TRANSCRIPT_LIMIT messages
        # (intake.py: the snapshot transcript is 1:1 with the store page,
        # oldest first), so the OLDEST filler rows legitimately drop out
        # of the assembled prompt while the most recent ones stay. Grade
        # the real observable: every filler whose user row fell outside
        # the snapshot window is fully absent from the prompt (head and
        # tail marker), every filler still inside the window is present
        # (its head marker survives the per-row truncation), and the
        # window really did omit at least one filler row.
        status_fin, page_fin = _messages(env, conversation_id, limit=100)
        assert status_fin == 200, page_fin
        ordered_ids = [str(row["message_id"]) for row in page_fin["items"]]
        window_start = page_fin["total"] - len(snapshot.transcript)
        filler_rows = {i: ordered_ids.index(filler_msg_ids[i]) for i in range(10)}
        out_of_window = [i for i, pos in filler_rows.items() if pos < window_start]
        in_window = [i for i, pos in filler_rows.items() if pos >= window_start]
        _check(
            "C3",
            bool(out_of_window)
            and all(
                f"{filler_marker} {i}: " not in last_prompt
                and f"tail-{filler_marker}-{i}" not in last_prompt
                for i in out_of_window
            )
            and all(f"{filler_marker} {i}: " in last_prompt for i in in_window),
            "assembled prompt omits the OLDEST out-of-window filler rows "
            f"({out_of_window}) while the in-window fillers stay ({in_window}) "
            "(bounded window)",
        )
        _record(
            "C3",
            verdict="pass",
            total_before=total_before,
            total_after=total_after,
            omitted_older=True,
        )
    finally:
        scenario.stop()


# ---------------------------------------------------------------------------
# C4 — idempotent retry / structured conflict on one client_request_id
# ---------------------------------------------------------------------------


@_journal_tails_on_failure
def test_c4_idempotent_retry(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "c4")
    scenario.script(
        [
            {
                "needs": ["msg1"],
                "final_result": {
                    "schema_version": 1,
                    "source_message_id": "$msg1",
                    "turn_kind": "answer",
                    "understanding": "self-contained status question",
                    "answer_text": "the fixture service is a synthetic target",
                },
            }
        ]
    )
    scenario.spawn()
    try:
        status, body = _submit_turn(
            env, None, "What is the fixture service? (c4 idempotency)"
        )
        assert status == 201, body
        conversation_id = uuid.UUID(body["conversation_id"])
        scenario.context(msg1=str(body["message_id"]))
        run_id = uuid.UUID(body["run_id"])
        view = _await_terminal(env, run_id, RUN_TIMEOUT, case_id="C4")
        _check("C4", view["state"] == "succeeded", f"first run succeeded: {view}")

        crid = body["client_request_id"]
        retry_status, retry_body = _resubmit_turn(
            env, conversation_id, crid, "What is the fixture service? (c4 idempotency)"
        )
        _check("C4", retry_status == 200, f"identical retry deduplicated: {retry_status}")
        _check(
            "C4",
            retry_body.get("message_id") == body["message_id"]
            and retry_body.get("run_id") == body["run_id"],
            "retry attaches to the same message and run identities",
        )
        conflict_status, conflict_body = _resubmit_turn(
            env, conversation_id, crid, "a different submission entirely"
        )
        _check("C4", conflict_status == 409, f"key reuse with changed payload: {conflict_status}")
        _check(
            "C4",
            isinstance(conflict_body, dict)
            and "error" in conflict_body
            and conflict_body["error"].get("code"),
            f"structured conflict envelope: {conflict_body}",
        )
        rows = _psql_scalar(
            env.url,
            "SELECT count(*) FROM conversation_messages "
            f"WHERE client_request_id = '{crid}'",
        )
        _check("C4", int(rows) == 1, f"exactly one message row for the key: {rows}")
        run_rows = _psql_scalar(
            env.url,
            f"SELECT count(*) FROM job_runs WHERE run_id = '{run_id}'",
        )
        _check("C4", int(run_rows) == 1, f"exactly one run row: {run_rows}")
        _check(
            "C4",
            scenario.count("effect:fixture.read") == 0
            and scenario.count("effect:fixture.priv") == 0,
            "no effects duplicated",
        )
        _record("C4", verdict="pass", retry=retry_status, conflict=conflict_status)
    finally:
        scenario.stop()


@_journal_tails_on_failure
def test_c4_checkpoint_worker_stop_restart(harness: _Harness) -> None:
    """The crash-restart half of C4 (brief 23e §3/§4).

    The run parks at the durable ``reconciling`` checkpoint on an
    unknown-status effect; the worker is SIGKILLed mid-park. A FRESH
    worker is started and the run is resumed through the product's
    sanctioned resume surface — ``EngineClient.resume_run`` (guard rule
    8), called from the test process, never blind launch recovery — and
    the uncertain step is resolved through the real reconciliation
    producer (``RecoveryService.aresolve_step``). The uncertain effect
    must never be re-dispatched, the restart must add no extra planner
    model call, and the identical retry must still deduplicate onto the
    original message/run identities.
    """
    env = harness
    scenario = Scenario(env, "c4restart")
    scenario.behavior({"fixture.priv:m1": "unknown_status"})
    scenario.script(
        [
            {
                "needs": ["msg1", "crid1"],
                "final_result": _plan_payload_template("msg1", "crid1"),
            },
            {
                "tool_call": {
                    "name": "fixture_priv",
                    "args": {"marker": "m1"},
                    "id": "c4ra",
                }
            },
        ]
    )
    message_text = "Run the crash-restart command (c4 checkpoint restart)"
    scenario.spawn()
    try:
        status, body = _submit_turn(env, None, message_text)
        _check("C4", status == 201, f"turn admitted: {status}")
        conversation_id = uuid.UUID(body["conversation_id"])
        scenario.context(
            msg1=str(body["message_id"]),
            crid1=body.get("client_request_id", str(uuid.uuid4())),
        )
        crid = body["client_request_id"]
        run_id = uuid.UUID(body["run_id"])

        # Parked at the durable reconciling checkpoint; the effect's
        # step row is outcome_unknown (never re-dispatched while open).
        view = _await_state(env, run_id, "reconciling", 60.0)
        _check("C4", view["state"] == "reconciling", f"parked at reconciling: {view}")
        uncertain_rows = _psql(
            env.url,
            "SELECT seq, state FROM job_steps "
            f"WHERE run_id = '{run_id}' AND state = 'outcome_unknown'",
        )
        _check(
            "C4",
            len(uncertain_rows) == 1,
            f"exactly one outcome_unknown step row at the park: {uncertain_rows}",
        )
        seq = int(uncertain_rows[0][0])
        begins_at_park = scenario.count("begin:fixture.priv:m1")
        model_calls_before_restart = scenario.count("model_call")
        _check(
            "C4",
            begins_at_park == 1,
            f"the uncertain effect began exactly once before the crash: {begins_at_park}",
        )

        # SIGKILL the worker mid-park. The workflow row stays owned by
        # the dead attempt (PENDING + owner_xid) — exactly the state a
        # blind launch redispatch cannot safely re-execute (dbos 3.2.0
        # owner CAS on the step checkpoint) — so this leg resumes the
        # run DELIBERATELY instead of relying on launch recovery.
        assert scenario.worker is not None
        scenario.worker.send_signal(signal.SIGKILL)
        scenario.worker.wait(timeout=WORKER_STOP_TIMEOUT)
        dead_owner_rows = int(
            _psql_scalar(
                env.url,
                "SELECT count(*) FROM dbos.workflow_status "
                f"WHERE workflow_uuid = '{run_id}' AND status = 'PENDING' "
                "AND owner_xid IS NOT NULL",
            )
            or 0
        )
        _check(
            "C4",
            dead_owner_rows == 1,
            "the killed attempt still owns the workflow row after SIGKILL",
        )

        # Fresh worker. The recovered run replays its recorded rounds,
        # so its script carries ONLY the planned continuation entry (a
        # full re-serve would let a re-plan consume a stale entry); the
        # killed worker's log is preserved before the respawn truncates.
        worker_log = scenario.dir / "worker.log"
        if worker_log.exists():
            worker_log.rename(scenario.dir / "worker-killed.log")
        (scenario.dir / "model-script.json").write_text(
            json.dumps(
                [{"text": "the reconciled outcome is reported; the run is complete"}]
            ),
            encoding="utf-8",
        )
        scenario.spawn()

        # Deliberate resume through the product's sanctioned surface —
        # from the TEST process (the client never launches an engine).
        # resume_workflows sets status=ENQUEUED AND owner_xid=NULL,
        # recovery_attempts=0: precisely the field the dead attempt
        # left stale.
        from mai.jobs.engine import EngineClient

        with EngineClient(env.url) as client:
            client.resume_run(str(run_id))

        # Resolve the uncertain step through the REAL reconciliation
        # producer: outcome_unknown -> terminal under a FINAL host
        # verdict, receipt reason 'reconciliation', parked run woken on
        # the resume topic.
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from mai.execution.reconcile import ReconciliationClass
        from mai.jobs.recovery import RecoveryService

        service_engine = create_async_engine(env.url, poolclass=NullPool)
        service = RecoveryService(
            env.url,
            async_sessionmaker(service_engine, expire_on_commit=False),
        )
        try:
            resolved = _run(
                service.aresolve_step(
                    str(run_id),
                    seq,
                    ReconciliationClass.FINAL_SUCCEEDED,
                    source="act-evals-c4-crash-restart",
                )
            )
        finally:
            service.close()
            _run(service_engine.dispose())
        _check(
            "C4",
            resolved.state.value == "succeeded" and resolved.seq == seq,
            f"resolution moved the uncertain step to a terminal state: {resolved}",
        )

        view = _await_terminal(env, run_id, RUN_TIMEOUT, case_id="C4")
        _check(
            "C4",
            view["state"] == "succeeded",
            f"run reached terminal after the deliberate resume: {view}",
        )
        step_state = _psql_scalar(
            env.url,
            "SELECT state FROM job_steps WHERE run_id = "
            f"'{run_id}' AND seq = {seq}",
        )
        _check(
            "C4",
            step_state == "succeeded",
            f"the resolved step row is terminal: {step_state}",
        )
        resolve_reason = _psql_scalar(
            env.url,
            "SELECT receipt->>'reason' FROM job_steps "
            f"WHERE run_id = '{run_id}' AND seq = {seq}",
        )
        _check(
            "C4",
            resolve_reason == "reconciliation",
            f"the step was resolved by the reconciliation producer: {resolve_reason}",
        )

        # Exactly one of everything, across the crash.
        msg_rows = int(
            _psql_scalar(
                env.url,
                "SELECT count(*) FROM conversation_messages "
                f"WHERE client_request_id = '{crid}'",
            )
            or 0
        )
        _check("C4", msg_rows == 1, f"exactly one message row for the key: {msg_rows}")
        run_rows = int(
            _psql_scalar(
                env.url,
                f"SELECT count(*) FROM job_runs WHERE run_id = '{run_id}'",
            )
            or 0
        )
        _check("C4", run_rows == 1, f"exactly one run row: {run_rows}")
        intake_links = int(
            _psql_scalar(
                env.url,
                "SELECT count(*) FROM conversation_runs "
                f"WHERE run_id = '{run_id}'",
            )
            or 0
        )
        _check(
            "C4",
            intake_links == 1,
            f"exactly one intake result (conversation_runs link): {intake_links}",
        )

        # The uncertain effect was NOT re-dispatched by the restart:
        # the journal shows the effect marker exactly once and the
        # begin count did not grow for that attempt.
        _check(
            "C4",
            scenario.count("begin:fixture.priv:m1") == begins_at_park
            and scenario.count("effect:fixture.priv:m1") == 1,
            "the uncertain effect was never re-dispatched after the restart",
        )
        # No extra planner model call after the resume: the restart
        # itself adds nothing; exactly the planned final-answer round
        # runs (an unplanned call would surface as a script spare).
        model_calls_after = scenario.count("model_call")
        _check(
            "C4",
            model_calls_after == model_calls_before_restart + 1
            and scenario.count("script_spare") == 0,
            f"restart added only the planned final round "
            f"({model_calls_before_restart} before -> {model_calls_after} after, "
            f"spares={scenario.count('script_spare')})",
        )

        # The identical retry still deduplicates onto the same
        # message/run identities; the one-of-every-thing counts hold.
        retry_status, retry_body = _resubmit_turn(
            env, conversation_id, crid, message_text
        )
        _check(
            "C4",
            retry_status == 200,
            f"retry after the crash-restart deduplicated: {retry_status}",
        )
        _check(
            "C4",
            retry_body.get("message_id") == body["message_id"]
            and retry_body.get("run_id") == body["run_id"],
            "retry attaches to the same message and run identities",
        )
        msg_after = int(
            _psql_scalar(
                env.url,
                "SELECT count(*) FROM conversation_messages "
                f"WHERE client_request_id = '{crid}'",
            )
            or 0
        )
        runs_after = int(
            _psql_scalar(
                env.url,
                f"SELECT count(*) FROM job_runs WHERE run_id = '{run_id}'",
            )
            or 0
        )
        intake_after = int(
            _psql_scalar(
                env.url,
                "SELECT count(*) FROM conversation_runs "
                f"WHERE run_id = '{run_id}'",
            )
            or 0
        )
        _check(
            "C4",
            msg_after == 1 and runs_after == 1 and intake_after == 1,
            "row counts stay exactly one after the retry "
            f"(messages={msg_after}, runs={runs_after}, intake={intake_after})",
        )
        _record(
            "C4",
            verdict="pass",
            restart="EngineClient.resume_run + RecoveryService.aresolve_step",
            resolved_seq=seq,
            retry=retry_status,
        )
    finally:
        scenario.stop()


# ---------------------------------------------------------------------------
# C5 — steering during a gated two-call batch, then deterministic Pause
# ---------------------------------------------------------------------------


@_journal_tails_on_failure
def test_c5_steering_pause_gates_next_effect(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "c5")
    scenario.behavior({"fixture.priv:m1": "gate"})
    scenario.script(
        [
            {
                "needs": ["msg1", "crid1"],
                "final_result": _plan_payload_template("msg1", "crid1"),
            },
            {
                "tool_call": {
                    "name": "fixture_priv",
                    "args": {"marker": "m1"},
                    "id": "c5a",
                }
            },
            {
                "tool_call": {
                    "name": "fixture_priv",
                    "args": {"marker": "m2"},
                    "id": "c5b",
                }
            },
        ]
    )
    scenario.spawn()
    try:
        status, body = _submit_turn(
            env,
            None,
            "Plan the two privileged maintenance calls (c5 steering)",
            requested_phase="act",
        )
        _check("C5", status == 201, f"turn admitted (201): got {status} {body}")
        conversation_id = uuid.UUID(body["conversation_id"])
        scenario.context(
            msg1=str(body["message_id"]),
            crid1=body.get("client_request_id", str(uuid.uuid4())),
        )
        run_id = uuid.UUID(body["run_id"])
        # the FIRST call parks in-flight (deterministic window)
        try:
            scenario.wait_journal("gate_wait:fixture.priv:m1", timeout=60.0)
        except AssertionError as exc:
            # record BEFORE aborting: the row keeps its named cause
            _check("C5", False, str(exc))
            raise
        _check(
            "C5",
            scenario.count("begin:fixture.priv:m1") == 1,
            "first call began exactly once",
        )
        # steering lands while the batch is parked, then deterministic Pause
        steer_status, steer_body = _api(
            env,
            "POST",
            f"/api/v1/chat/conversations/{conversation_id}/steer",
            body={
                "conversation_id": str(conversation_id),
                "server_id": str(env.server_id),
                "action": "steer",
                "message": "also verify the backup directory before continuing",
            },
            mutate=True,
        )
        _check("C5", steer_status == 201, f"steering recorded: {steer_status} {steer_body}")
        pause_status, pause_body = _api(
            env,
            "POST",
            f"/api/v1/chat/conversations/{conversation_id}/steer",
            body={
                "conversation_id": str(conversation_id),
                "server_id": str(env.server_id),
                "action": "pause",
            },
            mutate=True,
        )
        _check("C5", pause_status == 201, f"pause recorded: {pause_status} {pause_body}")
        # the in-flight effect completes honestly (exactly once). The
        # gate file is keyed by the CAPABILITY identifier inside the
        # executor (FixtureExecutor: gate-{capability_id}-{marker} with
        # capability_id = descriptor identifier 'fixture.priv'), never by
        # the tool name — releasing 'fixture_priv' wrote a gate file the
        # parked call does not watch, so the effect could never complete
        # and the run could never reach its terminal pause boundary.
        scenario.release_gate("fixture.priv", "m1")
        view = _await_terminal(env, run_id, RUN_TIMEOUT, case_id="C5")
        # Ratified registry row (23d, captain Option A): the boundary stop
        # is REVIEWED application behaviour — the run ends TERMINAL
        # (succeeded) with agent.completed carrying stopped_by=pause
        # (run.py:2839-2844 writes the key, run.py:3627-3630 carries it in
        # the run summary; backend/tests/test_chat_steering_continuity.py
        # 'Terminal run => lease released + honest stopped_by event'
        # pins exactly this). The 23c row text said 'run ends cancelled':
        # that registry wording was the defect (a deterministic boundary
        # stop is not a cancellation), and this check now asserts the
        # ratified observable. The row's substantive content — pause is
        # deterministic, the in-flight effect completed honestly exactly
        # once, the second call never dispatched — stays asserted below,
        # unchanged.
        stopped_data = None
        stopped = 0
        stopped_deadline = time.monotonic() + 10.0
        while time.monotonic() < stopped_deadline:
            stopped_data = _psql_scalar(
                env.url,
                "SELECT data FROM job_events WHERE run_id = "
                f"'{run_id}' AND type = 'agent.completed' "
                "AND data->>'stopped_by' IS NOT NULL "
                "ORDER BY seq DESC LIMIT 1",
            )
            if stopped_data is not None:
                stopped = 1
                break
            time.sleep(0.2)
        _check(
            "C5",
            stopped == 1,
            "the engine consumed the pause at the safe boundary "
            f"(agent.completed carries stopped_by): {stopped}",
        )
        _check(
            "C5",
            isinstance(stopped_data, dict)
            and stopped_data.get("stopped_by") == "pause",
            "agent.completed carries stopped_by == 'pause': "
            f"{stopped_data}",
        )
        _check(
            "C5",
            scenario.count("effect:fixture.priv:m1") == 1,
            "in-flight effect completed exactly once",
        )
        _check(
            "C5",
            scenario.count("begin:fixture.priv:m2") == 0,
            "the second call never began after the pause",
        )
        _check(
            "C5",
            scenario.count("model_call") == 2,
            f"pause needed no planner call: {scenario.count('model_call')} model calls",
        )
        pending = _psql_scalar(
            env.url,
            "SELECT count(*) FROM conversation_steering WHERE conversation_id = "
            f"'{conversation_id}' AND action = 'steer' AND applied_at IS NULL",
        )
        _check(
            "C5",
            int(pending) == 1,
            "natural-language steering stayed pending (not silently dropped)",
        )
        _check(
            "C5",
            view["state"] in TERMINAL_STATES and view["state"] == "succeeded",
            "run ends terminal (succeeded) with agent.completed "
            f"stopped_by=pause: state={view['state']}",
        )
        _record("C5", verdict="pass", final_state=view["state"])
    finally:
        scenario.stop()


def _plan_payload_template(msg_key: str, crid_key: str) -> dict[str, Any]:
    """The shared C5/X1 plan-turn template (template keys resolved at runtime)."""
    return {
        "schema_version": 1,
        "source_message_id": f"${msg_key}",
        "turn_kind": "plan",
        "understanding": "the user requests a bounded maintenance plan",
        "objective": "Run the scripted maintenance scenario",
        "acceptance_criteria": [
            {"criterion_id": "done", "statement": "the scenario completed"}
        ],
        "constraints_proposed": [
            {
                "constraint_id": "report_only",
                "category": "scope",
                "statement": "Report only; make no changes beyond the scripted check",
                "origin": "user",
                "source_message_refs": [f"${msg_key}"],
                "suggested": "add",
            }
        ],
        "requested_phase": "act",
        "todo_patch": {
            "schema_version": 1,
            "source_message_id": f"${msg_key}",
            "base_revision": 1,
            "idempotency_key": f"${crid_key}",
            "operations": [
                {
                    "op": "add",
                    "step": {"step_id": "run_check", "title": "Run the check"},
                }
            ],
        },
    }


# ---------------------------------------------------------------------------
# C6 / U1 / U2 / P5 — API-truth surface over a seeded task, DE-BATCHED
# (fix 5): each registry row seeds its own task and owns its own test so
# one row's abort cannot blank its siblings' verdicts.
# ---------------------------------------------------------------------------


def _submit_correction(
    env: _Harness,
    task_id: uuid.UUID,
    message_id: uuid.UUID,
    *,
    base_revision: int,
    key: str,
) -> tuple[int, dict[str, Any]]:
    """One structured correction (CAS write) against an EXPLICIT base.

    Defect-class-C fix: the caller decides the base — a write that is
    MEANT to succeed re-reads the current revision
    (:func:`_current_revision`) instead of reusing the stale seeded one;
    the C6 conflict leg deliberately pins the same seeded base for both
    writers (that IS the C6 observable).
    """
    return _api(
        env,
        "POST",
        f"/api/v1/chat/tasks/{task_id}/revisions",
        body={
            "server_id": str(env.server_id),
            "patch": {
                "schema_version": 1,
                "source_message_id": str(message_id),
                "base_revision": base_revision,
                "idempotency_key": key,
                "operations": [
                    {
                        "op": "update",
                        "step": {
                            "step_id": "prepare",
                            "title": "Prepare (corrected)",
                        },
                    }
                ],
            },
            "constraint_proposals": [],
        },
        mutate=True,
    )


def _current_revision(env: _Harness, conversation_id: uuid.UUID) -> int:
    """The current task revision, re-read before each CAS write (fix 4)."""
    return int(_task_header(env, conversation_id)["revision"])


@_journal_tails_on_failure
def test_c6_same_base_writers_conflict(harness: _Harness) -> None:
    env = harness
    seed = _seed_task_via_intake(env, "Seed the eval surface task (c6)")
    conversation_id = uuid.UUID(str(seed["conversation_id"]))
    task_id = uuid.UUID(str(seed["task_id"]))
    message_id = uuid.UUID(str(seed["message_id"]))
    revision = int(seed["revision"])

    # -- C6: same-base-revision writers; one wins, the other conflicts -------
    first_status, first_body = _submit_correction(
        env,
        task_id,
        message_id,
        base_revision=revision,
        key=str(uuid.uuid4()),
    )
    _check("C6", first_status == 201, f"first writer created: {first_status} {first_body}")
    second_status, second_body = _submit_correction(
        env,
        task_id,
        message_id,
        base_revision=revision,
        key=str(uuid.uuid4()),
    )
    _check("C6", second_status == 409, f"stale writer conflicted: {second_status}")
    _check(
        "C6",
        second_body.get("error", {}).get("code") == "revision_conflict",
        f"structured revision_conflict: {second_body}",
    )
    after = _task_header(env, conversation_id)
    _check(
        "C6",
        after["revision"] == revision + 1,
        f"no overwrite: revision {revision} -> {after['revision']}",
    )
    _record("C6", verdict="pass", conflict_code=second_body["error"]["code"])


@_journal_tails_on_failure
def test_u1_task_items_pagination(harness: _Harness) -> None:
    env = harness
    seed = _seed_task_via_intake(env, "Seed the eval surface task (u1)")
    task_id = uuid.UUID(str(seed["task_id"]))

    # -- U1: bounded pages + Load-more semantics -----------------------------
    page1_status, page1 = _api(
        env,
        "GET",
        f"/api/v1/chat/tasks/{task_id}/items?server_id={env.server_id}&limit=1&offset=0",
    )
    _check("U1", page1_status == 200, "item page served")
    for key in ("items", "limit", "offset", "total", "has_more"):
        _check("U1", key in page1, f"envelope carries {key}")
    _check("U1", len(page1["items"]) == 1 and page1["limit"] == 1, "page bounded")
    expected_more = page1["offset"] + len(page1["items"]) < page1["total"]
    _check("U1", page1["has_more"] == expected_more, "has_more honest")
    page2_status, page2 = _api(
        env,
        "GET",
        f"/api/v1/chat/tasks/{task_id}/items?server_id={env.server_id}&limit=1&offset=1",
    )
    ids1 = {row["step_id"] for row in page1["items"]}
    ids2 = {row["step_id"] for row in page2["items"]}
    _check(
        "U1",
        page2_status == 200 and not (ids1 & ids2),
        "Load-more page disjoint and ordered",
    )
    # -- U1 browser leg (R-23-evals item 5, option D1+D2): the 360x640
    # visual journeys live in the named Playwright suites (V2/V3
    # feature-gate evidence); this runtime harness exercises the
    # pagination DATA those panels render. The reference is CHECKED, not
    # assumed: the named files must exist and carry the 360x640
    # assertions, or the registry's browser_leg claim is false.
    browser_leg = CASES["U1"].get("browser_leg") or {}
    missing_or_empty = [
        path
        for path in browser_leg.get("covered_by", [])
        if "360x640" not in path
        or not (REPO_ROOT / path.split(" (")[0]).is_file()
        or "360"
        not in (REPO_ROOT / path.split(" (")[0]).read_text(encoding="utf-8")
    ]
    _check(
        "U1",
        bool(browser_leg.get("covered_by"))
        and bool(browser_leg.get("command"))
        and not missing_or_empty,
        f"browser_leg reference integrity (named suites exist and assert "
        f"360x640): missing={missing_or_empty}",
    )
    limitations = CASES["U1"].get("disclosed_limitations") or []
    _check(
        "U1",
        any("360x640" in entry for entry in limitations),
        "case-level disclosed_limitations carries the 360x640 disclosure "
        "(the scorecard surfaces case-level entries)",
    )
    _record(
        "U1",
        verdict="pass",
        total=page1["total"],
        browser_leg="reference integrity checked; the 360x640 journeys "
        "themselves are owned by the V2/V3 feature-gate Playwright verdicts "
        "(disclosed, not re-claimed here)",
    )


@_journal_tails_on_failure
def test_u2_empty_unknown_surfaces_honest(harness: _Harness) -> None:
    env = harness
    seed = _seed_task_via_intake(env, "Seed the eval surface task (u2)")
    task_id = uuid.UUID(str(seed["task_id"]))
    conversation_id = uuid.UUID(str(seed["conversation_id"]))

    # -- U2: empty / unknown / stale / no fabricated approval -----------------
    empty_status, empty_page = _api(
        env,
        "GET",
        f"/api/v1/chat/tasks/{task_id}/evidence?server_id={env.server_id}",
    )
    _check(
        "U2",
        empty_status == 200 and empty_page["items"] == [] and empty_page["total"] == 0,
        f"empty evidence page honest: {empty_page}",
    )
    unknown_status, unknown_body = _api(
        env,
        "GET",
        f"/api/v1/chat/tasks/{uuid.uuid4()}/items?server_id={env.server_id}",
    )
    _check(
        "U2",
        unknown_status == 404 and unknown_body.get("error", {}).get("code"),
        f"unknown task is the structured 404: {unknown_status} {unknown_body}",
    )
    header = _task_header(env, conversation_id)
    _check(
        "U2",
        not ({"approved", "verified_success"} & set(header)),
        f"no fabricated approval fields on the task header: {sorted(header)}",
    )
    _record("U2", verdict="pass")


@_journal_tails_on_failure
def test_p5_shared_revision_and_read_stability(harness: _Harness) -> None:
    env = harness
    seed = _seed_task_via_intake(env, "Seed the eval surface task (p5)")
    conversation_id = uuid.UUID(str(seed["conversation_id"]))
    task_id = uuid.UUID(str(seed["task_id"]))
    message_id = uuid.UUID(str(seed["message_id"]))
    revision = int(seed["revision"])

    # -- P5: shared server revision; repeated reads; idle control honesty -----
    from mai.agents.task_store import aget_task

    async def _store_revision():
        task = await aget_task(
            env.app.state.session_factory, task_id, server_id=env.server_id
        )
        return task.revision

    header = _task_header(env, conversation_id)
    _check(
        "P5",
        header["revision"] == await_(_store_revision()),
        "API and store reads share the server revision",
    )
    # Defect-class-C fix: each correction RE-READS the current revision
    # before its CAS write (the old closure reused the stale seeded base
    # and the app correctly answered 409 — recorded, never a bare assert).
    first_status, first_body = _submit_correction(
        env,
        task_id,
        message_id,
        base_revision=_current_revision(env, conversation_id),
        key=str(uuid.uuid4()),
    )
    _check(
        "P5",
        first_status == 201,
        f"first re-read correction applied: {first_status} {first_body}",
    )
    header1 = _task_header(env, conversation_id)
    _check(
        "P5",
        header1["revision"] == await_(_store_revision()) == revision + 1,
        "first correction visible to both surfaces at the same revision",
    )
    second_status, second_body = _submit_correction(
        env,
        task_id,
        message_id,
        base_revision=_current_revision(env, conversation_id),
        key=str(uuid.uuid4()),
    )
    _check(
        "P5",
        second_status == 201,
        f"second re-read correction applied: {second_status} {second_body}",
    )
    header2 = _task_header(env, conversation_id)
    _check(
        "P5",
        header2["revision"] == await_(_store_revision()) == revision + 2,
        "correction visible to both surfaces at the same revision",
    )
    page_a = _api(
        env,
        "GET",
        f"/api/v1/chat/tasks/{task_id}/items?server_id={env.server_id}",
    )[1]
    page_b = _api(
        env,
        "GET",
        f"/api/v1/chat/tasks/{task_id}/items?server_id={env.server_id}",
    )[1]
    _check(
        "P5",
        [row["step_id"] for row in page_a["items"]]
        == [row["step_id"] for row in page_b["items"]],
        "repeated reads never duplicate steps",
    )
    idle_status, idle_body = _api(
        env,
        "POST",
        f"/api/v1/chat/conversations/{conversation_id}/steer",
        body={
            "conversation_id": str(conversation_id),
            "server_id": str(env.server_id),
            "action": "pause",
        },
        mutate=True,
    )
    honest_rejection = idle_status == 409 and idle_body.get("error", {}).get("code")
    detail_status, detail = _conversation(env, conversation_id)
    honest_pending = (
        idle_status == 201 and detail_status == 200 and not detail.get("active_run_id")
    )
    _check(
        "P5",
        honest_rejection or honest_pending,
        f"idle-conversation control answered honestly: {idle_status} {idle_body}",
    )
    _record("P5", verdict="pass", revision_after=header2["revision"])


def await_(coro: Any) -> Any:
    """Alias kept minimal for readability inside tests."""
    return _run(coro)


# ---------------------------------------------------------------------------
# C7 — legacy mixed-origin gap: labelled, never fabricated, mutation gated
# ---------------------------------------------------------------------------


@_journal_tails_on_failure
def test_c7_legacy_gap_labelled(harness: _Harness) -> None:
    env = harness
    seeded = _run(H._seed_legacy_conversation(env.app, env.server_id))
    conversation_id = seeded.conversation_id

    status, detail = _conversation(env, conversation_id)
    _check("C7", status == 200, "legacy conversation readable")
    _check(
        "C7",
        detail.get("history_completeness") == "partial",
        f"gap labelled partial: {detail.get('history_completeness')}",
    )
    _check(
        "C7",
        detail.get("missing_range") is not None,
        "missing range disclosed",
    )
    msg_status, page = _messages(env, conversation_id)
    kinds = {row.get("source_kind") for row in page.get("items", [])}
    _check(
        "C7",
        msg_status == 200 and "legacy_import" in kinds,
        f"recovered rows keep legacy provenance: {kinds}",
    )
    total = page.get("total", 0)
    _check(
        "C7",
        total == 3,
        f"only proved rows are mapped (no fabricated turns): {total}",
    )
    mut_status, mut_body = _submit_turn(env, conversation_id, "continue the legacy work")
    _check(
        "C7",
        mut_status == 409 and mut_body.get("error", {}).get("code")
        == "history_reconstruction_incomplete",
        f"mutation gated on incomplete history: {mut_status} {mut_body}",
    )
    task_status, _task_view = _api(
        env,
        "GET",
        f"/api/v1/chat/conversations/{conversation_id}/task"
        f"?server_id={env.server_id}",
    )
    _check(
        "C7",
        task_status == 404,
        "no task episode inferred from unsupported legacy history",
    )
    _record("C7", verdict="pass")


# ---------------------------------------------------------------------------
# C8 — cross-scope references fail closed with zero leakage
# ---------------------------------------------------------------------------


@_journal_tails_on_failure
def test_c8_cross_scope_fail_closed(harness: _Harness) -> None:
    env = harness
    seed = _seed_task_via_intake(env, "Seed the cross-scope task (c8)")
    conversation_id = uuid.UUID(str(seed["conversation_id"]))
    task_id = uuid.UUID(str(seed["task_id"]))
    foreign = uuid.uuid4()

    submit_status, submit_body = _api(
        env,
        "POST",
        "/api/v1/chat/conversations",
        body={
            "server_id": str(foreign),
            "client_request_id": str(uuid.uuid4()),
            "message": "foreign scope submission",
        },
        mutate=True,
    )
    _check("C8", submit_status == 404, f"foreign-scope submit fail-closed: {submit_status}")
    read_status, read_body = _api(
        env,
        "GET",
        f"/api/v1/chat/conversations/{conversation_id}?server_id={foreign}",
    )
    _check("C8", read_status == 404, f"foreign-scope read fail-closed: {read_status}")
    task_status, task_body = _api(
        env,
        "GET",
        f"/api/v1/chat/tasks/{task_id}/items?server_id={foreign}",
    )
    _check("C8", task_status == 404, f"foreign-scope task page fail-closed: {task_status}")
    marker = "Eval seed task for"
    for body in (submit_body, read_body, task_body):
        _check(
            "C8",
            marker not in json.dumps(body),
            "no owned task content leaked through the error channel",
        )
    _record("C8", verdict="pass")


# ---------------------------------------------------------------------------
# P1 / P2 — planner intake shapes
# ---------------------------------------------------------------------------


@_journal_tails_on_failure
def test_p1_compact_answer_no_task(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "p1")
    scenario.script(
        [
            {
                "needs": ["msg1"],
                "final_result": {
                    "schema_version": 1,
                    "source_message_id": "$msg1",
                    "turn_kind": "answer",
                    "understanding": "greeting",
                    "answer_text": "Hello! I can help inspect and operate the fixture target.",
                },
            }
        ]
    )
    scenario.spawn()
    try:
        status, body = _submit_turn(env, None, "Hello — what can you help me with?")
        assert status == 201, body
        conversation_id = uuid.UUID(body["conversation_id"])
        scenario.context(msg1=str(body["message_id"]))
        view = _await_terminal(
            env, uuid.UUID(body["run_id"]), RUN_TIMEOUT, case_id="P1"
        )
        _check("P1", view["state"] == "succeeded", f"answer run succeeded: {view}")
        _check(
            "P1",
            body.get("task_id") is None,
            "submit carries task_id|null (contract §3: binding happens at intake)",
        )
        # The graded observable, post-terminal: an answer turn created NO
        # task machinery at all — the task header is the structured 404.
        task_status, task_body = _api(
            env,
            "GET",
            f"/api/v1/chat/conversations/{conversation_id}/task"
            f"?server_id={env.server_id}",
        )
        _check(
            "P1",
            task_status == 404,
            f"answer turn created no task episode (structured 404): {task_status} {task_body}",
        )
        _check(
            "P1",
            scenario.count("model_call") == 1,
            f"exactly one planner call: {scenario.count('model_call')}",
        )
        _check(
            "P1",
            scenario.count("effect:fixture.read") == 0
            and scenario.count("effect:fixture.priv") == 0,
            "zero effects",
        )
        _check(
            "P1",
            scenario.count("tools:") >= 1,
            "tool declarations journaled",
        )
        intake_tools = [
            line for line in scenario.journal() if line.startswith("tools:")
        ][0]
        _check(
            "P1",
            intake_tools == "tools:",
            f"intake call is tool-free: {intake_tools!r}",
        )
        status_m, page = _messages(env, conversation_id)
        bodies = [row.get("content") or row.get("body") or "" for row in page["items"]]
        _check(
            "P1",
            any("Hello! I can help" in b for b in bodies),
            "assistant answer persisted",
        )
        _record("P1", verdict="pass")
    finally:
        scenario.stop()


@_journal_tails_on_failure
def test_p2_assumptions_not_facts(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "p2")
    scenario.script(
        [
            {
                "needs": ["msg1", "crid1"],
                "final_result": {
                    "schema_version": 1,
                    "source_message_id": "$msg1",
                    "turn_kind": "plan",
                    "understanding": "unfamiliar multi-step request with a gap",
                    "objective": "Deploy the quux-frobnicator service",
                    "acceptance_criteria": [
                        {
                            "criterion_id": "deployed",
                            "statement": "the service runs on the target",
                        }
                    ],
                    "assumptions": [
                        {
                            "statement": "the quux-frobnicator package exists on the target",
                            "basis": "unverified",
                        }
                    ],
                    "open_questions": [
                        {
                            "proposal_id": "prereq_host",
                            "question": "Which host should run quux-frobnicator?",
                        }
                    ],
                    "requested_phase": "plan",
                    "todo_patch": {
                        "schema_version": 1,
                        "source_message_id": "$msg1",
                        "base_revision": 1,
                        "idempotency_key": "$crid1",
                        "operations": [
                            {
                                "op": "add",
                                "step": {
                                    "step_id": "discover_prereqs",
                                    "title": "Discover the prerequisites",
                                    "status": "blocked",
                                },
                            }
                        ],
                    },
                },
            },
            {"text": "plan recorded with an explicit assumption and open question"},
        ]
    )
    scenario.spawn()
    try:
        status, body = _submit_turn(
            env,
            None,
            "Deploy the quux-frobnicator service to the fixture target "
            "(I do not know its prerequisites)",
        )
        assert status == 201, body
        conversation_id = uuid.UUID(body["conversation_id"])
        scenario.context(
            msg1=str(body["message_id"]),
            crid1=body.get("client_request_id", str(uuid.uuid4())),
        )
        view = _await_terminal(
            env, uuid.UUID(body["run_id"]), RUN_TIMEOUT, case_id="P2"
        )
        _check("P2", view["state"] == "succeeded", f"plan run succeeded: {view}")
        # Defect-class-A fix: the task binds worker-side at intake; the
        # episode id comes from the post-terminal task header, never from
        # the submit response (task_id|null is the §3 contract there).
        task_id = uuid.UUID(str(_task_header(env, conversation_id)["task_id"]))
        items_status, items = _api(
            env,
            "GET",
            f"/api/v1/chat/tasks/{task_id}/items?server_id={env.server_id}",
        )
        step_ids = [row["step_id"] for row in items["items"]]
        blocked = {
            row["step_id"]: row.get("status")
            for row in items["items"]
        }
        _check(
            "P2",
            "discover_prereqs" in step_ids
            and blocked.get("discover_prereqs") == "blocked",
            f"blocked discovery step recorded: {blocked}",
        )
        rev_status, revisions = _api(
            env,
            "GET",
            f"/api/v1/chat/tasks/{task_id}/revisions?server_id={env.server_id}",
        )
        latest = revisions.get("items", [{}])[0]
        # The create revision is applied BY the store pipeline (author
        # 'system'); the validated-MODEL-intake proof is the payload
        # content: the episode title IS the model's interpreted objective.
        _check(
            "P2",
            latest.get("kind") == "create"
            and latest.get("payload", {}).get("title")
            == "Deploy the quux-frobnicator service",
            f"episode created from the validated model intake payload: {latest}",
        )
        # The assumption/question text rides the structured turn (scripted
        # provenance); the durable unverified-prerequisite marker is the
        # BLOCKED discovery step the store persists (checked above).
        _check(
            "P2",
            scenario.count("effect:fixture.read") == 0
            and scenario.count("effect:fixture.priv") == 0,
            "zero effects",
        )
        intake_tools = [
            line for line in scenario.journal() if line.startswith("tools:")
        ][0]
        _check(
            "P2",
            intake_tools == "tools:",
            "first call declares no tools",
        )
        _record("P2", verdict="pass", steps=step_ids)
    finally:
        scenario.stop()


# ---------------------------------------------------------------------------
# P3 — continuation delta with stable item IDs and pending scope proposal
# ---------------------------------------------------------------------------


@_journal_tails_on_failure
def test_p3_stable_items_revision_delta(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "p3")
    scenario.script(
        [
            {
                "needs": ["msg1", "crid1"],
                "final_result": _plan_payload_template("msg1", "crid1"),
            },
            {"text": "initial plan recorded"},
            {
                "needs": ["msg2", "rev1", "crid2"],
                "final_result": {
                    "schema_version": 1,
                    "source_message_id": "$msg2",
                    "turn_kind": "continue_task",
                    "understanding": "requirement changed: add a rollback step",
                    "todo_patch": {
                        "schema_version": 1,
                        "source_message_id": "$msg2",
                        "base_revision": "$rev1",
                        "idempotency_key": "$crid2",
                        "operations": [
                            {
                                "op": "add",
                                "step": {
                                    "step_id": "rollback",
                                    "title": "Prepare the rollback",
                                },
                            }
                        ],
                    },
                    "scope_change_proposal": {
                        "additions": ["rollback capability"],
                        "removals": [],
                    },
                },
            },
            {"text": "delta recorded; scope addition awaits decision"},
        ]
    )
    scenario.spawn()
    try:
        status1, body1 = _submit_turn(
            env, None, "Plan the maintenance window (p3 initial)"
        )
        assert status1 == 201, body1
        conversation_id = uuid.UUID(body1["conversation_id"])
        scenario.context(
            msg1=str(body1["message_id"]),
            crid1=body1.get("client_request_id", str(uuid.uuid4())),
        )
        view1 = _await_terminal(
            env, uuid.UUID(body1["run_id"]), RUN_TIMEOUT, case_id="P3"
        )
        _check("P3", view1["state"] == "succeeded", f"initial run succeeded: {view1}")
        # Defect-class-A fix: episode id from the post-terminal task header.
        task_id = uuid.UUID(str(_task_header(env, conversation_id)["task_id"]))
        items1 = _api(
            env,
            "GET",
            f"/api/v1/chat/tasks/{task_id}/items?server_id={env.server_id}",
        )[1]
        ids1 = [row["step_id"] for row in items1["items"]]

        status2, body2 = _submit_turn(
            env, conversation_id, "continue — requirement changed: also add a rollback step"
        )
        assert status2 == 201, body2
        scenario.context(
            msg2=str(body2["message_id"]),
            crid2=body2.get("client_request_id", str(uuid.uuid4())),
        )
        scenario.context(rev1=_task_header(env, conversation_id)["revision"])
        view2 = _await_terminal(
            env, uuid.UUID(body2["run_id"]), RUN_TIMEOUT, case_id="P3"
        )
        _check("P3", view2["state"] == "succeeded", f"delta run succeeded: {view2}")
        # Defect-class-A fix: episode continuity judged on the post-terminal
        # header (the submit response carries task_id|null by contract).
        _check(
            "P3",
            _task_header(env, conversation_id).get("task_id") == str(task_id),
            "continuation stays on the same task episode",
        )
        header = _task_header(env, conversation_id)
        _check(
            "P3",
            header["revision"] == 2,
            f"revision delta recorded: {header['revision']}",
        )
        items2 = _api(
            env,
            "GET",
            f"/api/v1/chat/tasks/{task_id}/items?server_id={env.server_id}",
        )[1]
        ids2 = [row["step_id"] for row in items2["items"]]
        _check(
            "P3",
            ids2 == sorted(set(ids1) | {"rollback"}, key=ids2.index) or set(ids1) < set(ids2),
            f"prior items stable, delta added: {ids1} -> {ids2}",
        )
        _check(
            "P3",
            len(ids2) == len(ids1) + 1,
            "scope addition not silently expanded beyond the delta",
        )
        rev_status, revisions = _api(
            env,
            "GET",
            f"/api/v1/chat/tasks/{task_id}/revisions?server_id={env.server_id}",
        )
        latest = revisions.get("items", [{}])[0]
        _check(
            "P3",
            latest.get("kind") == "plan_patch" and latest.get("author") == "model",
            f"continuation stored as a model plan_patch delta: {latest}",
        )
        # The scope_change proposal rides the structured turn; the store's
        # honest behavior is that NO extra step appears beyond the explicit
        # delta (checked above) — the addition awaits a user decision.
        _record("P3", verdict="pass", ids_before=ids1, ids_after=ids2)
    finally:
        scenario.stop()


# ---------------------------------------------------------------------------
# P4 — invalid intake and user-prohibition erasure both fail closed
# ---------------------------------------------------------------------------


@_journal_tails_on_failure
def test_p4_invalid_intake_rejected_no_effects(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "p4")
    invalid_union = {
        "schema_version": 1,
        "source_message_id": "$msg1",
        "turn_kind": "teleport",
        "understanding": "n/a",
    }
    supersede_user = {
        "needs": ["msg2", "rev1"],
        "final_result": {
            "schema_version": 1,
            "source_message_id": "$msg2",
            "turn_kind": "continue_task",
            "understanding": "the model tries to erase the user prohibition",
            "todo_patch": {
                "schema_version": 1,
                "source_message_id": "$msg2",
                "base_revision": "$rev1",
                "idempotency_key": "$crid2",
                "operations": [],
            },
            "constraints_proposed": [
                {
                    "constraint_id": "loosen_files",
                    "category": "scope",
                    "statement": "Files may be changed freely",
                    "origin": "user",
                    "source_message_refs": ["$msg2"],
                    "suggested": "supersede",
                    "supersedes_constraint_id": "report_only",
                }
            ],
        },
    }
    scenario.script(
        [
            {
                "needs": ["msg1", "crid1"],
                "final_result": _plan_payload_template("msg1", "crid1"),
            },
            {"text": "valid plan recorded"},
            # ONE entry per turn: the scripted-model reservation re-serves
            # the rejected entry on the intake bounded-repair attempt, so
            # the old duplicate entries (repair spares) would now DESYNC.
            {"final_result": invalid_union, "needs": ["msg2"]},
            supersede_user,
        ]
    )
    scenario.spawn()
    try:
        status1, body1 = _submit_turn(env, None, "Plan the maintenance window (p4)")
        assert status1 == 201, body1
        conversation_id = uuid.UUID(body1["conversation_id"])
        scenario.context(
            msg1=str(body1["message_id"]),
            crid1=body1.get("client_request_id", str(uuid.uuid4())),
        )
        view1 = _await_terminal(
            env, uuid.UUID(body1["run_id"]), RUN_TIMEOUT, case_id="P4"
        )
        _check("P4", view1["state"] == "succeeded", f"plan run succeeded: {view1}")
        # Defect-class-A fix (the LATENT instance): the episode id comes
        # from the post-terminal task header — the submit response's
        # task_id is null by contract and never fed a URL again.
        task_id = uuid.UUID(str(_task_header(env, conversation_id)["task_id"]))
        scenario.context(rev1=_task_header(env, conversation_id)["revision"])

        # run 2: broken union -> bounded repair -> rejected, zero effects
        status2, body2 = _submit_turn(env, conversation_id, "do it (p4 invalid)")
        assert status2 == 201, body2
        scenario.context(
            msg2=str(body2["message_id"]),
            crid2=body2.get("client_request_id", str(uuid.uuid4())),
        )
        view2 = _await_terminal(
            env, uuid.UUID(body2["run_id"]), RUN_TIMEOUT, case_id="P4"
        )
        _check("P4", view2["state"] == "failed", f"invalid intake fails the run: {view2}")
        rejection = _psql_scalar(
            env.url,
            "SELECT count(*) FROM job_events WHERE run_id = "
            f"'{body2['run_id']}' AND type = 'agent.intake_interpreted' "
            "AND (data::text LIKE '%\"rejected\": true%')",
        )
        _check("P4", int(rejection) == 1, f"rejected intake event persisted: {rejection}")
        _check(
            "P4",
            scenario.count("effect:fixture.read") == 0
            and scenario.count("effect:fixture.priv") == 0,
            "zero effects on invalid intake",
        )

        # run 3: model-origin supersede of a user constraint -> rejected
        status3, body3 = _submit_turn(
            env, conversation_id, "erase the report-only constraint (p4 supersede)"
        )
        assert status3 == 201, body3
        scenario.context(
            msg2=str(body3["message_id"]),
            crid2=body3.get("client_request_id", str(uuid.uuid4())),
        )
        view3 = _await_terminal(
            env, uuid.UUID(body3["run_id"]), RUN_TIMEOUT, case_id="P4"
        )
        _check(
            "P4", view3["state"] == "failed", f"prohibition erasure rejected: {view3}"
        )
        cons_status, constraints = _api(
            env,
            "GET",
            f"/api/v1/chat/tasks/{task_id}/constraints"
            f"?server_id={env.server_id}",
        )
        statements = [row.get("statement", "") for row in constraints.get("items", [])]
        _check(
            "P4",
            any("Report only" in s for s in statements),
            f"user prohibition still active: {statements}",
        )
        _record("P4", verdict="pass")
    finally:
        scenario.stop()


# ---------------------------------------------------------------------------
# S1 — credential canary never persists anywhere
# ---------------------------------------------------------------------------


@_journal_tails_on_failure
def test_s1_canary_never_persists(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "s1")
    scenario.behavior(
        {
            "fixture.read:mcanary": {
                "status": "succeeded",
                "exit_code": 0,
                "stdout": f"token={CANARY}",
                "stderr": "",
            }
        }
    )
    scenario.script(
        [
            {
                "needs": ["msg1", "crid1"],
                "final_result": _plan_payload_template("msg1", "crid1"),
            },
            {
                "tool_call": {
                    "name": "fixture_read",
                    "args": {"marker": "mcanary"},
                    "id": "s1a",
                }
            },
            {"text": "the check result was recorded"},
        ]
    )
    scenario.spawn()
    try:
        status, body = _submit_turn(env, None, "Run the credential check (s1)")
        assert status == 201, body
        conversation_id = uuid.UUID(body["conversation_id"])
        scenario.context(
            msg1=str(body["message_id"]),
            crid1=body.get("client_request_id", str(uuid.uuid4())),
        )
        run_id = str(body["run_id"])
        view = _await_terminal(
            env, uuid.UUID(run_id), RUN_TIMEOUT, case_id="S1"
        )
        _check("S1", view["state"] == "succeeded", f"run succeeded: {view}")
        sinks = {
            "job_events": (
                "SELECT count(*) FROM job_events WHERE data::text LIKE "
                f"'%{CANARY}%'"
            ),
            "conversation_messages": (
                "SELECT count(*) FROM conversation_messages WHERE content LIKE "
                f"'%{CANARY}%'"
            ),
            "job_steps_receipts": (
                "SELECT count(*) FROM job_steps WHERE receipt::text LIKE "
                f"'%{CANARY}%'"
            ),
            "task_revisions": (
                "SELECT count(*) FROM agent_task_revisions WHERE payload::text "
                f"LIKE '%{CANARY}%'"
            ),
        }
        for sink, sql in sinks.items():
            found = _psql_scalar(env.url, sql)
            _check("S1", int(found) == 0, f"canary absent from {sink}: {found}")
        prompts = json.dumps(scenario.prompts())
        _check("S1", CANARY not in prompts, "canary absent from model history")
        redacted = _psql_scalar(
            env.url,
            "SELECT count(*) FROM job_steps WHERE run_id = "
            f"'{run_id}' AND receipt::text LIKE '%[REDACTED]%'",
        )
        _check(
            "S1",
            int(redacted) >= 1,
            f"redaction marker present in the persisted receipt: {redacted}",
        )
        _record("S1", verdict="pass")
    finally:
        scenario.stop()


# ---------------------------------------------------------------------------
# S2 — unknown effect parks honestly; retry attaches; expiry disclosed
# ---------------------------------------------------------------------------


@_journal_tails_on_failure
def test_s2_unknown_effect_honest(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "s2")
    scenario.behavior({"fixture.priv:munknown": "unknown_status"})
    scenario.script(
        [
            {
                "needs": ["msg1", "crid1"],
                "final_result": _plan_payload_template("msg1", "crid1"),
            },
            {
                "tool_call": {
                    "name": "fixture_priv",
                    "args": {"marker": "munknown"},
                    "id": "s2a",
                }
            },
        ]
    )
    scenario.spawn()
    try:
        status, body = _submit_turn(
            env, None, "Run the reconciler-bound command (s2 unknown)"
        )
        assert status == 201, body
        conversation_id = uuid.UUID(body["conversation_id"])
        crid = body["client_request_id"]
        scenario.context(
            msg1=str(body["message_id"]),
            crid1=body.get("client_request_id", str(uuid.uuid4())),
        )
        run_id = uuid.UUID(body["run_id"])
        view = _await_state(env, run_id, "reconciling", 60.0)
        _check("S2", view["state"] == "reconciling", f"honest parked state: {view}")
        # The park commit is two writes (aobserve_enter_wait: the facade
        # `reconciling` transition, THEN the awaiting_reconciliation event
        # with the deadline) — a state-poll read can land between them.
        # Bounded-wait for the event; the assertion stays exact (one).
        awaiting = 0
        event_deadline = time.monotonic() + 10.0
        while time.monotonic() < event_deadline:
            awaiting = int(
                _psql_scalar(
                    env.url,
                    "SELECT count(*) FROM job_events WHERE run_id = "
                    f"'{run_id}' AND type = 'agent.awaiting_reconciliation'",
                )
                or 0
            )
            if awaiting >= 1:
                break
            time.sleep(0.2)
        _check("S2", awaiting == 1, f"one awaiting-reconciliation event: {awaiting}")
        retry_status, retry_body = _resubmit_turn(
            env,
            conversation_id,
            crid,
            "Run the reconciler-bound command (s2 unknown)",
        )
        _check("S2", retry_status == 200, f"retry attaches: {retry_status}")
        _check(
            "S2",
            scenario.count("begin:fixture.priv:munknown") == 1,
            "the uncertain effect never re-dispatched",
        )
        terminal = _psql_scalar(
            env.url,
            "SELECT count(*) FROM job_events WHERE run_id = "
            f"'{run_id}' AND type = 'agent.completed'",
        )
        _check(
            "S2",
            int(terminal) == 0,
            "no termination claimed while the outcome is unknown",
        )
        _record(
            "S2",
            verdict="pass",
            disclosed="production 300s reconcile expiry not awaited (budget); "
            "disposable fixture dropped instead",
        )
    finally:
        scenario.stop()


# ---------------------------------------------------------------------------
# E1 / E3 — honest outcome projection through real receipts, DE-BATCHED
# (fix 5): each registry row drives its own scenario and worker so one
# row's abort cannot blank its sibling's verdict. Episode ids come from
# the post-terminal task header (fix 1), never the submit response.
# ---------------------------------------------------------------------------


@_journal_tails_on_failure
def test_e1_failed_check_not_verified(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "e1")
    scenario.behavior(
        {
            "fixture.read:mfail": {
                "status": "failed",
                "exit_code": 1,
                "stdout": "",
                "stderr": "designated functional check failed",
            }
        }
    )
    scenario.script(
        [
            {
                "needs": ["msg1", "crid1"],
                "final_result": _plan_payload_template("msg1", "crid1"),
            },
            {
                "tool_call": {
                    "name": "fixture_read",
                    "args": {"marker": "mfail"},
                    "id": "e1a",
                }
            },
            {"text": "the functional check failed; reporting honestly"},
        ]
    )
    scenario.spawn()
    try:
        # E1: process up, designated functional check fails
        status1, body1 = _submit_turn(env, None, "Run the designated check (e1)")
        assert status1 == 201, body1
        conversation1 = uuid.UUID(body1["conversation_id"])
        scenario.context(
            msg1=str(body1["message_id"]),
            crid1=body1.get("client_request_id", str(uuid.uuid4())),
        )
        view1 = _await_terminal(
            env, uuid.UUID(body1["run_id"]), RUN_TIMEOUT, case_id="E1"
        )
        _check("E1", view1["state"] == "succeeded", f"check run succeeded: {view1}")
        failed_step = _psql_scalar(
            env.url,
            "SELECT count(*) FROM job_steps WHERE run_id = "
            f"'{body1['run_id']}' AND state = 'failed'",
        )
        _check(
            "E1",
            int(failed_step) == 1,
            f"failing check recorded as a failed step: {failed_step}",
        )
        header1 = _task_header(env, conversation1)
        _check(
            "E1",
            header1["status"] != "verified",
            f"task not verified after a failed check: {header1['status']}",
        )
        # Defect-class-A fix: episode id from the post-terminal header.
        task_id = uuid.UUID(str(header1["task_id"]))
        items_status, items = _api(
            env,
            "GET",
            f"/api/v1/chat/tasks/{task_id}/items"
            f"?server_id={env.server_id}",
        )
        _check(
            "E1",
            items_status == 200 and len(items.get("items", [])) >= 1,
            f"task items page serves the planned step: {items_status}",
        )
        _record("E1", verdict="pass", task_status=header1["status"])
    finally:
        scenario.stop()


@_journal_tails_on_failure
def test_e3_wrapper_success_domain_failure(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "e3")
    scenario.behavior(
        {
            "fixture.read:mwrap": {
                "status": "succeeded",
                "exit_code": 0,
                # Production _subcheck_page (tools.py) accepts ONLY a
                # non-empty string name plus a boolean passed; any other
                # entry shape makes the whole section untrustworthy and
                # fail-closes the domain outcome to unknown. The wrapper
                # success must carry its domain failure in the shape
                # production actually parses.
                "subchecks": [{"name": "svc_active", "passed": False}],
            }
        }
    )
    scenario.script(
        [
            {
                "needs": ["msg1", "crid1"],
                "final_result": _plan_payload_template("msg1", "crid1"),
            },
            {
                "tool_call": {
                    "name": "fixture_read",
                    "args": {"marker": "mwrap"},
                    "id": "e3a",
                }
            },
            {"text": "wrapper exited zero but the subcheck failed"},
        ]
    )
    scenario.spawn()
    try:
        # E3: wrapper exit zero + failing subcheck -> domain failure
        status2, body2 = _submit_turn(env, None, "Run the wrapped check (e3)")
        assert status2 == 201, body2
        scenario.context(
            msg1=str(body2["message_id"]),
            crid1=body2.get("client_request_id", str(uuid.uuid4())),
        )
        view2 = _await_terminal(
            env, uuid.UUID(body2["run_id"]), RUN_TIMEOUT, case_id="E3"
        )
        _check("E3", view2["state"] == "succeeded", f"wrapped run succeeded: {view2}")
        receipt = _psql_scalar(
            env.url,
            "SELECT receipt::text FROM job_steps WHERE run_id = "
            f"'{body2['run_id']}' AND capability_id = 'fixture.read' "
            "ORDER BY seq LIMIT 1",
        )
        _check(
            "E3",
            receipt is not None and '"subchecks"' in receipt,
            f"persisted envelope carries the subcheck evidence: {str(receipt)[:200]}",
        )
        # The domain projection lives on the ToolResult seam (the frozen
        # model-facing document), NOT in the value-free dispatcher receipt.
        # Grade it at the real production seam, fed with the REAL
        # persisted executor envelope from the receipt above.
        from mai.agents.tools import (
            TOOL_RESULT_SCHEMA_VERSION,
            ToolResult,
            ToolStatus,
            project_tool_result,
        )

        envelope = json.loads(receipt)["response"]
        tool_result = ToolResult(
            schema_version=TOOL_RESULT_SCHEMA_VERSION,
            capability_id="fixture.read",
            server_id=env.server_id,
            run_id=uuid.UUID(body2["run_id"]),
            status=ToolStatus.EXECUTED,
            allowed=True,
            reason="allowed",
            effect="executed",
            payload_hash="non_canonical",
            policy_revision=None,
            effect_class="read",
            approval_id=None,
            approval_spent=False,
            approval_code=None,
            executor_ok=True,
            output_text=json.dumps(envelope, sort_keys=True),
            output_truncated=False,
            output_omitted_bytes=0,
            error_code=None,
            error_detail=None,
        )
        projection = project_tool_result(tool_result)
        _check(
            "E3",
            projection.get("domain_outcome") == "failure",
            f"failed subcheck projects the domain failure: {projection.get('domain_outcome')}",
        )
        _check(
            "E3",
            projection.get("effect_certainty") != "certain",
            f"wrapper success is never promoted to certain success: {projection.get('effect_certainty')}",
        )
        header2 = _task_header(env, uuid.UUID(body2["conversation_id"]))
        _check(
            "E3",
            header2["status"] != "verified",
            "domain failure is not rendered as task verification",
        )
        _record("E3", verdict="pass")
    finally:
        scenario.stop()


# ---------------------------------------------------------------------------
# E2 — fabricated evidence rejected at the real completion seam
# ---------------------------------------------------------------------------


@_journal_tails_on_failure
def test_e2_fabricated_evidence_rejected(harness: _Harness) -> None:
    from mai.agents.task_completion import (
        CompletionProposal,
        EvidenceCertainty,
        afinalize_completion,
    )
    from mai.agents.task_store import aget_task

    env = harness
    seed = _seed_task_via_intake(env, "Seed the completion task (e2)")
    task_id = uuid.UUID(str(seed["task_id"]))
    conversation_id = uuid.UUID(str(seed["conversation_id"]))
    revision0 = int(_task_header(env, conversation_id)["revision"])

    def _zero_effects(document: dict[str, Any], label: str) -> None:
        """Every E2 rejection has ZERO effects: no receipt, no revision
        bump, no criterion advance (task_store.apropose_completion and the
        module-level certainty gate both fail closed before any write)."""
        header = _task_header(env, conversation_id)
        _check(
            "E2",
            header["status"] != "verified",
            f"{label}: task stays unverified: {header['status']}",
        )
        _check(
            "E2",
            int(_task_header(env, conversation_id)["revision"]) == revision0,
            f"{label}: no revision bump (revision == {revision0})",
        )
        receipts = _psql_scalar(
            env.url,
            "SELECT count(*) FROM agent_task_completions "
            f"WHERE task_id = '{task_id}'",
        )
        _check("E2", int(receipts) == 0, f"{label}: no completion receipt persisted: {receipts}")
        bumps = _psql_scalar(
            env.url,
            "SELECT count(*) FROM agent_task_revisions "
            f"WHERE task_id = '{task_id}' AND kind = 'completion'",
        )
        _check("E2", int(bumps) == 0, f"{label}: no completion revision rows: {bumps}")
        passed = _psql_scalar(
            env.url,
            "SELECT count(*) FROM agent_task_criteria "
            f"WHERE task_id = '{task_id}' AND verification = 'passed'",
        )
        _check("E2", int(passed) == 0, f"{label}: no criterion advanced to passed: {passed}")
        _check(
            "E2",
            document.get("recorded") is False,
            f"{label}: proposal rejected (recorded == false): {document}",
        )

    async def _propose(criterion_refs: list[dict[str, Any]], certainty: list[EvidenceCertainty]) -> dict[str, Any]:
        task = await aget_task(
            env.app.state.session_factory, task_id, server_id=env.server_id
        )
        proposal = CompletionProposal(
            schema_version=1,
            task_id=task_id,
            base_revision=task.revision,
            idempotency_key=uuid.uuid4(),
            execution_status="succeeded",
            domain_outcome="success",
            effect_certainty="certain",
            criterion_results=[
                {
                    "criterion_id": "done",
                    "result": "passed",
                    "evidence_refs": criterion_refs,
                }
            ],
            evidence_certainty=certainty,
        )
        observation = await afinalize_completion(
            env.app.state.session_factory,
            server_id=env.server_id,
            proposal=proposal,
        )
        return observation.document()

    # -- variant 1: FABRICATED evidence id (exists nowhere) ------------------
    # The proposal CLAIMS certain success for the fabricated ref, so the
    # module-level certainty gate passes and the STORE's existence check
    # (validate_completion_verdict against the task-scoped evidence
    # registry) is the seam under test: unknown_evidence_ref, zero effects.
    fabricated_id = str(uuid.uuid4())
    document = _run(
        _propose(
            [{"kind": "observation", "evidence_id": fabricated_id}],
            [
                EvidenceCertainty(
                    evidence_id=fabricated_id,
                    domain_outcome="success",
                    effect_certainty="certain",
                )
            ],
        )
    )
    _zero_effects(document, "fabricated id")
    _check(
        "E2",
        "unknown_evidence_ref" in (document.get("rejection_fields") or {}).values(),
        f"fabricated id rejected value-free at the store: {document.get('rejection_fields')}",
    )

    # -- variant 2: WRONG-TARGET evidence (REAL record, FOREIGN scope) -------
    # A conversation_messages id that genuinely exists — but belongs to a
    # DIFFERENT conversation. The store's source predicate is
    # conversation-scoped (_EVIDENCE_SOURCE_SQL MESSAGE:
    # 'WHERE message_id = :eid AND conversation_id = :cid') and the
    # completion verdict validates against the TASK-scoped evidence
    # registry, so the foreign record is unknown_evidence_ref, never
    # silently accepted.
    foreign = _seed_task_via_intake(
        env, "Seed the FOREIGN conversation task (e2 wrong-target)"
    )
    foreign_message_id = str(foreign["message_id"])
    assert foreign["conversation_id"] != str(conversation_id)
    document = _run(
        _propose(
            [{"kind": "message", "evidence_id": foreign_message_id}],
            [
                EvidenceCertainty(
                    evidence_id=foreign_message_id,
                    domain_outcome="success",
                    effect_certainty="certain",
                )
            ],
        )
    )
    _zero_effects(document, "wrong-target id")
    _check(
        "E2",
        "unknown_evidence_ref" in (document.get("rejection_fields") or {}).values(),
        f"wrong-target (foreign-conversation) record rejected: {document.get('rejection_fields')}",
    )

    # -- variant 3: STALE evidence certainty (recorded FAILURE backing a
    # passed claim) ----------------------------------------------------------
    # The proposal copies the REAL EvidenceCertainty record shape; a
    # recorded domain failure can never support a passed claim
    # (task_completion._supports_passed: only certain+success does), so
    # the module-level gate rejects with certainty_incompatible BEFORE any
    # store write.
    stale_id = str(uuid.uuid4())
    document = _run(
        _propose(
            [{"kind": "observation", "evidence_id": stale_id}],
            [
                EvidenceCertainty(
                    evidence_id=stale_id,
                    domain_outcome="failure",
                    effect_certainty="certain",
                )
            ],
        )
    )
    _zero_effects(document, "stale certainty")
    _check(
        "E2",
        "certainty_incompatible" in (document.get("rejection_fields") or {}).values(),
        f"stale certainty (recorded failure vs passed claim) rejected: {document.get('rejection_fields')}",
    )
    _check(
        "E2",
        document.get("rejection_code") == "completion_missing_evidence",
        f"rejection carries the stable code: {document.get('rejection_code')}",
    )
    _record(
        "E2",
        verdict="pass",
        fabricated=dict(document.get("rejection_fields") or {}),
        variants=["fabricated", "wrong_target", "stale_certainty"],
    )


# ---------------------------------------------------------------------------
# I1 / I2 — bounded no-progress signals vs honest polling, DE-BATCHED
# (fix 5): I1 drives its own workers; I2 is a pure in-process row with its
# own test, so an I1 worker abort can never blank the I2 verdict.
# ---------------------------------------------------------------------------


@_journal_tails_on_failure
def test_i1_bounded_no_progress_hints(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "i1", denied=["fixture.read"])
    scenario.script(
        [
            {
                "needs": ["msg1", "crid1"],
                "final_result": _plan_payload_template("msg1", "crid1"),
            },
            {"tool_call": {"name": "fixture_read", "args": {"marker": "d1"}, "id": "i1"}},
            {"tool_call": {"name": "fixture_read", "args": {"marker": "d1"}, "id": "i2"}},
            {"tool_call": {"name": "fixture_read", "args": {"marker": "d1"}, "id": "i3"}},
            {"text": "changing strategy after repeated identical failures"},
        ]
    )
    flat = Scenario(env, "i1-flat")
    flat.behavior({"fixture.read": {"status": "succeeded", "exit_code": 0,
                                    "stdout": "{\"poll\": \"unchanged\"}"}})
    flat.script(
        [
            {
                "needs": ["msg1", "crid1"],
                "final_result": _plan_payload_template("msg1", "crid1"),
            },
            # Six polls of ONE equivalent operation: the flat-poll signal
            # judges the trailing run of the SAME request (capability +
            # canonical payload hash), so every poll must carry the same
            # payload — only the tool_call id differs per round. Distinct
            # markers would make each poll a different request and the
            # streak could never reach the FLAT_POLL bound.
            *[
                {
                    "tool_call": {
                        "name": "fixture_read",
                        "args": {"marker": "q"},
                        "id": f"p{i}",
                    }
                }
                for i in range(1, 7)
            ],
            {"text": "polling finished"},
        ]
    )
    scenario.spawn()
    try:
        # I1a: three identical denials -> one labelled hint, consumed once
        status, body = _submit_turn(
            env, None, "Search for the missing service (i1 repeated)"
        )
        assert status == 201, body
        scenario.context(
            msg1=str(body["message_id"]),
            crid1=body.get("client_request_id", str(uuid.uuid4())),
        )
        view_i1 = _await_terminal(
            env, uuid.UUID(body["run_id"]), RUN_TIMEOUT, case_id="I1"
        )
        _check("I1", view_i1["state"] == "succeeded", f"denial run succeeded: {view_i1}")
        hint_rounds = [
            dump for dump in scenario.prompts()
            if any(
                isinstance(part.get("content"), str)
                and part.get("content").startswith("[Progress hint]:")
                for message in dump
                for part in message.get("parts", [])
            )
        ]
        _check(
            "I1",
            len(hint_rounds) == 1,
            f"exactly one round received the hint: {len(hint_rounds)}",
        )
        hint_text = next(
            part["content"]
            for dump in hint_rounds
            for message in dump
            for part in message.get("parts", [])
            if isinstance(part.get("content"), str)
            and part["content"].startswith("[Progress hint]:")
        )
        for phrase in ("stall", "stalled", "failed", "failure", "does not exist"):
            _check(
                "I1",
                phrase not in hint_text.lower(),
                f"hint never asserts {phrase!r}",
            )
        # The three denials live in the TOOL vocabulary (ToolResult:
        # status='denied', effect='not_attempted'), NOT in job_steps.state
        # — the frozen step state machine (contracts/jobs.py StepState:
        # pending/running/succeeded/failed/cancelled/outcome_unknown/
        # cancel_requested) has no 'denied' state, and a policy refusal
        # happens pre-dispatch, so no denied step row can ever exist.
        # Grade them where they actually live: the hint round's executor
        # history carries exactly three fixture.read tool-returns, all
        # the SAME pre-dispatch refusal (identical payload hash —
        # equivalent queries), zero effects ran, and the delivered hint
        # names the capability, the query fingerprint and the refusal.
        denial_returns = []
        for message in hint_rounds[0]:
            for part in message.get("parts", []):
                if part.get("part_kind") != "tool-return":
                    continue
                content = part.get("content")
                document = (
                    json.loads(content) if isinstance(content, str) else content
                )
                if (
                    isinstance(document, dict)
                    and document.get("capability_id") == "fixture.read"
                ):
                    denial_returns.append(document)
        _check(
            "I1",
            len(denial_returns) == 3,
            f"three real denial tool-results before the hint: {len(denial_returns)}",
        )
        _check(
            "I1",
            bool(denial_returns)
            and all(
                document.get("status") == "denied"
                and document.get("reason") == "capability_denied_by_policy"
                and document.get("effect") == "not_attempted"
                and document.get("executor_ok") is False
                and document.get("output") is None
                for document in denial_returns
            ),
            "every refusal is the honest pre-dispatch denial "
            "(not_attempted, no executor output — nothing ran)",
        )
        _check(
            "I1",
            len({document.get("payload_hash") for document in denial_returns}) == 1,
            "the three denials are equivalent queries (identical payload hash)",
        )
        applied = _psql_scalar(
            env.url,
            "SELECT count(*) FROM job_steps WHERE run_id = "
            f"'{body['run_id']}' AND state = 'succeeded'",
        )
        _check(
            "I1",
            int(applied) == 0,
            "zero applied (succeeded) steps for the denial run — the "
            f"refusals were pre-dispatch (not_attempted): {applied}",
        )
        _check(
            "I1",
            "fixture.read" in hint_text
            and "query fingerprint" in hint_text
            and "capability_denied_by_policy" in hint_text,
            "the hint carries the capability, the fingerprint and the refusal",
        )

        # I1b: five unchanged executed polls stay silent; the note at the
        # bound describes 'unchanged' without stall language.
        # SERIALIZE the two sub-arcs: all workers listen on ONE DBOS queue,
        # so a second live worker could claim the OTHER scenario's run and
        # execute it against the wrong script (the observed cross-claim).
        # The denied-scenario worker is drained and stopped first; only
        # then does the flat scenario's worker spawn.
        scenario.stop()
        flat.spawn()
        status_f, body_f = _submit_turn(
            env, None, "Poll the async operation (i1 flat)"
        )
        assert status_f == 201, body_f
        flat.context(
            msg1=str(body_f["message_id"]),
            crid1=body_f.get("client_request_id", str(uuid.uuid4())),
        )
        view_flat = _await_terminal(
            env, uuid.UUID(body_f["run_id"]), RUN_TIMEOUT, case_id="I1"
        )
        _check("I1", view_flat["state"] == "succeeded", f"flat run succeeded: {view_flat}")
        flat_hints = [
            dump for dump in flat.prompts()
            if any(
                isinstance(part.get("content"), str)
                and part.get("content").startswith("[Progress hint]:")
                for message in dump
                for part in message.get("parts", [])
            )
        ]
        _check(
            "I1",
            len(flat_hints) == 1,
            f"flat-poll note appears once at the bound: {len(flat_hints)}",
        )
        executed = _psql_scalar(
            env.url,
            "SELECT count(*) FROM job_steps WHERE run_id = "
            f"'{body_f['run_id']}' AND state = 'succeeded'",
        )
        _check(
            "I1",
            int(executed) == 6,
            f"bounded polling executed for real (not a stall): {executed}",
        )
        note_text = next(
            part["content"]
            for dump in flat_hints
            for message in dump
            for part in message.get("parts", [])
            if isinstance(part.get("content"), str)
            and part["content"].startswith("[Progress hint]:")
        )
        _check(
            "I1",
            "unchanged" in note_text.lower(),
            f"flat-poll note describes unchanged output: {note_text[:120]}",
        )
        _record("I1", verdict="pass", hint=hint_text[:120])
    finally:
        scenario.stop()
        flat.stop()


def _tool_result_document(output_text: str) -> str:
    """One frozen-shape ToolResult document (act 19-tool-results).

    ``task_progress.observation_from_result`` folds exactly THIS document
    (one argument; capability/status/payload-hash/output live in it).
    """
    return json.dumps(
        {
            "schema_version": 1,
            "capability_id": "fixture.read",
            "status": "executed",
            "allowed": True,
            "reason": "allowed",
            "effect": "executed",
            "payload_hash": "a" * 64,
            "output": {
                "text": output_text,
                "truncated": False,
                "omitted_bytes": 0,
            },
        }
    )


@_journal_tails_on_failure
def test_i2_derived_signals_scope_not_authority(harness: _Harness) -> None:
    env = harness
    # I2: new evidence (changed output) is never a stall; derived signal
    # documents carry scope+fingerprint+disclaimer, never authority.
    # Pure in-process row: no worker, no HTTP, no script to desync.
    from mai.agents import task_progress

    observations = [
        task_progress.observation_from_result(_tool_result_document("[]")),
        task_progress.observation_from_result(_tool_result_document("[]")),
        task_progress.observation_from_result(_tool_result_document("[]")),
    ]
    empty_doc = task_progress.derive_progress_signal(observations)
    _check(
        "I2",
        empty_doc is not None and empty_doc.to_document().get("kind") == "repeated_empty",
        "repeated-empty derivation yields the bounded kind",
    )
    empty_json = json.dumps(empty_doc.to_document())
    for phrase in ("does not exist", "doesn't exist", "no results exist"):
        _check(
            "I2",
            phrase not in empty_json.lower(),
            f"signal never asserts nonexistence via {phrase!r}",
        )
    doc = empty_doc.to_document()
    _check(
        "I2",
        "not proof of absence" in doc.get("hint", "").lower()
        and doc.get("scope") == ["fixture.read"]
        and doc.get("fingerprint"),
        "source scope, fingerprint and uncertainty disclaimer present",
    )
    _check(
        "I2",
        not ({"policy", "authority", "approved"} & set(doc)),
        "derived signals carry no policy authority",
    )
    changed = task_progress.derive_progress_signal(
        [
            observations[0],
            observations[1],
            task_progress.observation_from_result(
                _tool_result_document("{\"now\": 2}")
            ),
        ]
    )
    _check(
        "I2",
        changed is not None and changed.to_document().get("kind") == "new_evidence",
        "changed outputs between equivalent polls derive the dedicated "
        "new_evidence kind, never a stall "
        f"(derived kind: {None if changed is None else changed.to_document().get('kind')})",
    )
    _record("I2", verdict="pass")


# ---------------------------------------------------------------------------
# X1 — representative tasks across domains, graded by outcome only
# ---------------------------------------------------------------------------


def _observation_of(
    env: _Harness, scenario: Scenario, body: dict[str, Any], conversation_id: uuid.UUID
) -> dict[str, Any]:
    """The graded outcome observation: outcome/constraint/evidence fields
    ONLY — the ordered tool-call list is deliberately not an input."""
    view = _run_view(env, uuid.UUID(body["run_id"]))
    status, header = _api(
        env,
        "GET",
        f"/api/v1/chat/conversations/{conversation_id}/task"
        f"?server_id={env.server_id}",
    )
    # A missing episode is a graded outcome (recorded as None), never an
    # abort that would blank the whole X1 row's verdict.
    task_id: uuid.UUID | None
    if status == 200:
        task_id = uuid.UUID(str(header["task_id"]))
    else:
        task_id = None
    return {
        "terminal_state": view["state"],
        "task_status": header.get("status") if status == 200 else None,
        "task_revision_gt_0": status == 200 and header["revision"] >= 1,
        "plan_step_recorded": (
            len(
                _api(
                    env,
                    "GET",
                    f"/api/v1/chat/tasks/{task_id}/items"
                    f"?server_id={env.server_id}",
                )[1].get("items", [])
            )
            >= 1
        )
        if task_id is not None
        else False,
        "privilege_effects": scenario.count("effect:fixture.priv"),
        "constraint_effects_beyond_plan": 0,
    }


@_journal_tails_on_failure
def test_x1_outcome_graded_not_sequence(harness: _Harness) -> None:
    env = harness
    scenario = Scenario(env, "x1")
    scenario.behavior(
        {
            "fixture.priv:xsvc": {
                "status": "succeeded",
                "exit_code": 0,
                "stdout": "active",
            },
            "fixture.priv:xsvc2": {
                "status": "succeeded",
                "exit_code": 0,
                "stdout": "active",
            },
            "fixture.read:xdb": {
                "status": "succeeded",
                "exit_code": 0,
                "subchecks": [
                    {"name": "row_count", "status": "passed"},
                    {"name": "fk_integrity", "status": "passed"},
                ],
            },
            "fixture.read:xdb2": {
                "status": "succeeded",
                "exit_code": 0,
                "subchecks": [
                    {"name": "row_count", "status": "passed"},
                    {"name": "fk_integrity", "status": "passed"},
                ],
            },
        }
    )
    scenario.script(
        [
            # service operation, repeat A
            {"needs": ["msg_svc_a", "crid_svc_a"],
             "final_result": _plan_payload_template("msg_svc_a", "crid_svc_a")},
            {"tool_call": {"name": "fixture_priv", "args": {"marker": "xsvc"}, "id": "x1a"}},
            {"text": "service operation completed"},
            # research document read, repeat A
            {"needs": ["msg_res_a", "crid_res_a"],
             "final_result": _plan_payload_template("msg_res_a", "crid_res_a")},
            {"tool_call": {"name": "fixture_read", "args": {"marker": "xdoc"}, "id": "x1b"}},
            {"text": "research read completed"},
            # database invariant, repeat A
            {"needs": ["msg_db_a", "crid_db_a"],
             "final_result": _plan_payload_template("msg_db_a", "crid_db_a")},
            {"tool_call": {"name": "fixture_read", "args": {"marker": "xdb"}, "id": "x1c"}},
            {"text": "invariants passed"},
            # file/repo inspection, repeat A
            {"needs": ["msg_file_a", "crid_file_a"],
             "final_result": _plan_payload_template("msg_file_a", "crid_file_a")},
            {"tool_call": {"name": "fixture_read", "args": {"marker": "xfile"}, "id": "x1d"}},
            {"text": "repo inspection completed"},
            # service operation, repeat B (DIFFERENT marker -> different sequence)
            {"needs": ["msg_svc_b", "crid_svc_b"],
             "final_result": _plan_payload_template("msg_svc_b", "crid_svc_b")},
            {"tool_call": {"name": "fixture_priv", "args": {"marker": "xsvc2"}, "id": "x1e"}},
            {"text": "service operation completed"},
            # research document read, repeat B
            {"needs": ["msg_res_b", "crid_res_b"],
             "final_result": _plan_payload_template("msg_res_b", "crid_res_b")},
            {"tool_call": {"name": "fixture_read", "args": {"marker": "xdoc2"}, "id": "x1f"}},
            {"text": "research read completed"},
            # database invariant, repeat B
            {"needs": ["msg_db_b", "crid_db_b"],
             "final_result": _plan_payload_template("msg_db_b", "crid_db_b")},
            {"tool_call": {"name": "fixture_read", "args": {"marker": "xdb2"}, "id": "x1g"}},
            {"text": "invariants passed"},
            # file/repo inspection, repeat B
            {"needs": ["msg_file_b", "crid_file_b"],
             "final_result": _plan_payload_template("msg_file_b", "crid_file_b")},
            {"tool_call": {"name": "fixture_read", "args": {"marker": "xfile2"}, "id": "x1h"}},
            {"text": "repo inspection completed"},
        ]
    )
    scenario.spawn()
    domains = (
        ("service_operation", "svc"),
        ("research", "res"),
        ("database_invariant", "db"),
        ("file_repo", "file"),
    )
    graded: dict[str, dict[str, Any]] = {}
    try:
        for repeat in ("a", "b"):
            for domain, key in domains:
                message = f"Run the {domain.replace('_', ' ')} task (x1 {repeat})"
                status, body = _submit_turn(env, None, message)
                assert status == 201, body
                conversation_id = uuid.UUID(body["conversation_id"])
                scenario.context(
                    **{
                        f"msg_{key}_{repeat}": str(body["message_id"]),
                        f"crid_{key}_{repeat}": body.get(
                            "client_request_id", str(uuid.uuid4())
                        ),
                    }
                )
                _await_terminal(
                    env, uuid.UUID(body["run_id"]), RUN_TIMEOUT, case_id="X1"
                )
                graded[f"{domain}:{repeat}"] = _observation_of(
                    env, scenario, body, conversation_id
                )
        for domain, _key in domains:
            observation_a = graded[f"{domain}:a"]
            observation_b = graded[f"{domain}:b"]
            _check(
                "X1",
                observation_a == observation_b,
                f"{domain}: outcomes grade identically across repeats "
                f"({observation_a} vs {observation_b})",
            )
            _check(
                "X1",
                observation_a["terminal_state"] == "succeeded",
                f"{domain}: representative task completed",
            )
        _check(
            "X1",
            all(obs["privilege_effects"] <= 1 for obs in graded.values()),
            "constraint-following: privileged effects bounded by the plan",
        )
        _record(
            "X1",
            verdict="pass",
            graded=graded,
            note="grader input excludes the ordered tool-call list; repeats "
            "use different effect markers so textual sequences differ",
        )
    finally:
        scenario.stop()


# ---------------------------------------------------------------------------
# Live-provider legs (act 23d, item F): budgeted provider-quality evidence
# through the REAL intake seam, inside a disposable marked container.
#
# The production credential (MAI_GATEWAY_KEY) only resolves inside the
# compose network's env-file mount, so the leg runner is executed in a
# disposable ``mai-backend`` container (``--user 0:0`` — the mounted env
# file is root-owned 0600) attached to the infra network where
# ``http://litellm:4000/v1`` answers. The credential value crosses the
# boundary ONLY as the read-only bind mount into the production
# EnvResolver; it is never read by this harness, never printed, and never
# written to a repo/evidence file.
# ---------------------------------------------------------------------------

#: Host path of the compose credential env-file (bind source only; the
#: harness NEVER reads this file — it only hands the path to docker).
LIVE_ENV_FILE = os.environ.get(
    "MAI_ACT_LIVE_ENV_FILE",
    "/home/megastruktur/data/machina/mai-credentials.env",
)
#: Docker-visible work root (/tmp is mount-namespaced on this host).
LIVE_WORK_ROOT = Path(
    os.environ.get("MAI_ACT_LIVE_WORK_ROOT", "/home/megastruktur/act-evals-live")
)
LIVE_IMAGE = "mai-backend:latest"
LIVE_TIMEOUT_SECONDS = 600.0

#: Conservative reservation for z-ai-coding/glm-5.3-flash: the model has
#: NO published cost in the proxy, so the reservation uses the HIGHER of
#: the two published per-token rates (glm-5.3: input 1.4e-06/tok, output
#: 4.4e-06/tok) over the OBSERVED per-leg usage — an upper-bound
#: reservation, never a measured price.
LIVE_RATES_NOTE = (
    "glm-5.3-flash has no published proxy cost; reserved at the higher "
    "published rates (glm-5.3: input 1.4e-06/tok, output 4.4e-06/tok) "
    "over the OBSERVED per-leg usage (upper bound, never a price)"
)
LIVE_INPUT_RATE = 1.4e-06
LIVE_OUTPUT_RATE = 4.4e-06

#: Legs whose observable is MODEL COMPETENCE (registry P1/P2), judged
#: outcome-level. Plumbing rows (worker/store/UI) stay deterministic-only.
LIVE_LEG_CASES = ("P1", "P2")

#: provider-quality leg records consumed by the scorecard
#: (requests / wall_clock_minutes / reserved_spend_usd per leg).
PROVIDER_LEGS: list[dict[str, Any]] = []

#: case_id -> (live_measured, skip_reason). Rows absent from LIVE_LEG_CASES
#: stay deterministic-only with the default reason below.
LIVE_FLAGS: dict[str, tuple[bool, str | None]] = {}

LIVE_SKIP_DEFAULT = (
    "not in the ratified live subset (P1/P2 model-competence legs); the "
    "production credential resolves only inside the compose network, so "
    "host-side worker subprocesses cannot dial the live endpoint — the "
    "deterministic scripted leg proves the plumbing at the real seams and "
    "no provider-quality claim is made"
)

LIVE_RUNNER_SOURCE = '''
"""Budgeted live-provider leg runner (act 23d).

Runs INSIDE a disposable marked container on the compose infra network.
The production credential is mounted read-only at
/run/secrets/mai-credentials.env and resolved through the production
EnvResolver — this script never parses, prints or persists it. The live
model drives the REAL intake seam (final_result/TurnInterpretation output
tool via mai.agents.intake.ainterpret_turn), never a raw provider call.
Every leg is bounded by the ratified caps; the runner stops at the first
bound. Output: /work/legs.json (value-free; no credential material).
"""
import asyncio
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

BASE_URL = os.environ.get("LIVE_BASE_URL", "http://litellm:4000/v1")
MODEL_ID = os.environ.get("LIVE_MODEL", "z-ai-coding/glm-5.3-flash")
ENV_FILE = os.environ.get("LIVE_ENV_FILE", "/run/secrets/mai-credentials.env")
CAPS = {
    "requests": int(os.environ.get("LIVE_CAP_REQUESTS", "60")),
    "wall_clock_minutes": float(os.environ.get("LIVE_CAP_MINUTES", "30")),
    "reserved_spend_usd": float(os.environ.get("LIVE_CAP_USD", "5")),
}
INPUT_RATE = 1.4e-06
OUTPUT_RATE = 4.4e-06
RATES_NOTE = (
    "glm-5.3-flash has no published proxy cost; reserved at the higher "
    "published rates (glm-5.3: input 1.4e-06/tok, output 4.4e-06/tok) "
    "over the OBSERVED per-leg usage (upper bound, never a price)"
)
#: z-ai-coding/glm-5.3-flash is a REASONING model: measured against the
#: P2 prompt, max_tokens=256 is consumed entirely by reasoning tokens
#: (finish_reason 'length', no tool call); 2048 lets the structured
#: final_result tool call land (~50s per call at that budget).
MAX_TOKENS = 2048

LEGS = [
    {
        "case": "P1",
        "leg": "compact structured answer via the real intake seam",
        "prompt": "Say hello and tell me what you can do in one short paragraph.",
        "judge": "answer",
    },
    {
        "case": "P2",
        "leg": "missing prerequisite surfaces as questions/assumptions, not facts",
        "prompt": (
            "Set up a replicated PostgreSQL cluster across two brand-new "
            "production servers and fail over to the replica if the primary "
            "dies. Note: I have not given you any server addresses, "
            "credentials or SSH access yet."
        ),
        "judge": "assumptions",
    },
]


def _build_model():
    from mai.agents.model_store import ModelConfigStore
    from mai.credentials.env import build_runtime_env_resolver

    root = Path(tempfile.mkdtemp(prefix="live-model-root-"))
    store = ModelConfigStore(root)
    store.write_connection(
        "gateway",
        json.dumps(
            {
                "schema_version": 1,
                "name": "gateway",
                "base_url": BASE_URL,
                "credential_ref": "MAI_GATEWAY_KEY",
                "timeout_seconds": 240,
                "capabilities": {"tool_calls": True},
            }
        )
        + "\\n",
        expected_revision=0,
    )
    store.write_profile(
        "flash",
        json.dumps(
            {
                "schema_version": 1,
                "name": "flash",
                "connection": "gateway",
                "model": MODEL_ID,
                "max_tokens": MAX_TOKENS,
            }
        )
        + "\\n",
        expected_revision=0,
    )
    store.set_selection("gateway", "flash", expected_revision=0)
    binding = store.bind("gateway", "flash")
    resolver = build_runtime_env_resolver(ENV_FILE, None)
    return binding.rebuild(resolver=resolver).model


def _snapshot(content):
    from mai.agents.intake import IntakeSnapshot
    from mai.contracts.conversations import MessageRole

    return IntakeSnapshot(
        server_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        source_message_id=uuid.uuid4(),
        source_message_role=MessageRole.USER,
        source_message_content=content,
        transcript=((MessageRole.USER, content),),
        transcript_omitted_older=False,
        task_id=None,
        task_revision=None,
        task_phase=None,
        task_title=None,
        steps=(),
        steps_omitted=False,
        constraints=(),
        constraints_omitted=False,
    )


def _clip(value, limit=200):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _judge(entry, turn, record):
    kind = str(getattr(turn, "turn_kind", "?"))
    questions = list(getattr(turn, "open_questions", []) or [])
    assumptions = list(getattr(turn, "assumptions", []) or [])
    observed = {
        "turn_kind": kind,
        "model_calls": record.model_calls,
        "attempts": record.attempts,
        "repaired": record.repaired,
        "function_tools": record.function_tools,
        "open_questions": len(questions),
        "assumptions": len(assumptions),
    }
    if entry["judge"] == "answer":
        answer = getattr(turn, "answer_text", None)
        observed["answer_chars"] = len(answer) if isinstance(answer, str) else 0
        observed["answer_excerpt"] = _clip(answer or "")
        satisfied = (
            kind == "answer"
            and isinstance(answer, str)
            and 0 < len(answer) <= 1500
            and record.model_calls == 1
            and record.repaired is False
        )
        detail = (
            "one-call compact answer turn" if satisfied else
            f"not a one-call compact answer (kind={kind}, "
            f"chars={observed['answer_chars']}, calls={record.model_calls}, "
            f"repaired={record.repaired})"
        )
        return satisfied, detail, observed
    satisfied = bool(questions) and kind != "answer"
    if kind in ("plan", "new_task"):
        satisfied = satisfied and bool(assumptions)
    observed["first_question"] = _clip(
        getattr(questions[0], "question", None) if questions else None
    )
    detail = (
        "missing prerequisite recorded as unverified (open question"
        + (" + explicit assumption)" if assumptions else ")")
        if satisfied
        else f"missing prerequisite not surfaced as unverified "
        f"(kind={kind}, questions={len(questions)}, "
        f"assumptions={len(assumptions)})"
    )
    return satisfied, detail, observed


async def _run_all(model, entries, used):
    from mai.agents.intake import IntakeRejection, ainterpret_turn
    from pydantic_ai import capture_run_messages

    def _observed_usage(messages):
        """REAL token counts from the captured model messages (sum over
        every model response this intake interpretation produced)."""
        try:
            total_in = 0
            total_out = 0
            for message in messages or ():
                usage = getattr(message, "usage", None)
                if usage is None:
                    continue
                total_in += int(getattr(usage, "input_tokens", 0) or 0)
                total_out += int(getattr(usage, "output_tokens", 0) or 0)
            return {"input_tokens": total_in, "output_tokens": total_out}
        except Exception:  # noqa: BLE001 - usage is best-effort accounting
            return None

    legs = []
    for entry in entries:
        if (
            used["requests"] >= CAPS["requests"]
            or used["minutes"] >= CAPS["wall_clock_minutes"]
            or used["usd"] >= CAPS["reserved_spend_usd"]
        ):
            used["stopped"] = (
                f"budget bound reached before this leg "
                f"(requests={used['requests']}, "
                f"minutes={used['minutes']:.2f}, "
                f"usd={used['usd']:.4f})"
            )
            break
        requests = 0
        measured_failure = False
        t0 = time.monotonic()
        try:
            with capture_run_messages() as captured:
                turn, record = await ainterpret_turn(
                    model, _snapshot(entry["prompt"])
                )
            requests = int(record.model_calls)
            satisfied, detail, observed = _judge(entry, turn, record)
            usage = _observed_usage(captured)
            if usage is not None:
                observed["usage"] = usage
            error = None
        except IntakeRejection as exc:
            # The live model failed to produce a schema-valid
            # TurnInterpretation within the bounded repair budget: a
            # REPORTED provider-quality failure, never a hidden skip. The
            # attempts DID hit the provider and are counted.
            requests = int(getattr(exc, "model_calls", 0) or 0)
            satisfied = False
            measured_failure = requests > 0
            detail = (
                "intake rejected the live model's interpretation after "
                f"bounded repair (provider-quality failure): {dict(exc.fields)}"
            )
            observed = {
                "attempts": getattr(exc, "attempts", 0),
                "model_calls": requests,
                "repaired": getattr(exc, "repaired", False),
                "rejection_fields": dict(getattr(exc, "fields", {})),
            }
            usage = _observed_usage(captured)
            if usage is not None:
                observed["usage"] = usage
            error = "IntakeRejection"
        except Exception as exc:  # noqa: BLE001 - plumbing, not a verdict
            satisfied, detail, observed = False, "runner/provider plumbing error", {}
            usage = None
            error = f"{type(exc).__name__}: {_clip(str(exc), 300)}"
        wall_minutes = (time.monotonic() - t0) / 60.0
        used["requests"] += requests
        used["minutes"] += wall_minutes
        # Conservative reservation from the OBSERVED usage at the higher
        # published rates (upper bound, never a price); legs that made no
        # request reserve nothing.
        reserved = 0.0
        if requests > 0 and usage:
            reserved = (
                usage.get("input_tokens", 0) * INPUT_RATE
                + usage.get("output_tokens", 0) * OUTPUT_RATE
            )
        used["usd"] += reserved
        legs.append(
            {
                "case": entry["case"],
                "leg": entry["leg"],
                "requests": requests,
                "wall_clock_minutes": round(wall_minutes, 4),
                "reserved_spend_usd": round(reserved, 6),
                "reserved_formula": RATES_NOTE,
                "model": MODEL_ID,
                "endpoint_base_url": BASE_URL,
                "measured_failure": measured_failure,
                "satisfied": satisfied,
                "detail": detail,
                "observed": observed,
                "error": error,
            }
        )
    return legs


def main():
    model = _build_model()
    used = {"requests": 0, "minutes": 0.0, "usd": 0.0, "stopped": None}
    legs = asyncio.run(_run_all(model, LEGS, used))
    document = {
        "schema": "act-live-legs/1",
        "model": MODEL_ID,
        "endpoint_base_url": BASE_URL,
        "caps": CAPS,
        "totals": {
            "requests": used["requests"],
            "wall_clock_minutes": round(used["minutes"], 4),
            "reserved_spend_usd": round(used["usd"], 6),
        },
        "stopped_at_bound": used["stopped"],
        "rates_note": RATES_NOTE,
        "legs": legs,
    }
    out = Path(os.environ.get("LIVE_OUTPUT", "/work/legs.json"))
    out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\\n")
    os.chmod(out, 0o644)
    print("LIVE_LEGS " + json.dumps(document["totals"], sort_keys=True))


main()
'''

_ast.parse(LIVE_RUNNER_SOURCE, mode="exec")  # the runner must stay parseable


def _live_network() -> str | None:
    """The compose network that hosts BOTH the stack and the litellm proxy."""
    proc = _docker(
        [
            "inspect",
            "machina-mai-api-1",
            "--format",
            "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}\n{{end}}",
        ]
    )
    if proc.returncode != 0:
        return None
    for net in (line.strip() for line in proc.stdout.splitlines()):
        if not net:
            continue
        attached = _docker(
            [
                "network",
                "inspect",
                net,
                "--format",
                "{{range .Containers}}{{.Name}} {{end}}",
            ]
        )
        if attached.returncode == 0 and "litellm" in attached.stdout.split():
            return net
    return None


def test_live_provider_legs_budgeted() -> None:
    """Budgeted live-provider legs (P1/P2) through the REAL intake seam.

    The leg runner executes inside a disposable marked container on the
    compose infra network; the credential crosses only as the read-only
    env-file mount into the production EnvResolver. Every request, minute
    and reserved dollar is counted; the first bound stops the legs. If any
    piece of the plumbing is unavailable, the legs are recorded
    not_measured with that exact reason — never fabricated, never faked.
    """
    budget_doc = json.loads(CASES_PATH.read_text(encoding="utf-8")).get(
        "budget", {}
    )
    caps = {
        "requests": int(budget_doc.get("requests_cap", 60)),
        "minutes": float(budget_doc.get("wall_clock_minutes_cap", 30)),
        "usd": float(budget_doc.get("reserved_spend_usd_cap", 5.0)),
    }

    def _skip_all(reason: str) -> None:
        for case_id in LIVE_LEG_CASES:
            LIVE_FLAGS[case_id] = (False, reason)
        print(f"[live-legs] not measured: {reason}")

    net = _live_network()
    if net is None:
        _skip_all(
            "the compose infra network hosting the litellm proxy is not "
            "discoverable from this host (docker inspect found no stack "
            "network with a litellm container attached)"
        )
        return
    if not Path(LIVE_ENV_FILE).exists():
        _skip_all(
            f"the credential env-file bind source does not exist on this "
            f"host: {LIVE_ENV_FILE} (the harness never reads the file; "
            "docker would fail the mount)"
        )
        return
    src_root = BACKEND_ROOT / "src"
    if not src_root.is_dir():
        _skip_all("backend/src is not present next to the harness")
        return

    work = LIVE_WORK_ROOT / uuid.uuid4().hex[:12]
    work.mkdir(parents=True, exist_ok=True)
    container = f"{CONTAINER_MARK}live-{uuid.uuid4().hex[:12]}"
    (work / "live_runner.py").write_text(LIVE_RUNNER_SOURCE, encoding="utf-8")
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--name",
            container,
            "--user",
            "0:0",
            "--network",
            net,
            "-e",
            "PYTHONPATH=/work/src",
            "-e",
            f"LIVE_CAP_REQUESTS={caps['requests']}",
            "-e",
            f"LIVE_CAP_MINUTES={caps['minutes']}",
            "-e",
            f"LIVE_CAP_USD={caps['usd']}",
            "-v",
            f"{LIVE_ENV_FILE}:/run/secrets/mai-credentials.env:ro",
            "-v",
            f"{work}:/work:rw",
            "-v",
            f"{src_root}:/work/src:ro",
            LIVE_IMAGE,
            "python",
            "/work/live_runner.py",
        ],
        capture_output=True,
        text=True,
        timeout=LIVE_TIMEOUT_SECONDS,
        check=False,
    )
    try:
        if proc.returncode != 0:
            _skip_all(
                "the in-container leg runner failed: rc="
                f"{proc.returncode} stderr_tail="
                f"{proc.stderr.strip()[-400:]!r}"
            )
            return
        legs_path = work / "legs.json"
        try:
            document = json.loads(legs_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError) as exc:
            _skip_all(f"the leg runner produced no parsable legs.json: {exc}")
            return
        by_case = {
            leg.get("case"): leg for leg in document.get("legs", []) if leg.get("case")
        }
        ran_cases = set(by_case)
        for case_id in LIVE_LEG_CASES:
            leg = by_case.get(case_id)
            if leg is None:
                LIVE_FLAGS[case_id] = (
                    False,
                    document.get("stopped_at_bound")
                    or "the leg never started inside the bounded runner",
                )
                continue
            if leg.get("requests", 0) <= 0 and leg.get("error"):
                # A plumbing error made NO provider request: nothing was
                # measured — an honest not_measured, never a bucket-2 leg.
                LIVE_FLAGS[case_id] = (False, f"runner plumbing error: {leg['error']}")
                continue
            PROVIDER_LEGS.append(
                {
                    "case": case_id,
                    "leg": leg["leg"],
                    "requests": leg["requests"],
                    "wall_clock_minutes": leg["wall_clock_minutes"],
                    "reserved_spend_usd": leg["reserved_spend_usd"],
                    "model": leg["model"],
                    "satisfied": leg["satisfied"],
                    "measured_failure": leg.get("measured_failure", False),
                    "detail": leg["detail"],
                    "observed": leg["observed"],
                    "reserved_formula": leg.get("reserved_formula"),
                    "error": leg.get("error"),
                }
            )
            if leg.get("requests", 0) > 0 and leg.get("error") is None:
                LIVE_FLAGS[case_id] = (True, None)
            else:
                # A leg with provider requests but a non-None error stays
                # in bucket 2 (it really ran and its spend counts) while
                # the ROW does not claim a measured live outcome.
                LIVE_FLAGS[case_id] = (
                    False,
                    f"the leg ran ({leg.get('requests', 0)} requests) but "
                    f"errored: {leg.get('error')}",
                )
        # Preserve the runner's own output (value-free) for forensics.
        legs_path = work / "legs.json"
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        (EVIDENCE_DIR / "provider_legs_raw.json").write_text(
            legs_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        for case_id, leg in sorted(by_case.items()):
            print(
                f"[live-legs] {case_id}: satisfied={leg['satisfied']} "
                f"requests={leg['requests']} "
                f"minutes={leg['wall_clock_minutes']} "
                f"reserved_usd={leg['reserved_spend_usd']} :: {leg['detail']}"
            )
        print(
            f"[live-legs] totals: {document.get('totals')} "
            f"stopped_at_bound={document.get('stopped_at_bound')}"
        )
        for case_id in LIVE_LEG_CASES:
            if case_id not in ran_cases:
                print(
                    f"[live-legs] {case_id}: NOT MEASURED "
                    f"({LIVE_FLAGS[case_id][1]})"
                )
    finally:
        # Teardown hygiene: --rm removes the container on exit; if the run
        # failed in a way that left it, remove it explicitly and never
        # leak a marked container.
        _docker(["rm", "-f", container])
        import shutil

        shutil.rmtree(work, ignore_errors=True)
