"""Focused tests for the raw-shell host capability.

Card ``04-shell-capability`` against contracts.md **C03** (rules 2, 4, 5,
7) and **C04** (rules 2, 3, 5). Layers, mirroring the sibling cards:

- pure descriptor/decision pins (the raw-shell opt-in is VISIBLE: the
  privileged effect class routes every call through an approval decision
  in both modes and carries the OS-containment forfeiture advisory);
- pure payload→``CommandRequest`` mapping and constructor wiring checks;
- executor pre-effect guards against a scripted supervisor seam (NOTE on
  the double: it emulates the lane's surrounding contract — typed request
  validation, handle/report vocabulary — deliberately NOT any invariant
  under test here; status honesty itself is owned by the supervisor and
  reconcile cards);
- dispatcher-integration negative controls: deny outcomes can never reach
  this executor (lazy approval store, no database needed for denies);
- real-boundary feature-gate scenario on the disposable Arch sshd fixture
  (same image/fixture classes as ``test_supervisor.py`` — imported, not
  reimplemented — under THIS card's unique container namespace) with the
  REAL dispatcher, REAL ``ApprovalStore`` over disposable Postgres, REAL
  ``EnvResolver`` and the REAL supervisor lane: an allowed call writes a
  target sentinel through the wrapper; denied/revoked calls leave the
  target untouched (no sentinel, no receipt directory, decision not
  spent). Auto-skips when docker or ``MAI_TEST_DATABASE_URL`` is absent.

Acceptance mapping: every command carries explicit target/context through
the one dispatcher seam (payload names no target) and raw shell is a
visibly broad opt-in (privileged class); unapproved/revoked calls cannot
reach SSH, and safe results carry outcome AND evidence — status, exit
code, bounded output, receipt directory, intent path, notes — not only an
exit code.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shlex
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

import mai.capabilities.host_shell as host_shell_module
from mai.capabilities.dispatch import (
    CapabilityCall,
    CapabilityDispatcher,
    DispatchReason,
    ExecutorResult,
    canonical_payload_hash,
)
from mai.capabilities.host_shell import (
    CAPABILITY_ID,
    DEFAULT_OUTPUT_BUDGET_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    DISPLAY_LABEL,
    MAX_SETTLE_SECONDS,
    HostShellCapability,
    HostShellError,
    HostShellErrorCode,
    descriptor,
    request_from_payload,
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
    MAX_OUTPUT_BUDGET_BYTES,
    CommandRequest,
    CommandStatus,
)
from mai.execution.ssh import resolve_identity_key
from mai.execution.supervisor import (
    SupervisionHandle,
    SupervisionIntent,
    SupervisionReport,
    SupervisionScheme,
)
from mai.plugins.registry import CapabilityRegistry
from mai.policy.approvals import ApprovalRequest, ApprovalStore
from mai.policy.evaluate import (
    PRIVILEGED_FORFEITS_OS_CONTAINMENT,
    DecisionReason,
    ExecutionContext,
    Policy,
    PolicyMode,
    evaluate,
)

from test_supervisor import (  # sibling-card fixture, imported not copied
    _ArchSshdFixture,
    _docker,
    _docker_available,
    _ensure_fixture_image,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# C10 rule 2 fixture namespace: key files, known-hosts, intents, sentinels.
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-04-shell-tests")
FIXTURE_CONTAINER = "mai-fxt-04-shell-arch"
FIXTURE_IMAGE_TAG = "mai-fxt-04-shell-arch:sshd"
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_04_SHELL_FIXTURE"
FIXTURE_USER = "mai-fxt"
RECEIPT_ROOT = f"/home/{FIXTURE_USER}/mai-receipts"

SERVER_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
RUN_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
PRINCIPAL = "admin"
POLICY_REVISION = 7

GOOD_PAYLOAD = {"command": "echo shell-capability-fixture"}


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
# Descriptor and decision visibility (acceptance 1: broad opt-in is visible)
# ---------------------------------------------------------------------------


class TestDescriptor:
    def test_descriptor_is_registry_valid_and_frozen_shape(self) -> None:
        document = descriptor().to_json()
        assert document["identifier"] == CAPABILITY_ID == "host.shell"
        assert document["version"] == "1.0.0"
        assert document["effect_class"] == "privileged"
        assert document["execution_environments"] == ["linux_ssh"]
        assert document["required_grants"] == [CAPABILITY_ID]
        assert document["resource_scope"] == ["target.exec"]
        properties = document["input_schema"]["properties"]
        assert set(properties) == {
            "command",
            "timeout",
            "output_budget",
            "working_directory",
        }
        assert document["input_schema"]["required"] == ["command"]
        # the descriptor survives the registry's own validation path
        registry = _registry()
        assert registry.versions(CAPABILITY_ID) == ("1.0.0",)

    def test_raw_shell_requires_approval_in_ask_mode_and_allows_delegated_with_advisory(
        self,
    ) -> None:
        for mode, expected in (
            (PolicyMode.ASK, True),
            (PolicyMode.DELEGATED, False),
        ):
            decision = evaluate(
                context=_context(mode=mode),
                capability_id=CAPABILITY_ID,
                descriptor=descriptor(),
                grants=_grants(),  # type: ignore[arg-type]
                policy=_policy(),
                adopted=True,
            )
            assert decision.allowed is True
            assert decision.requires_approval is expected, mode
            assert decision.effect_class == "privileged"
            assert decision.advisories == (PRIVILEGED_FORFEITS_OS_CONTAINMENT,)

    def test_deny_precedence_and_scope_gates_still_bind_raw_shell(self) -> None:
        context = _context()
        descriptor_value = descriptor()
        grants = _grants()  # type: ignore[arg-type]

        def _decide(**overrides: object):  # type: ignore[no-untyped-def]
            arguments: dict[str, object] = {
                "context": context,
                "capability_id": CAPABILITY_ID,
                "descriptor": descriptor_value,
                "grants": grants,
                "policy": _policy(),
                "adopted": True,
            }
            arguments.update(overrides)
            return evaluate(**arguments)  # type: ignore[arg-type]

        denied = _decide(policy=_policy(denied=(CAPABILITY_ID,)))
        assert denied.allowed is False
        assert denied.reason is DecisionReason.CAPABILITY_DENIED_BY_POLICY
        ungranted = _decide(context=_context(allowed=()))
        assert ungranted.reason is DecisionReason.CAPABILITY_NOT_GRANTED
        assert _decide(adopted=False).reason is DecisionReason.SERVER_NOT_ADOPTED
        other_server = _decide(context=_context(server_id=uuid.uuid4()))
        assert other_server.reason is DecisionReason.SERVER_SCOPE_MISMATCH


# ---------------------------------------------------------------------------
# Payload → CommandRequest mapping (pure)
# ---------------------------------------------------------------------------


class TestRequestFromPayload:
    def test_minimal_payload_maps_with_module_defaults(self) -> None:
        op_id, server_id = uuid.uuid4(), SERVER_A
        request = request_from_payload(
            {"command": "echo hi"}, op_id=op_id, server_id=server_id
        )
        assert isinstance(request, CommandRequest)
        assert request.op_id == op_id
        assert request.server_id == server_id
        assert request.shell == "echo hi"
        assert request.argv is None
        assert request.display == DISPLAY_LABEL
        assert request.timeout == DEFAULT_TIMEOUT_SECONDS
        assert request.output_budget == DEFAULT_OUTPUT_BUDGET_BYTES
        assert request.working_directory is None

    def test_optional_fields_override_defaults(self) -> None:
        request = request_from_payload(
            {
                "command": "cd /tmp && pwd",
                "timeout": 30,
                "output_budget": 1024,
                "working_directory": "/tmp",
            },
            op_id=uuid.uuid4(),
            server_id=SERVER_A,
        )
        assert request.timeout == 30
        assert request.output_budget == 1024
        assert request.working_directory == "/tmp"

    @pytest.mark.parametrize(
        "payload",
        [
            "echo hi",  # not a mapping
            {},  # command missing
            {"command": ""},  # empty
            {"command": "   "},  # whitespace only
            {"command": 7},  # not a string
            {"command": "echo\x00hi"},  # NUL
            {"command": "echo hi", "target": "evil"},  # payload names no target
            {"command": "echo hi", "argv": ["/bin/sh"]},  # not this capability
            {"command": "echo", "timeout": True},  # bool is not a number
            {"command": "echo", "timeout": 0},
            {"command": "echo", "timeout": MAX_COMMAND_TIMEOUT_SECONDS + 1},
            {"command": "echo", "output_budget": True},
            {"command": "echo", "output_budget": 0},
            {"command": "echo", "output_budget": MAX_OUTPUT_BUDGET_BYTES + 1},
            {"command": "echo", "working_directory": "relative/path"},
            {"command": "echo", "working_directory": "/tmp\n/etc"},
        ],
    )
    def test_shape_violations_reject_before_any_effect(self, payload) -> None:
        with pytest.raises(HostShellError) as excinfo:
            request_from_payload(payload, op_id=uuid.uuid4(), server_id=SERVER_A)
        assert excinfo.value.code is HostShellErrorCode.INVALID_REQUEST


# ---------------------------------------------------------------------------
# Constructor wiring (fail-closed composition)
# ---------------------------------------------------------------------------


class TestWiring:
    def _capability(self, **overrides: object) -> HostShellCapability:
        base: dict[str, object] = {
            "profiles": lambda server_id: _profile(),
            "known_hosts": lambda server_id: "/tmp/mai-fxt-04-shell-known-hosts",
            "receipt_root": "/home/mai-fxt/mai-receipts",
            "intents_dir": "/tmp/mai-fxt-04-shell-intents",
        }
        base.update(overrides)
        return HostShellCapability(**base)  # type: ignore[arg-type]

    def test_valid_construction_is_accepted(self) -> None:
        capability = self._capability()
        assert repr(capability).startswith("<HostShellCapability host.shell")

    @pytest.mark.parametrize(
        "overrides",
        [
            {"profiles": "not-callable"},
            {"known_hosts": None},
            {"receipt_root": "relative/path"},
            {"receipt_root": "/tmp/\x00bad"},
            {"receipt_root": "/" + "x" * 5000},
            {"intents_dir": ""},
            {"default_timeout": True},
            {"default_timeout": 0},
            {"default_timeout": MAX_COMMAND_TIMEOUT_SECONDS + 1},
            {"default_output_budget": True},
            {"default_output_budget": 0},
            {"default_output_budget": MAX_OUTPUT_BUDGET_BYTES + 1},
            {"settle_seconds": True},
            {"settle_seconds": -1},
            {"settle_seconds": MAX_SETTLE_SECONDS + 1},
        ],
    )
    def test_wiring_violations_refuse_construction(self, overrides) -> None:
        with pytest.raises(HostShellError) as excinfo:
            self._capability(**overrides)
        assert excinfo.value.code is HostShellErrorCode.INVALID_CONTEXT


# ---------------------------------------------------------------------------
# Scripted supervisor seam (test double — see the module docstring NOTE)
# ---------------------------------------------------------------------------


class _ScriptedLane:
    """Stands in for the supervisor lane's module functions.

    Emulates the SURROUNDING contract only (typed request validation and
    the handle/report vocabulary); it proves the executor's decisions and
    document assembly, never remote-effect semantics.
    """

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.submits: list[tuple[object, CommandRequest, object, dict]] = []
        self.inspections = 0

    def submit(self, identity, request, mode, **kwargs):  # type: ignore[no-untyped-def]
        self.submits.append((identity, request, mode, kwargs))
        if not isinstance(request, CommandRequest):
            raise TypeError("request must be a CommandRequest (lane contract)")
        intent = SupervisionIntent(
            op_id=request.op_id,
            server_id=request.server_id,
            display=request.display,
            scheme=SupervisionScheme.WRAPPER,
            privilege_mode=mode.value,
            receipt_root=str(kwargs.get("receipt_root", "")),
            host=identity.host,
            port=identity.port,
            user=identity.user,
            payload_sha256="a" * 64,
            dispatch_ts="2026-10-14T00:00:00+00:00",
            boot_id="",
            status="submitted",
        )
        return SupervisionHandle(intent=intent, intent_path="/tmp/intents/x.json")

    def inspect(self, identity, intent, **kwargs):  # type: ignore[no-untyped-def]
        self.inspections += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        # lane contract: a report is keyed by the submitted operation
        return dataclasses.replace(item, op_id=intent.op_id)  # type: ignore[arg-type]


def _report(
    *,
    status: CommandStatus,
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    notes: tuple[str, ...] = (),
    op_id: uuid.UUID | None = None,
) -> object:
    """One scripted :class:`SupervisionReport` with the frozen shape."""
    return SupervisionReport(
        op_id=op_id or uuid.uuid4(),
        server_id=SERVER_A,
        display=DISPLAY_LABEL,
        scheme=SupervisionScheme.WRAPPER,
        status=status,
        receipt_missing=False,
        state_seq="4",
        state_ts="2026-10-14T00:00:00+00:00",
        receipt_boot_id="boot-1",
        current_boot_id="boot-1",
        boot_changed=False,
        wrapper_pid="10",
        payload_pid="20",
        payload_startticks="30",
        identity_resolved=True,
        heartbeat_age=0.5,
        exit_code=exit_code,
        descendants=(),
        stdout=stdout,
        stdout_bytes=len(stdout.encode("utf-8")),
        stdout_truncated=False,
        stderr=stderr,
        stderr_bytes=len(stderr.encode("utf-8")),
        stderr_truncated=False,
        events_tail="state=starting\nstate=running\nstate=exited\n",
        notes=notes,
    )


class _Harness:
    """A capability wired to a scratch namespace and a scripted lane."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.lane = _ScriptedLane([])
        monkeypatch.setattr(
            host_shell_module, "submit_supervised_command", self.lane.submit
        )
        monkeypatch.setattr(
            host_shell_module, "inspect_supervised_command", self.lane.inspect
        )
        monkeypatch.setattr(host_shell_module, "SETTLE_POLL_SECONDS", 0.01)
        self.scratch = tmp_path
        self.key_file = tmp_path / "fixture_key"
        self.key_file.write_text("not-a-real-key\n", encoding="utf-8")
        self.known_hosts = tmp_path / "known_hosts"
        self.known_hosts.write_text("pin\n", encoding="utf-8")
        self.profile = _profile(privilege=PrivilegeMode.NONE)
        self.capability = HostShellCapability(
            profiles=lambda server_id: self.profile,
            known_hosts=lambda server_id: self.known_hosts,
            receipt_root="/home/mai-fxt/mai-receipts",
            intents_dir=str(tmp_path / "intents"),
            settle_seconds=1.0,
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

    async def run(self, **kwargs: object) -> ExecutorResult:
        return await self.capability(self.call(**kwargs))  # type: ignore[arg-type]


class TestPreEffectGuards:
    """Every wiring/context failure refuses BEFORE the supervisor lane."""

    def _harness(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Harness:
        return _Harness(monkeypatch, tmp_path)

    def _rebuild(self, harness: _Harness, **overrides: object) -> None:
        base: dict[str, object] = {
            "profiles": lambda server_id: harness.profile,
            "known_hosts": lambda server_id: harness.known_hosts,
            "receipt_root": "/home/mai-fxt/mai-receipts",
            "intents_dir": str(harness.scratch / "intents"),
            "settle_seconds": 1.0,
        }
        base.update(overrides)
        harness.capability = HostShellCapability(**base)  # type: ignore[arg-type]

    def test_missing_profile_refuses_without_transport(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)

        def _missing(server_id: uuid.UUID) -> ServerProfile:
            raise KeyError(server_id)

        self._rebuild(harness, profiles=_missing)
        with pytest.raises(HostShellError) as excinfo:
            asyncio.run(harness.run())
        assert excinfo.value.code is HostShellErrorCode.PROFILE_UNAVAILABLE
        assert harness.lane.submits == []

    def test_non_profile_answer_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        self._rebuild(harness, profiles=lambda server_id: {"not": "a profile"})
        with pytest.raises(HostShellError) as excinfo:
            asyncio.run(harness.run())
        assert excinfo.value.code is HostShellErrorCode.PROFILE_UNAVAILABLE
        assert harness.lane.submits == []

    def test_profile_identity_mismatch_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        harness.profile = _profile(server_id=uuid.uuid4())
        with pytest.raises(HostShellError) as excinfo:
            asyncio.run(harness.run())
        assert excinfo.value.code is HostShellErrorCode.PROFILE_MISMATCH
        assert harness.lane.submits == []

    def test_missing_credential_reference_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        call = _call(GOOD_PAYLOAD, credentials={})
        with pytest.raises(HostShellError) as excinfo:
            asyncio.run(harness.capability(call))
        assert excinfo.value.code is HostShellErrorCode.CREDENTIAL_UNAVAILABLE
        assert FIXTURE_CREDENTIAL_REF in excinfo.value.detail
        assert harness.lane.submits == []

    def test_non_secret_credential_value_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        call = harness.call(
            credentials={FIXTURE_CREDENTIAL_REF: "/tmp/not-a-secret-object"}
        )
        with pytest.raises(HostShellError) as excinfo:
            asyncio.run(harness.capability(call))
        assert excinfo.value.code is HostShellErrorCode.CREDENTIAL_UNAVAILABLE
        assert harness.lane.submits == []

    def test_unresolvable_key_file_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        # a SecretValue whose file is gone: the key-file check refuses
        call = harness.call(
            credentials={FIXTURE_CREDENTIAL_REF: _secret(str(tmp_path / "gone"))}
        )
        with pytest.raises(HostShellError) as excinfo:
            asyncio.run(harness.capability(call))
        assert excinfo.value.code is HostShellErrorCode.KEY_FILE_UNAVAILABLE
        assert harness.lane.submits == []

    def test_unenrolled_known_hosts_refuse(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        self._rebuild(harness, known_hosts=lambda server_id: tmp_path / "absent")
        with pytest.raises(HostShellError) as excinfo:
            asyncio.run(harness.run())
        assert excinfo.value.code is HostShellErrorCode.HOST_KEY_UNENROLLED
        assert harness.lane.submits == []

    def test_malformed_payload_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        with pytest.raises(HostShellError) as excinfo:
            asyncio.run(harness.run(payload={"command": "x", "argv": ["sh"]}))
        assert excinfo.value.code is HostShellErrorCode.INVALID_REQUEST
        assert harness.lane.submits == []

    def test_non_call_input_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        with pytest.raises(HostShellError) as excinfo:
            asyncio.run(harness.capability({"command": "echo"}))  # type: ignore[arg-type]
        assert excinfo.value.code is HostShellErrorCode.INVALID_CONTEXT
        assert harness.lane.submits == []


class TestTransportComposition:
    """Document assembly and settle behavior over the scripted lane."""

    def test_succeeded_call_yields_evidence_document(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.lane.script = [
            _report(status=CommandStatus.SUCCEEDED, exit_code=0, stdout="hi\n")
        ]
        result = asyncio.run(harness.run())
        assert isinstance(result, ExecutorResult)
        assert result.ok is True
        document = json.loads(result.output)
        assert document["capability"] == CAPABILITY_ID
        assert document["status"] == "succeeded"
        assert document["exit_code"] == 0
        assert document["stdout"] == "hi\n"
        assert document["stdout_bytes"] == 3
        assert document["stdout_truncated"] is False
        assert document["stderr"] == ""
        assert document["privilege_mode"] == "none"
        assert document["scheme"] == "wrapper"
        assert document["receipt_dir"].startswith("/home/mai-fxt/mai-receipts/")
        assert document["intent_path"].endswith(".json")
        assert document["notes"] == []
        assert document["identity_resolved"] is True
        assert document["receipt_boot_id"] == "boot-1"
        # the scripted report's op id matches the submitted request
        (identity, request, mode, kwargs) = harness.lane.submits[0]
        assert document["op_id"] == str(request.op_id)
        assert document["receipt_dir"].endswith(str(request.op_id))
        assert mode is PrivilegeMode.NONE
        assert kwargs["receipt_root"] == "/home/mai-fxt/mai-receipts"
        assert kwargs["key_path"] == harness.key_file
        assert kwargs["known_hosts_path"] == harness.known_hosts
        assert harness.lane.inspections == 1

    def test_failed_exit_keeps_ok_false_and_evidence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.lane.script = [
            _report(
                status=CommandStatus.FAILED,
                exit_code=7,
                stderr="bang\n",
            )
        ]
        result = asyncio.run(harness.run())
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "failed"
        assert document["exit_code"] == 7
        assert document["stderr"] == "bang\n"

    def test_settle_polls_until_terminal_status(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.lane.script = [
            _report(status=CommandStatus.RUNNING),
            _report(status=CommandStatus.SUCCEEDED, exit_code=0, stdout="done"),
        ]
        result = asyncio.run(harness.run())
        assert result.ok is True
        assert harness.lane.inspections == 2
        assert json.loads(result.output)["status"] == "succeeded"

    def test_settle_budget_returns_last_proven_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.capability = HostShellCapability(
            profiles=lambda server_id: harness.profile,
            known_hosts=lambda server_id: harness.known_hosts,
            receipt_root="/home/mai-fxt/mai-receipts",
            intents_dir=str(tmp_path / "intents"),
            settle_seconds=0.05,
        )
        harness.lane.script = [_report(status=CommandStatus.RUNNING)]
        started = time.monotonic()
        result = asyncio.run(harness.run())
        assert time.monotonic() - started < 5.0  # bounded, not a hang
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "running"
        assert document["exit_code"] is None
        assert harness.lane.inspections >= 2

    def test_unknown_status_reports_notes_never_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.capability = HostShellCapability(
            profiles=lambda server_id: harness.profile,
            known_hosts=lambda server_id: harness.known_hosts,
            receipt_root="/home/mai-fxt/mai-receipts",
            intents_dir=str(tmp_path / "intents"),
            settle_seconds=0.05,
        )
        harness.lane.script = [
            _report(
                status=CommandStatus.OUTCOME_UNKNOWN,
                notes=("receipt_missing: an effect can precede its receipt",),
            )
        ]
        result = asyncio.run(harness.run())
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "outcome_unknown"
        assert document["exit_code"] is None
        assert document["notes"] == [
            "receipt_missing: an effect can precede its receipt"
        ]

    def test_sudo_profile_routes_escalation_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.profile = _profile(privilege=PrivilegeMode.SUDO)
        harness.lane.script = [
            _report(status=CommandStatus.SUCCEEDED, exit_code=0, stdout="0\n")
        ]
        result = asyncio.run(harness.run())
        assert result.ok is True
        (identity, request, mode, kwargs) = harness.lane.submits[0]
        assert mode is PrivilegeMode.SUDO
        assert json.loads(result.output)["privilege_mode"] == "sudo"

    def test_document_never_carries_secret_values(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.lane.script = [
            _report(status=CommandStatus.SUCCEEDED, exit_code=0, stdout="ok")
        ]
        result = asyncio.run(harness.run())
        rendered = result.output
        assert str(harness.key_file) not in rendered
        assert "not-a-real-key" not in rendered


# ---------------------------------------------------------------------------
# Dispatcher integration: deny outcomes never reach this executor
# (lazy approval store — no database needed for the deny paths)
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
) -> tuple[CapabilityDispatcher, HostShellCapability]:
    """The REAL descriptor + REAL capability object behind the dispatcher.

    The capability's providers raise AssertionError if invoked — the
    executor running AT ALL on a deny path fails the test; the approval
    store would fail on any real consumption.
    """

    def _boom(server_id: uuid.UUID) -> ServerProfile:
        raise AssertionError("executor invoked on a deny path")

    capability = HostShellCapability(
        profiles=_boom,
        known_hosts=_boom,
        receipt_root="/home/mai-fxt/mai-receipts",
        intents_dir=str(tmp_path / "intents"),
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
    async def test_privileged_call_without_decision_denies_in_ask_mode(
        self, tmp_path: Path
    ) -> None:
        """In ask mode, the raw-shell class requires an approval decision —
        an unapproved call stops at the dispatcher seam, executor uninvoked."""
        dispatcher, _capability = _deny_dispatcher(tmp_path)
        outcome = await dispatcher.dispatch(
            context=_context(mode=PolicyMode.ASK),
            capability_id=CAPABILITY_ID,
            payload=GOOD_PAYLOAD,
        )
        assert outcome.reason == DispatchReason.APPROVAL_REQUIRED_BUT_ABSENT
        assert outcome.effect == "not_attempted"


# ---------------------------------------------------------------------------
# Real boundary: disposable Arch target + disposable Postgres approvals
# (the feature-gate scenario halves this card owns)
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(ALEMBIC_INI))


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero (sibling recipe)."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the approval-seam claims need a "
            "real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                "DROP TABLE IF EXISTS approvals, admin_sessions, "
                "admin_credential, profile_projection_journal, "
                "profile_projection, alembic_version"
            )
            conn.commit()
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
    await engine.dispose()


@pytest.fixture
def store(sessions) -> ApprovalStore:
    return ApprovalStore(sessions)


class _ShellArchFixture(_ArchSshdFixture):
    """Same disposable image/fixture as 04-supervisor, card namespace.

    ``identity()``/``transport()`` are overridden because the inherited
    helpers hardcode the SUPERVISOR card's credential reference name;
    this card's profile/context/env resolve its OWN reference name.
    """

    container = FIXTURE_CONTAINER

    def __init__(self) -> None:
        # base-class init uses the SUPERVISOR namespace scratch path; this
        # card owns its own C10-rule-2 namespace instead
        self.scratch = FIXTURE_SCRATCH / uuid.uuid4().hex
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.key_path = self.scratch / "fixture_key"
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
            f"{FIXTURE_CREDENTIAL_REF}={self.key_path}\n", encoding="utf-8"
        )
        os.chmod(env_file, 0o600)
        key_path = resolve_identity_key(EnvResolver(env_file), FIXTURE_CREDENTIAL_REF)
        return key_path, self.scratch / "known_hosts"

    def ensure_image(self) -> None:
        if _docker("image", "inspect", FIXTURE_IMAGE_TAG).returncode == 0:
            return
        _ensure_fixture_image()  # same recipe builds the base tag
        build = _docker("tag", "mai-fxt-04-supervisor-arch:sshd", FIXTURE_IMAGE_TAG)
        if build.returncode != 0:
            context = self.scratch / "build-context"
            context.mkdir(parents=True, exist_ok=True)
            (context / "Dockerfile").write_text(
                "FROM archlinux:latest\n"
                "RUN pacman-key --init && pacman -Sy --noconfirm openssh sudo "
                "&& ssh-keygen -A\n",
                encoding="utf-8",
            )
            build = _docker(
                "build", "-q", "-t", FIXTURE_IMAGE_TAG, str(context), timeout=1200
            )
        assert build.returncode == 0, (
            f"fixture image build failed: {build.stderr[-800:]}"
        )

    def cleanup(self) -> str:
        proof = super().cleanup()
        strays = [
            name
            for name in _docker("ps", "-a", "--format", "{{.Names}}").stdout.split()
            if name.startswith("mai-fxt-04-shell-arch")
        ]
        return f"{proof}; shell-fixture strays: {strays or 'none'}"


def _pick_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _class_shell_fixture():
    if not _docker_available():
        pytest.skip("docker unavailable: Arch shell fixture tests require it")
    fixture = _ShellArchFixture()
    try:
        fixture.ensure_image()
        fixture.start()
        yield fixture
    finally:
        print(f"\nFIXTURE CLEANUP PROOF: {fixture.cleanup()}")


@pytest.fixture(scope="class")
def shell_arch() -> _ShellArchFixture:
    yield from _class_shell_fixture()


class _RealComposition:
    """The real composed stack: dispatcher → approval store → capability
    → supervisor lane → disposable target."""

    def __init__(
        self,
        fixture: _ShellArchFixture,
        store: ApprovalStore,
        *,
        privilege: PrivilegeMode = PrivilegeMode.NONE,
        grants: dict[str, object] | None = None,
        policy: Policy | None = None,
    ) -> None:
        key_path, known_hosts = fixture.transport()
        self.key_path = key_path
        self.store = store
        self.profile = _profile(fixture.identity(), privilege=privilege)
        self.capability = HostShellCapability(
            profiles=lambda server_id: self.profile,
            known_hosts=lambda server_id: known_hosts,
            receipt_root=RECEIPT_ROOT,
            intents_dir=fixture.scratch / "intents",
            settle_seconds=25.0,
        )
        self.dispatcher = CapabilityDispatcher(
            registry=_registry(),
            approvals=store,
            grants=(lambda: _grants()) if grants is None else (lambda: grants),  # type: ignore[arg-type,return-value]
            policy=(lambda: _policy()) if policy is None else (lambda: policy),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(fixture.scratch / "fixture.env"),
            executor=self.capability,
        )
        self.fixture = fixture

    async def approve(self, payload: dict) -> uuid.UUID:  # type: ignore[type-arg]
        approval = await self.store.create(
            ApprovalRequest(
                capability_id=CAPABILITY_ID,
                server_id=SERVER_A,
                payload_hash=canonical_payload_hash(payload),
                policy_revision=POLICY_REVISION,
                created_by=PRINCIPAL,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
        )
        return approval.approval_id

    def sentinel_path(self) -> str:
        return f"/home/{FIXTURE_USER}/sentinel-{uuid.uuid4().hex}"

    def target_exists(self, path: str) -> bool:
        quoted = shlex.quote(path)
        _rc, out, _err = self.fixture.ssh(f"[ -e {quoted} ] && echo Y || echo N")
        return out.strip() == "Y"

    def target_read(self, path: str) -> str:
        _rc, out, _err = self.fixture.ssh(f"cat {shlex.quote(path)}")
        return out

    def target_remove(self, path: str) -> None:
        self.fixture.ssh(f"rm -f {shlex.quote(path)}")

    def receipt_dir_exists(self, op_id: uuid.UUID) -> bool:
        return self.target_exists(f"{RECEIPT_ROOT}/{op_id}")

    def receipt_list(self) -> set[str]:
        _rc, out, _err = self.fixture.ssh(f"ls -A {shlex.quote(RECEIPT_ROOT)}")
        return set(out.split())


class TestAgainstShellArch:
    """Feature-gate scenario halves owned by this card (P04 gate)."""

    def _composition(
        self,
        shell_arch: _ShellArchFixture,
        store: ApprovalStore,
        **kwargs: object,
    ) -> _RealComposition:
        return _RealComposition(shell_arch, store, **kwargs)  # type: ignore[arg-type]

    @pytest.mark.anyio
    async def test_allowed_call_executes_and_records_target_sentinel(
        self, shell_arch: _ShellArchFixture, store: ApprovalStore
    ) -> None:
        """Allowed half: the command REALLY reaches the disposable target
        through the composed dispatcher and leaves the sentinel + receipt
        evidence behind."""
        world = self._composition(shell_arch, store)
        sentinel = world.sentinel_path()
        payload = {
            "command": f"printf %s shell-capability-sentinel > {sentinel}; "
            f"cat {sentinel}"
        }
        approval_id = await world.approve(payload)
        try:
            outcome = await world.dispatcher.dispatch(
                context=_context(mode=PolicyMode.ASK),
                capability_id=CAPABILITY_ID,
                payload=payload,
                approval_id=approval_id,
            )
            assert outcome.allowed is True
            assert outcome.effect == "executed"
            assert outcome.executor_ok is True
            assert outcome.reason == DecisionReason.ALLOWED.value
            assert outcome.approval_spent is True
            assert outcome.approval_id == approval_id
            assert outcome.output is not None
            document = json.loads(outcome.output.text)
            assert document["status"] == "succeeded"
            assert document["exit_code"] == 0
            assert "shell-capability-sentinel" in document["stdout"]
            assert document["stdout_truncated"] is False
            # target truth: the sentinel exists with the exact content and
            # the durable receipt directory was established
            assert world.target_read(sentinel) == "shell-capability-sentinel"
            assert world.receipt_dir_exists(uuid.UUID(document["op_id"]))
            # secret hygiene: the resolved key path never leaks into the
            # rendered receipt (dispatcher redaction + document discipline)
            rendered = json.dumps(outcome.to_json())
            assert str(world.key_path) not in rendered
            assert "fixture_key" not in rendered
        finally:
            world.target_remove(sentinel)

    @pytest.mark.anyio
    async def test_denied_grant_never_reaches_ssh_or_touches_target(
        self, shell_arch: _ShellArchFixture, store: ApprovalStore
    ) -> None:
        """Denied half: no grant ⇒ deny before any effect; the decision is
        not spent and the target is verifiably untouched."""
        world = self._composition(shell_arch, store, grants={}, policy=_policy())
        sentinel = world.sentinel_path()
        payload = {"command": f"printf %s shell-capability-sentinel > {sentinel}"}
        approval_id = await world.approve(payload)
        receipts_before = world.receipt_list()
        outcome = await world.dispatcher.dispatch(
            context=_context(mode=PolicyMode.ASK),
            capability_id=CAPABILITY_ID,
            payload=payload,
            approval_id=approval_id,
        )
        assert outcome.allowed is False
        assert outcome.reason == DecisionReason.CAPABILITY_NOT_GRANTED.value
        assert outcome.effect == "not_attempted"
        assert outcome.approval_spent is False
        record = await store.get(approval_id)
        assert record is not None and record.status == "pending"
        # target truth: nothing ran, nothing was written, no receipt appeared
        assert world.target_exists(sentinel) is False
        assert world.receipt_list() == receipts_before

    @pytest.mark.anyio
    async def test_unapproved_call_denied_and_decision_not_spent(
        self, shell_arch: _ShellArchFixture, store: ApprovalStore
    ) -> None:
        world = self._composition(shell_arch, store)
        sentinel = world.sentinel_path()
        payload = {"command": f"printf %s shell-capability-sentinel > {sentinel}"}
        approval_id = await world.approve(payload)
        receipts_before = world.receipt_list()
        outcome = await world.dispatcher.dispatch(
            context=_context(mode=PolicyMode.ASK),
            capability_id=CAPABILITY_ID,
            payload=payload,
            approval_id=None,
        )
        assert outcome.allowed is False
        assert outcome.reason == DispatchReason.APPROVAL_REQUIRED_BUT_ABSENT
        assert outcome.effect == "not_attempted"
        record = await store.get(approval_id)
        assert record is not None and record.status == "pending"
        assert world.target_exists(sentinel) is False
        assert world.receipt_list() == receipts_before

    @pytest.mark.anyio
    async def test_revoked_policy_denies_before_effect(
        self, shell_arch: _ShellArchFixture, store: ApprovalStore
    ) -> None:
        world = self._composition(
            shell_arch, store, policy=_policy(denied=(CAPABILITY_ID,))
        )
        sentinel = world.sentinel_path()
        payload = {"command": f"printf %s shell-capability-sentinel > {sentinel}"}
        approval_id = await world.approve(payload)
        receipts_before = world.receipt_list()
        outcome = await world.dispatcher.dispatch(
            context=_context(mode=PolicyMode.ASK),
            capability_id=CAPABILITY_ID,
            payload=payload,
            approval_id=approval_id,
        )
        assert outcome.allowed is False
        assert outcome.reason == DecisionReason.CAPABILITY_DENIED_BY_POLICY.value
        assert outcome.effect == "not_attempted"
        assert outcome.approval_spent is False
        record = await store.get(approval_id)
        assert record is not None and record.status == "pending"
        assert world.target_exists(sentinel) is False
        assert world.receipt_list() == receipts_before  # no receipt appeared

    @pytest.mark.anyio
    async def test_failed_exit_reports_evidence_not_only_exit(
        self, shell_arch: _ShellArchFixture, store: ApprovalStore
    ) -> None:
        world = self._composition(shell_arch, store)
        payload = {"command": "echo shell-bang >&2; exit 7"}
        approval_id = await world.approve(payload)
        outcome = await world.dispatcher.dispatch(
            context=_context(mode=PolicyMode.ASK),
            capability_id=CAPABILITY_ID,
            payload=payload,
            approval_id=approval_id,
        )
        assert outcome.allowed is True
        assert outcome.effect == "executed"
        assert outcome.executor_ok is False
        assert outcome.output is not None
        document = json.loads(outcome.output.text)
        assert document["status"] == "failed"
        assert document["exit_code"] == 7
        assert "shell-bang" in document["stderr"]
        assert document["stderr_bytes"] > 0
        assert document["receipt_dir"].startswith(f"{RECEIPT_ROOT}/")
        assert document["intent_path"].startswith("/tmp/mai-fxt-04-shell-tests/")

    @pytest.mark.anyio
    async def test_sudo_profile_runs_root_via_nopasswd(
        self, shell_arch: _ShellArchFixture, store: ApprovalStore
    ) -> None:
        world = self._composition(shell_arch, store, privilege=PrivilegeMode.SUDO)
        shell_arch.write_sudo_grant()
        try:
            payload = {"command": "/usr/bin/id -u"}
            approval_id = await world.approve(payload)
            outcome = await world.dispatcher.dispatch(
                context=_context(mode=PolicyMode.ASK),
                capability_id=CAPABILITY_ID,
                payload=payload,
                approval_id=approval_id,
            )
            assert outcome.allowed is True
            assert outcome.executor_ok is True
            assert outcome.output is not None
            document = json.loads(outcome.output.text)
            assert document["status"] == "succeeded"
            assert document["privilege_mode"] == "sudo"
            assert document["stdout"].strip() == "0"  # uid 0 on the target
        finally:
            shell_arch.remove_sudo_grant()
