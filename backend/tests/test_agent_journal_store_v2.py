"""Focused tests for the Runtime v2 objective/journal persistence (journal-store).

Acceptance (rv_journal-store.md; runtime-v2.md §3, §4, §5, §8, §10, §11,
§14 — real disposable PostgreSQL, real Alembic chain, REAL store APIs):

1. **AC1 — objective facts:** the verbatim operator request and operator
   constraints, the explicit operator-owned server scope (initiating
   server + in-scope members), run/child linkage and the bounded budget
   caps persist exactly as projected; model text is never authority
   (constraints are operator-origin only); oversized/secret-shaped input
   is projected or rejected BEFORE PostgreSQL.
2. **AC2 — canonical ledger:** every ``objective.*`` write is
   transactionally consistent with its authoritative transition (a failed
   transition rolls its events back); redaction is explicit (4096/32768
   bounds, secret-shaped values never persist); pagination is
   deterministic (``limit``/``offset``/``total``/``has_more`` with the
   50/200 bounds, same-filter ``COUNT(*)``, and the ``after_seq`` replay
   cursor deduplicates an SSE reconnect).
3. **AC3 — journal derivation:** the journal folds trusted records only
   (actions, receipts, verification, unknowns, children); a malformed
   advisory checklist event CANNOT erase effects or fail the read
   (honest ``malformed`` marker); completion is evidence-bound
   (``completion_missing_evidence`` for unevidenced ``verified`` claims);
   a stale/lagging cache catches up honestly from the ledger.
4. **AC4 — legacy truthfulness:** the legacy read adapter returns the
   legacy conversation/task/run facts verbatim with ``runtime="legacy"``
   and no fabricated v2 linkage; an in-flight (unsettled) legacy run can
   never become v2 (store guard + DB CHECK backstop).
5. **AC5 — scope authority:** scope edits require the exact operator
   action AND the expected revision (CAS); agent/child/model actors are
   rejected; the initiating server is immortal; a child run can never
   bind an out-of-scope server.

Boundary discipline (test policy): every storage claim runs against a
real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``) with the
real linear Alembic chain applied — the transactional claim by a real
rolled-back transaction, the concurrency claim by real concurrent
coroutines on real connections, and the chain claim by a real
downgrade/re-upgrade round trip with seeded legacy AND v2 rows. Fixtures
carry only fixed opaque UUIDs and synthetic text — the "secrets" below
are synthetic markers that merely LOOK secret-shaped.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from mai.agents.activity_store import (
    ObjectiveEventDraft,
    aappend_event,
    aappend_events,
    alatest_seq,
    alist_events,
    achecked_objective,
)
from mai.agents.conversation_store import (
    aattach_run,
    acreate_conversation,
)
from mai.agents.journal_store import (
    aderive_journal,
    ajournal_apply,
    aput_checklist,
    aread_checklist,
    aread_journal,
    async_journal_rebuild,
)
from mai.agents.legacy_view import alegacy_conversation_view
from mai.agents.objective_store import (
    abind_run,
    achange_scope,
    achange_status,
    acomplete_objective,
    acreate_objective,
    aget_objective,
    alist_objective_runs,
    alist_objectives,
)
from mai.agents.output_safety import REDACTED
from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID, SYSTEM_ASSIGNMENT_SOURCE
from mai.contracts.objectives import (
    MAX_PAGE_LIMIT,
    OBJECTIVE_COMPLETED_EVENT_TYPE,
    OBJECTIVE_CREATED_EVENT_TYPE,
    OBJECTIVE_JOURNAL_UPDATED_EVENT_TYPE,
    OBJECTIVE_SCOPE_CHANGED_EVENT_TYPE,
    OBJECTIVE_STATUS_CHANGED_EVENT_TYPE,
    ActorKind,
    ChecklistItem,
    JournalActionApplied,
    JournalChildLinked,
    JournalReceiptRecorded,
    JournalUnknownNoted,
    ObjectiveBudget,
    ObjectiveCompletionVerdict,
    ObjectiveContractError,
    ObjectiveEvent,
    ObjectiveStatus,
    fold_journal,
)
from mai.contracts.tasks import (
    CriterionResult,
    DomainOutcome,
    EffectCertainty,
    EvidenceKind,
    EvidenceRef,
    ExecutionStatus,
    TaskOutcome,
    VerificationStatus,
)
from mai.database import create_app_engine

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

REVISION_IDS = (
    "0001",
    "0002",
    "0003",
    "0004",
    "0005",
    "0006",
    "0007",
    "0008",
    "0009",
    "0010",
    "0011",
    "0012",
    "0013",
)

OBJECTIVE_TABLES = (
    "objective_journal",
    "objective_checklists",
    "objective_events",
    "objective_runs",
    "objective_servers",
    "objectives",
)

# Every app-owned table this suite truncates between tests (complete
# reset list so the suite composes safely with the sibling suites in ONE
# session — children before parents). The seeded tag CATALOG rows (the
# reserved ``unclassified`` tag the 0011 chain seeds) are fixture
# infrastructure, not per-test domain data: associations are removed,
# the catalog stays.
DOMAIN_TABLES = (
    *OBJECTIVE_TABLES,
    "conversation_steering",
    "conversation_runs",
    "conversation_messages",
    "conversations",
    "agent_task_completions",
    "agent_task_revisions",
    "agent_task_evidence",
    "agent_task_criteria",
    "agent_task_constraints",
    "agent_task_steps",
    "agent_tasks",
    "job_resource_locks",
    "job_schedule_fires",
    "job_schedules",
    "job_events",
    "job_steps",
    "job_runs",
    "approvals",
    "job_tags",
    "conversation_tags",
)

# Frozen fixed fixture identities (canonical lowercase UUID strings).
SERVER_A = "a10b8c5e-1d2f-4a3b-9c4d-0e5f6a7b8c9d"
SERVER_B = "b20c9d6f-2e3a-4b4c-8d5e-1f0a9b8c7d6e"
SERVER_C = "c30dae70-3f4b-4c5d-9e6f-2a1b0c9d8e7f"
CONV_A = "d41bf581-4a5c-4d6e-af70-3b2c1d0e9f8a"
LEGACY_RUN = "e52c0692-5b6d-4e7f-b081-4c3d2e1f0a9b"
LEGACY_RUN_2 = "f63d17a3-6c7e-4f80-c192-5d4e3f2a1b0c"
LEGACY_TASK = "071e28b4-7d8f-4a90-b2a3-5e6f7a8b9c0d"

SID_A = uuid.UUID(SERVER_A)
SID_B = uuid.UUID(SERVER_B)
SID_C = uuid.UUID(SERVER_C)

REQUEST_TEXT = "Deploy the staging stack and verify the health endpoint.\n"
CONSTRAINT_TEXT = "Never restart the prod-adjacent load balancer."
SECRET_SHAPED = "password=hunter2\napi_key: sk-abcdef1234567890abcdef12\n"


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _plain_url(url: str) -> str:
    """The same database as a psycopg-connectable URL (the fixture env may
    carry the SQLAlchemy ``+psycopg`` driver dialect)."""
    from sqlalchemy.engine import make_url

    return make_url(url).set(drivername="postgresql").render_as_string(
        hide_password=False
    )


def _drop_all(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state (engine too).

    The full reset includes the seeded tag CATALOG (per-test truncation
    deliberately does not — see DOMAIN_TABLES).
    """
    table_list = ", ".join(
        name
        for name in (
            *DOMAIN_TABLES,
            "tags",
            "profile_projection_journal",
            "profile_projection",
            "admin_sessions",
            "admin_credential",
            "notification_alerts",
            "observation_findings",
            "server_observations",
            "knowledge_doc_chunks",
            "knowledge_documents",
            "application_projection_journal",
            "application_projection",
            "alembic_version",
        )
    )
    with psycopg.connect(_plain_url(url)) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table_list}")
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``0013``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the objective-store claims need a "
            "real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_all(url)
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture
async def sessions(migrated_url: str):
    """NullPool session factory; ONE truncated domain per test."""
    engine, factory = create_app_engine(migrated_url)
    yield factory
    try:
        async with factory() as session:
            present = (
                await session.execute(
                    text(
                        "SELECT c.relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                        "AND c.relname = ANY(:names)"
                    ),
                    {"names": list(DOMAIN_TABLES)},
                )
            ).scalars().all()
            if present:
                await session.execute(
                    text(f"TRUNCATE {', '.join(sorted(present))} CASCADE")
                )
            await session.commit()
    finally:
        await engine.dispose()


