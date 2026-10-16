"""Agent-facing capability tools behind the enforced dispatch boundary.

Card ``06-tool-bridge`` against contracts.md **C06 rule 4** ("the
agent-to-tool bridge passes the ``ExecutionContext`` through the same
dispatcher used by deterministic UI/job calls"; "PydanticAI deferred
tools are not an authorization boundary") and **C03 rules 2–7**.

What this module is
-------------------

A presentation-only composition layer between two reviewed producers:
the declarative :class:`~mai.plugins.registry.CapabilityRegistry` (C03
rule 1) and the enforced boundary
:class:`~mai.capabilities.dispatch.CapabilityDispatcher` (C03). The
:class:`CapabilityToolBridge` turns the descriptors that are *enabled
for one* :class:`~mai.policy.evaluate.ExecutionContext` into PydanticAI
:class:`~pydantic_ai.tools.Tool` objects and routes every invocation —
model-facing or host resume — through that one dispatcher.

Fail-closed properties (each pinned by ``backend/tests/test_agent_tools.py``):

1. **Exposure is not authorization.** Only capabilities that are both
   registered and named in ``context.allowed_capabilities`` become
   tools. The dispatcher re-resolves current grants/policy/adoption and
   rechecks immediately before every effect, so a stale bridge object
   can never widen anything.
2. **Schema mismatch never dispatches.** PydanticAI schema-declared
   tools skip argument validation, so the bridge validates the model's
   payload against the descriptor's ``input_schema`` *before* calling
   the dispatcher. A mismatch returns a structured ``denied`` result —
   no state read, no approval consumed, no effect planned. The same
   gate guards the resume path.
3. **Exceptions are safe.** The model-facing tool function never
   raises: dispatcher decisions, infrastructure failures (e.g. an
   approval-store outage) and bridge-level misuse all become one
   structured :class:`ToolResult` JSON document. Exception *messages*
   never surface (they may quote secret material) — infrastructure
   failures report the exception *type name* only, mirroring the
   dispatcher's honesty rule.
4. **Deferred approvals stay durable.** When the boundary reports
   ``approval_required_but_absent``, the tool result carries
   ``status="approval_required"`` with the canonical payload hash and
   policy revision — everything the run layer (``06-agent-run``, jobs
   facade C05) needs to persist ``waiting_approval`` through the
   durable engine. Resumption calls :meth:`CapabilityToolBridge.dispatch_once`
   with the persisted approval id — the same dispatcher seam, never a
   private executor path.

What this module is not
-----------------------

- Not a policy engine: every decision is the dispatcher's.
- Not an execution transport: the executor is whatever the host wired
  into the dispatcher (C03 rule 7).
- Not a deferral mechanism: PydanticAI deferred tools / approval
  requests are deliberately not used — they are not an authorization
  boundary (C06 rule 4); the dispatcher's approval seam is.

Known framework edge (recorded, fail-closed): a provider that emits a
tool-call argument document which is valid JSON but *not an object*
crashes the PydanticAI run inside argument unpacking, before any
bridge code. That is the framework's behavior for every
``Tool.from_schema`` tool (its own MCP integration shares it); the
crash is fail-closed — no dispatch, no effect — and run-level
exception handling belongs to the ``06-agent-run`` consumer, not this
layer. Invalid-JSON argument strings are converted to a retry prompt
by PydanticAI before the bridge is reached.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic_ai.tools import Tool

from mai.capabilities.dispatch import (
    EFFECT_EXECUTED,
    EFFECT_UNKNOWN,
    CapabilityDispatcher,
    DispatchError,
    DispatchOutcome,
    canonical_payload_hash,
)
from mai.plugins.registry import CapabilityDescriptor, CapabilityRegistry
from mai.policy.evaluate import ExecutionContext

__all__ = [
    "BridgeError",
    "BridgeErrorCode",
    "CapabilityToolBridge",
    "MAX_TOOL_NAME_CHARS",
    "TOOL_RESULT_SCHEMA_VERSION",
    "ToolResult",
    "ToolStatus",
    "tool_name_for",
]

#: Version of the frozen :class:`ToolResult` JSON document shape.
TOOL_RESULT_SCHEMA_VERSION = 1

#: OpenAI-compatible function-name bound: ``^[A-Za-z0-9_-]{1,64}$``. The
#: descriptor slug charset (``[a-z0-9._-]``) is not wire-safe (dots), so
#: every mapped name is sanitized and length-capped; collisions are a
#: construction rejection, never a silent alias.
MAX_TOOL_NAME_CHARS = 64

_TOOL_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

#: Recursion bound for schema validation; the registry caps a schema
#: document at 16 KiB, this bounds pathological *nesting* shape only.
_MAX_SCHEMA_DEPTH = 24

#: JSON Schema keywords this bridge asserts. Anything else that can
#: affect validity (``allOf``, ``oneOf``, ``$ref``, ``if``, …) is
#: fail-closed: the call is denied, never executed, until the schema
#: producer reconciles to the supported subset.
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

#: Annotation-only keywords: carried for documentation, assert nothing.
_ANNOTATION_KEYWORDS = frozenset(
    {"title", "description", "default", "examples", "format", "$schema", "$id"}
)

_JSON_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")

#: Payload-hash marker for a payload that is not a canonicalizable JSON
#: object (the dispatcher would reject it the same way; the bridge
#: reports it before any state is read).
_NON_CANONICAL = "non_canonical"


class BridgeErrorCode(StrEnum):
    """Stable machine-readable bridge rejection codes (C01 style)."""

    INVALID_WIRING = "invalid_wiring"
    TOOL_NAME_COLLISION = "tool_name_collision"
    CAPABILITY_NOT_ENABLED = "capability_not_enabled"
    PAYLOAD_SCHEMA_MISMATCH = "payload_schema_mismatch"


class BridgeError(ValueError):
    """One structured, value-free bridge construction/use rejection.

    Raised only for host misuse (bad wiring, resume of a capability the
    context does not enable, dispatch of a schema-mismatched payload).
    Model-facing answers are never this exception — they are
    :class:`ToolResult` documents.
    """

    def __init__(self, code: BridgeErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class ToolStatus(StrEnum):
    """Frozen ``ToolResult`` status vocabulary — the honest lifecycle.

    ``unknown_effect`` means the invocation started and the outcome is
    unproven (host rule 3 honesty); the model must neither assume
    success nor retry blindly.
    """

    EXECUTED = "executed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"
    UNKNOWN_EFFECT = "unknown_effect"


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The model-facing audit document of one bridged call (AC2).

    A frozen-shape, value-free projection of the dispatcher's
    :class:`~mai.capabilities.dispatch.DispatchOutcome`: the payload is
    present as its canonical SHA-256 hash only, executor output only
    after the dispatcher's redact-then-bound pass, and error detail is
    either a stable code or an exception type name. Enough durable
    material (capability, target, run, payload hash, policy revision)
    for the jobs facade to persist a ``waiting_approval`` deferral and
    for the resume path to bind the human decision (C03 rule 4).
    """

    schema_version: int
    capability_id: str
    server_id: uuid.UUID
    run_id: uuid.UUID
    status: ToolStatus
    allowed: bool
    reason: str
    effect: str
    payload_hash: str
    policy_revision: int | None
    effect_class: str | None
    approval_id: uuid.UUID | None
    approval_spent: bool
    approval_code: str | None
    executor_ok: bool | None
    output_text: str | None
    output_truncated: bool | None
    output_omitted_bytes: int | None
    error_code: str | None
    error_detail: str | None

    @classmethod
    def from_outcome(cls, outcome: DispatchOutcome) -> ToolResult:
        """Project one dispatcher receipt onto the tool vocabulary."""
        if outcome.effect == EFFECT_EXECUTED:
            status = ToolStatus.EXECUTED
        elif outcome.effect == EFFECT_UNKNOWN:
            status = ToolStatus.UNKNOWN_EFFECT
        elif (
            not outcome.allowed
            and outcome.effect == "not_attempted"
            and outcome.reason == "approval_required_but_absent"
        ):
            status = ToolStatus.APPROVAL_REQUIRED
        else:
            status = ToolStatus.DENIED
        return cls(
            schema_version=TOOL_RESULT_SCHEMA_VERSION,
            capability_id=outcome.capability_id,
            server_id=outcome.server_id,
            run_id=outcome.run_id,
            status=status,
            allowed=outcome.allowed,
            reason=outcome.reason,
            effect=outcome.effect,
            payload_hash=outcome.payload_hash,
            policy_revision=outcome.policy_revision,
            effect_class=outcome.effect_class,
            approval_id=outcome.approval_id,
            approval_spent=outcome.approval_spent,
            approval_code=outcome.approval_code,
            executor_ok=outcome.executor_ok,
            output_text=None if outcome.output is None else outcome.output.text,
            output_truncated=None
            if outcome.output is None
            else outcome.output.truncated,
            output_omitted_bytes=None
            if outcome.output is None
            else outcome.output.omitted_bytes,
            error_code=None,
            error_detail=None,
        )

    @classmethod
    def rejected(
        cls,
        *,
        context: ExecutionContext,
        capability_id: str,
        payload_hash: str,
        error_code: str,
        error_detail: str,
    ) -> ToolResult:
        """A pre-dispatch rejection: nothing was read, consumed or run."""
        return cls(
            schema_version=TOOL_RESULT_SCHEMA_VERSION,
            capability_id=capability_id,
            server_id=context.server_id,
            run_id=context.run_id,
            status=ToolStatus.DENIED,
            allowed=False,
            reason=error_code,
            effect="not_attempted",
            payload_hash=payload_hash,
            policy_revision=None,
            effect_class=None,
            approval_id=None,
            approval_spent=False,
            approval_code=None,
            executor_ok=None,
            output_text=None,
            output_truncated=None,
            output_omitted_bytes=None,
            error_code=error_code,
            error_detail=error_detail,
        )

    def to_json_str(self) -> str:
        """Frozen-shape JSON text (stable key order; model-safe)."""
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "capability_id": self.capability_id,
                "server_id": str(self.server_id),
                "run_id": str(self.run_id),
                "status": self.status.value,
                "allowed": self.allowed,
                "reason": self.reason,
                "effect": self.effect,
                "payload_hash": self.payload_hash,
                "policy_revision": self.policy_revision,
                "effect_class": self.effect_class,
                "approval_id": None
                if self.approval_id is None
                else str(self.approval_id),
                "approval_spent": self.approval_spent,
                "approval_code": self.approval_code,
                "executor_ok": self.executor_ok,
                "output": None
                if self.output_text is None
                else {
                    "text": self.output_text,
                    "truncated": self.output_truncated,
                    "omitted_bytes": self.output_omitted_bytes,
                },
                "error": None
                if self.error_code is None
                else {"code": self.error_code, "detail": self.error_detail},
            },
            ensure_ascii=True,
            allow_nan=False,
        )

    def __repr__(self) -> str:
        return (
            f"<ToolResult {self.capability_id} {self.status.value} "
            f"reason={self.reason}>"
        )


