"""Focused tests for the ``host.file_read`` capability and its wiring.

Covers: descriptor/read-class gates, payload validation (including the
``file_read_roots`` allowlist and the deny-by-default list), structured
argv composition (quoting proof through the real transport composer),
the fixed remote helper's behavior against real files (pages, exact
boundaries, empty file, offset past EOF, symlink escape, sibling-prefix
escape, invalid UTF-8, byte budget), executor wiring (pre-effect
refusals before any transport, honest documents), and the profile
schema extension (``file_read_roots``, backward compatible).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from mai.capabilities import host_file_read as module
from mai.capabilities.dispatch import CapabilityCall
from mai.capabilities.host_file_read import (
    CAPABILITY_ID,
    DEFAULT_LIMIT,
    DISPLAY_LABEL,
    MAX_CONTENT_BYTES,
    MAX_LIMIT,
    TRANSPORT_OUTPUT_BUDGET_BYTES,
    FileReadError,
    FileReadErrorCode,
    HostFileReadCapability,
    _HELPER_PROGRAM,
    compose_read_argv,
    descriptor,
    read_query_from_payload,
)
from mai.contracts.server import (
    ExecutionEnvironment,
    PrivilegeMode,
    ProfileValidationError,
    ServerProfile,
    SshIdentity,
    parse_server_profile,
)
from mai.credentials.env import SecretValue
from mai.execution.commands import (
    CommandOutcome,
    CommandRequest,
    CommandStatus,
    compose_remote_command,
)
from mai.plugins.registry import CapabilityRegistry
from mai.policy.evaluate import (
    DecisionReason,
    ExecutionContext,
    Policy,
    PolicyMode,
    evaluate,
)

SERVER_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
RUN_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
PRINCIPAL = "admin"
CREDENTIAL_REF = "MAI_SSH_KEY_FILE_READ_FIXTURE"
ALLOWED_ROOT = "/srv/data"

GOOD_PAYLOAD = {"path": "/srv/data/notes.txt"}

BACKEND_ROOT = Path(__file__).resolve().parents[1]

#: The REAL worker lane (``_configure_default_agent_runtime``) probed in
#: a fresh subprocess: the runtime is process-global, so the wiring
#: proof must not share this pytest process. The dispatcher's verdict
#: distinguishes wiring states without any live database (the engine
#: URL is lazy): our executor raising ``FileReadError`` proves the
#: executors-map entry AND the profile-surfaced grant, while a server
#: without the ``file_read`` slug is refused as ``capability_not_granted``.
_WORKER_PROBE_SCRIPT = """\
import asyncio
import json
import os
import uuid

from mai.agents import run as agent_run
from mai.capabilities.host_file_read import descriptor as file_read_descriptor
from mai.jobs.worker import _configure_default_agent_runtime
from mai.policy.evaluate import ExecutionContext, PolicyMode

