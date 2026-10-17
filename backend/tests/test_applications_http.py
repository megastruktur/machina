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
            "job_runs",
            "approvals",
            "admin_sessions",
            "admin_credential",
        ]
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
