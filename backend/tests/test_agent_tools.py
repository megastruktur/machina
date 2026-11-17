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

from mai.agents.output_safety import REDACTED, project_text
from mai.agents.tools import (
    SECRET_RAW_VALUE_FIELDS,
    SECRET_STORE_CAPABILITY_ID,
    BridgeError,
    BridgeErrorCode,
    CapabilityToolBridge,
    TOOL_RESULT_PROJECTION_SCHEMA_VERSION,
    TOOL_RESULT_SCHEMA_VERSION,
    ToolResult,
    ToolStatus,
    project_tool_result,
    tool_name_for,
)
from mai.capabilities.dispatch import (
    REDACTED as DISPATCH_REDACTED,
)
from mai.contracts.tool_results import (
    RESULT_DATA_VALUE_MAX_CHARS,
    DomainOutcome,
    EffectCertainty,
    ExecutionStatus,
)
from mai.capabilities.dispatch import (
    CapabilityCall,
    CapabilityDispatcher,
    ExecutorResult,
    canonical_payload_hash,
)
from mai.capabilities.host_file_read import (
    FileReadError,
    FileReadErrorCode,
)
from mai.capabilities.host_shell import descriptor as host_shell_descriptor
from mai.capabilities.logs import descriptor as logs_descriptor
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.plugins.builtin_onepassword import (
    CAPABILITIES as ONEPASSWORD_CAPABILITIES,
    SECRET_STORE_CAPABILITY,
)
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
        count = (int(self.counter.read_text()) if self.counter.exists() else 0) + 1
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
    _, sessions = create_app_engine("postgresql://unused:unused@127.0.0.1:1/unused")
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
            conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
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
    assert [tool.name for tool in bridge.tools] == [tool_name_for(READ_CAPABILITY)]


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
    registry = _registry_with(_fixture_descriptor(identifier, "read", input_schema={}))
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=recorder),
        _context(allowed=(identifier,)),
    )
    result = _result_of(await _tool_of(bridge, identifier).function(**{"x": object()}))
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
    result = _result_of(await _tool_of(bridge, READ_CAPABILITY).function(**payload))
    assert result["status"] == ToolStatus.EXECUTED.value
    assert result["allowed"] is True
    assert result["effect"] == "executed"
    assert result["policy_revision"] == 7
    assert result["effect_class"] == "read"
    assert result["payload_hash"] == canonical_payload_hash(payload)
    assert result["output"]["text"] == "token=***redacted*** tail"
    # FRR T2: executed results carry no error block.
    assert result["error"] is None
    assert len(recorder.calls) == 1
    assert recorder.calls[0].payload == payload
    assert recorder.calls[0].context.run_id == RUN_1
    # The model-facing document is value-free.
    assert SECRET_VALUE not in json.dumps(result)


@pytest.mark.anyio
async def test_denied_scope_denies_without_effect(fxt_dir: Path, counter: Path) -> None:
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
    # FRR T2: the unknown-effect crash window carries NO error code —
    # only deterministic pre-effect denials do.
    assert result["error"] is None


@pytest.mark.anyio
async def test_pre_effect_denial_carries_the_stable_error_code(
    fxt_dir: Path, counter: Path
) -> None:
    """FRR T2: an executor raising a ``PreEffectError`` (the T1
    ``FileReadError(PATH_DENIED)`` shape) lands a ``denied`` tool result
    whose ``error`` block carries the stable refusal token — the model
    sees WHY the call was denied — while the exception detail text never
    surfaces."""
    counter.write_text("0")

    class _Denied:
        async def __call__(self, call: CapabilityCall) -> ExecutorResult:
            raise FileReadError(
                FileReadErrorCode.PATH_DENIED,
                "outside every allowed file_read_roots prefix",
            )

    registry = _real_registry()
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=_Denied()),
        _context(allowed=(READ_CAPABILITY,)),
    )
    result = _result_of(
        await _tool_of(bridge, READ_CAPABILITY).function(
            **{"source": "journal", "service": "mai-api"}
        )
    )
    assert result["status"] == ToolStatus.DENIED.value
    assert result["effect"] == "not_attempted"
    assert result["reason"] == "executor_failed"
    assert result["error"] == {"code": "path_denied", "detail": None}
    assert "outside every allowed" not in json.dumps(result)


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
            registry,
            _unused_approvals(),
            executor=_Recorder(counter),
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


