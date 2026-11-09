"""Focused tests for payload-bound approvals and one-use decisions.

Card ``03-approval-store`` against contracts.md **C03 rule 4** (approval
binding: payload hash, server/target, scope, policy revision, expiry) and
**C01 rules 1–2**; the registered P03 scenario coverage owned here is
APPROVAL-PAYLOAD plus the brief's gate scenario: *two actual processes race
to consume a decision in real Postgres; exactly one authorized consumption,
changed payload denied, process restart preserves decision state* — with the
effect counter unchanged on every denial.

Boundary discipline: every persistence/atomicity claim runs against a real
disposable Postgres through the real linear Alembic chain (fixture
``migrated_url``), never against mocks — mocks cannot prove the row-lock
serialization of concurrent consumption. Concurrency is proven two ways:
an in-suite 8-way concurrent race over separate pooled connections and a
real fresh-OS-process race (production ``ApprovalStore`` per process).

Secret hygiene: payloads exist only as synthetic SHA-256 digests (the store
API cannot accept payload bytes at all); fixture principals are inert
constants; nothing secret is printed to captured stdout or evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from mai.database import create_app_engine
from mai.policy.approvals import (
    Approval,
    ApprovalError,
    ApprovalErrorCode,
    ApprovalRequest,
    ApprovalStore,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: The constant tracks the full chain; head assertions target the TIP, so
#: every later chain appender (0004 jobs, 0005 knowledge, 0006 applications,
#: 0007 observations) keeps these chain-aware tests truthful.
REVISION_IDS = (
    "0001", "0002", "0003", "0004", "0005", "0006", "0007",
    "0008", "0009", "0010", "0011", "0012", "0013", "0014", "0015",
    "0016",
)

#: Disposable in-fixture stand-in for the privileged effect whose execution
#: the approval would authorize (APPROVAL-PAYLOAD observable: unchanged on
#: every denial). Created/dropped per test by the ``effect_counter`` fixture.
_EFFECT_TABLE = "approval_effect_counter"

#: Inert fixture binding (NOT secret material; digests of synthetic bytes).
CAPABILITY = "deploy.service"
OTHER_CAPABILITY = "shell.raw"
PRINCIPAL = "admin"
OTHER_PRINCIPAL = "operator"


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(ALEMBIC_INI))


def _payload_hash(payload: bytes) -> str:
    """Canonical dispatcher-side hash (the store never sees payload bytes)."""
    return hashlib.sha256(payload).hexdigest()


GOOD_PAYLOAD_HASH = _payload_hash(b'{"op": "deploy", "v": 1}')
ALTERED_PAYLOAD_HASH = _payload_hash(b'{"op": "deploy", "v": 2}')


# ---------------------------------------------------------------------------
# Live fixture: real disposable Postgres + real linear Alembic chain
# ---------------------------------------------------------------------------


def _reset_fixture_db(url: str) -> None:
    """Reset the disposable database with the explicit full-chain drop list.

    Earlier modules can leave tables from migrations 0001 through 0007.
    The observation sequence is not owned by its table and needs its own
    drop, matching the reset in tests/test_servers_http.py.
    """
    with psycopg.connect(url) as conn:
        # Whole-schema reset (D09): FK-safe and immune to future revisions.
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> Iterator[str]:
    """Migrate from zero, then restore pre-bootstrap state for repeat runs."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the approval-store claims need a "
            "real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _reset_fixture_db(url)
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    yield url
    _reset_fixture_db(url)


@pytest.fixture
async def sessions(migrated_url: str):
    engine, factory = create_app_engine(migrated_url)
    yield factory
    await engine.dispose()


@pytest.fixture
def store(sessions) -> ApprovalStore:
    return ApprovalStore(sessions)


@pytest.fixture
def clean_approvals(migrated_url: str):
    """Empty approval state per test (real DB)."""
    with psycopg.connect(migrated_url) as conn:
        conn.execute("TRUNCATE approvals")
        conn.commit()
    yield


@pytest.fixture
def effect_counter(migrated_url: str):
    """One disposable effect-counter table (the would-be privileged effect)."""
    with psycopg.connect(migrated_url) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {_EFFECT_TABLE}")
        conn.execute(
            f"CREATE TABLE {_EFFECT_TABLE} "
            "(n integer NOT NULL, consumed_by text NOT NULL)"
        )
        conn.commit()
    yield
    with psycopg.connect(migrated_url) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {_EFFECT_TABLE}")
        conn.commit()


