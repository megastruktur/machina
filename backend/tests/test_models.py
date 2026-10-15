"""Focused tests for model connection config and client resolution.

Cards/contracts: ``06-model-config`` against contracts.md **C06 rule 1**
(endpoint config and model selection are separate; secret refs resolve at
call time; only allowlisted settings compatible with the endpoint are sent;
deliberate capability failures are classified structured errors) and
**C03 rule 6** (credentials resolve from the configured ``.env`` source only;
references never values; ambient environment is never consulted), plus the
C01 envelope vocabulary for error classification.

Acceptance coverage:

- AC1: base URL/token reference and selected settings obey the explicit
  model profile; unsupported options are classified (``unknown_field``,
  ``inline_credential_forbidden``, ``unsupported_option``,
  ``invalid_selection``, ``missing_resolver``).
- AC2: reachability/auth/chat/tool-call tests are distinct scenarios; no
  production provider token appears in any model/config representation.

Secret hygiene: every "credential" below is the synthetic sentinel
``…not-a-secret…`` marker, never a real secret. It is designed to collide
with every rendering path (values, reprs, JSON dumps, CLI stdout) so the
absence assertions are meaningful non-disclosure controls.

Live-provider boundary: the real configured gateway is exercised by the
writer's evidence probe and at the ``06-verify`` gate (AGT-PROVIDER
scenario, runtime-scenarios.md); here a controlled in-thread OpenAI-compatible
endpoint provides deterministic reachability/auth/chat/tool-call
distinguishing without network, cost or nondeterminism.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent

from mai.agents.models import (
    API_PLACEHOLDER_KEY,
    EndpointCapabilities,
    ModelConfigError,
    ProviderFailureKind,
    build_model,
    classify_provider_error,
    model_settings,
    parse_connection,
    parse_profile,
    validate_selection,
)
from mai.credentials.env import EnvErrorCode, EnvFileError, EnvResolver

BACKEND_DIR = Path(__file__).resolve().parents[1]

#: Synthetic credential material: safe to commit, designed to collide with
#: any accidental rendering path (see module docstring).
_SENTINEL = "sk-sentinel-not-a-secret-0123456789abcdef"

_CONNECTION_DOC: dict[str, Any] = {
    "schema_version": 1,
    "name": "local-gateway",
    "base_url": "http://127.0.0.1:4000/v1",
    "credential_ref": "GATEWAY_KEY",
    "timeout_seconds": 30,
    "headers": {"X-Deployment": "tests"},
    "capabilities": {"tool_calls": True},
}

_PROFILE_DOC: dict[str, Any] = {
    "schema_version": 1,
    "name": "fast-chat",
    "connection": "local-gateway",
    "model": "test-model",
    "temperature": 0.2,
    "top_p": 0.9,
    "seed": 7,
    "max_tokens": 256,
}


# ---------------------------------------------------------------------------
# Controlled OpenAI-compatible endpoint (owned fixture)
# ---------------------------------------------------------------------------


def _error_body(message: str) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "param": None,
            "code": None,
        }
    }


def _completion(model: str, content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-controlled",
        "object": "chat.completion",
        "created": 1700000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def _tool_completion(
    model: str, call_id: str, name: str, arguments: str
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-controlled-tool",
        "object": "chat.completion",
        "created": 1700000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }


class _ControlledEndpoint:
    """In-thread OpenAI-compatible ``/v1/chat/completions`` endpoint.

    ``handler(request_body, headers) -> (status, payload)`` is the whole
    behavior; request bodies and auth headers are recorded for wire
    assertions. Loopback only, ephemeral port, no external network.
    """

    def __init__(
        self,
        handler: Callable[[dict[str, Any], dict[str, str]], tuple[int, dict[str, Any]]],
    ) -> None:
        self._handler = handler
        self.requests: list[dict[str, Any]] = []
        self.auth_headers: list[str | None] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_ControlledEndpoint":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (http.server API)
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(body)
                # Header names are lowercased: the SDK's HTTP stack transmits
                # lowercase names, and plain-dict lookups are case-sensitive.
                headers = {str(k).lower(): str(v) for k, v in self.headers.items()}
                auth = headers.get("authorization")
                outer.auth_headers.append(auth)
                status, payload = outer._handler(body, headers)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: object) -> None:  # silenced
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        assert self._server is not None
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/v1"


def _bearer_guard(
    token: str, respond: Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]
) -> Callable[[dict[str, Any], dict[str, str]], tuple[int, dict[str, Any]]]:
    """Wrap a responder with an exact-Bearer credential check (401 on miss)."""

    def handler(
        body: dict[str, Any], headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        if headers.get("authorization") != f"Bearer {token}":
            return 401, _error_body("invalid credential")
        return respond(body)

    return handler


def _write_env_file(directory: Path, entries: dict[str, str]) -> Path:
    path = directory / "agents.env"
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in entries.items()),
        encoding="utf-8",
    )
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def _selection(
    connection_doc: dict[str, Any] | None = None,
    profile_doc: dict[str, Any] | None = None,
) -> Any:
    connection = parse_connection(
        _CONNECTION_DOC if connection_doc is None else connection_doc
    )
    profile = parse_profile(_PROFILE_DOC if profile_doc is None else profile_doc)
    return validate_selection(connection, profile)


def _closed_loopback_port() -> int:
    """A loopback port with no listener (best effort; the loopback reuse
    window is acceptable for a deterministic connection-refused probe)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _agent_error(resolved: Any, prompt: str = "ping") -> tuple[type[BaseException], BaseException]:
    """Run one prompt through the resolved model and capture the failure."""

    async def probe() -> None:
        agent = Agent(resolved.model)
        try:
            await agent.run(prompt)
        finally:
            await resolved.provider.client.close()

    try:
        _run(probe())
    except BaseException as error:  # noqa: BLE001 — the failure IS the result
        return type(error), error
    raise AssertionError("expected the provider request to fail")


