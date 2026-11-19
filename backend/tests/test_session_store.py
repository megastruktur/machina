"""Focused tests for the D02 transactional session store (real Postgres).

Acceptance (BRIEF_D02 §B/§C; sfc_PLAN.md D02; sfc_RESEARCH §8):

1. **Concurrent writers**: two independent connections append 1,000 entries
   each to the SAME session concurrently — 2,000 rows, per-session ``seq``
   unique and contiguous 1..2000, no lost commit.
2. **Idempotent retry**: replaying the same
   ``(run_id, durable_source_id, kind)`` 100 times produces exactly ONE
   entry; every replay returns the same entry id and ``seq``.
3. **Crash/no-gap**: an aborted append transaction consumes no sequence —
   the next successful append continues the contiguous sequence, and the
   aborted source's retry is idempotent.
4. **10,000-entry bounds**: the latest tail is ≤30 entries, keyset pages ≤50,
   every page ≤64 KiB serialized, the keyset plan is an index scan (never a
   full scan), and paging reconstructs the exact sequence without
   duplicates or gaps.
5. **Scope blindness**: a session of server A is invisible to server B for
   read, append, admission and tags — the same value-free
   ``session_not_found`` code everywhere, with no content in the message.
6. **Run-state authority**: one state update + one ``run_state`` entry in
   ONE transaction; a repeated same-state call does not duplicate.
7. **Generic survival**: after the reset, the retained generic tag/job
   surface still executes with its seeded rows.
8. **Root admission seam** (queued-chat hotfix S1): the root writer
   creates ``session_runs`` + the depth-0 ``job_runs`` facade + the
   mandatory ``job_tags`` classification in ONE transaction with real
   caller-supplied revisions/grants; matching replay converges (including
   pre-hotfix rows lacking the facade), cross-identity collisions fail
   closed, and a mid-transaction failure rolls back every insert.

Boundary discipline: every claim runs against a real disposable PostgreSQL
fixture (``MAI_TEST_DATABASE_URL``; the chain needs pgvector —
``pgvector/pgvector:pg16``) with the real linear Alembic chain applied.
No mocks, no fake sessions.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import psycopg
import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.sessions import (
    EntryKind,
    MAX_PAGE_BYTES,
    SessionContractError,
    SessionErrorCode,
    SessionRunState,
    SessionSourceKind,
)
from mai.database import create_app_engine
from mai.session_store import (
    aappend_entry,
    aset_generated_session_title,
    aattach_session_tag,
    acreate_root_run,
    acreate_run,
    acreate_session,
    aget_session,
    alist_provider_artifacts,
    alist_sessions,
    alist_session_tags,
    aread_session_history,
    arecord_admission,
    arecord_provider_artifact,
    arecord_run_state,
    _append_entry_tx,
    _lock_session,
)
from mai.tag_store import (
    RESERVED_UNCLASSIFIED_TAG_ID,
    SYSTEM_ASSIGNMENT_SOURCE,
    alist_tags,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
SERVER_B = uuid.UUID("9a8b7c6d-1e2f-4a3b-8c5d-6e7f8a9b0c1d")
SESSION_A = uuid.UUID("b2f3d4e5-0001-4001-8001-0000000000b1")
RUN_1 = uuid.UUID("a1e2c3d4-0001-4001-8001-0000000000a1")

#: The suites run async tests through the anyio pytest plugin (the repo's
#: only async driver), one backend: asyncio.
pytestmark = pytest.mark.anyio


def _alembic_config():
    from alembic.config import Config as AlembicConfig

    return AlembicConfig(ALEMBIC_INI)


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database upgraded from zero to the current head."""
    url = os.environ.get(LIVE_URL_VAR, "")
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the store claims need a real "
            "disposable Postgres fixture (pgvector/pgvector:pg16)"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
        from alembic import command

        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture(scope="session")
def factory(migrated_url: str) -> async_sessionmaker[AsyncSession]:
    _, sessions = create_app_engine(migrated_url)
    return sessions


@pytest.fixture(autouse=True)
async def _clean_sessions(factory: async_sessionmaker[AsyncSession]):
    """Fresh session-first domain per test (generic tables untouched)."""
    yield
    async with factory() as session:
        async with session.begin():
            await session.execute(
                sql_text(
                    "TRUNCATE session_tags, session_provider_artifacts, "
                    "session_admissions, session_runs, session_entries, "
                    "sessions CASCADE"
                )
            )


