"""External MCP servers as scoped capability tools behind the enforced
dispatch boundary (contracts.md **C03 rules 3/6/7**, IDEA.md §13).

Card ``09-mcp``. "MCP is an integration protocol, not an authorization
boundary. Connecting a server does not imply enabling all its tools"
(IDEA.md §13); "MCP tools carry independent grants and server scope"
(contracts.md C03 rule 7). This module owns the *transport*: it connects
to one configured external MCP server (stdio or streamable HTTP — the
frozen v1 transports of the declared ``mcp`` dependency; the deprecated
SSE transports are deliberately not selected), discovers its tools,
maps **valid** tools onto the frozen C03 rule 1 descriptor vocabulary and
hands the *explicitly granted* subset to the caller's
:class:`~mai.plugins.registry.CapabilityRegistry`. Invocation is only
possible through the P03 capability dispatcher: the module's single
``call_tool`` site lives inside :attr:`McpServerRuntime.executor`, the
async callable wired into
:class:`~mai.capabilities.dispatch.CapabilityDispatcher` as its executor
— exactly the seam the native plugin runtime uses. There is no other
invocation path and no CLI (the dispatcher's no-CLI rule applies with
extra force here: a generic "call that MCP tool" entrypoint would be
precisely the unmediated backdoor the SERVICES route forbids).

What the bridge does — and deliberately does not
------------------------------------------------

- **Configuration** (C02/C03): an MCP server is a plugin configuration
  in the ``mai.plugins.config`` store namespace (that card's ownership
  decision): connection parameters live in the stored ``config``
  mapping, credential *reference names* in ``credential_refs`` (C03
  rule 6 — values never in configuration). :class:`McpServerConfig`
  re-validates the stored document against the frozen
  :data:`SERVER_CONFIG_SCHEMA` shape at composition (the store validated
  values at write time against the same schema; a reader re-checks
  instead of trusting). A disabled server refuses to compose.
- **Discovery is bounded and validates.** One bounded connection
  (initialize + ``tools/list``), at most ``max_tools`` tools examined,
  at most ``MAX_LIST_PAGES`` page requests within
  ``LIST_DEADLINE_SECONDS`` — an endless-cursor server (empty pages
  with a perpetual ``nextCursor``) or a slow-but-valid pager is a
  bounded, structured listing failure, never a hang; each tool is mapped
  through :func:`~mai.plugins.registry.parse_descriptor` — the frozen
  C03 rule 1 vocabulary owner — so malformed tools (wrong
  schema shape, oversized schema, unmappable name, name collisions)
  become value-free rejection records, never registrations.
- **Connecting ≠ enabling.** Only tools whose mapped capability id
  (``mcp.<plugin>.<tool>``) appears in the caller-supplied ``granted``
  set are registered. Ungranted tools are reported **by name** and
  stored nowhere: a dispatch for them denies
  ``capability_not_registered`` at the dispatcher's policy gate, and the
  agent tool bridge never sees a descriptor. Registration happens only
  at an explicit :meth:`McpServerRuntime.discover` (composition/
  recomposition); a reconnect never re-lists or registers tools, so a
  server cannot inject new capabilities mid-flight.
- **Prompt-like content does not bypass policy.** The bridge never calls
  ``prompts/list`` or ``resources/list`` — only tools exist here. Tool
  descriptions are dropped at mapping (the descriptor vocabulary has no
  description field; the model-facing tool description is generated from
  declarative fields only). Sampling, elicitation and roots are never
  declared in the client session, so a server's
  ``create_message``/``elicit`` requests are refused by the SDK's
  default callbacks. Tool *output* is untrusted data: it reaches the
  model only inside the dispatcher's redact-then-bound output boundary
  and the tool bridge's frozen :class:`~mai.agents.tools.ToolResult`
  envelope.
- **Bounded failure.** Every connection attempt is bounded by the
  configured timeouts; a failure before any request is sent answers a
  structured ``ok=False`` executor result (``connect_failed``), a
  failure *during* a tool call propagates — the dispatcher then records
  ``effect="unknown"`` because the outcome is unproven (host rule 3
  honesty). There is no automatic call retry: a mutate-class tool call
  whose outcome is unproven must never be replayed. Connections are
  opened per dispatch and torn down in the same task (no pooled session
  in v1 — pooling is a host-composition reconciliation, and sharing anyio
  cancel scopes across tasks is exactly the kind of teardown bug this
  design makes impossible). The *next* dispatch reconnects from scratch.
- **Subprocess credential env scoped (C03 rule 6).** A stdio server is
  spawned with an explicit environment: the SDK's platform-minimal
  safe-inherit set plus exactly the server's credential references,
  resolved from the configured ``.env`` source (discovery time) or by
  the dispatcher's credential seam (dispatch time — the values arrive in
  the :class:`~mai.capabilities.dispatch.CapabilityCall`). No other
  controller environment variable is inherited; credential references
  configured for *other* servers never enter this subprocess. Streamable
  HTTP servers are separately hosted: v1 injects no credentials into
  them at all (declaring ``credential_refs`` on an HTTP server is a
  configuration rejection — there is no honest injection point yet).

Value-free discipline: rejection details carry plugin names, tool names,
capability ids and stable codes — never schema document content, never
server-supplied text (descriptions, error strings, output), never
resolved credential material. Transport failures report the failure
*stage*, never the underlying message.

Thread safety: configuration and registry wiring are immutable after
construction; :meth:`McpServerRuntime.discover` runs once (an
undiscovered runtime must not look like an empty success, and a second
discovery would collide with the append-only registry). Dispatches are
independent bounded lifecycles and need no shared mutable state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from mai.capabilities.dispatch import CapabilityCall, ExecutorResult
from mai.credentials.env import EnvFileError, EnvResolver, SecretValue
from mai.plugins.config import (
    PluginConfigDocument,
    PluginConfigStore,
    parse_plugin_config_text,
)
from mai.plugins.registry import (
    CapabilityDescriptor,
    CapabilityError,
    CapabilityRegistry,
    parse_descriptor,
)

__all__ = [
    "DESCRIPTOR_VERSION",
    "DEFAULT_CALL_TIMEOUT_S",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_MAX_TOOLS",
    "MCP_CAPABILITY_PREFIX",
    "MCP_TOOL_EFFECT_CLASS",
    "MAX_ARGS",
    "MAX_ARG_CHARS",
    "MAX_COMMAND_CHARS",
    "MAX_CREDENTIAL_REFS",
    "MAX_LIST_PAGES",
    "MAX_MAX_TOOLS",
    "MAX_TIMEOUT_S",
    "MAX_TOOL_NAME_CHARS",
    "MAX_URL_CHARS",
    "LIST_DEADLINE_SECONDS",
    "SERVER_CONFIG_SCHEMA",
    "TRANSPORTS",
    "TRANSPORT_HTTP",
    "TRANSPORT_STDIO",
    "DiscoveryLimitExceeded",
    "McpBridgeError",
    "McpBridgeErrorCode",
    "McpDiscoveryReport",
    "McpServerConfig",
    "McpServerRuntime",
    "McpToolRejection",
    "capability_id_for",
    "granted_capability_ids",
    "server_plugins",
]

# ---------------------------------------------------------------------------
# Frozen transport/configuration vocabulary
# ---------------------------------------------------------------------------

#: Frozen v1 transports (declared ``mcp`` dependency decision): locally
#: spawned servers (``stdio``) and separately hosted servers
#: (``streamable_http``). The deprecated SSE transports are NOT selected.
TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "streamable_http"
TRANSPORTS = (TRANSPORT_STDIO, TRANSPORT_HTTP)

#: Capability-id namespace prefix for MCP-provided tools: the mapped
#: identifier is ``mcp.<plugin>.<tool>`` (C03 rule 1 slug shape).
MCP_CAPABILITY_PREFIX = "mcp"

#: MCP tools declare no effect class; the conservative default routes
#: every MCP tool through ``mutate`` — in ``ask`` mode each call prompts
#: for an approval decision (C03 rule 5), and no MCP tool can silently
#: claim the unattended ``read`` class. A per-tool class would be a
#: policy reconciliation, not a transport decision.
MCP_TOOL_EFFECT_CLASS = "mutate"

#: Descriptor version for every mapped tool. The MCP protocol has no
#: tool version; the descriptor line is pinned by this module (a schema
#: change on the server side surfaces at recomposition as a
#: ``incompatible_version`` registry rejection, not a silent swap).
DESCRIPTOR_VERSION = "1.0.0"

#: MCP tools execute controller-local (the subprocess or HTTP session
#: lives in the controller process; C03 rule 1 environment vocabulary).
MCP_TOOL_ENVIRONMENT = "controller"

DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_CALL_TIMEOUT_S = 120.0
MAX_TIMEOUT_S = 300.0

DEFAULT_MAX_TOOLS = 64
MAX_MAX_TOOLS = 1024

MAX_COMMAND_CHARS = 512
MAX_URL_CHARS = 2048
MAX_ARGS = 32
MAX_ARG_CHARS = 512
MAX_CWD_CHARS = 1024
MAX_CREDENTIAL_REFS = 16
MAX_TOOL_NAME_CHARS = 96

#: Upper bound on ``tools/list`` page requests (cursor round-trips) per
#: discovery. A hostile or broken server can keep a cursor alive
#: forever, and *empty* pages never reach the ``max_tools`` tool-count
#: bound — without this cap the discovery loop hangs. 128 pages at the
#: SDK's page size is orders of magnitude past any legitimate tool
#: surface (``max_tools`` caps at 1024).
MAX_LIST_PAGES = 128

#: Overall wall-clock budget for one discovery's whole paginated
#: ``tools/list`` phase. Complements ``MAX_LIST_PAGES``: it also bounds
#: servers that answer slowly but validly, where the per-request read
#: timeout never fires and pages accumulate at a trickle.
LIST_DEADLINE_SECONDS = 60.0

#: The JSON Schema a stored MCP server ``config`` must satisfy. Declared
#: here so the configuration writer (plugin API card) can validate with
#: the store's frozen keyword subset; :class:`McpServerConfig` enforces
#: the identical shape at composition. Field names deliberately carry no
#: credential-token substring (the store rejects those outright).
SERVER_CONFIG_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "transport": {"type": "string", "enum": list(TRANSPORTS)},
        "command": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_COMMAND_CHARS,
        },
        "args": {
            "type": "array",
            "items": {"type": "string", "maxLength": MAX_ARG_CHARS},
            "maxItems": MAX_ARGS,
        },
        "cwd": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CWD_CHARS,
        },
        "url": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_URL_CHARS,
        },
        "auth_header": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
        },
        "auth_scheme": {
            "type": "string",
            "maxLength": 64,
        },
        "connect_timeout_s": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": MAX_TIMEOUT_S,
        },
        "call_timeout_s": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": MAX_TIMEOUT_S,
        },
        "max_tools": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_MAX_TOOLS,
        },
    },
    "required": ["transport"],
    "additionalProperties": False,
}

_CONFIG_FIELDS = frozenset(SERVER_CONFIG_SCHEMA["properties"])  # type: ignore[arg-type]

#: Fields only valid for one transport (cross-field strictness the JSON
#: Schema subset cannot express; enforced by :class:`McpServerConfig`).
_TRANSPORT_FIELDS: dict[str, frozenset[str]] = {
    TRANSPORT_STDIO: frozenset({"command", "args", "cwd"}),
    TRANSPORT_HTTP: frozenset({"url", "auth_header", "auth_scheme"}),
}

_AUTH_HEADER_RE = re.compile(r"^[A-Za-z0-9-]+$")
_AUTH_SCHEME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_TOOL_NAME_FORBIDDEN = re.compile(r"[^a-z0-9_-]")


# ---------------------------------------------------------------------------
# Structured rejections
# ---------------------------------------------------------------------------


class McpBridgeErrorCode(StrEnum):
    """Stable machine-readable bridge rejection codes (C01 style)."""

    INVALID_CONFIG = "invalid_config"
    INVALID_WIRING = "invalid_wiring"
    ALREADY_DISCOVERED = "already_discovered"
    NOT_DISCOVERED = "not_discovered"
    CONNECT_FAILED = "connect_failed"
    CREDENTIAL_RESOLUTION_FAILED = "credential_resolution_failed"


class McpBridgeError(ValueError):
    """One structured, value-free bridge configuration/wiring/transport
    rejection.

    Raised for host misuse (bad wiring, composition errors, double
    discovery) and for bounded connection failures. Model-facing answers
    are never this exception — the executor converts pre-request
    connection failures into structured ``ok=False`` results and lets
    in-flight call failures propagate (the dispatcher records
    ``effect="unknown"``). Detail strings name stages and fields, never
    server-supplied text or resolved credential material.
    """

    def __init__(self, code: McpBridgeErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class DiscoveryLimitExceeded(Exception):
    """Internal signal: a discovery listing bound tripped (page count
    or wall-clock deadline). Never escapes the module —
    :meth:`McpServerRuntime.discover` normalizes it into a value-free
    :class:`McpBridgeError` (``connect_failed``), like every other
    bounded listing failure."""


@dataclass(frozen=True, slots=True)
class McpToolRejection:
    """One value-free per-tool discovery rejection.

    ``code`` is this module's tool-vocabulary (``tool_name_invalid``,
    ``tool_name_conflict``, ``tool_limit_exceeded``) or — for schema
    rejections — the registry's stable
    :class:`~mai.plugins.registry.CapabilityErrorCode` value (the
    dispatcher's decision-reason passthrough pattern).
    """

    tool: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class McpDiscoveryReport:
    """The value-free result of one bounded discovery run.

    ``registered`` are the capability ids actually registered (granted ∩
    discovered, sorted); ``ungranted`` are valid tools offered by the
    server but not in the granted set (names only, stored nowhere);
    ``stale_grants`` are granted ids the server does not currently offer
    (surfaced, never silently ignored); ``rejections`` are the malformed
    tools. ``tool_count`` is how many tools the server listed before the
    bound stopped examination.
    """

    plugin: str
    transport: str
    tool_count: int
    registered: tuple[str, ...]
    ungranted: tuple[str, ...]
    stale_grants: tuple[str, ...]
    rejections: tuple[McpToolRejection, ...]
    limit_exceeded: bool

    def to_json(self) -> dict[str, object]:
        """Frozen-shape JSON document (stable key order; audit view)."""
        return {
            "plugin": self.plugin,
            "transport": self.transport,
            "tool_count": self.tool_count,
            "registered": list(self.registered),
            "ungranted": list(self.ungranted),
            "stale_grants": list(self.stale_grants),
            "rejections": [
                {"tool": r.tool, "code": r.code, "detail": r.detail}
                for r in self.rejections
            ],
            "limit_exceeded": self.limit_exceeded,
        }

    def __repr__(self) -> str:
        return (
            f"<McpDiscoveryReport {self.plugin}/{self.transport} "
            f"tools={self.tool_count} registered={len(self.registered)} "
            f"rejected={len(self.rejections)}>"
        )


# ---------------------------------------------------------------------------
# Configuration (stored plugin config -> typed, re-validated config)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """Validated connection configuration of one MCP server plugin."""

    plugin: str
    transport: str
    credential_refs: tuple[str, ...]
    enabled: bool = True
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    url: str | None = None
    auth_header: str | None = None
    auth_scheme: str | None = None
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S
    max_tools: int = DEFAULT_MAX_TOOLS

    @classmethod
    def from_document(cls, document: object) -> McpServerConfig:
        """Validate one stored plugin configuration document.

        Accepts a :class:`~mai.plugins.config.PluginConfigDocument` (the
        store's parse output). The store validated the envelope and — at
        write time — the ``config`` values against
        :data:`SERVER_CONFIG_SCHEMA`; this reader re-checks the same
        frozen shape (a store written before a schema change must fail
        closed here, not connect somewhere unexpected).
        """
        if not isinstance(document, PluginConfigDocument):
            raise McpBridgeError(
                McpBridgeErrorCode.INVALID_WIRING,
                "config must be a PluginConfigDocument",
            )
        if not document.enabled:
            raise McpBridgeError(
                McpBridgeErrorCode.INVALID_CONFIG,
                f"plugin {document.plugin!r} is disabled; compose_runtime "
                "must not attach a disabled MCP server",
            )
        refs = document.credential_refs
        if len(set(refs)) != len(refs):
            raise McpBridgeError(
                McpBridgeErrorCode.INVALID_CONFIG,
                "credential_refs contains duplicates",
            )
        if len(refs) > MAX_CREDENTIAL_REFS:
            raise McpBridgeError(
                McpBridgeErrorCode.INVALID_CONFIG,
                f"credential_refs exceeds {MAX_CREDENTIAL_REFS} entries",
            )
        raw = document.config
        unknown = sorted(str(key) for key in raw if key not in _CONFIG_FIELDS)
        if unknown:
            raise McpBridgeError(
                McpBridgeErrorCode.INVALID_CONFIG,
                f"unknown config field {unknown[0]!r}",
            )
        transport = raw.get("transport")
        if transport not in TRANSPORTS:
            raise McpBridgeError(
                McpBridgeErrorCode.INVALID_CONFIG,
                "transport must be one of: " + ", ".join(TRANSPORTS),
            )
        assert isinstance(transport, str)
        foreign = sorted(_CONFIG_FIELDS & _TRANSPORT_FIELDS[
            TRANSPORT_HTTP if transport == TRANSPORT_STDIO else TRANSPORT_STDIO
        ] & set(raw))
        if foreign:
            raise McpBridgeError(
                McpBridgeErrorCode.INVALID_CONFIG,
                f"config field {foreign[0]!r} does not apply to "
                f"transport {transport!r}",
            )
        command = _opt_string(raw, "command", MAX_COMMAND_CHARS)
        cwd = _opt_string(raw, "cwd", MAX_CWD_CHARS)
        url = _opt_string(raw, "url", MAX_URL_CHARS)
        auth_header = _opt_auth_header(raw)
        auth_scheme = _opt_auth_scheme(raw, has_header=auth_header is not None)
        args = _opt_args(raw)
        if transport == TRANSPORT_STDIO:
            if command is None:
                raise McpBridgeError(
                    McpBridgeErrorCode.INVALID_CONFIG,
                    "stdio transport requires the command field",
                )
        else:
            if url is None:
                raise McpBridgeError(
                    McpBridgeErrorCode.INVALID_CONFIG,
                    "streamable_http transport requires the url field",
                )
            _check_http_url(url)
            if auth_header is None and refs:
                raise McpBridgeError(
                    McpBridgeErrorCode.INVALID_CONFIG,
                    "credential_refs on a streamable_http server requires "
                    "auth_header as the injection point; set auth_header or "
                    "remove the references",
                )
            if auth_header is not None and len(refs) != 1:
                raise McpBridgeError(
                    McpBridgeErrorCode.INVALID_CONFIG,
                    "auth_header on streamable_http requires exactly one "
                    "entry in credential_refs",
                )
        connect_timeout = _opt_timeout(raw, "connect_timeout_s")
        call_timeout = _opt_timeout(raw, "call_timeout_s")
        max_tools = _opt_int(raw, "max_tools")
        return cls(
            plugin=document.plugin,
            transport=transport,
            credential_refs=tuple(refs),
            enabled=True,
            command=command,
            args=args,
            cwd=cwd,
            url=url,
            auth_header=auth_header,
            auth_scheme=auth_scheme,
            connect_timeout_s=connect_timeout,
            call_timeout_s=call_timeout,
            max_tools=max_tools,
        )


def _opt_auth_header(raw: Mapping[str, object]) -> str | None:
    value = raw.get("auth_header")
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or not _AUTH_HEADER_RE.fullmatch(value)
    ):
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_CONFIG,
            "auth_header must be a valid HTTP header name (1..128 chars)",
        )
    return value


def _opt_auth_scheme(
    raw: Mapping[str, object], *, has_header: bool
) -> str | None:
    value = raw.get("auth_scheme")
    if value is None:
        return "Bearer" if has_header else None
    if not has_header:
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_CONFIG,
            "auth_scheme requires auth_header to be set",
        )
    if not isinstance(value, str) or len(value) > 64:
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_CONFIG,
            "auth_scheme must be a string up to 64 chars",
        )
    if value and not _AUTH_SCHEME_RE.fullmatch(value):
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_CONFIG,
            "auth_scheme must contain only [A-Za-z0-9._-] characters",
        )
    return value


def _opt_string(raw: Mapping[str, object], field: str, cap: int) -> str | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > cap:
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_CONFIG,
            f"{field} must be a non-empty string of at most {cap} characters",
        )
    return value


def _opt_args(raw: Mapping[str, object]) -> tuple[str, ...]:
    value = raw.get("args")
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_ARGS:
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_CONFIG,
            f"args must be a list of at most {MAX_ARGS} strings",
        )
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > MAX_ARG_CHARS
        ):
            raise McpBridgeError(
                McpBridgeErrorCode.INVALID_CONFIG,
                "args entries must be non-empty strings of at most "
                f"{MAX_ARG_CHARS} characters",
            )
    return tuple(value)


def _opt_timeout(raw: Mapping[str, object], field: str) -> float:
    value = raw.get(field)
    if value is None:
        return (
            DEFAULT_CONNECT_TIMEOUT_S
            if field == "connect_timeout_s"
            else DEFAULT_CALL_TIMEOUT_S
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_CONFIG,
            f"{field} must be a number (bool is not a number here)",
        )
    seconds = float(value)
    if not 0 < seconds <= MAX_TIMEOUT_S:
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_CONFIG,
            f"{field} must be in (0, {MAX_TIMEOUT_S}] seconds",
        )
    return seconds


def _opt_int(raw: Mapping[str, object], field: str) -> int:
    value = raw.get(field)
    if value is None:
        return DEFAULT_MAX_TOOLS
    if isinstance(value, bool) or not isinstance(value, int):
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_CONFIG,
            f"{field} must be an integer (bool is not an integer here)",
        )
    if not 1 <= value <= MAX_MAX_TOOLS:
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_CONFIG,
            f"{field} must be in [1, {MAX_MAX_TOOLS}]",
        )
    return int(value)


def _check_http_url(url: str) -> None:
    """HTTP(S) URL only, no userinfo (credentials never ride in URLs)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_CONFIG,
            "url must be an absolute http(s) URL",
        )
    if "@" in parts.netloc:
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_CONFIG,
            "url must not embed userinfo; credentials resolve by "
            "reference (C03 rule 6)",
        )


