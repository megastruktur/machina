"""Focused tests for the conversation-domain persistence (card 04-conversation-store).

Acceptance (BRIEF, act_CONTRACTS §1–§4):

1. Migration ``0008`` (``revision="0008"``, ``down_revision="0007"``)
   upgrades fresh and existing fixture databases and supports the
   ``downgrade 0007 → upgrade head`` round trip; message sequence numbers
   start at 1 per conversation and are allocated under
   ``SELECT ... FOR UPDATE`` on the conversation row; client-request
   deduplication is enforced by ``UNIQUE(conversation_id,
   client_request_id)`` (same payload → the existing row, changed payload
   → ``client_request_conflict``); pagination is deterministic
   (``limit``/``offset``/``total``/``has_more`` with ``COUNT(*)`` under the
   identical filter predicate).
2. Cross-server access fails closed (``conversation_not_found`` via
   ``assert_conversation_scope``, C8 — the other scope's existence never
   leaks); forged producer roles and invalid role/provenance combinations
   are rejected; repeated identical client requests (C4) never create
   duplicate ``conversation_messages`` rows and concurrent submissions
   (C6) serialize cleanly under the conversation row lock; raw
   secret-shaped content is scrubbed through
   ``mai.agents.output_safety.project_text`` BEFORE PostgreSQL (zero
   secret persistence).

Boundary discipline (test policy): every storage claim runs against a real
disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``) with the real
linear Alembic chain applied — dedup/serialization claims are proven by
real concurrent coroutines on real connections, the C8 claim by a real
storage query across two server IDs, and the chain claim by a real prior
round trip. Fixtures carry only fixed opaque UUIDs and synthetic text —
never real secret values (the "secrets" below are synthetic markers that
merely LOOK secret-shaped). The 00-runtime fixture suites stay untouched:
this card adds its own disposable database fixture.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from mai.agents.conversation_store import (
    MAX_PAGE_LIMIT,
    ROLE_SOURCE_MATRIX,
    MessagePageRow,
    RunLinkRow,
    SteeringRow,
    TurnAppendResult,
    aappend_producer_message,
    aattach_run,
    acreate_conversation,
    adelete_conversation,
    aenqueue_steering,
    aget_conversation,
    alist_conversations,
    alist_messages,
    aactive_run,
    aapply_steering,
    arelease_run,
    asubmit_turn,
)
from mai.contracts.conversations import (
    HistoryCompleteness,
    MessageRange,
    MessageRole,
    MessageSourceKind,
    SteerAction,
    TranscriptMessage,
    TurnDisposition,
    ConversationContractError,
    ConversationTranscriptPage,
    PageEnvelope,
    assert_conversation_scope,
    content_fingerprint,
)
from mai.database import create_app_engine

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Linear migration chain (exclusive migration-chain resource). Head
# assertions target the chain TIP; the tuple tracks the full chain.
# Chain-tip reconciliation (card 04-conversation-store, exclusive
# migration-chain owner): revision 0008 appends the conversation-domain
# tables and revision 0009 appends the agent-task domain; the tip
# constants are advanced here so this file's head assertions stay true.
REVISION_IDS = ("0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009")

# The exact public application schema at the 0009 tip: the 0008 set plus
# the seven agent-task domain tables (09-agent-tasks), and (from the 0011
# tip onward) the three history-tag tables (W1) — nothing else, in
# particular no engine-shadowing run-state table and no ``dbos`` schema
# before engine launch.
EXPECTED_APP_TABLES = frozenset(
    {
        "admin_credential",
        "admin_sessions",
        "alembic_version",
        "approvals",
        "profile_projection",
        "profile_projection_journal",
        "job_runs",
        "job_steps",
        "job_events",
        "job_schedules",
        "job_schedule_fires",
        "knowledge_documents",
        "knowledge_doc_chunks",
        "application_projection",
        "application_projection_journal",
        "server_observations",
        "observation_findings",
        "notification_alerts",
        "conversations",
        "conversation_messages",
        "conversation_runs",
        "conversation_steering",
        "agent_tasks",
        "agent_task_steps",
        "agent_task_constraints",
        "agent_task_criteria",
        "agent_task_evidence",
        "agent_task_revisions",
        "agent_task_completions",
        "tags",
        "job_tags",
        "conversation_tags",
    }
)

CONVERSATION_TABLES = (
    "conversations",
    "conversation_messages",
    "conversation_runs",
    "conversation_steering",
)

# Frozen fixed fixture identities (canonical lowercase UUID strings).
SERVER_A = "a10b8c5e-1d2f-4a3b-9c4d-0e5f6a7b8c9d"
SERVER_B = "b20c9d6f-2e3a-4b4c-8d5e-1f0a9b8c7d6e"
CONV_A = "c30dae70-3f4b-4c5d-9e6f-2a1b0c9d8e7f"
CONV_B = "d41bf581-4a5c-4d6e-af70-3b2c1d0e9f8a"
RUN_1 = "e52c0692-5b6d-4e7f-b081-4c3d2e1f0a9b"
RUN_2 = "f63d17a3-6c7e-4f80-c192-5d4e3f2a1b0c"

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

# Synthetic secret-SHAPED markers (not real credentials).
SECRET_PAYLOAD = (
    "please configure the deploy token\n"
    "password=hunter2\n"
    "api_key: sk-abcdef1234567890abcdef12\n"
    "Authorization: Bearer Zx9.Wq1-aaaabbbbccccdddd\n"
)


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _database_url(url: str, database: str) -> str:
    """Same server, different database (for isolated chain experiments)."""
    return make_url(url).set(database=database).render_as_string(hide_password=False)


def _drop_app_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        # Full app set at the chain tip — the 04-conversation-store tables,
        # the 09-agent-task tables (which hold the
        # fk_agent_tasks_conversation dependency on conversations) and the
        # 0011 history-tag tables (which hold the job_tags/conversation_tags
        # dependencies on conversations, job_runs and tags) are listed here
        # (chain-tip reconciliation) so the zero-to-head fixture stays
        # order-independent after revision 0009, 0011 and after sibling
        # suites ran on the same shared database.
        conn.execute(
            "DROP TABLE IF EXISTS conversation_steering, conversation_runs, "
            "conversation_messages, conversations, conversation_tags, "
            "notification_alerts, observation_findings, "
            "server_observations, application_projection_journal, "
            "application_projection, knowledge_doc_chunks, knowledge_documents, job_resource_locks, "
            "job_schedule_fires, job_schedules, job_events, job_steps, "
            "job_runs, job_tags, approvals, profile_projection_journal, "
            "profile_projection, admin_sessions, admin_credential, "
            "agent_task_completions, agent_task_revisions, "
            "agent_task_evidence, agent_task_criteria, "
            "agent_task_constraints, agent_task_steps, agent_tasks, "
            "tags, alembic_version"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``0008``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the storage claims need a real "
            "disposable Postgres fixture"
        )
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
    return url


@pytest.fixture
async def sessions(migrated_url: str):
    engine, factory = create_app_engine(migrated_url)
    yield factory
    # Each data test starts from an empty conversation domain (session-
    # scoped fixture database): delete children before parents.
    async with factory() as session:
        await session.execute(text("DELETE FROM conversation_steering"))
        await session.execute(text("DELETE FROM conversation_runs"))
        await session.execute(text("DELETE FROM conversation_messages"))
        await session.execute(text("DELETE FROM conversations"))
        await session.commit()
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
        "migration_0008",
        os.path.join(BACKEND_ROOT, "migrations", "versions", "0008_conversations.py"),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


async def _constraint_def(sessions, name: str) -> str | None:
    row = await _fetchone(
        sessions,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name",
        name=name,
    )
    return None if row is None else str(row[0])


def _literals_of(constraint: str | None) -> list[str]:
    if constraint is None:
        return []
    return re.findall(r"'([^']*)'", constraint)


def _codes_of(exc: ConversationContractError) -> dict[str, str]:
    return exc.fields


# ---------------------------------------------------------------------------
# AC1 — migration chain, vocabulary mirror, bounds
# ---------------------------------------------------------------------------


class TestMigrationChain:
    def test_revision_constants_target_the_0007_parent(self) -> None:
        module = _migration_module()
        assert module.revision == "0008"
        assert module.down_revision == "0007"

    def test_head_is_0009_and_upgrade_is_idempotent(self, migrated_url: str) -> None:
        # Sync on purpose: alembic's env.py drives its async engine with
        # asyncio.run(), which must not nest inside a test event loop.
        previous = os.environ.get("MAI_DATABASE_URL")
        os.environ["MAI_DATABASE_URL"] = migrated_url
        try:
            command.upgrade(_alembic_config(), "head")  # second run on the same DB
        finally:
            if previous is None:
                os.environ.pop("MAI_DATABASE_URL", None)
            else:
                os.environ["MAI_DATABASE_URL"] = previous
        with psycopg.connect(migrated_url) as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert row == (REVISION_IDS[-1],)

    @pytest.mark.anyio
    async def test_exact_app_tables_at_the_head(self, sessions) -> None:
        rows = await _fetchall(
            sessions,
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
            """,
        )
        tables = {row[0] for row in rows}
        assert tables == EXPECTED_APP_TABLES
        assert not any(name.startswith(("workflow_", "dbos_")) for name in tables)

    @pytest.mark.anyio
    async def test_check_vocabulary_mirrors_the_contract_enums(self, sessions) -> None:
        assert _literals_of(
            await _constraint_def(sessions, "ck_conversation_messages_role")
        ) == [role.value for role in MessageRole]
        assert _literals_of(
            await _constraint_def(sessions, "ck_conversation_messages_source")
        ) == [kind.value for kind in MessageSourceKind]
        assert _literals_of(
            await _constraint_def(sessions, "ck_conversations_history_completeness")
        ) == [value.value for value in HistoryCompleteness]
        assert _literals_of(
            await _constraint_def(sessions, "ck_conversation_steering_action")
        ) == [action.value for action in SteerAction]
        assert _literals_of(
            await _constraint_def(sessions, "ck_conversation_steering_delivery")
        ) == ["pending", "applied", "cancelled"]

    def test_downgrade_round_trip_restores_the_head(
        self, migrated_url: str
    ) -> None:
        # Sync for the alembic commands (see the head test); the store
        # writes run through asyncio.run in short-lived engines.
        async def _seed() -> int:
            engine, factory = create_app_engine(migrated_url)
            try:
                conv = await acreate_conversation(
                    factory, server_id=uuid.UUID(SERVER_A)
                )
                result = await asubmit_turn(
                    factory,
                    conversation_id=conv.conversation_id,
                    server_id=uuid.UUID(SERVER_A),
                    client_request_id=uuid.uuid4(),
                    message="survives only until the downgrade",
                )
                return result.message.sequence
            finally:
                await engine.dispose()

        assert asyncio.run(_seed()) == 1
        previous = os.environ.get("MAI_DATABASE_URL")
        os.environ["MAI_DATABASE_URL"] = migrated_url
        try:
            command.downgrade(_alembic_config(), "0007")
            with psycopg.connect(migrated_url) as conn:
                version = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                leftovers = conn.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = ANY(%(names)s)",
                    {"names": list(CONVERSATION_TABLES)},
                ).fetchone()
            assert version == ("0007",)
            assert leftovers == (0,)
            command.upgrade(_alembic_config(), "head")
        finally:
            if previous is None:
                os.environ.pop("MAI_DATABASE_URL", None)
            else:
                os.environ["MAI_DATABASE_URL"] = previous
        with psycopg.connect(migrated_url) as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert row == (REVISION_IDS[-1],)

        # The fresh 0008 schema accepts writes again, from seq 1.
        async def _write_after_round_trip() -> int:
            engine, factory = create_app_engine(migrated_url)
            try:
                conv = await acreate_conversation(
                    factory, server_id=uuid.UUID(SERVER_A)
                )
                result = await asubmit_turn(
                    factory,
                    conversation_id=conv.conversation_id,
                    server_id=uuid.UUID(SERVER_A),
                    client_request_id=uuid.uuid4(),
                    message="write after the round trip",
                )
                return result.message.sequence
            finally:
                await engine.dispose()

        assert asyncio.run(_write_after_round_trip()) == 1

    @pytest.mark.anyio
    async def test_schema_bounds_fail_closed(self, sessions) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        cid = conv.conversation_id
        with pytest.raises(IntegrityError):
            async with sessions() as session, session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO conversation_messages
                            (message_id, conversation_id, seq, role, source_kind,
                             content, content_fingerprint)
                        VALUES (:mid, :cid, 1, :role, 'api', 'x', :fp)
                        """
                    ),
                    {
                        "mid": uuid.uuid4(),
                        "cid": cid,
                        "role": "admin",  # forged role outside the frozen vocabulary
                        "fp": "0" * 64,
                    },
                )
        with pytest.raises(IntegrityError):
            async with sessions() as session, session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO conversation_messages
                            (message_id, conversation_id, seq, role, source_kind,
                             content, content_fingerprint)
                        VALUES (:mid, :cid, 1, 'user', :source, 'x', :fp)
                        """
                    ),
                    {
                        "mid": uuid.uuid4(),
                        "cid": cid,
                        "source": "carrier_pigeon",  # forged provenance
                        "fp": "0" * 64,
                    },
                )
        with pytest.raises(IntegrityError):
            async with sessions() as session, session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO conversation_messages
                            (message_id, conversation_id, seq, role, source_kind,
                             content, content_fingerprint)
                        VALUES (:mid, :cid, 0, 'user', 'api', 'x', :fp)
                        """
                    ),
                    {"mid": uuid.uuid4(), "cid": cid, "fp": "0" * 64},
                )
        with pytest.raises(IntegrityError):
            async with sessions() as session, session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO conversation_messages
                            (message_id, conversation_id, seq, role, source_kind,
                             content, content_fingerprint)
                        VALUES (:mid, :cid, 2, 'user', 'api', :content, :fp)
                        """
                    ),
                    {
                        "mid": uuid.uuid4(),
                        "cid": cid,
                        "content": "x" * 20_001,  # over the contract bound
                        "fp": "0" * 64,
                    },
                )
        with pytest.raises(IntegrityError):
            async with sessions() as session, session.begin():
                # pause/cancel are deterministic controls and carry no content.
                await session.execute(
                    text(
                        """
                        INSERT INTO conversation_steering
                            (steering_id, conversation_id, client_request_id,
                             action, content)
                        VALUES (:stid, :cid, :crid, 'pause', 'do it now')
                        """
                    ),
                    {"stid": uuid.uuid4(), "cid": cid, "crid": uuid.uuid4()},
                )
        with pytest.raises(IntegrityError):
            async with sessions() as session, session.begin():
                # complete history cannot declare a missing range
                await session.execute(
                    text(
                        """
                        INSERT INTO conversations
                            (conversation_id, server_id, history_completeness,
                             missing_from_seq, missing_to_seq)
                        VALUES (:cid2, :sid, 'complete', 3, 7)
                        """
                    ),
                    {"cid2": uuid.uuid4(), "sid": uuid.UUID(SERVER_A)},
                )