def _user_payload(n: int) -> dict[str, object]:
    from mai.contracts.sessions import UserMessageEntry

    return UserMessageEntry(
        kind=EntryKind.USER_MESSAGE,
        text=f"synthetic turn {n}",
        client_request_id=uuid.UUID(int=n),
        source=SessionSourceKind.INTERACTIVE,
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# 1. concurrent writers
# ---------------------------------------------------------------------------


async def test_two_concurrent_writers_produce_contiguous_unique_sequences(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await acreate_session(
        factory,
        server_id=SERVER_A,
        source=SessionSourceKind.INTERACTIVE,
        session_id=SESSION_A,
    )
    # Two INDEPENDENT sessionmakers (own pooled connections) so the appends
    # genuinely race for the session row lock instead of sharing a session.
    _, sessions_a = create_app_engine(_fixture_url())
    _, sessions_b = create_app_engine(_fixture_url())

    async def writer(
        sessions: async_sessionmaker[AsyncSession], tag: str, count: int
    ) -> None:
        for n in range(count):
            await aappend_entry(
                sessions,
                session_id=SESSION_A,
                server_id=SERVER_A,
                kind=EntryKind.USER_MESSAGE,
                payload=_user_payload(n),
                run_id=None,
                durable_source_id=f"{tag}-{n:05d}",
            )

    await asyncio.gather(
        writer(sessions_a, "a", 1000),
        writer(sessions_b, "b", 1000),
    )
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    sql_text(
                        "SELECT seq, durable_source_id FROM session_entries "
                        "WHERE session_id = :sid ORDER BY seq"
                    ),
                    {"sid": SESSION_A},
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 2000
    assert [r["seq"] for r in rows] == list(range(1, 2001))
    assert len({r["durable_source_id"] for r in rows}) == 2000
    print(
        "concurrency measurement: writers=2 x 1000 appends -> rows=2000, "
        "seq=1..2000 unique+contiguous, durable_source_ids=2000"
    )


def _fixture_url() -> str:
    url = os.environ.get("MAI_TEST_DATABASE_URL")
    assert url, "MAI_TEST_DATABASE_URL must be set for the fixture"
    return url


# ---------------------------------------------------------------------------
# 2. idempotent retry
# ---------------------------------------------------------------------------


async def test_hundred_replays_of_one_durable_source_yield_one_entry(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await acreate_session(
        factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
    )
    payload = {
        "kind": "assistant_message",
        "text": "synthetic assistant reply",
        "model_profile": "default",
    }
    seen: list[tuple[uuid.UUID, int]] = []
    for _ in range(100):
        row = await aappend_entry(
            factory,
            session_id=session_row.session_id,
            server_id=SERVER_A,
            kind=EntryKind.ASSISTANT_MESSAGE,
            payload=payload,
            run_id=RUN_1,
            durable_source_id="assistant-round-1",
        )
        seen.append((row.entry_id, row.seq))
    assert len(set(seen)) == 1  # same entry id AND same seq every replay
    async with factory() as session:
        total = (
            await session.execute(
                sql_text(
                    "SELECT COUNT(*) FROM session_entries "
                    "WHERE session_id = :sid"
                ),
                {"sid": session_row.session_id},
            )
        ).scalar_one()
    assert total == 1


# ---------------------------------------------------------------------------
# 3. crash / no-gap
# ---------------------------------------------------------------------------


async def test_aborted_append_consumes_no_sequence(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await acreate_session(
        factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
    )
    sid = session_row.session_id

    # A successful append first: the committed seq is 1.
    first = await aappend_entry(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        kind=EntryKind.USER_MESSAGE,
        payload=_user_payload(1),
        run_id=None,
        durable_source_id="u-1",
    )
    assert first.seq == 1

    # A crash INSIDE the append transaction (after allocation+insert).
    with pytest.raises(RuntimeError):
        async with factory() as session:
            async with session.begin():
                await _lock_session(
                    session, session_id=sid, server_id=SERVER_A
                )
                await _append_entry_tx(
                    session,
                    session_id=sid,
                    run_id=None,
                    kind=EntryKind.USER_MESSAGE,
                    payload=_user_payload(2),
                    durable_source_id="u-2",
                )
                raise RuntimeError("simulated crash before commit")

    # No row exists for the aborted source...
    async with factory() as session:
        count = (
            await session.execute(
                sql_text(
                    "SELECT COUNT(*) FROM session_entries "
                    "WHERE session_id = :sid"
                ),
                {"sid": sid},
            )
        ).scalar_one()
    assert count == 1

    # ...the next successful append continues the contiguous sequence...
    second = await aappend_entry(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        kind=EntryKind.USER_MESSAGE,
        payload=_user_payload(2),
        run_id=None,
        durable_source_id="u-2-retry",
    )
    assert second.seq == 2  # the aborted allocation was released, not consumed

    # ...and the aborted source's retry is idempotent.
    replay = await aappend_entry(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        kind=EntryKind.USER_MESSAGE,
        payload=_user_payload(2),
        run_id=None,
        durable_source_id="u-2-retry",
    )
    assert replay.entry_id == second.entry_id and replay.seq == 2

async def test_entry_payload_budget_counts_chars_not_escaped_ascii() -> None:
    """2026-11-12 incident regression: ``_validated_payload`` measures the
    entry budget in CHARACTERS (``ensure_ascii=False`` — the pydantic
    ``max_length`` unit). A 19k-char Cyrillic answer must not inflate to
    ~114k escaped-ASCII characters and reject the persist AFTER the model
    already succeeded; the over-budget rejection itself is unchanged."""
    from mai.contracts.sessions import MAX_ENTRY_TEXT_CHARS, AssistantMessageEntry
    from mai.session_store import _validated_payload

    def _assistant(text: str) -> dict[str, object]:
        return AssistantMessageEntry(
            kind=EntryKind.ASSISTANT_MESSAGE,
            text=text,
            model_profile="default",
        ).model_dump(mode="json")

    # Cyrillic at 19k chars passes and stays unescaped in the document...
    serialized = _validated_payload(_assistant("д" * 19_000))
    assert len(serialized) <= MAX_ENTRY_TEXT_CHARS
    assert "д" * 100 in serialized and "\\u" not in serialized
    # ...ASCII near the cap behaves identically...
    assert len(_validated_payload(_assistant("x" * 19_000))) <= MAX_ENTRY_TEXT_CHARS
    # ...and the over-budget rejection path is unchanged: 19 950 chars fit
    # the pydantic field but the serialized envelope still crosses the cap.
    with pytest.raises(SessionContractError) as err:
        _validated_payload(_assistant("x" * 19_950))
    # value-free summary: the field path + code, never content
    assert err.value.fields == {"payload": "forbidden_field"}


# ---------------------------------------------------------------------------
# 4. 10,000-entry bounds
# ---------------------------------------------------------------------------


async def test_ten_thousand_entry_bounded_pages(
    factory: async_sessionmaker[AsyncSession], migrated_url: str
) -> None:
    from mai.session_store import INITIAL_TAIL_MAX_ENTRIES
    from mai.session_store import HISTORY_PAGE_MAX_ENTRIES

    session_row = await acreate_session(
        factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
    )
    sid = session_row.session_id
    _bulk_seed_10k(migrated_url, sid, 10_000)

    # Latest tail: ≤30, ≤64 KiB, honest continuation metadata.
    page = await aread_session_history(
        factory, session_id=sid, server_id=SERVER_A
    )
    assert page.total == 10_000
    assert len(page.items) <= INITIAL_TAIL_MAX_ENTRIES
    assert page.byte_size <= MAX_PAGE_BYTES
    assert page.has_more is False  # the tail page is the newest window
    assert page.has_older is True
    assert page.next_before_seq == page.oldest_seq
    assert page.items[-1].seq == 10_000
    assert [item.seq for item in page.items] == sorted(
        item.seq for item in page.items
    )
    print(
        "10k measurement: total=10000 latest "
        f"items={len(page.items)} byte_size={page.byte_size} "
        f"truncated_by_bytes={page.truncated_by_bytes} "
        f"oldest={page.oldest_seq} latest={page.latest_seq}"
    )

    # Older page: ≤50, ≤64 KiB, cursor continues below the oldest seq.
    older = await aread_session_history(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        limit=HISTORY_PAGE_MAX_ENTRIES,
        before_seq=page.next_before_seq,
    )
    assert len(older.items) <= HISTORY_PAGE_MAX_ENTRIES
    assert older.byte_size <= MAX_PAGE_BYTES
    assert older.latest_seq < page.oldest_seq
    assert older.has_more is True  # newer entries exist above this page

    # Catch-up page: ascending, ≤50, ≤64 KiB.
    after = await aread_session_history(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        limit=HISTORY_PAGE_MAX_ENTRIES,
        after_seq=0,
    )
    assert len(after.items) <= HISTORY_PAGE_MAX_ENTRIES
    assert after.byte_size <= MAX_PAGE_BYTES
    assert after.items[0].seq == 1
    assert after.has_older is False

    # The keyset read plan is a bounded index scan, never a full scan.
    with psycopg.connect(migrated_url) as conn:
        explain = "\n".join(
            str(row[0])
            for row in conn.execute(
                """
                EXPLAIN (ANALYZE, BUFFERS)
                SELECT entry_id FROM session_entries
                WHERE session_id = %s AND seq < %s
                ORDER BY seq DESC
                LIMIT 50
                """,
                (sid, 5_000),
            ).fetchall()
        )
    print("EXPLAIN (ANALYZE, BUFFERS) keyset plan:\n" + explain)
    assert "Index Scan" in explain or "Index Only Scan" in explain
    assert "Seq Scan on session_entries" not in explain
    assert "rows=50" in explain  # the plan is bound by the LIMIT

    # Walk the whole history through the returned cursors: exact sequence,
    # no duplicates, no gaps.
    collected: list[int] = [item.seq for item in after.items]
    cursor = after.latest_seq
    while cursor is not None and cursor < 10_000:
        page_up = await aread_session_history(
            factory,
            session_id=sid,
            server_id=SERVER_A,
            limit=HISTORY_PAGE_MAX_ENTRIES,
            after_seq=cursor,
        )
        assert page_up.items
        collected.extend(item.seq for item in page_up.items)
        cursor = page_up.latest_seq
    assert collected == list(range(1, 10_001))

    collected_down: list[int] = [item.seq for item in page.items]
    before = page.next_before_seq
    while before is not None:
        page_down = await aread_session_history(
            factory,
            session_id=sid,
            server_id=SERVER_A,
            limit=HISTORY_PAGE_MAX_ENTRIES,
            before_seq=before,
        )
        assert page_down.items
        collected_down = [item.seq for item in page_down.items] + collected_down
        before = page_down.next_before_seq
    assert collected_down == list(range(1, 10_001))


def _bulk_seed_10k(url: str, session_id: uuid.UUID, count: int) -> None:
    """Bulk-seed ``count`` contiguous entries (migrated-history shape)."""
    from mai.contracts.sessions import EntryKind, SessionSourceKind, UserMessageEntry

    with psycopg.connect(url) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                for n in range(count):
                    payload = json.dumps(
                        UserMessageEntry(
                            kind=EntryKind.USER_MESSAGE,
                            text=f"seeded entry {n}",
                            client_request_id=uuid.UUID(int=n),
                            source=SessionSourceKind.INTERACTIVE,
                        ).model_dump(mode="json")
                    )
                    cur.execute(
                        """
                        INSERT INTO session_entries
                            (entry_id, session_id, run_id, seq, kind,
                             payload, durable_source_id)
                        VALUES (%s, %s, NULL, %s, 'user_message', %s::jsonb,
                                %s)
                        """,
                        (
                            uuid.uuid4(),
                            session_id,
                            n + 1,
                            payload,
                            f"seed-{n:06d}",
                        ),
                    )


async def test_byte_budget_truncates_oldest_items_honestly(
    factory: async_sessionmaker[AsyncSession], migrated_url: str
) -> None:
    """Five ~20k-char entries exceed the 64 KiB page budget by COUNT alone:
    the page must drop the OLDEST items, report honest ``byte_size`` and
    ``truncated_by_bytes``, and keep cursor consistency."""
    session_row = await acreate_session(
        factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
    )
    sid = session_row.session_id
    big_text = "x" * 19_000
    with psycopg.connect(migrated_url) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                for n in range(5):
                    payload = json.dumps(
                        {
                            "kind": "user_message",
                            "text": big_text,
                            "client_request_id": str(uuid.UUID(int=n)),
                            "source": "interactive",
                        }
                    )
                    cur.execute(
                        """
                        INSERT INTO session_entries
                            (entry_id, session_id, run_id, seq, kind,
                             payload, durable_source_id)
                        VALUES (%s, %s, NULL, %s, 'user_message',
                                %s::jsonb, %s)
                        """,
                        (uuid.uuid4(), sid, n + 1, payload, f"big-{n}"),
                    )
    page = await aread_session_history(
        factory, session_id=sid, server_id=SERVER_A
    )
    assert page.total == 5
    assert page.truncated_by_bytes is True
    assert page.byte_size <= MAX_PAGE_BYTES
    assert 0 < len(page.items) < 5  # oldest items were dropped
    assert page.oldest_seq > 1  # not the true oldest anymore
    assert page.latest_seq == 5  # the newest tail is kept
    assert page.has_older is True
    assert page.next_before_seq == page.oldest_seq
    assert [item.seq for item in page.items] == list(
        range(page.oldest_seq, 6)
    )
    print(
        "byte-budget measurement: 5 x ~19KB entries -> kept "
        f"items={len(page.items)} byte_size={page.byte_size} "
        f"truncated_by_bytes={page.truncated_by_bytes} "
        f"oldest_seq={page.oldest_seq}"
    )


# ---------------------------------------------------------------------------
# 5. scope blindness
# ---------------------------------------------------------------------------


async def test_cross_server_access_fails_scope_blind_everywhere(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await acreate_session(
        factory,
        server_id=SERVER_A,
        source=SessionSourceKind.INTERACTIVE,
        title="secret synthetic title",
    )
    sid = session_row.session_id

    async def _rejects(call) -> None:
        with pytest.raises(SessionContractError) as err:
            await call()
        # Scope blindness is the CODE: a missing session and a cross-server
        # session are the same not-found (the field path differs by entry —
        # missing → session_id, cross-server → the D01 scope guard's
        # server_id). No content leaks in either case.
        assert set(err.value.fields.values()) == {
            SessionErrorCode.SESSION_NOT_FOUND
        }
        assert "secret synthetic title" not in str(err.value)
        assert str(sid) not in str(err.value)

    await _rejects(lambda: aget_session(factory, sid, server_id=SERVER_B))
    await _rejects(
        lambda: aread_session_history(
            factory, session_id=sid, server_id=SERVER_B
        )
    )
    await _rejects(
        lambda: aappend_entry(
            factory,
            session_id=sid,
            server_id=SERVER_B,
            kind=EntryKind.USER_MESSAGE,
            payload=_user_payload(1),
            run_id=None,
            durable_source_id="b-1",
        )
    )
    await _rejects(
        lambda: arecord_admission(
            factory,
            session_id=sid,
            server_id=SERVER_B,
            client_request_id=uuid.UUID(int=0xBEEF),
            source=SessionSourceKind.INTERACTIVE,
        )
    )
    await _rejects(
        lambda: aattach_session_tag(
            factory,
            session_id=sid,
            server_id=SERVER_B,
            tag_id=uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
        )
    )
    await _rejects(
        lambda: alist_session_tags(
            factory, session_id=sid, server_id=SERVER_B
        )
    )
    await _rejects(
        lambda: alist_provider_artifacts(
            factory, session_id=sid, server_id=SERVER_B
        )
    )


# ---------------------------------------------------------------------------
# 6. run-state authority
# ---------------------------------------------------------------------------


async def test_run_state_updates_atomically_and_deduplicates(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    import sqlalchemy

    session_row = await acreate_session(
        factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
    )
    sid = session_row.session_id
    run = await acreate_run(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        run_id=RUN_1,
        model_profile="default",
        client_request_id=uuid.UUID(int=0xC001),
    )
    assert run.state == "queued"

    await arecord_run_state(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        run_id=RUN_1,
        state=SessionRunState.RUNNING,
    )
    await arecord_run_state(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        run_id=RUN_1,
        state=SessionRunState.RUNNING,  # repeated same state
    )
    await arecord_run_state(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        run_id=RUN_1,
        state=SessionRunState.WAITING_APPROVAL,
    )
    async with factory() as session:
        run_state = (
            await session.execute(
                sqlalchemy.text(
                    "SELECT state, completed_at FROM session_runs "
                    "WHERE run_id = :rid"
                ),
                {"rid": RUN_1},
            )
        ).mappings().one()
        entries = (
            await session.execute(
                sqlalchemy.text(
                    """
                    SELECT seq, payload FROM session_entries
                    WHERE session_id = :sid AND kind = 'run_state'
                    ORDER BY seq
                    """
                ),
                {"sid": sid},
            )
        ).mappings().all()
    assert run_state["state"] == "waiting_approval"
    assert run_state["completed_at"] is None  # waiting_* stays resumable
    assert len(entries) == 2  # one per DISTINCT state; the replay deduplicated
    assert [e["payload"]["state"] for e in entries] == [
        "running",
        "waiting_approval",
    ]

    # Terminal state stamps completed_at; an unknown run fails scope-blind.
    await arecord_run_state(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        run_id=RUN_1,
        state=SessionRunState.SUCCEEDED,
    )
    async with factory() as session:
        terminal = (
            await session.execute(
                sqlalchemy.text(
                    "SELECT completed_at FROM session_runs WHERE run_id = :rid"
                ),
                {"rid": RUN_1},
            )
        ).scalar_one()
    assert terminal is not None
    with pytest.raises(SessionContractError) as err:
        await arecord_run_state(
            factory,
            session_id=sid,
            server_id=SERVER_A,
            run_id=uuid.UUID(int=0xDEAD),
            state=SessionRunState.FAILED,
        )
    assert err.value.fields == {"run_id": SessionErrorCode.SESSION_NOT_FOUND}


# ---------------------------------------------------------------------------
# 7. generic survival + auxiliary store surfaces
# ---------------------------------------------------------------------------


async def test_generic_tag_surface_survives_and_store_aux_works(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    rows, total = await alist_tags(factory, limit=10, offset=0)
    names = {row.name for row in rows}
    assert total >= 4
    assert "unclassified" in names
    assert {"succeeded", "failed", "cancelled"} <= names

    session_row = await acreate_session(
        factory, server_id=SERVER_A, source=SessionSourceKind.RUNBOOK
    )
    sid = session_row.session_id

    # Session list: bounded, deterministic, honest total.
    listed, listed_total = await alist_sessions(
        factory, server_id=SERVER_A, limit=20, offset=0
    )
    assert listed_total == 1
    assert listed[0].session_id == sid

    # Provider artifacts: bounded write/read with the redacted flag honored.
    await arecord_provider_artifact(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        provider="openai",
        model_profile="default",
        artifact_kind="summary",
        content="synthetic bounded reasoning summary",
    )
    artifacts, artifact_total = await alist_provider_artifacts(
        factory, session_id=sid, server_id=SERVER_A, limit=10, offset=0
    )
    assert artifact_total == 1
    assert artifacts[0].artifact_kind == "summary"
    assert artifacts[0].redacted is False

    # Session tags: attach twice is one association; page is bounded.
    await aattach_session_tag(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        tag_id=uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
        assignment_source="user",
    )
    await aattach_session_tag(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        tag_id=uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
        assignment_source="user",
    )
    tags, tag_total = await alist_session_tags(
        factory, session_id=sid, server_id=SERVER_A
    )
    assert tag_total == 1
    assert tags[0].assignment_source == "user"

    # Admissions: a replayed client_request_id returns the same ledger row.
    creq = uuid.UUID(int=0x5EED)
    first = await arecord_admission(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        client_request_id=creq,
        source=SessionSourceKind.RUNBOOK,
        run_id=RUN_1,
    )
    second = await arecord_admission(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        client_request_id=creq,
        source=SessionSourceKind.RUNBOOK,
        run_id=RUN_1,
    )
    assert first.admission_id == second.admission_id
    assert first.run_id == RUN_1

# ---------------------------------------------------------------------------
# 8. root admission seam (queued-chat hotfix S1)
# ---------------------------------------------------------------------------


ROOT_RUN_1 = uuid.UUID("a1e2c3d4-0002-4002-8002-0000000000a2")
ROOT_RUN_2 = uuid.UUID("a1e2c3d4-0003-4003-8003-0000000000a3")
ROOT_RUN_FOREIGN = uuid.UUID("a1e2c3d4-0004-4004-8004-0000000000a4")
ROOT_CREQ = uuid.UUID(int=0x7C0)
#: Deliberately NON-default revision values: the facade must carry the
# caller's real binding (7/9), never a fabricated 1/1.
ROOT_PROFILE_REV = 7
ROOT_PROMPT_REV = 9
ROOT_GRANTS = ["chat.write", "history.read"]


async def _purge_root_facade_rows(
    factory: async_sessionmaker[AsyncSession], *run_ids: uuid.UUID
) -> None:
    """Remove THIS section's facade rows (``job_tags`` cascade with
    ``job_runs``).

    The shared autouse fixture truncates only the session-first tables;
    the generic job surface keeps its seeded rows (acceptance 7), so the
    root-seam tests scope their cleanup to exactly the run ids they own.
    The 0011 trigger refuses to delete an UNCLASSIFIED run, so a bare
    facade row (the foreign-identity fixture) is classified first.
    """
    async with factory() as session:
        async with session.begin():
            await session.execute(
                sql_text(
                    """
                    INSERT INTO job_tags (job_id, tag_id, assignment_source)
                    SELECT u.run_id, :tag, :source
                    FROM unnest(CAST(:rids AS uuid[])) AS u(run_id)
                    WHERE EXISTS (
                        SELECT 1 FROM job_runs jr WHERE jr.run_id = u.run_id
                    )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "rids": [str(rid) for rid in run_ids],
                    "tag": RESERVED_UNCLASSIFIED_TAG_ID,
                    "source": SYSTEM_ASSIGNMENT_SOURCE,
                },
            )
            await session.execute(
                sql_text(
                    "DELETE FROM job_runs WHERE run_id = ANY(CAST(:rids AS uuid[]))"
                ),
                {"rids": [str(rid) for rid in run_ids]},
            )


async def _root_seam_rows(
    factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> tuple[object | None, object | None, list[object]]:
    """Read back the full root-admission footprint of one run id."""
    async with factory() as session:
        run = (
            await session.execute(
                sql_text(
                    "SELECT session_id, server_id, parent_run_id, state, "
                    "model_profile, client_request_id FROM session_runs "
                    "WHERE run_id = :rid"
                ),
                {"rid": run_id},
            )
        ).mappings().first()
        facade = (
            await session.execute(
                sql_text(
                    "SELECT server_id, depth, parent_run_id, profile_revision, "
                    "prompt_revision, model_profile, requested_grants "
                    "FROM job_runs WHERE run_id = :rid"
                ),
                {"rid": run_id},
            )
        ).mappings().first()
        tags = (
            (
                await session.execute(
                    sql_text(
                        "SELECT tag_id, assignment_source FROM job_tags "
                        "WHERE job_id = :rid"
                    ),
                    {"rid": run_id},
                )
            )
            .mappings()
            .all()
        )
    return run, facade, list(tags)


async def test_root_admission_creates_run_facade_and_classification_atomically(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _purge_root_facade_rows(factory, ROOT_RUN_1)
    try:
        session_row = await acreate_session(
            factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
        )
        admitted = await acreate_root_run(
            factory,
            session_id=session_row.session_id,
            server_id=SERVER_A,
            run_id=ROOT_RUN_1,
            model_profile="primary",
            client_request_id=ROOT_CREQ,
            profile_revision=ROOT_PROFILE_REV,
            prompt_revision=ROOT_PROMPT_REV,
            requested_grants=ROOT_GRANTS,
        )
        assert admitted.state == "queued"
        assert admitted.parent_run_id is None  # a root, by construction
        assert admitted.model_profile == "primary"
        assert admitted.client_request_id == ROOT_CREQ

        run, facade, tags = await _root_seam_rows(factory, ROOT_RUN_1)
        assert run is not None
        assert run["session_id"] == session_row.session_id
        assert run["state"] == "queued"
        # The facade row: THIS server's root with the caller's real
        # revisions and grants — depth 0, no parent, nothing fabricated.
        assert facade is not None
        assert facade["server_id"] == SERVER_A
        assert facade["depth"] == 0
        assert facade["parent_run_id"] is None
        assert facade["profile_revision"] == ROOT_PROFILE_REV
        assert facade["prompt_revision"] == ROOT_PROMPT_REV
        assert facade["model_profile"] == "primary"
        assert facade["requested_grants"] == sorted(ROOT_GRANTS)
        # The 0011 mandatory classification, committed with the facade.
        assert len(tags) == 1
        assert tags[0]["tag_id"] == uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)
        assert tags[0]["assignment_source"] == SYSTEM_ASSIGNMENT_SOURCE
    finally:
        await _purge_root_facade_rows(factory, ROOT_RUN_1)


async def test_matching_root_admission_replay_is_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _purge_root_facade_rows(factory, ROOT_RUN_1)
    try:
        session_row = await acreate_session(
            factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
        )
        first = await acreate_root_run(
            factory,
            session_id=session_row.session_id,
            server_id=SERVER_A,
            run_id=ROOT_RUN_1,
            model_profile="primary",
            client_request_id=ROOT_CREQ,
            profile_revision=ROOT_PROFILE_REV,
            prompt_revision=ROOT_PROMPT_REV,
            requested_grants=ROOT_GRANTS,
        )
        second = await acreate_root_run(
            factory,
            session_id=session_row.session_id,
            server_id=SERVER_A,
            run_id=ROOT_RUN_1,
            model_profile="primary",
            client_request_id=ROOT_CREQ,
            profile_revision=ROOT_PROFILE_REV,
            prompt_revision=ROOT_PROMPT_REV,
            requested_grants=ROOT_GRANTS,
        )
        assert second == first  # same committed row, same timestamps
        async with factory() as session:
            run_count = (
                await session.execute(
                    sql_text(
                        "SELECT COUNT(*) FROM session_runs WHERE run_id = :rid"
                    ),
                    {"rid": ROOT_RUN_1},
                )
            ).scalar_one()
            facade_count = (
                await session.execute(
                    sql_text(
                        "SELECT COUNT(*) FROM job_runs WHERE run_id = :rid"
                    ),
                    {"rid": ROOT_RUN_1},
                )
            ).scalar_one()
            tag_count = (
                await session.execute(
                    sql_text(
                        "SELECT COUNT(*) FROM job_tags WHERE job_id = :rid"
                    ),
                    {"rid": ROOT_RUN_1},
                )
            ).scalar_one()
        assert run_count == 1
        assert facade_count == 1
        assert tag_count == 1
    finally:
        await _purge_root_facade_rows(factory, ROOT_RUN_1)


async def test_root_admission_replay_backfills_legacy_row_without_facade(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The production incident shape: a pre-hotfix root ``session_runs``
    row committed WITHOUT the facade. A retry through the seam converges:
    the existing run row is returned unchanged and the missing facade +
    classification are created — never a duplicate run."""
    await _purge_root_facade_rows(factory, ROOT_RUN_2)
    try:
        session_row = await acreate_session(
            factory, server_id=SERVER_A, source=SessionSourceKind.API
        )
        legacy = await acreate_run(  # the pre-hotfix root writer
            factory,
            session_id=session_row.session_id,
            server_id=SERVER_A,
            run_id=ROOT_RUN_2,
            model_profile="primary",
            client_request_id=ROOT_CREQ,
        )
        _, facade, tags = await _root_seam_rows(factory, ROOT_RUN_2)
        assert facade is None  # exactly the diagnosed missing-row state
        assert tags == []

        converged = await acreate_root_run(
            factory,
            session_id=session_row.session_id,
            server_id=SERVER_A,
            run_id=ROOT_RUN_2,
            model_profile="primary",
            client_request_id=ROOT_CREQ,
            profile_revision=ROOT_PROFILE_REV,
            prompt_revision=ROOT_PROMPT_REV,
            requested_grants=ROOT_GRANTS,
        )
        assert converged.run_id == legacy.run_id
        assert converged.created_at == legacy.created_at  # same row, not a copy
        run, facade, tags = await _root_seam_rows(factory, ROOT_RUN_2)
        assert run is not None
        assert facade is not None
        assert facade["profile_revision"] == ROOT_PROFILE_REV
        assert facade["prompt_revision"] == ROOT_PROMPT_REV
        assert facade["requested_grants"] == sorted(ROOT_GRANTS)
        assert len(tags) == 1
        async with factory() as session:
            run_count = (
                await session.execute(
                    sql_text(
                        "SELECT COUNT(*) FROM session_runs WHERE run_id = :rid"
                    ),
                    {"rid": ROOT_RUN_2},
                )
            ).scalar_one()
        assert run_count == 1
    finally:
        await _purge_root_facade_rows(factory, ROOT_RUN_2)


async def test_root_admission_rejects_cross_identity_and_rolls_back_atomically(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fail-closed identity conflicts, with REAL atomic rollback: the
    transaction inserts ``session_runs`` BEFORE the facade identity check
    fires, so the post-failure absence of that row proves Postgres rolled
    the whole transaction back."""
    await _purge_root_facade_rows(factory, ROOT_RUN_1, ROOT_RUN_FOREIGN)
    try:
        # (a) run id already admitted under a DIFFERENT session.
        first_session = await acreate_session(
            factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
        )
        second_session = await acreate_session(
            factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
        )
        await acreate_root_run(
            factory,
            session_id=first_session.session_id,
            server_id=SERVER_A,
            run_id=ROOT_RUN_1,
            model_profile="primary",
            client_request_id=ROOT_CREQ,
            profile_revision=ROOT_PROFILE_REV,
            prompt_revision=ROOT_PROMPT_REV,
            requested_grants=ROOT_GRANTS,
        )
        with pytest.raises(SessionContractError) as err:
            await acreate_root_run(
                factory,
                session_id=second_session.session_id,
                server_id=SERVER_A,
                run_id=ROOT_RUN_1,
                model_profile="primary",
                client_request_id=ROOT_CREQ,
                profile_revision=ROOT_PROFILE_REV,
                prompt_revision=ROOT_PROMPT_REV,
                requested_grants=ROOT_GRANTS,
            )
        assert err.value.fields == {
            "run_id": SessionErrorCode.SESSION_RUN_IDENTITY_CONFLICT
        }
        async with factory() as session:
            stolen = (
                await session.execute(
                    sql_text(
                        "SELECT COUNT(*) FROM session_runs "
                        "WHERE run_id = :rid AND session_id = :sid"
                    ),
                    {"rid": ROOT_RUN_1, "sid": second_session.session_id},
                )
            ).scalar_one()
        assert stolen == 0  # nothing committed for the second session

        # (b) facade owned by ANOTHER server (a foreign-surface run id; it
        # carries its own legitimate classification, as every writer must):
        # the session_runs insert inside this transaction succeeds, the
        # facade identity check then fails — everything must roll back.
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    sql_text(
                        """
                        INSERT INTO job_runs
                            (run_id, server_id, depth, profile_revision,
                             prompt_revision, model_profile, requested_grants,
                             submitted_at, updated_at)
                        VALUES
                            (:rid, :server_id, 0, 3, 3, 'foreign', :grants,
                             now(), now())
                        """
                    ),
                    {
                        "rid": ROOT_RUN_FOREIGN,
                        "server_id": SERVER_B,
                        "grants": json.dumps([]),
                    },
                )
                await session.execute(
                    sql_text(
                        """
                        INSERT INTO job_tags (job_id, tag_id, assignment_source)
                        VALUES (:rid, :tag, :source)
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    {
                        "rid": ROOT_RUN_FOREIGN,
                        "tag": RESERVED_UNCLASSIFIED_TAG_ID,
                        "source": SYSTEM_ASSIGNMENT_SOURCE,
                    },
                )
        third_session = await acreate_session(
            factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
        )
        with pytest.raises(SessionContractError) as facade_err:
            await acreate_root_run(
                factory,
                session_id=third_session.session_id,
                server_id=SERVER_A,
                run_id=ROOT_RUN_FOREIGN,
                model_profile="primary",
                client_request_id=ROOT_CREQ,
                profile_revision=ROOT_PROFILE_REV,
                prompt_revision=ROOT_PROMPT_REV,
                requested_grants=ROOT_GRANTS,
            )
        assert facade_err.value.fields == {
            "run_id": SessionErrorCode.SESSION_RUN_IDENTITY_CONFLICT
        }
        run, facade, tags = await _root_seam_rows(factory, ROOT_RUN_FOREIGN)
        assert run is None  # the in-transaction insert rolled back
        assert facade is not None
        assert facade["server_id"] == SERVER_B  # foreign row untouched
        assert facade["model_profile"] == "foreign"
        assert facade["profile_revision"] == 3  # this seam's 7 never landed
        # Exactly the foreign writer's OWN classification remains — the
        # classification count cannot grow through a failed admission.
        assert len(tags) == 1
        assert tags[0]["tag_id"] == uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)
    finally:
        await _purge_root_facade_rows(factory, ROOT_RUN_1, ROOT_RUN_FOREIGN)


async def test_root_admission_validation_is_fail_closed_and_scope_blind(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _purge_root_facade_rows(factory, ROOT_RUN_1)
    try:
        session_row = await acreate_session(
            factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
        )

        async def _admit(**overrides: object) -> SessionContractError | None:
            kwargs: dict[str, object] = {
                "session_id": session_row.session_id,
                "server_id": SERVER_A,
                "run_id": ROOT_RUN_1,
                "model_profile": "primary",
                "client_request_id": ROOT_CREQ,
                "profile_revision": ROOT_PROFILE_REV,
                "prompt_revision": ROOT_PROMPT_REV,
                "requested_grants": ROOT_GRANTS,
            }
            kwargs.update(overrides)
            try:
                await acreate_root_run(factory, **kwargs)  # type: ignore[arg-type]
            except SessionContractError as exc:
                return exc
            return None

        # Fabricated/out-of-contract revisions are refused before any write.
        assert (
            await _admit(profile_revision=0)
        ).fields == {"profile_revision": SessionErrorCode.FORBIDDEN_FIELD}
        assert (
            await _admit(prompt_revision=0)
        ).fields == {"prompt_revision": SessionErrorCode.FORBIDDEN_FIELD}
        assert (await _admit(profile_revision=True)) is not None  # bool is not a revision
        # Grants must be a real caller-supplied sequence of references.
        assert (
            await _admit(requested_grants=["chat.write", 5])
        ).fields == {"requested_grants": SessionErrorCode.FORBIDDEN_FIELD}
        assert (await _admit(requested_grants="history.read")) is not None
        assert (await _admit(requested_grants=None)) is not None
        assert (
            await _admit(model_profile="x" * 129)
        ).fields == {"model_profile": SessionErrorCode.FORBIDDEN_FIELD}
        # Nothing from the rejected calls reached the database.
        run, facade, tags = await _root_seam_rows(factory, ROOT_RUN_1)
        assert run is None and facade is None and tags == []

        # Scope blindness: another server's session is value-free absent.
        other = await acreate_session(
            factory, server_id=SERVER_B, source=SessionSourceKind.INTERACTIVE
        )
        scope_err = await _admit(
            session_id=other.session_id, server_id=SERVER_A
        )
        assert scope_err is not None
        assert scope_err.fields == {"server_id": SessionErrorCode.SESSION_NOT_FOUND}
    finally:
        await _purge_root_facade_rows(factory, ROOT_RUN_1)


# ---------------------------------------------------------------------------
# 9. generated-title seam (ST-PLAN §4.2; decisions D2/D3)
# ---------------------------------------------------------------------------


async def test_generated_title_writes_null_title_and_returns_committed_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await acreate_session(
        factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
    )
    assert session_row.title is None
    written = await aset_generated_session_title(
        factory,
        session_id=session_row.session_id,
        server_id=SERVER_A,
        title="Deploy the stack",
    )
    assert written is not None
    assert written.session_id == session_row.session_id
    assert written.title == "Deploy the stack"
    # The committed truth is visible to the read path too.
    stored = await aget_session(
        factory, session_row.session_id, server_id=SERVER_A
    )
    assert stored.title == "Deploy the stack"
    # The exact rename-path bound (120) is accepted as well.
    at_bound = await acreate_session(
        factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
    )
    written_bound = await aset_generated_session_title(
        factory,
        session_id=at_bound.session_id,
        server_id=SERVER_A,
        title="B" * 120,
    )
    assert written_bound is not None and written_bound.title == "B" * 120


async def test_generated_title_never_overwrites_existing_title(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await acreate_session(
        factory,
        server_id=SERVER_A,
        source=SessionSourceKind.INTERACTIVE,
        title="Manual rename",
    )
    refused = await aset_generated_session_title(
        factory,
        session_id=session_row.session_id,
        server_id=SERVER_A,
        title="Generated",
    )
    # D2: the manual rename wins; the generator degrades, never raises.
    assert refused is None
    stored = await aget_session(
        factory, session_row.session_id, server_id=SERVER_A
    )
    assert stored.title == "Manual rename"


async def test_generated_title_leaves_updated_at_untouched(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await acreate_session(
        factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
    )
    before = await aget_session(
        factory, session_row.session_id, server_id=SERVER_A
    )
    await asyncio.sleep(0.05)  # a stamped now() would move past `before`
    written = await aset_generated_session_title(
        factory,
        session_id=session_row.session_id,
        server_id=SERVER_A,
        title="Deploy the stack",
    )
    assert written is not None
    # D3: the write carries no updated_at stamp — the history list order
    # is untouched by a generated (or backfilled) title.
    assert written.updated_at == before.updated_at
    after = await aget_session(
        factory, session_row.session_id, server_id=SERVER_A
    )
    assert after.updated_at == before.updated_at
    assert after.title == "Deploy the stack"


async def test_generated_title_cross_server_is_value_free_absent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await acreate_session(
        factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
    )
    with pytest.raises(SessionContractError) as err:
        await aset_generated_session_title(
            factory,
            session_id=session_row.session_id,
            server_id=SERVER_B,
            title="Deploy the stack",
        )
    # Scope blindness: the SAME value-free not-found code as the rename
    # path; neither the session id nor the would-be title leaks.
    assert set(err.value.fields.values()) == {SessionErrorCode.SESSION_NOT_FOUND}
    assert "Deploy" not in str(err.value)
    assert str(session_row.session_id) not in str(err.value)
    stored = await aget_session(
        factory, session_row.session_id, server_id=SERVER_A
    )
    assert stored.title is None


async def test_generated_title_rejects_invalid_title_before_any_write(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await acreate_session(
        factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
    )
    for bad in ("", "   ", "x" * 121, None):
        with pytest.raises(SessionContractError) as err:
            await aset_generated_session_title(
                factory,
                session_id=session_row.session_id,
                server_id=SERVER_A,
                title=bad,  # type: ignore[arg-type]
            )
        # The contract rejection (never a database error), rename shape.
        assert err.value.fields == {"title": SessionErrorCode.FORBIDDEN_FIELD}
    # Nothing from the rejected calls reached the database.
    stored = await aget_session(
        factory, session_row.session_id, server_id=SERVER_A
    )
    assert stored.title is None
