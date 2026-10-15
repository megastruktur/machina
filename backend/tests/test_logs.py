"""Focused tests for the bounded target-log capability.

Card ``04-log-capability`` against contracts.md **C03** (rules 2, 5, 6,
7) and **C04** (rule 2), **C06** (rule 3). Layers, mirroring the sibling
capability cards:

- pure descriptor/decision pins (the read class needs NO approval in
  either prompting mode while every grant/scope/policy gate still binds,
  pinned against the real ``evaluate()``);
- pure payload→``LogQuery``/argv mapping and constructor wiring checks —
  including THE negative controls: an unsupported source and a path-like
  or metacharacter service name are rejected BEFORE any transport, so
  arbitrary host file reads are not implied;
- executor pre-effect guards and read composition against a scripted
  transport seam (NOTE on the double: it emulates the bounded lane's
  surrounding contract — typed outcomes, key/known-hosts kwargs —
  deliberately NOT any invariant under test here; transport boundedness
  itself is owned by the commands card);
- dispatcher-integration negative controls: deny outcomes never reach
  this executor, and the read class refuses a PRESENTED approval;
- real-boundary feature-gate scenario halves on the disposable Arch
  fixtures (fixture classes imported from ``test_supervisor``, not
  reimplemented, under THIS card's unique container/scratch namespace):
  a real journald (systemd-as-PID-1 target) service sentinel with an
  inline canary secret, a large-output retrieval bounded by the byte
  budget, a container-source retrieval through the real pinned-SSH
  transport, and denial of unsupported targets/sources with no
  controller-side artifact and no target-side invocation.

LIMITATION (disclosed, per test policy): the disposable fixture has no
container daemon, so the container-source half provisions a faithful
``docker`` CLI shim on the TARGET (exactly the argument surface this
capability composes: ``logs --since/--until --tail N --timestamps
NAME``, stdout/stderr split, docker-style error exits). It proves this
card's composition/transport/capture/redaction/artifact boundary for
the container source; real ``docker logs`` producer semantics are owned
by the ``04-verify`` assembled scenario.

Acceptance mapping: explicit target (context only)/service/time/line/
byte limits and secret filtering (acceptance 1); large output stored as
a scoped artifact with provenance, model context carries only a bounded
excerpt, and no path field exists to imply arbitrary file reads
(acceptance 2).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import shlex
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import mai.capabilities.logs as logs_module
from mai.capabilities.dispatch import (
    CapabilityCall,
    CapabilityDispatcher,
    DispatchReason,
    ExecutorResult,
    REDACTED,
)
from mai.capabilities.logs import (
    CAPABILITY_ID,
    DEFAULT_LOG_LINES,
    DEFAULT_OUTPUT_BUDGET_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    DISPLAY_LABEL,
    MAX_EXCERPT_BYTES,
    MAX_LOG_LINES,
    MAX_OUTPUT_BUDGET_BYTES,
    ArtifactReference,
    HostLogsCapability,
    LogSource,
    LogsError,
    LogsErrorCode,
    compose_log_argv,
    descriptor,
    log_query_from_payload,
)
from mai.contracts.server import (
    ExecutionEnvironment,
    PrivilegeMode,
    ServerProfile,
    SshIdentity,
)
from mai.credentials.env import EnvResolver, SecretValue
from mai.database import create_app_engine
from mai.execution.commands import (
    MAX_COMMAND_TIMEOUT_SECONDS,
    CommandOutcome,
    CommandRequest,
    CommandStatus,
    compose_remote_command,
)
from mai.execution.ssh import resolve_identity_key
from mai.plugins.registry import CapabilityRegistry
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import (
    DecisionReason,
    ExecutionContext,
    Policy,
    PolicyMode,
    evaluate,
)

from test_supervisor import (  # sibling-card fixture, imported not copied
    _ArchSshdFixture,
    _SystemdArchFixture,
    _docker,
    _docker_available,
    _ensure_fixture_image,
)

# C10 rule 2 fixture namespace: key files, known-hosts, artifacts, shim.
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-04-logs-tests")
FIXTURE_PLAIN_CONTAINER = "mai-fxt-04-logs-arch"
FIXTURE_SYSTEMD_CONTAINER = "mai-fxt-04-logs-sysd"
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_04_LOGS_FIXTURE"
CANARY_CREDENTIAL_REF = "MAI_LOGS_CANARY"
FIXTURE_USER = "mai-fxt"

SERVER_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
RUN_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
PRINCIPAL = "admin"
POLICY_REVISION = 7

GOOD_PAYLOAD = {"source": "journal", "service": "sshd.service"}


# ---------------------------------------------------------------------------
# Shared builders (pure)
# ---------------------------------------------------------------------------


def _identity(port: int = 2200) -> SshIdentity:
    return SshIdentity(
        host="127.0.0.1",
        port=port,
        user=FIXTURE_USER,
        host_key="SHA256:" + "A" * 43,
        credential_ref=FIXTURE_CREDENTIAL_REF,
    )


def _profile(
    identity: SshIdentity | None = None,
    *,
    privilege: PrivilegeMode = PrivilegeMode.NONE,
    server_id: uuid.UUID = SERVER_A,
) -> ServerProfile:
    return ServerProfile(
        schema_version=1,
        server_id=server_id,
        name="fixture target",
        prompt_revision=1,
        environment=ExecutionEnvironment.LINUX_SSH,
        ssh=identity or _identity(),
        privilege=privilege,
        capabilities=(CAPABILITY_ID,),
    )


def _context(
    *,
    server_id: uuid.UUID = SERVER_A,
    references: tuple[str, ...] = (FIXTURE_CREDENTIAL_REF,),
    allowed: tuple[str, ...] = (CAPABILITY_ID,),
    mode: PolicyMode = PolicyMode.DELEGATED,
) -> ExecutionContext:
    return ExecutionContext(
        principal=PRINCIPAL,
        server_id=server_id,
        run_id=RUN_1,
        parent_scope=None,
        profile_revision="prof-1",
        prompt_revision="prompt-1",
        credential_references=references,
        allowed_capabilities=allowed,
        mode=mode,
    )


def _policy(revision: int = POLICY_REVISION, denied: tuple[str, ...] = ()) -> Policy:
    return Policy(revision=revision, denied_capabilities=frozenset(denied))


def _grants(scope: frozenset[uuid.UUID] | None = None) -> dict[str, object]:
    return {
        CAPABILITY_ID: frozenset({SERVER_A}) if scope is None else scope,
    }


def _registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(descriptor())
    return registry


def _call(
    payload: object,
    *,
    context: ExecutionContext | None = None,
    credentials: dict[str, object] | None = None,
    op_id: uuid.UUID | None = None,
) -> CapabilityCall:
    return CapabilityCall(
        descriptor=descriptor(),
        payload=payload if isinstance(payload, dict) else {},
        context=context or _context(),
        credentials=credentials or {},  # type: ignore[arg-type]
        op_id=op_id or uuid.uuid4(),
    )


def _secret(value: str) -> SecretValue:
    return SecretValue(value)


# ---------------------------------------------------------------------------
# Descriptor and decision pins (acceptance 1: read class, gates intact)
# ---------------------------------------------------------------------------


def _decide(**overrides: object):  # type: ignore[no-untyped-def]
    arguments: dict[str, object] = {
        "context": _context(),
        "capability_id": CAPABILITY_ID,
        "descriptor": descriptor(),
        "grants": _grants(),
        "policy": _policy(),
        "adopted": True,
    }
    arguments.update(overrides)
    return evaluate(**arguments)  # type: ignore[arg-type]


class TestDescriptor:
    def test_descriptor_is_registry_valid_and_frozen_shape(self) -> None:
        registry = CapabilityRegistry()
        stored = registry.register(descriptor())
        assert stored.identifier == CAPABILITY_ID
        assert stored.version == "1.0.0"
        assert stored.effect_class == "read"
        assert stored.execution_environments == ("linux_ssh",)
        assert stored.resource_scope == ()
        assert stored.required_grants == (CAPABILITY_ID,)
        assert registry.versions(CAPABILITY_ID) == ("1.0.0",)
        document = descriptor().to_json()
        assert document["identifier"] == CAPABILITY_ID
        schema = document["input_schema"]
        assert schema["additionalProperties"] is False
        assert sorted(schema["required"]) == ["service", "source"]
        # no path field exists: arbitrary host file reads are not implied
        assert "path" not in schema["properties"]

    def test_read_class_needs_no_approval_in_either_mode(self) -> None:
        for mode in (PolicyMode.ASK, PolicyMode.DELEGATED):
            decision = _decide(context=_context(mode=mode))
            assert decision.allowed is True
            assert decision.requires_approval is False
            assert decision.effect_class == "read"
            assert decision.advisories == ()

    def test_every_authorization_gate_still_binds_reads(self) -> None:
        assert _decide(grants={}).reason is DecisionReason.CAPABILITY_NOT_GRANTED
        assert (
            _decide(policy=_policy(denied=(CAPABILITY_ID,))).reason
            is DecisionReason.CAPABILITY_DENIED_BY_POLICY
        )
        assert _decide(adopted=False).reason is DecisionReason.SERVER_NOT_ADOPTED
        assert (
            _decide(context=_context(server_id=uuid.uuid4())).reason
            is DecisionReason.SERVER_SCOPE_MISMATCH
        )
        assert (
            _decide(descriptor=None).reason is DecisionReason.CAPABILITY_NOT_REGISTERED
        )


# ---------------------------------------------------------------------------
# Payload → LogQuery mapping (pure)
# ---------------------------------------------------------------------------


class TestLogQueryFromPayload:
    def test_minimal_payload_maps_with_module_defaults(self) -> None:
        query = log_query_from_payload(GOOD_PAYLOAD)
        assert query.source is LogSource.JOURNAL
        assert query.service == "sshd.service"
        assert query.lines == DEFAULT_LOG_LINES
        assert query.output_budget == DEFAULT_OUTPUT_BUDGET_BYTES
        assert query.timeout == DEFAULT_TIMEOUT_SECONDS
        assert query.since is None and query.until is None

    def test_full_payload_overrides_every_bound(self) -> None:
        payload = {
            "source": "container",
            "service": "web-1",
            "since": "2026-10-14T10:00:00Z",
            "until": "2026-10-14 11:30:00",
            "lines": 50,
            "output_budget": 4096,
            "timeout": 12.5,
        }
        query = log_query_from_payload(payload)
        assert query.source is LogSource.CONTAINER
        assert query.service == "web-1"
        assert query.since == "2026-10-14T10:00:00Z"
        assert query.until == "2026-10-14 11:30:00"
        assert query.lines == 50
        assert query.output_budget == 4096
        assert query.timeout == 12.5

    @pytest.mark.parametrize(
        "payload",
        [
            "not-a-mapping",
            {},
            {"source": "journal"},
            {"service": "u.service"},
            {"source": "file", "service": "/etc/shadow"},
            {"source": "files", "service": "u.service"},
            {"source": "", "service": "u.service"},
            # THE path negative control: a path field or path-like
            # service name is a shape violation, never a read.
            {"source": "journal", "service": "u", "path": "/etc/shadow"},
            {"source": "journal", "service": "/etc/shadow"},
            {"source": "journal", "service": "../../etc/passwd"},
            {"source": "journal", "service": "u; rm -rf /"},
            {"source": "journal", "service": "u $(x)"},
            {"source": "journal", "service": "u `x`"},
            {"source": "journal", "service": "u | x"},
            {"source": "journal", "service": "a b"},
            {"source": "journal", "service": ""},
            {"source": "journal", "service": "x" * 129},
            {"source": "journal", "service": 7},
            {"source": "journal", "service": None},
            {"source": "journal", "service": "u", "since": "10m ago"},
            {"source": "journal", "service": "u", "since": "yesterday"},
            {"source": "journal", "service": "u", "since": "2026-10-14"},
            {"source": "journal", "service": "u", "since": 123},
            {"source": "journal", "service": "u", "until": ""},
            {"source": "journal", "service": "u", "lines": 0},
            {"source": "journal", "service": "u", "lines": -1},
            {"source": "journal", "service": "u", "lines": True},
            {"source": "journal", "service": "u", "lines": "5"},
            {"source": "journal", "service": "u", "lines": MAX_LOG_LINES + 1},
            {"source": "journal", "service": "u", "output_budget": 0},
            {"source": "journal", "service": "u", "output_budget": True},
            {
                "source": "journal",
                "service": "u",
                "output_budget": MAX_OUTPUT_BUDGET_BYTES + 1,
            },
            {"source": "journal", "service": "u", "timeout": 0},
            {
                "source": "journal",
                "service": "u",
                "timeout": MAX_COMMAND_TIMEOUT_SECONDS + 1,
            },
        ],
    )
    def test_shape_violations_reject_before_any_effect(self, payload) -> None:
        with pytest.raises(LogsError) as excinfo:
            log_query_from_payload(payload)
        assert excinfo.value.code is LogsErrorCode.INVALID_REQUEST


# ---------------------------------------------------------------------------
# Structured argv composition (C04 rule 2: never a shell string)
# ---------------------------------------------------------------------------


class TestComposeLogArgv:
    def test_journal_argv_is_structured_and_exact(self) -> None:
        query = log_query_from_payload({"source": "journal", "service": "sshd"})
        assert compose_log_argv(query) == (
            "journalctl",
            "--unit",
            "sshd",
            "--no-pager",
            "--utc",
            "--lines",
            "1000",
        )

    def test_journal_time_window_travels_with_the_command(self) -> None:
        query = log_query_from_payload(
            {
                "source": "journal",
                "service": "u.service",
                "since": "2026-10-14 10:00:00",
                "until": "2026-10-14T11:00:00Z",
                "lines": 7,
            }
        )
        assert compose_log_argv(query) == (
            "journalctl",
            "--unit",
            "u.service",
            "--no-pager",
            "--utc",
            "--lines",
            "7",
            "--since",
            "2026-10-14 10:00:00",
            "--until",
            "2026-10-14T11:00:00Z",
        )

    def test_container_argv_carries_tail_and_timestamps(self) -> None:
        query = log_query_from_payload(
            {"source": "container", "service": "web-1", "lines": 25}
        )
        assert compose_log_argv(query) == (
            "docker",
            "logs",
            "--tail",
            "25",
            "--timestamps",
            "web-1",
        )

    def test_container_time_window_precedes_the_name(self) -> None:
        query = log_query_from_payload(
            {
                "source": "container",
                "service": "web-1",
                "since": "2026-10-14T10:00:00Z",
                "until": "2026-10-14T11:00:00Z",
            }
        )
        assert compose_log_argv(query) == (
            "docker",
            "logs",
            "--since",
            "2026-10-14T10:00:00Z",
            "--until",
            "2026-10-14T11:00:00Z",
            "--tail",
            "1000",
            "--timestamps",
            "web-1",
        )

    def test_transport_quotes_each_element_into_one_remote_string(self) -> None:
        query = log_query_from_payload(
            {"source": "journal", "service": "app.server-1_x@y", "lines": 3}
        )
        request = CommandRequest(
            op_id=uuid.uuid4(),
            server_id=SERVER_A,
            display=DISPLAY_LABEL,
            timeout=query.timeout,
            output_budget=query.output_budget,
            argv=compose_log_argv(query),
        )
        composed = compose_remote_command(request)
        assert "journalctl" in composed
        assert "app.server-1_x@y" in composed
        for metachar in (";", "|", "$", "`"):
            assert metachar not in composed


# ---------------------------------------------------------------------------
# Constructor wiring (fail-closed composition)
# ---------------------------------------------------------------------------


class TestWiring:
    def _capability(self, **overrides: object) -> HostLogsCapability:
        base: dict[str, object] = {
            "profiles": lambda server_id: _profile(),
            "known_hosts": lambda server_id: "/tmp/mai-fxt-04-logs-tests/known_hosts",
            "artifacts_root": "/tmp/mai-fxt-04-logs-tests/artifacts",
        }
        base.update(overrides)
        return HostLogsCapability(**base)  # type: ignore[arg-type]

    def test_valid_construction_is_accepted(self) -> None:
        capability = self._capability(
            default_lines=5,
            default_output_budget=1024,
            default_timeout=7.5,
        )
        assert capability is not None

    @pytest.mark.parametrize(
        "overrides",
        [
            {"profiles": "not-callable"},
            {"known_hosts": 42},
            {"artifacts_root": ""},
            {"artifacts_root": "relative/path"},
            {"artifacts_root": "/tmp/x\x00y"},
            {"artifacts_root": "/tmp/x\ny"},
            {"artifacts_root": None},
            {"artifacts_root": "/" + "x" * 4096},
            {"default_lines": 0},
            {"default_lines": MAX_LOG_LINES + 1},
            {"default_lines": True},
            {"default_lines": 1.5},
            {"default_output_budget": 0},
            {"default_output_budget": MAX_OUTPUT_BUDGET_BYTES + 1},
            {"default_output_budget": True},
            {"default_timeout": 0},
            {"default_timeout": MAX_COMMAND_TIMEOUT_SECONDS + 1},
            {"default_timeout": True},
        ],
    )
    def test_wiring_violations_refuse_construction(self, overrides) -> None:
        with pytest.raises(LogsError) as excinfo:
            self._capability(**overrides)
        assert excinfo.value.code is LogsErrorCode.INVALID_CONTEXT


# ---------------------------------------------------------------------------
# Scripted transport seam (test double — see the module docstring NOTE)
# ---------------------------------------------------------------------------


class _ScriptedTransport:
    """Stands in for the bounded lane's module function.

    Emulates the SURROUNDING contract only (typed outcomes, key/
    known-hosts kwargs); it proves the executor's decisions, document
    assembly, redaction and artifact persistence, never remote-effect
    semantics.
    """

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[tuple[SshIdentity, CommandRequest, str, str]] = []

    def __call__(
        self,
        identity: SshIdentity,
        request: CommandRequest,
        *,
        key_path: str,
        known_hosts_path: str,
    ) -> CommandOutcome:
        self.calls.append((identity, request, key_path, known_hosts_path))
        if not isinstance(request, CommandRequest) or request.argv is None:
            raise TypeError("structured argv request required (lane contract)")
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        # lane contract: an outcome is keyed by the submitted operation
        return dataclasses.replace(item, op_id=request.op_id)


def _outcome(
    *,
    status: CommandStatus = CommandStatus.SUCCEEDED,
    exit_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    stdout_bytes: int | None = None,
    stderr_bytes: int | None = None,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    op_id: uuid.UUID | None = None,
) -> CommandOutcome:
    return CommandOutcome(
        op_id=op_id or uuid.uuid4(),
        server_id=SERVER_A,
        display=DISPLAY_LABEL,
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stdout_bytes=len(stdout.encode("utf-8"))
        if stdout_bytes is None
        else stdout_bytes,
        stdout_truncated=stdout_truncated,
        stderr=stderr,
        stderr_bytes=len(stderr.encode("utf-8"))
        if stderr_bytes is None
        else stderr_bytes,
        stderr_truncated=stderr_truncated,
        timed_out=False,
        cwd_change_failed=False,
        duration_seconds=0.01,
    )


class _Harness:
    """A capability wired to a scratch namespace and a scripted transport."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.transport = _ScriptedTransport([])
        monkeypatch.setattr(logs_module, "execute_command", self.transport)
        self.scratch = tmp_path
        self.key_file = tmp_path / "fixture_key"
        self.key_file.write_text("not-a-real-key\n", encoding="utf-8")
        self.known_hosts = tmp_path / "known_hosts"
        self.known_hosts.write_text("pin\n", encoding="utf-8")
        self.artifacts = tmp_path / "artifacts"
        self.profile = _profile()
        self.canary = "mai-canary-hunter2"
        self.capability = HostLogsCapability(
            profiles=lambda server_id: self.profile,
            known_hosts=lambda server_id: self.known_hosts,
            artifacts_root=self.artifacts,
        )

    def call(
        self,
        payload: object = GOOD_PAYLOAD,
        *,
        credentials: dict[str, object] | None = None,
        context: ExecutionContext | None = None,
    ) -> CapabilityCall:
        resolved: dict[str, object] = {
            FIXTURE_CREDENTIAL_REF: _secret(str(self.key_file))
        }
        if credentials is not None:
            resolved = credentials  # type: ignore[assignment]
        return _call(payload, context=context, credentials=resolved)

    def run(self, **kwargs: object) -> ExecutorResult:
        return asyncio.run(self.capability(self.call(**kwargs)))  # type: ignore[arg-type]

    def artifact_files(self) -> list[Path]:
        if not self.artifacts.is_dir():
            return []
        return sorted(self.artifacts.rglob("*"))


