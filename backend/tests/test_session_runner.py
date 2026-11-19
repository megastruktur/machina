"""D04 tests: the durable session runner on a real DBOS worker subprocess.

Reuses the PROVEN harness pattern from ``tests/test_agent_run.py`` (the
brief's mandate): the same worker subprocess spawn/kill -9/stop protocol,
the same journaled ``FixtureExecutor`` (begin/effect markers + gates), the
same scripted ``FunctionModel`` (gate + journal markers), the same
disposable pgvector Postgres + real Alembic chain + real engine bootstrap —
adapted to compose the D04 session runner instead of the legacy executor.

Required proof (BRIEF_D04 §Required Proof):

1. Two consecutive tool-using runs in the SAME ``session_id``: Run 2's
   model receives Run 1's complete native history (prompt, tool call,
   tool return, text) plus the new prompt; two distinct child runs.
2. ``kill -9`` mid-tool (crash window): the fence translates to
   ``outcome_unknown`` WITHOUT re-execution, reconciles, and completes
   with zero duplicate session entries; ``kill -9`` mid-model: the
   uncheckpointed round safely re-runs with zero duplicate entries.
3. Approval park + worker restart + payload-bound resume; altered-payload
   approval refused with zero effect.
4. Steering injects at the next boundary; cancel settles ``cancelled`` in
   both authorities with no effect.
5. An unrelated generic job executes on the same worker/engine.
6. Zero old-contract/harness imports in ``session_runner.py`` (AST).

Every claim runs against the real disposable Postgres fixture and a REAL
worker process (``python -m mai.jobs.worker``) driven by the real engine.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from psycopg.rows import dict_row
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents.model_store import ModelConfigStore
from mai.agents import session_runner as sr
from mai.agents.session_runner import (
    HANDLER_NAME as SESSION_HANDLER_NAME,
    SECRET_ROUTE_LATCHED_EVENT_TYPE,
    SessionRunExecutor,
    SessionRunIntent,
    SessionStageError,
    asubmit_session_control,
    parse_session_run_intent,
)
from mai.agents.session_runner import SessionSourceKind
from mai.contracts.jobs import parse_job_event
from mai.contracts.server import SCHEMA_VERSION as SERVER_SCHEMA_VERSION
from mai.agents.models import SCHEMA_VERSION as MODEL_SCHEMA_VERSION
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.jobs import engine, steps
from mai.jobs.engine import EngineClient
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import ExecutionContext, Policy, PolicyMode
from mai.contracts.sessions import SERVICE_MAP_SECTION_HEADER
from mai.session_store import acreate_session
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

# Reuse the PROVEN harness pieces (BRIEF_D04 mandate): the persistent test
# loop, the kill -9 / stop protocol, the wait helper and the journal
# readers come from the shared runtime-scenario harness (the legacy
# agent-run suite was deleted at D09; the helpers were extracted verbatim).
from agent_runtime_harness import (  # noqa: F401 - harness reuse
    HOST_KEY,
    PRIV_CAP,
    READY_TIMEOUT,
    READ_CAP,
    RUN_TIMEOUT,
    SECRET_NAME,
    SECRET_VALUE,
    SERVER_ID,
    _journal_count,
    _journal_lines,
    _kill9,
    _run,
    _stop,
    _wait_until,
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"
FIXTURE_ROOT = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "mai-fxt-d04-session"

SESSION_HANDLER_MODULE_NAME = "session_runner_scenario_handlers"


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state (D02-aware).

    The session-first tables (0014) reference the generic ``tags`` catalog,
    so they are dropped FIRST (child-first); the rest is the reviewed
    agent-run suite's drop list, unchanged.
    """
    with psycopg.connect(url) as conn:
        # Whole-schema reset (D09): FK-safe and immune to future revisions
        # (the enumerated drop list went stale across migrations).
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def agent_url() -> Iterator[str]:
    """Migrate the disposable DB, then restore pre-bootstrap state."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the durable session-runner claims "
            "need a real disposable Postgres fixture (pgvector/pgvector:pg16)"
        )
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        try:
            _drop_app_tables(url)
            command.upgrade(_alembic_config(), "head")
        finally:
            if previous is None:
                os.environ.pop("MAI_DATABASE_URL", None)
            else:
                os.environ["MAI_DATABASE_URL"] = previous
        engine.launch_worker(url)  # engine-owned dbos schema
        engine.shutdown()
        yield url
    finally:
        engine.shutdown()
        _drop_app_tables(url)


@pytest.fixture
def factory(agent_url: str) -> async_sessionmaker[AsyncSession]:
    """NullPool session factory for in-process facade assertions."""
    eng, sessions = create_app_engine(agent_url)

    async def _clean() -> None:
        async with sessions() as session:
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
                present = await session.execute(
                    __import__("sqlalchemy").text(
                        "SELECT to_regclass(:t) IS NOT NULL"
                    ),
                    {"t": f"public.{table}"},
                )
                if not present.scalar():
                    continue
                await session.execute(
                    __import__("sqlalchemy").text(f"DELETE FROM {table}")
                )
            await session.commit()
        await eng.dispose()

    yield sessions
    _run(_clean())


@pytest.fixture
def insert_run(factory: async_sessionmaker[AsyncSession]):
    """Insert the facade ``job_runs`` row (the submit owner's row)."""

    async def _insert(
        run_id: str,
        *,
        requested_grants: tuple[str, ...] = (READ_CAP,),
    ) -> None:
        from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID
        from sqlalchemy import text

        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO job_runs
                        (run_id, server_id, depth, profile_revision,
                         prompt_revision, model_profile, requested_grants,
                         submitted_at, updated_at)
                    VALUES
                        (:run_id, :server_id, 0, 3, 7, 'fixture-fast',
                         :requested_grants, now(), now())
                    """
                ),
                {
                    "run_id": uuid.UUID(run_id),
                    "server_id": uuid.UUID(SERVER_ID),
                    "requested_grants": json.dumps(requested_grants),
                },
            )
            # The 0011 mandatory-tag invariant: the run and its
            # classification commit in the SAME transaction (the real
            # writer's discipline).
            await session.execute(
                text(
                    """
                    INSERT INTO job_tags (job_id, tag_id, assignment_source)
                    VALUES (:run_id, :tag, 'system')
                    """
                ),
                {
                    "run_id": uuid.UUID(run_id),
                    "tag": uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
                },
            )
            await session.commit()

    return _insert


@pytest.fixture
def scenario(tmp_path: pathlib.Path) -> pathlib.Path:
    """One fresh scratch scenario directory (journal, gates, handlers)."""
    directory = tmp_path / "scenario"
    (directory / "gates").mkdir(parents=True)
    handlers = directory / "handlers"
    handlers.mkdir()
    (handlers / f"{SESSION_HANDLER_MODULE_NAME}.py").write_text(
        SESSION_HANDLER_MODULE_SOURCE, encoding="utf-8"
    )
    return directory


def _write_model_root(root: pathlib.Path, *, base_url: str) -> None:
    store = ModelConfigStore(root)
    connection = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "name": "gateway",
        "base_url": base_url,
        "timeout_seconds": 5,
        "capabilities": {"tool_calls": True},
    }
    store.write_connection(
        "gateway", json.dumps(connection) + "\n", expected_revision=0
    )
    profile = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "name": "fast",
        "connection": "gateway",
        "model": "fixture-model",
        "max_tokens": 256,
    }
    store.write_profile("fast", json.dumps(profile) + "\n", expected_revision=0)
    store.set_selection("gateway", "fast", expected_revision=0)


def _write_data_root(root: pathlib.Path, *, capability: str = READ_CAP) -> None:
    from mai.profiles.files import ProfileFileStore

    store = ProfileFileStore(root)
    profile_text = (
        f"schema_version: {SERVER_SCHEMA_VERSION}\n"
        f'server_id: "{SERVER_ID}"\n'
        'name: "session-runner-fixture"\n'
        "prompt_revision: 3\n"
        "environment: linux_ssh\n"
        "ssh:\n"
        '  host: "198.51.100.10"\n'
        "  port: 22\n"
        '  user: "deploy"\n'
        f'  host_key: "{HOST_KEY}"\n'
        '  credential_ref: "MAI_SSH_KEY_FIXTURE"\n'
        "privilege: none\n"
        "capabilities:\n"
        f"  - {capability}\n"
    )
    store.write(uuid.UUID(SERVER_ID), profile_text, expected_revision=0)
    server_dir = root / "servers" / SERVER_ID
    server_dir.chmod(0o700)
    prompt_path = server_dir / "prompt.md"
    prompt_path.write_text("fixture prompt\n", encoding="utf-8")
    os.chmod(prompt_path, 0o600)


@pytest.fixture
def composition(scenario: pathlib.Path) -> Callable[..., dict[str, pathlib.Path]]:
    """Write the fixture model/data roots once per scenario."""

    def _compose(
        *, base_url: str = "http://127.0.0.1:9/v1", capability: str = READ_CAP
    ) -> dict[str, pathlib.Path]:
        model_root = scenario / "model-root"
        data_root = scenario / "data-root"
        _write_model_root(model_root, base_url=base_url)
        _write_data_root(data_root, capability=capability)
        (scenario / "fixture.env").write_text(
            f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8"
        )
        os.chmod(scenario / "fixture.env", 0o600)
        return {"model_root": model_root, "data_root": data_root}

    return _compose


def _write_policy_files(
    scenario: pathlib.Path,
    *,
    grants: list[dict[str, bool]],
    denied: list[str] | None = None,
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


def _script(
    scenario: pathlib.Path,
    entries: list[dict],
    *,
    title: dict | None = None,
) -> None:
    """Write the scripted FunctionModel document.

    ST: an optional dedicated ``title`` entry serves the auto-title
    agent's bare requests (the title agent would otherwise re-read the
    FIRST round script entry — both requests carry a single message).
    A plain list keeps the legacy shape every existing scenario uses.
    """
    document: object = entries if title is None else {"rounds": entries, "title": title}
    (scenario / "model-script.json").write_text(
        json.dumps(document), encoding="utf-8"
    )


def _tool_call_entry(
    name: str = "fixture_read",
    marker: str = "m1",
    call_id: str = "c1",
) -> dict:
    return {"tool_call": {"name": name, "args": {"marker": marker}, "id": call_id}}


# ---------------------------------------------------------------------------
# the worker-side composition (adapted from the reviewed agent-run harness:
# same journaled FixtureExecutor, same scripted model gates/markers, but it
# composes the D04 SESSION runner over the same dispatcher)
# ---------------------------------------------------------------------------

SESSION_HANDLER_MODULE_SOURCE = '''
"""Worker-side composition for the D04 session runner: the journaled
FixtureExecutor, the deterministic scripted FunctionModel, and the D04
SessionRunExecutor composed over the SAME dispatcher (plus one trivial
generic handler proving unrelated jobs still execute on this worker)."""

import asyncio
import json
import os
import time
import uuid

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import mai.agents.session_runner as session_runner
from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
from mai.capabilities.host_file_read import FileReadError, FileReadErrorCode
from mai.agents.model_store import ModelConfigStore
from mai.credentials.env import EnvResolver
from mai.jobs import engine as jobs_engine
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
    """The reviewed journaled executor: begin/effect markers make
    exactly-once observable; the gate behavior is the kill -9 window."""

    def __init__(self):
        self.journal = os.environ["MAI_FX_JOURNAL"]
        self.gate_dir = os.environ["MAI_FX_GATE_DIR"]
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
        if behavior == "path_denied":
            # FRR T2 regression seam: the T1 pre-effect refusal shape —
            # the executor raises BEFORE the effect marker (no transport,
            # no effect), exactly like host.file_read outside its roots.
            raise FileReadError(
                FileReadErrorCode.PATH_DENIED,
                "outside every allowed file_read_roots prefix",
            )
        if behavior == "gate":
            gate = os.path.join(
                self.gate_dir, f"gate-{capability_id}-{marker}"
            )
            deadline = time.time() + 300
            while not os.path.exists(gate):
                if time.time() > deadline:
                    raise RuntimeError("gate timeout")
                time.sleep(0.1)
        self._mark(f"effect:{capability_id}:{marker}")
        status = (
            "outcome_unknown" if behavior == "unknown_status" else "succeeded"
        )
        document = {
            "status": status,
            "exit_code": 0 if status == "succeeded" else None,
            "stdout": "",
            "stderr": "",
        }
        return ExecutorResult(
            ok=status == "succeeded", output=json.dumps(document, sort_keys=True)
        )


def _script_model():
    script = json.loads(os.environ["MAI_FX_MODEL_SCRIPT"])
    journal = os.environ["MAI_FX_JOURNAL"]
    # ST: {"rounds": [...], "title": {...}} — the dedicated title entry
    # is served to the auto-title agent's requests; a plain list keeps
    # the legacy round-only shape.
    rounds_script: list = script
    title_entry: dict | None = None
    if isinstance(script, dict):
        rounds_script = script["rounds"]
        title_entry = script.get("title")

    def _mark(line):
        with open(journal, "a", encoding="utf-8") as handle:
            handle.write(line + "\\n")

    def _entry_payload(entry):
        if entry.get("gate"):
            gate = os.path.join(os.environ["MAI_FX_GATE_DIR"], entry["gate"])
            deadline = time.time() + 300
            while not os.path.exists(gate):
                if time.time() > deadline:
                    raise RuntimeError("model gate timeout")
                time.sleep(0.1)
        inner = entry.get("then", entry)
        if "error" in inner:
            message = str(inner["error"])
            exc_name = str(inner.get("error_type") or "RuntimeError")
            if exc_name == "RuntimeError":
                raise RuntimeError(message)
            # E2-fix1 probe seam: a sentinel-NAMED dynamic subclass (the
            # class NAME is the leak vector under audit F-A).
            raise type(exc_name, (RuntimeError,), {})(message)
        if "tool_call" in inner or "tool_calls" in inner:
            parts = []
            if inner.get("text"):
                # Optional leaked/side text beside the call(s).
                parts.append(TextPart(content=str(inner["text"])))
            calls = (
                [inner["tool_call"]]
                if "tool_call" in inner
                else list(inner.get("tool_calls", []))
            )
            for call in calls:
                parts.append(
                    ToolCallPart(
                        tool_name=call["name"],
                        args=call.get("args", {"marker": "m1"}),
                        tool_call_id=call.get("id", "c1"),
                    )
                )
            return ModelResponse(parts=parts)
        return ModelResponse(parts=[TextPart(content=inner.get("text", "done"))])

    def _model_fn(messages, info):
        req_instructions = (
            str(getattr(messages[-1], "instructions", "") or "")
            if messages
            else ""
        )
        if req_instructions.startswith("You generate chat session titles"):
            # ST: the auto-title agent's BARE request (single message, no
            # tools, title instructions) — journaled as its OWN marker
            # ONLY. The round-level model_call/policy markers belong to
            # the chat rounds; existing journal counts must not move, and
            # a title request would otherwise re-read round index 0.
            _mark("title_model_call")
            if title_entry is None:
                raise AssertionError("title script entry missing")
            return _entry_payload(title_entry)
        _mark("model_call")
        _mark(f"history_len:{len(messages)}")
        # ask_03-runtime-policy seam: capture the CURRENT request's
        # instructions (the composed operating policy) and prove the
        # history carries exactly ONE instructions-bearing request and
        # zero system-prompt parts (per-request injection, no duplicates).
        _mark(f"has_instructions:{bool(req_instructions)}")
        _mark(f"policy_latched:{'Secret routing status: ACTIVE' in req_instructions}")
        _mark(f"policy_role_root:{'Run role: ROOT run' in req_instructions}")
        _mark(f"policy_role_child:{'DELEGATED CHILD run' in req_instructions}")
        _mark(f"policy_rules_global:{'Binding rules:' in req_instructions}")
        _mark(
            f"policy_service_knowledge:{'app.resolve_service' in req_instructions}"
        )
        _mark(
            f"policy_secret_routing:{'secret-only credential routing' in req_instructions}"
        )
        _mark(
            "instructions_messages:"
            f"{sum(1 for m in messages if str(getattr(m, 'instructions', '') or ''))}"
        )
        _mark(
            "system_parts:"
            f"{sum(1 for m in messages for p in getattr(m, 'parts', []) if getattr(p, 'part_kind', '') == 'system-prompt')}"
        )
        kinds = []
        for message in messages:
            for part in getattr(message, "parts", []):
                kind = getattr(part, "part_kind", "")
                kinds.append(kind)
                if kind == "tool-call":
                    _mark(f"saw_tool_call:{part.tool_call_id}")
                elif kind == "tool-return":
                    _mark("saw_tool_return")
                elif kind == "text":
                    _mark(f"saw_text:{part.content[:40]}")
                elif kind == "user-prompt":
                    _mark(f"saw_user:{str(part.content)[:40]}")
        steering = [
            str(getattr(part, "content", ""))
            for message in messages
            for part in getattr(message, "parts", [])
            if "[operator steering]" in str(getattr(part, "content", ""))
        ]
        if steering:
            _mark("saw_steering")
        index = (len(messages) - 1) // 2
        if index >= len(rounds_script):
            raise AssertionError("model script exhausted")
        return _entry_payload(rounds_script[index])

    return FunctionModel(_model_fn)


async def _generic_session_job(payload_json: str) -> dict:
    """One trivial NON-agent handler on the same worker/engine."""
    with open(os.environ["MAI_FX_JOURNAL"], "a", encoding="utf-8") as handle:
        handle.write("generic:done\\n")
    return {"state": "succeeded"}


jobs_engine.register_job_handler("session_fixture.generic", _generic_session_job)


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
    # ask_07-runner-safety: the REAL built-in secret descriptors, so a
    # scenario granting secret.* exposes the true agent-projected surface
    # (generation/reference-only secret.store) and valid generation calls
    # dispatch through the actual schema gate; exposure follows
    # context ∩ registry, so scenarios that never allow them are unaffected.
    from mai.plugins.builtin_onepassword import (
        CAPABILITIES as ONEPASSWORD_CAPABILITIES,
    )

    for document in ONEPASSWORD_CAPABILITIES:
        registry.register(parse_descriptor(document))
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
    # MAI_FX_MODEL_SCRIPT carries the JSON CONTENT of the script (not a
    # path) — the reviewed harness contract. The scripted fake exists only
    # when the scenario provides it.
    scripted = os.environ.get("MAI_FX_MODEL_SCRIPT")
    fake_model = _script_model() if scripted else None
    session_runner.configure_session_run(
        session_runner.SessionRunExecutor(
            sessions=sessions,
            dispatcher=dispatcher,
            registry=registry,
            model_store=model_store,
            data_root=os.environ["MAI_FX_DATA_ROOT"],
            locks=ResourceLockService(url),
            resolver=EnvResolver(os.environ["MAI_FX_ENV"]),
            model_builder=(lambda **kwargs: fake_model) if fake_model else None,
        )
    )


_build()
'''


def _spawn_worker(
    url: str, handler_dir: pathlib.Path, scenario: pathlib.Path, tag: str
) -> subprocess.Popen:
    """Launch a REAL worker process against the fixture database."""
    env = dict(os.environ)
    env.pop(LIVE_URL_VAR, None)  # workers speak the production env contract
    env["MAI_DATABASE_URL"] = url
    env["MAI_WORKER_HANDLERS"] = SESSION_HANDLER_MODULE_NAME
    env["MAI_FX_JOURNAL"] = str(scenario / "journal.log")
    env["MAI_FX_GATE_DIR"] = str(scenario / "gates")
    env["MAI_FX_GRANTS"] = str(scenario / "grants.json")
    env["MAI_FX_POLICY"] = str(scenario / "policy.json")
    env["MAI_FX_ENV"] = str(scenario / "fixture.env")
    env["MAI_FX_MODEL_ROOT"] = str(scenario / "model-root")
    env["MAI_FX_DATA_ROOT"] = str(scenario / "data-root")
    behavior_path = scenario / "behavior.json"
    env.pop("MAI_FX_BEHAVIOR", None)
    if behavior_path.exists():
        env["MAI_FX_BEHAVIOR"] = behavior_path.read_text(encoding="utf-8")
    script_path = scenario / "model-script.json"
    if script_path.exists():
        env["MAI_FX_MODEL_SCRIPT"] = script_path.read_text(encoding="utf-8")
    else:
        env.pop("MAI_FX_MODEL_SCRIPT", None)
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


# ---------------------------------------------------------------------------
# intent document + session readers
# ---------------------------------------------------------------------------


def _context_doc(
    run_id: str,
    session_id: str,
    *,
    mode: str = "delegated",
    allowed: tuple[str, ...] = (READ_CAP,),
) -> dict:
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


def _intent_document(
    run_id: str,
    session_id: str,
    *,
    allowed: tuple[str, ...] | None = None,
    mode: str = "delegated",
    client_request_id: str | None = None,
    **overrides: object,
) -> dict:
    document: dict[str, object] = {
        "payload_version": 1,
        "session_id": session_id,
        "run_id": run_id,
        "server_id": SERVER_ID,
        "client_request_id": client_request_id or str(uuid.uuid4()),
        "source": "interactive",
        "prompt": "Run the fixture task and report the marker outcome.",
        "context": (
            _context_doc(run_id, session_id, mode=mode)
            if allowed is None
            else _context_doc(run_id, session_id, mode=mode, allowed=allowed)
        ),
        "max_rounds": 4,
        "wait_poll_seconds": 0.5,
    }
    document.update(overrides)
    return document


def _canonical(document: object) -> str:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


async def _make_session(factory: async_sessionmaker[AsyncSession]) -> str:
    session_id = str(uuid.UUID(int=int(uuid.uuid4().int) % (2**63) | (1 << 62)))
    await acreate_session(
        factory,
        server_id=uuid.UUID(SERVER_ID),
        source=__import__("mai.contracts.sessions", fromlist=["SessionSourceKind"]).SessionSourceKind.INTERACTIVE,
        session_id=uuid.UUID(session_id),
    )
    return session_id


def _read_session_entries(url: str, session_id: str) -> list[dict]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT entry_id, seq, run_id, kind, payload, durable_source_id "
            "FROM session_entries WHERE session_id = %s ORDER BY seq",
            (uuid.UUID(session_id),),
        ).fetchall()


def _read_session_runs(url: str, session_id: str) -> list[dict]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT run_id, state, model_profile FROM session_runs "
            "WHERE session_id = %s ORDER BY created_at",
            (uuid.UUID(session_id),),
        ).fetchall()


def _read_sessions_row(url: str, session_id: str) -> dict | None:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT session_id, server_id, status, source FROM sessions "
            "WHERE session_id = %s",
            (uuid.UUID(session_id),),
        ).fetchone()


def _read_step_rows(url: str, run_id: str) -> list[dict]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT step_id, seq, capability_id, state, receipt FROM job_steps "
            "WHERE run_id = %s ORDER BY seq",
            (uuid.UUID(run_id),),
        ).fetchall()


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
    from sqlalchemy import text

    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT data FROM job_events "
                        "WHERE run_id = :rid AND type = 'run.transition' "
                        "ORDER BY seq"
                    ),
                    {"rid": uuid.UUID(run_id)},
                )
            )
            .scalars()
            .all()
        )
    return tuple(row["to"] for row in rows)


async def _current_state(factory: async_sessionmaker, run_id: str) -> str | None:
    states = await _run_states(factory, run_id)
    return states[-1] if states else None


def _terminal_engine_state(client: EngineClient, run_id: str) -> str | None:
    try:
        state = client.run_state(run_id)
    except Exception:
        return None
    if state is None:
        return None
    value = getattr(state, "value", state)
    if value in ("succeeded", "failed", "cancelled"):
        return str(value)
    return None


def _await_terminal(
    client: EngineClient, run_id: str, expected: str, timeout: float, what: str
) -> str:
    """Wait for ANY terminal engine state, then assert the expected one.

    A failing workflow settles ``failed``/``cancelled`` quickly — waiting
    only for ``succeeded`` would burn the whole 120 s budget before the
    assertion instead of failing in one poll.
    """
    _wait_until(
        lambda: _terminal_engine_state(client, run_id) is not None,
        timeout,
        what,
    )
    actual = _terminal_engine_state(client, run_id)
    assert actual == expected, (
        f"{what}: engine state {actual!r} (expected {expected!r})"
    )
    return actual


def _send_wake(run_id: str, url: str, message: dict, *, topic: str | None = None) -> None:
    """Control-plane park wake: a durable message on the resume topic."""
    from dbos import DBOSClient

    client = DBOSClient(
        system_database_url=url,
        application_name="mai",
        lazy=True,
    )
    try:
        client.send(run_id, message, topic=topic or steps.RESUME_TOPIC)
    finally:
        client.destroy()


# ---------------------------------------------------------------------------
# 1. two consecutive tool-using runs in the SAME session
# ---------------------------------------------------------------------------


def test_two_consecutive_runs_share_one_session(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(
        scenario,
        [
            _tool_call_entry(name="fixture_read", marker="m1", call_id="c1"),
            {"text": "run one finished"},
            _tool_call_entry(name="fixture_read", marker="m2", call_id="c2"),
            {"text": "run two finished"},
        ],
    )
    session_id = _run(_make_session(factory))
    run_id_1 = str(uuid.uuid4())
    run_id_2 = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(run_id_1))
        client.enqueue_run(
            run_id_1,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id_1, session_id, client_request_id=str(uuid.uuid4())
                )
            ),
        )
        _await_terminal(client, run_id_1, "succeeded", RUN_TIMEOUT, "run 1")
        _run(insert_run(run_id_2))
        client.enqueue_run(
            run_id_2,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id_2, session_id, client_request_id=str(uuid.uuid4())
                )
            ),
        )
        _await_terminal(client, run_id_2, "succeeded", RUN_TIMEOUT, "run 2")
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    # Run 2's model received Run 1's complete native exchange.
    lines = _journal_lines(scenario)
    assert "saw_user:Run the fixture task" in lines or any(
        line.startswith("saw_user:") for line in lines
    )
    assert "saw_tool_call:c1" in lines, lines
    assert "saw_tool_return" in lines, lines
    assert any(
        line.startswith("saw_text:run one finished") for line in lines
    ), lines
    # The session identity stayed stable; two distinct child runs exist.
    runs = _read_session_runs(agent_url, session_id)
    assert {row["run_id"] for row in runs} == {
        uuid.UUID(run_id_1),
        uuid.UUID(run_id_2),
    }
    entries = _read_session_entries(agent_url, session_id)
    seqs = [entry["seq"] for entry in entries]
    assert seqs == list(range(1, len(seqs) + 1))  # contiguous, no gaps
    kinds = [entry["kind"] for entry in entries]
    # user, assistant(tc), tool_result, assistant(final) x 2 — and the run
    # state entries ride along (one per recorded transition).
    assert kinds.count("user_message") == 2
    assert kinds.count("tool_result") == 2
    assert kinds.count("assistant_message") >= 2
    assert kinds.count("run_state") >= 2
    assert [entry["payload"]["call_id"] for entry in entries if entry["kind"] == "tool_result"] == [
        "c1",
        "c2",
    ]
    # Both assistant tool-call groups are marked complete in the durable log.
    tool_calls_payloads = [
        entry["payload"]
        for entry in entries
        if entry["kind"] == "assistant_message" and entry["payload"].get("tool_calls")
    ]
    assert tool_calls_payloads, "assistant tool-call entries exist"
    assert all(
        payload.get("tool_group_state") == "complete"
        for payload in tool_calls_payloads
    )
    row = _read_sessions_row(agent_url, session_id)
    assert row is not None and row["status"] == "idle"
    assert _run(_run_states(factory, run_id_1))[-1] == "succeeded"
    assert _run(_run_states(factory, run_id_2))[-1] == "succeeded"


# ---------------------------------------------------------------------------
# 2a. kill -9 mid-tool: crash window, no re-execution, reconciliation
# ---------------------------------------------------------------------------


def test_kill9_mid_tool_reconciles_without_reexecution(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    (scenario / "behavior.json").write_text(
        json.dumps({READ_CAP: "gate"}), encoding="utf-8"
    )
    _script(
        scenario,
        [
            _tool_call_entry(name="fixture_read", marker="m1", call_id="c1"),
            {"text": "recovered and done"},
        ],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker1: subprocess.Popen | None = None
    worker2: subprocess.Popen | None = None
    try:
        worker1 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(run_id))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id,
                    session_id,
                    reconcile_wait_seconds=120.0,
                    client_request_id=str(uuid.uuid4()),
                )
            ),
        )
        # Parked inside the gated effect (the fence is held).
        _wait_until(
            lambda: _journal_count(scenario, "begin:fixture.read:m1") == 1,
            RUN_TIMEOUT,
            "gated begin marker",
        )
        _kill9(worker1)
        worker1 = None
        assert _journal_count(scenario, "effect:fixture.read:m1") == 0

        # Worker 2 recovers: the fence translates WITHOUT re-execution.
        worker2 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w2")
        _wait_until(
            lambda: any(
                entry["kind"] == "tool_result"
                and entry["payload"].get("status") == "unknown_effect"
                for entry in _read_session_entries(agent_url, session_id)
            ),
            RUN_TIMEOUT,
            "the honest unknown_effect session entry",
        )
        # The reconciler resolves the step row (the truth); the runner
        # updates the SAME keyed entry and continues.
        with psycopg.connect(agent_url, autocommit=True) as conn:
            conn.execute(
                "UPDATE job_steps SET state = 'succeeded' "
                "WHERE run_id = %s AND seq = 100",
                (uuid.UUID(run_id),),
            )
        _send_wake(run_id, agent_url, {"kind": "resolve"})
        _await_terminal(
            client, run_id, "succeeded", RUN_TIMEOUT, "reconciliation recovery"
        )
    finally:
        for worker in (worker1, worker2):
            if worker is not None:
                _stop(worker)
        client.close()

    # The effect NEVER ran; the begin happened EXACTLY once.
    assert _journal_count(scenario, "begin:fixture.read:m1") == 1
    assert _journal_count(scenario, "effect:fixture.read:m1") == 0
    entries = _read_session_entries(agent_url, session_id)
    tool_results = [e for e in entries if e["kind"] == "tool_result"]
    assert len(tool_results) == 1  # the SAME entry was updated, not duplicated
    resolved = tool_results[0]["payload"]
    assert resolved["call_id"] == "c1"
    # The reconciler resolved the row WITHOUT a remote execution receipt,
    # so the honest truth is: wrapper succeeded, effect evidence unproven.
    assert resolved["status"] == "unknown_effect"
    assert resolved["execution_status"] == "succeeded"
    assert resolved["effect_certainty"] == "uncertain"
    user_entries = [e for e in entries if e["kind"] == "user_message"]
    assert len(user_entries) == 1  # zero duplicate user entries
    assistant_entries = [e for e in entries if e["kind"] == "assistant_message"]
    assert len(assistant_entries) == 2  # tool-call round + final round only
    # The next model round saw the resolved result (honest continuation).
    assert "saw_tool_return" in _journal_lines(scenario)
    # The step row carries the reconciler resolution; the run succeeded.
    rows = _read_step_rows(agent_url, run_id)
    assert rows[0]["state"] == "succeeded"
    assert _run(_run_states(factory, run_id))[-1] == "succeeded"


# ---------------------------------------------------------------------------
# 2a-FRR. pre-effect denial: deterministic denial, NO reconciliation park
# ---------------------------------------------------------------------------


def test_pre_effect_denial_never_parks_reconciliation_frr_t2(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """FRR T2 regression (the 2026-11-14 incident): the executor raises a
    ``PreEffectError`` (the T1 ``path_denied`` refusal shape) for one tool
    call. The denial is deterministic — the tool result entry is ``denied``
    with the stable ``error_code`` token — and the run continues to the
    next model round WITHOUT ever entering ``reconciling``: no
    ``reconciling`` transition, no ``agent.awaiting_reconciliation`` event,
    no 300 s park. (The unknown-effect crash window beside it still parks —
    ``test_kill9_mid_tool_reconciles_without_reexecution``.)"""
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    (scenario / "behavior.json").write_text(
        json.dumps({f"{READ_CAP}:m1": "path_denied"}), encoding="utf-8"
    )
    _script(
        scenario,
        [
            _tool_call_entry(name="fixture_read", marker="m1", call_id="c1"),
            {"text": "denied and done"},
        ],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker: subprocess.Popen | None = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(run_id))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id, session_id, client_request_id=str(uuid.uuid4())
                )
            ),
        )
        # A reconciliation park would hold the run for the full
        # reconcile deadline and blow this budget before settling.
        _await_terminal(client, run_id, "succeeded", RUN_TIMEOUT, "denied run")
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    # The refusal was pre-effect (no effect marker) and the run CONTINUED:
    # the model got round 2 with the denied tool return and closed the run.
    lines = _journal_lines(scenario)
    assert _journal_count(scenario, "begin:fixture.read:m1") == 1
    assert _journal_count(scenario, "effect:fixture.read:m1") == 0
    assert lines.count("model_call") == 2, lines
    assert "saw_tool_return" in lines, lines

    # The durable log carries the round-2 assistant close-out.
    entries = _read_session_entries(agent_url, session_id)
    assistant_texts = [
        entry["payload"].get("text", "")
        for entry in entries
        if entry["kind"] == "assistant_message"
    ]
    assert "denied and done" in assistant_texts, assistant_texts

    # The model-facing tool result entry: denied + the stable code; the
    # exception detail text never reaches the durable entry.
    entries = _read_session_entries(agent_url, session_id)
    results = [e for e in entries if e["kind"] == "tool_result"]
    assert len(results) == 1
    payload = results[0]["payload"]
    assert payload["call_id"] == "c1"
    assert payload["status"] == "denied"
    assert payload["execution_status"] == "failed"
    assert payload["effect_certainty"] == "certain"
    assert payload["error_code"] == "path_denied"
    assert payload["error_detail"] is None
    assert "outside every allowed" not in json.dumps(payload)

    # NO reconciliation park for the denied step: no ``reconciling``
    # transition and no awaiting_reconciliation event was ever recorded.
    states = _run(_run_states(factory, run_id))
    assert states[-1] == "succeeded"
    assert "reconciling" not in states, states
    events = _read_events(agent_url, run_id)
    assert not [
        event for event in events if event.type == "agent.awaiting_reconciliation"
    ], [event.type for event in events]

    # The durable step receipt is the honest denial (the runner lane
    # stores its own value-free projection: reason + effect, no token —
    # the stable ``error_code`` rides the session tool_result entry
    # above; the jobs-facade lane's receipt carries it since T1).
    rows = _read_step_rows(agent_url, run_id)
    assert [row["state"] for row in rows] == ["failed"]
    receipt = rows[0]["receipt"]
    assert receipt["effect"] == "not_attempted"
    assert receipt["reason"] == "executor_failed"
    assert receipt["allowed"] is False


# ---------------------------------------------------------------------------
# 2b. kill -9 mid-model: the uncheckpointed round safely re-runs
# ---------------------------------------------------------------------------


def test_kill9_mid_model_round_reruns_without_duplicates(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(
        scenario,
        [
            {"gate": "model-gate", "then": _tool_call_entry()},
            {"text": "final after recovery"},
        ],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker1: subprocess.Popen | None = None
    worker2: subprocess.Popen | None = None
    try:
        worker1 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(run_id))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id, session_id, client_request_id=str(uuid.uuid4())
                )
            ),
        )
        # Parked INSIDE the model round (before its checkpoint completes).
        _wait_until(
            lambda: _journal_count(scenario, "model_call") == 1,
            RUN_TIMEOUT,
            "the gated model call",
        )
        _kill9(worker1)
        worker1 = None
        worker2 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w2")
        # Release the gate: the recovered round completes.
        (scenario / "gates" / "model-gate").write_text("go", encoding="utf-8")
        _await_terminal(
            client, run_id, "succeeded", RUN_TIMEOUT, "model-round recovery"
        )
    finally:
        for worker in (worker1, worker2):
            if worker is not None:
                _stop(worker)
        client.close()

    lines = _journal_lines(scenario)
    # The model was re-asked (the crashed round had no checkpoint)...
    assert lines.count("model_call") >= 2
    # ...the tool executed EXACTLY once...
    assert _journal_count(scenario, "begin:fixture.read:m1") == 1
    assert _journal_count(scenario, "effect:fixture.read:m1") == 1
    entries = _read_session_entries(agent_url, session_id)
    # ...and the durable log carries ZERO duplicate entries.
    tool_calls = [
        e for e in entries if e["kind"] == "assistant_message" and e["payload"].get("tool_calls")
    ]
    assert len(tool_calls) == 1
    assert len([e for e in entries if e["kind"] == "tool_result"]) == 1
    user_entries = [e for e in entries if e["kind"] == "user_message"]
    assert len(user_entries) == 1
    final_entries = [
        e
        for e in entries
        if e["kind"] == "assistant_message" and not e["payload"].get("tool_calls")
    ]
    assert len(final_entries) == 1
    assert _run(_run_states(factory, run_id))[-1] == "succeeded"


# ---------------------------------------------------------------------------
# 3. approval park + worker restart + payload-bound resume / altered refusal
# ---------------------------------------------------------------------------


def test_approval_park_survives_restart_and_resume_is_payload_bound(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    composition(capability=PRIV_CAP)
    _write_policy_files(scenario, grants=[{PRIV_CAP: True}])
    # The model retries the privileged call after the altered-approval
    # refusal, so the REAL payload-bound approval is presented afterwards
    # (one refused resume + one executing resume, exactly once each).
    _script(
        scenario,
        [
            _tool_call_entry(name="fixture_priv", marker="m1", call_id="c1"),
            _tool_call_entry(name="fixture_priv", marker="m1", call_id="c2"),
            {"text": "resumed and done"},
        ],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker1: subprocess.Popen | None = None
    worker2: subprocess.Popen | None = None
    try:
        worker1 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(run_id, requested_grants=(PRIV_CAP,)))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id,
                    session_id,
                    allowed=(PRIV_CAP,),
                    mode="ask",
                    approval_wait_seconds=120.0,
                    client_request_id=str(uuid.uuid4()),
                )
            ),
        )
        # The refusal parks the run durably on the approval wait (session
        # + facade + header all agree). Poll the HEADER: it commits after
        # the facade transition, so it implies both.
        _wait_until(
            lambda: _read_sessions_row(agent_url, session_id)["status"]
            == "waiting_approval",
            RUN_TIMEOUT,
            "the waiting_approval park",
        )
        assert _run(_current_state(factory, run_id)) == "waiting_approval"
        assert _read_session_runs(agent_url, session_id)[0]["state"] == "waiting_approval"
        row = _read_step_rows(agent_url, run_id)[0]
        assert row["state"] == "failed"
        receipt = row["receipt"]
        assert receipt["reason"] == "approval_required_but_absent"
        approval_id = receipt["approval_id"]
        assert approval_id and receipt["payload_hash"]
        assert receipt["policy_revision"] == 1
        assert _journal_count(scenario, "effect:fixture.priv:m1") == 0

        # Kill the parked worker; a fresh worker recovers the workflow and
        # the park survives (durable recv state).
        _kill9(worker1)
        worker1 = None
        worker2 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w2")

        # Negative first: an approval bound to an ALTERED payload is
        # presented — the dispatcher refuses it fail-closed (zero effect),
        # the honest denial lands, and the run continues.
        altered = _run(await_approval_for(
            factory, run_id, uuid.UUID(SERVER_ID), PRIV_CAP, {"marker": "altered"}
        ))
        _send_wake(
            run_id,
            agent_url,
            {"kind": "approval", "approval_id": str(altered), "decision": "approve"},
        )
        _wait_until(
            lambda: _current_tool_result_status(agent_url, session_id, "c1")
            == "denied",
            RUN_TIMEOUT,
            "the altered-approval refusal",
        )
        assert _journal_count(scenario, "effect:fixture.priv:m1") == 0

        # The REAL payload-bound approval resumes; the effect runs once.
        _send_wake(
            run_id,
            agent_url,
            {"kind": "approval", "approval_id": approval_id, "decision": "approve"},
        )
        _await_terminal(
            client, run_id, "succeeded", RUN_TIMEOUT, "approval resume"
        )
    finally:
        for worker in (worker1, worker2):
            if worker is not None:
                _stop(worker)
        client.close()

    assert _journal_count(scenario, "effect:fixture.priv:m1") == 1
    assert _journal_count(scenario, "begin:fixture.priv:m1") == 1
    entries = _read_session_entries(agent_url, session_id)
    results = [e for e in entries if e["kind"] == "tool_result"]
    assert [(e["payload"]["call_id"], e["payload"]["status"]) for e in results] == [
        ("c1", "denied"),
        ("c2", "executed"),
    ]
    # Resume attempts live on their OWN rows: the ALTERED presentation was
    # refused (failed, nothing spent), the REAL payload-bound approval
    # executed (succeeded, approval spent and consumed).
    resume_rows = [
        r for r in _read_step_rows(agent_url, run_id) if r["seq"] >= 1000
    ]
    assert [r["state"] for r in resume_rows] == ["failed", "succeeded"]
    spent_rows = [
        r
        for r in resume_rows
        if (r["receipt"] or {}).get("approval_spent") is True
    ]
    assert len(spent_rows) == 1 and spent_rows[0]["state"] == "succeeded"
    assert _run(_run_states(factory, run_id)) == (
        "queued",
        "running",
        "waiting_approval",
        "running",
        "waiting_approval",
        "running",
        "succeeded",
    )
    assert _read_sessions_row(agent_url, session_id)["status"] == "idle"


def _current_tool_result_status(
    agent_url: str, session_id: str, call_id: str
) -> str | None:
    entries = _read_session_entries(agent_url, session_id)
    for entry in entries:
        if entry["kind"] == "tool_result" and entry["payload"].get("call_id") == call_id:
            return str(entry["payload"]["status"])
    return None


async def await_approval_for(
    factory: async_sessionmaker[AsyncSession],
    run_id: str,
    server_id: uuid.UUID,
    capability_id: str,
    payload: dict,
) -> uuid.UUID:
    """Create one approval row bound to the GIVEN (possibly altered) payload."""
    from mai.capabilities.dispatch import canonical_payload_hash

    store = ApprovalStore(factory)
    from mai.policy.approvals import ApprovalRequest

    from datetime import timedelta

    request = ApprovalRequest(
        server_id=server_id,
        capability_id=capability_id,
        payload_hash=canonical_payload_hash(payload),
        policy_revision=1,
        created_by="test",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    created = await store.create(request)
    return created.approval_id


# ---------------------------------------------------------------------------
# 4. steering + cancel controls
# ---------------------------------------------------------------------------


def test_steering_reaches_the_model_and_log(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(
        scenario,
        [
            {"gate": "model-gate", "then": _tool_call_entry()},
            {"text": "steered and done"},
        ],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker: subprocess.Popen | None = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(run_id))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id, session_id, client_request_id=str(uuid.uuid4())
                )
            ),
        )
        # Parked inside the model round (before the first tool boundary).
        _wait_until(
            lambda: _journal_count(scenario, "model_call") == 1,
            RUN_TIMEOUT,
            "the gated model call",
        )
        submitted = _run(
            asubmit_session_control(
                factory,
                session_id=uuid.UUID(session_id),
                server_id=uuid.UUID(SERVER_ID),
                request=__import__(
                    "mai.contracts.sessions", fromlist=["SessionControlRequest"]
                ).SessionControlRequest(
                    session_id=uuid.UUID(session_id),
                    action=__import__(
                        "mai.contracts.sessions", fromlist=["ControlActionKind"]
                    ).ControlActionKind.STEER,
                    client_request_id=uuid.uuid4(),
                    target_run_id=uuid.UUID(run_id),
                    note="focus on the auth service logs",
                ),
                active_run_id=uuid.UUID(run_id),
                database_url=agent_url,
            )
        )
        assert submitted.accepted is True
        # Release the gate: the boundary drain injects the steering note.
        (scenario / "gates" / "model-gate").write_text("go", encoding="utf-8")
        _await_terminal(
            client, run_id, "succeeded", RUN_TIMEOUT, "steered run"
        )
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    lines = _journal_lines(scenario)
    assert "saw_steering" in lines, lines
    entries = _read_session_entries(agent_url, session_id)
    control_entries = [e for e in entries if e["kind"] == "control"]
    assert len(control_entries) == 1
    injected = [
        e
        for e in entries
        if e["kind"] == "user_message"
        and "focus on the auth service logs" in str(e["payload"].get("text", ""))
    ]
    assert injected, "the steering note is durable in the session log"
    acks = [
        e
        for e in entries
        if e["durable_source_id"].startswith("steer:control:")
    ]
    assert acks, "the steering entry is consumed exactly once (ack entry)"


def test_cancel_settles_both_authorities_without_effect(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(
        scenario,
        [
            {"gate": "model-gate", "then": _tool_call_entry()},
            {"text": "never reached"},
        ],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker: subprocess.Popen | None = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(run_id))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id, session_id, client_request_id=str(uuid.uuid4())
                )
            ),
        )
        _wait_until(
            lambda: _journal_count(scenario, "model_call") == 1,
            RUN_TIMEOUT,
            "the gated model call",
        )
        submitted = _run(
            asubmit_session_control(
                factory,
                session_id=uuid.UUID(session_id),
                server_id=uuid.UUID(SERVER_ID),
                request=__import__(
                    "mai.contracts.sessions", fromlist=["SessionControlRequest"]
                ).SessionControlRequest(
                    session_id=uuid.UUID(session_id),
                    action=__import__(
                        "mai.contracts.sessions", fromlist=["ControlActionKind"]
                    ).ControlActionKind.CANCEL,
                    client_request_id=uuid.uuid4(),
                    target_run_id=uuid.UUID(run_id),
                    note="operator cancelled",
                ),
                active_run_id=uuid.UUID(run_id),
                database_url=agent_url,
            )
        )
        assert submitted.accepted is True
        (scenario / "gates" / "model-gate").write_text("go", encoding="utf-8")
        # The engine lands CANCELLED and the runner aligns the session
        # domain — poll the domain postcondition (it commits after the
        # in-flight step returns).
        _wait_until(
            lambda: (_terminal_engine_state(client, run_id) or "") == "cancelled"
            and (_run(_current_state(factory, run_id)) == "cancelled")
            and (_read_sessions_row(agent_url, session_id)["status"] == "cancelled"),
            RUN_TIMEOUT,
            "the cancelled settlement",
        )
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    # No effect ran; both authorities recorded cancelled.
    assert _journal_count(scenario, "begin:fixture.read:m1") == 0
    assert _run(_run_states(factory, run_id))[-1] == "cancelled"
    runs = _read_session_runs(agent_url, session_id)
    assert runs[0]["state"] == "cancelled"
    assert _read_sessions_row(agent_url, session_id)["status"] == "cancelled"
    entries = _read_session_entries(agent_url, session_id)
    assert any(
        e["kind"] == "run_state" and e["payload"].get("state") == "cancelled"
        for e in entries
    )
    assert not [e for e in entries if e["kind"] == "tool_result"]


# ---------------------------------------------------------------------------
# 5. unrelated generic job on the same worker/engine
# ---------------------------------------------------------------------------


def test_generic_job_executes_on_the_same_worker(
    agent_url: str,
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker: subprocess.Popen | None = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(run_id))
        client.enqueue_run(run_id, "session_fixture.generic", _canonical({"any": "payload"}))
        _await_terminal(client, run_id, "succeeded", RUN_TIMEOUT, "generic job")
    finally:
        if worker is not None:
            _stop(worker)
        client.close()
    assert _journal_count(scenario, "generic:done") == 1


# ---------------------------------------------------------------------------
# 6. import hygiene
# ---------------------------------------------------------------------------

_FORBIDDEN_IMPORT_PREFIXES = (
    "mai.contracts.conversations",
    "mai.contracts.tasks",
    "mai.contracts.objectives",
    "mai.agents.conversation_store",
    "mai.agents.task_store",
    "mai.agents.objective_store",
    "mai.agents.journal_store",
    "mai.agents.intake",
    "pydantic_ai_harness",
)


def test_session_runner_has_zero_old_imports() -> None:
    module_path = (
        BACKEND_ROOT / "src" / "mai" / "agents" / "session_runner.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(_FORBIDDEN_IMPORT_PREFIXES):
                found.append(node.module)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(_FORBIDDEN_IMPORT_PREFIXES):
                    found.append(alias.name)
    assert found == [], found


def test_parse_session_run_intent_round_budget_bounds() -> None:
    """The shared parser pins the 90-round root budget: an explicit 90 is
    accepted, an omitted ``max_rounds`` defaults to 90, and 91 fails
    closed with ``invalid_max_rounds`` (the delegate CHILD guardrail
    1..8 is a separate parser, untouched)."""
    session_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    accepted = parse_session_run_intent(
        _canonical(_intent_document(run_id, session_id, max_rounds=90))
    )
    assert accepted.max_rounds == 90
    defaulted = parse_session_run_intent(
        _canonical(_intent_document(run_id, session_id, max_rounds=None))
    )
    assert defaulted.max_rounds == 90
    with pytest.raises(SessionStageError) as over:
        parse_session_run_intent(
            _canonical(_intent_document(run_id, session_id, max_rounds=91))
        )
    assert over.value.reason == "invalid_max_rounds"


def test_contract_rejection_detail_is_value_free() -> None:
    """A contract-rejected settlement names WHICH rule fired as sorted,
    deduplicated ``contract:code:field`` pairs only — bounded to
    ``MAX_DETAIL_CHARS`` — so operators skip the DBOS pickle; field
    values and messages never enter the detail (C01 rule 3), and an
    exception without classified errors settles detail-less (previous
    behavior)."""
    from mai.agents.session_runner import _contract_rejection_detail
    from mai.contracts.sessions import (
        MAX_DETAIL_CHARS,
        SessionContractError,
        SessionFieldError,
    )

    error = SessionContractError(
        [
            SessionFieldError(
                "forbidden_field",
                "payload",
                "entry payload exceeds the bounded entry budget",
            ),
            SessionFieldError(
                "forbidden_field",
                "payload",
                "duplicate identity carrying a secret sk-SENTINEL value",
            ),
            SessionFieldError("invalid_field", "tool_calls", "bad shape"),
        ]
    )
    detail = _contract_rejection_detail(error)
    assert detail == "contract:forbidden_field:payload,invalid_field:tool_calls"
    assert len(detail) <= MAX_DETAIL_CHARS
    assert "SENTINEL" not in detail and "budget" not in detail

    many = SessionContractError(
        [
            SessionFieldError("forbidden_field", f"field-{n}", "value-free")
            for n in range(100)
        ]
    )
    assert len(_contract_rejection_detail(many)) == MAX_DETAIL_CHARS

    assert _contract_rejection_detail(SessionContractError([])) is None


# ---------------------------------------------------------------------------
# R04 fix-round focused tests (M1-M4)
# ---------------------------------------------------------------------------


def test_stray_wake_never_denies_a_pending_approval_r04_m1(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """M1: a non-approval wake on RESUME_TOPIC (e.g. recovery.resolve) is
    consumed as a re-check trigger — the park CONTINUES and the approval
    still resolves to its payload-bound execution, never to a denial."""
    composition(capability=PRIV_CAP)
    _write_policy_files(scenario, grants=[{PRIV_CAP: True}])
    _script(
        scenario,
        [
            _tool_call_entry(name="fixture_priv", marker="m1", call_id="c1"),
            {"text": "approved after a stray wake"},
        ],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker: subprocess.Popen | None = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(run_id, requested_grants=(PRIV_CAP,)))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id,
                    session_id,
                    allowed=(PRIV_CAP,),
                    mode="ask",
                    approval_wait_seconds=120.0,
                    wait_poll_seconds=0.5,
                    client_request_id=str(uuid.uuid4()),
                )
            ),
        )
        _wait_until(
            lambda: _read_sessions_row(agent_url, session_id)["status"]
            == "waiting_approval",
            RUN_TIMEOUT,
            "the waiting_approval park",
        )
        # A stray non-approval wake (the recovery.resolve shape) arrives
        # first: the park must ignore it, not deny the approval.
        _send_wake(run_id, agent_url, {"kind": "recovery.resolve", "seq": 1})
        time.sleep(2)
        assert (
            _read_sessions_row(agent_url, session_id)["status"]
            == "waiting_approval"
        ), "a stray wake must not end the approval wait"
        row = _read_step_rows(agent_url, run_id)[0]
        approval_id = row["receipt"]["approval_id"]
        _send_wake(
            run_id,
            agent_url,
            {"kind": "approval", "approval_id": approval_id, "decision": "approve"},
        )
        _await_terminal(client, run_id, "succeeded", RUN_TIMEOUT, "approval resume")
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    # The stray wake never produced a denial; the effect ran exactly once.
    statuses = [
        e["payload"]["status"]
        for e in _read_session_entries(agent_url, session_id)
        if e["kind"] == "tool_result"
    ]
    assert statuses == ["executed"]
    assert _journal_count(scenario, "effect:fixture.priv:m1") == 1


def test_approval_expiry_fails_run_and_marks_group_unresolved_r04_m3(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """M3: the approval wait expiry settles the run FAILED with the
    classified reason (run_v2 parity), marks the tool group unresolved and
    never stamps a false 'complete'."""
    composition(capability=PRIV_CAP)
    _write_policy_files(scenario, grants=[{PRIV_CAP: True}])
    _script(
        scenario,
        [
            _tool_call_entry(name="fixture_priv", marker="m1", call_id="c1"),
            {"text": "never reached"},
        ],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker: subprocess.Popen | None = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(run_id, requested_grants=(PRIV_CAP,)))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id,
                    session_id,
                    allowed=(PRIV_CAP,),
                    mode="ask",
                    approval_wait_seconds=2.0,
                    wait_poll_seconds=0.5,
                    client_request_id=str(uuid.uuid4()),
                )
            ),
        )
        _wait_until(
            lambda: _read_sessions_row(agent_url, session_id)["status"]
            == "waiting_approval",
            RUN_TIMEOUT,
            "the waiting_approval park",
        )
        # No decision arrives: the persisted deadline expires the park.
        _await_terminal(client, run_id, "failed", RUN_TIMEOUT, "approval expiry")
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    assert _run(_run_states(factory, run_id)) == (
        "queued",
        "running",
        "waiting_approval",
        "running",
        "failed",
    )
    assert _read_sessions_row(agent_url, session_id)["status"] == "failed"
    assert _read_session_runs(agent_url, session_id)[0]["state"] == "failed"
    # Zero effect; the group is marked unresolved (never false 'complete').
    assert _journal_count(scenario, "effect:fixture.priv:m1") == 0
    entries = _read_session_entries(agent_url, session_id)
    groups = [
        e
        for e in entries
        if e["kind"] == "assistant_message" and e["payload"].get("tool_calls")
    ]
    assert groups and groups[0]["payload"]["tool_group_state"] == "unresolved"
    results = [e for e in entries if e["kind"] == "tool_result"]
    assert results == [], "an expired approval persists no tool result"
    events = _read_events(agent_url, run_id)
    failed = [event for event in events if event.type == "agent.failed"]
    assert failed and failed[0].data.get("reason") == "approval_expired"


def test_pause_stops_at_boundary_with_reason_paused_r04_m4(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """M4: PAUSE is a deterministic boundary stop — the run settles
    cancelled (facade) with reason_code 'paused', the header goes idle and
    no effect runs."""
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(
        scenario,
        [
            {"gate": "model-gate", "then": _tool_call_entry()},
            {"text": "never reached"},
        ],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker: subprocess.Popen | None = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(run_id))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id, session_id, client_request_id=str(uuid.uuid4())
                )
            ),
        )
        _wait_until(
            lambda: _journal_count(scenario, "model_call") == 1,
            RUN_TIMEOUT,
            "the gated model call",
        )
        submitted = _run(
            asubmit_session_control(
                factory,
                session_id=uuid.UUID(session_id),
                server_id=uuid.UUID(SERVER_ID),
                request=__import__(
                    "mai.contracts.sessions", fromlist=["SessionControlRequest"]
                ).SessionControlRequest(
                    session_id=uuid.UUID(session_id),
                    action=__import__(
                        "mai.contracts.sessions", fromlist=["ControlActionKind"]
                    ).ControlActionKind.PAUSE,
                    client_request_id=uuid.uuid4(),
                    target_run_id=uuid.UUID(run_id),
                    note="operator paused",
                ),
                active_run_id=uuid.UUID(run_id),
                database_url=agent_url,
            )
        )
        assert submitted.accepted is True
        (scenario / "gates" / "model-gate").write_text("go", encoding="utf-8")
        # A pause boundary-stop completes the workflow (the reviewed
        # runner's semantics: stopped_by rides the summary event); the
        # dual-authority DOMAIN postcondition is what the settlement
        # guarantees (facade cancelled, session cancelled, header
        # cancelled, reason 'paused', zero effect).
        _wait_until(
            lambda: (_run(_current_state(factory, run_id)) == "cancelled")
            and (_read_sessions_row(agent_url, session_id)["status"] == "cancelled")
            and any(
                e["kind"] == "run_state"
                and e["payload"].get("reason_code") == "paused"
                for e in _read_session_entries(agent_url, session_id)
            ),
            RUN_TIMEOUT,
            "the paused settlement",
        )
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    assert _journal_count(scenario, "begin:fixture.read:m1") == 0
    assert _run(_run_states(factory, run_id))[-1] == "cancelled"
    runs = _read_session_runs(agent_url, session_id)
    assert runs[0]["state"] == "cancelled"
    entries = _read_session_entries(agent_url, session_id)
    assert any(
        e["kind"] == "run_state" and e["payload"].get("reason_code") == "paused"
        for e in entries
    )
    assert not [e for e in entries if e["kind"] == "tool_result"]


# ---------------------------------------------------------------------------
# M2: replayed-terminal repair appends the missing session result
# ---------------------------------------------------------------------------


def _in_process_executor(factory: async_sessionmaker[AsyncSession], tmp: pathlib.Path):
    """A real SessionRunExecutor composed in-process (real dispatcher,
    real ApprovalStore, journaled executor) for direct-step tests."""
    from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
    from mai.credentials.env import EnvResolver
    from mai.plugins.registry import CapabilityRegistry, parse_descriptor
    from mai.policy.evaluate import Policy

    descriptor = parse_descriptor(
        {
            "identifier": READ_CAP,
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
            "effect_class": "read",
            "resource_scope": [],
            "required_grants": [],
        },
        source="<fixture.read descriptor>",
    )
    registry = CapabilityRegistry()
    registry.register(descriptor)

    async def executor_call(call):
        document = {
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }
        return ExecutorResult(ok=True, output=json.dumps(document, sort_keys=True))

    env_file = tmp / "fixture.env"
    env_file.write_text(f"{SECRET_NAME}=x\n", encoding="utf-8")
    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(factory),
        grants=lambda: {READ_CAP: {uuid.UUID(SERVER_ID)}},
        policy=lambda: Policy(revision=1, denied_capabilities=frozenset()),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(str(env_file)),
        executor=executor_call,
    )
    model_root = tmp / "model-root"
    model_root.mkdir(parents=True, exist_ok=True)
    return session_runner_mod.SessionRunExecutor(
        sessions=factory,
        dispatcher=dispatcher,
        registry=registry,
        model_store=ModelConfigStore(str(model_root)),
        data_root=str(tmp),
        locks=ResourceLockServiceMod(str(_fixture_url_for_locks())),
        resolver=None,
        model_builder=None,
    )


def _fixture_url_for_locks() -> str:
    return os.environ.get("MAI_DATABASE_URL") or os.environ.get(
        LIVE_URL_VAR, "postgresql://localhost/mai"
    )


# Local import aliases for the in-process composition (kept beside use to
# avoid shadowing the subprocess-worker module surface).
ResourceLockServiceMod = __import__(
    "mai.jobs.resource_locks", fromlist=["ResourceLockService"]
).ResourceLockService
session_runner_mod = __import__(
    "mai.agents.session_runner", fromlist=["SessionRunExecutor"]
)


def test_replayed_terminal_appends_missing_result_r04_m2(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    tmp_path: pathlib.Path,
) -> None:
    """M2: a crash between the outcome commit and the session append leaves
    a terminal step row WITHOUT a session result. The repair reconstructs
    the honest ToolResultEntry from the committed receipt, idempotently
    keyed — the executed result is never absent from the durable log."""
    import sqlalchemy as sa

    from mai.jobs.steps import StepIntent, StepState

    executor = _in_process_executor(factory, tmp_path)
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    _run(insert_run(run_id))
    intent = parse_session_run_intent(
        _canonical(
            _intent_document(
                run_id, session_id, client_request_id=str(uuid.uuid4())
            )
        )
    )
    _run(_ensure_run_row_helper(executor, intent))
    call = {
        "tool_call_id": "c1",
        "tool_name": "fixture_read",
        "capability_id": READ_CAP,
        "args": {"marker": "m1"},
        "seq": 100,
    }
    spec = StepIntent(seq=100, capability_id=READ_CAP, payload=dict(call["args"]))
    _run(executor.steps.aensure_step_intent(run_id, spec))
    _run(executor.steps.amark_running(run_id, 100, READ_CAP))
    # The committed outcome receipt (executor_ok True, effect executed):
    # the SIGKILL happened right after this commit, before the append.
    _run(
        executor.steps.apersist_outcome(
            run_id,
            100,
            READ_CAP,
            StepState.SUCCEEDED,
            {
                "v": 1,
                "op_id": None,
                "capability_id": READ_CAP,
                "allowed": True,
                "reason": "allowed",
                "effect": "executed",
                "payload_hash": "a" * 64,
                "policy_revision": 1,
                "executor_ok": True,
            },
            reason="allowed",
            op_id=None,
        )
    )
    row = _run(executor.steps.aread_step(run_id, 100))
    assert row is not None and row["state"] == "succeeded"
    # No session result yet (the crash window).
    assert not [
        e
        for e in _read_session_entries(agent_url, session_id)
        if e["kind"] == "tool_result"
    ]

    call_json = json.dumps(call)
    outcome_1 = _run_replay_step(executor, intent.to_json(), call_json, 0, row_dict=row)
    assert outcome_1["status"] == "executed" and outcome_1["state"] == "succeeded"
    entries = _read_session_entries(agent_url, session_id)
    results = [e for e in entries if e["kind"] == "tool_result"]
    assert len(results) == 1 and results[0]["payload"]["status"] == "executed"
    assert results[0]["payload"]["effect_certainty"] == "certain"

    # Idempotent: a replayed repair (row still terminal) adds no duplicate.
    row_again = _run(executor.steps.aread_step(run_id, 100))
    outcome_2 = _run_replay_step(executor, intent.to_json(), call_json, 0, row_dict=row_again)
    assert outcome_2["status"] == "executed"
    results_again = [
        e
        for e in _read_session_entries(agent_url, session_id)
        if e["kind"] == "tool_result"
    ]
    assert len(results_again) == 1
    del sa


async def _ensure_run_row_helper(executor, intent):
    from mai.agents.session_runner import _ensure_run_row

    await _ensure_run_row(executor, intent)


def _run_replay_step(executor, payload_json, call_json, round_index, *, row_dict):
    """Drive the repair helper directly (the step decorator is skipped)."""
    from mai.agents.session_runner import _repair_replayed_terminal_result

    async def _body():
        intent = parse_session_run_intent(
            payload_json
            if isinstance(payload_json, str)
            else json.dumps(payload_json)
        )
        return await _repair_replayed_terminal_result(
            executor,
            intent=intent,
            round_index=round_index,
            call=json.loads(call_json) if isinstance(call_json, str) else dict(call_json),
            row=row_dict,
        )

    return executor.run(_body())


def test_repair_reconstructs_denied_result_from_not_attempted_receipt_frr_t2(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    tmp_path: pathlib.Path,
) -> None:
    """FRR T2: the replay/repair seam (SIGKILL between outcome commit and
    session append) reconstructs a ``denied`` ToolResultEntry from a
    committed ``not_attempted`` receipt — the T1 denial shape — and the
    repair result carries an error_code (the receipt's ``reason`` token,
    the repair surface's existing vocabulary) instead of staying empty."""
    from mai.jobs.steps import StepIntent, StepState

    executor = _in_process_executor(factory, tmp_path)
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    _run(insert_run(run_id))
    intent = parse_session_run_intent(
        _canonical(
            _intent_document(
                run_id, session_id, client_request_id=str(uuid.uuid4())
            )
        )
    )
    _run(_ensure_run_row_helper(executor, intent))
    call = {
        "tool_call_id": "c1",
        "tool_name": "fixture_read",
        "capability_id": READ_CAP,
        "args": {"marker": "m1"},
        "seq": 100,
    }
    spec = StepIntent(seq=100, capability_id=READ_CAP, payload=dict(call["args"]))
    _run(executor.steps.aensure_step_intent(run_id, spec))
    _run(executor.steps.amark_running(run_id, 100, READ_CAP))
    # The committed outcome receipt (T1 deterministic denial: the executor
    # raised PreEffectError before any effect) — the SIGKILL happened right
    # after this commit, before the session append.
    _run(
        executor.steps.apersist_outcome(
            run_id,
            100,
            READ_CAP,
            StepState.FAILED,
            {
                "v": 1,
                "op_id": None,
                "capability_id": READ_CAP,
                "allowed": False,
                "reason": "executor_failed",
                "effect": "not_attempted",
                "payload_hash": "a" * 64,
                "policy_revision": 1,
                "executor_ok": None,
                "error_type": "FileReadError",
                "error_code": "path_denied",
            },
            reason="executor_failed",
            op_id=None,
        )
    )
    row = _run(executor.steps.aread_step(run_id, 100))
    assert row is not None and row["state"] == "failed"
    assert not [
        e
        for e in _read_session_entries(agent_url, session_id)
        if e["kind"] == "tool_result"
    ]

    call_json = json.dumps(call)
    outcome = _run_replay_step(executor, intent.to_json(), call_json, 0, row_dict=row)
    assert outcome["status"] == "denied" and outcome["state"] == "failed"
    entries = _read_session_entries(agent_url, session_id)
    results = [e for e in entries if e["kind"] == "tool_result"]
    assert len(results) == 1
    payload = results[0]["payload"]
    assert payload["status"] == "denied"
    assert payload["effect_certainty"] == "certain"
    assert payload["error_code"]  # the receipt reason token, never empty
    assert payload["error_detail"] is None

    # Idempotent: a replayed repair adds no duplicate entry.
    row_again = _run(executor.steps.aread_step(run_id, 100))
    outcome_again = _run_replay_step(
        executor, intent.to_json(), call_json, 0, row_dict=row_again
    )
    assert outcome_again["status"] == "denied"
    results_again = [
        e
        for e in _read_session_entries(agent_url, session_id)
        if e["kind"] == "tool_result"
    ]
    assert len(results_again) == 1


# ---------------------------------------------------------------------------
# 13. S5: legacy admission without the facade settles failed, not queued
# ---------------------------------------------------------------------------


def _legacy_root_run_row(
    factory: async_sessionmaker[AsyncSession], session_id: str, run_id: str
) -> object:
    """The PRE-hotfix root admission footprint (the original production
    defect shape): the ``session_runs`` row WITHOUT any ``job_runs``
    facade — exactly what the old ``acreate_run``-only admission left
    behind. The reviewed ``insert_run`` fixture is deliberately NOT
    used."""
    from mai.session_store import acreate_run

    async def _body():
        return await acreate_run(
            factory,
            session_id=uuid.UUID(session_id),
            server_id=uuid.UUID(SERVER_ID),
            run_id=uuid.UUID(run_id),
            model_profile="fixture-fast",
            client_request_id=uuid.uuid4(),
        )

    return _run(_body())


def _workflow_terminal_error(url: str, run_id: str) -> bool:
    """Whether the DBOS workflow has landed ERROR (terminal for this
    fault shape: the bootstrap step cannot observe a missing run)."""
    with psycopg.connect(url) as conn:
        status = conn.execute(
            "SELECT status FROM dbos.workflow_status WHERE workflow_uuid = %s",
            (run_id,),
        ).fetchone()
    return status is not None and str(status[0]).upper() == "ERROR"


def test_missing_facade_bootstrap_settles_session_failed_not_queued(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    scenario: pathlib.Path,
    composition,
) -> None:
    """S5 (queued-chat hotfix): a legacy admission (session run row, NO
    facade — the original production shape) must settle the durable
    session state ``failed`` with an honest reason when the real worker
    hits the missing bootstrap metadata. The failure PRECEDES model
    binding: zero model calls, zero tool effects, and the facade is
    never fabricated."""
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    # The script must never be consumed; its presence proves the model
    # path was reachable but never reached.
    _script(scenario, [{"text": "never reached"}])
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    _legacy_root_run_row(factory, session_id, run_id)
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w-legacy")
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(run_id, session_id, client_request_id=str(uuid.uuid4()))
            ),
        )
        _wait_until(
            lambda: _workflow_terminal_error(agent_url, run_id),
            RUN_TIMEOUT,
            "legacy-admission workflow to land ERROR",
        )
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    # The session domain settled honestly: failed run + failed header,
    # never stranded queued.
    runs = _read_session_runs(agent_url, session_id)
    assert len(runs) == 1, runs
    assert runs[0]["state"] == "failed", (
        f"session authority stranded in {runs[0]['state']!r} after the "
        "missing-facade bootstrap failure (must settle failed)"
    )
    header = _read_sessions_row(agent_url, session_id)
    assert header is not None and header["status"] == "failed"
    # The durable run_state entry carries the honest reason + evidence.
    entries = _read_session_entries(agent_url, session_id)
    failures = [
        entry["payload"]
        for entry in entries
        if entry["kind"] == "run_state" and entry["payload"]["state"] == "failed"
    ]
    assert failures, entries
    assert failures[0].get("reason_code") == "unexpected_failure"
    assert failures[0].get("detail"), "original bootstrap evidence must be preserved"
    # Zero model/tool activity and NO fabricated facade.
    lines = _journal_lines(scenario)
    assert "model_call" not in lines, lines
    assert not any(line.startswith("begin:") for line in lines), lines
    with psycopg.connect(agent_url) as conn:
        facades = conn.execute(
            "SELECT COUNT(*) FROM job_runs WHERE run_id = %s",
            (uuid.UUID(run_id),),
        ).fetchone()[0]
    assert facades == 0, "the facade must never be fabricated by settlement"


