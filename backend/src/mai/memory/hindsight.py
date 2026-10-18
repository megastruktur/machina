"""Bank-scoped operational memory (retain/recall/reflect) over bundled Hindsight.

Card ``11-memory-adapter`` (service ``memory``), against contracts.md **C07**
rules 4/6 (provenance-preserving results; Hindsight is never a source of docs
or live facts and the server bank/scope is enforced HERE, in the adapter),
**C03** rules 2/6 (the execution context is the only target vocabulary;
credentials resolve from ``.env`` — this module handles no secret values) and
**C06** rules 2/3 (memory enters context only as explicitly sourced historical
material, bounded with truncation notices). Consumes the reviewed
prerequisites ``11-hindsight-compose`` (the internal-only, profile-gated
``hindsight`` service, API 8888) and ``11-dependencies`` (the memory lane
speaks Hindsight's bank-scoped HTTP API through the already-pinned ``httpx2``
client; every in-process Hindsight distribution stays out of the controller).
Request/response shapes are pinned against the upstream **1.0.0** OpenAPI
specification (``hindsight-docs/static/openapi.json`` at tag ``v1.0.0``) —
the image the Compose card froze at integration.

- **Bank naming scheme (C07 rule 6 — frozen by this module).** Hindsight
  banks group memories; MAI maps server scoping onto them:
  every bank this adapter addresses is ``mai-server-<server_id>`` (the
  canonical lowercase UUID string of the ``ExecutionContext.server_id``) or
  ``mai-shared-<name>`` where ``<name>`` is an explicitly configured shared
  bank name (``[a-z0-9](-[a-z0-9]+)*``, at most 64 chars) wired into the
  adapter at construction. There is no third shape: a server bank can never
  collide with a shared bank (disjoint prefixes), and a payload can never
  name a bank — the scope vocabulary is ``"server"`` (resolves to the
  execution context's server, C03 rule 2) or ``"shared:<name>"`` (resolves
  through the configured allowlist only; an unlisted name is a
  ``bank_forbidden`` rejection, and the payload schema rejects smuggled
  ``bank``/``server_id`` fields outright). Cross-server recall is therefore
  unrepresentable in the payload and unobservable through the adapter.

- **Bounded operations (acceptance 1).** Retain takes at most 8 items of at
  most 4000 characters each (plus an optional bounded context line and
  document id) and always retains **synchronously** (``"async": false``):
  the 200 document ``{success, bank_id, items_count, …}`` is the actual
  receipt; an HTTP failure or a ``success: false`` document is returned as
  ``status="unavailable"`` — a failed write is never reported as success
  (KB-H2). Recall/reflect answers are token-bounded upstream
  (``max_tokens``, default 1024, hard cap 4096) and additionally bounded
  here: at most 16 hits are parsed (``results_truncated`` flag beyond that),
  each hit's text is excerpted at the per-hit character budget with a
  ``truncated`` flag, and reflect answers are character-budgeted the same
  way. Every outbound payload is a strict subset of the pinned upstream
  schema — unknown fields are rejected before any I/O.

- **Graceful degradation (acceptance 1).** The adapter is constructible
  without a provider (Hindsight profile not enabled / not wired): every
  operation then answers ``status="unavailable"``, ``code="memory_disabled"``
  without any I/O — never an import failure, never a crash, never a fake
  empty success. Against a wired provider, a timeout is
  ``provider_timeout``, a transport/connect failure is
  ``provider_unreachable``, an HTTP error status or a malformed document is
  ``provider_error``; unavailable outcomes name only the seam code and the
  exception *type*. Service outage/retry is caller policy: the adapter makes
  one bounded attempt per call and reports honestly (single-flight, no
  hidden retry loop).

- **Secrets excluded (acceptance 2).** Every outbound text — retain item
  content, retain context, recall/reflect queries — passes the secret-marker
  screen (same frozen classes as ``mai.research.search``: credential
  assignments, PEM blocks, JWTs, cloud access keys, bearer tokens) *before
  any network I/O*; a match refuses the operation (``sensitive_content``)
  naming only the screen class and the item position. Persisting a
  secret-shaped value into the shared memory bank (where later contexts
  would recall it) or into the service's audit/log surface is exactly the
  leak this screen prevents. The provider itself attaches no credential
  material: the bundled service is Compose-internal with no API auth, and
  C03 rule 6 keeps secret values out of this module entirely (the
  descriptor's payload schema cannot carry them either).

- **Provenance-preserving citations (C07 rule 4).** Every recalled hit
  carries its ``bank``, memory ``id``, fact ``type``, the freshness tuple
  (``occurred_start``/``occurred_end``/``mentioned_at`` as upstream recorded
  them), the upstream ``document_id`` when present, the bounded entity/tag
  lists, the upstream ranking score, and a rendered
  ``memory:<bank>/<id>`` citation. Reflect answers carry the bounded ids of
  the facts they were ``based_on``. Results are labelled
  ``"trust": "operational_memory"``, ``"role": "historical_context"``:
  retained memory is historical context, never a source of live facts or
  documents (C07 rule 6) — the consumer decides freshness; the adapter
  makes the provenance of every claim traceable and bounded.

- **Delete/export scope (acceptance 2).** Deletion is scoped exactly like
  the reads: ``delete_memories`` (optionally filtered by fact type) and
  ``delete_bank`` (the whole bank — every memory of that server or shared
  scope) address only the bank the scope resolves to, and return the
  upstream ``{success, deleted_count}`` receipt. ``export_bank`` returns
  the upstream bank manifest (the import/export pair the ADR §4.3 backup
  rule leans on; the scheduled backup remains the Postgres ``hindsight``
  database dump — this export is an explicit, per-bank operational tool,
  not a silent rebuild path).

Effect classes: ``memory.recall``/``memory.reflect`` are ``read`` (no
observable state change; every grant/scope/policy gate still applies ahead
of these executors), ``memory.retain`` is ``mutate`` (a bank write; the
policy layer treats it per its mode rules). Deletion is deliberately NOT a
dispatcher capability — bank deletion is an operator action through this
module's entrypoint, never a model-invocable tool. The network seam is the
injected provider alone (the httpx2 transport stays a constructor/test seam
exactly like ``07-search-capability``'s provider); the base URL is host
wiring, never payload or environment inside this module.

Production entrypoint (exit contract: 0 operation ran — the JSON status
distinguishes ``ok`` / ``unavailable``; 1 structured rejection; 2 usage
error)::

    uv run python -m mai.memory.hindsight recall --base-url URL \
        --scope server --query TEXT [--types world,experience] \
        [--budget low] [--max-tokens N] [--max-text-chars N]
    uv run python -m mai.memory.hindsight retain --base-url URL \
        --scope shared:ops --item CONTENT [--item CONTENT …]
    uv run python -m mai.memory.hindsight reflect --base-url URL \
        --scope server --query TEXT [--max-tokens N]
    uv run python -m mai.memory.hindsight status --base-url URL
    uv run python -m mai.memory.hindsight delete-memories --base-url URL \
        --scope server [--type world]
    uv run python -m mai.memory.hindsight delete-bank --base-url URL \
        --scope shared:scratch --yes
    uv run python -m mai.memory.hindsight export --base-url URL --scope server
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
from mai.research.search import sensitive_marker_reason

__all__ = [
    "DEFAULT_MAX_TEXT_CHARS",
    "DEFAULT_REFLECT_CHARS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TOKEN_BUDGET",
    "MAX_BUDGET_TOKENS",
    "MAX_CONTENT_CHARS",
    "MAX_CONTEXT_CHARS",
    "MAX_DOCUMENT_ID_CHARS",
    "MAX_ITEMS",
    "MAX_QUERY_CHARS",
    "MAX_RESULTS",
    "MAX_SHARED_BANKS",
    "MAX_TEXT_CHARS",
    "MAX_TIMEOUT_SECONDS",
    "MemoryAdapterError",
    "MemoryErrorCode",
    "MemoryHit",
    "MemoryProvider",
    "MemoryRecallCapability",
    "MemoryReflectCapability",
    "MemoryRetainCapability",
    "MemoryStatus",
    "RecallOutcome",
    "RecallRequest",
    "RECALL_CAPABILITY_ID",
    "ReflectOutcome",
    "REFLECT_CAPABILITY_ID",
    "RetainOutcome",
    "RetainReceipt",
    "RetainRequest",
    "RETAIN_CAPABILITY_ID",
    "SERVER_BANK_PREFIX",
    "SHARED_BANK_NAME_RE",
    "SHARED_BANK_PREFIX",
    "HindsightProvider",
    "VALID_BUDGETS",
    "VALID_FACT_TYPES",
    "bank_for_scope",
    "parse_shared_banks",
    "recall_descriptor",
    "recall_document",
    "reflect_descriptor",
    "shared_bank_id",
    "server_bank_id",
]

# ---------------------------------------------------------------------------
# Capability identity and frozen bounds
# ---------------------------------------------------------------------------

#: Capability slugs this module owns (registry identifier shape; C03 rule 1).
RECALL_CAPABILITY_ID = "memory.recall"
RETAIN_CAPABILITY_ID = "memory.retain"
REFLECT_CAPABILITY_ID = "memory.reflect"

#: Query text bound for recall/reflect (same discipline as web search: a
#: short phrase, bounded work for the upstream instance).
MAX_QUERY_CHARS = 500

#: Retain bounds: at most 8 items per call, each at most 4000 characters —
#: a retain is an episodic note for extraction, never a document dump (the
#: document lane owns documents; C07 rule 6).
MAX_ITEMS = 8
MAX_CONTENT_CHARS = 4000
MAX_CONTEXT_CHARS = 500
MAX_DOCUMENT_ID_CHARS = 128

#: Per-hit excerpt budget for recall text (C06 rule 3): a cut excerpt keeps
#: every provenance field and is flagged ``truncated``.
DEFAULT_MAX_TEXT_CHARS = 600
MIN_TEXT_CHARS = 100
MAX_TEXT_CHARS = 1000

#: Upstream token budget for recall/reflect answers (bounded context, C06
#: rule 3): MAI defaults below the upstream default of 4096 and caps there.
DEFAULT_TOKEN_BUDGET = 1024
MIN_TOKEN_BUDGET = 256
MAX_BUDGET_TOKENS = 4096

#: Reflect answer character budget (the upstream ``text`` field excerpted).
DEFAULT_REFLECT_CHARS = 1000
MIN_REFLECT_CHARS = 100
MAX_REFLECT_CHARS = 2000

#: Hard cap on parsed recall hits; beyond it the outcome is flagged
#: ``results_truncated`` (upstream is already token-bounded; this bounds a
#: pathological response that ignores the budget).
MAX_RESULTS = 16

#: Per-hit bounded metadata: entities and tags are provenance, not payload.
MAX_ENTITIES_PER_HIT = 16
MAX_ENTITY_CHARS = 128
MAX_TAGS_PER_HIT = 16
MAX_TAG_CHARS = 64

#: Provider request budget: retain waits for a synchronous LLM extraction
#: round trip, so the default settles that honestly; the cap bounds the
#: worst case a host may construct.
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_TIMEOUT_SECONDS = 300.0

#: The descriptive, non-identifying outbound User-Agent (internal compose
#: network traffic; names the capability lane, never the host).
USER_AGENT = "mai-memory-adapter/1.0 (internal operational memory)"

#: The provider provenance label of this capability's production backend
#: (the bundled Hindsight instance; C07 rule 7). It names the channel,
#: never an instance address.
PROVIDER_LABEL = "hindsight"

#: Trust/role labels of every recall/reflect document (C07 rule 6: memory is
#: historical operational context, never a source of live facts or docs).
TRUST_LABEL = "operational_memory"
ROLE_LABEL = "historical_context"

VALID_FACT_TYPES = ("world", "experience", "observation")
VALID_BUDGETS = ("low", "mid", "high")

# ---------------------------------------------------------------------------
# Bank naming scheme (C07 rule 6 — frozen by this module, see docstring)
# ---------------------------------------------------------------------------

#: Server bank prefix: ``mai-server-<canonical lowercase uuid4 string>``.
SERVER_BANK_PREFIX = "mai-server-"

#: Shared bank prefix: ``mai-shared-<name>``, ``name`` matched by
#: ``SHARED_BANK_NAME_RE`` and allowed only from the configured allowlist.
SHARED_BANK_PREFIX = "mai-shared-"

#: A shared bank name: lowercase digits/letters, hyphen-separated, 1..64.
SHARED_BANK_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

MAX_SHARED_NAME_CHARS = 64
MAX_SHARED_BANKS = 32
#: Longest bank id this adapter can produce (shared shape).
MAX_BANK_ID_CHARS = len(SHARED_BANK_PREFIX) + MAX_SHARED_NAME_CHARS

#: Payload scope vocabulary: ``"server"`` or ``"shared:<name>"``.
SERVER_SCOPE = "server"
SHARED_SCOPE_PREFIX = "shared:"


class MemoryStatus(StrEnum):
    """The honest answer of one memory operation."""

    OK = "ok"
    UNAVAILABLE = "unavailable"


class MemoryErrorCode(StrEnum):
    """Stable machine-readable memory rejection/outage codes."""

    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNREACHABLE = "provider_unreachable"
    PROVIDER_ERROR = "provider_error"
    MEMORY_DISABLED = "memory_disabled"
    BANK_FORBIDDEN = "bank_forbidden"
    SENSITIVE_CONTENT = "sensitive_content"
    INVALID_PAYLOAD = "invalid_payload"


class MemoryAdapterError(ValueError):
    """One structured, value-free payload/scope rejection.

    Raised only for malformed *direct* calls, forbidden scopes and
    secret-shaped outbound text; the ``06-tool-bridge`` schema gate denies
    malformed shapes before dispatch, so the dispatcher's normal path never
    reaches the payload rejects. A sensitive outbound text raises before any
    provider I/O — nothing leaves the process.
    """

    def __init__(self, code: MemoryErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def _reject(code: MemoryErrorCode, detail: str) -> MemoryAdapterError:
    return MemoryAdapterError(code, detail)


# ---------------------------------------------------------------------------
# Scope → bank resolution (the adapter-side enforcement of C07 rule 6)
# ---------------------------------------------------------------------------


def server_bank_id(context: ExecutionContext) -> str:
    """The server-scoped bank id for one execution context (pure)."""
    return SERVER_BANK_PREFIX + str(context.server_id)


def shared_bank_id(name: str) -> str:
    """The shared bank id for one explicitly configured shared name."""
    if (
        not isinstance(name, str)
        or not name
        or len(name) > MAX_SHARED_NAME_CHARS
        or not SHARED_BANK_NAME_RE.fullmatch(name)
    ):
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            f"shared bank name must match [a-z0-9-] "
            f"(1..{MAX_SHARED_NAME_CHARS} chars), got an invalid name",
        )
    return SHARED_BANK_PREFIX + name


def parse_shared_banks(names: Sequence[str] | None) -> tuple[str, ...]:
    """Validate constructor wiring: the explicit shared-bank allowlist.

    Empty/absent means the deployment exposes no shared banks (every
    ``shared:…`` scope is then ``bank_forbidden``). Duplicates are
    rejected — the allowlist is a set of deliberate names.
    """
    if names is None:
        return ()
    if not isinstance(names, Sequence) or isinstance(names, (str, bytes)):
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            "shared bank names must be a sequence of strings",
        )
    seen: dict[str, None] = {}
    for name in names:
        shared_bank_id(name)  # validates the name shape
        if name in seen:
            raise _reject(
                MemoryErrorCode.INVALID_PAYLOAD,
                f"duplicate shared bank name: {name}",
            )
        seen[name] = None
    if len(seen) > MAX_SHARED_BANKS:
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            f"at most {MAX_SHARED_BANKS} shared banks may be configured",
        )
    return tuple(seen.keys())


def bank_for_scope(
    scope: object,
    context: ExecutionContext,
    *,
    shared_banks: Sequence[str] = (),
) -> str:
    """Resolve one payload scope to its bank id (pure; value-free).

    ``"server"`` resolves to the execution context's server bank — the
    context is the only target vocabulary (C03 rule 2), so no payload can
    ever address another server's bank. ``"shared:<name>"`` resolves only
    through the configured allowlist; an unlisted name is
    ``bank_forbidden`` (the name itself is caller-supplied and safe to
    echo; nothing about the allowlist's other members is revealed).
    """
    if not isinstance(scope, str) or not scope:
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            'scope must be "server" or "shared:<name>"',
        )
    if scope == SERVER_SCOPE:
        return server_bank_id(context)
    if scope.startswith(SHARED_SCOPE_PREFIX):
        name = scope[len(SHARED_SCOPE_PREFIX) :]
        # Shape first: a malformed shared name is a payload error; only a
        # well-formed name can be measured against the allowlist.
        shared_bank_id(name)
        if name not in shared_banks:
            raise _reject(
                MemoryErrorCode.BANK_FORBIDDEN,
                f"shared bank {name!r} is not in the configured allowlist",
            )
        return shared_bank_id(name)
    raise _reject(
        MemoryErrorCode.INVALID_PAYLOAD,
        f'unknown scope {scope!r}: use "server" or "shared:<name>"',
    )


# ---------------------------------------------------------------------------
# Payload validation (defense in depth; the bridge schema gate is primary)
# ---------------------------------------------------------------------------


def _bounded_str(
    value: object,
    *,
    field: str,
    max_chars: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_chars
        or "\x00" in value
    ):
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            f"{field} must be a non-empty string of at most "
            f"{max_chars} characters",
        )
    if pattern is not None and not pattern.fullmatch(value):
        raise _reject(MemoryErrorCode.INVALID_PAYLOAD, f"{field} has an invalid format")
    return value


def _bounded_int(value: object, *, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            f"{field} must be an integer between {low} and {high}",
        )
    return value


def _check_unknown(payload: Mapping[str, object], allowed: set[str]) -> None:
    """Fail closed on unknown fields — no smuggled field reaches I/O."""
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            f"unknown payload fields: {', '.join(unknown)}",
        )


def _scope_field(payload: Mapping[str, object]) -> str:
    scope = payload.get("scope")
    if not isinstance(scope, str) or not scope:
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            'scope is required: "server" or "shared:<name>"',
        )
    return scope


@dataclass(frozen=True, slots=True)
class RetainItem:
    """One validated retain item (payload shape, pre-screening)."""

    content: str
    context: str | None
    document_id: str | None


@dataclass(frozen=True, slots=True)
class RetainRequest:
    """One validated retain request."""

    scope: str
    items: tuple[RetainItem, ...]


_DOCUMENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def retain_request_from_payload(payload: Mapping[str, object]) -> RetainRequest:
    """Validate one retain payload into a :class:`RetainRequest`.

    Strict and fail-closed: unknown keys are rejected at the top level and
    inside every item, so no smuggled field (``bank``, ``server_id``,
    ``async``, free-form metadata …) can reach the provider call. The
    adapter always retains synchronously — that is what makes the 200
    document an actual receipt (acceptance 1).
    """
    if not isinstance(payload, Mapping):
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            "payload must be a JSON object (mapping)",
        )
    _check_unknown(payload, {"scope", "items"})
    scope = _scope_field(payload)
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= MAX_ITEMS:
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            f"items must be a list of 1..{MAX_ITEMS} objects",
        )
    items: list[RetainItem] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            raise _reject(
                MemoryErrorCode.INVALID_PAYLOAD,
                f"item {index} must be a JSON object",
            )
        _check_unknown(raw, {"content", "context", "document_id"})
        content = _bounded_str(
            raw.get("content"), field=f"item {index} content", max_chars=MAX_CONTENT_CHARS
        )
        raw_context = raw.get("context")
        context = (
            None
            if raw_context is None
            else _bounded_str(
                raw_context,
                field=f"item {index} context",
                max_chars=MAX_CONTEXT_CHARS,
            )
        )
        raw_document = raw.get("document_id")
        document_id = (
            None
            if raw_document is None
            else _bounded_str(
                raw_document,
                field=f"item {index} document_id",
                max_chars=MAX_DOCUMENT_ID_CHARS,
                pattern=_DOCUMENT_ID_RE,
            )
        )
        items.append(
            RetainItem(
                content=content.strip(),
                context=None if context is None else context.strip(),
                document_id=document_id,
            )
        )
    return RetainRequest(scope=scope, items=tuple(items))


@dataclass(frozen=True, slots=True)
class RecallRequest:
    """One validated recall/reflect request."""

    scope: str
    query: str
    types: tuple[str, ...] | None
    budget: str
    max_tokens: int


def _query_request_from_payload(
    payload: Mapping[str, object],
    *,
    allow_types: bool,
) -> RecallRequest:
    """Shared validator for recall and reflect payloads."""
    if not isinstance(payload, Mapping):
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            "payload must be a JSON object (mapping)",
        )
    allowed = {"scope", "query", "budget", "max_tokens"}
    if allow_types:
        allowed.add("types")
    _check_unknown(payload, allowed)
    scope = _scope_field(payload)
    query = _bounded_str(payload.get("query"), field="query", max_chars=MAX_QUERY_CHARS)
    raw_types = payload.get("types")
    types: tuple[str, ...] | None = None
    if raw_types is not None:
        if not isinstance(raw_types, list) or not raw_types:
            raise _reject(
                MemoryErrorCode.INVALID_PAYLOAD,
                "types must be a non-empty list of fact type names",
            )
        seen: dict[str, None] = {}
        for name in raw_types:
            if name not in VALID_FACT_TYPES:
                raise _reject(
                    MemoryErrorCode.INVALID_PAYLOAD,
                    "types entries must be one of: " + ", ".join(VALID_FACT_TYPES),
                )
            seen[name] = None
        types = tuple(seen.keys())
    budget = payload.get("budget", VALID_BUDGETS[0])
    if budget not in VALID_BUDGETS:
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            "budget must be one of: " + ", ".join(VALID_BUDGETS),
        )
    return RecallRequest(
        scope=scope,
        query=query.strip(),
        types=types,
        budget=budget,  # type: ignore[arg-type]
        max_tokens=_bounded_int(
            payload.get("max_tokens", DEFAULT_TOKEN_BUDGET),
            field="max_tokens",
            low=MIN_TOKEN_BUDGET,
            high=MAX_BUDGET_TOKENS,
        ),
    )


def recall_request_from_payload(payload: Mapping[str, object]) -> RecallRequest:
    """Validate one recall payload into a :class:`RecallRequest`."""
    return _query_request_from_payload(payload, allow_types=True)


def reflect_request_from_payload(payload: Mapping[str, object]) -> RecallRequest:
    """Validate one reflect payload into a :class:`RecallRequest`."""
    return _query_request_from_payload(payload, allow_types=False)


def _screen_outbound(text: str, *, where: str) -> None:
    """Refuse secret-shaped outbound text before any I/O (value-free)."""
    marker = sensitive_marker_reason(text)
    if marker is not None:
        raise _reject(
            MemoryErrorCode.SENSITIVE_CONTENT,
            f"{where} carries a secret-shaped marker ({marker}); "
            f"refusing to persist or send it",
        )


# ---------------------------------------------------------------------------
# Result types (frozen shapes via the *_document builders)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetainReceipt:
    """The actual synchronous retain receipt (acceptance 1)."""

    bank: str
    items_count: int
    operation_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True, slots=True)
class RetainOutcome:
    """One honest retain answer: a receipt, or a value-free failure."""

    status: MemoryStatus
    code: MemoryErrorCode | None
    scope: str
    bank: str
    receipt: RetainReceipt | None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """One bounded, fully cited recall hit (C07 rule 4).

    ``citation`` is the rendered provenance pointer
    (``memory:<bank>/<id>`` — resolvable through the upstream per-memory
    endpoint); the freshness tuple is upstream-recorded; ``score`` is the
    upstream final ranking score when reported.
    """

    rank: int
    bank: str
    id: str
    citation: str
    text: str
    truncated: bool
    type: str | None
    occurred_start: str | None
    occurred_end: str | None
    mentioned_at: str | None
    document_id: str | None
    entities: tuple[str, ...]
    tags: tuple[str, ...]
    score: float | None


@dataclass(frozen=True, slots=True)
class RecallOutcome:
    """One honest recall answer, value-free on failure."""

    status: MemoryStatus
    code: MemoryErrorCode | None
    scope: str
    bank: str
    query: str
    hits: tuple[MemoryHit, ...]
    results_truncated: bool
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class ReflectOutcome:
    """One honest reflect answer (LLM-generated, provenance-counted)."""

    status: MemoryStatus
    code: MemoryErrorCode | None
    scope: str
    bank: str
    query: str
    text: str
    truncated: bool
    based_on_ids: tuple[str, ...]
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class DeleteOutcome:
    """One honest delete receipt (scoped exactly like the reads)."""

    status: MemoryStatus
    code: MemoryErrorCode | None
    kind: str
    scope: str
    bank: str
    deleted_count: int | None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class HealthOutcome:
    """One honest availability probe (KB-H1 adapter side)."""

    status: MemoryStatus
    code: MemoryErrorCode | None
    api_version: str | None
    error_type: str | None = None


# ---------------------------------------------------------------------------
# Provider seam (httpx2; the transport stays a constructor/test seam)
# ---------------------------------------------------------------------------


class MemoryProvider(Protocol):
    """The injected network seam: one call → one raw provider document.

    Implementations raise on transport/HTTP failure and for upstream
    documents that are not JSON objects; the capability classifies.
    Production is :class:`HindsightProvider`; tests inject deterministic
    fakes or an httpx2 ``MockTransport``.
    """

    async def retain(
        self, *, bank_id: str, items: Sequence[Mapping[str, object]]
    ) -> Mapping[str, object]: ...

    async def recall(
        self,
        *,
        bank_id: str,
        query: str,
        types: Sequence[str] | None,
        budget: str,
        max_tokens: int,
    ) -> Mapping[str, object]: ...

    async def reflect(
        self,
        *,
        bank_id: str,
        query: str,
        budget: str,
        max_tokens: int,
    ) -> Mapping[str, object]: ...

    async def delete_memories(
        self, *, bank_id: str, fact_type: str | None = None
    ) -> Mapping[str, object]: ...

    async def delete_bank(self, *, bank_id: str) -> Mapping[str, object]: ...

    async def export_bank(self, *, bank_id: str) -> Mapping[str, object]: ...

    async def version(self) -> Mapping[str, object]: ...


def _classify_provider_exception(exc: BaseException) -> MemoryErrorCode:
    """Map one provider exception to its stable seam code (value-free)."""
    import httpx2

    if isinstance(exc, httpx2.TimeoutException):
        return MemoryErrorCode.PROVIDER_TIMEOUT
    if isinstance(exc, httpx2.NetworkError):
        return MemoryErrorCode.PROVIDER_UNREACHABLE
    return MemoryErrorCode.PROVIDER_ERROR


class HindsightProvider:
    """The bundled-Hindsight bank-scoped HTTP API over an httpx2 client.

    Base URL is constructor wiring (in production the compose-internal
    ``http://hindsight:8888``; C07 rule 7), never read from the process
    environment inside this module. The client attaches no credential
    material (the bundled service has no API auth; C03 rule 6 keeps secret
    handling out of this module entirely). Every request path and body is
    the pinned 1.0.0 upstream shape; bank ids are validated here so a
    miswired caller cannot address an arbitrary path segment.
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
                MemoryErrorCode.INVALID_PAYLOAD,
                "base_url must be an absolute http(s) URL",
            )
        if not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise _reject(
                MemoryErrorCode.INVALID_PAYLOAD,
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
        return f"<HindsightProvider base_url={self._base_url!r}>"

    @property
    def base_url(self) -> str:
        return self._base_url

    def _check_bank(self, bank_id: str) -> None:
        prefix_ok = bank_id.startswith((SERVER_BANK_PREFIX, SHARED_BANK_PREFIX))
        rest_ok = (
            prefix_ok
            and len(bank_id) > min(len(SERVER_BANK_PREFIX), len(SHARED_BANK_PREFIX))
            and re.fullmatch(r"[a-z0-9][a-z0-9-]*", bank_id) is not None
        )
        if (
            not isinstance(bank_id, str)
            or len(bank_id) > MAX_BANK_ID_CHARS
            or not rest_ok
        ):
            raise _reject(
                MemoryErrorCode.INVALID_PAYLOAD,
                "bank_id must be this adapter's mai-server-/mai-shared- "
                "bank scheme",
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Mapping[str, object]:
        import httpx2

        client = httpx2.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,  # type: ignore[arg-type]
            headers={"User-Agent": USER_AGENT},
        )
        try:
            response = await client.request(
                method, path, json=None if json_body is None else dict(json_body), params=dict(params or {})
            )
            response.raise_for_status()
            document = response.json()
        finally:
            await client.aclose()
        if not isinstance(document, dict):
            raise _reject(
                MemoryErrorCode.PROVIDER_ERROR,
                f"provider returned a JSON value that is not an object "
                f"({path})",
            )
        return document

    def _bank_url(self, bank_id: str, suffix: str = "") -> str:
        self._check_bank(bank_id)
        return f"{self._base_url}/v1/default/banks/{bank_id}{suffix}"

    async def retain(
        self, *, bank_id: str, items: Sequence[Mapping[str, object]]
    ) -> Mapping[str, object]:
        """Synchronous retain: the 200 document is the actual receipt."""
        return await self._request(
            "POST",
            self._bank_url(bank_id, "/memories"),
            json_body={"items": list(items), "async": False},
        )

    async def recall(
        self,
        *,
        bank_id: str,
        query: str,
        types: Sequence[str] | None,
        budget: str,
        max_tokens: int,
    ) -> Mapping[str, object]:
        body: dict[str, object] = {
            "query": query,
            "budget": budget,
            "max_tokens": max_tokens,
        }
        if types:
            body["types"] = list(types)
        return await self._request(
            "POST", self._bank_url(bank_id, "/memories/recall"), json_body=body
        )

    async def reflect(
        self,
        *,
        bank_id: str,
        query: str,
        budget: str,
        max_tokens: int,
    ) -> Mapping[str, object]:
        return await self._request(
            "POST",
            self._bank_url(bank_id, "/reflect"),
            json_body={
                "query": query,
                "budget": budget,
                "max_tokens": max_tokens,
            },
        )

    async def delete_memories(
        self, *, bank_id: str, fact_type: str | None = None
    ) -> Mapping[str, object]:
        params = {"type": fact_type} if fact_type is not None else None
        return await self._request(
            "DELETE", self._bank_url(bank_id, "/memories"), params=params
        )

    async def delete_bank(self, *, bank_id: str) -> Mapping[str, object]:
        return await self._request("DELETE", self._bank_url(bank_id))

    async def export_bank(self, *, bank_id: str) -> Mapping[str, object]:
        return await self._request("GET", self._bank_url(bank_id, "/export"))

    async def version(self) -> Mapping[str, object]:
        return await self._request("GET", f"{self._base_url}/version")


# ---------------------------------------------------------------------------
# Provider-document parsing (pure; strict shapes, bounded fields)
# ---------------------------------------------------------------------------

#: Bound for one best-effort provenance id before truncation.
_MAX_ID_LOOKAHEAD = 256


def _optional_str(value: object, max_chars: int) -> str | None:
    if isinstance(value, str) and value and len(value) <= max_chars:
        return value
    return None


def _bounded_str_list(
    value: object, *, max_items: int, max_chars: int
) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item and len(item) <= max_chars:
            out.append(item)
        if len(out) >= max_items:
            break
    return tuple(out)


def _bounded_int_field(document: Mapping[str, object], key: str) -> int | None:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _retain_failure(scope: str, bank: str, error_type: str) -> RetainOutcome:
    return RetainOutcome(
        status=MemoryStatus.UNAVAILABLE,
        code=MemoryErrorCode.PROVIDER_ERROR,
        scope=scope,
        bank=bank,
        receipt=None,
        error_type=error_type,
    )


def parse_retain_document(
    document: Mapping[str, object], *, scope: str, bank: str
) -> RetainOutcome:
    """Turn one raw retain 200 document into a :class:`RetainOutcome`.

    ``success: true`` plus ``items_count`` is the receipt (acceptance 1);
    a ``success: false`` document — or a document without the required
    receipt fields — is an unavailable outcome: a failed write is never
    laundered into success (KB-H2).
    """
    if document.get("success") is not True:
        return _retain_failure(scope, bank, "operation_unsuccessful")
    items_count = _bounded_int_field(document, "items_count")
    if items_count is None:
        return _retain_failure(scope, bank, "receipt_incomplete")
    usage = document.get("usage")
    usage_map = usage if isinstance(usage, Mapping) else None
    return RetainOutcome(
        status=MemoryStatus.OK,
        code=None,
        scope=scope,
        bank=bank,
        receipt=RetainReceipt(
            bank=bank,
            items_count=items_count,
            operation_id=_optional_str(
                document.get("operation_id"), MAX_DOCUMENT_ID_CHARS
            ),
            input_tokens=(
                _bounded_int_field(usage_map, "input_tokens")
                if usage_map
                else None
            ),
            output_tokens=(
                _bounded_int_field(usage_map, "output_tokens")
                if usage_map
                else None
            ),
            total_tokens=(
                _bounded_int_field(usage_map, "total_tokens")
                if usage_map
                else None
            ),
        ),
    )


def parse_recall_document(
    document: Mapping[str, object],
    *,
    scope: str,
    bank: str,
    query: str,
    max_text_chars: int,
) -> RecallOutcome:
    """Turn one raw recall 200 document into a bounded :class:`RecallOutcome`.

    Fail-closed on shape: the document must carry a ``results`` list;
    anything else is ``provider_error``. Hits without an ``id`` + ``text``
    are skipped — a memory without provenance is never shown (C07 rule 4).
    Bounded at every field; beyond :data:`MAX_RESULTS` hits the outcome is
    flagged ``results_truncated``.
    """
    if not isinstance(document, Mapping) or not isinstance(document.get("results"), list):
        return RecallOutcome(
            status=MemoryStatus.UNAVAILABLE,
            code=MemoryErrorCode.PROVIDER_ERROR,
            scope=scope,
            bank=bank,
            query=query,
            hits=(),
            results_truncated=False,
            error_type="invalid_provider_document",
        )
    hits: list[MemoryHit] = []
    truncated_results = False
    for item in document["results"]:
        if len(hits) >= MAX_RESULTS:
            truncated_results = True
            break
        if not isinstance(item, Mapping):
            continue
        memory_id = _optional_str(item.get("id"), MAX_DOCUMENT_ID_CHARS)
        raw_text = item.get("text")
        if memory_id is None or not isinstance(raw_text, str):
            continue
        text = raw_text
        truncated = len(text) > max_text_chars
        if truncated:
            text = text[:max_text_chars]
        scores = item.get("scores")
        raw_score = scores.get("final") if isinstance(scores, Mapping) else None
        score = (
            float(raw_score)
            if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool)
            else None
        )
        hits.append(
            MemoryHit(
                rank=len(hits) + 1,
                bank=bank,
                id=memory_id,
                citation=f"memory:{bank}/{memory_id}",
                text=text,
                truncated=truncated,
                type=_optional_str(item.get("type"), 32),
                occurred_start=_optional_str(item.get("occurred_start"), 64),
                occurred_end=_optional_str(item.get("occurred_end"), 64),
                mentioned_at=_optional_str(item.get("mentioned_at"), 64),
                document_id=_optional_str(item.get("document_id"), MAX_DOCUMENT_ID_CHARS),
                entities=_bounded_str_list(
                    item.get("entities"),
                    max_items=MAX_ENTITIES_PER_HIT,
                    max_chars=MAX_ENTITY_CHARS,
                ),
                tags=_bounded_str_list(
                    item.get("tags"),
                    max_items=MAX_TAGS_PER_HIT,
                    max_chars=MAX_TAG_CHARS,
                ),
                score=score,
            )
        )
    return RecallOutcome(
        status=MemoryStatus.OK,
        code=None,
        scope=scope,
        bank=bank,
        query=query,
        hits=tuple(hits),
        results_truncated=truncated_results,
    )


def _based_on_ids(document: Mapping[str, object]) -> tuple[str, ...]:
    """Bounded fact ids a reflect answer reports as its basis.

    Upstream models ``based_on`` as a free-form object keyed by fact
    source; this extracts the string ids it can see (string values and
    lists of strings), bounded — an unexpected shape yields an empty tuple
    rather than an error (the answer itself is the payload; the basis list
    is best-effort provenance).
    """
    based_on = document.get("based_on")
    if not isinstance(based_on, Mapping):
        return ()
    flat: list[str] = []
    for value in based_on.values():
        if isinstance(value, str):
            flat.append(value)
        elif isinstance(value, list):
            flat.extend(item for item in value if isinstance(item, str) and item)
    bounded = [item[:MAX_DOCUMENT_ID_CHARS] for item in flat if len(item) <= _MAX_ID_LOOKAHEAD]
    return tuple(bounded[:MAX_RESULTS])


def parse_reflect_document(
    document: Mapping[str, object],
    *,
    scope: str,
    bank: str,
    query: str,
    max_answer_chars: int,
) -> ReflectOutcome:
    """Turn one raw reflect 200 document into a bounded ReflectOutcome."""
    raw_text = document.get("text")
    if not isinstance(raw_text, str):
        return ReflectOutcome(
            status=MemoryStatus.UNAVAILABLE,
            code=MemoryErrorCode.PROVIDER_ERROR,
            scope=scope,
            bank=bank,
            query=query,
            text="",
            truncated=False,
            based_on_ids=(),
            error_type="invalid_provider_document",
        )
    truncated = len(raw_text) > max_answer_chars
    return ReflectOutcome(
        status=MemoryStatus.OK,
        code=None,
        scope=scope,
        bank=bank,
        query=query,
        text=raw_text[:max_answer_chars],
        truncated=truncated,
        based_on_ids=_based_on_ids(document),
    )


def parse_delete_document(
    document: Mapping[str, object],
    *,
    kind: str,
    scope: str,
    bank: str,
) -> DeleteOutcome:
    """Turn one raw delete receipt into a :class:`DeleteOutcome`."""
    if document.get("success") is True:
        return DeleteOutcome(
            status=MemoryStatus.OK,
            code=None,
            kind=kind,
            scope=scope,
            bank=bank,
            deleted_count=_bounded_int_field(document, "deleted_count"),
        )
    return DeleteOutcome(
        status=MemoryStatus.UNAVAILABLE,
        code=MemoryErrorCode.PROVIDER_ERROR,
        kind=kind,
        scope=scope,
        bank=bank,
        deleted_count=None,
        error_type="operation_unsuccessful",
    )


# ---------------------------------------------------------------------------
# Frozen result documents
# ---------------------------------------------------------------------------


def retain_document(outcome: RetainOutcome) -> dict[str, object]:
    """The frozen JSON document of one retain outcome (stable key order)."""
    receipt = outcome.receipt
    return {
        "status": outcome.status.value,
        "code": outcome.code.value if outcome.code is not None else None,
        "provider": PROVIDER_LABEL,
        "operation": "retain",
        "scope": outcome.scope,
        "bank": outcome.bank,
        "receipt": None
        if receipt is None
        else {
            "items_count": receipt.items_count,
            "operation_id": receipt.operation_id,
            "usage": {
                "input_tokens": receipt.input_tokens,
                "output_tokens": receipt.output_tokens,
                "total_tokens": receipt.total_tokens,
            },
        },
        "error_type": outcome.error_type,
    }


def recall_document(outcome: RecallOutcome) -> dict[str, object]:
    """The frozen JSON document of one recall outcome (stable key order)."""
    return {
        "status": outcome.status.value,
        "code": outcome.code.value if outcome.code is not None else None,
        "provider": PROVIDER_LABEL,
        "operation": "recall",
        "trust": TRUST_LABEL,
        "role": ROLE_LABEL,
        "scope": outcome.scope,
        "bank": outcome.bank,
        "query": outcome.query,
        "results": [
            {
                "rank": hit.rank,
                "id": hit.id,
                "citation": hit.citation,
                "bank": hit.bank,
                "text": hit.text,
                "truncated": hit.truncated,
                "type": hit.type,
                "occurred_start": hit.occurred_start,
                "occurred_end": hit.occurred_end,
                "mentioned_at": hit.mentioned_at,
                "document_id": hit.document_id,
                "entities": list(hit.entities),
                "tags": list(hit.tags),
                "score": hit.score,
            }
            for hit in outcome.hits
        ],
        "results_truncated": outcome.results_truncated,
        "error_type": outcome.error_type,
    }


def reflect_document(outcome: ReflectOutcome) -> dict[str, object]:
    """The frozen JSON document of one reflect outcome (stable key order)."""
    return {
        "status": outcome.status.value,
        "code": outcome.code.value if outcome.code is not None else None,
        "provider": PROVIDER_LABEL,
        "operation": "reflect",
        "trust": TRUST_LABEL,
        "role": ROLE_LABEL,
        "scope": outcome.scope,
        "bank": outcome.bank,
        "query": outcome.query,
        "text": outcome.text,
        "truncated": outcome.truncated,
        "based_on": list(outcome.based_on_ids),
        "error_type": outcome.error_type,
    }


def delete_document(outcome: DeleteOutcome) -> dict[str, object]:
    """The frozen JSON document of one delete receipt (stable key order)."""
    return {
        "status": outcome.status.value,
        "code": outcome.code.value if outcome.code is not None else None,
        "provider": PROVIDER_LABEL,
        "operation": outcome.kind,
        "scope": outcome.scope,
        "bank": outcome.bank,
        "deleted_count": outcome.deleted_count,
        "error_type": outcome.error_type,
    }


def health_document(outcome: HealthOutcome) -> dict[str, object]:
    """The frozen JSON document of one availability probe."""
    return {
        "status": outcome.status.value,
        "code": outcome.code.value if outcome.code is not None else None,
        "provider": PROVIDER_LABEL,
        "operation": "status",
        "api_version": outcome.api_version,
        "error_type": outcome.error_type,
    }


# ---------------------------------------------------------------------------
# Capability descriptors (C03 rule 1 frozen vocabulary)
# ---------------------------------------------------------------------------

_SCOPE_DESCRIPTION = (
    'Target bank scope: "server" resolves to the execution context\'s '
    'server bank; "shared:<name>" resolves through the configured '
    "shared-bank allowlist only. A payload can never name a bank or "
    "another server."
)

_UNAVAILABLE_PROPERTIES: dict[str, object] = {
    "status": {
        "type": "string",
        "enum": sorted(item.value for item in MemoryStatus),
    },
    "code": {"type": ["string", "null"]},
    "provider": {"type": "string", "enum": [PROVIDER_LABEL]},
    "operation": {"type": "string"},
    "error_type": {"type": ["string", "null"]},
}


def _base_descriptor(
    capability_id: str,
    effect_class: str,
    input_schema: dict[str, object],
    output_schema: dict[str, object],
    *,
    resource_scope: tuple[str, ...] = (),
) -> CapabilityDescriptor:
    return parse_descriptor(
        {
            "identifier": capability_id,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "execution_environments": ["controller"],
            "effect_class": effect_class,
            "resource_scope": list(resource_scope),
            "required_grants": [capability_id],
        },
        source=f"<{capability_id} descriptor>",
    )


def recall_descriptor() -> CapabilityDescriptor:
    """The validated ``memory.recall`` descriptor (``read``)."""
    input_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["scope", "query"],
        "properties": {
            "scope": {"type": "string", "description": _SCOPE_DESCRIPTION},
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUERY_CHARS,
                "description": (
                    "Recall query; screened for secret-shaped markers and "
                    "refused before any request (the marker class is "
                    "named, never the value)."
                ),
            },
            "types": {
                "type": "array",
                "items": {"type": "string", "enum": list(VALID_FACT_TYPES)},
                "description": "Optional fact-type filter.",
            },
            "budget": {
                "type": "string",
                "enum": list(VALID_BUDGETS),
                "default": VALID_BUDGETS[0],
                "description": "Upstream retrieval budget level.",
            },
            "max_tokens": {
                "type": "integer",
                "minimum": MIN_TOKEN_BUDGET,
                "maximum": MAX_BUDGET_TOKENS,
                "default": DEFAULT_TOKEN_BUDGET,
                "description": "Upstream answer token budget (bounded context, C06 rule 3).",
            },
        },
    }
    hit_properties: dict[str, object] = {
        "rank": {"type": "integer"},
        "id": {"type": "string"},
        "citation": {"type": "string"},
        "bank": {"type": "string"},
        "text": {"type": "string"},
        "truncated": {"type": "boolean"},
        "type": {"type": ["string", "null"]},
        "occurred_start": {"type": ["string", "null"]},
        "occurred_end": {"type": ["string", "null"]},
        "mentioned_at": {"type": ["string", "null"]},
        "document_id": {"type": ["string", "null"]},
        "entities": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "score": {"type": ["number", "null"]},
    }
    output_schema: dict[str, object] = {
        "type": "object",
        "required": [
            "status",
            "code",
            "provider",
            "operation",
            "trust",
            "role",
            "scope",
            "bank",
            "query",
            "results",
            "results_truncated",
            "error_type",
        ],
        "properties": {
            **_UNAVAILABLE_PROPERTIES,
            "trust": {
                "type": "string",
                "enum": [TRUST_LABEL],
                "description": (
                    "Operational memory is historical context — never a "
                    "source of live facts or documents (C07 rule 6)."
                ),
            },
            "role": {"type": "string", "enum": [ROLE_LABEL]},
            "scope": {"type": "string"},
            "bank": {"type": "string"},
            "query": {"type": "string"},
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": sorted(hit_properties),
                    "properties": hit_properties,
                },
            },
            "results_truncated": {"type": "boolean"},
        },
    }
    return _base_descriptor(RECALL_CAPABILITY_ID, "read", input_schema, output_schema)


