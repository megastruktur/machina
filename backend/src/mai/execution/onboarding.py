"""Guided-onboarding building blocks: safe controller public-key export and
the side-effect-free, explicitly pinned SSH discovery/test (campaign
``server-onboarding``, task 01; frozen contract ``plans/so_PLAN.md``,
sections "Controller public key" and "Side-effect-free connection test").

Ownership map:

- **Default credential** — the controller's own SSH identity is server-side
  configuration: ``MAI_SSH_CREDENTIAL_REF`` when configured, else the
  installer-compatible ``MAI_SSH_KEY_MAI`` fallback
  (:func:`default_credential_ref`). The reference is a NAME resolved through
  the existing layered :class:`~mai.credentials.env.EnvResolver`
  (:func:`~mai.execution.ssh.resolve_identity_key`); no key path or value is
  ever reported to the browser.
- **Public-key export** — :func:`export_public_key` runs the bounded
  ``ssh-keygen -y -f`` derivation (10 s / 8 KiB budget) and accepts exactly
  one validated OpenSSH public-key line. The response carries only the
  public half: key type, blob and SHA256 fingerprint — never the private
  key, the resolver path, an env value or a PEM body.
- **Discovery** — :func:`discover_host_key_candidates` is candidate
  collection through the existing :func:`~mai.execution.ssh.scan_host_keys`:
  it makes NO authentication claim and writes NO durable state anywhere.
- **Confirmed test** — :func:`run_connection_test` re-scans (bounded by the
  caller's timeout) and requires the exact admin-selected fingerprint,
  mirroring :func:`~mai.execution.ssh.pin_host_key` semantics (no match ⇒
  nothing written), but writes the accepted key alone to a NEWLY created
  temporary known-hosts file (never an existing server's store). The SSH
  connection then uses the frozen probe surface
  (:func:`~mai.execution.ssh.ssh_arguments`: ``IdentitiesOnly=yes``,
  ``BatchMode=yes``, ``StrictHostKeyChecking=yes``) with the pinned default
  identity key, runs the fixed bounded identity probe
  (:func:`~mai.execution.ssh.probe_environment`) and the requested bounded
  privilege check (fixed noninteractive ``sudo -n true`` for sudo, actual
  remote ``id -u`` uid 0 for root, nothing for none). The temporary file is
  removed on success and failure; no durable state exists on any path.

There is no enrollment here: durable pin/profile writes belong to task 02.
Blocking subprocess work in this module is synchronous by design — the HTTP
layer owns dispatching it off the event loop (``asyncio.to_thread``).
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from mai.contracts.server import PrivilegeMode, SshIdentity
from mai.credentials.env import EnvResolver
from mai.execution.ssh import (
    DEFAULT_COMMAND_TIMEOUT,
    MAX_COMMAND_OUTPUT_BYTES,
    ScannedHostKey,
    SshConnectError,
    TargetIdentity,
    probe_environment,
    resolve_identity_key,
    scan_host_keys,
    ssh_arguments,
    target_fingerprint,
)

__all__ = [
    "CONTROLLER_KEY_TIMEOUT_SECONDS",
    "DEFAULT_CREDENTIAL_REF_VAR",
    "FALLBACK_CREDENTIAL_REF",
    "MAX_PUBLIC_KEY_EXPORT_BYTES",
    "ConnectionTestResult",
    "ExportedPublicKey",
    "OnboardingError",
    "OnboardingErrorCode",
    "default_credential_ref",
    "discover_host_key_candidates",
    "export_public_key",
    "run_connection_test",
]

#: Server-side override name for the default SSH credential reference
#: (so_PLAN: the setting is documented server configuration, never a
#: browser input).
DEFAULT_CREDENTIAL_REF_VAR = "MAI_SSH_CREDENTIAL_REF"
#: Installer-compatible fallback reference when the override is unset.
FALLBACK_CREDENTIAL_REF = "MAI_SSH_KEY_MAI"

#: Wall-clock budget for one ``ssh-keygen -y`` export (so_PLAN: bounded to
#: 10 seconds).
CONTROLLER_KEY_TIMEOUT_SECONDS = 10.0
#: Hard cap on captured ``ssh-keygen`` stdout before it is parsed.
MAX_PUBLIC_KEY_EXPORT_BYTES = 8 * 1024

#: The two fixed, bounded, read-only privilege probes. Each is exactly one
#: ssh argv element behind the frozen probe option surface; nothing else
#: ever reaches a target from this module.
SUDO_CHECK_COMMAND = "sudo -n true"
UID_CHECK_COMMAND = "id -u"

#: Host-key fingerprint shape frozen by the canonical profile schema; local
#: copy for pre-validation so a malformed request fingerprint fails with
#: ``invalid_configuration`` before any subprocess runs (the shape owner
#: ``mai.contracts.server`` is outside this task's allowlist).
_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")

#: Public-key algorithms accepted for the exported controller key and the
#: discovered/confirmed host keys (the OpenSSH public-key families this
#: controller understands; anything else is ``key_export_failed``).
_OPENSSH_PUBLIC_KEY_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
    }
)


class OnboardingErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary).

    The transport values mirror :class:`~mai.execution.ssh.SshErrorCode`
    (this module is a thin orchestration over it); ``privilege_unavailable``
    and ``key_export_failed`` are onboarding-specific.
    """

    INVALID_CONFIGURATION = "invalid_configuration"
    HOST_KEY_MISMATCH = "host_key_mismatch"
    ENVIRONMENT_UNSUPPORTED = "environment_unsupported"
    UNREACHABLE = "unreachable"
    AUTH_FAILED = "auth_failed"
    TIMEOUT = "timeout"
    BOUNDED_OUTPUT_EXCEEDED = "bounded_output_exceeded"
    SSH_FAILED = "ssh_failed"
    PRIVILEGE_UNAVAILABLE = "privilege_unavailable"
    KEY_EXPORT_FAILED = "key_export_failed"


