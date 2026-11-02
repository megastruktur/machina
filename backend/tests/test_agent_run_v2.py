"""Focused tests for the durable v2 working-agent handler (rv-action-runtime).

Acceptance (rv_action-runtime.md, executed against the REAL runtime):

1. **AC1 — one working loop:** the FIRST model call is the working agent;
   natural-language response, tool work and completion share one loop. No
   ``TurnInterpretation`` call, no ``agent.intake_*`` event, no mandatory
   todo (scenario 1 + the real-provider journey).
2. **AC2 — durable rounds + bounded correction:** every model round and
   tool attempt is a DBOS checkpoint; a malformed/invalid tool payload is
   denied before any effect and the SAME loop continues on the structured
   denial (scenario 2).
3. **AC3 — bounded authorized effects:** grants/policy/adoption/approval
   stay server-side (real dispatcher); an ask-mode mutation defers to
   ``waiting_approval`` and resumes ONLY the payload-bound operation
   through ``dispatch_once(approval_id=…)`` (scenario 3, deny + approve).
4. **AC4 — canonical activity + journal:** ``agent.*`` run events plus the
   ``objective.*`` journal projection (actions, receipts, unknowns,
   verification) capture model/tool/retry/effect state with redacted
   bounded payloads and honest outcomes (scenarios 1, 4, 5).
5. **AC5 — crash semantics:** kill -9 mid-effect ⇒ fresh worker translates
   ``outcome_unknown`` + parks ``reconciling``; settled rounds are not
   repeated; the unknown is journaled and never blindly replayed
   (scenario 4).
6. **AC6 — evidence-bound completion:** an unevidenced ``verified`` claim
   is rejected with ``completion_missing_evidence`` (objective stays
   active); a malformed advisory checklist is bookkeeping trouble — never
   a rejection, never an effect eraser (scenario 5, two runs).

Boundary discipline (test policy, mirrored from ``test_agent_run.py``):
the durable claims run against a real disposable PostgreSQL fixture
(``MAI_TEST_DATABASE_URL``) with the real Alembic chain, the real engine
adapter and REAL worker subprocesses (kill -9 mid-effect + fresh-worker
recovery). The dispatcher composition is the REAL one (real registry,
policy evaluation, approval store over the fixture database); the
capability executor is a journaled disposable fixture target whose effect
markers make "the effect happened exactly once" observable. Scripted
scenarios use a deterministic FunctionModel fake (C06 rule 6); the
real-provider journey uses the PRODUCTION model builder against the
pre-designated LiteLLM gateway and a disposable target only. Fixtures
carry tmp-namespace paths and marker words; the gateway key is read
in-memory into a ``0600`` ``.env`` and NEVER printed, logged or committed.
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
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents import run_v2
from mai.agents.journal_store import aread_journal
from mai.agents.model_store import ModelConfigStore
from mai.agents.models import SCHEMA_VERSION as MODEL_SCHEMA_VERSION
from mai.agents.objective_store import acreate_objective, aget_objective
from mai.capabilities.dispatch import canonical_payload_hash
from mai.contracts.jobs import parse_job_event
from mai.contracts.server import SCHEMA_VERSION as SERVER_SCHEMA_VERSION
from mai.jobs import steps
from mai.jobs.engine import EngineClient
from mai.jobs.lifecycle import RunLifecycle

BACKEND_ROOT = Path(__file__).resolve().parents[1]

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Scratch namespace per runtime-environment ADR (exclusive to this task).
FIXTURE_ROOT = Path(os.environ.get("TMPDIR", "/tmp")) / "mai-fxt-rv2-action"

READY_TIMEOUT = 90.0
RUN_TIMEOUT = 180.0

#: Frozen fixture identities (canonical lowercase UUID strings, C01 rule 1).
SERVER_ID = "7c2e5a90-3f4b-4d68-9a1b-0e5f6a7b8c9d"

READ_CAP = "fixture.read"
WRITE_CAP = "fixture.write"
PRIV_CAP = "fixture.priv"

HANDLER_MODULE_NAME = "agent_run_v2_scenario_handlers"

HOST_KEY = "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"

PROMPT = "You administer the rv2 fixture host. Keep changes announced.\n"
ROLE = "operator assistant"
TASK = "Run the rv2 fixture task and report the marker outcome."
GOAL = "Inspect the disposable target, apply the bounded change, verify it."

GATEWAY_CONFIG_PATH = Path(
    "/home/megastruktur/.hermes/profiles/inquisitor/config.yaml"
)
GATEWAY_MODEL = "z-ai-coding/glm-5.3-flash"
GATEWAY_BASE_URL = "http://127.0.0.1:4000/v1"
GATEWAY_KEY_REF = "MAI_GATEWAY_KEY"


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
# pure layer: v2 payload parsing
# ---------------------------------------------------------------------------


def _context_doc(
    run_id: str, *, mode: str = "delegated", allowed: tuple[str, ...] = (READ_CAP,)
) -> dict:
    from mai.policy.evaluate import ExecutionContext, PolicyMode

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


def _v2_intent_document(
    run_id: str,
    objective_id: str,
    *,
    allowed: tuple[str, ...] | None = None,
    mode: str = "delegated",
    **overrides: object,
) -> dict:
    document: dict[str, object] = {
        "payload_version": run_v2.RUN_PAYLOAD_VERSION_V2,
        "run_id": run_id,
        "server_id": SERVER_ID,
        "objective_id": objective_id,
        "context": _context_doc(run_id, mode=mode, allowed=allowed or (READ_CAP, WRITE_CAP)),
        "role": ROLE,
        "task": TASK,
    }
    document.update(overrides)
    return document


def _canonical(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class TestParseAgentRunIntentV2:
    def test_minimal_document_parses_with_defaults(self) -> None:
        run_id = str(uuid.uuid4())
        objective_id = str(uuid.uuid4())
        intent = run_v2.parse_agent_run_intent_v2(
            _canonical(_v2_intent_document(run_id, objective_id))
        )
        assert intent.run_id == run_id
        assert intent.objective_id == objective_id
        assert intent.server_id == SERVER_ID
        assert intent.max_rounds == run_v2.DEFAULT_MAX_ROUNDS
        assert (
            intent.approval_wait_seconds == run_v2.DEFAULT_APPROVAL_WAIT_SECONDS
        )
        legacy = intent.legacy()
        assert legacy.run_id == run_id and legacy.task == TASK

    def test_unknown_field_and_wrong_version_reject_value_free(self) -> None:
        run_id = str(uuid.uuid4())
        objective_id = str(uuid.uuid4())
        document = _v2_intent_document(run_id, objective_id)
        with pytest.raises(run_v2.AgentRunError) as error:
            run_v2.parse_agent_run_intent_v2(_canonical({**document, "todo": []}))
        assert error.value.code is run_v2.AgentRunErrorCode.INVALID_PAYLOAD
        mutated = dict(document)
        mutated["payload_version"] = 99
        with pytest.raises(run_v2.AgentRunError):
            run_v2.parse_agent_run_intent_v2(_canonical(mutated))
        legacy_shape = {k: v for k, v in document.items() if k != "objective_id"}
        legacy_shape["v"] = 1
        with pytest.raises(run_v2.AgentRunError):
            # The LEGACY payload must never parse as v2 (no silent reuse).
            run_v2.parse_agent_run_intent_v2(_canonical(legacy_shape))

    def test_identity_mismatch_rejects(self) -> None:
        run_id = str(uuid.uuid4())
        objective_id = str(uuid.uuid4())
        document = _v2_intent_document(run_id, objective_id)
        document["context"] = _context_doc(str(uuid.uuid4()))
        with pytest.raises(run_v2.AgentRunError):
            run_v2.parse_agent_run_intent_v2(_canonical(document))

    def test_approval_resume_seq_is_deterministic_and_collision_free(self) -> None:
        assert run_v2.approval_resume_seq(1) == 1 + 1024
        assert run_v2.approval_resume_seq(9) == run_v2.approval_resume_seq(9)
        # Normal round-major seqs never reach the stride.
        assert run_v2.approval_resume_seq(0) > 32 * 8


# ---------------------------------------------------------------------------
# fixture database + worker composition (mirrors test_agent_run.py)
# ---------------------------------------------------------------------------


def _alembic_config() -> Config:
    return Config(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state (engine schema too)."""
    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS objective_journal, objective_checklists, "
            "objective_events, objective_runs, objective_servers, "
            "objectives, "
            "job_resource_locks, job_schedule_fires, "
            "job_schedules, job_events, job_steps, job_runs, approvals, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, notification_alerts, observation_findings, "
            "server_observations, knowledge_doc_chunks, knowledge_documents, "
            "application_projection_journal, application_projection, "
            "conversation_steering, conversation_runs, conversation_messages, "
            "conversations, agent_task_completions, agent_task_revisions, "
            "agent_task_evidence, agent_task_criteria, "
            "agent_task_constraints, agent_task_steps, agent_tasks, "
            "conversation_tags, job_tags, tags, "
            "alembic_version"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq, tags_seq")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def agent_url() -> Iterator[str]:
    """Migrate the disposable DB, then restore pre-bootstrap state."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the durable v2 claims need a real "
            "disposable Postgres fixture"
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
        from mai.jobs import engine

        engine.launch_worker(url)  # creates the engine-owned dbos schema
        engine.shutdown()
        yield url
    finally:
        from mai.jobs import engine

        engine.shutdown()
        _drop_app_tables(url)


@pytest.fixture
def factory(agent_url: str) -> async_sessionmaker[AsyncSession]:
    """NullPool session factory for in-process facade assertions."""
    from mai.database import create_app_engine

    eng, sessions = create_app_engine(agent_url)

    async def _clean() -> None:
        async with sessions() as session:
            for table in (
                "objective_journal",
                "objective_checklists",
                "objective_events",
                "objective_runs",
                "objective_servers",
                "objectives",
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

    async def _insert(
        run_id: str, *, requested_grants: tuple[str, ...] = (READ_CAP, WRITE_CAP)
    ) -> None:
        from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID, SYSTEM_ASSIGNMENT_SOURCE

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
                         CAST(:requested_grants AS jsonb), now(), now())
                    """
                ),
                {
                    "run_id": uuid.UUID(run_id),
                    "server_id": uuid.UUID(SERVER_ID),
                    "requested_grants": json.dumps(list(requested_grants)),
                },
            )
            # The 0011 mandatory-classification invariant: every job run
            # commits with at least one tag; fixture rows take the reserved
            # default (the catalog row the chain seeds).
            await session.execute(
                text(
                    """
                    INSERT INTO job_tags
                        (job_id, tag_id, assigned_by, assignment_source)
                    VALUES (CAST(:rid AS uuid), CAST(:tag AS uuid), '', :source)
                    """
                ),
                {
                    "rid": run_id,
                    "tag": RESERVED_UNCLASSIFIED_TAG_ID,
                    "source": SYSTEM_ASSIGNMENT_SOURCE,
                },
            )
            await session.commit()

    return _insert


