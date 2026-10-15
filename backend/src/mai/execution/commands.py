"""Bounded target-command execution and safe output capture over pinned SSH.

Card ``04-command-output`` against contracts.md **C04** (transport frozen by
the host-supervision ADR, ``experiments/ssh_supervision_probe.py``):

- **C04 rule 2** — Linux command inputs are structured argv where possible;
  a shell string is accepted only as an EXPLICIT request (its own
  :class:`CommandRequest` field, mutually exclusive with ``argv``).
  Structured argv is POSIX shell-quoted element-wise, so the target default
  shell parses it back into the same argv: shell metacharacters inside an
  element (``;``, ``$``, globs, redirection) stay literal data. Credentials
  are never concatenated into shell or argv — this module has no field that
  could carry one; the identity key enters only as a controller-local
  ``PATH`` (``ssh -i``) resolved by the caller through
  :func:`mai.execution.ssh.resolve_identity_key`. Key material is never read.
- **C04 rule 3** — the bounded-execution request fields are enforced here:
  timeout (wall clock, controller-enforced), output budget (per stream,
  artifact-enforced) and the working-directory policy (absolute target path;
  a failed ``cd`` is distinguished from a command failure by a reserved exit
  code plus a fixed stderr marker — the command never starts).
- **C04 rule 4** — the outcome status vocabulary is implemented exactly as
  frozen: ``succeeded | failed | running | outcome_unknown |
  cancel_requested | cancelled``. This synchronous lane produces only
  ``succeeded``, ``failed`` and ``outcome_unknown``; ``running`` /
  ``cancel_requested`` / ``cancelled`` belong to the durable supervision
  lanes built on top (``04-supervisor`` records ``running`` and
  controller-side ``cancel_requested``; ``04-reconcile`` reports
  host-verified ``cancelled``). No additional values exist.

Boundary (**C04 rules 1/5**, host-supervision rules 1/3): this is the
bounded synchronous execution primitive that later P04 cards compose
(privilege modes: ``04-sudo``; durable dispatch intent + receipts:
``04-supervisor``). It persists NO dispatch intent and issues NO receipts,
so it never claims ownership across channel loss: when the wall-clock
budget or a dead client cuts the ssh(1) process, the outcome is
``outcome_unknown`` (the remote payload may keep running — measured exp1a:
sshd does not SIGHUP-kill non-tty children), never ``failed`` with an
invented exit code and never retry-safe. Callers that need ownership,
cancellation and reboot reconciliation must go through the supervisor lane,
not this module.

Safe output (**acceptance 2**): exit code, stdout and stderr are
distinguished fields; each stream is retained only up to the request's
output budget (first-N-bytes) while the full stream is still drained to EOF
so the exit status stays known; ``stdout_bytes``/``stderr_bytes`` carry the
true totals and ``*_truncated`` marks the cut, so truncation is never
silent. Error details are single-line, bounded and value-free (stable
reasons + a sanitized ssh stderr tail), reusing the frozen
:class:`mai.execution.ssh.SshErrorCode` transport vocabulary.
"""

from __future__ import annotations

import math
import os
import selectors
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from mai.contracts.server import SshIdentity
from mai.execution.ssh import SshConnectError, SshErrorCode, ssh_arguments

__all__ = [
    "CWD_EXIT_CODE",
    "CWD_FAILURE_MARKER",
    "CommandOutcome",
    "CommandRequest",
    "CommandRequestError",
    "CommandStatus",
    "MAX_ARGV_ELEMENTS",
    "MAX_COMMAND_TIMEOUT_SECONDS",
    "MAX_DISPLAY_CHARS",
    "MAX_OUTPUT_BUDGET_BYTES",
    "MAX_REMOTE_COMMAND_BYTES",
    "MAX_WORKING_DIRECTORY_CHARS",
    "compose_remote_command",
    "execute_command",
]


class CommandErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_REQUEST = "invalid_request"