class OnboardingError(ValueError):
    """One structured, value-free guided-onboarding rejection.

    ``detail`` carries only host:port, stable reasons and (for transport
    errors raised by the reused :mod:`mai.execution.ssh` helpers) the
    established sanitized single-line stderr tail — never credential
    material, key contents or ``ssh-keygen`` stderr, which can embed local
    key paths and is therefore dropped entirely for exports.
    """

    def __init__(self, code: OnboardingErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def _wrap_transport(exc: SshConnectError) -> OnboardingError:
    """Map one reused-transport rejection onto the onboarding vocabulary."""
    return OnboardingError(OnboardingErrorCode(exc.code.value), exc.detail)


@dataclass(frozen=True, slots=True)
class ExportedPublicKey:
    """The controller public half, safe for the browser (no private side)."""

    public_key: str
    key_type: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    """One successful pinned test: observed facts only, nothing inferred."""

    host_key: str
    key_type: str
    privilege: PrivilegeMode
    #: The real target-reported Linux identity (hostname, kernel_name /
    #: kernel_release, machine, os_id / os_version_id / os_pretty_name,
    #: boot_id) — nothing derived from the controller platform.
    identity: TargetIdentity


# ---------------------------------------------------------------------------
# Default credential reference (server-side configuration)
# ---------------------------------------------------------------------------


def default_credential_ref(env: Mapping[str, str] | None = None) -> str:
    """The configured default SSH credential reference name.

    ``MAI_SSH_CREDENTIAL_REF`` wins when set (non-blank); otherwise the
    installer-compatible ``MAI_SSH_KEY_MAI`` fallback applies. The value is
    a reference NAME only — it still has to resolve through the configured
    :class:`~mai.credentials.env.EnvResolver` at use time.
    """
    source = os.environ if env is None else env
    configured = (source.get(DEFAULT_CREDENTIAL_REF_VAR) or "").strip()
    return configured or FALLBACK_CREDENTIAL_REF


def resolve_default_identity_key(
    resolver: EnvResolver, env: Mapping[str, str] | None = None
) -> Path:
    """Resolve the default credential reference to the key file path.

    Thin composition of :func:`default_credential_ref` and the existing
    :func:`~mai.execution.ssh.resolve_identity_key` (fail-closed: the key
    file must exist and be a regular file). Rejections are
    :class:`OnboardingError` (``invalid_configuration``) or the structured
    :class:`~mai.credentials.env.EnvFileError` of the resolver itself.
    """
    try:
        return resolve_identity_key(resolver, default_credential_ref(env))
    except SshConnectError as exc:
        raise _wrap_transport(exc) from None


# ---------------------------------------------------------------------------
# Controller public-key export (bounded ssh-keygen -y)
# ---------------------------------------------------------------------------


def export_public_key(key_path: Path) -> ExportedPublicKey:
    """Derive the public half of the controller identity key.

    Exactly one bounded ``ssh-keygen -y -f`` invocation; the output must be
    one validated OpenSSH public-key line (supported type + base64 blob).
    Every rejection is value-free: ``ssh-keygen`` stderr can embed local key
    paths and is never forwarded.
    """
    try:
        proc = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(key_path)],
            capture_output=True,
            text=True,
            timeout=CONTROLLER_KEY_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise OnboardingError(
            OnboardingErrorCode.TIMEOUT,
            f"ssh-keygen export exceeded the {CONTROLLER_KEY_TIMEOUT_SECONDS:.0f}s budget",
        ) from None
    except OSError as exc:
        raise OnboardingError(
            OnboardingErrorCode.INVALID_CONFIGURATION,
            f"ssh-keygen could not be executed: {exc.strerror}",
        ) from None
    if proc.returncode != 0:
        raise OnboardingError(
            OnboardingErrorCode.KEY_EXPORT_FAILED,
            "the configured controller key could not be exported "
            "(missing, unreadable, wrong permissions or not a supported "
            "private key format)",
        )
    _check_export_budget(proc.stdout)
    return _parse_public_key_line(proc.stdout)


