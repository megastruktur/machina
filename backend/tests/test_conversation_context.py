"""Focused tests for conversation context assembly (06-context-continuity).

Acceptance (BRIEF, act_CONTRACTS §3–§4, act_RUNTIME C1/C2/C3/C4/C7):

1. **Canonical assistant producer & native multi-turn continuity** — the
   trusted producer boundary records ONE assistant row per run
   (idempotent per ``run_id`` via the safe ``content_ref`` marker), and a
   follow-up run's seed carries prior user/assistant turns as NATIVE
   ``ModelRequest(parts=[UserPromptPart])`` /
   ``ModelResponse(parts=[TextPart])`` messages in chronological order —
   zero synthetic wrappers, zero authority elevation (C1/C2).
2. **Budget overflow honesty** — oversized histories keep the most
   recent turns verbatim and compact older turns into ONE bounded
   provenance-carrying block on the SYSTEM channel (the pinned harness's
   ``SUMMARY_PREFIX`` summary form); counts are explicit, recent turns
   and the new user request are never dropped or duplicated (C3).
3. **Legacy honesty** — ``partial``/``unavailable`` completeness and the
   missing range are carried verbatim into the report; authoritative
   ``chat.submitted`` job events count as proven-but-unrecoverable
   submissions and are NEVER converted into fabricated turns or
   placeholder text (C7). Non-chat rows are skipped and counted, never
   silently dropped.
4. **Executor wiring** — ``AgentRunExecutor.abuild_conversation_seed``
   resolves the run's conversation link (fallback: the plain system+user
   seed), emits the value-free ``agent.conversation_history`` report and
   returns the checkpointed seed;
   ``AgentRunExecutor.arecord_assistant_reply`` records the canonical
   reply exactly once (producer idempotency, C4). The live multi-turn
   journey through HTTP + the real DBOS worker is the 00-runtime
   fixture's proof (see this card's evidence directory).

Boundary discipline (test policy): storage/producer/assembly claims run
against a real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``)
with the real linear Alembic chain, in-process (no subprocess pools).
The executor is the REAL composed ``AgentRunExecutor``; only the model
layer is unused here (assembly happens before any model call). Fixtures
carry only fixed opaque UUIDs and synthetic text — never secret values.
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents import conversation_context as cc
from mai.agents import run as agent_run
from mai.agents.conversation_store import (
    StoredMessage,
    aappend_producer_message,
    aattach_run,
    acreate_conversation,
    alist_messages,
    arelease_run,
    asubmit_turn,
)
from mai.agents.model_store import ModelConfigStore
from mai.capabilities.dispatch import CapabilityDispatcher
from mai.contracts.conversations import (
    ConversationContractError,
    HistoryCompleteness,
    MessageRange,
    MessageRole,
    MessageSourceKind,
    content_fingerprint,
)
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import ExecutionContext, Policy, PolicyMode

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"
ALEMBIC_INI = str(BACKEND_ROOT / "alembic.ini")

SERVER_ID = "6a91c2e4-1b3d-4f5a-8c7e-2d3b4a5f6e70"
SERVER_UUID = uuid.UUID(SERVER_ID)
SYSTEM_PROMPT = "You administer the continuity fixture host.\n"
READ_CAP = "fixture.read"

T1_USER = "Plan only; do not change files"
T1_ASSISTANT = "Understood: plan only, no file changes."
T2_USER = "continue"
T3_USER = "continue please"


# ---------------------------------------------------------------------------
# storage row builders
# ---------------------------------------------------------------------------


def _stored(
    sequence: int,
    role: MessageRole,
    content: str,
    *,
    source: MessageSourceKind = MessageSourceKind.API,
    client_request_id: uuid.UUID | None = None,
    redacted: bool = False,
) -> StoredMessage:
    return StoredMessage(
        conversation_id=SERVER_UUID,
        message_id=uuid.uuid4(),
        sequence=sequence,
        role=role,
        source_kind=source,
        content=content,
        content_fingerprint=content_fingerprint(content),
        redacted=redacted,
        content_ref=None,
        client_request_id=client_request_id,
        created_at=datetime.now(timezone.utc),
    )


def _messages_of(history_json: str) -> list:
    return ModelMessagesTypeAdapter.validate_json(history_json.encode("utf-8"))


# ---------------------------------------------------------------------------
# part A — pure native-turn shape (no DB; the architectural hard rule)
# ---------------------------------------------------------------------------


class TestNativeTurnShape:
    def test_user_and_assistant_turns_are_role_preserving_native_messages(
        self,
    ) -> None:
        rows = [
            _stored(1, MessageRole.USER, T1_USER),
            _stored(2, MessageRole.ASSISTANT, T1_ASSISTANT),
            _stored(3, MessageRole.USER, T2_USER),
        ]
        messages, users, assistants, skipped, excluded = cc.native_turns(
            rows, excluded_run_id=None, user_message="go"
        )
        assert (users, assistants, skipped, excluded) == (2, 1, 0, 0)
        assert isinstance(messages[0], ModelRequest)
        assert isinstance(messages[0].parts[0], UserPromptPart)
        assert messages[0].parts[0].content == T1_USER
        assert isinstance(messages[1], ModelResponse)
        assert isinstance(messages[1].parts[0], TextPart)
        assert messages[1].parts[0].content == T1_ASSISTANT
        assert isinstance(messages[2], ModelRequest)
        # Assistant text can NEVER ride user/system authority channels.
        for message in messages:
            if isinstance(message, ModelResponse):
                continue
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    assert part.content != T1_ASSISTANT
                if isinstance(part, SystemPromptPart):
                    assert part.content != T1_ASSISTANT

    def test_current_turn_row_excluded_by_client_request_id(self) -> None:
        run_id = uuid.uuid4()
        rows = [
            _stored(1, MessageRole.USER, T1_USER),
            _stored(2, MessageRole.ASSISTANT, T1_ASSISTANT),
            _stored(3, MessageRole.USER, T2_USER, client_request_id=run_id),
        ]
        messages, _, _, _, excluded = cc.native_turns(
            rows, excluded_run_id=str(run_id), user_message=T2_USER
        )
        assert excluded == 1
        assert [type(m).__name__ for m in messages] == [
            "ModelRequest",
            "ModelResponse",
        ]

    def test_current_turn_row_excluded_defensively_by_content(self) -> None:
        rows = [
            _stored(1, MessageRole.USER, T1_USER),
            _stored(2, MessageRole.USER, T2_USER),
        ]
        messages, _, _, _, excluded = cc.native_turns(
            rows, excluded_run_id=None, user_message=T2_USER
        )
        assert excluded == 1
        assert len(messages) == 1

    def test_non_chat_rows_skipped_and_counted(self) -> None:
        rows = [
            _stored(1, MessageRole.USER, T1_USER),
            _stored(
                2,
                MessageRole.TOOL,
                "tool receipt",
                source=MessageSourceKind.SYSTEM,
            ),
            _stored(
                3,
                MessageRole.SYSTEM,
                "platform note",
                source=MessageSourceKind.SYSTEM,
            ),
            _stored(4, MessageRole.ASSISTANT, T1_ASSISTANT),
        ]
        _, users, assistants, skipped, _ = cc.native_turns(
            rows, excluded_run_id=None, user_message="go"
        )
        assert (users, assistants, skipped) == (1, 1, 2)

    def test_seed_budget_leaves_run_checkpoint_headroom(self) -> None:
        assert cc.SEED_BUDGET_BYTES * 2 <= agent_run.MAX_HISTORY_BYTES


class TestCompactionBlock:
    def test_block_carries_harness_summary_prefix_and_provenance(self) -> None:
        rows = [
            _stored(1, MessageRole.USER, T1_USER),
            _stored(2, MessageRole.ASSISTANT, T1_ASSISTANT, redacted=True),
        ]
        block, elided = cc.compact_block_text(rows, conversation_id="cid")
        assert block.startswith("Summary of previous conversation:\n\n")
        assert "[turn 1 | user | api]" in block and T1_USER in block
        assert "[turn 2 | assistant | api | redacted]" in block
        assert elided == 0

    def test_block_excerpts_bounded(self) -> None:
        rows = [_stored(1, MessageRole.USER, "A" * 5000)]
        block, elided = cc.compact_block_text(rows, conversation_id="cid")
        assert elided == 0
        assert "[turn 1 | user | api] AAA" in block
        assert "..." in block  # the per-excerpt truncation marker

    def test_block_bites_with_explicit_elision_count(self, monkeypatch) -> None:
        monkeypatch.setattr(cc, "COMPACTED_BLOCK_MAX_CHARS", 700)
        rows = [
            _stored(index, MessageRole.USER, "B" * 300)
            for index in range(1, 6)
        ]
        block, elided = cc.compact_block_text(rows, conversation_id="cid")
        assert elided > 0
        assert f"[...] {elided} older compacted turn" in block
        assert len(block) <= 700 + len(
            f"\n[...] {elided} older compacted turn excerpt(s) elided at "
            "the block bound."
        )


# ---------------------------------------------------------------------------
# disposable PostgreSQL fixtures (real chain, in-process; no pools)
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _drop_all(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS conversation_steering, conversation_runs, "
            "conversation_messages, conversations, job_resource_locks, "
            "job_schedule_fires, job_schedules, job_events, job_steps, "
            "job_runs, approvals, profile_projection_journal, "
            "profile_projection, admin_sessions, admin_credential, "
            "alembic_version, notification_alerts, observation_findings, "
            "server_observations, knowledge_doc_chunks, knowledge_documents, "
            "application_projection_journal, application_projection"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through the head."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the continuity claims need a real "
            "disposable Postgres fixture"
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
    """NullPool session factory; empty conversation+jobs domain per test."""
    engine, factory = create_app_engine(migrated_url)
    yield factory
    async with factory() as session:
        for table in (
            "conversation_steering",
            "conversation_runs",
            "conversation_messages",
            "conversations",
            "job_resource_locks",
            "job_schedule_fires",
            "job_steps",
            "job_events",
            "job_schedules",
            "job_runs",
        ):
            present = await session.execute(
                text("SELECT to_regclass(:t) IS NOT NULL"),
                {"t": f"public.{table}"},
            )
            if not present.scalar():
                continue  # engine-owned tables may not exist pre-launch
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()
    await engine.dispose()


async def _seed_followup_conversation(
    sessions: async_sessionmaker[AsyncSession],
    *,
    run1: uuid.UUID,
    run2: uuid.UUID,
    assistant_text: str = T1_ASSISTANT,
) -> uuid.UUID:
    """Canonical turn-1 + recorded assistant reply + queued turn-2.

    Both runs attach to the conversation exactly like the canonical
    submit seam (05-turn-submit): the worker's conversation discovery
    resolves through ``conversation_runs``.
    """
    conversation = await acreate_conversation(
        sessions, server_id=SERVER_UUID, title=None
    )
    await asubmit_turn(
        sessions,
        conversation_id=conversation.conversation_id,
        server_id=SERVER_UUID,
        client_request_id=run1,
        message=T1_USER,
    )
    await aappend_producer_message(
        sessions,
        conversation_id=conversation.conversation_id,
        server_id=SERVER_UUID,
        role=MessageRole.ASSISTANT,
        source_kind=MessageSourceKind.SYSTEM,
        content=assistant_text,
        content_ref=f"{cc.PRODUCER_CONTENT_REF_PREFIX}{run1}",
    )
    await aattach_run(
        sessions,
        conversation_id=conversation.conversation_id,
        server_id=SERVER_UUID,
        run_id=run1,
    )
    await asubmit_turn(
        sessions,
        conversation_id=conversation.conversation_id,
        server_id=SERVER_UUID,
        client_request_id=run2,
        message=T2_USER,
    )
    # The canonical seam's lease hand-over: the terminal run releases
    # before the follow-up attaches (one active writer, §1).
    await arelease_run(
        sessions,
        conversation_id=conversation.conversation_id,
        server_id=SERVER_UUID,
        run_id=run1,
    )
    await aattach_run(
        sessions,
        conversation_id=conversation.conversation_id,
        server_id=SERVER_UUID,
        run_id=run2,
    )
    return conversation.conversation_id


async def _insert_run_row(
    sessions: async_sessionmaker[AsyncSession], run_id: str
) -> None:
    """One ``job_runs`` row (the event stream's FK parent)."""
    async with sessions() as session:
        await session.execute(
            text(
                """
                INSERT INTO job_runs
                    (run_id, server_id, depth, profile_revision,
                     prompt_revision, model_profile, requested_grants,
                     submitted_at, updated_at)
                VALUES
                    (:run_id, :server_id, 0, 3, 7, 'fixture-fast',
                     :grants, now(), now())
                """
            ),
            {
                "run_id": uuid.UUID(run_id),
                "server_id": SERVER_UUID,
                "grants": json.dumps([READ_CAP]),
            },
        )
        await session.commit()


async def _events_of(
    sessions: async_sessionmaker[AsyncSession], run_id: str
) -> list[dict[str, object]]:
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT seq, type, data FROM job_events "
                    "WHERE run_id = :rid ORDER BY seq"
                ),
                {"rid": uuid.UUID(run_id)},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# part B — store-bound assembly + producer (real disposable PostgreSQL)
