"""Bounded process identity/resource observations over SSH (card 10-process).

Frozen-shape producer for the deterministic ``process`` collector: one fixed,
read-only, self-bounding probe command gathers a bounded process snapshot
(identity, state, CPU ticks, resident memory, zombie/permission-gap counts,
systemd service inventory) from a pinned-identity SSH target (the
``04-ssh-connect`` transport: exactly the frozen probe option surface,
host-key pinning, no credential in argv) and the results persist as bounded
rows of the ``0007_observations`` schema:

- **Acceptance 1 (bounded identity/resource snapshot, args excluded)** — the
  probe reads ONLY ``/proc/<pid>/stat`` and ``/proc/<pid>/comm``. It never
  reads ``/proc/<pid>/cmdline`` or ``/proc/<pid>/environ``: command-line
  arguments and environment variables are excluded STRUCTURALLY, not
  filtered after capture — there is no code path that could carry a secret
  argv sentinel into the sample, the storage dump or an evidence file.
  Identity is ``(pid, comm, state, ppid, start_ticks)``; resources are
  cumulative CPU ticks and resident memory. The listing is bounded
  target-side (``sort -rn | head`` on the top-:data:`MAX_PROCESSES_LISTED`
  CPU consumers) and the controller hard-caps captured output
  (:data:`MAX_PROBE_OUTPUT_BYTES`), so no unbounded transcript can reach
  storage. ``comm`` is the kernel-truncated executable name (≤ 15 chars),
  additionally validated controller-side.
- **Acceptance 2 (permission gaps / stale data explicit; no kill
  capability)** — a target whose ``/proc`` entries are not all readable
  reports the gap explicitly (``unreadable`` count, ``quality='degraded'``,
  an active ``process.read_gaps`` finding); an unavailable or
  permission-refused target records ONE ``quality='unknown'`` observation
  with NO metric/value/observed_at (the schema's
  ``ck_server_observations_unknown_has_no_measurement`` structurally forbids
  a zero reading) and consumers derive staleness via the shared
  :func:`mai.contracts.observations.derive_freshness`. Missing data never
  closes a finding (``no_data`` drafts do nothing) and an unknown sample
  never re-evaluates an existing condition. Process monitoring confers NO
  mutation capability: the module exposes only the fixed read-only probe —
  no kill/signal/renice/stop function exists, the probe command contains no
  mutation primitive (negative control: token audit), and collection leaves
  every observed process running (negative control: fixture processes are
  still alive after collection).

Threshold policy is collector-owned (finding ``kind`` values are collector
references, not a frozen vocabulary — 10-observation-schema): three
``process.*`` kinds (zombie backlog, permission gaps, resident-memory hog)
with deterministic dedup keys, upserted with
``ON CONFLICT (server_id, kind, dedup_key)`` so repeated polls collapse onto
one finding per condition. Hog detection covers the bounded listing (the
top CPU consumers with their RSS); a CPU-*rate* hog would need delta
sampling across two probes, which is out of this card's single-sample
budget — the ticks-sorted listing IS the identity/load evidence.

The probe runs synchronously (same shape as the P04 ``probe_environment``
and the sibling ``system``/``network`` collectors: system ``ssh`` client via
:func:`subprocess.run`), persistence is async on the single application
engine (:func:`mai.database.create_app_engine`). A collection attempt never
raises for target unavailability — it returns the unknown path — but
controller misconfiguration (unresolvable key reference, missing
enrollment) still raises, failing closed.
"""

from __future__ import annotations

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
    "MAX_PROBE_OUTPUT_BYTES",
    "MAX_PROCESSES_LISTED",
    "MAX_SERVICES_LISTED",
    "MEM_HOG_CRIT_MIB",
    "MEM_HOG_WARN_MIB",
    "PROBE_COMMAND",
    "PROBE_TIMEOUT_SECONDS",
    "RETENTION_HORIZON",
    "SOURCE",
    "CollectionResult",
    "FindingDraft",
    "ProcessEntry",
    "ProcessSample",
    "ServiceEntry",
    "collect",
    "findings_for_sample",
    "parse_probe_output",
    "probe_processes",
    "record_sample",
    "record_unavailable",
]

#: Collector identity persisted on every row of this collector.
SOURCE = "process"

#: Wall-clock budget for one probe round trip (connect timeout is the frozen
#: P04 :data:`CONNECT_TIMEOUT_SECONDS`).
PROBE_TIMEOUT_SECONDS = 30.0

