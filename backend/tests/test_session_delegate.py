"""D05 tests: bounded durable session delegation on a real DBOS worker.

Reuses the PROVEN harness pattern from ``tests/test_session_runner.py``
(which reuses ``tests/test_agent_run.py``): the same worker subprocess
spawn/kill -9/stop protocol, the same journaled ``FixtureExecutor``
(begin/effect markers + gates), the same disposable pgvector Postgres +
real Alembic chain + real engine bootstrap — adapted to drive the D05
delegation surface (the parent runner calls ``session.delegate``; the
child is the D04 runner re-entry with NARROWED authority). The scripted
model selects the PARENT or CHILD script by the delegation goal marker
(``[child-task]`` in the child's user turn), so both executions are
deterministic and replay-safe.

Required proof (BRIEF_D05 §C):

1. **Real parent→child delegation** — the parent's ``session.delegate``
   call starts a REAL child ``agent.session_run`` workflow (deterministic
   id) with narrowed capabilities; the child's fixture effect happens
   exactly once; the parent receives exactly ONE bounded tool result whose
   preview is the redacted summary document; the child transcript is never
   spliced into the parent's surface; the parent's ``DelegationEntry``
   settles with the child's truth.
2. **Depth-1 / fanout-3 / descendants-4 refusals** — the fail-closed
   gates refuse honestly, persisting NOTHING beyond the denied result,
   with distinct error codes.
3. **Authority narrowing negatives** — the child's effective grants are
   the INTERSECTION (a request can never add); an ungranted-only request
   plans a NO-capability child; a scope-widening ``server_id`` key is
   rejected by the arguments contract; the child context carries no
   credential references and names the parent scope.
4. **Restart during delegation** — ``kill -9`` while the child's effect is
   gated mid-flight: the recovered parent replays onto the SAME child (one
   ``session_runs`` row), the effect happens exactly once, and the
   delegation reconciles to an honest ``executed`` result.
5. **AST hygiene** — the session path imports none of the forbidden legacy
   modules and no harness ``TieredCompaction``/``_compaction_capability``.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents.session_delegate import (
    MAX_CHILDREN_PER_SESSION_RUN,
    MAX_DESCENDANTS_PER_ROOT,
    SESSION_DELEGATE_TOOL_NAME,
    aplan_session_delegation,
    adescendant_count,
    bounded_child_summary_document,
    child_run_id_for,
    delegate_tool_json_schema,
    narrow_child_capabilities,
    parse_delegate_arguments,
)
from mai.agents.session_runner import (
    HANDLER_NAME as SESSION_HANDLER_NAME,
)
from mai.agents.session_runner import (
    _delegation_denial_code,
    _plan_delegation,
    parse_session_run_intent,
)
from mai.contracts.sessions import (
    MAX_TOOL_OUTPUT_PREVIEW_CHARS,
    EntryKind,
    SessionContractError,
    SessionSourceKind,
)
from mai.jobs.engine import EngineClient
from mai.policy.evaluate import ParentScope, execution_context_from_json
from mai.session_store import acreate_run, acreate_session

# Reuse the PROVEN harness pieces (the brief's mandate): the persistent
# test loop, the kill -9 / stop protocol, the wait helper, journal readers
# and fixture constants come from the shared runtime-scenario harness
# (the legacy agent-run suite was deleted at D09; extraction is verbatim).
from agent_runtime_harness import (  # noqa: F401 - harness reuse
    PRIV_CAP,
    READ_CAP,
    RUN_TIMEOUT,
    SECRET_NAME,
    SECRET_VALUE,
    SERVER_ID,
    _journal_lines,
    _kill9,
    _run,
    _stop,
    _wait_until,
)
from test_session_runner import (  # noqa: F401 - harness reuse
    _await_terminal,
    _canonical,
    _drop_app_tables,
    _intent_document,
    _make_session,
    _read_session_entries,
    _read_session_runs,
    _read_sessions_row,
    _write_data_root,
    _write_model_root,
    _write_policy_files,
)
from mai.session_store import aappend_entry

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
DELEGATE_HANDLER_MODULE_NAME = "session_delegate_scenario_handlers"

#: The suites run async tests through the anyio pytest plugin (the repo's
#: only async driver), one backend: asyncio.
pytestmark = pytest.mark.anyio

#: The delegation goal marker: the child's user turn carries it; the
#: parent's surface never does.
CHILD_MARKER = "[child-task]"


# ---------------------------------------------------------------------------
# the worker-side composition (the reviewed session-runner composition with
# the parent/child script selection)
# ---------------------------------------------------------------------------

SESSION_DELEGATE_HANDLER_MODULE_SOURCE = '''
"""Worker-side composition for the D05 delegation suite: the journaled
FixtureExecutor, the deterministic scripted FunctionModel with PARENT and
CHILD script lanes (selected by the delegation goal marker), and the D04
SessionRunExecutor composed over the SAME dispatcher."""

import json
import os
import time

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import mai.agents.session_runner as session_runner
from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
from mai.agents.model_store import ModelConfigStore
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
    scripts = json.loads(os.environ["MAI_FX_MODEL_SCRIPT"])
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
        _mark(f"history_len:{len(messages)}")
        child_lane = False
        for message in messages:
            for part in getattr(message, "parts", []):
                kind = getattr(part, "part_kind", "")
                content = str(getattr(part, "content", ""))
                if "[child-task]" in content:
                    child_lane = True
                if kind == "tool-call":
                    _mark(f"saw_tool_call:{part.tool_call_id}")
                elif kind == "tool-return":
                    _mark("saw_tool_return")
                elif kind == "text":
                    _mark(f"saw_text:{content[:40]}")
                elif kind == "user-prompt":
                    _mark(f"saw_user:{content[:40]}")
        script = scripts["child"] if child_lane else scripts["parent"]
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


import uuid  # noqa: E402 - the grants parser needs it

_build()
'''


@pytest.fixture(scope="session")
def agent_url() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from mai.jobs import engine

    url = os.environ.get("MAI_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "MAI_TEST_DATABASE_URL is not set; the delegation claims need a "
            "real disposable Postgres fixture (pgvector/pgvector:pg16)"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_app_tables(url)
        command.upgrade(AlembicConfig(str(BACKEND_ROOT / "alembic.ini")), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    engine.launch_worker(url)  # engine-owned dbos schema
    engine.shutdown()
    yield url
    engine.shutdown()
    _drop_app_tables(url)


@pytest.fixture
def factory(agent_url: str) -> async_sessionmaker[AsyncSession]:
    from mai.database import create_app_engine

    eng, sessions = create_app_engine(agent_url)

    async def _clean() -> None:
        import sqlalchemy

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
                    sqlalchemy.text("SELECT to_regclass(:t) IS NOT NULL"),
                    {"t": f"public.{table}"},
                )
                if not present.scalar():
                    continue
                await session.execute(sqlalchemy.text(f"DELETE FROM {table}"))
            await session.commit()
        # Kill-scenario hygiene: a SIGKILLed worker leaves PENDING workflows
        # in the dbos system schema; the NEXT test's worker would recover
        # them first (concurrency starvation, stale-session crashes). All
        # workers are stopped by the time this fixture teardown runs.
        import sqlalchemy

        async with sessions() as session:
            for table in (
                "dbos.workflow_events",
                "dbos.notifications",
                "dbos.operation_outputs",
                "dbos.workflow_status",
            ):
                present = await session.execute(
                    sqlalchemy.text("SELECT to_regclass(:t) IS NOT NULL"),
                    {"t": table},
                )
                if not present.scalar():
                    continue
                await session.execute(sqlalchemy.text(f"DELETE FROM {table}"))
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
        import sqlalchemy
        from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID

        async with factory() as session:
            await session.execute(
                sqlalchemy.text(
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
            await session.execute(
                sqlalchemy.text(
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
    (handlers / f"{DELEGATE_HANDLER_MODULE_NAME}.py").write_text(
        SESSION_DELEGATE_HANDLER_MODULE_SOURCE, encoding="utf-8"
    )
    return directory


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
        return {"model_root": model_root, "data_root": data_root}

    return _compose


def _spawn_worker(
    url: str, handler_dir: pathlib.Path, scenario: pathlib.Path, tag: str
) -> subprocess.Popen:
    """Launch a REAL worker process against the fixture database."""
    env = dict(os.environ)
    env.pop("MAI_TEST_DATABASE_URL", None)
    env["MAI_DATABASE_URL"] = url
    env["MAI_WORKER_HANDLERS"] = DELEGATE_HANDLER_MODULE_NAME
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
    env["MAI_FX_MODEL_SCRIPT"] = script_path.read_text(encoding="utf-8")
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


def _write_env_file(scenario: pathlib.Path) -> None:
    (scenario / "fixture.env").write_text(
        f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8"
    )
    os.chmod(scenario / "fixture.env", 0o600)


def _script(scenario: pathlib.Path, parent: list[dict], child: list[dict]) -> None:
    (scenario / "model-script.json").write_text(
        json.dumps({"parent": parent, "child": child}), encoding="utf-8"
    )


def _delegate_args(
    *,
    goal: str = f"{CHILD_MARKER} verify the fixture read",
    allowed: list[str] | None = None,
    max_rounds: int = 2,
    max_tool_calls: int = 4,
    timeout_seconds: float = 60.0,
    **extra: object,
) -> dict[str, object]:
    document: dict[str, object] = {
        "goal": goal,
        "context_refs": [],
        "artifact_refs": [],
        "allowed_capabilities": [READ_CAP] if allowed is None else list(allowed),
        "max_rounds": max_rounds,
        "max_tool_calls": max_tool_calls,
        "timeout_seconds": timeout_seconds,
    }
    document.update(extra)
    return document


def _delegate_call_entry(
    call_id: str = "del-1", **args: object
) -> dict:
    return {
        "tool_call": {
            "name": SESSION_DELEGATE_TOOL_NAME,
            "args": _delegate_args(**args),
            "id": call_id,
        }
    }


def _tool_call_entry(
    name: str = "fixture_read",
    marker: str = "m1",
    call_id: str = "c-child",
) -> dict:
    return {"tool_call": {"name": name, "args": {"marker": marker}, "id": call_id}}


def _plan_code(excinfo: pytest.ExceptionInfo[SessionContractError]) -> str:
    return str(excinfo.value)


def _make_executor(
    agent_url: str, factory: async_sessionmaker[AsyncSession], tmp: pathlib.Path
):
    """A minimal in-process SessionRunExecutor (the delegation plan path
    touches sessions + budgets only; wiring is constructor-checked)."""
    from mai.agents.model_store import ModelConfigStore
    from mai.agents.session_runner import SessionRunExecutor
    from mai.capabilities.dispatch import CapabilityDispatcher
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
                "resource_scope": [] if effect_class == "read" else ["target.exec"],
                "required_grants": [],
            },
            source=f"<{capability_id} descriptor>",
        )

    registry = CapabilityRegistry()
    registry.register(_descriptor("fixture.read", "read"))
    registry.register(_descriptor("fixture.priv", "privileged"))
    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(factory),
        grants=lambda: {READ_CAP: frozenset({uuid.UUID(SERVER_ID)})},
        policy=lambda: Policy(revision=1, denied_capabilities=frozenset()),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(str(tmp / "fixture.env")),
        executor=lambda call: None,  # never dispatched on the plan path
    )
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "fixture.env").write_text("MAI_SSH_KEY_FIXTURE=x\n", encoding="utf-8")
    os.chmod(tmp / "fixture.env", 0o600)
    return SessionRunExecutor(
        sessions=factory,
        dispatcher=dispatcher,
        registry=registry,
        model_store=ModelConfigStore(tmp / "model-root"),
        data_root=str(tmp / "data-root"),
        locks=ResourceLockService(agent_url),
        resolver=EnvResolver(str(tmp / "fixture.env")),
    )


async def _insert_budgeted_run(
    factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    budget: dict[str, object] | None = None,
) -> None:
    """Insert the facade ``job_runs`` row (+ classification), optionally
    budgeted (``root_budget`` JSON)."""
    import sqlalchemy
    from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID

    async with factory() as session:
        await session.execute(
            sqlalchemy.text(
                """
                INSERT INTO job_runs
                    (run_id, server_id, depth, profile_revision,
                     prompt_revision, model_profile, requested_grants,
                     root_budget, submitted_at, updated_at)
                VALUES
                    (:run_id, :server_id, 0, 3, 7, 'fixture-fast',
                     :requested_grants, CAST(:budget AS jsonb), now(), now())
                """
            ),
            {
                "run_id": uuid.UUID(run_id),
                "server_id": uuid.UUID(SERVER_ID),
                "requested_grants": json.dumps([READ_CAP, PRIV_CAP]),
                "budget": json.dumps(budget) if budget else None,
            },
        )
        await session.execute(
            sqlalchemy.text(
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


# ---------------------------------------------------------------------------
# R05 F1: the plan-step replay converges on the SAME child
# ---------------------------------------------------------------------------


async def test_plan_step_replay_converges_on_same_child(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    """Simulate the DBOS plan-step replay: re-run ``_plan_delegation`` for
    the SAME (session, parent, call_id) after the child rows committed but
    the step record did not. No IntegrityError, exactly one child row, the
    fan-out gate does NOT flip to denied, and the budget reservation is
    NOT double-charged."""

    executor = _make_executor(agent_url, factory, tmp_path)
    session_id = _run(_make_session(factory))
    parent_run_id = str(uuid.uuid4())
    from mai.session_store import acreate_run

    _run(
        acreate_run(
            factory,
            session_id=uuid.UUID(session_id),
            server_id=uuid.UUID(SERVER_ID),
            run_id=uuid.UUID(parent_run_id),
            model_profile="fixture-fast",
            client_request_id=uuid.uuid4(),
            parent_run_id=None,
        )
    )
    _run(
        _insert_budgeted_run(
            factory,
            parent_run_id,
            budget={
                "v": 1,
                "max_depth": 1,
                "max_children": 3,
                "max_active": 3,
                "max_steps": 100,
                "max_total_tokens": 1000000,
            },
        )
    )
    payload_json = _canonical(
        _intent_document(
            parent_run_id,
            session_id,
            allowed=(READ_CAP, PRIV_CAP),
            client_request_id=str(uuid.uuid4()),
        )
    )
    intent = parse_session_run_intent(payload_json)
    call = {
        "tool_call_id": "del-1",
        "tool_name": SESSION_DELEGATE_TOOL_NAME,
        "capability_id": "session.delegate",
        "args": _delegate_args(allowed=[READ_CAP], timeout_seconds=60.0),
        "entry_id": str(uuid.uuid4()),
        "seq": 100,
    }

    plan1 = await _plan_delegation(executor, intent, call=call, pins={})
    plan2 = await _plan_delegation(executor, intent, call=call, pins={})
    # The replay returned the SAME child (no UniqueViolation, no false
    # fanout denial — both plans completed).
    assert plan1["child_run_id"] == plan2["child_run_id"]
    child_run_id = uuid.UUID(str(plan1["child_run_id"]))

    # Exactly ONE child session_runs row under the parent.
    runs = _read_session_runs(agent_url, session_id)
    child_rows = [row for row in runs if row["run_id"] == child_run_id]
    assert len(child_rows) == 1
    with_psycopg = _run(_count_rows(agent_url,
        "SELECT COUNT(*) FROM session_runs WHERE session_id = %s AND parent_run_id = %s",
        (uuid.UUID(session_id), uuid.UUID(parent_run_id))))
    assert with_psycopg == 1

    # The budget reservation is NOT double-charged: exactly ONE reserve
    # event for the root, naming exactly this child.
    reserved = _run(_count_rows(agent_url,
        "SELECT COUNT(*) FROM job_events WHERE run_id = %s AND type = 'agent.budget_reserved'",
        (uuid.UUID(parent_run_id),)))
    assert reserved == 1
    child_json = _run(_scalar(agent_url,
        "SELECT data::text FROM job_events WHERE run_id = %s AND type = 'agent.budget_reserved'",
        (uuid.UUID(parent_run_id),)))
    assert str(child_run_id) in str(child_json)


async def _count_rows(url: str, query: str, params: tuple) -> int:
    import psycopg

    with psycopg.connect(url) as conn:
        return int(conn.execute(query, params).fetchone()[0])


async def _scalar(url: str, query: str, params: tuple) -> object:
    import psycopg

    with psycopg.connect(url) as conn:
        return conn.execute(query, params).fetchone()[0]


# ---------------------------------------------------------------------------
# R05 F2: budget overrun is an honest denied delegation result
# ---------------------------------------------------------------------------


async def test_budget_overrun_maps_to_honest_denial(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    """A budgeted root whose remaining active slots cannot fit the child
    refuses the plan with BudgetError; the mapping layer produces the
    honest machine-readable code (no child row, no reservation leak)."""
    executor = _make_executor(agent_url, factory, tmp_path)
    session_id = _run(_make_session(factory))
    parent_run_id = str(uuid.uuid4())
    _run(
        _insert_budgeted_run(
            factory,
            parent_run_id,
            budget={
                "v": 1,
                "max_depth": 1,
                "max_children": 3,
                "max_active": 1,
                "max_steps": 100,
                "max_total_tokens": 1000000,
            },
        )
    )
    from mai.agents.budgets import BudgetLedger

    ledger = BudgetLedger(factory)
    probe_child = uuid.uuid4()
    await ledger.areserve(parent_run_id, parent_run_id, [str(probe_child)])

    intent = parse_session_run_intent(
        _canonical(
            _intent_document(
                parent_run_id,
                session_id,
                allowed=(READ_CAP, PRIV_CAP),
                client_request_id=str(uuid.uuid4()),
            )
        )
    )
    call = {
        "tool_call_id": "del-1",
        "tool_name": SESSION_DELEGATE_TOOL_NAME,
        "capability_id": "session.delegate",
        "args": _delegate_args(allowed=[READ_CAP], timeout_seconds=60.0),
        "entry_id": str(uuid.uuid4()),
        "seq": 100,
    }
    from mai.agents.budgets import BudgetError

    with pytest.raises(BudgetError) as excinfo:
        await _plan_delegation(executor, intent, call=call, pins={})
    code = _delegation_denial_code(excinfo.value)
    assert code == "active_budget_exhausted"
    # NOTHING persisted: no child row for this call, and the reservation
    # stream still holds ONLY the probe (no leaked delegation slot).
    child_id = str(child_run_id_for(uuid.UUID(session_id), uuid.UUID(parent_run_id), "del-1"))
    children = _run(_count_rows(agent_url,
        "SELECT COUNT(*) FROM session_runs WHERE session_id = %s AND parent_run_id = %s",
        (uuid.UUID(session_id), uuid.UUID(parent_run_id))))
    assert children == 0
    reserved = _run(_count_rows(agent_url,
        "SELECT COUNT(*) FROM job_events WHERE run_id = %s AND type = 'agent.budget_reserved'",
        (uuid.UUID(parent_run_id),)))
    assert reserved == 1
    reserved_children = _run(_scalar(agent_url,
        "SELECT data::text FROM job_events WHERE run_id = %s AND type = 'agent.budget_reserved'",
        (uuid.UUID(parent_run_id),)))
    assert str(probe_child) in str(reserved_children)
    assert child_id not in str(reserved_children)


def test_budget_error_code_mapping() -> None:
    from mai.agents.budgets import BudgetError, BudgetErrorCode

    error = BudgetError(BudgetErrorCode.CHILD_CAP_EXCEEDS_BUDGET, "cap")
    assert _delegation_denial_code(error) == "child_cap_exceeds_budget"
    error = BudgetError(BudgetErrorCode.ACTIVE_BUDGET_EXHAUSTED, "slots")
    assert _delegation_denial_code(error) == "active_budget_exhausted"


def test_budget_overrun_delegation_is_an_honest_denied_result(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    scenario: pathlib.Path,
    composition,
) -> None:
    """A budgeted root whose remaining active slots cannot fit the child:
    the delegation step answers the model with the honest DENIED tool
    result (``active_budget_exhausted``), the parent run SUCCEEDS (no
    crash), and nothing leaks — no child row, no second reservation."""
    composition()
    _write_env_file(scenario)
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(
        scenario,
        parent=[
            _delegate_call_entry(call_id="del-1", allowed=[READ_CAP], timeout_seconds=60.0),
            {"text": "budget refusal observed"},
        ],
        child=[
            _tool_call_entry(name="fixture_read", marker="m1", call_id="c-child"),
            {"text": "child must never run here"},
        ],
    )
    session_id = _run(_make_session(factory))
    parent_run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        # A budgeted root with ONE active slot, already spent on a probe
        # reservation: the delegation's own reserve cannot fit.
        _run(
            _insert_budgeted_run(
                factory,
                parent_run_id,
                budget={
                    "v": 1,
                    "max_depth": 1,
                    "max_children": 3,
                    "max_active": 1,
                    "max_steps": 100,
                    "max_total_tokens": 1000000,
                },
            )
        )
        from mai.agents.budgets import BudgetLedger

        probe = str(uuid.uuid4())
        _run(BudgetLedger(factory).areserve(parent_run_id, parent_run_id, [probe]))
        client.enqueue_run(
            parent_run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    parent_run_id,
                    session_id,
                    allowed=(READ_CAP,),
                    client_request_id=str(uuid.uuid4()),
                )
            ),
        )
        _await_terminal(client, parent_run_id, "succeeded", RUN_TIMEOUT, "parent")
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    # The parent answered the model honestly and completed.
    entries = _read_session_entries(agent_url, session_id)
    parent_entries = [e for e in entries if e["run_id"] == uuid.UUID(parent_run_id)]
    results = [
        e
        for e in parent_entries
        if e["kind"] == "tool_result" and e["payload"]["call_id"] == "del-1"
    ]
    assert len(results) == 1
    payload = results[0]["payload"]
    assert payload["status"] == "denied"
    assert payload["execution_status"] == "failed"
    assert payload["effect_certainty"] == "certain"
    assert payload["error_code"] == "active_budget_exhausted"
    assert any(
        "budget refusal observed" in str(e["payload"].get("text", ""))
        for e in parent_entries
        if e["kind"] == "assistant_message"
    )
    # Nothing leaked: no child run row, no child facade row, no delegation
    # entry, and the reservation stream still holds ONLY the probe.
    expected_child = child_run_id_for(
        uuid.UUID(session_id), uuid.UUID(parent_run_id), "del-1"
    )
    runs = _read_session_runs(agent_url, session_id)
    assert expected_child not in {row["run_id"] for row in runs}
    assert [e for e in parent_entries if e["kind"] == "delegation"] == []
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(agent_url, row_factory=dict_row) as conn:
        facade = conn.execute(
            "SELECT COUNT(*) AS n FROM job_runs WHERE run_id = %s", (expected_child,)
        ).fetchone()
        reserves = conn.execute(
            "SELECT data::text FROM job_events WHERE run_id = %s "
            "AND type = 'agent.budget_reserved'",
            (uuid.UUID(parent_run_id),),
        ).fetchall()
    assert facade is not None and int(facade["n"]) == 0
    assert len(reserves) == 1
    assert probe in str(reserves[0]["data"])
    assert str(expected_child) not in str(reserves[0]["data"])
    # The child's fixture effect never happened.
    assert "effect:fixture.read:m1" not in _journal_lines(scenario)


# ---------------------------------------------------------------------------
# 1. real parent → child delegation (worker subprocess)
# ---------------------------------------------------------------------------


def test_parent_delegates_to_real_narrowed_child(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    composition()
    _write_env_file(scenario)
    _write_policy_files(scenario, grants=[{READ_CAP: True, PRIV_CAP: True}])
    _script(
        scenario,
        parent=[
            _delegate_call_entry(call_id="del-1", allowed=[READ_CAP], timeout_seconds=120.0),
            {"text": "parent saw the child settle"},
        ],
        child=[
            _tool_call_entry(name="fixture_read", marker="m1", call_id="c-child"),
            {"text": "child finished the delegated task"},
        ],
    )
    session_id = _run(_make_session(factory))
    parent_run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(parent_run_id, requested_grants=(READ_CAP, PRIV_CAP)))
        client.enqueue_run(
            parent_run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    parent_run_id,
                    session_id,
                    allowed=(READ_CAP, PRIV_CAP),
                    client_request_id=str(uuid.uuid4()),
                )
            ),
        )
        _await_terminal(client, parent_run_id, "succeeded", RUN_TIMEOUT, "parent")
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    # The deterministic child exists as a real subordinate run.
    expected_child = child_run_id_for(
        uuid.UUID(session_id), uuid.UUID(parent_run_id), "del-1"
    )
    runs = _read_session_runs(agent_url, session_id)
    run_ids = {row["run_id"] for row in runs}
    assert uuid.UUID(parent_run_id) in run_ids
    assert expected_child in run_ids
    child_row = next(row for row in runs if row["run_id"] == expected_child)
    assert child_row["state"] == "succeeded"

    # Exactly-once child effect: one begin, one effect in the journal.
    lines = _journal_lines(scenario)
    assert lines.count("begin:fixture.read:m1") == 1, lines
    assert lines.count("effect:fixture.read:m1") == 1, lines

    entries = _read_session_entries(agent_url, session_id)
    seqs = [entry["seq"] for entry in entries]
    assert seqs == list(range(1, len(seqs) + 1))

    # The parent's surface: exactly ONE delegation result, bounded, redacted.
    parent_entries = [e for e in entries if e["run_id"] == uuid.UUID(parent_run_id)]
    delegation_results = [
        e
        for e in parent_entries
        if e["kind"] == "tool_result" and e["payload"]["call_id"] == "del-1"
    ]
    assert len(delegation_results) == 1
    result = delegation_results[0]["payload"]
    assert result["status"] == "executed"
    assert result["execution_status"] == "succeeded"
    assert result["domain_outcome"] == "success"
    assert result["effect_certainty"] == "certain"
    preview = json.loads(str(result["output_preview"]))
    assert set(preview) == {"child_run_id", "status", "summary", "error_code"}
    assert preview["child_run_id"] == str(expected_child)
    assert preview["status"] == "succeeded"
    assert preview["error_code"] is None
    assert "child finished the delegated task" in preview["summary"]
    # The child transcript is never spliced into the parent's surface.
    parent_texts = " ".join(
        str(e["payload"].get("text", ""))
        for e in parent_entries
        if e["kind"] == "assistant_message"
    )
    assert "child finished the delegated task" not in parent_texts
    assert "parent saw the child settle" in parent_texts, parent_texts

    # The child's own surface carries the child's turn under the child run;
    # its fixture call executed cleanly there (never spliced into the
    # parent's surface).
    child_entries = [e for e in entries if e["run_id"] == expected_child]
    child_call_results = [
        e
        for e in child_entries
        if e["kind"] == "tool_result" and e["payload"]["call_id"] == "c-child"
    ]
    assert len(child_call_results) == 1
    assert child_call_results[0]["payload"]["status"] == "executed"

    # The parent's DelegationEntry settled with the child's truth.
    delegation_entries = [e for e in parent_entries if e["kind"] == "delegation"]
    assert len(delegation_entries) == 1
    delegation = delegation_entries[0]["payload"]
    assert delegation["child_run_id"] == str(expected_child)
    assert delegation["status"] == "succeeded"
    assert delegation["allowed_capabilities"] == [READ_CAP]  # narrowed
    assert "child finished the delegated task" in str(delegation["result_summary"])

    # The child's facade row carries the narrowed grants (job_runs).
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(agent_url, row_factory=dict_row) as conn:
        facade = conn.execute(
            "SELECT requested_grants, depth FROM job_runs WHERE run_id = %s",
            (expected_child,),
        ).fetchone()
    assert facade is not None
    grants_raw = facade["requested_grants"]
    if isinstance(grants_raw, str):
        assert json.loads(grants_raw) == [READ_CAP]
    else:
        assert list(grants_raw) == [READ_CAP]
    assert int(facade["depth"]) == 1

    # The compaction checkpoint ran but honestly did nothing (tiny session).
    assert [e for e in entries if e["kind"] == "compaction"] == []


# ---------------------------------------------------------------------------
# 2. fail-closed gate refusals (in-process, real store)
# ---------------------------------------------------------------------------


async def test_depth_gate_refuses_children_of_children(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await acreate_session(
        factory, server_id=uuid.UUID(SERVER_ID), source=SessionSourceKind.INTERACTIVE
    )
    sid = session_row.session_id
    server = uuid.UUID(SERVER_ID)
    root = uuid.UUID(int=0xA11)
    child = uuid.UUID(int=0xA12)
    await acreate_run(
        factory, session_id=sid, server_id=server, run_id=root,
        model_profile="default", client_request_id=uuid.UUID(int=0xB11),
        parent_run_id=None,
    )
    await acreate_run(
        factory, session_id=sid, server_id=server, run_id=child,
        model_profile="default", client_request_id=uuid.UUID(int=0xB12),
        parent_run_id=root,
    )
    with pytest.raises(SessionContractError) as excinfo:
        await aplan_session_delegation(
            factory,
            session_id=sid,
            server_id=server,
            parent_run_id=child,
            delegate_call_id="del-x",
            args=_delegate_args(),
            parent_allowed_capabilities=[READ_CAP],
            parent_mode="delegated",
            model_profile="default",
        )
    assert "delegation_depth_exhausted" in _plan_code(excinfo)


async def test_fanout_gate_refuses_the_fourth_child(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy import text as sql_text

    session_row = await acreate_session(
        factory, server_id=uuid.UUID(SERVER_ID), source=SessionSourceKind.INTERACTIVE
    )
    sid = session_row.session_id
    server = uuid.UUID(SERVER_ID)
    root = uuid.UUID(int=0xB21)
    await acreate_run(
        factory, session_id=sid, server_id=server, run_id=root,
        model_profile="default", client_request_id=uuid.UUID(int=0xC21),
        parent_run_id=None,
    )
    for n in range(MAX_CHILDREN_PER_SESSION_RUN):
        await acreate_run(
            factory, session_id=sid, server_id=server,
            run_id=uuid.UUID(int=0xB30 + n), model_profile="default",
            client_request_id=uuid.UUID(int=0xC30 + n), parent_run_id=root,
        )
    with pytest.raises(SessionContractError) as excinfo:
        await aplan_session_delegation(
            factory,
            session_id=sid,
            server_id=server,
            parent_run_id=root,
            delegate_call_id="del-y",
            args=_delegate_args(),
            parent_allowed_capabilities=[READ_CAP],
            parent_mode="delegated",
            model_profile="default",
        )
    assert "fanout_exhausted" in _plan_code(excinfo)
    # The refusal persisted NOTHING: the durable children count stayed.
    async with factory() as session:
        count = (
            await session.execute(
                sql_text("SELECT COUNT(*) FROM session_runs WHERE parent_run_id = :p"),
                {"p": root},
            )
        ).scalar_one()
    assert count == MAX_CHILDREN_PER_SESSION_RUN


async def test_descendant_tree_cap_is_enforced(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy import text as sql_text

    session_row = await acreate_session(
        factory, server_id=uuid.UUID(SERVER_ID), source=SessionSourceKind.INTERACTIVE
    )
    sid = session_row.session_id
    server = uuid.UUID(SERVER_ID)
    root = uuid.UUID(int=0xD01)
    await acreate_run(
        factory, session_id=sid, server_id=server, run_id=root,
        model_profile="default", client_request_id=uuid.UUID(int=0xE01),
        parent_run_id=None,
    )
    # Seed a root whose tree is ALREADY at the descendant cap: root → p1 →
    # (c1, c2, c3) = 4 descendants of the root. The per-run fan-out for the
    # ROOT is 1 child (< 3), so a fresh delegation from the ROOT is refused
    # specifically on the tree-wide descendant gate (gate order isolates it).
    p1 = uuid.UUID(int=0xD10)
    await acreate_run(
        factory, session_id=sid, server_id=server, run_id=p1,
        model_profile="default", client_request_id=uuid.UUID(int=0xE10),
        parent_run_id=root,
    )
    for k in range(3):
        await acreate_run(
            factory, session_id=sid, server_id=server,
            run_id=uuid.UUID(int=0xD20 + k),
            model_profile="default",
            client_request_id=uuid.UUID(int=0xE20 + k),
            parent_run_id=p1,
        )
    async with factory() as session:
        total = (
            await session.execute(
                sql_text(
                    """
                    WITH RECURSIVE tree(run_id) AS (
                        SELECT run_id FROM session_runs
                        WHERE session_id = :sid AND parent_run_id = :rid
                        UNION
                        SELECT c.run_id FROM session_runs c
                        JOIN tree t ON c.parent_run_id = t.run_id
                    )
                    SELECT COUNT(*) FROM tree
                    """
                ),
                {"sid": sid, "rid": root},
            )
        ).scalar_one()
    assert total == MAX_DESCENDANTS_PER_ROOT
    assert await adescendant_count(factory, sid, root) == MAX_DESCENDANTS_PER_ROOT
    # A further delegation under this root is refused on the descendant
    # gate before any new identity is generated (the fan-out gate passes:
    # the root has 1 child < 3).
    with pytest.raises(SessionContractError) as excinfo:
        await aplan_session_delegation(
            factory,
            session_id=sid,
            server_id=server,
            parent_run_id=root,
            delegate_call_id="del-t",
            args=_delegate_args(),
            parent_allowed_capabilities=[READ_CAP],
            parent_mode="delegated",
            model_profile="default",
        )
    assert "descendant_budget_exhausted" in _plan_code(excinfo)


async def test_grant_intersection_never_widens_authority() -> None:
    # The intersection is the parent's set ∩ the request; order preserved.
    assert narrow_child_capabilities(
        ["fixture.read", "fixture.priv"], ["fixture.priv", "host.shell"]
    ) == ("fixture.priv",)
    assert narrow_child_capabilities(["fixture.read"], []) == ()
    assert narrow_child_capabilities([], ["fixture.read"]) == ()
    # A scope-widening key (e.g. server_id) is rejected by the contract.
    with pytest.raises(SessionContractError):
        parse_delegate_arguments(_delegate_args(server_id=str(uuid.uuid4())))
    with pytest.raises(SessionContractError):
        parse_delegate_arguments({"goal": "x"})
    schema = delegate_tool_json_schema()
    assert "server_id" not in schema["properties"]  # type: ignore[operator]


async def test_ungranted_only_request_plans_a_no_capability_child(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await acreate_session(
        factory, server_id=uuid.UUID(SERVER_ID), source=SessionSourceKind.INTERACTIVE
    )
    sid = session_row.session_id
    server = uuid.UUID(SERVER_ID)
    root = uuid.UUID(int=0xF01)
    await acreate_run(
        factory, session_id=sid, server_id=server, run_id=root,
        model_profile="default", client_request_id=uuid.UUID(int=0xF02),
        parent_run_id=None,
    )
    plan = await aplan_session_delegation(
        factory,
        session_id=sid,
        server_id=server,
        parent_run_id=root,
        delegate_call_id="del-z",
        args=_delegate_args(allowed=["host.shell"]),
        parent_allowed_capabilities=[READ_CAP],
        parent_mode="delegated",
        model_profile="default",
    )
    # The child would start with NO capabilities: fail-closed narrowing.
    assert plan["narrowed_capabilities"] == []
    parsed = execution_context_from_json(plan["child_payload"]["context"])
    assert parsed.allowed_capabilities == ()
    assert parsed.credential_references == ()
    assert parsed.parent_scope == ParentScope(run_id=root, server_id=server)
    assert parsed.server_id == server  # scope inherited, never reassigned


# ---------------------------------------------------------------------------
# 3. restart during delegation: same child, exactly-once effect
# ---------------------------------------------------------------------------


def test_kill9_during_child_effect_recovers_same_child(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    composition()
    _write_env_file(scenario)
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    (scenario / "behavior.json").write_text(
        json.dumps({READ_CAP: "gate"}), encoding="utf-8"
    )
    _script(
        scenario,
        parent=[
            _delegate_call_entry(call_id="del-1", timeout_seconds=90.0),
            {"text": "parent recovered"},
        ],
        child=[
            _tool_call_entry(name="fixture_read", marker="m1", call_id="c-child"),
            {"text": "child finished after restart"},
        ],
    )
    session_id = _run(_make_session(factory))
    parent_run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker1: subprocess.Popen | None = None
    worker2: subprocess.Popen | None = None
    try:
        worker1 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(parent_run_id, requested_grants=(READ_CAP,)))
        client.enqueue_run(
            parent_run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    parent_run_id,
                    session_id,
                    allowed=(READ_CAP,),
                    client_request_id=str(uuid.uuid4()),
                )
            ),
        )
        # The child's fixture effect is in-flight (gated) while the parent
        # is parked on the delegation wait.
        _wait_until(
            lambda: "begin:fixture.read:m1" in _journal_lines(scenario),
            60.0,
            "child effect gated",
        )
        _kill9(worker1)
        worker1 = None
        # Release the gate: the recovered worker completes the child.
        gate = scenario / "gates" / "gate-fixture.read-m1"
        gate.write_text("go", encoding="utf-8")
        worker2 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w2")
        _await_terminal(client, parent_run_id, "succeeded", RUN_TIMEOUT, "parent")
    finally:
        for worker in (worker1, worker2):
            if worker is not None:
                _stop(worker)
        client.close()

    expected_child = child_run_id_for(
        uuid.UUID(session_id), uuid.UUID(parent_run_id), "del-1"
    )
    runs = _read_session_runs(agent_url, session_id)
    child_rows = [row for row in runs if row["run_id"] == expected_child]
    assert len(child_rows) == 1  # never a second admission
    assert child_rows[0]["state"] == "succeeded"
    # Exactly-once effect across the kill: the gated effect was killed
    # MID-EFFECT, so the crash-window fence NEVER re-dispatches it — one
    # begin, zero duplicate effects (the honest unproven truth).
    lines = _journal_lines(scenario)
    assert lines.count("begin:fixture.read:m1") == 1, lines
    assert lines.count("effect:fixture.read:m1") == 0, lines
    # The parent settled honestly: the child's truth surfaced once.
    entries = _read_session_entries(agent_url, session_id)
    parent_entries = [e for e in entries if e["run_id"] == uuid.UUID(parent_run_id)]
    results = [
        e
        for e in parent_entries
        if e["kind"] == "tool_result" and e["payload"]["call_id"] == "del-1"
    ]
    assert len(results) == 1
    assert results[0]["payload"]["status"] == "executed"
    preview = json.loads(str(results[0]["payload"]["output_preview"]))
    assert preview["status"] == "succeeded"
    assert preview["child_run_id"] == str(expected_child)
    delegation_entries = [e for e in parent_entries if e["kind"] == "delegation"]
    assert len(delegation_entries) == 1
    assert delegation_entries[0]["payload"]["status"] == "succeeded"


def test_kill9_during_child_model_round_effect_exactly_once(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """Kill the worker while the CHILD's first model round is in flight
    (before the effect): the uncheckpointed round safely re-runs on the
    recovered worker and the child's effect happens EXACTLY ONCE; the
    parent replays onto the SAME deterministic child."""
    composition()
    _write_env_file(scenario)
    _write_policy_files(scenario, grants=[{READ_CAP: True}])
    _script(
        scenario,
        parent=[
            _delegate_call_entry(call_id="del-1", timeout_seconds=90.0),
            {"text": "parent saw the child settle"},
        ],
        child=[
            _tool_call_entry(name="fixture_read", marker="m1", call_id="c-child"),
            {"text": "child finished after restart"},
        ],
    )
    session_id = _run(_make_session(factory))
    parent_run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker1: subprocess.Popen | None = None
    worker2: subprocess.Popen | None = None
    try:
        worker1 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _run(insert_run(parent_run_id, requested_grants=(READ_CAP,)))
        client.enqueue_run(
            parent_run_id,
            SESSION_HANDLER_NAME,
            _canonical(
                _intent_document(
                    parent_run_id,
                    session_id,
                    allowed=(READ_CAP,),
                    client_request_id=str(uuid.uuid4()),
                )
            ),
        )
        # Kill as soon as the child's first model round is visible. Where
        # exactly the kill lands, the outcome converges: an uncheckpointed
        # round safely re-runs; a checkpointed tool step repairs from its
        # receipt without re-dispatch — the effect happens EXACTLY ONCE.
        _wait_until(
            lambda: any(
                line.startswith("saw_user:[child-task]")
                for line in _journal_lines(scenario)
            ),
            60.0,
            "child model round in flight",
        )
        _kill9(worker1)
        worker1 = None
        worker2 = _spawn_worker(agent_url, scenario / "handlers", scenario, "w2")
        _await_terminal(client, parent_run_id, "succeeded", RUN_TIMEOUT, "parent")
    finally:
        for worker in (worker1, worker2):
            if worker is not None:
                _stop(worker)
        client.close()

    expected_child = child_run_id_for(
        uuid.UUID(session_id), uuid.UUID(parent_run_id), "del-1"
    )
    runs = _read_session_runs(agent_url, session_id)
    child_rows = [row for row in runs if row["run_id"] == expected_child]
    assert len(child_rows) == 1  # never a second admission
    assert child_rows[0]["state"] == "succeeded"
    # EXACTLY ONCE across the restart: one begin, one effect.
    lines = _journal_lines(scenario)
    assert lines.count("begin:fixture.read:m1") == 1, lines
    assert lines.count("effect:fixture.read:m1") == 1, lines
    entries = _read_session_entries(agent_url, session_id)
    child_entries = [e for e in entries if e["run_id"] == expected_child]
    child_call_results = [
        e
        for e in child_entries
        if e["kind"] == "tool_result" and e["payload"]["call_id"] == "c-child"
    ]
    assert len(child_call_results) == 1
    # Two honest convergence branches (kill-timing dependent): a kill
    # before the tool-step receipt commit re-runs the round fresh
    # ("executed"); a kill after it repairs from the receipt with
    # uncertain effect certainty ("unknown_effect"). Exactly-once
    # (begin/effect == 1 above) holds in either branch.
    assert child_call_results[0]["payload"]["status"] in (
        "executed",
        "unknown_effect",
    )
    parent_entries = [e for e in entries if e["run_id"] == uuid.UUID(parent_run_id)]
    results = [
        e
        for e in parent_entries
        if e["kind"] == "tool_result" and e["payload"]["call_id"] == "del-1"
    ]
    assert len(results) == 1
    assert results[0]["payload"]["status"] == "executed"
    preview = json.loads(str(results[0]["payload"]["output_preview"]))
    assert preview["status"] == "succeeded"
    assert preview["child_run_id"] == str(expected_child)
    delegation_entries = [e for e in parent_entries if e["kind"] == "delegation"]
    assert len(delegation_entries) == 1
    assert delegation_entries[0]["payload"]["status"] == "succeeded"


# ---------------------------------------------------------------------------
# 4. delegate tool surface bounds (pure)
# ---------------------------------------------------------------------------


def test_delegate_arguments_bounds_fail_closed() -> None:
    with pytest.raises(SessionContractError):
        parse_delegate_arguments(_delegate_args(goal=""))
    with pytest.raises(SessionContractError):
        parse_delegate_arguments(_delegate_args(max_rounds=99))
    with pytest.raises(SessionContractError):
        parse_delegate_arguments(_delegate_args(timeout_seconds=0))
    with pytest.raises(SessionContractError):
        parse_delegate_arguments(_delegate_args(timeout_seconds=10**9))
    parsed = parse_delegate_arguments(_delegate_args())
    assert parsed["allowed_capabilities"] == [READ_CAP]
    schema = delegate_tool_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(_delegate_args())


# ---------------------------------------------------------------------------
# 4b. bounded child summary document (pure): JSON preview-budget guarantee
# ---------------------------------------------------------------------------


def test_bounded_child_summary_truncates_within_json_preview_budget() -> None:
    """A >5000-char Cyrillic summary with newlines and quotes serializes to
    a JSON document that ALWAYS fits ``MAX_TOOL_OUTPUT_PREVIEW_CHARS`` and
    parses back with all four keys — the runner's
    ``preview[:MAX_TOOL_OUTPUT_PREVIEW_CHARS]`` slice can never cut the
    document mid-string."""
    child_run_id = uuid.uuid5(uuid.NAMESPACE_URL, "dwf-s1-child")
    body = 'Резюме подзадачи: "шаг выполнен"\nИтог: "готово".\n' * 120
    assert len(body) > 5000
    document = bounded_child_summary_document(
        child_run_id=child_run_id,
        child_status="succeeded",
        summary_text=body,
        error_code=None,
    )
    serialized = json.dumps(document, ensure_ascii=False)
    assert len(serialized) <= MAX_TOOL_OUTPUT_PREVIEW_CHARS
    parsed = json.loads(serialized)
    assert set(parsed) == {"child_run_id", "status", "summary", "error_code"}
    assert parsed["child_run_id"] == str(child_run_id)
    assert parsed["status"] == "succeeded"
    assert parsed["error_code"] is None
    # Explicit, honest truncation marker at the END of the kept prefix.
    assert re.search(r"\[truncated: \d+ chars omitted\]$", parsed["summary"])
    assert parsed["summary"].startswith("Резюме подзадачи")


def test_bounded_child_summary_short_summary_is_untouched() -> None:
    """A short summary passes through untruncated (no marker) and still
    round-trips within the preview budget."""
    child_run_id = uuid.uuid5(uuid.NAMESPACE_URL, "dwf-s1-short")
    summary_text = "Готово: сервис перезапущен.\nПроверка пройдена."
    document = bounded_child_summary_document(
        child_run_id=child_run_id,
        child_status="failed",
        summary_text=summary_text,
        error_code="child_failed",
    )
    assert document["summary"] == summary_text
    assert "truncated" not in document["summary"]
    serialized = json.dumps(document, ensure_ascii=False)
    assert len(serialized) <= MAX_TOOL_OUTPUT_PREVIEW_CHARS
    assert json.loads(serialized) == document


def test_bounded_child_summary_without_summary_text_round_trips() -> None:
    """A missing child summary yields an empty ``summary`` field and the
    document still serializes within the preview budget."""
    child_run_id = uuid.uuid5(uuid.NAMESPACE_URL, "dwf-s1-none")
    document = bounded_child_summary_document(
        child_run_id=child_run_id,
        child_status="timeout",
        summary_text=None,
        error_code="delegation_timeout",
    )
    assert document == {
        "child_run_id": str(child_run_id),
        "status": "timeout",
        "summary": "",
        "error_code": "delegation_timeout",
    }
    assert (
        len(json.dumps(document, ensure_ascii=False))
        <= MAX_TOOL_OUTPUT_PREVIEW_CHARS
    )


# ---------------------------------------------------------------------------
# 4c. delegation lifecycle isolation (real worker scenarios; DWF-S2)
# ---------------------------------------------------------------------------


def _compose_delegation_scenario(
    factory: async_sessionmaker[AsyncSession],
    scenario: pathlib.Path,
    composition,
    *,
    parent_script: list[dict],
    child_script: list[dict],
) -> str:
    """Write the scenario files + session row; the test owns the worker."""
    composition()
    _write_env_file(scenario)
    _write_policy_files(scenario, grants=[{READ_CAP: True, PRIV_CAP: True}])
    _script(scenario, parent=parent_script, child=child_script)
    return _run(_make_session(factory))


def _enqueue_parent_run(
    client: EngineClient,
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    session_id: str,
    parent_run_id: str,
) -> None:
    """Insert the facade row and start the parent session workflow."""
    _run(insert_run(parent_run_id, requested_grants=(READ_CAP, PRIV_CAP)))
    client.enqueue_run(
        parent_run_id,
        SESSION_HANDLER_NAME,
        _canonical(
            _intent_document(
                parent_run_id,
                session_id,
                allowed=(READ_CAP, PRIV_CAP),
                client_request_id=str(uuid.uuid4()),
            )
        ),
    )


def test_parent_waiting_child_lifecycle_and_child_isolation(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """The parent records ``running -> waiting_child -> running ->
    succeeded`` in chronological seq order (per-call source suffixes), the
    child run's entries are ALL namespaced under ``child:``, and a settled
    child never flips the shared ``sessions.status`` (the parent's final
    model round is gated AFTER the child settled: the header still reads
    ``running`` there — the pre-fix settlement wrote ``idle``)."""
    # The PARENT's final text round is gated: the child has fully settled
    # when the gate is observed, so the header check sees the exact
    # isolation window.
    parent_script = [
        _delegate_call_entry(
            call_id="del-1", allowed=[READ_CAP], timeout_seconds=120.0
        ),
        {"gate": "parent-final-gate", "text": "parent saw the child settle"},
    ]
    child_script = [
        _tool_call_entry(name="fixture_read", marker="m1", call_id="c-child"),
        {"text": "child finished the delegated task"},
    ]
    session_id = _compose_delegation_scenario(
        factory,
        scenario,
        composition,
        parent_script=parent_script,
        child_script=child_script,
    )
    parent_run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker: subprocess.Popen | None = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _enqueue_parent_run(
            client, agent_url, factory, insert_run, session_id, parent_run_id
        )
        # Parent round 0 + child rounds 0 and 1 ran; the parent's final
        # (gated) round is in flight — the child is FULLY settled here.
        _wait_until(
            lambda: _journal_lines(scenario).count("model_call") >= 4,
            RUN_TIMEOUT,
            "parent's gated final round",
        )

        # (b) child settlement did NOT flip the shared session header.
        header = _read_sessions_row(agent_url, session_id)
        assert header is not None and header["status"] == "running"

        entries = _read_session_entries(agent_url, session_id)
        parent_uuid = uuid.UUID(parent_run_id)
        expected_child = child_run_id_for(
            uuid.UUID(session_id), parent_uuid, "del-1"
        )

        # (a) parent lifecycle transitions, in seq order, keyed by the
        # delegation call id so the resumed running is a DISTINCT entry.
        parent_states = [
            (str(e["payload"]["state"]), str(e["durable_source_id"]))
            for e in entries
            if e["run_id"] == parent_uuid and e["kind"] == "run_state"
        ]
        assert parent_states == [
            ("running", "run_state:running"),
            ("waiting_child", "run_state:waiting_child:del-1"),
            ("running", "run_state:running:del-1"),
        ]

        # (c-prefix) every child-run entry is namespaced under ``child:``.
        child_sources = [
            str(e["durable_source_id"])
            for e in entries
            if e["run_id"] == expected_child
        ]
        assert child_sources, "child run appended its own entries"
        assert all(src.startswith("child:") for src in child_sources)

        # The delegation card already carries the child's terminal truth
        # (the child settled before the parent's gated final round).
        delegation = next(
            e
            for e in entries
            if e["run_id"] == parent_uuid and e["kind"] == "delegation"
        )
        assert delegation["payload"]["status"] == "succeeded"

        # Release the parent: the run settles succeeded and the header
        # goes idle THROUGH THE PARENT's own settlement.
        (scenario / "gates" / "parent-final-gate").write_text(
            "go", encoding="utf-8"
        )
        _await_terminal(client, parent_run_id, "succeeded", RUN_TIMEOUT, "parent")
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    entries = _read_session_entries(agent_url, session_id)
    parent_uuid = uuid.UUID(parent_run_id)
    parent_states = [
        (str(e["payload"]["state"]), str(e["durable_source_id"]))
        for e in entries
        if e["run_id"] == parent_uuid and e["kind"] == "run_state"
    ]
    assert parent_states == [
        ("running", "run_state:running"),
        ("waiting_child", "run_state:waiting_child:del-1"),
        ("running", "run_state:running:del-1"),
        ("succeeded", "run_state:succeeded"),
    ]
    expected_child = child_run_id_for(uuid.UUID(session_id), parent_uuid, "del-1")
    runs = _read_session_runs(agent_url, session_id)
    child_row = next(row for row in runs if row["run_id"] == expected_child)
    assert child_row["state"] == "succeeded"
    header = _read_sessions_row(agent_url, session_id)
    assert header is not None and header["status"] == "idle"
    delegation = next(
        e
        for e in entries
        if e["run_id"] == parent_uuid and e["kind"] == "delegation"
    )
    assert delegation["payload"]["status"] == "succeeded"
    # Exactly-once child effect; the parent's own tool never ran.
    lines = _journal_lines(scenario)
    assert lines.count("begin:fixture.read:m1") == 1, lines
    assert lines.count("effect:fixture.read:m1") == 1, lines
    assert "begin:fixture.read:m2" not in lines


def test_paused_delegation_run_marks_pending_group_unresolved(
    agent_url: str,
    factory: async_sessionmaker[AsyncSession],
    insert_run,
    scenario: pathlib.Path,
    composition,
) -> None:
    """A delegation round that aborts at the safe boundary (pause drained
    before the SECOND tool) never strands its assistant entry in
    ``tool_group_state == 'pending'``: settlement marks it ``unresolved``."""
    # The parent's SECOND tool response parks on the MODEL gate: the pause
    # control is injected while that model round is in flight (the child
    # has already settled), so it drains at the per-call safe boundary
    # AFTER the pending assistant entry exists and BEFORE the second tool
    # dispatches.
    parent_script = [
        _delegate_call_entry(
            call_id="del-1", allowed=[READ_CAP], timeout_seconds=120.0
        ),
        {
            "gate": "p2-gate",
            "then": {
                "tool_call": {
                    "name": "fixture_read",
                    "args": {"marker": "m2"},
                    "id": "c-p2",
                }
            },
        },
        {"text": "parent done"},
    ]
    child_script = [
        _tool_call_entry(name="fixture_read", marker="m1", call_id="c-child"),
        {"text": "child finished the delegated task"},
    ]
    session_id = _compose_delegation_scenario(
        factory,
        scenario,
        composition,
        parent_script=parent_script,
        child_script=child_script,
    )
    parent_run_id = str(uuid.uuid4())
    client = EngineClient(agent_url)
    worker: subprocess.Popen | None = None
    try:
        worker = _spawn_worker(agent_url, scenario / "handlers", scenario, "w1")
        _enqueue_parent_run(
            client, agent_url, factory, insert_run, session_id, parent_run_id
        )
        # Parent round 0 (delegate) + both child rounds ran; the parent's
        # gated second-round response is in flight (4 model calls total).
        _wait_until(
            lambda: _journal_lines(scenario).count("model_call") >= 4,
            RUN_TIMEOUT,
            "parent's gated second round",
        )
        # Pause now: it will drain at the boundary BEFORE the second tool.
        _run(
            aappend_entry(
                factory,
                session_id=uuid.UUID(session_id),
                server_id=uuid.UUID(SERVER_ID),
                kind=EntryKind.CONTROL,
                payload={
                    "action": "pause",
                    "client_request_id": str(uuid.uuid4()),
                    "target_run_id": parent_run_id,
                },
                run_id=uuid.UUID(parent_run_id),
                durable_source_id=f"ctl-pause-{uuid.uuid4()}",
            )
        )
        (scenario / "gates" / "p2-gate").write_text("go", encoding="utf-8")
        # A pause boundary-stop lands the DOMAIN truth (facade/session/
        # header cancelled); the engine classifies the stopped workflow
        # ERROR (the reviewed pause semantics — the harness pause test
        # asserts the same domain postconditions, never engine state).
        _wait_until(
            lambda: (
                (_read_sessions_row(agent_url, session_id) or {}).get("status")
                == "cancelled"
            )
            and any(
                e["kind"] == "run_state"
                and str(e["payload"]["state"]) == "cancelled"
                for e in _read_session_entries(agent_url, session_id)
                if e["run_id"] == uuid.UUID(parent_run_id)
            ),
            RUN_TIMEOUT,
            "the paused delegation settlement",
        )
    finally:
        if worker is not None:
            _stop(worker)
        client.close()

    lines = _journal_lines(scenario)
    # The pause drained BEFORE the second tool ever dispatched.
    assert "begin:fixture.read:m2" not in lines, lines
    # The child itself settled honestly (its truth is in the parent log).
    assert lines.count("effect:fixture.read:m1") == 1, lines

    entries = _read_session_entries(agent_url, session_id)
    parent_uuid = uuid.UUID(parent_run_id)
    parent_states = [
        str(e["payload"]["state"])
        for e in entries
        if e["run_id"] == parent_uuid and e["kind"] == "run_state"
    ]
    assert parent_states == [
        "running",
        "waiting_child",
        "running",
        "cancelled",
    ]

    assistants = [
        e
        for e in entries
        if e["run_id"] == parent_uuid and e["kind"] == "assistant_message"
    ]
    group_states: dict[str, str] = {}
    for e in assistants:
        state = str(e["payload"]["tool_group_state"])
        for call in e["payload"].get("tool_calls", []) or []:
            group_states[str(call["call_id"])] = state
    # NO stranded pending group: the aborted fixture_read round resolved
    # honestly to ``unresolved`` at settlement.
    assert group_states.get("c-p2") == "unresolved", group_states
    assert all(
        str(e["payload"]["tool_group_state"]) != "pending" for e in assistants
    ), group_states

    # The delegation card kept the child's truth; the run is cancelled.
    delegation = next(
        e for e in entries if e["run_id"] == parent_uuid and e["kind"] == "delegation"
    )
    assert delegation["payload"]["status"] == "succeeded"
    header = _read_sessions_row(agent_url, session_id)
    assert header is not None and header["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 5. AST hygiene: forbidden imports stay out of the session path
# ---------------------------------------------------------------------------


def test_session_modules_import_no_forbidden_modules() -> None:
    forbidden_modules = {
        "mai.contracts.conversations",
        "mai.contracts.tasks",
        "mai.contracts.objectives",
        "mai.agents.conversation_store",
        "mai.agents.task_store",
        "mai.agents.objective_store",
        "mai.agents.journal_store",
        "mai.agents.intake",
        "pydantic_ai_harness",
    }
    targets = [
        BACKEND_ROOT / "src" / "mai" / "agents" / "session_delegate.py",
        BACKEND_ROOT / "src" / "mai" / "agents" / "session_compaction.py",
        BACKEND_ROOT / "src" / "mai" / "agents" / "session_runner.py",
    ]
    for path in targets:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        hits = sorted(imported & forbidden_modules)
        assert hits == [], f"{path.name} imports forbidden modules: {hits}"
        assert "_compaction_capability" not in source, path.name
        assert "TieredCompaction" not in source, path.name
