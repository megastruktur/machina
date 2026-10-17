"""Read-only application discovery over pinned SSH (card ``08-discovery``).

Contracts.md **C09** rules 1 and 6 on the **C04** transport:

- **C09 rule 1** — discovery produces OBSERVED records only. Nothing here
  enables execution (G2, §C08 rule 4), mints a grant, writes a canonical
  manifest or relocates anything: the module performs fixed read-only
  target queries through the bounded synchronous lane
  (:func:`mai.execution.commands.execute_command`) and returns plain
  dataclasses. Adoption is a later explicit admin action (``08-adoption``).
- **C09 rule 6 / IDEA §6** — external Compose projects stay where they
  are. The records carry the ORIGINAL target-side source paths (compose
  working dir, config files, environment-file paths) as referenced
  metadata; no canonical ``servers/<server_id>/applications/<id>/manifest``
  record exists in this module's output and nothing is copied or moved.
- **Acceptance 2 (safe view)** — Compose secrets are redacted by
  CONSTRUCTION, not by post-filtering: environment values, container
  commands/args, mounts and arbitrary labels are never collected. Only a
  frozen allowlist of ``com.docker.compose.*`` metadata labels (project
  name, working dir, config files, environment-file PATHS, service name)
  is recorded; file CONTENTS are never read, so a sentinel inside a
  compose ``.env`` or a container environment can never reach the
  inventory. Discovered vs managed is an explicit per-record distinction:
  the caller supplies :class:`ManagedReference` selectors recorded at
  adoption time (application_id + project/working-dir/unit name); a match
  tags the record ``managed`` with that opaque ``application_id``, all
  other observations stay ``discovered`` (``managed=False``). Discovery
  never creates or implies either a grant or an adoption.

Sources, one bounded command each, lanes fail independently and are
reported per lane (:class:`LaneReport`, C08 rule 6: absence/offline is
reported, never all-green):

- ``docker ps -a --format '{{json .}}'`` — one JSON object per container.
  Compose projects are derived from the frozen label allowlist; containers
  without compose labels are standalone discovered containers.
- ``systemctl list-units --type=service --all --no-legend --no-pager`` plus
  ``systemctl list-unit-files --type=service --no-legend`` — joined per
  unit name. Discovery runs without a tty, so systemd does not truncate
  unit names.
- ``ss -tln`` — listening TCP ports of the target's network namespace.

A missing tool (exit 127), an unreachable Docker daemon, a permission-
denied socket and a non-systemd target are all REPORTED lane states, not
infections of the other lanes. Transport failures (unenrolled/rotated host
key, unreachable endpoint, auth rejection) raise
:class:`mai.execution.ssh.SshConnectError` before any command runs — the
pinned-identity enrollment boundary is owned by the P04 modules this
module composes. Wall clock and output stay bounded per lane
(:class:`mai.execution.commands.CommandRequest`); truncation is recorded
on the lane, never silently accepted as complete.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from mai.contracts.server import SshIdentity
from mai.execution.commands import (
    CommandRequest,
    CommandStatus,
    MAX_COMMAND_TIMEOUT_SECONDS,
    MAX_OUTPUT_BUDGET_BYTES,
    execute_command,
)

__all__ = [
    "COMPOSE_LABEL_PROJECT",
    "COMPOSE_LABEL_CONFIG_FILES",
    "COMPOSE_LABEL_ENVIRONMENT_FILES",
    "COMPOSE_LABEL_SERVICE",
    "COMPOSE_LABEL_WORKING_DIR",
    "DEFAULT_OUTPUT_BUDGET_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "ApplicationOrigin",
    "ComposeProjectObservation",
    "DockerContainerObservation",
    "DockerPsEntry",
    "DockerPsResult",
    "DiscoveryInventory",
    "LaneKind",
    "LaneReport",
    "LaneState",
    "ListenPortObservation",
    "ManagedReference",
    "ObservedApplication",
    "PortMapping",
    "StandaloneContainerObservation",
    "SystemdUnitObservation",
    "classify_managed",
    "discover_server",
    "group_docker_entries",
    "parse_docker_ps",
    "parse_listen_ports",
    "parse_systemd_units",
]

#: Default per-lane wall-clock budget (controller-enforced by
#: :func:`execute_command`; the remote payload never outlives the ssh call
#: for read-only probes).
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Default per-lane output budget (first-N-bytes, per stream). Truncation
#: is reported on the lane, never silently accepted as complete data.
DEFAULT_OUTPUT_BUDGET_BYTES = 2 * 1024 * 1024

# The frozen compose-metadata label allowlist (safe view). Values of these
# five keys are deployment METADATA (names and source paths); every other
# label — and any container env/command/mount data — is never collected.
COMPOSE_LABEL_PROJECT = "com.docker.compose.project"
COMPOSE_LABEL_WORKING_DIR = "com.docker.compose.project.working_dir"
COMPOSE_LABEL_CONFIG_FILES = "com.docker.compose.project.config_files"
COMPOSE_LABEL_ENVIRONMENT_FILES = "com.docker.compose.project.environment_file"
COMPOSE_LABEL_SERVICE = "com.docker.compose.service"

_COMPOSE_LABELS = (
    COMPOSE_LABEL_PROJECT,
    COMPOSE_LABEL_WORKING_DIR,
    COMPOSE_LABEL_CONFIG_FILES,
    COMPOSE_LABEL_ENVIRONMENT_FILES,
    COMPOSE_LABEL_SERVICE,
)


class ApplicationOrigin(StrEnum):
    """Which target source an observed application came from."""

    COMPOSE = "compose"
    DOCKER_CONTAINER = "docker_container"
    SYSTEMD_UNIT = "systemd_unit"


class LaneKind(StrEnum):
    """The three fixed discovery lanes."""

    DOCKER = "docker"
    SYSTEMD = "systemd"
    LISTEN_PORTS = "listen_ports"


class LaneState(StrEnum):
    """Reported per-lane availability (C08 rule 6: honest absence).

    ``observed`` — parsed data below; every other state means the lane
    contributed NO records. ``outcome_unknown`` reuses the frozen C04
    vocabulary: the command may or may not have run; no data is invented.
    """

    OBSERVED = "observed"
    TOOL_ABSENT = "tool_absent"
    DAEMON_UNAVAILABLE = "daemon_unavailable"
    PERMISSION_DENIED = "permission_denied"
    NOT_SYSTEMD = "not_systemd"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


# ---------------------------------------------------------------------------
# Observed records (plain frozen data; no behavior, no writes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PortMapping:
    """One host->container port mapping (``None`` = exposed only)."""

    container_port: int
    protocol: str
    host_ip: str | None = None
    host_port: int | None = None


@dataclass(frozen=True, slots=True)
class DockerContainerObservation:
    """One observed container: reported facts only.

    Deliberately absent (safe view): command/args, environment, mounts,
    healthcheck details and all non-allowlisted labels — these carry
    secret-bearing free text on real hosts.
    """

    container_id: str
    name: str
    image: str
    state: str
    status: str
    ports: tuple[PortMapping, ...]
    compose_service: str | None = None


@dataclass(frozen=True, slots=True)
class DockerPsEntry:
    """One parsed ``docker ps`` line plus its compose metadata labels."""

    container: DockerContainerObservation
    project_name: str | None = None
    working_dir: str | None = None
    config_files: tuple[str, ...] = ()
    environment_files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ComposeProjectObservation:
    """One observed Compose project at its ORIGINAL target paths.

    ``working_dir``/``config_files``/``environment_files`` are the
    referenced external deployment paths (C02 rule 3, C09 rule 6): paths
    only, never file contents. ``None``/empty means the target did not
    report the label — never a local guess.
    """

    project_name: str
    working_dir: str | None
    config_files: tuple[str, ...]
    environment_files: tuple[str, ...]
    services: tuple[str, ...]
    containers: tuple[DockerContainerObservation, ...]


@dataclass(frozen=True, slots=True)
class StandaloneContainerObservation:
    """One observed container WITHOUT compose labels (``docker run``)."""

    container: DockerContainerObservation


@dataclass(frozen=True, slots=True)
class SystemdUnitObservation:
    """One observed service unit.

    ``load_state``/``active_state``/``sub_state``/``description`` are
    ``None`` when the unit was known only from ``list-unit-files`` (never
    loaded in this boot) — absence is reported, not inferred. An empty
    ``description`` stays empty exactly as reported.
    """

    unit: str
    unit_file_state: str | None
    load_state: str | None = None
    active_state: str | None = None
    sub_state: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ListenPortObservation:
    """One listening TCP socket (``ss -tln`` local address:port)."""

    address: str
    port: int


@dataclass(frozen=True, slots=True)
class ObservedApplication:
    """The inventory record: one observed application.

    ``managed=True`` (and only then) carries the opaque ``application_id``
    of an ADOPTED application whose recorded reference matched; every
    other record is a discovered observation with ``application_id=None``.
    No field of this record enables execution, carries a grant or names a
    canonical MAI manifest path.
    """

    origin: ApplicationOrigin
    detail: ComposeProjectObservation | StandaloneContainerObservation | SystemdUnitObservation
    managed: bool = False
    application_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        expected = {
            ApplicationOrigin.COMPOSE: ComposeProjectObservation,
            ApplicationOrigin.DOCKER_CONTAINER: StandaloneContainerObservation,
            ApplicationOrigin.SYSTEMD_UNIT: SystemdUnitObservation,
        }
        if not isinstance(self.detail, expected[self.origin]):
            raise ValueError(
                f"origin {self.origin.value} requires {expected[self.origin].__name__}"
            )
        if self.managed != (self.application_id is not None):
            raise ValueError("managed flag and application_id must be set together")


@dataclass(frozen=True, slots=True)
class LaneReport:
    """Per-lane outcome (C08 rule 6: absence/offline/error states)."""

    lane: LaneKind
    state: LaneState
    detail: str = "<none>"
    truncated: bool = False
    malformed_lines: int = 0


@dataclass(frozen=True, slots=True)
class DiscoveryInventory:
    """The complete observed inventory for one server.

    ``observed_at`` is the controller-side UTC observation timestamp. The
    inventory is a value: discovering again produces a fresh record; no
    canonical file, projection row or grant is touched by this module.
    """

    server_id: uuid.UUID
    observed_at: str
    applications: tuple[ObservedApplication, ...]
    listen_ports: tuple[ListenPortObservation, ...]
    lanes: tuple[LaneReport, ...]


@dataclass(frozen=True, slots=True)
class ManagedReference:
    """One adoption-time reference used to tag records ``managed``.

    Selectors are exact reported values (compose project name, normalized
    working dir, systemd unit name). At least one selector is required; a
    reference with none could match nothing and is rejected as malformed.
    """

    application_id: uuid.UUID
    compose_project: str | None = None
    working_dir: str | None = None
    unit_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.application_id, uuid.UUID):
            raise ValueError("application_id must be an opaque UUID")
        for field in ("compose_project", "working_dir", "unit_name"):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field} selector must be None or a non-empty string")
        if self.compose_project is None and self.working_dir is None and self.unit_name is None:
            raise ValueError("a managed reference needs at least one selector")


# ---------------------------------------------------------------------------
# docker ps parsing (pure)
# ---------------------------------------------------------------------------

_PORT_SPEC_RE = re.compile(r"^(?:(?P<host>.+?)->)?(?P<cport>\d+)/(?P<proto>tcp|udp)$")


def parse_container_ports(ports_field: str | None) -> tuple[PortMapping, ...]:
    """Parse the ``docker ps`` Ports column (pure; unparseable items dropped).

    Shapes: ``80/tcp`` (exposed only), ``0.0.0.0:8080->80/tcp``,
    ``[::]:8080->80/tcp``; several mappings arrive comma-separated. The
    IPv6 host address keeps its brackets stripped (``::``), the interface
    suffix form is preserved verbatim.
    """
    if not ports_field:
        return ()
    mappings: list[PortMapping] = []
    for spec in ports_field.split(","):
        spec = spec.strip()
        if not spec:
            continue
        match = _PORT_SPEC_RE.match(spec)
        if match is None:
            continue
        host_part = match.group("host")
        host_ip: str | None = None
        host_port: int | None = None
        if host_part:
            if host_part.startswith("["):  # [::]:8080
                addr, _, port = host_part[1:].partition("]:")
                host_ip, host_port = addr, int(port)
            else:
                addr, _, port = host_part.rpartition(":")
                host_ip, host_port = (addr, int(port)) if port else (None, None)
                host_ip = host_ip or None
        mappings.append(
            PortMapping(
                container_port=int(match.group("cport")),
                protocol=match.group("proto"),
                host_ip=host_ip,
                host_port=host_port,
            )
        )
    return tuple(mappings)


def _parse_label_pairs(raw: str) -> dict[str, str]:
    """Parse docker's flat ``k=v,k2=v2`` label serialization.

    ``docker ps`` serializes labels as display text on current engines
    (measured: docker 29.x emits ``"Labels":"k=v,k2=v2"``), so a pair's
    VALUE may itself contain commas: a segment without ``=`` continues
    the previous value (the comma is restored). A value containing BOTH
    a comma and an ``=`` is genuinely ambiguous in this lossy wire form —
    an engine serialization limit, documented rather than hidden.
    """
    pairs: dict[str, str] = {}
    last_key: str | None = None
    for segment in raw.split(","):
        key, sep, value = segment.partition("=")
        if sep:
            pairs[key] = value
            last_key = key
        elif last_key is not None:
            pairs[last_key] += f",{segment}"
    return pairs


def _compose_meta(labels: dict[str, str] | str | None) -> dict[str, str | None]:
    """Allowlisted compose labels only; everything else is never read."""
    if isinstance(labels, str):
        source: dict[str, str] = _parse_label_pairs(labels)
    elif isinstance(labels, dict):
        source = labels
    else:
        source = {}
    return {key: (source.get(key) or None) for key in _COMPOSE_LABELS}


def _split_paths(value: str | None) -> tuple[str, ...]:
    """Comma-joined path list label -> tuple (paths only, order kept)."""
    if not value:
        return ()
    return tuple(path.strip() for path in value.split(",") if path.strip())


@dataclass(frozen=True, slots=True)
class DockerPsResult:
    """Parsed ``docker ps`` outcome: entries plus malformed-line count."""

    entries: tuple[DockerPsEntry, ...]
    malformed_lines: int


def parse_docker_ps(output: str) -> DockerPsResult:
    """Parse ``docker ps -a --format '{{json .}}'`` output (pure).

    One JSON object per line. Lines that are not valid JSON or lack the
    required container fields are COUNTED as malformed (bounded discovery
    reports partial parses; it never silently drops lines). The Labels
    field arrives as an object on older engines and as docker's flat
    ``k=v,k2=v2`` display string on current engines (measured 29.x);
    both are reduced to the frozen compose-label allowlist before any
    record exists. Non-allowlisted
    labels and all other fields are dropped before any record exists.
    """
    entries: list[DockerPsEntry] = []
    malformed = 0
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(raw, dict):
            malformed += 1
            continue
        container_id = raw.get("ID")
        image = raw.get("Image")
        state = raw.get("State")
        names = raw.get("Names")
        if not isinstance(container_id, str) or not container_id:
            malformed += 1
            continue
        if isinstance(names, list):  # newer engines report a list
            name = names[0] if names and isinstance(names[0], str) else ""
        elif isinstance(names, str):  # older engines report one string
            name = names
        else:
            name = ""
        if not name or not isinstance(image, str) or not image or not isinstance(state, str):
            malformed += 1
            continue
        meta = _compose_meta(raw.get("Labels"))
        status = raw.get("Status")
        entries.append(
            DockerPsEntry(
                container=DockerContainerObservation(
                    container_id=container_id,
                    name=name.removeprefix("/"),
                    image=image,
                    state=state,
                    status=status if isinstance(status, str) else "",
                    ports=parse_container_ports(
                        raw.get("Ports") if isinstance(raw.get("Ports"), str) else None
                    ),
                    compose_service=meta[COMPOSE_LABEL_SERVICE],
                ),
                project_name=meta[COMPOSE_LABEL_PROJECT],
                working_dir=meta[COMPOSE_LABEL_WORKING_DIR],
                config_files=_split_paths(meta[COMPOSE_LABEL_CONFIG_FILES]),
                environment_files=_split_paths(meta[COMPOSE_LABEL_ENVIRONMENT_FILES]),
            )
        )
    return DockerPsResult(entries=tuple(entries), malformed_lines=malformed)


def group_docker_entries(
    entries: tuple[DockerPsEntry, ...],
) -> tuple[ComposeProjectObservation | StandaloneContainerObservation, ...]:
    """Group parsed containers into compose projects + standalones (pure).

    Compose metadata (paths, env files) comes from the labels of the
    first entry per project in stable ``(container_id, name)`` order; a
    project whose containers disagree on the reported paths is a hostile
    or corrupted target and keeps the first-reported values (the conflict
    itself is NOT hidden: both containers stay listed with their own
    labels-derived service names). Output is deterministic.
    """
    by_project: dict[str, list[DockerPsEntry]] = {}
    standalone: list[DockerPsEntry] = []
    for entry in entries:
        if entry.project_name:
            by_project.setdefault(entry.project_name, []).append(entry)
        else:
            standalone.append(entry)
    projects: list[ComposeProjectObservation] = []
    for project_name in sorted(by_project):
        members = sorted(by_project[project_name], key=lambda e: (e.container.container_id, e.container.name))
        head = members[0]
        services = tuple(
            sorted({m.container.compose_service for m in members if m.container.compose_service})
        )
        projects.append(
            ComposeProjectObservation(
                project_name=project_name,
                working_dir=head.working_dir,
                config_files=head.config_files,
                environment_files=head.environment_files,
                services=services,
                containers=tuple(m.container for m in members),
            )
        )
    containers = tuple(
        StandaloneContainerObservation(container=e.container)
        for e in sorted(standalone, key=lambda e: (e.container.container_id, e.container.name))
    )
    return tuple(projects) + containers


# ---------------------------------------------------------------------------
# systemd parsing (pure)
# ---------------------------------------------------------------------------

_SYSTEMD_HEADERS = ("UNIT", "UNIT FILE")


def parse_systemd_units(
    units_output: str, unit_files_output: str
) -> tuple[SystemdUnitObservation, ...]:
    """Join ``systemctl list-units`` and ``list-unit-files`` (pure).

    ``list-units`` rows: ``UNIT LOAD ACTIVE SUB DESCRIPTION`` (description
    may be empty or contain spaces). ``list-unit-files`` rows:
    ``UNIT_FILE STATE [VENDOR-PRESET]``. The union is reported: a unit
    file never loaded this boot is a real installed service with
    ``None`` runtime states — absence from ``list-units`` is reported,
    never guessed into ``inactive``.
    """
    units: dict[str, tuple[str, str, str, str]] = {}
    malformed = 0
    for line in units_output.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.split()[0] in _SYSTEMD_HEADERS:  # defensive: legend leak
            continue
        fields = line.split(maxsplit=4)
        if len(fields) < 4:
            malformed += 1
            continue
        unit, load, active, sub = fields[0], fields[1], fields[2], fields[3]
        description = fields[4] if len(fields) == 5 else ""
        units[unit] = (load, active, sub, description)
    observations: dict[str, SystemdUnitObservation] = {}
    for line in unit_files_output.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split()
        if not fields or fields[0] in _SYSTEMD_HEADERS or len(fields) < 2:
            malformed += 1
            continue
        observations[fields[0]] = SystemdUnitObservation(
            unit=fields[0], unit_file_state=fields[1]
        )
    for unit, (load, active, sub, description) in units.items():
        known = observations.get(unit)
        observations[unit] = SystemdUnitObservation(
            unit=unit,
            unit_file_state=known.unit_file_state if known else None,
            load_state=load,
            active_state=active,
            sub_state=sub,
            description=description,
        )
    return tuple(sorted(observations.values(), key=lambda o: o.unit))


# ---------------------------------------------------------------------------
# ss parsing (pure)
# ---------------------------------------------------------------------------

_SS_LOCAL_INDEX = 3  # State Recv-Q Send-Q Local Peer


def parse_listen_ports(output: str) -> tuple[tuple[ListenPortObservation, ...], int]:
    """Parse ``ss -tln`` output; returns (ports, malformed-line count)."""
    ports: list[ListenPortObservation] = []
    malformed = 0
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split()
        if fields[0] in ("State", "Netid"):  # header
            continue
        if len(fields) <= _SS_LOCAL_INDEX:
            malformed += 1
            continue
        local = fields[_SS_LOCAL_INDEX]
        address, _, port_text = local.rpartition(":")
        if not port_text.isdigit():
            malformed += 1
            continue
        port = int(port_text)
        if port > 65535:
            malformed += 1
            continue
        if address.startswith("[") and address.endswith("]"):
            address = address[1:-1]
        ports.append(ListenPortObservation(address=address, port=port))
    return tuple(ports), malformed


# ---------------------------------------------------------------------------
# discovered/managed classification (pure)
# ---------------------------------------------------------------------------


def _normalized_dir(path: str) -> str:
    """Adoption-time normalization: trailing slashes only, case kept."""
    return path.rstrip("/")


def classify_managed(
    applications: tuple[ObservedApplication, ...],
    managed_refs: Sequence[ManagedReference],
) -> tuple[ObservedApplication, ...]:
    """Tag records whose reported identity matches an adoption reference.

    Matching is an EXACT comparison on reported values: a compose project
    matches on project name or normalized working dir; a systemd unit on
    the exact unit name; standalone containers have no frozen selector and
    stay discovered. References are applied in stable ``application_id``
    order and the first match wins (ambiguity between adoption records is
    an adoption-data problem owned by ``08-adoption``, not hidden here and
    not resolved by discovery-side inference).
    """
    if not managed_refs:
        return applications
    ordered = sorted(managed_refs, key=lambda ref: ref.application_id.bytes)
    tagged: list[ObservedApplication] = []
    for application in applications:
        matched: uuid.UUID | None = None
        for ref in ordered:
            detail = application.detail
            if isinstance(detail, ComposeProjectObservation):
                if ref.compose_project is not None and ref.compose_project == detail.project_name:
                    matched = ref.application_id
                    break
                if (
                    ref.working_dir is not None
                    and detail.working_dir is not None
                    and _normalized_dir(ref.working_dir) == _normalized_dir(detail.working_dir)
                ):
                    matched = ref.application_id
                    break
            elif isinstance(detail, SystemdUnitObservation):
                if ref.unit_name is not None and ref.unit_name == detail.unit:
                    matched = ref.application_id
                    break
        tagged.append(
            ObservedApplication(
                origin=application.origin,
                detail=application.detail,
                managed=matched is not None,
                application_id=matched,
            )
        )
    return tuple(tagged)


# ---------------------------------------------------------------------------
# SSH orchestration (the only effectful code: fixed read-only commands)
# ---------------------------------------------------------------------------

_DOCKER_PS_COMMAND = ("docker", "ps", "-a", "--format", "{{json .}}")
_SYSTEMD_UNITS_COMMAND = (
    "systemctl",
    "list-units",
    "--type=service",
    "--all",
    "--no-legend",
    "--no-pager",
)
_SYSTEMD_UNIT_FILES_COMMAND = (
    "systemctl",
    "list-unit-files",
    "--type=service",
    "--no-legend",
)
_SS_COMMAND = ("ss", "-tln")

_TOOL_NOT_FOUND_EXIT = 127


def _bounded_tail(stderr: str) -> str:
    """Bounded single-line value-free stderr tail (ssh.py precedent)."""
    flat = " ".join(stderr.split())
    return flat[-200:] if flat else "<none>"


def _run_lane(
    identity: SshIdentity,
    *,
    server_id: uuid.UUID,
    display: str,
    argv: tuple[str, ...],
    key_path: str,
    known_hosts_path: str,
    timeout: float,
    output_budget: int,
) -> tuple[CommandStatus, int | None, str, str, bool]:
    """Run one bounded read-only lane command; returns its raw outcome."""
    request = CommandRequest(
        op_id=uuid.uuid4(),
        server_id=server_id,
        display=display,
        timeout=timeout,
        output_budget=output_budget,
        argv=argv,
    )
    outcome = execute_command(
        identity,
        request,
        key_path=key_path,
        known_hosts_path=known_hosts_path,
    )
    return (
        outcome.status,
        outcome.exit_code,
        outcome.stdout,
        outcome.stderr,
        outcome.stdout_truncated,
    )


def _lane_failure_report(
    lane: LaneKind, status: CommandStatus, exit_code: int | None, stderr: str
) -> LaneReport:
    """Map one non-success lane outcome to its reported lane state."""
    detail = _bounded_tail(stderr)
    if status is CommandStatus.OUTCOME_UNKNOWN:
        return LaneReport(lane=lane, state=LaneState.OUTCOME_UNKNOWN, detail=detail)
    if exit_code == _TOOL_NOT_FOUND_EXIT:
        return LaneReport(lane=lane, state=LaneState.TOOL_ABSENT, detail=detail)
    lowered = stderr.lower()
    if "cannot connect to the docker daemon" in lowered or "docker daemon is not running" in lowered:
        return LaneReport(lane=lane, state=LaneState.DAEMON_UNAVAILABLE, detail=detail)
    if "permission denied" in lowered and "docker.sock" in lowered:
        return LaneReport(lane=lane, state=LaneState.PERMISSION_DENIED, detail=detail)
    if "system has not been booted with systemd" in lowered:
        return LaneReport(lane=lane, state=LaneState.NOT_SYSTEMD, detail=detail)
    return LaneReport(lane=lane, state=LaneState.FAILED, detail=detail)


def discover_server(
    identity: SshIdentity,
    *,
    server_id: uuid.UUID,
    key_path: str,
    known_hosts_path: str,
    managed_refs: Sequence[ManagedReference] = (),
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    output_budget: int = DEFAULT_OUTPUT_BUDGET_BYTES,
) -> DiscoveryInventory:
    """Discover applications on one enrolled server over pinned SSH.

    Preconditions are the P4 enrollment invariants (unenrolled/rotated
    endpoints are refused by :func:`execute_command` before anything
    runs). Every lane is independent: a lane failure is a
    :class:`LaneReport`, never a fabricated empty success and never a
    failure of the other lanes. Only observed records come back — no
    grant, no canonical manifest, no relocation (module docstring).

    Raises:
        CommandRequestError: malformed bounded-request parameters.
        SshConnectError: transport-level refusal/failure (the endpoint
            was never reached or the pinned identity was rejected).
    """
    if not isinstance(server_id, uuid.UUID):
        raise ValueError("server_id must be an opaque UUID (C01 rule 1)")
    if not 0 < timeout <= MAX_COMMAND_TIMEOUT_SECONDS:
        raise ValueError(f"timeout must be in (0, {MAX_COMMAND_TIMEOUT_SECONDS}] seconds")
    if not 0 < output_budget <= MAX_OUTPUT_BUDGET_BYTES:
        raise ValueError(f"output_budget must be in (0, {MAX_OUTPUT_BUDGET_BYTES}] bytes")

    applications: list[ObservedApplication] = []
    listen_ports: tuple[ListenPortObservation, ...] = ()
    lanes: list[LaneReport] = []

    # -- docker lane -------------------------------------------------------
    status, exit_code, stdout, stderr, truncated = _run_lane(
        identity,
        server_id=server_id,
        display="discovery: docker ps -a (compose + container inventory)",
        argv=_DOCKER_PS_COMMAND,
        key_path=key_path,
        known_hosts_path=known_hosts_path,
        timeout=timeout,
        output_budget=output_budget,
    )
    if status is CommandStatus.SUCCEEDED:
        parsed = parse_docker_ps(stdout)
        lanes.append(
            LaneReport(
                lane=LaneKind.DOCKER,
                state=LaneState.OBSERVED,
                truncated=truncated,
                malformed_lines=parsed.malformed_lines,
            )
        )
        applications.extend(
            ObservedApplication(
                origin=ApplicationOrigin.COMPOSE if isinstance(g, ComposeProjectObservation)
                else ApplicationOrigin.DOCKER_CONTAINER,
                detail=g,
            )
            for g in group_docker_entries(parsed.entries)
        )
    else:
        lanes.append(_lane_failure_report(LaneKind.DOCKER, status, exit_code, stderr))

    # -- systemd lane ------------------------------------------------------
    status, exit_code, stdout, stderr, truncated = _run_lane(
        identity,
        server_id=server_id,
        display="discovery: systemctl list-units (service inventory)",
        argv=_SYSTEMD_UNITS_COMMAND,
        key_path=key_path,
        known_hosts_path=known_hosts_path,
        timeout=timeout,
        output_budget=output_budget,
    )
    if status is CommandStatus.SUCCEEDED:
        units_status, units_exit, units_out, units_err, units_truncated = _run_lane(
            identity,
            server_id=server_id,
            display="discovery: systemctl list-unit-files (enablement states)",
            argv=_SYSTEMD_UNIT_FILES_COMMAND,
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            timeout=timeout,
            output_budget=output_budget,
        )
        if units_status is CommandStatus.SUCCEEDED:
            units = parse_systemd_units(stdout, units_out)
            lanes.append(
                LaneReport(
                    lane=LaneKind.SYSTEMD,
                    state=LaneState.OBSERVED,
                    truncated=truncated or units_truncated,
                )
            )
            applications.extend(
                ObservedApplication(origin=ApplicationOrigin.SYSTEMD_UNIT, detail=u)
                for u in units
            )
        else:
            lanes.append(_lane_failure_report(LaneKind.SYSTEMD, units_status, units_exit, units_err))
    else:
        lanes.append(_lane_failure_report(LaneKind.SYSTEMD, status, exit_code, stderr))

    # -- listen ports lane ---------------------------------------------------
    status, exit_code, stdout, stderr, truncated = _run_lane(
        identity,
        server_id=server_id,
        display="discovery: ss -tln (listening ports)",
        argv=_SS_COMMAND,
        key_path=key_path,
        known_hosts_path=known_hosts_path,
        timeout=timeout,
        output_budget=output_budget,
    )
    if status is CommandStatus.SUCCEEDED:
        ports, malformed = parse_listen_ports(stdout)
        lanes.append(
            LaneReport(
                lane=LaneKind.LISTEN_PORTS,
                state=LaneState.OBSERVED,
                truncated=truncated,
                malformed_lines=malformed,
            )
        )
        listen_ports = ports
    else:
        lanes.append(_lane_failure_report(LaneKind.LISTEN_PORTS, status, exit_code, stderr))

    return DiscoveryInventory(
        server_id=server_id,
        observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        applications=classify_managed(tuple(applications), managed_refs),
        listen_ports=listen_ports,
        lanes=tuple(lanes),
    )