def retain_descriptor() -> CapabilityDescriptor:
    """The validated ``memory.retain`` descriptor (``mutate``)."""
    input_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["scope", "items"],
        "properties": {
            "scope": {"type": "string", "description": _SCOPE_DESCRIPTION},
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_ITEMS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["content"],
                    "properties": {
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_CONTENT_CHARS,
                            "description": (
                                "Episodic note to extract facts from; "
                                "screened for secret-shaped markers "
                                "before any request."
                            ),
                        },
                        "context": {
                            "type": ["string", "null"],
                            "maxLength": MAX_CONTEXT_CHARS,
                            "description": (
                                "Optional bounded context line recorded "
                                "with the item."
                            ),
                        },
                        "document_id": {
                            "type": ["string", "null"],
                            "maxLength": MAX_DOCUMENT_ID_CHARS,
                            "description": (
                                "Optional caller handle for document "
                                "evolution: re-retaining with the same id "
                                "supersedes the earlier version."
                            ),
                        },
                    },
                },
                "description": (
                    "Retain is always synchronous: the 200 receipt is the "
                    "actual write evidence; failures are visible, never "
                    "success-laundered."
                ),
            },
        },
    }
    output_schema: dict[str, object] = {
        "type": "object",
        "required": [
            "status",
            "code",
            "provider",
            "operation",
            "scope",
            "bank",
            "receipt",
            "error_type",
        ],
        "properties": {
            **_UNAVAILABLE_PROPERTIES,
            "scope": {"type": "string"},
            "bank": {"type": "string"},
            "receipt": {
                "anyOf": [
                    {
                        "type": "object",
                        "required": ["items_count", "operation_id", "usage"],
                        "properties": {
                            "items_count": {"type": "integer"},
                            "operation_id": {"type": ["string", "null"]},
                            "usage": {
                                "type": "object",
                                "properties": {
                                    "input_tokens": {"type": ["integer", "null"]},
                                    "output_tokens": {"type": ["integer", "null"]},
                                    "total_tokens": {"type": ["integer", "null"]},
                                },
                            },
                        },
                    },
                    {"type": "null"},
                ]
            },
        },
    }
    return _base_descriptor(
        RETAIN_CAPABILITY_ID,
        "mutate",
        input_schema,
        output_schema,
        # A bank write declares its lock/planning surface (C03 rule 1:
        # non-read effects name their resource); all banks share the one
        # memory-bank resource namespace — the bank identity itself is the
        # scope this adapter derives from the execution context.
        resource_scope=("memory.bank",),
    )


