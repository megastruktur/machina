"""Sanitized web search with structured provenance over bundled SearXNG.

Card ``07-search-capability`` (service ``research``), against contracts.md
**C07** rule 7 (the bundled ``searxng`` + ``valkey`` pair placement; the
JSON format boundary) and rule 4 (provenance-preserving results), consuming
the reviewed prerequisites ``07-searxng`` (the internal-only, JSON-enabled,
limiter-off SearXNG service definition) and ``03-dispatcher`` /
``06-tool-bridge`` (the only invocation paths — this executor is reachable
exclusively as a trusted capability implementation behind
:class:`~mai.capabilities.dispatch.CapabilityDispatcher`).

- **Bounded queries and results (acceptance 1)** — the query text is
  character-capped, the result count is caller-bounded with a hard maximum,
  and every title/snippet is individually budgeted. The answer is a bounded
  list of cited hits, never a raw upstream document (SearXNG answers carry
  far more fields than a model context needs).
- **Sanitized outbound context (acceptance 1)** — the query passes a
  secret-marker screen *before any network I/O*: credential assignments
  (``api_key=…``, ``password: …``), bearer/JWT shapes, PEM private-key
  blocks and cloud access-key ids refuse the call outright
  (``sensitive_query``), because a redacted-but-sent query silently changes
  what was asked. The outbound request itself is minimal — ``q`` +
  ``format=json`` only, a descriptive non-identifying User-Agent, no
  client/request headers forwarded, no credentials attached: nothing that
  names the host or the product beyond an internal capability label leaves
  with the query (C07 rule 7: egress to public engines is deliberate, but
  MAI adds no host identity to it). Free-text PII scrubbing is out of scope
  by design — the screen refuses *secret-shaped* markers; it does not
  attempt NLP on natural language (documented limitation).
- **Timeout/provider error classified (acceptance 1)** — a timeout is
  ``provider_timeout``, a transport/connect failure is
  ``provider_unreachable``, an HTTP error status or a malformed upstream
  document is ``provider_error``; each ``status="unavailable"`` outcome
  names only the seam code and the exception *type*. A query that ran and
  returned zero organic results is ``status="ok"`` with an empty list; an
  unavailable provider is never laundered into an empty success.
- **Normalized provenance citations (C07 rule 4)** — every hit carries its
  source URL as the citation, normalized (scheme/host lowercased, default
  port and fragment dropped, known tracking parameters removed), plus the
  upstream engine list that produced it, so a snippet is traceable to both
  a fetchable source and the metasearch provenance chain.
- **Untrusted sourced data (acceptance 2)** — snippets are external content
  quoted into a model context: the frozen result document labels the result
  list ``"trust": "untrusted_external"`` at the top level and carries no
  field a consumer could mistake for an instruction channel. The capability
  performs quoting/bounding only; interpreting snippet text is the caller's
  (and the model layer's) job.
- **Descriptor availability to scoped subagents (acceptance 2)** —
  ``effect_class="read"`` (a metasearch GET changes no observable state, so
  the policy layer requires no approval decision in either prompting mode —
  while every grant/scope/policy gate still applies ahead of this
  executor), ``execution_environments=["controller"]`` (the
  ``mai-worker``-side implementation of the bundled search tooling, per the
  registry's frozen vocabulary), ``resource_scope=[]`` (no lockable
  resource), ``required_grants=["web.search"]``. Availability to a subagent
  is exactly the ``06-tool-bridge`` rule: a capability is exposed only when
  it is both registered and in the run's ``allowed_capabilities``, and its
  per-server grant scope is intersected, never unioned (C03 rule 3).
- **Value-free discipline** — rejections and outages carry stable codes and
  field/class names; upstream exception *messages* (which may quote the
  query or response data) never surface. Snippet/title content is
  legitimate search output; errors contain none.

Effect class ``read``; the module reads no process environment and takes no
clock. The network seam is the injected provider alone (the httpx2 transport
stays a constructor/test seam exactly like ``07-chunk-index``'s
:class:`~mai.knowledge.index.GatewayEmbedder`); the base URL is host wiring,
never payload or environment inside this module.

Production entrypoint (exit contract: 0 query ran — the JSON status
distinguishes ``ok`` / ``unavailable`` / zero results; 1 structured
rejection; 2 usage error)::

    uv run python -m mai.research.search query --base-url URL --query TEXT \
        [--limit N] [--max-snippet-chars N] [--timeout-seconds N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from mai.capabilities.dispatch import CapabilityCall, ExecutorResult
from mai.plugins.registry import CapabilityDescriptor, parse_descriptor
from mai.policy.evaluate import ExecutionContext, PolicyMode

__all__ = [
    "CAPABILITY_ID",
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_SNIPPET_CHARS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_LIMIT",
    "MAX_QUERY_CHARS",
    "MAX_SNIPPET_CHARS",
    "MIN_SNIPPET_CHARS",
    "MAX_TIMEOUT_SECONDS",
    "SearchError",
    "SearchErrorCode",
    "SearchHit",
    "SearchProvider",
    "SearchRequest",
    "SearchResult",
    "SearchStatus",
    "SearXNGProvider",
    "WebSearchCapability",
    "descriptor",
    "normalize_citation_url",
    "parse_provider_document",
    "request_from_payload",
    "result_document",
    "sensitive_marker_reason",
]

#: The capability slug this module owns (registry identifier shape; C03
#: rule 1). Grants name this id; the descriptor and the executor agree.
CAPABILITY_ID = "web.search"

#: Query text bound: a metasearch query is a short phrase, and the cap
#: keeps the outbound request bounded work for the upstream instance.
MAX_QUERY_CHARS = 500

#: Default and bounds for the hit count — bounded cited hits, never the
#: raw upstream result list (acceptance 1).
DEFAULT_LIMIT = 8
MAX_LIMIT = 10

#: Default and bounds for one hit's snippet budget. The floor keeps a
#: snippet a usable evidence unit; the ceiling keeps one hit small for
#: model contexts (C06 rule 3).
DEFAULT_MAX_SNIPPET_CHARS = 400
MIN_SNIPPET_CHARS = 100
MAX_SNIPPET_CHARS = 1000

#: Title display-label bound; a title is metadata, not evidence text, so a
#: hard slice is bounded output, not a silent evidence cut (snippets carry
#: the ``truncated`` flag instead).
MAX_TITLE_CHARS = 300

#: Engine-provenance bounds: per hit and for the outage list, each entry
#: one bounded slug.
MAX_ENGINES_PER_HIT = 8
MAX_ENGINE_SLUG_CHARS = 64
MAX_UNRESPONSIVE_ENGINES = 32

#: Provider request budget: the default settles a metasearch fan-out
#: honestly; the cap bounds the worst case a host may construct.
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TIMEOUT_SECONDS = 60.0

#: The descriptive, non-identifying outbound User-Agent (sanitized
#: outbound context): a library-default UA would advertise the HTTP client
#: and version to every public engine the query fans out to.
USER_AGENT = "mai-web-search/1.0 (internal research capability)"

#: The provider provenance label of this capability's production backend
#: (the bundled SearXNG instance; C07 rule 7). It names the channel, never
#: an instance address.
PROVIDER_LABEL = "searxng"


class SearchStatus(StrEnum):
    """The honest answer of one search attempt."""

    OK = "ok"
    UNAVAILABLE = "unavailable"


class SearchErrorCode(StrEnum):
    """Stable machine-readable search rejection/outage codes."""

    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNREACHABLE = "provider_unreachable"
    PROVIDER_ERROR = "provider_error"
    SENSITIVE_QUERY = "sensitive_query"
    INVALID_PAYLOAD = "invalid_payload"


class SearchError(ValueError):
    """One structured, value-free payload rejection (defense in depth).

    Raised only for malformed *direct* executor calls and for
    secret-shaped queries; the ``06-tool-bridge`` schema gate denies
    malformed shapes before dispatch, so the dispatcher's normal path
    never reaches the payload rejects. A sensitive query raises before
    any provider I/O — nothing leaves.
    """

    def __init__(self, code: SearchErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def _reject(code: SearchErrorCode, detail: str) -> SearchError:
    return SearchError(code, detail)


# ---------------------------------------------------------------------------
# Outbound query screen (sanitized outbound context; runs before any I/O)
# ---------------------------------------------------------------------------

#: ``key = value`` / ``key: value`` assignments whose key names a
#: credential. The value half must be present, so ordinary prose
#: ("rotate the password", "the token expired") is not a false positive.
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"\b(?:api[_-]?key|access[_-]?key|secret[_-]?key|secret|token|auth[_-]?token|"
    r"password|passwd|pwd|credential|credentials|client[_-]?secret|"
    r"session[_-]?id|private[_-]?key)\b\s*[:=]\s*\S+",
    re.IGNORECASE,
)

#: PEM private-key block opener.
_PRIVATE_KEY_BLOCK_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

#: JWT compact shape (three base64url segments).
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")

#: Cloud access-key id shapes (AWS-style prefixes + 16 uppercase alnum).
_CLOUD_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")

#: ``Authorization: Bearer …`` / bare ``Bearer <token>`` shapes.
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE)

#: Human-readable, value-free class names for the screen's match classes
#: (the matched *text* is never reported — it is the secret).
_SCREEN_CLASSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("credential_assignment", _CREDENTIAL_ASSIGNMENT_RE),
    ("private_key_block", _PRIVATE_KEY_BLOCK_RE),
    ("jwt", _JWT_RE),
    ("cloud_access_key", _CLOUD_KEY_RE),
    ("bearer_token", _BEARER_RE),
)


def sensitive_marker_reason(query: str) -> str | None:
    """The value-free screen class if ``query`` carries a secret marker.

    Returns one of the stable class names (``credential_assignment``,
    ``private_key_block``, ``jwt``, ``cloud_access_key``,
    ``bearer_token``) or ``None``. The matched substring is deliberately
    never returned, logged, or included in any error — it is the secret.
    """
    for name, pattern in _SCREEN_CLASSES:
        if pattern.search(query):
            return name
    return None


# ---------------------------------------------------------------------------
# Citation URL normalization (provenance citations stay fetchable+comparable)
# ---------------------------------------------------------------------------

#: Query-parameter names dropped as pure click-tracking (never content)
#: when normalizing a citation URL. Unknown parameters are preserved.
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
    }
)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_citation_url(url: object) -> str | None:
    """Normalize one hit URL into a comparable provenance citation.

    Scheme and host are lowercased, an explicit default port is dropped,
    the fragment is dropped (it never survives a fresh fetch), and known
    click-tracking parameters are removed; every other component and
    parameter order is preserved. Returns ``None`` when the URL is not an
    absolute http(s) URL — such an item cannot be cited and is dropped
    from the result list rather than forwarded without provenance.
    Normalization is fail-soft by construction: an unusual-but-parseable
    URL keeps its content, it is never discarded by the normalizer.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        return None
    netloc = host if port is None or port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"
    kept = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name.lower() not in _TRACKING_PARAMS and not name.lower().startswith("utm_")
    ]
    return urlunsplit((scheme, netloc, parts.path, urlencode(kept), ""))


