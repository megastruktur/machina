"""Focused tests for bank-scoped Hindsight operational memory
(card 11-memory-adapter).

Acceptance (BRIEF):

1. Server-specific bank/scope and explicit shared memory; actual retain
   receipt and failed write visible.
2. Memory not substituted for live facts/docs; secrets excluded and
   delete/export scope supported per approved API.

Boundary discipline (test policy, high-risk slice): bank-scope resolution,
payload validation, the secret screen and result parsing run pure; the
outbound-request shape and timeout/connect/HTTP classification run through
the real httpx2 client machinery over an explicit ``MockTransport`` (real
client boundary, deterministic upstream); the grant/policy gate path runs
through the real ``03-capability-registry``/``03-dispatcher``/``03-policy``
producers; the production entrypoint runs as a real subprocess against a
loopback Hindsight-shaped fixture that records every request. The
retain→recall round trip against the real bundled image is the assembled
KB-H2 concern of ``11-verify``; this suite proves the adapter contract
offline and records that limitation in the evidence README.
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
from mai.memory.hindsight import (
    DEFAULT_MAX_TEXT_CHARS,
    DEFAULT_TOKEN_BUDGET,
    MAX_BUDGET_TOKENS,
    MAX_CONTENT_CHARS,
    MAX_ITEMS,
    MAX_QUERY_CHARS,
    MAX_RESULTS,
    MAX_TEXT_CHARS,
    MemoryAdapterError,
    MemoryErrorCode,
    MemoryStatus,
    RecallOutcome,
    ReflectOutcome,
    RetainOutcome,
    SERVER_BANK_PREFIX,
    SHARED_BANK_PREFIX,
    HindsightProvider,
    MemoryRecallCapability,
    MemoryReflectCapability,
    MemoryRetainCapability,
    bank_for_scope,
    parse_recall_document,
    parse_reflect_document,
    parse_retain_document,
    recall_descriptor,
    recall_document,
    reflect_descriptor,
    reflect_document,
    retain_descriptor,
    retain_document,
    shared_bank_id,
)

BACKEND_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.anyio

# Frozen fixture identities (canonical lowercase UUID strings, TEST-NET style).
SERVER_A = "5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60"
SERVER_B = "8f1c2e3d-4a5b-4c6d-9e7f-0a1b2c3d4e5f"
RUN_1 = "6e7f8a9b-0c1d-4e2f-8a3b-4c5d6e7f8a90"
PRINCIPAL = "tester@mai.local"
SHARED_BANKS = ("ops", "runbooks")

#: A synthetic marker used by the screen tests; deliberately NOT a real
#: credential anywhere.
SYNTHETIC_SECRET = "sk-fxt11AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

SERVER_A_BANK = SERVER_BANK_PREFIX + SERVER_A
SERVER_B_BANK = SERVER_BANK_PREFIX + SERVER_B


# ---------------------------------------------------------------------------
# Pure seam: scope → bank resolution (C07 rule 6, KB-BANKS)
# ---------------------------------------------------------------------------


def _context(
    *,
    server_id: str = SERVER_A,
    allowed: tuple[str, ...] = ("memory.recall",),
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


def _call(
    payload: Mapping[str, object],
    context: ExecutionContext,
    descriptor_id: str = "memory.recall",
) -> CapabilityCall:
    descriptor = {
        "memory.recall": recall_descriptor,
        "memory.retain": retain_descriptor,
        "memory.reflect": reflect_descriptor,
    }[descriptor_id]()
    return CapabilityCall(
        descriptor=descriptor,
        payload=payload,
        context=context,
        credentials={},
        op_id=uuid.uuid4(),
    )


class TestBankScheme:
    def test_server_bank_is_deterministic_per_context(self) -> None:
        assert bank_for_scope("server", _context()) == SERVER_A_BANK
        assert bank_for_scope("server", _context()) == bank_for_scope(
            "server", _context()
        )

    def test_two_servers_never_share_a_bank(self) -> None:
        """KB-BANKS premise: two server-scoped fixtures resolve to two
        disjoint banks; the payload cannot influence which."""
        assert SERVER_A_BANK != SERVER_B_BANK
        assert bank_for_scope("server", _context(server_id=SERVER_B)) == (
            SERVER_B_BANK
        )

    def test_shared_bank_resolves_through_allowlist(self) -> None:
        bank = bank_for_scope(
            "shared:ops", _context(), shared_banks=SHARED_BANKS
        )
        assert bank == SHARED_BANK_PREFIX + "ops"

    def test_unlisted_shared_name_is_forbidden(self) -> None:
        with pytest.raises(MemoryAdapterError) as excinfo:
            bank_for_scope(
                "shared:secret-bank", _context(), shared_banks=SHARED_BANKS
            )
        assert excinfo.value.code is MemoryErrorCode.BANK_FORBIDDEN
        # No allowlist configured at all: every shared scope is forbidden.
        with pytest.raises(MemoryAdapterError) as excinfo:
            bank_for_scope("shared:ops", _context())
        assert excinfo.value.code is MemoryErrorCode.BANK_FORBIDDEN

    @pytest.mark.parametrize(
        "scope", ["", "bank", "server ", "shared:", "SERVER", None, 7]
    )
    def test_invalid_scope_rejected(self, scope: object) -> None:
        with pytest.raises(MemoryAdapterError) as excinfo:
            bank_for_scope(scope, _context(), shared_banks=SHARED_BANKS)
        assert excinfo.value.code is MemoryErrorCode.INVALID_PAYLOAD

    @pytest.mark.parametrize(
        "name",
        ["", "Ops", "ops_x", "-ops", "ops-", "a" * 65, None, 5],
    )
    def test_shared_name_shape_enforced(self, name: object) -> None:
        with pytest.raises(MemoryAdapterError) as excinfo:
            shared_bank_id(name)  # type: ignore[arg-type]
        assert excinfo.value.code is MemoryErrorCode.INVALID_PAYLOAD

    def test_shared_banks_allowlist_wiring(self) -> None:
        from mai.memory.hindsight import parse_shared_banks

        assert parse_shared_banks(None) == ()
        assert parse_shared_banks(["ops", "runbooks"]) == ("ops", "runbooks")
        with pytest.raises(MemoryAdapterError):
            parse_shared_banks(["ops", "ops"])  # duplicates
        with pytest.raises(MemoryAdapterError):
            parse_shared_banks(["Ops"])  # shape
        with pytest.raises(MemoryAdapterError):
            parse_shared_banks([str(i) for i in range(33)])  # cap
        with pytest.raises(MemoryAdapterError):
            parse_shared_banks("ops")  # not a sequence of names

    def test_prefixes_are_disjoint(self) -> None:
        assert not SERVER_BANK_PREFIX.startswith(SHARED_BANK_PREFIX)
        assert not SHARED_BANK_PREFIX.startswith(SERVER_BANK_PREFIX)


# ---------------------------------------------------------------------------
# Pure seam: payload validation (smuggled fields never reach I/O)
# ---------------------------------------------------------------------------


class TestRetainPayload:
    def test_valid_request_round_trips(self) -> None:
        request = self._retain_request(
            {
                "scope": "server",
                "items": [
                    {
                        "content": "restarted the worker",
                        "context": "maintenance window",
                        "document_id": "maint-2026-10-18",
                    }
                ],
            }
        )
        assert request.scope == "server"
        assert request.items[0].content == "restarted the worker"
        assert request.items[0].context == "maintenance window"
        assert request.items[0].document_id == "maint-2026-10-18"

    def _retain_request(self, payload: Mapping[str, object]):
        from mai.memory.hindsight import retain_request_from_payload

        return retain_request_from_payload(payload)

    def test_non_mapping_payload_rejected(self) -> None:
        with pytest.raises(MemoryAdapterError) as excinfo:
            self._retain_request(["items"])  # type: ignore[arg-type]
        assert excinfo.value.code is MemoryErrorCode.INVALID_PAYLOAD

    @pytest.mark.parametrize(
        "field",
        ["bank", "server_id", "async", "metadata", "tags", "credentials"],
    )
    def test_smuggled_field_rejected(self, field: str) -> None:
        payload = {"scope": "server", "items": [{"content": "x"}], field: "v"}
        with pytest.raises(MemoryAdapterError) as excinfo:
            self._retain_request(payload)
        assert excinfo.value.code is MemoryErrorCode.INVALID_PAYLOAD
        assert field in str(excinfo.value)

    @pytest.mark.parametrize(
        "items",
        [[], "x", [{"content": "x"}] * (MAX_ITEMS + 1), ["x"], [5]],
    )
    def test_bad_items_shape_rejected(self, items: object) -> None:
        with pytest.raises(MemoryAdapterError) as excinfo:
            self._retain_request({"scope": "server", "items": items})
        assert excinfo.value.code is MemoryErrorCode.INVALID_PAYLOAD

    @pytest.mark.parametrize(
        "field", ["timestamp", "tags", "strategy", "update_mode"]
    )
    def test_smuggled_item_field_rejected(self, field: str) -> None:
        payload = {
            "scope": "server",
            "items": [{"content": "x", field: "v"}],
        }
        with pytest.raises(MemoryAdapterError) as excinfo:
            self._retain_request(payload)
        assert excinfo.value.code is MemoryErrorCode.INVALID_PAYLOAD
        assert field in str(excinfo.value)

    @pytest.mark.parametrize(
        "content", ["", "   ", "x" * (MAX_CONTENT_CHARS + 1), "bad\x00text"]
    )
    def test_bad_content_rejected(self, content: str) -> None:
        with pytest.raises(MemoryAdapterError) as excinfo:
            self._retain_request(
                {"scope": "server", "items": [{"content": content}]}
            )
        assert excinfo.value.code is MemoryErrorCode.INVALID_PAYLOAD

    def test_bad_context_and_document_id_rejected(self) -> None:
        base = {"scope": "server", "items": [{"content": "x"}]}
        with pytest.raises(MemoryAdapterError):
            self._retain_request(
                {**base, "items": [{"content": "x", "context": "y" * 501}]}
            )
        with pytest.raises(MemoryAdapterError):
            self._retain_request(
                {
                    **base,
                    "items": [
                        {"content": "x", "document_id": "bad id spaces!"}
                    ],
                }
            )
        with pytest.raises(MemoryAdapterError):
            self._retain_request(
                {
                    **base,
                    "items": [
                        {"content": "x", "document_id": "d" * 129}
                    ],
                }
            )


class TestQueryPayload:
    def test_recall_defaults_applied(self) -> None:
        from mai.memory.hindsight import recall_request_from_payload

        request = recall_request_from_payload(
            {"scope": "server", "query": "postgres version"}
        )
        assert request.scope == "server"
        assert request.query == "postgres version"
        assert request.types is None
        assert request.budget == "low"
        assert request.max_tokens == DEFAULT_TOKEN_BUDGET

    def test_recall_types_and_bounds(self) -> None:
        from mai.memory.hindsight import recall_request_from_payload

        request = recall_request_from_payload(
            {
                "scope": "shared:ops",
                "query": "q",
                "types": ["world", "observation"],
                "budget": "high",
                "max_tokens": 4096,
            }
        )
        assert request.types == ("world", "observation")
        assert request.budget == "high"

    @pytest.mark.parametrize(
        "payload",
        [
            {"query": "q"},  # scope missing
            {"scope": "server"},  # query missing
            {"scope": "server", "query": ""},
            {"scope": "server", "query": "   "},
            {"scope": "server", "query": "x" * (MAX_QUERY_CHARS + 1)},
            {"scope": "server", "query": "q", "bank": "mai-server-x"},
            {"scope": "server", "query": "q", "server_id": SERVER_B},
            {"scope": "server", "query": "q", "types": []},
            {"scope": "server", "query": "q", "types": ["secret"]},
            {"scope": "server", "query": "q", "budget": "max"},
            {"scope": "server", "query": "q", "max_tokens": 0},
            {"scope": "server", "query": "q", "max_tokens": MAX_BUDGET_TOKENS + 1},
            {"scope": "server", "query": "q", "max_tokens": True},
            {"scope": "server", "query": "q", "max_tokens": "1024"},
        ],
    )
    def test_bad_recall_payloads_rejected(self, payload: dict) -> None:
        from mai.memory.hindsight import recall_request_from_payload

        with pytest.raises(MemoryAdapterError) as excinfo:
            recall_request_from_payload(payload)
        assert excinfo.value.code is MemoryErrorCode.INVALID_PAYLOAD

    def test_reflect_rejects_types_field(self) -> None:
        from mai.memory.hindsight import reflect_request_from_payload

        with pytest.raises(MemoryAdapterError) as excinfo:
            reflect_request_from_payload(
                {
                    "scope": "server",
                    "query": "q",
                    "types": ["world"],
                }
            )
        assert excinfo.value.code is MemoryErrorCode.INVALID_PAYLOAD
        assert "types" in str(excinfo.value)


class TestSecretScreen:
    async def test_sensitive_retain_item_never_touches_provider(self) -> None:
        provider = _RecordingProvider({"success": True, "items_count": 1})
        capability = MemoryRetainCapability(provider, shared_banks=SHARED_BANKS)
        with pytest.raises(MemoryAdapterError) as excinfo:
            await capability(
                _call(
                    {
                        "scope": "shared:ops",
                        "items": [
                            {"content": "benign"},
                            {"content": f"set api_key={SYNTHETIC_SECRET}"},
                        ],
                    },
                    _context(),
                    "memory.retain",
                )
            )
        assert excinfo.value.code is MemoryErrorCode.SENSITIVE_CONTENT
        # The marker class is named; the value never is (it is the secret).
        assert "credential_assignment" in excinfo.value.detail
        assert SYNTHETIC_SECRET not in excinfo.value.detail
        assert provider.calls == []  # nothing left the process

    async def test_sensitive_recall_query_refused_before_io(self) -> None:
        provider = _RecordingProvider({"results": []})
        capability = MemoryRecallCapability(provider)
        with pytest.raises(MemoryAdapterError) as excinfo:
            await capability(
                _call(
                    {"scope": "server", "query": f"token: {SYNTHETIC_SECRET}"},
                    _context(),
                )
            )
        assert excinfo.value.code is MemoryErrorCode.SENSITIVE_CONTENT
        assert provider.calls == []

    async def test_sensitive_reflect_query_refused_before_io(self) -> None:
        provider = _RecordingProvider({"text": "x"})
        capability = MemoryReflectCapability(provider)
        with pytest.raises(MemoryAdapterError) as excinfo:
            await capability(
                _call(
                    {
                        "scope": "server",
                        "query": "-----BEGIN RSA PRIVATE KEY-----",
                    },
                    _context(),
                )
            )
        assert excinfo.value.code is MemoryErrorCode.SENSITIVE_CONTENT
        assert provider.calls == []


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


_RETAIN_DOC_OK = {
    "success": True,
    "bank_id": SERVER_A_BANK,
    "items_count": 1,
    "async": False,
    "usage": {
        "input_tokens": 90,
        "output_tokens": 12,
        "total_tokens": 102,
    },
}

_RECALL_DOC = {
    "results": [
        {
            "id": "mem-0001",
            "text": "the worker runs postgres 18",
            "type": "world",
            "entities": ["postgres"],
            "context": "maintenance",
            "occurred_start": "2026-10-18T00:00:00Z",
            "occurred_end": "2026-10-18T00:00:00Z",
            "mentioned_at": "2026-10-18T01:00:00Z",
            "document_id": "maint-2026-10-18",
            "metadata": {"internal": "dropped-by-adapter"},
            "tags": ["infra"],
            "source_fact_ids": ["f-1"],
            "scores": {"final": 0.83, "reranker": 0.7},
        }
    ]
}


class TestProviderRequestBoundary:
    async def test_retain_request_is_the_pinned_sync_shape(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return _json_response(_RETAIN_DOC_OK)

        provider = HindsightProvider(
            "http://hindsight.internal:8888",
            transport=httpx2.MockTransport(handler),
        )
        document = await provider.retain(
            bank_id=SERVER_A_BANK,
            items=[{"content": "restarted the worker"}],
        )
        assert document["success"] is True
        request = seen[0]
        assert request.method == "POST"
        assert request.url.path == f"/v1/default/banks/{SERVER_A_BANK}/memories"
        body = json.loads(request.content)
        # Synchronous retain: the receipt is the actual write evidence.
        assert body == {"items": [{"content": "restarted the worker"}], "async": False}
        assert "python" not in request.headers["user-agent"].lower()
        assert "httpx" not in request.headers["user-agent"].lower()
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers

    async def test_recall_request_shape_and_optional_types(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return _json_response(_RECALL_DOC)

        provider = HindsightProvider(
            "http://hindsight.internal:8888",
            transport=httpx2.MockTransport(handler),
        )
        await provider.recall(
            bank_id=SERVER_A_BANK,
            query="postgres version",
            types=None,
            budget="low",
            max_tokens=1024,
        )
        assert seen[0].url.path == (
            f"/v1/default/banks/{SERVER_A_BANK}/memories/recall"
        )
        body = json.loads(seen[0].content)
        assert body == {"query": "postgres version", "budget": "low", "max_tokens": 1024}

        await provider.recall(
            bank_id=SERVER_A_BANK,
            query="q",
            types=("world",),
            budget="mid",
            max_tokens=256,
        )
        body = json.loads(seen[1].content)
        assert body["types"] == ["world"]
        assert body["budget"] == "mid"

    async def test_reflect_delete_export_version_paths(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            if request.method == "DELETE":
                if request.url.path.endswith("/memories"):
                    return _json_response({"success": True, "deleted_count": 3})
                return _json_response({"success": True, "deleted_count": 9})
            if request.url.path.endswith("/reflect"):
                return _json_response({"text": "answer", "based_on": {"world": ["f-1"]}})
            if request.url.path.endswith("/export"):
                return _json_response({"version": "1", "bank": {}, "memories": []})
            return _json_response({"api_version": "1.0.0", "features": {}})

        provider = HindsightProvider(
            "http://hindsight.internal:8888",
            transport=httpx2.MockTransport(handler),
        )
        reflect_document_raw = await provider.reflect(
            bank_id=SHARED_BANK_PREFIX + "ops", query="q", budget="low", max_tokens=1024
        )
        assert reflect_document_raw["text"] == "answer"
        delete_doc = await provider.delete_memories(
            bank_id=SERVER_A_BANK, fact_type="world"
        )
        assert delete_doc["success"] is True
        delete_bank_doc = await provider.delete_bank(bank_id=SERVER_A_BANK)
        assert delete_bank_doc["success"] is True
        export_doc = await provider.export_bank(bank_id=SERVER_A_BANK)
        assert export_doc["version"] == "1"
        version_doc = await provider.version()
        assert version_doc["api_version"] == "1.0.0"

        assert [request.method for request in seen] == [
            "POST",
            "DELETE",
            "DELETE",
            "GET",
            "GET",
        ]
        assert seen[0].url.path == f"/v1/default/banks/{SHARED_BANK_PREFIX}ops/reflect"
        assert seen[1].url.path == f"/v1/default/banks/{SERVER_A_BANK}/memories"
        assert dict(seen[1].url.params) == {"type": "world"}
        assert seen[2].url.path == f"/v1/default/banks/{SERVER_A_BANK}"
        assert seen[3].url.path == f"/v1/default/banks/{SERVER_A_BANK}/export"
        assert seen[4].url.path == "/version"
        # delete-memories without a filter sends no type parameter.
        await provider.delete_memories(bank_id=SERVER_A_BANK)
        assert dict(seen[5].url.params) == {}

    async def test_http_error_status_classified_provider_error(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(500, text="boom")

        provider = HindsightProvider(
            "http://hindsight.internal:8888",
            transport=httpx2.MockTransport(handler),
        )
        with pytest.raises(httpx2.HTTPStatusError):
            await provider.recall(
                bank_id=SERVER_A_BANK, query="q", types=None, budget="low", max_tokens=1024
            )

    async def test_timeout_and_connect_failure_classified(self) -> None:
        def timeout_handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.TimeoutException("simulated read timeout")

        provider = HindsightProvider(
            "http://hindsight.internal:8888",
            transport=httpx2.MockTransport(timeout_handler),
        )
        with pytest.raises(httpx2.TimeoutException):
            await provider.version()

        def connect_handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("simulated connect failure")

        provider = HindsightProvider(
            "http://hindsight.internal:8888",
            transport=httpx2.MockTransport(connect_handler),
        )
        with pytest.raises(httpx2.ConnectError):
            await provider.version()

    async def test_non_object_json_is_provider_error(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=["not", "an", "object"])

        provider = HindsightProvider(
            "http://hindsight.internal:8888",
            transport=httpx2.MockTransport(handler),
        )
        with pytest.raises(MemoryAdapterError) as excinfo:
            await provider.version()
        assert excinfo.value.code is MemoryErrorCode.PROVIDER_ERROR

    def test_bad_base_url_and_timeout_rejected_at_construction(self) -> None:
        with pytest.raises(MemoryAdapterError) as excinfo:
            HindsightProvider("not a url")
        assert excinfo.value.code is MemoryErrorCode.INVALID_PAYLOAD
        with pytest.raises(MemoryAdapterError):
            HindsightProvider("http://hindsight:8888", timeout_seconds=0)
        with pytest.raises(MemoryAdapterError):
            HindsightProvider("http://hindsight:8888", timeout_seconds=301)

    async def test_foreign_bank_id_refused_before_io(self) -> None:
        """The provider seam only dials this adapter's bank scheme: a
        miswired caller cannot turn the bank id into an arbitrary path."""

        def handler(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover
            raise AssertionError("no request may leave")

        provider = HindsightProvider(
            "http://hindsight.internal:8888",
            transport=httpx2.MockTransport(handler),
        )
        for bank in ("mai-other-x", "../../etc", "x" * 200, "mai-shared-"):
            with pytest.raises(MemoryAdapterError) as excinfo:
                await provider.recall(
                    bank_id=bank, query="q", types=None, budget="low", max_tokens=1024
                )
            assert excinfo.value.code is MemoryErrorCode.INVALID_PAYLOAD


# ---------------------------------------------------------------------------
# Executor: classification, degradation and honest receipts
# ---------------------------------------------------------------------------


class _RecordingProvider:
    """Deterministic provider double: records calls, returns one document."""

    def __init__(self, document: Mapping[str, object]) -> None:
        self._document = document
        self.calls: list[str] = []

    async def retain(self, *, bank_id, items, **_):
        self.calls.append(f"retain:{bank_id}")
        return self._document

    async def recall(self, *, bank_id, query, types, budget, max_tokens, **_):
        self.calls.append(f"recall:{bank_id}")
        return self._document

    async def reflect(self, *, bank_id, query, budget, max_tokens, **_):
        self.calls.append(f"reflect:{bank_id}")
        return self._document

    async def delete_memories(self, *, bank_id, fact_type=None, **_):
        self.calls.append(f"delete-memories:{bank_id}")
        return self._document

    async def delete_bank(self, *, bank_id, **_):
        self.calls.append(f"delete-bank:{bank_id}")
        return self._document

    async def export_bank(self, *, bank_id, **_):
        self.calls.append(f"export:{bank_id}")
        return self._document

    async def version(self, **_):
        self.calls.append("version")
        return self._document


class _FlakyProvider:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    async def recall(self, **_):
        self.calls += 1
        raise self._exc

    async def retain(self, *, bank_id, items, **_):
        self.calls += 1
        raise self._exc

    async def reflect(self, **_):
        self.calls += 1
        raise self._exc


class TestExecutorClassification:
    async def test_timeout_becomes_provider_timeout(self) -> None:
        capability = MemoryRecallCapability(_FlakyProvider(httpx2.TimeoutException("t")))
        result = await capability(_call({"scope": "server", "query": "q"}, _context()))
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "unavailable"
        assert document["code"] == "provider_timeout"
        assert document["error_type"] == "TimeoutException"
        assert document["results"] == []
        assert document["bank"] == SERVER_A_BANK

    async def test_connect_error_becomes_provider_unreachable(self) -> None:
        capability = MemoryRecallCapability(_FlakyProvider(httpx2.ConnectError("c")))
        result = await capability(_call({"scope": "server", "query": "q"}, _context()))
        document = json.loads(result.output)
        assert document["code"] == "provider_unreachable"
        assert document["error_type"] == "ConnectError"

    async def test_http_status_error_becomes_provider_error(self) -> None:
        request = httpx2.Request("POST", "http://hindsight.internal/recall")
        capability = MemoryRecallCapability(
            _FlakyProvider(
                httpx2.HTTPStatusError(
                    "500", request=request, response=httpx2.Response(500, request=request)
                )
            )
        )
        result = await capability(_call({"scope": "server", "query": "q"}, _context()))
        document = json.loads(result.output)
        assert document["code"] == "provider_error"
        assert document["error_type"] == "HTTPStatusError"

    async def test_unknown_provider_failure_is_value_free(self) -> None:
        capability = MemoryRecallCapability(_FlakyProvider(RuntimeError("the query was xyz")))
        result = await capability(_call({"scope": "server", "query": "q"}, _context()))
        document = json.loads(result.output)
        assert document["code"] == "provider_error"
        assert "xyz" not in result.output  # value-free
        assert document["error_type"] == "RuntimeError"

    async def test_retain_failure_is_visible_never_success(self) -> None:
        """KB-H2 core rule: an outage during retain is unavailable with no
        receipt — a failed write can never read as success."""
        capability = MemoryRetainCapability(_FlakyProvider(httpx2.ConnectError("c")))
        result = await capability(
            _call(
                {"scope": "server", "items": [{"content": "fact"}]},
                _context(),
                "memory.retain",
            )
        )
        assert result.ok is False
        document = json.loads(result.output)
        assert document["status"] == "unavailable"
        assert document["code"] == "provider_unreachable"
        assert document["receipt"] is None

    async def test_disabled_provider_degrades_gracefully(self) -> None:
        """Hindsight profile off / unwired: honest unavailable answers, no
        import failure, no crash, no fake empty success."""
        recall = MemoryRecallCapability(None)
        result = await recall(_call({"scope": "server", "query": "q"}, _context()))
        document = json.loads(result.output)
        assert result.ok is False
        assert document["status"] == "unavailable"
        assert document["code"] == "memory_disabled"
        assert document["results"] == []
        retain = MemoryRetainCapability(None)
        result = await retain(
            _call(
                {"scope": "server", "items": [{"content": "fact"}]},
                _context(),
                "memory.retain",
            )
        )
        document = json.loads(result.output)
        assert result.ok is False
        assert document["code"] == "memory_disabled"
        assert document["receipt"] is None
        reflect = MemoryReflectCapability(None)
        result = await reflect(
            _call({"scope": "server", "query": "q"}, _context(), "memory.reflect")
        )
        document = json.loads(result.output)
        assert result.ok is False
        assert document["code"] == "memory_disabled"

    async def test_direct_call_validates_inputs(self) -> None:
        capability = MemoryRecallCapability(_RecordingProvider(_RECALL_DOC))
        with pytest.raises(MemoryAdapterError) as excinfo:
            await capability("not-a-call")  # type: ignore[arg-type]
        assert excinfo.value.code is MemoryErrorCode.INVALID_PAYLOAD
        with pytest.raises(MemoryAdapterError) as excinfo:
            await capability(_call({"scope": "server", "query": ""}, _context()))
        assert excinfo.value.code is MemoryErrorCode.INVALID_PAYLOAD


# ---------------------------------------------------------------------------
# Parsing: receipts, citations, bounds (pure)
# ---------------------------------------------------------------------------


class TestRetainParsing:
    async def test_ok_receipt_round_trips(self) -> None:
        provider = _RecordingProvider(_RETAIN_DOC_OK)
        capability = MemoryRetainCapability(provider)
        result = await capability(
            _call(
                {"scope": "server", "items": [{"content": "fact"}]},
                _context(),
                "memory.retain",
            )
        )
        assert result.ok is True
        document = json.loads(result.output)
        assert document["receipt"] == {
            "items_count": 1,
            "operation_id": None,
            "usage": {
                "input_tokens": 90,
                "output_tokens": 12,
                "total_tokens": 102,
            },
        }
        assert provider.calls == [f"retain:{SERVER_A_BANK}"]

    @pytest.mark.parametrize(
        "document,error_type",
        [
            ({"success": False, "items_count": 0}, "operation_unsuccessful"),
            ({"success": True}, "receipt_incomplete"),
            ({}, "operation_unsuccessful"),
        ],
    )
    def test_failed_write_documents_never_report_success(
        self, document: Mapping[str, object], error_type: str
    ) -> None:
        outcome = parse_retain_document(document, scope="server", bank=SERVER_A_BANK)
        assert outcome.status is MemoryStatus.UNAVAILABLE
        assert outcome.code is MemoryErrorCode.PROVIDER_ERROR
        assert outcome.error_type == error_type
        assert outcome.receipt is None
        rendered = retain_document(outcome)
        assert rendered["status"] == "unavailable"
        assert rendered["receipt"] is None

    def test_usage_absent_is_tolerated(self) -> None:
        outcome = parse_retain_document(
            {"success": True, "items_count": 2}, scope="shared:ops", bank=SHARED_BANK_PREFIX + "ops"
        )
        assert outcome.status is MemoryStatus.OK
        assert outcome.receipt is not None
        assert outcome.receipt.items_count == 2
        assert outcome.receipt.input_tokens is None


def _recall_hit(**overrides: object) -> dict[str, object]:
    hit: dict[str, object] = {
        "id": "mem-0001",
        "text": "the worker runs postgres 18",
        "type": "world",
        "occurred_start": "2026-10-18T00:00:00Z",
        "occurred_end": "2026-10-18T00:00:00Z",
        "mentioned_at": "2026-10-18T01:00:00Z",
        "document_id": "maint",
        "entities": ["postgres"],
        "tags": ["infra"],
        "scores": {"final": 0.83},
    }
    hit.update(overrides)
    return hit


class TestRecallParsing:
    def test_full_hit_keeps_every_provenance_field(self) -> None:
        outcome = parse_recall_document(
            _RECALL_DOC,
            scope="server",
            bank=SERVER_A_BANK,
            query="q",
            max_text_chars=DEFAULT_MAX_TEXT_CHARS,
        )
        assert outcome.status is MemoryStatus.OK
        hit = outcome.hits[0]
        assert hit.rank == 1
        assert hit.bank == SERVER_A_BANK
        assert hit.id == "mem-0001"
        assert hit.citation == f"memory:{SERVER_A_BANK}/mem-0001"
        assert hit.type == "world"
        assert hit.occurred_start == "2026-10-18T00:00:00Z"
        assert hit.mentioned_at == "2026-10-18T01:00:00Z"
        assert hit.document_id == "maint-2026-10-18"
        assert hit.entities == ("postgres",)
        assert hit.tags == ("infra",)
        assert hit.score == 0.83
        assert hit.truncated is False
        rendered = recall_document(outcome)
        # Upstream free-form metadata is deliberately not forwarded.
        assert "metadata" not in rendered["results"][0]

    def test_excerpt_budget_flags_truncation(self) -> None:
        long_text = "x" * 700
        outcome = parse_recall_document(
            {"results": [_recall_hit(text=long_text)]},
            scope="server",
            bank=SERVER_A_BANK,
            query="q",
            max_text_chars=600,
        )
        hit = outcome.hits[0]
        assert hit.truncated is True
        assert len(hit.text) == 600
        # The citation survives the cut (C07 rule 4).
        assert hit.citation == f"memory:{SERVER_A_BANK}/mem-0001"

    def test_uncitable_hits_are_skipped(self) -> None:
        outcome = parse_recall_document(
            {
                "results": [
                    {"text": "no id"},
                    {"id": "mem-2"},
                    "not-an-object",
                    _recall_hit(id="mem-3"),
                ]
            },
            scope="server",
            bank=SERVER_A_BANK,
            query="q",
            max_text_chars=600,
        )
        assert [hit.id for hit in outcome.hits] == ["mem-3"]

    def test_results_cap_flags_truncation(self) -> None:
        results = [_recall_hit(id=f"mem-{i:04d}") for i in range(MAX_RESULTS + 4)]
        outcome = parse_recall_document(
            {"results": results},
            scope="server",
            bank=SERVER_A_BANK,
            query="q",
            max_text_chars=600,
        )
        assert len(outcome.hits) == MAX_RESULTS
        assert outcome.results_truncated is True

    def test_malformed_document_is_provider_error(self) -> None:
        outcome = parse_recall_document(
            {"nope": True}, scope="server", bank=SERVER_A_BANK, query="q", max_text_chars=600
        )
        assert outcome.status is MemoryStatus.UNAVAILABLE
        assert outcome.code is MemoryErrorCode.PROVIDER_ERROR
        assert outcome.error_type == "invalid_provider_document"

    def test_bool_score_is_not_a_number(self) -> None:
        outcome = parse_recall_document(
            {"results": [_recall_hit(scores={"final": True})]},
            scope="server",
            bank=SERVER_A_BANK,
            query="q",
            max_text_chars=600,
        )
        assert outcome.hits[0].score is None


class TestReflectParsing:
    def test_answer_budget_and_based_on(self) -> None:
        document = {
            "text": "a" * 1500,
            "based_on": {"world": ["f-1", "f-2"], "experience": "f-3"},
        }
        outcome = parse_reflect_document(
            document,
            scope="server",
            bank=SERVER_A_BANK,
            query="q",
            max_answer_chars=1000,
        )
        assert outcome.status is MemoryStatus.OK
        assert outcome.truncated is True
        assert len(outcome.text) == 1000
        assert outcome.based_on_ids == ("f-1", "f-2", "f-3")

    def test_missing_text_is_provider_error(self) -> None:
        outcome = parse_reflect_document(
            {"nope": 1},
            scope="server",
            bank=SERVER_A_BANK,
            query="q",
            max_answer_chars=1000,
        )
        assert outcome.status is MemoryStatus.UNAVAILABLE
        assert outcome.error_type == "invalid_provider_document"


# ---------------------------------------------------------------------------
# Document/descriptor agreement (frozen output shape)
# ---------------------------------------------------------------------------


class TestDocumentDescriptorAgreement:
    def test_recall_document_satisfies_descriptor(self) -> None:
        descriptor = recall_descriptor()
        required = descriptor.output_schema["required"]
        ok = recall_document(
            parse_recall_document(
                _RECALL_DOC,
                scope="server",
                bank=SERVER_A_BANK,
                query="q",
                max_text_chars=600,
            )
        )
        down = recall_document(
            RecallOutcome(
                status=MemoryStatus.UNAVAILABLE,
                code=MemoryErrorCode.MEMORY_DISABLED,
                scope="server",
                bank=SERVER_A_BANK,
                query="q",
                hits=(),
                results_truncated=False,
                error_type=None,
            )
        )
        for document in (ok, down):
            for key in required:
                assert key in document, key

    def test_retain_document_satisfies_descriptor(self) -> None:
        descriptor = retain_descriptor()
        required = descriptor.output_schema["required"]
        ok = retain_document(
            parse_retain_document(_RETAIN_DOC_OK, scope="server", bank=SERVER_A_BANK)
        )
        down = retain_document(
            RetainOutcome(
                status=MemoryStatus.UNAVAILABLE,
                code=MemoryErrorCode.MEMORY_DISABLED,
                scope="server",
                bank=SERVER_A_BANK,
                receipt=None,
                error_type=None,
            )
        )
        for document in (ok, down):
            for key in required:
                assert key in document, key

    def test_reflect_document_satisfies_descriptor(self) -> None:
        descriptor = reflect_descriptor()
        required = descriptor.output_schema["required"]
        ok = reflect_document(
            parse_reflect_document(
                {"text": "answer", "based_on": {"world": ["f-1"]}},
                scope="server",
                bank=SERVER_A_BANK,
                query="q",
                max_answer_chars=1000,
            )
        )
        down = reflect_document(
            ReflectOutcome(
                status=MemoryStatus.UNAVAILABLE,
                code=MemoryErrorCode.MEMORY_DISABLED,
                scope="server",
                bank=SERVER_A_BANK,
                query="q",
                text="",
                truncated=False,
                based_on_ids=(),
                error_type=None,
            )
        )
        for document in (ok, down):
            for key in required:
                assert key in document, key

    def test_descriptors_pin_policy_fields(self) -> None:
        for descriptor, effect, scope in (
            (recall_descriptor(), "read", ()),
            (reflect_descriptor(), "read", ()),
            (retain_descriptor(), "mutate", ("memory.bank",)),
        ):
            assert descriptor.effect_class == effect
            assert tuple(descriptor.resource_scope) == scope
            assert tuple(descriptor.execution_environments) == ("controller",)
            assert tuple(descriptor.required_grants) == (descriptor.identifier,)
            # No payload field can name a bank, server or credential.
            properties = descriptor.input_schema["properties"]
            assert set(properties) <= {"scope", "query", "items", "types", "budget", "max_tokens"}


# ---------------------------------------------------------------------------
# Registry/policy/dispatcher integration (real producers, no database)
# ---------------------------------------------------------------------------


def _registry(descriptor) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(descriptor)
    return registry


def _grants(descriptor_id: str, *servers: str):
    return {descriptor_id: frozenset(uuid.UUID(server) for server in servers)}


def _policy(revision: int = 7, denied: tuple[str, ...] = ()) -> Policy:
    return Policy(revision=revision, denied_capabilities=frozenset(denied))


def _unused_approvals() -> ApprovalStore:
    """A real store over a lazily-connecting engine (never dialed)."""
    _, sessions = create_app_engine("postgresql://unused:unused@127.0.0.1:1/unused")
    return ApprovalStore(sessions)


def _dispatcher(registry: CapabilityRegistry, executor, *, grants=None) -> CapabilityDispatcher:
    return CapabilityDispatcher(
        registry=registry,
        approvals=_unused_approvals(),
        grants=(lambda: grants) if grants is not None else (lambda: {}),
        policy=lambda: _policy(),
        adopted=lambda server_id: True,
        credentials=lambda: None,
        executor=executor,
        output_budget_bytes=64 * 1024,
    )


class _RecordingCapability:
    def __init__(self, capability) -> None:
        self.capability = capability
        self.calls: list[CapabilityCall] = []

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        self.calls.append(call)
        return await self.capability(call)


class TestDispatcherIntegration:
    async def test_granted_recall_executes_without_approval(self) -> None:
        capability = _RecordingCapability(
            MemoryRecallCapability(_RecordingProvider(_RECALL_DOC))
        )
        registry = _registry(recall_descriptor())
        dispatcher = _dispatcher(
            registry, capability, grants=_grants("memory.recall", SERVER_A)
        )
        outcome: DispatchOutcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_A),
            capability_id="memory.recall",
            payload={"scope": "server", "query": "postgres version"},
        )
        assert outcome.allowed is True
        assert outcome.effect == "executed"
        assert outcome.executor_ok is True
        assert outcome.approval_id is None  # read effect needs no approval
        document = json.loads(outcome.output.text)
        assert document["status"] == "ok"
        assert document["results"][0]["citation"].startswith("memory:")

    async def test_retain_as_mutate_executes_in_delegated_mode(self) -> None:
        capability = _RecordingCapability(
            MemoryRetainCapability(_RecordingProvider(_RETAIN_DOC_OK))
        )
        registry = _registry(retain_descriptor())
        dispatcher = _dispatcher(
            registry, capability, grants=_grants("memory.retain", SERVER_A)
        )
        outcome = await dispatcher.dispatch(
            context=_context(
                server_id=SERVER_A,
                allowed=("memory.retain",),
            ),
            capability_id="memory.retain",
            payload={"scope": "server", "items": [{"content": "fact"}]},
        )
        assert outcome.allowed is True
        assert outcome.effect == "executed"
        assert outcome.executor_ok is True

    async def test_server_outside_grant_scope_denied_before_executor(self) -> None:
        capability = _RecordingCapability(
            MemoryRecallCapability(_RecordingProvider(_RECALL_DOC))
        )
        registry = _registry(recall_descriptor())
        dispatcher = _dispatcher(
            registry, capability, grants=_grants("memory.recall", SERVER_A)
        )
        outcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_B),
            capability_id="memory.recall",
            payload={"scope": "server", "query": "q"},
        )
        assert outcome.allowed is False
        assert capability.calls == []
        assert outcome.effect == "not_attempted"

    async def test_policy_deny_list_blocks_memory(self) -> None:
        capability = _RecordingCapability(
            MemoryRecallCapability(_RecordingProvider(_RECALL_DOC))
        )
        registry = _registry(recall_descriptor())
        dispatcher = CapabilityDispatcher(
            registry=registry,
            approvals=_unused_approvals(),
            grants=lambda: _grants("memory.recall", SERVER_A),
            policy=lambda: _policy(denied=("memory.recall",)),
            adopted=lambda server_id: True,
            credentials=lambda: None,
            executor=capability,
            output_budget_bytes=64 * 1024,
        )
        outcome = await dispatcher.dispatch(
            context=_context(server_id=SERVER_A),
            capability_id="memory.recall",
            payload={"scope": "server", "query": "q"},
        )
        assert outcome.allowed is False
        assert capability.calls == []


# ---------------------------------------------------------------------------
# Production entrypoint: real subprocess against a Hindsight-shaped fixture
# ---------------------------------------------------------------------------


class _HindsightStubHandler(BaseHTTPRequestHandler):
    """Loopback Hindsight stand-in: pinned 1.0.0 response shapes, records
    every request (the controlled upstream of the entrypoint checks)."""

    requests: list[dict[str, object]] = []

    def _record(self, body: object) -> None:
        self.requests.append(
            {"method": self.command, "path": self.path, "body": body}
        )

    def _send(self, document: object, code: int = 200) -> None:
        payload = json.dumps(document).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    @property
    def bare_path(self) -> str:
        return self.path.partition("?")[0].rstrip("/")

    def _body(self) -> object:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return None
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self._record(None)
        path = self.bare_path
        if path.endswith("/version"):
            self._send({"api_version": "1.0.0-stub", "features": {}})
        elif path.endswith("/export"):
            self._send(
                {
                    "version": "1",
                    "bank": {"bank_id": "fixture"},
                    "memories": [{"id": "mem-0001", "text": "fact"}],
                }
            )
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        body = self._body()
        self._record(body)
        path = self.bare_path
        if path.endswith("/memories/recall"):
            self._send(_RECALL_DOC)
        elif path.endswith("/reflect"):
            self._send(
                {
                    "text": "The worker runs PostgreSQL 18.",
                    "based_on": {"world": ["mem-0001"]},
                }
            )
        elif path.endswith("/memories"):
            items = (body or {}).get("items", [])  # type: ignore[union-attr]
            self._send(
                {
                    "success": True,
                    "bank_id": path.split("/")[-2],
                    "items_count": len(items),
                    "async": False,
                    "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                }
            )
        else:
            self._send({"error": "not found"}, 404)

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib name
        self._record(None)
        if self.bare_path.endswith("/memories"):
            self._send({"success": True, "deleted_count": 3})
        else:
            self._send({"success": True, "deleted_count": 9})

    def log_message(self, *args) -> None:  # noqa: D102 - quiet fixture
        pass


@pytest.fixture
def hindsight_stub():
    """The controlled Hindsight on loopback; records every request."""
    _HindsightStubHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HindsightStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mai.memory.hindsight", *args],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        timeout=120,
    )


class TestCliEntrypoint:
    def test_status_probe(self, hindsight_stub: str) -> None:
        proc = _cli("status", "--base-url", hindsight_stub)
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout)
        assert document["status"] == "ok"
        assert document["api_version"] == "1.0.0-stub"

    def test_recall_produces_cited_hits(self, hindsight_stub: str) -> None:
        proc = _cli(
            "recall",
            "--base-url",
            hindsight_stub,
            "--scope",
            "server",
            "--query",
            "postgres version",
        )
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout)
        assert document["status"] == "ok"
        assert document["bank"] == SERVER_BANK_PREFIX + "00000000-0000-0000-0000-000000000000"
        hit = document["results"][0]
        assert hit["citation"].startswith("memory:")
        assert hit["type"] == "world"
        assert document["trust"] == "operational_memory"
        assert document["role"] == "historical_context"
        # The stub saw exactly the screened minimal request.
        assert len(_HindsightStubHandler.requests) == 1
        assert _HindsightStubHandler.requests[0]["path"].endswith("/memories/recall")

    def test_retain_receipt_and_synchronous_body(self, hindsight_stub: str) -> None:
        proc = _cli(
            "retain",
            "--base-url",
            hindsight_stub,
            "--scope",
            "shared:ops",
            "--shared-banks",
            "ops",
            "--item",
            "the worker was restarted",
            "--item",
            "the worker runs postgres 18",
        )
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout)
        assert document["status"] == "ok"
        assert document["bank"] == SHARED_BANK_PREFIX + "ops"
        assert document["receipt"]["items_count"] == 2
        request = _HindsightStubHandler.requests[0]
        assert request["path"].endswith("/memories")
        body = request["body"]
        assert body["async"] is False  # synchronous: the receipt is evidence
        assert [item["content"] for item in body["items"]] == [
            "the worker was restarted",
            "the worker runs postgres 18",
        ]

    def test_sensitive_retain_is_captured_by_nothing(self, hindsight_stub: str) -> None:
        proc = _cli(
            "retain",
            "--base-url",
            hindsight_stub,
            "--scope",
            "server",
            "--item",
            f"credentials api_key={SYNTHETIC_SECRET}",
        )
        assert proc.returncode == 1
        document = json.loads(proc.stdout)
        assert document["kind"] == "error"
        assert document["code"] == "sensitive_content"
        assert SYNTHETIC_SECRET not in proc.stdout
        assert _HindsightStubHandler.requests == []  # nothing left the process

    def test_forbidden_shared_scope_is_a_rejection(self, hindsight_stub: str) -> None:
        proc = _cli(
            "recall",
            "--base-url",
            hindsight_stub,
            "--scope",
            "shared:not-wired",
            "--query",
            "q",
        )
        assert proc.returncode == 1
        document = json.loads(proc.stdout)
        assert document["code"] == "bank_forbidden"
        assert _HindsightStubHandler.requests == []

    def test_reflect_and_delete_and_export(self, hindsight_stub: str) -> None:
        proc = _cli(
            "reflect",
            "--base-url",
            hindsight_stub,
            "--scope",
            "server",
            "--query",
            "what does the worker run?",
        )
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout)
        assert document["status"] == "ok"
        assert document["text"].startswith("The worker")
        assert document["based_on"] == ["mem-0001"]

        proc = _cli(
            "delete-memories",
            "--base-url",
            hindsight_stub,
            "--scope",
            "server",
            "--type",
            "world",
        )
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout)
        assert document["status"] == "ok"
        assert document["deleted_count"] == 3

        proc = _cli(
            "delete-bank",
            "--base-url",
            hindsight_stub,
            "--scope",
            "shared:ops",
            "--shared-banks",
            "ops",
            "--yes",
        )
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout)
        assert document["deleted_count"] == 9

        proc = _cli("export", "--base-url", hindsight_stub, "--scope", "server")
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout)
        assert document["status"] == "ok"
        assert document["manifest"]["memories"][0]["id"] == "mem-0001"

    def test_refused_deletion_without_confirmation(self, hindsight_stub: str) -> None:
        proc = _cli(
            "delete-bank", "--base-url", hindsight_stub, "--scope", "server"
        )
        assert proc.returncode == 2  # argparse: --yes required
        assert _HindsightStubHandler.requests == []

    def test_unreachable_service_is_honest_unavailable(self) -> None:
        """Real TCP refusal on a closed loopback port: exit 0 with an
        honest ``provider_unreachable`` answer — never a crash, never a
        fake success (graceful degradation, service-outage shape)."""
        proc = _cli(
            "recall",
            "--base-url",
            "http://127.0.0.1:1",
            "--scope",
            "server",
            "--query",
            "q",
            "--timeout-seconds",
            "5",
        )
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout)
        assert document["status"] == "unavailable"
        assert document["code"] == "provider_unreachable"
        assert document["results"] == []

    def test_usage_error_exit_code(self) -> None:
        proc = _cli("nonsense")
        assert proc.returncode == 2
