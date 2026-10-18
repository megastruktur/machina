"""Explicit passwordless sudo escalation over the bounded command lane.

Card ``04-sudo`` against contracts.md **C04 rule 1/3** (the target profile
determines the privilege mode; ``CommandRequest`` carries the privilege
request) and **C03 rule 5** (``deny`` takes precedence everywhere; enabling
a raw shell/root capability forfeits app-level OS containment claims). The
frozen target vocabulary is :class:`mai.contracts.server.PrilegeMode`
(``none | sudo | root``, produced by the reviewed P02 schema):

- ``none`` — the profile forbids escalation. Any sudo request is refused
  **before anything is composed or transported** (fail-closed; negative
  control at both the unit and fixture boundary). No configuration can
  relax this: the refusal precedes every effect.
- ``sudo`` — the target account holds an explicit passwordless grant
  (sudoers ``NOPASSWD``). Escalation is **noninteractive**
  (``sudo -n --``): sudo either runs the payload under its grant or fails
  immediately — it can never wait for a password, so a missing/insufficient
  grant is detected as a structured denial within the request's wall-clock
  budget instead of hanging (acceptance 1). This lane has NO password
  input by construction: interactive/passworded sudo and the approval-bound
  password channel are different cards (``04-password-channel`` with the
  approval store, contracts C03 rule 4). Secret material cannot enter this
  module, the escalated command or its outcome.
- ``root`` — the login user already holds the privileged identity; the
  payload runs verbatim (no wrapper, no grant assumed). That the payload
  really runs as uid 0 is a property of the TARGET profile and is verified
  only against a disposable fixture, never claimed about any other host
  (acceptance 2).

Boundary: this lane composes on :func:`mai.execution.commands.execute_command`
without modifying it — the derived request is a structured argv whose last
element is the shell-quoted privileged payload, so every C04 rule 2/3
guarantee (element-wise quoting, cwd guard, timeout, output budget,
transport vocabulary) applies unchanged to the escalated form. Timeout and
client-death semantics are inherited exactly: the escalated payload may
outlive the channel and run on as root, so the outcome stays
``outcome_unknown`` (never ``failed``, never retry-safe); durable dispatch
intent, receipts and cancellation belong to the supervisor lane
(``04-supervisor``/``04-reconcile``). Target-side sudoers scope (which
commands the grant covers) is administrative policy on the target, outside
this module's control; a grant that does not cover the wrapper is a denial.

Grant-failure classification mirrors the ``commands`` lane's marker rule
(the marker, not the exit code alone, decides): the escalated wrapper
emits a fixed start marker on stderr before the payload could run. A
``failed`` outcome is classified as a sudo denial only when the payload
provably never started: exit 1, untruncated stderr, start marker absent,
and a known sudo refusal string present. Anything else stays a generic
payload failure — misclassification can hide a denial, never invent one.
"""

from __future__ import annotations

import dataclasses
import shlex
from dataclasses import dataclass
from enum import StrEnum

from mai.contracts.server import PrivilegeMode, SshIdentity
from mai.execution.commands import (
    MAX_REMOTE_COMMAND_BYTES,
    CommandOutcome,
    CommandRequest,
    CommandRequestError,
    CommandStatus,
    compose_remote_command,
    execute_command,
)

__all__ = [
    "PAYLOAD_START_MARKER",
    "PrivilegeErrorCode",
    "PrivilegeRequestError",
    "SudoDeniedError",
    "SUDO_PROGRAM",
    "SUDO_WRAPPER_SHELL",
    "execute_privileged_command",
    "privileged_remote_command",
]

#: Noninteractive sudo client (``-n``: never prompt, fail instead) and the
#: POSIX shell that carries the composed payload. Both absolute-vocabulary
#: constants: the escalated argv is structured, the payload is quoted once.
SUDO_PROGRAM = "sudo"
SUDO_WRAPPER_SHELL = "/bin/sh"

#: Fixed stderr marker the wrapper emits BEFORE the payload could start.
#: A sudo refusal happens before this marker exists, so its absence is what
#: makes a denial classification sound (payload provably never started).
PAYLOAD_START_MARKER = "mai-exec: privileged payload started"