#: Hard cap on captured probe output. The probe emits at most ~50 short
#: lines by construction (16 process + 32 service + summary); anything
#: larger is a hostile/broken target.
MAX_PROBE_OUTPUT_BYTES = 32 * 1024

#: Collector-declared freshness horizon (C10 rule 4: consumers derive
#: staleness against their own clock via ``derive_freshness``).
FRESH_HORIZON = timedelta(minutes=15)

#: Retention bound stamped on every row this collector writes.
RETENTION_HORIZON = timedelta(days=7)

#: Bounded per-sample process listing (target-side ``head`` bound; the
#: controller re-applies it defensively).
MAX_PROCESSES_LISTED = 16

#: Bounded systemd service listing (target-side ``head``-equivalent awk
#: bound; full counts stay honest because the target counts BEFORE trimming).
MAX_SERVICES_LISTED = 32

#: Resident-memory page size assumption for KiB conversion (4 KiB pages on
#: the supported x86_64/AArch64 fleet; rss is consumed from
#: ``/proc/<pid>/stat`` field 24 in pages).
RSS_KIB_PER_PAGE = 4

# Hog thresholds (collector-owned policy; equal-or-worse readings open).
MEM_HOG_WARN_MIB = 1024.0
MEM_HOG_CRIT_MIB = 4096.0

#: The single remote command this module executes: fixed, read-only,
#: self-bounding, POSIX-sh (busybox targets parse the same). Reads ONLY
#: ``/proc/<pid>/stat`` and ``/proc/<pid>/comm`` — never ``cmdline`` or
#: ``environ`` (acceptance 1: args excluded structurally) — and contains no
#: process-mutation primitive (acceptance 2). ``${sl##*) }`` strips the
#: parenthesized comm field so later ``stat`` fields stay positional even
#: when a comm itself contains spaces or parentheses; per-process lines are
#: emitted with the leading CPU-tick count as a bare numeric key so
#: ``sort -rn`` ranks them numerically across digit lengths, trimmed
#: target-side (``head``) and only THEN prefixed with ``pp=`` for the
#: controller's key=value framing. Zombies are counted but not listed (they
#: consume no CPU). The systemd service inventory is three-valued like the
#: system collector's failed-units query: rc≠0 ⇒ counts UNKNOWN, never zero.
PROBE_COMMAND = (
    "printf 'ts=%s\\n' \"$(date -u '+%Y-%m-%dT%H:%M:%S' 2>/dev/null)\"; "
    "total=0; zombies=0; unreadable=0; "
    "for d in /proc/[0-9]*; do "
    "total=$((total+1)); "
    'if read -r sl < "$d/stat" 2>/dev/null; then '
    "st=${sl##*) }; st=${st%% *}; "
    '[ "$st" = "Z" ] && zombies=$((zombies+1)); '
    "else unreadable=$((unreadable+1)); fi; done; "
    "printf 'procs=%s\\n' \"$total\"; "
    "printf 'zombies=%s\\n' \"$zombies\"; "
    "printf 'unreadable=%s\\n' \"$unreadable\"; "
    "for d in /proc/[0-9]*; do "
    'read -r sl < "$d/stat" 2>/dev/null || continue; '
    "rest=${sl##*) }; set -- $rest; "
    "[ $# -ge 22 ] || continue; "
    'case "${12}${13}" in *[!0-9]*) continue ;; esac; '
    'read -r comm < "$d/comm" 2>/dev/null || comm=; '
    "printf '%s %s %s %s %s %s %s\\n' "
    '"$(( ${12} + ${13} ))" "${22}" "${1}" "${2}" "${20}" '
    '"${d##*/}" "$comm"; '
    "done 2>/dev/null | sort -rn | head -n 16 | sed 's/^/pp=/'; "
    "if command -v systemctl >/dev/null 2>&1; then "
    "printf 'systemd=yes\\n'; "
    "svcs=$(systemctl list-units --type=service --no-legend --no-pager "
    "2>/dev/null); rc=$?; "
    "printf 'svc_rc=%s\\n' \"$rc\"; "
    'if [ "$rc" = "0" ]; then printf \'%s\\n\' "$svcs" | '
    'awk \'NF>=4 {n++; c[$4]++; if (n<=32) print "svc=" $4 " " $1} '
    'END {print "svc_count=" n+0; print "svc_running=" c["running"]+0; '
    'print "svc_failed=" c["failed"]+0; print "svc_exited=" '
    'c["exited"]+0}\'; fi; '
    "else printf 'systemd=no\\n'; fi"
)

