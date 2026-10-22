"""Worker wiring tests: the production ``web.read`` byte ceiling.

Acceptance (card ``[MAI WEB W1]``): the wired provider's byte ceiling must
agree with the advertised ``web.read`` schema maximum, so a schema-valid
``max_bytes`` request is never refused by the wiring itself.

Boundary discipline (test policy): these tests drive the REAL production
construction path — :func:`mai.jobs.worker._build_web_read_capability` is
the seam the worker runtime itself calls — and the real
:class:`~mai.research.pages.HttpPageProvider` / httpx2 client over an
explicit ``MockTransport`` (deterministic upstream, no public-site
dependency, no production DB). The runtime-invocation test proves the
worker composition reaches that seam; nothing here greps source or
re-creates the intended wiring independently.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast

import httpx2
import pytest

from mai.capabilities.dispatch import CapabilityCall
from mai.jobs import worker
from mai.policy.evaluate import ExecutionContext, PolicyMode
from mai.research.pages import (
    CAPABILITY_ID,
    DEFAULT_MAX_BYTES,
    MAX_MAX_BYTES,
    MIN_MAX_BYTES,
    HttpPageProvider,
    descriptor,
)

pytestmark = pytest.mark.anyio

#: A deterministic public address for the resolver seam (nothing is ever
#: dialed — the transport seam intercepts every request).
PUBLIC_IP = "93.184.216.34"

#: The incident payload: a schema-valid request the old wiring refused.
INCIDENT_URL = "https://developer.valvesoftware.com/wiki/Server_queries"
INCIDENT_MAX_BYTES = 524_288
INCIDENT_MAX_MARKDOWN_CHARS = 10_000

SERVER_A = "5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60"
RUN_1 = "6e7f8a9b-0c1d-4e2f-8a3b-4c5d6e7f8a90"
PRINCIPAL = "tester@mai.local"

FIXTURE_HTML = (
    "<html><head><title>Server queries</title></head><body>"
    "<h1>Server queries</h1>"
    "<p>A bounded fixture page body for the worker wiring tests.</p>"
    "</body></html>"
)


def _public_resolver(host: str) -> Sequence[str]:
    return (PUBLIC_IP,)


def _context() -> ExecutionContext:
    return ExecutionContext(
        principal=PRINCIPAL,
        server_id=uuid.UUID(SERVER_A),
        run_id=uuid.UUID(RUN_1),
        parent_scope=None,
        profile_revision="prof-1",
        prompt_revision="prompt-1",
        credential_references=(),
        allowed_capabilities=(CAPABILITY_ID,),
        mode=PolicyMode.DELEGATED,
    )


def _call(payload: Mapping[str, object]) -> CapabilityCall:
    return CapabilityCall(
        descriptor=descriptor(),
        payload=payload,
        context=_context(),
        credentials={},
        op_id=uuid.uuid4(),
    )


def _html_response(
    body: str | bytes,
    *,
    status: int = 200,
    content_type: str = "text/html",
) -> httpx2.Response:
    payload = body.encode() if isinstance(body, str) else body
    return httpx2.Response(
        status, headers={"Content-Type": content_type}, content=payload
    )


class _RecordingTransport:
    """Deterministic upstream: records every request, serves a fixture."""

    def __init__(self, body: str | bytes = FIXTURE_HTML) -> None:
        self._body = body
        self.requests: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return _html_response(self._body)

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handler)


def _wired_capability(upstream: _RecordingTransport) -> Any:
    """The production construction path, with the provider's own seams."""
    return worker._build_web_read_capability(
        transport=upstream.transport, resolver=_public_resolver
    )


def _provider_of(capability: Any) -> HttpPageProvider:
    return cast(HttpPageProvider, capability._provider)


def _max_bytes_schema() -> dict[str, Any]:
    """The advertised ``max_bytes`` property of the ``web.read`` schema."""
    schema = descriptor().input_schema
    properties = cast(dict[str, Any], schema["properties"])
    return cast(dict[str, Any], properties["max_bytes"])


class TestCeilingAgreement:
    def test_default_max_bytes_unchanged(self) -> None:
        assert DEFAULT_MAX_BYTES == 262_144
        schema = _max_bytes_schema()
        assert schema["default"] == DEFAULT_MAX_BYTES
        assert schema["maximum"] == MAX_MAX_BYTES

    def test_wired_provider_ceiling_matches_advertised_maximum(self) -> None:
        upstream = _RecordingTransport()
        capability = _wired_capability(upstream)
        provider = _provider_of(capability)
        assert provider._max_bytes == MAX_MAX_BYTES == 1_048_576
        schema = _max_bytes_schema()
        assert provider._max_bytes == schema["maximum"]

    def test_capability_defaults_preserved(self) -> None:
        upstream = _RecordingTransport()
        capability = _wired_capability(upstream)
        assert capability._default_max_bytes == DEFAULT_MAX_BYTES


