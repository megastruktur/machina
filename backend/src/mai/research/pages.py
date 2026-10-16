"""Egress-controlled web page reading with provenance and clean Markdown.

Card ``07-page-reader`` (service ``research``), against contracts.md
**C07** rule 4 (retrieval returns excerpts plus citation IDs and
provenance; truncated results remain traceable) and rule 7's placement
companion (page fetching/extraction stays separate from search —
knowledge-packaging ADR §3.2), consuming the reviewed prerequisites
``07-search-capability`` (the provenance-citation normalizer and the
sibling ``web.search`` pattern this module mirrors), ``03-dispatcher`` and
``06-tool-bridge`` (the only invocation paths — this executor is reachable
exclusively as a trusted capability implementation behind
:class:`~mai.capabilities.dispatch.CapabilityDispatcher`).

- **Egress policy (acceptance 1, acceptance 2)** — the fetch target passes
  an address policy *before any connection* and again at every redirect
  hop: only ``http``/``https`` URLs without embedded credentials, whose
  host (IP literal, or every address the resolver returns) is globally
  routable, are ever contacted. Private, loopback, link-local, multicast,
  reserved and unspecified addresses refuse with ``address_refused`` —
  a redirect to ``http://127.0.0.1/`` or to a name that resolves to an
  internal address is refused exactly like a direct request, so redirects
  and DNS changes cannot bypass the policy (acceptance 2). The resolver
  The policy is host wiring
  (``allow_private_addresses``; default refuse); the payload carries no
  endpoint, address or engine field (C03 rule 2). The resolved addresses
  are never reported — internal infrastructure naming stays internal.
  The connect is pinned to a validated address (the request URL is
  rewritten to the validated IP with the original authority on the
  ``Host`` header and, for HTTPS, ``sni_hostname``), so a rebinding DNS
  answer cannot redirect the dial off-policy — there is no window
  between validation and connect.
- **Size/time/redirect budgets (acceptance 1)** — the response body is
  read as a bounded stream: a ``Content-Length`` over the byte budget
  refuses before reading, and the streaming read (the transfer itself,
  not just the retained bytes) stops at budget+1 decoded bytes, so an
  oversized page is ``response_too_large`` after at most ``max_bytes + 1``
  bytes of work. The fetch has a bounded timeout, at most
  ``MAX_REDIRECTS`` hops are followed, and only HTML-family content types
  are extracted (anything else is ``unsupported_content_type``). The
  capability is a reader, not a generic downloader: binary documents are
  out of scope by design.
- **Clean Markdown, no script execution (feature-gate scenario)** —
  extraction is a pure stdlib ``html.parser`` pass: ``script``/``style``/
  ``noscript``/``template``/embedded-object subtrees never reach the
  output, active content has no execution surface by construction (there
  is no DOM, no subresource loading, no event handling), and
  ``javascript:``/``data:`` link targets are dropped, so hostile page
  text can never mint a tool or a navigation. Headings, paragraphs,
  lists, quotes, code, links and tables become bounded Markdown; the
  output is character-capped with a ``truncated`` flag, so a cut page
  keeps its citation and stays traceable (C07 rule 4).
- **Provenance (acceptance 2)** — the result carries the requested URL,
  the final URL after any redirect hops, and the normalized citation, so
  quoted text is traceable to the fetchable source. The frozen document
  labels the page ``"trust": "untrusted_external"``: page text is sourced
  data quoted into a model context, never an instruction channel; the
  capability performs fetching/bounding/quoting only (C06 rule 2 —
  retrieved text is context, never system authority).
- **Honest failure taxonomy** — a timeout is ``fetch_timeout``, a
  transport/DNS failure is ``fetch_unreachable``, an HTTP error status is
  ``fetch_error``; policy and budget refusals
  (``address_refused``/``response_too_large``/``too_many_redirects``/
  ``unsupported_content_type``) are ``status="refused"`` — the policy
  decision succeeded, the fetch did not run. Errors carry stable codes
  and the exception *type* only, never messages.

Effect class ``read``; the module reads no process environment and takes
no clock. The network seam is the injected provider alone (the httpx2
transport and the resolver stay constructor/test seams exactly like
``07-search-capability``'s provider); no new dependency is introduced —
extraction is stdlib ``html.parser``.

Production entrypoint (exit contract: 0 read ran — the JSON status
distinguishes ``ok`` / ``refused`` / ``unavailable``; 1 structured
rejection; 2 usage error)::

    uv run python -m mai.research.pages read --url URL \
        [--max-bytes N] [--timeout-seconds N] [--max-markdown-chars N] \
        [--allow-private-addresses]   # operator/local-fixture wiring only
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import socket
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from mai.capabilities.dispatch import CapabilityCall, ExecutorResult
from mai.plugins.registry import CapabilityDescriptor, parse_descriptor
from mai.policy.evaluate import ExecutionContext, PolicyMode
from mai.research.search import normalize_citation_url

__all__ = [
    "CAPABILITY_ID",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_MARKDOWN_CHARS",
    "DEFAULT_TIMEOUT_SECONDS",
    "HttpPageProvider",
    "MAX_MAX_BYTES",
    "MAX_MARKDOWN_CHARS",
    "MAX_REDIRECTS",
    "MAX_TIMEOUT_SECONDS",
    "MAX_URL_CHARS",
    "MIN_MAX_BYTES",
    "MIN_MAX_MARKDOWN_CHARS",
    "PageContent",
    "PageError",
    "PageErrorCode",
    "PageProvider",
    "PageRequest",
    "PageResult",
    "PageStatus",
    "RawPage",
    "WebPageCapability",
    "check_address_policy",
    "descriptor",
    "extract_markdown",
    "ip_allowed",
    "request_from_payload",
    "result_document",
    "validate_fetch_url",
    "validated_addresses",
]

#: The capability slug this module owns (registry identifier shape; C03
#: rule 1). Grants name this id; the descriptor and the executor agree.
CAPABILITY_ID = "web.read"

#: URL bound: a cited page source is a fetchable reference, and the cap
#: keeps payloads and outbound request lines bounded.
MAX_URL_CHARS = 2000

#: Default and bounds for the raw-body byte budget. The default settles a
#: typical article honestly; the cap bounds the worst case a host may
#: construct. The floor keeps the budget a real page, not a probe.
DEFAULT_MAX_BYTES = 256 * 1024
MIN_MAX_BYTES = 16 * 1024
MAX_MAX_BYTES = 1024 * 1024

#: Default and bounds for the fetch timeout.
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_TIMEOUT_SECONDS = 60.0

#: Redirect hop bound: a cited source may move a few times; beyond this
#: the chain is treated as evasive and refused.
MAX_REDIRECTS = 5

#: Default and bounds for the extracted Markdown character budget. The
#: output is model-context material (C06 rule 3): bounded with an honest
#: ``truncated`` flag, never a silent cut.
DEFAULT_MAX_MARKDOWN_CHARS = 20_000
MIN_MAX_MARKDOWN_CHARS = 1_000
MAX_MARKDOWN_CHARS = 100_000

#: The HTML-family content types this reader extracts. Anything else is
#: ``unsupported_content_type`` — the capability reads pages, it is not a
#: generic file downloader (bounded scope by design).
_ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})

#: Redirect statuses followed as hops (GET semantics throughout; this
#: capability never sends a body).
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: The descriptive, non-identifying outbound User-Agent (sanitized
#: outbound context): a library-default UA would advertise the HTTP client
#: and version to every page host the reader contacts.
USER_AGENT = "mai-page-reader/1.0 (internal research capability)"

#: The streaming-read chunk size: bounded read granularity, not a payload
#: knob.
_READ_CHUNK_BYTES = 8192


class PageStatus(StrEnum):
    """The honest answer of one read attempt."""

    OK = "ok"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


class PageErrorCode(StrEnum):
    """Stable machine-readable page refusal/outage codes."""

    ADDRESS_REFUSED = "address_refused"
    RESPONSE_TOO_LARGE = "response_too_large"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    FETCH_TIMEOUT = "fetch_timeout"
    FETCH_UNREACHABLE = "fetch_unreachable"
    FETCH_ERROR = "fetch_error"
    INVALID_PAYLOAD = "invalid_payload"


class PageError(ValueError):
    """One structured, value-free payload/refusal rejection.

    Raised for malformed *direct* executor calls, for URLs that fail
    syntax validation, and for address-policy/redirect/byte/content
    refusals raised by the provider (before or at the bounded point — an
    oversized body is abandoned at budget+1 bytes). ``at_url`` records
    which hop refused when it differs from the requested URL, so a
    redirect-hop refusal stays traceable without naming addresses.
    """

    def __init__(
        self,
        code: PageErrorCode,
        detail: str,
        *,
        at_url: str | None = None,
    ) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail
        self.at_url = at_url


def _reject(
    code: PageErrorCode, detail: str, *, at_url: str | None = None
) -> PageError:
    return PageError(code, detail, at_url=at_url)


# ---------------------------------------------------------------------------
# URL validation and the address policy (before any connection)
# ---------------------------------------------------------------------------


def ip_allowed(address: str, *, allow_private: bool = False) -> bool:
    """Whether one resolved address may be contacted under the policy.

    The allowed set is exactly the globally routable one: global scope
    and neither multicast nor unspecified. Everything else — private,
    loopback, link-local, reserved/documentation ranges — refuses. With
    ``allow_private`` the check is host wiring's explicit opt-out (local
    fixtures, operator use), never a payload choice.
    """
    if allow_private:
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return bool(ip.is_global and not ip.is_multicast and not ip.is_unspecified)


def _host_addresses(
    url: str, host: str, resolver: Callable[[str], Sequence[str]]
) -> tuple[str, ...]:
    """The validated, deduplicated address set of one hostname.

    An IP-literal host is checked directly; a named host is resolved
    through the injected resolver seam (production: real DNS). Raises
    ``address_refused`` when resolution yields nothing usable; resolver
    failures (``socket.gaierror``) propagate so the provider can classify
    them as transport-level ``fetch_unreachable``.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        addresses = tuple(sorted({str(item) for item in resolver(host)}))
        if not addresses:
            raise _reject(
                PageErrorCode.ADDRESS_REFUSED,
                "host resolved to no addresses",
                at_url=url,
            )
        return addresses
    return (host,)