async def _fetchone(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).first()


async def _fetchall(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).all()


async def _fetchval(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).scalar()


def _migration_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0013",
        os.path.join(
            BACKEND_ROOT, "migrations", "versions",
            "0013_runtime_v2_journal_store.py",
        ),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


async def _constraint_def(sessions, name: str) -> str | None:
    row = await _fetchone(
        sessions,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = :name",
        name=name,
    )
    return None if row is None else str(row[0])


def _literals_of(constraint: str | None) -> list[str]:
    if constraint is None:
        return []
    return re.findall(r"'([^']*)'", constraint)


def _codes_of(exc: ObjectiveContractError) -> dict[str, str]:
    return exc.fields


async def _seed_job_run(
    sessions,
    run_id: str,
    *,
    server_id: str = SERVER_A,
    parent_run_id: str | None = None,
    depth: int = 0,
) -> None:
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                """
                INSERT INTO job_runs
                    (run_id, server_id, parent_run_id, depth,
                     profile_revision, prompt_revision, model_profile,
                     requested_grants)
                VALUES (CAST(:rid AS uuid), CAST(:sid AS uuid),
                        CAST(:parent AS uuid), :depth, 1, 1, 'fixture',
                        '[]'::jsonb)
                """
            ),
            {
                "rid": run_id,
                "sid": server_id,
                "parent": parent_run_id,
                "depth": depth,
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


async def _make_objective(
    sessions,
    *,
    server_id: uuid.UUID = SID_A,
    scope: tuple[uuid.UUID, ...] = (SID_A,),
    request: str = REQUEST_TEXT,
    constraints: tuple[str, ...] = (CONSTRAINT_TEXT,),
    budget: ObjectiveBudget | None = None,
) -> uuid.UUID:
    detail = await acreate_objective(
        sessions,
        server_id=server_id,
        original_request=request,
        constraints=list(constraints),
        scope_server_ids=list(scope),
        budget=budget,
    )
    return detail.objective_id


# ---------------------------------------------------------------------------
# migration chain (0013) — vocabulary mirror + round trip
# ---------------------------------------------------------------------------


class TestMigrationChain:
    def test_revision_constants_target_the_0012_parent(self) -> None:
        module = _migration_module()
        assert module.revision == "0013"
        assert module.down_revision == "0012"

    def test_head_is_the_registered_tip(self, migrated_url: str) -> None:
        with psycopg.connect(_plain_url(migrated_url)) as conn:
            row = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        assert row == (REVISION_IDS[-1],)

    @pytest.mark.anyio
    async def test_vocabulary_mirrors_the_contract_enums(
        self, sessions
    ) -> None:
        assert _literals_of(
            await _constraint_def(sessions, "ck_objectives_status")
        ) == [status.value for status in ObjectiveStatus]
        for name in (
            "ck_objective_servers_actor",
            "ck_objective_runs_actor",
            "ck_objective_events_actor",
        ):
            assert _literals_of(
                await _constraint_def(sessions, name)
            ) == [kind.value for kind in ActorKind], name
        assert _literals_of(
            await _constraint_def(sessions, "ck_objective_runs_handler_v2")
        ) == ["agent.run_v2"]

    @pytest.mark.anyio
    async def test_event_type_grammar_stays_in_family(
        self, sessions
    ) -> None:
        definition = await _constraint_def(
            sessions, "ck_objective_events_type_family"
        )
        assert definition is not None
        assert "objective.%" in definition
        assert "~" in definition  # dotted-slug grammar regex

    def test_downgrade_round_trip_with_seeded_legacy_and_v2_rows(
        self, migrated_url: str
    ) -> None:
        """The full chain scenario: seed legacy AND v2 rows at head,
        downgrade 0013 -> 0012 (legacy facts intact, v2 tables gone),
        re-upgrade -> 0013 (empty v2 domain, legacy STILL intact)."""

        def _run_upgrade(target: str) -> None:
            previous = os.environ.get("MAI_DATABASE_URL")
            os.environ["MAI_DATABASE_URL"] = migrated_url
            try:
                if target == "head" or int(target) >= 13:
                    command.upgrade(_alembic_config(), target)
                else:
                    command.downgrade(_alembic_config(), target)
            finally:
                if previous is None:
                    os.environ.pop("MAI_DATABASE_URL", None)
                else:
                    os.environ["MAI_DATABASE_URL"] = previous

        _run_upgrade("head")

        async def _seed() -> None:
            engine, factory = create_app_engine(migrated_url)
            try:
                await acreate_conversation(
                    factory, server_id=SID_A, conversation_id=uuid.UUID(CONV_A)
                )
                await _seed_job_run(factory, LEGACY_RUN)
                async with factory() as session, session.begin():
                    await session.execute(
                        text(
                            """
                            INSERT INTO agent_tasks
                                (task_id, conversation_id, server_id,
                                 revision, title, requested_phase, status)
                            VALUES (CAST(:tid AS uuid),
                                    CAST(:cid AS uuid), CAST(:sid AS uuid),
                                    1, 'legacy task', 'plan', 'in_progress')
                            """
                        ),
                        {
                            "tid": LEGACY_TASK,
                            "cid": CONV_A,
                            "sid": SERVER_A,
                        },
                    )
                await acreate_objective(
                    factory,
                    server_id=SID_A,
                    original_request=REQUEST_TEXT,
                    constraints=[CONSTRAINT_TEXT],
                    scope_server_ids=[SID_A],
                )
            finally:
                await engine.dispose()

        asyncio.run(_seed())

        def _facts() -> tuple[object, object, object, object, object]:
            with psycopg.connect(_plain_url(migrated_url)) as conn:
                version = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                legacy_conv = conn.execute(
                    "SELECT count(*) FROM conversations WHERE "
                    "conversation_id = %s::uuid",
                    (CONV_A,),
                ).fetchone()
                legacy_task = conn.execute(
                    "SELECT title, status FROM agent_tasks WHERE "
                    "task_id = %s::uuid",
                    (LEGACY_TASK,),
                ).fetchone()
                legacy_run = conn.execute(
                    "SELECT count(*) FROM job_runs WHERE run_id = %s::uuid",
                    (LEGACY_RUN,),
                ).fetchone()
                v2_objectives = conn.execute(
                    "SELECT count(*) FROM objectives"
                ).fetchone()
            return version, legacy_conv, legacy_task, legacy_run, v2_objectives

        version, legacy_conv, legacy_task, legacy_run, v2 = _facts()
        assert version == ("0013",)
        assert legacy_conv == (1,)
        assert legacy_task == ("legacy task", "in_progress")
        assert legacy_run == (1,)
        assert v2 == (1,)

        _run_upgrade("0012")

        with psycopg.connect(_plain_url(migrated_url)) as conn:
            version = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
            present = conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name = ANY(%(names)s)",
                {"names": list(OBJECTIVE_TABLES)},
            ).fetchone()
            legacy_conv = conn.execute(
                "SELECT count(*) FROM conversations WHERE "
                "conversation_id = %s::uuid",
                (CONV_A,),
            ).fetchone()
            legacy_task = conn.execute(
                "SELECT title, status FROM agent_tasks WHERE "
                "task_id = %s::uuid",
                (LEGACY_TASK,),
            ).fetchone()
            legacy_run = conn.execute(
                "SELECT count(*) FROM job_runs WHERE run_id = %s::uuid",
                (LEGACY_RUN,),
            ).fetchone()
        assert version == ("0012",)
        assert present == (0,), "downgrade must drop exactly the v2 tables"
        assert legacy_conv == (1,)
        assert legacy_task == ("legacy task", "in_progress")
        assert legacy_run == (1,)

        _run_upgrade("head")

        with psycopg.connect(_plain_url(migrated_url)) as conn:
            version = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
            v2_objectives = conn.execute(
                "SELECT count(*) FROM objectives"
            ).fetchone()
            legacy_conv = conn.execute(
                "SELECT count(*) FROM conversations WHERE "
                "conversation_id = %s::uuid",
                (CONV_A,),
            ).fetchone()
        assert version == ("0013",)
        assert v2_objectives == (0,), "re-upgrade starts an EMPTY v2 domain"
        assert legacy_conv == (1,), "legacy facts survive the round trip"


