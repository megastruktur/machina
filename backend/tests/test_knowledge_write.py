"""Focused tests for the provenance-bound fact CAS, merge/conflict and
retraction write seam (ask_12).

Real PostgreSQL (the campaign disposable fixture) + a real confined
``ServiceMapStore``: provenance must resolve to persisted same-scope
successful session entries BEFORE any write; generation and per-fact
compare-and-set refuse stale work; unrelated keys merge, identical
same-key proposals are idempotent, divergent same-key proposals are
conflicts; capacity is explicit; retraction never deletes; operator
notes and the catalog runbook stay byte-for-byte untouched; no
transcript blob is ever stored. No mocks of the code under test.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import async_sessionmaker

from mai.applications.knowledge_contracts import (
    FactCas,
    FactSourceKind,
    LearnRequest,
    MAX_ACTIVE_FACTS_PER_SERVICE,
    OperationStatus,
    ProposedFactDelta,
    ScopeMismatchError,
    TrustedSourceContext,
)
from mai.applications.knowledge_write import (
    FactApplyResult,
    KnowledgeWriteStore,
    ProvenanceRefusalError,
    RetractionResult,
)
from mai.applications.service_map import ServiceMapStore
from mai.database import create_app_engine

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
SERVER_B = uuid.UUID("9a8b7c6d-1e2f-4a3b-8c5d-6e7f8a9b0c1d")
APP_MAIN_ID = uuid.UUID("0aaaaaaa-0000-4000-8000-000000000001")

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


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _set_migration_env(url: str) -> dict[str, str | None]:
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url
    return {"previous": previous}


def _restore_migration_env(saved: dict[str, str | None]) -> None:
    if saved["previous"] is None:
        os.environ.pop("MAI_DATABASE_URL", None)
    else:
        os.environ["MAI_DATABASE_URL"] = str(saved["previous"])


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database upgraded from zero to the current head."""
    url = os.environ.get(LIVE_URL_VAR, "")
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the write claims need a real "
            "disposable Postgres fixture"
        )
    saved = _set_migration_env(url)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
        command.upgrade(_alembic_config(), "head")
    finally:
        _restore_migration_env(saved)
    return url


@pytest.fixture
def factory(migrated_url: str) -> async_sessionmaker:
    engine, sessions = create_app_engine(migrated_url)
    # Per-test SQL isolation: every test starts with clean knowledge
    # tables (seeded session log rows use fresh UUIDs per test).
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE TABLE service_learned_fact_revision, "
            "service_learned_fact, service_learning_operation, "
            "service_knowledge_identity CASCADE"
        )
    yield sessions
    _run(engine.dispose())


@pytest.fixture
def map_store(tmp_path: Path) -> ServiceMapStore:
    return ServiceMapStore(tmp_path / "service_map")


# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------


def _seed_session_log(
    migrated_url: str,
    *,
    server_id: uuid.UUID,
    entry_ids: dict[uuid.UUID, tuple[str, dict[str, object]]],
) -> uuid.UUID:
    """Persist one interactive session + one succeeded run whose tool
    results carry the given entry identities. Returns the run id."""
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, server_id, source, status) "
            "VALUES (%s, %s, 'interactive', 'idle')",
            (session_id, str(server_id)),
        )
        conn.execute(
            "INSERT INTO session_runs (run_id, session_id, server_id, "
            "state, model_profile, client_request_id) VALUES "
            "(%s, %s, %s, 'succeeded', 'test-profile', %s)",
            (run_id, session_id, str(server_id), str(uuid.uuid4())),
        )
        for seq, (entry_id, (kind, payload)) in enumerate(
            entry_ids.items(), start=1
        ):
            conn.execute(
                "INSERT INTO session_entries (entry_id, session_id, run_id, "
                "seq, kind, payload, durable_source_id) VALUES "
                "(%s, %s, %s, %s, %s, %s::jsonb, %s)",
                (
                    entry_id,
                    session_id,
                    run_id,
                    seq,
                    kind,
                    json.dumps(payload),
                    f"seed-{seq:06d}",
                ),
            )
    return run_id


def _successful_tool_entry(
    payload_tail: dict[str, object] | None = None,
) -> tuple[str, dict[str, object]]:
    payload: dict[str, object] = {
        "status": "executed",
        "tool": "app.list_units",
    }
    if payload_tail is not None:
        payload.update(payload_tail)
    return "tool_result", payload


