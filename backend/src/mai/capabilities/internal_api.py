"""Internal API → Agent Tool interface (C09 service adoption & operations).

One declared internal API operation produces ONE ``CapabilityDescriptor`` +
``CapabilityExecutor`` pair through a single shared builder, so a new internal
API (service management, inventory, lifecycle, monitoring, etc.) becomes an
agent-chat tool automatically the moment it is declared here — no manual
wiring across ``app.py``, ``worker.py``, ``chat.py``, and the UI.

The module owns the complete ``app.*`` internal-API capability surface
(service adoption/unadoption, discovery, inventory listing, Service Map CRUD)
and guarantees three contracts:

1. **Schema compatibility (C03 rule 1 / bridge contract).** Descriptors are
   generated with :func:`pydantic_to_descriptor_schema`, which projects a
   Pydantic ``BaseModel`` onto the strict subset of JSON Schema that
   :class:`~mai.agents.tools.CapabilityToolBridge._validate_payload` and
   :func:`~mai.plugins.registry.parse_descriptor` accept (``type``,
   ``properties``, ``required``, ``additionalProperties: False``, ``enum``,
   ``minLength``, ``maxLength``, ``minimum``, ``maximum``, ``pattern``,
   ``items``, ``minItems``, ``maxItems``). No ``$defs`` / ``anyOf`` /
   ``format`` keywords ever reach the registry.
2. **Outcome translation (DBOS jobs).** Every executor returns a
   ``{"status": "ok", ...}`` envelope on success and
   ``{"status": "unavailable"|"refused", "code": ..., ...}`` on failure, the
   exact shape ``mai.jobs.steps._translate_outcome`` resolves to
   ``StepState.SUCCEEDED`` / ``StepState.FAILED`` without falling into
   ``status_unresolved``.
3. **Composition parity (app + worker).** One builder
   :func:`build_internal_api_capabilities` produces the descriptor→executor
   mapping and the default-capability list once; ``mai.api.app`` and
   ``mai.jobs.worker`` consume the SAME result so the HTTP dispatcher and the
   DBOS worker can never drift apart (the ``app.upsert_map_entry`` worker
   omission this module closes).

The dispatcher still owns every authorization decision (grants, policy,
adoption probe, approval binding); executors run only after the decision
passes (C03 rule 6).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Final,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel, Field, ValidationError

from mai.applications.service_map import (
    CAPABILITY_ID as UPSERT_MAP_CAPABILITY,
    upsert_map_descriptor,
)
from mai.jobs.resource_locks import ResourceLockService
from mai.capabilities.chat_read import (
    CAPABILITY_ID as CHAT_READ_CAPABILITY,
    chat_read_descriptor,
)
from mai.capabilities.dispatch import CapabilityCall, ExecutorResult
from mai.plugins.registry import (
    CapabilityDescriptor,
    CapabilityRegistry,
    parse_descriptor,
)

if TYPE_CHECKING:  # composition-time types only; no runtime import cycles
    from mai.api.applications import (
        ApplicationAdoption,
        ApplicationDocService,
        ApplicationManifestStore,
    )
    from mai.applications.metadata_mutations import MetadataMutationCoordinator
    from mai.applications.service_map import ServiceMapStore
    from mai.knowledge.index import ScopeIndexer

# ---------------------------------------------------------------------------
# Capability identifiers (frozen vocabulary)
# ---------------------------------------------------------------------------

APP_DISCOVER_CAPABILITY: Final = "app.discover"
APP_LIST_ADOPTED_CAPABILITY: Final = "app.list_adopted"
APP_ADOPT_CAPABILITY: Final = "app.adopt"
APP_UNADOPT_CAPABILITY: Final = "app.unadopt"
APP_DELETE_MAP_ENTRY_CAPABILITY: Final = "app.delete_map_entry"
APP_LIST_CATALOG_CAPABILITY: Final = "app.list_catalog"
APP_UPSERT_CATALOG_RUNBOOK_CAPABILITY: Final = "app.upsert_catalog_runbook"
APP_DELETE_CATALOG_RUNBOOK_CAPABILITY: Final = "app.delete_catalog_runbook"
DOCS_LIST_CAPABILITY: Final = "docs.list"
DOCS_READ_CAPABILITY: Final = "docs.read"
DOCS_UPSERT_CAPABILITY: Final = "docs.upsert"
DOCS_DELETE_CAPABILITY: Final = "docs.delete"

#: FRR T5: profile file-read-root extension. SECURITY: forced ASK prompting
#: at the bridge seam (agents/tools.py dispatch_once) — interactive sessions
#: run DELEGATED, where a plain mutate would self-execute.
SERVER_PROFILE_UPDATE_CAPABILITY: Final = "server.profile.update"

#: The internal-API capability set the chat agent exposes by default across
#: both pinned-server conversations and General Consult (the UI/agent surface
#: parity anchor). ``server.profile.update`` is included by captain decision:
#: it is auto-granted to enrolled servers out of the box, and safe precisely
#: because every call is forced through captain approval.
INTERNAL_API_CAPABILITY_IDS: Final[tuple[str, ...]] = (
    APP_DISCOVER_CAPABILITY,
    APP_LIST_ADOPTED_CAPABILITY,
    APP_ADOPT_CAPABILITY,
    APP_UNADOPT_CAPABILITY,
    UPSERT_MAP_CAPABILITY,
    APP_DELETE_MAP_ENTRY_CAPABILITY,
    APP_LIST_CATALOG_CAPABILITY,
    APP_UPSERT_CATALOG_RUNBOOK_CAPABILITY,
    APP_DELETE_CATALOG_RUNBOOK_CAPABILITY,
    DOCS_LIST_CAPABILITY,
    DOCS_READ_CAPABILITY,
    DOCS_UPSERT_CAPABILITY,
    DOCS_DELETE_CAPABILITY,
    CHAT_READ_CAPABILITY,
    SERVER_PROFILE_UPDATE_CAPABILITY,
)

# ---------------------------------------------------------------------------
# Descriptor-spec vocabulary (the decorator's frozen keyword set)
# ---------------------------------------------------------------------------

_EFFECT_CLASSES: Final[frozenset[str]] = frozenset({"read", "mutate", "privileged"})
_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"controller", "linux_ssh"})

#: JSON Schema keywords the descriptor registry + tool bridge accept. Anything
#: else in a generated property is stripped by :func:`pydantic_to_descriptor_schema`
#: (fail-closed would be wrong here: ``format``/``$defs``/``anyOf`` are
#: producer-side annotations, not payload contract).
_ALLOWED_SCHEMA_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "pattern",
        # annotation-only keywords parse_descriptor + the bridge tolerate:
        "title",
        "description",
        "default",
        "examples",
        "$comment",
    }
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InternalApiError(Exception):
    """Structured domain rejection of one internal-API capability call.

    ``code`` is a stable, value-free machine token the model sees in the
    refused envelope; ``detail`` is the human-facing explanation.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Pydantic → descriptor-schema projection
# ---------------------------------------------------------------------------


def _unwrap_optional(annotation: object) -> object:
    """Unwrap Optional[X] / X | None to X when there is a single non-None member."""
    origin = get_origin(annotation)
    if str(origin) in ("typing.Union", "types.UnionType") or origin is getattr(
        __import__("typing"), "Union", None
    ):
        members = [m for m in get_args(annotation) if m is not type(None)]
        if len(members) == 1:
            return members[0]
    return annotation


def _flatten_json_type(annotation: object) -> tuple[str, ...] | None:
    """Project one Python/Pydantic annotation onto descriptor JSON types.

    Optional[X] / X | None flatten to X's type (the bridge's strict schema
    has no nullable keyword — optionality is expressed by absence from
    ``required``). Unions of ≥2 non-None members are refused (the agent tool
    surface has no ``anyOf``). Unsupported annotations yield ``None`` so the
    caller can decide (default: ``string`` coercion would be a lie).
    """
    origin = get_origin(annotation)
    if origin is None:
        if annotation is str:
            return ("string",)
        if annotation is bool:
            return ("boolean",)
        if annotation is int:
            return ("integer",)
        if annotation is float:
            return ("number",)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return ("object",)
        return None
    if getattr(origin, "__qualname__", "") == "Literal":
        values = get_args(annotation)
        if values and all(isinstance(v, str) for v in values):
            return ("string",)
        if values and all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            return ("integer",)
        return ("string",)
    # Optional / Union flattening
    if str(origin) in ("typing.Union", "types.UnionType") or origin is getattr(
        __import__("typing"), "Union", None
    ):
        members = [m for m in get_args(annotation) if m is not type(None)]
        if len(members) != 1:
            return None  # genuine union of several types: refuse (no anyOf)
        return _flatten_json_type(members[0])
    if origin in (list, Sequence, tuple, frozenset, set):
        return ("array",)
    if origin in (dict, Mapping):
        return ("object",)
    return None


def _project_property(
    annotation: object, field_schema: Mapping[str, Any]
) -> dict[str, Any]:
    """Project one model field onto the descriptor schema subset.

    Priority: the field's JSON-schema fragment (from Pydantic) for the base
    shape, then the annotation-derived JSON type as the fallback for fields
    Pydantic rendered as ``$ref`` / ``anyOf`` (enums, nested models, unions).
    Every keyword outside :data:`_ALLOWED_SCHEMA_KEYWORDS` is dropped.
    """
    projected: dict[str, Any] = {}
    core_annotation = _unwrap_optional(annotation)

    # Enum literals (Literal[...] and Enum subclasses) — the pattern that
    # Pydantic renders as {"enum": [...]} directly or via $defs/anyOf.
    enum_values = field_schema.get("enum")
    if isinstance(enum_values, list):
        projected["enum"] = list(enum_values)
    else:
        ann_enum = _enum_values_of(core_annotation)
        if ann_enum is not None:
            projected["enum"] = ann_enum

    # Base JSON type: annotation first (authoritative for the bridge), then
    # the Pydantic fragment's own "type" when the annotation is opaque.
    json_types = _flatten_json_type(core_annotation)
    if json_types is not None:
        projected["type"] = json_types[0] if len(json_types) == 1 else list(json_types)
    elif isinstance(field_schema.get("type"), str) and field_schema["type"] in (
        "object",
        "array",
        "string",
        "boolean",
        "null",
        "integer",
        "number",
    ):
        projected["type"] = field_schema["type"]

    # Array items: project the item annotation's enum/type/nested BaseModel
    # when known; an unconstrained item stays an open object.
    if projected.get("type") == "array":
        origin = get_origin(core_annotation)
        args = get_args(core_annotation)
        if origin in (list, Sequence, tuple, frozenset, set) and args:
            item_annotation = _unwrap_optional(args[0])
            if isinstance(item_annotation, type) and issubclass(
                item_annotation, BaseModel
            ):
                projected["items"] = _project_model_schema(item_annotation)
            else:
                item_enum = _enum_values_of(item_annotation)
                item_type = _flatten_json_type(item_annotation)
                item_schema: dict[str, Any] = {}
                if item_enum is not None:
                    item_schema["enum"] = item_enum
                if item_type is not None:
                    item_schema["type"] = item_type[0]
                if item_schema:
                    projected["items"] = item_schema

    # Nested object properties (inline BaseModel field or Optional[BaseModel]):
    # Pydantic inlines them as {"$defs":..., "allOf":[{"$ref":...}]} — we
    # re-project through the referenced model's own schema before copying
    # field-level annotations so the outer field's description wins.
    if isinstance(core_annotation, type) and issubclass(core_annotation, BaseModel):
        projected.update(_project_model_schema(core_annotation))

    # Scalar bounds and annotations (carry only the allowed subset).
    for keyword in (
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "pattern",
        "minItems",
        "maxItems",
        "description",
        "default",
        "examples",
        "title",
    ):
        if keyword in field_schema:
            projected[keyword] = field_schema[keyword]

    if not projected:
        # Last resort: an unconstrained field is a string slot (never a lie —
        # the descriptor requires the field only when the model does).
        projected["type"] = "string"
    return projected


def _enum_values_of(annotation: object) -> list[Any] | None:
    """The literal enum values of one annotation, when it is Literal/Enum."""
    origin = get_origin(annotation)
    if origin is not None and getattr(origin, "__qualname__", "") == "Literal":
        return [value for value in get_args(annotation)]
    if isinstance(annotation, type):
        import enum

        if issubclass(annotation, enum.Enum):
            return [member.value for member in annotation]
    return None


def _project_model_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Project one Pydantic model onto the descriptor object schema."""
    raw = model.model_json_schema()
    properties: dict[str, Any] = {}
    fields = model.model_fields
    for name, fragment in (raw.get("properties") or {}).items():
        field = fields.get(name)
        annotation = field.annotation if field is not None else str
        properties[name] = _project_property(annotation, fragment)
    required = list(raw.get("required") or [])
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def pydantic_to_descriptor_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Project a Pydantic request model onto the bridge-compatible subset.

    The result carries ONLY keywords in :data:`_ALLOWED_SCHEMA_KEYWORDS`;
    ``$defs``, ``anyOf``, ``format``, ``$ref`` never reach the registry.
    A model with zero fields produces the empty-object schema (no-argument
    tools such as ``app.discover``/``app.list_adopted``).
    """
    schema = _project_model_schema(model)
    # Strip any keyword the bridge/registry reject (defense in depth — the
    # projection above already filters, the strip makes the contract local).
    return {k: v for k, v in schema.items() if k in _ALLOWED_SCHEMA_KEYWORDS}


