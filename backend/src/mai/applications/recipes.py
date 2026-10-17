"""Canonical application recipe schema and policy-bound deployment plans.

Card ``08-recipe-schema``; sibling of ``mai.applications.adopt`` (explicit
adoption) and ``mai.applications.discover`` (observed records). The recipe is
the maintained-catalogue document that declares HOW an application is
deployed, verified, updated, removed and recovered on an enrolled target;
adoption records THAT an application exists. Contracts.md:

- **C09 rule 2 (frozen JSON shape)** — this module is the frozen producer of
  the recipe shape: ``supported_environment``, deploy paths (pinned image,
  published ports, persistent-data container locations), ``prerequisites``,
  ``required_grants`` (C03), ``health`` verification, and the
  ``update``/``removal``/``recovery``/``backup`` semantics sections. The
  shape is versioned (:data:`RECIPE_SCHEMA_VERSION` = 1); unknown fields and
  duplicate keys are rejected, never ignored (windowed ADR §8 rule 2 applied
  to canonical data).
- **C09 rule 3 (dry-run/plan)** — :func:`build_deployment_plan` turns a
  validated recipe plus explicit target identities into a
  :class:`DeploymentPlan`: a pure, deterministic description of the ACTUAL
  intended resources (exact target paths, exact pinned image, exact ports,
  exact rendered Compose bytes and their hash, exact operation list) — never
  a generic "will install". The executor applies only the plan's frozen
  operation vocabulary (§"Plan operations"); this module PERFORMS nothing.
- **Acceptance 2 (no hidden shell)** — the schema has NO field that carries a
  command string or script: health verification is an HTTP probe (kind,
  port, path, expected status, timing bounds) rendered as an argv-form
  ``CMD`` healthcheck, prerequisites/grants/credential names are strict
  tokens, and every other executable-sounding field is an unknown field and
  rejected. A recipe can never smuggle shell through the declarative
  surface.
- **C03 rule 2 / rule 6 (credentials)** — the recipe declares credential
  REFERENCES (environment-variable NAMES only). There is no field that
  could carry a secret value; values resolve at execution time from the
  configured resolver (``.env`` first). The plan binds the target path of
  the credentials file and its reference names — never any value.
- **C09 rule 4/5 (update/removal semantics)** — ``update.migrations``
  declares whether schema migrations are none/reversible/irreversible
  (irreversible data migration is a declared limitation, never hidden
  behind a universal rollback checkbox); ``recovery.image_rollback`` states
  whether the previous pinned digest remains a recovery path. Removal data
  policy is ``explicit_choice`` (the keep-or-delete decision is made
  explicitly at removal time and absent intent refuses deletion) or ``keep``
  (no data-deletion path at all); the value ``delete`` is rejected with
  :attr:`RecipeErrorCode.DATA_DELETE_FORBIDDEN` — a recipe can never
  pre-authorize data deletion.
- **C09 rule 1 boundary** — recipes are inert documents: nothing here reads
  or writes files, talks to targets or databases, or mints grants. Plans
  are policy-bound by construction: they carry the exact recipe revision
  hash (:func:`recipe_sha256`), the required grants (rechecked by the
  dispatcher per C03 rule 3) and per-operation bound resources.

Determinism: both renderers (:func:`render_recipe`, :func:`render_compose`)
emit sorted-key block YAML and every collection is order-normalized at
parse time, so identical validated inputs always produce identical bytes
(and therefore identical hashes). :class:`DeploymentPlan.plan_sha256` is the
SHA-256 over a canonical JSON projection of the whole plan.

Target-side path scheme (frozen by this module, C02 rule 3 pattern): every
managed-application path derives from the opaque
:class:`uuid.UUID` ``application_id`` alone below
:data:`TARGET_APPS_ROOT` —
``/var/lib/mai/apps/<application_id>/compose.yaml``,
``.../credentials.env`` and ``.../volumes/<volume_name>`` — so traversal
and user-supplied absolute host paths are unrepresentable by construction.
Volume declarations carry the CONTAINER path only; the host path is
allocated by the planner, never typed by the recipe author.

Pure module: no I/O, no clock, no environment reads, no module state. The
Compose rendering targets the Compose v2 spec (no ``version:`` key, argv
healthcheck, project ``name`` field); the rendered file deliberately
references the sibling ``credentials.env`` via ``env_file`` ONLY when the
recipe declares credential references, and Compose then fails closed if the
executor did not provision it.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, replace
from enum import StrEnum

import yaml
from yaml.nodes import MappingNode, Node, SequenceNode

__all__ = [
    "HEALTH_KIND_VOCABULARY",
    "MAX_DOCUMENT_BYTES",
    "OP_CREATE_DIRECTORIES",
    "OP_PULL_IMAGE",
    "OP_REQUIRE_BACKUP",
    "OP_START",
    "OP_VERIFY_PREREQUISITES",
    "OP_WAIT_HEALTHY",
    "OP_WRITE_COMPOSE",
    "OP_WRITE_CREDENTIALS",
    "PROTOCOL_VOCABULARY",
    "RECIPE_SCHEMA_VERSION",
    "REMOVAL_DATA_POLICY_VOCABULARY",
    "SUPPORTED_ARCH_VOCABULARY",
    "SUPPORTED_OS_VOCABULARY",
    "TARGET_APPS_ROOT",
    "TARGET_COMPOSE_FILENAME",
    "TARGET_CREDENTIALS_FILENAME",
    "UPDATE_MIGRATIONS_VOCABULARY",
    "ApplicationRecipe",
    "BackupSemantics",
    "DeploymentPlan",
    "HealthCheck",
    "HealthProbe",
    "PlanOperation",
    "PlannedVolume",
    "PortBinding",
    "RecipeError",
    "RecipeErrorCode",
    "RecoverySemantics",
    "RemovalDataPolicy",
    "RemovalSemantics",
    "SupportedEnvironment",
    "TargetEnvironment",
    "UpdateMigrations",
    "UpdateSemantics",
    "Volume",
    "build_deployment_plan",
    "canonical_plan_json",
    "parse_recipe_text",
    "recipe_sha256",
    "render_compose",
    "render_recipe",
]

# ---------------------------------------------------------------------------
# Frozen constants (shape version, vocabularies, target path scheme)
# ---------------------------------------------------------------------------

RECIPE_SCHEMA_VERSION = 1

#: Byte cap for one recipe document, enforced before parsing (same
#: resource-exhaustion boundary as the adoption/profile canonical files).
MAX_DOCUMENT_BYTES = 64 * 1024

#: Supported-environment vocabulary (v1 targets are Linux; Windows is a
#: frozen non-goal). A recipe cannot even DECLARE other values.
SUPPORTED_OS_VOCABULARY = ("linux",)
SUPPORTED_ARCH_VOCABULARY = ("x86_64", "aarch64")

HEALTH_KIND_VOCABULARY = ("http",)
PROTOCOL_VOCABULARY = ("tcp", "udp")

#: Declared migration classes (C09 rule 4: irreversibility is declared, not
#: discovered at update time; no universal rollback checkbox).
UPDATE_MIGRATIONS_VOCABULARY = ("none", "reversible", "irreversible")

#: Removal data policies (C09 rule 5: the keep/delete decision belongs to
#: the explicit removal action; a recipe pins "explicit" or "keep" only —
#: ``delete`` is rejected at parse time).
REMOVAL_DATA_POLICY_VOCABULARY = ("explicit_choice", "keep")

#: Target-side root for controller-managed applications (frozen scheme; all
#: paths below derive from the opaque application_id UUID alone).
TARGET_APPS_ROOT = "/var/lib/mai/apps"
TARGET_COMPOSE_FILENAME = "compose.yaml"
TARGET_CREDENTIALS_FILENAME = "credentials.env"

# Frozen plan-operation vocabulary (C09 rule 3: the executor applies ONLY
# these operations, each bound to the resources recorded in the plan).
OP_VERIFY_PREREQUISITES = "verify_prerequisites"
OP_REQUIRE_BACKUP = "require_backup"
OP_CREATE_DIRECTORIES = "create_directories"
OP_WRITE_COMPOSE = "write_compose"
OP_WRITE_CREDENTIALS = "write_credentials"
OP_PULL_IMAGE = "pull_image"
OP_START = "start"
OP_WAIT_HEALTHY = "wait_healthy"

# Bounded field limits (record hygiene; adopt.py precedents).
_NAME_MAX = 64
_PREREQ_MAX = 64
_GRANT_MAX = 64
_CRED_MAX = 64
_LIST_MAX = 64
_PATH_MAX = 1024
_HEALTH_PATH_MAX = 512

# Healthcheck timing bounds (seconds; fixed argv probe, no shell).
_INTERVAL_BOUNDS = (5, 3600)
_TIMEOUT_BOUNDS = (1, 60)
_RETRIES_BOUNDS = (1, 30)
_START_PERIOD_BOUNDS = (0, 3600)
_EXPECTED_STATUS_BOUNDS = (200, 399)

_PORT_MIN_CONTAINER, _PORT_MAX = 1, 65535
_PORT_MIN_HOST = 1024  # unprivileged published ports only

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_VOLUME_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_CREDENTIAL_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_PREREQUISITE_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}$")
_GRANT_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
#: Pinned reference: optional registry host[:port]/, repository, and a
#: REQUIRED sha256 digest. A tag is not a pin (`name:tag` fails the regex
#: and is reported as ``unpinned_image``).
_IMAGE_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?"  # registry
    r"/)?[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$"  # repo + digest
)

#: Fixed curl argv for the rendered Compose healthcheck (CMD form — the
#: container runs argv, never a shell string).
_HEALTHCHECK_CURL_ARGV_PREFIX = (
    "CMD",
    "curl",
    "--fail",
    "--silent",
    "--show-error",
    "--output",
    "/dev/null",
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "supported_environment",
        "prerequisites",
        "required_grants",
        "deploy",
        "health",
        "update",
        "removal",
        "recovery",
        "backup",
    }
)
_REQUIRED_TOP_LEVEL_FIELDS = (
    "schema_version",
    "name",
    "supported_environment",
    "deploy",
    "update",
    "removal",
    "recovery",
    "backup",
)
_ENVIRONMENT_FIELDS = frozenset({"os", "arch"})
_DEPLOY_FIELDS = frozenset({"image", "ports", "volumes", "credentials"})
_PORT_FIELDS = frozenset({"host", "container", "protocol"})
_VOLUME_FIELDS = frozenset({"name", "container_path"})
_HEALTH_FIELDS = frozenset(
    {
        "kind",
        "port",
        "path",
        "expected_status",
        "interval_seconds",
        "timeout_seconds",
        "retries",
        "start_period_seconds",
    }
)
_UPDATE_FIELDS = frozenset({"migrations"})
_REMOVAL_FIELDS = frozenset({"data_policy"})
_RECOVERY_FIELDS = frozenset({"image_rollback"})
_BACKUP_FIELDS = frozenset({"required"})


class RecipeErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    # document-level
    UNPARSABLE_DOCUMENT = "unparsable_document"
    DOCUMENT_TOO_LARGE = "document_too_large"
    DOCUMENT_TOO_DEEP = "document_too_deep"
    DOCUMENT_ALIAS_FORBIDDEN = "document_alias_forbidden"
    SCHEMA_VERSION_UNSUPPORTED = "schema_version_unsupported"
    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    INVALID_FIELD = "invalid_field"
    # field-level
    INVALID_NAME = "invalid_name"
    UNSUPPORTED_OS = "unsupported_os"
    UNSUPPORTED_ARCH = "unsupported_arch"
    UNSUPPORTED_HEALTH_KIND = "unsupported_health_kind"
    INVALID_IMAGE = "invalid_image"
    UNPINNED_IMAGE = "unpinned_image"
    INVALID_PORT = "invalid_port"
    DUPLICATE_PORT = "duplicate_port"
    INVALID_VOLUME = "invalid_volume"
    INVALID_VOLUME_PATH = "invalid_volume_path"
    DUPLICATE_VOLUME = "duplicate_volume"
    INVALID_CREDENTIAL_REFERENCE = "invalid_credential_reference"
    DUPLICATE_CREDENTIAL_REFERENCE = "duplicate_credential_reference"
    INVALID_PREREQUISITE = "invalid_prerequisite"
    INVALID_GRANT = "invalid_grant"
    HEALTH_PORT_UNBOUND = "health_port_unbound"
    INVALID_HEALTH_CHECK = "invalid_health_check"
    INVALID_UPDATE_SEMANTICS = "invalid_update_semantics"
    INVALID_REMOVAL_SEMANTICS = "invalid_removal_semantics"
    DATA_DELETE_FORBIDDEN = "data_delete_forbidden"
    # plan-level
    INVALID_SERVER_ID = "invalid_server_id"
    INVALID_APPLICATION_ID = "invalid_application_id"
    INVALID_TARGET_ENVIRONMENT = "invalid_target_environment"
    UNSUPPORTED_TARGET_OS = "unsupported_target_os"
    UNSUPPORTED_TARGET_ARCH = "unsupported_target_arch"


class RecipeError(ValueError):
    """One structured, value-free recipe rejection.

    ``detail`` never contains recipe field values (names, image refs, paths,
    credential names): transcripts and API bodies stay non-disclosing.
    """

    def __init__(self, code: RecipeErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Recipe records (validated output of :func:`parse_recipe_text`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SupportedEnvironment:
    """Declared target environments (C09 rule 2), order-normalized."""

    os: tuple[str, ...]
    arch: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PortBinding:
    """One published port: target host port -> container port."""

    host: int
    container: int
    protocol: str


@dataclass(frozen=True, slots=True)
class Volume:
    """One persistent-data location: NAME + CONTAINER path only.

    The target-side host path is allocated by the planner from the opaque
    application identity (C02 rule 3 pattern); it is never a recipe field.
    """

    name: str
    container_path: str


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """Declared HTTP health verification (no command field, no shell)."""

    port: int
    path: str
    expected_status: int = 200
    interval_seconds: int = 30
    timeout_seconds: int = 5
    retries: int = 3
    start_period_seconds: int = 30


@dataclass(frozen=True, slots=True)
class UpdateSemantics:
    """Declared update semantics (C09 rule 4)."""

    migrations: UpdateMigrations


@dataclass(frozen=True, slots=True)
class RemovalSemantics:
    """Declared removal data policy (C09 rule 5; ``delete`` unrepresentable)."""

    data_policy: RemovalDataPolicy


@dataclass(frozen=True, slots=True)
class RecoverySemantics:
    """Declared recovery limits: is the previous digest a recovery path."""

    image_rollback: bool


@dataclass(frozen=True, slots=True)
class BackupSemantics:
    """Declared backup requirement for data-affecting operations."""

    required: bool


@dataclass(frozen=True, slots=True)
class ApplicationRecipe:
    """One validated canonical application recipe (strict shape, v1)."""

    name: str
    supported_environment: SupportedEnvironment
    image: str
    ports: tuple[PortBinding, ...]
    volumes: tuple[Volume, ...]
    credentials: tuple[str, ...]
    prerequisites: tuple[str, ...]
    required_grants: tuple[str, ...]
    update: UpdateSemantics
    removal: RemovalSemantics
    recovery: RecoverySemantics
    backup: BackupSemantics
    health: HealthCheck | None


class UpdateMigrations(StrEnum):
    """Declared migration class (C09 rule 4)."""

    NONE = "none"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class RemovalDataPolicy(StrEnum):
    """Declared removal data policy (C09 rule 5)."""

    EXPLICIT_CHOICE = "explicit_choice"
    KEEP = "keep"


# ---------------------------------------------------------------------------
# Document hardening (adoption-store precedents)
# ---------------------------------------------------------------------------


class _StrictSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` that rejects duplicate and unhashable mapping keys."""

    def construct_mapping(self, node: MappingNode, deep: bool = False):  # type: ignore[override]
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


