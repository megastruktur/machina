"""Stable server profile schema: canonical ``profile.yaml`` validation.

Frozen schema producer for contracts.md C02 rule 2 ("Exact file field schemas
are frozen at P00/P02 by ``02-profile-schema`` against this section"), against:

- **C01 rule 1** — opaque stable UUID ``server_id`` is the only identity;
  display name, IP address or label is never identity. A rename (``name``
  change) therefore never changes identity or the canonical path (AC 2).
- **C02 rules 2–3** — canonical path scheme ``servers/<server_id>/profile.yaml``;
  paths resolve only from a validated opaque ID plus an approved root, so
  :func:`canonical_relative_path` accepts only a :class:`uuid.UUID` and
  traversal/symlink-style IDs are unrepresentable by construction.
- **C04 rule 1** — the target profile determines SSH host key, endpoint/user,
  environment and privilege mode. The host key is pinned as the OpenSSH
  ``SHA256:<43-char base64>`` fingerprint, the format measured in the P00 SSH
  spike (``mai_evidence/00-ssh-spike/exp4/identity-before.json``).
- **C03 rule 6** — credentials resolve by reference (an environment-variable
  name, the mandatory ``.env`` resolver); inline credential material is
  rejected, never stored in the canonical file (AC 2).
- Windowed ADR §8 rule 2 applied to canonical data: unknown fields are
  rejected, not ignored. Ignoring them would silently drop data on rewrite
  and would silently swallow injected credential fields.

Frozen vocabularies (this card is the P02 producer; additions require
reconciliation):

- ``environment``: exactly ``linux_ssh`` at this freeze — a Linux target
  reached over SSH (C04; SSH-reached container targets are the same
  environment; Windows/fleet are non-goals).
- ``privilege``: ``none | sudo | root`` (host-supervision ADR vocabulary,
  "privilege modes (root/``sudo``)").

Consumers: ``02-file-store`` (canonical IO), ``02-profile-api`` (maps
:class:`ProfileValidationError` 1:1 onto the C01 envelope: stable ``code`` +
``fields``), ``02-profile-projection`` (revision identity). The module is
pure: it reads no process environment, filesystem or network state — every
result derives from the parsed document, so validation is SSR/worker/replay
safe and evidence runs cannot leak ambient secret material.

YAML note: ``yaml`` arrives via the frozen ``dbos==3.2.0`` pin (pyyaml is a
direct dependency of dbos in ``backend/uv.lock``); adding an explicit
``backend/pyproject.toml`` entry is an out-of-allowlist edit for this card.

Production entrypoint (verified command; exit contract 0 all valid / 1 at
least one document rejected / 2 usage or IO error)::

    uv run python -m mai.contracts.server PROFILE.yaml [PROFILE.yaml ...]
"""

from __future__ import annotations

import ipaddress
import posixpath
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

SCHEMA_VERSION = 1

TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "server_id",
        "name",
        "prompt_revision",
        "environment",
        "ssh",
        "privilege",
        "capabilities",
        "file_read_roots",
    }
)
SSH_FIELDS = frozenset({"host", "port", "user", "host_key", "credential_ref"})

SUPPORTED_ENVIRONMENTS = frozenset({"linux_ssh"})
PRIVILEGE_MODES = frozenset({"none", "sudo", "root"})

# Substrings that mark an unknown field as inline credential material. Any
# unknown key carrying one is rejected with the specific
# ``inline_credential_forbidden`` code instead of generic ``unknown_field``.
_CREDENTIAL_TOKENS = (
    "password",
    "passwd",
    "secret",
    "token",
    "private_key",
    "passphrase",
    "api_key",
    "access_key",
    "credential",
)

