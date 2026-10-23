"""Focused tests for the memory HTTP surface (card 11-knowledge-api).

Card ``11-knowledge-api`` against contracts.md **C07** (bank-scoped memory,
KB-H2 honesty), **C03** (grant-gated, credential-reference-only), **C08**
(session/origin/CSRF boundary, registration under the shared app factory),
**C01** (envelope, identity, scope), **C02** (canonical stores).

Boundary discipline (test policy): every HTTP claim runs through the real
ASGI app (``create_app`` + lifespan + middleware + handlers) against a real
disposable Postgres (fixture ``migrated_url``) through the real linear
Alembic chain. Memory operations run the PRODUCER's own capability
executors over a recording provider double (the canned documents are the
upstream shapes the ``11-memory-adapter`` tests froze); grant denials run
through the REAL ``GrantConfigStore`` + policy evaluator.

Registered limitations: no live Hindsight instance (bank naming, scoping,
payload screening and honest failure documents are producer-executor truth;
the assembled two-bank live journey is ``11-verify``'s scenario).
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

import mai.api.knowledge_models as surface
from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.api.knowledge_models import MemoryServices
from mai.memory.hindsight import (
    MemoryErrorCode,
    server_bank_id,
)
from mai.policy.config import GrantConfigStore
from mai.policy.evaluate import ExecutionContext, PolicyMode

#: The suites run async tests through the anyio pytest plugin (the repo's
#: async test convention); sync tests stay unmarked and pass through.
pytestmark = pytest.mark.anyio

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credentials (NOT secrets; disposable fixtures only).
FIXTURE_PASSWORD = "fixture-password-11-knowledge-api-only"
FIXTURE_SSH_KEY_REF = "MAI_SSH_KEY_FIXTURE"

ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry
EVIL_ORIGIN = "https://attacker.example"

BACKEND_ROOT = Path(__file__).resolve().parents[1]

SERVER_ID = "11a6c2e4-1b3d-4f5a-8c7e-2d3b4a5f6e70"
SERVER_B_ID = "22b7d3f5-2c4e-5a6b-9d8f-3e4c5b6a7f81"
HOST_KEY = "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"
#: A syntactically valid (never trusted) ed25519 known-hosts blob: the
#: refused-connection transport test never reaches key verification.
KNOWN_HOSTS_BLOB = (
    "AAAAC3NzaC1lZDI1NTE5AAAAIB6gAn8ePdwNHQwEwRty5DCM+o7QkgmB8C1K3tixa8Od"
)

EXEC_CONTEXT_CAPABILITIES = ("memory.recall", "memory.retain", "memory.reflect")


# ---------------------------------------------------------------------------
# canned upstream documents (the shapes frozen by backend/tests/test_hindsight.py)
# ---------------------------------------------------------------------------

_RETAIN_DOC = {
    "success": True,
    "bank_id": "fixture",
    "items_count": 1,
    "async": False,
    "usage": {"input_tokens": 90, "output_tokens": 12, "total_tokens": 102},
}

_RECALL_DOC = {
    "results": [
        {
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
    ]
}

_REFLECT_DOC = {
    "text": "The worker runs PostgreSQL 18.",
    "based_on": {"world": ["f-1"]},
}

_DELETE_MEMORIES_DOC = {"success": True, "deleted_count": 3}
_DELETE_BANK_DOC = {"success": True, "deleted_count": 9}

_EXPORT_DOC = {
    "version": "1",
    "bank": {"bank_id": "fixture"},
    "memories": [{"id": "mem-0001", "text": "fact"}],
}

_VERSION_DOC = {"api_version": "1.0.0-stub", "features": {}}


class FakeMemoryProvider:
    """Recording provider double over the frozen upstream document shapes.

    Implements the producer's ``MemoryProvider`` seam; every call records
    ``(operation, bank_id)`` so tests can prove WHICH bank was addressed
    (cross-server isolation) and that refusals happened BEFORE any call.
    """

    def __init__(self, *, exc: BaseException | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.exc = exc

    def _before(self, operation: str, bank_id: str) -> None:
        self.calls.append((operation, bank_id))
        if self.exc is not None:
            raise self.exc

    async def retain(self, *, bank_id: str, items, **_) -> dict[str, object]:
        self._before("retain", bank_id)
        return dict(_RETAIN_DOC, items_count=len(list(items)))

    async def recall(self, *, bank_id: str, query, types, budget, max_tokens, **_):
        self._before("recall", bank_id)
        return dict(_RECALL_DOC)

    async def reflect(self, *, bank_id: str, query, budget, max_tokens, **_):
        self._before("reflect", bank_id)
        return dict(_REFLECT_DOC)

    async def delete_memories(self, *, bank_id: str, fact_type=None, **_):
        self._before("delete-memories", bank_id)
        return dict(_DELETE_MEMORIES_DOC)

    async def delete_bank(self, *, bank_id: str, **_):
        self._before("delete-bank", bank_id)
        return dict(_DELETE_BANK_DOC)

    async def export_bank(self, *, bank_id: str, **_):
        self._before("export", bank_id)
        return dict(_EXPORT_DOC)

    async def version(self, **_):
        self.calls.append(("version", ""))
        if self.exc is not None:
            raise self.exc
        return dict(_VERSION_DOC)


# ---------------------------------------------------------------------------
# ASGI harness (the sibling-card shape; no test-client dependency)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HttpResponse:
    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.decode("ascii").lower() == name:
                return value.decode("ascii")
        return None

    def header_all(self, name: str) -> list[str]:
        return [
            value.decode("ascii")
            for key, value in self.headers
            if key.decode("ascii").lower() == name
        ]

    def json(self) -> Any:
        return json.loads(self.body)


def _scope(method: str, path: str, headers: dict[str, str], body: bytes) -> dict:
    scope_headers = [(b"host", b"fixture")]
    scope_headers.extend(
        (key.encode("ascii").lower(), value.encode("ascii"))
        for key, value in headers.items()
    )
    url, _, query = path.partition("?")
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": url,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "root_path": "",
        "headers": scope_headers,
        "client": ("127.0.0.1", 0),
        "server": ("fixture", 80),
    }


async def call_app(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> HttpResponse:
    """Run one bounded HTTP request through the real ASGI callable."""
    flat_headers = dict(headers or {})
    if cookies:
        flat_headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    body = b""
    if json_body is not None:
        body = json.dumps(json_body).encode()
        flat_headers["content-type"] = "application/json"
    response = HttpResponse()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response.status = message["status"]
            response.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            response.body += message.get("body", b"")

    await app(_scope(method, path, flat_headers, body), receive, send)
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


def _mutation_headers(jar: dict[str, str]) -> dict[str, str]:
    return {"origin": ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]}


def _read_cookies(jar: dict[str, str]) -> dict[str, str]:
    return {SESSION_COOKIE: jar[SESSION_COOKIE], CSRF_COOKIE: jar[CSRF_COOKIE]}


# ---------------------------------------------------------------------------
# fixture data root: enrolled profile + pinned host + grants (real producers)
# ---------------------------------------------------------------------------


def _profile_text(server_id: str) -> str:
    from mai.contracts.server import SCHEMA_VERSION as SERVER_SCHEMA_VERSION

    return (
        f"schema_version: {SERVER_SCHEMA_VERSION}\n"
        f'server_id: "{server_id}"\n'
        'name: "knowledge-api-fixture"\n'
        "prompt_revision: 3\n"
        "environment: linux_ssh\n"
        "ssh:\n"
        '  host: "127.0.0.1"\n'
        "  port: 1\n"
        '  user: "deploy"\n'
        f'  host_key: "{HOST_KEY}"\n'
        f'  credential_ref: "{FIXTURE_SSH_KEY_REF}"\n'
        "privilege: none\n"
        "capabilities:\n"
        "  - fixture.read\n"
    )


def _seed_server(root: str | Path, server_id: str, *, known_hosts: bool) -> Path:
    from mai.profiles.files import ProfileFileStore

    root = Path(root)
    store = ProfileFileStore(root)
    store.write(uuid.UUID(server_id), _profile_text(server_id), expected_revision=0)
    server_dir = root / "servers" / server_id
    known = server_dir / "known_hosts"
    if known_hosts:
        known.write_text(f"[127.0.0.1]:1 ssh-ed25519 {KNOWN_HOSTS_BLOB}\n")
    return known


def _write_grants(
    root: str | Path,
    grants: list[tuple[str, list[str]]],
    *,
    denied: list[str] | None = None,
) -> None:
    document = {
        "policy": {"revision": 1, "denied_capabilities": list(denied or [])},
        "grants": [
            {"capability_id": capability, "server_ids": servers}
            for capability, servers in grants
        ],
    }
    GrantConfigStore(Path(root)).write(document, expected_revision=0)


def _fixture_env(tmp_path: Path, url: str, *, with_hindsight: bool) -> dict[str, str]:
    env = {
        "MAI_DATABASE_URL": url,
        "MAI_PUBLIC_ORIGINS": ORIGIN,
        "MAI_DATA_ROOT": str(tmp_path / "data-root"),
        "MAI_ENV_FILE": str(tmp_path / "fixtures.env"),
    }
    if with_hindsight:
        env["MAI_HINDSIGHT_URL"] = "http://hindsight.internal:8888"
        env["MAI_MEMORY_SHARED_BANKS"] = "ops"
    return env


def _write_env_file(tmp_path: Path, *, with_key: bool) -> None:
    env_file = tmp_path / "fixtures.env"
    lines: list[str] = []
    if with_key:
        key_path = tmp_path / "id_ed25519"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key_path)],
            check=True,
            capture_output=True,
        )
        lines.append(f"{FIXTURE_SSH_KEY_REF}={key_path}\n")
    env_file.write_text("".join(lines))
    os.chmod(env_file, 0o600)  # the resolver's fail-closed permission guard


# ---------------------------------------------------------------------------
# disposable fixture database (the sibling-card harness shape)
# ---------------------------------------------------------------------------


def _alembic_upgrade() -> None:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    config = AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))
    command.upgrade(config, "head")


def _drop_app_tables(url: str) -> None:
    import psycopg

    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS job_resource_locks, job_schedule_fires, "
            "job_schedules, job_events, job_steps, job_runs, approvals, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, observation_findings, notification_alerts, "
            "server_observations, knowledge_doc_chunks, knowledge_documents, "
            "application_projection, application_projection_journal, "
            "alembic_version"
        )
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the knowledge HTTP surface claims "
            "need a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_app_tables(url)
        _alembic_upgrade()
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture(scope="session")
def bootstrapped_url(migrated_url: str) -> str:
    """Create the engine-owned ``dbos`` schema the way production does."""
    from mai.jobs import engine as jobs_engine

    jobs_engine.launch_worker(migrated_url)
    jobs_engine.shutdown()
    return migrated_url


async def _truncate(url: str) -> None:
    """Reset the facade tables between tests (the session-scoped DB stays)."""
    from sqlalchemy import text

    from mai.database import create_app_engine

    engine, _sessions = create_app_engine(url)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            )
            existing = {row[0] for row in rows}
            for table in (
                "job_resource_locks",
                "job_schedule_fires",
                "job_steps",
                "job_events",
                "job_schedules",
                "job_runs",
                "approvals",
                "profile_projection_journal",
                "profile_projection",
                "admin_sessions",
                "admin_credential",
            ):
                if table in existing:
                    await conn.execute(text(f"DELETE FROM {table}"))
    finally:
        await engine.dispose()


@pytest.fixture
async def knowledge_app(bootstrapped_url: str, tmp_path: Path):
    """App on the real fixture DB with a fresh confined data root + .env.

    The lifespan wires the memory state from the env (exercised with the
    configured URL), then the fixture REPLACES the provider with the
    recording double — the routes consume ``app.state.memory`` per request,
    exactly as production would hand them the lifespan-built bundle.
    """
    await _truncate(bootstrapped_url)
    _write_env_file(tmp_path, with_key=True)
    app = create_app(env=_fixture_env(tmp_path, bootstrapped_url, with_hindsight=True))
    async with app.router.lifespan_context(app):
        app.state.memory = MemoryServices(
            provider=FakeMemoryProvider(), shared_banks=("ops",)
        )
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
async def jar(knowledge_app: FastAPI) -> dict[str, str]:
    return await login_session(knowledge_app)


@pytest.fixture
async def unconfigured_app(bootstrapped_url: str, tmp_path: Path):
    """App without any memory wiring: the surface must fail closed."""
    await _truncate(bootstrapped_url)
    _write_env_file(tmp_path, with_key=False)
    app = create_app(env=_fixture_env(tmp_path, bootstrapped_url, with_hindsight=False))
    async with app.router.lifespan_context(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


def _memory_path(server_id: str, operation: str) -> str:
    return f"/api/v1/servers/{server_id}/memory/{operation}"


def _grant_all_operations(server_id: str) -> list[tuple[str, list[str]]]:
    return [
        (f"memory.{operation}", [server_id])
        for operation in ("recall", "retain", "reflect")
    ]


# ---------------------------------------------------------------------------
# memory surface (acceptance 1/2: scoped via grants, honest failures)
# ---------------------------------------------------------------------------


class TestMemorySurface:
    async def test_status_reports_provider_version(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        _seed_server(
            knowledge_app.state.env["MAI_DATA_ROOT"], SERVER_ID, known_hosts=False
        )  # type: ignore[arg-type]
        response = await call_app(
            knowledge_app,
            "GET",
            _memory_path(SERVER_ID, "status"),
            cookies=_read_cookies(jar),
        )
        assert response.status == 200, response.body
        body = response.json()
        assert body["status"] == "ok"
        assert body["api_version"] == "1.0.0-stub"

    async def test_recall_returns_cited_hits_from_the_server_bank(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(root, _grant_all_operations(SERVER_ID))
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "recall"),
            json_body={"scope": "server", "query": "postgres version"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert response.status == 200, response.body
        body = response.json()
        assert body["status"] == "ok"
        assert body["bank"] == server_bank_id(_context(SERVER_ID))
        assert body["results"][0]["id"] == "mem-0001"
        assert body["results"][0]["citation"].startswith("memory:")
        provider: FakeMemoryProvider = knowledge_app.state.memory.provider  # type: ignore[index]
        assert provider.calls == [("recall", f"mai-server-{SERVER_ID}")]

    async def test_retain_returns_the_real_receipt(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(root, _grant_all_operations(SERVER_ID))
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "retain"),
            json_body={
                "scope": "server",
                "items": [{"content": "the worker runs postgres 18"}],
            },
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert response.status == 200, response.body
        body = response.json()
        assert body["status"] == "ok"
        assert body["bank"] == f"mai-server-{SERVER_ID}"
        assert isinstance(body["receipt"], dict) and body["receipt"]
        provider: FakeMemoryProvider = knowledge_app.state.memory.provider  # type: ignore[index]
        assert provider.calls == [("retain", f"mai-server-{SERVER_ID}")]

    async def test_reflect_answers_with_basis_ids(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(root, _grant_all_operations(SERVER_ID))
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "reflect"),
            json_body={"scope": "server", "query": "what does the worker run"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert response.status == 200, response.body
        body = response.json()
        assert body["status"] == "ok"
        assert body["text"] == "The worker runs PostgreSQL 18."
        assert body["based_on"] == ["f-1"]

    async def test_export_passes_the_manifest_through_without_csrf(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(root, _grant_all_operations(SERVER_ID))
        # A safe method: session layer only — deliberately NO origin/CSRF.
        response = await call_app(
            knowledge_app,
            "GET",
            _memory_path(SERVER_ID, "export") + "?scope=server",
            cookies=_read_cookies(jar),
        )
        assert response.status == 200, response.body
        body = response.json()
        assert body["status"] == "ok"
        assert body["manifest"]["memories"][0]["id"] == "mem-0001"
        provider: FakeMemoryProvider = knowledge_app.state.memory.provider  # type: ignore[index]
        assert provider.calls == [("export", f"mai-server-{SERVER_ID}")]

    async def test_delete_memories_and_bank_are_explicitly_receipted(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(root, _grant_all_operations(SERVER_ID))
        memories = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "delete"),
            json_body={"scope": "server", "target": "memories"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert memories.status == 200, memories.body
        assert memories.json()["status"] == "ok"
        assert memories.json()["operation"] == "delete-memories"
        assert memories.json()["deleted_count"] == 3
        bank = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "delete"),
            json_body={"scope": "server", "target": "bank"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert bank.status == 200, bank.body
        assert bank.json()["operation"] == "delete-bank"
        assert bank.json()["deleted_count"] == 9
        provider: FakeMemoryProvider = knowledge_app.state.memory.provider  # type: ignore[index]
        assert [call[0] for call in provider.calls] == [
            "delete-memories",
            "delete-bank",
        ]

    async def test_delete_rejects_unknown_fact_type(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(root, _grant_all_operations(SERVER_ID))
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "delete"),
            json_body={
                "scope": "server",
                "target": "memories",
                "fact_type": "everything",
            },
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert response.status == 422, response.body
        assert _error(response)["code"] == "validation_failed"

    async def test_provider_outage_is_an_honest_unavailable_answer(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(root, _grant_all_operations(SERVER_ID))
        knowledge_app.state.memory.provider = FakeMemoryProvider(  # type: ignore[index]
            exc=RuntimeError("hindsight pool exploded")
        )
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "retain"),
            json_body={"scope": "server", "items": [{"content": "note"}]},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        # KB-H2: a retain failure is NEVER reported as success — the
        # producer's own degradation document carries the classification.
        assert response.status == 200, response.body
        body = response.json()
        assert body["status"] == "unavailable"
        assert body["code"] == "provider_error"
        assert body["error_type"] == "RuntimeError"
        assert body["receipt"] is None

    async def test_secret_shaped_retain_is_refused_before_any_provider_call(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(root, _grant_all_operations(SERVER_ID))
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "retain"),
            json_body={
                "scope": "server",
                "items": [{"content": "password: fixture-secret-value"}],
            },
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert response.status == 422, response.body
        error = _error(response)
        assert error["fields"]["payload"] == MemoryErrorCode.SENSITIVE_CONTENT.value
        provider: FakeMemoryProvider = knowledge_app.state.memory.provider  # type: ignore[index]
        assert provider.calls == []  # nothing left the process

    async def test_server_banks_are_isolated_per_explicit_target(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _seed_server(root, SERVER_B_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(
            root,
            [
                (capability, [SERVER_ID, SERVER_B_ID])
                for capability, _ in _grant_all_operations(SERVER_ID)
            ],
        )
        for server in (SERVER_ID, SERVER_B_ID):
            response = await call_app(
                knowledge_app,
                "POST",
                _memory_path(server, "recall"),
                json_body={"scope": "server", "query": "postgres"},
                headers=_mutation_headers(jar),
                cookies=_read_cookies(jar),
            )
            assert response.status == 200, response.body
            assert response.json()["bank"] == f"mai-server-{server}"
        provider: FakeMemoryProvider = knowledge_app.state.memory.provider  # type: ignore[index]
        assert {call[1] for call in provider.calls} == {
            f"mai-server-{SERVER_ID}",
            f"mai-server-{SERVER_B_ID}",
        }

    async def test_unconfigured_memory_surface_fails_closed(
        self, unconfigured_app: FastAPI, jar: dict[str, str]
    ) -> None:
        _seed_server(
            unconfigured_app.state.env["MAI_DATA_ROOT"],  # type: ignore[arg-type]
            SERVER_ID,
            known_hosts=False,
        )
        _write_grants(
            unconfigured_app.state.env["MAI_DATA_ROOT"],  # type: ignore[arg-type]
            _grant_all_operations(SERVER_ID),
        )
        response = await call_app(
            unconfigured_app,
            "POST",
            _memory_path(SERVER_ID, "recall"),
            json_body={"scope": "server", "query": "q"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert response.status == 503, response.body
        assert _error(response)["code"] == "unavailable"

    async def test_disabled_provider_degrades_honestly(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(root, _grant_all_operations(SERVER_ID))
        knowledge_app.state.memory = MemoryServices(provider=None, shared_banks=())  # type: ignore[index]
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "recall"),
            json_body={"scope": "server", "query": "q"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert response.status == 200, response.body
        body = response.json()
        assert body["status"] == "unavailable"
        assert body["code"] == MemoryErrorCode.MEMORY_DISABLED.value


def _context(server_id: str) -> ExecutionContext:
    return ExecutionContext(
        principal="fixture",
        server_id=uuid.UUID(server_id),
        run_id=uuid.uuid4(),
        parent_scope=None,
        profile_revision="1",
        prompt_revision="3",
        credential_references=(),
        allowed_capabilities=EXEC_CONTEXT_CAPABILITIES,
        mode=PolicyMode.DELEGATED,
    )


# ---------------------------------------------------------------------------
# negative controls (real session/grant/scope safety boundaries)
# ---------------------------------------------------------------------------


class TestKnowledgeNegativeControls:
    async def test_nt_no_cookie_is_unauthenticated(
        self, knowledge_app: FastAPI
    ) -> None:
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "recall"),
            json_body={"scope": "server", "query": "q"},
            headers={"origin": ORIGIN},
        )
        assert response.status == 401, response.body
        assert _error(response)["code"] == "unauthenticated"

    async def test_nt_evil_origin_is_rejected(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "recall"),
            json_body={"scope": "server", "query": "q"},
            headers={"origin": EVIL_ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]},
            cookies=_read_cookies(jar),
        )
        assert response.status == 403, response.body
        assert _error(response)["code"] == "origin_rejected"

    async def test_nt_missing_csrf_token_is_rejected(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "recall"),
            json_body={"scope": "server", "query": "q"},
            headers={"origin": ORIGIN},
            cookies=_read_cookies(jar),
        )
        assert response.status == 403, response.body
        assert _error(response)["code"] == "csrf_rejected"

    async def test_nt_capability_not_granted_is_rejected(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        # Only memory.retain is granted: recall is a real 403, never a
        # silent empty answer and never a provider call.
        _write_grants(root, [("memory.retain", [SERVER_ID])])
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "recall"),
            json_body={"scope": "server", "query": "q"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert response.status == 403, response.body
        error = _error(response)
        assert error["code"] == "request_rejected"
        assert "capability_not_granted" in error["message"]
        provider: FakeMemoryProvider = knowledge_app.state.memory.provider  # type: ignore[index]
        assert provider.calls == []

    async def test_nt_server_outside_grant_scope_is_rejected(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _seed_server(root, SERVER_B_ID, known_hosts=False)  # type: ignore[arg-type]
        # memory.recall is granted ONLY for server B: a request naming
        # server A is denied with the stable scope-mismatch reason, and
        # nothing about B's grant travels into the response.
        _write_grants(root, [("memory.recall", [SERVER_B_ID])])
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "recall"),
            json_body={"scope": "server", "query": "q"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert response.status == 403, response.body
        error = _error(response)
        assert error["code"] == "request_rejected"
        assert "server_scope_mismatch" in error["message"]
        assert SERVER_B_ID not in response.body.decode()

    async def test_nt_policy_denied_capability_is_rejected(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(
            root,
            _grant_all_operations(SERVER_ID),
            denied=["memory.recall"],
        )
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "recall"),
            json_body={"scope": "server", "query": "q"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert response.status == 403, response.body
        assert "capability_denied_by_policy" in _error(response)["message"]

    async def test_nt_unenrolled_target_is_not_found(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        _write_grants(
            knowledge_app.state.env["MAI_DATA_ROOT"],  # type: ignore[arg-type]
            _grant_all_operations(SERVER_ID),
        )
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "recall"),
            json_body={"scope": "server", "query": "q"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert response.status == 404, response.body
        assert _error(response)["code"] == "not_found"

    async def test_nt_shared_bank_outside_allowlist_is_forbidden(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(root, _grant_all_operations(SERVER_ID))
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "recall"),
            json_body={"scope": "shared:not-configured", "query": "q"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert response.status == 422, response.body
        assert (
            _error(response)["fields"]["payload"]
            == MemoryErrorCode.BANK_FORBIDDEN.value
        )
        provider: FakeMemoryProvider = knowledge_app.state.memory.provider  # type: ignore[index]
        assert provider.calls == []

    async def test_shared_scope_resolves_through_the_allowlist(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(root, _grant_all_operations(SERVER_ID))
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "recall"),
            json_body={"scope": "shared:ops", "query": "q"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert response.status == 200, response.body
        assert response.json()["bank"] == "mai-shared-ops"
        provider: FakeMemoryProvider = knowledge_app.state.memory.provider  # type: ignore[index]
        assert provider.calls == [("recall", "mai-shared-ops")]

    async def test_nt_unknown_body_field_is_rejected(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(root, _grant_all_operations(SERVER_ID))
        response = await call_app(
            knowledge_app,
            "POST",
            _memory_path(SERVER_ID, "recall"),
            json_body={"scope": "server", "query": "q", "bank": "mai-other"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert response.status == 422, response.body
        assert _error(response)["code"] == "validation_failed"
        provider: FakeMemoryProvider = knowledge_app.state.memory.provider  # type: ignore[index]
        assert provider.calls == []  # no client-supplied bank name ever lands

    async def test_nt_forgeable_scope_frequencies_fail_closed(
        self, knowledge_app: FastAPI, jar: dict[str, str]
    ) -> None:
        root = knowledge_app.state.env["MAI_DATA_ROOT"]  # type: ignore[arg-type]
        _seed_server(root, SERVER_ID, known_hosts=False)  # type: ignore[arg-type]
        _write_grants(root, _grant_all_operations(SERVER_ID))
        for scope in ("", "other-server", "shared:", "serverB"):
            response = await call_app(
                knowledge_app,
                "POST",
                _memory_path(SERVER_ID, "recall"),
                json_body={"scope": scope, "query": "q"},
                headers=_mutation_headers(jar),
                cookies=_read_cookies(jar),
            )
            assert response.status == 422, (scope, response.body)
        provider: FakeMemoryProvider = knowledge_app.state.memory.provider  # type: ignore[index]
        assert provider.calls == []


class TestBodyShapes:
    def test_unknown_fields_rejected_everywhere(self) -> None:
        """Schema-strict bodies: unknown fields are rejected, never ignored."""
        for body_class, seed in (
            (surface.MemoryRecallBody, {"scope": "server", "query": "q"}),
            (
                surface.MemoryRetainBody,
                {"scope": "server", "items": [{"content": "c"}]},
            ),
            (surface.MemoryReflectBody, {"scope": "server", "query": "q"}),
            (surface.MemoryDeleteBody, {"scope": "server", "target": "bank"}),
        ):
            with pytest.raises(ValidationError):
                body_class.model_validate({**seed, "api_key": "sk-forged"})  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# registration + OpenAPI contract (acceptance 3)
# ---------------------------------------------------------------------------


class TestOpenApiContract:
    def test_surface_registers_under_the_shared_boundary(self) -> None:
        contract = create_app().openapi()
        paths = set(contract["paths"])
        expected = {
            f"/api/v1/servers/{{server_id}}/memory/{operation}"
            for operation in (
                "status",
                "recall",
                "retain",
                "reflect",
                "export",
                "delete",
            )
        }
        assert expected <= paths, sorted(expected - paths)
        components = set(contract["components"]["schemas"])
        for name in (
            "MemoryRecallBody",
            "MemoryRetainBody",
            "MemoryDeleteBody",
        ):
            assert name in components
        # Earlier surfaces' components stay intact (no namespace collision).
        for name in ("ChatCancelBody", "RunSubmitBody", "PlanView"):
            assert name in components

    def test_no_route_bypasses_the_api_prefix(self) -> None:
        contract = create_app().openapi()
        for path in contract["paths"]:
            assert path.startswith("/api/v1/")
