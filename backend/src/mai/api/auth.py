"""HTTP authentication boundary: cookies, origin/CSRF, throttling, G1 setup.

Ownership (auth ADR ``3e79449`` §10, contracts.md C01/C08): ``02-auth-api``
owns the request security boundary — cookie issuing/clearance, exact-origin
allowlist enforcement, CSRF double-submit, §5.3 login throttling, and the G1
setup-state surface. Credential/session record semantics stay in
:mod:`mai.auth.sessions` (``02-auth-store``); this module performs no SQL.

Frozen surface implemented here (design targets, ADR §3–§5, §9):

- ``POST /api/v1/auth/bootstrap`` — one-shot administrator bootstrap; ``201``
  once, ``409 conflict`` permanently afterwards (G1 setup-state recovery).
- ``POST /api/v1/auth/login`` — origin-checked, throttled; ``204`` mints the
  ``mai_session`` (HttpOnly) and ``mai_csrf`` (double-submit) cookies.
- ``POST /api/v1/auth/logout`` — session + origin + CSRF layers; revokes
  exactly the presenting session; effective on the next request (S2).
- ``GET  /api/v1/auth/csrf`` — authenticated CSRF re-mint (safe method).

Layered mutation rule (ADR §5), checked in order: valid session, then exact
``Origin`` allowlist match, then ``X-CSRF-Token`` == ``mai_csrf`` cookie.
``Referer`` is never an origin substitute. Login/bootstrap run before any
session exists, so their pre-auth layer is the exact-origin check; a fresh
token is minted at every successful login and no pre-auth token is ever
upgraded (fixation, §4). Safe methods perform no state change and skip
layers 2–3 (§5).

Trusted forwarded metadata (ADR §6.3, S4): ``X-Forwarded-For`` and
``X-Forwarded-Host`` are honored only from ``MAI_TRUSTED_PROXIES`` peers;
from any other peer they are ignored, and a trusted peer whose
``X-Forwarded-Host`` disagrees with ``Host`` is rejected
``400 request_rejected``. Behind the single trusted hop the rightmost
``X-Forwarded-For`` entry keys throttle state; the client-asserted leftmost
entry is never a security input.

Secret hygiene: error bodies carry stable value-free codes only (C01); the
session token exists only as an HttpOnly ``Set-Cookie`` value and is never
echoed; CSRF comparison is constant-time; bootstrap sources are read once
and dropped after the store's hashing call.

Throttle storage note — ADR §5.3 says "all verified server-side in
Postgres": the P02 migration chain is the exclusive ``migration-chain``
resource of ``02-auth-store`` (no throttle table exists, and this slice's
allowlist has no migration path), so the frozen counters live process-local
here with the exact frozen parameters and an async, store-shaped interface
a Postgres-backed owner can adopt without route changes. Counters survive
neither restart nor a multi-process deployment (single uvicorn process at
P02). Recorded in ``mai_evidence/02-auth-api/README.md`` as a limitation.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import secrets
import time
import urllib.parse
import os
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Final

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.status import (
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_429_TOO_MANY_REQUESTS,
)

from mai.auth.sessions import (
    SESSION_ABSOLUTE_TTL,
    AuthError,
    AuthErrorCode,
    IssuedSession,
    SessionStore,
)

__all__ = [
    "BOOTSTRAP_PATH",
    "BoundaryError",
    "CSRF_COOKIE",
    "CSRF_PATH",
    "CSRF_HEADER",
    "LoginThrottle",
    "LOGOUT_PATH",
    "LOGIN_PATH",
    "SESSION_COOKIE",
    "PasswordBody",
    "bootstrap_password_from_env",
    "clear_auth_cookies",
    "effective_client_ip",
    "forwarded_host_spoofed",
    "register_auth",
    "render_error_envelope",
    "resolve_public_origins",
    "resolve_trusted_proxies",
    "set_auth_cookies",
]

#: Frozen cookie/header names (ADR §4–§5; design targets implemented here).
SESSION_COOKIE: Final = "mai_session"
CSRF_COOKIE: Final = "mai_csrf"
CSRF_HEADER: Final = "x-csrf-token"

#: Frozen route names (ADR §3/§8; C08 rule 1).
BOOTSTRAP_PATH: Final = "/api/v1/auth/bootstrap"
LOGIN_PATH: Final = "/api/v1/auth/login"
LOGOUT_PATH: Final = "/api/v1/auth/logout"
CSRF_PATH: Final = "/api/v1/auth/csrf"

#: Environment contracts (ADR §3/§6.3/§7).
PUBLIC_ORIGINS_VAR: Final = "MAI_PUBLIC_ORIGINS"
TRUSTED_PROXIES_VAR: Final = "MAI_TRUSTED_PROXIES"
BOOTSTRAP_PASSWORD_VAR: Final = "MAI_ADMIN_PASSWORD"
BOOTSTRAP_PASSWORD_FILE_VAR: Final = "MAI_ADMIN_PASSWORD_FILE"

#: Input bound mirroring the store's DoS guard (mai.auth.sessions).
_MAX_PASSWORD_CHARS: Final = 1024

#: §5.3 frozen throttle parameters: fixed per-source-IP delay starting at
#: 1 s, doubling per consecutive failure, capped at 60 s, plus a global
#: ceiling of 5 failed attempts per rolling minute.
_THROTTLE_BASE_DELAY_S: Final = 1.0
_THROTTLE_MAX_DELAY_S: Final = 60.0
_THROTTLE_WINDOW_S: Final = 60.0
_THROTTLE_MAX_ATTEMPTS: Final = 5

#: Memory bounds: ``X-Forwarded-For`` behind a trusted proxy is
#: client-influenced, so per-IP state and the global window are capped.
_THROTTLE_MAX_TRACKED_IPS: Final = 4096
_THROTTLE_RECENT_BOUND: Final = 10_000

#: 256 bits of CSPRNG entropy per CSRF token (ADR §5).
_CSRF_TOKEN_BYTES: Final = 32


class BoundaryError(Exception):
    """One rejected request at the authentication boundary.

    Rendered as the C01 structured envelope by the handler registered in
    :func:`register_auth`. ``clear_auth_cookies`` marks the ADR §4 session
    rejections (missing/expired/revoked) that must also clear both cookies.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        clear_auth_cookies: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.clear_auth_cookies = clear_auth_cookies


