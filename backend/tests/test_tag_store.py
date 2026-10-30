"""Focused tests for the bounded tag catalog + association store (W2).

Claims under test (plans/history-tags.md slice 3, migration 0011 schema):

1. Canonical create: whitespace-normalizing, case-insensitively idempotent,
   and an idempotent re-create NEVER overwrites the stored description.
2. Bounds: normalized name 1..64 single-line printable, description <= 500,
   fail-closed validation (the store never writes a rejected value).
3. Catalog pages: deterministic ``lower(name)`` order, honest ``total``,
   case-insensitive name/description substring filters with escaped LIKE
   metacharacters.
4. Entity scoping: job/conversation tags resolve only inside the requesting
   server scope; missing and cross-server entities are the same closed
   ``entity_not_found`` refusal; job and conversation associations are
   fully independent (no implicit inheritance).
5. Assign/remove: idempotent; removing the FINAL ordinary tag requires an
   atomic ``replacement_tag_id``; absent-association removal is a no-op.
6. Deletion policy: unused deletes; used refuses without force; force
   proceeds only when every affected entity keeps another tag or receives
   the replacement in the SAME transaction; the reserved ``unclassified``
   row can never be deleted.
7. Concurrency: lost canonical create races resolve to one row; concurrent
   assign/force-delete contention serializes on the catalog-row lock and
   every entity keeps >= 1 tag at the end (the 0011 invariant).

Every claim runs against a real disposable PostgreSQL fixture
(``MAI_TEST_DATABASE_URL``) with the real linear Alembic chain applied —
the established storage-suite harness shape (fresh isolated database,
never a shared/polluted one).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.database import create_app_engine
from mai.tag_store import (
    RESERVED_UNCLASSIFIED_TAG_ID,
    EntityTagPreview,
    TagErrorCode,
    TagStoreError,
    acreate_tag,
    adelete_tag,
    aassign_conversation_tag,
    aassign_job_tag,
    alist_tags,
    anormalize_tag_name,
    apreview_conversation_tags,
    apreview_job_tags,
    aremove_conversation_tag,
    aremove_job_tag,
    atags_for_conversation,
    atags_for_job,
)
from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID as SRC_RESERVED_ID

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
SERVER_B = uuid.UUID("9a8b7c6d-1e2f-4a3b-8c5d-6e7f8a9b0c1d")

JOB_A = uuid.UUID("a1e2c3d4-0001-4001-8001-0000000000a1")
JOB_B = uuid.UUID("a1e2c3d4-0002-4002-8002-0000000000a2")
JOB_C = uuid.UUID("a1e2c3d4-0003-4003-8003-0000000000a3")
JOB_D = uuid.UUID("a1e2c3d4-0004-4004-8004-0000000000a4")
CONV_A = uuid.UUID("b2f3d4e5-0001-4001-8001-0000000000b1")
CONV_B = uuid.UUID("b2f3d4e5-0002-4002-8002-0000000000b2")
CONV_C = uuid.UUID("b2f3d4e5-0003-4003-8003-0000000000b3")
TAG_X = uuid.UUID("c3a4b5d6-0001-4001-8001-0000000000c1")
TAG_Y = uuid.UUID("c3a4b5d6-0002-4002-8002-0000000000c2")


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database upgraded from zero to the current head."""
    url = os.environ.get(LIVE_URL_VAR, "")
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the storage claims need a real "
            "disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
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
    """Fresh catalog + empty entity domain per test (reserved re-seeded)."""
    yield
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        # CASCADE covers the referencing job/conversation domain tables
        # (steps, events, schedules, runs, steering, tasks...) in one
        # statement; only public app tables are FK-connected.
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
    """Entity creation mirrors the real writers: classified in one txn."""
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


