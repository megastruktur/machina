"""Bounded network interface/listener observations over SSH (card 10-network, C04 + C10).

Frozen-shape producer for the deterministic ``network`` collector, sibling of
:mod:`mai.monitoring.system`: one fixed, read-only, self-bounding probe command
gathers an interface/listener/default-route snapshot from a pinned-identity SSH
target (the ``04-ssh-connect`` transport: exactly the frozen probe option
surface, host-key pinning, no credential in argv) and the results persist as
bounded rows of the ``0007_observations`` schema:

- **C10 rule 4** — every sample reports timestamp/method/quality. One row per
  metric: ``source='network'``, ``method='command'``, ``quality`` per
  :class:`mai.contracts.observations.ObservationQuality`, ``observed_at``
  (target-reported time when the target supplies one, controller time
  otherwise), ``fresh_until`` = collector-declared freshness horizon, and
  ``retain_until`` = retention bound (the sweep itself is the scheduled
  collectors card's ownership).
- **Acceptance 1 ("read-only, scoped to target")** — the probe only LISTS
  kernel state: ``/sys/class/net`` interface names + ``operstate``,
  ``/proc/net/{tcp,tcp6,udp,udp6}`` listener/bound-socket tables,
  ``/proc/net/route`` default routes and ``ip -o -4 addr show`` for the
  address table. There is NO packet capture path (no tcpdump/strace, no
  promiscuous mode), NO firewall query or change (no iptables/nft), and no
  write anywhere on the target. Output is self-bounded target-side (``head``
  on every listing) and hard-capped controller-side
  (:data:`MAX_PROBE_OUTPUT_BYTES`); every persisted value is a short
  normalized rendering (dotted-quad/compressed-IPv6 address, port, interface
  name) — never a raw transcript.
- **Acceptance 2 ("gaps explicit, bounded output useful for chat")** — every
  source carries its own three-valued presence: an unreadable/missing source
  (``*_rc`` nonzero, ``if_src=none``, ``addr_rc=none`` — e.g. a target
  without ``ip``) leaves that part of the sample UNKNOWN, never zero: zero
  listeners is only ever asserted when the listener table was actually read
  (the ``systemd`` rc pattern of the sibling collector). Sample quality is
  measured/degraded/unknown over the four core sources. Findings are
  health-flavored only (missing default route, interface down); judging an
  exposed listener as a security risk belongs to the separate 10-security
  card, which consumes this collector's data.

Threshold policy is collector-owned (finding ``kind`` values are collector
references, not a frozen vocabulary — 10-observation-schema): two
``network.*`` kinds with deterministic dedup keys, upserted with
``ON CONFLICT (server_id, kind, dedup_key)`` so repeated polls collapse onto
one finding per condition.

The probe runs synchronously (same shape as the P04 ``probe_environment``:
system ``ssh`` client via :func:`subprocess.run`), persistence is async on
the single application engine (:func:`mai.database.create_app_engine`). A
collection attempt never raises for target unavailability — it returns the
unknown path — but controller misconfiguration (unresolvable key reference,
missing enrollment) still raises, failing closed.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.observations import (
    SUMMARY_MAX_CHARS,
    ObservationMethod,
    ObservationQuality,
)
from mai.contracts.server import SshIdentity
from mai.execution.ssh import (
    SshConnectError,
    SshErrorCode,
    ssh_arguments,
)

__all__ = [
    "FRESH_HORIZON",
    "MAX_ADDRESSES",
    "MAX_INTERFACES",
    "MAX_PROBE_OUTPUT_BYTES",
    "MAX_ROUTES",
    "MAX_TCP_LISTENERS",
    "MAX_UDP_SOCKETS",
    "PROBE_COMMAND",
    "PROBE_TIMEOUT_SECONDS",
    "RETENTION_HORIZON",
    "SOURCE",
    "CollectionResult",
    "DefaultRoute",
    "FindingDraft",
    "InterfaceAddress",
    "InterfaceObservation",
    "Listener",
    "NetworkSample",
    "collect",
    "findings_for_sample",
    "parse_probe_output",
    "probe_network",
    "record_sample",
    "record_unavailable",
]

#: Collector identity persisted on every row of this collector.
SOURCE = "network"

#: Wall-clock budget for one probe round trip (connect timeout is the frozen
#: P04 :data:`CONNECT_TIMEOUT_SECONDS`).
PROBE_TIMEOUT_SECONDS = 30.0

#: Hard cap on captured probe output. The probe emits at most a few hundred
#: lines by construction; anything larger is a hostile/broken target.
MAX_PROBE_OUTPUT_BYTES = 32 * 1024

#: Collector-declared freshness horizon (C10 rule 4: consumers derive
#: staleness against their own clock via ``derive_freshness``).
FRESH_HORIZON = timedelta(minutes=15)

#: Retention bound stamped on every row this collector writes.
RETENTION_HORIZON = timedelta(days=7)

# Target-side listing bounds (enforced by ``head`` inside the probe; the
# parser re-applies them so a lying echo cannot inflate storage).
MAX_INTERFACES = 32
MAX_ADDRESSES = 32
MAX_TCP_LISTENERS = 32
MAX_UDP_SOCKETS = 16
MAX_ROUTES = 8

#: The single remote command this module executes: fixed, read-only,
#: self-bounding, POSIX-sh (busybox targets parse the same). It LISTS kernel
#: state only — interface names/operstate from ``/sys/class/net``, listener
#: tables from ``/proc/net/tcp*`` (state ``0A`` = LISTEN) and bound-idle UDP
#: sockets from ``/proc/net/udp*`` (the netstat zero-queue convention), the
#: IPv4 default routes from ``/proc/net/route`` and the IPv4 address table
#: from ``ip -o -4 addr show``. Every listing is failure-tolerant: its rc
#: marker distinguishes "source read, possibly zero entries" (rc=0) from
#: "source absent/refused" (rc≠0/none) — a gap is never reported as a zero.
PROBE_COMMAND = (
    "printf 'ts=%s\\n' \"$(date -u '+%Y-%m-%dT%H:%M:%S' 2>/dev/null)\"; "
    "if [ -d /sys/class/net ]; then "
    "printf 'if_src=ok\\n'; "
    "for IF in /sys/class/net/*; do "
    '[ -e "$IF" ] && printf \'if=%s:%s\\n\' "${IF##*/}" '
    '"$(cat "$IF/operstate" 2>/dev/null)"; done | head -n 32; '
    "else printf 'if_src=none\\n'; fi; "
    "if command -v ip >/dev/null 2>&1; then "
    "ip -o -4 addr show >/dev/null 2>&1; printf 'addr_rc=%s\\n' \"$?\"; "
    "ip -o -4 addr show 2>/dev/null | head -n 32 | sed -n "
    "'s/^[0-9]\\{1,\\}: *\\([^[:space:]]\\{1,\\}\\)[[:space:]]\\{1,\\}inet "
    "\\([0-9]\\{1,3\\}\\.[0-9]\\{1,3\\}\\.[0-9]\\{1,3\\}\\.[0-9]\\{1,3\\}"
    "\\/[0-9]\\{1,2\\}\\)[[:space:]].*/addr=\\1:\\2/p'; "
    "else printf 'addr_rc=none\\n'; fi; "
    "sed -n 's/^ *[0-9]\\{1,\\}: \\([0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\}\\) "
    "[0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\} 0A .*/tcp_l=\\1/p' /proc/net/tcp "
    ">/dev/null 2>&1; printf 'tcp_rc=%s\\n' \"$?\"; "
    "sed -n 's/^ *[0-9]\\{1,\\}: \\([0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\}\\) "
    "[0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\} 0A .*/tcp_l=\\1/p' /proc/net/tcp "
    "2>/dev/null | head -n 32; "
    "sed -n 's/^ *[0-9]\\{1,\\}: \\([0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\}\\) "
    "[0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\} 0A .*/tcp6_l=\\1/p' /proc/net/tcp6 "
    ">/dev/null 2>&1; printf 'tcp6_rc=%s\\n' \"$?\"; "
    "sed -n 's/^ *[0-9]\\{1,\\}: \\([0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\}\\) "
    "[0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\} 0A .*/tcp6_l=\\1/p' /proc/net/tcp6 "
    "2>/dev/null | head -n 32; "
    "sed -n 's/^ *[0-9]\\{1,\\}: \\([0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\}\\) "
    "[0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\} [0-9A-F]\\{2\\} "
    "00000000:00000000 .*/udp_b=\\1/p' /proc/net/udp "
    ">/dev/null 2>&1; printf 'udp_rc=%s\\n' \"$?\"; "
    "sed -n 's/^ *[0-9]\\{1,\\}: \\([0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\}\\) "
    "[0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\} [0-9A-F]\\{2\\} "
    "00000000:00000000 .*/udp_b=\\1/p' /proc/net/udp "
    "2>/dev/null | head -n 16; "
    "sed -n 's/^ *[0-9]\\{1,\\}: \\([0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\}\\) "
    "[0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\} [0-9A-F]\\{2\\} "
    "00000000:00000000 .*/udp6_b=\\1/p' /proc/net/udp6 "
    ">/dev/null 2>&1; printf 'udp6_rc=%s\\n' \"$?\"; "
    "sed -n 's/^ *[0-9]\\{1,\\}: \\([0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\}\\) "
    "[0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\} [0-9A-F]\\{2\\} "
    "00000000:00000000 .*/udp6_b=\\1/p' /proc/net/udp6 "
    "2>/dev/null | head -n 16; "
    "sed -n 's/^\\([^[:space:]]\\{1,\\}\\)[[:space:]]\\{1,\\}00000000"
    "[[:space:]]\\{1,\\}\\([0-9A-F]\\{8\\}\\)[[:space:]].*/route=\\1:\\2/p' "
    "/proc/net/route >/dev/null 2>&1; printf 'route_rc=%s\\n' \"$?\"; "
    "sed -n 's/^\\([^[:space:]]\\{1,\\}\\)[[:space:]]\\{1,\\}00000000"
    "[[:space:]]\\{1,\\}\\([0-9A-F]\\{8\\}\\)[[:space:]].*/route=\\1:\\2/p' "
    "/proc/net/route 2>/dev/null | head -n 8"
)

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

# Kernel /sys operstate vocabulary (IEEE 802.3 operational states); anything
# else is a data gap, not a renamed state.
_OPERSTATES = frozenset(
    {"up", "down", "testing", "dormant", "notpresent", "lowerlayerdown", "unknown"}
)

_INTERFACE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,15}$")
_CIDR_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}$")
_HEX32_RE = re.compile(r"^[0-9A-F]{32}$")
_HEX8_RE = re.compile(r"^[0-9A-F]{8}$")
_HEXDIGITS = frozenset("0123456789ABCDEF")


def _ipv4_hex(raw: str) -> str | None:
    """Decode a ``/proc`` big-endian-word hex dword into dotted quad."""
    if len(raw) != 8 or not _HEXDIGITS.issuperset(raw):
        return None
    return str(ipaddress.IPv4Address(bytes.fromhex(raw)[::-1]))


def _ipv6_hex(raw: str) -> str | None:
    """Decode a ``/proc/net/tcp6`` 32-hex address (four LE words)."""
    if not _HEX32_RE.fullmatch(raw):
        return None
    words = [bytes.fromhex(raw[i : i + 8])[::-1] for i in range(0, 32, 8)]
    return str(ipaddress.IPv6Address(b"".join(words)))


# ---------------------------------------------------------------------------
# Sample shape (pure; what one probe round trip observed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InterfaceObservation:
    """One network interface: kernel name + operational state.

    ``operstate`` is ``None`` when the state file was unreadable or its
    value left the kernel vocabulary — a data gap, never silently ``down``.
    """

    name: str
    operstate: str | None


@dataclass(frozen=True, slots=True)
class InterfaceAddress:
    """One IPv4 address table entry: interface name + ``ip/prefix``."""

    name: str
    cidr: str


@dataclass(frozen=True, slots=True)
class Listener:
    """One listening/bound socket as the target reported it.

    ``tcp``/``tcp6`` rows are kernel LISTEN states; ``udp``/``udp6`` rows are
    bound-idle sockets (the zero-queue convention). ``address`` is a dotted
    quad (v4) or the compressed IPv6 form (``0.0.0.0``/``::`` = any
    interface); it states the binding, nothing more.
    """

    proto: Literal["tcp", "tcp6", "udp", "udp6"]
    address: str
    port: int


@dataclass(frozen=True, slots=True)
class DefaultRoute:
    """One IPv4 default route: egress interface + gateway (``0.0.0.0`` =
    on-link)."""

    interface: str
    gateway: str


@dataclass(frozen=True, slots=True)
class NetworkSample:
    """One bounded network snapshot.

    ``None``/absent sources are genuinely unobserved — the collector never
    substitutes a zero (acceptance 2). The ``*_complete`` flags are the
    explicit per-source gaps: ``False`` means the source was absent/refused
    and the corresponding listing is UNKNOWN, while a complete source with
    zero entries is a trusted empty listing.
    """

    observed_at: datetime
    observed_at_source: Literal["target", "controller"]
    quality: ObservationQuality
    interfaces_complete: bool
    addresses_complete: bool
    tcp_complete: bool
    tcp6_complete: bool
    udp_complete: bool
    udp6_complete: bool
    routes_complete: bool
    interfaces: tuple[InterfaceObservation, ...] = ()
    addresses: tuple[InterfaceAddress, ...] = ()
    listeners: tuple[Listener, ...] = ()
    default_routes: tuple[DefaultRoute, ...] = ()


def _listener(
    proto: Literal["tcp", "tcp6", "udp", "udp6"], raw: str, limit: int
) -> Listener | None:
    address_hex, sep, port_hex = raw.rpartition(":")
    # Valid shapes: v4 "8-hex:4-hex" (13 chars) … v6 "32-hex:4-hex" (37).
    if not sep or len(port_hex) > 4 or not 6 <= len(raw) <= 37:
        return None
    try:
        port = int(port_hex, 16)
    except ValueError:
        return None
    if not 0 < port <= 65535:
        return None
    if proto in ("tcp", "udp"):
        address = _ipv4_hex(address_hex)
    else:
        address = _ipv6_hex(address_hex)
    if address is None:
        return None
    return Listener(proto=proto, address=address, port=port)


def parse_probe_output(output: str, *, controller_now: datetime) -> NetworkSample:
    """Parse fixed probe output into a :class:`NetworkSample` (pure).

    Order- and first-occurrence-sensitive like the P04 identity parser:
    recognized keys are captured on their first appearance, unknown keys are
    ignored, malformed values leave the field/skip the entry. Sample quality
    is the shared observation vocabulary applied to the four core sources
    (interfaces, IPv4 listener table, IPv4 address table, default routes):
    all present ⇒ ``measured``, some ⇒ ``degraded``, none ⇒ ``unknown``
    (the schema forbids any metric on an unknown row). The timestamp is the
    target's own ``date -u`` when parsable, otherwise the controller clock —
    a measured sample is always timestamped.
    """
    if controller_now.tzinfo is None:
        raise ValueError("controller_now must be timezone-aware")
    values: dict[str, str] = {}
    if_lines: list[str] = []
    addr_lines: list[str] = []
    listener_lines: dict[str, list[str]] = {
        "tcp_l": [],
        "tcp6_l": [],
        "udp_b": [],
        "udp6_b": [],
    }
    route_lines: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip("\r")
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key in listener_lines:
            listener_lines[key].append(value)
        elif key == "if":
            if_lines.append(value)
        elif key == "addr":
            addr_lines.append(value)
        elif key == "route":
            route_lines.append(value)
        elif key not in values:
            values[key] = value

    observed_at = controller_now
    observed_at_source: Literal["target", "controller"] = "controller"
    raw_ts = values.get("ts")
    if raw_ts:
        try:
            parsed = datetime.strptime(raw_ts, _TS_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            parsed = None
        if parsed is not None:
            observed_at = parsed
            observed_at_source = "target"

    interfaces_complete = values.get("if_src") == "ok"
    interfaces: list[InterfaceObservation] = []
    for raw in if_lines[:MAX_INTERFACES]:
        name, sep, state = raw.partition(":")
        if not sep or not _INTERFACE_NAME_RE.fullmatch(name):
            continue
        interfaces.append(
            InterfaceObservation(
                name=name, operstate=state if state in _OPERSTATES else None
            )
        )

    addresses_complete = values.get("addr_rc") == "0"
    addresses: list[InterfaceAddress] = []
    for raw in addr_lines[:MAX_ADDRESSES]:
        name, sep, cidr = raw.partition(":")
        if not sep or not _INTERFACE_NAME_RE.fullmatch(name):
            continue
        if not _CIDR_RE.fullmatch(cidr):
            continue
        try:
            ipaddress.IPv4Address(cidr.split("/", 1)[0])
        except ValueError:
            continue
        addresses.append(InterfaceAddress(name=name, cidr=cidr))

    tcp_complete = values.get("tcp_rc") == "0"
    tcp6_complete = values.get("tcp6_rc") == "0"
    udp_complete = values.get("udp_rc") == "0"
    udp6_complete = values.get("udp6_rc") == "0"
    listeners: list[Listener] = []
    for proto, key, limit in (
        ("tcp", "tcp_l", MAX_TCP_LISTENERS),
        ("tcp6", "tcp6_l", MAX_TCP_LISTENERS),
        ("udp", "udp_b", MAX_UDP_SOCKETS),
        ("udp6", "udp6_b", MAX_UDP_SOCKETS),
    ):
        for raw in listener_lines[key][:limit]:
            listener = _listener(proto, raw, limit)
            if listener is not None:
                listeners.append(listener)

    routes_complete = values.get("route_rc") == "0"
    default_routes: list[DefaultRoute] = []
    for raw in route_lines[:MAX_ROUTES]:
        interface, sep, gateway_hex = raw.partition(":")
        if not sep or not _INTERFACE_NAME_RE.fullmatch(interface):
            continue
        if not _HEX8_RE.fullmatch(gateway_hex):
            continue
        gateway = _ipv4_hex(gateway_hex)
        if gateway is None:
            continue
        default_routes.append(DefaultRoute(interface=interface, gateway=gateway))

    core_present = (
        interfaces_complete,
        tcp_complete,
        addresses_complete,
        routes_complete,
    )
    if all(core_present):
        quality = ObservationQuality.MEASURED
    elif any(core_present):
        quality = ObservationQuality.DEGRADED
    else:
        quality = ObservationQuality.UNKNOWN

    return NetworkSample(
        observed_at=observed_at,
        observed_at_source=observed_at_source,
        quality=quality,
        interfaces_complete=interfaces_complete,
        addresses_complete=addresses_complete,
        tcp_complete=tcp_complete,
        tcp6_complete=tcp6_complete,
        udp_complete=udp_complete,
        udp6_complete=udp6_complete,
        routes_complete=routes_complete,
        interfaces=tuple(interfaces),
        addresses=tuple(addresses),
        listeners=tuple(listeners),
        default_routes=tuple(default_routes),
    )


# ---------------------------------------------------------------------------
# Probe execution (P04 transport: frozen option surface, pinned host key)
# ---------------------------------------------------------------------------


def _sanitize_stderr(stderr: str) -> str:
    """Bounded single-line stderr tail for error details (value-free)."""
    flat = " ".join(stderr.split())
    return flat[-200:] if flat else "<no stderr>"


def _map_probe_failure(rc: int, stderr: str, endpoint: str) -> SshConnectError:
    """Map a non-zero ssh exit to the stable P04 rejection vocabulary.

    Local mirror of the measured-probe classification (the transport owns the
    canonical table): only the coarse class matters here — the collector maps
    any of these to the unknown path, the codes exist so evidence and logs
    name the same rejection reason the transport suite pins.
    """
    flat = " ".join(stderr.split())
    if (
        "Host key verification failed" in flat
        or "HOST IDENTIFICATION HAS CHANGED" in flat
    ):
        return SshConnectError(
            SshErrorCode.HOST_KEY_MISMATCH,
            f"{endpoint} refused by host key pinning (stderr: {_sanitize_stderr(stderr)})",
        )
    if "Permission denied" in flat or "bad permissions" in flat.lower():
        return SshConnectError(
            SshErrorCode.AUTH_FAILED,
            f"{endpoint} rejected the pinned identity key (stderr: {_sanitize_stderr(stderr)})",
        )
    if any(
        marker in flat
        for marker in (
            "Could not resolve hostname",
            "Connection refused",
            "Connection timed out",
            "Connection reset",
            "Network is unreachable",
        )
    ):
        return SshConnectError(
            SshErrorCode.UNREACHABLE,
            f"{endpoint} unreachable (stderr: {_sanitize_stderr(stderr)})",
        )
    return SshConnectError(
        SshErrorCode.SSH_FAILED,
        f"{endpoint} ssh exited rc={rc} (stderr: {_sanitize_stderr(stderr)})",
    )


def probe_network(
    identity: SshIdentity,
    *,
    key_path: Path,
    known_hosts_path: Path,
    controller_now: datetime,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> NetworkSample:
    """Run the fixed network probe over the pinned-identity transport.

    Preconditions mirror :func:`mai.execution.ssh.probe_environment`: the key
    path comes from ``resolve_identity_key`` and the managed known-hosts file
    from ``pin_host_key`` (enrollment). Raises :class:`SshConnectError`
    (stable codes) for any transport failure; a target that answers is
    parsed into a :class:`NetworkSample` under the hard output budget.
    """
    if not known_hosts_path.is_file():
        raise SshConnectError(
            SshErrorCode.INVALID_CONFIGURATION,
            f"managed known-hosts file {known_hosts_path} does not exist; "
            "enroll the pinned host key explicitly before collecting",
        )
    command = ssh_arguments(
        identity,
        key_path=key_path,
        known_hosts_path=known_hosts_path,
        remote_command=PROBE_COMMAND,
    )
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise SshConnectError(
            SshErrorCode.TIMEOUT, f"network probe timed out after {timeout}s"
        ) from None
    if len(proc.stdout.encode("utf-8")) > MAX_PROBE_OUTPUT_BYTES:
        raise SshConnectError(
            SshErrorCode.BOUNDED_OUTPUT_EXCEEDED,
            f"network probe output exceeded the {MAX_PROBE_OUTPUT_BYTES}-byte budget",
        )
    if proc.returncode != 0:
        raise _map_probe_failure(
            proc.returncode, proc.stderr, f"{identity.host}:{identity.port}"
        )
    return parse_probe_output(proc.stdout, controller_now=controller_now)


# ---------------------------------------------------------------------------
# Findings (collector-owned thresholds; dedup by (server_id, kind, dedup_key))
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FindingDraft:
    """One collector-owned finding evaluation for a sample.

    ``state`` is three-valued: ``active`` upserts the row, ``clear`` closes
    an open row, ``no_data`` leaves the row untouched (missing data is never
    evidence that a condition ended). Findings are deliberately
    health-flavored: classifying an exposed listener as a security risk is
    the separate 10-security card's decision over this data.
    """

    kind: str
    dedup_key: str
    severity: Literal["info", "warning", "critical"]
    summary: str
    state: Literal["active", "clear", "no_data"]


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def findings_for_sample(sample: NetworkSample) -> tuple[FindingDraft, ...]:
    """Evaluate the collector's threshold policy against one sample (pure)."""
    drafts: list[FindingDraft] = []

    if not sample.routes_complete:
        drafts.append(
            FindingDraft(
                kind="network.default_route",
                dedup_key="v4",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    else:
        active = not sample.default_routes
        first = sample.default_routes[0] if sample.default_routes else None
        drafts.append(
            FindingDraft(
                kind="network.default_route",
                dedup_key="v4",
                severity="warning" if active else "info",
                summary=_truncate(
                    f"{len(sample.default_routes)} default route(s)"
                    + (
                        ": none configured"
                        if active
                        else f" via {first.interface} (gateway {first.gateway})"
                    ),
                    SUMMARY_MAX_CHARS,
                ),
                state="active" if active else "clear",
            )
        )

    if not sample.interfaces_complete:
        drafts.append(
            FindingDraft(
                kind="network.interface_down",
                dedup_key="down",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    else:
        down = [
            iface.name
            for iface in sample.interfaces
            if iface.name != "lo" and iface.operstate == "down"
        ]
        listing = ", ".join(down[:3]) + (
            f" (+{len(down) - 3} more)" if len(down) > 3 else ""
        )
        drafts.append(
            FindingDraft(
                kind="network.interface_down",
                dedup_key="down",
                severity="warning" if down else "info",
                summary=_truncate(
                    f"{len(down)} non-loopback interface(s) down"
                    + (f": {listing}" if down else ""),
                    SUMMARY_MAX_CHARS,
                ),
                state="active" if down else "clear",
            )
        )

    return tuple(drafts)


# ---------------------------------------------------------------------------
# Persistence into the 0007 observation schema
# ---------------------------------------------------------------------------

_INSERT_OBSERVATION = sa.text(
    """
    INSERT INTO server_observations
        (observation_id, server_id, source, method, quality, metric, value,
         observed_at, collected_at, fresh_until, retain_until)
    VALUES
        (:observation_id, :server_id, :source, :method, :quality, :metric,
         :value, :observed_at, :collected_at, :fresh_until, :retain_until)
    """
)

_UPSERT_FINDING = sa.text(
    """
    INSERT INTO observation_findings
        (finding_id, server_id, kind, dedup_key, severity, summary, detail,
         observation_id, opened_at, updated_at, resolved_at, retain_until)
    VALUES
        (:finding_id, :server_id, :kind, :dedup_key, :severity, :summary,
         :detail, :observation_id, :collected_at, :collected_at, NULL,
         :retain_until)
    ON CONFLICT (server_id, kind, dedup_key) DO UPDATE SET
        severity = EXCLUDED.severity,
        summary = EXCLUDED.summary,
        detail = EXCLUDED.detail,
        observation_id = EXCLUDED.observation_id,
        updated_at = EXCLUDED.updated_at,
        resolved_at = NULL
    """
)

_CLOSE_FINDING = sa.text(
    """
    UPDATE observation_findings
       SET resolved_at = :resolved_at, updated_at = :resolved_at
     WHERE server_id = :server_id AND kind = :kind AND resolved_at IS NULL
    """
)


def _metric_rows(
    server_id: uuid.UUID,
    sample: NetworkSample,
    collected_at: datetime,
    fresh_until: datetime,
    retain_until: datetime,
) -> list[dict[str, object]]:
    """Bounded per-metric rows; the rendering stays a short normalized text."""

    def row(metric: str, value: str) -> dict[str, object]:
        return {
            "observation_id": uuid.uuid4(),
            "server_id": server_id,
            "source": SOURCE,
            "method": ObservationMethod.COMMAND.value,
            "quality": sample.quality.value,
            "metric": metric,
            "value": value,
            "observed_at": sample.observed_at,
            "collected_at": collected_at,
            "fresh_until": fresh_until,
            "retain_until": retain_until,
        }

    rows: list[dict[str, object]] = []
    if sample.interfaces_complete:
        for iface in sample.interfaces:
            # An unreadable operstate persists as the kernel's own "unknown"
            # state word — an explicit gap, never a fabricated down/up.
            rows.append(row(f"net.if.{iface.name}", iface.operstate or "unknown"))
    if sample.addresses_complete:
        for address in sample.addresses:
            rows.append(row(f"net.addr.{address.name}", address.cidr))
    for listener in sample.listeners:
        rows.append(
            row(
                f"net.listen.{listener.proto}",
                f"{listener.address}:{listener.port}",
            )
        )
    if sample.routes_complete:
        for route in sample.default_routes:
            rows.append(
                row("net.route.default", f"{route.interface} via {route.gateway}")
            )
    if sample.interfaces_complete:
        rows.append(row("net.iface_count", str(len(sample.interfaces))))
    if (
        sample.tcp_complete
        and sample.tcp6_complete
        and sample.udp_complete
        and sample.udp6_complete
    ):
        rows.append(row("net.listener_count", str(len(sample.listeners))))
    return rows


async def record_sample(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    sample: NetworkSample,
    *,
    controller_now: datetime,
) -> int:
    """Persist one sample as bounded observation rows + threshold findings.

    One row per observed metric (source/method/quality/timestamps per C10
    rule 4), then the collector's finding set: active drafts upsert onto
    ``(server_id, kind, dedup_key)``, cleared drafts close open rows,
    ``no_data`` drafts do nothing. An unknown-quality sample parses to zero
    metric rows (the schema forbids a measurement on an unknown row).
    Returns the number of observation rows written (findings excluded).
    """
    if controller_now.tzinfo is None:
        raise ValueError("controller_now must be timezone-aware")
    fresh_until = sample.observed_at + FRESH_HORIZON
    retain_until = controller_now + RETENTION_HORIZON
    rows = _metric_rows(server_id, sample, controller_now, fresh_until, retain_until)
    # Active findings anchor to a REAL inserted sample row (FK to
    # server_observations); with no metric rows no finding can be active.
    observation_id = rows[0]["observation_id"] if rows else uuid.uuid4()
    async with sessions() as session:
        for params in rows:
            await session.execute(_INSERT_OBSERVATION, params)
        for draft in findings_for_sample(sample):
            if draft.state == "active":
                await session.execute(
                    _UPSERT_FINDING,
                    {
                        "finding_id": uuid.uuid4(),
                        "server_id": server_id,
                        "kind": draft.kind,
                        "dedup_key": draft.dedup_key,
                        "severity": draft.severity,
                        "summary": draft.summary,
                        "detail": None,  # bounded text column stays unused here
                        "observation_id": observation_id,
                        "collected_at": controller_now,
                        "retain_until": retain_until,
                    },
                )
            elif draft.state == "clear":
                await session.execute(
                    _CLOSE_FINDING,
                    {
                        "resolved_at": controller_now,
                        "server_id": server_id,
                        "kind": draft.kind,
                    },
                )
        await session.commit()
    return len(rows)


async def record_unavailable(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    *,
    controller_now: datetime,
) -> int:
    """Persist the unknown path for a permission-refused/unreachable target.

    Exactly ONE ``quality='unknown'`` row with NO metric/value/observed_at
    (the schema's ``ck_server_observations_unknown_has_no_measurement``
    forbids anything else) — never a zero reading. Existing findings are
    deliberately left untouched: missing data neither re-evaluates nor
    closes a condition. Returns 1.
    """
    if controller_now.tzinfo is None:
        raise ValueError("controller_now must be timezone-aware")
    async with sessions() as session:
        await session.execute(
            _INSERT_OBSERVATION,
            {
                "observation_id": uuid.uuid4(),
                "server_id": server_id,
                "source": SOURCE,
                "method": ObservationMethod.COMMAND.value,
                "quality": ObservationQuality.UNKNOWN.value,
                "metric": None,
                "value": None,
                "observed_at": None,
                "collected_at": controller_now,
                "fresh_until": None,
                "retain_until": controller_now + RETENTION_HORIZON,
            },
        )
        await session.commit()
    return 1


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Outcome of one bounded collection attempt.

    ``quality`` is the sample's quality, or ``unknown`` when the target was
    unavailable — the caller-facing shape of acceptance 2 (a gap is a gap,
    never zero data). ``error`` carries only the stable rejection code.
    """

    quality: ObservationQuality
    observations: int
    server_id: uuid.UUID
    error: str | None = None


async def collect(
    sessions: async_sessionmaker[AsyncSession],
    identity: SshIdentity,
    *,
    key_path: Path,
    known_hosts_path: Path,
    server_id: uuid.UUID,
    controller_now: datetime,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> CollectionResult:
    """Probe a target and persist the sample, or persist the unknown path.

    Never raises for target-side failures (any :class:`SshConnectError`
    becomes a persisted unknown observation); controller misconfiguration
    (unresolvable key reference, missing enrollment) still raises — failing
    closed is the configuration boundary's job, not a collection sample.
    """
    try:
        sample = probe_network(
            identity,
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            controller_now=controller_now,
            timeout=timeout,
        )
    except SshConnectError as error:
        written = await record_unavailable(
            sessions, server_id, controller_now=controller_now
        )
        return CollectionResult(
            quality=ObservationQuality.UNKNOWN,
            observations=written,
            server_id=server_id,
            error=error.code.value,
        )
    written = await record_sample(
        sessions, server_id, sample, controller_now=controller_now
    )
    return CollectionResult(
        quality=sample.quality, observations=written, server_id=server_id
    )