async def _scalar(sessions, sql: str, **params: object) -> object:
    async with sessions() as session:
        return (await session.execute(text(sql), params)).scalar()


async def _effect_rows(sessions) -> list[tuple[int, str]]:
    async with sessions() as session:
        return (
            await session.execute(
                text(f"SELECT n, consumed_by FROM {_EFFECT_TABLE} ORDER BY 1")
            )
        ).all()


async def _backdate(
    sessions, approval_id: uuid.UUID, **columns: object
) -> None:
    """Clock simulator: move anchor + expiry columns together so the
    migration CHECKs (``expires_at > created_at``) stay satisfied — the
    same discipline the 02-auth-store clock simulator established."""
    assignments = ", ".join(f"{column} = :{column}" for column in columns)
    async with sessions() as session, session.begin():
        await session.execute(
            text(f"UPDATE approvals SET {assignments} WHERE approval_id = :a"),
            {"a": approval_id, **columns},
        )


def _reject(excinfo: pytest.ExceptionInfo, code: ApprovalErrorCode) -> ApprovalError:
    error = excinfo.value
    assert isinstance(error, ApprovalError) and error.code is code
    return error


def _request(**overrides: object) -> ApprovalRequest:
    values: dict[str, object] = {
        "capability_id": CAPABILITY,
        "server_id": uuid.uuid4(),
        "payload_hash": GOOD_PAYLOAD_HASH,
        "policy_revision": 7,
        "created_by": PRINCIPAL,
        "expires_at": _utcnow_awake() + timedelta(minutes=10),
    }
    values.update(overrides)
    return ApprovalRequest(**values)


def _utcnow_awake() -> datetime:
    return datetime.now(timezone.utc)


def _binding(request: ApprovalRequest, **overrides: object) -> dict[str, object]:
    """The consume-time presentation of the binding, from a request."""
    values: dict[str, object] = {
        "capability_id": request.capability_id,
        "server_id": request.server_id,
        "payload_hash": request.payload_hash,
        "policy_revision": request.policy_revision,
        "consumed_by": OTHER_PRINCIPAL,  # consuming principal ≠ granting one
    }
    values.update(overrides)
    return values


async def _create(store: ApprovalStore, **overrides: object) -> Approval:
    return await store.create(_request(**overrides))


# ---------------------------------------------------------------------------
# Migration chain — serialized after 0002 (exclusive migration-chain resource)
# ---------------------------------------------------------------------------


