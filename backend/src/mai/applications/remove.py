"""Durable, approval-gated, resource-locked application removal with
explicit two-tier persistent-data semantics.

Card ``08-remove``; consumer of the same integrated P08 producers as
``mai.applications.install``/``update`` (``recipes``, ``adopt``, the P03
gate, the P04 bounded command lane, ``mai.jobs.resource_locks``).
Contracts.md:

- **C09 rule 5 (two-tier data semantics)** — removal is split into two
  SEPARATELY AUTHORIZED operations that can never collapse into one:
  *Tier 1* :meth:`RemoveService.remove` stops and removes the application's
  containers (``docker compose down`` WITHOUT any volume flag) and
  tombstone/purges the canonical manifest + projection. It can NEVER
  delete persistent data: no code path here issues a data deletion, and
  :class:`RemoveRequest` requires the explicit ``data_intent``
  (``keep`` | ``delete``) at construction — an absent/inferred flag is
  unrepresentable, and a ``delete`` intent is only RECORDED (the receipt
  says the data survived). *Tier 2* :meth:`RemoveService.purge_data`
  deletes ONLY the plan's persistent-data locations, and only when the
  request literally carries ``delete_data=True``, the recipe's declared
  ``removal.data_policy`` is ``explicit_choice`` (a ``keep`` policy recipe
  refuses, always), the deployment is VERIFIED absent first, and a
  one-use approval bound to the exact deletion manifest is consumed. The
  manifest (:meth:`RemoveService.deletion_manifest`) is the bound
  deletion preview: the exact planner-allocated host paths, hashed, so
  any drift between preview and effect is the C03 rule 4 altered-payload
  denial. Absent data locations are never invented: a recipe without
  volumes refuses (``no_data_locations``).
- **C09 rule 1/6 (identity + adopted resources only)** — a canonical
  record for this ``application_id`` under another server refuses
  (``cross_server_conflict``); a record binding the identity to a
  different Compose project refuses (``external_record_conflict``).
  Removal verifies the deployed Compose bytes against the rebuilt plan
  BEFORE any effect (``state_diverged``) — the executor tears down and
  purges only what the bound plan describes, never a neighboring
  project or volume.
- **C03 rules 3–5 (gate)** — full policy decision taken and RE-checked
  immediately before the privileged effect; one-use approvals consumed
  atomically bound to ``(capability_id, server_id, payload_hash,
  policy_revision)`` with ``app.remove`` binding the plan hash and
  ``app.remove-data`` binding the deletion-manifest hash. Any denial
  returns ``refused`` with ``effect="not_attempted"`` and consumes
  nothing.
- **C05 rule 6 / LOCK-FENCE** — both operations run under the SAME
  exclusive ``application:<application_id>`` lease namespace install and
  update use, so remove/purge/install/update on one application exclude
  each other; a lapsed lease aborts the operation (``outcome_unknown``)
  and is left to lapse into a fence.
- **C04 rules 5–7 (honest outcomes)** — a timeout cut or transport death
  is ``outcome_unknown``: never retried silently. Teardown verification
  mirrors the verified-rollback read (``compose ps -q`` empty with a
  known exit); purge verification re-reads every location
  (``ls -A`` absent) — "deleted" is claimed only when observed.
- **C02 rule 5 / W4 (tombstone/purge canonical record)** — after
  VERIFIED container removal the canonical manifest + projection are
  revoked through :meth:`~mai.applications.adopt.ApplicationAdoption.revoke`
  (durable delete intent → file tombstone → projection reconcile) with a
  deterministic ``remove:`` intent id derived from the plan hash, so an
  interrupted canonical deletion converges on retry instead of
  resurrecting or duplicating. A tombstone refusal after verified
  teardown is recorded honestly (note ``manifest tombstone refused:
  …``) — the containers stay down; the retry re-converges the record.

Pure stdlib + integrated sibling modules; no engine registration here:
composition is the caller's wiring, exactly like ``install.py``. The
pure helpers with subtle behavior (transport, base64 write channel,
compose argv, lease heartbeat, vocabulary guard) are imported from the
VERIFIED ``mai.applications.install`` surface — one implementation of
each, not a fork.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from mai.applications.adopt import (
    ABSENT_REVISION,
    AdoptionError,
    AdoptionErrorCode,
    ApplicationAdoption,
    ApplicationManifestStore,
    parse_manifest_text,
)
from mai.applications.install import (
    _ACTION_APPLIED,
    _ACTION_VERIFIED,
    _DOWN_TIMEOUT_SECONDS,
    _LeaseHeartbeat,
    _OUTPUT_BUDGET_BYTES,
    _PREFLIGHT_TIMEOUT_SECONDS,
    _RECOVERABLE_ADOPTION_CODES,
    _compose_argv,
    AppliedOperation,
    DEFAULT_LEASE_TTL_SECONDS,
    EFFECT_EXECUTED,
    EFFECT_NOT_ATTEMPTED,
    EFFECT_UNKNOWN,
    LOCK_RESOURCE_PREFIX,
    STATUS_FAILED,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_REFUSED,
    STATUS_SUCCEEDED,
    Transport,
    ssh_transport,
)
from mai.applications.recipes import (
    ApplicationRecipe,
    DeploymentPlan,
    TargetEnvironment,
    build_deployment_plan,
)
from mai.execution.commands import (
    CommandOutcome,
    CommandRequest,
    CommandRequestError,
    CommandStatus,
)
from mai.jobs.resource_locks import (
    LockLease,
    ResourceLockError,
    ResourceLockService,
)
from mai.plugins.registry import CapabilityDescriptor
from mai.policy.approvals import ApprovalError, ApprovalStore
from mai.policy.evaluate import (
    ExecutionContext,
    Policy,
    PolicyDecision,
    ScopedGrants,
    evaluate,
)

__all__ = [
    "CAPABILITY_ID",
    "DEFAULT_LEASE_TTL_SECONDS",
    "LOCK_RESOURCE_PREFIX",
    "PURGE_CAPABILITY_ID",
    "RemoveDataIntent",
    "RemoveError",
    "RemoveErrorCode",
    "RemoveIntentNamespace",
    "RemoveRequest",
    "RemoveResult",
    "RemoveService",
    "PurgeDataRequest",
    "PurgeDataResult",
    "DeletionManifest",
    "purge_deletion_manifest",
    "purge_plan",
    "remove_intent_id",
    "ssh_transport",
]

#: Tier-1 capability slug: stop/remove containers + canonical tombstone.
#: Grants name this id; the descriptor wired into the service must carry it.
CAPABILITY_ID: str = "app.remove"

#: Tier-2 capability slug: delete the declared persistent-data locations.
#: A SEPARATE capability (and approval binding) — container removal never
#: implies data deletion (C09 rule 5).
PURGE_CAPABILITY_ID: str = "app.remove-data"

#: Deterministic canonical-delete intent namespace (distinct from the
#: install/update namespaces: a removal's tombstone is a different intent
#: even when plan hashes coincide across cards).
RemoveIntentNamespace = uuid.uuid5(
    uuid.NAMESPACE_URL, "mai:remove:intent-namespace:v1"
)


class RemoveErrorCode(StrEnum):
    """Stable machine-readable refusal/failure codes (C01 vocabulary)."""

    INVALID_REQUEST = "invalid_request"
    INVALID_PLAN = "invalid_plan"
    POLICY_DENIED = "policy_denied"
    APPROVAL_REJECTED = "approval_rejected"
    LOCK_UNAVAILABLE = "lock_unavailable"
    EXTERNAL_RECORD_CONFLICT = "external_record_conflict"
    CROSS_SERVER_CONFLICT = "cross_server_conflict"
    STATE_DIVERGED = "state_diverged"
    NOT_INSTALLED = "not_installed"
    #: purge-data without the literal ``delete_data=True`` intent.
    DATA_INTENT_REQUIRED = "data_intent_required"
    #: purge-data on a recipe whose declared policy keeps persistent data.
    DATA_POLICY_KEEP = "data_policy_keep"
    #: purge-data while the deployment is verified still present.
    APP_STILL_DEPLOYED = "app_still_deployed"
    #: purge-data on a recipe that declares no persistent-data locations.
    NO_DATA_LOCATIONS = "no_data_locations"
    APPLY_FAILED = "apply_failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class RemoveError(ValueError):
    """Structured, value-free wiring/input rejection (raised, not returned).

    Only for malformed constructor wiring or request fields — the same
    boundary :class:`~mai.applications.install.InstallService` draws.
    Gate denials and execution outcomes are returned as result values,
    never raised.
    """

    def __init__(self, code: RemoveErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class RemoveDataIntent(StrEnum):
    """The EXPLICIT keep-or-delete decision C09 rule 5 requires.

    A member of this enum must be constructed deliberately: there is no
    default, so an absent/inferred flag can never stand in for ``delete``.
    ``DELETE`` on the tier-1 removal is a RECORDED intent only — the data
    survives until the separately authorized tier-2 purge.
    """

    KEEP = "keep"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class RemoveRequest:
    """One validated removal intent (identity + deployed recipe + gate).

    ``recipe`` is the deployed validated recipe — the executor rebuilds
    the deterministic plan from it (compose path/project, plan hash,
    volume locations) and verifies the deployed Compose bytes against it
    before any effect. ``context.server_id`` MUST equal ``server_id``
    (C05 rule 3). ``approval_id`` is the pending one-use decision bound
    to the plan hash this service rebuilds (capability ``app.remove``).
    ``data_intent`` is the explicit C09 rule 5 decision; it is recorded
    on the receipt and NEVER triggers deletion by itself.
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    recipe: ApplicationRecipe
    target: TargetEnvironment
    context: ExecutionContext
    approval_id: uuid.UUID
    data_intent: RemoveDataIntent

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, uuid.UUID):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST, "server_id must be a uuid.UUID"
            )
        if not isinstance(self.application_id, uuid.UUID):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "application_id must be a uuid.UUID",
            )
        if not isinstance(self.recipe, ApplicationRecipe):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "recipe must be a validated ApplicationRecipe (parse_recipe_text)",
            )
        if not isinstance(self.target, TargetEnvironment):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "target must be a TargetEnvironment",
            )
        if not isinstance(self.context, ExecutionContext):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "context must be an ExecutionContext",
            )
        if self.context.server_id != self.server_id:
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "context.server_id does not match the request target "
                "(C05 rule 3 run binding)",
            )
        if not isinstance(self.approval_id, uuid.UUID):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST, "approval_id must be a uuid.UUID"
            )
        if not isinstance(self.data_intent, RemoveDataIntent):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "data_intent must be an explicit RemoveDataIntent member "
                "(no inferred absent flag may stand for delete; C09 rule 5)",
            )