def _trusted(server_id: uuid.UUID) -> TrustedSourceContext:
    return TrustedSourceContext(
        server_id=server_id,
        run_id=uuid.uuid4(),
        session_id=None,
        tool_call_id=f"call-{uuid.uuid4().hex[:12]}",
        actor="agent:mai",
        grants=["knowledge.learn"],
        context_at=datetime.now(tz=timezone.utc),
    )


def _delta(
    key: str,
    statement: str,
    entry_ids: list[uuid.UUID],
    *,
    source_kind: FactSourceKind = FactSourceKind.TOOL_RESULT,
) -> ProposedFactDelta:
    return ProposedFactDelta(
        category="runtime",
        key=key,
        statement=statement,
        source_kind=source_kind,
        source_refs=[str(entry_id) for entry_id in entry_ids],
    )


def _apply(
    factory: async_sessionmaker,
    *,
    server_id: uuid.UUID = SERVER_A,
    application_id: uuid.UUID = APP_MAIN_ID,
    facts: list[ProposedFactDelta],
    trusted: TrustedSourceContext | None = None,
    expected_generation: int = 1,
) -> FactApplyResult:
    store = KnowledgeWriteStore(factory)
    return _run(
        store.apply_facts(
            server_id=server_id,
            application_id=application_id,
            request=LearnRequest(
                facts=facts, expected_generation=expected_generation
            ),
            trusted=trusted if trusted is not None else _trusted(server_id),
            expected_generation=expected_generation,
        )
    )


def _fact_rows(migrated_url: str, application_id: uuid.UUID) -> list[dict]:
    with psycopg.connect(migrated_url) as conn:
        rows = conn.execute(
            "SELECT fact_id, category, key, statement, revision, status, "
            "source_kind, source_ref, supersedes_fact_id FROM "
            "service_learned_fact WHERE application_id = %s "
            "ORDER BY category, key, revision",
            (str(application_id),),
        ).fetchall()
    names = (
        "fact_id",
        "category",
        "key",
        "statement",
        "revision",
        "status",
        "source_kind",
        "source_ref",
        "supersedes_fact_id",
    )
    return [dict(zip(names, row, strict=True)) for row in rows]


def _history_count(migrated_url: str, application_id: uuid.UUID) -> int:
    with psycopg.connect(migrated_url) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM service_learned_fact_revision WHERE "
                "application_id = %s",
                (str(application_id),),
            ).fetchone()[0]
        )


# ---------------------------------------------------------------------------
# 1. provenance required
# ---------------------------------------------------------------------------


