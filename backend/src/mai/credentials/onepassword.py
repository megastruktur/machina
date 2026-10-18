"""Optional 1Password credential provider — the ``op://`` CLI resolver plugin.

Contracts.md **C03 rule 6**: ``.env`` is the mandatory first resolver and
1Password is an optional *later* plugin; names resolve only from the
configured source and host-wide secret scanning is prohibited. IDEA.md §14:
"1Password is an optional plugin, behind a provider interface that allows
other secret stores later." This module is that optional provider. It is
**not imported by any core module** — a host without it keeps the plain
``.env`` deployment fully functional (the absence surfaces only at this
module's own discovery boundary, never as a core import failure).

**Supported interface** (the documented 1Password CLI, never a bundled
distribution — the same selection rule as the system OpenSSH client):
``op read op://<vault>/<item>/<field>`` authenticates non-interactively
through the ``OP_SERVICE_ACCOUNT_TOKEN`` environment variable; service
accounts require CLI >= 2.18.0 (developer.1password.com, "Use service
accounts with 1Password CLI"). The ``onepassword-sdk`` distribution is NOT
selected (11-dependencies freeze): its wheels stop at CPython 3.13 and the
CLI it wraps is the documented interface anyway.

**Fail-closed availability.** Construction never probes: a resolver object
exists on every host. :meth:`OnePasswordResolver.availability` classifies
``op`` absence, a below-floor CLI version and a missing service-account
token as structured, value-free *unavailable* outcomes; :meth:`resolve`
raises the same classification. There is no fallback secret scan: a failed
``op`` lookup never reads ``.env``, never touches ambient environment
variables beyond the explicitly configured token lane, and never retries
another source.

**Token boundary.** The service-account token is configured by exactly one
lane: a ``credential_ref`` name resolved through the configured
:class:`mai.credentials.env.EnvResolver` (the mandatory first resolver), or
a single confined process-environment variable read **by explicit name**
(default ``OP_SERVICE_ACCOUNT_TOKEN`` — one named lookup, never a scan).
The token lives only in a redacted :class:`~mai.credentials.env.SecretValue`
and is passed to the ``op`` child **only** through its scrubbed environment
key ``OP_SERVICE_ACCOUNT_TOKEN`` (the documented auth variable) — never in
argv, never logged (the module performs no logging at all), never persisted
(the module writes nothing). Collected child ``stderr`` is scrubbed of the
token before any error detail is built.

**Bounded execution.** Every ``op`` invocation is a fresh ``argv``-list
subprocess (``shell=False``, ``stdin=DEVNULL`` so the CLI can never prompt
and hang) with a per-call timeout and output budgets: accepted ``stdout`` is
capped and over-budget output fails closed (``output_too_large`` — a
silently truncated credential would be a correctness landmine), ``stderr``
contributes only a bounded, token-scrubbed head to error details. The child
environment is scrubbed to exactly ``PATH``, ``HOME`` and (for ``read``) the
token key — no other ambient variable reaches the CLI. Residual limit,
owned honestly: between spawn and the timeout, a hostile ``op`` binary
could write unbounded bytes to the temp capture files; ``op`` is
EULA-licensed host software outside the threat model, and the budget
governs what the resolver *accepts*, which is the fail-closed boundary this
module owns.

``op://`` references validate against a documented strict subset of the
upstream grammar — ``op://<vault>/<item>/<field>``, three non-empty
segments of ``[A-Za-z0-9][A-Za-z0-9._-]{0,127}``, full-match, ≤ 400 chars.
Stricter than upstream (no spaces, no percent-encoding): a real-world name
outside the subset is a configuration rename, never a security incident.
The first-character rule makes an argv option injection (``-``) and
``.``/``..`` segments unrepresentable. A validated reference is
configuration, not secret material, and may appear in error details.

The resolved value is returned as ``SecretValue`` — ``repr``/``str``
render ``SecretValue(<redacted>)`` and the raw material exists only behind
the deliberate ``.value`` disclosure, exactly like ``.env`` resolution.
The field bytes are passed through unchanged (no newline stripping).

Thread safety: configuration is immutable after construction and every
call probes and executes fresh, so concurrent ``resolve`` calls are safe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from mai.credentials.env import (
    EnvErrorCode,
    EnvFileError,
    EnvResolver,
    SecretValue,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TOKEN_ENV",
    "MAX_OUTPUT_BYTES",
    "MAX_REFERENCE_CHARS",
    "MAX_STDERR_TAIL_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "OP_SERVICE_ACCOUNT_FLOOR",
    "OnePasswordError",
    "OnePasswordErrorCode",
    "OnePasswordResolver",
    "ProviderStatus",
    "main",
]

#: Service-account capability floor frozen by the 11-dependencies slice
#: (pyproject provenance: developer.1password.com, "Use service accounts
#: with 1Password CLI"). A lower CLI version is *unavailable*, not an error
#: to work around.
OP_SERVICE_ACCOUNT_FLOOR = (2, 18, 0)

#: Default binary name and confined token variable (documented interface).
DEFAULT_OP_BINARY = "op"
DEFAULT_TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"

#: Per-call child timeout bounds. A resolution is one bounded CLI call, not
#: an unattended sync; 10 s covers cold vault locks on local hosts.
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 120.0

#: Accepted ``stdout`` budget. Item fields are small; over-budget output
#: fails closed (never silently truncated credential material).
MAX_OUTPUT_BYTES = 256 * 1024

#: ``stderr`` head kept for error details (token-scrubbed, value-free of
#: secrets by construction).
MAX_STDERR_TAIL_BYTES = 2048

#: Total ``op://`` reference length cap (three 128-char segments plus
#: separators fit well below it; the cap gives a clear dedicated error).
MAX_REFERENCE_CHARS = 400

#: ``op://`` reference shape: documented strict subset (module docstring).
#: Full-match; the first-character rule makes option injection and
#: ``.``/``..`` segments unrepresentable.
_OP_REFERENCE_RE = re.compile(
    r"op://[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"/[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"/[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
)

#: Token-lane shapes. Kept local with the same review note as every other
#: consumer: the shapes are frozen by ``mai.contracts.server`` (credential
#: reference) and the ``.env`` grammar (environment key); the schema module
#: is the shape owner and outside this card's allowlist — a divergence
#: would be a review finding, not a silent import of a private symbol.
_TOKEN_REF_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_TOKEN_ENV_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: The one child-environment key carrying the token (documented op auth
#: variable), independent of which parent lane supplied the value.
_CHILD_TOKEN_KEY = "OP_SERVICE_ACCOUNT_TOKEN"


class OnePasswordErrorCode(StrEnum):
    """Stable machine-readable classification codes (C01 envelope vocabulary).

    ``AVAILABLE`` is the non-error terminal state of
    :meth:`OnePasswordResolver.availability`; every other code is a
    structured, value-free unavailability/rejection. Details carry only
    configuration names, reference shapes, exit statuses and scrubbed CLI
    stderr heads — never a token and never resolved material.
    """

    AVAILABLE = "available"
    INVALID_CONFIGURATION = "invalid_configuration"
    OP_NOT_FOUND = "op_not_found"
    OP_UNSUPPORTED_VERSION = "op_unsupported_version"
    TOKEN_MISSING = "token_missing"
    INVALID_REFERENCE = "invalid_reference"
    OP_FAILED = "op_failed"
    OP_TIMEOUT = "op_timeout"
    OUTPUT_TOO_LARGE = "output_too_large"
    INVALID_ENCODING = "invalid_encoding"
    SPAWN_FAILED = "spawn_failed"


class OnePasswordError(ValueError):
    """One structured, value-free provider rejection.

    ``detail`` never embeds a token or resolved material; hostile child
    ``stderr`` passes through the token scrub before reaching a message.
    """

    def __init__(self, code: OnePasswordErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """One availability classification (value-free).

    ``binary`` is the located executable path (configuration, not secret
    material) and is set only for internal execution after an
    ``available`` classification.
    """

    code: OnePasswordErrorCode
    detail: str
    binary: str | None = None

    @property
    def available(self) -> bool:
        return self.code is OnePasswordErrorCode.AVAILABLE


@dataclass(frozen=True, slots=True)
class _Completed:
    """Bounded capture of one finished child process."""

    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_overlarge: bool


class OnePasswordResolver:
    """Resolve ``op://`` references through the 1Password CLI (C03 rule 6).

    Construction validates configuration only — it never probes for the
    binary or token, so the plugin object exists (and imports) on hosts
    without 1Password. Exactly one token lane must be configured:

    - ``credential_ref`` + ``resolver``: the token is resolved through the
      configured ``.env`` source (mandatory first resolver lane);
    - ``token_env`` (default ``OP_SERVICE_ACCOUNT_TOKEN``): the token is
      read from that one confined process-environment variable by explicit
      name.

    Configuring both lanes, a ``credential_ref`` without its resolver, an
    empty lane name, or an out-of-bounds timeout raises
    ``invalid_configuration`` immediately.
    """

    def __init__(
        self,
        *,
        op_binary: str = DEFAULT_OP_BINARY,
        credential_ref: str | None = None,
        resolver: EnvResolver | None = None,
        token_env: str | None = DEFAULT_TOKEN_ENV,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(op_binary, str) or not op_binary.strip():
            raise OnePasswordError(
                OnePasswordErrorCode.INVALID_CONFIGURATION,
                "op_binary must be a non-empty command name or path",
            )
        self._op_binary = op_binary
        if credential_ref is not None:
            if not isinstance(credential_ref, str) or not _TOKEN_REF_RE.fullmatch(
                credential_ref
            ):
                raise OnePasswordError(
                    OnePasswordErrorCode.INVALID_CONFIGURATION,
                    "credential_ref must match [A-Z][A-Z0-9_]{0,127} "
                    "(the canonical credential-reference shape)",
                )
            if not isinstance(resolver, EnvResolver):
                raise OnePasswordError(
                    OnePasswordErrorCode.INVALID_CONFIGURATION,
                    "credential_ref requires the configured .env EnvResolver "
                    "instance (C03 rule 6: the first resolver owns the source)",
                )
            if token_env is not None:
                raise OnePasswordError(
                    OnePasswordErrorCode.INVALID_CONFIGURATION,
                    "configure exactly one token lane: credential_ref (.env) "
                    "or token_env (confined environment variable)",
                )
            self._credential_ref: str | None = credential_ref
            self._resolver: EnvResolver | None = resolver
            self._token_env: str | None = None
        else:
            if not isinstance(token_env, str) or not _TOKEN_ENV_RE.fullmatch(token_env):
                raise OnePasswordError(
                    OnePasswordErrorCode.INVALID_CONFIGURATION,
                    "token_env must be a valid environment-variable name "
                    "([A-Za-z_][A-Za-z0-9_]*); it is read by explicit name, "
                    "never scanned",
                )
            self._credential_ref = None
            self._resolver = None
            self._token_env = token_env
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= MAX_TIMEOUT_SECONDS
        ):
            raise OnePasswordError(
                OnePasswordErrorCode.INVALID_CONFIGURATION,
                f"timeout must be within (0, {MAX_TIMEOUT_SECONDS}] seconds",
            )
        self._timeout = float(timeout)

    def __repr__(self) -> str:
        lane = (
            f"credential_ref:{self._credential_ref}"
            if self._credential_ref is not None
            else f"env:{self._token_env}"
        )
        return (
            f"<OnePasswordResolver op={self._op_binary!r} token_lane={lane} "
            f"timeout={self._timeout}>"
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def availability(self) -> ProviderStatus:
        """Classify the provider's usability on this host, fail-closed.

        Fresh probe on every call (no cache, no clock state): locates the
        binary, holds it to the service-account version floor, then checks
        the configured token lane. Absence of any of the three is the
        expected state on hosts without the integration — a structured
        unavailable classification, never an exception from this method
        and never a fallback scan.
        """
        binary = shutil.which(self._op_binary)
        if binary is None:
            return ProviderStatus(
                OnePasswordErrorCode.OP_NOT_FOUND,
                f"1Password CLI {self._op_binary!r} not found; the optional "
                "provider is unavailable on this host (the .env resolver "
                "remains fully functional)",
            )
        try:
            probe = self._invoke(binary, ["--version"], token=None)
        except OnePasswordError as error:
            return ProviderStatus(error.code, error.detail)
        if probe.returncode != 0:
            return ProviderStatus(
                OnePasswordErrorCode.OP_FAILED,
                f"op --version exited with rc={probe.returncode}; stderr: "
                f"{self._scrub(probe.stderr)}",
            )
        match = re.match(r"(\d+)\.(\d+)\.(\d+)", probe.stdout.decode("utf-8", "replace"))
        if match is None:
            return ProviderStatus(
                OnePasswordErrorCode.OP_FAILED,
                "op --version produced an unparsable version string",
            )
        version = tuple(int(part) for part in match.groups())
        if version < OP_SERVICE_ACCOUNT_FLOOR:
            return ProviderStatus(
                OnePasswordErrorCode.OP_UNSUPPORTED_VERSION,
                f"op {'.'.join(map(str, version))} is below the "
                f"service-account floor "
                f"{'.'.join(map(str, OP_SERVICE_ACCOUNT_FLOOR))} "
                "(service accounts require CLI >= 2.18.0)",
            )
        try:
            self._service_account_token()
        except OnePasswordError as error:
            return ProviderStatus(error.code, error.detail)
        except EnvFileError as error:
            # The .env source itself is hostile (permissions, encoding…):
            # that is the first resolver's structured condition, propagate.
            if error.code in (EnvErrorCode.MISSING_KEY, EnvErrorCode.EMPTY_VALUE):
                return ProviderStatus(
                    OnePasswordErrorCode.TOKEN_MISSING, error.detail
                )
            raise
        return ProviderStatus(
            OnePasswordErrorCode.AVAILABLE,
            f"op {'.'.join(map(str, version))} at {binary} meets the "
            "service-account floor and the token lane is configured",
            binary=binary,
        )

    def resolve(self, reference: str) -> SecretValue:
        """Resolve one ``op://`` reference to redacted credential material.

        Raises :class:`OnePasswordError` with a value-free classification:
        ``invalid_reference`` (shape), then any unavailability code from
        :meth:`availability`, then execution codes (``op_timeout``,
        ``output_too_large``, ``invalid_encoding``, ``op_failed``,
        ``spawn_failed``). An ``EnvFileError`` from the ``.env`` token lane
        propagates unchanged except missing/empty token keys, which
        classify as ``token_missing``. There is no fallback: a failure here
        never consults any other source.
        """
        if (
            not isinstance(reference, str)
            or not _OP_REFERENCE_RE.fullmatch(reference)
            or len(reference) > MAX_REFERENCE_CHARS
        ):
            raise OnePasswordError(
                OnePasswordErrorCode.INVALID_REFERENCE,
                "credential reference must be op://<vault>/<item>/<field> "
                "with three [A-Za-z0-9][A-Za-z0-9._-]{0,127} segments "
                "(documented strict subset; spaces, percent-encoding and "
                "option-shaped segments are rejected)",
            )
        status = self.availability()
        if not status.available or status.binary is None:
            raise OnePasswordError(status.code, status.detail)
        token = self._service_account_token()
        completed = self._invoke(status.binary, ["read", reference], token=token)
        if completed.returncode != 0:
            raise OnePasswordError(
                OnePasswordErrorCode.OP_FAILED,
                f"op read exited with rc={completed.returncode}; stderr: "
                f"{self._scrub(completed.stderr)}",
            )
        if completed.stdout_overlarge:
            raise OnePasswordError(
                OnePasswordErrorCode.OUTPUT_TOO_LARGE,
                f"op read output exceeds the {MAX_OUTPUT_BYTES}-byte "
                "credential budget; refusing over-sized material "
                "(fail-closed, never truncated)",
            )
        if not completed.stdout:
            raise OnePasswordError(
                OnePasswordErrorCode.OP_FAILED,
                f"op read produced no output for the resolved reference; "
                "an empty credential is a configuration error (fail-closed)",
            )
        try:
            value = completed.stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise OnePasswordError(
                OnePasswordErrorCode.INVALID_ENCODING,
                "op read output is not valid UTF-8",
            ) from None
        return SecretValue(value)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _service_account_token(self) -> SecretValue:
        """Fetch the token through the configured lane (names only above)."""
        if self._credential_ref is not None:
            assert self._resolver is not None  # construction invariant
            try:
                return self._resolver.resolve(self._credential_ref)
            except EnvFileError as error:
                if error.code in (EnvErrorCode.MISSING_KEY, EnvErrorCode.EMPTY_VALUE):
                    raise OnePasswordError(
                        OnePasswordErrorCode.TOKEN_MISSING, error.detail
                    ) from None
                raise
        value = os.environ.get(self._token_env or "", "")
        if not value:
            raise OnePasswordError(
                OnePasswordErrorCode.TOKEN_MISSING,
                f"service-account token environment variable "
                f"{self._token_env} is not set (or empty); the provider is "
                "unavailable without it (no fallback scan)",
            )
        return SecretValue(value)

    def _child_env(self, token: SecretValue | None) -> dict[str, str]:
        """Scrubbed child environment: PATH, HOME and the token key only."""
        env: dict[str, str] = {}
        for key in ("PATH", "HOME"):
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        if token is not None:
            env[_CHILD_TOKEN_KEY] = token.value
        return env

    def _invoke(
        self, binary: str, argv: list[str], *, token: SecretValue | None
    ) -> _Completed:
        """One bounded, scrubbed child execution; value-free failures."""
        env = self._child_env(token)
        # Temp capture files keep memory flat and avoid pipe deadlocks;
        # read-back enforces the accept budgets (module docstring owns the
        # residual pre-timeout disk-write limit).
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                completed = subprocess.run(
                    [binary, *argv],
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    env=env,
                    timeout=self._timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise OnePasswordError(
                    OnePasswordErrorCode.OP_TIMEOUT,
                    f"op {argv[0]} did not finish within {self._timeout}s; "
                    "the child was killed (bounded execution)",
                ) from None
            except OSError as error:
                raise OnePasswordError(
                    OnePasswordErrorCode.SPAWN_FAILED,
                    f"op could not be executed: {error.strerror}",
                ) from None
            out.seek(0)
            stdout = out.read(MAX_OUTPUT_BYTES + 1)
            err.seek(0)
            stderr = err.read(MAX_STDERR_TAIL_BYTES + 1)
        return _Completed(
            returncode=completed.returncode,
            stdout=stdout[:MAX_OUTPUT_BYTES],
            stderr=stderr[:MAX_STDERR_TAIL_BYTES],
            stdout_overlarge=len(stdout) > MAX_OUTPUT_BYTES,
        )

    def _scrub(self, stderr: bytes) -> str:
        """Bounded stderr head with the token replaced, never emitted."""
        try:
            text = stderr.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - decode with replace cannot raise
            text = "<undecodable stderr>"
        if self._credential_ref is not None and self._resolver is not None:
            try:
                token = self._resolver.resolve(self._credential_ref).value
            except (EnvFileError, OnePasswordError):
                return text
            if token:
                text = text.replace(token, "<redacted>")
            return text
        value = os.environ.get(self._token_env or "", "")
        if value:
            text = text.replace(value, "<redacted>")
        return text


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mai.credentials.onepassword",
        description=(
            "Check or resolve through the optional 1Password CLI provider. "
            "Structured output only; credential values are never printed "
            "(resolution success is proven by the value's SHA-256 digest)."
        ),
    )
    parser.add_argument("command", choices=["check", "resolve"])
    parser.add_argument("reference", nargs="?", help="op://vault/item/field")
    parser.add_argument("--op", default=DEFAULT_OP_BINARY, help="CLI binary name/path")
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help="confined environment variable holding the service-account token",
    )
    parser.add_argument("--env-file", help=".env source for the credential_ref lane")
    parser.add_argument(
        "--credential-ref",
        help="token reference name resolved through --env-file (C03 rule 6)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-call child timeout in seconds (cap {MAX_TIMEOUT_SECONDS})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: 0 success/available, 1 structured rejection, 2 usage."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "resolve" and not args.reference:
        parser.error("resolve requires an op:// REFERENCE")
    if bool(args.env_file) != bool(args.credential_ref):
        parser.error("--env-file and --credential-ref must be given together")
    try:
        if args.env_file is not None:
            provider = OnePasswordResolver(
                op_binary=args.op,
                credential_ref=args.credential_ref,
                resolver=EnvResolver(args.env_file),
                token_env=None,
                timeout=args.timeout,
            )
        else:
            provider = OnePasswordResolver(
                op_binary=args.op,
                token_env=args.token_env,
                timeout=args.timeout,
            )
    except (OnePasswordError, EnvFileError) as error:
        code = getattr(error, "code", None)
        _emit(
            {
                "kind": "error",
                "code": code.value if isinstance(code, OnePasswordErrorCode) else code,
                "detail": str(error),
            }
        )
        return 1
    if args.command == "check":
        status = provider.availability()
        _emit(
            {
                "kind": "available" if status.available else "unavailable",
                "code": status.code.value,
                "detail": status.detail,
            }
        )
        return 0 if status.available else 1
    try:
        secret = provider.resolve(args.reference)
    except (OnePasswordError, EnvFileError) as error:
        code = getattr(error, "code", None)
        _emit(
            {
                "kind": "error",
                "code": code.value if isinstance(code, OnePasswordErrorCode) else code,
                "detail": str(error),
            }
        )
        return 1
    digest = hashlib.sha256(secret.value.encode("utf-8")).hexdigest()
    _emit({"kind": "resolved", "reference": args.reference, "sha256": digest})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