# ---------------------------------------------------------------------------
# AC2 — conversation identity and scope (C8)
# ---------------------------------------------------------------------------


class TestConversationIdentity:
    @pytest.mark.anyio
    async def test_create_and_get_roundtrip(self, sessions) -> None:
        created = await acreate_conversation(
            sessions,
            server_id=uuid.UUID(SERVER_A),
            title="deploy probe",
            conversation_id=uuid.UUID(CONV_A),
        )
        assert created.conversation_id == uuid.UUID(CONV_A)
        assert created.server_id == uuid.UUID(SERVER_A)
        assert created.history_completeness is HistoryCompleteness.COMPLETE
        assert created.missing_range is None
        fetched = await aget_conversation(
            sessions, uuid.UUID(CONV_A), server_id=uuid.UUID(SERVER_A)
        )
        assert fetched == created

    @pytest.mark.anyio
    async def test_legacy_conversation_declares_its_missing_range(
        self, sessions
    ) -> None:
        created = await acreate_conversation(
            sessions,
            server_id=uuid.UUID(SERVER_A),
            history_completeness=HistoryCompleteness.PARTIAL,
            missing_range=MessageRange(from_sequence=4, to_sequence=9),
        )
        fetched = await aget_conversation(
            sessions, created.conversation_id, server_id=uuid.UUID(SERVER_A)
        )
        assert fetched.history_completeness is HistoryCompleteness.PARTIAL
        assert fetched.missing_range is not None
        assert (
            fetched.missing_range.from_sequence,
            fetched.missing_range.to_sequence,
        ) == (4, 9)

    @pytest.mark.anyio
    async def test_cross_server_access_fails_closed_without_leaking(
        self, sessions
    ) -> None:
        created = await acreate_conversation(
            sessions, server_id=uuid.UUID(SERVER_A), conversation_id=uuid.UUID(CONV_A)
        )
        # C8: a different server's scope must not read, write, list or steer.
        server_b = uuid.UUID(SERVER_B)
        with pytest.raises(ConversationContractError) as exc:
            await aget_conversation(
                sessions, created.conversation_id, server_id=server_b
            )
        assert _codes_of(exc.value) == {"server_id": "conversation_not_found"}
        with pytest.raises(ConversationContractError):
            await asubmit_turn(
                sessions,
                conversation_id=created.conversation_id,
                server_id=server_b,
                client_request_id=uuid.uuid4(),
                message="cross-server write",
            )
        with pytest.raises(ConversationContractError):
            await alist_messages(
                sessions, created.conversation_id, server_id=server_b
            )
        with pytest.raises(ConversationContractError):
            await aattach_run(
                sessions,
                conversation_id=created.conversation_id,
                server_id=server_b,
                run_id=uuid.UUID(RUN_1),
            )
        with pytest.raises(ConversationContractError):
            await aenqueue_steering(
                sessions,
                conversation_id=created.conversation_id,
                server_id=server_b,
                action=SteerAction.PAUSE,
                client_request_id=uuid.uuid4(),
            )
        # The error channel carries no identity of the other scope.
        assert SERVER_A not in str(exc.value)
        assert str(created.conversation_id) not in str(exc.value)
        # An unknown conversation and an out-of-scope conversation are
        # indistinguishable at the error channel (fail closed, same code).
        with pytest.raises(ConversationContractError) as missing:
            await aget_conversation(sessions, uuid.uuid4(), server_id=server_b)
        assert _codes_of(missing.value) == {"conversation_id": "conversation_not_found"}
        # Nothing leaked into the other server's scope either.
        assert await _fetchval(
            sessions,
            "SELECT COUNT(*) FROM conversation_messages",
        ) == 0

    def test_scope_guard_rejects_mismatch_directly(self) -> None:
        with pytest.raises(ConversationContractError) as exc:
            assert_conversation_scope(uuid.UUID(SERVER_A), uuid.UUID(SERVER_B))
        assert _codes_of(exc.value) == {"server_id": "conversation_not_found"}

    @pytest.mark.anyio
    async def test_title_and_range_input_validation(self, sessions) -> None:
        with pytest.raises(ValueError, match="at most 240"):
            await acreate_conversation(
                sessions, server_id=uuid.UUID(SERVER_A), title="x" * 241
            )
        with pytest.raises(ValueError, match="complete history"):
            await acreate_conversation(
                sessions,
                server_id=uuid.UUID(SERVER_A),
                history_completeness=HistoryCompleteness.COMPLETE,
                missing_range=MessageRange(from_sequence=1, to_sequence=2),
            )

    @pytest.mark.anyio
    async def test_list_page_is_server_scoped_and_deterministic(self, sessions) -> None:
        for index in range(7):
            await acreate_conversation(
                sessions,
                server_id=uuid.UUID(SERVER_A),
                title=f"conv-{index}",
            )
        for _ in range(3):
            await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_B))
        items, total = await alist_conversations(
            sessions, server_id=uuid.UUID(SERVER_A), limit=3, offset=0
        )
        assert total == 7
        # Deterministic ORDER BY updated_at DESC, conversation_id — proven
        # against a Python-side sort of the same rows (never against a
        # second copy of the SQL).
        stored = await _fetchall(
            sessions,
            "SELECT conversation_id, title, updated_at FROM conversations "
            "WHERE server_id = :sid",
            sid=uuid.UUID(SERVER_A),
        )
        expected_order = [
            row[1]
            for row in sorted(
                stored, key=lambda row: (-row[2].timestamp(), row[0])
            )
        ]
        assert [item.title for item in items] == expected_order[:3]
        assert all(isinstance(item, MessagePageRow) for item in items)
        # Server B never sees A's rows and vice versa.
        _, total_b = await alist_conversations(
            sessions, server_id=uuid.UUID(SERVER_B), limit=MAX_PAGE_LIMIT, offset=0
        )
        assert total_b == 3
        tail, tail_total = await alist_conversations(
            sessions, server_id=uuid.UUID(SERVER_A), limit=3, offset=5
        )
        assert tail_total == 7
        assert [item.title for item in tail] == expected_order[5:8]
        with pytest.raises(ValueError, match="limit"):
            await alist_conversations(
                sessions, server_id=uuid.UUID(SERVER_A), limit=0
            )
        with pytest.raises(ValueError, match="limit"):
            await alist_conversations(
                sessions, server_id=uuid.UUID(SERVER_A), limit=MAX_PAGE_LIMIT + 1
            )
        with pytest.raises(ValueError, match="offset"):
            await alist_conversations(
                sessions, server_id=uuid.UUID(SERVER_A), offset=-1
            )

    @pytest.mark.anyio
    async def test_delete_cascades_the_whole_domain(self, sessions) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        await asubmit_turn(
            sessions,
            conversation_id=conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            client_request_id=uuid.uuid4(),
            message="hello",
        )
        await aattach_run(
            sessions,
            conversation_id=conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            run_id=uuid.UUID(RUN_1),
        )
        await aenqueue_steering(
            sessions,
            conversation_id=conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            action=SteerAction.PAUSE,
            client_request_id=uuid.uuid4(),
        )
        await adelete_conversation(
            sessions, conv.conversation_id, server_id=uuid.UUID(SERVER_A)
        )
        counts = await _fetchone(
            sessions,
            """
            SELECT
                (SELECT COUNT(*) FROM conversations),
                (SELECT COUNT(*) FROM conversation_messages),
                (SELECT COUNT(*) FROM conversation_runs),
                (SELECT COUNT(*) FROM conversation_steering)
            """,
        )
        assert counts == (0, 0, 0, 0)
        with pytest.raises(ConversationContractError):
            await aget_conversation(
                sessions, conv.conversation_id, server_id=uuid.UUID(SERVER_A)
            )