def test_provenance_nonexistent_ref_refused_without_writes(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    ghost = uuid.uuid4()
    result = _apply(
        factory,
        facts=[_delta("upstream.image", "traefik v3.1", [ghost])],
    )
    assert result.status is OperationStatus.FAILED
    assert result.error_code == "invalid_provenance"
    assert result.applied_fact_ids == []
    assert _fact_rows(migrated_url, APP_MAIN_ID) == []
    assert _history_count(migrated_url, APP_MAIN_ID) == 0


def test_provenance_unsuccessful_tool_result_refused(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    failed_entry = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={
            failed_entry: (
                "tool_result",
                {"status": "failed", "tool": "app.list_units"},
            )
        },
    )
    result = _apply(
        factory,
        facts=[_delta("upstream.image", "traefik v3.1", [failed_entry])],
    )
    assert result.status is OperationStatus.FAILED
    assert result.error_code == "invalid_provenance"
    assert _fact_rows(migrated_url, APP_MAIN_ID) == []


# ---------------------------------------------------------------------------
# 2. cross-server denial
# ---------------------------------------------------------------------------


def test_cross_server_provenance_denied_without_writes(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    foreign_entry = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_B,
        entry_ids={foreign_entry: _successful_tool_entry()},
    )
    with pytest.raises(ProvenanceRefusalError):
        _apply(
            factory,
            facts=[_delta("upstream.image", "traefik v3.1", [foreign_entry])],
        )
    assert _fact_rows(migrated_url, APP_MAIN_ID) == []
    assert _history_count(migrated_url, APP_MAIN_ID) == 0


def test_user_statement_provenance_accepts_user_message_entry(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    user_entry = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={
            user_entry: ("user_message", {"text": "please remember this"})
        },
    )
    result = _apply(
        factory,
        facts=[
            _delta(
                "operator.intent",
                "operator asked to keep the maintenance window",
                [user_entry],
                source_kind=FactSourceKind.USER_STATEMENT,
            )
        ],
    )
    assert result.status is OperationStatus.APPLIED
    rows = _fact_rows(migrated_url, APP_MAIN_ID)
    assert rows[0]["source_kind"] == "user_statement"


def test_user_statement_cannot_pose_as_tool_result(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    user_entry = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={user_entry: ("user_message", {"text": "hi"})},
    )
    result = _apply(
        factory,
        facts=[_delta("upstream.image", "traefik v3.1", [user_entry])],
    )
    assert result.status is OperationStatus.FAILED
    assert result.error_code == "invalid_provenance"
    assert _fact_rows(migrated_url, APP_MAIN_ID) == []


# ---------------------------------------------------------------------------
# 3. generation CAS
# ---------------------------------------------------------------------------


def test_generation_cas_stale_refused_correct_succeeds(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    entry_id = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={entry_id: _successful_tool_entry()},
    )
    stale = _apply(
        factory,
        facts=[_delta("upstream.image", "traefik v3.1", [entry_id])],
        expected_generation=5,
    )
    assert stale.status is OperationStatus.FAILED
    assert stale.error_code == "revision_conflict"
    assert stale.generation == -1
    # Stale work never fabricates an identity row either.
    with psycopg.connect(migrated_url) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM service_knowledge_identity"
            ).fetchone()[0]
            == 0
        )
    assert _fact_rows(migrated_url, APP_MAIN_ID) == []

    good = _apply(
        factory,
        facts=[_delta("upstream.image", "traefik v3.1", [entry_id])],
        expected_generation=1,
    )
    assert good.status is OperationStatus.APPLIED
    assert good.generation == 1
    assert len(good.applied_fact_ids) == 1
    # A second write at the SAME generation succeeds because apply_facts
    # does not bump generation.
    second = _apply(
        factory,
        facts=[_delta("port.mapping", "8080 published", [entry_id])],
        expected_generation=1,
    )
    assert second.status is OperationStatus.APPLIED
    rows = _fact_rows(migrated_url, APP_MAIN_ID)
    assert [row["key"] for row in rows] == ["port.mapping", "upstream.image"]
    # The identity stays at generation 1: a stale generation-2 CAS is
    # refused against it.
    stale_again = _apply(
        factory,
        facts=[_delta("cert.source", "letsencrypt", [entry_id])],
        expected_generation=2,
    )
    assert stale_again.status is OperationStatus.FAILED
    assert stale_again.error_code == "revision_conflict"
    assert stale_again.generation == 1
    assert [row["key"] for row in _fact_rows(migrated_url, APP_MAIN_ID)] == [
        "port.mapping",
        "upstream.image",
    ]


# ---------------------------------------------------------------------------
# 4. per-fact CAS correction
# ---------------------------------------------------------------------------


def test_per_fact_cas_stale_refused_correct_supersedes(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    entry_id = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={entry_id: _successful_tool_entry()},
    )
    applied = _apply(
        factory,
        facts=[_delta("upstream.image", "traefik v3.1", [entry_id])],
    )
    fact_id = applied.applied_fact_ids[0]
    assert _history_count(migrated_url, APP_MAIN_ID) == 1

    store = KnowledgeWriteStore(factory)
    stale = _run(
        store.correct_fact(
            server_id=SERVER_A,
            application_id=APP_MAIN_ID,
            cas=FactCas(
                fact_id=fact_id,
                expected_revision=2,
                new_statement="traefik v3.2",
            ),
            trusted=_trusted(SERVER_A),
            expected_generation=1,
        )
    )
    assert stale.status is OperationStatus.FAILED
    assert stale.error_code == "revision_conflict"
    assert _history_count(migrated_url, APP_MAIN_ID) == 1

    corrected = _run(
        store.correct_fact(
            server_id=SERVER_A,
            application_id=APP_MAIN_ID,
            cas=FactCas(
                fact_id=fact_id,
                expected_revision=1,
                new_statement="traefik v3.2",
            ),
            trusted=_trusted(SERVER_A),
            expected_generation=1,
        )
    )
    assert corrected.status is OperationStatus.APPLIED
    assert corrected.superseded_fact_ids == [fact_id]
    assert corrected.revisions_written == 1
    rows = _fact_rows(migrated_url, APP_MAIN_ID)
    by_status = {row["status"]: row for row in rows}
    assert set(by_status) == {"superseded", "active"}
    assert by_status["superseded"]["revision"] == 1
    assert by_status["active"]["revision"] == 2
    assert by_status["active"]["supersedes_fact_id"] == fact_id
    assert by_status["active"]["statement"] == "traefik v3.2"
    assert _history_count(migrated_url, APP_MAIN_ID) == 2