# ---------------------------------------------------------------------------
# Payload validation (defense in depth; the bridge schema gate is primary)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """One validated search request (capability payload shape)."""

    query: str
    limit: int
    max_snippet_chars: int


def request_from_payload(
    payload: Mapping[str, object],
    *,
    default_limit: int = DEFAULT_LIMIT,
    default_max_snippet_chars: int = DEFAULT_MAX_SNIPPET_CHARS,
) -> SearchRequest:
    """Validate one capability payload into a :class:`SearchRequest`.

    Strict and fail-closed exactly like the producer capabilities: unknown
    keys are rejected (``additionalProperties: false`` in the descriptor),
    so no smuggled field can reach the provider call. The payload carries
    no target or endpoint field (C03 rule 2): the provider endpoint is
    host wiring and the target vocabulary is the execution context alone.
    """
    if not isinstance(payload, Mapping):
        raise _reject(
            SearchErrorCode.INVALID_PAYLOAD,
            "payload must be a JSON object (mapping)",
        )
    allowed = {"query", "limit", "max_snippet_chars"}
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise _reject(
            SearchErrorCode.INVALID_PAYLOAD,
            f"unknown payload fields: {', '.join(unknown)}",
        )

    query = payload.get("query")
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > MAX_QUERY_CHARS
        or "\x00" in query
    ):
        raise _reject(
            SearchErrorCode.INVALID_PAYLOAD,
            f"query must be a non-empty string of at most "
            f"{MAX_QUERY_CHARS} characters without NUL bytes",
        )

    limit = payload.get("limit", default_limit)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise _reject(
            SearchErrorCode.INVALID_PAYLOAD,
            f"limit must be an integer between 1 and {MAX_LIMIT}",
        )

    max_snippet = payload.get("max_snippet_chars", default_max_snippet_chars)
    if (
        isinstance(max_snippet, bool)
        or not isinstance(max_snippet, int)
        or not MIN_SNIPPET_CHARS <= max_snippet <= MAX_SNIPPET_CHARS
    ):
        raise _reject(
            SearchErrorCode.INVALID_PAYLOAD,
            f"max_snippet_chars must be an integer between "
            f"{MIN_SNIPPET_CHARS} and {MAX_SNIPPET_CHARS}",
        )

    return SearchRequest(
        query=query.strip(), limit=limit, max_snippet_chars=max_snippet
    )


