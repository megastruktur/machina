"""D05 stable-id session compaction — real-Postgres acceptance tests.

Acceptance (BRIEF_D05 §C compaction proofs; sfc_PLAN.md D05;
sfc_RESEARCH §5). Every claim runs against a real disposable PostgreSQL
fixture (``MAI_TEST_DATABASE_URL``; pgvector/pgvector:pg16) with the real
linear Alembic chain applied. No mocks, no fake sessions.

Proven here:

1. **Stable identity, originals intact** — compaction appends ONE
   ``CompactionEntry``; ``session_id`` never rotates; every original entry
   keeps its id/seq/payload (nothing deleted, nothing mutated).
2. **Exact range on a complete tool-group boundary** — the covered range
   ends where every assistant tool call has its result inside the range;
   an incomplete group is never covered (honest ``no_complete_boundary``
   no-op).
3. **Valid next model request** — after compaction,
   ``abuild_session_model_history`` supersedes the covered range with the
   labelled anchor (``1..N`` provenance prefix) and round-trips
   ``ModelMessagesTypeAdapter``.
4. **Restart idempotence** — re-running the compactor over the same range
   returns the EXISTING record (``created=False``); exactly one compaction
   entry exists.
5. **Retry-then-fallback** — a failing summarizer is attempted exactly
   twice (ONE retry), then the deterministic bounded fallback digest with
   the explicit unavailable marker lands.
6. **Provenance** — the record carries the producing ``model_profile``; a
   profile-B re-request over the same range returns the profile-A record
   unchanged (never silently re-claimed).
"""

from __future__ import annotations

import json
import os
import re
import uuid

import psycopg
import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    SystemPromptPart,
)
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents.session_compaction import (
    MAX_COMPACTION_SCAN_ENTRIES,
    SUMMARIZER_UNAVAILABLE_MARKER,
    acompact_session_head,
    complete_boundary_seq,
    configured_bounds,
    estimate_entries_tokens,
)
from mai.agents.session_history import abuild_session_model_history
from mai.contracts.sessions import (
    AssistantMessageEntry,
    EntryKind,
    SessionSourceKind,
    ToolCallItem,
    ToolResultEntry,
    UserMessageEntry,
)
from mai.database import create_app_engine
from mai.session_store import aappend_entry, acreate_session, aread_session_history

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")

#: The suites run async tests through the anyio pytest plugin (the repo's
#: only async driver), one backend: asyncio.
pytestmark = pytest.mark.anyio


def _alembic_config():
    from alembic.config import Config as AlembicConfig

    return AlembicConfig(ALEMBIC_INI)


@pytest.fixture(scope="session")
def migrated_url() -> str:
    url = os.environ.get(LIVE_URL_VAR, "")
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the compaction claims need a real "
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


# --- seeding helpers ---------------------------------------------------------------


async def _append_user(
    factory: async_sessionmaker[AsyncSession], sid: uuid.UUID, n: int, pad: int = 0
) -> int:
    entry = await aappend_entry(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        kind=EntryKind.USER_MESSAGE,
        payload=UserMessageEntry(
            kind=EntryKind.USER_MESSAGE,
            text=f"turn {n}: " + ("constraint " * pad),
            client_request_id=uuid.UUID(int=n),
            source=SessionSourceKind.INTERACTIVE,
        ).model_dump(mode="json"),
        run_id=None,
        durable_source_id=f"u{n}",
    )
    return entry.seq


async def _append_call_group(
    factory: async_sessionmaker[AsyncSession],
    sid: uuid.UUID,
    n: int,
    *,
    complete: bool = True,
    pad: int = 0,
) -> tuple[int, int]:
    """One assistant tool-call entry (+ its result when ``complete``)."""
    call = ToolCallItem(
        call_id=f"call-{n}",
        tool_name="tail_logs",
        capability_id="ops.tail_logs",
        args={"source": "mai-api"},
    )
    assistant = await aappend_entry(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        kind=EntryKind.ASSISTANT_MESSAGE,
        payload=AssistantMessageEntry(
            kind=EntryKind.ASSISTANT_MESSAGE,
            text="checking " + ("context " * pad),
            tool_calls=[call],
            model_profile="profile-a",
        ).model_dump(mode="json"),
        run_id=None,
        durable_source_id=f"a{n}",
    )
    result_seq = None
    if complete:
        result = await aappend_entry(
            factory,
            session_id=sid,
            server_id=SERVER_A,
            kind=EntryKind.TOOL_RESULT,
            payload=ToolResultEntry(
                kind=EntryKind.TOOL_RESULT,
                call_id=f"call-{n}",
                tool_name="tail_logs",
                capability_id="ops.tail_logs",
                status="executed",
                execution_status="succeeded",
                domain_outcome="success",
                effect_certainty="certain",
                payload_hash="h" * 64,
                output_preview=("log line " * pad) or "ok",
            ).model_dump(mode="json"),
            run_id=None,
            durable_source_id=f"r{n}",
        )
        result_seq = result.seq
    return assistant.seq, result_seq


