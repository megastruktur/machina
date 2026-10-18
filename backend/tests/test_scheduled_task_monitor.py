"""Focused tests for the scheduler inventory collector (card 10-cron, C04+C10).

Acceptance coverage (BRIEF):

1. **Read-only target scheduler inventory with timer/cron identity and
   next/last status where supported** — the fixed probe command
   (``PROBE_COMMAND``) inventories systemd timers (unit identity + next
   elapse/last trigger as epoch µs, from ``systemctl list-timers --all
   --no-pager --output=json`` whose column names and µs typing are pinned
   against the systemd ``table_to_json`` serializer), system crontabs
   (``/etc/crontab``, ``/etc/cron.d/*``), user crontabs (both spool
   layouts) and anacron (``/etc/anacrontab`` + ``/var/spool/anacron/*``).
   A real container capture (embedded verbatim below) parses into exact
   inventory counts and persists as bounded ``source='cron'``
   ``method='command'`` rows.
2. **Unsupported/missing scheduler distinguished; no edits to target
   scheduled tasks** — every scheduler source is three-valued: measured
   (possibly empty), absent (explicit markers, e.g. ``systemd=no`` /
   ``cron_present=False`` → the ``cron.no_scheduler`` info finding), or
   unknown (present but unreadable: ``tm_rc≠0`` on a systemd target ⇒
   timers UNKNOWN never zero + the ``cron.timer_query`` warning;
   unreadable files ⇒ ``cron.read_gaps`` + quality capped at degraded).
   No-edit is proven twice: STRUCTURALLY (the probe contains no
   ``crontab`` invocation — the binary can edit; presence is checked with
   ``command -v`` only — no systemctl mutation verb, no file-writing
   primitive, only stderr discards) and on the REAL boundary (the
   ``real_target`` fixture hashes every planted schedule file before and
   after collection over the actual SSH transport and asserts equality).
   MAI scheduler records (``job_schedules``/``job_schedule_fires``, the
   DBOS domain) remain separate: the collector writes ONLY
   ``server_observations``/``observation_findings``, asserted against a
   seeded MAI schedule row on the real schema and on the real fixture.

Secret hygiene: the gate sentinel is a synthetic marker string, NOT a
secret. It is planted ONLY in target-side fixture crontab command lines
and environment values; the probe pipes those suffixes straight into
``sha256sum`` on the target, so the negative controls assert the sentinel
appears in NO probe byte and NO storage row. Fixture keys are throwaway
files under ``/tmp/mai-fxt-10-cron-*``; the fixture ``.env`` holds a PATH
reference, never material.

Layers per the test policy: pure parse/finding/structural tests (always
run); transport-boundary tests over monkeypatched subprocess; persistence
claims on a real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``,
real Alembic chain to ``0007``); ``real_target``-marked tests against
disposable Arch sshd containers (``mai-fxt-10-cron-*`` namespace), auto-
skipped without docker and removed with recorded proof. The systemd timer
JSON parse fixture is schema-exact but synthetic: this dev runtime has no
systemd manager (runtime-environment ADR: coordinator host userland and
overlay containers run no systemd; booting systemd as container PID 1
fails at ``/init.scope`` cgroup creation), so the timer-source "present
but unreadable" shape (``tm_rc=1``) is exercised on the REAL container
(systemd package installed, sshd is PID 1) and the "measured next/last"
mapping on the documented serializer shape.
"""

from __future__ import annotations

import os
import re
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