def _check_export_budget(output: str) -> None:
    if len(output.encode("utf-8")) > MAX_PUBLIC_KEY_EXPORT_BYTES:
        raise OnboardingError(
            OnboardingErrorCode.BOUNDED_OUTPUT_EXCEEDED,
            "ssh-keygen output exceeded the "
            f"{MAX_PUBLIC_KEY_EXPORT_BYTES}-byte budget",
        )


def _parse_public_key_line(stdout: str) -> ExportedPublicKey:
    """Validate exactly one OpenSSH public-key line: type, blob, [comment].

    ``ssh-keygen -y`` appends the private key's embedded comment when one
    exists, so a single trailing comment token is part of the line; the
    exported text stays exactly the validated line (safe for
    ``authorized_keys``).
    """
    text = stdout.strip()
    if not text or "\n" in text or "\r" in text:
        raise OnboardingError(
            OnboardingErrorCode.KEY_EXPORT_FAILED,
            "key export did not produce exactly one public-key line",
        )
    parts = text.split()
    if len(parts) not in (2, 3):
        raise OnboardingError(
            OnboardingErrorCode.KEY_EXPORT_FAILED,
            "key export is not an OpenSSH public-key line "
            "(type, blob and at most one comment token)",
        )
    key_type, blob_b64 = parts[0], parts[1]
    if key_type not in _OPENSSH_PUBLIC_KEY_TYPES:
        raise OnboardingError(
            OnboardingErrorCode.KEY_EXPORT_FAILED,
            f"exported key type {key_type} is not supported",
        )
    try:
        base64.b64decode(blob_b64, validate=True)
    except (ValueError, TypeError):
        raise OnboardingError(
            OnboardingErrorCode.KEY_EXPORT_FAILED,
            "exported key blob is not valid base64",
        ) from None
    return ExportedPublicKey(
        public_key=text,
        key_type=key_type,
        fingerprint=target_fingerprint(key_type, blob_b64),
    )


# ---------------------------------------------------------------------------
# Discovery (candidate collection only; no authentication claim)
# ---------------------------------------------------------------------------


