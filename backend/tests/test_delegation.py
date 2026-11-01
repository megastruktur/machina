"""Focused tests for delegated child runs (card 06-child-runs).

Acceptance (BRIEF):

1. A child persists the parent/server/context linkage (``job_runs.
   ````parent_run_id``/``depth`` + the context's ``parent_scope``) and its
   grants are the INTERSECTION of the parent's granted set with the child's
   explicit request; the yielding parent parks ``waiting_children`` and its
   wait never deadlocks worker capacity (children are engine children, not
   queue entries — proven against a saturated queue).
2. Children cannot reassign the server (the request vocabulary has no
   target field; the parent's server binding is pinned) and cannot amplify
   tools (a request outside the parent's grants is clamped, and the
   dispatcher still denies at the effect boundary); the terminal child
   result wakes the parent exactly through the domain contract (frozen
   ``waiting_children → running`` edge + the safe resolved event) and
   cancellation propagates to the tree (native engine child edges and the
   handler's belt).

Boundary discipline (test policy): the durable-tree claims run against a
real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``) with the
real Alembic chain, the real engine adapter and REAL worker subprocesses
(``kill -9`` mid-wait + fresh-worker recovery) — the same discipline as
``test_agent_run.py``. The dispatcher composition is the REAL one (real
registry, real policy evaluation, real approval store over the fixture
database); the capability executor is a journaled fixture whose effect
markers make "the effect happened exactly once" and "the child got ITS
task" observable. The model is a deterministic FunctionModel fake (C06
rule 6: fake LLMs prove MAI plumbing only); the model-requested
mid-conversation delegation assembly is 06-verify's scenario (recorded
limitation). Fixtures carry only tmp-namespace paths and marker words —
never secret values.
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

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents import delegate
from mai.agents import run as agent_run
from mai.agents.budgets import (
    BUDGET_CHARGED_EVENT_TYPE,
    BUDGET_PAYLOAD_VERSION,
    BUDGET_RELEASED_EVENT_TYPE,
    BUDGET_RESERVED_EVENT_TYPE,
    BudgetError,
    BudgetErrorCode,
    BudgetLedger,
)
from mai.agents.context import MAX_ROLE_CHARS
from mai.agents.model_store import ModelConfigStore
from mai.agents.models import SCHEMA_VERSION as MODEL_SCHEMA_VERSION
from mai.contracts.jobs import RunState, parse_job_event
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
from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID, SYSTEM_ASSIGNMENT_SOURCE

BACKEND_ROOT = Path(__file__).resolve().parents[1]

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Scratch namespace per runtime-environment ADR D2/D4 (recorded + cleaned
# in the card evidence). Each live scenario gets a fresh subdirectory.
FIXTURE_ROOT = Path("/tmp/mai-fxt-06-child-runs")

RUN_TIMEOUT = 150.0

# Frozen fixture identities (canonical lowercase UUID strings, C01 rule 1).
SERVER_ID = "7c2d4f6a-8b1e-4a3c-9d5f-2e4b6a8c0d1e"

READ_CAP = "fixture.read"
PRIV_CAP = "fixture.priv"

HANDLER_MODULE_NAME = "delegation_scenario_handlers"

SECRET_NAME = "MAI_FXT_TOKEN"
SECRET_VALUE = "fxt-synthetic-token-value-06child"

HOST_KEY = "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"

PROMPT = "You administer the delegation fixture host. Keep changes announced.\n"
ROLE = "operator assistant"
TASK = "Delegate the fixture investigations and report the outcomes."


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
# pure layer fixtures: payload documents
# ---------------------------------------------------------------------------


def _context_doc(
    run_id: str,
    *,
    mode: str = "delegated",
    allowed: tuple[str, ...] = (READ_CAP,),
    parent_scope: dict[str, str] | None = None,
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
    document = context.to_json()
    if parent_scope is not None:
        document["parent_scope"] = parent_scope
    return document


def _child_spec(
    task: str, *, capabilities: tuple[str, ...] | None = (READ_CAP,), **overrides: object
) -> dict:
    spec: dict[str, object] = {"task": task}
    if capabilities is not None:
        spec["capabilities"] = list(capabilities)
    spec.update(overrides)
    return spec


def _delegation_document(
    run_id: str,
    children: list[dict] | None = None,
    *,
    allowed: tuple[str, ...] | None = None,
    **overrides: object,
) -> dict:
    document: dict[str, object] = {
        "v": delegate.DELEGATION_PAYLOAD_VERSION,
        "run": agent_run_document(run_id, allowed=allowed),
        "children": children
        if children is not None
        else [_child_spec("Investigate aspect-alpha on the fixture host.")],
    }
    document.update(overrides)
    return document


def agent_run_document(
    run_id: str, *, allowed: tuple[str, ...] | None = None, **overrides: object
) -> dict:
    document: dict[str, object] = {
        "v": agent_run.RUN_PAYLOAD_VERSION,
        "run_id": run_id,
        "server_id": SERVER_ID,
        "context": (
            _context_doc(run_id)
            if allowed is None
            else _context_doc(run_id, allowed=allowed)
        ),
        "role": ROLE,
        "task": TASK,
        "connection": "gateway",
        "profile": "fast",
    }
    document.update(overrides)
    return document


def _canonical(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


#: The canonical fixture budget: a tree of at most depth 4, 4 children
#: per delegation, 3 concurrent active slots, 20 aggregate steps, 5k
#: tokens (the budgets suite's own document shape).
BUDGET_DOC: dict[str, object] = {
    "v": BUDGET_PAYLOAD_VERSION,
    "max_depth": 4,
    "max_children": 4,
    "max_active": 3,
    "max_steps": 20,
    "max_total_tokens": 5_000,
}


def _planned_intent(
    run_id: str | None = None,
    *,
    children: list[dict] | None = None,
    allowed: tuple[str, ...] = (READ_CAP,),
    depth: int = 0,
    child_ids: tuple[str, ...] | None = None,
    server_id: str = SERVER_ID,
    root_budget: dict[str, object] | None = None,
) -> tuple[delegate.DelegationIntent, delegate.ParentRunRow, tuple[str, ...]]:
    run_id = run_id or str(uuid.uuid4())
    intent = delegate.parse_delegation_intent(
        _canonical(_delegation_document(run_id, children, allowed=allowed))
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
    return intent, row, ids


class TestParseDelegationIntent:
    def test_minimal_document_parses_with_defaults(self) -> None:
        run_id = str(uuid.uuid4())
        intent = delegate.parse_delegation_intent(
            _canonical(_delegation_document(run_id))
        )
        assert intent.run.run_id == run_id
        assert intent.run.server_id == SERVER_ID
        assert intent.run.role == ROLE
        assert intent.run.connection == "gateway"
        assert intent.run.profile == "fast"
        assert len(intent.children) == 1
        child = intent.children[0]
        assert child.task == "Investigate aspect-alpha on the fixture host."
        assert child.role is None and child.connection is None
        assert child.profile is None
        assert child.capabilities == (READ_CAP,)
        assert (
            intent.children_wait_seconds == delegate.DEFAULT_CHILDREN_WAIT_SECONDS
        )
        assert intent.children_poll_seconds == delegate.DEFAULT_CHILDREN_POLL_SECONDS

    def test_explicit_child_fields_and_budgets_parse(self) -> None:
        run_id = str(uuid.uuid4())
        document = _delegation_document(
            run_id,
            [
                _child_spec(
                    "Investigate aspect-beta.",
                    role="investigator",
                    connection="gateway",
                    profile="deep",
                    capabilities=[READ_CAP, PRIV_CAP],
                )
            ],
            allowed=(READ_CAP, PRIV_CAP),
            children_wait_seconds=12.5,
            children_poll_seconds=0.5,
        )
        intent = delegate.parse_delegation_intent(_canonical(document))
        child = intent.children[0]
        assert child.role == "investigator"
        assert child.connection == "gateway" and child.profile == "deep"
        assert child.capabilities == (READ_CAP, PRIV_CAP)
        assert intent.children_wait_seconds == 12.5
        assert intent.children_poll_seconds == 0.5

    def test_omitted_capability_request_is_explicit_empty(self) -> None:
        run_id = str(uuid.uuid4())
        document = _delegation_document(
            run_id, [_child_spec("Investigate aspect-gamma.", capabilities=None)]
        )
        intent = delegate.parse_delegation_intent(_canonical(document))
        assert intent.children[0].capabilities == ()

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda doc: {**doc, "v": 2},
            lambda doc: {**doc, "unexpected": 1},
            lambda doc: {k: v for k, v in doc.items() if k != "run"},
            lambda doc: {k: v for k, v in doc.items() if k != "children"},
            lambda doc: {**doc, "children": []},
            lambda doc: {**doc, "children": "not-a-list"},
            lambda doc: {
                **doc,
                "children": [
                    _child_spec(f"Investigate aspect-{name}.")
                    for name in ("a", "b", "c", "d", "e", "f", "g", "h", "i")
                ],
            },
            lambda doc: {**doc, "children": ["not-an-object"]},
            lambda doc: {**doc, "children": [{"task": " "}]},
            lambda doc: {
                **doc,
                "children": [{"task": "Investigate aspect-delta.", "extra": 1}],
            },
            lambda doc: {
                **doc,
                "children": [{"task": "Investigate aspect-delta", "role": "two\nlines"}],
            },
            lambda doc: {
                **doc,
                "children": [
                    {"task": "Investigate aspect-delta", "role": "x" * (MAX_ROLE_CHARS + 1)}
                ],
            },
            lambda doc: {
                **doc,
                "children": [{"task": "Investigate aspect-delta", "connection": ""}],
            },
            lambda doc: {
                **doc,
                "children": [{"task": "Investigate aspect-delta", "profile": 5}],
            },
            lambda doc: {
                **doc,
                "children": [
                    {"task": "Investigate aspect-delta", "capabilities": "fixture.read"}
                ],
            },
            lambda doc: {
                **doc,
                "children": [{"task": "Investigate aspect-delta", "capabilities": [""]}],
            },
            lambda doc: {
                **doc,
                "children": [
                    {"task": "Investigate aspect-delta", "capabilities": [7]}
                ],
            },
            lambda doc: {
                **doc,
                "children": [
                    {
                        "task": "Investigate aspect-delta",
                        "server_id": str(uuid.uuid4()),
                    }
                ],
            },
            lambda doc: {**doc, "children_wait_seconds": 0},
            lambda doc: {
                **doc,
                "children_wait_seconds": delegate.MAX_CHILDREN_WAIT_SECONDS + 1,
            },
            lambda doc: {**doc, "children_poll_seconds": -1},
            lambda doc: {**doc, "children_wait_seconds": True},
            lambda doc: {**doc, "run": {**doc["run"], "v": 99}},
            lambda doc: {**doc, "run": "not-an-object"},
            lambda doc: {
                **doc,
                "run": {
                    **doc["run"],
                    "context": _context_doc(str(uuid.uuid4())),
                },
            },
        ]
        + [
            lambda doc, field=field: {
                **doc,
                "run": {k: v for k, v in doc["run"].items() if k != field},
            }
            for field in ("task", "role", "server_id", "context")
        ],
    )
    def test_invalid_documents_reject_value_free(self, mutation) -> None:
        run_id = str(uuid.uuid4())
        with pytest.raises(delegate.DelegationError) as error:
            delegate.parse_delegation_intent(_canonical(mutation(_delegation_document(run_id))))
        assert error.value.code is delegate.DelegationErrorCode.INVALID_PAYLOAD

    def test_non_string_and_non_json_payloads_reject(self) -> None:
        with pytest.raises(delegate.DelegationError):
            delegate.parse_delegation_intent("not json {")
        with pytest.raises(delegate.DelegationError):
            delegate.parse_delegation_intent(_canonical([1, 2, 3]))  # type: ignore[arg-type]


class TestPlanChildren:
    def test_child_grants_are_the_intersection(self) -> None:
        """GRANT-INTERSECT: requested ∩ parent, parent order, deduped."""
        intent, row, ids = _planned_intent(
            children=[
                _child_spec(
                    "Investigate aspect-alpha.", capabilities=[PRIV_CAP, READ_CAP, READ_CAP]
                )
            ],
            allowed=(READ_CAP, PRIV_CAP),
        )
        plan = delegate.plan_children(intent, row, ids)
        assert plan.children[0].grants == (READ_CAP, PRIV_CAP)

    def test_request_outside_the_parent_is_clamped_never_unioned(self) -> None:
        intent, row, ids = _planned_intent(
            children=[_child_spec("Investigate aspect-beta.", capabilities=[READ_CAP, PRIV_CAP])],
            allowed=(READ_CAP,),
        )
        plan = delegate.plan_children(intent, row, ids)
        assert plan.children[0].grants == (READ_CAP,)

    def test_omitted_request_grants_nothing(self) -> None:
        intent, row, ids = _planned_intent(
            children=[_child_spec("Investigate aspect-gamma.", capabilities=None)],
            allowed=(READ_CAP, PRIV_CAP),
        )
        plan = delegate.plan_children(intent, row, ids)
        assert plan.children[0].grants == ()

    def test_child_persists_parent_server_and_scope(self) -> None:
        """AC1: the linkage facts (C05 rule 3 + C03 parent scope)."""
        run_id = str(uuid.uuid4())
        intent, row, ids = _planned_intent(run_id=run_id)
        plan = delegate.plan_children(intent, row, ids)
        payload = json.loads(plan.children[0].payload_json)
        assert payload["run_id"] == ids[0]
        assert payload["server_id"] == SERVER_ID  # the parent's server, pinned
        context = payload["context"]
        assert context["server_id"] == SERVER_ID
        assert context["parent_scope"] == {"run_id": run_id, "server_id": SERVER_ID}
        assert context["principal"] == intent.run.context.principal
        assert context["profile_revision"] == intent.run.context.profile_revision
        assert context["prompt_revision"] == intent.run.context.prompt_revision
        assert context["mode"] == intent.run.context.mode.value
        # The payload re-validates through the ONE agent-run producer.
        parsed = agent_run.parse_agent_run_intent(plan.children[0].payload_json)
        assert parsed.context.parent_scope is not None
        assert str(parsed.context.parent_scope.run_id) == run_id
        assert parsed.role == ROLE and parsed.task == intent.children[0].task
        assert parsed.connection == "gateway" and parsed.profile == "fast"
        assert parsed.max_rounds == intent.run.max_rounds
        assert parsed.reconcile_wait_seconds == intent.run.reconcile_wait_seconds

    def test_child_role_and_selection_overrides_win(self) -> None:
        intent, row, ids = _planned_intent(
            children=[
                _child_spec(
                    "Investigate aspect-delta.",
                    role="investigator",
                    profile="deep",
                )
            ],
        )
        plan = delegate.plan_children(intent, row, ids)
        parsed = agent_run.parse_agent_run_intent(plan.children[0].payload_json)
        assert parsed.role == "investigator"
        assert parsed.profile == "deep"
        assert parsed.connection == "gateway"  # inherited

    def test_depth_exhausted_parent_cannot_delegate(self) -> None:
        intent, row, ids = _planned_intent(depth=delegate.MAX_DELEGATION_DEPTH)
        with pytest.raises(delegate.DelegationError) as error:
            delegate.plan_children(intent, row, ids)
        assert error.value.code is delegate.DelegationErrorCode.DELEGATION_DEPTH

    def test_parent_row_server_mismatch_rejects(self) -> None:
        intent, row, ids = _planned_intent(server_id=str(uuid.uuid4()))
        with pytest.raises(delegate.DelegationError) as error:
            delegate.plan_children(intent, row, ids)
        assert error.value.code is delegate.DelegationErrorCode.INVALID_RUN_STATE

    def test_identity_count_mismatch_rejects(self) -> None:
        intent, row, _ = _planned_intent()
        with pytest.raises(delegate.DelegationError) as error:
            delegate.plan_children(intent, row, ())
        assert error.value.code is delegate.DelegationErrorCode.INVALID_PAYLOAD

    def test_non_canonical_child_identity_rejects(self) -> None:
        intent, row, _ = _planned_intent()
        upper = str(uuid.uuid4()).upper()
        with pytest.raises(delegate.DelegationError) as error:
            delegate.plan_children(intent, row, (upper,))
        assert error.value.code is delegate.DelegationErrorCode.INVALID_PAYLOAD

    def test_multiple_children_plan_in_specification_order(self) -> None:
        intent, row, ids = _planned_intent(
            children=[
                _child_spec("Investigate aspect-alpha."),
                _child_spec("Investigate aspect-beta."),
                _child_spec("Investigate aspect-gamma.", capabilities=None),
            ],
        )
        plan = delegate.plan_children(intent, row, ids)
        assert plan.child_run_ids == ids
        assert plan.children[2].grants == ()


class TestBudgetNarrowedPlanning:
    """The budget document NARROWS the planner's gates (C05 rule 7)."""

    def test_budget_narrows_depth_fail_closed(self) -> None:
        budget = dict(BUDGET_DOC, max_depth=2)
        intent, row, ids = _planned_intent(depth=2, root_budget=budget)
        with pytest.raises(BudgetError) as error:
            delegate.plan_children(intent, row, ids)
        assert error.value.code is BudgetErrorCode.DEPTH_EXHAUSTED

    def test_budget_narrows_fanout_fail_closed(self) -> None:
        budget = dict(BUDGET_DOC, max_children=2)
        intent, row, ids = _planned_intent(
            children=[
                _child_spec("Investigate aspect-alpha."),
                _child_spec("Investigate aspect-beta."),
                _child_spec("Investigate aspect-gamma."),
            ],
            root_budget=budget,
        )
        with pytest.raises(BudgetError) as error:
            delegate.plan_children(intent, row, ids)
        assert error.value.code is BudgetErrorCode.FANOUT_EXHAUSTED

    def test_budget_at_the_module_bounds_plans(self) -> None:
        """A budget that merely restates the module bounds never rejects."""
        budget = dict(
            BUDGET_DOC,
            max_depth=delegate.MAX_DELEGATION_DEPTH,
            max_children=delegate.MAX_CHILDREN_PER_RUN,
        )
        intent, row, ids = _planned_intent(
            children=[_child_spec("Investigate aspect-alpha.")],
            root_budget=budget,
        )
        plan = delegate.plan_children(intent, row, ids)
        assert plan.child_run_ids == ids

    def test_unbudgeted_row_keeps_the_module_bounds_alone(self) -> None:
        intent, row, ids = _planned_intent(depth=delegate.MAX_DELEGATION_DEPTH - 1)
        plan = delegate.plan_children(intent, row, ids)
        assert plan.child_run_ids == ids

    def test_corrupt_stored_budget_rejects(self) -> None:
        intent, row, ids = _planned_intent(
            root_budget=dict(BUDGET_DOC, v=99),
        )
        with pytest.raises(delegate.DelegationError) as error:
            delegate.plan_children(intent, row, ids)
        assert error.value.code is delegate.DelegationErrorCode.INVALID_RUN_STATE


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
        credentials=lambda: EnvResolver("/tmp/mai-fxt-06-child-runs/absent.env"),
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
        executor = _wired_executor(tmp_path)
        client = EngineClient("postgresql://unused:unused@127.0.0.1:1/unused")
        sessions = _unused_sessions()
        with pytest.raises(delegate.DelegationError) as error:
            delegate.DelegationExecutor(sessions=object(), executor=executor, client=client, ledger=BudgetLedger(sessions))  # type: ignore[arg-type]
        assert error.value.code is delegate.DelegationErrorCode.INVALID_WIRING
        with pytest.raises(delegate.DelegationError) as error:
            delegate.DelegationExecutor(sessions=sessions, executor=object(), client=client, ledger=BudgetLedger(sessions))  # type: ignore[arg-type]
        assert error.value.code is delegate.DelegationErrorCode.INVALID_WIRING
        with pytest.raises(delegate.DelegationError) as error:
            delegate.DelegationExecutor(sessions=sessions, executor=executor, client=object(), ledger=BudgetLedger(sessions))  # type: ignore[arg-type]
        assert error.value.code is delegate.DelegationErrorCode.INVALID_WIRING
        with pytest.raises(delegate.DelegationError) as error:
            delegate.DelegationExecutor(sessions=sessions, executor=executor, client=client, ledger=object())  # type: ignore[arg-type]
        assert error.value.code is delegate.DelegationErrorCode.INVALID_WIRING

    def test_valid_wiring_composes_and_config_seam_round_trips(
        self, tmp_path: Path
    ) -> None:
        sessions = _unused_sessions()
        executor = delegate.DelegationExecutor(
            sessions=sessions,
            executor=_wired_executor(tmp_path),
            client=EngineClient("postgresql://unused:unused@127.0.0.1:1/unused"),
            ledger=BudgetLedger(sessions),
        )
        assert isinstance(executor.executor, agent_run.AgentRunExecutor)
        delegate.configure_delegation(executor)
        with pytest.raises(delegate.DelegationError) as error:
            delegate.configure_delegation(executor)
        assert error.value.code is delegate.DelegationErrorCode.ALREADY_CONFIGURED
        delegate.reset_delegation()
        delegate.reset_delegation()  # idempotent
        with pytest.raises(delegate.DelegationError) as error:
            delegate.configure_delegation(object())  # type: ignore[arg-type]
        assert error.value.code is delegate.DelegationErrorCode.INVALID_WIRING


