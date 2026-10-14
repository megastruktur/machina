"""MAI API application: ASGI entrypoint, ``/api/v1`` prefix, truthful health.

Ownership map (contracts.md C01/C08; auth ADR ``3e79449`` §8 G1; SERVICES
``mai-api`` row: "unhealthy dependencies reported accurately"):

- :func:`create_app` is the single ASGI application factory (uvicorn factory
  form: ``uvicorn mai.api.app:create_app --factory``). The lifespan starts
  the one app-side engine owned by :mod:`mai.database` and disposes it at
  shutdown. Database unavailability never blocks startup or liveness: the
  app serves, readiness reports the truth. The durable DBOS engine is
  launched later by its owning adapter card (C05), never here.
- ``GET /api/v1/health`` is liveness: it never touches a dependency, so it
  answers while PostgreSQL is down. G1 (auth ADR §8) serves it
  unauthenticated in setup state; it grants no privilege.
- ``GET /api/v1/health/ready`` is readiness: one classified probe through
  :func:`mai.database.check_database` → ``200`` when dependencies are ok,
  ``503`` carrying the stable health class otherwise. The response body
  never contains the URL, credentials, or server error text (C01: no secret
  material in any error body).
- Every error response uses the C01 structured envelope
  ``{"error": {"code", "message", "request_id", "fields"}}``; unhandled
  exceptions collapse to a generic ``internal_error`` with no traceback.

The health GET routes stay unauthenticated in setup state (auth ADR §8 G1).
The request security boundary (``mai.api.auth``: bootstrap/login/logout/csrf,
cookie/origin/CSRF layers, §5.3 throttling) is registered by
:func:`create_app` via :func:`mai.api.auth.register_auth`; later privileged
routers join under the same ``/api/v1`` prefix through the same boundary —
no route may bypass that prefix or the envelope. Public docs/OpenAPI
listeners stay off (G1 minimal unauthenticated surface); the schema is
produced in-process via ``app.openapi()`` for the central exporter owned by
the ``openapi-client`` resource.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import State
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.api.approvals import register_approvals
from mai.api.auth import (
    bootstrap_password_from_env,
    effective_client_ip,
    forwarded_host_spoofed,
    register_auth,
    render_error_envelope,
    resolve_public_origins,
    resolve_trusted_proxies,
)
from mai.api.servers import register_servers, resolve_profiles_root
from mai.auth.sessions import AuthError, SessionStore
from mai.database import (
    DatabaseConfigError,
    DatabaseHealth,
    DatabaseStatus,
    check_database,
    create_app_engine,
    resolve_database_url,
)
from mai.policy.approvals import ApprovalStore
from mai.profiles.files import ProfileFileError, ProfileFileStore
from mai.profiles.projection import ProfileProjection

API_PREFIX = "/api/v1"
HEALTH_PATH = f"{API_PREFIX}/health"
READINESS_PATH = f"{API_PREFIX}/health/ready"


async def _database_state(state: State) -> DatabaseStatus:
    """Classified dependency truth for readiness; never raises."""
    config_error = getattr(state, "database_config_error", None)
    if isinstance(config_error, DatabaseHealth):
        return DatabaseStatus(
            config_error, "configured database URL was rejected at startup"
        )
    return await check_database(getattr(state, "database_url", None))


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start/stop the app-side database engine (the P01 dependency set).

    A missing or rejected ``MAI_DATABASE_URL`` is recorded and reported by
    readiness (``not_configured``/``invalid_config``); it does not prevent
    the app from serving liveness and truthful readiness.
    """
    try:
        url = resolve_database_url(app.state.env)
    except DatabaseConfigError as exc:
        app.state.database_url = None
        app.state.database_config_error = exc.health
    else:
        app.state.database_url = url
        app.state.database_config_error = None
        app.state.engine, app.state.session_factory = create_app_engine(url)
        app.state.sessions = SessionStore(app.state.session_factory)
        # Approval decisions (C03 rule 4): the payload-bound one-use store
        # over the one app engine; the HTTP surface (mai.api.approvals)
        # fails closed with 503 when this is absent.
        app.state.approvals = ApprovalStore(app.state.session_factory)
        # Canonical profile service (C02): the confined file store over the
        # explicitly configured data root plus the journal worker over the
        # one app engine. A store that cannot be constructed (unusable
        # root) fails closed: routes answer 503 unavailable and touch no
        # state; the value-free marker is all readiness needs.
        app.state.profiles = None
        app.state.profiles_error = None
        app.state.projection = ProfileProjection(app.state.session_factory)
        try:
            app.state.profiles = ProfileFileStore(resolve_profiles_root(app.state.env))
        except (OSError, ProfileFileError):
            app.state.profiles_error = "profiles_root_unusable"
        # One-shot env bootstrap (auth ADR §3): ``MAI_ADMIN_PASSWORD_FILE``
        # first, then ``MAI_ADMIN_PASSWORD``; ignored once a credential
        # record exists (bootstrap-only, never a runtime override). The
        # plaintext is dropped right after the store's hashing call and is
        # never logged. An unusable source simply leaves setup state, where
        # ``POST /api/v1/auth/bootstrap`` is the frozen recovery path.
        password = bootstrap_password_from_env(app.state.env)
        if password is not None:
            try:
                if not await app.state.sessions.credential_exists():
                    await app.state.sessions.bootstrap_credential(password)
            except AuthError:
                pass  # a concurrent startup won the one-shot insert
            finally:
                del password  # not retained beyond the hashing call
    try:
        yield
    finally:
        engine = getattr(app.state, "engine", None)
        if engine is not None:
            await engine.dispose()


