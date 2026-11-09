"""Native PydanticAI model projector and atomic tool-group continuity.

Campaign ``machina-session-first-chat`` D03 (sfc_PLAN.md D03; sfc_RESEARCH
§2) over the pinned ``pydantic-ai-slim[openai]==2.60.0``. This module is the
ONLY bridge between the durable session log (:mod:`mai.session_store`) and
native provider message history — the model receives committed domain facts,
never a rebuilt workflow log, and never the deleted-at-D09 conversation
seeds (no ``conversation_context``, no harness, no old task contracts).

Governing rules:

- **Zero synthetic flattening** — one domain entry maps to its native part
  shapes: ``UserMessageEntry`` → ``ModelRequest([UserPromptPart])`` (with a
  bounded provenance prefix for document/runbook attachments — untrusted
  text is never elevated into a ``SystemPromptPart``),
  ``AssistantMessageEntry`` → ``ModelResponse([ThinkingPart?] [TextPart?]
  [ToolCallPart...])``, matching ``ToolResultEntry`` items → one
  ``ModelRequest([ToolReturnPart | RetryPromptPart...])`` per complete group.
  ``RunStateEntry`` / ``ControlEntry`` (except an operator steering note,
  which enters as a bounded, labelled user prompt) / ``DelegationEntry``
  (the UI/domain record; the parent's delegation tool call + result carries
  the child summary natively) emit nothing.
- **Atomic tool groups only** — ``ToolCallPart`` is emitted ONLY for a
  complete group (exact call/result set match, no duplicates, no cross-scope
  targets, same run). Incomplete/pending groups keep their safe assistant
  text as a plain ``TextPart`` and drop the dangling calls; an orphan
  ``ToolResultEntry`` (R01 O1) is never replayable. A pending group can
  therefore never produce ``400 tool_call_id did not have a response``.
- **Provider artifact isolation** — ``ThinkingPart`` rehydrates ONLY when
  the artifact's ``(provider, model_profile)`` identity matches the target
  run and the artifact is not redacted; an incompatible provider/model
  never receives foreign thinking signatures. Raw thinking never enters
  ``AssistantMessageEntry.text``.
- **Compaction supersession** — entries covered by the latest
  ``CompactionEntry`` are replaced in the model window by one bounded,
  provenance-labelled ``SystemPromptPart`` anchor; originals stay immutable
  in the store for UI scrollback. ``session_id`` never rotates.
- **Safe tool results, anti-forgery** — untrusted output passes
  ``redact_text``/``project_text`` + explicit bounds BEFORE any envelope is
  built; the model-facing envelope stays under the dispatcher's 64 KiB
  budget with explicit truncation metadata; ``status="executed"`` with any
  unproven execution or non-certain effect raises
  :class:`SessionContractError` (never silently rewritten), and committed
  rows are re-validated on replay so a forged ``executed`` cannot ride back
  into the model context. Untrusted tool output is data inside the
  envelope's ``output`` key; it can never overwrite the frozen vocabulary
  fields, which are constructed from typed enums, never parsed from output.

Pure module: no process environment, filesystem, network or clock access —
committed rows carry the authoritative timestamps; capture drafts carry an
explicit epoch placeholder that the store's committed ``created_at``
replaces. Every byte bound comes from frozen contract constants.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Final

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryInfo,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents.output_safety import project_text, redact_text
from mai.agents.tools import ToolResult, ToolStatus as BridgeToolStatus
from mai.agents.tools import project_tool_result
from mai.contracts.sessions import (
    HISTORY_PAGE_MAX_ENTRIES,
    MAX_ERROR_DETAIL_CHARS,
    MAX_ENTRY_TEXT_CHARS,
    MAX_HASH_CHARS,
    MAX_IDENTITY_CHARS,
    MAX_SERVICE_REF_PREFIX_CHARS,
    MAX_SUMMARY_TEXT_CHARS,
    MAX_TOOL_OUTPUT_PREVIEW_CHARS,
    SERVICE_MAP_SECTION_HEADER,
    UNSAFE_TEXT_CLASS,
    AssistantMessageEntry,
    CompactionEntry,
    ControlActionKind,
    ControlEntry,
    EntryKind,
    ProviderReasoningArtifact,
    SessionContractError,
    SessionEntryView,
    SessionErrorCode,
    SessionFieldError,
    SessionHistoryPage,
    SessionSourceKind,
    ToolCallItem,
    ToolResultEntry,
    UserMessageEntry,
    partition_replayable_tool_groups,
)
from mai.contracts.tool_results import DomainOutcome, EffectCertainty, ExecutionStatus
from mai.session_store import (
    aappend_entry,
    aread_session_history,
    arecord_provider_artifact,
)

__all__ = [
    "MAX_CONTEXT_ENTRIES",
    "MAX_RESULT_ENVELOPE_BYTES",
    "RETRYABLE_TOOL_ERROR_CODES",
    "TRUNCATION_MARKER_TEMPLATE",
    "abuild_session_model_history",
    "apersist_run_exchange",
    "assert_tool_result_honesty",
    "build_native_model_history",
    "extract_entries_from_run_messages",
    "project_tool_result_entry",
    "project_replay_envelope",
]

# --- frozen bounds ---------------------------------------------------------------

#: The model-facing tool envelope cap = the frozen 64 KiB dispatcher/page
#: discipline (AGENTS.md §1; NOT ``MAX_OUTPUT_BUDGET_BYTES``, which is the
#: dispatcher's 1 MiB construction-DoS bound).
MAX_RESULT_ENVELOPE_BYTES: Final[int] = 64 * 1024

#: Explicit truncation marker (brief §4: never mistake a truncated preview
#: for complete output).
TRUNCATION_MARKER_TEMPLATE: Final[str] = "[truncated: {omitted} bytes omitted]"

#: Tool results that represent retryable validation/schema failures and MAY
#: replay as ``RetryPromptPart`` when retry mode is requested (the bridge's
#: stable schema-gate code; everything else replays as a terminal result).
RETRYABLE_TOOL_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"payload_schema_mismatch"}
)
_RETRY_PROMPT_ERROR_CODE: Final[str] = "payload_schema_mismatch"

#: Fail-safe context-walk bound (entries): the compaction boundary normally
#: stops the walk; this cap keeps a compaction-free session honest too.
MAX_CONTEXT_ENTRIES: Final[int] = 800


class RunHistoryScope(Enum):
    """D05 delegation isolation for the native model window: a CHILD run's
    window carries ONLY its own entries; a ROOT run's window NEVER carries
    child-run entries (a child's transcript is never spliced into the
    parent's native history)."""

    ROOT_RUN = "root"
    CHILD_RUN = "child"


async def _child_run_ids(
    sessions: Any, *, session_id: uuid.UUID, server_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """The session's delegated (child) run ids — the entries these carry
    are the child transcripts that never splice into a root's window."""
    from sqlalchemy import text as _sql_text

    from mai.session_store import _lock_session

    async with sessions() as session:
        async with session.begin():
            await _lock_session(session, session_id=session_id, server_id=server_id)
            rows = (
                await session.execute(
                    _sql_text(
                        "SELECT run_id FROM session_runs "
                        "WHERE session_id = :sid AND parent_run_id IS NOT NULL"
                    ),
                    {"sid": session_id},
                )
            ).scalars().all()
    return frozenset(uuid.UUID(str(row)) for row in rows)


#: Operator steering note bound (the D01 control-note bound).
_STEER_NOTE_MAX_CHARS: Final[int] = 500

#: Stable prefix labelling bounded operator steering prompts.
_STEER_PROMPT_PREFIX: Final[str] = "[operator steering] "

#: Stable prefix labelling the bounded compaction anchor (a FIXED label plus
#: the covered entry range; identities stay in structured fields, never in
#: model text).
_COMPACTION_ANCHOR_PREFIX: Final[str] = (
    "[compacted session history summary; provenance: session entries "
    "{from_seq}..{to_seq}; originals retained in the session log]\n"
)

#: Artifact content bound for entry-level references (the DB artifact row
#: carries up to 20000 chars; the entry reference is bound by the summary
#: contract so thinking summaries reach the chat UI without the old
#: identity-sized 128-char truncation).
_ARTIFACT_REF_CONTENT_MAX_CHARS: Final[int] = MAX_SUMMARY_TEXT_CHARS

#: Provider thinking/signature content is sanitized to the contract's
#: safe-text set BEFORE capture (provider text is untrusted; the durable
#: contract rejects control characters). The rejected class IS the
#: contract's own ``UNSAFE_TEXT_CLASS`` — one source of truth.
_PROVIDER_TEXT_CONTROL_RE: Final[re.Pattern[str]] = re.compile(
    f"[{UNSAFE_TEXT_CLASS}]"
)

#: Raw non-JSON tool-call arguments bound (fail-safe normalization).
_ARGS_RAW_MAX_CHARS: Final[int] = MAX_IDENTITY_CHARS

#: Capture-time placeholder timestamp (UTC epoch); the store's committed
#: ``created_at`` (database ``now()``) is the authoritative value.
_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: ``ToolReturnPart.outcome`` mapped from the frozen bridge statuses. Every
#: value is a member of the pinned adapter's NON-Optional Literal
#: ('success'|'failed'|'denied'|'interrupted'|'retried') — ``None`` is
#: rejected by ``ModelMessagesTypeAdapter.validate_json`` (R03 F1), so an
#: ``unknown_effect`` result maps by its execution layer: ``failed`` for a
#: wrapper that ran and failed, ``interrupted`` for an unproven/uncertain
#: outcome (did not produce a trusted result), never ``None``.
_OUTCOME_BY_STATUS: Final[dict[str, str]] = {
    "executed": "success",
    "denied": "denied",
    "approval_required": "interrupted",
}


def _outcome_for(payload: ToolResultEntry) -> str:
    """The adapter-valid native outcome for one tool result (R03 F1)."""
    if payload.status == "unknown_effect":
        if payload.execution_status is ExecutionStatus.FAILED:
            return "failed"
        return "interrupted"
    outcome = _OUTCOME_BY_STATUS.get(payload.status)
    if outcome is None:
        # Frozen vocabulary is closed; an unknown status cannot occur.
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "status",
                    "tool result carries an unknown frozen status",
                )
            ]
        )
    return outcome


