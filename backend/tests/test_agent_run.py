"""Focused tests for durable agent conversation runs (card 06-agent-run).

Acceptance (BRIEF):

1. Conversation/run state persists according to the chosen engine adapter
   (DBOS checkpoints are the canonical conversation persistence) and safe
   event/tool artifacts are emitted (value-free receipts + the shared
   whole-run stream).
2. Model error/cancel/restart are classified with NO replay of already
   uncertain target effects: model failures classify through the reviewed
   provider-failure vocabulary; cancellation is requested ≠ verified; a
   worker kill mid-tool-effect translates to ``outcome_unknown`` +
   ``reconciling`` and the effect is never re-dispatched (fresh worker
   continues from the safe checkpoint after the reconciliation owner
   resolves the step).

Boundary discipline (test policy): the exactly-once, crash-window and
park/resume claims run against a real disposable PostgreSQL fixture
(``MAI_TEST_DATABASE_URL``) with the real Alembic chain, the real engine
adapter and REAL worker subprocesses (kill -9 mid-effect / mid-round +
fresh-worker recovery) — the same discipline as ``test_steps.py``. The
dispatcher composition is the REAL one (real registry, real policy
evaluation, real approval store over the fixture database); the capability
executor is a journaled fixture whose effect markers make "the effect
happened exactly once" observable. The model is either the PRODUCTION
builder (dead-endpoint classification scenarios) or a deterministic
FunctionModel fake (C06 rule 6: fake LLMs prove MAI plumbing only); the
live-provider + real-SSH assembly is AGT-PROVIDER, owned by this card's
producer pair at the P06 gate (recorded limitation). Fixtures carry only
tmp-namespace paths and marker words — never secret values.
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
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents import run as agent_run
from mai.agents.context import MAX_ROLE_CHARS
from mai.agents.model_store import ModelConfigStore
from mai.agents.models import SCHEMA_VERSION as MODEL_SCHEMA_VERSION
from mai.contracts.jobs import parse_job_event
from mai.contracts.server import SCHEMA_VERSION as SERVER_SCHEMA_VERSION
from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.jobs import engine, steps
from mai.jobs.engine import EngineClient
from mai.jobs.lifecycle import RunLifecycle
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import ExecutionContext, Policy, PolicyMode

BACKEND_ROOT = Path(__file__).resolve().parents[1]

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Scratch namespace per runtime-environment ADR D2/D4 (recorded + cleaned
# in the card evidence). Each live scenario gets a fresh subdirectory.
FIXTURE_ROOT = Path(os.environ.get("TMPDIR", "/tmp")) / "mai-fxt-06-agent-run"

READY_TIMEOUT = 90.0
RUN_TIMEOUT = 120.0

# Frozen fixture identities (canonical lowercase UUID strings, C01 rule 1).
SERVER_ID = "6a91c2e4-1b3d-4f5a-8c7e-2d3b4a5f6e70"

READ_CAP = "fixture.read"
PRIV_CAP = "fixture.priv"

HANDLER_MODULE_NAME = "agent_run_scenario_handlers"

SECRET_NAME = "MAI_FXT_TOKEN"
SECRET_VALUE = "fxt-synthetic-token-value-06agent"

HOST_KEY = "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"

PROMPT = "You administer the agent-run fixture host. Keep changes announced.\n"
ROLE = "operator assistant"
TASK = "Run the fixture task and report the marker outcome."


# ---------------------------------------------------------------------------
# persistent test loop (same discipline the production executor applies)
# ---------------------------------------------------------------------------

_TEST_LOOP: asyncio.AbstractEventLoop | None = None
_TEST_LOOP_LOCK = threading.Lock()


def _run(coro: object) -> object:
    """Run one coroutine on the persistent test loop; block for the result."""
    global _TEST_LOOP
    with _TEST_LOOP_LOCK:
        if _TEST_LOOP is None:
            _TEST_LOOP = asyncio.new_event_loop()
            threading.Thread(target=_TEST_LOOP.run_forever, daemon=True).start()
    assert _TEST_LOOP is not None
    return asyncio.run_coroutine_threadsafe(coro, _TEST_LOOP).result()


# ---------------------------------------------------------------------------
# pure layer: payload parsing, seq derivation, history seed
# ---------------------------------------------------------------------------


def _context_doc(
    run_id: str, *, mode: str = "delegated", allowed: tuple[str, ...] = (READ_CAP,)
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
    *,
    allowed: tuple[str, ...] | None = None,
    mode: str = "delegated",
    **overrides: object,
) -> dict:
    document: dict[str, object] = {
        "v": agent_run.RUN_PAYLOAD_VERSION,
        "run_id": run_id,
        "server_id": SERVER_ID,
        "context": (
            _context_doc(run_id, mode=mode)
            if allowed is None
            else _context_doc(run_id, mode=mode, allowed=allowed)
        ),
        "role": ROLE,
        "task": TASK,
    }
    document.update(overrides)
    return document


def _canonical(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class TestParseAgentRunIntent:
    def test_minimal_document_parses_with_defaults(self) -> None:
        run_id = str(uuid.uuid4())
        intent = agent_run.parse_agent_run_intent(_canonical(_intent_document(run_id)))
        assert intent.run_id == run_id
        assert intent.server_id == SERVER_ID
        assert intent.role == ROLE
        assert intent.task == TASK
        assert intent.connection is None and intent.profile is None
        assert intent.max_rounds == agent_run.DEFAULT_MAX_ROUNDS
        assert (
            intent.reconcile_wait_seconds == agent_run.DEFAULT_RECONCILE_WAIT_SECONDS
        )
        assert intent.wait_poll_seconds == agent_run.DEFAULT_WAIT_POLL_SECONDS

    def test_explicit_selection_and_budgets_parse(self) -> None:
        run_id = str(uuid.uuid4())
        intent = agent_run.parse_agent_run_intent(
            _canonical(
                _intent_document(
                    run_id,
                    connection="gateway",
                    profile="fast",
                    rounds=3,
                    reconcile_wait_seconds=2.5,
                    wait_poll_seconds=0.5,
                )
            )
        )
        assert (intent.connection, intent.profile) == ("gateway", "fast")
        assert intent.max_rounds == 3
        assert intent.reconcile_wait_seconds == 2.5
        assert intent.wait_poll_seconds == 0.5

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda doc: {**doc, "v": 2},
            lambda doc: {**doc, "unexpected": 1},
            lambda doc: {k: v for k, v in doc.items() if k != "task"},
            lambda doc: {k: v for k, v in doc.items() if k != "context"},
            lambda doc: {k: v for k, v in doc.items() if k != "role"},
            lambda doc: {**doc, "run_id": "not-a-uuid"},
            lambda doc: {**doc, "server_id": "not-a-uuid"},
            lambda doc: {**doc, "role": "two\nlines"},
            lambda doc: {**doc, "role": "x" * (MAX_ROLE_CHARS + 1)},
            lambda doc: {**doc, "role": "   "},
            lambda doc: {**doc, "task": ""},
            lambda doc: {**doc, "task": "x" * (agent_run.MAX_TASK_CHARS + 1)},
            lambda doc: {**doc, "rounds": 0},
            lambda doc: {**doc, "rounds": agent_run.MAX_MAX_ROUNDS + 1},
            lambda doc: {**doc, "rounds": True},
            lambda doc: {**doc, "reconcile_wait_seconds": 0},
            lambda doc: {
                **doc,
                "reconcile_wait_seconds": agent_run.MAX_RECONCILE_WAIT_SECONDS + 1,
            },
            lambda doc: {**doc, "wait_poll_seconds": -1},
            lambda doc: {**doc, "connection": ""},
            lambda doc: {**doc, "profile": 5},
        ],
    )
    def test_invalid_documents_reject_value_free(self, mutation) -> None:
        run_id = str(uuid.uuid4())
        with pytest.raises(agent_run.AgentRunError) as error:
            agent_run.parse_agent_run_intent(_canonical(mutation(_intent_document(run_id))))
        assert error.value.code is agent_run.AgentRunErrorCode.INVALID_PAYLOAD

    def test_context_identity_mismatch_rejects(self) -> None:
        run_id = str(uuid.uuid4())
        document = _intent_document(run_id)
        document["context"] = _context_doc(str(uuid.uuid4()))  # different run
        with pytest.raises(agent_run.AgentRunError) as error:
            agent_run.parse_agent_run_intent(_canonical(document))
        assert error.value.code is agent_run.AgentRunErrorCode.INVALID_PAYLOAD

    def test_non_string_and_non_json_payloads_reject(self) -> None:
        with pytest.raises(agent_run.AgentRunError):
            agent_run.parse_agent_run_intent("not json {")
        with pytest.raises(agent_run.AgentRunError):
            agent_run.parse_agent_run_intent(_canonical([1, 2, 3]))  # type: ignore[arg-type]


class TestDurableSeams:
    def test_tool_step_seq_is_deterministic_and_collision_free(self) -> None:
        seen: dict[int, tuple[int, int]] = {}
        for round_index in range(agent_run.MAX_MAX_ROUNDS):
            for call_index in range(agent_run.AGENT_TOOL_MAX_PER_ROUND):
                seq = agent_run.tool_step_seq(round_index, call_index)
                assert seq >= 1
                assert seq < 2**31
                assert seq not in seen, "seq collision across (round, index)"
                seen[seq] = (round_index, call_index)
        # Same inputs derive the same row (the exactly-once ledger key).
        assert agent_run.tool_step_seq(3, 5) == agent_run.tool_step_seq(3, 5)

    def test_initial_history_carries_system_then_user(self) -> None:
        from pydantic_ai.messages import ModelMessagesTypeAdapter

        history = ModelMessagesTypeAdapter.validate_json(
            agent_run.initial_history_json("SYSTEM", "TASK").encode("utf-8")
        )
        assert [message.kind for message in history] == ["request", "request"]
        kinds = [
            part.part_kind for message in history for part in message.parts
        ]
        assert kinds == ["system-prompt", "user-prompt"]
        assert history[0].parts[0].content == "SYSTEM"  # type: ignore[attr-defined]
        assert history[1].parts[0].content == "TASK"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# composition wiring (no database dialed)
# ---------------------------------------------------------------------------


def _unused_sessions() -> async_sessionmaker:
    _, sessions = create_app_engine("postgresql://unused:unused@127.0.0.1:1/unused")
    return sessions


def _fixture_descriptor(capability_id: str, effect_class: str) -> object:
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
            "output_schema": {"type": "object"},
            "execution_environments": ["linux_ssh"],
            "effect_class": effect_class,
            "resource_scope": [] if effect_class == "read" else ["target.exec"],
            "required_grants": [],
        },
        source=f"<{capability_id} descriptor>",
    )


def _real_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(_fixture_descriptor(READ_CAP, "read"))
    registry.register(_fixture_descriptor(PRIV_CAP, "privileged"))
    return registry


def _real_dispatcher() -> CapabilityDispatcher:
    return CapabilityDispatcher(
        registry=_real_registry(),
        approvals=ApprovalStore(_unused_sessions()),
        grants=lambda: {READ_CAP: frozenset({uuid.UUID(SERVER_ID)})},
        policy=lambda: Policy(revision=1, denied_capabilities=frozenset()),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver("/tmp/mai-fxt-06-agent-run/absent.env"),
        executor=_refusing_executor(),
    )


def _refusing_executor():
    async def _execute(call) -> ExecutorResult:
        raise AssertionError(f"executor invoked for a denied call: {call}")

    return _execute


def _wired_executor(tmp_path: Path, **overrides: object) -> agent_run.AgentRunExecutor:
    kwargs: dict[str, object] = {
        "sessions": _unused_sessions(),
        "dispatcher": _real_dispatcher(),
        "registry": _real_registry(),
        "model_store": ModelConfigStore(tmp_path / "model-root"),
        "data_root": str(tmp_path / "data-root"),
        "locks": ResourceLockService(
            "postgresql://unused:unused@127.0.0.1:1/unused"
        ),
    }
    kwargs.update(overrides)
    return agent_run.AgentRunExecutor(**kwargs)  # type: ignore[arg-type]


class TestCompositionWiring:
    def test_wrong_component_types_fail_closed(self, tmp_path: Path) -> None:
        with pytest.raises(agent_run.AgentRunError) as error:
            _wired_executor(tmp_path, sessions=object())
        assert error.value.code is agent_run.AgentRunErrorCode.INVALID_WIRING
        with pytest.raises(agent_run.AgentRunError) as error:
            _wired_executor(tmp_path, registry=object())
        assert error.value.code is agent_run.AgentRunErrorCode.INVALID_WIRING
        with pytest.raises(agent_run.AgentRunError) as error:
            _wired_executor(tmp_path, model_store=object())
        assert error.value.code is agent_run.AgentRunErrorCode.INVALID_WIRING
        with pytest.raises(agent_run.AgentRunError) as error:
            _wired_executor(tmp_path, data_root=123)  # type: ignore[arg-type]
        assert error.value.code is agent_run.AgentRunErrorCode.INVALID_WIRING
        with pytest.raises(agent_run.AgentRunError) as error:
            _wired_executor(tmp_path, locks=object())
        assert error.value.code is agent_run.AgentRunErrorCode.INVALID_WIRING
        with pytest.raises(agent_run.AgentRunError) as error:
            _wired_executor(tmp_path, model_builder="not callable")
        assert error.value.code is agent_run.AgentRunErrorCode.INVALID_WIRING

    def test_valid_wiring_composes_and_config_seam_round_trips(
        self, tmp_path: Path
    ) -> None:
        executor = _wired_executor(tmp_path)
        assert isinstance(executor.steps, steps.StepExecutor)
        agent_run.configure_agent_run(executor)
        with pytest.raises(agent_run.AgentRunError) as error:
            agent_run.configure_agent_run(executor)
        assert error.value.code is agent_run.AgentRunErrorCode.ALREADY_CONFIGURED
        agent_run.reset_agent_run()
        agent_run.reset_agent_run()  # idempotent
        with pytest.raises(agent_run.AgentRunError) as error:
            agent_run.configure_agent_run(object())  # type: ignore[arg-type]
        assert error.value.code is agent_run.AgentRunErrorCode.INVALID_WIRING


# ---------------------------------------------------------------------------
# live fixtures: real disposable Postgres + the real engine shape
# ---------------------------------------------------------------------------


def _alembic_config() -> Config:
    return Config(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state (engine schema too)."""
    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS job_resource_locks, job_schedule_fires, "
            "job_schedules, job_events, job_steps, job_runs, approvals, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, alembic_version, notification_alerts, "
            "observation_findings, server_observations, knowledge_doc_chunks, "
            "knowledge_documents, application_projection_journal, "
            "application_projection"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def agent_url() -> Iterator[str]:
    """Migrate the disposable DB, then restore pre-bootstrap state at teardown."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the durable agent-run claims need a "
            "real disposable Postgres fixture"
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
        engine.launch_worker(url)  # creates the engine-owned dbos schema
        engine.shutdown()
        yield url
    finally:
        # Dependent fixtures dispose their engines before session teardown;
        # stop this process's worker before dropping its schema as well.
        engine.shutdown()
        _drop_app_tables(url)


@pytest.fixture
def factory(agent_url: str) -> async_sessionmaker[AsyncSession]:
    """NullPool session factory for in-process facade assertions."""
    eng, sessions = create_app_engine(agent_url)

    async def _clean() -> None:
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

    async def _insert(run_id: str, *, requested_grants: tuple[str, ...] = (READ_CAP,)) -> None:
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
                {"run_id": uuid.UUID(run_id), "server_id": uuid.UUID(SERVER_ID),
                 "requested_grants": json.dumps(requested_grants)},
            )
            await session.commit()

    return _insert


@pytest.fixture
def scenario(tmp_path: Path) -> Path:
    """One fresh scratch scenario directory (journal, gates, handler)."""
    directory = tmp_path / "scenario"
    (directory / "gates").mkdir(parents=True)
    handlers = directory / "handlers"
    handlers.mkdir()
    (handlers / f"{HANDLER_MODULE_NAME}.py").write_text(
        HANDLER_MODULE_SOURCE, encoding="utf-8"
    )
    return directory


def _write_model_root(root: Path, *, base_url: str, credential: bool) -> None:
    store = ModelConfigStore(root)
    connection: dict[str, object] = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "name": "gateway",
        "base_url": base_url,
        "timeout_seconds": 5,
        "capabilities": {"tool_calls": True},
    }
    if credential:
        connection["credential_ref"] = SECRET_NAME
    store.write_connection("gateway", json.dumps(connection) + "\n", expected_revision=0)
    profile = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "name": "fast",
        "connection": "gateway",
        "model": "fixture-model",
        "max_tokens": 256,
    }
    store.write_profile("fast", json.dumps(profile) + "\n", expected_revision=0)
    store.set_selection("gateway", "fast", expected_revision=0)


def _write_data_root(root: Path, *, capability: str = READ_CAP) -> None:
    from mai.profiles.files import ProfileFileStore

    store = ProfileFileStore(root)
    profile_text = (
        f"schema_version: {SERVER_SCHEMA_VERSION}\n"
        f'server_id: "{SERVER_ID}"\n'
        'name: "agent-run-fixture"\n'
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
    prompt_path.write_text(PROMPT, encoding="utf-8")
    os.chmod(prompt_path, 0o600)


@pytest.fixture
def composition(
    scenario: Path, tmp_path: Path
) -> Callable[..., dict[str, Path]]:
    """Write the fixture model/data roots once per scenario."""

    def _compose(
        *, base_url: str = "http://127.0.0.1:9/v1", credential: bool = True,
        capability: str = READ_CAP,
    ) -> dict[str, Path]:
        model_root = scenario / "model-root"
        data_root = scenario / "data-root"
        _write_model_root(model_root, base_url=base_url, credential=credential)
        _write_data_root(data_root, capability=capability)
        (scenario / "fixture.env").write_text(
            f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8"
        )
        os.chmod(scenario / "fixture.env", 0o600)
        return {"model_root": model_root, "data_root": data_root}

    return _compose


# ---------------------------------------------------------------------------
# the worker-side composition (written into the scenario handler dir)
# ---------------------------------------------------------------------------

HANDLER_MODULE_SOURCE = '''
"""Worker-side composition for 06-agent-run: real dispatcher, journaled
effects, deterministic FunctionModel fake (or the production builder for
dead-endpoint classification scenarios)."""

import json
import os
import time
import uuid

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.exceptions import ModelHTTPError
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
    """Journaled capability executor: effect markers make exactly-once
    observable. Behaviors (``MAI_FX_BEHAVIOR`` JSON keyed by
    ``capability:marker`` then ``capability``):

    - ``ok`` (default): effect marker + terminal ``succeeded`` document;
    - ``gate``: ``begin`` marker, then wait for the gate file
      (``<gate dir>/gate-<capability>-<marker>``) BEFORE the effect —
      a kill -9 in that window is the crash-window scenario;
    - ``unknown_status``: terminal-shaped document whose status is
      ``outcome_unknown`` (a host receipt that never went terminal).
    """

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
        return ExecutorResult(
            ok=status == "succeeded", output=json.dumps(document, sort_keys=True)
        )


def _script_model():
    script = json.loads(os.environ["MAI_FX_MODEL_SCRIPT"])
    journal = os.environ["MAI_FX_JOURNAL"]

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
        if inner.get("raise") == "http401":
            raise ModelHTTPError(
                status_code=401, body=b"denied", model_name="fixture-model"
            )
        if "tool_call" in inner:
            call = inner["tool_call"]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=call["name"],
                        args=call.get("args", {"marker": "m1"}),
                        tool_call_id=call.get("id", "c1"),
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content=inner.get("text", "done"))])

    def _model_fn(messages, info):
        _mark("model_call")
        # Journal what the model sees as the latest tool return, so the
        # resume path's honesty is observable from the scenario journal.
        for message in reversed(messages):
            if message.kind == "request":
                for part in message.parts:
                    if getattr(part, "part_kind", "") == "tool-return":
                        try:
                            document = json.loads(part.content)
                            _mark(f"tool_return:{document.get('status')}")
                        except ValueError:
                            _mark("tool_return:unparsable")
                break
        # Replay-safe script position: derived from the DURABLE
        # conversation length, never from process-local call counts — a
        # recovered worker continues the same conversation at the same
        # script entry (the checkpointed history is the round identity).
        index = (len(messages) - 1) // 2
        if index >= len(script):
            raise AssertionError("model script exhausted")
        expected = script[index].get("expect_web_read")
        if expected is not None:
            returns = [
                part for part in messages[-1].parts
                if getattr(part, "part_kind", "") == "tool-return"
                and part.tool_name == "web_read"
            ]
            assert len(returns) == 1, "missing real web_read tool return"
            envelope = json.loads(returns[0].content)
            # Record actual bridge output before validating the scripted reply.
            target = os.environ["MAI_FX_WEB_READ"] + ".result"
            capture = expected.get("capture")
            if capture:
                target = os.environ["MAI_FX_WEB_READ"] + "." + capture + ".result"
            with open(target, "w") as handle:
                json.dump(envelope, handle)
            # The ACTUAL contract: an ordinary read failure reaches the model
            # as a non-success tool result (denied) whose parsed page carries
            # the honest code/status - never an exception, never a success.
            assert envelope["status"] == expected.get("status", "executed"), envelope
            assert envelope["executor_ok"] is expected["executor_ok"], envelope
            if "effect" in expected:
                assert envelope["effect"] == expected["effect"], envelope
            output = envelope["output"]
            if expected.get("output_none"):
                # A pre-dispatch refusal (the bridge gate): nothing ran and
                # no executor output exists.
                assert output is None, envelope
            else:
                assert output is not None, envelope
                assert output["truncated"] is False, envelope
                page = json.loads(output["text"])
                for key, value in expected["page"].items():
                    assert page[key] == value, (key, page)
                if "marker" in expected:
                    assert expected["marker"] in page["markdown"], page
            _mark("web_read_output_verified")
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
    registry.register(_descriptor("fixture.read", "read"))
    registry.register(_descriptor("fixture.priv", "privileged"))
    executor = FixtureExecutor()
    web_fixture = os.environ.get("MAI_FX_WEB_READ")
    if web_fixture:
        import httpx2
        from mai.jobs.worker import _build_web_read_capability
        from mai.research import pages

        with open(web_fixture, encoding="utf-8") as handle:
            response = json.load(handle)
        registry.register(pages.descriptor())

        if "responses" in response:
            # Scripted SEQUENCE of real capability results: every request
            # consumes the next scripted answer, in order. The host set
            # and the per-answer path are asserted, so a drifted request
            # fails the scenario instead of silently consuming the wrong
            # answer. No public site is contacted: the transport is the
            # httpx2 MockTransport and the resolver is a stub.
            hosts = list(response.get("hosts") or [])
            sequence = list(response["responses"])
            cursor = {"index": 0}

            def resolve(host):
                assert host in hosts, host
                return ("93.184.216.34",)

            def transport(request):
                index = cursor["index"]
                assert index < len(sequence), "unscripted web.read request"
                cursor["index"] = index + 1
                entry = sequence[index]
                if "host" in entry:
                    assert request.headers["host"] == entry["host"]
                if "path" in entry:
                    assert request.url.path == entry["path"]
                executor._mark("transport:web.read")
                executor._mark("transport:web.read:" + str(entry.get("path", "?")))
                return httpx2.Response(
                    entry["status"],
                    headers={"Content-Type": entry["content_type"]},
                    content=entry["body"].encode("utf-8"),
                )
        else:
            # The single-answer shape (the W6a fixture).
            def resolve(host):
                assert host == "developer.valvesoftware.com"
                return ("93.184.216.34",)

            def transport(request):
                assert request.headers["host"] == "developer.valvesoftware.com"
                assert request.url.path == "/wiki/Server_queries"
                executor._mark("transport:web.read")
                return httpx2.Response(
                    response["status"],
                    headers={"Content-Type": response["content_type"]},
                    content=response["body"].encode("utf-8"),
                )

        reader = _build_web_read_capability(
            transport=httpx2.MockTransport(transport), resolver=resolve
        )

        async def dispatch_fixture(call):
            if call.descriptor.identifier == pages.CAPABILITY_ID:
                return await reader(call)
            return await executor(call)
    else:
        dispatch_fixture = executor
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
        executor=dispatch_fixture,
    )
    model_store = ModelConfigStore(os.environ["MAI_FX_MODEL_ROOT"])
    scripted = os.environ.get("MAI_FX_MODEL_SCRIPT")
    # ONE fake model per process: the script state advances across the
    # conversation's rounds (a per-round factory would restart it).
    fake_model = _script_model() if scripted else None
    model_builder = (lambda **kwargs: fake_model) if scripted else None
    agent_run.configure_agent_run(
        agent_run.AgentRunExecutor(
            sessions=sessions,
            dispatcher=dispatcher,
            registry=registry,
            model_store=model_store,
            data_root=os.environ["MAI_FX_DATA_ROOT"],
            locks=ResourceLockService(url),
            resolver=EnvResolver(os.environ["MAI_FX_ENV"]),
            model_builder=model_builder,
        )
    )


_build()
'''


def _write_policy_files(scenario: Path, *, grants: list[dict], denied: list[str] | None = None) -> None:
    document = [
        {"capability_id": capability, "server_ids": [SERVER_ID] if present else []}
        for entry in grants
        for capability, present in entry.items()
    ]
    (scenario / "grants.json").write_text(json.dumps(document), encoding="utf-8")
    (scenario / "policy.json").write_text(
        json.dumps({"revision": 1, "denied": denied or []}), encoding="utf-8"
    )


def _journal_path(scenario: Path) -> Path:
    return scenario / "journal.log"


def _journal_lines(scenario: Path) -> list[str]:
    try:
        return _journal_path(scenario).read_text().splitlines()
    except FileNotFoundError:
        return []


def _journal_count(scenario: Path, marker: str) -> int:
    return sum(1 for line in _journal_lines(scenario) if line == marker)


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
    env["MAI_FX_ENV"] = str(scenario / "fixture.env")
    env["MAI_FX_MODEL_ROOT"] = str(scenario / "model-root")
    env["MAI_FX_DATA_ROOT"] = str(scenario / "data-root")
    web_path = scenario / "web-read.json"
    env.pop("MAI_FX_WEB_READ", None)
    if web_path.exists():
        env["MAI_FX_WEB_READ"] = str(web_path)
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
    client.enqueue_run(run_id, agent_run.HANDLER_NAME, _canonical(document))


def _wake(run_id: str, url: str) -> None:
    """Control-plane park wake: any message triggers a durable re-check."""
    from dbos import DBOSClient

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
    state = client.run_state(run_id)
    return None if state is None else state.value


def _failure_reasons(url: str, run_id: str) -> list[str]:
    """The classified failure reasons on the stream (agent.failed events)."""
    return [
        str(event.data["reason"])
        for event in _read_events(url, run_id)
        if event.type == "agent.failed"
    ]


def _script(scenario: Path, entries: list[dict]) -> None:
    (scenario / "model-script.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )


def _behavior(scenario: Path, behavior: dict[str, str]) -> None:
    (scenario / "behavior.json").write_text(json.dumps(behavior), encoding="utf-8")


def _tool_call_entry(
    name: str = "fixture_read",
    marker: str = "m1",
    call_id: str = "c1",
    args: dict | None = None,
) -> dict:
    return {
        "tool_call": {
            "name": name,
            "args": args if args is not None else {"marker": marker},
            "id": call_id,
        }
    }


# ---------------------------------------------------------------------------
# live: durable agent conversations (real engine, real database, real workers)
# ---------------------------------------------------------------------------


class TestDurableAgentRuns:
    def test_real_web_read_incident_budget_succeeds(
        self, agent_url, factory, insert_run, scenario, composition
    ) -> None:
        """Real reader/bridge/DBOS journey; only HTTP and model are controlled."""
        url = "https://developer.valvesoftware.com/wiki/Server_queries"
        marker = "W6A-DURABLE-PAGE-7f329b"
        body = f"<html><body><h1>Server queries</h1><p>{marker}</p></body></html>"
        composition(capability="web.read")
        _write_policy_files(scenario, grants=[{"web.read": True}])
        (scenario / "web-read.json").write_text(
            json.dumps({"status": 200, "content_type": "text/html", "body": body}),
            encoding="utf-8",
        )
        _script(scenario, [
            _tool_call_entry(name="web_read", args={
                "url": url, "max_bytes": 524288, "max_markdown_chars": 10000,
            }),
            {"expect_web_read": {
                "executor_ok": True,
                "page": {"status": "ok", "http_status": 200},
                "marker": marker,
            }, "text": "Verified page marker; all done"},
        ])
        run_id = str(uuid.uuid4())
        document = _intent_document(
            run_id, allowed=("web.read",), connection="gateway", profile="fast",
            wait_poll_seconds=0.5,
        )
        client = EngineClient(agent_url)
        worker = None
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "web")
            _run(insert_run(run_id, requested_grants=("web.read",)))
            client.enqueue_run(run_id, agent_run.HANDLER_NAME, _canonical(document))
            _wait_until(
                lambda: _terminal_engine_state(client, run_id) in ("succeeded", "failed"),
                RUN_TIMEOUT, "web.read terminal run",
            )
            assert _terminal_engine_state(client, run_id) == "succeeded", _journal_lines(scenario)
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        assert _journal_lines(scenario) == [
            "model_call", "transport:web.read", "model_call",
            "tool_return:executed", "web_read_output_verified",
        ]
        envelope = json.loads((scenario / "web-read.json.result").read_text())
        assert envelope["executor_ok"] is True
        assert envelope["approval_id"] is None
        assert envelope["approval_spent"] is False
        output = envelope["output"]
        assert output["truncated"] is False and output["omitted_bytes"] == 0
        assert len(output["text"].encode("utf-8")) < 65536
        page = json.loads(output["text"])
        assert page["status"] == "ok" and page["http_status"] == 200
        assert marker in page["markdown"]
        assert 0 < len(page["markdown"]) <= 10000
        assert page["bytes_read"] == len(body.encode("utf-8"))
        assert page["truncated"] is False
        assert page["url"] == page["final_url"] == page["citation"] == url
        assert page["trust"] == "untrusted_external"

        rows = _read_step_rows(agent_url, run_id)
        assert len(rows) == 1
        assert rows[0]["seq"] == 1 and rows[0]["capability_id"] == "web.read"
        assert rows[0]["state"] == "succeeded"
        receipt = rows[0]["receipt"]
        assert receipt["allowed"] is True and receipt["effect"] == "executed"
        assert receipt["status"] == "ok"
        assert receipt["response"] == page
        assert receipt["params"] == {
            "url": url, "max_bytes": 524288, "max_markdown_chars": 10000,
        }
        events = _read_events(agent_url, run_id)
        types = [event.type for event in events]
        assert "agent.completed" in types
        assert "agent.awaiting_reconciliation" not in types
        assert "agent.failed" not in types
        assert _run(_run_states(factory, run_id)) == ("queued", "running", "succeeded")

    def test_real_web_read_recovers_after_ordinary_read_failures(
        self, agent_url, factory, insert_run, scenario, composition
    ) -> None:
        """W6b: ordinary read failures are honest answers, not stalls.

        A scripted sequence of REAL capability results — a size refusal, a
        200 plain-text read, a 403 and a 404 (both carrying their numeric
        ``http_status``), a valid 512 KiB-budget read and a successful
        alternative — is delivered to the model through the real worker,
        dispatcher, bridge and durable conversation. The model observes each
        actual tool response; the run finishes successfully, every ordinary
        failure is recorded as a KNOWN outcome (never ``outcome_unknown``),
        the run never parks in reconciliation, and the successful answer
        keeps its valid JSON, citation and trust label intact.

        Only the HTTP boundary and the model are controlled: the transport is
        the httpx2 MockTransport and the resolver is a stub, so no public site
        is contacted.
        """
        host = "wiki.example.com"
        base = f"https://{host}"
        size_marker = "W6B-SIZE-REFUSAL-51c8"
        plain_marker = "W6B-PLAINTEXT-9ae2"
        big_marker = "W6B-BIG-512K-3f77"
        alt_marker = "W6B-ALTERNATIVE-7b40"
        big_body = f"{big_marker}\n" + "y" * 400000
        big_bytes = len(big_body.encode("utf-8"))
        (scenario / "web-read.json").write_text(
            json.dumps({
                "hosts": [host],
                "responses": [
                    {   # 1. size refusal: the body is over the byte budget
                        "host": host,
                        "path": "/too-big",
                        "status": 200,
                        "content_type": "text/plain",
                        "body": "x" * 100000,
                    },
                    {   # 2. a 200 plain-text read (returned verbatim)
                        "host": host,
                        "path": "/plain",
                        "status": 200,
                        "content_type": "text/plain",
                        "body": f"plain text body {plain_marker}\n",
                    },
                    {   # 3. a 403 carrying its numeric http_status
                        "host": host,
                        "path": "/forbidden",
                        "status": 403,
                        "content_type": "text/html",
                        "body": "<html><body>forbidden</body></html>",
                    },
                    {   # 4. a 404 carrying its numeric http_status
                        "host": host,
                        "path": "/missing",
                        "status": 404,
                        "content_type": "text/html",
                        "body": "<html><body>missing</body></html>",
                    },
                    {   # 5. a valid 512 KiB-budget read (a big plain body)
                        "host": host,
                        "path": "/big",
                        "status": 200,
                        "content_type": "text/plain",
                        "body": big_body,
                    },
                    {   # 6. the successful alternative the model switches to
                        "host": host,
                        "path": "/alternative",
                        "status": 200,
                        "content_type": "text/plain",
                        "body": f"alternative source {alt_marker}\n",
                    },
                ],
            }),
            encoding="utf-8",
        )
        composition(capability="web.read")
        _write_policy_files(scenario, grants=[{"web.read": True}])
        _script(scenario, [
            # round 0: the size refusal (max_bytes under the declared body)
            _tool_call_entry(
                name="web_read", call_id="c1",
                args={
                    "url": f"{base}/too-big", "max_bytes": 32768,
                    "max_markdown_chars": 10000,
                },
            ),
            # round 1: observes read 1 (refused), then reads the plain page
            {
                "expect_web_read": {
                    "capture": "r1",
                    "status": "denied",
                    "executor_ok": False,
                    "effect": "executed",
                    "page": {
                        "status": "refused",
                        "code": "response_too_large",
                        "http_status": None,
                    },
                },
                **_tool_call_entry(
                    name="web_read", call_id="c2",
                    args={
                        "url": f"{base}/plain", "max_bytes": 524288,
                        "max_markdown_chars": 10000,
                    },
                ),
            },
            # round 2: observes read 2 (ok), then hits the 403
            {
                "expect_web_read": {
                    "capture": "r2",
                    "status": "executed",
                    "executor_ok": True,
                    "effect": "executed",
                    "page": {"status": "ok", "http_status": 200},
                    "marker": plain_marker,
                },
                **_tool_call_entry(
                    name="web_read", call_id="c3",
                    args={
                        "url": f"{base}/forbidden", "max_bytes": 524288,
                        "max_markdown_chars": 10000,
                    },
                ),
            },
            # round 3: observes read 3 (403), then hits the 404
            {
                "expect_web_read": {
                    "capture": "r3",
                    "status": "denied",
                    "executor_ok": False,
                    "effect": "executed",
                    "page": {
                        "status": "unavailable",
                        "code": "fetch_error",
                        "http_status": 403,
                    },
                },
                **_tool_call_entry(
                    name="web_read", call_id="c4",
                    args={
                        "url": f"{base}/missing", "max_bytes": 524288,
                        "max_markdown_chars": 10000,
                    },
                ),
            },
            # round 4: observes read 4 (404), then makes the valid 512 KiB request
            {
                "expect_web_read": {
                    "capture": "r4",
                    "status": "denied",
                    "executor_ok": False,
                    "effect": "executed",
                    "page": {
                        "status": "unavailable",
                        "code": "fetch_error",
                        "http_status": 404,
                    },
                },
                **_tool_call_entry(
                    name="web_read", call_id="c5",
                    args={
                        "url": f"{base}/big", "max_bytes": 524288,
                        "max_markdown_chars": 10000,
                    },
                ),
            },
            # round 5: observes read 5 (the valid 512 KiB request), then the
            # successful alternative, and finally reports honestly.
            {
                "expect_web_read": {
                    "capture": "r5",
                    "status": "executed",
                    "executor_ok": True,
                    "effect": "executed",
                    "page": {
                        "status": "ok", "http_status": 200,
                        "bytes_read": big_bytes, "truncated": True,
                    },
                    "marker": big_marker,
                },
                **_tool_call_entry(
                    name="web_read", call_id="c6",
                    args={
                        "url": f"{base}/alternative", "max_bytes": 524288,
                        "max_markdown_chars": 10000,
                    },
                ),
            },
            # round 6: observes read 6 (the successful alternative) and finishes
            {
                "expect_web_read": {
                    "capture": "r6",
                    "status": "executed",
                    "executor_ok": True,
                    "effect": "executed",
                    "page": {"status": "ok", "http_status": 200},
                    "marker": alt_marker,
                },
                "text": (
                    "The refused, forbidden and missing pages were reported "
                    "honestly; the alternative source answered and is cited."
                ),
            },
        ])
        run_id = str(uuid.uuid4())
        document = _intent_document(
            run_id, allowed=("web.read",), connection="gateway", profile="fast",
            wait_poll_seconds=0.5,
            # A short reconciliation deadline: if an ordinary failure ever
            # parked the run as an unknown effect, the park would expire
            # quickly and the run would fail HONESTLY instead of hanging.
            reconcile_wait_seconds=5.0,
        )
        client = EngineClient(agent_url)
        worker = None
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "web")
            _run(insert_run(run_id, requested_grants=("web.read",)))
            client.enqueue_run(run_id, agent_run.HANDLER_NAME, _canonical(document))
            _wait_until(
                lambda: _terminal_engine_state(client, run_id) in ("succeeded", "failed"),
                RUN_TIMEOUT, "web.read recovery terminal run",
            )
            assert _terminal_engine_state(client, run_id) == "succeeded", _journal_lines(scenario)
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        # -- the model observed six real tool responses, in order ----------
        lines = _journal_lines(scenario)
        assert lines.count("model_call") == 7  # six tool rounds + the final one
        assert lines.count("transport:web.read") == 6
        for path in (
            "/too-big", "/plain", "/forbidden", "/missing", "/big", "/alternative",
        ):
            assert lines.count(f"transport:web.read:{path}") == 1, path
        assert lines.count("web_read_output_verified") == 6
        # The tool results the model received: the ordinary failures are
        # honest non-success answers, the successes are executions.
        assert [
            line for line in lines if line.startswith("tool_return:")
        ] == [
            "tool_return:denied",    # size refusal
            "tool_return:executed",  # 200 plain-text read
            "tool_return:denied",    # 403
            "tool_return:denied",    # 404
            "tool_return:executed",  # valid 512 KiB request
            "tool_return:executed",  # successful alternative
        ], lines

        # -- each recorded envelope is the honest answer -------------------
        r1 = json.loads((scenario / "web-read.json.r1.result").read_text())
        assert r1["status"] == "denied" and r1["executor_ok"] is False
        page1 = json.loads(r1["output"]["text"])
        assert page1["status"] == "refused"
        assert page1["code"] == "response_too_large"
        assert page1["http_status"] is None
        assert page1["trust"] == "untrusted_external"
        assert size_marker not in page1["markdown"]

        r2 = json.loads((scenario / "web-read.json.r2.result").read_text())
        assert r2["status"] == "executed" and r2["executor_ok"] is True
        page2 = json.loads(r2["output"]["text"])
        assert page2["status"] == "ok" and page2["http_status"] == 200
        assert plain_marker in page2["markdown"]
        assert page2["url"] == page2["final_url"] == page2["citation"] == (
            f"{base}/plain"
        )
        assert page2["trust"] == "untrusted_external"

        r3 = json.loads((scenario / "web-read.json.r3.result").read_text())
        assert r3["status"] == "denied" and r3["executor_ok"] is False
        page3 = json.loads(r3["output"]["text"])
        assert page3["status"] == "unavailable"
        assert page3["code"] == "fetch_error"
        assert page3["http_status"] == 403

        r4 = json.loads((scenario / "web-read.json.r4.result").read_text())
        assert r4["status"] == "denied" and r4["executor_ok"] is False
        page4 = json.loads(r4["output"]["text"])
        assert page4["status"] == "unavailable"
        assert page4["code"] == "fetch_error"
        assert page4["http_status"] == 404

        r5 = json.loads((scenario / "web-read.json.r5.result").read_text())
        assert r5["status"] == "executed" and r5["executor_ok"] is True
        page5 = json.loads(r5["output"]["text"])
        assert page5["status"] == "ok" and page5["http_status"] == 200
        assert big_marker in page5["markdown"]
        assert page5["truncated"] is True  # the Markdown budget cut it, honestly
        assert page5["trust"] == "untrusted_external"

        r6 = json.loads((scenario / "web-read.json.r6.result").read_text())
        assert r6["status"] == "executed" and r6["executor_ok"] is True
        page6 = json.loads(r6["output"]["text"])
        assert page6["status"] == "ok" and page6["http_status"] == 200
        assert alt_marker in page6["markdown"]
        assert page6["citation"] == f"{base}/alternative"
        assert page6["trust"] == "untrusted_external"

        # -- the durable rows: every failure is a KNOWN outcome ------------
        rows = _read_step_rows(agent_url, run_id)
        # Round-major seq derivation (one tool call per round).
        assert [row["seq"] for row in rows] == [
            1, 9, 17, 25, 33, 41,
        ]
        assert [row["state"] for row in rows] == [
            "failed", "succeeded", "failed", "failed", "succeeded", "succeeded",
        ]
        assert all(row["state"] != "outcome_unknown" for row in rows)
        assert [row["receipt"]["reason"] for row in rows] == [
            "response_too_large", "allowed", "fetch_error", "fetch_error",
            "allowed", "allowed",
        ]
        assert [row["receipt"]["executor_ok"] for row in rows] == [
            False, True, False, False, True, True,
        ]
        # The recorded payload of every attempt (the explicit request).
        assert [row["receipt"]["params"]["url"] for row in rows] == [
            f"{base}/too-big", f"{base}/plain", f"{base}/forbidden",
            f"{base}/missing", f"{base}/big", f"{base}/alternative",
        ]
        assert rows[4]["receipt"]["params"]["max_bytes"] == 524288
        assert rows[0]["receipt"]["params"]["max_bytes"] == 32768
        # The refusals carried their honest answer into the durable receipt.
        assert rows[0]["receipt"]["response"]["code"] == "response_too_large"
        assert rows[2]["receipt"]["response"]["http_status"] == 403
        assert rows[3]["receipt"]["response"]["http_status"] == 404

        # -- the run finished cleanly and never parked --------------------
        events = _read_events(agent_url, run_id)
        types = [event.type for event in events]
        assert "agent.completed" in types
        assert "agent.awaiting_reconciliation" not in types
        assert "agent.failed" not in types
        assert _run(_run_states(factory, run_id)) == ("queued", "running", "succeeded")

    def test_conversation_executes_tool_once_and_succeeds(
        self, agent_url, factory, insert_run, scenario, composition
    ) -> None:
        """AC1: the conversation persists, tool artifacts land, run succeeds."""
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _script(scenario, [_tool_call_entry(), {"text": "all done"}])
        run_id = str(uuid.uuid4())
        document = _intent_document(
            run_id, connection="gateway", profile="fast", wait_poll_seconds=0.5
        )
        client = EngineClient(agent_url)
        worker = None
        engine_final = None
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
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

        # The effect happened EXACTLY once; the model saw the executed result.
        assert _journal_lines(scenario) == [
            "model_call",
            "begin:fixture.read:m1",
            "effect:fixture.read:m1",
            "model_call",
            "tool_return:executed",
        ]

        # The durable tool row + value-free receipt.
        rows = _read_step_rows(agent_url, run_id)
        assert [row["seq"] for row in rows] == [1]
        row = rows[0]
        assert row["state"] == "succeeded"
        receipt = row["receipt"]
        assert receipt["op_id"]
        assert receipt["allowed"] is True
        assert receipt["effect"] == "executed"
        assert receipt["status"] == "succeeded"
        assert "stdout" not in receipt and "command" not in receipt

        # Safe events on the shared whole-run stream (value-free).
        events = _read_events(agent_url, run_id)
        assert [event.seq for event in events] == list(range(1, len(events) + 1))
        types = [event.type for event in events]
        assert types == [
            "run.transition",  # queued
            "run.transition",  # running
            "agent.started",
            "agent.context_assembled",
            "agent.model_round",  # round 0: tool_calls
            "step.started",
            "step.completed",
            "agent.model_round",  # round 1: final
            "run.transition",  # succeeded
            "agent.completed",  # the safe conversation summary
        ]
        started = next(event for event in events if event.type == "agent.started")
        assert started.data["model"] == "fixture-model"
        assert started.data["connection"] == "gateway"
        context_event = next(
            event for event in events if event.type == "agent.context_assembled"
        )
        assert context_event.data["budget"]["used_chars"] > 0
        assert "system_prompt" not in context_event.data
        assert PROMPT not in json.dumps(context_event.data)
        assert TASK not in json.dumps(context_event.data)
        rounds = [event for event in events if event.type == "agent.model_round"]
        assert [event.data["status"] for event in rounds] == ["tool_calls", "final"]

        # Legal run states through the frozen diagram.
        assert _run(_run_states(factory, run_id)) == ("queued", "running", "succeeded")

    def test_schema_mismatch_denied_without_effect_and_run_continues(
        self, agent_url, factory, insert_run, scenario, composition
    ) -> None:
        """Negative control: a schema-violating call never dispatches.

        PydanticAI's schema-declared tools skip argument validation, so
        the violation arrives as a deferred call; the bridge gate denies
        it BEFORE the dispatcher and the model receives the honest
        denial. The denial row is durable; the run continues.
        """
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _script(
            scenario,
            [
                _tool_call_entry(args={"marker": 5}),
                {"text": "understood, moving on"},
            ],
        )
        run_id = str(uuid.uuid4())
        document = _intent_document(run_id, connection="gateway", profile="fast")
        client = EngineClient(agent_url)
        worker = None
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "run success",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        # NO effect was attempted; the denial row is durable and the model
        # saw the honest denial on its continuation round.
        assert _journal_lines(scenario) == [
            "model_call",
            "model_call",
            "tool_return:denied",
        ]
        row = _read_step_row(agent_url, run_id, 1)
        assert row is not None and row["state"] == "failed"
        assert row["receipt"]["reason"] == "bridge_payload_schema_mismatch"
        assert row["receipt"]["effect"] == "not_attempted"
        assert row["receipt"]["capability_id"] == READ_CAP
        assert row["receipt"]["payload_hash"]
        assert _run(_run_states(factory, run_id)) == ("queued", "running", "succeeded")

    def test_privileged_call_records_approval_binding_and_run_continues(
        self, agent_url, factory, insert_run, scenario, composition
    ) -> None:
        """The approval_required answer carries the durable binding; nothing runs.

        ASK mode is the prompting mode (C03 rule 5): a granted privileged
        capability still needs an approval decision, so the call is
        refused before any effect and the durable binding the resume
        surface consumes is recorded.
        """
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}, {PRIV_CAP: True}])
        _script(
            scenario,
            [
                _tool_call_entry(name="fixture_priv"),
                {"text": "awaiting approval then"},
            ],
        )
        run_id = str(uuid.uuid4())
        document = _intent_document(
            run_id,
            allowed=(READ_CAP, PRIV_CAP),
            mode="ask",
            connection="gateway",
            profile="fast",
        )
        client = EngineClient(agent_url)
        worker = None
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "run success",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        assert _journal_lines(scenario) == [
            "model_call",
            "model_call",
            "tool_return:denied",
        ]
        row = _read_step_row(agent_url, run_id, 1)
        assert row is not None and row["state"] == "failed"
        receipt = row["receipt"]
        assert receipt["reason"] == "approval_required_but_absent"
        assert receipt["effect"] == "not_attempted"
        # The durable binding the resume surface consumes (C03 rule 4).
        assert receipt["payload_hash"]
        assert receipt["policy_revision"] == 1
        assert _run(_run_states(factory, run_id)) == ("queued", "running", "succeeded")

    def test_privileged_call_executes_once_in_delegated_mode(
        self, agent_url, factory, insert_run, scenario, composition
    ) -> None:
        """DELEGATED mode runs a granted privileged capability without prompting.

        The counterpart of the ASK-mode refusal above: the same granted
        privileged capability, inside the same grants, executes EXACTLY
        once when the context's mode is delegated (C03 rule 5 — the mode
        never widens a grant, it only decides whether a decision is
        prompted). The fixture capability is the controlled one; no real
        privileged operation runs.
        """
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}, {PRIV_CAP: True}])
        _script(
            scenario,
            [
                _tool_call_entry(name="fixture_priv"),
                {"text": "delegated run finished"},
            ],
        )
        run_id = str(uuid.uuid4())
        document = _intent_document(
            run_id,
            allowed=(READ_CAP, PRIV_CAP),
            mode="delegated",
            connection="gateway",
            profile="fast",
        )
        client = EngineClient(agent_url)
        worker = None
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "run success",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        # The effect happened EXACTLY once and the model saw the executed
        # result on its continuation round.
        assert _journal_lines(scenario) == [
            "model_call",
            "begin:fixture.priv:m1",
            "effect:fixture.priv:m1",
            "model_call",
            "tool_return:executed",
        ]

        row = _read_step_row(agent_url, run_id, 1)
        assert row is not None and row["state"] == "succeeded"
        receipt = row["receipt"]
        assert receipt["allowed"] is True
        assert receipt["reason"] == "allowed"
        assert receipt["effect"] == "executed"
        assert receipt["executor_ok"] is True
        assert receipt["policy_revision"] == 1
        assert receipt["payload_hash"]
        assert receipt["capability_id"] == PRIV_CAP
        # No approval was needed in delegated mode, so none was spent and
        # none was created.
        assert receipt["approval_spent"] is False
        assert receipt["approval_id"] is None
        # The privilege advisory lives on the policy decision; the durable
        # receipt is value-free and carries no advisory claim of its own.
        assert "advisories" not in receipt
        assert _run(_run_states(factory, run_id)) == ("queued", "running", "succeeded")

    def test_production_model_errors_classify_and_fail_without_effect(
        self, agent_url, factory, insert_run, scenario, composition
    ) -> None:
        """AC2: the PRODUCTION builder path classifies provider failures.

        Scenario A: the credential reference resolves (fixture .env), the
        real OpenAI-compatible client is built, the endpoint refuses the
        connection → ``model_reachability``. Scenario B (fresh worker:
        the .env source parses at composition) the .env lacks the
        referenced key → the call-time resolution fails closed
        (``model_credential_missing_key``) BEFORE any request. Neither
        attempts an effect.
        """
        composition(base_url="http://127.0.0.1:1/v1", credential=True)
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        run_id_a = str(uuid.uuid4())
        run_id_b = str(uuid.uuid4())
        client = EngineClient(agent_url)
        worker1 = None
        worker2 = None
        try:
            worker1 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(
                _enqueue_run(
                    client,
                    insert_run,
                    run_id_a,
                    _intent_document(run_id_a, connection="gateway", profile="fast"),
                )
            )
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id_a) or "") == "failed",
                RUN_TIMEOUT,
                "reachability failure",
            )
            assert _run(_current_state(factory, run_id_a)) == "failed"
            assert _failure_reasons(agent_url, run_id_a) == ["model_reachability"]
            assert _journal_lines(scenario) == []
            assert _read_step_rows(agent_url, run_id_a) == []
        finally:
            if worker1 is not None:
                _stop(worker1)

        try:
            # Scenario B: the same connection names a credential the .env
            # does not carry — classified before any request. A FRESH
            # worker re-composes the .env source (it parses at
            # construction, C03 rule 6).
            (scenario / "fixture.env").write_text(
                "MAI_FXT_UNRELATED=1\n", encoding="utf-8"
            )
            worker2 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w2")
            _run(
                _enqueue_run(
                    client,
                    insert_run,
                    run_id_b,
                    _intent_document(run_id_b, connection="gateway", profile="fast"),
                )
            )
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id_b) or "") == "failed",
                RUN_TIMEOUT,
                "credential failure",
            )
            assert _run(_current_state(factory, run_id_b)) == "failed"
        finally:
            if worker2 is not None:
                _stop(worker2)
            client.close()
        assert _failure_reasons(agent_url, run_id_b) == ["model_credential_missing_key"]
        assert _read_step_rows(agent_url, run_id_b) == []

    def test_fake_model_http_error_classifies_as_auth(
        self, agent_url, factory, insert_run, scenario, composition
    ) -> None:
        """AC2: a provider 401 classifies to ``model_auth`` (no effect)."""
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _script(scenario, [{"raise": "http401"}])
        run_id = str(uuid.uuid4())
        document = _intent_document(run_id, connection="gateway", profile="fast")
        client = EngineClient(agent_url)
        worker = None
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "failed",
                RUN_TIMEOUT,
                "auth failure",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()
        assert _journal_lines(scenario) == ["model_call"]
        assert _read_step_rows(agent_url, run_id) == []
        assert _failure_reasons(agent_url, run_id) == ["model_auth"]
        assert _run(_run_states(factory, run_id)) == ("queued", "running", "failed")

    def test_cancel_mid_round_records_cancelled_without_effect(
        self, agent_url, factory, insert_run, scenario, composition
    ) -> None:
        """AC2: requested ≠ verified; the cancellation owner translates.

        The facade ``cancelled`` translation is the reviewed recovery
        owner's (``RecoveryService.acancel_run``: request recorded once,
        engine cancel, engine truth applied to the facade diagram). The
        in-flight checkpointed round completes; NO tool step ever runs.
        """
        from mai.jobs.recovery import RecoveryService

        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _script(
            scenario,
            [
                {"gate": "gate-model-round-0", "then": _tool_call_entry()},
                {"text": "should never be reached"},
            ],
        )
        run_id = str(uuid.uuid4())
        document = _intent_document(run_id, connection="gateway", profile="fast")
        client = EngineClient(agent_url)
        recovery = RecoveryService(agent_url, factory)
        worker = None
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: _journal_count(scenario, "model_call") == 1,
                RUN_TIMEOUT,
                "the gated model round",
            )
            # The reviewed cancel owner: request once, engine cancel, then
            # apply the engine truth to the facade (guard rules 3–4).
            _run(recovery.acancel_run(run_id, outcome_timeout=60.0))
            # Release the gate so the worker's in-flight round settles and
            # the graceful shutdown stays clean (the cancelled workflow is
            # never resumed; the step result is simply never claimed).
            (scenario / "gates" / "gate-model-round-0").write_text(
                "go", encoding="utf-8"
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()
            recovery.close()

        assert _run(_run_states(factory, run_id)) == ("queued", "running", "cancelled")
        assert (_terminal_engine_state(client, run_id) or "") == "cancelled"
        # The round settled (checkpointed), but NO tool step ever ran.
        assert _journal_lines(scenario) == ["model_call"]
        assert _read_step_rows(agent_url, run_id) == []

    def test_kill_mid_tool_effect_parks_reconciles_and_never_replays(
        self, agent_url, factory, insert_run, scenario, composition
    ) -> None:
        """AC2 core: kill -9 across the model/tool boundary.

        The crashed attempt leaves ``running`` without a receipt; the
        fresh worker translates it to ``outcome_unknown`` + parks the run
        ``reconciling`` WITHOUT re-dispatching; after the reconciliation
        owner resolves the step, the conversation resumes from the safe
        checkpoint and finishes — the effect happened exactly once.
        """
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _behavior(scenario, {READ_CAP: "gate"})
        _script(scenario, [_tool_call_entry(), {"text": "resumed cleanly"}])
        run_id = str(uuid.uuid4())
        document = _intent_document(
            run_id,
            connection="gateway",
            profile="fast",
            reconcile_wait_seconds=90.0,
            wait_poll_seconds=0.5,
        )
        client = EngineClient(agent_url)
        worker1 = None
        worker2 = None
        try:
            worker1 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: _journal_count(scenario, "begin:fixture.read:m1") == 1,
                RUN_TIMEOUT,
                "the gated attempt to start",
            )
            _kill9(worker1)

            worker2 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w2")
            # The crash window translates; the effect is NOT re-dispatched.
            _wait_until(
                lambda: _run(_current_state(factory, run_id)) == "reconciling",
                RUN_TIMEOUT,
                "the reconciling park",
            )
            row = _read_step_row(agent_url, run_id, 1)
            assert row is not None and row["state"] == "outcome_unknown"
            assert row["receipt"]["reason"] == "crash_recovery"
            assert _journal_count(scenario, "effect:fixture.read:m1") == 0

            # Reconciliation resolves; the wake triggers the durable
            # re-check; the conversation continues from the checkpoint.
            _resolve_step(agent_url, run_id, 1, "succeeded")
            _wake(run_id, agent_url)
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "run success after reconciliation",
            )
        finally:
            for worker in (worker1, worker2):
                if worker is not None and worker.poll() is None:
                    _stop(worker)
            client.close()

        # Exactly one effect; exactly two model calls (the settled round
        # replayed from its checkpoint — never re-asked).
        assert _journal_lines(scenario) == [
            "model_call",
            "begin:fixture.read:m1",
            "model_call",
            "tool_return:executed",
        ]
        row = _read_step_row(agent_url, run_id, 1)
        assert row is not None and row["state"] == "succeeded"
        assert row["receipt"]["resolution"] == "terminal"
        events = _read_events(agent_url, run_id)
        assert any(event.type == "agent.awaiting_reconciliation" for event in events)
        reconciling = [
            event for event in events if event.type == "agent.awaiting_reconciliation"
        ]
        assert len(reconciling) == 1  # the wait deadline persisted once
        assert _run(_run_states(factory, run_id)) == (
            "queued",
            "running",
            "reconciling",
            "running",
            "succeeded",
        )

    def test_kill_mid_model_round_reexecutes_round_effect_stays_once(
        self, agent_url, factory, insert_run, scenario, composition
    ) -> None:
        """AC1/AC2: a crash INSIDE the model round re-asks the model (safe,
        no effect yet) and the following tool call still executes once."""
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _script(
            scenario,
            [
                {"gate": "gate-model-round-0", "then": _tool_call_entry()},
                {"text": "round replayed fine"},
            ],
        )
        run_id = str(uuid.uuid4())
        document = _intent_document(run_id, connection="gateway", profile="fast")
        client = EngineClient(agent_url)
        worker1 = None
        worker2 = None
        try:
            worker1 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: _journal_count(scenario, "model_call") == 1,
                RUN_TIMEOUT,
                "the gated model round",
            )
            _kill9(worker1)  # the round step had NO checkpoint yet
            # Release the gate so the RECOVERED round (fresh worker)
            # proceeds: the model round re-executes from scratch — the
            # safe, effect-free replay this scenario proves.
            (scenario / "gates" / "gate-model-round-0").write_text(
                "go", encoding="utf-8"
            )

            worker2 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w2")
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "run success after round replay",
            )
        finally:
            for worker in (worker1, worker2):
                if worker is not None and worker.poll() is None:
                    _stop(worker)
            client.close()

        # The model was asked THREE times (round 0 re-executed after the
        # crash, then the continuation); the effect ran exactly once.
        assert _journal_count(scenario, "model_call") == 3
        assert _journal_lines(scenario).count("begin:fixture.read:m1") == 1
        assert _journal_lines(scenario).count("effect:fixture.read:m1") == 1
        row = _read_step_row(agent_url, run_id, 1)
        assert row is not None and row["state"] == "succeeded"
        assert _run(_run_states(factory, run_id)) == ("queued", "running", "succeeded")

    def test_reconcile_deadline_expiry_fails_run_honestly(
        self, agent_url, factory, insert_run, scenario, composition
    ) -> None:
        """AC2: an unresolved uncertain effect never becomes a success."""
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _behavior(scenario, {READ_CAP: "unknown_status"})
        _script(scenario, [_tool_call_entry(), {"text": "unreachable"}])
        run_id = str(uuid.uuid4())
        document = _intent_document(
            run_id,
            connection="gateway",
            profile="fast",
            reconcile_wait_seconds=2.0,
            wait_poll_seconds=0.5,
        )
        client = EngineClient(agent_url)
        worker = None
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run(client, insert_run, run_id, document))
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "failed",
                RUN_TIMEOUT,
                "deadline failure",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        row = _read_step_row(agent_url, run_id, 1)
        assert row is not None and row["state"] == "outcome_unknown"
        events = _read_events(agent_url, run_id)
        awaiting = [
            event for event in events if event.type == "agent.awaiting_reconciliation"
        ]
        assert len(awaiting) == 1
        deadline = datetime.fromisoformat(awaiting[0].data["deadline"])
        assert deadline.tzinfo is not None
        assert _run(_run_states(factory, run_id)) == (
            "queued",
            "running",
            "reconciling",
            "running",
            "failed",
        )
