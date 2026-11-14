"""Agent-facing capability tools behind the enforced dispatch boundary.

Card ``06-tool-bridge`` against contracts.md **C06 rule 4** ("the
agent-to-tool bridge passes the ``ExecutionContext`` through the same
dispatcher used by deterministic UI/job calls"; "PydanticAI deferred
tools are not an authorization boundary") and **C03 rules 2–7**;
card ``19-tool-results`` adds the compact model-facing projection
(:func:`project_tool_result`) that separates the three layers a tool
answer must not conflate — wrapper execution (:class:`~mai.contracts.
tool_results.ExecutionStatus`), domain outcome (:class:`~mai.contracts.
tool_results.DomainOutcome`) and effect certainty (:class:`~mai.contracts.
tool_results.EffectCertainty`) — while every collection it carries stays paginated
and every string stays inside the dispatcher's redaction boundary.

What this module is
-------------------

A presentation-only composition layer between two reviewed producers:
the declarative :class:`~mai.plugins.registry.CapabilityRegistry` (C03
rule 1) and the enforced boundary
:class:`~mai.capabilities.dispatch.CapabilityDispatcher` (C03). The
:class:`CapabilityToolBridge` turns the descriptors that are *enabled
for one* :class:`~mai.policy.evaluate.ExecutionContext` into PydanticAI
:class:`~pydantic_ai.tools.Tool` objects and routes every invocation —
model-facing or host resume — through that one dispatcher.

Fail-closed properties (each pinned by ``backend/tests/test_agent_tools.py``):

1. **Exposure is not authorization.** Only capabilities that are both
   registered and named in ``context.allowed_capabilities`` become
   tools. The dispatcher re-resolves current grants/policy/adoption and
   rechecks immediately before every effect, so a stale bridge object
   can never widen anything.
2. **Schema mismatch never dispatches.** PydanticAI schema-declared
   tools skip argument validation, so the bridge validates the model's
   payload against the descriptor's ``input_schema`` *before* calling
   the dispatcher. A mismatch returns a structured ``denied`` result —
   no state read, no approval consumed, no effect planned. The same
   gate guards the resume path.
3. **Exceptions are safe.** The model-facing tool function never
   raises: dispatcher decisions, infrastructure failures (e.g. an
   approval-store outage) and bridge-level misuse all become one
   structured :class:`ToolResult` JSON document. Exception *messages*
   never surface (they may quote secret material) — infrastructure
   failures report the exception *type name* only, mirroring the
   dispatcher's honesty rule.
4. **Deferred approvals stay durable.** When the boundary reports
   ``approval_required_but_absent``, the tool result carries
   ``status="approval_required"`` with the canonical payload hash and
   policy revision — everything the run layer (``06-agent-run``, jobs
   facade C05) needs to persist ``waiting_approval`` through the
   durable engine. Resumption calls :meth:`CapabilityToolBridge.dispatch_once`
   with the persisted approval id — the same dispatcher seam, never a
   private executor path.

What this module is not
-----------------------

- Not a policy engine: every decision is the dispatcher's.
- Not an execution transport: the executor is whatever the host wired
  into the dispatcher (C03 rule 7).
- Not a deferral mechanism: PydanticAI deferred tools / approval
  requests are deliberately not used — they are not an authorization
  boundary (C06 rule 4); the dispatcher's approval seam is.

Known framework edge (recorded, fail-closed): a provider that emits a
tool-call argument document which is valid JSON but *not an object*
crashes the PydanticAI run inside argument unpacking, before any
bridge code. That is the framework's behavior for every
``Tool.from_schema`` tool (its own MCP integration shares it); the
crash is fail-closed — no dispatch, no effect — and run-level
exception handling belongs to the ``06-agent-run`` consumer, not this
layer. Invalid-JSON argument strings are converted to a retry prompt
by PydanticAI before the bridge is reached.
"""

from __future__ import annotations

import json
import re
import uuid
import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from pydantic_ai.tools import Tool

from mai.agents.output_safety import bound_text, redact_text
from mai.capabilities.dispatch import (
    EFFECT_EXECUTED,
    EFFECT_UNKNOWN,
    CapabilityDispatcher,
    DispatchError,
    DispatchOutcome,
    canonical_payload_hash,
)
from mai.contracts.tool_results import (
    MAX_EVIDENCE_REFS,
    RESULT_DATA_MAX_ENTRIES,
    RESULT_DATA_VALUE_MAX_CHARS,
    SECRET_DATA_KEYS,
    DomainOutcome,
    EffectCertainty,
    EvidenceKind,
    ExecutionStatus,
)
from mai.plugins.registry import CapabilityDescriptor, CapabilityRegistry
from mai.capabilities.internal_api import SERVER_PROFILE_UPDATE_CAPABILITY
from mai.policy.evaluate import ExecutionContext, PolicyMode
from mai.policy.exposure import ObjectiveExposure
from mai.jobs.resource_locks import ResourceLockError, ResourceLockService

__all__ = [
    "BridgeError",
    "BridgeErrorCode",
    "CapabilityToolBridge",
    "MAX_TOOL_NAME_CHARS",
    "TOOL_RESULT_PROJECTION_SCHEMA_VERSION",
    "TOOL_RESULT_SCHEMA_VERSION",
    "ToolResult",
    "ToolStatus",
    "project_tool_result",
    "tool_name_for",
]

#: Version of the frozen :class:`ToolResult` JSON document shape.
#:
#: ``2`` — act ``19-tool-results``: the document gained one additive key,
#: ``projection`` (the compact execution/domain/certainty block built by
#: :func:`project_tool_result`). Every version-1 key keeps its position,
#: name, type and semantics, so version-1 consumers stay compatible; the
#: bump marks the extension, it does not rename or re-order anything.
TOOL_RESULT_SCHEMA_VERSION = 2

#: Version of the frozen :class:`ToolResult` ``projection`` block shape
#: (act ``19-tool-results``). The projection is versioned independently so
#: the envelope and the projection can evolve on their own clocks.
TOOL_RESULT_PROJECTION_SCHEMA_VERSION = 1

#: The approval-deferral reason the dispatcher reports when a privileged
#: call arrives without its approval decision (mirrored verbatim from the
#: C03 dispatch vocabulary; the run layer parks the attempt on it).
_APPROVAL_REQUIRED_REASON = "approval_required_but_absent"

# --- neutral phase-gate mirror (D01 extraction; guidance only) -----------------
#
# The task contracts (deleted at D09 by the campaign plan) own the
# ratified phase×action admission map the runner classifies capabilities
# with. ``tools.py`` must not import those task-domain contracts, so the
# offer-shaping filter compares neutral *values* against this local mirror:
# metadata/plan actions are admissible in every phase; evidence collection
# in ``inspect``/``act``; execution only in ``act``. This stays GUIDANCE —
# the enforcement boundary is the dispatcher's own fail-closed phase gate,
# which the task contracts keep owning until D09 removes both together.

_PHASE_ANY: Final[frozenset[str]] = frozenset({"plan", "inspect", "act"})
_PHASE_INSPECT_OR_ACT: Final[frozenset[str]] = frozenset({"inspect", "act"})
_PHASE_ACT_ONLY: Final[frozenset[str]] = frozenset({"act"})