def _value_of(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


# --- anti-forgery guards -----------------------------------------------------------


def assert_tool_result_honesty(entry: ToolResultEntry) -> None:
    """Reject any tool result claiming ``executed`` without proof.

    A committed or caller-supplied ``ToolResultEntry`` may claim
    ``status="executed"`` ONLY with a proven successful wrapper execution
    (``execution_status="succeeded"``) AND certain effect evidence
    (``effect_certainty="certain"``). Anything else — ``outcome_unknown``,
    ``uncertain`` or ``unknown`` certainty, a denial or an approval
    deferral — is a forgery attempt and raises
    :class:`SessionContractError` (never silently rewritten). Untrusted tool
    output is data: a JSON document inside ``output_preview`` cannot touch
    these frozen vocabulary fields, because they are constructed from typed
    enums, never parsed from output.
    """
    if entry.status != "executed":
        return
    proven = (
        entry.execution_status is ExecutionStatus.SUCCEEDED
        and entry.effect_certainty is EffectCertainty.CERTAIN
    )
    if not proven:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "status",
                    "status 'executed' requires execution_status 'succeeded' "
                    "and effect_certainty 'certain'; unproven or uncertain "
                    "results can never claim execution",
                )
            ]
        )


def _coerce_status(
    result: ToolResult,
    execution_status: ExecutionStatus,
    effect_certainty: EffectCertainty,
    supplied_status: BridgeToolStatus | None = None,
) -> BridgeToolStatus:
    """Derive the honest frozen status from the bridge result + projection.

    Session-contract discipline (brief §4 anti-forgery guard):

    - ``unknown_effect`` is FORCED whenever the wrapper execution is
      unproven (``outcome_unknown``) or the effect evidence is not certain
      (``uncertain``/``unknown``) — a result can never carry ``executed``
      without proven, certain evidence. This is the honest downgrade for
      arbitrary work whose output carries no structured success marker.
    - An EXPLICITLY supplied ``status="executed"`` that contradicts the
      evidence raises :class:`SessionContractError` — never silently
      rewritten. Explicit refusals (``denied``/``approval_required``) keep
      their own vocabulary: nothing executed there, so nothing to overclaim.
    """
    proven = (
        execution_status is ExecutionStatus.SUCCEEDED
        and effect_certainty is EffectCertainty.CERTAIN
        and result.executor_ok is True
    )
    if supplied_status is not None:
        if supplied_status is BridgeToolStatus.EXECUTED and not proven:
            raise SessionContractError(
                [
                    SessionFieldError(
                        SessionErrorCode.FORBIDDEN_FIELD,
                        "status",
                        "status 'executed' requires a proven successful "
                        "execution with certain effect evidence",
                    )
                ]
            )
        return supplied_status
    if result.status is BridgeToolStatus.EXECUTED:
        if proven:
            return BridgeToolStatus.EXECUTED
        unproven = execution_status is ExecutionStatus.OUTCOME_UNKNOWN or (
            effect_certainty
            in (EffectCertainty.UNCERTAIN, EffectCertainty.UNKNOWN)
        )
        if unproven:
            # The brief's MUST: unproven/uncertain results are
            # unknown_effect — never silently 'executed'.
            return BridgeToolStatus.UNKNOWN_EFFECT
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "status",
                    "executed result with a failed wrapper execution is a "
                    "contradiction; reconcile before projecting",
                )
            ]
        )
    return result.status