# ---------------------------------------------------------------------------
# 5. merge vs conflict vs idempotent
# ---------------------------------------------------------------------------


def test_unrelated_keys_merge_in_one_request(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    first = uuid.uuid4()
    second = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={
            first: _successful_tool_entry(),
            second: _successful_tool_entry(),
        },
    )
    result = _apply(
        factory,
        facts=[
            _delta("upstream.image", "traefik v3.1", [first]),
            _delta("port.mapping", "8080 published", [second]),
        ],
    )
    assert result.status is OperationStatus.APPLIED
    assert len(result.applied_fact_ids) == 2
    assert result.conflict_keys == []
    rows = _fact_rows(migrated_url, APP_MAIN_ID)
    assert sorted(row["key"] for row in rows) == [
        "port.mapping",
        "upstream.image",
    ]
    assert _history_count(migrated_url, APP_MAIN_ID) == 2


def test_identical_same_key_is_idempotent(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    entry_id = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={entry_id: _successful_tool_entry()},
    )
    result = _apply(
        factory,
        facts=[
            _delta("upstream.image", "traefik v3.1", [entry_id]),
            _delta(
                "upstream.image", "traefik   v3.1", [entry_id, entry_id]
            ),
        ],
    )
    assert result.status is OperationStatus.APPLIED
    assert len(result.applied_fact_ids) == 1
    assert len(result.idempotent_fact_ids) == 1
    assert result.idempotent_fact_ids[0] == result.applied_fact_ids[0]
    rows = _fact_rows(migrated_url, APP_MAIN_ID)
    assert len(rows) == 1
    assert rows[0]["revision"] == 1
    assert _history_count(migrated_url, APP_MAIN_ID) == 1


def test_divergent_same_key_is_refused_conflict(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    first = uuid.uuid4()
    second = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={
            first: _successful_tool_entry(),
            second: _successful_tool_entry(),
        },
    )
    result = _apply(
        factory,
        facts=[
            _delta("upstream.image", "traefik v3.1", [first]),
            _delta("upstream.image", "traefik v3.2", [second]),
        ],
    )
    assert result.status is OperationStatus.FAILED
    assert result.error_code == "conflict"
    assert result.conflict_keys == ["upstream.image"]
    assert result.applied_fact_ids == []
    assert _fact_rows(migrated_url, APP_MAIN_ID) == []

    # Mixed request: unrelated keys still merge; the divergent key is
    # refused and never silently applied.
    mixed = _apply(
        factory,
        facts=[
            _delta("port.mapping", "8080 published", [first]),
            _delta("upstream.image", "traefik v3.1", [first]),
            _delta("upstream.image", "traefik v3.2", [second]),
        ],
    )
    assert mixed.status is OperationStatus.PARTIAL
    assert mixed.conflict_keys == ["upstream.image"]
    keys = [row["key"] for row in _fact_rows(migrated_url, APP_MAIN_ID)]
    assert keys == ["port.mapping"]
    assert "upstream.image" not in keys


def test_divergent_against_stored_active_fact_is_conflict(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    entry_id = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={entry_id: _successful_tool_entry()},
    )
    _apply(
        factory,
        facts=[_delta("upstream.image", "traefik v3.1", [entry_id])],
    )
    again = _apply(
        factory,
        facts=[_delta("upstream.image", "traefik v3.2", [entry_id])],
    )
    assert again.status is OperationStatus.FAILED
    assert again.error_code == "conflict"
    assert again.conflict_keys == ["upstream.image"]
    rows = _fact_rows(migrated_url, APP_MAIN_ID)
    assert len(rows) == 1
    assert rows[0]["statement"] == "traefik v3.1"
    # The identical re-proposal stays idempotent.
    same = _apply(
        factory,
        facts=[_delta("upstream.image", "traefik v3.1", [entry_id])],
    )
    assert same.status is OperationStatus.APPLIED
    assert same.applied_fact_ids == []
    assert len(same.idempotent_fact_ids) == 1
    assert _history_count(migrated_url, APP_MAIN_ID) == 1


