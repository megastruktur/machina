"""Runtime-v2 campaign acceptance (`rv-acceptance`) — RV2-* live scenarios.

Card ``rv-acceptance`` against runtime-v2.md §12 and the frozen scenario
registry (runtime-scenarios.md RV2-JOURNEY..RV2-LEGACY; RV2-DEPLOY-DRAIN
stays `design-frozen` for the deploy gate). This module is the campaign
acceptance producer: every claim executes on the REAL production path —
real Postgres (migrations + DBOS engine), real uvicorn ASGI app, real
worker subprocesses, real dispatcher/approval/grant machinery, the real
LiteLLM gateway for user journeys, and a REAL disposable Arch sshd
container as the mutation target.

Provenance discipline (AC6/AC7):
- the gateway key crosses ONLY in memory and one ``0600`` ``.env``; it is
  NEVER printed, logged, or asserted-into evidence;
- scripted ``FunctionModel`` runs are deterministic fault injection and
  are labelled as such at each scenario; user journeys use the REAL
  provider;
- the disposable target follows runtime-environment ADR D1-D5: unique
  ``mai-fxt-rv2-acc-*`` namespace, key auth, pinned host key, cleanup
  proof;
- journey metrics land in the campaign evidence directory
  (``RV2_ACCEPTANCE_EVIDENCE_DIR``) for ``verdict_matrix.json``.

Scenario → test map (one producer test may cover several rows):
- RV2-JOURNEY  : :class:`TestRV2Journey` (real provider, real sshd target)
- RV2-DENY     : :class:`TestRV2Deny` (research_only template, direct +
                 delegation-carried denial, CAS staleness)
- RV2-DELEGATE : :class:`TestRV2Delegate` (two scoped targets, operator
                 scope CAS, child capability/budget narrowing, receipts)
- RV2-APPROVAL-RESTART : :class:`TestRV2ApprovalRestart` (worker SIGKILL
                 mid-park, payload-bound single effect, mismatch refusal)
- RV2-REPLAY   : :class:`TestRV2ReplaySteerCancel` (crash/restart replay,
                 steering, pause boundary, cancellation)
- RV2-UNKNOWN  : :class:`TestRV2Unknown` (lost receipt -> reconciling ->
                 blind-resume refusal -> explicit classification)
- RV2-UI-A11Y  : executed by the Playwright suite (not this module; the
                 acceptance run records its result in the evidence dir)
- RV2-LEGACY   : :class:`TestRV2Legacy` (truthful markers, legacy replay
                 by the frozen ``agent.run`` worker, drain inventory)
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import psycopg
import pytest
from fastapi import FastAPI

from mai.contracts.conversations import SCHEMA_VERSION as CONVERSATION_SCHEMA_VERSION
from mai.agents.budgets import BUDGET_PAYLOAD_VERSION

from test_conversation_api_v2_http import (  # sibling harness, imported not copied
    BACKEND_ROOT,
    _cookies,
    _error,
    _mutation_headers,
    _read_step_row,
    _run_event_data,
    _run_event_types,
    _truncate,
    _wait_state,
    _wait_until,
    _alembic_config,
    _drop_app_tables,
    _seed_terminal_catalog,
    LIVE_URL_VAR,
    ORIGIN,
    call_app,
    create_objective,
    create_profile,
    login_session,
    seed_legacy_conversation,
    submit_v2_conversation,
)
from test_agent_run_v2 import (  # blocking preflight gateway (campaign-pinned)
    GATEWAY_BASE_URL,
    GATEWAY_CONFIG_PATH,
    GATEWAY_MODEL,
    _gateway_key,
)
from test_supervisor import (  # sibling-card fixture, imported not copied
    _ArchSshdFixture,
    _docker_available,
)

pytestmark = pytest.mark.anyio

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

EVIDENCE_DIR = Path(
    os.environ.get(
        "RV2_ACCEPTANCE_EVIDENCE_DIR",
        "/home/megastruktur/projects/dev/machina/plans/runtime-v2/"
        "rv_evidence/acceptance",
    )
)

#: The canary is a secret-SHAPED synthetic token (never a real secret): the
#: fx target embeds it in every tool output so redaction is observable.
CANARY_TOKEN = "sk-rv2acccanary0123456789abcdef"

#: Real-provider journey budget (bounded; the predeclared acceptance run
#: budget is 4200 s — this per-journey bound keeps one attempt honest).
JOURNEY_RUN_TIMEOUT = 420.0
FX_RUN_TIMEOUT = 120.0

SSHD_CREDENTIAL_REF = "MAI_SSH_KEY_04_SUPERVISOR_FIXTURE"
FX_CREDENTIAL_REF = "MAI_FXT_TOKEN"

FX_READ = "fixture.read"
FX_PRIV = "fixture.priv"


class AccSshdFixture(_ArchSshdFixture):
    """The disposable real target in THIS card's unique namespace (D2)."""

    container = "mai-fxt-rv2-acc-arch"


# ---------------------------------------------------------------------------
# evidence helpers
# ---------------------------------------------------------------------------