def reflect_descriptor() -> CapabilityDescriptor:
    """The validated ``memory.reflect`` descriptor (``read``)."""
    input_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["scope", "query"],
        "properties": {
            "scope": {"type": "string", "description": _SCOPE_DESCRIPTION},
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUERY_CHARS,
                "description": (
                    "Question answered from the bank's memories; screened "
                    "for secret-shaped markers before any request. The "
                    "generated answer is historical context, never a live "
                    "fact."
                ),
            },
            "budget": {
                "type": "string",
                "enum": list(VALID_BUDGETS),
                "default": VALID_BUDGETS[0],
            },
            "max_tokens": {
                "type": "integer",
                "minimum": MIN_TOKEN_BUDGET,
                "maximum": MAX_BUDGET_TOKENS,
                "default": DEFAULT_TOKEN_BUDGET,
            },
        },
    }
    output_schema: dict[str, object] = {
        "type": "object",
        "required": [
            "status",
            "code",
            "provider",
            "operation",
            "trust",
            "role",
            "scope",
            "bank",
            "query",
            "text",
            "truncated",
            "based_on",
            "error_type",
        ],
        "properties": {
            **_UNAVAILABLE_PROPERTIES,
            "trust": {
                "type": "string",
                "enum": [TRUST_LABEL],
                "description": (
                    "A generated answer over retained memory — historical "
                    "context, never a source of live facts or documents."
                ),
            },
            "role": {"type": "string", "enum": [ROLE_LABEL]},
            "scope": {"type": "string"},
            "bank": {"type": "string"},
            "query": {"type": "string"},
            "text": {"type": "string"},
            "truncated": {"type": "boolean"},
            "based_on": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Bounded fact ids the answer reports as its basis.",
            },
        },
    }
    return _base_descriptor(REFLECT_CAPABILITY_ID, "read", input_schema, output_schema)