# ---------------------------------------------------------------------------
# Result types (frozen shape via result_document)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One bounded, cited search hit.

    ``url`` is the normalized provenance citation (a fetchable source);
    ``engines`` is the upstream metasearch provenance (which engines
    produced the hit). The snippet is external, untrusted sourced data —
    bounded, flagged when cut, never instruction-shaped by this module.
    """

    rank: int
    title: str
    url: str
    snippet: str
    truncated: bool
    engines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One honest search answer, value-free on failure.

    ``status`` is ``ok`` only when the query actually ran upstream (zero
    organic results included); ``unavailable`` names which seam failed via
    ``code`` and the exception *type* only. The sanitized query echo keeps
    a transcript self-auditing (the value that left is the value recorded).
    """

    status: SearchStatus
    code: SearchErrorCode | None
    query: str
    results: tuple[SearchHit, ...]
    unresponsive_engines: tuple[str, ...]
    error_type: str | None = None


class SearchProvider(Protocol):
    """The injected network seam: one query → one raw provider document.

    Implementations raise on transport/HTTP failure; the capability
    classifies. Production is :class:`SearXNGProvider`; tests inject
    deterministic fakes or an httpx2 ``MockTransport``.
    """

    async def search(self, *, query: str) -> Mapping[str, object]:
        """Return the decoded JSON document for one bounded query."""
        ...


