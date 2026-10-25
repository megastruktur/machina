"""Evidence-backed task completion (act ``20-completion``; act_CONTRACTS §9).

Accept a typed completion proposal, derive the honest task outcome, and
record it through the ratified store seam — WITHOUT conflating a finished
run attempt with a verified goal.

The two layers this module separates (§9; scenario E1):

- **Technical run completion** — the frozen C04/``RunState`` machine owns
  it; a finished attempt reports ``succeeded`` regardless of what the
  criteria say. This module never touches it.
- **Domain task verification** — ``verified`` is reachable ONLY through
  an evidence-backed criterion set: every acceptance criterion must end
  up explicitly supported (``passed``), ``failed`` or ``unavailable``,
  with the cited evidence existing in the task scope (the ratified
  validator) and its recorded certainty being COMPATIBLE with the claim
  (this module; scenarios E2 — stale, wrong-target or fabricated refs
  cannot pass).

Authority discipline:

- The proposal carries NO writable outcome field (the §5 rule "the output
  schema contains no ... verified outcomes" applied to completion): the
  outcome is always DERIVED from the validated per-criterion states.
- The final assistant text is never consulted. Nothing parses prose; the
  only accepted input is this module's typed
  :class:`CompletionProposal`.
- Derivation never records a terminal state except ``verified`` (every
  criterion passed): a task with ``failed``/``unavailable`` criteria
  stays OPEN and resumable (P3) — repair, revision and cancellation
  remain explicit later decisions.
- Validation authority is NOT re-implemented: recording funnels through
  :func:`mai.agents.task_store.apropose_completion`, which runs the
  ratified :func:`mai.contracts.tasks.validate_completion_verdict`
  (existence, task scope, coverage, CAS, legal transitions) inside its
  transaction — a rejection there has zero effects.

Certainty compatibility (the module's own deterministic check, E1/E2):
a ``passed`` claim is admissible only when at least one cited ref carries
a RECORDED observation with ``effect_certainty=certain`` AND
``domain_outcome=success`` (the only combination the act-19 projection
ever certifies); a ``failed`` claim that cites refs needs at least one
recorded domain failure (an unproven effect — ``effect_certainty=unknown``
— can never report a failed criterion, only an ``unavailable`` one).
Certainty records ride the proposal as :class:`EvidenceCertainty` values
(the producer copies them from the act-19 ``projection`` blocks it
observed); a cited ref with NO record is certainty-unknown and fails the
compatibility check closed.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents import task_store
from mai.contracts.tasks import (
    MAX_CRITERIA,
    MAX_EVIDENCE_REFS,
    CriterionResult,
    DomainOutcome,
    EffectCertainty,
    EvidenceRef,
    ExecutionStatus,
    TaskCompletionVerdict,
    TaskContractError,
    TaskOutcome,
    VerificationStatus,
)

__all__ = [
    "COMPLETION_PROPOSAL_SCHEMA_VERSION",
    "CompletionProposal",
    "CriterionResolution",
    "EvidenceCertainty",
    "OutcomeObservation",
    "afinalize_completion",
    "aobserve_task_outcome",
    "derive_task_outcome",
    "resolve_proposed_criteria",
    "resolve_recorded_criteria",
]

#: Payload schema version of :class:`CompletionProposal`. Consumers fail
#: closed on any other value (server-contract pattern, mirrored from
#: ``mai.contracts.tasks.SCHEMA_VERSION``): a newer producer must bump
#: and reconcile, never drift silently.
COMPLETION_PROPOSAL_SCHEMA_VERSION: Final[int] = 1

#: Additive run-summary key the final seam fills with the observation
#: document (value-free; absent for taskless runs so every legacy
#: surface stays byte-identical).
TASK_OUTCOME_SUMMARY_KEY: Final[str] = "task_outcome"

#: Rejection detail codes this module adds to the contract's own detail
#: vocabulary. The contract enum is frozen, so the one new rule (recorded
#: certainty vs claimed result) carries a module-local stable code; every
#: other rejection reuses the exact contract code strings.
CERTAINTY_INCOMPATIBLE: Final[str] = "certainty_incompatible"

# ---------------------------------------------------------------------------
# typed proposal (§9 ``task.propose_completion`` payload; no writable
# outcome — the outcome is derived, never self-granted)
# ---------------------------------------------------------------------------


class EvidenceCertainty(BaseModel):
    """One RECORDED certainty observation for a cited evidence ref.

    The producer copies these from the act-19 ``projection`` blocks of
    the tool results it observed (``domain_outcome`` /
    ``effect_certainty`` are frozen contract vocabularies). The module
    never invents certainty: a cited ref without a record is treated as
    certainty-unknown and cannot back a ``passed``/``failed`` claim.
    """

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(
        min_length=1,
        max_length=64,
        description="Evidence reference id the observation was recorded for",
    )
    domain_outcome: DomainOutcome = Field(
        description="Recorded domain outcome of the cited observation",
    )
    effect_certainty: EffectCertainty = Field(
        description="Recorded certainty of the cited observation",
    )


class CompletionProposal(BaseModel):
    """Typed completion proposal (§9; no self-grantable outcome).

    Carries the requirement-result map, the closing attempt's own honest
    projection and the recorded certainty observations for the cited
    evidence. Deliberately has NO ``task_outcome`` field: the module
    derives it from the validated criterion states, so a proposal can
    never claim its way to ``verified``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(
        default=COMPLETION_PROPOSAL_SCHEMA_VERSION,
        description="Proposal schema version; consumers fail closed on drift",
    )
    task_id: uuid.UUID = Field(description="Opaque stable task identity (§1)")
    base_revision: int = Field(
        ge=1,
        description="Task revision this proposal was produced against (CAS)",
    )
    idempotency_key: uuid.UUID = Field(
        description="Caller key so retransmitted proposals deduplicate (C4)",
    )
    execution_status: ExecutionStatus = Field(
        description="Technical status of the closing attempt (§9)",
    )
    domain_outcome: DomainOutcome = Field(
        description="Domain outcome of the closing attempt; unknown stays unknown",
    )
    effect_certainty: EffectCertainty = Field(
        description="Certainty of the closing attempt's observed effects",
    )
    error_code: str | None = Field(
        default=None,
        max_length=64,
        description="Stable machine-readable error code, when failed",
    )
    retry_hint: str | None = Field(
        default=None,
        max_length=500,
        description="Advice only; policy decides what may execute (§9)",
    )
    criterion_results: list[CriterionResult] = Field(
        default_factory=list,
        max_length=MAX_CRITERIA,
        description="Requirement-result map bound to evidence",
    )
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list,
        max_length=MAX_EVIDENCE_REFS,
        description="Aggregate evidence supporting the proposal",
    )
    evidence_certainty: list[EvidenceCertainty] = Field(
        default_factory=list,
        max_length=2 * MAX_EVIDENCE_REFS,
        description="Recorded certainty observations for cited evidence refs",
    )

    @field_validator("schema_version")
    @classmethod
    def _version_supported(cls, value: int) -> int:
        if value != COMPLETION_PROPOSAL_SCHEMA_VERSION:
            raise ValueError(
                "unsupported completion proposal schema version "
                f"(supported: {COMPLETION_PROPOSAL_SCHEMA_VERSION})"
            )
        return value


