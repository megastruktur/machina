"""Focused tests for read-only application discovery (card ``08-discovery``).

Acceptance coverage:

- **AC1** — actual Docker/Compose metadata is read via SSH with bounded
  output and original source paths: fixed read-only argv (pure surface
  tests + a fixture assertion that the composed requests are exactly the
  frozen command set), per-lane budget/truncation reporting (unit + real
  fixture with a tiny budget) and real-SSH discovery of a REAL Compose
  project on a real daemon, recording its original working dir and config
  file paths (C09 rule 6: paths only, nothing relocated).
- **AC2** — observed records distinguish discovered/managed with no
  automatic grant or relocation: managed tagging only from caller-supplied
  adoption references (``application_id`` set iff ``managed``); the safe
  view is redaction by construction (env/commands/foreign labels never
  collected) proven with a sentinel that exists INSIDE the fixture
  container environment and compose project while never appearing in the
  inventory.

Two layers, per the test policy ("use a real host when the claim depends
on it"): pure/unit tests with a fake bounded-command lane (always run),
and fixture-backed tests against a disposable Arch sshd container with
the host docker socket mounted read-use (``mai-fxt-08-disc-*`` namespace,
runtime-environment ADR D1–D5), auto-skipped when docker is unavailable.
The fixture is producer-owned and removed with recorded proof (D4).

Secret hygiene: the sentinel below is a synthetic NOT-A-SECRET marker; the
fixture key is a throwaway in ``/tmp/mai-fxt-08-disc-*`` deleted with the
fixture; no credential material enters any record or evidence path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Sequence

import pytest

from mai.applications import discover as discover_module
from mai.applications.discover import (
    COMPOSE_LABEL_CONFIG_FILES,
    COMPOSE_LABEL_PROJECT,
    COMPOSE_LABEL_SERVICE,
    COMPOSE_LABEL_WORKING_DIR,
    ApplicationOrigin,
    ComposeProjectObservation,
    DockerContainerObservation,
    DockerPsEntry,
    LaneKind,
    LaneState,
    ListenPortObservation,
    ManagedReference,
    ObservedApplication,
    StandaloneContainerObservation,
    SystemdUnitObservation,
    classify_managed,
    discover_server,
    group_docker_entries,
    parse_container_ports,
    parse_docker_ps,
    parse_listen_ports,
    parse_systemd_units,
)
from mai.contracts.server import SshIdentity
from mai.execution.commands import CommandOutcome, CommandRequest, CommandStatus
from mai.execution.ssh import (
    SshConnectError,
    SshErrorCode,
    pin_host_key,
    resolve_identity_key,
    scan_host_keys,
)

#: Synthetic non-disclosure marker (NOT a secret): planted in the fixture
#: compose environment; any record path leaking environment values or file
#: contents would surface it.
SENTINEL = "SENTINEL-mai-08-disc-not-a-secret"


def _identity(host: str = "203.0.113.10", port: int = 2222) -> SshIdentity:
    return SshIdentity(
        host=host,
        port=port,
        user="root",
        host_key="SHA256:" + "a" * 43,
        credential_ref="MAI_SSH_KEY_TEST",
    )


def _entry(
    container_id: str,
    name: str,
    *,
    image: str = "nginx:1.27",
    state: str = "running",
    status: str = "Up 2 minutes",
    ports: str | None = "",
    labels: dict[str, str] | None = None,
    service: str | None = None,
) -> DockerPsEntry:
    container = DockerContainerObservation(
        container_id=container_id,
        name=name,
        image=image,
        state=state,
        status=status,
        ports=parse_container_ports(ports or None),
        compose_service=service,
    )
    labels = labels or {}
    return DockerPsEntry(
        container=container,
        project_name=labels.get(COMPOSE_LABEL_PROJECT),
        working_dir=labels.get(COMPOSE_LABEL_WORKING_DIR),
        config_files=_split(labels.get(COMPOSE_LABEL_CONFIG_FILES)),
        environment_files=_split(labels.get("com.docker.compose.project.environment_file")),
    )


def _split(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(p.strip() for p in value.split(",") if p.strip())


# ---------------------------------------------------------------------------
# docker ps parsing (pure)
# ---------------------------------------------------------------------------


class TestContainerPorts:
    def test_host_mapping_v4(self) -> None:
        (mapping,) = parse_container_ports("0.0.0.0:8080->80/tcp")
        assert mapping.host_ip == "0.0.0.0"
        assert mapping.host_port == 8080
        assert mapping.container_port == 80
        assert mapping.protocol == "tcp"

    def test_host_mapping_v6(self) -> None:
        (mapping,) = parse_container_ports("[::]:8080->80/tcp")
        assert mapping.host_ip == "::"
        assert mapping.host_port == 8080
        assert mapping.container_port == 80

    def test_exposed_only_has_no_host_side(self) -> None:
        (mapping,) = parse_container_ports("5353/udp")
        assert mapping.host_ip is None
        assert mapping.host_port is None
        assert mapping.container_port == 5353
        assert mapping.protocol == "udp"

    def test_multiple_specs(self) -> None:
        ports = parse_container_ports("0.0.0.0:80->80/tcp, [::]:80->80/tcp, 443/tcp")
        assert len(ports) == 3
        assert ports[2] == parse_container_ports("443/tcp")[0]

    def test_empty_and_junk(self) -> None:
        assert parse_container_ports(None) == ()
        assert parse_container_ports("") == ()
        assert parse_container_ports("not-a-port") == ()


class TestLabelPairStringForm:
    """docker 29.x serializes ``docker ps`` labels as display text."""

    def test_flat_pairs_parsed_and_continuation_restored(self) -> None:
        raw = (
            "com.docker.compose.project.config_files=/a/compose.yaml,/a/override.yaml,"
            "com.docker.compose.project=webapp,com.docker.compose.depends_on=,"
            "com.docker.compose.project.working_dir=/srv/webapp"
        )
        meta = discover_module._compose_meta(raw)
        assert meta[COMPOSE_LABEL_PROJECT] == "webapp"
        assert meta[COMPOSE_LABEL_WORKING_DIR] == "/srv/webapp"
        assert discover_module._split_paths(
            meta["com.docker.compose.project.config_files"]
        ) == ("/a/compose.yaml", "/a/override.yaml")

    def test_empty_string_labels_yield_no_meta(self) -> None:
        assert discover_module._compose_meta("") == dict.fromkeys(
            discover_module._COMPOSE_LABELS
        )

    def test_sentinel_in_foreign_string_label_never_collected(self) -> None:
        raw = f"com.example.secret={SENTINEL},com.docker.compose.project=proj"
        meta = discover_module._compose_meta(raw)
        assert SENTINEL not in repr(meta)
        assert meta[COMPOSE_LABEL_PROJECT] == "proj"


def _docker_json(**overrides: object) -> str:
    base: dict[str, object] = {
        "Command": '"nginx: daemon off;"',
        "CreatedAt": "2026-10-16 20:00:00 +0000 UTC",
        "ID": "abc123def456",
        "Image": "nginx:1.27",
        "Labels": {},
        "Names": ["web-1"],
        "Networks": ["bridge"],
        "Ports": "0.0.0.0:8080->80/tcp",
        "RunningFor": "2 minutes ago",
        "Size": "0B",
        "State": "running",
        "Status": "Up 2 minutes",
    }
    base.update(overrides)
    return json.dumps(base)


class TestDockerPsParsing:
    def test_fields_parsed_and_leading_slash_stripped(self) -> None:
        result = parse_docker_ps(_docker_json(Names=["/web-1"]) + "\n")
        (entry,) = result.entries
        assert entry.container.container_id == "abc123def456"
        assert entry.container.name == "web-1"
        assert entry.container.image == "nginx:1.27"
        assert entry.container.state == "running"
        assert entry.container.ports[0].host_port == 8080
        assert entry.project_name is None
        assert result.malformed_lines == 0

    def test_string_names_form_tolerated(self) -> None:
        (entry,) = parse_docker_ps(_docker_json(Names="web-1") + "\n").entries
        assert entry.container.name == "web-1"

    def test_compose_labels_extracted(self) -> None:
        labels = {
            COMPOSE_LABEL_PROJECT: "webapp",
            COMPOSE_LABEL_WORKING_DIR: "/srv/webapp",
            COMPOSE_LABEL_CONFIG_FILES: "/srv/webapp/compose.yaml,/srv/webapp/override.yaml",
            "com.docker.compose.project.environment_file": "/srv/webapp/.env",
            COMPOSE_LABEL_SERVICE: "api",
        }
        (entry,) = parse_docker_ps(_docker_json(Labels=labels) + "\n").entries
        assert entry.project_name == "webapp"
        assert entry.working_dir == "/srv/webapp"
        assert entry.config_files == (
            "/srv/webapp/compose.yaml",
            "/srv/webapp/override.yaml",
        )
        assert entry.environment_files == ("/srv/webapp/.env",)
        assert entry.container.compose_service == "api"

    def test_sentinel_in_foreign_label_never_collected(self) -> None:
        labels = {
            "com.example.custom": SENTINEL,
            "com.docker.compose.project": "proj",
        }
        result = parse_docker_ps(_docker_json(Labels=labels) + "\n")
        (entry,) = result.entries
        assert SENTINEL not in repr(result)
        assert entry.project_name == "proj"

    def test_malformed_lines_counted_never_silent(self) -> None:
        raw = "\n".join(
            [
                _docker_json(),
                "this is not json",
                _docker_json(ID="", Names=["x"]),  # missing ID -> malformed
                _docker_json(Image=""),  # missing image -> malformed
                "",  # blank lines are skipped, not counted
            ]
        )
        result = parse_docker_ps(raw + "\n")
        assert len(result.entries) == 1
        assert result.malformed_lines == 3

    def test_null_labels_and_missing_status_tolerated(self) -> None:
        (entry,) = parse_docker_ps(_docker_json(Labels=None, Status=None) + "\n").entries
        assert entry.container.status == ""
        assert entry.project_name is None


class TestGrouping:
    def test_projects_and_standalones_deterministic(self) -> None:
        entries = (
            _entry("c3", "b-1", labels={COMPOSE_LABEL_PROJECT: "beta", COMPOSE_LABEL_WORKING_DIR: "/b"},
                   service="two"),
            _entry("c1", "a-1", labels={COMPOSE_LABEL_PROJECT: "alpha", COMPOSE_LABEL_WORKING_DIR: "/a",
                                        COMPOSE_LABEL_CONFIG_FILES: "/a/compose.yaml"},
                   service="one"),
            _entry("c2", "a-2", labels={COMPOSE_LABEL_PROJECT: "alpha", COMPOSE_LABEL_WORKING_DIR: "/a"},
                   service="dup"),
            _entry("c0", "solo", ports="0.0.0.0:5432->5432/tcp"),
        )
        grouped = group_docker_entries(entries)
        again = group_docker_entries(entries)
        assert grouped == again  # deterministic
        alpha, beta = grouped[0], grouped[1]
        assert isinstance(alpha, ComposeProjectObservation)
        assert alpha.project_name == "alpha"
        assert alpha.working_dir == "/a"  # first entry in container-id order
        assert alpha.config_files == ("/a/compose.yaml",)
        assert alpha.services == ("dup", "one")
        assert [c.container_id for c in alpha.containers] == ["c1", "c2"]
        assert isinstance(beta, ComposeProjectObservation)
        assert beta.project_name == "beta"
        (solo,) = grouped[2:]
        assert isinstance(solo, StandaloneContainerObservation)
        assert solo.container.container_id == "c0"

    def test_no_entries(self) -> None:
        assert group_docker_entries(()) == ()


# ---------------------------------------------------------------------------
# systemd / ss parsing (pure)
# ---------------------------------------------------------------------------


class TestSystemdParsing:
    def test_join_units_and_unit_files(self) -> None:
        units_out = (
            "sshd.service loaded active running OpenSSH Secure Shell Daemon\n"
            "fallback.service loaded active exited   \n"
            "nginx.service loaded failed failed  A description with spaces\n"
        )
        files_out = "sshd.service enabled enabled\nnginx.service disabled disabled\n"
        units = parse_systemd_units(units_out, files_out)
        assert [u.unit for u in units] == ["fallback.service", "nginx.service", "sshd.service"]
        by_name = {u.unit: u for u in units}
        sshd = by_name["sshd.service"]
        assert (sshd.load_state, sshd.active_state, sshd.sub_state) == ("loaded", "active", "running")
        assert sshd.description == "OpenSSH Secure Shell Daemon"
        assert sshd.unit_file_state == "enabled"
        nginx = by_name["nginx.service"]
        assert nginx.description == "A description with spaces"
        assert nginx.unit_file_state == "disabled"
        fallback = by_name["fallback.service"]
        assert fallback.description == ""
        assert fallback.unit_file_state is None  # never loaded: reported, not guessed

    def test_unit_file_only_entry_gets_none_runtime_states(self) -> None:
        units = parse_systemd_units("", "orphan.service static static\n")
        (orphan,) = units
        assert orphan.unit == "orphan.service"
        assert orphan.unit_file_state == "static"
        assert orphan.load_state is None
        assert orphan.active_state is None

    def test_headers_and_malformed_rows(self) -> None:
        units = parse_systemd_units(
            "UNIT LOAD ACTIVE SUB DESCRIPTION\nshort.row only\n",
            "UNIT FILE STATE\n",
        )
        assert units == ()
        # "short.row only" (2 fields) and both header lines counted malformed
        assert parse_systemd_units("a b c\n", "onlyone\n") == ()


class TestListenPorts:
    def test_real_ss_shape(self) -> None:
        output = (
            "State  Recv-Q  Send-Q  Local Address:Port  Peer Address:Port\n"
            "LISTEN 0      4096    0.0.0.0:22          0.0.0.0:*\n"
            "LISTEN 0      4096       [::]:22             [::]:*\n"
            "LISTEN 0      128    127.0.0.53%lo:53      0.0.0.0:*\n"
            "LISTEN 0      511    127.0.0.1:8443        0.0.0.0:*\n"
        )
        ports, malformed = parse_listen_ports(output)
        assert malformed == 0
        assert (ports[0].address, ports[0].port) == ("0.0.0.0", 22)
        assert (ports[1].address, ports[1].port) == ("::", 22)
        assert ports[2].address == "127.0.0.53%lo"  # interface suffix kept verbatim
        assert ports[3].port == 8443

    def test_malformed_and_wild_ports(self) -> None:
        ports, malformed = parse_listen_ports(
            "LISTEN 0 4096 broken-line-no-port\n"
            "LISTEN 0 4096 0.0.0.0:99999 0.0.0.0:*\n"
        )
        assert ports == ()
        assert malformed == 2

    def test_empty(self) -> None:
        assert parse_listen_ports("") == ((), 0)


# ---------------------------------------------------------------------------
# discovered/managed classification + record invariants (pure)
# ---------------------------------------------------------------------------


class TestManagedClassification:
    def test_compose_project_matched_by_name_and_working_dir(self) -> None:
        app_id = uuid.uuid4()
        other_id = uuid.uuid4()
        managed_ref = ManagedReference(
            application_id=app_id, compose_project="webapp", working_dir="/elsewhere"
        )
        compose = ObservedApplication(
            origin=ApplicationOrigin.COMPOSE,
            detail=ComposeProjectObservation(
                project_name="webapp",
                working_dir="/srv/webapp/",
                config_files=("/srv/webapp/compose.yaml",),
                environment_files=(),
                services=("api",),
                containers=(),
            ),
        )
        units = ObservedApplication(
            origin=ApplicationOrigin.SYSTEMD_UNIT,
            detail=SystemdUnitObservation(unit="sshd.service", unit_file_state="enabled"),
        )
        tagged = classify_managed((compose, units), [managed_ref])
        assert tagged[0].managed is True
        assert tagged[0].application_id == app_id
        assert tagged[1].managed is False
        assert tagged[1].application_id is None
        # working_dir match normalizes trailing slashes only
        by_dir = ManagedReference(application_id=other_id, working_dir="/srv/webapp")
        (retagged,) = classify_managed((compose,), [by_dir])
        assert retagged.managed is True
        assert retagged.application_id == other_id

    def test_unit_exact_match_and_standalone_never_managed(self) -> None:
        app_id = uuid.uuid4()
        ref = ManagedReference(application_id=app_id, unit_name="sshd.service")
        unit = ObservedApplication(
            origin=ApplicationOrigin.SYSTEMD_UNIT,
            detail=SystemdUnitObservation(unit="sshd.service", unit_file_state=None),
        )
        (tagged,) = classify_managed((unit,), [ref])
        assert tagged.managed is True and tagged.application_id == app_id
        (missed,) = classify_managed(
            (unit,), [ManagedReference(application_id=app_id, unit_name="sshd2.service")]
        )
        assert missed.managed is False

    def test_no_refs_returns_records_unchanged(self) -> None:
        unit = ObservedApplication(
            origin=ApplicationOrigin.SYSTEMD_UNIT,
            detail=SystemdUnitObservation(unit="u.service", unit_file_state=None),
        )
        assert classify_managed((unit,), []) == (unit,)

    def test_first_match_in_application_id_order(self) -> None:
        low = uuid.UUID(int=1)
        high = uuid.UUID(int=2)
        compose = ObservedApplication(
            origin=ApplicationOrigin.COMPOSE,
            detail=ComposeProjectObservation(
                project_name="webapp", working_dir=None, config_files=(),
                environment_files=(), services=(), containers=(),
            ),
        )
        refs = [
            ManagedReference(application_id=high, compose_project="webapp"),
            ManagedReference(application_id=low, compose_project="webapp"),
        ]
        (tagged,) = classify_managed((compose,), refs)
        assert tagged.application_id == low  # deterministic, not input order

    def test_reference_and_record_validation(self) -> None:
        with pytest.raises(ValueError):
            ManagedReference(application_id=uuid.uuid4())  # no selector
        with pytest.raises(ValueError):
            ManagedReference(application_id="not-a-uuid", unit_name="u.service")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            ManagedReference(application_id=uuid.uuid4(), unit_name="   ")
        unit = SystemdUnitObservation(unit="u.service", unit_file_state=None)
        with pytest.raises(ValueError):
            ObservedApplication(
                origin=ApplicationOrigin.COMPOSE, detail=unit, managed=True
            )  # managed without application_id


class TestNoAdoptionSideEffects:
    """C09 rule 1/6 negative control: observations carry no adoption shape."""

    def test_records_carry_original_paths_and_no_manifest_shape(self) -> None:
        entry = _entry(
            "c1",
            "web-1",
            labels={
                COMPOSE_LABEL_PROJECT: "webapp",
                COMPOSE_LABEL_WORKING_DIR: "/srv/original/place",
                COMPOSE_LABEL_CONFIG_FILES: "/srv/original/place/compose.yaml",
            },
            service="api",
        )
        (project,) = group_docker_entries((entry,))
        assert isinstance(project, ComposeProjectObservation)
        # Original target paths verbatim: discovery relocates nothing.
        assert project.working_dir == "/srv/original/place"
        assert project.config_files == ("/srv/original/place/compose.yaml",)
        # No canonical-manifest vocabulary exists anywhere on the record.
        assert "manifest" not in repr(project)
        assert "applications/" not in repr(project)
        # The record set is observation-only: no grant/execution field exists.
        assert set(ComposeProjectObservation.__dataclass_fields__) == {
            "project_name", "working_dir", "config_files", "environment_files",
            "services", "containers",
        }
        assert set(ObservedApplication.__dataclass_fields__) == {
            "origin", "detail", "managed", "application_id",
        }


# ---------------------------------------------------------------------------
# discover_server orchestration (fake bounded-command lane, no docker)
# ---------------------------------------------------------------------------


class FakeLanes:
    """Queued fake for ``mai.execution.commands.execute_command``."""

    def __init__(self, outcomes: Sequence[CommandOutcome]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[CommandRequest] = []

    def __call__(
        self,
        identity: SshIdentity,
        request: CommandRequest,
        *,
        key_path: object,
        known_hosts_path: object,
    ) -> CommandOutcome:
        self.requests.append(request)
        return self.outcomes.pop(0)


def _outcome(
    status: CommandStatus,
    *,
    rc: int | None = None,
    out: str = "",
    err: str = "",
    truncated: bool = False,
) -> CommandOutcome:
    return CommandOutcome(
        op_id=uuid.uuid4(),
        server_id=uuid.uuid4(),
        display="fake",
        status=status,
        exit_code=rc,
        stdout=out,
        stdout_bytes=len(out.encode()),
        stdout_truncated=truncated,
        stderr=err,
        stderr_bytes=len(err.encode()),
        stderr_truncated=False,
        timed_out=status is CommandStatus.OUTCOME_UNKNOWN,
        cwd_change_failed=False,
        duration_seconds=0.01,
    )


DOCKER_OK = _outcome(
    CommandStatus.SUCCEEDED,
    out=_docker_json(
        Labels=(
            "com.docker.compose.project=webapp,"
            "com.docker.compose.project.working_dir=/srv/webapp,"
            "com.docker.compose.project.config_files=/srv/webapp/compose.yaml,"
            "com.docker.compose.service=api"
        ),
        Ports="0.0.0.0:8080->80/tcp",
    )
    + "\n"
    + _docker_json(ID="dead000beef0", Names=["solo"], Ports="")  # standalone
    + "\n",
)
UNITS_OK = _outcome(
    CommandStatus.SUCCEEDED,
    out="sshd.service loaded active running OpenSSH Secure Shell Daemon\n",
)
UNIT_FILES_OK = _outcome(CommandStatus.SUCCEEDED, out="sshd.service enabled enabled\n")
SS_OK = _outcome(
    CommandStatus.SUCCEEDED,
    out="State Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
        "LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\n",
)

LANE_COUNT = 4  # docker, systemctl list-units, systemctl list-unit-files, ss


class TestDiscoverServer:
    def _run(self, fake: FakeLanes, **kwargs: object) -> object:
        return discover_server(
            _identity(),
            server_id=uuid.uuid4(),
            key_path="/keys/k",
            known_hosts_path="/kh/known_hosts",
            **kwargs,
        )

    def test_all_lanes_observed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeLanes([DOCKER_OK, UNITS_OK, UNIT_FILES_OK, SS_OK])
        monkeypatch.setattr(discover_module, "execute_command", fake)
        inventory = self._run(fake)  # type: ignore[arg-type]
        assert [lane.state for lane in inventory.lanes] == [LaneState.OBSERVED] * 3
        assert [lane.lane for lane in inventory.lanes] == [
            LaneKind.DOCKER,
            LaneKind.SYSTEMD,
            LaneKind.LISTEN_PORTS,
        ]
        kinds = sorted(app.origin for app in inventory.applications)
        assert kinds == [
            ApplicationOrigin.COMPOSE,
            ApplicationOrigin.DOCKER_CONTAINER,
            ApplicationOrigin.SYSTEMD_UNIT,
        ]
        assert inventory.listen_ports == (ListenPortObservation(address="0.0.0.0", port=22),)
        assert inventory.observed_at.endswith("+00:00")  # UTC, explicit timezone
        assert len(fake.requests) == LANE_COUNT

    def test_requests_are_exactly_the_fixed_readonly_commands(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeLanes([DOCKER_OK, UNITS_OK, UNIT_FILES_OK, SS_OK])
        monkeypatch.setattr(discover_module, "execute_command", fake)
        server_id = uuid.uuid4()
        discover_server(
            _identity(host="target.example"),
            server_id=server_id,
            key_path="/keys/k",
            known_hosts_path="/kh/known_hosts",
        )
        argvs = [request.argv for request in fake.requests]
        assert argvs == [
            ("docker", "ps", "-a", "--format", "{{json .}}"),
            ("systemctl", "list-units", "--type=service", "--all", "--no-legend", "--no-pager"),
            ("systemctl", "list-unit-files", "--type=service", "--no-legend"),
            ("ss", "-tln"),
        ]
        for request in fake.requests:
            assert request.server_id == server_id
            assert request.working_directory is None  # no target-side writes
            assert request.shell is None  # structured argv only (C04 rule 2)

    def test_managed_refs_flow_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeLanes([DOCKER_OK, UNITS_OK, UNIT_FILES_OK, SS_OK])
        monkeypatch.setattr(discover_module, "execute_command", fake)
        app_id = uuid.uuid4()
        inventory = self._run(  # type: ignore[arg-type]
            fake, managed_refs=[ManagedReference(application_id=app_id, compose_project="webapp")]
        )
        by_origin = {app.origin: app for app in inventory.applications}
        assert by_origin[ApplicationOrigin.COMPOSE].managed is True
        assert by_origin[ApplicationOrigin.COMPOSE].application_id == app_id
        assert by_origin[ApplicationOrigin.DOCKER_CONTAINER].managed is False

    def test_docker_absent_does_not_infect_other_lanes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeLanes(
            [
                _outcome(CommandStatus.FAILED, rc=127, err="sh: docker: command not found"),
                UNITS_OK,
                UNIT_FILES_OK,
                SS_OK,
            ]
        )
        monkeypatch.setattr(discover_module, "execute_command", fake)
        inventory = self._run(fake)  # type: ignore[arg-type]
        docker_lane = inventory.lanes[0]
        assert (docker_lane.lane, docker_lane.state) == (LaneKind.DOCKER, LaneState.TOOL_ABSENT)
        assert inventory.applications and all(
            app.origin is ApplicationOrigin.SYSTEMD_UNIT for app in inventory.applications
        )
        assert inventory.listen_ports  # ports lane unaffected

    def test_daemon_unavailable_and_permission_denied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for stderr, expected in [
            ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
             LaneState.DAEMON_UNAVAILABLE),
            ("permission denied while trying to connect to the Docker daemon socket at "
             "unix:///var/run/docker.sock", LaneState.PERMISSION_DENIED),
        ]:
            fake = FakeLanes(
                [_outcome(CommandStatus.FAILED, rc=1, err=stderr), UNITS_OK, UNIT_FILES_OK, SS_OK]
            )
            monkeypatch.setattr(discover_module, "execute_command", fake)
            inventory = self._run(fake)  # type: ignore[arg-type]
            assert inventory.lanes[0].state is expected

    def test_non_systemd_target_skips_unit_files_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeLanes(
            [
                DOCKER_OK,
                _outcome(
                    CommandStatus.FAILED,
                    rc=1,
                    err="System has not been booted with systemd as init system",
                ),
                SS_OK,
            ]
        )
        monkeypatch.setattr(discover_module, "execute_command", fake)
        inventory = self._run(fake)  # type: ignore[arg-type]
        assert inventory.lanes[1].state is LaneState.NOT_SYSTEMD
        assert len(fake.requests) == 3  # unit-files never sent
        assert all(a[0] != "systemctl" or "list-unit-files" not in a for a in
                   (r.argv for r in fake.requests))

    def test_tool_absent_ss_lane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeLanes(
            [DOCKER_OK, UNITS_OK, UNIT_FILES_OK,
             _outcome(CommandStatus.FAILED, rc=127, err="sh: ss: command not found")]
        )
        monkeypatch.setattr(discover_module, "execute_command", fake)
        inventory = self._run(fake)  # type: ignore[arg-type]
        assert inventory.lanes[2].state is LaneState.TOOL_ABSENT
        assert inventory.listen_ports == ()

    def test_unknown_outcome_never_invents_records(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeLanes(
            [_outcome(CommandStatus.OUTCOME_UNKNOWN), UNITS_OK, UNIT_FILES_OK, SS_OK]
        )
        monkeypatch.setattr(discover_module, "execute_command", fake)
        inventory = self._run(fake)  # type: ignore[arg-type]
        docker_lane = inventory.lanes[0]
        assert docker_lane.state is LaneState.OUTCOME_UNKNOWN
        assert not any(
            app.origin in (ApplicationOrigin.COMPOSE, ApplicationOrigin.DOCKER_CONTAINER)
            for app in inventory.applications
        )

    def test_generic_failure_carries_bounded_single_line_detail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hostile = "boom " + "x" * 800 + "\nsecond line with KEY=value"
        fake = FakeLanes(
            [
                _outcome(CommandStatus.FAILED, rc=2, err=hostile),
                UNITS_OK,
                UNIT_FILES_OK,
                SS_OK,
            ]
        )
        monkeypatch.setattr(discover_module, "execute_command", fake)
        inventory = self._run(fake)  # type: ignore[arg-type]
        lane = inventory.lanes[0]
        assert lane.state is LaneState.FAILED
        assert "\n" not in lane.detail
        assert len(lane.detail) <= 200

    def test_truncation_reported_on_lane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeLanes(
            [
                _outcome(
                    CommandStatus.SUCCEEDED,
                    out=_docker_json() + "\n",
                    truncated=True,
                ),
                UNITS_OK,
                UNIT_FILES_OK,
                SS_OK,
            ]
        )
        monkeypatch.setattr(discover_module, "execute_command", fake)
        inventory = self._run(fake)  # type: ignore[arg-type]
        assert inventory.lanes[0].truncated is True

    def test_malformed_docker_lines_surface_on_lane(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeLanes(
            [
                _outcome(CommandStatus.SUCCEEDED, out="garbage\n"),
                UNITS_OK,
                UNIT_FILES_OK,
                SS_OK,
            ]
        )
        monkeypatch.setattr(discover_module, "execute_command", fake)
        inventory = self._run(fake)  # type: ignore[arg-type]
        assert inventory.lanes[0].malformed_lines == 1
        assert not any(
            app.origin in (ApplicationOrigin.COMPOSE, ApplicationOrigin.DOCKER_CONTAINER)
            for app in inventory.applications
        )

    def test_transport_failure_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refusing(*args: object, **kwargs: object) -> object:
            raise SshConnectError(SshErrorCode.HOST_KEY_MISMATCH, "refused by host key pinning")

        monkeypatch.setattr(discover_module, "execute_command", refusing)
        with pytest.raises(SshConnectError) as excinfo:
            discover_server(
                _identity(),
                server_id=uuid.uuid4(),
                key_path="/keys/k",
                known_hosts_path="/kh/known_hosts",
            )
        assert excinfo.value.code is SshErrorCode.HOST_KEY_MISMATCH

    def test_parameter_validation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeLanes([DOCKER_OK, UNITS_OK, UNIT_FILES_OK, SS_OK])
        monkeypatch.setattr(discover_module, "execute_command", fake)
        with pytest.raises(ValueError):
            discover_server(
                _identity(),
                server_id="not-a-uuid",  # type: ignore[arg-type]
                key_path="/keys/k",
                known_hosts_path="/kh/known_hosts",
            )
        with pytest.raises(ValueError):
            discover_server(
                _identity(),
                server_id=uuid.uuid4(),
                key_path="/keys/k",
                known_hosts_path="/kh/known_hosts",
                timeout=0,
            )
        with pytest.raises(ValueError):
            discover_server(
                _identity(),
                server_id=uuid.uuid4(),
                key_path="/keys/k",
                known_hosts_path="/kh/known_hosts",
                output_budget=10**9,
            )


# ---------------------------------------------------------------------------
# Disposable Arch sshd fixture with the docker socket (runtime ADR D1–D5)
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-08-disc-sshd"
FIXTURE_IMAGE_TAG = "mai-fxt-08-disc-sshd:latest"
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-08-disc")
FIXTURE_COMPOSE_PROJECT = "mai-fxt-08-disc-proj"
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_08_DISC_FIXTURE"


def _docker(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


class DiscoveryFixture:
    """Arch sshd + docker-cli container; also owns one real compose project."""

    def __init__(self) -> None:
        self.scratch = FIXTURE_SCRATCH / uuid.uuid4().hex
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.key_path = self.scratch / "fixture_key"
        self.compose_dir = self.scratch / "original-place"
        self.compose_dir.mkdir(parents=True, exist_ok=True)
        self.port = 0
        self.cleanup_proof = "<not cleaned>"

    def start(self) -> SshIdentity:
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key_path),
             "-C", f"{FIXTURE_CONTAINER}-test"],
            check=True, capture_output=True,
        )
        os.chmod(self.key_path, 0o600)
        self._ensure_image()
        host_socket = Path("/var/run/docker.sock")
        assert host_socket.exists(), "host docker socket missing for fixture mount"
        run = _docker(
            "run", "--rm", "-d", "--name", FIXTURE_CONTAINER,
            "-p", "127.0.0.1::22",
            "-v", f"{host_socket}:/var/run/docker.sock",
            FIXTURE_IMAGE_TAG,
            "sh", "-c",
            "rm -f /etc/ssh/ssh_host_* && ssh-keygen -A "
            "&& mkdir -p /var/empty /run/sshd "
            "&& exec /usr/bin/sshd -D",
        )
        assert run.returncode == 0, f"fixture start failed: {run.stderr[-800:]}"
        self._refresh_port()
        self._provision_authorized_key()
        self._wait_keyscan()
        return self.identity_for(self._current_fingerprint())

    def _ensure_image(self) -> None:
        if _docker("image", "inspect", FIXTURE_IMAGE_TAG).returncode == 0:
            return
        context = self.scratch / "build-context"
        context.mkdir(parents=True, exist_ok=True)
        (context / "Dockerfile").write_text(
            "FROM archlinux:latest\n"
            "# iproute2 (ss) ships in the base image; the Arch repos have no\n"
            "# docker-cli package — the `docker` package provides the CLI and\n"
            "# is installed WITHOUT ever starting its daemon (the target-side\n"
            "# daemon is the HOST's, reached read-only through the socket).\n"
            "RUN pacman-key --init && pacman -Sy --noconfirm openssh docker "
            "&& ssh-keygen -A\n",
            encoding="utf-8",
        )
        build = _docker("build", "-q", "-t", FIXTURE_IMAGE_TAG, str(context), timeout=1800)
        assert build.returncode == 0, f"fixture image build failed: {build.stderr[-800:]}"

    def _refresh_port(self) -> None:
        port_out = _docker(
            "inspect", "--format",
            "{{(index (index .NetworkSettings.Ports \"22/tcp\") 0).HostPort}}",
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

    def _provision_authorized_key(self) -> None:
        pub = self.key_path.with_suffix(".pub").read_bytes()
        provision = subprocess.run(
            ["docker", "exec", "-i", FIXTURE_CONTAINER, "sh", "-c",
             "mkdir -p /root/.ssh && chmod 700 /root/.ssh "
             "&& cat > /root/.ssh/authorized_keys "
             "&& chmod 600 /root/.ssh/authorized_keys"],
            input=pub, capture_output=True, timeout=30,
        )
        assert provision.returncode == 0, provision.stderr.decode()[-800:]

    def _scan_now(self) -> tuple[str, ...]:
        scan = subprocess.run(
            ["ssh-keyscan", "-p", str(self.port), "-t", "ed25519", "127.0.0.1"],
            capture_output=True, text=True, timeout=30,
        )
        return tuple(line for line in scan.stdout.splitlines() if line.strip())

    def _current_fingerprint(self) -> str:
        lines = self._scan_now()
        assert lines, "fixture keyscan produced no host keys"
        (ed25519,) = [line for line in lines if " ssh-ed25519 " in line] or [lines[0]]
        keyfile = self.scratch / "scan-hostkey.pub"
        keyfile.write_text(ed25519 + "\n", encoding="utf-8")
        keygen = subprocess.run(
            ["ssh-keygen", "-lf", str(keyfile)], capture_output=True, text=True
        )
        assert keygen.returncode == 0, keygen.stderr
        return keygen.stdout.split()[1]

    def _wait_keyscan(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                if self._scan_now():
                    return
                last_error = "empty keyscan"
            except subprocess.TimeoutExpired as error:
                last_error = str(error)
            time.sleep(1.0)
        raise AssertionError(f"fixture sshd never became ready: {last_error}")

    def env_resolver(self) -> object:
        from mai.credentials.env import EnvResolver

        env_file = self.scratch / "fixture.env"
        env_file.write_text(f"{FIXTURE_CREDENTIAL_REF}={self.key_path}\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        return EnvResolver(env_file)

    def start_compose_project(self) -> Path:
        """One real UNMANAGED compose project on the host daemon.

        The project carries the sentinel as a literal container
        environment value (not just a file): the safe-view claim is
        checked against an env value that really exists on the target.
        """
        (self.compose_dir / "compose.yaml").write_text(
            "services:\n"
            "  sleeper:\n"
            "    image: busybox:latest\n"
            "    command: [\"sleep\", \"300\"]\n"
            "    environment:\n"
            f"      MAI_SENTINEL: \"{SENTINEL}\"\n",
            encoding="utf-8",
        )
        up = subprocess.run(
            ["docker", "compose", "-p", FIXTURE_COMPOSE_PROJECT,
             "-f", str(self.compose_dir / "compose.yaml"), "up", "-d", "--quiet-pull"],
            capture_output=True, text=True, timeout=600, check=False,
        )
        assert up.returncode == 0, f"fixture compose up failed: {up.stderr[-800:]}"
        return self.compose_dir

    def cleanup(self) -> str:
        parts: list[str] = []
        down = subprocess.run(
            ["docker", "compose", "-p", FIXTURE_COMPOSE_PROJECT,
             "-f", str(self.compose_dir / "compose.yaml"), "down", "-v", "--remove-orphans"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        parts.append(f"compose down rc={down.returncode}")
        ps = _docker("ps", "-a", "--filter", f"label=com.docker.compose.project={FIXTURE_COMPOSE_PROJECT}",
                     "--format", "{{.Names}}")
        parts.append(f"compose containers after down: '{ps.stdout.strip() or '<none>'}'")
        rm = _docker("rm", "-f", FIXTURE_CONTAINER, timeout=60)
        parts.append(f"fixture container rm rc={rm.returncode}")
        listing = _docker(
            "ps", "-a", "--filter", f"name=^{FIXTURE_CONTAINER}$", "--format", "{{.Names}}"
        )
        parts.append(f"fixture container after rm: '{listing.stdout.strip() or '<none>'}'")
        shutil.rmtree(FIXTURE_SCRATCH, ignore_errors=True)
        parts.append(f"scratch {FIXTURE_SCRATCH} exists: {FIXTURE_SCRATCH.exists()}")
        self.cleanup_proof = "; ".join(parts)
        return self.cleanup_proof


@pytest.fixture(scope="module")
def discovery_fixture() -> object:
    if not _docker_available():
        pytest.skip("docker unavailable: fixture discovery tests require it")
    fixture = DiscoveryFixture()
    fixture.identity = fixture.start()
    yield fixture
    print(f"\nFIXTURE CLEANUP PROOF: {fixture.cleanup()}")


def _enrolled(fixture: DiscoveryFixture) -> tuple[SshIdentity, str, str]:
    identity = fixture.identity  # type: ignore[attr-defined]
    key_path = str(resolve_identity_key(fixture.env_resolver(), identity.credential_ref))  # type: ignore[attr-defined]
    known_hosts_path = str(fixture.scratch / "known_hosts")  # type: ignore[attr-defined]
    scan = scan_host_keys(identity)
    pin_host_key(identity, Path(known_hosts_path))
    assert scan
    return identity, key_path, known_hosts_path


class TestAgainstRealSshFixture:
    def test_real_ssh_discovery_reports_honest_lane_states(
        self, discovery_fixture: DiscoveryFixture
    ) -> None:
        identity, key_path, known_hosts_path = _enrolled(discovery_fixture)
        inventory = discover_server(
            identity, server_id=uuid.uuid4(), key_path=key_path, known_hosts_path=known_hosts_path
        )
        states = {lane.lane: lane.state for lane in inventory.lanes}
        # The fixture HAS docker (host socket) and ss; it has no systemd.
        assert states[LaneKind.DOCKER] is LaneState.OBSERVED
        assert states[LaneKind.LISTEN_PORTS] is LaneState.OBSERVED
        assert states[LaneKind.SYSTEMD] in (LaneState.TOOL_ABSENT, LaneState.NOT_SYSTEMD)
        # ss runs in the fixture's own network namespace: sshd on 22 is real.
        assert any(port.port == 22 for port in inventory.listen_ports)
        # Real daemon metadata flowed through real SSH: the docker lane saw
        # the host's containers (>= the fixture container itself).
        fixture_names = [
            app for app in inventory.applications
            if app.origin in (ApplicationOrigin.COMPOSE, ApplicationOrigin.DOCKER_CONTAINER)
        ]
        assert fixture_names, "docker lane parsed no containers from the real daemon"

    def test_unenrolled_endpoint_refused_before_any_command(
        self, discovery_fixture: DiscoveryFixture
    ) -> None:
        identity = discovery_fixture.identity  # type: ignore[attr-defined]
        key_path = str(resolve_identity_key(
            discovery_fixture.env_resolver(), identity.credential_ref  # type: ignore[attr-defined]
        ))
        with pytest.raises(SshConnectError) as excinfo:
            discover_server(
                identity,
                server_id=uuid.uuid4(),
                key_path=key_path,
                known_hosts_path=str(discovery_fixture.scratch / "never-enrolled"),  # type: ignore[attr-defined]
            )
        assert excinfo.value.code is SshErrorCode.INVALID_CONFIGURATION

    def test_real_compose_project_discovered_with_sentinel_safe_view(
        self, discovery_fixture: DiscoveryFixture
    ) -> None:
        original_dir = discovery_fixture.start_compose_project()  # type: ignore[attr-defined]
        identity, key_path, known_hosts_path = _enrolled(discovery_fixture)
        app_id = uuid.uuid4()
        inventory = discover_server(
            identity,
            server_id=uuid.uuid4(),
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            managed_refs=[ManagedReference(application_id=app_id, compose_project=FIXTURE_COMPOSE_PROJECT)],
        )
        matches = [
            app for app in inventory.applications
            if app.origin is ApplicationOrigin.COMPOSE
            and app.detail.project_name == FIXTURE_COMPOSE_PROJECT
        ]
        assert len(matches) == 1, f"fixture project not discovered: {inventory.applications!r}"
        project = matches[0].detail
        assert isinstance(project, ComposeProjectObservation)
        # AC1: original source paths recorded; nothing relocated, no
        # canonical manifest path anywhere in the inventory.
        assert project.working_dir == str(original_dir)
        assert project.config_files == (str(original_dir / "compose.yaml"),)
        assert project.services == ("sleeper",)
        assert matches[0].managed is True and matches[0].application_id == app_id
        rendered = repr(inventory)
        assert "manifest" not in rendered
        assert f"applications/{app_id}" not in rendered
        # AC2 safe view: the sentinel exists as a REAL container env value
        # on the target, yet never reaches any record.
        assert SENTINEL not in rendered
        # The discovered/managed distinction held for everything else.
        others = [app for app in inventory.applications if app is not matches[0]]
        assert all(app.managed is False and app.application_id is None for app in others)

    def test_output_budget_enforced_over_real_ssh(
        self, discovery_fixture: DiscoveryFixture
    ) -> None:
        identity, key_path, known_hosts_path = _enrolled(discovery_fixture)
        inventory = discover_server(
            identity,
            server_id=uuid.uuid4(),
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            output_budget=64,  # force a cut inside the first JSON line
        )
        docker_lane = next(lane for lane in inventory.lanes if lane.lane is LaneKind.DOCKER)
        assert docker_lane.truncated is True