# ---------------------------------------------------------------------------
# 6. capacity
# ---------------------------------------------------------------------------


def test_capacity_limit_refuses_explicitly(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    entry_id = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={entry_id: _successful_tool_entry()},
    )
    generation = 1
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO service_knowledge_identity (server_id, "
            "application_id, generation) VALUES (%s, %s, %s)",
            (str(SERVER_A), str(APP_MAIN_ID), generation),
        )
        for index in range(MAX_ACTIVE_FACTS_PER_SERVICE):
            conn.execute(
                "INSERT INTO service_learned_fact (fact_id, server_id, "
                "application_id, generation, category, key, statement, "
                "revision, status, source_kind, source_ref, source_run_id, "
                "verified_at) VALUES (%s, %s, %s, %s, 'runtime', %s, %s, "
                "1, 'active', 'tool_result', %s, %s, now())",
                (
                    uuid.uuid4(),
                    str(SERVER_A),
                    str(APP_MAIN_ID),
                    generation,
                    f"key-{index:03d}",
                    f"seeded fact {index}",
                    str(entry_id),
                    str(uuid.uuid4()),
                ),
            )

    over = _apply(
        factory,
        facts=[_delta("one.too.many", "would be the 65th fact", [entry_id])],
    )
    assert over.status is OperationStatus.FAILED
    assert over.error_code == "capacity_exceeded"
    rows = _fact_rows(migrated_url, APP_MAIN_ID)
    assert len(rows) == MAX_ACTIVE_FACTS_PER_SERVICE
    assert all(row["status"] == "active" for row in rows)

    # Replacing an existing ACTIVE fact stays within capacity.
    victim = rows[0]
    store = KnowledgeWriteStore(factory)
    corrected = _run(
        store.correct_fact(
            server_id=SERVER_A,
            application_id=APP_MAIN_ID,
            cas=FactCas(
                fact_id=victim["fact_id"],
                expected_revision=victim["revision"],
                new_statement="corrected in place",
            ),
            trusted=_trusted(SERVER_A),
            expected_generation=1,
        )
    )
    assert corrected.status is OperationStatus.APPLIED
    all_rows = _fact_rows(migrated_url, APP_MAIN_ID)
    active_after = [row for row in all_rows if row["status"] == "active"]
    assert len(active_after) == MAX_ACTIVE_FACTS_PER_SERVICE
    assert len(all_rows) == MAX_ACTIVE_FACTS_PER_SERVICE + 1


# ---------------------------------------------------------------------------
# 7. retraction
# ---------------------------------------------------------------------------


def test_retraction_cas_marks_retracted_without_delete(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    entry_id = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={entry_id: _successful_tool_entry()},
    )
    applied = _apply(
        factory,
        facts=[_delta("upstream.image", "traefik v3.1", [entry_id])],
    )
    fact_id = applied.applied_fact_ids[0]
    store = KnowledgeWriteStore(factory)

    stale = _run(
        store.retract_fact(
            server_id=SERVER_A,
            application_id=APP_MAIN_ID,
            fact_id=fact_id,
            expected_revision=3,
            expected_generation=1,
        )
    )
    assert stale.status is OperationStatus.FAILED
    assert stale.error_code == "revision_conflict"
    assert _history_count(migrated_url, APP_MAIN_ID) == 1

    stale_generation = _run(
        store.retract_fact(
            server_id=SERVER_A,
            application_id=APP_MAIN_ID,
            fact_id=fact_id,
            expected_revision=1,
            expected_generation=7,
        )
    )
    assert stale_generation.status is OperationStatus.FAILED
    assert stale_generation.error_code == "revision_conflict"
    assert _history_count(migrated_url, APP_MAIN_ID) == 1

    retracted = _run(
        store.retract_fact(
            server_id=SERVER_A,
            application_id=APP_MAIN_ID,
            fact_id=fact_id,
            expected_revision=1,
            expected_generation=1,
            reason="upstream confirmed the version was misread",
        )
    )
    assert isinstance(retracted, RetractionResult)
    assert retracted.status is OperationStatus.APPLIED
    assert retracted.new_revision == 2
    rows = _fact_rows(migrated_url, APP_MAIN_ID)
    assert len(rows) == 1  # the row is NOT deleted
    assert rows[0]["status"] == "retracted"
    assert rows[0]["revision"] == 2
    assert _history_count(migrated_url, APP_MAIN_ID) == 2
    with psycopg.connect(migrated_url) as conn:
        history = conn.execute(
            "SELECT revision, status FROM service_learned_fact_revision "
            "WHERE fact_id = %s ORDER BY revision",
            (str(fact_id),),
        ).fetchall()
    assert history == [(1, "active"), (2, "retracted")]


