"""Focused tests for the profile projection and reconciliation journal.

Card ``02-profile-projection``. Acceptance (BRIEF):

1. File and SQL revision ownership follows the frozen journal phases
   (``prepared -> file_committed -> reconciled``); the migration chain is
   linear.
2. The file-committed/SQL-unavailable scenario repairs the projection
   without discarding the canonical update or double-writing documents.

Boundary discipline (test policy, risk-critical slice): every
reconciliation claim runs against a real disposable PostgreSQL fixture
(``MAI_TEST_DATABASE_URL``) with the real Alembic chain applied — the W2
window is produced by a **real SIGKILL** of a child worker between the file
commit and the journal advance, and the W3 atomicity claim is proven by
injecting a failure between the two statements of the reconcile transaction
on the live server (the first statement really executed and must roll
back). Mock-free negative controls cover W1 re-execute/advance decisions,
file divergence, the interrupted canonical/metadata pair, the W4
resurrected-file tombstone conflict, the one-live-intent-per-server index
and the value-free error/CLI discipline. Documents are fixtures with
TEST-NET addresses and a credential *reference name* only.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.server import SCHEMA_VERSION, ProfileValidationError
from mai.database import DatabaseHealth, create_app_engine
from mai.profiles.files import FileStoreErrorCode, ProfileFileError, ProfileFileStore
from mai.profiles.projection import (
    PHASE_FILE_COMMITTED,
    PHASE_PREPARED,
    PHASE_RECONCILED,
    ProfileProjection,
    ProjectionErrorCode,
    ProjectionError,
    main,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Same frozen fixture shape as the schema/file-store sibling cards.
HOST_KEY = "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"

# Linear migration chain (exclusive migration-chain resource). 02-auth-store
# appended 0002_sessions and 0003_approvals; later cards appended 0004 jobs,
# 0005 knowledge, 0006 applications and 0007 observations. Head assertions
# below target the chain TIP, so this constant tracks the full chain rather
# than pinning a head.
REVISION_IDS = (
    "0001", "0002", "0003", "0004", "0005", "0006", "0007",
    "0008", "0009", "0010", "0011", "0012", "0013", "0014", "0015",
)


def _profile_text(server_id: uuid.UUID, name: str = "build-host") -> str:
    return f"""\
schema_version: {SCHEMA_VERSION}
server_id: "{server_id}"
name: "{name}"
prompt_revision: 3
environment: linux_ssh
ssh:
  host: "198.51.100.10"
  port: 22
  user: "deploy"
  host_key: "{HOST_KEY}"
  credential_ref: "MAI_SSH_KEY_BUILD_HOST"
privilege: none
capabilities:
  - shell.exec
  - files.read