# OpenSSH default fingerprint line: "SHA256:" + unpadded base64 of a 32-byte
# SHA-256 digest (exactly 43 characters from the standard alphabet).
_HOST_KEY_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
# Credential references are environment-variable names (C03 rule 6: the
# mandatory resolver is `.env`), e.g. ``MAI_SSH_KEY_BUILD_HOST``.
_CREDENTIAL_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
# Capability references (C03 rule 1 identifiers) are lowercase slugs;
# existence/grant checks belong to the policy/registry cards, not the schema.
_CAPABILITY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
# SSH login name: conservative charset, no whitespace, no ``@``/``:`` (keeps
# ``user@host`` unambiguous), 1–64 chars.
_SSH_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
# DNS hostname / IPv4 literal (single label allowed); 1–253 chars.
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9_])?$")

_MAX_NAME_CHARS = 200

# Optional per-server read-scope allowlist for the ``host.file_read``
# capability: absolute, normalized target-side directory prefixes.
# Absent/empty means every read is denied (fail closed). The filesystem
# root "/" is rejected as a root: it would silently allowlist the
# whole host.
MAX_FILE_READ_ROOTS = 64
_MAX_ROOT_CHARS = 4096


class ExecutionEnvironment(StrEnum):
    """Supported target execution environments (frozen set)."""

    LINUX_SSH = "linux_ssh"


class PrivilegeMode(StrEnum):
    """Target privilege modes (host-supervision ADR vocabulary)."""

    NONE = "none"
    SUDO = "sudo"
    ROOT = "root"


class ProfileErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_DOCUMENT = "invalid_document"
    UNPARSABLE_DOCUMENT = "unparsable_document"
    SCHEMA_VERSION_UNSUPPORTED = "schema_version_unsupported"
    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    INVALID_FIELD = "invalid_field"
    INVALID_SERVER_ID = "invalid_server_id"
    UNSUPPORTED_ENVIRONMENT = "unsupported_environment"
    INLINE_CREDENTIAL_FORBIDDEN = "inline_credential_forbidden"


@dataclass(frozen=True, slots=True)
class ProfileFieldError:
    """One rejection: field path + stable code + value-free reason."""

    code: ProfileErrorCode
    field: str
    detail: str


class ProfileValidationError(ValueError):
    """All rejections for one profile document.

    ``message``/``str`` contain only field paths, codes and fixed reasons —
    never document values — so the exception is safe to log, store and return
    to a browser (C01 rule 3: no secret material in any error body).
    """

    def __init__(self, errors: Sequence[ProfileFieldError]) -> None:
        self.errors: tuple[ProfileFieldError, ...] = tuple(errors)
        summary = ", ".join(f"{e.field}: {e.code}" for e in self.errors)
        super().__init__(f"profile rejected ({summary})")

    @property
    def fields(self) -> dict[str, str]:
        """Field path → stable code, ready for the C01 envelope ``fields``."""
        return {e.field: e.code for e in self.errors}


@dataclass(frozen=True, slots=True)
class SshIdentity:
    """SSH target identity (C04 rule 1); ``credential_ref`` is a reference."""

    host: str
    port: int
    user: str
    host_key: str
    credential_ref: str


@dataclass(frozen=True, slots=True)
class ServerProfile:
    """Validated canonical server profile (``servers/<server_id>/profile.yaml``).

    Identity is exactly ``server_id`` (C01 rule 1). ``name`` is a mutable
    display label: two profiles sharing ``server_id`` are the same server.
    """

    schema_version: int
    server_id: uuid.UUID
    name: str
    prompt_revision: int
    environment: ExecutionEnvironment
    ssh: SshIdentity
    privilege: PrivilegeMode
    capabilities: tuple[str, ...]
    file_read_roots: tuple[str, ...] = ()


def canonical_relative_path(server_id: uuid.UUID) -> str:
    """Canonical file path (C02 rule 2) below the approved data root.

    Accepts only a :class:`uuid.UUID`: the path is derived from the opaque ID
    alone, so traversal/absolute-path injection has no representation.
    """
    if not isinstance(server_id, uuid.UUID):
        raise TypeError("server_id must be a uuid.UUID (C02 rule 3)")
    return f"servers/{server_id}/profile.yaml"