# --- safe tool-result projection ---------------------------------------------------


def _bound_with_marker(text: str, limit: int) -> tuple[str, bool, int]:
    """Bound ``text`` to ``limit`` chars including an honest truncation mark.

    Redaction runs BEFORE any bound (callers project through
    :func:`project_text` first), so a cut can never split a secret. The
    marker is formatted with the REAL omitted count first (its digit count
    affects the budget) and the kept body plus marker never exceed ``limit``.
    """
    if len(text) <= limit:
        return text, False, 0
    omitted = len(text) - limit
    marker = TRUNCATION_MARKER_TEMPLATE.format(omitted=omitted)
    keep = max(0, limit - len(marker) - 1)  # newline between body and marker
    return text[:keep] + "\n" + marker, True, omitted


def project_tool_result_entry(
    result: ToolResult,
    *,
    call_id: str,
    status: BridgeToolStatus | None = None,
    durable_source_id: str | None = None,
) -> ToolResultEntry:
    """Project one bridge result onto the D01 tool-result contract.

    Untrusted output is redacted and JSON-document-normalized via
    :func:`mai.agents.output_safety.project_text`, bounded to
    ``MAX_TOOL_OUTPUT_PREVIEW_CHARS`` WITH an explicit truncation marker,
    and the frozen status/execution/domain/certainty vocabularies are taken
    from the bridge's compact projection (act_CONTRACTS §9) — never from
    output content. An executed wrapper whose output carries no structured
    success evidence downgrades honestly to ``unknown_effect``. An
    EXPLICITLY supplied ``status`` that contradicts the evidence raises
    :class:`SessionContractError`. ``durable_source_id`` is advisory here
    (the capture path owns the deterministic id); it is accepted so call
    sites cannot forget the pairing discipline.
    """
    projection = project_tool_result(result)
    execution_status = ExecutionStatus(str(projection["execution_status"]))
    domain_outcome = DomainOutcome(str(projection["domain_outcome"]))
    effect_certainty = EffectCertainty(str(projection["effect_certainty"]))
    status = _coerce_status(
        result, execution_status, effect_certainty, supplied_status=status
    )

    raw_output = result.output_text or ""
    # Redact first (value forms + token patterns + JSON-document unwrap);
    # the marker-aware bound is OURS so the truncation marker is explicit
    # and the preview provably fits the contract bound.
    safe_output = project_text(raw_output)
    preview, preview_truncated, preview_omitted = _bound_with_marker(
        safe_output, MAX_TOOL_OUTPUT_PREVIEW_CHARS
    )
    upstream_omitted = int(result.output_omitted_bytes or 0)
    truncated = preview_truncated or bool(result.output_truncated)
    if truncated:
        omitted_total = preview_omitted + upstream_omitted
        if not preview_truncated:
            # The upstream bound did the cutting; surface its metadata.
            marker = TRUNCATION_MARKER_TEMPLATE.format(omitted=omitted_total)
            preview = (preview + "\n" + marker)[:MAX_TOOL_OUTPUT_PREVIEW_CHARS]
    return ToolResultEntry(
        kind=EntryKind.TOOL_RESULT,
        call_id=call_id,
        tool_name=result.capability_id,
        capability_id=result.capability_id,
        status=status.value,
        execution_status=execution_status,
        domain_outcome=domain_outcome,
        effect_certainty=effect_certainty,
        payload_hash=result.payload_hash,
        output_preview=preview if raw_output else None,
        error_code=result.error_code,
        error_detail=result.error_detail,
        approval_id=result.approval_id,
    )