# ---------------------------------------------------------------------------
# Tool -> descriptor mapping (the C03 rule 1 vocabulary owner validates)
# ---------------------------------------------------------------------------


def capability_id_for(plugin: str, tool_name: str) -> str:
    """The deterministic capability id of one MCP tool (C03 rule 1 slug).

    ``mcp.<plugin>.<slug>`` where ``slug`` lowercases the wire tool name
    and maps every character outside ``[a-z0-9_-]`` to ``_``. Raises
    :class:`McpBridgeError` (``invalid_config``) when the result would
    overflow the identifier bound — the tool is then rejected at
    discovery, never silently truncated into a different identity.
    """
    slug = _TOOL_NAME_FORBIDDEN.sub("_", tool_name.lower())
    identifier = f"{MCP_CAPABILITY_PREFIX}.{plugin}.{slug}"
    if len(identifier) > 128:
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_CONFIG,
            f"mapped capability id for tool name overflows the 128 "
            f"character identifier bound ({len(identifier)})",
        )
    return identifier


def _map_tool(
    config: McpServerConfig, tool: types.Tool
) -> CapabilityDescriptor | McpToolRejection:
    """Map one discovered MCP tool onto a C03 rule 1 descriptor.

    Validation is delegated to :func:`~mai.plugins.registry.parse_descriptor`
    — the frozen vocabulary owner — so schema shape, size and JSON
    safety never get a second, weaker implementation here. The tool's
    ``description`` is deliberately dropped: the descriptor vocabulary
    has no description field, so server-supplied prose structurally
    cannot reach the model-facing tool surface.
    """
    name = tool.name
    if (
        not isinstance(name, str)
        or not name
        or len(name) > MAX_TOOL_NAME_CHARS
    ):
        return McpToolRejection(
            tool=str(name)[:MAX_TOOL_NAME_CHARS],
            code="tool_name_invalid",
            detail="tool name must be a non-empty string of at most "
            f"{MAX_TOOL_NAME_CHARS} characters",
        )
    try:
        identifier = capability_id_for(config.plugin, name)
    except McpBridgeError as error:
        return McpToolRejection(
            tool=name, code="tool_name_invalid", detail=error.detail
        )
    if not isinstance(tool.inputSchema, Mapping):
        return McpToolRejection(
            tool=name,
            code="invalid_schema",
            detail="input_schema must be a JSON object",
        )
    output_schema: object = tool.outputSchema
    if output_schema is not None and not isinstance(output_schema, Mapping):
        return McpToolRejection(
            tool=name,
            code="invalid_schema",
            detail="output_schema, when declared, must be a JSON object",
        )
    document: dict[str, object] = {
        "identifier": identifier,
        "version": DESCRIPTOR_VERSION,
        "config_schema": None,
        "input_schema": dict(tool.inputSchema),
        "output_schema": (
            {"type": "object"}
            if output_schema is None
            else dict(output_schema)
        ),
        "execution_environments": [MCP_TOOL_ENVIRONMENT],
        "effect_class": MCP_TOOL_EFFECT_CLASS,
        # Declared, lockable resource: this one server's tool surface
        # (required non-empty for the mutate class, C03 rule 1).
        "resource_scope": [f"{MCP_CAPABILITY_PREFIX}.{config.plugin}"],
        "required_grants": [identifier],
    }
    try:
        return parse_descriptor(document, source=f"mcp:{config.plugin}")
    except CapabilityError as error:
        return McpToolRejection(
            tool=name, code=error.code.value, detail=error.detail
        )


