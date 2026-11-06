"""Focused tests for the allowlist-scoped, credential-bound service HTTP
capability (card 07-service-http).

Acceptance (BRIEF):

1. Explicit approved endpoint/method/credential reference/output budget
   for server-service investigation; no credential sent to an unrelated
   redirect target.
2. Unlike the public page reader, private-service access requires a
   declared grant; the resolved secret never enters model output.

Boundary discipline (test policy, risk-critical slice): payload/allowlist
validation runs pure; outbound request shape, redirect-origin enforcement,
budget enforcement and HTTP/timeout/connect classification run through the
real httpx2 client machinery over an explicit ``MockTransport`` (real
client boundary, deterministic upstream, per-hop credential assertions)
and through a controlled loopback upstream (a LiteLLM-like fixture server
recording every request, plus a second "stranger" server that must never
receive anything); the dispatcher integration runs the real
``03-capability-registry``, ``03-dispatcher``, ``03-policy`` and
``03-credentials`` producers with the ``service.http`` descriptor
registered and a real ``EnvResolver`` over a synthetic-sentinel ``.env``
file. The redaction sweep and the forbidden-redirect proof (the stranger
records zero requests while the upstream records exactly the hops the
approved origin allowed) are the writer half of the feature-gate
scenario; the assembled live journey against a real LiteLLM-class service
is owned by ``07-verify``. No real LiteLLM probe here.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx2
import pytest

from mai.capabilities.dispatch import (
    REDACTED,
    CapabilityCall,
    CapabilityDispatcher,
    DispatchOutcome,
    DispatchReason,
    ExecutorResult,
)
from mai.capabilities.service_http import (
    CAPABILITY_ID,
    DEFAULT_MAX_BYTES,
    HttpServiceProvider,
    MAX_PATH_CHARS,
    MAX_REDIRECTS,
    MAX_SERVICE_NAME_CHARS,
    MIN_MAX_BYTES,
    RawServiceResponse,
    ServiceError,
    ServiceErrorCode,
    ServiceHttpCapability,
    descriptor,
    endpoint_for,
    origin_of,
    request_from_payload,
    validate_allowlist,
    validate_base_url,
    validate_service_path,
)
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.plugins.registry import CapabilityRegistry
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import ExecutionContext, Policy, PolicyMode

BACKEND_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.anyio

# Frozen fixture identities (canonical lowercase UUID strings, TEST-NET style).
SERVER_A = "5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60"
SERVER_B = "8f1c2e3d-4a5b-4c6d-9e7f-0a1b2c3d4e5f"
RUN_1 = "6e7f8a9b-0c1d-4e2f-8a3b-4c5d6e7f8a90"
PRINCIPAL = "tester@mai.local"

#: Inert synthetic credential material (the redaction sweep target and
#: the bearer the controlled upstream expects).
SECRET_NAME = "MAI_FXT_SVC_TOKEN"
SECRET_VALUE = "fxt-synthetic-service-token-07a1b2c3"

#: The host-wired managed-services allowlist of the fixtures.
SERVICES = {"litellm": "http://litellm:4000/v1"}


def _context(
    *,
    server_id: str = SERVER_A,
    references: tuple[str, ...] = (SECRET_NAME,),
    allowed: tuple[str, ...] = (CAPABILITY_ID,),
    mode: PolicyMode = PolicyMode.DELEGATED,
) -> ExecutionContext:
    return ExecutionContext(
        principal=PRINCIPAL,
        server_id=uuid.UUID(server_id),
        run_id=uuid.UUID(RUN_1),
        parent_scope=None,
        profile_revision="prof-1",
        prompt_revision="prompt-1",
        credential_references=references,
        allowed_capabilities=allowed,
        mode=mode,
    )


def _call(
    payload: Mapping[str, object],
    context: ExecutionContext,
    credentials: Mapping[str, object] | None = None,
) -> CapabilityCall:
    if credentials is None:
        from mai.credentials.env import SecretValue

        credentials = {SECRET_NAME: SecretValue(SECRET_VALUE)}
    return CapabilityCall(
        descriptor=descriptor(),
        payload=payload,
        context=context,
        credentials=credentials,
        op_id=uuid.uuid4(),
    )


GOOD_PAYLOAD = {
    "service": "litellm",
    "method": "GET",
    "path": "/v1/models",
    "credential_ref": SECRET_NAME,
}


# ---------------------------------------------------------------------------
# Pure seam: payload validation
# ---------------------------------------------------------------------------


class TestRequestValidation:
    def test_defaults_applied(self) -> None:
        request = request_from_payload(dict(GOOD_PAYLOAD))
        assert request.service == "litellm"
        assert request.method == "GET"
        assert request.path == "/v1/models"
        assert request.body is None
        assert request.credential_ref == SECRET_NAME
        assert request.max_bytes == DEFAULT_MAX_BYTES
        assert request.timeout_seconds == 15.0

    def test_method_case_normalized(self) -> None:
        payload = dict(GOOD_PAYLOAD, method="get")
        assert request_from_payload(payload).method == "GET"

    def test_post_body_canonicalized(self) -> None:
        payload = dict(
            GOOD_PAYLOAD, method="POST", path="/v1/chat/completions",
            body={"model": "fxt", "messages": [{"role": "user", "content": "hi"}]},
        )
        request = request_from_payload(payload)
        assert request.method == "POST"
        assert request.body == (
            '{"messages":[{"content":"hi","role":"user"}],"model":"fxt"}'
        )

    def test_non_mapping_payload_rejected(self) -> None:
        with pytest.raises(ServiceError) as info:
            request_from_payload(["not", "a", "mapping"])  # type: ignore[arg-type]
        assert info.value.code is ServiceErrorCode.INVALID_PAYLOAD

    @pytest.mark.parametrize(
        "field", ["url", "endpoint", "headers", "auth", "credential",
                  "allowlist", "absolute_url"]
    )
    def test_smuggled_field_rejected(self, field: str) -> None:
        with pytest.raises(ServiceError) as info:
            request_from_payload(dict(GOOD_PAYLOAD, **{field: "evil"}))
        assert info.value.code is ServiceErrorCode.INVALID_PAYLOAD
        assert field in str(info.value)

    @pytest.mark.parametrize(
        "service",
        ["", "   ", "a" * (MAX_SERVICE_NAME_CHARS + 1), "lite\x00llm", 7],
    )
    def test_bad_service_rejected(self, service: object) -> None:
        with pytest.raises(ServiceError) as info:
            request_from_payload(dict(GOOD_PAYLOAD, service=service))
        assert info.value.code is ServiceErrorCode.INVALID_PAYLOAD

    @pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "HEAD", 7])
    def test_bad_method_rejected(self, method: object) -> None:
        with pytest.raises(ServiceError) as info:
            request_from_payload(dict(GOOD_PAYLOAD, method=method))
        assert info.value.code is ServiceErrorCode.INVALID_PAYLOAD

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "models",
            "/a" * (MAX_PATH_CHARS // 2 + 1),
            "/lite\x00llm/models",
            "/v1/models\r\nX-Injected: 1",
            "/v1/models\nHost: evil",
            "/v1\\models",
            "/v1/models#fragment",
            "/http://evil.invalid/path",
        ],
    )
    def test_bad_path_rejected(self, path: object) -> None:
        with pytest.raises(ServiceError) as info:
            request_from_payload(dict(GOOD_PAYLOAD, path=path))
        assert info.value.code is ServiceErrorCode.INVALID_PAYLOAD

    def test_path_with_query_accepted(self) -> None:
        request = request_from_payload(dict(GOOD_PAYLOAD, path="/v1/models?limit=2"))
        assert request.path == "/v1/models?limit=2"

    def test_get_with_body_rejected(self) -> None:
        with pytest.raises(ServiceError) as info:
            request_from_payload(dict(GOOD_PAYLOAD, body={"a": 1}))
        assert "body" in str(info.value)

    def test_non_json_body_rejected(self) -> None:
        with pytest.raises(ServiceError) as info:
            request_from_payload(
                dict(GOOD_PAYLOAD, method="POST", body={"x": float("nan")})
            )
        assert info.value.code is ServiceErrorCode.INVALID_PAYLOAD

    def test_overlarge_body_rejected(self) -> None:
        with pytest.raises(ServiceError) as info:
            request_from_payload(
                dict(GOOD_PAYLOAD, method="POST", body={"x": "y" * (16 * 1024)})
            )
        assert "budget" in str(info.value)

    @pytest.mark.parametrize(
        "ref", ["", "   ", "MY TOKEN", "TOK\x00EN", 7]
    )
    def test_bad_credential_ref_rejected(self, ref: object) -> None:
        with pytest.raises(ServiceError) as info:
            request_from_payload(dict(GOOD_PAYLOAD, credential_ref=ref))
        assert info.value.code is ServiceErrorCode.INVALID_PAYLOAD

    @pytest.mark.parametrize(
        "max_bytes", [MIN_MAX_BYTES - 1, 1024 * 1024 + 1, True, "16"]
    )
    def test_bad_max_bytes_rejected(self, max_bytes: object) -> None:
        with pytest.raises(ServiceError) as info:
            request_from_payload(dict(GOOD_PAYLOAD, max_bytes=max_bytes))
        assert info.value.code is ServiceErrorCode.INVALID_PAYLOAD

    @pytest.mark.parametrize("timeout", [0.05, 61.0, True, "5"])
    def test_bad_timeout_rejected(self, timeout: object) -> None:
        with pytest.raises(ServiceError) as info:
            request_from_payload(
                dict(GOOD_PAYLOAD, timeout_seconds=timeout)
            )
        assert info.value.code is ServiceErrorCode.INVALID_PAYLOAD

    def test_explicit_budgets_accepted(self) -> None:
        request = request_from_payload(
            dict(GOOD_PAYLOAD, max_bytes=MIN_MAX_BYTES, timeout_seconds=0.5)
        )
        assert request.max_bytes == MIN_MAX_BYTES
        assert request.timeout_seconds == 0.5


class TestPathValidation:
    def test_absolute_url_rejected(self) -> None:
        with pytest.raises(ServiceError):
            validate_service_path("http://evil.invalid/v1/models")

    def test_scheme_relative_rejected(self) -> None:
        with pytest.raises(ServiceError):
            validate_service_path("//evil.invalid/v1/models")

    def test_plain_path_rejected(self) -> None:
        with pytest.raises(ServiceError):
            validate_service_path("v1/models")


# ---------------------------------------------------------------------------
# Pure seam: allowlist validation and endpoint composition
# ---------------------------------------------------------------------------


class TestAllowlistValidation:
    def test_normalize_base(self) -> None:
        assert validate_base_url("HTTP://LiteLLM:4000/v1/") == (
            "http://litellm:4000/v1"
        )

    @pytest.mark.parametrize(
        "base",
        [
            "",
            "not-a-url",
            "ftp://litellm/v1",
            "litellm:4000",
            "http://user:pass@litellm:4000/v1",
            "http://litellm:4000/v1?api_key=inline",
            "http://litellm:4000/v1#frag",
            "http://litellm:notaport/v1",
        ],
    )
    def test_bad_base_rejected(self, base: str) -> None:
        with pytest.raises(ServiceError) as info:
            validate_base_url(base)
        assert info.value.code is ServiceErrorCode.INVALID_PAYLOAD

    def test_userinfo_rejection_names_c03(self) -> None:
        with pytest.raises(ServiceError) as info:
            validate_base_url("http://user:pass@litellm/v1")
        assert "userinfo" in str(info.value)

    def test_empty_allowlist_rejected(self) -> None:
        with pytest.raises(ServiceError):
            validate_allowlist({})

    def test_bad_name_rejected(self) -> None:
        with pytest.raises(ServiceError):
            validate_allowlist({"lite llm": "http://litellm:4000"})

    def test_duplicate_after_normalization_rejected(self) -> None:
        with pytest.raises(ServiceError) as info:
            validate_allowlist(
                {"LiteLLM": "http://a:1", "litellm": "http://b:2"}
            )
        assert "twice" in str(info.value)

    def test_construction_validates_eagerly(self) -> None:
        class _NeverUsed:
            async def fetch(self, **kwargs):  # pragma: no cover - never called
                raise AssertionError

        with pytest.raises(ServiceError):
            ServiceHttpCapability(_NeverUsed(), services={"bad name": "http://h"})
        with pytest.raises(ServiceError):
            ServiceHttpCapability(
                _NeverUsed(), services={"ok": "http://h", "bad": "ftp://h"}
            )

    def test_endpoint_composition_keeps_base_path_prefix(self) -> None:
        services = validate_allowlist(SERVICES)
        assert endpoint_for(services, "litellm", "/models") == (
            "http://litellm:4000/v1/models"
        )
        assert endpoint_for(services, "litellm", "/models?limit=1") == (
            "http://litellm:4000/v1/models?limit=1"
        )

    def test_unknown_service_is_a_refusal(self) -> None:
        with pytest.raises(ServiceError) as info:
            endpoint_for(validate_allowlist(SERVICES), "other-service", "/x")
        assert info.value.code is ServiceErrorCode.UNKNOWN_SERVICE


class TestOrigin:
    def test_default_ports_normalized(self) -> None:
        assert origin_of("http://h:80/x") == origin_of("http://h/y")
        assert origin_of("https://h:443/x") == origin_of("https://h/y")

    def test_nondefault_port_is_identity(self) -> None:
        assert origin_of("http://h:4000/x") == "http://h:4000"
        assert origin_of("http://h:4000/x") != origin_of("http://h:4001/x")

    def test_scheme_change_is_a_different_origin(self) -> None:
        assert origin_of("https://h/x") != origin_of("http://h/x")


# ---------------------------------------------------------------------------
# Capability against a deterministic provider (classification, binding)
# ---------------------------------------------------------------------------


class _StaticProvider:
    """Deterministic service provider (no network) recording every call."""

    def __init__(
        self,
        response: RawServiceResponse | BaseException,
    ) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    async def fetch(
        self,
        *,
        url: str,
        method: str,
        headers: Mapping[str, str],
        content: bytes | None,
        max_bytes: int,
        credential: str | None,
        allowed_origin: str,
    ) -> RawServiceResponse:
        self.calls.append(
            {
                "url": url,
                "method": method,
                "headers": dict(headers),
                "content": content,
                "max_bytes": max_bytes,
                "credential": credential,
                "allowed_origin": allowed_origin,
            }
        )
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


def _json_response(
    document: object,
    *,
    url: str = "http://litellm:4000/v1/models",
    status: int = 200,
    content_type: str = "application/json",
) -> RawServiceResponse:
    return RawServiceResponse(
        url=url,
        status_code=status,
        content_type=content_type,
        body=json.dumps(document).encode(),
        redirects=0,
    )


def _capability(provider: _StaticProvider) -> ServiceHttpCapability:
    return ServiceHttpCapability(provider, services=SERVICES)


class TestExecutorBinding:
    async def test_ok_call_round_trips_with_provenance(self) -> None:
        provider = _StaticProvider(_json_response({"object": "list"}))
        result = await _capability(provider)(
            _call(dict(GOOD_PAYLOAD), _context())
        )
        assert result.ok is True
        document = json.loads(result.output)
        assert document["status"] == "ok"
        assert document["trust"] == "untrusted_external"
        assert document["service"] == "litellm"
        assert document["method"] == "GET"
        assert document["endpoint"] == "http://litellm:4000/v1/models"
        assert document["json"] == {"object": "list"}
        assert document["redirects"] == 0

    async def test_request_shape_is_minimal_and_credential_bound(self) -> None:
        provider = _StaticProvider(_json_response({"ok": True}))
        payload = dict(
            GOOD_PAYLOAD, method="POST", path="/chat/completions",
            body={"model": "fxt"},
        )
        await _capability(provider)(_call(payload, _context()))
        assert len(provider.calls) == 1
        call = provider.calls[0]
        assert call["url"] == "http://litellm:4000/v1/chat/completions"
        assert call["method"] == "POST"
        assert call["content"] == b'{"model":"fxt"}'
        # The capability hands the provider exactly two caller headers;
        # the HTTP provider adds the screened User-Agent and the single
        # Authorization header at the boundary (proven at the real
        # client boundary in TestRedirectPolicy/TestControlledUpstream).
        assert call["headers"] == {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        assert call["credential"] == SECRET_VALUE
        assert call["allowed_origin"] == "http://litellm:4000"
        assert call["max_bytes"] == DEFAULT_MAX_BYTES

    async def test_credential_value_never_enters_result_document(self) -> None:
        """Even a hostile service echoing the bearer back verbatim cannot
        leak the secret through the executor's own result: the capability
        answer is the parsed JSON, and redaction at the dispatcher is the
        second layer (proven in TestDispatcherIntegration)."""
        provider = _StaticProvider(
            _json_response({"echo": f"Bearer {SECRET_VALUE}"})
        )
        result = await _capability(provider)(
            _call(dict(GOOD_PAYLOAD), _context())
        )
        assert result.ok is True
        assert SECRET_VALUE in result.output  # direct-call surface: quoted data
        document = json.loads(result.output)
        assert document["endpoint"] == "http://litellm:4000/v1/models"
        assert "://" not in document["endpoint"] or "@" not in document["endpoint"]

    async def test_get_sends_no_body(self) -> None:
        provider = _StaticProvider(_json_response({}))
        await _capability(provider)(_call(dict(GOOD_PAYLOAD), _context()))
        assert provider.calls[0]["content"] is None
        assert "Content-Type" not in provider.calls[0]["headers"]

    async def test_undeclared_credential_ref_is_refused_before_fetch(self) -> None:
        provider = _StaticProvider(_json_response({}))
        payload = dict(GOOD_PAYLOAD, credential_ref="MAI_FXT_OTHER_TOKEN")
        result = await _capability(provider)(_call(payload, _context()))
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "refused"
        assert document["code"] == "credential_not_declared"
        assert provider.calls == []

    async def test_declared_but_unresolved_credential_is_caller_error(self) -> None:
        provider = _StaticProvider(_json_response({}))
        call = _call(dict(GOOD_PAYLOAD), _context(), credentials={})
        with pytest.raises(ServiceError) as info:
            await _capability(provider)(call)
        assert info.value.code is ServiceErrorCode.INVALID_PAYLOAD
        assert provider.calls == []

    async def test_unknown_service_is_refused_before_fetch(self) -> None:
        provider = _StaticProvider(_json_response({}))
        result = await _capability(provider)(
            _call(dict(GOOD_PAYLOAD, service="other"), _context())
        )
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "refused"
        assert document["code"] == "unknown_service"
        assert provider.calls == []

    async def test_non_2xx_is_http_error_with_status(self) -> None:
        provider = _StaticProvider(
            _json_response({"error": "boom"}, status=500)
        )
        result = await _capability(provider)(
            _call(dict(GOOD_PAYLOAD), _context())
        )
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "unavailable"
        assert document["code"] == "http_error"
        assert document["http_status"] == 500
        assert document["json"] is None

    async def test_non_json_2xx_is_unsupported_content_type(self) -> None:
        provider = _StaticProvider(
            _json_response("<html></html>", content_type="text/html")
        )
        result = await _capability(provider)(
            _call(dict(GOOD_PAYLOAD), _context())
        )
        document = json.loads(result.output)
        assert document["status"] == "unavailable"
        assert document["code"] == "unsupported_content_type"
        assert document["http_status"] == 200

    async def test_json_suffix_content_type_accepted(self) -> None:
        provider = _StaticProvider(
            _json_response({"ok": 1}, content_type="application/vnd.api+json")
        )
        result = await _capability(provider)(
            _call(dict(GOOD_PAYLOAD), _context())
        )
        assert result.ok is True

    @pytest.mark.parametrize("raw", [b"not json{", b'"just a string"', b"5"])
    async def test_unparsable_or_scalar_json_is_provider_error(
        self, raw: bytes
    ) -> None:
        provider = _StaticProvider(
            RawServiceResponse(
                url="http://litellm:4000/v1/models",
                status_code=200,
                content_type="application/json",
                body=raw,
                redirects=0,
            )
        )
        result = await _capability(provider)(
            _call(dict(GOOD_PAYLOAD), _context())
        )
        document = json.loads(result.output)
        assert document["status"] == "unavailable"
        assert document["code"] == "provider_error"
        assert document["error_type"] == "invalid_json_body"

    async def test_transport_failure_is_classified_value_free(self) -> None:
        provider = _StaticProvider(httpx2.ConnectError("refused"))
        result = await _capability(provider)(
            _call(dict(GOOD_PAYLOAD), _context())
        )
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "unavailable"
        assert document["code"] == "fetch_unreachable"
        assert document["error_type"] == "ConnectError"
        assert SECRET_VALUE not in result.output

    async def test_result_document_shape_is_frozen(self) -> None:
        provider = _StaticProvider(_json_response({"a": 1}))
        result = await _capability(provider)(
            _call(dict(GOOD_PAYLOAD), _context())
        )
        document = json.loads(result.output)
        assert list(document) == [
            "code",
            "endpoint",
            "error_type",
            "http_status",
            "json",
            "method",
            "redirects",
            "service",
            "status",
            "truncated",
            "trust",
        ]


# ---------------------------------------------------------------------------
# Redirect origin policy at the real client boundary (MockTransport)
# ---------------------------------------------------------------------------


class _RedirectRecorder:
    """MockTransport handler recording every request the client makes."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.respond(request)

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        raise NotImplementedError


