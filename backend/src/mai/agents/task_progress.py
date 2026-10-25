"""Task-local no-progress signals and bounded strategy-change hints (22).

Card ``22-progress`` against act_CONTRACTS **§10** (investigation and memory
boundary): *bounded no-progress signatures distinguish duplicate searches,
recurring errors, expected async polling and real new evidence; default
signals steer/explain, not arbitrary hard failure on two tries; negative
findings are scoped to the source actually checked.*

The module is ONE pure derivation over a run's OWN conversation artifacts —
the frozen ``ToolResult`` documents that already ride the checkpointed
PydanticAI history (06-context-continuity) plus the current round's result
map. It owns no storage, no store access and no cross-run state:

- **Task-local by construction** — every input comes from the deriving
  run's own history/result map; one task's observations can never leak
  into another task's signal (scenario I1's isolation half and AC 2's
  "no unbounded cross-task memory": memory retained across tasks is
  exactly zero bytes, not a bounded number).
- **No chain-of-thought storage** (AC 2) — the signal document carries
  only bounded value-free facts: counts, short digests/fingerprints,
  capability identifiers and the hint sentence. Model reasoning parts
  are never an input; output text is folded into a short digest and the
  original text is never retained.
- **Never a false nonexistence claim** (scenario I1 / AC 1) — a hint says
  what was repeated and suggests a strategy change (change the search
  terms / widen the source scope); it explicitly disclaims absence and
  is scoped to the exact capability + query fingerprint it observed.
- **Not a hard failure** (§10) — the derivation's only output is a
  bounded advisory document; below the thresholds it stays silent, and
  the caller (``mai.agents.run``) renders it as a ``[Progress hint]``
  round prefix. Nothing here blocks, fails or re-dispatches anything.

Boundary: task-local *questions and hypotheses* (§10 first sentence) are
the task store's records (``mai.agents.task_store`` / ``OpenQuestion``);
this module adds only the bounded no-progress signal derivation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "ABSENT_OUTPUT_DIGEST",
    "EMPTY_OUTPUT_DIGEST",
    "FLAT_POLL_THRESHOLD",
    "MAX_HINT_CHARS",
    "MAX_REASON_CHARS",
    "MAX_SCOPE_ITEMS",
    "OBSERVATION_WINDOW",
    "OUTPUT_DIGEST_CHARS",
    "REPEAT_THRESHOLD",
    "SCHEMA_VERSION",
    "ProgressSignal",
    "SignalKind",
    "ToolObservation",
    "derive_progress_signal",
    "observation_from_result",
    "observations_from_history",
    "observations_from_round_results",
    "progress_hint_document",
    "render_hint_text",
]

#: Payload schema version of the emitted signal document. Consumers fail
#: closed on any other value (server-contract pattern).
SCHEMA_VERSION: Final[int] = 1

#: Maximum trailing observations considered (the derivation window). The
#: history itself is already bounded by ``MAX_HISTORY_BYTES``; this window
#: keeps the derivation cost and the emitted document small.
OBSERVATION_WINDOW: Final[int] = 12

#: Repeats of one equivalent empty search / same refusal / same unknown
#: effect before the strategy-change hint fires. Deliberately above two
#: (§10: "not arbitrary hard failure on two tries"): two tries stay
#: silent, the third equivalent observation emits the bounded hint.
REPEAT_THRESHOLD: Final[int] = 3

#: Repeats of one equivalent call returning UNCHANGED non-empty output
#: before the bounded flat-poll note fires. Below it, identical repeats
#: of a read are expected bounded polling (scenario I1) — never a stall.
FLAT_POLL_THRESHOLD: Final[int] = 6

#: The hint sentence is cut to at most this many characters.
MAX_HINT_CHARS: Final[int] = 600

#: One refusal/unknown reason label rides the hint bounded to this many
#: characters (reasons are value-free vocabulary codes; the bound is
#: defense in depth, never redaction).
MAX_REASON_CHARS: Final[int] = 80

#: Distinct capability identifiers carried as the signal's source scope.
MAX_SCOPE_ITEMS: Final[int] = 4

#: Digest length (hex chars) of the request-equivalence fingerprint and
#: the output-content digest. Short by design: identities for comparison,
#: never content carriers.
FINGERPRINT_CHARS: Final[int] = 12
OUTPUT_DIGEST_CHARS: Final[int] = 16

#: Sentinel digests. ``EMPTY``: the call executed and returned no content
#: (or a trivial empty marker) — the repeated-empty signal's input.
#: ``ABSENT``: the document is a value-free receipt rebuild without an
#: output carrier (crash-window/reconciliation replays) — honesty requires
#: treating the output as unproven, never as empty.
EMPTY_OUTPUT_DIGEST: Final[str] = "empty"
ABSENT_OUTPUT_DIGEST: Final[str] = "absent"

#: Output texts treated as "no content returned" (exact, whitespace-
#: stripped, case-folded). Deliberately conservative: anything else is
#: real output with a real digest.
_EMPTY_OUTPUT_TEXTS: Final[frozenset[str]] = frozenset({"", "[]", "{}", "null"})

#: ``ToolResult`` statuses the derivation can interpret (the frozen
#: ``ToolStatus`` vocabulary minus ``approval_required`` — an approval
#: wait is neither progress nor a refusal). Any other status document
#: (e.g. a boundary's ``not_executed`` wire note) is not an observation.
_TRACKED_STATUSES: Final[frozenset[str]] = frozenset(
    {"executed", "denied", "unknown_effect"}
)


class SignalKind(StrEnum):
    """The bounded signal vocabulary (§10; scenario I1/I2 rows)."""

    #: No equivalent repetition observed (fresh investigation).
    FRESH = "fresh"
    #: New evidence: the latest equivalent call's output differs from the
    #: previous one — legitimate polling/iteration (scenario I1's "normal
    #: polling", I2's contradicting evidence). Never a stall.
    NEW_EVIDENCE = "new_evidence"
    #: Unchanged non-empty repeats below the flat-poll bound — expected
    #: bounded polling of an active operation. Never a stall.
    EXPECTED_POLLING = "expected_polling"
    #: Equivalent calls kept returning NO output — strategy-change hint.
    REPEATED_EMPTY = "repeated_empty"
    #: Equivalent calls kept failing with the SAME refusal — strategy-
    #: change hint scoped to the exact request.
    REPEATED_ERROR = "repeated_error"
    #: Equivalent calls remain outcome-unknown — a hint that steers to
    #: reconciliation instead of a re-dispatch (guard rule 2).
    REPEATED_UNKNOWN = "repeated_unknown"
    #: Equivalent polls keep returning UNCHANGED non-empty output past
    #: the bounded expectation — a note, still never a failure claim.
    FLAT_POLL = "flat_poll"


@dataclass(frozen=True, slots=True)
class ToolObservation:
    """One bounded, value-free tool-outcome observation.

    ``fingerprint`` is the request-equivalence identity (capability +
    canonical payload hash); ``output_digest`` folds the output text so
    equivalence-of-results is comparable without retaining any text.
    """

    capability_id: str
    payload_hash: str
    status: str
    reason: str
    fingerprint: str
    output_digest: str


@dataclass(frozen=True, slots=True)
class ProgressSignal:
    """The derived bounded signal for one derivation window."""

    kind: SignalKind
    equivalent_repeats: int
    window: int
    scope: tuple[str, ...]
    fingerprint: str | None
    hint: str | None

    def to_document(self) -> dict[str, object]:
        """The bounded value-free document (no content, no reasoning)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": self.kind.value,
            "equivalent_repeats": self.equivalent_repeats,
            "window": self.window,
            "scope": list(self.scope),
            "fingerprint": self.fingerprint,
            "hint": self.hint,
        }