def test_migration_chain_is_linear_and_approvals_follow_sessions(
    migrated_url: str,
) -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    assert script.get_heads() == [REVISION_IDS[-1]]
    revisions = {r.revision: r for r in script.walk_revisions()}
    assert sorted(revisions) == sorted(REVISION_IDS)
    assert revisions[REVISION_IDS[0]].down_revision is None
    for previous, following in zip(REVISION_IDS, REVISION_IDS[1:]):
        assert revisions[following].down_revision == previous  # strict order


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
async def test_approval_schema_matches_frozen_migration_columns(sessions) -> None:
    """0003 column/type/nullability map + PK + full CHECK inventory."""
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT column_name, data_type, is_nullable
                      FROM information_schema.columns
                     WHERE table_schema = 'public' AND table_name = 'approvals'
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
                     WHERE c.relname = 'approvals' AND i.indisprimary
                    """
                )
            )
        ).scalars().all()
        checks = (
            await session.execute(
                text(
                    """
                    SELECT conname FROM pg_constraint
                     WHERE contype = 'c' AND conrelid = 'approvals'::regclass
                    """
                )
            )
        ).scalars().all()
    columns = {name: (dtype, nullable) for name, dtype, nullable in rows}
    assert set(columns) == {
        "approval_id",
        "capability_id",
        "server_id",
        "payload_hash",
        "policy_revision",
        "created_by",
        "created_at",
        "expires_at",
        "consumed_at",
        "consumed_by",
    }
    assert columns["approval_id"] == ("uuid", "NO")
    assert columns["capability_id"] == ("text", "NO")
    assert columns["server_id"] == ("uuid", "NO")
    assert columns["payload_hash"] == ("text", "NO")
    assert columns["policy_revision"] == ("integer", "NO")
    assert columns["created_by"] == ("text", "NO")
    for column in ("created_at", "expires_at", "consumed_at"):
        assert columns[column] == ("timestamp with time zone", "NO" if column != "consumed_at" else "YES")
    assert columns["consumed_by"] == ("text", "YES")
    assert pk == ["approval_id"]
    assert set(checks) == {
        "ck_approvals_capability_slug",
        "ck_approvals_payload_hash",
        "ck_approvals_policy_revision",
        "ck_approvals_created_by",
        "ck_approvals_consumed_by",
        "ck_approvals_consumption_pair",
        "ck_approvals_expiry_after_creation",
        "ck_approvals_consumption_monotonic",
    }


@pytest.mark.parametrize(
    ("violating", "constraint"),
    [
        ({"payload_hash": "not-a-hash"}, "ck_approvals_payload_hash"),
        ({"payload_hash": "AB" * 32}, "ck_approvals_payload_hash"),
        ({"capability_id": "Deploy.Service"}, "ck_approvals_capability_slug"),
        ({"policy_revision": 0}, "ck_approvals_policy_revision"),
        ({"created_by": ""}, "ck_approvals_created_by"),
        ({"consumed_by": ""}, "ck_approvals_consumed_by"),
    ],
)
@pytest.mark.anyio
async def test_storage_check_constraints_reject_hostile_rows_at_the_boundary(
    sessions, clean_approvals, violating: dict, constraint: str
) -> None:
    """The DB itself (not only the module) refuses malformed approvals."""
    base = {
        "approval_id": uuid.uuid4(),
        "capability_id": CAPABILITY,
        "server_id": uuid.uuid4(),
        "payload_hash": GOOD_PAYLOAD_HASH,
        "policy_revision": 7,
        "created_by": PRINCIPAL,
        "expires_at": _utcnow_awake() + timedelta(minutes=10),
    }
    base.update(violating)
    columns = ", ".join(base)
    params = ", ".join(f":{name}" for name in base)
    with pytest.raises(IntegrityError) as excinfo:
        async with sessions() as session, session.begin():
            await session.execute(
                text(f"INSERT INTO approvals ({columns}) VALUES ({params})"),
                base,
            )
    assert constraint in str(excinfo.value)


@pytest.mark.anyio
async def test_storage_rejects_dead_expiry_and_lopsided_consumption_pairs(
    sessions, clean_approvals
) -> None:
    """Expiry/creation ordering and consumption-pair CHECKs are real."""
    row = {
        "approval_id": uuid.uuid4(),
        "capability_id": CAPABILITY,
        "server_id": uuid.uuid4(),
        "payload_hash": GOOD_PAYLOAD_HASH,
        "policy_revision": 7,
        "created_by": PRINCIPAL,
    }
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO approvals (approval_id, capability_id, server_id, "
                "payload_hash, policy_revision, created_by, created_at, "
                "expires_at) VALUES (:approval_id, :capability_id, :server_id, "
                ":payload_hash, :policy_revision, :created_by, "
                "now() - interval '2 hours', now() - interval '1 hour')"
            ),
            row,
        )
    # Lopsided consumption pair (consumed_at set, consumed_by NULL) must be
    # refused by ck_approvals_consumption_pair — the boundary of last resort.
    with pytest.raises(IntegrityError) as excinfo:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO approvals (approval_id, capability_id, server_id, "
                    "payload_hash, policy_revision, created_by, created_at, "
                    "expires_at, consumed_at) "
                    "VALUES (:approval_id, :capability_id, :server_id, "
                    ":payload_hash, :policy_revision, :created_by, "
                    "now() - interval '1 hour', now() + interval '1 hour', now())"
                ),
                row | {"approval_id": uuid.uuid4()},
            )
    assert "ck_approvals_consumption_pair" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Construction validation — value-free rejections BEFORE any SQL effect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"capability_id": "BAD CAP"},
        {"capability_id": ""},
        {"server_id": "not-a-uuid"},
        {"payload_hash": "short"},
        {"payload_hash": "AB" * 32},
        {"policy_revision": 0},
        {"policy_revision": True},
        {"created_by": ""},
        {"created_by": "p" * 257},
        {"expires_at": "2026-01-01T00:00:00+00:00"},
    ],
)
def test_request_construction_rejects_malformed_bindings(
    overrides: dict,
) -> None:
    with pytest.raises(ApprovalError) as excinfo:
        _request(**overrides)
    _reject(excinfo, ApprovalErrorCode.INVALID_APPROVAL)


@pytest.mark.anyio
async def test_create_refuses_to_persist_an_already_dead_approval(
    store: ApprovalStore, sessions, clean_approvals
) -> None:
    """``expires_at`` in the past at creation is rejected before INSERT."""
    with pytest.raises(ApprovalError) as excinfo:
        await store.create(
            _request(expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        )
    _reject(excinfo, ApprovalErrorCode.INVALID_APPROVAL)
    assert await _scalar(sessions, "SELECT count(*) FROM approvals") == 0


# ---------------------------------------------------------------------------
# Create + audit read
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_persists_pending_record_with_decision_ownership(
    store: ApprovalStore, sessions, clean_approvals
) -> None:
    request = _request()
    record = await store.create(request)
    assert isinstance(record.approval_id, uuid.UUID)
    assert record.status == "pending"
    assert record.consumed_at is None and record.consumed_by is None
    assert record.payload_hash == GOOD_PAYLOAD_HASH
    assert record.created_by == PRINCIPAL
    stored = await store.get(record.approval_id)
    assert stored == record
    # Two decisions are two rows with distinct opaque identities (C01 rule 1).
    other = await store.create(request)
    assert other.approval_id != record.approval_id


@pytest.mark.anyio
async def test_get_is_none_for_unknown_and_strict_about_input(
    store: ApprovalStore, clean_approvals
) -> None:
    assert await store.get(uuid.uuid4()) is None
    with pytest.raises(ApprovalError) as excinfo:
        await store.get("not-a-uuid")  # type: ignore[arg-type]
    _reject(excinfo, ApprovalErrorCode.INVALID_APPROVAL)


@pytest.mark.anyio
async def test_record_json_is_frozen_clock_free_audit_shape(
    store: ApprovalStore, clean_approvals
) -> None:
    record = await _create(store)
    document = record.to_json()
    assert list(document) == [
        "approval_id",
        "capability_id",
        "server_id",
        "payload_hash",
        "policy_revision",
        "status",
        "created_by",
        "created_at",
        "expires_at",
        "consumed_by",
        "consumed_at",
    ]
    assert document["status"] == "pending"
    assert GOOD_PAYLOAD_HASH not in repr(record)  # binding digests stay out


# ---------------------------------------------------------------------------
# Atomic one-use consumption — happy path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_consume_with_exact_binding_spends_the_decision_once(
    store: ApprovalStore, sessions, clean_approvals, effect_counter
) -> None:
    record = await _create(store)
    receipt = await store.consume(record.approval_id, **_binding(record))
    assert receipt.status == "consumed"
    assert receipt.consumed_at is not None
    assert receipt.consumed_by == OTHER_PRINCIPAL
    assert receipt.created_by == PRINCIPAL  # decision ownership preserved
    persisted = await store.get(record.approval_id)
    assert persisted == receipt  # the receipt IS the persisted state
    # The authorized effect runs exactly once.
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                f"INSERT INTO {_EFFECT_TABLE} (n, consumed_by) VALUES (1, :p)"
            ),
            {"p": receipt.consumed_by},
        )
    assert await _effect_rows(sessions) == [(1, OTHER_PRINCIPAL)]


# ---------------------------------------------------------------------------
# Denials — stable reasons, row untouched, effect counter unchanged
# ---------------------------------------------------------------------------


async def _assert_denial(
    store: ApprovalStore,
    sessions,
    record: Approval,
    code: ApprovalErrorCode,
    **overrides: object,
) -> ApprovalError:
    """One denial: stable code, pending/consumed state unchanged, no effect."""
    before = await store.get(record.approval_id)
    with pytest.raises(ApprovalError) as excinfo:
        await store.consume(record.approval_id, **_binding(record, **overrides))
    error = _reject(excinfo, code)
    assert await store.get(record.approval_id) == before
    assert await _effect_rows(sessions) == []
    return error


@pytest.mark.anyio
async def test_consume_rejects_altered_payload_and_requires_new_approval(
    store: ApprovalStore, sessions, clean_approvals, effect_counter
) -> None:
    record = await _create(store)
    error = await _assert_denial(
        store, sessions, record,
        ApprovalErrorCode.PAYLOAD_MISMATCH,
        payload_hash=ALTERED_PAYLOAD_HASH,
    )
    assert GOOD_PAYLOAD_HASH not in error.detail  # value-free detail
    # The denial did not burn the decision: the original payload still works.
    receipt = await store.consume(record.approval_id, **_binding(record))
    assert receipt.status == "consumed"


@pytest.mark.anyio
async def test_consume_rejects_changed_target(
    store: ApprovalStore, sessions, clean_approvals, effect_counter
) -> None:
    record = await _create(store)
    await _assert_denial(
        store, sessions, record,
        ApprovalErrorCode.TARGET_MISMATCH,
        server_id=uuid.uuid4(),
    )


@pytest.mark.anyio
async def test_consume_rejects_changed_capability(
    store: ApprovalStore, sessions, clean_approvals, effect_counter
) -> None:
    record = await _create(store)
    await _assert_denial(
        store, sessions, record,
        ApprovalErrorCode.CAPABILITY_MISMATCH,
        capability_id=OTHER_CAPABILITY,
    )


@pytest.mark.anyio
async def test_consume_rejects_revoked_grant_via_policy_revision(
    store: ApprovalStore, sessions, clean_approvals, effect_counter
) -> None:
    """Grant revocation is a new policy revision (03-policy-config): the
    stale approval can never match it (C03 rule 4 revision binding)."""
    record = await _create(store, policy_revision=7)
    await _assert_denial(
        store, sessions, record,
        ApprovalErrorCode.REVISION_MISMATCH,
        policy_revision=8,
    )


@pytest.mark.anyio
async def test_consume_rejects_expired_approval_without_consuming(
    store: ApprovalStore, sessions, clean_approvals, effect_counter
) -> None:
    record = await _create(store)
    await _backdate(
        sessions,
        record.approval_id,
        created_at=_utcnow_awake() - timedelta(hours=2),
        expires_at=_utcnow_awake() - timedelta(hours=1),
    )
    backdated = await store.get(record.approval_id)
    assert backdated is not None
    await _assert_denial(store, sessions, backdated, ApprovalErrorCode.EXPIRED)


@pytest.mark.anyio
async def test_replay_of_consumed_decision_is_rejected_and_state_immutable(
    store: ApprovalStore, sessions, clean_approvals, effect_counter
) -> None:
    record = await _create(store)
    first = await store.consume(record.approval_id, **_binding(record))
    replay = await _assert_denial(
        store, sessions, first, ApprovalErrorCode.ALREADY_CONSUMED
    )
    assert "one-use" in replay.detail
    still = await store.get(record.approval_id)
    assert still == first  # winner's receipt untouched (no re-consumption)


@pytest.mark.anyio
async def test_denial_precedence_is_frozen_first_match(
    store: ApprovalStore, sessions, clean_approvals, effect_counter
) -> None:
    """Consumed beats binding mismatch; expired beats binding mismatch."""
    consumed = await _create(store)
    await store.consume(consumed.approval_id, **_binding(consumed))
    await _assert_denial(
        store, sessions, consumed,
        ApprovalErrorCode.ALREADY_CONSUMED,
        payload_hash=ALTERED_PAYLOAD_HASH,
    )
    expired = await _create(store)
    await _backdate(
        sessions,
        expired.approval_id,
        created_at=_utcnow_awake() - timedelta(hours=2),
        expires_at=_utcnow_awake() - timedelta(hours=1),
    )
    backdated = await store.get(expired.approval_id)
    assert backdated is not None
    await _assert_denial(
        store, sessions, backdated,
        ApprovalErrorCode.EXPIRED,
        payload_hash=ALTERED_PAYLOAD_HASH,
    )


@pytest.mark.anyio
async def test_consume_of_unknown_id_is_not_found(
    store: ApprovalStore, clean_approvals, effect_counter
) -> None:
    with pytest.raises(ApprovalError) as excinfo:
        await store.consume(
            uuid.uuid4(),
            capability_id=CAPABILITY,
            server_id=uuid.uuid4(),
            payload_hash=GOOD_PAYLOAD_HASH,
            policy_revision=7,
            consumed_by=OTHER_PRINCIPAL,
        )
    _reject(excinfo, ApprovalErrorCode.NOT_FOUND)


@pytest.mark.anyio
async def test_consume_validates_input_before_any_sql(
    store: ApprovalStore, sessions, clean_approvals, effect_counter
) -> None:
    record = await _create(store)
    with pytest.raises(ApprovalError) as excinfo:
        await store.consume(
            record.approval_id,
            capability_id="BAD CAP",
            server_id=record.server_id,
            payload_hash=GOOD_PAYLOAD_HASH,
            policy_revision=record.policy_revision,
            consumed_by=OTHER_PRINCIPAL,
        )
    _reject(excinfo, ApprovalErrorCode.INVALID_APPROVAL)
    assert (await store.get(record.approval_id)).status == "pending"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Concurrency — real racing consumers over separate connections
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_eight_concurrent_consumers_produce_exactly_one_effect(
    store: ApprovalStore, sessions, clean_approvals, effect_counter
) -> None:
    record = await _create(store)

    async def contender(index: int):
        try:
            return await store.consume(
                record.approval_id,
                **_binding(record, consumed_by=f"racer-{index}"),
            )
        except ApprovalError as error:
            return error

    outcomes = await asyncio.gather(*(contender(i) for i in range(8)))
    receipts = [o for o in outcomes if isinstance(o, Approval)]
    denials = [o for o in outcomes if isinstance(o, ApprovalError)]
    assert len(receipts) == 1
    assert all(d.code is ApprovalErrorCode.ALREADY_CONSUMED for d in denials)
    persisted = await store.get(record.approval_id)
    assert persisted == receipts[0]
    assert await _scalar(sessions, "SELECT count(*) FROM approvals") == 1


@pytest.mark.anyio
async def test_concurrent_race_leaves_exactly_one_effect_row(
    store: ApprovalStore, sessions, clean_approvals, effect_counter
) -> None:
    """The authorized effect (counter insert) happens once despite racing
    consumers that all believe they may run it after their consume call."""
    record = await _create(store)

    async def authorized_effect(index: int) -> str:
        try:
            receipt = await store.consume(
                record.approval_id,
                **_binding(record, consumed_by=f"effect-{index}"),
            )
        except ApprovalError:
            return "denied"
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    f"INSERT INTO {_EFFECT_TABLE} (n, consumed_by) "
                    "VALUES (1, :p)"
                ),
                {"p": receipt.consumed_by},
            )
        return "consumed"

    outcomes = await asyncio.gather(*(authorized_effect(i) for i in range(8)))
    assert outcomes.count("consumed") == 1
    rows = await _effect_rows(sessions)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Registered gate scenario — real processes, real Postgres (APPROVAL-PAYLOAD)
# ---------------------------------------------------------------------------

_SCENARIO_CHILD = """\
import asyncio
import json
import os
import sys
from uuid import UUID