"""


def _sha256(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Live fixture: real disposable Postgres + real linear Alembic chain
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(ALEMBIC_INI))


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through the chain tip."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the projection claims need a real "
            "disposable Postgres fixture"
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
def store(tmp_path: Path) -> ProfileFileStore:
    return ProfileFileStore(tmp_path / "data-root")


@pytest.fixture
def projection(sessions) -> ProfileProjection:
    return ProfileProjection(sessions)


@pytest.fixture
def server_id() -> uuid.UUID:
    return uuid.uuid4()


async def _raw_scalar(sessions, sql: str, **params: object) -> object:
    async with sessions() as session:
        return (await session.execute(text(sql), params)).scalar()


# ---------------------------------------------------------------------------
# Acceptance 1 — linear migration chain and revision ownership
# ---------------------------------------------------------------------------


def test_migration_chain_is_linear_with_single_head(migrated_url: str) -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    assert script.get_heads() == [REVISION_IDS[-1]]
    revisions = {r.revision: r for r in script.walk_revisions()}
    assert sorted(revisions) == sorted(REVISION_IDS)
    assert revisions[REVISION_IDS[0]].down_revision is None
    for previous, following in zip(REVISION_IDS, REVISION_IDS[1:]):
        assert revisions[following].down_revision == previous


def test_migration_reupgrade_is_a_noop_and_records_the_revision(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAI_DATABASE_URL", migrated_url)
    command.upgrade(_alembic_config(), "head")  # second run on the same DB
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row == (REVISION_IDS[-1],)


@pytest.mark.anyio
async def test_projection_table_stores_only_the_version_triple(
    sessions, migrated_url: str
) -> None:
    """C02 rule 4: identity/version metadata only — never document text."""
    async with sessions() as session:
        columns = (
            await session.execute(
                text(
                    """
                    SELECT table_name, column_name
                      FROM information_schema.columns
                     WHERE table_schema = 'public'
                       AND table_name IN
                           ('profile_projection',
                            'profile_projection_journal')
                    """
                )
            )
        ).all()
    projection_columns = {c for t, c in columns if t == "profile_projection"}
    journal_columns = {c for t, c in columns if t == "profile_projection_journal"}
    assert projection_columns == {
        "server_id",
        "revision",
        "schema_revision",
        "sha256",
        "first_reconciled_at",
        "reconciled_at",
    }
    assert "payload" in journal_columns  # prepared-phase only (W1 re-execution)


@pytest.mark.anyio
async def test_apply_upsert_projects_the_file_triple(
    projection, store, server_id, sessions
) -> None:
    document = _profile_text(server_id)
    result = await projection.apply_upsert(
        store, server_id, document, expected_revision=0
    )
    assert (result.outcome, result.revision) == ("applied", 1)

    stored = store.read(server_id)
    state = await projection.read_projection(server_id)
    assert state is not None
    assert (state.revision, state.schema_revision) == (
        stored.revision,
        SCHEMA_VERSION,
    )
    assert state.sha256 == stored.sha256 == _sha256(document)
    assert state.stale is False

    intent = await projection.get_intent(result.intent_id)
    assert intent is not None
    assert intent.phase == PHASE_RECONCILED
    assert intent.target_revision == 1
    assert intent.target_sha256 == _sha256(document)
    assert intent.schema_revision == SCHEMA_VERSION
    assert intent.payload is None  # payload dropped at file_committed


@pytest.mark.anyio
async def test_apply_upsert_bumps_projection_with_each_revision(
    projection, store, server_id
) -> None:
    await projection.apply_upsert(
        store, server_id, _profile_text(server_id), expected_revision=0
    )
    updated = _profile_text(server_id, name="renamed-host")
    result = await projection.apply_upsert(
        store,
        server_id,
        updated,
        expected_revision=1,
        base_sha256=_sha256(_profile_text(server_id)),
    )
    assert result.revision == 2
    stored = store.read(server_id)
    state = await projection.read_projection(server_id)
    assert state is not None
    assert (state.revision, state.sha256) == (2, stored.sha256)


@pytest.mark.anyio
async def test_retry_same_intent_yields_exactly_one_profile(
    projection, store, server_id, sessions
) -> None:
    """FILE-CRASH scenario clause: retry same intent -> one profile."""
    document = _profile_text(server_id)
    intent_id = uuid.uuid4()
    first = await projection.apply_upsert(
        store, server_id, document, expected_revision=0, intent_id=intent_id
    )
    retry = await projection.apply_upsert(
        store,
        server_id,
        document,
        expected_revision=0,
        intent_id=intent_id,
    )
    assert (first.outcome, retry.outcome) == ("applied", "already_reconciled")
    assert store.read(server_id).revision == 1  # no second file write
    assert (
        await _raw_scalar(
            sessions,
            "SELECT count(*) FROM profile_projection WHERE server_id = :sid",
            sid=server_id,
        )
        == 1
    )
    assert (
        await _raw_scalar(
            sessions,
            "SELECT count(*) FROM profile_projection_journal "
            "WHERE intent_id = :iid",
            iid=intent_id,
        )
        == 1
    )


@pytest.mark.anyio
async def test_projection_stale_flag_tracks_live_intents(
    projection, store, server_id
) -> None:
    await projection.apply_upsert(
        store, server_id, _profile_text(server_id), expected_revision=0
    )
    assert (await projection.read_projection(server_id)).stale is False
    document = _profile_text(server_id, name="renamed-host")
    await projection.prepare_upsert(
        server_id,
        document,
        expected_revision=1,
        base_sha256=_sha256(_profile_text(server_id)),
    )
    state = await projection.read_projection(server_id)
    assert (state.revision, state.stale) == (1, True)


@pytest.mark.anyio
async def test_second_live_intent_for_same_server_is_rejected(
    projection, store, server_id, sessions
) -> None:
    document = _profile_text(server_id)
    await projection.prepare_upsert(server_id, document, expected_revision=0)
    with pytest.raises(ProjectionError) as excinfo:
        await projection.prepare_upsert(
            server_id,
            _profile_text(server_id, name="other"),
            expected_revision=0,
        )
    assert excinfo.value.code is ProjectionErrorCode.INTENT_PENDING
    # Real constraint proof at the storage boundary (not just the API guard):
    other = _profile_text(server_id, name="other")
    with pytest.raises(IntegrityError):
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO profile_projection_journal (
                        intent_id, server_id, op, phase, expected_revision,
                        base_sha256, target_revision, target_sha256,
                        schema_revision, payload
                    ) VALUES (
                        :iid, :sid, 'upsert', 'prepared', 0, NULL, 1,
                        :sha, :schema_rev, :payload
                    )
                    """
                ),
                {
                    "iid": uuid.uuid4(),
                    "sid": server_id,
                    "sha": _sha256(other),
                    "schema_rev": SCHEMA_VERSION,
                    "payload": other,
                },
            )


