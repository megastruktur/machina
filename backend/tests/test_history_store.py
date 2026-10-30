"""Focused tests for indexed tag search (history-tags W3A).

Claims under test (plans/history-tags.md slice W3A):

1. Tag-filtered job search: any/all semantics, deduped UUID tags,
   scope-checked, bounded limit/offset/total, deterministic ordering,
   N+1-free previews.
2. Tag-filtered conversation search: same contract as jobs.
3. Unknown tags: empty result, not an error.
4. Scope isolation: cross-server entities are invisible.
5. Duplicate prevention: no duplicate rows even when an entity matches
   multiple tags.
6. Stable pages/count: repeated calls with same params return identical
   results; total is pre-slice count.
7. Independent associations: job and conversation tags do not leak.
8. Caps: limit/offset bounds fail closed.

Every claim runs against a real disposable PostgreSQL fixture
(``MAI_TEST_DATABASE_URL``) with the real linear Alembic chain applied.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.database import create_app_engine
from mai.history_store import (
    ConversationSearchRow,
    JobSearchRow,
    asearch_conversations_by_tags,
    asearch_jobs_by_tags,
)
from mai.tag_store import (
    RESERVED_UNCLASSIFIED_TAG_ID,
    acreate_tag,
    aassign_conversation_tag,
    aassign_job_tag,
    aremove_conversation_tag,
    aremove_job_tag,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
SERVER_B = uuid.UUID("9a8b7c6d-1e2f-4a3b-8c5d-6e7f8a9b0c1d")

JOB_A = uuid.UUID("a1e2c3d4-0001-4001-8001-0000000000a1")
JOB_B = uuid.UUID("a1e2c3d4-0002-4002-8002-0000000000a2")
JOB_C = uuid.UUID("a1e2c3d4-0003-4003-8003-0000000000a3")
JOB_D = uuid.UUID("a1e2c3d4-0004-4004-8004-0000000000a4")
JOB_E = uuid.UUID("a1e2c3d4-0005-4005-8005-0000000000a5")
CONV_A = uuid.UUID("b2f3d4e5-0001-4001-8001-0000000000b1")
CONV_B = uuid.UUID("b2f3d4e5-0002-4002-8002-0000000000b2")
CONV_C = uuid.UUID("b2f3d4e5-0003-4003-8003-0000000000b3")
CONV_D = uuid.UUID("b2f3d4e5-0004-4004-8004-0000000000b4")
TAG_X = uuid.UUID("c3a4b5d6-0001-4001-8001-0000000000c1")
TAG_Y = uuid.UUID("c3a4b5d6-0002-4002-8002-0000000000c2")
TAG_Z = uuid.UUID("c3a4b5d6-0003-4003-8003-0000000000c3")


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


@pytest.fixture(scope="session")
def migrated_url() -> str:
    url = os.environ.get(LIVE_URL_VAR, "")
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the storage claims need a real "
            "disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture(autouse=True)
def clean_domain(migrated_url: str):
    yield
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE job_tags, conversation_tags, job_runs, conversations, "
            "tags CASCADE"
        )
        conn.execute(
            "INSERT INTO tags (tag_id, name, description) "
            "VALUES (%s, 'unclassified', 'reserved default') "
            "ON CONFLICT DO NOTHING",
            (uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),),
        )


@pytest.fixture
def factory(migrated_url: str) -> async_sessionmaker[AsyncSession]:
    _, sessions = create_app_engine(migrated_url)
    return sessions


def _insert_job(
    url: str, job_id: uuid.UUID, server_id: uuid.UUID = SERVER_A
) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            """
            INSERT INTO job_runs
                (run_id, server_id, depth, profile_revision, prompt_revision,
                 model_profile, requested_grants)
            VALUES (%s, %s, 0, 1, 1, 'profile', '[]'::jsonb)
            """,
            (job_id, server_id),
        )
        conn.execute(
            "INSERT INTO job_tags (job_id, tag_id) VALUES (%s, %s)",
            (job_id, uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)),
        )
        conn.commit()


def _insert_conversation(
    url: str, conversation_id: uuid.UUID, server_id: uuid.UUID = SERVER_A
) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO conversations (conversation_id, server_id) VALUES (%s, %s)",
            (conversation_id, server_id),
        )
        conn.execute(
            "INSERT INTO conversation_tags (conversation_id, tag_id) VALUES (%s, %s)",
            (conversation_id, uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# 1 — job search: any/all, deduped tags, scope, pagination, previews
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_job_search_any_match(factory, migrated_url) -> None:
    _insert_job(migrated_url, JOB_A)
    _insert_job(migrated_url, JOB_B)
    _insert_job(migrated_url, JOB_C)
    tag = await acreate_tag(factory, name="ops")
    await aassign_job_tag(factory, JOB_A, tag.tag_id, server_id=SERVER_A)
    await aassign_job_tag(factory, JOB_B, tag.tag_id, server_id=SERVER_A)

    items, total = await asearch_jobs_by_tags(
        factory, server_id=SERVER_A, tag_ids=[tag.tag_id], match="any"
    )
    assert total == 2
    assert {row.run_id for row in items} == {JOB_A, JOB_B}
    assert all(isinstance(row, JobSearchRow) for row in items)
    assert all(row.tag_preview.total >= 1 for row in items)


@pytest.mark.anyio
async def test_job_search_all_match(factory, migrated_url) -> None:
    _insert_job(migrated_url, JOB_A)
    _insert_job(migrated_url, JOB_B)
    tag_x = await acreate_tag(factory, name="x")
    tag_y = await acreate_tag(factory, name="y")
    await aassign_job_tag(factory, JOB_A, tag_x.tag_id, server_id=SERVER_A)
    await aassign_job_tag(factory, JOB_A, tag_y.tag_id, server_id=SERVER_A)
    await aassign_job_tag(factory, JOB_B, tag_x.tag_id, server_id=SERVER_A)

    items, total = await asearch_jobs_by_tags(
        factory,
        server_id=SERVER_A,
        tag_ids=[tag_x.tag_id, tag_y.tag_id],
        match="all",
    )
    assert total == 1
    assert items[0].run_id == JOB_A


@pytest.mark.anyio
async def test_job_search_deduped_tags(factory, migrated_url) -> None:
    _insert_job(migrated_url, JOB_A)
    tag = await acreate_tag(factory, name="ops")
    await aassign_job_tag(factory, JOB_A, tag.tag_id, server_id=SERVER_A)

    items, total = await asearch_jobs_by_tags(
        factory,
        server_id=SERVER_A,
        tag_ids=[tag.tag_id, tag.tag_id, tag.tag_id],
        match="any",
    )
    assert total == 1
    assert items[0].run_id == JOB_A


@pytest.mark.anyio
async def test_job_search_unknown_tag_returns_empty(factory, migrated_url) -> None:
    _insert_job(migrated_url, JOB_A)
    unknown = uuid.uuid4()
    items, total = await asearch_jobs_by_tags(
        factory, server_id=SERVER_A, tag_ids=[unknown], match="any"
    )
    assert total == 0
    assert items == []


@pytest.mark.anyio
async def test_job_search_scope_isolation(factory, migrated_url) -> None:
    _insert_job(migrated_url, JOB_A, SERVER_A)
    _insert_job(migrated_url, JOB_D, SERVER_B)
    tag = await acreate_tag(factory, name="ops")
    await aassign_job_tag(factory, JOB_A, tag.tag_id, server_id=SERVER_A)
    await aassign_job_tag(factory, JOB_D, tag.tag_id, server_id=SERVER_B)

    items, total = await asearch_jobs_by_tags(
        factory, server_id=SERVER_A, tag_ids=[tag.tag_id], match="any"
    )
    assert total == 1
    assert items[0].run_id == JOB_A


@pytest.mark.anyio
async def test_job_search_pagination_stable(factory, migrated_url) -> None:
    for job_id in (JOB_A, JOB_B, JOB_C):
        _insert_job(migrated_url, job_id)
    tag = await acreate_tag(factory, name="ops")
    for job_id in (JOB_A, JOB_B, JOB_C):
        await aassign_job_tag(factory, job_id, tag.tag_id, server_id=SERVER_A)

    page1, total1 = await asearch_jobs_by_tags(
        factory, server_id=SERVER_A, tag_ids=[tag.tag_id], limit=2, offset=0
    )
    page2, total2 = await asearch_jobs_by_tags(
        factory, server_id=SERVER_A, tag_ids=[tag.tag_id], limit=2, offset=2
    )
    assert total1 == total2 == 3
    assert len(page1) == 2
    assert len(page2) == 1
    assert page1[0].run_id != page2[0].run_id

    # Same params = identical results
    again, total_again = await asearch_jobs_by_tags(
        factory, server_id=SERVER_A, tag_ids=[tag.tag_id], limit=2, offset=0
    )
    assert [r.run_id for r in again] == [r.run_id for r in page1]
    assert total_again == total1


@pytest.mark.anyio
async def test_job_search_no_duplicate_rows(factory, migrated_url) -> None:
    """An entity matching multiple tags appears exactly once."""
    _insert_job(migrated_url, JOB_A)
    tag_x = await acreate_tag(factory, name="x")
    tag_y = await acreate_tag(factory, name="y")
    await aassign_job_tag(factory, JOB_A, tag_x.tag_id, server_id=SERVER_A)
    await aassign_job_tag(factory, JOB_A, tag_y.tag_id, server_id=SERVER_A)

    items, total = await asearch_jobs_by_tags(
        factory,
        server_id=SERVER_A,
        tag_ids=[tag_x.tag_id, tag_y.tag_id],
        match="any",
    )
    assert total == 1
    assert len(items) == 1
    assert items[0].run_id == JOB_A


@pytest.mark.anyio
async def test_job_search_bounds_fail_closed(factory, migrated_url) -> None:
    _insert_job(migrated_url, JOB_A)
    tag = await acreate_tag(factory, name="ops")
    with pytest.raises(ValueError):
        await asearch_jobs_by_tags(
            factory, server_id=SERVER_A, tag_ids=[tag.tag_id], limit=0
        )
    with pytest.raises(ValueError):
        await asearch_jobs_by_tags(
            factory, server_id=SERVER_A, tag_ids=[tag.tag_id], limit=201
        )
    with pytest.raises(ValueError):
        await asearch_jobs_by_tags(
            factory, server_id=SERVER_A, tag_ids=[tag.tag_id], offset=-1
        )


@pytest.mark.anyio
async def test_job_search_empty_tag_list_rejected(factory, migrated_url) -> None:
    with pytest.raises(ValueError):
        await asearch_jobs_by_tags(factory, server_id=SERVER_A, tag_ids=[])


@pytest.mark.anyio
async def test_job_search_independent_associations(factory, migrated_url) -> None:
    """Job tags do not leak into conversation search and vice versa."""
    _insert_job(migrated_url, JOB_A)
    _insert_conversation(migrated_url, CONV_A)
    tag = await acreate_tag(factory, name="shared")
    await aassign_job_tag(factory, JOB_A, tag.tag_id, server_id=SERVER_A)
    await aassign_conversation_tag(
        factory, CONV_A, tag.tag_id, server_id=SERVER_A
    )

    jobs, job_total = await asearch_jobs_by_tags(
        factory, server_id=SERVER_A, tag_ids=[tag.tag_id], match="any"
    )
    convs, conv_total = await asearch_conversations_by_tags(
        factory, server_id=SERVER_A, tag_ids=[tag.tag_id], match="any"
    )
    assert job_total == 1 and jobs[0].run_id == JOB_A
    assert conv_total == 1 and convs[0].conversation_id == CONV_A


# ---------------------------------------------------------------------------
# 2 — conversation search: any/all, deduped tags, scope, pagination, previews
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_conversation_search_any_match(factory, migrated_url) -> None:
    _insert_conversation(migrated_url, CONV_A)
    _insert_conversation(migrated_url, CONV_B)
    _insert_conversation(migrated_url, CONV_C)
    tag = await acreate_tag(factory, name="chat")
    await aassign_conversation_tag(
        factory, CONV_A, tag.tag_id, server_id=SERVER_A
    )
    await aassign_conversation_tag(
        factory, CONV_B, tag.tag_id, server_id=SERVER_A
    )

    items, total = await asearch_conversations_by_tags(
        factory, server_id=SERVER_A, tag_ids=[tag.tag_id], match="any"
    )
    assert total == 2
    assert {row.conversation_id for row in items} == {CONV_A, CONV_B}
    assert all(isinstance(row, ConversationSearchRow) for row in items)
    assert all(row.tag_preview.total >= 1 for row in items)


@pytest.mark.anyio
async def test_conversation_search_all_match(factory, migrated_url) -> None:
    _insert_conversation(migrated_url, CONV_A)
    _insert_conversation(migrated_url, CONV_B)
    tag_x = await acreate_tag(factory, name="x")
    tag_y = await acreate_tag(factory, name="y")
    await aassign_conversation_tag(
        factory, CONV_A, tag_x.tag_id, server_id=SERVER_A
    )
    await aassign_conversation_tag(
        factory, CONV_A, tag_y.tag_id, server_id=SERVER_A
    )
    await aassign_conversation_tag(
        factory, CONV_B, tag_x.tag_id, server_id=SERVER_A
    )

    items, total = await asearch_conversations_by_tags(
        factory,
        server_id=SERVER_A,
        tag_ids=[tag_x.tag_id, tag_y.tag_id],
        match="all",
    )
    assert total == 1
    assert items[0].conversation_id == CONV_A


@pytest.mark.anyio
async def test_conversation_search_deduped_tags(factory, migrated_url) -> None:
    _insert_conversation(migrated_url, CONV_A)
    tag = await acreate_tag(factory, name="chat")
    await aassign_conversation_tag(
        factory, CONV_A, tag.tag_id, server_id=SERVER_A
    )

    items, total = await asearch_conversations_by_tags(
        factory,
        server_id=SERVER_A,
        tag_ids=[tag.tag_id, tag.tag_id],
        match="any",
    )
    assert total == 1
    assert items[0].conversation_id == CONV_A


@pytest.mark.anyio
async def test_conversation_search_unknown_tag_returns_empty(
    factory, migrated_url
) -> None:
    _insert_conversation(migrated_url, CONV_A)
    unknown = uuid.uuid4()
    items, total = await asearch_conversations_by_tags(
        factory, server_id=SERVER_A, tag_ids=[unknown], match="any"
    )
    assert total == 0
    assert items == []


@pytest.mark.anyio
async def test_conversation_search_scope_isolation(factory, migrated_url) -> None:
    _insert_conversation(migrated_url, CONV_A, SERVER_A)
    _insert_conversation(migrated_url, CONV_D, SERVER_B)
    tag = await acreate_tag(factory, name="chat")
    await aassign_conversation_tag(
        factory, CONV_A, tag.tag_id, server_id=SERVER_A
    )
    await aassign_conversation_tag(
        factory, CONV_D, tag.tag_id, server_id=SERVER_B
    )

    items, total = await asearch_conversations_by_tags(
        factory, server_id=SERVER_A, tag_ids=[tag.tag_id], match="any"
    )
    assert total == 1
    assert items[0].conversation_id == CONV_A


@pytest.mark.anyio
async def test_conversation_search_pagination_stable(factory, migrated_url) -> None:
    for cid in (CONV_A, CONV_B, CONV_C):
        _insert_conversation(migrated_url, cid)
    tag = await acreate_tag(factory, name="chat")
    for cid in (CONV_A, CONV_B, CONV_C):
        await aassign_conversation_tag(
            factory, cid, tag.tag_id, server_id=SERVER_A
        )

    page1, total1 = await asearch_conversations_by_tags(
        factory, server_id=SERVER_A, tag_ids=[tag.tag_id], limit=2, offset=0
    )
    page2, total2 = await asearch_conversations_by_tags(
        factory, server_id=SERVER_A, tag_ids=[tag.tag_id], limit=2, offset=2
    )
    assert total1 == total2 == 3
    assert len(page1) == 2
    assert len(page2) == 1

    again, total_again = await asearch_conversations_by_tags(
        factory, server_id=SERVER_A, tag_ids=[tag.tag_id], limit=2, offset=0
    )
    assert [r.conversation_id for r in again] == [r.conversation_id for r in page1]
    assert total_again == total1


@pytest.mark.anyio
async def test_conversation_search_no_duplicate_rows(factory, migrated_url) -> None:
    _insert_conversation(migrated_url, CONV_A)
    tag_x = await acreate_tag(factory, name="x")
    tag_y = await acreate_tag(factory, name="y")
    await aassign_conversation_tag(
        factory, CONV_A, tag_x.tag_id, server_id=SERVER_A
    )
    await aassign_conversation_tag(
        factory, CONV_A, tag_y.tag_id, server_id=SERVER_A
    )

    items, total = await asearch_conversations_by_tags(
        factory,
        server_id=SERVER_A,
        tag_ids=[tag_x.tag_id, tag_y.tag_id],
        match="any",
    )
    assert total == 1
    assert len(items) == 1
    assert items[0].conversation_id == CONV_A


@pytest.mark.anyio
async def test_conversation_search_bounds_fail_closed(factory, migrated_url) -> None:
    _insert_conversation(migrated_url, CONV_A)
    tag = await acreate_tag(factory, name="chat")
    with pytest.raises(ValueError):
        await asearch_conversations_by_tags(
            factory, server_id=SERVER_A, tag_ids=[tag.tag_id], limit=0
        )
    with pytest.raises(ValueError):
        await asearch_conversations_by_tags(
            factory, server_id=SERVER_A, tag_ids=[tag.tag_id], limit=201
        )
    with pytest.raises(ValueError):
        await asearch_conversations_by_tags(
            factory, server_id=SERVER_A, tag_ids=[tag.tag_id], offset=-1
        )


@pytest.mark.anyio
async def test_conversation_search_empty_tag_list_rejected(
    factory, migrated_url
) -> None:
    with pytest.raises(ValueError):
        await asearch_conversations_by_tags(
            factory, server_id=SERVER_A, tag_ids=[]
        )


# ---------------------------------------------------------------------------
# 3 — match validation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_invalid_match_rejected(factory, migrated_url) -> None:
    _insert_job(migrated_url, JOB_A)
    tag = await acreate_tag(factory, name="ops")
    with pytest.raises(ValueError):
        await asearch_jobs_by_tags(
            factory, server_id=SERVER_A, tag_ids=[tag.tag_id], match="bad"
        )
    with pytest.raises(ValueError):
        await asearch_conversations_by_tags(
            factory, server_id=SERVER_A, tag_ids=[tag.tag_id], match="bad"
        )