from sqlalchemy import text

from mai.database import create_app_engine
from mai.policy.approvals import ApprovalError, ApprovalStore


async def main() -> int:
    phase = sys.argv[1]
    engine, sessions = create_app_engine(os.environ["MAI_DATABASE_URL"])
    store = ApprovalStore(sessions)
    try:
        approval_id = UUID(os.environ["MAI_TEST_APPROVAL_ID"])
        if phase == "read":
            record = await store.get(approval_id)
            if record is None:
                print(json.dumps({"kind": "absent"}))
                return 3
            print(json.dumps(
                {"kind": "state", "status": record.status,
                 "consumed_by": record.consumed_by}
            ))
            return 0
        binding = {
            "capability_id": os.environ["MAI_TEST_CAPABILITY"],
            "server_id": UUID(os.environ["MAI_TEST_SERVER_ID"]),
            "payload_hash": os.environ["MAI_TEST_PAYLOAD_HASH"],
            "policy_revision": int(os.environ["MAI_TEST_REVISION"]),
            "consumed_by": os.environ["MAI_TEST_PRINCIPAL"],
        }
        try:
            record = await store.consume(approval_id, **binding)
        except ApprovalError as exc:
            print(json.dumps({"kind": "denied", "code": exc.code.value}))
            return 0
        # The authorized privileged effect runs exactly once, here.
        async with sessions() as session, session.begin():
            await session.execute(
                text("INSERT INTO approval_effect_counter (n, consumed_by) "
                     "VALUES (1, :p)"),
                {"p": record.consumed_by},
            )
        print(json.dumps(
            {"kind": "consumed", "consumed_by": record.consumed_by}
        ))
        return 0
    finally:
        await engine.dispose()


