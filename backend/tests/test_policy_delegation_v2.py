"""Policy-delegation v2: exposure, research-only, scoped delegation (RV2).

Card ``runtime-v2/policy-delegation`` against ``docs/architecture/
runtime-v2.md`` §6 (capability exposure), §7 (delegation v2 in the ONE
engine), §13 pins and the RV2-DENY / RV2-DELEGATE scenario fixtures.

Real runtime surface (``MAI_TEST_DATABASE_URL`` disposable PostgreSQL):
REAL ``CapabilityDispatcher`` (store-backed grant/policy providers), REAL
``GrantConfigStore`` revision CAS, REAL ``ResourceLockService`` leases,
REAL approval store, and REAL engine worker trees for the durable
delegation scenarios. Two disposable scoped targets: server ``alpha``
(mutable) and server ``beta`` (research-only). Every denied mutation
carries a no-effect proof from the journaled fixture executor.
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
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents import delegate
from mai.agents import run as agent_run
from mai.agents.activity_store import (
    ObjectiveEventDraft,
    aappend_event,
    alist_events,
)
from mai.agents.budgets import BudgetError, BudgetErrorCode, BudgetLedger
from mai.agents.objective_store import (
    V2_HANDLER_NAME,
    abind_run,
    acreate_objective,
)
from mai.contracts.jobs import parse_job_event
from mai.contracts.objectives import ActorKind
from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID, SYSTEM_ASSIGNMENT_SOURCE
from mai.contracts.server import SCHEMA_VERSION as SERVER_SCHEMA_VERSION
from mai.agents.models import SCHEMA_VERSION as MODEL_SCHEMA_VERSION
from mai.agents.model_store import ModelConfigStore
from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.jobs import engine
from mai.jobs.engine import EngineClient
from mai.jobs.lifecycle import RunLifecycle
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.config import GrantConfigError, GrantConfigErrorCode, GrantConfigStore
from mai.policy.evaluate import (
    ExecutionContext,
    Policy,
    PolicyError,
    PolicyMode,
)
from mai.policy.exposure import ObjectiveExposure, project_exposure
from mai.policy.templates import (
    TemplateError,
    TemplateErrorCode,
    apply_research_only,
    project_research_only,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

FIXTURE_ROOT = Path("/tmp/mai-fxt-rv-policy-delegation")

RUN_TIMEOUT = 150.0

HANDLER_MODULE_NAME = "policy_v2_scenario_handlers"

# Frozen fixture identities (canonical lowercase UUID strings, C01 rule 1).
ALPHA_ID = "1a0f7d2e-5c3b-4a48-9e21-6f8d0a2b4c01"
BETA_ID = "2b1e8c3f-6d4c-4b59-8f32-7a9e1b3c5d02"
GAMMA_ID = "3c2f9d4a-7e5d-4c6a-9a43-8baf2c4d6e03"

READ_CAP = "fixture.read"
WRITE_CAP = "fixture.write"

ROLE = "operator assistant"
SECRET_NAME = "MAI_FXT_TOKEN"
SECRET_VALUE = "fxt-synthetic-token-value-policy-delegation"
HOST_KEY = "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"
PROMPT = "You administer the policy-delegation fixture host.\n"

#: The canonical fixture budget (budgets-suite document shape).
BUDGET_DOC: dict[str, object] = {
    "v": 1,
    "max_depth": 4,
    "max_children": 2,
    "max_active": 2,
    "max_steps": 20,
    "max_total_tokens": 5_000,
}


def _run_id(prefix: str) -> str:
    """Deterministic-shaped fresh run identity (canonical UUID)."""
    return str(uuid.uuid4())


def _run(coro: object) -> object:
    """Run one coroutine on the persistent test loop; block for the result."""
    global _TEST_LOOP
    with _TEST_LOOP_LOCK:
        if _TEST_LOOP is None:
            _TEST_LOOP = asyncio.new_event_loop()
            threading.Thread(target=_TEST_LOOP.run_forever, daemon=True).start()
    assert _TEST_LOOP is not None
    return asyncio.run_coroutine_threadsafe(coro, _TEST_LOOP).result()


_TEST_LOOP: asyncio.AbstractEventLoop | None = None
_TEST_LOOP_LOCK = threading.Lock()


def _canonical(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _wait_until(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


# ---------------------------------------------------------------------------
# pure-layer document builders
# ---------------------------------------------------------------------------


def _context_doc(
    run_id: str,
    *,
    server_id: str = ALPHA_ID,
    mode: str = "delegated",
    allowed: tuple[str, ...] = (READ_CAP, WRITE_CAP),
) -> dict:
    context = ExecutionContext(
        principal="hash:fixture-principal",
        server_id=uuid.UUID(server_id),
        run_id=uuid.UUID(run_id),
        parent_scope=None,
        profile_revision="profile-1",
        prompt_revision="prompt-1",
        credential_references=(),
        allowed_capabilities=allowed,
        mode=PolicyMode(mode),
    )
    return context.to_json()


def _run_document(
    run_id: str,
    *,
    server_id: str = ALPHA_ID,
    allowed: tuple[str, ...] = (READ_CAP, WRITE_CAP),
    task: str = "Coordinate the objective investigation.",
) -> dict:
    return {
        "v": agent_run.RUN_PAYLOAD_VERSION,
        "run_id": run_id,
        "server_id": server_id,
        "context": _context_doc(run_id, server_id=server_id, allowed=allowed),
        "role": ROLE,
        "task": task,
        "connection": "gateway",
        "profile": "fast",
    }


def _v2_child_spec(
    task: str,
    server_ids: tuple[str, ...],
    *,
    capabilities: tuple[str, ...] = (READ_CAP,),
    budget: dict[str, int] | None = None,
    **overrides: object,
) -> dict:
    spec: dict[str, object] = {
        "task": task,
        "server_ids": list(server_ids),
        "capabilities": list(capabilities),
    }
    if budget is not None:
        spec["budget"] = budget
    spec.update(overrides)
    return spec


def _v2_delegation_document(
    run_id: str,
    children: list[dict],
    *,
    server_id: str = ALPHA_ID,
    allowed: tuple[str, ...] = (READ_CAP, WRITE_CAP),
    **overrides: object,
) -> dict:
    document: dict[str, object] = {
        "v": delegate.DELEGATION_PAYLOAD_VERSION_V2,
        "run": _run_document(run_id, server_id=server_id, allowed=allowed),
        "children": children,
        "children_poll_seconds": 0.5,
    }
    document.update(overrides)
    return document


def _v2_intent(
    run_id: str | None = None,
    children: list[dict] | None = None,
    *,
    server_id: str = ALPHA_ID,
    allowed: tuple[str, ...] = (READ_CAP, WRITE_CAP),
    depth: int = 0,
    child_ids: tuple[str, ...] | None = None,
    root_budget: dict[str, object] | None = None,
    scope: tuple[str, ...] = (ALPHA_ID, BETA_ID),
    remaining: tuple[int | None, int | None] = (20, 5_000),
) -> tuple[
    delegate.DelegationIntentV2, delegate.ParentRunRow, tuple[str, ...], frozenset
]:
    """One parsed v2 intent + parent row + fresh child ids + scope set."""
    run_id = run_id or _run_id("parent")
    intent = delegate.parse_delegation_intent_v2(
        _canonical(
            _v2_delegation_document(
                run_id,
                children
                if children is not None
                else [_v2_child_spec("Investigate aspect-alpha.", (ALPHA_ID,))],
                server_id=server_id,
                allowed=allowed,
            )
        )
    )
    row = delegate.ParentRunRow(
        run_id=run_id,
        server_id=server_id,
        depth=depth,
        profile_revision=3,
        prompt_revision=7,
        model_profile="fixture-fast",
        root_budget=root_budget,
    )
    ids = child_ids or tuple(str(uuid.uuid4()) for _ in intent.children)
    return (
        intent,
        row,
        ids,
        frozenset(uuid.UUID(s) for s in scope),
    )


# ---------------------------------------------------------------------------
# pure: exposure projection (AC1)
# ---------------------------------------------------------------------------


class TestExposureProjection:
    def test_visible_targets_follow_grants_minus_denies(self) -> None:
        alpha, beta = uuid.UUID(ALPHA_ID), uuid.UUID(BETA_ID)
        exposure = project_exposure(
            grants={
                READ_CAP: [alpha, beta],
                WRITE_CAP: [alpha],
                "fixture.admin": [alpha],
            },
            policy=Policy(revision=2, denied_capabilities=frozenset({"fixture.admin"})),
            scope_server_ids=[alpha, beta],
        )
        assert exposure.capability_ids == (READ_CAP, WRITE_CAP)
        assert exposure.targets(READ_CAP) == (alpha, beta)
        assert exposure.targets(WRITE_CAP) == (alpha,)
        assert "fixture.admin" in exposure.policy_denied
        assert exposure.visible("fixture.admin") is False
        assert exposure.targets("fixture.admin") == ()

    def test_ungranted_plugin_is_never_advertised(self) -> None:
        alpha = uuid.UUID(ALPHA_ID)
        exposure = project_exposure(
            grants={READ_CAP: [alpha]},
            policy=Policy(revision=1),
            scope_server_ids=[alpha],
        )
        assert exposure.visible("fixture.write") is False
        assert "fixture.write" not in exposure.capability_ids
        assert "fixture.write" not in exposure.policy_denied
        assert "fixture.write" not in exposure.parent_denied

    def test_parent_denies_subtract_for_children(self) -> None:
        alpha, beta = uuid.UUID(ALPHA_ID), uuid.UUID(BETA_ID)
        exposure = project_exposure(
            grants={READ_CAP: [alpha, beta], WRITE_CAP: [alpha, beta]},
            policy=Policy(revision=1),
            scope_server_ids=[alpha, beta],
            parent_denies=(WRITE_CAP,),
        )
        assert exposure.visible(WRITE_CAP) is False
        assert exposure.parent_denied == (WRITE_CAP,)
        assert exposure.targets(READ_CAP) == (alpha, beta)

    def test_grant_outside_scope_is_invisible(self) -> None:
        alpha, beta, gamma = (
            uuid.UUID(ALPHA_ID),
            uuid.UUID(BETA_ID),
            uuid.UUID(GAMMA_ID),
        )
        exposure = project_exposure(
            grants={READ_CAP: [gamma]},
            policy=Policy(revision=1),
            scope_server_ids=[alpha, beta],
        )
        assert exposure.capability_ids == ()
        assert exposure.targets(READ_CAP) == ()

    def test_projection_is_deterministic_and_json_stable(self) -> None:
        alpha, beta = uuid.UUID(ALPHA_ID), uuid.UUID(BETA_ID)
        kwargs: dict[str, object] = {
            "grants": {WRITE_CAP: [beta, alpha], READ_CAP: [beta]},
            "policy": Policy(revision=4),
            "scope_server_ids": [beta, alpha],
        }
        first = project_exposure(**kwargs)  # type: ignore[arg-type]
        second = project_exposure(**kwargs)  # type: ignore[arg-type]
        assert first == second
        assert first.to_json() == second.to_json()
        assert ObjectiveExposure.from_json(first.to_json()) == first
        # sorted outputs: alpha < beta regardless of input order
        assert first.targets(WRITE_CAP) == (alpha, beta)

    def test_corrupt_inputs_reject_fail_closed(self) -> None:
        alpha = uuid.UUID(ALPHA_ID)
        with pytest.raises(PolicyError):
            project_exposure(
                grants={READ_CAP: ["not-a-uuid"]},
                policy=Policy(revision=1),
                scope_server_ids=[alpha],
            )
        with pytest.raises(PolicyError):
            project_exposure(
                grants={"BAD CAP": [alpha]},
                policy=Policy(revision=1),
                scope_server_ids=[alpha],
            )
        with pytest.raises(PolicyError):
            project_exposure(
                grants={READ_CAP: [alpha]},
                policy=Policy(revision=1),
                scope_server_ids=[],
            )


# ---------------------------------------------------------------------------
# pure: research_only template projection (AC2)
# ---------------------------------------------------------------------------


def _registry(read: tuple[str, ...] = (READ_CAP,), write: tuple[str, ...] = (WRITE_CAP,)):
    registry = CapabilityRegistry()
    for cap in read:
        registry.register(_descriptor(cap, "read"))
    for cap in write:
        registry.register(_descriptor(cap, "mutate"))
    return registry


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
            "output_schema": {"type": "object"},
            "execution_environments": ["linux_ssh"],
            "effect_class": effect_class,
            "resource_scope": [] if effect_class == "read" else ["target.exec"],
            "required_grants": [],
        },
        source=f"<{capability_id} descriptor>",
    )


class _UnreadableDescriptor:
    def __init__(self, identifier: str, effect_class: str) -> None:
        self.identifier = identifier
        self.effect_class = effect_class


class _FakeRegistry:
    """Test double: a registry carrying an UNCLASSIFIABLE effect class."""

    def __init__(self, *descriptors: object) -> None:
        self._descriptors = list(descriptors)

    def list_capabilities(self) -> tuple[object, ...]:
        return tuple(self._descriptors)


class TestResearchOnlyTemplate:
    def test_projection_grants_read_and_revokes_mutation(self, tmp_path: Path) -> None:
        alpha, beta = uuid.UUID(ALPHA_ID), uuid.UUID(BETA_ID)
        store = GrantConfigStore(tmp_path / "data-root")
        store.write(
            {
                "policy": {"revision": 1, "denied_capabilities": []},
                "grants": [
                    {"capability_id": READ_CAP, "server_ids": [ALPHA_ID, BETA_ID]},
                    {"capability_id": WRITE_CAP, "server_ids": [ALPHA_ID, BETA_ID]},
                ],
            },
            expected_revision=0,
        )
        current = store.read()
        projection = project_research_only(
            registry=_registry(),
            server_ids=[beta],
            current=current,
            expected_revision=1,
        )
        assert projection.template == "research_only"
        # read was ALREADY granted on beta: the audit records CHANGES only
        assert projection.granted == ()
        assert projection.revoked == (WRITE_CAP,)
        stored = store.write(projection.document, expected_revision=1)
        assert stored.revision == 2
        # beta: read only. alpha: untouched (read + write).
        assert set(stored.grants[READ_CAP]) == {alpha, beta}
        assert set(stored.grants[WRITE_CAP]) == {alpha}

    def test_projection_is_deterministic(self, tmp_path: Path) -> None:
        beta = uuid.UUID(BETA_ID)
        store = GrantConfigStore(tmp_path / "data-root")
        current = store.write(
            {
                "policy": {"revision": 1, "denied_capabilities": ["fixture.legacy"]},
                "grants": [
                    {"capability_id": READ_CAP, "server_ids": [BETA_ID]},
                ],
            },
            expected_revision=0,
        )
        first = project_research_only(
            registry=_registry(), server_ids=[beta], current=current, expected_revision=1
        )
        second = project_research_only(
            registry=_registry(), server_ids=[beta], current=current, expected_revision=1
        )
        assert first.document == second.document
        # global capability-level denies are preserved verbatim, never widened
        assert first.document["policy"]["denied_capabilities"] == ["fixture.legacy"]
        assert first.document["policy"]["revision"] == 2

    def test_stale_caller_observation_is_rejected(self, tmp_path: Path) -> None:
        beta = uuid.UUID(BETA_ID)
        store = GrantConfigStore(tmp_path / "data-root")
        current = store.write(
            {
                "policy": {"revision": 1, "denied_capabilities": []},
                "grants": [{"capability_id": READ_CAP, "server_ids": [BETA_ID]}],
            },
            expected_revision=0,
        )
        # a caller citing revision 0 while holding the revision-1 document
        # is inconsistent: the projection refuses to guess the next revision
        with pytest.raises(TemplateError) as error:
            project_research_only(
                registry=_registry(),
                server_ids=[beta],
                current=current,
                expected_revision=0,
            )
        assert error.value.code is TemplateErrorCode.STALE_REVISION

    def test_unknown_effect_class_fails_closed(self) -> None:
        registry = _FakeRegistry(
            _UnreadableDescriptor("fixture.exotic", "replicate"),
        )
        with pytest.raises(TemplateError) as error:
            project_research_only(
                registry=registry,  # type: ignore[arg-type]
                server_ids=[uuid.UUID(BETA_ID)],
                current=None,
                expected_revision=0,
            )
        assert error.value.code is TemplateErrorCode.UNSUPPORTED_EFFECT_CLASS

    def test_apply_research_only_cas_end_to_end(self, tmp_path: Path) -> None:
        beta = uuid.UUID(BETA_ID)
        store = GrantConfigStore(tmp_path / "data-root")
        stored, projection = apply_research_only(
            store=store,
            registry=_registry(),
            server_ids=[beta],
            expected_revision=0,
        )
        assert stored.revision == 1
        assert set(stored.grants) == {READ_CAP}
        # CAS: replaying the SAME expected revision is a store-level stale.
        with pytest.raises(GrantConfigError) as error:
            store.write(projection.document, expected_revision=0)
        assert error.value.code is GrantConfigErrorCode.STALE_REVISION


# ---------------------------------------------------------------------------
# pure: v2 parsing + child-handler seam
# ---------------------------------------------------------------------------


class TestDelegationV2Parsing:
    def test_v2_document_parses_with_scoped_children(self) -> None:
        run_id = _run_id("conv")
        intent = delegate.parse_delegation_intent_v2(
            _canonical(
                _v2_delegation_document(
                    run_id,
                    [
                        _v2_child_spec(
                            "Investigate aspect-alpha.",
                            (ALPHA_ID, BETA_ID),
                            budget={"max_steps": 3, "max_total_tokens": 900},
                        )
                    ],
                )
            )
        )
        assert isinstance(intent, delegate.DelegationIntentV2)
        (child,) = intent.children
        assert child.server_ids == (ALPHA_ID, BETA_ID)
        assert child.budget == delegate.ChildBudgetBounds(
            max_steps=3, max_total_tokens=900
        )
        assert intent.children_wait_seconds == delegate.DEFAULT_CHILDREN_WAIT_SECONDS

    def test_v1_document_rejected_by_v2_parser(self) -> None:
        run_id = _run_id("conv")
        document = {
            "v": delegate.DELEGATION_PAYLOAD_VERSION,
            "run": _run_document(run_id),
            "children": [
                {"task": "legacy child", "capabilities": [READ_CAP]},
            ],
        }
        with pytest.raises(delegate.DelegationError):
            delegate.parse_delegation_intent_v2(_canonical(document))

    def test_child_server_ids_are_strict(self) -> None:
        run_id = _run_id("conv")
        for bad in (
            [],
            [ALPHA_ID, ALPHA_ID],
            ["not-a-uuid"],
            [ALPHA_ID.upper()],
            "alpha",
        ):
            document = _v2_delegation_document(
                run_id, [_v2_child_spec("t", bad if isinstance(bad, list) else [])]
            )
            document["children"][0]["server_ids"] = bad
            with pytest.raises(delegate.DelegationError):
                delegate.parse_delegation_intent_v2(_canonical(document))

    def test_child_budget_bounds_are_strict(self) -> None:
        run_id = _run_id("conv")
        for bad in (
            {"max_steps": 1},
            {"max_total_tokens": 1},
            {"max_steps": 0, "max_total_tokens": 1},
            {"max_steps": 1, "max_total_tokens": 10**8},
            {"max_steps": 1, "max_total_tokens": 1, "extra": True},
            "nope",
        ):
            document = _v2_delegation_document(
                run_id,
                [_v2_child_spec("t", (ALPHA_ID,), budget=bad)],
            )
            with pytest.raises(delegate.DelegationError):
                delegate.parse_delegation_intent_v2(_canonical(document))

    def test_v2_child_payload_round_trips_through_its_one_parser(self) -> None:
        run_id = _run_id("child")
        payload = {
            "v": delegate.DELEGATION_PAYLOAD_VERSION_V2,
            "run": _run_document(run_id, allowed=(READ_CAP,)),
            "server_ids": [ALPHA_ID, BETA_ID],
            "budget": {"max_steps": 2, "max_total_tokens": 400},
        }
        parsed = delegate.parse_v2_child_payload(_canonical(payload))
        assert parsed.run.run_id == run_id
        assert parsed.run.context.server_id == uuid.UUID(ALPHA_ID)
        assert parsed.run.context.allowed_capabilities == (READ_CAP,)
        assert parsed.server_ids == (ALPHA_ID, BETA_ID)
        assert parsed.budget is not None
        assert parsed.budget.max_steps == 2
        for mutation in ({"v": 1}, {"v": 3}, {"v": "2"}):
            broken = dict(payload)
            broken.update(mutation)
            with pytest.raises(delegate.DelegationError):
                delegate.parse_v2_child_payload(_canonical(broken))

    def test_version_peek_and_child_handler_seam(self) -> None:
        v2_doc = _canonical(
            _v2_delegation_document(
                _run_id("conv"), [_v2_child_spec("t", (ALPHA_ID,))]
            )
        )
        v1_doc = _canonical(
            {
                "v": delegate.DELEGATION_PAYLOAD_VERSION,
                "run": _run_document(_run_id("conv")),
                "children": [{"task": "t", "capabilities": [READ_CAP]}],
            }
        )
        assert delegate._peek_delegation_version(v2_doc) == 2
        assert delegate._peek_delegation_version(v1_doc) == 1
        assert delegate._peek_delegation_version("not json at all {{{") == 1
        assert delegate._peek_delegation_version(_canonical({"unexpected": True})) == 1

        assert delegate.v2_child_handler_name() == "agent.run_v2"
        delegate.configure_v2_child_handler("agent.run_v9")
        try:
            assert delegate.v2_child_handler_name() == "agent.run_v9"
        finally:
            delegate.reset_v2_child_handler()
        assert delegate.v2_child_handler_name() == "agent.run_v2"
        with pytest.raises(delegate.DelegationError):
            delegate.configure_v2_child_handler("NOT A HANDLER")

    def test_v1_freeze_is_untouched_by_the_v2_surface(self) -> None:
        assert delegate.DELEGATION_PAYLOAD_VERSION == 1
        assert delegate.MAX_CHILDREN_PER_RUN == 8
        assert delegate.MAX_DELEGATION_DEPTH == 4
        assert "server_ids" not in delegate.ChildSpec.__dataclass_fields__
        run_id = _run_id("conv")
        intent = delegate.parse_delegation_intent(
            _canonical(
                {
                    "v": 1,
                    "run": _run_document(run_id),
                    "children": [
                        {"task": "legacy", "capabilities": [READ_CAP]},
                    ],
                }
            )
        )
        # a v1 parent's children still select the legacy handler
        assert delegate._peek_delegation_version(
            _canonical(
                {
                    "v": 1,
                    "run": _run_document(run_id),
                    "children": [{"task": "t"}],
                }
            )
        ) == 1


class TestChildResolution:
    def test_records_are_value_free_and_reversible(self) -> None:
        receipt = {"run_id": "r1", "state": "succeeded", "rounds": 2}
        resolution = delegate.ChildResolution(
            run_id="r1", state="succeeded", receipt=receipt
        )
        record = resolution.to_record()
        assert delegate.ChildResolution.from_record(record) == resolution

    def test_failed_children_carry_no_receipt(self) -> None:
        failed = delegate.ChildResolution.from_record(
            {"run_id": "r2", "state": "failed"}
        )
        assert failed.state == "failed"
        assert failed.receipt is None
        cancelled = delegate.ChildResolution.from_record(
            {"run_id": "r3", "state": "cancelled", "receipt": None}
        )
        assert cancelled.receipt is None

    def test_malformed_records_reject(self) -> None:
        for bad in ({"state": "succeeded"}, {"run_id": "r"}, {"run_id": "r", "state": "succeeded", "receipt": "prose"}):
            with pytest.raises(delegate.DelegationError):
                delegate.ChildResolution.from_record(bad)


# ---------------------------------------------------------------------------
# pure: v2 child planning (AC3: intersection/narrowing, never additive)
# ---------------------------------------------------------------------------


class TestPlanChildrenV2:
    def test_narrowing_plan_intersects_grants_and_pins_initiating_server(self) -> None:
        intent, row, ids, scope = _v2_intent(
            children=[
                _v2_child_spec(
                    "Investigate aspect-alpha.",
                    (BETA_ID, ALPHA_ID),
                    capabilities=(READ_CAP, WRITE_CAP),
                    budget={"max_steps": 10, "max_total_tokens": 2_000},
                )
            ],
            root_budget=BUDGET_DOC,
        )
        plan = delegate.plan_children_v2(
            intent, row, ids, scope_server_ids=scope,
            remaining_steps=20, remaining_tokens=5_000,
        )
        (child,) = plan.children
        assert child.run_id == ids[0]
        # effective grants are the parent∩request intersection, parent order
        assert child.grants == (READ_CAP, WRITE_CAP)
        # the FIRST spec server is the child's initiating/context server
        assert child.initiating_server_id == uuid.UUID(BETA_ID)
        payload = delegate.parse_v2_child_payload(child.payload_json)
        assert payload.run.context.server_id == uuid.UUID(BETA_ID)
        assert payload.run.context.parent_scope is not None
        assert payload.run.context.parent_scope.server_id == uuid.UUID(ALPHA_ID)
        assert payload.run.context.allowed_capabilities == (READ_CAP, WRITE_CAP)
        assert payload.server_ids == (BETA_ID, ALPHA_ID)
        assert payload.budget is not None
        assert payload.budget.max_steps == 10

    def test_out_of_scope_server_rejects_the_whole_child(self) -> None:
        for server_ids in ((GAMMA_ID,), (ALPHA_ID, GAMMA_ID)):
            intent, row, ids, scope = _v2_intent(
                children=[_v2_child_spec("t", server_ids)]
            )
            with pytest.raises(delegate.DelegationError) as error:
                delegate.plan_children_v2(
                    intent, row, ids, scope_server_ids=scope,
                    remaining_steps=None, remaining_tokens=None,
                )
            assert error.value.code is delegate.DelegationErrorCode.SCOPE_VIOLATION

    def test_ungranted_capability_request_rejects_as_expansion(self) -> None:
        intent, row, ids, scope = _v2_intent(
            children=[
                _v2_child_spec("t", (ALPHA_ID,), capabilities=(READ_CAP, "fixture.admin"))
            ],
            allowed=(READ_CAP, WRITE_CAP),
        )
        with pytest.raises(delegate.DelegationError) as error:
            delegate.plan_children_v2(
                intent, row, ids, scope_server_ids=scope,
                remaining_steps=None, remaining_tokens=None,
            )
        assert error.value.code is delegate.DelegationErrorCode.CAPABILITY_EXPANSION

    def test_budget_bounds_beyond_remaining_reject(self) -> None:
        for bounds, remaining in (
            ({"max_steps": 21, "max_total_tokens": 1_000}, (20, 5_000)),
            ({"max_steps": 1, "max_total_tokens": 5_001}, (20, 5_000)),
            ({"max_steps": 1, "max_total_tokens": 1}, (None, None)),
        ):
            intent, row, ids, scope = _v2_intent(
                children=[_v2_child_spec("t", (ALPHA_ID,), budget=bounds)],
                root_budget=BUDGET_DOC,
            )
            with pytest.raises(delegate.DelegationError) as error:
                delegate.plan_children_v2(
                    intent, row, ids, scope_server_ids=scope,
                    remaining_steps=remaining[0], remaining_tokens=remaining[1],
                )
            assert error.value.code is delegate.DelegationErrorCode.BUDGET_EXPANSION

    def test_depth_and_fanout_stay_the_frozen_gates(self) -> None:
        intent, row, ids, scope = _v2_intent(depth=delegate.MAX_DELEGATION_DEPTH)
        with pytest.raises(delegate.DelegationError) as error:
            delegate.plan_children_v2(
                intent, row, ids, scope_server_ids=scope,
                remaining_steps=None, remaining_tokens=None,
            )
        assert error.value.code is delegate.DelegationErrorCode.DELEGATION_DEPTH

        narrowed = dict(BUDGET_DOC, max_children=1)
        intent, row, ids, scope = _v2_intent(
            children=[
                _v2_child_spec("t1", (ALPHA_ID,)),
                _v2_child_spec("t2", (BETA_ID,)),
            ],
            root_budget=narrowed,
        )
        with pytest.raises(BudgetError) as error:
            delegate.plan_children_v2(
                intent, row, ids, scope_server_ids=scope,
                remaining_steps=20, remaining_tokens=5_000,
            )
        assert error.value.code is BudgetErrorCode.FANOUT_EXHAUSTED

    def test_empty_scope_rejects(self) -> None:
        intent, row, ids, _ = _v2_intent()
        with pytest.raises(delegate.DelegationError) as error:
            delegate.plan_children_v2(
                intent, row, ids, scope_server_ids=frozenset(),
                remaining_steps=None, remaining_tokens=None,
            )
        assert error.value.code is delegate.DelegationErrorCode.SCOPE_VIOLATION


# ---------------------------------------------------------------------------
# live fixtures: real disposable Postgres + real dispatcher + real engine
# ---------------------------------------------------------------------------


def _alembic_config() -> Config:
    return Config(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state (engine schema too)."""
    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS notification_alerts, observation_findings, "
            "server_observations, agent_task_completions, agent_task_revisions, "
            "agent_task_evidence, agent_task_criteria, agent_task_constraints, "
            "agent_task_steps, agent_tasks, knowledge_doc_chunks, "
            "knowledge_documents, conversation_steering, conversation_runs, "
            "conversation_messages, conversations, application_projection_journal, "
            "application_projection, objective_journal, objective_checklists, "
            "objective_events, objective_runs, objective_servers, objectives, "
            "job_resource_locks, job_schedule_fires, job_schedules, job_events, "
            "job_steps, job_tags, job_runs, approvals, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, alembic_version, conversation_tags, tags"
        )
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.commit()