def _rows(url: str, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# normalization + canonical idempotent create
# ---------------------------------------------------------------------------


def test_normalize_collapses_trims_and_bounds() -> None:
    assert anormalize_tag_name("  Deploy   Ops  ") == "Deploy Ops"
    assert anormalize_tag_name("a\tb") == "a b"  # runs collapse to one space
    assert anormalize_tag_name("a\nb") == "a b"
    assert anormalize_tag_name("тег") == "тег"
    for bad in ("", "   ", "a\x01b", "x" * 65):
        with pytest.raises(TagStoreError) as excinfo:
            anormalize_tag_name(bad)
        assert excinfo.value.code is TagErrorCode.VALIDATION
    assert len(anormalize_tag_name("x" * 64)) == 64


@pytest.mark.anyio
async def test_create_is_canonical_idempotent_and_preserves_description(
    factory, migrated_url
) -> None:
    first = await acreate_tag(
        factory, name="  Deploy  ", description="original text"
    )
    assert first.name == "Deploy"
    assert first.description == "original text"

    again = await acreate_tag(factory, name="deploy", description="OVERWRITE")
    assert again.tag_id == first.tag_id
    assert again.name == "Deploy"  # the stored display name never changes
    assert again.description == "original text"  # never overwritten

    rows = _rows(
        migrated_url,
        "SELECT tag_id, name FROM tags WHERE name ILIKE %s",
        ("deploy%",),
    )
    assert rows == [(first.tag_id, "Deploy")]


@pytest.mark.anyio
async def test_create_rejects_invalid_input_without_writing(
    factory, migrated_url
) -> None:
    for kwargs in (
        {"name": "   "},
        {"name": "a\x02b"},
        {"name": "x" * 65},
        {"name": "ok", "description": "d" * 501},
    ):
        with pytest.raises(TagStoreError) as excinfo:
            await acreate_tag(factory, **kwargs)
        assert excinfo.value.code is TagErrorCode.VALIDATION
    assert _rows(migrated_url, "SELECT count(*) FROM tags")[0][0] == 1  # reserved only
    assert (
        await acreate_tag(factory, name="x" * 64, description="d" * 500)
    ).name == "x" * 64


@pytest.mark.anyio
async def test_created_ids_match_the_runtime_constant(factory) -> None:
    assert RESERVED_UNCLASSIFIED_TAG_ID == SRC_RESERVED_ID


# ---------------------------------------------------------------------------
# catalog pages: order, total, filters
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_catalog_page_is_deterministic_with_honest_total(factory) -> None:
    for name in ("migrate", "deploy", "Audit", "backup", "logs", "omp", "security"):
        await acreate_tag(factory, name=name)
    items, total = await alist_tags(factory, limit=3, offset=0)
    assert total == 8  # seven + reserved unclassified
    assert [tag.name for tag in items] == ["Audit", "backup", "deploy"]
    page_two, total_two = await alist_tags(factory, limit=3, offset=3)
    assert total_two == 8
    assert [tag.name for tag in page_two] == ["logs", "migrate", "omp"]
    page_three, _ = await alist_tags(factory, limit=3, offset=6)
    assert [tag.name for tag in page_three] == ["security", "unclassified"]
    empty, total_empty = await alist_tags(factory, limit=3, offset=99)
    assert empty == [] and total_empty == 8
    with pytest.raises(ValueError):
        await alist_tags(factory, limit=0)
    with pytest.raises(ValueError):
        await alist_tags(factory, limit=201)


@pytest.mark.anyio
async def test_catalog_filters_are_case_insensitive_and_escaped(factory) -> None:
    await acreate_tag(factory, name="Deploy", description="ship it")
    await acreate_tag(factory, name="backup_100", description="100% verified")
    await acreate_tag(factory, name="audit")
    items, total = await alist_tags(factory, name_filter="dep")
    assert total == 1 and items[0].name == "Deploy"
    items, total = await alist_tags(factory, name_filter="100_100")
    assert total == 0  # the underscore is a literal, not a wildcard
    items, total = await alist_tags(factory, description_filter="100% ver")
    assert total == 1 and items[0].name == "backup_100"
    items, total = await alist_tags(factory, name_filter="dep", description_filter="ship")
    assert total == 1 and items[0].name == "Deploy"


# ---------------------------------------------------------------------------
# entity scoping + no inheritance
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_job_and_conversation_tags_are_scope_checked(factory, migrated_url) -> None:
    _insert_job(migrated_url, JOB_A, SERVER_A)
    _insert_conversation(migrated_url, CONV_A, SERVER_A)
    tag = await acreate_tag(factory, name="ops")
    await aassign_job_tag(factory, JOB_A, tag.tag_id, server_id=SERVER_A)
    await aassign_conversation_tag(
        factory, CONV_A, tag.tag_id, server_id=SERVER_A
    )

    for coro in (
        aassign_job_tag(factory, JOB_A, tag.tag_id, server_id=SERVER_B),
        aassign_conversation_tag(factory, CONV_A, tag.tag_id, server_id=SERVER_B),
        aremove_job_tag(factory, JOB_A, tag.tag_id, server_id=SERVER_B),
        atags_for_job(factory, JOB_A, server_id=SERVER_B),
        atags_for_conversation(factory, CONV_A, server_id=SERVER_B),
        atags_for_job(factory, uuid.uuid4(), server_id=SERVER_A),
        atags_for_conversation(factory, uuid.uuid4(), server_id=SERVER_A),
    ):
        with pytest.raises(TagStoreError) as excinfo:
            await coro
        assert excinfo.value.code is TagErrorCode.ENTITY_NOT_FOUND

    job_items, job_total = await atags_for_job(factory, JOB_A, server_id=SERVER_A)
    conv_items, conv_total = await atags_for_conversation(
        factory, CONV_A, server_id=SERVER_A
    )
    assert job_total == 2 and conv_total == 2  # reserved + ops each
    assert {row.name for row in job_items} == {"unclassified", "ops"}


@pytest.mark.anyio
async def test_no_implicit_inheritance_between_jobs_and_conversations(
    factory, migrated_url
) -> None:
    # The SAME uuid value in both domains: associations never leak across.
    _insert_job(migrated_url, JOB_A, SERVER_A)
    _insert_conversation(migrated_url, JOB_A, SERVER_A)
    tag = await acreate_tag(factory, name="investigation")
    await aassign_job_tag(factory, JOB_A, tag.tag_id, server_id=SERVER_A)
    job_items, _ = await atags_for_job(factory, JOB_A, server_id=SERVER_A)
    conv_items, conv_total = await atags_for_conversation(
        factory, JOB_A, server_id=SERVER_A
    )
    assert {row.name for row in job_items} == {"unclassified", "investigation"}
    assert conv_total == 1
    assert {row.name for row in conv_items} == {"unclassified"}


# ---------------------------------------------------------------------------
# assign/remove: idempotency, provenance, last-tag protection
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_assign_is_idempotent_and_keeps_first_provenance(
    factory, migrated_url
) -> None:
    _insert_job(migrated_url, JOB_A)
    tag = await acreate_tag(factory, name="ops")
    await aassign_job_tag(
        factory, JOB_A, tag.tag_id, server_id=SERVER_A,
        assigned_by="agent-1", assignment_source="agent",
    )
    await aassign_job_tag(
        factory, JOB_A, tag.tag_id, server_id=SERVER_A,
        assigned_by="someone-else", assignment_source="user",
    )
    items, total = await atags_for_job(factory, JOB_A, server_id=SERVER_A)
    assert total == 2  # reserved + ops, no duplicate
    ops = next(row for row in items if row.name == "ops")
    assert ops.assigned_by == "agent-1" and ops.assignment_source == "agent"
    with pytest.raises(TagStoreError) as excinfo:
        await aassign_job_tag(
            factory, JOB_A, uuid.uuid4(), server_id=SERVER_A
        )
    assert excinfo.value.code is TagErrorCode.TAG_NOT_FOUND


@pytest.mark.anyio
async def test_remove_is_idempotent_and_final_tag_needs_replacement(
    factory, migrated_url
) -> None:
    _insert_conversation(migrated_url, CONV_A)
    tag = await acreate_tag(factory, name="ops")
    # Absent association: idempotent no-op.
    await aremove_conversation_tag(factory, CONV_A, tag.tag_id, server_id=SERVER_A)
    await aassign_conversation_tag(factory, CONV_A, tag.tag_id, server_id=SERVER_A)

    # With two tags, removing either is a normal non-final removal.
    await aremove_conversation_tag(
        factory, CONV_A, uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
        server_id=SERVER_A,
    )
    _, total = await atags_for_conversation(factory, CONV_A, server_id=SERVER_A)
    assert total == 1
    # Removing the now-FINAL ordinary tag without a replacement refuses.
    with pytest.raises(TagStoreError) as excinfo:
        await aremove_conversation_tag(
            factory, CONV_A, tag.tag_id, server_id=SERVER_A
        )
    assert excinfo.value.code is TagErrorCode.LAST_TAG
    # The atomic replacement swaps both in one transaction.
    replacement = await acreate_tag(factory, name="followup")
    await aremove_conversation_tag(
        factory, CONV_A, tag.tag_id, server_id=SERVER_A,
        replacement_tag_id=replacement.tag_id,
    )
    items, total = await atags_for_conversation(factory, CONV_A, server_id=SERVER_A)
    assert total == 1
    assert items[0].name == "followup" and items[0].tag_id == replacement.tag_id
    # Replacement equal to the removed tag is rejected up front.
    with pytest.raises(TagStoreError) as excinfo:
        await aremove_conversation_tag(
            factory, CONV_A, replacement.tag_id, server_id=SERVER_A,
            replacement_tag_id=replacement.tag_id,
        )
    assert excinfo.value.code is TagErrorCode.VALIDATION


# ---------------------------------------------------------------------------
# deletion policy: unused / used / force / replacement / reserved
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unused_tag_deletes_and_used_tag_refuses(factory, migrated_url) -> None:
    _insert_job(migrated_url, JOB_A)
    unused = await acreate_tag(factory, name="unused")
    assert await adelete_tag(factory, unused.tag_id) == 0
    remaining, total = await alist_tags(factory, name_filter="unused")
    assert total == 0 and remaining == []

    used = await acreate_tag(factory, name="used")
    await aassign_job_tag(factory, JOB_A, used.tag_id, server_id=SERVER_A)
    with pytest.raises(TagStoreError) as excinfo:
        await adelete_tag(factory, used.tag_id)
    assert excinfo.value.code is TagErrorCode.TAG_IN_USE
    assert _rows(migrated_url, "SELECT count(*) FROM tags WHERE tag_id = %s", (used.tag_id,))[0][0] == 1


@pytest.mark.anyio
async def test_force_deletes_only_when_every_entity_keeps_a_tag(
    factory, migrated_url
) -> None:
    _insert_job(migrated_url, JOB_A)
    _insert_conversation(migrated_url, CONV_A)
    doomed = await acreate_tag(factory, name="doomed")
    await aassign_job_tag(factory, JOB_A, doomed.tag_id, server_id=SERVER_A)
    await aassign_conversation_tag(factory, CONV_A, doomed.tag_id, server_id=SERVER_A)

    removed = await adelete_tag(factory, doomed.tag_id, force=True)
    assert removed == 2
    assert _rows(migrated_url, "SELECT count(*) FROM tags WHERE tag_id = %s", (doomed.tag_id,))[0][0] == 0
    # Both entities keep the reserved default (the 0011 invariant holds).
    assert _rows(migrated_url, "SELECT count(*) FROM job_tags WHERE job_id = %s", (JOB_A,))[0][0] == 1
    assert _rows(migrated_url, "SELECT count(*) FROM conversation_tags WHERE conversation_id = %s", (CONV_A,))[0][0] == 1


@pytest.mark.anyio
async def test_force_needs_replacement_when_an_entity_keeps_only_it(
    factory, migrated_url
) -> None:
    _insert_job(migrated_url, JOB_A)
    doomed = await acreate_tag(factory, name="doomed")
    await aassign_job_tag(factory, JOB_A, doomed.tag_id, server_id=SERVER_A)
    await aremove_job_tag(
        factory, JOB_A, uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
        server_id=SERVER_A, replacement_tag_id=doomed.tag_id,
    )
    # JOB_A now keeps ONLY the doomed tag: plain force must refuse.
    with pytest.raises(TagStoreError) as excinfo:
        await adelete_tag(factory, doomed.tag_id, force=True)
    assert excinfo.value.code is TagErrorCode.TAG_IN_USE

    replacement = await acreate_tag(factory, name="successor")
    removed = await adelete_tag(
        factory, doomed.tag_id, force=True, replacement_tag_id=replacement.tag_id
    )
    assert removed == 1
    items, total = await atags_for_job(factory, JOB_A, server_id=SERVER_A)
    assert total == 1
    assert items[0].name == "successor"
    assert _rows(migrated_url, "SELECT count(*) FROM tags WHERE tag_id = %s", (doomed.tag_id,))[0][0] == 0


@pytest.mark.anyio
async def test_reserved_unclassified_is_undeletable(factory, migrated_url) -> None:
    _insert_job(migrated_url, JOB_A)
    reserved = uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)
    for kwargs in ({}, {"force": True}):
        with pytest.raises(TagStoreError) as excinfo:
            await adelete_tag(factory, reserved, **kwargs)
        assert excinfo.value.code is TagErrorCode.RESERVED_TAG
    assert _rows(migrated_url, "SELECT count(*) FROM tags WHERE tag_id = %s", (reserved,))[0][0] == 1