def discover_host_key_candidates(
    identity: SshIdentity, *, timeout: float = DEFAULT_COMMAND_TIMEOUT
) -> tuple[ScannedHostKey, ...]:
    """Collect the target's host-key candidates (no trust, no writes).

    Pure reuse of :func:`~mai.execution.ssh.scan_host_keys`: a bounded
    ``ssh-keyscan`` whose output is capped and parsed strictly. Nothing is
    authenticated and nothing durable exists on any path (success, failure
    or timeout).
    """
    try:
        return scan_host_keys(identity, timeout=timeout)
    except SshConnectError as exc:
        raise _wrap_transport(exc) from None


# ---------------------------------------------------------------------------
# Confirmed test (pinned temporary known-hosts; real identity + privilege)
# ---------------------------------------------------------------------------


def run_connection_test(
    identity: SshIdentity,
    *,
    privilege: PrivilegeMode,
    key_path: Path,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
    tmp_dir: str | Path | None = None,
) -> ConnectionTestResult:
    """Test the target against the admin-selected fingerprint (side-effect-free).

    Sequence, through the existing strict helpers:

    1. Bounded rescan (:func:`~mai.execution.ssh.scan_host_keys` with the
       caller's ``timeout``) and exact-fingerprint require: the target must
       currently present a key whose fingerprint equals ``identity.host_key``.
       The matching key alone is written into a NEWLY created temporary
       known-hosts file (mkstemp, 0600, in ``tmp_dir`` or the system
       temporary area — never an existing server's store), mirroring
       :func:`~mai.execution.ssh.pin_host_key` semantics: no match ⇒
       ``host_key_mismatch`` and NOTHING is written; no ``accept-new``, no
       silent trust.
    2. :func:`~mai.execution.ssh.probe_environment`: a real SSH connection
       with ``IdentitiesOnly=yes``/``BatchMode=yes``/
       ``StrictHostKeyChecking=yes`` and the pinned default identity key;
       the fixed bounded identity probe reports the actual Linux identity.
    3. The requested privilege check: ``none`` runs nothing, ``sudo`` runs
       the fixed noninteractive ``sudo -n true`` (password-required or
       absent sudo is a structured ``privilege_unavailable``), ``root``
       requires the actual remote ``id -u`` to report uid 0.

    The descriptor returned by ``mkstemp`` is closed immediately (the path
    is the only contract; the connection reads the file by name). The
    temporary file is removed on success and failure; no durable state is
    written on any path. Enrollment persistence (durable pin + canonical
    profile) is task 02, not this function.
    """
    if not identity.host_key or not _FINGERPRINT_RE.match(identity.host_key):
        raise OnboardingError(
            OnboardingErrorCode.INVALID_CONFIGURATION,
            "host_key is required for the confirmed test and must be a "
            "SHA256:<43-char> OpenSSH fingerprint",
        )
    fd, name = tempfile.mkstemp(prefix="mai-onboarding-known-hosts-", dir=tmp_dir)
    os.close(fd)  # never leaked: the file is used by path, not descriptor
    known_hosts_path = Path(name)
    try:
        try:
            scanned = scan_host_keys(identity, timeout=timeout)
            matches = [
                key for key in scanned if key.fingerprint == identity.host_key
            ]
            if not matches:
                presented = ", ".join(
                    sorted({key.fingerprint[:20] + "…" for key in scanned})
                )
                raise OnboardingError(
                    OnboardingErrorCode.HOST_KEY_MISMATCH,
                    f"{identity.host}:{identity.port} presents host keys "
                    f"{presented} matching none of the accepted fingerprint; "
                    "refuse without re-trust (enrollment is an explicit "
                    "admin action)",
                )
            accepted = matches[0]
            # The mkstemp file already exists, mode 0600; truncate-write the
            # single accepted line (public material only).
            known_hosts_path.write_text(
                accepted.known_hosts_line + "\n", encoding="utf-8"
            )
            observed = probe_environment(
                identity,
                key_path=key_path,
                known_hosts_path=known_hosts_path,
                timeout=timeout,
            )
            _check_privilege(
                identity,
                privilege,
                key_path=key_path,
                known_hosts_path=known_hosts_path,
                timeout=timeout,
            )
        except SshConnectError as exc:
            raise _wrap_transport(exc) from None
    finally:
        known_hosts_path.unlink(missing_ok=True)
    return ConnectionTestResult(
        host_key=accepted.fingerprint,
        key_type=accepted.key_type,
        privilege=privilege,
        identity=observed,
    )


