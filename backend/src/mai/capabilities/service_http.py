"""Allowlist-scoped, credential-reference-bound HTTP calls to managed services.

Card ``07-service-http`` against contracts.md **C03 rules 1, 2, 6, 7** and
C06 rule 3. Where ``07-page-reader``'s ``web.read`` walks the open internet
under an egress address policy with no credential of its own, this
capability is the private counterpart: the callable surface is an explicit
host-registered **allowlist of managed services** (name → endpoint base
URL), every call is **bound to one declared credential reference** resolved
by the dispatcher at effect time (C03 rule 6 — never serialized into an
``ExecutionContext``, never echoed into output), and the answer is bounded
JSON for server-service investigation, not a page and not a generic
downloader.

The safety boundary this module owns:

- **Allowlist scope.** The payload names a service; only names the host
  registered at construction resolve to an endpoint. The payload carries no
  absolute URL and no host vocabulary of its own (C03 rule 2: the
  ``ExecutionContext`` and host wiring own the target vocabulary) — a
  granted ``service.http`` call cannot be retargeted at an arbitrary host
  by payload content.
- **Credential binding.** One payload field names the credential reference
  to bind; the reference must be one the run's ``ExecutionContext``
  declares (``credential_references``), the dispatcher resolves it, and the
  value leaves the process exactly once as one ``Authorization: Bearer``
  header on the allowlisted endpoint. The value is never placed in the
  result document; the dispatcher's redaction is defense-in-depth on top.
- **Forbidden redirect denial.** Redirects are followed manually and only
  within the exact origin of the approved endpoint; a redirect to any other
  scheme/host/port is refused *before* the second connection is opened, so
  the credential can never be sent to an unrelated redirect target (AC1).
- **Bounded JSON answer.** The response body streams under an explicit byte
  budget, must be ``application/json`` (or a ``+json`` suffix), and parses
  into the frozen result document — bounded tool output for small model
  contexts (C06 rule 3), with provenance (service, method, final endpoint,
  HTTP status, redirect count, honest truncation flags).

Effect class ``read``: an investigation request to a service API changes no
observable MAI or target state, so the policy layer requires no approval
decision in either prompting mode — while every grant/scope/policy gate
still applies unchanged, and a run needs the capability granted **and** a
credential reference declared to use it (AC2: unlike the public page
reader, private-service access requires a declared grant plus an explicit
credential binding). The approval seam is the dispatcher's, not this
module's: if policy ever reclassifies the capability, the same code path
gains the approval requirement without a change here.

The module reads no process environment and takes no clock. The network
seam is the injected provider alone (the httpx2 client stays inside the
production provider, exactly like ``07-page-reader``/``07-search-capability``),
so tests run the real client machinery over a deterministic transport or a
controlled loopback upstream without any egress.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from mai.capabilities.dispatch import CapabilityCall, ExecutorResult
from mai.plugins.registry import CapabilityDescriptor, parse_descriptor
from mai.policy.evaluate import ExecutionContext, PolicyMode

__all__ = [
    "CAPABILITY_ID",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_BODY_BYTES",
    "MAX_MAX_BYTES",
    "MAX_PATH_CHARS",
    "MAX_REDIRECTS",
    "MAX_SERVICE_NAME_CHARS",
    "MAX_TIMEOUT_SECONDS",
    "MIN_MAX_BYTES",
    "HttpServiceProvider",
    "ServiceError",
    "ServiceErrorCode",
    "ServiceHttpCapability",
    "ServiceRequest",
    "ServiceStatus",
    "descriptor",
    "endpoint_for",
    "origin_of",
    "request_from_payload",
    "result_document",
    "validate_service_path",
]

#: The capability slug this module owns (registry identifier shape; C03
#: rule 1). Grants name this id; the descriptor and the executor agree.
CAPABILITY_ID = "service.http"

#: The methods this capability speaks. Investigation is GET or POST; no
#: PUT/PATCH/DELETE vocabulary exists because the capability does not
#: mutate service state (effect class ``read`` — the vocabulary is
#: deliberately narrower than the HTTP surface).
_ALLOWED_METHODS = frozenset({"GET", "POST"})

#: Name bound: one allowlist entry is one managed service.
MAX_SERVICE_NAME_CHARS = 64

#: Path bound: the payload path (plus optional query) stays a reference,
#: not a data channel.
MAX_PATH_CHARS = 2000

#: Body bound: a POST body is a small service-API envelope (e.g. one
#: chat/completions request), not a bulk upload.
MAX_BODY_BYTES = 16 * 1024

#: Default and bounds for the response-body byte budget. The default
#: settles a typical service-API answer honestly; the cap bounds the
#: worst case a host may construct. The floor keeps the budget a real
#: answer, not a probe.
DEFAULT_MAX_BYTES = 256 * 1024
MIN_MAX_BYTES = 16 * 1024
MAX_MAX_BYTES = 1024 * 1024

#: Default and bounds for the whole-call timeout.
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_TIMEOUT_SECONDS = 60.0

#: Redirect hop bound: an allowlisted service may move a path a few
#: times; beyond this the chain is treated as evasive and refused.
MAX_REDIRECTS = 5

#: The descriptive, non-identifying outbound User-Agent (sanitized
#: outbound context): a library-default UA would advertise the HTTP
#: client and version to every service the capability contacts.
USER_AGENT = "mai-service-http/1.0 (internal service capability)"

#: The one outbound credential scheme: the resolved reference value
#: rides a single Authorization header, nothing else.
_AUTH_SCHEME = "Bearer"

#: The streaming-read chunk size: bounded read granularity, not a
#: payload knob.
_READ_CHUNK_BYTES = 8192

#: Redirect statuses followed as same-origin hops.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: JSON-family content types this capability consumes. Anything else is
#: ``unsupported_content_type`` — the capability reads service API
#: answers, it is not a generic file downloader (bounded scope).
def _is_json_content_type(content_type: str) -> bool:
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json" or (
        media.startswith("application/") and media.endswith("+json")
    )


class ServiceStatus(StrEnum):
    """The honest answer of one service call."""

    OK = "ok"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


class ServiceErrorCode(StrEnum):
    """Stable machine-readable refusal/outage codes."""

    INVALID_PAYLOAD = "invalid_payload"
    UNKNOWN_SERVICE = "unknown_service"
    CREDENTIAL_NOT_DECLARED = "credential_not_declared"
    FORBIDDEN_REDIRECT = "forbidden_redirect"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    RESPONSE_TOO_LARGE = "response_too_large"
    FETCH_TIMEOUT = "fetch_timeout"
    FETCH_UNREACHABLE = "fetch_unreachable"
    FETCH_ERROR = "fetch_error"
    HTTP_ERROR = "http_error"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    PROVIDER_ERROR = "provider_error"


class ServiceError(ValueError):
    """One structured, value-free payload/refusal rejection.

    Raised only for malformed *inputs* (payload shape, host wiring) and
    for capability-level refusals discovered mid-flight (unknown
    service, forbidden redirect, budget abort). Like the page reader,
    the ``at_url`` slot carries the refusing hop URL so a transcript
    stays auditable; it never carries credential material — URLs in this
    capability never contain userinfo by construction.
    """

    def __init__(
        self,
        code: ServiceErrorCode,
        detail: str,
        *,
        at_url: str | None = None,
    ) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail
        self.at_url = at_url


def _reject(
    code: ServiceErrorCode, detail: str, *, at_url: str | None = None
) -> ServiceError:
    return ServiceError(code, detail, at_url=at_url)


# ---------------------------------------------------------------------------
# Allowlist validation (host wiring; constructed once, fail-closed)
# ---------------------------------------------------------------------------


def validate_base_url(base: str) -> str:
    """Validate one managed-service endpoint base URL (no network).

    The base must be an absolute http(s) URL with a host, no userinfo
    (a service endpoint never embeds credential material), and no
    query or fragment (the payload path owns path+query). The returned
    form is normalized: scheme and host lowercased, no trailing slash
    (composition with a payload path adds exactly one separator).
    """
    if not isinstance(base, str) or not base.strip():
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            "service base URL must be a non-empty string",
        )
    try:
        parts = urlsplit(base.strip())
        # Accessing .port validates the port literal.
        port = parts.port
    except ValueError:
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            f"service base URL {base!r} is not parseable",
        ) from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            f"service base URL {base!r} must be absolute http(s)",
        )
    if parts.username is not None or parts.password is not None:
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            f"service base URL {base!r} must not embed userinfo "
            "(credentials resolve by reference, C03 rule 6)",
        )
    if parts.query or parts.fragment:
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            f"service base URL {base!r} must not carry a query or "
            "fragment (the payload path owns path+query)",
        )
    host = parts.hostname.lower()
    bracketed = f"[{host}]" if ":" in host else host
    netloc = bracketed if port is None else f"{bracketed}:{port}"
    path = parts.path or ""
    return urlunsplit((scheme, netloc, path.rstrip("/"), "", ""))


def validate_allowlist(services: Mapping[str, str]) -> dict[str, str]:
    """Validate the whole managed-services allowlist (fail-closed).

    Every name and every base URL is validated; on any violation the
    construction raises and the capability never half-exists. The
    allowlist is host wiring, not payload surface: it cannot be changed
    per call.
    """
    if not isinstance(services, Mapping) or not services:
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            "services allowlist must be a non-empty mapping "
            "of name -> endpoint base URL",
        )
    validated: dict[str, str] = {}
    for raw_name, raw_base in services.items():
        name = raw_name.strip().lower() if isinstance(raw_name, str) else ""
        if (
            not name
            or len(name) > MAX_SERVICE_NAME_CHARS
            or "\x00" in name
            or any(ch.isspace() for ch in name)
        ):
            raise _reject(
                ServiceErrorCode.INVALID_PAYLOAD,
                f"service name {raw_name!r} must be a non-empty slug of "
                f"at most {MAX_SERVICE_NAME_CHARS} characters without "
                "whitespace or NUL bytes",
            )
        if name in validated:
            raise _reject(
                ServiceErrorCode.INVALID_PAYLOAD,
                f"service name {name!r} is registered twice",
            )
        validated[name] = validate_base_url(raw_base)
    return validated


def endpoint_for(services: Mapping[str, str], service: str, path: str) -> str:
    """Compose the absolute endpoint for one allowlisted call.

    ``base + path`` with exactly one separator: the payload path is
    validated to start with ``/`` and the base carries no trailing
    slash, so concatenation is the whole composition (``urljoin`` would
    drop a base path prefix such as ``/v1`` for root-absolute paths).
    A name outside the allowlist is ``unknown_service`` before any
    connection.
    """
    base = services.get(service)
    if base is None:
        raise _reject(
            ServiceErrorCode.UNKNOWN_SERVICE,
            f"service {service!r} is not in the managed-services "
            "allowlist",
        )
    return f"{base}{path}"


def origin_of(url: str) -> str:
    """The ``scheme://host[:port]`` origin of one URL.

    Default ports are dropped so ``http://h`` and ``http://h:80`` name
    one origin — the redirect comparison is about the authority that
    would receive the credential, not the spelling.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is None or (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    ):
        netloc = f"[{host}]" if ":" in host else host
    else:
        bracketed = f"[{host}]" if ":" in host else host
        netloc = f"{bracketed}:{port}"
    return urlunsplit((scheme, netloc, "", "", ""))