# ---------------------------------------------------------------------------
# batch previews (single query, bounded, scope-filtered)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_batch_previews_are_bounded_and_scope_filtered(
    factory, migrated_url
) -> None:
    _insert_job(migrated_url, JOB_A)
    _insert_job(migrated_url, JOB_B)
    _insert_job(migrated_url, JOB_C)
    _insert_job(migrated_url, JOB_D, SERVER_B)  # cross-server: absent
    names = ["alpha", "beta", "gamma", "delta", "epsilon"]
    tags = [await acreate_tag(factory, name=name) for name in names]
    await aassign_job_tag(factory, JOB_A, tags[0].tag_id, server_id=SERVER_A)
    for tag in tags[:3]:
        await aassign_job_tag(factory, JOB_B, tag.tag_id, server_id=SERVER_A)
    for tag in tags:
        await aassign_job_tag(factory, JOB_C, tag.tag_id, server_id=SERVER_A)

    previews = await apreview_job_tags(
        factory, [JOB_A, JOB_B, JOB_C, JOB_D], server_id=SERVER_A, preview_limit=3
    )
    assert set(previews) == {JOB_A, JOB_B, JOB_C}
    assert previews[JOB_A] == EntityTagPreview(
        preview=("alpha", "unclassified"), total=2
    )
    assert previews[JOB_B].preview == ("alpha", "beta", "gamma")
    assert previews[JOB_B].total == 4
    assert previews[JOB_C].preview == ("alpha", "beta", "delta")
    assert previews[JOB_C].total == 6
    with pytest.raises(ValueError):
        await apreview_job_tags(factory, [JOB_A], server_id=SERVER_A, preview_limit=0)
    with pytest.raises(ValueError):
        await apreview_job_tags(factory, [JOB_A], server_id=SERVER_A, preview_limit=11)

    _insert_conversation(migrated_url, CONV_A)
    conv_tag = await acreate_tag(factory, name="chat-tag")
    await aassign_conversation_tag(
        factory, CONV_A, conv_tag.tag_id, server_id=SERVER_A
    )
    conv_previews = await apreview_conversation_tags(
        factory, [CONV_A], server_id=SERVER_A
    )
    assert conv_previews[CONV_A].preview == ("chat-tag", "unclassified")
    assert conv_previews[CONV_A].total == 2