# ---------------------------------------------------------------------------


class TestConversationSeed:
    @pytest.mark.anyio
    async def test_followup_seed_is_native_alternating_history(
        self, sessions
    ) -> None:
        run1, run2 = uuid.uuid4(), uuid.uuid4()
        cid = await _seed_followup_conversation(sessions, run1=run1, run2=run2)
        seed = await cc.abuild_conversation_seed(
            sessions,
            conversation_id=str(cid),
            server_id=SERVER_ID,
            system_prompt=SYSTEM_PROMPT,
            user_message=T2_USER,
            run_id=str(run2),
        )
        messages = _messages_of(seed.history_json)
        assert [type(m).__name__ for m in messages] == [
            "ModelRequest",  # system
            "ModelRequest",  # user turn 1
            "ModelResponse",  # assistant turn 1
            "ModelRequest",  # the NEW user turn, LAST
        ]
        assert isinstance(messages[0].parts[0], SystemPromptPart)
        assert messages[0].parts[0].content == SYSTEM_PROMPT
        assert messages[1].parts[0].content == T1_USER
        assert messages[2].parts[0].content == T1_ASSISTANT
        assert messages[3].parts[0].content == T2_USER
        report = seed.report
        assert report.prior_turns_total == 3
        assert report.turns_included_verbatim == 2
        assert report.turns_compacted == 0
        assert report.current_turn_rows_excluded == 1
        assert report.history_completeness == "complete"
        assert report.conversation_linked is True
        assert report.seed_bytes <= cc.SEED_BUDGET_BYTES

    @pytest.mark.anyio
    async def test_scope_mismatch_fails_closed(self, sessions) -> None:
        run1, run2 = uuid.uuid4(), uuid.uuid4()
        cid = await _seed_followup_conversation(sessions, run1=run1, run2=run2)
        with pytest.raises(ConversationContractError) as error:
            await cc.abuild_conversation_seed(
                sessions,
                conversation_id=str(cid),
                server_id=str(uuid.uuid4()),  # another server's scope
                system_prompt=SYSTEM_PROMPT,
                user_message=T2_USER,
                run_id=str(run2),
            )
        assert error.value.fields["server_id"] == "conversation_not_found"

    @pytest.mark.anyio
    async def test_budget_overflow_keeps_recent_and_compacts_older(
        self, sessions
    ) -> None:
        conversation = await acreate_conversation(
            sessions, server_id=SERVER_UUID
        )
        cid = conversation.conversation_id
        total = 120
        big = "C" * 20_000
        for index in range(1, total + 1):
            role = MessageRole.USER if index % 2 else MessageRole.ASSISTANT
            if role is MessageRole.USER:
                await asubmit_turn(
                    sessions,
                    conversation_id=cid,
                    server_id=SERVER_UUID,
                    client_request_id=uuid.uuid4(),
                    message=big,
                )
            else:
                await aappend_producer_message(
                    sessions,
                    conversation_id=cid,
                    server_id=SERVER_UUID,
                    role=MessageRole.ASSISTANT,
                    source_kind=MessageSourceKind.SYSTEM,
                    content=big,
                    content_ref=f"agent-run:big-{index}",
                )
        current = "final bounded turn"
        seed = await cc.abuild_conversation_seed(
            sessions,
            conversation_id=str(cid),
            server_id=SERVER_ID,
            system_prompt=SYSTEM_PROMPT,
            user_message=current,
            run_id=str(uuid.uuid4()),  # no row match: content fallback
        )
        report = seed.report
        assert report.prior_turns_total == total
        assert report.turns_included_verbatim >= 1
        assert report.turns_compacted == total - report.turns_included_verbatim
        assert report.turns_compacted > 0
        assert report.seed_bytes <= cc.SEED_BUDGET_BYTES
        messages = _messages_of(seed.history_json)
        # The new user turn is ALWAYS the final message, never duplicated.
        assert isinstance(messages[-1], ModelRequest)
        assert messages[-1].parts[0].content == current
        assert (
            sum(
                1
                for m in messages
                if isinstance(m, ModelRequest)
                and any(
                    isinstance(p, UserPromptPart) and p.content == current
                    for p in m.parts
                )
            )
            == 1
        )
        # Recent prior turns stay verbatim native messages (C3: no silent
        # drop of the most recent user constraint). Rows alternate
        # user/assistant from user, so the last row (120) is an assistant
        # response and the second-to-last is the most recent user turn.
        assert isinstance(messages[-2], ModelResponse)
        assert messages[-2].parts[0].content == big
        assert isinstance(messages[-3], ModelRequest)
        assert messages[-3].parts[0].content == big
        # The compaction block rides the SYSTEM channel with provenance.
        block = messages[1]
        assert isinstance(block, ModelRequest)
        assert isinstance(block.parts[0], SystemPromptPart)
        assert block.parts[0].content.startswith(
            "Summary of previous conversation:\n\n"
        )
        assert "[turn 1 | user | api]" in block.parts[0].content
        assert report.excerpts_elided >= 1
        assert f"[...] {report.excerpts_elided} older compacted turn" in (
            block.parts[0].content
        )

    @pytest.mark.anyio
    async def test_legacy_partial_history_reports_gap_never_fabricates(
        self, sessions
    ) -> None:
        conversation = await acreate_conversation(
            sessions,
            server_id=SERVER_UUID,
            history_completeness=HistoryCompleteness.PARTIAL,
            missing_range=MessageRange(from_sequence=1, to_sequence=2),
        )
        cid = conversation.conversation_id
        # The only recoverable row: an authoritative legacy-import user row.
        await aappend_producer_message(
            sessions,
            conversation_id=cid,
            server_id=SERVER_UUID,
            role=MessageRole.USER,
            source_kind=MessageSourceKind.LEGACY_IMPORT,
            content="original user constraint: keep the service up",
        )
        # A provable-but-unrecoverable legacy submission (content lives
        # nowhere in app-owned records): one job_runs row + its event.
        legacy_run = uuid.uuid4()
        await _insert_run_row(sessions, str(legacy_run))
        async with sessions() as session:
            await session.execute(
                text(
                    "INSERT INTO job_events (event_id, run_id, seq, ts, "
                    "type, v, data) VALUES (:eid, :rid, 1, now(), "
                    "'chat.submitted', 1, :data)"
                ),
                {
                    "eid": uuid.uuid4(),
                    "rid": legacy_run,
                    "data": json.dumps({"conversation_id": str(cid)}),
                },
            )
            await session.commit()
        seed = await cc.abuild_conversation_seed(
            sessions,
            conversation_id=str(cid),
            server_id=SERVER_ID,
            system_prompt=SYSTEM_PROMPT,
            user_message="continue",
            run_id=str(uuid.uuid4()),
        )
        report = seed.report
        assert report.history_completeness == "partial"
        assert report.missing_from_sequence == 1
        assert report.missing_to_sequence == 2
        assert report.legacy_proven_submissions == 1
        assert report.turns_included_verbatim == 1
        messages = _messages_of(seed.history_json)
        # ONLY the authoritative row became a turn; the gap is reported,
        # never filled with a placeholder or fabricated turn.
        bodies = [
            part.content
            for m in messages
            for part in m.parts
            if isinstance(part, (UserPromptPart, TextPart))
        ]
        assert bodies == [
            "original user constraint: keep the service up",
            "continue",
        ]
        assert all("missing" not in body.lower() for body in bodies)