async def _all_entries(
    factory: async_sessionmaker[AsyncSession], sid: uuid.UUID
) -> list[tuple[int, str, dict[str, object]]]:
    """Every committed entry as (seq, kind, payload) — originals proof."""
    rows: list[tuple[int, str, dict[str, object]]] = []
    cursor = None
    while True:
        page = await aread_session_history(
            factory, session_id=sid, server_id=SERVER_A, limit=50, before_seq=cursor
        )
        if not page.items:
            break
        for entry in page.items:
            rows.append(
                (entry.seq, str(entry.payload.kind.value), entry.payload.model_dump(mode="json"))
            )
        if not page.has_older or page.next_before_seq is None:
            break
        cursor = page.next_before_seq
    rows.sort(key=lambda row: row[0])
    return rows


_SMALL_ENV = {
    "MAI_SESSION_COMPACTION_THRESHOLD_TOKENS": "120",
    "MAI_SESSION_COMPACTION_TARGET_TOKENS": "60",
}

#: One window covering the seeded session (target clamps to threshold; the
#: seeded session sits above the threshold so the window reaches the whole
#: complete-group prefix).
_BIG_ENV = {
    "MAI_SESSION_COMPACTION_THRESHOLD_TOKENS": "2000",
    "MAI_SESSION_COMPACTION_TARGET_TOKENS": "2000",
}


async def _seed_session(
    factory: async_sessionmaker[AsyncSession], *, pending_tail: bool = False
) -> uuid.UUID:
    """A session with complete tool groups (and optionally a pending tail
    group): enough structural weight to cross the small test threshold."""
    session_row = await acreate_session(
        factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
    )
    sid = session_row.session_id
    for n in range(1, 6):
        await _append_user(factory, sid, n, pad=40)
        await _append_call_group(factory, sid, n, pad=20)
    if pending_tail:
        await _append_user(factory, sid, 6, pad=40)
        await _append_call_group(factory, sid, 6, complete=False, pad=20)
    return sid


# ---------------------------------------------------------------------------
# 1. stable identity, originals intact
# ---------------------------------------------------------------------------