# ---------------------------------------------------------------------------
# Payload validation (defense in depth; the bridge schema gate is primary)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServiceRequest:
    """One validated service-call request (capability payload shape)."""

    service: str
    method: str
    path: str
    body: str | None  # canonical JSON text, or None
    credential_ref: str
    max_bytes: int
    timeout_seconds: float


def validate_service_path(path: object) -> str:
    """Validate one payload path (no network): an absolute path+query
    reference within the allowlisted endpoint, never an absolute URL
    and never a credential carrier."""
    if not isinstance(path, str) or not path:
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            "path must be a non-empty string starting with '/'",
        )
    if (
        len(path) > MAX_PATH_CHARS
        or not path.startswith("/")
        or path.startswith("//")
        or "\x00" in path
        or "\r" in path
        or "\n" in path
        or "\\" in path
        or "#" in path
        or "://" in path
    ):
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            f"path must start with '/', be at most {MAX_PATH_CHARS} "
            "characters, and contain no NUL/CR/LF/backslash/fragment/"
            "scheme separators",
        )
    return path


def _canonical_body(body: object) -> str:
    """Canonical JSON text for one POST body, bounded by ``MAX_BODY_BYTES``."""
    try:
        text = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            f"body is not JSON-canonicalizable ({type(error).__name__})",
        ) from None
    if len(text.encode("utf-8")) > MAX_BODY_BYTES:
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            f"body exceeds the {MAX_BODY_BYTES}-byte envelope budget",
        )
    return text