# ---------------------------------------------------------------------------
# concurrency: canonical create race + assign/force contention
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_concurrent_canonical_creates_resolve_to_one_row(
    factory, migrated_url
) -> None:
    first, second = await asyncio.gather(
        acreate_tag(factory, name="Race Tag"),
        acreate_tag(factory, name="race  tag"),
    )
    # Both callers observe THE SAME committed row — the race winner's
    # stored display form, never two catalog rows and never a drift.
    assert first.tag_id == second.tag_id
    assert first.name == second.name
    assert first.name in {"Race Tag", "race tag"}
    assert (
        _rows(migrated_url, "SELECT count(*) FROM tags WHERE lower(name) = 'race tag'")[0][0]
        == 1
    )


@pytest.mark.anyio
async def test_concurrent_assign_and_force_serializes_safely(
    factory, migrated_url
) -> None:
    """Assign vs force-delete on one tag: the catalog lock serializes.

    Whichever wins, the loser is translated (tag_not_found) or the force
    refuses, and EVERY entity keeps at least one tag at the end — the 0011
    invariant is never observable broken.
    """
    _insert_job(migrated_url, JOB_A)
    _insert_job(migrated_url, JOB_B)
    contested = await acreate_tag(factory, name="contested")
    await aassign_job_tag(factory, JOB_A, contested.tag_id, server_id=SERVER_A)

    async def _assign() -> None:
        await aassign_job_tag(factory, JOB_B, contested.tag_id, server_id=SERVER_A)

    async def _force() -> None:
        await adelete_tag(factory, contested.tag_id, force=True)

    results = await asyncio.gather(_assign(), _force(), return_exceptions=True)
    # The store either completed the assign or refused it with a typed code —
    # never a raw integrity failure.
    for result in results:
        assert not isinstance(result, Exception) or (
            isinstance(result, TagStoreError)
            and result.code in {TagErrorCode.TAG_NOT_FOUND, TagErrorCode.TAG_IN_USE}
        )
    # The contested catalog row is gone in both interleavings (both
    # entities keep only the reserved default).
    assert _rows(migrated_url, "SELECT count(*) FROM tags WHERE tag_id = %s", (contested.tag_id,))[0][0] == 0
    for job in (JOB_A, JOB_B):
        assert _rows(
            migrated_url, "SELECT count(*) FROM job_tags WHERE job_id = %s", (job,)
        )[0][0] == 1