# ---------------------------------------------------------------------------
# The trusted capability implementations (dispatcher-gated)
# ---------------------------------------------------------------------------


def _split_call(call: object) -> tuple[Mapping[str, object], ExecutionContext]:
    """Validate one dispatcher call envelope (same discipline as search)."""
    if not isinstance(call, CapabilityCall):
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            "executor input must be a dispatcher CapabilityCall",
        )
    context = call.context
    if not isinstance(context, ExecutionContext):
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            "call context must be an ExecutionContext",
        )
    return call.payload, context


def _int_between(
    value: object, *, low: int, high: int, label: str
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _reject(
            MemoryErrorCode.INVALID_PAYLOAD,
            f"{label} must be an integer between {low} and {high}",
        )
    return value


class _MemoryCapability:
    """Shared executor wiring: provider seam + scope policy + budgets."""

    def __init__(
        self,
        provider: MemoryProvider | None,
        *,
        shared_banks: Sequence[str] = (),
        default_max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        default_token_budget: int = DEFAULT_TOKEN_BUDGET,
        default_reflect_chars: int = DEFAULT_REFLECT_CHARS,
    ) -> None:
        self._provider = provider
        self._shared_banks = parse_shared_banks(shared_banks)
        self._default_max_text_chars = _int_between(
            default_max_text_chars,
            low=MIN_TEXT_CHARS,
            high=MAX_TEXT_CHARS,
            label="default_max_text_chars",
        )
        self._default_token_budget = _int_between(
            default_token_budget,
            low=MIN_TOKEN_BUDGET,
            high=MAX_BUDGET_TOKENS,
            label="default_token_budget",
        )
        self._default_reflect_chars = _int_between(
            default_reflect_chars,
            low=MIN_REFLECT_CHARS,
            high=MAX_REFLECT_CHARS,
            label="default_reflect_chars",
        )

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} provider={self._provider!r} "
            f"shared_banks={list(self._shared_banks)!r}>"
        )

    def _bank(self, scope: str, context: ExecutionContext) -> str:
        return bank_for_scope(scope, context, shared_banks=self._shared_banks)

    @property
    def disabled_code(self) -> MemoryErrorCode:
        return MemoryErrorCode.MEMORY_DISABLED