def validated_addresses(
    url: str,
    resolver: Callable[[str], Sequence[str]],
    *,
    allow_private: bool = False,
) -> tuple[str, ...]:
    """The validated address set of one URL, or ``address_refused``.

    Runs before any connection and again at every redirect hop; the
    resolver is re-invoked per hop, so a name whose resolution changes
    across hops (DNS rebind across the chain) is re-checked and refused.
    Returns the sorted address set so the caller dials an address that
    passed the policy — the connect is pinned, not re-resolved.
    """
    parts = urlsplit(url)
    host = parts.hostname
    if host is None:
        raise _reject(
            PageErrorCode.ADDRESS_REFUSED, "URL carries no host", at_url=url
        )
    addresses = _host_addresses(url, host, resolver)
    for address in addresses:
        if not ip_allowed(address, allow_private=allow_private):
            raise _reject(
                PageErrorCode.ADDRESS_REFUSED,
                "host resolves to a non-public address; refusing "
                "contact under the configured address policy",
                at_url=url,
            )
    return addresses


def check_address_policy(
    url: str,
    resolver: Callable[[str], Sequence[str]],
    *,
    allow_private: bool = False,
) -> None:
    """Refuse ``url`` unless every address it can connect to is allowed.

    The policy half of :func:`validated_addresses`; kept as the explicit
    name for policy tests and callers that do not need the address set.
    """
    validated_addresses(url, resolver, allow_private=allow_private)


