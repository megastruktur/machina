"""Versioned capability descriptor registry (contracts.md **C03 rule 1/7**).

A capability is declared, not executed. This module is the production
registry that validates and stores :class:`CapabilityDescriptor` records —
the frozen metadata vocabulary every executor path (C04), the agent bridge
(C06) and the jobs facade (C05) consume when *selecting* tools. It grants
nothing:

- **Registration ≠ authorization.** The registry stores declarative
  metadata only. There is no call/execute surface, no stored code
  reference and no handle: a descriptor is JSON-shaped data. Grants,
  adoption and policy rechecks before any privileged effect belong to the
  dispatcher/policy cards (C03 rules 3–5); ``list_capabilities()`` output
  is safe to render for UI/model tool selection because every field is
  declarative (schemas, slugs, enums) and no field can carry credential
  material or code.
- **Frozen vocabulary (C03 rule 1).** Identifier, version, config/input/
  output schemas, supported execution environments, effect class,
  declared resource scope, required grants. The JSON field names below
  are the frozen examples this card owns against that section::

      {
        "identifier": "deploy.app",
        "version": "1.2.0",
        "config_schema": null,
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "execution_environments": ["linux_ssh"],
        "effect_class": "mutate",
        "resource_scope": ["target.fs"],
        "required_grants": ["deploy.app"]
      }

  Unknown top-level fields are rejected (strict, mirroring the canonical
  profile schema), so a descriptor structurally cannot smuggle in a code
  reference (``handler``, ``entry_point``, …) or a credential value.
- **Duplicates and incompatible versions are rejected (AC1).** The
  registry is keyed by ``(identifier, version)``: a second registration
  of the same pair fails with ``duplicate_registration`` even if the
  content is identical (registration is a declared fact, not an upsert).
  Within one identifier, all versions sharing a major must publish the
  *same* input/output schemas (canonical JSON equality): semver reserves
  breaking call-shape changes for a major bump, so an incompatible
  sibling under an existing major fails with ``incompatible_version``.
  The first registration in a major pins the line's call contract.
- **Atomic registration.** Every check runs before any state mutation;
  a rejected registration leaves the registry exactly as it was — no
  partially enabled tool (feature-gate scenario).
- **Trusted-code boundary (C03 rule 7).** Native Python plugins are
  trusted installed code; :func:`descriptor_from_module` imports a
  module and reads its module-level ``CAPABILITY`` declaration through
  the same validation path. Loading executes that trusted code by
  design — validation is metadata validation, **not** sandboxing. The
  module object is dropped after the call: the registry keeps only the
  validated declaration, and the loader never inserts into
  ``sys.modules``.

Value-free discipline: rejection details carry field names, identifier/
version/slug strings (declarative, not secret) and source paths — never
schema document content. The CLI prints structured JSON only. The module
performs no logging, reads no process environment and touches no clock
or network state.

Thread safety: registry mutations are serialized by an internal lock;
reads take an immutable snapshot. Cross-process sharing or durability is
out of scope here (the composition boundary constructs one registry per
process; a durable projection belongs to its own card).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "CAPABILITY_DECLARATION",
    "EFFECT_CLASSES",
    "EXECUTION_ENVIRONMENTS",
    "MAX_CAPABILITY_MODULE_BYTES",
    "MAX_SCHEMA_JSON_BYTES",
    "VERSION_RE",
    "CapabilityDescriptor",
    "CapabilityError",
    "CapabilityErrorCode",
    "CapabilityRegistry",
    "descriptor_from_module",
    "main",
    "parse_descriptor",
]

#: Frozen execution-environment vocabulary for descriptors. ``linux_ssh``
#: reuses the canonical profile schema's target environment (``mai.contracts.
#: server`` ``SUPPORTED_ENVIRONMENTS``); ``controller`` names the ``mai-worker``
#: execution unit (SERVICES row) for capabilities that run controller-local
#: (bundled search/docs/memory tooling) instead of on an SSH target.
EXECUTION_ENVIRONMENTS = frozenset({"controller", "linux_ssh"})

#: Frozen effect-class vocabulary. ``read`` changes no observable state;
#: ``mutate`` changes target/controller state within granted scope;
#: ``privileged`` is the explicitly broad class (raw shell/root — enabling
#: it forfeits app-level OS containment claims, C03 rule 5). Consumers
#: (policy/approval/dispatcher) route on this metadata; the registry only
#: validates it.
EFFECT_CLASSES = frozenset({"read", "mutate", "privileged"})

#: Capability identifier shape, frozen by the canonical profile schema
#: (``mai.contracts.server`` ``_CAPABILITY_RE``: "C03 rule 1 identifiers are
#: lowercase slugs"). Kept local: the schema module is the shape owner and
#: is outside this card's allowlist; a divergence would be a review
#: finding, not a silent import of a private symbol.
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

#: Declared resource-scope slugs name lockable resources (consumed by the
#: resource-lock owner); shorter than identifiers, same lowercase charset.
_RESOURCE_SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: Strict semver core: ``MAJOR.MINOR.PATCH``, no leading zeros, no
#: prerelease/build suffixes (a prerelease descriptor is not a published
#: tool surface; add suffixes only via a contract reconciliation).
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

#: Frozen descriptor vocabulary — exactly the C03 rule 1 fields, in output
#: order. Anything else is ``unknown_field``.
_VOCABULARY: tuple[str, ...] = (
    "identifier",
    "version",
    "config_schema",
    "input_schema",
    "output_schema",
    "execution_environments",
    "effect_class",
    "resource_scope",
    "required_grants",
)

_REQUIRED_FIELDS: tuple[str, ...] = (
    "identifier",
    "version",
    "input_schema",
    "output_schema",
    "execution_environments",
    "effect_class",
    "resource_scope",
    "required_grants",
)

#: Byte cap for one schema document (config/input/output alike). A
#: descriptor is selection metadata, not a data channel; the cap bounds
#: validation work and keeps list output renderable.
MAX_SCHEMA_JSON_BYTES = 16 * 1024

#: Byte cap for one capability module's source (trusted code is small;
#: the cap bounds the read, not the interpreter).
MAX_CAPABILITY_MODULE_BYTES = 256 * 1024

#: Module-level declaration name read by :func:`descriptor_from_module`.
CAPABILITY_DECLARATION = "CAPABILITY"


class CapabilityErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    NOT_A_MAPPING = "not_a_mapping"
    INVALID_JSON = "invalid_json"
    NOT_READABLE = "not_readable"
    UNKNOWN_FIELD = "unknown_field"
    MISSING_FIELD = "missing_field"
    INVALID_IDENTIFIER = "invalid_identifier"
    INVALID_VERSION = "invalid_version"
    INVALID_SCHEMA = "invalid_schema"
    INVALID_ENVIRONMENTS = "invalid_environments"
    INVALID_EFFECT_CLASS = "invalid_effect_class"
    INVALID_RESOURCE_SCOPE = "invalid_resource_scope"
    INVALID_GRANTS = "invalid_grants"
    DUPLICATE_REGISTRATION = "duplicate_registration"
    INCOMPATIBLE_VERSION = "incompatible_version"
    MODULE_LOAD_ERROR = "module_load_error"
    DECLARATION_MISSING = "capability_declaration_missing"


class CapabilityError(ValueError):
    """One structured, value-free descriptor/registry rejection.

    ``detail`` names fields, slugs, versions and source paths. Schema
    *content* is never quoted (a schema may describe credential-shaped
    configuration); tests pin this with a sentinel sweep.
    """

    def __init__(self, code: CapabilityErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Validated capability descriptor (C03 rule 1 vocabulary).

    Instances are immutable; the mapping fields hold private plain-dict
    copies. :meth:`to_json` hands out a fresh deep copy per call, so a
    consumer mutating returned data can never corrupt registry state.
    """

    identifier: str
    version: str
    config_schema: Mapping[str, object] | None
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    execution_environments: tuple[str, ...]
    effect_class: str
    resource_scope: tuple[str, ...]
    required_grants: tuple[str, ...]

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        """Parsed ``(major, minor, patch)``."""
        major, minor, patch = (int(part) for part in self.version.split("."))
        return (major, minor, patch)

    @property
    def major(self) -> int:
        """The semver major (compatibility line key)."""
        return self.version_tuple[0]

    def to_json(self) -> dict[str, object]:
        """Frozen-shape JSON document, fresh copy per call."""
        document: dict[str, object] = {
            "identifier": self.identifier,
            "version": self.version,
            "config_schema": None
            if self.config_schema is None
            else json.loads(json.dumps(self.config_schema)),
            "input_schema": json.loads(json.dumps(self.input_schema)),
            "output_schema": json.loads(json.dumps(self.output_schema)),
            "execution_environments": list(self.execution_environments),
            "effect_class": self.effect_class,
            "resource_scope": list(self.resource_scope),
            "required_grants": list(self.required_grants),
        }
        return document

    def __repr__(self) -> str:
        return (
            f"<CapabilityDescriptor {self.identifier}@{self.version} "
            f"{self.effect_class}>"
        )