@dataclass(frozen=True, slots=True)
class RemoveResult:
    """The audit receipt of one tier-1 removal attempt.

    ``data_intent`` is the operator's declared decision; ``data_deleted``
    is ALWAYS False — persistent data is unrepresentable in tier 1 (the
    field exists so the invariant is explicit and testable, not inferred
    from an absent field). ``effect`` never overclaims: ``refused`` pairs
    with ``not_attempted``; ``outcome_unknown`` with ``unknown``; only a
    VERIFIED container removal claims ``executed``.
    ``manifest_removed``/``manifest_revision`` report the canonical
    tombstone (C02 rule 5) — a teardown with a refused tombstone is still
    a teardown; the note records the refusal.
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    plan_sha256: str
    status: str
    effect: str
    reason: str | None
    data_intent: str
    operations: tuple[AppliedOperation, ...] = ()
    notes: tuple[str, ...] = ()
    approval_id: uuid.UUID | None = None
    manifest_removed: bool = False
    manifest_revision: int | None = None
    data_deleted: bool = False
    lock_released: bool = False


def remove_intent_id(
    server_id: uuid.UUID, application_id: uuid.UUID, plan_sha256: str
) -> uuid.UUID:
    """Deterministic canonical-delete intent id (resume-safe, C02 rule 5)."""
    return uuid.uuid5(
        RemoveIntentNamespace,
        f"remove:{server_id}:{application_id}:{plan_sha256}",
    )


@dataclass(frozen=True, slots=True)
class DeletionManifest:
    """The bound deletion preview (acceptance 1).

    ``locations`` are the exact planner-allocated host paths tier 2 will
    delete — nothing else, ever. ``manifest_sha256`` hashes the canonical
    manifest document; the tier-2 approval binds THIS hash, so any change
    of recipe revision, identity or location set between preview and
    effect is the altered-payload denial (C03 rule 4).
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    plan_sha256: str
    locations: tuple[str, ...]
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class PurgeDataRequest:
    """One validated tier-2 data-deletion intent.

    ``delete_data`` must be the literal ``True`` — it has NO default, so
    the deletion intent cannot be inferred from an absent flag (C09 rule
    5); ``False`` is a gate refusal, not a wiring error. ``approval_id``
    is the pending one-use decision bound to the deletion-manifest hash
    (capability ``app.remove-data``) — the same approval consumed only
    after the deployment is verified absent.
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    recipe: ApplicationRecipe
    target: TargetEnvironment
    context: ExecutionContext
    approval_id: uuid.UUID
    delete_data: bool

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, uuid.UUID):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST, "server_id must be a uuid.UUID"
            )
        if not isinstance(self.application_id, uuid.UUID):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "application_id must be a uuid.UUID",
            )
        if not isinstance(self.recipe, ApplicationRecipe):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "recipe must be a validated ApplicationRecipe (parse_recipe_text)",
            )
        if not isinstance(self.target, TargetEnvironment):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "target must be a TargetEnvironment",
            )
        if not isinstance(self.context, ExecutionContext):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "context must be an ExecutionContext",
            )
        if self.context.server_id != self.server_id:
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "context.server_id does not match the request target "
                "(C05 rule 3 run binding)",
            )
        if not isinstance(self.approval_id, uuid.UUID):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST, "approval_id must be a uuid.UUID"
            )
        if not isinstance(self.delete_data, bool):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "delete_data must be a bool",
            )


@dataclass(frozen=True, slots=True)
class PurgeDataResult:
    """The audit receipt of one tier-2 data-deletion attempt.

    ``locations`` is the deletion manifest's location list (value-free
    planner paths); ``verified`` is True only when EVERY location was
    observed absent after the effect. Already-absent locations are
    recorded as verified no-ops (idempotent retry converges).
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    manifest_sha256: str
    status: str
    effect: str
    reason: str | None
    locations: tuple[str, ...] = ()
    operations: tuple[AppliedOperation, ...] = ()
    notes: tuple[str, ...] = ()
    approval_id: uuid.UUID | None = None
    verified: bool = False
    lock_released: bool = False