# ---------------------------------------------------------------------------
# Result-document parsing (pure; strict shape, bounded fields)
# ---------------------------------------------------------------------------


def _bounded_engine_list(value: object) -> tuple[str, ...]:
    """The bounded engine-slug tuple out of one raw ``engines`` field."""
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item and len(item) <= MAX_ENGINE_SLUG_CHARS:
            out.append(item)
        if len(out) >= MAX_ENGINES_PER_HIT:
            break
    return tuple(out)


def parse_provider_document(
    document: Mapping[str, object],
    *,
    query: str,
    limit: int,
    max_snippet_chars: int,
) -> SearchResult:
    """Turn one raw provider JSON document into a bounded :class:`SearchResult`.

    Fail-closed on shape: the document must be a JSON object with a
    ``results`` list; anything else is a ``provider_error`` (the upstream
    answered, but not in the contract this capability consumes). Items
    that are not objects, or carry no citable http(s) URL, are skipped —
    a hit without provenance is never shown. Bounded at every field.
    """
    if not isinstance(document, Mapping) or not isinstance(
        document.get("results"), list
    ):
        return SearchResult(
            status=SearchStatus.UNAVAILABLE,
            code=SearchErrorCode.PROVIDER_ERROR,
            query=query,
            results=(),
            unresponsive_engines=(),
            error_type="invalid_provider_document",
        )

    hits: list[SearchHit] = []
    for item in document["results"]:
        if len(hits) >= limit:
            break
        if not isinstance(item, Mapping):
            return SearchResult(
                status=SearchStatus.UNAVAILABLE,
                code=SearchErrorCode.PROVIDER_ERROR,
                query=query,
                results=(),
                unresponsive_engines=(),
                error_type="invalid_provider_result_item",
            )
        citation = normalize_citation_url(item.get("url"))
        if citation is None:
            continue
        raw_title = item.get("title")
        title = (
            raw_title[:MAX_TITLE_CHARS]
            if isinstance(raw_title, str) and raw_title
            else ""
        )
        raw_snippet = item.get("content")
        snippet = raw_snippet if isinstance(raw_snippet, str) else ""
        truncated = len(snippet) > max_snippet_chars
        if truncated:
            snippet = snippet[:max_snippet_chars]
        hits.append(
            SearchHit(
                rank=len(hits) + 1,
                title=title,
                url=citation,
                snippet=snippet,
                truncated=truncated,
                engines=_bounded_engine_list(item.get("engines")),
            )
        )

    unresponsive: list[str] = []
    raw_unresponsive = document.get("unresponsive_engines")
    if isinstance(raw_unresponsive, list):
        for name in raw_unresponsive:
            if isinstance(name, str) and name:
                unresponsive.append(name[:MAX_ENGINE_SLUG_CHARS])
            if len(unresponsive) >= MAX_UNRESPONSIVE_ENGINES:
                break

    return SearchResult(
        status=SearchStatus.OK,
        code=None,
        query=query,
        results=tuple(hits),
        unresponsive_engines=tuple(unresponsive),
    )


