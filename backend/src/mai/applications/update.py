"""Durable, approval-gated, resource-locked application update with
verified rollback to the prior pinned digest.

Card ``08-update``; consumer of the same integrated P08 producers as
``mai.applications.install`` (``recipes``, ``adopt``, the P03 gate, the
P04 bounded command lane, ``mai.jobs.resource_locks``). Contracts.md:

- **C09 rule 4 (revision/image-aware, honest recovery limits)** — the
  update records the VERIFIED prior version (the request's declared
  baseline recipe is confirmed against the deployed Compose bytes before
  any effect; a mismatch refuses ``state_diverged``) and the new pinned
  digest in the result. Rollback is claimed only within the recipe's
  declared limits: a health regression triggers the automatic rollback to
  the prior pinned digest ONLY when ``recovery.image_rollback`` is true;
  otherwise the failure is surfaced with the plan's own limitation
  (``recovery: no image rollback``) and nothing is restored — there is no
  universal rollback checkbox. Irreversible migrations keep the plan's
  ``data rollback is not possible`` limitation on every receipt.
- **C09 rule 3 (apply only the bound plan)** — the executor rebuilds the
  deterministic plan for the NEW recipe, validates the frozen operation
  vocabulary, and applies ONLY the plan's operations in plan order. The
  pre-update snapshot and the baseline restore are executor-owned safety
  steps recorded in the receipt, never plan operations.
- **C03 rules 3–5 (gate)** — full policy decision taken and re-checked
  immediately before the privileged effect; the one-use approval is
  consumed atomically bound to ``(capability_id=app.update, server_id,
  payload_hash=new plan sha256, policy_revision)``. Any denial returns a
  ``refused`` result with ``effect="not_attempted"`` and consumes nothing.
- **C05 rule 6 (locks)** — the whole apply runs under the SAME exclusive
  ``application:<application_id>`` lease namespace install uses, so an
  update and an install (or a concurrent update) on one application
  exclude each other; a lapsed lease aborts the apply
  (``outcome_unknown``) and the lease is left to lapse into a fence.
- **C04 rules 5–7 (honest outcomes)** — a timeout cut or transport death
  is ``outcome_unknown``: never retried silently and NEVER restored (the
  prior process may live; a duplicate mutation is the forbidden effect).
  Deterministic failures restore the VERIFIED baseline only when no such
  ambiguity exists.
- **Pre-update snapshot** — before the first mutating operation, every
  declared persistent-data location is archived to a deterministic
  snapshot path below the application's compose directory (``tar``). The
  snapshot only ever CREATES a file (C09 rule 5: no deletion path) and
  its verified path is recorded in the result.
- **Rollback mechanics** — restore the baseline Compose document (the
  bytes whose hash was verified pre-update), ``compose up -d`` the PRIOR
  pinned digest (no registry pull: that digest already ran on this
  target), and re-run the FULL health gate against the baseline plan.
  ``rollback_verified`` means the baseline deployment answered healthy
  again; a restore that cannot be verified is reported honestly.
- **C09 canonical update** — only fully verified success updates the
  canonical manifest/projection, through the adoption pipeline with a
  deterministic intent id derived from the new plan hash (an interrupted
  canonical update converges on retry instead of duplicating). A failed
  or rolled-back update commits nothing: the canonical record keeps
  describing the deployed baseline.

Pure stdlib + integrated sibling modules; no engine registration here:
composition is the caller's wiring, exactly like ``install.py``. The
pure helpers reused from ``mai.applications.install`` (transport, write
channel, health verdict, lease heartbeat) are the VERIFIED 08-install
surface — one implementation of each subtle behavior, not a fork.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from time import monotonic, sleep

from mai.applications.adopt import (
    AdoptionError,
    AdoptionErrorCode,
    AdoptionRequest,
    ApplicationAdoption,
    ApplicationManifestStore,
    parse_manifest_text,
)
from mai.applications.discover import ApplicationOrigin
from mai.applications.install import (
    _ACTION_APPLIED,
    _ACTION_VERIFIED,
    _KNOWN_PREREQUISITES,
    _LeaseHeartbeat,
    _OUTPUT_BUDGET_BYTES,
    _PREFLIGHT_TIMEOUT_SECONDS,
    _PULL_TIMEOUT_SECONDS,
    _RECOVERABLE_ADOPTION_CODES,
    _StoredRevision,
    _UP_TIMEOUT_SECONDS,
    _compose_argv,
    _compose_health_verdict,
    _published_health_port,
    _write_file_command,
    AppliedOperation,
    DEFAULT_LEASE_TTL_SECONDS,
    EFFECT_EXECUTED,
    EFFECT_NOT_ATTEMPTED,
    EFFECT_UNKNOWN,
    LOCK_RESOURCE_PREFIX,
    STATUS_FAILED,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_REFUSED,
    STATUS_ROLLED_BACK,
    STATUS_SUCCEEDED,
    Transport,
    ssh_transport,
)
from mai.applications.recipes import (
    OP_CREATE_DIRECTORIES,
    OP_PULL_IMAGE,
    OP_REQUIRE_BACKUP,
    OP_START,
    OP_VERIFY_PREREQUISITES,
    OP_WAIT_HEALTHY,
    OP_WRITE_COMPOSE,
    OP_WRITE_CREDENTIALS,
    ApplicationRecipe,
    DeploymentPlan,
    PlanOperation,
    TargetEnvironment,
    UpdateMigrations,
    build_deployment_plan,
)
from mai.credentials.env import EnvFileError, EnvResolver
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
    "SNAPSHOT_DIRNAME",
    "AppliedOperation",
    "UpdateError",
    "UpdateErrorCode",
    "UpdateIntentNamespace",
    "UpdateRequest",
    "UpdateResult",
    "UpdateService",
    "ssh_transport",
    "update_intent_id",
]

#: The capability slug this module executes (C03 rule 1 shape). Grants
#: name this id; the descriptor wired into the service must carry it.
CAPABILITY_ID: str = "app.update"

#: Deterministic canonical-update intent namespace (distinct from the
#: install namespace: an update's canonical commit is a different intent
#: even when plan hashes coincide across cards).
UpdateIntentNamespace = uuid.uuid5(
    uuid.NAMESPACE_URL, "mai:update:intent-namespace:v1"
)

#: Snapshot directory below the application's compose directory.
SNAPSHOT_DIRNAME = "snapshots"

# Per-lane bound specific to this card (the others are reused verbatim
# from the install lane above).
_SNAPSHOT_TIMEOUT_SECONDS = 600.0
_HEALTH_POLL_INTERVAL_SECONDS = 2.0


class UpdateErrorCode(StrEnum):
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
    PRIOR_STATE_UNHEALTHY = "prior_state_unhealthy"
    BACKUP_REQUIRED = "backup_required"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    UNSUPPORTED_PREREQUISITE = "unsupported_prerequisite"
    HEALTH_NOT_DECLARED = "health_not_declared"
    APPLY_FAILED = "apply_failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class UpdateError(ValueError):
    """Structured, value-free wiring/input rejection (raised, not returned).

    Only for malformed constructor wiring or request fields — the same
    boundary :class:`~mai.applications.install.InstallService` draws. Gate
    denials and execution outcomes are returned as :class:`UpdateResult`
    values, never raised.
    """

    def __init__(self, code: UpdateErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class UpdateRequest:
    """One validated update intent (baseline + new recipe + gate handles).

    ``recipe`` is the NEW validated recipe (pinned to the target digest);
    ``current_recipe`` is the baseline the operator asserts is deployed —
    the executor VERIFIES that assertion against the deployed Compose
    bytes before any effect and refuses ``state_diverged`` otherwise, so
    the recorded prior version is observed, never assumed. Both recipes
    share the application identity (the plan paths and Compose project
    derive from ``application_id`` alone). ``context.server_id`` MUST
    equal ``server_id`` (C05 rule 3). ``approval_id`` is the pending
    one-use decision bound to the NEW plan this service rebuilds.
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    recipe: ApplicationRecipe
    current_recipe: ApplicationRecipe
    target: TargetEnvironment
    context: ExecutionContext
    approval_id: uuid.UUID
    #: Confirms the operator took the declared backup for existing volume
    #: data before this data-affecting operation (C09 rule 4/5).
    backup_confirmed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, uuid.UUID):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST, "server_id must be a uuid.UUID"
            )
        if not isinstance(self.application_id, uuid.UUID):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST,
                "application_id must be a uuid.UUID",
            )
        if not isinstance(self.recipe, ApplicationRecipe):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST,
                "recipe must be a validated ApplicationRecipe (parse_recipe_text)",
            )
        if not isinstance(self.current_recipe, ApplicationRecipe):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST,
                "current_recipe must be a validated ApplicationRecipe "
                "(parse_recipe_text)",
            )
        if not isinstance(self.target, TargetEnvironment):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST,
                "target must be a TargetEnvironment",
            )
        if not isinstance(self.context, ExecutionContext):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST,
                "context must be an ExecutionContext",
            )
        if self.context.server_id != self.server_id:
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST,
                "context.server_id does not match the request target "
                "(C05 rule 3 run binding)",
            )
        if not isinstance(self.approval_id, uuid.UUID):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST, "approval_id must be a uuid.UUID"
            )
        if not isinstance(self.backup_confirmed, bool):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST, "backup_confirmed must be a bool"
            )


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """The audit receipt of one update attempt.

    ``prior_image`` is the VERIFIED baseline digest (set once the prior
    state is confirmed; ``None`` when refused before that) and
    ``new_image`` the requested target digest. ``effect`` never overclaims:
    ``refused`` pairs with ``not_attempted``; ``outcome_unknown`` with
    ``unknown``; only verified success or a verified baseline restore
    claims ``executed``. ``manifest_revision`` is set only when the
    canonical manifest/projection update RECONCILED (C02 rule 4). The
    declared data/recovery constraints travel in ``migrations`` and
    ``image_rollback_supported`` (C09 rule 4: recorded, not guessed).
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    plan_sha256: str
    status: str
    effect: str
    reason: str | None
    new_image: str
    migrations: str
    image_rollback_supported: bool
    operations: tuple[AppliedOperation, ...] = ()
    notes: tuple[str, ...] = ()
    prior_image: str | None = None
    prior_healthy: bool = False
    snapshot_paths: tuple[str, ...] = ()
    approval_id: uuid.UUID | None = None
    manifest_revision: int | None = None
    manifest_sha256: str | None = None
    health_verified: bool = False
    published_ports: tuple[int, ...] = ()
    rollback_attempted: bool = False
    rollback_verified: bool = False
    lock_released: bool = False


def update_intent_id(
    server_id: uuid.UUID, application_id: uuid.UUID, plan_sha256: str
) -> uuid.UUID:
    """Deterministic canonical-update intent id (resume-safe, C02 rule 5)."""
    return uuid.uuid5(
        UpdateIntentNamespace,
        f"update:{server_id}:{application_id}:{plan_sha256}",
    )


def _snapshot_path(plan: DeploymentPlan) -> str:
    return (
        f"{plan.compose_path.rsplit('/', 1)[0]}/{SNAPSHOT_DIRNAME}"
        f"/pre-update-{plan.plan_sha256[:12]}.tar"
    )


def _validate_plan_vocabulary(plan: DeploymentPlan) -> None:
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
            raise UpdateError(
                UpdateErrorCode.INVALID_PLAN,
                "plan carries an operation outside the frozen vocabulary",
            )


class UpdateService:
    """The enforced update boundary: baseline verification → plan → policy
    → approval → lock → snapshot → apply → health gate → verified rollback
    or canonical update.

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
        credentials: Callable[[], EnvResolver],
        descriptor: CapabilityDescriptor,
        lease_ttl: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        if not callable(transport):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST, "transport must be callable"
            )
        if not isinstance(locks, ResourceLockService):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST,
                "locks must be a ResourceLockService",
            )
        if not isinstance(approvals, ApprovalStore):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST,
                "approvals must be an ApprovalStore",
            )
        if not isinstance(adoption, ApplicationAdoption):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST,
                "adoption must be an ApplicationAdoption",
            )
        if not isinstance(store, ApplicationManifestStore):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST,
                "store must be an ApplicationManifestStore",
            )
        for name, provider in (
            ("grants", grants),
            ("policy", policy),
            ("adopted", adopted),
            ("credentials", credentials),
        ):
            if not callable(provider):
                raise UpdateError(
                    UpdateErrorCode.INVALID_REQUEST, f"{name} must be callable"
                )
        if not isinstance(descriptor, CapabilityDescriptor):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST,
                "descriptor must be a CapabilityDescriptor",
            )
        if descriptor.identifier != CAPABILITY_ID:
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST,
                "descriptor identifier must be app.update",
            )
        if descriptor.effect_class != "privileged":
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST,
                "update is a privileged effect; the descriptor must declare "
                "the privileged effect class",
            )
        if isinstance(lease_ttl, bool) or not isinstance(lease_ttl, (int, float)):
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST, "lease_ttl must be a number"
            )
        self._transport = transport
        self._locks = locks
        self._approvals = approvals
        self._adoption = adoption
        self._store = store
        self._grants = grants
        self._policy = policy
        self._adopted = adopted
        self._credentials = credentials
        self._descriptor = descriptor
        self._lease_ttl = float(lease_ttl)

    # -- entry ---------------------------------------------------------------

    async def update(
        self,
        request: UpdateRequest,
        *,
        intent_id: uuid.UUID | None = None,
    ) -> UpdateResult:
        """Apply one validated recipe update over its verified baseline
        (see module doc). ``intent_id`` defaults to the deterministic
        plan-derived id so a re-run after an interrupted canonical update
        converges instead of duplicating.
        """
        sid, aid = request.server_id, request.application_id
        plan = build_deployment_plan(request.recipe, sid, aid, request.target)
        baseline = build_deployment_plan(
            request.current_recipe, sid, aid, request.target
        )
        _validate_plan_vocabulary(plan)
        refusal = await self._prelock_guards(request, plan, baseline)
        if refusal is not None:
            return refusal

        try:
            lease = self._locks.acquire(
                str(sid),
                f"{LOCK_RESOURCE_PREFIX}{aid}",
                owner_ref=f"update:{request.context.run_id}",
                ttl=self._lease_ttl,
            )
        except ResourceLockError as error:
            return self._refused(
                request,
                plan,
                UpdateErrorCode.LOCK_UNAVAILABLE,
                reason=(
                    f"{UpdateErrorCode.LOCK_UNAVAILABLE.value}:{error.code.value}"
                ),
            )
        return await self._gated_apply(request, plan, baseline, lease, intent_id)

    # -- gate phases -----------------------------------------------------------

    def _refused(
        self,
        request: UpdateRequest,
        plan: DeploymentPlan,
        code: UpdateErrorCode,
        *,
        reason: str | None = None,
        notes: tuple[str, ...] = (),
    ) -> UpdateResult:
        return UpdateResult(
            server_id=request.server_id,
            application_id=request.application_id,
            plan_sha256=plan.plan_sha256,
            status=STATUS_REFUSED,
            effect=EFFECT_NOT_ATTEMPTED,
            reason=reason if reason is not None else code.value,
            new_image=plan.image,
            migrations=request.recipe.update.migrations.value,
            image_rollback_supported=request.recipe.recovery.image_rollback,
            notes=notes,
        )

    async def _prelock_guards(
        self,
        request: UpdateRequest,
        plan: DeploymentPlan,
        baseline: DeploymentPlan,
    ) -> UpdateResult | None:
        """Identity/projection guards that need no lease and cause no effect."""
        sid = request.server_id
        aid = request.application_id
        # Cross-server identity guard (C09 rule 1; mirrors install).
        if any(s != sid for s in self._store.servers_storing(aid)):
            return self._refused(
                request, plan, UpdateErrorCode.CROSS_SERVER_CONFLICT
            )
        unknown = [
            token
            for token in request.recipe.prerequisites
            if token not in _KNOWN_PREREQUISITES
        ]
        if unknown:
            return self._refused(
                request, plan, UpdateErrorCode.UNSUPPORTED_PREREQUISITE
            )
        if request.recipe.health is None or baseline.health_probe is None:
            # Completion AND rollback both require declared verification:
            # refuse before any target contact rather than run an update
            # whose success (or recovery) cannot be proven.
            return self._refused(
                request, plan, UpdateErrorCode.HEALTH_NOT_DECLARED
            )
        # Canonical store consistency: an interrupted/half pair is surfaced
        # (C02 W2), never healed by an update.
        try:
            stored = self._store.read(sid, aid)
        except AdoptionError as error:
            if error.code is not AdoptionErrorCode.NOT_FOUND:
                return self._refused(
                    request,
                    plan,
                    UpdateErrorCode.STATE_DIVERGED,
                    notes=(f"canonical store: {error.code.value}",),
                )
            stored = None
        documents: list[object] = []
        if stored is not None:
            documents.append(parse_manifest_text(stored.text))
        existing = await self._adoption.read(sid, aid)
        if existing is not None:
            documents.append(existing)
        # The identity must not already be bound to a different project
        # (an adopted EXTERNAL deployment keeps its own paths, C09 rule 6).
        for document in documents:
            project = getattr(document, "compose_project", None)
            if project is not None and project != plan.project_name:
                return self._refused(
                    request, plan, UpdateErrorCode.EXTERNAL_RECORD_CONFLICT
                )
        return None

    async def _gated_apply(
        self,
        request: UpdateRequest,
        plan: DeploymentPlan,
        baseline: DeploymentPlan,
        lease: LockLease,
        intent_id: uuid.UUID | None,
    ) -> UpdateResult:
        operations: list[AppliedOperation] = []
        notes: list[str] = []
        snapshot_paths: list[str] = []
        released = False

        def release() -> None:
            nonlocal released
            self._locks.release(lease)
            released = True

        def result(**kw: object) -> UpdateResult:
            base: dict[str, object] = dict(
                server_id=request.server_id,
                application_id=request.application_id,
                plan_sha256=plan.plan_sha256,
                new_image=plan.image,
                migrations=request.recipe.update.migrations.value,
                image_rollback_supported=request.recipe.recovery.image_rollback,
                operations=tuple(operations),
                notes=tuple(notes),
                snapshot_paths=tuple(snapshot_paths),
                published_ports=tuple(sorted(p.host for p in plan.ports)),
                lock_released=released,
            )
            base.update(kw)
            return UpdateResult(**base)  # type: ignore[arg-type]

        # ---- policy gate (first read; re-checked before the effect) ----
        decision = self._decide(request)
        if not decision.allowed:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=f"{UpdateErrorCode.POLICY_DENIED.value}:"
                f"{decision.reason.value}",
            )

        # ---- pre-flight reads (no target effect) ----
        probe = CommandRequest(
            op_id=uuid.uuid4(),
            server_id=request.server_id,
            display="verify declared prerequisites",
            timeout=_PREFLIGHT_TIMEOUT_SECONDS,
            output_budget=_OUTPUT_BUDGET_BYTES,
            argv=("docker", "version", "--format", "{{.Server.Version}}"),
        )
        outcome = self._transport(probe)
        operations.append(
            AppliedOperation(
                OP_VERIFY_PREREQUISITES,
                ",".join(request.recipe.prerequisites),
                _ACTION_APPLIED,
                outcome.status,
                outcome.exit_code,
            )
        )
        if outcome.status is not CommandStatus.SUCCEEDED:
            return self._finish_failed(
                request, plan, baseline, notes, operations,
                failed_op=OP_VERIFY_PREREQUISITES, mutated=False,
                result_fn=result, release=release,
            )

        # Baseline verification (C09 rule 4: the prior version is observed,
        # never assumed). The deployed Compose bytes identify the deployed
        # revision deterministically (both plans share the frozen path).
        hash_out = self._read(
            request,
            display="read managed compose file hash",
            argv=("sha256sum", "--", plan.compose_path),
        )
        if hash_out is None:
            return result(
                status=STATUS_OUTCOME_UNKNOWN,
                effect=EFFECT_UNKNOWN,
                reason=UpdateErrorCode.OUTCOME_UNKNOWN.value,
            )
        parts = hash_out.stdout.split()
        observed_hash = (
            parts[0]
            if hash_out.status is CommandStatus.SUCCEEDED and parts
            else None
        )
        if observed_hash is None:
            release()
            return self._refused(
                request, plan, UpdateErrorCode.NOT_INSTALLED,
                notes=("no managed compose document on the target",),
            )
        if (
            observed_hash == plan.compose_sha256
            and observed_hash != baseline.compose_sha256
        ):
            # The deployed bytes already ARE the requested update: the
            # prior deployment cannot be verified from here (an interrupted
            # earlier attempt). Reconciliation is explicit — never a silent
            # converge that would record an unverified prior version.
            release()
            return self._refused(
                request, plan, UpdateErrorCode.STATE_DIVERGED,
                notes=(
                    "deployed compose matches the requested update plan; "
                    "explicit reconciliation required before re-update "
                    "(reinstall converges the deployment, then re-request)",
                ),
            )
        if observed_hash != baseline.compose_sha256:
            release()
            return self._refused(
                request, plan, UpdateErrorCode.STATE_DIVERGED,
                notes=(
                    "deployed compose matches neither the declared baseline "
                    "nor the requested plan",
                ),
            )

        running_out = self._read(
            request,
            display="read managed compose project state",
            argv=_compose_argv(plan, "ps", "-q"),
        )
        if running_out is None:
            return result(
                status=STATUS_OUTCOME_UNKNOWN,
                effect=EFFECT_UNKNOWN,
                reason=UpdateErrorCode.OUTCOME_UNKNOWN.value,
            )
        project_running = running_out.status is CommandStatus.SUCCEEDED and bool(
            running_out.stdout.strip()
        )
        if not project_running:
            release()
            return self._refused(
                request, plan, UpdateErrorCode.NOT_INSTALLED,
                notes=("the declared baseline deployment is not running",),
            )
        # The baseline must be HEALTHY before the update: a regression is
        # only attributable against a verified healthy prior state. Its
        # verification probes are pre-flight reads; receipts stay off the
        # apply receipt flow (the result records ``prior_healthy``).
        baseline_receipts: list[AppliedOperation] = []
        baseline_healthy = self._verify_health(
            request, baseline, baseline_receipts, notes
        )
        if baseline_healthy is None:
            return result(
                status=STATUS_OUTCOME_UNKNOWN,
                effect=EFFECT_UNKNOWN,
                reason=UpdateErrorCode.OUTCOME_UNKNOWN.value,
            )
        if not baseline_healthy:
            release()
            return self._refused(
                request, plan, UpdateErrorCode.PRIOR_STATE_UNHEALTHY,
                notes=(
                    "the declared baseline deployment fails its declared "
                    "health verification; update blocked",
                ),
            )
        notes.append("baseline deployment verified healthy (prior version)")

        # Backup gate: existing volume data needs the explicit operator
        # confirmation (C09 rule 4/5 discipline applied at update time).
        if request.recipe.backup.required and request.recipe.volumes:
            data_present = self._volume_data_present(request, plan)
            if data_present is None:
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=UpdateErrorCode.OUTCOME_UNKNOWN.value,
                )
            if data_present and not request.backup_confirmed:
                release()
                return self._refused(
                    request, plan, UpdateErrorCode.BACKUP_REQUIRED
                )

        # Credentials resolve BEFORE the approval is spent: a missing
        # reference must not consume the one-use decision.
        credential_env: bytes | None = None
        if plan.credentials_path is not None:
            try:
                credential_env = self._render_credentials(request.recipe)
            except EnvFileError:
                release()
                return result(
                    status=STATUS_REFUSED,
                    effect=EFFECT_NOT_ATTEMPTED,
                    reason=UpdateErrorCode.CREDENTIAL_UNAVAILABLE.value,
                )

        # ---- pre-effect recheck + approval consumption (C03 rule 3/4) ----
        recheck = self._decide(request)
        if not recheck.allowed:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=f"{UpdateErrorCode.POLICY_DENIED.value}:"
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
                    f"{UpdateErrorCode.APPROVAL_REJECTED.value}:{error.code.value}"
                ),
                approval_id=request.approval_id,
            )

        # ---- the apply: snapshot, then plan operations in plan order ----
        with _LeaseHeartbeat(self._locks, lease) as heartbeat:
            mutated = False  # the deployed compose bytes were rewritten
            started = False  # a start-class effect happened (pull onward)
            snapshot_taken = False
            for op in plan.operations:
                if heartbeat.lapsed:
                    return result(
                        status=STATUS_OUTCOME_UNKNOWN,
                        effect=EFFECT_UNKNOWN,
                        reason=UpdateErrorCode.OUTCOME_UNKNOWN.value,
                        notes=("resource lease lapsed mid-apply",),
                        prior_image=baseline.image,
                        prior_healthy=True,
                        approval_id=approval.approval_id,
                    )
                if op.op == OP_VERIFY_PREREQUISITES:
                    continue  # applied during pre-flight
                if op.op == OP_WAIT_HEALTHY:
                    continue  # executed as the completion gate below
                if (
                    op.op in (OP_REQUIRE_BACKUP, OP_CREATE_DIRECTORIES)
                    and plan.volumes
                    and not snapshot_taken
                ):
                    # Executor-owned pre-update snapshot BEFORE the first
                    # mutating operation and before the backup-gate receipt
                    # (the gate proves the operator's decision; the snapshot
                    # is the data-protection step it authorizes).
                    snapshot_taken = True
                    snap_ok = self._snapshot_volumes(
                        request, plan, operations, snapshot_paths, notes
                    )
                    if snap_ok is None:
                        return result(
                            status=STATUS_OUTCOME_UNKNOWN,
                            effect=EFFECT_UNKNOWN,
                            reason=UpdateErrorCode.OUTCOME_UNKNOWN.value,
                            prior_image=baseline.image,
                            prior_healthy=True,
                            approval_id=approval.approval_id,
                        )
                    if not snap_ok:
                        return self._finish_failed(
                            request, plan, baseline, notes, operations,
                            failed_op="pre_update_snapshot", mutated=False,
                            result_fn=result, approval_id=approval.approval_id,
                            release=release,
                        )
                if op.op == OP_REQUIRE_BACKUP:
                    # Verified during pre-flight (data presence + explicit
                    # confirmation); the receipt records the verified no-op.
                    operations.append(
                        AppliedOperation(
                            op.op, op.subject, _ACTION_VERIFIED,
                            CommandStatus.SUCCEEDED, 0,
                        )
                    )
                    continue
                receipt = self._apply_operation(
                    request, plan, op, credential_env, operations
                )
                if op.op == OP_WRITE_COMPOSE and receipt is not None:
                    mutated = True
                if op.op in (OP_PULL_IMAGE, OP_START):
                    started = True
                if receipt is None:
                    continue  # verified-present; receipt already recorded
                if receipt.status is CommandStatus.FAILED:
                    return self._finish_failed(
                        request, plan, baseline, notes, operations,
                        failed_op=op.op, mutated=mutated, started=started,
                        result_fn=result, approval_id=approval.approval_id,
                        release=release,
                    )
                if receipt.status is not CommandStatus.SUCCEEDED:
                    return result(
                        status=STATUS_OUTCOME_UNKNOWN,
                        effect=EFFECT_UNKNOWN,
                        reason=UpdateErrorCode.OUTCOME_UNKNOWN.value,
                        prior_image=baseline.image,
                        prior_healthy=True,
                        approval_id=approval.approval_id,
                    )
            # ---- the health gate ----
            healthy = self._verify_health(request, plan, operations, notes)
            if healthy is None:
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=UpdateErrorCode.OUTCOME_UNKNOWN.value,
                    prior_image=baseline.image,
                    prior_healthy=True,
                    approval_id=approval.approval_id,
                    lock_released=False,
                )
            if not healthy:
                notes.append("health regression after update")
                return self._finish_failed(
                    request, plan, baseline, notes, operations,
                    failed_op=OP_WAIT_HEALTHY, mutated=mutated, started=True,
                    result_fn=result, approval_id=approval.approval_id,
                    release=release,
                )

        # Verified success → canonical manifest/projection (C09).
        release()
        manifest = await self._update_manifest(request, plan, intent_id, notes)
        return result(
            status=STATUS_SUCCEEDED,
            effect=EFFECT_EXECUTED,
            reason=None,
            prior_image=baseline.image,
            prior_healthy=True,
            approval_id=approval.approval_id,
            manifest_revision=None if manifest is None else manifest.revision,
            manifest_sha256=None if manifest is None else manifest.sha256,
            health_verified=True,
        )

    # -- policy ----------------------------------------------------------------

    def _decide(self, request: UpdateRequest) -> PolicyDecision:
        return evaluate(
            context=request.context,
            capability_id=CAPABILITY_ID,
            descriptor=self._descriptor,
            grants=self._grants(),
            policy=self._policy(),
            adopted=self._adopted(request.server_id),
        )

    # -- target-state helpers (verify-then-mutate reads) ------------------------

    def _read(
        self,
        request: UpdateRequest,
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

    def _volume_data_present(
        self, request: UpdateRequest, plan: DeploymentPlan
    ) -> bool | None:
        """True when a planned volume host path already holds data."""
        for volume in plan.volumes:
            outcome = self._read(
                request,
                display="preflight check persistent data location",
                argv=("ls", "-A", "--", volume.host_path),
            )
            if outcome is None:
                return None
            if outcome.status is CommandStatus.SUCCEEDED and outcome.stdout.strip():
                return True
        return False

    def _render_credentials(self, recipe: ApplicationRecipe) -> bytes:
        """Resolve every declared reference from the configured .env source.

        Returns the exact ``credentials.env`` bytes (or raises a value-free
        :class:`EnvFileError`). Values exist only here and in the write
        channel; they never reach a receipt or result field.
        """
        resolver = self._credentials()
        return "".join(
            f"{name}={resolver.resolve(name).value}\n"
            for name in recipe.credentials
        ).encode("utf-8")

    # -- pre-update snapshot -----------------------------------------------------

    def _snapshot_volumes(
        self,
        request: UpdateRequest,
        plan: DeploymentPlan,
        operations: list[AppliedOperation],
        snapshot_paths: list[str],
        notes: list[str],
    ) -> bool | None:
        """Archive every declared volume below the application directory.

        ``True`` ⇒ the snapshot was created and verified; ``False`` ⇒ a
        deterministic snapshot failure (the running service is untouched);
        ``None`` ⇒ the snapshot attempt is itself unknown.
        """
        snap_path = _snapshot_path(plan)
        snap_dir = snap_path.rsplit("/", 1)[0]
        mkdir = self._transport(
            CommandRequest(
                op_id=uuid.uuid4(),
                server_id=request.server_id,
                display="create managed application snapshot directory",
                timeout=_PREFLIGHT_TIMEOUT_SECONDS,
                output_budget=_OUTPUT_BUDGET_BYTES,
                argv=("mkdir", "-p", "--", snap_dir),
            )
        )
        if mkdir.status is not CommandStatus.SUCCEEDED:
            notes.append("pre-update snapshot: snapshot directory unavailable")
            return False
        parent = plan.volumes[0].host_path.rsplit("/", 1)[0]
        names = tuple(v.host_path.rsplit("/", 1)[1] for v in plan.volumes)
        tar = self._transport(
            CommandRequest(
                op_id=uuid.uuid4(),
                server_id=request.server_id,
                display="snapshot persistent data before update",
                timeout=_SNAPSHOT_TIMEOUT_SECONDS,
                output_budget=_OUTPUT_BUDGET_BYTES,
                argv=(
                    "tar",
                    "--create",
                    "--file",
                    snap_path,
                    "--directory",
                    parent,
                    *names,
                ),
            )
        )
        operations.append(
            AppliedOperation(
                "pre_update_snapshot",
                ",".join(names),
                _ACTION_APPLIED,
                tar.status,
                tar.exit_code,
            )
        )
        if tar.status is CommandStatus.OUTCOME_UNKNOWN or tar.exit_code is None:
            return None
        if tar.status is not CommandStatus.SUCCEEDED:
            return False
        # Verify-then-trust: the archive must exist and be non-empty.
        verify = self._read(
            request,
            display="verify pre-update snapshot exists",
            argv=("test", "-s", snap_path),
        )
        if verify is None:
            return None
        if verify.status is not CommandStatus.SUCCEEDED:
            notes.append("pre-update snapshot could not be verified")
            return False
        snapshot_paths.append(snap_path)
        notes.append(f"pre-update snapshot recorded: {snap_path}")
        return True

    # -- plan operation handlers -------------------------------------------------

    def _apply_operation(
        self,
        request: UpdateRequest,
        plan: DeploymentPlan,
        op: PlanOperation,
        credential_env: bytes | None,
        operations: list[AppliedOperation],
    ) -> AppliedOperation | None:
        """Apply one plan operation verify-first; never invents a status.

        Returns the receipt when the caller must inspect it (terminal
        classification), ``None`` when the receipt was recorded as a
        verified no-op.
        """
        if op.op == OP_CREATE_DIRECTORIES:
            return self._op_command(
                request, plan, op,
                argv=("mkdir", "-p", "--", *plan.directories),
                timeout=_PREFLIGHT_TIMEOUT_SECONDS,
                display="create managed application directories",
                operations=operations,
            )
        if op.op == OP_WRITE_COMPOSE:
            return self._op_write_compose(request, plan, op, operations)
        if op.op == OP_WRITE_CREDENTIALS:
            return self._op_write_credentials(
                request, plan, op, credential_env, operations
            )
        if op.op == OP_PULL_IMAGE:
            return self._op_command(
                request, plan, op,
                argv=_compose_argv(plan, "pull", "--quiet"),
                timeout=_PULL_TIMEOUT_SECONDS,
                display="pull pinned application image",
                operations=operations,
            )
        if op.op == OP_START:
            return self._op_command(
                request, plan, op,
                argv=_compose_argv(plan, "up", "-d"),
                timeout=_UP_TIMEOUT_SECONDS,
                display="start managed compose project",
                operations=operations,
            )
        raise UpdateError(
            UpdateErrorCode.INVALID_PLAN,
            "plan operation outside the frozen vocabulary reached the apply loop",
        )

    def _file_hash(self, request: UpdateRequest, path: str) -> str | None:
        """Observed target file hash, ``None`` when absent/unreadable."""
        outcome = self._read(
            request,
            display="read managed application file hash",
            argv=("sha256sum", "--", path),
        )
        if outcome is None or outcome.status is not CommandStatus.SUCCEEDED:
            return None
        parts = outcome.stdout.split()
        return parts[0] if parts else None

    def _op_write_compose(
        self,
        request: UpdateRequest,
        plan: DeploymentPlan,
        op: PlanOperation,
        operations: list[AppliedOperation],
    ) -> AppliedOperation | None:
        if self._file_hash(request, plan.compose_path) == plan.compose_sha256:
            operations.append(
                AppliedOperation(
                    op.op, op.subject, _ACTION_VERIFIED,
                    CommandStatus.SUCCEEDED, 0,
                )
            )
            return None
        outcome = self._transport(
            _write_file_command(
                request.server_id, plan.compose_path, plan.compose_text.encode("utf-8")
            )
        )
        receipt = AppliedOperation(
            op.op, op.subject, _ACTION_APPLIED, outcome.status, outcome.exit_code
        )
        if (
            outcome.status is CommandStatus.SUCCEEDED
            and self._file_hash(request, plan.compose_path) != plan.compose_sha256
        ):
            # Verify-then-trust: the write claims success but the bytes on
            # the target are not the plan's bytes — a deterministic failure.
            receipt = replace(receipt, status=CommandStatus.FAILED, exit_code=None)
        operations.append(receipt)
        return receipt

    def _op_write_credentials(
        self,
        request: UpdateRequest,
        plan: DeploymentPlan,
        op: PlanOperation,
        credential_env: bytes | None,
        operations: list[AppliedOperation],
    ) -> AppliedOperation | None:
        assert plan.credentials_path is not None  # plan only plans the op then
        if self._file_hash(request, plan.credentials_path) is not None:
            operations.append(
                AppliedOperation(
                    op.op, op.subject, _ACTION_VERIFIED,
                    CommandStatus.SUCCEEDED, 0,
                )
            )
            return None
        if credential_env is None:  # unreachable: resolved before approval
            raise UpdateError(
                UpdateErrorCode.INVALID_REQUEST,
                "credential bytes missing at write time",
            )
        outcome = self._transport(
            _write_file_command(request.server_id, plan.credentials_path, credential_env)
        )
        receipt = AppliedOperation(
            op.op, op.subject, _ACTION_APPLIED, outcome.status, outcome.exit_code
        )
        operations.append(receipt)
        return receipt

    def _op_command(
        self,
        request: UpdateRequest,
        plan: DeploymentPlan,
        op: PlanOperation,
        *,
        argv: tuple[str, ...],
        timeout: float,
        display: str,
        operations: list[AppliedOperation],
    ) -> AppliedOperation | None:
        outcome = self._transport(
            CommandRequest(
                op_id=uuid.uuid4(),
                server_id=request.server_id,
                display=display,
                timeout=timeout,
                output_budget=_OUTPUT_BUDGET_BYTES,
                argv=argv,
            )
        )
        receipt = AppliedOperation(
            op.op, op.subject, _ACTION_APPLIED, outcome.status, outcome.exit_code
        )
        operations.append(receipt)
        return receipt

    # -- health gate -----------------------------------------------------------

    def _verify_health(
        self,
        request: UpdateRequest,
        plan: DeploymentPlan,
        operations: list[AppliedOperation],
        notes: list[str],
    ) -> bool | None:
        """Real verification: every service healthy within the declared
        budget; plus the published-port loopback probe when verifiable.

        Returns ``None`` when a verification read is itself unknown.
        """
        probe_spec = plan.health_probe
        assert probe_spec is not None  # prelock refused undeclared health
        budget = (
            probe_spec.start_period_seconds
            + probe_spec.retries * probe_spec.interval_seconds
            + 60.0
        )
        deadline = monotonic() + budget
        healthy = False
        while True:
            outcome = self._read(
                request,
                display="verify managed compose project health",
                argv=_compose_argv(plan, "ps", "--format", "json"),
            )
            if outcome is None:
                return None
            if outcome.status is CommandStatus.SUCCEEDED:
                verdict = _compose_health_verdict(outcome.stdout)
                if verdict is None:
                    notes.append("compose state output was not parseable")
                    return False
                if verdict:
                    healthy = True
                    break
            if monotonic() >= deadline:
                break
            sleep(_HEALTH_POLL_INTERVAL_SECONDS)
        if not healthy:
            return False

        # Access verification from the host network for the published
        # health port (schema guarantees it is published: health_port_unbound).
        host_port = _published_health_port(plan)
        assert host_port is not None  # schema-guaranteed
        probe_url = f"http://127.0.0.1:{host_port}{probe_spec.path}"
        outcome = self._read(
            request,
            display="verify published port responds",
            argv=(
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code}",
                probe_url,
            ),
        )
        if outcome is None:
            return None
        if outcome.exit_code == 127:
            notes.append("curl is not installed; published-port probe skipped")
        elif not (
            outcome.status is CommandStatus.SUCCEEDED
            and outcome.stdout.strip() == str(probe_spec.expected_status)
        ):
            notes.append(
                "published-port probe did not return the expected status"
            )
            return False
        operations.append(
            AppliedOperation(
                OP_WAIT_HEALTHY,
                f"http://127.0.0.1:{probe_spec.port}{probe_spec.path}",
                _ACTION_VERIFIED,
                CommandStatus.SUCCEEDED,
                0,
            )
        )
        return True

    # -- failure / rollback / canonical update --------------------------------

    def _finish_failed(
        self,
        request: UpdateRequest,
        plan: DeploymentPlan,
        baseline: DeploymentPlan,
        notes: list[str],
        operations: list[AppliedOperation],
        *,
        failed_op: str,
        mutated: bool,
        started: bool = False,
        result_fn: Callable[..., UpdateResult],
        approval_id: uuid.UUID | None = None,
        release: Callable[[], None] = lambda: None,
    ) -> UpdateResult:
        """Deterministic failure: verified baseline restore within the
        recipe's declared recovery limits, never beyond them.

        - The restore runs when the deployed compose bytes were rewritten
          (mutated) or the service was restarted (started) AND the recipe
          declares image rollback; the restore re-deploys the PRIOR pinned
          digest (already on the target; no registry pull) and must pass
          the baseline health gate to claim ``rollback_verified``.
        - ``image_rollback: false`` recipes surface the failure with the
          plan's own limitation — blocked recovery, nothing restored.
        - Irreversible migrations keep the data-rollback limitation on the
          receipt (C09 rule 4: no universal rollback claim).
        """
        if request.recipe.update.migrations is UpdateMigrations.IRREVERSIBLE:
            notes.append(
                "declared update migrations may be irreversible; "
                "data rollback is not possible"
            )
        if not (mutated or started):
            release()
            return result_fn(
                status=STATUS_FAILED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=UpdateErrorCode.APPLY_FAILED.value,
                prior_image=baseline.image,
                prior_healthy=True,
                approval_id=approval_id,
                health_verified=False,
            )
        if not request.recipe.recovery.image_rollback:
            notes.append(
                "recovery: no image rollback; recovery requires a new "
                "recipe revision"
            )
            release()
            return result_fn(
                status=STATUS_FAILED,
                effect=EFFECT_EXECUTED if started else EFFECT_NOT_ATTEMPTED,
                reason=UpdateErrorCode.APPLY_FAILED.value,
                prior_image=baseline.image,
                prior_healthy=True,
                approval_id=approval_id,
                health_verified=False,
                rollback_attempted=False,
                rollback_verified=False,
            )
        notes.append(f"rolling back to the prior pinned digest after: {failed_op}")
        restore = self._transport(
            _write_file_command(
                request.server_id,
                baseline.compose_path,
                baseline.compose_text.encode("utf-8"),
            )
        )
        rollback_verified = (
            restore.status is CommandStatus.SUCCEEDED
            and self._file_hash(request, baseline.compose_path)
            == baseline.compose_sha256
        )
        if not rollback_verified:
            notes.append("baseline compose restore could not be verified")
        else:
            up = self._transport(
                CommandRequest(
                    op_id=uuid.uuid4(),
                    server_id=request.server_id,
                    display="roll back managed compose project",
                    timeout=_UP_TIMEOUT_SECONDS,
                    output_budget=_OUTPUT_BUDGET_BYTES,
                    argv=_compose_argv(baseline, "up", "-d"),
                )
            )
            if up.status is CommandStatus.OUTCOME_UNKNOWN:
                return result_fn(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=UpdateErrorCode.OUTCOME_UNKNOWN.value,
                    prior_image=baseline.image,
                    prior_healthy=True,
                    approval_id=approval_id,
                    rollback_attempted=True,
                    rollback_verified=False,
                    health_verified=False,
                    lock_released=False,
                )
            if up.status is not CommandStatus.SUCCEEDED:
                rollback_verified = False
                notes.append("baseline redeployment failed")
            else:
                verdict = self._verify_health(
                    request, baseline, operations, notes
                )
                if verdict is None:
                    return result_fn(
                        status=STATUS_OUTCOME_UNKNOWN,
                        effect=EFFECT_UNKNOWN,
                        reason=UpdateErrorCode.OUTCOME_UNKNOWN.value,
                        prior_image=baseline.image,
                        prior_healthy=True,
                        approval_id=approval_id,
                        rollback_attempted=True,
                        rollback_verified=False,
                        health_verified=False,
                        lock_released=False,
                    )
                rollback_verified = verdict
                if not verdict:
                    notes.append("rollback could not be verified")
        release()
        return result_fn(
            status=STATUS_ROLLED_BACK if rollback_verified else STATUS_FAILED,
            effect=EFFECT_EXECUTED,
            reason=UpdateErrorCode.APPLY_FAILED.value,
            prior_image=baseline.image,
            prior_healthy=True,
            approval_id=approval_id,
            rollback_attempted=True,
            rollback_verified=rollback_verified,
            health_verified=False,
        )

    async def _update_manifest(
        self,
        request: UpdateRequest,
        plan: DeploymentPlan,
        intent_id: uuid.UUID | None,
        notes: list[str],
    ) -> object | None:
        """Canonical manifest + projection via the adoption pipeline.

        Deterministic intent id ⇒ an interrupted canonical update converges
        on retry instead of duplicating (C02 rule 5). A refusal here is a
        recorded note — the verified deployment is not destroyed for a
        bookkeeping failure.
        """
        sid, aid = request.server_id, request.application_id
        intent = intent_id or update_intent_id(sid, aid, plan.plan_sha256)
        compose_dir = plan.compose_path.rsplit("/", 1)[0]
        for attempt in (0, 1):
            existing = await self._adoption.read(sid, aid)
            document = AdoptionRequest(
                server_id=sid,
                application_id=aid,
                name=request.recipe.name,
                origin=ApplicationOrigin.COMPOSE,
                compose_project=plan.project_name,
                working_dir=compose_dir,
                expected_revision=0 if existing is None else existing.revision,
            )
            try:
                outcome = await self._adoption.adopt(
                    self._store, document, intent_id=intent
                )
            except AdoptionError as error:
                if attempt == 0 and error.code in _RECOVERABLE_ADOPTION_CODES:
                    # One recovery sweep for an interrupted canonical pair
                    # (C02 W1/W2), then one retry; never a silent heal.
                    await self._adoption.recover(self._store, server_id=sid)
                    continue
                notes.append(f"manifest update refused: {error.code.value}")
                return None
            return _StoredRevision(
                revision=outcome.revision, sha256=outcome.sha256
            )
        return None