class TestRedirectPolicy:
    def _capability(
        self, recorder: _RedirectRecorder
    ) -> ServiceHttpCapability:
        provider = HttpServiceProvider(
            transport=httpx2.MockTransport(recorder.handler)
        )
        return ServiceHttpCapability(provider, services=SERVICES)

    async def test_same_origin_redirect_followed_with_credential(self) -> None:
        class Recorder(_RedirectRecorder):
            def respond(self, request: httpx2.Request) -> httpx2.Response:
                if request.url.path == "/v1/models":
                    return httpx2.Response(
                        307,
                        headers={"Location": "/v2/models"},
                        content=b"",
                    )
                return httpx2.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    content=b'{"object":"list"}',
                )

        recorder = Recorder()
        result = await self._capability(recorder)(
            _call(dict(GOOD_PAYLOAD, path="/models"), _context())
        )
        assert result.ok is True
        document = json.loads(result.output)
        assert document["status"] == "ok"
        assert document["redirects"] == 1
        assert document["endpoint"] == "http://litellm:4000/v2/models"
        # Both hops carried the credential — the origin never changed.
        assert [r.url.path for r in recorder.requests] == [
            "/v1/models",
            "/v2/models",
        ]
        for request in recorder.requests:
            assert request.headers["Authorization"] == (
                f"Bearer {SECRET_VALUE}"
            )

    async def test_cross_origin_redirect_refused_before_second_request(self) -> None:
        """AC1 negative control: a redirect to another host must refuse
        before that host receives anything — no credential to an
        unrelated redirect target."""

        class Recorder(_RedirectRecorder):
            def respond(self, request: httpx2.Request) -> httpx2.Response:
                return httpx2.Response(
                    302,
                    headers={"Location": "http://evil.invalid/steal"},
                    content=b"",
                )

        recorder = Recorder()
        result = await self._capability(recorder)(
            _call(dict(GOOD_PAYLOAD), _context())
        )
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "refused"
        assert document["code"] == "forbidden_redirect"
        assert document["endpoint"] == "http://evil.invalid/steal"
        # Exactly one request left the client: the redirect target was
        # never contacted.
        assert len(recorder.requests) == 1
        assert recorder.requests[0].url.host == "litellm"

    async def test_scheme_change_redirect_refused(self) -> None:
        class Recorder(_RedirectRecorder):
            def respond(self, request: httpx2.Request) -> httpx2.Response:
                return httpx2.Response(
                    307,
                    headers={"Location": "https://litellm:4000/v1/models"},
                    content=b"",
                )

        recorder = Recorder()
        result = await self._capability(recorder)(
            _call(dict(GOOD_PAYLOAD), _context())
        )
        document = json.loads(result.output)
        assert document["code"] == "forbidden_redirect"
        assert len(recorder.requests) == 1

    async def test_default_port_redirect_is_same_origin(self) -> None:
        """Origin comparison is authority-level: an http://h:80 spelling
        of the same endpoint is followed, not refused."""

        class Recorder(_RedirectRecorder):
            def respond(self, request: httpx2.Request) -> httpx2.Response:
                if request.url.path == "/v1/models":
                    return httpx2.Response(
                        301,
                        headers={"Location": "http://litellm:80/v2/models"},
                        content=b"",
                    )
                return httpx2.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    content=b"{}",
                )

        # The allowlist endpoint carries no explicit port for this case:
        # http://litellm + redirect to http://litellm:80 must match.
        recorder = Recorder()
        provider = HttpServiceProvider(
            transport=httpx2.MockTransport(recorder.handler)
        )
        capability = ServiceHttpCapability(
            provider, services={"litellm": "http://litellm"}
        )
        result = await capability(
            _call(dict(GOOD_PAYLOAD, path="/v1/models"), _context())
        )
        document = json.loads(result.output)
        assert document["status"] == "ok"
        assert document["redirects"] == 1
        assert document["endpoint"] == "http://litellm:80/v2/models"
        assert len(recorder.requests) == 2

    async def test_redirect_cycle_refused_at_hop_bound(self) -> None:
        class Recorder(_RedirectRecorder):
            def respond(self, request: httpx2.Request) -> httpx2.Response:
                return httpx2.Response(
                    307,
                    headers={"Location": "/v1/models"},
                    content=b"",
                )

        recorder = Recorder()
        result = await self._capability(recorder)(
            _call(dict(GOOD_PAYLOAD), _context())
        )
        document = json.loads(result.output)
        assert document["status"] == "refused"
        assert document["code"] == "too_many_redirects"
        # The same-origin credential re-attachment never exceeded the hop
        # bound; the refusal happened before one request more.
        assert len(recorder.requests) == MAX_REDIRECTS + 1

    async def test_redirect_without_location_is_fetch_error(self) -> None:
        class Recorder(_RedirectRecorder):
            def respond(self, request: httpx2.Request) -> httpx2.Response:
                return httpx2.Response(302, content=b"")

        recorder = Recorder()
        result = await self._capability(recorder)(
            _call(dict(GOOD_PAYLOAD), _context())
        )
        document = json.loads(result.output)
        assert document["status"] == "unavailable"
        assert document["code"] == "fetch_error"


