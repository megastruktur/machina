"""Focused tests for the bounded system health collector (card 10-system, C04 + C10).

Acceptance coverage (BRIEF):

1. **Actual CPU/memory/disk snapshot has source and timestamp with bounded
   target command output** — the fixed probe command (``PROBE_COMMAND``) is
   read-only and self-bounding; the controller hard-caps captured output
   (``MAX_PROBE_OUTPUT_BYTES``); a real fixture snapshot persists as bounded
   per-metric rows carrying ``source='system'``/``method='command'`` plus
   ``observed_at`` (target clock when the target reports one) and the
   collector horizons (``fresh_until``/``retain_until``).
2. **Permission/unavailable target returns stale/unknown, not zero usage;
   process details handled by separate module** — an unavailable/permission-
   refused target records exactly ONE ``quality='unknown'`` row with NO
   metric/value/observed_at (the 0007 schema structurally forbids a zero
   reading); absent sources inside a readable target degrade the sample and
   are ``None``/absent — never fabricated zeros (the real Arch fixture
   demonstrates this with ``systemd_failed_rc=1``: failed units are UNKNOWN,
   not zero); existing findings are never re-evaluated or closed from
   missing data; the probe contains no process listing (sentinel negative
   control on the real target).

Layers per the test policy: pure parse/finding tests (always run);
transport-boundary tests over monkeypatched subprocess; persistence claims
on a real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``, real
Alembic chain to ``0007``); ``real_target``-marked tests against a disposable
Arch sshd container (``mai-fxt-10-system-arch`` namespace, mirroring the
reviewed 04-ssh-connect fixture), auto-skipped without docker and removed
with recorded proof.

Secret hygiene: fixture keys are throwaway files under ``/tmp/mai-fxt-10-
system-*``; the ``.env`` holds a PATH reference, never material; the
non-disclosure sentinel is a synthetic marker string, not a secret.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from mai.contracts.observations import (
    SOURCE_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    ObservationFreshness,
    ObservationQuality,
    derive_freshness,
)
from mai.contracts.server import SshIdentity
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.execution.ssh import (
    SshConnectError,
    SshErrorCode,
    pin_host_key,
    resolve_identity_key,
    ssh_arguments,
)
from mai.monitoring.system import (
    DISK_CRIT_USED_PCT,
    DISK_WARN_USED_PCT,
    MAX_FAILED_UNITS_LISTED,
    MAX_PROBE_OUTPUT_BYTES,
    MEM_CRIT_AVAIL_PCT,
    MEM_WARN_AVAIL_PCT,
    PROBE_COMMAND,
    PROBE_TIMEOUT_SECONDS,
    RETENTION_HORIZON,
    SOURCE,
    DiskSample,
    SystemSample,
    collect,
    findings_for_sample,
    parse_probe_output,
    probe_system,
    record_sample,
    record_unavailable,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Frozen fixed fixture identities (canonical lowercase UUID strings, distinct
# from every other card's constants).
SERVER_A = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"
SERVER_B = "7e2d3c4b-5a6f-4e70-9b8c-0d1e2f3a4b5c"

NOW = datetime(2026, 10, 17, 18, 0, 0, tzinfo=UTC)

#: Non-disclosure sentinel planted in a FIXTURE PROCESS ARGV on the real
#: target (NOT a secret): no observation row may ever contain it because the
#: system probe has no process-listing path at all.
ARGV_SENTINEL = "mai-10-system-argv-sentinel-not-a-secret"

# Real probe output captured verbatim from the disposable Arch fixture target
# (2026-10-17, mai-fxt-10-system-arch; shared kernel: 32 host cores, host
# overlay root, systemd binaries present but PID 1 is sshd ⇒ the failed-unit
# query fails with rc=1 ⇒ units UNKNOWN). Embedded as the parse fixture so
# the pure tests exercise the exact byte shape the real target emits.
_ARCH_FIXTURE_OUTPUT = (
    "ts=2026-10-17T17:48:27\n"
    "load1=1.35\n"
    "load5=0.85\n"
    "load15=0.79\n"
    "cores=32\n"
    "mem_total_kib=127394948\n"
    "mem_available_kib=49680744\n"
    "disk_total_kib=1949318196\n"
    "disk_avail_kib=548302196\n"
    "disk_used_pct=72%\n"
    "uptime_s=733079.81\n"
    "systemd=yes\n"
    "systemd_failed_rc=1\n"
)


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _drop_app_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        # Full app set at the chain tip (same reconciliation list as
        # 10-observation-schema): order-independent zero-to-head fixture.
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
            f"{LIVE_URL_VAR} is not set; the persistence claims need a real "
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
    async with factory() as session:
        await session.execute(text("DELETE FROM notification_alerts"))
        await session.execute(text("DELETE FROM observation_findings"))
        await session.execute(text("DELETE FROM server_observations"))
        await session.commit()
    await engine.dispose()


async def _fetchone(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).first()


async def _fetchall(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).all()


async def _storage_dump(sessions) -> str:
    """Every stored value of the observation domain, for sentinel audits."""
    parts = []
    for table in ("server_observations", "observation_findings", "notification_alerts"):
        rows = await _fetchall(sessions, f"SELECT * FROM {table}")  # noqa: S608 (fixed names)
        parts.extend(str(tuple(r)) for r in rows)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Sample builders (pure layer)
# ---------------------------------------------------------------------------


def _disk(used_pct: float = 37.0, total_kib: int = 1_000_000) -> DiskSample:
    used_kib = int(total_kib * used_pct / 100.0)
    return DiskSample(
        total_kib=total_kib,
        available_kib=total_kib - used_kib,
        used_pct=used_pct,
    )


def _sample(**overrides: object) -> SystemSample:
    fields: dict[str, object] = {
        "observed_at": NOW,
        "observed_at_source": "target",
        "quality": ObservationQuality.MEASURED,
        "load1": 0.5,
        "load5": 0.4,
        "load15": 0.3,
        "cores": 32,
        "mem_total_kib": 1_000_000,
        "mem_available_kib": 500_000,
        "disk_root": _disk(),
        "uptime_s": 733_079.81,
        "systemd_present": True,
        "systemd_failed_units": (),
    }
    fields.update(overrides)
    return SystemSample(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure parsing: bounded target output → honest sample
# ---------------------------------------------------------------------------


class TestParseProbeOutput:
    def test_real_fixture_capture_parses_measured(self) -> None:
        sample = parse_probe_output(_ARCH_FIXTURE_OUTPUT, controller_now=NOW)
        assert sample.quality is ObservationQuality.MEASURED
        # Target-reported timestamp, controller clock untouched.
        assert sample.observed_at == datetime(2026, 10, 17, 17, 48, 27, tzinfo=UTC)
        assert sample.observed_at_source == "target"
        assert (sample.load1, sample.load5, sample.load15) == (1.35, 0.85, 0.79)
        assert sample.cores == 32
        assert sample.mem_total_kib == 127_394_948
        assert sample.mem_available_kib == 49_680_744
        assert sample.disk_root is not None
        assert sample.disk_root.used_pct == 72.0
        assert sample.disk_root.total_kib == 1_949_318_196
        assert sample.uptime_s == 733_079.81
        # The REAL fixture case: systemd binaries exist but the failed-unit
        # query failed (rc=1, sshd is PID 1) ⇒ UNKNOWN units, never zero.
        assert sample.systemd_present is True
        assert sample.systemd_failed_units is None

    def test_empty_output_is_unknown_without_any_metric(self) -> None:
        sample = parse_probe_output("", controller_now=NOW)
        assert sample.quality is ObservationQuality.UNKNOWN
        assert sample.load1 is None
        assert sample.mem_total_kib is None
        assert sample.disk_root is None
        assert sample.systemd_failed_units is None
        assert sample.observed_at == NOW
        assert sample.observed_at_source == "controller"

    def test_partial_output_is_degraded_with_absent_fields_none(self) -> None:
        output = "load1=0.10\nload5=0.20\nload15=0.30\n"  # no memory, no disk
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.quality is ObservationQuality.DEGRADED
        assert sample.load1 == 0.10
        assert sample.mem_total_kib is None
        assert sample.disk_root is None
        assert sample.cores is None

    def test_unparsable_timestamp_falls_back_to_controller_clock(self) -> None:
        sample = parse_probe_output("ts=nonsense\nload1=0.5\n", controller_now=NOW)
        assert sample.observed_at_source == "controller"
        assert sample.observed_at == NOW
        assert sample.quality is ObservationQuality.DEGRADED  # load only

    def test_junk_values_become_none_never_zero(self) -> None:
        output = (
            "load1=abc\n"
            "load5=NaN\n"
            "load15=inf\n"
            "cores=32x\n"
            "mem_total_kib=-5\n"
            "disk_total_kib=10\n"
            "disk_avail_kib=5\n"
            "disk_used_pct=150%\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.load1 is None
        assert sample.load5 is None  # NaN rejected, not 0.0
        assert sample.load15 is None  # inf rejected
        assert sample.cores is None
        assert sample.mem_total_kib is None  # negative is not a count
        assert sample.disk_root is None  # 150% is not a capacity
        assert sample.quality is ObservationQuality.UNKNOWN  # no core source

    def test_first_occurrence_wins_for_repeated_keys(self) -> None:
        sample = parse_probe_output("load1=0.25\nload1=99.99\n", controller_now=NOW)
        assert sample.load1 == 0.25

    def test_systemd_zero_is_only_zero_when_the_query_succeeded(self) -> None:
        sample = parse_probe_output(
            "systemd=yes\nsystemd_failed_rc=0\n", controller_now=NOW
        )
        assert sample.systemd_present is True
        assert sample.systemd_failed_units == ()

        sample = parse_probe_output(
            "systemd=yes\nsystemd_failed_rc=1\nfailed_unit=liar.service\n",
            controller_now=NOW,
        )
        assert sample.systemd_failed_units is None  # rc≠0 ⇒ listing not trusted

        sample = parse_probe_output("load1=0.1\nsystemd=no\n", controller_now=NOW)
        assert sample.systemd_present is False
        assert sample.systemd_failed_units is None  # absent source ≠ zero

        sample = parse_probe_output("load1=0.1\n", controller_now=NOW)
        assert sample.systemd_present is None
        assert sample.systemd_failed_units is None

    def test_failed_units_parsed_bounded_and_validated(self) -> None:
        lines = ["systemd=yes", "systemd_failed_rc=0"]
        lines += [f"failed_unit=unit{i}.service" for i in range(25)]
        lines += [
            "failed_unit=bad name with spaces",
            "failed_unit=drop$char",
            "failed_unit=",
        ]
        sample = parse_probe_output("\n".join(lines) + "\n", controller_now=NOW)
        assert sample.systemd_failed_units is not None
        assert len(sample.systemd_failed_units) == MAX_FAILED_UNITS_LISTED
        assert sample.systemd_failed_units[0] == "unit0.service"
        assert all(" " not in u and "$" not in u for u in sample.systemd_failed_units)

    def test_unknown_keys_ignored_and_carriage_returns_tolerated(self) -> None:
        output = "future_key=x\r\nload1=0.5\r\nfailed_unit=orphan.service\r\n"
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.load1 == 0.5
        # No systemd marker ⇒ the orphan line is not trusted as units data.
        assert sample.systemd_failed_units is None

    def test_naive_controller_clock_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            parse_probe_output("load1=0.5\n", controller_now=datetime(2026, 10, 17))


# ---------------------------------------------------------------------------
# Pure findings: thresholds, three-valued states, bounded summaries
# ---------------------------------------------------------------------------


class TestFindingsForSample:
    def test_idle_sample_clears_every_condition(self) -> None:
        drafts = findings_for_sample(_sample())
        assert [d.state for d in drafts] == ["clear", "clear", "clear", "clear"]
        assert all(d.severity == "info" for d in drafts)

    def test_disk_thresholds_warning_critical_and_boundaries(self) -> None:
        by_pct = {
            89.9: "clear",
            DISK_WARN_USED_PCT: "active",  # >= warn bound
            94.9: "active",
            DISK_CRIT_USED_PCT: "active",  # >= crit bound wins severity
        }
        for pct, state in by_pct.items():
            draft = findings_for_sample(_sample(disk_root=_disk(pct)))[0]
            assert draft.state == state, pct
            assert draft.kind == "system.disk_pressure"
            if state == "clear":
                expected_sev = "info"  # below-threshold drafts are not findings
            else:
                expected_sev = "critical" if pct >= DISK_CRIT_USED_PCT else "warning"
            assert draft.severity == expected_sev, pct

    def test_memory_thresholds_on_available_share(self) -> None:
        low = findings_for_sample(_sample(mem_available_kib=40_000))[1]  # 4%
        assert low.state == "active" and low.severity == "critical"
        edge = findings_for_sample(_sample(mem_available_kib=100_000))[1]  # 10%
        assert edge.state == "active" and edge.severity == "warning"
        assert MEM_CRIT_AVAIL_PCT < MEM_WARN_AVAIL_PCT

    def test_load_threshold_is_one_full_machine(self) -> None:
        at = findings_for_sample(_sample(load1=32.0))[2]
        assert at.state == "active" and at.severity == "warning"
        under = findings_for_sample(_sample(load1=31.9))[2]
        assert under.state == "clear"

    def test_failed_units_active_listing_bounded(self) -> None:
        draft = findings_for_sample(
            _sample(systemd_failed_units=("nginx.service", "sshd.service"))
        )[3]
        assert draft.state == "active"
        assert draft.severity == "warning"
        assert "nginx.service" in draft.summary
        assert draft.dedup_key == "failed"

    def test_missing_data_is_no_data_and_never_closes(self) -> None:
        drafts = findings_for_sample(_sample(disk_root=None))
        assert drafts[0].state == "no_data"  # absent disk is NOT a clear
        unknown = findings_for_sample(
            _sample(
                quality=ObservationQuality.UNKNOWN,
                load1=None,
                load5=None,
                load15=None,
                cores=None,
                mem_total_kib=None,
                mem_available_kib=None,
                disk_root=None,
                uptime_s=None,
                systemd_present=None,
                systemd_failed_units=None,
            )
        )
        assert all(d.state == "no_data" for d in unknown)

    def test_summaries_never_exceed_the_contract_bound(self) -> None:
        units = tuple(f"{'u' * 60}{i}.service" for i in range(20))
        drafts = findings_for_sample(_sample(systemd_failed_units=units))
        for draft in drafts:
            if draft.state == "active":
                assert len(draft.summary) <= SUMMARY_MAX_CHARS

    def test_zero_failed_units_is_a_clear_not_an_active(self) -> None:
        draft = findings_for_sample(_sample(systemd_failed_units=()))[3]
        assert draft.state == "clear"


# ---------------------------------------------------------------------------
# Transport boundary without docker: budgets, frozen surface, rejection codes
# ---------------------------------------------------------------------------


def _identity(host: str = "203.0.113.10", port: int = 2222) -> SshIdentity:
    return SshIdentity(
        host=host,
        port=port,
        user="root",
        host_key="SHA256:" + "A" * 43,
        credential_ref="MAI_SSH_KEY_TEST",
    )


class TestProbeBoundaries:
    def test_probe_argv_is_exactly_the_frozen_p04_surface(self) -> None:
        argv = ssh_arguments(
            _identity(host="t.example", port=2200),
            key_path=Path("/keys/k"),
            known_hosts_path=Path("/kh"),
            remote_command=PROBE_COMMAND,
        )
        assert argv[0] == "ssh"
        assert argv[argv.index("-p") + 1] == "2200"
        assert argv[argv.index("-i") + 1] == "/keys/k"
        assert "-oIdentitiesOnly=yes" not in argv  # separate flag/value pair
        assert argv[argv.index("-o") + 1] == "IdentitiesOnly=yes"
        assert "BatchMode=yes" in argv and "StrictHostKeyChecking=yes" in argv
        assert "UserKnownHostsFile=/kh" in argv
        assert argv[-1] == PROBE_COMMAND

    def test_missing_enrollment_fails_closed_before_ssh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a: object, **k: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("ssh must not run without enrollment")

        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(SshConnectError) as excinfo:
            probe_system(
                _identity(),
                key_path=tmp_path / "k",
                known_hosts_path=tmp_path / "absent",
                controller_now=NOW,
            )
        assert excinfo.value.code is SshErrorCode.INVALID_CONFIGURATION

    def test_output_budget_enforced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pin = tmp_path / "known_hosts"
        pin.write_text("x\n", encoding="utf-8")

        def fake_run(*a: object, **k: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                a, 0, "x" * (MAX_PROBE_OUTPUT_BYTES + 1), ""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(SshConnectError) as excinfo:
            probe_system(
                _identity(),
                key_path=tmp_path / "k",
                known_hosts_path=pin,
                controller_now=NOW,
            )
        assert excinfo.value.code is SshErrorCode.BOUNDED_OUTPUT_EXCEEDED

    def test_timeout_maps_to_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pin = tmp_path / "known_hosts"
        pin.write_text("x\n", encoding="utf-8")

        def fake_run(*a: object, **k: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=PROBE_TIMEOUT_SECONDS)

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(SshConnectError) as excinfo:
            probe_system(
                _identity(),
                key_path=tmp_path / "k",
                known_hosts_path=pin,
                controller_now=NOW,
            )
        assert excinfo.value.code is SshErrorCode.TIMEOUT

    @pytest.mark.parametrize(
        ("rc", "stderr", "expected"),
        [
            (255, "Permission denied (publickey).", SshErrorCode.AUTH_FAILED),
            (
                255,
                "ssh: connect to host 203.0.113.10 port 2222: Connection refused",
                SshErrorCode.UNREACHABLE,
            ),
            (255, "Host key verification failed.", SshErrorCode.HOST_KEY_MISMATCH),
            (255, "some unexpected transport error", SshErrorCode.SSH_FAILED),
            (1, "remote command failed", SshErrorCode.SSH_FAILED),
        ],
    )
    def test_failure_mapping_uses_the_frozen_vocabulary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        rc: int,
        stderr: str,
        expected: SshErrorCode,
    ) -> None:
        pin = tmp_path / "known_hosts"
        pin.write_text("x\n", encoding="utf-8")

        def fake_run(*a: object, **k: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(a, rc, "", stderr)

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(SshConnectError) as excinfo:
            probe_system(
                _identity(),
                key_path=tmp_path / "k",
                known_hosts_path=pin,
                controller_now=NOW,
            )
        assert excinfo.value.code is expected

    def test_source_collector_identity_is_bounded(self) -> None:
        assert SOURCE == "system"
        assert len(SOURCE) <= SOURCE_MAX_CHARS


# ---------------------------------------------------------------------------
# Persistence on the real 0007 schema
# ---------------------------------------------------------------------------


class TestRecordSample:
    @pytest.mark.anyio
    async def test_measured_snapshot_persists_bounded_rows(self, sessions) -> None:
        sample = parse_probe_output(_ARCH_FIXTURE_OUTPUT, controller_now=NOW)
        written = await record_sample(
            sessions, uuid.UUID(SERVER_A), sample, controller_now=NOW
        )
        # load×3, cores, mem×2 + derived pct, disk×3, uptime = 11; the failed
        # units row is honestly ABSENT (units UNKNOWN on the real fixture).
        assert written == 11
        rows = await _fetchall(
            sessions,
            "SELECT source, method, quality, metric, value, observed_at, "
            "collected_at, fresh_until, retain_until, seq "
            "FROM server_observations WHERE server_id = :s ORDER BY metric",
            s=uuid.UUID(SERVER_A),
        )
        assert len(rows) == 11
        metrics = {r.metric: r.value for r in rows}
        assert metrics["cpu.load1"] == "1.35"
        assert metrics["cpu.cores"] == "32"
        assert metrics["mem.available_pct"] == "39.0"
        assert metrics["disk.root.used_pct"] == "72.0"
        assert "system.failed_units" not in metrics  # unknown ≠ zero row
        for r in rows:
            assert r.source == "system"
            assert r.method == "command"
            assert r.quality == "measured"
            assert r.observed_at == datetime(2026, 10, 17, 17, 48, 27, tzinfo=UTC)
            assert r.fresh_until == r.observed_at + timedelta(minutes=15)
            assert r.retain_until == NOW + RETENTION_HORIZON
            assert r.seq is not None  # schema-owned sequence stamped it

    @pytest.mark.anyio
    async def test_zero_failed_units_persists_a_zero_row(self, sessions) -> None:
        sample = parse_probe_output(
            "systemd=yes\nsystemd_failed_rc=0\nload1=0.1\nload5=0.1\nload15=0.1\n"
            "cores=4\nmem_total_kib=1000\nmem_available_kib=500\n"
            "disk_total_kib=100\ndisk_avail_kib=50\ndisk_used_pct=50%\n"
            "uptime_s=12.5\n",
            controller_now=NOW,
        )
        assert sample.systemd_failed_units == ()
        await record_sample(sessions, uuid.UUID(SERVER_A), sample, controller_now=NOW)
        row = await _fetchone(
            sessions,
            "SELECT value FROM server_observations "
            "WHERE metric = 'system.failed_units'",
        )
        assert row is not None and row.value == "0"

    @pytest.mark.anyio
    async def test_finding_lifecycle_open_update_close_reopen(self, sessions) -> None:
        t1 = NOW
        t2 = NOW + timedelta(minutes=1)
        t3 = NOW + timedelta(minutes=2)
        t4 = NOW + timedelta(minutes=3)
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(disk_root=_disk(96.0)),
            controller_now=t1,
        )
        first = await _fetchone(
            sessions,
            "SELECT finding_id, severity, opened_at, updated_at, resolved_at "
            "FROM observation_findings WHERE server_id = :s "
            "AND kind = 'system.disk_pressure'",
            s=uuid.UUID(SERVER_A),
        )
        assert first.severity == "critical" and first.resolved_at is None
        assert first.opened_at == t1 and first.updated_at == t1

        # Repeated poll: same condition collapses onto the same row.
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(disk_root=_disk(96.5)),
            controller_now=t2,
        )
        rows = await _fetchall(
            sessions,
            "SELECT finding_id, opened_at, updated_at, resolved_at "
            "FROM observation_findings WHERE server_id = :s "
            "AND kind = 'system.disk_pressure'",
            s=uuid.UUID(SERVER_A),
        )
        assert len(rows) == 1
        assert rows[0].finding_id == first.finding_id
        assert rows[0].opened_at == t1  # first observation preserved
        assert rows[0].updated_at == t2

        # Measured recovery closes; the row (ack-style evidence) survives.
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(disk_root=_disk(50.0)),
            controller_now=t3,
        )
        closed = await _fetchone(
            sessions,
            "SELECT resolved_at FROM observation_findings WHERE server_id = :s "
            "AND kind = 'system.disk_pressure'",
            s=uuid.UUID(SERVER_A),
        )
        assert closed.resolved_at == t3

        # Recurrence reopens the SAME row without resetting opened_at.
        t4 = NOW + timedelta(minutes=3)
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(disk_root=_disk(97.0)),
            controller_now=t4,
        )
        reopened = await _fetchone(
            sessions,
            "SELECT opened_at, updated_at, resolved_at, severity "
            "FROM observation_findings WHERE server_id = :s "
            "AND kind = 'system.disk_pressure'",
            s=uuid.UUID(SERVER_A),
        )
        assert reopened.resolved_at is None
        assert reopened.opened_at == t1
        assert reopened.updated_at == t4 and reopened.severity == "critical"

    @pytest.mark.anyio
    async def test_unknown_collection_never_touches_findings(self, sessions) -> None:
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(disk_root=_disk(96.0)),
            controller_now=NOW,
        )
        written = await record_unavailable(
            sessions, uuid.UUID(SERVER_A), controller_now=NOW + timedelta(minutes=1)
        )
        assert written == 1
        row = await _fetchone(
            sessions,
            "SELECT quality, metric, value, observed_at, fresh_until, retain_until "
            "FROM server_observations WHERE quality = 'unknown'",
        )
        assert row is not None
        assert row.metric is None and row.value is None and row.observed_at is None
        assert row.fresh_until is None
        assert row.retain_until is not None
        finding = await _fetchone(
            sessions,
            "SELECT resolved_at, updated_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'system.disk_pressure'",
            s=uuid.UUID(SERVER_A),
        )
        assert finding.resolved_at is None  # unknown never re-evaluates
        assert finding.updated_at == NOW  # untouched by the unknown path

    @pytest.mark.anyio
    async def test_unknown_with_a_metric_is_a_real_integrity_violation(
        self, sessions
    ) -> None:
        # The structural half of "stale/unknown, not zero usage": the schema
        # itself refuses an unknown row that carries a measurement.
        with pytest.raises(IntegrityError):
            async with sessions() as session:
                await session.execute(
                    text(
                        "INSERT INTO server_observations (observation_id, server_id, "
                        "source, method, quality, metric, value, collected_at, "
                        "retain_until) VALUES (:oid, :sid, 'system', 'command', "
                        "'unknown', 'cpu.load1', '0.00', now(), now())"
                    ),
                    {"oid": uuid.uuid4(), "sid": uuid.UUID(SERVER_A)},
                )
                await session.commit()

    @pytest.mark.anyio
    async def test_two_servers_are_a_storage_boundary_and_freshness_derives(
        self, sessions
    ) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(load1=1.0), controller_now=NOW
        )
        await record_sample(
            sessions, uuid.UUID(SERVER_B), _sample(load1=2.0), controller_now=NOW
        )
        rows_a = await _fetchall(
            sessions,
            "SELECT metric, value FROM server_observations WHERE server_id = :s",
            s=uuid.UUID(SERVER_A),
        )
        rows_b = await _fetchall(
            sessions,
            "SELECT metric, value FROM server_observations WHERE server_id = :s",
            s=uuid.UUID(SERVER_B),
        )
        assert {r.value for r in rows_a if r.metric == "cpu.load1"} == {"1.00"}
        assert {r.value for r in rows_b if r.metric == "cpu.load1"} == {"2.00"}
        assert len(rows_a) == len(rows_b)

        # Freshness on LIVE stored rows: fresh inside the horizon, stale past
        # it (consumer-clock derivation), unknown without a sample.
        stored = await _fetchone(
            sessions,
            "SELECT quality, observed_at, fresh_until FROM server_observations "
            "WHERE server_id = :s AND metric = 'cpu.load1'",
            s=uuid.UUID(SERVER_A),
        )
        assert (
            derive_freshness(
                stored.quality,
                stored.observed_at,
                stored.fresh_until,
                NOW + timedelta(minutes=10),
            )
            is ObservationFreshness.FRESH
        )
        assert (
            derive_freshness(
                stored.quality,
                stored.observed_at,
                stored.fresh_until,
                NOW + timedelta(minutes=16),
            )
            is ObservationFreshness.STALE
        )
        await record_unavailable(sessions, uuid.UUID(SERVER_B), controller_now=NOW)
        unk = await _fetchone(
            sessions,
            "SELECT quality, observed_at, fresh_until FROM server_observations "
            "WHERE server_id = :s AND quality = 'unknown'",
            s=uuid.UUID(SERVER_B),
        )
        assert (
            derive_freshness(unk.quality, unk.observed_at, unk.fresh_until, NOW)
            is ObservationFreshness.UNKNOWN
        )


# ---------------------------------------------------------------------------
# Disposable Arch sshd fixture (runtime-environment ADR D1–D5; the card's
# exclusive runtime-target fixture, unique 10-system namespace)
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-10-system-arch"
FIXTURE_IMAGE_TAG = "mai-fxt-10-system-arch:sshd"
FALLBACK_IMAGE_TAG = "mai-fxt-04-ssh-arch:sshd"  # identical reviewed recipe
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-10-system-tests")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_10_SYSTEM_FIXTURE"


def _docker(*args: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _ensure_fixture_image() -> str:
    """Own tag first; identical reviewed 04 recipe reused; else built."""
    if _docker("image", "inspect", FIXTURE_IMAGE_TAG).returncode == 0:
        return "own tag present"
    if _docker("image", "inspect", FALLBACK_IMAGE_TAG).returncode == 0:
        tag = _docker("tag", FALLBACK_IMAGE_TAG, FIXTURE_IMAGE_TAG)
        assert tag.returncode == 0, tag.stderr
        return f"tagged from {FALLBACK_IMAGE_TAG} (identical reviewed recipe)"
    context = FIXTURE_SCRATCH / "build-context"
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(
        "FROM archlinux:latest\n"
        "RUN pacman-key --init && pacman -Sy --noconfirm openssh && ssh-keygen -A\n",
        encoding="utf-8",
    )
    build = _docker("build", "-q", "-t", FIXTURE_IMAGE_TAG, str(context), timeout=1200)
    assert build.returncode == 0, f"fixture image build failed: {build.stderr[-800:]}"
    return f"built from archlinux:latest ({FALLBACK_IMAGE_TAG} absent)"


class ArchSshdFixture:
    """One disposable Arch sshd container; removes itself with proof."""

    def __init__(self) -> None:
        self.scratch = FIXTURE_SCRATCH / uuid.uuid4().hex
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.key_path = self.scratch / "fixture_key"
        self.port = 0
        self.identity: SshIdentity | None = None
        self.image_provenance = "<untouched>"
        self.cleanup_proof = "<not cleaned>"

    def start(self) -> SshIdentity:
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(self.key_path),
                "-C",
                f"{FIXTURE_CONTAINER}-test",
            ],
            check=True,
            capture_output=True,
        )
        os.chmod(self.key_path, 0o600)
        self.image_provenance = _ensure_fixture_image()
        run = _docker(
            "run",
            "--rm",
            "-d",
            "--name",
            FIXTURE_CONTAINER,
            "-p",
            "127.0.0.1::22",
            FIXTURE_IMAGE_TAG,
            "sh",
            "-c",
            "rm -f /etc/ssh/ssh_host_* && ssh-keygen -A "
            "&& mkdir -p /var/empty /run/sshd "
            "&& exec /usr/bin/sshd -D",
        )
        assert run.returncode == 0, f"fixture start failed: {run.stderr[-800:]}"
        self._refresh_port()
        self._provision_authorized_key(self.key_path)
        self._wait_keyscan()
        return self.identity_for(self._current_fingerprint())

    def _wait_keyscan(self, attempts: int = 30) -> None:
        """sshd emits host keys only after generating them; poll briefly."""
        last = ""
        for _ in range(attempts):
            try:
                if self._scan_now():
                    return
                last = "no keys yet"
            except subprocess.TimeoutExpired as exc:
                last = str(exc)
            time.sleep(1.0)
        raise AssertionError(f"fixture sshd never presented host keys: {last}")

    def _refresh_port(self) -> None:
        port_out = _docker(
            "inspect",
            "--format",
            '{{(index (index .NetworkSettings.Ports "22/tcp") 0).HostPort}}',
            FIXTURE_CONTAINER,
        )
        assert port_out.returncode == 0 and port_out.stdout.strip().isdigit(), (
            f"fixture port inspect failed: {port_out.stderr[-400:]}"
        )
        self.port = int(port_out.stdout.strip())

    def identity_for(self, host_key: str) -> SshIdentity:
        return SshIdentity(
            host="127.0.0.1",
            port=self.port,
            user="root",
            host_key=host_key,
            credential_ref=FIXTURE_CREDENTIAL_REF,
        )

    def _provision_authorized_key(self, key_path: Path) -> None:
        pub = Path(f"{key_path}.pub").read_text(encoding="utf-8").strip()
        provision = _docker(
            "exec",
            "-e",
            f"KEY={pub}",
            FIXTURE_CONTAINER,
            "sh",
            "-c",
            "mkdir -p /root/.ssh && chmod 700 /root/.ssh "
            "&& printf '%s\\n' \"$KEY\" > /root/.ssh/authorized_keys "
            "&& chmod 600 /root/.ssh/authorized_keys",
        )
        assert provision.returncode == 0, provision.stderr

    def enroll(self) -> None:
        assert self.identity is not None
        pin_host_key(self.identity, self.known_hosts_path)

    @property
    def known_hosts_path(self) -> Path:
        return self.scratch / "known_hosts"

    def env_resolver(self) -> EnvResolver:
        env_file = self.scratch / "fixture.env"
        env_file.write_text(
            f"{FIXTURE_CREDENTIAL_REF}={self.key_path}\n", encoding="utf-8"
        )
        os.chmod(env_file, 0o600)
        return EnvResolver(env_file)

    def unauthorized_resolver(self) -> EnvResolver:
        """Resolver whose referenced key is NOT authorized on the target."""
        wrong_key = self.scratch / "unauthorized_key"
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(wrong_key),
                "-C",
                f"{FIXTURE_CONTAINER}-unauthorized",
            ],
            check=True,
            capture_output=True,
        )
        os.chmod(wrong_key, 0o600)
        env_file = self.scratch / "unauthorized.env"
        env_file.write_text(f"{FIXTURE_CREDENTIAL_REF}={wrong_key}\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        return EnvResolver(env_file)

    def start_argv_sentinel_and_load(self, sentinel: str) -> None:
        """Controlled CPU load + a live process carrying the sentinel in ARGV.

        ``sh -c 'sleep 120; :' name SENTINEL`` keeps a shell alive for the
        whole collection window with the sentinel in its argv (a two-command
        sequence prevents the shell's exec-tail optimization); ``yes`` burns
        one core. Both are reaped by the container teardown.
        """
        self.run_remote(
            f"yes >/dev/null 2>&1 & sh -c 'sleep 120; :' fixture-argv0 {sentinel} "
            ">/dev/null 2>&1 & sleep 1"
        )

    def run_remote(self, remote_command: str, timeout: float = 30.0) -> str:
        assert self.identity is not None
        resolver = self.env_resolver()
        key_path = resolve_identity_key(resolver, self.identity.credential_ref)
        argv = ssh_arguments(
            self.identity,
            key_path=key_path,
            known_hosts_path=self.known_hosts_path,
            remote_command=remote_command,
        )
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        assert proc.returncode == 0, (
            f"fixture control command failed: {proc.stderr[-400:]}"
        )
        return proc.stdout

    def _scan_now(self) -> tuple[str, ...]:
        scan = subprocess.run(
            ["ssh-keyscan", "-p", str(self.port), "-t", "ed25519,rsa", "127.0.0.1"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return tuple(line for line in scan.stdout.splitlines() if line.strip())

    def _current_fingerprint(self) -> str:
        lines = self._scan_now()
        assert lines, "fixture keyscan produced no host keys"
        ed25519 = next((line for line in lines if " ssh-ed25519 " in line), lines[0])
        keyfile = self.scratch / "scan-hostkey.pub"
        keyfile.write_text(ed25519 + "\n", encoding="utf-8")
        keygen = subprocess.run(
            ["ssh-keygen", "-lf", str(keyfile)], capture_output=True, text=True
        )
        assert keygen.returncode == 0, keygen.stderr
        return keygen.stdout.split()[1]

    def stop(self) -> None:
        stop = _docker("stop", "-t", "1", FIXTURE_CONTAINER)
        assert stop.returncode == 0, stop.stderr

    def cleanup(self) -> str:
        _docker("rm", "-f", FIXTURE_CONTAINER, timeout=60)
        listing = _docker(
            "ps",
            "-a",
            "--filter",
            f"name=^{FIXTURE_CONTAINER}$",
            "--format",
            "{{.Names}}",
        )
        shutil.rmtree(self.scratch, ignore_errors=True)
        # The image-build scratch lives at the fixture-scratch root, so remove
        # it here too; _ensure_fixture_image() recreates everything on demand.
        shutil.rmtree(FIXTURE_SCRATCH, ignore_errors=True)
        self.cleanup_proof = (
            f"container {FIXTURE_CONTAINER} after rm: "
            f"'{listing.stdout.strip() or '<none>'}'; "
            f"scratch {FIXTURE_SCRATCH} removed: {not FIXTURE_SCRATCH.exists()}"
        )
        return self.cleanup_proof


@pytest.fixture()
def arch_sshd() -> ArchSshdFixture:
    if not _docker_available():
        pytest.skip("docker unavailable: Arch sshd fixture tests require it")
    fixture = ArchSshdFixture()
    try:
        fixture.identity = fixture.start()
        fixture.enroll()
        yield fixture
    finally:
        print(f"\nFIXTURE IMAGE: {fixture.image_provenance}")
        print(f"FIXTURE CLEANUP PROOF: {fixture.cleanup()}")


@pytest.mark.real_target
class TestAgainstArchFixture:
    @pytest.mark.anyio
    async def test_real_snapshot_is_measured_bounded_and_stays_secret_free(
        self, arch_sshd: ArchSshdFixture, migrated_url: str, sessions
    ) -> None:
        arch_sshd.start_argv_sentinel_and_load(ARGV_SENTINEL)
        resolver = arch_sshd.env_resolver()
        key_path = resolve_identity_key(resolver, arch_sshd.identity.credential_ref)
        result = await collect(
            sessions,
            arch_sshd.identity,
            key_path=key_path,
            known_hosts_path=arch_sshd.known_hosts_path,
            server_id=uuid.UUID(SERVER_A),
            controller_now=datetime.now(UTC),
        )
        assert result.error is None
        assert result.quality is ObservationQuality.MEASURED

        rows = await _fetchall(
            sessions,
            "SELECT source, method, quality, metric, value, observed_at "
            "FROM server_observations WHERE server_id = :s",
            s=uuid.UUID(SERVER_A),
        )
        metrics = {r.metric: r.value for r in rows}
        # Acceptance 1: source + timestamp on every row of a real snapshot.
        assert rows and all(
            r.source == "system" and r.method == "command" for r in rows
        )
        assert all(r.observed_at is not None for r in rows)
        drift = max(
            abs((r.observed_at - datetime.now(UTC)).total_seconds()) for r in rows
        )
        assert drift < 600  # target clock ≈ controller clock (shared host)
        # Real measured content, never fabricated.
        assert int(metrics["cpu.cores"]) >= 1
        assert int(metrics["mem.total_kib"]) > 0
        assert 0.0 < float(metrics["disk.root.used_pct"]) <= 100.0
        assert float(metrics["cpu.load1"]) >= 0.0
        assert int(metrics["system.uptime_s"]) >= 0
        # Acceptance 2, structural half: no unknown row carries a measurement.
        assert all(
            r.metric is None and r.value is None and r.observed_at is None
            for r in rows
            if r.quality == "unknown"
        )
        # No secret sentinel from process args, anywhere in the domain.
        assert ARGV_SENTINEL not in await _storage_dump(sessions)

    @pytest.mark.anyio
    async def test_real_disconnect_records_unknown_never_zero(
        self, arch_sshd: ArchSshdFixture, migrated_url: str, sessions
    ) -> None:
        resolver = arch_sshd.env_resolver()
        key_path = resolve_identity_key(resolver, arch_sshd.identity.credential_ref)
        before = await collect(
            sessions,
            arch_sshd.identity,
            key_path=key_path,
            known_hosts_path=arch_sshd.known_hosts_path,
            server_id=uuid.UUID(SERVER_A),
            controller_now=datetime.now(UTC),
        )
        assert before.quality is ObservationQuality.MEASURED

        arch_sshd.stop()  # disconnect the target mid-lifecycle
        after = await collect(
            sessions,
            arch_sshd.identity,
            key_path=key_path,
            known_hosts_path=arch_sshd.known_hosts_path,
            server_id=uuid.UUID(SERVER_A),
            controller_now=datetime.now(UTC),
        )
        # Acceptance 2: unavailable target ⇒ unknown, NOT zero usage.
        assert after.quality is ObservationQuality.UNKNOWN
        assert after.error == "unreachable"
        assert after.observations == 1

        row = await _fetchone(
            sessions,
            "SELECT metric, value, observed_at FROM server_observations "
            "WHERE quality = 'unknown' AND server_id = :s",
            s=uuid.UUID(SERVER_A),
        )
        assert row is not None
        assert row.metric is None and row.value is None and row.observed_at is None
        violations = await _fetchone(
            sessions,
            "SELECT count(*) AS n FROM server_observations WHERE quality = 'unknown' "
            "AND (metric IS NOT NULL OR value IS NOT NULL OR observed_at IS NOT NULL)",
        )
        assert violations.n == 0  # the "not zero usage" structural bound
        stale = await _fetchone(
            sessions,
            "SELECT quality, observed_at, fresh_until FROM server_observations "
            "WHERE server_id = :s AND quality = 'measured' LIMIT 1",
            s=uuid.UUID(SERVER_A),
        )
        # The pre-disconnect sample ages honestly: past its horizon the shared
        # derivation reports STALE — a dead target is never re-read as fresh.
        assert (
            derive_freshness(
                stale.quality,
                stale.observed_at,
                stale.fresh_until,
                stale.observed_at + RETENTION_HORIZON,
            )
            is ObservationFreshness.STALE
        )

    @pytest.mark.anyio
    async def test_real_permission_denied_records_unknown(
        self, arch_sshd: ArchSshdFixture, migrated_url: str, sessions
    ) -> None:
        # A target refusing the identity (permission boundary) takes the same
        # unknown path: recorded once, never zeroed.
        resolver = arch_sshd.unauthorized_resolver()
        key_path = resolve_identity_key(resolver, FIXTURE_CREDENTIAL_REF)
        assert key_path.name == "unauthorized_key"
        result = await collect(
            sessions,
            arch_sshd.identity,
            key_path=key_path,
            known_hosts_path=arch_sshd.known_hosts_path,
            server_id=uuid.UUID(SERVER_A),
            controller_now=datetime.now(UTC),
        )
        assert result.quality is ObservationQuality.UNKNOWN
        assert result.error == "auth_failed"
        row = await _fetchone(
            sessions,
            "SELECT metric, value, observed_at FROM server_observations "
            "WHERE quality = 'unknown' AND server_id = :s",
            s=uuid.UUID(SERVER_A),
        )
        assert row is not None
        assert row.metric is None and row.value is None and row.observed_at is None
