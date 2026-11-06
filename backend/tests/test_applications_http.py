"""Applications HTTP surface: discovery/adoption/plan/lifecycle (card 08-app-api).

Exercises the REAL app harness (``create_app`` + lifespan + middleware +
handlers) against a real disposable Postgres (fixture ``migrated_url``)
through the real linear migration chain — the established
``test_schedules_http``/``test_jobs_http`` harness shape. Mocks are used
only where no fixture can produce the condition (there are none here: the
SSH lanes are NOT exercised — the real-transport discovery journey is the
``08-verify`` assembled gate; this module proves the HTTP boundary, the
canonical adoption writes, the pure plan preview and the durable facade
submits, including the C01 envelope mappings and the explicit
data-removal rule).

Secret hygiene: fixture credentials are inert constants for the disposable
fixture DB; session/CSRF tokens are minted at runtime and never asserted
verbatim.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE

BACKEND_ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = BACKEND_ROOT.parent / "catalogue" / "example-http.yaml"

#: The suites run async tests through the anyio pytest plugin (the repo's
#: async test convention); sync helpers stay unmarked and pass through.
pytestmark = pytest.mark.anyio

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-08-app-api-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry


def _catalogue_revisions() -> tuple[str, str]:
    """Split the fixture stream into its two recipe documents (v1, v2)."""
    documents = [
        yaml.serialize(node)
        for node in yaml.compose_all(CATALOGUE.read_text(encoding="utf-8"))
    ]
    return documents[0], documents[1]


RECIPE_V1, RECIPE_V2 = _catalogue_revisions()


# ---------------------------------------------------------------------------
# minimal ASGI harness (the reviewed schedules/jobs shape)
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


def _scope(method: str, path: str, headers: Mapping[str, str], body: bytes) -> dict:
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
    headers: Mapping[str, str] | None = None,
    cookies: Mapping[str, str] | None = None,
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


def _mutation_headers(jar: Mapping[str, str]) -> dict[str, str]:
    return {
        "origin": ORIGIN,
        "x-csrf-token": jar[CSRF_COOKIE],
    }


async def _login(app: FastAPI) -> dict[str, str]:
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
    return await _login(app)


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
# request-body builders
# ---------------------------------------------------------------------------


def _adopt_body(
    application_id: uuid.UUID,
    *,
    name: str = "fixture-app",
    origin: str = "compose",
    expected_revision: int = 0,
    **selectors: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "application_id": str(application_id),
        "name": name,
        "origin": origin,
        "expected_revision": expected_revision,
        "compose_project": selectors.get("compose_project", "fixture-project"),
        "working_dir": selectors.get("working_dir", "/srv/fixture"),
        "external_working_dir": selectors.get("external_working_dir"),
        "unit_name": selectors.get("unit_name"),
    }
    if origin == "systemd_unit":
        body.pop("compose_project")
        body.pop("working_dir")
    return body


def _plan_body(
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    recipe: str = RECIPE_V1,
    *,
    target_os: str = "linux",
    target_arch: str = "x86_64",
) -> dict[str, Any]:
    return {
        "application_id": str(application_id),
        "recipe": recipe,
        "target_os": target_os,
        "target_arch": target_arch,
    }


def _install_body(
    run_id: uuid.UUID,
    recipe: str = RECIPE_V1,
    *,
    target_arch: str = "x86_64",
) -> dict[str, Any]:
    return {
        "run_id": str(run_id),
        "recipe": recipe,
        "target_os": "linux",
        "target_arch": target_arch,
    }


def _update_body(
    run_id: uuid.UUID,
    *,
    recipe: str = RECIPE_V2,
    current_recipe: str = RECIPE_V1,
) -> dict[str, Any]:
    return {
        "run_id": str(run_id),
        "recipe": recipe,
        "current_recipe": current_recipe,
        "target_os": "linux",
        "target_arch": "x86_64",
    }


def _remove_body(
    run_id: uuid.UUID,
    recipe: str = RECIPE_V1,
    *,
    data_intent: str | None = "keep",
) -> dict[str, Any]:
    body = _install_body(run_id, recipe)
    if data_intent is not None:
        body["data_intent"] = data_intent
    return body


# ---------------------------------------------------------------------------
# fixture database + real app harness
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state (whole schema)."""
    with psycopg.connect(url) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


def _truncate(url: str) -> None:
    """Per-test isolation: fresh auth + jobs + adoption domains."""
    with psycopg.connect(url) as conn:
        present = conn.execute(
            "SELECT to_regclass('public.application_projection') IS NOT NULL"
        ).fetchone()
        tables = [
            "job_schedule_fires",
            "job_steps",
            "job_events",
            "job_schedules",
            "job_tags",
            "job_runs",
            "approvals",
            "admin_sessions",
            "admin_credential",
        ]
        # 0007 observation domain: notification_alerts references
        # job_runs.run_id, so TRUNCATE of job_runs is refused unless the
        # referencing table is emptied in the same statement. Only the
        # tables that actually exist are named.
        observation_tables = [
            "notification_alerts",
            "observation_findings",
            "server_observations",
        ]
        for table in observation_tables:
            exists = conn.execute(
                "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)
            ).fetchone()
            if exists and exists[0]:
                tables.append(table)
        if present and present[0]:
            tables.extend(
                ["application_projection_journal", "application_projection"]
            )
        conn.execute(f"TRUNCATE {', '.join(tables)}")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the applications HTTP surface claims "
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


