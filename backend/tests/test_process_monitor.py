"""Focused tests for the bounded process collector (card 10-process, C04 + C10).

Acceptance coverage (BRIEF):

1. **Bounded process identity/resource snapshot, explicit permitted
   metadata, args filtered or excluded** — the fixed probe command
   (``PROBE_COMMAND``) reads ONLY ``/proc/<pid>/stat`` and
   ``/proc/<pid>/comm``: command-line arguments and environment variables
   are excluded STRUCTURALLY (negative control: the probe string contains
   neither ``cmdline`` nor ``environ``). Identity is pid/comm/state/ppid/
   start_ticks; resources are CPU ticks and RSS. The listing is bounded
   target-side (top-16 by CPU ticks) and the controller hard-caps captured
   output (``MAX_PROBE_OUTPUT_BYTES``); a real fixture snapshot persists as
   bounded per-metric rows carrying ``source='process'``/``method='command'``
   plus ``observed_at`` and the collector horizons.
2. **Permission gaps/stale data explicit; no kill capability** — an
   unavailable/permission-refused target records exactly ONE
   ``quality='unknown'`` row with NO metric/value/observed_at (the 0007
   schema structurally forbids a zero reading); a target with unreadable
   ``/proc`` entries reports the gap explicitly (``unreadable`` count,
   ``degraded`` quality, an active ``process.read_gaps`` finding) — a
   partially visible process table is never ``measured``; missing data
   never re-evaluates or closes a finding; consumers derive staleness via
   the shared ``derive_freshness``. Process monitoring confers NO mutation
   capability: the module exposes no kill/signal/renice surface, the probe
   command contains no mutation primitive, and on the real fixture every
   spawned process is STILL ALIVE after collection (the ungranted-mutation
   attempt has no caller-reachable effect).

Layers per the test policy: pure parse/finding tests (always run);
transport-boundary tests over monkeypatched subprocess; persistence claims
on a real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``, real
Alembic chain to ``0007``); ``real_target``-marked tests against a
disposable Arch sshd container (``mai-fxt-10-process-arch`` namespace,
mirroring the reviewed 04-ssh-connect fixture), auto-skipped without docker
and removed with recorded proof.

Secret hygiene: fixture keys are throwaway files under ``/tmp/mai-fxt-10-
process-*``; the fixture ``.env`` holds a PATH reference, never material;
the argv sentinel is a synthetic marker string, not a secret — it is
planted ONLY in fixture-process ARGV, and the negative control asserts it
never reaches the observation domain (storage dump audit).
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

import mai.monitoring.processes as processes_module

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
from mai.monitoring.processes import (
    RSS_KIB_PER_PAGE,
    MAX_PROBE_OUTPUT_BYTES,
    MAX_PROCESSES_LISTED,
    MAX_SERVICES_LISTED,
    MEM_HOG_CRIT_MIB,
    MEM_HOG_WARN_MIB,
    PROBE_COMMAND,
    PROBE_TIMEOUT_SECONDS,
    RETENTION_HORIZON,
    SOURCE,
    ProcessEntry,
    ProcessSample,
    ServiceEntry,
    collect,
    findings_for_sample,
    parse_probe_output,
    probe_processes,
    record_sample,
    record_unavailable,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Frozen fixed fixture identities (canonical lowercase UUID strings, distinct
# from every other card's constants).
SERVER_A = "aa1b2c3d-4e5f-4a60-8b7c-9d0e1f2a3b4c"
SERVER_B = "bb2c3d4e-5f6a-4b70-9c8d-0e1f2a3b4c5d"

NOW = datetime(2026, 10, 17, 19, 30, 0, tzinfo=UTC)

#: Non-disclosure sentinel planted in a FIXTURE PROCESS ARGV on the real
#: target (NOT a secret): no observation row may ever contain it because the
#: process probe never reads ``cmdline``/``environ`` and stores only the
#: kernel-truncated ``comm``.
ARGV_SENTINEL = "mai-10-process-argv-sentinel-not-a-secret"

# Real probe output captured verbatim from the disposable Arch fixture target
# (2026-10-17, mai-fxt-10-process-arch; container PID namespace: sshd is PID 1,
# the spawned ``yes`` CPU burner tops the CPU-tick listing with its sentinel-
# carrying ``sh`` parent visible as identity-only ``sh``; systemd binaries are
# present but PID 1 is sshd ⇒ the service query fails with rc=1 ⇒ services
# UNKNOWN, the exact real-world "absent source ≠ zero" shape). Embedded as the
# parse fixture so the pure tests exercise the exact byte shape the real
# target emits — including the argv sentinel's ABSENCE from the listing.
_ARCH_FIXTURE_OUTPUT = (
    "ts=2026-10-17T19:32:15\n"
    "procs=7\n"
    "zombies=0\n"
    "unreadable=0\n"
    "pp=220 541 R 1 73930604 24 yes\n"
    "pp=2 3128 S 1 73930809 30 sshd-session\n"
    "pp=2 1896 S 0 73930287 1 sshd\n"
    "pp=0 987 S 1 73930604 25 sh\n"
    "pp=0 819 S 33 73930824 34 bash\n"
    "pp=0 538 R 34 73930825 37 bash\n"
    "pp=0 530 S 25 73930604 27 sleep\n"
    "pp=0 405 S 34 73930825 38 sort\n"
    "pp=0 1881 S 30 73930824 33 sshd-session\n"
    "systemd=yes\n"
    "svc_rc=1\n"
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


def _entry(
    pid: int = 34,
    comm: str | None = "yes",
    state: str = "R",
    ppid: int = 1,
    cpu_ticks: int = 5121,
    rss_kib: int = 558,
    start_ticks: int = 73877248,
) -> ProcessEntry:
    return ProcessEntry(
        pid=pid,
        comm=comm,
        state=state,
        ppid=ppid,
        cpu_ticks=cpu_ticks,
        rss_kib=rss_kib,
        start_ticks=start_ticks,
    )


def _sample(**overrides: object) -> ProcessSample:
    fields: dict[str, object] = {
        "observed_at": NOW,
        "observed_at_source": "target",
        "quality": ObservationQuality.MEASURED,
        "total_procs": 7,
        "zombies": 0,
        "unreadable": 0,
        "processes": (_entry(),),
        "systemd_present": True,
        "services": None,
        "svc_total": None,
        "svc_running": None,
        "svc_failed": None,
        "svc_exited": None,
    }
    fields.update(overrides)
    return ProcessSample(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure parsing: bounded target output → honest sample
# ---------------------------------------------------------------------------


class TestParseProbeOutput:
    def test_real_fixture_capture_parses_measured(self) -> None:
        sample = parse_probe_output(_ARCH_FIXTURE_OUTPUT, controller_now=NOW)
        assert sample.quality is ObservationQuality.MEASURED
        # Target-reported timestamp, controller clock untouched.
        assert sample.observed_at == datetime(2026, 10, 17, 19, 32, 15, tzinfo=UTC)
        assert sample.observed_at_source == "target"
        assert (sample.total_procs, sample.zombies, sample.unreadable) == (7, 0, 0)
        # Listing keeps the target's CPU-tick rank: strictly non-increasing
        # across MIXED digit lengths (the numeric-sort regression fixture).
        pids = [p.pid for p in sample.processes]
        assert pids[0] == 24
        ticks = [p.cpu_ticks for p in sample.processes]
        assert ticks == sorted(ticks, reverse=True)
        assert len(sample.processes) == 9
        assert sample.processes[0].comm == "yes"
        assert sample.processes[0].cpu_ticks == 220
        assert sample.processes[0].rss_kib == 541 * RSS_KIB_PER_PAGE  # pages → KiB
        assert sample.processes[0].state == "R"
        # The sentinel-carrying process appears as identity-only comm.
        assert any(p.pid == 25 and p.comm == "sh" for p in sample.processes)
        # The REAL fixture case: systemd binaries exist but the service query
        # failed (rc=1, sshd is PID 1) ⇒ services UNKNOWN, never zero.
        assert sample.systemd_present is True
        assert sample.services is None
        assert sample.svc_total is None

    def test_empty_output_is_unknown_without_any_metric(self) -> None:
        sample = parse_probe_output("", controller_now=NOW)
        assert sample.quality is ObservationQuality.UNKNOWN
        assert sample.total_procs is None
        assert sample.zombies is None
        assert sample.unreadable is None
        assert sample.processes == ()
        assert sample.services is None
        assert sample.observed_at == NOW
        assert sample.observed_at_source == "controller"

    def test_counts_only_is_degraded(self) -> None:
        sample = parse_probe_output(
            "procs=12\nzombies=0\nunreadable=0\n", controller_now=NOW
        )
        assert sample.quality is ObservationQuality.DEGRADED
        assert sample.total_procs == 12
        assert sample.processes == ()

    def test_listing_only_is_degraded(self) -> None:
        sample = parse_probe_output("pp=5 100 S 0 10 7 init\n", controller_now=NOW)
        assert sample.quality is ObservationQuality.DEGRADED
        assert sample.total_procs is None
        assert len(sample.processes) == 1

    def test_read_gap_caps_quality_at_degraded_and_reports_explicitly(self) -> None:
        sample = parse_probe_output(
            "procs=9\nzombies=0\nunreadable=2\npp=5 100 S 0 10 7 init\n",
            controller_now=NOW,
        )
        # Both core sources present, but the table was only partially
        # visible: degraded, with the explicit gap count preserved.
        assert sample.quality is ObservationQuality.DEGRADED
        assert sample.unreadable == 2
        assert sample.total_procs == 9
        assert len(sample.processes) == 1

    def test_junk_values_become_none_never_zero(self) -> None:
        sample = parse_probe_output(
            "procs=abc\nzombies=-3\nunreadable=1e3\n"
            "pp=junk 100 S 0 10 7 init\n"
            "pp=5 notanumber S 0 10 7 init\n"
            "pp=5 100 5 0 10 7 init\n"  # invalid state token
            "pp=5 100 S 0 10 0 init\n"  # pid 0 is not a process pid
            "pp=5 100 S 0 10 4294967296 init\n"  # pid beyond the bound
            "pp=5 100 S 0 10 7\n",  # missing comm field is still parseable
            controller_now=NOW,
        )
        assert sample.total_procs is None
        assert sample.zombies is None
        assert sample.unreadable is None
        assert len(sample.processes) == 1
        assert sample.processes[0].pid == 7
        assert sample.processes[0].comm is None
        assert sample.quality is ObservationQuality.DEGRADED  # listing only

    def test_comm_validation_rejects_oversize_and_control_chars(self) -> None:
        sample = parse_probe_output(
            "pp=5 100 S 0 10 7 " + "x" * 16 + "\n"
            "pp=5 100 S 0 10 8 bad\tcomm\n"
            "pp=5 100 S 0 10 9 systemd-udevd\n"  # 13 chars, fine
            "pp=5 100 S 0 10 10 kworker/u33:1\n",  # slash allowed
            controller_now=NOW,
        )
        comms = {p.pid: p.comm for p in sample.processes}
        assert comms[7] is None  # >15 chars truncated name rejected outright
        assert comms[8] is None  # control character rejected
        assert comms[9] == "systemd-udevd"
        assert comms[10] == "kworker/u33:1"

    def test_comm_may_contain_spaces_and_equals_after_partition(self) -> None:
        # The line is split at the FIRST '=' (the ``pp=`` marker); a comm
        # containing '=' or spaces survives as the trailing field.
        sample = parse_probe_output("pp=5 100 S 0 10 7 my app=v2\n", controller_now=NOW)
        assert sample.processes[0].comm == "my app=v2"

    def test_zombie_state_is_a_valid_identity(self) -> None:
        sample = parse_probe_output(
            "procs=2\nzombies=1\nunreadable=0\npp=0 64 Z 9 10 8 sleeper\n",
            controller_now=NOW,
        )
        assert sample.processes[0].state == "Z"

    def test_first_occurrence_wins_for_repeated_keys_and_pids(self) -> None:
        sample = parse_probe_output(
            "procs=4\nprocs=99\npp=5 100 S 0 10 7 init\npp=9 200 R 0 10 7 dupe\n",
            controller_now=NOW,
        )
        assert sample.total_procs == 4
        assert [p.pid for p in sample.processes] == [7]

    def test_listing_is_capped_at_the_frozen_bound(self) -> None:
        lines = [f"pp={1000 - i} 100 S 0 10 {i + 1} p{i}" for i in range(40)]
        sample = parse_probe_output("\n".join(lines) + "\n", controller_now=NOW)
        assert len(sample.processes) == MAX_PROCESSES_LISTED
        assert sample.processes[0].cpu_ticks == 1000  # rank preserved

    def test_services_parsed_and_validated(self) -> None:
        lines = [
            "systemd=yes",
            "svc_rc=0",
            "svc=running sshd.service",
            "svc=exited systemd-journald.service",
            "svc=RUNNING upper.service",  # invalid sub token
            "svc=running bad unit.service",  # unit with a space
            "svc=running " + "u" * 65 + ".service",  # overlong unit
        ]
        sample = parse_probe_output("\n".join(lines) + "\n", controller_now=NOW)
        assert sample.services == (
            ServiceEntry(unit="sshd.service", sub="running"),
            ServiceEntry(unit="systemd-journald.service", sub="exited"),
        )
        subs = {s.sub for s in sample.services}
        assert subs <= {"running", "exited", "failed"}

    def test_service_listing_is_capped_at_the_frozen_bound(self) -> None:
        lines = ["systemd=yes", "svc_rc=0"]
        lines += [f"svc=running unit{i}.service" for i in range(40)]
        sample = parse_probe_output("\n".join(lines) + "\n", controller_now=NOW)
        assert sample.services is not None
        assert len(sample.services) == MAX_SERVICES_LISTED
        assert sample.services[0] == ServiceEntry(unit="unit0.service", sub="running")
        assert sample.svc_total is None  # counts were not in this capture

    def test_failed_service_query_is_unknown_never_zero(self) -> None:
        sample = parse_probe_output(
            "systemd=yes\nsvc_rc=1\nsvc=running liar.service\n",
            controller_now=NOW,
        )
        assert sample.systemd_present is True
        assert sample.services is None  # rc≠0 ⇒ listing not trusted

        sample = parse_probe_output("procs=1\nsystemd=no\n", controller_now=NOW)
        assert sample.systemd_present is False
        assert sample.services is None  # absent source ≠ zero

        sample = parse_probe_output(
            "systemd=yes\nsvc_rc=0\nsvc_count=0\nsvc_running=0\n"
            "svc_failed=0\nsvc_exited=0\n",
            controller_now=NOW,
        )
        # A SUCCEEDING query may honestly report zero.
        assert sample.services == ()
        assert (sample.svc_total, sample.svc_running) == (0, 0)

    def test_success_without_counts_keeps_them_none(self) -> None:
        # svc_rc=0 but the counting awk produced nothing (broken/absent):
        # counts stay unknown — never fabricated zeros.
        sample = parse_probe_output(
            "systemd=yes\nsvc_rc=0\nsvc=running a.service\n", controller_now=NOW
        )
        assert sample.services is not None
        assert sample.svc_total is None and sample.svc_running is None

    def test_unparsable_timestamp_falls_back_to_controller_clock(self) -> None:
        sample = parse_probe_output("ts=nonsense\nprocs=1\n", controller_now=NOW)
        assert sample.observed_at_source == "controller"
        assert sample.observed_at == NOW

    def test_unknown_keys_ignored_and_carriage_returns_tolerated(self) -> None:
        sample = parse_probe_output(
            "future_key=x\r\nprocs=3\r\npp=5 100 S 0 10 7 init\r\n",
            controller_now=NOW,
        )
        assert sample.total_procs == 3
        assert len(sample.processes) == 1

    def test_naive_controller_clock_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            parse_probe_output("procs=1\n", controller_now=datetime(2026, 10, 17))


# ---------------------------------------------------------------------------
# Pure findings: thresholds, three-valued states, bounded summaries
# ---------------------------------------------------------------------------


class TestFindingsForSample:
    def test_idle_sample_clears_every_condition(self) -> None:
        drafts = findings_for_sample(_sample())
        assert [d.state for d in drafts] == ["clear", "clear", "clear"]
        assert all(d.severity == "info" for d in drafts)

    def test_zombie_threshold(self) -> None:
        active = findings_for_sample(_sample(zombies=2))[0]
        assert active.state == "active" and active.severity == "warning"
        assert active.kind == "process.zombies" and active.dedup_key == "count"
        cleared = findings_for_sample(_sample(zombies=0))[0]
        assert cleared.state == "clear"

    def test_read_gap_finding_is_the_explicit_permission_boundary(self) -> None:
        active = findings_for_sample(_sample(unreadable=3))[1]
        assert active.state == "active" and active.severity == "warning"
        assert active.kind == "process.read_gaps"
        assert "3 /proc entries unreadable" in active.summary
        cleared = findings_for_sample(_sample(unreadable=0))[1]
        assert cleared.state == "clear"

    def test_memory_hog_thresholds_on_the_bounded_listing(self) -> None:
        mib = 1024
        warn = findings_for_sample(
            _sample(processes=(_entry(pid=7, comm="hoarder", rss_kib=mib * mib),))
        )[2]
        assert warn.state == "active" and warn.severity == "warning"
        assert MEM_HOG_WARN_MIB == 1024.0
        crit = findings_for_sample(
            _sample(processes=(_entry(pid=7, comm="hoarder", rss_kib=4 * mib * mib),))
        )[2]
        assert crit.state == "active" and crit.severity == "critical"
        assert MEM_HOG_CRIT_MIB == 4096.0
        below = findings_for_sample(
            _sample(processes=(_entry(pid=7, comm="hoarder", rss_kib=mib * mib - 1),))
        )[2]
        assert below.state == "clear" and below.severity == "info"

    def test_hog_summary_names_comm_never_arguments(self) -> None:
        draft = findings_for_sample(
            _sample(processes=(_entry(pid=7, comm="hoarder", rss_kib=5 * 1024 * 1024),))
        )[2]
        assert "hoarder" in draft.summary
        assert ARGV_SENTINEL not in draft.summary
        assert len(draft.summary) <= SUMMARY_MAX_CHARS

    def test_hog_without_comm_names_the_pid(self) -> None:
        draft = findings_for_sample(
            _sample(processes=(_entry(pid=7, comm=None, rss_kib=5 * 1024 * 1024),))
        )[2]
        assert "pid 7" in draft.summary

    def test_missing_data_is_no_data_and_never_closes(self) -> None:
        drafts = findings_for_sample(
            _sample(
                quality=ObservationQuality.UNKNOWN,
                total_procs=None,
                zombies=None,
                unreadable=None,
                processes=(),
            )
        )
        assert all(d.state == "no_data" for d in drafts)
        # An empty listing with counts present is no_data for the hog only.
        drafts = findings_for_sample(_sample(processes=()))
        assert [d.state for d in drafts] == ["clear", "clear", "no_data"]

    def test_zero_rss_entries_are_not_hogs(self) -> None:
        drafts = findings_for_sample(_sample(processes=(_entry(rss_kib=0),)))
        assert drafts[2].state == "no_data"


# ---------------------------------------------------------------------------
# Transport boundary without docker: budgets, frozen surface, structural
# redaction and the no-mutation capability proof
# ---------------------------------------------------------------------------


def _identity(host: str = "203.0.113.10", port: int = 2222) -> SshIdentity:
    return SshIdentity(
        host=host,
        port=port,
        user="root",
        host_key="SHA256:" + "A" * 43,
        credential_ref="MAI_SSH_KEY_TEST",
    )


class TestStructuralRedactionAndNoMutation:
    def test_probe_never_reads_arguments_or_environment(self) -> None:
        # Acceptance 1: args are EXCLUDED structurally — the single remote
        # command cannot leak what it never reads.
        assert "cmdline" not in PROBE_COMMAND
        assert "environ" not in PROBE_COMMAND

    def test_probe_contains_no_process_mutation_primitive(self) -> None:
        for token in (
            "kill",
            "pkill",
            "renice",
            "reboot",
            "shutdown",
            "halt",
            "xargs",
            "rm ",
            "mv ",
            "chmod",
            "chown",
            "mkfs",
        ):
            assert token not in PROBE_COMMAND, token

    def test_module_exposes_no_process_mutation_capability(self) -> None:
        # Acceptance 2: monitoring must not confer kill capability. There is
        # no kill/signal/renice/stop entry point to call.
        forbidden = ("kill", "signal", "terminate", "stop", "renice", "pkill")
        mutating = [
            name
            for name in vars(processes_module)
            if not name.startswith("_") and any(t in name.lower() for t in forbidden)
        ]
        assert mutating == []
        for attempt in (
            "kill_process",
            "kill",
            "terminate",
            "signal_process",
            "stop_process",
            "renice_process",
        ):
            assert getattr(processes_module, attempt, None) is None

    def test_source_collector_identity_is_bounded(self) -> None:
        assert SOURCE == "process"
        assert len(SOURCE) <= SOURCE_MAX_CHARS


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
            probe_processes(
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
            probe_processes(
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
            probe_processes(
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
            probe_processes(
                _identity(),
                key_path=tmp_path / "k",
                known_hosts_path=pin,
                controller_now=NOW,
            )
        assert excinfo.value.code is expected


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
        # 4 count rows + 9 listed processes × (cpu_ticks + rss_kib + comm).
        assert written == 4 + 9 * 3
        rows = await _fetchall(
            sessions,
            "SELECT source, method, quality, metric, value, observed_at, "
            "collected_at, fresh_until, retain_until, seq "
            "FROM server_observations WHERE server_id = :s ORDER BY metric",
            s=uuid.UUID(SERVER_A),
        )
        assert len(rows) == written
        metrics = {r.metric: r.value for r in rows}
        assert metrics["proc.count"] == "7"
        assert metrics["proc.listed"] == "9"
        assert metrics["proc.zombies"] == "0"
        assert metrics["proc.unreadable"] == "0"
        assert metrics["proc.24.cpu_ticks"] == "220"
        assert metrics["proc.24.rss_kib"] == str(541 * RSS_KIB_PER_PAGE)
        assert metrics["proc.24.comm"] == "yes"
        assert metrics["proc.1.comm"] == "sshd"
        assert metrics["proc.25.comm"] == "sh"  # sentinel process, args absent
        assert "proc.svc_total" not in metrics  # rc=1 ⇒ counts UNKNOWN ≠ zero
        assert not any(m.startswith("proc.svc") for m in metrics)
        for r in rows:
            assert r.source == "process"
            assert r.method == "command"
            assert r.quality == "measured"
            assert r.observed_at == datetime(2026, 10, 17, 19, 32, 15, tzinfo=UTC)
            assert r.fresh_until == r.observed_at + timedelta(minutes=15)
            assert r.retain_until == NOW + RETENTION_HORIZON
            assert r.seq is not None  # schema-owned sequence stamped it

    @pytest.mark.anyio
    async def test_sentinel_never_reaches_storage(self, sessions) -> None:
        sample = parse_probe_output(_ARCH_FIXTURE_OUTPUT, controller_now=NOW)
        await record_sample(sessions, uuid.UUID(SERVER_A), sample, controller_now=NOW)
        assert ARGV_SENTINEL not in await _storage_dump(sessions)

    @pytest.mark.anyio
    async def test_service_inventory_persists_only_when_reported(
        self, sessions
    ) -> None:
        sample = _sample(
            services=(
                ServiceEntry(unit="sshd.service", sub="running"),
                ServiceEntry(unit="journald.service", sub="exited"),
            ),
            svc_total=2,
            svc_running=1,
            svc_failed=0,
            svc_exited=1,
        )
        await record_sample(sessions, uuid.UUID(SERVER_A), sample, controller_now=NOW)
        rows = await _fetchall(
            sessions,
            "SELECT metric, value FROM server_observations WHERE server_id = :s",
            s=uuid.UUID(SERVER_A),
        )
        metrics = {r.metric: r.value for r in rows}
        assert metrics["proc.svc_total"] == "2"
        assert metrics["proc.svc_running"] == "1"
        assert metrics["proc.svc_failed"] == "0"  # honest zero: query succeeded
        assert metrics["proc.svc_exited"] == "1"
        assert sorted(r.value for r in rows if r.metric == "proc.svc") == [
            "exited:journald.service",
            "running:sshd.service",
        ]

    @pytest.mark.anyio
    async def test_finding_lifecycle_open_update_close_reopen(self, sessions) -> None:
        t1 = NOW
        t2 = NOW + timedelta(minutes=1)
        t3 = NOW + timedelta(minutes=2)
        t4 = NOW + timedelta(minutes=3)
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(zombies=2), controller_now=t1
        )
        first = await _fetchone(
            sessions,
            "SELECT finding_id, severity, opened_at, updated_at, resolved_at "
            "FROM observation_findings WHERE server_id = :s "
            "AND kind = 'process.zombies'",
            s=uuid.UUID(SERVER_A),
        )
        assert first.severity == "warning" and first.resolved_at is None
        assert first.opened_at == t1 and first.updated_at == t1

        # Repeated poll: same condition collapses onto the same row.
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(zombies=3), controller_now=t2
        )
        rows = await _fetchall(
            sessions,
            "SELECT finding_id, opened_at, updated_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'process.zombies'",
            s=uuid.UUID(SERVER_A),
        )
        assert len(rows) == 1
        assert rows[0].finding_id == first.finding_id
        assert rows[0].opened_at == t1
        assert rows[0].updated_at == t2

        # Measured recovery closes; the row (ack-style evidence) survives.
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(zombies=0), controller_now=t3
        )
        closed = await _fetchone(
            sessions,
            "SELECT resolved_at FROM observation_findings WHERE server_id = :s "
            "AND kind = 'process.zombies'",
            s=uuid.UUID(SERVER_A),
        )
        assert closed.resolved_at == t3

        # Recurrence reopens the SAME row without resetting opened_at.
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(zombies=2), controller_now=t4
        )
        reopened = await _fetchone(
            sessions,
            "SELECT opened_at, updated_at, resolved_at, severity "
            "FROM observation_findings WHERE server_id = :s "
            "AND kind = 'process.zombies'",
            s=uuid.UUID(SERVER_A),
        )
        assert reopened.resolved_at is None
        assert reopened.opened_at == t1
        assert reopened.updated_at == t4 and reopened.severity == "warning"

    @pytest.mark.anyio
    async def test_read_gap_finding_tracks_the_permission_boundary(
        self, sessions
    ) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(unreadable=4), controller_now=NOW
        )
        finding = await _fetchone(
            sessions,
            "SELECT severity, summary FROM observation_findings "
            "WHERE server_id = :s AND kind = 'process.read_gaps'",
            s=uuid.UUID(SERVER_A),
        )
        assert finding.severity == "warning" and "4 /proc entries" in finding.summary
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(unreadable=0), controller_now=NOW
        )
        cleared = await _fetchone(
            sessions,
            "SELECT resolved_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'process.read_gaps'",
            s=uuid.UUID(SERVER_A),
        )
        assert cleared.resolved_at is not None

    @pytest.mark.anyio
    async def test_unknown_collection_never_touches_findings(self, sessions) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(zombies=2), controller_now=NOW
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
            "WHERE server_id = :s AND kind = 'process.zombies'",
            s=uuid.UUID(SERVER_A),
        )
        assert finding.resolved_at is None  # unknown never re-evaluates
        assert finding.updated_at == NOW  # untouched by the unknown path

    @pytest.mark.anyio
    async def test_unknown_with_a_metric_is_a_real_integrity_violation(
        self, sessions
    ) -> None:
        # The structural half of "unknown, not zero": the schema itself
        # refuses an unknown row that carries a measurement.
        with pytest.raises(IntegrityError):
            async with sessions() as session:
                await session.execute(
                    text(
                        "INSERT INTO server_observations (observation_id, server_id, "
                        "source, method, quality, metric, value, collected_at, "
                        "retain_until) VALUES (:oid, :sid, 'process', 'command', "
                        "'unknown', 'proc.count', '0', now(), now())"
                    ),
                    {"oid": uuid.uuid4(), "sid": uuid.UUID(SERVER_A)},
                )
                await session.commit()

    @pytest.mark.anyio
    async def test_two_servers_are_a_storage_boundary_and_freshness_derives(
        self, sessions
    ) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(total_procs=7), controller_now=NOW
        )
        await record_sample(
            sessions, uuid.UUID(SERVER_B), _sample(total_procs=9), controller_now=NOW
        )
        rows_a = await _fetchall(
            sessions,
            "SELECT metric, value FROM server_observations WHERE server_id = :s "
            "AND metric = 'proc.count'",
            s=uuid.UUID(SERVER_A),
        )
        rows_b = await _fetchall(
            sessions,
            "SELECT metric, value FROM server_observations WHERE server_id = :s "
            "AND metric = 'proc.count'",
            s=uuid.UUID(SERVER_B),
        )
        assert [r.value for r in rows_a] == ["7"]
        assert [r.value for r in rows_b] == ["9"]

        stored = await _fetchone(
            sessions,
            "SELECT quality, observed_at, fresh_until FROM server_observations "
            "WHERE server_id = :s AND metric = 'proc.count'",
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
# exclusive runtime-target fixture, unique 10-process namespace)
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-10-process-arch"
FIXTURE_IMAGE_TAG = "mai-fxt-10-process-arch:sshd"
FALLBACK_IMAGE_TAG = "mai-fxt-04-ssh-arch:sshd"  # identical reviewed recipe
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-10-process-tests")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_10_PROCESS_FIXTURE"


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
    async def test_real_sentinel_load_snapshot_is_measured_bounded_and_secret_free(
        self, arch_sshd: ArchSshdFixture, migrated_url: str, sessions
    ) -> None:
        # Gate scenario: spawn harmless fixture processes with a secret
        # sentinel argument, collect through actual SSH, confirm identity/load
        # and the ABSENCE of the sentinel; attempt ungranted process mutation.
        arch_sshd.start_argv_sentinel_and_load(ARGV_SENTINEL)
        before = arch_sshd.run_remote("ps -eo comm= | sort | uniq -c")
        assert "yes" in before  # burner alive pre-collection

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
        comms = {v for m, v in metrics.items() if m.endswith(".comm")}
        # Acceptance 1: source + timestamp on every row of a real snapshot.
        assert rows and all(
            r.source == "process" and r.method == "command" for r in rows
        )
        assert all(r.observed_at is not None for r in rows)
        drift = max(
            abs((r.observed_at - datetime.now(UTC)).total_seconds()) for r in rows
        )
        assert drift < 600  # target clock ≈ controller clock (shared host)
        # Identity: the burner is listed with its comm and REAL tick count.
        assert "yes" in comms
        burner = await _fetchone(
            sessions,
            "SELECT value FROM server_observations WHERE server_id = :s "
            "AND metric LIKE 'proc.%.cpu_ticks' AND value <> '0' "
            "ORDER BY CAST(value AS integer) DESC LIMIT 1",
            s=uuid.UUID(SERVER_A),
        )
        assert burner is not None and int(burner.value) > 0  # real load reading
        listed = metrics.get("proc.listed")
        assert listed is not None and int(listed) >= 2
        assert int(metrics["proc.count"]) >= 1
        assert int(metrics["proc.unreadable"]) == 0  # root sees the whole table
        assert all(len(c) <= 15 for c in comms)  # kernel-truncated comm only
        # Acceptance 2, structural half: no unknown row carries a measurement.
        assert all(
            r.metric is None and r.value is None and r.observed_at is None
            for r in rows
            if r.quality == "unknown"
        )
        # No secret sentinel from process args, anywhere in the domain.
        assert ARGV_SENTINEL not in await _storage_dump(sessions)

        # Ungranted process mutation is DENIED: no module surface can effect
        # it, and collection demonstrably killed nothing.
        for attempt in (
            "kill_process",
            "kill",
            "terminate",
            "signal_process",
            "stop_process",
            "renice_process",
        ):
            assert getattr(processes_module, attempt, None) is None
        after = arch_sshd.run_remote("ps -eo comm= | sort | uniq -c")
        assert "yes" in after  # burner STILL ALIVE after collection
        assert "sleep" in after  # sentinel shell's child STILL ALIVE

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
        # Acceptance 2: unavailable target ⇒ unknown, NOT a zero process count.
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
        assert violations.n == 0  # the "not zero" structural bound
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