_configure_default_agent_runtime(os.environ["MAI_DATABASE_URL"])
dispatcher = agent_run._EXECUTOR._steps._dispatcher
context = ExecutionContext(
    principal="probe",
    server_id=uuid.UUID(os.environ["PROBE_SERVER_ID"]),
    run_id=uuid.uuid4(),
    parent_scope=None,
    profile_revision="1",
    prompt_revision="1",
    credential_references=(),
    allowed_capabilities=("host.file_read",),
    mode=PolicyMode.DELEGATED,
)
outcome = asyncio.run(
    dispatcher.dispatch(
        context=context,
        capability_id="host.file_read",
        payload={"path": "/srv/data/notes.txt"},
    )
)
print(
    json.dumps(
        {
            "allowed": outcome.allowed,
            "reason": outcome.reason,
            "error_type": outcome.error_type,
        }
    )
)
"""


# ---------------------------------------------------------------------------
# Shared builders (pure)
# ---------------------------------------------------------------------------


def _identity() -> SshIdentity:
    return SshIdentity(
        host="127.0.0.1",
        port=2200,
        user="mai-fxt",
        host_key="SHA256:" + "A" * 43,
        credential_ref=CREDENTIAL_REF,
    )


def _profile(
    server_id: uuid.UUID = SERVER_A,
    *,
    file_read_roots: tuple[str, ...] = (ALLOWED_ROOT,),
) -> ServerProfile:
    return ServerProfile(
        schema_version=1,
        server_id=server_id,
        name="fixture target",
        prompt_revision=1,
        environment=ExecutionEnvironment.LINUX_SSH,
        ssh=_identity(),
        privilege=PrivilegeMode.NONE,
        capabilities=(CAPABILITY_ID,),
        file_read_roots=file_read_roots,
    )


def _context(
    *,
    server_id: uuid.UUID = SERVER_A,
    references: tuple[str, ...] = (CREDENTIAL_REF,),
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


def _call(
    payload: object = GOOD_PAYLOAD,
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


def _decide(**overrides: object):  # type: ignore[no-untyped-def]
    arguments: dict[str, object] = {
        "context": _context(),
        "capability_id": CAPABILITY_ID,
        "descriptor": descriptor(),
        "grants": {CAPABILITY_ID: frozenset({SERVER_A})},
        "policy": Policy(revision=1, denied_capabilities=frozenset()),
        "adopted": True,
    }
    arguments.update(overrides)
    return evaluate(**arguments)  # type: ignore[arg-type]


class _ScriptedTransport:
    """Records composed requests, replays scripted outcomes."""

    def __init__(self, script: list[CommandOutcome]) -> None:
        self.script = list(script)
        self.calls: list[tuple[object, CommandRequest, str, str]] = []

    def __call__(
        self,
        identity: object,
        request: CommandRequest,
        *,
        key_path: str,
        known_hosts_path: str,
    ) -> CommandOutcome:
        self.calls.append((identity, request, key_path, known_hosts_path))
        if not self.script:
            raise AssertionError("transport called without a scripted outcome")
        return self.script.pop(0)


def _outcome(
    *,
    status: CommandStatus = CommandStatus.SUCCEEDED,
    exit_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
) -> CommandOutcome:
    return CommandOutcome(
        op_id=uuid.uuid4(),
        server_id=SERVER_A,
        display=DISPLAY_LABEL,
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stdout_bytes=len(stdout.encode("utf-8")),
        stdout_truncated=False,
        stderr=stderr,
        stderr_bytes=len(stderr.encode("utf-8")),
        stderr_truncated=False,
        timed_out=False,
        cwd_change_failed=False,
        duration_seconds=0.01,
    )


class _Harness:
    """A capability wired to fakes and a scripted transport."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.transport = _ScriptedTransport([])
        monkeypatch.setattr(module, "execute_command", self.transport)
        self.key_file = tmp_path / "fixture_key"
        self.key_file.write_text("not-a-real-key\n", encoding="utf-8")
        self.known_hosts = tmp_path / "known_hosts"
        self.known_hosts.write_text("pin\n", encoding="utf-8")
        self.profile = _profile()
        self.capability = HostFileReadCapability(
            profiles=lambda server_id: self.profile,
            known_hosts=lambda server_id: self.known_hosts,
        )

    def run(
        self,
        payload: object = GOOD_PAYLOAD,
        *,
        credentials: dict[str, object] | None = None,
        context: ExecutionContext | None = None,
        op_id: uuid.UUID | None = None,
    ) -> object:
        resolved = credentials
        if resolved is None:
            resolved = {CREDENTIAL_REF: _secret(str(self.key_file))}
        return asyncio.run(
            self.capability(
                _call(payload, context=context, credentials=resolved, op_id=op_id)
            )
        )