class MemoryRecallCapability(_MemoryCapability):
    """``memory.recall`` executor: one gated query → cited historical hits."""

    async def __call__(self, call: object) -> ExecutorResult:
        payload, context = _split_call(call)
        request = recall_request_from_payload(payload)
        bank = self._bank(request.scope, context)
        _screen_outbound(request.query, where="recall query")
        if self._provider is None:
            # Graceful degradation: Hindsight disabled/unwired is an honest
            # unavailable answer, never an import failure or a fake success.
            outcome = RecallOutcome(
                status=MemoryStatus.UNAVAILABLE,
                code=self.disabled_code,
                scope=request.scope,
                bank=bank,
                query=request.query,
                hits=(),
                results_truncated=False,
                error_type=None,
            )
        else:
            try:
                document = await self._provider.recall(
                    bank_id=bank,
                    query=request.query,
                    types=request.types,
                    budget=request.budget,
                    max_tokens=request.max_tokens,
                )
            except MemoryAdapterError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified, value-free
                outcome = RecallOutcome(
                    status=MemoryStatus.UNAVAILABLE,
                    code=_classify_provider_exception(exc),
                    scope=request.scope,
                    bank=bank,
                    query=request.query,
                    hits=(),
                    results_truncated=False,
                    error_type=type(exc).__name__,
                )
            else:
                outcome = parse_recall_document(
                    document,
                    scope=request.scope,
                    bank=bank,
                    query=request.query,
                    max_text_chars=self._default_max_text_chars,
                )
        return ExecutorResult(
            ok=outcome.status is MemoryStatus.OK,
            output=json.dumps(recall_document(outcome), sort_keys=True, ensure_ascii=True),
        )


