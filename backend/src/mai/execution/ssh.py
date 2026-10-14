"""SSH transport: pinned-identity connection and target environment detection.

Card ``04-ssh-connect`` against contracts.md **C04** (transport frozen by the
host-supervision ADR, measured in ``experiments/ssh_supervision_probe.py``):

- **C04 rule 1** — the target profile determines the SSH host key,
  endpoint/user and environment. The host key is pinned as the OpenSSH
  ``SHA256:<43-char base64>`` fingerprint (format measured in the P00 spike,
  ``mai_evidence/00-ssh-spike/exp4/identity-before.json``). A changed host
  key is refused — there is no silent auto-trust on reconnect
  (AUTH-S4-class negative control, scenario SSH-PIN). Re-enrollment is the
  explicit :func:`pin_host_key` call with the admin-accepted fingerprint,
  which records the accepted key in the managed known-hosts file.
- **C03 rule 6** — credentials resolve by reference. ``ssh.credential_ref``
  is an environment-variable name; the host resolves it through
  :class:`mai.credentials.env.EnvResolver` (the mandatory first resolver) to
  the controller-local private-key path passed to ``ssh -i``. This module
  never reads key material, never serializes a :class:`SecretValue`, and all
  error details are value-free.
- **C04 rule 3 boundary** — this module is the *connection + detection*
  lane only. Command dispatch stays with ``04-supervisor`` (target-local
  wrapper + durable receipts; the SSH session is never the owner). The only
  remote command executed here is the fixed, bounded identity probe.

Dependency decision (frozen at P00, guarded by
``backend/tests/test_execution_dependencies.py``): the transport is the
SYSTEM OpenSSH client driven via ``subprocess`` — no third-party SSH
library. The ``ssh(1)`` option surface is exactly the frozen probe surface
(``IdentitiesOnly``/``BatchMode``/``StrictHostKeyChecking=yes``/
``UserKnownHostsFile``/``ConnectTimeout``); nothing else may be relied on.

Environment detection (**acceptance 2**): :func:`probe_environment` returns
only what the TARGET itself reports (``uname``, ``/etc/os-release``,
``/proc/sys/kernel/random/boot_id``). No field is derived from the local
platform — the controller being a container, Windows or WSL is never
projected onto the target. A target whose kernel does not report ``Linux``
is refused with ``environment_unsupported`` (the frozen environment
vocabulary is exactly ``linux_ssh``), never guessed around.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from mai.contracts.server import SshIdentity
from mai.credentials.env import EnvResolver

__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_COMMAND_TIMEOUT",
    "IDENTITY_PROBE_COMMAND",
    "MAX_COMMAND_OUTPUT_BYTES",
    "MAX_SCAN_OUTPUT_BYTES",
    "PinnedHostKey",
    "SshConnectError",
    "SshErrorCode",
    "TargetIdentity",
    "parse_identity_output",
    "parse_scan_output",
    "pin_host_key",
    "probe_environment",
    "resolve_identity_key",
    "scan_host_keys",
    "ssh_arguments",
    "target_fingerprint",
]

#: TCP connect timeout, frozen by the probe option surface
#: (``backend/tests/test_execution_dependencies.py`` PROBE_SSH_OPTIONS).
CONNECT_TIMEOUT_SECONDS = 5

#: Default wall-clock budget for one remote identity probe / keyscan.
DEFAULT_COMMAND_TIMEOUT = 30.0

#: Hard cap on captured keyscan output before it is parsed.
MAX_SCAN_OUTPUT_BYTES = 1024 * 1024

#: Hard cap on captured remote-command output (the identity probe emits a
#: few dozen bytes; anything larger is a hostile/broken target).
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024

#: Host-key fingerprint shape frozen by the canonical profile schema
#: (``mai.contracts.server`` ``_HOST_KEY_RE``). Local copy: the schema module
#: is the shape owner and outside this card's allowlist; a divergence would
#: be a review finding, not a silent import of a private symbol.
_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")

#: Host key algorithms scanned for pinning (probe pattern: ed25519 + rsa).
_SCAN_KEY_TYPES = "ed25519,rsa"


class SshErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_CONFIGURATION = "invalid_configuration"
    HOST_KEY_MISMATCH = "host_key_mismatch"
    ENVIRONMENT_UNSUPPORTED = "environment_unsupported"
    UNREACHABLE = "unreachable"
    AUTH_FAILED = "auth_failed"
    TIMEOUT = "timeout"
    BOUNDED_OUTPUT_EXCEEDED = "bounded_output_exceeded"
    SSH_FAILED = "ssh_failed"


class SshConnectError(ValueError):
    """One structured, value-free transport rejection (C01 envelope shape).

    ``detail`` carries only host:port, stable reasons and a sanitized stderr
    tail — never credential material, key contents or full command output.
    """

    def __init__(self, code: SshErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def target_fingerprint(key_type: str, blob_b64: str) -> str:
    """OpenSSH ``SHA256:`` fingerprint of one base64 key blob (pure function).

    Same computation as ``ssh-keygen -lf``: SHA-256 over the decoded key
    blob, unpadded standard base64. Fixture tests cross-check this against
    the real ``ssh-keygen`` binary.
    """
    try:
        blob = base64.b64decode(blob_b64, validate=True)
    except (ValueError, TypeError):
        raise SshConnectError(
            SshErrorCode.INVALID_CONFIGURATION,
            f"host key blob for {key_type} is not valid base64",
        ) from None
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _transport_host(host: str) -> str:
    """Normalize a profile host for ssh/ssh-keyscan (schema allows ``[v6]``).

    OpenSSH destinations and known_hosts entries use the raw literal; the
    bracketed form is a profile-schema spelling only.
    """
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _sanitize_stderr(stderr: str) -> str:
    """Bounded single-line stderr tail for error details (value-free)."""
    flat = " ".join(stderr.split())
    return flat[-200:] if flat else "<no stderr>"


def _check_output_budget(output: str, budget: int, code: SshErrorCode, label: str) -> str:
    if len(output.encode("utf-8")) > budget:
        raise SshConnectError(
            code, f"{label} output exceeded the {budget}-byte budget"
        )
    return output


# ---------------------------------------------------------------------------
# Credential reference resolution (C03 rule 6)
# ---------------------------------------------------------------------------


def resolve_identity_key(resolver: EnvResolver, credential_ref: str) -> Path:
    """Resolve ``ssh.credential_ref`` to the controller-local key file path.

    The reference resolves through the configured ``.env`` source (mandatory
    first resolver); its value is the private key file path handed to
    ``ssh -i``. The key file must exist and be a regular file — the check
    happens before any ssh invocation so a stale reference fails closed with
    ``invalid_configuration`` instead of relying on client-side auth
    failure. Key material is never read by this module.

    Raises:
        SshConnectError: ``invalid_configuration`` when the resolved value is
            not an existing regular file. :class:`mai.credentials.env.EnvFileError`
            propagates unchanged for resolver-level rejections (missing
            reference, empty value, hostile source file) — they are already
            structured and value-free.
    """
    secret = resolver.resolve(credential_ref)
    path = Path(secret.value)
    if not path.is_file():
        raise SshConnectError(
            SshErrorCode.INVALID_CONFIGURATION,
            f"credential reference {credential_ref} does not resolve to an "
            "existing key file",
        )
    return path


# ---------------------------------------------------------------------------
# ssh(1) command construction — frozen probe option surface
# ---------------------------------------------------------------------------


def ssh_arguments(
    identity: SshIdentity,
    *,
    key_path: Path,
    known_hosts_path: Path,
    remote_command: str,
) -> list[str]:
    """Build one ``ssh`` argv with exactly the frozen probe option surface.

    The five ``-o`` options are the P00-frozen surface
    (``PROBE_SSH_OPTIONS``); port and identity file are positional flags as
    in the measured probe. ``BatchMode`` prevents any prompt fallback and
    ``IdentitiesOnly=yes`` pins authentication to the profile-referenced
    key, so the client can never succeed through an ambient agent key.
    """
    return [
        "ssh",
        "-p",
        str(identity.port),
        "-i",
        str(key_path),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
        f"{identity.user}@{_transport_host(identity.host)}",
        remote_command,
    ]


# ---------------------------------------------------------------------------
# Host-key pinning (explicit enrollment path; SSH-PIN)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScannedHostKey:
    """One parsed ``ssh-keyscan`` line (host spelling preserved verbatim)."""

    marker: str
    key_type: str
    blob_b64: str

    @property
    def fingerprint(self) -> str:
        return target_fingerprint(self.key_type, self.blob_b64)

    @property
    def known_hosts_line(self) -> str:
        return f"{self.marker} {self.key_type} {self.blob_b64}"


@dataclass(frozen=True, slots=True)
class PinnedHostKey:
    """The enrolled host key: verified against the profile fingerprint."""

    host: str
    port: int
    key_type: str
    fingerprint: str


def parse_scan_output(stdout: str) -> tuple[ScannedHostKey, ...]:
    """Parse raw ``ssh-keyscan`` output (pure); banners/comments dropped.

    ``ssh-keyscan`` prints ``# host SSH-2.0-…`` banner comments on stdout —
    exactly three tokens, so a token-count check alone would misparse them
    as keys. Lines are kept only when they carry a non-comment marker and a
    valid base64 blob.
    """
    scanned: list[ScannedHostKey] = []
    for line in stdout.splitlines():
        if line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            base64.b64decode(parts[2], validate=True)
        except (ValueError, TypeError):
            continue
        scanned.append(ScannedHostKey(marker=parts[0], key_type=parts[1], blob_b64=parts[2]))
    return tuple(scanned)


def scan_host_keys(
    identity: SshIdentity, *, timeout: float = DEFAULT_COMMAND_TIMEOUT
) -> tuple[ScannedHostKey, ...]:
    """Scan the target's host keys with ``ssh-keyscan`` (no trust implied).

    Scanning is only candidate collection: nothing is trusted until a
    candidate fingerprint equals the profile-pinned fingerprint
    (:func:`pin_host_key`). Output is hard-capped and parsed strictly.
    """
    command = [
        "ssh-keyscan",
        "-p",
        str(identity.port),
        "-t",
        _SCAN_KEY_TYPES,
        "-T",
        str(int(timeout)),
        _transport_host(identity.host),
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise SshConnectError(
            SshErrorCode.TIMEOUT, f"ssh-keyscan timed out after {timeout}s"
        ) from None
    _check_output_budget(
        proc.stdout, MAX_SCAN_OUTPUT_BYTES, SshErrorCode.BOUNDED_OUTPUT_EXCEEDED, "keyscan"
    )
    scanned = parse_scan_output(proc.stdout)
    if not scanned:
        raise SshConnectError(
            SshErrorCode.UNREACHABLE,
            f"{identity.host}:{identity.port} answered keyscan with no host keys "
            f"(stderr: {_sanitize_stderr(proc.stderr)})",
        )
    return tuple(scanned)


def pin_host_key(
    identity: SshIdentity, known_hosts_path: Path
) -> PinnedHostKey:
    """Verify the scanned host key against the profile pin and record it.

    This is the explicit enrollment path (SSH-PIN): the profile fingerprint
    is the admin-accepted trust decision; here it is verified against the
    keys the target currently presents and the matching key is recorded
    (atomically, mode 0600) as the managed known-hosts file used by every
    later connection with ``StrictHostKeyChecking=yes``.

    A target presenting no key with the pinned fingerprint — changed host
    key, recreated target, wrong endpoint — is refused with
    ``host_key_mismatch`` and NOTHING is written: no silent re-trust.
    """
    if not _FINGERPRINT_RE.match(identity.host_key):
        raise SshConnectError(
            SshErrorCode.INVALID_CONFIGURATION,
            "profile host_key is not a SHA256:<43-char> OpenSSH fingerprint",
        )
    scanned = scan_host_keys(identity)
    matches = [key for key in scanned if key.fingerprint == identity.host_key]
    if not matches:
        presented = ", ".join(sorted({key.fingerprint[:20] + "…" for key in scanned}))
        raise SshConnectError(
            SshErrorCode.HOST_KEY_MISMATCH,
            f"{identity.host}:{identity.port} presents host keys {presented} "
            "matching none of the pinned fingerprint; refuse without re-trust "
            "(re-enrollment is an explicit admin action)",
        )
    pinned = matches[0]
    entries = f"{pinned.known_hosts_line}\n"
    # Atomic replace in the same directory; the file holds public key
    # material only, 0600 keeps even that confined to the service user.
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = known_hosts_path.with_name(f"{known_hosts_path.name}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(entries)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    os.replace(tmp_path, known_hosts_path)
    return PinnedHostKey(
        host=identity.host,
        port=identity.port,
        key_type=pinned.key_type,
        fingerprint=pinned.fingerprint,
    )


# ---------------------------------------------------------------------------
# Target identity / environment detection (acceptance 2)
# ---------------------------------------------------------------------------


#: The single remote command this module executes: fixed, bounded, read-only.
#: Every field of :class:`TargetIdentity` derives from its output alone; the
#: raw ``/etc/os-release`` values are printed verbatim and unquoted by the
#: parser below (POSIX sh only, so busybox targets parse the same).
IDENTITY_PROBE_COMMAND = (
    "printf 'kernel_name=%s\\n' \"$(uname -s)\"; "
    "printf 'kernel_release=%s\\n' \"$(uname -r)\"; "
    "printf 'machine=%s\\n' \"$(uname -m)\"; "
    "printf 'hostname=%s\\n' \"$(uname -n)\"; "
    "printf 'boot_id=%s\\n' "
    "\"$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)\"; "
    "sed -n "
    "'s/^PRETTY_NAME=/os_pretty_name=/p;"
    "s/^VERSION_ID=/os_version_id=/p;"
    "s/^ID=/os_id=/p' /etc/os-release 2>/dev/null"
)


def _unquote(value: str) -> str:
    """Strip one pair of surrounding double/single quotes (os-release form)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