def _tool_output(result: types.CallToolResult) -> str:
    """Render one tool result as bounded-later textual output.

    Text blocks are joined verbatim (untrusted data — the dispatcher
    redacts and bounds them); non-text blocks are counted, never
    embedded; ``structuredContent`` is appended as canonical JSON when
    present. Server-supplied strings are never interpreted by this
    module.
    """
    lines: list[str] = []
    non_text = 0
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            lines.append(text)
        else:
            non_text += 1
    if non_text:
        lines.append(f"[{non_text} non-text content block(s) omitted]")
    if result.structuredContent is not None:
        lines.append(
            json.dumps(
                result.structuredContent,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        )
    return "\n".join(lines)


def _error_line(payload: dict[str, str]) -> str:
    """One-line value-free JSON envelope for pre-request failures."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------------------
# The runtime
# ---------------------------------------------------------------------------

#: Current ``.env`` credential source (constructed per call by the host;
#: the dispatcher's ``CredentialSource`` shape). Discovery-time spawns
#: resolve the server's references through it; dispatch-time values
#: arrive pre-resolved in the :class:`CapabilityCall`.
type CredentialSource = Callable[[], EnvResolver]


class McpServerRuntime:
    """One configured MCP server as registry descriptors plus an
    executor for the :class:`~mai.capabilities.dispatch.CapabilityDispatcher`.

    Composition is constructor-checked; :meth:`discover` runs exactly
    once (bounded, real transport) and freezes the tool bindings. The
    only invocation surface is :attr:`executor`; every connection is
    opened and torn down inside the single dispatch task that uses it.
    """

    def __init__(
        self,
        *,
        config: McpServerConfig,
        registry: CapabilityRegistry,
        credentials: CredentialSource | None = None,
    ) -> None:
        if not isinstance(config, McpServerConfig):
            raise McpBridgeError(
                McpBridgeErrorCode.INVALID_WIRING,
                "config must be an McpServerConfig",
            )
        if not isinstance(registry, CapabilityRegistry):
            raise McpBridgeError(
                McpBridgeErrorCode.INVALID_WIRING,
                "registry must be a CapabilityRegistry",
            )
        if config.transport == TRANSPORT_STDIO and config.credential_refs:
            if not callable(credentials):
                raise McpBridgeError(
                    McpBridgeErrorCode.INVALID_WIRING,
                    "a stdio server with credential_refs requires a "
                    "credentials source callable (discovery-time spawn "
                    "env, C03 rule 6)",
                )
        elif credentials is not None:
            raise McpBridgeError(
                McpBridgeErrorCode.INVALID_WIRING,
                "credentials source applies to stdio servers only in "
                "this version",
            )
        self._config = config
        self._registry = registry
        self._credentials = credentials
        self._discovered = False
        self._report: McpDiscoveryReport | None = None
        self._tools: dict[str, str] = {}

    def __repr__(self) -> str:
        return (
            f"<McpServerRuntime {self._config.plugin}/{self._config.transport} "
            f"discovered={self._discovered} tools={len(self._tools)}>"
        )

    # -- composition surface -------------------------------------------------

    @property
    def plugin(self) -> str:
        """The server's plugin (configuration) name."""
        return self._config.plugin

    @property
    def transport(self) -> str:
        """The frozen transport name."""
        return self._config.transport

    @property
    def credential_references(self) -> tuple[str, ...]:
        """The server's credential reference *names* (C03 rule 2).

        The host passes these into the ``ExecutionContext``; values
        resolve only in the dispatcher's credential seam (dispatch) or
        from the configured source (discovery spawn).
        """
        return self._config.credential_refs

    @property
    def discovered(self) -> bool:
        """Whether :meth:`discover` has completed."""
        return self._discovered

    @property
    def discovery_report(self) -> McpDiscoveryReport:
        """The one discovery report (requires discovery, fail-closed)."""
        self._require_discovered()
        assert self._report is not None
        return self._report

    @property
    def tools(self) -> tuple[str, ...]:
        """Registered capability ids (sorted) — the granted tool surface."""
        self._require_discovered()
        return tuple(sorted(self._tools))

    def _require_discovered(self) -> None:
        if not self._discovered:
            raise McpBridgeError(
                McpBridgeErrorCode.NOT_DISCOVERED,
                "runtime has not discovered tools yet; call discover() "
                "first (an undiscovered runtime must not look like an "
                "empty success)",
            )

    # -- discovery -----------------------------------------------------------

    async def discover(
        self, *, granted: Collection[str]
    ) -> McpDiscoveryReport:
        """Connect once, discover tools, register the granted subset.

        ``granted`` is the explicitly granted capability-id set for this
        server (the host reads it from the plugin configuration store's
        grant bindings — installing or connecting never grants,
        SERVICES decision-dependency row). Connection and listing are
        bounded by the configured timeouts, ``max_tools``,
        ``MAX_LIST_PAGES`` and ``LIST_DEADLINE_SECONDS``. Malformed
        tools become per-tool rejection records; a malformed tool can
        never prevent the remaining discovery.
        """
        if self._discovered:
            raise McpBridgeError(
                McpBridgeErrorCode.ALREADY_DISCOVERED,
                "runtime already discovered tools; recomposition creates "
                "a fresh runtime (the registry is append-only)",
            )
        if isinstance(granted, str) or not isinstance(granted, Collection):
            raise McpBridgeError(
                McpBridgeErrorCode.INVALID_WIRING,
                "granted must be a collection of capability-id strings",
            )
        granted_ids = frozenset(granted)
        config = self._config

        # Session and teardown are task-local: discovery, listing and
        # close all run in the caller's task (no cross-task cancel-scope
        # exits, no pooled state).
        session, stack = await self._connect(self._discovery_credentials())
        try:
            listed: list[types.Tool] = []
            cursor: str | None = None
            limit_exceeded = False
            pages = 0
            listing_deadline = time.monotonic() + LIST_DEADLINE_SECONDS
            try:
                while True:
                    pages += 1
                    if pages > MAX_LIST_PAGES:
                        raise DiscoveryLimitExceeded(
                            f"tools/list exceeded the "
                            f"{MAX_LIST_PAGES}-page bound"
                        )
                    if time.monotonic() >= listing_deadline:
                        raise DiscoveryLimitExceeded(
                            f"tools/list exceeded the "
                            f"{LIST_DEADLINE_SECONDS}s listing deadline"
                        )
                    result = await session.list_tools(cursor=cursor)
                    for tool in result.tools:
                        if len(listed) >= config.max_tools:
                            limit_exceeded = True
                            break
                        listed.append(tool)
                    if limit_exceeded or result.nextCursor is None:
                        break
                    cursor = result.nextCursor
            except Exception as error:
                # Bounded listing failure (timeout, protocol error, or a
                # listing bound: an endless-cursor server answering empty
                # pages forever never reaches the max_tools count, and a
                # slow-but-valid pager never trips the read timeout). The
                # tool surface is unknown, so nothing registers — fail
                # the discovery honestly, value-free.
                if isinstance(error, DiscoveryLimitExceeded):
                    raise McpBridgeError(
                        McpBridgeErrorCode.CONNECT_FAILED,
                        f"{config.transport} tool listing failed ({error})",
                    ) from None
                raise McpBridgeError(
                    McpBridgeErrorCode.CONNECT_FAILED,
                    f"{config.transport} tool listing failed "
                    f"({type(error).__name__})",
                ) from None
        finally:
            await stack.aclose()

        registered: dict[str, CapabilityDescriptor] = {}
        tool_names: dict[str, str] = {}
        rejections: list[McpToolRejection] = []
        ungranted: list[str] = []
        for tool in listed:
            mapped = _map_tool(config, tool)
            if isinstance(mapped, McpToolRejection):
                rejections.append(mapped)
                continue
            identifier = mapped.identifier
            if identifier not in granted_ids:
                ungranted.append(tool.name)
                continue
            if identifier in registered:
                rejections.append(
                    McpToolRejection(
                        tool=tool.name,
                        code="tool_name_conflict",
                        detail="maps to an already-bound capability id",
                    )
                )
                continue
            registered[identifier] = mapped
            tool_names[identifier] = tool.name
        # Deterministic registration order; a registry rejection (e.g.
        # duplicate with another composition) aborts discovery honestly —
        # a partially registered surface is not silently accepted.
        for identifier, descriptor in sorted(
            registered.items(), key=lambda item: item[0]
        ):
            self._registry.register(descriptor)
        stale = tuple(sorted(granted_ids - set(registered)))
        report = McpDiscoveryReport(
            plugin=config.plugin,
            transport=config.transport,
            tool_count=len(listed),
            registered=tuple(sorted(registered)),
            ungranted=tuple(sorted(ungranted)),
            stale_grants=stale,
            rejections=tuple(rejections),
            limit_exceeded=limit_exceeded,
        )
        self._discovered = True
        self._report = report
        self._tools = tool_names
        return report

    def _discovery_credentials(self) -> dict[str, str]:
        """Resolve the server's references for the discovery spawn env."""
        refs = self._config.credential_refs
        if not refs:
            return {}
        assert self._credentials is not None  # constructor-checked
        try:
            resolver = self._credentials()
        except Exception as error:
            raise McpBridgeError(
                McpBridgeErrorCode.CREDENTIAL_RESOLUTION_FAILED,
                f"credential source unavailable ({type(error).__name__})",
            ) from None
        values: dict[str, str] = {}
        for reference in refs:
            try:
                secret: SecretValue = resolver.resolve(reference)
            except EnvFileError as error:
                raise McpBridgeError(
                    McpBridgeErrorCode.CREDENTIAL_RESOLUTION_FAILED,
                    f"reference {reference}: {error.code.value}",
                ) from None
            values[reference] = secret.value
        return values

    # -- the one execution seam ----------------------------------------------

    @property
    def executor(self) -> Callable[[CapabilityCall], Awaitable[ExecutorResult]]:
        """The dispatcher-facing executor (C03 rule 7).

        Wire into ``CapabilityDispatcher(executor=...)``. Routes an
        allowed, rechecked dispatch to this server over one bounded
        connection. A capability without a binding on this server
        (registered by another composition, or a probe) answers a
        structured ``ok=False`` instead of executing. A connection
        failure *before any request* is sent answers ``ok=False``
        (``connect_failed``) — nothing external happened. A failure
        *during* a tool call propagates: the dispatcher records
        ``effect="unknown"`` because the outcome is unproven, and the
        next dispatch reconnects from scratch (no automatic retry — an
        unproven mutate-class call is never replayed).
        """
        self._require_discovered()
        config = self._config
        tools = self._tools

        async def execute_mcp(call: CapabilityCall) -> ExecutorResult:
            tool_name = tools.get(call.descriptor.identifier)
            if tool_name is None:
                return ExecutorResult(
                    ok=False,
                    output=_error_line(
                        {
                            "error": "capability_not_bound",
                            "capability_id": call.descriptor.identifier,
                            "plugin": config.plugin,
                        }
                    ),
                )
            values: dict[str, str] = {}
            if config.credential_refs:
                missing = [
                    reference
                    for reference in config.credential_refs
                    if reference not in call.credentials
                ]
                if missing:
                    return ExecutorResult(
                        ok=False,
                        output=_error_line(
                            {
                                "error": "credential_ref_unresolved",
                                "ref": missing[0],
                                "plugin": config.plugin,
                            }
                        ),
                    )
                values = {
                    reference: call.credentials[reference].value
                    for reference in config.credential_refs
                }
            try:
                session, stack = await self._connect(values)
            except McpBridgeError:
                # Bounded, pre-request: no MCP message left this process.
                return ExecutorResult(
                    ok=False,
                    output=_error_line(
                        {
                            "error": "connect_failed",
                            "transport": config.transport,
                            "plugin": config.plugin,
                        }
                    ),
                )
            try:
                result = await session.call_tool(
                    tool_name, arguments=dict(call.payload)
                )
            finally:
                await stack.aclose()
            return ExecutorResult(
                ok=not bool(result.isError), output=_tool_output(result)
            )

        return execute_mcp

    # -- bounded connection lifecycle (task-local) ---------------------------

    async def _connect(
        self, credential_values: Mapping[str, str]
    ) -> tuple[ClientSession, contextlib.AsyncExitStack]:
        """Open one bounded client session; caller closes the stack.

        Every step is bounded by ``connect_timeout_s``; any failure is
        normalized to :class:`McpBridgeError` (``connect_failed``) with
        the *stage* named — never the underlying transport message
        (server-supplied text must not enter controller error paths).
        """
        config = self._config
        stack = contextlib.AsyncExitStack()
        stage = "spawn"
        try:
            if config.transport == TRANSPORT_STDIO:
                parameters = StdioServerParameters(
                    command=config.command,
                    args=list(config.args),
                    cwd=config.cwd,
                    env=_scoped_environment(
                        config.credential_refs, credential_values
                    ),
                )
                read, write = await self._bounded(
                    stack.enter_async_context(stdio_client(parameters))
                )
            else:
                http_headers = _scoped_http_headers(
                    config, credential_values
                )
                read, write, _session_id = await self._bounded(
                    stack.enter_async_context(
                        streamablehttp_client(
                            config.url,
                            headers=http_headers,
                            timeout=config.connect_timeout_s,
                            sse_read_timeout=config.call_timeout_s,
                        )
                    )
                )
            stage = "session"
            session = await self._bounded(
                stack.enter_async_context(
                    ClientSession(
                        read,
                        write,
                        # No sampling/elicitation/roots callbacks: the
                        # client declares none of these capabilities, so
                        # a server's create_message/elicit request is
                        # refused by the SDK's default error callbacks.
                        read_timeout_seconds=timedelta(
                            seconds=config.call_timeout_s
                        ),
                    )
                )
            )
            stage = "initialize"
            await self._bounded(session.initialize())
        except BaseException as error:
            with contextlib.suppress(Exception):
                await stack.aclose()
            if isinstance(error, McpBridgeError):
                raise
            raise McpBridgeError(
                McpBridgeErrorCode.CONNECT_FAILED,
                f"{config.transport} connection failed at {stage} "
                f"({type(error).__name__})",
            ) from None
        return session, stack

    async def _bounded(self, awaitable: Awaitable) -> object:
        """One connect step under the bounded timeout (fail → TimeoutError)."""
        return await asyncio.wait_for(
            awaitable, timeout=self._config.connect_timeout_s
        )


def _scoped_http_headers(
    config: McpServerConfig, values: Mapping[str, str]
) -> dict[str, str] | None:
    """Build scoped HTTP headers for streamable_http when auth_header is configured."""
    if config.auth_header is None or not config.credential_refs:
        return None
    reference = config.credential_refs[0]
    raw_secret = values.get(reference)
    if raw_secret is None:
        raise McpBridgeError(
            McpBridgeErrorCode.CREDENTIAL_RESOLUTION_FAILED,
            f"reference {reference} has no resolved value",
        )
    if "\r" in raw_secret or "\n" in raw_secret:
        raise McpBridgeError(
            McpBridgeErrorCode.CREDENTIAL_RESOLUTION_FAILED,
            f"reference {reference} contains invalid header control characters",
        )
    header_val = (
        f"{config.auth_scheme} {raw_secret}"
        if config.auth_scheme
        else raw_secret
    )
    return {config.auth_header: header_val}


def _scoped_environment(
    refs: tuple[str, ...], values: Mapping[str, str]
) -> dict[str, str]:
    """The scoped stdio subprocess environment (C03 rule 6).

    The SDK's platform-minimal safe-inherit set (``HOME``, ``PATH``, …)
    plus **exactly** this server's credential references. Nothing else
    from the controller environment is inherited, and references
    configured for other servers cannot leak in (``values`` is built
    from ``refs`` only).
    """
    environment = get_default_environment()
    for reference in refs:
        value = values.get(reference)
        if value is None:
            # Fail closed: a partially resolved credential set must not
            # spawn a half-authenticated server. Callers pre-check; this
            # guards the seam itself.
            raise McpBridgeError(
                McpBridgeErrorCode.CREDENTIAL_RESOLUTION_FAILED,
                f"reference {reference} has no resolved value",
            )
        environment[reference] = value
    return environment


# ---------------------------------------------------------------------------
# Store composition helpers (the worker-restart assembly surface)
# ---------------------------------------------------------------------------


def server_plugins(store: PluginConfigStore) -> tuple[McpServerConfig, ...]:
    """MCP server configurations stored in the plugin store, name-sorted.

    Convention (shared with the plugin configuration store's MCP note):
    a stored plugin config whose ``config`` mapping carries a
    ``transport`` field is an MCP server connection — native plugin
    configs never declare one. Such documents are validated strictly by
    :meth:`McpServerConfig.from_document`; an invalid MCP document raises
    rather than silently skipping a configured server (a broken MCP
    configuration must be surfaced, never ignored). Disabled servers
    raise the same way: composition must never attach them.
    """
    if not isinstance(store, PluginConfigStore):
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_WIRING,
            "store must be a PluginConfigStore",
        )
    configs: list[McpServerConfig] = []
    for plugin in store.list_plugins():
        document = parse_plugin_config_text(store.read_config(plugin).text)
        if "transport" not in document.config:
            continue
        configs.append(McpServerConfig.from_document(document))
    return tuple(configs)


def granted_capability_ids(
    store: PluginConfigStore, plugin: str
) -> tuple[str, ...]:
    """The explicit grant set for one MCP server plugin: the union of its
    stored grant bindings (sorted, unique).

    This is the ``granted`` input of :meth:`McpServerRuntime.discover` —
    connecting or discovering never grants (SERVICES decision-dependency
    row: no installation-time auto-grant); only these admin-written
    binding documents do. Capability ids bound to a *different* plugin
    are the store's data-integrity concern; here they simply cannot match
    a discovered tool and surface as stale grants.
    """
    if not isinstance(store, PluginConfigStore):
        raise McpBridgeError(
            McpBridgeErrorCode.INVALID_WIRING,
            "store must be a PluginConfigStore",
        )
    granted: set[str] = set()
    for document in store.list_bindings(plugin):
        granted.update(document.capabilities)
    return tuple(sorted(granted))
