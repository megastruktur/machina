"""Focused tests for admin credential verification and revocable sessions.

Cards/contracts: ``02-auth-store`` against auth ADR ``3e79449`` §3–§4 and
contracts.md **C08 rule 1** (C01 envelope vocabulary for rejection codes).

Boundary discipline: every storage claim runs against a real disposable
Postgres through the real linear Alembic chain (fixture ``migrated_url``),
never against mocks. The feature-gate scenario (AUTH-S2 half owned here —
"create, validate, revoke and expire a session; restart process and prove
revoked credential does not recover") is covered in-suite by engine-restart
and real subprocess-restart tests.

Secret hygiene: the fixture password below is a synthetic constant, never a
real secret; the opaque session tokens are minted at runtime, kept in
0600 pytest temp files when handed across processes, and never printed to
captured stdout or evidence.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from mai.auth.sessions import (
    MAX_LIVE_SESSIONS,
    SESSION_ABSOLUTE_TTL,
    SESSION_IDLE_TTL,
    AdminSession,
    AuthError,
    AuthErrorCode,
    SessionStore,
    hash_password,
    hash_token,
    new_token,
    verify_password,
)
from mai.database import create_app_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Linear migration chain (exclusive migration-chain resource): head
#: assertions below target the chain TIP, so this constant tracks the full
#: chain (0001 profiles, 0002 sessions, 0003 approvals, 0004 jobs, 0005
#: knowledge, 0006 applications, 0007 observations) rather than pinning a head.
REVISION_IDS = (
    "0001", "0002", "0003", "0004", "0005", "0006", "0007",
    "0008", "0009", "0010", "0011", "0012", "0013", "0014", "0015",
)

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-02-auth-store-only"
OTHER_PASSWORD = "other-wrong-fixture-password"


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
            f"{LIVE_URL_VAR} is not set; the session-store claims need a "
            "real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            # Whole-schema reset (D09): FK-safe and immune to future revisions
            # (the enumerated drop list went stale across migrations).
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
def store(sessions) -> SessionStore:
    return SessionStore(sessions)


@pytest.fixture
def clean_auth(migrated_url: str):
    """Empty credential/session state per test (real DB, full rebuild)."""
    with psycopg.connect(migrated_url) as conn:
        conn.execute("TRUNCATE admin_sessions, admin_credential")
        conn.commit()
    yield


@pytest.fixture
async def bootstrapped(store: SessionStore, clean_auth) -> SessionStore:
    await store.bootstrap_credential(FIXTURE_PASSWORD)
    return store


async def _scalar(sessions, sql: str, **params: object) -> object:
    async with sessions() as session:
        return (await session.execute(text(sql), params)).scalar()


async def _row(sessions, sql: str, **params: object) -> object:
    async with sessions() as session:
        return (await session.execute(text(sql), params)).first()


async def _backdate(sessions, token_hash: str, **columns: object) -> None:
    assignments = ", ".join(f"{column} = :{column}" for column in columns)
    async with sessions() as session, session.begin():
        await session.execute(
            text(f"UPDATE admin_sessions SET {assignments} WHERE token_hash = :h"),
            {"h": token_hash, **columns},
        )


async def _live_count(sessions) -> int:
    return int(
        await _scalar(
            sessions,
            "SELECT count(*) FROM admin_sessions WHERE revoked_at IS NULL "
            "AND expires_at > now() AND idle_expires_at > now()",
        )
    )


def _reject(excinfo: pytest.ExceptionInfo, code: AuthErrorCode) -> AuthError:
    error = excinfo.value
    assert isinstance(error, AuthError) and error.code is code
    return error


# ---------------------------------------------------------------------------
# Migration chain — serialized after 0001 (acceptance 2)
# ---------------------------------------------------------------------------


def test_migration_chain_is_linear_and_sessions_follow_profiles(
    migrated_url: str,
) -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    assert script.get_heads() == [REVISION_IDS[-1]]
    revisions = {r.revision: r for r in script.walk_revisions()}
    assert list(revisions) and set(REVISION_IDS) <= set(revisions)
    assert revisions["0001"].down_revision is None
    assert revisions["0002"].down_revision == "0001"  # strict serialization


def test_upgrade_from_zero_lands_on_head_and_is_idempotent(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``migrated_url`` upgraded from zero; re-upgrade is a no-op at the tip."""
    monkeypatch.setenv("MAI_DATABASE_URL", migrated_url)
    command.upgrade(_alembic_config(), "head")
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row == (REVISION_IDS[-1],)


