"""Focused tests for the history-tag management HTTP surface (HT-W6B).

Card ``history-tags`` W6B against the accepted tag store (``mai.tag_store``)
and the C01/C08 boundary conventions: the global tag catalog CRUD (bounded
deterministic pages, canonical idempotent create, the used/unused deletion
policy, the reserved ``unclassified`` row), same-server job-run and
job-run tag assignments (idempotent, user-sourced, final-tag
protected), scope-blind 404s (missing ≡ cross-server), the zero UUID
General Consult scope on the CONVERSATION routes only, and the bounded
``tag_id`` list filters on the two list seams (covered in-depth in
``test_jobs_http.py``.)

Boundary discipline: every claim runs through the real ASGI app
(``create_app`` + lifespan + middleware + handlers) against a real
disposable Postgres (fixture ``migrated_url``) through the real linear
Alembic chain. No worker is spawned: submit-level runs stay QUEUED, which
is all the tag surface needs. Mocks are used only where no fixture can
produce the condition.

Secret hygiene: fixture credentials/principals are inert constants for the
disposable fixture DB; session and CSRF tokens are minted at runtime and
never printed into assertion messages.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from sqlalchemy import text

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID

BACKEND_ROOT = Path(__file__).resolve().parents[1]

#: The suites run async tests through the anyio pytest plugin (the repo's
#: async test convention); sync helpers stay unmarked and pass through.
pytestmark = pytest.mark.anyio

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-history-tags-http-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry

#: Opaque server identities for the cross-server (C8) proof. SERVER_A is
#: minted per test through the canonical servers surface; SERVER_B and the
#: General Consult sentinel are fixed opaque UUIDs.
SERVER_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
GENERAL_CONSULT = uuid.UUID(int=0)
RESERVED = uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)


# ---------------------------------------------------------------------------
# ASGI harness (the proven in-process shape; no test-client dependency)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HttpResponse:
    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body: bytes = b""

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


def _scope_blind(response: HttpResponse) -> dict[str, str]:
    """The comparable scope-blind envelope: code + message, no request_id."""
    body = _error(response)
    return {"code": body["code"], "message": body["message"]}


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


async def create_profile(app: FastAPI, jar: dict[str, str], name: str) -> uuid.UUID:
    """Create one canonical server profile through the real HTTP surface."""
    response = await call_app(
        app,
        "POST",
        "/api/v1/servers",
        json_body={
            "name": name,
            "prompt_revision": 7,
            "environment": "linux_ssh",
            "ssh": {
                "host": "fixture.internal",
                "port": 22,
                "user": "fixture",
                "host_key": "SHA256:6VhOyUC6fvXW0aQ9WNe7Kx3nZZf+zxgupEWLoZXoTMU",
                "credential_ref": "MAI_SSH_KEY_FIXTURE",
            },
            "privilege": "none",
            "capabilities": [],
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status == 201, response.body
    return uuid.UUID(response.json()["server_id"])


# ---------------------------------------------------------------------------
# disposable database + app fixtures
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state (engine schema too)."""
    import psycopg

    with psycopg.connect(url) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the history-tag HTTP surface "
            "claims need a real disposable Postgres fixture"
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


@pytest.fixture(scope="session")
def bootstrapped_url(migrated_url: str) -> str:
    """Create the engine-owned ``dbos`` schema the way production does."""
    from mai.jobs import engine as jobs_engine

    jobs_engine.launch_worker(migrated_url)
    jobs_engine.shutdown()
    return migrated_url