# ---------------------------------------------------------------------------
# The declared-operation record and the generic executor
# ---------------------------------------------------------------------------


class InternalApiOperation(Protocol):
    """The one async callable an executor binds (payload → result document).

    Implementations raise :class:`InternalApiError` for structured refusals
    (the executor maps them onto the refused envelope) and return a JSON-
    serializable document on success (the executor wraps it in
    ``{"status": "ok", ...}``).
    """

    async def __call__(
        self, call: CapabilityCall, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class InternalApiSpec:
    """One declared internal API → capability binding (frozen)."""

    capability_id: str
    version: str
    effect_class: str
    resource_scope: tuple[str, ...]
    environments: tuple[str, ...]
    payload_model: type[BaseModel]
    description: str


def _check_capability_id(value: str) -> str:
    """The registry's identifier shape (fail-closed at declaration time)."""
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]*", value):
        raise ValueError(f"capability id must match the registry slug shape: {value!r}")
    return value


def _check_effect_class(value: str) -> str:
    if value not in _EFFECT_CLASSES:
        raise ValueError(
            f"effect_class must be one of {sorted(_EFFECT_CLASSES)}: {value!r}"
        )
    return value


def _check_environments(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values:
        raise ValueError("at least one execution environment is required")
    for value in values:
        if value not in _ENVIRONMENTS:
            raise ValueError(
                f"environment must be one of {sorted(_ENVIRONMENTS)}: {value!r}"
            )
    return values


MIN_TOOL_DESCRIPTION_CHARS: Final[int] = 30
MIN_FIELD_DESCRIPTION_CHARS: Final[int] = 12


def _check_tool_description(capability_id: str, description: object) -> str:
    """Enforce that the endpoint/tool description is meaningful for LLM tool matching."""
    if not isinstance(description, str) or len(description.strip()) < MIN_TOOL_DESCRIPTION_CHARS:
        raise ValueError(
            f"{capability_id}: tool description must be a non-empty string of at least "
            f"{MIN_TOOL_DESCRIPTION_CHARS} characters explaining what the tool does"
        )
    cleaned = description.strip()
    if cleaned.lower() == capability_id.lower():
        raise ValueError(
            f"{capability_id}: tool description must not merely repeat capability_id"
        )
    return cleaned


def _check_model_field_descriptions(
    capability_id: str,
    model: type[BaseModel],
    *,
    prefix: str = "",
) -> None:
    """Recursively enforce that every field on ``model`` (and any nested BaseModel)
    carries an explicit, LLM-meaningful ``Field(description=...)`` annotation.
    """
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise ValueError(
            f"{capability_id}: payload_model must be a Pydantic BaseModel subclass"
        )
    for field_name, field_info in model.model_fields.items():
        path = f"{prefix}.{field_name}" if prefix else field_name
        desc = field_info.description
        if not isinstance(desc, str) or len(desc.strip()) < MIN_FIELD_DESCRIPTION_CHARS:
            raise ValueError(
                f"{capability_id}: parameter '{path}' on {model.__name__} must declare "
                f"Field(description=...) of at least {MIN_FIELD_DESCRIPTION_CHARS} characters"
            )
        if desc.strip().lower().replace("_", " ") == field_name.lower().replace("_", " "):
            raise ValueError(
                f"{capability_id}: parameter '{path}' description must not merely repeat the field name"
            )
        core = _unwrap_optional(field_info.annotation)
        if isinstance(core, type) and issubclass(core, BaseModel):
            _check_model_field_descriptions(capability_id, core, prefix=path)
        else:
            origin = get_origin(core)
            args = get_args(core)
            if origin in (list, Sequence, tuple, frozenset, set) and args:
                item_core = _unwrap_optional(args[0])
                if isinstance(item_core, type) and issubclass(item_core, BaseModel):
                    _check_model_field_descriptions(
                        capability_id, item_core, prefix=f"{path}[]"
                    )


def _check_schema_property_descriptions(
    capability_id: str,
    properties: Mapping[str, Any],
    *,
    prefix: str = "",
) -> None:
    """Recursively verify that every property in a compiled JSON Schema object has a description."""
    for prop_name, prop_schema in properties.items():
        path = f"{prefix}.{prop_name}" if prefix else prop_name
        if not isinstance(prop_schema, Mapping):
            raise ValueError(f"{capability_id}: schema property '{path}' must be a mapping")
        desc = prop_schema.get("description")
        if not isinstance(desc, str) or len(desc.strip()) < MIN_FIELD_DESCRIPTION_CHARS:
            raise ValueError(
                f"{capability_id}: compiled schema property '{path}' is missing a description "
                f"of at least {MIN_FIELD_DESCRIPTION_CHARS} characters"
            )
        nested_props = prop_schema.get("properties")
        if isinstance(nested_props, Mapping):
            _check_schema_property_descriptions(
                capability_id, nested_props, prefix=path
            )
        items_schema = prop_schema.get("items")
        if isinstance(items_schema, Mapping):
            item_props = items_schema.get("properties")
            if isinstance(item_props, Mapping):
                _check_schema_property_descriptions(
                    capability_id, item_props, prefix=f"{path}[]"
                )


def assert_agent_ready_descriptor(descriptor: CapabilityDescriptor) -> CapabilityDescriptor:
    """Fail-closed gate verifying a ``CapabilityDescriptor`` has complete LLM-ready
    endpoint and per-parameter descriptions (including nested objects and array items).
    """
    cid = descriptor.identifier
    schema = descriptor.input_schema
    if not isinstance(schema, Mapping):
        raise ValueError(f"{cid}: input_schema must be a JSON object mapping")
    _check_tool_description(cid, schema.get("description"))
    props = schema.get("properties")
    if isinstance(props, Mapping):
        _check_schema_property_descriptions(cid, props)
    return descriptor


def internal_api_tool(
    *,
    capability_id: str,
    version: str,
    effect_class: str,
    resource_scope: Sequence[str],
    environments: Sequence[str],
    payload_model: type[BaseModel],
    description: str,
) -> Callable[[InternalApiOperation], tuple[InternalApiSpec, InternalApiOperation]]:
    """Declare one internal API operation as an agent tool.

    Decorates the operation's async implementation; returns the frozen
    ``(spec, operation)`` pair :func:`build_internal_api_capabilities`
    registers. Mis-shaped declarations — including missing or insufficient
    endpoint or parameter descriptions — raise ``ValueError`` at decoration/import
    time (fail-closed composition).
    """

    checked_id = _check_capability_id(capability_id)
    checked_effect = _check_effect_class(effect_class)
    checked_envs = _check_environments(tuple(environments))
    checked_scope = tuple(resource_scope)
    checked_desc = _check_tool_description(checked_id, description)
    _check_model_field_descriptions(checked_id, payload_model)

    def _wrap(
        operation: InternalApiOperation,
    ) -> tuple[InternalApiSpec, InternalApiOperation]:
        spec = InternalApiSpec(
            capability_id=checked_id,
            version=version,
            effect_class=checked_effect,
            resource_scope=checked_scope,
            environments=checked_envs,
            payload_model=payload_model,
            description=checked_desc,
        )
        return spec, operation

    return _wrap


def build_descriptor(spec: InternalApiSpec) -> CapabilityDescriptor:
    """The validated registry descriptor of one declared operation."""
    _check_tool_description(spec.capability_id, spec.description)
    _check_model_field_descriptions(spec.capability_id, spec.payload_model)
    descriptor = parse_descriptor(
        {
            "identifier": spec.capability_id,
            "version": spec.version,
            "config_schema": None,
            "input_schema": {
                **pydantic_to_descriptor_schema(spec.payload_model),
                "description": spec.description,
            },
            "output_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": {"type": "string"},
                },
                "required": ["status"],
            },
            "resource_scope": list(spec.resource_scope),
            "effect_class": spec.effect_class,
            "execution_environments": list(spec.environments),
            "required_grants": [spec.capability_id],
        },
        source=f"internal_api:{spec.capability_id}",
    )
    return assert_agent_ready_descriptor(descriptor)


#: Refusal-message bounds: report the first few errors and cap each part,
#: so even a pathological payload produces a bounded refusal.
_MAX_REPORTED_VALIDATION_ERRORS: Final[int] = 6
_MAX_VALIDATION_GUIDANCE_CHARS: Final[int] = 200
_MAX_VALIDATION_MESSAGE_CHARS: Final[int] = 1200

_UUID_FORMAT_TEXT = (
    "must be a canonical UUID string "
    "(8-4-4-4-12 hexadecimal, e.g. str(uuid.uuid4()))"
)


def _annotation_guidance(annotation: object) -> str | None:
    """Type-level correction guidance for UUID / enum-annotated fields."""
    if isinstance(annotation, type) and issubclass(annotation, uuid.UUID):
        return _UUID_FORMAT_TEXT
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return "must be one of: " + ", ".join(item.value for item in annotation)
    origin = get_origin(annotation)
    if origin is Union:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return _annotation_guidance(args[0])
    return None


def _field_guidance(
    payload_model: type[BaseModel], loc: tuple[Any, ...]
) -> str | None:
    """Guidance for the payload-model field at ``loc`` (sequence indices skipped)."""
    target: object = payload_model
    for part in loc:
        if isinstance(part, int):
            continue
        if not (isinstance(target, type) and issubclass(target, BaseModel)):
            return None
        field = target.model_fields.get(str(part))
        if field is None:
            return None
        target = field.annotation
    return _annotation_guidance(target)


def _render_error_loc(loc: tuple[Any, ...]) -> str:
    """One dotted payload path with sequence indices (``evidence_refs[0].kind``)."""
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += ("." if path else "") + str(part)
    return path or "payload"


def _validation_refusal_message(
    payload_model: type[BaseModel], exc: ValidationError
) -> str:
    """Field-specific, model-correctable refusal text for one rejected payload.

    Names each offending field and the required format/vocabulary (UUID
    shape, allowed enum values) so a model can fix and re-send WITHOUT
    guessing. Value-free by construction: neither the supplied values
    (possibly secret-shaped) nor validator internals or stack details are
    ever echoed; the envelope code stays ``invalid_request``.
    """
    errors = exc.errors(
        include_url=False, include_context=False, include_input=False
    )
    parts: list[str] = []
    for error in errors[:_MAX_REPORTED_VALIDATION_ERRORS]:
        loc = tuple(error.get("loc", ()))
        guidance = _field_guidance(payload_model, loc)
        if guidance is None:
            error_type = error.get("type", "")
            if error_type == "missing":
                guidance = "this field is required"
            elif error_type == "extra_forbidden":
                guidance = "unexpected field (payloads reject extra inputs)"
            else:
                guidance = str(error.get("msg", "invalid value")).removeprefix(
                    "Value error, "
                )
        parts.append(
            f"{_render_error_loc(loc)}: {guidance}"[:_MAX_VALIDATION_GUIDANCE_CHARS]
        )
    if len(errors) > _MAX_REPORTED_VALIDATION_ERRORS:
        parts.append(
            f"payload: {len(errors) - _MAX_REPORTED_VALIDATION_ERRORS} "
            "more invalid fields"
        )
    return ("invalid request payload — " + "; ".join(parts))[
        :_MAX_VALIDATION_MESSAGE_CHARS
    ]


def build_executor(
    spec: InternalApiSpec, operation: InternalApiOperation
) -> Callable[[CapabilityCall], Any]:
    """The uniform executor: validate → run → wrap the outcome envelope.

    ``InternalApiError`` maps to ``{"status": "refused", "code": ...}``;
    unexpected exceptions surface as ``{"status": "unavailable", "code":
    "internal_error"}`` with the exception type name only (C01: capability
    failures are value-free over the wire).
    """

    async def _execute(call: CapabilityCall) -> ExecutorResult:
        try:
            payload_model = spec.payload_model.model_validate(call.payload)
        except ValidationError as exc:
            envelope = {
                "status": "refused",
                "code": "invalid_request",
                "message": _validation_refusal_message(spec.payload_model, exc),
                "capability_id": spec.capability_id,
            }
            return ExecutorResult(ok=False, output=json.dumps(envelope, sort_keys=True))
        except Exception:
            envelope = {
                "status": "refused",
                "code": "invalid_request",
                "message": "payload failed request-model validation",
                "capability_id": spec.capability_id,
            }
            return ExecutorResult(ok=False, output=json.dumps(envelope, sort_keys=True))
        try:
            result = await operation(call, payload_model.model_dump(mode="json"))
        except InternalApiError as exc:
            envelope = {
                "status": "refused",
                "code": exc.code,
                "message": exc.detail,
                "capability_id": spec.capability_id,
            }
            return ExecutorResult(ok=False, output=json.dumps(envelope, sort_keys=True))
        except Exception as exc:  # infrastructure: type name only
            envelope = {
                "status": "unavailable",
                "code": "internal_error",
                "message": type(exc).__name__,
                "capability_id": spec.capability_id,
            }
            return ExecutorResult(ok=False, output=json.dumps(envelope, sort_keys=True))
        envelope = _bound_internal_api_envelope({"status": "ok", **dict(result)})
        return ExecutorResult(ok=True, output=json.dumps(envelope, sort_keys=True))

    return _execute