def _drop_facade_row(url: str, run_id: str) -> None:
    """Simulate an externally dropped facade mid-run (S5 second catch):
    events first (RESTRICT children), then the run row — job_tags go in
    the same transaction (the 0011 triggers are deferrable)."""
    with psycopg.connect(url) as conn:
        conn.execute(
            "DELETE FROM job_events WHERE run_id = %s", (uuid.UUID(run_id),)
        )
        conn.execute(
            "DELETE FROM job_tags WHERE job_id = %s", (uuid.UUID(run_id),)
        )
        conn.execute(
            "DELETE FROM job_runs WHERE run_id = %s", (uuid.UUID(run_id),)
        )
        conn.commit()


def test_missing_facade_model_error_settles_safe_identity_no_sentinels(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """E2 (F3b): a sentinel-bearing model failure reaching the
    missing-facade settlement through the REAL workflow settles failed
    with a VALUE-FREE detail — the exception TYPE only, never the raw
    message — and zero sentinels anywhere durable or logged (this test
    supersedes the S5-fix1 raw-evidence expectation with the explicit
    safe-identity contract; the length bound is still proven separately
    by the boundary contract test below)."""
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    # The model parks at the gate; the test drops the facade row, then
    # releases the gate so the model raises the sentinel-soup error and
    # the real settlement runs against a missing facade (second catch).
    _script(scenario, [{"gate": "g-s5fix1", "then": {"error": _E2_SOUP}}])
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    _run(insert_run(run_id))
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w-s5f1")
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(run_id, session_id, client_request_id=str(uuid.uuid4()))
            ),
        )
        _wait_until(
            lambda: "model_call" in _journal_lines(scenario),
            RUN_TIMEOUT,
            "model to reach the gate",
        )
        _drop_facade_row(agent_url, run_id)
        (scenario / "gates" / "g-s5fix1").write_text("", encoding="utf-8")
        _wait_until(
            lambda: _workflow_terminal_error(agent_url, run_id),
            RUN_TIMEOUT,
            "dropped-facade workflow to land ERROR",
        )
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    runs = _read_session_runs(agent_url, session_id)
    assert len(runs) == 1 and runs[0]["state"] == "failed", runs
    header = _read_sessions_row(agent_url, session_id)
    assert header is not None and header["status"] == "failed"
    failures = [
        entry["payload"]
        for entry in _read_session_entries(agent_url, session_id)
        if entry["kind"] == "run_state" and entry["payload"]["state"] == "failed"
    ]
    assert failures, "the durable failed run_state entry must exist"
    detail = str(failures[0].get("detail") or "")
    # The EXPLICIT safe-identity contract: fixed prefix + exception TYPE
    # only — no raw message, no sentinel soup, no long unicode payload.
    assert detail == (
        "facade row missing at settlement; original failure: runtime_error"
    ), detail
    assert len(detail) <= 500
    # Zero sentinels across session history, DBOS sinks and worker logs.
    assert _sentinel_hits(agent_url, run_id, session_id, scenario) == []
    # No tool effects and no fabricated facade.
    assert not any(line.startswith("begin:") for line in _journal_lines(scenario))
    with psycopg.connect(agent_url) as conn:
        facades = conn.execute(
            "SELECT COUNT(*) FROM job_runs WHERE run_id = %s",
            (uuid.UUID(run_id),),
        ).fetchone()[0]
    assert facades == 0