# ---------------------------------------------------------------------------
# derivation results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CriterionResolution:
    """The explicit end state of ONE acceptance criterion (AC 1).

    Every known criterion resolves — covered or not, accepted or not —
    so a criterion is never silently dropped and never assumed
    supported. ``state`` uses the frozen contract vocabulary
    (:class:`VerificationStatus`; ``passed`` ≙ supported).
    """

    criterion_id: str
    state: VerificationStatus
    #: Where the state came from: ``recorded`` = a validated verdict
    #: advanced the stored row; ``derived`` = this module's explicit
    #: derivation (never written back as verification).
    source: Literal["recorded", "derived"]
    #: Stable detail code when the claim could not be honored
    #: (``certainty_incompatible``, ``missing_evidence_refs``, ...).
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class OutcomeObservation:
    """The outcome of one finalization/observation call (§9 receipt view).

    ``recorded=True`` means a validated verdict was persisted (the
    episode's header, criteria and revision advanced transactionally).
    ``recorded=False`` with ``fields`` means the proposal was REJECTED
    with zero effects (E2 fail-closed direction). The seam observation
    path always records nothing — a finished attempt alone is never a
    verification.
    """

    task_id: uuid.UUID | None
    outcome: TaskOutcome | None
    criteria: tuple[CriterionResolution, ...]
    recorded: bool
    verified: bool
    disposition: str | None = None
    revision: int | None = None
    top_level_code: str | None = None
    fields: Mapping[str, str] | None = None

    def document(self) -> dict[str, object]:
        """The value-free additive document for the run summary.

        Identifiers and frozen-vocabulary states only — never criterion
        statements, model prose or payload material (C05 rule 4).
        """
        counts = {"supported": 0, "failed": 0, "unavailable": 0}
        for resolution in self.criteria:
            if resolution.state is VerificationStatus.PASSED:
                counts["supported"] += 1
            elif resolution.state is VerificationStatus.FAILED:
                counts["failed"] += 1
            else:
                counts["unavailable"] += 1
        document: dict[str, object] = {
            "task_id": None if self.task_id is None else str(self.task_id),
            "outcome": None if self.outcome is None else self.outcome.value,
            "verified": self.verified,
            "recorded": self.recorded,
            "criteria": {
                "total": len(self.criteria),
                **counts,
            },
            "criterion_states": [
                {
                    "criterion_id": resolution.criterion_id,
                    "state": resolution.state.value,
                }
                for resolution in self.criteria
            ],
            "proposal": "accepted"
            if self.recorded
            else ("rejected" if self.fields else "none"),
        }
        if self.disposition is not None:
            document["disposition"] = self.disposition
        if self.revision is not None:
            document["revision"] = self.revision
        if self.top_level_code is not None:
            document["rejection_code"] = self.top_level_code
        if self.fields:
            document["rejection_fields"] = dict(self.fields)
        return document