# ---------------------------------------------------------------------------
# 8. operator notes untouched
# ---------------------------------------------------------------------------


def test_operator_notes_and_runbook_byte_for_byte_untouched(
    migrated_url: str, factory: async_sessionmaker, map_store: ServiceMapStore
) -> None:
    entry_id = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={entry_id: _successful_tool_entry()},
    )
    entry = map_store.upsert_entry(
        SERVER_A,
        application_id=APP_MAIN_ID,
        name="app-main",
        category="app",
        origin="compose",
        compose_project="main",
        working_dir="/srv/main",
        description="the main app",
        custom_instructions="operator notes for app-main",
    )
    runbook_before = map_store.upsert_catalog_runbook(
        name="app-main",
        category="app",
        description="restart the main app",
        instructions={
            "install": "docker compose up -d",
            "update": "docker compose pull && docker compose up -d",
            "uninstall": "docker compose down",
            "backup": "tar czf /tmp/main.tgz /srv/main",
            "verify": "curl -f http://localhost:8080/health",
        },
    )
    instructions_before = entry.custom_instructions
    runbook_before = map_store.get_catalog_runbook(runbook_before.runbook_id)
    assert runbook_before is not None
    assert instructions_before == "operator notes for app-main"

    applied = _apply(
        factory,
        facts=[_delta("upstream.image", "traefik v3.1", [entry_id])],
    )
    fact_id = applied.applied_fact_ids[0]
    store = KnowledgeWriteStore(factory)
    retracted = _run(
        store.retract_fact(
            server_id=SERVER_A,
            application_id=APP_MAIN_ID,
            fact_id=fact_id,
            expected_revision=1,
            expected_generation=1,
        )
    )
    assert retracted.status is OperationStatus.APPLIED

    entry_after = map_store.get_entry(SERVER_A, entry.application_id)
    assert entry_after is not None
    assert entry_after.custom_instructions == instructions_before
    assert entry_after.description == entry.description
    assert entry_after.revision == entry.revision
    runbook_after = map_store.get_catalog_runbook(runbook_before.runbook_id)
    assert runbook_after is not None
    assert runbook_after.to_json() == runbook_before.to_json()


# ---------------------------------------------------------------------------
# 9. no transcript retention
# ---------------------------------------------------------------------------


def test_no_transcript_payload_retained(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    secret_tail = "SECRET_RAW_OUTPUT_MARKER_do_not_store"
    entry_id = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={
            entry_id: _successful_tool_entry({"output": secret_tail})
        },
    )
    result = _apply(
        factory,
        facts=[
            _delta("upstream.image", "traefik v3.1", [entry_id])
        ],
    )
    assert result.status is OperationStatus.APPLIED
    with psycopg.connect(migrated_url) as conn:
        columns = [
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE "
                "table_name = 'service_learned_fact'"
            ).fetchall()
        ]
        rows = conn.execute(
            "SELECT statement, source_ref, source_tool_call_id FROM "
            "service_learned_fact WHERE application_id = %s",
            (str(APP_MAIN_ID),),
        ).fetchall()
        blob_columns = [
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE "
                "table_name = 'service_learned_fact' AND "
                "(data_type LIKE '%bytea%' OR data_type = 'jsonb' OR "
                "column_name ILIKE '%payload%' OR column_name ILIKE "
                "'%transcript%' OR column_name ILIKE '%raw%')"
            ).fetchall()
        ]
    assert blob_columns == []
    for forbidden in ("payload", "transcript", "raw_output", "output"):
        assert forbidden not in columns
    for statement, source_ref, _tool_call in rows:
        assert secret_tail not in statement
        assert secret_tail not in source_ref
        assert len(source_ref) <= 256
        assert str(entry_id) in source_ref