def _bound_internal_api_envelope(
    envelope: dict[str, Any], max_bytes: int = 60000
) -> dict[str, Any]:
    """Guarantee that the JSON-serialized envelope stays strictly under the
    dispatcher's 64 KiB output budget so ``CapabilityDispatcher._bound``
    never slices the JSON string in half (which would cause ``status_unresolved``).
    """
    raw = json.dumps(envelope, sort_keys=True).encode("utf-8")
    if len(raw) <= max_bytes:
        return envelope

    # 1. If listen_ports is present and large, cap it first.
    if isinstance(envelope.get("listen_ports"), list) and len(envelope["listen_ports"]) > 25:
        envelope["listen_ports_total"] = len(envelope["listen_ports"])
        envelope["listen_ports"] = envelope["listen_ports"][:25]
        envelope["listen_ports_truncated"] = True
        raw = json.dumps(envelope, sort_keys=True).encode("utf-8")
        if len(raw) <= max_bytes:
            return envelope

    # 2. If applications have verbose container/port details (e.g. app.discover),
    # compact the nested container lists before trimming the applications list itself.
    if isinstance(envelope.get("applications"), list):
        compact_apps: list[Any] = []
        for app in envelope["applications"]:
            if not isinstance(app, dict):
                compact_apps.append(app)
                continue
            app_copy = dict(app)
            detail = app_copy.get("detail")
            if isinstance(detail, dict):
                detail_copy = dict(detail)
                if isinstance(detail_copy.get("containers"), list):
                    detail_copy["containers"] = [
                        {
                            "name": c.get("name"),
                            "compose_service": c.get("compose_service"),
                            "image": c.get("image"),
                            "state": c.get("state"),
                        }
                        if isinstance(c, dict)
                        else c
                        for c in detail_copy["containers"][:8]
                    ]
                app_copy["detail"] = detail_copy
            compact_apps.append(app_copy)
        envelope["applications"] = compact_apps
        raw = json.dumps(envelope, sort_keys=True).encode("utf-8")
        if len(raw) <= max_bytes:
            return envelope

        # 3. Still over budget: strip detail.containers and cap applications list.
        for app_copy in compact_apps:
            if isinstance(app_copy, dict) and isinstance(app_copy.get("detail"), dict):
                app_copy["detail"].pop("containers", None)
        envelope["applications"] = compact_apps
        raw = json.dumps(envelope, sort_keys=True).encode("utf-8")
        if len(raw) <= max_bytes:
            return envelope

        while (
            len(envelope["applications"]) > 1
            and len(json.dumps(envelope, sort_keys=True).encode("utf-8")) > max_bytes
        ):
            envelope["applications_total"] = envelope.get(
                "applications_total", len(envelope["applications"])
            )
            envelope["applications"] = envelope["applications"][
                : max(1, len(envelope["applications"]) // 2)
            ]
            envelope["applications_truncated"] = True

    if isinstance(envelope.get("runbooks"), list):
        while (
            len(envelope["runbooks"]) > 1
            and len(json.dumps(envelope, sort_keys=True).encode("utf-8")) > max_bytes
        ):
            envelope["runbooks"] = envelope["runbooks"][
                : max(1, len(envelope["runbooks"]) // 2)
            ]
            envelope["has_more"] = True

    return envelope


# ---------------------------------------------------------------------------
# The app.* internal API operations (service adoption & operations surface)
# ---------------------------------------------------------------------------


class ServiceInstructionsPayload(BaseModel):
    """Operational runbook commands/procedures for a managed service."""

    model_config = {"extra": "forbid"}

    install: str = Field(
        default="",
        description="Step-by-step commands or procedure to install/deploy this service on the host.",
    )
    update: str = Field(
        default="",
        description="Step-by-step commands or procedure to update/upgrade this service safely.",
    )
    uninstall: str = Field(
        default="",
        description="Step-by-step commands or procedure to remove/uninstall this service cleanly.",
    )
    backup: str = Field(
        default="",
        description="Step-by-step commands or paths to back up this service's state and configuration.",
    )
    verify: str = Field(
        default="",
        description="Health check or verification commands to confirm this service is operating normally.",
    )


class ServiceCategoryPayload(BaseModel):
    """Custom Service Map category definition."""

    model_config = {"extra": "forbid"}

    machine_id: str = Field(
        description="Lowercase slug identifier for the category (e.g. 'automation' or 'devtools')."
    )
    label: str = Field(
        description="Human-readable category display title (e.g. 'Automation' or 'Developer Tools')."
    )
    description: str = Field(
        default="",
        description="Concise explanation of what kinds of services belong in this category.",
    )


class AppDiscoverPayload(BaseModel):
    """Discover live services on the target server (read-only).

    Scans Docker Compose projects, systemd units, and standalone containers.
    Optional ``origin`` filter ("compose", "systemd_unit", "standalone_container")
    and optional ``query`` substring filter narrow the returned applications.
    """

    model_config = {"extra": "forbid"}

    server_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical server UUID override. Omit when the chat "
            "conversation is already pinned to a server."
        ),
    )
    origin: Literal["compose", "systemd_unit", "standalone_container"] | None = Field(
        default=None,
        description=(
            "Optional origin filter: 'compose', 'systemd_unit', or "
            "'standalone_container'. Omit to return all origins."
        ),
    )
    query: str | None = Field(
        default=None,
        description=(
            "Optional case-insensitive substring filter on application/project/unit/container name."
        ),
    )
    include_ports: bool = Field(
        default=False,
        description=(
            "Include the raw host listen_ports table. Defaults to false to keep the response concise."
        ),
    )
    limit: int = Field(
        default=25,
        ge=1,
        le=100,
        description="Maximum number of discovered applications to return per page (default 25, max 100).",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Zero-based pagination offset for discovered applications (default 0).",
    )


class AppListAdoptedPayload(BaseModel):
    """List adopted applications and Service Map entries on the target server."""

    model_config = {"extra": "forbid"}

    server_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical server UUID override. Omit when the chat "
            "conversation is already pinned to a server."
        ),
    )
    limit: int | None = Field(
        default=25,
        ge=1,
        le=100,
        description="Optional maximum number of adopted entries to return per page (default 25, max 100).",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Zero-based pagination offset (default 0).",
    )


class AppAdoptPayload(BaseModel):
    """Adopt a service/application into Machina management and Service Map."""

    model_config = {"extra": "forbid"}

    server_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical server UUID override. Omit when the chat "
            "conversation is already pinned to a server."
        ),
    )
    application_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical UUID for the application. Omit to mint a "
            "deterministic UUID from (server_id, name)."
        ),
    )
    name: str = Field(
        description="Human-readable service display name (required, e.g. 'Immich' or 'omp (oh-my-pi)')."
    )
    origin: Literal["manual", "compose", "systemd_unit"] = Field(
        default="manual",
        description=(
            "Service origin: 'compose' (requires compose_project + working_dir), "
            "'systemd_unit' (requires unit_name), or 'manual' (for standalone "
            "CLI tools, binaries, or custom services)."
        ),
    )
    compose_project: str | None = Field(
        default=None,
        description="Docker Compose project name (required when origin='compose').",
    )
    working_dir: str | None = Field(
        default=None,
        description="Absolute path to the Compose working directory on the host (required when origin='compose').",
    )
    unit_name: str | None = Field(
        default=None,
        description="Systemd unit name ending in .service/.socket/.timer (required when origin='systemd_unit').",
    )
    external_working_dir: str | None = Field(
        default=None,
        description="Optional external working directory path on the host.",
    )
    external_config_files: list[str] = Field(
        default_factory=list,
        description="Optional list of absolute config file paths on the host.",
    )
    external_environment_files: list[str] = Field(
        default_factory=list,
        description="Optional list of absolute environment (.env) file paths on the host.",
    )
    expected_revision: int | None = Field(
        default=None,
        description=(
            "Optimistic concurrency revision: 0 when adopting a new service, "
            "the current revision when updating, or omit to auto-resolve."
        ),
    )
    category: str | None = Field(
        default=None,
        description=(
            "Service Map category slug (e.g. 'system', 'ai', 'media', 'dev', "
            "'infrastructure', 'observability', 'agent', 'games', or a custom slug). "
            "Omit to inherit from the global Runbook Catalog or existing entry."
        ),
    )
    description: str | None = Field(
        default=None,
        description=(
            "Concise summary of what the service does. Omit to inherit from "
            "the global Runbook Catalog or existing entry."
        ),
    )
    instructions: ServiceInstructionsPayload | None = Field(
        default=None,
        description=(
            "Operational runbook object with optional string keys: "
            "'install', 'update', 'uninstall', 'backup', 'verify'. Omit to "
            "inherit from the global Runbook Catalog or existing entry."
        ),
    )
    custom_instructions: str | None = Field(
        default=None,
        description=(
            "Server-specific operational instructions for this host (e.g. "
            "ports, bind addresses, config/env file paths, volume mounts, "
            "systemd overrides, backup targets), injected into the agent's "
            "@service context alongside the global catalog runbook. Omit to "
            "preserve the stored value."
        ),
    )
    installed: bool | None = Field(
        default=None,
        description=(
            "Whether the service is verified installed on this host (true "
            "when confirmed present on the system; false when staged from "
            "the catalog but not installed). Omit to preserve the stored value."
        ),
    )
    upsert_categories: list[ServiceCategoryPayload] = Field(
        default_factory=list,
        description=(
            "Optional list of custom categories to create/update: "
            "[{'machine_id': '...', 'label': '...', 'description': '...'}]."
        ),
    )


class AppListCatalogPayload(BaseModel):
    """List global Runbook Catalog recipes (fleet-wide, server-agnostic)."""

    model_config = {"extra": "forbid"}

    query: str | None = Field(
        default=None,
        max_length=256,
        description=(
            "Optional case-insensitive substring filter over catalog runbook "
            "names, categories, and descriptions."
        ),
    )
    category: str | None = Field(
        default=None,
        max_length=64,
        description="Optional category machine_id filter (e.g. 'media', 'ai', 'dev').",
    )
    limit: int = Field(
        default=25,
        ge=1,
        le=100,
        description="Maximum number of catalog runbooks to return per page (default 25, max 100).",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Zero-based pagination offset for catalog runbooks (default 0).",
    )


class AppUpsertCatalogRunbookPayload(BaseModel):
    """Add or update an application recipe in the global Runbook Catalog (catalog.json)."""

    model_config = {"extra": "forbid"}

    runbook_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical UUID of an existing catalog runbook to update. "
            "Omit to match by name or mint a deterministic catalog UUID5."
        ),
    )
    name: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Display name of the application/service in the global Runbook "
            "Catalog (e.g. 'Immich', 'Forgejo', 'Prometheus')."
        ),
    )
    category: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description=(
            "Service taxonomy category machine_id ('games', 'ai', 'agent', "
            "'system', 'dev', 'infrastructure', 'media', 'observability', or "
            "a custom slug). Defaults to deterministic classification or 'system'."
        ),
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "General, server-agnostic description of the application, its "
            "purpose, and architecture."
        ),
    )
    instructions: ServiceInstructionsPayload | None = Field(
        default=None,
        description=(
            "Global operational runbook instructions researched from official "
            "documentation: 'install', 'update', 'uninstall', 'backup', 'verify'."
        ),
    )
    upsert_categories: list[ServiceCategoryPayload] = Field(
        default_factory=list,
        description=(
            "Optional custom taxonomy categories to create or update before "
            "saving the catalog runbook."
        ),
    )


class AppDeleteCatalogRunbookPayload(BaseModel):
    """Delete an application recipe from the global Runbook Catalog (catalog.json only)."""

    model_config = {"extra": "forbid"}

    runbook_id: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Canonical UUID of the global catalog runbook to delete (from "
            "app.list_catalog). Per-server adopted entries are not affected."
        ),
    )


class AppUnadoptPayload(BaseModel):
    """Remove (unadopt/revoke) an application from Machina management and Service Map."""

    model_config = {"extra": "forbid"}

    server_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical server UUID override. Omit when the chat "
            "conversation is already pinned to a server."
        ),
    )
    application_id: str = Field(
        description="Canonical UUID of the adopted application to remove (from app.list_adopted)."
    )
    expected_revision: int | None = Field(
        default=None,
        description="Optional expected revision; auto-resolved from the current record when omitted.",
    )


class AppDeleteMapEntryPayload(BaseModel):
    """Remove a manual Service Map entry by application_id."""

    model_config = {"extra": "forbid"}

    server_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical server UUID override. Omit when the chat "
            "conversation is already pinned to a server."
        ),
    )
    application_id: str = Field(
        description="Canonical UUID of the manual Service Map entry to delete."
    )