import mai.monitoring.scheduled_tasks as scheduled_tasks_module

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
from mai.monitoring.scheduled_tasks import (
    MAX_ANACRON_LISTED,
    MAX_ENV_LISTED,
    MAX_JOBS_LISTED,
    MAX_PROBE_OUTPUT_BYTES,
    MAX_TIMERS_LISTED,
    PROBE_COMMAND,
    PROBE_TIMEOUT_SECONDS,
    RETENTION_HORIZON,
    SOURCE,
    AnacronEntry,
    AnacronSpoolEntry,
    CronEnvEntry,
    CronJobEntry,
    ScheduledTaskSample,
    TimerEntry,
    collect,
    findings_for_sample,
    parse_probe_output,
    probe_scheduled_tasks,
    record_sample,
    record_unavailable,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Frozen fixed fixture identities (canonical lowercase UUID strings, distinct
# from every other card's constants).
SERVER_A = "ac1b2c3d-4e5f-4a60-8b7c-9d0e1f2a3b4c"
SERVER_B = "bc2c3d4e-5f6a-4b70-9c8d-0e1f2a3b4c5d"

NOW = datetime(2026, 10, 17, 20, 30, 0, tzinfo=UTC)

#: Non-disclosure sentinel planted ONLY in fixture crontab command lines and
#: environment values on the real target (NOT a secret): no probe byte and
#: no observation row may ever contain it because those suffixes are hashed
#: target-side and never emitted.
SENTINEL = "mai-10-cron-sentinel-not-a-secret"

# Real probe output captured verbatim from the disposable Arch fixture
# container (2026-10-17, mai-fxt-10-cron-arch with the systemd package
# installed but sshd as PID 1): the manager is unreachable ⇒ tm_rc=1 ⇒ the
# timer source is UNKNOWN, while the planted cron/anacron inventory measures
# — the exact real-world "present-but-unreadable source ≠ zero" shape.
# Command lines and env values appear ONLY as 16-hex fingerprints.
_ARCH_FIXTURE_OUTPUT = (
    "ts=2026-10-17T20:23:01\n"
    "systemd=yes\n"
    "tm_rc=1\n"
    "hash=yes\n"
    "cron_cmd=no\n"
    "anacron_cmd=no\n"
    "syscrontab=yes\n"
    "anacron_tab=yes\n"
    "cron_d=yes\n"
    "spool=/var/spool/cron/crontabs\n"
    "spool=/var/spool/cron\n"
    "env=/etc/crontab:1 SHELL=77bafa9e3a8a092a\n"
    "env=/etc/crontab:2 PATH=33d64f7f93e12a11\n"
    "env=/etc/crontab:3 MAILTO=4813494d137e1631\n"
    "j_total=0\n"
    "e_total=3\n"
    "a_total=0\n"
    "malformed=0\n"
    "job=/etc/cron.d/mai-fxt-ping:1 user=root cmd=67ca26bbacbbd7b3 17 * * * *\n"
    "j_total=1\n"
    "e_total=0\n"
    "a_total=0\n"
    "malformed=0\n"
    "job=/etc/cron.d/mai-fxt-reboot:1 user=root cmd=8eae34acc0d300f2 @reboot\n"
    "j_total=1\n"
    "e_total=0\n"
    "a_total=0\n"
    "malformed=0\n"
    "env=/var/spool/cron/crontabs/root:1 MAILTO=12ae32cb1ec02d01\n"
    "env=/var/spool/cron/crontabs/root:2 FXT_SECRET_URL=eb820221c6dc9edc\n"
    "job=/var/spool/cron/crontabs/root:3 user=- cmd=5b7bdc38423ea01d 45 2 * * *\n"
    "j_total=1\n"
    "e_total=2\n"
    "a_total=0\n"
    "malformed=0\n"
    "an=/etc/anacrontab:1 7 10 mai-fxt cmd=37e4fad9d372c0d1\n"
    "j_total=0\n"
    "e_total=0\n"
    "a_total=1\n"
    "malformed=0\n"
    "anspool=mai-fxt 20261017\n"
    "unreadable=0\n"
    "files=5\n"
)

# Schema-exact timer JSON (systemd list-timers --output=json serializer:
# columns next/left/last/passed/unit/activates, TABLE_TIMESTAMP cells as
# epoch µs, null when unset, activates a string OR array). Synthetic because
# this runtime has no bootable systemd manager; field names/types verified
# against the systemd v257 source (systemctl-list-units.c output_timers_list
# + format-table.c table_data_to_json).
_TIMER_JSON = (
    '[{"next": 1760724720123456, "left": 2520000000, '
    '"last": 1760722200000000, "passed": 120000000, '
    '"unit": "logrotate.timer", "activates": "logrotate.service"}, '
    '{"next": null, "left": null, "last": null, "passed": null, '
    '"unit": "fstrim.timer", "activates": ["fstrim.service", "backup.service"]}]'
)


def _timer_fixture_output(
    *, rc: int = 0, tmj: str | None = _TIMER_JSON, truncated: bool = False
) -> str:
    lines = [
        "ts=2026-10-17T20:23:01",
        "systemd=yes",
        f"tm_rc={rc}",
    ]
    if rc == 0:
        lines.append(f"tm_truncated={'yes' if truncated else 'no'}")
        if tmj is not None:
            lines.append(f"tmj={tmj}")
    lines += [
        "hash=yes",
        "cron_cmd=no",
        "anacron_cmd=no",
        "syscrontab=no",
        "anacron_tab=no",
        "cron_d=missing",
        "j_total=0",
        "e_total=0",
        "a_total=0",
        "malformed=0",
        "unreadable=0",
        "files=0",
    ]
    return "\n".join(lines) + "\n"


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
        await session.execute(text("DELETE FROM job_schedule_fires"))
        await session.execute(text("DELETE FROM job_schedules"))
        await session.commit()
    await engine.dispose()


async def _fetchone(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).first()


async def _fetchall(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).all()


async def _storage_dump(sessions) -> str:
    """Every stored value of the observation + schedule domain, for audits."""
    parts = []
    for table in (
        "server_observations",
        "observation_findings",
        "notification_alerts",
        "job_schedules",
        "job_schedule_fires",
    ):
        rows = await _fetchall(sessions, f"SELECT * FROM {table}")  # noqa: S608 (fixed names)
        parts.extend(str(tuple(r)) for r in rows)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Sample builders (pure layer)
# ---------------------------------------------------------------------------


def _job(
    location: str = "/etc/cron.d/mai-fxt-ping:1",
    schedule: str = "17 * * * *",
    user: str | None = "root",
    cmd_sha256: str | None = "67ca26bbacbbd7b3",
) -> CronJobEntry:
    return CronJobEntry(
        location=location, schedule=schedule, user=user, cmd_sha256=cmd_sha256
    )


def _sample(**overrides: object) -> ScheduledTaskSample:
    fields: dict[str, object] = {
        "observed_at": NOW,
        "observed_at_source": "target",
        "quality": ObservationQuality.MEASURED,
        "systemd_present": True,
        "timers": (
            TimerEntry(
                unit="logrotate.timer",
                next_us=1760724720123456,
                last_us=1760722200000000,
                activates=("logrotate.service",),
            ),
        ),
        "timers_total": 1,
        "timers_query": "ok",
        "timer_json_truncated": False,
        "cron_cmd_present": False,
        "anacron_cmd_present": False,
        "system_crontab": True,
        "anacrontab": True,
        "cron_d": "yes",
        "spool_dirs": ("/var/spool/cron/crontabs",),
        "spool_unreadable": (),
        "cron_present": True,
        "jobs": (_job(),),
        "jobs_total": 3,
        "envs": (CronEnvEntry("/etc/crontab:1", "SHELL", "77bafa9e3a8a092a"),),
        "envs_total": 5,
        "anacron_jobs": (
            AnacronEntry("/etc/anacrontab:1", "7", "10", "mai-fxt", "37e4fad9d372c0d1"),
        ),
        "anacron_total": 1,
        "anacron_spool": (AnacronSpoolEntry("mai-fxt", "20261017"),),
        "unreadable": 0,
        "gap_files": (),
        "malformed": 0,
        "hash_supported": True,
    }
    fields.update(overrides)
    return ScheduledTaskSample(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure parsing: bounded target output → honest sample
# ---------------------------------------------------------------------------


class TestParseProbeOutput:
    def test_real_fixture_capture_parses_with_exact_inventory(self) -> None:
        sample = parse_probe_output(_ARCH_FIXTURE_OUTPUT, controller_now=NOW)
        assert sample.observed_at == datetime(2026, 10, 17, 20, 23, 1, tzinfo=UTC)
        assert sample.observed_at_source == "target"
        # systemd present, manager unreachable ⇒ timers UNKNOWN, never zero.
        assert sample.systemd_present is True
        assert sample.timers is None
        assert sample.timers_query == "rc=1"
        assert sample.quality is ObservationQuality.DEGRADED
        # Cron/anacron inventory measured exactly.
        assert sample.cron_present is True
        assert sample.cron_cmd_present is False
        assert sample.system_crontab is True
        assert sample.anacrontab is True
        assert sample.cron_d == "yes"
        assert sample.spool_dirs == (
            "/var/spool/cron/crontabs",
            "/var/spool/cron",
        )
        assert sample.hash_supported is True
        assert sample.unreadable == 0 and sample.malformed == 0
        assert sample.jobs_total == 3
        assert sample.jobs == (
            CronJobEntry(
                "/etc/cron.d/mai-fxt-ping:1", "17 * * * *", "root", "67ca26bbacbbd7b3"
            ),
            CronJobEntry(
                "/etc/cron.d/mai-fxt-reboot:1", "@reboot", "root", "8eae34acc0d300f2"
            ),
            CronJobEntry(
                "/var/spool/cron/crontabs/root:3",
                "45 2 * * *",
                None,
                "5b7bdc38423ea01d",
            ),
        )
        assert sample.envs_total == 5
        assert sample.envs is not None and len(sample.envs) == 5
        assert sample.envs[4] == CronEnvEntry(
            "/var/spool/cron/crontabs/root:2", "FXT_SECRET_URL", "eb820221c6dc9edc"
        )
        assert sample.anacron_total == 1
        assert sample.anacron_jobs == (
            AnacronEntry("/etc/anacrontab:1", "7", "10", "mai-fxt", "37e4fad9d372c0d1"),
        )
        assert sample.anacron_spool == (AnacronSpoolEntry("mai-fxt", "20261017"),)
        # The capture's command lines carry the sentinel ONLY as fingerprints
        # — the parser output cannot carry what the probe never emitted.
        assert SENTINEL not in str(sample.jobs) + str(sample.envs)

    def test_timer_json_maps_next_last_micros_and_activates(self) -> None:
        sample = parse_probe_output(_timer_fixture_output(), controller_now=NOW)
        assert sample.quality is ObservationQuality.MEASURED
        assert sample.timers_query == "ok"
        assert sample.timers_total == 2
        timers = sample.timers
        assert timers is not None and len(timers) == 2
        first, second = timers
        assert first.unit == "logrotate.timer"
        # Epoch µs → timezone-aware UTC datetimes (acceptance 1: next/last
        # where supported).
        assert first.next_elapse == datetime.fromtimestamp(
            1760724720123456 / 1_000_000, tz=UTC
        )
        assert first.last_trigger == datetime.fromtimestamp(
            1760722200000000 / 1_000_000, tz=UTC
        )
        assert first.activates == ("logrotate.service",)
        # JSON null (timer with no next elapse / last trigger) stays None —
        # never a fabricated zero.
        assert second.next_us is None and second.last_us is None
        assert second.next_elapse is None and second.last_trigger is None
        assert second.activates == ("fstrim.service", "backup.service")

    def test_empty_output_is_unknown_without_any_domain_data(self) -> None:
        sample = parse_probe_output("", controller_now=NOW)
        assert sample.quality is ObservationQuality.UNKNOWN
        assert sample.systemd_present is None
        assert sample.timers is None and sample.timers_query is None
        assert sample.cron_present is None
        assert sample.jobs is None and sample.envs is None
        assert sample.anacron_jobs is None
        assert sample.unreadable is None and sample.malformed is None

    def test_explicit_absence_is_measured_with_no_scheduler(self) -> None:
        output = (
            "ts=2026-10-17T20:23:01\n"
            "systemd=no\n"
            "hash=yes\n"
            "cron_cmd=no\n"
            "anacron_cmd=no\n"
            "syscrontab=no\n"
            "anacron_tab=no\n"
            "cron_d=missing\n"
            "j_total=0\ne_total=0\na_total=0\nmalformed=0\n"
            "unreadable=0\nfiles=0\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.quality is ObservationQuality.MEASURED
        assert sample.systemd_present is False
        assert sample.timers_query == "absent"
        assert sample.timers is None  # absent ≠ empty listing
        assert sample.cron_present is False
        assert sample.jobs == () and sample.envs == () and sample.anacron_jobs == ()

    def test_junk_values_become_none_never_zero(self) -> None:
        output = (
            "ts=not-a-date\n"
            "systemd=maybe\n"
            "hash=yes\n"
            "cron_cmd=yes\n"
            "syscrontab=yes\n"
            "anacron_tab=no\n"
            "cron_d=yes\n"
            "j_total=many\ne_total=0\na_total=0\nmalformed=oops\n"
            "unreadable=some\nfiles=0\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.observed_at == NOW
        assert sample.observed_at_source == "controller"
        assert sample.systemd_present is None
        assert sample.timers_query is None
        assert sample.jobs_total is None
        assert sample.malformed is None
        assert sample.unreadable is None
        assert sample.quality is ObservationQuality.UNKNOWN

    def test_scalars_first_occurrence_and_delta_counters_sum(self) -> None:
        output = (
            "ts=2026-10-17T20:23:01\nsystemd=no\nhash=yes\n"
            "cron_cmd=no\nsyscrontab=no\nanacron_tab=no\ncron_d=missing\n"
            "j_total=2\ne_total=1\na_total=0\nmalformed=1\n"
            "j_total=1\ne_total=0\na_total=3\nmalformed=2\n"
            "unreadable=0\nfiles=0\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.systemd_present is False  # first occurrence wins
        assert sample.jobs_total == 3  # deltas sum: 2 + 1
        assert sample.envs_total == 1
        assert sample.anacron_total == 3
        assert sample.malformed == 3

    def test_listings_capped_at_frozen_bounds_with_honest_totals(self) -> None:
        job_lines = [
            f"/etc/cron.d/f{i}:1 user=root cmd={'0' * 16} 17 * * * *"
            for i in range(MAX_JOBS_LISTED + 8)
        ]
        env_lines = [
            f"/etc/crontab:{i + 1} V{i}={'a' * 16}" for i in range(MAX_ENV_LISTED + 8)
        ]
        an_lines = [
            f"/etc/anacrontab:{i + 1} 7 5 job{i} cmd={'b' * 16}"
            for i in range(MAX_ANACRON_LISTED + 8)
        ]
        timer_rows = [
            '{"next": 1, "left": 1, "last": null, "passed": null, '
            f'"unit": "t{i}.timer", "activates": "t{i}.service"}}'.replace("}}", "}")
            for i in range(MAX_TIMERS_LISTED + 24)
        ]
        timer_json = "[" + ",".join(timer_rows) + "]"
        output = (
            _timer_fixture_output(tmj=timer_json)
            .replace("j_total=0", f"j_total={len(job_lines)}")
            .replace("e_total=0", f"e_total={len(env_lines)}")
            .replace("a_total=0", f"a_total={len(an_lines)}")
            + "\n".join(f"job={line}" for line in job_lines)
            + "\n"
            + "\n".join(f"env={line}" for line in env_lines)
            + "\n"
            + "\n".join(f"an={line}" for line in an_lines)
            + "\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.jobs is not None and len(sample.jobs) == MAX_JOBS_LISTED
        assert sample.jobs_total == MAX_JOBS_LISTED + 8  # honest full count
        assert sample.envs is not None and len(sample.envs) == MAX_ENV_LISTED
        assert sample.envs_total == MAX_ENV_LISTED + 8
        assert sample.anacron_jobs is not None
        assert len(sample.anacron_jobs) == MAX_ANACRON_LISTED
        assert sample.anacron_total == MAX_ANACRON_LISTED + 8
        assert sample.timers is not None
        assert len(sample.timers) == MAX_TIMERS_LISTED
        assert sample.timers_total == MAX_TIMERS_LISTED + 24

    def test_hostile_timer_json_is_unparseable_never_zero(self) -> None:
        # Not an array.
        sample = parse_probe_output(
            _timer_fixture_output(tmj='{"a": 1}'), controller_now=NOW
        )
        assert sample.timers is None and sample.timers_query == "unparseable"
        assert sample.quality is ObservationQuality.DEGRADED
        # An array whose EVERY row fails validation is equally unusable.
        hostile = (
            '[{"unit": "ok.timer", "next": "not-a-number", "last": null}, '
            '{"unit": "BAD UNIT!", "next": 1, "last": 2}]'
        )
        sample = parse_probe_output(
            _timer_fixture_output(tmj=hostile), controller_now=NOW
        )
        assert sample.timers is None and sample.timers_query == "unparseable"

    def test_partial_hostile_timer_rows_drop_but_others_survive(self) -> None:
        hostile_unit = "x" * 80
        mixed = (
            '[{"unit": "good.timer", "next": 1760724720123456, "last": null, '
            '"activates": ["a.service", 42, "b.service"]}, '
            f'{{"unit": "{hostile_unit}", "next": 1, "last": 1}}, '
            '{"next": 1, "last": 2}, "junk"]'
        )
        sample = parse_probe_output(
            _timer_fixture_output(tmj=mixed), controller_now=NOW
        )
        assert sample.timers_query == "ok"
        assert sample.timers == (
            TimerEntry(
                unit="good.timer",
                next_us=1760724720123456,
                last_us=None,
                activates=("a.service", "b.service"),
            ),
        )
        assert sample.timers_total == 4  # honest: one valid row of four

    def test_truncated_timer_json_forces_unknown_source(self) -> None:
        sample = parse_probe_output(
            _timer_fixture_output(truncated=True), controller_now=NOW
        )
        assert sample.timers is None
        assert sample.timers_query == "truncated"
        assert sample.timer_json_truncated is True
        assert sample.quality is ObservationQuality.DEGRADED

    def test_missing_tmj_with_rc0_is_unparseable(self) -> None:
        sample = parse_probe_output(
            _timer_fixture_output(rc=0, tmj=None), controller_now=NOW
        )
        assert sample.timers is None and sample.timers_query == "unparseable"

    def test_hash_gap_keeps_dash_markers_and_degrades(self) -> None:
        output = _ARCH_FIXTURE_OUTPUT.replace("hash=yes", "hash=no")
        for hex_hash in re.findall(r"cmd=([0-9a-f]{16})", output):
            output = output.replace(hex_hash, "-")
        for hex_hash in re.findall(
            r"(?:SHELL|PATH|MAILTO|FXT_SECRET_URL)=([0-9a-f]{16})", output
        ):
            output = output.replace(hex_hash, "-")
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.hash_supported is False
        assert sample.quality is ObservationQuality.DEGRADED
        assert sample.jobs is not None
        assert all(job.cmd_sha256 is None for job in sample.jobs)
        assert sample.envs is not None
        assert all(env.value_sha256 is None for env in sample.envs)
        assert sample.anacron_jobs is not None
        assert all(a.cmd_sha256 is None for a in sample.anacron_jobs)

    def test_read_gaps_and_malformed_cap_quality_at_degraded(self) -> None:
        output = (
            _timer_fixture_output()
            .replace(
                "unreadable=0\nfiles=0",
                "unreadable=2\nfiles=2\ngap_file=/var/spool/cron/crontabs/daemon\ngap_file=/var/spool/anacron/x",
            )
            .replace("malformed=0", "malformed=4")
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.quality is ObservationQuality.DEGRADED
        assert sample.unreadable == 2
        assert sample.malformed == 4
        assert sample.gap_files == (
            "/var/spool/cron/crontabs/daemon",
            "/var/spool/anacron/x",
        )

    def test_spool_unreadable_is_explicit_and_degrades(self) -> None:
        output = _timer_fixture_output() + "spool_unreadable=/var/spool/cron\n"
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.spool_unreadable == ("/var/spool/cron",)
        assert sample.quality is ObservationQuality.DEGRADED
        assert sample.cron_present is True  # a marker answered: layout exists

    def test_malformed_inventory_lines_are_dropped_not_stored(self) -> None:
        output = (
            _timer_fixture_output()
            + "job=nonsense\n"
            + "job=/ok:1 user=root cmd=zzzz 17 * * * *\n"  # bad hash
            + "job=/ok:2 user=root cmd=0123456789abcdef Bad!Schedule\n"
            + "env=/ok:3 9BAD=x\n"  # invalid env name
            + "an=/ok:4 x 5 id cmd=0123456789abcdef\n"  # bad period
            + "anspool=** 20261017\n"  # invalid id charset: dropped
            + "anspool=mai-fxt2 not-a-stamp\n"  # valid id, unparsable stamp
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.jobs == () and sample.envs == () and sample.anacron_jobs == ()
        assert sample.anacron_spool == (AnacronSpoolEntry("mai-fxt2", None),)

    def test_crlf_and_leading_whitespace_tolerated(self) -> None:
        output = (
            "ts=2026-10-17T20:23:01\r\n"
            "systemd=no\r\n"
            "hash=yes\r\n"
            "cron_cmd=no\r\nsyscrontab=no\r\nanacron_tab=no\r\ncron_d=missing\r\n"
            "j_total=0\r\ne_total=0\r\na_total=0\r\nmalformed=0\r\n"
            "unreadable=0\r\nfiles=0\r\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.observed_at_source == "target"
        assert sample.quality is ObservationQuality.MEASURED


# ---------------------------------------------------------------------------
# Pure findings: inventory conditions, three-valued
# ---------------------------------------------------------------------------


class TestFindingsForSample:
    def test_read_gaps_lifecycle_none_zero_many(self) -> None:
        no_data = next(
            d
            for d in findings_for_sample(_sample(unreadable=None))
            if d.kind == "cron.read_gaps"
        )
        assert no_data.state == "no_data"
        clear = next(
            d
            for d in findings_for_sample(_sample(unreadable=0))
            if d.kind == "cron.read_gaps"
        )
        assert clear.state == "clear" and clear.severity == "info"
        active = next(
            d
            for d in findings_for_sample(_sample(unreadable=2))
            if d.kind == "cron.read_gaps"
        )
        assert active.state == "active" and active.severity == "warning"
        assert "2 scheduler files unreadable" in active.summary

    def test_timer_query_active_only_when_systemd_present_unreadable(self) -> None:
        active = next(
            d
            for d in findings_for_sample(_sample(timers=None, timers_query="rc=1"))
            if d.kind == "cron.timer_query"
        )
        assert active.state == "active" and active.severity == "warning"
        assert "rc=1" in active.summary
        measured = next(
            d for d in findings_for_sample(_sample()) if d.kind == "cron.timer_query"
        )
        assert measured.state == "clear"
        absent = next(
            d
            for d in findings_for_sample(
                _sample(systemd_present=False, timers_query="absent")
            )
            if d.kind == "cron.timer_query"
        )
        assert absent.state == "clear"  # absent is an answer, not a failure
        unknown_systemd = next(
            d
            for d in findings_for_sample(
                _sample(systemd_present=None, timers_query=None)
            )
            if d.kind == "cron.timer_query"
        )
        assert unknown_systemd.state == "no_data"

    def test_no_scheduler_active_only_for_explicit_double_absence(self) -> None:
        active = next(
            d
            for d in findings_for_sample(
                _sample(
                    systemd_present=False, timers_query="absent", cron_present=False
                )
            )
            if d.kind == "cron.no_scheduler"
        )
        assert active.state == "active" and active.severity == "info"
        assert "explicit absence" in active.summary
        clear = next(
            d for d in findings_for_sample(_sample()) if d.kind == "cron.no_scheduler"
        )
        assert clear.state == "clear"
        unknown = next(
            d
            for d in findings_for_sample(_sample(systemd_present=None))
            if d.kind == "cron.no_scheduler"
        )
        assert unknown.state == "no_data"

    def test_summaries_carry_counts_and_reasons_never_inventory_text(self) -> None:
        drafts = findings_for_sample(
            _sample(
                jobs=(_job(location="/etc/cron.d/secret-file:1", schedule="@reboot"),),
                timers=None,
                timers_query="rc=1",
                unreadable=7,
            )
        )
        joined = " | ".join(d.summary for d in drafts if d.summary)
        assert "/etc/cron.d/secret-file" not in joined
        assert "@reboot" not in joined
        assert len(joined) < SUMMARY_MAX_CHARS * len(drafts)


# ---------------------------------------------------------------------------
# Structural redaction and the no-edit guarantee (acceptance 2)
# ---------------------------------------------------------------------------


def _identity(host: str = "203.0.113.10", port: int = 2222) -> SshIdentity:
    return SshIdentity(
        host=host,
        port=port,
        user="root",
        host_key="SHA256:" + "A" * 43,
        credential_ref="MAI_SSH_KEY_TEST",
    )


class TestStructuralRedactionAndNoEdit:
    def test_probe_never_invokes_the_crontab_binary(self) -> None:
        # `crontab` can edit spools; presence is probed with `command -v`
        # only. No invocation form exists: no flag call, no substituted
        # call, no command-position call.
        assert "crontab -" not in PROBE_COMMAND
        assert "crontab -l" not in PROBE_COMMAND
        assert "$(crontab" not in PROBE_COMMAND
        assert re.search(r"(?:^|;|\|\||&&)\s*crontab[\s\"']", PROBE_COMMAND) is None
        assert PROBE_COMMAND.count("command -v crontab") == 1

    def test_probe_contains_no_scheduler_mutation_verb(self) -> None:
        assert PROBE_COMMAND.count("systemctl") == 2  # presence check + list-timers
        for verb in (
            "start",
            "stop",
            "restart",
            "reload",
            "enable",
            "disable",
            "mask",
            "unmask",
            "kill",
            "isolate",
            "set-default",
            "daemon-reload",
            "freeze",
            "thaw",
            "clean",
        ):
            assert f"systemctl {verb}" not in PROBE_COMMAND, verb
        # The anacron binary is only ever probed, never invoked with flags.
        assert PROBE_COMMAND.count("command -v anacron") == 1
        assert "anacron -" not in PROBE_COMMAND
        # The only systemctl query is the read-only timer listing.
        assert "systemctl list-timers --all --no-pager --output=json" in PROBE_COMMAND

    def test_probe_has_no_file_writing_primitive(self) -> None:
        for token in (
            "touch",
            "tee ",
            "mkfifo",
            "dd ",
            "cp ",
            "rm ",
            "mv ",
            "chmod",
            "chown",
            "ln ",
            "truncate ",
            "install -",
            ">>",
            "> /",
            '>"',
            "eval ",
            "`",
        ):
            assert token not in PROBE_COMMAND, token
        # Every /dev/null use is a self-output discard: a stderr redirect
        # (2>/dev/null) or a presence-check stdout+stderr discard
        # (>/dev/null 2>&1). No capture, no file write.
        assert PROBE_COMMAND.count("/dev/null") == (
            PROBE_COMMAND.count("2>/dev/null") + PROBE_COMMAND.count(">/dev/null 2>&1")
        )
        # The redaction wiring is exact: command/value fields (J.f5, E.f4,
        # A.f6) are the ONLY content fields, and each goes straight into the
        # target-side hashof — never into a printf verbatim argument list.
        assert (
            'printf \'job=%s user=%s cmd=%s %s\\n\' "$f2" "$f4" '
            '"$(hashof "$f5")" "$f3"' in PROBE_COMMAND
        )
        assert (
            'printf \'env=%s %s=%s\\n\' "$f2" "$f3" "$(hashof "$f4")"' in PROBE_COMMAND
        )
        assert (
            'printf \'an=%s %s %s %s cmd=%s\\n\' "$f2" "$f3" "$f4" "$f5" '
            '"$(hashof "$f6")"' in PROBE_COMMAND
        )

    def test_probe_reads_only_the_frozen_path_set(self) -> None:
        # The exact target paths the probe may touch — nothing outside them.
        paths = {
            p.rstrip("/")
            for p in re.findall(r"/(?:etc|var)/[A-Za-z0-9/_.-]*", PROBE_COMMAND)
        }
        allowed = {
            "/etc/crontab",
            "/etc/anacrontab",
            "/etc/cron.d",
            "/var/spool/cron/crontabs",
            "/var/spool/cron",
            "/var/spool/anacron",
        }
        assert paths <= allowed, paths - allowed

    def test_module_exposes_no_scheduler_mutation_capability(self) -> None:
        forbidden = (
            "install",
            "uninstall",
            "write",
            "edit",
            "apply",
            "enable",
            "disable",
            "remove",
            "delete",
            "create_schedule",
        )
        mutating = [
            name
            for name in vars(scheduled_tasks_module)
            if not name.startswith("_") and any(t in name.lower() for t in forbidden)
        ]
        assert mutating == []
        for attempt in (
            "install_cron_job",
            "remove_cron_job",
            "write_crontab",
            "edit_timer",
            "enable_timer",
            "create_schedule",
        ):
            assert getattr(scheduled_tasks_module, attempt, None) is None

    def test_source_collector_identity_is_bounded(self) -> None:
        assert SOURCE == "cron"
        assert len(SOURCE) <= SOURCE_MAX_CHARS


# ---------------------------------------------------------------------------
# Transport boundary without docker: budgets, frozen surface
# ---------------------------------------------------------------------------


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
            probe_scheduled_tasks(
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
            probe_scheduled_tasks(
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
            probe_scheduled_tasks(
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
            probe_scheduled_tasks(
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
    async def test_real_fixture_sample_persists_bounded_rows(self, sessions) -> None:
        sample = parse_probe_output(_ARCH_FIXTURE_OUTPUT, controller_now=NOW)
        written = await record_sample(
            sessions, uuid.UUID(SERVER_A), sample, controller_now=NOW
        )
        rows = await _fetchall(
            sessions,
            "SELECT metric, value, source, method, quality, observed_at, seq "
            "FROM server_observations WHERE server_id = :sid ORDER BY seq",
            sid=uuid.UUID(SERVER_A),
        )
        assert written == len(rows)
        assert rows
        metrics = {r.metric: r.value for r in rows}
        assert {r.source for r in rows} == {SOURCE}
        assert {r.method for r in rows} == {"command"}
        assert {r.quality for r in rows} == {"degraded"}
        assert {r.observed_at for r in rows} == {
            datetime(2026, 10, 17, 20, 23, 1, tzinfo=UTC)
        }
        assert all(row.seq is not None for row in rows)  # schema-owned sequence
        assert metrics["cron.systemd"] == "yes"
        assert metrics["cron.timers_query"] == "rc=1"
        assert "cron.timers_total" not in metrics  # unknown, never zero
        assert metrics["cron.jobs_total"] == "3"
        assert metrics["cron.jobs_listed"] == "3"
        assert metrics["cron.env_total"] == "5"
        assert metrics["cron.anacron_total"] == "1"
        assert metrics["cron.present"] == "yes"
        assert metrics["cron.hash"] == "ok"
        assert metrics["cron.unreadable"] == "0"
        assert metrics["cron.malformed"] == "0"
        job_values = [
            r.value
            for r in await _fetchall(
                sessions,
                "SELECT value FROM server_observations WHERE metric = 'cron.job' "
                "AND server_id = :sid ORDER BY seq",
                sid=uuid.UUID(SERVER_A),
            )
        ]
        assert job_values == [
            "/etc/cron.d/mai-fxt-ping:1 user=root cmd=67ca26bbacbbd7b3 17 * * * *",
            "/etc/cron.d/mai-fxt-reboot:1 user=root cmd=8eae34acc0d300f2 @reboot",
            "/var/spool/cron/crontabs/root:3 user=- cmd=5b7bdc38423ea01d 45 2 * * *",
        ]
        # All bounded renderings fit the 0007 column CHECKs.
        assert all(len(r.metric) <= 64 and len(r.value) <= 512 for r in rows)
        # Inventory findings: only ACTIVE drafts persist. The degraded sample
        # has rc=1 timers on a systemd target (active warning); unreadable=0
        # and a present scheduler CLOSE their conditions — closing a
        # never-opened row is a no-op, so exactly one finding row exists.
        findings = await _fetchall(
            sessions,
            "SELECT kind, severity, resolved_at FROM observation_findings "
            "WHERE server_id = :sid",
            sid=uuid.UUID(SERVER_A),
        )
        assert [(f.kind, f.severity, f.resolved_at is None) for f in findings] == [
            ("cron.timer_query", "warning", True)
        ]

    @pytest.mark.anyio
    async def test_timer_rows_carry_next_last_identity(self, sessions) -> None:
        sample = parse_probe_output(_timer_fixture_output(), controller_now=NOW)
        await record_sample(sessions, uuid.UUID(SERVER_A), sample, controller_now=NOW)
        timer_rows = await _fetchall(
            sessions,
            "SELECT value FROM server_observations WHERE metric = 'cron.timer' "
            "AND server_id = :sid",
            sid=uuid.UUID(SERVER_A),
        )
        values = [r.value for r in timer_rows]
        assert values == [
            "logrotate.timer next=1760724720123456 last=1760722200000000 "
            "activates=logrotate.service",
            "fstrim.timer next=- last=- activates=fstrim.service,backup.service",
        ]

    @pytest.mark.anyio
    async def test_unknown_with_metric_is_structurally_forbidden(
        self, sessions
    ) -> None:
        with pytest.raises(IntegrityError):
            async with sessions() as session:
                await session.execute(
                    text(
                        "INSERT INTO server_observations (observation_id, "
                        "server_id, source, method, quality, metric, value, "
                        "collected_at, retain_until) VALUES (:oid, :sid, 'cron', "
                        "'command', 'unknown', 'cron.jobs_total', '0', :now, :ret)"
                    ),
                    {
                        "oid": uuid.uuid4(),
                        "sid": uuid.UUID(SERVER_A),
                        "now": NOW,
                        "ret": NOW + RETENTION_HORIZON,
                    },
                )
                await session.commit()

    @pytest.mark.anyio
    async def test_record_unavailable_writes_single_measurement_free_row(
        self, sessions
    ) -> None:
        written = await record_unavailable(
            sessions, uuid.UUID(SERVER_A), controller_now=NOW
        )
        assert written == 1
        row = await _fetchone(
            sessions,
            "SELECT quality, metric, value, observed_at, fresh_until, "
            "retain_until FROM server_observations WHERE server_id = :sid",
            sid=uuid.UUID(SERVER_A),
        )
        assert row.quality == "unknown"
        assert row.metric is None and row.value is None and row.observed_at is None
        assert row.fresh_until is None
        assert row.retain_until == NOW + RETENTION_HORIZON

    @pytest.mark.anyio
    async def test_missing_data_never_closes_finding(self, sessions) -> None:
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(unreadable=2),
            controller_now=NOW,
        )
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(unreadable=None),
            controller_now=NOW,
        )
        row = await _fetchone(
            sessions,
            "SELECT resolved_at FROM observation_findings WHERE server_id = :sid "
            "AND kind = 'cron.read_gaps'",
            sid=uuid.UUID(SERVER_A),
        )
        assert row.resolved_at is None  # still open

    @pytest.mark.anyio
    async def test_finding_lifecycle_open_close_reopen_single_row(
        self, sessions
    ) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(unreadable=2), controller_now=NOW
        )
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(unreadable=0), controller_now=NOW
        )
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(unreadable=5), controller_now=NOW
        )
        rows = await _fetchall(
            sessions,
            "SELECT severity, resolved_at FROM observation_findings WHERE "
            "server_id = :sid AND kind = 'cron.read_gaps'",
            sid=uuid.UUID(SERVER_A),
        )
        assert len(rows) == 1  # dedup on (server_id, kind, dedup_key)
        assert rows[0].severity == "warning"
        assert rows[0].resolved_at is None  # reopened

    @pytest.mark.anyio
    async def test_two_server_boundary(self, sessions) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(unreadable=2), controller_now=NOW
        )
        await record_sample(
            sessions, uuid.UUID(SERVER_B), _sample(unreadable=0), controller_now=NOW
        )
        rows = await _fetchall(
            sessions,
            "SELECT server_id, resolved_at FROM observation_findings WHERE "
            "kind = 'cron.read_gaps' ORDER BY server_id",
        )
        # SERVER_A's gap finding is open; SERVER_B recorded zero unreadable —
        # its clear draft closed nothing (no row was ever open).
        assert [(r.server_id, r.resolved_at is None) for r in rows] == [
            (uuid.UUID(SERVER_A), True)
        ]

    @pytest.mark.anyio
    async def test_mai_schedules_remain_separate(self, sessions) -> None:
        # Seed the MAI/DBOS schedule domain (migration 0004): one schedule
        # for the observed server, one for another server.
        schedule_a = uuid.uuid5(uuid.NAMESPACE_URL, f"{SERVER_A}-mai-schedule")
        schedule_b = uuid.uuid5(uuid.NAMESPACE_URL, f"{SERVER_B}-mai-schedule")
        async with sessions() as session:
            await session.execute(
                text(
                    "INSERT INTO job_schedules (schedule_id, server_id, cron, "
                    "run_spec) VALUES (:sid1, :s1, '0 3 * * *', CAST(:spec AS jsonb)), "
                    "(:sid2, :s2, '*/10 * * * *', CAST(:spec AS jsonb))"
                ),
                {
                    "sid1": schedule_a,
                    "s1": uuid.UUID(SERVER_A),
                    "sid2": schedule_b,
                    "s2": uuid.UUID(SERVER_B),
                    "spec": '{"kind": "maintenance", "step_refs": []}',
                },
            )
            await session.commit()

        # Collection (both paths) must not touch the schedule domain.
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        await record_unavailable(sessions, uuid.UUID(SERVER_B), controller_now=NOW)
        schedules = await _fetchall(
            sessions,
            "SELECT schedule_id, server_id, cron FROM job_schedules ORDER BY cron",
        )
        assert [(s.schedule_id, s.server_id, s.cron) for s in schedules] == [
            (schedule_a, uuid.UUID(SERVER_A), "0 3 * * *"),
            (schedule_b, uuid.UUID(SERVER_B), "*/10 * * * *"),
        ]
        fires = await _fetchall(sessions, "SELECT * FROM job_schedule_fires")
        assert fires == []
        # The observation domain is the ONLY place collector rows live.
        domains = await _fetchall(
            sessions,
            "SELECT DISTINCT source FROM server_observations",
        )
        assert [d.source for d in domains] == [SOURCE]

    @pytest.mark.anyio
    async def test_storage_never_carries_command_material(self, sessions) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        dump = await _storage_dump(sessions)
        assert SENTINEL not in dump
        # Fingerprints are 16 hex chars — never a reconstructible command.
        assert "curl" not in dump and "https://" not in dump

    @pytest.mark.anyio
    async def test_freshness_derivation_shared(self, sessions) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        row = await _fetchone(
            sessions,
            "SELECT quality, observed_at, fresh_until FROM server_observations "
            "WHERE server_id = :sid LIMIT 1",
            sid=uuid.UUID(SERVER_A),
        )
        assert (
            derive_freshness(
                row.quality,
                row.observed_at,
                row.fresh_until,
                NOW + timedelta(minutes=5),
            )
            is ObservationFreshness.FRESH
        )
        assert (
            derive_freshness(
                row.quality,
                row.observed_at,
                row.fresh_until,
                NOW + timedelta(hours=2),
            )
            is ObservationFreshness.STALE
        )


# ---------------------------------------------------------------------------
# Real disposable Arch fixture (unique 10-cron namespace)
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-10-cron-arch"
FIXTURE_IMAGE_TAG = "mai-fxt-10-cron-arch:sshd"
BARE_CONTAINER = "mai-fxt-10-cron-plain"
BARE_IMAGE_TAG = "mai-fxt-10-cron-arch:sshd-bare"
# NO fallback tag: the shared 04-ssh fixture image has since been rebuilt by
# sibling cards WITH the systemd package inside (provenance drift in the
# shared image cache), so tagging from it would silently turn the "bare"
# target into a systemd-layout target. Both images are built from their own
# Dockerfiles under this card's namespace instead.
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-10-cron-tests")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_10_CRON_FIXTURE"

_SYSTEMD_DOCKERFILE = (
    "FROM archlinux:latest\n"
    "RUN pacman-key --init && pacman -Sy --noconfirm openssh systemd "
    "&& ssh-keygen -A\n"
)
_BARE_DOCKERFILE = (
    "FROM archlinux:latest\n"
    "RUN pacman-key --init && pacman -Sy --noconfirm openssh && ssh-keygen -A\n"
)

_SSHD_COMMAND = (
    "rm -f /etc/ssh/ssh_host_* && ssh-keygen -A "
    "&& mkdir -p /var/empty /run/sshd && exec /usr/bin/sshd -D"
)

# The bare target emulates the real no-systemd-tools target class (busybox
# containers): current Arch `base` ships the systemd package, so the start
# command REMOVES the systemctl binary and every cron-family layout inside
# the disposable container BEFORE sshd starts — fixture setup shaping the
# target, never a probe action. rm -f guards keep it drift-proof.
_BARE_SSHD_COMMAND = (
    "rm -f /usr/bin/systemctl /etc/crontab /etc/anacrontab; "
    "rm -rf /etc/cron.d /var/spool/cron /var/spool/anacron; " + _SSHD_COMMAND
)

# The benign fixture schedules planted inside the disposable container
# (command lines deliberately carry the NOT-A-SECRET sentinel).
_PLANT_SCRIPT = f"""
mkdir -p /etc/cron.d /var/spool/cron/crontabs /var/spool/anacron
printf 'SHELL=/bin/sh\\nPATH=/usr/local/sbin:/usr/local/bin:/sbin:/usr/bin\\nMAILTO=root\\n' > /etc/crontab
printf '17 * * * * root curl -sf -H "X-Api-Key: {SENTINEL}" https://fixture.invalid/ping >/dev/null 2>&1 # fxt\\n' > /etc/cron.d/mai-fxt-ping
printf '@reboot root logger -t mai-fxt reboot-marker\\n' > /etc/cron.d/mai-fxt-reboot
printf 'MAILTO=""\\nFXT_SECRET_URL=https://user:{SENTINEL}@fixture.invalid\\n45 2 * * * /usr/local/bin/mai-fxt-backup.sh --token {SENTINEL}\\n' > /var/spool/cron/crontabs/root
printf '7 10 mai-fxt /usr/bin/true --secret {SENTINEL} # fxt\\n' > /etc/anacrontab
echo 20261017 > /var/spool/anacron/mai-fxt
"""

_INVENTORY_ATTEST = (
    "sha256sum /etc/crontab /etc/cron.d/mai-fxt-ping /etc/cron.d/mai-fxt-reboot "
    "/var/spool/cron/crontabs/root /etc/anacrontab /var/spool/anacron/mai-fxt; "
    "ls -la /etc/cron.d /var/spool/cron/crontabs /var/spool/anacron"
)


def _docker(*args: str, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _ensure_fixture_image(tag: str, dockerfile: str) -> str:
    if _docker("image", "inspect", tag).returncode == 0:
        return "own tag present"
    context = FIXTURE_SCRATCH / ("build-" + tag.replace(":", "-").replace("/", "-"))
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    build = _docker("build", "-q", "-t", tag, str(context))
    assert build.returncode == 0, f"fixture image build failed: {build.stderr[-800:]}"
    return f"built from archlinux:latest ({tag.split(':')[0]} own recipe)"


class ArchSshdFixture:
    """One disposable Arch sshd container; removes itself with proof.

    ``with_systemd=True`` bakes the systemd package into the image so
    ``systemctl`` exists while sshd stays PID 1 — the real-world "systemd
    layout present, manager not running" shape (tm_rc=1).
    """

    def __init__(self, *, name: str, tag: str, with_systemd: bool) -> None:
        self.name = name
        self.tag = tag
        self.with_systemd = with_systemd
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
                f"{self.name}-test",
            ],
            check=True,
            capture_output=True,
        )
        os.chmod(self.key_path, 0o600)
        if self.with_systemd:
            self.image_provenance = _ensure_fixture_image(self.tag, _SYSTEMD_DOCKERFILE)
        else:
            self.image_provenance = _ensure_fixture_image(self.tag, _BARE_DOCKERFILE)
        run = _docker(
            "run",
            "--rm",
            "-d",
            "--name",
            self.name,
            "--cpus=2",
            "--memory=2g",
            "-p",
            "127.0.0.1::22",
            self.tag,
            "sh",
            "-c",
            _SSHD_COMMAND if self.with_systemd else _BARE_SSHD_COMMAND,
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
            self.name,
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
            self.name,
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
                f"{self.name}-unauthorized",
            ],
            check=True,
            capture_output=True,
        )
        os.chmod(wrong_key, 0o600)
        env_file = self.scratch / "unauthorized.env"
        env_file.write_text(f"{FIXTURE_CREDENTIAL_REF}={wrong_key}\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        return EnvResolver(env_file)

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

    def plant_fixture_schedules(self) -> None:
        proc = _docker("exec", self.name, "sh", "-c", _PLANT_SCRIPT)
        assert proc.returncode == 0, proc.stderr
        # Sanity: the probe path sees a systemd layout with no manager.
        check = _docker("exec", self.name, "sh", "-c", "command -v systemctl")
        assert check.returncode == 0, check.stderr

    def inventory_attestation(self) -> str:
        return self.run_remote(_INVENTORY_ATTEST)

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

    def remove_container(self) -> str:
        """Remove ONLY the container; scratch files survive (disconnect
        tests collect through the dead endpoint while key material still
        resolves)."""
        _docker("rm", "-f", self.name, timeout=60)
        listing = _docker(
            "ps",
            "-a",
            "--filter",
            f"name=^{self.name}$",
            "--format",
            "{{.Names}}",
        )
        return f"container {self.name} after rm: '{listing.stdout.strip() or '<none>'}'"

    def cleanup(self) -> str:
        self.remove_container()
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.cleanup_proof = (
            f"container {self.name} removed; scratch {self.scratch} removed: "
            f"{not self.scratch.exists()}"
        )
        return self.cleanup_proof


def _start_fixture(**kwargs: bool) -> ArchSshdFixture:
    fixture = ArchSshdFixture(**kwargs)
    fixture.identity = fixture.start()
    fixture.enroll()
    return fixture


@pytest.mark.real_target
class TestAgainstArchFixture:
    @pytest.mark.anyio
    async def test_gate_scenario_fixture_schedules_collected_redacted_untouched(
        self, migrated_url: str, sessions
    ) -> None:
        if not _docker_available():
            pytest.skip("docker unavailable: Arch sshd fixture tests require it")
        fixture = _start_fixture(
            name=FIXTURE_CONTAINER, tag=FIXTURE_IMAGE_TAG, with_systemd=True
        )
        try:
            fixture.plant_fixture_schedules()
            before = fixture.inventory_attestation()
            # Positive plant check: the attestation lists every planted
            # schedule file (its sha256sum output never carries command
            # text — that is the whole point).
            for planted in (
                "/etc/crontab",
                "/etc/cron.d/mai-fxt-ping",
                "/etc/cron.d/mai-fxt-reboot",
                "/var/spool/cron/crontabs/root",
                "/etc/anacrontab",
                "/var/spool/anacron/mai-fxt",
            ):
                assert planted in before, planted
            # The sentinel lives in the planted FILES (raw command lines).
            assert SENTINEL in fixture.run_remote(
                "grep -h 'sentinel' /etc/cron.d/mai-fxt-ping "
                "/var/spool/cron/crontabs/root /etc/anacrontab"
            )

            # Seed the MAI schedule domain for the SAME server: it must be
            # untouched by collection (schedulers observed separately).
            schedule_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{SERVER_A}-mai-schedule")
            async with sessions() as session:
                await session.execute(
                    text(
                        "INSERT INTO job_schedules (schedule_id, server_id, cron, "
                        "run_spec) VALUES (:sid, :s, '30 4 * * *', CAST(:spec AS jsonb))"
                    ),
                    {
                        "sid": schedule_id,
                        "s": uuid.UUID(SERVER_A),
                        "spec": '{"kind": "maintenance", "step_refs": []}',
                    },
                )
                await session.commit()

            resolver = fixture.env_resolver()
            key_path = resolve_identity_key(resolver, fixture.identity.credential_ref)
            result = await collect(
                sessions,
                fixture.identity,
                key_path=key_path,
                known_hosts_path=fixture.known_hosts_path,
                server_id=uuid.UUID(SERVER_A),
                controller_now=datetime.now(UTC),
            )
            assert result.error is None
            # systemd layout present (systemctl exists) but the manager does
            # not run in the container ⇒ timer source UNKNOWN, never zero;
            # the cron/anacron inventory MEASURES with the planted entries.
            assert result.quality is ObservationQuality.DEGRADED
            rows = await _fetchall(
                sessions,
                "SELECT metric, value FROM server_observations WHERE server_id = :sid",
                sid=uuid.UUID(SERVER_A),
            )
            metrics = {r.metric: r.value for r in rows}
            assert metrics["cron.timers_query"] == "rc=1"
            assert "cron.timers_total" not in metrics
            assert metrics["cron.jobs_total"] == "3"
            assert metrics["cron.jobs_listed"] == "3"
            assert metrics["cron.env_total"] == "5"
            assert metrics["cron.anacron_total"] == "1"
            spool_values = {r.value for r in rows if r.metric == "cron.spool"}
            assert spool_values == {"/var/spool/cron/crontabs", "/var/spool/cron"}
            assert metrics["cron.present"] == "yes"
            finding = await _fetchone(
                sessions,
                "SELECT severity FROM observation_findings WHERE server_id = :sid "
                "AND kind = 'cron.timer_query'",
                sid=uuid.UUID(SERVER_A),
            )
            assert finding is not None and finding.severity == "warning"

            # Sentinel redaction over the REAL transport: no probe byte, no
            # storage row, no finding summary carries it — only fingerprints.
            dump = await _storage_dump(sessions)
            assert SENTINEL not in dump
            assert SENTINEL not in str(metrics)

            # No-edit proof: every planted schedule file hashes IDENTICALLY
            # after collection (and the listings did not change).
            after = fixture.inventory_attestation()
            assert after == before

            # MAI scheduler records remain separate: exactly the seeded row.
            schedules = await _fetchall(
                sessions, "SELECT schedule_id, cron FROM job_schedules"
            )
            assert [(s.schedule_id, s.cron) for s in schedules] == [
                (schedule_id, "30 4 * * *")
            ]
            assert await _fetchall(sessions, "SELECT * FROM job_schedule_fires") == []
        finally:
            print(f"\nFIXTURE IMAGE: {fixture.image_provenance}")
            print(f"FIXTURE CLEANUP PROOF: {fixture.cleanup()}")

    @pytest.mark.anyio
    async def test_disconnect_records_unknown_never_zero(
        self, migrated_url: str, sessions
    ) -> None:
        if not _docker_available():
            pytest.skip("docker unavailable: Arch sshd fixture tests require it")
        fixture = _start_fixture(
            name=FIXTURE_CONTAINER, tag=FIXTURE_IMAGE_TAG, with_systemd=True
        )
        try:
            fixture.plant_fixture_schedules()
            resolver = fixture.env_resolver()
            key_path = resolve_identity_key(resolver, fixture.identity.credential_ref)
            online = await collect(
                sessions,
                fixture.identity,
                key_path=key_path,
                known_hosts_path=fixture.known_hosts_path,
                server_id=uuid.UUID(SERVER_A),
                controller_now=datetime.now(UTC),
            )
            assert online.error is None
            proof = fixture.remove_container()
            print(f"\nFIXTURE DISCONNECT PROOF: {proof}")
            offline = await collect(
                sessions,
                fixture.identity,
                key_path=key_path,
                known_hosts_path=fixture.known_hosts_path,
                server_id=uuid.UUID(SERVER_A),
                controller_now=datetime.now(UTC),
            )
            # Key material still resolves (scratch intact); the removed
            # container's loopback port refuses the connection.
            assert offline.error == "unreachable"
            assert offline.quality is ObservationQuality.UNKNOWN
        finally:
            print(f"FIXTURE CLEANUP PROOF: {fixture.cleanup()}")
        row = await _fetchone(
            sessions,
            "SELECT quality, metric, value FROM server_observations WHERE "
            "server_id = :sid AND quality = 'unknown'",
            sid=uuid.UUID(SERVER_A),
        )
        assert row is not None
        assert row.metric is None and row.value is None  # never a zero count

    @pytest.mark.anyio
    async def test_unauthorized_identity_records_unknown(
        self, migrated_url: str, sessions
    ) -> None:
        if not _docker_available():
            pytest.skip("docker unavailable: Arch sshd fixture tests require it")
        fixture = _start_fixture(
            name=FIXTURE_CONTAINER, tag=FIXTURE_IMAGE_TAG, with_systemd=True
        )
        try:
            resolver = fixture.unauthorized_resolver()
            with pytest.raises(SshConnectError) as excinfo:
                probe_scheduled_tasks(
                    fixture.identity,
                    key_path=resolve_identity_key(
                        resolver, fixture.identity.credential_ref
                    ),
                    known_hosts_path=fixture.known_hosts_path,
                    controller_now=datetime.now(UTC),
                )
            assert excinfo.value.code is SshErrorCode.AUTH_FAILED
        finally:
            print(f"\nFIXTURE CLEANUP PROOF: {fixture.cleanup()}")

    @pytest.mark.anyio
    async def test_bare_target_reports_missing_schedulers_explicitly(
        self, migrated_url: str, sessions
    ) -> None:
        if not _docker_available():
            pytest.skip("docker unavailable: Arch sshd fixture tests require it")
        fixture = _start_fixture(
            name=BARE_CONTAINER, tag=BARE_IMAGE_TAG, with_systemd=False
        )
        try:
            resolver = fixture.env_resolver()
            key_path = resolve_identity_key(resolver, fixture.identity.credential_ref)
            result = await collect(
                sessions,
                fixture.identity,
                key_path=key_path,
                known_hosts_path=fixture.known_hosts_path,
                server_id=uuid.UUID(SERVER_B),
                controller_now=datetime.now(UTC),
            )
            # No systemd, no cron/anacron layout: MEASURED absence — distinct
            # from unknown, with the info finding stating it.
            assert result.error is None
            assert result.quality is ObservationQuality.MEASURED
            rows = await _fetchall(
                sessions,
                "SELECT metric, value FROM server_observations WHERE server_id = :sid",
                sid=uuid.UUID(SERVER_B),
            )
            metrics = {r.metric: r.value for r in rows}
            assert metrics["cron.systemd"] == "no"
            assert metrics["cron.timers_query"] == "absent"
            assert metrics["cron.present"] == "no"
            assert metrics["cron.jobs_total"] == "0"  # explicit empty inventory
            finding = await _fetchone(
                sessions,
                "SELECT severity, resolved_at FROM observation_findings WHERE "
                "server_id = :sid AND kind = 'cron.no_scheduler'",
                sid=uuid.UUID(SERVER_B),
            )
            assert finding is not None
            assert finding.severity == "info" and finding.resolved_at is None
        finally:
            print(f"\nFIXTURE IMAGE: {fixture.image_provenance}")
            print(f"FIXTURE CLEANUP PROOF: {fixture.cleanup()}")