@pytest.fixture
def make_objective(factory: async_sessionmaker[AsyncSession]) -> Callable[[], str]:
    """Create one operator-owned objective episode; return its id."""

    def _create() -> str:
        objective = _run(
            acreate_objective(
                factory,
                server_id=uuid.UUID(SERVER_ID),
                original_request=GOAL,
                constraints=("announce every change", "stay on the fixture host"),
                scope_server_ids=[uuid.UUID(SERVER_ID)],
                title="rv2 scenario objective",
            )
        )
        return str(objective.objective_id)

    return _create


@pytest.fixture
def scenario(tmp_path: Path) -> Path:
    """One fresh scratch scenario directory (journal, gates, handler)."""
    directory = tmp_path / "scenario"
    (directory / "gates").mkdir(parents=True)
    handlers = directory / "handlers"
    handlers.mkdir()
    (handlers / f"{HANDLER_MODULE_NAME}.py").write_text(
        V2_HANDLER_MODULE_SOURCE, encoding="utf-8"
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
        connection["credential_ref"] = "MAI_FXT_TOKEN"
    store.write_connection("gateway", json.dumps(connection) + "\n", expected_revision=0)
    profile = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "name": "fast",
        "connection": "gateway",
        "model": "fixture-model",
        "max_tokens": 512,
    }
    store.write_profile("fast", json.dumps(profile) + "\n", expected_revision=0)
    store.set_selection("gateway", "fast", expected_revision=0)