def test_tool_result_json_shape_is_frozen_and_value_free() -> None:
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
        "projection",
    ]
    assert document["schema_version"] == TOOL_RESULT_SCHEMA_VERSION
    # The projection block is versioned on its own clock (act 19).
    assert (
        document["projection"]["schema_version"]
        == TOOL_RESULT_PROJECTION_SCHEMA_VERSION
    )
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
        _context(allowed=(PRIVILEGED_CAPABILITY,), mode=PolicyMode.ASK),
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
    bridge = _bridge(
        registry,
        _dispatcher(registry, store, executor=recorder),
        _context(allowed=(PRIVILEGED_CAPABILITY,), mode=PolicyMode.ASK),
    )
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
        _context(allowed=(PRIVILEGED_CAPABILITY,), mode=PolicyMode.ASK),
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
    bridge = _bridge(
        registry,
        _dispatcher(registry, store, executor=recorder),
        _context(allowed=(PRIVILEGED_CAPABILITY,), mode=PolicyMode.ASK),
    )
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


def _scripted_agent(bridge: CapabilityToolBridge, script: list[ModelResponse]) -> Agent:
    step = {"index": 0}

    async def model_fn(messages: list[ModelMessage], info) -> ModelResponse:
        response = script[min(step["index"], len(script) - 1)]
        step["index"] += 1
        return response

    return Agent(
        model=FunctionModel(model_fn), instructions="probe", tools=list(bridge.tools)
    )


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
        _context(allowed=(PRIVILEGED_CAPABILITY,), mode=PolicyMode.ASK),
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


# ---------------------------------------------------------------------------
# act 19-tool-results: compact model-facing projection (E3/S1/AC1/AC2)
# ---------------------------------------------------------------------------


def _tool_result(
    *,
    status: ToolStatus = ToolStatus.EXECUTED,
    output_text: str | None = None,
    executor_ok: bool | None = True,
    reason: str = "allowed",
    effect: str = "executed",
    error_code: str | None = None,
    error_detail: str | None = None,
) -> ToolResult:
    """One directly-constructed result with exactly the run-layer kwargs.

    Mirrors ``mai.agents.run._result_document``: the run layer builds
    :class:`ToolResult` from outcome fields without passing any
    projection input, so the projection must derive everything from
    these twenty fields alone (versioned/adapted compatibility, AC2).
    """
    return ToolResult(
        schema_version=TOOL_RESULT_SCHEMA_VERSION,
        capability_id="fixture.cap",
        server_id=SERVER_A,
        run_id=RUN_1,
        status=status,
        allowed=status is ToolStatus.EXECUTED,
        reason=reason,
        effect=effect,
        payload_hash="0" * 64,
        policy_revision=7,
        effect_class="read",
        approval_id=None,
        approval_spent=False,
        approval_code=None,
        executor_ok=None if status is ToolStatus.UNKNOWN_EFFECT else executor_ok,
        output_text=output_text,
        output_truncated=None if output_text is None else False,
        output_omitted_bytes=None if output_text is None else 0,
        error_code=error_code,
        error_detail=error_detail,
    )


def _projection_of(result: ToolResult) -> dict[str, object]:
    projected = project_tool_result(result)
    assert projected["schema_version"] == TOOL_RESULT_PROJECTION_SCHEMA_VERSION
    return projected


def test_shell_wrapper_exit_zero_stays_unknown_and_uncertain() -> None:
    """E3: a wrapper exit code alone is never domain evidence."""
    result = _tool_result(
        output_text=json.dumps(
            {
                "status": "succeeded",
                "exit_code": 0,
                "stdout": "all checks look fine",
                "stderr": "",
            }
        )
    )
    projection = _projection_of(result)
    assert projection["execution_status"] == ExecutionStatus.SUCCEEDED.value
    assert projection["domain_outcome"] == DomainOutcome.UNKNOWN.value
    assert projection["effect_certainty"] == EffectCertainty.UNCERTAIN.value
    assert "unproven" in str(projection["retry_hint"])