# E2 synthetic sentinels (NEVER real secrets): token/DSN/auth/query
# shapes that must never reach durable session detail, evidence attrs,
# serialized DBOS sinks or runner logs.
_E2_SENTINELS = (
    "sk-e2-SENTINEL-abc123",
    "SECRETTOKEN-e2-xyz",
    "tok-e2-BEARER-987",
    "postgres://e2user:e2pass@127.0.0.1:5432/e2db",
    "Authorization: Bearer tok-e2-BEARER-987",
    "SELECT * FROM secrets WHERE token='sk-e2-SENTINEL-abc123'",
)
_E2_SOUP = " ; ".join(_E2_SENTINELS) + " ; " + ("échec-🔐-" * 60)


def _sentinel_hits(
    url: str,
    run_id: str,
    session_id: str,
    scenario: pathlib.Path,
    needles: tuple[str, ...] = _E2_SENTINELS,
) -> list[tuple[str, str]]:
    """Every (location, sentinel) leak across the affected durable and
    diagnostic surfaces: session history payloads, serialized DBOS
    error/output sinks (raw AND base64-decoded as the harness does) and
    the worker logs."""
    import base64

    hits: list[tuple[str, str]] = []
    for entry in _read_session_entries(url, session_id):
        blob = json.dumps(entry["payload"], ensure_ascii=False, default=str)
        for sentinel in needles:
            if sentinel in blob:
                hits.append((f"session_entries:{entry['kind']}", sentinel))
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            """
            SELECT 'workflow_status.error', COALESCE(error, '')
            FROM dbos.workflow_status WHERE workflow_uuid = %s
            UNION ALL
            SELECT 'workflow_output.error', COALESCE(error, '')
            FROM dbos.workflow_output WHERE workflow_uuid = %s
            UNION ALL
            SELECT 'workflow_output.output', COALESCE(output, '')
            FROM dbos.workflow_output WHERE workflow_uuid = %s
            UNION ALL
            SELECT 'operation_outputs.output', COALESCE(output, '')
            FROM dbos.operation_outputs WHERE workflow_uuid = %s
            UNION ALL
            SELECT 'operation_outputs.error', COALESCE(error, '')
            FROM dbos.operation_outputs WHERE workflow_uuid = %s
            """,
            (run_id, run_id, run_id, run_id, run_id),
        ).fetchall()
    for source, blob in rows:
        for sentinel in needles:
            if sentinel in (blob or ""):
                hits.append((source, sentinel))
            try:  # decode base64-serialized sinks the way DBOS does
                decoded = base64.b64decode(blob or "", validate=True).decode(
                    "utf-8", errors="replace"
                )
            except Exception:  # noqa: BLE001 - not base64
                continue
            if sentinel in decoded:
                hits.append((f"{source}(b64)", sentinel))
    for log_path in sorted(scenario.glob("*.log")):
        text = log_path.read_text(encoding="utf-8", errors="replace")
        for sentinel in needles:
            if sentinel in text:
                hits.append((f"log:{log_path.name}", sentinel))
    return hits


