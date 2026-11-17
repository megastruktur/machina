"""Focused tests for the pre-persistence sensitive-call sanitizer (ask_05).

Covers: secret-namespace classification (store/get/list/unknown),
raw-value key rejection (top-level, nested, case variants), unparseable
args, generation-required store contract, unsupported parameters,
secret-shaped metadata fields, exact-value scrubbing across text,
reasoning and sibling args (raw + JSON-escaped + URL-encoded + base64
forms), terminal rejected ToolResultEntry synthesis, non-dispatchable
marker args (checked against the REAL agent-projected secret.store
schema), bounds on hostile oversized input, and determinism.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse

from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
)

from mai.agents.secret_capture import (
    SENSITIVE_CALL_REJECTED_CODE,
    SECRET_RAW_VALUE_FIELDS,
    SanitizedModelResponse,
    sanitize_model_response,
    sanitize_tool_call,
    scrub_sensitive_text,
)
from mai.agents.tools import (
    SECRET_STORE_CAPABILITY_ID,
    _agent_input_schema_for,
    _validate_payload,
)
from mai.contracts.sessions import MAX_ENTRY_TEXT_CHARS
from mai.plugins.builtin_onepassword import SECRET_STORE_CAPABILITY
from mai.plugins.registry import parse_descriptor

_CANARY = "hunter2-synthetic-secret-9f3a"
_SPECIAL = "p@ss wörd&=+?/x"  # exercises the URL-encoding form layer


def _response(*parts: object) -> ModelResponse:
    return ModelResponse(parts=list(parts))  # type: ignore[arg-type]


def _store_call(call_id: str = "c1", args: object = None) -> ToolCallPart:
    return ToolCallPart(tool_call_id=call_id, tool_name="secret_store", args=args)


# ---------------------------------------------------------------------------
# 1. non-secret passthrough
# ---------------------------------------------------------------------------


def test_non_secret_calls_pass_through_unchanged() -> None:
    for name in ("host_shell", "host.shell", "app.adopt", "fixture_read"):
        call, values = sanitize_tool_call(
            ToolCallPart(
                tool_call_id="c1",
                tool_name=name,
                args={"command": "echo status"},
            )
        )
        assert call.rejected is False
        assert call.rejection_reason is None
        assert call.rejection_result is None
        assert call.safe_args == {"command": "echo status"}
        assert values == ()


def test_raw_value_key_on_non_secret_tool_is_not_classified() -> None:
    """The rejection authority is the secret namespace; a raw-value key
    on a non-secret tool passes through structurally — the dispatcher's
    own schema gate remains the boundary there."""
    call, values = sanitize_tool_call(
        ToolCallPart(
            tool_call_id="c1",
            tool_name="fixture_read",
            args={"value": _CANARY},
        )
    )
    assert call.rejected is False
    assert call.safe_args == {"value": _CANARY}
    assert values == ()


# ---------------------------------------------------------------------------
# 2. rejection classes
# ---------------------------------------------------------------------------


def test_raw_value_keys_rejected_top_level_and_nested() -> None:
    for args in (
        {"title": "db", "value": _CANARY},
        {"title": "db", "Password": _CANARY},
        {"title": "db", "API-KEY": _CANARY},
        {"title": "db", "generate": True, "meta": {"token": _CANARY}},
        {"title": "db", "generate": True, "list": [{"private_key": _CANARY}]},
        {"vault": "v", "item": "i", "secret": _CANARY},
        {"vault": "v", "passphrase": _CANARY},
        {"vault": "v", "client_secret": _CANARY},
    ):
        call, values = sanitize_tool_call(_store_call(args=args))
        assert call.rejected is True, args
        assert call.rejection_reason == "raw_secret_value_argument", args
        assert call.rejection_result is not None
        assert values and _CANARY in values, args


def test_unparseable_or_non_object_args_rejected_on_secret_tools() -> None:
    malformed = json.dumps({"title": "db", "value": _CANARY})[:-2]
    for name in ("secret_store", "secret.get", "secret_list"):
        for args in (malformed, "[1, 2]", '"just a string"', 42):
            call, _ = sanitize_tool_call(
                ToolCallPart(tool_call_id="c1", tool_name=name, args=args)
            )
            assert call.rejected is True, (name, args)
            assert call.rejection_reason == "unparseable_secret_tool_args"
        # The malformed-JSON string itself is extracted for scrubbing.
        call, values = sanitize_tool_call(
            ToolCallPart(tool_call_id="c1", tool_name=name, args=malformed)
        )
        assert any("title" in value for value in values)


def test_store_requires_generate_true_and_forbids_unsupported() -> None:
    for args in (
        {"title": "db"},
        {"title": "db", "generate": False},
        {"title": "db", "generate": "yes"},
        {"title": "db", "generate": 1},
        {"title": "db", "generate": True, "source_ref": "op://v/i/p"},
    ):
        call, _ = sanitize_tool_call(_store_call(args=args))
        assert call.rejected is True, args
        assert call.rejection_reason == "secret_store_requires_generate_true"
        assert call.rejection_result is not None


def test_unknown_secret_operations_rejected() -> None:
    for name in ("secret_rotate", "secret.rotate", "secret_delete", "secret_vault"):
        call, values = sanitize_tool_call(
            ToolCallPart(tool_call_id="c1", tool_name=name, args={"item": "i"})
        )
        assert call.rejected is True, name
        assert call.rejection_reason == "unknown_secret_operation"
        assert values == ("i",), name


def test_unsupported_parameters_rejected_on_get() -> None:
    call, _ = sanitize_tool_call(
        ToolCallPart(
            tool_call_id="c1",
            tool_name="secret_get",
            args={"item": "i", "raw_value": "not-a-field"},
        )
    )
    assert call.rejected is True
    assert call.rejection_reason == "unknown_secret_operation"


def test_secret_shaped_metadata_fields_rejected() -> None:
    cases = (
        {"title": "db", "generate": True, "notes": "password: hunter2xyz"},
        {
            "title": "db",
            "generate": True,
            "url": "https://user:hunter2x@example.com",
        },
        {
            "vault": "v",
            "item": "login with token: eyJhbGciOiJIUzI1NiIsJ."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4",
        },
    )
    for args in cases:
        name = "secret_store" if "generate" in args else "secret_get"
        call, values = sanitize_tool_call(
            ToolCallPart(tool_call_id="c1", tool_name=name, args=args)
        )
        assert call.rejected is True, args
        assert call.rejection_reason == "secret_shaped_metadata_field"
        assert values, args


# ---------------------------------------------------------------------------
# 3. valid secret calls are allowed unchanged
# ---------------------------------------------------------------------------


def test_valid_store_generate_call_allowed_unchanged() -> None:
    args = {
        "vault": "fixture-vault",
        "title": "app-db",
        "field": "password",
        "generate": True,
        "length": 24,
        "category": "password",
        "username": "deploy",
        "url": "https://db.example.internal",
        "notes": "created by the agent run",
        "export_credential_ref": "APP_DB_PASSWORD",
    }
    call, values = sanitize_tool_call(_store_call(args=args))
    assert call.rejected is False
    assert call.rejection_result is None
    assert call.safe_args == args
    assert values == ()


def test_valid_get_and_list_calls_allowed() -> None:
    get_args = {
        "vault": "fixture-vault",
        "item": "app-db",
        "field": "password",
        "export_credential_ref": "APP_DB_PASSWORD",
    }
    call, values = sanitize_tool_call(
        ToolCallPart(tool_call_id="c1", tool_name="secret_get", args=get_args)
    )
    assert call.rejected is False
    assert call.safe_args == get_args
    assert values == ()
    list_args = {"vault": "fixture-vault", "query": "app", "limit": 10, "offset": 0}
    call, _ = sanitize_tool_call(
        ToolCallPart(tool_call_id="c2", tool_name="secret_list", args=list_args)
    )
    assert call.rejected is False
    assert call.safe_args == list_args


# ---------------------------------------------------------------------------
# 4. exact-value scrubbing across text, reasoning and sibling args
# ---------------------------------------------------------------------------


def test_rejected_value_scrubbed_from_text_thinking_and_sibling_args() -> None:
    response = _response(
        ThinkingPart(content=f"plan: use {_CANARY} carefully"),
        TextPart(f"storing {_CANARY} now"),
        ToolCallPart(
            tool_call_id="c1",
            tool_name="secret_store",
            args={"title": "db", "value": _CANARY},
        ),
        ToolCallPart(
            tool_call_id="c2",
            tool_name="host_shell",
            args={"command": f"echo {_CANARY}"},
        ),
        ToolCallPart(
            tool_call_id="c3",
            tool_name="secret_get",
            args={"item": "app-db"},
        ),
    )
    sanitized = sanitize_model_response(response)
    assert sanitized.had_rejected_calls is True
    assert _CANARY not in sanitized.text
    assert "[REDACTED]" in sanitized.text
    assert sanitized.thinking_parts[0].content == "plan: use [REDACTED] carefully"
    by_id = {call.call_id: call for call in sanitized.tool_calls}
    assert by_id["c1"].rejected is True
    assert by_id["c1"].safe_args["_sensitive_call_redacted"] is True
    assert by_id["c1"].safe_args["rejected_keys"] == ["value"]
    assert _CANARY not in json.dumps(by_id["c1"].safe_args)
    # Sibling allowed call: scrubbed, never rejected.
    assert by_id["c2"].rejected is False
    assert by_id["c2"].safe_args == {"command": "echo [REDACTED]"}
    assert by_id["c3"].rejected is False
    assert by_id["c3"].safe_args == {"item": "app-db"}


def test_encoded_value_forms_scrubbed() -> None:
    escaped = json.dumps(_SPECIAL, ensure_ascii=False)[1:-1]
    quoted = urllib.parse.quote(_SPECIAL, safe="")
    b64 = base64.b64encode(_SPECIAL.encode()).decode()
    b64url = base64.urlsafe_b64encode(_SPECIAL.encode()).decode()
    response = _response(
        TextPart(
            f"raw={_SPECIAL} json={escaped} url={quoted} "
            f"b64={b64} b64url={b64url}"
        ),
        ToolCallPart(
            tool_call_id="c1",
            tool_name="secret_store",
            args={"title": "db", "value": _SPECIAL},
        ),
    )
    sanitized = sanitize_model_response(response)
    assert _SPECIAL not in sanitized.text
    for form in (escaped, quoted, b64, b64url):
        assert form not in sanitized.text, form
    assert sanitized.text.count("[REDACTED]") >= 5


def test_short_values_are_not_tracked() -> None:
    assert scrub_sensitive_text("fix the db pw", ("pw",)) == "fix the db pw"


def test_scrub_sensitive_text_standalone() -> None:
    scrubbed = scrub_sensitive_text(
        f"a {json.dumps(_CANARY)} b {_CANARY} c", (_CANARY,)
    )
    assert _CANARY not in scrubbed
    assert json.dumps(_CANARY) not in scrubbed
    assert "[REDACTED]" in scrubbed


# ---------------------------------------------------------------------------
# 5. terminal rejected ToolResultEntry synthesis
# ---------------------------------------------------------------------------


def test_rejected_result_contract_and_deterministic_hash() -> None:
    first, _ = sanitize_tool_call(
        _store_call(call_id="call-1", args={"title": "db", "value": _CANARY})
    )
    second, _ = sanitize_tool_call(
        _store_call(call_id="call-1", args={"title": "db", "value": _CANARY})
    )
    other, _ = sanitize_tool_call(
        _store_call(call_id="call-2", args={"title": "db", "value": _CANARY})
    )
    for call in (first, second, other):
        result = call.rejection_result
        assert result is not None
        assert result.kind.value == "tool_result"
        assert result.status == "denied"
        assert result.execution_status.value == "failed"
        assert result.domain_outcome.value == "failure"
        assert result.effect_certainty.value == "certain"
        assert result.error_code == SENSITIVE_CALL_REJECTED_CODE
        assert result.output_preview is None
        assert _CANARY not in (result.error_detail or "")
        assert (result.error_detail or "") != ""
    assert first.rejection_result == second.rejection_result
    assert first.rejection_result != other.rejection_result
    assert first.rejection_result.payload_hash != other.rejection_result.payload_hash
    # Never a hash of the raw secret value.
    assert (
        hashlib.sha256(_CANARY.encode()).hexdigest()
        != first.rejection_result.payload_hash
    )


def test_multiple_rejected_calls_produce_matching_results() -> None:
    response = _response(
        ToolCallPart(
            tool_call_id="c1",
            tool_name="secret_store",
            args={"title": "db", "value": _CANARY},
        ),
        ToolCallPart(
            tool_call_id="c2", tool_name="secret_rotate", args={"item": "i"}
        ),
        ToolCallPart(
            tool_call_id="c3", tool_name="secret_get", args="not json {"
        ),
    )
    sanitized = sanitize_model_response(response)
    assert sanitized.had_rejected_calls is True
    assert [call.rejected for call in sanitized.tool_calls] == [True, True, True]
    assert len(sanitized.rejected_results) == 3
    assert [result.call_id for result in sanitized.rejected_results] == [
        "c1",
        "c2",
        "c3",
    ]


def test_rejection_reason_vocabulary() -> None:
    reasons = {
        "raw_secret_value_argument": {"title": "db", "value": _CANARY},
        "unparseable_secret_tool_args": "broken {",
        "secret_store_requires_generate_true": {"title": "db"},
        "unknown_secret_operation": {"item": "i"},
    }
    seen: set[str] = set()
    for args in reasons.values():
        call, _ = sanitize_tool_call(_store_call(args=args))
        seen.add(call.rejection_reason or "")
        assert call.rejection_reason in reasons
    call, _ = sanitize_tool_call(
        ToolCallPart(tool_call_id="c9", tool_name="secret_wipe", args={})
    )
    seen.add(call.rejection_reason or "")
    assert seen == set(reasons)
    assert SENSITIVE_CALL_REJECTED_CODE == "sensitive_call_rejected"


# ---------------------------------------------------------------------------
# 6. marker args are never dispatchable (real projected schema gate)
# ---------------------------------------------------------------------------


def test_rejected_marker_args_fail_the_real_schema_gate() -> None:
    descriptor = parse_descriptor(SECRET_STORE_CAPABILITY)
    projected = _agent_input_schema_for(SECRET_STORE_CAPABILITY_ID, descriptor)
    call, _ = sanitize_tool_call(
        _store_call(args={"title": "db", "value": _CANARY})
    )
    violation = _validate_payload(call.safe_args, projected, path="$")
    assert violation is not None
    assert _CANARY not in json.dumps(call.safe_args)


def test_raw_value_field_vocabulary_matches_the_bridge() -> None:
    assert SECRET_RAW_VALUE_FIELDS >= {"value", "secret", "password"}
    assert "title" not in SECRET_RAW_VALUE_FIELDS


# ---------------------------------------------------------------------------
# 7. bounds on hostile oversized input
# ---------------------------------------------------------------------------


def test_oversized_secret_value_stays_bounded() -> None:
    huge = "s" * 40_000
    response = _response(
        TextPart(f"answer {huge}"),
        ToolCallPart(
            tool_call_id="c1",
            tool_name="secret_store",
            args={"title": "db", "value": huge},
        ),
    )
    sanitized = sanitize_model_response(response)
    assert len(sanitized.text) <= MAX_ENTRY_TEXT_CHARS
    assert huge not in sanitized.text
    result = sanitized.rejected_results[0]
    assert len(result.error_detail or "") <= 200


def test_oversized_malformed_args_rejected_with_bounded_detail() -> None:
    part = ToolCallPart(
        tool_call_id="c1", tool_name="secret_store", args="x" * 30_000
    )
    call, values = sanitize_tool_call(part)
    assert call.rejected is True
    assert call.rejection_reason == "unparseable_secret_tool_args"
    assert len(call.safe_args) <= 4
    assert call.rejection_result is not None
    assert len(call.rejection_result.error_detail or "") <= 200
    assert len(values[0]) <= MAX_ENTRY_TEXT_CHARS


def test_sanitized_response_is_deterministic() -> None:
    response = _response(
        ThinkingPart(content=f"note {_CANARY}"),
        TextPart(f"text {_CANARY}"),
        ToolCallPart(
            tool_call_id="c1",
            tool_name="secret_store",
            args={"title": "db", "value": _CANARY},
        ),
    )
    first = sanitize_model_response(response)
    second = sanitize_model_response(response)
    assert first == second
    assert isinstance(first, SanitizedModelResponse)
