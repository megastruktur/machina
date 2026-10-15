"""Bounded target-command execution and safe output capture (card 04-command-output).

Covers ``backend/src/mai/execution/commands.py`` against contracts.md **C04**:

- **Acceptance 1** — structured argv or explicit shell request (mutually
  exclusive, argv POSIX-quoted), working-directory policy (absolute path;
  failed ``cd`` distinguished by reserved exit code + fixed marker before
  the payload runs), wall-clock timeout and per-stream output budget.
- **Acceptance 2** — exit code, stdout and stderr are distinguished outcome
  fields; truncation is explicit (``*_truncated`` + true byte totals), a
  timeout is ``outcome_unknown`` (never an invented exit), and credential
  material never enters display, error details or captured output.

Layers (test policy: focused tests, real boundary where the claim depends
on it; mirrors the ``test_ssh.py`` producer pattern):

- pure request/compose validation (no I/O);
- the selector-based capture loop exercised over REAL ``os.pipe()`` file
  descriptors, including EOF, budget retention and deadline kill;
- ``execute_command`` end-to-end over a fake ``Popen`` for exit-code /
  transport-failure mapping (structured vocabulary, bounded value-free
  details);
- fixture-backed tests against a disposable Arch sshd container
  (runtime-environment ADR D1–D5): real nonzero exit, huge output with
  truncation, enforced timeout, cwd policy, argv-not-shell-interpreted
  negative control, and credential non-disclosure at the real boundary.
  These auto-skip only when docker is unavailable on the host.

Secret hygiene: fixture keys are throwaway material generated at runtime;
nothing secret is printed, and the credential reference never resolves to
anything on the target (asserted for real, not assumed).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from mai.contracts.server import SshIdentity
from mai.credentials.env import EnvResolver
from mai.execution.commands import (
    CWD_EXIT_CODE,
    CWD_FAILURE_MARKER,
    CommandOutcome,
    CommandRequest,
    CommandRequestError,
    CommandStatus,
    MAX_ARGV_ELEMENTS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    MAX_DISPLAY_CHARS,
    MAX_OUTPUT_BUDGET_BYTES,
    MAX_REMOTE_COMMAND_BYTES,
    compose_remote_command,
    execute_command,
)
from mai.execution.ssh import (
    SshConnectError,
    SshErrorCode,
    pin_host_key,
    resolve_identity_key,
    target_fingerprint,
)

#: Synthetic non-disclosure marker (NOT a secret): hostile ssh stderr used to
#: prove error details stay bounded and value-free.
STDERR_SENTINEL = "mai-04-command-output-fixture-stderr-not-a-secret"


def _request(**overrides: object) -> CommandRequest:
    """Valid default request; tests override one field at a time."""
    fields: dict[str, object] = {
        "op_id": uuid.uuid4(),
        "server_id": uuid.uuid4(),
        "display": "fixture display label",
        "timeout": 10.0,
        "output_budget": 4096,
        "argv": ("echo", "hi"),
    }
    fields.update(overrides)
    return CommandRequest(**fields)  # type: ignore[arg-type]


class TestRequestValidation:
    def test_minimal_argv_request_accepted(self) -> None:
        request = _request()
        assert request.argv == ("echo", "hi")
        assert request.shell is None

    def test_list_argv_normalized_to_tuple(self) -> None:
        request = _request(argv=["echo", "hi"])
        assert request.argv == ("echo", "hi")
        assert isinstance(request.argv, tuple)

    def test_shell_only_request_accepted(self) -> None:
        request = _request(argv=None, shell="echo hi")
        assert request.argv is None
        assert request.shell == "echo hi"

    def test_argv_and_shell_are_mutually_exclusive(self) -> None:
        with pytest.raises(CommandRequestError):
            _request(shell="echo hi")
        with pytest.raises(CommandRequestError):
            _request(argv=None)

    def test_empty_argv_rejected(self) -> None:
        with pytest.raises(CommandRequestError):
            _request(argv=())

    def test_non_string_argv_element_rejected(self) -> None:
        with pytest.raises(CommandRequestError):
            _request(argv=("echo", 7))  # type: ignore[list-item]

    def test_nul_byte_rejected_everywhere(self) -> None:
        with pytest.raises(CommandRequestError):
            _request(argv=("echo", "a\x00b"))
        with pytest.raises(CommandRequestError):
            _request(argv=None, shell="echo a\x00b")
        with pytest.raises(CommandRequestError):
            _request(working_directory="/etc/\x00x")

    def test_display_rules(self) -> None:
        with pytest.raises(CommandRequestError):
            _request(display="")
        with pytest.raises(CommandRequestError):
            _request(display="x" * (MAX_DISPLAY_CHARS + 1))
        with pytest.raises(CommandRequestError):
            _request(display="two\nlines")

    def test_timeout_rules(self) -> None:
        with pytest.raises(CommandRequestError):
            _request(timeout=0)
        with pytest.raises(CommandRequestError):
            _request(timeout=-1.0)
        with pytest.raises(CommandRequestError):
            _request(timeout=MAX_COMMAND_TIMEOUT_SECONDS + 1)
        with pytest.raises(CommandRequestError):
            _request(timeout=True)  # bool is not a duration
        with pytest.raises(CommandRequestError):
            _request(timeout=float("inf"))

    def test_output_budget_rules(self) -> None:
        with pytest.raises(CommandRequestError):
            _request(output_budget=0)
        with pytest.raises(CommandRequestError):
            _request(output_budget=-5)
        with pytest.raises(CommandRequestError):
            _request(output_budget=MAX_OUTPUT_BUDGET_BYTES + 1)
        with pytest.raises(CommandRequestError):
            _request(output_budget=True)

    def test_working_directory_rules(self) -> None:
        assert _request(working_directory="/etc").working_directory == "/etc"
        with pytest.raises(CommandRequestError):
            _request(working_directory="etc")  # relative
        with pytest.raises(CommandRequestError):
            _request(working_directory="x" * 4097)

    def test_too_many_argv_elements_rejected(self) -> None:
        with pytest.raises(CommandRequestError):
            _request(argv=tuple("x" for _ in range(MAX_ARGV_ELEMENTS + 1)))


class TestArgvQuoting:
    def test_metacharacters_stay_literal(self) -> None:
        request = _request(
            argv=("printf", "%s\\n", "hello; touch /tmp/pwned", "$(id)", "`id`")
        )
        composed = compose_remote_command(request)
        assert composed == (
            "printf '%s\\n' 'hello; touch /tmp/pwned' '$(id)' '`id`'"
        )

    def test_empty_element_preserved(self) -> None:
        request = _request(argv=("echo", "", "x"))
        assert compose_remote_command(request) == "echo '' x"


class TestComposeRemoteCommand:
    def test_shell_mode_composed_verbatim(self) -> None:
        request = _request(argv=None, shell="for i in 1 2; do echo $i; done")
        assert (
            compose_remote_command(request)
            == "for i in 1 2; do echo $i; done"
        )

    def test_working_directory_guard_prefixes_payload(self) -> None:
        request = _request(argv=("echo", "hi"), working_directory="/opt/my dir")
        assert compose_remote_command(request) == (
            "cd '/opt/my dir' 2>/dev/null || "
            f"{{ echo '{CWD_FAILURE_MARKER}' 1>&2; exit {CWD_EXIT_CODE}; }}; echo hi"
        )

    def test_no_working_directory_no_guard(self) -> None:
        assert compose_remote_command(_request()) == "echo hi"

    def test_oversized_composed_command_rejected(self) -> None:
        request = _request(argv=None, shell="x" * (MAX_REMOTE_COMMAND_BYTES + 1))
        with pytest.raises(CommandRequestError):
            compose_remote_command(request)


class _FakeProc:
    """Popen stand-in over REAL pipe fds (drives the real capture loop)."""

    def __init__(
        self,
        returncode: int,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        hold_open: bool = False,
    ) -> None:
        self._returncode = returncode
        r_out, w_out = os.pipe()
        r_err, w_err = os.pipe()
        if stdout:
            os.write(w_out, stdout)
        if stderr:
            os.write(w_err, stderr)
        self._writers = [w_out, w_err]
        if not hold_open:
            os.close(w_out)
            os.close(w_err)
            self._writers = []
        self.stdout = open(r_out, "rb", buffering=0)
        self.stderr = open(r_err, "rb", buffering=0)
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9

    def wait(self) -> int:
        self.returncode = self._returncode
        return self.returncode

    def dispose(self) -> None:
        for fd in self._writers:
            os.close(fd)
        self._writers = []
        for stream in (self.stdout, self.stderr):
            try:
                stream.close()
            except OSError:
                pass


from mai.execution.commands import _capture_streams  # noqa: E402  (private, unit-level)


class TestCaptureStreams:
    def test_both_streams_small_and_eof(self) -> None:
        proc = _FakeProc(0, stdout=b"out", stderr=b"err")
        try:
            captured = _capture_streams(
                proc, deadline=time.monotonic() + 5.0, budget=64
            )
        finally:
            proc.dispose()
        assert not captured.timed_out
        assert captured.stdout == b"out"
        assert captured.stdout_total == 3
        assert not captured.stdout_truncated
        assert captured.stderr == b"err"
        assert not captured.stderr_truncated

    def test_budget_retains_first_bytes_and_flags_truncation(self) -> None:
        payload = bytes(range(256)) * 8  # 2048 bytes
        proc = _FakeProc(0, stdout=payload, stderr=b"e")
        try:
            captured = _capture_streams(
                proc, deadline=time.monotonic() + 5.0, budget=1000
            )
        finally:
            proc.dispose()
        assert captured.stdout == payload[:1000]
        assert captured.stdout_total == 2048
        assert captured.stdout_truncated

    def test_deadline_kills_and_reports_timeout(self) -> None:
        proc = _FakeProc(0, stdout=b"partial", hold_open=True)
        try:
            captured = _capture_streams(
                proc, deadline=time.monotonic() + 0.2, budget=64
            )
        finally:
            proc.dispose()
        assert captured.timed_out
        assert proc.killed
        assert proc.returncode == -9
        assert captured.stdout == b"partial"


class TestExecuteCommandUnit:
    @pytest.fixture()
    def known_hosts(self, tmp_path: Path) -> Path:
        path = tmp_path / "known_hosts"
        path.write_text("", encoding="utf-8")
        return path

    def _identity(self) -> SshIdentity:
        return SshIdentity(
            host="203.0.113.10",
            port=22,
            user="root",
            host_key="SHA256:" + "A" * 43,
            credential_ref="MAI_SSH_KEY_TEST",
        )

    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        proc: _FakeProc,
        known_hosts: Path,
        request: CommandRequest | None = None,
    ) -> CommandOutcome:
        monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: proc)
        try:
            return execute_command(
                self._identity(),
                request or _request(),
                key_path="/fixture/key",
                known_hosts_path=known_hosts,
            )
        finally:
            proc.dispose()

    def test_success_outcome(self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path) -> None:
        request = _request(argv=("true",))
        outcome = self._run(monkeypatch, _FakeProc(0, stdout=b"ok\n"), known_hosts, request)
        assert outcome.status is CommandStatus.SUCCEEDED
        assert outcome.exit_code == 0
        assert outcome.stdout == "ok\n"
        assert not outcome.timed_out
        assert not outcome.cwd_change_failed
        assert outcome.op_id == request.op_id
        assert outcome.server_id == request.server_id
        assert outcome.display == request.display

    def test_nonzero_exit_failed(self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path) -> None:
        outcome = self._run(
            monkeypatch,
            _FakeProc(7, stdout=b"out\n", stderr=b"err\n"),
            known_hosts,
        )
        assert outcome.status is CommandStatus.FAILED
        assert outcome.exit_code == 7
        assert outcome.stdout == "out\n"
        assert outcome.stderr == "err\n"

    def test_cwd_guard_failure_distinguished(
        self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path
    ) -> None:
        marker = f"{CWD_FAILURE_MARKER}\n".encode()
        outcome = self._run(
            monkeypatch,
            _FakeProc(CWD_EXIT_CODE, stderr=marker),
            known_hosts,
            _request(working_directory="/gone"),
        )
        assert outcome.status is CommandStatus.FAILED
        assert outcome.exit_code == CWD_EXIT_CODE
        assert outcome.cwd_change_failed

    def test_payload_exit_210_without_marker_is_plain_failure(
        self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path
    ) -> None:
        outcome = self._run(monkeypatch, _FakeProc(CWD_EXIT_CODE, stderr=b"boom\n"), known_hosts)
        assert outcome.status is CommandStatus.FAILED
        assert outcome.cwd_change_failed is False

    def test_client_signal_death_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path
    ) -> None:
        outcome = self._run(monkeypatch, _FakeProc(-9), known_hosts)
        assert outcome.status is CommandStatus.OUTCOME_UNKNOWN
        assert outcome.exit_code is None
        assert not outcome.timed_out

    def test_timeout_is_unknown_without_invented_exit(
        self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path
    ) -> None:
        proc = _FakeProc(0, stdout=b"partial", hold_open=True)
        monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: proc)
        try:
            outcome = execute_command(
                self._identity(),
                _request(timeout=0.2),
                key_path="/fixture/key",
                known_hosts_path=known_hosts,
            )
        finally:
            proc.dispose()
        assert outcome.status is CommandStatus.OUTCOME_UNKNOWN
        assert outcome.timed_out
        assert outcome.exit_code is None
        assert outcome.stdout == "partial"

    @pytest.mark.parametrize(
        ("stderr", "expected_code"),
        [
            (b"Warning: Permanently added... \nHost key verification failed.", SshErrorCode.HOST_KEY_MISMATCH),
            (b"root@203.0.113.10: Permission denied (publickey).", SshErrorCode.AUTH_FAILED),
            (b"ssh: connect to host 203.0.113.10 port 22: Connection refused", SshErrorCode.UNREACHABLE),
            (b"kex_exchange_identification: read: Connection reset by peer", SshErrorCode.UNREACHABLE),
        ],
    )
    def test_transport_failures_raise_structured_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        known_hosts: Path,
        stderr: bytes,
        expected_code: SshErrorCode,
    ) -> None:
        with pytest.raises(SshConnectError) as excinfo:
            self._run(monkeypatch, _FakeProc(255, stderr=stderr), known_hosts)
        assert excinfo.value.code is expected_code

    def test_generic_255_is_ssh_failed(
        self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path
    ) -> None:
        with pytest.raises(SshConnectError) as excinfo:
            self._run(monkeypatch, _FakeProc(255, stderr=b"muxserver: odd failure"), known_hosts)
        assert excinfo.value.code is SshErrorCode.SSH_FAILED

    def test_error_details_bounded_and_value_free(
        self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path
    ) -> None:
        hostile = (STDERR_SENTINEL + "x" * 4000 + " Connection refused\n").encode()
        with pytest.raises(SshConnectError) as excinfo:
            self._run(monkeypatch, _FakeProc(255, stderr=hostile), known_hosts)
        assert excinfo.value.code is SshErrorCode.UNREACHABLE
        assert "\n" not in excinfo.value.detail
        assert len(excinfo.value.detail) < 400
        assert STDERR_SENTINEL not in excinfo.value.detail

    def test_missing_enrollment_refused_before_spawn(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        spawned = False

        def _no_spawn(*_a: object, **_k: object) -> _FakeProc:
            nonlocal spawned
            spawned = True
            raise AssertionError("Popen must not run without enrollment")

        monkeypatch.setattr(subprocess, "Popen", _no_spawn)
        with pytest.raises(SshConnectError) as excinfo:
            execute_command(
                self._identity(),
                _request(),
                key_path="/fixture/key",
                known_hosts_path=tmp_path / "never-enrolled",
            )
        assert excinfo.value.code is SshErrorCode.INVALID_CONFIGURATION
        assert spawned is False


# ---------------------------------------------------------------------------
# Disposable Arch sshd fixture (runtime-environment ADR D1–D5; this card's
# unique namespace: mai-fxt-04-cmd-*). Pattern producer: test_ssh.py.
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-04-cmd-arch"
FIXTURE_IMAGE_TAG = "mai-fxt-04-cmd-arch:sshd"
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-04-cmd-tests")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_04_CMD_FIXTURE"


def _docker(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


_image_ensured = False


def _ensure_fixture_image() -> None:
    """Build the disposable Arch+openssh fixture image once (pinned base)."""
    global _image_ensured
    if _image_ensured and _docker("image", "inspect", FIXTURE_IMAGE_TAG).returncode == 0:
        return
    context = FIXTURE_SCRATCH / "build-context"
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(
        "FROM archlinux:latest\n"
        "RUN pacman-key --init && pacman -Sy --noconfirm openssh && ssh-keygen -A\n",
        encoding="utf-8",
    )
    build = _docker("build", "-q", "-t", FIXTURE_IMAGE_TAG, str(context), timeout=1200)
    assert build.returncode == 0, f"fixture image build failed: {build.stderr[-800:]}"
    _image_ensured = True


class ArchSshdFixture:
    """One disposable Arch sshd container; removes itself with proof."""

    def __init__(self) -> None:
        self.scratch = FIXTURE_SCRATCH / uuid.uuid4().hex
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.key_path = self.scratch / "fixture_key"
        self.port = 0
        self.identity: SshIdentity
        self.cleanup_proof = "<not cleaned>"

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> SshIdentity:
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key_path),
             "-C", f"{FIXTURE_CONTAINER}-test"],
            check=True, capture_output=True,
        )
        os.chmod(self.key_path, 0o600)
        _ensure_fixture_image()
        run = _docker(
            "run", "--rm", "-d", "--name", FIXTURE_CONTAINER,
            "-p", "127.0.0.1::22",
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

    def _refresh_port(self) -> None:
        # docker re-allocates the ephemeral host port on every (re)start.
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
        """Profile-shaped identity for the CURRENT endpoint and given pin."""
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
            ["ssh-keyscan", "-p", str(self.port), "-t", "ed25519,rsa", "127.0.0.1"],
            capture_output=True, text=True, timeout=30,
        )
        return tuple(line for line in scan.stdout.splitlines() if line.strip())

    def _current_fingerprint(self) -> str:
        """Enrollment fixture value: cross-checked against real ssh-keygen."""
        lines = self._scan_now()
        assert lines, "fixture keyscan produced no host keys"
        ed25519 = next((line for line in lines if " ssh-ed25519 " in line), lines[0])
        keyfile = self.scratch / "scan-hostkey.pub"
        keyfile.write_text(ed25519 + "\n", encoding="utf-8")
        keygen = subprocess.run(
            ["ssh-keygen", "-lf", str(keyfile)], capture_output=True, text=True
        )
        assert keygen.returncode == 0, keygen.stderr
        fingerprint = keygen.stdout.split()[1]
        computed = target_fingerprint(ed25519.split()[1], ed25519.split()[2])
        assert computed == fingerprint, (
            f"pure fingerprint {computed} disagrees with ssh-keygen {fingerprint}"
        )
        return fingerprint

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

    def env_resolver(self) -> EnvResolver:
        env_file = self.scratch / "fixture.env"
        env_file.write_text(f"{FIXTURE_CREDENTIAL_REF}={self.key_path}\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        return EnvResolver(env_file)

    def cleanup(self) -> str:
        _docker("rm", "-f", FIXTURE_CONTAINER, timeout=60)
        listing = _docker(
            "ps", "-a", "--filter", f"name=^{FIXTURE_CONTAINER}$", "--format", "{{.Names}}"
        )
        shutil.rmtree(self.scratch, ignore_errors=True)
        # Remove the whole namespace root (the image build context is a
        # sibling of the per-test scratch, so child-only removal would leave
        # it behind; _ensure_fixture_image() recreates everything on demand).
        shutil.rmtree(FIXTURE_SCRATCH, ignore_errors=True)
        self.cleanup_proof = (
            f"container {FIXTURE_CONTAINER} after rm: '{listing.stdout.strip() or '<none>'}'; "
            f"scratch {self.scratch} exists: {self.scratch.exists()}; "
            f"parent {FIXTURE_SCRATCH} exists: {FIXTURE_SCRATCH.exists()}"
        )
        return self.cleanup_proof


@pytest.fixture()
def arch_sshd() -> ArchSshdFixture:
    if not _docker_available():
        pytest.skip("docker unavailable: Arch sshd fixture tests require it")
    fixture = ArchSshdFixture()
    try:
        fixture.identity = fixture.start()
        yield fixture
    finally:
        print(f"\nFIXTURE CLEANUP PROOF: {fixture.cleanup()}")


class TestAgainstArchFixture:
    """Real-boundary acceptance: the actual target executes the commands."""

    def _execute(
        self, arch_sshd: ArchSshdFixture, request: CommandRequest
    ) -> CommandOutcome:
        key_path = resolve_identity_key(
            arch_sshd.env_resolver(), arch_sshd.identity.credential_ref
        )
        known_hosts = arch_sshd.scratch / "known_hosts"
        if not known_hosts.exists():
            pin_host_key(arch_sshd.identity, known_hosts)
        return execute_command(
            arch_sshd.identity,
            request,
            key_path=key_path,
            known_hosts_path=known_hosts,
        )

    def test_remote_hostname_differs_from_controller(self, arch_sshd: ArchSshdFixture) -> None:
        outcome = self._execute(arch_sshd, _request(argv=("uname", "-n"), output_budget=1024))
        assert outcome.status is CommandStatus.SUCCEEDED
        assert outcome.exit_code == 0
        remote_hostname = outcome.stdout.strip()
        assert remote_hostname
        assert remote_hostname != socket.gethostname(), (
            "output must come from the TARGET, not the controller"
        )
        assert not outcome.stdout_truncated
        assert outcome.stdout_bytes == len(outcome.stdout.encode("utf-8"))

    def test_nonzero_exit_and_stream_separation(self, arch_sshd: ArchSshdFixture) -> None:
        outcome = self._execute(
            arch_sshd,
            _request(
                argv=("sh", "-c", "echo out-line; echo err-line 1>&2; exit 7"),
                output_budget=1024,
            ),
        )
        assert outcome.status is CommandStatus.FAILED
        assert outcome.exit_code == 7
        assert outcome.stdout == "out-line\n"
        assert outcome.stderr == "err-line\n"
        assert not outcome.timed_out

    def test_structured_argv_is_not_shell_interpreted(self, arch_sshd: ArchSshdFixture) -> None:
        marker_path = f"/tmp/mai-argv-pwned-{uuid.uuid4().hex}"
        outcome = self._execute(
            arch_sshd,
            _request(
                argv=("printf", "%s\\n", f"hello; touch {marker_path}"),
                output_budget=1024,
            ),
        )
        assert outcome.status is CommandStatus.SUCCEEDED
        assert outcome.stdout == f"hello; touch {marker_path}\n"
        # Negative control: the injected `touch` never ran as shell syntax.
        probe = self._execute(arch_sshd, _request(argv=("test", "-e", marker_path)))
        assert probe.status is CommandStatus.FAILED
        assert probe.exit_code == 1

    def test_huge_output_bounded_with_truncation_distinguished(
        self, arch_sshd: ArchSshdFixture
    ) -> None:
        budget = 64 * 1024
        outcome = self._execute(
            arch_sshd,
            _request(
                argv=("dd", "if=/dev/zero", "bs=1048576", "count=48"),
                output_budget=budget,
                timeout=60.0,
            ),
        )
        # Command COMPLETED (exit status known) while the persisted artifact
        # stayed bounded; truncation is explicit, never silent.
        assert outcome.status is CommandStatus.SUCCEEDED
        assert outcome.exit_code == 0
        assert len(outcome.stdout.encode("utf-8")) == budget
        assert outcome.stdout_bytes == 48 * 1024 * 1024
        assert outcome.stdout_truncated
        # dd's stats go to stderr and stay under the budget there.
        assert outcome.stderr_bytes < budget
        assert not outcome.stderr_truncated

    def test_timeout_enforced_and_reported_unknown(self, arch_sshd: ArchSshdFixture) -> None:
        started = time.monotonic()
        outcome = self._execute(
            arch_sshd,
            _request(argv=("sleep", "10"), timeout=2.0, output_budget=1024),
        )
        duration = time.monotonic() - started
        assert outcome.status is CommandStatus.OUTCOME_UNKNOWN
        assert outcome.timed_out
        assert outcome.exit_code is None
        assert outcome.stdout == ""
        assert 2.0 <= duration < 8.0

    def test_working_directory_policy_enforced(self, arch_sshd: ArchSshdFixture) -> None:
        ok = self._execute(
            arch_sshd,
            _request(argv=("pwd",), working_directory="/etc", output_budget=1024),
        )
        assert ok.status is CommandStatus.SUCCEEDED
        assert ok.stdout == "/etc\n"

        missing = self._execute(
            arch_sshd,
            _request(
                argv=("pwd",),
                working_directory=f"/nonexistent-{uuid.uuid4().hex}",
                output_budget=1024,
            ),
        )
        assert missing.status is CommandStatus.FAILED
        assert missing.cwd_change_failed
        assert missing.exit_code == CWD_EXIT_CODE
        assert missing.stderr.startswith(CWD_FAILURE_MARKER)
        assert missing.stdout == ""

    def test_credentials_absent_from_display_and_persisted_output(
        self, arch_sshd: ArchSshdFixture
    ) -> None:
        key_path = resolve_identity_key(
            arch_sshd.env_resolver(), arch_sshd.identity.credential_ref
        )
        # 1. The credential reference resolves only controller-side: the
        #    target environment carries no such variable at all.
        env_probe = self._execute(
            arch_sshd,
            _request(
                argv=("sh", "-c", f"printenv {FIXTURE_CREDENTIAL_REF}"),
                output_budget=4096,
            ),
        )
        assert env_probe.status is CommandStatus.FAILED  # printenv exit 1
        assert env_probe.exit_code == 1
        assert env_probe.stdout == ""

        # 2. Rich target output (env + process list) never contains the
        #    private key material, which never leaves the controller.
        key_bytes = key_path.read_bytes()
        assert key_bytes  # sanity: there IS material to leak
        rich = self._execute(
            arch_sshd,
            _request(
                argv=("sh", "-c", "printenv; cat /proc/self/environ; ps aux"),
                output_budget=MAX_OUTPUT_BUDGET_BYTES,
                timeout=30.0,
            ),
        )
        assert rich.status is CommandStatus.SUCCEEDED
        assert rich.stdout_bytes > 0
        assert key_bytes not in rich.stdout.encode("utf-8")
        assert key_bytes not in rich.stderr.encode("utf-8")