class TestIncidentPayload:
    async def test_incident_payload_reaches_transport_and_succeeds(self) -> None:
        upstream = _RecordingTransport()
        capability = _wired_capability(upstream)
        result = await capability(
            _call(
                {
                    "url": INCIDENT_URL,
                    "max_bytes": INCIDENT_MAX_BYTES,
                    "max_markdown_chars": INCIDENT_MAX_MARKDOWN_CHARS,
                }
            )
        )
        assert result.ok is True
        document = json.loads(result.output)
        assert document["status"] == "ok"
        assert document["url"] == INCIDENT_URL
        assert document["title"] == "Server queries"
        assert document["bytes_read"] == len(FIXTURE_HTML.encode())
        # The request really reached the transport (pinned dial + Host
        # header preserved, as the provider does for every hop).
        assert len(upstream.requests) == 1
        assert upstream.requests[0].headers["host"] == (
            "developer.valvesoftware.com"
        )

    async def test_schema_maximum_request_succeeds(self) -> None:
        upstream = _RecordingTransport()
        capability = _wired_capability(upstream)
        result = await capability(
            _call(
                {
                    "url": INCIDENT_URL,
                    "max_bytes": MAX_MAX_BYTES,
                    "max_markdown_chars": INCIDENT_MAX_MARKDOWN_CHARS,
                }
            )
        )
        assert result.ok is True
        document = json.loads(result.output)
        assert document["status"] == "ok"
        assert len(upstream.requests) == 1

    async def test_default_request_uses_default_budget(self) -> None:
        upstream = _RecordingTransport()
        capability = _wired_capability(upstream)
        result = await capability(_call({"url": INCIDENT_URL}))
        assert result.ok is True
        document = json.loads(result.output)
        assert document["status"] == "ok"
        assert document["bytes_read"] == len(FIXTURE_HTML.encode())

    async def test_over_budget_body_is_refused(self) -> None:
        body = b"<p>" + (b"x" * (INCIDENT_MAX_BYTES + 4096)) + b"</p>"
        upstream = _RecordingTransport(body)
        capability = _wired_capability(upstream)
        result = await capability(
            _call(
                {
                    "url": INCIDENT_URL,
                    "max_bytes": INCIDENT_MAX_BYTES,
                    "max_markdown_chars": INCIDENT_MAX_MARKDOWN_CHARS,
                }
            )
        )
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "refused"
        assert document["code"] == "response_too_large"
        assert document["bytes_read"] == 0
        assert document["markdown"] == ""

    async def test_above_schema_maximum_rejected_before_transport(self) -> None:
        # W2: an out-of-schema payload is a known, structured refusal —
        # a refused result, not a raised exception and never an unknown
        # effect. It is still rejected before any transport I/O.
        upstream = _RecordingTransport()
        capability = _wired_capability(upstream)
        result = await capability(
            _call(
                {
                    "url": INCIDENT_URL,
                    "max_bytes": MAX_MAX_BYTES + 1,
                    "max_markdown_chars": INCIDENT_MAX_MARKDOWN_CHARS,
                }
            )
        )
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "refused"
        assert document["code"] == "invalid_payload"
        # Rejected before any transport I/O.
        assert upstream.requests == []

    async def test_below_floor_rejected_before_transport(self) -> None:
        upstream = _RecordingTransport()
        capability = _wired_capability(upstream)
        result = await capability(
            _call({"url": INCIDENT_URL, "max_bytes": MIN_MAX_BYTES - 1})
        )
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "refused"
        assert document["code"] == "invalid_payload"
        assert upstream.requests == []


class TestRuntimeInvocation:
    def test_worker_runtime_invokes_the_construction_seam(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The worker composition reaches the seam this slice changed.

        The runtime is composed with a disposable data root and a URL that
        is never connected to (no production DB, no network): the test
        asserts that the production composition calls the very seam the
        ``web.read`` executor is built from.
        """
        from mai.agents import delegate as agent_delegate
        from mai.agents import run as agent_run

        calls: list[object] = []
        original = worker._build_web_read_capability

        def spy(**kwargs: object) -> object:
            calls.append(kwargs)
            return original(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(worker, "_build_web_read_capability", spy)
        monkeypatch.setenv("MAI_DATA_ROOT", str(tmp_path))
        agent_run.reset_agent_run()
        agent_delegate.reset_delegation()
        try:
            worker._configure_default_agent_runtime(
                "postgresql://mai:mai@127.0.0.1:5999/mai"
            )
        finally:
            agent_run.reset_agent_run()
            agent_delegate.reset_delegation()

        assert len(calls) == 1
        # Production wiring passes no transport/resolver seam: real DNS,
        # a real dial, the real address policy.
        assert calls[0] == {}