@pytest.fixture(scope="session")
def bootstrapped_url(migrated_url: str) -> str:
    """Create the engine-owned ``dbos`` schema the way production does.

    The facade's enqueue path opens a DBOSClient against the system
    schema; one launch + clean shutdown plays the worker's bootstrap role
    (the engine itself is NOT left running — no run is executed here).
    """
    from mai.jobs import engine as jobs_engine

    jobs_engine.launch_worker(migrated_url)
    jobs_engine.shutdown()
    return migrated_url


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
async def apps_app(bootstrapped_url: str, tmp_path: Path):
    """App on the real fixture DB with a fresh confined data root."""
    _truncate(bootstrapped_url)
    app = _build_app(bootstrapped_url, tmp_path / "data-root")
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()
    recovery = getattr(app.state, "job_recovery", None)
    if recovery is not None:
        recovery.close()
    schedules = getattr(app.state, "schedules", None)
    if schedules is not None:
        schedules.close()


@pytest.fixture
async def jar(apps_app: FastAPI) -> dict[str, str]:
    return await login_session(apps_app)


@pytest.fixture
async def server_a(apps_app: FastAPI, jar: dict[str, str]) -> uuid.UUID:
    return await create_profile(apps_app, jar, "fixture-a")


@pytest.fixture
async def server_b(apps_app: FastAPI, jar: dict[str, str]) -> uuid.UUID:
    return await create_profile(apps_app, jar, "fixture-b")


def _apps_root(server_id: uuid.UUID) -> str:
    return f"/api/v1/servers/{server_id}/applications"


# ---------------------------------------------------------------------------
# boundary: session → origin → CSRF on every route
# ---------------------------------------------------------------------------


class TestAuthBoundary:
    async def test_mutations_without_session_are_401(
        self, apps_app: FastAPI, server_a: uuid.UUID
    ) -> None:
        application_id = uuid.uuid4()
        cases = [
            ("GET", _apps_root(server_a), None),
            (
                "POST",
                _apps_root(server_a),
                _adopt_body(application_id),
            ),
            ("GET", f"{_apps_root(server_a)}/discoveries", None),
            (
                "DELETE",
                f"{_apps_root(server_a)}/{application_id}",
                {"expected_revision": 1},
            ),
            (
                "POST",
                f"{_apps_root(server_a)}/plan",
                _plan_body(server_a, application_id),
            ),
            (
                "POST",
                f"{_apps_root(server_a)}/{application_id}/install",
                _install_body(uuid.uuid4()),
            ),
            (
                "POST",
                f"{_apps_root(server_a)}/{application_id}/update",
                _update_body(uuid.uuid4()),
            ),
            (
                "POST",
                f"{_apps_root(server_a)}/{application_id}/remove",
                _remove_body(uuid.uuid4()),
            ),
        ]
        for method, path, body in cases:
            response = await call_app(
                apps_app, method, path, json_body=body, headers={"origin": ORIGIN}
            )
            assert response.status == 401, (method, path, response.body)
            assert _error(response)["code"] == "unauthenticated"

    async def test_adopt_rejects_missing_origin_and_csrf(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        no_origin = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body=_adopt_body(uuid.uuid4()),
            cookies=jar,
        )
        assert no_origin.status == 403
        assert _error(no_origin)["code"] == "origin_rejected"
        bad_csrf = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body=_adopt_body(uuid.uuid4()),
            headers={"origin": ORIGIN, "x-csrf-token": "forged"},
            cookies=jar,
        )
        assert bad_csrf.status == 403
        assert _error(bad_csrf)["code"] == "csrf_rejected"


# ---------------------------------------------------------------------------
# listings: discovery and adoption are separate resources (AC 1)
# ---------------------------------------------------------------------------