def project_replay_envelope(
    entry: ToolResultEntry, *, limit: int = MAX_RESULT_ENVELOPE_BYTES
) -> dict[str, object]:
    """The bounded model-facing envelope replayed as ``ToolReturnPart.content``.

    Built from frozen vocabulary fields and the already-redacted
    ``output_preview`` — output text is DATA inside the ``output`` key and
    can never overwrite the outer envelope's ``status``/execution/outcome
    fields. The serialized size is checked against the dispatcher's 64 KiB
    budget; an over-budget preview is re-bounded (halving) with an explicit
    marker until it fits.
    """
    envelope: dict[str, object] = {
        "status": entry.status,
        "capability_id": entry.capability_id,
        "payload_hash": entry.payload_hash,
        "execution_status": entry.execution_status.value,
        "domain_outcome": entry.domain_outcome.value,
        "effect_certainty": entry.effect_certainty.value,
        "output": {"text": entry.output_preview, "artifact_ref": entry.artifact_ref},
        "error_code": entry.error_code,
        "error_detail": entry.error_detail,
    }
    while _envelope_bytes(envelope) > limit:
        output = envelope["output"]
        current = ""
        if isinstance(output, Mapping):
            current = str(output.get("text") or "")
        if not current:
            raise SessionContractError(
                [
                    SessionFieldError(
                        SessionErrorCode.FORBIDDEN_FIELD,
                        "output_preview",
                        "tool-result envelope cannot fit the dispatcher "
                        "budget even with an empty preview",
                    )
                ]
            )
        bounded, _truncated, _omitted = _bound_with_marker(
            current, max(1, len(current) // 2)
        )
        envelope["output"] = {
            "text": bounded,
            "artifact_ref": entry.artifact_ref,
        }
    return envelope


def _envelope_bytes(envelope: Mapping[str, object]) -> int:
    return len(json.dumps(envelope, ensure_ascii=False, default=str).encode("utf-8"))


# --- capture: native run messages -> durable session entries -----------------------


def extract_entries_from_run_messages(
    new_messages: Sequence[ModelMessage],
    *,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    model_profile: str,
    provider: str,
    durable_prefix: str = "run",
) -> list[SessionEntryView]:
    """Convert the ``ModelMessage``s of ONE PydanticAI run into durable
    :class:`SessionEntryView` drafts with deterministic durable source ids.

    Pairs every ``ToolCallPart`` of a ``ModelResponse`` with the matching
    ``ToolReturnPart``/``RetryPromptPart`` of the following ``ModelRequest``
    (single, parallel and retry shapes; ``BuiltinToolCallPart``/
    ``BuiltinToolReturnPart`` do not exist in the pinned 2.60.0 messages
    API — verified — and therefore cannot occur here). ``ThinkingPart``
    content becomes bounded :class:`ProviderReasoningArtifact` references on
    the assistant entry; :func:`apersist_run_exchange` additionally persists
    the full artifact row. An assistant message whose calls are not all
    answered within this exchange stays ``tool_group_state="pending"``
    (deferred/approval-required or crash window) — durable first, replay
    safety enforced at projection time by the atomic-group partition.
    Deterministic ``(run_id, durable_source_id, kind)`` ids make the persist
    idempotent across crash/replay windows.
    """
    drafts: list[SessionEntryView] = []
    pending_assistant: SessionEntryView | None = None

    def _draft(payload: Any, source_id: str) -> SessionEntryView:
        return SessionEntryView(
            entry_id=uuid.uuid4(),
            session_id=session_id,
            # Capture drafts are not committed history: seq 1 is a placeholder
            # that the store's locked allocation replaces on append.
            seq=1,
            run_id=run_id,
            durable_source_id=source_id,
            created_at=_EPOCH,
            payload=payload,
        )

    for index, message in enumerate(new_messages):
        if isinstance(message, ModelResponse):
            text = "".join(
                part.content for part in message.parts if isinstance(part, TextPart)
            )
            tool_calls: list[ToolCallItem] = []
            artifacts: list[ProviderReasoningArtifact] = []
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    tool_calls.append(
                        ToolCallItem(
                            call_id=part.tool_call_id,
                            tool_name=part.tool_name,
                            capability_id=part.tool_name,
                            args=_normalized_args(part.args),
                        )
                    )
                elif isinstance(part, ThinkingPart):
                    kind = "signature" if part.signature else "summary"
                    content = part.signature or part.content
                    # Provider text is untrusted: strip the control
                    # characters the durable contract rejects BEFORE the
                    # bound, so capture can never crash on malformed
                    # reasoning content.
                    content = _PROVIDER_TEXT_CONTROL_RE.sub("", content)
                    artifacts.append(
                        ProviderReasoningArtifact(
                            provider=provider,
                            artifact_kind=kind,
                            content=content[:_ARTIFACT_REF_CONTENT_MAX_CHARS],
                        )
                    )
            if not text and not tool_calls and not artifacts:
                continue  # a partless response carries nothing durable
            payload = AssistantMessageEntry(
                kind=EntryKind.ASSISTANT_MESSAGE,
                text=text,
                tool_calls=tool_calls,
                model_profile=model_profile,
                provider_artifacts=artifacts,
                tool_group_state="pending" if tool_calls else "none",
            )
            assistant_draft = _draft(payload, f"{durable_prefix}:{index}")
            drafts.append(assistant_draft)
            if tool_calls:
                pending_assistant = assistant_draft
            continue
        if isinstance(message, ModelRequest):
            results: list[tuple[str, ToolResultEntry]] = []
            prompts: list[tuple[str, UserMessageEntry]] = []
            answered_ids: set[str] = set()
            return_ordinal = 0
            prompt_ordinal = 0
            for part in message.parts:
                if isinstance(part, ToolReturnPart):
                    answered_ids.add(part.tool_call_id)
                    results.append(
                        (
                            f"{durable_prefix}:{index}:r{return_ordinal}",
                            _tool_result_from_return_part(part),
                        )
                    )
                    return_ordinal += 1
                elif isinstance(part, RetryPromptPart):
                    answered_ids.add(part.tool_call_id)
                    results.append(
                        (
                            f"{durable_prefix}:{index}:r{return_ordinal}",
                            _tool_result_from_retry_part(part),
                        )
                    )
                    return_ordinal += 1
                elif isinstance(part, UserPromptPart):
                    prompts.append(
                        (
                            f"{durable_prefix}:{index}:u{prompt_ordinal}",
                            _user_entry_payload(part),
                        )
                    )
                    prompt_ordinal += 1
            if results and pending_assistant is not None:
                payload = pending_assistant.payload
                if isinstance(payload, AssistantMessageEntry):
                    expected = {call.call_id for call in payload.tool_calls}
                    if expected and expected <= answered_ids:
                        marked = payload.model_copy(
                            update={"tool_group_state": "complete"}
                        )
                        completed = pending_assistant.model_copy(
                            update={"payload": marked}
                        )
                        position = _draft_position(drafts, pending_assistant)
                        drafts[position] = completed
                pending_assistant = None
            for source_id, user_payload in prompts:
                drafts.append(_draft(user_payload, source_id))
            for source_id, result_payload in results:
                drafts.append(_draft(result_payload, source_id))
            continue
    return drafts


def _draft_position(drafts: list[SessionEntryView], target: SessionEntryView) -> int:
    for position, draft in enumerate(drafts):
        if draft.durable_source_id == target.durable_source_id:
            return position
    raise AssertionError("unreachable: draft identity tracked by source id")


def _user_entry_payload(part: UserPromptPart) -> UserMessageEntry:
    return UserMessageEntry(
        kind=EntryKind.USER_MESSAGE,
        text=_bounded_user_text(part.content),
        client_request_id=uuid.uuid4(),
        source=SessionSourceKind.INTERACTIVE,
    )


def _normalized_args(args: str | dict[str, Any] | None) -> dict[str, Any]:
    """Normalize one provider tool-call ``args`` to a bounded JSON object."""
    if args is None:
        return {}
    if isinstance(args, Mapping):
        return dict(args)
    try:
        parsed = json.loads(args)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    return {"raw": redact_text(str(args))[:_ARGS_RAW_MAX_CHARS]}


def _bounded_user_text(content: Any) -> str:
    text = content if isinstance(content, str) else str(content)
    return text[:MAX_ENTRY_TEXT_CHARS]


def _tool_result_from_return_part(part: ToolReturnPart) -> ToolResultEntry:
    """Project one ``ToolReturnPart`` onto the tool-result contract.

    The content document is untrusted data: when it carries the trusted
    producer's frozen-vocabulary projection it is re-used verbatim and
    validated by the anti-forgery guard at projection time; otherwise the
    safe string projection is bounded into ``output_preview`` with the
    outcome-derived honest status. A schema-validation retry return (the
    pinned adapter converts invalid model arguments into
    ``outcome="retried"`` with ``RetryInfo(cause="validation_error")``)
    projects as a pre-execution denial carrying the retryable error code.
    """
    outcome = str(part.outcome) if part.outcome is not None else "failed"
    status_by_outcome: Final[dict[str, BridgeToolStatus]] = {
        "success": BridgeToolStatus.EXECUTED,
        "denied": BridgeToolStatus.DENIED,
        "interrupted": BridgeToolStatus.APPROVAL_REQUIRED,
        "retried": BridgeToolStatus.DENIED,
        "failed": BridgeToolStatus.UNKNOWN_EFFECT,
    }
    status = status_by_outcome.get(outcome, BridgeToolStatus.UNKNOWN_EFFECT)
    retry_cause = getattr(part.retry, "cause", None) if part.retry else None
    validation_retry = retry_cause == "validation_error"
    execution_status = {
        BridgeToolStatus.EXECUTED: ExecutionStatus.SUCCEEDED,
        BridgeToolStatus.DENIED: ExecutionStatus.FAILED,
        BridgeToolStatus.APPROVAL_REQUIRED: ExecutionStatus.FAILED,
        BridgeToolStatus.UNKNOWN_EFFECT: ExecutionStatus.OUTCOME_UNKNOWN,
    }[status]
    domain_outcome = {
        BridgeToolStatus.EXECUTED: DomainOutcome.SUCCESS,
        BridgeToolStatus.DENIED: DomainOutcome.FAILURE,
        BridgeToolStatus.APPROVAL_REQUIRED: DomainOutcome.UNKNOWN,
        BridgeToolStatus.UNKNOWN_EFFECT: DomainOutcome.UNKNOWN,
    }[status]
    effect_certainty = (
        EffectCertainty.UNKNOWN
        if status is BridgeToolStatus.UNKNOWN_EFFECT
        else EffectCertainty.CERTAIN
    )
    if validation_retry:
        preview = None
        error_code = _RETRY_PROMPT_ERROR_CODE
        error_detail = _bounded_detail(
            json.dumps(part.content, ensure_ascii=False, default=str)
        )
    else:
        preview, error_code, error_detail = _safe_content_projection(part.content)
    return ToolResultEntry(
        kind=EntryKind.TOOL_RESULT,
        call_id=part.tool_call_id,
        tool_name=part.tool_name,
        capability_id=part.tool_name,
        status=status.value,
        execution_status=execution_status,
        domain_outcome=domain_outcome,
        effect_certainty=effect_certainty,
        payload_hash=_payload_hash_of(part),
        output_preview=preview,
        error_code=error_code,
        error_detail=error_detail,
    )


def _tool_result_from_retry_part(part: RetryPromptPart) -> ToolResultEntry:
    """Project one ``RetryPromptPart`` (retryable validation failure)."""
    return ToolResultEntry(
        kind=EntryKind.TOOL_RESULT,
        call_id=part.tool_call_id,
        tool_name=part.tool_name or "unknown",
        capability_id=part.tool_name or "unknown",
        status=BridgeToolStatus.DENIED.value,
        execution_status=ExecutionStatus.FAILED,
        domain_outcome=DomainOutcome.FAILURE,
        effect_certainty=EffectCertainty.CERTAIN,
        payload_hash=_payload_hash_of(part),
        output_preview=None,
        error_code=_RETRY_PROMPT_ERROR_CODE,
        error_detail=_bounded_detail(str(part.content)[:200]),
    )


def _safe_content_projection(
    content: Any,
) -> tuple[str | None, str | None, str | None]:
    """Redact + bound one tool-return content into (preview, code, detail).

    When the content is the trusted producer's envelope document, its frozen
    projection fields are re-used verbatim (the anti-forgery guard
    re-validates them); when it is anything else (arbitrary tool return
    values), the whole content is treated as untrusted output text.
    """
    if isinstance(content, Mapping):
        document = dict(content)
        projection = document.get("projection")
        if isinstance(projection, Mapping):
            output = document.get("output")
            text = output.get("text") if isinstance(output, Mapping) else None
            preview = (
                None
                if text is None
                else project_text(str(text), max_chars=MAX_TOOL_OUTPUT_PREVIEW_CHARS)
            )
            raw_code = document.get("error_code")
            raw_detail = document.get("error_detail")
            return (
                preview,
                str(raw_code) if raw_code else None,
                _bounded_detail(str(raw_detail)) if raw_detail else None,
            )
        if "status" in document:
            # Unknown envelope shape with a status field: redact the whole
            # document as output text (never parse vocabulary from it).
            preview = project_text(
                json.dumps(document, ensure_ascii=False, default=str),
                max_chars=MAX_TOOL_OUTPUT_PREVIEW_CHARS,
            )
            return preview, None, None
    if content is None:
        return None, None, None
    preview = project_text(
        content
        if isinstance(content, str)
        else json.dumps(content, ensure_ascii=False, default=str),
        max_chars=MAX_TOOL_OUTPUT_PREVIEW_CHARS,
    )
    return preview, None, None


def _payload_hash_of(part: Any) -> str:
    """Stable per-part identity binding the result to its call.

    The bridge's canonical payload hash lives on the envelope document when
    present; otherwise the call id is hashed into a fixed-length identity.
    """
    if isinstance(part, ToolReturnPart) and isinstance(part.content, Mapping):
        payload_hash = part.content.get("payload_hash")
        if isinstance(payload_hash, str) and payload_hash:
            return payload_hash[:MAX_HASH_CHARS]
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"tool-call:{part.tool_call_id}")
    return f"derived:{digest.hex}"[:MAX_HASH_CHARS]