# ---------------------------------------------------------------------------
# AC1/AC2 — turn submission: seq, C4 idempotency, C6 serialization
# ---------------------------------------------------------------------------


class TestTurnSubmit:
    @pytest.mark.anyio
    async def test_sequences_start_at_one_and_monotonically_advance(
        self, sessions
    ) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        results: list[TurnAppendResult] = []
        for index in range(5):
            results.append(
                (
                    await asubmit_turn(
                        sessions,
                        conversation_id=conv.conversation_id,
                        server_id=uuid.UUID(SERVER_A),
                        client_request_id=uuid.uuid4(),
                        message=f"turn {index}",
                    )
                )
            )
        assert [r.message.sequence for r in results] == [1, 2, 3, 4, 5]
        assert all(r.disposition is TurnDisposition.CREATED for r in results)
        assert len({r.message.message_id for r in results}) == 5
        # A second conversation allocates its own independent sequence.
        other = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        first_other = await asubmit_turn(
            sessions,
            conversation_id=other.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            client_request_id=uuid.uuid4(),
            message="independent",
        )
        assert first_other.message.sequence == 1

    @pytest.mark.anyio
    async def test_same_request_id_deduplicates_onto_the_original_row(
        self, sessions
    ) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        key = uuid.uuid4()
        first = await asubmit_turn(
            sessions,
            conversation_id=conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            client_request_id=key,
            message="retry me (browser refresh retransmits this)",
        )
        assert first.disposition is TurnDisposition.CREATED
        retry = await asubmit_turn(
            sessions,
            conversation_id=conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            client_request_id=key,
            message="retry me (browser refresh retransmits this)",
        )
        assert retry.disposition is TurnDisposition.DEDUPLICATED
        assert retry.message.message_id == first.message.message_id
        assert retry.message.sequence == first.message.sequence
        assert await _fetchval(
            sessions,
            "SELECT COUNT(*) FROM conversation_messages "
            "WHERE conversation_id = :cid",
            cid=conv.conversation_id,
        ) == 1
        # C4 contract: same key + changed content is a conflict, not a write.
        with pytest.raises(ConversationContractError) as exc:
            await asubmit_turn(
                sessions,
                conversation_id=conv.conversation_id,
                server_id=uuid.UUID(SERVER_A),
                client_request_id=key,
                message="retry me but CHANGED",
            )
        assert _codes_of(exc.value) == {"client_request_id": "client_request_conflict"}
        assert await _fetchval(
            sessions,
            "SELECT COUNT(*) FROM conversation_messages "
            "WHERE conversation_id = :cid",
            cid=conv.conversation_id,
        ) == 1
        # A different key with the same content is a new, honest message.
        second = await asubmit_turn(
            sessions,
            conversation_id=conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            client_request_id=uuid.uuid4(),
            message="retry me (browser refresh retransmits this)",
        )
        assert second.disposition is TurnDisposition.CREATED
        assert second.message.sequence == 2

    @pytest.mark.anyio
    async def test_concurrent_distinct_submissions_serialize_without_gaps(
        self, sessions
    ) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))

        async def submit(index: int) -> TurnAppendResult:
            return await asubmit_turn(
                sessions,
                conversation_id=conv.conversation_id,
                server_id=uuid.UUID(SERVER_A),
                client_request_id=uuid.uuid4(),
                message=f"concurrent turn {index}",
            )

        results = await asyncio.gather(*(submit(i) for i in range(8)))
        assert all(r.disposition is TurnDisposition.CREATED for r in results)
        # C6: the conversation row lock serializes allocation — exactly the
        # sequences 1..8, no duplicates, no gaps.
        assert sorted(r.message.sequence for r in results) == list(range(1, 9))
        assert len({r.message.message_id for r in results}) == 8
        assert await _fetchval(
            sessions,
            "SELECT COUNT(*) FROM conversation_messages "
            "WHERE conversation_id = :cid",
            cid=conv.conversation_id,
        ) == 8

    @pytest.mark.anyio
    async def test_concurrent_same_key_submissions_yield_exactly_one_row(
        self, sessions
    ) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        key = uuid.uuid4()

        async def submit() -> TurnAppendResult:
            return await asubmit_turn(
                sessions,
                conversation_id=conv.conversation_id,
                server_id=uuid.UUID(SERVER_A),
                client_request_id=key,
                message="one message racing against its own retransmissions",
            )

        results = await asyncio.gather(*(submit() for _ in range(6)))
        dispositions = [r.disposition for r in results]
        assert dispositions.count(TurnDisposition.CREATED) == 1
        assert dispositions.count(TurnDisposition.DEDUPLICATED) == 5
        assert len({r.message.message_id for r in results}) == 1
        assert await _fetchval(
            sessions,
            "SELECT COUNT(*) FROM conversation_messages "
            "WHERE conversation_id = :cid",
            cid=conv.conversation_id,
        ) == 1

    @pytest.mark.anyio
    async def test_concurrent_conflicting_payloads_admit_exactly_one_writer(
        self, sessions
    ) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        key = uuid.uuid4()

        async def submit(payload: str) -> TurnAppendResult:
            return await asubmit_turn(
                sessions,
                conversation_id=conv.conversation_id,
                server_id=uuid.UUID(SERVER_A),
                client_request_id=key,
                message=payload,
            )

        results = await asyncio.gather(
            *(submit(f"variant {i}") for i in range(6)), return_exceptions=True
        )
        created = [r for r in results if isinstance(r, TurnAppendResult)]
        conflicts = [
            r
            for r in results
            if isinstance(r, ConversationContractError)
            and _codes_of(r) == {"client_request_id": "client_request_conflict"}
        ]
        assert len(created) == 1
        assert len(conflicts) == 5
        assert await _fetchval(
            sessions,
            "SELECT COUNT(*) FROM conversation_messages "
            "WHERE conversation_id = :cid",
            cid=conv.conversation_id,
        ) == 1

    @pytest.mark.anyio
    async def test_submit_path_rejects_non_user_provenance_and_blank_text(
        self, sessions
    ) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        with pytest.raises(ValueError, match="browser/api provenance only"):
            await asubmit_turn(
                sessions,
                conversation_id=conv.conversation_id,
                server_id=uuid.UUID(SERVER_A),
                client_request_id=uuid.uuid4(),
                message="forged legacy turn",
                source_kind=MessageSourceKind.LEGACY_IMPORT,
            )
        with pytest.raises(ValueError, match="non-blank"):
            await asubmit_turn(
                sessions,
                conversation_id=conv.conversation_id,
                server_id=uuid.UUID(SERVER_A),
                client_request_id=uuid.uuid4(),
                message="   ",
            )
        assert await _fetchval(
            sessions, "SELECT COUNT(*) FROM conversation_messages"
        ) == 0