@dataclass(frozen=True, slots=True)
class TargetIdentity:
    """Observed target identity — reported facts only, no local inference.

    Optional fields are ``None`` when the target genuinely lacks the source
    (e.g. a rolling distribution without ``VERSION_ID``). Nothing here is
    inferred from the controller platform (container/Windows/WSL or
    otherwise), and no availability heuristic is applied: absence is a
    reported ``None``, never a local assumption.
    """

    hostname: str
    kernel_name: str
    kernel_release: str
    machine: str
    os_id: str | None
    os_version_id: str | None
    os_pretty_name: str | None
    boot_id: str | None


_UNAME_FIELDS = ("kernel_name", "kernel_release", "machine", "hostname")
_OS_FIELDS = ("os_pretty_name", "os_version_id", "os_id")


def parse_identity_output(output: str) -> TargetIdentity:
    """Parse identity-probe output into a :class:`TargetIdentity`.

    Order-sensitive prefix parsing: ``ID=`` is checked only after
    ``os_version_id``/``os_pretty_name`` so ``VERSION_ID=`` never leaks into
    ``os_id``. Surrounding quotes written by ``/etc/os-release`` are
    stripped. Malformed required fields (empty, whitespace-bearing,
    multi-line) are a structured ``invalid_output`` rejection, never a
    guess.
    """
    values: dict[str, str | None] = {name: None for name in (*_UNAME_FIELDS, *_OS_FIELDS)}
    values["boot_id"] = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key == "boot_id":
            values["boot_id"] = value.strip() or None
            continue
        if key in values and values[key] is None:
            values[key] = _unquote(value.strip()) if key in _OS_FIELDS else value.strip()
    required = {name: values[name] for name in _UNAME_FIELDS}
    missing = sorted(name for name, value in required.items() if not value)
    if missing:
        raise SshConnectError(
            SshErrorCode.SSH_FAILED,
            f"identity probe output missing required fields: {', '.join(missing)}",
        )
    for name, value in required.items():
        if len(value.split()) != 1:  # type: ignore[union-attr]
            raise SshConnectError(
                SshErrorCode.SSH_FAILED,
                f"identity probe field {name} is not a single token",
            )
    kernel_name = required["kernel_name"]
    assert kernel_name is not None
    if kernel_name != "Linux":
        raise SshConnectError(
            SshErrorCode.ENVIRONMENT_UNSUPPORTED,
            f"target kernel reports {kernel_name!r}; the frozen environment "
            "vocabulary is linux_ssh only — refusing instead of guessing",
        )
    return TargetIdentity(
        hostname=required["hostname"],  # type: ignore[arg-type]
        kernel_name=kernel_name,
        kernel_release=required["kernel_release"],  # type: ignore[arg-type]
        machine=required["machine"],  # type: ignore[arg-type]
        os_id=values["os_id"],
        os_version_id=values["os_version_id"],
        os_pretty_name=values["os_pretty_name"],
        boot_id=values["boot_id"],
    )