# ---------------------------------------------------------------------------
# W1 — crash after prepared intent, before file commit
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_w1_recovery_reexecutes_uncommitted_intent(
    projection, store, server_id
) -> None:
    first = _profile_text(server_id)
    await projection.apply_upsert(store, server_id, first, expected_revision=0)
    update = _profile_text(server_id, name="renamed-host")
    intent = await projection.prepare_upsert(
        server_id,
        update,
        expected_revision=1,
        base_sha256=_sha256(first),
    )
    outcomes = await projection.recover(store, server_id=server_id)
    assert [o.status for o in outcomes] == ["reexecuted_reconciled"]
    assert outcomes[0].revision == 2
    stored = store.read(server_id)
    assert (stored.revision, stored.text) == (2, update)
    state = await projection.read_projection(server_id)
    assert (state.revision, state.sha256, state.stale) == (
        2,
        stored.sha256,
        False,
    )
    journal = await projection.get_intent(intent.intent_id)
    assert (journal.phase, journal.payload) == (PHASE_RECONCILED, None)


@pytest.mark.anyio
async def test_w1_recovery_recognizes_commit_without_rewriting_the_file(
    projection, store, server_id
) -> None:
    """Crash-after-replace variant: file at target -> advance, never rewrite."""
    first = _profile_text(server_id)
    await projection.apply_upsert(store, server_id, first, expected_revision=0)
    update = _profile_text(server_id, name="renamed-host")
    await projection.prepare_upsert(
        server_id,
        update,
        expected_revision=1,
        base_sha256=_sha256(first),
    )
    # Simulate the crash-after-replace state directly through the store.
    store.write(server_id, update, expected_revision=1)
    canonical = store.root / "servers" / str(server_id) / "profile.yaml"
    before = canonical.stat()
    outcomes = await projection.recover(store, server_id=server_id)
    assert [o.status for o in outcomes] == ["advanced_reconciled"]
    after = canonical.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert store.read(server_id).revision == 2  # no revision bump
    state = await projection.read_projection(server_id)
    assert (state.revision, state.stale) == (2, False)


@pytest.mark.anyio
async def test_w1_recovery_surfaces_file_divergence_instead_of_overwriting(
    projection, store, server_id
) -> None:
    first = _profile_text(server_id)
    await projection.apply_upsert(store, server_id, first, expected_revision=0)
    update = _profile_text(server_id, name="renamed-host")
    await projection.prepare_upsert(
        server_id,
        update,
        expected_revision=1,
        base_sha256=_sha256(first),
    )
    external = _profile_text(server_id, name="external-writer")
    store.write(server_id, external, expected_revision=1)
    outcomes = await projection.recover(store, server_id=server_id)
    assert [o.status for o in outcomes] == ["conflict"]
    assert outcomes[0].code == ProjectionErrorCode.FILE_DIVERGED.value
    # Nothing was overwritten or claimed: file, projection and journal intact.
    assert store.read(server_id).text == external
    state = await projection.read_projection(server_id)
    assert (state.revision, state.sha256) == (1, _sha256(first))
    live = await projection.list_live_intents(server_id=server_id)
    assert [i.phase for i in live] == [PHASE_PREPARED]
    # Value-free discipline: fixture profile name never leaks.
    rendered = json.dumps(
        {
            "code": outcomes[0].code,
            "detail": outcomes[0].detail,
            "revision": outcomes[0].revision,
        }
    )
    assert "build-host" not in rendered
    assert "external-writer" not in rendered
    assert "MAI_SSH_KEY_BUILD_HOST" not in rendered