#: Listing bounds enforced by the probe itself (head/awk); mirrored here.
_MAX_LISTED_BOUND_TARGET = MAX_PROCESSES_LISTED
_MAX_SERVICES_BOUND_TARGET = MAX_SERVICES_LISTED

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

_COMM_RE = re.compile(r"^[\x20-\x7e]{1,15}$")
_STATE_RE = re.compile(r"^[A-Za-z]$")
_UNIT_RE = re.compile(r"^[A-Za-z0-9@._-]{1,64}$")
_SUB_RE = re.compile(r"^[a-z][a-z-]{0,31}$")


# ---------------------------------------------------------------------------
# Sample shape (pure; what one probe round trip observed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProcessEntry:
    """One listed process: identity + resources, NEVER command arguments.

    ``comm`` is the kernel-truncated executable name; ``cpu_ticks`` is the
    cumulative utime+stime from ``/proc/<pid>/stat``; ``rss_kib`` converts
    the page count at :data:`RSS_KIB_PER_PAGE`. ``start_ticks`` is field 22
    (boot-relative start time), the same identity anchor the C04 receipt
    contract uses.
    """

    pid: int
    comm: str | None
    state: str
    ppid: int
    cpu_ticks: int
    rss_kib: int
    start_ticks: int


@dataclass(frozen=True, slots=True)
class ServiceEntry:
    """One systemd service unit as ``list-units`` reported it."""

    unit: str
    sub: str


@dataclass(frozen=True, slots=True)
class ProcessSample:
    """One bounded process snapshot.

    ``None`` fields are genuinely unobserved sources — the collector never
    substitutes a zero (acceptance 2). ``services`` is three-valued by
    design: ``None`` = unknown (no systemd, or the query failed),
    ``()`` = systemd answered and reported zero service units. An empty
    ``processes`` listing with ``total_procs`` present is a real (weird but
    honest) observation of an unreadable/empty listing, not a fabricated
    zero.
    """

    observed_at: datetime
    observed_at_source: Literal["target", "controller"]
    quality: ObservationQuality
    total_procs: int | None = None
    zombies: int | None = None
    unreadable: int | None = None
    processes: tuple[ProcessEntry, ...] = ()
    systemd_present: bool | None = None
    services: tuple[ServiceEntry, ...] | None = None
    svc_total: int | None = None
    svc_running: int | None = None
    svc_failed: int | None = None
    svc_exited: int | None = None


def _int_field(raw: str | None) -> int | None:
    if raw is None or not raw.isdigit():
        return None
    return int(raw)


def _comm_field(raw: str) -> str | None:
    comm = raw.strip()
    if _COMM_RE.fullmatch(comm) is None:
        return None
    return comm


def _state_field(raw: str) -> str | None:
    state = raw.strip()
    if _STATE_RE.fullmatch(state) is None:
        return None
    return state


def _unit_field(raw: str) -> str | None:
    unit = raw.strip()
    if _UNIT_RE.fullmatch(unit) is None:
        return None
    return unit


def _sub_field(raw: str) -> str | None:
    sub = raw.strip()
    if _SUB_RE.fullmatch(sub) is None:
        return None
    return sub


def _parse_process_line(raw: str) -> ProcessEntry | None:
    """Parse one ``pp=`` listing line; malformed input drops the entry."""
    fields = raw.split(maxsplit=6)
    if len(fields) < 6:
        return None
    ticks = _int_field(fields[0])
    rss_pages = _int_field(fields[1])
    state = _state_field(fields[2])
    ppid = _int_field(fields[3])
    start = _int_field(fields[4])
    pid = _int_field(fields[5])
    comm = _comm_field(fields[6]) if len(fields) > 6 else None
    if (
        ticks is None
        or rss_pages is None
        or state is None
        or ppid is None
        or start is None
        or pid is None
        or pid <= 0
        or pid > 2**31
    ):
        return None
    return ProcessEntry(
        pid=pid,
        comm=comm,
        state=state,
        ppid=ppid,
        cpu_ticks=ticks,
        rss_kib=rss_pages * RSS_KIB_PER_PAGE,
        start_ticks=start,
    )