class MemoryRetainCapability(_MemoryCapability):
    """``memory.retain`` executor: synchronous write with an honest receipt."""

    async def __call__(self, call: object) -> ExecutorResult:
        payload, context = _split_call(call)
        request = retain_request_from_payload(payload)
        bank = self._bank(request.scope, context)
        for index, item in enumerate(request.items):
            _screen_outbound(item.content, where=f"retain item {index}")
            if item.context is not None:
                _screen_outbound(item.context, where=f"retain item {index} context")
        if self._provider is None:
            outcome = RetainOutcome(
                status=MemoryStatus.UNAVAILABLE,
                code=self.disabled_code,
                scope=request.scope,
                bank=bank,
                receipt=None,
                error_type=None,
            )
        else:
            payload_items: list[Mapping[str, object]] = []
            for item in request.items:
                upstream: dict[str, object] = {"content": item.content}
                if item.context is not None:
                    upstream["context"] = item.context
                if item.document_id is not None:
                    upstream["document_id"] = item.document_id
                payload_items.append(upstream)
            try:
                document = await self._provider.retain(
                    bank_id=bank, items=payload_items
                )
            except MemoryAdapterError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified, value-free
                outcome = RetainOutcome(
                    status=MemoryStatus.UNAVAILABLE,
                    code=_classify_provider_exception(exc),
                    scope=request.scope,
                    bank=bank,
                    receipt=None,
                    error_type=type(exc).__name__,
                )
            else:
                outcome = parse_retain_document(document, scope=request.scope, bank=bank)
        return ExecutorResult(
            ok=outcome.status is MemoryStatus.OK,
            output=json.dumps(retain_document(outcome), sort_keys=True, ensure_ascii=True),
        )