@pytest.fixture(scope="session")
def policy_url(request):
    """Disposable fixture database: Alembic chain + engine-owned schema."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the policy-delegation claims need a "
            "real disposable Postgres fixture"
        )
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
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

    # Session teardown: reset the shared fixture database to PRE-bootstrap
    # state so a downstream suite's narrower drop list (budgets, intake)
    # never trips over tables this suite's upgrade created.
    def _reset() -> None:
        try:
            _drop_app_tables(url)
        except Exception:
            pass

    request.addfinalizer(_reset)
    return url


@pytest.fixture
def factory(policy_url: str) -> async_sessionmaker[AsyncSession]:
    """NullPool session factory for in-process facade assertions."""
    eng, sessions = create_app_engine(policy_url)

    async def _clean() -> None:
        async with sessions() as session:
            for table in (
                "job_resource_locks",
                "objective_runs",
                "objective_events",
                "objective_journal",
                "objective_checklists",
                "objectives",
                "job_schedule_fires",
                "job_steps",
                "job_events",
                "job_schedules",
                "job_tags",
                "job_runs",
                "approvals",
                "notification_alerts",
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
    """Insert one root ``job_runs`` row (the seeded run, by SQL)."""

    async def _insert(
        run_id: str,
        *,
        server_id: str = ALPHA_ID,
        root_budget: dict[str, object] | None = None,
        depth: int = 0,
        allowed: tuple[str, ...] = (READ_CAP, WRITE_CAP),
    ) -> None:
        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO job_runs
                        (run_id, server_id, depth, profile_revision,
                         prompt_revision, model_profile, requested_grants,
                         root_budget, submitted_at, updated_at)
                    VALUES
                        (:run_id, :server_id, :depth, 3, 7, 'fixture-fast',
                         :grants, :root_budget, now(), now())
                    """
                ),
                {
                    "run_id": uuid.UUID(run_id),
                    "server_id": uuid.UUID(server_id),
                    "depth": depth,
                    "grants": json.dumps(list(allowed)),
                    "root_budget": (
                        json.dumps(root_budget) if root_budget is not None else None
                    ),
                },
            )
            # 0011 mandatory-tag invariant: the seed classifies the row in
            # the SAME transaction, exactly like the real submit writer.
            await session.execute(
                text(
                    """
                    INSERT INTO job_tags (job_id, tag_id, assignment_source)
                    VALUES (:run_id, :tag, :source)
                    """
                ),
                {
                    "run_id": uuid.UUID(run_id),
                    "tag": uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
                    "source": SYSTEM_ASSIGNMENT_SOURCE,
                },
            )
            await session.commit()

    return _insert