# ---------------------------------------------------------------------------
# pure derivation (AC 1: explicit, evidence-backed, never assumed)
# ---------------------------------------------------------------------------


def _certainty_of(
    index: Mapping[str, EvidenceCertainty], ref: EvidenceRef
) -> EvidenceCertainty | None:
    return index.get(str(ref.evidence_id))


def _supports_passed(record: EvidenceCertainty) -> bool:
    """The ONLY combination the act-19 projection certifies as proven
    success (executed + structured consistent success evidence)."""
    return (
        record.effect_certainty is EffectCertainty.CERTAIN
        and record.domain_outcome is DomainOutcome.SUCCESS
    )


def _supports_failed(record: EvidenceCertainty) -> bool:
    """Explicit negative evidence proves a failure; an unproven effect
    (``unknown`` certainty — nothing ran / unresolvable) never does."""
    return (
        record.domain_outcome is DomainOutcome.FAILURE
        and record.effect_certainty is not EffectCertainty.UNKNOWN
    )


def resolve_proposed_criteria(
    known_criterion_ids: Sequence[str],
    results: Sequence[CriterionResult],
    certainty_index: Mapping[str, EvidenceCertainty],
) -> tuple[dict[str, CriterionResolution], dict[str, str]]:
    """Resolve every known criterion against one proposal (pure; AC 1/E2).

    Returns ``(resolutions, rejections)``. ``resolutions`` covers EVERY
    known criterion id — a criterion the proposal does not mention is
    explicitly ``unavailable``, never assumed supported. ``rejections``
    maps field path → stable code and is NON-EMPTY exactly when the
    proposal must be refused with zero effects:

    - a ``passed`` claim needs at least one cited ref whose recorded
      certainty is ``certain`` + domain ``success`` — otherwise
      ``certainty_incompatible`` (or the contract-mirrored
      ``missing_evidence_refs`` when it cites nothing);
    - a ``failed`` claim that cites refs needs at least one recorded
      domain failure — certainty-unknown or success-only citations are
      ``certainty_incompatible`` (a failed claim with NO refs mirrors
      the ratified validator and stays admissible: failure claims are
      not self-grants);
    - an ``unavailable`` claim is always compatible (honest
      "could not verify").
    """
    resolutions: dict[str, CriterionResolution] = {}
    rejections: dict[str, str] = {}
    for index, result in enumerate(results):
        field = f"criterion_results[{index}]"
        if result.criterion_id not in known_criterion_ids:
            # Unknown-criterion coverage is the ratified validator's
            # authority (fail closed at the store); mirror the code here
            # so the caller sees one rejection shape.
            rejections.setdefault(f"{field}.criterion_id", "unknown_criterion")
            resolutions[result.criterion_id] = CriterionResolution(
                criterion_id=result.criterion_id,
                state=VerificationStatus.UNAVAILABLE,
                source="derived",
                reason_code="unknown_criterion",
            )
            continue
        if result.result is VerificationStatus.PASSED:
            if not result.evidence_refs:
                rejections.setdefault(field, "missing_evidence_refs")
                resolutions[result.criterion_id] = CriterionResolution(
                    criterion_id=result.criterion_id,
                    state=VerificationStatus.UNAVAILABLE,
                    source="derived",
                    reason_code="missing_evidence_refs",
                )
                continue
            if not any(
                _supports_passed(record)
                for record in (
                    _certainty_of(certainty_index, ref) for ref in result.evidence_refs
                )
                if record is not None
            ):
                rejections.setdefault(field, CERTAINTY_INCOMPATIBLE)
                resolutions[result.criterion_id] = CriterionResolution(
                    criterion_id=result.criterion_id,
                    state=VerificationStatus.UNAVAILABLE,
                    source="derived",
                    reason_code=CERTAINTY_INCOMPATIBLE,
                )
                continue
            resolutions[result.criterion_id] = CriterionResolution(
                criterion_id=result.criterion_id,
                state=VerificationStatus.PASSED,
                source="derived",
            )
        elif result.result is VerificationStatus.FAILED:
            if result.evidence_refs and not any(
                _supports_failed(record)
                for record in (
                    _certainty_of(certainty_index, ref) for ref in result.evidence_refs
                )
                if record is not None
            ):
                rejections.setdefault(field, CERTAINTY_INCOMPATIBLE)
                resolutions[result.criterion_id] = CriterionResolution(
                    criterion_id=result.criterion_id,
                    state=VerificationStatus.UNAVAILABLE,
                    source="derived",
                    reason_code=CERTAINTY_INCOMPATIBLE,
                )
                continue
            resolutions[result.criterion_id] = CriterionResolution(
                criterion_id=result.criterion_id,
                state=VerificationStatus.FAILED,
                source="derived",
            )
        else:  # VerificationStatus.UNAVAILABLE — always compatible
            resolutions[result.criterion_id] = CriterionResolution(
                criterion_id=result.criterion_id,
                state=VerificationStatus.UNAVAILABLE,
                source="derived",
            )
    for criterion_id in known_criterion_ids:
        if criterion_id not in resolutions:
            # Explicit, never dropped, never assumed supported (AC 1).
            resolutions[criterion_id] = CriterionResolution(
                criterion_id=criterion_id,
                state=VerificationStatus.UNAVAILABLE,
                source="derived",
                reason_code="not_proposed",
            )
    return resolutions, rejections