def record_metrics(scenario_id: str, document: dict[str, Any]) -> Path:
    """Merge one scenario's measured metrics into the evidence JSON."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / "journey_metrics.json"
    try:
        merged: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        merged = {}
    merged[scenario_id] = document
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# the worker-side composition (written into the scenario handlers dir)
# ---------------------------------------------------------------------------

ACC_HANDLER_MODULE_NAME = "rv2_acceptance_handlers"

ACC_HANDLER_MODULE_SOURCE = '''
"""Worker-side composition for the rv-acceptance live scenarios: the REAL
dispatcher machinery (GrantConfigStore-backed grants/policy, real approval
store), the REAL ``host.shell`` capability against the disposable sshd
target, a journaled fx target for deterministic fault injection, and the
REAL gateway model unless a labelled fault-injection script is set."""

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import mai.agents.run as agent_run
from mai.agents.budgets import BudgetLedger
from mai.agents.model_store import ModelConfigStore
from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
from mai.capabilities.host_shell import (
    HostShellCapability,
    descriptor as host_shell_descriptor,
)
from mai.credentials.env import EnvResolver
from mai.jobs.engine import EngineClient
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.config import GrantConfigStore
from mai.policy.evaluate import Policy
from mai.contracts.server import parse_server_profile_text
from mai.profiles.files import ProfileFileStore
from mai.execution.ssh import SshIdentity, pin_host_key

FX_READ = "fixture.read"
FX_PRIV = "fixture.priv"

# The canary is synthetic (never a real secret).
CANARY = os.environ.get("MAI_FX_CANARY", "sk-rv2acccanary0123456789abcdef")


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
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
            "execution_environments": ["linux_ssh"],
            "effect_class": effect_class,
            "resource_scope": [] if effect_class == "read" else ["target.exec"],
            "required_grants": [],
        },
        source=f"<rv2-acc {capability_id}>",
    )


class FixtureExecutor:
    """Journaled fx target for LABELLED deterministic fault injection:
    behaviors keyed ``capability:marker`` then ``capability`` — ``ok`` |
    ``gate`` | ``unknown_status``. Every output carries the synthetic
    canary plus oversized padding so redaction/truncation is observable."""

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
            "stdout": f"leak token {CANARY} end " + "x" * 5000,
            "stderr": "",
        }
        return ExecutorResult(
            ok=status == "succeeded", output=json.dumps(document, sort_keys=True)
        )


def _script_model():
    """LABELLED deterministic fault injection: mission-branched scripts."""
    scripts = json.loads(os.environ["MAI_FX_MODEL_SCRIPT"])
    journal = os.environ["MAI_FX_JOURNAL"]

    def _mark(line):
        with open(journal, "a", encoding="utf-8") as handle:
            handle.write(line + "\\n")

    def _mission_of(messages):
        for message in reversed(messages):
            if message.kind == "request":
                for part in message.parts:
                    if getattr(part, "part_kind", "") == "user-prompt":
                        text = str(getattr(part, "content", ""))
                        found = re.search(r"mission=([a-z-]+)", text)
                        if found:
                            return found.group(1)
        return "main"

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

    def _mission_of(messages):
        for message in reversed(messages):
            if message.kind == "request":
                for part in message.parts:
                    if getattr(part, "part_kind", "") == "user-prompt":
                        text = str(getattr(part, "content", ""))
                        for marker in ("aspect-alpha", "aspect-beta"):
                            if marker in text:
                                _mark(f"user_marker:{marker}")
                        found = re.search(r"mission=([a-z-]+)", text)
                        if found:
                            return found.group(1)
        return "main"

    def _model_fn(messages, info):
        _mark("model_call")
        script = scripts[_mission_of(messages)]
        index = (len(messages) - 1) // 2
        if index >= len(script):
            raise AssertionError("model script exhausted for the mission")
        return _entry_payload(script[index])

    async def _stream_fn(messages, info):
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

    return FunctionModel(_model_fn, stream_function=_stream_fn)


def _build():
    url = os.environ["MAI_DATABASE_URL"]
    app_engine = create_async_engine(url, poolclass=NullPool)
    sessions = async_sessionmaker(app_engine, expire_on_commit=False)
    # The worker shares the APP's data root: ONE GrantConfigStore, ONE
    # canonical profile store, ONE model store (the production contract).
    data_root = Path(os.environ["MAI_FX_DATA_ROOT"])
    resolver = EnvResolver(os.environ["MAI_FX_ENV"])

    fx_executor = FixtureExecutor()
    registry = CapabilityRegistry()
    registry.register(_descriptor(FX_READ, "read"))
    registry.register(_descriptor(FX_PRIV, "privileged"))
    registry.register(host_shell_descriptor())

    profile_store = ProfileFileStore(data_root)

    def _load_profile(server_id):
        return parse_server_profile_text(profile_store.read(server_id).text)

    known_hosts_dir = data_root / "known_hosts"
    known_hosts_dir.mkdir(parents=True, exist_ok=True)

    def _known_hosts_for(server_id):
        prof = _load_profile(server_id)
        target = known_hosts_dir / f"{server_id}.known_hosts"
        pin_host_key(
            SshIdentity(
                host=prof.ssh.host,
                port=prof.ssh.port,
                user=prof.ssh.user,
                host_key=prof.ssh.host_key,
                credential_ref=prof.ssh.credential_ref,
            ),
            target,
        )
        return target

    host_shell_cap = HostShellCapability(
        profiles=_load_profile,
        known_hosts=_known_hosts_for,
        receipt_root="/tmp/mai-receipts-rv2-acc",
        intents_dir=data_root / "intents",
    )
    (data_root / "intents").mkdir(parents=True, exist_ok=True)

    executors = {
        FX_READ: fx_executor,
        FX_PRIV: fx_executor,
        "host.shell": host_shell_cap,
    }

    async def _composite_executor(call):
        import dataclasses

        target = executors.get(call.descriptor.identifier)
        if target is None:
            return ExecutorResult(
                ok=False,
                output=f"capability {call.descriptor.identifier!r} has no "
                "runtime executor",
            )
        # The production credential seam (worker.py): the enrolled
        # profile's ssh credential reference resolves HERE, at effect
        # time, from the .env — never through the model surface.
        if call.context.server_id != uuid.UUID(int=0):
            try:
                prof = _load_profile(call.context.server_id)
                ref = prof.ssh.credential_ref
                if ref and ref not in call.credentials:
                    secret = resolver.resolve(ref)
                    call = dataclasses.replace(
                        call,
                        credentials={**call.credentials, ref: secret},
                    )
            except Exception:
                pass
        return await target(call)

    grants_store = GrantConfigStore(data_root)

    def _read_grants():
        # The production reader: the store first, then the enrolled
        # profiles' own capability declarations (worker.py rule).
        grants: dict[str, set] = {}
        try:
            stored = grants_store.read()
            for cid, sids in stored.grants.items():
                grants.setdefault(cid, set()).update(sids)
        except Exception:
            pass
        servers_dir = data_root / "servers"
        if servers_dir.is_dir():
            for child in servers_dir.iterdir():
                if not child.is_dir():
                    continue
                try:
                    prof = _load_profile(uuid.UUID(child.name))
                except Exception:
                    continue
                for cap_slug in prof.capabilities:
                    normalized = cap_slug.replace("_", ".")
                    grants.setdefault(cap_slug, set()).add(uuid.UUID(child.name))
                    grants.setdefault(normalized, set()).add(uuid.UUID(child.name))
        return {cid: frozenset(sids) for cid, sids in grants.items()}

    def _read_policy():
        try:
            return grants_store.read().policy
        except Exception:
            return Policy(revision=1, denied_capabilities=frozenset())

    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(sessions),
        grants=_read_grants,
        policy=_read_policy,
        adopted=lambda server_id: True,
        credentials=lambda: resolver,
        executor=_composite_executor,
    )
    model_store = ModelConfigStore(os.environ["MAI_FX_MODEL_ROOT"])
    fake_model = _script_model() if os.environ.get("MAI_FX_MODEL_SCRIPT") else None
    model_builder = (lambda **kwargs: fake_model) if fake_model is not None else None
    agent_executor = agent_run.AgentRunExecutor(
        sessions=sessions,
        dispatcher=dispatcher,
        registry=registry,
        model_store=model_store,
        data_root=str(data_root),
        locks=ResourceLockService(url),
        resolver=resolver,
        model_builder=model_builder,
        host_supervisor=host_shell_cap,
    )
    agent_run.configure_agent_run(agent_executor)
    from mai.agents import delegate as agent_delegate

    agent_delegate.configure_delegation(
        agent_delegate.DelegationExecutor(
            sessions=sessions,
            executor=agent_executor,
            client=EngineClient(url),
            ledger=BudgetLedger(sessions),
        )
    )
    import mai.agents.run_v2  # noqa: F401

    return host_shell_cap


composed = _build()
'''


# ---------------------------------------------------------------------------
# the acceptance scenario (one disposable composition per test)
# ---------------------------------------------------------------------------


class AccScenario:
    """One disposable worker composition sharing the APP's data root."""

    def __init__(self, tmp_path: Path, data_root: Path) -> None:
        self.root = tmp_path / "acc-scenario"
        (self.root / "gates").mkdir(parents=True)
        handlers = self.root / "handlers"
        handlers.mkdir()
        (handlers / f"{ACC_HANDLER_MODULE_NAME}.py").write_text(
            ACC_HANDLER_MODULE_SOURCE, encoding="utf-8"
        )
        self.data_root = data_root
        self.worker: subprocess.Popen | None = None

    @property
    def journal(self) -> Path:
        return self.root / "journal.log"

    @property
    def last_worker_log(self) -> Path:
        return self.root / f"worker-{max(getattr(self, '_spawn_count', 1) - 1, 0)}.log"

    def lines(self) -> list[str]:
        try:
            return self.journal.read_text().splitlines()
        except FileNotFoundError:
            return []

    def count(self, marker: str) -> int:
        return sum(1 for line in self.lines() if line == marker)

    def write_env(self, extra: dict[str, str] | None = None) -> Path:
        env_path = self.data_root / ".env"
        lines = [
            f"{SSHD_CREDENTIAL_REF}=<set-by-the-sshd-fixture>",
            f"{FX_CREDENTIAL_REF}=fxt-synthetic-token-value-rv2-acc",
        ]
        if extra:
            lines.extend(f"{k}={v}" for k, v in extra.items())
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(env_path, 0o600)
        return env_path

    def write_model_script(self, scripts: dict[str, list[dict]]) -> None:
        (self.root / "model-scripts.json").write_text(
            json.dumps(scripts), encoding="utf-8"
        )

    def set_behavior(self, behavior: dict[str, str]) -> None:
        (self.root / "behavior.json").write_text(
            json.dumps(behavior), encoding="utf-8"
        )

    def spawn_worker(
        self, url: str, *, canary: str = CANARY_TOKEN
    ) -> subprocess.Popen:
        env = dict(os.environ)
        env.pop("MAI_TEST_DATABASE_URL", None)
        env["MAI_DATABASE_URL"] = url
        env["MAI_WORKER_HANDLERS"] = ACC_HANDLER_MODULE_NAME
        env["MAI_FX_JOURNAL"] = str(self.journal)
        env["MAI_FX_GATE_DIR"] = str(self.root / "gates")
        env["MAI_FX_ENV"] = str(self.data_root / ".env")
        env["MAI_FX_DATA_ROOT"] = str(self.data_root)
        env["MAI_FX_MODEL_ROOT"] = str(self.data_root)
        env["MAI_FX_CANARY"] = canary
        env.pop("MAI_FX_BEHAVIOR", None)
        behavior = self.root / "behavior.json"
        if behavior.exists():
            env["MAI_FX_BEHAVIOR"] = behavior.read_text(encoding="utf-8")
        script = self.root / "model-scripts.json"
        env.pop("MAI_FX_MODEL_SCRIPT", None)
        if script.exists():
            env["MAI_FX_MODEL_SCRIPT"] = script.read_text(encoding="utf-8")
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.root / "handlers"), *(p for p in [env.get("PYTHONPATH")] if p)]
        )
        spawn_index = getattr(self, "_spawn_count", 0)
        self._spawn_count = spawn_index + 1
        log = open(self.root / f"worker-{spawn_index}.log", "w")  # noqa: SIM115
        self.worker = subprocess.Popen(
            [sys.executable, "-m", "mai.jobs.worker", "--concurrency", "2"],
            cwd=str(BACKEND_ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.worker.poll() is not None:
                raise AssertionError(
                f"worker exited during startup: "
                f"{self.last_worker_log.read_text(encoding='utf-8')[-2000:]}"
                )
            time.sleep(0.25)
        return self.worker

    def kill_worker(self) -> None:
        """Deterministic crash: SIGKILL the worker mid-flight."""
        if self.worker is None or self.worker.poll() is not None:
            raise AssertionError("no live worker to kill")
        self.worker.send_signal(signal.SIGKILL)
        self.worker.wait(timeout=30)

    def release_gate(self, capability: str, marker: str) -> None:
        (self.root / "gates" / f"gate-{capability}-{marker}").write_text("go")

    def stop(self) -> int:
        if self.worker is None or self.worker.poll() is not None:
            return 0
        self.worker.send_signal(signal.SIGTERM)
        try:
            return self.worker.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.worker.kill()
            return self.worker.wait(timeout=30)


@pytest.fixture
async def acc_app(bootstrapped_url: str, tmp_path: Path):
    """The REAL ASGI app over the shared data root (one store set)."""
    from mai.agents.model_store import ModelConfigStore
    from mai.api.app import create_app

    await _truncate(bootstrapped_url)
    data_root = tmp_path / "acc-data-root"
    app = create_app(
        env={
            "MAI_DATABASE_URL": bootstrapped_url,
            "MAI_PUBLIC_ORIGINS": ORIGIN,
            "MAI_DATA_ROOT": str(data_root),
        }
    )
    # A bounded controller-side model selection (the canonical submit path's
    # C06 gate). The worker re-reads the SAME store; the REAL-journey test
    # rewrites the connection/profile to the preflight gateway.
    store = ModelConfigStore(data_root)
    connection: dict[str, object] = {
        "schema_version": 1,
        "name": "gateway",
        "base_url": "http://127.0.0.1:9/v1",
        "timeout_seconds": 5,
        "capabilities": {"tool_calls": True},
    }
    store.write_connection("gateway", json.dumps(connection) + "\n", expected_revision=0)
    profile: dict[str, object] = {
        "schema_version": 1,
        "name": "fast",
        "connection": "gateway",
        "model": "fixture-model",
        "max_tokens": 2048,
    }
    store.write_profile("fast", json.dumps(profile) + "\n", expected_revision=0)
    store.set_selection("gateway", "fast", expected_revision=0)
    async with app.router.lifespan_context(app):
        app.state.acc_data_root = data_root
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
async def acc_jar(acc_app: FastAPI) -> dict[str, str]:
    return await login_session(acc_app)


@pytest.fixture
def acc_scenario(tmp_path: Path, acc_app: FastAPI):
    box = AccScenario(tmp_path, Path(acc_app.state.acc_data_root))
    box.write_env()
    yield box
    box.stop()


async def enroll_sshd_server(
    acc_app: FastAPI,
    acc_jar: dict[str, str],
    fixture: AccSshdFixture,
    *,
    capabilities: list[str],
) -> uuid.UUID:
    """Enroll the REAL disposable sshd target through the real surface."""
    response = await call_app(
        acc_app,
        "POST",
        "/api/v1/servers",
        json_body={
            "name": "rv2-acc-sshd-target",
            "prompt_revision": 7,
            "environment": "linux_ssh",
            "ssh": {
                "host": "127.0.0.1",
                "port": fixture.port,
                "user": fixture.identity().user,
                "host_key": fixture.host_key_fingerprint,
                "credential_ref": SSHD_CREDENTIAL_REF,
            },
            "privilege": "none",
            "capabilities": capabilities,
        },
        headers=_mutation_headers(acc_jar),
        cookies=acc_jar,
    )
    assert response.status == 201, response.body
    server_id = uuid.UUID(response.json()["server_id"])
    # The worker's credential resolver must find the fixture key: point the
    # shared .env's ref at the REAL key path (0600, tmp namespace).
    env_path = Path(acc_app.state.acc_data_root) / ".env"
    lines = [
        line
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"{SSHD_CREDENTIAL_REF}=")
    ]
    lines.append(f"{SSHD_CREDENTIAL_REF}={fixture.key_path}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)
    return server_id


async def put_grants_for(
    acc_app: FastAPI,
    acc_jar: dict[str, str],
    grants: dict[str, list[uuid.UUID]],
    *,
    expected_revision: int = 0,
    denied: list[str] | None = None,
) -> dict[str, Any]:
    """Grant capabilities through the REAL grants surface (one revision)."""
    response = await call_app(
        acc_app,
        "PUT",
        "/api/v1/grants",
        json_body={
            "expected_revision": expected_revision,
            "denied_capabilities": denied or [],
            "grants": [
                {"capability_id": cid, "server_ids": [str(s) for s in sids]}
                for cid, sids in grants.items()
            ],
        },
        headers=_mutation_headers(acc_jar),
        cookies=acc_jar,
    )
    assert response.status == 200, response.body
    return response.json()


async def point_model_at_gateway(acc_app: FastAPI) -> str:
    """Rewrite the shared model selection to the REAL gateway (journeys)."""
    from mai.agents.model_store import ModelConfigStore

    store = ModelConfigStore(Path(acc_app.state.acc_data_root))
    key = _gateway_key()
    connection = {
        "schema_version": 1,
        "name": "gateway",
        "base_url": GATEWAY_BASE_URL,
        "timeout_seconds": 180,
        "capabilities": {"tool_calls": True},
        "credential_ref": "MAI_GATEWAY_KEY",
    }
    store.write_connection(
        "gateway",
        json.dumps(connection) + "\n",
        expected_revision=store.read_connection("gateway").revision,
    )
    profile = {
        "schema_version": 1,
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
    revision = store.get_selection().revision
    store.set_selection("gateway", "fast", expected_revision=revision)
    env_path = Path(acc_app.state.acc_data_root) / ".env"
    lines = [
        line
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("MAI_GATEWAY_KEY=")
    ]
    lines.append(f"MAI_GATEWAY_KEY={key}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)
    return GATEWAY_MODEL


async def objective_journal_projection(
    acc_app: FastAPI,
    acc_jar: dict[str, str],
    objective_id: uuid.UUID,
    server_id: uuid.UUID,
) -> dict[str, Any]:
    response = await call_app(
        acc_app,
        "GET",
        f"/api/v1/objectives/{objective_id}/journal?server_id={server_id}",
        headers=_cookies(acc_jar),
    )
    assert response.status == 200, response.body
    return response.json()


async def seed_v2_delegation_parent(
    url: str,
    *,
    parent_run_id: str,
    server_id: str,
    objective_id: str,
    task: str,
    children: list[dict[str, Any]],
    parent_grants: tuple[str, ...] = ("fixture.read",),
    root_budget: dict[str, int] | None = None,
    children_wait_seconds: float = 90.0,
) -> str:
    """Seed one v2 delegating parent onto an existing objective through the
    REAL store seams: the parent ``job_runs`` row (+ the 0011 mandatory
    unclassified tag, exactly like the canonical submit writer), the ONE
    durable §3.3 binding, and ONE engine enqueue of ``agent.delegate`` with
    a parse-validated payload-v2 document. The delegation ENGINE owns
    everything else (planning, child rows/tags/bindings, dispatch)."""
    from mai.agents import delegate as agent_delegate
    from mai.agents import run as agent_run
    from mai.agents.objective_store import abind_run
    from mai.contracts.objectives import ActorKind
    from mai.database import create_app_engine
    from mai.jobs.engine import EngineClient
    from mai.policy.evaluate import ExecutionContext, PolicyMode
    from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID, SYSTEM_ASSIGNMENT_SOURCE
    from sqlalchemy import text

    async def _seed() -> None:
        engine, sessions = create_app_engine(url)
        try:
            context = ExecutionContext(
                principal="hash:rv2-acceptance-fixture",
                server_id=uuid.UUID(server_id),
                run_id=uuid.UUID(parent_run_id),
                parent_scope=None,
                profile_revision="acc-1",
                prompt_revision="acc-1",
                credential_references=(),
                allowed_capabilities=parent_grants,
                mode=PolicyMode("delegated"),
            )
            run_document = {
                "v": agent_run.RUN_PAYLOAD_VERSION,
                "run_id": parent_run_id,
                "server_id": server_id,
                "context": context.to_json(),
                "role": "operator assistant",
                "task": task,
            }
            document: dict[str, object] = {
                "v": agent_delegate.DELEGATION_PAYLOAD_VERSION_V2,
                "run": run_document,
                "children": children,
                "children_wait_seconds": children_wait_seconds,
            }
            agent_delegate.parse_delegation_intent_v2(
                json.dumps(document, sort_keys=True, separators=(",", ":"))
            )
            async with sessions() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO job_runs
                            (run_id, server_id, depth, profile_revision,
                             prompt_revision, model_profile, requested_grants,
                             root_budget, submitted_at, updated_at)
                        VALUES
                            (:run_id, :server_id, 0, 1, 1, 'fast',
                             CAST(:requested_grants AS jsonb),
                             CAST(:root_budget AS jsonb), now(), now())
                        """
                    ),
                    {
                        "run_id": uuid.UUID(parent_run_id),
                        "server_id": uuid.UUID(server_id),
                        "requested_grants": json.dumps(list(parent_grants)),
                        "root_budget": (
                            json.dumps(root_budget) if root_budget is not None else None
                        ),
                    },
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO job_tags
                            (job_id, tag_id, assigned_by, assignment_source)
                        VALUES (CAST(:rid AS uuid), CAST(:tag AS uuid), '', :source)
                        """
                    ),
                    {
                        "rid": parent_run_id,
                        "tag": RESERVED_UNCLASSIFIED_TAG_ID,
                        "source": SYSTEM_ASSIGNMENT_SOURCE,
                    },
                )
                await session.commit()
            await abind_run(
                sessions,
                uuid.UUID(objective_id),
                server_id=uuid.UUID(server_id),
                run_id=uuid.UUID(parent_run_id),
                initiating_server_id=uuid.UUID(server_id),
                actor_kind=ActorKind.AGENT,
                handler="agent.run_v2",
            )
        finally:
            await engine.dispose()
        client = EngineClient(url)
        client.enqueue_run(
            parent_run_id,
            "agent.delegate",
            json.dumps(document, sort_keys=True, separators=(",", ":")),
        )

    await _seed()
    return parent_run_id


# ---------------------------------------------------------------------------
# session fixtures: disposable DB chain, real gateway + real disposable target
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def bootstrapped_url() -> Iterator[str]:
    """The disposable migrated database (one chain for the whole module).

    Same recipe as the sibling harness (drop to bare schema, migrate to
    head, launch the engine once for the DBOS schema, seed the terminal
    tag catalog); defined here so this module owns its chain regardless
    of collection order.
    """
    from alembic import command

    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the RV2 live scenarios need a real "
            "disposable Postgres fixture"
        )
    scratch = Path("/tmp/mai-fxt-rv2-acc")
    scratch.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url
    try:
        _drop_app_tables(url)
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    from mai.jobs import engine as jobs_engine

    jobs_engine.launch_worker(url)
    jobs_engine.shutdown()
    _seed_terminal_catalog(url)
    yield url
    jobs_engine.shutdown()
    _drop_app_tables(url)


@pytest.fixture(scope="session")
def acc_gateway() -> str:
    """Blocking preflight: the REAL gateway must be reachable up front."""
    try:
        import httpx

        key = _gateway_key()
        probe = httpx.get(
            f"{GATEWAY_BASE_URL}/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        assert probe.status_code == 200, "gateway preflight failed"
        return key
    except Exception as error:
        pytest.fail(
            "the real-provider journeys require the preflight gateway "
            f"({GATEWAY_CONFIG_PATH} @ {GATEWAY_BASE_URL}): "
            f"{type(error).__name__}"
        )


@pytest.fixture(scope="session")
def acc_sshd() -> AccSshdFixture:
    """The REAL disposable mutation target (ADR D1-D5; cleanup proof)."""
    if not _docker_available():
        pytest.skip("docker unavailable: the real sshd target requires it")
    fixture = AccSshdFixture()
    try:
        fixture.start()
    except Exception:
        fixture.cleanup()
        raise
    yield fixture
    fixture.cleanup()


# ---------------------------------------------------------------------------
# RV2-JOURNEY — real provider, real target, one submission (AC1/AC2)
# ---------------------------------------------------------------------------


class TestRV2Journey:
    @pytest.mark.real_target
    async def test_real_provider_single_turn_inspect_act_verify_report(
        self,
        acc_app: FastAPI,
        acc_jar: dict[str, str],
        acc_scenario: AccScenario,
        acc_sshd: AccSshdFixture,
        acc_gateway: str,
        bootstrapped_url: str,
    ) -> None:
        """ONE authorized v2 submission completes inspect -> act -> verify
        -> report on the REAL sshd target through the REAL provider with
        ZERO intake events, ZERO second user turn, receipt-backed journal
        entries, and honest journey metrics (AC1/AC2, RV2-JOURNEY)."""
        import httpx

        server = await enroll_sshd_server(
            acc_app,
            acc_jar,
            acc_sshd,
            capabilities=["host_shell"],
        )
        await put_grants_for(acc_app, acc_jar, {"host.shell": [server]})
        model = await point_model_at_gateway(acc_app)
        objective = await create_objective(
            acc_app,
            acc_jar,
            server,
            original_request=(
                "Inspect the fixture target, apply one bounded reversible "
                "change, and verify it."
            ),
        )
        oid = uuid.UUID(objective.json()["objective_id"])
        marker = f"/tmp/rv2-acc-{uuid.uuid4().hex}.marker"
        message = (
            "mission=journey On the enrolled target: (1) run `uname -n` with "
            f"the host_shell tool to inspect it; (2) create the marker file "
            f"with host_shell using the command `printf acc-ok > {marker}`; "
            f"(3) verify with host_shell using `cat {marker}`; then answer "
            "with one short sentence reporting the hostname and marker "
            "content."
        )
        # Preflight: the gateway answers before anything is submitted.
        probe = httpx.get(
            f"{GATEWAY_BASE_URL}/models",
            headers={"Authorization": f"Bearer {acc_gateway}"},
            timeout=10,
        )
        assert probe.status_code == 200, "gateway preflight failed"

        acc_scenario.spawn_worker(bootstrapped_url)
        key = uuid.uuid4()
        submit_started = time.monotonic()
        submitted = await submit_v2_conversation(
            acc_app,
            acc_jar,
            server,
            oid,
            client_request_id=key,
            message=message,
            mode="delegated",
        )
        assert submitted.status == 201, submitted.body
        run_id = uuid.UUID(submitted.json()["run_id"])
        assert submitted.json()["disposition"] == "created"

        deadline = submit_started + JOURNEY_RUN_TIMEOUT
        final = None
        while time.monotonic() < deadline:
            state = await _run_state_of(bootstrapped_url, run_id)
            if state is not None and state not in {"running", "queued"}:
                final = state
                break
            await asyncio.sleep(3)
        total_latency_ms = round((time.monotonic() - submit_started) * 1000)
        assert final == "succeeded", (
            f"real-provider journey ended {final!r}; worker log: "
            f"{acc_scenario.last_worker_log.read_text(encoding='utf-8')[-2000:]}"
        )

        run_events = await _read_run_events(acc_app, acc_jar, run_id)
        types = [event["type"] for event in run_events]
        assert not [
            event_type for event_type in types if event_type.startswith("agent.intake_")
        ], "a v2 run must never emit intake events"
        assert types.count("chat.submitted") == 1, "one submission, no second turn"

        started = next(event for event in run_events if event["type"] == "agent.started")
        assert started["data"]["model"] == model
        rounds = [e for e in run_events if e["type"] == "agent.model_round"]
        requested = [e for e in run_events if e["type"] == "agent.tool_requested"]
        assert requested, "the real model must request at least one tool"
        completed = next(e for e in run_events if e["type"] == "agent.completed")
        assert int(completed["data"]["output_chars"]) > 0

        # AC2 timing: the first useful action (first authorized tool
        # dispatch request) measured from the durable submit anchor.
        first_action_ms = round(
            (
                _ts_of(requested[0]) - _ts_of(
                    next(e for e in run_events if e["type"] == "chat.submitted")
                )
            )
            * 1000
        )
        stop_events = [
            t
            for t in types
            if t
            in {
                "agent.awaiting_approval",
                "agent.awaiting_reconciliation",
            }
        ]
        assert not stop_events, "the authorized journey must not stop unnecessarily"
        assert completed["data"].get("stopped_by") is None

        # The REAL target felt the journey: inspect, bounded mutation,
        # verification read — proven ON the container, beyond any receipt.
        rc, hostname_out, _ = acc_sshd.ssh("uname -n")
        assert rc == 0
        rc, marker_out, _ = acc_sshd.ssh(f"cat {marker}")
        assert rc == 0, "the bounded mutation must exist on the real target"
        assert marker_out.strip() == "acc-ok"

        # Receipt-backed journal: actions + receipts on the objective.
        journal = await objective_journal_projection(acc_app, acc_jar, oid, server)
        actions = journal.get("actions") or []
        receipts = journal.get("receipts") or []
        assert actions, "the work journal must record the dispatched actions"
        assert receipts, "the work journal must record dispatcher receipts"

        # Secret hygiene: the gateway key exists ONLY in the 0600 .env.
        for event in run_events:
            assert acc_gateway not in json.dumps(event, default=str)
        assert acc_gateway not in (Path(acc_app.state.acc_data_root) / ".env").read_text(
            encoding="utf-8"
        ).split("MAI_GATEWAY_KEY=")[0]

        metrics = {
            "scenario": "RV2-JOURNEY",
            "handler": "agent.run_v2",
            "model": model,
            "gateway_base_url": GATEWAY_BASE_URL,
            "first_useful_action_ms": first_action_ms,
            "model_calls": len(rounds),
            "total_latency_ms": total_latency_ms,
            "unnecessary_stops": 0,
            "tool_requests": len(requested),
            "intake_events": 0,
            "target_effects": {
                "hostname": hostname_out.strip(),
                "marker_verified": marker_out.strip() == "acc-ok",
            },
            "journal_actions": len(actions),
            "journal_receipts": len(receipts),
        }
        path = record_metrics("RV2-JOURNEY", metrics)
        assert acc_gateway not in path.read_text(encoding="utf-8")


async def _read_run_events(
    acc_app: FastAPI, acc_jar: dict[str, str], run_id: uuid.UUID
) -> list[dict[str, Any]]:
    """The run's PERSISTED events (the durable truth the SSE surface
    replays): seq/ts/type/data straight from ``job_events``."""
    del acc_app, acc_jar  # the durable read needs no app or session
    with psycopg.connect(
        os.environ["MAI_TEST_DATABASE_URL"], row_factory=psycopg.rows.dict_row
    ) as conn:
        rows = conn.execute(
            "SELECT seq, ts, type, data FROM job_events "
            "WHERE run_id = %s ORDER BY seq",
            (run_id,),
        ).fetchall()
    return [
        {
            "seq": row["seq"],
            "ts": row["ts"].isoformat(),
            "type": row["type"],
            "data": row["data"],
        }
        for row in rows
    ]


def _ts_of(event: dict[str, Any]) -> float:
    from datetime import datetime

    stamp = event.get("ts")
    assert isinstance(stamp, str), f"event carries no timestamp: {event}"
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


async def _run_state_of(url: str, run_id: uuid.UUID) -> str | None:
    """The run's replayed lifecycle state (None when unrecorded)."""
    from mai.database import create_app_engine
    from mai.jobs.lifecycle import RunLifecycle

    engine, sessions = create_app_engine(url)
    try:
        value = await RunLifecycle(sessions).current_state(str(run_id))
        return None if value is None else value.value
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# RV2-DENY — research_only negative controls (AC3)
# ---------------------------------------------------------------------------


class TestRV2Deny:
    async def test_research_only_template_denies_direct_and_delegated_mutation(
        self,
        acc_app: FastAPI,
        acc_jar: dict[str, str],
        acc_scenario: AccScenario,
        bootstrapped_url: str,
    ) -> None:
        """The ``research_only`` template (REAL GrantConfigStore) denies a
        direct privileged attempt AND a delegation-carried one with ZERO
        target effect and sanitized journaled denials. The model here is a
        LABELLED deterministic fault-injection script (a negative control,
        not a user journey)."""
        from mai.plugins.registry import CapabilityRegistry
        from mai.policy.config import GrantConfigStore
        from mai.policy.templates import TemplateError, apply_research_only

        server_fx = await create_profile(
            acc_app, acc_jar, "rv2-acc-fx", capabilities=["fixture.read", "fixture_priv"]
        )
        server_research = await create_profile(
            acc_app, acc_jar, "rv2-acc-research", capabilities=["fixture.read"]
        )
        await put_grants_for(
            acc_app,
            acc_jar,
            {
                FX_READ: [server_fx, server_research],
                FX_PRIV: [server_fx],
            },
        )

        # The ONE template seam: project research_only for the research
        # server through the REAL registry + REAL store (CAS). A stale
        # expected_revision is refused first (the CAS negative control).
        registry = CapabilityRegistry()
        store = GrantConfigStore(Path(acc_app.state.acc_data_root))

        def _descriptor(capability_id: str, effect_class: str):
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
                        "required": ["status"],
                        "properties": {"status": {"type": "string"}},
                    },
                    "execution_environments": ["linux_ssh"],
                    "effect_class": effect_class,
                    "resource_scope": [] if effect_class == "read" else ["target.exec"],
                    "required_grants": [],
                },
                source=f"<rv2-acc {capability_id}>",
            )

        registry.register(_descriptor(FX_READ, "read"))
        registry.register(_descriptor(FX_PRIV, "privileged"))
        stale_revision = store.read().revision
        with pytest.raises(TemplateError):
            apply_research_only(
                store=store,
                registry=registry,
                server_ids=[server_research],
                expected_revision=stale_revision + 1,
            )
        stored, projection = apply_research_only(
            store=store,
            registry=registry,
            server_ids=[server_research],
            expected_revision=stale_revision,
        )
        # The template's own truth: the research server's write capability
        # was removed (or never present) and its read capability stands.
        grants_now = {cid: set(sids) for cid, sids in stored.grants.items()}
        assert uuid.UUID(str(server_research)) not in grants_now.get("fixture.priv", set())
        assert uuid.UUID(str(server_research)) in grants_now.get("fixture.read", set())

        acc_scenario.write_model_script(
            {
                "deny": [
                    {"tool_call": {"name": "fixture_priv", "args": {"marker": "d1"}}},
                    {"text": "the mutation was denied; reporting honestly"},
                ],
                "denychild": [
                    {"tool_call": {"name": "fixture_priv", "args": {"marker": "dc1"}}},
                    {"text": "child mutation denied; reporting honestly"},
                ],
            }
        )
        acc_scenario.spawn_worker(bootstrapped_url)

        # -- DIRECT: the research server's run attempts the mutation ------
        # The ungranted capability is NOT ADVERTISED (intent §3: visibility
        # is not authorization): the bridge refuses the unknown tool before
        # any dispatch — zero effects, the run completes honestly.
        objective = await create_objective(acc_app, acc_jar, server_research)
        oid = uuid.UUID(objective.json()["objective_id"])
        key = uuid.uuid4()
        submitted = await submit_v2_conversation(
            acc_app,
            acc_jar,
            server_research,
            oid,
            client_request_id=key,
            message="mission=deny Attempt the fixture mutation with marker d1.",
        )
        assert submitted.status == 201, submitted.body
        run_id = uuid.UUID(submitted.json()["run_id"])
        _wait_state(bootstrapped_url, run_id, {"succeeded"}, FX_RUN_TIMEOUT)

        # ZERO target effect: the fx target never began the privileged op.
        assert acc_scenario.count("begin:fixture.priv:d1") == 0
        assert acc_scenario.count("effect:fixture.priv:d1") == 0
        # The refusal is sanitized: the tool surface names availability
        # only (no payload, no marker content).
        run_events = await _read_run_events(acc_app, acc_jar, run_id)
        tool_results = [
            event
            for event in run_events
            if event["type"] == "agent.tool_result"
        ]
        assert tool_results, "the refused attempt must reach the run stream"
        assert "Unknown tool name" in json.dumps(tool_results[0]["data"])
        assert "d1" not in json.dumps(tool_results[0]["data"])

        # -- DELEGATION-CARRIED: a child pinned to the research server ----
        objective_b = await create_objective(acc_app, acc_jar, server_fx)
        oid_b = uuid.UUID(objective_b.json()["objective_id"])
        expanded = await call_app(
            acc_app,
            "POST",
            f"/api/v1/objectives/{oid_b}/scope",
            json_body={
                "server_id": str(server_fx),
                "expected_revision": 1,
                "add_server_ids": [str(server_research)],
            },
            headers=_mutation_headers(acc_jar),
            cookies=acc_jar,
        )
        assert expanded.status == 200, expanded.body
        parent_run_id = str(uuid.uuid4())
        await seed_v2_delegation_parent(
            bootstrapped_url,
            parent_run_id=parent_run_id,
            server_id=str(server_fx),
            objective_id=str(oid_b),
            task="Delegate the mutation attempt to the research target.",
            children=[
                {
                    "task": "mission=denychild Attempt the mutation marker dc1.",
                    "server_ids": [str(server_research)],
                    "capabilities": ["fixture.priv"],
                }
            ],
            parent_grants=("fixture.read", "fixture.priv"),
        )
        parent_run = uuid.UUID(parent_run_id)
        _wait_state(bootstrapped_url, parent_run, {"succeeded"}, 240)

        # The child's attempt produced ZERO effect on the research server.
        assert acc_scenario.count("begin:fixture.priv:dc1") == 0
        assert acc_scenario.count("effect:fixture.priv:dc1") == 0

        # The parent aggregated the child resolution; the tree read is 200.
        parent_view = await call_app(
            acc_app,
            "GET",
            f"/api/v1/chat/runs/{parent_run}",
            headers=_cookies(acc_jar),
        )
        assert parent_view.status == 200, parent_view.body
        children = parent_view.json().get("children") or []
        assert len(children) == 1
        child_run = uuid.UUID(children[0]["run_id"])
        child_state = _wait_state(
            bootstrapped_url, child_run, {"succeeded", "failed"}, 10
        )
        # The child run itself completes honestly (the denial is content,
        # not a crash); the MUTATION never happened.
        assert child_state == "succeeded"
        child_events_types = _run_event_types(bootstrapped_url, child_run)
        assert any(
            event_type == "agent.tool_result" for event_type in child_events_types
        )
        journal_child = await objective_journal_projection(
            acc_app, acc_jar, oid_b, server_fx
        )
        child_denied = [
            action
            for action in (journal_child.get("actions") or [])
            if action.get("decision") == "denied"
        ]
        # §8.1: the journaled ACTION carries the dispatcher's decision and
        # the secret-sanitized payload projection (markers are not
        # secrets); the canary must never appear.
        assert child_denied, "the delegation-carried denial must be journaled"
        assert all(
            action.get("decision") == "denied" for action in child_denied
        )
        assert CANARY_TOKEN not in json.dumps(child_denied)

        record_metrics(
            "RV2-DENY",
            {
                "scenario": "RV2-DENY",
                "direct_effects": 0,
                "delegated_effects": 0,
                "direct_refusal_layer": "tool_surface_visibility",
                "delegated_denial_layer": "dispatcher_authorization",
                "research_server_write_grants": 0,
                "research_server_read_grants": 1,
                "journaled_denied_actions": len(child_denied),
            },
        )