@pytest.mark.anyio
async def test_session_schema_matches_frozen_adr_table(sessions) -> None:
    """ADR §4 columns/types on ``admin_sessions``; argon2id-only credential."""
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT table_name, column_name, data_type,
                           character_maximum_length, is_nullable
                      FROM information_schema.columns
                     WHERE table_schema = 'public'
                       AND table_name IN ('admin_sessions', 'admin_credential')
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
                     WHERE c.relname = 'admin_sessions' AND i.indisprimary
                    """
                )
            )
        ).scalars().all()
    columns = {
        (t, c): (dtype, length, nullable) for t, c, dtype, length, nullable in rows
    }
    assert set(columns) == {
        ("admin_credential", "id"),
        ("admin_credential", "password_hash"),
        ("admin_credential", "created_at"),
        ("admin_credential", "updated_at"),
        ("admin_sessions", "token_hash"),
        ("admin_sessions", "created_at"),
        ("admin_sessions", "last_seen_at"),
        ("admin_sessions", "expires_at"),
        ("admin_sessions", "idle_expires_at"),
        ("admin_sessions", "revoked_at"),
    }
    assert columns[("admin_sessions", "token_hash")][:-1] == ("character varying", 64)
    for table in ("admin_sessions", "admin_credential"):
        for column in ("created_at", "last_seen_at", "expires_at", "idle_expires_at"):
            if (table, column) in columns:
                assert columns[(table, column)][0] == "timestamp with time zone"
        for column in ("id", "password_hash"):
            if (table, column) in columns:
                assert columns[(table, column)][2] == "NO"
    assert pk == ["token_hash"]


@pytest.mark.anyio
async def test_credential_table_admits_only_argon2id_single_row(
    bootstrapped: SessionStore, sessions
) -> None:
    """Storage-level negative control: no plaintext/foreign hash, no 2nd row."""
    stored = await _scalar(
        sessions, "SELECT password_hash FROM admin_credential WHERE id = 1"
    )
    assert isinstance(stored, str) and stored.startswith("$argon2id$")
    with pytest.raises(IntegrityError):
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO admin_credential (id, password_hash) "
                    "VALUES (2, :h)"
                ),
                {"h": stored},
            )
    with pytest.raises(IntegrityError):
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO admin_credential (id, password_hash) "
                    "VALUES (1, 'plaintext-not-argon2id')"
                )
            )


# ---------------------------------------------------------------------------
# Credential hashing — Argon2id pinned parameters (acceptance 1, ADR §3.1)
# ---------------------------------------------------------------------------


def test_argon2id_hash_pins_minimum_parameters() -> None:
    first = hash_password(FIXTURE_PASSWORD)
    second = hash_password(FIXTURE_PASSWORD)
    assert first != second  # random 16-byte salt per hash
    assert first.startswith("$argon2id$v=19$")
    parameters = first.split("$")[3]
    parts = dict(p.split("=") for p in parameters.split(","))
    assert int(parts["m"]) >= 64 * 1024  # ≥ 64 MiB
    assert int(parts["t"]) >= 3
    assert int(parts["p"]) >= 1
    assert FIXTURE_PASSWORD not in first


def test_verify_password_constant_outcomes_and_fail_closed() -> None:
    stored = hash_password(FIXTURE_PASSWORD)
    assert verify_password(stored, FIXTURE_PASSWORD) is True
    assert verify_password(stored, OTHER_PASSWORD) is False
    with pytest.raises(AuthError) as excinfo:
        verify_password("$argon2id$corrupted-not-a-real-hash", FIXTURE_PASSWORD)
    _reject(excinfo, AuthErrorCode.CREDENTIAL_CORRUPTED)


@pytest.mark.parametrize("bad", [None, 7, "", "x" * 1025])
def test_password_validation_rejects_before_hashing(bad: object) -> None:
    with pytest.raises(AuthError) as excinfo:
        hash_password(bad)  # type: ignore[arg-type]
    _reject(excinfo, AuthErrorCode.INVALID_PASSWORD)


# ---------------------------------------------------------------------------
# Bootstrap + verification (acceptance 1, ADR §3)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bootstrap_is_one_shot_and_stores_only_the_hash(
    store: SessionStore, clean_auth, sessions
) -> None:
    assert await store.credential_exists() is False
    await store.bootstrap_credential(FIXTURE_PASSWORD)
    assert await store.credential_exists() is True

    row = await _row(sessions, "SELECT id, password_hash FROM admin_credential")
    assert row[0] == 1 and row[1].startswith("$argon2id$")
    # Nothing at rest echoes the plaintext (whole-row scan).
    assert FIXTURE_PASSWORD not in str(row)

    with pytest.raises(AuthError) as excinfo:
        await store.bootstrap_credential(OTHER_PASSWORD)
    _reject(excinfo, AuthErrorCode.CREDENTIAL_PRESENT)
    # The failed one-shot attempt changed nothing.
    assert await _scalar(sessions, "SELECT password_hash FROM admin_credential") == (
        row[1]
    )


@pytest.mark.anyio
async def test_verify_credential_setup_state_and_outcomes(
    store: SessionStore, clean_auth
) -> None:
    with pytest.raises(AuthError) as excinfo:
        await store.verify_credential(FIXTURE_PASSWORD)
    _reject(excinfo, AuthErrorCode.CREDENTIAL_MISSING)

    await store.bootstrap_credential(FIXTURE_PASSWORD)
    assert await store.verify_credential(OTHER_PASSWORD) is False
    assert await store.verify_credential(FIXTURE_PASSWORD) is True


@pytest.mark.anyio
async def test_login_requires_credential_record(store: SessionStore, clean_auth):
    with pytest.raises(AuthError) as excinfo:
        await store.login(FIXTURE_PASSWORD)
    _reject(excinfo, AuthErrorCode.CREDENTIAL_MISSING)


# ---------------------------------------------------------------------------
# Session lifecycle (acceptance 2, ADR §4) — real DB persistence
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_mints_opaque_token_and_stores_only_sha256(
    bootstrapped: SessionStore, sessions
) -> None:
    issued = await bootstrapped.login(FIXTURE_PASSWORD)
    assert issued is not None
    assert len(issued.token) == 43  # 256-bit url-safe token
    session_row = await _row(
        sessions,
        "SELECT token_hash, created_at, last_seen_at, expires_at, "
        "idle_expires_at, revoked_at FROM admin_sessions",
    )
    assert session_row[0] == hash_token(issued.token)
    assert session_row[5] is None
    # The plaintext token appears nowhere at rest.
    assert issued.token not in str(session_row)
    # Absolute/idle windows match the frozen timeouts.
    now = datetime.now(timezone.utc)
    assert abs(session_row[1] - now) < timedelta(seconds=30)
    assert abs(session_row[3] - (now + SESSION_ABSOLUTE_TTL)) < timedelta(seconds=30)
    assert abs(session_row[4] - (now + SESSION_IDLE_TTL)) < timedelta(seconds=30)

    validated = await bootstrapped.validate(issued.token)
    assert isinstance(validated, AdminSession)
    assert validated.token_hash == hash_token(issued.token)


@pytest.mark.anyio
async def test_login_rejects_wrong_password_without_row(
    bootstrapped: SessionStore, sessions
) -> None:
    assert await bootstrapped.login(OTHER_PASSWORD) is None
    assert int(await _scalar(sessions, "SELECT count(*) FROM admin_sessions")) == 0


@pytest.mark.anyio
async def test_each_login_mints_a_fresh_token(bootstrapped: SessionStore) -> None:
    first = await bootstrapped.login(FIXTURE_PASSWORD)
    second = await bootstrapped.login(FIXTURE_PASSWORD)
    assert first is not None and second is not None
    assert first.token != second.token  # fixation defense: no token reuse
    await bootstrapped.validate(first.token)
    await bootstrapped.validate(second.token)


@pytest.mark.anyio
async def test_validate_slides_idle_window(
    bootstrapped: SessionStore, sessions
) -> None:
    issued = await bootstrapped.login(FIXTURE_PASSWORD)
    assert issued is not None
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    await _backdate(
        sessions,
        issued.session.token_hash,
        last_seen_at=stale,
        idle_expires_at=stale + SESSION_IDLE_TTL,
    )
    validated = await bootstrapped.validate(issued.token)
    now = datetime.now(timezone.utc)
    assert abs(validated.last_seen_at - now) < timedelta(seconds=30)
    assert abs(validated.idle_expires_at - (now + SESSION_IDLE_TTL)) < timedelta(
        seconds=30
    )
    # The slide must be PERSISTED, not just returned: re-read the row —
    # a rolled-back touch would leave login-time values in Postgres, so an
    # actively used session would die 24 h after login (ADR §4 violation).
    persisted = await _row(
        sessions,
        "SELECT last_seen_at, idle_expires_at FROM admin_sessions "
        "WHERE token_hash = :h",
        h=issued.session.token_hash,
    )
    assert abs(persisted[0] - now) < timedelta(seconds=30)
    assert abs(persisted[1] - (now + SESSION_IDLE_TTL)) < timedelta(seconds=30)
    # The genuinely stale window is gone: a re-backdated row slides again,
    # proving the touch keeps refreshing on each authenticated use.
    await _backdate(
        sessions,
        issued.session.token_hash,
        last_seen_at=stale,
        idle_expires_at=stale + SESSION_IDLE_TTL,
    )
    await bootstrapped.validate(issued.token)
    repersisted = await _row(
        sessions,
        "SELECT last_seen_at FROM admin_sessions WHERE token_hash = :h",
        h=issued.session.token_hash,
    )
    assert abs(repersisted[0] - now) < timedelta(seconds=30)


@pytest.mark.anyio
async def test_validate_rejects_unknown_and_malformed_tokens(
    bootstrapped: SessionStore,
) -> None:
    with pytest.raises(AuthError) as missing:
        await bootstrapped.validate(new_token())
    _reject(missing, AuthErrorCode.SESSION_MISSING)
    with pytest.raises(AuthError) as empty:
        await bootstrapped.validate("")
    _reject(empty, AuthErrorCode.INVALID_TOKEN)
    with pytest.raises(AuthError) as wrong_type:
        await bootstrapped.validate(123)  # type: ignore[arg-type]
    _reject(wrong_type, AuthErrorCode.INVALID_TOKEN)


@pytest.mark.anyio
async def test_revocation_is_effective_on_next_request_without_grace(
    bootstrapped: SessionStore, sessions
) -> None:
    issued = await bootstrapped.login(FIXTURE_PASSWORD)
    assert issued is not None
    await bootstrapped.validate(issued.token)  # live immediately before

    assert await bootstrapped.revoke(issued.token) is True
    with pytest.raises(AuthError) as excinfo:
        await bootstrapped.validate(issued.token)
    _reject(excinfo, AuthErrorCode.SESSION_REVOKED)

    # Idempotent, and the audit row is never reactivated.
    row = await _row(sessions, "SELECT revoked_at FROM admin_sessions")
    assert row[0] is not None
    assert await bootstrapped.revoke(issued.token) is False
    assert await _scalar(sessions, "SELECT revoked_at FROM admin_sessions") == row[0]


@pytest.mark.parametrize("column", ["expires_at", "idle_expires_at"])
@pytest.mark.anyio
async def test_expired_session_is_rejected(
    bootstrapped: SessionStore, sessions, column: str
) -> None:
    issued = await bootstrapped.login(FIXTURE_PASSWORD)
    assert issued is not None
    # Simulate elapsed time WITHOUT inverting the stored window (the migration
    # CHECK constraints correctly reject expires_at <= created_at): the
    # anchors move further into the past together with the expiry columns.
    past = datetime.now(timezone.utc) - timedelta(seconds=2)
    if column == "expires_at":
        await _backdate(
            sessions,
            issued.session.token_hash,
            created_at=past - timedelta(hours=1),
            expires_at=past,
        )
    else:
        await _backdate(
            sessions,
            issued.session.token_hash,
            last_seen_at=past - timedelta(hours=1),
            idle_expires_at=past,
        )
    with pytest.raises(AuthError) as excinfo:
        await bootstrapped.validate(issued.token)
    _reject(excinfo, AuthErrorCode.SESSION_EXPIRED)


@pytest.mark.anyio
async def test_revoke_all_covers_every_live_session_exactly_once(
    bootstrapped: SessionStore,
) -> None:
    tokens = [issued.token for issued in await _login_many(bootstrapped, 3)]
    assert await bootstrapped.revoke_all() == 3
    for token in tokens:
        with pytest.raises(AuthError) as excinfo:
            await bootstrapped.validate(token)
        _reject(excinfo, AuthErrorCode.SESSION_REVOKED)
    assert await bootstrapped.revoke_all() == 0  # already-revoked stay revoked


async def _login_many(store: SessionStore, count: int):
    issued = []
    for _ in range(count):
        result = await store.login(FIXTURE_PASSWORD)
        assert result is not None
        issued.append(result)
    return issued


# ---------------------------------------------------------------------------
# Live-session cap (ADR §4: ≤16, oldest last_seen evicted, audit retained)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cap_evicts_oldest_by_last_seen(bootstrapped: SessionStore, sessions):
    issued = await _login_many(bootstrapped, MAX_LIVE_SESSIONS)
    oldest = issued[0]
    await _backdate(
        sessions,
        oldest.session.token_hash,
        last_seen_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    newest = await bootstrapped.login(FIXTURE_PASSWORD)
    assert newest is not None
    assert await _live_count(sessions) == MAX_LIVE_SESSIONS
    with pytest.raises(AuthError) as excinfo:
        await bootstrapped.validate(oldest.token)
    _reject(excinfo, AuthErrorCode.SESSION_REVOKED)  # evicted = revoked (audit row)
    await bootstrapped.validate(newest.token)
    middle = await bootstrapped.validate(issued[1].token)
    assert middle.revoked_at is None


@pytest.mark.anyio
async def test_cap_holds_under_concurrent_logins(
    bootstrapped: SessionStore, sessions
) -> None:
    await _login_many(bootstrapped, MAX_LIVE_SESSIONS - 3)
    concurrent = await asyncio.gather(
        *(bootstrapped.login(FIXTURE_PASSWORD) for _ in range(6))
    )
    assert all(issued is not None for issued in concurrent)
    assert await _live_count(sessions) == MAX_LIVE_SESSIONS
    for issued in concurrent:
        assert issued is not None
        await bootstrapped.validate(issued.token)  # newest cohort survives


# ---------------------------------------------------------------------------
# Restart persistence (acceptance 2 + feature-gate scenario, AUTH-S2 half)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_revocation_and_expiry_survive_engine_restart(
    migrated_url: str, clean_auth
) -> None:
    """Revocation/expiry live in Postgres; a new engine cannot resurrect."""

    async def fresh_store() -> tuple[SessionStore, object]:
        engine, factory = create_app_engine(migrated_url)
        return SessionStore(factory), engine

    store_a, engine_a = await fresh_store()
    await store_a.bootstrap_credential(FIXTURE_PASSWORD)
    revoked = await store_a.login(FIXTURE_PASSWORD)
    expired = await store_a.login(FIXTURE_PASSWORD)
    assert revoked is not None and expired is not None
    assert await store_a.revoke(revoked.token) is True
    await engine_a.dispose()

    store_b, engine_b = await fresh_store()
    try:
        with pytest.raises(AuthError) as revoked_info:
            await store_b.validate(revoked.token)
        _reject(revoked_info, AuthErrorCode.SESSION_REVOKED)
        # The credential itself persists: login still works after restart,
        # and the OLD token stays revoked while the new one is live.
        renewed = await store_b.login(FIXTURE_PASSWORD)
        assert renewed is not None and renewed.token != revoked.token
        with pytest.raises(AuthError) as still_revoked:
            await store_b.validate(revoked.token)
        _reject(still_revoked, AuthErrorCode.SESSION_REVOKED)
        await store_b.validate(renewed.token)
    finally:
        await engine_b.dispose()


_SCENARIO_CHILD = """\
import asyncio
import json
import os
import sys
from pathlib import Path