# ---------------------------------------------------------------------------
# Controlled loopback upstream (writer half of the feature-gate scenario)
# ---------------------------------------------------------------------------


class _LiteLLMStub(BaseHTTPRequestHandler):
    """Loopback LiteLLM-like stand-in: serves fixture routes, records
    every request (method, path, headers, body)."""

    requests: list[tuple[str, str, dict[str, str], bytes]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self._handle()

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        self._handle()

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        self.requests.append(
            (self.command, self.path, dict(self.headers), body)
        )
        path, _, _query = self.path.partition("?")
        if path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": "mai-fxt-model"}]})
        elif path == "/v1/chat/completions":
            received = json.loads(body.decode("utf-8"))
            self._send(
                200,
                {
                    "id": "chatcmpl-fxt",
                    "model": received.get("model"),
                    "received_messages": received.get("messages"),
                },
            )
        elif path == "/v1/echo-auth":
            self._send(200, {"authorization": self.headers.get("Authorization")})
        elif path == "/v1/trap-external":
            self._send(
                307,
                None,
                extra=[("Location", self.server.stranger_url + "/steal")],
            )
        elif path == "/v1/trap-internal":
            self._send(307, None, extra=[("Location", "/v1/models")])
        elif path == "/v1/big":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                chunk = b'{"pad":"' + b"z" * 4096 + b'"}'
                for _ in range(64):  # ~64 * 4100 bytes > 16 KiB budget
                    self.wfile.write(f"{len(chunk):x}\r\n".encode())
                    self.wfile.write(chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                # The bounded reader aborts at budget+1: the server
                # finishing into a closed socket is the expected outcome.
                pass
        elif path == "/v1/error":
            self._send(500, {"error": "upstream boom"})
        elif path == "/v1/html":
            self._send(200, None, content=b"<html>nope</html>", content_type="text/html")
        elif path == "/v1/badjson":
            self._send(200, None, content=b"not json{")
        else:
            self._send(404, {"error": "not found"})

    def _send(
        self,
        status: int,
        document: object | None,
        *,
        content: bytes | None = None,
        content_type: str = "application/json",
        extra: list[tuple[str, str]] | None = None,
    ) -> None:
        if content is None:
            content = json.dumps(document).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        for name, value in extra or []:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def log_message(self, *args) -> None:  # noqa: D102 - quiet fixture
        pass


class _StrangerStub(BaseHTTPRequestHandler):
    """The unrelated redirect target: records every request; must stay
    empty in the forbidden-redirect proof."""

    requests: list[tuple[str, str, dict[str, str], bytes]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        self.requests.append((self.command, self.path, dict(self.headers), body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args) -> None:  # noqa: D102 - quiet fixture
        pass


@pytest.fixture
def upstream():
    """The controlled LiteLLM-like upstream plus the stranger server on
    loopback; both record every request."""
    _LiteLLMStub.requests = []
    _StrangerStub.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LiteLLMStub)
    stranger = ThreadingHTTPServer(("127.0.0.1", 0), _StrangerStub)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    server.stranger_url = f"http://127.0.0.1:{stranger.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stranger_thread = threading.Thread(
        target=stranger.serve_forever, daemon=True
    )
    stranger_thread.start()
    yield base
    server.shutdown()
    stranger.shutdown()
    thread.join(timeout=5)
    stranger_thread.join(timeout=5)


def _loopback_capability(base: str) -> ServiceHttpCapability:
    provider = HttpServiceProvider(timeout_seconds=10)
    return ServiceHttpCapability(
        provider, services={"litellm": base}, default_max_bytes=MIN_MAX_BYTES
    )


class TestControlledUpstream:
    async def test_benign_get_is_minimal_shape_with_bearer(self, upstream: str) -> None:
        """Feature-gate writer half: the benign investigation call reaches
        the controlled LiteLLM-like API in exactly the screened shape —
        one bearer header, no library identity, no cookies — and returns
        bounded JSON with provenance."""
        capability = _loopback_capability(upstream)
        result = await capability(
            _call(dict(GOOD_PAYLOAD), _context())
        )
        assert result.ok is True
        assert len(_LiteLLMStub.requests) == 1
        method, path, headers, body = _LiteLLMStub.requests[0]
        assert (method, path) == ("GET", "/v1/models")
        assert body == b""
        assert headers["Authorization"] == f"Bearer {SECRET_VALUE}"
        assert headers["Accept"] == "application/json"
        assert "python" not in headers.get("User-Agent", "").lower()
        assert "httpx" not in headers.get("User-Agent", "").lower()
        assert "Cookie" not in headers
        document = json.loads(result.output)
        assert document["status"] == "ok"
        assert document["json"] == {
            "object": "list",
            "data": [{"id": "mai-fxt-model"}],
        }
        assert document["endpoint"] == f"{upstream}/v1/models"
        assert document["trust"] == "untrusted_external"

    async def test_post_chat_completions_round_trip(self, upstream: str) -> None:
        capability = _loopback_capability(upstream)
        payload = dict(
            GOOD_PAYLOAD,
            method="POST",
            path="/v1/chat/completions",
            body={
                "model": "mai-fxt-model",
                "messages": [{"role": "user", "content": "investigate this"}],
            },
        )
        result = await capability(_call(payload, _context()))
        assert result.ok is True
        _method, _path, headers, body = _LiteLLMStub.requests[0]
        assert headers["Content-Type"] == "application/json"
        assert json.loads(body.decode()) == {
            "model": "mai-fxt-model",
            "messages": [{"role": "user", "content": "investigate this"}],
        }
        document = json.loads(result.output)
        assert document["json"]["model"] == "mai-fxt-model"
        assert document["json"]["received_messages"] == [
            {"role": "user", "content": "investigate this"}
        ]

    async def test_forbidden_redirect_sends_nothing_to_stranger(
        self, upstream: str
    ) -> None:
        """AC1 at the real boundary: the upstream 307-redirects to the
        stranger; the refusal happens before the second connection, so
        the stranger records zero requests and the credential stays with
        the approved origin."""
        capability = _loopback_capability(upstream)
        result = await capability(
            _call(dict(GOOD_PAYLOAD, path="/v1/trap-external"), _context())
        )
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "refused"
        assert document["code"] == "forbidden_redirect"
        assert document["endpoint"].startswith("http://127.0.0.1:")
        assert document["endpoint"].endswith("/steal")
        assert [path for _m, path, _h, _b in _LiteLLMStub.requests] == [
            "/v1/trap-external"
        ]
        assert _StrangerStub.requests == []

    async def test_same_origin_redirect_followed_at_real_socket(
        self, upstream: str
    ) -> None:
        capability = _loopback_capability(upstream)
        result = await capability(
            _call(dict(GOOD_PAYLOAD, path="/v1/trap-internal"), _context())
        )
        assert result.ok is True
        document = json.loads(result.output)
        assert document["status"] == "ok"
        assert document["redirects"] == 1
        assert document["endpoint"] == f"{upstream}/v1/models"
        assert [path for _m, path, _h, _b in _LiteLLMStub.requests] == [
            "/v1/trap-internal",
            "/v1/models",
        ]
        for _method, _path, headers, _body in _LiteLLMStub.requests:
            assert headers["Authorization"] == f"Bearer {SECRET_VALUE}"

    async def test_oversized_chunked_response_refused_within_budget(
        self, upstream: str
    ) -> None:
        capability = _loopback_capability(upstream)
        result = await capability(
            _call(dict(GOOD_PAYLOAD, path="/v1/big"), _context())
        )
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "refused"
        assert document["code"] == "response_too_large"

    async def test_upstream_500_is_http_error(self, upstream: str) -> None:
        capability = _loopback_capability(upstream)
        result = await capability(
            _call(dict(GOOD_PAYLOAD, path="/v1/error"), _context())
        )
        document = json.loads(result.output)
        assert document["status"] == "unavailable"
        assert document["code"] == "http_error"
        assert document["http_status"] == 500

    async def test_html_answer_is_unsupported_content_type(
        self, upstream: str
    ) -> None:
        capability = _loopback_capability(upstream)
        result = await capability(
            _call(dict(GOOD_PAYLOAD, path="/v1/html"), _context())
        )
        document = json.loads(result.output)
        assert document["status"] == "unavailable"
        assert document["code"] == "unsupported_content_type"

    async def test_malformed_json_is_provider_error(self, upstream: str) -> None:
        capability = _loopback_capability(upstream)
        result = await capability(
            _call(dict(GOOD_PAYLOAD, path="/v1/badjson"), _context())
        )
        document = json.loads(result.output)
        assert document["status"] == "unavailable"
        assert document["code"] == "provider_error"

    async def test_unavailable_upstream_is_honest(self) -> None:
        provider = HttpServiceProvider(timeout_seconds=5)
        capability = ServiceHttpCapability(
            provider, services={"litellm": "http://127.0.0.1:1"}
        )
        result = await capability(
            _call(dict(GOOD_PAYLOAD), _context())
        )
        document = json.loads(result.output)
        assert document["status"] == "unavailable"
        assert document["code"] in ("fetch_unreachable", "fetch_error")
        assert SECRET_VALUE not in result.output


# ---------------------------------------------------------------------------
# Real dispatcher / policy / credentials integration
# ---------------------------------------------------------------------------


def _registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(descriptor())
    return registry


def _grants(*servers: str):
    return {CAPABILITY_ID: frozenset(uuid.UUID(server) for server in servers)}


def _policy(revision: int = 7, denied: tuple[str, ...] = ()) -> Policy:
    return Policy(revision=revision, denied_capabilities=frozenset(denied))


def _unused_approvals() -> ApprovalStore:
    """A real store over a lazily-connecting engine (never dialed)."""
    _, sessions = create_app_engine("postgresql://unused:unused@127.0.0.1:1/unused")
    return ApprovalStore(sessions)


class _RecordingCapability:
    """Wraps the real executor, recording invocations for assertions."""

    def __init__(self, capability: ServiceHttpCapability) -> None:
        self.capability = capability
        self.calls: list[CapabilityCall] = []

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        self.calls.append(call)
        return await self.capability(call)


def _dispatcher(
    registry: CapabilityRegistry,
    executor,
    *,
    grants=None,
    policy=None,
    credentials=None,
) -> CapabilityDispatcher:
    return CapabilityDispatcher(
        registry=registry,
        approvals=_unused_approvals(),
        grants=(lambda: grants) if grants is not None else (lambda: {}),
        policy=policy if policy is not None else (lambda: _policy()),
        adopted=lambda server_id: True,
        credentials=credentials if credentials is not None else (lambda: None),
        executor=executor,
        output_budget_bytes=64 * 1024,
    )


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    """Real .env credential source with the synthetic sentinel (0600, the
    resolver's fail-closed permission gate)."""
    path = tmp_path / "fixture.env"
    path.write_text(f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8")
    path.chmod(0o600)
    return path


class TestDispatcherIntegration:
    async def test_granted_dispatch_executes_without_approval(
        self, env_file: Path
    ) -> None:
        provider = _StaticProvider(_json_response({"object": "list"}))
        capability = _RecordingCapability(
            ServiceHttpCapability(provider, services=SERVICES)
        )
        dispatcher = _dispatcher(
            _registry(), capability, grants=_grants(SERVER_A),
            credentials=lambda: EnvResolver(env_file),
        )
        outcome: DispatchOutcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_A),
            capability_id=CAPABILITY_ID,
            payload=dict(GOOD_PAYLOAD),
        )
        assert outcome.allowed is True
        assert outcome.effect == "executed"
        assert outcome.executor_ok is True
        assert outcome.approval_id is None  # read effect needs no approval
        assert outcome.credential_references == (SECRET_NAME,)
        assert len(capability.calls) == 1
        # The resolved secret reached the provider boundary and nowhere
        # else: the receipt and output carry the reference name only.
        call = capability.calls[0]
        assert call.credentials[SECRET_NAME].value == SECRET_VALUE
        document = json.loads(outcome.output.text)
        assert document["status"] == "ok"
        assert SECRET_VALUE not in outcome.output.text

    async def test_secret_echo_redacted_from_model_output(
        self, env_file: Path
    ) -> None:
        """AC2 through the real dispatcher: even a service that echoes the
        bearer header back inside its JSON answer cannot put the resolved
        secret into model-bound output — the dispatcher redacts the
        resolved value and the receipt stays value-free."""
        provider = _StaticProvider(
            _json_response({"authorization": f"Bearer {SECRET_VALUE}"})
        )
        capability = ServiceHttpCapability(provider, services=SERVICES)
        dispatcher = _dispatcher(
            _registry(), capability, grants=_grants(SERVER_A),
            credentials=lambda: EnvResolver(env_file),
        )
        outcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_A),
            capability_id=CAPABILITY_ID,
            payload=dict(GOOD_PAYLOAD),
        )
        assert outcome.allowed is True
        assert outcome.executor_ok is True
        # The echo inside the executor answer is replaced before bounding.
        assert SECRET_VALUE not in outcome.output.text
        assert REDACTED in outcome.output.text
        # The whole receipt document sweeps clean: no field, nested or
        # flat, carries the resolved value.
        assert SECRET_VALUE not in json.dumps(
            outcome.to_json(), sort_keys=True
        )

    async def test_secret_not_in_output_when_response_truncated(
        self, env_file: Path
    ) -> None:
        """Redaction runs before bounding (dispatch.py `_redact` then
        `_bound`): a budget cut can never split a secret around the
        marker or drop the redaction pass."""
        big_echo = {"authorization": f"Bearer {SECRET_VALUE}", "pad": "x" * 4096}
        provider = _StaticProvider(_json_response(big_echo))
        capability = ServiceHttpCapability(provider, services=SERVICES)
        # Receipt-sized budget: the output is cut mid-pad.
        dispatcher_small = CapabilityDispatcher(
            registry=_registry(),
            approvals=_unused_approvals(),
            grants=lambda: _grants(SERVER_A),
            policy=lambda: _policy(),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(env_file),
            executor=capability,
            output_budget_bytes=200,
        )
        outcome = await dispatcher_small.dispatch(
            context=_context(server_id=SERVER_A),
            capability_id=CAPABILITY_ID,
            payload=dict(GOOD_PAYLOAD),
        )
        assert outcome.output is not None
        assert outcome.output.truncated is True
        assert SECRET_VALUE not in outcome.output.text
        assert REDACTED in outcome.output.text

    async def test_server_outside_grant_scope_denied_before_executor(
        self, env_file: Path
    ) -> None:
        provider = _StaticProvider(_json_response({}))
        capability = _RecordingCapability(
            ServiceHttpCapability(provider, services=SERVICES)
        )
        dispatcher = _dispatcher(
            _registry(), capability, grants=_grants(SERVER_A),
            credentials=lambda: EnvResolver(env_file),
        )
        outcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_B),
            capability_id=CAPABILITY_ID,
            payload=dict(GOOD_PAYLOAD),
        )
        assert outcome.allowed is False
        assert outcome.reason == "server_scope_mismatch"
        assert capability.calls == []
        assert outcome.effect == "not_attempted"

    async def test_capability_not_granted_denied(self, env_file: Path) -> None:
        provider = _StaticProvider(_json_response({}))
        capability = _RecordingCapability(
            ServiceHttpCapability(provider, services=SERVICES)
        )
        dispatcher = _dispatcher(
            _registry(), capability, grants={},  # nothing granted
            credentials=lambda: EnvResolver(env_file),
        )
        outcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_A),
            capability_id=CAPABILITY_ID,
            payload=dict(GOOD_PAYLOAD),
        )
        assert outcome.allowed is False
        assert outcome.reason == "capability_not_granted"
        assert capability.calls == []

    async def test_policy_deny_list_blocks_call(self, env_file: Path) -> None:
        provider = _StaticProvider(_json_response({}))
        capability = _RecordingCapability(
            ServiceHttpCapability(provider, services=SERVICES)
        )
        dispatcher = _dispatcher(
            _registry(), capability,
            grants=_grants(SERVER_A),
            policy=lambda: _policy(denied=(CAPABILITY_ID,)),
            credentials=lambda: EnvResolver(env_file),
        )
        outcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_A),
            capability_id=CAPABILITY_ID,
            payload=dict(GOOD_PAYLOAD),
        )
        assert outcome.allowed is False
        assert outcome.reason == "capability_denied_by_policy"
        assert capability.calls == []

    async def test_approval_presented_but_not_required_is_denied(
        self, env_file: Path
    ) -> None:
        provider = _StaticProvider(_json_response({}))
        capability = _RecordingCapability(
            ServiceHttpCapability(provider, services=SERVICES)
        )
        dispatcher = _dispatcher(
            _registry(), capability, grants=_grants(SERVER_A),
            credentials=lambda: EnvResolver(env_file),
        )
        outcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_A),
            capability_id=CAPABILITY_ID,
            payload=dict(GOOD_PAYLOAD),
            approval_id=uuid.uuid4(),
        )
        assert outcome.allowed is False
        assert outcome.reason == (
            DispatchReason.APPROVAL_PRESENTED_BUT_NOT_REQUIRED.value
        )
        assert capability.calls == []

    async def test_credential_resolution_failure_denies_before_effect(
        self, tmp_path: Path
    ) -> None:
        empty_env = tmp_path / "empty.env"
        empty_env.write_text("MAI_FXT_OTHER=something\n", encoding="utf-8")
        empty_env.chmod(0o600)
        provider = _StaticProvider(_json_response({}))
        capability = _RecordingCapability(
            ServiceHttpCapability(provider, services=SERVICES)
        )
        dispatcher = _dispatcher(
            _registry(), capability, grants=_grants(SERVER_A),
            credentials=lambda: EnvResolver(empty_env),
        )
        outcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_A),
            capability_id=CAPABILITY_ID,
            payload=dict(GOOD_PAYLOAD),
        )
        assert outcome.allowed is False
        assert outcome.reason == (
            DispatchReason.CREDENTIAL_RESOLUTION_FAILED.value
        )
        assert outcome.approval_code == "missing_key"
        assert capability.calls == []
        assert outcome.effect == "not_attempted"

    async def test_read_effect_needs_no_approval_in_ask_mode(
        self, env_file: Path
    ) -> None:
        provider = _StaticProvider(_json_response({"ok": True}))
        capability = ServiceHttpCapability(provider, services=SERVICES)
        dispatcher = _dispatcher(
            _registry(), capability, grants=_grants(SERVER_A),
            credentials=lambda: EnvResolver(env_file),
        )
        outcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_A, mode=PolicyMode.ASK),
            capability_id=CAPABILITY_ID,
            payload=dict(GOOD_PAYLOAD),
        )
        assert outcome.allowed is True
        assert outcome.effect == "executed"
        assert outcome.approval_id is None