def _short_digest(value: str, length: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def observation_from_result(result_text: object) -> ToolObservation | None:
    """Fold ONE frozen ``ToolResult`` document into an observation.

    Non-documents, untracked statuses and malformed shapes yield ``None``
    (fail-closed inert: a hint must never crash on an odd result). The
    capability id and payload hash come from the document itself, so the
    fingerprint is exactly the dispatcher's request identity (C03 hash
    discipline) — "equivalent repeated searches" means identical canonical
    payloads, never a guessed semantic similarity.
    """
    if not isinstance(result_text, str) or not result_text:
        return None
    try:
        document = json.loads(result_text)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    capability_id = document.get("capability_id")
    status = document.get("status")
    if not isinstance(capability_id, str) or not capability_id:
        return None
    if not isinstance(status, str) or status not in _TRACKED_STATUSES:
        return None
    payload_hash = document.get("payload_hash")
    if not isinstance(payload_hash, str) or not payload_hash:
        payload_hash = "non_canonical"
    reason = document.get("reason")
    reason_text = str(reason) if isinstance(reason, str) else ""
    # The frozen wire shape (19-tool-results) carries the output as the
    # nested ``output`` object (``{text, truncated, omitted_bytes}``);
    # legacy ``output_text`` is accepted for tolerance. Anything else is
    # a receipt-only rebuild: the output is unproven, never empty.
    output = document.get("output")
    output_text: object = None
    if isinstance(output, Mapping):
        output_text = output.get("text")
    elif "output" not in document and "output_text" in document:
        output_text = document.get("output_text")
    if status == "executed":
        if not isinstance(output_text, str):
            # Receipt-only rebuild: the output is unproven, never empty.
            digest = ABSENT_OUTPUT_DIGEST
        else:
            stripped = output_text.strip().lower()
            digest = (
                EMPTY_OUTPUT_DIGEST
                if stripped in _EMPTY_OUTPUT_TEXTS
                else _short_digest(stripped, OUTPUT_DIGEST_CHARS)
            )
    else:
        # A refusal/unknown's identity is its value-free reason label.
        digest = _short_digest(reason_text[:MAX_REASON_CHARS], OUTPUT_DIGEST_CHARS)
    return ToolObservation(
        capability_id=capability_id,
        payload_hash=payload_hash,
        status=status,
        reason=reason_text[:MAX_REASON_CHARS],
        fingerprint=_short_digest(
            f"{capability_id}\x00{payload_hash}", FINGERPRINT_CHARS
        ),
        output_digest=digest,
    )


def observations_from_history(history_json: object) -> list[ToolObservation]:
    """The bounded chronological observations from one round history.

    Walks the PydanticAI message history's tool-return parts (the frozen
    ``ToolResult`` documents the model already received) and keeps the
    last :data:`OBSERVATION_WINDOW` of them. Unparsable history yields
    ``[]`` (inert: a hint derivation must never break a round).
    """
    if not isinstance(history_json, str) or not history_json:
        return []
    try:
        from pydantic_ai.messages import ModelMessagesTypeAdapter

        messages = ModelMessagesTypeAdapter.validate_json(history_json.encode("utf-8"))
    except Exception:
        return []
    raw: list[str] = []
    for message in messages:
        for part in getattr(message, "parts", ()) or ():
            if getattr(part, "part_kind", None) != "tool-return":
                continue
            content = getattr(part, "content", None)
            if isinstance(content, str):
                raw.append(content)
    observations = [
        observation
        for content in raw
        if (observation := observation_from_result(content)) is not None
    ]
    return observations[-OBSERVATION_WINDOW:]


def observations_from_round_results(
    round_results_json: object,
) -> list[ToolObservation]:
    """The observations of one round's result map (``{"calls": {...}}``).

    Key order follows the canonical (sorted) map; each value is one
    frozen ``ToolResult`` document. Boundary wire notes (``not_executed``)
    and malformed values are not observations.
    """
    if not isinstance(round_results_json, str) or not round_results_json:
        return []
    try:
        document = json.loads(round_results_json)
    except ValueError:
        return []
    calls = document.get("calls") if isinstance(document, dict) else None
    if not isinstance(calls, dict):
        return []
    observations = [
        observation
        for value in calls.values()
        if (observation := observation_from_result(value)) is not None
    ]
    return observations[-OBSERVATION_WINDOW:]


def derive_progress_signal(
    observations: Sequence[ToolObservation],
) -> ProgressSignal:
    """Derive the bounded no-progress signal (pure; §10 signatures).

    Only the TRAILING run of one equivalent request (same fingerprint) is
    judged, and only its trailing same-result streak counts: an alternat-
    ing or interrupted pattern never accumulates into a false stall, and
    any changed output inside the equivalent run is new evidence that
    resets the streak (scenario I1: "bounded expected polling is not
    falsely a stall"; I2: new live evidence stays productive).
    """
    window = tuple(observations[-OBSERVATION_WINDOW:])
    if not window:
        return ProgressSignal(SignalKind.FRESH, 0, 0, (), None, None)
    last = window[-1]
    tail: list[ToolObservation] = []
    for observation in reversed(window):
        if observation.fingerprint != last.fingerprint:
            break
        tail.append(observation)
    tail.reverse()
    discriminator = last.output_digest
    streak = 0
    for observation in reversed(tail):
        if observation.output_digest != discriminator:
            break
        streak += 1
    kind = SignalKind.FRESH
    hint: str | None = None
    if last.status == "executed":
        if discriminator == EMPTY_OUTPUT_DIGEST:
            if streak >= REPEAT_THRESHOLD:
                kind = SignalKind.REPEATED_EMPTY
                hint = (
                    f"{streak} equivalent {last.capability_id} calls in this "
                    f"task (query fingerprint {last.fingerprint}) returned no "
                    "new output. Change the search terms or widen the source "
                    "scope before repeating the identical query; no output "
                    "here is not proof of absence."
                )
        elif streak == 1:
            kind = SignalKind.NEW_EVIDENCE
        elif streak < FLAT_POLL_THRESHOLD:
            kind = SignalKind.EXPECTED_POLLING
        else:
            kind = SignalKind.FLAT_POLL
            hint = (
                f"{streak} polls of the same {last.capability_id} operation "
                f"(query fingerprint {last.fingerprint}) returned unchanged "
                "output. Expected bounded polling is fine; to make progress, "
                "check the operation's own status or observe something new "
                "instead of repeating the identical request."
            )
    elif last.status == "denied":
        if streak >= REPEAT_THRESHOLD:
            kind = SignalKind.REPEATED_ERROR
            hint = (
                f"{streak} equivalent {last.capability_id} calls in this "
                f"task (query fingerprint {last.fingerprint}) returned the "
                f"same refusal ({last.reason}). Change the approach or the "
                "request before repeating; the refusal is scoped to this "
                "exact query, not a general verdict."
            )
    else:  # unknown_effect
        if streak >= REPEAT_THRESHOLD:
            kind = SignalKind.REPEATED_UNKNOWN
            hint = (
                f"{streak} equivalent {last.capability_id} calls in this "
                f"task (query fingerprint {last.fingerprint}) remain "
                "outcome-unknown. Never re-request the identical operation; "
                "reconcile the recorded attempt or check a different "
                "observation."
            )
    scope: list[str] = []
    for observation in window:
        if observation.capability_id not in scope:
            scope.append(observation.capability_id)
        if len(scope) >= MAX_SCOPE_ITEMS:
            break
    if hint is not None:
        hint = hint[:MAX_HINT_CHARS]
    return ProgressSignal(
        kind=kind,
        equivalent_repeats=streak,
        window=len(window),
        scope=tuple(scope),
        fingerprint=last.fingerprint if kind is not SignalKind.FRESH else None,
        hint=hint,
    )


def progress_hint_document(
    history_json: object, round_results_json: object
) -> dict[str, object] | None:
    """The checkpointed derivation entry point (``mai.agents.run`` hook).

    Combines the history's prior-round observations with the current
    round's result map (chronological: history first) and returns the
    bounded hint document — or ``None`` when the signal stays below the
    thresholds (the default: silence, never a hard failure).
    """
    observations = [
        *observations_from_history(history_json),
        *observations_from_round_results(round_results_json),
    ]
    signal = derive_progress_signal(observations)
    return signal.to_document() if signal.hint is not None else None


def render_hint_text(document: object) -> str:
    """The bounded hint sentence of one signal document (fail-closed).

    Malformed or foreign documents render as an empty string — the caller
    then injects nothing, and a round never fails because of a hint.
    """
    if not isinstance(document, dict):
        return ""
    hint = document.get("hint")
    if not isinstance(hint, str):
        return ""
    return hint[:MAX_HINT_CHARS]
