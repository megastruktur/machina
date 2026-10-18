"""Focused tests for the bounded network collector (card 10-network, C04 + C10).

Acceptance coverage (BRIEF):

1. **Read-only interface/listener/connectivity data scoped to target; no
   sniffing all traffic or changing firewall** — the fixed probe command
   (``PROBE_COMMAND``) only LISTS kernel state (``/sys/class/net``,
   ``/proc/net/{tcp,tcp6,udp,udp6,route}``, ``ip -o -4 addr show``); there is
   no packet-capture path and no firewall tool in the command. The controller
   hard-caps captured output (``MAX_PROBE_OUTPUT_BYTES``) and the parser
   re-applies the target-side ``head`` bounds. A real fixture listener opened
   on an allocated port is confirmed by its actual binding
   (``net.listen.tcp = 127.0.0.1:<port>``) and its interface via the IPv4
   address table (127.0.0.1 ∈ ``lo``'s ``127.0.0.1/8``); closing it and
   refreshing shows it gone.
2. **Source/permission gaps explicit and bounded output useful for chat** —
   every source carries its own presence marker: an unreadable/missing source
   (``*_rc`` ≠ 0, ``if_src=none``, ``addr_rc=none`` — e.g. a limited-
   privilege target without ``ip``) leaves that part of the sample UNKNOWN
   and absent from storage — never zero (the schema structurally rejects an
   unknown row with a measurement, and empty listings are only trusted when
   the source read succeeded, rc=0). Persisted values are short normalized
   renderings (interface state words, ``address:port``, ``iface via gateway``
   ) with source/method/quality/timestamp and the collector horizons on every
   row. Findings are health-flavored (default route, interface down);
   exposed-listener security judgment belongs to 10-security.

Layers per the test policy: pure parse/finding tests (always run);
transport-boundary tests over monkeypatched subprocess; persistence claims
on a real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``, real
Alembic chain to ``0007``); ``real_target``-marked tests against a disposable
Arch sshd container (``mai-fxt-10-network-arch`` namespace), auto-skipped
without docker and removed with recorded proof.

Secret hygiene: fixture keys are throwaway files under ``/tmp/mai-fxt-10-
network-tests``; the ``.env`` holds a PATH reference, never material; the
non-disclosure sentinel is a synthetic marker string, not a secret.
"""

from __future__ import annotations

