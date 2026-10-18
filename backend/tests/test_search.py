"""Focused tests for sanitized web search with structured provenance
(card 07-search-capability).

Acceptance (BRIEF):

1. Bounded queries/results and sanitized outbound context, timeout/provider
   error classified.
2. Search descriptors available to scoped subagents; snippets remain
   untrusted sourced data.

Boundary discipline (test policy, risk-critical slice): payload validation,
the secret-marker screen, citation normalization and result parsing run
pure; outbound-request shape and HTTP/timeout/connect classification run
through the real httpx2 client machinery over an explicit ``MockTransport``
(real client boundary, deterministic upstream); the end-to-end
capability/dispatcher/tool integration runs against the real
``03-capability-registry``, ``03-dispatcher``, ``03-policy`` and
``06-tool-bridge`` producers with the ``web.search`` descriptor registered.
The controlled-upstream fixture (a loopback HTTP server recording every
request) proves the writer half of the feature-gate scenario: a benign
query reaches the upstream in exactly the screened shape, and a
synthetic sensitive-marker query refuses before any I/O — the upstream
captures nothing (not raw log forwarding). The assembled live journey
against the bundled SearXNG compose service is owned by ``07-verify``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import uuid
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mai.capabilities.dispatch import (
    CapabilityCall,
    CapabilityDispatcher,
    DispatchOutcome,
    ExecutorResult,
)
from mai.database import create_app_engine
from mai.plugins.registry import CapabilityRegistry
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import ExecutionContext, Policy, PolicyMode
from mai.research.search import (
    CAPABILITY_ID,
    DEFAULT_LIMIT,
    DEFAULT_MAX_SNIPPET_CHARS,
    MAX_LIMIT,
    MAX_QUERY_CHARS,
    MAX_SNIPPET_CHARS,
    MIN_SNIPPET_CHARS,
    SearchError,
    SearchErrorCode,
    SearchStatus,
    SearXNGProvider,
    WebSearchCapability,
    descriptor,
    normalize_citation_url,
    parse_provider_document,
    request_from_payload,
    result_document,
    sensitive_marker_reason,
)

BACKEND_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.anyio

# Frozen fixture identities (canonical lowercase UUID strings, TEST-NET style).
SERVER_A = "5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60"
SERVER_B = "8f1c2e3d-4a5b-4c6d-9e7f-0a1b2c3d4e5f"
RUN_1 = "6e7f8a9b-0c1d-4e2f-8a3b-4c5d6e7f8a90"
PRINCIPAL = "tester@mai.local"

#: A synthetic marker used by the screen tests; deliberately NOT a real
#: credential anywhere.
SYNTHETIC_SECRET = "sk-fxt07AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


# ---------------------------------------------------------------------------
# Pure seam: request validation
# ---------------------------------------------------------------------------


class TestRequestValidation:
    def test_defaults_applied(self) -> None:
        request = request_from_payload({"query": "postgres replication"})
        assert request.query == "postgres replication"
        assert request.limit == DEFAULT_LIMIT
        assert request.max_snippet_chars == DEFAULT_MAX_SNIPPET_CHARS

    def test_non_mapping_payload_rejected(self) -> None:
        with pytest.raises(SearchError) as excinfo:
            request_from_payload(["query"])  # type: ignore[arg-type]
        assert excinfo.value.code is SearchErrorCode.INVALID_PAYLOAD

    @pytest.mark.parametrize(
        "field", ["server_id", "endpoint", "engine", "headers"]
    )
    def test_smuggled_field_rejected(self, field: str) -> None:
        payload = {"query": "x", field: SERVER_B}
        with pytest.raises(SearchError) as excinfo:
            request_from_payload(payload)
        assert excinfo.value.code is SearchErrorCode.INVALID_PAYLOAD
        assert field in str(excinfo.value)

    @pytest.mark.parametrize(
        "query", ["", "   ", "x" * (MAX_QUERY_CHARS + 1), "bad\x00query"]
    )
    def test_bad_query_rejected(self, query: str) -> None:
        with pytest.raises(SearchError) as excinfo:
            request_from_payload({"query": query})
        assert excinfo.value.code is SearchErrorCode.INVALID_PAYLOAD

    @pytest.mark.parametrize("limit", [0, MAX_LIMIT + 1, True, 1.5, "3"])
    def test_bad_limit_rejected(self, limit: object) -> None:
        with pytest.raises(SearchError) as excinfo:
            request_from_payload({"query": "x", "limit": limit})
        assert excinfo.value.code is SearchErrorCode.INVALID_PAYLOAD

    @pytest.mark.parametrize(
        "budget", [MIN_SNIPPET_CHARS - 1, MAX_SNIPPET_CHARS + 1, False, "200"]
    )
    def test_bad_snippet_budget_rejected(self, budget: object) -> None:
        with pytest.raises(SearchError) as excinfo:
            request_from_payload({"query": "x", "max_snippet_chars": budget})
        assert excinfo.value.code is SearchErrorCode.INVALID_PAYLOAD

    def test_query_whitespace_is_stripped(self) -> None:
        request = request_from_payload({"query": "  padded query  "})
        assert request.query == "padded query"


# ---------------------------------------------------------------------------
# Pure seam: the secret-marker screen (sanitized outbound context)
# ---------------------------------------------------------------------------


class TestSensitiveMarkerScreen:
    @pytest.mark.parametrize(
        "query",
        [
            f"rotate api_key={SYNTHETIC_SECRET}",
            f"check password: {SYNTHETIC_SECRET}",
            f"token={SYNTHETIC_SECRET} expiry",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nfxt-not-a-key",
            "header payload "
            + "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJtYWktdGVzdCJ9."
            + "fakesigfakesigfakesig",
            f"aws key AKIAIOSFODNN7EXAMPLE in config {SYNTHETIC_SECRET}",
            f"Authorization: Bearer {SYNTHETIC_SECRET}",
        ],
        ids=[
            "api_key_assignment",
            "password_assignment",
            "token_assignment",
            "private_key_block",
            "jwt_compact",
            "cloud_access_key",
            "bearer_token",
        ],
    )
    def test_marker_class_refused(self, query: str) -> None:
        assert sensitive_marker_reason(query) is not None

    @pytest.mark.parametrize(
        "query",
        [
            "how to rotate the password on ubuntu",  # prose, no assignment
            "jwt token expiry handling",  # words, no token material
            "AKIA what the prefix means",  # prefix alone, no 16-char suffix
            "difference between token and session",
            "-----BEGIN CERTIFICATE----- chain order",  # not a private key
            "set the api key in your client config",  # no := value
        ],
    )
    def test_benign_prose_passes(self, query: str) -> None:
        assert sensitive_marker_reason(query) is None

    def test_reason_names_class_never_value(self) -> None:
        reason = sensitive_marker_reason(f"api_key={SYNTHETIC_SECRET}")
        assert reason == "credential_assignment"
        exc = SearchError(SearchErrorCode.SENSITIVE_QUERY, f"({reason})")
        assert SYNTHETIC_SECRET not in str(exc)


# ---------------------------------------------------------------------------
# Pure seam: citation URL normalization (normalized provenance)
# ---------------------------------------------------------------------------


class TestCitationNormalization:
    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            (
                "HTTPS://Example.COM:443/a/path?utm_source=x&b=2#frag",
                "https://example.com/a/path?b=2",
            ),
            (
                "http://example.com:8080/page?gclid=1&x=%C3%A4",
                "http://example.com:8080/page?x=%C3%A4",
            ),
            ("https://example.com", "https://example.com"),
            (
                "https://sub.example.co.uk/a?FBCLID=abc&Utm_Medium=m&k=v",
                "https://sub.example.co.uk/a?k=v",
            ),
        ],
    )
    def test_normalized(self, raw: str, want: str) -> None:
        assert normalize_citation_url(raw) == want

    @pytest.mark.parametrize(
        "raw",
        ["", "not a url", "ftp://example.com/x", "javascript:alert(1)", None, 42],
    )
    def test_not_citable(self, raw: object) -> None:
        assert normalize_citation_url(raw) is None


# ---------------------------------------------------------------------------
# Pure seam: result parsing (bounded, classified, provenance-cited)
# ---------------------------------------------------------------------------

_PROVIDER_DOC: Mapping[str, object] = {
    "query": "postgres replication",
    "number_of_results": 2,
    "results": [
        {
            "url": "HTTPS://Docs.Postgres.QL.ORG:443/replication?utm_source=x",
            "title": "PostgreSQL Replication",
            "content": "Streaming replication basics. " + "x" * 900,
            "engine": "duckduckgo",
            "engines": ["duckduckgo", "google"],
        },
        {
            "url": "https://wiki.postgresql.org/wiki/Replication",
            "title": "Wiki",
            "content": "short",
            "engines": "not-a-list",
        },
    ],
    "unresponsive_engines": ["bing", "brave"],
}


class TestResultParsing:
    def test_ok_document_bounded_and_cited(self) -> None:
        result = parse_provider_document(
            _PROVIDER_DOC,
            query="postgres replication",
            limit=5,
            max_snippet_chars=200,
        )
        assert result.status is SearchStatus.OK
        assert result.code is None
        assert len(result.results) == 2
        first = result.results[0]
        assert first.rank == 1
        # Normalized provenance: scheme/host lowered, default port, the
        # tracking parameter and fragment gone.
        assert first.url == "https://docs.postgres.ql.org/replication"
        assert first.truncated is True
        assert len(first.snippet) == 200
        assert first.engines == ("duckduckgo", "google")
        assert result.results[1].engines == ()
        assert result.unresponsive_engines == ("bing", "brave")

    def test_limit_bounds_hit_count(self) -> None:
        result = parse_provider_document(
            _PROVIDER_DOC, query="q", limit=1, max_snippet_chars=400
        )
        assert [hit.rank for hit in result.results] == [1]

    def test_zero_results_is_ok_empty(self) -> None:
        result = parse_provider_document(
            {"results": []}, query="q", limit=5, max_snippet_chars=400
        )
        assert result.status is SearchStatus.OK
        assert result.results == ()

    @pytest.mark.parametrize(
        "document",
        [
            {},
            {"results": "not-a-list"},
            {"results": None},
            ["not", "a", "mapping"],
        ],
    )
    def test_bad_document_is_provider_error(self, document: object) -> None:
        result = parse_provider_document(
            document,  # type: ignore[arg-type]
            query="q",
            limit=5,
            max_snippet_chars=400,
        )
        assert result.status is SearchStatus.UNAVAILABLE
        assert result.code is SearchErrorCode.PROVIDER_ERROR
        assert result.error_type is not None
        assert result.results == ()

    def test_non_object_item_is_provider_error(self) -> None:
        result = parse_provider_document(
            {"results": ["string-item"]},
            query="q",
            limit=5,
            max_snippet_chars=400,
        )
        assert result.status is SearchStatus.UNAVAILABLE
        assert result.code is SearchErrorCode.PROVIDER_ERROR

    def test_item_without_citable_url_is_skipped(self) -> None:
        result = parse_provider_document(
            {
                "results": [
                    {"title": "no url"},
                    {"url": "ftp://example.com/x"},
                    {"url": "https://ok.example/x", "title": "ok"},
                ]
            },
            query="q",
            limit=5,
            max_snippet_chars=400,
        )
        assert result.status is SearchStatus.OK
        assert [hit.title for hit in result.results] == ["ok"]

    def test_result_document_is_untrusted_sourced_data(self) -> None:
        result = parse_provider_document(
            _PROVIDER_DOC, query="q", limit=5, max_snippet_chars=400
        )
        document = result_document(result)
        assert document["trust"] == "untrusted_external"
        assert document["provider"] == "searxng"
        assert document["status"] == "ok"
        for hit in document["results"]:
            assert hit["citation"] == hit["url"]
            assert set(hit) >= {"rank", "title", "url", "citation",
                                "snippet", "truncated", "engines"}


# ---------------------------------------------------------------------------
# Real httpx2 client boundary (MockTransport): request shape + classification
# ---------------------------------------------------------------------------

import httpx2  # noqa: E402 - grouped with the transport-boundary seam


def _json_response(document: Mapping[str, object]) -> "httpx2.Response":
    return httpx2.Response(
        200,
        json=document,
        headers={"Content-Type": "application/json"},
    )


class TestProviderRequestBoundary:
    def test_outbound_request_is_minimal_and_non_identifying(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return _json_response({"results": []})

        provider = SearXNGProvider(
            "http://searxng.internal:8080", transport=httpx2.MockTransport(handler)
        )
        document = asyncio.run(provider.search(query="benign query"))
        assert document == {"results": []}
        assert len(seen) == 1
        request = seen[0]
        assert request.url.path == "/search"
        assert dict(request.url.params) == {"q": "benign query", "format": "json"}
        assert "python" not in request.headers["user-agent"].lower()
        assert "httpx" not in request.headers["user-agent"].lower()
        # No caller identity or authorization material is attached.
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers

    def test_query_is_urlencoded_on_the_wire(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return _json_response({"results": []})

        provider = SearXNGProvider(
            "http://searxng.internal:8080", transport=httpx2.MockTransport(handler)
        )
        asyncio.run(provider.search(query="q=a&b=c d"))
        # The query value is a single url-encoded parameter on the wire:
        # metacharacters of the value never become request structure.
        assert dict(seen[0].url.params) == {"q": "q=a&b=c d", "format": "json"}

    def test_http_error_status_classified_provider_error(self) -> None:
        # 403 is the documented upstream answer for a non-enabled format
        # (C07 rule 7) — it must classify, never crash.
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(403, text="forbidden")

        provider = SearXNGProvider(
            "http://searxng.internal:8080", transport=httpx2.MockTransport(handler)
        )
        with pytest.raises(httpx2.HTTPStatusError):
            asyncio.run(provider.search(query="q"))

    def test_timeout_classified(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.TimeoutException("simulated read timeout")

        provider = SearXNGProvider(
            "http://searxng.internal:8080", transport=httpx2.MockTransport(handler)
        )
        with pytest.raises(httpx2.TimeoutException):
            asyncio.run(provider.search(query="q"))

    def test_connect_failure_classified(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("simulated connect failure")

        provider = SearXNGProvider(
            "http://searxng.internal:8080", transport=httpx2.MockTransport(handler)
        )
        with pytest.raises(httpx2.ConnectError):
            asyncio.run(provider.search(query="q"))

    def test_malformed_json_classified(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                text="{not json",
                headers={"Content-Type": "application/json"},
            )

        provider = SearXNGProvider(
            "http://searxng.internal:8080", transport=httpx2.MockTransport(handler)
        )
        with pytest.raises(Exception) as excinfo:
            asyncio.run(provider.search(query="q"))
        assert excinfo.value.__class__.__name__ == "JSONDecodeError"

    def test_bad_base_url_rejected_at_construction(self) -> None:
        with pytest.raises(SearchError) as excinfo:
            SearXNGProvider("not a url")
        assert excinfo.value.code is SearchErrorCode.INVALID_PAYLOAD


# ---------------------------------------------------------------------------
# Executor: classification of provider failures (value-free, stable codes)
# ---------------------------------------------------------------------------


class _FlakyProvider:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    async def search(self, *, query: str) -> Mapping[str, object]:
        self.calls += 1
        raise self._exc


def _context(
    *,
    server_id: str = SERVER_A,
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
        credential_references=(),
        allowed_capabilities=allowed,
        mode=mode,
    )


def _call(payload: Mapping[str, object], context: ExecutionContext) -> CapabilityCall:
    return CapabilityCall(
        descriptor=descriptor(),
        payload=payload,
        context=context,
        credentials={},
        op_id=uuid.uuid4(),
    )


class _StaticProvider:
    def __init__(self, document: Mapping[str, object]) -> None:
        self._document = document
        self.queries: list[str] = []

    async def search(self, *, query: str) -> Mapping[str, object]:
        self.queries.append(query)
        return self._document


class TestExecutorClassification:
    async def test_timeout_becomes_provider_timeout(self) -> None:
        provider = _FlakyProvider(httpx2.TimeoutException("t"))
        capability = WebSearchCapability(provider)
        result = await capability(_call({"query": "q"}, _context()))
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "unavailable"
        assert document["code"] == "provider_timeout"
        assert document["error_type"] == "TimeoutException"
        assert document["results"] == []
        assert document["query"] == "q"

    async def test_connect_error_becomes_provider_unreachable(self) -> None:
        provider = _FlakyProvider(httpx2.ConnectError("c"))
        capability = WebSearchCapability(provider)
        result = await capability(_call({"query": "q"}, _context()))
        document = json.loads(result.output)
        assert document["code"] == "provider_unreachable"
        assert document["error_type"] == "ConnectError"

    async def test_http_status_error_becomes_provider_error(self) -> None:
        request = httpx2.Request("GET", "http://searxng.internal/search")
        provider = _FlakyProvider(
            httpx2.HTTPStatusError(
                "403", request=request, response=httpx2.Response(403, request=request)
            )
        )
        capability = WebSearchCapability(provider)
        result = await capability(_call({"query": "q"}, _context()))
        document = json.loads(result.output)
        assert document["code"] == "provider_error"
        assert document["error_type"] == "HTTPStatusError"

    async def test_unknown_provider_failure_is_provider_error(self) -> None:
        provider = _FlakyProvider(RuntimeError("anything"))
        capability = WebSearchCapability(provider)
        result = await capability(_call({"query": "q"}, _context()))
        document = json.loads(result.output)
        assert document["code"] == "provider_error"
        assert "anything" not in result.output  # value-free

    async def test_sensitive_query_never_touches_provider(self) -> None:
        provider = _StaticProvider({"results": []})
        capability = WebSearchCapability(provider)
        with pytest.raises(SearchError) as excinfo:
            await capability(
                _call({"query": f"leak api_key={SYNTHETIC_SECRET}"}, _context())
            )
        assert excinfo.value.code is SearchErrorCode.SENSITIVE_QUERY
        assert provider.queries == []  # nothing left the process

    async def test_ok_result_round_trips(self) -> None:
        provider = _StaticProvider(_PROVIDER_DOC)
        capability = WebSearchCapability(provider)
        result = await capability(
            _call({"query": "postgres replication", "limit": 2}, _context())
        )
        assert result.ok is True
        document = json.loads(result.output)
        assert document["status"] == "ok"
        assert len(document["results"]) == 2
        assert provider.queries == ["postgres replication"]

    async def test_direct_call_validates_inputs(self) -> None:
        capability = WebSearchCapability(_StaticProvider({"results": []}))
        with pytest.raises(SearchError) as excinfo:
            await capability("not-a-call")  # type: ignore[arg-type]
        assert excinfo.value.code is SearchErrorCode.INVALID_PAYLOAD
        call = _call({"query": ""}, _context())
        with pytest.raises(SearchError) as excinfo:
            await capability(call)
        assert excinfo.value.code is SearchErrorCode.INVALID_PAYLOAD


# ---------------------------------------------------------------------------
# Controlled upstream fixture: the feature-gate writer half (real HTTP)
# ---------------------------------------------------------------------------


class _UpstreamHandler(BaseHTTPRequestHandler):
    """Loopback SearXNG stand-in: serves one fixed document, records all
    requests (the controlled upstream of the feature-gate scenario)."""

    document: Mapping[str, object] = {"results": []}
    requests: list[tuple[str, dict[str, str]]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        path, _, query = self.path.partition("?")
        self.requests.append((path, {"query": query}))
        payload = json.dumps(self.document).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # noqa: D102 - quiet fixture
        pass


@pytest.fixture
def upstream():
    """The controlled upstream on loopback; records every request."""
    _UpstreamHandler.requests = []
    _UpstreamHandler.document = {"results": []}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


class TestControlledUpstream:
    async def test_benign_query_reaches_upstream_in_screened_shape(
        self, upstream: str
    ) -> None:
        """Feature-gate writer half: the benign query is the only thing the
        upstream ever sees, in exactly the minimal screened shape."""
        _UpstreamHandler.document = _PROVIDER_DOC
        provider = SearXNGProvider(upstream, timeout_seconds=10)
        capability = WebSearchCapability(provider)
        result = await capability(
            _call({"query": "postgres replication"}, _context())
        )
        assert result.ok is True
        assert len(_UpstreamHandler.requests) == 1
        path, captured = _UpstreamHandler.requests[0]
        assert path == "/search"
        params = dict(
            pair.split("=", 1) for pair in captured["query"].split("&")
        )
        assert params["format"] == "json"
        assert params["q"] == "postgres+replication"  # url-encoded on the wire

    async def test_sensitive_query_captured_by_nothing(self, upstream: str) -> None:
        """The synthetic sensitive-marker query must be captured by NO
        upstream request — sanitization is refusal before I/O, not raw
        forwarding of the marker (feature-gate negative control)."""
        provider = SearXNGProvider(upstream, timeout_seconds=10)
        capability = WebSearchCapability(provider)
        with pytest.raises(SearchError) as excinfo:
            await capability(
                _call(
                    {"query": f"logs for api_key={SYNTHETIC_SECRET}"},
                    _context(),
                )
            )
        assert excinfo.value.code is SearchErrorCode.SENSITIVE_QUERY
        assert _UpstreamHandler.requests == []
        assert SYNTHETIC_SECRET not in json.dumps(
            [list(r) for r in _UpstreamHandler.requests]
        )


# ---------------------------------------------------------------------------
# Real dispatcher / policy / bridge integration (descriptor availability)
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

    def __init__(self, capability: WebSearchCapability) -> None:
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
) -> CapabilityDispatcher:
    return CapabilityDispatcher(
        registry=registry,
        approvals=_unused_approvals(),
        grants=(lambda: grants) if grants is not None else (lambda: {}),
        policy=policy if policy is not None else (lambda: _policy()),
        adopted=lambda server_id: True,
        credentials=lambda: None,
        executor=executor,
        output_budget_bytes=64 * 1024,
    )


class TestDispatcherIntegration:
    async def test_granted_dispatch_executes_without_approval(self) -> None:
        capability = _RecordingCapability(
            WebSearchCapability(_StaticProvider(_PROVIDER_DOC))
        )
        registry = _registry()
        dispatcher = _dispatcher(registry, capability, grants=_grants(SERVER_A))
        outcome: DispatchOutcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_A),
            capability_id=CAPABILITY_ID,
            payload={"query": "postgres replication"},
        )
        assert outcome.allowed is True
        assert outcome.effect == "executed"
        assert outcome.executor_ok is True
        assert outcome.approval_id is None  # read effect needs no approval
        assert len(capability.calls) == 1
        document = json.loads(outcome.output.text)
        assert document["status"] == "ok"
        assert document["results"][0]["citation"]

    async def test_server_outside_grant_scope_denied_before_executor(self) -> None:
        capability = _RecordingCapability(
            WebSearchCapability(_StaticProvider(_PROVIDER_DOC))
        )
        registry = _registry()
        dispatcher = _dispatcher(registry, capability, grants=_grants(SERVER_A))
        outcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_B),
            capability_id=CAPABILITY_ID,
            payload={"query": "q"},
        )
        assert outcome.allowed is False
        assert capability.calls == []
        assert outcome.effect == "not_attempted"

    async def test_policy_deny_list_blocks_search(self) -> None:
        capability = _RecordingCapability(
            WebSearchCapability(_StaticProvider(_PROVIDER_DOC))
        )
        registry = _registry()
        dispatcher = _dispatcher(
            registry,
            capability,
            grants=_grants(SERVER_A),
            policy=lambda: _policy(denied=(CAPABILITY_ID,)),
        )
        outcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_A),
            capability_id=CAPABILITY_ID,
            payload={"query": "q"},
        )
        assert outcome.allowed is False
        assert capability.calls == []

    async def test_sensitive_query_receipt_marks_executor_failure(self) -> None:
        """Through the real dispatcher a sensitive query is an honest
        failure receipt (invocation started, effect unproven, executor
        refused), never a silent send."""
        capability = WebSearchCapability(_StaticProvider(_PROVIDER_DOC))
        registry = _registry()
        dispatcher = _dispatcher(registry, capability, grants=_grants(SERVER_A))
        outcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_A),
            capability_id=CAPABILITY_ID,
            payload={"query": f"leak password: {SYNTHETIC_SECRET}"},
        )
        # Policy gates passed (the screen, not policy, refused): the
        # receipt says the invocation started and the effect is unproven.
        assert outcome.allowed is False
        assert outcome.effect == "unknown"
        assert outcome.reason == "executor_failed"
        assert outcome.executor_ok is None
        assert outcome.error_type == "SearchError"


class TestToolBridgeAvailability:
    def test_descriptor_exposed_only_when_allowed(self) -> None:
        from mai.agents.tools import CapabilityToolBridge

        registry = _registry()
        dispatcher = _dispatcher(
            registry,
            WebSearchCapability(_StaticProvider({"results": []})),
            grants=_grants(SERVER_A),
        )
        exposed = CapabilityToolBridge(
            registry=registry,
            dispatcher=dispatcher,
            context=_context(server_id=SERVER_A, allowed=(CAPABILITY_ID,)),
        )
        names = {tool.name for tool in exposed.tools}
        assert any("search" in name for name in names)
        hidden = CapabilityToolBridge(
            registry=registry,
            dispatcher=dispatcher,
            context=_context(server_id=SERVER_A, allowed=()),
        )
        assert {tool.name for tool in hidden.tools} == set()

    async def test_bridge_executes_granted_query(self) -> None:
        from mai.agents.tools import CapabilityToolBridge

        registry = _registry()
        dispatcher = _dispatcher(
            registry,
            WebSearchCapability(_StaticProvider(_PROVIDER_DOC)),
            grants=_grants(SERVER_A),
        )
        bridge = CapabilityToolBridge(
            registry=registry,
            dispatcher=dispatcher,
            context=_context(server_id=SERVER_A),
        )
        tool = next(t for t in bridge.tools if "search" in t.name)
        result = json.loads(await tool.function(query="postgres replication"))
        assert result["status"] == "executed"
        assert result["allowed"] is True
        document = json.loads(result["output"]["text"])
        assert document["status"] == "ok"
        assert document["results"][0]["citation"].startswith("https://")

    async def test_bridge_denies_smuggled_field_before_dispatch(self) -> None:
        from mai.agents.tools import CapabilityToolBridge

        capability = _RecordingCapability(
            WebSearchCapability(_StaticProvider(_PROVIDER_DOC))
        )
        registry = _registry()
        dispatcher = _dispatcher(registry, capability, grants=_grants(SERVER_A))
        bridge = CapabilityToolBridge(
            registry=registry,
            dispatcher=dispatcher,
            context=_context(server_id=SERVER_A),
        )
        tool = next(t for t in bridge.tools if "search" in t.name)
        result = json.loads(
            await tool.function(query="q", base_url="http://evil.example")
        )
        assert result["status"] == "denied"
        assert result["error"]["code"] == "payload_schema_mismatch"
        assert capability.calls == []
        assert result["effect"] == "not_attempted"


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


# ---------------------------------------------------------------------------
# Production entrypoint: real subprocess against the controlled upstream
# ---------------------------------------------------------------------------


class TestCliEntrypoint:
    def test_cli_query_hits_upstream(self, upstream: str) -> None:
        _UpstreamHandler.document = _PROVIDER_DOC
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "mai.research.search",
                "query",
                "--base-url",
                upstream,
                "--query",
                "postgres replication",
                "--limit",
                "2",
            ],
            capture_output=True,
            text=True,
            cwd=BACKEND_ROOT,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout.strip().splitlines()[-1])
        assert document["status"] == "ok"
        assert len(document["results"]) == 2
        assert document["query"] == "postgres replication"
        assert len(_UpstreamHandler.requests) == 1

    def test_cli_sensitive_query_is_structured_rejection(self, upstream: str) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "mai.research.search",
                "query",
                "--base-url",
                upstream,
                "--query",
                f"leak api_key={SYNTHETIC_SECRET}",
            ],
            capture_output=True,
            text=True,
            cwd=BACKEND_ROOT,
            timeout=120,
        )
        assert proc.returncode == 1
        document = json.loads(proc.stdout.strip().splitlines()[-1])
        assert document["kind"] == "error"
        assert document["code"] == "sensitive_query"
        assert SYNTHETIC_SECRET not in proc.stdout
        assert _UpstreamHandler.requests == []

    def test_cli_unavailable_provider_is_honest_status(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "mai.research.search",
                "query",
                "--base-url",
                "http://127.0.0.1:1",  # nothing listens: refused fast
                "--query",
                "benign query",
                "--timeout-seconds",
                "5",
            ],
            capture_output=True,
            text=True,
            cwd=BACKEND_ROOT,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout.strip().splitlines()[-1])
        assert document["status"] == "unavailable"
        assert document["code"] in (
            "provider_unreachable",
            "provider_error",
        )
        assert document["results"] == []