def _contains_alias(root: Node) -> bool:
    """Detect YAML alias references before any construction."""
    seen: set[int] = set()
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            return True
        seen.add(id(node))
        if isinstance(node, MappingNode):
            for key_node, value_node in node.value:
                stack.append(key_node)
                stack.append(value_node)
        elif isinstance(node, SequenceNode):
            stack.extend(node.value)
    return False


def _checked_document(text: str) -> None:
    """Byte cap + alias/depth hardening before any parse."""
    data = text.encode("utf-8")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise RecipeError(
            RecipeErrorCode.DOCUMENT_TOO_LARGE,
            f"recipe is {len(data)} bytes, limit is {MAX_DOCUMENT_BYTES}",
        )
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return
    except RecursionError:
        raise RecipeError(
            RecipeErrorCode.DOCUMENT_TOO_DEEP,
            "recipe nesting exceeds the supported depth",
        ) from None
    if node is not None and _contains_alias(node):
        raise RecipeError(
            RecipeErrorCode.DOCUMENT_ALIAS_FORBIDDEN,
            "recipe contains YAML alias nodes; anchor/alias expansion is "
            "not permitted in canonical files",
        )


# ---------------------------------------------------------------------------
# Small validators (value-free; reject, never coerce)
# ---------------------------------------------------------------------------