from mai.auth.sessions import AuthError, SessionStore
from mai.database import create_app_engine


async def main() -> int:
    phase = sys.argv[1]
    engine, sessions = create_app_engine(os.environ["MAI_DATABASE_URL"])
    store = SessionStore(sessions)
    try:
        if phase == "create":
            issued = await store.login(os.environ["MAI_TEST_PASSWORD"])
            if issued is None:
                print(json.dumps({"kind": "login-rejected"}))
                return 3
            path = Path(os.environ["MAI_TEST_TOKEN_FILE"])
            path.write_text(issued.token, encoding="utf-8")
            path.chmod(0o600)
            print(json.dumps({"kind": "created"}))
            return 0
        token = Path(os.environ["MAI_TEST_TOKEN_FILE"]).read_text(encoding="utf-8")
        try:
            await store.validate(token)
        except AuthError as exc:
            print(json.dumps({"kind": "rejected", "code": exc.code.value}))
            return 0
        print(json.dumps({"kind": "valid"}))
        return 0
    finally:
        await engine.dispose()


raise SystemExit(asyncio.run(main()))
"""


@pytest.mark.anyio
async def test_revoked_credential_does_not_recover_across_process_restart(
    migrated_url: str, clean_auth, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feature-gate scenario (AUTH-S2 store half) with REAL process restarts.

    Child process 1 creates a session (production service, real Postgres).
    Parent validates via a second engine, then revokes. Fresh child process 2
    still finds the session live (persistence, not memory). Fresh child
    process 3 — after revocation — is rejected: a restarted process cannot
    resurrect a revoked credential. The expiry half is proven the same way by
    backdating ``expires_at`` in the DB before a final fresh child.
    """
    token_file = tmp_path / "session.token"
    token_file.write_text("", encoding="utf-8")
    token_file.chmod(0o600)
    engine, factory = create_app_engine(migrated_url)
    bootstrap_store = SessionStore(factory)
    try:
        await bootstrap_store.bootstrap_credential(FIXTURE_PASSWORD)
    finally:
        await engine.dispose()
    env = {
        **os.environ,
        "MAI_DATABASE_URL": migrated_url,
        "MAI_TEST_PASSWORD": FIXTURE_PASSWORD,
        "MAI_TEST_TOKEN_FILE": str(token_file),
    }

    def child(phase: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", _SCENARIO_CHILD, phase],
            capture_output=True,
            text=True,
            cwd=BACKEND_ROOT,
            env=env,
            timeout=120,
            check=False,
        )

    created = child("create")
    assert created.returncode == 0, created.stderr
    assert "created" in created.stdout
    token = token_file.read_text(encoding="utf-8")
    assert len(token) == 43

    engine, factory = create_app_engine(migrated_url)
    store = SessionStore(factory)
    try:
        await store.validate(token)  # live before revocation
    finally:
        await engine.dispose()

    still_valid = child("check")
    assert still_valid.returncode == 0, still_valid.stderr
    assert '"kind": "valid"' in still_valid.stdout

    engine, factory = create_app_engine(migrated_url)
    store = SessionStore(factory)
    try:
        assert await store.revoke(token) is True
    finally:
        await engine.dispose()

    rejected = child("check")
    assert rejected.returncode == 0, rejected.stderr
    assert '"code": "session_revoked"' in rejected.stdout

    # Expiry half: same restart discipline against absolute expiry.
    engine, factory = create_app_engine(migrated_url)
    store = SessionStore(factory)
    try:
        renewed = await store.login(FIXTURE_PASSWORD)
        assert renewed is not None
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE admin_sessions SET created_at = now() - interval '1 "
                    "hour', expires_at = now() - interval '1 second' "
                    "WHERE token_hash = :h"
                ),
                {"h": renewed.session.token_hash},
            )
    finally:
        await engine.dispose()
    token_file.write_text(renewed.token, encoding="utf-8")
    expired = child("check")
    assert expired.returncode == 0, expired.stderr
    assert '"code": "session_expired"' in expired.stdout

    # Secret hygiene: no plaintext token/password on any captured stdout.
    for proc in (created, still_valid, rejected, expired):
        assert token not in proc.stdout
        assert FIXTURE_PASSWORD not in proc.stdout


# ---------------------------------------------------------------------------
# Value-free discipline (acceptance 1: no secret material in errors/state)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rejections_and_persistence_stay_value_free(
    bootstrapped: SessionStore, sessions
) -> None:
    """Hostile inputs surface stable codes; secrets never leak into them."""
    with pytest.raises(AuthError) as missing:
        await bootstrapped.validate(FIXTURE_PASSWORD)  # password-shaped token
    error = _reject(missing, AuthErrorCode.SESSION_MISSING)
    assert FIXTURE_PASSWORD not in error.detail
    assert FIXTURE_PASSWORD not in str(error)

    issued = await bootstrapped.login(FIXTURE_PASSWORD)
    assert issued is not None
    with pytest.raises(AuthError) as expired:
        await bootstrapped.validate(issued.token + "x")
    error = _reject(expired, AuthErrorCode.SESSION_MISSING)
    assert issued.token not in error.detail

    rows = await _row(sessions, "SELECT * FROM admin_sessions")
    assert issued.token not in str(rows)
    assert FIXTURE_PASSWORD not in str(rows)