def _bounded_detail(detail: str) -> str:
    return detail[:MAX_ERROR_DETAIL_CHARS]


async def apersist_run_exchange(
    sessions: async_sessionmaker[AsyncSession],
    *,
    new_messages: Sequence[ModelMessage],
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    run_id: uuid.UUID,
    model_profile: str,
    provider: str,
    durable_prefix: str = "run",
) -> list[SessionEntryView]:
    """Persist ONE PydanticAI run's ``new_messages()`` into the session log.

    Idempotent end to end: every draft carries a deterministic
    ``(run_id, durable_source_id, kind)`` key, so a crash/replay of the same
    exchange re-reads the committed entries instead of duplicating them.
    ``ThinkingPart`` content is additionally persisted as full provider
    artifact rows (bounded to the schema's 20000-char CHECK with an explicit
    marker), isolated by the ``(provider, model_profile)`` compatibility
    identity.
    """
    drafts = extract_entries_from_run_messages(
        new_messages,
        session_id=session_id,
        run_id=run_id,
        model_profile=model_profile,
        provider=provider,
        durable_prefix=durable_prefix,
    )
    persisted: list[SessionEntryView] = []
    for draft in drafts:
        payload = draft.payload
        row = await aappend_entry(
            sessions,
            session_id=session_id,
            server_id=server_id,
            kind=payload.kind,
            payload=payload.model_dump(mode="json"),
            run_id=run_id,
            durable_source_id=draft.durable_source_id,
        )
        persisted.append(
            SessionEntryView(
                entry_id=row.entry_id,
                session_id=row.session_id,
                seq=row.seq,
                run_id=row.run_id,
                durable_source_id=row.durable_source_id,
                created_at=row.created_at,
                payload=row.payload,
            )
        )
        if isinstance(payload, AssistantMessageEntry):
            for artifact in payload.provider_artifacts:
                content, _truncated, _omitted = _bound_with_marker(
                    artifact.content, _ARTIFACT_ROW_CONTENT_MAX_CHARS
                )
                await arecord_provider_artifact(
                    sessions,
                    session_id=session_id,
                    server_id=server_id,
                    provider=provider,
                    model_profile=model_profile,
                    artifact_kind=artifact.artifact_kind,
                    content=content,
                    run_id=run_id,
                )
    return persisted


