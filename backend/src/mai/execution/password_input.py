"""Approval-gated sudo password channel — the ephemeral secret transport.

Card ``04-password-channel`` against contracts.md **C03 rules 2/4/6** (the
``ExecutionContext`` never serializes secret values; approval binding and
one-use consumption; credentials resolve at effect time) and **C04 rule 2**
(Linux command inputs are structured argv where possible; *credentials are
never concatenated into shell or argv*). This module is the password-
REQUIRED sibling of the passwordless lane (``mai.execution.privilege``):
when the target account holds no ``NOPASSWD`` grant, the human decision and
the sudo secret travel together through this channel — and nothing flows
until the approval store has spent that decision.

The gate is the approval, not the caller (C03 rule 4)
-----------------------------------------------------

:func:`execute_passworded_command` consumes the one-use decision itself,
via :meth:`mai.policy.approvals.ApprovalStore.consume`, BEFORE any secret
byte is placed on any wire. Every denial of that consumption — unknown id,
replay, expiry, changed capability/target/payload/policy-revision — is the
store's structured, value-free :class:`mai.policy.approvals.ApprovalError`
propagating unchanged; no transport attempt is made and the secret buffer
is wiped. Two pre-consume rejections protect the decision from being
burned on a doomed presentation (the row stays pending, exactly like the
store's own denial discipline):

- **account binding** — the challenge names the authenticated target
  account (:attr:`PasswordChallenge.account`); a challenge presented over
  an :class:`~mai.contracts.server.SshIdentity` logged in as a different
  account is refused (``account_mismatch``). ``sudo -S`` authenticates the
  invoking account only, so a mismatched pair could never succeed anyway —
  the refusal makes the failure explicit instead of spending the decision
  on it.
- **payload binding** — the channel recomputes the SHA-256 of the exact
  payload it would transport and requires it to equal the approval's
  ``payload_hash`` field-for-field (constant-time compare;
  ``payload_binding_mismatch``). One decision authorizes exactly one
  command, so an approval can never ride along on an altered payload
  (C03 rule 4; the tabletop altered-payload denial, enforced at this lane
  too).

Secret transport shape
----------------------

The secret travels as ONE stdin line to ``sudo -S`` across the
pinned-identity SSH channel (system ``ssh(1)``, frozen option surface):

- never in argv, never in the process environment, never in a file on
  either host — the composed remote command contains no secret parameter
  by construction (C04 rule 2);
- protected in transit by the SSH transport itself (pinned host key,
  ``IdentitiesOnly`` key auth);
- single-use and short-lived: one challenge ⇒ one command ⇒ the caller's
  ``bytearray`` is zero-filled after the attempt, whatever the outcome
  (success, wrong password, timeout, transport error). The wipe covers the
  buffer this module was handed; CPython cannot guarantee that no transient
  copy ever existed (documented honesty, same posture as the store's
  hash-only payloads — producers SHOULD pass the material as a
  ``bytearray`` and never as an immutable ``str``/``bytes``);
- no persistence anywhere: the module performs no logging, no store writes
  and no file IO; error details are value-free and additionally
  best-effort redact the secret (defense in depth). Job-state records,
  receipts and logs belong to the supervisor/reconcile lanes, which never
  see the secret because it never enters any request or outcome field.

Outcome and refusal classification
----------------------------------

The lane reuses the bounded-lane outcome vocabulary
(:class:`mai.execution.commands.CommandOutcome`, frozen C04 rule 4):
``succeeded`` / ``failed`` / ``outcome_unknown`` with the same unknown-
semantics honesty — a budget-cut escalated payload may live on as root,
so the outcome is never an invented exit and never retry-safe.

A sudo authentication refusal is CLASSIFIED only when the payload provably
never started (the privilege-lane start-marker rule): exit 1, untruncated
stderr, start marker absent, and a known sudo refusal text present. The
measured C-locale texts (disposable Arch fixture, sudo over PAM):

- wrong password ⇒ ``sudo: <N> incorrect password attempt(s)`` ⇒
  ``password_rejected``;
- expired/locked account ⇒ ``sudo: account or password is expired/locked,
  contact your administrator`` ⇒ ``account_locked``.

Fail-safe direction: an untruncated unknown refusal stays a generic
``failed`` outcome — misclassification can hide a denial, never invent one.
The channel performs EXACTLY ONE authentication attempt: no retry loop,
so a human re-decision (a fresh approval) is required for another attempt
and target lockout counters are never prolonged (acceptance 2).

**Locked-account limitation (explicit, acceptance 2):** a shadow-locked
account (``passwd -l``) makes even the CORRECT password fail exactly like
a wrong one through PAM — the two are indistinguishable at this boundary,
so the channel reports ``password_rejected`` for both and the operator
must reconcile the account state on the target. Only sudo's own
expired/locked diagnostic (measured for expired accounts) is classified
``account_locked``. No unlock, reset or retry is attempted by this module.
**Measured aggravation (disposable Arch fixture, pam_faillock active in
``/etc/pam.d/system-auth``):** failed sudo attempts arm the target's
fail-lock tally, and once armed even the CORRECT password keeps being
denied until the tally decays or an operator resets it — one more reason
this lane performs exactly ONE attempt per decision and requires a fresh
human decision (plus target-side reconciliation) after any refusal.

**Channel-reuse limitation:** the lane assumes the target sudoers grant
actually requires a password for this account/command (the card's
premise). A target that silently skips authentication (``NOPASSWD``,
``!authenticate``) leaves the secret line unconsumed on the payload's
stdin; producers must not point this channel at accounts whose profile
grant bypasses authentication. Related: a target with ``Defaults
requiretty`` cannot use this lane at all (no tty exists); its refusal
text is not classified here and surfaces as a generic ``failed`` outcome.

Layering: this lane composes the frozen transport primitives
(:func:`mai.execution.ssh.ssh_arguments`,
:class:`mai.execution.commands.CommandRequest` vocabulary) without
modifying them; durable dispatch intent, receipts, cancellation and
outcome reconciliation belong to the supervisor lane (``04-supervisor`` /
``04-reconcile``). The HTTP challenge endpoint is a different card
(``04-password-api``); this module neither parses requests nor serves.

Composition duty: the function is ``async`` because the approval gate is
(the store is async), while the transport itself is the bounded BLOCKING
system-ssh lane. An async host (the API process) must run the whole call
via ``anyio.to_thread.run_sync`` / ``asyncio.to_thread`` so the bounded
wait never stalls its event loop; the secret buffer is owned by the
calling thread for the duration of the call, which is exactly the
single-use window.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import selectors
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from mai.contracts.server import SshIdentity
from mai.execution.commands import (
    MAX_REMOTE_COMMAND_BYTES,
    READ_CHUNK_BYTES,
    CommandOutcome,
    CommandRequest,
    CommandRequestError,
    CommandStatus,
    compose_remote_command,
)
from mai.execution.privilege import (
    PAYLOAD_START_MARKER,
    SUDO_PROGRAM,
    SUDO_WRAPPER_SHELL,
)
from mai.execution.ssh import SshConnectError, SshErrorCode, ssh_arguments
from mai.policy.approvals import ApprovalStore

__all__ = [
    "MAX_SECRET_BYTES",
    "PasswordChallenge",
    "PasswordChannelError",
    "PasswordErrorCode",
    "execute_passworded_command",
    "guarded_passworded_payload",
    "passworded_payload_hash",
    "passworded_remote_command",
]

#: Hard cap on the accepted secret (bytes). DoS/line-protocol bound, not a
#: password policy: the secret travels as ONE stdin line, so it must be
#: finite, non-empty and free of line delimiters.
MAX_SECRET_BYTES: Final[int] = 1024

#: The password-required sudo client invocation: read the passphrase from
#: stdin (``-S``), with an EMPTY prompt (``-p ''`` — the refusal texts on
#: stderr remain; no prompt noise ever reaches an outcome), stop parsing
#: sudo options at ``--`` so a payload can never inject client options,
#: and run the once-quoted guarded payload under the escalation shell.
_SUDO_PASSWORD_ARGV_HEAD: Final[tuple[str, ...]] = (
    SUDO_PROGRAM,
    "-S",
    "-p",
    "",
    "--",
    SUDO_WRAPPER_SHELL,
    "-c",
)

#: Measured sudo authentication-refusal texts (C locale, sudo over PAM on
#: the disposable Arch fixture). Local copies of the privilege lane's
#: marker discipline: the classification needs texts, the texts are sudo's
#: C-locale diagnostics; an unknown variant stays a generic payload
#: failure — no invented denial.
_WRONG_PASSWORD_MARKERS: Final[tuple[str, ...]] = (
    "incorrect password attempt",
)
_ACCOUNT_LOCKED_MARKERS: Final[tuple[str, ...]] = (
    "account or password is expired/locked",
)

#: Replacement token for best-effort secret redaction in error details.
_REDACTED: Final[str] = "***redacted***"

#: Capability identifier shape — the registry slug charset the approval
#: store validates (local copy of the shared note: the shape owner is
#: outside this card's allowlist; a divergence is a review finding, not a
#: silent import of a private symbol).
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

#: SHA-256 hex digest shape for the payload binding (approval store shape).
_PAYLOAD_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

#: SSH login account name shape — the canonical profile schema's conservative
#: charset (no whitespace, no ``@``/``:``), kept local for the same reason
#: as above: the schema module owns the shape and is outside the allowlist.
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")

#: Principal length bound — mirrors the approval store's session-principal
#: bound (C01 rule 2: principals are authenticated identities, DoS-bounded).
MAX_PRINCIPAL_CHARS: Final[int] = 256


class PasswordErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary).

    Approval-consumption denials are NOT re-encoded here: the store's
    frozen codes (``not_found``, ``already_consumed``, ``expired``,
    ``*_mismatch``) propagate unchanged as
    :class:`mai.policy.approvals.ApprovalError` — the decision layer keeps
    one vocabulary.
    """

    INVALID_REQUEST = "invalid_request"
    ACCOUNT_MISMATCH = "account_mismatch"
    PAYLOAD_BINDING_MISMATCH = "payload_binding_mismatch"
    PASSWORD_REJECTED = "password_rejected"
    ACCOUNT_LOCKED = "account_locked"