class ErrorBody(BaseModel):
    """C01 stable error fields (browser-safe; never secret material)."""

    code: str
    message: str
    request_id: str
    fields: dict[str, str] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    """C01 envelope shape for OpenAPI documentation of error responses."""

    error: ErrorBody


class PasswordBody(BaseModel):
    """Login/bootstrap payload; bounds mirror the store's input guard."""

    password: str = Field(min_length=1, max_length=_MAX_PASSWORD_CHARS)


# ---------------------------------------------------------------------------
# Environment resolution (fail-closed on unusable configuration)
# ---------------------------------------------------------------------------


def _env_value(env: Mapping[str, str] | None, key: str) -> str:
    source = os.environ if env is None else env
    return source.get(key, "")


def resolve_public_origins(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """Exact browser origins allowed to mutate (ADR §7).

    Comma-separated ``MAI_PUBLIC_ORIGINS`` entries; wildcard entries and
    non-origin shapes raise ``ValueError`` (the caller fails closed to an
    empty allowlist — every mutation then rejects).
    """
    origins: set[str] = set()
    for entry in _env_value(env, PUBLIC_ORIGINS_VAR).split(","):
        origin = entry.strip()
        if not origin:
            continue
        if "*" in origin:
            raise ValueError(f"{PUBLIC_ORIGINS_VAR}: wildcard entries are not allowed")
        parts = urllib.parse.urlsplit(origin)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.netloc
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError(f"{PUBLIC_ORIGINS_VAR}: entry is not an exact origin")
        origins.add(origin)
    return frozenset(origins)


def resolve_trusted_proxies(
    env: Mapping[str, str] | None = None,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network | str, ...]:
    """Peers allowed to set forwarded metadata (ADR §6.3).

    ``MAI_TRUSTED_PROXIES`` entries are CIDR networks where parseable,
    otherwise literal host names. Unset/empty ⇒ no trusted peer ⇒ forwarded
    headers are ignored everywhere (fail-closed default).
    """
    entries: list[ipaddress.IPv4Network | ipaddress.IPv6Network | str] = []
    for part in _env_value(env, TRUSTED_PROXIES_VAR).split(","):
        token = part.strip()
        if not token:
            continue
        try:
            entries.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            entries.append(token)
    return tuple(entries)


def bootstrap_password_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """First configured credential source (ADR §3 order): file, then direct.

    Returns ``None`` when unconfigured or unusable (setup state stays
    reachable through ``POST /api/v1/auth/bootstrap`` — the frozen recovery
    path). Only trailing EOL characters are stripped from the file form; the
    value is dropped by the caller after the store's hashing call and is
    never logged.
    """
    file_path = _env_value(env, BOOTSTRAP_PASSWORD_FILE_VAR)
    if file_path:
        try:
            raw = Path(file_path).read_text(encoding="utf-8")
        except OSError:
            return None
        password = raw.rstrip("\r\n")
        return password or None
    direct = _env_value(env, BOOTSTRAP_PASSWORD_VAR)
    if direct and len(direct) <= _MAX_PASSWORD_CHARS:
        return direct
    return None


# ---------------------------------------------------------------------------
# Trusted-forwarded-metadata helpers (ADR §6.3, S4)
# ---------------------------------------------------------------------------


def _is_loopback_host(host: str) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _origin_is_loopback(origin: str) -> bool:
    return _is_loopback_host(urllib.parse.urlsplit(origin).hostname or "")


def _peer_is_trusted(
    peer: str,
    trusted: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network | str, ...],
) -> bool:
    if not peer or not trusted:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return any(isinstance(entry, str) and entry == peer for entry in trusted)
    return any(
        isinstance(entry, (ipaddress.IPv4Network, ipaddress.IPv6Network))
        and entry.version == address.version
        and address in entry
        for entry in trusted
    )