# ---------------------------------------------------------------------------
# AC1 — connection schema: base URL + token reference, classification
# ---------------------------------------------------------------------------


def test_connection_minimal_document_parses() -> None:
    connection = parse_connection(
        {
            "schema_version": 1,
            "name": "local",
            "base_url": "http://127.0.0.1:9000/v1/",
        }
    )
    assert connection.name == "local"
    assert connection.base_url == "http://127.0.0.1:9000/v1"
    assert connection.credential_ref is None
    assert connection.timeout_seconds is None
    assert connection.headers == ()
    assert connection.capabilities == EndpointCapabilities()


def test_connection_full_document_parses() -> None:
    connection = parse_connection(_CONNECTION_DOC)
    assert connection.credential_ref == "GATEWAY_KEY"
    assert connection.timeout_seconds == 30.0
    assert connection.headers == (("X-Deployment", "tests"),)
    assert connection.capabilities == EndpointCapabilities(
        tool_calls=True, reasoning=False
    )


def test_unknown_field_vs_inline_credential_classified() -> None:
    with pytest.raises(ModelConfigError) as excinfo:
        parse_connection({**_CONNECTION_DOC, "typo_key": "x"})
    assert excinfo.value.fields == {"typo_key": "unknown_field"}

    inline = f"{_SENTINEL}-inline"
    with pytest.raises(ModelConfigError) as excinfo:
        parse_connection({**_CONNECTION_DOC, "api_key": inline})
    assert excinfo.value.fields == {"api_key": "inline_credential_forbidden"}
    # The value must never surface through the error (C01 rule 3).
    assert inline not in str(excinfo.value)


def test_base_url_rules_classified() -> None:
    for bad in ("ftp://host/v1", "http:///v1", "not a url", "http://host/v1?x=1"):
        with pytest.raises(ModelConfigError) as excinfo:
            parse_connection(
                {"schema_version": 1, "name": "local", "base_url": bad}
            )
        assert excinfo.value.fields == {"base_url": "invalid_field"}
    with pytest.raises(ModelConfigError) as excinfo:
        parse_connection(
            {
                "schema_version": 1,
                "name": "local",
                "base_url": "http://user:pw@host/v1",
            }
        )
    assert excinfo.value.fields == {"base_url": "inline_credential_forbidden"}


def test_credential_ref_shape_classified() -> None:
    with pytest.raises(ModelConfigError) as excinfo:
        parse_connection({**_CONNECTION_DOC, "credential_ref": "gateway-key"})
    assert excinfo.value.fields == {"credential_ref": "invalid_field"}


