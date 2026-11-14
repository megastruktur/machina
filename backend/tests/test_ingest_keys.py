"""Focused tests for the machine ingest-principal credential store.

Cards/contracts: notification-inbox task ``nbi-keystore`` against auth ADR
addendum **§13** (auth-and-origin.md) and notification-inbox.md §2/§6.

Boundary discipline: every storage claim runs against a real disposable
Postgres through the real linear Alembic chain (fixture ``migrated_url``),
never against mocks — same convention as ``test_sessions.py``.

Secret hygiene: the plaintext ingest keys minted below are runtime values,
never real secrets; the suite asserts they appear only as mint/validate
return values and never in stored rows, error texts, or list payloads.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from mai.auth.ingest_keys import (
    DEFAULT_PAGE_LIMIT,
    INGEST_RATE_WINDOW_SPAN,
    LABEL_MAX_CHARS,
    MAX_PAGE_LIMIT,
    IngestKey,
    IngestKeyError,
    IngestKeyErrorCode,
    IngestKeyStore,
    IssuedIngestKey,
)
from mai.auth.sessions import hash_token, new_token
from mai.contracts.observations import INGEST_RATE_WINDOW
from mai.database import create_app_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(ALEMBIC_INI))


# ---------------------------------------------------------------------------
# Live fixture: real disposable Postgres + real linear Alembic chain
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero to the chain tip."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the ingest-key-store claims need a "
            "real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
            conn.commit()
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
    await engine.dispose()


@pytest.fixture
def store(sessions) -> IngestKeyStore:
    return IngestKeyStore(sessions)


@pytest.fixture
def clean_keys(migrated_url: str):
    """Empty key state per test (real DB; ingest_keys has no inbound FK)."""
    with psycopg.connect(migrated_url) as conn:
        conn.execute("TRUNCATE ingest_keys")
        conn.commit()
    yield


async def _scalar(sessions, sql: str, **params: object) -> object:
    async with sessions() as session:
        return (await session.execute(text(sql), params)).scalar()


async def _row(sessions, sql: str, **params: object) -> object:
    async with sessions() as session:
        return (await session.execute(text(sql), params)).first()


async def _backdate(sessions, key_id: uuid.UUID, **columns: object) -> None:
    assignments = ", ".join(f"{column} = :{column}" for column in columns)
    async with sessions() as session, session.begin():
        await session.execute(
            text(f"UPDATE ingest_keys SET {assignments} WHERE key_id = :kid"),
            {"kid": key_id, **columns},
        )


async def _key_count(sessions) -> int:
    return int(await _scalar(sessions, "SELECT count(*) FROM ingest_keys"))


def _reject(excinfo: pytest.ExceptionInfo, code: IngestKeyErrorCode) -> IngestKeyError:
    error = excinfo.value
    assert isinstance(error, IngestKeyError) and error.code is code
    return error


def _assert_material_free(
    excinfo: pytest.ExceptionInfo, key: str, key_hash: str
) -> None:
    """Value-free rejection: no plaintext, no hash, in text or repr."""
    error = excinfo.value
    assert key not in str(error)
    assert key not in repr(error)
    assert key not in error.detail
    assert key_hash not in str(error)
    assert key_hash not in repr(error)


# ---------------------------------------------------------------------------
# Token primitives are imported, never reimplemented (shared contract)
# ---------------------------------------------------------------------------


def test_store_reuses_session_token_primitives() -> None:
    from mai.auth import ingest_keys as ingest_keys_module
    from mai.auth import sessions as sessions_module

    assert ingest_keys_module.new_token is sessions_module.new_token
    assert ingest_keys_module.hash_token is sessions_module.hash_token
    assert INGEST_RATE_WINDOW == 60
    assert INGEST_RATE_WINDOW_SPAN == timedelta(seconds=60)


@pytest.mark.anyio
async def test_ingest_keys_schema_matches_frozen_shape(sessions) -> None:
    """0017 columns/types on ``ingest_keys``; key_id PK, key_hash UNIQUE."""
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT column_name, data_type, character_maximum_length,
                           is_nullable
                      FROM information_schema.columns
                     WHERE table_schema = 'public'
                       AND table_name = 'ingest_keys'
                    """
                )
            )
        ).all()
        pk = (
            await session.execute(
                text(
                    """
                    SELECT a.attname
                      FROM pg_index i
                      JOIN pg_class c ON c.oid = i.indrelid
                      JOIN pg_attribute a ON a.attrelid = c.oid
                                         AND a.attnum = ANY(i.indkey)
                     WHERE c.relname = 'ingest_keys' AND i.indisprimary
                    """
                )
            )
        ).scalars().all()
        unique_hashes = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM pg_index i
                    JOIN pg_class c ON c.oid = i.indexrelid
                    WHERE c.relname = 'uq_ingest_keys_key_hash' AND i.indisunique
                    """
                )
            )
        ).scalar_one()
    columns = {name: (dtype, length, nullable) for name, dtype, length, nullable in rows}
    assert set(columns) == {
        "key_id",
        "key_hash",
        "label",
        "server_id",
        "created_at",
        "last_used_at",
        "expires_at",
        "revoked_at",
        "rate_window_start",
        "rate_window_count",
    }
    assert columns["key_hash"] == ("character", 64, "NO")
    assert columns["label"] == ("text", None, "NO")
    assert columns["server_id"] == ("uuid", None, "YES")
    assert columns["rate_window_count"] == ("integer", None, "NO")
    for column in (
        "created_at",
        "last_used_at",
        "expires_at",
        "revoked_at",
        "rate_window_start",
    ):
        assert columns[column][0] == "timestamp with time zone"
    assert columns["created_at"][2] == "NO"
    assert pk == ["key_id"]
    assert unique_hashes == 1


# ---------------------------------------------------------------------------
# Mint: plaintext returned once, only the hash persisted (acceptance 1)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mint_returns_plaintext_once_and_stores_only_hash(
    store: IngestKeyStore, clean_keys, sessions
) -> None:
    issued = await store.mint("ci-bot")
    assert isinstance(issued, IssuedIngestKey)
    assert len(issued.key) == 43  # 256-bit url-safe token, sessions pattern
    record = issued.record
    assert isinstance(record, IngestKey)
    assert record.label == "ci-bot"
    assert record.server_id is None  # global-only scope by default
    assert record.revoked_at is None
    assert record.last_used_at is None
    assert record.rate_window_start is None
    assert record.rate_window_count == 0

    row = await _row(
        sessions,
        "SELECT key_hash, label, server_id, created_at, expires_at, "
        "revoked_at, rate_window_start, rate_window_count "
        "FROM ingest_keys WHERE key_id = :kid",
        kid=record.key_id,
    )
    key_hash = hash_token(issued.key)
    assert re.fullmatch(r"[0-9a-f]{64}", key_hash)
    assert row[0] == key_hash  # only the SHA-256 hex is at rest
    assert issued.key not in str(row)  # plaintext appears nowhere at rest

    second = await store.mint("ci-bot-2")
    assert second.key != issued.key
    assert hash_token(second.key) != key_hash

    validated = await store.validate(issued.key)
    assert validated.key_id == record.key_id
    assert validated.last_used_at is not None
    assert validated.rate_window_count == 1
    assert validated.rate_window_start is not None


@pytest.mark.anyio
async def test_mint_trims_label_and_pins_scope(
    store: IngestKeyStore, clean_keys
) -> None:
    padded = await store.mint("  padded label  ")
    assert padded.record.label == "padded label"

    server_id = uuid.uuid4()
    scoped = await store.mint("scoped", server_id=server_id)
    assert scoped.record.server_id == server_id
    # A string UUID is accepted and normalized (route-layer convenience).
    as_str = await store.mint("scoped-str", server_id=str(server_id))
    assert as_str.record.server_id == server_id

    expiry = datetime.now(timezone.utc) + timedelta(days=7)
    dated = await store.mint("dated", expires_at=expiry)
    assert dated.record.expires_at is not None
    assert abs(dated.record.expires_at - expiry) < timedelta(seconds=30)


@pytest.mark.anyio
async def test_mint_rejects_bad_inputs_before_sql(
    store: IngestKeyStore, clean_keys, sessions
) -> None:
    bad_labels: list[object] = ["", "   ", "x" * (LABEL_MAX_CHARS + 1), None, 7]
    for bad in bad_labels:
        with pytest.raises(IngestKeyError) as excinfo:
            await store.mint(bad)  # type: ignore[arg-type]
        _reject(excinfo, IngestKeyErrorCode.INVALID_LABEL)
        # The rejected label content is never echoed (value-free).
        if isinstance(bad, str) and bad:
            assert bad not in excinfo.value.detail

    with pytest.raises(IngestKeyError) as excinfo:
        await store.mint("ok", server_id=123)  # type: ignore[arg-type]
    _reject(excinfo, IngestKeyErrorCode.INVALID_SERVER_SCOPE)
    with pytest.raises(IngestKeyError) as excinfo:
        await store.mint("ok", server_id="not-a-uuid")
    _reject(excinfo, IngestKeyErrorCode.INVALID_SERVER_SCOPE)

    naive = datetime.now() + timedelta(days=1)
    with pytest.raises(IngestKeyError) as excinfo:
        await store.mint("ok", expires_at=naive)
    _reject(excinfo, IngestKeyErrorCode.INVALID_EXPIRY)

    assert await _key_count(sessions) == 0  # no SQL effect from any failure


# ---------------------------------------------------------------------------
# Validate: scope persistence + fail-closed outcomes (acceptance 2)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_validate_persists_scope_fields_exactly(
    store: IngestKeyStore, clean_keys, sessions
) -> None:
    server_id = uuid.uuid4()
    scoped = await store.mint("db-1 monitor", server_id=server_id)
    validated = await store.validate(scoped.key)
    assert validated.server_id == server_id
    assert validated.label == "db-1 monitor"

    persisted = await _row(
        sessions,
        "SELECT server_id, label FROM ingest_keys WHERE key_id = :kid",
        kid=scoped.record.key_id,
    )
    assert persisted[0] == server_id
    assert persisted[1] == "db-1 monitor"

    global_key = await store.mint("global")
    assert global_key.record.server_id is None
    assert (await store.validate(global_key.key)).server_id is None


@pytest.mark.anyio
async def test_validate_rejects_unknown_revoked_expired_value_free(
    store: IngestKeyStore, clean_keys, sessions
) -> None:
    victim = await store.mint("victim")
    key, key_hash = victim.key, hash_token(victim.key)

    # Unknown credential (256-bit token never minted): same shape of failure.
    stranger = new_token()
    with pytest.raises(IngestKeyError) as excinfo:
        await store.validate(stranger)
    _reject(excinfo, IngestKeyErrorCode.KEY_UNKNOWN)
    assert stranger not in str(excinfo.value)

    # Non-string / empty presented material is rejected before hashing.
    for bad in (None, 42, ""):
        with pytest.raises(IngestKeyError) as excinfo:
            await store.validate(bad)  # type: ignore[arg-type]
        _reject(excinfo, IngestKeyErrorCode.INVALID_KEY)

    # Revocation is one conditional UPDATE, effective on the next validate.
    assert await store.revoke(victim.record.key_id) is True
    assert await store.revoke(victim.record.key_id) is False  # idempotent
    with pytest.raises(IngestKeyError) as excinfo:
        await store.validate(key)
    _reject(excinfo, IngestKeyErrorCode.KEY_REVOKED)
    _assert_material_free(excinfo, key, key_hash)

    # Expiry: backdated expires_at fails closed as a distinct internal code.
    dated = await store.mint("dated")
    await _backdate(
        sessions,
        dated.record.key_id,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with pytest.raises(IngestKeyError) as excinfo:
        await store.validate(dated.key)
    _reject(excinfo, IngestKeyErrorCode.KEY_EXPIRED)
    _assert_material_free(excinfo, dated.key, hash_token(dated.key))


@pytest.mark.anyio
async def test_revoke_ignores_unknown_key_id(store: IngestKeyStore, clean_keys) -> None:
    assert await store.revoke(uuid.uuid4()) is False


# ---------------------------------------------------------------------------
# Rate window: 60/min durable fixed window (acceptance 3)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rate_window_budget_61st_refused_rollover_resets(
    store: IngestKeyStore, clean_keys, sessions
) -> None:
    issued = await store.mint("windowed")
    for expected in range(1, INGEST_RATE_WINDOW + 1):
        validated = await store.validate(issued.key)
        assert validated.rate_window_count == expected  # same open window
    assert int(
        await _scalar(sessions, "SELECT rate_window_count FROM ingest_keys")
    ) == INGEST_RATE_WINDOW

    with pytest.raises(IngestKeyError) as excinfo:
        await store.validate(issued.key)  # 61st inside the same 60 s span
    _reject(excinfo, IngestKeyErrorCode.RATE_WINDOW_EXHAUSTED)
    _assert_material_free(excinfo, issued.key, hash_token(issued.key))
    # The refusing request is itself counted (notification-inbox.md §7).
    assert int(
        await _scalar(sessions, "SELECT rate_window_count FROM ingest_keys")
    ) == INGEST_RATE_WINDOW + 1

    # After the span rolls over, counting restarts at 1.
    await _backdate(
        sessions,
        issued.record.key_id,
        rate_window_start=(
            datetime.now(timezone.utc) - INGEST_RATE_WINDOW_SPAN - timedelta(seconds=1)
        ),
    )
    rolled = await store.validate(issued.key)
    assert rolled.rate_window_count == 1
    assert (
        datetime.now(timezone.utc) - rolled.rate_window_start
    ) < timedelta(seconds=30)


@pytest.mark.anyio
async def test_rate_window_survives_engine_restart(
    migrated_url: str, clean_keys
) -> None:
    """Scenario 2: window state is DB-backed; new engines keep counting."""
    engine_a, factory_a = create_app_engine(migrated_url)
    store_a = IngestKeyStore(factory_a)
    issued = await store_a.mint("restart-window")
    await store_a.validate(issued.key)  # count 1, window opens on engine A
    await engine_a.dispose()

    engine_b, factory_b = create_app_engine(migrated_url)
    store_b = IngestKeyStore(factory_b)
    for _ in range(INGEST_RATE_WINDOW - 1):
        await store_b.validate(issued.key)  # counts 2..60: SAME window
    await engine_b.dispose()

    # A brand-new engine sees the persisted count: 61st in-window refusal.
    engine_c, factory_c = create_app_engine(migrated_url)
    store_c = IngestKeyStore(factory_c)
    with pytest.raises(IngestKeyError) as excinfo:
        await store_c.validate(issued.key)
    _reject(excinfo, IngestKeyErrorCode.RATE_WINDOW_EXHAUSTED)
    assert int(
        await _scalar(factory_c, "SELECT rate_window_count FROM ingest_keys")
    ) == INGEST_RATE_WINDOW + 1

    # Rollover resets counting even on the restarted engine.
    await _backdate(
        factory_c,
        issued.record.key_id,
        rate_window_start=(
            datetime.now(timezone.utc) - INGEST_RATE_WINDOW_SPAN - timedelta(seconds=1)
        ),
    )
    rolled = await store_c.validate(issued.key)
    assert rolled.rate_window_count == 1
    await engine_c.dispose()


# ---------------------------------------------------------------------------
# §13 rule-3 counting entries (over-budget path support)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_consume_entries_count_requests_value_free(
    store: IngestKeyStore, clean_keys, sessions
) -> None:
    issued = await store.mint("entries")
    key_hash = hash_token(issued.key)

    first = await store.consume_rate_window(issued.key)
    assert first.rate_window_count == 1
    second = await store.validate_and_consume(issued.key)
    assert second.rate_window_count == 2
    third = await store.validate(issued.key)
    assert third.rate_window_count == 3

    # Unknown credentials fail value-free from every entry point.
    stranger = new_token()
    for entry in (
        store.consume_rate_window,
        store.validate_and_consume,
        store.validate,
    ):
        with pytest.raises(IngestKeyError) as excinfo:
            await entry(stranger)
        _reject(excinfo, IngestKeyErrorCode.KEY_UNKNOWN)
        assert stranger not in str(excinfo.value)

    # A revoked key is refused by the counting entries too.
    await store.revoke(issued.record.key_id)
    with pytest.raises(IngestKeyError) as excinfo:
        await store.consume_rate_window(issued.key)
    _reject(excinfo, IngestKeyErrorCode.KEY_REVOKED)
    _assert_material_free(excinfo, issued.key, key_hash)
    assert int(
        await _scalar(sessions, "SELECT rate_window_count FROM ingest_keys")
    ) == 3  # the refused request changed nothing


# ---------------------------------------------------------------------------
# list: bounded, deterministic, secret-free (acceptance 4)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_page_is_bounded_deterministic_and_secret_free(
    store: IngestKeyStore, clean_keys, sessions
) -> None:
    older = await store.mint("older")
    tied = await store.mint("tied")
    newest = await store.mint("newest")
    stamp = datetime.now(timezone.utc) - timedelta(minutes=10)
    await _backdate(sessions, older.record.key_id, created_at=stamp)
    await _backdate(sessions, tied.record.key_id, created_at=stamp)
    await _backdate(
        sessions, newest.record.key_id, created_at=stamp + timedelta(minutes=5)
    )

    material = [older.key, tied.key, newest.key]
    hashes = [hash_token(key) for key in material]

    default_page = await store.list_keys()
    assert (default_page.limit, default_page.offset, default_page.total) == (
        DEFAULT_PAGE_LIMIT,
        0,
        3,
    )
    assert default_page.has_more is False
    assert [item.key_id for item in default_page.items] == [
        newest.record.key_id,
        max((older.record.key_id, tied.record.key_id)),  # key_id DESC tiebreak
        min((older.record.key_id, tied.record.key_id)),
    ]

    first = await store.list_keys(limit=2, offset=0)
    assert first.limit == 2 and first.offset == 0
    assert len(first.items) == 2
    assert first.total == 3 and first.has_more is True
    second = await store.list_keys(limit=2, offset=2)
    assert len(second.items) == 1
    assert second.total == 3 and second.has_more is False
    assert {item.key_id for item in first.items}.isdisjoint(
        {item.key_id for item in second.items}
    )

    # Never key material: plaintext and hash appear nowhere in page shapes.
    for page in (default_page, first, second):
        rendered = str(page) + repr(page)
        for key, key_hash in zip(material, hashes, strict=True):
            assert key not in rendered
            assert key_hash not in rendered
        for item in page.items:
            assert not hasattr(item, "key_hash")

    # Bounded page parameters fail closed.
    for bad_limit in (0, -1, MAX_PAGE_LIMIT + 1):
        with pytest.raises(ValueError):
            await store.list_keys(limit=bad_limit)
    with pytest.raises(ValueError):
        await store.list_keys(offset=-1)


# ---------------------------------------------------------------------------
# Concurrent mint + hash uniqueness (acceptance 5)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_concurrent_mints_distinct_and_hash_unique_enforced(
    store: IngestKeyStore, clean_keys, sessions
) -> None:
    issued = await asyncio.gather(
        *[store.mint(f"concurrent-{index}") for index in range(8)]
    )
    keys = [item.key for item in issued]
    assert len(set(keys)) == 8  # distinct 256-bit plaintexts
    assert len({hash_token(key) for key in keys}) == 8
    assert await _key_count(sessions) == 8

    # UNIQUE(key_hash) makes a hash collision impossible at rest.
    duplicate = hash_token(keys[0])
    with pytest.raises(IntegrityError):
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO ingest_keys "
                    "(key_id, key_hash, label) "
                    "VALUES (:kid, :h, 'collision-probe')"
                ),
                {"kid": uuid.uuid4(), "h": duplicate},
            )


# ---------------------------------------------------------------------------
# Secret hygiene sweep (acceptance 1/2, scenario 3)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_plaintext_never_lands_in_rows_or_error_payloads(
    store: IngestKeyStore, clean_keys, sessions
) -> None:
    first = await store.mint("hygiene-1")
    second = await store.mint("hygiene-2")
    material = [first.key, second.key]
    hashes = [hash_token(key) for key in material]

    async with sessions() as session:
        rows = (await session.execute(text("SELECT * FROM ingest_keys"))).all()
    rendered = str(rows)
    for key, key_hash in zip(material, hashes, strict=True):
        assert key not in rendered
        assert key_hash in rendered  # the hash IS the only stored form
        assert key != key_hash

    # Exhausted-window errors stay value-free under sustained pressure.
    await _backdate(
        sessions,
        first.record.key_id,
        rate_window_count=INGEST_RATE_WINDOW + 1,
        rate_window_start=datetime.now(timezone.utc),
    )
    with pytest.raises(IngestKeyError) as excinfo:
        await store.validate(first.key)
    _reject(excinfo, IngestKeyErrorCode.RATE_WINDOW_EXHAUSTED)
    for key, key_hash in zip(material, hashes, strict=True):
        _assert_material_free(excinfo, key, key_hash)


@pytest.mark.anyio
async def test_last_used_stamp_updates_on_validate(
    store: IngestKeyStore, clean_keys, sessions
) -> None:
    issued = await store.mint("audited")
    assert issued.record.last_used_at is None
    await store.validate(issued.key)
    stamped = await _scalar(
        sessions, "SELECT last_used_at FROM ingest_keys WHERE key_id = :kid",
        kid=issued.record.key_id,
    )
    assert stamped is not None
    assert datetime.now(timezone.utc) - stamped < timedelta(seconds=30)
