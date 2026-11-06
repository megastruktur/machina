"""Focused tests for indexed tag search (history-tags W3A; D09 job-only).

Claims under test (plans/history-tags.md slice W3A, post-D09 surface):

1. Tag-filtered job search: any/all semantics, deduped UUID tags,
   scope-checked, bounded limit/offset/total, deterministic ordering,
   N+1-free previews.
2. Unknown tags: empty result, not an error.
3. Scope isolation: cross-server entities are invisible.
4. Duplicate prevention: no duplicate rows even when an entity matches
   multiple tags.
5. Stable pages/count: repeated calls with same params return identical
   results; total is pre-slice count.
6. Caps: limit/offset bounds fail closed.
7. Job content reader: one server-scoped run's content row (parentage,
   frozen label, replayed lifecycle state, error/evidence references).

The conversation search/content half was removed at D09 together with the
dropped ``conversations``/``conversation_tags``/``conversation_messages``
tables (migration 0015); session content reads live on the bounded
agent-session history surface.

Every claim runs against a real disposable PostgreSQL fixture
(``MAI_TEST_DATABASE_URL``) with the real linear Alembic chain applied.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from datetime import datetime

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.jobs import TERMINAL_RUN_STATES, RunState
from mai.database import create_app_engine
from mai.history_store import (
    JobContent,
    JobSearchRow,
    asearch_jobs_by_tags,
    aread_job_content,
)
from mai.jobs.lifecycle import RunLifecycle
from mai.tag_store import (
    RESERVED_UNCLASSIFIED_TAG_ID,
    acreate_tag,
    aassign_job_tag,
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
        conn.execute("TRUNCATE job_tags, job_runs, tags CASCADE")
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


def _seed_terminal_tags(url: str) -> None:
    """Seed the terminal run-state catalog tags (idempotent).

    A terminal ``record_transition`` atomically swaps the reserved
    ``unclassified`` assignment for a catalog tag named after the target
    state; a fresh 0011 catalog only seeds ``unclassified``, so tests
    replaying terminal transitions seed these rows first — the same
    harness contract the jobs suite's live scenarios use.
    """
    with psycopg.connect(url) as conn:
        for state in TERMINAL_RUN_STATES:
            conn.execute(
                "INSERT INTO tags (tag_id, name, description) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (uuid.uuid4(), state.value, "terminal run classification"),
            )
        conn.commit()


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


def _insert_job_full(
    url: str,
    job_id: uuid.UUID,
    server_id: uuid.UUID = SERVER_A,
    *,
    parent_run_id: uuid.UUID | None = None,
    label: str | None = None,
    error_ref: str | None = None,
    evidence_ref: str | None = None,
) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            """
            INSERT INTO job_runs
                (run_id, server_id, parent_run_id, depth, label,
                 profile_revision, prompt_revision, model_profile,
                 requested_grants, error_ref, evidence_ref)
            VALUES (%s, %s, %s, %s, %s, 1, 1, 'profile', '[]'::jsonb, %s, %s)
            """,
            (
                job_id,
                server_id,
                parent_run_id,
                0 if parent_run_id is None else 1,
                label if label is not None else "",
                error_ref,
                evidence_ref,
            ),
        )
        conn.execute(
            "INSERT INTO job_tags (job_id, tag_id) VALUES (%s, %s)",
            (job_id, uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)),
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
async def test_invalid_match_rejected(factory, migrated_url) -> None:
    _insert_job(migrated_url, JOB_A)
    tag = await acreate_tag(factory, name="ops")
    with pytest.raises(ValueError):
        await asearch_jobs_by_tags(
            factory, server_id=SERVER_A, tag_ids=[tag.tag_id], match="bad"
        )


# ---------------------------------------------------------------------------
# 2 — job content reader
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_job_content_fields_state_and_honest_nones(
    factory, migrated_url
) -> None:
    """Frozen content row: identity, parentage, label, refs; honest Nones.

    ``state`` is ``None`` before any lifecycle event and replays from the
    persisted event stream afterwards; a run submitted without a label
    stores the schema default empty string.
    """
    parent = uuid.uuid4()
    child = uuid.uuid4()
    _insert_job_full(migrated_url, parent)
    _insert_job_full(
        migrated_url,
        child,
        parent_run_id=parent,
        label="deploy checkout",
        error_ref="err://logs/1",
        evidence_ref="ev://trace/1",
    )

    content = await aread_job_content(factory, child, server_id=SERVER_A)

    assert isinstance(content, JobContent)
    assert content.run_id == child
    assert content.parent_run_id == parent
    assert content.label == "deploy checkout"
    assert content.error_ref == "err://logs/1"
    assert content.evidence_ref == "ev://trace/1"
    assert content.state is None  # no lifecycle events yet
    assert isinstance(content.submitted_at, datetime)
    assert isinstance(content.updated_at, datetime)
    with pytest.raises(dataclasses.FrozenInstanceError):
        content.label = "mutated"  # type: ignore[misc]

    # Lifecycle state replays from the persisted event stream (str value).
    # Terminal transitions swap to catalog tags named after the state, so
    # seed them idempotently first (fresh 0011 catalog lacks them).
    lifecycle = RunLifecycle(factory)
    _seed_terminal_tags(migrated_url)
    await lifecycle.record_transition(str(child), RunState.QUEUED)
    await lifecycle.record_transition(str(child), RunState.RUNNING)
    await lifecycle.record_transition(str(child), RunState.FAILED)
    content = await aread_job_content(factory, child, server_id=SERVER_A)
    assert content.state == "failed"

    # Label-less submit stores the schema default empty string verbatim.
    plain = uuid.uuid4()
    _insert_job_full(migrated_url, plain)
    bare = await aread_job_content(factory, plain, server_id=SERVER_A)
    assert bare.label == ""
    assert bare.state is None


@pytest.mark.anyio
async def test_job_content_scope_isolation(factory, migrated_url) -> None:
    """A run outside ``server_id`` is a fail-closed ``ValueError``."""
    job_a = uuid.uuid4()
    _insert_job_full(migrated_url, job_a, server_id=SERVER_A)
    job_b = uuid.uuid4()
    _insert_job_full(migrated_url, job_b, server_id=SERVER_B)

    content = await aread_job_content(factory, job_a, server_id=SERVER_A)
    assert content.run_id == job_a
    with pytest.raises(ValueError, match="not found in server"):
        await aread_job_content(factory, job_b, server_id=SERVER_A)