def test_header_rules_classified() -> None:
    with pytest.raises(ModelConfigError) as excinfo:
        parse_connection({**_CONNECTION_DOC, "headers": {"Authorization": "Bearer x"}})
    assert excinfo.value.fields == {
        "headers.Authorization": "inline_credential_forbidden"
    }

    with pytest.raises(ModelConfigError) as excinfo:
        parse_connection({**_CONNECTION_DOC, "headers": {"X-Api-Key": _SENTINEL}})
    assert excinfo.value.fields == {
        "headers.X-Api-Key": "inline_credential_forbidden"
    }
    assert _SENTINEL not in str(excinfo.value)


def test_capabilities_strictly_classified() -> None:
    with pytest.raises(ModelConfigError) as excinfo:
        parse_connection({**_CONNECTION_DOC, "capabilities": {"streaming": True}})
    assert excinfo.value.fields == {"capabilities.streaming": "unknown_field"}

    with pytest.raises(ModelConfigError) as excinfo:
        parse_connection({**_CONNECTION_DOC, "capabilities": {"tool_calls": "yes"}})
    assert excinfo.value.fields == {"capabilities.tool_calls": "invalid_field"}


def test_timeout_bounds_classified() -> None:
    for bad in (0.5, 601):
        with pytest.raises(ModelConfigError) as excinfo:
            parse_connection({**_CONNECTION_DOC, "timeout_seconds": bad})
        assert excinfo.value.fields == {"timeout_seconds": "invalid_field"}


def test_profile_parses_and_keeps_selected_settings() -> None:
    profile = parse_profile(_PROFILE_DOC)
    assert profile.model == "test-model"
    assert profile.connection == "local-gateway"
    assert (profile.temperature, profile.top_p, profile.seed, profile.max_tokens) == (
        0.2,
        0.9,
        7,
        256,
    )
    assert profile.reasoning_effort is None


def test_profile_unknown_setting_and_bad_value_classified() -> None:
    with pytest.raises(ModelConfigError) as excinfo:
        parse_profile({**_PROFILE_DOC, "logit_bias": {"x": 1}})
    assert excinfo.value.fields == {"logit_bias": "unknown_field"}

    with pytest.raises(ModelConfigError) as excinfo:
        parse_profile({**_PROFILE_DOC, "reasoning_effort": "maximum"})
    assert excinfo.value.fields == {"reasoning_effort": "invalid_field"}


def test_profile_schema_version_2_rejected() -> None:
    with pytest.raises(ModelConfigError) as excinfo:
        parse_profile({**_PROFILE_DOC, "schema_version": 2})
    assert excinfo.value.fields == {"schema_version": "schema_version_unsupported"}


def test_profile_out_of_range_temperature_classified() -> None:
    with pytest.raises(ModelConfigError) as excinfo:
        parse_profile({**_PROFILE_DOC, "temperature": 3.5})
    assert excinfo.value.fields == {"temperature": "invalid_field"}


# ---------------------------------------------------------------------------
# AC1 — selection binding: explicit pairing and capability classification
# ---------------------------------------------------------------------------


def test_selection_mismatch_classified() -> None:
    with pytest.raises(ModelConfigError) as excinfo:
        _selection(profile_doc={**_PROFILE_DOC, "connection": "elsewhere"})
    assert excinfo.value.fields == {"connection": "invalid_selection"}


def test_unsupported_reasoning_option_classified_without_any_request() -> None:
    endpoint = _ControlledEndpoint(
        lambda body, headers: (200, _completion("test-model", "hi"))
    )
    profile = parse_profile({**_PROFILE_DOC, "reasoning_effort": "low"})
    with endpoint:
        connection = parse_connection(_CONNECTION_DOC)
        with pytest.raises(ModelConfigError) as excinfo:
            validate_selection(connection, profile)
    assert excinfo.value.fields == {"reasoning_effort": "unsupported_option"}
    assert endpoint.requests == [], "bind-time rejection must not touch the wire"


def test_reasoning_allowed_when_capability_declared() -> None:
    doc = {
        **_CONNECTION_DOC,
        "capabilities": {"tool_calls": True, "reasoning": True},
    }
    selection = _selection(
        connection_doc=doc,
        profile_doc={**_PROFILE_DOC, "reasoning_effort": "low"},
    )
    assert model_settings(selection.profile)["openai_reasoning_effort"] == "low"