# ---------------------------------------------------------------------------
# AC1 — creation facts, bounds, projection
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_creation_persists_verbatim_facts_scope_budget_events(
    sessions,
) -> None:
    budget = ObjectiveBudget(max_active_runs=4, max_total_steps=100)
    detail = await acreate_objective(
        sessions,
        server_id=SID_A,
        original_request=REQUEST_TEXT,
        constraints=[CONSTRAINT_TEXT, "Read-only before any mutation."],
        scope_server_ids=[SID_A, SID_B],
        title="staging deploy",
        budget=budget,
    )
    assert detail.status is ObjectiveStatus.ACTIVE
    assert detail.revision == 1
    assert detail.original_request == REQUEST_TEXT
    assert detail.constraints == [
        CONSTRAINT_TEXT,
        "Read-only before any mutation.",
    ]
    assert detail.budget == budget
    assert {member.server_id for member in detail.scope} == {SID_A, SID_B}
    home = [m for m in detail.scope if m.server_id == SID_A][0]
    assert home.added_by_actor_kind is ActorKind.OPERATOR

    # The created event is ledger seq 1, operator-provenance, value-free.
    page = await alist_events(
        sessions, detail.objective_id, server_id=SID_A, type=OBJECTIVE_CREATED_EVENT_TYPE
    )
    assert page.total == 1 and len(page.items) == 1
    created = page.items[0]
    assert created.sequence == 1
    assert created.actor_kind is ActorKind.OPERATOR
    assert created.data["scope_server_ids"] == sorted([str(SID_A), str(SID_B)])
    assert created.data["constraint_count"] == 2
    assert REQUEST_TEXT not in str(created.data)  # value-free: no prose

    # The journal cache seeds itself from the ledger (fold through seq 1).
    row = await _fetchone(
        sessions,
        "SELECT derived_seq FROM objective_journal WHERE objective_id = :oid",
        oid=detail.objective_id,
    )
    assert row is not None and int(row[0]) == 1


@pytest.mark.anyio
async def test_creation_bounds_fail_closed(sessions) -> None:
    with pytest.raises(ValueError, match="scope"):
        await acreate_objective(
            sessions,
            server_id=SID_A,
            original_request=REQUEST_TEXT,
            scope_server_ids=[],
        )
    with pytest.raises(ObjectiveContractError) as excinfo:
        await acreate_objective(
            sessions,
            server_id=SID_B,
            original_request=REQUEST_TEXT,
            scope_server_ids=[SID_A],  # home not a member
        )
    assert _codes_of(excinfo.value)["scope_server_ids"] == "scope_violation"
    with pytest.raises(ObjectiveContractError) as excinfo:
        await acreate_objective(
            sessions,
            server_id=SID_A,
            original_request=REQUEST_TEXT,
            scope_server_ids=[SID_A],
            constraints=[f"constraint {i}" for i in range(13)],
        )
    assert _codes_of(excinfo.value)["constraints"] == "invalid_payload"
    with pytest.raises(ObjectiveContractError) as excinfo:
        await acreate_objective(
            sessions,
            server_id=SID_A,
            original_request=REQUEST_TEXT,
            scope_server_ids=[
                SID_A,
                *(
                    uuid.uuid5(uuid.NAMESPACE_DNS, f"scope-server-{i}")
                    for i in range(16)
                ),
            ],
        )
    assert _codes_of(excinfo.value)["scope_server_ids"] == "scope_violation"
    with pytest.raises(ValueError, match="non-blank"):
        await acreate_objective(
            sessions,
            server_id=SID_A,
            original_request="   ",
            scope_server_ids=[SID_A],
        )


@pytest.mark.anyio
async def test_creation_projects_secrets_before_postgres(sessions) -> None:
    detail = await acreate_objective(
        sessions,
        server_id=SID_A,
        original_request=f"Rotate credentials.\n{SECRET_SHAPED}",
        scope_server_ids=[SID_A],
    )
    stored = await _fetchval(
        sessions,
        "SELECT original_request FROM objectives WHERE objective_id = :oid",
        oid=detail.objective_id,
    )
    assert isinstance(stored, str)
    assert "hunter2" not in stored
    assert "sk-abcdef1234567890" not in stored
    assert REDACTED in stored  # the honest redaction marker