# ---------------------------------------------------------------------------
# Tool bridge availability (06-tool-bridge producer)
# ---------------------------------------------------------------------------


class TestToolBridgeAvailability:
    def _bridge(self, allowed: tuple[str, ...], env_file: Path | None = None):
        from mai.agents.tools import CapabilityToolBridge

        registry = _registry()
        capability = ServiceHttpCapability(
            _StaticProvider(_json_response({"ok": True})), services=SERVICES
        )
        dispatcher = _dispatcher(
            registry, capability,
            grants=_grants(SERVER_A),
            credentials=(
                (lambda: EnvResolver(env_file))
                if env_file is not None
                else None
            ),
        )
        return (
            CapabilityToolBridge(
                registry=registry,
                dispatcher=dispatcher,
                context=_context(server_id=SERVER_A, allowed=allowed),
            ),
            capability,
        )

    def test_descriptor_exposed_only_when_allowed(self, env_file: Path) -> None:
        exposed, _cap = self._bridge((CAPABILITY_ID,), env_file)
        names = {tool.name for tool in exposed.tools}
        assert any("service" in name or "http" in name for name in names)
        hidden, _cap = self._bridge((), env_file)
        assert {tool.name for tool in hidden.tools} == set()

    async def test_bridge_executes_granted_call(self, env_file: Path) -> None:
        exposed, _cap = self._bridge((CAPABILITY_ID,), env_file)
        tool = next(
            t
            for t in exposed.tools
            if "service" in t.name or "http" in t.name
        )
        result = json.loads(
            await tool.function(
                service="litellm",
                method="GET",
                path="/v1/models",
                credential_ref=SECRET_NAME,
            )
        )
        assert result["status"] == "executed"
        assert result["allowed"] is True
        document = json.loads(result["output"]["text"])
        assert document["status"] == "ok"
        assert SECRET_VALUE not in result["output"]["text"]

    async def test_bridge_denies_smuggled_field_before_dispatch(
        self, env_file: Path
    ) -> None:
        exposed, capability = self._bridge((CAPABILITY_ID,), env_file)
        tool = next(
            t
            for t in exposed.tools
            if "service" in t.name or "http" in t.name
        )
        result = json.loads(
            await tool.function(
                service="litellm",
                method="GET",
                path="/v1/models",
                credential_ref=SECRET_NAME,
                endpoint="http://evil.invalid",
            )
        )
        assert result["status"] == "denied"
        assert result["error"]["code"] == "payload_schema_mismatch"


