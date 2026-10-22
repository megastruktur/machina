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
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Final,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    get_args,
    get_origin,
)

from pydantic import BaseModel, Field

from mai.applications.service_map import (
    CAPABILITY_ID as UPSERT_MAP_CAPABILITY,
    ServiceMapUpsertCapability,
    upsert_map_descriptor,
)
from mai.capabilities.chat_read import (
    CAPABILITY_ID as CHAT_READ_CAPABILITY,
    ChatReadSessionCapability,
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
    from mai.applications.service_map import ServiceMapStore

# ---------------------------------------------------------------------------
# Capability identifiers (frozen vocabulary)
# ---------------------------------------------------------------------------

APP_DISCOVER_CAPABILITY: Final = "app.discover"
APP_LIST_ADOPTED_CAPABILITY: Final = "app.list_adopted"
APP_ADOPT_CAPABILITY: Final = "app.adopt"
APP_UNADOPT_CAPABILITY: Final = "app.unadopt"
APP_DELETE_MAP_ENTRY_CAPABILITY: Final = "app.delete_map_entry"

#: The internal-API capability set the chat agent exposes by default across
#: both pinned-server conversations and General Consult (the UI/agent surface
#: parity anchor).
INTERNAL_API_CAPABILITY_IDS: Final[tuple[str, ...]] = (
    APP_DISCOVER_CAPABILITY,
    APP_LIST_ADOPTED_CAPABILITY,
    APP_ADOPT_CAPABILITY,
    APP_UNADOPT_CAPABILITY,
    UPSERT_MAP_CAPABILITY,
    APP_DELETE_MAP_ENTRY_CAPABILITY,
    CHAT_READ_CAPABILITY,
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
    expected_revision: int = Field(
        default=0,
        description="Optimistic concurrency revision: 0 when adopting a new service, or the current revision when updating.",
    )
    category: str | None = Field(
        default=None,
        description=(
            "Service Map category slug (e.g. 'system', 'ai', 'media', 'network', "
            "'storage', 'monitoring', 'security', 'games', or a custom slug)."
        ),
    )
    description: str | None = Field(
        default=None,
        description="Concise summary of what the service does and how it is configured on this host.",
    )
    instructions: ServiceInstructionsPayload | None = Field(
        default=None,
        description=(
            "Operational runbook object with optional string keys: "
            "'install', 'update', 'uninstall', 'backup', 'verify'."
        ),
    )
    upsert_categories: list[ServiceCategoryPayload] = Field(
        default_factory=list,
        description=(
            "Optional list of custom categories to create/update: "
            "[{'machine_id': '...', 'label': '...', 'description': '...'}]."
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
        ApplicationDocService,
        AdoptionRequest,
        ApplicationOrigin,
        _render_manual_record,
        resolve_identity_key,
    )
    from mai.applications.discover import discover_server
    from mai.applications.document import record_doc_id
    from mai.applications.service_map import ServiceMapStore, entry_digest
    from mai.contracts.server import parse_server_profile_text
    from mai.knowledge.documents import (
        DocumentNamespace,
        DocumentStore,
        DocumentStoreError,
        DocumentStoreErrorCode,
        SourceKind,
        VerificationStatus,
    )
    from mai.profiles.files import ProfileFileStore

    operations: list[tuple[InternalApiSpec, InternalApiOperation]] = []

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
        server_id = _resolve_server_id(call, payload, APP_ADOPT_CAPABILITY)
        origin_value = payload.get("origin", "manual")
        name = str(payload["name"])
        application_id: uuid.UUID
        if payload.get("application_id"):
            application_id = uuid.UUID(str(payload["application_id"]))
        else:
            application_id = uuid.uuid4()

        if origin_value == "manual":
            # The chat/manual addition: persist directly in the Service Map +
            # canonical identity doc (the UI's `_adopt_manual_entry` flow).
            if payload.get("upsert_categories"):
                for category in payload["upsert_categories"]:
                    service_map.upsert_category(
                        str(category["machine_id"]),
                        label=str(category.get("label", category["machine_id"])),
                        description=str(category.get("description", "")),
                    )
            entry = service_map.upsert_entry(
                server_id,
                name=name,
                category=payload.get("category") or "system",
                origin="manual",
                application_id=application_id,
                compose_project=payload.get("compose_project"),
                working_dir=payload.get("working_dir"),
                unit_name=payload.get("unit_name"),
                description=payload.get("description") or "",
                instructions=payload.get("instructions") or {},
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
        request = AdoptionRequest(
            server_id=server_id,
            application_id=application_id,  # type: ignore[arg-type]
            name=name,
            origin=origin,
            compose_project=payload.get("compose_project"),
            working_dir=payload.get("working_dir"),
            unit_name=payload.get("unit_name"),
            external_working_dir=payload.get("external_working_dir"),
            external_config_files=tuple(payload.get("external_config_files", [])),
            external_environment_files=tuple(
                payload.get("external_environment_files", [])
            ),
            expected_revision=int(payload.get("expected_revision", 0)),
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
        # Service Map + categories (the UI's adopt flow).
        if payload.get("upsert_categories"):
            for category in payload["upsert_categories"]:
                service_map.upsert_category(
                    str(category["machine_id"]),
                    label=str(category.get("label", category["machine_id"])),
                    description=str(category.get("description", "")),
                )
        service_map.upsert_entry(
            server_id,
            application_id=result.application_id,
            name=name,
            origin=origin.value,
            compose_project=payload.get("compose_project"),
            working_dir=payload.get("working_dir"),
            unit_name=payload.get("unit_name"),
            category=str(payload.get("category", "system")),
            description=str(payload.get("description", "")),
            instructions=payload.get("instructions") or {},
        )
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
            "outcome": result.outcome,
            "revision": result.revision,
            "sha256": result.sha256,
            "record_doc": doc_outcome,
        }

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
        server_id = _resolve_server_id(call, payload, APP_DELETE_MAP_ENTRY_CAPABILITY)
        application_id = uuid.UUID(str(payload["application_id"]))
        removed = service_map.delete_entry(server_id, application_id)
        if not removed:
            raise InternalApiError(
                "not_found",
                f"no service map entry stored for {application_id}",
            )
        return {
            "application_id": str(application_id),
            "server_id": str(server_id),
            "removed": True,
        }

    operations.append(_app_delete_map_entry)

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
    "APP_DELETE_MAP_ENTRY_CAPABILITY",
    "APP_DISCOVER_CAPABILITY",
    "APP_LIST_ADOPTED_CAPABILITY",
    "APP_UNADOPT_CAPABILITY",
    "INTERNAL_API_CAPABILITY_IDS",
    "InternalApiComposition",
    "InternalApiError",
    "InternalApiOperation",
    "InternalApiSpec",
    "MIN_FIELD_DESCRIPTION_CHARS",
    "MIN_TOOL_DESCRIPTION_CHARS",
    "ServiceCategoryPayload",
    "ServiceInstructionsPayload",
    "assert_agent_ready_descriptor",
    "build_descriptor",
    "build_executor",
    "build_internal_api_capabilities",
    "internal_api_tool",
    "pydantic_to_descriptor_schema",
]