@pytest.mark.anyio
async def test_creation_dedupes_same_client_request_and_conflicts_changed(
    sessions,
) -> None:
    crid = uuid.uuid4()
    first = await acreate_objective(
        sessions,
        server_id=SID_A,
        original_request=REQUEST_TEXT,
        scope_server_ids=[SID_A],
        client_request_id=crid,
    )
    again = await acreate_objective(
        sessions,
        server_id=SID_A,
        original_request=REQUEST_TEXT,
        scope_server_ids=[SID_A],
        client_request_id=crid,
    )
    assert again.objective_id == first.objective_id
    assert (
        await _fetchval(sessions, "SELECT count(*) FROM objectives") == 1
    )
    with pytest.raises(ObjectiveContractError) as excinfo:
        await acreate_objective(
            sessions,
            server_id=SID_A,
            original_request="A different episode entirely.",
            scope_server_ids=[SID_A],
            client_request_id=crid,
        )
    assert (
        _codes_of(excinfo.value)["client_request_id"]
        == "client_request_conflict"
    )


@pytest.mark.anyio
async def test_listing_is_bounded_and_deterministic(sessions) -> None:
    for index in range(4):
        await acreate_objective(
            sessions,
            server_id=SID_A,
            original_request=f"objective {index}",
            scope_server_ids=[SID_A, SID_B],
        )
    page = await alist_objectives(sessions, server_id=SID_B, limit=3)
    assert page.total == 4
    assert len(page.items) == 3
    assert page.has_more is True
    rest = await alist_objectives(sessions, server_id=SID_B, limit=3, offset=3)
    assert len(rest.items) == 1
    assert rest.has_more is False
    marked = {item.objective_id for item in (*page.items, *rest.items)}
    assert len(marked) == 4
    # A server outside every scope sees nothing (and no error).
    empty = await alist_objectives(sessions, server_id=SID_C)
    assert empty.total == 0 and empty.items == []


# ---------------------------------------------------------------------------
# AC2 — canonical ledger: transactional consistency, redaction, pagination
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_scope_change_is_transactional_with_its_event(sessions) -> None:
    oid = await _make_objective(sessions, scope=(SID_A,))
    before = await aget_objective(sessions, oid, server_id=SID_A)
    detail = await achange_scope(
        sessions,
        oid,
        server_id=SID_A,
        expected_revision=before.revision,
        add_server_ids=[SID_B],
        actor_kind=ActorKind.OPERATOR,
    )
    assert detail.revision == before.revision + 1
    assert {m.server_id for m in detail.scope} == {SID_A, SID_B}
    page = await alist_events(
        sessions, oid, server_id=SID_A, type=OBJECTIVE_SCOPE_CHANGED_EVENT_TYPE
    )
    assert page.total == 1
    event = page.items[0]
    assert event.sequence == 2  # after objective.created
    assert event.data == {
        "added_server_ids": [str(SID_B)],
        "removed_server_ids": [],
        "revision": 2,
    }

    # The membership table IS the scope; the ledger IS the history.
    members = await _fetchall(
        sessions,
        "SELECT server_id FROM objective_servers WHERE objective_id = :oid "
        "ORDER BY server_id",
        oid=oid,
    )
    assert [str(row[0]) for row in members] == sorted([str(SID_A), str(SID_B)])


@pytest.mark.anyio
async def test_failed_transition_rolls_its_events_back(sessions) -> None:
    oid = await _make_objective(sessions, scope=(SID_A,))
    draft = ObjectiveEventDraft(
        type=OBJECTIVE_STATUS_CHANGED_EVENT_TYPE,
        actor_kind=ActorKind.OPERATOR,
        data={"from_status": "active", "to_status": "failed", "revision": 2},
    )
    with pytest.raises(RuntimeError):
        async with sessions() as session:
            async with session.begin():
                await achecked_objective(
                    session, oid, server_id=SID_A, for_update=True
                )
                await aappend_events(session, oid, [draft])
                raise RuntimeError("crash between event and commit")
    count = await _fetchval(
        sessions,
        "SELECT count(*) FROM objective_events WHERE objective_id = :oid",
        oid=oid,
    )
    assert count == 1  # only objective.created survived the rollback


@pytest.mark.anyio
async def test_scope_failure_rolls_the_whole_transition_back(
    sessions, monkeypatch
) -> None:
    """The store-level AC2 proof: a ledger failure mid-transition rolls
    back the authoritative state change too (never one without the
    other)."""
    from mai.agents import objective_store as store_module

    oid = await _make_objective(sessions, scope=(SID_A,))
    before = await aget_objective(sessions, oid, server_id=SID_A)

    async def _boom(*args: object, **kwargs: object) -> list[int]:
        raise RuntimeError("ledger write fails")

    monkeypatch.setattr(store_module, "aappend_events", _boom)
    with pytest.raises(RuntimeError):
        await achange_scope(
            sessions,
            oid,
            server_id=SID_A,
            expected_revision=before.revision,
            add_server_ids=[SID_B],
            actor_kind=ActorKind.OPERATOR,
        )
    monkeypatch.undo()
    after = await aget_objective(sessions, oid, server_id=SID_A)
    assert after.revision == before.revision
    assert {m.server_id for m in after.scope} == {SID_A}
    total = await _fetchval(
        sessions,
        "SELECT count(*) FROM objective_events WHERE objective_id = :oid",
        oid=oid,
    )
    assert total == 1


@pytest.mark.anyio
async def test_concurrent_event_appends_serialize_under_the_row_lock(
    sessions,
) -> None:
    oid = await _make_objective(sessions, scope=(SID_A,))

    async def _append(index: int) -> None:
        await aappend_event(
            sessions,
            oid,
            server_id=SID_A,
            draft=ObjectiveEventDraft(
                type=OBJECTIVE_STATUS_CHANGED_EVENT_TYPE,
                actor_kind=ActorKind.AUTOMATION,
                data={
                    "from_status": "active",
                    "to_status": "failed",
                    "revision": index,
                },
            ),
        )

    await asyncio.gather(*(_append(i) for i in range(8)))
    rows = await _fetchall(
        sessions,
        "SELECT seq FROM objective_events WHERE objective_id = :oid "
        "ORDER BY seq",
        oid=oid,
    )
    assert [int(row[0]) for row in rows] == list(range(1, 10))  # dense, unique