async def _truncate(url: str) -> None:
    """Reset the domain rows between tests; the seeded catalog stays.

    The reserved ``unclassified`` row is migration-seeded and MUST survive
    (entity creation attaches it inside the same transaction), so the
    catalog reset deletes every NON-reserved tag only.
    """
    from mai.database import create_app_engine

    engine, _sessions = create_app_engine(url)
    try:
        async with engine.begin() as conn:
            for table in (
                # The obsolete chat/task/objective tables are dropped by
                # migration 0015 — nothing to reset there anymore.
                "job_resource_locks",
                "job_schedule_fires",
                "job_steps",
                "job_events",
                "job_tags",
                "job_schedules",
                "job_runs",
                "approvals",
                "profile_projection_journal",
                "profile_projection",
                "admin_sessions",
                "admin_credential",
                            ):
                # Engine/service-owned tables are absent until first use.
                present = await conn.execute(
                    text("SELECT to_regclass(:t) IS NOT NULL"),
                    {"t": f"public.{table}"},
                )
                if not present.scalar():
                    continue
                await conn.execute(text(f"DELETE FROM {table}"))
            present = await conn.execute(
                text("SELECT to_regclass('public.tags') IS NOT NULL")
            )
            if present.scalar():
                await conn.execute(
                    text("DELETE FROM tags WHERE tag_id <> :reserved"),
                    {"reserved": RESERVED},
                )
    finally:
        await engine.dispose()


def _build_app(url: str, data_root: Path) -> FastAPI:
    return create_app(
        env={
            "MAI_DATABASE_URL": url,
            "MAI_PUBLIC_ORIGINS": ORIGIN,
            "MAI_DATA_ROOT": str(data_root),
        }
    )


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def tags_app(bootstrapped_url: str, tmp_path: Path):
    """App on the real fixture DB with a fresh confined data root."""
    await _truncate(bootstrapped_url)
    app = _build_app(bootstrapped_url, tmp_path / "data-root")
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
async def jar(tags_app: FastAPI) -> dict[str, str]:
    return await login_session(tags_app)


@pytest.fixture
async def server_a(tags_app: FastAPI, jar: dict[str, str]) -> uuid.UUID:
    return await create_profile(tags_app, jar, "tags-fixture-a")


# ---------------------------------------------------------------------------
# seed helpers (real producers only)
# ---------------------------------------------------------------------------