def _check_json_keys(value: object, path: str) -> None:
    """Every object key in a schema must be a string (JSON objects only).

    ``json.dumps`` would silently coerce non-string keys; a descriptor
    schema is frozen JSON, so coercion is a rejection, not a rewrite.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CapabilityError(
                    CapabilityErrorCode.INVALID_SCHEMA,
                    f"{path}: object keys must be strings",
                )
            _check_json_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_json_keys(item, f"{path}[{index}]")


def _validated_schema(value: object, field: str, source: str) -> dict[str, object]:
    """Validate one schema field: JSON object, JSON-safe, size-capped.

    Structural validation only: full JSON-Schema semantic checking would
    need an undeclared dependency (allowlist forbids new deps); anything
    that is a JSON object is accepted verbatim.
    """
    where = f"{source}: {field}"
    if not isinstance(value, dict):
        raise CapabilityError(
            CapabilityErrorCode.INVALID_SCHEMA, f"{where} must be a JSON object"
        )
    _check_json_keys(value, where)
    try:
        canonical = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise CapabilityError(
            CapabilityErrorCode.INVALID_SCHEMA,
            f"{where} is not JSON-safe ({type(error).__name__})",
        ) from None
    if len(canonical.encode("utf-8")) > MAX_SCHEMA_JSON_BYTES:
        raise CapabilityError(
            CapabilityErrorCode.INVALID_SCHEMA,
            f"{where} exceeds {MAX_SCHEMA_JSON_BYTES} bytes",
        )
    return json.loads(canonical)


def _slug_tuple(
    value: object,
    field: str,
    source: str,
    *,
    pattern: re.Pattern[str],
    code: CapabilityErrorCode,
    max_len_error: int,
) -> tuple[str, ...]:
    """Validate one slug-list field (resource scopes / required grants)."""
    where = f"{source}: {field}"
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CapabilityError(code, f"{where} must be a list of strings")
    if len(set(value)) != len(value):
        raise CapabilityError(code, f"{where} contains duplicates")
    for item in value:
        if not pattern.fullmatch(item):
            raise CapabilityError(
                code,
                f"{where} entry must match {pattern.pattern} "
                f"(at most {max_len_error} characters)",
            )
    return tuple(value)


def parse_descriptor(
    document: object, *, source: str = "<descriptor>"
) -> CapabilityDescriptor:
    """Validate one descriptor document and return the typed record.

    Strict vocabulary, required fields, frozen enums/charset rules; see
    the module docstring for the frozen JSON shape and the cross-field
    rule (``mutate``/``privileged`` must declare a non-empty resource
    scope — an effect without declared resources cannot be lock-planned).
    Raises :class:`CapabilityError` with a stable code.
    """
    if not isinstance(document, Mapping):
        raise CapabilityError(
            CapabilityErrorCode.NOT_A_MAPPING,
            f"{source}: descriptor must be a JSON object",
        )
    unknown = sorted(str(key) for key in document if key not in _VOCABULARY)
    if unknown:
        raise CapabilityError(
            CapabilityErrorCode.UNKNOWN_FIELD,
            f"{source}: unknown descriptor field {unknown[0]!r} "
            f"(frozen vocabulary: {', '.join(_VOCABULARY)})",
        )
    for field in _REQUIRED_FIELDS:
        if field not in document:
            raise CapabilityError(
                CapabilityErrorCode.MISSING_FIELD, f"{source}: missing {field}"
            )

    identifier = document["identifier"]
    if not isinstance(identifier, str) or not _IDENTIFIER_RE.fullmatch(identifier):
        raise CapabilityError(
            CapabilityErrorCode.INVALID_IDENTIFIER,
            f"{source}: identifier must match {_IDENTIFIER_RE.pattern}",
        )
    version = document["version"]
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise CapabilityError(
            CapabilityErrorCode.INVALID_VERSION,
            f"{source}: version must be MAJOR.MINOR.PATCH "
            f"(semver core, no leading zeros), got {version!r}",
        )

    effect = document["effect_class"]
    if not isinstance(effect, str) or effect not in EFFECT_CLASSES:
        raise CapabilityError(
            CapabilityErrorCode.INVALID_EFFECT_CLASS,
            f"{source}: effect_class must be one of "
            f"{', '.join(sorted(EFFECT_CLASSES))}",
        )

    raw_environments = document["execution_environments"]
    if not isinstance(raw_environments, list) or not all(
        isinstance(item, str) for item in raw_environments
    ):
        raise CapabilityError(
            CapabilityErrorCode.INVALID_ENVIRONMENTS,
            f"{source}: execution_environments must be a list of strings",
        )
    if not raw_environments:
        raise CapabilityError(
            CapabilityErrorCode.INVALID_ENVIRONMENTS,
            f"{source}: execution_environments must not be empty",
        )
    if len(set(raw_environments)) != len(raw_environments):
        raise CapabilityError(
            CapabilityErrorCode.INVALID_ENVIRONMENTS,
            f"{source}: execution_environments contains duplicates",
        )
    for item in raw_environments:
        if item not in EXECUTION_ENVIRONMENTS:
            raise CapabilityError(
                CapabilityErrorCode.INVALID_ENVIRONMENTS,
                f"{source}: execution environment {item!r} is not one of "
                f"{', '.join(sorted(EXECUTION_ENVIRONMENTS))}",
            )

    resource_scope = _slug_tuple(
        document["resource_scope"],
        "resource_scope",
        source,
        pattern=_RESOURCE_SCOPE_RE,
        code=CapabilityErrorCode.INVALID_RESOURCE_SCOPE,
        max_len_error=64,
    )
    if effect != "read" and not resource_scope:
        raise CapabilityError(
            CapabilityErrorCode.INVALID_RESOURCE_SCOPE,
            f"{source}: effect_class {effect!r} requires a non-empty "
            "resource_scope (declared resources are the lock/planning "
            "surface)",
        )

    required_grants = _slug_tuple(
        document["required_grants"],
        "required_grants",
        source,
        pattern=_IDENTIFIER_RE,
        code=CapabilityErrorCode.INVALID_GRANTS,
        max_len_error=128,
    )

    raw_config = document.get("config_schema")
    config_schema = (
        None
        if raw_config is None
        else _validated_schema(raw_config, "config_schema", source)
    )
    input_schema = _validated_schema(document["input_schema"], "input_schema", source)
    output_schema = _validated_schema(
        document["output_schema"], "output_schema", source
    )

    return CapabilityDescriptor(
        identifier=identifier,
        version=version,
        config_schema=config_schema,
        input_schema=input_schema,
        output_schema=output_schema,
        execution_environments=tuple(raw_environments),
        effect_class=effect,
        resource_scope=resource_scope,
        required_grants=required_grants,
    )


def _canonical_call_schemas(
    descriptor: CapabilityDescriptor,
) -> tuple[str, str]:
    """Canonical JSON for the call contract (input/output schemas)."""
    return (
        json.dumps(
            descriptor.input_schema, sort_keys=True, separators=(",", ":")
        ),
        json.dumps(
            descriptor.output_schema, sort_keys=True, separators=(",", ":")
        ),
    )


class CapabilityRegistry:
    """In-process store of validated descriptors (declarative only).

    Registration is atomic: descriptor parsing, duplicate check and
    same-major compatibility check all complete before the single state
    mutation, so a rejected registration cannot leave a partially
    enabled tool. Reads return immutable snapshots; :meth:`list_capabilities`
    output is safe for UI/model tool selection (declarative fields only).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._descriptors: dict[tuple[str, str], CapabilityDescriptor] = {}
        # (identifier, major) -> call contract pinned by the first
        # registration in that major (canonical input/output JSON).
        self._major_contracts: dict[tuple[str, int], tuple[str, str]] = {}

    def __repr__(self) -> str:
        return f"<CapabilityRegistry descriptors={len(self._descriptors)}>"

    def __len__(self) -> int:
        return len(self._descriptors)

    def register(
        self, descriptor: Mapping[str, object] | CapabilityDescriptor
    ) -> CapabilityDescriptor:
        """Validate and store one descriptor; return the stored record.

        Raises :class:`CapabilityError` with ``duplicate_registration``
        (same identifier+version already registered, regardless of
        content) or ``incompatible_version`` (same identifier and major
        with a different input/output schema than the line's pinned call
        contract).
        """
        parsed = (
            descriptor
            if isinstance(descriptor, CapabilityDescriptor)
            else parse_descriptor(descriptor)
        )
        key = (parsed.identifier, parsed.version)
        line = (parsed.identifier, parsed.major)
        contract = _canonical_call_schemas(parsed)
        with self._lock:
            if key in self._descriptors:
                raise CapabilityError(
                    CapabilityErrorCode.DUPLICATE_REGISTRATION,
                    f"{parsed.identifier}@{parsed.version} is already "
                    "registered; versions are immutable, publish a new "
                    "version instead",
                )
            pinned = self._major_contracts.get(line)
            if pinned is not None and pinned != contract:
                raise CapabilityError(
                    CapabilityErrorCode.INCOMPATIBLE_VERSION,
                    f"{parsed.identifier}@{parsed.version} breaks the "
                    f"call contract pinned for major {parsed.major}; a "
                    "breaking input/output schema change requires a new "
                    "major version",
                )
            self._descriptors[key] = parsed
            self._major_contracts.setdefault(line, contract)
            return parsed

    def get(self, identifier: str, version: str) -> CapabilityDescriptor | None:
        """The stored descriptor for one ``(identifier, version)``."""
        with self._lock:
            return self._descriptors.get((identifier, version))

    def versions(self, identifier: str) -> tuple[str, ...]:
        """All registered versions of one identifier, semver-sorted."""
        with self._lock:
            found = [
                version
                for (found_id, version) in self._descriptors
                if found_id == identifier
            ]
        return tuple(
            sorted(found, key=lambda v: tuple(int(p) for p in v.split(".")))
        )

    def list_capabilities(self) -> tuple[CapabilityDescriptor, ...]:
        """Immutable snapshot sorted by ``(identifier, version)``.

        Safe for UI/model tool selection: descriptors are declarative
        metadata (the strict vocabulary structurally excludes code
        references and credential values) and are handed out as
        immutable records with per-call fresh JSON copies.
        """
        with self._lock:
            snapshot = tuple(
                sorted(
                    self._descriptors.values(),
                    key=lambda d: (d.identifier, d.version_tuple),
                )
            )
        return snapshot


