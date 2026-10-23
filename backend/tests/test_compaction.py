"""Conversation compaction in the durable agent run loop (compaction card).

Acceptance:

1. The per-round agent carries the harness ``TieredCompaction``
   capability with the reviewed tier configuration (cheap-to-expensive:
   clamp oversized parts, blank older tool results, LLM summarize), so a
   conversation that outgrows ``target_fraction`` of the bound profile's
   declared context window is compacted INSIDE the model request — the
   compacted history is what the next round's checkpointed
   ``history_json`` holds (no separate durable step).
2. Machina's system prompt — the leading ``SystemPromptPart`` of the
   seeded history — survives every tier verbatim.
3. Tool-call/tool-return pairing is never broken by compaction.
4. ``MAX_HISTORY_BYTES`` stays the honest LAST resort AFTER compaction.

Boundary discipline (same as ``test_agent_run.py``): the durable claim
runs against a real disposable PostgreSQL fixture with the real engine
adapter and a REAL worker subprocess; the capability executor is a
journaled fixture; the model is a deterministic FunctionModel fake whose
journal records what the model actually SAW each call (message count,
serialized history bytes, head-of-history kind, pairing integrity). Only
HTTP-free fakes — no provider is contacted.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

# The disposable-database fixtures (session migration, engine schema,
# per-test table cleanup, job_runs row) are the reviewed 06-agent-run
# machinery — reuse them, not a second convention.
from test_agent_run import _run, agent_url, factory, insert_run  # noqa: F401

from mai.agents import run as agent_run
from mai.agents.model_store import ModelConfigStore
from mai.agents.models import SCHEMA_VERSION as MODEL_SCHEMA_VERSION
from mai.contracts.server import SCHEMA_VERSION as SERVER_SCHEMA_VERSION
from mai.jobs.engine import EngineClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]

SERVER_ID = "6a91c2e4-1b3d-4f5a-8c7e-2d3b4a5f6e70"
READ_CAP = "fixture.read"
HANDLER_MODULE_NAME = "compaction_scenario_handlers"

SECRET_NAME = "MAI_FXT_TOKEN"
SECRET_VALUE = "fxt-synthetic-token-value-compaction"

HOST_KEY = "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"

#: Sentinel carried by the administrator prompt — the surviving system
#: prompt is asserted by content, never by position alone.
SYSTEM_SENTINEL = "COMPACTION-FIXTURE-SYSTEM-4d81ee"
PROMPT = f"You administer the compaction fixture host. {SYSTEM_SENTINEL}\n"
ROLE = "operator assistant"
TASK = "Collect the fixture markers and report."

RUN_TIMEOUT = 120.0
#: Fixture profile window: sized so the FIRST tier trigger lands at ~6
#: chunky tool returns — the clearing tier then blanks 3 pairs at once
#: (a deterministic byte drop), after which the rolling window blanks
#: one return per round.
CONTEXT_LIMIT = 8000
OUTPUT_LIMIT = 512

#: Chunky fake capability stdout per tool call (approx. 1k heuristic
#: tokens each): the payload the zero-LLM clearing tier reclaims.
TOOL_STDOUT_CHARS = 4000

TOOL_ROUNDS = 8


# ---------------------------------------------------------------------------
# offline: the reviewed tier configuration
# ---------------------------------------------------------------------------


class TestCompactionWiring:
    def test_tier_configuration_is_the_reviewed_one(self) -> None:
        capability = agent_run._compaction_capability(CONTEXT_LIMIT)
        assert capability.target_fraction == agent_run.COMPACTION_TARGET_FRACTION
        assert capability.context_window == CONTEXT_LIMIT
        clamp, clear, summarize = capability.tiers
        assert type(clamp).__name__ == "ClampOversizedMessages"
        assert clamp.max_part_tokens == agent_run.COMPACTION_CLAMP_MAX_PART_TOKENS
        assert type(clear).__name__ == "ClearToolResults"
        assert clear.keep_pairs == agent_run.COMPACTION_KEEP_TOOL_PAIRS
        assert type(summarize).__name__ == "SummarizingCompaction"
        assert summarize.keep_messages == agent_run.COMPACTION_SUMMARY_KEEP_MESSAGES
        assert summarize.model is None, "summarizer must inherit the run's model"

    def test_undeclared_profile_window_falls_back_to_registry(self) -> None:
        capability = agent_run._compaction_capability(None)
        assert capability.context_window is None


# ---------------------------------------------------------------------------
# the worker-side composition (journaled fake model + chunky executor)
# ---------------------------------------------------------------------------

HANDLER_MODULE_SOURCE = '''
"""Worker-side composition for the compaction scenario: real dispatcher,
real bridge, deterministic FunctionModel fake that JOURNALS what the
model sees each call, and a read capability whose stdout payload is
chunky enough to trip the tiered-compaction budget."""

import json
import os
import time
import uuid

from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelResponse,
    RequestUsage,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import FunctionModel
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

SYSTEM_SENTINEL = "COMPACTION-FIXTURE-SYSTEM-4d81ee"
TOOL_STDOUT_CHARS = 4000


def _descriptor():
    return parse_descriptor(
        {
            "identifier": "fixture.read",
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


class ChunkyExecutor:
    """Journaled read executor whose stdout payload is the compaction
    ballast: every dispatched marker is journaled (exactly-once is
    observable) and every success carries the chunky stdout."""

    def __init__(self):
        self.journal = os.environ["MAI_FX_JOURNAL"]

    def _mark(self, marker):
        with open(self.journal, "a", encoding="utf-8") as handle:
            handle.write(marker + "\\n")

    async def __call__(self, call):
        marker = call.payload.get("marker", "?")
        self._mark(f"begin:fixture.read:{marker}")
        self._mark(f"effect:fixture.read:{marker}")
        document = {
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "x" * TOOL_STDOUT_CHARS,
            "stderr": "",
        }
        return ExecutorResult(
            ok=True, output=json.dumps(document, sort_keys=True)
        )


def _script_model():
    script = json.loads(os.environ["MAI_FX_MODEL_SCRIPT"])
    journal = os.environ["MAI_FX_JOURNAL"]

    def _mark(line):
        with open(journal, "a", encoding="utf-8") as handle:
            handle.write(line + "\\n")

    def _pairing(messages):
        """(every tool call answered, no orphan returns) over the whole
        history the model is about to see."""
        called = {}
        returned = set()
        for message in messages:
            parts = getattr(message, "parts", ())
            if getattr(message, "kind", "") == "response":
                for part in parts:
                    if getattr(part, "part_kind", "") == "tool-call":
                        called[part.tool_call_id] = part.tool_name
            else:
                for part in parts:
                    if getattr(part, "part_kind", "") == "tool-return":
                        returned.add(part.tool_call_id)
        orphans = len(returned - set(called))
        unanswered = len(set(called) - returned)
        return orphans, unanswered

    def _head_is_system(messages):
        if not messages:
            return False
        first = messages[0]
        parts = getattr(first, "parts", ())
        return bool(parts) and (
            isinstance(parts[0], SystemPromptPart)
            and SYSTEM_SENTINEL in parts[0].content
        )

    def _is_summary_request(messages):
        # The summarizer tier's nested agent carries the harness's own
        # summarization instructions as its leading system prompt — never
        # the fixture conversation seed.
        first = messages[0] if messages else None
        parts = getattr(first, "parts", ()) if first is not None else ()
        return bool(parts) and isinstance(parts[0], SystemPromptPart) and (
            "context summarization assistant" in parts[0].content
        )

    def _model_fn(messages, info):
        # Provider-style usage: a real gateway reports input_tokens for
        # the WHOLE request it received; the compaction estimator anchors
        # on exactly that, so the fake reports its actual input size
        # (the serialized history it just saw). Without this the harness
        # anchors on pydantic-ai's synthetic per-node usage and the
        # fixture could never cross the tier budget honestly.
        serialized = ModelMessagesTypeAdapter.dump_json(messages)
        usage = RequestUsage(input_tokens=len(serialized) // 4, output_tokens=8)
        if _is_summary_request(messages):
            _mark("summarizer_call")
            return ModelResponse(
                parts=[TextPart(content="fixture summary")], usage=usage
            )
        _mark("model_call")
        orphans, unanswered = _pairing(messages)
        _mark(
            "seen:%d:%d:%d:%d:%d"
            % (
                len(messages),
                len(serialized),
                1 if _head_is_system(messages) else 0,
                orphans,
                unanswered,
            )
        )
        # Replay-safe script position from the durable conversation
        # length (the checkpointed history is the round identity).
        index = (len(messages) - 1) // 2
        if index >= len(script):
            raise AssertionError("model script exhausted")
        entry = script[index]
        if "tool_call" in entry:
            call = entry["tool_call"]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=call["name"],
                        args=call.get("args", {"marker": "m1"}),
                        tool_call_id=call.get("id", "c1"),
                    )
                ],
                usage=usage,
            )
        return ModelResponse(
            parts=[TextPart(content=entry.get("text", "done"))], usage=usage
        )

    return FunctionModel(_model_fn)


def _build():
    url = os.environ["MAI_DATABASE_URL"]
    engine_async = create_async_engine(url, poolclass=NullPool)
    sessions = async_sessionmaker(engine_async, expire_on_commit=False)
    registry = CapabilityRegistry()
    registry.register(_descriptor())

    def _grants_map():
        document = json.loads(open(os.environ["MAI_FX_GRANTS"], encoding="utf-8").read())
        mapping = {}
        for entry in document:
            mapping[entry["capability_id"]] = frozenset(
                uuid.UUID(server) for server in entry["server_ids"]
            )
        return mapping
    grants_map = _grants_map()

    async def executor(call):
        return await ChunkyExecutor()(call)

    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(sessions),
        grants=lambda: grants_map,
        policy=lambda: Policy(revision=1, denied_capabilities=frozenset()),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(os.environ["MAI_FX_ENV"]),
        executor=executor,
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


# ---------------------------------------------------------------------------
# scenario fixtures (compaction-specific roots; DB fixtures reused)
# ---------------------------------------------------------------------------


@pytest.fixture
def scenario(tmp_path: Path) -> Path:
    directory = tmp_path / "compaction-scenario"
    (directory / "gates").mkdir(parents=True)
    handlers = directory / "handlers"
    handlers.mkdir()
    (handlers / f"{HANDLER_MODULE_NAME}.py").write_text(
        HANDLER_MODULE_SOURCE, encoding="utf-8"
    )
    return directory


def _write_model_root(root: Path) -> None:
    store = ModelConfigStore(root)
    connection = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "name": "gateway",
        "base_url": "http://127.0.0.1:9/v1",
        "timeout_seconds": 5,
        "capabilities": {"tool_calls": True},
        "credential_ref": SECRET_NAME,
    }
    store.write_connection("gateway", json.dumps(connection) + "\n", expected_revision=0)
    profile = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "name": "fast",
        "connection": "gateway",
        "model": "fixture-model",
        "max_tokens": 256,
        # The declared window TieredCompaction drives toward
        # (target = 0.8 x context_limit tokens, estimated).
        "context_limit": CONTEXT_LIMIT,
        "output_limit": OUTPUT_LIMIT,
    }
    store.write_profile("fast", json.dumps(profile) + "\n", expected_revision=0)
    store.set_selection("gateway", "fast", expected_revision=0)


def _write_data_root(root: Path) -> None:
    from mai.profiles.files import ProfileFileStore

    store = ProfileFileStore(root)
    profile_text = (
        f"schema_version: {SERVER_SCHEMA_VERSION}\n"
        f'server_id: "{SERVER_ID}"\n'
        'name: "compaction-fixture"\n'
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
    )
    store.write(uuid.UUID(SERVER_ID), profile_text, expected_revision=0)
    server_dir = root / "servers" / SERVER_ID
    server_dir.chmod(0o700)
    (server_dir / "prompt.md").write_text(PROMPT, encoding="utf-8")
    os.chmod(server_dir / "prompt.md", 0o600)


@pytest.fixture
def composition(scenario: Path) -> dict[str, Path]:
    model_root = scenario / "model-root"
    data_root = scenario / "data-root"
    _write_model_root(model_root)
    _write_data_root(data_root)
    (scenario / "grants.json").write_text(
        json.dumps([{"capability_id": READ_CAP, "server_ids": [SERVER_ID]}]),
        encoding="utf-8",
    )
    (scenario / "fixture.env").write_text(
        f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8"
    )
    os.chmod(scenario / "fixture.env", 0o600)
    return {"model_root": model_root, "data_root": data_root}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _canonical(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _intent_document(run_id: str) -> dict:
    from mai.policy.evaluate import ExecutionContext, PolicyMode

    context = ExecutionContext(
        principal="hash:fixture-principal",
        server_id=uuid.UUID(SERVER_ID),
        run_id=uuid.UUID(run_id),
        parent_scope=None,
        profile_revision="profile-1",
        prompt_revision="prompt-1",
        credential_references=(),
        allowed_capabilities=(READ_CAP,),
        mode=PolicyMode("delegated"),
    )
    return {
        "v": agent_run.RUN_PAYLOAD_VERSION,
        "run_id": run_id,
        "server_id": SERVER_ID,
        "context": context.to_json(),
        "role": ROLE,
        "task": TASK,
        "connection": "gateway",
        "profile": "fast",
    }


def _spawn_worker(url: str, scenario: Path, tag: str) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("MAI_TEST_DATABASE_URL", None)
    env["MAI_DATABASE_URL"] = url
    env["MAI_WORKER_HANDLERS"] = HANDLER_MODULE_NAME
    env["MAI_FX_JOURNAL"] = str(scenario / "journal.log")
    env["MAI_FX_GATE_DIR"] = str(scenario / "gates")
    env["MAI_FX_GRANTS"] = str(scenario / "grants.json")
    env["MAI_FX_ENV"] = str(scenario / "fixture.env")
    env["MAI_FX_MODEL_ROOT"] = str(scenario / "model-root")
    env["MAI_FX_DATA_ROOT"] = str(scenario / "data-root")
    env["MAI_FX_MODEL_SCRIPT"] = (scenario / "model-script.json").read_text(
        encoding="utf-8"
    )
    env["PYTHONPATH"] = os.pathsep.join(
        [str(scenario / "handlers"), *(p for p in [env.get("PYTHONPATH")] if p)]
    )
    log = open(scenario / f"{tag}.log", "w")  # noqa: SIM115 - closed with proc
    return subprocess.Popen(
        [sys.executable, "-m", "mai.jobs.worker", "--concurrency", "2"],
        cwd=str(BACKEND_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _stop(proc: subprocess.Popen) -> int:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
    try:
        return proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=30)


def _journal_lines(scenario: Path) -> list[str]:
    try:
        return (scenario / "journal.log").read_text().splitlines()
    except FileNotFoundError:
        return []


def _wait_until(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _terminal_state(client: EngineClient, run_id: str) -> str | None:
    state = client.run_state(run_id)
    return None if state is None else state.value


def _seen_records(scenario: Path) -> list[dict[str, int]]:
    """One record per journaled model call: what the model saw."""
    records = []
    for line in _journal_lines(scenario):
        if line.startswith("seen:"):
            _, msgs, size, head, orphans, unanswered = line.split(":")
            records.append(
                {
                    "messages": int(msgs),
                    "bytes": int(size),
                    "head_system": int(head),
                    "orphans": int(orphans),
                    "unanswered": int(unanswered),
                }
            )
    return records


# ---------------------------------------------------------------------------
# live: the durable compaction claim
# ---------------------------------------------------------------------------


class TestDurableCompaction:
    def test_run_compacts_history_and_survives(
        self, agent_url, factory, insert_run, scenario, composition  # noqa: F841
    ) -> None:
        """Eight chunky tool rounds blow past 0.8 x the declared window.

        The journaled model view proves, per call: the seeded system
        prompt still heads the history, tool pairing is intact, the
        message COUNT grows by exactly the round's two messages (no
        message ever dropped), and the serialized history BYTES shrink
        between consecutive calls — only possible if the previous round's
        checkpointed ``history_json`` came back compacted (the run loop
        feeds the checkpoint forward; nothing else rewrites history).
        """
        script = [
            {
                "tool_call": {
                    "name": "fixture_read",
                    "args": {"marker": f"m{index + 1}"},
                    "id": f"c{index + 1}",
                }
            }
            for index in range(TOOL_ROUNDS)
        ]
        script.append({"text": "All markers collected; compaction fixture done."})
        (scenario / "model-script.json").write_text(json.dumps(script), encoding="utf-8")

        run_id = str(uuid.uuid4())
        document = _intent_document(run_id)
        client = EngineClient(agent_url)
        worker = None
        try:
            worker = _spawn_worker(agent_url, scenario, "worker")
            _run(insert_run(run_id, requested_grants=(READ_CAP,)))
            client.enqueue_run(run_id, agent_run.HANDLER_NAME, _canonical(document))
            _wait_until(
                lambda: _terminal_state(client, run_id) in ("succeeded", "failed"),
                RUN_TIMEOUT,
                "compaction run terminal state",
            )
            assert _terminal_state(client, run_id) == "succeeded", _journal_lines(
                scenario
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        lines = _journal_lines(scenario)

        # Exactly-once chunky effects: 8 dispatched markers, each once.
        begins = [line for line in lines if line.startswith("begin:fixture.read:")]
        effects = [line for line in lines if line.startswith("effect:fixture.read:")]
        assert sorted(begins) == sorted(
            f"begin:fixture.read:m{index + 1}" for index in range(TOOL_ROUNDS)
        ), begins
        assert sorted(effects) == sorted(
            f"effect:fixture.read:m{index + 1}" for index in range(TOOL_ROUNDS)
        ), effects

        records = _seen_records(scenario)
        # 8 tool rounds + 1 final round = 9 model calls.
        assert len(records) == TOOL_ROUNDS + 1, lines

        # The system prompt survives at the head of EVERY model view —
        # including the compacted ones.
        assert all(record["head_system"] == 1 for record in records), records

        # Pairing integrity across every view the model received.
        assert all(record["orphans"] == 0 for record in records), records
        assert all(record["unanswered"] == 0 for record in records), records

        # No message is ever dropped: the count grows by exactly two per
        # round (assistant response + tool-return request). Compaction
        # reclaims CONTENT, never structure.
        for previous, current in zip(records, records[1:]):
            assert current["messages"] == previous["messages"] + 2, records

        # History bytes SHRINK between consecutive calls at the first
        # trigger: the clearing tier blanks 3 pairs at once there, more
        # than the round adds. The only writer of the next round's
        # history is the checkpointed round record, so a decrease proves
        # the COMPACTED history was what got checkpointed.
        decreases = [
            (index, previous["bytes"], current["bytes"])
            for index, (previous, current) in enumerate(zip(records, records[1:]))
            if current["bytes"] < previous["bytes"]
        ]
        assert decreases, records

        # And the final checkpointed history is materially below the
        # uncompacted accumulation (baseline derived from the observed
        # pre-trigger round growth — at least one full round reclaimed).
        round_growth = records[1]["bytes"] - records[0]["bytes"]
        uncompacted_final = records[0]["bytes"] + round_growth * (
            len(records) - 1
        )
        assert records[-1]["bytes"] < uncompacted_final - round_growth, records

        # The run completed with the final text round intact.
        assert lines.count("model_call") == TOOL_ROUNDS + 1
        # The summarizing tier never needed its own model call here: the
        # zero-LLM clearing tier alone reclaimed the budget (cheap tiers
        # first is the point of TieredCompaction).
        assert "summarizer_call" not in lines
