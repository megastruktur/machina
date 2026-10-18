"""Focused tests for the bounded observation + notification schema (card 10-observation-schema).

Acceptance (BRIEF):

1. Timestamp/server/source/staleness metadata, finding/event dedup and
   retention bounds are encoded in the schema: ``server_observations``
   carries the frozen C10 rule-4 ``method``/``quality`` vocabulary plus both
   timestamps (``observed_at`` sample time, ``collected_at`` write time) and
   the collector-declared ``fresh_until`` staleness horizon (derivation is
   shared via ``mai.contracts.observations.derive_freshness`` — never a
   stored flag); ``observation_findings`` dedups on UNIQUE
   ``(server_id, kind, dedup_key)``; ``notification_alerts`` dedups on
   UNIQUE ``(server_id, dedup_key)`` (C10 rule 3); every row of all three
   tables carries a NOT NULL ``retain_until`` bound and every text column is
   ``char_length``-bounded.
2. No raw credential dumps (no JSONB payload column, no credential-shaped
   column, every text bounded) and sequence ownership independent of LLM
   jobs (the schema-owned ``server_observations_seq`` stamps ingestion
   order; inserts never touch job/step/event rows or DBOS objects).

Boundary discipline (test policy, risk-critical slice): every storage claim
runs against a real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``)
with the real linear Alembic chain applied — dedup/staleness/retention
claims are proven by real integrity violations and real sweep deletions,
the two-server claim by a real storage-boundary query across two server
IDs, and the chain claim by a real prior-head upgrade plus a full
downgrade/upgrade round trip. Fixtures carry only fixed opaque UUIDs and
synthetic text — never secret values. The 05/07/08 chain fixtures stay
untouched: this card adds its own auxiliary experiment databases
(``mai_fxt_10_prior``, ``mai_fxt_10_down``).
"""

from __future__ import annotations

import importlib.util
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from mai.contracts.observations import (
    DEDUP_KEY_MAX_CHARS,
    DETAIL_MAX_CHARS,
    KIND_MAX_CHARS,
    SOURCE_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    TITLE_MAX_CHARS,
    AlertOrigin,
    DeliveryStatus,
    FindingSeverity,
    ObservationFreshness,
    ObservationMethod,
    ObservationQuality,
    derive_freshness,
    validate_alert_shape,
)
from mai.database import create_app_engine

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Linear migration chain (exclusive migration-chain resource). Head
# assertions target the chain TIP; the tuple tracks the full chain.
# Chain-tip reconciliation (card 10-observation-schema, exclusive
# migration-chain owner): revision 0007 appends the bounded observation +
# notification persistence; the tip constants are advanced here so this
# file's head assertions stay true.
REVISION_IDS = ("0001", "0002", "0003", "0004", "0005", "0006", "0007")

# The exact public application schema after 0007 — the 0006 set plus the
# three observation-domain tables (10-observation-schema); nothing else, in
# particular no engine-shadowing run-state table and no ``dbos`` schema
# before engine launch.
EXPECTED_APP_TABLES = frozenset(
    {
        "admin_credential",
        "admin_sessions",
        "alembic_version",
        "approvals",
        "profile_projection",
        "profile_projection_journal",
        "job_runs",
        "job_steps",
        "job_events",
        "job_schedules",
        "job_schedule_fires",
        "knowledge_documents",
        "knowledge_doc_chunks",
        "application_projection",
        "application_projection_journal",
        "server_observations",
        "observation_findings",
        "notification_alerts",
    }
)

# Frozen fixed fixture identities (canonical lowercase UUID strings).
SERVER_A = "a10b8c5e-1d2f-4a3b-9c4d-0e5f6a7b8c9d"
SERVER_B = "b20c9d6f-2e3a-4b4c-8d5e-1f0a9b8c7d6e"
APP_ID = "c30dae70-3f4b-4c5d-9e6f-2a1b0c9d8e7f"
OBS_A1 = "d41bf581-4a5c-4d6e-af70-3b2c1d0e9f8a"
OBS_A2 = "e52c0692-5b6d-4e7f-b081-4c3d2e1f0a9b"
OBS_B1 = "f63d17a3-6c7e-4f80-c192-5d4e3f2a1b0c"
FINDING_A = "074e28b4-7d8f-4a91-d2a3-6e5f4a3b2c1d"
ALERT_A = "185f39c5-8e90-4ba2-e3b4-7f6a5b4c3d2e"

RUN_ID = "39481f2a-5b6c-4d7e-8f90-a1b2c3d4e5f6"

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
FRESH_HORIZON = NOW + timedelta(hours=1)
STALE_HORIZON = NOW - timedelta(minutes=5)
# Retention/freshness horizons that must hold against the REAL database
# clock are computed relative to it: the fixture is disposable but runs at
# an arbitrary wall-clock time, so fixed past/future stamps would flake.
RETAIN_FUTURE = datetime.now(timezone.utc) + timedelta(days=30)
RETAIN_PAST = datetime.now(timezone.utc) - timedelta(days=1)


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _database_url(url: str, database: str) -> str:
    """Same server, different database (for isolated chain experiments)."""
    return make_url(url).set(database=database).render_as_string(hide_password=False)