class PasswordChannelError(ValueError):
    """One structured channel rejection (C01 envelope shape).

    ``detail`` is single-line, bounded and value-free — field names, host
    and stable reasons only. When a sudo refusal detail is built, any
    occurrence of the presented secret is redacted (defense in depth; the
    refusal texts themselves name no material).
    """

    def __init__(self, code: PasswordErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PasswordChallenge:
    """The approved, bound presentation of one sudo password challenge.

    ``approval_id``/``capability_id``/``server_id``/``payload_hash``/
    ``policy_revision`` are the store's binding fields, presented
    field-for-field at consumption. ``account`` is the authenticated
    target account whose sudo credential this challenge authorizes (it
    must equal :attr:`SshIdentity.user` of the transport identity —
    ``sudo -S`` can only authenticate the invoking account).
    ``consuming_principal`` is the authenticated session principal this
    submission is attributed to (C01 rule 2: from the session, never a
    client-supplied flag — the HTTP layer's obligation).

    Raises:
        PasswordChannelError: ``invalid_request`` for any malformed field,
            before any state is read and without consuming anything.
    """

    approval_id: uuid.UUID
    capability_id: str
    server_id: uuid.UUID
    payload_hash: str
    policy_revision: int
    account: str
    consuming_principal: str

    def __post_init__(self) -> None:
        if not isinstance(self.approval_id, uuid.UUID):
            raise _invalid("approval_id must be a uuid.UUID (C01 rule 1)")
        if not isinstance(self.capability_id, str) or not _IDENTIFIER_RE.fullmatch(
            self.capability_id
        ):
            raise _invalid("capability_id must match the capability slug shape")
        if not isinstance(self.server_id, uuid.UUID):
            raise _invalid("server_id must be a uuid.UUID (C01 rule 1)")
        if not isinstance(self.payload_hash, str) or not _PAYLOAD_HASH_RE.fullmatch(
            self.payload_hash
        ):
            raise _invalid(
                "payload_hash must be a lowercase SHA-256 hex digest of the "
                "approved payload (C03 rule 4)"
            )
        if isinstance(self.policy_revision, bool) or not isinstance(
            self.policy_revision, int
        ) or self.policy_revision < 1:
            raise _invalid("policy_revision must be an int >= 1")
        if not isinstance(self.account, str) or not _ACCOUNT_RE.fullmatch(
            self.account
        ):
            raise _invalid("account must be a valid SSH account name")
        if not isinstance(self.consuming_principal, str) or not (
            1 <= len(self.consuming_principal) <= MAX_PRINCIPAL_CHARS
        ):
            raise _invalid(
                "consuming_principal must be a non-empty session principal "
                f"(at most {MAX_PRINCIPAL_CHARS} chars, C01 rule 2)"
            )


def _invalid(detail: str) -> PasswordChannelError:
    return PasswordChannelError(PasswordErrorCode.INVALID_REQUEST, detail)


# ---------------------------------------------------------------------------
# Pure composition (byte-identical with the transported form)
# ---------------------------------------------------------------------------


def guarded_passworded_payload(request: CommandRequest) -> str:
    """The escalated payload: start marker first, then the bounded command.

    Byte-identical with what the transport runs inside
    ``/bin/sh -c``. The marker is emitted BEFORE the payload could start,
    so its absence on a failed outcome is the soundness proof of the
    refusal classification (payload provably never ran) — the same rule
    the passwordless lane established.
    """
    return f"echo '{PAYLOAD_START_MARKER}' 1>&2; {compose_remote_command(request)}"


def passworded_remote_command(request: CommandRequest) -> str:
    """Transport representation: the once-quoted ``sudo -S`` argv (pure).

    Raises:
        CommandRequestError: the composed form exceeds
            ``MAX_REMOTE_COMMAND_BYTES`` (rejected before any effect).
    """
    remote = " ".join(
        shlex.quote(element)
        for element in (*_SUDO_PASSWORD_ARGV_HEAD, guarded_passworded_payload(request))
    )
    if len(remote.encode("utf-8")) > MAX_REMOTE_COMMAND_BYTES:
        raise CommandRequestError(
            f"composed passworded remote command exceeds "
            f"{MAX_REMOTE_COMMAND_BYTES} bytes"
        )
    return remote


def passworded_payload_hash(request: CommandRequest) -> str:
    """SHA-256 hex of the exact transported payload (approval binding input).

    Producers create the human decision bound to THIS hash — the channel
    recomputes it at use time and refuses a mismatch before consuming the
    decision, so one approval can never authorize a different command.
    """
    return hashlib.sha256(
        guarded_passworded_payload(request).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Secret buffer discipline
# ---------------------------------------------------------------------------


def _validated_secret(secret: object) -> bytearray:
    """Validate the presented secret material (value-free rejections).

    The secret MUST be a caller-owned ``bytearray``: the channel's wipe
    contract (zero-fill after the attempt) needs a mutable buffer it was
    handed, and immutable ``str``/``bytes`` cannot honor it — accepting
    them would silently downgrade the acceptance guarantee. A copy is
    never made: the buffer is written to the pipe in place and zeroed in
    place.
    """
    if not isinstance(secret, bytearray):
        raise _invalid(
            "secret must be a bytearray (the channel wipes the buffer it "
            "was handed; immutable str/bytes cannot be wiped)"
        )
    if not 1 <= len(secret) <= MAX_SECRET_BYTES:
        raise _invalid(
            f"secret must be 1..{MAX_SECRET_BYTES} bytes (single stdin line)"
        )
    if b"\n" in secret or b"\r" in secret or b"\x00" in secret:
        raise _invalid(
            "secret must be a single line without newline/CR/NUL bytes"
        )
    return secret


def _wipe(secret: bytearray) -> None:
    """Zero-fill the handed buffer (best-effort, in place)."""
    secret[:] = b"\x00" * len(secret)


def _redact(text: str, secret: bytearray) -> str:
    """Best-effort removal of the secret from a diagnostic string."""
    material = secret.decode("utf-8", errors="replace")
    if material:
        text = text.replace(material, _REDACTED)
    return text


# ---------------------------------------------------------------------------
# Bounded stream capture (local copy of the commands-lane loop)
# ---------------------------------------------------------------------------


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
) -> tuple[bytes, int, bytes, int, bool]:
    """Drain both pipes until EOF or ``deadline``; kill the client at cut.

    Local copy of the bounded lane's capture discipline (that module is
    outside this card's allowlist; its helper is private): non-blocking
    reads, bounded retention, full drain so a completing command's exit
    stays known, and a deadline kill after which the remote payload may
    live on (the outcome stays unknown).
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
    return (
        bytes(captures["stdout"].retained),
        captures["stdout"].total,
        bytes(captures["stderr"].retained),
        captures["stderr"].total,
        timed_out,
    )


def _bounded_tail(stderr: str) -> str:
    """Bounded single-line stderr tail for error details (value-free)."""
    flat = " ".join(stderr.split())
    return flat[-200:] if flat else "<no stderr>"


def _map_transport_failure(rc: int, stderr: str, endpoint: str) -> SshConnectError:
    """Map a non-zero ssh exit to the frozen transport vocabulary.

    Local copy of the bounded lane's mapper (same allowlist note): ssh(1)
    reports its own errors as exit 255; classification is by stderr text.
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


# ---------------------------------------------------------------------------
# The gated transport
# ---------------------------------------------------------------------------


def _classify_refusal(
    outcome: CommandOutcome, secret: bytearray
) -> PasswordChannelError | None:
    """Classify a failed outcome as a sudo authentication refusal (pure).

    Soundness rule (privilege-lane marker rule): exit 1, stderr NOT
    truncated (a cut refusal stream is evidence we cannot read), the start
    marker absent (payload provably never started) and a KNOWN refusal
    text present. Anything else is ``None`` — an unclassified failure
    stays a generic payload outcome (fail-safe: never invent a denial).
    """
    if (
        outcome.exit_code != 1
        or outcome.stderr_truncated
        or PAYLOAD_START_MARKER in outcome.stderr
    ):
        return None
    if any(marker in outcome.stderr for marker in _ACCOUNT_LOCKED_MARKERS):
        detail = _redact(
            f"sudo refused the challenge as expired/locked "
            f"(stderr: {_bounded_tail(outcome.stderr)})",
            secret,
        )
        return PasswordChannelError(PasswordErrorCode.ACCOUNT_LOCKED, detail)
    if any(marker in outcome.stderr for marker in _WRONG_PASSWORD_MARKERS):
        detail = _redact(
            "sudo rejected the presented secret "
            f"(stderr: {_bounded_tail(outcome.stderr)}); "
            "the decision is spent — a retry requires a fresh approval",
            secret,
        )
        return PasswordChannelError(PasswordErrorCode.PASSWORD_REJECTED, detail)
    return None


async def execute_passworded_command(
    store: ApprovalStore,
    challenge: PasswordChallenge,
    identity: SshIdentity,
    request: CommandRequest,
    secret: bytearray,
    *,
    key_path: os.PathLike[str] | str,
    known_hosts_path: os.PathLike[str] | str,
) -> CommandOutcome:
    """Spend one approval decision, then transport its secret to ``sudo -S``.

    Order (fail-closed at every seam; the decision is never burned on a
    doomed presentation):

    1. structural validation of every argument (``invalid_request``);
    2. account binding: ``challenge.account`` must equal the identity's
       login account (``account_mismatch``);
    3. composition + payload binding: the recomputed hash of the exact
       transported payload must equal the challenge's ``payload_hash``
       (``payload_binding_mismatch``; composition budget violations raise
       :class:`~mai.execution.commands.CommandRequestError` here, still
       pre-consume);
    4. ONE-USE consumption via ``store.consume`` — the human gate. Any
       :class:`~mai.policy.approvals.ApprovalError` (unknown id, replay,
       expiry, changed binding) propagates unchanged: no transport
       attempt, no effect;
    5. transport: the secret is written as one stdin line to ``sudo -S``
       over the pinned SSH channel; the buffer is wiped in ``finally``;
    6. classification: a refusal whose payload provably never started
       raises :class:`PasswordChannelError` (``password_rejected`` /
       ``account_locked``); every started payload returns its
       :class:`~mai.execution.commands.CommandOutcome`.

    Exactly ONE authentication attempt is made — never a retry loop.

    Returns:
        CommandOutcome: for every payload that RAN — ``succeeded``,
        ``failed`` (unclassified payload exit, including an unknown sudo
        refusal variant) or ``outcome_unknown`` (budget cut / client
        death; the root payload may live on — never retry-safe).

    Raises:
        PasswordChannelError: malformed arguments (``invalid_request``),
            account mismatch, payload-binding mismatch, or a classified
            sudo authentication refusal.
        ApprovalError: the approval gate denied (stable store codes,
            unchanged).
        CommandRequestError: malformed request or oversized composed
            transport form (pre-consume).
        SshConnectError: transport-level failure — nothing ran on the
            target; the secret never reached it (ssh never launched the
            remote command).
    """
    if not isinstance(challenge, PasswordChallenge):
        raise _invalid("challenge must be a PasswordChallenge")
    if not isinstance(identity, SshIdentity):
        raise _invalid("identity must be an SshIdentity from mai.contracts.server")
    if not isinstance(request, CommandRequest):
        raise _invalid("request must be a CommandRequest")
    checked_secret = _validated_secret(secret)

    if challenge.account != identity.user:
        raise PasswordChannelError(
            PasswordErrorCode.ACCOUNT_MISMATCH,
            f"challenge account {challenge.account!r} is not the "
            f"authenticated transport account {identity.user!r} "
            "(sudo -S can only authenticate the invoking account)",
        )

    remote = passworded_remote_command(request)
    computed_hash = passworded_payload_hash(request)
    if not hmac.compare_digest(computed_hash, challenge.payload_hash):
        raise PasswordChannelError(
            PasswordErrorCode.PAYLOAD_BINDING_MISMATCH,
            "presented payload_hash differs from the recomputed hash of the "
            "transported payload (altered payload requires a new approval, "
            "C03 rule 4); the decision was NOT consumed",
        )

    known_hosts = os.fspath(known_hosts_path)
    if not os.path.isfile(known_hosts):
        raise SshConnectError(
            SshErrorCode.INVALID_CONFIGURATION,
            f"managed known-hosts file {known_hosts} does not exist; "
            "enroll the pinned host key explicitly before connecting",
        )

    # ---- the gate: from here the decision may be spent; the secret is ----
    # ---- consumed (wiped) whatever happens below.                     ----
    try:
        await store.consume(
            challenge.approval_id,
            capability_id=challenge.capability_id,
            server_id=challenge.server_id,
            payload_hash=challenge.payload_hash,
            policy_revision=challenge.policy_revision,
            consumed_by=challenge.consuming_principal,
        )

        argv = ssh_arguments(
            identity,
            key_path=os.fspath(key_path),
            known_hosts_path=known_hosts,
            remote_command=remote,
        )
        started = time.monotonic()
        try:
            # Bounded blocking subprocess by design (the frozen system-ssh
            # lane; the wall clock is the request's timeout). ASYNC220 is
            # deliberate: async callers MUST run this whole lane via
            # anyio.to_thread/asyncio.to_thread so the bounded wait never
            # stalls their event loop — the composition boundary's duty.
            proc = subprocess.Popen(  # noqa: ASYNC220
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise SshConnectError(
                SshErrorCode.SSH_FAILED,
                f"ssh client could not be started ({type(error).__name__})",
            ) from None
        try:
            try:
                assert proc.stdin is not None  # Popen(PIPE) guarantees
                proc.stdin.write(checked_secret)
                proc.stdin.write(b"\n")
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                # The client is dying before sudo could read: proceed to
                # the capture loop; the outcome assembly decides (unknown
                # on a dead client, never an invented exit).
                pass
            out, out_total, err, err_total, timed_out = _capture_streams(
                proc,
                deadline=started + request.timeout,
                budget=request.output_budget,
            )
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
        duration = time.monotonic() - started

        outcome = _assemble_outcome(
            f"{identity.host}:{identity.port}",
            request,
            proc,
            out, out_total, err, err_total,
            timed_out, duration,
        )
        if outcome.status is CommandStatus.FAILED:
            refusal = _classify_refusal(outcome, checked_secret)
            if refusal is not None:
                raise refusal
        return outcome
    finally:
        _wipe(checked_secret)


def _assemble_outcome(
    endpoint: str,
    request: CommandRequest,
    proc: subprocess.Popen[bytes],
    out: bytes,
    out_total: int,
    err: bytes,
    err_total: int,
    timed_out: bool,
    duration: float,
) -> CommandOutcome:
    """Map the captured transport to the frozen outcome vocabulary."""
    stdout_text = out.decode("utf-8", errors="replace")
    stderr_text = err.decode("utf-8", errors="replace")
    exit_code: int | None
    if timed_out:
        status = CommandStatus.OUTCOME_UNKNOWN
        exit_code = None
    else:
        returncode = proc.returncode
        assert returncode is not None  # proc.wait() completed
        if returncode == 0:
            exit_code = 0
            status = CommandStatus.SUCCEEDED
        elif returncode == 255:
            raise _map_transport_failure(returncode, stderr_text, endpoint)
        elif returncode < 0:
            # ssh client died by signal: the remote payload may live on —
            # unknown, not failed; no client-side signal is a command exit.
            status = CommandStatus.OUTCOME_UNKNOWN
            exit_code = None
        else:
            exit_code = returncode
            status = CommandStatus.FAILED
    return CommandOutcome(
        op_id=request.op_id,
        server_id=request.server_id,
        display=request.display,
        status=status,
        exit_code=exit_code,
        stdout=stdout_text,
        stdout_bytes=out_total,
        stdout_truncated=out_total > len(out),
        stderr=stderr_text,
        stderr_bytes=err_total,
        stderr_truncated=err_total > len(err),
        timed_out=timed_out,
        # The cwd guard travels INSIDE the escalated payload (the guarded
        # composition), so the convenience flag is never populated here —
        # the reserved exit code + marker remain the detection contract.
        cwd_change_failed=False,
        duration_seconds=duration,
    )