#: Full artifact row content bound = the schema CHECK on
#: ``session_provider_artifacts.content``.
_ARTIFACT_ROW_CONTENT_MAX_CHARS: Final[int] = 20_000


# --- projection: session entries -> native model history ----------------------------


@dataclass(frozen=True, slots=True)
class SessionNativeHistory:
    """The native model window plus the honest exclusion ledger.

    ``messages`` is what the provider receives; ``excluded`` records every
    entry kept OUT of native replay (pending groups, orphan results,
    superseded/compacted entries) so the caller can prove nothing was
    silently dropped and the UI can badge pending groups honestly.
    """

    messages: tuple[ModelMessage, ...]
    excluded: tuple[SessionEntryView, ...]
    superseded_by_compaction: int


def build_native_model_history(
    entries: Sequence[SessionEntryView],
    *,
    provider: str,
    model_profile: str,
    include_thinking: bool = True,
    retry_mode: bool = False,
    session_server_id: uuid.UUID | None = None,
) -> SessionNativeHistory:
    """Project a chronological entry window onto native PydanticAI messages.

    Pure and deterministic. See the module docstring for the mapping rules;
    the atomic-group partition (with the R01 O1 orphan rule) decides which
    entries may produce ``ToolCallPart``/``ToolReturnPart`` shapes.
    """
    scope = session_server_id if session_server_id is not None else uuid.UUID(int=0)
    all_entries = list(entries)
    compaction = _latest_compaction(all_entries)
    superseded = 0
    if compaction is not None:
        superseded = sum(1 for e in all_entries if e.seq <= compaction.to_seq)
        # Superseded entries leave the model window entirely; only the
        # post-compaction tail is partitioned for native replay.
        entries = [e for e in all_entries if e.seq > compaction.to_seq]
    partition = partition_replayable_tool_groups(
        entries, session_server_id=scope
    )
    pending = list(partition.pending)

    messages: list[ModelMessage] = []
    if compaction is not None:
        messages.append(_compaction_anchor(compaction))

    # Membership by durable source id: pending assistant copies keep their
    # source id, orphan results keep their object identity.
    pending_by_source = {entry.durable_source_id: entry for entry in pending}

    for entry in entries:
        payload = entry.payload
        if isinstance(payload, UserMessageEntry):
            messages.append(
                ModelRequest(
                    parts=[UserPromptPart(content=_user_prompt_content(payload), timestamp=entry.created_at)]
                )
            )
            continue
        if isinstance(payload, AssistantMessageEntry):
            if entry.durable_source_id in pending_by_source:
                # Pending group: the dangling tool calls are dropped, the
                # safe text stays visible (never fabricated success).
                if payload.text:
                    messages.append(
                        ModelResponse(
                            parts=[TextPart(content=payload.text)],
                            timestamp=entry.created_at,
                            usage=RequestUsage(),
                        )
                    )
                continue
            message = _assistant_response(
                entry,
                payload,
                provider=provider,
                model_profile=model_profile,
                include_thinking=include_thinking,
            )
            if message is not None:
                messages.append(message)
            continue
        if isinstance(payload, ToolResultEntry):
            if entry.durable_source_id in pending_by_source:
                continue  # pending/orphan results never replay
            assert_tool_result_honesty(payload)
            part = _tool_result_part(
                payload, retry_mode=retry_mode, timestamp=entry.created_at
            )
            if messages and isinstance(messages[-1], ModelRequest) and all(
                isinstance(p, (ToolReturnPart, RetryPromptPart))
                for p in messages[-1].parts
            ):
                messages[-1] = ModelRequest(
                    parts=[*messages[-1].parts, part],
                    timestamp=entry.created_at,
                )
            else:
                messages.append(
                    ModelRequest(parts=[part], timestamp=entry.created_at)
                )
            continue
        if isinstance(payload, ControlEntry):
            if payload.action is ControlActionKind.STEER and payload.note:
                bounded = payload.note[:_STEER_NOTE_MAX_CHARS]
                messages.append(
                    ModelRequest(
                        parts=[
                            UserPromptPart(
                                content=_STEER_PROMPT_PREFIX + bounded,
                                timestamp=entry.created_at,
                            )
                        ]
                    )
                )
            # every other control action is an operator acknowledgement,
            # never model context
            continue
        # RunStateEntry / DelegationEntry / non-anchor CompactionEntry:
        # domain/UI records, never model prose.
        continue

    excluded: list[SessionEntryView] = list(pending)
    if compaction is not None:
        excluded.extend(e for e in all_entries if e.seq <= compaction.to_seq)
    return SessionNativeHistory(
        messages=tuple(messages),
        excluded=tuple(excluded),
        superseded_by_compaction=superseded,
    )