def parse_probe_output(output: str, *, controller_now: datetime) -> ProcessSample:
    """Parse fixed probe output into a :class:`ProcessSample` (pure).

    Order- and first-occurrence-sensitive like the sibling parsers:
    recognized scalar keys are captured on their first appearance, unknown
    keys are ignored, malformed values leave the field ``None``. The
    process listing keeps the target's CPU-tick rank, is capped at
    :data:`MAX_PROCESSES_LISTED` and drops malformed lines (a hostile
    target cannot inject fabricated entries). Sample quality covers the
    two core sources (the global counts and the listing): both present ⇒
    ``measured``, some ⇒ ``degraded``, none ⇒ ``unknown``; an explicit
    ``unreadable`` gap (permission boundary on the target) caps quality at
    ``degraded`` — a partially visible process table is never reported as
    fully measured. The systemd service inventory does NOT degrade the
    sample (optional source, same policy as the system collector's failed
    units). The timestamp is the target's own ``date -u`` when parsable,
    otherwise the controller clock — a measured sample is always
    timestamped.
    """
    if controller_now.tzinfo is None:
        raise ValueError("controller_now must be timezone-aware")
    values: dict[str, str] = {}
    process_lines: list[str] = []
    service_lines: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "pp":
            process_lines.append(value)
        elif key == "svc":
            service_lines.append(value)
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

    entries: list[ProcessEntry] = []
    seen_pids: set[int] = set()
    for raw in process_lines[: _MAX_LISTED_BOUND_TARGET * 4]:
        entry = _parse_process_line(raw)
        if entry is None or entry.pid in seen_pids:
            continue
        seen_pids.add(entry.pid)
        entries.append(entry)
        if len(entries) >= _MAX_LISTED_BOUND_TARGET:
            break

    systemd_present: bool | None
    services: tuple[ServiceEntry, ...] | None
    marker = values.get("systemd")
    if marker == "yes":
        systemd_present = True
        if values.get("svc_rc") == "0":
            parsed_services: list[ServiceEntry] = []
            for raw in service_lines[:_MAX_SERVICES_BOUND_TARGET]:
                sub_raw, _, unit_raw = raw.strip().partition(" ")
                sub = _sub_field(sub_raw)
                unit = _unit_field(unit_raw)
                if sub is None or unit is None:
                    continue
                parsed_services.append(ServiceEntry(unit=unit, sub=sub))
            services = tuple(parsed_services)
        else:
            # A failed/bounded query is NOT zero service units.
            services = None
    elif marker == "no":
        systemd_present = False
        services = None  # absent source is unknown, never zero
    else:
        systemd_present = None
        services = None

    total_procs = _int_field(values.get("procs"))
    zombies = _int_field(values.get("zombies"))
    unreadable = _int_field(values.get("unreadable"))

    counts_present = total_procs is not None
    listing_present = bool(entries)
    gap = unreadable is not None and unreadable > 0
    if counts_present and listing_present:
        quality = ObservationQuality.DEGRADED if gap else ObservationQuality.MEASURED
    elif counts_present or listing_present:
        quality = ObservationQuality.DEGRADED
    else:
        quality = ObservationQuality.UNKNOWN

    return ProcessSample(
        observed_at=observed_at,
        observed_at_source=observed_at_source,
        quality=quality,
        total_procs=total_procs,
        zombies=zombies,
        unreadable=unreadable,
        processes=tuple(entries),
        systemd_present=systemd_present,
        services=services,
        svc_total=_int_field(values.get("svc_count")),
        svc_running=_int_field(values.get("svc_running")),
        svc_failed=_int_field(values.get("svc_failed")),
        svc_exited=_int_field(values.get("svc_exited")),
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

    Local mirror of the measured-probe classification (the transport owns
    the canonical table): only the coarse class matters here — the
    collector maps any of these to the unknown path, the codes exist so
    evidence and logs name the same rejection reason the transport suite
    pins.
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


def probe_processes(
    identity: SshIdentity,
    *,
    key_path: Path,
    known_hosts_path: Path,
    controller_now: datetime,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProcessSample:
    """Run the fixed process probe over the pinned-identity transport.

    Preconditions mirror :func:`mai.execution.ssh.probe_environment`: the
    key path comes from ``resolve_identity_key`` and the managed
    known-hosts file from ``pin_host_key`` (enrollment). Raises
    :class:`SshConnectError` (stable codes) for any transport failure; a
    target that answers is parsed into a :class:`ProcessSample` under the
    hard output budget.
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
            SshErrorCode.TIMEOUT, f"process probe timed out after {timeout}s"
        ) from None
    if len(proc.stdout.encode("utf-8")) > MAX_PROBE_OUTPUT_BYTES:
        raise SshConnectError(
            SshErrorCode.BOUNDED_OUTPUT_EXCEEDED,
            f"process probe output exceeded the {MAX_PROBE_OUTPUT_BYTES}-byte budget",
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


def findings_for_sample(sample: ProcessSample) -> tuple[FindingDraft, ...]:
    """Evaluate the collector's threshold policy against one sample (pure)."""
    drafts: list[FindingDraft] = []

    if sample.zombies is None:
        drafts.append(
            FindingDraft(
                kind="process.zombies",
                dedup_key="count",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    else:
        active = sample.zombies > 0
        drafts.append(
            FindingDraft(
                kind="process.zombies",
                dedup_key="count",
                severity="warning" if active else "info",
                summary=_truncate(
                    f"{sample.zombies} zombie processes await reaping",
                    SUMMARY_MAX_CHARS,
                ),
                state="active" if active else "clear",
            )
        )

    if sample.unreadable is None:
        drafts.append(
            FindingDraft(
                kind="process.read_gaps",
                dedup_key="unreadable",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    else:
        active = sample.unreadable > 0
        drafts.append(
            FindingDraft(
                kind="process.read_gaps",
                dedup_key="unreadable",
                severity="warning" if active else "info",
                summary=_truncate(
                    f"{sample.unreadable} /proc entries unreadable to the "
                    "collector identity (permission gap; sample is partial)",
                    SUMMARY_MAX_CHARS,
                ),
                state="active" if active else "clear",
            )
        )

    listed = [p for p in sample.processes if p.rss_kib > 0]
    if not listed:
        drafts.append(
            FindingDraft(
                kind="process.memory_hog",
                dedup_key="top_rss",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    else:
        hog = max(listed, key=lambda p: p.rss_kib)
        hog_mib = hog.rss_kib / 1024.0
        name = hog.comm if hog.comm is not None else f"pid {hog.pid}"
        severity: Literal["info", "warning", "critical"] = "info"
        if hog_mib >= MEM_HOG_CRIT_MIB:
            severity = "critical"
        elif hog_mib >= MEM_HOG_WARN_MIB:
            severity = "warning"
        drafts.append(
            FindingDraft(
                kind="process.memory_hog",
                dedup_key="top_rss",
                severity=severity,
                summary=_truncate(
                    f"{name} holds {hog_mib:.0f} MiB resident "
                    "(largest in the bounded listing)",
                    SUMMARY_MAX_CHARS,
                ),
                state="active" if severity != "info" else "clear",
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
    sample: ProcessSample,
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
    if sample.total_procs is not None:
        rows.append(row("proc.count", str(sample.total_procs)))
    if sample.zombies is not None:
        rows.append(row("proc.zombies", str(sample.zombies)))
    if sample.unreadable is not None:
        rows.append(row("proc.unreadable", str(sample.unreadable)))
    if sample.processes:
        rows.append(row("proc.listed", str(len(sample.processes))))
    for entry in sample.processes:
        rows.append(row(f"proc.{entry.pid}.cpu_ticks", str(entry.cpu_ticks)))
        rows.append(row(f"proc.{entry.pid}.rss_kib", str(entry.rss_kib)))
        if entry.comm is not None:
            rows.append(row(f"proc.{entry.pid}.comm", entry.comm))
    # Service counts are written only when the target actually reported
    # them (rc=0 query); a failed/unavailable source never becomes a zero.
    for name, value in (
        ("proc.svc_total", sample.svc_total),
        ("proc.svc_running", sample.svc_running),
        ("proc.svc_failed", sample.svc_failed),
        ("proc.svc_exited", sample.svc_exited),
    ):
        if value is not None:
            rows.append(row(name, str(value)))
    if sample.services is not None:
        for service in sample.services:
            rows.append(row("proc.svc", f"{service.sub}:{service.unit}"))
    return rows


async def record_sample(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    sample: ProcessSample,
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
    forbids anything else) — never a zero process count. Existing findings
    are deliberately left untouched: missing data neither re-evaluates nor
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
    unavailable — the caller-facing shape of acceptance 2. ``error`` carries
    only the stable rejection code.
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
        sample = probe_processes(
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