def validate_fetch_url(url: str) -> str:
    """Validate one absolute fetch URL syntactically (no network).

    Returns the stripped URL when it is a fetchable http(s) target:
    absolute, http(s) scheme, no embedded userinfo (a credential-bearing
    URL is never fetchable), parseable host/port, and within the URL
    bound. Address-policy enforcement is a separate step
    (:func:`check_address_policy`) so it can be re-run per redirect hop.
    Raises ``PageError`` with a stable code otherwise.
    """
    if not isinstance(url, str) or not url.strip():
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            "url must be a non-empty string",
        )
    stripped = url.strip()
    if len(stripped) > MAX_URL_CHARS or "\x00" in stripped or "\n" in stripped:
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            f"url must be at most {MAX_URL_CHARS} characters without "
            "control characters",
        )
    try:
        parts = urlsplit(stripped)
    except ValueError:
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            "url is not parseable",
        ) from None
    if parts.scheme.lower() not in ("http", "https"):
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            "url scheme must be http or https",
        )
    if parts.username is not None or parts.password is not None:
        # Embedded credentials would ride the request; a reader never
        # sends credentials it was handed inside a URL.
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            "url must not embed userinfo credentials",
        )
    if not parts.hostname:
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            "url must carry a host",
        )
    if any(char.isspace() for char in parts.hostname):
        # urlsplit tolerates spaces inside a hostname; such a name can
        # never be dialed and only feeds parser differentials.
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            "url host must not contain whitespace",
        )
    try:
        parts.port
    except ValueError:
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            "url port is not parseable",
        ) from None
    return stripped


def _default_resolver(host: str) -> Sequence[str]:
    """The production resolver seam: real DNS via ``getaddrinfo``.

    DNS failures propagate as ``socket.gaierror``; the provider maps them
    to ``fetch_unreachable`` at the boundary.
    """
    infos = socket.getaddrinfo(host, None)
    return tuple(sorted({str(info[4][0]) for info in infos}))


# ---------------------------------------------------------------------------
# Payload validation (defense in depth; the bridge schema gate is primary)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageRequest:
    """One validated page-read request (capability payload shape)."""

    url: str
    max_bytes: int
    timeout_seconds: float
    max_markdown_chars: int


def request_from_payload(
    payload: Mapping[str, object],
    *,
    default_max_bytes: int = DEFAULT_MAX_BYTES,
    default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    default_max_markdown_chars: int = DEFAULT_MAX_MARKDOWN_CHARS,
) -> PageRequest:
    """Validate one capability payload into a :class:`PageRequest`.

    Strict and fail-closed exactly like the sibling capability: unknown
    keys are rejected (``additionalProperties: false`` in the descriptor),
    so no smuggled field can reach the fetch. The payload carries no
    endpoint or address field (C03 rule 2): the address policy is host
    wiring and the target vocabulary is the execution context alone.
    """
    if not isinstance(payload, Mapping):
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            "payload must be a JSON object (mapping)",
        )
    allowed = {"url", "max_bytes", "timeout_seconds", "max_markdown_chars"}
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            f"unknown payload fields: {', '.join(unknown)}",
        )

    url = payload.get("url")
    if not isinstance(url, str) or not url.strip():
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            "url must be a non-empty string",
        )

    max_bytes = payload.get("max_bytes", default_max_bytes)
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not MIN_MAX_BYTES <= max_bytes <= MAX_MAX_BYTES
    ):
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            f"max_bytes must be an integer between "
            f"{MIN_MAX_BYTES} and {MAX_MAX_BYTES}",
        )

    timeout = payload.get("timeout_seconds", default_timeout_seconds)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            "timeout_seconds must be a number",
        )
    timeout = float(timeout)
    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            f"timeout_seconds must be within (0, {MAX_TIMEOUT_SECONDS}]",
        )

    max_markdown = payload.get("max_markdown_chars", default_max_markdown_chars)
    if (
        isinstance(max_markdown, bool)
        or not isinstance(max_markdown, int)
        or not MIN_MAX_MARKDOWN_CHARS <= max_markdown <= MAX_MARKDOWN_CHARS
    ):
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            f"max_markdown_chars must be an integer between "
            f"{MIN_MAX_MARKDOWN_CHARS} and {MAX_MARKDOWN_CHARS}",
        )

    return PageRequest(
        url=url.strip(),
        max_bytes=max_bytes,
        timeout_seconds=timeout,
        max_markdown_chars=max_markdown,
    )