@pytest.mark.anyio
async def test_recovery_surfaces_interrupted_file_pair(
    projection, store, server_id
) -> None:
    """File-store W2 half (canonical replaced, metadata lagging) is surfaced,
    never silently repaired by the projection worker."""
    first = _profile_text(server_id)
    await projection.apply_upsert(store, server_id, first, expected_revision=0)
    update = _profile_text(server_id, name="renamed-host")
    await projection.prepare_upsert(
        server_id,
        update,
        expected_revision=1,
        base_sha256=_sha256(first),
    )
    canonical = store.root / "servers" / str(server_id) / "profile.yaml"
    canonical.write_bytes(canonical.read_bytes()[:20])  # interrupted commit
    outcomes = await projection.recover(store, server_id=server_id)
    assert [o.status for o in outcomes] == ["conflict"]
    assert outcomes[0].code == FileStoreErrorCode.HASH_MISMATCH.value
    state = await projection.read_projection(server_id)
    assert (state.revision, state.stale) == (1, True)
    live = await projection.list_live_intents(server_id=server_id)
    assert [i.phase for i in live] == [PHASE_PREPARED]


@pytest.mark.anyio
async def test_apply_resumes_a_preexisting_live_intent(
    projection, store, server_id
) -> None:
    document = _profile_text(server_id)
    intent = await projection.prepare_upsert(
        server_id, document, expected_revision=0
    )
    result = await projection.apply_upsert(
        store, server_id, document, expected_revision=0, intent_id=intent.intent_id
    )
    assert result.outcome == "resumed"
    assert (await projection.read_projection(server_id)).revision == 1


@pytest.mark.anyio
async def test_retry_with_different_document_is_rejected(
    projection, store, server_id
) -> None:
    document = _profile_text(server_id)
    intent_id = uuid.uuid4()
    await projection.apply_upsert(
        store, server_id, document, expected_revision=0, intent_id=intent_id
    )
    with pytest.raises(ProjectionError) as excinfo:
        await projection.apply_upsert(
            store,
            server_id,
            _profile_text(server_id, name="other"),
            expected_revision=0,
            intent_id=intent_id,
        )
    assert excinfo.value.code is ProjectionErrorCode.PAYLOAD_MISMATCH
    assert store.read(server_id).text == document
    assert (await projection.read_projection(server_id)).revision == 1


@pytest.mark.anyio
async def test_intent_id_reuse_across_operations_is_rejected(
    projection, store, server_id
) -> None:
    intent_id = uuid.uuid4()
    await projection.apply_upsert(
        store,
        server_id,
        _profile_text(server_id),
        expected_revision=0,
        intent_id=intent_id,
    )
    with pytest.raises(ProjectionError) as excinfo:
        await projection.prepare_delete(
            server_id, expected_revision=1, intent_id=intent_id
        )
    assert excinfo.value.code is ProjectionErrorCode.INTENT_MISMATCH


# ---------------------------------------------------------------------------
# W2 — crash after file commit, before projection (core acceptance 2)
# ---------------------------------------------------------------------------

_W2_CHILD = """\
import asyncio, os, signal, uuid
from pathlib import Path

from mai.database import create_app_engine
from mai.profiles.files import ProfileFileStore
from mai.profiles.projection import ProfileProjection


async def main() -> None:
    sid = uuid.UUID(os.environ["MAI_TEST_SERVER_ID"])
    engine, sessions = create_app_engine(os.environ["MAI_DATABASE_URL"])
    store = ProfileFileStore(os.environ["MAI_DATA_ROOT"])
    projection = ProfileProjection(sessions)
    document = Path(os.environ["MAI_TEST_DOCUMENT"]).read_text(encoding="utf-8")
    await projection.prepare_upsert(
        sid,
        document,
        expected_revision=1,
        base_sha256=os.environ["MAI_TEST_BASE_SHA256"],
        intent_id=uuid.UUID(os.environ["MAI_TEST_INTENT_ID"]),
    )
    store.write(sid, document, expected_revision=1)
    await engine.dispose()
    # W2 window: die after the file commit, before the journal advance.
    os.kill(os.getpid(), signal.SIGKILL)


asyncio.run(main())
"""


