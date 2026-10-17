"""Bounded system health observations over SSH (card 10-system, C04 + C10).

Frozen-shape producer for the deterministic ``system`` collector: one fixed,
read-only, self-bounding probe command gathers a CPU/memory/disk snapshot from
a pinned-identity SSH target (the ``04-ssh-connect`` transport: exactly the
frozen probe option surface, host-key pinning, no credential in argv) and the
results persist as bounded rows of the ``0007_observations`` schema:

- **C10 rule 4** — every sample reports timestamp/method/quality. One row per
  metric: ``source='system'``, ``method='command'``, ``quality`` per
  :class:`mai.contracts.observations.ObservationQuality`, ``observed_at``
  (target-reported time when the target supplies one, controller time
  otherwise), ``fresh_until`` = collector-declared freshness horizon, and
  ``retain_until`` = retention bound (the sweep itself is the scheduled
  collectors card's ownership).
- **Acceptance 1** — an actual snapshot carries source + timestamp and only
  bounded target command output: the probe script bounds its own emission
  (``head`` on the systemd listing) and the controller hard-caps the captured
  output (:data:`MAX_PROBE_OUTPUT_BYTES`), so no unbounded transcript can
  reach storage — every persisted value is a short normalized rendering.
- **Acceptance 2 ("stale/unknown, not zero usage")** — an unavailable or
  permission-refused target is recorded as ONE ``quality='unknown'``
  observation row with NO metric/value/observed_at (the schema's
  ``ck_server_observations_unknown_has_no_measurement`` structurally forbids a
  zero reading) and consumers derive staleness via the shared
  :func:`mai.contracts.observations.derive_freshness`. Missing sources inside
  an otherwise readable target degrade the sample (``degraded``) and the
  affected metrics are simply absent — never fabricated zeros. Absent
  conditions are only ever CLOSED from a real measurement; an unknown sample
  never resolves or re-evaluates an existing finding.
- **Process details are a separate module's ownership** (10-process): this
  collector's probe command contains NO process listing; its only process
  touchpoint is the load-average runnable counters, which carry no argv.
  That is the structural half of the "no secret sentinel from process args"
  scenario — there is no command path that could read one.

Threshold policy is collector-owned (finding ``kind`` values are collector
references, not a frozen vocabulary — 10-observation-schema): four
``system.*`` kinds with deterministic dedup keys, upserted with
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
    "DISK_CRIT_USED_PCT",
    "DISK_WARN_USED_PCT",
    "FRESH_HORIZON",
    "LOAD_WARN_RATIO",
    "MAX_FAILED_UNITS_LISTED",
    "MAX_PROBE_OUTPUT_BYTES",
    "MEM_CRIT_AVAIL_PCT",
    "MEM_WARN_AVAIL_PCT",
    "PROBE_TIMEOUT_SECONDS",
    "RETENTION_HORIZON",
    "SOURCE",
    "CollectionResult",
    "FindingDraft",
    "PROBE_COMMAND",
    "SystemSample",
    "collect",
    "findings_for_sample",
    "parse_probe_output",
    "probe_system",
    "record_sample",
    "record_unavailable",
]

#: Collector identity persisted on every row of this collector.
SOURCE = "system"

#: Wall-clock budget for one probe round trip (connect timeout is the frozen
#: P04 :data:`CONNECT_TIMEOUT_SECONDS`).
PROBE_TIMEOUT_SECONDS = 30.0

#: Hard cap on captured probe output. The probe emits at most a few hundred
#: bytes by construction; anything larger is a hostile/broken target.
MAX_PROBE_OUTPUT_BYTES = 32 * 1024

#: Collector-declared freshness horizon (C10 rule 4: consumers derive
#: staleness against their own clock via ``derive_freshness``).
FRESH_HORIZON = timedelta(minutes=15)

#: Retention bound stamped on every row this collector writes.
RETENTION_HORIZON = timedelta(days=7)

#: The single remote command this module executes: fixed, read-only,
#: self-bounding, POSIX-sh (busybox targets parse the same). Every line is
#: failure-tolerant so one absent source degrades rather than aborts the
#: sample; ``set --`` over the one ``df -kP /`` line keeps the disk fields
#: positional without a second parse on the controller. No process listing:
#: process details belong to the separate 10-process module.
PROBE_COMMAND = (
    "printf 'ts=%s\\n' \"$(date -u '+%Y-%m-%dT%H:%M:%S' 2>/dev/null)\"; "
    "printf 'load1=%s\\n' \"$(cut -d ' ' -f1 /proc/loadavg 2>/dev/null)\"; "
    "printf 'load5=%s\\n' \"$(cut -d ' ' -f2 /proc/loadavg 2>/dev/null)\"; "
    "printf 'load15=%s\\n' \"$(cut -d ' ' -f3 /proc/loadavg 2>/dev/null)\"; "
    "printf 'cores=%s\\n' \"$(nproc 2>/dev/null)\"; "
    "printf 'mem_total_kib=%s\\n' \"$(sed -n "
    "'s/^MemTotal: *\\([0-9][0-9]*\\) kB$/\\1/p' /proc/meminfo 2>/dev/null)\"; "
    "printf 'mem_available_kib=%s\\n' \"$(sed -n "
    "'s/^MemAvailable: *\\([0-9][0-9]*\\) kB$/\\1/p' /proc/meminfo 2>/dev/null)\"; "
    "set -- $(df -kP / 2>/dev/null | sed -n '2p'); "
    "printf 'disk_total_kib=%s\\n' \"$2\"; "
    "printf 'disk_avail_kib=%s\\n' \"$4\"; "
    "printf 'disk_used_pct=%s\\n' \"$5\"; "
    "printf 'uptime_s=%s\\n' \"$(cut -d ' ' -f1 /proc/uptime 2>/dev/null)\"; "
    "if command -v systemctl >/dev/null 2>&1; then "
    "printf 'systemd=yes\\n'; "
    "units=$(systemctl list-units --failed --no-legend --no-pager 2>/dev/null); "
    "printf 'systemd_failed_rc=%s\\n' \"$?\"; "
    "printf '%s\\n' \"$units\" | head -n 20; "
    "else printf 'systemd=no\\n'; fi"
)

#: Systemd failed-unit listing bound (enforced target-side by ``head``).
MAX_FAILED_UNITS_LISTED = 20

# Threshold policy (collector-owned; contract C10 leaves finding kinds to the
# collector cards). All are open on equal-or-worse readings.
DISK_WARN_USED_PCT = 90.0
DISK_CRIT_USED_PCT = 95.0
MEM_WARN_AVAIL_PCT = 10.0
MEM_CRIT_AVAIL_PCT = 5.0
LOAD_WARN_RATIO = 1.0

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


# ---------------------------------------------------------------------------
# Sample shape (pure; what one probe round trip observed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiskSample:
    """Root-filesystem usage exactly as the target reported it (KiB)."""

    total_kib: int
    available_kib: int
    used_pct: float


@dataclass(frozen=True, slots=True)
class SystemSample:
    """One bounded system snapshot.

    ``None`` fields are genuinely unobserved sources — the collector never
    substitutes a zero (acceptance 2). ``systemd_failed_units`` is
    three-valued by design: ``None`` = unknown (no systemd or a failed
    query), ``()`` = systemd answered and reported zero failed units.
    ``observed_at_source`` records which clock stamped ``observed_at``.
    """

    observed_at: datetime
    observed_at_source: Literal["target", "controller"]
    quality: ObservationQuality
    load1: float | None = None
    load5: float | None = None
    load15: float | None = None
    cores: int | None = None
    mem_total_kib: int | None = None
    mem_available_kib: int | None = None
    disk_root: DiskSample | None = None
    uptime_s: float | None = None
    systemd_present: bool | None = None
    systemd_failed_units: tuple[str, ...] | None = None


def _int_field(raw: str | None) -> int | None:
    if raw is None or not raw.isdigit():
        return None
    return int(raw)


def _float_field(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return (
        value if value == value and value not in (float("inf"), float("-inf")) else None
    )


def _pct_field(raw: str | None) -> float | None:
    if raw is None or not raw.endswith("%"):
        return None
    value = _float_field(raw[:-1])
    if value is None or not 0.0 <= value <= 100.0:
        return None
    return value


def _unit_name(raw: str) -> str | None:
    name = raw.strip()
    if not name or len(name) > 64:
        return None
    if not all(ch.isalnum() or ch in "@._-" for ch in name):
        return None
    return name


def parse_probe_output(output: str, *, controller_now: datetime) -> SystemSample:
    """Parse fixed probe output into a :class:`SystemSample` (pure).

    Order- and first-occurrence-sensitive like the P04 identity parser:
    recognized keys are captured on their first appearance, unknown keys are
    ignored, malformed values leave the field ``None``. Sample quality is the
    shared observation vocabulary applied to the three core sources (load,
    memory, disk): all present ⇒ ``measured``, some ⇒ ``degraded``,
    none ⇒ ``unknown`` (the schema forbids any metric on an unknown row).
    The timestamp is the target's own ``date -u`` when parsable, otherwise
    the controller clock — a measured sample is always timestamped.
    """
    if controller_now.tzinfo is None:
        raise ValueError("controller_now must be timezone-aware")
    values: dict[str, str] = {}
    unit_lines: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip("\r")
        if not line or "=" not in line:
            if line.startswith("failed_unit="):
                unit_lines.append(line.removeprefix("failed_unit="))
            continue
        key, _, value = line.partition("=")
        if key == "failed_unit":
            unit_lines.append(value)
            continue
        if key not in values:
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

    systemd_present: bool | None
    systemd_failed: tuple[str, ...] | None
    marker = values.get("systemd")
    if marker == "yes":
        systemd_present = True
        if values.get("systemd_failed_rc") == "0":
            units = []
            for raw in unit_lines[:MAX_FAILED_UNITS_LISTED]:
                name = _unit_name(raw)
                if name is not None:
                    units.append(name)
            systemd_failed = tuple(units)
        else:
            # A failed/bounded query is NOT zero failed units.
            systemd_failed = None
    elif marker == "no":
        systemd_present = False
        systemd_failed = None  # absent source is unknown, never zero
    else:
        systemd_present = None
        systemd_failed = None

    load1 = _float_field(values.get("load1"))
    mem_total = _int_field(values.get("mem_total_kib"))
    disk_total = _int_field(values.get("disk_total_kib"))
    disk_avail = _int_field(values.get("disk_avail_kib"))
    disk_pct = _pct_field(values.get("disk_used_pct"))
    disk = (
        DiskSample(total_kib=disk_total, available_kib=disk_avail, used_pct=disk_pct)
        if disk_total is not None and disk_avail is not None and disk_pct is not None
        else None
    )

    core_present = (load1 is not None, mem_total is not None, disk is not None)
    if all(core_present):
        quality = ObservationQuality.MEASURED
    elif any(core_present):
        quality = ObservationQuality.DEGRADED
    else:
        quality = ObservationQuality.UNKNOWN

    return SystemSample(
        observed_at=observed_at,
        observed_at_source=observed_at_source,
        quality=quality,
        load1=load1,
        load5=_float_field(values.get("load5")),
        load15=_float_field(values.get("load15")),
        cores=_int_field(values.get("cores")),
        mem_total_kib=mem_total,
        mem_available_kib=_int_field(values.get("mem_available_kib")),
        disk_root=disk,
        uptime_s=_float_field(values.get("uptime_s")),
        systemd_present=systemd_present,
        systemd_failed_units=systemd_failed,
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


def probe_system(
    identity: SshIdentity,
    *,
    key_path: Path,
    known_hosts_path: Path,
    controller_now: datetime,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> SystemSample:
    """Run the fixed system probe over the pinned-identity transport.

    Preconditions mirror :func:`mai.execution.ssh.probe_environment`: the key
    path comes from ``resolve_identity_key`` and the managed known-hosts file
    from ``pin_host_key`` (enrollment). Raises :class:`SshConnectError`
    (stable codes) for any transport failure; a target that answers is
    parsed into a :class:`SystemSample` under the hard output budget.
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
            SshErrorCode.TIMEOUT, f"system probe timed out after {timeout}s"
        ) from None
    if len(proc.stdout.encode("utf-8")) > MAX_PROBE_OUTPUT_BYTES:
        raise SshConnectError(
            SshErrorCode.BOUNDED_OUTPUT_EXCEEDED,
            f"system probe output exceeded the {MAX_PROBE_OUTPUT_BYTES}-byte budget",
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
    evidence that a condition ended).
    """

    kind: str
    dedup_key: str
    severity: Literal["info", "warning", "critical"]
    summary: str
    state: Literal["active", "clear", "no_data"]


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def findings_for_sample(sample: SystemSample) -> tuple[FindingDraft, ...]:
    """Evaluate the collector's threshold policy against one sample (pure)."""
    drafts: list[FindingDraft] = []

    disk = sample.disk_root
    if disk is None:
        drafts.append(
            FindingDraft(
                kind="system.disk_pressure",
                dedup_key="root",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    else:
        severity: Literal["info", "warning", "critical"] = "info"
        if disk.used_pct >= DISK_CRIT_USED_PCT:
            severity = "critical"
        elif disk.used_pct >= DISK_WARN_USED_PCT:
            severity = "warning"
        drafts.append(
            FindingDraft(
                kind="system.disk_pressure",
                dedup_key="root",
                severity=severity,
                summary=_truncate(
                    f"root filesystem {disk.used_pct:.1f}% used "
                    f"({disk.available_kib} KiB free)",
                    SUMMARY_MAX_CHARS,
                ),
                state="active" if severity != "info" else "clear",
            )
        )

    if sample.mem_total_kib is None or sample.mem_available_kib is None:
        drafts.append(
            FindingDraft(
                kind="system.memory_pressure",
                dedup_key="available",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    else:
        avail_pct = sample.mem_available_kib / sample.mem_total_kib * 100.0
        severity = "info"
        if avail_pct <= MEM_CRIT_AVAIL_PCT:
            severity = "critical"
        elif avail_pct <= MEM_WARN_AVAIL_PCT:
            severity = "warning"
        drafts.append(
            FindingDraft(
                kind="system.memory_pressure",
                dedup_key="available",
                severity=severity,
                summary=_truncate(
                    f"{avail_pct:.1f}% memory available "
                    f"({sample.mem_available_kib}/{sample.mem_total_kib} KiB)",
                    SUMMARY_MAX_CHARS,
                ),
                state="active" if severity != "info" else "clear",
            )
        )

    if sample.load1 is None or sample.cores is None:
        drafts.append(
            FindingDraft(
                kind="system.load_pressure",
                dedup_key="load1",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    else:
        active = sample.load1 >= sample.cores * LOAD_WARN_RATIO
        drafts.append(
            FindingDraft(
                kind="system.load_pressure",
                dedup_key="load1",
                severity="warning" if active else "info",
                summary=_truncate(
                    f"load1 {sample.load1:.2f} on {sample.cores} cores",
                    SUMMARY_MAX_CHARS,
                ),
                state="active" if active else "clear",
            )
        )

    units = sample.systemd_failed_units
    if units is None:
        drafts.append(
            FindingDraft(
                kind="system.failed_units",
                dedup_key="failed",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    else:
        active = bool(units)
        listing = ", ".join(units[:3]) + ("…" if len(units) > 3 else "")
        drafts.append(
            FindingDraft(
                kind="system.failed_units",
                dedup_key="failed",
                severity="warning" if active else "info",
                summary=_truncate(
                    f"{len(units)} failed systemd units"
                    + (f": {listing}" if active else ""),
                    SUMMARY_MAX_CHARS,
                ),
                state="active" if active else "clear",
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
    sample: SystemSample,
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
    if sample.load1 is not None:
        rows.append(row("cpu.load1", f"{sample.load1:.2f}"))
    if sample.load5 is not None:
        rows.append(row("cpu.load5", f"{sample.load5:.2f}"))
    if sample.load15 is not None:
        rows.append(row("cpu.load15", f"{sample.load15:.2f}"))
    if sample.cores is not None:
        rows.append(row("cpu.cores", str(sample.cores)))
    if sample.mem_total_kib is not None:
        rows.append(row("mem.total_kib", str(sample.mem_total_kib)))
    if sample.mem_available_kib is not None:
        rows.append(row("mem.available_kib", str(sample.mem_available_kib)))
    if sample.mem_total_kib is not None and sample.mem_available_kib is not None:
        pct = sample.mem_available_kib / sample.mem_total_kib * 100.0
        rows.append(row("mem.available_pct", f"{pct:.1f}"))
    if sample.disk_root is not None:
        rows.append(row("disk.root.used_pct", f"{sample.disk_root.used_pct:.1f}"))
        rows.append(row("disk.root.total_kib", str(sample.disk_root.total_kib)))
        rows.append(row("disk.root.available_kib", str(sample.disk_root.available_kib)))
    if sample.uptime_s is not None:
        rows.append(row("system.uptime_s", f"{sample.uptime_s:.0f}"))
    if sample.systemd_failed_units is not None:
        rows.append(row("system.failed_units", str(len(sample.systemd_failed_units))))
    return rows


async def record_sample(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    sample: SystemSample,
    *,
    controller_now: datetime,
) -> int:
    """Persist one sample as bounded observation rows + threshold findings.

    One row per observed metric (source/method/quality/timestamps per C10
    rule 4), then the collector's finding set: active drafts upsert onto
    ``(server_id, kind, dedup_key)``, cleared drafts close open rows,
    ``no_data`` drafts do nothing. Returns the number of observation rows
    written (findings excluded).
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
    unavailable — the caller-facing shape of acceptance 2 ("stale/unknown,
    not zero usage"). ``error`` carries only the stable rejection code.
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
        sample = probe_system(
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