def test_exit_zero_with_failed_subcheck_is_failure_never_certain() -> None:
    """E3/AC1: exit 0 + a failed subcheck stays failure/uncertain — the
    failed subcheck is never promoted to certain success."""
    result = _tool_result(
        output_text=json.dumps(
            {
                "status": "succeeded",
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "subchecks": [
                    {"name": "unit_active", "passed": True},
                    {"name": "healthz_200", "passed": False},
                ],
            }
        )
    )
    projection = _projection_of(result)
    assert projection["execution_status"] == ExecutionStatus.SUCCEEDED.value
    assert projection["domain_outcome"] == DomainOutcome.FAILURE.value
    assert projection["effect_certainty"] != EffectCertainty.CERTAIN.value
    assert projection["effect_certainty"] == EffectCertainty.UNCERTAIN.value
    subchecks = projection["subchecks"]
    assert isinstance(subchecks, list) and len(subchecks) == 2
    assert {"name": "healthz_200", "passed": False} in subchecks
    assert "failure" in str(projection["retry_hint"])


def test_structured_ok_envelope_proves_certain_success() -> None:
    """Structured tools report their semantic outcome explicitly; a
    proven execution plus an explicit ``ok`` envelope is the one
    ``certain``-success combination."""
    result = _tool_result(
        output_text=json.dumps(
            {
                "status": "ok",
                "items": ["a", "b"],
                "limit": 10,
                "offset": 0,
                "total": 2,
                "has_more": False,
            }
        )
    )
    projection = _projection_of(result)
    assert projection["execution_status"] == ExecutionStatus.SUCCEEDED.value
    assert projection["domain_outcome"] == DomainOutcome.SUCCESS.value
    assert projection["effect_certainty"] == EffectCertainty.CERTAIN.value


def test_refused_envelope_is_failed_execution_and_uncertain_failure() -> None:
    """A structured refusal is explicit negative evidence: the technical
    execution failed, the domain outcome is failure — and per AC1 the
    certainty stays ``uncertain`` (only positive success evidence is
    ever ``certain``)."""
    result = _tool_result(
        output_text=json.dumps(
            {
                "status": "refused",
                "code": "vault_not_allowed",
                "message": "credential refused by policy",
                "capability_id": "fixture.cap",
            }
        ),
        executor_ok=False,
    )
    projection = _projection_of(result)
    assert projection["execution_status"] == ExecutionStatus.FAILED.value
    assert projection["domain_outcome"] == DomainOutcome.FAILURE.value
    assert projection["effect_certainty"] == EffectCertainty.UNCERTAIN.value
    assert projection["error_code"] == "vault_not_allowed"
    assert projection["error_detail"] == "credential refused by policy"


def test_unknown_effect_claims_nothing() -> None:
    """S2 honesty at the projection layer: an unproven effect stays
    ``outcome_unknown``/``unknown``/``unknown`` — no termination claim,
    no invented outcome."""
    projection = _projection_of(_tool_result(status=ToolStatus.UNKNOWN_EFFECT))
    assert projection["execution_status"] == ExecutionStatus.OUTCOME_UNKNOWN.value
    assert projection["domain_outcome"] == DomainOutcome.UNKNOWN.value
    assert projection["effect_certainty"] == EffectCertainty.UNKNOWN.value
    assert "reconcile" in str(projection["retry_hint"])


def test_denial_reports_error_code_and_never_a_domain_claim() -> None:
    """A pre-dispatch rejection carries its stable code; nothing ran, so
    no domain outcome and no effect evidence exist."""
    result = _tool_result(
        status=ToolStatus.DENIED,
        effect="not_attempted",
        reason="payload_schema_mismatch",
        error_code="payload_schema_mismatch",
        error_detail="$.command: required property is missing",
        executor_ok=None,
    )
    projection = _projection_of(result)
    assert projection["execution_status"] == ExecutionStatus.FAILED.value
    assert projection["domain_outcome"] == DomainOutcome.UNKNOWN.value
    assert projection["effect_certainty"] == EffectCertainty.UNKNOWN.value
    assert projection["error_code"] == "payload_schema_mismatch"


def test_approval_required_is_outcome_unknown_with_approval_hint() -> None:
    """The approval deferral (bridge status or the run layer's parked
    ``approval_required_but_absent`` row) is honest: no effect ran, the
    outcome is unknown, the hint names the approval path."""
    for result in (
        _tool_result(status=ToolStatus.APPROVAL_REQUIRED, effect="not_attempted"),
        _tool_result(
            status=ToolStatus.DENIED,
            effect="not_attempted",
            reason="approval_required_but_absent",
        ),
    ):
        projection = _projection_of(result)
        assert projection["execution_status"] == ExecutionStatus.OUTCOME_UNKNOWN.value
        assert projection["domain_outcome"] == DomainOutcome.UNKNOWN.value
        assert projection["effect_certainty"] == EffectCertainty.UNKNOWN.value
        assert "approval" in str(projection["retry_hint"])