def forwarded_host_spoofed(request: Request) -> bool:
    """True when a trusted peer's ``X-Forwarded-Host`` disagrees with Host.

    From non-trusted peers forwarded headers are ignored entirely (ADR
    §6.3), so only trusted peers can trip the ``400 request_rejected`` S4
    marker.
    """
    peer = request.client.host if request.client else ""
    trusted: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network | str, ...] = (
        request.app.state.trusted_proxies
    )
    if not _peer_is_trusted(peer, trusted):
        return False
    forwarded = request.headers.get("x-forwarded-host")
    host = request.headers.get("host")
    return forwarded is not None and host is not None and forwarded != host


def effective_client_ip(request: Request) -> str:
    """Throttle key: TCP peer, or the trusted proxy's observed client.

    With a trusted peer, the rightmost ``X-Forwarded-For`` entry is the
    client observed by the nearest trusted hop (single-proxy deployment);
    the client-asserted leftmost entry is never a security input.
    """
    peer = request.client.host if request.client else "unknown"
    trusted: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network | str, ...] = (
        request.app.state.trusted_proxies
    )
    if _peer_is_trusted(peer, trusted):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            rightmost = forwarded.split(",")[-1].strip()
            if rightmost:
                return rightmost
    return peer


# ---------------------------------------------------------------------------
# §5.3 login throttling (process-local counters; see module docstring)
# ---------------------------------------------------------------------------