# ---------------------------------------------------------------------------
# RV2-DELEGATE — two scoped targets, CAS scope, narrowing, receipts (AC3)
# ---------------------------------------------------------------------------


class TestRV2Delegate:
    async def test_two_target_delegation_with_scope_and_budget_narrowing(
        self,
        acc_app: FastAPI,
        acc_jar: dict[str, str],
        acc_scenario: AccScenario,
        bootstrapped_url: str,
    ) -> None:
        """Real parent/child v2 runs over TWO scoped fx targets (LABELLED
        deterministic model script; the delegation ENGINE, objective CAS,
        budget ledger and facade are the REAL production machinery):
        operator scope expansion, child capability/budget subsets hold,
        both children settle Succeeded, the parent aggregates receipts,
        and the facade read is 200."""
        server_a = await create_profile(
            acc_app, acc_jar, "rv2-acc-del-a", capabilities=["fixture.read"]
        )
        server_b = await create_profile(
            acc_app, acc_jar, "rv2-acc-del-b", capabilities=["fixture.read"]
        )
        server_c = await create_profile(
            acc_app, acc_jar, "rv2-acc-del-c", capabilities=["fixture.read"]
        )
        await put_grants_for(
            acc_app,
            acc_jar,
            {FX_READ: [server_a, server_b, server_c]},
        )
        acc_scenario.write_model_script(
            {
                "child": [
                    {"tool_call": {"name": "fixture_read", "args": {"marker": "cd1"}}},
                    {"text": "scoped read complete"},
                ],
            }
        )
        acc_scenario.spawn_worker(bootstrapped_url)

        objective = await create_objective(acc_app, acc_jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])
        # Operator scope expansion through the REAL CAS route.
        expanded = await call_app(
            acc_app,
            "POST",
            f"/api/v1/objectives/{oid}/scope",
            json_body={
                "server_id": str(server_a),
                "expected_revision": 1,
                "add_server_ids": [str(server_b), str(server_c)],
            },
            headers=_mutation_headers(acc_jar),
            cookies=acc_jar,
        )
        assert expanded.status == 200, expanded.body
        assert expanded.json()["revision"] == 2
        stale = await call_app(
            acc_app,
            "POST",
            f"/api/v1/objectives/{oid}/scope",
            json_body={
                "server_id": str(server_a),
                "expected_revision": 1,
                "add_server_ids": [str(uuid.uuid4())],
            },
            headers=_mutation_headers(acc_jar),
            cookies=acc_jar,
        )
        assert stale.status == 409
        assert _error(stale)["code"] == "revision_conflict"

        # The REAL delegation engine, seeded through the REAL store seams:
        # the operator scope is ALREADY expanded (CAS above), the parent
        # row carries a narrowing root budget, and the two ChildSpecV2
        # children pin one scoped target each with a READ-ONLY capability
        # subset. The payload-v2 document is validated by its ONE producer
        # inside the seed; children route through agent.run_v2.
        parent_run_id = str(uuid.uuid4())
        await seed_v2_delegation_parent(
            bootstrapped_url,
            parent_run_id=parent_run_id,
            server_id=str(server_a),
            objective_id=str(oid),
            task="Delegate the scoped reads across the objective targets.",
            children=[
                {
                    "task": "mission=child Read target B (marker cd1).",
                    "server_ids": [str(server_b)],
                    "capabilities": ["fixture.read"],
                },
                {
                    "task": "mission=child Read target C (marker cd1).",
                    "server_ids": [str(server_c)],
                    "capabilities": ["fixture.read"],
                },
            ],
            parent_grants=("fixture.read",),
            root_budget={
                "v": BUDGET_PAYLOAD_VERSION,
                "max_depth": 2,
                "max_children": 4,
                "max_active": 2,
                "max_steps": 2000,
                "max_total_tokens": 2_000_000,
            },
        )
        parent_run = uuid.UUID(parent_run_id)
        _wait_state(bootstrapped_url, parent_run, {"succeeded"}, 240)

        # Both children ran their scoped read EXACTLY once on their own
        # target: two distinct effects, no cross-target leakage.
        assert acc_scenario.count("begin:fixture.read:cd1") == 2
        assert acc_scenario.count("effect:fixture.read:cd1") == 2

        # The parent aggregated typed child RESOLUTIONS with receipts (§7.5).
        resolutions_doc = _run_event_data(
            bootstrapped_url, parent_run, "agent.children_resolved"
        )
        resolutions = resolutions_doc["children"]
        assert [r["state"] for r in resolutions] == ["succeeded", "succeeded"]
        assert all(
            isinstance(r.get("receipt"), dict) and r["receipt"] for r in resolutions
        ), "succeeded children carry their own summary receipts"

        # The REAL budget ledger reserved and settled the tree (C05 r7).
        parent_types = _run_event_types(bootstrapped_url, parent_run)
        assert "agent.budget_reserved" in parent_types
        assert "agent.budget_charged" in parent_types
        assert "agent.budget_released" in parent_types

        # The facade read is clean: 200 with both children (D2 seam).
        parent_view = await call_app(
            acc_app,
            "GET",
            f"/api/v1/chat/runs/{parent_run}",
            headers=_cookies(acc_jar),
        )
        assert parent_view.status == 200, parent_view.body
        assert len(parent_view.json().get("children") or []) == 2

        # Objective bindings: the parent AND both children are bound.
        runs_page = await call_app(
            acc_app,
            "GET",
            f"/api/v1/objectives/{oid}/runs?server_id={server_a}",
            headers=_cookies(acc_jar),
        )
        assert runs_page.status == 200
        assert runs_page.json()["total"] == 3

        record_metrics(
            "RV2-DELEGATE",
            {
                "scenario": "RV2-DELEGATE",
                "children": 2,
                "child_states": ["succeeded", "succeeded"],
                "effects": 2,
                "budget_events": ["reserved", "charged", "released"],
                "facade_children": 2,
                "scope_expansion_revision": 2,
                "scope_cas_conflict": 409,
            },
        )