# ---------------------------------------------------------------------------
# concurrency: commit-time deferred-trigger refusals are typed (LAST_TAG)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_two_concurrent_removers_loser_gets_typed_last_tag(
    factory, migrated_url
) -> None:
    """Two removers race different tags off one job: exactly one commits.

    Both pass their in-transaction checks (two tags present), so the loser's
    refusal can only surface at COMMIT — deferred 0011 trigger, SQLSTATE
    P0001 under asyncpg (a ProgrammingError). It must arrive as the typed
    LAST_TAG code, and the job must retain its final tag.
    """
    _insert_job(migrated_url, JOB_A)
    first = await acreate_tag(factory, name="racer-one")
    second = await acreate_tag(factory, name="racer-two")
    await aremove_job_tag(
        factory,
        JOB_A,
        uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
        server_id=SERVER_A,
        replacement_tag_id=first.tag_id,
    )
    await aassign_job_tag(factory, JOB_A, second.tag_id, server_id=SERVER_A)
    assert _rows(
        migrated_url, "SELECT count(*) FROM job_tags WHERE job_id = %s", (JOB_A,)
    )[0][0] == 2

    results = await asyncio.gather(
        aremove_job_tag(factory, JOB_A, first.tag_id, server_id=SERVER_A),
        aremove_job_tag(factory, JOB_A, second.tag_id, server_id=SERVER_A),
        return_exceptions=True,
    )
    losers = [r for r in results if r is not None]
    winners = [r for r in results if r is None]
    assert len(winners) == 1, f"expected exactly one committed remover: {results!r}"
    assert len(losers) == 1, f"expected exactly one refused remover: {results!r}"
    loser = losers[0]
    assert isinstance(loser, TagStoreError), f"raw DB error leaked: {loser!r}"
    assert loser.code is TagErrorCode.LAST_TAG
    # Final-tag preservation: the loser's delete rolled back; the job keeps
    # exactly one tag — the one the winner did not remove.
    remaining = _rows(
        migrated_url, "SELECT tag_id FROM job_tags WHERE job_id = %s", (JOB_A,)
    )
    assert len(remaining) == 1
    assert remaining[0][0] in (first.tag_id, second.tag_id)