def test_malformed_subchecks_fail_closed_over_ok_status() -> None:
    """Broken structured evidence never falls through to the weaker
    envelope rule: a present-but-malformed (or empty) ``subchecks``
    section keeps the outcome ``unknown`` even beside ``status: ok``."""
    for output in (
        json.dumps({"status": "ok", "subchecks": "all good"}),
        json.dumps({"status": "ok", "subchecks": []}),
        json.dumps({"status": "ok", "subchecks": [{"name": "x", "passed": "yes"}]}),
        json.dumps({"status": "ok", "subchecks": [{"name": 1, "passed": True}]}),
    ):
        projection = _projection_of(_tool_result(output_text=output))
        assert projection["domain_outcome"] == DomainOutcome.UNKNOWN.value
        assert projection["effect_certainty"] == EffectCertainty.UNCERTAIN.value


def test_invalid_domain_outcome_marker_fails_closed() -> None:
    projection = _projection_of(
        _tool_result(
            output_text=json.dumps({"status": "ok", "domain_outcome": "excellent"})
        )
    )
    assert projection["domain_outcome"] == DomainOutcome.UNKNOWN.value
    assert projection["effect_certainty"] == EffectCertainty.UNCERTAIN.value


def test_explicit_failure_marker_wins_over_ok_envelope() -> None:
    """Negative evidence pins the outcome: an explicit ``failure`` marker
    is never promoted away by the envelope's ``ok``."""
    projection = _projection_of(
        _tool_result(
            output_text=json.dumps({"status": "ok", "domain_outcome": "failure"})
        )
    )
    assert projection["domain_outcome"] == DomainOutcome.FAILURE.value
    assert projection["effect_certainty"] == EffectCertainty.UNCERTAIN.value


def test_non_json_output_has_no_semantic_evidence() -> None:
    """Arbitrary text (no regex oracle, §9): execution layer facts only."""
    projection = _projection_of(
        _tool_result(output_text="service restarted successfully\n")
    )
    assert projection["execution_status"] == ExecutionStatus.SUCCEEDED.value
    assert projection["domain_outcome"] == DomainOutcome.UNKNOWN.value
    assert projection["effect_certainty"] == EffectCertainty.UNCERTAIN.value


def test_projection_is_deterministic() -> None:
    result = _tool_result(
        output_text=json.dumps(
            {
                "status": "ok",
                "subchecks": [{"name": "a", "passed": True}],
                "evidence_refs": [{"kind": "run_event", "evidence_id": str(RUN_1)}],
            }
        )
    )
    assert project_tool_result(result) == project_tool_result(result)


def test_projection_pagination_slices_and_totals() -> None:
    """Every extracted collection is a bounded page with honest
    metadata: sliced totals and ``has_more`` — truncation is never
    hidden success (wire honesty C08 rule 6)."""
    subchecks = [{"name": f"check_{i:02d}", "passed": i % 3 != 0} for i in range(20)]
    evidence = [{"kind": "observation", "evidence_id": str(RUN_1)} for _ in range(18)]
    result_data = {f"metric_{i:02d}": str(i) for i in range(12)}
    projection = _projection_of(
        _tool_result(
            output_text=json.dumps(
                {
                    "status": "ok",
                    "subchecks": subchecks,
                    "evidence_refs": evidence,
                    "result_data": result_data,
                }
            )
        )
    )
    pagination = projection["pagination"]
    assert isinstance(pagination, dict)
    assert pagination["subchecks"] == {
        "limit": 16,
        "offset": 0,
        "total": 20,
        "has_more": True,
    }
    assert pagination["evidence_refs"] == {
        "limit": 16,
        "offset": 0,
        "total": 18,
        "has_more": True,
    }
    assert pagination["result_data"] == {
        "limit": 8,
        "offset": 0,
        "total": 12,
        "has_more": True,
    }
    assert len(projection["subchecks"]) == 16
    assert len(projection["evidence_refs"]) == 16
    assert len(projection["result_data"]) == 8