# ---------------------------------------------------------------------------
# Clean Markdown extraction (pure stdlib; no script execution surface)
# ---------------------------------------------------------------------------

#: Subtrees dropped wholesale: active content, form controls and
#: non-evidence material never reach the Markdown output. ``meta``/
#: ``link``/``base`` are void elements whose attributes are never read;
#: they are listed so self-closing forms stay balanced in the skip-depth
#: accounting. There is no DOM and no execution surface — the parser
#: only turns markup into text.
_DROP_SUBTREES = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "iframe",
        "svg",
        "canvas",
        "object",
        "embed",
        "applet",
        "form",
        "input",
        "button",
        "select",
        "textarea",
        "option",
        "fieldset",
        "label",
        "datalist",
        "output",
        "meta",
        "link",
        "base",
    }
)

#: Link targets allowed to survive into Markdown. Anything else
#: (``javascript:``, ``data:``, ``vbscript:``) is dropped: hostile page
#: text can never mint a navigation or a fetch.
_ALLOWED_LINK_SCHEMES = frozenset({"http", "https", "mailto"})

_HEADING_PREFIXES = {
    "h1": "# ",
    "h2": "## ",
    "h3": "### ",
    "h4": "#### ",
    "h5": "##### ",
    "h6": "###### ",
}

_BLOCK_BREAK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "nav",
        "figure",
        "figcaption",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "table",
        "blockquote",
        "pre",
        "hr",
        "body",
    }
)

_INLINE_CLOSE = {"code": "`", "strong": "**", "b": "**", "em": "*", "i": "*"}


def _safe_link(target: str) -> str | None:
    """One sanitized link target, or ``None`` to drop the link.

    Relative references (no scheme) and the allowed absolute schemes
    survive with whitespace and control characters stripped; every other
    scheme — active or binary — is dropped.
    """
    cleaned = "".join(target.split())
    if not cleaned:
        return None
    try:
        scheme = urlsplit(cleaned).scheme.lower()
    except ValueError:
        return None
    if scheme and scheme not in _ALLOWED_LINK_SCHEMES:
        return None
    return cleaned