@pytest.mark.anyio
async def test_concurrent_force_delete_vs_remover_typed_last_tag(
    factory, migrated_url
) -> None:
    """Forced catalog deletion vs a concurrent conversation-tag remover.

    The conversation keeps two tags; the force-delete cascades one, the
    remover takes the other. Exactly one writer survives, the loser is the
    typed LAST_TAG refusal (commit-time P0001 for whoever loses the commit
    race), never a raw ProgrammingError, and the conversation always keeps
    exactly one final tag. Exercises adelete_tag and
    aremove_conversation_tag commit paths together.
    """
    _insert_conversation(migrated_url, CONV_A)
    keeper = await acreate_tag(factory, name="keeper")
    doomed = await acreate_tag(factory, name="doomed")
    await aremove_conversation_tag(
        factory,
        CONV_A,
        uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
        server_id=SERVER_A,
        replacement_tag_id=keeper.tag_id,
    )
    await aassign_conversation_tag(factory, CONV_A, doomed.tag_id, server_id=SERVER_A)
    assert _rows(
        migrated_url,
        "SELECT count(*) FROM conversation_tags WHERE conversation_id = %s",
        (CONV_A,),
    )[0][0] == 2

    results = await asyncio.gather(
        aremove_conversation_tag(factory, CONV_A, keeper.tag_id, server_id=SERVER_A),
        adelete_tag(factory, doomed.tag_id, force=True),
        return_exceptions=True,
    )
    # In the overlapped interleaving (the real race) both in-transaction
    # checks pass and the loser is refused AT COMMIT — deferred 0011 trigger
    # P0001 surfaced as the typed LAST_TAG. Boundary interleavings (one
    # writer fully precedes the other's snapshot) yield the contracted
    # in-transaction TAG_IN_USE / LAST_TAG refusals instead. Either way the
    # refusal is typed — never a raw DB exception — and exactly one writer
    # commits.
    losers = [r for r in results if r is not None]
    winners = [r for r in results if r is None]
    assert len(winners) == 1, f"expected exactly one committed writer: {results!r}"
    assert len(losers) == 1, f"expected exactly one refused writer: {results!r}"
    loser = losers[0]
    assert isinstance(loser, TagStoreError), f"raw DB error leaked: {loser!r}"
    assert loser.code in {TagErrorCode.LAST_TAG, TagErrorCode.TAG_IN_USE}
    # Final-tag preservation in every interleaving: exactly one tag remains
    # on the conversation.
    remaining = _rows(
        migrated_url,
        "SELECT tag_id FROM conversation_tags WHERE conversation_id = %s",
        (CONV_A,),
    )
    assert len(remaining) == 1
    assert remaining[0][0] in (keeper.tag_id, doomed.tag_id)