def test_projection_result_data_bounds_and_drops_secret_keys() -> None:
    """Safe bounded result data (§9): values capped, credential-shaped
    keys never carried — and dropped keys stay visible in the total."""
    long_value = "x" * (RESULT_DATA_VALUE_MAX_CHARS + 500)
    projection = _projection_of(
        _tool_result(
            output_text=json.dumps(
                {
                    "status": "ok",
                    "result_data": {
                        "password": "must-not-ride",
                        "API_KEY": "also-not",
                        "ok_metric": long_value,
                    },
                }
            )
        )
    )
    result_data = projection["result_data"]
    assert isinstance(result_data, dict)
    assert "password" not in result_data and "API_KEY" not in result_data
    assert result_data["ok_metric"] == "x" * RESULT_DATA_VALUE_MAX_CHARS
    pagination = projection["pagination"]
    assert isinstance(pagination, dict)
    assert pagination["result_data"]["total"] == 3
    assert "must-not-ride" not in json.dumps(projection)


def test_projection_evidence_refs_survive_with_kinds() -> None:
    """Evidence refs survive the projection (no silent drops): object
    entries normalize to contract shape, bare string ids are carried,
    and the pagination total counts every entry."""
    projection = _projection_of(
        _tool_result(
            output_text=json.dumps(
                {
                    "status": "ok",
                    "evidence_refs": [
                        {
                            "kind": "run_event",
                            "evidence_id": str(RUN_1),
                            "note": "dispatcher receipt",
                        },
                        str(SERVER_A),
                        {"evidence_id": "partial"},
                        42,
                    ],
                }
            )
        )
    )
    refs = projection["evidence_refs"]
    assert isinstance(refs, list)
    assert refs[0] == {
        "kind": "run_event",
        "evidence_id": str(RUN_1),
        "note": "dispatcher receipt",
    }
    assert refs[1] == {"evidence_id": str(SERVER_A)}
    assert refs[2] == {"evidence_id": "partial"}
    assert len(refs) == 3  # the non-string/non-mapping entry cannot be shaped
    pagination = projection["pagination"]
    assert isinstance(pagination, dict)
    assert pagination["evidence_refs"]["total"] == 4


def test_version_one_callers_stay_compatible() -> None:
    """AC2: the envelope change is additive and versioned — every
    version-1 key keeps its position, and the legacy 20-kwarg
    construction (the run layer's exact call shape) serializes with the
    projection appended."""
    result = _tool_result()
    document = json.loads(result.to_json_str())
    version_one_keys = [
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
    assert list(document)[: len(version_one_keys)] == version_one_keys
    assert list(document)[-1] == "projection"
    assert document["schema_version"] == TOOL_RESULT_SCHEMA_VERSION


def test_projection_extracted_fields_stay_pattern_clean() -> None:
    """Defense-in-depth (S1): extracted projection fields run through the
    output-safety pattern layer even when only the dispatcher's
    value-redaction ran (the bridge's direct model-facing path)."""
    token = "sk-" + "Z" * 30
    result = _tool_result(
        output_text=json.dumps(
            {
                "status": "ok",
                "result_data": {"last_error": f"auth failed for {token}"},
                "subchecks": [{"name": f"probe {token}", "passed": True}],
            }
        )
    )
    projected = json.dumps(_projection_of(result))
    assert token not in projected
    assert REDACTED in projected


# ===========================================================================
# S1: redaction guarantees retained through the real dispatcher + run seam
# ===========================================================================


def test_redaction_canaries_never_reach_the_model_envelope(fxt_dir: Path) -> None:
    """The production composition — dispatcher value-redaction → run-seam
    ``project_text`` → :class:`ToolResult` → projection — leaves no
    canary in any form (raw, JSON-escaped, JSON-encoded inner payload)
    anywhere in the serialized envelope.

    The base64-pickled canary documents the recorded boundary: no layer
    decodes opaque encoded blobs (unpickling untrusted bytes is an RCE
    vector), so the assertion pins what the layers DO guarantee — the
    extracted projection copy is capped, and every decodable form is
    gone (see report.md, limitations)."""
    import base64
    import pickle

    resolved = "fxt-resolved-secret-19a86c"
    escaped = json.dumps(resolved, ensure_ascii=True)[1:-1]
    inner = json.dumps({"password": "fxt-inner-secret-44b2"})
    pickled = base64.b64encode(
        pickle.dumps({"password": "fxt-pickled-secret-77c3"})
    ).decode("ascii")
    token = "sk-" + "Q" * 30
    raw_output = json.dumps(
        {
            "status": "ok",
            "note": f"leak attempt {resolved} and {escaped}",
            "encoded": inner,
            "checkpoint": pickled,
            "token": token,
            "result_data": {
                "echo": resolved,
                "blob": pickled,
                "token_echo": token,
            },
        }
    )
    # The exact run-seam composition (run.py `_result_document` feeds the
    # projection `project_text(output, resolved_values)`).
    safe_output = project_text(raw_output, [resolved])
    result = _tool_result(output_text=safe_output)
    envelope = result.to_json_str()

    assert resolved not in envelope
    assert escaped not in envelope
    assert "fxt-inner-secret-44b2" not in envelope
    assert token not in envelope
    assert REDACTED in envelope
    # Every decodable secret form is gone from the projection block too.
    projection_text = json.dumps(json.loads(envelope)["projection"])
    assert resolved not in projection_text
    assert "fxt-inner-secret-44b2" not in projection_text
    # The opaque base64-pickled blob cannot ride an extracted field whole:
    # extracted copies are capped at the §9 value bound.
    projected = json.loads(envelope)["projection"]
    blob_copy = projected["result_data"].get("blob")
    assert blob_copy is not None
    assert len(blob_copy) <= RESULT_DATA_VALUE_MAX_CHARS


@pytest.mark.anyio
async def test_bridge_structured_output_projects_domain_success(
    fxt_dir: Path, counter: Path
) -> None:
    """Full chain through the real bridge + dispatcher: a structured
    ``ok`` envelope projects success/certain end-to-end."""
    registry = _real_registry()
    executor = _Recorder(
        counter,
        output=json.dumps(
            {
                "status": "ok",
                "items": ["one"],
                "limit": 10,
                "offset": 0,
                "total": 1,
                "has_more": False,
            }
        ),
    )
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=executor),
        _context(allowed=(READ_CAPABILITY,)),
    )
    document = _result_of(
        await _tool_of(bridge, READ_CAPABILITY).function(
            **{"source": "journal", "service": "mai-api"}
        )
    )
    assert document["status"] == ToolStatus.EXECUTED.value
    projection = document["projection"]
    assert projection["execution_status"] == ExecutionStatus.SUCCEEDED.value
    assert projection["domain_outcome"] == DomainOutcome.SUCCESS.value
    assert projection["effect_certainty"] == EffectCertainty.CERTAIN.value
    assert counter.exists()