class CommandRequestError(ValueError):
    """One structured, value-free request rejection (C01 envelope shape)."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"{CommandErrorCode.INVALID_REQUEST.value}: {detail}")
        self.code = CommandErrorCode.INVALID_REQUEST
        self.detail = detail


class CommandStatus(StrEnum):
    """``CommandOutcome`` status vocabulary, frozen by contracts.md C04 rule 4.

    P04/P05 schema producers implement exactly these six values; additions
    require contract reconciliation. This bounded synchronous lane produces
    ``succeeded`` (exit 0), ``failed`` (known non-zero exit) and
    ``outcome_unknown`` (budget cut or client death — the remote payload may
    still be alive, so the exit is unknowable without reconciliation).
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RUNNING = "running"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


#: Reserved remote exit code signalling "the working-directory policy failed;
#: the payload never started". ssh(1) reports transport errors as 255, and a
#: payload that deliberately exits 210 *and* prints the marker byte-for-byte
#: can only fake this by construction — the supervisor receipt contract
#: disambiguates such corner cases once durable dispatch exists.
CWD_EXIT_CODE = 210

#: Fixed stderr marker emitted together with :data:`CWD_EXIT_CODE`; the
#: marker, not the code alone, is what the outcome classifier matches.
CWD_FAILURE_MARKER = "mai-exec: cannot change working directory"

#: Hard cap on the safe display string (C04 rule 3: single line, no secrets
#: are ever placed here by this module; callers pass their own label).
MAX_DISPLAY_CHARS = 200

#: Working-directory policy: absolute target-side path, bounded length.
MAX_WORKING_DIRECTORY_CHARS = 4096

#: Structured argv upper bound (elements; per-element size is covered by the
#: composed-remote-command budget).
MAX_ARGV_ELEMENTS = 4096

#: Hard cap on the composed remote command handed to ``ssh(1)`` as one argv
#: element (keeps the transport call inside sane exec-argv limits).
MAX_REMOTE_COMMAND_BYTES = 128 * 1024

#: Wall-clock ceiling for one bounded synchronous command (controller-
#: enforced; the remote payload may outlive a timeout — see module docstring).
MAX_COMMAND_TIMEOUT_SECONDS = 3600.0

#: Hard cap on the per-stream output budget accepted from a request.
MAX_OUTPUT_BUDGET_BYTES = 8 * 1024 * 1024

#: Incremental read size while draining remote output.
READ_CHUNK_BYTES = 64 * 1024


def _reject(detail: str) -> NoReturn:
    raise CommandRequestError(detail)