def _latest_compaction(entries: Sequence[SessionEntryView]) -> CompactionEntry | None:
    latest: CompactionEntry | None = None
    for entry in entries:
        payload = entry.payload
        if isinstance(payload, CompactionEntry):
            if latest is None or payload.to_seq > latest.to_seq:
                latest = payload
    return latest


def _compaction_anchor(compaction: CompactionEntry) -> ModelRequest:
    prefix = _COMPACTION_ANCHOR_PREFIX.format(
        from_seq=compaction.from_seq,
        to_seq=compaction.to_seq,
    )
    content = prefix + compaction.summary_text[:MAX_SUMMARY_TEXT_CHARS]
    return ModelRequest(
        parts=[SystemPromptPart(content=content, timestamp=_EPOCH)],
    )


def _service_scope_prefix(payload: UserMessageEntry) -> str | None:
    """The compact follow-on identity prefix (SCOPE-2, fixed vocabulary,
    inside the reviewed 512-char prefix cap).

    The FIRST service-bound entry is skipped: its persisted text already
    carries the full Service Map section (identity included), so a second
    prefix would duplicate material. An unavailable name renders the
    deterministic bounded fallback — the identity survives a deleted map
    entry without borrowing another server's label."""
    ref = payload.service_ref
    if ref is None:
        return None
    if SERVICE_MAP_SECTION_HEADER in payload.text:
        return None
    if ref.name:
        return f"[service scope: @{ref.name} ({ref.service_id})]"
    short = str(ref.service_id)[:8]
    return f"[service scope: @application-{short} (unavailable) id {ref.service_id}]"