def resolve_recorded_criteria(
    stored: Mapping[str, VerificationStatus],
) -> dict[str, CriterionResolution]:
    """The explicit states of every known criterion from STORED truth.

    The observation path uses this: a finished attempt consults what
    validated verdicts already recorded and renders every criterion
    explicitly — ``passed`` as supported, everything else honestly
    unavailable — without ever writing verification state itself.
    """
    return {
        criterion_id: CriterionResolution(
            criterion_id=criterion_id,
            state=state
            if state is not VerificationStatus.NOT_CHECKED
            else (VerificationStatus.UNAVAILABLE),
            source="recorded",
        )
        for criterion_id, state in stored.items()
    }


def derive_task_outcome(
    states: Mapping[str, VerificationStatus],
) -> TaskOutcome:
    """Derive the episode outcome from explicit criterion states (pure).

    - every criterion ``passed`` (and at least one exists) →
      ``verified`` — the ONLY terminal state this path can produce;
    - at least one ``passed`` (rest failed/unavailable) →
      ``partially_verified`` — open, resumable (E1 "partial");
    - otherwise → ``in_progress`` — open, resumable; a proven-failed
      criterion demands repair or an explicit user decision, never a
      silently closed episode (P3: unfinished tasks continue safely).

    An empty criterion set is ``in_progress``: ``verified`` without any
    criterion would be a vacuous self-grant.
    """
    if not states:
        return TaskOutcome.IN_PROGRESS
    values = list(states.values())
    if all(state is VerificationStatus.PASSED for state in values):
        return TaskOutcome.VERIFIED
    if any(state is VerificationStatus.PASSED for state in values):
        return TaskOutcome.PARTIALLY_VERIFIED
    return TaskOutcome.IN_PROGRESS


