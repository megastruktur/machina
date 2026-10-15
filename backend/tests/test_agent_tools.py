"""Focused tests for the agent tool bridge (card ``06-tool-bridge``).

Contracts.md **C06 rule 4** + **C03 rules 2–7**: registered capabilities
become PydanticAI tools only for the capabilities the execution context
enables, and every execution — model-facing or resume — passes the one
``CapabilityDispatcher`` boundary. The dispatcher's own gate/credential/
recheck machinery is proven by ``test_dispatch.py``; this suite proves
the bridge cannot bypass it and is honest about outcomes:

- exposure is exactly enabled ∧ registered (AC1);
- a schema mismatch is denied *before* dispatch — no approval consumed,
  no effect, no exception into the model loop (AC2);
- the deferred-approval result carries the durable binding (payload
  hash, policy revision) and the resume path consumes exactly one
  real approval decision through the real ``ApprovalStore`` (AC2,
  disposable Postgres fixture);
- tool failures surface as structured results, never exceptions
  (AC2);
- real PydanticAI runs (deterministic fake LLM, C06 rule 6) request
  bridged tools and the boundary answers: granted scope executes, denied
  scope denies without effect.

Boundary discipline: the executor here is a benign in-process
recorder whose invocation IS the effect (refusing sentinel for every
denial path); the real-SSH transport claim is the P06 gate assembly's
(``06-verify``). The Postgres-backed seam tests run the real
``ApprovalStore`` through the real Alembic chain, mirroring
``test_dispatch.py``.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel

from mai.agents.tools import (
    BridgeError,
    BridgeErrorCode,
    CapabilityToolBridge,
    TOOL_RESULT_SCHEMA_VERSION,
    ToolResult,
    ToolStatus,
    tool_name_for,
)
from mai.capabilities.dispatch import (
    CapabilityCall,
    CapabilityDispatcher,
    ExecutorResult,
    canonical_payload_hash,
)
from mai.capabilities.host_shell import descriptor as host_shell_descriptor
from mai.capabilities.logs import descriptor as logs_descriptor
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalRequest, ApprovalStore
from mai.policy.evaluate import ExecutionContext, Policy, PolicyMode

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: C10 rule 2 fixture namespace: the fixture ``.env`` (synthetic secret)
#: and the effect counter live here; nothing outside it is touched.
FXT_ROOT = Path("/tmp/mai-fxt-06-tool-bridge")

SECRET_NAME = "MAI_FXT_TOKEN"
SECRET_VALUE = "fxt-synthetic-token-value-06b47d"

SERVER_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
SERVER_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
RUN_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
PRINCIPAL = "admin"

#: The P04 read capability: the granted-scope execution path needs no
#: approval seam, so the pure-Postgres-free tests exercise a full
#: dispatch → effect with the REAL integrated descriptor.
READ_CAPABILITY = "host.logs"

#: A privileged fixture shaped like ``host.shell``: the schema gate and
#: the deferred-approval path need an input schema with a required
#: string field, and ``privileged`` always requires an approval.
PRIVILEGED_CAPABILITY = "host.shell"

UNREGISTERED_CAPABILITY = "ghost.cap"

#: Inert synthetic credential material — the redaction sweep target.
REFERENCE_PAYLOAD = {"command": "echo status"}


def _fixture_descriptor(
    identifier: str,
    effect_class: str,
    *,
    input_schema: dict[str, object] | None = None,
    version: str = "1.0.0",
) -> dict[str, object]:
    """A registry-shaped descriptor with a meaningful input schema."""
    if input_schema is None:
        input_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["command"],
            "properties": {
                "command": {"type": "string", "minLength": 1},
            },
        }
    return {
        "identifier": identifier,
        "version": version,
        "config_schema": None,
        "input_schema": input_schema,
        "output_schema": {"type": "object"},
        "execution_environments": ["linux_ssh"],
        "effect_class": effect_class,
        "resource_scope": [] if effect_class == "read" else ["target.exec"],
        "required_grants": [],
    }


def _numbers_descriptor(identifier: str) -> dict[str, object]:
    return _fixture_descriptor(
        identifier,
        "read",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["count"],
            "properties": {
                "count": {"type": "number", "minimum": 1, "maximum": 10},
                "exact": {"type": "integer"},
            },
        },
    )


def _unsupported_keyword_descriptor(identifier: str) -> dict[str, object]:
    return _fixture_descriptor(
        identifier,
        "read",
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "allOf": [{"type": "string"}],
                },
            },
        },
    )


def _registry_with(*documents: dict[str, object]) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for document in documents:
        registry.register(parse_descriptor(document))
    return registry


def _real_registry() -> CapabilityRegistry:
    """The real P04 producers plus fixture-only capabilities."""
    registry = CapabilityRegistry()
    registry.register(host_shell_descriptor())
    registry.register(logs_descriptor())
    registry.register(parse_descriptor(_fixture_descriptor("deploy.service", "mutate")))
    return registry


def _grants(*pairs: tuple[str, uuid.UUID]):
    grants: dict[str, frozenset[uuid.UUID]] = {}
    for capability_id, server_id in pairs:
        grants.setdefault(capability_id, frozenset())
        grants[capability_id] = grants[capability_id] | {server_id}
    return grants


def _default_grants(registry: CapabilityRegistry):
    """Current grants: every registered capability scoped to SERVER_A."""
    return _grants(
        *[
            (descriptor.identifier, SERVER_A)
            for descriptor in registry.list_capabilities()
        ]
    )


def _policy(revision: int = 7, denied: tuple[str, ...] = ()) -> Policy:
    return Policy(revision=revision, denied_capabilities=frozenset(denied))


def _context(
    *,
    server_id: uuid.UUID = SERVER_A,
    run_id: uuid.UUID = RUN_1,
    allowed: tuple[str, ...] = (
        READ_CAPABILITY,
        PRIVILEGED_CAPABILITY,
        UNREGISTERED_CAPABILITY,
    ),
    mode: PolicyMode = PolicyMode.DELEGATED,
    references: tuple[str, ...] = (),
) -> ExecutionContext:
    return ExecutionContext(
        principal=PRINCIPAL,
        server_id=server_id,
        run_id=run_id,
        parent_scope=None,
        profile_revision="prof-1",
        prompt_revision="prompt-1",
        credential_references=references,
        allowed_capabilities=allowed,
        mode=mode,
    )


class _Recorder:
    """The benign effect: invocation appends to the counter file."""

    def __init__(self, counter: Path, output: str = "done", ok: bool = True):
        self.counter = counter
        self.calls: list[CapabilityCall] = []
        self._output = output
        self._ok = ok

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        self.calls.append(call)
        count = (
            int(self.counter.read_text()) if self.counter.exists() else 0
        ) + 1
        self.counter.write_text(str(count))
        return ExecutorResult(ok=self._ok, output=self._output)


def _refusing_executor():
    async def _execute(call: CapabilityCall) -> ExecutorResult:
        raise AssertionError(f"executor invoked for a denied call: {call}")

    return _execute


def _dispatcher(
    registry: CapabilityRegistry,
    approvals: ApprovalStore,
    *,
    executor=_refusing_executor,
    grants=None,
    policy=lambda: _policy(),
    adopted=lambda server_id: True,
    output_budget_bytes: int = 64 * 1024,
) -> CapabilityDispatcher:
    if grants is None:
        grants = lambda: _default_grants(registry)  # noqa: E731
    return CapabilityDispatcher(
        registry=registry,
        approvals=approvals,
        grants=grants,
        policy=policy,
        adopted=adopted,
        credentials=lambda: EnvResolver(FXT_ROOT / "fixture.env"),
        executor=executor,  # type: ignore[arg-type]
        output_budget_bytes=output_budget_bytes,
    )


def _unused_approvals() -> ApprovalStore:
    """A real store over a lazily-connecting engine (never dialed)."""
    _, sessions = create_app_engine(
        "postgresql://unused:unused@127.0.0.1:1/unused"
    )
    return ApprovalStore(sessions)


def _bridge(
    registry: CapabilityRegistry,
    dispatcher: CapabilityDispatcher,
    context: ExecutionContext | None = None,
) -> CapabilityToolBridge:
    return CapabilityToolBridge(
        registry=registry,
        dispatcher=dispatcher,
        context=context if context is not None else _context(),
    )


def _tool_of(bridge: CapabilityToolBridge, capability_id: str):
    for tool in bridge.tools:
        if tool.name == tool_name_for(capability_id):
            return tool
    raise AssertionError(f"no tool for {capability_id}")


def _result_of(text: str) -> dict[str, object]:
    parsed = json.loads(text)
    assert isinstance(parsed, dict)
    return parsed


# ---------------------------------------------------------------------------
# Live fixture: real disposable Postgres + real chain + real .env fixture
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(ALEMBIC_INI))


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through head."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the approval-resume claims need "
            "a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                "DROP SCHEMA public CASCADE; CREATE SCHEMA public"
            )
            conn.commit()
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture
async def sessions(migrated_url: str):
    engine, factory = create_app_engine(migrated_url)
    yield factory
    await engine.dispose()


@pytest.fixture
def store(sessions) -> ApprovalStore:
    return ApprovalStore(sessions)


@pytest.fixture
def clean_approvals(migrated_url: str):
    """Empty approval state per test (real DB)."""
    with psycopg.connect(migrated_url) as conn:
        conn.execute("TRUNCATE approvals")
        conn.commit()
    yield


@pytest.fixture(scope="session")
def fxt_dir() -> Path:
    """C10-rule-2 fixture namespace: synthetic .env + effect counter."""
    import shutil

    FXT_ROOT.mkdir(mode=0o700, exist_ok=True)
    os.chmod(FXT_ROOT, 0o700)
    for child in FXT_ROOT.iterdir():
        if child.name == "url.env":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    (FXT_ROOT / "fixture.env").write_text(
        f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8"
    )
    os.chmod(FXT_ROOT / "fixture.env", 0o600)
    yield FXT_ROOT
    # The evidence workflow keeps url.env (DB fixture binding); every
    # other disposable artifact is removed by the suite itself.
    for child in FXT_ROOT.iterdir():
        if child.name == "url.env":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


@pytest.fixture
def counter(fxt_dir: Path) -> Path:
    path = fxt_dir / "effect.counter"
    path.unlink(missing_ok=True)
    return path


# ---------------------------------------------------------------------------
# Construction and tool naming
# ---------------------------------------------------------------------------


def test_wiring_rejects_wrong_component_types(fxt_dir: Path) -> None:
    context = _context()
    real_registry = _real_registry()
    real_dispatcher = _dispatcher(real_registry, _unused_approvals())
    with pytest.raises(BridgeError) as registry_error:
        CapabilityToolBridge(
            registry=object(),  # type: ignore[arg-type]
            dispatcher=real_dispatcher,
            context=context,
        )
    assert registry_error.value.code is BridgeErrorCode.INVALID_WIRING
    with pytest.raises(BridgeError) as dispatcher_error:
        CapabilityToolBridge(
            registry=real_registry,
            dispatcher=object(),  # type: ignore[arg-type]
            context=context,
        )
    assert dispatcher_error.value.code is BridgeErrorCode.INVALID_WIRING
    with pytest.raises(BridgeError) as context_error:
        CapabilityToolBridge(
            registry=real_registry,
            dispatcher=real_dispatcher,
            context=object(),  # type: ignore[arg-type]
        )
    assert context_error.value.code is BridgeErrorCode.INVALID_WIRING


def test_tool_name_mapping_is_provider_safe() -> None:
    assert tool_name_for(PRIVILEGED_CAPABILITY) == "host_shell"
    assert tool_name_for(READ_CAPABILITY) == "host_logs"
    weird = "a.b-c_d.e" * 20
    name = tool_name_for(weird)
    assert len(name) <= 64
    assert set(name) <= set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    )


def test_tool_name_collision_refuses_construction(
    fxt_dir: Path,
) -> None:
    registry = _registry_with(
        _fixture_descriptor("a.b", "read"),
        _fixture_descriptor("a_b", "read"),
    )
    dispatcher = _dispatcher(registry, _unused_approvals())
    context = _context(allowed=("a.b", "a_b"))
    with pytest.raises(BridgeError) as error:
        _bridge(registry, dispatcher, context)
    assert error.value.code is BridgeErrorCode.TOOL_NAME_COLLISION


def test_enabled_but_unregistered_is_not_exposed(fxt_dir: Path) -> None:
    registry = _registry_with(_fixture_descriptor(READ_CAPABILITY, "read"))
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals()),
        _context(allowed=(READ_CAPABILITY, UNREGISTERED_CAPABILITY)),
    )
    assert bridge.enabled_capabilities == (READ_CAPABILITY,)
    assert [tool.name for tool in bridge.tools] == [
        tool_name_for(READ_CAPABILITY)
    ]


# ---------------------------------------------------------------------------
# AC1: exposure is exactly enabled ∧ registered; no bypass seam
# ---------------------------------------------------------------------------


def test_exposes_exactly_enabled_registered_descriptors(
    fxt_dir: Path,
) -> None:
    registry = _real_registry()
    bridge = _bridge(registry, _dispatcher(registry, _unused_approvals()))
    assert bridge.enabled_capabilities == (
        READ_CAPABILITY,
        PRIVILEGED_CAPABILITY,
    )
    names = {tool.name for tool in bridge.tools}
    assert names == {"host_logs", "host_shell"}
    # The published argument schema is the descriptor's, verbatim.
    logs_tool = _tool_of(bridge, READ_CAPABILITY)
    definition = logs_tool.tool_def
    assert definition.parameters_json_schema == json.loads(
        json.dumps(logs_descriptor().input_schema)
    )
    # The privileged tool discloses the approval requirement up front.
    shell_tool = _tool_of(bridge, PRIVILEGED_CAPABILITY)
    assert "approval" in (shell_tool.tool_def.description or "")


@pytest.mark.anyio
async def test_disabled_capability_has_no_dispatch_path(
    fxt_dir: Path,
) -> None:
    registry = _real_registry()
    bridge = _bridge(registry, _dispatcher(registry, _unused_approvals()))
    assert bridge.descriptor_for("deploy.service") is None
    with pytest.raises(BridgeError) as error:
        await bridge.dispatch_once("deploy.service", {"op": "deploy"})
    assert error.value.code is BridgeErrorCode.CAPABILITY_NOT_ENABLED


# ---------------------------------------------------------------------------
# AC2: schema mismatch never dispatches
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_schema_mismatch_denies_without_dispatch(
    fxt_dir: Path, counter: Path
) -> None:
    recorder = _Recorder(counter)
    registry = _real_registry()
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=recorder),
    )
    tool = _tool_of(bridge, PRIVILEGED_CAPABILITY)
    mismatches = (
        {"command": 42},  # wrong type
        {"command": "echo", "extra": True},  # additionalProperties: False
        {},  # missing required
    )
    for payload in mismatches:
        result = _result_of(await tool.function(**payload))
        assert result["status"] == ToolStatus.DENIED.value
        assert result["reason"] == BridgeErrorCode.PAYLOAD_SCHEMA_MISMATCH.value
        assert result["effect"] == "not_attempted"
        assert result["policy_revision"] is None
        assert result["effect_class"] is None
        assert result["error"]["code"] == "payload_schema_mismatch"
    assert recorder.calls == []
    assert not counter.exists()


@pytest.mark.anyio
async def test_unsupported_schema_keyword_fails_closed(
    fxt_dir: Path, counter: Path
) -> None:
    recorder = _Recorder(counter)
    identifier = "strict.read"
    registry = _registry_with(_unsupported_keyword_descriptor(identifier))
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=recorder),
        _context(allowed=(identifier,)),
    )
    # The resume path raises (host misuse / contract drift, never a
    # decision); the model-facing path answers with a denial document.
    with pytest.raises(BridgeError) as resume_error:
        await bridge.dispatch_once(identifier, {"command": "ok"})
    assert resume_error.value.code is BridgeErrorCode.PAYLOAD_SCHEMA_MISMATCH
    result = _result_of(
        await _tool_of(bridge, identifier).function(**{"command": "ok"})
    )
    assert result["status"] == ToolStatus.DENIED.value
    assert "unsupported keyword" in str(result["error"]["detail"])
    assert recorder.calls == []


@pytest.mark.anyio
async def test_non_canonical_payload_rejected_before_state(
    fxt_dir: Path, counter: Path
) -> None:
    recorder = _Recorder(counter)
    identifier = "free.form"
    registry = _registry_with(
        _fixture_descriptor(identifier, "read", input_schema={})
    )
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=recorder),
        _context(allowed=(identifier,)),
    )
    result = _result_of(
        await _tool_of(bridge, identifier).function(**{"x": object()})
    )
    assert result["status"] == ToolStatus.DENIED.value
    assert result["error"]["code"] == "invalid_request"
    assert result["payload_hash"] == "non_canonical"
    assert recorder.calls == []


@pytest.mark.anyio
async def test_schema_gate_type_matrix(fxt_dir: Path, counter: Path) -> None:
    """Bool is not a number; integral floats are integers; enums are
    type-aware; patterns search; nested containers validate by path."""
    identifier = "typed.read"
    registry = _registry_with(_numbers_descriptor(identifier))
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=_Recorder(counter)),
        _context(allowed=(identifier,)),
    )
    dispatch = bridge.dispatch_once
    # bool never satisfies number/integer even though Python says int.
    with pytest.raises(BridgeError):
        await dispatch(identifier, {"count": True})
    with pytest.raises(BridgeError):
        await dispatch(identifier, {"count": 1, "exact": True})
    # integral floats are valid JSON integers; fractional are not.
    assert (await dispatch(identifier, {"count": 1.0, "exact": 2.0})).allowed
    with pytest.raises(BridgeError):
        await dispatch(identifier, {"count": 1, "exact": 2.5})
    # numeric bounds.
    with pytest.raises(BridgeError):
        await dispatch(identifier, {"count": 11})
    with pytest.raises(BridgeError):
        await dispatch(identifier, {"count": 0})

    enum_id = "choiced.read"
    registry.register(
        parse_descriptor(
            _fixture_descriptor(
                enum_id,
                "read",
                input_schema={
                    "type": "object",
                    "properties": {
                        "mode": {"enum": ["a", 1, True, None]},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "nested": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {"deep": {"type": "boolean"}},
                        },
                    },
                },
            )
        )
    )
    enum_bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=_Recorder(counter)),
        _context(allowed=(enum_id,)),
    )
    edispatch = enum_bridge.dispatch_once
    assert (await edispatch(enum_id, {"mode": 1})).allowed
    with pytest.raises(BridgeError):
        await edispatch(enum_id, {"mode": 2})
    with pytest.raises(BridgeError) as nested_error:
        await edispatch(enum_id, {"nested": {"deep": "yes"}})
    assert ".deep" in nested_error.value.detail
    with pytest.raises(BridgeError) as item_error:
        await edispatch(enum_id, {"tags": ["ok", 3]})
    assert "[1]" in item_error.value.detail


# ---------------------------------------------------------------------------
# AC1: granted scope executes through the boundary; outcomes are honest
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_granted_read_executes_and_maps_outcome(
    fxt_dir: Path, counter: Path
) -> None:
    recorder = _Recorder(counter, output=f"token={SECRET_VALUE} tail")
    registry = _real_registry()
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=recorder),
        _context(allowed=(READ_CAPABILITY,), references=(SECRET_NAME,)),
    )
    payload = {"source": "journal", "service": "mai-api"}
    result = _result_of(
        await _tool_of(bridge, READ_CAPABILITY).function(**payload)
    )
    assert result["status"] == ToolStatus.EXECUTED.value
    assert result["allowed"] is True
    assert result["effect"] == "executed"
    assert result["policy_revision"] == 7
    assert result["effect_class"] == "read"
    assert result["payload_hash"] == canonical_payload_hash(payload)
    assert result["output"]["text"] == "token=***redacted*** tail"
    assert len(recorder.calls) == 1
    assert recorder.calls[0].payload == payload
    assert recorder.calls[0].context.run_id == RUN_1
    # The model-facing document is value-free.
    assert SECRET_VALUE not in json.dumps(result)


@pytest.mark.anyio
async def test_denied_scope_denies_without_effect(
    fxt_dir: Path, counter: Path
) -> None:
    recorder = _Recorder(counter)
    registry = _real_registry()
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=recorder),
        _context(allowed=(READ_CAPABILITY,), server_id=SERVER_B),
    )
    result = _result_of(
        await _tool_of(bridge, READ_CAPABILITY).function(
            **{"source": "journal", "service": "mai-api"}
        )
    )
    assert result["status"] == ToolStatus.DENIED.value
    assert result["reason"] == "server_scope_mismatch"
    assert result["effect"] == "not_attempted"
    assert recorder.calls == []
    assert not counter.exists()


@pytest.mark.anyio
async def test_executor_crash_surfaces_unknown_effect_status(
    fxt_dir: Path, counter: Path
) -> None:
    class _Boom:
        async def __call__(self, call: CapabilityCall) -> ExecutorResult:
            raise RuntimeError("boom with secret fxt-synthetic-token-value-06b47d")

    registry = _real_registry()
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=_Boom()),
        _context(allowed=(READ_CAPABILITY,), references=(SECRET_NAME,)),
    )
    result = _result_of(
        await _tool_of(bridge, READ_CAPABILITY).function(
            **{"source": "journal", "service": "mai-api"}
        )
    )
    assert result["status"] == ToolStatus.UNKNOWN_EFFECT.value
    assert result["allowed"] is False
    assert result["reason"] == "executor_failed"
    assert result["executor_ok"] is None
    assert result["output"] is None


@pytest.mark.anyio
async def test_infrastructure_failure_returns_structured_denial(
    fxt_dir: Path, counter: Path
) -> None:
    def _broken_grants():
        raise RuntimeError("db down")

    registry = _real_registry()
    bridge = _bridge(
        registry,
        _dispatcher(
            registry, _unused_approvals(), executor=_Recorder(counter),
            grants=_broken_grants,
        ),
        _context(allowed=(READ_CAPABILITY,)),
    )
    result = _result_of(
        await _tool_of(bridge, READ_CAPABILITY).function(
            **{"source": "journal", "service": "mai-api"}
        )
    )
    assert result["status"] == ToolStatus.DENIED.value
    assert result["reason"] == "grants_unavailable"
    # A dispatcher decision is a decision, not a bridge error: the
    # error field stays empty for boundary denials.
    assert result["error"] is None
    assert not counter.exists()


@pytest.mark.anyio
async def test_approval_store_outage_never_raises_from_tool(
    fxt_dir: Path, counter: Path
) -> None:
    """An infrastructure failure that propagates out of the dispatcher
    (e.g. the approval store's database being down) still becomes one
    structured denial — the model loop never sees an exception, and the
    exception message never surfaces (type name only)."""

    class _OutageDispatcher(CapabilityDispatcher):
        async def dispatch(self, **kwargs):  # type: ignore[override]
            raise RuntimeError("approval db down: connect failed")

    registry = _real_registry()
    bridge = _bridge(
        registry,
        _OutageDispatcher(
            registry=registry,
            approvals=_unused_approvals(),
            grants=lambda: _default_grants(registry),
            policy=lambda: _policy(),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(FXT_ROOT / "fixture.env"),
            executor=_Recorder(counter),
        ),
        _context(allowed=(READ_CAPABILITY,)),
    )
    result = _result_of(
        await _tool_of(bridge, READ_CAPABILITY).function(
            **{"source": "journal", "service": "mai-api"}
        )
    )
    assert result["status"] == ToolStatus.DENIED.value
    assert result["effect"] == "not_attempted"
    assert result["error"] == {
        "code": "dispatch_failed",
        "detail": "RuntimeError",
    }
    assert "approval db down" not in json.dumps(result)
    assert not counter.exists()


def test_tool_result_json_shape_is_frozen_and_value_free(
    fxt_dir: Path,
) -> None:
    registry = _real_registry()
    bridge = _bridge(registry, _dispatcher(registry, _unused_approvals()))
    tool = _tool_of(bridge, READ_CAPABILITY)
    # The rejected document pins the frozen key order/shape.
    document = json.loads(
        ToolResult.rejected(
            context=_context(),
            capability_id=READ_CAPABILITY,
            payload_hash="0" * 64,
            error_code="payload_schema_mismatch",
            error_detail="$.x: property is not allowed",
        ).to_json_str()
    )
    assert list(document) == [
        "schema_version",
        "capability_id",
        "server_id",
        "run_id",
        "status",
        "allowed",
        "reason",
        "effect",
        "payload_hash",
        "policy_revision",
        "effect_class",
        "approval_id",
        "approval_spent",
        "approval_code",
        "executor_ok",
        "output",
        "error",
    ]
    assert document["schema_version"] == TOOL_RESULT_SCHEMA_VERSION
    assert SECRET_VALUE not in json.dumps(document)


# ---------------------------------------------------------------------------
# AC2: deferred approval — durable binding, one-use resume (real Postgres)
# ---------------------------------------------------------------------------


def _approval_request(
    capability_id: str,
    payload_hash: str,
    policy_revision: int,
    *,
    server_id: uuid.UUID = SERVER_A,
) -> ApprovalRequest:
    return ApprovalRequest(
        capability_id=capability_id,
        server_id=server_id,
        payload_hash=payload_hash,
        policy_revision=policy_revision,
        created_by=PRINCIPAL,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


@pytest.mark.anyio
async def test_deferred_approval_persists_binding_and_resumes_once(
    store: ApprovalStore, fxt_dir: Path, counter: Path, clean_approvals
) -> None:
    recorder = _Recorder(counter)
    registry = _real_registry()
    bridge = _bridge(
        registry,
        _dispatcher(registry, store, executor=recorder),
        _context(
            allowed=(PRIVILEGED_CAPABILITY,), mode=PolicyMode.DELEGATED
        ),
    )
    payload = {"command": "systemctl status mai"}
    # 1. The model-facing call defers: nothing ran, binding recorded.
    result = _result_of(
        await _tool_of(bridge, PRIVILEGED_CAPABILITY).function(**payload)
    )
    assert result["status"] == ToolStatus.APPROVAL_REQUIRED.value
    assert result["reason"] == "approval_required_but_absent"
    assert result["effect"] == "not_attempted"
    assert result["policy_revision"] == 7
    assert result["approval_id"] is None
    assert result["approval_spent"] is False
    assert recorder.calls == []
    assert not counter.exists()

    # 2. The run layer persists the deferral via jobs (its lane); the
    # human decision is created bound to the recorded hash + revision.
    request = _approval_request(
        PRIVILEGED_CAPABILITY,
        str(result["payload_hash"]),
        int(result["policy_revision"]),  # type: ignore[arg-type]
    )
    approval = await store.create(request)

    # 3. Resume through the same seam: exactly one effect, once.
    outcome = await bridge.dispatch_once(
        PRIVILEGED_CAPABILITY, payload, approval_id=approval.approval_id
    )
    assert outcome.allowed and outcome.effect == "executed"
    assert outcome.approval_spent is True
    assert len(recorder.calls) == 1
    assert counter.read_text() == "1"

    # 4. The decision was one-use: a replay is denied, no second effect.
    replay = await bridge.dispatch_once(
        PRIVILEGED_CAPABILITY, payload, approval_id=approval.approval_id
    )
    assert not replay.allowed
    assert replay.reason == "approval_rejected"
    assert replay.approval_code == "already_consumed"
    assert counter.read_text() == "1"


@pytest.mark.anyio
async def test_resume_with_altered_payload_denies_and_preserves_decision(
    store: ApprovalStore, fxt_dir: Path, counter: Path, clean_approvals
) -> None:
    recorder = _Recorder(counter)
    registry = _real_registry()
    bridge = _bridge(registry, _dispatcher(registry, store, executor=recorder))
    payload = {"command": "systemctl status mai"}
    approval = await store.create(
        _approval_request(
            PRIVILEGED_CAPABILITY,
            canonical_payload_hash(payload),
            _policy().revision,
        )
    )
    altered = {"command": "systemctl stop mai"}
    denied = await bridge.dispatch_once(
        PRIVILEGED_CAPABILITY, altered, approval_id=approval.approval_id
    )
    assert not denied.allowed
    assert denied.reason == "approval_rejected"
    assert denied.approval_code == "payload_mismatch"
    assert recorder.calls == []
    # The decision survives the wrong payload: the true resume works.
    outcome = await bridge.dispatch_once(
        PRIVILEGED_CAPABILITY, payload, approval_id=approval.approval_id
    )
    assert outcome.allowed and outcome.effect == "executed"
    assert counter.read_text() == "1"


@pytest.mark.anyio
async def test_resume_after_policy_revision_bump_denies(
    store: ApprovalStore, fxt_dir: Path, counter: Path, clean_approvals
) -> None:
    recorder = _Recorder(counter)
    registry = _real_registry()
    revision = {"value": 7}
    bridge = _bridge(
        registry,
        _dispatcher(
            registry,
            store,
            executor=recorder,
            policy=lambda: _policy(revision["value"]),
        ),
    )
    payload = {"command": "systemctl status mai"}
    approval = await store.create(
        _approval_request(
            PRIVILEGED_CAPABILITY,
            canonical_payload_hash(payload),
            revision["value"],
        )
    )
    revision["value"] = 8
    denied = await bridge.dispatch_once(
        PRIVILEGED_CAPABILITY, payload, approval_id=approval.approval_id
    )
    assert not denied.allowed
    assert denied.reason == "approval_rejected"
    assert denied.approval_code == "revision_mismatch"
    assert denied.approval_spent is False
    assert recorder.calls == []
    assert not counter.exists()


@pytest.mark.anyio
async def test_mismatched_resume_never_consumes_the_decision(
    store: ApprovalStore, fxt_dir: Path, counter: Path, clean_approvals
) -> None:
    recorder = _Recorder(counter)
    registry = _real_registry()
    bridge = _bridge(registry, _dispatcher(registry, store, executor=recorder))
    payload = {"command": "systemctl status mai"}
    approval = await store.create(
        _approval_request(
            PRIVILEGED_CAPABILITY,
            canonical_payload_hash(payload),
            _policy().revision,
        )
    )
    # A schema-violating resume fails at the bridge, before dispatch.
    with pytest.raises(BridgeError) as error:
        await bridge.dispatch_once(
            PRIVILEGED_CAPABILITY,
            {"command": "ok", "extra": 1},
            approval_id=approval.approval_id,
        )
    assert error.value.code is BridgeErrorCode.PAYLOAD_SCHEMA_MISMATCH
    # The pending decision was untouched by the failed resume.
    pending = await store.get(approval.approval_id)
    assert pending is not None and pending.consumed_at is None
    outcome = await bridge.dispatch_once(
        PRIVILEGED_CAPABILITY, payload, approval_id=approval.approval_id
    )
    assert outcome.allowed and outcome.effect == "executed"


# ---------------------------------------------------------------------------
# Real PydanticAI runs over the bridge (deterministic fake LLM, C06 rule 6)
# ---------------------------------------------------------------------------


def _scripted_agent(
    bridge: CapabilityToolBridge, script: list[ModelResponse]
) -> Agent:
    step = {"index": 0}

    async def model_fn(messages: list[ModelMessage], info) -> ModelResponse:
        response = script[min(step["index"], len(script) - 1)]
        step["index"] += 1
        return response

    return Agent(model=FunctionModel(model_fn), instructions="probe", tools=list(bridge.tools))


@pytest.mark.anyio
async def test_agent_run_granted_tool_call_executes_through_boundary(
    fxt_dir: Path, counter: Path
) -> None:
    recorder = _Recorder(counter, output="journal lines here")
    registry = _real_registry()
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=recorder),
        _context(allowed=(READ_CAPABILITY,)),
    )
    agent = _scripted_agent(
        bridge,
        [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="host_logs",
                        args={"source": "journal", "service": "mai-api"},
                    )
                ]
            ),
            ModelResponse(parts=[TextPart("reported")]),
        ],
    )
    with capture_run_messages() as messages:
        run = await agent.run("tail the api logs")
    assert run.output == "reported"
    returns = [
        part
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart)
    ]
    assert len(returns) == 1
    document = _result_of(str(returns[0].content))
    assert document["status"] == ToolStatus.EXECUTED.value
    assert document["effect"] == "executed"
    assert document["output"]["text"] == "journal lines here"
    assert len(recorder.calls) == 1
    assert counter.read_text() == "1"


@pytest.mark.anyio
async def test_agent_run_denied_scope_gets_denial_and_continues(
    fxt_dir: Path, counter: Path
) -> None:
    recorder = _Recorder(counter)
    registry = _real_registry()
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=recorder),
        _context(allowed=(READ_CAPABILITY,), server_id=SERVER_B),
    )
    agent = _scripted_agent(
        bridge,
        [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="host_logs",
                        args={"source": "journal", "service": "mai-api"},
                    )
                ]
            ),
            ModelResponse(parts=[TextPart("denied, reported")]),
        ],
    )
    with capture_run_messages() as messages:
        run = await agent.run("tail the api logs")
    assert run.output == "denied, reported"
    returns = [
        part
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart)
    ]
    document = _result_of(str(returns[0].content))
    assert document["status"] == ToolStatus.DENIED.value
    assert document["reason"] == "server_scope_mismatch"
    assert recorder.calls == []
    assert not counter.exists()


@pytest.mark.anyio
async def test_agent_run_schema_mismatch_is_denial_not_dispatch(
    fxt_dir: Path, counter: Path
) -> None:
    """PydanticAI schema-declared tools skip argument validation; the
    bridge's gate catches what the framework skipped."""
    recorder = _Recorder(counter)
    registry = _real_registry()
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=recorder),
        _context(allowed=(READ_CAPABILITY,)),
    )
    agent = _scripted_agent(
        bridge,
        [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="host_logs",
                        args={"source": "journal", "service": 42},
                    )
                ]
            ),
            ModelResponse(parts=[TextPart("corrected course")]),
        ],
    )
    with capture_run_messages() as messages:
        run = await agent.run("tail the api logs")
    assert run.output == "corrected course"
    returns = [
        part
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart)
    ]
    document = _result_of(str(returns[0].content))
    assert document["status"] == ToolStatus.DENIED.value
    assert document["error"]["code"] == "payload_schema_mismatch"
    assert recorder.calls == []


@pytest.mark.anyio
async def test_agent_run_defers_privileged_call_for_approval(
    fxt_dir: Path, counter: Path
) -> None:
    recorder = _Recorder(counter)
    registry = _real_registry()
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=recorder),
        _context(allowed=(PRIVILEGED_CAPABILITY,)),
    )
    agent = _scripted_agent(
        bridge,
        [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="host_shell",
                        args={"command": "systemctl status mai"},
                    )
                ]
            ),
            ModelResponse(parts=[TextPart("waiting for approval")]),
        ],
    )
    with capture_run_messages() as messages:
        run = await agent.run("restart the service")
    assert run.output == "waiting for approval"
    returns = [
        part
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart)
    ]
    document = _result_of(str(returns[0].content))
    assert document["status"] == ToolStatus.APPROVAL_REQUIRED.value
    assert document["payload_hash"] == canonical_payload_hash(
        {"command": "systemctl status mai"}
    )
    assert document["policy_revision"] == 7
    assert recorder.calls == []
    assert not counter.exists()