# ---------------------------------------------------------------------------
# RV2-APPROVAL-RESTART — approval deferral survives a real restart (AC3)
# ---------------------------------------------------------------------------


class TestRV2ApprovalRestart:
    async def test_approval_survives_worker_restart_and_binds_payload(
        self,
        acc_app: FastAPI,
        acc_jar: dict[str, str],
        acc_scenario: AccScenario,
        bootstrapped_url: str,
    ) -> None:
        """The approval-deferred effect survives a REAL worker SIGKILL
        mid-park; the payload-bound decision wakes a FRESH worker and the
        operation executes its EXACT payload exactly once; an altered
        payload decision is refused. LABELLED deterministic fault-injection
        script drives the deferral."""
        from mai.capabilities.dispatch import canonical_payload_hash

        server = await create_profile(
            acc_app, acc_jar, "rv2-acc-appr", capabilities=["fixture.priv"]
        )
        await put_grants_for(acc_app, acc_jar, {FX_PRIV: [server]})
        acc_scenario.write_model_script(
            {
                "main": [
                    {"tool_call": {"name": "fixture_priv", "args": {"marker": "ap1"}}},
                    {"text": "approved operation complete"},
                ],
            }
        )
        acc_scenario.spawn_worker(bootstrapped_url)

        objective = await create_objective(acc_app, acc_jar, server)
        oid = uuid.UUID(objective.json()["objective_id"])
        key = uuid.uuid4()
        submitted = await submit_v2_conversation(
            acc_app,
            acc_jar,
            server,
            oid,
            client_request_id=key,
            message="Apply the privileged fixture change (marker ap1).",
            mode="ask",
        )
        assert submitted.status == 201, submitted.body
        run_id = uuid.UUID(submitted.json()["run_id"])
        _wait_state(bootstrapped_url, run_id, {"waiting_approval"}, FX_RUN_TIMEOUT)
        step = _read_step_row(bootstrapped_url, run_id, 1)
        assert step is not None
        assert step["receipt"]["reason"] == "approval_required_but_absent"
        approval_id = uuid.UUID(step["receipt"]["approval_id"])
        payload_hash = step["receipt"]["payload_hash"]
        assert payload_hash == canonical_payload_hash({"marker": "ap1"})
        assert acc_scenario.count("effect:fixture.priv:ap1") == 0

        # REAL restart mid-park: SIGKILL the worker, spawn a fresh one.
        acc_scenario.kill_worker()
        acc_scenario.spawn_worker(bootstrapped_url)

        # An altered payload/target decision is a structured refusal; the
        # approval row is untouched and the fresh worker stays parked.
        mismatch = await call_app(
            acc_app,
            "POST",
            f"/api/v1/approvals/{approval_id}/decision",
            json_body={
                "capability_id": FX_PRIV,
                "server_id": str(server),
                "payload_hash": canonical_payload_hash({"marker": "OTHER"}),
                "policy_revision": 1,
            },
            headers=_mutation_headers(acc_jar),
            cookies=acc_jar,
        )
        assert mismatch.status == 409
        assert _error(mismatch)["code"] == "payload_mismatch"
        assert acc_scenario.count("effect:fixture.priv:ap1") == 0

        # The EXACT payload-bound decision wakes the fresh worker.
        decided = await call_app(
            acc_app,
            "POST",
            f"/api/v1/approvals/{approval_id}/decision",
            json_body={
                "capability_id": FX_PRIV,
                "server_id": str(server),
                "payload_hash": payload_hash,
                "policy_revision": 1,
            },
            headers=_mutation_headers(acc_jar),
            cookies=acc_jar,
        )
        assert decided.status == 200, decided.body
        _wait_state(bootstrapped_url, run_id, {"succeeded"}, FX_RUN_TIMEOUT)

        # Exactly once: the approved operation ran precisely one effect,
        # on the resumed attempt row (stride 1024), approval spent.
        assert acc_scenario.count("effect:fixture.priv:ap1") == 1
        resume_row = _read_step_row(bootstrapped_url, run_id, 1025)
        assert resume_row is not None
        assert resume_row["state"] == "succeeded"
        assert resume_row["receipt"]["approval_spent"] is True

        record_metrics(
            "RV2-APPROVAL-RESTART",
            {
                "scenario": "RV2-APPROVAL-RESTART",
                "worker_restart": "SIGKILL mid-park",
                "payload_hash": payload_hash,
                "mismatch_denied": 409,
                "effects_after_restart": 1,
                "resume_seq": 1025,
                "approval_spent": True,
            },
        )