# ---------------------------------------------------------------------------
# live fixtures: real disposable Postgres + the real engine shape
# ---------------------------------------------------------------------------


def _alembic_config() -> Config:
    return Config(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state (engine schema too).

    The 0011 tag associations drop before their ``job_runs``/``tags``
    parents so the reset stays FK-safe on a schema-bearing database
    (history-tags W1-FIX1).
    """
    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS objective_journal, objective_checklists, "
            "objective_events, objective_runs, objective_servers, "
            "objectives, "
            "job_resource_locks, job_schedule_fires, "
            "job_schedules, job_events, job_steps, job_tags, job_runs, "
            "approvals, profile_projection_journal, profile_projection, "
            "admin_sessions, admin_credential, alembic_version, "
            "conversation_tags, tags"
        )
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def delegate_url() -> str:
    """Disposable fixture database: Alembic chain + engine-owned schema."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the durable delegation claims need a "
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
    return url


@pytest.fixture
def factory(delegate_url: str) -> async_sessionmaker[AsyncSession]:
    """NullPool session factory for in-process facade assertions."""
    eng, sessions = create_app_engine(delegate_url)

    async def _clean() -> None:
        async with sessions() as session:
            for table in (
                "job_resource_locks",
                "job_schedule_fires",
                "job_steps",
                "job_events",
                "job_schedules",
                "job_tags",
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
    """Insert a root ``job_runs`` row (the submit owner's row, by SQL).

    The 0011 mandatory-tag invariant requires the classification in the
    SAME transaction as the row (history-tags W1-FIX1): the fixture seeds
    the reserved unclassified tag exactly like the real submit writer.
    """

    async def _insert(
        run_id: str, root_budget: dict[str, object] | None = None
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
                        (:run_id, :server_id, 0, 3, 7, 'fixture-fast',
                         '["fixture.read"]', :root_budget, now(), now())
                    """
                ),
                {
                    "run_id": uuid.UUID(run_id),
                    "server_id": uuid.UUID(SERVER_ID),
                    "root_budget": (
                        json.dumps(root_budget) if root_budget is not None else None
                    ),
                },
            )
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


def _write_data_root(root: Path) -> None:
    from mai.profiles.files import ProfileFileStore

    store = ProfileFileStore(root)
    profile_text = (
        f"schema_version: {SERVER_SCHEMA_VERSION}\n"
        f'server_id: "{SERVER_ID}"\n'
        'name: "delegation-fixture"\n'
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
        "  - fixture.read\n"
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
        *, base_url: str = "http://127.0.0.1:9/v1", credential: bool = True
    ) -> dict[str, Path]:
        model_root = scenario / "model-root"
        data_root = scenario / "data-root"
        _write_model_root(model_root, base_url=base_url, credential=credential)
        _write_data_root(data_root)
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
"""Worker-side composition for 06-child-runs: real dispatcher, journaled
effects, deterministic FunctionModel fake, shared agent-run + delegation
executors (the agent.run children and the agent.delegate parent run in
the same worker process)."""

import json
import os
import re
import time
import uuid

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
from mai.jobs.engine import EngineClient
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
    """Journaled capability executor (same discipline as 06-agent-run):
    ``begin`` before the effect, optional gate wait, then the effect
    marker — "the effect happened exactly once" is observable."""

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
    aspect_re = re.compile(r"aspect-([a-z]+)")

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
        # Journal WHICH task this conversation carries (the first user
        # prompt): "the child got ITS task" is observable per marker.
        for message in messages:
            if message.kind == "request":
                for part in message.parts:
                    if getattr(part, "part_kind", "") == "user-prompt":
                        found = aspect_re.findall(str(part.content))
                        for token in found:
                            _mark(f"aspect:{token}")
                        break
                break
        # Journal the latest tool return status (denial honesty, AC2).
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
        # conversation length, never from process-local call counts.
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


_build()
'''


def _write_policy_files(
    scenario: Path, *, grants: list[dict], denied: list[str] | None = None
) -> None:
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


def _open_gate(scenario: Path, name: str) -> None:
    (scenario / "gates" / name).write_text("go", encoding="utf-8")


def _read_step_rows(url: str, run_id: str) -> list[dict]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT step_id, seq, capability_id, state, receipt FROM job_steps "
            "WHERE run_id = %s ORDER BY seq",
            (uuid.UUID(run_id),),
        ).fetchall()


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
            "SELECT run_id, parent_run_id, depth, requested_grants, root_budget "
            "FROM job_runs WHERE parent_run_id = %s ORDER BY run_id",
            (uuid.UUID(parent_run_id),),
        ).fetchall()


async def _run_states(factory: async_sessionmaker, run_id: str) -> tuple:
    return await RunLifecycle(factory).replay_states(run_id)


async def _current_state(factory: async_sessionmaker, run_id: str) -> str | None:
    state = await RunLifecycle(factory).current_state(run_id)
    return None if state is None else state.value


async def _enqueue_delegation(
    client: EngineClient,
    insert_run: Callable[..., object],
    run_id: str,
    document: dict,
    root_budget: dict[str, object] | None = None,
) -> None:
    await insert_run(run_id, root_budget=root_budget)
    client.enqueue_run(run_id, delegate.HANDLER_NAME, _canonical(document))


async def _enqueue_decoy_steps_run(
    client: EngineClient,
    insert_run: Callable[[str], object],
    run_id: str,
    marker: str,
) -> None:
    """A saturated-queue decoy: ONE gated fixture.read step (slot holder)."""
    await insert_run(run_id)
    document = {
        "v": steps.RUN_PAYLOAD_VERSION,
        "run_id": run_id,
        "server_id": SERVER_ID,
        "context": _context_doc(run_id),
        "steps": [
            {"seq": 1, "capability_id": READ_CAP, "payload": {"marker": marker}}
        ],
    }
    client.enqueue_run(run_id, steps.HANDLER_NAME, _canonical(document))


def _engine_state(client: EngineClient, run_id: str) -> str | None:
    state = client.run_state(run_id)
    return None if state is None else state.value


def _workflow_status(url: str, run_id: str):
    """The engine's own workflow status (child edge, queue binding, input)."""
    from dbos import DBOSClient

    client = DBOSClient(
        system_database_url=url,
        application_name=engine.APPLICATION_NAME,
        lazy=True,
    )
    try:
        return client.retrieve_workflow(run_id).get_status()
    finally:
        client.destroy()


def _failure_reasons(url: str, run_id: str) -> list[str]:
    """The classified failure reasons on the stream (agent.failed events)."""
    return [
        str(event.data["reason"])
        for event in _read_events(url, run_id)
        if event.type == "agent.failed"
    ]


# ---------------------------------------------------------------------------
# live: the durable delegation tree (real engine, real database, real workers)
# ---------------------------------------------------------------------------


class TestDelegatingParents:
    def test_seeded_run_carries_reserved_classification(
        self, factory: async_sessionmaker[AsyncSession], insert_run
    ) -> None:
        """0011 invariant: the raw fixture seeds the tag transactionally."""
        run_id = str(uuid.uuid4())
        _run(insert_run(run_id))

        async def _associations():
            async with factory() as session:
                return (
                    await session.execute(
                        text(
                            "SELECT tag_id, assignment_source FROM job_tags "
                            "WHERE job_id = :job_id"
                        ),
                        {"job_id": uuid.UUID(run_id)},
                    )
                ).fetchall()

        assert _run(_associations()) == [
            (uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID), SYSTEM_ASSIGNMENT_SOURCE)
        ]

    def test_parent_delegates_parallel_children_and_yields_with_full_queue(
        self, delegate_url, factory, insert_run, scenario, composition
    ) -> None:
        """AC1: parallel benign investigations under a saturated queue.

        The worker's queue holds TWO slots; the parent (parked) and the
        gated decoy occupy both. The children are engine children, NOT
        queue entries, so all three still execute — the yielding parent
        never deadlocks worker capacity (C05 rule 7).
        """
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _script = [
            {"tool_call": {"name": "fixture_read", "args": {"marker": "m1"}, "id": "c1"}},
            {"text": "investigation done"},
        ]
        (scenario / "model-script.json").write_text(json.dumps(_script), encoding="utf-8")
        (scenario / "behavior.json").write_text(
            json.dumps({"fixture.read:gate-decoy": "gate"}), encoding="utf-8"
        )
        parent_id = str(uuid.uuid4())
        decoy_id = str(uuid.uuid4())
        document = _delegation_document(
            parent_id,
            [
                _child_spec("Investigate aspect-alpha on the fixture host."),
                _child_spec("Investigate aspect-beta on the fixture host."),
                _child_spec("Investigate aspect-gamma on the fixture host."),
            ],
            children_poll_seconds=0.5,
        )
        client = EngineClient(delegate_url)
        worker = None
        try:
            worker = _spawn_worker(delegate_url, scenario / "handlers", scenario, "w1")
            # The decoy claims a queue slot FIRST and holds it while gated.
            _run(_enqueue_decoy_steps_run(client, insert_run, decoy_id, "gate-decoy"))
            _run(_enqueue_delegation(client, insert_run, parent_id, document))
            _wait_until(
                lambda: (_engine_state(client, parent_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "parent success while the queue is saturated",
            )
            assert _journal_count(scenario, "effect:fixture.read:m1") == 3
            assert _journal_count(scenario, "begin:fixture.read:m1") == 3
        finally:
            # Release the decoy for a clean shutdown; a stalled graceful
            # stop falls back to SIGKILL and never masks a body failure.
            _open_gate(scenario, "gate-fixture.read-gate-decoy")
            if worker is not None:
                _stop(worker)
            client.close()

        # Correct prompt/target: each child investigated ITS aspect.
        for token in ("alpha", "beta", "gamma"):
            assert _journal_count(scenario, f"aspect:{token}") == 2
        assert _journal_count(scenario, "tool_return:executed") == 3

        # Engine child edges: every child names the parent; none is a
        # queue entry; the parent (and only the parent) is on the queue.
        parent_status = _workflow_status(delegate_url, parent_id)
        assert parent_status.queue_name == engine.QUEUE_NAME
        for child_id in [str(row["run_id"]) for row in _read_child_rows(delegate_url, parent_id)]:
            status = _workflow_status(delegate_url, child_id)
            assert status.parent_workflow_id == parent_id
            assert status.queue_name is None

        # The persisted linkage (C05 rule 3): parent_run_id, depth,
        # requested_grants = the intersection, root_budget inherited.
        rows = _read_child_rows(delegate_url, parent_id)
        assert len(rows) == 3
        for row in rows:
            assert row["parent_run_id"] == uuid.UUID(parent_id)
            assert row["depth"] == 1
            assert row["requested_grants"] == ["fixture.read"]
            assert row["root_budget"] is None

        # The child payload's context carried the parent scope.
        child_status = _workflow_status(delegate_url, str(rows[0]["run_id"]))
        child_args = child_status.input["args"]
        child_document = json.loads(child_args[1])
        assert child_document["context"]["parent_scope"] == {
            "run_id": parent_id,
            "server_id": SERVER_ID,
        }
        # The child executes the agent-run handler under its own identity.
        assert child_args[0] == agent_run.HANDLER_NAME

        # The parent's frozen lifecycle: parked, then woken, then done.
        assert _run(_run_states(factory, parent_id)) == (
            "queued",
            "running",
            "waiting_children",
            "running",
            "succeeded",
        )
        events = _read_events(delegate_url, parent_id)
        planned = [e for e in events if e.type == delegate.DELEGATION_PLANNED_EVENT_TYPE]
        assert len(planned) == 1
        assert planned[0].data["depth"] == 1
        assert sorted(planned[0].data["children"]) == sorted(
            str(row["run_id"]) for row in rows
        )
        assert [e.data for e in events if e.type == delegate.DELEGATION_WAIT_EVENT_TYPE]
        resolved = [
            e for e in events if e.type == delegate.CHILDREN_RESOLVED_EVENT_TYPE
        ]
        assert len(resolved) == 1
        assert all(
            item["state"] == "succeeded" for item in resolved[0].data["children"]
        )
        assert any(
            e.type == delegate.DELEGATION_COMPLETED_EVENT_TYPE for e in events
        )

    def test_child_escalation_denied_at_grant_boundary(
        self, delegate_url, factory, insert_run, scenario, composition
    ) -> None:
        """AC2: a child cannot amplify tools; the denial is honest.

        The parent's context grants BOTH capabilities, so the child's
        request intersects to both — but the SERVER holds no fixture.priv
        grant, and the dispatcher denies at the effect boundary
        (GRANT-INTERSECT recheck). The child answers inline and succeeds;
        the effect never ran.
        """
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _script = [
            {"tool_call": {"name": "fixture_priv", "args": {"marker": "e1"}, "id": "c1"}},
            {"text": "escalation denied, done"},
        ]
        (scenario / "model-script.json").write_text(json.dumps(_script), encoding="utf-8")
        parent_id = str(uuid.uuid4())
        document = _delegation_document(
            parent_id,
            [_child_spec("Investigate aspect-alpha.", capabilities=[READ_CAP, PRIV_CAP])],
            allowed=(READ_CAP, PRIV_CAP),
            children_poll_seconds=0.5,
        )
        client = EngineClient(delegate_url)
        worker = None
        try:
            worker = _spawn_worker(delegate_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_delegation(client, insert_run, parent_id, document))
            _wait_until(
                lambda: (_engine_state(client, parent_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "parent success with the denied escalation",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        # Nothing ran: no begin, no effect marker for the privileged call.
        assert _journal_count(scenario, "begin:fixture.priv") == 0
        assert _journal_count(scenario, "effect:fixture.priv") == 0
        # The model saw the honest denial and finished.
        assert _journal_count(scenario, "tool_return:denied") == 1

        rows = _read_child_rows(delegate_url, parent_id)
        assert len(rows) == 1
        assert rows[0]["requested_grants"] == [READ_CAP, PRIV_CAP]
        child_id = str(rows[0]["run_id"])
        assert _run(_current_state(factory, child_id)) == "succeeded"
        step_rows = _read_step_rows(delegate_url, child_id)
        assert len(step_rows) == 1
        receipt = step_rows[0]["receipt"]
        assert step_rows[0]["state"] == "failed"
        assert receipt["reason"] == "capability_not_granted"
        assert receipt["effect"] == "not_attempted"
        assert _run(_run_states(factory, parent_id)) == (
            "queued",
            "running",
            "waiting_children",
            "running",
            "succeeded",
        )

    def test_facade_tree_cancel_propagates_through_child_edges(
        self, delegate_url, factory, insert_run, scenario, composition
    ) -> None:
        """AC2: explicit tree cancellation rides the engine child edges."""
        from mai.jobs.recovery import RecoveryService

        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _script = [
            {"tool_call": {"name": "fixture_read", "args": {"marker": "m1"}, "id": "c1"}},
            {"text": "should never finish"},
        ]
        (scenario / "model-script.json").write_text(json.dumps(_script), encoding="utf-8")
        (scenario / "behavior.json").write_text(
            json.dumps({"fixture.read:m1": "gate"}), encoding="utf-8"
        )
        parent_id = str(uuid.uuid4())
        document = _delegation_document(
            parent_id,
            [_child_spec("Investigate aspect-alpha.")],
            children_poll_seconds=0.5,
        )
        client = EngineClient(delegate_url)
        recovery = RecoveryService(delegate_url, factory)
        worker = None
        try:
            worker = _spawn_worker(delegate_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_delegation(client, insert_run, parent_id, document))
            _wait_until(
                lambda: _journal_count(scenario, "begin:fixture.read:m1") == 1,
                RUN_TIMEOUT,
                "the child's gated effect",
            )
            assert _run(_current_state(factory, parent_id)) == "waiting_children"
            result = _run(
                recovery.acancel_run(
                    parent_id, cancel_children=True, outcome_timeout=60.0
                )
            )
            assert result.outcome.value == "cancelled"
            assert (_engine_state(client, parent_id) or "") == "cancelled"
            rows = _read_child_rows(delegate_url, parent_id)
            child_id = str(rows[0]["run_id"])
            _wait_until(
                lambda: (_engine_state(client, child_id) or "") == "cancelled",
                RUN_TIMEOUT,
                "the native child-edge cancellation",
            )
            # The reviewed recovery owner's lagging-facade repair applies
            # the engine truth to the child's facade diagram (its
            # ``acancel_run`` entry rejects a run with an in-flight step —
            # recorded as an out-of-scope finding for the recovery owner).
            child_facade = _run(recovery.aapply_engine_outcome(child_id))
            assert child_facade is RunState.CANCELLED
            assert _run(_current_state(factory, child_id)) == "cancelled"
            # The gated effect never completed.
            assert _journal_count(scenario, "effect:fixture.read:m1") == 0
        finally:
            _open_gate(scenario, "gate-fixture.read-m1")
            if worker is not None:
                _stop(worker)
            client.close()

        # The facade truth applied on both sides of the edge.
        assert _run(_run_states(factory, parent_id)) == (
            "queued",
            "running",
            "waiting_children",
            "running",
            "cancelled",
        )
        _wait_until(
            lambda: _run(
                _current_state(
                    factory,
                    str(_read_child_rows(delegate_url, parent_id)[0]["run_id"]),
                )
            )
            == "cancelled",
            RUN_TIMEOUT,
            "the child's facade cancellation",
        )

    def test_parent_cancel_belt_stops_children_when_children_are_kept(
        self, delegate_url, factory, insert_run, scenario, composition
    ) -> None:
        """AC2: the parent is cancelled WITHOUT tree cancel (guard rule 4:
        children keep running) — the handler's belt still stops the tree:
        a cancelled parent never orphans its children."""
        from mai.jobs.recovery import RecoveryService

        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _script = [
            {"tool_call": {"name": "fixture_read", "args": {"marker": "m1"}, "id": "c1"}},
            {"text": "should never finish"},
        ]
        (scenario / "model-script.json").write_text(json.dumps(_script), encoding="utf-8")
        (scenario / "behavior.json").write_text(
            json.dumps({"fixture.read:m1": "gate"}), encoding="utf-8"
        )
        parent_id = str(uuid.uuid4())
        document = _delegation_document(
            parent_id,
            [_child_spec("Investigate aspect-alpha.")],
            children_poll_seconds=0.5,
        )
        client = EngineClient(delegate_url)
        recovery = RecoveryService(delegate_url, factory)
        worker = None
        try:
            worker = _spawn_worker(delegate_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_delegation(client, insert_run, parent_id, document))
            _wait_until(
                lambda: _journal_count(scenario, "begin:fixture.read:m1") == 1,
                RUN_TIMEOUT,
                "the child's gated effect",
            )
            result = _run(
                recovery.acancel_run(
                    parent_id, cancel_children=False, outcome_timeout=60.0
                )
            )
            assert result.outcome.value == "cancelled"
            rows = _read_child_rows(delegate_url, parent_id)
            child_id = str(rows[0]["run_id"])
            # The belt: within one poll interval the parent's cancellation
            # surfaces at a checkpoint and the children are stopped.
            _wait_until(
                lambda: (_engine_state(client, child_id) or "") == "cancelled",
                RUN_TIMEOUT,
                "the belt's child cancellation",
            )
            assert _journal_count(scenario, "effect:fixture.read:m1") == 0
        finally:
            _open_gate(scenario, "gate-fixture.read-m1")
            if worker is not None:
                _stop(worker)
            client.close()

        assert _run(_run_states(factory, parent_id)) == (
            "queued",
            "running",
            "waiting_children",
            "running",
            "cancelled",
        )

    def test_kill_mid_wait_recovers_with_the_same_children(
        self, delegate_url, factory, insert_run, scenario, composition
    ) -> None:
        """AC1: the parked tree survives a worker kill -9: the plan's child
        identities are checkpointed (no re-plan, no duplicate rows), the
        park replays idempotently, and the tree completes on a fresh worker."""
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _script = [
            {
                "gate": "gate-model-round-0",
                "tool_call": {
                    "name": "fixture_read",
                    "args": {"marker": "m1"},
                    "id": "c1",
                },
            },
            {"text": "investigation done"},
        ]
        (scenario / "model-script.json").write_text(json.dumps(_script), encoding="utf-8")
        parent_id = str(uuid.uuid4())
        document = _delegation_document(
            parent_id,
            [
                _child_spec("Investigate aspect-alpha."),
                _child_spec("Investigate aspect-beta."),
            ],
            children_poll_seconds=0.5,
        )
        client = EngineClient(delegate_url)
        worker = None
        try:
            worker = _spawn_worker(delegate_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_delegation(client, insert_run, parent_id, document))
            _wait_until(
                lambda: _journal_count(scenario, "model_call") == 2,
                RUN_TIMEOUT,
                "both children parked in the gated model round",
            )
            _wait_until(
                lambda: _run(_current_state(factory, parent_id)) == "waiting_children",
                RUN_TIMEOUT,
                "the parent parked",
            )
            planned = [
                e
                for e in _read_events(delegate_url, parent_id)
                if e.type == delegate.DELEGATION_PLANNED_EVENT_TYPE
            ]
            assert len(planned) == 1
            child_ids = sorted(planned[0].data["children"])
            assert len(child_ids) == 2

            _kill9(worker)
            worker = _spawn_worker(
                delegate_url, scenario / "handlers", scenario, "w2"
            )
            _wait_until(
                lambda: _run(_current_state(factory, parent_id)) == "waiting_children",
                RUN_TIMEOUT,
                "the fresh worker's recovered park",
            )
            # The checkpointed plan: SAME child identities, ONE plan row
            # set, ONE wait event (budgets never restart).
            rows = _read_child_rows(delegate_url, parent_id)
            assert sorted(str(row["run_id"]) for row in rows) == child_ids
            events = _read_events(delegate_url, parent_id)
            assert (
                len(
                    [
                        e
                        for e in events
                        if e.type == delegate.DELEGATION_PLANNED_EVENT_TYPE
                    ]
                )
                == 1
            )
            assert (
                len(
                    [
                        e
                        for e in events
                        if e.type == delegate.DELEGATION_WAIT_EVENT_TYPE
                    ]
                )
                == 1
            )

            _open_gate(scenario, "gate-model-round-0")
            _wait_until(
                lambda: (_engine_state(client, parent_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "parent success after recovery",
            )
        finally:
            _open_gate(scenario, "gate-model-round-0")
            if worker is not None:
                _stop(worker)
            client.close()

        # Each child's effect ran exactly once across the crash.
        assert _journal_count(scenario, "effect:fixture.read:m1") == 2
        assert _journal_count(scenario, "begin:fixture.read:m1") == 2
        rows = _read_child_rows(delegate_url, parent_id)
        for row in rows:
            assert (_engine_state(client, str(row["run_id"])) or "") == "succeeded"
        resolved = [
            e
            for e in _read_events(delegate_url, parent_id)
            if e.type == delegate.CHILDREN_RESOLVED_EVENT_TYPE
        ]
        assert len(resolved) == 1
        assert all(
            item["state"] == "succeeded" for item in resolved[0].data["children"]
        )

    def test_children_wait_deadline_fails_parent_and_stops_tree(
        self, delegate_url, factory, insert_run, scenario, composition
    ) -> None:
        """AC1/C05 rule 7: the wait budget is explicit and honest — expiry
        fails the parent (never a silent success) and stops the tree."""
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _script = [
            {
                "gate": "gate-model-round-0",
                "tool_call": {
                    "name": "fixture_read",
                    "args": {"marker": "m1"},
                    "id": "c1",
                },
            },
            {"text": "unreached"},
        ]
        (scenario / "model-script.json").write_text(json.dumps(_script), encoding="utf-8")
        parent_id = str(uuid.uuid4())
        document = _delegation_document(
            parent_id,
            [_child_spec("Investigate aspect-alpha.")],
            children_wait_seconds=1.0,
            children_poll_seconds=0.5,
        )
        client = EngineClient(delegate_url)
        worker = None
        try:
            worker = _spawn_worker(delegate_url, scenario / "handlers", scenario, "w1")
            _run(_enqueue_delegation(client, insert_run, parent_id, document))
            _wait_until(
                lambda: (_engine_state(client, parent_id) or "") == "failed",
                RUN_TIMEOUT,
                "the honest deadline failure",
            )
            rows = _read_child_rows(delegate_url, parent_id)
            child_id = str(rows[0]["run_id"])
            # The belt stopped the tree.
            _wait_until(
                lambda: (_engine_state(client, child_id) or "") == "cancelled",
                RUN_TIMEOUT,
                "the belted child cancellation",
            )
        finally:
            _open_gate(scenario, "gate-model-round-0")
            if worker is not None:
                _stop(worker)
            client.close()

        assert _failure_reasons(delegate_url, parent_id) == [
            delegate.CHILDREN_WAIT_DEADLINE_REASON
        ]
        assert _run(_run_states(factory, parent_id)) == (
            "queued",
            "running",
            "waiting_children",
            "running",
            "failed",
        )
        # The linkage rows persist (the tree is recorded, not erased).
        assert len(_read_child_rows(delegate_url, parent_id)) == 1


class TestBudgetedDelegations:
    """The root ledger, live: reserve → charge → release through the
    REAL engine + workers (BUDGET-CAP's assembly half at the producer)."""

    def test_budgeted_delegation_reserves_charges_and_releases(
        self, delegate_url, factory, insert_run, scenario, composition
    ) -> None:
        """A budgeted tree: the batch's slots are reserved BEFORE any
        child starts, each child's durable rounds are charged to the
        root's aggregate budget, and the batch is released when the
        children resolve — the folded truth on the root's stream."""
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _script = [
            {"tool_call": {"name": "fixture_read", "args": {"marker": "m1"}}},
            {"text": "child investigation done"},
        ]
        (scenario / "model-script.json").write_text(json.dumps(_script), encoding="utf-8")
        parent_id = str(uuid.uuid4())
        document = _delegation_document(
            parent_id,
            [
                _child_spec("Investigate aspect-alpha on the fixture host."),
                _child_spec("Investigate aspect-beta on the fixture host."),
            ],
            children_poll_seconds=0.5,
        )
        client = EngineClient(delegate_url)
        worker = None
        try:
            worker = _spawn_worker(delegate_url, scenario / "handlers", scenario, "w1")
            _run(
                _enqueue_delegation(
                    client,
                    insert_run,
                    parent_id,
                    document,
                    root_budget=dict(BUDGET_DOC),
                )
            )
            _wait_until(
                lambda: (_engine_state(client, parent_id) or "") == "succeeded",
                RUN_TIMEOUT,
                "the budgeted parent success",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        rows = _read_child_rows(delegate_url, parent_id)
        assert len(rows) == 2
        for row in rows:
            # The verbatim budget propagation (C05 rule 3) now bites: the
            # child rows carry the SAME document the ledger enforces.
            assert row["root_budget"] == BUDGET_DOC
        child_ids = sorted(str(row["run_id"]) for row in rows)
        events = _read_events(delegate_url, parent_id)
        types = [event.type for event in events]

        reserved = next(
            event for event in events if event.type == BUDGET_RESERVED_EVENT_TYPE
        )
        assert reserved.data["owner"] == parent_id
        assert sorted(reserved.data["children"]) == child_ids
        # The reservation precedes the children's start: it commits in the
        # dedicated checkpoint between planning and the engine child edges.
        assert types.index(BUDGET_RESERVED_EVENT_TYPE) < types.index(
            "agent.delegation_wait"
        )

        charged = [event for event in events if event.type == BUDGET_CHARGED_EVENT_TYPE]
        assert sorted(str(event.data["run"]) for event in charged) == child_ids
        # Each child ran exactly two checkpointed model rounds; tokens
        # stay unclaimed (the model surface's recorded boundary).
        assert all(event.data["steps"] == 2 for event in charged)
        assert all(event.data["tokens"] == 0 for event in charged)

        released = next(
            event for event in events if event.type == BUDGET_RELEASED_EVENT_TYPE
        )
        assert released.data["owner"] == parent_id
        assert sorted(released.data["children"]) == child_ids
        # The close-out precedes the completion event (settle → finish).
        assert types.index(BUDGET_RELEASED_EVENT_TYPE) < types.index(
            delegate.DELEGATION_COMPLETED_EVENT_TYPE
        )

        # The fold is the truth: 4 aggregate durable steps, no slots held.
        snapshot = _run(BudgetLedger(factory).asnapshot(parent_id))
        assert snapshot.consumed_steps == 4
        assert snapshot.outstanding == ()
        assert snapshot.active_reserved == 0
        assert snapshot.remaining_steps == BUDGET_DOC["max_steps"] - 4

    def test_budget_fanout_rejection_fails_parent_before_children(
        self, delegate_url, factory, insert_run, scenario, composition
    ) -> None:
        """A delegation that can never fit the budget's fan-out is
        rejected whole: classified budget failure, no child rows, no
        reservation — the aggregate gate bites before any start."""
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        parent_id = str(uuid.uuid4())
        document = _delegation_document(
            parent_id,
            [
                _child_spec("Investigate aspect-alpha."),
                _child_spec("Investigate aspect-beta."),
            ],
            children_poll_seconds=0.5,
        )
        client = EngineClient(delegate_url)
        worker = None
        try:
            worker = _spawn_worker(delegate_url, scenario / "handlers", scenario, "w1")
            _run(
                _enqueue_delegation(
                    client,
                    insert_run,
                    parent_id,
                    document,
                    root_budget=dict(BUDGET_DOC, max_children=1),
                )
            )
            _wait_until(
                lambda: (_engine_state(client, parent_id) or "") == "failed",
                RUN_TIMEOUT,
                "the classified budget failure",
            )
        finally:
            if worker is not None:
                _stop(worker)
            client.close()

        assert _failure_reasons(delegate_url, parent_id) == ["budget_fanout_exhausted"]
        # Nothing was ever started: no child rows, no reservation.
        assert _read_child_rows(delegate_url, parent_id) == []
        types = [event.type for event in _read_events(delegate_url, parent_id)]
        assert BUDGET_RESERVED_EVENT_TYPE not in types

    def test_budgeted_deadline_releases_the_batch(
        self, delegate_url, factory, insert_run, scenario, composition
    ) -> None:
        """The deadline path's ledger half: the stopped tree's unused
        slots are refunded (no charges — the batch never settled)."""
        composition()
        _write_policy_files(scenario, grants=[{READ_CAP: True}])
        _script = [
            {
                "gate": "gate-model-round-0",
                "tool_call": {
                    "name": "fixture_read",
                    "args": {"marker": "m1"},
                    "id": "c1",
                },
            },
            {"text": "unreached"},
        ]
        (scenario / "model-script.json").write_text(json.dumps(_script), encoding="utf-8")
        parent_id = str(uuid.uuid4())
        document = _delegation_document(
            parent_id,
            [_child_spec("Investigate aspect-alpha.")],
            children_wait_seconds=1.0,
            children_poll_seconds=0.5,
        )
        client = EngineClient(delegate_url)
        worker = None
        try:
            worker = _spawn_worker(delegate_url, scenario / "handlers", scenario, "w1")
            _run(
                _enqueue_delegation(
                    client,
                    insert_run,
                    parent_id,
                    document,
                    root_budget=dict(BUDGET_DOC),
                )
            )
            _wait_until(
                lambda: (_engine_state(client, parent_id) or "") == "failed",
                RUN_TIMEOUT,
                "the honest deadline failure",
            )
            rows = _read_child_rows(delegate_url, parent_id)
            child_id = str(rows[0]["run_id"])
            _wait_until(
                lambda: (_engine_state(client, child_id) or "") == "cancelled",
                RUN_TIMEOUT,
                "the belted child cancellation",
            )
        finally:
            _open_gate(scenario, "gate-model-round-0")
            if worker is not None:
                _stop(worker)
            client.close()

        assert _failure_reasons(delegate_url, parent_id) == [
            delegate.CHILDREN_WAIT_DEADLINE_REASON
        ]
        events = _read_events(delegate_url, parent_id)
        released = [
            event for event in events if event.type == BUDGET_RELEASED_EVENT_TYPE
        ]
        assert len(released) == 1
        assert released[0].data["owner"] == parent_id
        # The deadline path never settles: no charge, and the fold holds
        # no outstanding slots after the refund.
        assert BUDGET_CHARGED_EVENT_TYPE not in [event.type for event in events]
        snapshot = _run(BudgetLedger(factory).asnapshot(parent_id))
        assert snapshot.outstanding == ()
        assert snapshot.consumed_steps == 0