# ---------------------------------------------------------------------------
# bounded reads (deterministic order; the criterion set is invariant-
# bounded at MAX_CRITERIA by the store, mirroring the store's own
# internal completion-path load)
# ---------------------------------------------------------------------------

_CRITERIA_SQL: Final[str] = (
    "SELECT criterion_id, verification FROM agent_task_criteria "
    "WHERE task_id = :tid ORDER BY position, criterion_id"
)

_TASK_FOR_RUN_SQL: Final[str] = (
    "SELECT t.task_id, t.server_id FROM conversation_runs r "
    "JOIN agent_tasks t ON t.task_id = r.task_id "
    "WHERE r.run_id = :rid "
    "ORDER BY r.created_at DESC, r.run_id LIMIT 1"
)


async def _astored_criteria(
    session: AsyncSession, task_id: uuid.UUID
) -> dict[str, VerificationStatus]:
    rows = (await session.execute(text(_CRITERIA_SQL), {"tid": task_id})).all()
    stored: dict[str, VerificationStatus] = {}
    for criterion_id, verification in rows:
        try:
            stored[str(criterion_id)] = VerificationStatus(str(verification))
        except ValueError:
            # Unknown stored value: fail closed to unavailable, never
            # guess a verification state.
            stored[str(criterion_id)] = VerificationStatus.UNAVAILABLE
    return stored


# ---------------------------------------------------------------------------
# seam observation (final-response seam; records nothing)
# ---------------------------------------------------------------------------


async def aobserve_task_outcome(
    sessions: async_sessionmaker[AsyncSession],
    *,
    server_id: uuid.UUID,
    run_id: str,
) -> dict[str, object] | None:
    """The additive final-seam observation for one finishing run.

    Resolves the task bound to the run (``conversation_runs`` link, the
    same seam the phase gate reads), derives the HONEST outcome from the
    recorded criterion states and returns the value-free document — or
    ``None`` for an unbound/out-of-scope run (C8 hides existence), so
    taskless runs keep every legacy surface byte-identical.

    Records NOTHING: no verdict, no revision, no criterion advance, no
    terminal transition. A finished attempt whose final text claims
    success therefore cannot close or verify the episode — the task
    stays exactly as resumable as it was (AC 2). ``verified`` in the
    document can only ever mirror a previously validated verdict.
    """
    async with sessions() as session:
        row = (
            await session.execute(text(_TASK_FOR_RUN_SQL), {"rid": uuid.UUID(run_id)})
        ).first()
        if row is None:
            return None
        task_id, task_server = uuid.UUID(str(row[0])), uuid.UUID(str(row[1]))
        if task_server != server_id:
            # Cross-scope link: fail closed, hide existence (C8).
            return None
        stored = await _astored_criteria(session, task_id)
    resolutions = resolve_recorded_criteria(stored)
    outcome = derive_task_outcome(stored)
    observation = OutcomeObservation(
        task_id=task_id,
        outcome=outcome,
        criteria=tuple(resolutions[criterion_id] for criterion_id in stored),
        recorded=False,
        verified=outcome is TaskOutcome.VERIFIED,
    )
    return observation.document()