def _write_data_root(root: Path) -> None:
    from mai.profiles.files import ProfileFileStore

    store = ProfileFileStore(root)
    profile_text = (
        f"schema_version: {SERVER_SCHEMA_VERSION}\n"
        f'server_id: "{SERVER_ID}"\n'
        'name: "rv2-action-fixture"\n'
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
        f"  - {READ_CAP}\n"
        f"  - {WRITE_CAP}\n"
        f"  - {PRIV_CAP}\n"
    )
    store.write(uuid.UUID(SERVER_ID), profile_text, expected_revision=0)
    server_dir = root / "servers" / SERVER_ID
    server_dir.chmod(0o700)
    prompt_path = server_dir / "prompt.md"
    prompt_path.write_text(PROMPT, encoding="utf-8")
    os.chmod(prompt_path, 0o600)


@pytest.fixture
def composition(scenario: Path) -> Callable[..., dict[str, Path]]:
    """Write the fixture model/data roots once per scenario."""

    def _compose(
        *,
        base_url: str = "http://127.0.0.1:9/v1",
        credential: bool = True,
    ) -> dict[str, Path]:
        model_root = scenario / "model-root"
        data_root = scenario / "data-root"
        _write_model_root(model_root, base_url=base_url, credential=credential)
        _write_data_root(data_root)
        (scenario / "fixture.env").write_text(
            "MAI_FXT_TOKEN=fxt-synthetic-token-value-rv2\n", encoding="utf-8"
        )
        os.chmod(scenario / "fixture.env", 0o600)
        return {"model_root": model_root, "data_root": data_root}

    return _compose


# ---------------------------------------------------------------------------
# the worker-side composition (written into the scenario handler dir)
# ---------------------------------------------------------------------------