@pytest.mark.anyio
async def test_bridge_shell_exit_zero_projects_unknown_domain(
    fxt_dir: Path, counter: Path
) -> None:
    """E3 through the real chain: the wrapper executed and exited zero,
    and the projection honestly reports an unproven domain outcome."""
    registry = _real_registry()
    executor = _Recorder(
        counter,
        output=json.dumps(
            {
                "status": "succeeded",
                "exit_code": 0,
                "stdout": "done",
                "stderr": "",
            }
        ),
    )
    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=executor),
        _context(allowed=(READ_CAPABILITY,)),
    )
    document = _result_of(
        await _tool_of(bridge, READ_CAPABILITY).function(
            **{"source": "journal", "service": "mai-api"}
        )
    )
    projection = document["projection"]
    assert projection["execution_status"] == ExecutionStatus.SUCCEEDED.value
    assert projection["domain_outcome"] == DomainOutcome.UNKNOWN.value
    assert projection["effect_certainty"] == EffectCertainty.UNCERTAIN.value


@pytest.mark.anyio
async def test_bridge_canary_output_projects_without_leaks(
    fxt_dir: Path, counter: Path
) -> None:
    """S1 through the real chain: the dispatcher's value-redaction layer
    scrubs executor-declared secret material before the projection ever
    runs; no canary and no raw payload material appears anywhere in the
    model-facing envelope, and the payload is present as its hash only."""
    registry = _real_registry()

    class _Leaky:
        async def __call__(self, call: CapabilityCall) -> ExecutorResult:
            assert call.payload == {"source": "journal", "service": "mai-api"}
            output = json.dumps(
                {
                    "status": "ok",
                    "echo": REFERENCE_PAYLOAD["command"],
                    "leak": SECRET_VALUE,
                    "result_data": {"echo": SECRET_VALUE},
                }
            )
            return ExecutorResult(ok=True, output=output, redact_values=(SECRET_VALUE,))

    bridge = _bridge(
        registry,
        _dispatcher(registry, _unused_approvals(), executor=_Leaky()),
        _context(allowed=(READ_CAPABILITY,)),
    )
    document = _result_of(
        await _tool_of(bridge, READ_CAPABILITY).function(
            **{"source": "journal", "service": "mai-api"}
        )
    )
    envelope = json.dumps(document)
    assert SECRET_VALUE not in envelope
    assert DISPATCH_REDACTED in envelope
    assert document["payload_hash"] == canonical_payload_hash(
        {"source": "journal", "service": "mai-api"}
    )
    projection = document["projection"]
    assert projection["result_data"]["echo"] == DISPATCH_REDACTED
    assert projection["domain_outcome"] == DomainOutcome.SUCCESS.value
    assert projection["effect_certainty"] == EffectCertainty.CERTAIN.value


