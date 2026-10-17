"""Canonical per-application operational documentation (card 08-app-docs).

Connects lifecycle verification (adoption, install, update, removal) to
the canonical document store's per-application docs/runbooks layout
(contracts.md **C02 rule 2**) under the C07/C09 provenance and
verification boundaries:

- **C07 rule 5** — generated docs separate outcomes from hypotheses.
  Only a VERIFIED lifecycle success may write the authoritative
  operational runbook (version/paths/health/recovery) in the
  ``runbooks`` collection; a failed or outcome-uncertain receipt records
  an INCIDENT document in ``docs`` and NEVER overwrites the last
  verified state as a new success. A refusal (``not_attempted``) is not
  a documentation event — nothing happened, so nothing is fabricated.
- **C07 rule 1/2** — every document carries full provenance
  (``source_kind=generated``, origin bound to the exact verified plan
  hash, ``verification`` status) and every canonical write/delete emits
  the store's durable reindex intent: the queue the chunk indexer
  consumes. A verified removal with a reconciled canonical tombstone
  retires the application's documents so stale knowledge (and its
  index chunks) disappears.
- **C09 rules 2/4/5** — the runbook renders the recipe's declared
  semantics (health verification, migration class, image-rollback
  recovery limits, removal data policy) next to what the receipt
  actually verified: recorded limits, never guessed recovery.
- **C02 rule 3/W4** — documents live at UUID-confined store paths keyed
  by the opaque identity; a recreate after a tombstone goes through the
  store's exact-tombstone-revision path, never a silent overwrite.

Renderers are pure, deterministic functions of settled receipts (no
clock, environment or target access), so a retried recorder converges
on byte-identical content and the store's revision check stays the only
concurrency guard. Composition is constructor-explicit (dispatch
pattern); the optional ``reconcile`` hook is the reviewed indexer seam
(07-chunk-index). Without it the durable intents stay pending — honest,
the next reconcile converges — never a fabricated "reindexed" claim.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from mai.applications.adopt import AdoptedApplication
from mai.applications.install import (
    EFFECT_EXECUTED,
    EFFECT_NOT_ATTEMPTED,
    EFFECT_UNKNOWN,
    STATUS_REFUSED,
    STATUS_SUCCEEDED,
    InstallResult,
)
from mai.applications.recipes import (
    ApplicationRecipe,
    DeploymentPlan,
    TargetEnvironment,
    build_deployment_plan,
)
from mai.applications.remove import RemoveResult
from mai.applications.update import UpdateResult
from mai.knowledge.documents import (
    ABSENT_REVISION,
    DocumentNamespace,
    DocumentStore,
    DocumentStoreError,
    DocumentStoreErrorCode,
    ReindexIntent,
    SourceKind,
    StoredDocument,
    VerificationStatus,
)

__all__ = [
    "DOC_ID_NAMESPACE",
    "AppDocError",
    "AppDocErrorCode",
    "AppDocOutcome",
    "ApplicationDocService",
    "DocEvent",
    "LifecyclePhase",
    "ReconcileFn",
    "RetiredDocument",
    "RunbookFacts",
    "incident_doc_id",
    "record_doc_id",
    "render_incident",
    "render_record",
    "render_removed_runbook",
    "render_runbook",
    "state_doc_id",
]

#: Deterministic document-id namespace: the same lifecycle truth always
#: maps to the SAME canonical document (idempotent retries converge on
#: content; the store's revision check guards concurrent writers).
DOC_ID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "mai:applications:doc-namespace:v1"
)

GENERATOR_LABEL = "mai.applications.document"
RENDER_VERSION = 1

#: Bounded rendering: receipts can grow, canonical documents must not
#: (the store enforces the byte cap; these bounds keep renders
#: deterministic and far below it).
MAX_OPERATION_LINES = 32
MAX_NOTE_LINES = 16
MAX_LIST_ITEMS = 16

#: The optional reconcile seam: consumes the scope's durable intent
#: outbox (the reviewed chunk indexer) and returns per-intent outcomes.
type ReconcileFn = Callable[[], Awaitable[Sequence[object]]]


class LifecyclePhase(StrEnum):
    """The lifecycle operation a receipt documents (C09 rule 2)."""

    ADOPT = "adopt"
    INSTALL = "install"
    UPDATE = "update"
    REMOVE = "remove"


class DocEvent(StrEnum):
    """What one receipt meant for the canonical documentation."""

    RECORD = "record"                  # adoption identity record written
    RUNBOOK = "runbook"                # verified operational runbook written
    INCIDENT = "incident"              # failed/uncertain receipt recorded
    RETIRED = "retired"                # verified removal retired the docs
    NOT_DOCUMENTED = "not_documented"  # refusal / nothing to document


class AppDocErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_REQUEST = "invalid_request"
    INVALID_RECEIPT = "invalid_receipt"
    PLAN_MISMATCH = "plan_mismatch"
    STALE_RECORD = "stale_record"


class AppDocError(ValueError):
    """One structured, value-free documentation rejection."""

    def __init__(self, code: AppDocErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RetiredDocument:
    """One application document retired by a verified removal."""

    doc_id: uuid.UUID
    namespace: DocumentNamespace
    #: The durable tombstone intent (delete reindex queued; C02 W4: it
    #: stays until the indexer acknowledges).
    intent: ReindexIntent


@dataclass(frozen=True, slots=True)
class AppDocOutcome:
    """The honest outcome of documenting one lifecycle receipt.

    ``document`` is the canonical document written (record, runbook or
    incident); ``retired`` lists documents removed by a verified
    removal. ``reindex`` is ``None`` when no reconcile hook is wired —
    the durable intent is still queued, only the immediate indexing is
    absent — and the hook's outcomes otherwise.
    """

    event: DocEvent
    phase: LifecyclePhase
    verification: VerificationStatus | None
    document: StoredDocument | None
    retired: tuple[RetiredDocument, ...] = ()
    reindex: tuple[object, ...] | None = None
    reason: str | None = None

    @property
    def documented(self) -> bool:
        return self.event is not DocEvent.NOT_DOCUMENTED

    @property
    def reindex_queued(self) -> bool:
        """A canonical write/delete always emits its durable intent."""
        return self.event is not DocEvent.NOT_DOCUMENTED


def state_doc_id(server_id: uuid.UUID, application_id: uuid.UUID) -> uuid.UUID:
    """The one canonical operational runbook per application per server."""
    return uuid.uuid5(
        DOC_ID_NAMESPACE, f"state:{server_id}:{application_id}"
    )


def record_doc_id(server_id: uuid.UUID, application_id: uuid.UUID) -> uuid.UUID:
    """The one canonical adoption identity record per application per server."""
    return uuid.uuid5(
        DOC_ID_NAMESPACE, f"record:{server_id}:{application_id}"
    )


def incident_doc_id(
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    phase: LifecyclePhase,
    plan_sha256: str,
    status: str,
) -> uuid.UUID:
    """One incident document per (identity, phase, plan, outcome class).

    Repeated failures of the SAME plan with the same status converge on
    the same document (revision-bumped, latest attempt wins); a
    different outcome class is a separate record. The per-run audit
    trail stays in the job engine (C05); docs are the knowledge surface.
    """
    return uuid.uuid5(
        DOC_ID_NAMESPACE,
        f"incident:{server_id}:{application_id}:{phase.value}:{plan_sha256}:{status}",
    )


# ---------------------------------------------------------------------------
# Pure deterministic renderers (identical inputs -> byte-identical docs)
# ---------------------------------------------------------------------------


def _footer() -> list[str]:
    return [
        "---",
        f"generated_by: {GENERATOR_LABEL} v{RENDER_VERSION}"
        " (deterministic receipt-backed render; content changes only"
        " with new settled lifecycle receipts)",
    ]


def _bounded(values: Sequence[str], limit: int = MAX_LIST_ITEMS) -> str:
    shown = list(values[:limit])
    rendered = ", ".join(f"`{value}`" for value in shown)
    if len(values) > limit:
        rendered += f" (+{len(values) - limit} more)"
    return rendered or "none"


def _recipe_migrations(recipe: ApplicationRecipe) -> str:
    """The declared migration class (an install receipt carries none)."""
    return recipe.update.migrations.value


def _receipt_revision(manifest_revision: int | None) -> str | None:
    return None if manifest_revision is None else str(manifest_revision)


@dataclass(frozen=True, slots=True)
class RunbookFacts:
    """The verified deployment facts one runbook render describes.

    Built ONLY from a receipt that passed the verified-success guard
    (status succeeded, effect executed, health verified) plus the exact
    plan that receipt's hash binds — never from prose or predictions.
    """

    phase: LifecyclePhase
    name: str
    plan: DeploymentPlan
    image: str
    prior_image: str | None
    migrations: str | None
    image_rollback: bool | None
    manifest_revision: int | None
    manifest_sha256: str | None
    published_ports: tuple[int, ...]
    rollback_attempted: bool
    rollback_verified: bool
    snapshot_paths: tuple[str, ...]
    notes: tuple[str, ...]


def render_runbook(facts: RunbookFacts) -> str:
    """Render the authoritative operational runbook (verified success)."""
    plan = facts.plan
    lines = [
        f"# Application operational runbook: {facts.name}",
        "",
        "- classification: runbook",
        f"- verification: {VerificationStatus.VERIFIED.value}",
        f"- server_id: `{plan.server_id}`",
        f"- application_id: `{plan.application_id}`",
        f"- recorded phase: {facts.phase.value}",
        f"- image: `{facts.image}`",
    ]
    if facts.prior_image is not None:
        lines.append(f"- prior image: `{facts.prior_image}`")
    lines.append(f"- plan_sha256: `{plan.plan_sha256}`")
    if facts.manifest_revision is not None:
        lines.append(f"- manifest revision: {facts.manifest_revision}")
    if facts.manifest_sha256 is not None:
        lines.append(f"- manifest sha256: `{facts.manifest_sha256}`")
    lines += [
        "",
        "## Paths",
        "",
        f"- compose: `{plan.compose_path}`",
        "- credentials: "
        + (
            f"`{plan.credentials_path}`"
            if plan.credentials_path is not None
            else "none declared"
        ),
        f"- data locations: {_bounded([f'{v.name}: container {v.container_path} host {v.host_path}' for v in plan.volumes])}",
        "",
        "## Health verification",
        "",
    ]
    probe = plan.health_probe
    if probe is not None:
        lines += [
            f"- probe: http port {probe.port} path {probe.path}"
            f" expecting status {probe.expected_status}",
            f"- probe budget: interval {probe.interval_seconds}s,"
            f" timeout {probe.timeout_seconds}s, retries {probe.retries},"
            f" start period {probe.start_period_seconds}s",
        ]
    else:
        lines.append("- probe: none declared (deployment success unverified)")
    ports = (
        ", ".join(str(port) for port in facts.published_ports)
        if facts.published_ports
        else "none observed"
    )
    lines.append(f"- last verified by: {facts.phase.value} receipt"
                 f" {plan.plan_sha256[:12]} (published host ports: {ports})")
    lines += [
        "",
        "## Update and recovery (declared limits, recorded result)",
        "",
        f"- migrations: {facts.migrations if facts.migrations is not None else 'undeclared'}",
        "- image rollback: "
        + (
            "available (previous pinned digest remains deployable)"
            if facts.image_rollback
            else "not available (recovery requires a new recipe revision)"
        ),
        f"- last recovery: rollback attempted"
        f" {'yes' if facts.rollback_attempted else 'no'};"
        f" verified {'yes' if facts.rollback_verified else 'no'}",
        f"- pre-update snapshots: {_bounded(list(facts.snapshot_paths))}",
        "",
        "## Declared limitations",
        "",
    ]
    lines += [f"- {limitation}" for limitation in plan.limitations]
    if facts.notes:
        lines += ["", "## Operation notes", ""]
        lines += [
            f"- {note}" for note in facts.notes[:MAX_NOTE_LINES]
        ]
    lines += ["", *_footer()]
    return "\n".join(lines) + "\n"


def render_removed_runbook(result: RemoveResult) -> str:
    """Render the runbook after a VERIFIED teardown whose canonical
    tombstone did not reconcile (record conflict pending resolution).

    The receipt is settled truth, so the verification is ``verified`` —
    but the content records a removal, never a deployable application.
    """
    lines = [
        "# Application operational record: REMOVED",
        "",
        "- classification: runbook",
        f"- verification: {VerificationStatus.VERIFIED.value}"
        " (settled removal receipt)",
        f"- server_id: `{result.server_id}`",
        f"- application_id: `{result.application_id}`",
        "- recorded phase: remove",
        "- removal: succeeded (project teardown verified absent)",
        "- canonical tombstone: NOT reconciled (manifest tombstone"
        " refused; record conflict awaits operator resolution)",
        f"- declared data intent: {result.data_intent}"
        " (tier-1 removal never deletes persistent data)",
        f"- plan_sha256: `{result.plan_sha256}`",
        "",
        "This application is not deployed; do not operate it from older"
        " revisions of this document.",
        "",
        *_footer(),
    ]
    return "\n".join(lines) + "\n"


def render_record(record: AdoptedApplication) -> str:
    """Render the adoption identity record (no deployment claim)."""
    lines = [
        f"# Application record: {record.name}",
        "",
        "- classification: record",
        f"- verification: {VerificationStatus.UNVERIFIED.value}"
        " (identity record, not a deployment claim)",
        f"- server_id: `{record.server_id}`",
        f"- application_id: `{record.application_id}`",
        f"- origin: {record.origin.value}",
        f"- record revision: {record.revision}",
        f"- record sha256: `{record.sha256}`",
    ]
    for key, value in (
        ("compose project", record.compose_project),
        ("working directory", record.working_dir),
        ("systemd unit", record.unit_name),
        ("external working directory", record.external_working_dir),
    ):
        if value is not None:
            lines.append(f"- {key}: `{value}`")
    for key, values in (
        ("external config files", record.external_config_files),
        ("external environment files", record.external_environment_files),
    ):
        if values:
            lines.append(f"- {key}: {_bounded(list(values))}")
    lines += [
        "",
        "Deployment state is NOT recorded here: the verified operational"
        " runbook (install/update) and lifecycle incidents are separate"
        " canonical documents.",
        "",
        *_footer(),
    ]
    return "\n".join(lines) + "\n"


def _incident_verification(
    *, effect: str, rollback_attempted: bool, rollback_verified: bool
) -> VerificationStatus:
    """C07 rule 5 boundary for incident records.

    A receipt with an UNKNOWN effect — or one whose recorded rollback
    could not be verified — is a hypothesis about the target; anything
    else is settled observation.
    """
    if effect == EFFECT_UNKNOWN:
        return VerificationStatus.HYPOTHESIS
    if rollback_attempted and not rollback_verified:
        return VerificationStatus.HYPOTHESIS
    return VerificationStatus.VERIFIED


def render_incident(
    *,
    phase: LifecyclePhase,
    status: str,
    effect: str,
    verification: VerificationStatus,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    plan_sha256: str,
    reason: str | None,
    approval_id: uuid.UUID | None,
    manifest_revision: int | None,
    data_intent: str | None,
    published_ports: tuple[int, ...],
    rollback_attempted: bool,
    rollback_verified: bool,
    snapshot_paths: tuple[str, ...],
    operations: tuple[object, ...],
    notes: tuple[str, ...],
) -> str:
    """Render one lifecycle incident (failed/uncertain receipt record)."""
    lines = [
        f"# Lifecycle incident: {phase.value} {status}",
        "",
        "- classification: incident",
        f"- verification: {verification.value}",
        f"- phase: {phase.value}",
        f"- receipt status: {status}",
        f"- effect: {effect}",
        f"- server_id: `{server_id}`",
        f"- application_id: `{application_id}`",
        f"- plan_sha256: `{plan_sha256}`",
        f"- reason: {reason if reason is not None else 'no reason recorded'}",
    ]
    if approval_id is not None:
        lines.append(f"- approval_id: `{approval_id}`")
    if manifest_revision is not None:
        lines.append(
            f"- canonical manifest revision: {manifest_revision}"
            " (record reconcile state)"
        )
    if data_intent is not None:
        lines.append(f"- declared data intent: {data_intent}")
    if published_ports:
        lines.append(
            "- observed published ports at failure: "
            + ", ".join(str(port) for port in published_ports)
        )
    lines += [
        f"- recovery: rollback attempted {'yes' if rollback_attempted else 'no'};"
        f" verified {'yes' if rollback_verified else 'no'}",
        f"- pre-update snapshots: {_bounded(list(snapshot_paths))}",
        "",
        "This incident does NOT update the verified operational runbook;"
        " the last verified state remains authoritative until a new"
        " verified receipt supersedes it.",
        "",
    ]
    if operations:
        lines += ["## Operations", ""]
        for operation in operations[:MAX_OPERATION_LINES]:
            lines.append(
                f"- op {operation.op} on {operation.subject}:"
                f" {operation.action} status {operation.status.value}"
                f" exit {operation.exit_code}"
            )
    if notes:
        lines += ["", "## Receipt notes", ""]
        lines += [f"- {note}" for note in notes[:MAX_NOTE_LINES]]
    lines += ["", *_footer()]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The documentation service
# ---------------------------------------------------------------------------


class ApplicationDocService:
    """Receipt-driven maintainer of one application's canonical docs.

    Constructor-checked and fail-closed (dispatch pattern). The service
    owns NO lifecycle effects and NO index effects: it renders settled
    receipts into the canonical store (which emits the durable reindex
    intents) and optionally runs the injected indexer seam. A store
    rejection (stale revision, tombstone conflict, hash mismatch)
    surfaces unchanged — never healed, never swallowed.
    """

    def __init__(
        self,
        store: DocumentStore,
        *,
        reconcile: ReconcileFn | None = None,
    ) -> None:
        if not isinstance(store, DocumentStore):
            raise AppDocError(
                AppDocErrorCode.INVALID_REQUEST,
                "store must be a DocumentStore",
            )
        if reconcile is not None and not callable(reconcile):
            raise AppDocError(
                AppDocErrorCode.INVALID_REQUEST,
                "reconcile must be callable or None",
            )
        self._store = store
        self._reconcile = reconcile

    # -- entries -------------------------------------------------------------

    async def record_adoption(self, record: AdoptedApplication) -> AppDocOutcome:
        """Write the adoption identity record (docs, unverified).

        Refuses a record with a live journal intent (``stale=True``):
        documenting an unreconciled record would fabricate canonical
        knowledge (C02 W2).
        """
        if not isinstance(record, AdoptedApplication):
            raise AppDocError(
                AppDocErrorCode.INVALID_RECEIPT,
                "record must be an AdoptedApplication projection",
            )
        self._validated_identity(record.server_id, record.application_id)
        if record.stale:
            raise AppDocError(
                AppDocErrorCode.STALE_RECORD,
                "adoption record has a live journal intent; document only"
                " reconciled records (C02 W2)",
            )
        doc = self._upsert_doc(
            record_doc_id(record.server_id, record.application_id),
            render_record(record),
            server_id=record.server_id,
            app_id=record.application_id,
            namespace=DocumentNamespace.DOCS,
            origin=f"adoption:{record.revision}@{record.sha256[:12]}",
            origin_revision=str(record.revision),
            verification=VerificationStatus.UNVERIFIED,
        )
        return await self._outcome(
            DocEvent.RECORD,
            LifecyclePhase.ADOPT,
            VerificationStatus.UNVERIFIED,
            doc,
        )

    async def record_install(
        self,
        *,
        recipe: ApplicationRecipe,
        target: TargetEnvironment,
        result: InstallResult,
    ) -> AppDocOutcome:
        """Document one install receipt (runbook / incident / refusal)."""
        plan = self._bound_plan(recipe, target, result)
        if self._is_verified_success(result.status, result.effect, result.health_verified):
            doc = self._upsert_doc(
                state_doc_id(result.server_id, result.application_id),
                render_runbook(
                    RunbookFacts(
                        phase=LifecyclePhase.INSTALL,
                        name=recipe.name,
                        plan=plan,
                        image=plan.image,
                        prior_image=None,
                        migrations=_recipe_migrations(recipe),
                        image_rollback=recipe.recovery.image_rollback,
                        manifest_revision=result.manifest_revision,
                        manifest_sha256=result.manifest_sha256,
                        published_ports=result.published_ports,
                        rollback_attempted=result.rollback_attempted,
                        rollback_verified=result.rollback_verified,
                        snapshot_paths=(),
                        notes=result.notes,
                    )
                ),
                server_id=result.server_id,
                app_id=result.application_id,
                namespace=DocumentNamespace.RUNBOOKS,
                origin=f"install:{result.plan_sha256}",
                origin_revision=_receipt_revision(result.manifest_revision),
                verification=VerificationStatus.VERIFIED,
            )
            return await self._outcome(
                DocEvent.RUNBOOK, LifecyclePhase.INSTALL, VerificationStatus.VERIFIED, doc
            )
        return await self._failed_or_refused(
            result,
            LifecyclePhase.INSTALL,
            plan_sha256=result.plan_sha256,
            manifest_revision=result.manifest_revision,
            data_intent=None,
            published_ports=result.published_ports,
            rollback_attempted=result.rollback_attempted,
            rollback_verified=result.rollback_verified,
            snapshot_paths=(),
            operations=result.operations,
            notes=result.notes,
        )

    async def record_update(
        self,
        *,
        recipe: ApplicationRecipe,
        target: TargetEnvironment,
        result: UpdateResult,
    ) -> AppDocOutcome:
        """Document one update receipt (runbook refresh / incident)."""
        plan = self._bound_plan(recipe, target, result)
        if self._is_verified_success(result.status, result.effect, result.health_verified):
            doc = self._upsert_doc(
                state_doc_id(result.server_id, result.application_id),
                render_runbook(
                    RunbookFacts(
                        phase=LifecyclePhase.UPDATE,
                        name=recipe.name,
                        plan=plan,
                        image=result.new_image,
                        prior_image=result.prior_image,
                        migrations=result.migrations,
                        image_rollback=result.image_rollback_supported,
                        manifest_revision=result.manifest_revision,
                        manifest_sha256=result.manifest_sha256,
                        published_ports=result.published_ports,
                        rollback_attempted=result.rollback_attempted,
                        rollback_verified=result.rollback_verified,
                        snapshot_paths=result.snapshot_paths,
                        notes=result.notes,
                    )
                ),
                server_id=result.server_id,
                app_id=result.application_id,
                namespace=DocumentNamespace.RUNBOOKS,
                origin=f"update:{result.plan_sha256}",
                origin_revision=_receipt_revision(result.manifest_revision),
                verification=VerificationStatus.VERIFIED,
            )
            return await self._outcome(
                DocEvent.RUNBOOK, LifecyclePhase.UPDATE, VerificationStatus.VERIFIED, doc
            )
        return await self._failed_or_refused(
            result,
            LifecyclePhase.UPDATE,
            plan_sha256=result.plan_sha256,
            manifest_revision=result.manifest_revision,
            data_intent=None,
            published_ports=result.published_ports,
            rollback_attempted=result.rollback_attempted,
            rollback_verified=result.rollback_verified,
            snapshot_paths=result.snapshot_paths,
            operations=result.operations,
            notes=result.notes,
        )

    async def record_remove(self, result: RemoveResult) -> AppDocOutcome:
        """Document one removal receipt.

        A verified removal with a RECONCILED canonical tombstone retires
        the application's runbook and identity record (stale knowledge
        leaves the store and the index via durable delete intents). A
        verified teardown whose tombstone did NOT reconcile rewrites the
        runbook honestly as removed (the record still exists). Failed or
        uncertain removals record incidents; refusals document nothing.
        """
        self._validated_receipt(result, RemoveResult)
        self._validated_identity(result.server_id, result.application_id)
        if result.status == STATUS_SUCCEEDED and result.effect == EFFECT_EXECUTED:
            if result.manifest_removed:
                retired = tuple(
                    retired
                    for retired in (
                        self._retire(
                            state_doc_id(result.server_id, result.application_id),
                            server_id=result.server_id,
                            app_id=result.application_id,
                            namespace=DocumentNamespace.RUNBOOKS,
                        ),
                        self._retire(
                            record_doc_id(result.server_id, result.application_id),
                            server_id=result.server_id,
                            app_id=result.application_id,
                            namespace=DocumentNamespace.DOCS,
                        ),
                    )
                    if retired is not None
                )
                if not retired:
                    return self._not_documented(
                        LifecyclePhase.REMOVE,
                        "verified removal with reconciled tombstone;"
                        " no application documents were stored",
                    )
                return await self._outcome(
                    DocEvent.RETIRED,
                    LifecyclePhase.REMOVE,
                    None,
                    None,
                    retired=retired,
                )
            doc = self._upsert_doc(
                state_doc_id(result.server_id, result.application_id),
                render_removed_runbook(result),
                server_id=result.server_id,
                app_id=result.application_id,
                namespace=DocumentNamespace.RUNBOOKS,
                origin=f"remove:{result.plan_sha256}",
                origin_revision=_receipt_revision(result.manifest_revision),
                verification=VerificationStatus.VERIFIED,
            )
            return await self._outcome(
                DocEvent.RUNBOOK,
                LifecyclePhase.REMOVE,
                VerificationStatus.VERIFIED,
                doc,
                reason="canonical tombstone not reconciled; runbook"
                " rewritten as removed",
            )
        if result.status == STATUS_REFUSED and result.effect == EFFECT_NOT_ATTEMPTED:
            return self._not_documented(
                LifecyclePhase.REMOVE,
                "refusal (not_attempted) is not a documentation event",
            )
        return await self._failed_or_refused(
            result,
            LifecyclePhase.REMOVE,
            plan_sha256=result.plan_sha256,
            manifest_revision=result.manifest_revision,
            data_intent=result.data_intent,
            published_ports=(),
            rollback_attempted=False,
            rollback_verified=False,
            snapshot_paths=(),
            operations=result.operations,
            notes=result.notes,
        )

    # -- classification and render plumbing -----------------------------------

    @staticmethod
    def _is_verified_success(
        status: str, effect: str, health_verified: bool
    ) -> bool:
        """The ONLY path to the authoritative runbook (C07 rule 5).

        Belt and braces: receipts carry settled truth, but the runbook
        requires the full verified-success combination — a
        ``succeeded`` receipt without verified health is recorded as an
        incident, never as a success instruction.
        """
        return (
            status == STATUS_SUCCEEDED
            and effect == EFFECT_EXECUTED
            and health_verified
        )

    def _bound_plan(
        self,
        recipe: ApplicationRecipe,
        target: TargetEnvironment,
        result: InstallResult | UpdateResult,
    ) -> DeploymentPlan:
        """Rebuild the exact plan the receipt's hash binds.

        ``build_deployment_plan`` is pure and deterministic; a hash
        mismatch means the recipe/target drifted from the verified
        operation and the receipt must not be documented against a
        different plan (C03 rule 4 altered-payload boundary).
        """
        self._validated_receipt(result, (InstallResult, UpdateResult))
        self._validated_identity(result.server_id, result.application_id)
        if not isinstance(recipe, ApplicationRecipe):
            raise AppDocError(
                AppDocErrorCode.INVALID_REQUEST,
                "recipe must be a validated ApplicationRecipe"
                " (parse_recipe_text)",
            )
        if not isinstance(target, TargetEnvironment):
            raise AppDocError(
                AppDocErrorCode.INVALID_REQUEST,
                "target must be a TargetEnvironment",
            )
        plan = build_deployment_plan(
            recipe, result.server_id, result.application_id, target
        )
        if plan.plan_sha256 != result.plan_sha256:
            raise AppDocError(
                AppDocErrorCode.PLAN_MISMATCH,
                "receipt plan hash does not match the recipe-derived plan;"
                " documenting a foreign plan is refused",
            )
        return plan

    @staticmethod
    def _validated_receipt(result: object, expected: type | tuple[type, ...]) -> None:
        if not isinstance(result, expected):
            raise AppDocError(
                AppDocErrorCode.INVALID_RECEIPT,
                "receipt must be the lifecycle result dataclass produced by"
                " the matching service",
            )

    @staticmethod
    def _validated_identity(server_id: object, application_id: object) -> None:
        for name, value in (
            ("server_id", server_id),
            ("application_id", application_id),
        ):
            if not isinstance(value, uuid.UUID):
                raise AppDocError(
                    AppDocErrorCode.INVALID_RECEIPT,
                    f"receipt {name} must be a uuid.UUID",
                )

    async def _failed_or_refused(
        self,
        result: InstallResult | UpdateResult | RemoveResult,
        phase: LifecyclePhase,
        *,
        plan_sha256: str,
        manifest_revision: int | None,
        data_intent: str | None,
        published_ports: tuple[int, ...],
        rollback_attempted: bool,
        rollback_verified: bool,
        snapshot_paths: tuple[str, ...],
        operations: tuple[object, ...],
        notes: tuple[str, ...],
    ) -> AppDocOutcome:
        if result.status == STATUS_REFUSED and result.effect == EFFECT_NOT_ATTEMPTED:
            return self._not_documented(
                phase, "refusal (not_attempted) is not a documentation event"
            )
        verification = _incident_verification(
            effect=result.effect,
            rollback_attempted=rollback_attempted,
            rollback_verified=rollback_verified,
        )
        doc = self._upsert_doc(
            incident_doc_id(
                result.server_id,
                result.application_id,
                phase,
                plan_sha256,
                result.status,
            ),
            render_incident(
                phase=phase,
                status=result.status,
                effect=result.effect,
                verification=verification,
                server_id=result.server_id,
                application_id=result.application_id,
                plan_sha256=plan_sha256,
                reason=result.reason,
                approval_id=result.approval_id,
                manifest_revision=manifest_revision,
                data_intent=data_intent,
                published_ports=published_ports,
                rollback_attempted=rollback_attempted,
                rollback_verified=rollback_verified,
                snapshot_paths=snapshot_paths,
                operations=operations,
                notes=notes,
            ),
            server_id=result.server_id,
            app_id=result.application_id,
            namespace=DocumentNamespace.DOCS,
            origin=f"{phase.value}-incident:{plan_sha256}",
            origin_revision=_receipt_revision(manifest_revision),
            verification=verification,
        )
        return await self._outcome(
            DocEvent.INCIDENT, phase, verification, doc
        )

    @staticmethod
    def _not_documented(phase: LifecyclePhase, reason: str) -> AppDocOutcome:
        return AppDocOutcome(
            event=DocEvent.NOT_DOCUMENTED,
            phase=phase,
            verification=None,
            document=None,
            reason=reason,
        )

    # -- canonical store plumbing ---------------------------------------------

    def _tombstone_revision(
        self,
        doc_id: uuid.UUID,
        *,
        server_id: uuid.UUID,
        app_id: uuid.UUID,
        namespace: DocumentNamespace,
    ) -> int:
        """The revision to recreate from after a verified retirement.

        The store allows a recreate only from the EXACT tombstone
        revision (C02 W4); a caller that never saw the tombstone gets
        ``ABSENT_REVISION`` and the store's ``stale_revision`` refusal.
        """
        for intent in self._store.read_intents(
            server_id=server_id, app_id=app_id, namespace=namespace
        ):
            if intent.doc_id == doc_id and intent.tombstone:
                return intent.revision
        return ABSENT_REVISION

    def _upsert_doc(
        self,
        doc_id: uuid.UUID,
        text: str,
        *,
        server_id: uuid.UUID,
        app_id: uuid.UUID,
        namespace: DocumentNamespace,
        origin: str,
        origin_revision: str | None,
        verification: VerificationStatus,
    ) -> StoredDocument:
        try:
            current = self._store.read(
                doc_id, server_id=server_id, app_id=app_id, namespace=namespace
            )
            expected_revision: int = current.revision
        except DocumentStoreError as error:
            if error.code is not DocumentStoreErrorCode.NOT_FOUND:
                raise
            expected_revision = self._tombstone_revision(
                doc_id, server_id=server_id, app_id=app_id, namespace=namespace
            )
        return self._store.write(
            doc_id,
            text,
            server_id=server_id,
            app_id=app_id,
            namespace=namespace,
            expected_revision=expected_revision,
            source_kind=SourceKind.GENERATED,
            origin=origin,
            origin_revision=origin_revision,
            verification=verification,
        )

    def _retire(
        self,
        doc_id: uuid.UUID,
        *,
        server_id: uuid.UUID,
        app_id: uuid.UUID,
        namespace: DocumentNamespace,
    ) -> RetiredDocument | None:
        """Revision-checked retirement with a durable delete intent.

        Nothing stored is an honest no-op (a removal can precede any
        documentation); a live tombstone conflict surfaces unchanged.
        """
        try:
            current = self._store.read(
                doc_id, server_id=server_id, app_id=app_id, namespace=namespace
            )
        except DocumentStoreError as error:
            if error.code is not DocumentStoreErrorCode.NOT_FOUND:
                raise
            return None
        intent = self._store.delete(
            doc_id,
            server_id=server_id,
            app_id=app_id,
            namespace=namespace,
            expected_revision=current.revision,
        )
        return RetiredDocument(
            doc_id=doc_id, namespace=namespace, intent=intent
        )

    async def _outcome(
        self,
        event: DocEvent,
        phase: LifecyclePhase,
        verification: VerificationStatus | None,
        document: StoredDocument | None,
        *,
        retired: tuple[RetiredDocument, ...] = (),
        reason: str | None = None,
    ) -> AppDocOutcome:
        reindex: tuple[object, ...] | None = None
        if self._reconcile is not None:
            reindex = tuple(await self._reconcile())
        return AppDocOutcome(
            event=event,
            phase=phase,
            verification=verification,
            document=document,
            retired=retired,
            reindex=reindex,
            reason=reason,
        )