def test_bounded_source_ref_multi_ref_deterministic(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    first = uuid.uuid4()
    second = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={
            first: _successful_tool_entry(),
            second: _successful_tool_entry(),
        },
    )
    result = _apply(
        factory,
        facts=[_delta("upstream.image", "traefik v3.1", [second, first])],
    )
    assert result.status is OperationStatus.APPLIED
    rows = _fact_rows(migrated_url, APP_MAIN_ID)
    expected_joined = ",".join(sorted([str(first), str(second)]))
    assert rows[0]["source_ref"] == expected_joined


# ---------------------------------------------------------------------------
# fix round 1: provenance hardening, category scoping, scope guards
# ---------------------------------------------------------------------------


def test_same_entry_claimed_under_two_kinds_is_refused(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    entry_id = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={entry_id: _successful_tool_entry()},
    )
    result = _apply(
        factory,
        facts=[
            _delta("upstream.image", "traefik v3.1", [entry_id]),
            _delta(
                "operator.note",
                "traefik confirmed verbally",
                [entry_id],
                source_kind=FactSourceKind.USER_STATEMENT,
            ),
        ],
    )
    # One persisted entry has exactly ONE kind: a reference claimed as
    # both a tool result and a user statement can never satisfy both.
    assert result.status is OperationStatus.FAILED
    assert result.error_code == "invalid_provenance"
    assert _fact_rows(migrated_url, APP_MAIN_ID) == []
    assert _history_count(migrated_url, APP_MAIN_ID) == 0


def test_non_uuid_ref_refused_cleanly_and_uppercase_accepted(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    entry_id = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={entry_id: _successful_tool_entry()},
    )
    garbage = _apply(
        factory,
        facts=[_delta("upstream.image", "traefik v3.1", ["not-a-uuid"])],
    )
    # The refusal is the structured invalid_provenance code — never an
    # unhandled driver cast error.
    assert garbage.status is OperationStatus.FAILED
    assert garbage.error_code == "invalid_provenance"
    assert _fact_rows(migrated_url, APP_MAIN_ID) == []

    upper = _apply(
        factory,
        facts=[
            _delta(
                "upstream.image", "traefik v3.1", [str(entry_id).upper()]
            )
        ],
    )
    assert upper.status is OperationStatus.APPLIED
    assert len(upper.applied_fact_ids) == 1


def test_same_key_under_different_categories_merge(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    entry_id = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={entry_id: _successful_tool_entry()},
    )
    runtime = ProposedFactDelta(
        category="runtime",
        key="port",
        statement="8080 published",
        source_kind=FactSourceKind.TOOL_RESULT,
        source_refs=[str(entry_id)],
    )
    network = ProposedFactDelta(
        category="network",
        key="port",
        statement="published behind traefik",
        source_kind=FactSourceKind.TOOL_RESULT,
        source_refs=[str(entry_id)],
    )
    result = _apply(factory, facts=[runtime, network])
    # The active-key identity is (category, key): the same key under a
    # different category is a DISTINCT fact, not a conflict.
    assert result.status is OperationStatus.APPLIED
    assert result.conflict_keys == []
    assert len(result.applied_fact_ids) == 2
    rows = _fact_rows(migrated_url, APP_MAIN_ID)
    assert sorted(row["key"] for row in rows) == ["port", "port"]
    assert sorted(row["category"] for row in rows) == ["network", "runtime"]
    assert _history_count(migrated_url, APP_MAIN_ID) == 2


def test_correct_fact_foreign_trusted_scope_refused(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    entry_id = uuid.uuid4()
    _seed_session_log(
        migrated_url,
        server_id=SERVER_A,
        entry_ids={entry_id: _successful_tool_entry()},
    )
    applied = _apply(
        factory,
        facts=[_delta("upstream.image", "traefik v3.1", [entry_id])],
    )
    fact_id = applied.applied_fact_ids[0]
    store = KnowledgeWriteStore(factory)
    with pytest.raises(ScopeMismatchError):
        _run(
            store.correct_fact(
                server_id=SERVER_A,
                application_id=APP_MAIN_ID,
                cas=FactCas(
                    fact_id=fact_id,
                    expected_revision=1,
                    new_statement="traefik v3.2",
                ),
                trusted=_trusted(SERVER_B),
                expected_generation=1,
            )
        )
    rows = _fact_rows(migrated_url, APP_MAIN_ID)
    assert len(rows) == 1
    assert rows[0]["status"] == "active"
    assert rows[0]["statement"] == "traefik v3.1"
    assert _history_count(migrated_url, APP_MAIN_ID) == 1