class TestPreEffectGuards:
    """Every wiring/context failure refuses BEFORE the transport lane."""

    def _harness(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Harness:
        return _Harness(monkeypatch, tmp_path)

    def test_missing_profile_refuses_without_transport(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)

        def _missing(server_id: uuid.UUID) -> ServerProfile:
            raise KeyError(server_id)

        harness.profile = _missing  # type: ignore[assignment]
        with pytest.raises(LogsError) as excinfo:
            harness.run()
        assert excinfo.value.code is LogsErrorCode.PROFILE_UNAVAILABLE
        assert harness.transport.calls == []
        assert harness.artifact_files() == []

    def test_non_profile_answer_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        harness.profile = {"not": "a profile"}  # type: ignore[assignment]
        with pytest.raises(LogsError) as excinfo:
            harness.run()
        assert excinfo.value.code is LogsErrorCode.PROFILE_UNAVAILABLE
        assert harness.transport.calls == []

    def test_profile_identity_mismatch_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        harness.profile = _profile(server_id=uuid.uuid4())
        with pytest.raises(LogsError) as excinfo:
            harness.run()
        assert excinfo.value.code is LogsErrorCode.PROFILE_MISMATCH
        assert harness.transport.calls == []

    def test_missing_credential_reference_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        with pytest.raises(LogsError) as excinfo:
            harness.run(credentials={})
        assert excinfo.value.code is LogsErrorCode.CREDENTIAL_UNAVAILABLE
        assert FIXTURE_CREDENTIAL_REF in excinfo.value.detail
        assert harness.transport.calls == []

    def test_non_secret_credential_value_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        with pytest.raises(LogsError) as excinfo:
            harness.run(
                credentials={FIXTURE_CREDENTIAL_REF: "/tmp/not-a-secret-object"}
            )
        assert excinfo.value.code is LogsErrorCode.CREDENTIAL_UNAVAILABLE
        assert harness.transport.calls == []

    def test_unresolvable_key_file_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        with pytest.raises(LogsError) as excinfo:
            harness.run(
                credentials={FIXTURE_CREDENTIAL_REF: _secret(str(tmp_path / "gone"))}
            )
        assert excinfo.value.code is LogsErrorCode.KEY_FILE_UNAVAILABLE
        assert harness.transport.calls == []

    def test_unenrolled_known_hosts_refuse(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        harness.known_hosts = tmp_path / "absent"  # type: ignore[assignment]
        with pytest.raises(LogsError) as excinfo:
            harness.run()
        assert excinfo.value.code is LogsErrorCode.HOST_KEY_UNENROLLED
        assert harness.transport.calls == []
        assert harness.artifact_files() == []

    def test_unsupported_source_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        with pytest.raises(LogsError) as excinfo:
            harness.run(payload={"source": "file", "service": "/etc/shadow"})
        assert excinfo.value.code is LogsErrorCode.INVALID_REQUEST
        assert harness.transport.calls == []
        assert harness.artifact_files() == []

    def test_path_like_service_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        with pytest.raises(LogsError) as excinfo:
            harness.run(payload={"source": "journal", "service": "../../etc/passwd"})
        assert excinfo.value.code is LogsErrorCode.INVALID_REQUEST
        assert harness.transport.calls == []

    def test_non_call_input_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        with pytest.raises(LogsError) as excinfo:
            asyncio.run(harness.capability({"source": "journal"}))  # type: ignore[arg-type]
        assert excinfo.value.code is LogsErrorCode.INVALID_CONTEXT
        assert harness.transport.calls == []


class TestReadComposition:
    """Document assembly, artifact persistence and bounds over the
    scripted transport."""

    def test_succeeded_call_yields_bounded_evidence_document(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.transport.script = [_outcome(stdout="oct 14 sshd[1]: started\n")]
        result = harness.run()
        assert isinstance(result, ExecutorResult)
        assert result.ok is True
        document = json.loads(result.output)
        assert document["capability"] == CAPABILITY_ID
        assert document["status"] == "succeeded"
        assert document["exit_code"] == 0
        assert document["source"] == "journal"
        assert document["service"] == "sshd.service"
        assert document["since"] is None and document["until"] is None
        assert document["lines_requested"] == DEFAULT_LOG_LINES
        assert document["output_budget_bytes"] == DEFAULT_OUTPUT_BUDGET_BYTES
        assert document["timeout_seconds"] == DEFAULT_TIMEOUT_SECONDS
        assert document["stdout_truncated"] is False
        assert document["stderr_bytes"] == 0
        assert document["secrets_redacted"] is False
        assert document["excerpt"] == "oct 14 sshd[1]: started\n"
        assert document["excerpt_truncated"] is False
        assert document["notes"] == []
        # the composed request is structured argv from THIS payload
        (identity, request, key_path, known_hosts) = harness.transport.calls[0]
        query = log_query_from_payload(GOOD_PAYLOAD)
        assert request.argv == compose_log_argv(query)
        assert request.display == DISPLAY_LABEL
        assert request.op_id == uuid.UUID(document["op_id"])
        assert request.server_id == SERVER_A
        assert request.working_directory is None
        assert key_path == str(harness.key_file)
        assert known_hosts == str(harness.known_hosts)

    def test_artifact_and_provenance_sidecar_are_written(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.transport.script = [
            _outcome(stdout="line-1\nline-2\n", stderr="warn: disk\n")
        ]
        result = harness.run()
        document = json.loads(result.output)
        # scoped layout: <root>/<server_id>/<op_id>.log + .json
        artifact_path = Path(document["artifact"]["path"])
        assert artifact_path == (
            harness.artifacts / str(SERVER_A) / f"{document['op_id']}.log"
        )
        content = artifact_path.read_bytes()
        assert content == b"line-1\nline-2\n\n--- stderr ---\nwarn: disk\n"
        assert document["artifact"]["bytes"] == len(content)
        assert hashlib.sha256(content).hexdigest() == document["artifact"]["sha256"]
        sidecar = json.loads(Path(document["artifact"]["provenance"]).read_text())
        assert sidecar["capability"] == CAPABILITY_ID
        assert sidecar["op_id"] == document["op_id"]
        assert sidecar["server_id"] == str(SERVER_A)
        assert sidecar["run_id"] == str(RUN_1)
        assert sidecar["principal"] == PRINCIPAL
        assert sidecar["source"] == "journal"
        assert sidecar["service"] == "sshd.service"
        assert sidecar["status"] == "succeeded"
        assert sidecar["stdout_bytes"] == 14
        assert sidecar["stderr_bytes"] == 11
        assert sidecar["secrets_redacted"] is False
        assert sidecar["artifact_bytes"] == len(content)
        # restrictive evidence permissions
        assert (os.stat(artifact_path).st_mode & 0o777) == 0o600
        assert (os.stat(artifact_path.parent).st_mode & 0o777) == 0o700

    def test_excerpt_is_the_tail_of_large_stdout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        body = "".join(f"line-{n:05d}\n" for n in range(2000))  # ~20 KB
        harness.transport.script = [_outcome(stdout=body)]
        document = json.loads(harness.run().output)
        assert document["excerpt_truncated"] is True
        assert document["excerpt_bytes"] == MAX_EXCERPT_BYTES
        assert document["excerpt"].endswith("line-01999\n")
        assert not document["excerpt"].startswith("line-00000")
        # the artifact still holds the complete retained body
        artifact_path = Path(document["artifact"]["path"])
        assert artifact_path.read_text() == body

    def test_excerpt_falls_back_to_stderr_diagnostics(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.transport.script = [
            _outcome(
                status=CommandStatus.FAILED,
                exit_code=1,
                stderr="Failed to query journal: permission denied\n",
            )
        ]
        result = harness.run()
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "failed"
        assert document["exit_code"] == 1
        assert "permission denied" in document["excerpt"]
        assert any("stderr" in note for note in document["notes"])

    def test_transport_truncation_is_reported_not_hidden(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        retained = "a" * 4096
        harness.transport.script = [
            _outcome(
                stdout=retained,
                stdout_bytes=50_000,
                stdout_truncated=True,
            )
        ]
        document = json.loads(harness.run().output)
        assert document["stdout_bytes"] == 50_000
        assert document["stdout_truncated"] is True
        artifact_path = Path(document["artifact"]["path"])
        assert artifact_path.stat().st_size == 4096
        assert any("head window" in note for note in document["notes"])

    def test_resolved_secrets_are_redacted_everywhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        canary = "s3cr3t-canary-value"
        harness.transport.script = [
            _outcome(
                stdout=f"token={canary} on line one\nline two\n",
                stderr=f"key at {harness.key_file} leaked\n",
            )
        ]
        result = harness.run(
            credentials={
                FIXTURE_CREDENTIAL_REF: _secret(str(harness.key_file)),
                CANARY_CREDENTIAL_REF: _secret(canary),
            },
        )
        document = json.loads(result.output)
        assert document["secrets_redacted"] is True
        rendered = result.output
        artifact = Path(document["artifact"]["path"]).read_text()
        sidecar = json.loads(Path(document["artifact"]["provenance"]).read_text())
        # the token never appears where log content lives
        for where in (rendered, artifact):
            assert canary not in where
            assert str(harness.key_file) not in where
            assert REDACTED in where
        # the sidecar is metadata-only: it records THAT filtering happened
        assert sidecar["secrets_redacted"] is True
        assert canary not in json.dumps(sidecar)
        assert str(harness.key_file) not in json.dumps(sidecar)
        assert f"token={REDACTED}" in document["excerpt"]
        assert f"key at {REDACTED}" in artifact

    def test_outcome_unknown_still_persists_evidence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.transport.script = [
            _outcome(
                status=CommandStatus.OUTCOME_UNKNOWN,
                exit_code=None,
                stdout="partial\n",
            )
        ]
        result = harness.run()
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "outcome_unknown"
        assert document["exit_code"] is None
        assert Path(document["artifact"]["path"]).read_text() == "partial\n"

    def test_empty_output_is_an_honest_empty_artifact(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.transport.script = [_outcome()]
        document = json.loads(harness.run().output)
        assert document["excerpt"] == ""
        assert document["excerpt_truncated"] is False
        assert Path(document["artifact"]["path"]).stat().st_size == 0
        assert document["artifact"]["bytes"] == 0

    def test_artifact_failure_fails_the_call_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.transport.script = [_outcome(stdout="big body\n" * 1000)]

        def _broken(*args: object, **kwargs: object) -> ArtifactReference:
            raise OSError("disk full")

        monkeypatch.setattr(logs_module, "write_evidence_artifact", _broken)
        with pytest.raises(LogsError) as excinfo:
            harness.run()
        assert excinfo.value.code is LogsErrorCode.ARTIFACT_UNAVAILABLE
        # no unbacked log content escaped the executor
        assert harness.transport.calls != []


# ---------------------------------------------------------------------------
# Dispatcher integration: deny outcomes never reach this executor; the
# read class refuses a PRESENTED approval (lazy approval store — no
# database needed for these paths)
# ---------------------------------------------------------------------------


def _lazy_approvals() -> ApprovalStore:
    _, sessions = create_app_engine("postgresql://unused:unused@127.0.0.1:1/unused")
    return ApprovalStore(sessions)


def _deny_dispatcher(
    tmp_path: Path,
    *,
    grants: object = None,
    policy: Policy | None = None,
    adopted: bool = True,
) -> tuple[CapabilityDispatcher, HostLogsCapability]:
    """The REAL descriptor + REAL capability object behind the dispatcher.

    The capability's providers raise AssertionError if invoked — the
    executor running AT ALL on a deny path fails the test.
    """

    def _boom(server_id: uuid.UUID) -> ServerProfile:
        raise AssertionError("executor invoked on a deny path")

    capability = HostLogsCapability(
        profiles=_boom,
        known_hosts=_boom,
        artifacts_root=str(tmp_path / "artifacts"),
    )
    dispatcher = CapabilityDispatcher(
        registry=_registry(),
        approvals=_lazy_approvals(),
        grants=(lambda: _grants()) if grants is None else (lambda: grants),  # type: ignore[arg-type,return-value]
        policy=(lambda: _policy()) if policy is None else (lambda: policy),
        adopted=lambda server_id: adopted,
        credentials=lambda: EnvResolver(tmp_path / "absent.env"),
        executor=capability,
    )
    return dispatcher, capability


class TestDeniedCallsNeverReachTheExecutor:
    @pytest.mark.anyio
    async def test_ungranted_capability_denies_without_effect(
        self, tmp_path: Path
    ) -> None:
        dispatcher, _capability = _deny_dispatcher(tmp_path, grants={})
        outcome = await dispatcher.dispatch(
            context=_context(),
            capability_id=CAPABILITY_ID,
            payload=GOOD_PAYLOAD,
        )
        assert outcome.allowed is False
        assert outcome.reason == DecisionReason.CAPABILITY_NOT_GRANTED.value
        assert outcome.effect == "not_attempted"
        assert outcome.approval_spent is False
        assert outcome.executor_ok is None

    @pytest.mark.anyio
    async def test_policy_deny_precedes_everything(self, tmp_path: Path) -> None:
        dispatcher, _capability = _deny_dispatcher(
            tmp_path, policy=_policy(denied=(CAPABILITY_ID,))
        )
        outcome = await dispatcher.dispatch(
            context=_context(),
            capability_id=CAPABILITY_ID,
            payload=GOOD_PAYLOAD,
        )
        assert outcome.reason == DecisionReason.CAPABILITY_DENIED_BY_POLICY.value
        assert outcome.effect == "not_attempted"

    @pytest.mark.anyio
    async def test_unadopted_server_denies_without_effect(self, tmp_path: Path) -> None:
        dispatcher, _capability = _deny_dispatcher(tmp_path, adopted=False)
        outcome = await dispatcher.dispatch(
            context=_context(),
            capability_id=CAPABILITY_ID,
            payload=GOOD_PAYLOAD,
        )
        assert outcome.reason == DecisionReason.SERVER_NOT_ADOPTED.value
        assert outcome.effect == "not_attempted"

    @pytest.mark.anyio
    async def test_out_of_scope_server_id_denies_without_effect(
        self, tmp_path: Path
    ) -> None:
        dispatcher, _capability = _deny_dispatcher(tmp_path)
        outcome = await dispatcher.dispatch(
            context=_context(server_id=uuid.uuid4()),
            capability_id=CAPABILITY_ID,
            payload=GOOD_PAYLOAD,
        )
        assert outcome.reason == DecisionReason.SERVER_SCOPE_MISMATCH.value
        assert outcome.effect == "not_attempted"

    @pytest.mark.anyio
    async def test_read_class_refuses_a_presented_approval(
        self, tmp_path: Path
    ) -> None:
        """Reads need no approval decision — presenting one is a
        protocol violation the dispatcher refuses before any effect."""
        dispatcher, _capability = _deny_dispatcher(tmp_path)
        outcome = await dispatcher.dispatch(
            context=_context(),
            capability_id=CAPABILITY_ID,
            payload=GOOD_PAYLOAD,
            approval_id=uuid.uuid4(),
        )
        assert outcome.allowed is False
        assert outcome.reason == DispatchReason.APPROVAL_PRESENTED_BUT_NOT_REQUIRED
        assert outcome.effect == "not_attempted"
        assert outcome.approval_spent is False

    @pytest.mark.anyio
    async def test_allowed_read_reaches_the_executor_without_approval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole seam for the happy path: grant + adoption + policy
        allow a read with NO approval decision and no consumed decision."""
        harness = _Harness(monkeypatch, tmp_path)
        harness.transport.script = [_outcome(stdout="sentinel\n")]
        env_file = tmp_path / "fixture.env"
        env_file.write_text(f"{FIXTURE_CREDENTIAL_REF}={harness.key_file}\n")
        os.chmod(env_file, 0o600)
        dispatcher = CapabilityDispatcher(
            registry=_registry(),
            approvals=_lazy_approvals(),
            grants=lambda: _grants(),
            policy=lambda: _policy(),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(env_file),
            executor=harness.capability,
        )
        outcome = await dispatcher.dispatch(
            context=_context(references=(FIXTURE_CREDENTIAL_REF,)),
            capability_id=CAPABILITY_ID,
            payload=GOOD_PAYLOAD,
            approval_id=None,
        )
        assert outcome.allowed is True
        assert outcome.effect == "executed"
        assert outcome.executor_ok is True
        assert outcome.approval_id is None
        assert outcome.approval_spent is False
        document = json.loads(outcome.output.text)
        assert document["status"] == "succeeded"


# ---------------------------------------------------------------------------
# Real boundary: disposable Arch targets, real pinned-SSH transport
# (the feature-gate scenario halves this card owns)
# ---------------------------------------------------------------------------


class _LogsPlainFixture(_ArchSshdFixture):
    """Plain sshd Arch target in THIS card's namespace.

    ``identity()``/``transport()`` are overridden because the inherited
    helpers hardcode the SUPERVISOR card's credential reference name;
    this card's profile/context/env resolve its OWN reference name (plus
    a canary reference used by the redaction halves).
    """

    container = FIXTURE_PLAIN_CONTAINER

    def __init__(self) -> None:
        self.scratch = FIXTURE_SCRATCH / uuid.uuid4().hex
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.key_path = self.scratch / "fixture_key"
        self.canary = f"mai-canary-{uuid.uuid4().hex}"
        self.port = 0
        self.host_key_fingerprint = ""
        self.cleanup_proof = "<not cleaned>"

    def identity(self) -> SshIdentity:
        return SshIdentity(
            host="127.0.0.1",
            port=self.port,
            user=FIXTURE_USER,
            host_key=self.host_key_fingerprint,
            credential_ref=FIXTURE_CREDENTIAL_REF,
        )

    def transport(self) -> tuple[Path, Path]:
        env_file = self.scratch / "fixture.env"
        env_file.write_text(
            f"{FIXTURE_CREDENTIAL_REF}={self.key_path}\n"
            f"{CANARY_CREDENTIAL_REF}={self.canary}\n",
            encoding="utf-8",
        )
        os.chmod(env_file, 0o600)
        key_path = resolve_identity_key(EnvResolver(env_file), FIXTURE_CREDENTIAL_REF)
        return key_path, self.scratch / "known_hosts"

    def ensure_image(self) -> None:
        _ensure_fixture_image()  # same recipe builds the base image

    def cleanup(self) -> str:
        proof = super().cleanup()
        strays = [
            name
            for name in _docker("ps", "-a", "--format", "{{.Names}}").stdout.split()
            if name.startswith("mai-fxt-04-logs")
        ]
        return f"{proof}; logs-fixture strays: {strays or 'none'}"


class _LogsSystemdFixture(_LogsPlainFixture, _SystemdArchFixture):
    """systemd-as-PID-1 Arch target (real journald) in THIS card's
    namespace; the fixture user is added to ``systemd-journal`` so the
    capability's own user can read system units — mirroring an enrolled
    target's deployment grant."""

    container = FIXTURE_SYSTEMD_CONTAINER

    def cleanup(self) -> str:
        if getattr(self, "_sudo_ready", False):
            try:
                self.remove_sudo_grant()
            except AssertionError:
                pass  # container already gone — nothing to revoke
        return super().cleanup()

    def _provision(self) -> None:
        super()._provision()  # creates the fixture user (sibling recipe)
        group = _docker(
            "exec",
            self.container,
            "sh",
            "-c",
            f"usermod -aG systemd-journal {FIXTURE_USER}",
        )
        assert group.returncode == 0, group.stderr[-400:]

    def run_system_unit(self, unit: str, script: str) -> None:
        """Start one transient SYSTEM unit (root via the fixture's
        NOPASSWD /bin/sh sudoers rule) and wait until its output reached
        the journal. The sudo grant is card-session scoped: written on
        first use, removed again at fixture cleanup."""
        if not getattr(self, "_sudo_ready", False):
            self.write_sudo_grant()
            self._sudo_ready = True
        service = f"{unit}.service"
        inner = f"systemd-run --unit={unit} --collect /bin/sh -c {shlex.quote(script)}"
        run = self.ssh(f"sudo /bin/sh -c {shlex.quote(inner)}")
        assert run[0] == 0, f"systemd-run failed: {run[2][-400:]}"
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            _rc, out, _err = self.ssh(
                f"journalctl -u {shlex.quote(service)} -n 1 --quiet"
            )
            if out.strip():
                return
            time.sleep(0.5)
        raise AssertionError(f"unit {service} never reached the journal")


class _RealComposition:
    """The real composed stack: dispatcher → capability → bounded lane
    → disposable target, with controller-side evidence artifacts."""

    def __init__(
        self,
        fixture: _LogsPlainFixture,
        *,
        grants: dict[str, object] | None = None,
        policy: Policy | None = None,
    ) -> None:
        key_path, known_hosts = fixture.transport()
        self.fixture = fixture
        self.key_path = key_path
        self.profile = _profile(fixture.identity())
        self.artifacts = fixture.scratch / "artifacts"
        self.capability = HostLogsCapability(
            profiles=lambda server_id: self.profile,
            known_hosts=lambda server_id: known_hosts,
            artifacts_root=self.artifacts,
            default_timeout=30.0,
        )
        self.dispatcher = CapabilityDispatcher(
            registry=_registry(),
            approvals=_lazy_approvals(),
            grants=(lambda: _grants()) if grants is None else (lambda: grants),  # type: ignore[arg-type,return-value]
            policy=(lambda: _policy()) if policy is None else (lambda: policy),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(fixture.scratch / "fixture.env"),
            executor=self.capability,
        )

    async def dispatch(self, payload: dict):  # type: ignore[no-untyped-def]
        return await self.dispatcher.dispatch(
            context=_context(
                references=(FIXTURE_CREDENTIAL_REF, CANARY_CREDENTIAL_REF)
            ),
            capability_id=CAPABILITY_ID,
            payload=payload,
        )

    def artifact_files(self) -> set[str]:
        if not self.artifacts.is_dir():
            return set()
        return {str(path) for path in self.artifacts.rglob("*") if path.is_file()}


def _class_fixture(fixture_cls: type):  # type: ignore[no-untyped-def]
    if not _docker_available():
        pytest.skip("docker unavailable: Arch logs fixture tests require it")
    fixture = fixture_cls()
    try:
        fixture.ensure_image()
        fixture.start()
        yield fixture
    finally:
        print(f"\nFIXTURE CLEANUP PROOF: {fixture.cleanup()}")


@pytest.fixture(scope="class")
def logs_arch() -> _LogsPlainFixture:
    yield from _class_fixture(_LogsPlainFixture)


@pytest.fixture(scope="class")
def logs_sysd() -> _LogsSystemdFixture:
    yield from _class_fixture(_LogsSystemdFixture)


DOCKER_SHIM = (
    """\
#!/bin/sh
# mai-04-log-capability fixture shim: the exact `docker logs` argument
# surface the host.logs capability composes. E:-prefixed lines are
# emitted on stderr (docker splits container stderr the same way).
: > /tmp/mai-logs-shim-invoked
[ "$1" = "logs" ] || { echo "shim: unsupported subcommand $1" >&2; exit 64; }
shift
since=""; until=""; tail_n=""; stamp=0; name=""
while [ $# -gt 0 ]; do
  case "$1" in
    --since) shift 2 ;;
    --until) shift 2 ;;
    --tail) shift; tail_n="$1"; shift ;;
    --timestamps) stamp=1; shift ;;
    *) name="$1"; shift ;;
  esac
done
[ -n "$name" ] || { echo "Error from daemon: no container name" >&2; exit 1; }
file="/home/"""
    + FIXTURE_USER
    + """/logs-fixture-$name.log"
[ -f "$file" ] || { echo "Error from daemon: No such container: $name" >&2; exit 1; }
[ -n "$tail_n" ] || tail_n=10000000
tail -n "$tail_n" "$file" | while IFS= read -r line; do
  case "$line" in
    E:*) printf '%s\\n' "$line" >&2 ;;
    *)
      if [ "$stamp" -eq 1 ]; then
        printf '%s %s\\n' "2026-10-14T12:00:00.000000000Z" "$line"
      else
        printf '%s\\n' "$line"
      fi
      ;;
  esac
done
"""
)


def _provision_docker_shim(fixture: _LogsPlainFixture) -> None:
    install = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            fixture.container,
            "sh",
            "-c",
            "cat > /usr/local/bin/docker && chmod +x /usr/local/bin/docker",
        ],
        input=DOCKER_SHIM,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert install.returncode == 0, install.stderr[-400:]
    _rc, resolved, _err = fixture.ssh("command -v docker")
    assert resolved.strip() == "/usr/local/bin/docker", (
        f"shim not on the target's PATH: {resolved!r} {_err!r}"
    )


class TestAgainstLogsArch:
    """Feature-gate scenario halves owned by this card (P04 gate)."""

    # -- denial halves first: shim-invocation absence is only meaningful
    #    before any allowed call has run the shim.

    @pytest.mark.anyio
    async def test_unsupported_source_denied_with_no_side_effects(
        self, logs_arch: _LogsPlainFixture
    ) -> None:
        """THE path negative control on the real stack: a file-like
        request is refused pre-transport — no artifact, no target
        invocation, honest unproven-effect receipt."""
        world = _RealComposition(logs_arch)
        outcome = await world.dispatch({"source": "file", "service": "/etc/shadow"})
        assert outcome.allowed is False
        assert outcome.reason == DispatchReason.EXECUTOR_FAILED
        assert outcome.effect == "unknown"
        assert outcome.error_type == "LogsError"
        assert world.artifact_files() == set()
        _rc, marker, _err = logs_arch.ssh(
            "test -e /tmp/mai-logs-shim-invoked && echo Y || echo N"
        )
        assert marker.strip() == "N"

    @pytest.mark.anyio
    async def test_ungranted_capability_denied_before_transport(
        self, logs_arch: _LogsPlainFixture
    ) -> None:
        world = _RealComposition(logs_arch, grants={})
        outcome = await world.dispatch(GOOD_PAYLOAD)
        assert outcome.allowed is False
        assert outcome.reason == DecisionReason.CAPABILITY_NOT_GRANTED.value
        assert outcome.effect == "not_attempted"
        assert world.artifact_files() == set()
        _rc, marker, _err = logs_arch.ssh(
            "test -e /tmp/mai-logs-shim-invoked && echo Y || echo N"
        )
        assert marker.strip() == "N"

    # -- container-source half (real transport, faithful shim producer)

    @pytest.mark.anyio
    async def test_container_logs_bounded_redacted_through_real_transport(
        self, logs_arch: _LogsPlainFixture
    ) -> None:
        _provision_docker_shim(logs_arch)
        sentinel = f"mai-logs-sentinel-{uuid.uuid4().hex[:12]}"
        log_file = f"/home/{FIXTURE_USER}/logs-fixture-mai-app.log"
        provision = (
            f"seq 1 6000 | sed 's/^/line-/' > {shlex.quote(log_file)}; "
            f"sed -i '1010s/.*/{sentinel}/' {shlex.quote(log_file)}; "
            f"sed -i '1011s/.*/token={logs_arch.canary}/' {shlex.quote(log_file)}; "
            f"sed -i '5990s/.*/E: warn-5990/' {shlex.quote(log_file)}; "
            f"sed -i '5995s/.*/E: warn-5995/' {shlex.quote(log_file)}"
        )
        rc, out, err = logs_arch.ssh(provision)
        assert rc == 0, err[-400:]
        world = _RealComposition(logs_arch)
        outcome = await world.dispatch(
            {
                "source": "container",
                "service": "mai-app",
                "since": "2026-10-14T00:00:00Z",
                "lines": 5000,
                "output_budget": 8192,
            }
        )
        assert outcome.allowed is True
        assert outcome.effect == "executed"
        assert outcome.executor_ok is True
        document = json.loads(outcome.output.text)
        assert document["source"] == "container"
        assert document["service"] == "mai-app"
        assert document["since"] == "2026-10-14T00:00:00Z"
        assert document["status"] == "succeeded"
        # the composed argv REALLY ran on the target through ssh
        _rc, marker, _err = logs_arch.ssh("cat /tmp/mai-logs-shim-invoked && echo Y")
        assert marker.strip().endswith("Y")
        # bounded, honest totals: 5000 tail lines ≫ the 8192-byte budget
        assert document["stdout_bytes"] > 8192
        assert document["stdout_truncated"] is True
        assert any("head window" in note for note in document["notes"])
        artifact_path = Path(document["artifact"]["path"])
        content = artifact_path.read_bytes()
        assert (
            len(content) <= 8192 + len(b"\n--- stderr ---\n") + document["stderr_bytes"]
        )
        assert document["stderr_bytes"] > 0  # E: lines rode the stderr stream
        # scoped artifact + provenance under THIS server's namespace
        assert str(SERVER_A) in str(artifact_path)
        sidecar = json.loads(Path(document["artifact"]["provenance"]).read_text())
        assert sidecar["source"] == "container"
        assert sidecar["service"] == "mai-app"
        assert sidecar["lines_requested"] == 5000
        assert sidecar["output_budget_bytes"] == 8192
        assert sidecar["secrets_redacted"] is True
        # sentinel evidence present, canary secret redacted everywhere
        assert sentinel in content.decode("utf-8", errors="replace")
        rendered = json.dumps(outcome.to_json())
        for where in (
            rendered,
            content.decode("utf-8", errors="replace"),
            document["excerpt"],
        ):
            assert logs_arch.canary not in where
            assert REDACTED in where
        assert str(world.key_path) not in rendered

    # -- journal halves (real journald on the systemd target)

    @pytest.mark.anyio
    async def test_journal_sentinel_bounded_and_canary_redacted(
        self, logs_sysd: _LogsSystemdFixture
    ) -> None:
        world = _RealComposition(logs_sysd)
        unit = f"mai-logs-{uuid.uuid4().hex[:12]}"
        sentinel = f"mai-logs-sentinel-{uuid.uuid4().hex[:12]}"
        logs_sysd.run_system_unit(
            unit,
            f"echo token={logs_sysd.canary}; echo {sentinel}",
        )
        since = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        outcome = await world.dispatch(
            {
                "source": "journal",
                "service": f"{unit}.service",
                "since": since,
                "lines": 200,
            }
        )
        assert outcome.allowed is True
        assert outcome.effect == "executed"
        assert outcome.executor_ok is True
        document = json.loads(outcome.output.text)
        assert document["source"] == "journal"
        assert document["service"] == f"{unit}.service"
        assert document["since"] == since
        assert document["status"] == "succeeded"
        assert document["exit_code"] == 0
        assert document["stdout_truncated"] is False
        assert sentinel in document["excerpt"]
        assert document["secrets_redacted"] is True
        artifact_path = Path(document["artifact"]["path"])
        content = artifact_path.read_text()
        assert sentinel in content
        rendered = json.dumps(outcome.to_json())
        for where in (rendered, content, document["excerpt"]):
            assert logs_sysd.canary not in where
            assert REDACTED in where
        sidecar = json.loads(Path(document["artifact"]["provenance"]).read_text())
        assert sidecar["run_id"] == str(RUN_1)
        assert sidecar["principal"] == PRINCIPAL
        assert sidecar["stdout_bytes"] == document["stdout_bytes"]

    @pytest.mark.anyio
    async def test_journal_large_output_bounded_by_byte_budget(
        self, logs_sysd: _LogsSystemdFixture
    ) -> None:
        world = _RealComposition(logs_sysd)
        unit = f"mai-logs-{uuid.uuid4().hex[:12]}"
        logs_sysd.run_system_unit(unit, "seq 1 3000")
        outcome = await world.dispatch(
            {
                "source": "journal",
                "service": f"{unit}.service",
                "lines": 5000,
                "output_budget": 4096,
            }
        )
        assert outcome.allowed is True
        assert outcome.executor_ok is True
        document = json.loads(outcome.output.text)
        assert document["status"] == "succeeded"
        assert document["lines_requested"] == 5000
        assert document["output_budget_bytes"] == 4096
        # true total honestly reported; retention bounded at the budget
        assert document["stdout_bytes"] > 10_000
        assert document["stdout_truncated"] is True
        artifact_path = Path(document["artifact"]["path"])
        assert artifact_path.stat().st_size == 4096
        assert any("head window" in note for note in document["notes"])
        sidecar = json.loads(Path(document["artifact"]["provenance"]).read_text())
        assert sidecar["stdout_bytes"] == document["stdout_bytes"]
        assert sidecar["stdout_truncated"] is True
        # the never-unbounded-model-context claim: the receipt itself is small
        assert len(outcome.output.text) < 16_384
