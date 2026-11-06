"""Stable-id session compaction — the ONE session compactor (D05).

Campaign ``machina-session-first-chat`` D05 (sfc_PLAN.md D05;
sfc_RESEARCH §5). Compaction NEVER rotates ``session_id``, NEVER deletes or
mutates original ``session_entries``, and is the SOLE session compaction
writer (no harness compaction machinery, no second writer): it appends ONE
``CompactionEntry`` covering an EXPLICIT source range; the D03 projector
(``_latest_compaction``/``_compaction_anchor``) supersedes the covered
range with the labelled anchor.

Governing rules:

- **Stable id, immutable originals** — the covered range is explicit
  (``from_seq``..``to_seq``) and the compaction is idempotent PER RANGE
  (durable id ``compaction:{from_seq}:{to_seq}`` under the session-row
  fence): re-running returns the EXISTING record — never a second summary,
  never double coverage. ``session_id`` never rotates; the compaction is
  just another appended entry.
- **Complete-tool-group boundary** — the covered range always ends where
  every assistant tool call so far has its matching result inside the
  range; an incomplete group is never silently covered (the range is cut
  at the last complete boundary and the pending group stays uncovered).
- **Retry-then-fallback** — the summarizer gets ONE retry (2 attempts
  total); on failure (or unavailability) a DETERMINISTIC bounded fallback
  digest lands instead, explicitly labelled. A valid bounded
  ``CompactionEntry`` is always produced — never a silent skip.
- **Provenance** — the record carries the producing ``model_profile``;
  real usage anchors ride the summarizer seam (``source_usage_tokens`` is
  ``None`` at this seam — an estimate is an input to the trigger decision,
  never a claim about provider usage).
- **Config seam** — ``MAI_SESSION_COMPACTION_THRESHOLD_TOKENS`` /
  ``MAI_SESSION_COMPACTION_TARGET_TOKENS``; values are clamped (a config
  value can lower, never exceed, the maxima). No implicit env reads: the
  bounds take an explicit mapping (callers pass ``os.environ``).

The module imports none of the deleted-at-D09 conversation/task/objective
stores and none of the harness.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from mai.contracts.sessions import (
    AssistantMessageEntry,
    CompactionEntry,
    SessionEntryView,
    ToolResultEntry,
)
from mai.session_store import (
    HISTORY_PAGE_MAX_ENTRIES,
    SessionEntryRow,
    alist_compactions,
    aread_session_history,
    arecord_compaction,
)

__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_COMPACTION_TARGET_TOKENS",
    "DEFAULT_COMPACTION_THRESHOLD_TOKENS",
    "MAX_COMPACTION_PAGES",
    "MAX_COMPACTION_SCAN_ENTRIES",
    "MAX_COMPACTION_TARGET_TOKENS",
    "MAX_COMPACTION_THRESHOLD_TOKENS",
    "SUMMARIZER_ATTEMPTS",
    "SUMMARIZER_UNAVAILABLE_MARKER",
    "acompact_session_head",
    "complete_boundary_seq",
    "configured_bounds",
    "estimate_entries_tokens",
]

# --- bounds and the explicit config seam -------------------------------------------

DEFAULT_COMPACTION_THRESHOLD_TOKENS: Final[int] = 60_000
DEFAULT_COMPACTION_TARGET_TOKENS: Final[int] = 24_000
MAX_COMPACTION_THRESHOLD_TOKENS: Final[int] = 500_000
MAX_COMPACTION_TARGET_TOKENS: Final[int] = 250_000

#: The total uncovered-entry scan budget (bounded page walk).
MAX_COMPACTION_SCAN_ENTRIES: Final[int] = 800

#: The page-fetch budget for one compaction decision (R05 F3): the walk
#: stops at the coverage floor, so this cap bounds a pathological scan —
#: pages stay proportional to the UNCOVERED region, never to the history.
MAX_COMPACTION_PAGES: Final[int] = 16

#: Summarizer attempts: ONE retry after the first failure.
SUMMARIZER_ATTEMPTS: Final[int] = 2

#: The explicit marker inside the deterministic fallback digest.
SUMMARIZER_UNAVAILABLE_MARKER: Final[str] = (
    "[summarizer unavailable; deterministic bounded fallback]"
)

#: Conservative structural estimate (chars per token).
CHARS_PER_TOKEN: Final[int] = 4

_THRESHOLD_ENV: Final[str] = "MAI_SESSION_COMPACTION_THRESHOLD_TOKENS"
_TARGET_ENV: Final[str] = "MAI_SESSION_COMPACTION_TARGET_TOKENS"


def _bounded_int(value: object, default: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return min(number, maximum)


def configured_bounds(env: Mapping[str, str] | None = None) -> tuple[int, int]:
    """``(threshold, target)`` from the explicit config seam (clamped)."""
    source: Mapping[str, str] = os.environ if env is None else env
    threshold = _bounded_int(
        source.get(_THRESHOLD_ENV),
        DEFAULT_COMPACTION_THRESHOLD_TOKENS,
        MAX_COMPACTION_THRESHOLD_TOKENS,
    )
    target = _bounded_int(
        source.get(_TARGET_ENV),
        DEFAULT_COMPACTION_TARGET_TOKENS,
        min(MAX_COMPACTION_TARGET_TOKENS, threshold),
    )
    return threshold, target


# --- structural estimation --------------------------------------------------------


def _payload_text(payload: object) -> str:
    text = getattr(payload, "text", None)
    return text if isinstance(text, str) else ""


def estimate_entries_tokens(entries: Sequence[SessionEntryView]) -> int:
    """Conservative structural estimate over the entries' serialized text
    (an input to the trigger decision — never a usage claim)."""
    import json as _json

    chars = 0
    for entry in entries:
        chars += len(_payload_text(entry.payload))
        chars += len(_json.dumps(entry.payload.model_dump(mode="json"), default=str))
    return max(1, chars // CHARS_PER_TOKEN)


# --- range selection (complete tool-group boundary) --------------------------------


def complete_boundary_seq(entries: Sequence[SessionEntryView]) -> int | None:
    """The last sequence in ``entries`` that ends a COMPLETE tool-group
    boundary: every assistant tool call so far has its matching result
    inside the range. Lifecycle/control entries are transparent."""
    pending: set[str] = set()
    boundary: int | None = None
    for entry in entries:
        payload = entry.payload
        if isinstance(payload, AssistantMessageEntry) and payload.tool_calls:
            for call in payload.tool_calls:
                pending.add(call.call_id)
        elif isinstance(payload, ToolResultEntry):
            pending.discard(payload.call_id)
        if not pending:
            boundary = entry.seq
    return boundary


# --- the compaction entry point -----------------------------------------------------


async def acompact_session_head(
    sessions: Any,
    *,
    session_id: Any,
    server_id: Any,
    model_profile: str,
    env: Mapping[str, str] | None = None,
    summarize: Callable[[str], str] | None = None,
) -> dict[str, object]:
    """Compact the covered head of the session when the threshold is due.

    The decision is structural: uncovered-entry token estimate vs
    ``threshold``; the window is the OLDEST uncovered entries up to
    ``target``, cut at a complete tool-group boundary. The session-row
    fence serializes concurrent compactors; the range key makes a re-run
    over the SAME range return the EXISTING record (``created=False``).

    Returns a value-free decision document: ``compacted``, ``record``
    (``CompactionEntry`` or ``None``), ``created``, ``covered_range``,
    ``estimated_tokens``, ``summary_source`` (``model`` | ``fallback`` |
    ``existing``) and ``reason`` for a no-op.
    """
    threshold, target = configured_bounds(env)
    compactions = await alist_compactions(
        sessions, session_id=session_id, server_id=server_id, limit=16
    )
    floor_seq = 0
    for row in compactions:
        covered_to = int(row.payload.get("to_seq", 0) or 0)
        floor_seq = max(floor_seq, covered_to)

    uncovered, pages_fetched = await _collect_uncovered(
        sessions, session_id=session_id, server_id=server_id, floor_seq=floor_seq
    )
    if not uncovered:
        return _decision(
            False, None, False, 0, None, "none", "no_uncovered_entries", pages_fetched
        )
    total_estimate = estimate_entries_tokens(uncovered)
    if total_estimate <= threshold:
        return _decision(
            False,
            None,
            False,
            total_estimate,
            None,
            "none",
            "below_threshold",
            pages_fetched,
        )

    # The window: oldest entries first, up to the target estimate.
    window: list[SessionEntryView] = []
    for entry in uncovered:
        window.append(entry)
        if estimate_entries_tokens(window) >= target:
            break
    boundary = complete_boundary_seq(window)
    first_seq = window[0].seq
    if boundary is None or boundary < first_seq:
        return _decision(
            False,
            None,
            False,
            total_estimate,
            None,
            "none",
            "no_complete_boundary",
            pages_fetched,
        )
    covered = [entry for entry in window if entry.seq <= boundary]
    from_seq = covered[0].seq
    to_seq = covered[-1].seq

    summary_text, summary_source = _summarize_with_retry(
        summarize, covered, from_seq, to_seq
    )
    import uuid as _uuid

    row, created = await arecord_compaction(
        sessions,
        session_id=session_id,
        server_id=server_id,
        compaction=CompactionEntry(
            kind="compaction",
            compaction_id=_uuid.uuid4(),
            from_seq=from_seq,
            to_seq=to_seq,
            summary_text=summary_text,
            model_profile=model_profile,
            token_usage_anchor=None,
        ),
    )
    payload = CompactionEntry.model_validate(row.payload)
    return _decision(
        True,
        payload,
        created,
        total_estimate,
        (payload.from_seq, payload.to_seq),
        "existing" if not created else summary_source,
        "compacted",
        pages_fetched,
    )


def _decision(
    compacted: bool,
    record: CompactionEntry | None,
    created: bool,
    estimated_tokens: int,
    covered_range: tuple[int, int] | None,
    summary_source: str,
    reason: str,
    pages_fetched: int = 0,
) -> dict[str, object]:
    return {
        "compacted": compacted,
        "record": record,
        "created": created,
        "estimated_tokens": estimated_tokens,
        "covered_range": covered_range,
        "summary_source": summary_source,
        "reason": reason,
        "pages_fetched": pages_fetched,
    }


async def _collect_uncovered(
    sessions: Any,
    *,
    session_id: Any,
    server_id: Any,
    floor_seq: int,
) -> tuple[list[SessionEntryView], int]:
    """The uncovered entries, newest-first page walk (bounded pages, bounded
    total), returned chronological — plus the real page-fetch count.

    R05 F3: the walk STOPS as soon as a page reaches the coverage floor
    (``oldest_seq <= floor_seq``): every older page is fully covered by the
    existing compaction, so pages stay proportional to the UNCOVERED
    region, never to the whole history.
    """
    collected: list[SessionEntryView] = []
    pages_fetched = 0
    cursor: int | None = None
    while len(collected) < MAX_COMPACTION_SCAN_ENTRIES and pages_fetched < MAX_COMPACTION_PAGES:
        page = await aread_session_history(
            sessions,
            session_id=session_id,
            server_id=server_id,
            limit=min(64, HISTORY_PAGE_MAX_ENTRIES),
            before_seq=cursor,
        )
        pages_fetched += 1
        if not page.items:
            break
        for entry in reversed(page.items):
            if entry.seq > floor_seq:
                collected.append(entry)
            else:
                break
        # The coverage floor sits inside (or below) this page: every older
        # page is fully covered — the uncovered region is fully traversed.
        if page.oldest_seq is not None and page.oldest_seq <= floor_seq:
            break
        if len(collected) >= MAX_COMPACTION_SCAN_ENTRIES:
            break
        if not page.has_older or page.next_before_seq is None:
            break
        cursor = page.next_before_seq
    collected.sort(key=lambda entry: entry.seq)
    return collected, pages_fetched


# --- summarization (retry-then-fallback) ---------------------------------------------


def _summarize_with_retry(
    summarize: Callable[[str], str] | None,
    covered: Sequence[SessionEntryView],
    from_seq: int,
    to_seq: int,
) -> tuple[str, str]:
    """ONE retry, then the deterministic bounded fallback digest."""
    if summarize is not None:
        prompt_text = _summary_prompt(covered, from_seq, to_seq)
        for _attempt in range(SUMMARIZER_ATTEMPTS):
            try:
                text = summarize(prompt_text)
            except Exception:
                continue
            if isinstance(text, str) and text.strip():
                return text.strip()[:4000], "model"
    return _fallback_digest(covered, from_seq, to_seq), "fallback"


def _summary_prompt(
    covered: Sequence[SessionEntryView], from_seq: int, to_seq: int
) -> str:
    lines = [
        f"Summarize session entries {from_seq}..{to_seq} for future model "
        "context. Preserve user constraints and unresolved obligations. "
        "Be bounded.",
    ]
    for entry in covered:
        payload = entry.payload
        kind = type(payload).__name__
        text = _payload_text(payload)
        line = f"[{entry.seq}] {kind}"
        if text.strip():
            line += f": {text[:200]}"
        calls = getattr(payload, "tool_calls", None)
        if calls:
            line += f": tool calls {', '.join(c.call_id for c in calls)}"
        lines.append(line)
    return "\n".join(lines)[:8000]


def _fallback_digest(
    covered: Sequence[SessionEntryView], from_seq: int, to_seq: int
) -> str:
    """The deterministic bounded fallback digest (value-free structure):
    entry kinds + counts + tool-call ids; labelled as unavailable."""
    kinds: Counter[str] = Counter()
    tool_calls: list[str] = []
    for entry in covered:
        payload = entry.payload
        kinds[type(payload).__name__] += 1
        calls = getattr(payload, "tool_calls", None)
        if calls:
            for call in calls:
                tool_calls.append(call.call_id)
    counts = ", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items()))
    body = (
        f"{SUMMARIZER_UNAVAILABLE_MARKER} deterministic bounded digest of "
        f"session entries {from_seq}..{to_seq}: entries ({counts or 'none'}); "
        f"tool calls: {', '.join(tool_calls) or 'none'}. User constraints "
        "and unresolved obligations remain in the retained originals."
    )
    return body[:4000]