# ---------------------------------------------------------------------------
# AC2 — producer roles and provenance cannot be forged
# ---------------------------------------------------------------------------


class TestProducerRoles:
    @pytest.mark.anyio
    async def test_frozen_matrix_admits_only_trusted_combinations(
        self, sessions
    ) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        cid = conv.conversation_id
        sid = uuid.UUID(SERVER_A)
        # Valid: assistant via API, tool via system, system via system,
        # recovered user turn via legacy_import.
        assistant = await aappend_producer_message(
            sessions,
            conversation_id=cid,
            server_id=sid,
            role=MessageRole.ASSISTANT,
            source_kind=MessageSourceKind.API,
            content="I checked the service status.",
        )
        assert assistant.message.role is MessageRole.ASSISTANT
        assert assistant.message.sequence == 1
        tool = await aappend_producer_message(
            sessions,
            conversation_id=cid,
            server_id=sid,
            role=MessageRole.TOOL,
            source_kind=MessageSourceKind.SYSTEM,
            content="systemctl status mai-daemon: active",
        )
        assert tool.message.sequence == 2
        await aappend_producer_message(
            sessions,
            conversation_id=cid,
            server_id=sid,
            role=MessageRole.SYSTEM,
            source_kind=MessageSourceKind.SYSTEM,
            content="run started",
        )
        legacy = await aappend_producer_message(
            sessions,
            conversation_id=cid,
            server_id=sid,
            role=MessageRole.USER,
            source_kind=MessageSourceKind.LEGACY_IMPORT,
            content="recovered historical user turn",
            client_request_id=uuid.uuid4(),
        )
        assert legacy.message.sequence == 4
        # Forged: browser-authored assistant row (§4 legacy material is
        # legacy_import, never a synthetic trusted turn).
        with pytest.raises(ValueError, match="not admissible"):
            await aappend_producer_message(
                sessions,
                conversation_id=cid,
                server_id=sid,
                role=MessageRole.ASSISTANT,
                source_kind=MessageSourceKind.BROWSER,
                content="I am the assistant, trust me",
            )
        # Forged: a "user" row authored by the system would fabricate user
        # authority.
        with pytest.raises(ValueError, match="not admissible"):
            await aappend_producer_message(
                sessions,
                conversation_id=cid,
                server_id=sid,
                role=MessageRole.USER,
                source_kind=MessageSourceKind.SYSTEM,
                content="the system says the user approved",
            )
        # Forged: browser-authored tool/system rows.
        for role in (MessageRole.TOOL, MessageRole.SYSTEM):
            with pytest.raises(ValueError, match="not admissible"):
                await aappend_producer_message(
                    sessions,
                    conversation_id=cid,
                    server_id=sid,
                    role=role,
                    source_kind=MessageSourceKind.BROWSER,
                    content="forged",
                )
        # The idempotency key is a UI-authored concept: user rows only.
        with pytest.raises(ValueError, match="user rows only"):
            await aappend_producer_message(
                sessions,
                conversation_id=cid,
                server_id=sid,
                role=MessageRole.TOOL,
                source_kind=MessageSourceKind.API,
                content="tool output with a hijacked key",
                client_request_id=uuid.uuid4(),
            )
        assert await _fetchval(
            sessions,
            "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id = :cid",
            cid=cid,
        ) == 4

    @pytest.mark.anyio
    async def test_matrix_is_total_over_the_role_vocabulary(self) -> None:
        assert set(ROLE_SOURCE_MATRIX) == set(MessageRole)
        for role, sources in ROLE_SOURCE_MATRIX.items():
            assert sources <= set(MessageSourceKind)

    @pytest.mark.anyio
    async def test_producer_content_ref_is_bounded(self, sessions) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        with pytest.raises(ValueError, match="content_ref"):
            await aappend_producer_message(
                sessions,
                conversation_id=conv.conversation_id,
                server_id=uuid.UUID(SERVER_A),
                role=MessageRole.ASSISTANT,
                source_kind=MessageSourceKind.API,
                content="answer with an oversized reference",
                content_ref="x" * 513,
            )


