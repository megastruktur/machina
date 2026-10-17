"""Focused tests for the bounded security posture collector (card 10-security, C04 + C10).

Acceptance coverage (BRIEF):

1. **Small declared baseline checks produce evidence/severity/remediation
   suggestions without automatic hardening** — the fixed probe command
   (``PROBE_COMMAND``) only READS declared posture facts: sshd presence +
   effective directives (``sshd -T``) + sshd_config file fallback, firewall
   tooling presence and ruleset LINE COUNTS (never rule content), the TCP
   LISTEN tables, the sudoers NOPASSWD grant COUNT (never sudoers content)
   and automatic-update markers. Every active finding carries a bounded
   remediation SUGGESTION in ``detail`` (persistence-verified), and the
   probe has no write/reload/rule path — nothing is ever changed on the
   target. The gate scenario runs on the real disposable fixture: the weak
   posture (explicit ``PermitRootLogin yes`` / ``PasswordAuthentication
   yes``, a NOPASSWD sudoers grant, a wildcard-bound benign listener) is
   collected with evidence, then fixed and re-evaluated (findings close).
2. **No compliance/all-safe claims; unavailable facts explicit and secret
   content excluded** — a dedicated pure control asserts no draft ever
   claims compliance/safety; refusals become ``no_data``/absent metrics
   (unreadable sudoers, unlistable ruleset, refused ``sshd -T``), never
   clean values; and the secret-exclusion sentinels (a string planted INSIDE
   the weak sudoers line, one planted in a workload argv) are asserted
   absent from every stored column of the observation domain — the probe
   has no path that could carry either.

Layers per the test policy: pure parse/finding tests (always run);
transport-boundary tests over monkeypatched subprocess; persistence claims
on a real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``, real
Alembic chain to ``0007``); ``real_target``-marked tests against a disposable
Arch sshd container (``mai-fxt-10-security-arch`` namespace, openssh + sudo +
openbsd-netcat, sshd under a restart loop so the suite can reconfigure and
reload it), auto-skipped without docker and removed with recorded proof.

Secret hygiene: fixture keys are throwaway files under
``/tmp/mai-fxt-10-security-tests``; the ``.env`` holds a PATH reference,
never material; both sentinels are synthetic marker strings, not secrets.
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
    DETAIL_MAX_CHARS,
    METRIC_MAX_CHARS,
    METRIC_VALUE_MAX_CHARS,
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
from mai.monitoring.security import (
    MAX_EXPOSED_FINDINGS,
    MAX_PROBE_OUTPUT_BYTES,
    PROBE_COMMAND,
    PROBE_TIMEOUT_SECONDS,
    RETENTION_HORIZON,
    SOURCE,
    Listener,
    SecuritySample,
    collect,
    exposed_listener_keys,
    findings_for_sample,
    parse_probe_output,
    probe_security,
    record_sample,
    record_unavailable,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Frozen fixed fixture identities (canonical lowercase UUID strings, distinct
# from every other card's constants).
SERVER_A = "7c2d3e4f-5a6b-4c90-8b1a-2d3e4f5a6b7c"
SERVER_B = "8d3e4f5a-6b7c-4d81-9c2b-3e4f5a6b7c8d"

NOW = datetime(2026, 10, 17, 21, 30, 0, tzinfo=UTC)

#: Secret-exclusion sentinels. The CONTENT sentinel is planted inside the
#: weak sudoers GRANT LINE on the real fixture (never a secret): no
#: observation row may contain it because the probe emits only a COUNT of
#: matching lines. The ARGV sentinel lives in a workload argv: the probe has
#: no process-listing path at all.
CONTENT_SENTINEL = "mai-10-security-sudoers-content-sentinel"
ARGV_SENTINEL = "mai-10-security-argv-sentinel-not-a-secret"

#: The gate scenario's allocated listener port (fixture-internal, unused
#: unprivileged port on the disposable target; open → collect → close).
LISTEN_PORT = 45678

# Real probe output captured verbatim from the disposable Arch fixture target
# (2026-10-17, mai-fxt-10-security-arch; archlinux + openssh + sudo +
# openbsd-netcat image). Embedded as the parse fixture so the pure tests
# exercise the exact byte shape the real target emits. WEAK state: explicit
# weak sshd directives, one NOPASSWD sudoers grant, a wildcard-bound benign
# listener on the allocated port. Note the honest firewall gap: the image has
# nft/iptables binaries but the container lacks CAP_NET_ADMIN, so both
# listings are REFUSED (nft_rc=1, ipt_rc=4) — ruleset counts stay absent.
_ARCH_WEAK_OUTPUT = (
    "ts=2026-10-17T21:31:21\n"
    "sshd_present=yes\n"
    "sshd_t_rc=0\n"
    "eff=PermitRootLogin yes\n"
    "eff=PasswordAuthentication yes\n"
    "cfg_rc=0\n"
    "cfg=PermitRootLogin yes\n"
    "cfg=PasswordAuthentication yes\n"
    "nft_present=yes\n"
    "nft_rc=1\n"
    "ipt_present=yes\n"
    "ipt_rc=4\n"
    "tcp_rc=0\n"
    "tcp_l=00000000:B26E\n"
    "tcp_l=00000000:0016\n"
    "tcp6_rc=0\n"
    "tcp6_l=00000000000000000000000000000000:0016\n"
    "sudo_present=yes\n"
    "sudo_read=ok\n"
    "sudo_nopasswd=1\n"
    "pkg_apt=no\n"
    "pkg_dnf=no\n"
)

# The same target AFTER the fix arm: weak directives removed, explicit
# hardening directives added, weak sudoers file removed, benign listener
# closed. sshd's own wildcard listeners remain (honest baseline).
_ARCH_FIXED_OUTPUT = (
    "ts=2026-10-17T21:31:31\n"
    "sshd_present=yes\n"
    "sshd_t_rc=0\n"
    "eff=PermitRootLogin prohibit-password\n"
    "eff=PasswordAuthentication no\n"
    "cfg_rc=0\n"
    "cfg=PermitRootLogin prohibit-password\n"
    "cfg=PasswordAuthentication no\n"
    "nft_present=yes\n"
    "nft_rc=1\n"
    "ipt_present=yes\n"
    "ipt_rc=4\n"
    "tcp_rc=0\n"
    "tcp_l=00000000:0016\n"
    "tcp6_rc=0\n"
    "tcp6_l=00000000000000000000000000000000:0016\n"
    "sudo_present=yes\n"
    "sudo_read=ok\n"
    "sudo_nopasswd=0\n"
    "pkg_apt=no\n"
    "pkg_dnf=no\n"
)

# A limited-privilege target: no sshd binary (the ``sshd_present=no`` branch),
# unreadable sudoers, no firewall tooling at all, tcp6 table refused.
_LIMITED_OUTPUT = (
    "ts=2026-10-17T10:00:00\n"
    "sshd_present=no\n"
    "cfg_rc=1\n"
    "nft_present=no\n"
    "ipt_present=no\n"
    "tcp_rc=0\n"
    "tcp_l=0100007F:1F90\n"
    "tcp6_rc=1\n"
    "sudo_present=yes\n"
    "sudo_read=refused\n"
    "pkg_apt=yes\n"
    "apt_auto=no\n"
    "pkg_dnf=no\n"
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


def _sample(**overrides: object) -> SecuritySample:
    """A weak-fixture-shaped sample (the real capture, normalized)."""
    fields: dict[str, object] = {
        "observed_at": NOW,
        "observed_at_source": "target",
        "quality": ObservationQuality.DEGRADED,  # the refused firewall listing
        "sshd_present": True,
        "sshd_effective_ok": True,
        "sshd_config_readable": True,
        "sshd_source": "effective",
        "permit_root_login": "yes",
        "password_authentication": "yes",
        "nft_present": True,
        "nft_rules": None,  # listing refused on the real container
        "iptables_present": True,
        "iptables_rules": None,
        "tcp_complete": True,
        "tcp6_complete": True,
        "listeners": (
            Listener(proto="tcp", address="0.0.0.0", port=22),
            Listener(proto="tcp", address="0.0.0.0", port=LISTEN_PORT),
            Listener(proto="tcp6", address="::", port=22),
        ),
        "sudo_present": True,
        "sudo_nopasswd": 1,
        "apt_present": False,
        "apt_auto": None,
        "dnf_present": False,
        "dnf_auto": None,
    }
    fields.update(overrides)
    return SecuritySample(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure parsing: bounded target output → honest sample
# ---------------------------------------------------------------------------


class TestParseProbeOutput:
    def test_real_weak_capture_parses_measured(self) -> None:
        sample = parse_probe_output(_ARCH_WEAK_OUTPUT, controller_now=NOW)
        # Target-reported timestamp, controller clock untouched.
        assert sample.observed_at == datetime(2026, 10, 17, 21, 31, 21, tzinfo=UTC)
        assert sample.observed_at_source == "target"
        # Effective sshd parse answered both directives (weak posture) and
        # wins over the config-file lines the same output carries.
        assert sample.sshd_present is True
        assert sample.sshd_effective_ok is True
        assert sample.sshd_source == "effective"
        assert sample.permit_root_login == "yes"
        assert sample.password_authentication == "yes"
        # Firewall tooling present but BOTH listings refused → counts stay
        # absent (a gap, never an empty ruleset).
        assert sample.nft_present is True
        assert sample.nft_rules is None
        assert sample.iptables_present is True
        assert sample.iptables_rules is None
        # Listener tables decoded in the target's own order: the allocated
        # benign port first, then sshd's wildcard on both families.
        assert sample.listeners == (
            Listener(proto="tcp", address="0.0.0.0", port=LISTEN_PORT),
            Listener(proto="tcp", address="0.0.0.0", port=22),
            Listener(proto="tcp6", address="::", port=22),
        )
        assert sample.tcp_complete and sample.tcp6_complete
        # sudoers readable: exactly the one planted NOPASSWD grant counted.
        assert sample.sudo_present is True
        assert sample.sudo_nopasswd == 1
        # No supported package manager: explicit, not inferred.
        assert sample.apt_present is False and sample.dnf_present is False
        assert sample.updates_auto_configured is None
        # Four of five declared checks resolved; the unlistable firewall is
        # the honest degradation.
        assert sample.quality is ObservationQuality.DEGRADED

    def test_real_fixed_capture_flips_posture_values(self) -> None:
        sample = parse_probe_output(_ARCH_FIXED_OUTPUT, controller_now=NOW)
        assert sample.observed_at == datetime(2026, 10, 17, 21, 31, 31, tzinfo=UTC)
        assert sample.permit_root_login == "prohibit-password"
        assert sample.password_authentication == "no"
        assert sample.sudo_nopasswd == 0
        assert [f"{lis.address}:{lis.port}" for lis in sample.listeners] == [
            "0.0.0.0:22",
            ":::22",
        ]
        assert sample.quality is ObservationQuality.DEGRADED  # firewall refusal

    def test_empty_output_is_unknown_without_any_metric(self) -> None:
        sample = parse_probe_output("", controller_now=NOW)
        assert sample.quality is ObservationQuality.UNKNOWN
        assert sample.sshd_present is None and sample.sshd_source is None
        assert sample.permit_root_login is None
        assert sample.listeners == ()
        assert sample.nft_present is None and sample.iptables_present is None
        assert sample.sudo_present is None and sample.sudo_nopasswd is None
        assert sample.observed_at == NOW
        assert sample.observed_at_source == "controller"

    def test_limited_target_degrades_with_explicit_gaps(self) -> None:
        """The limited-privilege shape: absent sshd, refused sudoers/tcp6.

        Resolved checks (no sshd, no firewall tooling, apt without auto
        config) sit beside explicit gaps (sudoers unreadable, tcp6 refused).
        The gaps stay absent — never fabricated as clean values.
        """
        sample = parse_probe_output(_LIMITED_OUTPUT, controller_now=NOW)
        assert sample.quality is ObservationQuality.DEGRADED
        assert sample.sshd_present is False
        assert sample.sshd_posture_resolved is True  # absence is an answer
        assert sample.permit_root_login is None
        assert sample.firewall_resolved is True  # "no tooling" is an answer
        assert sample.listeners == (
            Listener(proto="tcp", address="127.0.0.1", port=8080),
        )
        assert sample.listeners_complete is False  # tcp6 refused
        assert sample.sudo_present is True
        assert sample.sudo_nopasswd is None  # the explicit gap
        assert sample.apt_present is True and sample.apt_auto is False
        assert sample.updates_auto_configured is False

    def test_config_fallback_used_when_effective_refused(self) -> None:
        """``sshd -T`` refused but the file readable: config answers.

        Case-insensitive directive matching, values normalized to the
        lowercase vocabulary, and the source records the fallback honestly.
        """
        output = (
            "ts=2026-10-17T10:00:00\n"
            "sshd_present=yes\n"
            "sshd_t_rc=1\n"
            "cfg_rc=0\n"
            "cfg=PermitRootLogin NO\n"
            "cfg=PasswordAuthentication Yes\n"
            "nft_present=no\n"
            "ipt_present=no\n"
            "tcp_rc=0\n"
            "tcp6_rc=0\n"
            "sudo_present=no\n"
            "pkg_apt=no\n"
            "pkg_dnf=no\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.sshd_source == "config"
        assert sample.permit_root_login == "no"
        assert sample.password_authentication == "yes"
        assert sample.quality is ObservationQuality.MEASURED
        # The config fallback still sees the weak directive word when present.
        weak = parse_probe_output(
            output.replace("PermitRootLogin NO", "PermitRootLogin yes"),
            controller_now=NOW,
        )
        assert weak.permit_root_login == "yes"
        root = next(
            d for d in findings_for_sample(weak) if d.kind == "security.sshd_root_login"
        )
        assert root.state == "active"

    def test_first_cfg_match_wins_like_sshd_single_file(self) -> None:
        """sshd_config semantics: the FIRST obtained value is used."""
        output = (
            "sshd_present=yes\nsshd_t_rc=1\ncfg_rc=0\n"
            "cfg=PermitRootLogin no\n"
            "cfg=PermitRootLogin yes\n"
            "cfg=PasswordAuthentication no\n"
            "cfg=PasswordAuthentication yes\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.permit_root_login == "no"
        assert sample.password_authentication == "no"

    def test_effective_parse_wins_over_config(self) -> None:
        output = (
            "sshd_present=yes\nsshd_t_rc=0\ncfg_rc=0\n"
            "cfg=PermitRootLogin no\n"
            "eff=permitrootlogin yes\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.sshd_source == "effective"
        assert sample.permit_root_login == "yes"

    def test_directive_outside_vocabulary_is_a_gap_not_a_value(self) -> None:
        output = (
            "sshd_present=yes\nsshd_t_rc=0\ncfg_rc=1\n"
            "eff=permitrootlogin banana\n"
            "eff=passwordauthentication maybe\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        # Posture source resolved, but the unrecognized words stay UNKNOWN —
        # they are never coerced to yes/no and never stored.
        assert sample.sshd_source == "effective"
        assert sample.permit_root_login is None
        assert sample.password_authentication is None

    def test_unparsable_timestamp_falls_back_to_controller_clock(self) -> None:
        sample = parse_probe_output(
            "ts=nonsense\nsshd_present=no\n", controller_now=NOW
        )
        assert sample.observed_at_source == "controller"
        assert sample.observed_at == NOW

    def test_junk_values_become_gaps_never_clean(self) -> None:
        output = (
            "sshd_present=banana\n"  # not a bool word
            "sshd_t_rc=hosed\n"
            "nft_present=yes\n"  # present but no rules key → refused listing
            "ipt_present=no\n"
            "sudo_present=yes\n"  # readable marker missing → count unknown
            "sudo_nopasswd=7\n"
            "pkg_apt=yes\napt_auto=banana\npkg_dnf=no\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.sshd_present is None
        assert sample.sshd_effective_ok is None
        assert sample.nft_rules is None  # refused, never zero
        assert sample.sudo_nopasswd is None  # readability unknown, count unused
        assert sample.updates_auto_configured is None  # marker unreadable
        # Package-manager PRESENCE did answer (apt yes / dnf no), so that
        # declared check resolved: the sample degrades rather than unknown.
        assert sample.quality is ObservationQuality.DEGRADED

    def test_listener_rows_reject_junk_and_degenerate_ports(self) -> None:
        output = (
            "tcp_rc=0\n"
            "tcp_l=0100007F:0000\n"  # port 0 is not a listener
            "tcp_l=0100007F:ZZZZ\n"  # not hex
            "tcp_l=0100007F1:0050\n"  # 9-hex junk address
            "tcp_l=0100007F:1F90\n"  # 127.0.0.1:8080 — the one valid row
            "tcp6_rc=0\n"
            "tcp6_l=00000000000000000000000001000000:0019\n"  # ::1:25
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.listeners == (
            Listener(proto="tcp", address="127.0.0.1", port=8080),
            Listener(proto="tcp6", address="::1", port=25),
        )

    def test_zero_rules_count_only_when_ruleset_read(self) -> None:
        # rc=0 with a zero count is a TRUSTED empty listing.
        sample = parse_probe_output(
            "nft_present=yes\nnft_rc=0\nnft_rules=0\nipt_present=no\n",
            controller_now=NOW,
        )
        assert sample.nft_rules == 0
        assert sample.firewall_resolved is True
        # A refused listing is NOT an empty ruleset (no count key at all).
        sample = parse_probe_output(
            "nft_present=yes\nnft_rc=1\nipt_present=no\n", controller_now=NOW
        )
        assert sample.nft_rules is None
        assert sample.firewall_resolved is False

    def test_listener_listings_are_bounded(self) -> None:
        lines = [
            f"tcp_l=0A0000{i % 256:02X}:0050" for i in range(MAX_EXPOSED_FINDINGS + 8)
        ]
        sample = parse_probe_output(
            "tcp_rc=0\n" + "\n".join(lines) + "\ntcp6_rc=1\n", controller_now=NOW
        )
        assert len(sample.listeners) == MAX_EXPOSED_FINDINGS

    def test_rule_counts_are_bounded_ints(self) -> None:
        sample = parse_probe_output(
            "nft_present=yes\nnft_rc=0\nnft_rules=99999999\nipt_present=no\n",
            controller_now=NOW,
        )
        assert sample.nft_rules == 99999999  # capped again at metric render
        sample = parse_probe_output(
            "nft_present=yes\nnft_rc=0\nnft_rules=hosed\nipt_present=no\n",
            controller_now=NOW,
        )
        assert sample.nft_rules is None

    def test_controller_now_must_be_timezone_aware(self) -> None:
        with pytest.raises(ValueError):
            parse_probe_output(
                "sshd_present=no\n", controller_now=datetime(2026, 10, 17)
            )


# ---------------------------------------------------------------------------
# Pure findings: per-check three-valued outputs, remediation suggestions
# ---------------------------------------------------------------------------


class TestFindingsForSample:
    def test_weak_sshd_directives_are_active_warnings_with_remediation(self) -> None:
        drafts = findings_for_sample(_sample())
        root = next(d for d in drafts if d.kind == "security.sshd_root_login")
        assert (root.state, root.severity) == ("active", "warning")
        assert "permitrootlogin=yes" in root.summary
        assert "effective" in root.summary
        assert root.detail is not None and "never changes the target" in root.detail
        auth = next(d for d in drafts if d.kind == "security.sshd_password_auth")
        assert (auth.state, auth.severity) == ("active", "warning")
        assert "passwordauthentication=yes" in auth.summary
        assert auth.detail is not None and "PasswordAuthentication no" in auth.detail

    def test_hardened_directives_close_and_restate_the_observed_value(self) -> None:
        drafts = findings_for_sample(
            _sample(permit_root_login="prohibit-password", password_authentication="no")
        )
        root = next(d for d in drafts if d.kind == "security.sshd_root_login")
        assert (root.state, root.severity) == ("clear", "info")
        assert "permitrootlogin=prohibit-password" in root.summary
        assert root.detail is None
        auth = next(d for d in drafts if d.kind == "security.sshd_password_auth")
        assert (auth.state, auth.severity) == ("clear", "info")
        assert "passwordauthentication=no" in auth.summary

    def test_unknown_sshd_posture_is_no_data(self) -> None:
        drafts = findings_for_sample(_sample(sshd_present=None))
        for kind in ("security.sshd_root_login", "security.sshd_password_auth"):
            draft = next(d for d in drafts if d.kind == kind)
            assert draft.state == "no_data"

    def test_directive_gap_in_resolved_posture_is_no_data(self) -> None:
        """Effective parse resolved but a directive value out of vocabulary."""
        drafts = findings_for_sample(
            _sample(
                permit_root_login=None,
                password_authentication=None,
                nft_rules=3,  # keep the firewall check resolved
            )
        )
        for kind in ("security.sshd_root_login", "security.sshd_password_auth"):
            draft = next(d for d in drafts if d.kind == kind)
            assert draft.state == "no_data"

    def test_exposed_listeners_are_per_key_active_loopback_skipped(self) -> None:
        sample = _sample(
            listeners=(
                Listener(proto="tcp", address="0.0.0.0", port=22),
                Listener(proto="tcp", address="127.0.0.1", port=8080),
                Listener(proto="tcp6", address="::", port=22),
                Listener(proto="tcp6", address="::1", port=25),
            )
        )
        assert exposed_listener_keys(sample) == (
            "tcp:0.0.0.0:22",
            "tcp6::::22",
        )
        drafts = [
            d
            for d in findings_for_sample(sample)
            if d.kind == "security.exposed_listener"
        ]
        assert [d.dedup_key for d in drafts] == ["tcp:0.0.0.0:22", "tcp6::::22"]
        assert all(d.state == "active" and d.severity == "warning" for d in drafts)
        assert all("non-loopback" in d.summary and d.detail for d in drafts)

    def test_incomplete_listener_table_is_no_data_never_active(self) -> None:
        sample = _sample(tcp_complete=False, listeners=())
        draft = next(
            d
            for d in findings_for_sample(sample)
            if d.kind == "security.exposed_listener"
        )
        assert (draft.state, draft.dedup_key, draft.summary) == (
            "no_data",
            "table",
            "",
        )
        assert exposed_listener_keys(sample) == ()

    def test_exposed_listeners_are_bounded(self) -> None:
        sample = _sample(
            listeners=tuple(
                Listener(proto="tcp", address=f"10.0.0.{i % 256}", port=1000 + i)
                for i in range(MAX_EXPOSED_FINDINGS + 10)
            )
        )
        assert len(exposed_listener_keys(sample)) == MAX_EXPOSED_FINDINGS

    def test_sudo_count_states(self) -> None:
        active = next(
            d
            for d in findings_for_sample(_sample())
            if d.kind == "security.sudo_nopasswd"
        )
        assert (active.state, active.severity) == ("active", "warning")
        assert "1 NOPASSWD sudo grant line(s)" in active.summary
        assert active.detail is not None and "minimum commands" in active.detail
        clear = next(
            d
            for d in findings_for_sample(_sample(sudo_nopasswd=0))
            if d.kind == "security.sudo_nopasswd"
        )
        assert (clear.state, clear.severity) == ("clear", "info")
        assert "0 NOPASSWD" in clear.summary
        absent = next(
            d
            for d in findings_for_sample(
                _sample(sudo_present=False, sudo_nopasswd=None)
            )
            if d.kind == "security.sudo_nopasswd"
        )
        assert (absent.state, absent.severity) == ("clear", "info")
        assert "not installed" in absent.summary
        refused = next(
            d
            for d in findings_for_sample(_sample(sudo_nopasswd=None))
            if d.kind == "security.sudo_nopasswd"
        )
        assert refused.state == "no_data"  # unreadable is never "no grants"

    def test_firewall_states(self) -> None:
        empty = next(
            d
            for d in findings_for_sample(_sample(nft_rules=0))
            if d.kind == "security.firewall_rules"
        )
        assert (empty.state, empty.severity) == ("active", "warning")
        assert "0 rule line(s) in the nft ruleset" in empty.summary
        assert "empty ruleset" in empty.summary
        rules = next(
            d
            for d in findings_for_sample(_sample(nft_rules=5))
            if d.kind == "security.firewall_rules"
        )
        assert (rules.state, rules.severity) == ("clear", "info")
        assert "5 rule line(s)" in rules.summary
        refused = next(
            d
            for d in findings_for_sample(_sample())
            if d.kind == "security.firewall_rules"
        )
        assert refused.state == "no_data"  # tool present, listing refused
        no_tools = next(
            d
            for d in findings_for_sample(
                _sample(nft_present=False, iptables_present=False)
            )
            if d.kind == "security.firewall_rules"
        )
        assert no_tools.state == "no_data"  # no tooling: state unknown

    def test_updates_states(self) -> None:
        active = next(
            d
            for d in findings_for_sample(
                _sample(apt_present=True, apt_auto=False, dnf_present=False)
            )
            if d.kind == "security.updates_auto"
        )
        assert (active.state, active.severity) == ("active", "warning")
        assert "no automatic security-update configuration detected (apt)" in (
            active.summary
        )
        assert active.detail is not None and "unattended-upgrades" in active.detail
        clear = next(
            d
            for d in findings_for_sample(
                _sample(apt_present=True, apt_auto=True, dnf_present=False)
            )
            if d.kind == "security.updates_auto"
        )
        assert (clear.state, clear.severity) == ("clear", "info")
        assert "detected (apt)" in clear.summary
        none_mgr = next(
            d
            for d in findings_for_sample(_sample())
            if d.kind == "security.updates_auto"
        )
        assert none_mgr.state == "no_data"  # no supported manager: not a pass
        unreadable = next(
            d
            for d in findings_for_sample(
                _sample(apt_present=True, apt_auto=None, dnf_present=False)
            )
            if d.kind == "security.updates_auto"
        )
        assert unreadable.state == "no_data"

    def test_no_compliance_or_all_safe_claims_anywhere(self) -> None:
        """Acceptance 2: even a fully clear sample never claims safety."""
        good = _sample(
            permit_root_login="prohibit-password",
            password_authentication="no",
            nft_rules=12,
            sudo_nopasswd=0,
            apt_present=True,
            apt_auto=True,
            dnf_present=False,
            listeners=(Listener(proto="tcp", address="127.0.0.1", port=22),),
        )
        forbidden = ("compliant", "secure", "all safe", "no risks", "protected", "safe")
        for draft in findings_for_sample(good):
            text = f"{draft.summary} {draft.detail or ''}".lower()
            for word in forbidden:
                assert word not in text, (draft.kind, word, text)

    def test_detail_suggestions_are_bounded(self) -> None:
        for draft in findings_for_sample(_sample()):
            if draft.detail is not None:
                assert len(draft.detail) <= DETAIL_MAX_CHARS
                assert "never changes the target" in draft.detail


# ---------------------------------------------------------------------------
# Transport boundary: frozen option surface, budget, stable rejections
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _identity() -> SshIdentity:
    return SshIdentity(
        host="127.0.0.1",
        port=2222,
        user="root",
        host_key="SHA256:" + "A" * 43,
        credential_ref="MAI_SSH_KEY_TEST",
    )


def _known_hosts(tmp_path: Path) -> Path:
    path = tmp_path / "managed_known_hosts"
    path.write_text("")
    return path


def _refusing_run(stderr: str):
    """A subprocess.run double whose ssh exits 255 with ``stderr``."""

    def refusing_run(argv, **kwargs):
        return _FakeProc(stderr=stderr, returncode=255)

    return refusing_run


class TestProbeBoundary:
    def test_missing_known_hosts_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(SshConnectError) as exc:
            probe_security(
                _identity(),
                key_path=tmp_path / "k",
                known_hosts_path=tmp_path / "absent_known_hosts",
                controller_now=NOW,
            )
        assert exc.value.code is SshErrorCode.INVALID_CONFIGURATION

    def test_probe_uses_frozen_option_surface(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _FakeProc(stdout=_ARCH_WEAK_OUTPUT)

        monkeypatch.setattr("mai.monitoring.security.subprocess.run", fake_run)
        key_path = tmp_path / "key"
        known_hosts = _known_hosts(tmp_path)
        probe_security(
            _identity(),
            key_path=key_path,
            known_hosts_path=known_hosts,
            controller_now=NOW,
        )
        argv = captured["argv"]
        assert argv[0] == "ssh"
        assert argv[argv.index("-p") + 1] == "2222"
        assert argv[argv.index("-i") + 1] == str(key_path)
        options = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
        # Exactly the frozen P04 probe surface — nothing ambient.
        assert options == [
            "IdentitiesOnly=yes",
            "BatchMode=yes",
            "StrictHostKeyChecking=yes",
            f"UserKnownHostsFile={known_hosts}",
            "ConnectTimeout=5",
        ]
        assert argv[-1] == PROBE_COMMAND
        assert f"{_identity().user}@{_identity().host}" in argv
        kwargs = captured["kwargs"]
        assert kwargs["timeout"] == PROBE_TIMEOUT_SECONDS
        assert kwargs["stdin"] == subprocess.DEVNULL

    def test_successful_probe_parses_under_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "mai.monitoring.security.subprocess.run",
            lambda argv, **kwargs: _FakeProc(stdout=_ARCH_WEAK_OUTPUT),
        )
        sample = probe_security(
            _identity(),
            key_path=tmp_path / "k",
            known_hosts_path=_known_hosts(tmp_path),
            controller_now=NOW,
        )
        assert sample.quality is ObservationQuality.DEGRADED
        assert sample.permit_root_login == "yes"

    def test_oversized_probe_output_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "mai.monitoring.security.subprocess.run",
            lambda argv, **kwargs: _FakeProc(stdout="x" * (MAX_PROBE_OUTPUT_BYTES + 1)),
        )
        with pytest.raises(SshConnectError) as exc:
            probe_security(
                _identity(),
                key_path=tmp_path / "k",
                known_hosts_path=_known_hosts(tmp_path),
                controller_now=NOW,
            )
        assert exc.value.code is SshErrorCode.BOUNDED_OUTPUT_EXCEEDED

    def test_probe_timeout_maps_to_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

        monkeypatch.setattr("mai.monitoring.security.subprocess.run", raise_timeout)
        with pytest.raises(SshConnectError) as exc:
            probe_security(
                _identity(),
                key_path=tmp_path / "k",
                known_hosts_path=_known_hosts(tmp_path),
                controller_now=NOW,
            )
        assert exc.value.code is SshErrorCode.TIMEOUT

    def test_permission_refused_and_unreachable_map_to_stable_codes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cases = {
            "ssh: Permission denied (publickey).": SshErrorCode.AUTH_FAILED,
            "ssh: connect to host 127.0.0.1 port 2222: Connection refused": (
                SshErrorCode.UNREACHABLE
            ),
            "Host key verification failed.": SshErrorCode.HOST_KEY_MISMATCH,
            "kex_exchange_identification read: Connection reset by peer": (
                SshErrorCode.UNREACHABLE
            ),
            "some totally unexpected failure": SshErrorCode.SSH_FAILED,
        }
        for stderr, expected in cases.items():
            monkeypatch.setattr(
                "mai.monitoring.security.subprocess.run",
                _refusing_run(stderr),
            )
            with pytest.raises(SshConnectError) as exc:
                probe_security(
                    _identity(),
                    key_path=tmp_path / "k",
                    known_hosts_path=_known_hosts(tmp_path),
                    controller_now=NOW,
                )
            assert exc.value.code is expected


# ---------------------------------------------------------------------------
# Persistence into the real 0007 schema
# ---------------------------------------------------------------------------


class TestRecordSample:
    @pytest.mark.anyio
    async def test_weak_snapshot_persists_bounded_rows_and_active_findings(
        self, sessions
    ) -> None:
        written = await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        # 4 sshd + 2 firewall presence (rule counts refused → absent) +
        # 3 listeners + 1 count + 2 sudo + 1 updates.
        assert written == 13
        rows = await _fetchall(
            sessions,
            "SELECT metric, value, source, method, quality, observed_at, "
            "collected_at, fresh_until, retain_until, seq "
            "FROM server_observations WHERE server_id = :s ORDER BY metric, seq",
            s=uuid.UUID(SERVER_A),
        )
        # Two tcp rows share the metric name; compare the rest by map and
        # the tcp rows by their ordered value list.
        single = {r.metric: r.value for r in rows if r.metric != "sec.listen.tcp"}
        assert single == {
            "sec.sshd.present": "yes",
            "sec.sshd.source": "effective",
            "sec.sshd.permit_root_login": "yes",
            "sec.sshd.password_authentication": "yes",
            "sec.firewall.nft": "yes",
            "sec.firewall.iptables": "yes",
            "sec.listen.tcp6": ":::22",
            "sec.listen_count": "3",
            "sec.sudo.present": "yes",
            "sec.sudo.nopasswd_count": "1",
            "sec.updates.pkg_mgr": "none",
        }
        tcp_values = [r.value for r in rows if r.metric == "sec.listen.tcp"]
        assert tcp_values == ["0.0.0.0:22", f"0.0.0.0:{LISTEN_PORT}"]
        # Acceptance 1: source + timestamp + horizons on every row.
        assert rows and all(
            r.source == SOURCE and r.method == "command" and r.quality == "degraded"
            for r in rows
        )
        assert all(r.observed_at == NOW for r in rows)
        assert all(r.fresh_until == NOW + timedelta(minutes=15) for r in rows)
        assert all(r.retain_until == NOW + RETENTION_HORIZON for r in rows)
        assert all(r.seq > 0 for r in rows)
        # Active findings with bounded remediation suggestions.
        findings = await _fetchall(
            sessions,
            "SELECT kind, dedup_key, severity, summary, detail FROM observation_findings "
            "WHERE server_id = :s AND resolved_at IS NULL",
            s=uuid.UUID(SERVER_A),
        )
        kinds = {f.kind for f in findings}
        assert kinds == {
            "security.sshd_root_login",
            "security.sshd_password_auth",
            "security.exposed_listener",
            "security.sudo_nopasswd",
        }
        exposed = sorted(
            f.dedup_key for f in findings if f.kind == "security.exposed_listener"
        )
        assert exposed == [
            "tcp6::::22",
            "tcp:0.0.0.0:22",
            f"tcp:0.0.0.0:{LISTEN_PORT}",
        ]
        for f in findings:
            assert f.severity == "warning"
            assert f.detail is not None
            assert len(f.detail) <= DETAIL_MAX_CHARS
            assert "never changes the target" in f.detail
        # No firewall/updates rows: their checks were no_data on this sample.
        assert not any("firewall" in k for k in kinds)
        assert not any("updates" in k for k in kinds)

    @pytest.mark.anyio
    async def test_metric_value_bounds_are_structural(self, sessions) -> None:
        with pytest.raises(IntegrityError):
            async with sessions() as session:
                await session.execute(
                    text(
                        "INSERT INTO server_observations (observation_id, server_id, "
                        "source, method, quality, metric, value, observed_at, "
                        "collected_at, fresh_until, retain_until) VALUES "
                        "(:oid, :sid, 'security', 'command', 'measured', :m, 'v', "
                        "now(), now(), now(), now())"
                    ),
                    {
                        "oid": uuid.uuid4(),
                        "sid": uuid.UUID(SERVER_A),
                        "m": "x" * (METRIC_MAX_CHARS + 1),
                    },
                )
                await session.commit()
        with pytest.raises(IntegrityError):
            async with sessions() as session:
                await session.execute(
                    text(
                        "INSERT INTO server_observations (observation_id, server_id, "
                        "source, method, quality, metric, value, observed_at, "
                        "collected_at, fresh_until, retain_until) VALUES "
                        "(:oid, :sid, 'security', 'command', 'measured', 'sec.sshd.present', "
                        ":v, now(), now(), now(), now())"
                    ),
                    {
                        "oid": uuid.uuid4(),
                        "sid": uuid.UUID(SERVER_A),
                        "v": "x" * (METRIC_VALUE_MAX_CHARS + 1),
                    },
                )
                await session.commit()
        with pytest.raises(IntegrityError):
            # The remediation-suggestion detail column is bounded too: the
            # schema rejects any oversized detail the collector could emit.
            async with sessions() as session:
                await session.execute(
                    text(
                        "INSERT INTO observation_findings (finding_id, server_id, "
                        "kind, dedup_key, severity, summary, detail, retain_until) "
                        "VALUES (:fid, :sid, 'security.sshd_root_login', "
                        "'config', 'warning', 'summary', :d, now())"
                    ),
                    {
                        "fid": uuid.uuid4(),
                        "sid": uuid.UUID(SERVER_A),
                        "d": "x" * (DETAIL_MAX_CHARS + 1),
                    },
                )
                await session.commit()

    @pytest.mark.anyio
    async def test_unknown_with_a_metric_is_a_real_integrity_violation(
        self, sessions
    ) -> None:
        with pytest.raises(IntegrityError):
            async with sessions() as session:
                await session.execute(
                    text(
                        "INSERT INTO server_observations (observation_id, server_id, "
                        "source, method, quality, metric, value, collected_at, "
                        "retain_until) VALUES (:oid, :sid, 'security', 'command', "
                        "'unknown', 'sec.sudo.nopasswd_count', '0', now(), now())"
                    ),
                    {"oid": uuid.uuid4(), "sid": uuid.UUID(SERVER_A)},
                )
                await session.commit()

    @pytest.mark.anyio
    async def test_degraded_sample_persists_gaps_as_absent_not_clean(
        self, sessions
    ) -> None:
        """Unreadable sudoers + refused firewall + refused tcp6: gaps absent."""
        sample = _sample(
            quality=ObservationQuality.DEGRADED,
            nft_rules=None,
            iptables_rules=None,
            listeners=(Listener(proto="tcp", address="0.0.0.0", port=22),),
            tcp6_complete=False,
            sudo_nopasswd=None,
        )
        written = await record_sample(
            sessions, uuid.UUID(SERVER_A), sample, controller_now=NOW
        )
        # 4 sshd + 2 firewall presence + 1 sudo presence + 1 updates = 8;
        # every gap (rule counts, all listener rows, sudoers count)
        # contributes NO row — the incomplete tcp6 table suppresses the
        # whole listener listing rather than a partial one.
        assert written == 8
        rows = await _fetchall(
            sessions,
            "SELECT metric FROM server_observations WHERE server_id = :s",
            s=uuid.UUID(SERVER_A),
        )
        metrics = {r.metric for r in rows}
        assert "sec.sudo.nopasswd_count" not in metrics
        assert "sec.firewall.nft_rules" not in metrics
        assert "sec.firewall.iptables_rules" not in metrics
        assert not any(m.startswith("sec.listen.tcp6") for m in metrics)
        # The unresolved checks opened NO findings either.
        findings = await _fetchall(
            sessions,
            "SELECT kind FROM observation_findings WHERE server_id = :s",
            s=uuid.UUID(SERVER_A),
        )
        kinds = {f.kind for f in findings}
        assert "security.firewall_rules" not in kinds
        # …and the incomplete listener table never rates exposure.
        assert "security.exposed_listener" not in kinds

    @pytest.mark.anyio
    async def test_sshd_finding_open_close_reopen_lifecycle(self, sessions) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        opened = await _fetchone(
            sessions,
            "SELECT finding_id, severity, resolved_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'security.sshd_root_login'",
            s=uuid.UUID(SERVER_A),
        )
        assert opened.resolved_at is None and opened.severity == "warning"
        # Fix: the directives flip (the real fixture capture's fixed shape).
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(
                permit_root_login="prohibit-password",
                password_authentication="no",
                sudo_nopasswd=0,
                listeners=(Listener(proto="tcp", address="0.0.0.0", port=22),),
            ),
            controller_now=NOW + timedelta(minutes=1),
        )
        closed = await _fetchone(
            sessions,
            "SELECT finding_id, resolved_at, updated_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'security.sshd_root_login'",
            s=uuid.UUID(SERVER_A),
        )
        assert closed.finding_id == opened.finding_id  # dedup: same row updated
        assert closed.resolved_at is not None
        # Reopen: the weak value returns; the same row flips back open.
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(),
            controller_now=NOW + timedelta(minutes=2),
        )
        reopened = await _fetchone(
            sessions,
            "SELECT finding_id, resolved_at, updated_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'security.sshd_root_login'",
            s=uuid.UUID(SERVER_A),
        )
        assert reopened.finding_id == opened.finding_id
        assert reopened.resolved_at is None
        assert reopened.updated_at > closed.updated_at

    @pytest.mark.anyio
    async def test_exposed_listener_rows_reconcile_per_key(self, sessions) -> None:
        """Complete tables close exactly the vanished listeners; survivors
        stay open; an INCOMPLETE table leaves every row untouched."""
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        two_open = await _fetchall(
            sessions,
            "SELECT dedup_key FROM observation_findings WHERE server_id = :s "
            "AND kind = 'security.exposed_listener' AND resolved_at IS NULL",
            s=uuid.UUID(SERVER_A),
        )
        assert len(two_open) == 3
        # The benign listener closes; sshd's own wildcard rows stay open.
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(
                listeners=(
                    Listener(proto="tcp", address="0.0.0.0", port=22),
                    Listener(proto="tcp6", address="::", port=22),
                )
            ),
            controller_now=NOW + timedelta(minutes=1),
        )
        rows = await _fetchall(
            sessions,
            "SELECT dedup_key, resolved_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'security.exposed_listener'",
            s=uuid.UUID(SERVER_A),
        )
        state = {r.dedup_key: r.resolved_at for r in rows}
        assert state[f"tcp:0.0.0.0:{LISTEN_PORT}"] is not None
        assert state["tcp:0.0.0.0:22"] is None
        assert state["tcp6::::22"] is None
        # Zero exposed listeners on a complete table closes the whole kind.
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(listeners=(Listener(proto="tcp", address="127.0.0.1", port=22),)),
            controller_now=NOW + timedelta(minutes=2),
        )
        open_rows = await _fetchall(
            sessions,
            "SELECT dedup_key FROM observation_findings WHERE server_id = :s "
            "AND kind = 'security.exposed_listener' AND resolved_at IS NULL",
            s=uuid.UUID(SERVER_A),
        )
        assert open_rows == []
        # An INCOMPLETE table must not resolve or reopen anything.
        before = await _fetchall(
            sessions,
            "SELECT dedup_key, resolved_at, updated_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'security.exposed_listener' "
            "ORDER BY dedup_key",
            s=uuid.UUID(SERVER_A),
        )
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(tcp_complete=False, listeners=()),
            controller_now=NOW + timedelta(minutes=3),
        )
        after = await _fetchall(
            sessions,
            "SELECT dedup_key, resolved_at, updated_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'security.exposed_listener' "
            "ORDER BY dedup_key",
            s=uuid.UUID(SERVER_A),
        )
        assert before == after

    @pytest.mark.anyio
    async def test_no_data_finding_state_leaves_rows_untouched(self, sessions) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        before = await _fetchone(
            sessions,
            "SELECT updated_at, resolved_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'security.sudo_nopasswd'",
            s=uuid.UUID(SERVER_A),
        )
        # sudoers becomes unreadable: a GAP must not close the open finding.
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(sudo_nopasswd=None),
            controller_now=NOW + timedelta(minutes=1),
        )
        after = await _fetchone(
            sessions,
            "SELECT updated_at, resolved_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'security.sudo_nopasswd'",
            s=uuid.UUID(SERVER_A),
        )
        assert after.updated_at == before.updated_at
        assert after.resolved_at is None

    @pytest.mark.anyio
    async def test_unknown_collection_never_touches_findings(self, sessions) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        written = await record_unavailable(
            sessions, uuid.UUID(SERVER_A), controller_now=NOW + timedelta(minutes=1)
        )
        assert written == 1
        row = await _fetchone(
            sessions,
            "SELECT metric, value, observed_at FROM server_observations "
            "WHERE quality = 'unknown' AND server_id = :s",
            s=uuid.UUID(SERVER_A),
        )
        assert row is not None
        assert row.metric is None and row.value is None and row.observed_at is None
        finding = await _fetchone(
            sessions,
            "SELECT resolved_at, updated_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'security.sudo_nopasswd'",
            s=uuid.UUID(SERVER_A),
        )
        assert finding.resolved_at is None  # still open, untouched by the gap

    @pytest.mark.anyio
    async def test_two_servers_are_a_storage_boundary_and_freshness_derives(
        self, sessions
    ) -> None:
        await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        await record_unavailable(sessions, uuid.UUID(SERVER_B), controller_now=NOW)
        a_rows = await _fetchall(
            sessions,
            "SELECT quality, observed_at, fresh_until FROM server_observations "
            "WHERE server_id = :s",
            s=uuid.UUID(SERVER_A),
        )
        assert a_rows and all(r.quality == "degraded" for r in a_rows)
        b_rows = await _fetchall(
            sessions,
            "SELECT quality, observed_at, fresh_until FROM server_observations "
            "WHERE server_id = :s",
            s=uuid.UUID(SERVER_B),
        )
        assert len(b_rows) == 1 and b_rows[0].quality == "unknown"
        stored = a_rows[0]
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
        assert (
            derive_freshness(
                b_rows[0].quality, b_rows[0].observed_at, b_rows[0].fresh_until, NOW
            )
            is ObservationFreshness.UNKNOWN
        )


# ---------------------------------------------------------------------------
# Disposable Arch sshd fixture (runtime-environment ADR D1–D5; the card's
# exclusive runtime-target fixture, unique 10-security namespace)
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-10-security-arch"
FIXTURE_IMAGE_TAG = "mai-fxt-10-security-arch:sshd"
# NOTE: no tag-from-sibling fallback — this card's target needs sudo +
# openbsd-netcat on top of the reviewed 04 recipe, so the image is always
# built fresh into the unique namespace (provenance printed by the fixture).
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-10-security-tests")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_10_SECURITY_FIXTURE"


def _docker(*args: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _ensure_fixture_image() -> str:
    if _docker("image", "inspect", FIXTURE_IMAGE_TAG).returncode == 0:
        return "own tag present (built from archlinux:latest + openssh + sudo + openbsd-netcat)"
    context = FIXTURE_SCRATCH / "build-context"
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(
        "FROM archlinux:latest\n"
        "RUN pacman-key --init && pacman -Sy --noconfirm openssh sudo "
        "openbsd-netcat\n",
        encoding="utf-8",
    )
    build = _docker("build", "-q", "-t", FIXTURE_IMAGE_TAG, str(context), timeout=1200)
    assert build.returncode == 0, f"fixture image build failed: {build.stderr[-800:]}"
    return "built from archlinux:latest (+openssh, sudo, openbsd-netcat)"


class ArchSshdFixture:
    """One disposable Arch sshd container; removes itself with proof.

    sshd runs under a restart loop so the suite can reconfigure
    ``sshd_config`` and reload it WITHOUT touching the container lifecycle
    (``pkill -x sshd``; the loop relaunches within ~1s).
    """

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
            "&& while :; do /usr/bin/sshd -D; sleep 1; done",
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

    def run_remote(self, remote_command: str, timeout: float = 30.0) -> str:
        proc = self.run_remote_raw(remote_command, timeout=timeout)
        assert proc.returncode == 0, (
            f"fixture control command failed: {proc.stderr[-400:]}"
        )
        return proc.stdout

    def run_remote_raw(
        self, remote_command: str, timeout: float = 30.0
    ) -> subprocess.CompletedProcess[str]:
        """One control ssh round trip WITHOUT asserting on the outcome.

        The restart-loop fixture drops in-flight connections while
        ``pkill -x sshd`` reloads the daemon; control logic that survives
        that window (polling, reloading) needs the raw result.
        """
        assert self.identity is not None
        resolver = self.env_resolver()
        key_path = resolve_identity_key(resolver, self.identity.credential_ref)
        argv = ssh_arguments(
            self.identity,
            key_path=key_path,
            known_hosts_path=self.known_hosts_path,
            remote_command=remote_command,
        )
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )

    def _proc_tcp_count(self, hex_port: str) -> int:
        proc = self.run_remote_raw(
            f"grep -c ' 00000000:{hex_port} ' /proc/net/tcp 2>/dev/null || true"
        )
        count = proc.stdout.strip()
        return int(count) if proc.returncode == 0 and count.isdigit() else 0

    def wait_sshd_listening(self, attempts: int = 20) -> None:
        """The restart loop relaunched sshd: poll the kernel LISTEN table."""
        for _ in range(attempts):
            if self._proc_tcp_count("0016") > 0:
                return
            time.sleep(1.0)
        raise AssertionError("fixture sshd never re-listened after reload")

    def arm_weak_posture(self) -> None:
        """Gate-scenario weak state, all INSIDE the disposable container.

        - explicit weak sshd directives (reload via the restart loop);
        - one weak NOPASSWD sudoers grant whose LINE carries the content
          sentinel (the count may include it, the content may not);
        - a benign wildcard-bound listener on the allocated port;
        - a workload argv carrying the argv sentinel (the probe has no
          process-listing path — structural non-disclosure).
        """
        self.run_remote(
            "printf '\\nPermitRootLogin yes\\nPasswordAuthentication yes\\n' "
            ">> /etc/ssh/sshd_config"
        )
        self.run_remote(
            f"printf 'root ALL=(ALL) NOPASSWD: ALL # {CONTENT_SENTINEL}\\n' "
            "> /etc/sudoers.d/mai-fxt-weak && chmod 440 /etc/sudoers.d/mai-fxt-weak"
        )
        self.run_remote(
            f"setsid nc -l 0.0.0.0 {LISTEN_PORT} >/dev/null 2>&1 & "
            f"setsid sh -c 'exec sleep 600' fixture-argv0 {ARGV_SENTINEL} "
            ">/dev/null 2>&1 & sleep 1"
        )
        # The reload drops this very connection — tolerate the raw result.
        self.run_remote_raw("pkill -x sshd >/dev/null 2>&1; sleep 1; true")
        self.wait_sshd_listening()
        assert self._proc_tcp_count(f"{LISTEN_PORT:04X}") == 1

    def fix_posture(self) -> None:
        """Remove every planted weakness; keep the target serving SSH.

        The weak directives are deleted and explicit hardening directives
        appended (OpenSSH's upstream default for PasswordAuthentication is
        ``yes`` — only an explicit ``no`` resolves the check honestly).
        """
        self.run_remote(
            "sed -i '/^PermitRootLogin yes$/d; /^PasswordAuthentication yes$/d' "
            "/etc/ssh/sshd_config && printf '\\nPermitRootLogin prohibit-password\\n"
            "PasswordAuthentication no\\n' >> /etc/ssh/sshd_config"
        )
        self.run_remote("rm -f /etc/sudoers.d/mai-fxt-weak; pkill -x nc; true")
        self.run_remote_raw("pkill -x sshd >/dev/null 2>&1; sleep 1; true")
        self.wait_sshd_listening()
        assert self._proc_tcp_count(f"{LISTEN_PORT:04X}") == 0

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
    async def test_weak_posture_collected_then_fix_reevaluates(
        self, arch_sshd: ArchSshdFixture, migrated_url: str, sessions
    ) -> None:
        """The gate scenario: arm → collect evidence → fix → re-evaluate."""
        arch_sshd.arm_weak_posture()
        resolver = arch_sshd.env_resolver()
        key_path = resolve_identity_key(resolver, arch_sshd.identity.credential_ref)
        weak = await collect(
            sessions,
            arch_sshd.identity,
            key_path=key_path,
            known_hosts_path=arch_sshd.known_hosts_path,
            server_id=uuid.UUID(SERVER_A),
            controller_now=datetime.now(UTC),
        )
        assert weak.error is None
        assert weak.quality is ObservationQuality.DEGRADED  # firewall refusal gap

        rows = await _fetchall(
            sessions,
            "SELECT source, method, quality, metric, value, observed_at "
            "FROM server_observations WHERE server_id = :s",
            s=uuid.UUID(SERVER_A),
        )
        # Acceptance 1: source + timestamp on every row of a real sample.
        assert rows and all(r.source == SOURCE and r.method == "command" for r in rows)
        assert all(r.observed_at is not None for r in rows)
        assert all(
            len(r.metric) <= METRIC_MAX_CHARS
            and r.value is not None
            and len(r.value) <= METRIC_VALUE_MAX_CHARS
            for r in rows
        )
        metrics = {r.metric: r.value for r in rows}
        assert metrics["sec.sshd.permit_root_login"] == "yes"
        assert metrics["sec.sudo.nopasswd_count"] == "1"
        tcp_values = [r.value for r in rows if r.metric == "sec.listen.tcp"]
        assert f"0.0.0.0:{LISTEN_PORT}" in tcp_values

        open_findings = await _fetchall(
            sessions,
            "SELECT kind, dedup_key, summary, detail FROM observation_findings "
            "WHERE server_id = :s AND resolved_at IS NULL",
            s=uuid.UUID(SERVER_A),
        )
        weak_kinds = {f.kind for f in open_findings}
        assert {
            "security.sshd_root_login",
            "security.sshd_password_auth",
            "security.sudo_nopasswd",
            "security.exposed_listener",
        } <= weak_kinds
        assert f"tcp:0.0.0.0:{LISTEN_PORT}" in {
            f.dedup_key for f in open_findings if f.kind == "security.exposed_listener"
        }

        # Acceptance 2, secret half: NEITHER sentinel may appear anywhere in
        # the observation domain. The content sentinel sat inside the counted
        # sudoers LINE; the argv sentinel sat in a live workload argv.
        dump = await _storage_dump(sessions)
        assert CONTENT_SENTINEL not in dump
        assert ARGV_SENTINEL not in dump

        # Fix the fixture and re-evaluate.
        arch_sshd.fix_posture()
        fixed = await collect(
            sessions,
            arch_sshd.identity,
            key_path=key_path,
            known_hosts_path=arch_sshd.known_hosts_path,
            server_id=uuid.UUID(SERVER_A),
            controller_now=datetime.now(UTC),
        )
        assert fixed.error is None
        assert fixed.quality is ObservationQuality.DEGRADED
        fixed_rows = await _fetchall(
            sessions,
            "SELECT metric, value FROM server_observations WHERE server_id = :s "
            "AND collected_at = (SELECT max(collected_at) FROM server_observations "
            "WHERE server_id = :s2)",
            s=uuid.UUID(SERVER_A),
            s2=uuid.UUID(SERVER_A),
        )
        fixed_metrics = {r.metric: r.value for r in fixed_rows}
        assert fixed_metrics["sec.sshd.permit_root_login"] == "prohibit-password"
        assert fixed_metrics["sec.sudo.nopasswd_count"] == "0"

        findings = await _fetchall(
            sessions,
            "SELECT kind, dedup_key, resolved_at FROM observation_findings "
            "WHERE server_id = :s",
            s=uuid.UUID(SERVER_A),
        )
        state = {(f.kind, f.dedup_key): f.resolved_at is not None for f in findings}
        # The armed conditions closed against the real fixed target…
        assert state[("security.sshd_root_login", "config")]
        assert state[("security.sshd_password_auth", "config")]
        assert state[("security.sudo_nopasswd", "grants")]
        assert state[("security.exposed_listener", f"tcp:0.0.0.0:{LISTEN_PORT}")]
        # …while sshd's own wildcard listeners remain honestly open.
        assert not state[("security.exposed_listener", "tcp:0.0.0.0:22")]
        assert not state[("security.exposed_listener", "tcp6::::22")]

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
        assert before.quality is ObservationQuality.DEGRADED

        arch_sshd.stop()  # disconnect the target mid-lifecycle
        after = await collect(
            sessions,
            arch_sshd.identity,
            key_path=key_path,
            known_hosts_path=arch_sshd.known_hosts_path,
            server_id=uuid.UUID(SERVER_A),
            controller_now=datetime.now(UTC),
        )
        # Acceptance 2: unavailable target ⇒ unknown, NOT a clean posture.
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
        assert violations.n == 0  # the "never a reading" structural bound

    @pytest.mark.anyio
    async def test_real_permission_denied_records_unknown(
        self, arch_sshd: ArchSshdFixture, migrated_url: str, sessions
    ) -> None:
        # A target refusing the identity (permission boundary) takes the same
        # unknown path: recorded once, never evaluated as a clean posture.
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