V2_HANDLER_MODULE_SOURCE = '''
"""Worker-side composition for rv-action-runtime: real dispatcher,
journaled disposable target, deterministic FunctionModel fake (or the
production builder for the real-provider journey), plus the v2 handler
registration."""

import asyncio
import json
import os
import time
import uuid

from pydantic_ai.models.function import DeltaToolCall, FunctionModel
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
    """Journaled disposable target: effect markers make exactly-once
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
        # Replay-safe script position: derived from the DURABLE
        # conversation length, never from process-local call counts —
        # a recovered worker continues the same conversation at the same
        # script entry (the checkpointed history is the round identity).
        index = (len(messages) - 1) // 2
        if index >= len(script):
            raise AssertionError("model script exhausted")
        # Journal what the model sees as the latest tool return so the
        # correction/resume honesty is observable from the scenario journal.
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
        return _entry_payload(script[index])

    async def _model_stream_fn(messages, info):
        # The production run path is a STREAMED model round, so the fake
        # must speak provider deltas; the scripted decision is identical.
        response = await asyncio.to_thread(_model_fn, messages, info)
        for index, part in enumerate(response.parts):
            if isinstance(part, ToolCallPart):
                yield {
                    index: DeltaToolCall(
                        name=part.tool_name,
                        json_args=part.args_as_json_str(),
                        tool_call_id=part.tool_call_id,
                    )
                }
            elif isinstance(part, TextPart):
                yield part.content

    return FunctionModel(_model_fn, stream_function=_model_stream_fn)


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
    registry.register(_descriptor("fixture.write", "mutate"))
    registry.register(_descriptor("fixture.priv", "privileged"))
    executor = FixtureExecutor()
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
        executor=executor,
    )
    model_store = ModelConfigStore(os.environ["MAI_FX_MODEL_ROOT"])
    scripted = os.environ.get("MAI_FX_MODEL_SCRIPT")
    # ONE model per process: the script state advances across the
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
    # The v2 handler registers itself through the ONE engine seam at
    # import (runtime-v2.md §2); the executor above is the SAME composed
    # seam the v2 loop drives — no second executor exists.
    import mai.agents.run_v2  # noqa: F401


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
    env["MAI_WORKER_HANDLERS"] = f"{HANDLER_MODULE_NAME},mai.agents.run_v2"
    env["MAI_FX_JOURNAL"] = str(_journal_path(scenario))
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


def _event_types(url: str, run_id: str) -> list[str]:
    return [event.type for event in _read_events(url, run_id)]


async def _run_states(factory: async_sessionmaker, run_id: str) -> tuple:
    return await RunLifecycle(factory).replay_states(run_id)


async def _current_state(factory: async_sessionmaker, run_id: str) -> str | None:
    state = await RunLifecycle(factory).current_state(run_id)
    return None if state is None else state.value


async def _enqueue_run_v2(
    client: EngineClient,
    insert_run: Callable[..., object],
    run_id: str,
    objective_id: str,
    document: dict,
) -> None:
    await insert_run(run_id)
    client.enqueue_run(run_id, run_v2.HANDLER_NAME, _canonical(document))


def _send_wake(run_id: str, url: str, message: dict) -> None:
    """Control-plane park wake: a durable message on the resume topic."""
    from dbos import DBOSClient

    client = DBOSClient(
        system_database_url=url,
        application_name="mai",
        lazy=True,
    )
    try:
        client.send(run_id, message, topic=steps.RESUME_TOPIC)
    finally:
        client.destroy()


def _resolve_step(url: str, run_id: str, seq: int, state: str) -> None:
    """Reconciliation-owner stand-in: resolve one unknown step terminally.

    Writes ONLY what the reconciliation owner may write — a guarded
    terminal step transition with the verdict recorded on the receipt.
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


def _read_objective_events(factory: async_sessionmaker, objective_id: str) -> list[dict]:
    async def _read() -> list[dict]:
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT seq, type, actor_kind, run_id, data "
                        "FROM objective_events WHERE objective_id = :oid "
                        "ORDER BY seq"
                    ),
                    {"oid": uuid.UUID(objective_id)},
                )
            ).mappings().all()
            return [dict(row) for row in rows]

    return _run(_read())  # type: ignore[return-value]


def _read_journal(factory: async_sessionmaker, objective_id: str):
    return _run(aread_journal(factory, uuid.UUID(objective_id), server_id=uuid.UUID(SERVER_ID)))


async def _objective_status(factory: async_sessionmaker, objective_id: str) -> str:
    detail = await aget_objective(
        factory, uuid.UUID(objective_id), server_id=uuid.UUID(SERVER_ID)
    )
    return detail.status.value


# ---------------------------------------------------------------------------
# live: durable v2 working-agent runs (real engine, database, dispatcher)
# ---------------------------------------------------------------------------


class TestV2DurableRuns:
    def test_read_mutate_verify_one_submission_journals_the_journey(
        self, agent_url, factory, insert_run, make_objective, scenario, composition
    ) -> None:
        """AC1+AC4: direct read -> authorized mutation -> verification in ONE
        submission; the loop ends on the natural-language response; every
        effect is journaled with its dispatcher decision and receipt."""
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}, {WRITE_CAP: True}])
        _script(
            scenario,
            [
                _tool_call_entry("fixture_read", "probe", "c1"),
                _tool_call_entry("fixture_write", "change-1", "c2"),
                _tool_call_entry("fixture_read", "verify", "c3"),
                {"text": "Change applied and verified on the fixture host."},
            ],
        )
        objective_id = make_objective()
        run_id = str(uuid.uuid4())
        document = _v2_intent_document(run_id, objective_id, wait_poll_seconds=0.5)
        client = EngineClient(agent_url)
        worker = None
        engine_final = None
        started = time.monotonic()
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run_v2(client, insert_run, run_id, objective_id, document))
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
        elapsed = time.monotonic() - started

        # AC1: the working loop, exactly the scripted journey, no intake.
        assert _journal_lines(scenario) == [
            "model_call",
            "begin:fixture.read:probe",
            "effect:fixture.read:probe",
            "model_call",
            "tool_return:executed",
            "begin:fixture.write:change-1",
            "effect:fixture.write:change-1",
            "model_call",
            "tool_return:executed",
            "begin:fixture.read:verify",
            "effect:fixture.read:verify",
            "model_call",
            "tool_return:executed",
        ]
        types = _event_types(agent_url, run_id)
        intake_types = [
            event_type
            for event_type in types
            if event_type.startswith("agent.intake_")
            or event_type == "agent.conversation_history"
        ]
        assert intake_types == [], "no legacy intake event may appear on a v2 run"
        assert types.count("agent.started") == 1
        assert types.count("agent.context_assembled") == 1
        rounds = [
            event
            for event in _read_events(agent_url, run_id)
            if event.type == "agent.model_round"
        ]
        assert [event.data["status"] for event in rounds] == [
            "tool_calls",
            "tool_calls",
            "tool_calls",
            "final",
        ]
        assert types[-1] == "agent.completed"

        # Each effect is its own exactly-once guarded attempt row.
        rows = _read_step_rows(agent_url, run_id)
        assert [row["seq"] for row in rows] == [1, 9, 17]
        assert [row["state"] for row in rows] == ["succeeded"] * 3
        assert [row["capability_id"] for row in rows] == [
            READ_CAP,
            WRITE_CAP,
            READ_CAP,
        ]
        assert all(row["receipt"]["allowed"] is True for row in rows)
        assert all(row["receipt"]["effect"] == "executed" for row in rows)

        # AC4: the objective journal carries actions + receipts per effect.
        journal = _read_journal(factory, objective_id)
        assert [(a.capability_id, a.decision) for a in journal.actions] == [
            (READ_CAP, "allowed"),
            (WRITE_CAP, "allowed"),
            (READ_CAP, "allowed"),
        ]
        assert all(a.server_id == uuid.UUID(SERVER_ID) for a in journal.actions)
        assert [(r.outcome, r.effect_class) for r in journal.receipts] == [
            ("succeeded", "read"),
            ("succeeded", "mutate"),
            ("succeeded", "read"),
        ]
        assert journal.unknowns == []
        assert journal.verification is None
        assert journal.status.value == "active"

        # The durable run↔objective binding (§3.3) + legal lifecycle.
        bindings = _run(_read_objective_run_bindings(factory, objective_id))
        assert bindings == [(str(uuid.UUID(run_id)), "agent.run_v2")]
        assert _run(_run_states(factory, run_id)) == (
            "queued",
            "running",
            "succeeded",
        )
        assert elapsed < RUN_TIMEOUT

    def test_malformed_tool_payload_denied_and_loop_recovers(
        self, agent_url, factory, insert_run, make_objective, scenario, composition
    ) -> None:
        """AC2: a schema-violating call is denied BEFORE any effect and the
        SAME working loop continues on the bounded structured denial."""
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}, {WRITE_CAP: True}])
        _script(
            scenario,
            [
                _tool_call_entry("fixture_read", "bad", "c1", args={"marker": 5}),
                _tool_call_entry("fixture_read", "fixed", "c2"),
                {"text": "corrected and done"},
            ],
        )
        objective_id = make_objective()
        run_id = str(uuid.uuid4())
        document = _v2_intent_document(run_id, objective_id)
        client = EngineClient(agent_url)
        worker = None
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run_v2(client, insert_run, run_id, objective_id, document))
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "run success",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        # The run did NOT fail: the model saw the honest denial and the
        # corrected call executed once in the same conversation.
        assert (_terminal_engine_state(client, run_id) or "") == "succeeded"
        assert _journal_lines(scenario) == [
            "model_call",
            "model_call",
            "tool_return:denied",
            "begin:fixture.read:fixed",
            "effect:fixture.read:fixed",
            "model_call",
            "tool_return:executed",
        ]
        denied = _read_step_row(agent_url, run_id, 1)
        assert denied is not None and denied["state"] == "failed"
        assert denied["receipt"]["reason"] == "bridge_payload_schema_mismatch"
        assert denied["receipt"]["effect"] == "not_attempted"
        fixed = _read_step_row(agent_url, run_id, 9)
        assert fixed is not None and fixed["state"] == "succeeded"
        journal = _read_journal(factory, objective_id)
        assert [(a.capability_id, a.decision) for a in journal.actions] == [
            (READ_CAP, "denied"),
            (READ_CAP, "allowed"),
        ]
        assert len(journal.receipts) == 1  # only the executed attempt has an op
        assert _run(_run_states(factory, run_id)) == ("queued", "running", "succeeded")

    def test_approval_defer_parks_and_payload_bound_resume_executes_once(
        self, agent_url, factory, insert_run, make_objective, scenario, composition
    ) -> None:
        """AC3: an ask-mode mutation defers to ``waiting_approval``; the
        resume presents the ONE payload-bound approval through
        ``dispatch_once(approval_id=…)`` and the effect runs exactly once."""
        composition()
        _write_policy_files(scenario, grants=[{WRITE_CAP: True}])
        _script(
            scenario,
            [
                _tool_call_entry("fixture_write", "approved-1", "c1"),
                {"text": "resumed and done"},
            ],
        )
        objective_id = make_objective()
        run_id = str(uuid.uuid4())
        payload = {"marker": "approved-1"}
        document = _v2_intent_document(
            run_id,
            objective_id,
            allowed=(WRITE_CAP,),
            mode="ask",
            approval_wait_seconds=60.0,
            wait_poll_seconds=0.5,
        )
        client = EngineClient(agent_url)
        worker = None
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run_v2(client, insert_run, run_id, objective_id, document))
            # The refusal parks the run durably on the approval wait.
            _wait_until(
                lambda: _run(_current_state(factory, run_id)) == "waiting_approval",
                RUN_TIMEOUT,
                "the waiting_approval park",
            )
            row = _read_step_row(agent_url, run_id, 1)
            assert row is not None and row["state"] == "failed"
            approval_id = row["receipt"]["approval_id"]
            assert approval_id
            assert row["receipt"]["reason"] == "approval_required_but_absent"
            assert row["receipt"]["effect"] == "not_attempted"
            assert _journal_count(scenario, "effect:fixture.write:approved-1") == 0
            events = _read_events(agent_url, run_id)
            awaiting = [
                event
                for event in events
                if event.type == "agent.awaiting_approval"
            ]
            assert len(awaiting) == 1
            assert awaiting[0].data["approval_id"] == approval_id
            assert awaiting[0].data["deadline"]

            # The operator decision arrives; the worker-side one-shot
            # resume presents the payload-bound approval (C03 rule 4).
            _send_wake(
                run_id,
                agent_url,
                {"kind": "approval", "approval_id": approval_id, "decision": "approve"},
            )
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "run success after approval resume",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        # The effect happened EXACTLY once — on the resume attempt row.
        assert _journal_lines(scenario) == [
            "model_call",
            "begin:fixture.write:approved-1",
            "effect:fixture.write:approved-1",
            "model_call",
            "tool_return:executed",
        ]
        resume_row = _read_step_row(agent_url, run_id, run_v2.approval_resume_seq(1))
        assert resume_row is not None and resume_row["state"] == "succeeded"
        assert resume_row["receipt"]["approval_spent"] is True
        assert resume_row["receipt"]["approval_id"] == approval_id
        # The approval was consumed bound to the EXACT payload hash.
        with psycopg.connect(agent_url, row_factory=dict_row) as conn:
            approval = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = %s",
                (uuid.UUID(approval_id),),
            ).fetchone()
        assert approval is not None
        assert approval["consumed_at"] is not None
        assert approval["payload_hash"] == canonical_payload_hash(payload)
        # The model saw the RESUMED executed outcome (honest continuation).
        assert "tool_return:executed" in _journal_lines(scenario)
        # Lifecycle: the durable park and its resume are on the diagram.
        assert _run(_run_states(factory, run_id)) == (
            "queued",
            "running",
            "waiting_approval",
            "running",
            "succeeded",
        )
        journal = _read_journal(factory, objective_id)
        decisions = [(a.capability_id, a.decision) for a in journal.actions]
        assert (WRITE_CAP, "denied") in decisions  # the refused attempt
        assert (WRITE_CAP, "allowed") in decisions  # the resumed attempt

    def test_approval_deny_resumes_without_effect(
        self, agent_url, factory, insert_run, make_objective, scenario, composition
    ) -> None:
        """AC3: a denied approval resumes the run honestly with NO effect."""
        composition()
        _write_policy_files(scenario, grants=[{WRITE_CAP: True}])
        _script(
            scenario,
            [
                _tool_call_entry("fixture_write", "denied-1", "c1"),
                {"text": "understood; the change was not applied"},
            ],
        )
        objective_id = make_objective()
        run_id = str(uuid.uuid4())
        document = _v2_intent_document(
            run_id,
            objective_id,
            allowed=(WRITE_CAP,),
            mode="ask",
            approval_wait_seconds=60.0,
            wait_poll_seconds=0.5,
        )
        client = EngineClient(agent_url)
        worker = None
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run_v2(client, insert_run, run_id, objective_id, document))
            _wait_until(
                lambda: _run(_current_state(factory, run_id)) == "waiting_approval",
                RUN_TIMEOUT,
                "the waiting_approval park",
            )
            row = _read_step_row(agent_url, run_id, 1)
            approval_id = row["receipt"]["approval_id"]
            _send_wake(
                run_id,
                agent_url,
                {"kind": "approval", "approval_id": approval_id, "decision": "deny"},
            )
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "run success after denial",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        # NO effect ever ran; the model saw the honest denial and finished
        # in the same working loop.
        assert _journal_lines(scenario) == [
            "model_call",
            "model_call",
            "tool_return:denied",
        ]
        assert _read_step_rows(agent_url, run_id)[0]["state"] == "failed"
        assert _run(_run_states(factory, run_id)) == (
            "queued",
            "running",
            "waiting_approval",
            "running",
            "succeeded",
        )
        journal = _read_journal(factory, objective_id)
        assert [(a.capability_id, a.decision) for a in journal.actions] == [
            (WRITE_CAP, "denied")
        ]

    def test_kill_mid_effect_parks_reconciling_journals_unknown_never_replays(
        self, agent_url, factory, insert_run, make_objective, scenario, composition
    ) -> None:
        """AC5: kill -9 across the effect boundary translates to
        ``outcome_unknown`` + ``reconciling``; settled rounds are not
        repeated; the unknown is journaled and only explicit
        classification resolves it — never a blind replay."""
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}, {WRITE_CAP: True}])
        _behavior(scenario, {WRITE_CAP: "gate"})
        _script(
            scenario,
            [
                _tool_call_entry("fixture_write", "crash-1", "c1"),
                {"text": "resumed cleanly"},
            ],
        )
        objective_id = make_objective()
        run_id = str(uuid.uuid4())
        document = _v2_intent_document(
            run_id,
            objective_id,
            reconcile_wait_seconds=90.0,
            wait_poll_seconds=0.5,
        )
        client = EngineClient(agent_url)
        worker1 = None
        worker2 = None
        try:
            worker1 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run_v2(client, insert_run, run_id, objective_id, document))
            _wait_until(
                lambda: _journal_count(scenario, "begin:fixture.write:crash-1") == 1,
                RUN_TIMEOUT,
                "the gated attempt to start",
            )
            _kill9(worker1)

            worker2 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w2")
            _wait_until(
                lambda: _run(_current_state(factory, run_id)) == "reconciling",
                RUN_TIMEOUT,
                "the reconciling park",
            )
            row = _read_step_row(agent_url, run_id, 1)
            assert row is not None and row["state"] == "outcome_unknown"
            assert row["receipt"]["reason"] == "crash_recovery"
            assert _journal_count(scenario, "effect:fixture.write:crash-1") == 0
            # AC4/AC5: the unknown effect is journaled honestly while open.
            journal = _read_journal(factory, objective_id)
            assert len(journal.unknowns) == 1
            assert journal.unknowns[0].reconciled is False
            assert journal.unknowns[0].server_id == uuid.UUID(SERVER_ID)

            # Explicit classification resolves; the wake triggers the
            # durable re-check and the loop continues from the checkpoint.
            _resolve_step(agent_url, run_id, 1, "succeeded")
            _send_wake(run_id, agent_url, {"kind": "resume"})
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

        # Exactly one attempt; the settled round was replayed from its
        # checkpoint (the model was asked once before the crash, once after).
        assert _journal_count(scenario, "begin:fixture.write:crash-1") == 1
        assert _journal_count(scenario, "model_call") == 2
        row = _read_step_row(agent_url, run_id, 1)
        assert row is not None and row["state"] == "succeeded"
        assert row["receipt"]["resolution"] == "terminal"
        events = _event_types(agent_url, run_id)
        assert events.count("agent.awaiting_reconciliation") == 1
        assert _run(_run_states(factory, run_id)) == (
            "queued",
            "running",
            "reconciling",
            "running",
            "succeeded",
        )
        # The unknown flipped to reconciled. The receipt section stays
        # EMPTY — honest: the crash-window receipt never carried an
        # operation reference, so there is no op to record (the resolved
        # action decision + the reconciled unknown carry the outcome).
        journal = _read_journal(factory, objective_id)
        assert len(journal.unknowns) == 1
        assert journal.unknowns[0].reconciled is True
        assert journal.receipts == []
        assert any(
            a.capability_id == WRITE_CAP and a.decision == "allowed"
            for a in journal.actions
        )

    def test_optional_checklist_and_evidence_bound_completion(
        self, agent_url, factory, insert_run, make_objective, scenario, composition
    ) -> None:
        """AC6: a malformed advisory checklist is bookkeeping trouble; an
        unevidenced ``verified`` claim is rejected with
        ``completion_missing_evidence`` (objective stays active, effects
        intact); an evidenced claim settles the objective."""
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}, {WRITE_CAP: True}])
        objective_id = make_objective()

        # --- Run A: malformed checklist + unevidenced completion claim. ---
        _script(
            scenario,
            [
                _tool_call_entry("fixture_write", "prep-a", "c1"),
                {
                    "text": (
                        "```machina-checklist\n{\"items\": \"not-a-list\"}\n```\n"
                        "```machina-complete\n"
                        "{\"task_outcome\": \"verified\", "
                        "\"criteria\": [{\"criterion_id\": \"applied\", "
                        "\"result\": \"passed\", \"evidence\": [\"step:999\"]}]}\n"
                        "```\n"
                        "Done."
                    )
                },
            ],
        )
        run_a = str(uuid.uuid4())
        document_a = _v2_intent_document(run_a, objective_id)
        client = EngineClient(agent_url)
        worker = None
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run_v2(client, insert_run, run_a, objective_id, document_a))
            _wait_until(
                lambda: (_terminal_engine_state(client, run_a) or "") == "succeeded",
                RUN_TIMEOUT,
                "run A success",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        # The run did NOT fail and the effect stayed journaled.
        assert _journal_count(scenario, "effect:fixture.write:prep-a") == 1
        degraded = [
            event
            for event in _read_events(agent_url, run_a)
            if event.type == "agent.journal_degraded"
        ]
        # The malformed checklist degrades to ONE honest bookkeeping
        # observation; the unevidenced completion is rejected through the
        # structured missing-evidence journal note below (not a run error).
        assert {event.data["section"] for event in degraded} == {"checklist"}
        journal = _read_journal(factory, objective_id)
        assert journal.verification is not None
        assert journal.verification.missing_evidence is True
        assert (
            journal.verification.verdict.task_outcome.value == "verified"
        )
        # The objective is NOT completed: the claim had no durable evidence.
        assert _run(_objective_status(factory, objective_id)) == "active"
        assert "objective.completed" not in [
            row["type"] for row in _read_objective_events(factory, objective_id)
        ]
        # The malformed checklist produced NO checklist ledger event.
        assert "objective.checklist_updated" not in [
            row["type"] for row in _read_objective_events(factory, objective_id)
        ]

        # --- Run B: valid advisory checklist + evidence-bound completion. ---
        _script(
            scenario,
            [
                _tool_call_entry("fixture_write", "apply-b", "c1"),
                _tool_call_entry("fixture_read", "verify-b", "c2"),
                {
                    "text": (
                        "```machina-checklist\n"
                        "{\"items\": [{\"text\": \"apply change\", \"done\": true}]}\n"
                        "```\n"
                        "```machina-complete\n"
                        "{\"task_outcome\": \"verified\", "
                        "\"domain_outcome\": \"unknown\", "
                        "\"criteria\": [{\"criterion_id\": \"applied\", "
                        "\"result\": \"passed\", \"evidence\": [\"step:9\"]}]}\n"
                        "```\n"
                        "Applied and verified."
                    )
                },
            ],
        )
        run_b = str(uuid.uuid4())
        document_b = _v2_intent_document(run_b, objective_id)
        client = EngineClient(agent_url)
        worker = None
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w2")
            _run(_enqueue_run_v2(client, insert_run, run_b, objective_id, document_b))
            _wait_until(
                lambda: (_terminal_engine_state(client, run_b) or "") == "succeeded",
                RUN_TIMEOUT,
                "run B success",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        # The evidenced verdict settles the objective (evidence = the
        # run's OWN succeeded verification step row, seq 9).
        assert _run(_objective_status(factory, objective_id)) == "completed"
        objective_types = [
            row["type"] for row in _read_objective_events(factory, objective_id)
        ]
        assert "objective.completed" in objective_types
        assert "objective.checklist_updated" in objective_types
        journal = _read_journal(factory, objective_id)
        assert journal.verification is not None
        assert journal.verification.missing_evidence is False
        assert journal.checklist.malformed is False
        assert [item.text for item in journal.checklist.items] == ["apply change"]
        assert journal.status.value == "completed"
        # Run A's effects were never erased by either projection.
        assert any(
            a.capability_id == WRITE_CAP and a.decision == "allowed"
            for a in journal.actions
        )


async def _read_objective_run_bindings(
    factory: async_sessionmaker, objective_id: str
) -> list[tuple[str, str]]:
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT run_id, handler FROM objective_runs "
                    "WHERE objective_id = :oid ORDER BY created_at"
                ),
                {"oid": uuid.UUID(objective_id)},
            )
        ).all()
    return [(str(row[0]), str(row[1])) for row in rows]


# ---------------------------------------------------------------------------
# live: the REAL-provider action journey (blocking preflight model/target)
# ---------------------------------------------------------------------------


def _gateway_key() -> str:
    """Read the LiteLLM gateway key IN MEMORY (never printed/logged)."""
    import yaml

    document = yaml.safe_load(GATEWAY_CONFIG_PATH.read_text(encoding="utf-8"))
    key = document["model"]["api_key"]
    if not isinstance(key, str) or not key:
        raise RuntimeError("gateway config carries no usable api_key")
    return key


@pytest.fixture(scope="session")
def gateway_key() -> str:
    try:
        import yaml  # noqa: F401

        return _gateway_key()
    except Exception as error:
        pytest.fail(
            "the real-provider journey requires the preflight gateway "
            f"config ({GATEWAY_CONFIG_PATH}): {type(error).__name__}"
        )


class TestV2RealProviderJourney:
    def test_real_provider_action_journey_read_mutate_verify(
        self,
        agent_url,
        factory,
        insert_run,
        make_objective,
        scenario,
        composition,
        gateway_key,
        tmp_path,
    ) -> None:
        """The REAL model drives the v2 loop end-to-end: inspect -> act ->
        verify on a disposable target via the REAL dispatcher. Asserts zero
        intake events/calls and records model id, provider latency and the
        first useful action in a journey receipt."""
        import httpx

        # Preflight: the gateway is reachable before anything submits.
        probe = httpx.get(
            f"{GATEWAY_BASE_URL}/models",
            headers={"Authorization": f"Bearer {gateway_key}"},
            timeout=10,
        )
        assert probe.status_code == 200, "gateway preflight failed"

        # The credential crosses ONLY through a 0600 .env in tmp_path.
        env_path = tmp_path / "gateway.env"
        env_path.write_text(f"{GATEWAY_KEY_REF}={gateway_key}\n", encoding="utf-8")
        os.chmod(env_path, 0o600)
        assert oct(env_path.stat().st_mode & 0o777) == "0o600"

        composition(credential=False)
        # Rewrite the model root for the REAL provider (connection carries
        # only the credential REFERENCE; the key resolves at call time).
        store = ModelConfigStore(scenario / "model-root")
        connection = {
            "schema_version": MODEL_SCHEMA_VERSION,
            "name": "gateway",
            "base_url": GATEWAY_BASE_URL,
            "timeout_seconds": 180,
            "capabilities": {"tool_calls": True},
            "credential_ref": GATEWAY_KEY_REF,
        }
        store.write_connection(
            "gateway",
            json.dumps(connection) + "\n",
            expected_revision=store.read_connection("gateway").revision,
        )
        profile = {
            "schema_version": MODEL_SCHEMA_VERSION,
            "name": "fast",
            "connection": "gateway",
            "model": GATEWAY_MODEL,
            "max_tokens": 2048,
        }
        store.write_profile(
            "fast",
            json.dumps(profile) + "\n",
            expected_revision=store.read_profile("fast").revision,
        )
        selection_revision = store.get_selection().revision
        store.set_selection("gateway", "fast", expected_revision=selection_revision)
        # The worker's .env is the tmp_path file (never the scenario one).
        (scenario / "fixture.env").write_text(
            env_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        os.chmod(scenario / "fixture.env", 0o600)

        _write_policy_files(scenario, grants=[{READ_CAP: True}, {WRITE_CAP: True}])
        objective_id = make_objective()
        run_id = str(uuid.uuid4())
        task = (
            "Inspect the target with fixture_read (marker 'rv2-live-probe'), "
            "then apply the bounded change with fixture_write (marker "
            "'rv2-live-change'), verify it with fixture_read (marker "
            "'rv2-live-verify'), then answer with one short sentence."
        )
        document = _v2_intent_document(
            run_id,
            objective_id,
            rounds=8,
        )
        # The task rides the intent (strict parse bounds only).
        document["task"] = task
        client = EngineClient(agent_url)
        worker = None
        started = time.monotonic()
        try:
            worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_run_v2(client, insert_run, run_id, objective_id, document))
            _wait_until(
                lambda: (_terminal_engine_state(client, run_id) or "")
                in {"succeeded", "failed", "error", "cancelled"},
                420.0,
                "real-provider run completion",
            )
            engine_final = _terminal_engine_state(client, run_id)
        finally:
            if worker is not None:
                _stop(worker)
            client.close()
        latency = time.monotonic() - started
        assert engine_final == "succeeded", (
            f"real-provider journey ended {engine_final!r}; "
            f"worker log: {(scenario / 'w1.log').read_text(encoding='utf-8')[-2000:]}"
        )

        events = _read_events(agent_url, run_id)
        types = [event.type for event in events]
        # AC1: zero intake events (and therefore zero intake model calls —
        # the legacy intake seam was never composed into the v2 loop).
        assert not [
            event_type
            for event_type in types
            if event_type.startswith("agent.intake_")
        ]
        started_event = next(event for event in events if event.type == "agent.started")
        assert started_event.data["model"] == GATEWAY_MODEL
        requested = [event for event in events if event.type == "agent.tool_requested"]
        assert requested, "the real model must request at least one tool"
        first_action = {
            "capability_id": requested[0].data.get("capability_id"),
            "at_seq": requested[0].seq,
        }
        rounds = [event for event in events if event.type == "agent.model_round"]
        completed = next(event for event in events if event.type == "agent.completed")
        assert int(completed.data["output_chars"]) > 0, "a real answer was recorded"

        # The disposable target felt the real journey: at least one read,
        # the bounded mutation, and a verification read — each a real
        # dispatch through the ONE authorization boundary.
        lines = _journal_lines(scenario)
        read_markers = [
            line for line in lines if line.startswith("effect:fixture.read:")
        ]
        write_markers = [
            line for line in lines if line.startswith("effect:fixture.write:")
        ]
        assert read_markers, "the real model must perform a real read"
        assert write_markers, "the real model must perform the bounded mutation"
        assert len(read_markers) >= 2, "the journey must verify after the mutation"

        receipt = {
            "scenario": "rv2-real-provider-action-journey",
            "handler": run_v2.HANDLER_NAME,
            "model": GATEWAY_MODEL,
            "base_url": GATEWAY_BASE_URL,
            "provider_latency_seconds": round(latency, 2),
            "model_rounds": len(rounds),
            "first_useful_action": first_action,
            "tool_requests": len(requested),
            "engine_state": engine_final,
            "intake_events": 0,
            "effects": {"reads": len(read_markers), "mutations": len(write_markers)},
        }
        receipt_path = scenario / "journey_receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(f"REAL-PROVIDER JOURNEY RECEIPT: {json.dumps(receipt)}")
        # The key must never leak into recorded artifacts or event data
        # (the 0600 .env is the ONLY place it exists on disk).
        for event in events:
            assert gateway_key not in json.dumps(event.data, default=str)
        assert gateway_key not in receipt_path.read_text(encoding="utf-8")