import ipaddress
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
from mai.monitoring.network import (
    MAX_PROBE_OUTPUT_BYTES,
    MAX_TCP_LISTENERS,
    PROBE_COMMAND,
    PROBE_TIMEOUT_SECONDS,
    RETENTION_HORIZON,
    SOURCE,
    DefaultRoute,
    InterfaceAddress,
    InterfaceObservation,
    Listener,
    NetworkSample,
    collect,
    findings_for_sample,
    parse_probe_output,
    probe_network,
    record_sample,
    record_unavailable,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Frozen fixed fixture identities (canonical lowercase UUID strings, distinct
# from every other card's constants).
SERVER_A = "8a3d4e5f-6b7c-4a81-9c2d-3e4f5a6b7c8d"
SERVER_B = "9b4e5f6a-7c8d-4b92-ad3e-4f5a6b7c8d9e"

NOW = datetime(2026, 10, 17, 19, 0, 0, tzinfo=UTC)

#: Non-disclosure sentinel planted in a FIXTURE PROCESS ARGV on the real
#: target (NOT a secret): no observation row may ever contain it because the
#: network probe has no process-listing path at all.
ARGV_SENTINEL = "mai-10-network-argv-sentinel-not-a-secret"

#: The gate scenario's allocated listener port (fixture-internal, unused
#: unprivileged port on the disposable target; open → collect → confirm →
#: close → refresh).
LISTEN_PORT = 45678

# Real probe output captured verbatim from the disposable Arch fixture target
# (2026-10-17, mai-fxt-10-network-arch; openssh + iproute2 + openbsd-netcat
# image). Embedded as the parse fixture so the pure tests exercise the exact
# byte shape the real target emits: sshd listening on both families, lo with
# the kernel's own "unknown" operstate, one default route via the bridge
# gateway.
_ARCH_FIXTURE_OUTPUT = (
    "ts=2026-10-17T18:35:06\n"
    "if_src=ok\n"
    "if=eth0:up\n"
    "if=lo:unknown\n"
    "addr_rc=0\n"
    "addr=lo:127.0.0.1/8\n"
    "addr=eth0:172.17.0.5/16\n"
    "tcp_rc=0\n"
    "tcp_l=00000000:0016\n"
    "tcp6_rc=0\n"
    "tcp6_l=00000000000000000000000000000000:0016\n"
    "udp_rc=0\n"
    "udp6_rc=0\n"
    "route_rc=0\n"
    "route=eth0:010011AC\n"
)

# The same fixture with the gate scenario's listener OPEN on the allocated
# port (captured verbatim; 127.0.0.1:45678 = 0100007F:B26E).
_ARCH_LISTENER_OUTPUT = (
    "ts=2026-10-17T18:35:19\n"
    "if_src=ok\n"
    "if=eth0:up\n"
    "if=lo:unknown\n"
    "addr_rc=0\n"
    "addr=lo:127.0.0.1/8\n"
    "addr=eth0:172.17.0.5/16\n"
    "tcp_rc=0\n"
    "tcp_l=0100007F:B26E\n"
    "tcp_l=00000000:0016\n"
    "tcp6_rc=0\n"
    "tcp6_l=00000000000000000000000000000000:0016\n"
    "udp_rc=0\n"
    "udp6_rc=0\n"
    "route_rc=0\n"
    "route=eth0:010011AC\n"
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


def _sample(**overrides: object) -> NetworkSample:
    """A measured fixture-shaped sample (the real capture, normalized)."""
    fields: dict[str, object] = {
        "observed_at": NOW,
        "observed_at_source": "target",
        "quality": ObservationQuality.MEASURED,
        "interfaces_complete": True,
        "addresses_complete": True,
        "tcp_complete": True,
        "tcp6_complete": True,
        "udp_complete": True,
        "udp6_complete": True,
        "routes_complete": True,
        "interfaces": (
            InterfaceObservation(name="eth0", operstate="up"),
            InterfaceObservation(name="lo", operstate="unknown"),
        ),
        "addresses": (
            InterfaceAddress(name="lo", cidr="127.0.0.1/8"),
            InterfaceAddress(name="eth0", cidr="172.17.0.5/16"),
        ),
        "listeners": (Listener(proto="tcp", address="0.0.0.0", port=22),),
        "default_routes": (DefaultRoute(interface="eth0", gateway="172.17.0.1"),),
    }
    fields.update(overrides)
    return NetworkSample(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure parsing: bounded target output → honest sample
# ---------------------------------------------------------------------------


class TestParseProbeOutput:
    def test_real_fixture_capture_parses_measured(self) -> None:
        sample = parse_probe_output(_ARCH_FIXTURE_OUTPUT, controller_now=NOW)
        assert sample.quality is ObservationQuality.MEASURED
        # Target-reported timestamp, controller clock untouched.
        assert sample.observed_at == datetime(2026, 10, 17, 18, 35, 6, tzinfo=UTC)
        assert sample.observed_at_source == "target"
        # Interfaces: kernel names + honest operstate (lo IS "unknown" — the
        # kernel's own word, preserved; never rewritten to up/down).
        assert sample.interfaces == (
            InterfaceObservation(name="eth0", operstate="up"),
            InterfaceObservation(name="lo", operstate="unknown"),
        )
        assert sample.interfaces_complete is True
        # Address table maps names to bounded IPv4 cidr renderings.
        assert sample.addresses == (
            InterfaceAddress(name="lo", cidr="127.0.0.1/8"),
            InterfaceAddress(name="eth0", cidr="172.17.0.5/16"),
        )
        assert sample.addresses_complete is True
        # Listeners decoded from the /proc hex form: sshd on both families.
        assert sample.listeners == (
            Listener(proto="tcp", address="0.0.0.0", port=22),
            Listener(proto="tcp6", address="::", port=22),
        )
        assert sample.tcp_complete and sample.tcp6_complete
        assert sample.udp_complete and sample.udp6_complete
        # Default route decoded from the little-endian dword hex (010011AC =
        # 172.17.0.1) on its egress interface.
        assert sample.default_routes == (
            DefaultRoute(interface="eth0", gateway="172.17.0.1"),
        )
        assert sample.routes_complete is True

    def test_fixture_listener_capture_confirms_binding_and_interface(self) -> None:
        """Gate-scenario pure arm: the allocated port's ACTUAL binding.

        The listener is on 127.0.0.1 — confirmed as the exact bound address,
        and its interface confirmed via the address table (127.0.0.1 is inside
        lo's 127.0.0.1/8), not invented from the port number.
        """
        sample = parse_probe_output(_ARCH_LISTENER_OUTPUT, controller_now=NOW)
        assert sample.quality is ObservationQuality.MEASURED
        allocated = [
            listener for listener in sample.listeners if listener.port == LISTEN_PORT
        ]
        assert allocated == [
            Listener(proto="tcp", address="127.0.0.1", port=LISTEN_PORT)
        ]
        bound_network = ipaddress.ip_network("127.0.0.1/8", strict=False)
        assert ipaddress.ip_address(allocated[0].address) in bound_network
        lo_cidrs = {a.cidr for a in sample.addresses if a.name == "lo"}
        assert any(
            ipaddress.ip_address(allocated[0].address)
            in ipaddress.ip_network(cidr, strict=False)
            for cidr in lo_cidrs
        )
        # The wildcard sshd listeners stay distinct from the loopback binding.
        assert len(sample.listeners) == 3

    def test_empty_output_is_unknown_without_any_metric(self) -> None:
        sample = parse_probe_output("", controller_now=NOW)
        assert sample.quality is ObservationQuality.UNKNOWN
        assert sample.interfaces == ()
        assert sample.listeners == ()
        assert sample.addresses == ()
        assert sample.default_routes == ()
        assert not sample.interfaces_complete
        assert not sample.tcp_complete
        assert not sample.addresses_complete
        assert not sample.routes_complete
        assert sample.observed_at == NOW
        assert sample.observed_at_source == "controller"

    def test_partial_output_is_degraded_with_explicit_gaps(self) -> None:
        """The limited-privilege shape: no ``ip`` tool, no /proc/net/route.

        Interfaces and the IPv4 listener table are readable; the address
        table (``addr_rc=none``) and route source (``route_rc=1``) are
        explicit UNKNOWN gaps — the sample degrades, the gaps stay absent,
        and nothing is fabricated as zero.
        """
        output = (
            "ts=2026-10-17T10:00:00\n"
            "if_src=ok\n"
            "if=eth0:up\n"
            "addr_rc=none\n"
            "tcp_rc=0\n"
            "tcp_l=0100007F:1F90\n"
            "tcp6_rc=1\n"
            "udp_rc=1\n"
            "udp6_rc=1\n"
            "route_rc=1\n"
        )
        sample = parse_probe_output(output, controller_now=NOW)
        assert sample.quality is ObservationQuality.DEGRADED
        assert sample.interfaces_complete is True
        assert sample.addresses_complete is False  # the explicit data gap
        assert sample.addresses == ()
        assert sample.tcp_complete is True
        assert sample.tcp6_complete is False
        assert sample.udp_complete is False
        assert sample.udp6_complete is False
        assert sample.routes_complete is False
        assert sample.default_routes == ()
        # The readable listener source still yields its real entry.
        assert sample.listeners == (
            Listener(proto="tcp", address="127.0.0.1", port=8080),
        )

    def test_unparsable_timestamp_falls_back_to_controller_clock(self) -> None:
        sample = parse_probe_output("ts=nonsense\nif_src=ok\n", controller_now=NOW)
        assert sample.observed_at_source == "controller"
        assert sample.observed_at == NOW
        assert sample.quality is ObservationQuality.DEGRADED  # interfaces only

    def test_junk_values_become_none_never_zero(self) -> None:
        output = (
            "if_src=ok\n"
            "if=eth0:hosed\n"  # not an operstate word
            "if=@@@bad:up\n"  # not an interface name
            "addr_rc=0\n"
            "addr=eth0:not-an-ip/8\n"  # not a cidr
            "addr=eth0:999.999.999.999/8\n"  # not an address
            "route_rc=0\n"
            "route=eth0:ZZZZZZZZ\n"  # not hex
            "route=@@@:010011AC\n"  # not an interface name
        )
        sample = parse_probe_output(output, controller_now=NOW)
        # Sources claim ok, so emptiness here is parse-rejection — and every
        # rejected entry is ABSENT (unknown/gap), never zeroed state. eth0 is
        # a REAL interface whose state word is outside the kernel vocabulary:
        # it stays, with the state itself the explicit gap (None), never
        # "down"; the hostile name is skipped outright.
        assert sample.interfaces == (InterfaceObservation(name="eth0", operstate=None),)
        assert sample.addresses == ()
        assert sample.default_routes == ()
        assert sample.interfaces_complete is True
        assert sample.addresses_complete is True
        assert sample.routes_complete is True

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

    def test_first_occurrence_wins_for_repeated_keys(self) -> None:
        sample = parse_probe_output("tcp_rc=0\ntcp_rc=1\n", controller_now=NOW)
        assert sample.tcp_complete is True

    def test_zero_listeners_only_when_table_read(self) -> None:
        # rc=0 with zero rows is a TRUSTED empty listing (a host genuinely
        # listening on nothing in that table).
        sample = parse_probe_output(
            "tcp_rc=0\ntcp6_rc=0\nudp_rc=0\nudp6_rc=0\n", controller_now=NOW
        )
        assert sample.listeners == ()
        assert sample.tcp_complete and sample.tcp6_complete
        assert sample.udp_complete and sample.udp6_complete
        assert sample.quality is ObservationQuality.DEGRADED  # no if/addr/route
        # A refused/absent listener table is NOT zero listeners.
        sample = parse_probe_output("tcp_rc=1\n", controller_now=NOW)
        assert sample.tcp_complete is False
        assert sample.listeners == ()  # absent, but flagged unknown above

    def test_listener_listings_are_bounded(self) -> None:
        lines = [f"tcp_l=0A0000{i:02X}:0050" for i in range(MAX_TCP_LISTENERS + 8)]
        sample = parse_probe_output(
            "tcp_rc=0\n" + "\n".join(lines) + "\n", controller_now=NOW
        )
        assert len(sample.listeners) == MAX_TCP_LISTENERS

    def test_controller_now_must_be_timezone_aware(self) -> None:
        with pytest.raises(ValueError):
            parse_probe_output("if_src=ok\n", controller_now=datetime(2026, 10, 17))


# ---------------------------------------------------------------------------
# Pure findings: health-flavored thresholds, three-valued states
# ---------------------------------------------------------------------------


class TestFindingsForSample:
    def test_missing_default_route_is_active_warning(self) -> None:
        drafts = findings_for_sample(_sample(default_routes=()))
        route = next(d for d in drafts if d.kind == "network.default_route")
        assert (route.state, route.severity) == ("active", "warning")
        assert "0 default route(s): none configured" in route.summary

    def test_present_default_route_closes_finding(self) -> None:
        drafts = findings_for_sample(_sample())
        route = next(d for d in drafts if d.kind == "network.default_route")
        assert (route.state, route.severity) == ("clear", "info")
        assert "via eth0 (gateway 172.17.0.1)" in route.summary

    def test_unknown_route_source_is_no_data_never_active(self) -> None:
        drafts = findings_for_sample(_sample(routes_complete=False, default_routes=()))
        route = next(d for d in drafts if d.kind == "network.default_route")
        assert route.state == "no_data"
        # Missing data is never evidence the condition ended (or started).

    def test_interface_down_is_active_warning_with_bounded_listing(self) -> None:
        sample = _sample(
            interfaces=(
                InterfaceObservation(name="eth0", operstate="down"),
                InterfaceObservation(name="eth1", operstate="down"),
                InterfaceObservation(name="eth2", operstate="down"),
                InterfaceObservation(name="eth3", operstate="down"),
                InterfaceObservation(name="lo", operstate="unknown"),
            )
        )
        drafts = findings_for_sample(sample)
        down = next(d for d in drafts if d.kind == "network.interface_down")
        assert (down.state, down.severity) == ("active", "warning")
        assert "4 non-loopback interface(s) down" in down.summary
        assert "eth0, eth1, eth2 (+1 more)" in down.summary

    def test_all_up_closes_and_loopback_state_is_not_a_finding(self) -> None:
        drafts = findings_for_sample(_sample())
        down = next(d for d in drafts if d.kind == "network.interface_down")
        assert (down.state, down.severity) == ("clear", "info")
        # lo reporting a transient state word must never open the finding.
        sample = _sample(
            interfaces=(
                InterfaceObservation(name="lo", operstate="down"),
                InterfaceObservation(name="eth0", operstate="up"),
            )
        )
        assert (
            next(
                d
                for d in findings_for_sample(sample)
                if d.kind == "network.interface_down"
            ).state
            == "clear"
        )

    def test_unknown_interface_listing_is_no_data(self) -> None:
        drafts = findings_for_sample(_sample(interfaces_complete=False, interfaces=()))
        down = next(d for d in drafts if d.kind == "network.interface_down")
        assert down.state == "no_data"

    def test_degraded_operstate_word_is_a_gap_not_down(self) -> None:
        sample = _sample(
            interfaces=(
                InterfaceObservation(name="eth0", operstate=None),
                InterfaceObservation(name="eth1", operstate="weird"),
            )
        )
        down = next(
            d for d in findings_for_sample(sample) if d.kind == "network.interface_down"
        )
        # An unreadable/nonsense operstate is unknown data — never "down".
        assert down.state == "clear"


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


class TestProbeBoundary:
    def test_missing_known_hosts_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(SshConnectError) as exc:
            probe_network(
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
            return _FakeProc(stdout=_ARCH_FIXTURE_OUTPUT)

        monkeypatch.setattr("mai.monitoring.network.subprocess.run", fake_run)
        key_path = tmp_path / "key"
        known_hosts = tmp_path / "known_hosts"
        known_hosts.write_text("")
        probe_network(
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
            "mai.monitoring.network.subprocess.run",
            lambda argv, **kwargs: _FakeProc(stdout=_ARCH_FIXTURE_OUTPUT),
        )
        sample = probe_network(
            _identity(),
            key_path=tmp_path / "k",
            known_hosts_path=_known_hosts(tmp_path),
            controller_now=NOW,
        )
        assert sample.quality is ObservationQuality.MEASURED
        assert sample.observed_at_source == "target"

    def test_oversized_probe_output_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "mai.monitoring.network.subprocess.run",
            lambda argv, **kwargs: _FakeProc(stdout="x" * (MAX_PROBE_OUTPUT_BYTES + 1)),
        )
        with pytest.raises(SshConnectError) as exc:
            probe_network(
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

        monkeypatch.setattr("mai.monitoring.network.subprocess.run", raise_timeout)
        with pytest.raises(SshConnectError) as exc:
            probe_network(
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
                "mai.monitoring.network.subprocess.run",
                _refusing_run(stderr),
            )
            with pytest.raises(SshConnectError) as exc:
                probe_network(
                    _identity(),
                    key_path=tmp_path / "k",
                    known_hosts_path=_known_hosts(tmp_path),
                    controller_now=NOW,
                )
            assert exc.value.code is expected


def _known_hosts(tmp_path: Path) -> Path:
    path = tmp_path / "managed_known_hosts"
    path.write_text("")
    return path


def _refusing_run(stderr: str):
    """A subprocess.run double whose ssh exits 255 with ``stderr``."""

    def refusing_run(argv, **kwargs):
        return _FakeProc(stderr=stderr, returncode=255)

    return refusing_run


# ---------------------------------------------------------------------------
# Persistence into the real 0007 schema
# ---------------------------------------------------------------------------


class TestRecordSample:
    @pytest.mark.anyio
    async def test_measured_snapshot_persists_bounded_rows(self, sessions) -> None:
        written = await record_sample(
            sessions, uuid.UUID(SERVER_A), _sample(), controller_now=NOW
        )
        # 2 interfaces + 2 addresses + 1 listener + 1 route + 2 counts.
        assert written == 8
        rows = await _fetchall(
            sessions,
            "SELECT metric, value, source, method, quality, observed_at, "
            "collected_at, fresh_until, retain_until, seq "
            "FROM server_observations WHERE server_id = :s ORDER BY metric",
            s=uuid.UUID(SERVER_A),
        )
        assert {r.metric: r.value for r in rows} == {
            "net.if.eth0": "up",
            "net.if.lo": "unknown",  # the kernel's own honest state word
            "net.addr.lo": "127.0.0.1/8",
            "net.addr.eth0": "172.17.0.5/16",
            "net.listen.tcp": "0.0.0.0:22",
            "net.route.default": "eth0 via 172.17.0.1",
            "net.iface_count": "2",
            "net.listener_count": "1",
        }
        # Acceptance 1: source + timestamp + horizons on every row.
        assert rows and all(
            r.source == SOURCE and r.method == "command" and r.quality == "measured"
            for r in rows
        )
        assert all(r.observed_at == NOW for r in rows)
        assert all(r.fresh_until == NOW + timedelta(minutes=15) for r in rows)
        assert all(r.retain_until == NOW + RETENTION_HORIZON for r in rows)
        assert all(r.seq > 0 for r in rows)

    @pytest.mark.anyio
    async def test_unknown_with_a_metric_is_a_real_integrity_violation(
        self, sessions
    ) -> None:
        # The schema itself forbids the "zero usage" lie for an unknown row.
        with pytest.raises(IntegrityError):
            async with sessions() as session:
                await session.execute(
                    text(
                        "INSERT INTO server_observations (observation_id, server_id, "
                        "source, method, quality, metric, value, collected_at, "
                        "retain_until) VALUES (:oid, :sid, 'network', 'command', "
                        "'unknown', 'net.listen.tcp', '0', now(), now())"
                    ),
                    {"oid": uuid.uuid4(), "sid": uuid.UUID(SERVER_A)},
                )
                await session.commit()

    @pytest.mark.anyio
    async def test_oversized_metric_or_value_rejected_by_schema_bounds(
        self, sessions
    ) -> None:
        # The bounded-output guarantee is structural: no metric/value can
        # exceed the contract bounds, so no unbounded transcript can land.
        with pytest.raises(IntegrityError):
            async with sessions() as session:
                await session.execute(
                    text(
                        "INSERT INTO server_observations (observation_id, server_id, "
                        "source, method, quality, metric, value, observed_at, "
                        "collected_at, fresh_until, retain_until) VALUES "
                        "(:oid, :sid, 'network', 'command', 'measured', :m, 'v', "
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
                        "(:oid, :sid, 'network', 'command', 'measured', 'net.if.eth0', "
                        ":v, now(), now(), now(), now())"
                    ),
                    {
                        "oid": uuid.uuid4(),
                        "sid": uuid.UUID(SERVER_A),
                        "v": "x" * (METRIC_VALUE_MAX_CHARS + 1),
                    },
                )
                await session.commit()

    @pytest.mark.anyio
    async def test_degraded_sample_persists_gap_as_absent_not_zero(
        self, sessions
    ) -> None:
        """The limited-privilege gap is displayed as unknown/absent in storage."""
        sample = _sample(
            quality=ObservationQuality.DEGRADED,
            addresses_complete=False,
            addresses=(),
        )
        written = await record_sample(
            sessions, uuid.UUID(SERVER_A), sample, controller_now=NOW
        )
        assert written == 6  # gap contributes NO row at all
        rows = await _fetchall(
            sessions,
            "SELECT metric, quality FROM server_observations WHERE server_id = :s",
            s=uuid.UUID(SERVER_A),
        )
        assert all(r.quality == "degraded" for r in rows)
        assert not any(r.metric.startswith("net.addr.") for r in rows)
        # And no fabricated zero-count stand-in for the missing table.
        assert not any("addr" in (r.metric or "") for r in rows)

    @pytest.mark.anyio
    async def test_findings_open_close_reopen_lifecycle(self, sessions) -> None:
        # Open: no default route.
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(default_routes=()),
            controller_now=NOW,
        )
        opened = await _fetchone(
            sessions,
            "SELECT finding_id, severity, summary, opened_at, updated_at, resolved_at "
            "FROM observation_findings WHERE server_id = :s AND kind = 'network.default_route'",
            s=uuid.UUID(SERVER_A),
        )
        assert opened is not None
        assert opened.resolved_at is None and opened.severity == "warning"
        # Close: the route appears.
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(),
            controller_now=NOW + timedelta(minutes=1),
        )
        closed = await _fetchone(
            sessions,
            "SELECT finding_id, severity, resolved_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'network.default_route'",
            s=uuid.UUID(SERVER_A),
        )
        assert closed.finding_id == opened.finding_id  # dedup: same row updated
        # Closing stamps resolved_at only; the episode keeps the severity it
        # was opened with (the close path never rewrites the row's policy).
        assert closed.resolved_at is not None
        assert closed.severity == "warning"
        # Reopen: the route disappears again; the same row flips back open.
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(default_routes=()),
            controller_now=NOW + timedelta(minutes=2),
        )
        reopened = await _fetchone(
            sessions,
            "SELECT finding_id, severity, resolved_at, updated_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'network.default_route'",
            s=uuid.UUID(SERVER_A),
        )
        assert reopened.finding_id == opened.finding_id
        assert reopened.resolved_at is None and reopened.severity == "warning"
        assert reopened.updated_at > opened.updated_at

    @pytest.mark.anyio
    async def test_no_data_finding_state_leaves_rows_untouched(self, sessions) -> None:
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(default_routes=()),
            controller_now=NOW,
        )
        before = await _fetchone(
            sessions,
            "SELECT updated_at, resolved_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'network.default_route'",
            s=uuid.UUID(SERVER_A),
        )
        # A sample whose route SOURCE is unknown must not touch the finding:
        # missing data neither closes nor re-affirms the condition.
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(routes_complete=False, default_routes=()),
            controller_now=NOW + timedelta(minutes=3),
        )
        after = await _fetchone(
            sessions,
            "SELECT updated_at, resolved_at FROM observation_findings "
            "WHERE server_id = :s AND kind = 'network.default_route'",
            s=uuid.UUID(SERVER_A),
        )
        assert after.updated_at == before.updated_at
        assert after.resolved_at is None

    @pytest.mark.anyio
    async def test_unknown_collection_never_touches_findings(self, sessions) -> None:
        await record_sample(
            sessions,
            uuid.UUID(SERVER_A),
            _sample(default_routes=()),
            controller_now=NOW,
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
            "WHERE server_id = :s AND kind = 'network.default_route'",
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
        assert a_rows and all(r.quality == "measured" for r in a_rows)
        b_rows = await _fetchall(
            sessions,
            "SELECT quality, observed_at, fresh_until FROM server_observations "
            "WHERE server_id = :s",
            s=uuid.UUID(SERVER_B),
        )
        assert len(b_rows) == 1 and b_rows[0].quality == "unknown"
        # Freshness is derived against the consumer's clock from stored
        # columns: fresh inside the horizon, stale past it, unknown without.
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
# exclusive runtime-target fixture, unique 10-network namespace)
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-10-network-arch"
FIXTURE_IMAGE_TAG = "mai-fxt-10-network-arch:sshd"
# NOTE: no tag-from-04 fallback — this card's target needs iproute2 +
# openbsd-netcat on top of the reviewed 04 recipe, so the image is always
# built fresh into the unique namespace (provenance printed by the fixture).
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-10-network-tests")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_10_NETWORK_FIXTURE"


def _docker(*args: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _ensure_fixture_image() -> str:
    if _docker("image", "inspect", FIXTURE_IMAGE_TAG).returncode == 0:
        return "own tag present (built from archlinux:latest + openssh + iproute2 + openbsd-netcat)"
    context = FIXTURE_SCRATCH / "build-context"
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(
        "FROM archlinux:latest\n"
        "RUN pacman-key --init && pacman -Sy --noconfirm openssh iproute2 "
        "openbsd-netcat\n",
        encoding="utf-8",
    )
    build = _docker("build", "-q", "-t", FIXTURE_IMAGE_TAG, str(context), timeout=1200)
    assert build.returncode == 0, f"fixture image build failed: {build.stderr[-800:]}"
    return "built from archlinux:latest (+openssh, iproute2, openbsd-netcat)"


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

    def start_fixture_workload(self, sentinel: str, port: int) -> None:
        """Gate-scenario workload: the allocated listener + an ARGV sentinel.

        ``nc -l 127.0.0.1 <port>`` is the fixture listener (loopback-bound,
        unprivileged); ``sh -c 'sleep 120; :' fixture-argv0 <sentinel>`` keeps
        a shell alive for the whole window with the sentinel in its argv
        (two commands prevent the exec-tail optimization). Both live inside
        the disposable container and die with it.
        """
        self.run_remote(
            f"setsid nc -l 127.0.0.1 {port} >/dev/null 2>&1 & "
            f"setsid sh -c 'sleep 120; :' fixture-argv0 {sentinel} >/dev/null 2>&1 & "
            "sleep 1"
        )

    def listener_port_open(self, port: int) -> bool:
        # Read-only check against the kernel table itself. An ``nc -z``
        # probe would CONNECT to the listener — openbsd ``nc -l`` accepts
        # one connection and exits, consuming the fixture.
        hex_port = f"{port:04X}"
        out = self.run_remote(f"grep -c ' 0100007F:{hex_port} ' /proc/net/tcp || true")
        count = out.strip()
        return count.isdigit() and int(count) > 0

    def stop_listener(self) -> None:
        self.run_remote("pkill -x nc >/dev/null 2>&1; sleep 1; true")

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
    async def test_real_listener_cycle_confirms_binding_interface_and_stays_sentinel_free(
        self, arch_sshd: ArchSshdFixture, migrated_url: str, sessions
    ) -> None:
        """The gate scenario: open → collect → confirm → close → refresh."""
        arch_sshd.start_fixture_workload(ARGV_SENTINEL, LISTEN_PORT)
        assert arch_sshd.listener_port_open(LISTEN_PORT)

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
        assert rows and all(r.source == SOURCE and r.method == "command" for r in rows)
        assert all(r.observed_at is not None for r in rows)
        drift = max(
            abs((r.observed_at - datetime.now(UTC)).total_seconds()) for r in rows
        )
        assert drift < 600  # target clock ≈ controller clock (shared host)
        # The allocated listener's ACTUAL binding, confirmed in storage.
        # (Multiple tcp rows share the metric name; compare the value list.)
        tcp_values = [r.value for r in rows if r.metric == "net.listen.tcp"]
        assert f"127.0.0.1:{LISTEN_PORT}" in tcp_values
        # Its interface confirmed via the real address table (lo's 127.0.0.1/8).
        assert metrics["net.addr.lo"] == "127.0.0.1/8"
        assert ipaddress.ip_address("127.0.0.1") in ipaddress.ip_network(
            metrics["net.addr.lo"], strict=False
        )
        # Bounded chat-useful renderings everywhere; counts present.
        assert all(
            len(r.metric) <= METRIC_MAX_CHARS
            and r.value is not None
            and len(r.value) <= METRIC_VALUE_MAX_CHARS
            for r in rows
        )
        assert int(metrics["net.iface_count"]) >= 2
        assert int(metrics["net.listener_count"]) >= 1
        # Acceptance 2, structural half: no unknown row carries a measurement.
        assert all(
            r.metric is None and r.value is None and r.observed_at is None
            for r in rows
            if r.quality == "unknown"
        )
        # No secret sentinel from process args, anywhere in the domain.
        assert ARGV_SENTINEL not in await _storage_dump(sessions)

        # Close it and refresh: the binding disappears from a fresh sample.
        arch_sshd.stop_listener()
        assert not arch_sshd.listener_port_open(LISTEN_PORT)
        refresh = await collect(
            sessions,
            arch_sshd.identity,
            key_path=key_path,
            known_hosts_path=arch_sshd.known_hosts_path,
            server_id=uuid.UUID(SERVER_A),
            controller_now=datetime.now(UTC),
        )
        assert refresh.error is None and refresh.quality is ObservationQuality.MEASURED
        rows2 = await _fetchall(
            sessions,
            "SELECT metric, value FROM server_observations WHERE server_id = :s "
            "AND collected_at = (SELECT max(collected_at) FROM server_observations "
            "WHERE server_id = :s2) AND quality <> 'unknown'",
            s=uuid.UUID(SERVER_A),
            s2=uuid.UUID(SERVER_A),
        )
        tcp_values2 = [r.value for r in rows2 if r.metric == "net.listen.tcp"]
        assert f"127.0.0.1:{LISTEN_PORT}" not in tcp_values2
        # sshd's own wildcard listener remains the honest remaining content.
        assert "0.0.0.0:22" in tcp_values2

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
        # Acceptance 2: unavailable target ⇒ unknown, NOT zero data.
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
        assert violations.n == 0  # the "never zero" structural bound
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
