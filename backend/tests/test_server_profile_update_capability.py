"""Focused tests for the ``server.profile.update`` agent capability (FRR T5).

The closed loop: the agent gets ``path_denied`` (host.file_read), calls
``server.profile.update`` to request new read roots, the captain approves
through the EXISTING approval machinery, the roots extend, the read works.

Claims proven here:

1. Descriptor contract: the internal-API builder gates pass
   (``assert_agent_ready_descriptor``), the wire shape is frozen
   (mutate / controller / resource_scope ``server_profile`` / self-grant),
   and every payload Field carries a ≥12-char model-correctable description.
2. The executor is an add-only merge through the SAME canonical pipeline as
   the HTTP surface (``ProfileProjection.apply_upsert``): existing roots are
   kept, new ones appended (dedup), prompt.md untouched.
3. Every refusal is pre-effect and structured: the host.file_read credential
   deny list (REUSED by import, never copied), stale ``expected_revision``,
   invalid roots (relative / ``/`` / duplicates / >16 per request).
4. SECURITY CORE — forced captain approval: interactive sessions run
   DELEGATED, where a plain ``mutate`` would self-execute. The bridge seam
   (``dispatch_once``) pins the context mode to ASK for this capability
   alone: a DELEGATED dispatch answers ``approval_required_but_absent``
   (never executed), and a DELEGATED dispatch of a sibling READ capability
   in the very same bridge still executes without any approval.

Harness: the reviewed internal-api composition
(``build_internal_api_capabilities``) over a real ``ProfileFileStore`` data
root; the write-pipeline claims run against a real disposable Postgres
(``MAI_TEST_DATABASE_URL``) while every pre-effect refusal runs without a
database (they happen before ``apply_upsert`` — proven by the unchanged file).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from mai.agents.tools import CapabilityToolBridge
from mai.capabilities.dispatch import CapabilityCall, CapabilityDispatcher
from mai.capabilities.errors import PreEffectError
from mai.capabilities.host_file_read import _DENY_PATH_PREFIXES
from mai.capabilities.internal_api import (
    INTERNAL_API_CAPABILITY_IDS,
    SERVER_PROFILE_UPDATE_CAPABILITY,
    ServerProfileUpdatePayload,
    InternalApiError,
    assert_agent_ready_descriptor,
    build_internal_api_capabilities,
)
from mai.plugins.registry import CapabilityRegistry
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import ExecutionContext, Policy, PolicyMode
from mai.profiles.files import ProfileFileStore

pytestmark = pytest.mark.anyio

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

HOST_KEY = "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"
SERVER_A = uuid.UUID("9a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
RUN_1 = uuid.UUID("7d8e9f0a-1b2c-4d3e-8f4a-5b6c7d8e9f0a")

#: The lazy-approval harness env file (the sibling suites' convention).
FIXTURE_ENV_FILE = Path(os.environ.get("MAI_ENV_FILE", "/tmp/mai-fxt-frr-t5.env"))


def _profile_text(server_id: uuid.UUID, *, roots: list[str] | None = None) -> str:
    """One canonical profile (the exact frozen field order)."""
    roots_block = ""
    if roots:
        roots_block = "file_read_roots:\n" + "".join(f"  - {r}\n" for r in roots)
    return (
        f"schema_version: 1\n"
        f'server_id: "{server_id}"\n'
        f'name: "build-host"\n'
        f"prompt_revision: 3\n"
        f"environment: linux_ssh\n"
        f"ssh:\n"
        f'  host: "198.51.100.10"\n'
        f"  port: 22\n"
        f'  user: "deploy"\n'
        f'  host_key: "{HOST_KEY}"\n'
        f'  credential_ref: "MAI_SSH_KEY_BUILD_HOST"\n'
        f"privilege: none\n"
        f"capabilities:\n"
        f"  - shell.exec\n"
        f"{roots_block}"
    )


# ---------------------------------------------------------------------------
# Live fixture: real disposable Postgres (write-pipeline claims only)
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(ALEMBIC_INI))


@pytest.fixture(scope="session")
def migrated_url() -> str:
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the apply_upsert claims need a real "
            "disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url
    try:
        with psycopg.connect(url) as conn:
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
            conn.commit()
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture
async def session_factory(migrated_url: str):
    from mai.database import create_app_engine

    engine, factory = create_app_engine(migrated_url)
    yield factory
    await engine.dispose()


# ---------------------------------------------------------------------------
# Composition harness (the production builder, real data root)
# ---------------------------------------------------------------------------


class _Sentinel:
    """A truthy stand-in for services a refusal path never touches."""


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data-root"


def _composition(
    *,
    session_factory: Any = _Sentinel(),
    data_root: Any = None,
):
    return build_internal_api_capabilities(
        applications=None,
        adoption=None,
        docs=None,
        service_map=None,
        session_factory=session_factory,
        data_root=data_root,
    )


def _executor(data_root: Path, session_factory: Any = _Sentinel()):
    composition = _composition(session_factory=session_factory, data_root=data_root)
    return composition.executors[SERVER_PROFILE_UPDATE_CAPABILITY]


def _descriptor():
    composition = _composition(session_factory=_Sentinel(), data_root="x")
    return next(
        d
        for d in composition.descriptors
        if d.identifier == SERVER_PROFILE_UPDATE_CAPABILITY
    )


def _context(mode: PolicyMode = PolicyMode.DELEGATED) -> ExecutionContext:
    return ExecutionContext(
        principal="principal",
        server_id=SERVER_A,
        run_id=RUN_1,
        parent_scope=None,
        profile_revision="1",
        prompt_revision="1",
        credential_references=(),
        allowed_capabilities=(
            SERVER_PROFILE_UPDATE_CAPABILITY,
            "docs.list",
        ),
        mode=mode,
    )


async def _run_executor(executor: Any, payload: dict[str, Any]) -> dict[str, Any]:
    call = CapabilityCall(
        descriptor=_descriptor(),
        payload=payload,
        context=_context(),
        credentials={},
        op_id=uuid.uuid4(),
    )
    result = await executor(call)
    return {"ok": result.ok, "envelope": json.loads(result.output)}


async def _seed_profile(store: ProfileFileStore, *, roots: list[str]) -> int:
    store.write(SERVER_A, _profile_text(SERVER_A, roots=roots), expected_revision=0)
    return store.read(SERVER_A).revision


def _profile_roots(store: ProfileFileStore) -> list[str]:
    import yaml

    profile_file = (
        store.root / "servers" / str(SERVER_A) / "profile.yaml"
    )
    document = yaml.safe_load(profile_file.read_text())
    return list(document.get("file_read_roots") or [])


# ---------------------------------------------------------------------------
# Descriptor contract (builder gates + frozen wire shape)
# ---------------------------------------------------------------------------


def test_capability_is_declared_auto_granted_and_agent_ready() -> None:
    assert SERVER_PROFILE_UPDATE_CAPABILITY == "server.profile.update"
    assert SERVER_PROFILE_UPDATE_CAPABILITY in INTERNAL_API_CAPABILITY_IDS

    descriptor = _descriptor()
    assert descriptor.version == "1.0.0"
    assert descriptor.effect_class == "mutate"
    assert descriptor.resource_scope == ("server_profile",)
    assert descriptor.execution_environments == ("controller",)
    assert descriptor.required_grants == (SERVER_PROFILE_UPDATE_CAPABILITY,)
    # The builder gate (≥30-char tool description + ≥12-char per-field
    # descriptions) already ran at composition; assert it explicitly.
    assert assert_agent_ready_descriptor(descriptor) is descriptor

    for name, field in ServerProfileUpdatePayload.model_fields.items():
        description = field.description or ""
        assert len(description) >= 12, (name, description)


def test_payload_bounds_are_frozen() -> None:
    from pydantic import ValidationError

    model = ServerProfileUpdatePayload.model_validate(
        {
            "roots": ["/srv/data"],
            "expected_revision": 1,
            "reason": "the deploy job writes under /srv/data",
        }
    )
    assert model.roots == ["/srv/data"]

    with pytest.raises(ValidationError):
        ServerProfileUpdatePayload.model_validate(
            {"roots": [], "expected_revision": 1, "reason": "empty"}
        )
    with pytest.raises(ValidationError):
        ServerProfileUpdatePayload.model_validate(
            {
                "roots": [f"/srv/{index}" for index in range(17)],
                "expected_revision": 1,
                "reason": "too many",
            }
        )
    with pytest.raises(ValidationError):
        ServerProfileUpdatePayload.model_validate(
            {"roots": ["/srv/data"], "expected_revision": 0, "reason": "rev"}
        )
    with pytest.raises(ValidationError):
        ServerProfileUpdatePayload.model_validate(
            {"roots": ["/srv/data"], "expected_revision": 1, "reason": "r" * 201}
        )


# ---------------------------------------------------------------------------
# Pre-effect refusals (no database: they happen before apply_upsert)
# ---------------------------------------------------------------------------


async def test_composition_fail_closed_when_services_missing(tmp_path: Path) -> None:
    composition = _composition(session_factory=None, data_root=None)
    executor = composition.executors[SERVER_PROFILE_UPDATE_CAPABILITY]
    result = await _run_executor(
        executor,
        {"roots": ["/srv/data"], "expected_revision": 1, "reason": "needs roots"},
    )
    assert result["ok"] is False
    assert result["envelope"]["status"] == "refused"
    assert result["envelope"]["code"] == "unavailable"


async def test_stale_revision_refuses_without_effect(tmp_path: Path) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    await _seed_profile(store, roots=["/srv/data"])
    before = (store.root / "servers" / str(SERVER_A) / "profile.yaml").read_text()

    result = await _run_executor(
        _executor(store.root),
        {"roots": ["/opt/tools"], "expected_revision": 999, "reason": "stale"},
    )
    assert result["ok"] is False
    assert result["envelope"]["status"] == "refused"
    assert result["envelope"]["code"] == "revision_conflict"
    assert (
        store.root / "servers" / str(SERVER_A) / "profile.yaml"
    ).read_text() == before


@pytest.mark.parametrize(
    "roots",
    [
        ["/etc/shadow"],
        ["/etc/shadow/conf.d/x"],
        ["/root/.ssh"],
        ["/root/.ssh/authorized_keys"],
    ],
)
async def test_deny_list_roots_refused_and_profile_unchanged(
    tmp_path: Path, roots: list[str]
) -> None:
    """The SAME credential-material deny list as host.file_read (imported,
    not copied) refuses the extension — the capability can never widen
    reads onto exactly the paths the read capability hard-denies."""
    store = ProfileFileStore(tmp_path / "data-root")
    await _seed_profile(store, roots=["/srv/data"])
    before = (store.root / "servers" / str(SERVER_A) / "profile.yaml").read_text()

    result = await _run_executor(
        _executor(store.root),
        {"roots": roots, "expected_revision": 1, "reason": "needs these"},
    )
    assert result["ok"] is False
    assert result["envelope"]["status"] == "refused"
    assert result["envelope"]["code"] == "path_denied"
    assert (
        store.root / "servers" / str(SERVER_A) / "profile.yaml"
    ).read_text() == before
    # Cross-check the reuse: every refused root is denied by the canonical list.
    for root in roots:
        assert any(
            root == prefix or root.startswith(prefix + "/")
            for prefix in _DENY_PATH_PREFIXES
        )


@pytest.mark.parametrize(
    "roots",
    [
        ["srv/data"],  # relative
        ["/"],  # the filesystem root
        ["/srv/data", "/srv/data"],  # duplicate within the request
    ],
)
async def test_invalid_roots_refused_without_effect(
    tmp_path: Path, roots: list[str]
) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    await _seed_profile(store, roots=["/srv/data"])
    before = (store.root / "servers" / str(SERVER_A) / "profile.yaml").read_text()

    result = await _run_executor(
        _executor(store.root),
        {"roots": roots, "expected_revision": 1, "reason": "invalid"},
    )
    assert result["ok"] is False
    assert result["envelope"]["status"] == "refused"
    assert result["envelope"]["code"] == "path_denied"
    assert (
        store.root / "servers" / str(SERVER_A) / "profile.yaml"
    ).read_text() == before


async def test_oversized_roots_list_refused_by_the_payload_model(
    tmp_path: Path,
) -> None:
    """>16 roots per request: the payload model refuses (invalid_request)
    inside the uniform executor BEFORE the operation ever runs."""
    store = ProfileFileStore(tmp_path / "data-root")
    await _seed_profile(store, roots=["/srv/data"])
    before = (store.root / "servers" / str(SERVER_A) / "profile.yaml").read_text()

    result = await _run_executor(
        _executor(store.root),
        {
            "roots": [f"/srv/{index}" for index in range(17)],
            "expected_revision": 1,
            "reason": "too many",
        },
    )
    assert result["ok"] is False
    assert result["envelope"]["status"] == "refused"
    assert result["envelope"]["code"] == "invalid_request"
    assert (
        store.root / "servers" / str(SERVER_A) / "profile.yaml"
    ).read_text() == before


async def test_merge_refuses_past_the_canonical_root_cap(tmp_path: Path) -> None:
    """64 roots exist already; one more would breach the canonical cap
    (MAX_FILE_READ_ROOTS) — refused before any effect."""
    from mai.contracts.server import MAX_FILE_READ_ROOTS

    store = ProfileFileStore(tmp_path / "data-root")
    full = [f"/srv/dir-{index:02d}" for index in range(MAX_FILE_READ_ROOTS)]
    await _seed_profile(store, roots=full)

    result = await _run_executor(
        _executor(store.root),
        {"roots": ["/srv/one-more"], "expected_revision": 1, "reason": "more"},
    )
    assert result["ok"] is False
    assert result["envelope"]["code"] == "too_many_roots"
    assert _profile_roots(store) == full


# ---------------------------------------------------------------------------
# The write pipeline (real disposable Postgres via apply_upsert)
# ---------------------------------------------------------------------------


async def test_add_only_merge_persists_through_the_canonical_pipeline(
    tmp_path: Path, session_factory: Any
) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    revision = await _seed_profile(store, roots=["/srv/data"])

    result = await _run_executor(
        _executor(store.root, session_factory=session_factory),
        {
            "roots": ["/opt/tools", "/srv/data", "/var/log/app"],
            "expected_revision": revision,
            "reason": "the deploy job writes under /opt/tools",
        },
    )
    assert result["ok"] is True, result
    assert result["envelope"]["status"] == "ok"
    assert result["envelope"]["revision"] == revision + 1
    assert len(result["envelope"]["sha256"]) == 64
    # Add-only merge: existing root preserved, duplicates dropped, new
    # roots appended in request order.
    assert result["envelope"]["roots"] == ["/srv/data", "/opt/tools", "/var/log/app"]
    assert _profile_roots(store) == ["/srv/data", "/opt/tools", "/var/log/app"]

    # prompt.md is untouched (never written by this capability).
    assert not (store.root / "servers" / str(SERVER_A) / "prompt.md").exists()

    # The canonical YAML renders file_read_roots AFTER capabilities.
    stored = (store.root / "servers" / str(SERVER_A) / "profile.yaml").read_text()
    assert stored.index("capabilities:") < stored.index("file_read_roots:")


async def test_second_merge_dedupes_existing_roots(
    tmp_path: Path, session_factory: Any
) -> None:
    store = ProfileFileStore(tmp_path / "data-root")
    revision = await _seed_profile(store, roots=["/srv/data", "/opt/tools"])

    result = await _run_executor(
        _executor(store.root, session_factory=session_factory),
        {
            "roots": ["/opt/tools", "/srv/new"],
            "expected_revision": revision,
            "reason": "repeat request",
        },
    )
    assert result["ok"] is True, result
    assert result["envelope"]["roots"] == ["/srv/data", "/opt/tools", "/srv/new"]
    assert _profile_roots(store) == ["/srv/data", "/opt/tools", "/srv/new"]


# ---------------------------------------------------------------------------
# SECURITY CORE: forced captain approval in a DELEGATED context
# ---------------------------------------------------------------------------


def _roots_bridge(
    context: ExecutionContext,
    approvals: ApprovalStore,
    *,
    data_root: Any = "unused",
):
    """The REAL composed surface through the REAL bridge → dispatcher seam."""
    composition = _composition(session_factory=_Sentinel(), data_root=data_root)
    registry = CapabilityRegistry()
    for descriptor in composition.descriptors:
        registry.register(descriptor)
    FIXTURE_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_ENV_FILE.write_text("MAI_FIXTURE=value\n")
    from mai.credentials.env import EnvResolver

    def _route(call: Any) -> Any:
        # Belt-and-braces: the forced-ASK denial means the profile executor
        # must never be reached from a DELEGATED dispatch (the outcome test
        # below proves it; this guard turns a regression into a loud error).
        if call.descriptor.identifier == SERVER_PROFILE_UPDATE_CAPABILITY:
            return _never_executes(call)
        return composition.executors[call.descriptor.identifier](call)

    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=approvals,
        grants=lambda: {
            descriptor.identifier: {SERVER_A}
            for descriptor in registry.list_capabilities()
        },
        policy=lambda: Policy(revision=1, denied_capabilities=frozenset()),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(FIXTURE_ENV_FILE),
        executor=_route,
    )
    return CapabilityToolBridge(
        registry=registry, dispatcher=dispatcher, context=context
    )


def _never_executes(call: Any) -> Any:
    raise AssertionError(f"executor must not run for {call.descriptor.identifier}")


def _unused_approvals() -> ApprovalStore:
    """A real store over a lazily-connecting engine (never dialed)."""
    from mai.database import create_app_engine

    _, sessions = create_app_engine(
        "postgresql://unused:unused@127.0.0.1:1/unused"
    )
    return ApprovalStore(sessions)


async def test_forced_approval_in_delegated_context(tmp_path: Path) -> None:
    """SECURITY CORE: interactive sessions run DELEGATED, where a plain
    mutate executes without approval. ``dispatch_once`` pins ASK for this
    capability alone, so the dispatcher answers
    ``approval_required_but_absent`` — never executed — and the same
    DELEGATED bridge still runs a sibling READ capability approval-free."""
    payload = {
        "roots": ["/opt/escalation"],
        "expected_revision": 1,
        "reason": "the agent wants more read scope",
    }

    # DELEGATED context: the forced-ASK override must fire.
    delegated_bridge = _roots_bridge(
        _context(PolicyMode.DELEGATED),
        _unused_approvals(),
        data_root=tmp_path / "bridge-root",
    )
    outcome = await delegated_bridge.dispatch_once(
        SERVER_PROFILE_UPDATE_CAPABILITY, payload
    )
    assert outcome.allowed is False
    assert outcome.reason == "approval_required_but_absent"
    assert outcome.effect == "not_attempted"
    assert outcome.approval_spent is False
    # The executor never ran: no canonical file exists under the bridge root.
    assert not (tmp_path / "bridge-root" / "servers" / str(SERVER_A)).exists()

    # An explicit ASK context: same prompting verdict.
    ask_bridge = _roots_bridge(_context(PolicyMode.ASK), _unused_approvals())
    outcome = await ask_bridge.dispatch_once(SERVER_PROFILE_UPDATE_CAPABILITY, payload)
    assert outcome.reason == "approval_required_but_absent"
    assert outcome.effect == "not_attempted"

    # Sibling READ capability in the SAME DELEGATED bridge: executes WITHOUT
    # any approval requirement (docs.list lists an empty store here — the
    # effect lane ran, proving no approval gate is forced onto reads).
    docs_capability = "docs.list"
    delegated_bridge = _roots_bridge(_context(PolicyMode.DELEGATED), _unused_approvals())
    sibling = await delegated_bridge.dispatch_once(docs_capability, {})
    assert sibling.allowed is True
    assert sibling.effect == "executed"
    assert sibling.executor_ok is True
    assert sibling.reason == "allowed"


async def test_internal_api_error_is_not_a_pre_effect_marker() -> None:
    """The T1 PreEffectError marker intentionally does NOT extend to
    InternalApiError: its refusals ride the structured executor envelope
    (executed+executor_ok=False), unchanged in this slice."""
    error = InternalApiError("path_denied", "refused")
    assert not isinstance(error, PreEffectError)