@pytest.fixture
def seed_objective(factory: async_sessionmaker[AsyncSession]) -> Callable[..., str]:
    """Create one objective spanning alpha+beta and bind a v2 run to it."""

    def _seed(
        conv_run_id: str,
        *,
        scope: tuple[str, ...] = (ALPHA_ID, BETA_ID),
        initiating: str = ALPHA_ID,
    ) -> str:
        async def _go() -> str:
            detail = await acreate_objective(
                factory,
                server_id=uuid.UUID(ALPHA_ID),
                original_request="Investigate both disposable fixture hosts.",
                scope_server_ids=[uuid.UUID(s) for s in scope],
            )
            await abind_run(
                factory,
                detail.objective_id,
                server_id=uuid.UUID(ALPHA_ID),
                run_id=uuid.UUID(conv_run_id),
                initiating_server_id=uuid.UUID(initiating),
                actor_kind=ActorKind.AGENT,
                handler=V2_HANDLER_NAME,
            )
            return str(detail.objective_id)

        return str(_run(_go()))  # type: ignore[arg-type]

    return _seed


def _journal_path(scenario: Path) -> Path:
    return scenario / "journal.log"


def _journal_lines(scenario: Path) -> list[str]:
    try:
        return _journal_path(scenario).read_text().splitlines()
    except FileNotFoundError:
        return []