@dataclass(frozen=True, slots=True)
class CommandRequest:
    """One bounded target command (C04 rule 3 fields owned by this lane).

    Exactly one of ``argv`` (structured, preferred) and ``shell`` (explicit
    request) must be set. ``display`` is the caller's safe one-line label
    echoed in outcomes — this module never adds credential material to it.
    ``output_budget`` applies to stdout and stderr independently.
    ``op_id``/``server_id`` are opaque correlation IDs (C01); this lane does
    NOT persist dispatch intent (C04 rule 5 — that is the supervisor lane).

    Raises:
        CommandRequestError: on any malformed field; details are value-free.
    """

    op_id: uuid.UUID
    server_id: uuid.UUID
    display: str
    timeout: float
    output_budget: int
    argv: tuple[str, ...] | None = None
    shell: str | None = None
    working_directory: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.op_id, uuid.UUID):
            _reject("op_id must be an opaque UUID")
        if not isinstance(self.server_id, uuid.UUID):
            _reject("server_id must be an opaque UUID")
        if not isinstance(self.display, str) or not self.display:
            _reject("display must be a non-empty string")
        if len(self.display) > MAX_DISPLAY_CHARS:
            _reject(f"display exceeds {MAX_DISPLAY_CHARS} characters")
        if not self.display.isprintable():
            _reject("display must be single-line printable text")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
            _reject("timeout must be a number of seconds")
        if not math.isfinite(self.timeout):
            _reject("timeout must be finite")
        if not 0 < self.timeout <= MAX_COMMAND_TIMEOUT_SECONDS:
            _reject(f"timeout must be in (0, {MAX_COMMAND_TIMEOUT_SECONDS}] seconds")
        if isinstance(self.output_budget, bool) or not isinstance(self.output_budget, int):
            _reject("output_budget must be an integer byte count")
        if not 0 < self.output_budget <= MAX_OUTPUT_BUDGET_BYTES:
            _reject(f"output_budget must be in (0, {MAX_OUTPUT_BUDGET_BYTES}] bytes")
        if (self.argv is None) == (self.shell is None):
            _reject(
                "exactly one of argv (structured) or shell (explicit request) "
                "must be set"
            )
        if self.argv is not None:
            if isinstance(self.argv, list):
                object.__setattr__(self, "argv", tuple(self.argv))
            if not isinstance(self.argv, tuple) or not self.argv:
                _reject("argv must be a non-empty sequence of strings")
            if len(self.argv) > MAX_ARGV_ELEMENTS:
                _reject(f"argv exceeds {MAX_ARGV_ELEMENTS} elements")
            for element in self.argv:
                if not isinstance(element, str):
                    _reject("argv elements must be strings")
                if "\x00" in element:
                    _reject("argv elements must not contain NUL bytes")
        if self.shell is not None:
            if not isinstance(self.shell, str) or not self.shell.strip():
                _reject("shell must be a non-empty command string")
            if "\x00" in self.shell:
                _reject("shell must not contain NUL bytes")
        if self.working_directory is not None:
            cwd = self.working_directory
            if not isinstance(cwd, str) or not cwd:
                _reject("working_directory must be a non-empty string")
            if len(cwd) > MAX_WORKING_DIRECTORY_CHARS:
                _reject(f"working_directory exceeds {MAX_WORKING_DIRECTORY_CHARS} characters")
            if not cwd.startswith("/"):
                _reject("working_directory must be an absolute target-side path")
            if "\x00" in cwd or "\n" in cwd or "\r" in cwd or not cwd.isprintable():
                _reject("working_directory must be printable single-line text")


