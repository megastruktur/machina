"""Durable, approval-gated, resource-locked application installation.

Card ``08-install``; consumer of the integrated P08 producers
``mai.applications.recipes`` (validated recipes + policy-bound plans),
``mai.applications.adopt`` (canonical manifest + SQL projection) and the
P03/P04/P05 seams (``mai.policy`` gate, ``mai.execution`` bounded command
lane, ``mai.jobs.resource_locks`` exclusive leases). Contracts.md:

- **C09 rule 3 (apply only the bound plan)** — the executor takes a
  validated :class:`~mai.applications.recipes.ApplicationRecipe` plus the
  explicit target identities, rebuilds the deterministic
  :class:`~mai.applications.recipes.DeploymentPlan` via
  :func:`~mai.applications.recipes.build_deployment_plan`, and applies ONLY
  the plan's frozen operation vocabulary, in plan order. No code path runs
  a target command outside ``InstallService``'s per-operation handlers; an
  operation outside the vocabulary fails closed (``invalid_plan``) before
  any effect.
- **C03 rules 3–5 (gate)** — the full policy decision is taken and then
  RE-CHECKED immediately before the privileged effect (G2 step 4); the
  one-use approval is consumed atomically bound to
  ``(capability_id, server_id, payload_hash=plan.plan_sha256,
  policy_revision)``. Any denial returns a ``refused`` result with
  ``effect="not_attempted"`` and consumes nothing.
- **C05 rule 6 / LOCK-FENCE** — the whole apply runs under one exclusive
  ``application:<application_id>`` lease on the target ``server_id``;
  a background heartbeat keeps it alive and a LAPSED lease aborts the
  apply immediately (``outcome_unknown``) instead of mutating fenced
  ground. The lease is released only after a VERIFIED terminal outcome
  (succeeded, refused, or failed/rolled_back with the rollback verified);
  an ``outcome_unknown`` apply leaves the lease to lapse into a fence so
  no conflicting worker can start while the prior process may live.
- **C04 rules 5–7 (honest outcomes)** — every operation goes through the
  bounded command lane (:class:`~mai.execution.commands.CommandRequest`);
  a timeout cut or transport death is ``outcome_unknown``: never retried
  silently, never rolled back (the prior process may live — a duplicate
  mutation is exactly what the contract forbids). Resume is
  reconciliation: every mutating handler VERIFIES the current target state
  first and acts only on verified absence, so a re-run after an
  interruption converges without duplicating the Compose project or the
  volume data (acceptance 2).
- **C09 rules 1/5/6 (identity + data + external paths)** — a canonical
  record or file for this ``application_id`` under another server refuses
  (``cross_server_conflict``); an existing record binding the identity to
  a different Compose project (an adopted EXTERNAL deployment) refuses
  (``external_record_conflict``) — install never touches another
  deployment's files. Rollback is ``docker compose down`` WITHOUT volume
  flags: data deletion is unrepresentable here (C09 rule 5).
- **C03 rule 2/6 (credentials)** — credential references resolve from the
  configured ``.env`` source at effect time and are written to the plan's
  target ``credentials.env`` path over the same base64 transport channel
  the supervision wrapper uses (no quoting round-trip can reinterpret the
  bytes). Reference NAMES appear in receipts; resolved values never appear
  in any command display, result field or error message. The rendered
  Compose document carries the ``env_file`` POINTER only (frozen by
  ``recipes.render_compose``), so the container fails closed if the file
  was never provisioned.
- **Completion gate (acceptance 2)** — success REQUIRES observed real
  verification, never an exit-code inference alone: every project service
  must report Docker health ``healthy`` (the recipe's declared argv curl
  probe) within the declared start budget, and when the health port is
  published, a target-side loopback HTTP probe must answer the expected
  status from the host network. Recipes that declare NO health check are
  refused before any effect (``health_not_declared``): this card does not
  complete an install it cannot verify (the plan's own limitation string
  says "deployment success is unverified").
- **C09 canonical update** — after verified health the canonical manifest
  is written through
  :meth:`~mai.applications.adopt.ApplicationAdoption.adopt` (journal +
  file + projection, deterministic intent id from the plan hash) so an
  interrupted canonical update re-converges on retry instead of
  duplicating the record.

Failure policy: a deterministic operation failure after the start effect
triggers a VERIFIED rollback (``compose down`` + empty ``ps``); the
canonical manifest is written only for fully verified success; a manifest
update refusal after a healthy deployment is recorded honestly (note
``manifest update refused: …``) — the deployment is not destroyed for a
bookkeeping failure and the idempotent retry converges.

Pure stdlib + integrated sibling modules; no engine registration here:
composition (session factories, SSH identity resolution, providers) is
the caller's wiring, exactly like
:class:`~mai.capabilities.dispatch.CapabilityDispatcher`.
"""