def test_e2_sentinels_never_persist_on_facade_present_model_failure(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """E2 (F3b): with the facade PRESENT, a sentinel-bearing model
    failure settles failed with detail=None (the healthy settlement
    passes no detail) and zero sentinels anywhere durable or logged —
    including the worker log, which previously printed the raw error."""
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(scenario, [{"error": _E2_SOUP}])
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    _run(insert_run(run_id))
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w-e2p")
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(run_id, session_id, client_request_id=str(uuid.uuid4()))
            ),
        )
        _wait_until(
            lambda: _workflow_terminal_error(agent_url, run_id),
            RUN_TIMEOUT,
            "sentinel-bearing model failure to land ERROR",
        )
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    runs = _read_session_runs(agent_url, session_id)
    assert len(runs) == 1 and runs[0]["state"] == "failed", runs
    failures = [
        entry["payload"]
        for entry in _read_session_entries(agent_url, session_id)
        if entry["kind"] == "run_state" and entry["payload"]["state"] == "failed"
    ]
    assert failures, "the healthy settlement must record the failed run_state"
    assert failures[0].get("detail") is None, failures[0]
    assert failures[0].get("reason_code") == "model_round_failed", failures[0]
    assert _sentinel_hits(agent_url, run_id, session_id, scenario) == []