def create_app(env: Mapping[str, str] | None = None) -> FastAPI:
    """Build the MAI ASGI application.

    ``env`` overrides the environment for database URL resolution (test
    seam, mirroring :func:`mai.database.resolve_database_url`); ``None``
    reads the process environment.
    """
    app = FastAPI(
        title="MAI controller API",
        version="0.1.0",
        lifespan=_lifespan,
        # G1 minimal unauthenticated surface: no public docs/OpenAPI listeners.
        # The schema is still generated in-process (app.openapi()).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.env = env

    # Request-boundary configuration (auth ADR §6.3/§7): exact origin
    # allowlist and trusted proxies. Unusable configuration fails closed —
    # an empty allowlist rejects every mutation, an empty proxy list ignores
    # forwarded metadata everywhere.
    try:
        app.state.public_origins = resolve_public_origins(env)
    except ValueError:
        app.state.public_origins = frozenset()
    try:
        app.state.trusted_proxies = resolve_trusted_proxies(env)
    except ValueError:
        app.state.trusted_proxies = ()
    register_auth(app)
    # Privileged routers join the same /api/v1 prefix and boundary (C01/C08):
    # the canonical server-profile surface (C02) and the approval decision
    # surface (C03 rule 4).
    register_servers(app)
    register_approvals(app)

    @app.middleware("http")
    async def attach_request_boundary(request: Request, call_next):  # noqa: ANN202
        request.state.request_id = uuid.uuid4().hex
        if forwarded_host_spoofed(request):
            # Auth ADR §6.3/S4: a trusted peer's forwarded metadata must
            # agree with Host; disagreement is an origin-spoof marker.
            response = render_error_envelope(
                request,
                HTTP_400_BAD_REQUEST,
                "request_rejected",
                "forwarded host does not match request host",
            )
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        request.state.client_ip = effective_client_ip(request)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.get(HEALTH_PATH, tags=["health"], summary="Liveness")
    async def liveness() -> dict[str, str]:
        """Process liveness; never checks dependencies (auth ADR §8 G1)."""
        return {"status": "ok"}

    @app.get(
        READINESS_PATH,
        tags=["health"],
        summary="Readiness",
        responses={
            HTTP_503_SERVICE_UNAVAILABLE: {
                "description": "At least one dependency is unhealthy"
            }
        },
    )
    async def readiness(request: Request) -> JSONResponse:
        """Classified, secret-free dependency readiness; never raises."""
        status = await _database_state(request.app.state)
        body = {
            "status": "ok" if status.ok else "unhealthy",
            "database": str(status.health),
        }
        return JSONResponse(
            status_code=HTTP_200_OK if status.ok else HTTP_503_SERVICE_UNAVAILABLE,
            content=body,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # C01: field paths only — the submitted values are never echoed.
        fields = {
            ".".join(str(part) for part in error.get("loc", ())) or "request": "invalid"
            for error in exc.errors()[:20]
        }
        return render_error_envelope(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "request body failed validation",
            fields=fields,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # Stable, browser-safe messages only; exception payloads are never
        # echoed.
        messages = {
            404: ("not_found", "resource not found"),
            405: ("method_not_allowed", "method not allowed"),
        }
        if exc.status_code >= 500:
            code, message = "internal_error", "internal server error"
        else:
            code, message = messages.get(
                exc.status_code, ("request_rejected", "request rejected")
            )
        return render_error_envelope(request, exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        # C01: no traceback, stack frame, or secret material in any body.
        return render_error_envelope(
            request, 500, "internal_error", "internal server error"
        )

    return app
