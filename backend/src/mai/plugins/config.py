"""Confined atomic plugin/MCP configuration store (contracts.md C02/C03).

Card ``09-plugin-store`` against **C02 rules 1–5** (explicit data root,
confined canonical paths, revision = content revision + schema revision +
hash, atomic temp+replace, crash windows W1/W2 detected honestly — the
SQL projection/journal halves belong to the projection owner) and
**C03 rules 1/2/6/7** (credential *references* only; secret values are
never serialized into stored configuration; plugin availability and
per-server grants are separate concerns; no installation-time auto-grant).

What is canonical here
----------------------

Two document kinds, one file pair each (canonical document + revision
metadata sidecar), below one explicitly configured data root:

- plugin connection config ``plugins/<plugin>/config.yaml`` — the frozen
  document is ``{schema_version, plugin, enabled, credential_refs,
  config}``. ``enabled=False`` is the load-time disable knob the worker
  composition passes to
  :class:`mai.plugins.native.NativePluginRuntime`; ``credential_refs``
  stores reference *names* only (the mandatory ``.env`` resolver owns
  values, C03 rule 6); ``config`` carries connection parameters.
- server grant binding ``plugins/<plugin>/bindings/<server_id>.yaml`` —
  ``{schema_version, plugin, server_id, capabilities}``: the capability
  identifiers of that plugin explicitly granted to one server. Bindings
  are separate documents, written by explicit admin action — installing
  or configuring a plugin never creates one (SERVICES decision-dependency
  row: no installation-time auto-grant).

MCP server connections are plugin configs in this namespace (C03 rule 7:
MCP tools carry independent grants and server scope — per-server scope
lives in these grant bindings); the MCP transport itself is ``09-mcp``'s
ownership and shares this store through its public API.

Boundary discipline (sibling-pattern provenance)
------------------------------------------------

- Confinement, atomic replace, metadata sidecars and stale-revision
  rejection mirror the reviewed ``mai.profiles.files`` producer (card
  ``02-file-store``); the helpers are local copies because the canonical
  path scheme and identity are this card's own (plugin name + server
  UUID, not ``server_id`` alone). A divergence in the shared mechanics
  would be a review finding, not a silent import of private symbols.
- ``plugin`` names, capability identifiers and credential references
  reuse the frozen shapes of ``mai.plugins.registry`` /
  ``mai.contracts.server`` (the vocabulary owners, both outside this
  allowlist) as anchored local copies — same review note as the native
  runtime's.
- ``config`` values are validated against the plugin's declared
  ``config_schema`` (the C03 rule 1 descriptor field) using the same
  fail-closed JSON-Schema subset the tool bridge asserts for payloads
  (``mai.agents.tools``): an unsupported applicative keyword is a
  rejection, never a silent pass. The bridge validates *call payloads*;
  this module validates *stored configuration* — a local implementation
  of the identical keyword subset, drift between the two is a review
  finding.
- Any ``config`` key carrying a credential-token substring (the profile
  schema owner's token list) is rejected outright, declared in the
  schema or not: stored configuration has no legitimate plaintext
  credential field — credentials resolve by reference (C03 rule 6). A
  plugin that needs a token-shaped *non-secret* number (a TTL, a retry
  count) must name it without a credential token; this is the same
  categorical stance as the descriptor envelope, traded deliberately
  against per-schema exceptions.
- Every rejection is value-free: details carry field paths, names,
  identifiers and fixed reasons — never document values, never resolved
  credential material. Errors are safe to log and to return to a
  browser (C01).
- The store resolves no credentials, reads no process environment and
  performs no execution: it is persistence and validation only.
  Availability (``enabled`` + installed distribution → registered
  descriptor) and authorization (grants/bindings → dispatcher/policy)
  meet only in the read-only views :func:`server_exposure` and
  :func:`unbacked_bindings`.

Deleted-plugin safety (AC2): exposure is an **intersection** of stored
grants with registered descriptors (C03 rule 3 discipline — never a
union). A plugin that is uninstalled, disabled or updated away from a
capability therefore cannot keep a binding silently usable: the
capability simply is not registered, so it is not exposed and a dispatch
would deny ``capability_not_registered``. The state is additionally
*surfaced* by :func:`unbacked_bindings` (C02 W4 discipline: a stale
reference is a conflict reported to the admin, never silently honored or
silently dropped). Deleting a connection config is refused while grant
bindings still reference it — bindings are removed first, so no binding
can outlive its config through the store's own API.

Single-writer per (plugin, server) is a caller invariant, as in the
profile store; the store guarantees atomic visibility of each committed
file pair member, stale-caller rejection and root confinement.

Thread safety: instances are immutable after construction; every call
re-reads canonical state from the filesystem.

Production entrypoint (verified command; exit contract 0 success /
1 structured store rejection / 2 usage or IO error)::

    uv run python -m mai.plugins.config inspect DATA_ROOT
    uv run python -m mai.plugins.config compose DATA_ROOT
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import types
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

import yaml

from mai.plugins.native import NativePluginRuntime
from mai.plugins.registry import CapabilityRegistry

__all__ = [
    "BINDINGS_DIRNAME",
    "MAX_DOCUMENT_BYTES",
    "PLUGIN_CONFIG_FILENAME",
    "PLUGINS_DIRNAME",
    "SCHEMA_VERSION",
    "GrantBindingDocument",
    "PluginConfigDocument",
    "PluginConfigError",
    "PluginConfigErrorCode",
    "PluginConfigStore",
    "StoredGrantBinding",
    "StoredPluginConfig",
    "UnbackedBinding",
    "canonical_config_relative_path",
    "canonical_binding_relative_path",
    "compose_runtime",
    "main",
    "parse_grant_binding",
    "parse_grant_binding_text",
    "parse_plugin_config",
    "parse_plugin_config_text",
    "server_exposure",
    "unbacked_bindings",
]

#: Frozen schema revision of both document kinds (this card is the
#: producer; additions require contract reconciliation).
SCHEMA_VERSION = 1

PLUGINS_DIRNAME = "plugins"
BINDINGS_DIRNAME = "bindings"
PLUGIN_CONFIG_FILENAME = "config.yaml"
PLUGIN_META_FILENAME = "config.meta.json"

#: Byte cap for one canonical document, enforced before parsing. Same
#: rationale as the profile store's cap (mai.profiles.files): plugin
#: configuration is a small admin-controlled document; 64 KiB is two
#: orders of magnitude above any legitimate fixture and bounds parser
#: work. Local constant: the cap boundary for this namespace is owned
#: here, not shared by import with the profile lane.
MAX_DOCUMENT_BYTES = 64 * 1024

PLUGIN_CONFIG_FIELDS = frozenset(
    {"schema_version", "plugin", "enabled", "credential_refs", "config"}
)
BINDING_FIELDS = frozenset(
    {"schema_version", "plugin", "server_id", "capabilities"}
)

#: Revision convention for "no canonical file stored" (create baseline).
ABSENT_REVISION = 0

_META_FORMAT = 1

_HEX_DIGITS = frozenset("0123456789abcdef")

#: Plugin (entry-point) name shape — local copy of the native runtime's
#: frozen pattern (capability slug charset, C03 rule 1). Anchored
#: explicitly; the charset excludes ``/`` and a leading ``.`` so the
#: canonical path is traversal-proof by construction.
PLUGIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

#: Credential-reference shape — local copy of the canonical profile
#: schema owner's pattern (C03 rule 6), anchored like the native
#: runtime's copy.
_CREDENTIAL_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")

#: Capability identifier shape — local copy of the registry's frozen
#: pattern (C03 rule 1).
_CAPABILITY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

#: Credential-material markers for ``config`` keys — the profile schema
#: owner's token list. Any ``config`` key carrying one is rejected
#: outright: stored configuration never holds credential-shaped fields.
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


class PluginConfigErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_PLUGIN_NAME = "invalid_plugin_name"
    INVALID_SERVER_ID = "invalid_server_id"
    INVALID_REVISION = "invalid_revision"
    INVALID_WIRING = "invalid_wiring"
    PLUGIN_MISMATCH = "plugin_mismatch"
    SERVER_ID_MISMATCH = "server_id_mismatch"
    INVALID_DOCUMENT = "invalid_document"
    UNPARSABLE_DOCUMENT = "unparsable_document"
    DOCUMENT_TOO_LARGE = "document_too_large"
    DOCUMENT_TOO_DEEP = "document_too_deep"
    DOCUMENT_ALIAS_FORBIDDEN = "document_alias_forbidden"
    SCHEMA_VERSION_UNSUPPORTED = "schema_version_unsupported"
    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    FIELD_TYPE_INVALID = "field_type_invalid"
    CREDENTIAL_REF_INVALID = "credential_ref_invalid"
    INLINE_CREDENTIAL_FORBIDDEN = "inline_credential_forbidden"
    CONFIG_SCHEMA_INVALID = "config_schema_invalid"
    CONFIG_SCHEMA_UNSUPPORTED = "config_schema_unsupported_keyword"
    PATH_ESCAPE = "path_escape"
    NOT_FOUND = "not_found"
    STALE_REVISION = "stale_revision"
    BINDINGS_PRESENT = "bindings_present"
    METADATA_MISSING = "metadata_missing"
    METADATA_INVALID = "metadata_invalid"
    ORPHAN_METADATA = "orphan_metadata"
    HASH_MISMATCH = "hash_mismatch"


class PluginConfigError(ValueError):
    """One structured, value-free store/schema rejection."""

    def __init__(self, code: PluginConfigErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PluginConfigDocument:
    """Validated plugin connection config (``plugins/<p>/config.yaml``).

    ``config`` is wrapped in a read-only mapping: parsed documents are
    immutable views, mutation is a caller bug, never a store write path.
    """

    plugin: str
    enabled: bool
    credential_refs: tuple[str, ...]
    config: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class GrantBindingDocument:
    """Validated server grant binding (``plugins/<p>/bindings/<sid>.yaml``)."""

    plugin: str
    server_id: uuid.UUID
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StoredPluginConfig:
    """Stored config: content plus the C02 rule-4 version triple."""

    plugin: str
    text: str
    revision: int
    schema_revision: int
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredGrantBinding:
    """Stored binding: content plus the C02 rule-4 version triple."""

    plugin: str
    server_id: uuid.UUID
    text: str
    revision: int
    schema_revision: int
    sha256: str


@dataclass(frozen=True, slots=True)
class UnbackedBinding:
    """One stored binding whose granted capabilities are not registered.

    Surfaced by :func:`unbacked_bindings` so a deleted/uninstalled/
    disabled plugin or a capability removed by an update is a *reported*
    conflict (C02 W4 discipline), never a silently honored grant.
    """

    plugin: str
    server_id: uuid.UUID
    missing_capabilities: tuple[str, ...]


def canonical_config_relative_path(plugin: str) -> str:
    """Canonical config path below the approved data root.

    Accepts only a validated plugin name (slug charset — no ``/``, no
    leading ``.``, so traversal is unrepresentable), mirroring the
    profile schema's UUID-only rule (C02 rule 3).
    """
    _validated_plugin_name(plugin)
    return f"{PLUGINS_DIRNAME}/{plugin}/{PLUGIN_CONFIG_FILENAME}"


def canonical_binding_relative_path(plugin: str, server_id: uuid.UUID) -> str:
    """Canonical binding path below the approved data root."""
    _validated_plugin_name(plugin)
    _validated_server_id(server_id)
    return (
        f"{PLUGINS_DIRNAME}/{plugin}/{BINDINGS_DIRNAME}/"
        f"{server_id}.yaml"
    )


# ---------------------------------------------------------------------------
# Document schema (frozen vocabulary, strict-field validation)
# ---------------------------------------------------------------------------


class _StrictSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` rejecting duplicate and unhashable mapping keys.

    PyYAML would otherwise silently keep the last of duplicate keys —
    unacceptable for revision-bearing canonical files (profile schema
    owner's loader, local copy).
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


def _validated_plugin_name(plugin: object) -> str:
    if not isinstance(plugin, str) or not PLUGIN_NAME_RE.fullmatch(plugin):
        raise PluginConfigError(
            PluginConfigErrorCode.INVALID_PLUGIN_NAME,
            f"plugin name must match {PLUGIN_NAME_RE.pattern}",
        )
    return plugin


def _validated_server_id(server_id: object) -> uuid.UUID:
    if not isinstance(server_id, uuid.UUID):
        raise PluginConfigError(
            PluginConfigErrorCode.INVALID_SERVER_ID,
            "server_id must be a uuid.UUID instance",
        )
    return server_id


def _validated_expected_revision(expected_revision: object) -> int:
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 0
    ):
        raise PluginConfigError(
            PluginConfigErrorCode.INVALID_REVISION,
            "expected_revision must be a non-negative int",
        )
    return expected_revision


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_canonical_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX_DIGITS


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _key_error(key: object, path: str) -> PluginConfigError:
    """Classify one out-of-place key: credential material vs plain unknown."""
    label = key if isinstance(key, str) else "<non-string-key>"
    field = f"{path}.{label}" if path else str(label)
    if isinstance(key, str) and any(
        token in key.lower() for token in _CREDENTIAL_TOKENS
    ):
        return PluginConfigError(
            PluginConfigErrorCode.INLINE_CREDENTIAL_FORBIDDEN,
            f"{field}: inline credential fields are forbidden in stored "
            "configuration; credentials resolve by reference (C03 rule 6)",
        )
    return PluginConfigError(
        PluginConfigErrorCode.UNKNOWN_FIELD,
        f"{field}: unknown field (unknown fields are rejected, never ignored)",
    )


def _parse_refs(raw: object) -> tuple[str, ...]:
    """Validate the ``credential_refs`` list: reference names, unique."""
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise PluginConfigError(
            PluginConfigErrorCode.FIELD_TYPE_INVALID,
            "credential_refs: must be a list of strings",
        )
    for item in raw:
        if not _CREDENTIAL_REF_RE.fullmatch(item):
            raise PluginConfigError(
                PluginConfigErrorCode.CREDENTIAL_REF_INVALID,
                f"credential_refs: {item!r} must be an environment-variable "
                "name (C03 rule 6: credentials resolve by reference)",
            )
    if len(set(raw)) != len(raw):
        raise PluginConfigError(
            PluginConfigErrorCode.FIELD_TYPE_INVALID,
            "credential_refs: contains duplicates",
        )
    return tuple(raw)


def _check_config_keys(value: object, path: str, depth: int) -> None:
    """Every ``config`` mapping key must be a string without token markers.

    Iterative traversal with a hard depth bound: hostile nesting cannot
    drive recursion. Values are never inspected beyond container shape —
    the rejection carries the key path only, never a value.
    """
    if depth > _MAX_STRUCTURE_DEPTH:
        raise PluginConfigError(
            PluginConfigErrorCode.DOCUMENT_TOO_DEEP,
            f"{path}: configuration nesting exceeds the supported depth",
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PluginConfigError(
                    PluginConfigErrorCode.FIELD_TYPE_INVALID,
                    f"{path}: configuration keys must be strings",
                )
            if any(token in key.lower() for token in _CREDENTIAL_TOKENS):
                raise PluginConfigError(
                    PluginConfigErrorCode.INLINE_CREDENTIAL_FORBIDDEN,
                    f"{path}.{key}: inline credential fields are forbidden "
                    "in stored configuration; credentials resolve by "
                    "reference (C03 rule 6)",
                )
            _check_config_keys(item, f"{path}.{key}", depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_config_keys(item, f"{path}[{index}]", depth + 1)


def _validate_config_document(document: object) -> PluginConfigDocument:
    """Validate one parsed plugin-config document against the frozen shape."""
    if not isinstance(document, Mapping):
        raise PluginConfigError(
            PluginConfigErrorCode.INVALID_DOCUMENT,
            "document must be a mapping",
        )
    for raw_key in document:
        if not isinstance(raw_key, str) or raw_key not in PLUGIN_CONFIG_FIELDS:
            raise _key_error(raw_key, "")
    missing = [name for name in PLUGIN_CONFIG_FIELDS if name not in document]
    if missing:
        raise PluginConfigError(
            PluginConfigErrorCode.MISSING_FIELD,
            f"missing required field(s): {', '.join(sorted(missing))}",
        )
    raw_version = document["schema_version"]
    if not _is_int(raw_version) or raw_version != SCHEMA_VERSION:
        raise PluginConfigError(
            PluginConfigErrorCode.SCHEMA_VERSION_UNSUPPORTED,
            f"schema_version: unsupported (supported: {SCHEMA_VERSION})",
        )
    plugin = _validated_plugin_name(document["plugin"])
    raw_enabled = document["enabled"]
    if not isinstance(raw_enabled, bool):
        raise PluginConfigError(
            PluginConfigErrorCode.FIELD_TYPE_INVALID,
            "enabled: must be a boolean",
        )
    refs = _parse_refs(document["credential_refs"])
    raw_config = document["config"]
    if not isinstance(raw_config, Mapping):
        raise PluginConfigError(
            PluginConfigErrorCode.FIELD_TYPE_INVALID,
            "config: must be a JSON object",
        )
    _check_config_keys(raw_config, "config", 0)
    return PluginConfigDocument(
        plugin=plugin,
        enabled=raw_enabled,
        credential_refs=refs,
        config=types.MappingProxyType(dict(raw_config)),
    )


def _validate_binding_document(document: object) -> GrantBindingDocument:
    """Validate one parsed grant-binding document against the frozen shape."""
    if not isinstance(document, Mapping):
        raise PluginConfigError(
            PluginConfigErrorCode.INVALID_DOCUMENT,
            "document must be a mapping",
        )
    for raw_key in document:
        if not isinstance(raw_key, str) or raw_key not in BINDING_FIELDS:
            raise _key_error(raw_key, "")
    missing = [name for name in BINDING_FIELDS if name not in document]
    if missing:
        raise PluginConfigError(
            PluginConfigErrorCode.MISSING_FIELD,
            f"missing required field(s): {', '.join(sorted(missing))}",
        )
    raw_version = document["schema_version"]
    if not _is_int(raw_version) or raw_version != SCHEMA_VERSION:
        raise PluginConfigError(
            PluginConfigErrorCode.SCHEMA_VERSION_UNSUPPORTED,
            f"schema_version: unsupported (supported: {SCHEMA_VERSION})",
        )
    plugin = _validated_plugin_name(document["plugin"])
    raw_server = document["server_id"]
    if not isinstance(raw_server, str):
        raise PluginConfigError(
            PluginConfigErrorCode.INVALID_SERVER_ID,
            "server_id: must be a canonical lowercase hyphenated UUID string",
        )
    try:
        parsed_server = uuid.UUID(raw_server)
    except ValueError:
        raise PluginConfigError(
            PluginConfigErrorCode.INVALID_SERVER_ID,
            "server_id: must be a canonical lowercase hyphenated UUID string",
        ) from None
    if str(parsed_server) != raw_server:
        raise PluginConfigError(
            PluginConfigErrorCode.INVALID_SERVER_ID,
            "server_id: must be a canonical lowercase hyphenated UUID string",
        )
    raw_caps = document["capabilities"]
    if not isinstance(raw_caps, list) or any(
        not isinstance(item, str) for item in raw_caps
    ):
        raise PluginConfigError(
            PluginConfigErrorCode.FIELD_TYPE_INVALID,
            "capabilities: must be a list of identifier strings",
        )
    if not raw_caps:
        raise PluginConfigError(
            PluginConfigErrorCode.FIELD_TYPE_INVALID,
            "capabilities: must not be empty (a binding grants something)",
        )
    for item in raw_caps:
        if not _CAPABILITY_RE.fullmatch(item):
            raise PluginConfigError(
                PluginConfigErrorCode.FIELD_TYPE_INVALID,
                f"capabilities: {item!r} is not a capability identifier",
            )
    if len(set(raw_caps)) != len(raw_caps):
        raise PluginConfigError(
            PluginConfigErrorCode.FIELD_TYPE_INVALID,
            "capabilities: contains duplicates",
        )
    return GrantBindingDocument(
        plugin=plugin,
        server_id=parsed_server,
        capabilities=tuple(raw_caps),
    )


def parse_plugin_config(document: object) -> PluginConfigDocument:
    """Validate one parsed plugin-config document (frozen vocabulary)."""
    return _validate_config_document(document)


def parse_plugin_config_text(text: str) -> PluginConfigDocument:
    """Parse YAML text (strict duplicate-key loader) and validate it."""
    try:
        document = yaml.load(text, Loader=_StrictSafeLoader)
    except yaml.YAMLError:
        raise PluginConfigError(
            PluginConfigErrorCode.UNPARSABLE_DOCUMENT,
            "document is not parsable YAML (or has duplicate keys)",
        ) from None
    except RecursionError:
        raise PluginConfigError(
            PluginConfigErrorCode.DOCUMENT_TOO_DEEP,
            "document nesting exceeds the supported depth",
        ) from None
    return _validate_config_document(document)


def parse_grant_binding(document: object) -> GrantBindingDocument:
    """Validate one parsed grant-binding document (frozen vocabulary)."""
    return _validate_binding_document(document)


def parse_grant_binding_text(text: str) -> GrantBindingDocument:
    """Parse YAML text (strict duplicate-key loader) and validate it."""
    try:
        document = yaml.load(text, Loader=_StrictSafeLoader)
    except yaml.YAMLError:
        raise PluginConfigError(
            PluginConfigErrorCode.UNPARSABLE_DOCUMENT,
            "document is not parsable YAML (or has duplicate keys)",
        ) from None
    except RecursionError:
        raise PluginConfigError(
            PluginConfigErrorCode.DOCUMENT_TOO_DEEP,
            "document nesting exceeds the supported depth",
        ) from None
    return _validate_binding_document(document)


# ---------------------------------------------------------------------------
# JSON-Schema subset for stored ``config`` values (tool-bridge parity)
# ---------------------------------------------------------------------------

#: Byte cap for one ``config_schema`` document. It arrives from a
#: validated descriptor (registry cap: 16 KiB per schema document); the
#: write path re-bounds it so a caller-assembled mapping cannot bypass
#: the producer's cap.
MAX_CONFIG_SCHEMA_BYTES = 16 * 1024

#: Recursion bound mirroring the tool bridge's payload validator.
_MAX_SCHEMA_DEPTH = 24
_MAX_STRUCTURE_DEPTH = 24

#: The fail-closed keyword subset — the exact set the tool bridge asserts
#: for call payloads (``mai.agents.tools``), applied here to stored
#: ``config`` values. Anything else that can affect validity is a
#: rejection, never a silent pass.
_SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "enum",
        "const",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
    }
)

_ANNOTATION_KEYWORDS = frozenset(
    {"title", "description", "default", "examples", "format", "$schema", "$id"}
)

_JSON_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")


def _json_equal(left: object, right: object) -> bool:
    """JSON equality: ``True`` never equals ``1`` (bool is its own type)."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right