# E2-fix1 sentinels: the exception CLASS NAME is the leak vector (audit
# F-A) plus a fire-once raw DB error on the unexpected path (audit F-B).
_E2F1_EXC_NAME = "ProbeSECRETTOKENe2sk-e2-CLASSNAME99"
_E2F1_NEEDLES = _E2_SENTINELS + (_E2F1_EXC_NAME,)
_E2B_NEEDLES = ("ER2ProbeB", "sk-e2-MSGb789")


def test_e2f1_sentinel_named_dynamic_class_never_leaks(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """E2-fix1 (F-A): a sentinel-NAMED dynamic exception class (the
    class NAME is data-derived and was previously echoed verbatim by
    _safe_error_identity) raised by the real model through the real
    workflow — facade PRESENT and ABSENT. Diagnostics must carry only
    the trusted category (runtime_error), and the scan must find zero
    sentinels (class name AND message soup) anywhere."""
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])

    # Facade PRESENT: classified stage failure, detail=None, no leaks.
    _script(
        scenario,
        [{"error": _E2_SOUP, "error_type": _E2F1_EXC_NAME}],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    _run(insert_run(run_id))
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w-e2f1a")
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(run_id, session_id, client_request_id=str(uuid.uuid4()))
            ),
        )
        _wait_until(
            lambda: _workflow_terminal_error(agent_url, run_id),
            RUN_TIMEOUT,
            "present-facade sentinel-named failure to land ERROR",
        )
    finally:
        if worker is not None:
            _stop(worker)
        client.close()
    runs = _read_session_runs(agent_url, session_id)
    assert len(runs) == 1 and runs[0]["state"] == "failed", runs
    failures = [
        entry["payload"]
        for entry in _read_session_entries(agent_url, session_id)
        if entry["kind"] == "run_state" and entry["payload"]["state"] == "failed"
    ]
    assert failures and failures[0].get("detail") is None, failures
    assert failures[0].get("reason_code") == "model_round_failed", failures
    assert _sentinel_hits(agent_url, run_id, session_id, scenario, _E2F1_NEEDLES) == []

    # Facade ABSENT: the missing-facade settlement detail carries ONLY
    # the trusted category — never the sentinel class name.
    baseline = _journal_lines(scenario).count("model_call")

    _script(
        scenario,
        [{"gate": "g-e2f1b", "then": {"error": _E2_SOUP, "error_type": _E2F1_EXC_NAME}}],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    _run(insert_run(run_id))
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w-e2f1b")
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(run_id, session_id, client_request_id=str(uuid.uuid4()))
            ),
        )
        _wait_until(
            lambda: _journal_lines(scenario).count("model_call") > baseline,
            RUN_TIMEOUT,
            "absent-facade model to reach the gate",
        )
        _drop_facade_row(agent_url, run_id)
        (scenario / "gates" / "g-e2f1b").write_text("", encoding="utf-8")
        _wait_until(
            lambda: _workflow_terminal_error(agent_url, run_id),
            RUN_TIMEOUT,
            "absent-facade sentinel-named failure to land ERROR",
        )
    finally:
        if worker is not None:
            _stop(worker)
        client.close()
    runs = _read_session_runs(agent_url, session_id)
    assert len(runs) == 1 and runs[0]["state"] == "failed", runs
    failures = [
        entry["payload"]
        for entry in _read_session_entries(agent_url, session_id)
        if entry["kind"] == "run_state" and entry["payload"]["state"] == "failed"
    ]
    assert failures, failures
    assert failures[0].get("detail") == (
        "facade row missing at settlement; original failure: runtime_error"
    ), failures[0]
    assert _sentinel_hits(agent_url, run_id, session_id, scenario, _E2F1_NEEDLES) == []


