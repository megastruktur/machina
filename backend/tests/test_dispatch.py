"""Focused tests for the capability dispatch boundary.

Card ``03-dispatcher`` against contracts.md **C03 rules 2–7** (and the
C05 ``waiting_approval`` seam): every call resolves current
grants/approval and secret references via an explicit
``ExecutionContext``, and a denied call can never trigger an external
effect while an executed one leaves bounded, redacted output with an
audit receipt.

Boundary discipline: the gate-scenario coverage owned here
(``runtime-scenarios.md`` APPROVAL-PAYLOAD co-ownership, dispatcher
half) runs the **real composed dispatcher** — real
``ApprovalStore`` over real disposable Postgres through the real
Alembic chain (fixture ``migrated_url``), real ``EnvResolver`` over a
real ``.env`` file, real ``GrantConfigStore`` revision bumps for
revocation, and a **real benign fixture executable** (separate OS
process) writing a counter file. Denied/revoked/altered-server calls
must leave the counter unchanged; the one allowed call records exactly
one effect. Mocks cannot prove any of that.

Secret hygiene: the only "secret" in play is the inert synthetic
``.env`` value ``MAI_FXT_TOKEN``; assertions sweep every receipt and
message for its value — redaction is a pinned behavior, not a hope.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from mai.capabilities.dispatch import (
    REDACTED,
    CapabilityCall,
    CapabilityDispatcher,
    DispatchError,
    DispatchErrorCode,
    DispatchReason,
    ExecutorResult,
    canonical_payload_hash,
    canonical_payload_json,
    default_output_budget_bytes,
)
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalRequest, ApprovalStore
from mai.policy.config import GrantConfigStore
from mai.policy.evaluate import (
    ExecutionContext,
    ParentScope,
    Policy,
    PolicyMode,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: The constant tracks the full chain (0003 head at this card's base).
REVISION_IDS = ("0001", "0002", "0003")

#: C10 rule 2 fixture namespace: the benign counter executable, its
#: counter file and the fixture .env live here; nothing outside it is
#: touched. Created/destroyed per session by the ``fxt_dir`` fixture.
FXT_ROOT = Path("/tmp/mai-fxt-03-dispatcher")

#: Fixture capability under test: effect class ``privileged`` so every
#: meaningful call exercises the full approval seam.
CAPABILITY = "deploy.service"
READ_CAPABILITY = "status.inspect"
UNGRANTED_CAPABILITY = "shell.raw"
DENIED_BY_POLICY_CAPABILITY = "network.probe"

SERVER_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
SERVER_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
RUN_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
RUN_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
PRINCIPAL = "admin"

#: Inert synthetic credential material (the redaction sweep target).
SECRET_NAME = "MAI_FXT_TOKEN"
SECRET_VALUE = "fxt-synthetic-token-value-03c9a1"

GOOD_PAYLOAD = {"op": "deploy", "v": 1}
ALTERED_PAYLOAD = {"op": "deploy", "v": 2}

#: Hand-written canonical form of GOOD_PAYLOAD — the literal pins the
#: hash format without trusting the implementation under test.
GOOD_PAYLOAD_CANONICAL = '{"op":"deploy","v":1}'


def _descriptor_document(identifier: str, effect_class: str) -> dict[str, object]:
    return {
        "identifier": identifier,
        "version": "1.0.0",
        "input_schema": {},
        "output_schema": {},
        "execution_environments": ["controller"],
        "effect_class": effect_class,
        "resource_scope": ["deploy"] if effect_class != "read" else [],
        "required_grants": [],
    }


def _policy(revision: int = 7, denied: tuple[str, ...] = ()) -> Policy:
    return Policy(revision=revision, denied_capabilities=frozenset(denied))


def _grants():
    """Current grants: the privileged capability and the read capability,
    both scoped to SERVER_A only."""
    return {
        CAPABILITY: frozenset({SERVER_A}),
        READ_CAPABILITY: frozenset({SERVER_A}),
    }


def _context(
    *,
    server_id: uuid.UUID = SERVER_A,
    run_id: uuid.UUID = RUN_1,
    references: tuple[str, ...] = (),
    allowed: tuple[str, ...] = (CAPABILITY,),
    mode: PolicyMode = PolicyMode.DELEGATED,
    principal: str = PRINCIPAL,
    parent_scope: ParentScope | None = None,
) -> ExecutionContext:
    return ExecutionContext(
        principal=principal,
        server_id=server_id,
        run_id=run_id,
        parent_scope=parent_scope,
        profile_revision="prof-1",
        prompt_revision="prompt-1",
        credential_references=references,
        allowed_capabilities=allowed,
        mode=mode,
    )


def _refusing_executor() -> object:
    """An executor whose invocation IS the external effect: it fails the
    test. Deny outcomes must never reach it."""

    async def _execute(call: CapabilityCall) -> ExecutorResult:
        raise AssertionError(
            f"executor invoked for a call that must not execute: {call}"
        )

    return _execute


def _recording_executor(
    output: str = "done", ok: bool = True
) -> tuple[object, list[CapabilityCall]]:
    calls: list[CapabilityCall] = []

    async def _execute(call: CapabilityCall) -> ExecutorResult:
        calls.append(call)
        return ExecutorResult(ok=ok, output=output)

    return _execute, calls


def _dispatcher(
    *,
    registry: CapabilityRegistry,
    approvals: ApprovalStore,
    grants=_grants,
    policy=lambda: _policy(),
    adopted=lambda server_id: True,
    credentials=lambda: EnvResolver(FXT_ROOT / "fixture.env"),
    executor: object | None = None,
    output_budget_bytes: int = default_output_budget_bytes,
) -> CapabilityDispatcher:
    if executor is None:
        executor = _refusing_executor()
    return CapabilityDispatcher(
        registry=registry,
        approvals=approvals,
        grants=grants,
        policy=policy,
        adopted=adopted,
        credentials=credentials,
        executor=executor,  # type: ignore[arg-type]
        output_budget_bytes=output_budget_bytes,
    )


def _unused_approvals() -> ApprovalStore:
    """A real ``ApprovalStore`` over a lazily-connecting engine.

    Non-DB tests never present an approval, so no connection is ever
    opened; construction still proves the wiring type boundary.
    """
    _, sessions = create_app_engine(
        "postgresql://unused:unused@127.0.0.1:1/unused"
    )
    return ApprovalStore(sessions)


def _registry_with(*documents: dict[str, object]) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for document in documents:
        registry.register(parse_descriptor(document))
    return registry


def _utcnow_awake() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Live fixture: real disposable Postgres + real chain + real file fixtures
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(ALEMBIC_INI))


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``0003``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the dispatcher approval-seam claims "
            "need a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                "DROP TABLE IF EXISTS approvals, admin_sessions, "
                "admin_credential, profile_projection_journal, "
                "profile_projection, alembic_version"
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
    """C10-rule-2 fixture namespace: benign executable, .env, counter."""
    import shutil

    FXT_ROOT.mkdir(mode=0o700, exist_ok=True)
    os.chmod(FXT_ROOT, 0o700)
    # Defensive reset: prior runs (e.g. the evidence transcript sharing
    # this namespace) may have left grant-config/counter state behind;
    # the suite starts clean but keeps url.env (DB fixture binding).
    for child in FXT_ROOT.iterdir():
        if child.name == "url.env":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    # The benign fixture executable: appends one line to the counter file
    # given as argv[1], prints the new count. Nothing else.
    import stat

    program = FXT_ROOT / "counter_payload.py"
    program.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "counter = Path(sys.argv[1])\n"
        "count = (int(counter.read_text()) if counter.exists() else 0) + 1\n"
        "counter.write_text(str(count))\n"
        "print(f'count={count}')\n",
        encoding="utf-8",
    )
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    (FXT_ROOT / "fixture.env").write_text(
        f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8"
    )
    os.chmod(FXT_ROOT / "fixture.env", 0o600)
    yield FXT_ROOT
    # Cleanup proof lives in the evidence set; the suite removes its own
    # artifacts but keeps url.env (the disposable Postgres fixture
    # binding) alive for the entrypoint transcript, which re-sources it.
    import shutil

    for child in FXT_ROOT.iterdir():
        if child.name == "url.env":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


@pytest.fixture
def counter_path(fxt_dir: Path) -> Path:
    counter = fxt_dir / "counter"
    counter.unlink(missing_ok=True)
    return counter


@pytest.fixture
def counter_program(fxt_dir: Path) -> Path:
    return fxt_dir / "counter_payload.py"


def subprocess_executor(program: Path):
    """The real external effect: a separate OS process running the benign
    fixture executable (the registry is declarative; the host supplies
    the implementation — here, a real subprocess)."""

    async def _execute(call: CapabilityCall) -> ExecutorResult:
        counter = call.payload["counter_path"]
        if not isinstance(counter, str):
            raise ValueError("payload counter_path must be a string path")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(program),
            counter,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return ExecutorResult(
            ok=proc.returncode == 0,
            output=stdout.decode("utf-8", errors="replace")
            + stderr.decode("utf-8", errors="replace"),
        )

    return _execute


async def _read_counter(path: Path) -> int:
    return int(path.read_text()) if path.exists() else 0


async def _create_approval(
    store: ApprovalStore,
    *,
    capability_id: str = CAPABILITY,
    server_id: uuid.UUID = SERVER_A,
    payload: dict[str, object] = GOOD_PAYLOAD,
    policy_revision: int = 7,
    principal: str = PRINCIPAL,
):
    return await store.create(
        ApprovalRequest(
            capability_id=capability_id,
            server_id=server_id,
            payload_hash=canonical_payload_hash(payload),
            policy_revision=policy_revision,
            created_by=principal,
            expires_at=_utcnow_awake() + timedelta(minutes=10),
        )
    )


def _sweep(document: object, *forbidden: str) -> None:
    """No forbidden value may appear anywhere in the JSON-able receipt."""
    rendered = json.dumps(document)
    for value in forbidden:
        assert value not in rendered


# ---------------------------------------------------------------------------
# Construction and request validation (fail-closed before any state read)
# ---------------------------------------------------------------------------


def test_construction_rejects_malformed_wiring() -> None:
    registry = _registry_with(_descriptor_document(CAPABILITY, "privileged"))
    base: dict[str, object] = {
        "registry": registry,
        "approvals": _unused_approvals(),
        "grants": _grants,
        "policy": lambda: _policy(),
        "adopted": lambda server_id: True,
        "credentials": lambda: EnvResolver(FXT_ROOT / "fixture.env"),
        "executor": _refusing_executor(),
    }

    def _reject(**overrides: object) -> None:
        values = dict(base)
        values.update(overrides)
        with pytest.raises(DispatchError) as excinfo:
            CapabilityDispatcher(**values)  # type: ignore[arg-type]
        assert excinfo.value.code is DispatchErrorCode.INVALID_WIRING

    _reject(registry=object())
    _reject(approvals=object())
    _reject(grants="not-callable")
    _reject(policy=None)
    _reject(adopted=42)
    _reject(credentials="nope")
    _reject(executor="not-callable")
    for budget in (0, -1, True, 2**20 + 1, "65536"):
        _reject(output_budget_bytes=budget)


@pytest.mark.anyio
async def test_malformed_request_raises_before_any_state_or_effect() -> None:
    """Malformed requests raise; no provider is consulted and the
    executor is never reached (fail-closed construction rejection)."""
    reads: list[str] = []

    def _counting_grants():
        reads.append("grants")
        return _grants()

    dispatcher = _dispatcher(
        registry=_registry_with(_descriptor_document(CAPABILITY, "privileged")),
        approvals=_unused_approvals(),
        grants=_counting_grants,
    )
    context = _context()
    with pytest.raises(DispatchError) as excinfo:
        await dispatcher.dispatch(
            context=context, capability_id="Bad_Slug", payload=GOOD_PAYLOAD
        )
    assert excinfo.value.code is DispatchErrorCode.INVALID_REQUEST
    with pytest.raises(DispatchError):
        await dispatcher.dispatch(
            context=context, capability_id=CAPABILITY, payload="not-a-mapping"
        )
    with pytest.raises(DispatchError):
        await dispatcher.dispatch(
            context=context, capability_id=CAPABILITY, payload={"x": {1, 2}}
        )
    with pytest.raises(DispatchError):
        await dispatcher.dispatch(
            context=context, capability_id=CAPABILITY, payload={"x": float("nan")}
        )
    with pytest.raises(DispatchError):
        await dispatcher.dispatch(
            context=context,
            capability_id=CAPABILITY,
            payload=GOOD_PAYLOAD,
            approval_id="not-a-uuid",
        )
    assert reads == []  # validation precedes every state read


def test_canonical_payload_hash_is_order_stable_and_literal_pinned() -> None:
    assert canonical_payload_json(GOOD_PAYLOAD) == GOOD_PAYLOAD_CANONICAL
    assert canonical_payload_hash(GOOD_PAYLOAD) == canonical_payload_hash(
        {"v": 1, "op": "deploy"}
    )
    assert canonical_payload_hash(GOOD_PAYLOAD) != canonical_payload_hash(
        ALTERED_PAYLOAD
    )


# ---------------------------------------------------------------------------
# Policy gate denies: no effect, no consumption, honest receipt
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_every_policy_deny_denies_without_effect() -> None:
    """The frozen deny precedence passes through verbatim; the executor
    (the external effect) is never invoked; the receipt claims nothing."""
    registry = _registry_with(
        _descriptor_document(CAPABILITY, "privileged"),
        _descriptor_document(READ_CAPABILITY, "read"),
        _descriptor_document(UNGRANTED_CAPABILITY, "privileged"),
        _descriptor_document(DENIED_BY_POLICY_CAPABILITY, "mutate"),
    )
    # (name, wiring overrides, context, capability_id, expected reason)
    cases = [
        (
            "not adopted",
            {"adopted": lambda server_id: False},
            _context(),
            CAPABILITY,
            "server_not_adopted",
        ),
        (
            "denied by policy list",
            {"policy": lambda: _policy(denied=(DENIED_BY_POLICY_CAPABILITY,))},
            _context(allowed=(CAPABILITY, DENIED_BY_POLICY_CAPABILITY)),
            DENIED_BY_POLICY_CAPABILITY,
            "capability_denied_by_policy",
        ),
        (
            "revoked/never registered",
            {},
            _context(),
            "gone.capability",
            "capability_not_registered",
        ),
        (
            "not granted",
            {},
            _context(allowed=(CAPABILITY, UNGRANTED_CAPABILITY)),
            UNGRANTED_CAPABILITY,
            "capability_not_granted",
        ),
        (
            "server scope mismatch",
            {},
            _context(server_id=SERVER_B),
            CAPABILITY,
            "server_scope_mismatch",
        ),
    ]
    for _name, wiring, context, capability_id, expected_reason in cases:
        dispatcher = _dispatcher(
            registry=registry,
            approvals=_unused_approvals(),
            grants=wiring.get("grants", _grants),  # type: ignore[arg-type]
            policy=wiring.get("policy", lambda: _policy()),  # type: ignore[arg-type]
            adopted=wiring.get("adopted", lambda server_id: True),  # type: ignore[arg-type,return-value]
        )
        outcome = await dispatcher.dispatch(
            context=context, capability_id=capability_id, payload=GOOD_PAYLOAD
        )
        assert outcome.allowed is False, expected_reason
        assert outcome.reason == expected_reason
        assert outcome.effect == "not_attempted"
        assert outcome.approval_id is None and outcome.approval_spent is False
        assert outcome.output is None and outcome.executor_ok is None
        document = outcome.to_json()
        _sweep(document, SECRET_VALUE, GOOD_PAYLOAD_CANONICAL)


@pytest.mark.anyio
async def test_grants_provider_failure_denies_without_effect() -> None:
    def _broken():
        raise RuntimeError("grants source down")

    dispatcher = _dispatcher(
        registry=_registry_with(_descriptor_document(CAPABILITY, "privileged")),
        approvals=_unused_approvals(),
        grants=_broken,
    )
    outcome = await dispatcher.dispatch(
        context=_context(), capability_id=CAPABILITY, payload=GOOD_PAYLOAD
    )
    assert outcome.allowed is False
    assert outcome.reason == DispatchReason.GRANTS_UNAVAILABLE.value
    assert outcome.effect == "not_attempted"


# ---------------------------------------------------------------------------
# Approval seam (shape-level; full binding against real Postgres below)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_approval_required_without_decision_denies_before_credentials() -> None:
    """Privileged always requires an approval (both modes); ask-mode
    mutate likewise. No approval ⇒ deny; executor and credentials
    untouched."""
    registry = _registry_with(
        _descriptor_document(CAPABILITY, "privileged"),
        _descriptor_document(DENIED_BY_POLICY_CAPABILITY, "mutate"),
    )
    both_grants = lambda: {  # noqa: E731 - test fixture wiring
        CAPABILITY: frozenset({SERVER_A}),
        DENIED_BY_POLICY_CAPABILITY: frozenset({SERVER_A}),
    }
    for mode, capability in (
        (PolicyMode.DELEGATED, CAPABILITY),
        (PolicyMode.ASK, CAPABILITY),
        (PolicyMode.ASK, DENIED_BY_POLICY_CAPABILITY),
    ):
        context = _context(mode=mode, allowed=(CAPABILITY, DENIED_BY_POLICY_CAPABILITY))
        credential_reads: list[str] = []

        def _counting_source() -> EnvResolver:
            credential_reads.append("read")
            return EnvResolver(FXT_ROOT / "fixture.env")

        dispatcher = _dispatcher(
            registry=registry,
            approvals=_unused_approvals(),
            grants=both_grants,
            credentials=_counting_source,
        )
        outcome = await dispatcher.dispatch(
            context=context, capability_id=capability, payload=GOOD_PAYLOAD
        )
        assert outcome.reason == DispatchReason.APPROVAL_REQUIRED_BUT_ABSENT.value
        assert credential_reads == []  # credentials come after the approval seam


@pytest.mark.anyio
async def test_approval_presented_for_approval_free_call_is_denied() -> None:
    """An unvalidated decision must never ride along on a weaker call."""
    executor, calls = _recording_executor()
    dispatcher = _dispatcher(
        registry=_registry_with(_descriptor_document(READ_CAPABILITY, "read")),
        approvals=_unused_approvals(),
        executor=executor,
    )
    outcome = await dispatcher.dispatch(
        context=_context(allowed=(READ_CAPABILITY,)),
        capability_id=READ_CAPABILITY,
        payload=GOOD_PAYLOAD,
        approval_id=uuid.uuid4(),
    )
    assert outcome.reason == DispatchReason.APPROVAL_PRESENTED_BUT_NOT_REQUIRED.value
    assert outcome.effect == "not_attempted"
    assert calls == []


# ---------------------------------------------------------------------------
# Credential seam: real .env resolver, value-free denial, redaction
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_missing_credential_reference_denies_value_free(fxt_dir: Path) -> None:
    dispatcher = _dispatcher(
        registry=_registry_with(_descriptor_document(READ_CAPABILITY, "read")),
        approvals=_unused_approvals(),
    )
    outcome = await dispatcher.dispatch(
        context=_context(
            references=("MAI_FXT_ABSENT",), allowed=(READ_CAPABILITY,)
        ),
        capability_id=READ_CAPABILITY,
        payload=GOOD_PAYLOAD,
    )
    assert outcome.reason == DispatchReason.CREDENTIAL_RESOLUTION_FAILED.value
    assert outcome.approval_code == "missing_key"
    assert outcome.effect == "not_attempted"
    _sweep(outcome.to_json(), SECRET_VALUE)


@pytest.mark.anyio
async def test_credential_source_failure_denies_without_effect(fxt_dir: Path) -> None:
    def _broken_source() -> EnvResolver:
        raise FileNotFoundError("env source missing")

    dispatcher = _dispatcher(
        registry=_registry_with(_descriptor_document(READ_CAPABILITY, "read")),
        approvals=_unused_approvals(),
        credentials=_broken_source,
    )
    outcome = await dispatcher.dispatch(
        context=_context(references=(SECRET_NAME,), allowed=(READ_CAPABILITY,)),
        capability_id=READ_CAPABILITY,
        payload=GOOD_PAYLOAD,
    )
    assert outcome.reason == DispatchReason.CREDENTIAL_SOURCE_UNAVAILABLE.value
    assert outcome.effect == "not_attempted"


@pytest.mark.anyio
async def test_executed_output_is_redacted_and_bounded(fxt_dir: Path) -> None:
    """The executor echoes the resolved secret (a leak attempt) and far
    too much output: the receipt carries neither the secret nor more
    than the budget."""
    secret_echo = f"token={SECRET_VALUE} " + "x" * (default_output_budget_bytes + 99)
    executor, calls = _recording_executor(output=secret_echo)
    dispatcher = _dispatcher(
        registry=_registry_with(_descriptor_document(READ_CAPABILITY, "read")),
        approvals=_unused_approvals(),
        executor=executor,
    )
    outcome = await dispatcher.dispatch(
        context=_context(references=(SECRET_NAME,), allowed=(READ_CAPABILITY,)),
        capability_id=READ_CAPABILITY,
        payload=GOOD_PAYLOAD,
    )
    assert outcome.allowed is True and outcome.effect == "executed"
    assert outcome.output is not None
    assert outcome.output.truncated is True
    assert outcome.output.text.startswith("token=" + REDACTED)
    assert (
        len(outcome.output.text.encode("utf-8")) <= default_output_budget_bytes
    )
    assert outcome.output.omitted_bytes > 0
    _sweep(outcome.to_json(), SECRET_VALUE)
    assert calls and calls[0].credentials[SECRET_NAME].value == SECRET_VALUE


@pytest.mark.anyio
async def test_resolved_credentials_reach_executor_only_as_secret_values(
    fxt_dir: Path,
) -> None:
    """C03 rule 2/6: the context carries names; values materialize only
    at the effect boundary as ``SecretValue``."""
    executor, calls = _recording_executor(output="ok")
    dispatcher = _dispatcher(
        registry=_registry_with(_descriptor_document(READ_CAPABILITY, "read")),
        approvals=_unused_approvals(),
        executor=executor,
    )
    outcome = await dispatcher.dispatch(
        context=_context(references=(SECRET_NAME,), allowed=(READ_CAPABILITY,)),
        capability_id=READ_CAPABILITY,
        payload=GOOD_PAYLOAD,
    )
    assert outcome.effect == "executed"
    call = calls[0]
    assert call.context.credential_references == (SECRET_NAME,)
    assert repr(call.credentials[SECRET_NAME]) == "SecretValue(<redacted>)"
    _sweep(call.context.to_json(), SECRET_VALUE)


# ---------------------------------------------------------------------------
# Effect honesty: executor crash / contract violation is never "executed"
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_executor_crash_reports_unknown_effect_with_type_only() -> None:
    async def _exploding(call: CapabilityCall) -> ExecutorResult:
        raise RuntimeError(f"boom with {SECRET_VALUE} inside")

    dispatcher = _dispatcher(
        registry=_registry_with(_descriptor_document(READ_CAPABILITY, "read")),
        approvals=_unused_approvals(),
        executor=_exploding,
    )
    outcome = await dispatcher.dispatch(
        context=_context(allowed=(READ_CAPABILITY,)),
        capability_id=READ_CAPABILITY,
        payload=GOOD_PAYLOAD,
    )
    assert outcome.effect == "unknown"
    assert outcome.error_type == "RuntimeError"
    assert outcome.executor_ok is None and outcome.output is None
    _sweep(outcome.to_json(), SECRET_VALUE, "boom")


@pytest.mark.anyio
async def test_executor_contract_violation_is_unknown_not_executed() -> None:
    async def _bad_contract(call: CapabilityCall) -> ExecutorResult:
        return "not an ExecutorResult"  # type: ignore[return-value]

    dispatcher = _dispatcher(
        registry=_registry_with(_descriptor_document(READ_CAPABILITY, "read")),
        approvals=_unused_approvals(),
        executor=_bad_contract,
    )
    outcome = await dispatcher.dispatch(
        context=_context(allowed=(READ_CAPABILITY,)),
        capability_id=READ_CAPABILITY,
        payload=GOOD_PAYLOAD,
    )
    assert outcome.effect == "unknown"
    assert outcome.reason == DispatchReason.EXECUTOR_CONTRACT_VIOLATION.value


# ---------------------------------------------------------------------------
# Gate scenario (APPROVAL-PAYLOAD dispatcher half) — real Postgres, real
# benign fixture executable writing a real counter file
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_gate_scenario_denied_revoked_altered_leave_counter_unchanged(
    sessions, store, clean_approvals, fxt_dir, counter_program, counter_path
) -> None:
    """The brief's scenario: the actual dispatcher invokes the benign
    fixture executable writing a counter; denied/revoked/altered-server
    calls leave the counter unchanged and the allowed call records one
    effect."""
    registry = _registry_with(
        _descriptor_document(CAPABILITY, "privileged"),
        _descriptor_document(READ_CAPABILITY, "read"),
    )
    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=store,
        grants=lambda: _grants(),
        policy=lambda: _policy(),
        adopted=lambda server_id: server_id == SERVER_A,
        credentials=lambda: EnvResolver(FXT_ROOT / "fixture.env"),
        executor=subprocess_executor(counter_program),
    )
    operation = {"op": "count", "counter_path": str(counter_path)}
    altered = {**operation, "v": 2}
    assert await _read_counter(counter_path) == 0

    # 1. Denied call: capability not granted at all → no effect.
    outcome = await dispatcher.dispatch(
        context=_context(allowed=(READ_CAPABILITY,)),
        capability_id=CAPABILITY,
        payload=operation,
    )
    assert outcome.reason == "capability_not_granted"
    assert await _read_counter(counter_path) == 0

    # 2. Approval-required call without a decision → no effect.
    outcome = await dispatcher.dispatch(
        context=_context(), capability_id=CAPABILITY, payload=operation
    )
    assert outcome.reason == DispatchReason.APPROVAL_REQUIRED_BUT_ABSENT.value
    assert await _read_counter(counter_path) == 0

    # 3. Altered payload: approval bound to the operation, call carries
    #    the altered payload ⇒ payload_mismatch, counter unchanged.
    approval = await _create_approval(store, payload=operation)
    outcome = await dispatcher.dispatch(
        context=_context(),
        capability_id=CAPABILITY,
        payload=altered,
        approval_id=approval.approval_id,
    )
    assert outcome.reason == DispatchReason.APPROVAL_REJECTED.value
    assert outcome.approval_code == "payload_mismatch"
    assert await _read_counter(counter_path) == 0

    # 4. Altered server: the capability is granted on BOTH servers, the
    #    call on SERVER_B passes every policy gate, but the decision was
    #    bound to SERVER_A ⇒ target_mismatch, counter unchanged.
    approval = await _create_approval(store, server_id=SERVER_A, payload=operation)
    outcome = await CapabilityDispatcher(
        registry=registry,
        approvals=store,
        grants=lambda: {CAPABILITY: frozenset({SERVER_A, SERVER_B})},
        policy=lambda: _policy(),
        adopted=lambda server_id: server_id in (SERVER_A, SERVER_B),
        credentials=lambda: EnvResolver(FXT_ROOT / "fixture.env"),
        executor=subprocess_executor(counter_program),
    ).dispatch(
        context=_context(server_id=SERVER_B),
        capability_id=CAPABILITY,
        payload=operation,
        approval_id=approval.approval_id,
    )
    assert outcome.reason == DispatchReason.APPROVAL_REJECTED.value
    assert outcome.approval_code == "target_mismatch"
    assert await _read_counter(counter_path) == 0

    # 5. Policy changed since the decision (revocation channel: the real
    #    GrantConfigStore bumps the revision). The call itself would
    #    still evaluate allowed under the NEW policy, but the approval is
    #    bound to revision 1 ⇒ revision_mismatch, counter unchanged.
    grants_root = FXT_ROOT / "policy-root"
    grants_store = GrantConfigStore(grants_root)
    grants_store.write(
        {
            "policy": {"revision": 1, "denied_capabilities": []},
            "grants": [
                {"capability_id": CAPABILITY, "server_ids": [str(SERVER_A)]}
            ],
        },
        expected_revision=0,
    )
    approval = await _create_approval(store, payload=operation, policy_revision=1)
    grants_store.write(
        {
            "policy": {"revision": 2, "denied_capabilities": []},
            "grants": [
                {"capability_id": CAPABILITY, "server_ids": [str(SERVER_A)]}
            ],
        },
        expected_revision=1,
    )
    dispatcher_revoked = CapabilityDispatcher(
        registry=registry,
        approvals=store,
        grants=lambda: grants_store.read().grants,
        policy=lambda: grants_store.read().policy,
        adopted=lambda server_id: server_id == SERVER_A,
        credentials=lambda: EnvResolver(FXT_ROOT / "fixture.env"),
        executor=subprocess_executor(counter_program),
    )
    outcome = await dispatcher_revoked.dispatch(
        context=_context(),
        capability_id=CAPABILITY,
        payload=operation,
        approval_id=approval.approval_id,
    )
    assert outcome.reason == DispatchReason.APPROVAL_REJECTED.value
    assert outcome.approval_code == "revision_mismatch"
    assert await _read_counter(counter_path) == 0

    # 6. The one allowed call: valid approval, exact binding ⇒ exactly
    #    one effect, receipt records the executed boundary.
    approval = await _create_approval(store, payload=operation, policy_revision=7)
    outcome = await dispatcher.dispatch(
        context=_context(),
        capability_id=CAPABILITY,
        payload=operation,
        approval_id=approval.approval_id,
    )
    assert outcome.allowed is True
    assert outcome.effect == "executed" and outcome.executor_ok is True
    assert outcome.approval_spent is True
    assert outcome.approval_id == approval.approval_id
    assert outcome.output is not None
    assert outcome.output.text == "count=1\n"
    assert outcome.effect_class == "privileged"
    assert outcome.policy_revision == 7
    assert await _read_counter(counter_path) == 1

    # 7. Replay of the same decision: already_consumed, counter frozen.
    outcome = await dispatcher.dispatch(
        context=_context(),
        capability_id=CAPABILITY,
        payload=operation,
        approval_id=approval.approval_id,
    )
    assert outcome.reason == DispatchReason.APPROVAL_REJECTED.value
    assert outcome.approval_code == "already_consumed"
    assert await _read_counter(counter_path) == 1


@pytest.mark.anyio
async def test_recheck_denies_on_revocation_between_gate_and_effect(
    sessions, store, clean_approvals, fxt_dir, counter_path
) -> None:
    """C03 rule 3 / G2 step 4: grants fetched again immediately before
    the effect. A mid-flight revocation denies the effect even though a
    decision was already consumed — the receipt says so honestly."""
    registry = _registry_with(_descriptor_document(CAPABILITY, "privileged"))
    generations = iter([_grants(), {}])  # second fetch: grant gone

    dispatcher = _dispatcher(
        registry=registry,
        approvals=store,
        grants=lambda: next(generations),
    )
    approval = await _create_approval(store)
    outcome = await dispatcher.dispatch(
        context=_context(),
        capability_id=CAPABILITY,
        payload=GOOD_PAYLOAD,
        approval_id=approval.approval_id,
    )
    assert outcome.allowed is False
    assert outcome.reason == "capability_not_granted"  # recheck verdict
    assert outcome.approval_spent is True  # decision was consumed first
    assert outcome.effect == "not_attempted"
    assert await _read_counter(counter_path) == 0
    spent = await store.get(approval.approval_id)
    assert spent is not None and spent.status == "consumed"


@pytest.mark.anyio
async def test_concurrent_dispatches_share_one_decision_exactly_one_effect(
    sessions, store, clean_approvals, fxt_dir, counter_program, counter_path
) -> None:
    """Six real concurrent dispatches over one approval: exactly one
    external effect; the losers are already_consumed denials."""
    registry = _registry_with(_descriptor_document(CAPABILITY, "privileged"))
    dispatcher = _dispatcher(
        registry=registry,
        approvals=store,
        executor=subprocess_executor(counter_program),
    )
    operation = {"op": "count", "counter_path": str(counter_path)}
    approval = await _create_approval(store, payload=operation)

    async def _one() -> object:
        return await dispatcher.dispatch(
            context=_context(),
            capability_id=CAPABILITY,
            payload={"op": "count", "counter_path": str(counter_path)},
            approval_id=approval.approval_id,
        )

    outcomes = await asyncio.gather(*(_one() for _ in range(6)))
    executed = [o for o in outcomes if o.effect == "executed"]  # type: ignore[attr-defined]
    rejected = [o for o in outcomes if o.effect == "not_attempted"]  # type: ignore[attr-defined]
    assert len(executed) == 1
    assert all(
        o.reason == DispatchReason.APPROVAL_REJECTED.value
        and o.approval_code == "already_consumed"
        for o in rejected
    )
    assert len(rejected) == 5
    assert await _read_counter(counter_path) == 1


@pytest.mark.anyio
async def test_read_call_executes_without_consuming_any_decision(
    sessions, store, clean_approvals, fxt_dir, counter_program, counter_path
) -> None:
    """Read class in delegated mode: approval-free execution is the one
    sanctioned shortcut; the receipt records approval_spent=False."""
    registry = _registry_with(_descriptor_document(READ_CAPABILITY, "read"))
    dispatcher = _dispatcher(
        registry=registry,
        approvals=store,
        executor=subprocess_executor(counter_program),
    )
    outcome = await dispatcher.dispatch(
        context=_context(allowed=(READ_CAPABILITY,)),
        capability_id=READ_CAPABILITY,
        payload={"op": "count", "counter_path": str(counter_path)},
    )
    assert outcome.allowed is True and outcome.effect == "executed"
    assert outcome.approval_spent is False and outcome.approval_id is None
    assert outcome.effect_class == "read"
    assert await _read_counter(counter_path) == 1


@pytest.mark.anyio
async def test_receipt_json_shape_is_frozen_and_value_free(
    sessions, store, clean_approvals, fxt_dir, counter_program, counter_path
) -> None:
    """The audit receipt embeds the payload hash — never the payload —
    and never the resolved secret material."""
    dispatcher = _dispatcher(
        registry=_registry_with(_descriptor_document(READ_CAPABILITY, "read")),
        approvals=store,
        executor=subprocess_executor(counter_program),
    )
    outcome = await dispatcher.dispatch(
        context=_context(
            references=(SECRET_NAME,),
            allowed=(READ_CAPABILITY,),
            parent_scope=ParentScope(run_id=RUN_2, server_id=SERVER_A),
        ),
        capability_id=READ_CAPABILITY,
        payload={"counter_path": str(counter_path), "op": "count"},
    )
    document = outcome.to_json()
    assert tuple(document) == (
        "op_id",
        "ts",
        "capability_id",
        "server_id",
        "run_id",
        "principal",
        "mode",
        "allowed",
        "reason",
        "policy_revision",
        "effect_class",
        "payload_hash",
        "effect",
        "approval_id",
        "approval_spent",
        "approval_code",
        "executor_ok",
        "output",
        "error_type",
        "credential_references",
    )
    assert document["payload_hash"] == canonical_payload_hash(
        {"counter_path": str(counter_path), "op": "count"}
    )
    _sweep(document, SECRET_VALUE, "counter_path")
    assert uuid.UUID(str(document["op_id"]))
    assert document["ts"].endswith("+00:00")