# ---------------------------------------------------------------------------
# RV2-REPLAY (+ steering/cancellation) — durability under restart (AC3)
# ---------------------------------------------------------------------------


class TestRV2ReplaySteerCancel:
    async def test_crash_restart_replay_keeps_checkpoints_no_duplicate_effect(
        self,
        acc_app: FastAPI,
        acc_jar: dict[str, str],
        acc_scenario: AccScenario,
        bootstrapped_url: str,
    ) -> None:
        """SIGKILL mid-effect (inside the dispatch fence): recovery keeps
        the SETTLED checkpoints (the first model round never re-executes),
        the recovered attempt translates to ``outcome_unknown`` (the frozen
        guard rule — an uncertain effect is never blindly replayed), the
        run RE-PARKS on the reconciliation wait, and the explicit
        classification wakes it to completion with ZERO re-dispatched
        effects. LABELLED fault injection (the gate + a real crash)."""
        server = await create_profile(
            acc_app, acc_jar, "rv2-acc-replay", capabilities=["fixture.read"]
        )
        await put_grants_for(acc_app, acc_jar, {FX_READ: [server]})
        acc_scenario.write_model_script(
            {
                "main": [
                    {"tool_call": {"name": "fixture_read", "args": {"marker": "r1"}}},
                    {"text": "post-recovery completion"},
                ],
            }
        )
        acc_scenario.set_behavior({"fixture.read:r1": "gate"})
        acc_scenario.spawn_worker(bootstrapped_url)

        objective = await create_objective(acc_app, acc_jar, server)
        oid = uuid.UUID(objective.json()["objective_id"])
        key = uuid.uuid4()
        submitted = await submit_v2_conversation(
            acc_app,
            acc_jar,
            server,
            oid,
            client_request_id=key,
            message="Read the fixture marker r1 and report.",
        )
        run_id = uuid.UUID(submitted.json()["run_id"])
        _wait_state(bootstrapped_url, run_id, {"running"}, 60)
        _wait_until(
            lambda: acc_scenario.count("begin:fixture.read:r1") == 1,
            60,
            "the gated attempt beginning",
        )

        # The crash window: SIGKILL inside the dispatch fence (the effect
        # never applied).
        acc_scenario.kill_worker()
        assert acc_scenario.count("effect:fixture.read:r1") == 0
        acc_scenario.spawn_worker(bootstrapped_url)

        # The recovered run RE-PARKS on the reconciliation wait: the
        # fence-window crash is outcome_unknown, never a blind replay.
        _wait_state(bootstrapped_url, run_id, {"reconciling"}, 240)
        assert acc_scenario.count("effect:fixture.read:r1") == 0
        assert acc_scenario.count("model_call") == 1, (
            "the settled first model round must never re-execute"
        )

        # The operator classifies the uncertain attempt; the parked run
        # wakes and completes WITHOUT re-dispatching the effect.
        resolved = await call_app(
            acc_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/reconcile",
            json_body={
                "server_id": str(server),
                "classifications": [{"seq": 1, "verdict": "confirm_applied"}],
            },
            headers=_mutation_headers(acc_jar),
            cookies=acc_jar,
        )
        assert resolved.status == 200, resolved.body
        _wait_state(bootstrapped_url, run_id, {"succeeded"}, 240)
        assert acc_scenario.count("effect:fixture.read:r1") == 0
        types_after = _run_event_types(bootstrapped_url, run_id)
        assert types_after.count("chat.submitted") == 1

        # The persisted stream keeps unique sequences (no replay dupes).
        events = await _read_run_events(acc_app, acc_jar, run_id)
        seqs = [event["seq"] for event in events]
        assert len(seqs) == len(set(seqs)), "replay must not duplicate sequences"

        record_metrics(
            "RV2-REPLAY",
            {
                "scenario": "RV2-REPLAY",
                "crash": "SIGKILL inside the dispatch fence",
                "settled_round_reexecuted": False,
                "re_parked_wait": "reconciling",
                "duplicate_effects": 0,
                "duplicate_sequences": 0,
                "classification_wake": "confirm_applied",
            },
        )

    async def test_steering_and_pause_and_cancel_honor_state_rules(
        self,
        acc_app: FastAPI,
        acc_jar: dict[str, str],
        acc_scenario: AccScenario,
        bootstrapped_url: str,
    ) -> None:
        """Live steering reaches the model at the safe boundary; a pause
        wins its boundary and the next effect never begins; a parked
        (approval-deferred) run cancels without executing. LABELLED fault
        injection scripts throughout."""
        server = await create_profile(
            acc_app, acc_jar, "rv2-acc-ctl", capabilities=["fixture.read", "fixture_priv"]
        )
        await put_grants_for(
            acc_app, acc_jar, {FX_READ: [server], FX_PRIV: [server]}
        )

        # -- steering: consumed exactly once at the safe boundary ---------
        acc_scenario.write_model_script(
            {
                "main": [
                    {"tool_call": {"name": "fixture_read", "args": {"marker": "s1"}}},
                    {"text": "steered investigation complete"},
                ],
            }
        )
        acc_scenario.set_behavior({"fixture.read:s1": "gate"})
        acc_scenario.spawn_worker(bootstrapped_url)
        objective = await create_objective(acc_app, acc_jar, server)
        oid = uuid.UUID(objective.json()["objective_id"])
        key = uuid.uuid4()
        submitted = await submit_v2_conversation(
            acc_app,
            acc_jar,
            server,
            oid,
            client_request_id=key,
            message="Read the fixture marker s1 and report.",
        )
        run_id = uuid.UUID(submitted.json()["run_id"])
        _wait_state(bootstrapped_url, run_id, {"running"}, 60)
        _wait_until(
            lambda: acc_scenario.count("begin:fixture.read:s1") == 1, 60, "gate"
        )
        steered = await call_app(
            acc_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/steer",
            json_body={
                "conversation_id": str(key),
                "server_id": str(server),
                "action": "steer",
                "message": "focus on aspect-alpha please",
                "client_request_id": str(uuid.uuid4()),
            },
            headers=_mutation_headers(acc_jar),
            cookies=acc_jar,
        )
        assert steered.status == 201, steered.body
        acc_scenario.release_gate("fixture.read", "s1")
        _wait_state(bootstrapped_url, run_id, {"succeeded"}, FX_RUN_TIMEOUT)
        assert acc_scenario.count("user_marker:aspect-alpha") == 1

        # -- pause: wins its boundary, the next effect never begins ------
        # (each phase respawns: the deterministic script + behavior set
        # loads at worker composition, one worker per phase.)
        acc_scenario.stop()
        acc_scenario.write_model_script(
            {
                "main": [
                    {"tool_call": {"name": "fixture_read", "args": {"marker": "p1"}}},
                    {"tool_call": {"name": "fixture_read", "args": {"marker": "p2"}}},
                    {"text": "paused point"},
                    {"text": "resumed complete"},
                    {"text": "slack entry"},
                ],
            }
        )
        acc_scenario.set_behavior({"fixture.read:p1": "gate"})
        acc_scenario.spawn_worker(bootstrapped_url)
        key2 = uuid.uuid4()
        submitted2 = await submit_v2_conversation(
            acc_app,
            acc_jar,
            server,
            oid,
            client_request_id=key2,
            message="Read the fixture marker p1 then p2 and report.",
        )
        run2 = uuid.UUID(submitted2.json()["run_id"])
        _wait_state(bootstrapped_url, run2, {"running"}, 60)
        _wait_until(
            lambda: acc_scenario.count("begin:fixture.read:p1") == 1, 60, "gate p1"
        )
        paused = await call_app(
            acc_app,
            "POST",
            f"/api/v1/chat/conversations/{key2}/steer",
            json_body={
                "conversation_id": str(key2),
                "server_id": str(server),
                "action": "pause",
                "client_request_id": str(uuid.uuid4()),
            },
            headers=_mutation_headers(acc_jar),
            cookies=acc_jar,
        )
        assert paused.status == 201, paused.body
        acc_scenario.release_gate("fixture.read", "p1")
        _wait_state(bootstrapped_url, run2, {"succeeded"}, FX_RUN_TIMEOUT)
        assert acc_scenario.count("begin:fixture.read:p2") == 0
        summary = _run_event_data(bootstrapped_url, run2, "agent.completed")
        assert summary.get("stopped_by") == "pause"

        # -- cancel: a parked approval-deferred run never executes -------
        acc_scenario.stop()
        acc_scenario.write_model_script(
            {
                "main": [
                    {"tool_call": {"name": "fixture_priv", "args": {"marker": "c1"}}},
                    {"text": "never reached"},
                ],
            }
        )
        acc_scenario.set_behavior({})
        acc_scenario.spawn_worker(bootstrapped_url)
        key3 = uuid.uuid4()
        submitted3 = await submit_v2_conversation(
            acc_app,
            acc_jar,
            server,
            oid,
            client_request_id=key3,
            message="Attempt the privileged change (marker c1).",
            mode="ask",
        )
        run3 = uuid.UUID(submitted3.json()["run_id"])
        _wait_state(bootstrapped_url, run3, {"waiting_approval"}, FX_RUN_TIMEOUT)
        cancelled = await call_app(
            acc_app,
            "POST",
            f"/api/v1/chat/runs/{run3}/cancel",
            json_body={"cancel_children": True},
            headers=_mutation_headers(acc_jar),
            cookies=acc_jar,
        )
        assert cancelled.status in (200, 202), cancelled.body
        _wait_state(bootstrapped_url, run3, {"cancelled"}, FX_RUN_TIMEOUT)
        assert acc_scenario.count("effect:fixture.priv:c1") == 0

        record_metrics(
            "RV2-REPLAY-CONTROLS",
            {
                "scenario": "RV2-REPLAY-CONTROLS",
                "steer_consumed_once": 1,
                "pause_stopped_by": "pause",
                "next_effect_after_pause": 0,
                "cancelled_parked_effects": 0,
            },
        )