class MemoryReflectCapability(_MemoryCapability):
    """``memory.reflect`` executor: bounded generated answer + basis ids."""

    async def __call__(self, call: object) -> ExecutorResult:
        payload, context = _split_call(call)
        request = reflect_request_from_payload(payload)
        bank = self._bank(request.scope, context)
        _screen_outbound(request.query, where="reflect query")
        if self._provider is None:
            outcome = ReflectOutcome(
                status=MemoryStatus.UNAVAILABLE,
                code=self.disabled_code,
                scope=request.scope,
                bank=bank,
                query=request.query,
                text="",
                truncated=False,
                based_on_ids=(),
                error_type=None,
            )
        else:
            try:
                document = await self._provider.reflect(
                    bank_id=bank,
                    query=request.query,
                    budget=request.budget,
                    max_tokens=request.max_tokens,
                )
            except MemoryAdapterError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified, value-free
                outcome = ReflectOutcome(
                    status=MemoryStatus.UNAVAILABLE,
                    code=_classify_provider_exception(exc),
                    scope=request.scope,
                    bank=bank,
                    query=request.query,
                    text="",
                    truncated=False,
                    based_on_ids=(),
                    error_type=type(exc).__name__,
                )
            else:
                outcome = parse_reflect_document(
                    document,
                    scope=request.scope,
                    bank=bank,
                    query=request.query,
                    max_answer_chars=self._default_reflect_chars,
                )
        return ExecutorResult(
            ok=outcome.status is MemoryStatus.OK,
            output=json.dumps(reflect_document(outcome), sort_keys=True, ensure_ascii=True),
        )


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _emit(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _cli_error(exc: MemoryAdapterError) -> int:
    _emit({"kind": "error", "code": exc.code.value, "detail": exc.detail})
    return 1


def _cli_context() -> ExecutionContext:
    return ExecutionContext(
        principal="cli-operator",
        server_id=uuid.UUID(int=0),
        run_id=uuid.uuid4(),
        parent_scope=None,
        profile_revision="cli",
        prompt_revision="cli",
        credential_references=(),
        allowed_capabilities=(RECALL_CAPABILITY_ID,),
        mode=PolicyMode.DELEGATED,
    )


def _cli_provider(args: argparse.Namespace) -> HindsightProvider:
    return HindsightProvider(
        args.base_url,
        timeout_seconds=(
            DEFAULT_TIMEOUT_SECONDS
            if args.timeout_seconds is None
            else args.timeout_seconds
        ),
    )


def _cli_shared_banks(args: argparse.Namespace) -> tuple[str, ...]:
    raw = getattr(args, "shared_banks", None)
    if not raw:
        return ()
    return parse_shared_banks([name.strip() for name in raw.split(",") if name.strip()])


async def _status_async(args: argparse.Namespace) -> int:
    try:
        provider = _cli_provider(args)
    except MemoryAdapterError as exc:
        return _cli_error(exc)
    try:
        document = await provider.version()
    except Exception as exc:  # noqa: BLE001 - classified, value-free
        _emit(
            health_document(
                HealthOutcome(
                    status=MemoryStatus.UNAVAILABLE,
                    code=_classify_provider_exception(exc),
                    api_version=None,
                    error_type=type(exc).__name__,
                )
            )
        )
        return 0
    _emit(
        health_document(
            HealthOutcome(
                status=MemoryStatus.OK,
                code=None,
                api_version=_optional_str(document.get("api_version"), 32),
                error_type=None,
            )
        )
    )
    return 0


async def _recall_async(args: argparse.Namespace) -> int:
    try:
        capability = MemoryRecallCapability(
            _cli_provider(args),
            shared_banks=_cli_shared_banks(args),
            default_max_text_chars=args.max_text_chars,
        )
        payload: dict[str, object] = {"scope": args.scope, "query": args.query}
        if args.types:
            payload["types"] = [name.strip() for name in args.types.split(",") if name.strip()]
        if args.budget is not None:
            payload["budget"] = args.budget
        if args.max_tokens is not None:
            payload["max_tokens"] = args.max_tokens
        call = CapabilityCall(
            descriptor=recall_descriptor(),
            payload=payload,
            context=_cli_context(),
            credentials={},
            op_id=uuid.uuid4(),
        )
    except MemoryAdapterError as exc:
        return _cli_error(exc)
    try:
        result = await capability(call)
    except MemoryAdapterError as exc:
        return _cli_error(exc)
    _emit(json.loads(result.output))
    return 0


async def _retain_async(args: argparse.Namespace) -> int:
    try:
        capability = MemoryRetainCapability(
            _cli_provider(args),
            shared_banks=_cli_shared_banks(args),
        )
        call = CapabilityCall(
            descriptor=retain_descriptor(),
            payload={
                "scope": args.scope,
                "items": [{"content": content} for content in args.item],
            },
            context=_cli_context(),
            credentials={},
            op_id=uuid.uuid4(),
        )
    except MemoryAdapterError as exc:
        return _cli_error(exc)
    try:
        result = await capability(call)
    except MemoryAdapterError as exc:
        return _cli_error(exc)
    _emit(json.loads(result.output))
    return 0


async def _reflect_async(args: argparse.Namespace) -> int:
    try:
        capability = MemoryReflectCapability(
            _cli_provider(args),
            shared_banks=_cli_shared_banks(args),
            default_reflect_chars=args.max_answer_chars,
        )
        payload: dict[str, object] = {"scope": args.scope, "query": args.query}
        if args.max_tokens is not None:
            payload["max_tokens"] = args.max_tokens
        call = CapabilityCall(
            descriptor=reflect_descriptor(),
            payload=payload,
            context=_cli_context(),
            credentials={},
            op_id=uuid.uuid4(),
        )
    except MemoryAdapterError as exc:
        return _cli_error(exc)
    try:
        result = await capability(call)
    except MemoryAdapterError as exc:
        return _cli_error(exc)
    _emit(json.loads(result.output))
    return 0


async def _delete_async(args: argparse.Namespace, *, bank: bool) -> int:
    kind = "delete-bank" if bank else "delete-memories"
    try:
        provider = _cli_provider(args)
        bank_id = bank_for_scope(
            args.scope, _cli_context(), shared_banks=_cli_shared_banks(args)
        )
        fact_type = getattr(args, "fact_type", None)
        if fact_type is not None and fact_type not in VALID_FACT_TYPES:
            raise _reject(
                MemoryErrorCode.INVALID_PAYLOAD,
                "type must be one of: " + ", ".join(VALID_FACT_TYPES),
            )
        if bank:
            document = await provider.delete_bank(bank_id=bank_id)
        else:
            document = await provider.delete_memories(
                bank_id=bank_id, fact_type=fact_type
            )
    except MemoryAdapterError as exc:
        return _cli_error(exc)
    except Exception as exc:  # noqa: BLE001 - classified, value-free
        _emit(
            delete_document(
                DeleteOutcome(
                    status=MemoryStatus.UNAVAILABLE,
                    code=_classify_provider_exception(exc),
                    kind=kind,
                    scope=args.scope,
                    bank=bank_id,
                    deleted_count=None,
                    error_type=type(exc).__name__,
                )
            )
        )
        return 0
    _emit(
        delete_document(
            parse_delete_document(document, kind=kind, scope=args.scope, bank=bank_id)
        )
    )
    return 0


async def _export_async(args: argparse.Namespace) -> int:
    try:
        provider = _cli_provider(args)
        bank_id = bank_for_scope(
            args.scope, _cli_context(), shared_banks=_cli_shared_banks(args)
        )
        document = await provider.export_bank(bank_id=bank_id)
    except MemoryAdapterError as exc:
        return _cli_error(exc)
    except Exception as exc:  # noqa: BLE001 - classified, value-free
        _emit(
            {
                "status": MemoryStatus.UNAVAILABLE.value,
                "code": _classify_provider_exception(exc).value,
                "provider": PROVIDER_LABEL,
                "operation": "export",
                "error_type": type(exc).__name__,
            }
        )
        return 0
    # The manifest IS the data (the per-bank export the ADR backup rule
    # leans on); it is passed through unmodified and can be large.
    _emit({"status": MemoryStatus.OK.value, "manifest": document})
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mai.memory.hindsight",
        description="Bank-scoped operational memory over the bundled "
        "Hindsight instance (operator entrypoint; the product path is the "
        "policy-bound capability dispatch).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(service: argparse.ArgumentParser, *, scope: bool) -> None:
        service.add_argument("--base-url", required=True, help="Hindsight API root")
        if scope:
            service.add_argument("--scope", required=True, help='"server" or "shared:<name>"')
            service.add_argument(
                "--shared-banks",
                default="",
                help="comma-separated configured shared bank names",
            )
        service.add_argument("--timeout-seconds", type=float, default=None)

    status = sub.add_parser("status", help="availability + version probe")
    common(status, scope=False)

    recall = sub.add_parser("recall", help="bounded cited recall")
    common(recall, scope=True)
    recall.add_argument("--query", required=True)
    recall.add_argument("--types", default=None, help="comma-separated fact types")
    recall.add_argument("--budget", default=None, choices=list(VALID_BUDGETS))
    recall.add_argument("--max-tokens", type=int, default=None)
    recall.add_argument("--max-text-chars", type=int, default=DEFAULT_MAX_TEXT_CHARS)

    retain = sub.add_parser("retain", help="synchronous retain with receipt")
    common(retain, scope=True)
    retain.add_argument("--item", action="append", required=True, help="content text")

    reflect = sub.add_parser("reflect", help="bounded generated answer")
    common(reflect, scope=True)
    reflect.add_argument("--query", required=True)
    reflect.add_argument("--max-tokens", type=int, default=None)
    reflect.add_argument("--max-answer-chars", type=int, default=DEFAULT_REFLECT_CHARS)

    delete_memories = sub.add_parser(
        "delete-memories", help="delete recalled memories of the scope"
    )
    common(delete_memories, scope=True)
    delete_memories.add_argument("--type", dest="fact_type", default=None)

    delete_bank = sub.add_parser(
        "delete-bank", help="delete a whole bank (operator action)"
    )
    common(delete_bank, scope=True)
    delete_bank.add_argument("--yes", action="store_true", required=True, help="confirm deletion")

    export = sub.add_parser("export", help="bank export manifest")
    common(export, scope=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: exit 0 operation ran (JSON status distinguishes
    ok / unavailable) / 1 structured rejection / 2 usage error."""
    args = _build_parser().parse_args(argv)
    if args.command == "status":
        return asyncio.run(_status_async(args))
    if args.command == "recall":
        return asyncio.run(_recall_async(args))
    if args.command == "retain":
        return asyncio.run(_retain_async(args))
    if args.command == "reflect":
        return asyncio.run(_reflect_async(args))
    if args.command == "delete-memories":
        return asyncio.run(_delete_async(args, bank=False))
    if args.command == "delete-bank":
        return asyncio.run(_delete_async(args, bank=True))
    if args.command == "export":
        return asyncio.run(_export_async(args))
    return 2  # pragma: no cover - argparse `required=True` guards this


if __name__ == "__main__":  # pragma: no cover - argparse glue
    sys.exit(main())