# ---------------------------------------------------------------------------
# AC1 — settings obey the explicit profile
# ---------------------------------------------------------------------------


def test_settings_payload_is_exactly_the_selected_allowlist() -> None:
    profile = parse_profile(_PROFILE_DOC)
    assert dict(model_settings(profile)) == {
        "temperature": 0.2,
        "top_p": 0.9,
        "seed": 7,
        "max_tokens": 256,
    }
    bare = parse_profile(
        {"schema_version": 1, "name": "p", "connection": "c", "model": "m"}
    )
    assert dict(model_settings(bare)) == {}


def test_built_model_carries_profile_identity_and_settings() -> None:
    with _ControlledEndpoint(
        lambda body, headers: (200, _completion("test-model", "hi"))
    ) as endpoint:
        connection = parse_connection(
            {
                **_CONNECTION_DOC,
                "base_url": endpoint.base_url,
                "credential_ref": None,
            }
        )
        profile = parse_profile(_PROFILE_DOC)
        resolved = build_model(validate_selection(connection, profile))
        try:
            assert resolved.model.model_name == profile.model
            assert resolved.provider.base_url == f"{endpoint.base_url}/"
            assert dict(resolved.model.settings) == dict(model_settings(profile))
        finally:
            _run(resolved.provider.client.close())


# ---------------------------------------------------------------------------
# AC1/AC2 — credential resolution: reference only, configured source only
# ---------------------------------------------------------------------------


def test_unauthenticated_connection_ignores_ambient_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", _SENTINEL)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://ambient.invalid/v1")
    selection = _selection(connection_doc={**_CONNECTION_DOC, "credential_ref": None})
    resolved = build_model(selection)
    try:
        assert resolved.provider.client.api_key == API_PLACEHOLDER_KEY
        assert resolved.provider.base_url == "http://127.0.0.1:4000/v1/"
    finally:
        _run(resolved.provider.client.close())


def test_timeout_default_preserved_when_unset() -> None:
    from openai import DEFAULT_TIMEOUT

    unset = build_model(_selection(connection_doc={**_CONNECTION_DOC, "timeout_seconds": None, "credential_ref": None}))
    try:
        assert unset.provider.client.timeout == DEFAULT_TIMEOUT
    finally:
        _run(unset.provider.client.close())
    declared = build_model(
        _selection(
            connection_doc={**_CONNECTION_DOC, "credential_ref": None}  # 30s
        )
    )
    try:
        assert declared.provider.client.timeout == 30.0
    finally:
        _run(declared.provider.client.close())


def test_credential_ref_resolves_only_via_configured_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A decoy ambient variable must never be consulted (C03 rule 6).
    monkeypatch.setenv("GATEWAY_KEY", "ambient-not-allowed")
    env_file = _write_env_file(tmp_path, {"GATEWAY_KEY": _SENTINEL})
    resolved = build_model(_selection(), resolver=EnvResolver(env_file))
    try:
        assert resolved.provider.client.api_key == _SENTINEL
        # The reference (config) may appear; the value may not.
        assert "GATEWAY_KEY" in repr(resolved)
        assert _SENTINEL not in repr(resolved)
    finally:
        _run(resolved.provider.client.close())


def test_no_production_token_in_any_model_representation(tmp_path: Path) -> None:
    env_file = _write_env_file(tmp_path, {"GATEWAY_KEY": _SENTINEL})
    resolved = build_model(_selection(), resolver=EnvResolver(env_file))
    try:
        renderings = {
            "resolved": repr(resolved),
            "model": repr(resolved.model),
            "provider": repr(resolved.provider),
            "client": repr(resolved.provider.client),
            "connection": json.dumps(dataclasses.asdict(resolved.connection)),
            "profile": json.dumps(dataclasses.asdict(resolved.profile)),
            "settings": json.dumps(dict(model_settings(resolved.profile))),
        }
        for label, rendering in renderings.items():
            assert _SENTINEL not in rendering, f"token leaked via {label}"
    finally:
        _run(resolved.provider.client.close())


def test_missing_resolver_classified() -> None:
    with pytest.raises(ModelConfigError) as excinfo:
        build_model(_selection())
    assert excinfo.value.fields == {"credential_ref": "missing_resolver"}


