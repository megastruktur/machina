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
``op read op://<vault>/<item>/<field>``, ``op item list``, ``op item
create``/``op item edit`` and ``op vault get``/``op vault list``
authenticate non-interactively through the ``OP_SERVICE_ACCOUNT_TOKEN``
environment variable; service accounts require CLI >= 2.18.0
(developer.1password.com, "Use service accounts with 1Password CLI"). The
``onepassword-sdk`` distribution is NOT selected (11-dependencies freeze):
its wheels stop at CPython 3.13 and the CLI it wraps is the documented
interface anyway.

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
subprocess (``shell=False``; ``stdin`` is ``DEVNULL`` except for
:meth:`OnePasswordResolver.store_secret`, which feeds its JSON item template
on the stdin pipe
so secret material never touches ``argv``) with a per-call timeout and
output budgets: accepted ``stdout`` is capped and over-budget output fails
closed (``output_too_large`` — a silently truncated credential would be a
correctness landmine), ``stderr`` contributes only a bounded,
token-scrubbed head to error details. The child
environment is scrubbed to exactly ``PATH``, ``HOME``,
``XDG_CONFIG_HOME`` (only when the controller process itself sets it —
the compose-level redirect of op's config/socket root away from a
persistent volume) and (for authenticated calls) the token key — no
other ambient variable reaches the CLI. Residual limit,
owned honestly: between spawn and the timeout, a hostile ``op`` binary
could write unbounded bytes to the temp capture files; ``op`` is
EULA-licensed host software outside the threat model, and the budget
governs what the resolver *accepts*, which is the fail-closed boundary this
module owns.

**Write path.** :meth:`OnePasswordResolver.store_secret` is the one
deliberate write. The plaintext never travels through ``argv``: it is
embedded in a JSON item template fed to ``op item create``/``op item edit``
on the stdin pipe, every stderr-derived detail scrubs both the token and
any caller-supplied value, and a write is verified by reading the stored
material back through :meth:`OnePasswordResolver.resolve`. A write to an
existing title updates that item (idempotent), never duplicates it. Only
generated secrets carry a SHA-256 digest in the receipt (L3: no digest
oracle for human-chosen material).

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