class DocsListPayload(BaseModel):
    """List canonical Markdown documents and runbooks stored in the Documents section."""

    model_config = {"extra": "forbid"}

    scope: Literal["server", "shared"] = Field(
        default="server",
        description=(
            "Target document scope: 'server' (pinned conversation server or "
            "server_id) or 'shared' (fleet-wide shared documents store)."
        ),
    )
    server_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical server UUID override when scope='server'. "
            "Omit when the conversation is already pinned to a server."
        ),
    )
    app_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical application UUID to filter documents scoped "
            "to a specific adopted application on the server."
        ),
    )
    namespace: Literal["docs", "runbooks"] | None = Field(
        default=None,
        description=(
            "Optional collection filter: 'docs' (documentation, notes, "
            "incidents) or 'runbooks' (operational procedures). Omit for both."
        ),
    )
    query: str | None = Field(
        default=None,
        description=(
            "Optional case-insensitive substring filter matched against "
            "document title, Markdown content, origin, or doc_id."
        ),
    )
    limit: int = Field(
        default=25,
        ge=1,
        le=100,
        description="Maximum number of documents to return in one page (1..100, default 25).",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Zero-based pagination offset for the document list (default 0).",
    )


class DocsReadPayload(BaseModel):
    """Read the full hash-verified Markdown content and metadata of one canonical document."""

    model_config = {"extra": "forbid"}

    doc_id: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Canonical UUID of the document to read (obtained from docs.list "
            "or docs.retrieve)."
        ),
    )
    namespace: Literal["docs", "runbooks"] = Field(
        default="docs",
        description=(
            "Document collection namespace: 'docs' or 'runbooks' (default 'docs')."
        ),
    )
    scope: Literal["server", "shared"] = Field(
        default="server",
        description=(
            "Target document scope: 'server' (pinned conversation server or "
            "server_id) or 'shared' (fleet-wide shared documents store)."
        ),
    )
    server_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical server UUID override when scope='server'. "
            "Omit when the conversation is already pinned to a server."
        ),
    )
    app_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical application UUID when reading an "
            "application-scoped document on the server."
        ),
    )
    max_chars: int = Field(
        default=32000,
        ge=256,
        le=48000,
        description=(
            "Maximum number of Markdown characters to return in content "
            "(256..48000, default 32000)."
        ),
    )


class DocsUpsertPayload(BaseModel):
    """Create or update a canonical Markdown document or runbook in the Documents section."""

    model_config = {"extra": "forbid"}

    doc_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical UUID of an existing document to update. "
            "Omit to create a new document (or update an existing document "
            "with the same Markdown title in the same scope and namespace)."
        ),
    )
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Optional document title. If content does not start with a '# ' "
            "Markdown heading, this title is prepended as '# <title>'."
        ),
    )
    content: str = Field(
        min_length=1,
        max_length=200000,
        description=(
            "Full Markdown body of the document or operational runbook to "
            "store in the canonical Documents store."
        ),
    )
    namespace: Literal["docs", "runbooks"] = Field(
        default="docs",
        description=(
            "Target collection namespace: 'docs' (general documentation, "
            "architecture notes, incidents) or 'runbooks' (operational procedures)."
        ),
    )
    scope: Literal["server", "shared"] = Field(
        default="server",
        description=(
            "Target document scope: 'server' for the pinned/specified server, "
            "or 'shared' for fleet-wide shared documentation."
        ),
    )
    server_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical server UUID override when scope='server'. "
            "Omit when the conversation is already pinned to a server."
        ),
    )
    app_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical application UUID to scope the document to a "
            "specific adopted application on the server."
        ),
    )
    verification: Literal["unverified", "verified", "hypothesis"] = Field(
        default="unverified",
        description=(
            "Verification status: 'verified' (proven by host execution), "
            "'hypothesis' (unconfirmed theory/workaround), or 'unverified' "
            "(authored reference or note)."
        ),
    )
    expected_revision: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional optimistic concurrency revision check. When omitted on "
            "update, the current stored revision is resolved automatically."
        ),
    )


class DocsDeletePayload(BaseModel):
    """Delete a canonical Markdown document or runbook from the Documents section."""
    model_config = {"extra": "forbid"}

    doc_id: str = Field(
        min_length=1,
        max_length=64,
        description="Canonical UUID of the document to delete (from docs.list).",
    )
    namespace: Literal["docs", "runbooks"] = Field(
        default="docs",
        description=(
            "Document collection namespace: 'docs' or 'runbooks' (default 'docs')."
        ),
    )
    scope: Literal["server", "shared"] = Field(
        default="server",
        description=(
            "Target document scope: 'server' (pinned conversation server or "
            "server_id) or 'shared' (fleet-wide shared documents store)."
        ),
    )
    server_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical server UUID override when scope='server'. "
            "Omit when the conversation is already pinned to a server."
        ),
    )
    app_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical application UUID when deleting an "
            "application-scoped document on the server."
        ),
    )
    expected_revision: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional expected revision; auto-resolved from the current "
            "stored document when omitted."
        ),
    )


class ServerProfileUpdatePayload(BaseModel):
    """Request ADDING file-read roots to the target server's canonical profile.

    Add-only: the merged profile keeps every existing root; the request can
    never remove or reorder. There is deliberately NO ``server_id`` field —
    the target is the execution context's pinned server (same convention as
    ``host.file_read``). ``reason`` is shown to the captain in the approval
    preview and is otherwise without effect.
    """

    model_config = {"extra": "forbid"}

    roots: list[str] = Field(
        min_length=1,
        max_length=16,
        description=(
            "1..16 absolute normalized directory paths to ADD to the "
            "profile's file_read_roots (add-only; existing roots are kept)"
        ),
    )
    expected_revision: int = Field(
        ge=1,
        description=(
            "The profile revision the caller last observed; a stale value "
            "refuses with revision_conflict before any effect"
        ),
    )
    reason: str = Field(
        max_length=200,
        description=(
            "Why these read roots are needed; shown to the captain in the "
            "approval preview"
        ),
    )


# ---------------------------------------------------------------------------
# The shared composition builder (app.py + worker.py consume the SAME result)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InternalApiComposition:
    """The built internal-API capability surface: descriptors + executors."""

    descriptors: tuple[CapabilityDescriptor, ...]
    executors: dict[str, Callable[[CapabilityCall], Any]]


def _resolve_server_id(
    call: CapabilityCall, payload: Mapping[str, Any], capability_id: str
) -> uuid.UUID:
    """Resolve the target server_id from the payload override or call context."""
    raw = payload.get("server_id")
    if isinstance(raw, str) and raw.strip():
        try:
            sid = uuid.UUID(raw.strip())
            if sid != uuid.UUID(int=0):
                return sid
        except ValueError:
            raise InternalApiError(
                "invalid_request",
                f"{capability_id}: server_id must be a canonical UUID",
            ) from None
    ctx_sid = call.context.server_id
    if ctx_sid is not None and ctx_sid != uuid.UUID(int=0):
        return ctx_sid
    raise InternalApiError(
        "invalid_request",
        f"{capability_id} requires a target server_id (either pinned to the conversation or passed as server_id)",
    )