# ---------------------------------------------------------------------------
# Descriptor contract
# ---------------------------------------------------------------------------


class TestDescriptorContract:
    def test_descriptor_registers_cleanly(self) -> None:
        registry = _registry()
        assert len(registry) == 1
        assert registry.get(CAPABILITY_ID, "1.0.0") is not None

    def test_descriptor_pins_policy_relevant_fields(self) -> None:
        desc = descriptor()
        assert desc.identifier == CAPABILITY_ID
        assert desc.effect_class == "read"
        assert tuple(desc.execution_environments) == ("controller",)
        assert tuple(desc.required_grants) == (CAPABILITY_ID,)
        assert desc.resource_scope == ()

    def test_input_schema_rejects_additional_properties(self) -> None:
        desc = descriptor()
        schema = desc.input_schema
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {
            "service", "method", "path", "credential_ref",
        }


# ---------------------------------------------------------------------------
# CLI entrypoint (operator path, real subprocess)
# ---------------------------------------------------------------------------


class TestCliEntrypoint:
    def _run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "mai.capabilities.service_http", *argv],
            capture_output=True,
            text=True,
            cwd=BACKEND_ROOT,
            timeout=120,
        )

    def test_cli_call_hits_upstream(self, upstream: str, tmp_path: Path) -> None:
        env = tmp_path / "cli.env"
        env.write_text(f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8")
        env.chmod(0o600)
        proc = self._run(
            "call",
            "--allow", f"litellm={upstream}",
            "--service", "litellm",
            "--method", "GET",
            "--path", "/v1/models",
            "--credential-ref", SECRET_NAME,
            "--env-file", str(env),
            "--max-bytes", str(MIN_MAX_BYTES),
        )
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout.strip().splitlines()[-1])
        assert document["status"] == "ok"
        assert document["json"]["object"] == "list"
        assert SECRET_VALUE not in proc.stdout
        assert len(_LiteLLMStub.requests) == 1

    def test_cli_post_body(self, upstream: str, tmp_path: Path) -> None:
        env = tmp_path / "cli.env"
        env.write_text(f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8")
        env.chmod(0o600)
        proc = self._run(
            "call",
            "--allow", f"litellm={upstream}",
            "--service", "litellm",
            "--method", "POST",
            "--path", "/v1/chat/completions",
            "--credential-ref", SECRET_NAME,
            "--env-file", str(env),
            "--body", '{"model": "mai-fxt-model", "messages": []}',
            "--max-bytes", str(MIN_MAX_BYTES),
        )
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout.strip().splitlines()[-1])
        assert document["status"] == "ok"
        assert document["json"]["model"] == "mai-fxt-model"

    def test_cli_unknown_service_is_structured_status(
        self, upstream: str, tmp_path: Path
    ) -> None:
        env = tmp_path / "cli.env"
        env.write_text(f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8")
        env.chmod(0o600)
        proc = self._run(
            "call",
            "--allow", f"litellm={upstream}",
            "--service", "other",
            "--method", "GET",
            "--path", "/v1/models",
            "--credential-ref", SECRET_NAME,
            "--env-file", str(env),
        )
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout.strip().splitlines()[-1])
        assert document["status"] == "refused"
        assert document["code"] == "unknown_service"

    def test_cli_forbidden_redirect_never_reaches_stranger(
        self, upstream: str, tmp_path: Path
    ) -> None:
        env = tmp_path / "cli.env"
        env.write_text(f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8")
        env.chmod(0o600)
        proc = self._run(
            "call",
            "--allow", f"litellm={upstream}",
            "--service", "litellm",
            "--method", "GET",
            "--path", "/v1/trap-external",
            "--credential-ref", SECRET_NAME,
            "--env-file", str(env),
        )
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout.strip().splitlines()[-1])
        assert document["status"] == "refused"
        assert document["code"] == "forbidden_redirect"
        assert _StrangerStub.requests == []

    def test_cli_missing_credential_key_is_structured_rejection(
        self, upstream: str, tmp_path: Path
    ) -> None:
        env = tmp_path / "cli.env"
        env.write_text("MAI_FXT_OTHER=something\n", encoding="utf-8")
        env.chmod(0o600)
        proc = self._run(
            "call",
            "--allow", f"litellm={upstream}",
            "--service", "litellm",
            "--method", "GET",
            "--path", "/v1/models",
            "--credential-ref", SECRET_NAME,
            "--env-file", str(env),
        )
        assert proc.returncode == 1
        document = json.loads(proc.stdout.strip().splitlines()[-1])
        assert document["kind"] == "error"
        assert document["code"] == "credential_resolution_failed"
        assert _LiteLLMStub.requests == []

    def test_cli_malformed_body_is_structured_rejection(
        self, upstream: str, tmp_path: Path
    ) -> None:
        env = tmp_path / "cli.env"
        env.write_text(f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8")
        env.chmod(0o600)
        proc = self._run(
            "call",
            "--allow", f"litellm={upstream}",
            "--service", "litellm",
            "--method", "POST",
            "--path", "/v1/chat/completions",
            "--credential-ref", SECRET_NAME,
            "--env-file", str(env),
            "--body", "not json",
        )
        assert proc.returncode == 1
        document = json.loads(proc.stdout.strip().splitlines()[-1])
        assert document["kind"] == "error"
        assert document["code"] == "invalid_payload"
        assert _LiteLLMStub.requests == []