def _check_privilege(
    identity: SshIdentity,
    privilege: PrivilegeMode,
    *,
    key_path: Path,
    known_hosts_path: Path,
    timeout: float,
) -> None:
    """One fixed bounded probe for the requested privilege mode."""
    if privilege is PrivilegeMode.NONE:
        return
    if privilege is PrivilegeMode.SUDO:
        proc = _run_bounded(
            ssh_arguments(
                identity,
                key_path=key_path,
                known_hosts_path=known_hosts_path,
                remote_command=SUDO_CHECK_COMMAND,
            ),
            timeout=timeout,
            label="sudo check",
        )
        if proc.returncode == 0:
            return
        if proc.returncode == 255:
            raise _map_remote_failure(
                proc.stderr, f"{identity.host}:{identity.port}", "sudo check"
            )
        raise OnboardingError(
            OnboardingErrorCode.PRIVILEGE_UNAVAILABLE,
            f"{identity.host}:{identity.port} cannot use noninteractive "
            "sudo (password required or sudo absent); privilege_unavailable",
        )
    # privilege is PrivilegeMode.ROOT: actual remote uid must be 0.
    proc = _run_bounded(
        ssh_arguments(
            identity,
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            remote_command=UID_CHECK_COMMAND,
        ),
        timeout=timeout,
        label="uid check",
    )
    if proc.returncode == 255:
        raise _map_remote_failure(
            proc.stderr, f"{identity.host}:{identity.port}", "uid check"
        )
    if proc.returncode != 0:
        raise OnboardingError(
            OnboardingErrorCode.SSH_FAILED,
            f"{identity.host}:{identity.port} uid check exited "
            f"rc={proc.returncode}",
        )
    if proc.stdout.strip() != "0":
        raise OnboardingError(
            OnboardingErrorCode.PRIVILEGE_UNAVAILABLE,
            f"{identity.host}:{identity.port} remote account is not uid 0; "
            "root privilege mode requires the actual root identity",
        )


def _run_bounded(
    command: list[str], *, timeout: float, label: str
) -> subprocess.CompletedProcess[str]:
    """Run one fixed ssh command with bounded time and output."""
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
        raise OnboardingError(
            OnboardingErrorCode.TIMEOUT, f"{label} timed out after {timeout}s"
        ) from None
    if len(proc.stdout.encode("utf-8")) > MAX_COMMAND_OUTPUT_BYTES:
        raise OnboardingError(
            OnboardingErrorCode.BOUNDED_OUTPUT_EXCEEDED,
            f"{label} output exceeded the "
            f"{MAX_COMMAND_OUTPUT_BYTES}-byte budget",
        )
    return proc


def _map_remote_failure(stderr: str, endpoint: str, label: str) -> OnboardingError:
    """Map an ssh-transport (rc=255) stderr onto the stable vocabulary.

    Same marker classes as the existing transport mapper: host-key refusal,
    authentication rejection, reachability; anything else is ``ssh_failed``.
    """
    flat = " ".join(stderr.split())
    if "Host key verification failed" in flat or "HOST IDENTIFICATION HAS CHANGED" in flat:
        return OnboardingError(
            OnboardingErrorCode.HOST_KEY_MISMATCH,
            f"{endpoint} refused by host key pinning during {label}; the "
            "presented key is not the accepted fingerprint",
        )
    if "Permission denied" in flat or "bad permissions" in flat.lower():
        return OnboardingError(
            OnboardingErrorCode.AUTH_FAILED,
            f"{endpoint} rejected the pinned identity key during {label}",
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
        return OnboardingError(
            OnboardingErrorCode.UNREACHABLE,
            f"{endpoint} unreachable during {label}",
        )
    return OnboardingError(
        OnboardingErrorCode.SSH_FAILED,
        f"{endpoint} {label} failed at the transport level",
    )
