"""Common enforced capability execution boundary (contracts.md **C03**).

Card ``03-dispatcher``. This module is the one seam every capability
application passes through (C03 ownership row; SERVICES "End-to-end
execution route": context/grant/approval gates before any effect). It
composes the reviewed producers — the declarative
:class:`~mai.plugins.registry.CapabilityRegistry` (C03 rule 1), the pure
decision function :func:`~mai.policy.evaluate.evaluate` (C03 rules 3–5),
the payload-bound one-use :class:`~mai.policy.approvals.ApprovalStore`
(C03 rule 4), the ``.env`` :class:`~mai.credentials.env.EnvResolver`
(C03 rule 6) and the :class:`~mai.policy.config.GrantConfigStore`
policy/grant state — and owns nothing they already own.

The dispatch sequence (frozen order, fail-closed at every seam)
---------------------------------------------------------------

1. **Request validation** — capability slug shape, JSON-safe canonical
   payload, optional approval UUID. Malformed requests raise
   :class:`DispatchError` *before any state is read, nothing is
   consumed and no effect is planned* (construction-rejection pattern of
   the sibling modules).
2. **Policy gate** — current grants/policy/adoption are fetched from
   caller-supplied providers and
   :func:`~mai.policy.evaluate.evaluate` decides. Every ambiguity is a
   deny *outcome* with a stable reason (never an exception, never a
   partial allow). A deny outcome invokes no executor, consumes no
   approval and resolves no credential.
3. **Approval seam** — when the decision says the effect requires an
   approval decision, one is consumed atomically, bound to capability,
   target, canonical payload hash and current policy revision
   (C03 rule 4; C05 ``waiting_approval``). Any
   :class:`~mai.policy.approvals.ApprovalError` denies with the store's
   stable code recorded on the receipt. A store *infrastructure* failure
   (database outage) propagates as an exception — nothing was consumed
   and no effect is planned; surfacing it is the host's retry concern.
   Presenting an approval for a call that needs none is itself denied:
   an unvalidated decision must never ride along on a weaker call.
4. **Credential seam** — the context's credential *references*
   (C03 rule 2: names only, never values) resolve from the configured
   ``.env`` source at effect time (C03 rule 6). Any resolver failure
   denies with a value-free reason; resolved
   :class:`~mai.credentials.env.SecretValue` material exists only as
   local call input and is dropped with the call frame.
5. **Immediate recheck** — grants/policy/adoption are fetched *again*
   and :func:`~mai.policy.evaluate.evaluate` runs again immediately
   before the effect (C03 rule 3, auth ADR §8 G2 step 4). A changed
   verdict denies here even if an approval was already spent — the
   receipt honestly reports the spent decision
   (:attr:`DispatchOutcome.approval_spent`); an approval bound to the
   old policy revision can never match after a revocation bump, so a
   privileged effect is doubly fenced.
6. **Effect** — the caller-supplied executor (the host's trusted
   capability implementation; C03 rule 7 — the registry is declarative
   and deliberately stores no code) receives a typed
   :class:`CapabilityCall` carrying the resolved secrets. An executor
   crash can never be reported as clean: the receipt records
   ``effect="unknown"`` — the effect may or may not have happened
   (host rule 3 honesty, applied in-process) — with the exception
   *type name* only (never its message, which may quote secrets).
7. **Output boundary** — executor output is redacted (every resolved
   secret value replaced, defense-in-depth per C03 rule 6) *before* it
   is bounded to the configured byte budget with an explicit truncation
   notice (C06 rule 3). The :class:`DispatchOutcome` is the audit
   receipt: it embeds the decision, the approval id, the canonical
   payload **hash** (never the payload — the payload may quote
   credential-shaped material) and the bounded output. Its JSON shape
   carries no secret value, no credential value and no executor
   exception text.

Why there is no CLI
-------------------

Sibling modules expose ``python -m`` entrypoints because they decide or
parse. This module *executes effects*; a generic "run this request
document" CLI would be exactly the alternate execution backdoor the
SERVICES route forbids. Composition belongs to the owning host process
(worker/API), which supplies the providers and the executor. The
focused gate-scenario evidence runs the real composed object from a
fresh OS process instead.

Ownership note: the effect transport (SSH receipts, timeouts,
cancellation) is C04/P04 business and deliberately absent here — the
executor interface is the seam where it will attach.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from mai.credentials.env import EnvFileError, EnvResolver, SecretValue
from mai.plugins.registry import CapabilityDescriptor, CapabilityRegistry
from mai.policy.approvals import ApprovalError, ApprovalStore
from mai.policy.evaluate import (
    DecisionReason,
    ExecutionContext,
    Policy,
    ScopedGrants,
    evaluate,
)

__all__ = [
    "CapabilityCall",
    "DispatchError",
    "DispatchErrorCode",
    "DispatchOutcome",
    "DispatchReason",
    "ExecutorResult",
    "MAX_OUTPUT_BUDGET_BYTES",
    "REDACTED",
    "canonical_payload_hash",
    "canonical_payload_json",
    "default_output_budget_bytes",
    "CapabilityDispatcher",
]

#: Capability identifier shape — the registry's slug charset, kept local
#: with the same note as ``mai.policy.evaluate`` (the shape owner is
#: outside this card's allowlist; a divergence is a review finding, not
#: a silent import of a private symbol).
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

#: Upper bound a dispatcher may be constructed with (DoS bound for the
#: receipt, not a transport contract — C04 owns real output budgets).
MAX_OUTPUT_BUDGET_BYTES = 1024 * 1024

#: Construction default: bounded tool output (C06 rule 3) for small
#: model contexts.
default_output_budget_bytes = 64 * 1024

#: The one replacement token for resolved secret material found in
#: executor output (defense-in-depth; C03 rule 6).
REDACTED = "***redacted***"

#: Frozen receipt ``effect`` values: the honest lifecycle of the external
#: boundary. ``unknown`` means "invocation started, outcome unproven".
EFFECT_NOT_ATTEMPTED = "not_attempted"
EFFECT_EXECUTED = "executed"
EFFECT_UNKNOWN = "unknown"


class DispatchErrorCode(StrEnum):
    """Stable machine-readable construction-rejection codes (C01 style)."""

    INVALID_REQUEST = "invalid_request"
    INVALID_WIRING = "invalid_wiring"


class DispatchError(ValueError):
    """One structured, value-free request/wiring rejection.

    Raised only for malformed *inputs* (request shape, canonicalization
    failure, wiring type errors) before any state is read, any approval
    is consumed and any effect is planned. Authorization ambiguity is
    never an exception — it is a deny :class:`DispatchOutcome`.
    """

    def __init__(self, code: DispatchErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class DispatchReason(StrEnum):
    """Stable dispatch-seam denial reasons (distinct from the policy
    :class:`~mai.policy.evaluate.DecisionReason` vocabulary that passes
    through verbatim when the policy gate itself denies)."""

    APPROVAL_REQUIRED_BUT_ABSENT = "approval_required_but_absent"
    APPROVAL_PRESENTED_BUT_NOT_REQUIRED = "approval_presented_but_not_required"
    APPROVAL_REJECTED = "approval_rejected"
    CREDENTIAL_SOURCE_UNAVAILABLE = "credential_source_unavailable"
    CREDENTIAL_RESOLUTION_FAILED = "credential_resolution_failed"
    GRANTS_UNAVAILABLE = "grants_unavailable"
    POLICY_UNAVAILABLE = "policy_unavailable"
    ADOPTION_UNAVAILABLE = "adoption_unavailable"
    EXECUTOR_FAILED = "executor_failed"
    EXECUTOR_CONTRACT_VIOLATION = "executor_contract_violation"


@dataclass(frozen=True, slots=True)
class CapabilityCall:
    """The typed, secrets-carrying input handed to the executor.

    Built by the dispatcher only, after all gates passed. ``credentials``
    holds resolved material keyed by reference name (C03 rule 6: values
    at effect time only); the executor is trusted code (C03 rule 7) and
    the one deliberate disclosure point.
    """

    descriptor: CapabilityDescriptor
    payload: Mapping[str, object]
    context: ExecutionContext
    credentials: Mapping[str, SecretValue]
    op_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ExecutorResult:
    """One honest executor answer: outcome flag plus textual output.

    ``error_code`` is the executor's optional stable machine token for a
    structured refusal (e.g. ``vault_not_allowed``); it is executor-side
    metadata and never overrides the dispatcher's own decision vocabulary.

    ``redact_values`` names raw secret material the executor placed in
    ``output`` (C03 rule 6 defense-in-depth beyond the resolved
    credentials): the dispatcher scrubs every non-empty string before the
    :class:`DispatchOutcome` becomes durable audit surface.
    """

    ok: bool
    output: str
    error_code: str | None = None
    redact_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BoundedOutput:
    """Redacted, size-bounded executor output (C06 rule 3)."""

    text: str
    truncated: bool
    omitted_bytes: int


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """The audit receipt of one dispatch attempt (AC2).

    ``reason`` is a stable string from either the policy vocabulary
    (:class:`~mai.policy.evaluate.DecisionReason`) or the dispatch-seam
    vocabulary (:class:`DispatchReason`). ``effect`` is one of
    ``not_attempted | executed | unknown`` — it never claims more than
    was proven. ``allowed=True`` plus ``effect="executed"`` is the only
    combination asserting an external effect occurred. The payload is
    represented by its canonical SHA-256 hash only.
    """

    op_id: uuid.UUID
    ts: datetime
    capability_id: str
    server_id: uuid.UUID
    run_id: uuid.UUID
    principal: str
    mode: str
    allowed: bool
    reason: str
    policy_revision: int | None
    effect_class: str | None
    payload_hash: str
    effect: str
    approval_id: uuid.UUID | None
    approval_spent: bool
    approval_code: str | None
    executor_ok: bool | None
    output: BoundedOutput | None
    error_type: str | None
    credential_references: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        """Frozen-shape JSON document (stable key order; audit view)."""
        return {
            "op_id": str(self.op_id),
            "ts": self.ts.isoformat(),
            "capability_id": self.capability_id,
            "server_id": str(self.server_id),
            "run_id": str(self.run_id),
            "principal": self.principal,
            "mode": self.mode,
            "allowed": self.allowed,
            "reason": self.reason,
            "policy_revision": self.policy_revision,
            "effect_class": self.effect_class,
            "payload_hash": self.payload_hash,
            "effect": self.effect,
            "approval_id": None
            if self.approval_id is None
            else str(self.approval_id),
            "approval_spent": self.approval_spent,
            "approval_code": self.approval_code,
            "executor_ok": self.executor_ok,
            "output": None
            if self.output is None
            else {
                "text": self.output.text,
                "truncated": self.output.truncated,
                "omitted_bytes": self.output.omitted_bytes,
            },
            "error_type": self.error_type,
            "credential_references": list(self.credential_references),
        }

    def __repr__(self) -> str:
        return (
            f"<DispatchOutcome op={self.op_id} {self.capability_id}"
            f"@{self.server_id} {self.reason} effect={self.effect}>"
        )


# ---------------------------------------------------------------------------
# Provider types (host-composed current state)
# ---------------------------------------------------------------------------

#: Current scoped grants (C03 rule 3: rechecked per call, never cached).
type GrantsProvider = Callable[[], ScopedGrants]
#: Current policy (revision binding for approvals, C03 rule 4).
type PolicyProvider = Callable[[], Policy]
#: Current adoption verdict for one server (G2 step 1).
type AdoptionProbe = Callable[[uuid.UUID], bool]
#: Current ``.env`` credential source (constructed per call by the host).
type CredentialSource = Callable[[], EnvResolver]
#: The trusted capability implementation (C03 rule 7).
type Executor = Callable[[CapabilityCall], Awaitable[ExecutorResult]]


def canonical_payload_json(payload: object) -> str:
    """Deterministic JSON text for one operation payload.

    ``sort_keys`` gives the canonical byte form the approval binds
    (identical content in any key order binds identically);
    ``allow_nan=False`` refuses NaN/Infinity (not JSON);
    ``ensure_ascii`` keeps the hash byte-stable across locales. Hosts
    creating approvals call :func:`canonical_payload_hash` with the same
    payload the dispatcher will present.
    """
    if not isinstance(payload, Mapping):
        raise DispatchError(
            DispatchErrorCode.INVALID_REQUEST,
            "payload must be a JSON object (mapping)",
        )
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise DispatchError(
            DispatchErrorCode.INVALID_REQUEST,
            f"payload is not JSON-canonicalizable ({type(error).__name__})",
        ) from None


def canonical_payload_hash(payload: object) -> str:
    """The canonical SHA-256 hex digest binding one operation payload
    into an approval (C03 rule 4); the store API never sees payload
    bytes, only this digest."""
    return hashlib.sha256(canonical_payload_json(payload).encode("utf-8")).hexdigest()


def _secret_output_forms(secrets: list[str]) -> list[str]:
    """Every serialized form one secret value can take in output text.

    A value may appear raw, or inside a JSON-serialized string where
    ``json.dumps`` escapes ``"``, ``\\`` and control characters — and,
    with ``ensure_ascii=True`` (the default for durable JSON documents),
    every non-ASCII codepoint as ``\\uXXXX``. Each escape mode yields
    different bytes, so scrubbing must try them all or a JSON-encoded
    copy of the secret survives redaction (C2).
    """
    forms: set[str] = set()
    for value in secrets:
        if not value:
            continue
        forms.add(value)
        forms.add(json.dumps(value, ensure_ascii=True)[1:-1])
        forms.add(json.dumps(value, ensure_ascii=False)[1:-1])
    # Longest first: an escaped form can only contain its raw form when
    # it IS the raw form (no escapable characters), and replacing the
    # longest match first never strands a partial secret.
    return sorted(forms, key=len, reverse=True)


def _redact(text: str, secrets: list[str]) -> str:
    """Replace every resolved secret value in ``text`` (defense-in-depth).

    Runs *before* bounding so a budget cut can never split a secret
    around the marker. Raw and JSON-escaped forms both scrub: executor
    output is frequently ``json.dumps`` text, and replacing only the raw
    value would leave the ``\\n``/``\\uXXXX``-escaped copy intact.
    """
    for value in _secret_output_forms(secrets):
        text = text.replace(value, REDACTED)
    return text


def _bound(text: str, budget_bytes: int) -> BoundedOutput:
    """Bound UTF-8 text to ``budget_bytes`` with an explicit notice."""
    raw = text.encode("utf-8")
    if len(raw) <= budget_bytes:
        return BoundedOutput(text=text, truncated=False, omitted_bytes=0)
    kept = raw[:budget_bytes].decode("utf-8", errors="ignore")
    return BoundedOutput(
        text=kept,
        truncated=True,
        omitted_bytes=len(raw) - len(kept.encode("utf-8")),
    )


class CapabilityDispatcher:
    """The enforced boundary: policy → approval → credentials → effect.

    Composition is constructor-checked (fail-closed wiring: a dispatcher
    missing any component refuses to exist). Providers are called twice
    per dispatch — once for the gate, once immediately before the
    effect — so callers plug *current* state sources (e.g.
    ``GrantConfigStore.read()``, which re-resolves files per call); the
    dispatcher caches nothing.

    The dispatcher performs no I/O of its own beyond the approval store
    and the credential resolver the host handed it.
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        approvals: ApprovalStore,
        grants: GrantsProvider,
        policy: PolicyProvider,
        adopted: AdoptionProbe,
        credentials: CredentialSource,
        executor: Executor,
        output_budget_bytes: int = default_output_budget_bytes,
    ) -> None:
        if not isinstance(registry, CapabilityRegistry):
            raise DispatchError(
                DispatchErrorCode.INVALID_WIRING,
                "registry must be a CapabilityRegistry",
            )
        if not isinstance(approvals, ApprovalStore):
            raise DispatchError(
                DispatchErrorCode.INVALID_WIRING,
                "approvals must be an ApprovalStore",
            )
        for name, provider, want in (
            ("grants", grants, "callable returning ScopedGrants"),
            ("policy", policy, "callable returning Policy"),
            ("adopted", adopted, "callable(server_id) -> bool"),
            ("credentials", credentials, "callable returning EnvResolver"),
            ("executor", executor, "async callable(CapabilityCall)"),
        ):
            if not callable(provider):
                raise DispatchError(
                    DispatchErrorCode.INVALID_WIRING,
                    f"{name} must be a {want}",
                )
        if (
            isinstance(output_budget_bytes, bool)
            or not isinstance(output_budget_bytes, int)
            or not 1 <= output_budget_bytes <= MAX_OUTPUT_BUDGET_BYTES
        ):
            raise DispatchError(
                DispatchErrorCode.INVALID_WIRING,
                f"output_budget_bytes must be an int in "
                f"[1, {MAX_OUTPUT_BUDGET_BYTES}]",
            )
        self._registry = registry
        self._approvals = approvals
        self._grants = grants
        self._policy = policy
        self._adopted = adopted
        self._credentials = credentials
        self._executor = executor
        self._budget = output_budget_bytes

    def __repr__(self) -> str:
        return (
            f"<CapabilityDispatcher descriptors={len(self._registry)} "
            f"budget={self._budget}>"
        )

    # -- descriptor resolution ---------------------------------------------

    def _descriptor_for(self, capability_id: str) -> CapabilityDescriptor | None:
        """Newest registered version of one capability (semver order).

        The registry is version-keyed; the dispatcher executes the
        newest registered version. Version pinning is a host-composition
        concern, not a dispatcher knob. No registered version ⇒
        ``None`` ⇒ :func:`evaluate` denies ``capability_not_registered``
        (a stale grant cannot resurrect a revoked capability).
        """
        versions = self._registry.versions(capability_id)
        if not versions:
            return None
        return self._registry.get(capability_id, versions[-1])

    # -- the boundary --------------------------------------------------------

    async def dispatch(
        self,
        *,
        context: ExecutionContext,
        capability_id: str,
        payload: Mapping[str, object],
        approval_id: uuid.UUID | None = None,
    ) -> DispatchOutcome:
        """Apply one capability once under every gate; return the receipt.

        Raises :class:`DispatchError` only for malformed requests (before
        any state read, consumption or effect). Every *decision* answer —
        allow, policy deny, seam denial, effect — is a
        :class:`DispatchOutcome`. Infrastructure failures from the
        approval store (database outage) propagate as exceptions after
        the gate: nothing has been consumed and no effect is planned.
        """
        op_id = uuid.uuid4()
        ts = datetime.now(timezone.utc)
        if not isinstance(capability_id, str) or not _IDENTIFIER_RE.fullmatch(
            capability_id
        ):
            raise DispatchError(
                DispatchErrorCode.INVALID_REQUEST,
                "capability_id must match "
                f"{_IDENTIFIER_RE.pattern} (C03 rule 1 slug shape)",
            )
        if approval_id is not None and not isinstance(approval_id, uuid.UUID):
            raise DispatchError(
                DispatchErrorCode.INVALID_REQUEST,
                "approval_id must be a uuid.UUID or None (C01 rule 1)",
            )
        canonical = canonical_payload_json(payload)
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        def outcome(
            *,
            allowed: bool,
            reason: str,
            effect: str = EFFECT_NOT_ATTEMPTED,
            policy_revision: int | None = None,
            effect_class: str | None = None,
            approval_spent: bool = False,
            consumed_approval: uuid.UUID | None = None,
            approval_code: str | None = None,
            executor_ok: bool | None = None,
            output: BoundedOutput | None = None,
            error_type: str | None = None,
        ) -> DispatchOutcome:
            return DispatchOutcome(
                op_id=op_id,
                ts=ts,
                capability_id=capability_id,
                server_id=context.server_id,
                run_id=context.run_id,
                principal=context.principal,
                mode=context.mode.value,
                allowed=allowed,
                reason=reason,
                policy_revision=policy_revision,
                effect_class=effect_class,
                payload_hash=payload_hash,
                effect=effect,
                approval_id=consumed_approval,
                approval_spent=approval_spent,
                approval_code=approval_code,
                executor_ok=executor_ok,
                output=output,
                error_type=error_type,
                credential_references=context.credential_references,
            )

        # -- 2. policy gate on current state --------------------------------
        try:
            grants = self._grants()
        except Exception:
            return outcome(allowed=False, reason=DispatchReason.GRANTS_UNAVAILABLE)
        try:
            policy = self._policy()
        except Exception:
            return outcome(allowed=False, reason=DispatchReason.POLICY_UNAVAILABLE)
        try:
            adopted = self._adopted(context.server_id)
        except Exception:
            return outcome(allowed=False, reason=DispatchReason.ADOPTION_UNAVAILABLE)
        descriptor = self._descriptor_for(capability_id)
        decision = evaluate(
            context=context,
            capability_id=capability_id,
            descriptor=descriptor,
            grants=grants,
            policy=policy,
            adopted=adopted,
        )
        if not decision.allowed:
            return outcome(
                allowed=False,
                reason=decision.reason.value,
                policy_revision=decision.policy_revision,
                effect_class=decision.effect_class,
            )

        # -- 3. approval seam (C03 rule 4) ----------------------------------
        approval_spent = False
        consumed: uuid.UUID | None = None
        if decision.requires_approval:
            if approval_id is None:
                return outcome(
                    allowed=False,
                    reason=DispatchReason.APPROVAL_REQUIRED_BUT_ABSENT,
                    policy_revision=decision.policy_revision,
                    effect_class=decision.effect_class,
                )
            try:
                receipt = await self._approvals.consume(
                    approval_id,
                    capability_id=capability_id,
                    server_id=context.server_id,
                    payload_hash=payload_hash,
                    policy_revision=decision.policy_revision,
                    consumed_by=context.principal,
                )
            except ApprovalError as error:
                return outcome(
                    allowed=False,
                    reason=DispatchReason.APPROVAL_REJECTED,
                    policy_revision=decision.policy_revision,
                    effect_class=decision.effect_class,
                    approval_code=error.code.value,
                )
            approval_spent = True
            consumed = receipt.approval_id
        elif approval_id is not None:
            return outcome(
                allowed=False,
                reason=DispatchReason.APPROVAL_PRESENTED_BUT_NOT_REQUIRED,
                policy_revision=decision.policy_revision,
                effect_class=decision.effect_class,
            )

        # -- 4. credential seam (C03 rule 6, at effect time) ----------------
        credentials: dict[str, SecretValue] = {}
        if context.credential_references:
            try:
                resolver = self._credentials()
            except Exception:
                return outcome(
                    allowed=False,
                    reason=DispatchReason.CREDENTIAL_SOURCE_UNAVAILABLE,
                    policy_revision=decision.policy_revision,
                    effect_class=decision.effect_class,
                    approval_spent=approval_spent,
                    consumed_approval=consumed,
                )
            for reference in context.credential_references:
                try:
                    credentials[reference] = resolver.resolve(reference)
                except EnvFileError as error:
                    return outcome(
                        allowed=False,
                        reason=DispatchReason.CREDENTIAL_RESOLUTION_FAILED,
                        policy_revision=decision.policy_revision,
                        effect_class=decision.effect_class,
                        approval_spent=approval_spent,
                        consumed_approval=consumed,
                        approval_code=error.code.value,
                    )

        # -- 5. immediate recheck before the effect (C03 rule 3, G2 step 4) --
        try:
            grants_now = self._grants()
        except Exception:
            return outcome(
                allowed=False,
                reason=DispatchReason.GRANTS_UNAVAILABLE,
                policy_revision=decision.policy_revision,
                effect_class=decision.effect_class,
                approval_spent=approval_spent,
                consumed_approval=consumed,
            )
        try:
            policy_now = self._policy()
        except Exception:
            return outcome(
                allowed=False,
                reason=DispatchReason.POLICY_UNAVAILABLE,
                policy_revision=decision.policy_revision,
                effect_class=decision.effect_class,
                approval_spent=approval_spent,
                consumed_approval=consumed,
            )
        try:
            adopted_now = self._adopted(context.server_id)
        except Exception:
            return outcome(
                allowed=False,
                reason=DispatchReason.ADOPTION_UNAVAILABLE,
                policy_revision=decision.policy_revision,
                effect_class=decision.effect_class,
                approval_spent=approval_spent,
                consumed_approval=consumed,
            )
        descriptor_now = self._descriptor_for(capability_id)
        recheck = evaluate(
            context=context,
            capability_id=capability_id,
            descriptor=descriptor_now,
            grants=grants_now,
            policy=policy_now,
            adopted=adopted_now,
        )
        if not recheck.allowed:
            # Final verdict reason stands on the receipt; a spent approval
            # is reported honestly via approval_spent.
            return outcome(
                allowed=False,
                reason=recheck.reason.value,
                policy_revision=recheck.policy_revision,
                effect_class=recheck.effect_class,
                approval_spent=approval_spent,
                consumed_approval=consumed,
            )
        if recheck.requires_approval and not approval_spent:
            return outcome(
                allowed=False,
                reason=DispatchReason.APPROVAL_REQUIRED_BUT_ABSENT,
                policy_revision=recheck.policy_revision,
                effect_class=recheck.effect_class,
            )

        # -- 6. effect -------------------------------------------------------
        call = CapabilityCall(
            descriptor=descriptor_now if descriptor_now is not None else descriptor,
            payload=dict(payload),
            context=context,
            credentials=credentials,
            op_id=op_id,
        )
        try:
            result = await self._executor(call)
        except Exception as exc:
            # The invocation started: the effect is unproven, never
            # "executed". Exception *type name* only — messages can quote
            # secret material.
            return outcome(
                allowed=False,
                reason=DispatchReason.EXECUTOR_FAILED,
                effect=EFFECT_UNKNOWN,
                policy_revision=recheck.policy_revision,
                effect_class=recheck.effect_class,
                approval_spent=approval_spent,
                consumed_approval=consumed,
                error_type=type(exc).__name__,
            )
        if (
            not isinstance(result, ExecutorResult)
            or not isinstance(result.ok, bool)
            or not isinstance(result.output, str)
        ):
            return outcome(
                allowed=False,
                reason=DispatchReason.EXECUTOR_CONTRACT_VIOLATION,
                effect=EFFECT_UNKNOWN,
                policy_revision=recheck.policy_revision,
                effect_class=recheck.effect_class,
                approval_spent=approval_spent,
                consumed_approval=consumed,
            )

        # -- 7. output boundary: redact first, then bound --------------------
        # Executor-declared secret material (C2) scrubs alongside the
        # resolved credential values; non-string/empty entries are the
        # executor's bug and are dropped, never matched.
        secrets = [secret.value for secret in credentials.values()]
        secrets.extend(
            s for s in result.redact_values if isinstance(s, str) and s
        )
        bounded = _bound(
            _redact(result.output, secrets),
            self._budget,
        )
        return outcome(
            allowed=True,
            reason=DecisionReason.ALLOWED.value,
            effect=EFFECT_EXECUTED,
            policy_revision=recheck.policy_revision,
            effect_class=recheck.effect_class,
            approval_spent=approval_spent,
            consumed_approval=consumed,
            executor_ok=result.ok,
            output=bounded,
        )