class TestListings:
    async def test_adopted_page_starts_empty_and_is_server_scoped(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        response = await call_app(
            apps_app,
            "GET",
            _apps_root(server_a),
            cookies=jar,
        )
        assert response.status == 200
        page = response.json()
        assert page["server_id"] == str(server_a)
        assert page["applications"] == []
        assert page["total"] == 0


class TestDiscovery:
    async def test_unknown_server_is_404_before_any_transport(
        self, apps_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await call_app(
            apps_app,
            "GET",
            f"{_apps_root(uuid.uuid4())}/discoveries",
            cookies=jar,
        )
        assert response.status == 404
        assert _error(response)["code"] == "not_found"

    async def test_unconfigured_credential_source_fails_closed(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        # The harness configures no MAI_ENV_FILE: the credential source the
        # identity key resolves from does not exist — the honest answer is
        # fail-closed 503, never an empty inventory.
        response = await call_app(
            apps_app,
            "GET",
            f"{_apps_root(server_a)}/discoveries",
            cookies=jar,
        )
        assert response.status == 503
        assert _error(response)["code"] == "unavailable"


# ---------------------------------------------------------------------------
# adoption: the explicit admin action (C09 rule 1) + app-docs integration
# ---------------------------------------------------------------------------


class TestAdoption:
    async def test_adopt_writes_projection_and_record_doc(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        application_id = uuid.uuid4()
        created = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body=_adopt_body(application_id),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status == 201, created.body
        body = created.json()
        assert body["application_id"] == str(application_id)
        assert body["outcome"] in {"applied", "resumed"}
        assert body["revision"] == 1
        assert body["application"]["stale"] is False
        # The app-docs identity record was seeded and is readable through
        # the canonical documents surface (session-gated).
        record_doc = body["record_doc"]
        assert record_doc["status"] == "documented"
        doc = await call_app(
            apps_app,
            "GET",
            f"/api/v1/docs/{record_doc['doc_id']}"
            f"?server_id={server_a}&app_id={application_id}",
            cookies=jar,
        )
        assert doc.status == 200, doc.body
        assert doc.json()["app_id"] == str(application_id)

        listed = await call_app(apps_app, "GET", _apps_root(server_a), cookies=jar)
        assert listed.status == 200
        page = listed.json()
        assert page["total"] == 1
        assert page["applications"][0]["application_id"] == str(application_id)
        assert page["applications"][0]["origin"] == "compose"

    async def test_duplicate_create_conflicts_and_stale_revision_conflicts(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        application_id = uuid.uuid4()
        first = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body=_adopt_body(application_id),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status == 201
        duplicate = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body=_adopt_body(application_id),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert duplicate.status == 409
        assert _error(duplicate)["code"] == "conflict"
        stale = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body=_adopt_body(application_id, expected_revision=9),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert stale.status == 409

    async def test_cross_server_identity_conflict_is_denied(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID,
        server_b: uuid.UUID
    ) -> None:
        application_id = uuid.uuid4()
        first = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body=_adopt_body(application_id),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status == 201
        relocated = await call_app(
            apps_app,
            "POST",
            _apps_root(server_b),
            json_body=_adopt_body(application_id),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert relocated.status == 409
        assert _error(relocated)["code"] == "conflict"

    async def test_selector_overlap_on_same_server_conflicts(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        first = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body=_adopt_body(uuid.uuid4()),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status == 201
        overlap = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body=_adopt_body(uuid.uuid4(), name="other-app"),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert overlap.status == 409

    async def test_adopt_without_selector_or_unknown_server_fails(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        body = _adopt_body(uuid.uuid4())
        body.pop("compose_project")
        body.pop("working_dir")
        no_selector = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body=body,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert no_selector.status == 422
        assert _error(no_selector)["code"] == "validation_failed"
        strict = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body={**_adopt_body(uuid.uuid4()), "surprise": 1},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert strict.status == 422
        unknown = await call_app(
            apps_app,
            "POST",
            _apps_root(uuid.uuid4()),
            json_body=_adopt_body(uuid.uuid4()),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert unknown.status == 404


# ---------------------------------------------------------------------------
# revoke: non-destructive tombstone (C09 rule 6)
# ---------------------------------------------------------------------------


class TestRevoke:
    async def test_revoke_removes_record_and_keeps_external_files(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID,
        tmp_path: Path
    ) -> None:
        external = tmp_path / "external-project"
        external.mkdir()
        (external / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        application_id = uuid.uuid4()
        created = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body={
                "application_id": str(application_id),
                "name": "fixture-app",
                "origin": "compose",
                "compose_project": "fixture-project",
                "working_dir": str(external),
                "external_working_dir": str(external),
                "expected_revision": 0,
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status == 201, created.body
        revision = created.json()["revision"]

        stale = await call_app(
            apps_app,
            "DELETE",
            f"{_apps_root(server_a)}/{application_id}",
            json_body={"expected_revision": revision + 5},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert stale.status == 409

        revoked = await call_app(
            apps_app,
            "DELETE",
            f"{_apps_root(server_a)}/{application_id}",
            json_body={"expected_revision": revision},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert revoked.status == 204
        assert (external / "compose.yaml").read_text(encoding="utf-8") == (
            "services: {}\n"
        )
        listed = await call_app(apps_app, "GET", _apps_root(server_a), cookies=jar)
        assert listed.json()["total"] == 0
        gone = await call_app(
            apps_app,
            "DELETE",
            f"{_apps_root(server_a)}/{application_id}",
            json_body={"expected_revision": revision},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert gone.status == 404


# ---------------------------------------------------------------------------
# listing: orphan adoption metadata is hidden, manual map entries survive
# ---------------------------------------------------------------------------


def _map_entries(app: FastAPI, server_id: uuid.UUID) -> list[dict[str, Any]]:
    """The raw Service Map sidecar records for one server (on-disk truth)."""
    store = app.state.service_map
    return [entry.to_json() for entry in store.list_entries(server_id)]


async def _adopt_manual(
    app: FastAPI, jar: dict[str, str], server_id: uuid.UUID, name: str
) -> uuid.UUID:
    """Adopt one manual (chat/agent) map entry through the HTTP surface."""
    application_id = uuid.uuid4()
    created = await call_app(
        app,
        "POST",
        _apps_root(server_id),
        json_body={
            "application_id": str(application_id),
            "name": name,
            "origin": "manual",
            "category": "media",
            "description": f"the {name} service",
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert created.status == 201, created.body
    return application_id


class TestListingOrphanMetadata:
    async def test_orphan_sidecars_are_excluded_and_manual_entry_survives(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        # A live adopted compose application (canonical projection row).
        live_id = uuid.uuid4()
        adopted = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body={
                "application_id": str(live_id),
                "name": "live-app",
                "origin": "compose",
                "compose_project": "live-project",
                "working_dir": "/srv/live",
                "category": "dev",
                "description": "the live service",
                "expected_revision": 0,
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert adopted.status == 201, adopted.body
        revision = adopted.json()["revision"]

        # A legitimate manual map-only entry (no SQL adoption row).
        manual_id = await _adopt_manual(apps_app, jar, server_a, "manual-app")

        # Orphan sidecars: Service Map metadata whose adoption record is
        # gone (revoke) or never existed. They are written straight into the
        # sidecar store to reproduce the post-revoke shape.
        store = apps_app.state.service_map
        orphan_compose = uuid.uuid4()
        store.upsert_entry(
            server_a,
            name="orphan-compose",
            origin="compose",
            application_id=orphan_compose,
            compose_project="orphan-project",
        )
        orphan_systemd = uuid.uuid4()
        store.upsert_entry(
            server_a,
            name="orphan-systemd",
            origin="systemd_unit",
            application_id=orphan_systemd,
            unit_name="orphan.service",
        )
        orphan_docker = uuid.uuid4()
        store.upsert_entry(
            server_a,
            name="orphan-docker",
            origin="docker_container",
            application_id=orphan_docker,
        )

        listed = await call_app(apps_app, "GET", _apps_root(server_a), cookies=jar)
        assert listed.status == 200, listed.body
        page = listed.json()
        ids = [app["application_id"] for app in page["applications"]]
        assert page["total"] == 2
        assert set(ids) == {str(live_id), str(manual_id)}
        assert str(orphan_compose) not in ids
        assert str(orphan_systemd) not in ids
        assert str(orphan_docker) not in ids

        # The live adopted entry keeps its Service Map metadata.
        live_view = next(
            a for a in page["applications"] if a["application_id"] == str(live_id)
        )
        assert live_view["category"] == "dev"
        assert live_view["description"] == "the live service"

        # Revoke the live application: its orphaned sidecar must not
        # reappear in the listing.
        revoked = await call_app(
            apps_app,
            "DELETE",
            f"{_apps_root(server_a)}/{live_id}",
            json_body={"expected_revision": revision},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert revoked.status == 204, revoked.body
        listed = await call_app(apps_app, "GET", _apps_root(server_a), cookies=jar)
        assert listed.status == 200, listed.body
        page = listed.json()
        ids = [app["application_id"] for app in page["applications"]]
        assert page["total"] == 1
        assert ids == [str(manual_id)]

        # The sidecar itself is retained on disk (non-destructive), and the
        # manual entry is still there.
        stored = _map_entries(apps_app, server_a)
        stored_ids = {entry["application_id"] for entry in stored}
        assert str(live_id) in stored_ids
        assert str(manual_id) in stored_ids

    async def test_pagination_counts_only_the_filtered_set(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        manual_ids = [
            await _adopt_manual(apps_app, jar, server_a, f"manual-{index}")
            for index in range(3)
        ]
        store = apps_app.state.service_map
        for index in range(2):
            store.upsert_entry(
                server_a,
                name=f"orphan-{index}",
                origin="compose",
                application_id=uuid.uuid4(),
                compose_project=f"orphan-project-{index}",
            )

        first = await call_app(
            apps_app,
            "GET",
            f"{_apps_root(server_a)}?limit=2&offset=0",
            cookies=jar,
        )
        assert first.status == 200, first.body
        page = first.json()
        assert page["total"] == 3
        assert page["limit"] == 2
        assert page["offset"] == 0
        first_ids = [app["application_id"] for app in page["applications"]]
        assert len(first_ids) == 2

        second = await call_app(
            apps_app,
            "GET",
            f"{_apps_root(server_a)}?limit=2&offset=2",
            cookies=jar,
        )
        assert second.status == 200, second.body
        page = second.json()
        assert page["total"] == 3
        assert page["limit"] == 2
        assert page["offset"] == 2
        second_ids = [app["application_id"] for app in page["applications"]]
        assert len(second_ids) == 1

        # Deterministic repeated pages: the same request yields the same
        # ids, and the pages cover exactly the manual entries.
        again = await call_app(
            apps_app,
            "GET",
            f"{_apps_root(server_a)}?limit=2&offset=0",
            cookies=jar,
        )
        assert again.status == 200, again.body
        assert [
            app["application_id"] for app in again.json()["applications"]
        ] == first_ids
        assert set(first_ids + second_ids) == {str(aid) for aid in manual_ids}

    async def test_readoption_does_not_duplicate_the_listed_entry(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        application_id = uuid.uuid4()
        body = {
            "application_id": str(application_id),
            "name": "recycled-app",
            "origin": "compose",
            "compose_project": "recycled-project",
            "working_dir": "/srv/recycled",
            "expected_revision": 0,
        }
        created = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body=body,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status == 201, created.body
        revision = created.json()["revision"]
        revoked = await call_app(
            apps_app,
            "DELETE",
            f"{_apps_root(server_a)}/{application_id}",
            json_body={"expected_revision": revision},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert revoked.status == 204, revoked.body

        readopted = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body=body,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert readopted.status == 201, readopted.body

        listed = await call_app(apps_app, "GET", _apps_root(server_a), cookies=jar)
        assert listed.status == 200, listed.body
        page = listed.json()
        ids = [app["application_id"] for app in page["applications"]]
        assert page["total"] == 1
        assert ids == [str(application_id)]


# ---------------------------------------------------------------------------
# plan preview: pure, deterministic, actual resources (C09 rule 3)
# ---------------------------------------------------------------------------


class TestPlanPreview:
    async def test_plan_shows_actual_resources_and_is_deterministic(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        application_id = uuid.uuid4()
        body = _plan_body(server_a, application_id)
        first = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/plan",
            json_body=body,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status == 200, first.body
        plan = first.json()
        assert plan["server_id"] == str(server_a)
        assert plan["application_id"] == str(application_id)
        assert plan["compose_path"] == (
            f"/var/lib/mai/apps/{application_id}/compose.yaml"
        )
        assert plan["image"].startswith("docker.io/filebrowser/filebrowser@sha256:")
        assert plan["required_grants"] == ["docker.compose"]
        assert plan["health_probe"]["path"] == "/health"
        operations = [o["op"] for o in plan["operations"]]
        assert "pull_image" in operations and "start" in operations
        assert plan["limitations"]
        assert len(plan["plan_sha256"]) == 64

        again = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/plan",
            json_body=body,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert again.status == 200
        assert again.json()["plan_sha256"] == plan["plan_sha256"]

    async def test_plan_rejects_unpinned_image_and_unsupported_target(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        application_id = uuid.uuid4()
        unpinned_recipe = RECIPE_V1.replace(
            "@sha256:a4da9ca8364b0a1fc5dd36f7add92582bf673c0ae0bda8dd9bd13062c41d1715",
            ":v2.31.2",
        )
        unpinned = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/plan",
            json_body=_plan_body(server_a, application_id, unpinned_recipe),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert unpinned.status == 422
        assert _error(unpinned)["fields"] == {"recipe": "invalid"}
        arm = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/plan",
            json_body=_plan_body(
                server_a, application_id, target_arch="arm"
            ),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert arm.status == 422
        assert _error(arm)["fields"] == {"target_arch": "invalid"}

    async def test_plan_unknown_server_is_404(
        self, apps_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(uuid.uuid4())}/plan",
            json_body=_plan_body(uuid.uuid4(), uuid.uuid4()),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 404


# ---------------------------------------------------------------------------
# lifecycle submits through the EXISTING jobs facade (AC 1/2)
# ---------------------------------------------------------------------------


class TestLifecycleSubmits:
    async def test_install_submit_is_idempotent_and_durable(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        application_id = uuid.uuid4()
        run_id = uuid.uuid4()
        body = _install_body(run_id)
        created = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/{application_id}/install",
            json_body=body,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status == 201, created.body
        created_view = created.json()
        assert created_view["run"]["run_id"] == str(run_id)
        assert created_view["deduplicated"] is False

        # Idempotent submit (C05 rule 5): the same intent re-attaches to
        # the SAME run with 200, never a second run row.
        repeat = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/{application_id}/install",
            json_body=body,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert repeat.status == 200, repeat.body
        assert repeat.json()["deduplicated"] is True
        assert repeat.json()["run"]["run_id"] == str(run_id)

        # A different intent under the same id is a genuine conflict.
        conflict = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/{application_id}/install",
            json_body=_install_body(run_id, target_arch="aarch64"),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert conflict.status == 409

        # The submit is durable: the replayed state is the frozen
        # ``queued`` edge and the facade row names the lifecycle capability
        # as the requested grant (the plan payload — capability id, plan
        # document, lease resource — travels digest-bound into the engine;
        # the step itself runs worker-side, the 08-verify gate's journey).
        detail = await call_app(
            apps_app,
            "GET",
            f"/api/v1/jobs/runs/{run_id}",
            cookies=jar,
        )
        assert detail.status == 200, detail.body
        assert detail.json()["state"] == "queued"

    async def test_lifecycle_submit_records_honest_service_label(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        """F3: the frozen run label names the service target and action.

        The label is derived server-side from the validated recipe name and
        the pinned lifecycle capability (never client text), rides the
        digest-bound step payload, and lands in ``job_runs.label`` through
        the REAL lifecycle-submit → jobs facade path.
        """

        async def _submit_and_label(
            application_id: uuid.UUID, action: str, body: dict[str, Any]
        ) -> str:
            run_id = uuid.UUID(body["run_id"])
            created = await call_app(
                apps_app,
                "POST",
                f"{_apps_root(server_a)}/{application_id}/{action}",
                json_body=body,
                headers=_mutation_headers(jar),
                cookies=jar,
            )
            assert created.status == 201, created.body
            detail = await call_app(
                apps_app, "GET", f"/api/v1/jobs/runs/{run_id}", cookies=jar
            )
            assert detail.status == 200, detail.body
            return detail.json()["label"]

        # Install: "example-http — install" (recipe name — action).
        install_label = await _submit_and_label(
            uuid.uuid4(), "install", _install_body(uuid.uuid4())
        )
        assert install_label == "example-http — install"

        # Update: same service target, honest update action; unusual recipe
        # names (underscores/digits are valid slugs) pass through verbatim.
        # The baseline assertion must pin a DIFFERENT revision (v1).
        unusual_recipe = RECIPE_V1.replace(
            "name: example-http", "name: omp_service_2"
        )
        update_label = await _submit_and_label(
            uuid.uuid4(),
            "update",
            _update_body(
                uuid.uuid4(), recipe=unusual_recipe, current_recipe=RECIPE_V1
            ),
        )
        assert update_label == "omp_service_2 — update"

    async def test_remove_requires_explicit_data_intent(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        application_id = uuid.uuid4()
        absent = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/{application_id}/remove",
            json_body=_remove_body(uuid.uuid4(), data_intent=None),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert absent.status == 422
        assert _error(absent)["code"] == "validation_failed"
        unknown = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/{application_id}/remove",
            json_body=_remove_body(uuid.uuid4(), data_intent="wipe"),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert unknown.status == 422
        kept = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/{application_id}/remove",
            json_body=_remove_body(uuid.uuid4(), data_intent="keep"),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert kept.status == 201, kept.body

    async def test_update_refuses_baseline_equal_to_new_recipe(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        application_id = uuid.uuid4()
        same = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/{application_id}/update",
            json_body=_update_body(uuid.uuid4(), recipe=RECIPE_V1,
                                   current_recipe=RECIPE_V1),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert same.status == 422
        updated = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/{application_id}/update",
            json_body=_update_body(uuid.uuid4()),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert updated.status == 201, updated.body
        run_id = updated.json()["run"]["run_id"]
        detail = await call_app(
            apps_app, "GET", f"/api/v1/jobs/runs/{run_id}", cookies=jar
        )
        assert detail.json()["state"] == "queued"

    async def test_cross_server_application_identity_conflicts(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID,
        server_b: uuid.UUID
    ) -> None:
        application_id = uuid.uuid4()
        adopted = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body=_adopt_body(application_id),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert adopted.status == 201
        conflict = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_b)}/{application_id}/install",
            json_body=_install_body(uuid.uuid4()),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert conflict.status == 409
        assert _error(conflict)["code"] == "conflict"

    async def test_lifecycle_submit_rejects_bad_recipe_and_unknown_target(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        application_id = uuid.uuid4()
        unpinned = RECIPE_V1.replace(
            "@sha256:a4da9ca8364b0a1fc5dd36f7add92582bf673c0ae0bda8dd9bd13062c41d1715",
            ":latest",
        )
        bad_recipe = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/{application_id}/install",
            json_body=_install_body(uuid.uuid4(), unpinned),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert bad_recipe.status == 422
        unknown = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(uuid.uuid4())}/{application_id}/install",
            json_body=_install_body(uuid.uuid4()),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert unknown.status == 404

    async def test_submitted_run_records_the_requested_grant(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        application_id = uuid.uuid4()
        run_id = uuid.uuid4()
        created = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/{application_id}/install",
            json_body=_install_body(run_id),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status == 201
        # The facade row names the lifecycle capability as the requested
        # grant (the plan's own docker.compose grant travels inside the
        # digest-bound payload).
        with psycopg.connect(apps_app.state.database_url) as conn:
            grants = conn.execute(
                "SELECT requested_grants FROM job_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()[0]
        assert list(grants) == ["app.install"]


# ---------------------------------------------------------------------------
# D09: runbook execution through the fresh non-interactive session
# ---------------------------------------------------------------------------


def _catalog_body(name: str = "fixture-runbook") -> dict[str, Any]:
    return {
        "name": name,
        "category": "system",
        "description": "fixture runbook for the session admission proof",
        "instructions": {
            "install": "install via compose up -d",
            "update": "pull and restart",
            "uninstall": "compose down",
            "backup": "tar the volume",
            "verify": "curl the health endpoint",
        },
    }


class TestRunbookSessionExecution:
    async def _seed_catalog(
        self, apps_app: FastAPI, jar: dict[str, str]
    ) -> str:
        response = await call_app(
            apps_app,
            "POST",
            "/api/v1/applications/catalog",
            json_body=_catalog_body(),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 200, response.body
        return response.json()["runbook_id"]

    async def test_execute_admits_a_fresh_runbook_session(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        runbook_id = await self._seed_catalog(apps_app, jar)
        created = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/{runbook_id}/execute",
            json_body={"action": "verify"},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status == 201, created.body
        view = created.json()
        session_id = uuid.UUID(view["session_id"])
        run_id = uuid.UUID(view["run_id"])
        assert view["state"] == "queued"
        assert view["application_id"] == runbook_id

        # The durable admission: one session row (source=runbook), one
        # queued child run, one admission-ledger row, ONE user entry
        # carrying the runbook_ref provenance.
        with psycopg.connect(apps_app.state.database_url, row_factory=psycopg.rows.dict_row) as conn:
            session_row = conn.execute(
                "SELECT server_id, source, status, title FROM sessions "
                "WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            assert session_row is not None
            assert str(session_row["server_id"]) == str(server_a)
            assert session_row["source"] == "runbook"
            assert session_row["status"] == "idle"
            assert session_row["title"].endswith(" — verify")
            run_row = conn.execute(
                "SELECT session_id, state, model_profile FROM session_runs "
                "WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            assert run_row is not None
            assert str(run_row["session_id"]) == str(session_id)
            assert run_row["state"] == "queued"
            assert run_row["model_profile"] == "default"
            admission = conn.execute(
                "SELECT source, run_id FROM session_admissions "
                "WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            assert admission is not None
            assert admission["source"] == "runbook"
            entries = conn.execute(
                "SELECT kind, payload, run_id, durable_source_id "
                "FROM session_entries WHERE session_id = %s ORDER BY seq",
                (session_id,),
            ).fetchall()
            assert len(entries) == 2  # user_message + queued run_state
            entry = entries[0]
            assert entry["kind"] == "user_message"
            assert str(entry["run_id"]) == str(run_id)
            assert entry["durable_source_id"].startswith("turn:")
            payload = (
                entry["payload"]
                if isinstance(entry["payload"], dict)
                else json.loads(entry["payload"])
            )
            assert payload["source"] == "runbook"
            assert payload["runbook_ref"]["runbook_id"] == runbook_id
            assert "verify" in payload["text"]
            assert entries[1]["kind"] == "run_state"

    async def test_execute_persists_custom_instructions_on_catalog_staging(
        self, apps_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        runbook_id = await self._seed_catalog(apps_app, jar)
        created = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(server_a)}/{runbook_id}/execute",
            json_body={
                "action": "install",
                "custom_instructions": "host path: /srv/fixture",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status == 201, created.body
        # The catalog-only target was STAGED as an installed=False entry so
        # the operator notes survive (unchanged service-map semantics).
        listed = await call_app(
            apps_app,
            "GET",
            _apps_root(server_a),
            headers={"origin": ORIGIN},
            cookies=jar,
        )
        assert listed.status == 200
        entries = listed.json()["applications"]
        staged = [
            item
            for item in entries
            if item["application_id"] == runbook_id
        ]
        assert len(staged) == 1
        assert staged[0]["installed"] is False
        assert staged[0]["custom_instructions"] == "host path: /srv/fixture"

    async def test_execute_admits_root_run_with_execution_facade(
        self,
        apps_app: FastAPI,
        jar: dict[str, str],
        server_a: uuid.UUID,
    ) -> None:
        """S3 (queued-chat hotfix): runbook root admission carries the
        retained execution facade atomically — job_runs depth 0 with the
        REAL resolved profile/prompt revisions and exactly the narrowed
        capability binding the execution intent freezes, plus the
        mandatory classification. Every admission stays exactly one row
        of each kind; a cross-server attempt admits nothing."""
        from types import SimpleNamespace

        from mai.api.capability_defaults import resolve_default_capabilities
        from mai.contracts.server import parse_server_profile_text
        from mai.tag_store import RESERVED_UNCLASSIFIED_TAG_ID

        # A server-SCOPED adopted entry (unlike catalog runbooks, which
        # are deliberately server-agnostic): the scope-rejection leg
        # below relies on this binding.
        application_id = uuid.uuid4()
        adopted = await call_app(
            apps_app,
            "POST",
            _apps_root(server_a),
            json_body=_adopt_body(application_id),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert adopted.status == 201, adopted.body

        # The expected binding, resolved the way admission resolves it.
        stored = apps_app.state.profiles.read(server_a)
        parsed = parse_server_profile_text(stored.text)
        expected_caps = resolve_default_capabilities(
            SimpleNamespace(app=apps_app), server_a, parsed.capabilities
        )
        models_store = getattr(apps_app.state, "models", None)
        expected_model = None
        if models_store is not None:
            try:
                expected_model = models_store.get_selection().profile
            except Exception:
                expected_model = None
        if expected_model is None:
            expected_model = "default"

        async def _execute(server: uuid.UUID, action: str) -> HttpResponse:
            return await call_app(
                apps_app,
                "POST",
                f"{_apps_root(server)}/{application_id}/execute",
                json_body={"action": action},
                headers=_mutation_headers(jar),
                cookies=jar,
            )

        first = await _execute(server_a, "update")
        assert first.status == 201, first.body
        view = first.json()
        assert view["state"] == "queued"
        run_id = uuid.UUID(view["run_id"])

        with psycopg.connect(
            apps_app.state.database_url, row_factory=psycopg.rows.dict_row
        ) as conn:
            run_row = conn.execute(
                "SELECT state, parent_run_id, model_profile FROM session_runs "
                "WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            facade = conn.execute(
                "SELECT server_id, depth, parent_run_id, profile_revision, "
                "prompt_revision, model_profile, requested_grants "
                "FROM job_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            tags = conn.execute(
                "SELECT tag_id, assignment_source FROM job_tags "
                "WHERE job_id = %s",
                (run_id,),
            ).fetchall()
        assert run_row is not None and facade is not None
        assert run_row["state"] == "queued"
        assert run_row["parent_run_id"] is None
        assert run_row["model_profile"] == expected_model
        assert facade["server_id"] == server_a
        assert facade["depth"] == 0  # a root on the retained execution tree
        assert facade["parent_run_id"] is None
        assert facade["profile_revision"] == stored.revision
        assert facade["prompt_revision"] == parsed.prompt_revision
        assert facade["model_profile"] == expected_model
        # No widening: exactly the resolved capability binding, sorted.
        assert facade["requested_grants"] == sorted(expected_caps)
        assert len(tags) == 1  # the 0011 mandatory classification
        assert tags[0]["tag_id"] == uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)
        assert tags[0]["assignment_source"] == "system"

        # A second admission is a FRESH session/run by design (random
        # identities): every admitted root keeps exactly one row of each
        # kind — no duplicates from the admission path itself.
        second = await _execute(server_a, "verify")
        assert second.status == 201, second.body
        run2_id = uuid.UUID(second.json()["run_id"])
        assert run2_id != run_id
        with psycopg.connect(
            apps_app.state.database_url, row_factory=psycopg.rows.dict_row
        ) as conn:
            runs = conn.execute(
                "SELECT COUNT(*) AS n FROM session_runs "
                "WHERE run_id = ANY(%s)",
                ([run_id, run2_id],),
            ).fetchone()["n"]
            facades = conn.execute(
                "SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE depth = 0) "
                "AS roots FROM job_runs WHERE run_id = ANY(%s)",
                ([run_id, run2_id],),
            ).fetchone()
            classifications = conn.execute(
                "SELECT COUNT(*) AS n FROM job_tags WHERE job_id = ANY(%s)",
                ([run_id, run2_id],),
            ).fetchone()["n"]
            facade2 = conn.execute(
                "SELECT profile_revision, prompt_revision, requested_grants "
                "FROM job_runs WHERE run_id = %s",
                (run2_id,),
            ).fetchone()
            total_facades = conn.execute(
                "SELECT COUNT(*) AS n FROM job_runs"
            ).fetchone()["n"]
        assert runs == 2
        assert facades["n"] == 2 and facades["roots"] == 2
        assert classifications == 2
        assert facade2["profile_revision"] == stored.revision
        assert facade2["prompt_revision"] == parsed.prompt_revision
        assert facade2["requested_grants"] == sorted(expected_caps)
        assert total_facades == 2  # no stray facade rows anywhere

        # Scope rejection: the catalog record is deliberately fleet-wide,
        # so the honest scope boundary is a server with NO canonical
        # profile — value-free 404 before anything is admitted.
        unknown_server = uuid.uuid4()
        cross = await call_app(
            apps_app,
            "POST",
            f"{_apps_root(unknown_server)}/{application_id}/execute",
            json_body={"action": "verify"},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert cross.status == 404, cross.body
        assert _error(cross)["code"] == "not_found"
        with psycopg.connect(
            apps_app.state.database_url, row_factory=psycopg.rows.dict_row
        ) as conn:
            after = conn.execute(
                "SELECT (SELECT COUNT(*) FROM sessions "
                "        WHERE server_id = %s) AS unknown_sessions, "
                "       (SELECT COUNT(*) FROM session_runs "
                "        WHERE server_id = %s) AS unknown_runs, "
                "       (SELECT COUNT(*) FROM job_runs "
                "        WHERE server_id = %s) AS unknown_facades",
                (unknown_server, unknown_server, unknown_server),
            ).fetchone()
        assert after["unknown_sessions"] == 0
        assert after["unknown_runs"] == 0
        assert after["unknown_facades"] == 0