class LoginThrottle:
    """Per-source-IP doubling delay + global failed-attempts ceiling.

    Frozen parameters (ADR §5.3): delay starts at 1 s, doubles per
    consecutive failure, caps at 60 s; a global ceiling rejects the 5th
    failed attempt inside a rolling minute; a successful login resets the
    per-IP counter. ``clock``/``sleep`` are injection seams for
    deterministic tests; production uses monotonic time and asyncio sleep.

    Storage is process-local (limitation in the module docstring): the
    async, store-shaped interface lets a Postgres-backed owner replace the
    counters without touching the routes.
    """

    __slots__ = (
        "_base",
        "_cap",
        "_clock",
        "_failures",
        "_limit",
        "_max_ips",
        "_recent",
        "_recent_bound",
        "_sleep",
        "_window",
    )

    def __init__(
        self,
        *,
        base_delay_s: float = _THROTTLE_BASE_DELAY_S,
        max_delay_s: float = _THROTTLE_MAX_DELAY_S,
        window_s: float = _THROTTLE_WINDOW_S,
        max_attempts: int = _THROTTLE_MAX_ATTEMPTS,
        max_tracked_ips: int = _THROTTLE_MAX_TRACKED_IPS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._base = base_delay_s
        self._cap = max_delay_s
        self._window = window_s
        self._limit = max_attempts
        self._max_ips = max_tracked_ips
        self._recent_bound = _THROTTLE_RECENT_BOUND
        self._clock = clock
        self._sleep = sleep
        self._failures: OrderedDict[str, int] = OrderedDict()
        self._recent: deque[float] = deque()

    def delay_for(self, ip: str) -> float:
        """Delay owed by ``ip``: base doubling per consecutive failure."""
        failures = self._failures.get(ip, 0)
        if failures <= 0:
            return 0.0
        return min(self._base * 2 ** (failures - 1), self._cap)

    def at_global_limit(self) -> bool:
        """Whether the rolling-minute ceiling of failed attempts is met."""
        cutoff = self._clock() - self._window
        recent = self._recent
        while recent and recent[0] <= cutoff:
            recent.popleft()
        return len(recent) >= self._limit

    def record_failure(self, ip: str) -> None:
        """Record one rejected login attempt for ``ip`` and the ceiling."""
        failures = self._failures
        if ip not in failures and len(failures) >= self._max_ips:
            failures.popitem(last=False)  # oldest-inserted key; bounded memory
        failures[ip] = failures.get(ip, 0) + 1
        failures.move_to_end(ip)
        if len(self._recent) < self._recent_bound:
            self._recent.append(self._clock())

    async def penalize(self, ip: str) -> None:
        """Sleep the delay owed by ``ip`` (identical for 401 and 429 paths)."""
        delay = self.delay_for(ip)
        if delay > 0.0:
            await self._sleep(delay)

    def reset(self, ip: str) -> None:
        """Successful login resets the per-IP counter (ADR §5.3)."""
        self._failures.pop(ip, None)


# ---------------------------------------------------------------------------
# C01 envelope rendering (single renderer for every /api/v1 error)
# ---------------------------------------------------------------------------


def render_error_envelope(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    fields: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Render the C01 structured error envelope for one request."""
    request_id = getattr(request.state, "request_id", "")
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "fields": dict(fields or {}),
            }
        },
    )


def clear_auth_cookies(response: Response) -> None:
    """Clear both auth cookies (expired/revoked ⇒ 401 + clearance, ADR §4)."""
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", samesite="lax")


def set_auth_cookies(
    response: Response,
    *,
    origin: str,
    session_token: str,
    csrf_token: str,
) -> None:
    """Mint ``mai_session`` (HttpOnly) + ``mai_csrf`` (double-submit).

    Host-only (no ``Domain``), ``Path=/``, ``SameSite=Lax``; ``Secure`` on
    every non-loopback origin (ADR §4–§5). The CSRF cookie is deliberately
    readable by first-party JS and grants nothing without the session.
    """
    max_age = int(SESSION_ABSOLUTE_TTL.total_seconds())
    secure = not _origin_is_loopback(origin)
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=max_age,
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=max_age,
        path="/",
        httponly=False,
        secure=secure,
        samesite="lax",
    )


def _new_csrf_token() -> str:
    return secrets.token_urlsafe(_CSRF_TOKEN_BYTES)


# ---------------------------------------------------------------------------
# Layered guards (ADR §5 order: session → origin → CSRF)
# ---------------------------------------------------------------------------


def _sessions(request: Request) -> SessionStore:
    """The lifespan-built store; without it nothing authenticates (G1)."""
    store = getattr(request.app.state, "sessions", None)
    if store is None:
        raise BoundaryError(
            HTTP_401_UNAUTHORIZED, "unauthenticated", "authentication is unavailable"
        )
    return store


async def _require_session(request: Request) -> tuple[SessionStore, str]:
    """Layer 1: the principal comes from the validated session only (C01).

    ``validate`` re-checks revocation/both expiries and slides the idle
    window in one transaction (§4); missing/expired/revoked collapse into
    one value-free 401 that also clears both cookies.
    """
    store = _sessions(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        raise BoundaryError(
            HTTP_401_UNAUTHORIZED, "unauthenticated", "authentication required"
        )
    try:
        await store.validate(token)
    except AuthError:
        raise BoundaryError(
            HTTP_401_UNAUTHORIZED,
            "unauthenticated",
            "authentication required",
            clear_auth_cookies=True,
        ) from None
    return store, token


def _require_allowed_origin(request: Request) -> str:
    """Layer 2: exact ``Origin`` allowlist match; ``Referer`` never counts."""
    origins: frozenset[str] = request.app.state.public_origins
    origin = request.headers.get("origin", "")
    if not origin or origin not in origins:
        raise BoundaryError(
            HTTP_403_FORBIDDEN, "origin_rejected", "request origin is not allowed"
        )
    return origin


def _require_csrf(request: Request) -> None:
    """Layer 3: double-submit ``X-CSRF-Token`` == ``mai_csrf`` cookie."""
    header = request.headers.get(CSRF_HEADER, "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not header or not cookie or not hmac.compare_digest(header, cookie):
        raise BoundaryError(
            HTTP_403_FORBIDDEN, "csrf_rejected", "CSRF token missing or mismatched"
        )


# ---------------------------------------------------------------------------
# Routes (frozen design targets, ADR §3–§5/§8–§9)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post(
    "/bootstrap",
    status_code=HTTP_201_CREATED,
    summary="One-shot administrator bootstrap (G1)",
    responses={
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not in the configured allowlist",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "The credential record already exists (permanent)",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
    },
)
async def bootstrap(request: Request, payload: PasswordBody) -> Response:
    """Create the administrator credential record exactly once (ADR §3, §8).

    Setup-state route: it runs before any session exists, so the frozen
    pre-auth layer applied here is the exact-origin check — a cross-site
    bootstrap would hand the attacker the credential. Afterwards the route
    answers ``409 conflict`` permanently.
    """
    _require_allowed_origin(request)
    store = _sessions(request)
    try:
        await store.bootstrap_credential(payload.password)
    except AuthError as exc:
        if exc.code is AuthErrorCode.CREDENTIAL_PRESENT:
            raise BoundaryError(
                HTTP_409_CONFLICT,
                "conflict",
                "administrator credential already exists",
            ) from None
        raise BoundaryError(500, "internal_error", "bootstrap failed") from None
    return Response(status_code=HTTP_201_CREATED)


@router.post(
    "/login",
    status_code=HTTP_204_NO_CONTENT,
    summary="Administrator login",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "Credential rejected (value-free)",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not in the configured allowlist",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
        HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorEnvelope,
            "description": "Global failed-attempt ceiling reached (§5.3)",
        },
    },
)
async def login(request: Request, payload: PasswordBody) -> Response:
    """Verify the credential and mint a fresh session (ADR §4, §5.3).

    The global ceiling is checked before the credential store so a flood
    never reaches Argon2id. Wrong-password and throttled responses share
    the same per-source-IP delay profile (§5.3 latency parity); both are
    recorded as failures, and only success resets the per-IP counter.
    Setup state collapses to the same value-free 401 (G1 fail-closed).
    """
    origin = _require_allowed_origin(request)
    store = _sessions(request)
    throttle: LoginThrottle = request.app.state.login_throttle
    client_ip: str = request.state.client_ip
    globally_throttled = throttle.at_global_limit()
    issued: IssuedSession | None = None
    if not globally_throttled:
        try:
            issued = await store.login(payload.password)
        except AuthError:
            issued = None  # setup state / corrupt record: value-free failure
    if issued is None:
        throttle.record_failure(client_ip)
        await throttle.penalize(client_ip)
        if globally_throttled:
            raise BoundaryError(
                HTTP_429_TOO_MANY_REQUESTS,
                "throttled",
                "too many failed login attempts",
            )
        raise BoundaryError(
            HTTP_401_UNAUTHORIZED, "unauthenticated", "authentication failed"
        )
    throttle.reset(client_ip)
    response = Response(status_code=HTTP_204_NO_CONTENT)
    set_auth_cookies(
        response,
        origin=origin,
        session_token=issued.token,
        csrf_token=_new_csrf_token(),
    )
    return response


@router.post(
    "/logout",
    status_code=HTTP_204_NO_CONTENT,
    summary="Revoke the presenting session",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowed or CSRF token rejected",
        },
    },
)
async def logout(request: Request) -> Response:
    """Revoke exactly the presenting session (ADR §4, S2/S3).

    All three §5 layers apply in order; a denied logout runs no revocation,
    and a successful one is effective on the next request (no grace
    window).
    """
    store, token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    await store.revoke(token)  # idempotent; audit row retained (§4)
    response = Response(status_code=HTTP_204_NO_CONTENT)
    clear_auth_cookies(response)
    return response


@router.get(
    "/csrf",
    status_code=HTTP_204_NO_CONTENT,
    summary="Re-mint the CSRF cookie for the authenticated session",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
    },
)
async def mint_csrf(request: Request) -> Response:
    """Authenticated CSRF re-mint (ADR §5).

    Safe method: no state change beyond issuing a fresh token, so layers
    2–3 do not apply; the token grants nothing without the session.
    """
    await _require_session(request)
    response = Response(status_code=HTTP_204_NO_CONTENT)
    response.set_cookie(
        CSRF_COOKIE,
        _new_csrf_token(),
        max_age=int(SESSION_ABSOLUTE_TTL.total_seconds()),
        path="/",
        httponly=False,
        secure=not _is_loopback_host(request.client.host if request.client else ""),
        samesite="lax",
    )
    return response


def register_auth(app: FastAPI) -> None:
    """Register the boundary surface and its C01 rendering (api-wiring)."""
    app.state.login_throttle = LoginThrottle()

    @app.exception_handler(BoundaryError)
    async def boundary_error_handler(
        request: Request, exc: BoundaryError
    ) -> JSONResponse:
        response = render_error_envelope(
            request, exc.status_code, exc.code, exc.message
        )
        if exc.clear_auth_cookies:
            clear_auth_cookies(response)
        return response

    app.include_router(router)