# ---------------------------------------------------------------------------
# Trusted native-module loading (C03 rule 7)
# ---------------------------------------------------------------------------


def descriptor_from_module(
    path: str, *, source: str | None = None
) -> CapabilityDescriptor:
    """Load a Python capability module and validate its declaration.

    Reads the module-level ``CAPABILITY`` mapping (see
    :data:`CAPABILITY_DECLARATION`) and runs it through the same
    :func:`parse_descriptor` validation as every other document. The
    import executes the module — trusted installed code by contract
    (C03 rule 7); this is loading, **not** sandboxing. The module object
    is dropped on return, nothing is inserted into ``sys.modules`` and
    the registry never stores a reference to module code: registration
    records metadata, it does not enable an executable.
    """
    file = Path(path)
    where = source or str(file)
    try:
        data = file.read_bytes()
    except OSError as error:
        raise CapabilityError(
            CapabilityErrorCode.MODULE_LOAD_ERROR,
            f"{where}: capability module cannot be read ({type(error).__name__})",
        ) from None
    if len(data) > MAX_CAPABILITY_MODULE_BYTES:
        raise CapabilityError(
            CapabilityErrorCode.MODULE_LOAD_ERROR,
            f"{where}: capability module exceeds "
            f"{MAX_CAPABILITY_MODULE_BYTES} bytes",
        )
    digest = hashlib.sha256(file.resolve().as_posix().encode("utf-8")).hexdigest()[:12]
    spec = spec_from_file_location(f"_mai_capability_{digest}", file)
    if spec is None or spec.loader is None:
        raise CapabilityError(
            CapabilityErrorCode.MODULE_LOAD_ERROR,
            f"{where}: not importable as a Python module",
        )
    module = module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # trusted code; classify, never propagate raw
        raise CapabilityError(
            CapabilityErrorCode.MODULE_LOAD_ERROR,
            f"{where}: import raised {type(error).__name__}",
        ) from None
    declaration = getattr(module, CAPABILITY_DECLARATION, None)
    if declaration is None:
        raise CapabilityError(
            CapabilityErrorCode.DECLARATION_MISSING,
            f"{where}: module does not declare {CAPABILITY_DECLARATION}",
        )
    return parse_descriptor(declaration, source=where)


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mai.plugins.registry",
        description=(
            "Validate capability descriptors or load a capability module "
            "declaration. Structured JSON output only; descriptors are "
            "declarative metadata, registration never executes anything."
        ),
    )
    parser.add_argument("command", choices=["validate", "load-module"])
    parser.add_argument("path", nargs="+", help="descriptor JSON file(s) / module path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: 0 success, 1 structured rejection, 2 usage error."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    any_invalid = False
    for raw_path in args.path:
        if args.command == "load-module":
            try:
                descriptor = descriptor_from_module(raw_path)
            except CapabilityError as error:
                _emit(
                    {
                        "kind": "error",
                        "file": raw_path,
                        "code": error.code.value,
                        "detail": error.detail,
                    }
                )
                any_invalid = True
                continue
            _emit(
                {
                    "kind": "ok",
                    "file": raw_path,
                    "identifier": descriptor.identifier,
                    "version": descriptor.version,
                    "effect_class": descriptor.effect_class,
                }
            )
            continue
        try:
            document = json.loads(Path(raw_path).read_text(encoding="utf-8"))
            descriptor = parse_descriptor(document, source=raw_path)
        except json.JSONDecodeError:
            _emit(
                {
                    "kind": "error",
                    "file": raw_path,
                    "code": CapabilityErrorCode.INVALID_JSON.value,
                    "detail": f"{raw_path}: not valid JSON",
                }
            )
            any_invalid = True
            continue
        except OSError as error:
            _emit(
                {
                    "kind": "error",
                    "file": raw_path,
                    "code": CapabilityErrorCode.NOT_READABLE.value,
                    "detail": f"{raw_path}: cannot be read "
                    f"({type(error).__name__})",
                }
            )
            any_invalid = True
            continue
        except CapabilityError as error:
            _emit(
                {
                    "kind": "error",
                    "file": raw_path,
                    "code": error.code.value,
                    "detail": error.detail,
                }
            )
            any_invalid = True
            continue
        _emit(
            {
                "kind": "ok",
                "file": raw_path,
                "identifier": descriptor.identifier,
                "version": descriptor.version,
                "effect_class": descriptor.effect_class,
            }
        )
    return 1 if any_invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