def request_from_payload(
    payload: Mapping[str, object],
    *,
    default_max_bytes: int = DEFAULT_MAX_BYTES,
    default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ServiceRequest:
    """Validate one capability payload into a :class:`ServiceRequest`.

    Strict and fail-closed exactly like the sibling capabilities:
    unknown keys are rejected (``additionalProperties: false`` in the
    descriptor), so no smuggled field can reach the provider call. The
    payload carries no absolute endpoint: the service name selects an
    allowlist entry and the path stays a path (C03 rule 2).
    """
    if not isinstance(payload, Mapping):
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            "payload must be a JSON object (mapping)",
        )
    allowed = {
        "service",
        "method",
        "path",
        "body",
        "credential_ref",
        "max_bytes",
        "timeout_seconds",
    }
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            f"unknown payload fields: {', '.join(unknown)}",
        )

    service = payload.get("service")
    if (
        not isinstance(service, str)
        or not service.strip()
        or len(service) > MAX_SERVICE_NAME_CHARS
        or "\x00" in service
    ):
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            f"service must be a non-empty string of at most "
            f"{MAX_SERVICE_NAME_CHARS} characters without NUL bytes",
        )

    method = payload.get("method")
    if not isinstance(method, str) or method.upper() not in _ALLOWED_METHODS:
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            f"method must be one of {sorted(_ALLOWED_METHODS)}",
        )

    path = validate_service_path(payload.get("path"))

    has_body = "body" in payload
    body = _canonical_body(payload["body"]) if has_body else None
    if body is not None and method.upper() == "GET":
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            "body is only valid with method POST",
        )

    credential_ref = payload.get("credential_ref")
    if (
        not isinstance(credential_ref, str)
        or not credential_ref
        or "\x00" in credential_ref
        or any(ch.isspace() for ch in credential_ref)
    ):
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            "credential_ref must be a non-empty credential reference "
            "name without whitespace or NUL bytes",
        )

    max_bytes = payload.get("max_bytes", default_max_bytes)
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not MIN_MAX_BYTES <= max_bytes <= MAX_MAX_BYTES
    ):
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            f"max_bytes must be an integer between {MIN_MAX_BYTES} "
            f"and {MAX_MAX_BYTES}",
        )

    timeout = payload.get("timeout_seconds", default_timeout_seconds)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0.1 <= float(timeout) <= MAX_TIMEOUT_SECONDS
    ):
        raise _reject(
            ServiceErrorCode.INVALID_PAYLOAD,
            f"timeout_seconds must be a number between 0.1 and "
            f"{MAX_TIMEOUT_SECONDS}",
        )

    return ServiceRequest(
        service=service.strip(),
        method=method.upper(),
        path=path,
        body=body,
        credential_ref=credential_ref,
        max_bytes=max_bytes,
        timeout_seconds=float(timeout),
    )