@pytest.mark.anyio
async def test_event_redaction_and_bounds(sessions) -> None:
    oid = await _make_objective(sessions, scope=(SID_A,))

    # Secret-shaped values are projected BEFORE persistence.
    await aappend_event(
        sessions,
        oid,
        server_id=SID_A,
        draft=ObjectiveEventDraft(
            type=OBJECTIVE_JOURNAL_UPDATED_EVENT_TYPE,
            actor_kind=ActorKind.AGENT,
            data={
                "kind": "unknown",
                "unknown": {
                    "effect_ref": "op-1",
                    "detail": f"stdout: {SECRET_SHAPED}",
                },
            },
        ),
    )
    raw = await _fetchval(
        sessions,
        "SELECT data::text FROM objective_events WHERE objective_id = :oid "
        "AND seq = 2",
        oid=oid,
    )
    assert isinstance(raw, str)
    assert "hunter2" not in raw
    assert "sk-abcdef1234567890" not in raw
    assert REDACTED in raw

    # Per-text bound: an oversized string is cut AFTER redaction.
    await aappend_event(
        sessions,
        oid,
        server_id=SID_A,
        draft=ObjectiveEventDraft(
            type=OBJECTIVE_JOURNAL_UPDATED_EVENT_TYPE,
            actor_kind=ActorKind.AGENT,
            data={
                "kind": "unknown",
                "unknown": {"effect_ref": "op-2", "detail": "x" * 9000},
            },
        ),
    )
    raw = await _fetchval(
        sessions,
        "SELECT data->'unknown'->>'detail' FROM objective_events "
        "WHERE objective_id = :oid AND seq = 3",
        oid=oid,
    )
    assert isinstance(raw, str) and len(raw) == 4096

    # Whole-payload bound: oversized STRUCTURED payloads fail closed
    # (a silent cut would corrupt a typed payload).
    with pytest.raises(ObjectiveContractError) as excinfo:
        await aappend_event(
            sessions,
            oid,
            server_id=SID_A,
            draft=ObjectiveEventDraft(
                type=OBJECTIVE_JOURNAL_UPDATED_EVENT_TYPE,
                actor_kind=ActorKind.AGENT,
                data={
                    "kind": "unknown",
                    "unknown": {
                        "effect_ref": "op-3",
                        "detail": "y" * 40,
                    },
                    "pad": ["z" * 9000] * 10,
                },
            ),
        )
    assert _codes_of(excinfo.value)["data"] == "event_too_large"
    total = await _fetchval(
        sessions,
        "SELECT count(*) FROM objective_events WHERE objective_id = :oid",
        oid=oid,
    )
    assert total == 3  # the rejected append wrote nothing


@pytest.mark.anyio
async def test_ledger_pagination_and_reconnect_cursor(sessions) -> None:
    oid = await _make_objective(sessions, scope=(SID_A,))
    for index in range(55):
        await aappend_event(
            sessions,
            oid,
            server_id=SID_A,
            draft=ObjectiveEventDraft(
                type=OBJECTIVE_JOURNAL_UPDATED_EVENT_TYPE,
                actor_kind=ActorKind.AGENT,
                data={
                    "kind": "action",
                    "action": {
                        "action_id": str(uuid.uuid4()),
                        "capability_id": "server.read",
                        "server_id": str(SID_A),
                        "decision": "allowed",
                    },
                },
            ),
        )
    # Default page (50) with derived has_more.
    page = await alist_events(sessions, oid, server_id=SID_A)
    assert page.limit == 50 and page.total == 56
    assert len(page.items) == 50 and page.has_more is True
    assert [event.sequence for event in page.items] == list(range(1, 51))
    # Offset page completes the collection deterministically.
    rest = await alist_events(sessions, oid, server_id=SID_A, offset=50)
    assert [event.sequence for event in rest.items] == list(range(51, 57))
    assert rest.has_more is False
    # Same-filter COUNT under a type filter.
    filtered = await alist_events(
        sessions,
        oid,
        server_id=SID_A,
        type=OBJECTIVE_JOURNAL_UPDATED_EVENT_TYPE,
        limit=10,
    )
    assert filtered.total == 55 and len(filtered.items) == 10
    # SSE-reconnect cursor: strictly-newer events only, no duplicates.
    cursor = await alatest_seq(sessions, oid, server_id=SID_A)
    assert cursor == 56
    reconnect = await alist_events(sessions, oid, server_id=SID_A, after_seq=cursor)
    assert reconnect.items == [] and reconnect.total == 0
    one_more = await aappend_event(
        sessions,
        oid,
        server_id=SID_A,
        draft=ObjectiveEventDraft(
            type=OBJECTIVE_STATUS_CHANGED_EVENT_TYPE,
            actor_kind=ActorKind.OPERATOR,
            data={"from_status": "active", "to_status": "failed", "revision": 2},
        ),
    )
    after = await alist_events(
        sessions, oid, server_id=SID_A, after_seq=cursor
    )
    assert [event.sequence for event in after.items] == [one_more.sequence]
    # Page bounds fail closed, never clamp.
    for bad in ({"limit": 0}, {"limit": 201}, {"limit": True}):
        with pytest.raises(ValueError, match="limit"):
            await alist_events(sessions, oid, server_id=SID_A, **bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="offset"):
        await alist_events(sessions, oid, server_id=SID_A, offset=-1)


# ---------------------------------------------------------------------------
# AC3 — journal derivation, malformed checklist, evidence-bound completion
# ---------------------------------------------------------------------------


def _verdict(
    oid: uuid.UUID,
    revision: int,
    *,
    outcome: TaskOutcome = TaskOutcome.VERIFIED,
    with_evidence: bool = True,
    idempotency_key: uuid.UUID | None = None,
) -> ObjectiveCompletionVerdict:
    evidence = (
        [
            EvidenceRef(
                kind=EvidenceKind.RUN_EVENT,
                evidence_id=uuid.uuid4(),
                note="job_events:1",
            )
        ]
        if with_evidence
        else []
    )
    return ObjectiveCompletionVerdict(
        objective_id=oid,
        base_revision=revision,
        idempotency_key=idempotency_key or uuid.uuid4(),
        task_outcome=outcome,
        execution_status=ExecutionStatus.SUCCEEDED,
        domain_outcome=DomainOutcome.UNKNOWN,  # honest for shell work
        effect_certainty=EffectCertainty.CERTAIN,
        evidence_refs=evidence,
        criterion_results=(
            [
                CriterionResult(
                    criterion_id="health_endpoint",
                    result=VerificationStatus.PASSED,
                    evidence_refs=evidence,
                )
            ]
            if with_evidence
            else []
        ),
    )


@pytest.mark.anyio
async def test_journal_folds_trusted_records_and_matches_derivation(
    sessions,
) -> None:
    oid = await _make_objective(sessions, scope=(SID_A,))
    action = JournalActionApplied(
        action_id=uuid.uuid4(),
        capability_id="server.exec",
        server_id=SID_A,
        decision="allowed",
        operation_id="op-1",
        detail=f"docker compose up -d\n{SECRET_SHAPED}",
    )
    state = await ajournal_apply(
        sessions,
        oid,
        server_id=SID_A,
        mutation=action,
        actor_kind=ActorKind.AGENT,
    )
    receipt = JournalReceiptRecorded(
        operation_id="op-1",
        outcome="succeeded",
        effect_class="mutating",
        server_id=SID_A,
    )
    state = await ajournal_apply(
        sessions, oid, server_id=SID_A, mutation=receipt, actor_kind=ActorKind.AGENT
    )
    state = await ajournal_apply(
        sessions,
        oid,
        server_id=SID_A,
        mutation=JournalUnknownNoted(effect_ref="op-9", server_id=None),
        actor_kind=ActorKind.AUTOMATION,
    )
    assert [a.action_id for a in state.actions] == [action.action_id]
    assert [r.operation_id for r in state.receipts] == ["op-1"]
    assert [u.effect_ref for u in state.unknowns] == ["op-9"]
    # The sanitized action detail was projected at the journal boundary.
    assert state.actions[0].detail is not None
    assert "hunter2" not in state.actions[0].detail

    derived = await aderive_journal(sessions, oid, server_id=SID_A)
    assert derived == state
    cached = await aread_journal(sessions, oid, server_id=SID_A)
    assert cached == derived
    # Every journal mutation appended exactly one ledger event.
    journal_events = await alist_events(
        sessions,
        oid,
        server_id=SID_A,
        type=OBJECTIVE_JOURNAL_UPDATED_EVENT_TYPE,
        limit=MAX_PAGE_LIMIT,
    )
    assert journal_events.total == 3