def compose_remote_command(request: CommandRequest) -> str:
    """Compose the remote command string (pure; transport representation).

    argv mode quotes every element (``shlex.quote``) so the target default
    shell reconstructs the same argv without interpreting metacharacters;
    shell mode passes the explicit request through verbatim. When a
    working directory is set, a POSIX-sh guard prefixes the payload: a
    failed ``cd`` (missing/unusable directory) prints the fixed
    :data:`CWD_FAILURE_MARKER` on stderr and exits with
    :data:`CWD_EXIT_CODE` — the payload never runs and the failure is
    distinguishable from a payload exit.

    Raises:
        CommandRequestError: when the composed string exceeds
            ``MAX_REMOTE_COMMAND_BYTES``.
    """
    if request.argv is not None:
        payload = " ".join(shlex.quote(element) for element in request.argv)
    else:
        assert request.shell is not None  # request validation guarantees one
        payload = request.shell
    if request.working_directory is not None:
        guard = (
            f"cd {shlex.quote(request.working_directory)} 2>/dev/null || "
            f"{{ echo '{CWD_FAILURE_MARKER}' 1>&2; exit {CWD_EXIT_CODE}; }}"
        )
        payload = f"{guard}; {payload}"
    if len(payload.encode("utf-8")) > MAX_REMOTE_COMMAND_BYTES:
        _reject(f"composed remote command exceeds {MAX_REMOTE_COMMAND_BYTES} bytes")
    return payload


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Structured result of one bounded target command (C04 rule 4 shape).

    ``exit_code`` is the target command's exit status where known; ``None``
    when the outcome is unknown (timeout cut / client death — the remote
    payload may still be running). ``stdout``/``stderr`` hold at most
    ``output_budget`` bytes each (first-N-bytes, UTF-8 with replacement);
    ``*_bytes`` carry the true totals and ``*_truncated`` marks a cut, so a
    truncated artifact is never mistaken for complete output. None of these
    fields ever contains credential material: this module never reads the
    identity key and never places credential references into the command.
    """

    op_id: uuid.UUID
    server_id: uuid.UUID
    display: str
    status: CommandStatus
    exit_code: int | None
    stdout: str
    stdout_bytes: int
    stdout_truncated: bool
    stderr: str
    stderr_bytes: int
    stderr_truncated: bool
    timed_out: bool
    cwd_change_failed: bool
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class _Captured:
    """Raw per-stream capture before outcome assembly."""

    stdout: bytes
    stdout_total: int
    stderr: bytes
    stderr_total: int
    timed_out: bool

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")

    @property
    def stdout_truncated(self) -> bool:
        return self.stdout_total > len(self.stdout)

    @property
    def stderr_truncated(self) -> bool:
        return self.stderr_total > len(self.stderr)


class _StreamCapture:
    """Retains the first ``budget`` bytes of one stream; counts the rest."""

    __slots__ = ("budget", "retained", "total")

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.retained = bytearray()
        self.total = 0

    def record(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.budget - len(self.retained)
        if room > 0:
            self.retained += chunk[:room]


def _capture_streams(
    proc: subprocess.Popen[bytes], *, deadline: float, budget: int
) -> _Captured:
    """Drain both pipes with selectors until EOF or ``deadline``.

    Non-blocking reads (no pipe-buffer deadlock between the two streams),
    bounded retention (:class:`_StreamCapture`), full drain so a completing
    command's exit status stays known. On deadline the ssh client is killed
    (the remote payload may survive the channel — outcome stays unknown).
    """
    captures = {
        "stdout": _StreamCapture(budget),
        "stderr": _StreamCapture(budget),
    }
    selector = selectors.DefaultSelector()
    streams: dict[int, tuple[object, str]] = {}
    timed_out = False
    try:
        for name in ("stdout", "stderr"):
            stream = getattr(proc, name)
            if stream is None:  # pragma: no cover - Popen(PIPE) guarantees
                continue
            fd = stream.fileno()
            os.set_blocking(fd, False)
            selector.register(fd, selectors.EVENT_READ, name)
            streams[fd] = (stream, name)
        while streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _mask in selector.select(remaining):
                fd = key.fd
                stream, name = streams[fd]
                try:
                    chunk = os.read(fd, READ_CHUNK_BYTES)
                except (BlockingIOError, InterruptedError):
                    continue
                if chunk:
                    captures[name].record(chunk)
                else:
                    selector.unregister(fd)
                    stream.close()
                    del streams[fd]
    finally:
        for stream, _name in list(streams.values()):
            stream.close()
        streams.clear()
        selector.close()
        if timed_out:
            proc.kill()
        proc.wait()
    return _Captured(
        stdout=bytes(captures["stdout"].retained),
        stdout_total=captures["stdout"].total,
        stderr=bytes(captures["stderr"].retained),
        stderr_total=captures["stderr"].total,
        timed_out=timed_out,
    )


def _bounded_tail(stderr: str) -> str:
    """Bounded single-line stderr tail for error details (value-free)."""
    flat = " ".join(stderr.split())
    return flat[-200:] if flat else "<no stderr>"


def _map_transport_failure(rc: int, stderr: str, endpoint: str) -> SshConnectError:
    """Map a non-zero ssh exit to the frozen transport rejection vocabulary.

    Local copy of the ``mai.execution.ssh`` mapping: that module is outside
    this card's allowlist and its mapper is private; a divergence would be
    a review finding, not a silent import of a private symbol. ssh(1)
    reports its own errors as exit 255 — which a remote command can also
    produce, an ssh(1) limitation the durable receipt contract resolves
    later (documented limitation of this lane).
    """
    flat = " ".join(stderr.split())
    if "Host key verification failed" in flat or "HOST IDENTIFICATION HAS CHANGED" in flat:
        return SshConnectError(
            SshErrorCode.HOST_KEY_MISMATCH,
            f"{endpoint} refused by host key pinning "
            f"(stderr: {_bounded_tail(stderr)}); re-enrollment is an "
            "explicit admin action, never automatic",
        )
    if "Permission denied" in flat or "bad permissions" in flat.lower():
        return SshConnectError(
            SshErrorCode.AUTH_FAILED,
            f"{endpoint} rejected the pinned identity key "
            f"(stderr: {_bounded_tail(stderr)})",
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
            f"{endpoint} unreachable (stderr: {_bounded_tail(stderr)})",
        )
    return SshConnectError(
        SshErrorCode.SSH_FAILED,
        f"{endpoint} ssh exited rc={rc} (stderr: {_bounded_tail(stderr)})",
    )


def execute_command(
    identity: SshIdentity,
    request: CommandRequest,
    *,
    key_path: os.PathLike[str] | str,
    known_hosts_path: os.PathLike[str] | str,
) -> CommandOutcome:
    """Run one bounded command over the pinned-identity SSH transport.

    Preconditions match :func:`mai.execution.ssh.probe_environment`: the key
    path comes from :func:`mai.execution.ssh.resolve_identity_key` and the
    managed known-hosts file from :func:`mai.execution.ssh.pin_host_key`;
    ``StrictHostKeyChecking=yes`` refuses an unenrolled/rotated endpoint
    before anything runs.

    Returns:
        CommandOutcome: for every command that RAN on the target —
        ``succeeded`` (exit 0), ``failed`` (known non-zero exit, including
        the working-directory guard) or ``outcome_unknown`` (timeout cut or
        ssh client death; ``exit_code`` is ``None`` and no status is
        invented).

    Raises:
        CommandRequestError: malformed request or oversized composed command
            (rejected before any remote effect).
        SshConnectError: transport-level failure (endpoint refused,
            unreachable, auth rejected, ssh client broken) — the command
            never reached/started on the target.
    """
    known_hosts = os.fspath(known_hosts_path)
    if not os.path.isfile(known_hosts):
        raise SshConnectError(
            SshErrorCode.INVALID_CONFIGURATION,
            f"managed known-hosts file {known_hosts} does not exist; "
            "enroll the pinned host key explicitly before connecting",
        )
    remote_command = compose_remote_command(request)
    command = ssh_arguments(
        identity,
        key_path=os.fspath(key_path),
        known_hosts_path=known_hosts,
        remote_command=remote_command,
    )
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise SshConnectError(
            SshErrorCode.SSH_FAILED,
            f"ssh client could not be started ({type(error).__name__})",
        ) from None
    captured = _capture_streams(
        proc, deadline=started + request.timeout, budget=request.output_budget
    )
    duration = time.monotonic() - started
    exit_code: int | None = None
    cwd_change_failed = False
    if captured.timed_out:
        status = CommandStatus.OUTCOME_UNKNOWN
    else:
        returncode = proc.returncode
        assert returncode is not None  # proc.wait() completed in _capture_streams
        if returncode == 0:
            exit_code = 0
            status = CommandStatus.SUCCEEDED
        elif returncode == 255:
            raise _map_transport_failure(
                returncode, captured.stderr_text, f"{identity.host}:{identity.port}"
            )
        elif returncode < 0:
            # ssh client died by signal (external kill): the remote payload
            # may live on — unknown, not failed (host-supervision rule 3).
            # exit_code stays None: no client-side signal is a command exit.
            status = CommandStatus.OUTCOME_UNKNOWN
        else:
            exit_code = returncode
            status = CommandStatus.FAILED
            if (
                returncode == CWD_EXIT_CODE
                and request.working_directory is not None
                and captured.stderr_text.startswith(CWD_FAILURE_MARKER)
            ):
                cwd_change_failed = True
    return CommandOutcome(
        op_id=request.op_id,
        server_id=request.server_id,
        display=request.display,
        status=status,
        exit_code=exit_code,
        stdout=captured.stdout_text,
        stdout_bytes=captured.stdout_total,
        stdout_truncated=captured.stdout_truncated,
        stderr=captured.stderr_text,
        stderr_bytes=captured.stderr_total,
        stderr_truncated=captured.stderr_truncated,
        timed_out=captured.timed_out,
        cwd_change_failed=cwd_change_failed,
        duration_seconds=duration,
    )