def _check_type(value: object, expected: object) -> bool:
    """One JSON Schema type assertion (bool is never a number)."""
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and value.is_integer()
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False  # unknown type name: fail-closed


def _check_schema(
    value: object, schema: object, *, path: str, depth: int = 0
) -> str | None:
    """Validate one config value against the declared schema subset.

    Returns ``None`` when satisfied, else a value-free violation
    description (values are never quoted — they may carry
    credential-shaped material).
    """
    if depth > _MAX_SCHEMA_DEPTH:
        return f"{path}: schema nesting exceeds the supported depth"
    if not isinstance(schema, Mapping):
        return f"{path}: schema must be a JSON object"
    for keyword in schema:
        if (
            keyword not in _SUPPORTED_KEYWORDS
            and keyword not in _ANNOTATION_KEYWORDS
        ):
            return f"{path}: schema uses unsupported keyword {keyword!r}"

    expected_type = schema.get("type")
    if expected_type is not None:
        names = expected_type if isinstance(expected_type, list) else [expected_type]
        if not names or any(name not in _JSON_TYPES for name in names):
            return f"{path}: schema declares an unsupported type"
        if not any(_check_type(value, name) for name in names):
            return (
                f"{path}: value does not match declared type "
                f"{[str(name) for name in names]}"
            )

    if "const" in schema and not _json_equal(value, schema["const"]):
        return f"{path}: value does not match the const value"
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list):
            return f"{path}: schema enum must be an array"
        if not any(_json_equal(value, choice) for choice in choices):
            return f"{path}: value is not one of the enum values"

    if isinstance(value, Mapping):
        return _check_object(value, schema, path=path, depth=depth)
    if isinstance(value, list):
        return _check_array(value, schema, path=path, depth=depth)
    if isinstance(value, str):
        return _check_string(value, schema, path=path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _check_number(value, schema, path=path)
    return None


def _check_object(
    value: Mapping[object, object],
    schema: Mapping[object, object],
    *,
    path: str,
    depth: int,
) -> str | None:
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return f"{path}: schema properties must be an object"
    for key in schema.get("required", []):
        if not isinstance(key, str):
            return f"{path}: schema required entries must be strings"
        if key not in value:
            return f"{path}.{key}: required property is missing"
    additional = schema.get("additionalProperties", True)
    for key, item in value.items():
        if not isinstance(key, str):
            return f"{path}: property names must be strings"
        if key in properties:
            violation = _check_schema(
                item, properties[key], path=f"{path}.{key}", depth=depth + 1
            )
            if violation is not None:
                return violation
        elif additional is False:
            return f"{path}.{key}: property is not allowed"
        elif isinstance(additional, Mapping):
            violation = _check_schema(
                item, additional, path=f"{path}.{key}", depth=depth + 1
            )
            if violation is not None:
                return violation
    return None


def _check_array(
    value: list[object],
    schema: Mapping[object, object],
    *,
    path: str,
    depth: int,
) -> str | None:
    min_items = schema.get("minItems")
    max_items = schema.get("maxItems")
    for name, bound, violated in (
        (
            "minItems",
            min_items,
            len(value) < min_items if min_items is not None else False,
        ),
        (
            "maxItems",
            max_items,
            len(value) > max_items if max_items is not None else False,
        ),
    ):
        if bound is not None and (not isinstance(bound, int) or isinstance(bound, bool)):
            return f"{path}: schema {name} must be an integer"
        if violated:
            return f"{path}: array violates the {name} bound"
    items = schema.get("items")
    if items is not None:
        for index, element in enumerate(value):
            violation = _check_schema(
                element, items, path=f"{path}[{index}]", depth=depth + 1
            )
            if violation is not None:
                return violation
    return None


def _check_string(
    value: str, schema: Mapping[object, object], *, path: str
) -> str | None:
    min_length = schema.get("minLength")
    max_length = schema.get("maxLength")
    for name, bound, violated in (
        (
            "minLength",
            min_length,
            len(value) < min_length if min_length is not None else False,
        ),
        (
            "maxLength",
            max_length,
            len(value) > max_length if max_length is not None else False,
        ),
    ):
        if bound is not None and (
            not isinstance(bound, int) or isinstance(bound, bool) or bound < 0
        ):
            return f"{path}: schema {name} must be a non-negative integer"
        if violated:
            return f"{path}: string violates the {name} bound"
    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            return f"{path}: schema pattern must be a string"
        try:
            if re.search(pattern, value) is None:
                return f"{path}: string does not match the declared pattern"
        except re.error:
            return f"{path}: schema pattern is not a valid regex"
    return None


def _check_number(
    value: int | float, schema: Mapping[object, object], *, path: str
) -> str | None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    exclusive_minimum = schema.get("exclusiveMinimum")
    exclusive_maximum = schema.get("exclusiveMaximum")
    for name, bound in (
        ("minimum", minimum),
        ("maximum", maximum),
        ("exclusiveMinimum", exclusive_minimum),
        ("exclusiveMaximum", exclusive_maximum),
    ):
        if bound is not None and (
            not isinstance(bound, (int, float)) or isinstance(bound, bool)
        ):
            return f"{path}: schema {name} must be a number"
    if minimum is not None and value < minimum:
        return f"{path}: number is below the minimum"
    if maximum is not None and value > maximum:
        return f"{path}: number is above the maximum"
    if exclusive_minimum is not None and not value > exclusive_minimum:
        return f"{path}: number violates the exclusiveMinimum bound"
    if exclusive_maximum is not None and not value < exclusive_maximum:
        return f"{path}: number violates the exclusiveMaximum bound"
    return None


def _checked_config_values(
    config: Mapping[str, object], config_schema: Mapping[str, object] | None
) -> None:
    """Validate stored ``config`` values against the descriptor's schema.

    ``config_schema=None`` means the plugin declares no configuration:
    any non-empty ``config`` is then a rejection (fail-closed — there is
    nothing declared to validate against).
    """
    if config_schema is None:
        if config:
            raise PluginConfigError(
                PluginConfigErrorCode.CONFIG_SCHEMA_INVALID,
                "config: plugin declares no config_schema; stored config "
                "must be empty",
            )
        return
    if not isinstance(config_schema, Mapping):
        raise PluginConfigError(
            PluginConfigErrorCode.CONFIG_SCHEMA_INVALID,
            "config_schema: must be a JSON object",
        )
    try:
        encoded = json.dumps(config_schema, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError):
        raise PluginConfigError(
            PluginConfigErrorCode.CONFIG_SCHEMA_INVALID,
            "config_schema: must be a JSON-safe document",
        ) from None
    if len(encoded) > MAX_CONFIG_SCHEMA_BYTES:
        raise PluginConfigError(
            PluginConfigErrorCode.CONFIG_SCHEMA_INVALID,
            f"config_schema exceeds {MAX_CONFIG_SCHEMA_BYTES} bytes",
        )
    violation = _check_schema(config, config_schema, path="config")
    if violation is not None:
        raise PluginConfigError(
            PluginConfigErrorCode.CONFIG_SCHEMA_INVALID,
            violation,
        )


# ---------------------------------------------------------------------------
# Confined atomic storage (mirrors mai.profiles.files, own identities)
# ---------------------------------------------------------------------------


def canonical_meta_relative_path(plugin: str) -> str:
    """Config sidecar metadata path (C02 rule 4) below the data root."""
    _validated_plugin_name(plugin)
    return f"{PLUGINS_DIRNAME}/{plugin}/{PLUGIN_META_FILENAME}"


def canonical_binding_meta_relative_path(
    plugin: str, server_id: uuid.UUID
) -> str:
    """Binding sidecar metadata path (C02 rule 4) below the data root."""
    _validated_plugin_name(plugin)
    _validated_server_id(server_id)
    return (
        f"{PLUGINS_DIRNAME}/{plugin}/{BINDINGS_DIRNAME}/{server_id}.meta.json"
    )


def _fsync_directory(directory: Path) -> None:
    """Persist a directory entry change (rename/unlink durability)."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _contains_alias(root: yaml.Node) -> bool:
    """Detect YAML alias references before any construction (local copy).

    Composed aliases reuse the anchor's node object, so node-identity
    reuse in the composed tree proves an alias reference. Iterative
    walk — no recursion on hostile documents.
    """
    seen: set[int] = set()
    stack: list[yaml.Node] = [root]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            return True
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                stack.append(key_node)
                stack.append(value_node)
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
    return False


def _checked_document(text: str) -> bytes:
    """Encode and harden one candidate canonical document before parsing."""
    data = text.encode("utf-8")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise PluginConfigError(
            PluginConfigErrorCode.DOCUMENT_TOO_LARGE,
            f"document is {len(data)} bytes, limit is {MAX_DOCUMENT_BYTES}",
        )
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return data
    except RecursionError:
        raise PluginConfigError(
            PluginConfigErrorCode.DOCUMENT_TOO_DEEP,
            "document nesting exceeds the supported depth",
        ) from None
    if node is not None and _contains_alias(node):
        raise PluginConfigError(
            PluginConfigErrorCode.DOCUMENT_ALIAS_FORBIDDEN,
            "document contains YAML alias nodes; anchor/alias expansion is "
            "not permitted in canonical files",
        )
    return data


@dataclass(frozen=True, slots=True)
class _Target:
    """One confined file pair (canonical document + metadata sidecar)."""

    plugin: str
    server_id: uuid.UUID | None
    document: Path
    meta: Path
    parent: Path


class PluginConfigStore:
    """Confined, revision-checked, atomic plugin configuration storage (C02).

    Single-writer per ``(plugin, server_id)`` is a caller invariant; the
    store guarantees atomic visibility of each committed file pair
    member, stale-caller rejection and root confinement. Canonical
    document first, metadata second (C02 W2): a crash in between leaves
    the new file with lagging metadata, which reads report as
    ``hash_mismatch`` — honest detection, never invented repair.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve(strict=False)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)

    @property
    def root(self) -> Path:
        """Resolved data root (C02 rule 1: explicitly configured entry point)."""
        return self._root

    # -- plugin connection configs -----------------------------------------

    def read_config(self, plugin: str) -> StoredPluginConfig:
        """Return the stored config, hash-verified against metadata."""
        plugin = _validated_plugin_name(plugin)
        target = self._config_target(plugin)
        if not target.document.exists():
            raise PluginConfigError(
                PluginConfigErrorCode.NOT_FOUND,
                f"no canonical config stored for plugin {plugin!r}",
            )
        meta = self._load_meta(target)
        data = self._read_verified(target.document, meta)
        return StoredPluginConfig(
            plugin=plugin,
            text=data.decode("utf-8"),
            revision=meta["revision"],
            schema_revision=meta["schema_revision"],
            sha256=meta["sha256"],
        )

    def write_config(
        self,
        plugin: str,
        text: str,
        *,
        expected_revision: int,
        config_schema: Mapping[str, object] | None,
    ) -> StoredPluginConfig:
        """Validate then atomically replace the canonical plugin config.

        ``expected_revision`` is the revision the caller last observed
        (``0`` to create); success stores ``expected + 1``. A
        schema-invalid document raises before any filesystem effect.
        ``config_schema`` is the plugin descriptor's declared
        ``config_schema`` (``None`` when the plugin declares none) —
        required explicitly so stored values can never skip validation.
        """
        plugin = _validated_plugin_name(plugin)
        _validated_expected_revision(expected_revision)
        data = _checked_document(text)
        document = parse_plugin_config_text(text)
        if document.plugin != plugin:
            raise PluginConfigError(
                PluginConfigErrorCode.PLUGIN_MISMATCH,
                f"embedded plugin {document.plugin!r} does not match the "
                f"storage identity {plugin!r}",
            )
        _checked_config_values(document.config, config_schema)
        target = self._config_target(plugin)
        current = self._current_revision(target, tolerate_orphan=False)
        if current != expected_revision:
            raise PluginConfigError(
                PluginConfigErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        self._ensure_dir(target.parent)
        self._remove_stale_temps(
            target.parent,
            prefixes=(f"{PLUGIN_CONFIG_FILENAME}.", f"{PLUGIN_META_FILENAME}."),
        )
        digest = hashlib.sha256(data).hexdigest()
        revision = current + 1
        # C02 W1: crash before this replace leaves the previous pair intact.
        self._atomic_write(target.document, data)
        # C02 W2: canonical first, metadata second — the file is
        # authoritative; divergence is detected on read, never repaired.
        self._atomic_write(
            target.meta,
            self._meta_bytes(
                kind="config",
                plugin=plugin,
                server_id=None,
                revision=revision,
                digest=digest,
            ),
        )
        return StoredPluginConfig(
            plugin=plugin,
            text=text,
            revision=revision,
            schema_revision=SCHEMA_VERSION,
            sha256=digest,
        )

    def delete_config(self, plugin: str, *, expected_revision: int) -> None:
        """Revision-checked removal of the plugin connection config.

        Refused while grant bindings still reference the plugin: bindings
        are removed first, so no binding can outlive its config through
        this API (AC2 — a deleted plugin cannot leave active bindings).
        """
        plugin = _validated_plugin_name(plugin)
        _validated_expected_revision(expected_revision)
        if self.list_bindings(plugin):
            raise PluginConfigError(
                PluginConfigErrorCode.BINDINGS_PRESENT,
                f"plugin {plugin!r} still has server grant bindings; "
                "delete the bindings first",
            )
        target = self._config_target(plugin)
        current = self._current_revision(target, tolerate_orphan=True)
        if current == ABSENT_REVISION:
            raise PluginConfigError(
                PluginConfigErrorCode.NOT_FOUND,
                f"nothing stored for plugin {plugin!r}",
            )
        if current != expected_revision:
            raise PluginConfigError(
                PluginConfigErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        self._remove_stale_temps(
            target.parent,
            prefixes=(f"{PLUGIN_CONFIG_FILENAME}.", f"{PLUGIN_META_FILENAME}."),
        )
        target.document.unlink(missing_ok=True)
        target.meta.unlink(missing_ok=True)
        _fsync_directory(target.parent)

    def list_plugins(self) -> tuple[str, ...]:
        """Configured plugin names, sorted.

        Candidates come from the ``plugins/`` namespace: only name-shaped
        directory entries are addressable by this store (every other name
        is rejected by the API), so only those are listed. Content
        problems (corrupt documents, hash mismatch) surface on
        :meth:`read_config`, which every view calls.
        """
        plugins_dir = self._root / PLUGINS_DIRNAME
        if not plugins_dir.is_dir():
            return ()
        found = []
        for entry in plugins_dir.iterdir():
            if not entry.is_dir() or entry.is_symlink():
                continue
            if not PLUGIN_NAME_RE.fullmatch(entry.name):
                continue
            found.append(entry.name)
        return tuple(sorted(found))

    # -- server grant bindings ----------------------------------------------

    def read_binding(self, plugin: str, server_id: uuid.UUID) -> StoredGrantBinding:
        """Return the stored binding, hash-verified against metadata."""
        plugin = _validated_plugin_name(plugin)
        server_id = _validated_server_id(server_id)
        target = self._binding_target(plugin, server_id)
        if not target.document.exists():
            raise PluginConfigError(
                PluginConfigErrorCode.NOT_FOUND,
                f"no grant binding stored for plugin {plugin!r}, "
                f"server {server_id}",
            )
        meta = self._load_meta(target)
        data = self._read_verified(target.document, meta)
        return StoredGrantBinding(
            plugin=plugin,
            server_id=server_id,
            text=data.decode("utf-8"),
            revision=meta["revision"],
            schema_revision=meta["schema_revision"],
            sha256=meta["sha256"],
        )

    def write_binding(
        self,
        plugin: str,
        server_id: uuid.UUID,
        text: str,
        *,
        expected_revision: int,
    ) -> StoredGrantBinding:
        """Validate then atomically replace one server grant binding.

        Requires the plugin's connection config to exist (identity-first:
        a grant binding without a stored connection config is not
        representable). Writing a config never creates a binding —
        grants are explicit admin actions only (no installation-time
        auto-grant, SERVICES decision-dependency row).
        """
        plugin = _validated_plugin_name(plugin)
        server_id = _validated_server_id(server_id)
        _validated_expected_revision(expected_revision)
        data = _checked_document(text)
        document = parse_grant_binding_text(text)
        if document.plugin != plugin:
            raise PluginConfigError(
                PluginConfigErrorCode.PLUGIN_MISMATCH,
                f"embedded plugin {document.plugin!r} does not match the "
                f"storage identity {plugin!r}",
            )
        if document.server_id != server_id:
            raise PluginConfigError(
                PluginConfigErrorCode.SERVER_ID_MISMATCH,
                f"embedded server_id does not match the storage identity",
            )
        config_target = self._config_target(plugin)
        if self._current_revision(config_target, tolerate_orphan=False) == (
            ABSENT_REVISION
        ):
            raise PluginConfigError(
                PluginConfigErrorCode.NOT_FOUND,
                f"no plugin config stored for {plugin!r}; store the "
                "connection config before grant bindings",
            )
        target = self._binding_target(plugin, server_id)
        current = self._current_revision(target, tolerate_orphan=False)
        if current != expected_revision:
            raise PluginConfigError(
                PluginConfigErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        self._ensure_dir(target.parent)
        self._remove_stale_temps(
            target.parent,
            prefixes=(f"{server_id}.yaml.", f"{server_id}.meta.json."),
        )
        digest = hashlib.sha256(data).hexdigest()
        revision = current + 1
        self._atomic_write(target.document, data)
        self._atomic_write(
            target.meta,
            self._meta_bytes(
                kind="binding",
                plugin=plugin,
                server_id=server_id,
                revision=revision,
                digest=digest,
            ),
        )
        return StoredGrantBinding(
            plugin=plugin,
            server_id=server_id,
            text=text,
            revision=revision,
            schema_revision=SCHEMA_VERSION,
            sha256=digest,
        )

    def delete_binding(
        self, plugin: str, server_id: uuid.UUID, *, expected_revision: int
    ) -> None:
        """Revision-checked removal of one server grant binding."""
        plugin = _validated_plugin_name(plugin)
        server_id = _validated_server_id(server_id)
        _validated_expected_revision(expected_revision)
        target = self._binding_target(plugin, server_id)
        current = self._current_revision(target, tolerate_orphan=True)
        if current == ABSENT_REVISION:
            raise PluginConfigError(
                PluginConfigErrorCode.NOT_FOUND,
                f"no grant binding stored for plugin {plugin!r}, "
                f"server {server_id}",
            )
        if current != expected_revision:
            raise PluginConfigError(
                PluginConfigErrorCode.STALE_REVISION,
                f"expected revision {expected_revision}, "
                f"current revision is {current}",
            )
        self._remove_stale_temps(
            target.parent,
            prefixes=(f"{server_id}.yaml.", f"{server_id}.meta.json."),
        )
        target.document.unlink(missing_ok=True)
        target.meta.unlink(missing_ok=True)
        _fsync_directory(target.parent)

    def list_bindings(self, plugin: str) -> tuple[GrantBindingDocument, ...]:
        """Stored bindings of one plugin, sorted by server id.

        Only UUID-named binding files are addressable (every other name
        is rejected by the API), so only those are listed; content
        problems surface through :meth:`read_binding` on the affected
        entry.
        """
        plugin = _validated_plugin_name(plugin)
        bindings_dir = self._root / PLUGINS_DIRNAME / plugin / BINDINGS_DIRNAME
        if not bindings_dir.is_dir():
            return ()
        found: list[GrantBindingDocument] = []
        for entry in bindings_dir.glob("*.yaml"):
            if entry.is_symlink() or not entry.is_file():
                continue
            try:
                server_id = uuid.UUID(entry.name[: -len(".yaml")])
            except ValueError:
                continue
            found.append(
                parse_grant_binding_text(self.read_binding(plugin, server_id).text)
            )
        return tuple(sorted(found, key=lambda doc: (doc.plugin, doc.server_id)))

    # -- derived views -------------------------------------------------------

    def disabled_plugins(self) -> tuple[str, ...]:
        """Plugins stored with ``enabled: false`` (sorted).

        The worker-restart composition input:
        :func:`compose_runtime` passes this set to the native runtime,
        whose load-time disable skips these plugins before any import.
        """
        disabled = []
        for plugin in self.list_plugins():
            stored = self.read_config(plugin)
            if not parse_plugin_config_text(stored.text).enabled:
                disabled.append(plugin)
        return tuple(disabled)

    def server_grants(self, server_id: uuid.UUID) -> tuple[str, ...]:
        """Capability identifiers granted to one server, sorted and unique.

        Aggregated across all configured plugins' bindings. This is the
        grant half of exposure; availability (registered descriptors) is
        the other half, intersected in :func:`server_exposure`.
        """
        server_id = _validated_server_id(server_id)
        granted: set[str] = set()
        for plugin in self.list_plugins():
            for document in self.list_bindings(plugin):
                if document.server_id == server_id:
                    granted.update(document.capabilities)
        return tuple(sorted(granted))

    # -- confinement and state helpers ---------------------------------------

    def _confined(self, relative: str) -> Path:
        """Reject symlinked components; containment is structural.

        Local copy of the profile store's confinement: plugin names and
        UUIDs are shape-validated (no separators, no leading dot), so the
        resolved path cannot leave the root; the per-component symlink
        check additionally refuses planted links.
        """
        current = self._root
        for part in PurePosixPath(relative).parts:
            current /= part
            if current.is_symlink():
                raise PluginConfigError(
                    PluginConfigErrorCode.PATH_ESCAPE,
                    f"path component {part!r} below the data root is a "
                    "symbolic link",
                )
        resolved = current.resolve(strict=False)
        if not resolved.is_relative_to(self._root):
            raise PluginConfigError(
                PluginConfigErrorCode.PATH_ESCAPE,
                "resolved path left the data root",
            )
        return current

    def _config_target(self, plugin: str) -> _Target:
        document = self._confined(canonical_config_relative_path(plugin))
        meta = self._confined(canonical_meta_relative_path(plugin))
        return _Target(
            plugin=plugin,
            server_id=None,
            document=document,
            meta=meta,
            parent=document.parent,
        )

    def _binding_target(self, plugin: str, server_id: uuid.UUID) -> _Target:
        document = self._confined(canonical_binding_relative_path(plugin, server_id))
        meta = self._confined(
            canonical_binding_meta_relative_path(plugin, server_id)
        )
        return _Target(
            plugin=plugin,
            server_id=server_id,
            document=document,
            meta=meta,
            parent=document.parent,
        )

    def _load_meta(self, target: _Target) -> dict[str, object]:
        identity = self._meta_identity(target)
        try:
            raw = target.meta.read_bytes()
        except FileNotFoundError:
            raise PluginConfigError(
                PluginConfigErrorCode.METADATA_MISSING,
                "canonical document exists without revision metadata; "
                "recovery must reconcile before further writes",
            ) from None
        try:
            meta = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise PluginConfigError(
                PluginConfigErrorCode.METADATA_INVALID,
                "revision metadata is not a valid JSON document",
            ) from None
        expected = {
            "format": _META_FORMAT,
            "kind": "binding" if target.server_id is not None else "config",
            **identity,
        }
        if (
            not isinstance(meta, dict)
            or not _is_positive_int(meta.get("revision"))
            or not _is_positive_int(meta.get("schema_revision"))
            or not _is_canonical_sha256(meta.get("sha256"))
        ):
            raise PluginConfigError(
                PluginConfigErrorCode.METADATA_INVALID,
                "revision metadata does not match the expected shape",
            )
        for key, value in expected.items():
            if meta.get(key) != value:
                raise PluginConfigError(
                    PluginConfigErrorCode.METADATA_INVALID,
                    f"revision metadata field {key!r} does not match the "
                    "storage identity",
                )
        return meta

    def _read_verified(self, path: Path, meta: dict[str, object]) -> bytes:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            raise PluginConfigError(
                PluginConfigErrorCode.NOT_FOUND,
                "canonical document is missing; recovery must reconcile "
                "before further writes",
            ) from None
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != meta["sha256"]:
            raise PluginConfigError(
                PluginConfigErrorCode.HASH_MISMATCH,
                "canonical content hash differs from revision metadata "
                "(interrupted commit or external modification); recovery "
                "must reconcile from the file hash",
            )
        return data

    def _current_revision(self, target: _Target, *, tolerate_orphan: bool) -> int:
        """Pairing-aware current revision; ``ABSENT_REVISION`` when both absent."""
        has_file = target.document.exists()
        has_meta = target.meta.exists()
        if not has_file and not has_meta:
            return ABSENT_REVISION
        if has_file and not has_meta:
            raise PluginConfigError(
                PluginConfigErrorCode.METADATA_MISSING,
                "canonical document exists without revision metadata; "
                "recovery must reconcile before further writes",
            )
        meta = self._load_meta(target)
        if not has_file and not tolerate_orphan:
            raise PluginConfigError(
                PluginConfigErrorCode.ORPHAN_METADATA,
                "revision metadata exists without a canonical document; "
                "complete the delete before writing",
            )
        return int(meta["revision"])

    @staticmethod
    def _meta_identity(target: _Target) -> dict[str, str]:
        identity = {"plugin": target.plugin}
        if target.server_id is not None:
            identity["server_id"] = str(target.server_id)
        return identity

    @staticmethod
    def _ensure_dir(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory.parent, 0o700)
        os.chmod(directory, 0o700)

    @staticmethod
    def _meta_bytes(
        *,
        kind: str,
        plugin: str,
        server_id: uuid.UUID | None,
        revision: int,
        digest: str,
    ) -> bytes:
        payload: dict[str, object] = {
            "format": _META_FORMAT,
            "kind": kind,
            "plugin": plugin,
            "revision": revision,
            "schema_revision": SCHEMA_VERSION,
            "sha256": digest,
        }
        if server_id is not None:
            payload["server_id"] = str(server_id)
        return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """Write ``data`` to ``path`` via temp file + fsync + atomic replace."""
        directory = path.parent
        fd, temp_name = tempfile.mkstemp(
            dir=directory, prefix=f"{path.name}.", suffix=".tmp"
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        _fsync_directory(directory)

    @staticmethod
    def _remove_stale_temps(directory: Path, *, prefixes: tuple[str, ...]) -> None:
        """Best-effort cleanup of this store's crash-orphaned temp files."""
        if not directory.is_dir():
            return
        for stale in directory.glob("*.tmp"):
            if stale.name.startswith(prefixes):
                with contextlib.suppress(OSError):
                    stale.unlink()


# ---------------------------------------------------------------------------
# Composition views (worker restart, exposure, stale-binding surface)
# ---------------------------------------------------------------------------


def compose_runtime(
    store: PluginConfigStore, *, registry: CapabilityRegistry
) -> NativePluginRuntime:
    """Compose the worker's plugin runtime from stored configuration.

    The restart path: the stored ``enabled: false`` set becomes the
    native runtime's load-time disable set (skipped before any import);
    every other installed plugin loads. The caller invokes
    :meth:`NativePluginRuntime.discover` — composition is constructor-
    checked, discovery is the explicit load event.
    """
    if not isinstance(registry, CapabilityRegistry):
        raise PluginConfigError(
            PluginConfigErrorCode.INVALID_WIRING,
            "registry must be a CapabilityRegistry",
        )
    if not isinstance(store, PluginConfigStore):
        raise PluginConfigError(
            PluginConfigErrorCode.INVALID_WIRING,
            "store must be a PluginConfigStore",
        )
    return NativePluginRuntime(registry=registry, disabled=store.disabled_plugins())


def server_exposure(
    store: PluginConfigStore, server_id: uuid.UUID, registry: CapabilityRegistry
) -> tuple[str, ...]:
    """Capability identifiers exposed to one server: grants ∩ registered.

    The intersection is the safety property (C03 rule 3: effective
    grants are an intersection/subset, never a union): a binding for a
    plugin that is uninstalled, disabled (not registered at load time)
    or updated away from a capability contributes nothing — a deleted
    plugin cannot keep a tool binding silently usable (AC2).
    """
    if not isinstance(registry, CapabilityRegistry):
        raise PluginConfigError(
            PluginConfigErrorCode.INVALID_WIRING,
            "registry must be a CapabilityRegistry",
        )
    registered = {
        descriptor.identifier for descriptor in registry.list_capabilities()
    }
    return tuple(
        identifier
        for identifier in store.server_grants(server_id)
        if identifier in registered
    )


def unbacked_bindings(
    store: PluginConfigStore, registry: CapabilityRegistry
) -> tuple[UnbackedBinding, ...]:
    """Stored bindings whose granted capabilities are not registered.

    The surfacing half of deleted-plugin safety (C02 W4 discipline):
    stale references are reported to the admin, never silently honored
    and never silently dropped. Reasons are intentionally not
    distinguished — uninstalled, disabled and capability-removed all
    mean "granted but not currently backed by a registered descriptor";
    the store cannot and must not guess admin intent.
    """
    if not isinstance(registry, CapabilityRegistry):
        raise PluginConfigError(
            PluginConfigErrorCode.INVALID_WIRING,
            "registry must be a CapabilityRegistry",
        )
    registered = {
        descriptor.identifier for descriptor in registry.list_capabilities()
    }
    found: list[UnbackedBinding] = []
    for plugin in store.list_plugins():
        for document in store.list_bindings(plugin):
            missing = tuple(
                identifier
                for identifier in document.capabilities
                if identifier not in registered
            )
            if missing:
                found.append(
                    UnbackedBinding(
                        plugin=document.plugin,
                        server_id=document.server_id,
                        missing_capabilities=missing,
                    )
                )
    return tuple(found)


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mai.plugins.config",
        description=(
            "Declarative inspection of the plugin/MCP configuration store "
            "(contracts.md C02/C03). Output is value-free: plugin names, "
            "credential reference names, server ids, capability "
            "identifiers, revisions — never configuration values."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser(
        "inspect", help="list stored plugin configs and grant bindings"
    )
    inspect.add_argument("root", help="plugin configuration data root")

    compose = subparsers.add_parser(
        "compose",
        help="worker-restart view: compose the runtime and report exposure",
    )
    compose.add_argument("root", help="plugin configuration data root")
    return parser


def _inspect_payload(root: Path) -> dict[str, object]:
    store = PluginConfigStore(root)
    plugins: list[dict[str, object]] = []
    bindings: list[dict[str, object]] = []
    for plugin in store.list_plugins():
        stored = store.read_config(plugin)
        document = parse_plugin_config_text(stored.text)
        plugins.append(
            {
                "plugin": plugin,
                "enabled": document.enabled,
                "credential_refs": list(document.credential_refs),
                "revision": stored.revision,
                "schema_revision": stored.schema_revision,
            }
        )
        for document_binding in store.list_bindings(plugin):
            binding = store.read_binding(plugin, document_binding.server_id)
            bindings.append(
                {
                    "plugin": plugin,
                    "server_id": str(document_binding.server_id),
                    "capabilities": list(document_binding.capabilities),
                    "revision": binding.revision,
                    "schema_revision": binding.schema_revision,
                }
            )
    return {
        "root": str(store.root),
        "plugins": plugins,
        "bindings": bindings,
        "disabled": list(store.disabled_plugins()),
    }


def _compose_payload(root: Path) -> dict[str, object]:
    store = PluginConfigStore(root)
    registry = CapabilityRegistry()
    runtime = compose_runtime(store, registry=registry)
    runtime.discover()
    server_ids: list[uuid.UUID] = []
    for plugin in store.list_plugins():
        for document in store.list_bindings(plugin):
            if document.server_id not in server_ids:
                server_ids.append(document.server_id)
    exposure = {
        str(server_id): list(server_exposure(store, server_id, registry))
        for server_id in sorted(server_ids, key=str)
    }
    return {
        "root": str(store.root),
        "disabled": list(store.disabled_plugins()),
        "plugins": [
            {
                "plugin": loaded.plugin,
                "identifier": loaded.descriptor_name,
                "version": loaded.version,
                "effect_class": loaded.effect_class,
                "credential_references": list(loaded.credential_references),
            }
            for loaded in runtime.plugins
        ],
        "skipped_disabled": list(runtime.skipped_disabled),
        "rejections": [
            {
                "plugin": rejection.plugin,
                "code": rejection.code,
                "detail": rejection.detail,
            }
            for rejection in runtime.rejections
        ],
        "exposure": exposure,
        "unbacked": [
            {
                "plugin": stale.plugin,
                "server_id": str(stale.server_id),
                "missing_capabilities": list(stale.missing_capabilities),
            }
            for stale in unbacked_bindings(store, registry)
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: 0 success, 1 structured store rejection, 2 usage/IO."""
    parser = _build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    root = Path(args.root)
    if not root.is_dir():
        print(f"IO-ERROR {args.root}: data root does not exist", file=sys.stderr)
        return 2
    try:
        payload = (
            _inspect_payload(root)
            if args.command == "inspect"
            else _compose_payload(root)
        )
    except PluginConfigError as error:
        print(
            json.dumps({"error": {"code": error.code.value, "detail": error.detail}})
        )
        return 1
    _emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