@pytest.mark.anyio
async def test_malformed_checklist_event_cannot_erase_effects(sessions) -> None:
    oid = await _make_objective(sessions, scope=(SID_A,))
    await aput_checklist(
        sessions,
        oid,
        server_id=SID_A,
        items=[ChecklistItem(text="deploy stack"), ChecklistItem(text="verify")],
        expected_revision=0,
        actor_kind=ActorKind.AGENT,
    )
    state = await ajournal_apply(
        sessions,
        oid,
        server_id=SID_A,
        mutation=JournalReceiptRecorded(
            operation_id="op-1",
            outcome="succeeded",
            effect_class="mutating",
            server_id=SID_A,
        ),
        actor_kind=ActorKind.AGENT,
    )
    assert len(state.receipts) == 1

    # A malformed advisory checklist event lands in the ledger (simulating
    # an out-of-band or future-version writer).
    await aappend_event(
        sessions,
        oid,
        server_id=SID_A,
        draft=ObjectiveEventDraft(
            type="objective.checklist_updated",
            actor_kind=ActorKind.AGENT,
            data={"items": "not-a-list", "checklist_revision": 9},
        ),
    )
    derived = await aderive_journal(sessions, oid, server_id=SID_A)
    assert derived.checklist.malformed is True
    assert derived.checklist.detail is not None
    assert [item.text for item in derived.checklist.items] == [
        "deploy stack",
        "verify",
    ], "the last valid advisory items stay visible"
    assert len(derived.receipts) == 1, "effects are never erased"
    # The fold is pure: the same replay yields the same document.
    replay = fold_journal(None, (await _all_events(sessions, oid))).journal
    assert replay == derived
    # And the read path (with cache catch-up) agrees.
    assert await aread_journal(sessions, oid, server_id=SID_A) == derived


async def _all_events(sessions, oid: uuid.UUID) -> list[ObjectiveEvent]:
    events: list[ObjectiveEvent] = []
    offset = 0
    while True:
        page = await alist_events(
            sessions, oid, server_id=SID_A, limit=MAX_PAGE_LIMIT, offset=offset
        )
        events.extend(page.items)
        if not page.has_more:
            return events
        offset += len(page.items)


@pytest.mark.anyio
async def test_checklist_put_is_revision_cas_and_bounded(sessions) -> None:
    oid = await _make_objective(sessions, scope=(SID_A,))
    first = await aput_checklist(
        sessions,
        oid,
        server_id=SID_A,
        items=[{"text": "deploy stack"}],
        expected_revision=0,
        actor_kind=ActorKind.AGENT,
    )
    assert first.revision == 1
    assert first.items[0].done is False
    with pytest.raises(ObjectiveContractError) as excinfo:
        await aput_checklist(
            sessions,
            oid,
            server_id=SID_A,
            items=[{"text": "stale write"}],
            expected_revision=0,
            actor_kind=ActorKind.AGENT,
        )
    assert _codes_of(excinfo.value)["expected_revision"] == "revision_conflict"
    second = await aput_checklist(
        sessions,
        oid,
        server_id=SID_A,
        items=[{"text": "deploy stack", "done": True}],
        expected_revision=1,
        actor_kind=ActorKind.AGENT,
    )
    assert second.revision == 2
    # Malformed writes are bookkeeping trouble at the boundary: rejected
    # BEFORE persistence, nothing changes.
    with pytest.raises(ValueError, match="bounded at 50"):
        await aput_checklist(
            sessions,
            oid,
            server_id=SID_A,
            items=[{"text": f"item {i}"} for i in range(51)],
            expected_revision=2,
            actor_kind=ActorKind.AGENT,
        )
    with pytest.raises(ValueError, match="non-blank"):
        await aput_checklist(
            sessions,
            oid,
            server_id=SID_A,
            items=[{"text": "   "}],
            expected_revision=2,
            actor_kind=ActorKind.AGENT,
        )
    unchanged = await aread_checklist(sessions, oid, server_id=SID_A)
    assert unchanged is not None and unchanged.revision == 2


@pytest.mark.anyio
async def test_stale_cache_catches_up_honestly(sessions) -> None:
    """Crash boundary: a projection write that never happened (or an
    out-of-band ledger write) leaves the cache behind; reads catch up
    from the ledger instead of showing stale truth."""
    oid = await _make_objective(sessions, scope=(SID_A,))
    await ajournal_apply(
        sessions,
        oid,
        server_id=SID_A,
        mutation=JournalActionApplied(
            action_id=uuid.uuid4(),
            capability_id="server.read",
            server_id=SID_A,
            decision="allowed",
        ),
        actor_kind=ActorKind.AGENT,
    )
    # Roll the cache back to the creation cursor (simulated crash).
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE objective_journal SET derived_seq = 1, "
                "projection = :proj WHERE objective_id = :oid"
            ),
            {
                "oid": oid,
                "proj": (
                    '{"schema_version":1,'
                    + f'"objective_id":"{oid}",'
                    + '"derived_seq":1}'
                ),
            },
        )
    state = await aread_journal(sessions, oid, server_id=SID_A)
    assert state.derived_seq == 2
    assert len(state.actions) == 1
    rebuilt = await async_journal_rebuild(sessions, oid, server_id=SID_A)
    assert rebuilt == state
    derived = await aderive_journal(sessions, oid, server_id=SID_A)
    assert derived == rebuilt


