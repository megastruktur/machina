"""Explicit passwordless sudo execution mode (card 04-sudo).

Covers ``backend/src/mai/execution/privilege.py`` against contracts.md
**C04 rules 1/3** (profile privilege mode; privilege request) and **C03
rule 5** (``deny`` precedence; raw shell/root forfeits OS containment):

- **Acceptance 1** — NOPASSWD (``sudo`` profile mode) escalates
  NONINTERACTIVELY (``sudo -n --``, frozen in the composed argv) and a
  missing/insufficient grant is DETECTED as a structured
  :class:`SudoDeniedError` within the request budget instead of hanging —
  proven at the real boundary by removing the fixture sudoers grant.
- **Acceptance 2** — ``none`` (disabled) mode cannot request sudo: the
  refusal happens BEFORE composition/transport (negative control at the
  unit boundary: no process spawned; at the fixture boundary: real error
  with a working unprivileged lane). Actual root identity (uid 0) of the
  escalated payload is verified ONLY against the disposable fixture,
  never claimed of any other host; ``root`` profile mode is proven to add
  no escalation of its own.

Layers (test policy: focused, real boundary where the claim depends on it;
mirrors the ``test_commands.py`` producer pattern):

- pure composition: mode dispatch, noninteractive wrapper shape, payload
  quoted once (metacharacters stay literal), cwd guard moved INSIDE the
  escalation, budget enforcement, ``none`` refusal;
- pure denial classification: the start-marker rule (payload provably
  never started) over crafted outcomes, including the conservative
  non-denial corners (truncated stderr, started payload, other exits);
- ``execute_privileged_command`` unit tests over a fake ``Popen`` with
  REAL pipe fds (denial raises, payload failure returns, transport errors
  propagate, timeout stays ``outcome_unknown``);
- fixture-backed tests against a disposable Arch sshd container with an
  unprivileged account and a command-scoped NOPASSWD sudoers grant
  (runtime-environment ADR D1–D5, unique namespace ``mai-fxt-04-sudo-*``):
  real uid 0 under escalation, grant-removal denial without hanging,
  never-started negative control, recovery after grant restore, root-mode
  identity on the disposable target, credential non-disclosure under the
  escalated environment. These auto-skip only when docker is unavailable.

Secret hygiene: the lane has NO password input by construction (the
password channel is card ``04-password-channel`` via the approval store);
fixture keys are throwaway runtime material, nothing secret is printed,
and the fixture grant is NOPASSWD — no password value exists anywhere.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from mai.contracts.server import PrivilegeMode, SshIdentity
from mai.credentials.env import EnvResolver
from mai.execution.commands import (
    MAX_REMOTE_COMMAND_BYTES,
    CommandOutcome,
    CommandRequest,
    CommandRequestError,
    CommandStatus,
    compose_remote_command,
    execute_command,
)
from mai.execution.privilege import (
    PAYLOAD_START_MARKER,
    SUDO_PROGRAM,
    SUDO_WRAPPER_SHELL,
    PrivilegeErrorCode,
    PrivilegeRequestError,
    SudoDeniedError,
    _is_sudo_denial,  # noqa: E402  (private, unit-level classification rule)
    execute_privileged_command,
    privileged_remote_command,
)
from mai.execution.ssh import (
    SshConnectError,
    pin_host_key,
    resolve_identity_key,
    target_fingerprint,
)


def _request(**overrides: object) -> CommandRequest:
    """Valid default request; tests override one field at a time."""
    fields: dict[str, object] = {
        "op_id": uuid.uuid4(),
        "server_id": uuid.uuid4(),
        "display": "fixture display label",
        "timeout": 10.0,
        "output_budget": 4096,
        "argv": ("/usr/bin/id", "-u"),
    }
    fields.update(overrides)
    return CommandRequest(**fields)  # type: ignore[arg-type]


def _outcome(**overrides: object) -> CommandOutcome:
    """Valid default FAILED outcome; classification tests override fields."""
    fields: dict[str, object] = {
        "op_id": uuid.uuid4(),
        "server_id": uuid.uuid4(),
        "display": "outcome label",
        "status": CommandStatus.FAILED,
        "exit_code": 1,
        "stdout": "",
        "stdout_bytes": 0,
        "stdout_truncated": False,
        "stderr": "",
        "stderr_bytes": 0,
        "stderr_truncated": False,
        "timed_out": False,
        "cwd_change_failed": False,
        "duration_seconds": 0.1,
    }
    fields.update(overrides)
    return CommandOutcome(**fields)  # type: ignore[arg-type]


def _wrapper(remote_argv: tuple[str, ...]) -> str:
    """The transport representation of the escalation argv (pure mirror)."""
    return " ".join(shlex.quote(element) for element in remote_argv)


class TestPrivilegedRemoteCommand:
    def test_disabled_mode_refuses_escalation(self) -> None:
        with pytest.raises(PrivilegeRequestError) as excinfo:
            privileged_remote_command(_request(), PrivilegeMode.NONE)
        assert excinfo.value.code is PrivilegeErrorCode.PRIVILEGE_DISABLED
        assert "none" in str(excinfo.value)

    def test_raw_string_mode_refused(self) -> None:
        with pytest.raises(TypeError):
            privileged_remote_command(_request(), "sudo")  # type: ignore[arg-type]

    def test_root_mode_payload_verbatim(self) -> None:
        request = _request()
        assert (
            privileged_remote_command(request, PrivilegeMode.ROOT)
            == compose_remote_command(request)
        )

    def test_root_mode_keeps_cwd_guard(self) -> None:
        request = _request(working_directory="/etc")
        assert (
            privileged_remote_command(request, PrivilegeMode.ROOT)
            == compose_remote_command(request)
        )
        assert "cd /etc 2>/dev/null" in privileged_remote_command(
            request, PrivilegeMode.ROOT
        )

    def test_sudo_mode_composes_noninteractive_wrapper(self) -> None:
        request = _request(argv=("echo", "hi"))
        composed = privileged_remote_command(request, PrivilegeMode.SUDO)
        # Exact frozen shape: noninteractive sudo, option end, wrapper shell.
        head = composed.split(" ", 5)
        assert head[0] == SUDO_PROGRAM
        assert head[1] == "-n"  # acceptance 1: never prompts, fails instead
        assert head[2] == "--"
        assert head[3] == SUDO_WRAPPER_SHELL
        assert head[4] == "-c"
        guarded = (
            f"echo '{PAYLOAD_START_MARKER}' 1>&2; "
            + compose_remote_command(request)
        )
        assert composed == _wrapper(
            (SUDO_PROGRAM, "-n", "--", SUDO_WRAPPER_SHELL, "-c", guarded)
        )

    def test_sudo_mode_keeps_payload_literal(self) -> None:
        request = _request(
            argv=("printf", "%s\\n", "hello; touch /tmp/pwned", "$(id)")
        )
        composed = privileged_remote_command(request, PrivilegeMode.SUDO)
        guarded = (
            f"echo '{PAYLOAD_START_MARKER}' 1>&2; "
            + compose_remote_command(request)
        )
        # The payload appears exactly once, inside the single quoted -c
        # element; the injected fragment can never terminate it.
        assert composed == _wrapper(
            (SUDO_PROGRAM, "-n", "--", SUDO_WRAPPER_SHELL, "-c", guarded)
        )
        assert composed.count("touch /tmp/pwned") == 1

    def test_sudo_mode_moves_cwd_guard_inside_escalation(self) -> None:
        request = _request(working_directory="/opt/my dir")
        composed = privileged_remote_command(request, PrivilegeMode.SUDO)
        guarded = (
            f"echo '{PAYLOAD_START_MARKER}' 1>&2; "
            + compose_remote_command(request)
        )
        # The cd guard travels INSIDE the escalated payload: the privileged
        # shell honours it (marker first, then the guard).
        assert guarded.startswith(f"echo '{PAYLOAD_START_MARKER}' 1>&2; ")
        assert "cd '/opt/my dir' 2>/dev/null" in guarded
        assert composed == _wrapper(
            (SUDO_PROGRAM, "-n", "--", SUDO_WRAPPER_SHELL, "-c", guarded)
        )

    def test_sudo_mode_wraps_explicit_shell_request(self) -> None:
        request = _request(argv=None, shell="for i in 1 2; do echo $i; done")
        composed = privileged_remote_command(request, PrivilegeMode.SUDO)
        guarded = (
            f"echo '{PAYLOAD_START_MARKER}' 1>&2; " + request.shell
        )
        assert composed == _wrapper(
            (SUDO_PROGRAM, "-n", "--", SUDO_WRAPPER_SHELL, "-c", guarded)
        )

    def test_oversized_escalation_rejected_before_effect(self) -> None:
        # Inner payload stays inside the lane budget; only the escalation
        # wrapper (start marker + sudo argv + quoting) pushes the composed
        # form over it.
        request = _request(
            argv=None, shell="x" * (MAX_REMOTE_COMMAND_BYTES - 30)
        )
        assert len(compose_remote_command(request).encode()) <= MAX_REMOTE_COMMAND_BYTES
        with pytest.raises(CommandRequestError):
            privileged_remote_command(request, PrivilegeMode.SUDO)


class TestSudoDenialClassification:
    def test_password_required_classified_denial(self) -> None:
        outcome = _outcome(stderr="sudo: a password is required\n")
        assert _is_sudo_denial(outcome)

    def test_not_in_sudoers_classified_denial(self) -> None:
        outcome = _outcome(
            stderr="sudo: a password is required\nsudo: 1 incorrect password attempt\n"
        )
        assert _is_sudo_denial(outcome)
        variant = _outcome(
            stderr="We trust you have received the usual lecture...\n"
                   "sudo: user is not in the sudoers file. This incident will be reported.\n"
        )
        assert _is_sudo_denial(variant)

    def test_command_not_allowed_classified_denial(self) -> None:
        outcome = _outcome(
            stderr="sudo: Sorry, user mai-fxt may not run /usr/bin/id on fxt.\n"
        )
        assert _is_sudo_denial(outcome)

    def test_no_tty_classified_denial(self) -> None:
        outcome = _outcome(
            stderr="sudo: no tty present and no askpass program specified\n"
        )
        assert _is_sudo_denial(outcome)

    def test_broken_sudoers_classified_denial(self) -> None:
        outcome = _outcome(stderr="sudo: problem with sudoers entries\n")
        assert _is_sudo_denial(outcome)

    def test_started_payload_is_never_a_denial(self) -> None:
        outcome = _outcome(
            stderr=(f"{PAYLOAD_START_MARKER}\nsudo: a password is required\n"),
        )
        assert not _is_sudo_denial(outcome)

    def test_truncated_stderr_is_never_a_denial(self) -> None:
        outcome = _outcome(
            stderr="sudo: a password is required\n", stderr_truncated=True
        )
        assert not _is_sudo_denial(outcome)

    def test_other_exit_codes_are_never_a_denial(self) -> None:
        assert not _is_sudo_denial(
            _outcome(exit_code=7, stderr="sudo: a password is required\n")
        )
        assert not _is_sudo_denial(
            _outcome(exit_code=127, stderr="sh: sudo: command not found\n")
        )
        assert not _is_sudo_denial(_outcome(exit_code=0, status=CommandStatus.SUCCEEDED))
        assert not _is_sudo_denial(_outcome(stderr=""))

    def test_denial_of_unknown_variant_stays_payload_failure(self) -> None:
        outcome = _outcome(stderr="sudo: some future refusal text\n")
        assert not _is_sudo_denial(outcome)


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


class TestExecutePrivilegedCommandUnit:
    @pytest.fixture()
    def known_hosts(self, tmp_path: Path) -> Path:
        path = tmp_path / "known_hosts"
        path.write_text("", encoding="utf-8")
        return path

    def _identity(self) -> SshIdentity:
        return SshIdentity(
            host="203.0.113.10",
            port=22,
            user="mai-fxt",
            host_key="SHA256:" + "a" * 43,
            credential_ref="MAI_SSH_KEY_UNIT",
        )

    def test_disabled_mode_refused_before_any_spawn(
        self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path
    ) -> None:
        spawned = False

        def _no_spawn(*_a: object, **_k: object) -> _FakeProc:
            nonlocal spawned
            spawned = True
            raise AssertionError("Popen must not run for mode=none")

        monkeypatch.setattr(subprocess, "Popen", _no_spawn)
        with pytest.raises(PrivilegeRequestError) as excinfo:
            execute_privileged_command(
                self._identity(),
                _request(),
                PrivilegeMode.NONE,
                key_path="/fixture/key",
                known_hosts_path=known_hosts,
            )
        assert excinfo.value.code is PrivilegeErrorCode.PRIVILEGE_DISABLED
        assert spawned is False

    def test_sudo_denial_raises_structured_value_free_error(
        self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path
    ) -> None:
        proc = _FakeProc(1, stderr=b"sudo: a password is required\n")
        monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: proc)
        with pytest.raises(SudoDeniedError) as excinfo:
            execute_privileged_command(
                self._identity(),
                _request(),
                PrivilegeMode.SUDO,
                key_path="/fixture/key",
                known_hosts_path=known_hosts,
            )
        assert excinfo.value.code is PrivilegeErrorCode.SUDO_DENIED
        detail = excinfo.value.detail
        assert "203.0.113.10:22" in detail
        assert "\n" not in detail
        assert len(detail) < 400

    def test_denial_detail_bounded_against_hostile_stderr(
        self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path
    ) -> None:
        hostile = (b"x" * 4000 + b"\nsudo: a password is required\n")
        proc = _FakeProc(1, stderr=hostile)
        monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: proc)
        with pytest.raises(SudoDeniedError) as excinfo:
            execute_privileged_command(
                self._identity(),
                _request(),
                PrivilegeMode.SUDO,
                key_path="/fixture/key",
                known_hosts_path=known_hosts,
            )
        detail = excinfo.value.detail
        assert "\n" not in detail
        assert len(detail) < 400
        assert "x" * 4000 not in detail
        assert "a password is required" in detail

    def test_started_payload_failure_returns_outcome(
        self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path
    ) -> None:
        stderr = f"{PAYLOAD_START_MARKER}\npayload blew up\n".encode()
        proc = _FakeProc(3, stderr=stderr)
        monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: proc)
        outcome = execute_privileged_command(
            self._identity(),
            _request(),
            PrivilegeMode.SUDO,
            key_path="/fixture/key",
            known_hosts_path=known_hosts,
        )
        assert outcome.status is CommandStatus.FAILED
        assert outcome.exit_code == 3

    def test_root_mode_runs_payload_without_wrapper(
        self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path
    ) -> None:
        captured: list[list[str]] = []
        proc = _FakeProc(0, stdout=b"0\n")
        def _spy(command: list[str], *_a: object, **_k: object) -> _FakeProc:
            captured.append(command)
            return proc
        monkeypatch.setattr(subprocess, "Popen", _spy)
        request = _request()
        outcome = execute_privileged_command(
            self._identity(),
            request,
            PrivilegeMode.ROOT,
            key_path="/fixture/key",
            known_hosts_path=known_hosts,
        )
        assert outcome.status is CommandStatus.SUCCEEDED
        remote = captured[0][-1]
        assert remote == compose_remote_command(request)
        assert SUDO_PROGRAM not in remote.split()

    def test_sudo_mode_preserves_request_identity_fields(
        self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path
    ) -> None:
        captured: list[list[str]] = []
        proc = _FakeProc(0, stdout=b"0\n")
        def _spy(command: list[str], *_a: object, **_k: object) -> _FakeProc:
            captured.append(command)
            return proc
        monkeypatch.setattr(subprocess, "Popen", _spy)
        request = _request()
        outcome = execute_privileged_command(
            self._identity(),
            request,
            PrivilegeMode.SUDO,
            key_path="/fixture/key",
            known_hosts_path=known_hosts,
        )
        assert outcome.status is CommandStatus.SUCCEEDED
        assert outcome.op_id == request.op_id
        assert outcome.server_id == request.server_id
        assert outcome.display == request.display
        remote = captured[0][-1]
        assert remote == privileged_remote_command(request, PrivilegeMode.SUDO)
        assert remote.split(" ", 2)[:2] == ["sudo", "-n"]

    def test_transport_failure_propagates_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path
    ) -> None:
        proc = _FakeProc(
            255, stderr=b"root@203.0.113.10: Permission denied (publickey)."
        )
        monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: proc)
        with pytest.raises(SshConnectError) as excinfo:
            execute_privileged_command(
                self._identity(),
                _request(),
                PrivilegeMode.SUDO,
                key_path="/fixture/key",
                known_hosts_path=known_hosts,
            )
        assert "auth_failed" in str(excinfo.value)

    def test_timeout_of_escalated_payload_stays_unknown(
        self, monkeypatch: pytest.MonkeyPatch, known_hosts: Path
    ) -> None:
        proc = _FakeProc(0, stdout=b"partial", hold_open=True)
        monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: proc)
        try:
            outcome = execute_privileged_command(
                self._identity(),
                _request(timeout=0.2),
                PrivilegeMode.SUDO,
                key_path="/fixture/key",
                known_hosts_path=known_hosts,
            )
        finally:
            proc.dispose()
        assert outcome.status is CommandStatus.OUTCOME_UNKNOWN
        assert outcome.timed_out
        assert outcome.exit_code is None


# ---------------------------------------------------------------------------
# Disposable Arch sshd fixture with a command-scoped NOPASSWD grant
# (runtime-environment ADR D1–D5; unique namespace mai-fxt-04-sudo-*).
# Pattern producer: test_commands.py ArchSshdFixture.
# ---------------------------------------------------------------------------

FIXTURE_CONTAINER = "mai-fxt-04-sudo-arch"
FIXTURE_IMAGE_TAG = "mai-fxt-04-sudo-arch:sshd"
FIXTURE_SCRATCH = Path("/tmp/mai-fxt-04-sudo-tests")
FIXTURE_CREDENTIAL_REF = "MAI_SSH_KEY_04_SUDO_FIXTURE"
FIXTURE_USER = "mai-fxt"
SUDOERS_DROPIN = f"{FIXTURE_USER}-nopasswd"
#: The grant is command-scoped: exactly the escalation wrapper shell, the
#: only command the sudo lane ever runs. No blanket ALL, no password.
FIXTURE_SUDOERS_RULE = f"{FIXTURE_USER} ALL=(root) NOPASSWD: {SUDO_WRAPPER_SHELL}"


def _docker(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


_image_ensured = False


def _ensure_fixture_image() -> None:
    """Build the disposable Arch+openssh+sudo fixture image once (pinned base)."""
    global _image_ensured
    if _image_ensured and _docker("image", "inspect", FIXTURE_IMAGE_TAG).returncode == 0:
        return
    context = FIXTURE_SCRATCH / "build-context"
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(
        "FROM archlinux:latest\n"
        "RUN pacman-key --init && pacman -Sy --noconfirm openssh sudo "
        "&& ssh-keygen -A\n",
        encoding="utf-8",
    )
    build = _docker("build", "-q", "-t", FIXTURE_IMAGE_TAG, str(context), timeout=1200)
    assert build.returncode == 0, f"fixture image build failed: {build.stderr[-800:]}"
    _image_ensured = True


class ArchSudoFixture:
    """One disposable Arch sshd container with an unprivileged sudo account.

    The fixture account holds a command-scoped NOPASSWD sudoers grant for
    exactly the escalation wrapper shell. The grant is removable/restorable
    in place, which is the acceptance-1 denial scenario.
    """

    def __init__(self) -> None:
        self.scratch = FIXTURE_SCRATCH / uuid.uuid4().hex
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.key_path = self.scratch / "fixture_key"
        self.port = 0
        self.host_key_fingerprint = ""
        self.cleanup_proof = "<not cleaned>"

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
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
            f"&& useradd -m -s /bin/sh {FIXTURE_USER} "
            "&& exec /usr/bin/sshd -D",
        )
        assert run.returncode == 0, f"fixture start failed: {run.stderr[-800:]}"
        self._refresh_port()
        # `docker run -d` returns before the container's start chain
        # (…&& useradd …&& sshd) finishes; wait for the account to exist
        # before provisioning its authorized_keys (race measured: chown
        # "invalid user" when exec beat useradd).
        self._wait_account(FIXTURE_USER)
        self._provision_authorized_key("root", "/root")
        self._provision_authorized_key(
            FIXTURE_USER, f"/home/{FIXTURE_USER}"
        )
        self.write_sudo_grant()
        self._wait_keyscan()
        self.host_key_fingerprint = self._current_fingerprint()

    def _wait_account(self, name: str, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        check = _docker("exec", FIXTURE_CONTAINER, "true")
        while time.monotonic() < deadline:
            check = _docker("exec", FIXTURE_CONTAINER, "id", "-u", name)
            if check.returncode == 0:
                return
            time.sleep(0.2)
        raise AssertionError(
            f"fixture account {name} never appeared: {check.stderr[-400:]}"
        )

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

    def _provision_authorized_key(self, user: str, home: str) -> None:
        pub = self.key_path.with_suffix(".pub").read_bytes()
        provision = subprocess.run(
            ["docker", "exec", "-i", FIXTURE_CONTAINER, "sh", "-c",
             f"mkdir -p {home}/.ssh && chmod 700 {home}/.ssh "
             f"&& cat > {home}/.ssh/authorized_keys "
             f"&& chmod 600 {home}/.ssh/authorized_keys "
             f"&& chown -R {user}:{user} {home}/.ssh"],
            input=pub, capture_output=True, timeout=30,
        )
        assert provision.returncode == 0, provision.stderr.decode()[-800:]

    # -- sudo grant state (the acceptance-1 scenario control) ---------------

    def write_sudo_grant(self) -> None:
        script = (
            "grep -q '^@includedir /etc/sudoers.d$' /etc/sudoers "
            "|| echo '@includedir /etc/sudoers.d' >> /etc/sudoers; "
            f"echo '{FIXTURE_SUDOERS_RULE}' > /etc/sudoers.d/{SUDOERS_DROPIN}; "
            f"chmod 440 /etc/sudoers.d/{SUDOERS_DROPIN}; "
            f"visudo -cf /etc/sudoers.d/{SUDOERS_DROPIN}"
        )
        result = _docker("exec", FIXTURE_CONTAINER, "sh", "-c", script)
        assert result.returncode == 0, (
            f"sudoers provisioning failed: {result.stdout[-400:]}{result.stderr[-400:]}"
        )

    def remove_sudo_grant(self) -> None:
        result = _docker(
            "exec", FIXTURE_CONTAINER,
            "rm", "-f", f"/etc/sudoers.d/{SUDOERS_DROPIN}",
        )
        assert result.returncode == 0, result.stderr[-400:]
        gone = _docker(
            "exec", FIXTURE_CONTAINER,
            "test", "!", "-e", f"/etc/sudoers.d/{SUDOERS_DROPIN}",
        )
        assert gone.returncode == 0, "grant file still present after removal"

    # -- identities and probes ----------------------------------------------

    def identity_for(self, user: str) -> SshIdentity:
        return SshIdentity(
            host="127.0.0.1",
            port=self.port,
            user=user,
            host_key=self.host_key_fingerprint,
            credential_ref=FIXTURE_CREDENTIAL_REF,
        )

    def env_resolver(self) -> EnvResolver:
        env_file = self.scratch / "fixture.env"
        env_file.write_text(
            f"{FIXTURE_CREDENTIAL_REF}={self.key_path}\n", encoding="utf-8"
        )
        os.chmod(env_file, 0o600)
        return EnvResolver(env_file)

    def _transport(self, identity: SshIdentity) -> tuple[Path, Path]:
        key_path = resolve_identity_key(self.env_resolver(), identity.credential_ref)
        known_hosts = self.scratch / f"known_hosts-{identity.user}"
        if not known_hosts.exists():
            pin_host_key(identity, known_hosts)
        return key_path, known_hosts

    def run_unprivileged(
        self, user: str, request: CommandRequest
    ) -> CommandOutcome:
        key_path, known_hosts = self._transport(self.identity_for(user))
        return execute_command(
            self.identity_for(user),
            request,
            key_path=key_path,
            known_hosts_path=known_hosts,
        )

    def run_privileged(
        self, user: str, request: CommandRequest, mode: PrivilegeMode
    ) -> CommandOutcome:
        key_path, known_hosts = self._transport(self.identity_for(user))
        return execute_privileged_command(
            self.identity_for(user),
            request,
            mode,
            key_path=key_path,
            known_hosts_path=known_hosts,
        )

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

    def cleanup(self) -> str:
        _docker("rm", "-f", FIXTURE_CONTAINER, timeout=60)
        listing = _docker(
            "ps", "-a", "--filter", f"name=^{FIXTURE_CONTAINER}$", "--format", "{{.Names}}"
        )
        shutil.rmtree(self.scratch, ignore_errors=True)
        shutil.rmtree(FIXTURE_SCRATCH, ignore_errors=True)
        self.cleanup_proof = (
            f"container {FIXTURE_CONTAINER} after rm: '{listing.stdout.strip() or '<none>'}'; "
            f"scratch {self.scratch} exists: {self.scratch.exists()}; "
            f"parent {FIXTURE_SCRATCH} exists: {FIXTURE_SCRATCH.exists()}"
        )
        return self.cleanup_proof


@pytest.fixture()
def arch_sudo() -> ArchSudoFixture:
    if not _docker_available():
        pytest.skip("docker unavailable: Arch sudo fixture tests require it")
    fixture = ArchSudoFixture()
    try:
        fixture.start()
        yield fixture
    finally:
        print(f"\nFIXTURE CLEANUP PROOF: {fixture.cleanup()}")


class TestAgainstArchSudoFixture:
    """Real-boundary acceptance: an actual target grants, denies, recovers."""

    def test_privileged_identity_command_runs_as_root(
        self, arch_sudo: ArchSudoFixture
    ) -> None:
        # Acceptance 1 + scenario half 1: harmless privileged identity
        # command under the scoped NOPASSWD grant; REAL uid 0, disposable
        # target only.
        started = time.monotonic()
        outcome = arch_sudo.run_privileged(
            FIXTURE_USER,
            _request(argv=("/usr/bin/id", "-u"), timeout=30.0, output_budget=1024),
            PrivilegeMode.SUDO,
        )
        elapsed = time.monotonic() - started
        assert outcome.status is CommandStatus.SUCCEEDED
        assert outcome.exit_code == 0
        assert outcome.stdout == "0\n"
        assert elapsed < 30.0
        assert not outcome.stderr_truncated

    def test_removed_grant_denies_without_hanging_and_payload_never_runs(
        self, arch_sudo: ArchSudoFixture
    ) -> None:
        # Acceptance 1 + scenario half 2: remove the fixture sudo grant,
        # verify the CLASSIFIED denial within the budget, and prove the
        # privileged payload never started (negative control).
        arch_sudo.remove_sudo_grant()
        canary = f"/tmp/mai-sudo-canary-{uuid.uuid4().hex}"
        started = time.monotonic()
        with pytest.raises(SudoDeniedError) as excinfo:
            arch_sudo.run_privileged(
                FIXTURE_USER,
                _request(
                    argv=("/bin/sh", "-c", f"touch {canary}"),
                    timeout=30.0,
                    output_budget=4096,
                ),
                PrivilegeMode.SUDO,
            )
        elapsed = time.monotonic() - started
        assert excinfo.value.code is PrivilegeErrorCode.SUDO_DENIED
        assert "a password is required" in excinfo.value.detail
        # No hang: sudo -n must fail immediately, far inside the budget.
        assert elapsed < 15.0
        probe = arch_sudo.run_unprivileged(
            FIXTURE_USER,
            _request(argv=("test", "-e", canary), output_budget=1024),
        )
        assert probe.status is CommandStatus.FAILED
        assert probe.exit_code == 1  # canary absent: the payload never ran

    def test_restored_grant_recovers(
        self, arch_sudo: ArchSudoFixture
    ) -> None:
        arch_sudo.remove_sudo_grant()
        with pytest.raises(SudoDeniedError):
            arch_sudo.run_privileged(
                FIXTURE_USER,
                _request(argv=("/usr/bin/id", "-u"), timeout=30.0),
                PrivilegeMode.SUDO,
            )
        arch_sudo.write_sudo_grant()
        outcome = arch_sudo.run_privileged(
            FIXTURE_USER,
            _request(argv=("/usr/bin/id", "-u"), timeout=30.0, output_budget=1024),
            PrivilegeMode.SUDO,
        )
        assert outcome.status is CommandStatus.SUCCEEDED
        assert outcome.stdout == "0\n"

    def test_root_mode_identity_verified_on_disposable_target(
        self, arch_sudo: ArchSudoFixture
    ) -> None:
        # Acceptance 2, second half: the root identity claim is proven only
        # here, on the disposable fixture. `root` profile mode adds NO
        # escalation: as root it is already uid 0; as the unprivileged
        # account it stays unprivileged (the mode trusts the profile).
        outcome = arch_sudo.run_privileged(
            "root",
            _request(argv=("/usr/bin/id", "-u"), output_budget=1024),
            PrivilegeMode.ROOT,
        )
        assert outcome.status is CommandStatus.SUCCEEDED
        assert outcome.stdout == "0\n"

        unprivileged = arch_sudo.run_privileged(
            FIXTURE_USER,
            _request(argv=("/usr/bin/id", "-u"), output_budget=1024),
            PrivilegeMode.ROOT,
        )
        assert unprivileged.status is CommandStatus.SUCCEEDED
        uid = int(unprivileged.stdout.strip())
        assert uid != 0

    def test_disabled_mode_cannot_request_sudo(
        self, arch_sudo: ArchSudoFixture
    ) -> None:
        # Acceptance 2, first half, at the real boundary: `none` refuses
        # sudo before any effect while the plain unprivileged lane keeps
        # working for the same request.
        request = _request(argv=("/usr/bin/id", "-u"), output_budget=1024)
        with pytest.raises(PrivilegeRequestError) as excinfo:
            arch_sudo.run_privileged(
                FIXTURE_USER, request, PrivilegeMode.NONE
            )
        assert excinfo.value.code is PrivilegeErrorCode.PRIVILEGE_DISABLED

        plain = arch_sudo.run_unprivileged(FIXTURE_USER, request)
        assert plain.status is CommandStatus.SUCCEEDED
        assert int(plain.stdout.strip()) != 0

    def test_escalated_environment_carries_no_credential_material(
        self, arch_sudo: ArchSudoFixture
    ) -> None:
        key_path = resolve_identity_key(
            arch_sudo.env_resolver(), FIXTURE_CREDENTIAL_REF
        )
        key_bytes = key_path.read_bytes()
        assert key_bytes  # sanity: there IS material to leak
        outcome = arch_sudo.run_privileged(
            FIXTURE_USER,
            _request(
                argv=("/bin/sh", "-c", "/usr/bin/env; /bin/cat /proc/self/environ"),
                timeout=30.0,
                output_budget=64 * 1024,
            ),
            PrivilegeMode.SUDO,
        )
        assert outcome.status is CommandStatus.SUCCEEDED
        assert FIXTURE_CREDENTIAL_REF not in outcome.stdout
        assert FIXTURE_CREDENTIAL_REF not in outcome.stderr
        assert key_bytes not in outcome.stdout.encode("utf-8")
        assert key_bytes not in outcome.stderr.encode("utf-8")

    def test_timeout_of_escalated_payload_reports_unknown(
        self, arch_sudo: ArchSudoFixture
    ) -> None:
        # Inherited boundary: a budget-cut ROOT payload may live on — the
        # outcome stays unknown, no exit is invented (host-supervision rule 3).
        outcome = arch_sudo.run_privileged(
            FIXTURE_USER,
            _request(argv=("sleep", "10"), timeout=2.0, output_budget=1024),
            PrivilegeMode.SUDO,
        )
        assert outcome.status is CommandStatus.OUTCOME_UNKNOWN
        assert outcome.timed_out
        assert outcome.exit_code is None