def tool_name_for(capability_id: str) -> str:
    """The deterministic provider-safe tool name for one capability.

    Sanitized to ``[A-Za-z0-9_-]`` (descriptor dots become underscores)
    and capped at 64 characters. Name collisions between enabled
    capabilities are a construction rejection (fail-closed), not a
    silent alias.
    """
    return _TOOL_NAME_UNSAFE.sub("_", capability_id)[:MAX_TOOL_NAME_CHARS]


@dataclass(frozen=True, slots=True)
class _Binding:
    """One enabled capability's descriptor and its tool surface."""

    capability_id: str
    descriptor: CapabilityDescriptor
    tool: Tool


class CapabilityToolBridge:
    """Registered capabilities as PydanticAI tools over one dispatcher.

    Composition is constructor-checked (fail-closed wiring, the sibling
    pattern). The tool surface is built once from the context's enabled
    capabilities; a context is immutable, so the surface cannot drift.
    Every execution — model-facing tool call or host resume with a
    persisted approval — funnels through :meth:`dispatch_once`, the one
    seam into the dispatcher; there is no second path to the executor.
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        dispatcher: CapabilityDispatcher,
        context: ExecutionContext,
    ) -> None:
        if not isinstance(registry, CapabilityRegistry):
            raise BridgeError(
                BridgeErrorCode.INVALID_WIRING,
                "registry must be a CapabilityRegistry",
            )
        if not isinstance(dispatcher, CapabilityDispatcher):
            raise BridgeError(
                BridgeErrorCode.INVALID_WIRING,
                "dispatcher must be a CapabilityDispatcher",
            )
        if not isinstance(context, ExecutionContext):
            raise BridgeError(
                BridgeErrorCode.INVALID_WIRING,
                "context must be an ExecutionContext",
            )
        self._registry = registry
        self._dispatcher = dispatcher
        self._context = context
        bindings: dict[str, _Binding] = {}
        names: dict[str, str] = {}
        for capability_id in context.allowed_capabilities:
            versions = registry.versions(capability_id)
            if not versions:
                # Granted but not installed: nothing to expose. The
                # dispatcher would deny ``capability_not_registered``
                # anyway; exposure follows the registry, authorization
                # stays with the boundary.
                continue
            descriptor = registry.get(capability_id, versions[-1])
            assert descriptor is not None  # versions() just listed it
            name = tool_name_for(capability_id)
            collision = names.get(name)
            if collision is not None:
                raise BridgeError(
                    BridgeErrorCode.TOOL_NAME_COLLISION,
                    f"{collision!r} and {capability_id!r} both map to "
                    f"tool name {name!r}; rename one capability",
                )
            names[name] = capability_id
            bindings[capability_id] = _Binding(
                capability_id=capability_id,
                descriptor=descriptor,
                tool=self._build_tool(capability_id, descriptor, name),
            )
        self._bindings = bindings

    # -- surface -------------------------------------------------------------

    @property
    def tools(self) -> tuple[Tool, ...]:
        """The enabled tool surface (enabled descriptors only, AC1)."""
        return tuple(binding.tool for binding in self._bindings.values())

    @property
    def enabled_capabilities(self) -> tuple[str, ...]:
        """The capability ids behind :attr:`tools`, in context order."""
        return tuple(self._bindings)

    def descriptor_for(self, capability_id: str) -> CapabilityDescriptor | None:
        """The newest registered descriptor behind one enabled tool."""
        binding = self._bindings.get(capability_id)
        return None if binding is None else binding.descriptor

    def _build_tool(
        self, capability_id: str, descriptor: CapabilityDescriptor, name: str
    ) -> Tool:
        """One schema-declared tool bound to this bridge and capability.

        The descriptor's ``input_schema`` is published verbatim as the
        function-argument schema; PydanticAI's schema-declared tools
        skip argument validation, which is exactly why the function
        re-validates against the same schema before dispatching.
        """
        bridge = self
        advisory = (
            "Every execution requires an approval decision through the "
            "durable approval path before the effect runs."
            if descriptor.effect_class == "privileged"
            else "Execution passes the common policy boundary; grants "
            "and policy are rechecked immediately before the effect."
        )
        description = (
            f"Capability {capability_id} v{descriptor.version} "
            f"({descriptor.effect_class} effect, targets: "
            f"{', '.join(descriptor.execution_environments)}). {advisory} "
            "Arguments must match the declared input schema."
        )

        async def invoke_bridge_tool(**payload: Any) -> str:
            return await bridge._invoke_model_facing(capability_id, payload)

        return Tool.from_schema(
            invoke_bridge_tool,
            name=name,
            description=description,
            json_schema=json.loads(json.dumps(descriptor.input_schema)),
        )

    # -- the one seam --------------------------------------------------------

    async def _invoke_model_facing(
        self, capability_id: str, payload: dict[str, Any]
    ) -> str:
        """Tool entrypoint: never raises, always a :class:`ToolResult`."""
        binding = self._bindings[capability_id]
        payload_hash = _try_payload_hash(payload)
        if payload_hash == _NON_CANONICAL:
            return ToolResult.rejected(
                context=self._context,
                capability_id=capability_id,
                payload_hash=payload_hash,
                error_code="invalid_request",
                error_detail="payload must be a JSON object (mapping)",
            ).to_json_str()
        try:
            violation = _validate_payload(
                payload, binding.descriptor.input_schema, path="$"
            )
            if violation is not None:
                return ToolResult.rejected(
                    context=self._context,
                    capability_id=capability_id,
                    payload_hash=payload_hash,
                    error_code=BridgeErrorCode.PAYLOAD_SCHEMA_MISMATCH.value,
                    error_detail=violation,
                ).to_json_str()
            outcome = await self._dispatcher.dispatch(
                context=self._context,
                capability_id=capability_id,
                payload=payload,
                approval_id=None,
            )
            return ToolResult.from_outcome(outcome).to_json_str()
        except BridgeError as error:
            return ToolResult.rejected(
                context=self._context,
                capability_id=capability_id,
                payload_hash=payload_hash,
                error_code=error.code.value,
                error_detail=error.detail,
            ).to_json_str()
        except DispatchError as error:
            return ToolResult.rejected(
                context=self._context,
                capability_id=capability_id,
                payload_hash=payload_hash,
                error_code=error.code.value,
                error_detail=error.detail,
            ).to_json_str()
        except Exception as error:  # infrastructure: type name only
            return ToolResult.rejected(
                context=self._context,
                capability_id=capability_id,
                payload_hash=payload_hash,
                error_code="dispatch_failed",
                error_detail=type(error).__name__,
            ).to_json_str()

    async def dispatch_once(
        self,
        capability_id: str,
        payload: Mapping[str, Any],
        *,
        approval_id: uuid.UUID | None = None,
    ) -> DispatchOutcome:
        """Apply one capability once through the dispatcher (the seam).

        Used by the model-facing tools (no approval) and by the run
        layer resuming a persisted ``waiting_approval`` deferral (the
        approval id created for the recorded payload hash, C03 rule 4).
        Bridge-level pre-checks are misuse guards only; every
        *decision* — grants, policy, adoption, approval binding,
        immediate recheck — is the dispatcher's.
        """
        binding = self._bindings.get(capability_id)
        if binding is None:
            raise BridgeError(
                BridgeErrorCode.CAPABILITY_NOT_ENABLED,
                f"{capability_id!r} is not enabled for this context; "
                "there is no tool and no resume path for it",
            )
        violation = _validate_payload(
            payload, binding.descriptor.input_schema, path="$"
        )
        if violation is not None:
            raise BridgeError(BridgeErrorCode.PAYLOAD_SCHEMA_MISMATCH, violation)
        return await self._dispatcher.dispatch(
            context=self._context,
            capability_id=capability_id,
            payload=payload,
            approval_id=approval_id,
        )

    def __repr__(self) -> str:
        return (
            f"<CapabilityToolBridge tools={len(self._bindings)} "
            f"run={self._context.run_id}>"
        )


# ---------------------------------------------------------------------------
# Pre-dispatch payload gate (AC2: schema mismatch never executes)
# ---------------------------------------------------------------------------


def _try_payload_hash(payload: object) -> str:
    """Canonical payload hash, or the rejection marker if not canonical."""
    try:
        return canonical_payload_hash(payload)
    except DispatchError:
        return _NON_CANONICAL


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


def _validate_payload(
    value: object, schema: object, *, path: str, depth: int = 0
) -> str | None:
    """Validate one payload against the descriptor's input schema.

    Returns ``None`` when the payload satisfies the schema, else a
    value-free violation path/description (payload values are never
    quoted — they may carry credential-shaped material). An unsupported
    applicative keyword is itself a violation: fail-closed, the call
    can never execute until the schema producer reconciles.
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
        names = (
            expected_type if isinstance(expected_type, list) else [expected_type]
        )
        if not names or any(name not in _JSON_TYPES for name in names):
            return f"{path}: schema declares an unsupported type"
        if not any(_check_type(value, name) for name in names):
            return (
                f"{path}: payload does not match declared type "
                f"{[str(name) for name in names]}"
            )

    if "const" in schema and not _json_equal(value, schema["const"]):
        return f"{path}: payload does not match the const value"
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list):
            return f"{path}: schema enum must be an array"
        if not any(_json_equal(value, choice) for choice in choices):
            return f"{path}: payload is not one of the enum values"

    if isinstance(value, Mapping):
        return _validate_object(value, schema, path=path, depth=depth)
    if isinstance(value, list):
        return _validate_array(value, schema, path=path, depth=depth)
    if isinstance(value, str):
        return _validate_string(value, schema, path=path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _validate_number(value, schema, path=path)
    return None


def _validate_object(
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
            violation = _validate_payload(
                item, properties[key], path=f"{path}.{key}", depth=depth + 1
            )
            if violation is not None:
                return violation
        elif additional is False:
            return f"{path}.{key}: property is not allowed"
        elif isinstance(additional, Mapping):
            violation = _validate_payload(
                item, additional, path=f"{path}.{key}", depth=depth + 1
            )
            if violation is not None:
                return violation
    return None


def _validate_array(
    value: list[object],
    schema: Mapping[object, object],
    *,
    path: str,
    depth: int,
) -> str | None:
    min_items = schema.get("minItems")
    max_items = schema.get("maxItems")
    for name, bound, comparison in (
        ("minItems", min_items, len(value) < min_items if min_items is not None else False),
        ("maxItems", max_items, len(value) > max_items if max_items is not None else False),
    ):
        if bound is not None and (
            not isinstance(bound, int) or isinstance(bound, bool)
        ):
            return f"{path}: schema {name} must be an integer"
        if comparison:
            return f"{path}: array violates the {name} bound"
    items = schema.get("items")
    if items is not None:
        for index, element in enumerate(value):
            violation = _validate_payload(
                element, items, path=f"{path}[{index}]", depth=depth + 1
            )
            if violation is not None:
                return violation
    return None


def _validate_string(
    value: str, schema: Mapping[object, object], *, path: str
) -> str | None:
    min_length = schema.get("minLength")
    max_length = schema.get("maxLength")
    for name, bound, comparison in (
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
        if comparison:
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


def _validate_number(
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
