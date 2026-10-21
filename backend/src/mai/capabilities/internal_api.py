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
    Mapping,
    Protocol,
    Sequence,
    get_args,
    get_origin,
)

from pydantic import BaseModel

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
        return None
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

    # Enum literals (Literal[...] and Enum subclasses) — the pattern that
    # Pydantic renders as {"enum": [...]} directly or via $defs/anyOf.
    enum_values = field_schema.get("enum")
    if isinstance(enum_values, list):
        projected["enum"] = list(enum_values)

    # Base JSON type: annotation first (authoritative for the bridge), then
    # the Pydantic fragment's own "type" when the annotation is opaque.
    json_types = _flatten_json_type(annotation)
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

    # Array items: project the item annotation's enum/type when known; an
    # unconstrained item stays an open object (the bridge permits any item
    # shape when no "items" keyword is declared).
    if projected.get("type") == "array":
        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin in (list, Sequence, tuple, frozenset, set) and args:
            item_annotation = args[0]
            item_enum = _enum_values_of(item_annotation)
            item_schema: dict[str, Any] = {}
            if item_enum is not None:
                item_schema["enum"] = item_enum
                item_type = _flatten_json_type(item_annotation)
                if item_type is not None:
                    item_schema["type"] = item_type[0]
            if item_schema:
                projected["items"] = item_schema

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

    # Nested object properties (inline BaseModel field): Pydantic inlines
    # them as {"$defs":..., "allOf":[{"$ref":...}]} — we re-project through
    # the referenced model's own schema when the annotation IS a BaseModel.
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        projected.update(_project_model_schema(annotation))

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
    registers. Mis-shaped declarations raise ``ValueError`` at import time
    (fail-closed composition, the sibling-module contract).
    """

    checked_id = _check_capability_id(capability_id)
    checked_effect = _check_effect_class(effect_class)
    checked_envs = _check_environments(tuple(environments))
    checked_scope = tuple(resource_scope)

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
            description=description,
        )
        return spec, operation

    return _wrap


def build_descriptor(spec: InternalApiSpec) -> CapabilityDescriptor:
    """The validated registry descriptor of one declared operation."""
    return parse_descriptor(
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
        envelope = {"status": "ok", **dict(result)}
        return ExecutorResult(ok=True, output=json.dumps(envelope, sort_keys=True))

    return _execute


# ---------------------------------------------------------------------------
# The app.* internal API operations (service adoption & operations surface)
# ---------------------------------------------------------------------------


class AppDiscoverPayload(BaseModel):
    """No-argument tool: the server's live inventory through the reviewed lanes.

    Discover live Docker Compose projects, systemd units, and standalone
    containers on the target server through the reviewed read-only lanes.
    Returns the observed inventory with origins, managed flags, and suggested
    categories — never writes.
    """

    model_config = {"extra": "forbid"}

    server_id: str | None = None


class AppListAdoptedPayload(BaseModel):
    """No-argument tool: adopted applications + Service Map entries page.

    List the adopted applications of the target server: the reconciled
    projection records (revision, sha256, adopted_at) merged with their
    Service Map entries (category, description, instructions). Discovery
    observations never merge into this list.
    """

    model_config = {"extra": "forbid"}

    server_id: str | None = None
    limit: int | None = None
    offset: int = 0


class AppAdoptPayload(BaseModel):
    """The adoption request (mirrors the HTTP ``AdoptBody`` surface).

    Adopt one application into Machina management: writes the canonical
    adoption manifest, reconciles the projection, seeds the application
    document, and upserts the Service Map entry in one operation. The origin
    must be a frozen selector (manual, compose, systemd_unit); standalone
    containers are refused.
    """

    model_config = {"extra": "forbid"}

    server_id: str | None = None
    application_id: str | None = None
    name: str
    origin: str = "manual"  # "manual" | "compose" | "systemd_unit"
    compose_project: str | None = None
    working_dir: str | None = None
    unit_name: str | None = None
    external_working_dir: str | None = None
    external_config_files: list[str] = []
    external_environment_files: list[str] = []
    expected_revision: int = 0
    category: str | None = None
    description: str | None = None
    instructions: dict[str, Any] | None = None
    upsert_categories: list[dict[str, Any]] = []


class AppUnadoptPayload(BaseModel):
    """Revoke/unadopt request; ``expected_revision`` auto-resolves when omitted.

    Remove one application from adoption (revoke): writes the canonical
    tombstone, deletes the projection row, and removes the Service Map entry.
    Machina stops managing the application; the running containers/units on
    the host are NOT touched.
    """

    model_config = {"extra": "forbid"}

    server_id: str | None = None
    application_id: str
    expected_revision: int | None = None


class AppDeleteMapEntryPayload(BaseModel):
    """Remove one Service Map entry by identity.

    Remove one Service Map entry by identity. Only manual (un-adopted)
    entries can be removed; adopted entries require app.unadopt first.
    """

    model_config = {"extra": "forbid"}

    server_id: str | None = None
    application_id: str


# ---------------------------------------------------------------------------
# The shared composition builder (app.py + worker.py consume the SAME result)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InternalApiComposition:
    """The built internal-API capability surface: descriptors + executors."""

    descriptors: tuple[CapabilityDescriptor, ...]
    executors: dict[str, Callable[[CapabilityCall], Any]]


def build_internal_api_capabilities(
    *,
    applications: "ApplicationManifestStore | None",
    adoption: "ApplicationAdoption | None",
    docs: "ApplicationDocService | None",
    service_map: "ServiceMapStore | None",
    session_factory: Callable[[], Any] | None = None,
) -> InternalApiComposition:
    """Build every ``app.*`` descriptor + executor over the composed state.

    ``None`` services (fail-closed app composition) produce executors whose
    every call answers ``{"status": "unavailable", ...}`` — the composition
    contract of the HTTP routes, mirrored for the agent lane.
    """
    # Deferred imports keep the module free of API-layer import cycles.
    from mai.api.applications import (
        ApplicationDocService,
        AdoptionRequest,
        ApplicationOrigin,
    )
    from mai.applications.service_map import ServiceMapStore

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
        if adoption is None:
            raise InternalApiError(
                "unavailable", "applications service is unavailable"
            )
        server_id = call.context.server_id
        if server_id is None:
            raise InternalApiError(
                "invalid_request",
                "app.discover requires a pinned server context",
            )
        try:
            inventory = await adoption.discover(server_id)
        except Exception as exc:
            raise InternalApiError(
                "discovery_failed",
                f"bounded discovery failed: {type(exc).__name__}",
            ) from None
        applications_view: list[dict[str, Any]] = []
        for observed in inventory.applications:
            detail = observed.detail
            applications_view.append(
                {
                    "origin": observed.origin,
                    "managed": observed.managed,
                    "application_id": (
                        str(observed.application_id)
                        if observed.application_id is not None
                        else None
                    ),
                    "name": getattr(
                        detail, "project_name", getattr(detail, "unit", getattr(
                            getattr(detail, "container", None), "name", "unknown"
                        ))
                    ),
                }
            )
        return {
            "server_id": str(server_id),
            "observed_at": inventory.observed_at.isoformat(),
            "applications": applications_view,
            "listen_ports": [dict(p) for p in inventory.listen_ports],
            "lanes": [
                {
                    "lane": lane.lane.value,
                    "state": lane.state.value,
                    "detail": lane.detail,
                }
                for lane in inventory.lanes
            ],
        }

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
        server_id = call.context.server_id
        if server_id is None:
            raise InternalApiError(
                "invalid_request",
                "app.list_adopted requires a pinned server context",
            )
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
            if aid not in adopted_ids:
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
        return {
            "server_id": str(server_id),
            "applications": applications,
            "total": len(applications),
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
        server_id = call.context.server_id
        if server_id is None:
            raise InternalApiError(
                "invalid_request",
                "app.adopt requires a pinned server context",
            )
        origin_value = payload.get("origin", "manual")
        try:
            origin = ApplicationOrigin(origin_value)
        except ValueError:
            raise InternalApiError(
                "invalid_request",
                f"origin must be one of {[o.value for o in ApplicationOrigin]}",
            ) from None
        request = AdoptionRequest(
            server_id=server_id,
            application_id=(
                uuid.UUID(str(payload["application_id"]))
                if payload.get("application_id")
                else None
            ),
            name=str(payload["name"]),
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
            name=str(payload["name"]),
            origin=origin.value,
            compose_project=payload.get("compose_project"),
            working_dir=payload.get("working_dir"),
            unit_name=payload.get("unit_name"),
            category=str(payload.get("category", "system")),
            description=str(payload.get("description", "")),
            instructions=payload.get("instructions") or {},
        )
        # Canonical identity document (honest pending on doc-service gaps).
        doc_outcome = {"status": "pending", "doc_id": None, "reason": None}
        if docs is not None:
            try:
                doc = await docs.record_adopted(
                    server_id=server_id,
                    application_id=result.application_id,
                    name=str(payload["name"]),
                )
                doc_outcome = {
                    "status": "documented",
                    "doc_id": str(doc.doc_id),
                    "reason": None,
                }
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
        server_id = call.context.server_id
        if server_id is None:
            raise InternalApiError(
                "invalid_request",
                "app.unadopt requires a pinned server context",
            )
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
        server_id = call.context.server_id
        if server_id is None:
            raise InternalApiError(
                "invalid_request",
                "app.delete_map_entry requires a pinned server context",
            )
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
    "build_descriptor",
    "build_executor",
    "build_internal_api_capabilities",
    "internal_api_tool",
    "pydantic_to_descriptor_schema",
]