class _MarkdownExtractor(HTMLParser):
    """The pure HTML→Markdown pass (stdlib ``html.parser`` only).

    Emits Markdown into ``self._out``; maintains a drop-depth so refused
    subtrees contribute nothing, an inline-formatting stack for
    code/emphasis, and one pending-prefix slot so list markers, heading
    hashes and quote markers precede the next text run. Character
    references are resolved by the parser itself (``convert_charrefs``);
    there is no scripting, no subresource loading and no event handling
    anywhere in this pass — a script subtree's text is simply never
    emitted.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._drop_depth = 0
        self._inline: list[str] = []  # active wrappers, innermost last
        self._pending_prefix: str | None = None
        self._link_stack: list[str | None] = []  # href per open <a>
        self._pre_depth = 0
        self._list_stack: list[str] = []  # "ul"/"ol" per open list
        self.title_parts: list[str] = []
        self._in_title = False

    # -- helpers ------------------------------------------------------

    def _emit(self, text: str) -> None:
        if not text:
            return
        if self._pending_prefix is not None:
            self._out.append(self._pending_prefix)
            self._pending_prefix = None
        self._out.append(text)

    def _newline_block(self) -> None:
        self._out.append("\n\n")

    # -- tag handling ---------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._drop_depth:
            if tag in _DROP_SUBTREES:
                self._drop_depth += 1
            return
        if tag in _DROP_SUBTREES:
            self._drop_depth = 1
            return

        attr = {name: value for name, value in attrs if value is not None}

        if tag == "title":
            self._in_title = True
        elif tag in _HEADING_PREFIXES:
            self._newline_block()
            self._pending_prefix = _HEADING_PREFIXES[tag]
        elif tag == "li":
            self._out.append("\n")
            self._pending_prefix = (
                "1. " if self._list_stack and self._list_stack[-1] == "ol" else "- "
            )
        elif tag in ("ul", "ol"):
            self._list_stack.append(tag)
            self._out.append("\n")
        elif tag == "blockquote":
            self._newline_block()
            self._pending_prefix = "> "
        elif tag == "pre":
            self._newline_block()
            self._out.append("```\n")
            self._pre_depth += 1
        elif tag == "code" and not self._pre_depth:
            self._inline.append("`")
            self._out.append("`")
        elif tag in ("strong", "b"):
            self._inline.append("**")
            self._out.append("**")
        elif tag in ("em", "i"):
            self._inline.append("*")
            self._out.append("*")
        elif tag == "br":
            self._out.append("\n")
        elif tag == "hr":
            self._newline_block()
            self._out.append("---\n")
        elif tag == "a":
            target = _safe_link(attr["href"]) if "href" in attr else None
            self._link_stack.append(target)
            if target is not None:
                self._out.append("[")
        elif tag == "img":
            source = _safe_link(attr["src"]) if "src" in attr else None
            if source is not None:
                alt = (attr.get("alt") or "").strip()
                self._emit(f"![{alt}]({source})")
        elif tag in ("td", "th"):
            self._out.append(" | ")
        elif tag == "tr":
            self._out.append("\n")
        elif tag in _BLOCK_BREAK_TAGS or tag in _HEADING_PREFIXES:
            self._newline_block()

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._drop_depth:
            if tag in _DROP_SUBTREES:
                self._drop_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        elif tag in ("ul", "ol") and self._list_stack:
            self._list_stack.pop()
            self._out.append("\n")
        elif tag == "pre" and self._pre_depth:
            self._out.append("\n```\n")
            self._pre_depth -= 1
        elif tag in _INLINE_CLOSE and self._inline:
            # Close the innermost wrapper only; mismatched HTML cannot
            # corrupt output outside its own span.
            wanted = _INLINE_CLOSE[tag]
            if self._inline[-1] == wanted:
                self._inline.pop()
                self._out.append(wanted)
        elif tag == "a":
            target = self._link_stack.pop() if self._link_stack else None
            if target is not None:
                self._out.append(f"]({target})")
        elif tag in _BLOCK_BREAK_TAGS or tag in _HEADING_PREFIXES or tag == "li":
            self._newline_block()

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._pre_depth:
            self._emit(data)
            return
        # Squash interior runs of whitespace to one space, but keep one
        # boundary space each way: inline formatting (``**bold**``) and
        # adjacent links only read correctly with their edge spaces.
        leading = data[:1].isspace()
        trailing = data[-1:].isspace()
        core = " ".join(data.split())
        if not core:
            text = " " if leading and trailing else ""
        else:
            text = (" " if leading else "") + core + (" " if trailing else "")
        self._emit(text)


@dataclass(frozen=True, slots=True)
class PageContent:
    """The bounded, extracted page: clean Markdown plus the title."""

    title: str
    markdown: str
    truncated: bool


def _collapse_markdown(text: str) -> str:
    """Normalize the raw emit stream into clean Markdown lines."""
    lines = [line.strip() for line in text.split("\n")]
    out: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank and out:
                out.append("")
            blank = True
            continue
        blank = False
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def extract_markdown(
    html: str, *, max_chars: int = DEFAULT_MAX_MARKDOWN_CHARS
) -> PageContent:
    """Extract clean, bounded Markdown from one HTML document string.

    Pure and side-effect free: ``script``/``style``/form/embedded-object
    subtrees never reach the output, active URL schemes are dropped at
    the link layer, and the result is character-capped with an honest
    ``truncated`` flag — a cut page keeps its citation downstream.
    """
    if not isinstance(html, str):
        raise _reject(
            PageErrorCode.INVALID_PAYLOAD,
            "html must be a decoded string",
        )
    extractor = _MarkdownExtractor()
    extractor.feed(html)
    extractor.close()
    title = " ".join("".join(extractor.title_parts).split())[:300]
    body = _collapse_markdown("".join(extractor._out))
    truncated = len(body) > max_chars
    if truncated:
        body = body[:max_chars]
    return PageContent(title=title, markdown=body, truncated=truncated)


# ---------------------------------------------------------------------------
# Provider seam (httpx2; transport and resolver stay constructor/test seams)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawPage:
    """One fetched page at the provider boundary.

    ``url`` is the final URL after any redirect hops; ``hops`` counts
    them; ``charset`` is the response-declared encoding (or ``utf-8``).
    """

    url: str
    content_type: str
    bytes_read: int
    body: bytes
    charset: str
    hops: int


class PageProvider(Protocol):
    """The injected network seam: one URL → one raw page.

    Implementations raise :class:`PageError` for policy/budget refusals
    and on transport failure; the capability classifies. Production is
    :class:`HttpPageProvider`; tests inject deterministic fakes or an
    httpx2 ``MockTransport``.
    """

    async def fetch(self, *, url: str, max_bytes: int) -> RawPage:
        """Fetch one syntactically valid URL with policy enforced."""
        ...


def _classify_fetch_exception(exc: BaseException) -> PageErrorCode:
    """Map one provider exception to its stable seam code (value-free)."""
    import httpx2

    if isinstance(exc, httpx2.TimeoutException):
        return PageErrorCode.FETCH_TIMEOUT
    if isinstance(exc, httpx2.NetworkError):
        return PageErrorCode.FETCH_UNREACHABLE
    return PageErrorCode.FETCH_ERROR


class HttpPageProvider:
    """The bounded egress-controlled page fetch over an httpx2 client.

    Enforces, in order per hop: syntax validation, the address policy
    (resolve + validate, then dial the validated address — the connect
    is pinned), the hop count, the content-type gate, and the byte
    budget (``Content-Length`` pre-check, then a streaming read
    abandoned at budget+1 decoded bytes). Sends a descriptive
    non-identifying User-Agent — no caller headers, no credentials, no
    host identity beyond the capability label (sanitized outbound
    context). The resolver and transport are constructor seams
    (production: real DNS / real dial; tests: deterministic fakes),
    exactly like ``07-search-capability``'s provider.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_redirects: int = MAX_REDIRECTS,
        allow_private_addresses: bool = False,
        resolver: Callable[[str], Sequence[str]] | None = None,
        transport: object | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise _reject(
                PageErrorCode.INVALID_PAYLOAD,
                f"timeout_seconds must be within (0, {MAX_TIMEOUT_SECONDS}]",
            )
        if not MIN_MAX_BYTES <= max_bytes <= MAX_MAX_BYTES:
            raise _reject(
                PageErrorCode.INVALID_PAYLOAD,
                f"max_bytes must be an integer between "
                f"{MIN_MAX_BYTES} and {MAX_MAX_BYTES}",
            )
        if not 0 < max_redirects <= MAX_REDIRECTS:
            raise _reject(
                PageErrorCode.INVALID_PAYLOAD,
                f"max_redirects must be an integer between 1 and {MAX_REDIRECTS}",
            )
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._allow_private = allow_private_addresses
        self._resolver = resolver if resolver is not None else _default_resolver
        self._transport = transport

    def __repr__(self) -> str:
        return (
            f"<HttpPageProvider timeout={self._timeout_seconds!r} "
            f"max_bytes={self._max_bytes!r} allow_private={self._allow_private!r}>"
        )

    def _pinned_target(self, url: str) -> tuple[str, str, str | None]:
        """The pinned dial target for one validated URL.

        Resolves and validates the host (:func:`validated_addresses`), then
        rewrites the request URL to dial the validated address directly —
        the connect cannot be re-resolved by a rebinding DNS answer. The
        original authority rides the ``Host`` header; HTTPS also passes
        ``sni_hostname`` so TLS SNI and certificate validation use the
        original hostname, not the address (httpcore2 honors
        ``extensions["sni_hostname"]``). Returns ``(request_url,
        host_header, sni_hostname_or_None)``.
        """
        parts = urlsplit(url)
        host = parts.hostname or ""
        scheme = parts.scheme.lower()
        port = parts.port
        address = validated_addresses(
            url, self._resolver, allow_private=self._allow_private
        )[0]
        dial_host = f"[{address}]" if ":" in address else address
        default_port = 443 if scheme == "https" else 80
        if port is None or port == default_port:
            netloc = dial_host
            header_host = host
        else:
            netloc = f"{dial_host}:{port}"
            bracketed = f"[{host}]" if ":" in host else host
            header_host = f"{bracketed}:{port}"
        target = urlunsplit((scheme, netloc, parts.path, parts.query, ""))
        sni = host if scheme == "https" else None
        return target, header_host, sni

    async def fetch(self, *, url: str, max_bytes: int) -> RawPage:
        """Fetch one URL with full policy/budget enforcement.

        Redirects are followed manually so every hop re-resolves and
        re-validates the address policy; each hop dials its validated
        address directly (pinned connect), so neither a redirect target
        nor a DNS answer change can bypass the policy. The body streams
        under the byte budget — the transfer itself is bounded, not just
        the retained bytes. Refusals raise :class:`PageError` with the
        refusing hop URL; transport failures propagate as httpx2
        exceptions for the capability to classify.
        """
        import httpx2

        if not MIN_MAX_BYTES <= max_bytes <= self._max_bytes:
            raise _reject(
                PageErrorCode.INVALID_PAYLOAD,
                f"max_bytes must be an integer between "
                f"{MIN_MAX_BYTES} and the provider ceiling {self._max_bytes}",
            )
        current = validate_fetch_url(url)
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
                        PageErrorCode.TOO_MANY_REDIRECTS,
                        f"more than {self._max_redirects} redirect hops",
                        at_url=current,
                    )
                try:
                    target, host_header, sni = self._pinned_target(current)
                except socket.gaierror as exc:  # DNS failure: transport class
                    raise httpx2.ConnectError(str(exc)) from exc
                request_headers = {"Host": host_header}
                extensions = {"sni_hostname": sni} if sni is not None else {}
                # client.stream keeps the body unread until the budgeted
                # read below: unlike client.get (which fully downloads
                # before returning), the transfer is bounded by the early
                # abort at budget+1 bytes.
                async with client.stream(
                    "GET", target, headers=request_headers, extensions=extensions
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise _reject(
                                PageErrorCode.FETCH_ERROR,
                                "redirect response carried no Location header",
                                at_url=current,
                            )
                        current = validate_fetch_url(urljoin(current, location))
                        hops += 1
                        continue
                    # The stream context owns closing the response (normal
                    # exit, refusal raise, or budget abort alike).
                    return await self._read_body(
                        response, current, hops, max_bytes
                    )
        finally:
            await client.aclose()

    async def _read_body(
        self, response: object, url: str, hops: int, max_bytes: int
    ) -> RawPage:
        """Content-type gate, length pre-check, bounded streaming read.

        Runs inside the caller's ``client.stream`` context: the response
        is closed by that context on every exit path, and the streaming
        abort at budget+1 *decoded* bytes bounds the transfer itself.
        """
        import httpx2

        resp: httpx2.Response = response  # type: ignore[assignment]
        content_type = (
            resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        if content_type not in _ALLOWED_CONTENT_TYPES:
            raise _reject(
                PageErrorCode.UNSUPPORTED_CONTENT_TYPE,
                f"content type {content_type!r} is not an HTML page",
                at_url=url,
            )
        declared = resp.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared.strip()) > max_bytes:
                    raise _reject(
                        PageErrorCode.RESPONSE_TOO_LARGE,
                        f"declared Content-Length exceeds the "
                        f"{max_bytes}-byte budget",
                        at_url=url,
                    )
            except ValueError:
                pass  # non-numeric length: the streaming cap governs
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes(_READ_CHUNK_BYTES):
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise _reject(
                    PageErrorCode.RESPONSE_TOO_LARGE,
                    f"response exceeded the {max_bytes}-byte budget",
                    at_url=url,
                )
        if resp.status_code >= 400:
            # Read-then-status keeps the refusal taxonomy honest about
            # where the bytes went; an error page is fetch_error, not a
            # page.
            raise httpx2.HTTPStatusError(
                f"HTTP {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        charset = resp.charset_encoding or "utf-8"
        return RawPage(
            url=url,
            content_type=content_type,
            bytes_read=total,
            body=b"".join(chunks),
            charset=charset,
            hops=hops,
        )


# ---------------------------------------------------------------------------
# Result types (frozen shape via result_document)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageResult:
    """One honest page-read answer, value-free on refusal/unavailable.

    ``status`` is ``ok`` only when a page was fetched and extracted.
    ``refused`` is the policy/budget decision succeeding (the fetch did
    not run); ``unavailable`` names which seam failed via ``code`` and
    the exception *type* only. The requested/final/citation URLs keep
    every outcome traceable to its source (C07 rule 4).
    """

    status: PageStatus
    code: PageErrorCode | None
    url: str
    final_url: str
    title: str
    markdown: str
    truncated: bool
    bytes_read: int
    redirects: int
    error_type: str | None = None


def result_document(result: PageResult) -> dict[str, object]:
    """The frozen JSON document of one page-read result (stable keys)."""
    citation = normalize_citation_url(result.final_url)
    return {
        "status": result.status.value,
        "code": result.code.value if result.code is not None else None,
        "trust": "untrusted_external",
        "url": result.url,
        "final_url": result.final_url,
        "citation": citation if citation is not None else result.final_url,
        "title": result.title,
        "markdown": result.markdown,
        "truncated": result.truncated,
        "bytes_read": result.bytes_read,
        "redirects": result.redirects,
        "error_type": result.error_type,
    }


# ---------------------------------------------------------------------------
# Capability descriptor (C03 rule 1 frozen vocabulary)
# ---------------------------------------------------------------------------


def descriptor() -> CapabilityDescriptor:
    """The validated capability descriptor (C03 rule 1 vocabulary).

    ``effect_class="read"`` is the load-bearing field: a page GET
    changes no observable state, so the policy layer requires no
    approval decision in either prompting mode — while every
    grant/scope/policy gate still applies unchanged. Availability to a
    subagent is exactly the ``06-tool-bridge`` rule: exposed only when
    registered and in the run's ``allowed_capabilities``, grants
    intersected, never unioned (C03 rule 3). ``additionalProperties:
    false`` pins the payload surface: no field can name an endpoint, an
    address or a policy (the address policy is host wiring; the target
    vocabulary is the execution context alone).
    """
    return parse_descriptor(
        {
            "identifier": CAPABILITY_ID,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["url"],
                "properties": {
                    "url": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_URL_CHARS,
                        "description": (
                            "Absolute http(s) page URL to read. Private, "
                            "loopback and otherwise non-public addresses "
                            "refuse before any connection, at every "
                            "redirect hop (code address_refused); URLs "
                            "with embedded credentials are rejected."
                        ),
                    },
                    "max_bytes": {
                        "type": "integer",
                        "minimum": MIN_MAX_BYTES,
                        "maximum": MAX_MAX_BYTES,
                        "default": DEFAULT_MAX_BYTES,
                        "description": (
                            "Raw-body byte budget; a Content-Length over "
                            "budget refuses before reading, the streaming "
                            "read stops at budget+1 (code "
                            "response_too_large)."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "minimum": 0.1,
                        "maximum": MAX_TIMEOUT_SECONDS,
                        "default": DEFAULT_TIMEOUT_SECONDS,
                        "description": (
                            "Whole-fetch time budget; a timeout classifies "
                            "as fetch_timeout, value-free."
                        ),
                    },
                    "max_markdown_chars": {
                        "type": "integer",
                        "minimum": MIN_MAX_MARKDOWN_CHARS,
                        "maximum": MAX_MARKDOWN_CHARS,
                        "default": DEFAULT_MAX_MARKDOWN_CHARS,
                        "description": (
                            "Extracted Markdown character budget; a cut "
                            "page keeps its citation and is flagged "
                            "truncated."
                        ),
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "required": [
                    "status",
                    "trust",
                    "url",
                    "final_url",
                    "citation",
                    "title",
                    "markdown",
                    "truncated",
                ],
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": sorted(item.value for item in PageStatus),
                    },
                    "code": {"type": ["string", "null"]},
                    "trust": {
                        "type": "string",
                        "enum": ["untrusted_external"],
                        "description": (
                            "Page text is external sourced data quoted "
                            "into context, never an instruction channel."
                        ),
                    },
                    "url": {"type": "string"},
                    "final_url": {"type": "string"},
                    "citation": {"type": "string"},
                    "title": {"type": "string"},
                    "markdown": {"type": "string"},
                    "truncated": {"type": "boolean"},
                    "bytes_read": {"type": "integer"},
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


class WebPageCapability:
    """``web.read`` executor: one gated call → one honest page answer.

    Reachable only through the dispatcher (C03 rule 7); performs no
    policy decision of its own — the :class:`ExecutionContext` supplies
    the only target vocabulary (C03 rule 2) and grants are intersected
    upstream of this executor. Order per call: validate payload → one
    bounded fetch (syntax + address re-validation per hop inside the
    provider) → decode → extract bounded Markdown → one frozen JSON
    document. Policy/budget refusals and provider outages are honest
    statuses in the result document; only malformed direct calls raise.
    """

    def __init__(
        self,
        provider: PageProvider,
        *,
        default_max_bytes: int = DEFAULT_MAX_BYTES,
        default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        default_max_markdown_chars: int = DEFAULT_MAX_MARKDOWN_CHARS,
    ) -> None:
        if not hasattr(provider, "fetch"):
            raise _reject(
                PageErrorCode.INVALID_PAYLOAD,
                "provider must implement the PageProvider protocol "
                "(an async fetch(url=..., max_bytes=...) -> RawPage)",
            )
        self._provider = provider
        self._default_max_bytes = default_max_bytes
        self._default_timeout = default_timeout_seconds
        self._default_max_markdown = default_max_markdown_chars

    def __repr__(self) -> str:
        return f"<WebPageCapability provider={self._provider!r}>"

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        """Execute one gated call (see class docstring for the order)."""
        if not isinstance(call, CapabilityCall):
            raise _reject(
                PageErrorCode.INVALID_PAYLOAD,
                "executor input must be a dispatcher CapabilityCall",
            )
        context = call.context
        if not isinstance(context, ExecutionContext):
            raise _reject(
                PageErrorCode.INVALID_PAYLOAD,
                "call context must be an ExecutionContext",
            )
        request = request_from_payload(
            call.payload,
            default_max_bytes=self._default_max_bytes,
            default_timeout_seconds=self._default_timeout,
            default_max_markdown_chars=self._default_max_markdown,
        )
        try:
            raw = await self._provider.fetch(
                url=request.url, max_bytes=request.max_bytes
            )
        except PageError as exc:
            if exc.code is PageErrorCode.INVALID_PAYLOAD:
                # A malformed direct call is a caller error, not a policy
                # outcome — raise exactly like payload validation does.
                raise
            # Policy/budget refusal (possibly at a redirect hop): the
            # refusal is the honest answer, value-free.
            result = PageResult(
                status=PageStatus.REFUSED,
                code=exc.code,
                url=request.url,
                final_url=exc.at_url or request.url,
                title="",
                markdown="",
                truncated=False,
                bytes_read=0,
                redirects=0,
            )
        except Exception as exc:  # noqa: BLE001 - classified, value-free
            result = PageResult(
                status=PageStatus.UNAVAILABLE,
                code=_classify_fetch_exception(exc),
                url=request.url,
                final_url=request.url,
                title="",
                markdown="",
                truncated=False,
                bytes_read=0,
                redirects=0,
                error_type=type(exc).__name__,
            )
        else:
            text = raw.body.decode(raw.charset, errors="replace")
            content = extract_markdown(text, max_chars=request.max_markdown_chars)
            result = PageResult(
                status=PageStatus.OK,
                code=None,
                url=request.url,
                final_url=normalize_citation_url(raw.url) or raw.url,
                title=content.title,
                markdown=content.markdown,
                truncated=content.truncated,
                bytes_read=raw.bytes_read,
                redirects=raw.hops,
            )
        return ExecutorResult(
            ok=result.status is PageStatus.OK,
            output=json.dumps(
                result_document(result), sort_keys=True, ensure_ascii=True
            ),
        )


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))


async def _read_async(args: argparse.Namespace) -> int:
    provider = HttpPageProvider(
        timeout_seconds=(
            DEFAULT_TIMEOUT_SECONDS
            if args.timeout_seconds is None
            else args.timeout_seconds
        ),
        max_bytes=args.max_bytes,
        allow_private_addresses=args.allow_private_addresses,
    )
    capability = WebPageCapability(
        provider,
        default_max_bytes=args.max_bytes,
        default_timeout_seconds=(
            DEFAULT_TIMEOUT_SECONDS
            if args.timeout_seconds is None
            else args.timeout_seconds
        ),
        default_max_markdown_chars=args.max_markdown_chars,
    )
    call = CapabilityCall(
        descriptor=descriptor(),
        payload={"url": args.url},
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
    except PageError as exc:
        _emit({"kind": "error", "code": exc.code.value, "detail": str(exc)})
        return 1
    _emit(json.loads(result.output))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mai.research.pages",
        description="Egress-controlled web page reading with clean, "
        "bounded Markdown and provenance (operator entrypoint; the "
        "product path is the policy-bound capability dispatch).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    read = sub.add_parser("read", help="read one page into bounded Markdown")
    read.add_argument("--url", required=True, help="absolute http(s) page URL")
    read.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    read.add_argument("--timeout-seconds", type=float, default=None)
    read.add_argument(
        "--max-markdown-chars", type=int, default=DEFAULT_MAX_MARKDOWN_CHARS
    )
    read.add_argument(
        "--allow-private-addresses",
        action="store_true",
        help="operator/local-fixture wiring only: contact non-public "
        "addresses (the product path always enforces the default "
        "refuse-private policy)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: exit 0 read ran (JSON status distinguishes
    ok / refused / unavailable) / 1 structured rejection / 2 usage."""
    args = _build_parser().parse_args(argv)
    if args.command == "read":
        return asyncio.run(_read_async(args))
    return 2  # pragma: no cover - argparse `required=True` guards this


if __name__ == "__main__":  # pragma: no cover - argparse glue
    sys.exit(main())