# ---------------------------------------------------------------------------
# Provider seam (httpx2; transport stays a constructor/test seam)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawServiceResponse:
    """One fetched service answer at the provider boundary."""

    url: str  # final URL after any followed hops
    status_code: int
    content_type: str
    body: bytes
    redirects: int


class ServiceProvider(Protocol):
    """The injected network seam: one endpoint → one raw response.

    Implementations raise :class:`ServiceError` on capability-level
    refusals (redirect policy, budget abort) and let transport
    exceptions propagate for the capability to classify. Production is
    :class:`HttpServiceProvider`; tests inject deterministic transports
    or a controlled loopback upstream.
    """

    async def fetch(
        self,
        *,
        url: str,
        method: str,
        headers: Mapping[str, str],
        content: bytes | None,
        max_bytes: int,
        credential: str | None,
        allowed_origin: str,
    ) -> RawServiceResponse:
        """Perform one bounded call; enforce the redirect origin policy."""
        ...


def _classify_fetch_exception(exc: BaseException) -> ServiceErrorCode:
    """Map one provider exception to its stable seam code (value-free)."""
    import httpx2

    if isinstance(exc, httpx2.TimeoutException):
        return ServiceErrorCode.FETCH_TIMEOUT
    if isinstance(exc, httpx2.NetworkError):
        return ServiceErrorCode.FETCH_UNREACHABLE
    return ServiceErrorCode.FETCH_ERROR