def test_missing_env_key_structured_and_value_free(tmp_path: Path) -> None:
    env_file = _write_env_file(tmp_path, {"OTHER_KEY": _SENTINEL})
    with pytest.raises(EnvFileError) as excinfo:
        build_model(_selection(), resolver=EnvResolver(env_file))
    assert excinfo.value.code is EnvErrorCode.MISSING_KEY
    assert _SENTINEL not in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC2 — distinguished reachability/auth/chat/tool-call scenarios
# ---------------------------------------------------------------------------


def test_reachability_failure_classified() -> None:
    connection = parse_connection(
        {
            **_CONNECTION_DOC,
            "base_url": f"http://127.0.0.1:{_closed_loopback_port()}/v1",
            "credential_ref": None,
        }
    )
    resolved = build_model(
        validate_selection(connection, parse_profile(_PROFILE_DOC))
    )
    _, error = _agent_error(resolved)
    assert classify_provider_error(error) is ProviderFailureKind.REACHABILITY
    assert classify_provider_error(error) is not ProviderFailureKind.AUTH


def test_auth_failure_classified(tmp_path: Path) -> None:
    with _ControlledEndpoint(
        _bearer_guard(_SENTINEL, lambda body: (200, _completion("test-model", "hi")))
    ) as endpoint:
        selection = _selection(
            connection_doc={**_CONNECTION_DOC, "base_url": endpoint.base_url},
        )
        # A well-formed credential the endpoint does not recognize: the
        # resolution succeeds locally, the endpoint rejects it with 401.
        resolved = build_model(
            selection,
            resolver=EnvResolver(
                _write_env_file(tmp_path, {"GATEWAY_KEY": f"{_SENTINEL}-wrong"})
            ),
        )
        _, error = _agent_error(resolved)
    kind = classify_provider_error(error)
    assert kind is ProviderFailureKind.AUTH
    assert kind is not ProviderFailureKind.REACHABILITY
    assert endpoint.requests, "the request must actually reach the endpoint"
    assert endpoint.auth_headers[0] == f"Bearer {_SENTINEL}-wrong"


def test_chat_completion_round_trip_obeyes_profile(tmp_path: Path) -> None:
    with _ControlledEndpoint(
        _bearer_guard(
            _SENTINEL,
            lambda body: (200, _completion(body["model"], "controlled-ok")),
        )
    ) as endpoint:
        selection = _selection(
            connection_doc={**_CONNECTION_DOC, "base_url": endpoint.base_url},
        )
        resolved = build_model(
            selection, resolver=EnvResolver(_write_env_file(tmp_path, {"GATEWAY_KEY": _SENTINEL}))
        )
        agent = Agent(resolved.model)

        async def chat() -> Any:
            try:
                return await agent.run("Say the controlled phrase.")
            finally:
                await resolved.provider.client.close()

        result = _run(chat())
    assert result.output == "controlled-ok"
    request = endpoint.requests[0]
    profile = selection.profile
    assert request["model"] == profile.model
    assert request["temperature"] == profile.temperature
    assert request["top_p"] == profile.top_p
    assert request["seed"] == profile.seed
    # The adapter maps the profile output limit onto the Chat Completions
    # field its model profile selects (max_completion_tokens on modern
    # endpoints, max_tokens otherwise); the selected value must reach the wire.
    assert request.get("max_completion_tokens", request.get("max_tokens")) == (
        profile.max_tokens
    )
    assert endpoint.auth_headers[0] == f"Bearer {_SENTINEL}"