# ---------------------------------------------------------------------------
# Descriptor and decision pins (read class, gates intact)
# ---------------------------------------------------------------------------


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
        schema = descriptor().to_json()["input_schema"]
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["path"]
        assert sorted(schema["properties"]) == ["limit", "offset", "path"]

    def test_read_class_needs_no_approval_in_either_mode(self) -> None:
        for mode in (PolicyMode.ASK, PolicyMode.DELEGATED):
            decision = _decide(context=_context(mode=mode))
            assert decision.allowed is True
            assert decision.requires_approval is False
            assert decision.effect_class == "read"

    def test_every_authorization_gate_still_binds_reads(self) -> None:
        assert _decide(grants={}).reason is DecisionReason.CAPABILITY_NOT_GRANTED
        policy = Policy(revision=1, denied_capabilities=frozenset({CAPABILITY_ID}))
        assert (
            _decide(policy=policy).reason is DecisionReason.CAPABILITY_DENIED_BY_POLICY
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
# Payload → FileReadQuery (pure, allowlist + denylist before transport)
# ---------------------------------------------------------------------------


class TestReadQueryFromPayload:
    def test_minimal_payload_maps_with_module_defaults(self) -> None:
        query = read_query_from_payload(GOOD_PAYLOAD, file_read_roots=(ALLOWED_ROOT,))
        assert query.path == "/srv/data/notes.txt"
        assert query.offset == 0
        assert query.limit == DEFAULT_LIMIT
        assert query.file_read_roots == (ALLOWED_ROOT,)

    def test_full_payload_overrides_every_bound(self) -> None:
        query = read_query_from_payload(
            {"path": "/srv/data/sub/log.txt", "offset": 40, "limit": 7},
            file_read_roots=(ALLOWED_ROOT,),
        )
        assert query.path == "/srv/data/sub/log.txt"
        assert query.offset == 40
        assert query.limit == 7

    def test_nested_path_under_a_root_is_allowed(self) -> None:
        query = read_query_from_payload(
            {"path": "/srv/data/a/b/c.txt"}, file_read_roots=(ALLOWED_ROOT,)
        )
        assert query.path == "/srv/data/a/b/c.txt"

    def test_empty_allowlist_denies_every_read(self) -> None:
        with pytest.raises(FileReadError) as excinfo:
            read_query_from_payload(GOOD_PAYLOAD, file_read_roots=())
        assert excinfo.value.code is FileReadErrorCode.PATH_DENIED

    def test_missing_allowlist_argument_denies(self) -> None:
        with pytest.raises(FileReadError) as excinfo:
            read_query_from_payload(GOOD_PAYLOAD, file_read_roots=[])
        assert excinfo.value.code is FileReadErrorCode.PATH_DENIED

    def test_path_outside_every_root_is_denied(self) -> None:
        with pytest.raises(FileReadError) as excinfo:
            read_query_from_payload(
                {"path": "/etc/passwd"}, file_read_roots=(ALLOWED_ROOT,)
            )
        assert excinfo.value.code is FileReadErrorCode.PATH_DENIED

    def test_sibling_prefix_escape_is_denied(self) -> None:
        # /srv/datax is NOT below /srv/data — a naive startswith check
        # would allow it; the exact root+'/' boundary must refuse it.
        with pytest.raises(FileReadError) as excinfo:
            read_query_from_payload(
                {"path": "/srv/datax/secret.txt"},
                file_read_roots=(ALLOWED_ROOT,),
            )
        assert excinfo.value.code is FileReadErrorCode.PATH_DENIED

    def test_traversal_is_normalized_before_the_containment_check(self) -> None:
        with pytest.raises(FileReadError) as excinfo:
            read_query_from_payload(
                {"path": "/srv/data/../data/../../etc/passwd"},
                file_read_roots=(ALLOWED_ROOT,),
            )
        assert excinfo.value.code is FileReadErrorCode.PATH_DENIED

    @pytest.mark.parametrize(
        "path",
        [
            "/srv/data/.env",
            "/srv/data/sub/.env",
            "/srv/data/id_rsa",
            "/srv/data/sub/id_ed25519",
            "/srv/data/server.pem",
            "/etc/shadow",
            "/etc/shadow-",
            "/root/.ssh/config",
        ],
    )
    def test_deny_by_default_list_applies_after_the_allowlist(self, path) -> None:
        with pytest.raises(FileReadError) as excinfo:
            read_query_from_payload({"path": path}, file_read_roots=(ALLOWED_ROOT,))
        assert excinfo.value.code is FileReadErrorCode.PATH_DENIED

    @pytest.mark.parametrize(
        "payload",
        [
            "not-a-mapping",
            {},
            {"path": ""},
            {"path": "relative/notes.txt"},
            {"path": "notes.txt"},
            {"path": "/etc/passwd", "extra": 1},
            {"path": 7},
            {"path": None},
            {"path": "x" * 4097},
            {"path": "/srv/data/a\x00b.txt"},
            {"path": "/srv/data/a\nb.txt"},
            {"path": "/srv/data/a\tb.txt".replace("\t", "\v")},
            {"path": "/srv/data/notes.txt", "offset": -1},
            {"path": "/srv/data/notes.txt", "offset": True},
            {"path": "/srv/data/notes.txt", "offset": "2"},
            {"path": "/srv/data/notes.txt", "offset": 1_000_000_001},
            {"path": "/srv/data/notes.txt", "limit": 0},
            {"path": "/srv/data/notes.txt", "limit": -3},
            {"path": "/srv/data/notes.txt", "limit": True},
            {"path": "/srv/data/notes.txt", "limit": "5"},
            {"path": "/srv/data/notes.txt", "limit": MAX_LIMIT + 1},
        ],
    )
    def test_shape_violations_reject_before_any_effect(self, payload) -> None:
        with pytest.raises(FileReadError) as excinfo:
            read_query_from_payload(payload, file_read_roots=(ALLOWED_ROOT,))
        assert excinfo.value.code is FileReadErrorCode.INVALID_REQUEST


# ---------------------------------------------------------------------------
# Structured argv composition (never a shell string)
# ---------------------------------------------------------------------------


class TestComposeReadArgv:
    def test_argv_is_structured_and_exact(self) -> None:
        query = read_query_from_payload(
            {"path": "/srv/data/notes.txt", "offset": 4, "limit": 9},
            file_read_roots=(ALLOWED_ROOT,),
        )
        argv = compose_read_argv(query)
        assert argv[0] == "python3"
        assert argv[1] == "-c"
        assert argv[2] == _HELPER_PROGRAM
        assert argv[3] == "--"
        assert argv[4] == "/srv/data/notes.txt"
        assert argv[5] == json.dumps([ALLOWED_ROOT])
        assert argv[6:] == ("4", "9", str(MAX_CONTENT_BYTES))

    def test_composed_transport_command_roundtrips_the_argv(self) -> None:
        # A path full of shell metacharacters must survive the REAL
        # lane composer: the sh-joined command reconstructs the exact
        # argv (this is how ssh(1) hands the string to the target shell).
        query = read_query_from_payload(
            {"path": "/srv/data/a;b|$(x)`y`.txt"},
            file_read_roots=(ALLOWED_ROOT,),
        )
        argv = compose_read_argv(query)
        request = CommandRequest(
            op_id=uuid.uuid4(),
            server_id=SERVER_A,
            display=DISPLAY_LABEL,
            timeout=30.0,
            output_budget=TRANSPORT_OUTPUT_BUDGET_BYTES,
            argv=argv,
        )
        import shlex

        rebuilt = shlex.split(compose_remote_command(request))
        assert rebuilt == list(argv)


# ---------------------------------------------------------------------------
# The fixed remote helper against real files
# ---------------------------------------------------------------------------


def _run_helper(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-c", _HELPER_PROGRAM, "--", *args],
        capture_output=True,
        timeout=60,
        check=False,
    )


def _ok_payload(result: subprocess.CompletedProcess[bytes]) -> dict[str, object]:
    assert result.returncode == 0, result
    document = json.loads(result.stdout.decode("utf-8"))
    assert "error" not in document
    return document


def _refused(result: subprocess.CompletedProcess[bytes], code: str) -> None:
    assert result.returncode == 3, result
    document = json.loads(result.stdout.decode("utf-8"))
    assert document == {"error": code}


class TestHelperProgram:
    def test_first_page_is_exact_and_counts_total_lines(self, tmp_path: Path) -> None:
        target = tmp_path / "notes.txt"
        target.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
        result = _run_helper(
            str(target), json.dumps([str(tmp_path)]), "0", "3", str(MAX_CONTENT_BYTES)
        )
        document = _ok_payload(result)
        assert document == {"content": "one\ntwo\nthree\n", "total": 5, "returned": 3}

    def test_second_page_starts_at_the_offset(self, tmp_path: Path) -> None:
        target = tmp_path / "notes.txt"
        target.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
        document = _ok_payload(
            _run_helper(
                str(target),
                json.dumps([str(tmp_path)]),
                "3",
                "10",
                str(MAX_CONTENT_BYTES),
            )
        )
        assert document == {"content": "four\nfive\n", "total": 5, "returned": 2}

    def test_offset_past_eof_is_an_honest_empty_page(self, tmp_path: Path) -> None:
        target = tmp_path / "notes.txt"
        target.write_text("one\ntwo\n", encoding="utf-8")
        document = _ok_payload(
            _run_helper(
                str(target),
                json.dumps([str(tmp_path)]),
                "9",
                "3",
                str(MAX_CONTENT_BYTES),
            )
        )
        assert document == {"content": "", "total": 2, "returned": 0}

    def test_empty_file_is_an_empty_page(self, tmp_path: Path) -> None:
        target = tmp_path / "empty.txt"
        target.write_text("", encoding="utf-8")
        document = _ok_payload(
            _run_helper(
                str(target),
                json.dumps([str(tmp_path)]),
                "0",
                "3",
                str(MAX_CONTENT_BYTES),
            )
        )
        assert document == {"content": "", "total": 0, "returned": 0}

    def test_final_line_without_newline_is_returned(self, tmp_path: Path) -> None:
        target = tmp_path / "notes.txt"
        target.write_text("one\ntwo\nthree", encoding="utf-8")
        document = _ok_payload(
            _run_helper(
                str(target),
                json.dumps([str(tmp_path)]),
                "2",
                "5",
                str(MAX_CONTENT_BYTES),
            )
        )
        assert document == {"content": "three", "total": 3, "returned": 1}

    def test_byte_budget_cuts_the_page_at_a_line_boundary(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "notes.txt"
        target.write_text("aaa\nbbbb\ncc\n", encoding="utf-8")
        document = _ok_payload(
            _run_helper(str(target), json.dumps([str(tmp_path)]), "0", "10", "6")
        )
        # "aaa\n" fits (4 bytes); "bbbb\n" would exceed the 6-byte budget.
        assert document == {"content": "aaa\n", "total": 3, "returned": 1}

    def test_invalid_utf8_in_the_page_is_a_structured_rejection(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "blob.bin"
        target.write_bytes(b"ok\n\xff\xfe\n")
        _refused(
            _run_helper(
                str(target),
                json.dumps([str(tmp_path)]),
                "0",
                "10",
                str(MAX_CONTENT_BYTES),
            ),
            "decode_error",
        )

    def test_invalid_utf8_outside_the_page_does_not_block_other_pages(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "mixed.txt"
        target.write_bytes(b"ok\n\xff\xfe\n")
        document = _ok_payload(
            _run_helper(
                str(target),
                json.dumps([str(tmp_path)]),
                "0",
                "1",
                str(MAX_CONTENT_BYTES),
            )
        )
        assert document == {"content": "ok\n", "total": 2, "returned": 1}

    def test_missing_file_is_not_found(self, tmp_path: Path) -> None:
        _refused(
            _run_helper(
                str(tmp_path / "gone.txt"),
                json.dumps([str(tmp_path)]),
                "0",
                "3",
                str(MAX_CONTENT_BYTES),
            ),
            "path_not_found",
        )

    def test_directory_target_is_not_a_regular_file(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        _refused(
            _run_helper(
                str(tmp_path / "sub"),
                json.dumps([str(tmp_path)]),
                "0",
                "3",
                str(MAX_CONTENT_BYTES),
            ),
            "not_regular_file",
        )

    def test_filesystem_root_target_is_refused(self, tmp_path: Path) -> None:
        _refused(
            _run_helper("/", json.dumps([str(tmp_path)]), "0", "3", "1000"),
            "not_regular_file",
        )

    def test_symlink_final_component_is_denied(self, tmp_path: Path) -> None:
        real = tmp_path / "real.txt"
        real.write_text("data\n", encoding="utf-8")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        _refused(
            _run_helper(
                str(link),
                json.dumps([str(tmp_path)]),
                "0",
                "3",
                str(MAX_CONTENT_BYTES),
            ),
            "path_denied",
        )

    def test_symlink_escape_to_outside_the_root_is_denied(
        self, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
        root = tmp_path / "root"
        root.mkdir()
        (root / "esc").symlink_to(outside)
        _refused(
            _run_helper(
                str(root / "esc" / "secret.txt"),
                json.dumps([str(root)]),
                "0",
                "3",
                str(MAX_CONTENT_BYTES),
            ),
            "path_denied",
        )

    def test_sibling_prefix_root_boundary_is_enforced(self, tmp_path: Path) -> None:
        (tmp_path / "data").mkdir()
        datax = tmp_path / "datax"
        datax.mkdir()
        (datax / "f.txt").write_text("x\n", encoding="utf-8")
        _refused(
            _run_helper(
                str(datax / "f.txt"),
                json.dumps([str(tmp_path / "data")]),
                "0",
                "3",
                str(MAX_CONTENT_BYTES),
            ),
            "path_denied",
        )

    def test_relative_root_argument_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "notes.txt"
        target.write_text("one\n", encoding="utf-8")
        _refused(
            _run_helper(
                str(target), json.dumps(["relative/root"]), "0", "3", "1000"
            ),
            "path_denied",
        )

    def test_empty_roots_argument_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "notes.txt"
        target.write_text("one\n", encoding="utf-8")
        _refused(
            _run_helper(str(target), json.dumps([]), "0", "3", "1000"),
            "path_denied",
        )

    def test_relative_path_argument_is_refused(self, tmp_path: Path) -> None:
        _refused(
            _run_helper("relative.txt", json.dumps([str(tmp_path)]), "0", "3", "1000"),
            "invalid_request",
        )

    def test_wrong_argv_shape_is_refused(self) -> None:
        _refused(_run_helper("/tmp/x", "[]"), "invalid_request")
        _refused(
            _run_helper("/tmp/x", "[]", "0", "3", "1000", "extra"),
            "invalid_request",
        )

    def test_non_integer_offset_argument_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "notes.txt"
        target.write_text("one\n", encoding="utf-8")
        _refused(
            _run_helper(
                str(target),
                json.dumps([str(tmp_path)]),
                "zero",
                "3",
                str(MAX_CONTENT_BYTES),
            ),
            "invalid_request",
        )

    def test_end_to_end_quoting_through_the_real_transport_composer(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        target = root / "a;b|$(x).txt"
        target.write_text("metachars\n", encoding="utf-8")
        query = read_query_from_payload(
            {"path": str(target)}, file_read_roots=(str(root),)
        )
        request = CommandRequest(
            op_id=uuid.uuid4(),
            server_id=SERVER_A,
            display=DISPLAY_LABEL,
            timeout=30.0,
            output_budget=TRANSPORT_OUTPUT_BUDGET_BYTES,
            argv=compose_read_argv(query),
        )
        # ssh(1) hands this exact string to the target default shell;
        # sh must reconstruct the structured argv, never interpret it.
        result = subprocess.run(
            ["sh", "-c", compose_remote_command(request)],
            capture_output=True,
            timeout=60,
            check=False,
        )
        document = _ok_payload(result)
        assert document == {"content": "metachars\n", "total": 1, "returned": 1}


# ---------------------------------------------------------------------------
# Executor wiring: pre-effect refusals and honest documents
# ---------------------------------------------------------------------------


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
        with pytest.raises(FileReadError) as excinfo:
            harness.run()
        assert excinfo.value.code is FileReadErrorCode.PROFILE_UNAVAILABLE
        assert harness.transport.calls == []

    def test_non_profile_answer_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        harness.profile = {"not": "a profile"}  # type: ignore[assignment]
        with pytest.raises(FileReadError) as excinfo:
            harness.run()
        assert excinfo.value.code is FileReadErrorCode.PROFILE_UNAVAILABLE
        assert harness.transport.calls == []

    def test_profile_identity_mismatch_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        harness.profile = _profile(server_id=uuid.uuid4())
        with pytest.raises(FileReadError) as excinfo:
            harness.run()
        assert excinfo.value.code is FileReadErrorCode.PROFILE_MISMATCH
        assert harness.transport.calls == []

    def test_missing_credential_reference_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        with pytest.raises(FileReadError) as excinfo:
            harness.run(credentials={})
        assert excinfo.value.code is FileReadErrorCode.CREDENTIAL_UNAVAILABLE
        assert harness.transport.calls == []

    def test_unresolvable_key_file_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        with pytest.raises(FileReadError) as excinfo:
            harness.run(
                credentials={CREDENTIAL_REF: _secret(str(tmp_path / "gone"))}
            )
        assert excinfo.value.code is FileReadErrorCode.KEY_FILE_UNAVAILABLE
        assert harness.transport.calls == []

    def test_unenrolled_known_hosts_refuse(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        harness.known_hosts = tmp_path / "absent"  # type: ignore[assignment]
        with pytest.raises(FileReadError) as excinfo:
            harness.run()
        assert excinfo.value.code is FileReadErrorCode.HOST_KEY_UNENROLLED
        assert harness.transport.calls == []

    def test_empty_profile_allowlist_refuses_without_transport(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        harness.profile = _profile(file_read_roots=())
        with pytest.raises(FileReadError) as excinfo:
            harness.run()
        assert excinfo.value.code is FileReadErrorCode.PATH_DENIED
        assert harness.transport.calls == []

    def test_invalid_payload_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        with pytest.raises(FileReadError) as excinfo:
            harness.run(payload={"path": "/etc/passwd"})
        assert excinfo.value.code is FileReadErrorCode.PATH_DENIED
        assert harness.transport.calls == []

    def test_non_call_input_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = self._harness(monkeypatch, tmp_path)
        with pytest.raises(FileReadError) as excinfo:
            asyncio.run(harness.capability({"path": "/srv/data/x"}))  # type: ignore[arg-type]
        assert excinfo.value.code is FileReadErrorCode.INVALID_CONTEXT
        assert harness.transport.calls == []


class TestReadComposition:
    def test_succeeded_call_yields_the_exact_page_document(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.transport.script = [
            _outcome(
                stdout=json.dumps(
                    {"content": "alpha\nbeta\n", "total": 5, "returned": 2}
                )
            )
        ]
        result = harness.run()
        assert result.ok is True
        assert result.error_code is None
        document = json.loads(result.output)
        assert document == {
            "path": "/srv/data/notes.txt",
            "content": "alpha\nbeta\n",
            "offset": 0,
            "limit": DEFAULT_LIMIT,
            "total": 5,
            "has_more": True,
        }
        (identity, request, key_path, known_hosts) = harness.transport.calls[0]
        query = read_query_from_payload(GOOD_PAYLOAD, file_read_roots=(ALLOWED_ROOT,))
        assert request.argv == compose_read_argv(query)
        assert request.display == DISPLAY_LABEL
        assert request.output_budget == TRANSPORT_OUTPUT_BUDGET_BYTES
        assert request.timeout == 30.0
        assert request.working_directory is None
        assert key_path == str(harness.key_file)
        assert known_hosts == str(harness.known_hosts)

    def test_last_page_reports_has_more_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.transport.script = [
            _outcome(
                stdout=json.dumps(
                    {"content": "four\nfive\n", "total": 5, "returned": 2}
                )
            )
        ]
        result = harness.run({"path": "/srv/data/notes.txt", "offset": 3})
        document = json.loads(result.output)
        assert document["offset"] == 3
        assert document["total"] == 5
        assert document["has_more"] is False

    def test_structured_remote_rejection_maps_onto_the_vocabulary(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.transport.script = [
            _outcome(
                status=CommandStatus.FAILED,
                exit_code=3,
                stdout=json.dumps({"error": "path_not_found"}) + "\n",
            )
        ]
        result = harness.run()
        assert result.ok is False
        assert result.error_code == "path_not_found"
        document = json.loads(result.output)
        assert document == {
            "path": "/srv/data/notes.txt",
            "status": "refused",
            "code": "path_not_found",
        }

    def test_transport_timeout_is_an_honest_unknown_outcome(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.transport.script = [
            _outcome(status=CommandStatus.OUTCOME_UNKNOWN, exit_code=None)
        ]
        result = harness.run()
        assert result.ok is False
        assert result.error_code is None
        document = json.loads(result.output)
        assert document["status"] == "outcome_unknown"

    def test_unreadable_helper_response_fails_without_content(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.transport.script = [_outcome(stdout="not json at all")]
        result = harness.run()
        assert result.ok is False
        assert result.error_code is None
        assert "content" not in json.loads(result.output)

    def test_unknown_helper_error_code_fails_honestly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _Harness(monkeypatch, tmp_path)
        harness.transport.script = [
            _outcome(
                status=CommandStatus.FAILED,
                exit_code=3,
                stdout=json.dumps({"error": "mystery_code"}),
            )
        ]
        result = harness.run()
        assert result.ok is False
        assert result.error_code is None
        assert json.loads(result.output)["status"] == "failed"


# ---------------------------------------------------------------------------
# Profile schema extension (file_read_roots, backward compatible)
# ---------------------------------------------------------------------------


def _profile_doc(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "server_id": str(SERVER_A),
        "name": "fixture",
        "prompt_revision": 1,
        "environment": "linux_ssh",
        "ssh": {
            "host": "127.0.0.1",
            "port": 22,
            "user": "mai-fxt",
            "host_key": "SHA256:" + "A" * 43,
            "credential_ref": CREDENTIAL_REF,
        },
        "privilege": "none",
        "capabilities": [CAPABILITY_ID],
    }
    document.update(overrides)  # type: ignore[arg-type]
    return document


class TestProfileFileReadRootsSchema:
    def test_profile_without_the_field_defaults_to_no_roots(self) -> None:
        profile = parse_server_profile(_profile_doc())
        assert profile.file_read_roots == ()

    def test_valid_roots_roundtrip(self) -> None:
        profile = parse_server_profile(
            _profile_doc(file_read_roots=["/srv/data", "/var/log/app"])
        )
        assert profile.file_read_roots == ("/srv/data", "/var/log/app")

    def test_explicitly_empty_roots_mean_deny_all(self) -> None:
        profile = parse_server_profile(_profile_doc(file_read_roots=[]))
        assert profile.file_read_roots == ()

    @pytest.mark.parametrize(
        "roots",
        [
            "not-a-list",
            ["/srv/data", 7],
            ["/srv/data"] * 65,
            ["relative/path"],
            ["/srv/data/"],
            ["/"],
            ["/srv/data/.."],
            ["/srv/data/./sub"],
            ["/srv/data\x00hidden"],
            ["/srv/data/sub\n"],
            ["/srv/data", "/srv/data"],
        ],
    )
    def test_invalid_roots_reject_the_document(self, roots) -> None:
        with pytest.raises(ProfileValidationError) as excinfo:
            parse_server_profile(_profile_doc(file_read_roots=roots))
        assert excinfo.value.fields.get("file_read_roots") == "invalid_field"

    def test_unknown_sibling_fields_are_still_rejected(self) -> None:
        with pytest.raises(ProfileValidationError) as excinfo:
            parse_server_profile(_profile_doc(file_read_allowlist=["/srv/data"]))
        assert excinfo.value.fields.get("file_read_allowlist") == "unknown_field"


# ---------------------------------------------------------------------------
# Wiring: profile slug normalization stays in sync across surfaces
# ---------------------------------------------------------------------------


class TestWiring:
    def test_chat_profile_map_routes_both_file_read_slugs(self) -> None:
        from mai.api.chat import SERVER_PROFILE_CAPABILITY_MAP

        assert SERVER_PROFILE_CAPABILITY_MAP["file_read"] == CAPABILITY_ID
        assert SERVER_PROFILE_CAPABILITY_MAP["host.file_read"] == CAPABILITY_ID

    def test_worker_lane_wires_executor_and_profile_grants(
        self, tmp_path: Path
    ) -> None:
        """The REAL worker composition (``_configure_default_agent_runtime``)
        registers the executor AND surfaces the grant from a profile that
        declares ``file_read``: a dispatch for a profile-declaring server
        reaches OUR executor (it raises ``FileReadError`` — the composite
        reports a missing executor as plain ``ok=False``, never
        ``executor_failed``), while a server without the slug is refused
        as ``capability_not_granted``. Runs in a subprocess: the runtime
        is process-global. The database URL stays lazy (never dialed)."""
        sid_granted = uuid.uuid4()
        sid_plain = uuid.uuid4()
        data_root = tmp_path / "data"

        def _profile_text(server_id: uuid.UUID, *, granted: bool) -> str:
            capabilities = "  - file_read" if granted else "  - host_shell"
            roots = "file_read_roots:\n  - /srv/data\n" if granted else ""
            return (
                "schema_version: 1\n"
                f"server_id: {server_id}\n"
                "name: probe\n"
                "prompt_revision: 1\n"
                "environment: linux_ssh\n"
                "ssh:\n"
                "  host: 127.0.0.1\n"
                "  port: 22\n"
                "  user: probe\n"
                f"  host_key: SHA256:{'A' * 43}\n"
                "  credential_ref: MAI_SSH_KEY_PROBE\n"
                "privilege: none\n"
                "capabilities:\n"
                f"{capabilities}\n"
                f"{roots}"
            )

        # Profiles are created through the REAL canonical store (the
        # hash-verified metadata sidecar is part of enrollment).
        from mai.profiles.files import ProfileFileStore

        store = ProfileFileStore(data_root)
        for server_id, granted in (
            (sid_granted, True),
            (sid_plain, False),
        ):
            store.write(server_id, _profile_text(server_id, granted=granted), expected_revision=0)
        script = tmp_path / "worker_probe.py"
        script.write_text(_WORKER_PROBE_SCRIPT, encoding="utf-8")
        base_env = dict(os.environ)
        base_env["MAI_DATA_ROOT"] = str(data_root)
        base_env["MAI_DATABASE_URL"] = "postgresql://unused:unused@127.0.0.1:1/unused"
        base_env["MAI_ENV_FILE"] = ""
        base_env.pop("MAI_WORKER_HANDLERS", None)

        def _probe(server_id: uuid.UUID) -> dict[str, object]:
            result = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                env={**base_env, "PROBE_SERVER_ID": str(server_id)},
                cwd=BACKEND_ROOT,
                check=False,
                timeout=180,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            return json.loads(result.stdout)  # type: ignore[no-any-return]

        granted = _probe(sid_granted)
        assert granted["reason"] == "executor_failed"
        assert granted["error_type"] == "FileReadError"

        plain = _probe(sid_plain)
        # host.file_read exists in the grant map but is scoped to the
        # other server only — the policy vocabulary for that refusal is
        # server_scope_mismatch; either way the executor never ran.
        assert plain["reason"] == "server_scope_mismatch"
        assert plain["allowed"] is False