@pytest.mark.anyio
async def test_completion_is_evidence_bound_and_terminal(sessions) -> None:
    oid = await _make_objective(sessions, scope=(SID_A,))
    detail = await aget_objective(sessions, oid, server_id=SID_A)

    # An unevidenced `verified` claim is the honest structured rejection.
    with pytest.raises(ObjectiveContractError) as excinfo:
        await acomplete_objective(
            sessions,
            oid,
            server_id=SID_A,
            expected_revision=detail.revision,
            verdict=_verdict(oid, detail.revision, with_evidence=False),
            actor_kind=ActorKind.AGENT,
        )
    assert (
        _codes_of(excinfo.value)["criterion_results"]
        == "completion_missing_evidence"
    )
    still_active = await aget_objective(sessions, oid, server_id=SID_A)
    assert still_active.status is ObjectiveStatus.ACTIVE
    assert still_active.revision == detail.revision

    # The evidenced verdict settles the episode with its event.
    verdict = _verdict(oid, detail.revision)
    settled = await acomplete_objective(
        sessions,
        oid,
        server_id=SID_A,
        expected_revision=detail.revision,
        verdict=verdict,
        actor_kind=ActorKind.AGENT,
    )
    assert settled.status is ObjectiveStatus.COMPLETED
    assert settled.revision == detail.revision + 1
    completed = await alist_events(
        sessions, oid, server_id=SID_A, type=OBJECTIVE_COMPLETED_EVENT_TYPE
    )
    assert completed.total == 1
    assert completed.items[0].data["missing_evidence"] is False
    assert completed.items[0].data["domain_outcome"] == "unknown"
    journal = await aread_journal(sessions, oid, server_id=SID_A)
    assert journal.status is ObjectiveStatus.COMPLETED
    assert journal.verification is not None
    assert journal.verification.missing_evidence is False

    # Retransmission of the SAME verdict key deduplicates onto the
    # settled episode (no second event, no state change); a DIFFERENT
    # completion attempt is the structured rejection.
    retransmitted = await acomplete_objective(
        sessions,
        oid,
        server_id=SID_A,
        expected_revision=settled.revision,
        verdict=verdict,
        actor_kind=ActorKind.AGENT,
    )
    assert retransmitted.status is ObjectiveStatus.COMPLETED
    assert retransmitted.revision == settled.revision
    assert (
        await alist_events(
            sessions, oid, server_id=SID_A, type=OBJECTIVE_COMPLETED_EVENT_TYPE
        )
    ).total == 1
    first = await aget_objective(sessions, oid, server_id=SID_A)
    assert first.status is ObjectiveStatus.COMPLETED
    with pytest.raises(ObjectiveContractError) as excinfo:
        await acomplete_objective(
            sessions,
            oid,
            server_id=SID_A,
            expected_revision=first.revision,
            verdict=_verdict(oid, first.revision),  # a different key
            actor_kind=ActorKind.AGENT,
        )
    assert (
        _codes_of(excinfo.value)["to_status"] == "invalid_status_transition"
    )
    # Terminal states are frozen on every path.
    with pytest.raises(ObjectiveContractError):
        await achange_status(
            sessions,
            oid,
            server_id=SID_A,
            expected_revision=first.revision,
            to_status=ObjectiveStatus.FAILED,
            actor_kind=ActorKind.OPERATOR,
        )


@pytest.mark.anyio
async def test_status_change_records_transition_and_requires_cas(
    sessions,
) -> None:
    oid = await _make_objective(sessions, scope=(SID_A,))
    detail = await aget_objective(sessions, oid, server_id=SID_A)
    with pytest.raises(ObjectiveContractError) as excinfo:
        await achange_status(
            sessions,
            oid,
            server_id=SID_A,
            expected_revision=detail.revision + 5,
            to_status=ObjectiveStatus.FAILED,
            actor_kind=ActorKind.OPERATOR,
        )
    assert _codes_of(excinfo.value)["expected_revision"] == "revision_conflict"
    changed = await achange_status(
        sessions,
        oid,
        server_id=SID_A,
        expected_revision=detail.revision,
        to_status=ObjectiveStatus.FAILED,
        actor_kind=ActorKind.OPERATOR,
    )
    assert changed.status is ObjectiveStatus.FAILED
    page = await alist_events(
        sessions, oid, server_id=SID_A, type=OBJECTIVE_STATUS_CHANGED_EVENT_TYPE
    )
    assert page.items[-1].data == {
        "from_status": "active",
        "to_status": "failed",
        "revision": 2,
    }
    journal = await aread_journal(sessions, oid, server_id=SID_A)
    assert journal.status is ObjectiveStatus.FAILED


# ---------------------------------------------------------------------------
# AC4 — legacy read adapter is truthful; legacy runs never become v2
# ---------------------------------------------------------------------------


async def _seed_legacy_conversation(sessions) -> uuid.UUID:
    conversation = await acreate_conversation(
        sessions,
        server_id=SID_A,
        conversation_id=uuid.UUID(CONV_A),
        title="legacy episode",
    )
    await _seed_job_run(sessions, LEGACY_RUN)
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                """
                INSERT INTO agent_tasks
                    (task_id, conversation_id, server_id, revision, title,
                     requested_phase, status)
                VALUES (CAST(:tid AS uuid), CAST(:cid AS uuid),
                        CAST(:sid AS uuid), 3, 'legacy task', 'act',
                        'in_progress')
                """
            ),
            {"tid": LEGACY_TASK, "cid": CONV_A, "sid": SERVER_A},
        )
    await aattach_run(
        sessions,
        conversation_id=conversation.conversation_id,
        server_id=SID_A,
        run_id=uuid.UUID(LEGACY_RUN),
        task_id=uuid.UUID(LEGACY_TASK),
    )
    return conversation.conversation_id


@pytest.mark.anyio
async def test_legacy_view_returns_facts_verbatim_and_no_v2(sessions) -> None:
    conversation_id = await _seed_legacy_conversation(sessions)
    view = await alegacy_conversation_view(
        sessions, conversation_id, server_id=SID_A
    )
    assert view.runtime == "legacy"
    assert view.conversation.conversation_id == conversation_id
    assert view.conversation.server_id == SID_A
    assert view.conversation.history_completeness.value == "complete"
    assert view.conversation.title == "legacy episode"
    assert view.task is not None
    assert view.task.task_id == uuid.UUID(LEGACY_TASK)
    assert view.task.status == "in_progress"
    assert view.task.requested_phase == "act"
    assert view.task.revision == 3
    assert len(view.runs) == 1
    assert view.runs[0].run_id == uuid.UUID(LEGACY_RUN)
    assert view.runs[0].lease_holding is True  # unsettled: lease still held
    assert view.objective_ids == (), "no fabricated v2 linkage"
    # Cross-scope reads fail closed like the legacy surface.
    from mai.agents.conversation_store import ConversationContractError

    with pytest.raises(ConversationContractError):
        await alegacy_conversation_view(
            sessions, conversation_id, server_id=SID_B
        )