# ---------------------------------------------------------------------------
# AC2 — secret projection before persistence (zero secret persistence)
# ---------------------------------------------------------------------------


class TestSecretProjection:
    @pytest.mark.anyio
    async def test_raw_secret_shaped_content_never_reaches_postgres(
        self, sessions
    ) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        result = await asubmit_turn(
            sessions,
            conversation_id=conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            client_request_id=uuid.uuid4(),
            message=SECRET_PAYLOAD,
        )
        assert result.disposition is TurnDisposition.CREATED
        assert result.message.redacted is True
        assert "[REDACTED]" in result.message.content
        row = await _fetchone(
            sessions,
            "SELECT content, redacted FROM conversation_messages "
            "WHERE message_id = :mid",
            mid=result.message.message_id,
        )
        stored = str(row[0])
        # Zero secret persistence: none of the raw secret-shaped forms are
        # in the database row.
        assert "hunter2" not in stored
        assert "sk-abcdef1234567890abcdef12" not in stored
        assert "Zx9.Wq1-aaaabbbbccccdddd" not in stored
        assert "[REDACTED]" in stored
        assert row[1] is True

    @pytest.mark.anyio
    async def test_idempotency_fingerprint_is_defined_over_projected_truth(
        self, sessions
    ) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        key = uuid.uuid4()
        first = await asubmit_turn(
            sessions,
            conversation_id=conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            client_request_id=key,
            message=SECRET_PAYLOAD,
        )
        # The stored fingerprint is the fingerprint of the PROJECTED text.
        assert first.message.content_fingerprint == content_fingerprint(
            first.message.content
        )
        # Retransmitting the same raw secret text projects identically →
        # dedup (C4 still holds for secret-bearing retransmissions).
        retry = await asubmit_turn(
            sessions,
            conversation_id=conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            client_request_id=key,
            message=SECRET_PAYLOAD,
        )
        assert retry.disposition is TurnDisposition.DEDUPLICATED
        assert retry.message.message_id == first.message.message_id

    @pytest.mark.anyio
    async def test_producer_rows_are_projected_too(self, sessions) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        result = await aappend_producer_message(
            sessions,
            conversation_id=conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            role=MessageRole.ASSISTANT,
            source_kind=MessageSourceKind.API,
            content="the credential is api_key: sk-zzzzzz9999zzzzzzzzzz99",
        )
        row = await _fetchone(
            sessions,
            "SELECT content FROM conversation_messages WHERE message_id = :mid",
            mid=result.message.message_id,
        )
        assert "sk-zzzzzz9999zzzzzzzzzz99" not in str(row[0])
        assert "[REDACTED]" in str(row[0])
        steer, disposition = await aenqueue_steering(
            sessions,
            conversation_id=conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            action=SteerAction.STEER,
            client_request_id=uuid.uuid4(),
            content="use password=tiger2 for the restart",
        )
        assert disposition is TurnDisposition.CREATED
        assert "tiger2" not in (steer.content or "")
        assert "[REDACTED]" in (steer.content or "")

    @pytest.mark.anyio
    async def test_oversized_content_is_bounded_after_redaction(
        self, sessions
    ) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        huge = ("password=leaksecret " * 3000) + "tail-marker"
        result = await asubmit_turn(
            sessions,
            conversation_id=conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            client_request_id=uuid.uuid4(),
            message=huge,
        )
        assert len(result.message.content) <= 20_000
        assert "leaksecret" not in result.message.content
        assert "[REDACTED]" in result.message.content