# ---------------------------------------------------------------------------
# RV2-UNKNOWN — lost receipt, reconciling park, classification gate (AC3)
# ---------------------------------------------------------------------------


class TestRV2Unknown:
    async def test_lost_receipt_parks_reconciling_until_classified(
        self,
        acc_app: FastAPI,
        acc_jar: dict[str, str],
        acc_scenario: AccScenario,
        bootstrapped_url: str,
    ) -> None:
        """An effect whose receipt is lost becomes outcome_unknown, the run
        parks reconciling, a blind resume is refused (409), and only the
        explicit operator classification wakes it — never re-dispatching
        the uncertain effect. LABELLED fault injection."""
        server = await create_profile(
            acc_app, acc_jar, "rv2-acc-unknown", capabilities=["fixture.read"]
        )
        await put_grants_for(acc_app, acc_jar, {FX_READ: [server]})
        acc_scenario.write_model_script(
            {
                "main": [
                    {"tool_call": {"name": "fixture_read", "args": {"marker": "u1"}}},
                    {"text": "after reconciliation"},
                ],
            }
        )
        acc_scenario.set_behavior({"fixture.read:u1": "unknown_status"})
        acc_scenario.spawn_worker(bootstrapped_url)

        objective = await create_objective(acc_app, acc_jar, server)
        oid = uuid.UUID(objective.json()["objective_id"])
        key = uuid.uuid4()
        submitted = await submit_v2_conversation(
            acc_app,
            acc_jar,
            server,
            oid,
            client_request_id=key,
            message="Read the fixture marker u1 and report.",
        )
        run_id = uuid.UUID(submitted.json()["run_id"])
        _wait_state(bootstrapped_url, run_id, {"reconciling"}, FX_RUN_TIMEOUT)
        row = _read_step_row(bootstrapped_url, run_id, 1)
        assert row is not None
        assert row["state"] == "outcome_unknown"
        assert acc_scenario.count("effect:fixture.read:u1") == 1

        blind = await call_app(
            acc_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/resume",
            json_body={"server_id": str(server), "message": "just continue"},
            headers=_mutation_headers(acc_jar),
            cookies=acc_jar,
        )
        assert blind.status == 409
        assert _error(blind)["code"] == "classification_required"

        # The uncertain effect is journaled as an explicit unknown.
        journal = await objective_journal_projection(acc_app, acc_jar, oid, server)
        assert journal.get("unknowns"), "the unknown effect must be journaled"

        resolved = await call_app(
            acc_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/reconcile",
            json_body={
                "server_id": str(server),
                "classifications": [{"seq": 1, "verdict": "confirm_applied"}],
            },
            headers=_mutation_headers(acc_jar),
            cookies=acc_jar,
        )
        assert resolved.status == 200, resolved.body
        assert resolved.json()["results"][0]["verdict"] == "final_succeeded"
        _wait_state(bootstrapped_url, run_id, {"succeeded"}, FX_RUN_TIMEOUT)
        # The uncertain effect was NEVER re-dispatched (exactly once).
        assert acc_scenario.count("effect:fixture.read:u1") == 1

        # Secrets/redaction canary: the fx outputs carry the synthetic
        # canary; it must never surface in the run stream, the journal
        # projection, or the agent-activity (objective events) surface.
        run_events = await _read_run_events(acc_app, acc_jar, run_id)
        assert all(
            CANARY_TOKEN not in json.dumps(event, default=str)
            for event in run_events
        )
        assert CANARY_TOKEN not in json.dumps(journal, default=str)
        objective_events = await call_app(
            acc_app,
            "GET",
            f"/api/v1/objectives/{oid}/events?server_id={server}&limit=100",
            headers=_cookies(acc_jar),
        )
        assert objective_events.status == 200
        assert CANARY_TOKEN not in objective_events.body.decode("utf-8")

        record_metrics(
            "RV2-UNKNOWN",
            {
                "scenario": "RV2-UNKNOWN",
                "step_state": "outcome_unknown",
                "blind_resume": 409,
                "classification": "confirm_applied",
                "redispatches": 0,
                "canary_leaks": 0,
            },
        )