# ---------------------------------------------------------------------------
# ask_04-secret-surface: the agent-facing secret.* projection (D6/C6).
# The REAL built-in 1Password descriptors are registered; the bridge
# projects a generation/reference-only secret.store surface and enforces
# it on BOTH invocation seams before any dispatch. The backend-only
# provider is a synthetic fixture (mirrors the builtin executor's exact
# success document, redacted by the dispatcher's redact-then-bound pass).
# ---------------------------------------------------------------------------

_SECRET_CAPS = ("secret.list", "secret.get", "secret.store")

_CANARY = " plaintext-canary "

_SYNTHETIC_GENERATED = "fxt-synthetic-generated-value-06Abc"


def _secret_registry() -> CapabilityRegistry:
    return _registry_with(*ONEPASSWORD_CAPABILITIES)


def _secret_bridge(dispatcher) -> CapabilityToolBridge:
    return _bridge(_secret_registry(), dispatcher, _context(allowed=_SECRET_CAPS))


def _store_capture_executor(calls: list[CapabilityCall]):
    async def _execute(call: CapabilityCall) -> ExecutorResult:
        calls.append(call)
        document = {
            "status": "ok",
            "vault": "fixture-vault",
            "item_id": "itm_synthetic_01",
            "title": call.payload.get("title"),
            "field": "password",
            "reference": "op://fixture-vault/itm_synthetic_01/password",
            "generated": True,
            "value": _SYNTHETIC_GENERATED,
            "sha256": "3" * 64,
        }
        return ExecutorResult(
            ok=True,
            output=json.dumps(document, sort_keys=True),
            redact_values=(_SYNTHETIC_GENERATED,),
        )

    return _execute


@pytest.mark.anyio
async def test_secret_surface_exposed_with_valid_unique_names(fxt_dir: Path) -> None:
    bridge = _secret_bridge(_dispatcher(_secret_registry(), _unused_approvals()))
    names = [tool.name for tool in bridge.tools]
    assert names == ["secret_list", "secret_get", "secret_store"]
    assert len(set(names)) == len(names)
    for tool in bridge.tools:
        schema = tool.tool_def.parameters_json_schema
        assert isinstance(schema, dict) and schema.get("type") == "object"
        assert tool.tool_def.description


@pytest.mark.anyio
async def test_secret_store_schema_is_generation_only(fxt_dir: Path) -> None:
    bridge = _secret_bridge(_dispatcher(_secret_registry(), _unused_approvals()))
    tool = _tool_of(bridge, SECRET_STORE_CAPABILITY_ID)
    schema = tool.tool_def.parameters_json_schema
    properties = schema["properties"]
    for field in SECRET_RAW_VALUE_FIELDS | {"source_ref"}:
        assert field not in properties, field
    assert schema["required"] == ["title", "generate"]
    assert properties["generate"]["const"] is True
    assert properties["generate"]["type"] == "boolean"
    assert len(properties["generate"]["description"]) >= 12
    assert schema["additionalProperties"] is False
    description = tool.tool_def.description or ""
    assert "GENERATION/REFERENCE-ONLY" in description
    assert "op CLI" in description
    # The list/get surfaces state the exclusive secret-routing rule too.
    for capability_id in ("secret.list", "secret.get"):
        assert "op CLI" in (_tool_of(bridge, capability_id).tool_def.description or "")
    # The trusted non-agent descriptor contract is untouched: the raw
    # capability document still carries `value` and its original
    # required list, and so does the descriptor behind the binding.
    raw_properties = SECRET_STORE_CAPABILITY["input_schema"]["properties"]
    assert "value" in raw_properties
    assert SECRET_STORE_CAPABILITY["input_schema"]["required"] == ["title"]
    descriptor = bridge.descriptor_for(SECRET_STORE_CAPABILITY_ID)
    assert descriptor is not None
    assert "value" in descriptor.input_schema["properties"]