async def _create_tag(
    app: FastAPI,
    jar: dict[str, str],
    name: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Create one catalog tag through the real HTTP surface."""
    body: dict[str, Any] = {"name": name}
    if description is not None:
        body["description"] = description
    response = await call_app(
        app,
        "POST",
        "/api/v1/tags",
        json_body=body,
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status == 201, response.body
    return response.json()




async def _submit_run(
    app: FastAPI, jar: dict[str, str], server_id: uuid.UUID
) -> uuid.UUID:
    """One durable run intent through the real submit seam (stays QUEUED)."""
    run_id = uuid.uuid4()
    response = await call_app(
        app,
        "POST",
        "/api/v1/jobs/runs",
        json_body={
            "run_id": str(run_id),
            "server_id": str(server_id),
            "model_profile": "build-agent",
            "mode": "delegated",
            "steps": [{"capability_id": "fixture.read", "payload": {"marker": "m1"}}],
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status == 201, response.body
    return run_id


async def _assign(
    app: FastAPI,
    jar: dict[str, str],
    path: str,
    tag_id: uuid.UUID,
    server_id: uuid.UUID,
) -> HttpResponse:
    """POST one entity-tag assignment body."""
    return await call_app(
        app,
        "POST",
        path,
        json_body={"tag_id": str(tag_id), "server_id": str(server_id)},
        headers=_mutation_headers(jar),
        cookies=jar,
    )


# ---------------------------------------------------------------------------
# boundary (auth ADR §5: session → origin → CSRF)
# ---------------------------------------------------------------------------


class TestBoundary:
    async def test_safe_reads_require_session(self, tags_app: FastAPI) -> None:
        response = await call_app(tags_app, "GET", "/api/v1/tags")
        assert response.status == 401
        assert _error(response)["code"] == "unauthenticated"

    async def test_mutations_require_origin_then_csrf(
        self, tags_app: FastAPI, jar: dict[str, str]
    ) -> None:
        no_origin = await call_app(
            tags_app, "POST", "/api/v1/tags", json_body={"name": "x"}, cookies=jar
        )
        assert no_origin.status == 403
        assert _error(no_origin)["code"] == "origin_rejected"

        no_csrf = await call_app(
            tags_app,
            "POST",
            "/api/v1/tags",
            json_body={"name": "x"},
            headers={"origin": ORIGIN},
            cookies=jar,
        )
        assert no_csrf.status == 403
        assert _error(no_csrf)["code"] == "csrf_rejected"

        delete_no_csrf = await call_app(
            tags_app,
            "DELETE",
            f"/api/v1/tags/{RESERVED}",
            headers={"origin": ORIGIN},
            cookies=jar,
        )
        assert delete_no_csrf.status == 403
        assert _error(delete_no_csrf)["code"] == "csrf_rejected"


# ---------------------------------------------------------------------------
# global catalog (bounded pages, canonical idempotent create, validation)
# ---------------------------------------------------------------------------


class TestCatalog:
    async def test_create_and_list_roundtrip_is_deterministic(
        self, tags_app: FastAPI, jar: dict[str, str]
    ) -> None:
        await _create_tag(tags_app, jar, "zulu", "last alphabetically")
        await _create_tag(tags_app, jar, "alpha", "first alphabetically")
        await _create_tag(tags_app, jar, "mike")

        page = await call_app(tags_app, "GET", "/api/v1/tags", cookies=jar)
        assert page.status == 200, page.body
        body = page.json()
        assert body["limit"] == 50 and body["offset"] == 0
        # The reserved ``unclassified`` seed is part of the catalog.
        assert body["total"] == 4
        assert body["has_more"] is False
        names = [row["name"] for row in body["items"]]
        assert names == sorted(names, key=str.lower)
        assert "unclassified" in names
        by_name = {row["name"]: row for row in body["items"]}
        assert by_name["alpha"]["description"] == "first alphabetically"
        assert by_name["mike"]["description"] is None
        assert by_name["alpha"]["created_at"].endswith("Z")
        assert by_name["alpha"]["tag_id"]

    async def test_create_is_idempotent_on_the_canonical_name(
        self, tags_app: FastAPI, jar: dict[str, str]
    ) -> None:
        first = await _create_tag(tags_app, jar, "Deploy Prod", "production deploys")
        # Different spacing + case normalizes to the SAME canonical row;
        # the stored description is never overwritten by a re-create.
        second = await _create_tag(tags_app, jar, "  deploy   PROD ")
        assert second["tag_id"] == first["tag_id"]
        assert second["name"] == first["name"] == "Deploy Prod"
        assert second["description"] == "production deploys"

        page = await call_app(tags_app, "GET", "/api/v1/tags", cookies=jar)
        assert page.json()["total"] == 2  # one canonical row + the seed, not two

    async def test_create_rejects_invalid_bodies(
        self, tags_app: FastAPI, jar: dict[str, str]
    ) -> None:
        cases: list[tuple[dict[str, Any], str | None]] = [
            ({"name": ""}, None),  # empty name
            ({"name": "x" * 65}, None),  # over the 64-char bound
            ({"name": "ok", "description": "d" * 501}, None),  # over 500
            ({"name": "ok", "unknown": True}, None),  # strict body
            ({"name": " \t "}, "name"),  # normalizes to empty (store 422)
            ({"name": "bad\x00name"}, "name"),  # control characters
        ]
        for body, field_name in cases:
            response = await call_app(
                tags_app,
                "POST",
                "/api/v1/tags",
                json_body=body,
                headers=_mutation_headers(jar),
                cookies=jar,
            )
            assert response.status == 422, (body, response.body)
            error = _error(response)
            assert error["code"] == "validation_failed"
            if field_name is not None:
                assert field_name in error["fields"]
        page = await call_app(tags_app, "GET", "/api/v1/tags", cookies=jar)
        assert page.json()["total"] == 1  # only the migration seed persists

    async def test_list_name_filter_is_case_insensitive_and_escaped(
        self, tags_app: FastAPI, jar: dict[str, str]
    ) -> None:
        await _create_tag(tags_app, jar, "one-alpha")
        await _create_tag(tags_app, jar, "two-beta")
        await _create_tag(tags_app, jar, "50%_off")

        filtered = await call_app(
            tags_app, "GET", "/api/v1/tags?name_filter=BETA", cookies=jar
        )
        body = filtered.json()
        assert body["total"] == 1
        assert body["items"][0]["name"] == "two-beta"

        # LIKE metacharacters are literal: "50%" must not match everything.
        percent = await call_app(
            tags_app, "GET", "/api/v1/tags?name_filter=50%25", cookies=jar
        )
        body = percent.json()
        assert body["total"] == 1
        assert body["items"][0]["name"] == "50%_off"

    async def test_list_pagination_is_bounded_and_honest(
        self, tags_app: FastAPI, jar: dict[str, str]
    ) -> None:
        for name in ("p-one", "p-two", "p-three"):
            await _create_tag(tags_app, jar, name)

        first = await call_app(
            tags_app, "GET", "/api/v1/tags?limit=2&offset=0", cookies=jar
        )
        body = first.json()
        assert body["total"] == 4  # 3 created + the reserved seed
        assert body["has_more"] is True
        assert [row["name"] for row in body["items"]] == ["p-one", "p-three"]

        second = await call_app(
            tags_app, "GET", "/api/v1/tags?limit=2&offset=2", cookies=jar
        )
        body = second.json()
        assert body["total"] == 4
        assert body["has_more"] is False
        assert [row["name"] for row in body["items"]] == ["p-two", "unclassified"]

    async def test_list_rejects_out_of_bounds_pagination(
        self, tags_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await call_app(
            tags_app, "GET", "/api/v1/tags?limit=0", cookies=jar
        )
        assert response.status == 422
        response = await call_app(
            tags_app, "GET", "/api/v1/tags?limit=201", cookies=jar
        )
        assert response.status == 422
        response = await call_app(
            tags_app, "GET", "/api/v1/tags?offset=-1", cookies=jar
        )
        assert response.status == 422


# ---------------------------------------------------------------------------
# deletion policy (unused / used / forced / replacement / reserved)
# ---------------------------------------------------------------------------


class TestDeletionPolicy:
    async def test_unused_tag_deletes_and_unknown_is_404(
        self, tags_app: FastAPI, jar: dict[str, str]
    ) -> None:
        tag = await _create_tag(tags_app, jar, "ephemeral")
        response = await call_app(
            tags_app,
            "DELETE",
            f"/api/v1/tags/{tag['tag_id']}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 204
        assert response.body == b""

        page = await call_app(tags_app, "GET", "/api/v1/tags", cookies=jar)
        assert page.json()["total"] == 1  # only the migration seed remains

        repeat = await call_app(
            tags_app,
            "DELETE",
            f"/api/v1/tags/{tag['tag_id']}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert repeat.status == 404
        assert _error(repeat)["code"] == "tag_not_found"

    async def test_reserved_tag_cannot_be_deleted(
        self, tags_app: FastAPI, jar: dict[str, str]
    ) -> None:
        for extra in ("", "?force=true", "?force=true&replacement_tag_id=x"):
            response = await call_app(
                tags_app,
                "DELETE",
                f"/api/v1/tags/{RESERVED}{extra}",
                headers=_mutation_headers(jar),
                cookies=jar,
            )
            if "replacement_tag_id=x" in extra:
                assert response.status == 422  # malformed UUID fails closed
                continue
            assert response.status == 409, response.body
            assert _error(response)["code"] == "reserved_tag"

    async def test_used_tag_refuses_without_force(
        self, tags_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run = await _submit_run(tags_app, jar, server_a)
        tag = await _create_tag(tags_app, jar, "kept-tag")
        assigned = await _assign(
            tags_app,
            jar,
            f"/api/v1/jobs/runs/{run}/tags",
            uuid.UUID(tag["tag_id"]),
            server_a,
        )
        assert assigned.status == 204

        refused = await call_app(
            tags_app,
            "DELETE",
            f"/api/v1/tags/{tag['tag_id']}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert refused.status == 409
        assert _error(refused)["code"] == "tag_in_use"

        page = await call_app(tags_app, "GET", "/api/v1/tags", cookies=jar)
        assert page.json()["total"] == 2  # the refusal changed nothing

    async def test_forced_delete_of_a_final_tag_requires_replacement(
        self, tags_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run = await _submit_run(tags_app, jar, server_a)
        tag = await _create_tag(tags_app, jar, "sole-classification")
        tag_id = uuid.UUID(tag["tag_id"])
        path = f"/api/v1/jobs/runs/{run}/tags"

        # Make the tag the run's ONLY tag: replace the seed.
        swap = await call_app(
            tags_app,
            "DELETE",
            f"{path}/{RESERVED}?server_id={server_a}"
            f"&replacement_tag_id={tag_id}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert swap.status == 204, swap.body

        base = f"/api/v1/tags/{tag_id}"
        forced = await call_app(
            tags_app,
            "DELETE",
            f"{base}?force=true",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert forced.status == 409
        assert _error(forced)["code"] == "tag_in_use"

        replaced = await call_app(
            tags_app,
            "DELETE",
            f"{base}?force=true&replacement_tag_id={RESERVED}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert replaced.status == 204

        listed = await call_app(
            tags_app, "GET", f"{path}?server_id={server_a}", cookies=jar
        )
        body = listed.json()
        assert body["total"] == 1
        row = body["items"][0]
        assert row["tag_id"] == str(RESERVED)
        assert row["assignment_source"] == "system"  # store's swap writer

    async def test_forced_delete_succeeds_when_entities_keep_other_tags(
        self, tags_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run = await _submit_run(tags_app, jar, server_a)
        doomed = uuid.UUID((await _create_tag(tags_app, jar, "doomed"))["tag_id"])
        keeper = uuid.UUID((await _create_tag(tags_app, jar, "keeper"))["tag_id"])
        path = f"/api/v1/jobs/runs/{run}/tags"
        for tag_id in (doomed, keeper):
            assigned = await _assign(tags_app, jar, path, tag_id, server_a)
            assert assigned.status == 204

        response = await call_app(
            tags_app,
            "DELETE",
            f"/api/v1/tags/{doomed}?force=true",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 204  # another tag survives: no replacement

        listed = await call_app(
            tags_app, "GET", f"{path}?server_id={server_a}", cookies=jar
        )
        names = {row["name"] for row in listed.json()["items"]}
        assert names == {"unclassified", "keeper"}

    async def test_replacement_must_exist_and_differ(
        self, tags_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run = await _submit_run(tags_app, jar, server_a)
        doomed = uuid.UUID((await _create_tag(tags_app, jar, "doomed"))["tag_id"])
        assigned = await _assign(
            tags_app,
            jar,
            f"/api/v1/jobs/runs/{run}/tags",
            doomed,
            server_a,
        )
        assert assigned.status == 204

        self_replace = await call_app(
            tags_app,
            "DELETE",
            f"/api/v1/tags/{doomed}?force=true&replacement_tag_id={doomed}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert self_replace.status == 422
        assert _error(self_replace)["fields"]

        unknown_replace = await call_app(
            tags_app,
            "DELETE",
            f"/api/v1/tags/{doomed}?force=true"
            f"&replacement_tag_id={uuid.uuid4()}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert unknown_replace.status == 404
        assert _error(unknown_replace)["code"] == "tag_not_found"


class TestJobTags:
    async def test_add_list_remove_cycle(
        self, tags_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run = await _submit_run(tags_app, jar, server_a)
        tag_id = uuid.UUID((await _create_tag(tags_app, jar, "hotfix"))["tag_id"])
        path = f"/api/v1/jobs/runs/{run}/tags"

        assigned = await _assign(tags_app, jar, path, tag_id, server_a)
        assert assigned.status == 204

        listed = await call_app(
            tags_app, "GET", f"{path}?server_id={server_a}", cookies=jar
        )
        assert listed.status == 200, listed.body
        body = listed.json()
        assert body["run_id"] == str(run)
        assert body["total"] == 2
        row = next(item for item in body["items"] if item["tag_id"] == str(tag_id))
        assert row["assignment_source"] == "user"
        assert len(row["assigned_by"]) == 64

        removed = await call_app(
            tags_app,
            "DELETE",
            f"{path}/{tag_id}?server_id={server_a}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert removed.status == 204
        relisted = await call_app(
            tags_app, "GET", f"{path}?server_id={server_a}", cookies=jar
        )
        assert relisted.json()["total"] == 1

    async def test_run_scope_is_blind_and_zero_uuid_is_refused(
        self, tags_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run = await _submit_run(tags_app, jar, server_a)
        path = f"/api/v1/jobs/runs/{run}/tags"

        foreign = await call_app(
            tags_app, "GET", f"{path}?server_id={SERVER_B}", cookies=jar
        )
        assert foreign.status == 404
        missing = await call_app(
            tags_app,
            "GET",
            f"/api/v1/jobs/runs/{uuid.uuid4()}/tags?server_id={SERVER_B}",
            cookies=jar,
        )
        assert missing.status == 404
        assert _scope_blind(foreign) == _scope_blind(missing)
        assert _scope_blind(foreign)["code"] == "not_found"

        general_consult = await call_app(
            tags_app,
            "GET",
            f"{path}?server_id={GENERAL_CONSULT}",
            cookies=jar,
        )
        assert general_consult.status == 404  # zero UUID: never a job-run scope

        tag_id = uuid.UUID((await _create_tag(tags_app, jar, "run-tag"))["tag_id"])
        foreign_assign = await _assign(
            tags_app, jar, path, tag_id, SERVER_B
        )
        assert foreign_assign.status == 404

    async def test_run_final_tag_protection(
        self, tags_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run = await _submit_run(tags_app, jar, server_a)
        path = f"/api/v1/jobs/runs/{run}/tags"

        refused = await call_app(
            tags_app,
            "DELETE",
            f"{path}/{RESERVED}?server_id={server_a}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert refused.status == 409
        assert _error(refused)["code"] == "last_tag"

        replacement = uuid.UUID(
            (await _create_tag(tags_app, jar, "completed"))["tag_id"]
        )
        swapped = await call_app(
            tags_app,
            "DELETE",
            f"{path}/{RESERVED}?server_id={server_a}"
            f"&replacement_tag_id={replacement}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert swapped.status == 204
        listed = await call_app(
            tags_app, "GET", f"{path}?server_id={server_a}", cookies=jar
        )
        body = listed.json()
        assert body["total"] == 1
        assert body["items"][0]["tag_id"] == str(replacement)


# ---------------------------------------------------------------------------
# the OpenAPI contract (schema generation is the frontend client's source)
# ---------------------------------------------------------------------------


def test_openapi_declares_the_history_tag_surface() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]
    assert set(paths["/api/v1/tags"]) == {"get", "post"}
    assert set(paths["/api/v1/tags/{tag_id}"]) == {"delete"}
    assert set(paths["/api/v1/jobs/runs/{run_id}/tags"]) == {"get", "post"}
    assert set(paths["/api/v1/jobs/runs/{run_id}/tags/{tag_id}"]) == {"delete"}
    # The conversation-tag routes died with the D09 coordinated deletion.
    assert (
        "/api/v1/chat/conversations/{conversation_id}/tags" not in paths
    )
    component_names = set(schema["components"]["schemas"])
    assert {
        "TagCreateBody",
        "TagView",
        "TagPage",
        "TagAssignBody",
        "EntityTagView",
        "JobTagsPage",
    } <= component_names
    assert "ConversationTagsPage" not in component_names