_PHASE_ACTION_ADMISSION: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "metadata_update": _PHASE_ANY,
        "plan_proposal": _PHASE_ANY,
        "collect_evidence": _PHASE_INSPECT_OR_ACT,
        "execute_steps": _PHASE_ACT_ONLY,
    }
)


def _phase_allows(phase: object, action: object) -> bool:
    """Value-based mirror of the ratified phase×action admission map.

    ``phase``/``action`` arrive as task-domain ``StrEnum`` members (or plain
    strings) from legacy callers; both are compared by ``value`` so this
    bridge stays free of task-contract imports. Unknown actions are refused
    (fail-closed), matching the enforcement boundary's behavior.
    """
    phase_value = getattr(phase, "value", phase)
    action_value = getattr(action, "value", action)
    if not isinstance(phase_value, str) or not isinstance(action_value, str):
        return False
    allowed_phases = _PHASE_ACTION_ADMISSION.get(action_value)
    return allowed_phases is not None and phase_value in allowed_phases

#: Bounded projection slices (act_CONTRACTS §9 "safe bounded result data"
#: + the mandatory-pagination invariant). Subchecks have no central bound
#: of their own; the projection caps them at the evidence-ref bound.
_MAX_SUBCHECKS = MAX_EVIDENCE_REFS
_SUBCHECK_NAME_MAX_CHARS = 128
_ERROR_CODE_MAX_CHARS = 64
_ERROR_DETAIL_MAX_CHARS = 200
_RETRY_HINT_MAX_CHARS = 200
_RESULT_KEY_MAX_CHARS = 64
_EVIDENCE_ID_MAX_CHARS = 64
_EVIDENCE_NOTE_MAX_CHARS = 200

#: Internal-API envelope statuses that carry semantic meaning (the
#: ``{"status": "ok"}`` / ``{"status": "refused"|"unavailable", "code"}``
#: convention every internal-API executor wraps its result in). A shell
#: wrapper envelope NEVER reaches this branch — see
#: :func:`_domain_evidence`.
_OK_ENVELOPE_STATUS = "ok"
_FAILURE_ENVELOPE_STATUSES = frozenset({"refused", "unavailable"})

#: OpenAI-compatible function-name bound: ``^[A-Za-z0-9_-]{1,64}$``. The
#: descriptor slug charset (``[a-z0-9._-]``) is not wire-safe (dots), so
#: every mapped name is sanitized and length-capped; collisions are a
#: construction rejection, never a silent alias.
MAX_TOOL_NAME_CHARS = 64

_TOOL_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

#: Recursion bound for schema validation; the registry caps a schema
#: document at 16 KiB, this bounds pathological *nesting* shape only.
_MAX_SCHEMA_DEPTH = 24

#: JSON Schema keywords this bridge asserts. Anything else that can
#: affect validity (``allOf``, ``oneOf``, ``$ref``, ``if``, …) is
#: fail-closed: the call is denied, never executed, until the schema
#: producer reconciles to the supported subset.
_SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "enum",
        "const",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
    }
)

#: Annotation-only keywords: carried for documentation, assert nothing.
_ANNOTATION_KEYWORDS = frozenset(
    {"title", "description", "default", "examples", "format", "$schema", "$id"}
)

_JSON_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")

#: Payload-hash marker for a payload that is not a canonicalizable JSON
#: object (the dispatcher would reject it the same way; the bridge
#: reports it before any state is read).
_NON_CANONICAL = "non_canonical"


class BridgeErrorCode(StrEnum):
    """Stable machine-readable bridge rejection codes (C01 style)."""

    INVALID_WIRING = "invalid_wiring"
    TOOL_NAME_COLLISION = "tool_name_collision"
    CAPABILITY_NOT_ENABLED = "capability_not_enabled"
    PAYLOAD_SCHEMA_MISMATCH = "payload_schema_mismatch"
    #: v2 exposure: the call named a server outside the capability's
    #: valid target set (a hidden target is never callable).
    TARGET_NOT_IN_SCOPE = "target_not_in_scope"
    #: v2: read fan-out was attempted for a non-read capability.
    READ_FANOUT_UNSUPPORTED = "read_fanout_unsupported"
    #: v2: mutation dispatch lost the declared resource lock.
    LOCK_CONTENTION = "lock_contention"