def _journal_count(scenario: Path, marker: str) -> int:
    return sum(1 for line in _journal_lines(scenario) if line == marker)


def _open_gate(scenario: Path, name: str) -> None:
    (scenario / "gates" / name).write_text("go", encoding="utf-8")


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


def _read_child_rows(url: str, parent_run_id: str) -> list[dict]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT run_id, server_id, parent_run_id, depth, requested_grants "
            "FROM job_runs WHERE parent_run_id = %s ORDER BY run_id",
            (uuid.UUID(parent_run_id),),
        ).fetchall()


async def _current_state(factory: async_sessionmaker, run_id: str) -> str | None:
    state = await RunLifecycle(factory).current_state(run_id)
    return None if state is None else state.value


def _engine_state(client: EngineClient, run_id: str) -> str | None:
    state = client.run_state(run_id)
    return None if state is None else state.value


def _failure_reasons(url: str, run_id: str) -> list[str]:
    return [
        str(event.data["reason"])
        for event in _read_events(url, run_id)
        if event.type == "agent.failed"
    ]


def _objective_run_rows(url: str, objective_id: str) -> list[dict]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT run_id, parent_run_id, initiating_server_id, handler, "
            "actor_kind FROM objective_runs WHERE objective_id = %s "
            "ORDER BY run_id",
            (uuid.UUID(objective_id),),
        ).fetchall()