from __future__ import annotations

import base64
import json
import shlex
import threading
import uuid
from collections.abc import Callable, Mapping
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
    build_deployment_plan,
)
from mai.credentials.env import EnvFileError, EnvResolver
from mai.execution.commands import (
    CommandOutcome,
    CommandRequest,
    CommandRequestError,
    CommandStatus,
    execute_command,
)
from mai.execution.ssh import SshIdentity
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
    "INSTALL_INTENT_NAMESPACE",
    "LOCK_RESOURCE_PREFIX",
    "AppliedOperation",
    "InstallError",
    "InstallErrorCode",
    "InstallRequest",
    "InstallResult",
    "InstallService",
    "install_intent_id",
    "ssh_transport",
]

#: The capability slug this module executes (C03 rule 1 shape). Grants
#: name this id; the descriptor wired into the service must carry it.
CAPABILITY_ID: str = "app.install"

#: Lock resource namespace: one exclusive key per application identity
#: (C05 rule 6: lock keys include ``server_id`` + resource identity).
LOCK_RESOURCE_PREFIX: str = "application:"

#: Deterministic adoption-intent namespace: the same plan re-applied after
#: an interruption derives the SAME intent id, so the canonical update
#: deduplicates instead of duplicating (C02 rule 5 idempotent pipeline).
INSTALL_INTENT_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "mai:install:intent-namespace:v1"
)

#: Default lease budget for one bounded install (commands are individually
#: bounded well below this; the lease covers the whole apply including the
#: health wait). A killed worker stops heartbeating and the lease lapses
#: into a fence — exactly the LOCK-FENCE behavior the contract wants.
DEFAULT_LEASE_TTL_SECONDS: float = 3600.0

# Per-lane bounds (controller-enforced; every remote command carries its
# own timeout so one stuck op can never wedge the whole apply).
_PREFLIGHT_TIMEOUT_SECONDS = 30.0
_DEFAULT_OP_TIMEOUT_SECONDS = 120.0
_PULL_TIMEOUT_SECONDS = 900.0
_UP_TIMEOUT_SECONDS = 600.0
_DOWN_TIMEOUT_SECONDS = 300.0
_OUTPUT_BUDGET_BYTES = 64 * 1024
_HEALTH_POLL_INTERVAL_SECONDS = 2.0

#: The only prerequisite token this card knows how to verify (the recipe
#: vocabulary is strict tokens; anything else is a refusal, never silence).
_KNOWN_PREREQUISITES = frozenset({"docker"})

#: Result status vocabulary (audit receipt of one install attempt).
STATUS_REFUSED = "refused"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_OUTCOME_UNKNOWN = "outcome_unknown"

#: Effect vocabulary (mirrors the dispatch seam: never claim more than
#: was proven).
EFFECT_NOT_ATTEMPTED = "not_attempted"
EFFECT_EXECUTED = "executed"
EFFECT_UNKNOWN = "unknown"

#: Per-operation action vocabulary (verify-then-mutate receipt).
_ACTION_APPLIED = "applied"
_ACTION_VERIFIED = "verified"  # desired state already observed; nothing mutated

#: Adoption refusals a recovery sweep may legitimately clear before ONE
#: retry (W1/W2 interrupted pairs; a live prior intent).
_RECOVERABLE_ADOPTION_CODES = frozenset(
    {
        AdoptionErrorCode.STALE_REVISION,
        AdoptionErrorCode.INTENT_PENDING,
        AdoptionErrorCode.FILE_DIVERGED,
    }
)


