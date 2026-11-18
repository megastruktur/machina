"""Agent read tools over the knowledge read store (ask_20, campaign D2/C1/C3).

THREE read-only agent capabilities over
:class:`mai.applications.knowledge_read.KnowledgeReadStore`:

- ``app.resolve_service`` — same-server service resolution (exact
  application id, canonical selector, bounded candidate page);
- ``app.get_service_knowledge`` — the bounded per-service knowledge read
  (operator map state and catalog runbook clearly separated from the
  active learned-fact page and the conflict summary);
- ``app.get_learning_status`` — the latest durable operation receipt.

Read-only honesty (AC): the executors perform NO writes anywhere — no
map upserts, no lock acquisition, no session commits. The store is
injected pre-built; the capability never constructs engines.

Scope vocabulary (C3): the ONLY target server is
``call.context.server_id``. A payload ``server_id`` may CONFIRM it; an
explicit DIFFERENT server id is rejected (``scope_mismatch``), never
substituted. (Unlike ``internal_api._resolve_server_id``, this module
never allows payload retargeting.)

Every collection result is envelope-checked against the 64 KiB
dispatcher cap via
:func:`mai.applications.knowledge_contracts.enforce_envelope_bound` —
an overflow answers a structured ``envelope_overflow`` refusal, never a
sliced/truncated JSON document.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mai.applications.knowledge_contracts import (
    EnvelopeOverflowError,
    ScopeMismatchError,
    enforce_envelope_bound,
)
from mai.applications.knowledge_read import (
    KnowledgeReadStore,
    ServiceKnowledgeNotFoundError,
)
from mai.capabilities.dispatch import CapabilityCall, ExecutorResult
from mai.plugins.registry import CapabilityDescriptor, parse_descriptor
from mai.policy.evaluate import ExecutionContext

__all__ = [
    "GET_LEARNING_STATUS_CAPABILITY_ID",
    "GET_SERVICE_KNOWLEDGE_CAPABILITY_ID",
    "GetLearningStatusCapability",
    "GetLearningStatusPayload",
    "GetServiceKnowledgeCapability",
    "GetServiceKnowledgePayload",
    "RESOLVE_SERVICE_CAPABILITY_ID",
    "ResolveServiceCapability",
    "ResolveServicePayload",
    "get_learning_status_descriptor",
    "get_service_knowledge_descriptor",
    "resolve_service_descriptor",
]

#: Agent capability identifiers (frozen vocabulary, independent of the
#: ``app.upsert_map_entry`` writer capability).
RESOLVE_SERVICE_CAPABILITY_ID: Final[str] = "app.resolve_service"
GET_SERVICE_KNOWLEDGE_CAPABILITY_ID: Final[str] = "app.get_service_knowledge"
GET_LEARNING_STATUS_CAPABILITY_ID: Final[str] = "app.get_learning_status"

#: The strictly-under-64-KiB dispatcher cap (same constant family the
#: envelope check enforces).
_MAX_ENVELOPE_BYTES: Final[int] = 64 * 1024


# ---------------------------------------------------------------------------
# payload models (strict; every field described; C1-bounded paging)
# ---------------------------------------------------------------------------


class _Payload(BaseModel):
    """Base for the read-tool payloads (fail-closed capture)."""

    model_config = ConfigDict(extra="forbid")


class ResolveServicePayload(_Payload):
    """Resolve one same-server service by id, canonical selector, or
    bounded name/selector candidate page."""

    application_id: uuid.UUID | None = Field(
        default=None,
        description="Canonical application identity for an exact-id resolution",
    )
    name_query: str | None = Field(
        default=None,
        max_length=120,
        description="Bounded name/category/selector substring for the candidate page",
    )
    compose_project: str | None = Field(
        default=None,
        description="Compose project selector for an exact-selector resolution",
    )
    working_dir: str | None = Field(
        default=None,
        description="Compose working directory narrowing the compose selector",
    )
    unit_name: str | None = Field(
        default=None,
        description="Systemd unit selector for an exact-selector resolution",
    )
    server_id: str | None = Field(
        default=None,
        description="Scope confirmation: must equal the context server identity",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of candidates returned in this page",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Zero-based offset of this page into the candidate list",
    )


class GetServiceKnowledgePayload(_Payload):
    """Read the bounded per-service knowledge state of ONE service."""

    application_id: uuid.UUID = Field(
        description="Canonical application identity whose knowledge is read",
    )
    include_conflicts: bool = Field(
        default=False,
        description="True also surfaces current-generation CONFLICT facts",
    )
    server_id: str | None = Field(
        default=None,
        description="Scope confirmation: must equal the context server identity",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of ACTIVE learned facts in this page",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Zero-based offset of this page into the active facts",
    )


class GetLearningStatusPayload(_Payload):
    """Read the latest durable learning-operation receipt for filters."""

    application_id: uuid.UUID | None = Field(
        default=None,
        description="Canonical application identity narrowing the receipt lookup",
    )
    operation_id: uuid.UUID | None = Field(
        default=None,
        description="Exact durable operation identity to look up",
    )
    source_run_id: uuid.UUID | None = Field(
        default=None,
        description="Source run identity narrowing the receipt lookup",
    )
    server_id: str | None = Field(
        default=None,
        description="Scope confirmation: must equal the context server identity",
    )


# ---------------------------------------------------------------------------
# scope resolution (C3: confirm, never retarget)
# ---------------------------------------------------------------------------


def _resolve_scope(
    call: CapabilityCall,
    payload: Mapping[str, Any],
    capability_id: str,
) -> tuple[uuid.UUID, ExecutorResult | None]:
    """Resolve the effective server scope for one call.

    Returns ``(server_id, None)`` on success or
    ``(context.server_id, refusal)`` when the payload's ``server_id``
    is malformed or names a DIFFERENT server (C3: rejected, never
    substituted)."""
    context = call.context
    if not isinstance(context, ExecutionContext):
        raise ValueError("call context must be an ExecutionContext")
    server_id = uuid.UUID(str(context.server_id))
    raw = payload.get("server_id")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return server_id, None
    try:
        confirmed = uuid.UUID(str(raw).strip())
    except (ValueError, AttributeError):
        return server_id, _refusal(
            capability_id,
            "invalid_request",
            f"server_id must be a canonical UUID string, got {raw!r}",
        )
    if confirmed != server_id:
        detail = (
            f"payload server_id {confirmed} is outside the authorized "
            f"scope {server_id}"
        )
        return server_id, _refusal(capability_id, "scope_mismatch", detail)
    return server_id, None


def _refusal(
    capability_id: str,
    code: str,
    message: str,
) -> ExecutorResult:
    """One structured refusal envelope (value-free, bounded)."""
    return ExecutorResult(
        ok=False,
        error_code=code,
        output=json.dumps(
            {
                "status": "refused",
                "code": code,
                "message": message[:500],
                "capability_id": capability_id,
            },
            sort_keys=True,
        ),
    )


def _payload_error(
    capability_id: str,
    error: ValidationError,
) -> ExecutorResult:
    """Map payload-model validation onto a bounded field-error refusal
    (at most six field errors, C01 value-free)."""
    errors = error.errors()[:6]
    rendered = "; ".join(
        f"{'.'.join(str(part) for part in item.get('loc', ())) or 'payload'}: "
        f"{item.get('msg', 'invalid')}"
        for item in errors
    )
    return _refusal(capability_id, "invalid_request", rendered or "invalid payload")


def _ok_envelope(
    capability_id: str,
    envelope: dict[str, Any],
) -> ExecutorResult:
    """Serialize, envelope-check (< 64 KiB), and wrap one success."""
    try:
        encoded = enforce_envelope_bound(
            json.dumps(envelope, sort_keys=True).encode("utf-8")
        )
    except EnvelopeOverflowError as error:
        return _refusal(
            capability_id,
            "envelope_overflow",
            (
                f"serialized envelope is {error.actual_bytes} bytes, at or "
                f"above the {_MAX_ENVELOPE_BYTES}-byte cap; narrow the page"
            ),
        )
    return ExecutorResult(ok=True, output=encoded.decode("utf-8"))


# ---------------------------------------------------------------------------
# descriptor builders (fail-closed agent readiness at import time)
# ---------------------------------------------------------------------------


def _page_properties() -> dict[str, Any]:
    return {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "default": 10,
            "description": "Maximum number of records returned in this page",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": "Zero-based offset of this page into the collection",
        },
    }


def _server_id_property() -> dict[str, Any]:
    return {
        "type": ["string", "null"],
        "description": (
            "Scope confirmation: must equal the context server identity"
        ),
    }


def resolve_service_descriptor() -> CapabilityDescriptor:
    """The validated ``app.resolve_service`` descriptor (C2/C3)."""
    descriptor = parse_descriptor(
        {
            "identifier": RESOLVE_SERVICE_CAPABILITY_ID,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "description": (
                    "Resolve one service on the target server by exact "
                    "application id, canonical selector, or a bounded "
                    "name/selector candidate page. Read-only."
                ),
                "required": [],
                "properties": {
                    "application_id": {
                        "type": ["string", "null"],
                        "format": "uuid",
                        "description": (
                            "Canonical application identity for an exact-id "
                            "resolution"
                        ),
                    },
                    "name_query": {
                        "type": ["string", "null"],
                        "maxLength": 120,
                        "description": (
                            "Bounded name/category/selector substring for "
                            "the candidate page"
                        ),
                    },
                    "compose_project": {
                        "type": ["string", "null"],
                        "description": (
                            "Compose project selector for an exact-selector "
                            "resolution"
                        ),
                    },
                    "working_dir": {
                        "type": ["string", "null"],
                        "description": (
                            "Compose working directory narrowing the compose "
                            "selector"
                        ),
                    },
                    "unit_name": {
                        "type": ["string", "null"],
                        "description": (
                            "Systemd unit selector for an exact-selector "
                            "resolution"
                        ),
                    },
                    "server_id": _server_id_property(),
                    **_page_properties(),
                },
            },
            "output_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "status",
                    "exact_match",
                    "match_kind",
                    "ambiguous",
                    "candidates",
                    "total",
                    "offset",
                    "limit",
                    "has_more",
                ],
                "properties": {
                    "status": {"type": "string", "enum": ["ok"]},
                    "exact_match": {"type": "boolean"},
                    "match_kind": {"type": "string"},
                    "ambiguous": {"type": "boolean"},
                    "candidates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "application_id",
                                "server_id",
                                "name",
                                "origin",
                                "category",
                                "installed",
                                "map_revision",
                                "generation",
                                "service_revision",
                                "knowledge_revision",
                                "manual_identity",
                                "auto_adopt_suppressed",
                            ],
                            "properties": {
                                "application_id": {
                                    "type": "string",
                                    "format": "uuid",
                                },
                                "server_id": {
                                    "type": "string",
                                    "format": "uuid",
                                },
                                "name": {"type": "string"},
                                "origin": {"type": "string"},
                                "compose_project": {
                                    "type": ["string", "null"],
                                },
                                "working_dir": {
                                    "type": ["string", "null"],
                                },
                                "unit_name": {
                                    "type": ["string", "null"],
                                },
                                "category": {"type": "string"},
                                "installed": {"type": "boolean"},
                                "map_revision": {"type": "integer"},
                                "generation": {"type": "integer"},
                                "service_revision": {"type": "integer"},
                                "knowledge_revision": {"type": "integer"},
                                "manual_identity": {"type": "boolean"},
                                "auto_adopt_suppressed": {
                                    "type": "boolean"
                                },
                            },
                        },
                    },
                    "total": {"type": "integer", "minimum": 0},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1},
                    "has_more": {"type": "boolean"},
                },
            },
            "execution_environments": ["controller"],
            "effect_class": "read",
            "resource_scope": ["service_map", "knowledge"],
            "required_grants": [RESOLVE_SERVICE_CAPABILITY_ID],
        }
    )
    from mai.capabilities.internal_api import assert_agent_ready_descriptor

    return assert_agent_ready_descriptor(descriptor)


def get_service_knowledge_descriptor() -> CapabilityDescriptor:
    """The validated ``app.get_service_knowledge`` descriptor (C2/C3)."""
    descriptor = parse_descriptor(
        {
            "identifier": GET_SERVICE_KNOWLEDGE_CAPABILITY_ID,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "description": (
                    "Read the bounded per-service knowledge state: operator "
                    "map entry and catalog runbook separated from the ACTIVE "
                    "learned-fact page and the conflict summary. Read-only."
                ),
                "required": ["application_id"],
                "properties": {
                    "application_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "Canonical application identity whose knowledge "
                            "is read"
                        ),
                    },
                    "include_conflicts": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "True also surfaces current-generation CONFLICT "
                            "facts"
                        ),
                    },
                    "server_id": _server_id_property(),
                    **_page_properties(),
                },
            },
            "output_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "status",
                    "server_id",
                    "application_id",
                    "found_in_map",
                    "generation",
                    "service_revision",
                    "knowledge_revision",
                    "manual_identity",
                    "auto_adopt_suppressed",
                    "conflict_count",
                    "conflicts",
                    "facts",
                ],
                "properties": {
                    "status": {"type": "string", "enum": ["ok"]},
                    "server_id": {"type": "string", "format": "uuid"},
                    "application_id": {"type": "string", "format": "uuid"},
                    "found_in_map": {"type": "boolean"},
                    "generation": {"type": "integer", "minimum": 1},
                    "service_revision": {"type": "integer", "minimum": 0},
                    "knowledge_revision": {"type": "integer", "minimum": 0},
                    "manual_identity": {"type": "boolean"},
                    "auto_adopt_suppressed": {"type": "boolean"},
                    "name": {"type": ["string", "null"]},
                    "origin": {"type": ["string", "null"]},
                    "category": {"type": ["string", "null"]},
                    "description": {"type": ["string", "null"]},
                    "custom_instructions": {"type": ["string", "null"]},
                    "instructions": {"type": ["object", "null"]},
                    "installed": {"type": ["boolean", "null"]},
                    "map_revision": {"type": ["integer", "null"]},
                    "catalog_runbook": {"type": ["object", "null"]},
                    "conflict_count": {"type": "integer", "minimum": 0},
                    "conflicts": {"type": "array"},
                    "facts": {"type": "object"},
                },
            },
            "execution_environments": ["controller"],
            "effect_class": "read",
            "resource_scope": ["service_map", "knowledge"],
            "required_grants": [GET_SERVICE_KNOWLEDGE_CAPABILITY_ID],
        }
    )
    from mai.capabilities.internal_api import assert_agent_ready_descriptor

    return assert_agent_ready_descriptor(descriptor)


def get_learning_status_descriptor() -> CapabilityDescriptor:
    """The validated ``app.get_learning_status`` descriptor (C2/C3)."""
    descriptor = parse_descriptor(
        {
            "identifier": GET_LEARNING_STATUS_CAPABILITY_ID,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "description": (
                    "Read the latest durable learning-operation receipt "
                    "matching the optional application, operation, or source "
                    "run filters. Read-only; answers found=false when none "
                    "exists."
                ),
                "required": [],
                "properties": {
                    "application_id": {
                        "type": ["string", "null"],
                        "format": "uuid",
                        "description": (
                            "Canonical application identity narrowing the "
                            "receipt lookup"
                        ),
                    },
                    "operation_id": {
                        "type": ["string", "null"],
                        "format": "uuid",
                        "description": (
                            "Exact durable operation identity to look up"
                        ),
                    },
                    "source_run_id": {
                        "type": ["string", "null"],
                        "format": "uuid",
                        "description": (
                            "Source run identity narrowing the receipt lookup"
                        ),
                    },
                    "server_id": _server_id_property(),
                },
            },
            "output_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["status", "found"],
                "properties": {
                    "status": {"type": "string", "enum": ["ok"]},
                    "found": {"type": "boolean"},
                    "operation_id": {"type": "string", "format": "uuid"},
                    "receipt_status": {"type": "string"},
                    "payload_digest": {"type": "string"},
                    "sub_effects": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "accepted_at": {"type": "string"},
                    "workflow_id": {"type": "string"},
                },
            },
            "execution_environments": ["controller"],
            "effect_class": "read",
            "resource_scope": ["knowledge"],
            "required_grants": [GET_LEARNING_STATUS_CAPABILITY_ID],
        }
    )
    from mai.capabilities.internal_api import assert_agent_ready_descriptor

    return assert_agent_ready_descriptor(descriptor)


# ---------------------------------------------------------------------------
# executors (read-only; one store injected, never constructed here)
# ---------------------------------------------------------------------------


class ResolveServiceCapability:
    """``app.resolve_service`` executor: scope-confirmed resolution."""

    def __init__(self, store: KnowledgeReadStore) -> None:
        if not isinstance(store, KnowledgeReadStore):
            raise ValueError("store must be a KnowledgeReadStore")
        self._store = store

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        if not isinstance(call, CapabilityCall):
            raise ValueError("executor input must be a dispatcher CapabilityCall")
        if not isinstance(call.context, ExecutionContext):
            raise ValueError("call context must be an ExecutionContext")
        payload_raw = call.payload if isinstance(call.payload, Mapping) else {}
        server_id, refusal = _resolve_scope(
            call, payload_raw, RESOLVE_SERVICE_CAPABILITY_ID
        )
        if refusal is not None:
            return refusal
        try:
            payload = ResolveServicePayload.model_validate(dict(payload_raw))
        except ValidationError as error:
            return _payload_error(RESOLVE_SERVICE_CAPABILITY_ID, error)
        selector: Any = None
        selector_parts = {
            "compose_project": payload.compose_project,
            "compose_directory": payload.working_dir,
            "systemd_unit": payload.unit_name,
        }
        if any(value is not None for value in selector_parts.values()):
            from types import SimpleNamespace

            selector = SimpleNamespace(**selector_parts)
        try:
            result = await self._store.resolve_service(
                server_id=server_id,
                application_id=payload.application_id,
                selector=selector,
                query=payload.name_query,
                limit=payload.limit,
                offset=payload.offset,
            )
        except ServiceKnowledgeNotFoundError as error:
            return _refusal(
                RESOLVE_SERVICE_CAPABILITY_ID,
                "not_found",
                str(error),
            )
        except ScopeMismatchError as error:
            return _refusal(
                RESOLVE_SERVICE_CAPABILITY_ID,
                "scope_mismatch",
                str(error),
            )
        except EnvelopeOverflowError as error:
            return _refusal(
                RESOLVE_SERVICE_CAPABILITY_ID,
                "envelope_overflow",
                str(error),
            )
        except ValueError as error:
            return _refusal(
                RESOLVE_SERVICE_CAPABILITY_ID,
                "invalid_request",
                str(error),
            )
        envelope = {"status": "ok", **result.model_dump(mode="json")}
        return _ok_envelope(RESOLVE_SERVICE_CAPABILITY_ID, envelope)


class GetServiceKnowledgeCapability:
    """``app.get_service_knowledge`` executor: bounded knowledge read."""

    def __init__(self, store: KnowledgeReadStore) -> None:
        if not isinstance(store, KnowledgeReadStore):
            raise ValueError("store must be a KnowledgeReadStore")
        self._store = store

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        if not isinstance(call, CapabilityCall):
            raise ValueError("executor input must be a dispatcher CapabilityCall")
        if not isinstance(call.context, ExecutionContext):
            raise ValueError("call context must be an ExecutionContext")
        payload_raw = call.payload if isinstance(call.payload, Mapping) else {}
        server_id, refusal = _resolve_scope(
            call, payload_raw, GET_SERVICE_KNOWLEDGE_CAPABILITY_ID
        )
        if refusal is not None:
            return refusal
        try:
            payload = GetServiceKnowledgePayload.model_validate(dict(payload_raw))
        except ValidationError as error:
            return _payload_error(GET_SERVICE_KNOWLEDGE_CAPABILITY_ID, error)
        try:
            result = await self._store.get_service_knowledge(
                server_id=server_id,
                application_id=payload.application_id,
                limit=payload.limit,
                offset=payload.offset,
                include_conflicts=payload.include_conflicts,
            )
        except ServiceKnowledgeNotFoundError as error:
            return _refusal(
                GET_SERVICE_KNOWLEDGE_CAPABILITY_ID,
                "not_found",
                str(error),
            )
        except ScopeMismatchError as error:
            return _refusal(
                GET_SERVICE_KNOWLEDGE_CAPABILITY_ID,
                "scope_mismatch",
                str(error),
            )
        except EnvelopeOverflowError as error:
            return _refusal(
                GET_SERVICE_KNOWLEDGE_CAPABILITY_ID,
                "envelope_overflow",
                str(error),
            )
        except ValueError as error:
            return _refusal(
                GET_SERVICE_KNOWLEDGE_CAPABILITY_ID,
                "invalid_request",
                str(error),
            )
        envelope = {"status": "ok", **result.model_dump(mode="json")}
        return _ok_envelope(GET_SERVICE_KNOWLEDGE_CAPABILITY_ID, envelope)


class GetLearningStatusCapability:
    """``app.get_learning_status`` executor: latest receipt or honest
    ``found: false``."""

    def __init__(self, store: KnowledgeReadStore) -> None:
        if not isinstance(store, KnowledgeReadStore):
            raise ValueError("store must be a KnowledgeReadStore")
        self._store = store

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        if not isinstance(call, CapabilityCall):
            raise ValueError("executor input must be a dispatcher CapabilityCall")
        if not isinstance(call.context, ExecutionContext):
            raise ValueError("call context must be an ExecutionContext")
        payload_raw = call.payload if isinstance(call.payload, Mapping) else {}
        server_id, refusal = _resolve_scope(
            call, payload_raw, GET_LEARNING_STATUS_CAPABILITY_ID
        )
        if refusal is not None:
            return refusal
        try:
            payload = GetLearningStatusPayload.model_validate(dict(payload_raw))
        except ValidationError as error:
            return _payload_error(GET_LEARNING_STATUS_CAPABILITY_ID, error)
        try:
            receipt = await self._store.get_learning_status(
                server_id=server_id,
                application_id=payload.application_id,
                operation_id=payload.operation_id,
                source_run_id=payload.source_run_id,
            )
        except ScopeMismatchError as error:
            return _refusal(
                GET_LEARNING_STATUS_CAPABILITY_ID,
                "scope_mismatch",
                str(error),
            )
        except EnvelopeOverflowError as error:
            return _refusal(
                GET_LEARNING_STATUS_CAPABILITY_ID,
                "envelope_overflow",
                str(error),
            )
        except ValueError as error:
            return _refusal(
                GET_LEARNING_STATUS_CAPABILITY_ID,
                "invalid_request",
                str(error),
            )
        if receipt is None:
            envelope: dict[str, Any] = {"status": "ok", "found": False}
            return _ok_envelope(GET_LEARNING_STATUS_CAPABILITY_ID, envelope)
        receipt_data = receipt.model_dump(mode="json")
        receipt_status = receipt_data.pop("status")
        envelope = {
            "status": "ok",
            "found": True,
            "receipt_status": receipt_status,
            **receipt_data,
        }
        return _ok_envelope(GET_LEARNING_STATUS_CAPABILITY_ID, envelope)
