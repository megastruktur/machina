"""Documents HTTP API focused tests (C01/C02/C07/C08).

Live-boundary discipline (mirrors tests/test_servers_http.py and
tests/test_chat_http.py): the claims below run against a real disposable
Postgres fixture (``MAI_TEST_DATABASE_URL``), the real ASGI callable, the
real session/origin/CSRF boundary and the real canonical file store. The
embedding seam is a loopback OpenAI-compatible fixture server, so the
real ``GatewayEmbedder`` + ``ScopeRetriever`` + ``ScopeIndexer`` path is
exercised; no producer is mocked. Fixture identities are frozen
TEST-NET-style UUIDs; the fixture password is a synthetic constant.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.knowledge.index import EMBEDDING_DIMENSIONS

pytestmark = pytest.mark.anyio

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry
EVIL_ORIGIN = "https://attacker.example"
FIXTURE_PASSWORD = "fixture-password-07-docs-api-only"

# Frozen fixture identities (canonical lowercase UUID strings, TEST-NET style).
SERVER_A = "6e4f5a1b-2c3d-4e5f-9a0b-1c2d3e4f5a60"
SERVER_B = "7a5b6c1d-8e2f-4a3b-9c4d-5e6f7a8b9c0d"
SERVER_C = "8b6c7d2e-9f3a-4b5c-8d9e-0f1a2b3c4d5e"
RUN_OK = "8b9c0d1e-2f3a-4b5c-8d7e-9f0a1b2c3d4e"
RUN_FAIL = "0d1e2f3a-4b5c-6d7e-8f9a-0b1c2d3e4f5a"

# Distinctive single-token markers for FTS-traceable retrieval assertions.
MARKER_A = "zetarestartomega"
MARKER_B = "bravorestoretheta"
MARKER_SHARED = "alphacatalogdelta"
MARKER_RUNBOOK = "gammaincidentdelta"


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state."""
    with psycopg.connect(url) as conn:
        # Whole-schema reset (D09): FK-safe and immune to future revisions
        # (the enumerated drop list went stale across migrations).
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the documents HTTP surface claims "
            "need a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_app_tables(url)
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