def result_document(result: SearchResult) -> dict[str, object]:
    """The frozen JSON document of one search result (stable key order)."""
    return {
        "status": result.status.value,
        "code": result.code.value if result.code is not None else None,
        "provider": PROVIDER_LABEL,
        "trust": "untrusted_external",
        "query": result.query,
        "results": [
            {
                "rank": hit.rank,
                "title": hit.title,
                "url": hit.url,
                "citation": hit.url,
                "snippet": hit.snippet,
                "truncated": hit.truncated,
                "engines": list(hit.engines),
            }
            for hit in result.results
        ],
        "unresponsive_engines": list(result.unresponsive_engines),
        "error_type": result.error_type,
    }


# ---------------------------------------------------------------------------
# Provider seam (httpx2; the transport stays a constructor/test seam)
# ---------------------------------------------------------------------------


def _classify_provider_exception(exc: BaseException) -> SearchErrorCode:
    """Map one provider exception to its stable seam code (value-free)."""
    import httpx2

    if isinstance(exc, httpx2.TimeoutException):
        return SearchErrorCode.PROVIDER_TIMEOUT
    if isinstance(exc, httpx2.NetworkError):
        return SearchErrorCode.PROVIDER_UNREACHABLE
    return SearchErrorCode.PROVIDER_ERROR