def test_tool_call_round_trip_executes_local_tool(tmp_path: Path) -> None:
    phase = {"step": 0}

    def respond(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if phase["step"] == 0:
            phase["step"] = 1
            return (
                200,
                _tool_completion(
                    body["model"], "call_1", "get_temperature", '{"city": "oslo"}'
                ),
            )
        return 200, _completion(body["model"], "oslo is 12C")

    with _ControlledEndpoint(_bearer_guard(_SENTINEL, respond)) as endpoint:
        selection = _selection(
            connection_doc={**_CONNECTION_DOC, "base_url": endpoint.base_url},
        )
        resolved = build_model(
            selection, resolver=EnvResolver(_write_env_file(tmp_path, {"GATEWAY_KEY": _SENTINEL}))
        )
        agent = Agent(resolved.model)
        calls: list[str] = []

        @agent.tool_plain
        def get_temperature(city: str) -> str:
            calls.append(city)
            return "12C"

        async def run_tool_loop() -> Any:
            try:
                return await agent.run("Temperature in oslo?")
            finally:
                await resolved.provider.client.close()

        result = _run(run_tool_loop())
    assert calls == ["oslo"], "the local tool must execute exactly once"
    assert result.output == "oslo is 12C"
    first, second = endpoint.requests[0], endpoint.requests[1]
    assert any(
        tool["function"]["name"] == "get_temperature" for tool in first["tools"]
    )
    assert any(
        message.get("role") == "tool" and "12C" in json.dumps(message)
        for message in second["messages"]
    ), "the tool result must be sent back to the endpoint"


def test_runtime_unsupported_setting_classified(tmp_path: Path) -> None:
    def reject_reasoning(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if "reasoning_effort" in body:
            return 400, _error_body("Unsupported parameter: 'reasoning_effort'")
        return 200, _completion(body["model"], "hi")

    with _ControlledEndpoint(_bearer_guard(_SENTINEL, reject_reasoning)) as endpoint:
        doc = {
            **_CONNECTION_DOC,
            "base_url": endpoint.base_url,
            "capabilities": {"tool_calls": True, "reasoning": True},
        }
        selection = _selection(
            connection_doc=doc,
            profile_doc={**_PROFILE_DOC, "reasoning_effort": "high"},
        )
        resolved = build_model(
            selection, resolver=EnvResolver(_write_env_file(tmp_path, {"GATEWAY_KEY": _SENTINEL}))
        )
        _, error = _agent_error(resolved)
    assert classify_provider_error(error) is ProviderFailureKind.REJECTED_REQUEST


def test_provider_and_unknown_failures_classified() -> None:
    with _ControlledEndpoint(
        lambda body, headers: (500, _error_body("upstream exploded"))
    ) as endpoint:
        selection = _selection(
            connection_doc={
                **_CONNECTION_DOC,
                "base_url": endpoint.base_url,
                "credential_ref": None,
            },
        )
        resolved = build_model(selection)
        _, error = _agent_error(resolved)
    assert classify_provider_error(error) is ProviderFailureKind.PROVIDER
    assert (
        classify_provider_error(ValueError("not an SDK error"))
        is ProviderFailureKind.UNKNOWN
    )


# ---------------------------------------------------------------------------
# Minimal actual entrypoint (structured, value-free CLI)
# ---------------------------------------------------------------------------


def test_entrypoint_validate_and_resolve(tmp_path: Path) -> None:
    connection_path = tmp_path / "connection.json"
    profile_path = tmp_path / "profile.json"
    env_path = _write_env_file(tmp_path, {"GATEWAY_KEY": _SENTINEL})
    endpoint = _ControlledEndpoint(
        lambda body, headers: (200, _completion(body["model"], "hi"))
    )
    with endpoint:
        connection_path.write_text(
            json.dumps({**_CONNECTION_DOC, "base_url": endpoint.base_url})
        )
    profile_path.write_text(json.dumps(_PROFILE_DOC))

    def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "mai.agents.models", *args],
            cwd=BACKEND_DIR,
            capture_output=True,
            text=True,
            check=False,
        )

    ok = run_cli("validate-connection", str(connection_path))
    assert ok.returncode == 0, ok.stderr
    payload = json.loads(ok.stdout)
    assert payload["kind"] == "ok"
    assert payload["connection"] == "local-gateway"

    ok = run_cli("validate-profile", str(profile_path))
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["model"] == "test-model"

    ok = run_cli(
        "resolve", str(connection_path), str(profile_path), "--env", str(env_path)
    )
    assert ok.returncode == 0, ok.stderr
    payload = json.loads(ok.stdout)
    assert payload["kind"] == "resolved"
    assert payload["credential_ref"] == "GATEWAY_KEY"
    assert payload["credential_sha256"] == hashlib.sha256(
        _SENTINEL.encode("utf-8")
    ).hexdigest()
    assert payload["settings"]["temperature"] == 0.2
    # Value-free output: the sentinel must never reach stdout/stderr.
    assert _SENTINEL not in ok.stdout + ok.stderr

    bad = run_cli("resolve", str(connection_path), str(profile_path))
    assert bad.returncode == 1
    assert json.loads(bad.stdout)["fields"] == {"credential_ref": "missing_resolver"}