def _compose_roots(scenario: Path) -> dict[str, Path]:
    """Model root + data root with TWO scoped server profiles + env file."""
    model_root = scenario / "model-root"
    data_root = scenario / "data-root"
    store = ModelConfigStore(model_root)
    connection: dict[str, object] = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "name": "gateway",
        "base_url": "http://127.0.0.1:9/v1",
        "timeout_seconds": 5,
        "capabilities": {"tool_calls": True},
    }
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
    for server_id in (ALPHA_ID, BETA_ID):
        profile_store = __import__(
            "mai.profiles.files", fromlist=["ProfileFileStore"]
        ).ProfileFileStore(data_root)
        profile_text = (
            f"schema_version: {SERVER_SCHEMA_VERSION}\n"
            f'server_id: "{server_id}"\n'
            'name: "policy-delegation-fixture"\n'
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
        profile_store.write(uuid.UUID(server_id), profile_text, expected_revision=0)
        server_dir = data_root / "servers" / server_id
        server_dir.chmod(0o700)
        prompt_path = server_dir / "prompt.md"
        prompt_path.write_text(PROMPT, encoding="utf-8")
        os.chmod(prompt_path, 0o600)
    (scenario / "fixture.env").write_text(f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8")
    os.chmod(scenario / "fixture.env", 0o600)
    return {"model_root": model_root, "data_root": data_root}


def _seed_grant_config(
    data_root: Path,
    grants: list[dict],
    *,
    denied: list[str] | None = None,
) -> GrantConfigStore:
    store = GrantConfigStore(data_root)
    store.write(
        {
            "policy": {"revision": 1, "denied_capabilities": list(denied or [])},
            "grants": grants,
        },
        expected_revision=0,
    )
    return store


@pytest.fixture
def scenario(tmp_path: Path) -> Path:
    directory = tmp_path / "scenario"
    (directory / "gates").mkdir(parents=True)
    handlers = directory / "handlers"
    handlers.mkdir()
    (handlers / f"{HANDLER_MODULE_NAME}.py").write_text(
        HANDLER_MODULE_SOURCE, encoding="utf-8"
    )
    return directory


@pytest.fixture
def probe(scenario: Path, factory: async_sessionmaker[AsyncSession], policy_url: str):
    """In-process real dispatcher + store + locks + journaled executor."""
    roots = _compose_roots(scenario)
    data_root = roots["data_root"]
    store = _seed_grant_config(
        data_root,
        grants=[
            {"capability_id": READ_CAP, "server_ids": [ALPHA_ID, BETA_ID]},
            {"capability_id": WRITE_CAP, "server_ids": [ALPHA_ID]},
        ],
    )
    registry = CapabilityRegistry()
    registry.register(_descriptor(READ_CAP, "read"))
    registry.register(_descriptor(WRITE_CAP, "mutate"))
    executor = _InProcessExecutor(scenario)
    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(factory),
        grants=lambda: store.read().grants,
        policy=lambda: store.read().policy,
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(str(scenario / "fixture.env")),
        executor=executor,
    )
    locks = ResourceLockService(policy_url)
    locks.ensure_schema()
    return SimpleNamespace(
        scenario=scenario,
        factory=factory,
        store=store,
        registry=registry,
        dispatcher=dispatcher,
        executor=executor,
        locks=locks,
        data_root=data_root,
    )


class _InProcessExecutor:
    """Journaled capability executor: begin → effect, with behaviors."""

    def __init__(self, scenario: Path, behaviors: dict[str, str] | None = None) -> None:
        self._journal = _journal_path(scenario)
        self._gate_dir = scenario / "gates"
        self._behaviors = behaviors or {}

    def _mark(self, line: str) -> None:
        with open(self._journal, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    async def __call__(self, call) -> ExecutorResult:
        capability_id = call.descriptor.identifier
        marker = str(call.payload.get("marker", "?"))
        server = call.context.server_id
        behavior = self._behaviors.get(
            f"{capability_id}:{marker}@{server}",
            self._behaviors.get(f"{capability_id}:{marker}", "ok"),
        )
        self._mark(f"begin:{capability_id}:{marker}@{server}")
        if behavior == "gate":
            gate = self._gate_dir / f"gate-{capability_id}-{marker}"
            deadline = time.time() + 120
            while not gate.exists():
                if time.time() > deadline:
                    raise RuntimeError("gate timeout")
                await asyncio.sleep(0.05)
        if behavior == "slow":
            await asyncio.sleep(0.4)
        self._mark(f"effect:{capability_id}:{marker}@{server}")
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


def _bridge_for(
    probe,
    *,
    server_id: str = ALPHA_ID,
    exposure: ObjectiveExposure | None = None,
    with_locks: bool = False,
) -> "object":
    from mai.agents.tools import CapabilityToolBridge

    run_id = uuid.uuid4()
    grants = probe.store.read().grants
    policy = probe.store.read().policy
    scope = [uuid.UUID(ALPHA_ID), uuid.UUID(BETA_ID)]
    resolved = exposure or project_exposure(
        grants=grants, policy=policy, scope_server_ids=scope
    )
    context = ExecutionContext(
        principal="hash:fixture-principal",
        server_id=uuid.UUID(server_id),
        run_id=run_id,
        parent_scope=None,
        profile_revision="profile-1",
        prompt_revision="prompt-1",
        credential_references=(),
        allowed_capabilities=resolved.capability_ids,
        mode=PolicyMode.DELEGATED,
    )
    return CapabilityToolBridge(
        registry=probe.registry,
        dispatcher=probe.dispatcher,
        context=context,
        exposure=resolved,
        locks=probe.locks if with_locks else None,
    )


# ---------------------------------------------------------------------------
# in-process: exposure adapter + read fan-out + locks (AC1/AC4)
# ---------------------------------------------------------------------------


class TestExposureAdapter:
    def test_tools_name_their_valid_target_sets(self, probe) -> None:
        bridge = _bridge_for(probe)
        assert sorted(bridge.enabled_capabilities) == sorted((READ_CAP, WRITE_CAP))
        read_tool = next(t for t in bridge.tools if t.name == "fixture_read")
        description = read_tool.description
        assert ALPHA_ID in description and BETA_ID in description
        assert "server_id" in description
        write_tool = next(t for t in bridge.tools if t.name == "fixture_write")
        assert ALPHA_ID in write_tool.description
        assert BETA_ID not in write_tool.description

    def test_model_facing_call_pins_the_named_target(self, probe) -> None:
        bridge = _bridge_for(probe)
        # read on beta: allowed, effect journaled against BETA
        result = _run(
            bridge._invoke_model_facing(
                READ_CAP, {"marker": "m-beta", "server_id": BETA_ID}
            )
        )
        document = json.loads(result)
        assert document["status"] == "executed"
        assert document["server_id"] == BETA_ID
        assert (
            _journal_count(
                probe.scenario, f"effect:{READ_CAP}:m-beta@{uuid.UUID(BETA_ID)}"
            )
            == 1
        )
        # write on beta: a hidden target is refused BEFORE any dispatch
        result = _run(
            bridge._invoke_model_facing(
                WRITE_CAP, {"marker": "m-denied", "server_id": BETA_ID}
            )
        )
        document = json.loads(result)
        assert document["status"] == "denied"
        assert document["error"]["code"] == "target_not_in_scope"
        assert not any("m-denied" in line for line in _journal_lines(probe.scenario))
        # a malformed target is refused, never guessed
        result = _run(
            bridge._invoke_model_facing(
                READ_CAP,
                {"marker": "m-bad", "server_id": "4d4d4d4d-0000-0000-0000-000000000004"},
            )
        )
        assert json.loads(result)["error"]["code"] == "target_not_in_scope"

    def test_dispatcher_revalidates_a_forged_multi_target_context(self, probe) -> None:
        """Exposure is a projection: the ONE boundary revalidates targets."""
        context = ExecutionContext(
            principal="hash:fixture-principal",
            server_id=uuid.UUID(BETA_ID),
            run_id=uuid.uuid4(),
            parent_scope=None,
            profile_revision="profile-1",
            prompt_revision="prompt-1",
            credential_references=(),
            allowed_capabilities=(WRITE_CAP,),
            mode=PolicyMode.DELEGATED,
        )
        outcome = _run(
            probe.dispatcher.dispatch(
                context=context, capability_id=WRITE_CAP,
                payload={"marker": "forged"}, approval_id=None,
            )
        )
        assert outcome.allowed is False
        assert outcome.reason == "server_scope_mismatch"
        assert outcome.effect == "not_attempted"
        assert _journal_count(probe.scenario, f"effect:{WRITE_CAP}:forged@{uuid.UUID(BETA_ID)}") == 0


class TestReadFanOut:
    def test_reads_fan_out_concurrently_across_scoped_targets(self, probe) -> None:
        bridge = _bridge_for(probe)
        probe.executor._behaviors[f"{READ_CAP}:slow@{uuid.UUID(ALPHA_ID)}"] = "slow"
        alpha, beta = uuid.UUID(ALPHA_ID), uuid.UUID(BETA_ID)
        outcomes = _run(
            bridge.fan_out_read(READ_CAP, {"marker": "slow"}, targets=[alpha, beta])
        )
        assert [o.server_id for o in outcomes] == [alpha, beta]
        assert all(o.allowed and o.effect == "executed" for o in outcomes)
        lines = _journal_lines(probe.scenario)
        begin_alpha = lines.index(f"begin:{READ_CAP}:slow@{alpha}")
        begin_beta = lines.index(f"begin:{READ_CAP}:slow@{beta}")
        effect_beta = lines.index(f"effect:{READ_CAP}:slow@{beta}")
        effect_alpha = lines.index(f"effect:{READ_CAP}:slow@{alpha}")
        # concurrency: alpha sleeps 400ms, beta does not — beta's WHOLE
        # execution (begin..effect) lands inside alpha's execution window
        assert begin_beta < effect_alpha
        assert effect_beta < effect_alpha

    def test_fan_out_refuses_mutations_and_foreign_targets(self, probe) -> None:
        from mai.agents.tools import BridgeError, BridgeErrorCode

        bridge = _bridge_for(probe)
        with pytest.raises(BridgeError) as error:
            _run(bridge.fan_out_read(WRITE_CAP, {"marker": "x"}))
        assert error.value.code is BridgeErrorCode.READ_FANOUT_UNSUPPORTED
        with pytest.raises(BridgeError) as error:
            _run(
                bridge.fan_out_read(
                    READ_CAP, {"marker": "x"}, targets=[uuid.UUID(GAMMA_ID)]
                )
            )
        assert error.value.code is BridgeErrorCode.TARGET_NOT_IN_SCOPE


class TestMutationSerialization:
    def test_overlapping_mutations_serialize_through_declared_locks(self, probe) -> None:
        from mai.agents.tools import BridgeError, BridgeErrorCode

        bridge = _bridge_for(probe, with_locks=True)
        probe.executor._behaviors[f"{WRITE_CAP}:hold"] = "gate"
        alpha = uuid.UUID(ALPHA_ID)

        async def _scenario() -> None:
            first = asyncio.ensure_future(
                bridge.dispatch_under_lock(
                    WRITE_CAP, {"marker": "hold"}, resource="deploy.lock",
                    owner_ref="first",
                )
            )
            deadline = time.monotonic() + 30
            while (
                f"begin:{WRITE_CAP}:hold@{alpha}" not in _journal_lines(probe.scenario)
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("first mutation never began")
                await asyncio.sleep(0.05)
            with pytest.raises(BridgeError) as error:
                await bridge.dispatch_under_lock(
                    WRITE_CAP, {"marker": "hold2"}, resource="deploy.lock",
                    owner_ref="second",
                )
            assert error.value.code is BridgeErrorCode.LOCK_CONTENTION
            assert "held" in error.value.detail
            _open_gate(probe.scenario, f"gate-{WRITE_CAP}-hold")
            outcome = await first
            assert outcome.allowed and outcome.effect == "executed"

        _run(_scenario())
        # exactly one effect ran; the contending attempt never dispatched
        assert _journal_count(probe.scenario, f"effect:{WRITE_CAP}:hold@{alpha}") == 1
        assert not any("hold2" in line for line in _journal_lines(probe.scenario))
        # the lease was released: the next mutation passes
        outcome = _run(
            bridge.dispatch_under_lock(
                WRITE_CAP, {"marker": "after"}, resource="deploy.lock"
            )
        )
        assert outcome.allowed

    def test_fenced_or_foreign_locks_fail_closed_without_effect(self, probe) -> None:
        from mai.agents.tools import BridgeError, BridgeErrorCode

        bridge = _bridge_for(probe, with_locks=True)
        alpha = uuid.UUID(ALPHA_ID)
        lease = probe.locks.acquire(str(alpha), "other.lock", owner_ref="external")
        try:
            with pytest.raises(BridgeError) as error:
                _run(
                    bridge.dispatch_under_lock(
                        WRITE_CAP, {"marker": "blocked"}, resource="other.lock"
                    )
                )
            assert error.value.code is BridgeErrorCode.LOCK_CONTENTION
        finally:
            probe.locks.release(lease)
        assert _journal_count(probe.scenario, f"effect:{WRITE_CAP}:blocked@{alpha}") == 0
        outcome = _run(
            bridge.dispatch_under_lock(
                WRITE_CAP, {"marker": "blocked"}, resource="other.lock"
            )
        )
        assert outcome.allowed


# ---------------------------------------------------------------------------
# in-process: research_only denial journaled with no effect (AC2)
# ---------------------------------------------------------------------------


class TestResearchOnlyDenial:
    def test_direct_mutation_denied_journaled_without_effect(self, probe) -> None:
        beta = uuid.UUID(BETA_ID)
        # 1) template projection with CAS onto the LIVE store
        stored, projection = apply_research_only(
            store=probe.store,
            registry=probe.registry,
            server_ids=[beta],
            expected_revision=1,
        )
        assert stored.revision == 2
        assert set(stored.grants[WRITE_CAP]) == {uuid.UUID(ALPHA_ID)}
        # 2) exposure after the template: write is invisible on beta
        exposure = project_exposure(
            grants=probe.store.read().grants,
            policy=probe.store.read().policy,
            scope_server_ids=[beta],
        )
        assert exposure.capability_ids == (READ_CAP,)
        # 3) a DIRECT dispatch of the mutation at beta is denied at the ONE
        #    dispatcher (stale grant references revalidated), no effect.
        context = ExecutionContext(
            principal="hash:fixture-principal",
            server_id=beta,
            run_id=uuid.uuid4(),
            parent_scope=None,
            profile_revision="profile-1",
            prompt_revision="prompt-1",
            credential_references=(),
            allowed_capabilities=(WRITE_CAP,),
            mode=PolicyMode.DELEGATED,
        )
        outcome = _run(
            probe.dispatcher.dispatch(
                context=context, capability_id=WRITE_CAP,
                payload={"marker": "direct-write"}, approval_id=None,
            )
        )
        assert outcome.allowed is False
        assert outcome.reason == "server_scope_mismatch"
        assert outcome.effect == "not_attempted"
        assert outcome.policy_revision == 2
        assert _journal_count(probe.scenario, f"effect:{WRITE_CAP}:direct-write@{beta}") == 0
        # 4) the sanitized decision lands VISIBLY in the objective ledger.
        objective_id = _run(
            acreate_objective(
                probe.factory,
                server_id=beta,
                original_request="Research-only visibility check.",
                scope_server_ids=[beta, uuid.UUID(ALPHA_ID)],
            )
        ).objective_id
        event = _run(
            aappend_event(
                probe.factory,
                objective_id,
                server_id=beta,
                draft=ObjectiveEventDraft(
                    type="objective.journal_updated",
                    actor_kind=ActorKind.AGENT,
                    run_id=context.run_id,
                    data={"decision": outcome.to_json()},
                ),
            )
        )
        page = _run(
            alist_events(
                probe.factory,
                objective_id,
                server_id=beta,
                type="objective.journal_updated",
            )
        )
        assert page.total == 1
        (recorded,) = page.items
        decision = recorded.data["decision"]
        assert decision["allowed"] is False
        assert decision["reason"] == "server_scope_mismatch"
        assert decision["effect"] == "not_attempted"
        assert decision["op_id"] == str(outcome.op_id)
        # sanitized: the denied call's payload prose appears nowhere
        assert "direct-write" not in json.dumps(recorded.data)
        # 5) replaying the stale revision is rejected by the CAS
        with pytest.raises(TemplateError) as error:
            apply_research_only(
                store=probe.store,
                registry=probe.registry,
                server_ids=[beta],
                expected_revision=1,
            )
        assert error.value.code is TemplateErrorCode.STALE_REVISION


# ---------------------------------------------------------------------------
# live: the durable v2 delegation tree (real engine, real workers)
# ---------------------------------------------------------------------------


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
        [sys.executable, "-m", "mai.jobs.worker", "--concurrency", "3"],
        cwd=str(BACKEND_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _stop(proc: subprocess.Popen) -> int:
    proc.send_signal(signal.SIGTERM)
    try:
        return proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=30)


class TestScopedDelegationTree:
    def test_v2_parent_fans_out_children_and_aggregates_receipts(
        self, policy_url, factory, insert_run, seed_objective, scenario
    ) -> None:
        """AC3/AC4/AC5: scoped children over two targets; honest receipts."""
        _compose_roots(scenario)
        _seed_grant_config(
            scenario / "data-root",
            grants=[
                {"capability_id": READ_CAP, "server_ids": [ALPHA_ID, BETA_ID]},
                {"capability_id": WRITE_CAP, "server_ids": [ALPHA_ID]},
            ],
        )
        (scenario / "model-script.json").write_text(
            json.dumps(
                {
                    "alpha": [
                        {"tool_call": {"name": "fixture_read", "args": {"marker": "m1"}, "id": "c1"}},
                        {"text": "alpha investigation done"},
                    ],
                    "beta": [
                        {"tool_call": {"name": "fixture_read", "args": {"marker": "m2"}, "id": "c2"}},
                        {"text": "beta investigation done"},
                    ],
                    "gamma": [{"raise": "http401"}],
                }
            ),
            encoding="utf-8",
        )
        conv_id = _run_id("conv")
        parent_id = _run_id("deleg")
        _run(insert_run(conv_id, server_id=ALPHA_ID))
        objective_id = seed_objective(conv_id)
        document = _v2_delegation_document(
            conv_id,
            [
                _v2_child_spec("Investigate aspect-alpha.", (ALPHA_ID,)),
                _v2_child_spec("Investigate aspect-beta.", (BETA_ID,)),
                _v2_child_spec("Investigate aspect-gamma.", (ALPHA_ID,)),
            ],
        )
        client = EngineClient(policy_url)
        worker = None
        try:
            worker = _spawn_worker(policy_url, scenario / "handlers", scenario, "w1")
            _run(insert_run(parent_id, server_id=ALPHA_ID))
            client.enqueue_run(parent_id, delegate.HANDLER_NAME, _canonical(document))
            _wait_until(
                lambda: (_engine_state(client, parent_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "the v2 parent's success",
            )
            # AC5: receipts aggregated; the failed child stays honestly failed
            summary = client.await_run(parent_id)
            assert summary["state"] == "succeeded"
            assert summary["succeeded"] == 2 and summary["failed"] == 1
            assert summary["cancelled"] == 0
            # AC3: children ran with THEIR initiating servers and the
            # INTERSECTED grants; scope stayed inside the objective.
            child_rows = {
                str(row["run_id"]): row for row in _read_child_rows(policy_url, conv_id)
            }
            assert len(child_rows) == 3
            beta_rows = [
                rid
                for rid, row in child_rows.items()
                if str(row["server_id"]) == BETA_ID
            ]
            assert len(beta_rows) == 1
            # psycopg returns the JSONB array parsed
            assert child_rows[beta_rows[0]]["requested_grants"] == [READ_CAP]
            assert all(int(row["depth"]) == 1 for row in child_rows.values())
            by_state = {c["run_id"]: c for c in summary["children"]}
            assert set(by_state) == set(child_rows)
            # the beta child succeeded with ITS OWN receipt document
            assert by_state[beta_rows[0]]["state"] == "succeeded"
            assert by_state[beta_rows[0]]["receipt"]["run_id"] == beta_rows[0]
            # the aspect-gamma child failed honestly, with NO receipt
            failed = [
                rid for rid, c in by_state.items() if c["state"] == "failed"
            ]
            assert len(failed) == 1
            assert "receipt" not in by_state[failed[0]]
            succeeded = [
                rid for rid, c in by_state.items() if c["state"] == "succeeded"
            ]
            assert len(succeeded) == 2
            # children are objective-bound v2 runs under the bound parent
            bindings = {
                str(row["run_id"]): row
                for row in _objective_run_rows(policy_url, objective_id)
            }
            assert set(bindings) == {conv_id, *child_rows}
            assert all(
                bindings[rid]["handler"] == V2_HANDLER_NAME for rid in child_rows
            )
            assert str(bindings[beta_rows[0]]["parent_run_id"]) == conv_id
            assert (
                str(bindings[beta_rows[0]]["initiating_server_id"]) == BETA_ID
            )
            # effects: reads hit BOTH targets; the failed child never began
            alpha, beta = uuid.UUID(ALPHA_ID), uuid.UUID(BETA_ID)
            assert _journal_count(scenario, f"effect:{READ_CAP}:m1@{alpha}") == 1
            assert _journal_count(scenario, f"effect:{READ_CAP}:m2@{beta}") == 1
            # exactly the two successful reads produced effects
            assert (
                sum(
                    1
                    for line in _journal_lines(scenario)
                    if line.startswith("effect:")
                )
                == 2
            )
            # the completion event carries the receipt-level resolutions
            # (the parent's events ride the BOUND conversation run stream)
            completed = [
                e for e in _read_events(policy_url, conv_id)
                if e.type == delegate.DELEGATION_COMPLETED_EVENT_TYPE
            ]
            assert completed, "parent completion event missing"
            children_data = completed[-1].data["children"]
            receipts = [c for c in children_data if c.get("receipt")]
            assert len(receipts) == 2
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

    def test_budget_exhaustion_cancel_refund_and_recovery(
        self, policy_url, factory, insert_run, seed_objective, scenario
    ) -> None:
        """AC3/AC5: a batch that can never fit the active budget is
        refused WHOLE (classified, nothing starts); a cancelled
        delegation refunds its reservations to the fold; a fresh tree
        runs honestly afterwards."""
        from mai.jobs.recovery import RecoveryService

        _compose_roots(scenario)
        _seed_grant_config(
            scenario / "data-root",
            grants=[
                {"capability_id": READ_CAP, "server_ids": [ALPHA_ID, BETA_ID]},
                {"capability_id": WRITE_CAP, "server_ids": [ALPHA_ID]},
            ],
        )
        (scenario / "model-script.json").write_text(
            json.dumps(
                {
                    "default": [
                        {"tool_call": {"name": "fixture_read", "args": {"marker": "hold"}, "id": "c1"}},
                        {"text": "investigation done"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        (scenario / "behavior.json").write_text(
            json.dumps({f"{READ_CAP}:hold": "gate"}),
            encoding="utf-8",
        )
        exhausted_budget = dict(BUDGET_DOC, max_children=4, max_active=2)
        tree_budget = dict(BUDGET_DOC, max_children=2, max_active=2)
        alpha, beta = uuid.UUID(ALPHA_ID), uuid.UUID(BETA_ID)
        client = EngineClient(policy_url)
        worker = None
        try:
            worker = _spawn_worker(policy_url, scenario / "handlers", scenario, "w1")

            def _seed_tree(tag: str, budget: dict[str, object]) -> tuple[str, str]:
                conv_id = _run_id(f"conv-{tag}")
                carrier_id = _run_id(f"carrier-{tag}")
                _run(insert_run(conv_id, server_id=ALPHA_ID, root_budget=budget))
                _run(insert_run(carrier_id, server_id=ALPHA_ID, root_budget=budget))
                objective_id = seed_objective(conv_id)
                return conv_id, carrier_id

            # 1) exhaustion: a 3-child delegation can NEVER fit the root's
            #    active budget (max_active=2) -> classified whole refusal.
            conv1, carrier1 = _seed_tree("exh", exhausted_budget)
            client.enqueue_run(
                carrier1,
                delegate.HANDLER_NAME,
                _canonical(
                    _v2_delegation_document(
                        conv1,
                        [
                            _v2_child_spec("Investigate aspect-one.", (ALPHA_ID,)),
                            _v2_child_spec("Investigate aspect-two.", (BETA_ID,)),
                            _v2_child_spec("Investigate aspect-three.", (ALPHA_ID,)),
                        ],
                    )
                ),
            )
            _wait_until(
                lambda: (_engine_state(client, carrier1) or "") == "failed",
                RUN_TIMEOUT,
                "the never-fits delegation's classified refusal",
            )
            assert _failure_reasons(policy_url, conv1) == [
                "budget_child_cap_exceeds_budget"
            ]
            # the whole batch was planned but NOTHING ever started
            planned = _read_child_rows(policy_url, conv1)
            assert len(planned) == 3
            assert all(
                _engine_state(client, str(row["run_id"])) is None
                for row in planned
            )
            assert not _journal_lines(scenario)
            state = _run(BudgetLedger(factory).asnapshot(conv1))
            assert state.active_reserved == 0

            # 2) cancel/refund: two gated children reserve BOTH slots; the
            #    cancelled tree refunds them (the fold returns to zero).
            conv2, carrier2 = _seed_tree("tree", tree_budget)
            client.enqueue_run(
                carrier2,
                delegate.HANDLER_NAME,
                _canonical(
                    _v2_delegation_document(
                        conv2,
                        [
                            _v2_child_spec("Investigate aspect-one.", (ALPHA_ID,)),
                            _v2_child_spec("Investigate aspect-two.", (BETA_ID,)),
                        ],
                    )
                ),
            )
            _wait_until(
                lambda: _journal_count(
                    scenario, f"begin:{READ_CAP}:hold@{alpha}"
                ) == 1
                and _journal_count(scenario, f"begin:{READ_CAP}:hold@{beta}") == 1,
                RUN_TIMEOUT,
                "both gated children reserving the tree's active budget",
            )
            state = _run(BudgetLedger(factory).asnapshot(conv2))
            assert state.active_reserved == 2
            recovery = RecoveryService(policy_url, factory)
            result = _run(
                recovery.acancel_run(carrier2, cancel_children=True, outcome_timeout=60.0)
            )
            assert result.outcome.value == "cancelled"
            _open_gate(scenario, f"gate-{READ_CAP}-hold")
            _wait_until(
                lambda: (_engine_state(client, carrier2) or "") == "cancelled",
                RUN_TIMEOUT,
                "the cancelled parent's facade translation",
            )
            _wait_until(
                lambda: _run(BudgetLedger(factory).asnapshot(conv2)).active_reserved
                == 0,
                60.0,
                "the cancellation refund freeing both slots",
            )
            released = [
                e for e in _read_events(policy_url, conv2)
                if e.type == "agent.budget_released"
            ]
            assert released, "the cancellation refund never reached the ledger"

            # 3) recovery: a fresh tree under the same budget document runs
            #    to completion with aggregated receipts.
            conv3, carrier3 = _seed_tree("after", tree_budget)
            client.enqueue_run(
                carrier3,
                delegate.HANDLER_NAME,
                _canonical(
                    _v2_delegation_document(
                        conv3,
                        [_v2_child_spec("Investigate aspect-after.", (ALPHA_ID,))],
                    )
                ),
            )
            _wait_until(
                lambda: (_engine_state(client, carrier3) or "") == "succeeded",
                RUN_TIMEOUT,
                "the post-cancel fresh tree's success",
            )
            summary = client.await_run(carrier3)
            assert summary["succeeded"] == 1 and summary["failed"] == 0
            (child_record,) = summary["children"]
            assert child_record["state"] == "succeeded"
            assert child_record["receipt"]["run_id"] == child_record["run_id"]
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

    def test_out_of_scope_child_expansion_fails_the_parent(
        self, policy_url, factory, insert_run, seed_objective, scenario
    ) -> None:
        """AC3: naming a server outside the objective scope fails closed."""
        _compose_roots(scenario)
        _seed_grant_config(
            scenario / "data-root",
            grants=[
                {"capability_id": READ_CAP, "server_ids": [ALPHA_ID, BETA_ID]},
            ],
        )
        conv_id = _run_id("conv")
        d4 = _run_id("d4")
        _run(insert_run(conv_id, server_id=ALPHA_ID))
        objective_id = seed_objective(conv_id, scope=(ALPHA_ID, BETA_ID))
        client = EngineClient(policy_url)
        worker = None
        try:
            worker = _spawn_worker(policy_url, scenario / "handlers", scenario, "w1")
            _run(insert_run(d4, server_id=ALPHA_ID))
            # gamma is granted+in-scope NOWHERE: expansion attempt
            client.enqueue_run(
                d4,
                delegate.HANDLER_NAME,
                _canonical(
                    _v2_delegation_document(
                        conv_id,
                        [_v2_child_spec("Investigate aspect-x.", (GAMMA_ID,))],
                    )
                ),
            )
            _wait_until(
                lambda: (_engine_state(client, d4) or "") == "failed",
                RUN_TIMEOUT,
                "the scope-expansion delegation's failure",
            )
            # the plan rejected WHOLE: no child row was ever persisted
            assert _read_child_rows(policy_url, conv_id) == []
            # the facade translation lands on the BOUND conversation run
            # (intent.run.run_id) and the engine carrier both failed — the
            # DelegationError path records the transition (no reason event,
            # the v1-frozen translation discipline)
            assert _failure_reasons(policy_url, conv_id) == []
            assert _run(_current_state(factory, conv_id)) == "failed"
            assert (_engine_state(client, d4) or "") == "failed"
        finally:
            if worker is not None:
                _stop(worker)
            client.close()


# ---------------------------------------------------------------------------
# the worker-side composition (written into the scenario handler dir)
# ---------------------------------------------------------------------------

HANDLER_MODULE_SOURCE = '''
"""Worker-side composition for the policy-delegation v2 card: real
dispatcher with GrantConfigStore-backed providers, journaled effects,
aspect-scripted FunctionModel, agent.run_v2 child handler unwrapping the
ONE delegation module's v2 child payload."""

import asyncio
import json
import os
import re
import time

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import mai.agents.delegate as delegate
import mai.agents.run as agent_run
from mai.agents.budgets import BudgetLedger
from mai.agents.model_store import ModelConfigStore
from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
from mai.credentials.env import EnvResolver
from mai.jobs import engine
from mai.jobs.engine import EngineClient
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.config import GrantConfigStore
from mai.policy.evaluate import Policy


ALPHA = "1a0f7d2e-5c3b-4a48-9e21-6f8d0a2b4c01"
BETA = "2b1e8c3f-6d4c-4b59-8f32-7a9e1b3c5d02"
READ_CAP = "fixture.read"
WRITE_CAP = "fixture.write"


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
    """Journaled capability executor with server-stamped markers."""

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
        server = call.context.server_id
        behavior = self._behavior(capability_id, str(marker))
        self._mark(f"begin:{capability_id}:{marker}@{server}")
        if behavior == "gate":
            gate = os.path.join(self.gate_dir, f"gate-{capability_id}-{marker}")
            deadline = time.time() + 300
            while not os.path.exists(gate):
                if time.time() > deadline:
                    raise RuntimeError("gate timeout")
                # non-blocking: the dispatch runs on the executor's ONE
                # shared loop; a blocking sleep would starve sibling calls
                await asyncio.sleep(0.1)
        self._mark(f"effect:{capability_id}:{marker}@{server}")
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

    def _entry_payload(entry):
        if entry.get("raise") == "http401":
            raise ModelHTTPError(
                status_code=401, body=b"denied", model_name="fixture-model"
            )
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

    def _aspect_of(messages):
        for message in messages:
            if getattr(message, "kind", "") != "request":
                continue
            for part in message.parts:
                if getattr(part, "part_kind", "") == "user-prompt":
                    found = re.search(r"aspect-([a-z]+)", str(part.content))
                    if found:
                        return found.group(1)
        return None

    def _model_fn(messages, info):
        index = (len(messages) - 1) // 2
        entries = script.get(_aspect_of(messages)) or script["default"]
        if index >= len(entries):
            raise AssertionError("model script exhausted")
        return _entry_payload(entries[index])

    async def _model_stream_fn(messages, info):
        # The production run path is a STREAMED model round (run.py always
        # supplies an event_stream_handler), so the fake speaks provider
        # deltas: same scripted decision, streaming transport.
        from pydantic_ai.models.function import DeltaToolCall

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
    store = GrantConfigStore(os.environ["MAI_FX_DATA_ROOT"])
    registry = CapabilityRegistry()
    registry.register(_descriptor(READ_CAP, "read"))
    registry.register(_descriptor(WRITE_CAP, "mutate"))
    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(sessions),
        grants=lambda: store.read().grants,
        policy=lambda: store.read().policy,
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(os.environ["MAI_FX_ENV"]),
        executor=FixtureExecutor(),
    )
    model_store = ModelConfigStore(os.environ["MAI_FX_MODEL_ROOT"])
    scripted = os.environ.get("MAI_FX_MODEL_SCRIPT")
    fake_model = _script_model() if scripted else None
    model_builder = (lambda **kwargs: fake_model) if scripted else None
    executor = agent_run.AgentRunExecutor(
        sessions=sessions,
        dispatcher=dispatcher,
        registry=registry,
        model_store=model_store,
        data_root=os.environ["MAI_FX_DATA_ROOT"],
        locks=ResourceLockService(url),
        resolver=EnvResolver(os.environ["MAI_FX_ENV"]),
        model_builder=model_builder,
    )
    agent_run.configure_agent_run(executor)
    delegate.configure_delegation(
        delegate.DelegationExecutor(
            sessions=sessions,
            executor=executor,
            client=EngineClient(url),
            ledger=BudgetLedger(sessions),
        )
    )
    delegate.configure_v2_child_handler(
        os.environ.get("MAI_FX_V2_CHILD_HANDLER", "agent.run_v2")
    )

    async def _run_v2_child(payload_json):
        # the ONE parser: the v2 child payload is the delegation module's
        delegate.parse_v2_child_payload(payload_json)
        inner = json.loads(payload_json)["run"]
        return await agent_run.run_agent_conversation(
            agent_run.canonical_json(inner)
        )

    engine.register_job_handler(delegate.V2_CHILD_HANDLER_NAME, _run_v2_child)


_build()
'''