class _EmbeddingsHandler(BaseHTTPRequestHandler):
    """Loopback OpenAI-compatible embeddings endpoint (deterministic).

    One distinct unit vector per request index: offline-embedder
    semantics identical to tests/test_index.py ``_FakeEmbedder``, served
    over HTTP so the real ``GatewayEmbedder`` client path is exercised.
    """

    server_version = "mai-fxt-embeddings/1.0"

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        if self.path.rstrip("/") not in ("/v1/embeddings", "/embeddings"):
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            document = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self.send_response(400)
            self.end_headers()
            return
        if getattr(self.server, "fail", False):  # type: ignore[attr-defined]
            self.send_response(503)
            self.end_headers()
            return
        texts = document.get("input")
        if not isinstance(texts, list):
            self.send_response(422)
            self.end_headers()
            return
        data = []
        for index, _ in enumerate(texts):
            vector = [0.0] * EMBEDDING_DIMENSIONS
            vector[index % EMBEDDING_DIMENSIONS] = 1.0
            data.append({"index": index, "embedding": vector})
        payload = json.dumps(
            {
                "object": "list",
                "model": "text-embedding-3-small",
                "data": data,
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return  # value-free fixture: no request logging


@pytest.fixture(scope="session")
def embeddings_url() -> Iterator[str]:  # noqa: F821
    """The loopback embeddings fixture; lives for the test session."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app


def _truncate(url: str) -> None:
    """Reset per-test SQL truth (sessions, projection, knowledge index)."""
    with psycopg.connect(url) as conn:
        conn.execute(
            "TRUNCATE admin_sessions, admin_credential, "
            "profile_projection, profile_projection_journal, "
            "knowledge_documents, knowledge_doc_chunks CASCADE"
        )
        conn.commit()


@pytest.fixture
async def docs_app(migrated_url: str, embeddings_url: str, tmp_path: Path):
    """App on the real fixture DB with a fresh confined data root."""
    _truncate(migrated_url)
    app = create_app(
        env={
            "MAI_DATABASE_URL": migrated_url,
            "MAI_PUBLIC_ORIGINS": ORIGIN,
            "MAI_DATA_ROOT": str(tmp_path / "data-root"),
            "MAI_EMBEDDINGS_BASE_URL": embeddings_url,
        }
    )
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app


def _truncate(url: str) -> None:
    """Reset per-test SQL truth (sessions, projection, knowledge index)."""
    with psycopg.connect(url) as conn:
        conn.execute(
            "TRUNCATE admin_sessions, admin_credential, "
            "profile_projection, profile_projection_journal, "
            "knowledge_documents, knowledge_doc_chunks CASCADE"
        )
        conn.commit()


@pytest.fixture
async def docs_app(migrated_url: str, embeddings_url: str, tmp_path: Path):
    """App on the real fixture DB with a fresh confined data root."""
    _truncate(migrated_url)
    app = create_app(
        env={
            "MAI_DATABASE_URL": migrated_url,
            "MAI_PUBLIC_ORIGINS": ORIGIN,
            "MAI_DATA_ROOT": str(tmp_path / "data-root"),
            "MAI_EMBEDDINGS_BASE_URL": embeddings_url,
        }
    )
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
def embeddings_ctl(embeddings_url: str):
    """Handle to the session fixture server for outage toggling."""

    server_holder: dict[str, Any] = {}

    def _register(server: Any) -> None:
        server_holder["server"] = server

    yield _EmbeddingsCtl(server_holder)


class _EmbeddingsCtl:
    def __init__(self, holder: dict[str, Any]) -> None:
        self._holder = holder

    def set_fail(self, fail: bool) -> None:
        # Re-find the running session server by port is unnecessary: the
        # fixture handler reads the flag from its server instance; the
        # test module keeps the instance reachable through the fixture.
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Minimal ASGI harness (no test-client dependency; bodies + cookies)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HttpResponse:
    status: int = 200
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        wanted = name.lower().encode()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value.decode()
        return None

    def header_all(self, name: str) -> list[str]:
        wanted = name.lower().encode()
        return [
            value.decode()
            for key, value in self.headers
            if key.lower() == wanted
        ]

    def json(self) -> Any:
        return json.loads(self.body.decode())


async def call_app(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
    cookies: Mapping[str, str] | None = None,
    client: tuple[str, int] = ("127.0.0.1", 0),
) -> HttpResponse:
    """Run one HTTP request through the real ASGI callable.

    ``path`` may carry a ``?query`` suffix (split into the ASGI query
    string, mirroring what a real server does).
    """
    raw_path, _, query = path.partition("?")
    scope_headers = [(b"host", b"fixture")]
    scope_headers.extend(
        (key.encode("ascii").lower(), value.encode("ascii"))
        for key, value in (headers or {}).items()
    )
    if cookies:
        scope_headers.append(
            (b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode())
        )
    body = b""
    if json_body is not None:
        body = json.dumps(json_body).encode()
        scope_headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "root_path": "",
        "headers": scope_headers,
        "client": client,
        "server": ("fixture", 80),
    }
    response = HttpResponse()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response.status = message["status"]
            response.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            response.body += message.get("body", b"")

    try:
        await app(scope, receive, send)
    except Exception:  # noqa: BLE001 - ServerErrorMiddleware re-raises after reply
        pass
    return response


def _set_cookies(response: HttpResponse) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in response.header_all("set-cookie"):
        name, _, value = raw.partition("=")
        parsed[name] = value.split(";", 1)[0]
    return parsed


def _error(response: HttpResponse) -> dict[str, Any]:
    envelope = response.json()
    assert set(envelope) == {"error"}
    body = envelope["error"]
    assert set(body) == {"code", "message", "request_id", "fields"}
    assert "Traceback" not in response.body.decode()
    return body


async def login_session(app: FastAPI) -> dict[str, str]:
    """Bootstrap once and return the authenticated cookie jar."""
    bootstrap = await call_app(
        app,
        "POST",
        "/api/v1/auth/bootstrap",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert bootstrap.status == 201, bootstrap.body
    login = await call_app(
        app,
        "POST",
        "/api/v1/auth/login",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert login.status == 204, login.body
    jar = _set_cookies(login)
    assert SESSION_COOKIE in jar and CSRF_COOKIE in jar
    return jar


def _mutation_headers(jar: Mapping[str, str]) -> dict[str, str]:
    return {
        "origin": ORIGIN,
        "x-csrf-token": jar[CSRF_COOKIE],
    }


# ---------------------------------------------------------------------------
# Route helpers
# ---------------------------------------------------------------------------


async def create_doc(
    app: FastAPI,
    jar: Mapping[str, str],
    body: Mapping[str, Any],
) -> HttpResponse:
    return await call_app(
        app,
        "POST",
        "/api/v1/docs",
        json_body=dict(body),
        headers=_mutation_headers(jar),
        cookies=jar,
    )


async def get_doc(
    app: FastAPI,
    jar: Mapping[str, str],
    doc_id: str,
    query: str = "",
) -> HttpResponse:
    path = f"/api/v1/docs/{doc_id}"
    if query:
        path = f"{path}?{query}"
    return await call_app(app, "GET", path, cookies=jar)


async def put_doc(
    app: FastAPI,
    jar: Mapping[str, str],
    doc_id: str,
    body: Mapping[str, Any],
) -> HttpResponse:
    return await call_app(
        app,
        "PUT",
        f"/api/v1/docs/{doc_id}",
        json_body=dict(body),
        headers=_mutation_headers(jar),
        cookies=jar,
    )


async def delete_doc(
    app: FastAPI,
    jar: Mapping[str, str],
    doc_id: str,
    body: Mapping[str, Any],
) -> HttpResponse:
    return await call_app(
        app,
        "DELETE",
        f"/api/v1/docs/{doc_id}",
        json_body=dict(body),
        headers=_mutation_headers(jar),
        cookies=jar,
    )


async def retrieve(
    app: FastAPI,
    jar: Mapping[str, str],
    body: Mapping[str, Any],
) -> HttpResponse:
    return await call_app(
        app,
        "POST",
        "/api/v1/docs/retrieve",
        json_body=dict(body),
        headers=_mutation_headers(jar),
        cookies=jar,
    )


async def reconcile(
    app: FastAPI,
    jar: Mapping[str, str],
    body: Mapping[str, Any],
) -> HttpResponse:
    return await call_app(
        app,
        "POST",
        "/api/v1/docs/index/reconcile",
        json_body=dict(body),
        headers=_mutation_headers(jar),
        cookies=jar,
    )


async def index_status(
    app: FastAPI,
    jar: Mapping[str, str],
    query: str,
) -> HttpResponse:
    return await call_app(
        app, "GET", f"/api/v1/docs/index?{query}", cookies=jar
    )


async def generate(
    app: FastAPI,
    jar: Mapping[str, str],
    body: Mapping[str, Any],
) -> HttpResponse:
    return await call_app(
        app,
        "POST",
        "/api/v1/docs/generate",
        json_body=dict(body),
        headers=_mutation_headers(jar),
        cookies=jar,
    )


async def research(
    app: FastAPI,
    jar: Mapping[str, str],
    body: Mapping[str, Any],
) -> HttpResponse:
    return await call_app(
        app,
        "POST",
        "/api/v1/docs/research",
        json_body=dict(body),
        headers=_mutation_headers(jar),
        cookies=jar,
    )


async def create_server(
    app: FastAPI,
    jar: Mapping[str, str],
    name: str,
) -> HttpResponse:
    return await call_app(
        app,
        "POST",
        "/api/v1/servers",
        json_body={
            "name": name,
            "prompt_revision": 1,
            "environment": "linux_ssh",
            "ssh": {
                "host": f"{name}.example.internal",
                "port": 22,
                "user": "deploy",
                "host_key": "SHA256:" + "A" * 43,  # OpenSSH fingerprint shape
                "credential_ref": "MAI_FXT_SSH_KEY",
            },
            "privilege": "sudo",
            "capabilities": ["site.deploy"],
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )


async def grant_retrieval(
    app: FastAPI,
    jar: Mapping[str, str],
    server_id: str,
) -> None:
    response = await call_app(
        app,
        "PUT",
        "/api/v1/grants",
        json_body={
            "expected_revision": 0,
            "denied_capabilities": [],
            "grants": [
                {"capability_id": "docs.retrieve", "server_ids": [server_id]}
            ],
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status == 200, response.body


def _evidence(
    *,
    run_id: str = RUN_OK,
    server_id: str = SERVER_A,
    status: str = "succeeded",
    host_verified: bool = True,
    summary: str = f"{MARKER_RUNBOOK} restart procedure completed.",
    error: str | None = None,
    step_status: str = "succeeded",
    step_verified: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "v": 1,
        "run_id": run_id,
        "server_id": server_id,
        "status": status,
        "host_verified": host_verified,
        "summary": summary,
        "steps": [
            {
                "seq": 1,
                "capability_id": "host.shell",
                "status": step_status,
                "verified": step_verified,
                "display": "restart fixture service",
                "exit_code": 0 if step_status == "succeeded" else 1,
                "evidence_refs": ["receipts/op-7d1c/state"],
            }
        ],
    }
    if error is not None:
        payload["error"] = error
    return payload


# ---------------------------------------------------------------------------
# Auth boundary (C08 ADR §5; every route joins the shared boundary)
# ---------------------------------------------------------------------------


class TestAuthBoundary:
    async def test_list_requires_session(self, docs_app: FastAPI):
        response = await call_app(
            docs_app,
            "GET",
            f"/api/v1/docs?server_id={SERVER_A}",
        )
        assert response.status == 401
        assert _error(response)["code"] == "unauthenticated"

    async def test_create_requires_csrf(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        response = await call_app(
            docs_app,
            "POST",
            "/api/v1/docs",
            json_body={"server_id": SERVER_A, "content": "# hi"},
            headers={"origin": ORIGIN},
            cookies=jar,
        )
        assert response.status == 403
        assert _error(response)["code"] == "csrf_rejected"

    async def test_create_rejects_foreign_origin(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        response = await call_app(
            docs_app,
            "POST",
            "/api/v1/docs",
            json_body={"server_id": SERVER_A, "content": "# hi"},
            headers={"origin": EVIL_ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]},
            cookies=jar,
        )
        assert response.status == 403
        assert _error(response)["code"] == "origin_rejected"

    async def test_retrieve_requires_session(self, docs_app: FastAPI):
        response = await call_app(
            docs_app,
            "POST",
            "/api/v1/docs/retrieve",
            json_body={"server_id": SERVER_A, "query": "anything"},
        )
        assert response.status == 401

    async def test_research_requires_csrf(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        response = await call_app(
            docs_app,
            "POST",
            "/api/v1/docs/research",
            json_body={"server_id": SERVER_A, "payload": {"question": "q"}},
            headers={"origin": ORIGIN},
            cookies=jar,
        )
        assert response.status == 403


# ---------------------------------------------------------------------------
# Document CRUD (C01 identity/revision; C02 canonical pipeline; C07 rule 1)
# ---------------------------------------------------------------------------


class TestDocumentCrud:
    async def test_create_read_roundtrip_with_provenance(
        self, docs_app: FastAPI
    ):
        jar = await login_session(docs_app)
        created = await create_doc(
            docs_app,
            jar,
            {
                "server_id": SERVER_A,
                "namespace": "docs",
                "content": f"# Ops\n\n{MARKER_A} restart steps.",
            },
        )
        assert created.status == 201, created.body
        body = created.json()
        doc_id = body["doc_id"]
        assert body["revision"] == 1
        assert body["source_kind"] == "authored"
        assert body["origin"] == "operator"
        assert body["verification"] == "unverified"
        assert body["namespace"] == "docs"
        assert body["citation"] == f"docs:{doc_id}#r1"
        assert body["server_id"] == SERVER_A

        read = await get_doc(docs_app, jar, doc_id, f"server_id={SERVER_A}")
        assert read.status == 200, read.body
        read_body = read.json()
        assert read_body["content"] == f"# Ops\n\n{MARKER_A} restart steps."
        assert read_body["revision"] == 1
        assert len(read_body["sha256"]) == 64

    async def test_edit_revision_checked_and_stale_rejects(
        self, docs_app: FastAPI
    ):
        jar = await login_session(docs_app)
        created = await create_doc(
            docs_app, jar, {"server_id": SERVER_A, "content": "# v1"}
        )
        doc_id = created.json()["doc_id"]

        edited = await put_doc(
            docs_app,
            jar,
            doc_id,
            {
                "server_id": SERVER_A,
                "content": "# v2",
                "expected_revision": 1,
            },
        )
        assert edited.status == 200, edited.body
        assert edited.json()["revision"] == 2

        stale = await put_doc(
            docs_app,
            jar,
            doc_id,
            {
                "server_id": SERVER_A,
                "content": "# v3",
                "expected_revision": 1,
            },
        )
        assert stale.status == 409
        assert _error(stale)["code"] == "conflict"

        reread = await get_doc(docs_app, jar, doc_id, f"server_id={SERVER_A}")
        assert reread.json()["content"] == "# v2"

    async def test_delete_emits_durable_tombstone(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        created = await create_doc(
            docs_app, jar, {"server_id": SERVER_A, "content": "# gone"}
        )
        doc_id = created.json()["doc_id"]
        deleted = await delete_doc(
            docs_app,
            jar,
            doc_id,
            {"server_id": SERVER_A, "expected_revision": 1},
        )
        assert deleted.status == 200, deleted.body
        intent = deleted.json()["intent"]
        assert intent["op"] == "delete"
        assert intent["tombstone"] is True
        assert intent["status"] == "pending"

        read = await get_doc(docs_app, jar, doc_id, f"server_id={SERVER_A}")
        assert read.status == 404
        assert _error(read)["code"] == "not_found"

        status = await index_status(docs_app, jar, f"server_id={SERVER_A}")
        assert status.status == 200
        pending = status.json()["pending"]
        assert any(
            entry["doc_id"] == doc_id and entry["op"] == "delete"
            for entry in pending
        )

    async def test_wrong_scope_read_finds_nothing(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        created = await create_doc(
            docs_app, jar, {"server_id": SERVER_A, "content": "# scoped"}
        )
        doc_id = created.json()["doc_id"]
        foreign = await get_doc(
            docs_app, jar, doc_id, f"server_id={SERVER_B}"
        )
        assert foreign.status == 404
        assert _error(foreign)["code"] == "not_found"

    async def test_shared_scope_roundtrip(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        created = await create_doc(
            docs_app,
            jar,
            {
                "shared": True,
                "namespace": "runbooks",
                "content": f"# Shared\n\n{MARKER_SHARED}.",
            },
        )
        assert created.status == 201, created.body
        doc_id = created.json()["doc_id"]
        assert created.json()["server_id"] is None
        read = await get_doc(docs_app, jar, doc_id, "shared=1&namespace=runbooks")
        assert read.status == 200
        assert read.json()["namespace"] == "runbooks"

    async def test_app_scope_requires_server(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        response = await create_doc(
            docs_app,
            jar,
            {"app_id": str(uuid.uuid4()), "content": "# orphan"},
        )
        assert response.status == 422
        assert _error(response)["code"] == "validation_failed"

    async def test_ambiguous_scope_rejected(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        response = await create_doc(
            docs_app,
            jar,
            {"server_id": SERVER_A, "shared": True, "content": "# both"},
        )
        assert response.status == 422

    async def test_create_requires_exactly_one_source(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        neither = await create_doc(
            docs_app, jar, {"server_id": SERVER_A}
        )
        assert neither.status == 422
        both = await create_doc(
            docs_app,
            jar,
            {
                "server_id": SERVER_A,
                "content": "# x",
                "source_path": "/tmp/whatever.md",
            },
        )
        assert both.status == 422

    async def test_import_outside_file_is_explicit(
        self, docs_app: FastAPI, tmp_path: Path
    ):
        jar = await login_session(docs_app)
        source = tmp_path / "outside.md"
        source.write_text(f"# Imported\n\n{MARKER_A} outside origin.", "utf-8")
        imported = await create_doc(
            docs_app,
            jar,
            {"server_id": SERVER_A, "source_path": str(source)},
        )
        assert imported.status == 201, imported.body
        assert imported.json()["source_kind"] == "imported"
        assert imported.json()["origin"].startswith("import:")
        doc_id = imported.json()["doc_id"]
        read = await get_doc(docs_app, jar, doc_id, f"server_id={SERVER_A}")
        assert read.status == 200
        assert "outside origin" in read.json()["content"]

    async def test_import_secret_name_refused_without_materializing(
        self, docs_app: FastAPI, tmp_path: Path
    ):
        jar = await login_session(docs_app)
        secret = tmp_path / ".env"
        secret.write_text("TOKEN=fixture", "utf-8")
        response = await create_doc(
            docs_app,
            jar,
            {"server_id": SERVER_A, "source_path": str(secret)},
        )
        assert response.status == 422
        error = _error(response)
        assert error["code"] == "validation_failed"
        assert error["fields"] == {"document": "secret_file_forbidden"}
        # Nothing indexable exists for the refused import: the scope's
        # durable outbox stayed empty.
        status = await index_status(docs_app, jar, f"server_id={SERVER_A}")
        assert status.json()["pending"] == []

    async def test_import_symlink_refused(self, docs_app: FastAPI, tmp_path: Path):
        jar = await login_session(docs_app)
        real = tmp_path / "real.md"
        real.write_text("# real", "utf-8")
        link = tmp_path / "link.md"
        link.symlink_to(real)
        response = await create_doc(
            docs_app,
            jar,
            {"server_id": SERVER_A, "source_path": str(link)},
        )
        assert response.status == 422
        assert _error(response)["fields"] == {"document": "symlink_forbidden"}

    async def test_list_uses_projection_after_reconcile(
        self, docs_app: FastAPI
    ):
        jar = await login_session(docs_app)
        created = await create_doc(
            docs_app,
            jar,
            {"server_id": SERVER_A, "content": f"# Listable\n\n{MARKER_A}."},
        )
        doc_id = created.json()["doc_id"]
        listed_before = await call_app(
            docs_app,
            "GET",
            f"/api/v1/docs?server_id={SERVER_A}",
            cookies=jar,
        )
        assert listed_before.status == 200
        assert listed_before.json()["documents"] == []

        reconciled = await reconcile(docs_app, jar, {"server_id": SERVER_A})
        assert reconciled.status == 200, reconciled.body
        listed = await call_app(
            docs_app,
            "GET",
            f"/api/v1/docs?server_id={SERVER_A}",
            cookies=jar,
        )
        assert listed.status == 200
        entries = listed.json()["documents"]
        assert [entry["doc_id"] for entry in entries] == [doc_id]
        assert entries[0]["revision"] == 1
        assert entries[0]["source_kind"] == "authored"
        assert entries[0]["citation"] == f"docs:{doc_id}#r1"


# ---------------------------------------------------------------------------
# Retrieval (C07 rules 2/3/4: storage-boundary scope, citations, honesty)
# ---------------------------------------------------------------------------


class TestRetrieval:
    async def _seed(self, docs_app: FastAPI, jar: Mapping[str, str]) -> None:
        for body in (
            {
                "server_id": SERVER_A,
                "content": f"# A\n\n{MARKER_A} restart the queue worker.",
            },
            {
                "server_id": SERVER_B,
                "content": f"# B\n\n{MARKER_B} restore the queue worker.",
            },
            {
                "shared": True,
                "content": f"# S\n\n{MARKER_SHARED} catalog of services.",
            },
        ):
            created = await create_doc(docs_app, jar, body)
            assert created.status == 201, created.body
        reconciled = await reconcile(
            docs_app, jar, {"server_id": SERVER_A}
        )
        assert reconciled.status == 200, reconciled.body
        reconciled_b = await reconcile(
            docs_app, jar, {"server_id": SERVER_B}
        )
        assert reconciled_b.status == 200, reconciled_b.body
        reconciled_s = await reconcile(docs_app, jar, {"shared": True})
        assert reconciled_s.status == 200, reconciled_s.body

    async def test_retrieval_citations_and_scope_isolation(
        self, docs_app: FastAPI
    ):
        jar = await login_session(docs_app)
        await self._seed(docs_app, jar)

        result = await retrieve(
            docs_app,
            jar,
            {"server_id": SERVER_A, "query": f"{MARKER_A} queue worker"},
        )
        assert result.status == 200, result.body
        body = result.json()
        assert body["status"] == "ok"
        assert body["scope"] == "server"
        assert body["server_id"] == SERVER_A
        assert len(body["excerpts"]) >= 1
        assert any(MARKER_A in item["excerpt"] for item in body["excerpts"])
        excerpt = next(
            item for item in body["excerpts"] if MARKER_A in item["excerpt"]
        )
        assert excerpt["doc_id"] != ""
        assert excerpt["revision"] >= 1
        assert len(excerpt["sha256"]) == 64
        assert excerpt["ord"] >= 0
        assert excerpt["citation"].startswith("docs:")
        assert excerpt["source_kind"] == "authored"
        # Storage-boundary isolation: the other server's matching doc and
        # the shared doc never appear in a server-scoped answer.
        assert all(
            excerpt["doc_id"] != "" for excerpt in body["excerpts"]
        )
        for item in body["excerpts"]:
            read = await get_doc(
                docs_app, jar, item["doc_id"], f"server_id={SERVER_A}"
            )
            assert read.status == 200, read.body

    async def test_shared_scope_answers_only_shared(
        self, docs_app: FastAPI
    ):
        jar = await login_session(docs_app)
        await self._seed(docs_app, jar)
        result = await retrieve(
            docs_app,
            jar,
            {"shared": True, "query": f"{MARKER_SHARED} catalog"},
        )
        assert result.status == 200, result.body
        body = result.json()
        assert body["status"] == "ok"
        assert body["scope"] == "shared"
        assert body["server_id"] is None
        assert any(
            MARKER_SHARED in item["excerpt"] for item in body["excerpts"]
        )

    async def test_zero_matches_is_ok_not_unavailable(
        self, docs_app: FastAPI
    ):
        # An empty scope (no documents, never indexed) is the zero-results
        # state: status ok with an empty excerpt list, never "unavailable".
        jar = await login_session(docs_app)
        await self._seed(docs_app, jar)
        result = await retrieve(
            docs_app,
            jar,
            {
                "server_id": SERVER_C,
                "query": f"{MARKER_A} queue worker",
            },
        )
        assert result.status == 200
        body = result.json()
        assert body["status"] == "ok"
        assert body["server_id"] == SERVER_C
        assert body["excerpts"] == []


# ---------------------------------------------------------------------------
# Index status + reconcile (AC2: pending/failed explicit)
# ---------------------------------------------------------------------------


class TestIndexStatus:
    async def test_pending_then_indexed_then_deleted(
        self, docs_app: FastAPI
    ):
        jar = await login_session(docs_app)
        created = await create_doc(
            docs_app,
            jar,
            {"server_id": SERVER_A, "content": f"# Pending\n\n{MARKER_A}."},
        )
        doc_id = created.json()["doc_id"]

        before = await index_status(docs_app, jar, f"server_id={SERVER_A}")
        assert before.status == 200
        before_body = before.json()
        assert before_body["projection"] == []
        pending = before_body["pending"]
        assert [entry["doc_id"] for entry in pending] == [doc_id]
        assert pending[0]["status"] == "pending"
        assert pending[0]["op"] == "upsert"

        reconciled = await reconcile(docs_app, jar, {"server_id": SERVER_A})
        assert reconciled.status == 200
        outcomes = reconciled.json()["outcomes"]
        assert [outcome["doc_id"] for outcome in outcomes] == [doc_id]
        assert outcomes[0]["status"] == "indexed"
        assert outcomes[0]["chunks"] >= 1

        after = await index_status(docs_app, jar, f"server_id={SERVER_A}")
        after_body = after.json()
        assert after_body["pending"] == []
        rows = after_body["projection"]
        assert [row["doc_id"] for row in rows] == [doc_id]
        assert rows[0]["status"] == "indexed"
        assert rows[0]["chunks"] >= 1

        deleted = await delete_doc(
            docs_app,
            jar,
            doc_id,
            {"server_id": SERVER_A, "expected_revision": 1},
        )
        assert deleted.status == 200
        reconciled_again = await reconcile(
            docs_app, jar, {"server_id": SERVER_A}
        )
        assert reconciled_again.status == 200
        outcomes = reconciled_again.json()["outcomes"]
        assert outcomes[-1]["doc_id"] == doc_id
        assert outcomes[-1]["op"] == "delete"
        assert outcomes[-1]["status"] == "deleted"

        final = await index_status(docs_app, jar, f"server_id={SERVER_A}")
        final_rows = final.json()["projection"]
        assert all(
            row["status"] == "tombstoned" for row in final_rows
        ) or final_rows == []

    async def test_reconcile_unavailable_embedding_stays_pending(
        self, docs_app: FastAPI, embeddings_url: str, monkeypatch: pytest.MonkeyPatch
    ):
        jar = await login_session(docs_app)
        created = await create_doc(
            docs_app,
            jar,
            {"server_id": SERVER_A, "content": f"# Outage\n\n{MARKER_A}."},
        )
        doc_id = created.json()["doc_id"]
        # Point the embedder at a dead port: the gateway seam fails closed.
        dead = embeddings_url.rsplit(":", 1)[0] + ":1"
        monkeypatch.setattr(
            docs_app.state.knowledge.retriever._embedder, "_connection",
            type(
                "C",
                (),
                {
                    "base_url": dead,
                    "credential_ref": None,
                    "headers": {},
                    "timeout_seconds": 2.0,
                },
            )(),
        )
        indexer = docs_app.state.knowledge.indexer
        monkeypatch.setattr(
            indexer._embedder, "_connection",
            docs_app.state.knowledge.retriever._embedder._connection,
        )
        reconciled = await reconcile(docs_app, jar, {"server_id": SERVER_A})
        assert reconciled.status == 200
        outcomes = reconciled.json()["outcomes"]
        assert outcomes[0]["doc_id"] == doc_id
        assert outcomes[0]["status"] == "pending"
        assert outcomes[0]["code"] in (
            "embedding_unavailable",
            "index_unavailable",
        )
        status = await index_status(docs_app, jar, f"server_id={SERVER_A}")
        assert status.json()["pending"] != []

    async def test_retrieval_unavailable_index_is_explicit(
        self, docs_app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ):
        jar = await login_session(docs_app)

        class _DeadSessions:
            def __call__(self) -> Any:
                raise RuntimeError("fixture outage")

        monkeypatch.setattr(
            docs_app.state.knowledge.retriever, "_sessions", _DeadSessions()
        )
        result = await retrieve(
            docs_app, jar, {"server_id": SERVER_A, "query": MARKER_A}
        )
        assert result.status == 200
        body = result.json()
        assert body["status"] == "unavailable"
        assert body["code"] == "index_unavailable"
        assert body["excerpts"] == []


# ---------------------------------------------------------------------------
# Generation (C07 rule 5; C02 W4; 07-doc-generation surface)
# ---------------------------------------------------------------------------


class TestGenerate:
    async def test_preview_commits_nothing(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        evidence = _evidence()
        preview = await generate(
            docs_app, jar, {"evidence": evidence, "preview": True}
        )
        assert preview.status == 200, preview.body
        body = preview.json()
        assert body["preview"] is True
        assert body["classification"] == "runbook"
        assert body["namespace"] == "runbooks"
        assert body["verification"] == "verified"
        assert MARKER_RUNBOOK in body["content"]
        # No canonical write happened: the deterministic run id resolves
        # to nothing.
        from mai.knowledge.generate import doc_id_for_run

        doc_id = str(doc_id_for_run(uuid.UUID(RUN_OK)))
        read = await get_doc(
            docs_app,
            jar,
            doc_id,
            f"server_id={SERVER_A}&namespace=runbooks",
        )
        assert read.status == 404

    async def test_commit_writes_verified_runbook_and_citations_resolve(
        self, docs_app: FastAPI
    ):
        jar = await login_session(docs_app)
        committed = await generate(
            docs_app, jar, {"evidence": _evidence()}
        )
        assert committed.status == 200, committed.body
        body = committed.json()
        assert body["preview"] is False
        doc = body["doc"]
        assert doc["namespace"] == "runbooks"
        assert doc["source_kind"] == "generated"
        assert doc["origin"] == f"run:{RUN_OK}"
        assert doc["verification"] == "verified"
        assert body["classification"] == "runbook"
        assert body["reindex"] is not None
        assert any(
            outcome["status"] == "indexed" for outcome in body["reindex"]
        )

        result = await retrieve(
            docs_app,
            jar,
            {"server_id": SERVER_A, "query": f"{MARKER_RUNBOOK} restart"},
        )
        assert result.status == 200
        excerpts = result.json()["excerpts"]
        assert any(
            item["doc_id"] == doc["doc_id"]
            and item["source_kind"] == "generated"
            and item["origin"] == f"run:{RUN_OK}"
            for item in excerpts
        )

    async def test_failed_run_commits_incident_record(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        committed = await generate(
            docs_app,
            jar,
            {
                "evidence": _evidence(
                    run_id=RUN_FAIL,
                    status="failed",
                    host_verified=True,
                    summary=f"{MARKER_RUNBOOK} failed run incident.",
                    error="fixture step failed",
                )
            },
        )
        assert committed.status == 200, committed.body
        body = committed.json()
        assert body["classification"] == "incident"
        assert body["doc"]["namespace"] == "docs"
        assert body["doc"]["verification"] == "verified"

    async def test_unsettled_run_rejected_before_store_touch(
        self, docs_app: FastAPI
    ):
        jar = await login_session(docs_app)
        response = await generate(
            docs_app,
            jar,
            {"evidence": _evidence(status="running")},
        )
        assert response.status == 422
        assert _error(response)["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# Deep research (07-deep-research composition through the ONE dispatcher)
# ---------------------------------------------------------------------------


class TestResearch:
    async def _enrolled_server(
        self, docs_app: FastAPI, jar: Mapping[str, str]
    ) -> str:
        """Create one enrolled profile; return the minted server identity."""
        response = await create_server(docs_app, jar, "research-a")
        assert response.status == 201, response.body
        return str(response.json()["server_id"])

    @staticmethod
    def _write_prompt(tmp_path: Path, server_id: str) -> None:
        """Canonical administrator prompt for the context pin (fixture).

        ``prompt.md`` has no writer surface in this phase (the prompt-edit
        card owns it later); the research context pin reads the canonical
        file, so the fixture writes one directly into its own data root —
        the same discipline as tests/test_research_workflow.py.
        """
        server_dir = tmp_path / "data-root" / "servers" / server_id
        server_dir.mkdir(parents=True, exist_ok=True)
        (server_dir / "prompt.md").write_text(
            "# Fixture administrator prompt\n\nOperate carefully.",
            "utf-8",
        )

    async def test_research_run_reports_cited_sources(
        self, docs_app: FastAPI, tmp_path: Path
    ):
        jar = await login_session(docs_app)
        server_id = await self._enrolled_server(docs_app, jar)
        await grant_retrieval(docs_app, jar, server_id)
        self._write_prompt(tmp_path, server_id)
        created = await create_doc(
            docs_app,
            jar,
            {
                "server_id": server_id,
                "content": f"# Evidence\n\n{MARKER_A} worker restart facts.",
            },
        )
        assert created.status == 201, created.body
        reconciled = await reconcile(docs_app, jar, {"server_id": server_id})
        assert reconciled.status == 200, reconciled.body

        report = await research(
            docs_app,
            jar,
            {
                "server_id": server_id,
                "payload": {
                    "v": 1,
                    "question": f"What do we know about {MARKER_A}?",
                    "search_queries": [],
                    "page_reads": [],
                    "logs": [],
                    "children": [],
                    "synthesize": False,
                },
            },
        )
        assert report.status == 200, report.body
        body = report.json()
        assert body["status"] == "succeeded"
        assert body["server_id"] == server_id
        steps = {step["capability_id"]: step for step in body["steps"]}
        assert steps["docs.retrieve"]["status"] == "succeeded"
        assert any(
            source["kind"] == "scoped_doc"
            and MARKER_A in (source.get("text") or "")
            for source in body["sources"]
        )
        assert body["prompt_revision"] == 1

    async def test_research_step_denied_without_grant(
        self, docs_app: FastAPI, tmp_path: Path
    ):
        jar = await login_session(docs_app)
        server_id = await self._enrolled_server(docs_app, jar)
        self._write_prompt(tmp_path, server_id)
        report = await research(
            docs_app,
            jar,
            {
                "server_id": server_id,
                "payload": {
                    "v": 1,
                    "question": "ungated question",
                    "synthesize": False,
                },
            },
        )
        assert report.status == 200, report.body
        body = report.json()
        steps = {step["capability_id"]: step for step in body["steps"]}
        assert steps["docs.retrieve"]["status"] == "failed"

    async def test_synthesis_channel_unavailable_pre_flight(
        self, docs_app: FastAPI
    ):
        jar = await login_session(docs_app)
        server_id = await self._enrolled_server(docs_app, jar)
        response = await research(
            docs_app,
            jar,
            {
                "server_id": server_id,
                "payload": {"v": 1, "question": "q", "synthesize": True},
            },
        )
        assert response.status == 503
        assert _error(response)["code"] == "unavailable"

    async def test_web_channel_unavailable_pre_flight(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        server_id = await self._enrolled_server(docs_app, jar)
        response = await research(
            docs_app,
            jar,
            {
                "server_id": server_id,
                "payload": {
                    "v": 1,
                    "question": "q",
                    "search_queries": ["anything"],
                },
            },
        )
        assert response.status == 503

    async def test_children_unavailable_pre_flight(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        server_id = await self._enrolled_server(docs_app, jar)
        response = await research(
            docs_app,
            jar,
            {
                "server_id": server_id,
                "payload": {
                    "v": 1,
                    "question": "q",
                    "children": [{"task": "dig deeper"}],
                },
            },
        )
        assert response.status == 503

    async def test_unenrolled_server_not_found(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        response = await research(
            docs_app,
            jar,
            {
                "server_id": SERVER_C,
                "payload": {"v": 1, "question": "q"},
            },
        )
        assert response.status == 404
        assert _error(response)["code"] == "not_found"

    async def test_invalid_payload_rejected(self, docs_app: FastAPI):
        jar = await login_session(docs_app)
        server_id = await self._enrolled_server(docs_app, jar)
        response = await research(
            docs_app,
            jar,
            {"server_id": server_id, "payload": {"v": 1}},
        )
        assert response.status == 422
        assert _error(response)["code"] == "validation_failed"