def _user_prompt_content(payload: UserMessageEntry) -> str:
    text = payload.text
    prefixes: list[str] = []
    if payload.document_ref is not None:
        prefixes.append(
            f"[document attachment: {payload.document_ref.artifact_ref}]"
        )
    if payload.runbook_ref is not None:
        prefixes.append(
            f"[runbook execution: service {payload.runbook_ref.service_id} "
            f"runbook {payload.runbook_ref.runbook_id}]"
        )
    service_prefix = _service_scope_prefix(payload)
    if service_prefix is not None:
        prefixes.append(service_prefix)
    if not prefixes:
        return text
    bounded_prefix = "\n".join(prefixes)[:MAX_SERVICE_REF_PREFIX_CHARS]
    return bounded_prefix + "\n" + text


def _assistant_response(
    entry: SessionEntryView,
    payload: AssistantMessageEntry,
    *,
    provider: str,
    model_profile: str,
    include_thinking: bool,
) -> ModelResponse | None:
    parts: list[Any] = []
    if include_thinking:
        for artifact in payload.provider_artifacts:
            if artifact.redacted:
                continue
            if artifact.provider != provider:
                continue
            if payload.model_profile != model_profile:
                continue
            parts.append(
                ThinkingPart(
                    content=artifact.content,
                    signature=(
                        artifact.content
                        if artifact.artifact_kind == "signature"
                        else None
                    ),
                    provider_name=artifact.provider,
                )
            )
    if payload.text:
        parts.append(TextPart(content=payload.text))
    for call in payload.tool_calls:
        parts.append(
            ToolCallPart(
                tool_name=call.tool_name,
                args=call.args,
                tool_call_id=call.call_id,
            )
        )
    if not parts:
        return None  # nothing natively sayable (e.g. stripped artifacts only)
    return ModelResponse(parts=parts, timestamp=entry.created_at, usage=RequestUsage())


def _tool_result_part(
    payload: ToolResultEntry, *, retry_mode: bool, timestamp: datetime
) -> ToolReturnPart:
    if retry_mode and payload.error_code in RETRYABLE_TOOL_ERROR_CODES:
        # The pinned adapter's canonical retry shape (RetryPromptPart is
        # deprecated for removal in v3): a retried tool return carrying the
        # validation feedback.
        return ToolReturnPart(
            tool_name=payload.tool_name,
            content=(
                "tool call rejected before execution (payload validation): "
                + (payload.error_detail or payload.error_code or "invalid payload")
            ),
            tool_call_id=payload.call_id,
            outcome="retried",
            retry=RetryInfo(cause="validation_error", attempt=1, max_retries=1),
            timestamp=timestamp,
        )
    outcome = _outcome_for(payload)
    return ToolReturnPart(
        tool_name=payload.tool_name,
        content=project_replay_envelope(payload),
        tool_call_id=payload.call_id,
        outcome=outcome,
        timestamp=timestamp,
    )


async def abuild_session_model_history(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    provider: str,
    model_profile: str,
    include_thinking: bool = True,
    retry_mode: bool = False,
    max_context_entries: int = MAX_CONTEXT_ENTRIES,
    run_scope: tuple[RunHistoryScope, uuid.UUID] | None = None,
) -> SessionNativeHistory:
    """Load the bounded model window from the store and project it natively.

    The walk reads keyset pages (≤50 entries, ≤64 KiB each — never the whole
    history in one query) from the newest tail backward and stops at the
    latest compaction boundary (entries covered by the anchor are superseded
    and stay in the store) or at :data:`MAX_CONTEXT_ENTRIES`, whichever comes
    first. The result carries the honest exclusion ledger.

    ``run_scope`` (D05, additive): ``(kind, run_id)`` isolates DELEGATION
    surfaces from each other — the child's native window sees ONLY its own
    run's entries; a root run's window NEVER sees child-run entries (a
    child's transcript is never spliced into the parent's native history).
    Sibling ROOT runs of the same session stay visible (the D04 cross-run
    continuation proof). ``None`` (every existing caller) keeps the
    whole-session window.
    """
    keep: Callable[[SessionEntryView], bool] | None = None
    if run_scope is not None:
        scope_kind, scope_run_id = run_scope
        if scope_kind is RunHistoryScope.CHILD_RUN:
            keep = lambda entry: entry.run_id == scope_run_id  # noqa: E731
        else:
            child_run_ids = await _child_run_ids(
                sessions, session_id=session_id, server_id=server_id
            )
            keep = lambda entry: (  # noqa: E731
                entry.run_id is None or entry.run_id not in child_run_ids
            )
    window: list[SessionEntryView] = []
    boundary_seq: int | None = None  # compaction coverage seen so far
    cursor: int | None = None
    while len(window) < max_context_entries:
        page: SessionHistoryPage = await aread_session_history(
            sessions,
            session_id=session_id,
            server_id=server_id,
            limit=HISTORY_PAGE_MAX_ENTRIES,
            before_seq=cursor,
        )
        if not page.items:
            break
        window.extend(page.items)
        newest_compaction = _latest_compaction(page.items)
        if newest_compaction is not None and (
            boundary_seq is None or newest_compaction.to_seq > boundary_seq
        ):
            boundary_seq = newest_compaction.to_seq
        if boundary_seq is not None and page.oldest_seq is not None and page.oldest_seq <= boundary_seq:
            break  # the anchor covers everything older than this page
        if not page.has_older or page.next_before_seq is None:
            break
        cursor = page.next_before_seq
    window.sort(key=lambda entry: entry.seq)
    if keep is not None:
        window = [entry for entry in window if keep(entry)]
    window = window[-max_context_entries:]
    # build_native_model_history owns compaction handling: it finds the
    # latest compaction in the window, emits the anchor and supersedes the
    # covered entries (which stay in the store).
    return build_native_model_history(
        window,
        provider=provider,
        model_profile=model_profile,
        include_thinking=include_thinking,
        retry_mode=retry_mode,
        session_server_id=server_id,
    )