def _drop_app_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        # Full app set at the chain tip — the 10-observation-schema tables
        # are listed here (chain-tip reconciliation) so the zero-to-head
        # fixture stays order-independent after revision 0007.
        conn.execute(
            "DROP TABLE IF EXISTS notification_alerts, observation_findings, "
            "server_observations, application_projection_journal, "
            "application_projection, knowledge_doc_chunks, knowledge_documents, "
            "job_schedule_fires, job_schedules, "
            "job_events, job_steps, job_runs, approvals, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, alembic_version"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``0007``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the schema claims need a real "
            "disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_app_tables(url)
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
    # Each data test starts from an empty observation domain (session-scoped
    # fixture database): delete children before parents; the job_runs row
    # used by the alert FK test goes last.
    async with factory() as session:
        await session.execute(text("DELETE FROM notification_alerts"))
        await session.execute(text("DELETE FROM observation_findings"))
        await session.execute(text("DELETE FROM server_observations"))
        await session.execute(text("DELETE FROM job_runs"))
        await session.commit()
    await engine.dispose()


async def _exec(sessions, sql: str, **params: object) -> None:
    async with sessions() as session:
        await session.execute(text(sql), params)
        await session.commit()


async def _fetchone(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).first()


async def _fetchall(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).all()


async def _insert_observation(
    sessions,
    observation_id: str,
    *,
    server_id: str = SERVER_A,
    app_id: str | None = None,
    source: str = "system",
    method: str = "command",
    quality: str = "measured",
    metric: str | None = "disk.used_pct",
    value: str | None = "37.5",
    observed_at: datetime | None = NOW,
    fresh_until: datetime | None = FRESH_HORIZON,
    retain_until: datetime = RETAIN_FUTURE,
    seq: int | None = None,
) -> int:
    """Insert one observation row; returns its stamped ``seq``."""
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO server_observations
                        (observation_id, server_id, application_id, source,
                         method, quality, metric, value, observed_at,
                         fresh_until, retain_until, seq)
                    VALUES
                        (:oid, :sid, :aid, :source, :method, :quality,
                         :metric, :value, :observed_at, :fresh_until,
                         :retain_until,
                         COALESCE(:seq, nextval('server_observations_seq')))
                    RETURNING seq
                    """
                ),
                {
                    "oid": uuid.UUID(observation_id),
                    "sid": uuid.UUID(server_id),
                    "aid": uuid.UUID(app_id) if app_id else None,
                    "source": source,
                    "method": method,
                    "quality": quality,
                    "metric": metric,
                    "value": value,
                    "observed_at": observed_at,
                    "fresh_until": fresh_until,
                    "retain_until": retain_until,
                    "seq": seq,
                },
            )
        ).scalar()
        await session.commit()
    return int(row)


async def _insert_finding(
    sessions,
    finding_id: str = FINDING_A,
    *,
    server_id: str = SERVER_A,
    observation_id: str | None = None,
    kind: str = "update.available",
    dedup_key: str = "compose|watchtower|v2.3",
    severity: str = "warning",
    summary: str = "watchtower 2.4 published upstream",
    detail: str | None = "pin the adopted manifest to the new tag",
    resolved_at: datetime | None = None,
    retain_until: datetime = RETAIN_FUTURE,
) -> None:
    await _exec(
        sessions,
        """
        INSERT INTO observation_findings
            (finding_id, server_id, application_id, observation_id, kind,
             dedup_key, severity, summary, detail, resolved_at, retain_until)
        VALUES
            (:fid, :sid, :aid, :oid, :kind, :dedup, :severity, :summary,
             :detail, :resolved_at, :retain_until)
        """,
        fid=uuid.UUID(finding_id),
        sid=uuid.UUID(server_id),
        aid=None,
        oid=uuid.UUID(observation_id) if observation_id else None,
        kind=kind,
        dedup=dedup_key,
        severity=severity,
        summary=summary,
        detail=detail,
        resolved_at=resolved_at,
        retain_until=retain_until,
    )


async def _insert_job_run(sessions, run_id: str = RUN_ID) -> None:
    await _exec(
        sessions,
        """
        INSERT INTO job_runs
            (run_id, server_id, depth, profile_revision,
             prompt_revision, model_profile, requested_grants)
        VALUES (:rid, :sid, 0, 1, 1, 'probe-agent', '["shell.exec"]')
        """,
        rid=uuid.UUID(run_id),
        sid=uuid.UUID(SERVER_A),
    )


async def _insert_alert(
    sessions,
    alert_id: str = ALERT_A,
    *,
    server_id: str = SERVER_A,
    origin: str = "finding",
    run_id: str | None = None,
    finding_id: str | None = FINDING_A,
    dedup_key: str = "finding|update.available|compose|watchtower|v2.3",
    title: str = "Update available: watchtower 2.4",
    delivery_status: str = "pending",
    delivered_at: datetime | None = None,
    acknowledged_at: datetime | None = None,
    retain_until: datetime = RETAIN_FUTURE,
) -> None:
    await _exec(
        sessions,
        """
        INSERT INTO notification_alerts
            (alert_id, server_id, origin, run_id, finding_id, dedup_key,
             title, delivery_status, delivered_at, acknowledged_at,
             retain_until)
        VALUES
            (:aid, :sid, :origin, :rid, :fid, :dedup, :title,
             :status, :delivered_at, :acknowledged_at, :retain_until)
        """,
        aid=uuid.UUID(alert_id),
        sid=uuid.UUID(server_id),
        origin=origin,
        rid=uuid.UUID(run_id) if run_id else None,
        fid=uuid.UUID(finding_id) if finding_id else None,
        dedup=dedup_key,
        title=title,
        status=delivery_status,
        delivered_at=delivered_at,
        acknowledged_at=acknowledged_at,
        retain_until=retain_until,
    )


def _migration_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0007",
        os.path.join(BACKEND_ROOT, "migrations", "versions", "0007_observations.py"),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


async def _constraint_def(sessions, name: str) -> str | None:
    row = await _fetchone(
        sessions,
        """
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conname = :name
        """,
        name=name,
    )
    return None if row is None else str(row[0])


def _literals_of(constraint: str | None) -> list[str]:
    if constraint is None:
        return []
    return re.findall(r"'([^']*)'", constraint)


# ---------------------------------------------------------------------------
# AC1 — timestamp/server/source/staleness metadata
# ---------------------------------------------------------------------------


class TestObservationMetadata:
    @pytest.mark.anyio
    async def test_observation_roundtrips_full_metadata(
        self, sessions
    ) -> None:
        stamped = await _insert_observation(sessions, OBS_A1, app_id=APP_ID)
        row = await _fetchone(
            sessions,
            """
            SELECT observation_id, server_id, application_id, source, method,
                   quality, metric, value, observed_at, fresh_until,
                   retain_until, collected_at IS NOT NULL
            FROM server_observations WHERE observation_id = :oid
            """,
            oid=uuid.UUID(OBS_A1),
        )
        assert row == (
            uuid.UUID(OBS_A1),
            uuid.UUID(SERVER_A),
            uuid.UUID(APP_ID),
            "system",
            "command",
            "measured",
            "disk.used_pct",
            "37.5",
            NOW,
            FRESH_HORIZON,
            RETAIN_FUTURE,
            True,
        )
        assert stamped >= 1

    @pytest.mark.anyio
    async def test_source_is_bounded_not_free_payload(self, sessions) -> None:
        await _insert_observation(
            sessions, OBS_A1, source="x" * SOURCE_MAX_CHARS
        )
        with pytest.raises(IntegrityError):
            await _insert_observation(
                sessions, OBS_A2, source="x" * (SOURCE_MAX_CHARS + 1)
            )

    @pytest.mark.anyio
    async def test_empty_source_rejected(self, sessions) -> None:
        with pytest.raises(IntegrityError):
            await _insert_observation(sessions, OBS_A1, source="")

    @pytest.mark.anyio
    @pytest.mark.parametrize("column", ["method", "quality"])
    async def test_c10_rule4_vocabularies_reject_unknown_values(
        self, sessions, column: str
    ) -> None:
        with pytest.raises(IntegrityError):
            await _insert_observation(
                sessions, OBS_A1, **{column: "not-a-frozen-value"}
            )

    @pytest.mark.anyio
    async def test_unknown_quality_carries_no_measurement(self, sessions) -> None:
        # 10-system rule: an unavailable target yields unknown, never a zero
        # reading — the CHECK forbids metric/value/observed_at on unknown.
        with pytest.raises(IntegrityError):
            await _insert_observation(
                sessions, OBS_A1, quality="unknown", metric=None, value=None
            )  # observed_at still set: forbidden pairing
        await _insert_observation(
            sessions,
            OBS_A1,
            quality="unknown",
            metric=None,
            value=None,
            observed_at=None,
            fresh_until=None,
        )
        row = await _fetchone(
            sessions,
            "SELECT metric, value, observed_at FROM server_observations "
            "WHERE observation_id = :oid",
            oid=uuid.UUID(OBS_A1),
        )
        assert row == (None, None, None)

    @pytest.mark.anyio
    async def test_metric_and_value_are_a_paired_column(self, sessions) -> None:
        with pytest.raises(IntegrityError):
            await _insert_observation(
                sessions, OBS_A1, metric="disk.used_pct", value=None
            )
        with pytest.raises(IntegrityError):
            await _insert_observation(
                sessions, OBS_A1, metric=None, value="37.5"
            )

    @pytest.mark.anyio
    async def test_two_server_ids_are_a_storage_boundary(
        self, sessions
    ) -> None:
        # CROSS-SERVER fixture obligation: the freshness query over two
        # server IDs returns each server's rows only; server A's fresh
        # sample never leaks into server B's result set (C01 rule 7).
        # Horizons are relative to the real clock (staleness is derived
        # against the consumer's clock by design).
        clock = datetime.now(timezone.utc)
        fresh = clock + timedelta(hours=1)
        stale = clock - timedelta(minutes=5)
        await _insert_observation(
            sessions, OBS_A1, fresh_until=fresh
        )
        await _insert_observation(
            sessions,
            OBS_A2,
            server_id=SERVER_A,
            fresh_until=stale,
            observed_at=clock - timedelta(hours=2),
        )
        await _insert_observation(
            sessions,
            OBS_B1,
            server_id=SERVER_B,
            quality="unknown",
            metric=None,
            value=None,
            observed_at=None,
            fresh_until=None,
        )
        rows = await _fetchall(
            sessions,
            """
            SELECT server_id, observation_id,
                   (fresh_until IS NOT NULL AND fresh_until < now()) AS stale,
                   (fresh_until IS NULL AND quality = 'unknown') AS unknown
            FROM server_observations
            WHERE server_id = ANY(:ids)
              AND (
                    fresh_until IS NULL
                    OR fresh_until < :now
                  )
            ORDER BY server_id, observation_id
            """,
            ids=[uuid.UUID(SERVER_A), uuid.UUID(SERVER_B)],
            now=clock,
        )
        assert [(str(r[0]), str(r[1]), r[2], r[3]) for r in rows] == [
            (SERVER_A, OBS_A2, True, False),  # stale sample, timestamp shown
            (SERVER_B, OBS_B1, False, True),  # unknown sample, no timestamp
        ]
        # OBS_A1 (fresh) is NOT in the stale/unknown result set.
        assert all(str(r[1]) != OBS_A1 for r in rows)


class TestFreshnessDerivation:
    def test_measured_within_horizon_is_fresh(self) -> None:
        assert (
            derive_freshness("measured", NOW, FRESH_HORIZON, NOW)
            is ObservationFreshness.FRESH
        )

    def test_measured_past_horizon_is_stale(self) -> None:
        assert (
            derive_freshness("measured", NOW, STALE_HORIZON, NOW)
            is ObservationFreshness.STALE
        )

    def test_unknown_quality_or_missing_sample_is_unknown(self) -> None:
        for quality, observed_at, fresh_until in (
            ("unknown", None, None),
            ("measured", None, FRESH_HORIZON),
            ("measured", NOW, None),
            ("degraded", None, None),
        ):
            assert (
                derive_freshness(quality, observed_at, fresh_until, NOW)
                is ObservationFreshness.UNKNOWN
            )

    def test_naive_clock_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            derive_freshness("measured", NOW, FRESH_HORIZON, NOW.replace(tzinfo=None))


# ---------------------------------------------------------------------------
# AC2 — sequence ownership independent of LLM jobs
# ---------------------------------------------------------------------------


class TestSequenceOwnership:
    @pytest.mark.anyio
    async def test_seq_is_stamped_by_the_schema_owned_sequence(
        self, sessions
    ) -> None:
        first = await _insert_observation(sessions, OBS_A1)
        second = await _insert_observation(sessions, OBS_A2)
        assert second == first + 1
        row = await _fetchone(sessions, "SELECT last_value FROM server_observations_seq")
        assert row is not None and row[0] >= second

    @pytest.mark.anyio
    async def test_seq_uniqueness_is_a_real_violation(self, sessions) -> None:
        stamped = await _insert_observation(sessions, OBS_A1)
        with pytest.raises(IntegrityError):
            await _insert_observation(sessions, OBS_A2, seq=stamped)

    @pytest.mark.anyio
    async def test_observation_inserts_touch_no_job_rows(self, sessions) -> None:
        await _insert_job_run(sessions)  # baseline job-domain row
        counts_before = await _fetchone(
            sessions,
            """
            SELECT
              (SELECT count(*) FROM job_runs),
              (SELECT count(*) FROM job_steps),
              (SELECT count(*) FROM job_events)
            """,
        )
        await _insert_observation(sessions, OBS_A1)
        await _insert_observation(sessions, OBS_A2)
        counts_after = await _fetchone(
            sessions,
            """
            SELECT
              (SELECT count(*) FROM job_runs),
              (SELECT count(*) FROM job_steps),
              (SELECT count(*) FROM job_events)
            """,
        )
        assert counts_before == counts_after == (1, 0, 0)

    def test_sequence_object_is_migration_owned_not_engine_owned(
        self, migrated_url: str
    ) -> None:
        with psycopg.connect(migrated_url) as conn:
            row = conn.execute(
                """
                SELECT count(*) FROM information_schema.sequences
                WHERE sequence_schema = 'public'
                  AND sequence_name = 'server_observations_seq'
                """
            ).fetchone()
            schemata = {
                r[0]
                for r in conn.execute(
                    "SELECT schema_name FROM information_schema.schemata"
                )
            }
        assert row == (1,)  # owned by THIS chain, dropped again on downgrade
        assert "dbos" not in schemata  # engine-owned schema absent pre-launch


# ---------------------------------------------------------------------------
# AC1 — finding dedup (UNIQUE server_id, kind, dedup_key)
# ---------------------------------------------------------------------------


class TestFindingDedup:
    @pytest.mark.anyio
    async def test_duplicate_finding_key_rejected(self, sessions) -> None:
        await _insert_finding(sessions)
        # Re-observing the same condition collapses onto one finding: the
        # duplicate is a real integrity violation, not a second row.
        with pytest.raises(IntegrityError):
            await _insert_finding(
                sessions, finding_id=str(uuid.uuid4()), kind="update.available"
            )

    @pytest.mark.anyio
    async def test_same_key_on_other_server_is_not_a_collision(
        self, sessions
    ) -> None:
        await _insert_finding(sessions, server_id=SERVER_A)
        await _insert_finding(
            sessions,
            finding_id=str(uuid.uuid4()),
            server_id=SERVER_B,
            observation_id=None,
        )
        counts = await _fetchone(
            sessions, "SELECT count(*) FROM observation_findings"
        )
        assert counts == (2,)

    @pytest.mark.anyio
    async def test_same_key_other_kind_is_distinct(self, sessions) -> None:
        await _insert_finding(
            sessions, kind="security.baseline", dedup_key="ssh|root-login"
        )
        await _insert_finding(
            sessions,
            finding_id=str(uuid.uuid4()),
            kind="threshold",
            dedup_key="ssh|root-login",
        )
        counts = await _fetchone(
            sessions, "SELECT count(*) FROM observation_findings"
        )
        assert counts == (2,)

    @pytest.mark.anyio
    async def test_severity_vocabulary_rejects_unknown(self, sessions) -> None:
        with pytest.raises(IntegrityError):
            await _insert_finding(sessions, severity="catastrophic")

    @pytest.mark.anyio
    async def test_resolved_not_before_opened(self, sessions) -> None:
        with pytest.raises(IntegrityError):
            await _insert_finding(
                sessions,
                resolved_at=NOW - timedelta(hours=1),
            )

    @pytest.mark.anyio
    async def test_observation_reference_is_a_real_fk_and_survives_sweep(
        self, sessions
    ) -> None:
        await _insert_observation(sessions, OBS_A1)
        await _insert_finding(sessions, observation_id=OBS_A1)
        # The FK is a real boundary: a dangling observation id violates.
        with pytest.raises(IntegrityError):
            await _exec(
                sessions,
                "INSERT INTO observation_findings (finding_id, server_id, "
                "observation_id, kind, dedup_key, severity, summary, "
                "retain_until) VALUES (:fid, :sid, :oid, 'k', 'd', 'info', "
                "'s', :rt)",
                fid=uuid.uuid4(),
                sid=uuid.UUID(SERVER_A),
                oid=uuid.UUID(OBS_A2),  # no such observation
                rt=RETAIN_FUTURE,
            )
        # Retention sweep of the aged observation keeps the finding alive
        # with the reference severed (ON DELETE SET NULL).
        await _exec(
            sessions,
            "DELETE FROM server_observations WHERE observation_id = :oid",
            oid=uuid.UUID(OBS_A1),
        )
        row = await _fetchone(
            sessions,
            "SELECT observation_id FROM observation_findings "
            "WHERE finding_id = :fid",
            fid=uuid.UUID(FINDING_A),
        )
        assert row == (None,)


class TestAlertCreationShape:
    """Frozen CREATION shape (contract validator; storage severs legally)."""

    def test_finding_notice_requires_finding_id(self) -> None:
        with pytest.raises(ValueError, match="finding_id"):
            validate_alert_shape(AlertOrigin.FINDING, run_id=None, finding_id=None)

    def test_job_result_notice_requires_run_id(self) -> None:
        with pytest.raises(ValueError, match="run_id"):
            validate_alert_shape(
                AlertOrigin.JOB_RESULT, run_id=None, finding_id=None
            )

    def test_valid_shapes_pass(self) -> None:
        finding_id = uuid.uuid4()
        run_id = uuid.uuid4()
        validate_alert_shape(AlertOrigin.FINDING, run_id=None, finding_id=finding_id)
        validate_alert_shape(
            AlertOrigin.JOB_RESULT, run_id=run_id, finding_id=None
        )
        validate_alert_shape("finding", None, finding_id)
        validate_alert_shape("job_result", run_id, None)


# ---------------------------------------------------------------------------
# C10 rule 3 — notification alerts: delivery, dedup key, severance
# ---------------------------------------------------------------------------


class TestNotificationAlerts:
    @pytest.mark.anyio
    async def test_duplicate_notification_key_rejected(self, sessions) -> None:
        # The feature-gate scenario control: a repeated poll of the same
        # condition must be a REAL integrity violation on the dedup key.
        await _insert_finding(sessions)
        await _insert_alert(sessions)
        with pytest.raises(IntegrityError):
            await _insert_alert(sessions, alert_id=str(uuid.uuid4()))

    @pytest.mark.anyio
    async def test_same_key_on_other_server_is_distinct(self, sessions) -> None:
        await _insert_finding(sessions, server_id=SERVER_A)
        await _insert_alert(sessions, server_id=SERVER_A)
        finding_b = str(uuid.uuid4())
        await _insert_finding(sessions, finding_id=finding_b, server_id=SERVER_B)
        await _insert_alert(
            sessions,
            alert_id=str(uuid.uuid4()),
            server_id=SERVER_B,
            finding_id=finding_b,
        )
        counts = await _fetchone(sessions, "SELECT count(*) FROM notification_alerts")
        assert counts == (2,)

    @pytest.mark.anyio
    async def test_job_result_notice_survives_run_deletion(
        self, sessions
    ) -> None:
        # C10 rule 3 + AC2: the notice references the run, but is
        # independent of the LLM job engine — run deletion severs the
        # reference (ON DELETE SET NULL) and keeps the notice itself.
        await _insert_job_run(sessions)
        await _insert_alert(
            sessions,
            origin="job_result",
            finding_id=None,
            run_id=RUN_ID,
            dedup_key="job_result|39481f2a",
            title="Backup run finished",
        )
        await _exec(
            sessions,
            "DELETE FROM job_runs WHERE run_id = :rid",
            rid=uuid.UUID(RUN_ID),
        )
        row = await _fetchone(
            sessions,
            "SELECT run_id, origin, title FROM notification_alerts "
            "WHERE alert_id = :aid",
            aid=uuid.UUID(ALERT_A),
        )
        assert row == (None, "job_result", "Backup run finished")

    @pytest.mark.anyio
    async def test_delivery_stamp_equivalence_is_enforced(self, sessions) -> None:
        await _insert_finding(sessions)
        with pytest.raises(IntegrityError):
            await _insert_alert(
                sessions,
                delivery_status=DeliveryStatus.DELIVERED.value,
                delivered_at=None,
            )
        with pytest.raises(IntegrityError):
            await _insert_alert(
                sessions,
                delivery_status=DeliveryStatus.PENDING.value,
                delivered_at=NOW,
            )

    @pytest.mark.anyio
    async def test_ack_cannot_precede_creation(self, sessions) -> None:
        await _insert_finding(sessions)
        with pytest.raises(IntegrityError):
            await _insert_alert(sessions, acknowledged_at=NOW - timedelta(hours=1))

    @pytest.mark.anyio
    async def test_delivered_and_acked_notice_roundtrips(self, sessions) -> None:
        await _insert_finding(sessions)
        await _insert_alert(
            sessions,
            delivery_status=DeliveryStatus.DELIVERED.value,
            delivered_at=RETAIN_FUTURE,
            acknowledged_at=RETAIN_FUTURE + timedelta(minutes=5),
        )
        row = await _fetchone(
            sessions,
            "SELECT delivery_status, delivered_at, acknowledged_at, context_ref "
            "FROM notification_alerts WHERE alert_id = :aid",
            aid=uuid.UUID(ALERT_A),
        )
        assert row[0] == "delivered" and row[2] is not None

    @pytest.mark.anyio
    async def test_retained_notice_outlives_deleted_finding(
        self, sessions
    ) -> None:
        # Ack does not delete source evidence; the retention sweep deletes
        # the aged finding but the notice survives with a severed reference.
        await _insert_observation(sessions, OBS_A1)
        await _insert_finding(sessions, observation_id=OBS_A1)
        await _insert_alert(sessions, acknowledged_at=RETAIN_FUTURE)
        await _exec(
            sessions,
            "DELETE FROM observation_findings WHERE finding_id = :fid",
            fid=uuid.UUID(FINDING_A),
        )
        row = await _fetchone(
            sessions,
            "SELECT finding_id, acknowledged_at FROM notification_alerts "
            "WHERE alert_id = :aid",
            aid=uuid.UUID(ALERT_A),
        )
        assert row[0] is None and row[1] is not None


# ---------------------------------------------------------------------------
# AC1/AC2 — retention bounds + structural no-dump boundary
# ---------------------------------------------------------------------------


class TestRetentionAndBoundedPersistence:
    @pytest.mark.anyio
    @pytest.mark.parametrize("table", ["server_observations", "observation_findings", "notification_alerts"])
    async def test_retain_until_is_mandatory_everywhere(
        self, sessions, table: str
    ) -> None:
        with pytest.raises(IntegrityError):
            if table == "server_observations":
                await _insert_observation(sessions, OBS_A1, retain_until=None)  # type: ignore[arg-type]
            elif table == "observation_findings":
                await _exec(
                    sessions,
                    "INSERT INTO observation_findings (finding_id, server_id, "
                    "kind, dedup_key, severity, summary, retain_until) VALUES "
                    "(:fid, :sid, 'k', 'd', 'info', 's', NULL)",
                    fid=uuid.uuid4(),
                    sid=uuid.UUID(SERVER_A),
                )
            else:
                await _insert_finding(sessions)  # anchor for the alert CHECK
                await _insert_alert(sessions, retain_until=None)  # type: ignore[arg-type]

    @pytest.mark.anyio
    async def test_retention_sweep_deletes_only_expired_synthetic_observations(
        self, sessions
    ) -> None:
        # The feature-gate scenario control: synthetic observations past
        # their encoded bound are deleted; live rows stay.
        await _insert_observation(sessions, OBS_A1, retain_until=RETAIN_FUTURE)
        await _insert_observation(
            sessions,
            OBS_A2,
            fresh_until=STALE_HORIZON,
            retain_until=RETAIN_PAST,
        )
        await _insert_observation(
            sessions,
            OBS_B1,
            server_id=SERVER_B,
            retain_until=RETAIN_PAST,
        )
        await _insert_finding(sessions, retain_until=RETAIN_PAST)
        await _insert_alert(sessions, retain_until=RETAIN_PAST)
        for table in (
            "notification_alerts",
            "observation_findings",
            "server_observations",
        ):
            await _exec(
                sessions,
                f"DELETE FROM {table} WHERE retain_until < :now",
                now=datetime.now(timezone.utc),
            )
        observations = await _fetchall(
            sessions, "SELECT observation_id FROM server_observations"
        )
        assert [(str(r[0])) for r in observations] == [OBS_A1]
        assert await _fetchone(sessions, "SELECT count(*) FROM observation_findings") == (0,)
        assert await _fetchone(sessions, "SELECT count(*) FROM notification_alerts") == (0,)

    @pytest.mark.anyio
    async def test_text_columns_are_length_bounded(self, sessions) -> None:
        await _insert_finding(sessions, summary="x" * SUMMARY_MAX_CHARS)
        await _insert_finding(
            sessions,
            finding_id=str(uuid.uuid4()),
            detail="x" * DETAIL_MAX_CHARS,
            dedup_key="d1",
        )
        await _insert_alert(sessions, title="x" * TITLE_MAX_CHARS, dedup_key="k1")
        with pytest.raises(IntegrityError):
            await _insert_finding(sessions, summary="x" * (SUMMARY_MAX_CHARS + 1))
        with pytest.raises(IntegrityError):
            await _insert_alert(sessions, title="x" * (TITLE_MAX_CHARS + 1))
        with pytest.raises(IntegrityError):
            await _insert_finding(sessions, detail="x" * (DETAIL_MAX_CHARS + 1))
        with pytest.raises(IntegrityError):
            await _insert_finding(sessions, kind="x" * (KIND_MAX_CHARS + 1))

    def test_no_credential_shaped_column_exists_anywhere(
        self, migrated_url: str
    ) -> None:
        # AC2 structural boundary: the three tables have NO column whose
        # name is credential-shaped, and NO non-bounded payload column
        # (no JSONB at all in the observation domain).
        secretish = re.compile(
            r"(secret|credential|password|passwd|token|api_key|apikey|private)",
            re.IGNORECASE,
        )
        with psycopg.connect(migrated_url) as conn:
            columns = conn.execute(
                """
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name IN ('server_observations',
                                     'observation_findings',
                                     'notification_alerts')
                """
            ).fetchall()
        assert columns, "observation tables missing"
        assert not [c for c in columns if secretish.search(c[1])]
        assert not [c for c in columns if c[2] == "jsonb"]

    def test_every_text_column_is_char_length_bounded(
        self, migrated_url: str
    ) -> None:
        # AC2: the schema structurally cannot store a dump — every text
        # column of the three tables sits under a char_length CHECK.
        with psycopg.connect(migrated_url) as conn:
            text_columns = {
                (r[0], r[1])
                for r in conn.execute(
                    """
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND data_type = 'text'
                      AND table_name IN ('server_observations',
                                         'observation_findings',
                                         'notification_alerts')
                    """
                )
            }
            constraints = {
                r[0]: str(r[1])
                for r in conn.execute(
                    """
                    SELECT conname, pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE contype = 'c'
                      AND conrelid IN (
                        'server_observations'::regclass,
                        'observation_findings'::regclass,
                        'notification_alerts'::regclass)
                    """
                )
            }
        bounded: set[str] = set()
        vocab_pinned: set[str] = set()
        for definition in constraints.values():
            bounded.update(re.findall(r"char_length\((\w+)\)", definition))
            # Enum vocabulary columns are inherently bounded: the CHECK is
            # an = ANY (ARRAY[...]) membership test over a finite set.
            vocab_pinned.update(re.findall(r"(\w+) = ANY", definition))
        unbounded = {column for _, column in text_columns} - bounded - vocab_pinned
        assert not unbounded, f"text columns without a bound: {sorted(unbounded)}"

    def test_live_bounds_match_the_contract_constants(
        self, migrated_url: str
    ) -> None:
        from mai.contracts.observations import (
            CONTEXT_REF_MAX_CHARS,
            METRIC_MAX_CHARS,
            METRIC_VALUE_MAX_CHARS,
        )

        expected = {
            "ck_server_observations_source": SOURCE_MAX_CHARS,
            "ck_server_observations_metric": METRIC_MAX_CHARS,
            "ck_server_observations_value": METRIC_VALUE_MAX_CHARS,
            "ck_observation_findings_kind": KIND_MAX_CHARS,
            "ck_observation_findings_dedup_key": DEDUP_KEY_MAX_CHARS,
            "ck_observation_findings_summary": SUMMARY_MAX_CHARS,
            "ck_observation_findings_detail": DETAIL_MAX_CHARS,
            "ck_notification_alerts_dedup_key": DEDUP_KEY_MAX_CHARS,
            "ck_notification_alerts_title": TITLE_MAX_CHARS,
            "ck_notification_alerts_summary": SUMMARY_MAX_CHARS,
            "ck_notification_alerts_context_ref": CONTEXT_REF_MAX_CHARS,
        }
        with psycopg.connect(migrated_url) as conn:
            definitions = {
                r[0]: str(r[1])
                for r in conn.execute(
                    """
                    SELECT conname, pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE contype = 'c'
                      AND conrelid IN (
                        'server_observations'::regclass,
                        'observation_findings'::regclass,
                        'notification_alerts'::regclass)
                    """
                )
            }
        for name, bound in expected.items():
            definition = definitions.get(name)
            assert definition is not None, f"missing constraint {name}"
            match = re.search(r"char_length\(\w+\) <= (\d+)", definition)
            lower = re.search(r"char_length\(\w+\) >= 1", definition)
            assert (
                match and lower
            ), f"{name} is not a 1..N char_length bound: {definition}"
            assert int(match.group(1)) == bound, f"{name} drifted from the contract"

    def test_migration_vocabularies_mirror_the_contract_module(
        self, migrated_url: str
    ) -> None:
        module = _migration_module()
        with psycopg.connect(migrated_url) as conn:
            definitions = {
                r[0]: str(r[1])
                for r in conn.execute(
                    """
                    SELECT conname, pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE contype = 'c'
                      AND conrelid IN (
                        'server_observations'::regclass,
                        'observation_findings'::regclass,
                        'notification_alerts'::regclass)
                    """
                )
            }
        assert set(_literals_of(definitions.get("ck_server_observations_method"))) == {
            m.value for m in ObservationMethod
        }
        assert set(_literals_of(definitions.get("ck_server_observations_quality"))) == {
            q.value for q in ObservationQuality
        }
        assert set(_literals_of(definitions.get("ck_observation_findings_severity"))) == {
            s.value for s in FindingSeverity
        }
        assert set(_literals_of(definitions.get("ck_notification_alerts_origin"))) == {
            o.value for o in AlertOrigin
        }
        assert set(
            _literals_of(definitions.get("ck_notification_alerts_delivery_status"))
        ) == {d.value for d in DeliveryStatus}
        # The migration module's SQL fragments stay in lockstep too.
        assert module._METHODS_SQL == "('command', 'derived', 'job_receipt')"
        assert module._QUALITIES_SQL == "('measured', 'degraded', 'unknown')"
        assert module._SEVERITIES_SQL == "('info', 'warning', 'critical')"
        assert module._ALERT_ORIGINS_SQL == "('finding', 'job_result')"
        assert module._DELIVERY_STATUSES_SQL == "('pending', 'delivered', 'failed')"


# ---------------------------------------------------------------------------
# Chain position, prior-head preservation, downgrade/upgrade round trip,
# and the single-database/no-surprise-store boundary
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


@pytest.fixture(scope="session")
def prior_database_url(migrated_url: str) -> str:
    """Fixture database carried from the prior head (``0006``) to ``0007``.

    Feature-gate scenario fragment: upgrading a real database from the
    previous head must preserve prior-domain rows untouched.
    """
    url = migrated_url
    prior_db = "mai_fxt_10_prior"
    prior_url = _database_url(url, prior_db)
    with psycopg.connect(_database_url(url, "postgres"), autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{prior_db}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{prior_db}"')
    previous = os.environ.get("MAI_DATABASE_URL")
    try:
        os.environ["MAI_DATABASE_URL"] = prior_url
        command.upgrade(_alembic_config(), "0006")
        run_id = uuid.uuid4()
        with psycopg.connect(prior_url) as conn:
            conn.execute(
                """
                INSERT INTO job_runs
                    (run_id, server_id, depth, profile_revision,
                     prompt_revision, model_profile, requested_grants)
                VALUES (%s, %s, 0, 1, 1, 'probe-agent', '["shell.exec"]')
                """,
                (run_id, uuid.UUID(SERVER_A)),
            )
            conn.commit()
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    yield prior_url
    with psycopg.connect(_database_url(url, "postgres"), autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{prior_db}" WITH (FORCE)')


def test_upgrade_from_prior_head_preserves_prior_rows(
    prior_database_url: str,
) -> None:
    with psycopg.connect(prior_database_url) as conn:
        run = conn.execute(
            "SELECT count(*) FROM job_runs WHERE server_id = %s",
            (uuid.UUID(SERVER_A),),
        ).fetchone()
        new_tables = conn.execute(
            """
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN ('server_observations',
                                 'observation_findings',
                                 'notification_alerts')
            """
        ).fetchone()
        sequence = conn.execute(
            """
            SELECT count(*) FROM information_schema.sequences
            WHERE sequence_schema = 'public'
              AND sequence_name = 'server_observations_seq'
            """
        ).fetchone()
    assert run == (1,)
    assert new_tables == (3,)
    assert sequence == (1,)


def test_downgrade_upgrades_cleanly(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Down/upgrade round trip of the new revision on an isolated database."""
    down_db = "mai_fxt_10_down"
    down_url = _database_url(migrated_url, down_db)
    with psycopg.connect(
        _database_url(migrated_url, "postgres"), autocommit=True
    ) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{down_db}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{down_db}"')
    previous = os.environ.get("MAI_DATABASE_URL")
    try:
        os.environ["MAI_DATABASE_URL"] = down_url
        command.upgrade(_alembic_config(), "head")
        with psycopg.connect(down_url) as conn:
            conn.execute(
                """
                INSERT INTO server_observations
                    (observation_id, server_id, source, method, quality,
                     metric, value, observed_at, fresh_until, retain_until)
                VALUES (%s, %s, 'system', 'command', 'measured',
                        'disk.used_pct', '37.5', %s, %s, %s)
                """,
                (
                    uuid.UUID(OBS_A1),
                    uuid.UUID(SERVER_A),
                    NOW,
                    FRESH_HORIZON,
                    RETAIN_FUTURE,
                ),
            )
            conn.commit()
        command.downgrade(_alembic_config(), "0006")
        with psycopg.connect(down_url) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            leftovers = conn.execute(
                """
                SELECT count(*) FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('server_observations',
                                     'observation_findings',
                                     'notification_alerts')
                """
            ).fetchone()
            sequence = conn.execute(
                """
                SELECT count(*) FROM information_schema.sequences
                WHERE sequence_schema = 'public'
                  AND sequence_name = 'server_observations_seq'
                """
            ).fetchone()
            prior = conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'job_runs'"
            ).fetchone()
        assert version == ("0006",)
        assert leftovers == (0,)
        assert sequence == (0,)  # the schema-owned sequence goes with the tables
        assert prior == (1,)  # the P05 schema is untouched by our downgrade
        command.upgrade(_alembic_config(), "head")
        with psycopg.connect(down_url) as conn:
            assert conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone() == (REVISION_IDS[-1],)
            roundtrip = conn.execute(
                "SELECT count(*) FROM server_observations WHERE observation_id = %s",
                (uuid.UUID(OBS_A1),),
            ).fetchone()
        assert roundtrip == (0,)  # downgrade removed the data relation cleanly
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
        with psycopg.connect(
            _database_url(migrated_url, "postgres"), autocommit=True
        ) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{down_db}" WITH (FORCE)')


def test_public_schema_is_exactly_the_frozen_app_set(migrated_url: str) -> None:
    """One database, one app schema set: no engine-shadowing tables, no
    ``dbos`` schema before engine launch, nothing beyond the frozen set."""
    with psycopg.connect(migrated_url) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                """
            )
        }
        schemata = {
            row[0]
            for row in conn.execute(
                "SELECT schema_name FROM information_schema.schemata"
            )
        }
    assert tables == EXPECTED_APP_TABLES
    assert "dbos" not in schemata  # engine-owned schema, created at launch