def build_internal_api_capabilities(
    *,
    applications: "ApplicationManifestStore | None",
    adoption: "ApplicationAdoption | None",
    docs: "ApplicationDocService | None",
    service_map: "ServiceMapStore | None",
    session_factory: Callable[[], Any] | None = None,
    data_root: Any | None = None,
    env_resolver: Callable[[], Any] | None = None,
    doc_indexer: "ScopeIndexer | None" = None,
    locks: "ResourceLockService | None" = None,
    metadata_coordinator: "MetadataMutationCoordinator | None" = None,
) -> InternalApiComposition:
    """Build every ``app.*`` descriptor + executor over the composed state.

    ``None`` services (fail-closed app composition) produce executors whose
    every call answers ``{"status": "unavailable", ...}`` — the composition
    contract of the HTTP routes, mirrored for the agent lane.
    """
    import asyncio
    from dataclasses import asdict
    from pathlib import Path
    from mai.api.applications import (
        AdoptionRequest,
        ApplicationOrigin,
        _render_manual_record,
        resolve_identity_key,
    )
    from mai.applications.discover import discover_server
    from mai.applications.document import record_doc_id
    from mai.applications.service_map import (
        INSTRUCTION_KEYS,
        ServiceMapError,
        classify_service,
        entry_digest,
    )
    from mai.contracts.server import MAX_FILE_READ_ROOTS, parse_server_profile_text
    from mai.knowledge.documents import (
        DocumentNamespace,
        DocumentStore,
        DocumentStoreError,
        DocumentStoreErrorCode,
        SourceKind,
        VerificationStatus,
    )
    from mai.profiles.files import ProfileFileStore
    from mai.profiles.projection import ProfileProjection

    import posixpath

    import yaml

    #: The host file-read credential-material deny list (FRR T5): REUSED, not
    #: copied — the profile root extension can never widen access to exactly
    #: the paths host.file_read hard-denies.
    from mai.capabilities.host_file_read import _DENY_PATH_PREFIXES

    from mai.applications.learning_operations import LearningOperationStore
    from mai.applications.metadata_mutations import (
        MetadataMutationCoordinator,
        MetadataMutationError,
    )
    from mai.applications.adopt import ApplicationAdoption
    from mai.jobs.resource_locks import ResourceLockError, ResourceLockService
    from sqlalchemy import text as _sql_text

    operations: list[tuple[InternalApiSpec, InternalApiOperation]] = []

    # -- ask_17 agent-writer retrofit: the common mutation guard -------------

    #: The shared service-metadata coordinator (slice 16). The lease
    #: service is AUTO-DERIVED from the session factory's async engine URL
    #: when omitted (same fixture/production DSN, sync driver form); an
    #: explicitly injected coordinator always wins. The writer executors
    #: FAIL CLOSED (``unavailable``) when the coordinator or the identity
    #: seam is absent — never an unguarded mutation path.
    coordinator: MetadataMutationCoordinator | None = None
    learning_store: LearningOperationStore | None = None
    effective_locks: ResourceLockService | None = locks
    if session_factory is not None and service_map is not None:
        learning_store = LearningOperationStore(session_factory)  # type: ignore[arg-type]
        if metadata_coordinator is not None:
            # An explicitly injected coordinator is ALWAYS honored — even
            # without (or with an underivable) locks service; its own
            # lock service performs the releases.
            coordinator = metadata_coordinator
            effective_locks = locks or getattr(
                metadata_coordinator, "_locks", None
            )
        else:
            if effective_locks is None:
                bind = getattr(session_factory, "kw", {}).get("bind")
                bind_url = getattr(bind, "url", None)
                if bind is not None and bind_url is not None:
                    dsn = (
                        bind_url.render_as_string(hide_password=False)
                        .replace("postgresql+asyncpg://", "postgresql://")
                        .replace("postgres+asyncpg://", "postgresql://")
                    )
                    effective_locks = ResourceLockService(dsn)
            if effective_locks is not None:
                coordinator = metadata_coordinator or MetadataMutationCoordinator(
                    locks=effective_locks,
                    map_store=service_map,
                    learning=learning_store,
                    adoption=(
                        adoption if isinstance(adoption, ApplicationAdoption) else None
                    ),
                    sessions=session_factory,  # type: ignore[arg-type]
                )

    async def _identity_suppressed(
        session_factory: Callable[[], Any],
        server_id: uuid.UUID,
        application_id: uuid.UUID,
    ) -> bool:
        """True when the identity row exists and carries the auto-adopt
        tombstone (absent row: NOT suppressed — creation paths are the
        adoption seam's business)."""
        async with session_factory() as session:
            row = (
                await session.execute(
                    _sql_text(
                        "SELECT auto_adopt_suppressed FROM "
                        "service_knowledge_identity WHERE server_id = :s "
                        "AND application_id = :a"
                    ),
                    {"s": str(server_id), "a": str(application_id)},
                )
            ).first()
        return row is not None and bool(row[0])

    def _release_leases(held: Sequence[Any]) -> None:
        """Release coordinator-acquired leases in reverse order (a lease
        that lapsed mid-operation was already fenced by the touch).
        Releases through the coordinator's OWN lock service when one is
        injected, else through the builder's (possibly auto-derived)
        service."""
        lock_svc = getattr(coordinator, "_locks", None) or effective_locks
        for lease in reversed(tuple(held)):
            if lock_svc is None:
                return
            try:
                lock_svc.release(lease)
            except ResourceLockError:
                pass

    def _lease_guard_error(error: MetadataMutationError) -> InternalApiError:
        """Map the coordinator's guard refusal onto the structured
        internal-API envelope (new stable code, additive to the contract)."""
        return InternalApiError(
            error.code.value, f"metadata mutation refused: {error.detail}"
        )

    # -- app.discover --------------------------------------------------------

    @internal_api_tool(
        capability_id=APP_DISCOVER_CAPABILITY,
        version="1.0.0",
        effect_class="read",
        resource_scope=["applications", "service_map"],
        environments=["controller"],
        payload_model=AppDiscoverPayload,
        description=(
            "Discover live Docker Compose projects, systemd units, and "
            "standalone containers on the target server through the reviewed "
            "read-only lanes. Returns the observed inventory with origins, "
            "managed flags, and suggested categories — never writes."
        ),
    )
    async def _app_discover(
        call: CapabilityCall, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if adoption is None or applications is None:
            raise InternalApiError(
                "unavailable", "applications service is unavailable"
            )
        server_id = _resolve_server_id(call, payload, APP_DISCOVER_CAPABILITY)
        root_dir = Path(data_root) if data_root is not None else applications.root
        try:
            profile_store = ProfileFileStore(root_dir)
            stored_prof = profile_store.read(server_id)
            profile = parse_server_profile_text(stored_prof.text)
            resolver = env_resolver() if env_resolver is not None else None
            if resolver is None:
                raise InternalApiError(
                    "unavailable", "credential source is not configured"
                )
            known_hosts_path = root_dir / "servers" / str(server_id) / "known_hosts"
            managed_refs = await adoption.managed_references(server_id)
            key_path = resolve_identity_key(resolver, profile.ssh.credential_ref)
            inventory = await asyncio.to_thread(
                discover_server,
                profile.ssh,
                server_id=server_id,
                key_path=str(key_path),
                known_hosts_path=str(known_hosts_path),
                managed_refs=managed_refs,
            )
        except InternalApiError:
            raise
        except Exception as exc:
            raise InternalApiError(
                "discovery_failed",
                f"bounded discovery failed: {type(exc).__name__}",
            ) from None
        applications_view: list[dict[str, Any]] = []
        raw_origin = payload.get("origin")
        want_origin = (
            raw_origin.strip().lower()
            if isinstance(raw_origin, str) and raw_origin.strip()
            else None
        )
        raw_query = payload.get("query")
        want_query = (
            raw_query.strip().lower()
            if isinstance(raw_query, str) and raw_query.strip()
            else None
        )
        for observed in inventory.applications:
            detail = observed.detail
            origin_str = (
                observed.origin.value
                if hasattr(observed.origin, "value")
                else str(observed.origin)
            )
            name_str = getattr(
                detail,
                "project_name",
                getattr(
                    detail,
                    "unit",
                    getattr(getattr(detail, "container", None), "name", "unknown"),
                ),
            )
            if want_origin and origin_str.lower() != want_origin:
                continue
            if want_query and want_query not in str(name_str).lower():
                continue
            applications_view.append(
                {
                    "origin": origin_str,
                    "managed": observed.managed,
                    "application_id": (
                        str(observed.application_id)
                        if observed.application_id is not None
                        else None
                    ),
                    "name": name_str,
                    "detail": asdict(detail) if hasattr(detail, "__dataclass_fields__") else {},
                }
            )
        total_apps = len(applications_view)
        eff_limit = int(payload.get("limit") or 25)
        eff_offset = max(0, int(payload.get("offset") or 0))
        paged_apps = applications_view[eff_offset : eff_offset + eff_limit]
        result_doc: dict[str, Any] = {
            "server_id": str(server_id),
            "observed_at": str(inventory.observed_at),
            "applications": paged_apps,
            "total": total_apps,
            "limit": eff_limit,
            "offset": eff_offset,
            "has_more": (eff_offset + len(paged_apps)) < total_apps,
            "lanes": [
                {
                    "lane": lane.lane.value,
                    "state": lane.state.value,
                    "detail": lane.detail,
                }
                for lane in inventory.lanes
            ],
        }
        if payload.get("include_ports"):
            result_doc["listen_ports"] = [asdict(p) for p in inventory.listen_ports]
        return result_doc

    operations.append(_app_discover)

    # -- app.list_adopted ------------------------------------------------------

    @internal_api_tool(
        capability_id=APP_LIST_ADOPTED_CAPABILITY,
        version="1.0.0",
        effect_class="read",
        resource_scope=["applications", "service_map"],
        environments=["controller"],
        payload_model=AppListAdoptedPayload,
        description=(
            "List the adopted applications of the target server: the "
            "reconciled projection records (revision, sha256, adopted_at) "
            "merged with their Service Map entries (category, description, "
            "instructions). Discovery observations never merge into this list."
        ),
    )
    async def _app_list_adopted(
        call: CapabilityCall, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if adoption is None or service_map is None:
            raise InternalApiError(
                "unavailable", "applications service is unavailable"
            )
        server_id = _resolve_server_id(call, payload, APP_LIST_ADOPTED_CAPABILITY)
        records = await adoption.list_adopted(server_id)
        entries = {e.application_id: e for e in service_map.list_entries(server_id)}
        applications = []
        for record in records:
            entry = entries.get(str(record.application_id))
            applications.append(
                {
                    "application_id": str(record.application_id),
                    "server_id": str(record.server_id),
                    "name": entry.name if entry is not None else record.name,
                    "origin": record.origin.value,
                    "revision": record.revision,
                    "sha256": record.sha256,
                    "adopted_at": record.adopted_at.isoformat(),
                    "category": entry.category if entry is not None else "system",
                    "description": entry.description if entry is not None else "",
                    "custom_instructions": (
                        entry.custom_instructions if entry is not None else ""
                    ),
                    "installed": entry.installed if entry is not None else True,
                }
            )
        adopted_ids = {a["application_id"] for a in applications}
        for aid, entry in entries.items():
            # Map-only fallback: a Service Map sidecar without a canonical
            # adoption record is listed ONLY when it is a genuine manual
            # map addition (chat/agent entries, which never own a live
            # compose/systemd/docker target). A non-manual sidecar whose
            # adoption record is gone (revoked) is ORPHANED adoption
            # metadata and stays hidden — the same membership rule the
            # HTTP listing applies (agent-tool parity). The sidecar itself
            # is retained on disk, untouched; filtering happens BEFORE
            # sort/total/pagination, so total/limit/offset/has_more
            # describe the filtered set.
            if aid not in adopted_ids and entry.origin == "manual":
                applications.append(
                    {
                        "application_id": aid,
                        "server_id": str(server_id),
                        "name": entry.name,
                        "origin": entry.origin,
                        "revision": entry.revision,
                        "sha256": None,
                        "adopted_at": entry.updated_at,
                        "category": entry.category,
                        "description": entry.description,
                        "custom_instructions": entry.custom_instructions,
                        "installed": entry.installed,
                    }
                )
        applications.sort(key=lambda a: (a["name"], a["application_id"]))
        total = len(applications)
        eff_limit = int(payload.get("limit") or 25)
        eff_offset = max(0, int(payload.get("offset") or 0))
        paged = applications[eff_offset : eff_offset + eff_limit]
        return {
            "server_id": str(server_id),
            "applications": paged,
            "total": total,
            "limit": eff_limit,
            "offset": eff_offset,
            "has_more": (eff_offset + len(paged)) < total,
        }

    operations.append(_app_list_adopted)

    # -- app.adopt -------------------------------------------------------------

    @internal_api_tool(
        capability_id=APP_ADOPT_CAPABILITY,
        version="1.0.0",
        effect_class="mutate",
        resource_scope=["applications", "service_map"],
        environments=["controller"],
        payload_model=AppAdoptPayload,
        description=(
            "Adopt one application into Machina management: writes the "
            "canonical adoption manifest, reconciles the projection, seeds "
            "the application document, and upserts the Service Map entry in "
            "one operation. The origin must be a frozen selector (manual, "
            "compose, systemd_unit); standalone containers are refused."
        ),
    )
    async def _app_adopt(
        call: CapabilityCall, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if adoption is None or docs is None or service_map is None:
            raise InternalApiError(
                "unavailable", "applications service is unavailable"
            )
        if coordinator is None or learning_store is None:
            # Fail-closed: no unguarded mutation path exists (AC1).
            raise InternalApiError(
                "unavailable", "service metadata coordinator is unavailable"
            )
        server_id = _resolve_server_id(call, payload, APP_ADOPT_CAPABILITY)
        name = str(payload["name"]).strip()
        raw_call_payload = call.payload if isinstance(call.payload, Mapping) else {}
        explicit_origin = "origin" in raw_call_payload and raw_call_payload.get("origin") is not None
        origin_value = str(payload.get("origin") or "manual")

        # Match any existing Service Map entry on this server or global
        # Catalog Runbook so adopting a catalog service inherits its
        # category, description, and 5-slot runbook instructions while
        # adding server-specific custom_instructions and installed state.
        raw_aid = payload.get("application_id")
        parsed_aid: uuid.UUID | None = None
        if raw_aid:
            try:
                parsed_aid = uuid.UUID(str(raw_aid).strip())
            except ValueError:
                raise InternalApiError(
                    "invalid_request",
                    "application_id must be a canonical UUID string",
                ) from None

        existing_entry = None
        for candidate in service_map.list_entries(server_id):
            if parsed_aid is not None and candidate.application_id == str(parsed_aid):
                existing_entry = candidate
                break
            if parsed_aid is None and candidate.name.lower() == name.lower():
                existing_entry = candidate
                break

        catalog_runbook = None
        for rb in service_map.list_catalog_runbooks():
            if parsed_aid is not None and rb.runbook_id == str(parsed_aid):
                catalog_runbook = rb
                break
            if rb.name.lower() == name.lower():
                catalog_runbook = rb
                break

        # If updating an existing server entry and origin was not explicitly
        # changed by the caller, preserve its stored origin and selectors.
        if not explicit_origin and existing_entry is not None:
            origin_value = existing_entry.origin

        compose_project = payload.get("compose_project")
        if compose_project is None and existing_entry is not None and origin_value == existing_entry.origin:
            compose_project = existing_entry.compose_project
        working_dir = payload.get("working_dir")
        if working_dir is None and existing_entry is not None and origin_value == existing_entry.origin:
            working_dir = existing_entry.working_dir
        unit_name = payload.get("unit_name")
        if unit_name is None and existing_entry is not None and origin_value == existing_entry.origin:
            unit_name = existing_entry.unit_name

        # Resolve per-server application_id (never reuse a bare catalog
        # runbook_id across multiple servers' compose/systemd adoptions,
        # which would collide on cross_server_conflict).
        existing_adopted = None
        try:
            for rec in await adoption.list_adopted(server_id):
                if parsed_aid is not None and rec.application_id == parsed_aid:
                    existing_adopted = rec
                    break
                if parsed_aid is None and rec.name.lower() == name.lower():
                    existing_adopted = rec
                    break
        except Exception:
            existing_adopted = None

        application_id: uuid.UUID
        if parsed_aid is not None:
            if (
                origin_value != "manual"
                and catalog_runbook is not None
                and str(parsed_aid) == catalog_runbook.runbook_id
                and existing_adopted is None
            ):
                application_id = uuid.uuid5(
                    server_id, f"service-map:{name.lower()}"
                )
            else:
                application_id = parsed_aid
        elif existing_adopted is not None:
            application_id = existing_adopted.application_id
        elif existing_entry is not None and (
            origin_value == "manual"
            or catalog_runbook is None
            or existing_entry.application_id != catalog_runbook.runbook_id
        ):
            application_id = uuid.UUID(existing_entry.application_id)
        else:
            application_id = uuid.uuid5(
                server_id, f"service-map:{name.lower()}"
            )

        # Merge inherited category, description, and 5-slot runbook instructions.
        effective_category = (
            str(payload["category"])
            if payload.get("category")
            else (
                existing_entry.category
                if existing_entry is not None
                else (
                    catalog_runbook.category
                    if catalog_runbook is not None
                    else classify_service(
                        name, str(unit_name or compose_project or "")
                    )
                )
            )
        )
        effective_description = (
            str(payload["description"])
            if payload.get("description") is not None
            else (
                existing_entry.description
                if existing_entry is not None and existing_entry.description
                else (
                    catalog_runbook.description
                    if catalog_runbook is not None
                    else ""
                )
            )
        )
        merged_instructions: dict[str, str] = {k: "" for k in INSTRUCTION_KEYS}
        if catalog_runbook is not None:
            for key, val in catalog_runbook.instructions.to_json().items():
                if val:
                    merged_instructions[key] = val
        if existing_entry is not None:
            for key, val in existing_entry.instructions.to_json().items():
                if val:
                    merged_instructions[key] = val
        raw_instr = raw_call_payload.get("instructions")
        if isinstance(raw_instr, Mapping):
            for key in INSTRUCTION_KEYS:
                if key in raw_instr and raw_instr[key] is not None:
                    merged_instructions[key] = str(raw_instr[key])
        elif isinstance(payload.get("instructions"), Mapping):
            for key, val in payload["instructions"].items():
                if val:
                    merged_instructions[key] = str(val)

        # ask_17 common guard: the whole read-modify-write effect section
        # (category upserts, map write, canonical adoption, stale-entry
        # cleanup) runs under the C4 lease set for this application — and
        # for a superseded manual entry's OWN identity too, so its stale
        # cleanup is fenced as well. Reads above stay lock-free (atomic,
        # read-only). Without the lease service the executors already
        # failed closed above.
        lease_app_ids = sorted(
            {
                application_id,
                *(
                    [uuid.UUID(existing_entry.application_id)]
                    if existing_entry is not None
                    else []
                ),
            },
            key=str,
        )
        try:
            held_leases = coordinator.lease_set(server_id, lease_app_ids)
        except MetadataMutationError as error:
            raise _lease_guard_error(error) from None
        try:
            if payload.get("upsert_categories"):
                for category in payload["upsert_categories"]:
                    service_map.upsert_category(
                        str(category["machine_id"]),
                        label=str(category.get("label", category["machine_id"])),
                        description=str(category.get("description", "")),
                    )

            if origin_value == "manual":
                # The chat/manual addition: persist directly in the Service Map +
                # canonical identity doc (the UI's `_adopt_manual_entry` flow).
                entry = service_map.upsert_entry(
                    server_id,
                    name=name,
                    category=effective_category,
                    origin="manual",
                    application_id=application_id,
                    compose_project=compose_project,
                    working_dir=working_dir,
                    unit_name=unit_name,
                    description=effective_description,
                    instructions=merged_instructions,
                    custom_instructions=payload.get("custom_instructions"),
                    installed=payload.get("installed"),
                )
                if (
                    existing_entry is not None
                    and existing_entry.application_id != str(application_id)
                    and existing_entry.origin == "manual"
                ):
                    service_map.delete_entry(server_id, existing_entry.application_id)
                # An explicit adopt RE-ADOPTS a tombstoned identity: clears
                # the suppression and bumps the generation (D4 readopt).
                if await _identity_suppressed(session_factory, server_id, application_id):
                    await learning_store.readopt_service_identity(
                        server_id, application_id, manual_identity=True
                    )
                doc_outcome = {"status": "pending", "doc_id": None, "reason": None}
                if data_root is not None:
                    try:
                        doc_store = DocumentStore(Path(data_root))
                        doc_id = record_doc_id(server_id, application_id)
                        expected_revision = 0
                        try:
                            current = doc_store.read(
                                doc_id, server_id=server_id, app_id=application_id
                            )
                            expected_revision = current.revision
                        except DocumentStoreError as error:
                            if error.code is not DocumentStoreErrorCode.NOT_FOUND:
                                raise
                        document = doc_store.write(
                            doc_id,
                            _render_manual_record(entry),
                            server_id=server_id,
                            app_id=application_id,
                            namespace=DocumentNamespace.DOCS,
                            expected_revision=expected_revision,
                            source_kind=SourceKind.GENERATED,
                            origin=f"service-map:{entry.revision}",
                            origin_revision=str(entry.revision),
                            verification=VerificationStatus.UNVERIFIED,
                        )
                        doc_outcome = {
                            "status": "documented",
                            "doc_id": str(document.doc_id),
                            "reason": None,
                        }
                    except Exception as exc:
                        doc_outcome["reason"] = type(exc).__name__
                return {
                    "application_id": str(application_id),
                    "server_id": str(server_id),
                    "name": entry.name,
                    "origin": entry.origin,
                    "category": entry.category,
                    "description": entry.description,
                    "custom_instructions": entry.custom_instructions,
                    "installed": entry.installed,
                    "instructions": entry.instructions.to_json(),
                    "outcome": "reconciled",
                    "revision": entry.revision,
                    "sha256": entry_digest(entry),
                    "record_doc": doc_outcome,
                }

            try:
                origin = ApplicationOrigin(origin_value)
            except ValueError:
                raise InternalApiError(
                    "invalid_request",
                    f"origin must be one of {[o.value for o in ApplicationOrigin]}",
                ) from None
            expected_rev = payload.get("expected_revision")
            if expected_rev is None:
                try:
                    live_rec = await adoption.read(server_id, application_id)
                except Exception:
                    live_rec = None
                expected_rev = 0 if live_rec is None else live_rec.revision
            request = AdoptionRequest(
                server_id=server_id,
                application_id=application_id,  # type: ignore[arg-type]
                name=name,
                origin=origin,
                compose_project=compose_project,
                working_dir=working_dir,
                unit_name=unit_name,
                external_working_dir=payload.get("external_working_dir"),
                external_config_files=tuple(payload.get("external_config_files", [])),
                external_environment_files=tuple(
                    payload.get("external_environment_files", [])
                ),
                expected_revision=int(expected_rev),
            )
            try:
                result = await adoption.adopt(
                    store=applications, request=request  # type: ignore[arg-type]
                )
            except Exception as exc:
                raise InternalApiError(
                    "adoption_failed",
                    f"adoption refused: {type(exc).__name__}",
                ) from None
            entry = service_map.upsert_entry(
                server_id,
                application_id=result.application_id,
                name=name,
                origin=origin.value,
                compose_project=compose_project,
                working_dir=working_dir,
                unit_name=unit_name,
                category=effective_category,
                description=effective_description,
                instructions=merged_instructions,
                custom_instructions=payload.get("custom_instructions"),
                installed=payload.get("installed"),
            )
            # An explicit adopt RE-ADOPTS a tombstoned identity: clears the
            # suppression and bumps the generation (D4 readopt).
            if await _identity_suppressed(session_factory, server_id, result.application_id):
                await learning_store.readopt_service_identity(
                    server_id, result.application_id, manual_identity=False
                )
            if (
                existing_entry is not None
                and existing_entry.application_id != str(result.application_id)
                and existing_entry.origin == "manual"
            ):
                service_map.delete_entry(server_id, existing_entry.application_id)
            # Canonical identity document (honest pending on doc-service gaps).
            record = await adoption.read(server_id, result.application_id)
            doc_outcome = {"status": "pending", "doc_id": None, "reason": None}
            if docs is not None and record is not None:
                try:
                    outcome = await docs.record_adoption(record)
                    if outcome.document is not None and outcome.documented:
                        doc_outcome = {
                            "status": "documented",
                            "doc_id": str(outcome.document.doc_id),
                            "reason": None,
                        }
                    else:
                        doc_outcome["reason"] = outcome.reason or "not_documented"
                except Exception as exc:
                    doc_outcome["reason"] = type(exc).__name__
            return {
                "application_id": str(result.application_id),
                "server_id": str(server_id),
                "name": entry.name,
                "origin": entry.origin,
                "category": entry.category,
                "description": entry.description,
                "custom_instructions": entry.custom_instructions,
                "installed": entry.installed,
                "instructions": entry.instructions.to_json(),
                "outcome": result.outcome,
                "revision": result.revision,
                "sha256": result.sha256,
                "record_doc": doc_outcome,
            }
        finally:
            _release_leases(held_leases)

    operations.append(_app_adopt)

    # -- app.unadopt -----------------------------------------------------------

    @internal_api_tool(
        capability_id=APP_UNADOPT_CAPABILITY,
        version="1.0.0",
        effect_class="mutate",
        resource_scope=["applications", "service_map"],
        environments=["controller"],
        payload_model=AppUnadoptPayload,
        description=(
            "Remove one application from adoption (revoke): writes the "
            "canonical tombstone, deletes the projection row, and removes "
            "the Service Map entry. Machina stops managing the application; "
            "the running containers/units on the host are NOT touched."
        ),
    )
    async def _app_unadopt(
        call: CapabilityCall, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if adoption is None or service_map is None:
            raise InternalApiError(
                "unavailable", "applications service is unavailable"
            )
        if coordinator is None or learning_store is None:
            # Fail-closed: no unguarded mutation path exists (AC1).
            raise InternalApiError(
                "unavailable", "service metadata coordinator is unavailable"
            )
        server_id = _resolve_server_id(call, payload, APP_UNADOPT_CAPABILITY)
        application_id = uuid.UUID(str(payload["application_id"]))
        expected_revision = payload.get("expected_revision")
        if expected_revision is None:
            # Auto-resolve the current revision so the model never guesses.
            try:
                record = await adoption.read(server_id, application_id)
            except Exception:
                record = None
            expected_revision = 0 if record is None else record.revision
        # ask_17 common guard: under the C4 lease set the auto-adopt
        # TOMBSTONE is persisted and stale pending preservation is
        # cancelled BEFORE the canonical revoke and the map deletion
        # (suppression-before-removal, D4). Without the lease service the
        # legacy path runs unchanged.
        try:
            held_leases = coordinator.lease_set(server_id, [application_id])
        except MetadataMutationError as error:
            raise _lease_guard_error(error) from None
        try:
            await learning_store.suppress_for_unadopt(server_id, application_id)
            try:
                result = await adoption.revoke(
                    store=applications,  # type: ignore[arg-type]
                    server_id=server_id,
                    application_id=application_id,
                    expected_revision=int(expected_revision),
                )
            except Exception as exc:
                raise InternalApiError(
                    "revoke_failed",
                    f"revoke refused: {type(exc).__name__}",
                ) from None
            # Remove the Service Map entry (the UI's unadopt flow).
            service_map.delete_entry(server_id, application_id)
            return {
                "application_id": str(application_id),
                "server_id": str(server_id),
                "outcome": result.outcome,
                "revision": result.revision,
                "sha256": result.sha256,
            }
        finally:
            _release_leases(held_leases)

    operations.append(_app_unadopt)

    # -- app.delete_map_entry --------------------------------------------------

    @internal_api_tool(
        capability_id=APP_DELETE_MAP_ENTRY_CAPABILITY,
        version="1.0.0",
        effect_class="mutate",
        resource_scope=["service_map"],
        environments=["controller"],
        payload_model=AppDeleteMapEntryPayload,
        description=(
            "Remove one Service Map entry by identity. Only manual "
            "(un-adopted) entries can be removed; adopted entries require "
            "app.unadopt first."
        ),
    )
    async def _app_delete_map_entry(
        call: CapabilityCall, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if service_map is None:
            raise InternalApiError(
                "unavailable", "service map service is unavailable"
            )
        if coordinator is None or learning_store is None:
            # Fail-closed: no unguarded mutation path exists (AC1).
            raise InternalApiError(
                "unavailable", "service metadata coordinator is unavailable"
            )
        server_id = _resolve_server_id(call, payload, APP_DELETE_MAP_ENTRY_CAPABILITY)
        application_id = uuid.UUID(str(payload["application_id"]))
        # ask_17 common guard: the deletion runs under the C4 lease set.
        try:
            held_leases = coordinator.lease_set(server_id, [application_id])
        except MetadataMutationError as error:
            raise _lease_guard_error(error) from None
        try:
            # A missing entry is refused BEFORE any tombstone is written:
            # suppression never fabricates identity for a phantom service.
            existing = service_map.get_entry(server_id, application_id)
            if existing is None:
                raise InternalApiError(
                    "not_found",
                    f"no service map entry stored for {application_id}",
                )
            # A canonically adopted entry is never deleted behind the
            # adoption seam's back: app.unadopt owns that path. A FAILED
            # adoption read (unhealthy store) refuses the deletion — it
            # must NEVER be treated as "no adoption record" (fail-closed).
            adoption_record = None
            if adoption is not None and isinstance(adoption, ApplicationAdoption):
                try:
                    adoption_record = await adoption.read(
                        server_id, application_id
                    )
                except Exception:
                    raise InternalApiError(
                        "unavailable",
                        "adoption state could not be verified; deletion refused",
                    ) from None
            if existing.origin != "manual" or adoption_record is not None:
                raise InternalApiError(
                    "conflict",
                    f"service {application_id} is canonically adopted; "
                    "use app.unadopt instead",
                )
            # Tombstone BEFORE removal (D4): the deleted manual service can
            # never be silently re-adopted by background work.
            await learning_store.suppress_for_unadopt(server_id, application_id)
            service_map.delete_entry(server_id, application_id)
            return {
                "application_id": str(application_id),
                "server_id": str(server_id),
                "removed": True,
            }
        finally:
            _release_leases(held_leases)

    operations.append(_app_delete_map_entry)

    # -- app.list_catalog ------------------------------------------------------

    @internal_api_tool(
        capability_id=APP_LIST_CATALOG_CAPABILITY,
        version="1.0.0",
        effect_class="read",
        resource_scope=["service_map"],
        environments=["controller"],
        payload_model=AppListCatalogPayload,
        description=(
            "List global Runbook Catalog recipes across the fleet (server-agnostic). "
            "Returns paginated catalog runbooks (runbook_id, name, category, "
            "description, 5-slot instructions, revision) and taxonomy categories."
        ),
    )
    async def _app_list_catalog(
        call: CapabilityCall, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        del call
        if service_map is None:
            raise InternalApiError(
                "unavailable", "service map service is unavailable"
            )
        runbooks = list(service_map.list_catalog_runbooks())
        category_filter = (payload.get("category") or "").strip().lower()
        if category_filter:
            runbooks = [
                rb for rb in runbooks if rb.category.lower() == category_filter
            ]
        query = (payload.get("query") or "").strip().lower()
        if query:
            runbooks = [
                rb
                for rb in runbooks
                if query in rb.name.lower()
                or query in rb.category.lower()
                or query in rb.description.lower()
            ]
        total = len(runbooks)
        eff_limit = int(payload.get("limit") or 25)
        eff_offset = max(0, int(payload.get("offset") or 0))
        paged = runbooks[eff_offset : eff_offset + eff_limit]
        return {
            "runbooks": [rb.to_json() for rb in paged],
            "categories": [
                cat.to_json() for cat in service_map.list_categories()
            ],
            "total": total,
            "limit": eff_limit,
            "offset": eff_offset,
            "has_more": (eff_offset + len(paged)) < total,
        }

    operations.append(_app_list_catalog)

    # -- app.upsert_catalog_runbook --------------------------------------------

    @internal_api_tool(
        capability_id=APP_UPSERT_CATALOG_RUNBOOK_CAPABILITY,
        version="1.0.0",
        effect_class="mutate",
        resource_scope=["service_map"],
        environments=["controller"],
        payload_model=AppUpsertCatalogRunbookPayload,
        description=(
            "Add or update an application recipe in the global Runbook Catalog "
            "(catalog.json only; server-agnostic, does not adopt onto any server). "
            "Use after researching official documentation to store the service's "
            "category, description, and 5 operational runbook slots (install, "
            "update, uninstall, backup, verify)."
        ),
    )
    async def _app_upsert_catalog_runbook(
        call: CapabilityCall, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        del call
        if service_map is None:
            raise InternalApiError(
                "unavailable", "service map service is unavailable"
            )
        name = str(payload["name"]).strip()
        raw_rid = payload.get("runbook_id")
        parsed_rid: uuid.UUID | None = None
        if raw_rid:
            try:
                parsed_rid = uuid.UUID(str(raw_rid).strip())
            except ValueError:
                raise InternalApiError(
                    "invalid_request",
                    "runbook_id must be a canonical UUID string",
                ) from None

        if payload.get("upsert_categories"):
            for category in payload["upsert_categories"]:
                try:
                    service_map.upsert_category(
                        str(category["machine_id"]),
                        label=str(category.get("label", category["machine_id"])),
                        description=str(category.get("description", "")),
                    )
                except ServiceMapError as exc:
                    raise InternalApiError(exc.code.value, exc.detail) from None

        existing = None
        for candidate in service_map.list_catalog_runbooks():
            if parsed_rid is not None and candidate.runbook_id == str(parsed_rid):
                existing = candidate
                break
            if parsed_rid is None and candidate.name.lower() == name.lower():
                existing = candidate
                break
        if parsed_rid is None and existing is not None:
            parsed_rid = uuid.UUID(existing.runbook_id)

        effective_category = (
            str(payload["category"])
            if payload.get("category")
            else (
                existing.category
                if existing is not None
                else classify_service(
                    name, str(payload.get("description") or "")
                )
            )
        )
        effective_description = (
            str(payload["description"])
            if payload.get("description") is not None
            else (existing.description if existing is not None else "")
        )
        merged_instructions: dict[str, str] = {k: "" for k in INSTRUCTION_KEYS}
        if existing is not None:
            for key, val in existing.instructions.to_json().items():
                if val:
                    merged_instructions[key] = val
        if isinstance(payload.get("instructions"), Mapping):
            for key, val in payload["instructions"].items():
                if val:
                    merged_instructions[key] = str(val)

        try:
            runbook = service_map.upsert_catalog_runbook(
                name=name,
                category=effective_category,
                description=effective_description,
                instructions=merged_instructions,
                runbook_id=parsed_rid,
            )
        except ServiceMapError as exc:
            raise InternalApiError(exc.code.value, exc.detail) from None

        return {
            "runbook": runbook.to_json(),
            "runbook_id": runbook.runbook_id,
            "name": runbook.name,
            "category": runbook.category,
            "revision": runbook.revision,
        }

    operations.append(_app_upsert_catalog_runbook)

    # -- app.delete_catalog_runbook --------------------------------------------

    @internal_api_tool(
        capability_id=APP_DELETE_CATALOG_RUNBOOK_CAPABILITY,
        version="1.0.0",
        effect_class="mutate",
        resource_scope=["service_map"],
        environments=["controller"],
        payload_model=AppDeleteCatalogRunbookPayload,
        description=(
            "Delete one application recipe from the global Runbook Catalog "
            "(catalog.json only). Per-server adopted applications and Service "
            "Map entries are never modified or removed by this tool."
        ),
    )
    async def _app_delete_catalog_runbook(
        call: CapabilityCall, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        del call
        if service_map is None:
            raise InternalApiError(
                "unavailable", "service map service is unavailable"
            )
        try:
            rid = uuid.UUID(str(payload["runbook_id"]).strip())
        except ValueError:
            raise InternalApiError(
                "invalid_request",
                "runbook_id must be a canonical UUID string",
            ) from None
        removed = service_map.delete_catalog_runbook(rid)
        if not removed:
            raise InternalApiError(
                "not_found",
                f"no global catalog runbook stored for {rid}",
            )
        return {
            "runbook_id": str(rid),
            "removed": True,
        }

    operations.append(_app_delete_catalog_runbook)

    # -- docs.* helpers & capabilities -----------------------------------------

    def _resolve_doc_store() -> DocumentStore:
        store_attr = getattr(docs, "_store", None)
        if isinstance(store_attr, DocumentStore):
            return store_attr
        if data_root is not None:
            try:
                return DocumentStore(Path(data_root))
            except Exception as exc:
                raise InternalApiError(
                    "unavailable",
                    f"documents store is unavailable: {type(exc).__name__}",
                ) from None
        raise InternalApiError(
            "unavailable", "documents service is unavailable"
        )

    def _resolve_doc_scope(
        call: CapabilityCall, payload: Mapping[str, Any], capability_id: str
    ) -> tuple[str, uuid.UUID | None, uuid.UUID | None]:
        raw_scope = payload.get("scope", "server")
        scope = str(raw_scope).strip() if raw_scope is not None else "server"
        if scope not in ("server", "shared"):
            raise InternalApiError(
                "invalid_request",
                f"{capability_id}: scope must be 'server' or 'shared'",
            )
        raw_app_id = payload.get("app_id")
        parsed_app_id: uuid.UUID | None = None
        if isinstance(raw_app_id, str) and raw_app_id.strip():
            if scope == "shared":
                raise InternalApiError(
                    "invalid_request",
                    f"{capability_id}: app_id cannot be used with scope='shared'",
                )
            try:
                parsed_app_id = uuid.UUID(raw_app_id.strip())
            except ValueError:
                raise InternalApiError(
                    "invalid_request",
                    f"{capability_id}: app_id must be a canonical UUID",
                ) from None
            if parsed_app_id == uuid.UUID(int=0):
                raise InternalApiError(
                    "invalid_request",
                    f"{capability_id}: app_id must be a non-zero canonical UUID",
                )
        if scope == "shared":
            return "shared", None, None
        target_server_id = _resolve_server_id(call, payload, capability_id)
        return "server", target_server_id, parsed_app_id

    def _extract_doc_title(text: str) -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip()
                if heading:
                    return heading[:160]
            return stripped[:160]
        return "Untitled"

    def _map_doc_store_error(exc: DocumentStoreError) -> InternalApiError:
        if exc.code is DocumentStoreErrorCode.NOT_FOUND:
            return InternalApiError("not_found", str(exc))
        if exc.code in (
            DocumentStoreErrorCode.STALE_REVISION,
            DocumentStoreErrorCode.TOMBSTONE_CONFLICT,
        ):
            return InternalApiError("conflict", str(exc))
        return InternalApiError("invalid_request", str(exc))

    async def _best_effort_reconcile(
        doc_store: DocumentStore,
        *,
        server_id: uuid.UUID | None,
        app_id: uuid.UUID | None,
        doc_id: uuid.UUID,
        namespace: DocumentNamespace,
    ) -> str:
        if doc_indexer is not None:
            try:
                await doc_indexer.reconcile_scope(
                    server_id=server_id, app_id=app_id
                )
            except Exception:
                pass
        try:
            pending_ids = {
                intent.doc_id
                for intent in doc_store.read_intents(
                    server_id=server_id,
                    app_id=app_id,
                    namespace=namespace,
                )
            }
        except Exception:
            return "pending"
        return "pending" if doc_id in pending_ids else "indexed"

    # -- docs.list -------------------------------------------------------------

    @internal_api_tool(
        capability_id=DOCS_LIST_CAPABILITY,
        version="1.0.0",
        effect_class="read",
        resource_scope=["docs", "runbooks"],
        environments=["controller"],
        payload_model=DocsListPayload,
        description=(
            "List canonical Markdown documents and operational runbooks stored "
            "in the Documents section for the target server (or shared store) "
            "with bounded pagination and optional namespace/query filters."
        ),
    )
    async def _docs_list(
        call: CapabilityCall, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        doc_store = _resolve_doc_store()
        scope, target_server_id, target_app_id = _resolve_doc_scope(
            call, payload, DOCS_LIST_CAPABILITY
        )
        raw_ns = payload.get("namespace")
        ns_filter: DocumentNamespace | None = None
        if isinstance(raw_ns, str) and raw_ns.strip():
            ns_filter = DocumentNamespace(raw_ns.strip())
        try:
            stored_docs = doc_store.list_documents(
                server_id=target_server_id,
                app_id=target_app_id,
                namespace=ns_filter,
            )
            pending_keys = {
                (intent.namespace.value, intent.doc_id)
                for intent in doc_store.read_intents(
                    server_id=target_server_id,
                    app_id=target_app_id,
                    namespace=ns_filter,
                )
            }
        except DocumentStoreError as exc:
            raise _map_doc_store_error(exc) from None

        raw_query = payload.get("query")
        needle = (
            raw_query.strip().lower()
            if isinstance(raw_query, str) and raw_query.strip()
            else ""
        )
        matched: list[dict[str, Any]] = []
        for item in stored_docs:
            title = _extract_doc_title(item.text)
            if needle:
                haystack = (
                    f"{title}\n{item.text}\n{item.origin}\n{item.doc_id}"
                ).lower()
                if needle not in haystack:
                    continue
            matched.append(
                {
                    "doc_id": str(item.doc_id),
                    "namespace": item.namespace.value,
                    "title": title,
                    "revision": item.revision,
                    "sha256": item.sha256,
                    "citation": f"{item.namespace.value}:{item.doc_id}#r{item.revision}",
                    "source_kind": item.source_kind.value,
                    "origin": item.origin,
                    "origin_revision": item.origin_revision,
                    "verification": item.verification.value,
                    "index_pending": (item.namespace.value, item.doc_id)
                    in pending_keys,
                }
            )
        total = len(matched)
        eff_limit = int(payload.get("limit", 25))
        eff_offset = int(payload.get("offset", 0))
        sliced = matched[eff_offset : eff_offset + eff_limit]
        return {
            "scope": scope,
            "server_id": str(target_server_id)
            if target_server_id is not None
            else None,
            "app_id": str(target_app_id)
            if target_app_id is not None
            else None,
            "namespace": ns_filter.value if ns_filter is not None else None,
            "documents": sliced,
            "total": total,
            "limit": eff_limit,
            "offset": eff_offset,
            "has_more": (eff_offset + len(sliced)) < total,
        }

    operations.append(_docs_list)

    # -- docs.read -------------------------------------------------------------

    @internal_api_tool(
        capability_id=DOCS_READ_CAPABILITY,
        version="1.0.0",
        effect_class="read",
        resource_scope=["docs", "runbooks"],
        environments=["controller"],
        payload_model=DocsReadPayload,
        description=(
            "Read the full hash-verified Markdown content and metadata of one "
            "canonical document or runbook from the Documents section by doc_id."
        ),
    )
    async def _docs_read(
        call: CapabilityCall, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        doc_store = _resolve_doc_store()
        scope, target_server_id, target_app_id = _resolve_doc_scope(
            call, payload, DOCS_READ_CAPABILITY
        )
        try:
            parsed_doc_id = uuid.UUID(str(payload["doc_id"]).strip())
        except ValueError:
            raise InternalApiError(
                "invalid_request",
                "docs.read: doc_id must be a canonical UUID string",
            ) from None
        ns = DocumentNamespace(str(payload.get("namespace", "docs")).strip())
        try:
            stored = doc_store.read(
                parsed_doc_id,
                server_id=target_server_id,
                app_id=target_app_id,
                namespace=ns,
            )
        except DocumentStoreError as exc:
            raise _map_doc_store_error(exc) from None
        max_chars = int(payload.get("max_chars", 32000))
        content = stored.text[:max_chars]
        return {
            "doc_id": str(stored.doc_id),
            "scope": scope,
            "server_id": str(target_server_id)
            if target_server_id is not None
            else None,
            "app_id": str(target_app_id)
            if target_app_id is not None
            else None,
            "namespace": stored.namespace.value,
            "title": _extract_doc_title(stored.text),
            "revision": stored.revision,
            "sha256": stored.sha256,
            "citation": f"{stored.namespace.value}:{stored.doc_id}#r{stored.revision}",
            "source_kind": stored.source_kind.value,
            "origin": stored.origin,
            "origin_revision": stored.origin_revision,
            "verification": stored.verification.value,
            "content": content,
            "total_chars": len(stored.text),
            "truncated": len(stored.text) > max_chars,
        }

    operations.append(_docs_read)

    # -- docs.upsert -----------------------------------------------------------

    @internal_api_tool(
        capability_id=DOCS_UPSERT_CAPABILITY,
        version="1.0.0",
        effect_class="mutate",
        resource_scope=["docs", "runbooks"],
        environments=["controller"],
        payload_model=DocsUpsertPayload,
        description=(
            "Create or update a canonical Markdown document or operational "
            "runbook in the Documents section (server, application, or shared "
            "scope), recording its verification status and queueing indexing."
        ),
    )
    async def _docs_upsert(
        call: CapabilityCall, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        doc_store = _resolve_doc_store()
        scope, target_server_id, target_app_id = _resolve_doc_scope(
            call, payload, DOCS_UPSERT_CAPABILITY
        )
        ns = DocumentNamespace(str(payload.get("namespace", "docs")).strip())
        verification = VerificationStatus(
            str(payload.get("verification", "unverified")).strip()
        )
        raw_title = payload.get("title")
        title_str = (
            raw_title.strip()
            if isinstance(raw_title, str) and raw_title.strip()
            else None
        )
        body_text = str(payload["content"]).strip()
        if title_str is not None and not body_text.startswith("#"):
            markdown_text = f"# {title_str}\n\n{body_text}\n"
        else:
            markdown_text = body_text + ("\n" if not body_text.endswith("\n") else "")

        resolved_title = title_str or _extract_doc_title(markdown_text)
        raw_doc_id = payload.get("doc_id")
        raw_expected_rev = payload.get("expected_revision")
        target_doc_id: uuid.UUID | None = None
        expected_rev: int | None = (
            int(raw_expected_rev) if raw_expected_rev is not None else None
        )

        if isinstance(raw_doc_id, str) and raw_doc_id.strip():
            try:
                target_doc_id = uuid.UUID(raw_doc_id.strip())
            except ValueError:
                raise InternalApiError(
                    "invalid_request",
                    "docs.upsert: doc_id must be a canonical UUID string",
                ) from None
            if expected_rev is None:
                try:
                    existing = doc_store.read(
                        target_doc_id,
                        server_id=target_server_id,
                        app_id=target_app_id,
                        namespace=ns,
                    )
                    expected_rev = existing.revision
                except DocumentStoreError as exc:
                    if exc.code is DocumentStoreErrorCode.NOT_FOUND:
                        expected_rev = 0
                    else:
                        raise _map_doc_store_error(exc) from None
        else:
            # Match by normalized title in the same scope + namespace when
            # expected_revision was not explicitly pinned to 0.
            if expected_rev in (None, 0):
                try:
                    existing_docs = doc_store.list_documents(
                        server_id=target_server_id,
                        app_id=target_app_id,
                        namespace=ns,
                    )
                except DocumentStoreError as exc:
                    raise _map_doc_store_error(exc) from None
                if expected_rev is None and resolved_title:
                    norm_title = resolved_title.strip().lower()
                    for candidate in existing_docs:
                        if (
                            _extract_doc_title(candidate.text).strip().lower()
                            == norm_title
                        ):
                            target_doc_id = candidate.doc_id
                            expected_rev = candidate.revision
                            break
            if target_doc_id is None:
                target_doc_id = uuid.uuid4()
                if expected_rev is None:
                    expected_rev = 0
        eff_expected_rev = expected_rev if expected_rev is not None else 0

        try:
            stored = doc_store.write(
                target_doc_id,
                markdown_text,
                server_id=target_server_id,
                app_id=target_app_id,
                namespace=ns,
                expected_revision=eff_expected_rev,
                source_kind=SourceKind.AUTHORED,
                origin="agent",
                origin_revision=f"r{eff_expected_rev + 1}",
                verification=verification,
            )
        except DocumentStoreError as exc:
            raise _map_doc_store_error(exc) from None

        index_status = await _best_effort_reconcile(
            doc_store,
            server_id=target_server_id,
            app_id=target_app_id,
            doc_id=stored.doc_id,
            namespace=stored.namespace,
        )
        return {
            "doc_id": str(stored.doc_id),
            "scope": scope,
            "server_id": str(target_server_id)
            if target_server_id is not None
            else None,
            "app_id": str(target_app_id)
            if target_app_id is not None
            else None,
            "namespace": stored.namespace.value,
            "title": _extract_doc_title(stored.text),
            "revision": stored.revision,
            "sha256": stored.sha256,
            "citation": f"{stored.namespace.value}:{stored.doc_id}#r{stored.revision}",
            "source_kind": stored.source_kind.value,
            "origin": stored.origin,
            "origin_revision": stored.origin_revision,
            "verification": stored.verification.value,
            "index_status": index_status,
        }

    operations.append(_docs_upsert)

    # -- docs.delete -----------------------------------------------------------

    @internal_api_tool(
        capability_id=DOCS_DELETE_CAPABILITY,
        version="1.0.0",
        effect_class="mutate",
        resource_scope=["docs", "runbooks"],
        environments=["controller"],
        payload_model=DocsDeletePayload,
        description=(
            "Delete a canonical Markdown document or operational runbook from "
            "the Documents section by doc_id and record a durable tombstone."
        ),
    )
    async def _docs_delete(
        call: CapabilityCall, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        doc_store = _resolve_doc_store()
        scope, target_server_id, target_app_id = _resolve_doc_scope(
            call, payload, DOCS_DELETE_CAPABILITY
        )
        try:
            parsed_doc_id = uuid.UUID(str(payload["doc_id"]).strip())
        except ValueError:
            raise InternalApiError(
                "invalid_request",
                "docs.delete: doc_id must be a canonical UUID string",
            ) from None
        ns = DocumentNamespace(str(payload.get("namespace", "docs")).strip())
        raw_expected_rev = payload.get("expected_revision")
        if raw_expected_rev is not None:
            expected_rev = int(raw_expected_rev)
        else:
            try:
                existing = doc_store.read(
                    parsed_doc_id,
                    server_id=target_server_id,
                    app_id=target_app_id,
                    namespace=ns,
                )
            except DocumentStoreError as exc:
                raise _map_doc_store_error(exc) from None
            expected_rev = existing.revision

        try:
            intent = doc_store.delete(
                parsed_doc_id,
                server_id=target_server_id,
                app_id=target_app_id,
                namespace=ns,
                expected_revision=expected_rev,
            )
        except DocumentStoreError as exc:
            raise _map_doc_store_error(exc) from None

        index_status = await _best_effort_reconcile(
            doc_store,
            server_id=target_server_id,
            app_id=target_app_id,
            doc_id=parsed_doc_id,
            namespace=ns,
        )
        return {
            "doc_id": str(parsed_doc_id),
            "scope": scope,
            "server_id": str(target_server_id)
            if target_server_id is not None
            else None,
            "app_id": str(target_app_id)
            if target_app_id is not None
            else None,
            "namespace": ns.value,
            "deleted": True,
            "revision": intent.revision,
            "index_status": index_status,
        }

    operations.append(_docs_delete)

    # -- server.profile.update (FRR T5) --------------------------------------
    # Add-only read-root extension through the SAME canonical pipeline as the
    # HTTP surface (journal + revision pre-check); prompt.md is never touched.
    # Approval is FORCED at the bridge seam (dispatch_once pins ASK), so the
    # executor here is deliberately plain mutate code: it only runs after a
    # captain decision (resume path) or in an explicitly ASK-mode context.

    @internal_api_tool(
        capability_id=SERVER_PROFILE_UPDATE_CAPABILITY,
        version="1.0.0",
        effect_class="mutate",
        resource_scope=["server_profile"],
        environments=["controller"],
        payload_model=ServerProfileUpdatePayload,
        description=(
            "Request ADDING file-read roots to the target server's canonical "
            "profile (add-only merge; prompt text untouched). EVERY call "
            "requires explicit captain approval before it executes."
        ),
    )
    async def _server_profile_update(
        call: CapabilityCall, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if session_factory is None or data_root is None:
            raise InternalApiError(
                "unavailable",
                "canonical profile service is unavailable (composition incomplete)",
            )
        server_id = call.context.server_id
        if server_id is None or server_id == uuid.UUID(int=0):
            raise InternalApiError(
                "invalid_request",
                f"{SERVER_PROFILE_UPDATE_CAPABILITY} requires a pinned target "
                "server (no server_id payload field exists)",
            )
        requested: list[str] = list(payload.get("roots") or [])
        expected_revision = int(payload.get("expected_revision") or 0)

        # (1) Read the canonical profile (pattern of app.discover).
        try:
            store = ProfileFileStore(Path(data_root))
            stored = store.read(server_id)
            profile = parse_server_profile_text(stored.text)
        except InternalApiError:
            raise
        except Exception as exc:
            raise InternalApiError(
                "unavailable",
                f"canonical profile read failed ({type(exc).__name__})",
            ) from None

        # (2) Stale caller: structured refusal BEFORE any effect.
        if expected_revision != stored.revision:
            raise InternalApiError(
                "revision_conflict",
                f"expected revision {expected_revision} does not match the "
                f"stored profile revision {stored.revision}; re-read and retry",
            )

        # (3) Validate every requested root (canonical rules + the shared
        # deny list). Value-free refusals: the root itself is never echoed.
        seen: set[str] = set()
        for root in requested:
            if (
                not isinstance(root, str)
                or not root.startswith("/")
                or root == "/"
                or posixpath.normpath(root) != root
                or not root.isprintable()
                or "\x00" in root
            ):
                raise InternalApiError(
                    "path_denied",
                    "each root must be an absolute normalized directory path "
                    '(never "/", printable single line)',
                )
            if root in seen:
                raise InternalApiError(
                    "path_denied", "duplicate read root within the request"
                )
            seen.add(root)
            for prefix in _DENY_PATH_PREFIXES:
                if root == prefix or root.startswith(prefix + "/"):
                    raise InternalApiError(
                        "path_denied",
                        "read root matches the credential-material deny list",
                    )

        # (4) Add-only merge (preserve order, dedupe) with the canonical cap.
        merged = list(profile.file_read_roots)
        merged.extend(root for root in requested if root not in merged)
        if len(merged) > MAX_FILE_READ_ROOTS:
            raise InternalApiError(
                "too_many_roots",
                f"merging would exceed the {MAX_FILE_READ_ROOTS}-root cap",
            )

        # (5) Render the canonical document in the frozen field order (the
        # api/servers.py _canonical_document shape: file_read_roots after
        # capabilities, key omitted when empty) and write through the SAME
        # journal/revision pipeline as the HTTP surface. prompt.md untouched.
        document = yaml.safe_dump(
            {
                "schema_version": profile.schema_version,
                "server_id": str(profile.server_id),
                "name": profile.name,
                "prompt_revision": profile.prompt_revision,
                "environment": profile.environment.value,
                "ssh": {
                    "host": profile.ssh.host,
                    "port": profile.ssh.port,
                    "user": profile.ssh.user,
                    "host_key": profile.ssh.host_key,
                    "credential_ref": profile.ssh.credential_ref,
                },
                "privilege": profile.privilege.value,
                "capabilities": list(profile.capabilities),
                **(
                    {"file_read_roots": merged}
                    if merged
                    else {}
                ),
            },
            sort_keys=False,
            default_flow_style=False,
        )
        projection = ProfileProjection(session_factory)
        try:
            result = await projection.apply_upsert(
                store,
                server_id,
                document,
                expected_revision=stored.revision,
            )
        except InternalApiError:
            raise
        except Exception as exc:
            raise InternalApiError(
                "unavailable",
                f"canonical profile write failed ({type(exc).__name__})",
            ) from None
        if result.revision is None or result.sha256 is None:
            raise InternalApiError(
                "unavailable", "upsert outcome is missing its committed triple"
            )
        return {
            "status": "ok",
            "revision": result.revision,
            "sha256": result.sha256,
            "roots": merged,
        }

    operations.append(_server_profile_update)

    # -- registration ----------------------------------------------------------

    registry = CapabilityRegistry()
    executors: dict[str, Callable[[CapabilityCall], Any]] = {}
    for spec, operation in operations:
        descriptor = build_descriptor(spec)
        registry.register(descriptor)
        executors[spec.capability_id] = build_executor(spec, operation)
    # Also enforce the same fail-closed documentation gate on the sibling
    # internal-API descriptors included in INTERNAL_API_CAPABILITY_IDS.
    for sibling_desc in (upsert_map_descriptor(), chat_read_descriptor()):
        assert_agent_ready_descriptor(sibling_desc)
    return InternalApiComposition(
        descriptors=tuple(registry.list_capabilities()),
        executors=executors,
    )


__all__ = [
    "APP_ADOPT_CAPABILITY",
    "APP_DELETE_CATALOG_RUNBOOK_CAPABILITY",
    "APP_DELETE_MAP_ENTRY_CAPABILITY",
    "APP_DISCOVER_CAPABILITY",
    "APP_LIST_ADOPTED_CAPABILITY",
    "APP_LIST_CATALOG_CAPABILITY",
    "APP_UNADOPT_CAPABILITY",
    "APP_UPSERT_CATALOG_RUNBOOK_CAPABILITY",
    "AppAdoptPayload",
    "AppDeleteCatalogRunbookPayload",
    "AppDeleteMapEntryPayload",
    "AppDiscoverPayload",
    "AppListAdoptedPayload",
    "AppListCatalogPayload",
    "AppUnadoptPayload",
    "AppUpsertCatalogRunbookPayload",
    "DOCS_DELETE_CAPABILITY",
    "DOCS_LIST_CAPABILITY",
    "DOCS_READ_CAPABILITY",
    "DOCS_UPSERT_CAPABILITY",
    "DocsDeletePayload",
    "DocsListPayload",
    "DocsReadPayload",
    "DocsUpsertPayload",
    "INTERNAL_API_CAPABILITY_IDS",
    "InternalApiComposition",
    "InternalApiError",
    "InternalApiOperation",
    "InternalApiSpec",
    "MIN_FIELD_DESCRIPTION_CHARS",
    "MIN_TOOL_DESCRIPTION_CHARS",
    "SERVER_PROFILE_UPDATE_CAPABILITY",
    "ServerProfileUpdatePayload",
    "ServiceCategoryPayload",
    "ServiceInstructionsPayload",
    "assert_agent_ready_descriptor",
    "build_descriptor",
    "build_executor",
    "build_internal_api_capabilities",
    "internal_api_tool",
    "pydantic_to_descriptor_schema",
]