# ---------------------------------------------------------------------------
# AC1 — deterministic pagination with same-filter totals
# ---------------------------------------------------------------------------


async def _seed_transcript(sessions, cid, sid, *, users: int, assistants: int) -> None:
    for index in range(users):
        await asubmit_turn(
            sessions,
            conversation_id=cid,
            server_id=sid,
            client_request_id=uuid.uuid4(),
            message=f"user turn {index}",
        )
        if index < assistants:
            await aappend_producer_message(
                sessions,
                conversation_id=cid,
                server_id=sid,
                role=MessageRole.ASSISTANT,
                source_kind=MessageSourceKind.API,
                content=f"assistant turn {index}",
            )


class TestTranscriptPagination:
    @pytest.mark.anyio
    async def test_pages_are_ordered_bounded_and_honest(self, sessions) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        await _seed_transcript(
            sessions,
            conv.conversation_id,
            uuid.UUID(SERVER_A),
            users=13,
            assistants=12,
        )
        page1 = await alist_messages(
            sessions,
            conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            limit=10,
            offset=0,
        )
        assert isinstance(page1, ConversationTranscriptPage)
        assert (page1.limit, page1.offset, page1.total) == (10, 0, 25)
        assert page1.has_more is True
        assert [m.sequence for m in page1.items] == list(range(1, 11))
        page2 = await alist_messages(
            sessions,
            conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            limit=10,
            offset=10,
        )
        assert (page2.offset, page2.total, page2.has_more) == (10, 25, True)
        assert [m.sequence for m in page2.items] == list(range(11, 21))
        page3 = await alist_messages(
            sessions,
            conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            limit=10,
            offset=20,
        )
        assert (page3.offset, page3.total, page3.has_more) == (20, 25, False)
        assert [m.sequence for m in page3.items] == list(range(21, 26))
        # Pages concatenate into the full ordered transcript without dupes.
        all_rows = page1.items + page2.items + page3.items
        assert len({m.message_id for m in all_rows}) == 25
        assert [m.sequence for m in all_rows] == list(range(1, 26))
        # The page envelope is the frozen contract model.
        assert all(isinstance(m, TranscriptMessage) for m in all_rows)
        # Offset past the total is an honest empty page.
        empty = await alist_messages(
            sessions,
            conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            limit=10,
            offset=100,
        )
        assert (len(empty.items), empty.total, empty.has_more) == (0, 25, False)

    @pytest.mark.anyio
    async def test_role_filter_total_matches_the_identical_predicate(
        self, sessions
    ) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        await _seed_transcript(
            sessions,
            conv.conversation_id,
            uuid.UUID(SERVER_A),
            users=7,
            assistants=7,
        )
        user_page = await alist_messages(
            sessions,
            conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            limit=3,
            offset=0,
            role=MessageRole.USER,
        )
        assert user_page.total == 7
        assert all(m.role is MessageRole.USER for m in user_page.items)
        assert [m.sequence for m in user_page.items] == [1, 3, 5]
        # COUNT(*) ran under the identical filter predicate: total equals a
        # direct count with the same WHERE clause.
        assert await _fetchval(
            sessions,
            "SELECT COUNT(*) FROM conversation_messages "
            "WHERE conversation_id = :cid AND role = :role",
            cid=conv.conversation_id,
            role="user",
        ) == 7
        tool_page = await alist_messages(
            sessions,
            conv.conversation_id,
            server_id=uuid.UUID(SERVER_A),
            limit=MAX_PAGE_LIMIT,
            offset=0,
            role=MessageRole.TOOL,
        )
        assert tool_page.total == 0
        assert tool_page.has_more is False
        assert len(tool_page.items) == 0

    @pytest.mark.anyio
    async def test_page_bounds_fail_closed(self, sessions) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        with pytest.raises(ValueError, match="limit"):
            await alist_messages(
                sessions,
                conv.conversation_id,
                server_id=uuid.UUID(SERVER_A),
                limit=0,
            )
        with pytest.raises(ValueError, match="limit"):
            await alist_messages(
                sessions,
                conv.conversation_id,
                server_id=uuid.UUID(SERVER_A),
                limit=MAX_PAGE_LIMIT + 1,
            )
        with pytest.raises(ValueError, match="offset"):
            await alist_messages(
                sessions,
                conv.conversation_id,
                server_id=uuid.UUID(SERVER_A),
                offset=-1,
            )
        with pytest.raises(ValueError, match="MessageRole"):
            await alist_messages(
                sessions,
                conv.conversation_id,
                server_id=uuid.UUID(SERVER_A),
                role="assistant",  # type: ignore[arg-value]
            )

    def test_transcript_page_model_rejects_inconsistent_envelopes(self) -> None:
        def _message(seq: int) -> TranscriptMessage:
            return TranscriptMessage(
                conversation_id=uuid.UUID(CONV_A),
                message_id=uuid.uuid4(),
                sequence=seq,
                role=MessageRole.USER,
                source_kind=MessageSourceKind.API,
                created_at=NOW,
                content=f"m{seq}",
            )

        # has_more must equal offset + len(items) < total.
        with pytest.raises(Exception, match="has_more"):
            ConversationTranscriptPage(
                conversation_id=uuid.UUID(CONV_A),
                limit=10,
                offset=0,
                total=1,
                has_more=True,
                items=[_message(1)],
            )
        with pytest.raises(Exception, match="sequence-ordered"):
            ConversationTranscriptPage(
                conversation_id=uuid.UUID(CONV_A),
                limit=10,
                offset=0,
                total=2,
                has_more=False,
                items=[
                    TranscriptMessage(
                        conversation_id=uuid.UUID(CONV_A),
                        message_id=uuid.uuid4(),
                        sequence=2,
                        role=MessageRole.USER,
                        source_kind=MessageSourceKind.API,
                        created_at=NOW,
                        content="b",
                    ),
                    TranscriptMessage(
                        conversation_id=uuid.UUID(CONV_A),
                        message_id=uuid.uuid4(),
                        sequence=1,
                        role=MessageRole.USER,
                        source_kind=MessageSourceKind.API,
                        created_at=NOW,
                        content="a",
                    ),
                ],
            )
        with pytest.raises(Exception):
            PageEnvelope(limit=0, offset=0, total=0, has_more=False)