class SearXNGProvider:
    """The bundled-SearXNG JSON API over an httpx2 client.

    Sends exactly ``q`` + ``format=json`` with the descriptive internal
    User-Agent — no caller headers, no credentials, no host identity
    beyond the capability label (sanitized outbound context). The base
    URL is constructor wiring (in production the compose-internal
    ``http://searxng:8080``; C07 rule 7), never read from the process
    environment inside this module.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: object | None = None,
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise _reject(
                SearchErrorCode.INVALID_PAYLOAD,
                "base_url must be an absolute http(s) URL",
            )
        if not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise _reject(
                SearchErrorCode.INVALID_PAYLOAD,
                f"timeout_seconds must be within (0, {MAX_TIMEOUT_SECONDS}]",
            )
        self._base_url = normalized
        self._timeout_seconds = timeout_seconds
        # Test seam: an explicit httpx2 transport (e.g. MockTransport)
        # keeps the provider boundary provable offline; production passes
        # None and the client dials the configured endpoint (same
        # discipline as 07-chunk-index's GatewayEmbedder).
        self._transport = transport

    def __repr__(self) -> str:
        return f"<SearXNGProvider base_url={self._base_url!r}>"

    async def search(self, *, query: str) -> Mapping[str, object]:
        import httpx2

        client = httpx2.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,  # type: ignore[arg-type]
            headers={"User-Agent": USER_AGENT},
        )
        try:
            response = await client.get(
                f"{self._base_url}/search",
                params={"q": query, "format": "json"},
            )
            response.raise_for_status()
            document = response.json()
        finally:
            await client.aclose()
        if not isinstance(document, dict):
            raise _reject(
                SearchErrorCode.PROVIDER_ERROR,
                "provider returned a JSON value that is not an object",
            )
        return document


# ---------------------------------------------------------------------------
# Capability descriptor (C03 rule 1 frozen vocabulary)
# ---------------------------------------------------------------------------


def descriptor() -> CapabilityDescriptor:
    """The validated capability descriptor (C03 rule 1 vocabulary).

    ``effect_class="read"`` is the load-bearing field: a metasearch GET
    changes no observable state, so the policy layer requires no approval
    decision in either prompting mode — while every grant/scope/policy
    gate still applies unchanged. ``additionalProperties: false`` pins the
    payload surface: no field can name an endpoint, a target or an engine
    (the endpoint is host wiring; the target vocabulary is the execution
    context alone).
    """
    return parse_descriptor(
        {
            "identifier": CAPABILITY_ID,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_QUERY_CHARS,
                        "description": (
                            "Search query text; screened for secret-shaped "
                            "markers and refused before any outbound request "
                            "(the marker class is named, never the value)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LIMIT,
                        "default": DEFAULT_LIMIT,
                        "description": (
                            "Bounded hit count; the upstream result list is "
                            "never returned whole."
                        ),
                    },
                    "max_snippet_chars": {
                        "type": "integer",
                        "minimum": MIN_SNIPPET_CHARS,
                        "maximum": MAX_SNIPPET_CHARS,
                        "default": DEFAULT_MAX_SNIPPET_CHARS,
                        "description": (
                            "Per-snippet character budget; a cut snippet "
                            "keeps its provenance citation and is flagged "
                            "truncated."
                        ),
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "required": ["status", "provider", "trust", "query", "results"],
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": sorted(item.value for item in SearchStatus),
                    },
                    "code": {"type": ["string", "null"]},
                    "provider": {"type": "string", "enum": [PROVIDER_LABEL]},
                    "trust": {
                        "type": "string",
                        "enum": ["untrusted_external"],
                        "description": (
                            "Every entry in results is external sourced "
                            "data quoted into context, never an instruction "
                            "channel."
                        ),
                    },
                    "query": {"type": "string"},
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": [
                                "rank",
                                "title",
                                "url",
                                "citation",
                                "snippet",
                                "truncated",
                                "engines",
                            ],
                            "properties": {
                                "rank": {"type": "integer"},
                                "title": {"type": "string"},
                                "url": {"type": "string"},
                                "citation": {"type": "string"},
                                "snippet": {"type": "string"},
                                "truncated": {"type": "boolean"},
                                "engines": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    },
                    "unresponsive_engines": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
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


class WebSearchCapability:
    """``web.search`` executor: one gated call → one honest result.

    Reachable only through the dispatcher (C03 rule 7); performs no policy
    decision of its own — the :class:`ExecutionContext` supplies the only
    target vocabulary (C03 rule 2) and grants are intersected upstream of
    this executor. Order per call: validate payload → screen the query
    (no I/O on a sensitive marker) → one provider call → parse/bound →
    one frozen JSON document.
    """

    def __init__(
        self,
        provider: SearchProvider,
        *,
        default_limit: int = DEFAULT_LIMIT,
        default_max_snippet_chars: int = DEFAULT_MAX_SNIPPET_CHARS,
    ) -> None:
        if not hasattr(provider, "search"):
            raise _reject(
                SearchErrorCode.INVALID_PAYLOAD,
                "provider must implement the SearchProvider protocol",
            )
        self._provider = provider
        self._default_limit = default_limit
        self._default_max_snippet = default_max_snippet_chars

    def __repr__(self) -> str:
        return f"<WebSearchCapability provider={self._provider!r}>"

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        """Execute one gated call (see module docstring for the order)."""
        if not isinstance(call, CapabilityCall):
            raise _reject(
                SearchErrorCode.INVALID_PAYLOAD,
                "executor input must be a dispatcher CapabilityCall",
            )
        context = call.context
        if not isinstance(context, ExecutionContext):
            raise _reject(
                SearchErrorCode.INVALID_PAYLOAD,
                "call context must be an ExecutionContext",
            )
        request = request_from_payload(
            call.payload,
            default_limit=self._default_limit,
            default_max_snippet_chars=self._default_max_snippet,
        )
        marker = sensitive_marker_reason(request.query)
        if marker is not None:
            # Fail closed before any network I/O: the query is never
            # sent, redacted or otherwise; only the class name is named.
            raise _reject(
                SearchErrorCode.SENSITIVE_QUERY,
                f"query carries a secret-shaped marker ({marker}); "
                f"refusing to send it to external search engines",
            )
        try:
            document = await self._provider.search(query=request.query)
        except SearchError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified, value-free
            result = SearchResult(
                status=SearchStatus.UNAVAILABLE,
                code=_classify_provider_exception(exc),
                query=request.query,
                results=(),
                unresponsive_engines=(),
                error_type=type(exc).__name__,
            )
        else:
            result = parse_provider_document(
                document,
                query=request.query,
                limit=request.limit,
                max_snippet_chars=request.max_snippet_chars,
            )
        return ExecutorResult(
            ok=result.status is SearchStatus.OK,
            output=json.dumps(result_document(result), sort_keys=True, ensure_ascii=True),
        )


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))


async def _query_async(args: argparse.Namespace) -> int:
    try:
        provider = SearXNGProvider(
            args.base_url,
            timeout_seconds=(
                DEFAULT_TIMEOUT_SECONDS
                if args.timeout_seconds is None
                else args.timeout_seconds
            ),
        )
    except SearchError as exc:
        _emit({"kind": "error", "code": exc.code.value, "detail": str(exc)})
        return 1
    capability = WebSearchCapability(
        provider,
        default_limit=args.limit,
        default_max_snippet_chars=args.max_snippet_chars,
    )
    call = CapabilityCall(
        descriptor=descriptor(),
        payload={"query": args.query},
        context=ExecutionContext(
            principal="cli-operator",
            server_id=uuid.UUID(int=0),
            run_id=uuid.uuid4(),
            parent_scope=None,
            profile_revision="cli",
            prompt_revision="cli",
            credential_references=(),
            allowed_capabilities=(CAPABILITY_ID,),
            mode=PolicyMode.DELEGATED,
        ),
        credentials={},
        op_id=uuid.uuid4(),
    )
    try:
        result = await capability(call)
    except SearchError as exc:
        _emit({"kind": "error", "code": exc.code.value, "detail": str(exc)})
        return 1
    _emit(json.loads(result.output))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mai.research.search",
        description="Sanitized, provenance-cited web search over the "
        "bundled SearXNG instance (operator entrypoint; the product path "
        "is the policy-bound capability dispatch).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    query = sub.add_parser("query", help="run one bounded sanitized query")
    query.add_argument("--base-url", required=True, help="SearXNG instance root")
    query.add_argument("--query", required=True, help="query text")
    query.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    query.add_argument(
        "--max-snippet-chars", type=int, default=DEFAULT_MAX_SNIPPET_CHARS
    )
    query.add_argument("--timeout-seconds", type=float, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: exit 0 query ran (JSON status distinguishes
    ok / unavailable / zero results) / 1 structured rejection / 2 usage."""
    args = _build_parser().parse_args(argv)
    if args.command == "query":
        return asyncio.run(_query_async(args))
    return 2  # pragma: no cover - argparse `required=True` guards this


if __name__ == "__main__":  # pragma: no cover - argparse glue
    sys.exit(main())