def _require_mapping(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RecipeError(RecipeErrorCode.INVALID_FIELD, f"{where} must be a mapping")
    return value


def _reject_unknown(
    mapping: dict[str, object], allowed: frozenset[str], where: str
) -> None:
    for key in mapping:
        if not isinstance(key, str) or key not in allowed:
            raise RecipeError(
                RecipeErrorCode.UNKNOWN_FIELD,
                f"unknown field in {where} (unknown fields are rejected, "
                "never ignored)",
            )


def _require_fields(
    mapping: dict[str, object], required: tuple[str, ...], where: str
) -> None:
    for key in required:
        if key not in mapping:
            raise RecipeError(
                RecipeErrorCode.MISSING_FIELD,
                f"missing required field {key!r} in {where}",
            )


def _validated_int(
    value: object, where: str, low: int, high: int, code: RecipeErrorCode
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RecipeError(code, f"{where} must be an integer")
    if not low <= value <= high:
        raise RecipeError(code, f"{where} must be in the range [{low}, {high}]")
    return value


def _validated_bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise RecipeError(RecipeErrorCode.INVALID_FIELD, f"{where} must be a boolean")
    return value


def _validated_token(
    value: object, where: str, pattern: re.Pattern[str], code: RecipeErrorCode
) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RecipeError(code, f"{where} does not match the required token shape")
    return value


def _validated_token_list(
    value: object,
    where: str,
    pattern: re.Pattern[str],
    code: RecipeErrorCode,
    duplicate_code: RecipeErrorCode,
    maximum: int,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RecipeError(code, f"{where} must be a list of tokens")
    if len(value) > maximum:
        raise RecipeError(code, f"{where} exceeds the entry limit")
    seen: set[str] = set()
    tokens: list[str] = []
    for entry in value:
        token = _validated_token(entry, f"{where} entry", pattern, code)
        if token in seen:
            raise RecipeError(duplicate_code, f"{where} contains a duplicate entry")
        seen.add(token)
        tokens.append(token)
    return tuple(sorted(tokens))


def _validated_container_path(value: object) -> str:
    """Absolute, normalized POSIX container path (record hygiene bounds).

    ``$`` is rejected outright: the path renders into the deployment
    Compose document, and Compose would interpolate ``$VAR``/``${VAR}``
    at apply time — the schema guarantees a literal, interpolation-free
    artifact (no-interpolation invariant).
    """
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value == "/"
        or value.startswith("//")
        or "$" in value
        or len(value) > _PATH_MAX
        or not value.isascii()
        or not value.isprintable()
    ):
        raise RecipeError(
            RecipeErrorCode.INVALID_VOLUME_PATH,
            "volume container_path must be an absolute, normalized, "
            "printable POSIX path without '$' (Compose interpolation)",
        )
    segments = value.split("/")[1:]
    if any(segment in ("", ".", "..") for segment in segments):
        raise RecipeError(
            RecipeErrorCode.INVALID_VOLUME_PATH,
            "volume container_path must be normalized: no empty, '.' or "
            "'..' segments and no trailing slash",
        )
    return value


# ---------------------------------------------------------------------------
# Section parsers (document order; every rejection value-free)
# ---------------------------------------------------------------------------


def _parse_environment(value: object) -> SupportedEnvironment:
    mapping = _require_mapping(value, "supported_environment")
    _reject_unknown(mapping, _ENVIRONMENT_FIELDS, "supported_environment")
    _require_fields(mapping, ("os", "arch"), "supported_environment")

    def _vocabulary(
        field: str, vocabulary: tuple[str, ...], code: RecipeErrorCode
    ) -> tuple[str, ...]:
        raw = mapping[field]
        if not isinstance(raw, list) or not raw or len(raw) > len(vocabulary):
            raise RecipeError(
                code,
                f"supported_environment.{field} must be a non-empty list "
                "within the frozen v1 vocabulary",
            )
        seen: set[str] = set()
        for entry in raw:
            if not isinstance(entry, str) or entry not in vocabulary or entry in seen:
                raise RecipeError(
                    code,
                    f"supported_environment.{field} values are restricted "
                    "to the frozen v1 vocabulary",
                )
            seen.add(entry)
        return tuple(sorted(seen))

    return SupportedEnvironment(
        os=_vocabulary("os", SUPPORTED_OS_VOCABULARY, RecipeErrorCode.UNSUPPORTED_OS),
        arch=_vocabulary(
            "arch", SUPPORTED_ARCH_VOCABULARY, RecipeErrorCode.UNSUPPORTED_ARCH
        ),
    )


def _parse_ports(value: object) -> tuple[PortBinding, ...]:
    if not isinstance(value, list):
        raise RecipeError(RecipeErrorCode.INVALID_PORT, "deploy.ports must be a list")
    if len(value) > _LIST_MAX:
        raise RecipeError(
            RecipeErrorCode.INVALID_PORT, "deploy.ports exceeds the entry limit"
        )
    bindings: list[PortBinding] = []
    host_seen: set[tuple[int, str]] = set()
    container_seen: set[tuple[int, str]] = set()
    for index, entry in enumerate(value):
        where = f"deploy.ports[{index}]"
        mapping = _require_mapping(entry, where)
        _reject_unknown(mapping, _PORT_FIELDS, where)
        _require_fields(mapping, ("host", "container", "protocol"), where)
        host = _validated_int(
            mapping["host"],
            f"{where}.host",
            _PORT_MIN_HOST,
            _PORT_MAX,
            RecipeErrorCode.INVALID_PORT,
        )
        container = _validated_int(
            mapping["container"],
            f"{where}.container",
            _PORT_MIN_CONTAINER,
            _PORT_MAX,
            RecipeErrorCode.INVALID_PORT,
        )
        protocol = mapping["protocol"]
        if protocol not in PROTOCOL_VOCABULARY:
            raise RecipeError(
                RecipeErrorCode.INVALID_PORT,
                f"{where}.protocol is restricted to the frozen vocabulary",
            )
        if (host, protocol) in host_seen or (container, protocol) in container_seen:
            raise RecipeError(
                RecipeErrorCode.DUPLICATE_PORT,
                f"{where} duplicates an already-declared port binding",
            )
        host_seen.add((host, protocol))
        container_seen.add((container, protocol))
        bindings.append(PortBinding(host=host, container=container, protocol=protocol))
    return tuple(sorted(bindings, key=lambda p: (p.host, p.container, p.protocol)))


def _parse_volumes(value: object) -> tuple[Volume, ...]:
    if not isinstance(value, list):
        raise RecipeError(
            RecipeErrorCode.INVALID_VOLUME, "deploy.volumes must be a list"
        )
    if len(value) > _LIST_MAX:
        raise RecipeError(
            RecipeErrorCode.INVALID_VOLUME, "deploy.volumes exceeds the entry limit"
        )
    volumes: list[Volume] = []
    names: set[str] = set()
    paths: set[str] = set()
    for index, entry in enumerate(value):
        where = f"deploy.volumes[{index}]"
        mapping = _require_mapping(entry, where)
        _reject_unknown(mapping, _VOLUME_FIELDS, where)
        _require_fields(mapping, ("name", "container_path"), where)
        name = _validated_token(
            mapping["name"],
            f"{where}.name",
            _VOLUME_NAME_RE,
            RecipeErrorCode.INVALID_VOLUME,
        )
        path = _validated_container_path(mapping["container_path"])
        if name in names or path in paths:
            raise RecipeError(
                RecipeErrorCode.DUPLICATE_VOLUME,
                f"{where} duplicates an already-declared volume name or container path",
            )
        names.add(name)
        paths.add(path)
        volumes.append(Volume(name=name, container_path=path))
    return tuple(sorted(volumes, key=lambda v: v.name))


def _parse_health(value: object, ports: tuple[PortBinding, ...]) -> HealthCheck | None:
    if value is None:
        return None
    mapping = _require_mapping(value, "health")
    _reject_unknown(mapping, _HEALTH_FIELDS, "health")
    _require_fields(mapping, ("kind", "port", "path"), "health")
    kind = mapping["kind"]
    if kind not in HEALTH_KIND_VOCABULARY:
        raise RecipeError(
            RecipeErrorCode.UNSUPPORTED_HEALTH_KIND,
            "health.kind is restricted to the frozen v1 vocabulary "
            "(argv-rendered HTTP probe; command/shell probes are not "
            "representable)",
        )
    port = _validated_int(
        mapping["port"],
        "health.port",
        _PORT_MIN_CONTAINER,
        _PORT_MAX,
        RecipeErrorCode.INVALID_HEALTH_CHECK,
    )
    binding = next((p for p in ports if p.container == port), None)
    if binding is None:
        raise RecipeError(
            RecipeErrorCode.HEALTH_PORT_UNBOUND,
            "health.port must reference a container port declared in deploy.ports",
        )
    if binding.protocol != "tcp":
        raise RecipeError(
            RecipeErrorCode.INVALID_HEALTH_CHECK,
            "health.port must reference a tcp port binding",
        )
    path = mapping["path"]
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "$" in path
        or len(path) > _HEALTH_PATH_MAX
        or not path.isascii()
        or not path.isprintable()
        or any(character.isspace() for character in path)
    ):
        raise RecipeError(
            RecipeErrorCode.INVALID_HEALTH_CHECK,
            "health.path must be an absolute, printable, whitespace-free "
            "URL path without '$' (Compose interpolation)",
        )
    expected_status = _validated_int(
        mapping.get("expected_status", 200),
        "health.expected_status",
        *_EXPECTED_STATUS_BOUNDS,
        RecipeErrorCode.INVALID_HEALTH_CHECK,
    )
    interval = _validated_int(
        mapping.get("interval_seconds", 30),
        "health.interval_seconds",
        *_INTERVAL_BOUNDS,
        RecipeErrorCode.INVALID_HEALTH_CHECK,
    )
    timeout = _validated_int(
        mapping.get("timeout_seconds", 5),
        "health.timeout_seconds",
        *_TIMEOUT_BOUNDS,
        RecipeErrorCode.INVALID_HEALTH_CHECK,
    )
    retries = _validated_int(
        mapping.get("retries", 3),
        "health.retries",
        *_RETRIES_BOUNDS,
        RecipeErrorCode.INVALID_HEALTH_CHECK,
    )
    start_period = _validated_int(
        mapping.get("start_period_seconds", 30),
        "health.start_period_seconds",
        *_START_PERIOD_BOUNDS,
        RecipeErrorCode.INVALID_HEALTH_CHECK,
    )
    return HealthCheck(
        port=port,
        path=path,
        expected_status=expected_status,
        interval_seconds=interval,
        timeout_seconds=timeout,
        retries=retries,
        start_period_seconds=start_period,
    )


# ---------------------------------------------------------------------------
# Canonical document: strict parse + deterministic render (pure)
# ---------------------------------------------------------------------------


def parse_recipe_text(text: str) -> ApplicationRecipe:
    """Strictly parse and validate one canonical recipe document.

    Unknown fields, duplicate keys, alias expansion, over-size/over-depth
    documents and every out-of-vocabulary value are rejected with value-free
    :class:`RecipeError`s (no field values in messages). Collections are
    order-normalized at parse time so rendering is deterministic.
    """
    _checked_document(text)
    try:
        document = yaml.load(text, Loader=_StrictSafeLoader)
    except yaml.YAMLError:
        raise RecipeError(
            RecipeErrorCode.UNPARSABLE_DOCUMENT,
            "recipe is not a valid strict YAML document",
        ) from None
    mapping = _require_mapping(document, "recipe")
    _reject_unknown(mapping, _TOP_LEVEL_FIELDS, "recipe")
    _require_fields(mapping, _REQUIRED_TOP_LEVEL_FIELDS, "recipe")
    if mapping["schema_version"] != RECIPE_SCHEMA_VERSION:
        raise RecipeError(
            RecipeErrorCode.SCHEMA_VERSION_UNSUPPORTED,
            f"recipe schema_version must be {RECIPE_SCHEMA_VERSION}",
        )
    name = _validated_token(
        mapping["name"], "name", _NAME_RE, RecipeErrorCode.INVALID_NAME
    )
    environment = _parse_environment(mapping["supported_environment"])

    deploy = _require_mapping(mapping["deploy"], "deploy")
    _reject_unknown(deploy, _DEPLOY_FIELDS, "deploy")
    _require_fields(deploy, ("image",), "deploy")
    image_raw = deploy["image"]
    if not isinstance(image_raw, str) or not image_raw:
        raise RecipeError(
            RecipeErrorCode.INVALID_IMAGE, "deploy.image must be a non-empty string"
        )
    if "@" not in image_raw:
        raise RecipeError(
            RecipeErrorCode.UNPINNED_IMAGE,
            "deploy.image must be pinned as name@sha256:<64-hex-digest> "
            "(tags are not pins)",
        )
    if not _IMAGE_RE.fullmatch(image_raw):
        raise RecipeError(
            RecipeErrorCode.INVALID_IMAGE,
            "deploy.image must match <registry/>repository@sha256:<64 hex>",
        )
    ports = _parse_ports(deploy.get("ports", []))
    volumes = _parse_volumes(deploy.get("volumes", []))
    credentials = _validated_token_list(
        deploy.get("credentials", []),
        "deploy.credentials",
        _CREDENTIAL_RE,
        RecipeErrorCode.INVALID_CREDENTIAL_REFERENCE,
        RecipeErrorCode.DUPLICATE_CREDENTIAL_REFERENCE,
        _CRED_MAX,
    )
    prerequisites = _validated_token_list(
        mapping.get("prerequisites", []),
        "prerequisites",
        _PREREQUISITE_RE,
        RecipeErrorCode.INVALID_PREREQUISITE,
        RecipeErrorCode.INVALID_PREREQUISITE,
        _PREREQ_MAX,
    )
    required_grants = _validated_token_list(
        mapping.get("required_grants", []),
        "required_grants",
        _GRANT_RE,
        RecipeErrorCode.INVALID_GRANT,
        RecipeErrorCode.INVALID_GRANT,
        _GRANT_MAX,
    )

    update_mapping = _require_mapping(mapping["update"], "update")
    _reject_unknown(update_mapping, _UPDATE_FIELDS, "update")
    _require_fields(update_mapping, ("migrations",), "update")
    migrations_raw = update_mapping["migrations"]
    if migrations_raw not in UPDATE_MIGRATIONS_VOCABULARY:
        raise RecipeError(
            RecipeErrorCode.INVALID_UPDATE_SEMANTICS,
            "update.migrations is restricted to the frozen vocabulary",
        )
    update = UpdateSemantics(migrations=UpdateMigrations(migrations_raw))

    removal_mapping = _require_mapping(mapping["removal"], "removal")
    _reject_unknown(removal_mapping, _REMOVAL_FIELDS, "removal")
    _require_fields(removal_mapping, ("data_policy",), "removal")
    policy_raw = removal_mapping["data_policy"]
    if policy_raw == "delete":
        raise RecipeError(
            RecipeErrorCode.DATA_DELETE_FORBIDDEN,
            "a recipe can never pre-authorize data deletion; removal data "
            "policy is explicit_choice or keep (C09 rule 5)",
        )
    if policy_raw not in REMOVAL_DATA_POLICY_VOCABULARY:
        raise RecipeError(
            RecipeErrorCode.INVALID_REMOVAL_SEMANTICS,
            "removal.data_policy is restricted to the frozen vocabulary",
        )
    removal = RemovalSemantics(data_policy=RemovalDataPolicy(policy_raw))

    recovery_mapping = _require_mapping(mapping["recovery"], "recovery")
    _reject_unknown(recovery_mapping, _RECOVERY_FIELDS, "recovery")
    _require_fields(recovery_mapping, ("image_rollback",), "recovery")
    recovery = RecoverySemantics(
        image_rollback=_validated_bool(
            recovery_mapping["image_rollback"], "recovery.image_rollback"
        )
    )

    backup_mapping = _require_mapping(mapping["backup"], "backup")
    _reject_unknown(backup_mapping, _BACKUP_FIELDS, "backup")
    _require_fields(backup_mapping, ("required",), "backup")
    backup = BackupSemantics(
        required=_validated_bool(backup_mapping["required"], "backup.required")
    )

    health = _parse_health(mapping.get("health"), ports)

    return ApplicationRecipe(
        name=name,
        supported_environment=environment,
        image=image_raw,
        ports=ports,
        volumes=volumes,
        credentials=credentials,
        prerequisites=prerequisites,
        required_grants=required_grants,
        update=update,
        removal=removal,
        recovery=recovery,
        backup=backup,
        health=health,
    )


def render_recipe(recipe: ApplicationRecipe) -> str:
    """Render the canonical recipe document for one validated recipe.

    Deterministic: sorted keys, block style, order-normalized collections —
    the same recipe always produces the same bytes, so
    :func:`recipe_sha256` is a stable revision binding.
    """
    document: dict[str, object] = {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "name": recipe.name,
        "supported_environment": {
            "os": sorted(recipe.supported_environment.os),
            "arch": sorted(recipe.supported_environment.arch),
        },
        "prerequisites": sorted(recipe.prerequisites),
        "required_grants": sorted(recipe.required_grants),
        "deploy": {
            "image": recipe.image,
            "ports": [
                {"host": p.host, "container": p.container, "protocol": p.protocol}
                for p in sorted(
                    recipe.ports, key=lambda p: (p.host, p.container, p.protocol)
                )
            ],
            "volumes": [
                {"name": v.name, "container_path": v.container_path}
                for v in sorted(recipe.volumes, key=lambda v: v.name)
            ],
            "credentials": sorted(recipe.credentials),
        },
        "update": {"migrations": recipe.update.migrations.value},
        "removal": {"data_policy": recipe.removal.data_policy.value},
        "recovery": {"image_rollback": recipe.recovery.image_rollback},
        "backup": {"required": recipe.backup.required},
    }
    if recipe.health is not None:
        document["health"] = {
            "kind": "http",
            "port": recipe.health.port,
            "path": recipe.health.path,
            "expected_status": recipe.health.expected_status,
            "interval_seconds": recipe.health.interval_seconds,
            "timeout_seconds": recipe.health.timeout_seconds,
            "retries": recipe.health.retries,
            "start_period_seconds": recipe.health.start_period_seconds,
        }
    return yaml.safe_dump(
        document, sort_keys=True, default_flow_style=False, allow_unicode=True
    )


def recipe_sha256(recipe: ApplicationRecipe) -> str:
    """SHA-256 of the canonical recipe rendering (revision binding)."""
    return hashlib.sha256(render_recipe(recipe).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Deterministic Compose rendering (policy-bound deployment artifact)
# ---------------------------------------------------------------------------


def application_compose_dir(application_id: uuid.UUID) -> str:
    """Target-side directory for one managed application (frozen scheme)."""
    if not isinstance(application_id, uuid.UUID):
        raise TypeError("application_id must be a uuid.UUID (C02 rule 3)")
    return f"{TARGET_APPS_ROOT}/{application_id}"


def volume_host_path(application_id: uuid.UUID, volume: Volume) -> str:
    """Planner-allocated target host path for one declared volume."""
    if not isinstance(application_id, uuid.UUID):
        raise TypeError("application_id must be a uuid.UUID (C02 rule 3)")
    if not _VOLUME_NAME_RE.fullmatch(volume.name):
        raise RecipeError(
            RecipeErrorCode.INVALID_VOLUME,
            "volume name does not match the required token shape",
        )
    return f"{TARGET_APPS_ROOT}/{application_id}/volumes/{volume.name}"


def project_name(application_id: uuid.UUID) -> str:
    """Deterministic Compose project name for one managed application."""
    if not isinstance(application_id, uuid.UUID):
        raise TypeError("application_id must be a uuid.UUID (C02 rule 3)")
    return f"mai-app-{application_id.hex}"


def render_compose(recipe: ApplicationRecipe, application_id: uuid.UUID) -> str:
    """Render the deployment Compose document (Compose v2 spec).

    Deterministic and shell-free: the image is the pinned
    ``name@sha256:<digest>`` reference, ports/volumes are the validated
    bindings with planner-allocated host paths, the healthcheck is the
    argv-form ``CMD`` curl probe (never ``CMD-SHELL``), and credential
    references appear ONLY as an ``env_file`` pointer to the sibling
    ``credentials.env`` the executor provisions from the configured
    resolver (values are never rendered). The rendered document contains no
    ``$`` interpolation at all.
    """
    service: dict[str, object] = {
        "image": recipe.image,
        "restart": "unless-stopped",
    }
    if recipe.ports:
        service["ports"] = [
            f"{p.host}:{p.container}/{p.protocol}"
            for p in sorted(
                recipe.ports, key=lambda p: (p.host, p.container, p.protocol)
            )
        ]
    if recipe.volumes:
        service["volumes"] = [
            f"{volume_host_path(application_id, v)}:{v.container_path}"
            for v in sorted(recipe.volumes, key=lambda v: v.name)
        ]
    if recipe.credentials:
        service["env_file"] = [TARGET_CREDENTIALS_FILENAME]
    if recipe.health is not None:
        probe_url = f"http://127.0.0.1:{recipe.health.port}{recipe.health.path}"
        service["healthcheck"] = {
            "test": [*_HEALTHCHECK_CURL_ARGV_PREFIX, probe_url],
            "interval": f"{recipe.health.interval_seconds}s",
            "timeout": f"{recipe.health.timeout_seconds}s",
            "retries": recipe.health.retries,
            "start_period": f"{recipe.health.start_period_seconds}s",
        }
    document = {
        "name": project_name(application_id),
        "services": {recipe.name: service},
    }
    return yaml.safe_dump(
        document, sort_keys=True, default_flow_style=False, allow_unicode=True
    )


# ---------------------------------------------------------------------------
# Policy-bound deployment plan (C09 rule 3; pure description, no effects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TargetEnvironment:
    """The enrolled target's observed environment (explicit plan input)."""

    os: str
    arch: str


@dataclass(frozen=True, slots=True)
class PlannedVolume:
    """One planned persistent-data location with its allocated host path."""

    name: str
    container_path: str
    host_path: str


@dataclass(frozen=True, slots=True)
class HealthProbe:
    """The declarative health probe bound into the plan (no shell)."""

    kind: str
    port: int
    path: str
    expected_status: int
    interval_seconds: int
    timeout_seconds: int
    retries: int
    start_period_seconds: int


@dataclass(frozen=True, slots=True)
class PlanOperation:
    """One frozen-vocabulary operation bound to its exact resource."""

    op: str
    subject: str


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    """The complete policy-bound resource plan for one managed deployment.

    Self-contained: carries the exact rendered Compose bytes and their
    hash, the exact target paths, the exact pinned image, the operation
    list (the ONLY operations the executor may apply) and the recipe
    revision hash binding this plan to one validated recipe revision.
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    project_name: str
    target_os: str
    target_arch: str
    image: str
    recipe_sha256: str
    compose_path: str
    compose_sha256: str
    compose_text: str
    directories: tuple[str, ...]
    ports: tuple[PortBinding, ...]
    volumes: tuple[PlannedVolume, ...]
    credentials: tuple[str, ...]
    credentials_path: str | None
    health_probe: HealthProbe | None
    prerequisites: tuple[str, ...]
    required_grants: tuple[str, ...]
    operations: tuple[PlanOperation, ...]
    limitations: tuple[str, ...]
    plan_sha256: str


# Limitation strings are frozen module constants: plan text is deterministic
# and evidence-comparable (C09 rule 3).
_LIMIT_NO_HEALTH = "no declared health verification; deployment success is unverified"
_LIMIT_MIGRATIONS_REVERSIBLE = "declared update migrations are reversible"
_LIMIT_MIGRATIONS_IRREVERSIBLE = (
    "declared update migrations may be irreversible; data rollback is not possible"
)
_LIMIT_ROLLBACK_AVAILABLE = (
    "recovery: the previous pinned image digest remains deployable"
)
_LIMIT_ROLLBACK_NONE = (
    "recovery: no image rollback; recovery requires a new recipe revision"
)
_LIMIT_REMOVAL_EXPLICIT = (
    "removal requires an explicit keep-or-delete data decision; absent "
    "intent refuses data deletion"
)
_LIMIT_REMOVAL_KEEP = (
    "declared removal policy keeps all persistent data; no data deletion"
)
_LIMIT_BACKUP_REQUIRED = (
    "a backup of persistent data is required before data-affecting operations"
)


def build_deployment_plan(
    recipe: ApplicationRecipe,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    target: TargetEnvironment,
) -> DeploymentPlan:
    """Build the policy-bound plan for deploying ``recipe`` on one target.

    Refuses targets outside the recipe's declared supported environment
    (``unsupported_target_os`` / ``unsupported_target_arch``) and validates
    that every input is the explicit, typed identity the caller supplies.
    Pure: the plan describes resources and operations; applying it belongs
    to the executor cards.
    """
    if not isinstance(recipe, ApplicationRecipe):
        raise RecipeError(
            RecipeErrorCode.INVALID_FIELD,
            "recipe must be a validated ApplicationRecipe (parse_recipe_text)",
        )
    if not isinstance(server_id, uuid.UUID):
        raise RecipeError(
            RecipeErrorCode.INVALID_SERVER_ID, "server_id must be a uuid.UUID"
        )
    if not isinstance(application_id, uuid.UUID):
        raise RecipeError(
            RecipeErrorCode.INVALID_APPLICATION_ID,
            "application_id must be a uuid.UUID",
        )
    if (
        not isinstance(target, TargetEnvironment)
        or not isinstance(target.os, str)
        or not isinstance(target.arch, str)
    ):
        raise RecipeError(
            RecipeErrorCode.INVALID_TARGET_ENVIRONMENT,
            "target must be a TargetEnvironment with string os/arch",
        )
    if target.os not in recipe.supported_environment.os:
        raise RecipeError(
            RecipeErrorCode.UNSUPPORTED_TARGET_OS,
            "target os is outside the recipe's declared supported environment",
        )
    if target.arch not in recipe.supported_environment.arch:
        raise RecipeError(
            RecipeErrorCode.UNSUPPORTED_TARGET_ARCH,
            "target arch is outside the recipe's declared supported environment",
        )

    compose_dir = application_compose_dir(application_id)
    compose_path = f"{compose_dir}/{TARGET_COMPOSE_FILENAME}"
    compose_text = render_compose(recipe, application_id)
    directories = tuple(
        sorted(
            {
                compose_dir,
                *(volume_host_path(application_id, v) for v in recipe.volumes),
            }
        )
    )
    planned_volumes = tuple(
        sorted(
            (
                PlannedVolume(
                    name=v.name,
                    container_path=v.container_path,
                    host_path=volume_host_path(application_id, v),
                )
                for v in recipe.volumes
            ),
            key=lambda v: v.name,
        )
    )
    credentials_path = (
        f"{compose_dir}/{TARGET_CREDENTIALS_FILENAME}" if recipe.credentials else None
    )
    health_probe = (
        HealthProbe(
            kind="http",
            port=recipe.health.port,
            path=recipe.health.path,
            expected_status=recipe.health.expected_status,
            interval_seconds=recipe.health.interval_seconds,
            timeout_seconds=recipe.health.timeout_seconds,
            retries=recipe.health.retries,
            start_period_seconds=recipe.health.start_period_seconds,
        )
        if recipe.health is not None
        else None
    )

    operations: list[PlanOperation] = [
        PlanOperation(
            op=OP_VERIFY_PREREQUISITES,
            subject=",".join(recipe.prerequisites)
            if recipe.prerequisites
            else "<none>",
        )
    ]
    if recipe.backup.required and recipe.volumes:
        operations.append(
            PlanOperation(
                op=OP_REQUIRE_BACKUP,
                subject=",".join(v.host_path for v in planned_volumes),
            )
        )
    operations.extend(
        [
            PlanOperation(op=OP_CREATE_DIRECTORIES, subject=",".join(directories)),
            PlanOperation(op=OP_WRITE_COMPOSE, subject=compose_path),
        ]
    )
    if credentials_path is not None:
        operations.append(
            PlanOperation(op=OP_WRITE_CREDENTIALS, subject=credentials_path)
        )
    operations.extend(
        [
            PlanOperation(op=OP_PULL_IMAGE, subject=recipe.image),
            PlanOperation(op=OP_START, subject=project_name(application_id)),
        ]
    )
    if health_probe is not None:
        operations.append(
            PlanOperation(
                op=OP_WAIT_HEALTHY,
                subject=f"http://127.0.0.1:{health_probe.port}{health_probe.path}",
            )
        )

    limitations: list[str] = []
    if health_probe is None:
        limitations.append(_LIMIT_NO_HEALTH)
    limitations.append(
        _LIMIT_MIGRATIONS_IRREVERSIBLE
        if recipe.update.migrations is UpdateMigrations.IRREVERSIBLE
        else _LIMIT_MIGRATIONS_REVERSIBLE
    )
    limitations.append(
        _LIMIT_ROLLBACK_AVAILABLE
        if recipe.recovery.image_rollback
        else _LIMIT_ROLLBACK_NONE
    )
    limitations.append(
        _LIMIT_REMOVAL_EXPLICIT
        if recipe.removal.data_policy is RemovalDataPolicy.EXPLICIT_CHOICE
        else _LIMIT_REMOVAL_KEEP
    )
    if recipe.backup.required and recipe.volumes:
        limitations.append(_LIMIT_BACKUP_REQUIRED)

    compose_sha256 = hashlib.sha256(compose_text.encode("utf-8")).hexdigest()
    plan = DeploymentPlan(
        server_id=server_id,
        application_id=application_id,
        project_name=project_name(application_id),
        target_os=target.os,
        target_arch=target.arch,
        image=recipe.image,
        recipe_sha256=recipe_sha256(recipe),
        compose_path=compose_path,
        compose_sha256=compose_sha256,
        compose_text=compose_text,
        directories=directories,
        ports=recipe.ports,
        volumes=planned_volumes,
        credentials=recipe.credentials,
        credentials_path=credentials_path,
        health_probe=health_probe,
        prerequisites=recipe.prerequisites,
        required_grants=recipe.required_grants,
        operations=tuple(operations),
        limitations=tuple(limitations),
        plan_sha256="",
    )
    return replace(
        plan,
        plan_sha256=hashlib.sha256(
            canonical_plan_json(plan).encode("utf-8")
        ).hexdigest(),
    )


def canonical_plan_json(plan: DeploymentPlan) -> str:
    """Canonical JSON projection of a plan (input to :data:`plan_sha256`)."""
    return json.dumps(
        _plan_mapping(plan), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _plan_mapping(plan: DeploymentPlan) -> dict[str, object]:
    probe = plan.health_probe
    return {
        "server_id": str(plan.server_id),
        "application_id": str(plan.application_id),
        "project_name": plan.project_name,
        "target_os": plan.target_os,
        "target_arch": plan.target_arch,
        "image": plan.image,
        "recipe_sha256": plan.recipe_sha256,
        "compose_path": plan.compose_path,
        "compose_sha256": plan.compose_sha256,
        "compose_text": plan.compose_text,
        "directories": list(plan.directories),
        "ports": [
            {"host": p.host, "container": p.container, "protocol": p.protocol}
            for p in plan.ports
        ],
        "volumes": [
            {
                "name": v.name,
                "container_path": v.container_path,
                "host_path": v.host_path,
            }
            for v in plan.volumes
        ],
        "credentials": list(plan.credentials),
        "credentials_path": plan.credentials_path,
        "health_probe": (
            {
                "kind": probe.kind,
                "port": probe.port,
                "path": probe.path,
                "expected_status": probe.expected_status,
                "interval_seconds": probe.interval_seconds,
                "timeout_seconds": probe.timeout_seconds,
                "retries": probe.retries,
                "start_period_seconds": probe.start_period_seconds,
            }
            if probe is not None
            else None
        ),
        "prerequisites": list(plan.prerequisites),
        "required_grants": list(plan.required_grants),
        "operations": [{"op": o.op, "subject": o.subject} for o in plan.operations],
        "limitations": list(plan.limitations),
    }