def _run_remote(
    command: list[str],
    *,
    timeout: float,
    budget: int,
    label: str,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise SshConnectError(
            SshErrorCode.TIMEOUT, f"{label} timed out after {timeout}s"
        ) from None
    _check_output_budget(
        proc.stdout, budget, SshErrorCode.BOUNDED_OUTPUT_EXCEEDED, label
    )
    return proc


def _map_ssh_failure(rc: int, stderr: str, endpoint: str) -> SshConnectError:
    """Map a non-zero ssh exit to the stable rejection vocabulary."""
    flat = " ".join(stderr.split())
    if "Host key verification failed" in flat or "HOST IDENTIFICATION HAS CHANGED" in flat:
        return SshConnectError(
            SshErrorCode.HOST_KEY_MISMATCH,
            f"{endpoint} refused by host key pinning "
            f"(stderr: {_sanitize_stderr(stderr)}); re-enrollment is an "
            "explicit admin action, never automatic",
        )
    if "Permission denied" in flat or "bad permissions" in flat.lower():
        return SshConnectError(
            SshErrorCode.AUTH_FAILED,
            f"{endpoint} rejected the pinned identity key "
            f"(stderr: {_sanitize_stderr(stderr)})",
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
            f"{endpoint} unreachable (stderr: {_sanitize_stderr(stderr)})",
        )
    return SshConnectError(
        SshErrorCode.SSH_FAILED,
        f"{endpoint} ssh exited rc={rc} (stderr: {_sanitize_stderr(stderr)})",
    )


def probe_environment(
    identity: SshIdentity,
    *,
    key_path: Path,
    known_hosts_path: Path,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> TargetIdentity:
    """Connect with the pinned identity and return the target's identity.

    Preconditions: the key path comes from :func:`resolve_identity_key` and
    the managed known-hosts file from :func:`pin_host_key` (enrollment). The
    connection uses ``StrictHostKeyChecking=yes``, so a target presenting a
    key other than the enrolled one is refused before any command runs —
    the probe command only ever reaches a fingerprint-verified endpoint.
    """
    if not known_hosts_path.is_file():
        raise SshConnectError(
            SshErrorCode.INVALID_CONFIGURATION,
            f"managed known-hosts file {known_hosts_path} does not exist; "
            "enroll the pinned host key explicitly before connecting",
        )
    command = ssh_arguments(
        identity,
        key_path=key_path,
        known_hosts_path=known_hosts_path,
        remote_command=IDENTITY_PROBE_COMMAND,
    )
    proc = _run_remote(
        command,
        timeout=timeout,
        budget=MAX_COMMAND_OUTPUT_BYTES,
        label="identity probe",
    )
    if proc.returncode != 0:
        raise _map_ssh_failure(
            proc.returncode, proc.stderr, f"{identity.host}:{identity.port}"
        )
    return parse_identity_output(proc.stdout)