class TestAssistantProducer:
    @pytest.mark.anyio
    async def test_reply_recorded_once_and_idempotent_per_run(
        self, sessions
    ) -> None:
        conversation = await acreate_conversation(
            sessions, server_id=SERVER_UUID
        )
        cid = conversation.conversation_id
        run_id = uuid.uuid4()
        first = await cc.arecord_assistant_reply(
            sessions,
            conversation_id=str(cid),
            server_id=SERVER_ID,
            run_id=str(run_id),
            content=T1_ASSISTANT,
        )
        assert first is not None
        assert first.role is MessageRole.ASSISTANT
        assert first.source_kind is MessageSourceKind.SYSTEM
        assert first.content_ref == f"{cc.PRODUCER_CONTENT_REF_PREFIX}{run_id}"
        # Replay / restart / crash-before-checkpoint: still exactly one row.
        again = await cc.arecord_assistant_reply(
            sessions,
            conversation_id=str(cid),
            server_id=SERVER_ID,
            run_id=str(run_id),
            content="a different replay text must NOT duplicate",
        )
        assert again is not None and again.message_id == first.message_id
        page = await alist_messages(
            sessions, cid, server_id=SERVER_UUID, limit=200
        )
        assistants = [item for item in page.items if item.role is MessageRole.ASSISTANT]
        assert len(assistants) == 1
        assert assistants[0].content == T1_ASSISTANT

    @pytest.mark.anyio
    async def test_blank_output_records_nothing(self, sessions) -> None:
        conversation = await acreate_conversation(
            sessions, server_id=SERVER_UUID
        )
        cid = conversation.conversation_id
        recorded = await cc.arecord_assistant_reply(
            sessions,
            conversation_id=str(cid),
            server_id=SERVER_ID,
            run_id=str(uuid.uuid4()),
            content="   \n  ",
        )
        assert recorded is None
        page = await alist_messages(
            sessions, cid, server_id=SERVER_UUID, limit=10
        )
        assert page.total == 0


