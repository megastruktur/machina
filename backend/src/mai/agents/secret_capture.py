"""Pure pre-persistence sensitive-call sanitizer (ask_05, D6/C6).

Classifies secret-namespace tool calls on a PydanticAI ``ModelResponse``
BEFORE anything is persisted: every malformed or forbidden secret call is
rejected pre-execution, every extracted exact-value form of the rejected
raw secret material is scrubbed from the response's text, reasoning and —
for sibling calls — arguments, and each rejected call gets a terminal
pre-execution rejected ``ToolResultEntry`` draft so slices 06/07 can
persist a safe, honest protocol record without ever dispatching (and
without ever dispatching a redaction marker as a real payload).

Pure and bounded: no filesystem, network, clock or database I/O; no
mutable singletons; deterministic given the same message. Sensitive
values and value forms are bounded; oversized hostile inputs degrade to
bounded redacted text, never a crash.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart

from mai.agents.output_safety import MAX_JSON_DEPTH, REDACTED, redact_text
from mai.agents.tools import SECRET_RAW_VALUE_FIELDS
from mai.contracts.sessions import (
    EntryKind,
    MAX_ERROR_DETAIL_CHARS,
    MAX_IDENTITY_CHARS,
    MAX_ENTRY_TEXT_CHARS,
    ToolResultEntry,
)
from mai.contracts.tool_results import (
    DomainOutcome,
    EffectCertainty,
    ExecutionStatus,
)

__all__ = [
    "MAX_CAPTURE_ARGS_DEPTH",
    "MAX_SENSITIVE_VALUES",
    "SECRET_RAW_VALUE_FIELDS",
    "SENSITIVE_CALL_REJECTED_CODE",
    "SanitizedModelResponse",
    "SanitizedToolCall",
    "sanitize_model_response",
    "sanitize_tool_call",
    "scrub_sensitive_text",
]

#: Error code for every pre-execution sensitive-call rejection.
SENSITIVE_CALL_REJECTED_CODE: Final[str] = "sensitive_call_rejected"

#: Bounded args traversal depth (matches the seam's JSON depth budget).
MAX_CAPTURE_ARGS_DEPTH: Final[int] = MAX_JSON_DEPTH

#: Bounded count of extracted sensitive values per response.
MAX_SENSITIVE_VALUES: Final[int] = 64

#: Per-value bound before form extraction (a hostile 20k "secret" cannot
#: explode the scrub table; longer strings are redacted as text anyway).
_MAX_SENSITIVE_VALUE_CHARS: Final[int] = MAX_ENTRY_TEXT_CHARS

#: Bounded count of encoded value forms per extracted value.
_MAX_VALUE_FORMS: Final[int] = 8

_SECRET_PREFIXES: Final[tuple[str, ...]] = ("secret.", "secret_")

#: Known secret operations in canonical (dotted, lowered) form.
_KNOWN_SECRET_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"secret.list", "secret.get", "secret.store"}
)

#: Non-value parameters allowed per canonical operation (D6/C6: the
#: generation/reference-only agent contract; ``secret.list`` additionally
#: admits the bridge descriptor's ``query`` filter and the brief's ``tag``).
_ALLOWED_PARAMS: Final[dict[str, frozenset[str]]] = {
    "secret.store": frozenset(
        {
            "vault",
            "title",
            "field",
            "generate",
            "length",
            "category",
            "username",
            "url",
            "notes",
            "export_credential_ref",
        }
    ),
    "secret.get": frozenset({"vault", "item", "field", "export_credential_ref"}),
    "secret.list": frozenset(
        {"vault", "category", "query", "tag", "limit", "offset"}
    ),
}

#: Safe-string metadata fields scanned for secret-SHAPED content (3e).
_METADATA_FIELDS: Final[frozenset[str]] = frozenset(
    {"notes", "url", "item", "title", "field"}
)

_REJECTION_DETAILS: Final[dict[str, str]] = {
    "raw_secret_value_forbidden": (
        "secret tool call carries a raw secret-value argument; only "
        "generation/reference-safe calls are persistable"
    ),
    "malformed_secret_arguments": (
        "secret tool call arguments are not a parseable JSON object; the "
        "call cannot be proven generation/reference-safe"
    ),
    "secret_store_requires_generate_true": (
        "secret.store requires generate=true and forbids raw secret "
        "value arguments and unsupported parameters"
    ),
    "unknown_secret_operation": (
        "unknown secret-namespace operation; only secret.list, "
        "secret.get and secret.store are persistable"
    ),
    "unsupported_secret_parameter": (
        "secret.get and secret.list forbid unsupported parameters; only "
        "reference lookup and listing arguments are permitted"
    ),
    "secret_material_in_arguments": (
        "secret tool call carries secret-shaped content in a metadata "
        "field; the value is rejected pre-persistence"
    ),
}

_STORE_REASON: Final[str] = "secret_store_requires_generate_true"

#: Known operations with a foreign parameter: the operation is known;
#: only its parameters are outside the generation/reference contract.
_UNSUPPORTED_PARAM_REASON: Final[str] = "unsupported_secret_parameter"

#: Well-known NON-secret metadata keys: on an unknown secret operation
#: their values are ordinary words ("default", "login"), never extracted
#: as literal secrets unless they themselves match the pattern layer.
_NON_SENSITIVE_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {"vault", "category", "tag", "limit", "offset", "generate", "length"}
)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def _normalized_tool_name(tool_name: str | None) -> str:
    """Lowered tool/capability name (``None`` degrades to empty)."""
    return (tool_name or "").strip().lower()


def _canonical_operation(name: str) -> str:
    """Canonical dotted operation id for a secret-namespace name."""
    if name.startswith("secret_"):
        return "secret." + name[len("secret_") :]
    return name


def is_secret_namespace(tool_name: str | None) -> bool:
    """Whether one tool/capability name lives in the secret namespace."""
    normalized = _normalized_tool_name(tool_name)
    return any(normalized.startswith(prefix) for prefix in _SECRET_PREFIXES)


def _normalized_key(key: str) -> str:
    return key.strip().lower().replace("-", "_").replace(" ", "_")


#: Well-known category/vault words that are NEVER literal secrets on an
#: unknown secret operation.
_ORDINARY_METADATA_WORDS: Final[frozenset[str]] = frozenset(
    {
        "default",
        "login",
        "password",
        "api_credential",
        "secure_note",
        "ssh_key",
        "database",
        "server",
        "custom",
        "true",
        "false",
    }
)

_PLAIN_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_-]*")


def _is_ordinary_metadata_word(value: str) -> bool:
    """True ONLY for a short, plain, lowercase identifier word.

    Mixed uppercase/digit/special shapes and high-entropy strings are
    never "ordinary", and anything the trusted pattern layer flags is
    excluded even if it is lowercase — so ``"default"`` stays plain but
    ``"Xk9fQ2mZpL8vR4tW"`` is treated as candidate secret material.
    """
    if not 1 <= len(value) <= 24:
        return False
    if _PLAIN_WORD_RE.fullmatch(value) is None:
        return False
    if (
        value not in _ORDINARY_METADATA_WORDS
        and re.fullmatch(r"[a-z]{3,16}", value) is None
    ):
        return False
    return redact_text(value) == value


def _raw_value_keys(args: Mapping[str, Any]) -> tuple[str, ...]:
    """Raw-secret argument keys at the top level or nested (bounded)."""
    found: list[str] = []
    stack: list[tuple[Any, int]] = [(args, 0)]
    seen: set[int] = set()
    while stack:
        node, depth = stack.pop()
        if depth > MAX_CAPTURE_ARGS_DEPTH or not isinstance(node, Mapping):
            continue
        if id(node) in seen:
            continue
        seen.add(id(node))
        for key, value in node.items():
            if not isinstance(key, str):
                continue
            if _normalized_key(key) in SECRET_RAW_VALUE_FIELDS:
                found.append(key)
            if isinstance(value, Mapping):
                stack.append((value, depth + 1))
            elif isinstance(value, list):
                stack.extend((item, depth + 1) for item in value)
    return tuple(dict.fromkeys(found))[:MAX_SENSITIVE_VALUES]


def _iter_string_values(node: Any, *, depth: int = 0) -> Sequence[str]:
    """Bounded in-order string leaves of one args tree."""
    if depth > MAX_CAPTURE_ARGS_DEPTH:
        return ()
    if isinstance(node, str):
        return (node,)
    if isinstance(node, Mapping):
        values: list[str] = []
        for value in node.values():
            values.extend(_iter_string_values(value, depth=depth + 1))
        return values[:MAX_SENSITIVE_VALUES]
    if isinstance(node, list):
        values = []
        for item in node:
            values.extend(_iter_string_values(item, depth=depth + 1))
        return values[:MAX_SENSITIVE_VALUES]
    return ()


def _collect_raw_values(args: Mapping[str, Any]) -> tuple[str, ...]:
    """String values stored under raw-secret keys (bounded, in order)."""
    values: list[str] = []
    stack: list[tuple[Any, int]] = [(args, 0)]
    seen: set[int] = set()
    while stack:
        node, depth = stack.pop()
        if depth > MAX_CAPTURE_ARGS_DEPTH or not isinstance(node, Mapping):
            continue
        if id(node) in seen:
            continue
        seen.add(id(node))
        for key, value in node.items():
            if not isinstance(key, str):
                continue
            if _normalized_key(key) in SECRET_RAW_VALUE_FIELDS:
                if isinstance(value, str):
                    values.append(value[:_MAX_SENSITIVE_VALUE_CHARS])
                elif isinstance(value, (Mapping, list)):
                    for leaf in _iter_string_values(value):
                        values.append(leaf[:_MAX_SENSITIVE_VALUE_CHARS])
            elif isinstance(value, Mapping):
                stack.append((value, depth + 1))
            elif isinstance(value, list):
                stack.extend((item, depth + 1) for item in value)
    return tuple(values[:MAX_SENSITIVE_VALUES])


def _value_forms(value: str) -> tuple[str, ...]:
    """Exact-value forms of one sensitive string (bounded).

    Raw, JSON-escaped, URL-encoded and base64 forms; a form shorter than
    4 characters is never tracked (redaction of tiny strings corrupts
    text without protecting anything).
    """
    if len(value) < 4:
        return ()
    bounded = value[:_MAX_SENSITIVE_VALUE_CHARS]
    forms = {bounded}
    try:
        escaped = json.dumps(bounded, ensure_ascii=False)[1:-1]
        if escaped != bounded:
            forms.add(escaped[:_MAX_SENSITIVE_VALUE_CHARS])
        ascii_escaped = json.dumps(bounded, ensure_ascii=True)[1:-1]
        if ascii_escaped != bounded and ascii_escaped != escaped:
            forms.add(ascii_escaped[:_MAX_SENSITIVE_VALUE_CHARS])
        quoted = urllib.parse.quote(bounded, safe="")
        if quoted != bounded:
            forms.add(quoted)
        encoded = base64.b64encode(bounded.encode("utf-8")).decode("ascii")
        forms.add(encoded)
        urlsafe = base64.urlsafe_b64encode(bounded.encode("utf-8")).decode(
            "ascii"
        )
        if urlsafe != encoded:
            forms.add(urlsafe)
    except (TypeError, ValueError):  # pragma: no cover - str inputs only
        pass
    return tuple(sorted(forms, key=len, reverse=True)[:_MAX_VALUE_FORMS])


def scrub_sensitive_text(text: str, sensitive_values: Sequence[str]) -> str:
    """Replace every exact sensitive-value form, then run the pattern layer.

    Deterministic and bounded: forms longer than the per-value bound are
    impossible by construction; the pattern layer adds secret-SHAPED
    token redaction on top. A cut can never split a secret because the
    caller bounds only AFTER this function.
    """
    forms: list[str] = []
    for value in sensitive_values[:MAX_SENSITIVE_VALUES]:
        forms.extend(_value_forms(value[:_MAX_SENSITIVE_VALUE_CHARS]))
    forms = sorted(set(forms), key=len, reverse=True)
    for form in forms:
        text = text.replace(form, REDACTED)
    return redact_text(text)


def _redact_args_strings(
    args: Mapping[str, Any], sensitive_values: Sequence[str]
) -> dict[str, Any]:
    """Exact-value scrub of one SAFE call's args tree (bounded depth).

    Used for sibling/allowed calls whose serialized arguments leaked an
    extracted value from a rejected secret call in the same response.
    """
    forms: list[str] = []
    for value in sensitive_values[:MAX_SENSITIVE_VALUES]:
        forms.extend(_value_forms(value[:_MAX_SENSITIVE_VALUE_CHARS]))
    forms = sorted(set(forms), key=len, reverse=True)

    def _walk(node: Any, depth: int) -> Any:
        if depth > MAX_CAPTURE_ARGS_DEPTH:
            return REDACTED
        if isinstance(node, str):
            scrubbed = node
            for form in forms:
                scrubbed = scrubbed.replace(form, REDACTED)
            return redact_text(scrubbed)
        if isinstance(node, Mapping):
            return {
                key: _walk(value, depth + 1) for key, value in node.items()
            }
        if isinstance(node, list):
            return [_walk(item, depth + 1) for item in node]
        return node

    return {key: _walk(value, 1) for key, value in args.items()}


def _parse_args(raw: str | dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    """Normalize provider tool-call args; ``unparseable`` flags bad shapes.

    ``(args, False)`` when the payload is a JSON object; ``( {}, True)``
    when a non-empty string is malformed JSON or parses to a non-object
    (the raw string is the caller's scrubbing responsibility).
    """
    if raw is None:
        return {}, False
    if isinstance(raw, Mapping):
        return dict(raw), False
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError):
            # json.loads raises RecursionError (not ValueError) on
            # deeply nested hostile strings; degrade to unparseable.
            return {}, True
        if isinstance(parsed, Mapping):
            return dict(parsed), False
        return {}, True
    return {}, True


def _rejection_result(
    call_id: str, tool_name: str, capability_id: str, reason: str
) -> ToolResultEntry:
    """The terminal pre-execution rejected result draft (value-free).

    The payload hash binds call identity + rejection code only — the raw
    secret value is never hashed, persisted or dispatched.
    """
    payload_hash = hashlib.sha256(
        f"{call_id}:{tool_name}:{SENSITIVE_CALL_REJECTED_CODE}".encode("utf-8")
    ).hexdigest()
    return ToolResultEntry(
        kind=EntryKind.TOOL_RESULT,
        call_id=call_id[:MAX_IDENTITY_CHARS],
        tool_name=tool_name[:MAX_IDENTITY_CHARS],
        capability_id=capability_id[:MAX_IDENTITY_CHARS],
        status="denied",
        execution_status=ExecutionStatus.FAILED,
        domain_outcome=DomainOutcome.FAILURE,
        effect_certainty=EffectCertainty.CERTAIN,
        payload_hash=payload_hash,
        output_preview=None,
        error_code=SENSITIVE_CALL_REJECTED_CODE,
        error_detail=(
            _REJECTION_DETAILS.get(reason, "sensitive call rejected")[
                :MAX_ERROR_DETAIL_CHARS
            ]
        ),
    )


# ---------------------------------------------------------------------------
# the sanitizer
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SanitizedToolCall:
    """One classified, sanitized tool call (the safe persisted shape)."""

    call_id: str
    tool_name: str
    capability_id: str
    safe_args: dict[str, Any]
    rejected: bool
    rejection_reason: str | None = None
    rejection_result: ToolResultEntry | None = None


@dataclass(frozen=True, slots=True)
class SanitizedModelResponse:
    """The safe projection of one assistant ``ModelResponse``."""

    text: str
    thinking_parts: tuple[ThinkingPart, ...]
    tool_calls: tuple[SanitizedToolCall, ...]
    rejected_results: tuple[ToolResultEntry, ...]
    had_rejected_calls: bool


def sanitize_tool_call(
    part: ToolCallPart,
) -> tuple[SanitizedToolCall, tuple[str, ...]]:
    """Classify and sanitize one tool-call part (pure; bounded).

    Returns the safe persisted call plus the exact sensitive values
    extracted from a REJECTED call (empty for allowed/passthrough
    calls). Non-secret calls pass through unchanged here; exact-value
    leakage from a rejected sibling is scrubbed by
    :func:`sanitize_model_response`.
    """
    call_id = (part.tool_call_id or "")[:MAX_IDENTITY_CHARS]
    tool_name = (part.tool_name or "")[:MAX_IDENTITY_CHARS]
    capability_id = tool_name
    args, unparseable = _parse_args(part.args)

    if not is_secret_namespace(tool_name):
        if unparseable:
            # Non-secret tools mirror the capture normalization: the raw
            # string is redacted as text, never structured.
            return (
                SanitizedToolCall(
                    call_id=call_id,
                    tool_name=tool_name,
                    capability_id=capability_id,
                    safe_args={"raw": redact_text(str(part.args))[:MAX_ENTRY_TEXT_CHARS]},
                    rejected=False,
                ),
                (),
            )
        return (
            SanitizedToolCall(
                call_id=call_id,
                tool_name=tool_name,
                capability_id=capability_id,
                safe_args=args,
                rejected=False,
            ),
            (),
        )

    operation = _canonical_operation(_normalized_tool_name(tool_name))
    sensitive: list[str] = []

    if unparseable:
        reason = "malformed_secret_arguments"
        if isinstance(part.args, str):
            sensitive.append(part.args[:_MAX_SENSITIVE_VALUE_CHARS])
        return (
            _rejected_call(call_id, tool_name, capability_id, reason, ()),
            tuple(sensitive),
        )

    raw_keys = _raw_value_keys(args)
    if raw_keys:
        reason = "raw_secret_value_forbidden"
        sensitive.extend(_collect_raw_values(args))
        return (
            _rejected_call(
                call_id, tool_name, capability_id, reason, raw_keys
            ),
            tuple(sensitive),
        )

    if operation not in _KNOWN_SECRET_OPERATIONS:
        # Unknown operation: extract raw-value keys, arbitrary/unknown
        # keys, and any metadata leaf that is not an ORDINARY word — a
        # metadata key carrying a container or a high-entropy/plain
        # non-ordinary value (vault=["sk-…"], tag="Xk9fQ2mZpL8vR4tW")
        # still yields its leaves for scrubbing, while "default"/"login"
        # stay plain.
        reason = "unknown_secret_operation"
        for key, value in args.items():
            if not isinstance(key, str):
                continue
            metadata_key = _normalized_key(key) in _NON_SENSITIVE_METADATA_KEYS
            for leaf in _iter_string_values(value):
                if (
                    metadata_key
                    and isinstance(value, str)
                    and _is_ordinary_metadata_word(leaf)
                ):
                    continue
                sensitive.append(leaf[:_MAX_SENSITIVE_VALUE_CHARS])
        return (
            _rejected_call(call_id, tool_name, capability_id, reason, ()),
            tuple(sensitive),
        )

    allowed = _ALLOWED_PARAMS[operation]
    unsupported = tuple(
        sorted(
            key
            for key in args
            if isinstance(key, str) and _normalized_key(key) not in allowed
        )
    )
    if operation == "secret.store":
        generate = args.get("generate")
        if generate is not True or unsupported:
            reason = _STORE_REASON
            _secret_shaped_values(args, sensitive)
            return (
                _rejected_call(
                    call_id,
                    tool_name,
                    capability_id,
                    reason,
                    tuple(unsupported),
                ),
                tuple(sensitive),
            )
    elif unsupported:
        reason = _UNSUPPORTED_PARAM_REASON
        _secret_shaped_values(args, sensitive)
        return (
            _rejected_call(
                call_id, tool_name, capability_id, reason, unsupported
            ),
            tuple(sensitive),
        )

    metadata_reason = _metadata_violation(args, sensitive)
    if metadata_reason is not None:
        return (
            _rejected_call(
                call_id, tool_name, capability_id, metadata_reason, ()
            ),
            tuple(sensitive),
        )

    return (
        SanitizedToolCall(
            call_id=call_id,
            tool_name=tool_name,
            capability_id=capability_id,
            safe_args=args,
            rejected=False,
        ),
        (),
    )


def _secret_shaped_values(
    args: Mapping[str, Any], into: list[str]
) -> None:
    """Append secret-SHAPED metadata values to the sensitive list."""
    for key, value in args.items():
        if (
            isinstance(key, str)
            and _normalized_key(key) in _METADATA_FIELDS
            and isinstance(value, str)
            and redact_text(value) != value
        ):
            into.append(value[:_MAX_SENSITIVE_VALUE_CHARS])


def _metadata_violation(args: Mapping[str, Any], sensitive: list[str]) -> str | None:
    """3e: secret-shaped content inside safe-string metadata fields."""
    before = len(sensitive)
    _secret_shaped_values(args, sensitive)
    if len(sensitive) > before:
        return "secret_material_in_arguments"
    return None


def _rejected_call(
    call_id: str,
    tool_name: str,
    capability_id: str,
    reason: str,
    rejected_keys: tuple[str, ...],
) -> SanitizedToolCall:
    """The safe marker-args shape for one rejected call.

    The persisted args are NEVER dispatchable: unknown marker keys fail
    every schema gate, and no raw value or ``[REDACTED]`` marker rides
    along.
    """
    safe_args: dict[str, Any] = {
        "_sensitive_call_redacted": True,
        "rejection_reason": reason,
        "rejected_keys": sorted({key[:MAX_IDENTITY_CHARS] for key in rejected_keys}),
    }
    return SanitizedToolCall(
        call_id=call_id,
        tool_name=tool_name,
        capability_id=capability_id,
        safe_args=safe_args,
        rejected=True,
        rejection_reason=reason,
        rejection_result=_rejection_result(call_id, tool_name, capability_id, reason),
    )


def sanitize_model_response(message: ModelResponse) -> SanitizedModelResponse:
    """Sanitize one assistant ``ModelResponse`` (pure; deterministic).

    Order of operations: classify every call first (collecting rejected
    calls' exact sensitive values), then scrub the exact-value forms from
    the text, reasoning and ANY sibling call's args (a leaked secret in
    ``host.shell`` args is scrubbed even though the shell call itself is
    allowed), keeping outputs bounded throughout.
    """
    calls: list[SanitizedToolCall] = []
    sensitive: list[str] = []
    for part in message.parts:
        if isinstance(part, ToolCallPart):
            call, values = sanitize_tool_call(part)
            calls.append(call)
            for value in values:
                if len(sensitive) < MAX_SENSITIVE_VALUES:
                    sensitive.append(value)

    if sensitive:
        calls = [
            (
                call
                if call.rejected
                else SanitizedToolCall(
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    capability_id=call.capability_id,
                    safe_args=_redact_args_strings(call.safe_args, sensitive),
                    rejected=False,
                )
            )
            for call in calls
        ]

    text = "".join(
        part.content for part in message.parts if isinstance(part, TextPart)
    )
    safe_text = scrub_sensitive_text(text, sensitive)[:MAX_ENTRY_TEXT_CHARS]

    thinking: list[ThinkingPart] = []
    for part in message.parts:
        if isinstance(part, ThinkingPart):
            content = scrub_sensitive_text(part.content, sensitive)
            content = content[:MAX_ENTRY_TEXT_CHARS]
            signature = part.signature
            if signature:
                signature = scrub_sensitive_text(signature, sensitive)[
                    :MAX_ENTRY_TEXT_CHARS
                ]
            thinking.append(
                ThinkingPart(content=content, signature=signature or None)
            )

    rejected_results = tuple(
        call.rejection_result for call in calls if call.rejection_result is not None
    )
    return SanitizedModelResponse(
        text=safe_text,
        thinking_parts=tuple(thinking),
        tool_calls=tuple(calls),
        rejected_results=rejected_results,
        had_rejected_calls=bool(rejected_results),
    )