@pytest.mark.anyio
async def test_no_in_flight_legacy_run_becomes_v2(sessions) -> None:
    conversation_id = await _seed_legacy_conversation(sessions)
    view = await alegacy_conversation_view(
        sessions, conversation_id, server_id=SID_A
    )
    assert view.runs[0].lease_holding is True  # the run is IN FLIGHT
    oid = await _make_objective(sessions, scope=(SID_A,))

    # The only binding seam rejects the legacy handler identity.
    with pytest.raises(ObjectiveContractError) as excinfo:
        await abind_run(
            sessions,
            oid,
            server_id=SID_A,
            run_id=uuid.UUID(LEGACY_RUN),
            initiating_server_id=SID_A,
            actor_kind=ActorKind.AGENT,
            handler="agent.run",
        )
    assert (
        _codes_of(excinfo.value)["handler"] == "legacy_run_not_convertible"
    )
    # The DB CHECK is the durable backstop against any writer.
    with pytest.raises(IntegrityError):
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO objective_runs
                        (objective_id, run_id, initiating_server_id,
                         handler, actor_kind)
                    VALUES (CAST(:oid AS uuid), CAST(:rid AS uuid),
                            CAST(:sid AS uuid), 'agent.run', 'agent')
                    """
                ),
                {"oid": oid, "rid": LEGACY_RUN, "sid": SERVER_A},
            )
    assert await _legacy_run_unbound(sessions, LEGACY_RUN)


async def _legacy_run_unbound(sessions, run_id: str) -> bool:
    count = await _fetchval(
        sessions,
        "SELECT count(*) FROM objective_runs WHERE run_id = :rid",
        rid=run_id,
    )
    return count == 0


# ---------------------------------------------------------------------------
# AC5 — scope authority: operator-only, revision CAS, child containment
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_scope_change_requires_operator_and_exact_revision(
    sessions,
) -> None:
    oid = await _make_objective(sessions, scope=(SID_A,))
    detail = await aget_objective(sessions, oid, server_id=SID_A)
    for actor in (ActorKind.AGENT, ActorKind.CHILD_AGENT, ActorKind.AUTOMATION):
        with pytest.raises(ObjectiveContractError) as excinfo:
            await achange_scope(
                sessions,
                oid,
                server_id=SID_A,
                expected_revision=detail.revision,
                add_server_ids=[SID_B],
                actor_kind=actor,
            )
        assert _codes_of(excinfo.value)["actor_kind"] == "operator_required"
    with pytest.raises(ObjectiveContractError) as excinfo:
        await achange_scope(
            sessions,
            oid,
            server_id=SID_A,
            expected_revision=detail.revision + 1,  # stale
            add_server_ids=[SID_B],
            actor_kind=ActorKind.OPERATOR,
        )
    assert _codes_of(excinfo.value)["expected_revision"] == "revision_conflict"
    after = await aget_objective(sessions, oid, server_id=SID_A)
    assert after.revision == detail.revision
    assert {m.server_id for m in after.scope} == {SID_A}
    total = await _fetchval(
        sessions,
        "SELECT count(*) FROM objective_events WHERE objective_id = :oid",
        oid=oid,
    )
    assert total == 1  # rejected edits write NOTHING


@pytest.mark.anyio
async def test_scope_change_guardrails(sessions) -> None:
    oid = await _make_objective(sessions, scope=(SID_A, SID_B))
    detail = await aget_objective(sessions, oid, server_id=SID_A)
    # The initiating server is immortal.
    with pytest.raises(ObjectiveContractError) as excinfo:
        await achange_scope(
            sessions,
            oid,
            server_id=SID_A,
            expected_revision=detail.revision,
            remove_server_ids=[SID_A],
            actor_kind=ActorKind.OPERATOR,
        )
    assert _codes_of(excinfo.value)["remove_server_ids"] == "scope_violation"
    # Add and remove the same server in one call is invalid.
    with pytest.raises(ObjectiveContractError) as excinfo:
        await achange_scope(
            sessions,
            oid,
            server_id=SID_A,
            expected_revision=detail.revision,
            add_server_ids=[SID_C],
            remove_server_ids=[SID_C],
            actor_kind=ActorKind.OPERATOR,
        )
    assert _codes_of(excinfo.value)["server_ids"] == "invalid_scope_change"
    # Removing everything but the home is fine; emptying is not.
    changed = await achange_scope(
        sessions,
        oid,
        server_id=SID_A,
        expected_revision=detail.revision,
        remove_server_ids=[SID_B],
        actor_kind=ActorKind.OPERATOR,
    )
    assert {m.server_id for m in changed.scope} == {SID_A}
    with pytest.raises(ObjectiveContractError) as excinfo:
        await achange_scope(
            sessions,
            oid,
            server_id=SID_A,
            expected_revision=changed.revision,
            add_server_ids=[SID_B],
            remove_server_ids=[SID_A],  # would empty AND kill the home
            actor_kind=ActorKind.OPERATOR,
        )
    assert _codes_of(excinfo.value)["remove_server_ids"] == "scope_violation"


@pytest.mark.anyio
async def test_run_binding_links_runs_and_children_without_broadening(
    sessions,
) -> None:
    oid = await _make_objective(sessions, scope=(SID_A, SID_B))
    await _seed_job_run(sessions, LEGACY_RUN)
    await _seed_job_run(sessions, LEGACY_RUN_2, parent_run_id=LEGACY_RUN, depth=1)
    parent = await abind_run(
        sessions,
        oid,
        server_id=SID_A,
        run_id=uuid.UUID(LEGACY_RUN),
        initiating_server_id=SID_B,
        actor_kind=ActorKind.AGENT,
    )
    assert parent.handler == "agent.run_v2"
    child = await abind_run(
        sessions,
        oid,
        server_id=SID_A,
        run_id=uuid.UUID(LEGACY_RUN_2),
        initiating_server_id=SID_A,
        actor_kind=ActorKind.CHILD_AGENT,
        parent_run_id=uuid.UUID(LEGACY_RUN),
    )
    assert child.parent_run_id == uuid.UUID(LEGACY_RUN)

    # A child can never smuggle an out-of-scope initiating server in.
    await _seed_job_run(sessions, "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
    with pytest.raises(ObjectiveContractError) as excinfo:
        await abind_run(
            sessions,
            oid,
            server_id=SID_A,
            run_id=uuid.UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"),
            initiating_server_id=SID_C,  # NOT in scope
            actor_kind=ActorKind.CHILD_AGENT,
            parent_run_id=uuid.UUID(LEGACY_RUN),
        )
    assert (
        _codes_of(excinfo.value)["initiating_server_id"] == "run_not_in_scope"
    )
    # ...even when the caller lies about which server is asking.
    with pytest.raises(ObjectiveContractError):
        await abind_run(
            sessions,
            oid,
            server_id=SID_C,  # requesting server itself is out of scope
            run_id=uuid.UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"),
            initiating_server_id=SID_A,
            actor_kind=ActorKind.CHILD_AGENT,
        )
    # Children stay inside the parent's objective episode; a run binds
    # to exactly one objective (UNIQUE(run_id) is the DB backstop).
    other = await _make_objective(
        sessions, scope=(SID_A,), request="another episode"
    )
    with pytest.raises(IntegrityError):
        await abind_run(
            sessions,
            other,
            server_id=SID_A,
            run_id=uuid.UUID(LEGACY_RUN_2),  # already bound to the first
            initiating_server_id=SID_A,
            actor_kind=ActorKind.CHILD_AGENT,
        )
    # A run binds exactly once (idempotent re-bind returns the link).
    again = await abind_run(
        sessions,
        oid,
        server_id=SID_A,
        run_id=uuid.UUID(LEGACY_RUN),
        initiating_server_id=SID_B,
        actor_kind=ActorKind.AGENT,
    )
    assert again.created_at == parent.created_at
    # The linkage page is bounded and deterministic.
    page = await alist_objective_runs(sessions, oid, server_id=SID_A, limit=1)
    assert page.total == 2 and len(page.items) == 1 and page.has_more is True
    # Delegation links fold into the journal as trusted records.
    state = await ajournal_apply(
        sessions,
        oid,
        server_id=SID_A,
        mutation=JournalChildLinked(
            run_id=uuid.UUID(LEGACY_RUN_2),
            parent_run_id=uuid.UUID(LEGACY_RUN),
            server_ids=[SID_A],
            budget=ObjectiveBudget(max_total_steps=10),
        ),
        actor_kind=ActorKind.AGENT,
    )
    assert len(state.children) == 1
    derived = await aderive_journal(sessions, oid, server_id=SID_A)
    assert derived == state