class HttpServiceProvider:
    """The bounded service call over an httpx2 client.

    Redirects are followed manually and only within ``allowed_origin``:
    a hop whose resolved Location leaves the approved origin is refused
    *before* the second connection is opened — the credential rides
    only same-origin hops, so it can never reach an unrelated redirect
    target (AC1). The byte budget is a streaming cap abandoned at
    budget+1 decoded bytes (the transfer itself is bounded, like the
    page reader), with a Content-Length pre-check when declared.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = MAX_MAX_BYTES,
        max_redirects: int = MAX_REDIRECTS,
        transport: object | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise _reject(
                ServiceErrorCode.INVALID_PAYLOAD,
                f"timeout_seconds must be within (0, {MAX_TIMEOUT_SECONDS}]",
            )
        if not MIN_MAX_BYTES <= max_bytes <= MAX_MAX_BYTES:
            raise _reject(
                ServiceErrorCode.INVALID_PAYLOAD,
                f"max_bytes must be an integer between {MIN_MAX_BYTES} "
                f"and {MAX_MAX_BYTES}",
            )
        if not 0 < max_redirects <= MAX_REDIRECTS:
            raise _reject(
                ServiceErrorCode.INVALID_PAYLOAD,
                f"max_redirects must be an integer between 1 and "
                f"{MAX_REDIRECTS}",
            )
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._transport = transport

    def __repr__(self) -> str:
        return (
            f"<HttpServiceProvider timeout={self._timeout_seconds!r} "
            f"max_bytes={self._max_bytes!r}>"
        )

    async def fetch(
        self,
        *,
        url: str,
        method: str,
        headers: Mapping[str, str],
        content: bytes | None,
        max_bytes: int,
        credential: str | None,
        allowed_origin: str,
    ) -> RawServiceResponse:
        """Perform one bounded call with same-origin redirect policy."""
        import httpx2

        if not MIN_MAX_BYTES <= max_bytes <= self._max_bytes:
            raise _reject(
                ServiceErrorCode.INVALID_PAYLOAD,
                f"max_bytes must be an integer between {MIN_MAX_BYTES} "
                f"and the provider ceiling {self._max_bytes}",
            )
        current = url
        client = httpx2.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,  # type: ignore[arg-type]
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )
        try:
            hops = 0
            while True:
                if hops > self._max_redirects:
                    raise _reject(
                        ServiceErrorCode.TOO_MANY_REDIRECTS,
                        f"more than {self._max_redirects} redirect hops",
                        at_url=current,
                    )
                request_headers = dict(headers)
                if credential is not None:
                    request_headers["Authorization"] = (
                        f"{_AUTH_SCHEME} {credential}"
                    )
                # client.stream keeps the body unread until the budgeted
                # read below: the transfer is bounded by the early abort
                # at budget+1 bytes.
                async with client.stream(
                    method, current, headers=request_headers, content=content
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            # A malformed redirect response is the
                            # service's seam failing, not a capability
                            # policy decision: classify with the
                            # transport failures, value-free.
                            raise httpx2.HTTPError(
                                "redirect response carried no Location "
                                "header"
                            )
                        target = urljoin(current, location)
                        if origin_of(target) != allowed_origin:
                            raise _reject(
                                ServiceErrorCode.FORBIDDEN_REDIRECT,
                                "redirect target leaves the approved "
                                "endpoint origin; the credential is not "
                                "sent to an unrelated redirect target",
                                at_url=target,
                            )
                        current = target
                        hops += 1
                        continue
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        try:
                            if int(declared.strip()) > max_bytes:
                                raise _reject(
                                    ServiceErrorCode.RESPONSE_TOO_LARGE,
                                    f"declared Content-Length exceeds the "
                                    f"{max_bytes}-byte budget",
                                    at_url=current,
                                )
                        except ValueError:
                            pass  # non-numeric length: the streaming cap
                            # governs
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes(
                        _READ_CHUNK_BYTES
                    ):
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > max_bytes:
                            raise _reject(
                                ServiceErrorCode.RESPONSE_TOO_LARGE,
                                f"response exceeded the {max_bytes}-byte "
                                "budget",
                                at_url=current,
                            )
                    return RawServiceResponse(
                        url=current,
                        status_code=response.status_code,
                        content_type=content_type,
                        body=b"".join(chunks),
                        redirects=hops,
                    )
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Result types (frozen shape via result_document)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServiceResult:
    """One honest service-call answer, value-free on refusal/unavailable.

    ``status`` is ``ok`` only when the endpoint answered 2xx with
    parseable JSON. ``refused`` is the capability policy decision
    succeeding (no credential-bearing request reached the refusing
    target); ``unavailable`` names which seam failed via ``code`` and
    the exception *type* only. The endpoint keeps every outcome
    traceable to its allowlisted target (C07 rule 4 provenance) and
    never carries credential material — URLs in this capability are
    userinfo-free by construction.
    """

    status: ServiceStatus
    code: ServiceErrorCode | None
    service: str
    method: str
    endpoint: str
    http_status: int | None
    json_body: object | None
    truncated: bool
    redirects: int
    error_type: str | None = None


def result_document(result: ServiceResult) -> dict[str, object]:
    """The frozen JSON document of one service-call result (stable keys)."""
    return {
        "status": result.status.value,
        "code": result.code.value if result.code is not None else None,
        "trust": "untrusted_external",
        "service": result.service,
        "method": result.method,
        "endpoint": result.endpoint,
        "http_status": result.http_status,
        "json": result.json_body,
        "truncated": result.truncated,
        "redirects": result.redirects,
        "error_type": result.error_type,
    }


# ---------------------------------------------------------------------------
# Capability descriptor (C03 rule 1 frozen vocabulary)
# ---------------------------------------------------------------------------


def descriptor() -> CapabilityDescriptor:
    """The validated capability descriptor (C03 rule 1 vocabulary).

    ``effect_class="read"`` is the load-bearing field: a service
    investigation call changes no observable state, so the policy layer
    requires no approval decision in either prompting mode — while
    every grant/scope/policy gate still applies unchanged (AC2: the
    private-service boundary is the declared grant plus the declared
    credential reference, not a human approval). ``additionalProperties:
    false`` pins the payload surface: no field can name an absolute
    endpoint, a header, or a credential value — the target vocabulary
    is the host allowlist plus the execution context alone.
    """
    return parse_descriptor(
        {
            "identifier": CAPABILITY_ID,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["service", "method", "path", "credential_ref"],
                "properties": {
                    "service": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_SERVICE_NAME_CHARS,
                        "description": (
                            "Managed service name from the host-registered "
                            "allowlist; any other name refuses before any "
                            "connection (code unknown_service)."
                        ),
                    },
                    "method": {
                        "type": "string",
                        "enum": sorted(_ALLOWED_METHODS),
                        "description": (
                            "HTTP method the capability speaks; "
                            "investigation is GET or POST only."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PATH_CHARS,
                        "description": (
                            "Absolute path (with optional query) within "
                            "the allowlisted endpoint, e.g. /v1/models; "
                            "absolute URLs, fragments, CRLF and "
                            "backslashes are rejected."
                        ),
                    },
                    "body": {
                        "type": "object",
                        "description": (
                            "JSON request envelope for method=POST "
                            f"(canonical, at most {MAX_BODY_BYTES} bytes); "
                            "invalid with GET."
                        ),
                    },
                    "credential_ref": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "description": (
                            "Name of the credential reference to bind as "
                            "the single Authorization header; the "
                            "reference must be one the run's execution "
                            "context declares (else code "
                            "credential_not_declared) and resolves from "
                            "the configured .env source at effect time "
                            "(C03 rule 6). The value is never part of "
                            "the payload, the result, or any redirect "
                            "target outside the approved origin."
                        ),
                    },
                    "max_bytes": {
                        "type": "integer",
                        "minimum": MIN_MAX_BYTES,
                        "maximum": MAX_MAX_BYTES,
                        "default": DEFAULT_MAX_BYTES,
                        "description": (
                            "Response-body byte budget; a Content-Length "
                            "over budget refuses before reading, the "
                            "streaming read stops at budget+1 (code "
                            "response_too_large)."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "minimum": 0.1,
                        "maximum": MAX_TIMEOUT_SECONDS,
                        "default": DEFAULT_TIMEOUT_SECONDS,
                        "description": (
                            "Whole-call time budget; a timeout classifies "
                            "as fetch_timeout, value-free."
                        ),
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "required": [
                    "status",
                    "trust",
                    "service",
                    "method",
                    "endpoint",
                    "truncated",
                    "redirects",
                ],
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": sorted(item.value for item in ServiceStatus),
                    },
                    "code": {"type": ["string", "null"]},
                    "trust": {
                        "type": "string",
                        "enum": ["untrusted_external"],
                        "description": (
                            "Service JSON is external sourced data quoted "
                            "into context, never an instruction channel."
                        ),
                    },
                    "service": {"type": "string"},
                    "method": {"type": "string"},
                    "endpoint": {"type": "string"},
                    "http_status": {"type": ["integer", "null"]},
                    "json": {"type": ["object", "array", "null"]},
                    "truncated": {"type": "boolean"},
                    "redirects": {"type": "integer"},
                    "error_type": {"type": ["string", "null"]},
                },
            },
            "execution_environments": ["controller"],
            "effect_class": "read",
            "resource_scope": [],
            "required_grants": [CAPABILITY_ID],
        },
        source=f"<{CAPABILITY_ID} descriptor>",
    )


# ---------------------------------------------------------------------------
# The trusted capability implementation (dispatcher-gated)
# ---------------------------------------------------------------------------


class ServiceHttpCapability:
    """``service.http`` executor: one gated call → one honest JSON answer.

    Reachable only through the dispatcher (C03 rule 7); performs no
    policy decision of its own — grants are intersected upstream and the
    one deliberate disclosure point is this trusted code attaching the
    resolved reference value to the allowlisted endpoint. Order per
    call: validate payload → bind the declared credential reference
    (must be in the execution context, resolved material present) →
    compose the endpoint from the host allowlist → one bounded fetch
    with same-origin redirect policy → JSON answer in the frozen result
    document. Policy refusals and provider outages are honest statuses;
    only malformed direct calls raise.
    """

    def __init__(
        self,
        provider: ServiceProvider,
        *,
        services: Mapping[str, str],
        default_max_bytes: int = DEFAULT_MAX_BYTES,
        default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not hasattr(provider, "fetch"):
            raise _reject(
                ServiceErrorCode.INVALID_PAYLOAD,
                "provider must implement the ServiceProvider protocol "
                "(an async fetch(url=..., method=..., headers=..., "
                "content=..., max_bytes=..., credential=..., "
                "allowed_origin=...) -> RawServiceResponse)",
            )
        self._services = validate_allowlist(services)
        self._provider = provider
        self._default_max_bytes = default_max_bytes
        self._default_timeout = default_timeout_seconds

    def __repr__(self) -> str:
        services = ",".join(sorted(self._services))
        return (
            f"<ServiceHttpCapability services=[{services}] "
            f"provider={self._provider!r}>"
        )

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        """Execute one gated call (see class docstring for the order)."""
        if not isinstance(call, CapabilityCall):
            raise _reject(
                ServiceErrorCode.INVALID_PAYLOAD,
                "executor input must be a dispatcher CapabilityCall",
            )
        context = call.context
        if not isinstance(context, ExecutionContext):
            raise _reject(
                ServiceErrorCode.INVALID_PAYLOAD,
                "call context must be an ExecutionContext",
            )
        request = request_from_payload(
            call.payload,
            default_max_bytes=self._default_max_bytes,
            default_timeout_seconds=self._default_timeout,
        )
        try:
            endpoint = endpoint_for(
                self._services, request.service, request.path
            )
        except ServiceError as exc:
            # A payload-named service outside the host allowlist is a
            # scope refusal, not an outage: the honest answer carries no
            # connection attempt.
            result = ServiceResult(
                status=ServiceStatus.REFUSED,
                code=exc.code,
                service=request.service,
                method=request.method,
                endpoint=exc.at_url or request.path,
                http_status=None,
                json_body=None,
                truncated=False,
                redirects=0,
            )
            return ExecutorResult(
                ok=False,
                output=json.dumps(
                    result_document(result), sort_keys=True, ensure_ascii=True
                ),
            )
        # The payload may name only a reference the run itself declared;
        # the dispatcher resolved exactly those into ``call.credentials``.
        if request.credential_ref not in context.credential_references:
            result = ServiceResult(
                status=ServiceStatus.REFUSED,
                code=ServiceErrorCode.CREDENTIAL_NOT_DECLARED,
                service=request.service,
                method=request.method,
                endpoint=endpoint,
                http_status=None,
                json_body=None,
                truncated=False,
                redirects=0,
            )
            return ExecutorResult(
                ok=False,
                output=json.dumps(
                    result_document(result), sort_keys=True, ensure_ascii=True
                ),
            )
        secret = call.credentials.get(request.credential_ref)
        if secret is None:
            raise _reject(
                ServiceErrorCode.INVALID_PAYLOAD,
                f"credential reference {request.credential_ref!r} is "
                "declared in the context but unresolved in the call: "
                "direct calls must carry resolved material for every "
                "declared reference",
            )
        headers = {"Accept": "application/json"}
        content = request.body.encode("utf-8") if request.body else None
        if content is not None:
            headers["Content-Type"] = "application/json"
        try:
            raw = await self._provider.fetch(
                url=endpoint,
                method=request.method,
                headers=headers,
                content=content,
                max_bytes=request.max_bytes,
                credential=secret.value,
                allowed_origin=origin_of(endpoint),
            )
        except ServiceError as exc:
            if exc.code is ServiceErrorCode.INVALID_PAYLOAD:
                # A malformed provider/wiring input is a caller error,
                # not a policy outcome — raise exactly like payload
                # validation does.
                raise
            # Capability-level refusal (unknown hop origin, budget
            # abort): the refusal is the honest answer, value-free.
            result = ServiceResult(
                status=ServiceStatus.REFUSED,
                code=exc.code,
                service=request.service,
                method=request.method,
                endpoint=exc.at_url or endpoint,
                http_status=None,
                json_body=None,
                truncated=False,
                redirects=0,
            )
        except Exception as exc:  # noqa: BLE001 - classified, value-free
            result = ServiceResult(
                status=ServiceStatus.UNAVAILABLE,
                code=_classify_fetch_exception(exc),
                service=request.service,
                method=request.method,
                endpoint=endpoint,
                http_status=None,
                json_body=None,
                truncated=False,
                redirects=0,
                error_type=type(exc).__name__,
            )
        else:
            if not 200 <= raw.status_code < 300:
                result = ServiceResult(
                    status=ServiceStatus.UNAVAILABLE,
                    code=ServiceErrorCode.HTTP_ERROR,
                    service=request.service,
                    method=request.method,
                    endpoint=raw.url,
                    http_status=raw.status_code,
                    json_body=None,
                    truncated=False,
                    redirects=raw.redirects,
                )
            elif not _is_json_content_type(raw.content_type):
                result = ServiceResult(
                    status=ServiceStatus.UNAVAILABLE,
                    code=ServiceErrorCode.UNSUPPORTED_CONTENT_TYPE,
                    service=request.service,
                    method=request.method,
                    endpoint=raw.url,
                    http_status=raw.status_code,
                    json_body=None,
                    truncated=False,
                    redirects=raw.redirects,
                )
            else:
                try:
                    parsed = json.loads(raw.body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    parsed = None
                if parsed is None or not isinstance(parsed, (dict, list)):
                    result = ServiceResult(
                        status=ServiceStatus.UNAVAILABLE,
                        code=ServiceErrorCode.PROVIDER_ERROR,
                        service=request.service,
                        method=request.method,
                        endpoint=raw.url,
                        http_status=raw.status_code,
                        json_body=None,
                        truncated=False,
                        redirects=raw.redirects,
                        error_type="invalid_json_body",
                    )
                else:
                    result = ServiceResult(
                        status=ServiceStatus.OK,
                        code=None,
                        service=request.service,
                        method=request.method,
                        endpoint=raw.url,
                        http_status=raw.status_code,
                        json_body=parsed,
                        truncated=False,
                        redirects=raw.redirects,
                    )
        return ExecutorResult(
            ok=result.status is ServiceStatus.OK,
            output=json.dumps(
                result_document(result), sort_keys=True, ensure_ascii=True
            ),
        )


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))


async def _call_async(args: argparse.Namespace) -> int:
    from mai.credentials.env import EnvResolver

    try:
        services = dict(item.split("=", 1) for item in args.allow)
    except ValueError:
        _emit(
            {
                "kind": "error",
                "code": ServiceErrorCode.INVALID_PAYLOAD.value,
                "detail": "--allow entries must be NAME=BASE_URL",
            }
        )
        return 1
    if any(not name.strip() or not base.strip() for name, base in services.items()):
        _emit(
            {
                "kind": "error",
                "code": ServiceErrorCode.INVALID_PAYLOAD.value,
                "detail": "--allow entries must be NAME=BASE_URL",
            }
        )
        return 1
    provider = HttpServiceProvider(
        timeout_seconds=(
            DEFAULT_TIMEOUT_SECONDS
            if args.timeout_seconds is None
            else args.timeout_seconds
        ),
        max_bytes=args.max_bytes,
    )
    capability = ServiceHttpCapability(
        provider,
        services=services,
        default_max_bytes=args.max_bytes,
        default_timeout_seconds=(
            DEFAULT_TIMEOUT_SECONDS
            if args.timeout_seconds is None
            else args.timeout_seconds
        ),
    )
    payload: dict[str, object] = {
        "service": args.service,
        "method": args.method,
        "path": args.path,
        "credential_ref": args.credential_ref,
        "max_bytes": args.max_bytes,
    }
    if args.body is not None:
        try:
            payload["body"] = json.loads(args.body)
        except json.JSONDecodeError:
            _emit(
                {
                    "kind": "error",
                    "code": ServiceErrorCode.INVALID_PAYLOAD.value,
                    "detail": "--body must be a JSON document",
                }
            )
            return 1
    resolver = EnvResolver(args.env_file)
    try:
        resolved = resolver.resolve(args.credential_ref)
    except Exception as exc:  # noqa: BLE001 - structured, value-free
        _emit(
            {
                "kind": "error",
                "code": "credential_resolution_failed",
                "detail": type(exc).__name__,
            }
        )
        return 1
    call = CapabilityCall(
        descriptor=descriptor(),
        payload=payload,
        context=ExecutionContext(
            principal="cli-operator",
            server_id=uuid.UUID(int=0),
            run_id=uuid.uuid4(),
            parent_scope=None,
            profile_revision="cli",
            prompt_revision="cli",
            credential_references=(args.credential_ref,),
            allowed_capabilities=(CAPABILITY_ID,),
            mode=PolicyMode.DELEGATED,
        ),
        credentials={args.credential_ref: resolved},
        op_id=uuid.uuid4(),
    )
    try:
        result = await capability(call)
    except ServiceError as exc:
        _emit({"kind": "error", "code": exc.code.value, "detail": str(exc)})
        return 1
    _emit(json.loads(result.output))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mai.capabilities.service_http",
        description="Allowlist-scoped, credential-bound HTTP calls to "
        "managed services (operator entrypoint; the product path is the "
        "policy-bound capability dispatch).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    call = sub.add_parser("call", help="call one managed service endpoint")
    call.add_argument(
        "--allow",
        action="append",
        required=True,
        metavar="NAME=BASE_URL",
        help="managed service allowlist entry (repeatable); the only "
        "endpoints this invocation may reach",
    )
    call.add_argument("--service", required=True, help="allowlisted name")
    call.add_argument(
        "--method", required=True, choices=sorted(_ALLOWED_METHODS)
    )
    call.add_argument(
        "--path", required=True, help="absolute path within the endpoint"
    )
    call.add_argument(
        "--credential-ref", required=True, help="credential reference name"
    )
    call.add_argument(
        "--env-file",
        required=True,
        help="the configured .env credential source (C03 rule 6)",
    )
    call.add_argument("--body", default=None, help="JSON envelope for POST")
    call.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    call.add_argument("--timeout-seconds", type=float, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: exit 0 call ran (JSON status distinguishes
    ok / refused / unavailable) / 1 structured rejection / 2 usage."""
    args = _build_parser().parse_args(argv)
    if args.command == "call":
        return asyncio.run(_call_async(args))
    return 2  # pragma: no cover - argparse `required=True` guards this


if __name__ == "__main__":  # pragma: no cover - argparse glue
    sys.exit(main())
