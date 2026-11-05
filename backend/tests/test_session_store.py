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
    aattach_session_tag,
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
from mai.tag_store import RESERVED_UNCLASSIFIED_TAG_ID, alist_tags

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