# ---------------------------------------------------------------------------
# part C — the REAL AgentRunExecutor wiring, in-process (no subprocess)
# ---------------------------------------------------------------------------


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
    return registry


def _refusing_executor():
    async def _execute(call) -> object:
        raise AssertionError(f"executor invoked for a denied call: {call}")

    return _execute


def _real_dispatcher(
    sessions: async_sessionmaker[AsyncSession],
) -> CapabilityDispatcher:
    return CapabilityDispatcher(
        registry=_real_registry(),
        approvals=ApprovalStore(sessions),
        grants=lambda: {READ_CAP: frozenset({SERVER_UUID})},
        policy=lambda: Policy(revision=1, denied_capabilities=frozenset()),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(
            str(BACKEND_ROOT / ".." / "absent-fixture.env")
        ),
        executor=_refusing_executor(),
    )


@pytest.fixture
async def executor(
    sessions, tmp_path: pathlib.Path
) -> AsyncIterator[agent_run.AgentRunExecutor]:
    """The REAL composed executor over the REAL session factory.

    The seed/producer seams run BEFORE any model call, so the model
    layer stays uncomposed; assembly and recording exercise the real
    store, the real event stream and the real report shape.
    """
    yield agent_run.AgentRunExecutor(
        sessions=sessions,
        dispatcher=_real_dispatcher(sessions),
        registry=_real_registry(),
        model_store=ModelConfigStore(tmp_path / "model-root"),
        data_root=str(tmp_path / "data-root"),
        locks=ResourceLockService(
            "postgresql://unused:unused@127.0.0.1:1/unused"
        ),
    )


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _intent(run_id: str, task: str) -> agent_run.AgentRunIntent:
    context = ExecutionContext(
        principal="hash:fixture-principal",
        server_id=SERVER_UUID,
        run_id=uuid.UUID(run_id),
        parent_scope=None,
        profile_revision="profile-1",
        prompt_revision="prompt-1",
        credential_references=(),
        allowed_capabilities=(READ_CAP,),
        mode=PolicyMode("delegated"),
    )
    document = {
        "v": agent_run.RUN_PAYLOAD_VERSION,
        "run_id": run_id,
        "server_id": SERVER_ID,
        "context": context.to_json(),
        "role": "operator assistant",
        "task": task,
        "connection": "gateway",
        "profile": "fast",
    }
    return agent_run.parse_agent_run_intent(_canonical(document))