@pytest.mark.anyio
async def test_secret_store_denies_raw_value_and_generate_false_pre_dispatch(
    fxt_dir: Path, counter: Path
) -> None:
    calls: list[CapabilityCall] = []
    recorder = _Recorder(counter)
    bridge = _secret_bridge(
        _dispatcher(_secret_registry(), _unused_approvals(), executor=recorder)
    )
    tool = _tool_of(bridge, SECRET_STORE_CAPABILITY_ID)
    bad_payloads = (
        {"title": "db", "value": _CANARY},
        {"title": "db", "generate": False},
        {"title": "db"},
        {"title": "db", "generate": True, "value": _CANARY},
    )
    for payload in bad_payloads:
        text = await tool.function(**payload)
        result = _result_of(text)
        assert result["status"] == ToolStatus.DENIED.value
        assert result["reason"] == BridgeErrorCode.PAYLOAD_SCHEMA_MISMATCH.value
        assert result["effect"] == "not_attempted"
        assert result["error"]["code"] == "payload_schema_mismatch"
        assert _CANARY not in text
    assert recorder.calls == []
    assert not counter.exists()
    # The resume path raises the same contract before any dispatch, and
    # the raised detail stays value-free.
    with pytest.raises(BridgeError) as error:
        await bridge.dispatch_once("secret.store", {"title": "db", "value": _CANARY})
    assert error.value.code is BridgeErrorCode.PAYLOAD_SCHEMA_MISMATCH
    assert _CANARY not in str(error.value)
    assert calls == []


@pytest.mark.anyio
async def test_secret_store_generation_dispatches_and_redacts(fxt_dir: Path) -> None:
    calls: list[CapabilityCall] = []
    bridge = _secret_bridge(
        _dispatcher(
            _secret_registry(), _unused_approvals(), executor=_store_capture_executor(calls)
        )
    )
    # 1. Model-facing tool invocation.
    tool = _tool_of(bridge, SECRET_STORE_CAPABILITY_ID)
    text = await tool.function(title="app-db", generate=True, length=24)
    result = _result_of(text)
    assert result["status"] == ToolStatus.EXECUTED.value
    assert result["effect"] == "executed"
    output_text = result["output"]["text"]
    document = json.loads(output_text)
    assert document["status"] == "ok"
    assert document["reference"] == "op://fixture-vault/itm_synthetic_01/password"
    assert document["generated"] is True
    assert DISPATCH_REDACTED in output_text
    assert _SYNTHETIC_GENERATED not in text
    assert calls[0].payload == {"title": "app-db", "generate": True, "length": 24}
    # 2. The durable resume seam dispatches the same generation contract.
    outcome = await bridge.dispatch_once(
        "secret.store", {"title": "resume-item", "generate": True}
    )
    assert outcome.allowed and outcome.effect == "executed"
    assert len(calls) == 2
    # 3. A scripted agent run over the bridge reaches the same boundary.
    calls.clear()
    agent = _scripted_agent(
        bridge,
        [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="secret_store",
                        args={"title": "agent-db", "generate": True, "length": 24},
                    )
                ]
            ),
            ModelResponse(parts=[TextPart("stored")]),
        ],
    )
    with capture_run_messages() as messages:
        run = await agent.run("store a fresh db secret")
    assert run.output == "stored"
    returns = [
        part
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart)
    ]
    assert len(returns) == 1
    agent_document = _result_of(str(returns[0].content))
    assert agent_document["status"] == ToolStatus.EXECUTED.value
    assert DISPATCH_REDACTED in str(returns[0].content)
    assert _SYNTHETIC_GENERATED not in str(returns[0].content)
    assert calls[0].payload == {"title": "agent-db", "generate": True, "length": 24}


@pytest.mark.anyio
async def test_non_agent_dispatcher_keeps_raw_value_contract(fxt_dir: Path) -> None:
    """The trusted non-agent dispatcher lane keeps the full descriptor
    contract: raw `value` passes descriptor validation and reaches the
    executor unchanged (compatibility, C6)."""
    calls: list[CapabilityCall] = []
    registry = _registry_with(SECRET_STORE_CAPABILITY)
    dispatcher = _dispatcher(
        registry, _unused_approvals(), executor=_store_capture_executor(calls)
    )
    outcome = await dispatcher.dispatch(
        context=_context(allowed=("secret.store",)),
        capability_id="secret.store",
        payload={"title": "operator-item", "value": "operator-provided"},
        approval_id=None,
    )
    assert outcome.allowed and outcome.effect == "executed"
    assert len(calls) == 1
    assert calls[0].payload == {
        "title": "operator-item",
        "value": "operator-provided",
    }