raise SystemExit(asyncio.run(main()))
"""


@pytest.mark.anyio
async def test_gate_scenario_two_processes_race_one_consumption_changed_payload_denied_restart_preserves_state(
    store: ApprovalStore,
    sessions,
    migrated_url: str,
    clean_approvals,
    effect_counter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """APPROVAL-PAYLOAD + brief gate scenario with REAL OS processes.

    1. Altered-payload consume in a fresh process → denied, decision stays
       spendable, effect counter unchanged.
    2. Two fresh processes race to consume in real Postgres → exactly one
       authorized consumption, exactly one effect row.
    3. Replay in a fresh process → already_consumed, counter unchanged.
    4. Fresh process (restart) reads the decision: consumed state preserved.
    """
    request = _request()
    record = await store.create(request)
    base_env = {
        **os.environ,
        "MAI_DATABASE_URL": migrated_url,
        "MAI_TEST_APPROVAL_ID": str(record.approval_id),
        "MAI_TEST_CAPABILITY": request.capability_id,
        "MAI_TEST_SERVER_ID": str(request.server_id),
        "MAI_TEST_REVISION": str(request.policy_revision),
    }

    def child(phase: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", _SCENARIO_CHILD, phase],
            capture_output=True,
            text=True,
            cwd=BACKEND_ROOT,
            env={**base_env, **env},
            timeout=120,
            check=False,
        )

    # 1. Changed payload denied by a real process; decision survives.
    changed = child(
        "consume",
        MAI_TEST_PAYLOAD_HASH=ALTERED_PAYLOAD_HASH,
        MAI_TEST_PRINCIPAL=OTHER_PRINCIPAL,
    )
    assert changed.returncode == 0, changed.stderr
    assert '"kind": "denied", "code": "payload_mismatch"' in changed.stdout
    assert await _effect_rows(sessions) == []
    assert (await store.get(record.approval_id)).status == "pending"  # type: ignore[union-attr]

    # 2. The race: two fresh interpreters, both presented the exact binding.
    racing = [
        subprocess.Popen(
            [sys.executable, "-c", _SCENARIO_CHILD, "consume"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=BACKEND_ROOT,
            env={
                **base_env,
                "MAI_TEST_PAYLOAD_HASH": request.payload_hash,
                "MAI_TEST_PRINCIPAL": f"racer-{index}",
            },
        )
        for index in (1, 2)
    ]
    outputs = [proc.communicate(timeout=120) for proc in racing]
    verdicts = [stdout for stdout, _ in outputs]
    for proc, (stdout, stderr) in zip(racing, outputs):
        assert proc.returncode == 0, stderr
    consumed = [v for v in verdicts if '"kind": "consumed"' in v]
    denied = [v for v in verdicts if '"code": "already_consumed"' in v]
    assert len(consumed) == 1 and len(denied) == 1, verdicts

    rows = await _effect_rows(sessions)
    assert len(rows) == 1
    winner_principal = consumed[0].split('"consumed_by": "')[1].split('"')[0]
    assert rows == [(1, winner_principal)]

    # 3. Replay by a third fresh process: denied, counter unchanged.
    replay = child(
        "consume",
        MAI_TEST_PAYLOAD_HASH=request.payload_hash,
        MAI_TEST_PRINCIPAL="replayer",
    )
    assert replay.returncode == 0, replay.stderr
    assert '"kind": "denied", "code": "already_consumed"' in replay.stdout
    assert await _effect_rows(sessions) == rows

    # 4. Process restart preserves the decision state (durable, not memory).
    reader = child("read")
    assert reader.returncode == 0, reader.stderr
    assert '"kind": "state", "status": "consumed"' in reader.stdout
    assert f'"consumed_by": "{winner_principal}"' in reader.stdout