class TestExecutorWiring:
    @pytest.mark.anyio
    async def test_executor_seed_for_followup_is_native_and_reported(
        self, sessions, executor
    ) -> None:
        run1, run2 = uuid.uuid4(), uuid.uuid4()
        await _seed_followup_conversation(sessions, run1=run1, run2=run2)
        await _insert_run_row(sessions, str(run2))
        result = executor.run(
            executor.abuild_conversation_seed(
                _intent(str(run2), T2_USER),
                {"system_prompt": SYSTEM_PROMPT, "user_message": T2_USER},
            )
        )
        messages = _messages_of(str(result["history_json"]))
        assert [type(m).__name__ for m in messages] == [
            "ModelRequest",
            "ModelRequest",
            "ModelResponse",
            "ModelRequest",
        ]
        assert messages[1].parts[0].content == T1_USER
        assert messages[2].parts[0].content == T1_ASSISTANT
        assert messages[3].parts[0].content == T2_USER
        report = dict(result["report"])
        assert report["conversation_linked"] is True
        assert report["turns_included_verbatim"] == 2
        assert report["current_turn_rows_excluded"] == 1
        events = await _events_of(sessions, str(run2))
        history_events = [
            e for e in events if e["type"] == "agent.conversation_history"
        ]
        assert len(history_events) == 1
        assert history_events[0]["data"]["conversation_linked"] is True
        assert history_events[0]["data"]["prior_turns_total"] == 3

    @pytest.mark.anyio
    async def test_executor_seed_without_link_falls_back_plain(
        self, sessions, executor
    ) -> None:
        run_id = str(uuid.uuid4())
        # The engine's run row always exists by execution time (even for
        # a legacy run); the report event needs its FK parent.
        await _insert_run_row(sessions, run_id)
        result = executor.run(
            executor.abuild_conversation_seed(
                _intent(run_id, T1_USER),
                {"system_prompt": SYSTEM_PROMPT, "user_message": T1_USER},
            )
        )
        # The exact legacy seed shape (system + user): identical parsed
        # structure and content (pydantic_ai stamps part timestamps, so
        # byte equality does not apply across two serializations).
        legacy = _messages_of(
            agent_run.initial_history_json(SYSTEM_PROMPT, T1_USER)
        )
        built = _messages_of(str(result["history_json"]))
        assert len(built) == len(legacy) == 2
        for got, want in zip(built, legacy):
            assert type(got) is type(want)
            assert [p.content for p in got.parts] == [
                p.content for p in want.parts
            ]
        report = dict(result["report"])
        assert report["conversation_linked"] is False
        assert report["history_completeness"] == "unavailable"

    @pytest.mark.anyio
    async def test_executor_producer_records_once_per_run(
        self, sessions, executor
    ) -> None:
        run_id = str(uuid.uuid4())
        conversation = await acreate_conversation(
            sessions, server_id=SERVER_UUID
        )
        cid = conversation.conversation_id
        await _insert_run_row(sessions, run_id)
        await aattach_run(
            sessions,
            conversation_id=cid,
            server_id=SERVER_UUID,
            run_id=uuid.UUID(run_id),
        )
        recorded = executor.run(
            executor.arecord_assistant_reply(_intent(run_id, T2_USER), T1_ASSISTANT)
        )
        assert recorded["recorded"] is True
        message_id = recorded["message_id"]
        # A replayed workflow step (same run, different text) dedups.
        replayed = executor.run(
            executor.arecord_assistant_reply(
                _intent(run_id, T2_USER), "replay text differs"
            )
        )
        assert replayed["recorded"] is True
        assert replayed["message_id"] == message_id
        async def _count() -> int:
            return len(
                (
                    await alist_messages(
                        sessions, cid, server_id=SERVER_UUID, limit=200
                    )
                ).items
            )
        assert await _count() == 1

    @pytest.mark.anyio
    async def test_executor_producer_without_link_records_nothing(
        self, executor
    ) -> None:
        run_id = str(uuid.uuid4())
        recorded = executor.run(
            executor.arecord_assistant_reply(_intent(run_id, T1_USER), T1_ASSISTANT)
        )
        assert recorded == {
            "recorded": False,
            "reason": "no_conversation_link",
        }

    @pytest.mark.anyio
    async def test_three_turn_seed_shape_in_process(
        self, sessions, executor
    ) -> None:
        """The in-process three-turn native-history proof.

        After TWO produced assistant turns, the third run's seed is
        exactly ``[Sys, U1, A1, U2, A2, U3]`` — native alternating
        messages, the new user turn last, prior assistant text never on
        a user/system channel.
        """
        runs = [str(uuid.uuid4()) for _ in range(3)]
        users = [T1_USER, T2_USER, T3_USER]
        async def _build_history() -> uuid.UUID:
            conversation = await acreate_conversation(
                sessions, server_id=SERVER_UUID
            )
            cid = conversation.conversation_id
            for index, run_id in enumerate(runs):
                await asubmit_turn(
                    sessions,
                    conversation_id=cid,
                    server_id=SERVER_UUID,
                    client_request_id=uuid.UUID(run_id),
                    message=users[index],
                )
                if index < len(runs) - 1:
                    # Production ordering: the assistant reply for a run
                    # is recorded AFTER that run finishes, so the last
                    # (current) turn has no reply row yet when the NEXT
                    # seed is assembled — mirror that here for run3.
                    await aappend_producer_message(
                        sessions,
                        conversation_id=cid,
                        server_id=SERVER_UUID,
                        role=MessageRole.ASSISTANT,
                        source_kind=MessageSourceKind.SYSTEM,
                        content=f"{T1_ASSISTANT} (turn {index + 1})",
                        content_ref=f"{cc.PRODUCER_CONTENT_REF_PREFIX}{run_id}",
                    )
                if index > 0:
                    # lease hand-over: release the previous writer first
                    await arelease_run(
                        sessions,
                        conversation_id=cid,
                        server_id=SERVER_UUID,
                        run_id=uuid.UUID(runs[index - 1]),
                    )
                await aattach_run(
                    sessions,
                    conversation_id=cid,
                    server_id=SERVER_UUID,
                    run_id=uuid.UUID(run_id),
                )
            return cid
        await _build_history()
        await _insert_run_row(sessions, runs[2])
        run3 = _intent(runs[2], T3_USER)
        result = executor.run(
            executor.abuild_conversation_seed(
                run3, {"system_prompt": SYSTEM_PROMPT, "user_message": T3_USER}
            )
        )
        messages = _messages_of(str(result["history_json"]))
        assert [type(m).__name__ for m in messages] == [
            "ModelRequest",
            "ModelRequest",
            "ModelResponse",
            "ModelRequest",
            "ModelResponse",
            "ModelRequest",
        ]
        assert [m.parts[0].content for m in messages[1:]] == [
            T1_USER,
            f"{T1_ASSISTANT} (turn 1)",
            T2_USER,
            f"{T1_ASSISTANT} (turn 2)",
            T3_USER,
        ]
        assert isinstance(messages[0].parts[0], SystemPromptPart)
        assert messages[0].parts[0].content == SYSTEM_PROMPT
        for message in messages:
            if isinstance(message, ModelResponse):
                continue
            for part in message.parts:
                if isinstance(part, (UserPromptPart, SystemPromptPart)):
                    assert "(turn" not in part.content