# ---------------------------------------------------------------------------
# proposal finalization (accept → derive → record through the store)
# ---------------------------------------------------------------------------


async def afinalize_completion(
    sessions: async_sessionmaker[AsyncSession],
    *,
    server_id: uuid.UUID,
    proposal: CompletionProposal,
    author: Literal["user", "model"] = "model",
) -> OutcomeObservation:
    """Accept one typed completion proposal and record its derived outcome.

    Pipeline (every rejection below has ZERO effects — no receipt, no
    revision bump, no criterion advance, no terminal transition):

    1. typed-shape + schema-version rejection (Pydantic, at construction);
    2. the module's certainty-compatibility check
       (:func:`resolve_proposed_criteria`, E1/E2);
    3. the DERIVED outcome (:func:`derive_task_outcome`) is written into
       a :class:`TaskCompletionVerdict` — the proposal itself carries no
       outcome to grant;
    4. :func:`mai.agents.task_store.apropose_completion` re-validates
       with the ratified authority (criteria existence, task-scoped
       evidence registry, coverage, CAS ``base_revision`` for staleness,
       legal transitions) inside one transaction and records the receipt.

    A task with ``failed``/``unavailable`` criteria derives an OPEN
    outcome and stays resumable; only a fully supported criterion set
    can derive ``verified``.
    """
    try:
        async with sessions() as session:
            stored = await _astored_criteria(session, proposal.task_id)
    except TaskContractError:
        stored = {}
    certainty_index = {
        record.evidence_id: record for record in proposal.evidence_certainty
    }
    resolutions, rejections = resolve_proposed_criteria(
        list(stored), proposal.criterion_results, certainty_index
    )
    if rejections:
        return OutcomeObservation(
            task_id=proposal.task_id,
            outcome=None,
            criteria=tuple(resolutions[criterion_id] for criterion_id in stored),
            recorded=False,
            verified=False,
            top_level_code="completion_missing_evidence",
            fields=dict(rejections),
        )
    # The outcome derives from the RESOLVED proposal states (the claim
    # set plus the explicit unavailable coverage), never from the stale
    # stored snapshot alone.
    outcome = derive_task_outcome(
        {
            resolution.criterion_id: resolution.state
            for resolution in resolutions.values()
        }
    )
    verdict = TaskCompletionVerdict(
        task_id=proposal.task_id,
        base_revision=proposal.base_revision,
        idempotency_key=proposal.idempotency_key,
        task_outcome=outcome,
        execution_status=proposal.execution_status,
        domain_outcome=proposal.domain_outcome,
        effect_certainty=proposal.effect_certainty,
        error_code=proposal.error_code,
        retry_hint=proposal.retry_hint,
        evidence_refs=list(proposal.evidence_refs),
        criterion_results=list(proposal.criterion_results),
    )
    try:
        result = await task_store.apropose_completion(
            sessions,
            proposal.task_id,
            server_id=server_id,
            verdict=verdict,
            author=author,
        )
    except TaskContractError as exc:
        return OutcomeObservation(
            task_id=proposal.task_id,
            outcome=None,
            criteria=tuple(resolutions[criterion_id] for criterion_id in stored),
            recorded=False,
            verified=False,
            top_level_code="completion_missing_evidence",
            fields=dict(exc.fields),
        )
    return OutcomeObservation(
        task_id=proposal.task_id,
        outcome=result.task.status,
        criteria=tuple(resolutions[criterion_id] for criterion_id in stored),
        recorded=True,
        verified=result.task.status is TaskOutcome.VERIFIED,
        disposition=result.disposition.value,
        revision=result.task.revision,
    )