def test_e2f1_unexpected_failure_wraps_raw_step_exception(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """E2-fix1 (F-B): a sentinel-bearing raw DATABASE error injected at a
    durable non-model write (fire-once deferred constraint trigger on
    session_entries, the reviewer's probe mechanics) crosses a DBOS step
    boundary. The runner must wrap it at the EARLIEST durable-step
    boundary with a safe classified exception (detached cause/attrs), so
    DBOS never pickles the raw object; the session settles failed with
    the trusted database category and zero sentinel hits."""
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(scenario, [{"text": "done"}])
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    _run(insert_run(run_id))
    with psycopg.connect(agent_url) as conn:
        conn.execute("DROP TABLE IF EXISTS _er2_fireonce")
        conn.execute(
            "CREATE TABLE _er2_fireonce "
            "(id int PRIMARY KEY, fired bool NOT NULL DEFAULT false)"
        )
        conn.execute("INSERT INTO _er2_fireonce (id) VALUES (1)")
        conn.execute(
            """
            CREATE OR REPLACE FUNCTION _er2_probe_b() RETURNS trigger AS $$
            BEGIN
                IF (SELECT fired FROM _er2_fireonce WHERE id = 1) THEN
                    RETURN NEW;
                END IF;
                UPDATE _er2_fireonce SET fired = true WHERE id = 1;
                RAISE EXCEPTION '%', 'ER2ProbeB sk-e2-MSGb789 raw failure detail';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        conn.execute(
            "CREATE CONSTRAINT TRIGGER _er2_b AFTER INSERT ON session_entries "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE PROCEDURE "
            "_er2_probe_b()"
        )
        conn.commit()
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w-e2f1b2")
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(run_id, session_id, client_request_id=str(uuid.uuid4()))
            ),
        )
        _wait_until(
            lambda: _workflow_terminal_error(agent_url, run_id),
            RUN_TIMEOUT,
            "fire-once unexpected failure to land ERROR",
        )
    finally:
        if worker is not None:
            _stop(worker)
        client.close()
        with psycopg.connect(agent_url) as conn:
            conn.execute("DROP TRIGGER IF EXISTS _er2_b ON session_entries")
            conn.execute("DROP FUNCTION IF EXISTS _er2_probe_b()")
            conn.commit()

    # The workflow landed ERROR; the RAW DB error never crossed any
    # durable boundary (both steps' pickled errors are the safe
    # classified stage). The session state itself stays queued BY THE
    # INJECTED ARTIFACT: the fire-once trigger aborts the settlement
    # transaction too (the reviewer's own P3 observation) — the leak
    # guarantee here is settlement-INDEPENDENT.
    assert _wf_status(agent_url, run_id) == "ERROR", _wf_status(agent_url, run_id)
    runs = _read_session_runs(agent_url, session_id)
    assert len(runs) == 1 and runs[0]["state"] == "queued", runs
    import base64 as _b64

    with psycopg.connect(agent_url) as conn:
        step_errors = conn.execute(
            "SELECT COALESCE(error, '') FROM dbos.operation_outputs "
            "WHERE workflow_uuid = %s",
            (run_id,),
        ).fetchall()
    decoded = ""
    for (blob,) in step_errors:
        try:
            decoded += _b64.b64decode(blob or "", validate=True).decode(
                "utf-8", errors="replace"
            )
        except Exception:  # noqa: BLE001 - not base64
            continue
    assert "step_unexpected_failure" in decoded, decoded[:200]
    assert "database_error" in decoded, decoded[:200]
    assert _sentinel_hits(agent_url, run_id, session_id, scenario, _E2B_NEEDLES) == []


def test_missing_facade_detail_boundary_contract(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """S5-fix1 (R5): the bounded detail through the REAL construction
    helper and the REAL durable write — at/under/over the canonical
    ``RunStateEntry`` limit, unicode included, never a ValidationError."""
    from types import SimpleNamespace

    from mai.agents.session_runner import (
        _missing_facade_detail,
        _record_session_state,
        parse_session_run_intent,
    )
    from mai.jobs.lifecycle import RunLifecycleError, RunLifecycleErrorCode

    prefix = "facade row missing at settlement; original failure: "
    error = RunLifecycleError(
        RunLifecycleErrorCode.RUN_NOT_FOUND, "no job_runs row for this run_id"
    )
    executor = SimpleNamespace(
        sessions=factory,
        run=lambda coro: _run(coro),
    )

    def _settle_and_read(evidence: str) -> str:
        summary = {
            "state": "failed",
            "reason": "unexpected_failure",
            "detail": evidence,
        }
        detail = _missing_facade_detail(summary, error)  # type: ignore[arg-type]
        session_id = _run(_make_session(factory))
        run_id = str(uuid.uuid4())
        _legacy_root_run_row(factory, session_id, run_id)
        intent = parse_session_run_intent(
            _canonical(_intent_document(run_id, session_id))
        )
        from mai.contracts.sessions import SessionRunState

        _run(
            _record_session_state(  # type: ignore[arg-type]
                executor,
                intent,
                SessionRunState.FAILED,
                reason_code="unexpected_failure",
                detail=detail,
            )
        )
        # The write went through the REAL store contract: the persisted
        # run_state entry carries exactly the bounded detail.
        persisted = [
            entry["payload"]
            for entry in _read_session_entries(agent_url, session_id)
            if entry["kind"] == "run_state"
        ]
        assert persisted, "durable run_state entry must exist"
        assert persisted[0]["detail"] == detail
        return detail

    # The evidence falls back to the lifecycle identity when absent.
    fallback = _missing_facade_detail({}, error)  # type: ignore[arg-type]
    assert fallback == prefix + "lifecycle"  # trusted-category identity
    assert len(fallback) < 500

    # Exactly at the bound: untruncated, durably persisted.
    at_bound = "x" * (500 - len(prefix))
    assert len(_settle_and_read(at_bound)) == 500

    # One over and far over (unicode): truncated to exactly 500 through
    # the real durable write, never a ValidationError.
    for evidence in ("y" * (501 - len(prefix)), "🔐-détail—" * 100):
        assert len(_settle_and_read(evidence)) == 500


# ---------------------------------------------------------------------------
# 14. E1: authoritative terminal state across facade/session/workflow
# ---------------------------------------------------------------------------


def _wf_status(url: str, run_id: str) -> str | None:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT status FROM dbos.workflow_status WHERE workflow_uuid = %s",
            (run_id,),
        ).fetchone()
    return None if row is None else str(row[0]).upper()


def _wf_output(url: str, run_id: str) -> dict | None:
    """The workflow result through the SUPPORTED engine client (DBOS
    serializes results — JSON or base64 pickle per its own policy)."""
    client = EngineClient(url)
    try:
        handle = client._client.retrieve_workflow(run_id)  # noqa: SLF001
        result = handle.get_result()
    except Exception:  # noqa: BLE001 - terminal without a result
        return None
    finally:
        client.close()
    return result if isinstance(result, dict) else None


def _facade_final(url: str, run_id: str) -> str | None:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT data FROM job_events WHERE run_id = %s ORDER BY seq",
            (uuid.UUID(run_id),),
        ).fetchall()
    last = None
    for (data,) in rows:
        payload = data if isinstance(data, dict) else json.loads(data)
        if "to" in payload or "to_state" in payload:
            last = payload.get("to_state") or payload.get("to")
    return str(last) if last is not None else None


def _force_facade_terminal(url: str, run_id: str, target: str) -> None:
    """One EXTERNAL legal transition (the recovery/API seam) while the
    real worker is parked mid-round."""
    from mai.database import create_app_engine
    from mai.jobs.lifecycle import RunLifecycle, RunState

    eng, sessions = create_app_engine(url)

    async def _force() -> None:
        await RunLifecycle(sessions).record_transition(
            run_id, RunState[str(target).upper()]
        )

    try:
        _run(_force())
    finally:
        _run(eng.dispose())


def _run_terminal_states(url: str, session_id: str) -> list[tuple[str, object]]:
    return [
        (entry["payload"].get("state"), entry["payload"].get("reason_code"))
        for entry in _read_session_entries(url, session_id)
        if entry["kind"] == "run_state"
    ]


def _drive_to_terminal(
    agent_url: str,
    scenario: pathlib.Path,
    *,
    run_id: str,
    session_id: str,
    gate: str,
    before_release,
    baseline_calls: int = 0,
) -> int:
    """Park the real worker at the model gate, run the caller's
    ``before_release`` side effect, release the gate and wait for the
    workflow to land any terminal DBOS status. ``baseline_calls`` skips
    model calls already in the shared journal (multi-phase scenarios).
    Returns the model-call count at release time (no-rerun baseline)."""
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w-e1")
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(run_id, session_id, client_request_id=str(uuid.uuid4()))
            ),
        )
        _wait_until(
            lambda: _journal_lines(scenario).count("model_call") > baseline_calls,
            RUN_TIMEOUT,
            "model to reach the gate",
        )
        calls_before = _journal_lines(scenario).count("model_call")
        before_release()
        (scenario / "gates" / gate).write_text("", encoding="utf-8")
        _wait_until(
            lambda: _wf_status(agent_url, run_id)
            in ("SUCCESS", "ERROR", "CANCELLED"),
            RUN_TIMEOUT,
            "workflow to land a terminal status",
        )
        return calls_before
    finally:
        if worker is not None:
            _stop(worker)
        client.close()


def test_e1_dropped_facade_at_success_settlement_lands_error_not_success(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """E1/F1: a SUCCESS round whose settlement finds the facade missing
    must land DBOS ERROR — never SUCCESS carrying a failed result — with
    the session domain failed and zero re-execution."""
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(scenario, [{"gate": "g-e1f1", "then": {"text": "e1-round-ok"}}])
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    _run(insert_run(run_id))
    calls_before = _drive_to_terminal(
        agent_url,
        scenario,
        run_id=run_id,
        session_id=session_id,
        gate="g-e1f1",
        before_release=lambda: _drop_facade_row(agent_url, run_id),
    )

    assert _wf_status(agent_url, run_id) == "ERROR", _wf_status(agent_url, run_id)
    assert _wf_output(agent_url, run_id) is None  # no SUCCESS result exists
    runs = _read_session_runs(agent_url, session_id)
    assert len(runs) == 1 and runs[0]["state"] == "failed", runs
    header = _read_sessions_row(agent_url, session_id)
    assert header is not None and header["status"] == "failed"
    failures = [
        payload
        for payload in _run_terminal_states(agent_url, session_id)
        if payload[0] == "failed"
    ]
    assert failures, "the honest failed settlement must be durable"
    assert failures[0][1] != "unexpected_failure"  # no corrupted reason
    # Zero re-execution and no fabricated facade.
    assert _journal_lines(scenario).count("model_call") == calls_before
    assert not any(line.startswith("begin:") for line in _journal_lines(scenario))
    with psycopg.connect(agent_url) as conn:
        facades = conn.execute(
            "SELECT COUNT(*) FROM job_runs WHERE run_id = %s",
            (uuid.UUID(run_id),),
        ).fetchone()[0]
    assert facades == 0


def test_e1_preterminal_failed_facade_realigned_consistently(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """E1/F2: an externally FAILED facade (the recovery seam) meeting a
    successful round must realign every authority to FAILED — through
    the settlement, not a TERMINAL_IMMUTABLE crash and not a corrupted
    ``unexpected_failure`` second pass."""
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(scenario, [{"gate": "g-e1f2", "then": {"text": "e1-round-ok-2"}}])
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    _run(insert_run(run_id))
    calls_before = _drive_to_terminal(
        agent_url,
        scenario,
        run_id=run_id,
        session_id=session_id,
        gate="g-e1f2",
        before_release=lambda: _force_facade_terminal(agent_url, run_id, "FAILED"),
    )

    assert _wf_status(agent_url, run_id) == "ERROR", _wf_status(agent_url, run_id)
    assert _facade_final(agent_url, run_id) == "failed"
    runs = _read_session_runs(agent_url, session_id)
    assert len(runs) == 1 and runs[0]["state"] == "failed", runs
    header = _read_sessions_row(agent_url, session_id)
    assert header is not None and header["status"] == "failed"
    sequence = _run_terminal_states(agent_url, session_id)
    assert sequence.count(("running", None)) == 1, sequence
    # ONE failed settlement with an honest reason — never the crash
    # handler's misclassified unexpected_failure, never a second pass.
    failed_entries = [entry for entry in sequence if entry[0] == "failed"]
    assert len(failed_entries) == 1, sequence
    assert failed_entries[0][1] != "unexpected_failure", sequence
    assert _journal_lines(scenario).count("model_call") == calls_before


def test_e1_preterminal_cancelled_and_succeeded_facade_align_consistently(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """E1: preterminal CANCELLED and SUCCEEDED facades meeting a
    successful round stay aligned in EVERY authority — the settled
    result carries the facade's terminal value, the engine lands
    CANCELLED for a cancelled facade, and SUCCESS for a succeeded one."""
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    baseline = 0
    for gate, target, expected_status in (
        ("g-e1c", "CANCELLED", "CANCELLED"),
        ("g-e1s", "SUCCEEDED", "SUCCESS"),
    ):
        _script(scenario, [{"gate": gate, "then": {"text": f"round-{target}"}}])
        session_id = _run(_make_session(factory))
        run_id = str(uuid.uuid4())
        _run(insert_run(run_id))
        calls_before = _drive_to_terminal(
            agent_url,
            scenario,
            run_id=run_id,
            session_id=session_id,
            gate=gate,
            before_release=lambda target=target: _force_facade_terminal(
                agent_url, run_id, target
            ),
            baseline_calls=baseline,
        )
        baseline = _journal_lines(scenario).count("model_call")
        assert _wf_status(agent_url, run_id) == expected_status, (
            target,
            _wf_status(agent_url, run_id),
        )
        assert _facade_final(agent_url, run_id) == target.lower()
        runs = _read_session_runs(agent_url, session_id)
        assert len(runs) == 1 and runs[0]["state"] == target.lower(), runs
        header = _read_sessions_row(agent_url, session_id)
        assert header is not None and header["status"] in ("cancelled", "idle")
        if expected_status == "SUCCESS":
            output = _wf_output(agent_url, run_id)
            assert output is not None and output.get("state") == "succeeded", output
        else:
            assert _wf_output(agent_url, run_id) is None
        assert _journal_lines(scenario).count("model_call") == calls_before


# ---------------------------------------------------------------------------
# S2: bridge-receipt projection is redacted and bounded BEFORE the entry
# ---------------------------------------------------------------------------


def test_bridge_outcome_oversized_output_bounded_with_marker_s2() -> None:
    """S2 criterion 1: a bridge receipt carrying the dispatcher's full
    output budget (``host.logs`` output_budget=4096 > the 4,000-char
    preview bound) projects onto ``ToolResultEntry`` WITHOUT a Pydantic
    ``ValidationError``; the preview keeps the explicit truncation marker,
    secret-shaped tokens are redacted before the cut, and an oversized
    error detail stays inside its own contract cap."""
    import dataclasses

    from mai.agents.output_safety import project_text
    from mai.agents.session_history import (
        TRUNCATION_MARKER_TEMPLATE,
        _bound_with_marker,
    )
    from mai.agents.session_runner import _entry_from_bridge_outcome
    from mai.agents.tools import ToolResult
    from mai.capabilities.dispatch import (
        BoundedOutput,
        DispatchOutcome,
        EFFECT_EXECUTED,
    )
    from mai.contracts.sessions import (
        MAX_ERROR_DETAIL_CHARS,
        MAX_TOOL_OUTPUT_PREVIEW_CHARS,
    )

    secret = "sk-s2regression0123456789xyz"
    big_output = (
        f"audit head {secret}\n" + "log line\n" * 470
    )  # > the 4,000-char preview bound
    outcome = DispatchOutcome(
        op_id=uuid.uuid4(),
        ts=datetime.now(timezone.utc),
        capability_id="host.logs",
        server_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        principal="fixture",
        mode="interactive",
        allowed=True,
        reason="allow",
        policy_revision=1,
        effect_class=None,
        payload_hash="a" * 64,
        effect=EFFECT_EXECUTED,
        approval_id=None,
        approval_spent=False,
        approval_code=None,
        executor_ok=True,
        credential_references=(),
        output=BoundedOutput(text=big_output, truncated=False, omitted_bytes=0),
        error_type=None,
    )
    entry = _entry_from_bridge_outcome(outcome, "call-s2")
    assert entry.status == "executed"
    assert entry.output_preview is not None
    # Redaction runs first; the honest marker-bound preview fits the
    # contract bound exactly.
    safe_output = project_text(big_output)
    expected_preview, preview_truncated, preview_omitted = _bound_with_marker(
        safe_output, MAX_TOOL_OUTPUT_PREVIEW_CHARS
    )
    assert preview_truncated and preview_omitted > 0
    assert entry.output_preview == expected_preview
    assert len(entry.output_preview) == MAX_TOOL_OUTPUT_PREVIEW_CHARS
    assert (
        TRUNCATION_MARKER_TEMPLATE.format(omitted=preview_omitted)
        in entry.output_preview
    )
    # Secret-shaped tokens are redacted BEFORE the cut: the kept head
    # cannot leak the key even though it sat inside the kept prefix.
    assert secret not in entry.output_preview

    # error_detail is bounded to its own contract cap (defensive seam: a
    # receipt projection that someday carries detail text can never
    # overflow the entry contract).
    denied_outcome = dataclasses.replace(
        outcome,
        effect="not_attempted",
        allowed=False,
        reason="policy_denied",
        executor_ok=None,
        output=None,
    )
    base = ToolResult.from_outcome(denied_outcome)
    long_detail = "d" * 400  # > MAX_ERROR_DETAIL_CHARS (200)
    original_from_outcome = ToolResult.from_outcome

    def _with_detail(outcome_obj: object) -> ToolResult:
        return dataclasses.replace(base, error_detail=long_detail)

    try:
        ToolResult.from_outcome = classmethod(  # type: ignore[method-assign]
            lambda cls, o: _with_detail(o)
        )
        denied_entry = _entry_from_bridge_outcome(denied_outcome, "call-s2d")
    finally:
        ToolResult.from_outcome = original_from_outcome  # type: ignore[method-assign]
    assert denied_entry.status == "denied"
    assert denied_entry.error_detail is not None
    assert len(denied_entry.error_detail) == MAX_ERROR_DETAIL_CHARS
    assert long_detail not in denied_entry.error_detail


# ---------------------------------------------------------------------------
# ask_03-runtime-policy: the operating policy on EVERY production model
# round (root + child), and the durable one-way secret-route latch.
# In-process seams drive the REAL _round_model/_resolve_run_secret_route_
# latch code against the real fixture DB; worker scenarios prove the
# same contract through the real DBOS worker path.
# ---------------------------------------------------------------------------

SECRET_CAP = "secret.get"

_INPROC_PINS = {"connection": "gateway", "profile": "fast"}


def _inproc_descriptor(capability_id: str, effect_class: str):
    from mai.plugins.registry import parse_descriptor

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


def _inproc_executor(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    scenario: pathlib.Path,
    roots: dict[str, pathlib.Path],
    *,
    allowed: tuple[str, ...],
    captured: list[list[object]],
) -> SessionRunExecutor:
    """A REAL SessionRunExecutor over the fixture DB + scenario roots,
    with a FunctionModel that captures every model request it sees."""
    from mai.capabilities.dispatch import CapabilityDispatcher
    from mai.plugins.registry import CapabilityRegistry as Registry
    from mai.policy.evaluate import Policy as EvalPolicy

    registry = Registry()
    for capability_id in allowed:
        registry.register(_inproc_descriptor(capability_id, "read"))
    grants = {
        capability_id: frozenset({uuid.UUID(SERVER_ID)})
        for capability_id in allowed
    }

    def _capture_model_fn(messages, info):
        captured.append(list(messages))
        return ModelResponse(parts=[TextPart(content="done")])

    env_path = str(scenario / "fixture.env")
    return SessionRunExecutor(
        sessions=factory,
        dispatcher=CapabilityDispatcher(
            registry=registry,
            approvals=ApprovalStore(factory),
            grants=lambda: grants,
            policy=lambda: EvalPolicy(
                revision=1, denied_capabilities=frozenset()
            ),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(env_path),
            executor=lambda call: None,
        ),
        registry=registry,
        model_store=ModelConfigStore(roots["model_root"]),
        data_root=roots["data_root"],
        locks=ResourceLockService(agent_url),
        resolver=EnvResolver(env_path),
        model_builder=lambda **kwargs: FunctionModel(_capture_model_fn),
    )


def _inproc_intent(
    run_id: str,
    session_id: str,
    *,
    allowed: tuple[str, ...],
    parent_run_id: str | None = None,
) -> SessionRunIntent:
    from mai.policy.evaluate import ExecutionContext as ExecContext

    context = ExecContext(
        principal="hash:fixture-principal",
        server_id=uuid.UUID(SERVER_ID),
        run_id=uuid.UUID(run_id),
        parent_scope=None,
        profile_revision="profile-1",
        prompt_revision="prompt-1",
        credential_references=(),
        allowed_capabilities=allowed,
        mode=PolicyMode("delegated"),
    )
    return SessionRunIntent(
        payload_version=1,
        session_id=session_id,
        run_id=run_id,
        server_id=SERVER_ID,
        client_request_id=str(uuid.uuid4()),
        context=context,
        prompt="Run the fixture task and report the marker outcome.",
        parent_run_id=parent_run_id,
        source=SessionSourceKind.API,
        connection="gateway",
        profile="fast",
        max_rounds=4,
        approval_wait_seconds=30.0,
        reconcile_wait_seconds=5.0,
        wait_poll_seconds=0.5,
        document_ref=None,
        runbook_ref=None,
    )


def _read_latch_events(url: str, run_id: str) -> list[dict]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT seq, type, data FROM job_events "
            "WHERE run_id = %s AND type = %s ORDER BY seq",
            (uuid.UUID(run_id), SECRET_ROUTE_LATCHED_EVENT_TYPE),
        ).fetchall()
    for row in rows:
        if isinstance(row["data"], str):
            row["data"] = json.loads(row["data"])
    return rows


def test_round_model_injects_policy_once_on_root_and_child(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """Round seam (D1): every production model round — root AND delegated
    child — carries exactly ONE composed operating-policy instruction on
    the current request; history never duplicates it and no system-prompt
    part appears."""
    roots = composition()
    captured: list[list[object]] = []
    executor = _inproc_executor(
        agent_url, factory, scenario, roots, allowed=(READ_CAP,), captured=captured
    )
    session_id = _run(_make_session(factory))
    root_run = str(uuid.uuid4())
    _run(insert_run(root_run))
    root_intent = _inproc_intent(root_run, session_id, allowed=(READ_CAP,))
    _run(sr._ensure_run_row(executor, root_intent))
    _run(sr._append_user_turn(executor, root_intent))

    record = _run(sr._round_model(executor, root_intent, _INPROC_PINS, 0))
    assert record["status"] == "final"
    assert len(captured) == 1
    messages = captured[-1]
    instructions = str(getattr(messages[-1], "instructions", "") or "")
    assert instructions.startswith("Run role: ROOT run.")
    assert (
        "You are MAI, a supervised server-administration agent." in instructions
    )
    assert "Binding rules:" in instructions
    assert "app.resolve_service" in instructions
    assert "secret-only credential routing" in instructions
    assert "Run role: ROOT run" in instructions
    assert "DELEGATED CHILD" not in instructions
    # The bounded administrator server prompt rides as a subordinate input.
    assert "fixture prompt" in instructions
    # Exactly ONE instructions-bearing request; zero system-prompt parts.
    assert sum(1 for m in messages if str(getattr(m, "instructions", "") or "")) == 1
    assert not [
        part
        for m in messages
        for part in getattr(m, "parts", [])
        if getattr(part, "part_kind", "") == "system-prompt"
    ]

    # The delegated child surface composes the CHILD role of the same policy.
    child_run = str(uuid.uuid4())
    _run(insert_run(child_run))
    child_intent = _inproc_intent(
        child_run, session_id, allowed=(READ_CAP,), parent_run_id=root_run
    )
    _run(sr._ensure_run_row(executor, child_intent))
    _run(sr._append_user_turn(executor, child_intent))
    _run(sr._round_model(executor, child_intent, _INPROC_PINS, 0))
    child_instructions = str(getattr(captured[-1][-1], "instructions", "") or "")
    assert "Run role: DELEGATED CHILD run" in child_instructions
    assert "Run role: ROOT run" not in child_instructions
    assert "Binding rules:" in child_instructions
    assert "app.resolve_service" in child_instructions


def test_secret_route_latch_is_durable_and_idempotent_per_run(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """D6/C6 seam: first secret.* exposure latches the run durably (one
    ``agent.secret_route_latched`` event); later rounds keep the latch
    even with NO current secret exposure; a fresh unexposed run never
    latches; a missing facade row degrades to the round-local computation
    without failing."""
    roots = composition()
    captured: list[list[object]] = []
    allowed = (READ_CAP, SECRET_CAP)
    executor = _inproc_executor(
        agent_url, factory, scenario, roots, allowed=allowed, captured=captured
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    _run(insert_run(run_id, requested_grants=allowed))
    intent = _inproc_intent(run_id, session_id, allowed=allowed)
    _run(sr._ensure_run_row(executor, intent))
    _run(sr._append_user_turn(executor, intent))

    _run(sr._round_model(executor, intent, _INPROC_PINS, 0))
    instructions = str(getattr(captured[-1][-1], "instructions", "") or "")
    assert "Secret routing status: ACTIVE" in instructions
    events = _read_latch_events(agent_url, run_id)
    assert len(events) == 1
    assert events[0]["data"] == {"policy_version": 1}

    # Exposure later disappears (revocation / recovery / replay): the
    # durable row still binds the run — and no duplicate event lands.
    assert _run(sr._resolve_run_secret_route_latch(executor, intent, ())) is True
    assert len(_read_latch_events(agent_url, run_id)) == 1

    # A fresh run with NO secret exposure never latches and never writes.
    fresh_run = str(uuid.uuid4())
    _run(insert_run(fresh_run))
    fresh_intent = _inproc_intent(fresh_run, session_id, allowed=(READ_CAP,))
    assert (
        _run(sr._resolve_run_secret_route_latch(executor, fresh_intent, (READ_CAP,)))
        is False
    )
    assert _read_latch_events(agent_url, fresh_run) == []

    # Missing facade row (mid-run facade-drop seam): degrade honestly to
    # the round-local latch instead of failing the round; nothing persists.
    ghost_intent = _inproc_intent(str(uuid.uuid4()), session_id, allowed=allowed)
    assert (
        _run(sr._resolve_run_secret_route_latch(executor, ghost_intent, allowed))
        is True
    )
    assert _read_latch_events(agent_url, ghost_intent.run_id) == []


def test_every_worker_round_receives_one_policy_block(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """Worker seam: rounds 0 and 1 (after a real tool call) BOTH receive
    exactly one operating-policy instruction block through the REAL
    worker path."""
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(
        scenario,
        [
            _tool_call_entry(name="fixture_read", marker="m1", call_id="c1"),
            {"text": "all done"},
        ],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w-pol")
        _run(insert_run(run_id))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id, session_id, client_request_id=str(uuid.uuid4())
                )
            ),
        )
        _await_terminal(client, run_id, "succeeded", RUN_TIMEOUT, "policy run")
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    lines = _journal_lines(scenario)
    assert lines.count("model_call") == 2, lines
    for mark in (
        "has_instructions:True",
        "instructions_messages:1",
        "system_parts:0",
        "policy_rules_global:True",
        "policy_service_knowledge:True",
        "policy_secret_routing:True",
        "policy_role_root:True",
        "policy_role_child:False",
        "policy_latched:False",
    ):
        assert lines.count(mark) == 2, (mark, lines)


def test_secret_exposure_latches_route_for_the_whole_run(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """Worker seam (D6/C6): a granted secret.get surface latches the run
    in round 0; round 1 (after the real tool call) still receives the
    latched policy, and exactly ONE durable latch event exists."""
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True, SECRET_CAP: True}])
    _script(
        scenario,
        [
            _tool_call_entry(name="secret_get", marker="m1", call_id="c1"),
            {"text": "secret handled via references only"},
        ],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w-latch")
        _run(insert_run(run_id, requested_grants=(READ_CAP, SECRET_CAP)))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id,
                    session_id,
                    allowed=(READ_CAP, SECRET_CAP),
                    client_request_id=str(uuid.uuid4()),
                )
            ),
        )
        _await_terminal(client, run_id, "succeeded", RUN_TIMEOUT, "latch run")
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    lines = _journal_lines(scenario)
    assert lines.count("model_call") == 2, lines
    assert lines.count("has_instructions:True") == 2, lines
    assert lines.count("policy_latched:True") == 2, lines
    assert lines.count("policy_secret_routing:True") == 2, lines
    assert "saw_tool_call:c1" in lines, lines
    assert "saw_tool_return" in lines, lines
    # The latch persisted exactly once for the run.
    assert len(_read_latch_events(agent_url, run_id)) == 1


# ---------------------------------------------------------------------------
# ask_07-runner-safety: rejected secret calls never reach the dispatch
# fence, the executor, or any durable checkpoint. The REAL built-in
# secret descriptors are registered in the worker (generation/reference-
# only agent surface); the backend-only provider is the synthetic
# FixtureExecutor.
# ---------------------------------------------------------------------------

_RAW_CANARY = "RAW-SECRET-CANARY-777"
_CRASH_CANARY = "CRASH-CANARY-888"

#: The generation/reference-only surface under test (slice 04's
#: projected schema lives on THIS capability).
SECRET_STORE_CAP = "secret.store"

_ZERO_LEAK_TABLES = (
    "session_entries",
    "session_provider_artifacts",
    "job_steps",
    "job_events",
    "dbos.operation_outputs",
    "dbos.workflow_status",
)


def _whole_table_blobs(url: str) -> dict[str, str]:
    """Every row of every durable table as JSON text (zero-leak scan)."""
    blobs: dict[str, str] = {}
    with psycopg.connect(url) as conn:
        for table in _ZERO_LEAK_TABLES:
            rows = conn.execute(f"SELECT to_jsonb(t) FROM {table} AS t").fetchall()
            blobs[table] = json.dumps(
                [row[0] for row in rows], ensure_ascii=False, default=str
            )
    return blobs


def _assert_secret_absent(blobs: dict[str, str], secret: str) -> None:
    import base64
    import urllib.parse

    forms = {
        secret,
        json.dumps(secret),
        urllib.parse.quote(secret, safe=""),
        base64.b64encode(secret.encode()).decode(),
        base64.urlsafe_b64encode(secret.encode()).decode(),
    }
    for table, blob in blobs.items():
        for form in forms:
            assert form not in blob, (table, form)


def test_rejected_secret_store_never_dispatches_generation_succeeds(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """Round 0: raw-value secret_store call (leaked canary in the side
    text) is rejected PRE-dispatch — no executor effect, no job_steps
    row — and the group completes. Round 1: the permitted generation
    call dispatches through the REAL schema gate. Round 2 finishes. The
    canary is absent from every durable table including the DBOS
    checkpoints."""
    composition()
    _write_policy_files(
        scenario, grants=[{READ_CAP: True, SECRET_STORE_CAP: True}]
    )
    _script(
        scenario,
        [
            {
                "tool_call": {
                    "name": "secret_store",
                    "args": {"title": "db", "value": _RAW_CANARY},
                    "id": "c1",
                },
                "text": f"storing {_RAW_CANARY} for you",
            },
            {
                "tool_call": {
                    "name": "secret_store",
                    "args": {"title": "db", "generate": True, "length": 24},
                    "id": "c2",
                }
            },
            {"text": "generated via reference; stored"},
        ],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w-saf")
        _run(insert_run(run_id, requested_grants=(READ_CAP, SECRET_STORE_CAP)))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id,
                    session_id,
                    allowed=(READ_CAP, SECRET_STORE_CAP),
                    client_request_id=str(uuid.uuid4()),
                )
            ),
        )
        _await_terminal(client, run_id, "succeeded", RUN_TIMEOUT, "safety run")
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    lines = _journal_lines(scenario)
    # Three model rounds: rejected turn, generation turn, final text.
    assert lines.count("model_call") == 3, lines
    # The rejected call NEVER reached the executor: exactly ONE
    # secret.store begin marker exists — the generation call's.
    assert lines.count("begin:secret.store:?") == 1, lines
    assert lines.count("effect:secret.store:?") == 1, lines
    assert "policy_latched:True" in lines
    assert lines.count("policy_latched:True") == 3
    # No job_steps row for the rejected call (its denial is capture-level).
    with psycopg.connect(agent_url) as conn:
        step_rows = conn.execute(
            "SELECT seq, capability_id, state FROM job_steps WHERE run_id = %s "
            "ORDER BY seq",
            (uuid.UUID(run_id),),
        ).fetchall()
    assert [(row[0], row[1]) for row in step_rows] == [(116, "secret.store")]
    assert step_rows[0][2] == "succeeded"
    # The durable log: denied c1 result, executed c2 result, both groups
    # complete.
    entries = _read_session_entries(agent_url, session_id)
    results = [
        entry["payload"]
        for entry in entries
        if entry["kind"] == "tool_result"
    ]
    assert [payload["call_id"] for payload in results] == ["c1", "c2"]
    assert results[0]["status"] == "denied"
    assert results[0]["error_code"] == "sensitive_call_rejected"
    assert results[1]["status"] == "executed"
    assistant_payloads = [
        entry["payload"]
        for entry in entries
        if entry["kind"] == "assistant_message"
        and entry["payload"].get("tool_calls")
    ]
    assert all(
        payload.get("tool_group_state") == "complete"
        for payload in assistant_payloads
    )
    blobs = _whole_table_blobs(agent_url)
    _assert_secret_absent(blobs, _RAW_CANARY)


def test_crash_replay_keeps_rejected_secret_undispatched(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """Crash window on a MIXED group: the rejected secret call is never
    dispatched before or after the worker restart, the allowed call
    reconciles honestly, the group completes, and the canary is absent
    from every durable table and checkpoint."""
    composition()
    _write_policy_files(
        scenario, grants=[{READ_CAP: True, SECRET_STORE_CAP: True}]
    )
    (scenario / "behavior.json").write_text(
        json.dumps({READ_CAP: "gate"}), encoding="utf-8"
    )
    _script(
        scenario,
        [
            {
                "tool_calls": [
                    {
                        "name": "secret_store",
                        "args": {"title": "db", "value": _CRASH_CANARY},
                        "id": "c1",
                    },
                    {
                        "name": "fixture_read",
                        "args": {"marker": "m1"},
                        "id": "c2",
                    },
                ]
            },
            {"text": "recovered and done"},
        ],
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker1: subprocess.Popen | None = None
    worker2: subprocess.Popen | None = None
    try:
        worker1 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1s")
        _run(insert_run(run_id, requested_grants=(READ_CAP, SECRET_STORE_CAP)))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id,
                    session_id,
                    allowed=(READ_CAP, SECRET_STORE_CAP),
                    reconcile_wait_seconds=120.0,
                    client_request_id=str(uuid.uuid4()),
                )
            ),
        )
        _wait_until(
            lambda: _journal_count(scenario, "begin:fixture.read:m1") == 1,
            RUN_TIMEOUT,
            "gated begin marker",
        )
        # The rejected secret call produced NO executor activity.
        assert _journal_count(scenario, "begin:secret.store:?") == 0
        _kill9(worker1)
        worker1 = None
        assert _journal_count(scenario, "effect:fixture.read:m1") == 0

        worker2 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w2s")
        _wait_until(
            lambda: any(
                entry["kind"] == "tool_result"
                and entry["payload"].get("status") == "unknown_effect"
                for entry in _read_session_entries(agent_url, session_id)
            ),
            RUN_TIMEOUT,
            "the honest unknown_effect session entry",
        )
        with psycopg.connect(agent_url, autocommit=True) as conn:
            conn.execute(
                "UPDATE job_steps SET state = 'succeeded' "
                "WHERE run_id = %s AND seq = 101",
                (uuid.UUID(run_id),),
            )
        _send_wake(run_id, agent_url, {"kind": "resolve"})
        _await_terminal(client, run_id, "succeeded", RUN_TIMEOUT, "crash run")
    finally:
        for worker in (worker1, worker2):
            if worker is not None:
                _stop(worker)
        client.close()

    lines = _journal_lines(scenario)
    assert lines.count("begin:fixture.read:m1") == 1
    assert lines.count("effect:fixture.read:m1") == 0  # never re-executed
    assert "begin:secret.store:?" not in lines
    assert "effect:secret.store:?" not in lines
    assert not any(line.startswith("begin:secret.store") for line in lines), lines
    assert not any(line.startswith("effect:secret.store") for line in lines), lines
    entries = _read_session_entries(agent_url, session_id)
    assistant_payloads = [
        entry["payload"]
        for entry in entries
        if entry["kind"] == "assistant_message"
        and entry["payload"].get("tool_calls")
    ]
    assert assistant_payloads
    assert assistant_payloads[0]["tool_group_state"] == "complete"
    rejected_results = [
        entry["payload"]
        for entry in entries
        if entry["kind"] == "tool_result"
        and entry["payload"].get("call_id") == "c1"
    ]
    assert len(rejected_results) == 1
    assert rejected_results[0]["error_code"] == "sensitive_call_rejected"
    blobs = _whole_table_blobs(agent_url)
    _assert_secret_absent(blobs, _CRASH_CANARY)


# ---------------------------------------------------------------------------
# ST: the generated session-title step (ST-PLAN §4.3; D1/D2/D3/D7/D8)
# ---------------------------------------------------------------------------


def _read_title_row(url: str, session_id: str) -> dict:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT title, created_at, updated_at FROM sessions "
            "WHERE session_id = %s",
            (uuid.UUID(session_id),),
        ).fetchone()
    assert row is not None, "session row missing"
    return row


def test_title_source_strips_service_map_section() -> None:
    """The title prompt source is the user's RAW first message: anything
    from the Service Map marker onward never enters it (ST-PLAN §4.3)."""
    raw = "Fix nginx" + "\n\n" + SERVICE_MAP_SECTION_HEADER + "\nmaterial"
    assert sr._title_source_text(raw) == "Fix nginx"
    assert sr._title_source_text("plain first message") == "plain first message"


def test_generated_title_guards_run_before_any_model_call(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
    monkeypatch,
) -> None:
    """Every guard degrades value-free BEFORE the model runs; only the
    clean path reaches the model once and lands the store write."""
    roots = composition()
    captured: list[list[object]] = []
    executor = _inproc_executor(
        agent_url, factory, scenario, roots, allowed=(READ_CAP,), captured=captured
    )

    # Guard 1: a delegated child NEVER titles (before any DB/model work).
    child_session = _run(_make_session(factory))
    child_intent = _inproc_intent(
        str(uuid.uuid4()),
        child_session,
        allowed=(READ_CAP,),
        parent_run_id=str(uuid.uuid4()),
    )
    assert _run(
        sr._generated_session_title_document(executor, child_intent, _INPROC_PINS)
    ) == {"titled": False, "reason": "delegated_child"}
    assert captured == []

    # Guard 2: a later root run of the same session does not own the
    # FIRST user entry (exact identity, not a counter).
    later_session = _run(_make_session(factory))
    owner_run = str(uuid.uuid4())
    _run(insert_run(owner_run))
    owner_intent = _inproc_intent(owner_run, later_session, allowed=(READ_CAP,))
    _run(sr._ensure_run_row(executor, owner_intent))
    _run(sr._append_user_turn(executor, owner_intent))
    later_intent = _inproc_intent(
        str(uuid.uuid4()), later_session, allowed=(READ_CAP,)
    )
    assert _run(
        sr._generated_session_title_document(executor, later_intent, _INPROC_PINS)
    ) == {"titled": False, "reason": "not_the_first_user_turn"}
    assert captured == []

    # Guard 3: an existing (manual) title wins before the model runs.
    titled_session = str(
        uuid.UUID(int=int(uuid.uuid4().int) % (2**63) | (1 << 62))
    )
    _run(
        acreate_session(
            factory,
            server_id=uuid.UUID(SERVER_ID),
            source=SessionSourceKind.INTERACTIVE,
            session_id=uuid.UUID(titled_session),
            title="Manual rename",
        )
    )
    titled_run = str(uuid.uuid4())
    _run(insert_run(titled_run))
    titled_intent = _inproc_intent(titled_run, titled_session, allowed=(READ_CAP,))
    _run(sr._ensure_run_row(executor, titled_intent))
    _run(sr._append_user_turn(executor, titled_intent))
    manual_before = _read_title_row(agent_url, titled_session)
    assert _run(
        sr._generated_session_title_document(executor, titled_intent, _INPROC_PINS)
    ) == {"titled": False, "reason": "title_already_present"}
    assert captured == []
    manual_after = _read_title_row(agent_url, titled_session)
    assert manual_after["title"] == "Manual rename"
    assert manual_after["updated_at"] == manual_before["updated_at"]

    # Guard 4 (D7): the kill-switch disables generation entirely.
    kill_session = _run(_make_session(factory))
    kill_run = str(uuid.uuid4())
    _run(insert_run(kill_run))
    kill_intent = _inproc_intent(kill_run, kill_session, allowed=(READ_CAP,))
    _run(sr._ensure_run_row(executor, kill_intent))
    _run(sr._append_user_turn(executor, kill_intent))
    monkeypatch.setenv("MAI_SESSION_TITLE", "0")
    assert _run(
        sr._generated_session_title_document(executor, kill_intent, _INPROC_PINS)
    ) == {"titled": False, "reason": "disabled"}
    assert captured == []
    monkeypatch.undo()  # the kill-switch must not leak into the clean path

    # Clean path: the model runs ONCE and the store write lands (D2/D3).
    happy_session = _run(_make_session(factory))
    happy_run = str(uuid.uuid4())
    _run(insert_run(happy_run))
    happy_intent = _inproc_intent(happy_run, happy_session, allowed=(READ_CAP,))
    _run(sr._ensure_run_row(executor, happy_intent))
    _run(sr._append_user_turn(executor, happy_intent))
    happy_before = _read_title_row(agent_url, happy_session)["updated_at"]
    assert _run(
        sr._generated_session_title_document(executor, happy_intent, _INPROC_PINS)
    ) == {"titled": True}
    assert len(captured) == 1
    written = _read_title_row(agent_url, happy_session)
    assert written["title"] == "done"  # the scripted FunctionModel's answer
    # D3 at the runner seam: the title write never moves updated_at.
    assert written["updated_at"] == happy_before


def test_first_run_titles_the_session(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(
        scenario,
        [{"text": "run finished"}],
        title={"text": "**Deploy the stack**"},
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(run_id))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id, session_id, client_request_id=str(uuid.uuid4())
                )
            ),
        )
        _await_terminal(client, run_id, "succeeded", RUN_TIMEOUT, "title run")
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    assert _journal_count(scenario, "title_model_call") == 1
    row = _read_title_row(agent_url, session_id)
    assert row["title"] == "Deploy the stack"  # markdown stripped end-to-end
    assert _run(_run_states(factory, run_id))[-1] == "succeeded"


def test_failing_generator_leaves_title_null_and_run_succeeds(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(
        scenario,
        [{"text": "the answer"}],
        title={"error": "provider exploded"},
    )
    session_id = _run(_make_session(factory))
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(run_id))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id, session_id, client_request_id=str(uuid.uuid4())
                )
            ),
        )
        _await_terminal(client, run_id, "succeeded", RUN_TIMEOUT, "failing title run")
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    # The generator failed, the model WAS called once — and the run still
    # settled succeeded with title NULL (a failed title never fails a run).
    assert _journal_count(scenario, "title_model_call") == 1
    row = _read_title_row(agent_url, session_id)
    assert row["title"] is None
    assert _run(_run_states(factory, run_id))[-1] == "succeeded"


def test_second_turn_never_retitles_the_session(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(
        scenario,
        [{"text": "run one finished"}, {"text": "run two finished"}],
        title={"text": "Deploy the stack"},
    )
    session_id = _run(_make_session(factory))
    run_id_1 = str(uuid.uuid4())
    run_id_2 = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        for tag, run_id in (("run 1", run_id_1), ("run 2", run_id_2)):
            _run(insert_run(run_id))
            client.enqueue_run(
                run_id,
                SESSION_HANDLER_NAME,
                _canonical(
                    _intent_document(
                        run_id, session_id, client_request_id=str(uuid.uuid4())
                    )
                ),
            )
            _await_terminal(client, run_id, "succeeded", RUN_TIMEOUT, tag)
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    # The SECOND turn's title guard fired before the model: exactly ONE
    # title model call across both runs, and the original title stands.
    assert _journal_count(scenario, "title_model_call") == 1
    row = _read_title_row(agent_url, session_id)
    assert row["title"] == "Deploy the stack"
    assert _run(_run_states(factory, run_id_1))[-1] == "succeeded"
    assert _run(_run_states(factory, run_id_2))[-1] == "succeeded"


def test_manual_title_survives_the_auto_title_run(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    composition()
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(
        scenario,
        [{"text": "the answer"}],
        title={"text": "Unwanted generated title"},
    )
    session_id = str(
        uuid.UUID(int=int(uuid.uuid4().int) % (2**63) | (1 << 62))
    )
    _run(
        acreate_session(
            factory,
            server_id=uuid.UUID(SERVER_ID),
            source=SessionSourceKind.INTERACTIVE,
            session_id=uuid.UUID(session_id),
            title="Manual rename",
        )
    )
    run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(run_id))
        client.enqueue_run(
            run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    run_id, session_id, client_request_id=str(uuid.uuid4())
                )
            ),
        )
        _await_terminal(client, run_id, "succeeded", RUN_TIMEOUT, "manual title run")
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    # Guard 3 fired BEFORE the model: the title entry was never consumed.
    assert _journal_count(scenario, "title_model_call") == 0
    row = _read_title_row(agent_url, session_id)
    assert row["title"] == "Manual rename"
    assert _run(_run_states(factory, run_id))[-1] == "succeeded"