class InstallErrorCode(StrEnum):
    """Stable machine-readable refusal/failure codes (C01 vocabulary)."""

    INVALID_REQUEST = "invalid_request"
    INVALID_PLAN = "invalid_plan"
    POLICY_DENIED = "policy_denied"
    APPROVAL_REJECTED = "approval_rejected"
    LOCK_UNAVAILABLE = "lock_unavailable"
    EXTERNAL_RECORD_CONFLICT = "external_record_conflict"
    CROSS_SERVER_CONFLICT = "cross_server_conflict"
    STATE_DIVERGED = "state_diverged"
    PORT_CONFLICT = "port_conflict"
    BACKUP_REQUIRED = "backup_required"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    UNSUPPORTED_PREREQUISITE = "unsupported_prerequisite"
    HEALTH_NOT_DECLARED = "health_not_declared"
    APPLY_FAILED = "apply_failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class InstallError(ValueError):
    """Structured, value-free wiring/input rejection (raised, not returned).

    Only for malformed constructor wiring or request fields — the same
    boundary :class:`~mai.capabilities.dispatch.CapabilityDispatcher`
    draws. Gate denials and execution outcomes are returned as
    :class:`InstallResult` values, never raised.
    """

    def __init__(self, code: InstallErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class AppliedOperation:
    """One plan operation's receipt (value-free subject echoes the plan)."""

    op: str
    subject: str
    action: str
    status: CommandStatus
    exit_code: int | None


@dataclass(frozen=True, slots=True)
class InstallRequest:
    """One validated install intent (identity + recipe + gate handles).

    ``context.server_id`` MUST equal ``server_id`` (C05 rule 3: the run
    binding names the target; a mismatching context is a construction
    rejection, never a silent retarget). ``approval_id`` is the pending
    one-use decision bound to the plan this service rebuilds — a plan
    hash mismatch is the C03 rule 4 altered-payload denial.
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    recipe: ApplicationRecipe
    target: TargetEnvironment
    context: ExecutionContext
    approval_id: uuid.UUID
    #: Confirms the operator took the declared backup for pre-existing
    #: volume data (interrupted-install resume with data on disk). A
    #: fresh install (verified-empty data locations) does not need it.
    backup_confirmed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, uuid.UUID):
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST, "server_id must be a uuid.UUID"
            )
        if not isinstance(self.application_id, uuid.UUID):
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST,
                "application_id must be a uuid.UUID",
            )
        if not isinstance(self.recipe, ApplicationRecipe):
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST,
                "recipe must be a validated ApplicationRecipe (parse_recipe_text)",
            )
        if not isinstance(self.target, TargetEnvironment):
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST,
                "target must be a TargetEnvironment",
            )
        if not isinstance(self.context, ExecutionContext):
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST,
                "context must be an ExecutionContext",
            )
        if self.context.server_id != self.server_id:
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST,
                "context.server_id does not match the request target "
                "(C05 rule 3 run binding)",
            )
        if not isinstance(self.approval_id, uuid.UUID):
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST, "approval_id must be a uuid.UUID"
            )
        if not isinstance(self.backup_confirmed, bool):
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST, "backup_confirmed must be a bool"
            )


@dataclass(frozen=True, slots=True)
class InstallResult:
    """The audit receipt of one install attempt.

    ``status`` + ``effect`` never overclaim: ``refused`` always pairs with
    ``effect="not_attempted"``; ``outcome_unknown`` pairs with
    ``effect="unknown"``; only verified success or a verified rollback
    claims ``effect="executed"``. ``manifest_revision`` is set only when
    the canonical manifest/projection update RECONCILED (C02 rule 4).
    """

    server_id: uuid.UUID
    application_id: uuid.UUID
    plan_sha256: str
    status: str
    effect: str
    reason: str | None
    operations: tuple[AppliedOperation, ...] = ()
    notes: tuple[str, ...] = ()
    approval_id: uuid.UUID | None = None
    manifest_revision: int | None = None
    manifest_sha256: str | None = None
    health_verified: bool = False
    published_ports: tuple[int, ...] = ()
    rollback_attempted: bool = False
    rollback_verified: bool = False
    lock_released: bool = False


def install_intent_id(
    server_id: uuid.UUID, application_id: uuid.UUID, plan_sha256: str
) -> uuid.UUID:
    """Deterministic canonical-update intent id (resume-safe, C02 rule 5)."""
    return uuid.uuid5(
        INSTALL_INTENT_NAMESPACE,
        f"install:{server_id}:{application_id}:{plan_sha256}",
    )


#: One bounded target transport (the P04 bounded command lane; tests
#: substitute a fake target simulator).
type Transport = Callable[[CommandRequest], CommandOutcome]


def ssh_transport(
    identity: SshIdentity,
    *,
    key_path: str,
    known_hosts_path: str,
) -> Transport:
    """Production transport: the pinned-identity bounded SSH command lane.

    Preconditions match :func:`~mai.execution.ssh.probe_environment`: the
    key path comes from ``resolve_identity_key`` and the known-hosts file
    from ``pin_host_key`` (enrollment is an explicit admin action).
    """

    def run(request: CommandRequest) -> CommandOutcome:
        return execute_command(
            identity,
            request,
            key_path=key_path,
            known_hosts_path=known_hosts_path,
        )

    return run


# ---------------------------------------------------------------------------
# Target-side command composition (pure; base64 channel like 04-supervisor)
# ---------------------------------------------------------------------------


def _write_file_command(server_id: uuid.UUID, path: str, data: bytes) -> CommandRequest:
    """Atomically provision one target file via the base64 channel.

    Mirrors the supervision wrapper's transport representation: the bytes
    travel base64-encoded inside ONE composed remote command (no quoting
    round-trip can reinterpret them), land under a restrictive umask and
    are moved into place, then hashed back for verify-then-trust. Used for
    the rendered Compose document AND the provisioned ``credentials.env``.
    The display label is a fixed value-free string.
    """
    directory = path.rsplit("/", 1)[0]
    encoded = base64.b64encode(data).decode("ascii")
    shell = (
        "umask 077 && "
        f"mkdir -p {shlex.quote(directory)} && "
        f"printf %s '{encoded}' | base64 -d > {shlex.quote(path)}.tmp && "
        f"mv {shlex.quote(path)}.tmp {shlex.quote(path)} && "
        f"sha256sum -- {shlex.quote(path)}"
    )
    return CommandRequest(
        op_id=uuid.uuid4(),
        server_id=server_id,
        display="provision managed application file",
        timeout=_DEFAULT_OP_TIMEOUT_SECONDS,
        output_budget=_OUTPUT_BUDGET_BYTES,
        shell=shell,
    )


def _compose_argv(plan: DeploymentPlan, *rest: str) -> tuple[str, ...]:
    return (
        "docker",
        "compose",
        "--project-name",
        plan.project_name,
        "--file",
        plan.compose_path,
        *rest,
    )


def _compose_health_verdict(stdout: str) -> bool | None:
    """True iff every listed service is running AND healthy.

    ``None`` ⇒ unparseable output (never guessed). Accepts every shape
    real Compose emits across versions: the JSON array, the
    line-delimited object stream (N containers), and the single JSON
    object (exactly one container — ``json.loads`` then yields a Mapping,
    observed on the live daemon with compose v5.5); ``Health`` may be a
    plain string or ``{"Status": …}``.
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        entries: object = json.loads(text)
    except json.JSONDecodeError:
        entries = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                return None
    if isinstance(entries, Mapping):
        entries = [entries]
    if not isinstance(entries, list) or not entries:
        return None
    for entry in entries:
        if not isinstance(entry, Mapping):
            return None
        if entry.get("State") != "running":
            return False
        health = entry.get("Health")
        status = health.get("Status") if isinstance(health, Mapping) else health
        if status != "healthy":
            return False
    return True


def _published_health_port(plan: DeploymentPlan) -> int | None:
    """Host port the health port is published on (None ⇒ not published)."""
    probe = plan.health_probe
    assert probe is not None
    for binding in plan.ports:
        if binding.container == probe.port:
            return binding.host
    return None


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
            raise InstallError(
                InstallErrorCode.INVALID_PLAN,
                "plan carries an operation outside the frozen vocabulary",
            )


class _LeaseHeartbeat:
    """Keep the apply's lease alive; expose a lapsed flag (fail closed)."""

    def __init__(self, locks: ResourceLockService, lease: LockLease) -> None:
        self._locks = locks
        self._lease = lease
        self._stop = threading.Event()
        self._lapsed = False
        interval = max(1.0, min(10.0, lease.lease_ttl / 3.0))
        self._thread = threading.Thread(
            target=self._loop, args=(interval,), daemon=True, name="mai-install-hb"
        )

    def _loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                alive = self._locks.heartbeat(self._lease)
            except ResourceLockError:
                alive = False
            if not alive:
                self._lapsed = True
                return

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    @property
    def lapsed(self) -> bool:
        return self._lapsed


@dataclass(frozen=True, slots=True)
class _TargetState:
    """Verified target classification (all values read, never assumed)."""

    compose_present: bool
    compose_hash_matches: bool
    project_running: bool


class InstallService:
    """The enforced install boundary: identity → plan → policy → approval
    → lock → verify-then-mutate apply → health gate → canonical update.

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
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST, "transport must be callable"
            )
        if not isinstance(locks, ResourceLockService):
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST,
                "locks must be a ResourceLockService",
            )
        if not isinstance(approvals, ApprovalStore):
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST,
                "approvals must be an ApprovalStore",
            )
        if not isinstance(adoption, ApplicationAdoption):
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST,
                "adoption must be an ApplicationAdoption",
            )
        if not isinstance(store, ApplicationManifestStore):
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST,
                "store must be an ApplicationManifestStore",
            )
        for name, provider in (
            ("grants", grants),
            ("policy", policy),
            ("adopted", adopted),
            ("credentials", credentials),
        ):
            if not callable(provider):
                raise InstallError(
                    InstallErrorCode.INVALID_REQUEST, f"{name} must be callable"
                )
        if not isinstance(descriptor, CapabilityDescriptor):
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST,
                "descriptor must be a CapabilityDescriptor",
            )
        if descriptor.identifier != CAPABILITY_ID:
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST,
                "descriptor identifier must be app.install",
            )
        if descriptor.effect_class != "privileged":
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST,
                "install is a privileged effect; the descriptor must declare "
                "the privileged effect class",
            )
        if isinstance(lease_ttl, bool) or not isinstance(lease_ttl, (int, float)):
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST, "lease_ttl must be a number"
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

    async def install(
        self,
        request: InstallRequest,
        *,
        intent_id: uuid.UUID | None = None,
    ) -> InstallResult:
        """Apply one validated recipe as its bound plan (see module doc).

        ``intent_id`` defaults to the deterministic plan-derived id so a
        re-run after interruption converges the canonical update instead
        of duplicating it.
        """
        sid, aid = request.server_id, request.application_id
        plan = build_deployment_plan(request.recipe, sid, aid, request.target)
        _validate_plan_vocabulary(plan)
        refusal = await self._prelock_guards(request, plan)
        if refusal is not None:
            return refusal

        try:
            lease = self._locks.acquire(
                str(sid),
                f"{LOCK_RESOURCE_PREFIX}{aid}",
                owner_ref=f"install:{request.context.run_id}",
                ttl=self._lease_ttl,
            )
        except ResourceLockError as error:
            return self._refused(
                request,
                plan,
                InstallErrorCode.LOCK_UNAVAILABLE,
                reason=(
                    f"{InstallErrorCode.LOCK_UNAVAILABLE.value}:{error.code.value}"
                ),
            )
        return await self._gated_apply(request, plan, lease, intent_id)

    # -- gate phases -----------------------------------------------------------

    def _refused(
        self,
        request: InstallRequest,
        plan: DeploymentPlan,
        code: InstallErrorCode,
        *,
        reason: str | None = None,
        notes: tuple[str, ...] = (),
    ) -> InstallResult:
        return InstallResult(
            server_id=request.server_id,
            application_id=request.application_id,
            plan_sha256=plan.plan_sha256,
            status=STATUS_REFUSED,
            effect=EFFECT_NOT_ATTEMPTED,
            reason=reason if reason is not None else code.value,
            notes=notes,
        )

    async def _prelock_guards(
        self, request: InstallRequest, plan: DeploymentPlan
    ) -> InstallResult | None:
        """Identity/projection guards that need no lease and cause no effect."""
        sid, aid = request.server_id, request.application_id
        # Cross-server identity guard (C09 rule 1; mirrors adopt).
        if any(s != sid for s in self._store.servers_storing(aid)):
            return self._refused(
                request, plan, InstallErrorCode.CROSS_SERVER_CONFLICT
            )
        unknown = [
            token
            for token in request.recipe.prerequisites
            if token not in _KNOWN_PREREQUISITES
        ]
        if unknown:
            return self._refused(
                request, plan, InstallErrorCode.UNSUPPORTED_PREREQUISITE
            )
        if request.recipe.health is None:
            # Completion requires real health verification (module doc):
            # refuse before any target contact rather than complete an
            # install the plan itself calls unverified.
            return self._refused(
                request, plan, InstallErrorCode.HEALTH_NOT_DECLARED
            )
        # Canonical store consistency: an interrupted/half pair is surfaced
        # (C02 W2), never healed by an install.
        try:
            stored = self._store.read(sid, aid)
        except AdoptionError as error:
            if error.code is not AdoptionErrorCode.NOT_FOUND:
                return self._refused(
                    request,
                    plan,
                    InstallErrorCode.STATE_DIVERGED,
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
                    request, plan, InstallErrorCode.EXTERNAL_RECORD_CONFLICT
                )
        return None

    async def _gated_apply(
        self,
        request: InstallRequest,
        plan: DeploymentPlan,
        lease: LockLease,
        intent_id: uuid.UUID | None,
    ) -> InstallResult:
        operations: list[AppliedOperation] = []
        notes: list[str] = []
        released = False

        def release() -> None:
            nonlocal released
            self._locks.release(lease)
            released = True

        def result(**kw: object) -> InstallResult:
            base: dict[str, object] = dict(
                server_id=request.server_id,
                application_id=request.application_id,
                plan_sha256=plan.plan_sha256,
                operations=tuple(operations),
                notes=tuple(notes),
                published_ports=tuple(sorted(p.host for p in plan.ports)),
                lock_released=released,
            )
            base.update(kw)
            return InstallResult(**base)  # type: ignore[arg-type]

        # ---- policy gate (first read; re-checked before the effect) ----
        decision = self._decide(request)
        if not decision.allowed:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=f"{InstallErrorCode.POLICY_DENIED.value}:"
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
                request, plan, notes, failed_op=OP_VERIFY_PREREQUISITES,
                started=False, result_fn=result, release=release,
            )

        state = self._discover_state(request, plan)
        if state is None:
            return result(
                status=STATUS_OUTCOME_UNKNOWN,
                effect=EFFECT_UNKNOWN,
                reason=InstallErrorCode.OUTCOME_UNKNOWN.value,
            )
        if not state.project_running:
            ports_ok = self._preflight_ports(request, plan, notes)
            if ports_ok is None:
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=InstallErrorCode.OUTCOME_UNKNOWN.value,
                )
            if ports_ok is False:
                release()
                return result(
                    status=STATUS_REFUSED,
                    effect=EFFECT_NOT_ATTEMPTED,
                    reason=InstallErrorCode.PORT_CONFLICT.value,
                )

        if plan.health_probe is None:  # pragma: no cover - prelock refuses
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=InstallErrorCode.HEALTH_NOT_DECLARED.value,
            )

        if state.compose_present and not state.compose_hash_matches:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=InstallErrorCode.STATE_DIVERGED.value,
                notes=("compose file hash differs from the bound plan",),
            )

        # Backup gate: pre-existing volume data needs the explicit operator
        # confirmation (C09 rule 4/5 discipline applied at install time).
        if request.recipe.backup.required and request.recipe.volumes:
            data_present = self._volume_data_present(request, plan)
            if data_present is None:
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=InstallErrorCode.OUTCOME_UNKNOWN.value,
                )
            if data_present and not request.backup_confirmed:
                release()
                return result(
                    status=STATUS_REFUSED,
                    effect=EFFECT_NOT_ATTEMPTED,
                    reason=InstallErrorCode.BACKUP_REQUIRED.value,
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
                    reason=InstallErrorCode.CREDENTIAL_UNAVAILABLE.value,
                )

        # Already-verified fast path: intact plan + running + healthy ⇒
        # converge the record, mutate nothing (acceptance 2: no duplicate
        # create on re-run).
        if state.compose_present and state.project_running:
            healthy = self._verify_health(request, plan, operations, notes)
            if healthy is None:
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=InstallErrorCode.OUTCOME_UNKNOWN.value,
                )
            if not healthy:
                # A running project that fails its declared verification is
                # torn down (verified rollback) so the retry starts clean.
                return self._finish_failed(
                    request, plan, notes,
                    failed_op=OP_WAIT_HEALTHY, started=True, result_fn=result,
                    release=release,
                )
            release()
            manifest = await self._update_manifest(request, plan, intent_id, notes)
            return result(
                status=STATUS_SUCCEEDED,
                effect=EFFECT_EXECUTED,
                reason=None,
                manifest_revision=None if manifest is None else manifest.revision,
                manifest_sha256=None if manifest is None else manifest.sha256,
                health_verified=True,
            )

        # ---- pre-effect recheck + approval consumption (C03 rule 3/4) ----
        recheck = self._decide(request)
        if not recheck.allowed:
            release()
            return result(
                status=STATUS_REFUSED,
                effect=EFFECT_NOT_ATTEMPTED,
                reason=f"{InstallErrorCode.POLICY_DENIED.value}:"
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
                    f"{InstallErrorCode.APPROVAL_REJECTED.value}:{error.code.value}"
                ),
                approval_id=request.approval_id,
            )

        # ---- the apply: plan operations in plan order, under heartbeat ----
        with _LeaseHeartbeat(self._locks, lease) as heartbeat:
            started = False  # a start-class effect happened (pull onward)
            for op in plan.operations:
                if heartbeat.lapsed:
                    return result(
                        status=STATUS_OUTCOME_UNKNOWN,
                        effect=EFFECT_UNKNOWN,
                        reason=InstallErrorCode.OUTCOME_UNKNOWN.value,
                        notes=("resource lease lapsed mid-apply",),
                        approval_id=approval.approval_id,
                    )
                if op.op == OP_VERIFY_PREREQUISITES:
                    continue  # applied during pre-flight
                if op.op == OP_WAIT_HEALTHY:
                    continue  # executed as the completion gate below
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
                if op.op == OP_START:
                    started = True
                if receipt is None:
                    continue  # verified-present; receipt already recorded
                if receipt.status is CommandStatus.FAILED:
                    return self._finish_failed(
                        request, plan, notes,
                        failed_op=op.op, started=started, result_fn=result,
                        approval_id=approval.approval_id, release=release,
                    )
                if receipt.status is not CommandStatus.SUCCEEDED:
                    return result(
                        status=STATUS_OUTCOME_UNKNOWN,
                        effect=EFFECT_UNKNOWN,
                        reason=InstallErrorCode.OUTCOME_UNKNOWN.value,
                        approval_id=approval.approval_id,
                    )
            # ---- the health gate ----
            healthy = self._verify_health(request, plan, operations, notes)
            if healthy is None:
                return result(
                    status=STATUS_OUTCOME_UNKNOWN,
                    effect=EFFECT_UNKNOWN,
                    reason=InstallErrorCode.OUTCOME_UNKNOWN.value,
                    approval_id=approval.approval_id,
                    lock_released=False,
                )
            if not healthy:
                return self._finish_failed(
                    request, plan, notes,
                    failed_op=OP_WAIT_HEALTHY, started=True, result_fn=result,
                    approval_id=approval.approval_id, release=release,
                )

        # Verified success → canonical manifest/projection (C09).
        release()
        manifest = await self._update_manifest(request, plan, intent_id, notes)
        return result(
            status=STATUS_SUCCEEDED,
            effect=EFFECT_EXECUTED,
            reason=None,
            approval_id=approval.approval_id,
            manifest_revision=None if manifest is None else manifest.revision,
            manifest_sha256=None if manifest is None else manifest.sha256,
            health_verified=True,
        )

    # -- policy ----------------------------------------------------------------

    def _decide(self, request: InstallRequest) -> PolicyDecision:
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
        request: InstallRequest,
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

    def _discover_state(
        self, request: InstallRequest, plan: DeploymentPlan
    ) -> _TargetState | None:
        """Classify the target; ``None`` ⇒ a state read is unknown (C04.5)."""
        hash_out = self._read(
            request,
            display="read managed compose file hash",
            argv=("sha256sum", "--", plan.compose_path),
        )
        if hash_out is None:
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

    def _preflight_ports(
        self,
        request: InstallRequest,
        plan: DeploymentPlan,
        notes: list[str],
    ) -> bool | None:
        """Fresh-install port pre-flight.

        ``True`` ⇒ all planned host ports free (or honestly unverifiable);
        ``False`` ⇒ conflict; ``None`` ⇒ the check itself is unknown.
        """
        outcome = self._read(
            request,
            display="preflight check published ports",
            argv=("ss", "-ltnH"),
        )
        if outcome is None:
            return None
        if outcome.exit_code == 127:
            # Honest absence (C08 rule 6): the check cannot run on this
            # target; `compose up` still fails closed on a real conflict.
            notes.append("port preflight unavailable: ss is not installed")
            return True
        if outcome.status is not CommandStatus.SUCCEEDED:
            return None
        listening: set[int] = set()
        for line in outcome.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4 or ":" not in parts[-2]:
                continue
            _addr, _, port = parts[-2].rpartition(":")
            if port.isdigit():
                listening.add(int(port))
        return not ({p.host for p in plan.ports} & listening)

    def _volume_data_present(
        self, request: InstallRequest, plan: DeploymentPlan
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

    # -- plan operation handlers -------------------------------------------------

    def _apply_operation(
        self,
        request: InstallRequest,
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
            return self._op_mkdir(request, plan, op, operations)
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
        raise InstallError(
            InstallErrorCode.INVALID_PLAN,
            "plan operation outside the frozen vocabulary reached the apply loop",
        )

    def _op_mkdir(
        self,
        request: InstallRequest,
        plan: DeploymentPlan,
        op: PlanOperation,
        operations: list[AppliedOperation],
    ) -> AppliedOperation | None:
        outcome = self._transport(
            CommandRequest(
                op_id=uuid.uuid4(),
                server_id=request.server_id,
                display="create managed application directories",
                timeout=_DEFAULT_OP_TIMEOUT_SECONDS,
                output_budget=_OUTPUT_BUDGET_BYTES,
                argv=("mkdir", "-p", "--", *plan.directories),
            )
        )
        receipt = AppliedOperation(
            op.op, op.subject, _ACTION_APPLIED, outcome.status, outcome.exit_code
        )
        operations.append(receipt)
        return receipt

    def _file_hash(
        self, request: InstallRequest, path: str
    ) -> str | None:
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
        request: InstallRequest,
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
        request: InstallRequest,
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
            raise InstallError(
                InstallErrorCode.INVALID_REQUEST,
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
        request: InstallRequest,
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
        request: InstallRequest,
        plan: DeploymentPlan,
        operations: list[AppliedOperation],
        notes: list[str],
    ) -> bool | None:
        """Real verification: every service healthy within the declared
        budget; plus the published-port loopback probe when verifiable.

        Returns ``None`` when a verification read is itself unknown.
        """
        probe_spec = plan.health_probe
        assert probe_spec is not None  # refused earlier otherwise
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
        # health port. The recipe schema guarantees the health port is one
        # of the declared published container ports (health_port_unbound),
        # so a validated plan always has one. Honest absence: a target
        # without curl records the limitation instead of pretending.
        host_port = _published_health_port(plan)
        assert host_port is not None  # schema-guaranteed (health_port_unbound)
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

    # -- failure / canonical update ------------------------------------------------

    def _finish_failed(
        self,
        request: InstallRequest,
        plan: DeploymentPlan,
        notes: list[str],
        *,
        failed_op: str,
        started: bool,
        result_fn: Callable[..., InstallResult],
        approval_id: uuid.UUID | None = None,
        release: Callable[[], None] = lambda: None,
    ) -> InstallResult:
        """Deterministic failure: verified rollback when an effect started."""
        rolled_back = False
        rollback_verified = False
        if started:
            notes.append(f"rollback after failed operation: {failed_op}")
            self._transport(
                CommandRequest(
                    op_id=uuid.uuid4(),
                    server_id=request.server_id,
                    display="roll back managed compose project",
                    timeout=_DOWN_TIMEOUT_SECONDS,
                    output_budget=_OUTPUT_BUDGET_BYTES,
                    argv=_compose_argv(plan, "down", "--remove-orphans"),
                )
            )
            rolled_back = True
            verify = self._read(
                request,
                display="verify rollback removed project containers",
                argv=_compose_argv(plan, "ps", "-q"),
            )
            rollback_verified = (
                verify is not None
                and not verify.stdout.strip()
                and (
                    verify.status is CommandStatus.SUCCEEDED
                    or verify.exit_code is not None
                )
            )
            if not rollback_verified:
                notes.append("rollback could not be verified")
        release()
        return result_fn(
            status=STATUS_ROLLED_BACK if rolled_back else STATUS_FAILED,
            effect=EFFECT_EXECUTED if started else EFFECT_NOT_ATTEMPTED,
            reason=InstallErrorCode.APPLY_FAILED.value,
            approval_id=approval_id,
            rollback_attempted=rolled_back,
            rollback_verified=rollback_verified,
            health_verified=False,
        )

    async def _update_manifest(
        self,
        request: InstallRequest,
        plan: DeploymentPlan,
        intent_id: uuid.UUID | None,
        notes: list[str],
    ) -> _StoredRevision | None:
        """Canonical manifest + projection via the adoption pipeline.

        Deterministic intent id ⇒ an interrupted canonical update converges
        on retry instead of duplicating (C02 rule 5). A refusal here is a
        recorded note — the verified deployment is not destroyed for a
        bookkeeping failure.
        """
        sid, aid = request.server_id, request.application_id
        intent = intent_id or install_intent_id(sid, aid, plan.plan_sha256)
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


@dataclass(frozen=True, slots=True)
class _StoredRevision:
    """Canonical manifest version triple observed after the update."""

    revision: int | None
    sha256: str | None