from mai.credentials.env import (
    EnvErrorCode,
    EnvFileError,
    EnvResolver,
    SecretValue,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TOKEN_ENV",
    "MAX_LIST_OUTPUT_BYTES",
    "MAX_OUTPUT_BYTES",
    "MAX_REFERENCE_CHARS",
    "MAX_STDERR_TAIL_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "OP_SERVICE_ACCOUNT_FLOOR",
    "OnePasswordError",
    "OnePasswordErrorCode",
    "OnePasswordItemPage",
    "OnePasswordItemSummary",
    "OnePasswordProbeReport",
    "OnePasswordResolver",
    "ProviderStatus",
    "StoredSecretReceipt",
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

#: Accepted ``stdout`` budget for collection reads (``op item list``): a
#: page of item summaries is small, but a hostile/huge vault listing fails
#: closed at 2 MiB instead of growing without bound.
MAX_LIST_OUTPUT_BYTES = 2 * 1024 * 1024

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

#: Single ``op://``-segment shape (vault, item title, field label): the
#: same documented strict subset used by :data:`_OP_REFERENCE_RE`, shared
#: by ``list_items``/``store_secret`` argument validation. Full-match; the
#: first-character rule again makes option injection unrepresentable.
_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

#: ``op item list --categories`` value shape. Category names may contain a
#: space ("API Credential"); the leading-alnum rule still makes option
#: injection unrepresentable.
_CATEGORY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")

#: Categories accepted by :meth:`OnePasswordResolver.store_secret`
#: (uppercase form is what the ``op`` item template grammar expects).
_STORE_CATEGORIES = ("password", "login", "api_credential")

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

#: Ambient keys passed through to the ``op`` child verbatim besides the
#: token. ``PATH``/``HOME`` locate the binary and home; ``XDG_CONFIG_HOME``
#: is a path-only redirect (never secret material): when the controller
#: redirects op's config root away from a persistent volume (compose
#: ``XDG_CONFIG_HOME``), the child must see it or op 2.x writes its
#: ``op-daemon.sock`` under ``$HOME/.config/op/`` — dead sockets in the
#: canonical data root (finding t_8168ffeb). Unset in the parent means
#: unset in the child (no invented values).
_CHILD_PASSTHROUGH_KEYS = ("PATH", "HOME", "XDG_CONFIG_HOME")


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
class OnePasswordItemSummary:
    """One vault item as listed by ``op item list`` (metadata only).

    Carries configuration-shaped fields (ids, titles, category names,
    timestamps) — never secret material: ``op item list`` output has no
    field values by construction.
    """

    id: str
    title: str
    category: str
    vault: str
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class OnePasswordItemPage:
    """One bounded, pre-sliced page of :class:`OnePasswordItemSummary`.

    ``total`` is the pre-slice matching count and ``has_more`` reports
    whether another offset exists (repository-wide pagination envelope,
    AGENTS.md §1).
    """

    items: tuple[OnePasswordItemSummary, ...]
    limit: int
    offset: int
    total: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class StoredSecretReceipt:
    """One verified ``store_secret`` outcome.

    ``value`` is the read-back material behind the usual redacted
    ``SecretValue`` disclosure. ``sha256`` is the digest of the read-back
    value and is populated **only** for generated secrets (L3: no digest
    oracle for human-chosen material).
    """

    vault: str
    item_id: str
    title: str
    field: str
    reference: str
    value: SecretValue
    sha256: str | None
    generated: bool


@dataclass(frozen=True, slots=True)
class OnePasswordProbeReport:
    """One value-free readiness classification, optionally vault-scoped.

    ``code`` is ``"ready"`` or a :class:`OnePasswordErrorCode` value;
    ``detail`` never embeds a token or secret material.
    """

    available: bool
    code: str
    detail: str
    checked_vault: str | None = None


@dataclass(frozen=True, slots=True)
class _Completed:
    """Bounded capture of one finished child process."""

    returncode: int
    stdout: bytes
    stderr: bytes


def _validated_segment(kind: str, value: object) -> str:
    """Validate one ``op://``-style segment; fail closed before execution."""
    if not isinstance(value, str) or _SEGMENT_RE.fullmatch(value) is None:
        raise OnePasswordError(
            OnePasswordErrorCode.INVALID_REFERENCE,
            f"{kind} segment must be a non-empty "
            "[A-Za-z0-9][A-Za-z0-9._-]{0,127} string (no '/', no control "
            "characters, no option shapes)",
        )
    return value


def _parse_json_rows(stdout: bytes, what: str) -> list[dict[str, object]]:
    """Decode one ``--format json`` listing; fail closed, value-free."""
    try:
        parsed = json.loads(stdout.decode("utf-8"))
    except UnicodeDecodeError:
        raise OnePasswordError(
            OnePasswordErrorCode.INVALID_ENCODING,
            f"op {what} output is not valid UTF-8",
        ) from None
    except json.JSONDecodeError:
        raise OnePasswordError(
            OnePasswordErrorCode.OP_FAILED,
            f"op {what} produced unparsable JSON",
        ) from None
    if not isinstance(parsed, list):
        raise OnePasswordError(
            OnePasswordErrorCode.OP_FAILED,
            f"op {what} produced an unexpected JSON shape",
        )
    return [row for row in parsed if isinstance(row, dict)]


def _item_summary(row: dict[str, object]) -> OnePasswordItemSummary:
    """One ``op item list`` row → metadata summary (vault name or id)."""
    vault_field = row.get("vault")
    if isinstance(vault_field, dict):
        vault = str(vault_field.get("name") or vault_field.get("id") or "")
    elif isinstance(vault_field, str):
        vault = vault_field
    else:
        vault = ""
    updated_at = row.get("updated_at")
    return OnePasswordItemSummary(
        id=str(row.get("id") or ""),
        title=str(row.get("title") or ""),
        category=str(row.get("category") or ""),
        vault=vault,
        updated_at=updated_at if isinstance(updated_at, str) else None,
    )


def _extract_item_id(stdout: bytes, verb: str) -> str:
    """Pull the created/updated item id from ``--format json`` output."""
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        raise OnePasswordError(
            OnePasswordErrorCode.OP_FAILED,
            f"op item {verb} produced unparsable JSON",
        ) from None
    item_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(item_id, str) or not item_id:
        raise OnePasswordError(
            OnePasswordErrorCode.OP_FAILED,
            f"op item {verb} produced no item id",
        )
    return item_id


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
        match = re.match(
            r"(\d+)\.(\d+)\.(\d+)", probe.stdout.decode("utf-8", "replace")
        )
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
                return ProviderStatus(OnePasswordErrorCode.TOKEN_MISSING, error.detail)
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
        if not completed.stdout:
            raise OnePasswordError(
                OnePasswordErrorCode.OP_FAILED,
                "op read produced no output for the resolved reference; "
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

    def list_items(
        self,
        vault: str,
        *,
        query: str | None = None,
        category: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> OnePasswordItemPage:
        """List vault item metadata through ``op item list``, paginated.

        One bounded CLI call (2 MiB budget, fail-closed), deterministic
        ordering ``(title.lower(), id)``, case-insensitive substring
        ``query`` on title/id, and the repository-wide page envelope
        (``items``/``limit``/``offset``/``total``/``has_more``; AGENTS.md
        §1). The output carries metadata only — ``op item list`` has no
        field values by construction.
        """
        _validated_segment("vault", vault)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise OnePasswordError(
                OnePasswordErrorCode.INVALID_CONFIGURATION,
                "limit must be an integer within [1, 100]",
            )
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise OnePasswordError(
                OnePasswordErrorCode.INVALID_CONFIGURATION,
                "offset must be an integer >= 0",
            )
        argv = ["item", "list", "--vault", vault, "--format", "json"]
        if category:
            if not isinstance(category, str) or not _CATEGORY_RE.fullmatch(category):
                raise OnePasswordError(
                    OnePasswordErrorCode.INVALID_CONFIGURATION,
                    "category must match [A-Za-z0-9][A-Za-z0-9 ._-]{0,63} "
                    "(documented strict subset; no option shapes)",
                )
            argv += ["--categories", category]
        binary, token = self._execution_lane()
        completed = self._invoke(
            binary, argv, token=token, max_output_bytes=MAX_LIST_OUTPUT_BYTES
        )
        if completed.returncode != 0:
            raise OnePasswordError(
                OnePasswordErrorCode.OP_FAILED,
                f"op item list exited with rc={completed.returncode}; "
                f"stderr: {self._scrub(completed.stderr)}",
            )
        items = [
            _item_summary(row)
            for row in _parse_json_rows(completed.stdout, "item list")
        ]
        if query:
            needle = query.lower()
            items = [
                item
                for item in items
                if needle in item.title.lower() or needle in item.id.lower()
            ]
        items.sort(key=lambda item: (item.title.lower(), item.id))
        total = len(items)
        return OnePasswordItemPage(
            items=tuple(items[offset : offset + limit]),
            limit=limit,
            offset=offset,
            total=total,
            has_more=offset + limit < total,
        )

    def store_secret(
        self,
        vault: str,
        *,
        title: str,
        field: str = "password",
        value: str | None = None,
        generate: bool = False,
        length: int = 32,
        category: str = "password",
        username: str | None = None,
        url: str | None = None,
        notes: str | None = None,
    ) -> StoredSecretReceipt:
        """Create or update one vault item; verify by reading it back.

        The plaintext never touches ``argv``: it (or the
        ``--generate-password`` mandate) rides in a JSON item template on
        the child's stdin pipe. An existing item with the same title is
        updated via ``op item edit`` (idempotent), never duplicated. The
        write is verified by resolving ``op://<vault>/<title>/<field>``;
        for caller-chosen values a read-back mismatch fails closed. Only
        generated secrets return a SHA-256 digest (L3: no digest oracle
        for human-chosen material).
        """
        _validated_segment("vault", vault)
        _validated_segment("title", title)
        _validated_segment("field", field)
        if category not in _STORE_CATEGORIES:
            raise OnePasswordError(
                OnePasswordErrorCode.INVALID_CONFIGURATION,
                "category must be one of " + ", ".join(_STORE_CATEGORIES),
            )
        if (
            isinstance(length, bool)
            or not isinstance(length, int)
            or not 16 <= length <= 128
        ):
            raise OnePasswordError(
                OnePasswordErrorCode.INVALID_CONFIGURATION,
                "length must be an integer within [16, 128]",
            )
        if value is not None and not isinstance(value, str):
            raise OnePasswordError(
                OnePasswordErrorCode.INVALID_CONFIGURATION,
                "value must be a string",
            )
        if not generate and not value:
            raise OnePasswordError(
                OnePasswordErrorCode.INVALID_REFERENCE,
                "store_secret requires either generate=True or a non-empty value",
            )
        if generate and value is not None:
            raise OnePasswordError(
                OnePasswordErrorCode.INVALID_REFERENCE,
                "store_secret cannot combine generate=True with an explicit value",
            )
        fields: list[dict[str, str]] = []
        if value is not None:
            fields.append(
                {
                    "id": field,
                    "label": field,
                    "type": "CONCEALED",
                    "purpose": "PASSWORD",
                    "value": value,
                }
            )
        elif generate:
            # ``--generate-password`` fills the designated password field;
            # the template declares it without a value.
            fields.append(
                {
                    "id": field,
                    "label": field,
                    "type": "CONCEALED",
                    "purpose": "PASSWORD",
                }
            )
        if username is not None:
            fields.append(
                {
                    "id": "username",
                    "label": "username",
                    "type": "STRING",
                    "purpose": "USERNAME",
                    "value": username,
                }
            )
        template: dict[str, object] = {
            "title": title,
            "category": category.upper(),
            "fields": fields,
        }
        if notes is not None:
            template["notes"] = notes
        if url is not None:
            template["urls"] = [{"href": url}]
        payload = json.dumps(template).encode("utf-8")
        # The stored value is scrubbed from every stderr-derived detail.
        extra_scrub: tuple[str, ...] = (value,) if value else ()
        binary, token = self._execution_lane()
        existing = self.list_items(vault, query=title, limit=100)
        match = next((item for item in existing.items if item.title == title), None)
        if match is not None:
            verb = "edit"
            argv = [
                "item",
                "edit",
                match.id,
                "--vault",
                vault,
                "--format",
                "json",
                "-",
            ]
        else:
            verb = "create"
            argv = ["item", "create", "--vault", vault, "--format", "json", "-"]
        if generate:
            argv.append(f"--generate-password={length},letters,digits,symbols")
        completed = self._invoke(
            binary, argv, token=token, stdin_bytes=payload, extra_scrub=extra_scrub
        )
        if completed.returncode != 0:
            raise OnePasswordError(
                OnePasswordErrorCode.OP_FAILED,
                f"op item {verb} exited with rc={completed.returncode}; "
                f"stderr: {self._scrub(completed.stderr, extra_scrub)}",
            )
        item_id = _extract_item_id(completed.stdout, verb)
        reference = f"op://{vault}/{title}/{field}"
        resolved = self.resolve(reference)
        if not generate and resolved.value != value:
            raise OnePasswordError(
                OnePasswordErrorCode.OP_FAILED,
                "stored secret read-back verification mismatch",
            )
        return StoredSecretReceipt(
            vault=vault,
            item_id=item_id,
            title=title,
            field=field,
            reference=reference,
            value=resolved,
            sha256=(
                hashlib.sha256(resolved.value.encode("utf-8")).hexdigest()
                if generate
                else None
            ),
            generated=generate,
        )

    def probe(self, vault: str | None = None) -> OnePasswordProbeReport:
        """Classify provider readiness, optionally one vault, non-destructively.

        Value-free outcome either way: ``available=True`` with code
        ``"ready"``, or ``available=False`` with a structured code
        (``op_not_found``, unsupported version, missing token,
        ``invalid_reference`` for a malformed vault segment,
        ``op_failed`` for an unknown vault, ``output_too_large`` for an
        over-budget vault listing). Classification shapes never raise;
        a hostile ``.env`` token source still propagates its own
        ``EnvFileError`` (same contract as :meth:`availability`).
        """
        avail = self.availability()
        if not avail.available or avail.binary is None:
            return OnePasswordProbeReport(
                available=False,
                code=avail.code.value,
                detail=avail.detail,
                checked_vault=vault,
            )
        try:
            if vault is not None:
                _validated_segment("vault", vault)
                argv = ["vault", "get", vault, "--format", "json"]
            else:
                argv = ["vault", "list", "--format", "json"]
            token = self._service_account_token()
            completed = self._invoke(avail.binary, argv, token=token)
            if completed.returncode != 0:
                raise OnePasswordError(
                    OnePasswordErrorCode.OP_FAILED,
                    f"op {argv[1]} exited with rc={completed.returncode}; "
                    f"stderr: {self._scrub(completed.stderr)}",
                )
        except OnePasswordError as error:
            return OnePasswordProbeReport(
                available=False,
                code=error.code.value,
                detail=error.detail,
                checked_vault=vault,
            )
        return OnePasswordProbeReport(
            available=True, code="ready", detail=avail.detail, checked_vault=vault
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _execution_lane(self) -> tuple[str, SecretValue]:
        """Binary path + token for an authenticated call, fail-closed."""
        status = self.availability()
        if not status.available or status.binary is None:
            raise OnePasswordError(status.code, status.detail)
        return status.binary, self._service_account_token()

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
        """Scrubbed child environment: ``PATH``, ``HOME``,
        ``XDG_CONFIG_HOME`` (when the parent sets it) and the token key
        only — every other ambient variable is dropped.
        """
        env: dict[str, str] = {}
        for key in _CHILD_PASSTHROUGH_KEYS:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        if token is not None:
            env[_CHILD_TOKEN_KEY] = token.value
        return env

    def _invoke(
        self,
        binary: str,
        argv: list[str],
        *,
        token: SecretValue | None,
        stdin_bytes: bytes | None = None,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        extra_scrub: Sequence[str] = (),
    ) -> _Completed:
        """One bounded, scrubbed child execution; value-free failures.

        ``stdin_bytes`` feeds a child stdin pipe (``subprocess.run``
        wraps ``stdin=PIPE`` + ``communicate(input=...)``); the default
        stays ``DEVNULL`` so plain reads can never be prompted into
        hanging. ``max_output_bytes`` is enforced here: over-budget
        ``stdout`` raises ``output_too_large`` (fail-closed, never
        truncated). ``extra_scrub`` strings (e.g. a stored value) are
        scrubbed from stderr-derived details by the callers that own
        them, alongside the token.
        """
        env = self._child_env(token)
        # Temp capture files keep memory flat and avoid pipe deadlocks;
        # read-back enforces the accept budgets (module docstring owns the
        # residual pre-timeout disk-write limit).
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                if stdin_bytes is None:
                    completed = subprocess.run(
                        [binary, *argv],
                        stdin=subprocess.DEVNULL,
                        stdout=out,
                        stderr=err,
                        env=env,
                        timeout=self._timeout,
                        check=False,
                    )
                else:
                    completed = subprocess.run(
                        [binary, *argv],
                        input=stdin_bytes,
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
            stdout = out.read(max_output_bytes + 1)
            err.seek(0)
            stderr = err.read(MAX_STDERR_TAIL_BYTES + 1)
        if len(stdout) > max_output_bytes:
            raise OnePasswordError(
                OnePasswordErrorCode.OUTPUT_TOO_LARGE,
                f"op {argv[0]} output exceeds the {max_output_bytes}-byte "
                "budget; refusing over-sized output (fail-closed, never "
                "truncated)",
            )
        return _Completed(
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr[:MAX_STDERR_TAIL_BYTES],
        )

    def _scrub(self, stderr: bytes, extra_scrub: Sequence[str] = ()) -> str:
        """Bounded stderr head with the token replaced, never emitted.

        ``extra_scrub`` strings (e.g. a caller-supplied secret value) are
        replaced after the token; empty needles are skipped.
        """
        try:
            text = stderr.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - decode with replace cannot raise
            text = "<undecodable stderr>"
        if self._credential_ref is not None and self._resolver is not None:
            try:
                token = self._resolver.resolve(self._credential_ref).value
            except EnvFileError, OnePasswordError:
                return text
            if token:
                text = text.replace(token, "<redacted>")
        else:
            value = os.environ.get(self._token_env or "", "")
            if value:
                text = text.replace(value, "<redacted>")
        for needle in extra_scrub:
            if needle:
                text = text.replace(needle, "<redacted>")
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