class BridgeError(ValueError):
    """One structured, value-free bridge construction/use rejection.

    Raised only for host misuse (bad wiring, resume of a capability the
    context does not enable, dispatch of a schema-mismatched payload).
    Model-facing answers are never this exception — they are
    :class:`ToolResult` documents.
    """

    def __init__(self, code: BridgeErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class ToolStatus(StrEnum):
    """Frozen ``ToolResult`` status vocabulary — the honest lifecycle.

    ``unknown_effect`` means the invocation started and the outcome is
    unproven (host rule 3 honesty); the model must neither assume
    success nor retry blindly.
    """

    EXECUTED = "executed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"
    UNKNOWN_EFFECT = "unknown_effect"


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The model-facing audit document of one bridged call (AC2).

    A frozen-shape, value-free projection of the dispatcher's
    :class:`~mai.capabilities.dispatch.DispatchOutcome`: the payload is
    present as its canonical SHA-256 hash only, executor output only
    after the dispatcher's redact-then-bound pass, and error detail is
    either a stable code or an exception type name. Enough durable
    material (capability, target, run, payload hash, policy revision)
    for the jobs facade to persist a ``waiting_approval`` deferral and
    for the resume path to bind the human decision (C03 rule 4).

    The serialized document (:meth:`to_json_str`) carries every
    version-1 key unchanged plus one additive ``projection`` key: the
    compact execution/domain/certainty block built by
    :func:`project_tool_result` (act ``19-tool-results``; act_CONTRACTS
    §9). The derivation reads only the fields already on the result, so
    every construction path — the bridge's ``from_outcome``, pre-dispatch
    ``rejected`` and the run layer's direct replay documents — projects
    identically and deterministically.
    """

    schema_version: int
    capability_id: str
    server_id: uuid.UUID
    run_id: uuid.UUID
    status: ToolStatus
    allowed: bool
    reason: str
    effect: str
    payload_hash: str
    policy_revision: int | None
    effect_class: str | None
    approval_id: uuid.UUID | None
    approval_spent: bool
    approval_code: str | None
    executor_ok: bool | None
    output_text: str | None
    output_truncated: bool | None
    output_omitted_bytes: int | None
    error_code: str | None
    error_detail: str | None

    @classmethod
    def from_outcome(cls, outcome: DispatchOutcome) -> ToolResult:
        """Project one dispatcher receipt onto the tool vocabulary."""
        if outcome.effect == EFFECT_EXECUTED:
            status = ToolStatus.EXECUTED
        elif outcome.effect == EFFECT_UNKNOWN:
            status = ToolStatus.UNKNOWN_EFFECT
        elif (
            not outcome.allowed
            and outcome.effect == "not_attempted"
            and outcome.reason == "approval_required_but_absent"
        ):
            status = ToolStatus.APPROVAL_REQUIRED
        else:
            status = ToolStatus.DENIED
        # FRR T2: the model sees WHY a call was denied — the dispatcher's
        # stable refusal token (T1), in the DENIED branch only. The
        # outcome carries no safe detail text (T1 keeps receipts
        # value-free), so ``error_detail`` stays None — never invented.
        error_code = outcome.error_code if status is ToolStatus.DENIED else None
        return cls(
            schema_version=TOOL_RESULT_SCHEMA_VERSION,
            capability_id=outcome.capability_id,
            server_id=outcome.server_id,
            run_id=outcome.run_id,
            status=status,
            allowed=outcome.allowed,
            reason=outcome.reason,
            effect=outcome.effect,
            payload_hash=outcome.payload_hash,
            policy_revision=outcome.policy_revision,
            effect_class=outcome.effect_class,
            approval_id=outcome.approval_id,
            approval_spent=outcome.approval_spent,
            approval_code=outcome.approval_code,
            executor_ok=outcome.executor_ok,
            output_text=None if outcome.output is None else outcome.output.text,
            output_truncated=None
            if outcome.output is None
            else outcome.output.truncated,
            output_omitted_bytes=None
            if outcome.output is None
            else outcome.output.omitted_bytes,
            error_code=error_code,
            error_detail=None,
        )

    @classmethod
    def rejected(
        cls,
        *,
        context: ExecutionContext,
        capability_id: str,
        payload_hash: str,
        error_code: str,
        error_detail: str,
    ) -> ToolResult:
        """A pre-dispatch rejection: nothing was read, consumed or run."""
        return cls(
            schema_version=TOOL_RESULT_SCHEMA_VERSION,
            capability_id=capability_id,
            server_id=context.server_id,
            run_id=context.run_id,
            status=ToolStatus.DENIED,
            allowed=False,
            reason=error_code,
            effect="not_attempted",
            payload_hash=payload_hash,
            policy_revision=None,
            effect_class=None,
            approval_id=None,
            approval_spent=False,
            approval_code=None,
            executor_ok=None,
            output_text=None,
            output_truncated=None,
            output_omitted_bytes=None,
            error_code=error_code,
            error_detail=error_detail,
        )

    def to_json_str(self) -> str:
        """Frozen-shape JSON text (stable key order; model-safe).

        ``ensure_ascii=False``: Cyrillic (and any other non-ASCII output)
        stays real UTF-8 — ASCII ``\\uXXXX`` escapes would leak into the
        persisted ``output_preview`` and render unreadable in the chat.
        """
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "capability_id": self.capability_id,
                "server_id": str(self.server_id),
                "run_id": str(self.run_id),
                "status": self.status.value,
                "allowed": self.allowed,
                "reason": self.reason,
                "effect": self.effect,
                "payload_hash": self.payload_hash,
                "policy_revision": self.policy_revision,
                "effect_class": self.effect_class,
                "approval_id": None
                if self.approval_id is None
                else str(self.approval_id),
                "approval_spent": self.approval_spent,
                "approval_code": self.approval_code,
                "executor_ok": self.executor_ok,
                "output": None
                if self.output_text is None
                else {
                    "text": self.output_text,
                    "truncated": self.output_truncated,
                    "omitted_bytes": self.output_omitted_bytes,
                },
                "error": None
                if self.error_code is None
                else {"code": self.error_code, "detail": self.error_detail},
                "projection": project_tool_result(self),
            },
            ensure_ascii=False,
            allow_nan=False,
        )

    def __repr__(self) -> str:
        return (
            f"<ToolResult {self.capability_id} {self.status.value} "
            f"reason={self.reason}>"
        )


# ---------------------------------------------------------------------------
# Compact model-facing projection (act 19-tool-results; act_CONTRACTS §9)
# ---------------------------------------------------------------------------


def _safe_slice(text: str, limit: int) -> str:
    """Redact, then bound, one extracted projection string.

    Extracted fields are substrings of the dispatcher-redacted output,
    so this is defense-in-depth (the model-facing bridge path runs only
    the dispatcher's value-redaction; the pattern layer here also covers
    that path). The order is the output-safety invariant: redaction
    FIRST, the explicit bound AFTER — a cut can never split or expose a
    secret redaction already replaced.
    """
    return bound_text(redact_text(text), limit)


def _parsed_output_document(result: ToolResult) -> dict[str, object] | None:
    """The executor envelope, when the bounded output parses as a JSON
    object.

    The dispatcher redacted and bounded the text before this module ever
    saw it (C03 rule 6, C06 rule 3), so every string inside the parsed
    document is already redaction-safe. Non-JSON output (arbitrary shell
    text, prose) yields ``None`` — there is no structured evidence to
    read, and none is invented.
    """
    text = result.output_text
    if not text or len(text) > 262144:
        return None
    stripped = text.lstrip()
    if stripped[:1] != "{":
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_shell_envelope(document: Mapping[str, object]) -> bool:
    """Whether the envelope is a command-wrapper receipt (host.shell and
    siblings: ``exit_code``/``stdout`` present).

    A wrapper's own ``status``/``exit_code`` prove only that the command
    ran — never what it semantically did (E3: "wrapper exits zero" is not
    domain evidence). Explicit structured domain markers (``subchecks``,
    ``domain_outcome``) still win over this rule; the wrapper ``status``
    key alone never does.
    """
    return "exit_code" in document or "stdout" in document


def _subcheck_page(
    document: Mapping[str, object],
) -> tuple[list[dict[str, object]], int, bool, bool]:
    """Validate the subcheck section; return the bounded slice.

    ``(page, total, well_formed, any_failed)``. A subcheck entry MUST be
    an object with a non-empty string ``name`` and a boolean ``passed`` —
    any other shape anywhere in the section makes the whole section
    untrustworthy (``well_formed=False``), and untrustworthy structured
    evidence must never fall through to a weaker promotion rule. The
    slice is deterministic: declaration order, first
    :data:`_MAX_SUBCHECKS` well-formed entries; ``any_failed`` is
    evaluated over the FULL section, not just the slice.
    """
    raw = document.get("subchecks")
    if raw is None:
        return [], 0, True, False
    if not isinstance(raw, list):
        return [], 0, False, False
    page: list[dict[str, object]] = []
    any_failed = False
    for entry in raw:
        if not isinstance(entry, Mapping):
            return [], len(raw), False, False
        name = entry.get("name")
        passed = entry.get("passed")
        if not isinstance(name, str) or not name.strip():
            return [], len(raw), False, False
        if not isinstance(passed, bool):
            return [], len(raw), False, False
        if passed is False:
            any_failed = True
        if len(page) < _MAX_SUBCHECKS:
            page.append(
                {"name": _safe_slice(name, _SUBCHECK_NAME_MAX_CHARS), "passed": passed}
            )
    return page, len(raw), True, any_failed


def _page_meta(total: int, limit: int) -> dict[str, object]:
    """Pagination metadata for one sliced collection (mandatory
    pagination invariant): the pre-slice total rides beside the slice."""
    return {
        "limit": limit,
        "offset": 0,
        "total": total,
        "has_more": total > limit,
    }


def _domain_evidence(
    document: Mapping[str, object] | None,
) -> tuple[DomainOutcome, bool]:
    """Derive ``(domain_outcome, fail_closed)`` from the executor envelope.

    Deterministic precedence, documented so consumers can audit it:

    1. Explicit structured domain markers win: ``subchecks`` (structured
       tools report meaningful subchecks explicitly, §9) and a
       ``domain_outcome`` marker in the frozen vocabulary. Any explicit
       negative evidence (a failed subcheck, an explicit failure) pins
       the outcome to ``failure`` — a wrapper exit code can never promote
       it away.
       A ``subchecks``/``domain_outcome`` key that is PRESENT but
       malformed (wrong shape, out-of-vocabulary value) is fail-closed:
       the outcome stays ``unknown`` even if a weaker rule (envelope
       ``status``) would have said ``ok`` — broken evidence never
       promotes.
    2. A shell wrapper envelope (``exit_code``/``stdout`` keys) has NO
       semantic evidence: ``unknown``, whatever its ``status`` claims.
    3. Otherwise the internal-API envelope convention decides:
       ``status == "ok"`` → ``success``; ``"refused"``/``"unavailable"``
       → ``failure``; anything else → ``unknown``.
    4. No parseable envelope at all → ``unknown`` (arbitrary shell work,
       plain text — no regex oracle is built here, §9).
    """
    if document is None:
        return DomainOutcome.UNKNOWN, False
    negative = False
    positive = False
    fail_closed = False

    _page, total, well_formed, any_failed = _subcheck_page(document)
    if "subchecks" in document:
        if not well_formed or total == 0:
            fail_closed = True
        elif any_failed:
            negative = True
        else:
            positive = True

    marker = document.get("domain_outcome")
    if marker is not None:
        try:
            outcome = DomainOutcome(marker)
        except ValueError:
            fail_closed = True
        else:
            if outcome is DomainOutcome.FAILURE:
                negative = True
            elif outcome is DomainOutcome.SUCCESS:
                positive = True

    if fail_closed:
        return DomainOutcome.UNKNOWN, True
    if negative:
        return DomainOutcome.FAILURE, False
    if positive:
        return DomainOutcome.SUCCESS, False
    if _is_shell_envelope(document):
        return DomainOutcome.UNKNOWN, False
    status = document.get("status")
    if status == _OK_ENVELOPE_STATUS:
        return DomainOutcome.SUCCESS, False
    if isinstance(status, str) and status in _FAILURE_ENVELOPE_STATUSES:
        return DomainOutcome.FAILURE, False
    return DomainOutcome.UNKNOWN, False


def _execution_status(result: ToolResult) -> ExecutionStatus:
    """The technical wrapper execution status (§9 ``execution_status``).

    Separate layer from the domain outcome: the dispatcher's honest
    lifecycle maps onto the frozen C04 subset. A denied/approval-required
    attempt closed without running (nothing to verify); an executed call
    with ``executor_ok=False`` is a technical failure (the executor
    answered, negatively); an unproven effect stays ``outcome_unknown``.
    ``cancelled`` is unreachable from the bridge vocabulary (no
    cancellation concept exists at this seam) and is therefore never
    claimed.
    """
    if result.status is ToolStatus.EXECUTED:
        if result.executor_ok is False:
            return ExecutionStatus.FAILED
        return ExecutionStatus.SUCCEEDED
    if result.status is ToolStatus.UNKNOWN_EFFECT:
        return ExecutionStatus.OUTCOME_UNKNOWN
    if (
        result.status is ToolStatus.APPROVAL_REQUIRED
        or result.reason == _APPROVAL_REQUIRED_REASON
    ):
        return ExecutionStatus.OUTCOME_UNKNOWN
    return ExecutionStatus.FAILED


def _effect_certainty(
    result: ToolResult, domain: DomainOutcome, fail_closed: bool
) -> EffectCertainty:
    """How certain the effect evidence is (§9 ``effect_certainty``).

    ``certain`` is reserved for one combination: the effect is proven
    executed AND explicit consistent structured evidence reports success.
    Everything else degrades honestly — negative evidence (a failed
    subcheck, a refused envelope) keeps ``uncertain`` so downstream
    reconciliation re-checks instead of trusting the projection (AC1:
    a failed subcheck is never promoted to certain success), and any
    unproven effect (denied, approval-pending, unknown) keeps ``unknown``
    — no effect evidence exists at all.
    """
    if result.status is not ToolStatus.EXECUTED:
        return EffectCertainty.UNKNOWN
    if fail_closed:
        return EffectCertainty.UNCERTAIN
    if domain is DomainOutcome.SUCCESS:
        return EffectCertainty.CERTAIN
    return EffectCertainty.UNCERTAIN


def _retry_hint(
    result: ToolResult, execution: ExecutionStatus, domain: DomainOutcome
) -> str:
    """Fixed, value-free advice (§9: advice only; policy decides)."""
    if (
        result.status is ToolStatus.APPROVAL_REQUIRED
        or result.reason == _APPROVAL_REQUIRED_REASON
    ):
        return (
            "No effect ran; obtain an explicit approval decision and "
            "resume once through the durable approval path."
        )
    if result.status is ToolStatus.UNKNOWN_EFFECT:
        return (
            "Neither success nor failure is proven; reconcile the unknown "
            "effect before any retry."
        )
    if result.status is ToolStatus.DENIED:
        return (
            "Do not retry identically; reconcile the stable reason and "
            "error code first."
        )
    if execution is ExecutionStatus.FAILED or domain is DomainOutcome.FAILURE:
        return (
            "The structured result reports failure; inspect error_code and "
            "result_data before any retry."
        )
    if domain is DomainOutcome.UNKNOWN:
        return (
            "The effect ran but the domain outcome is unproven; verify with "
            "a structured check before treating it as done."
        )
    return "Success is proven by the structured result; no retry needed."


def _error_code(
    result: ToolResult, document: Mapping[str, object] | None
) -> str | None:
    """The stable machine-readable error code, when one exists.

    Bridge/dispatcher rejections carry it on the result; executor
    envelopes carry it as the structured ``code`` field. Never an
    exception message, never payload material.
    """
    if result.error_code is not None:
        return _safe_slice(result.error_code, _ERROR_CODE_MAX_CHARS)
    if document is not None:
        code = document.get("code")
        if isinstance(code, str) and code:
            return _safe_slice(code, _ERROR_CODE_MAX_CHARS)
    return None


def _error_detail(
    result: ToolResult, document: Mapping[str, object] | None
) -> str | None:
    """The bounded safe error detail: a stable bridge/violation string or
    the executor envelope's ``message`` — both value-free by contract,
    both substrings of already-redacted text."""
    if result.error_detail is not None:
        return _safe_slice(result.error_detail, _ERROR_DETAIL_MAX_CHARS)
    if document is not None:
        for field in ("message", "detail"):
            value = document.get(field)
            if isinstance(value, str) and value:
                return _safe_slice(value, _ERROR_DETAIL_MAX_CHARS)
    return None


def _evidence_ref_page(
    document: Mapping[str, object] | None,
) -> tuple[list[dict[str, object]], int]:
    """The bounded evidence-reference slice plus its pre-slice total.

    Evidence refs survive the projection (no silent drops): every entry
    is carried in contract shape — ``{kind, evidence_id, note?}`` for
    object entries, ``{evidence_id}`` for bare string ids — and the
    pagination metadata reports the full pre-slice total, so a sliced
    page is visible as sliced, never as the whole truth.
    """
    if document is None:
        return [], 0
    raw = document.get("evidence_refs")
    if raw is None:
        return [], 0
    if not isinstance(raw, list):
        return [], 0
    page: list[dict[str, object]] = []
    for entry in raw[:MAX_EVIDENCE_REFS]:
        if isinstance(entry, str):
            if entry.strip():
                page.append({"evidence_id": _safe_slice(entry, _EVIDENCE_ID_MAX_CHARS)})
            continue
        if not isinstance(entry, Mapping):
            continue
        evidence_id = entry.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            continue
        ref: dict[str, object] = {
            "evidence_id": _safe_slice(evidence_id, _EVIDENCE_ID_MAX_CHARS)
        }
        kind = entry.get("kind")
        if isinstance(kind, str) and kind in {item.value for item in EvidenceKind}:
            ref["kind"] = kind
        note = entry.get("note")
        if isinstance(note, str) and note.strip():
            ref["note"] = _safe_slice(note, _EVIDENCE_NOTE_MAX_CHARS)
        page.append(ref)
    return page, len(raw)


def _result_data_page(
    document: Mapping[str, object] | None,
) -> tuple[dict[str, str], int]:
    """The bounded safe result-data slice plus its pre-slice total.

    Credential-shaped keys are never carried (the S1 boundary — same
    discipline as the completion verdict); every value is a bounded
    substring of already-redacted output. Dropped keys stay visible in
    the pagination total.
    """
    if document is None:
        return {}, 0
    raw = document.get("result_data")
    if raw is None:
        return {}, 0
    if not isinstance(raw, Mapping):
        return {}, 0
    page: dict[str, str] = {}
    for key, value in list(raw.items())[:RESULT_DATA_MAX_ENTRIES]:
        if not isinstance(key, str) or not key.strip():
            continue
        if key.lower() in SECRET_DATA_KEYS:
            continue
        if isinstance(value, str):
            text = value
        elif isinstance(value, (bool, int, float)) or value is None:
            text = json.dumps(value)
        else:
            text = json.dumps(value, sort_keys=True, default=str)
        page[_safe_slice(key, _RESULT_KEY_MAX_CHARS)] = _safe_slice(
            text, RESULT_DATA_VALUE_MAX_CHARS
        )
    return page, len(raw)


def project_tool_result(result: ToolResult) -> dict[str, object]:
    """The compact model-facing projection of one tool result (§9).

    Separates the three layers the model must not conflate:

    - ``execution_status`` — the technical wrapper execution (frozen C04
      subset via :class:`~mai.contracts.tool_results.ExecutionStatus`);
    - ``domain_outcome`` — what the operation semantically did
      (:class:`~mai.contracts.tool_results.DomainOutcome`); ``unknown`` is the
      honest answer for arbitrary shell work — a wrapper exit code alone
      NEVER produces ``success`` (E3);
    - ``effect_certainty`` — how certain the effect evidence is
      (:class:`~mai.contracts.tool_results.EffectCertainty`); ``certain`` only
      for a proven execution with explicit consistent structured success
      evidence (AC1: a failed subcheck or an unknown semantic outcome is
      never promoted to certain success).

    Plus the surviving decision material: stable ``error_code``, fixed
    value-free ``retry_hint`` advice, evidence references, subchecks and
    bounded ``result_data`` — every extracted collection paginated
    (``limit``/``offset``/``total``/``has_more``) so a slice is never
    mistaken for the whole set. All strings originate in the
    dispatcher-redacted output or in frozen vocabularies, so the
    projection cannot leak what redaction removed.

    Pure and deterministic: the same :class:`ToolResult` always projects
    to the same document, on the live tool path and on replay alike.
    """
    document = _parsed_output_document(result)
    domain, fail_closed = _domain_evidence(document)
    execution = _execution_status(result)
    subchecks, subchecks_total, _, _ = _subcheck_page(document or {})
    evidence_refs, evidence_total = _evidence_ref_page(document)
    result_data, result_data_total = _result_data_page(document)
    return {
        "schema_version": TOOL_RESULT_PROJECTION_SCHEMA_VERSION,
        "execution_status": execution.value,
        "domain_outcome": domain.value,
        "effect_certainty": _effect_certainty(result, domain, fail_closed).value,
        "error_code": _error_code(result, document),
        "error_detail": _error_detail(result, document),
        "retry_hint": _safe_slice(
            _retry_hint(result, execution, domain), _RETRY_HINT_MAX_CHARS
        ),
        "evidence_refs": evidence_refs,
        "subchecks": subchecks,
        "result_data": result_data,
        "pagination": {
            "subchecks": _page_meta(subchecks_total, _MAX_SUBCHECKS),
            "evidence_refs": _page_meta(evidence_total, MAX_EVIDENCE_REFS),
            "result_data": _page_meta(result_data_total, RESULT_DATA_MAX_ENTRIES),
        },
    }


def tool_name_for(capability_id: str) -> str:
    """The deterministic provider-safe tool name for one capability.

    Sanitized to ``[A-Za-z0-9_-]`` (descriptor dots become underscores)
    and capped at 64 characters. Name collisions between enabled
    capabilities are a construction rejection (fail-closed), not a
    silent alias.
    """
    return _TOOL_NAME_UNSAFE.sub("_", capability_id)[:MAX_TOOL_NAME_CHARS]


def _validated_target(
    raw: object, valid_targets: tuple[uuid.UUID, ...]
) -> uuid.UUID | None:
    """One canonical-UUID target check against a valid set.

    Returns the parsed target when ``raw`` names exactly one valid
    target; ``None`` otherwise (a missing, malformed or out-of-set
    target — the caller refuses visibly, never guesses).
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = uuid.UUID(raw)
    except ValueError:
        return None
    if str(parsed) != raw or parsed not in valid_targets:
        return None
    return parsed


@dataclass(frozen=True, slots=True)
class _Binding:
    """One enabled capability's descriptor and its tool surface."""

    capability_id: str
    descriptor: CapabilityDescriptor
    tool: Tool


class CapabilityToolBridge:
    """Registered capabilities as PydanticAI tools over one dispatcher.

    Composition is constructor-checked (fail-closed wiring, the sibling
    pattern). The tool surface is built once from the context's enabled
    capabilities; a context is immutable, so the surface cannot drift.
    Every execution — model-facing tool call or host resume with a
    persisted approval — funnels through :meth:`dispatch_once`, the one
    seam into the dispatcher; there is no second path to the executor.
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        dispatcher: CapabilityDispatcher,
        context: ExecutionContext,
        exposure: ObjectiveExposure | None = None,
        locks: ResourceLockService | None = None,
    ) -> None:
        if not isinstance(registry, CapabilityRegistry):
            raise BridgeError(
                BridgeErrorCode.INVALID_WIRING,
                "registry must be a CapabilityRegistry",
            )
        if not isinstance(dispatcher, CapabilityDispatcher):
            raise BridgeError(
                BridgeErrorCode.INVALID_WIRING,
                "dispatcher must be a CapabilityDispatcher",
            )
        if not isinstance(context, ExecutionContext):
            raise BridgeError(
                BridgeErrorCode.INVALID_WIRING,
                "context must be an ExecutionContext",
            )
        if exposure is not None and not isinstance(exposure, ObjectiveExposure):
            raise BridgeError(
                BridgeErrorCode.INVALID_WIRING,
                "exposure must be an ObjectiveExposure or None",
            )
        if locks is not None and not isinstance(locks, ResourceLockService):
            raise BridgeError(
                BridgeErrorCode.INVALID_WIRING,
                "locks must be a ResourceLockService or None",
            )
        self._registry = registry
        self._dispatcher = dispatcher
        self._context = context
        self._exposure = exposure
        self._locks = locks
        bindings: dict[str, _Binding] = {}
        names: dict[str, str] = {}
        for capability_id in context.allowed_capabilities:
            versions = registry.versions(capability_id)
            if not versions:
                # Granted but not installed: nothing to expose. The
                # dispatcher would deny ``capability_not_registered``
                # anyway; exposure follows the registry, authorization
                # stays with the boundary.
                continue
            descriptor = registry.get(capability_id, versions[-1])
            assert descriptor is not None  # versions() just listed it
            name = tool_name_for(capability_id)
            collision = names.get(name)
            if collision is not None:
                raise BridgeError(
                    BridgeErrorCode.TOOL_NAME_COLLISION,
                    f"{collision!r} and {capability_id!r} both map to "
                    f"tool name {name!r}; rename one capability",
                )
            names[name] = capability_id
            bindings[capability_id] = _Binding(
                capability_id=capability_id,
                descriptor=descriptor,
                tool=self._build_tool(capability_id, descriptor, name),
            )
        self._bindings = bindings

    # -- surface -------------------------------------------------------------

    @property
    def tools(self) -> tuple[Tool, ...]:
        """The enabled tool surface (enabled descriptors only, AC1)."""
        return tuple(binding.tool for binding in self._bindings.values())

    @property
    def enabled_capabilities(self) -> tuple[str, ...]:
        """The capability ids behind :attr:`tools`, in context order."""
        return tuple(self._bindings)

    def descriptor_for(self, capability_id: str) -> CapabilityDescriptor | None:
        """The newest registered descriptor behind one enabled tool."""
        binding = self._bindings.get(capability_id)
        return None if binding is None else binding.descriptor

    def tools_for_phase(
        self,
        phase: object,
        action_of: Callable[[str, str], object],
    ) -> tuple[Tool, ...]:
        """The phase-guided tool surface (§6 guidance).

        Excludes enabled tools whose §6 phase action ``phase`` necessarily
        refuses (for a task-bound ``inspect`` round: target execute
        effects such as ``host.shell`` — the model is never invited into
        a necessarily-refused attempt). ``action_of`` is the
        caller-supplied ratified capability→action mapping
        ((capability id, effect class) → action), so task-state control
        tools stay classified exactly as the execution gate classifies
        them.

        D01 neutral extraction: this bridge no longer imports the task
        contracts, so ``phase``/``action_of`` results are compared by
        neutral value (``StrEnum.value`` or plain ``str``) against the
        local :func:`_phase_allows` mirror of the ratified phase×action
        admission map.

        GUIDANCE, not the permission boundary: the dispatcher's phase
        gate stays fail-closed at the execution boundary — replayed,
        stale, racing or direct calls are still denied before dispatch;
        this filter only shapes the offer.
        """
        kept: list[Tool] = []
        for binding in self._bindings.values():
            action = action_of(
                binding.capability_id,
                str(binding.descriptor.effect_class),
            )
            if _phase_allows(phase, action):
                kept.append(binding.tool)
        return tuple(kept)

    def _build_tool(
        self, capability_id: str, descriptor: CapabilityDescriptor, name: str
    ) -> Tool:
        """One schema-declared tool bound to this bridge and capability.

        The descriptor's ``input_schema`` is published verbatim as the
        function-argument schema; PydanticAI's schema-declared tools
        skip argument validation, which is exactly why the function
        re-validates against the same schema before dispatching.

        With a multi-target exposure (§6) the published schema GAINS the
        required ``server_id`` argument and the description names the
        capability's valid target set — every tool descriptor identifies
        its valid targets; no hidden global target selection. The
        adapter strips ``server_id`` before schema validation and
        dispatch, pinning the per-call context to the named target (the
        ONE dispatcher revalidates it against the current grants).
        """
        bridge = self
        targets = (
            () if self._exposure is None else self._exposure.targets(capability_id)
        )
        advisory = (
            "Every execution requires an approval decision through the "
            "durable approval path before the effect runs."
            if descriptor.effect_class == "privileged"
            else "Execution passes the common policy boundary; grants "
            "and policy are rechecked immediately before the effect."
        )
        root_description = (
            descriptor.input_schema.get("description")
            if isinstance(descriptor.input_schema, Mapping)
            and isinstance(descriptor.input_schema.get("description"), str)
            else None
        )
        prefix = f"{root_description} " if root_description else ""
        description = (
            f"{prefix}Capability {capability_id} v{descriptor.version} "
            f"({descriptor.effect_class} effect, targets: "
            f"{', '.join(descriptor.execution_environments)}). {advisory} "
            "Arguments must match the declared input schema."
        )
        schema = json.loads(json.dumps(descriptor.input_schema))
        if targets:
            target_list = ", ".join(str(t) for t in targets)
            description += (
                f" Valid targets (server_id): {target_list}. Every call "
                "names exactly one target server."
            )
            properties = schema.get("properties")
            required = schema.get("required")
            if not isinstance(properties, dict) or not isinstance(required, list):
                raise BridgeError(
                    BridgeErrorCode.INVALID_WIRING,
                    "a multi-target capability needs an object input schema "
                    "with properties and required",
                )
            if "server_id" in properties:
                raise BridgeError(
                    BridgeErrorCode.INVALID_WIRING,
                    "the exposure adapter reserves the server_id argument; "
                    "the descriptor already declares it",
                )
            properties["server_id"] = {
                "type": "string",
                "description": (
                    "Target server UUID; exactly one of: " + target_list
                ),
            }
            required.append("server_id")

        async def invoke_bridge_tool(**payload: Any) -> str:
            return await bridge._invoke_model_facing(capability_id, payload)

        return Tool.from_schema(
            invoke_bridge_tool,
            name=name,
            description=description,
            json_schema=schema,
        )

    # -- the one seam --------------------------------------------------------

    async def _invoke_model_facing(
        self, capability_id: str, payload: dict[str, Any]
    ) -> str:
        """Tool entrypoint: never raises, always a :class:`ToolResult`."""
        binding = self._bindings[capability_id]
        payload_hash = _try_payload_hash(payload)
        if payload_hash == _NON_CANONICAL:
            return ToolResult.rejected(
                context=self._context,
                capability_id=capability_id,
                payload_hash=payload_hash,
                error_code="invalid_request",
                error_detail="payload must be a JSON object (mapping)",
            ).to_json_str()
        try:
            # Multi-target exposure (§6): the model names its target; the
            # adapter pins the per-call context to it and strips the
            # argument before descriptor validation. A target outside the
            # capability's valid set is a visible refusal — a hidden
            # target is never callable, and the ONE dispatcher STILL
            # revalidates the pinned target against current grants.
            dispatch_context = self._context
            dispatch_payload: Mapping[str, Any] = payload
            if self._exposure is not None:
                work = dict(payload)
                raw_target = work.pop("server_id", None)
                valid_targets = self._exposure.targets(capability_id)
                parsed_target = _validated_target(raw_target, valid_targets)
                if parsed_target is None:
                    return ToolResult.rejected(
                        context=self._context,
                        capability_id=capability_id,
                        payload_hash=payload_hash,
                        error_code=BridgeErrorCode.TARGET_NOT_IN_SCOPE.value,
                        error_detail=(
                            "server_id must be exactly one of the "
                            "capability's valid targets: "
                            + ", ".join(str(t) for t in valid_targets)
                        ),
                    ).to_json_str()
                dispatch_context = replace(self._context, server_id=parsed_target)
                dispatch_payload = work
            violation = _validate_payload(
                dispatch_payload, binding.descriptor.input_schema, path="$"
            )
            if violation is not None:
                return ToolResult.rejected(
                    context=self._context,
                    capability_id=capability_id,
                    payload_hash=payload_hash,
                    error_code=BridgeErrorCode.PAYLOAD_SCHEMA_MISMATCH.value,
                    error_detail=violation,
                ).to_json_str()
            outcome = await self._dispatcher.dispatch(
                context=dispatch_context,
                capability_id=capability_id,
                payload=dispatch_payload,
                approval_id=None,
            )
            return ToolResult.from_outcome(outcome).to_json_str()
        except BridgeError as error:
            return ToolResult.rejected(
                context=self._context,
                capability_id=capability_id,
                payload_hash=payload_hash,
                error_code=error.code.value,
                error_detail=error.detail,
            ).to_json_str()
        except DispatchError as error:
            return ToolResult.rejected(
                context=self._context,
                capability_id=capability_id,
                payload_hash=payload_hash,
                error_code=error.code.value,
                error_detail=error.detail,
            ).to_json_str()
        except Exception as error:  # infrastructure: type name only
            return ToolResult.rejected(
                context=self._context,
                capability_id=capability_id,
                payload_hash=payload_hash,
                error_code="dispatch_failed",
                error_detail=type(error).__name__,
            ).to_json_str()

    async def dispatch_once(
        self,
        capability_id: str,
        payload: Mapping[str, Any],
        *,
        approval_id: uuid.UUID | None = None,
    ) -> DispatchOutcome:
        """Apply one capability once through the dispatcher (the seam).

        Used by the model-facing tools (no approval) and by the run
        layer resuming a persisted ``waiting_approval`` deferral (the
        approval id created for the recorded payload hash, C03 rule 4).
        Bridge-level pre-checks are misuse guards only; every
        *decision* — grants, policy, adoption, approval binding,
        immediate recheck — is the dispatcher's.
        """
        binding = self._bindings.get(capability_id)
        if binding is None:
            raise BridgeError(
                BridgeErrorCode.CAPABILITY_NOT_ENABLED,
                f"{capability_id!r} is not enabled for this context; "
                "there is no tool and no resume path for it",
            )
        violation = _validate_payload(
            payload, binding.descriptor.input_schema, path="$"
        )
        if violation is not None:
            raise BridgeError(BridgeErrorCode.PAYLOAD_SCHEMA_MISMATCH, violation)
        # FRR T5 security core: interactive sessions run DELEGATED, where a
        # `mutate` capability would execute WITHOUT approval (policy
        # evaluate(): requires_approval only in ASK). ``server.profile.update``
        # must be captain-approved on EVERY call, so this seam pins the
        # context mode to ASK for it alone — dataclasses.replace rebuilds the
        # frozen context with identical grants/policy/scope (a mode can only
        # ADD a prompt, never widen a grant). The resume path re-enters
        # through this same method, so ASK holds on resume too.
        dispatch_context = self._context
        if capability_id == SERVER_PROFILE_UPDATE_CAPABILITY:
            dispatch_context = replace(self._context, mode=PolicyMode.ASK)
        return await self._dispatcher.dispatch(
            context=dispatch_context,
            capability_id=capability_id,
            payload=payload,
            approval_id=approval_id,
        )

    # -- v2 multi-target seams (§6/§7; exposure adapter) --------------------

    async def fan_out_read(
        self,
        capability_id: str,
        payload: Mapping[str, Any],
        *,
        targets: Sequence[uuid.UUID] | None = None,
    ) -> list[DispatchOutcome]:
        """Dispatch one READ capability across scoped targets concurrently.

        Safe reads fan out (§7, AC4): one dispatch per target, each with
        its OWN context pinned to that target, run concurrently — the
        ONE dispatcher authorizes every target separately against
        current grants, so a denied target is a denied outcome in the
        result list, never a skipped entry. Only ``read``-effect
        capabilities fan out (mutations never do); ``targets`` must be a
        subset of the capability's valid exposure targets (order
        preserved in the result).
        """
        binding = self._bindings.get(capability_id)
        if binding is None:
            raise BridgeError(
                BridgeErrorCode.CAPABILITY_NOT_ENABLED,
                f"{capability_id!r} is not enabled for this context",
            )
        if self._exposure is None:
            raise BridgeError(
                BridgeErrorCode.INVALID_WIRING,
                "read fan-out requires a multi-target exposure",
            )
        if binding.descriptor.effect_class != "read":
            raise BridgeError(
                BridgeErrorCode.READ_FANOUT_UNSUPPORTED,
                f"{capability_id!r} is a "
                f"{binding.descriptor.effect_class} effect; only read "
                "capabilities fan out across targets",
            )
        valid = self._exposure.targets(capability_id)
        chosen = tuple(valid if targets is None else targets)
        valid_set = set(valid)
        for target in chosen:
            if target not in valid_set:
                raise BridgeError(
                    BridgeErrorCode.TARGET_NOT_IN_SCOPE,
                    f"{target} is not a valid target of {capability_id!r}",
                )
        coros = [
            self._dispatcher.dispatch(
                context=replace(self._context, server_id=target),
                capability_id=capability_id,
                payload=payload,
                approval_id=None,
            )
            for target in chosen
        ]
        return list(await asyncio.gather(*coros))

    async def dispatch_under_lock(
        self,
        capability_id: str,
        payload: Mapping[str, Any],
        *,
        resource: str,
        owner_ref: str = "",
        ttl: float | None = None,
    ) -> DispatchOutcome:
        """Dispatch one mutation under its declared resource lock (AC4).

        Overlapping mutations serialize through the declared
        :class:`~mai.jobs.resource_locks.ResourceLockService` lease on
        ``(server_id, resource)``: the effect runs ONLY inside the lease
        (acquire → dispatch → release, released on every exit path). A
        live competing lease raises the structured ``lock_contention``
        :class:`BridgeError` — the lock service never waits, so the
        caller (model surface or step) retries; effects never interleave.
        """
        binding = self._bindings.get(capability_id)
        if binding is None:
            raise BridgeError(
                BridgeErrorCode.CAPABILITY_NOT_ENABLED,
                f"{capability_id!r} is not enabled for this context",
            )
        if self._locks is None:
            raise BridgeError(
                BridgeErrorCode.INVALID_WIRING,
                "lock-fenced dispatch requires a ResourceLockService",
            )
        try:
            lease = self._locks.acquire(
                str(self._context.server_id),
                resource,
                owner_ref=owner_ref,
                ttl=ttl,
            )
        except ResourceLockError as error:
            raise BridgeError(
                BridgeErrorCode.LOCK_CONTENTION,
                f"{error.code.value}: {error.detail}",
            ) from None
        try:
            return await self._dispatcher.dispatch(
                context=self._context,
                capability_id=capability_id,
                payload=payload,
                approval_id=None,
            )
        finally:
            self._locks.release(lease)

    def __repr__(self) -> str:
        return (
            f"<CapabilityToolBridge tools={len(self._bindings)} "
            f"run={self._context.run_id}>"
        )


# ---------------------------------------------------------------------------
# Pre-dispatch payload gate (AC2: schema mismatch never executes)
# ---------------------------------------------------------------------------


def _try_payload_hash(payload: object) -> str:
    """Canonical payload hash, or the rejection marker if not canonical."""
    try:
        return canonical_payload_hash(payload)
    except DispatchError:
        return _NON_CANONICAL


def _json_equal(left: object, right: object) -> bool:
    """JSON equality: ``True`` never equals ``1`` (bool is its own type)."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right