class _StrictSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` that rejects duplicate and unhashable mapping keys.

    PyYAML would otherwise silently keep the last of duplicate keys —
    unacceptable for identity-bearing canonical files.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False):  # type: ignore[override]
        seen: list[object] = []
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:
                raise yaml.YAMLError("unhashable mapping key") from None
            if duplicate:
                raise yaml.YAMLError("duplicate mapping key")
            seen.append(key)
        return super().construct_mapping(node, deep=deep)


_MISSING = object()


def _value_of(document: Mapping[object, object], key: str) -> object:
    return document.get(key, _MISSING)


def _key_error(key: str, path: str) -> ProfileFieldError:
    """Classify one out-of-place key: credential material vs plain unknown."""
    field = f"{path}.{key}" if path else key
    if any(token in key.lower() for token in _CREDENTIAL_TOKENS):
        return ProfileFieldError(
            ProfileErrorCode.INLINE_CREDENTIAL_FORBIDDEN,
            field,
            "inline credential fields are forbidden; "
            "credentials resolve by reference (C03 rule 6)",
        )
    return ProfileFieldError(
        ProfileErrorCode.UNKNOWN_FIELD,
        field,
        "unknown field (unknown fields are rejected, never ignored)",
    )


def _walk_structure(
    document: Mapping[object, object], errors: list[ProfileFieldError]
) -> None:
    """Check keys of the document and of its ``ssh`` submapping.

    Bounded to exactly these two levels: any other container shape belongs to
    an already-rejected field, so descending into it would only add noise.
    """
    for raw_key in document:
        if not isinstance(raw_key, str):
            errors.append(
                ProfileFieldError(
                    ProfileErrorCode.UNKNOWN_FIELD,
                    "<non-string-key>",
                    "mapping keys must be strings",
                )
            )
            continue
        if raw_key not in TOP_LEVEL_FIELDS:
            errors.append(_key_error(raw_key, ""))
    raw_ssh = document.get("ssh")
    if isinstance(raw_ssh, Mapping):
        for raw_key in raw_ssh:
            if not isinstance(raw_key, str):
                errors.append(
                    ProfileFieldError(
                        ProfileErrorCode.UNKNOWN_FIELD,
                        "ssh.<non-string-key>",
                        "mapping keys must be strings",
                    )
                )
                continue
            if raw_key not in SSH_FIELDS:
                errors.append(_key_error(raw_key, "ssh"))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_clean_text(value: object) -> bool:
    return isinstance(value, str) and value.isprintable()


def _valid_ssh_host(value: str) -> bool:
    if value.startswith("["):
        # Bracketed IPv6 literal, e.g. "[2001:db8::10]"; the port stays in
        # its own field, so a trailing ":22" inside the brackets is invalid.
        if not value.endswith("]"):
            return False
        try:
            return ipaddress.ip_address(value[1:-1]).version == 6
        except ValueError:
            return False
    return bool(_SSH_HOST_RE.match(value))


def parse_server_profile(document: object) -> ServerProfile:
    """Validate one parsed profile document and return the typed profile.

    Raises :class:`ProfileValidationError` carrying every rejection found;
    field values are never included in the error payload.
    """
    errors: list[ProfileFieldError] = []
    if not isinstance(document, Mapping):
        raise ProfileValidationError(
            [
                ProfileFieldError(
                    ProfileErrorCode.INVALID_DOCUMENT,
                    "",
                    "document must be a mapping",
                )
            ]
        )

    _walk_structure(document, errors)

    if _value_of(document, "schema_version") is _MISSING:
        errors.append(
            ProfileFieldError(
                ProfileErrorCode.MISSING_FIELD, "schema_version", "required field"
            )
        )
    else:
        raw_version = document["schema_version"]
        if not _is_int(raw_version):
            errors.append(
                ProfileFieldError(
                    ProfileErrorCode.SCHEMA_VERSION_UNSUPPORTED,
                    "schema_version",
                    "must be an integer",
                )
            )
        elif raw_version != SCHEMA_VERSION:
            errors.append(
                ProfileFieldError(
                    ProfileErrorCode.SCHEMA_VERSION_UNSUPPORTED,
                    "schema_version",
                    f"unsupported schema major version (supported: {SCHEMA_VERSION})",
                )
            )

    raw_id = _value_of(document, "server_id")
    server_id: uuid.UUID | None = None
    if raw_id is _MISSING:
        errors.append(
            ProfileFieldError(
                ProfileErrorCode.MISSING_FIELD, "server_id", "required field"
            )
        )
    else:
        bad_id = ProfileFieldError(
            ProfileErrorCode.INVALID_SERVER_ID,
            "server_id",
            "must be a canonical lowercase hyphenated UUID string "
            "(C01 rule 1: opaque stable UUID identity)",
        )
        if not isinstance(raw_id, str):
            errors.append(bad_id)
        else:
            try:
                parsed = uuid.UUID(raw_id)
            except ValueError:
                errors.append(bad_id)
            else:
                if str(parsed) != raw_id:
                    errors.append(bad_id)
                else:
                    server_id = parsed

    raw_name = _value_of(document, "name")
    if raw_name is _MISSING:
        errors.append(
            ProfileFieldError(ProfileErrorCode.MISSING_FIELD, "name", "required field")
        )
    elif not _is_clean_text(raw_name) or not raw_name.strip():
        errors.append(
            ProfileFieldError(
                ProfileErrorCode.INVALID_FIELD,
                "name",
                f"must be 1..{_MAX_NAME_CHARS} printable characters, not blank",
            )
        )
    elif len(raw_name) > _MAX_NAME_CHARS:
        errors.append(
            ProfileFieldError(
                ProfileErrorCode.INVALID_FIELD,
                "name",
                f"must be 1..{_MAX_NAME_CHARS} printable characters, not blank",
            )
        )

    raw_revision = _value_of(document, "prompt_revision")
    if raw_revision is _MISSING:
        errors.append(
            ProfileFieldError(
                ProfileErrorCode.MISSING_FIELD, "prompt_revision", "required field"
            )
        )
    elif not _is_int(raw_revision) or raw_revision < 1:
        errors.append(
            ProfileFieldError(
                ProfileErrorCode.INVALID_FIELD,
                "prompt_revision",
                "must be an integer >= 1 (versioned prompt edition, C08 rule 5)",
            )
        )

    raw_environment = _value_of(document, "environment")
    if raw_environment is _MISSING:
        errors.append(
            ProfileFieldError(
                ProfileErrorCode.MISSING_FIELD, "environment", "required field"
            )
        )
    elif not isinstance(raw_environment, str) or (
        raw_environment not in SUPPORTED_ENVIRONMENTS
    ):
        errors.append(
            ProfileFieldError(
                ProfileErrorCode.UNSUPPORTED_ENVIRONMENT,
                "environment",
                "unsupported execution environment (supported: "
                + ", ".join(sorted(SUPPORTED_ENVIRONMENTS))
                + ")",
            )
        )

    raw_privilege = _value_of(document, "privilege")
    if raw_privilege is _MISSING:
        errors.append(
            ProfileFieldError(
                ProfileErrorCode.MISSING_FIELD, "privilege", "required field"
            )
        )
    elif not isinstance(raw_privilege, str) or raw_privilege not in PRIVILEGE_MODES:
        errors.append(
            ProfileFieldError(
                ProfileErrorCode.INVALID_FIELD,
                "privilege",
                "must be one of: " + ", ".join(sorted(PRIVILEGE_MODES)),
            )
        )

    raw_capabilities = _value_of(document, "capabilities")
    capabilities: tuple[str, ...] = ()
    if raw_capabilities is _MISSING:
        errors.append(
            ProfileFieldError(
                ProfileErrorCode.MISSING_FIELD, "capabilities", "required field"
            )
        )
    elif not isinstance(raw_capabilities, list) or not all(
        isinstance(item, str) for item in raw_capabilities
    ):
        errors.append(
            ProfileFieldError(
                ProfileErrorCode.INVALID_FIELD,
                "capabilities",
                "must be a list of capability identifier strings",
            )
        )
    else:
        bad_refs = [item for item in raw_capabilities if not _CAPABILITY_RE.match(item)]
        if bad_refs:
            errors.append(
                ProfileFieldError(
                    ProfileErrorCode.INVALID_FIELD,
                    "capabilities",
                    "capability identifiers must be lowercase slugs "
                    "[a-z0-9][a-z0-9._-]*",
                )
            )
        if len(set(raw_capabilities)) != len(raw_capabilities):
            errors.append(
                ProfileFieldError(
                    ProfileErrorCode.INVALID_FIELD,
                    "capabilities",
                    "duplicate capability identifier",
                )
            )
        if not bad_refs and len(set(raw_capabilities)) == len(raw_capabilities):
            capabilities = tuple(raw_capabilities)

    raw_roots = document.get("file_read_roots")
    file_read_roots: tuple[str, ...] = ()
    if raw_roots is not None:
        roots_error = ProfileFieldError(
            ProfileErrorCode.INVALID_FIELD,
            "file_read_roots",
            "must be a list of at most "
            f"{MAX_FILE_READ_ROOTS} absolute normalized directory paths "
            "(printable single line, no NUL, never '/', no duplicates)",
        )
        if not isinstance(raw_roots, list) or not all(
            isinstance(item, str) for item in raw_roots
        ):
            errors.append(roots_error)
        elif len(raw_roots) > MAX_FILE_READ_ROOTS:
            errors.append(roots_error)
        else:
            valid_roots: list[str] = []
            for item in raw_roots:
                if (
                    not item
                    or len(item) > _MAX_ROOT_CHARS
                    or not item.startswith("/")
                    or item == "/"
                    or not item.isprintable()
                    or "\x00" in item
                    or "\n" in item
                    or "\r" in item
                    or posixpath.normpath(item) != item
                ):
                    errors.append(roots_error)
                    break
                if item in valid_roots:
                    errors.append(roots_error)
                    break
                valid_roots.append(item)
            else:
                file_read_roots = tuple(raw_roots)

    ssh: SshIdentity | None = _validate_ssh(document, errors)

    if errors:
        raise ProfileValidationError(errors)

    assert server_id is not None and ssh is not None  # guaranteed above
    return ServerProfile(
        schema_version=SCHEMA_VERSION,
        server_id=server_id,
        name=raw_name,
        prompt_revision=raw_revision,
        environment=ExecutionEnvironment(raw_environment),
        ssh=ssh,
        privilege=PrivilegeMode(raw_privilege),
        capabilities=capabilities,
        file_read_roots=file_read_roots,
    )


def _validate_ssh(
    document: Mapping[object, object], errors: list[ProfileFieldError]
) -> SshIdentity | None:
    raw_ssh = _value_of(document, "ssh")
    if raw_ssh is _MISSING:
        errors.append(
            ProfileFieldError(ProfileErrorCode.MISSING_FIELD, "ssh", "required field")
        )
        return None
    if not isinstance(raw_ssh, Mapping):
        errors.append(
            ProfileFieldError(
                ProfileErrorCode.INVALID_FIELD, "ssh", "must be a mapping"
            )
        )
        return None

    for key in sorted(SSH_FIELDS):
        if key not in raw_ssh:
            errors.append(
                ProfileFieldError(
                    ProfileErrorCode.MISSING_FIELD, f"ssh.{key}", "required field"
                )
            )

    def _string_error(key: str, reason: str) -> None:
        errors.append(
            ProfileFieldError(ProfileErrorCode.INVALID_FIELD, f"ssh.{key}", reason)
        )

    host: str | None = None
    raw_host = raw_ssh.get("host", _MISSING)
    if raw_host is not _MISSING:
        if not isinstance(raw_host, str) or not _valid_ssh_host(raw_host):
            _string_error(
                "host",
                "must be a hostname, IPv4 literal or bracketed IPv6 literal "
                "(no scheme, userinfo, port suffix or whitespace)",
            )
        else:
            host = raw_host

    port: int | None = None
    raw_port = raw_ssh.get("port", _MISSING)
    if raw_port is not _MISSING:
        if not _is_int(raw_port) or not 1 <= raw_port <= 65535:
            _string_error("port", "must be an integer in 1..65535")
        else:
            port = raw_port

    user: str | None = None
    raw_user = raw_ssh.get("user", _MISSING)
    if raw_user is not _MISSING:
        if not isinstance(raw_user, str) or not _SSH_USER_RE.match(raw_user):
            _string_error(
                "user",
                "must be 1..64 chars [A-Za-z0-9_][A-Za-z0-9._-]* "
                "(no whitespace, '@' or ':')",
            )
        else:
            user = raw_user

    host_key: str | None = None
    raw_host_key = raw_ssh.get("host_key", _MISSING)
    if raw_host_key is not _MISSING:
        if not isinstance(raw_host_key, str) or not _HOST_KEY_RE.match(raw_host_key):
            _string_error(
                "host_key",
                "must be an OpenSSH SHA256 fingerprint: 'SHA256:' plus 43 "
                "base64 characters (pinned host key, C04 rule 1)",
            )
        else:
            host_key = raw_host_key

    credential_ref: str | None = None
    raw_ref = raw_ssh.get("credential_ref", _MISSING)
    if raw_ref is not _MISSING:
        if not isinstance(raw_ref, str) or not _CREDENTIAL_REF_RE.match(raw_ref):
            _string_error(
                "credential_ref",
                "must be an environment variable name [A-Z][A-Z0-9_]* "
                "(credentials resolve by reference, C03 rule 6)",
            )
        else:
            credential_ref = raw_ref

    if None in (host, port, user, host_key, credential_ref):
        return None
    return SshIdentity(
        host=host,
        port=port,
        user=user,
        host_key=host_key,
        credential_ref=credential_ref,
    )


def parse_server_profile_text(text: str) -> ServerProfile:
    """Parse YAML text (strict duplicate-key loader) and validate it."""
    try:
        document = yaml.load(text, Loader=_StrictSafeLoader)
    except yaml.YAMLError:
        raise ProfileValidationError(
            [
                ProfileFieldError(
                    ProfileErrorCode.UNPARSABLE_DOCUMENT,
                    "",
                    "document is not parsable YAML",
                )
            ]
        ) from None
    return parse_server_profile(document)


_USAGE = (
    "usage: python -m mai.contracts.server PROFILE.yaml [PROFILE.yaml ...]\n"
    "\n"
    "Validate canonical server profile documents (contracts.md C02 rule 2,\n"
    "schema frozen by 02-profile-schema).\n"
    "\n"
    "Exit codes:\n"
    "  0  every document is valid\n"
    "  1  at least one document was rejected (per-file structured line;\n"
    "     field values are never printed)\n"
    "  2  usage or IO error (bad arguments, missing/unreadable file)\n"
)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: validate each file, print one structured line each."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--help" in args or "-h" in args:
        print(_USAGE, end="")
        return 0
    if not args:
        print(_USAGE, file=sys.stderr, end="")
        return 2

    any_invalid = False
    for raw_path in args:
        try:
            text = Path(raw_path).read_text(encoding="utf-8")
        except OSError:
            print(f"IO-ERROR {raw_path}", file=sys.stderr)
            return 2
        try:
            parse_server_profile_text(text)
        except ProfileValidationError as exc:
            any_invalid = True
            fields = ",".join(f"{field}={code}" for field, code in exc.fields.items())
            print(f"INVALID {raw_path} {fields}")
        else:
            print(f"OK {raw_path}")
    return 1 if any_invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