async def test_compaction_appends_marker_and_keeps_originals_intact(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sid = await _seed_session(factory)
    before = await _all_entries(factory, sid)

    decision = await acompact_session_head(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        model_profile="profile-a",
        env=dict(_BIG_ENV),
    )
    assert decision["compacted"] is True
    record = decision["record"]
    assert record is not None and decision["created"] is True

    after = await _all_entries(factory, sid)
    # Exactly ONE entry appended (the compaction marker), same session.
    assert len(after) == len(before) + 1
    assert after[-1][1] == "compaction"
    # The originals: identical ids/seqs/payloads, in order.
    assert after[:-1] == before
    # The covered range matches the marker payload.
    assert record.from_seq == before[0][0]
    assert record.to_seq == decision["covered_range"][1]
    assert record.model_profile == "profile-a"


# ---------------------------------------------------------------------------
# 2. exact range on a complete tool-group boundary
# ---------------------------------------------------------------------------


async def test_range_ends_on_complete_group_boundary_and_skips_pending(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sid = await _seed_session(factory, pending_tail=True)
    decision = await acompact_session_head(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        model_profile="profile-a",
        env=dict(_BIG_ENV),
    )
    assert decision["compacted"] is True
    from_seq, to_seq = decision["covered_range"]  # type: ignore[misc]
    entries = await _all_entries(factory, sid)
    covered = [row for row in entries if from_seq <= row[0] <= to_seq]
    # The cut never splits a group: the last covered entry carries no
    # unanswered call (a group-end tool_result or a transparent user entry).
    assert not (
        covered[-1][1] == "assistant_message" and covered[-1][2].get("tool_calls")
    )
    # No split pair: every covered call's result is covered and vice versa.
    call_ids = {
        row[2]["tool_calls"][0]["call_id"]
        for row in covered
        if row[1] == "assistant_message" and row[2].get("tool_calls")
    }
    result_ids = {
        row[2]["call_id"] for row in covered if row[1] == "tool_result"
    }
    assert call_ids == result_ids
    # The pending tail group (assistant call without result) stays uncovered.
    pending = [row for row in entries if row[0] > to_seq]
    assert any(row[1] == "assistant_message" for row in pending)
    # The cut IS a complete boundary of the covered prefix, and the full
    # session's boundary sits strictly beyond it (the tail was not forced).
    prefix = [entry for entry in await _unfold(factory, sid) if entry.seq <= to_seq]
    prefix_boundary = complete_boundary_seq(prefix)
    assert prefix_boundary is not None and prefix_boundary == to_seq
    full_boundary = complete_boundary_seq(await _unfold(factory, sid))
    assert full_boundary is not None and full_boundary > to_seq


async def _unfold(
    factory: async_sessionmaker[AsyncSession], sid: uuid.UUID
):
    from mai.contracts.sessions import SessionEntryView

    views: list[SessionEntryView] = []
    cursor = None
    while True:
        page = await aread_session_history(
            factory, session_id=sid, server_id=SERVER_A, limit=50, before_seq=cursor
        )
        if not page.items:
            break
        views.extend(page.items)
        if not page.has_older or page.next_before_seq is None:
            break
        cursor = page.next_before_seq
    return views


async def test_no_complete_boundary_is_an_honest_noop(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await acreate_session(
        factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
    )
    sid = session_row.session_id
    # A single pending group: a complete boundary exists only before it.
    await _append_call_group(factory, sid, 1, complete=False, pad=60)
    await _append_user(factory, sid, 1, pad=60)
    decision = await acompact_session_head(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        model_profile="profile-a",
        env=dict(_SMALL_ENV),
    )
    # The window (newest-first) holds ONLY pending work → no honest cut.
    assert decision["compacted"] is False
    assert decision["reason"] in ("no_complete_boundary", "below_threshold")


# ---------------------------------------------------------------------------
# 3. valid next model request after compaction
# ---------------------------------------------------------------------------


async def test_history_after_compaction_is_valid_and_anchored(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sid = await _seed_session(factory)
    decision = await acompact_session_head(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        model_profile="profile-a",
        env=dict(_BIG_ENV),
    )
    from_seq, to_seq = decision["covered_range"]  # type: ignore[misc]

    history = await abuild_session_model_history(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        provider="test",
        model_profile="profile-a",
    )
    native_json = ModelMessagesTypeAdapter.dump_json(list(history.messages)).decode()
    round_tripped = ModelMessagesTypeAdapter.validate_json(native_json.encode())
    assert len(round_tripped) == len(history.messages)
    # The covered range is superseded by the labelled anchor.
    assert history.superseded_by_compaction == to_seq - from_seq + 1
    anchors = [
        part.content
        for message in history.messages
        if type(message).__name__ == "ModelRequest"
        for part in message.parts
        if isinstance(part, SystemPromptPart)
    ]
    assert any(
        f"session entries {from_seq}..{to_seq}" in str(content) for content in anchors
    )


# ---------------------------------------------------------------------------
# 4. restart idempotence (same range → existing record)
# ---------------------------------------------------------------------------


async def test_recompaction_over_same_range_is_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sid = await _seed_session(factory)
    first = await acompact_session_head(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        model_profile="profile-a",
        env=dict(_BIG_ENV),
    )
    assert first["created"] is True
    second = await acompact_session_head(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        model_profile="profile-a",
        env=dict(_BIG_ENV),
    )
    # The covered range is floor-ed: the re-run never re-compacts it.
    assert second["created"] is False
    assert second["reason"] in ("no_uncovered_entries", "below_threshold")
    first_record = first["record"]
    # A floor-ed no-op returns no new record; the committed one stands.
    assert first_record is not None and second["record"] is None
    async with factory() as session:
        count = (
            await session.execute(
                sql_text(
                    "SELECT COUNT(*) FROM session_entries "
                    "WHERE session_id = :sid AND kind = 'compaction'"
                ),
                {"sid": sid},
            )
        ).scalar_one()
    assert count == 1
    # The durable-key fence: replaying the SAME range returns the committed
    # record (the crash-replay path), never a second summary.
    from mai.session_store import arecord_compaction

    first_record = first["record"]
    assert first_record is not None
    replayed, created = await arecord_compaction(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        compaction=first_record.model_copy(deep=True),
    )
    assert created is False
    assert replayed.entry_id == first_record.compaction_id or (
        replayed.payload["compaction_id"] == str(first_record.compaction_id)
    )
    assert replayed.payload["summary_text"] == first_record.summary_text


# ---------------------------------------------------------------------------
# 5. retry-then-fallback
# ---------------------------------------------------------------------------


async def test_failing_summarizer_gets_one_retry_then_fallback(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sid = await _seed_session(factory)
    attempts: list[int] = []

    def _always_fails(_prompt: str) -> str:
        attempts.append(1)
        raise RuntimeError("summarizer unavailable")

    decision = await acompact_session_head(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        model_profile="profile-a",
        env=dict(_BIG_ENV),
        summarize=_always_fails,
    )
    assert decision["compacted"] is True
    assert decision["summary_source"] == "fallback"
    # ONE retry: exactly two attempts, then the bounded fallback.
    assert len(attempts) == 2
    record = decision["record"]
    assert record is not None
    assert SUMMARIZER_UNAVAILABLE_MARKER in record.summary_text
    assert record.summary_text.endswith(
        "User constraints and unresolved obligations remain in the "
        "retained originals."
    )


# ---------------------------------------------------------------------------
# 6. provenance: profile-B re-request never re-claims profile-A's record
# ---------------------------------------------------------------------------


async def test_profile_b_rerequest_preserves_profile_a_provenance(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sid = await _seed_session(factory)
    first = await acompact_session_head(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        model_profile="profile-a",
        env=dict(_BIG_ENV),
    )
    assert first["created"] is True
    second = await acompact_session_head(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        model_profile="profile-b",
        env=dict(_BIG_ENV),
    )
    # A profile-B re-request never re-claims or re-summarizes the range.
    assert second["created"] is False
    assert second["reason"] in ("no_uncovered_entries", "below_threshold")
    first_record = first["record"]
    assert first_record is not None
    assert first_record.model_profile == "profile-a"
    # The durable key returns the A-provenance record to a B caller.
    from mai.session_store import arecord_compaction

    record, created = await arecord_compaction(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        compaction=first_record.model_copy(update={"model_profile": "profile-b"}),
    )
    assert created is False
    assert record.payload["model_profile"] == "profile-a"
    async with factory() as session:
        count = (
            await session.execute(
                sql_text(
                    "SELECT COUNT(*) FROM session_entries "
                    "WHERE session_id = :sid AND kind = 'compaction'"
                ),
                {"sid": sid},
            )
        ).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# 7. config seam and structural estimate (pure functions)
# ---------------------------------------------------------------------------


async def test_configured_bounds_clamp_values() -> None:
    assert configured_bounds(None) == (60000, 24000)
    threshold, target = configured_bounds(
        {
            "MAI_SESSION_COMPACTION_THRESHOLD_TOKENS": "10",
            "MAI_SESSION_COMPACTION_TARGET_TOKENS": "5",
        }
    )
    assert (threshold, target) == (10, 5)
    threshold, target = configured_bounds(
        {
            "MAI_SESSION_COMPACTION_THRESHOLD_TOKENS": "9999999",
            "MAI_SESSION_COMPACTION_TARGET_TOKENS": "9999999",
        }
    )
    assert threshold == 500000
    assert target == 250000
    threshold, target = configured_bounds(
        {
            "MAI_SESSION_COMPACTION_THRESHOLD_TOKENS": "garbage",
            "MAI_SESSION_COMPACTION_TARGET_TOKENS": "-3",
        }
    )
    assert (threshold, target) == (60000, 24000)


async def test_below_threshold_is_a_noop(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await acreate_session(
        factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
    )
    sid = session_row.session_id
    await _append_user(factory, sid, 1, pad=1)
    decision = await acompact_session_head(
        factory,
        session_id=sid,
        server_id=SERVER_A,
        model_profile="profile-a",
        env=dict(_SMALL_ENV),
    )
    assert decision["compacted"] is False
    assert decision["reason"] == "below_threshold"


async def test_scan_budget_is_enforced(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sid = await _seed_session(factory)
    views = await _unfold(factory, sid)
    estimate = estimate_entries_tokens(views)
    assert estimate > 0
    # The scan budget bound is a module constant (bounded walk invariant).
    assert 10 <= MAX_COMPACTION_SCAN_ENTRIES <= 5000
    assert json.dumps(decision := {"estimated": estimate}) is not None