def _validate_plan_vocabulary(plan: DeploymentPlan) -> None:
    """The plan is install-vocabulary; an unknown op means the recipe
    schema evolved beyond this card's understanding — refuse, never guess."""
    from mai.applications.recipes import (
        OP_CREATE_DIRECTORIES,
        OP_PULL_IMAGE,
        OP_REQUIRE_BACKUP,
        OP_START,
        OP_VERIFY_PREREQUISITES,
        OP_WAIT_HEALTHY,
        OP_WRITE_COMPOSE,
        OP_WRITE_CREDENTIALS,
    )

    known = {
        OP_VERIFY_PREREQUISITES,
        OP_REQUIRE_BACKUP,
        OP_CREATE_DIRECTORIES,
        OP_WRITE_COMPOSE,
        OP_WRITE_CREDENTIALS,
        OP_PULL_IMAGE,
        OP_START,
        OP_WAIT_HEALTHY,
    }
    for op in plan.operations:
        if op.op not in known:
            raise RemoveError(
                RemoveErrorCode.INVALID_PLAN,
                "plan carries an operation outside the frozen vocabulary",
            )


def purge_plan(request: PurgeDataRequest) -> DeploymentPlan:
    """Rebuild the deterministic plan a purge is bound to (pure)."""
    plan = build_deployment_plan(
        request.recipe,
        request.server_id,
        request.application_id,
        request.target,
    )
    _validate_plan_vocabulary(plan)
    return plan