# ---------------------------------------------------------------------------
# AC2 — run linkage/lease and the durable steering queue
# ---------------------------------------------------------------------------


class TestRunLinksAndSteering:
    @pytest.mark.anyio
    async def test_attach_active_and_release_the_writer_lease(self, sessions) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        cid, sid = conv.conversation_id, uuid.UUID(SERVER_A)
        link = await aattach_run(
            sessions, conversation_id=cid, server_id=sid, run_id=uuid.UUID(RUN_1)
        )
        assert isinstance(link, RunLinkRow)
        assert link.released_at is None
        holder = await aactive_run(sessions, cid, server_id=sid)
        assert holder is not None
        assert holder.run_id == uuid.UUID(RUN_1)
        # Attach is idempotent: the same run links once.
        await aattach_run(
            sessions, conversation_id=cid, server_id=sid, run_id=uuid.UUID(RUN_1)
        )
        assert await _fetchval(
            sessions,
            "SELECT COUNT(*) FROM conversation_runs WHERE conversation_id = :cid",
            cid=cid,
        ) == 1
        released = await arelease_run(
            sessions, conversation_id=cid, server_id=sid, run_id=uuid.UUID(RUN_1)
        )
        assert released.released_at is not None
        assert await aactive_run(sessions, cid, server_id=sid) is None
        # Release is idempotent.
        again = await arelease_run(
            sessions, conversation_id=cid, server_id=sid, run_id=uuid.UUID(RUN_1)
        )
        assert again.released_at is not None
        with pytest.raises(ValueError, match="no conversation_runs link"):
            await arelease_run(
                sessions, conversation_id=cid, server_id=sid, run_id=uuid.UUID(RUN_2)
            )

    @pytest.mark.anyio
    async def test_steer_enqueue_is_atomic_and_idempotent(self, sessions) -> None:
        conv = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        cid, sid = conv.conversation_id, uuid.UUID(SERVER_A)
        key = uuid.uuid4()
        first, disposition = await aenqueue_steering(
            sessions,
            conversation_id=cid,
            server_id=sid,
            action=SteerAction.STEER,
            client_request_id=key,
            content="switch to the staging target after this step",
        )
        assert disposition is TurnDisposition.CREATED
        assert first.delivery_status == "pending"
        assert first.applied_at is None
        # Crash-recovery replay lands on the SAME durable row.
        replay, replay_disposition = await aenqueue_steering(
            sessions,
            conversation_id=cid,
            server_id=sid,
            action=SteerAction.STEER,
            client_request_id=key,
            content="switch to the staging target after this step",
        )
        assert replay_disposition is TurnDisposition.DEDUPLICATED
        assert replay.steering_id == first.steering_id
        assert await _fetchval(
            sessions,
            "SELECT COUNT(*) FROM conversation_steering WHERE conversation_id = :cid",
            cid=cid,
        ) == 1
        # Deterministic controls carry no planner input.
        pause, pause_disposition = await aenqueue_steering(
            sessions,
            conversation_id=cid,
            server_id=sid,
            action=SteerAction.PAUSE,
            client_request_id=uuid.uuid4(),
        )
        assert pause_disposition is TurnDisposition.CREATED
        assert pause.content is None
        with pytest.raises(ValueError, match="carry no message"):
            await aenqueue_steering(
                sessions,
                conversation_id=cid,
                server_id=sid,
                action=SteerAction.CANCEL,
                client_request_id=uuid.uuid4(),
                content="cancel but also do this",
            )
        with pytest.raises(ValueError, match="non-blank"):
            await aenqueue_steering(
                sessions,
                conversation_id=cid,
                server_id=sid,
                action=SteerAction.STEER,
                client_request_id=uuid.uuid4(),
                content="  ",
            )

    @pytest.mark.anyio
    async def test_steer_application_is_scoped_and_idempotent(self, sessions) -> None:
        conv_a = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        conv_b = await acreate_conversation(sessions, server_id=uuid.UUID(SERVER_A))
        sid = uuid.UUID(SERVER_A)
        steer, _ = await aenqueue_steering(
            sessions,
            conversation_id=conv_a.conversation_id,
            server_id=sid,
            action=SteerAction.PAUSE,
            client_request_id=uuid.uuid4(),
        )
        applied = await aapply_steering(
            sessions,
            conversation_id=conv_a.conversation_id,
            server_id=sid,
            steering_id=steer.steering_id,
        )
        assert isinstance(applied, SteeringRow)
        assert applied.delivery_status == "applied"
        assert applied.applied_at is not None
        # Idempotent re-apply keeps the durable state.
        reapplied = await aapply_steering(
            sessions,
            conversation_id=conv_a.conversation_id,
            server_id=sid,
            steering_id=steer.steering_id,
        )
        assert reapplied.delivery_status == "applied"
        # Cross-conversation steering ids do not exist in the other scope.
        with pytest.raises(ValueError, match="no steering row"):
            await aapply_steering(
                sessions,
                conversation_id=conv_b.conversation_id,
                server_id=sid,
                steering_id=steer.steering_id,
            )
        # C8: steering apply from another server fails closed.
        with pytest.raises(ConversationContractError):
            await aapply_steering(
                sessions,
                conversation_id=conv_a.conversation_id,
                server_id=uuid.UUID(SERVER_B),
                steering_id=steer.steering_id,
            )