# ---------------------------------------------------------------------------
# RV2-LEGACY — truthful legacy markers, replay, drain inventory (AC3)
# ---------------------------------------------------------------------------


class TestRV2Legacy:
    async def test_legacy_truthful_markers_real_replay_and_drain(
        self,
        acc_app: FastAPI,
        acc_jar: dict[str, str],
        acc_scenario: AccScenario,
        bootstrapped_url: str,
    ) -> None:
        """Legacy conversations stay readable with honest ``is_legacy``
        markers; a REAL legacy run replays to completion by the FROZEN
        ``agent.run`` worker (never reinterpreted as v2); the drain
        inventory reports exact legacy/v2 counts and flips readiness once
        drained."""
        server = await create_profile(
            acc_app, acc_jar, "rv2-acc-legacy", capabilities=["fixture.read"]
        )
        await put_grants_for(acc_app, acc_jar, {FX_READ: [server]})

        # A v2 conversation bound to an objective, queued (no worker yet).
        objective = await create_objective(acc_app, acc_jar, server)
        oid = uuid.UUID(objective.json()["objective_id"])
        v2_key = uuid.uuid4()
        v2 = await submit_v2_conversation(
            acc_app, acc_jar, server, oid, client_request_id=v2_key
        )
        assert v2.status == 201

        # A historical legacy conversation, seeded through the internal
        # store seams and enqueued so the REAL frozen ``agent.run``
        # worker replays it (no public legacy selector).
        legacy_key = uuid.uuid4()
        legacy_run = await seed_legacy_conversation(
            bootstrapped_url,
            server,
            client_request_id=legacy_key,
            message="mission=legacy Answer this legacy turn directly.",
            enqueue=True,
        )
        assert legacy_run == str(legacy_key)

        v2_doc = (
            await call_app(
                acc_app,
                "GET",
                f"/api/v1/chat/conversations/{v2_key}?server_id={server}",
                headers=_cookies(acc_jar),
            )
        ).json()
        assert v2_doc["is_legacy"] is False
        assert v2_doc["handler_name"] == "agent.run_v2"

        legacy_doc = (
            await call_app(
                acc_app,
                "GET",
                f"/api/v1/chat/conversations/{legacy_key}?server_id={server}",
                headers=_cookies(acc_jar),
            )
        ).json()
        assert legacy_doc["is_legacy"] is True
        assert legacy_doc["handler_name"] == "agent.run"
        assert legacy_doc["objective_ids"] == []

        # The drain inventory reports the exact unsettled inventories
        # honestly: BOTH queued runs are unsettled, so the legacy window
        # is NOT ready to retire.
        drain = await call_app(
            acc_app,
            "GET",
            "/api/v1/objectives/drain-status",
            headers=_cookies(acc_jar),
        )
        assert drain.status == 200
        doc = drain.json()
        assert doc["legacy"]["handler"] == "agent.run"
        assert doc["v2"]["handler"] == "agent.run_v2"
        assert doc["legacy"]["unsettled"] == 1
        assert doc["v2"]["unsettled"] == 1
        assert doc["ready_to_retire_legacy"] is False

        # The legacy run REPLAYS by its FROZEN worker to a truthful
        # completion. The canonical submit links a conversation, so the
        # frozen intake runner interprets the turn: the scripted model
        # answers through the frozen intake's own ``final_result`` output
        # tool with the REAL accepted source message id (P1 plain-answer
        # completion). The script is the labelled deterministic stand-in;
        # the handler, engine and transcript are the real frozen ones.
        with psycopg.connect(bootstrapped_url) as conn:
            source_row = conn.execute(
                "SELECT message_id FROM conversation_messages "
                "WHERE client_request_id = %s AND role = 'user' LIMIT 1",
                (legacy_key,),
            ).fetchone()
        assert source_row is not None, "the accepted turn must carry its message"
        acc_scenario.write_model_script(
            {
                "main": [{"text": "v2 side answer"}],
                "legacy": [
                    {
                        "tool_call": {
                            "name": "final_result",
                            "args": {
                                "response": {
                                    "schema_version": CONVERSATION_SCHEMA_VERSION,
                                    "source_message_id": str(source_row[0]),
                                    "turn_kind": "answer",
                                    "understanding": "Answering the legacy turn.",
                                    "answer_text": "legacy answer delivered",
                                }
                            },
                        }
                    }
                ],
            }
        )
        acc_scenario.spawn_worker(bootstrapped_url)
        legacy_run_id = uuid.UUID(legacy_run)
        _wait_state(
            bootstrapped_url, legacy_run_id, {"succeeded"}, FX_RUN_TIMEOUT
        )
        legacy_events = await _read_run_events(acc_app, acc_jar, legacy_run_id)
        legacy_types = [event["type"] for event in legacy_events]
        assert "chat.submitted" in legacy_types
        assert "agent.completed" in legacy_types

        # The v2 run settles by its own handler on the SAME worker; both
        # inventories drain and the readiness flips honestly.
        _wait_state(
            bootstrapped_url, uuid.UUID(v2.json()["run_id"]), {"succeeded"}, FX_RUN_TIMEOUT
        )
        drained = await call_app(
            acc_app,
            "GET",
            "/api/v1/objectives/drain-status",
            headers=_cookies(acc_jar),
        )
        assert drained.status == 200
        final_doc = drained.json()
        assert final_doc["legacy"]["unsettled"] == 0
        assert final_doc["v2"]["unsettled"] == 0
        assert final_doc["legacy"]["total"] >= 1 and final_doc["v2"]["total"] >= 1
        assert final_doc["ready_to_retire_legacy"] is True

        record_metrics(
            "RV2-LEGACY",
            {
                "scenario": "RV2-LEGACY",
                "legacy_replay_state": "succeeded",
                "legacy_marker_is_legacy": True,
                "legacy_handler": "agent.run",
                "drain_legacy_unsettled": 0,
                "ready_to_retire_legacy": True,
            },
        )