@pytest.mark.anyio
async def test_w2_sigkill_between_file_commit_and_projection_converges(
    migrated_url: str, tmp_path: Path
) -> None:
    """Feature-gate scenario half (FILE-CRASH): real Postgres + data-root.

    A real child worker dies by SIGKILL after the canonical file commit and
    before the journal advance ("stop connection after file commit"). The
    restarted projection worker (the production entrypoint) repairs the
    projection to the converged revision/hash without discarding the
    canonical update, and the same-intent retry confirms exactly one
    resulting profile with no document double-write.
    """
    sid = uuid.uuid4()
    root = tmp_path / "data-root"
    store = ProfileFileStore(root)
    engine, sessions = create_app_engine(migrated_url)
    projection = ProfileProjection(sessions)
    first = _profile_text(server_id=sid)
    update = _profile_text(server_id=sid, name="renamed-host")
    update_path = tmp_path / "update.yaml"
    update_path.write_text(update, encoding="utf-8")
    intent_id = uuid.uuid4()
    try:
        await projection.apply_upsert(store, sid, first, expected_revision=0)

        env = {
            **os.environ,
            "MAI_DATABASE_URL": migrated_url,
            "MAI_DATA_ROOT": str(root),
            "MAI_TEST_SERVER_ID": str(sid),
            "MAI_TEST_INTENT_ID": str(intent_id),
            "MAI_TEST_DOCUMENT": str(update_path),
            "MAI_TEST_BASE_SHA256": _sha256(first),
        }
        killed = subprocess.run(
            [sys.executable, "-c", _W2_CHILD],
            capture_output=True,
            text=True,
            cwd=BACKEND_ROOT,
            env=env,
            timeout=120,
            check=False,
        )
        assert killed.returncode == -signal.SIGKILL, killed.stderr

        # Window state: file authoritative, projection stale, intent live.
        assert store.read(sid).revision == 2
        stale = await projection.read_projection(sid)
        assert (stale.revision, stale.sha256, stale.stale) == (
            1,
            _sha256(first),
            True,
        )
        intent = await projection.get_intent(intent_id)
        assert intent.phase == PHASE_PREPARED
        assert intent.payload == update

        # Restart the projection worker via the real entrypoint.
        recovered = subprocess.run(
            [
                sys.executable,
                "-m",
                "mai.profiles.projection",
                "recover",
                str(root),
                str(sid),
            ],
            capture_output=True,
            text=True,
            cwd=BACKEND_ROOT,
            env={**os.environ, "MAI_DATABASE_URL": migrated_url},
            timeout=120,
            check=False,
        )
        assert recovered.returncode == 0, recovered.stderr
        assert "postgresql://" not in recovered.stdout
        lines = [json.loads(line) for line in recovered.stdout.splitlines()]
        assert lines[0]["kind"] == "recovery"
        assert (lines[0]["status"], lines[0]["revision"]) == (
            "advanced_reconciled",
            2,
        )
        assert lines[-1] == {
            "kind": "recovery_summary",
            "count": 1,
            "unresolved": 0,
        }
        assert "build-host" not in recovered.stdout

        # Converged revision/hash, staleness cleared.
        stored = store.read(sid)
        state = await projection.read_projection(sid)
        assert (state.revision, state.schema_revision, state.stale) == (
            stored.revision,
            stored.schema_revision,
            False,
        )
        assert state.sha256 == stored.sha256

        # No document double-write at rest.
        payload = await _raw_scalar(
            sessions,
            "SELECT payload FROM profile_projection_journal "
            "WHERE intent_id = :iid",
            iid=intent_id,
        )
        assert payload is None

        # Retry the same intent: no effect, one resulting profile.
        retry = await projection.apply_upsert(
            store,
            sid,
            update,
            expected_revision=1,
            base_sha256=_sha256(first),
            intent_id=intent_id,
        )
        assert retry.outcome == "already_reconciled"
        assert store.read(sid).revision == 2
        assert (
            await _raw_scalar(
                sessions,
                "SELECT count(*) FROM profile_projection "
                "WHERE server_id = :sid",
                sid=sid,
            )
            == 1
        )
        assert (
            await _raw_scalar(
                sessions,
                "SELECT count(*) FROM profile_projection_journal "
                "WHERE intent_id = :iid",
                iid=intent_id,
            )
            == 1
        )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# W3 — crash during the projection write (transaction boundary)