#: Known sudo refusal texts (C locale) for the classified denial. An unknown
#: refusal variant stays a generic payload failure — a fail-safe direction:
#: no invented success, no invented denial.
_SUDO_DENIAL_MARKERS = (
    "a password is required",  # no NOPASSWD grant covers this escalation
    "not in the sudoers file",  # account has no sudo membership at all
    "may not run",  # grant exists but refuses this command ("Sorry, user …")
    "no tty present",  # defensive: a prompt demand can never be answered here
    "problem with sudoers entries",  # broken target sudoers configuration
)


class PrivilegeErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    PRIVILEGE_DISABLED = "privilege_disabled"
    SUDO_DENIED = "sudo_denied"


class PrivilegeRequestError(ValueError):
    """Pre-effect refusal of a privilege request (C01 envelope shape).

    Raised before anything is composed or transported — ``mode=none``
    cannot request sudo, whatever the request contains. ``detail`` is
    value-free: it never quotes request content.
    """

    def __init__(self, code: PrivilegeErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class SudoDeniedError(ValueError):
    """Classified refusal of the passwordless sudo grant on the target.

    Raised after transport succeeded and sudo refused the escalation
    WITHOUT running the payload (start-marker rule). ``detail`` carries
    only host:port, the stable reason and a bounded value-free stderr
    tail — sudo refusal texts name no secret material.
    """

    def __init__(self, code: PrivilegeErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def _bounded_tail(stderr: str) -> str:
    """Bounded single-line stderr tail for error details (value-free).

    Local copy of the ``mai.execution.commands`` helper: that module is
    outside this card's allowlist and its helper is private; a divergence
    would be a review finding, not a silent import of a private symbol.
    """
    flat = " ".join(stderr.split())
    return flat[-200:] if flat else "<no stderr>"


def _sudo_wrapper_argv(guarded_payload: str) -> tuple[str, ...]:
    """Structured escalation argv around the once-quoted payload (pure).

    ``sudo -n`` is the noninteractivity contract (acceptance 1); ``--``
    ends sudo's own option parsing so a payload can never inject client
    options; ``/bin/sh -c`` executes the composed payload exactly as the
    unprivileged lane would have (its cwd guard included, now under the
    privileged identity).
    """
    return (
        SUDO_PROGRAM,
        "-n",
        "--",
        SUDO_WRAPPER_SHELL,
        "-c",
        guarded_payload,
    )


def _guarded_payload(request: CommandRequest) -> str:
    """Payload prefixed by the stderr start marker (pure; sudo lane only)."""
    return f"echo '{PAYLOAD_START_MARKER}' 1>&2; {compose_remote_command(request)}"


def privileged_remote_command(request: CommandRequest, mode: PrivilegeMode) -> str:
    """Compose the remote command for the profile privilege mode (pure).

    Returns the transport representation that
    :func:`mai.execution.commands.execute_command` would drive for the
    derived request, byte-identically:

    - ``none`` — raises :class:`PrivilegeRequestError` (``privilege_disabled``)
      before any string is produced (acceptance 2, fail-closed);
    - ``root`` — the payload verbatim (the login identity is privileged);
    - ``sudo`` — ``sudo -n -- /bin/sh -c <quoted payload-with-start-marker>``.

    Raises:
        PrivilegeRequestError: ``mode`` is :attr:`PrivilegeMode.NONE`.
        CommandRequestError: the composed escalation exceeds
            ``MAX_REMOTE_COMMAND_BYTES`` (rejected before any effect).

    Raises ``TypeError`` for a ``mode`` that is not a
    :class:`mai.contracts.server.PrilegeMode` — raw strings are refused so
    an unvalidated profile value can never select a lane by accident.
    """
    if not isinstance(mode, PrivilegeMode):
        raise TypeError("mode must be a PrivilegeMode from mai.contracts.server")
    if mode is PrivilegeMode.NONE:
        raise PrivilegeRequestError(
            PrivilegeErrorCode.PRIVILEGE_DISABLED,
            "privilege mode 'none' cannot request sudo escalation",
        )
    if mode is PrivilegeMode.ROOT:
        return compose_remote_command(request)
    remote = " ".join(
        shlex.quote(element)
        for element in _sudo_wrapper_argv(_guarded_payload(request))
    )
    if len(remote.encode("utf-8")) > MAX_REMOTE_COMMAND_BYTES:
        raise CommandRequestError(
            f"composed privileged remote command exceeds "
            f"{MAX_REMOTE_COMMAND_BYTES} bytes"
        )
    return remote


def _is_sudo_denial(outcome: CommandOutcome) -> bool:
    """Decide whether a failed escalated outcome is a grant refusal (pure).

    Soundness rule: the payload provably never started. All of — exit 1
    (sudo's refusal exit; a payload exit shares it, hence the rest),
    stderr NOT truncated (a truncated marker stream would be evidence we
    cannot read), the start marker absent (payload never ran) and a known
    sudo refusal text present.
    """
    if outcome.exit_code != 1 or outcome.stderr_truncated:
        return False
    if PAYLOAD_START_MARKER in outcome.stderr:
        return False
    return any(marker in outcome.stderr for marker in _SUDO_DENIAL_MARKERS)


def execute_privileged_command(
    identity: SshIdentity,
    request: CommandRequest,
    mode: PrivilegeMode,
    *,
    key_path: os.PathLike[str] | str,
    known_hosts_path: os.PathLike[str] | str,
) -> CommandOutcome:
    """Run one bounded command under the profile's privilege mode.

    Composition is delegated to the frozen bounded lane: the derived
    request preserves ``op_id``/``server_id``/``display``/``timeout``/
    ``output_budget`` and carries the escalation as structured argv
    (``working_directory`` moves INSIDE the escalated payload, so the
    privileged process honours the target-side ``cd`` guard; the direct
    lane's ``cwd_change_failed`` convenience flag is therefore not set for
    escalated cwd refusals — the reserved exit code plus stderr marker
    remain the detection contract, as in ``commands.py``).

    Returns:
        CommandOutcome: for every escalated command that RAN — ``succeeded``
        (exit 0), ``failed`` (known non-zero payload exit, including an
        unclassified sudo refusal variant) or ``outcome_unknown`` (timeout
        cut or ssh client death — the root payload may live on; no exit is
        invented, the outcome is never retry-safe).

    Raises:
        PrivilegeRequestError: ``mode`` is :attr:`PrivilegeMode.NONE` —
            before any composition, connection or effect (acceptance 2).
        TypeError: ``mode`` is not a :class:`PrivilegeMode`.
        CommandRequestError: malformed request or oversized escalation
            (rejected before any remote effect).
        SshConnectError: transport-level failure (endpoint refused,
            unreachable, auth rejected) — nothing ran on the target.
        SudoDeniedError: the target refused the passwordless sudo grant
            and the payload provably never started (acceptance 1; detected
            inside the request timeout — ``sudo -n`` cannot hang on a
            password prompt).
    """
    # Validates mode (incl. the `none` refusal) and the escalation budget
    # before anything is transported; composition is repeated byte-identically
    # by the bounded lane for the derived request.
    privileged_remote_command(request, mode)
    if mode is PrivilegeMode.SUDO:
        derived = dataclasses.replace(
            request,
            argv=_sudo_wrapper_argv(_guarded_payload(request)),
            shell=None,
            working_directory=None,
        )
    else:
        derived = request  # root mode: privileged identity already, run verbatim
    outcome = execute_command(
        identity,
        derived,
        key_path=key_path,
        known_hosts_path=known_hosts_path,
    )
    if mode is PrivilegeMode.SUDO and outcome.status is CommandStatus.FAILED:
        if _is_sudo_denial(outcome):
            raise SudoDeniedError(
                PrivilegeErrorCode.SUDO_DENIED,
                f"{identity.host}:{identity.port} refused the passwordless "
                f"sudo escalation; no NOPASSWD grant covers this command "
                f"(stderr: {_bounded_tail(outcome.stderr)})",
            )
    return outcome