def purge_deletion_manifest(
    request: PurgeDataRequest,
) -> tuple[DeploymentPlan, DeletionManifest]:
    """The plan plus its bound deletion manifest (one build, pure)."""
    plan = purge_plan(request)
    locations = tuple(sorted(v.host_path for v in plan.volumes))
    payload = json.dumps(
        {
            "v": 1,
            "server_id": str(plan.server_id),
            "application_id": str(plan.application_id),
            "plan_sha256": plan.plan_sha256,
            "locations": list(locations),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    manifest = DeletionManifest(
        server_id=plan.server_id,
        application_id=plan.application_id,
        plan_sha256=plan.plan_sha256,
        locations=locations,
        manifest_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )
    return plan, manifest


@dataclass(frozen=True, slots=True)
class _TargetState:
    """Verified target classification (all values read, never assumed)."""

    compose_present: bool
    compose_hash_matches: bool
    project_running: bool


class RemoveService:
    """The enforced removal boundary: identity → plan → policy → approval
    → lock → verified teardown → canonical tombstone; and the separately
    authorized tier-2 data purge under the same gates.

    Wiring is constructor-checked and fail-closed (dispatch pattern). The
    grants/policy/adopted providers are called per attempt — current state
    is re-read, never cached (C03 rule 3).
    """

    def __init__(
        self,
        *,
        transport: Transport,
        locks: ResourceLockService,
        approvals: ApprovalStore,
        adoption: ApplicationAdoption,
        store: ApplicationManifestStore,
        grants: Callable[[], ScopedGrants],
        policy: Callable[[], Policy],
        adopted: Callable[[uuid.UUID], bool],
        descriptor: CapabilityDescriptor,
        purge_descriptor: CapabilityDescriptor,
        lease_ttl: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        if not callable(transport):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST, "transport must be callable"
            )
        if not isinstance(locks, ResourceLockService):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "locks must be a ResourceLockService",
            )
        if not isinstance(approvals, ApprovalStore):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "approvals must be an ApprovalStore",
            )
        if not isinstance(adoption, ApplicationAdoption):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "adoption must be an ApplicationAdoption",
            )
        if not isinstance(store, ApplicationManifestStore):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "store must be an ApplicationManifestStore",
            )
        for name, provider in (
            ("grants", grants),
            ("policy", policy),
            ("adopted", adopted),
        ):
            if not callable(provider):
                raise RemoveError(
                    RemoveErrorCode.INVALID_REQUEST, f"{name} must be callable"
                )
        if not isinstance(descriptor, CapabilityDescriptor):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "descriptor must be a CapabilityDescriptor",
            )
        if descriptor.identifier != CAPABILITY_ID:
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "descriptor identifier must be app.remove",
            )
        if descriptor.effect_class != "privileged":
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "remove is a privileged effect; the descriptor must declare "
                "the privileged effect class",
            )
        if not isinstance(purge_descriptor, CapabilityDescriptor):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "purge_descriptor must be a CapabilityDescriptor",
            )
        if purge_descriptor.identifier != PURGE_CAPABILITY_ID:
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "purge_descriptor identifier must be app.remove-data",
            )
        if purge_descriptor.effect_class != "privileged":
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST,
                "data deletion is a privileged effect; the purge descriptor "
                "must declare the privileged effect class",
            )
        if isinstance(lease_ttl, bool) or not isinstance(lease_ttl, (int, float)):
            raise RemoveError(
                RemoveErrorCode.INVALID_REQUEST, "lease_ttl must be a number"
            )
        self._transport = transport
        self._locks = locks
        self._approvals = approvals
        self._adoption = adoption
        self._store = store
        self._grants = grants
        self._policy = policy
        self._adopted = adopted
        self._descriptor = descriptor
        self._purge_descriptor = purge_descriptor
        self._lease_ttl = float(lease_ttl)

    # -- deletion preview (pure; bound approval payload) ----------------------

    def deletion_manifest(self, request: PurgeDataRequest) -> DeletionManifest:
        """The bound deletion preview: exact locations, hashed.

        Pure (no target contact, no gate, no approval consumption): the
        operator previews EXACTLY what tier 2 would delete and creates the
        approval against ``manifest_sha256``; :meth:`purge_data` re-derives
        the manifest and consumes the approval bound to it, so preview and
        effect can never diverge silently (acceptance 1).
        """
        return purge_deletion_manifest(request)[1]

    # -- tier 1: removal -------------------------------------------------------

    async def remove(self, request: RemoveRequest) -> RemoveResult:
        """Stop/remove the application's containers and tombstone the
        canonical record. NEVER deletes persistent data (module doc)."""
        plan = self._plan(request)
        refusal = await self._prelock_guards(request, plan)
        if refusal is not None:
            return refusal

        try:
            lease = self._locks.acquire(
                str(request.server_id),
                f"{LOCK_RESOURCE_PREFIX}{request.application_id}",
                owner_ref=f"remove:{request.context.run_id}",
                ttl=self._lease_ttl,
            )
        except ResourceLockError as error:
            return self._refused(
                request,
                plan,
                RemoveErrorCode.LOCK_UNAVAILABLE,
                reason=(
                    f"{RemoveErrorCode.LOCK_UNAVAILABLE.value}:{error.code.value}"
                ),
            )
        return await self._gated_remove(request, plan, lease)

    # -- tier 2: separately authorized data deletion ---------------------------

    async def purge_data(self, request: PurgeDataRequest) -> PurgeDataResult:
        """Delete the manifest's persistent-data locations — and nothing
        else — under the tier-2 gate (module doc)."""
        plan, manifest = purge_deletion_manifest(request)
        notes: list[str] = []
        if not manifest.locations:
            return self._purge_refused(
                request, manifest, RemoveErrorCode.NO_DATA_LOCATIONS
            )
        if request.recipe.removal.data_policy.value == "keep":
            # The DECLARED policy keeps persistent data: deletion is
            # unrepresentable for this recipe (C09 rule 5), intent or not.
            return self._purge_refused(
                request, manifest, RemoveErrorCode.DATA_POLICY_KEEP
            )
        if request.delete_data is not True:
            return self._purge_refused(
                request, manifest, RemoveErrorCode.DATA_INTENT_REQUIRED
            )

        try:
            lease = self._locks.acquire(
                str(request.server_id),
                f"{LOCK_RESOURCE_PREFIX}{request.application_id}",
                owner_ref=f"purge:{request.context.run_id}",
                ttl=self._lease_ttl,
            )
        except ResourceLockError as error:
            return self._purge_refused(
                request,
                manifest,
                RemoveErrorCode.LOCK_UNAVAILABLE,
                reason=(
                    f"{RemoveErrorCode.LOCK_UNAVAILABLE.value}:{error.code.value}"
                ),
            )
        return await self._gated_purge(request, plan, manifest, lease, notes)

    # -- shared helpers ----------------------------------------------------------

    def _plan(self, request: RemoveRequest | PurgeDataRequest) -> DeploymentPlan:
        plan = build_deployment_plan(
            request.recipe,
            request.server_id,
            request.application_id,
            request.target,
        )
        _validate_plan_vocabulary(plan)
        return plan

    def _refused(
        self,
        request: RemoveRequest,
        plan: DeploymentPlan,
        code: RemoveErrorCode,
        *,
        reason: str | None = None,
        notes: tuple[str, ...] = (),
    ) -> RemoveResult:
        return RemoveResult(
            server_id=request.server_id,
            application_id=request.application_id,
            plan_sha256=plan.plan_sha256,
            status=STATUS_REFUSED,
            effect=EFFECT_NOT_ATTEMPTED,
            reason=reason if reason is not None else code.value,
            data_intent=request.data_intent.value,
            notes=notes,
        )

    def _purge_refused(
        self,
        request: PurgeDataRequest,
        manifest: DeletionManifest,
        code: RemoveErrorCode,
        *,
        reason: str | None = None,
    ) -> PurgeDataResult:
        return PurgeDataResult(
            server_id=request.server_id,
            application_id=request.application_id,
            manifest_sha256=manifest.manifest_sha256,
            status=STATUS_REFUSED,
            effect=EFFECT_NOT_ATTEMPTED,
            reason=reason if reason is not None else code.value,
            locations=manifest.locations,
        )

    def _decide(
        self,
        request: RemoveRequest | PurgeDataRequest,
        capability_id: str,
        descriptor: CapabilityDescriptor,
    ) -> PolicyDecision:
        return evaluate(
            context=request.context,
            capability_id=capability_id,
            descriptor=descriptor,
            grants=self._grants(),
            policy=self._policy(),
            adopted=self._adopted(request.server_id),
        )

    def _read(
        self,
        request: RemoveRequest | PurgeDataRequest,
        *,
        display: str,
        argv: tuple[str, ...],
    ) -> CommandOutcome | None:
        """One bounded read; ``None`` ⇒ malformed request (wiring bug)."""
        probe = CommandRequest(
            op_id=uuid.uuid4(),
            server_id=request.server_id,
            display=display,
            timeout=_PREFLIGHT_TIMEOUT_SECONDS,
            output_budget=_OUTPUT_BUDGET_BYTES,
            argv=argv,
        )
        try:
            return self._transport(probe)
        except CommandRequestError:
            return None

    async def _prelock_guards(
        self, request: RemoveRequest, plan: DeploymentPlan
    ) -> RemoveResult | None:
        """Identity/projection guards that need no lease and cause no effect."""
        sid, aid = request.server_id, request.application_id
        # Cross-server identity guard (C09 rule 1; mirrors install/adopt).
        if any(s != sid for s in self._store.servers_storing(aid)):
            return self._refused(request, plan, RemoveErrorCode.CROSS_SERVER_CONFLICT)
        # Canonical store consistency: an interrupted/half pair is surfaced
        # (C02 W2), never healed by a removal.
        try:
            stored = self._store.read(sid, aid)
        except AdoptionError as error:
            if error.code is not AdoptionErrorCode.NOT_FOUND:
                return self._refused(
                    request,
                    plan,
                    RemoveErrorCode.STATE_DIVERGED,
                    notes=(f"canonical store: {error.code.value}",),
                )
            stored = None
        documents: list[object] = []
        if stored is not None:
            documents.append(parse_manifest_text(stored.text))
        existing = await self._adoption.read(sid, aid)
        if existing is not None:
            documents.append(existing)
        # The identity must not be bound to a different project (an adopted
        # EXTERNAL deployment keeps its own paths, C09 rule 6).
        for document in documents:
            project = getattr(document, "compose_project", None)
            if project is not None and project != plan.project_name:
                return self._refused(
                    request, plan, RemoveErrorCode.EXTERNAL_RECORD_CONFLICT
                )
        return None

    def _discover_state(
        self, request: RemoveRequest | PurgeDataRequest, plan: DeploymentPlan
    ) -> _TargetState | None:
        """Classify the target; ``None`` ⇒ a state read is unknown (C04.5)."""
        hash_out = self._read(
            request,
            display="read managed compose file hash",
            argv=("sha256sum", "--", plan.compose_path),
        )
        if hash_out is None:
            return None
        if (
            hash_out.status is not CommandStatus.SUCCEEDED
            and hash_out.exit_code is None
        ):
            # A timeout cut on the hash read is an UNKNOWN state, never a
            # verified absence (C04.5).
            return None
        compose_present = hash_out.status is CommandStatus.SUCCEEDED
        compose_hash_matches = compose_present and hash_out.stdout.split()[:1] == [
            plan.compose_sha256
        ]
        ps_out = self._read(
            request,
            display="read managed compose project state",
            argv=_compose_argv(plan, "ps", "-q"),
        )
        if ps_out is None:
            return None
        if ps_out.status is CommandStatus.SUCCEEDED:
            project_running = bool(ps_out.stdout.strip())
        elif ps_out.exit_code is not None:
            # A defined non-zero exit is compose's "no such project" — a
            # verified absence, not an unknown.
            project_running = False
        else:
            return None
        return _TargetState(
            compose_present=compose_present,
            compose_hash_matches=bool(compose_hash_matches),
            project_running=project_running,
        )

    def _project_absent(
        self, request: RemoveRequest | PurgeDataRequest, plan: DeploymentPlan
    ) -> bool | None:
        """Verified-absence read: True absent, False present, None unknown."""
        outcome = self._read(
            request,
            display="verify managed compose project removal",
            argv=_compose_argv(plan, "ps", "-q"),
        )
        if outcome is None:
            return None
        if outcome.stdout.strip():
            return False
        if outcome.status is CommandStatus.SUCCEEDED or outcome.exit_code is not None:
            return True
        return None

    # -- tier 1 flow -----------------------------------------------------------

    async def _gated_remove(
        self,
        request: RemoveRequest,
        plan: DeploymentPlan,
        lease: LockLease,
    ) -> RemoveResult:
        operations: list[AppliedOperation] = []
        notes: list[str] = []
        released = False

        def release() -> None:
            nonlocal released
            self._locks.release(lease)
            released = True

        def result(**kw: object) -> RemoveResult:
            base: dict[str, object] = dict(
                server_id=request.server_id,
                application_id=request.application_id,
                plan_sha256=plan.plan_sha256,
                data_intent=request.data_intent.value,
                operations=tuple(operations),
                notes=tuple(notes),
                lock_released=released,
            )
            base.update(kw)
            return RemoveResult(**base)  # type: ignore[arg-type]

        # ---- policy gate (first read; re-checked before the effect) ----
        decision = self._decide(request, CAPABILITY_ID, self._descriptor)
        if not decision.allowed:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=f"{RemoveErrorCode.POLICY_DENIED.value}:"
                f"{decision.reason.value}",
            )

        # ---- target-state classification (reads; no effect) ----
        state = self._discover_state(request, plan)
        if state is None:
            # The lease is left held: it lapses into a fence while the
            # target state is unresolved (C04.5/C05.7, mirrors install).
            return result(
                status=STATUS_OUTCOME_UNKNOWN,
                effect=EFFECT_UNKNOWN,
                reason=RemoveErrorCode.OUTCOME_UNKNOWN.value,
            )
        if not state.compose_present and not state.project_running:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=RemoveErrorCode.NOT_INSTALLED.value,
            )
        if not state.compose_present and state.project_running:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=RemoveErrorCode.STATE_DIVERGED.value,
                notes=("managed compose file absent while the project runs",),
            )
        if not state.compose_hash_matches:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=RemoveErrorCode.STATE_DIVERGED.value,
                notes=("compose file hash differs from the bound plan",),
            )

        # ---- pre-effect recheck + approval consumption (C03 rule 3/4) ----
        recheck = self._decide(request, CAPABILITY_ID, self._descriptor)
        if not recheck.allowed:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=f"{RemoveErrorCode.POLICY_DENIED.value}:"
                f"{recheck.reason.value}",
            )
        try:
            approval = await self._approvals.consume(
                request.approval_id,
                capability_id=CAPABILITY_ID,
                server_id=request.server_id,
                payload_hash=plan.plan_sha256,
                policy_revision=recheck.policy_revision,
                consumed_by=request.context.principal,
            )
        except ApprovalError as error:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=(
                    f"{RemoveErrorCode.APPROVAL_REJECTED.value}:{error.code.value}"
                ),
                approval_id=request.approval_id,
            )

        # ---- the teardown: one bounded down, under heartbeat ----
        with _LeaseHeartbeat(self._locks, lease) as heartbeat:
            if heartbeat.lapsed:
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=RemoveErrorCode.OUTCOME_UNKNOWN.value,
                    notes=("resource lease lapsed mid-apply",),
                    approval_id=approval.approval_id,
                )
            down = self._transport(
                CommandRequest(
                    op_id=uuid.uuid4(),
                    server_id=request.server_id,
                    display="remove managed compose project",
                    timeout=_DOWN_TIMEOUT_SECONDS,
                    output_budget=_OUTPUT_BUDGET_BYTES,
                    argv=_compose_argv(plan, "down", "--remove-orphans"),
                )
            )
            operations.append(
                AppliedOperation(
                    "compose_down",
                    plan.project_name,
                    _ACTION_APPLIED,
                    down.status,
                    down.exit_code,
                )
            )
            if down.status is not CommandStatus.SUCCEEDED and down.exit_code is None:
                # Timeout cut / transport death: the prior process may
                # live. outcome_unknown; the lease stays held (fence).
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=RemoveErrorCode.OUTCOME_UNKNOWN.value,
                    approval_id=approval.approval_id,
                )
            if heartbeat.lapsed:
                # The lease was lost while the teardown ran: never claim
                # the outcome nor touch the canonical record on fenced
                # ground (C05 rule 6/7; mirrors the per-op checks).
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=RemoveErrorCode.OUTCOME_UNKNOWN.value,
                    notes=("resource lease lapsed mid-apply",),
                    approval_id=approval.approval_id,
                )
            absent = self._project_absent(request, plan)
            if absent is None:
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=RemoveErrorCode.OUTCOME_UNKNOWN.value,
                    approval_id=approval.approval_id,
                )
            if not absent:
                # "Removed" is claimed only when observed (C04.6): a down
                # that left containers is a deterministic failure.
                notes.append("compose down did not verify project removal")
                release()
                return result(
                    status=STATUS_FAILED,
                    effect=EFFECT_EXECUTED,
                    reason=RemoveErrorCode.APPLY_FAILED.value,
                    approval_id=approval.approval_id,
                )
            if down.status is not CommandStatus.SUCCEEDED:
                # e.g. compose's "no such project" — the project is
                # VERIFIED absent, which is the removal's goal.
                notes.append(
                    "down reported "
                    f"exit {down.exit_code}; project verified absent"
                )

        # Verified teardown → canonical tombstone/purge (C02 rule 5/W4).
        removed, revision = await self._revoke_manifest(request, plan, notes)
        release()
        if request.data_intent is RemoveDataIntent.DELETE:
            notes.append(
                "declared delete-data intent stands; persistent data survives "
                "until the separately authorized purge-data operation "
                f"({PURGE_CAPABILITY_ID})"
            )
        return result(
            status=STATUS_SUCCEEDED,
            effect=EFFECT_EXECUTED,
            reason=None,
            approval_id=approval.approval_id,
            manifest_removed=removed,
            manifest_revision=revision,
            data_deleted=False,
        )

    async def _revoke_manifest(
        self,
        request: RemoveRequest,
        plan: DeploymentPlan,
        notes: list[str],
    ) -> tuple[bool, int | None]:
        """Canonical tombstone/purge through the adoption pipeline.

        Deterministic intent id ⇒ an interrupted canonical deletion
        converges on retry (C02 rule 5). A refusal here is a recorded
        note — the verified teardown is not undone for a bookkeeping
        failure; no manifest is ever resurrected.
        """
        sid, aid = request.server_id, request.application_id
        intent = remove_intent_id(sid, aid, plan.plan_sha256)
        for attempt in (0, 1):
            try:
                existing = await self._adoption.read(sid, aid)
            except AdoptionError:
                existing = None
            expected = ABSENT_REVISION if existing is None else existing.revision
            try:
                await self._adoption.revoke(
                    self._store,
                    sid,
                    aid,
                    expected_revision=expected,
                    intent_id=intent,
                )
            except AdoptionError as error:
                if (
                    error.code is AdoptionErrorCode.NOT_ADOPTED
                    and existing is None
                ):
                    notes.append("no canonical record to tombstone")
                    return False, None
                if attempt == 0 and error.code in _RECOVERABLE_ADOPTION_CODES:
                    # One recovery sweep for an interrupted canonical pair
                    # (C02 W1/W2/W4), then one retry; never a silent heal.
                    await self._adoption.recover(self._store, server_id=sid)
                    continue
                notes.append(f"manifest tombstone refused: {error.code.value}")
                return False, None
            return True, (None if existing is None else existing.revision)
        notes.append("manifest tombstone refused: recovery sweep exhausted")
        return False, None

    # -- tier 2 flow -------------------------------------------------------------

    async def _gated_purge(
        self,
        request: PurgeDataRequest,
        plan: DeploymentPlan,
        manifest: DeletionManifest,
        lease: LockLease,
        notes: list[str],
    ) -> PurgeDataResult:
        operations: list[AppliedOperation] = []
        released = False

        def release() -> None:
            nonlocal released
            self._locks.release(lease)
            released = True

        def result(**kw: object) -> PurgeDataResult:
            base: dict[str, object] = dict(
                server_id=request.server_id,
                application_id=request.application_id,
                manifest_sha256=manifest.manifest_sha256,
                locations=manifest.locations,
                operations=tuple(operations),
                notes=tuple(notes),
                lock_released=released,
            )
            base.update(kw)
            return PurgeDataResult(**base)  # type: ignore[arg-type]

        # ---- policy gate (first read; re-checked before the effect) ----
        decision = self._decide(
            request, PURGE_CAPABILITY_ID, self._purge_descriptor
        )
        if not decision.allowed:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=f"{RemoveErrorCode.POLICY_DENIED.value}:"
                f"{decision.reason.value}",
            )

        # ---- target-state guard: the deployment must be VERIFIED gone ----
        state = self._discover_state(request, plan)
        if state is None:
            return result(
                status=STATUS_OUTCOME_UNKNOWN,
                effect=EFFECT_UNKNOWN,
                reason=RemoveErrorCode.OUTCOME_UNKNOWN.value,
            )
        if state.compose_present and not state.compose_hash_matches:
            # Bind the deletion manifest to the DEPLOYED reality: different
            # bytes may mean different volume paths (C09 rule 5 honesty).
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=RemoveErrorCode.STATE_DIVERGED.value,
                notes=("compose file hash differs from the bound plan",),
            )
        absent = self._project_absent(request, plan)
        if absent is None:
            return result(
                status=STATUS_OUTCOME_UNKNOWN,
                effect=EFFECT_UNKNOWN,
                reason=RemoveErrorCode.OUTCOME_UNKNOWN.value,
            )
        if not absent:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=RemoveErrorCode.APP_STILL_DEPLOYED.value,
                notes=(
                    "refusing data deletion while the deployment is present; "
                    "run the removal operation first",
                ),
            )

        # ---- pre-effect recheck + approval consumption (C03 rule 3/4) ----
        recheck = self._decide(
            request, PURGE_CAPABILITY_ID, self._purge_descriptor
        )
        if not recheck.allowed:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=f"{RemoveErrorCode.POLICY_DENIED.value}:"
                f"{recheck.reason.value}",
            )
        try:
            approval = await self._approvals.consume(
                request.approval_id,
                capability_id=PURGE_CAPABILITY_ID,
                server_id=request.server_id,
                payload_hash=manifest.manifest_sha256,
                policy_revision=recheck.policy_revision,
                consumed_by=request.context.principal,
            )
        except ApprovalError as error:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=(
                    f"{RemoveErrorCode.APPROVAL_REJECTED.value}:{error.code.value}"
                ),
                approval_id=request.approval_id,
            )

        # ---- the deletion: manifest locations in manifest order ----
        verified_all = True
        with _LeaseHeartbeat(self._locks, lease) as heartbeat:
            for location in manifest.locations:
                if heartbeat.lapsed:
                    return result(
                        status=STATUS_OUTCOME_UNKNOWN,
                        effect=EFFECT_UNKNOWN,
                        reason=RemoveErrorCode.OUTCOME_UNKNOWN.value,
                        notes=("resource lease lapsed mid-apply",),
                        approval_id=approval.approval_id,
                    )
                present = self._data_present(request, location)
                if present is None:
                    return result(
                        status=STATUS_OUTCOME_UNKNOWN,
                        effect=EFFECT_UNKNOWN,
                        reason=RemoveErrorCode.OUTCOME_UNKNOWN.value,
                        approval_id=approval.approval_id,
                    )
                if not present:
                    # Verified absence: nothing to delete, nothing mutated.
                    operations.append(
                        AppliedOperation(
                            "delete_data", location, _ACTION_VERIFIED,
                            CommandStatus.SUCCEEDED, 0,
                        )
                    )
                    continue
                rm = self._transport(
                    CommandRequest(
                        op_id=uuid.uuid4(),
                        server_id=request.server_id,
                        display="delete application persistent data",
                        timeout=_PREFLIGHT_TIMEOUT_SECONDS,
                        output_budget=_OUTPUT_BUDGET_BYTES,
                        argv=("rm", "-rf", "--", location),
                    )
                )
                operations.append(
                    AppliedOperation(
                        "delete_data",
                        location,
                        _ACTION_APPLIED,
                        rm.status,
                        rm.exit_code,
                    )
                )
                if (
                    rm.status is not CommandStatus.SUCCEEDED
                    and rm.exit_code is None
                ):
                    # Timeout cut / transport death: the rm process may
                    # live. outcome_unknown; the lease stays held (fence).
                    return result(
                        status=STATUS_OUTCOME_UNKNOWN,
                        effect=EFFECT_UNKNOWN,
                        reason=RemoveErrorCode.OUTCOME_UNKNOWN.value,
                        approval_id=approval.approval_id,
                    )
                if heartbeat.lapsed:
                    # The lease was lost while the deletion ran: claim
                    # nothing further on fenced ground (C05 rule 6/7).
                    return result(
                        status=STATUS_OUTCOME_UNKNOWN,
                        effect=EFFECT_UNKNOWN,
                        reason=RemoveErrorCode.OUTCOME_UNKNOWN.value,
                        notes=("resource lease lapsed mid-apply",),
                        approval_id=approval.approval_id,
                    )
                still = self._data_present(request, location)
                if still is None:
                    return result(
                        status=STATUS_OUTCOME_UNKNOWN,
                        effect=EFFECT_UNKNOWN,
                        reason=RemoveErrorCode.OUTCOME_UNKNOWN.value,
                        approval_id=approval.approval_id,
                    )
                if still:
                    # Verify-then-trust: the rm claims success but the
                    # location still holds data — deterministic failure.
                    notes.append(
                        "deletion could not be verified for a persistent-data "
                        "location"
                    )
                    verified_all = False
                    release()
                    return result(
                        status=STATUS_FAILED,
                        effect=EFFECT_EXECUTED,
                        reason=RemoveErrorCode.APPLY_FAILED.value,
                        approval_id=approval.approval_id,
                        verified=False,
                    )
                if rm.status is not CommandStatus.SUCCEEDED:
                    notes.append(
                        f"delete reported exit {rm.exit_code}; location "
                        "verified absent"
                    )

        release()
        return result(
            status=STATUS_SUCCEEDED if verified_all else STATUS_FAILED,
            effect=EFFECT_EXECUTED,
            reason=None if verified_all else RemoveErrorCode.APPLY_FAILED.value,
            approval_id=approval.approval_id,
            verified=verified_all,
        )

    def _data_present(
        self, request: PurgeDataRequest, location: str
    ) -> bool | None:
        """True when the location holds data, False when verified absent,
        ``None`` when the read itself is unknown."""
        outcome = self._read(
            request,
            display="read persistent data location",
            argv=("ls", "-A", "--", location),
        )
        if outcome is None:
            return None
        if outcome.status is CommandStatus.SUCCEEDED:
            return bool(outcome.stdout.strip())
        if outcome.exit_code is not None:
            return False  # defined non-zero: verified absence
        return None