# ---------------------------------------------------------------------------


class _FailAfterProjectionWrite(AsyncSession):
    """Aborts the transaction between the reconcile's two statements.

    The first statement (projection upsert) really executes on the live
    server; the injected failure must roll it back together with the journal
    advance — the W3 contract that the pair is never mixed.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._saw_projection_write = False

    async def execute(self, statement: object, *args: object, **kwargs: object):
        if self._saw_projection_write:
            raise RuntimeError(
                "injected failure between projection write and journal advance"
            )
        if "INSERT INTO profile_projection (" in str(statement):
            self._saw_projection_write = True
        return await super().execute(statement, *args, **kwargs)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_w3_projection_and_journal_advance_are_atomic(
    migrated_url: str, tmp_path: Path
) -> None:
    sid = uuid.uuid4()
    store = ProfileFileStore(tmp_path / "data-root")
    engine, real_sessions = create_app_engine(migrated_url)
    try:
        real = ProfileProjection(real_sessions)
        first = _profile_text(server_id=sid)
        update = _profile_text(server_id=sid, name="renamed-host")
        await real.apply_upsert(store, sid, first, expected_revision=0)

        # Simulate the crashed worker's partial progress: file committed and
        # journal advanced to file_committed, projection not yet written.
        intent = await real.prepare_upsert(
            sid, update, expected_revision=1, base_sha256=_sha256(first)
        )
        store.write(sid, update, expected_revision=1)
        await real._mark_file_committed(intent.intent_id)

        failing_sessions = async_sessionmaker(
            engine, class_=_FailAfterProjectionWrite, expire_on_commit=False
        )
        with pytest.raises(RuntimeError):
            await ProfileProjection(failing_sessions)._reconcile_upsert_from_file(
                intent, store
            )

        # Rolled back together: projection still revision 1, journal still
        # file_committed (the pair is never mixed).
        state = await real.read_projection(sid)
        assert (state.revision, state.sha256) == (1, _sha256(first))
        journal = await real.get_intent(intent.intent_id)
        assert journal.phase == PHASE_FILE_COMMITTED

        # The restarted worker converges from the file's observed truth.
        outcomes = await real.recover(store, server_id=sid)
        assert [o.status for o in outcomes] == ["advanced_reconciled"], [
            (o.status, o.code, o.detail) for o in outcomes
        ]
        state = await real.read_projection(sid)
        assert (state.revision, state.sha256, state.stale) == (
            2,
            _sha256(update),
            False,
        )
        journal = await real.get_intent(intent.intent_id)
        assert (journal.phase, journal.payload) == (PHASE_RECONCILED, None)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# W4 — delete tombstone, purge and resurrected-file conflict
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_retry_of_reconciled_intent_repairs_stale_projection(
    projection, store, server_id, sessions
) -> None:
    """Reconciled intent + projection drifted behind: a retry must repair the
    projection from the canonical file instead of claiming a success the
    projection does not hold (found in the fixture-scenario transcript)."""
    first = _profile_text(server_id)
    update = _profile_text(server_id, name="renamed-host")
    await projection.apply_upsert(store, server_id, first, expected_revision=0)
    intent = await projection.prepare_upsert(
        server_id, update, expected_revision=1, base_sha256=_sha256(first)
    )
    store.write(server_id, update, expected_revision=1)
    await projection._mark_file_committed(intent.intent_id)
    # External interference: the journal row is forced to 'reconciled' while
    # the projection write never happened (the state a partial external tool
    # or an earlier bug can leave behind).
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE profile_projection_journal SET phase = 'reconciled' "
                "WHERE intent_id = :iid"
            ),
            {"iid": intent.intent_id},
        )
    state = await projection.read_projection(server_id)
    assert (state.revision, state.stale) == (1, False)  # stale, no live intent

    result = await projection.apply_upsert(
        store,
        server_id,
        update,
        expected_revision=1,
        base_sha256=_sha256(first),
        intent_id=intent.intent_id,
    )
    assert result.outcome == "resumed"
    stored = store.read(server_id)
    assert stored.revision == 2  # canonical update was never discarded
    state = await projection.read_projection(server_id)
    assert (state.revision, state.sha256, state.stale) == (
        2,
        stored.sha256,
        False,
    )


@pytest.mark.anyio
async def test_w4_delete_purges_projection_after_tombstone(
    projection, store, server_id
) -> None:
    first = _profile_text(server_id)
    await projection.apply_upsert(store, server_id, first, expected_revision=0)
    result = await projection.apply_delete(
        store, server_id, expected_revision=1, base_sha256=_sha256(first)
    )
    assert result.outcome == "applied"
    assert await projection.read_projection(server_id) is None
    with pytest.raises(ProfileFileError) as excinfo:
        store.read(server_id)
    assert excinfo.value.code is FileStoreErrorCode.NOT_FOUND
    journal = await projection.get_intent(result.intent_id)
    assert (journal.op, journal.phase, journal.payload) == (
        "delete",
        PHASE_RECONCILED,
        None,
    )


@pytest.mark.anyio
async def test_w4_resurrected_file_with_live_tombstone_conflicts(
    projection, store, server_id
) -> None:
    first = _profile_text(server_id)
    await projection.apply_upsert(store, server_id, first, expected_revision=0)
    await projection.prepare_delete(
        server_id, expected_revision=1, base_sha256=_sha256(first)
    )
    resurrected = _profile_text(server_id, name="recreated-host")
    store.write(server_id, resurrected, expected_revision=1)
    outcomes = await projection.recover(store, server_id=server_id)
    assert [o.status for o in outcomes] == ["conflict"]
    assert outcomes[0].code == ProjectionErrorCode.TOMBSTONE_CONFLICT.value
    # The resurrected file stands, the stale projection row stands, the
    # tombstone stays live for explicit admin resolution.
    assert store.read(server_id).text == resurrected
    state = await projection.read_projection(server_id)
    assert (state.revision, state.sha256, state.stale) == (
        1,
        _sha256(first),
        True,
    )
    live = await projection.list_live_intents(server_id=server_id)
    assert [i.op for i in live] == ["delete"]


@pytest.mark.anyio
async def test_w4_delete_resume_when_file_already_gone(
    projection, store, server_id
) -> None:
    first = _profile_text(server_id)
    await projection.apply_upsert(store, server_id, first, expected_revision=0)
    await projection.prepare_delete(
        server_id, expected_revision=1, base_sha256=_sha256(first)
    )
    store.delete(server_id, expected_revision=1)  # crash before journal advance
    outcomes = await projection.recover(store, server_id=server_id)
    assert [o.status for o in outcomes] == ["advanced_reconciled"]
    assert await projection.read_projection(server_id) is None
    assert await projection.list_live_intents(server_id=server_id) == []


@pytest.mark.anyio
async def test_purge_reconciled_removes_history_and_keeps_live(
    projection, store, server_id, sessions
) -> None:
    first = _profile_text(server_id)
    update = _profile_text(server_id, name="renamed-host")
    await projection.apply_upsert(store, server_id, first, expected_revision=0)
    await projection.apply_upsert(
        store,
        server_id,
        update,
        expected_revision=1,
        base_sha256=_sha256(first),
    )
    live_intent = await projection.prepare_delete(
        server_id, expected_revision=2, base_sha256=_sha256(update)
    )

    async def reconciled_for(sid: uuid.UUID) -> int:
        return (
            await _raw_scalar(
                sessions,
                "SELECT count(*) FROM profile_projection_journal "
                "WHERE server_id = :sid AND phase = 'reconciled'",
                sid=sid,
            )
            or 0
        )

    # Whole-table maintenance sweep: this server's two reconciled rows are
    # included (other tests may hold more; assertions stay server-scoped).
    assert await reconciled_for(server_id) == 2
    assert await projection.purge_reconciled() >= 2
    assert await reconciled_for(server_id) == 0
    live = await projection.list_live_intents(server_id=server_id)
    assert [i.intent_id for i in live] == [live_intent.intent_id]
    # Explicit tombstone purge honors the frozen order: only after reconcile.
    # The live tombstone blocks a new upsert intent (one live intent per
    # server), so reconcile it first, then create a fresh reconciled row.
    fresh = _profile_text(server_id, name="second-host")
    with pytest.raises(ProjectionError) as excinfo:
        await projection.apply_upsert(
            store, server_id, fresh, expected_revision=0
        )
    assert excinfo.value.code is ProjectionErrorCode.INTENT_PENDING
    await projection.apply_delete(
        store,
        server_id,
        expected_revision=2,
        base_sha256=_sha256(update),
        intent_id=live_intent.intent_id,
    )
    await projection.apply_upsert(store, server_id, fresh, expected_revision=0)
    assert await reconciled_for(server_id) == 2
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert await projection.purge_reconciled(before=past) >= 0
    assert await reconciled_for(server_id) == 2
    assert await projection.purge_reconciled(before=future) >= 2
    assert await reconciled_for(server_id) == 0


# ---------------------------------------------------------------------------
# Input validation — no filesystem or SQL effect before success
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_input_validation_rejects_before_any_effect() -> None:
    # Sentinel factory: any SQL usage would explode loudly — validation must
    # fire first and keep the rejection value-free.
    projection = ProfileProjection(
        cast("async_sessionmaker[AsyncSession]", object())
    )
    sid = uuid.uuid4()
    with pytest.raises(ProjectionError) as excinfo:
        await projection.prepare_upsert("not-a-uuid", "doc", expected_revision=0)
    assert excinfo.value.code is ProjectionErrorCode.INVALID_SERVER_ID
    with pytest.raises(ProjectionError) as excinfo:
        await projection.prepare_upsert(sid, "doc", expected_revision=0, intent_id=7)
    assert excinfo.value.code is ProjectionErrorCode.INVALID_INTENT_ID
    with pytest.raises(ProjectionError) as excinfo:
        await projection.prepare_upsert(sid, "doc", expected_revision=-1)
    assert excinfo.value.code is ProjectionErrorCode.INVALID_REVISION
    with pytest.raises(ProjectionError) as excinfo:
        await projection.prepare_upsert(
            sid, "doc", expected_revision=True
        )
    assert excinfo.value.code is ProjectionErrorCode.INVALID_REVISION
    with pytest.raises(ProjectionError) as excinfo:
        await projection.prepare_upsert(
            sid, "doc", expected_revision=0, base_sha256="nothex"
        )
    assert excinfo.value.code is ProjectionErrorCode.INVALID_HASH
    with pytest.raises(ProjectionError) as excinfo:
        await projection.prepare_upsert(sid, 123, expected_revision=0)
    assert excinfo.value.code is ProjectionErrorCode.INVALID_DOCUMENT
    with pytest.raises(ProjectionError) as excinfo:
        await projection.prepare_delete(sid, expected_revision=True)
    assert excinfo.value.code is ProjectionErrorCode.INVALID_REVISION
    with pytest.raises(ProjectionError) as excinfo:
        await projection.read_projection("not-a-uuid")
    assert excinfo.value.code is ProjectionErrorCode.INVALID_SERVER_ID
    # Schema-invalid documents are refused at prepare time (frozen producer).
    with pytest.raises(ProfileValidationError):
        await projection.prepare_upsert(sid, "schema_version: 9\n", expected_revision=0)


# ---------------------------------------------------------------------------
# CLI entrypoint contract (value-free JSON lines; 0/1/2 exits)
# ---------------------------------------------------------------------------


def test_cli_status_recover_and_error_contracts(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MAI_DATABASE_URL", migrated_url)
    root = str(tmp_path / "cli-root")
    # Scope to a fresh server so sibling tests' live intents stay untouched.
    fresh = str(uuid.uuid4())
    assert main(["status", root, fresh]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert json.loads(lines[-1]) == {"kind": "status", "live_intents": 0}
    assert any(json.loads(line)["kind"] == "projection" for line in lines[:-1])
    assert main(["recover", root, fresh]) == 0
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary == {"kind": "recovery_summary", "count": 0, "unresolved": 0}
    assert main(["recover", root, "not-a-uuid"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == ProjectionErrorCode.INVALID_SERVER_ID.value


def test_cli_missing_database_config_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("MAI_DATABASE_URL", raising=False)
    assert main(["status", str(tmp_path)]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == DatabaseHealth.NOT_CONFIGURED.value


def test_module_entrypoint_runs_under_python_m(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAI_DATABASE_URL", migrated_url)
    done = subprocess.run(
        [sys.executable, "-m", "mai.profiles.projection", "--help"],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "recover" in done.stdout
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "mai.profiles.projection",
            "status",
            str(tmp_path / "s-root"),
        ],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.splitlines()[-1])["kind"] == "status"