def _check_type(value: object, expected: object) -> bool:
    """One JSON Schema type assertion (bool is never a number)."""
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and value.is_integer()
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False  # unknown type name: fail-closed


def _validate_payload(
    value: object, schema: object, *, path: str, depth: int = 0
) -> str | None:
    """Validate one payload against the descriptor's input schema.

    Returns ``None`` when the payload satisfies the schema, else a
    value-free violation path/description (payload values are never
    quoted — they may carry credential-shaped material). An unsupported
    applicative keyword is itself a violation: fail-closed, the call
    can never execute until the schema producer reconciles.
    """
    if depth > _MAX_SCHEMA_DEPTH:
        return f"{path}: schema nesting exceeds the supported depth"
    if not isinstance(schema, Mapping):
        return f"{path}: schema must be a JSON object"
    for keyword in schema:
        if keyword not in _SUPPORTED_KEYWORDS and keyword not in _ANNOTATION_KEYWORDS:
            return f"{path}: schema uses unsupported keyword {keyword!r}"

    expected_type = schema.get("type")
    if expected_type is not None:
        names = expected_type if isinstance(expected_type, list) else [expected_type]
        if not names or any(name not in _JSON_TYPES for name in names):
            return f"{path}: schema declares an unsupported type"
        if not any(_check_type(value, name) for name in names):
            return (
                f"{path}: payload does not match declared type "
                f"{[str(name) for name in names]}"
            )

    if "const" in schema and not _json_equal(value, schema["const"]):
        return f"{path}: payload does not match the const value"
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list):
            return f"{path}: schema enum must be an array"
        if not any(_json_equal(value, choice) for choice in choices):
            return f"{path}: payload is not one of the enum values"

    if isinstance(value, Mapping):
        return _validate_object(value, schema, path=path, depth=depth)
    if isinstance(value, list):
        return _validate_array(value, schema, path=path, depth=depth)
    if isinstance(value, str):
        return _validate_string(value, schema, path=path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _validate_number(value, schema, path=path)
    return None


def _validate_object(
    value: Mapping[object, object],
    schema: Mapping[object, object],
    *,
    path: str,
    depth: int,
) -> str | None:
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return f"{path}: schema properties must be an object"
    for key in schema.get("required", []):
        if not isinstance(key, str):
            return f"{path}: schema required entries must be strings"
        if key not in value:
            return f"{path}.{key}: required property is missing"
    additional = schema.get("additionalProperties", True)
    for key, item in value.items():
        if not isinstance(key, str):
            return f"{path}: property names must be strings"
        if key in properties:
            violation = _validate_payload(
                item, properties[key], path=f"{path}.{key}", depth=depth + 1
            )
            if violation is not None:
                return violation
        elif additional is False:
            return f"{path}.{key}: property is not allowed"
        elif isinstance(additional, Mapping):
            violation = _validate_payload(
                item, additional, path=f"{path}.{key}", depth=depth + 1
            )
            if violation is not None:
                return violation
    return None


def _validate_array(
    value: list[object],
    schema: Mapping[object, object],
    *,
    path: str,
    depth: int,
) -> str | None:
    min_items = schema.get("minItems")
    max_items = schema.get("maxItems")
    for name, bound, comparison in (
        (
            "minItems",
            min_items,
            len(value) < min_items if min_items is not None else False,
        ),
        (
            "maxItems",
            max_items,
            len(value) > max_items if max_items is not None else False,
        ),
    ):
        if bound is not None and (
            not isinstance(bound, int) or isinstance(bound, bool)
        ):
            return f"{path}: schema {name} must be an integer"
        if comparison:
            return f"{path}: array violates the {name} bound"
    items = schema.get("items")
    if items is not None:
        for index, element in enumerate(value):
            violation = _validate_payload(
                element, items, path=f"{path}[{index}]", depth=depth + 1
            )
            if violation is not None:
                return violation
    return None


def _validate_string(
    value: str, schema: Mapping[object, object], *, path: str
) -> str | None:
    min_length = schema.get("minLength")
    max_length = schema.get("maxLength")
    for name, bound, comparison in (
        (
            "minLength",
            min_length,
            len(value) < min_length if min_length is not None else False,
        ),
        (
            "maxLength",
            max_length,
            len(value) > max_length if max_length is not None else False,
        ),
    ):
        if bound is not None and (
            not isinstance(bound, int) or isinstance(bound, bool) or bound < 0
        ):
            return f"{path}: schema {name} must be a non-negative integer"
        if comparison:
            return f"{path}: string violates the {name} bound"
    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            return f"{path}: schema pattern must be a string"
        try:
            if re.search(pattern, value) is None:
                return f"{path}: string does not match the declared pattern"
        except re.error:
            return f"{path}: schema pattern is not a valid regex"
    return None


def _validate_number(
    value: int | float, schema: Mapping[object, object], *, path: str
) -> str | None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    exclusive_minimum = schema.get("exclusiveMinimum")
    exclusive_maximum = schema.get("exclusiveMaximum")
    for name, bound in (
        ("minimum", minimum),
        ("maximum", maximum),
        ("exclusiveMinimum", exclusive_minimum),
        ("exclusiveMaximum", exclusive_maximum),
    ):
        if bound is not None and (
            not isinstance(bound, (int, float)) or isinstance(bound, bool)
        ):
            return f"{path}: schema {name} must be a number"
    if minimum is not None and value < minimum:
        return f"{path}: number is below the minimum"
    if maximum is not None and value > maximum:
        return f"{path}: number is above the maximum"
    if exclusive_minimum is not None and not value > exclusive_minimum:
        return f"{path}: number violates the exclusiveMinimum bound"
    if exclusive_maximum is not None and not value < exclusive_maximum:
        return f"{path}: number violates the exclusiveMaximum bound"
    return None
