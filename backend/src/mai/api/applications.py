"""Applications HTTP API: discovery, adoption, plans, lifecycle submits (C09).

Ownership map (contracts.md **C09** catalogue/adoption/maintenance, **C08**
UI/API and auth, **C01** envelope/identity/scope; SERVICES ``mai-api`` row:
router owners; producers ``08-discovery``, ``08-adoption``, ``08-recipe-schema``,
``08-install``, ``08-update``, ``08-remove``, ``08-app-docs``; downstream
consumers ``08-web-*`` and the ``08-verify`` gate scenario):

- **Discovery and adoption are listed separately (C09 rule 1, AC 1).**
  ``GET …/discoveries`` returns one live observed inventory through the
  reviewed read-only SSH lanes (:func:`mai.applications.discover.discover_server`)
  — observation never enables execution and mints no ids, grants nothing,
  writes nothing. ``GET ""`` returns the projected adoption records through
  :meth:`mai.applications.adopt.ApplicationAdoption.list_adopted`. The two
  resources never merge: a discovered application appears in the adopted
  list only after the explicit admin adoption below.
- **Adoption/revoke are the only canonical writes in this surface**
  (C09 rules 1/6): ``POST ""`` composes the reviewed
  :class:`~mai.applications.adopt.AdoptionRequest` from the admin decision
  and runs the journaled manifest store + SQL projection pipeline; the
  external deployment paths named in the request are REFERENCED metadata,
  never touched. ``DELETE /{application_id}`` is the non-destructive
  revoke (tombstone first; only the canonical manifest pair is removed).
  A successful adoption also seeds the application's canonical identity
  record through the reviewed ``08-app-docs`` producer
  (:class:`mai.applications.document.ApplicationDocService`); a refused or
  absent doc service is reported honestly on the created record
  (``record_doc.status="pending"``) — the committed adoption is never
  rolled back for a documentation outage, and the upsert converges on the
  next adoption revision.
- **Plans are previews and durable intents, never route effects (AC 1/2).**
  ``POST …/plan`` is the pure C09 rule 3 preview: the deterministic
  policy-bound plan (:func:`mai.applications.recipes.build_deployment_plan`)
  with its exact resources, operations, required grants, limitations and
  plan hash — "will install" is never said. ``POST …/install``,
  ``…/update`` and ``…/remove`` submit that SAME deterministic plan as ONE
  capability step through the EXISTING jobs facade
  (:meth:`mai.api.jobs.submit_run` — the reviewed idempotent submit, C05
  rule 5): HTTP persists intent only, no SSH, no Docker, no capability
  dispatch ever runs inside a request (acceptance 2), and effects stay in
  the worker behind the dispatcher/policy/approval gates. The submitted
  step names the frozen capability id (``app.install``/``app.update``/
  ``app.remove`` — the executor services' own vocabulary), carries the
  plan document (recipe grants included) and binds the step resource to
  the ``application:<application_id>`` lease identity (C05 rule 6).
- **Data-removal intention is explicit (C09 rule 5, AC 2).** The remove
  submit body REQUIRES ``data_intent`` (``keep`` | ``delete``); the field
  has no default, so an absent flag is a 422 — an inferred absent flag can
  never become delete. The decision travels inside the digest-bound step
  payload and never triggers deletion by itself (the executor records it
  on the receipt; policy may delegate, inference never).
- **Policy denial is explicit and bound** (C03 rules 3–5, AC 2): the
  route performs NO policy decision of its own — the durable intent names
  exactly the capability and required grants the worker's G2 gate must
  allow (the plan's ``required_grants`` are part of the digest-bound
  payload and the ``job_runs.requested_grants`` row), and a denial
  surfaces as the executor's structured ``refused`` receipt on the run —
  never as a silent no-op at this boundary.
- **Failure mapping** is the C01 envelope everywhere: producer rejections
  keep their stable codes (``validation_failed`` / ``conflict`` /
  ``not_found``), transport-level discovery refusals surface their class
  (a changed host key is ``409 conflict`` — a security boundary, not a
  retry), and an unusable dependency fails closed with ``503
  unavailable`` — never a guessed success.

Route/doc listeners stay off (G1); the schema is produced in-process via
``app.openapi()`` for the central exporter owned by the ``openapi-client``
resource.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Final, Literal

from fastapi import APIRouter, FastAPI, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.api.auth import (
    BoundaryError,
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
    render_error_envelope,
)
from mai.api.jobs import (
    RunCreated,
    RunSubmitBody,
    StepBody,
    _canonical_profile,
    submit_run,
)
from mai.applications.adopt import (
    ABSENT_REVISION,
    AdoptionError,
    AdoptionErrorCode,
    AdoptionRequest,
    ApplicationAdoption,
    ApplicationManifestStore,
    AdoptedApplication,
)
from mai.applications.discover import (
    ApplicationOrigin,
    ComposeProjectObservation,
    DiscoveryInventory,
    ObservedApplication,
    StandaloneContainerObservation,
    SystemdUnitObservation,
    discover_server,
)
from mai.applications.document import (
    AppDocError,
    ApplicationDocService,
    record_doc_id,
)
from mai.applications.service_map import (
    CATEGORY_MACHINE_ID_RE,
    INSTRUCTION_KEYS,
    MAX_NAME_CHARS as MAP_NAME_CHARS,
    ServiceInstructions,
    ServiceMapEntry,
    ServiceMapError,
    ServiceMapStore,
    classify_service,
    entry_digest,
)
from mai.applications.install import LOCK_RESOURCE_PREFIX
from mai.applications.recipes import (
    DeploymentPlan,
    RecipeError,
    RecipeErrorCode,
    TargetEnvironment,
    build_deployment_plan,
    canonical_plan_json,
    parse_recipe_text,
    render_recipe,
    recipe_sha256,
)
from mai.applications.remove import RemoveDataIntent
from mai.contracts.server import parse_server_profile_text
from mai.credentials.env import EnvFileError
from mai.execution.ssh import (
    SshConnectError,
    SshErrorCode,
    resolve_identity_key,
)
from mai.knowledge.documents import (
    DocumentStore,
    DocumentStoreError,
    DocumentStoreErrorCode,
    DocumentNamespace,
    SourceKind,
    VerificationStatus,
)

__all__ = ["register_applications"]

# ---------------------------------------------------------------------------
# Transport bounds (module-owned policy, explicitly bounded)
# ---------------------------------------------------------------------------

#: List page bounds (C01 rule 5: list endpoints are bounded/paginated).
DEFAULT_PAGE_LIMIT: Final[int] = 50
MAX_PAGE_LIMIT: Final[int] = 200

#: The ``job_runs.model_profile`` value for lifecycle submits. The column is
#: mandatory for every durable run; the ``capability.steps`` handler never
#: touches a model, so the value is the no-model lifecycle lane marker —
#: never a model name.
LIFECYCLE_MODEL_PROFILE: Final[str] = "app-lifecycle"

#: Frozen capability ids (the executor services' own vocabulary).
INSTALL_CAPABILITY: Final[str] = "app.install"
UPDATE_CAPABILITY: Final[str] = "app.update"
REMOVE_CAPABILITY: Final[str] = "app.remove"


# ---------------------------------------------------------------------------
# Wire shapes (transport only; truth is the reviewed producers)
# ---------------------------------------------------------------------------


class ServiceInstructionsView(BaseModel):
    """One service's operational runbook (the five frozen slots)."""

    install: str = ""
    update: str = ""
    uninstall: str = ""
    backup: str = ""
    verify: str = ""


class ServiceInstructionsBody(ServiceInstructionsView):
    """The adoption-side runbook body (strict like every mutation body)."""

    model_config = ConfigDict(extra="forbid")


class ServiceCategoryView(BaseModel):
    """One taxonomy category (built-in or operator-defined)."""

    machine_id: str
    label: str
    description: str
    built_in: bool


class ServiceCategoryBody(BaseModel):
    """One custom category upsert (the confined-store grammar)."""

    model_config = ConfigDict(extra="forbid")

    machine_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)

    @field_validator("machine_id")
    @classmethod
    def _machine_id_slug(cls, value: str) -> str:
        if not CATEGORY_MACHINE_ID_RE.fullmatch(value):
            raise ValueError(
                f"machine_id must match {CATEGORY_MACHINE_ID_RE.pattern}"
            )
        return value


class ApplicationView(BaseModel):
    """One projected adoption record (the facade-typed projection row).

    ``category``/``description``/``instructions`` are the Service Map
    metadata (default ``"system"``/empty for unmapped records; the map
    entry's values for mapped ones — including chat/manual additions
    that have no SQL adoption row at all).
    """

    application_id: str
    server_id: str
    name: str
    origin: str
    compose_project: str | None
    working_dir: str | None
    unit_name: str | None
    external_working_dir: str | None
    external_config_files: list[str]
    external_environment_files: list[str]
    revision: int
    schema_revision: int
    sha256: str
    adopted_at: str
    reconciled_at: str
    stale: bool
    category: str = "system"
    description: str = ""
    instructions: ServiceInstructionsView = Field(
        default_factory=ServiceInstructionsView
    )


class ApplicationPage(BaseModel):
    """One bounded adoption list page for one explicit server (C01 r5/7).

    ``categories`` is the merged taxonomy (8 built-ins + customs) for
    the category picker; the applications list includes manual/standalone
    map entries that have no SQL adoption row (seamless chat additions).
    """

    server_id: str
    applications: list[ApplicationView]
    categories: list[ServiceCategoryView] = Field(default_factory=list)
    limit: int
    offset: int
    total: int


class AdoptBody(BaseModel):
    """Adoption body (strict; the explicit C09 rule 1 admin decision).

    ``application_id`` is the admin-minted opaque identity (C01 rule 1:
    discovery mints no ids, so the decision supplies one).
    ``expected_revision`` follows C01 rule 6: ``0`` creates, otherwise it
    must equal the live record's revision. At least one reference selector
    is required (the producers enforce the full shape).

    The Service Map fields are backwards-compatible enrichments:
    ``category``/``description``/``instructions`` persist map metadata
    for the adopted (or manual-added) service (absent/``None`` keeps the
    ``"system"``/empty defaults); ``upsert_categories`` saves
    operator-defined taxonomy categories in the same request. With
    ``origin="manual"`` the entry is persisted directly in the Service
    Map + canonical document store (outcome ``"reconciled"``) without
    hitting the adoption pipeline's live-target restriction.
    """

    model_config = ConfigDict(extra="forbid")

    application_id: str
    name: str = Field(min_length=1, max_length=MAP_NAME_CHARS)
    origin: ApplicationOrigin | Literal["manual"]
    compose_project: str | None = None
    working_dir: str | None = None
    unit_name: str | None = None
    external_working_dir: str | None = None
    external_config_files: list[str] = Field(default_factory=list)
    external_environment_files: list[str] = Field(default_factory=list)
    expected_revision: int = ABSENT_REVISION
    category: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    instructions: ServiceInstructionsBody = Field(
        default_factory=ServiceInstructionsBody
    )
    upsert_categories: list[ServiceCategoryBody] = Field(default_factory=list)

    @field_validator("category")
    @classmethod
    def _category_slug(cls, value: str | None) -> str | None:
        if value is not None and not CATEGORY_MACHINE_ID_RE.fullmatch(value):
            raise ValueError(
                f"category must match {CATEGORY_MACHINE_ID_RE.pattern}"
            )
        return value


class RecordDocView(BaseModel):
    """The honest documentation outcome of one adoption (C08 rule 6)."""

    status: str  # "documented" | "pending"
    doc_id: str | None = None
    reason: str | None = None


class AdoptionCreated(BaseModel):
    """One committed adoption/revoke-shaped result with its doc outcome."""

    intent_id: str
    application_id: str
    server_id: str
    outcome: str
    revision: int | None
    sha256: str | None
    application: ApplicationView
    record_doc: RecordDocView


class RevokeBody(BaseModel):
    """Revoke body: the C01 rule 6 expected revision (stale writes 409)."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int


class PlanBody(BaseModel):
    """Plan preview body (strict): identity + recipe document + target.

    ``recipe`` is ONE canonical recipe document (a catalogue revision
    stream must be split at its document boundaries by the consumer — the
    schema rejects the whole stream). ``target_os``/``target_arch`` are
    the explicit observed target environment the plan binds.
    """

    model_config = ConfigDict(extra="forbid")

    application_id: str
    recipe: str = Field(min_length=1)
    target_os: str = Field(min_length=1, max_length=64)
    target_arch: str = Field(min_length=1, max_length=64)


class PortBindingView(BaseModel):
    host: int
    container: int
    protocol: str


class PlannedVolumeView(BaseModel):
    name: str
    container_path: str
    host_path: str


class HealthProbeView(BaseModel):
    kind: str
    port: int
    path: str
    expected_status: int
    interval_seconds: int
    timeout_seconds: int
    retries: int
    start_period_seconds: int


class PlanOperationView(BaseModel):
    op: str
    subject: str


class PlanView(BaseModel):
    """The C09 rule 3 preview: actual intended resources and limitations."""

    server_id: str
    application_id: str
    project_name: str
    target_os: str
    target_arch: str
    image: str
    recipe_sha256: str
    compose_path: str
    compose_sha256: str
    compose_text: str
    directories: list[str]
    ports: list[PortBindingView]
    volumes: list[PlannedVolumeView]
    credentials: list[str]
    credentials_path: str | None
    health_probe: HealthProbeView | None
    prerequisites: list[str]
    required_grants: list[str]
    operations: list[PlanOperationView]
    limitations: list[str]
    plan_sha256: str


class LifecycleSubmitBody(BaseModel):
    """Lifecycle submit base (strict): identity is the path; the plan is
    rebuilt deterministically server-side (never accepted from the wire).

    ``run_id`` is the client-generated idempotency key (C05 rule 5).
    ``backup_confirmed`` records the operator's declared-backup decision
    for data-affecting operations (C09 rule 4/5); it is transported in the
    digest-bound step payload, never stored as route state.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    recipe: str = Field(min_length=1)
    target_os: str = Field(min_length=1, max_length=64)
    target_arch: str = Field(min_length=1, max_length=64)
    backup_confirmed: bool = False


class UpdateSubmitBody(LifecycleSubmitBody):
    """Update submit: adds the CURRENT recipe baseline assertion.

    The executor VERIFIES this assertion against the deployed Compose
    bytes before any effect (``state_diverged`` refuses otherwise), so the
    recorded prior version is observed, never assumed (C09 rule 4).
    """

    current_recipe: str = Field(min_length=1)


class RemoveSubmitBody(LifecycleSubmitBody):
    """Remove submit: the EXPLICIT C09 rule 5 data decision.

    No default: an absent intent is a 422, never an inferred delete.
    """

    data_intent: RemoveDataIntent


class ContainerView(BaseModel):
    """One observed container (the discovery safe view: no env/mounts)."""

    container_id: str
    name: str
    image: str
    state: str
    status: str
    ports: list[dict[str, Any]]
    compose_service: str | None


class ComposeDetailView(BaseModel):
    kind: Literal["compose_project"] = "compose_project"
    project_name: str
    working_dir: str | None
    config_files: list[str]
    environment_files: list[str]
    services: list[str]
    containers: list[ContainerView]


class StandaloneDetailView(BaseModel):
    kind: Literal["standalone_container"] = "standalone_container"
    container: ContainerView


class SystemdUnitDetailView(BaseModel):
    kind: Literal["systemd_unit"] = "systemd_unit"
    unit: str
    unit_file_state: str | None
    load_state: str | None
    active_state: str | None
    sub_state: str | None
    description: str | None


class ObservedApplicationView(BaseModel):
    """One observed application; ``managed`` ONLY with an adopted id.

    ``suggested_category`` is the deterministic taxonomy hint for the
    adoption decision (never an enforced label); ``category`` is the
    already-mapped category when the observed application carries a
    managed id with Service Map metadata (``None`` otherwise).
    """

    origin: str
    managed: bool
    application_id: str | None
    suggested_category: str = "system"
    category: str | None = None
    detail: (
        ComposeDetailView | StandaloneDetailView | SystemdUnitDetailView
    )


class LaneView(BaseModel):
    """One lane outcome (C08 rule 6: honest absence, never empty success)."""

    lane: str
    state: str
    detail: str
    truncated: bool
    malformed_lines: int


class DiscoveryView(BaseModel):
    """The live observed inventory for one server (read-only snapshot).

    ``categories`` is the merged service taxonomy (8 built-ins +
    customs) for the adoption picker.
    """

    server_id: str
    observed_at: str
    applications: list[ObservedApplicationView]
    categories: list[ServiceCategoryView] = Field(default_factory=list)
    listen_ports: list[dict[str, Any]]
    lanes: list[LaneView]


# ---------------------------------------------------------------------------
# Service access + small helpers
# ---------------------------------------------------------------------------


def _store(request: Request) -> ApplicationManifestStore:
    """The lifespan-composed canonical manifest store; without it fail closed."""
    store = getattr(request.app.state, "applications", None)
    if store is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "applications service is unavailable",
        )
    return store


def _adoption(request: Request) -> ApplicationAdoption:
    """The lifespan-composed adoption worker; without it fail closed."""
    adoption = getattr(request.app.state, "app_adoption", None)
    if adoption is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "applications service is unavailable",
        )
    return adoption


def _docs(request: Request) -> ApplicationDocService | None:
    """The app-docs maintainer; ``None`` is honest (doc outcome pending)."""
    return getattr(request.app.state, "app_docs", None)


def _map_store(request: Request) -> ServiceMapStore:
    """The lifespan-composed Service Map store; without it fail closed."""
    store = getattr(request.app.state, "service_map", None)
    if store is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "service map is unavailable",
        )
    return store


def _application_id(value: str) -> uuid.UUID | None:
    """Canonical UUID parse for wire identities (``None`` = malformed)."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


def _validation_error(
    request: Request, field: str, detail: str
) -> Response:
    return render_error_envelope(
        request,
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        detail,
        fields={field: "invalid"},
    )


#: Producer rejection code → (HTTP status, envelope code). Facade messages
#: are already value-free (C01 rule 3) and travel as-is.
_ADOPTION_STATUS: Final[dict[AdoptionErrorCode, tuple[int, str]]] = {
    AdoptionErrorCode.NOT_FOUND: (HTTP_404_NOT_FOUND, "not_found"),
    AdoptionErrorCode.NOT_ADOPTED: (HTTP_404_NOT_FOUND, "not_found"),
    AdoptionErrorCode.ALREADY_ADOPTED: (HTTP_409_CONFLICT, "conflict"),
    AdoptionErrorCode.STALE_REVISION: (HTTP_409_CONFLICT, "conflict"),
    AdoptionErrorCode.REFERENCE_CONFLICT: (HTTP_409_CONFLICT, "conflict"),
    AdoptionErrorCode.CROSS_SERVER_CONFLICT: (HTTP_409_CONFLICT, "conflict"),
    AdoptionErrorCode.INTENT_PENDING: (HTTP_409_CONFLICT, "conflict"),
    AdoptionErrorCode.INTENT_MISMATCH: (HTTP_409_CONFLICT, "conflict"),
    AdoptionErrorCode.INTENT_PHASE: (HTTP_409_CONFLICT, "conflict"),
    AdoptionErrorCode.PAYLOAD_MISMATCH: (HTTP_409_CONFLICT, "conflict"),
    AdoptionErrorCode.FILE_DIVERGED: (HTTP_409_CONFLICT, "conflict"),
    AdoptionErrorCode.TOMBSTONE_CONFLICT: (HTTP_409_CONFLICT, "conflict"),
    AdoptionErrorCode.ORPHAN_MANIFEST: (HTTP_409_CONFLICT, "conflict"),
    AdoptionErrorCode.METADATA_MISSING: (HTTP_409_CONFLICT, "conflict"),
    AdoptionErrorCode.METADATA_INVALID: (HTTP_409_CONFLICT, "conflict"),
    AdoptionErrorCode.HASH_MISMATCH: (HTTP_409_CONFLICT, "conflict"),
    AdoptionErrorCode.PATH_ESCAPE: (HTTP_409_CONFLICT, "conflict"),
}


def _adoption_error(request: Request, error: AdoptionError) -> Response:
    """Map one value-free producer rejection onto the C01 envelope."""
    mapped = _ADOPTION_STATUS.get(error.code)
    if mapped is not None:
        status, code = mapped
        return render_error_envelope(request, status, code, str(error))
    # Request-shape rejections (invalid ids/name/origin/reference/revision,
    # unparsable documents) are the caller's field errors.
    return _validation_error(request, "body", str(error))


def _recipe_error(request: Request, error: RecipeError) -> Response:
    """Map one recipe/plan rejection onto the C01 envelope (AC 1)."""
    field = "recipe"
    if error.code is RecipeErrorCode.UNSUPPORTED_TARGET_OS:
        field = "target_os"
    elif error.code is RecipeErrorCode.UNSUPPORTED_TARGET_ARCH:
        field = "target_arch"
    elif error.code is RecipeErrorCode.INVALID_TARGET_ENVIRONMENT:
        field = "target"
    elif error.code is RecipeErrorCode.INVALID_APPLICATION_ID:
        field = "application_id"
    return _validation_error(request, field, str(error))


def _instructions_view(instructions: ServiceInstructions) -> ServiceInstructionsView:
    """Type one store runbook into the wire view (pure projection)."""
    return ServiceInstructionsView(**instructions.to_json())


def _categories_view(store: ServiceMapStore) -> list[ServiceCategoryView]:
    """The merged taxonomy (built-ins + customs) as the wire view."""
    return [
        ServiceCategoryView(
            machine_id=category.machine_id,
            label=category.label,
            description=category.description,
            built_in=category.built_in,
        )
        for category in store.list_categories()
    ]


def _application_view(
    record: AdoptedApplication, entry: ServiceMapEntry | None = None
) -> ApplicationView:
    """Type one projection record into the wire view (frozen ``…Z`` times).

    When the Service Map holds metadata for this application, its
    category/description/instructions enrich the view (defaults
    ``"system"``/empty keep unmapped records source-compatible).
    """
    return ApplicationView(
        application_id=str(record.application_id),
        server_id=str(record.server_id),
        name=record.name,
        origin=record.origin.value,
        compose_project=record.compose_project,
        working_dir=record.working_dir,
        unit_name=record.unit_name,
        external_working_dir=record.external_working_dir,
        external_config_files=list(record.external_config_files),
        external_environment_files=list(record.external_environment_files),
        revision=record.revision,
        schema_revision=record.schema_revision,
        sha256=record.sha256,
        adopted_at=_wire_ts(record.adopted_at),
        reconciled_at=_wire_ts(record.reconciled_at),
        stale=record.stale,
        category="system" if entry is None else entry.category,
        description="" if entry is None else entry.description,
        instructions=(
            ServiceInstructionsView()
            if entry is None
            else _instructions_view(entry.instructions)
        ),
    )


def _map_entry_view(entry: ServiceMapEntry) -> ApplicationView:
    """The wire view of one Service Map entry with no SQL adoption row.

    Chat/manual additions appear seamlessly in the adopted listing: the
    entry identity/timestamps/revision stand in for the projection
    fields, the digest is the entry's canonical hash, and there is no
    external deployment reference (``None``/empty — manual entries are
    controller-local metadata only).
    """
    return ApplicationView(
        application_id=entry.application_id,
        server_id=entry.server_id,
        name=entry.name,
        origin=entry.origin,
        compose_project=entry.compose_project,
        working_dir=entry.working_dir,
        unit_name=entry.unit_name,
        external_working_dir=None,
        external_config_files=[],
        external_environment_files=[],
        revision=entry.revision,
        schema_revision=0,
        sha256=entry_digest(entry),
        adopted_at=entry.updated_at,
        reconciled_at=entry.updated_at,
        stale=False,
        category=entry.category,
        description=entry.description,
        instructions=_instructions_view(entry.instructions),
    )


def _wire_ts(value: datetime) -> str:
    """Render one stored UTC timestamp in the frozen ``…Z`` wire form."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _plan_view(plan: DeploymentPlan) -> PlanView:
    """Type one deterministic plan into the wire view (pure projection)."""
    probe = plan.health_probe
    return PlanView(
        server_id=str(plan.server_id),
        application_id=str(plan.application_id),
        project_name=plan.project_name,
        target_os=plan.target_os,
        target_arch=plan.target_arch,
        image=plan.image,
        recipe_sha256=plan.recipe_sha256,
        compose_path=plan.compose_path,
        compose_sha256=plan.compose_sha256,
        compose_text=plan.compose_text,
        directories=list(plan.directories),
        ports=[
            PortBindingView(host=p.host, container=p.container, protocol=p.protocol)
            for p in plan.ports
        ],
        volumes=[
            PlannedVolumeView(name=v.name, container_path=v.container_path, host_path=v.host_path)
            for v in plan.volumes
        ],
        credentials=list(plan.credentials),
        credentials_path=plan.credentials_path,
        health_probe=(
            HealthProbeView(
                kind=probe.kind,
                port=probe.port,
                path=probe.path,
                expected_status=probe.expected_status,
                interval_seconds=probe.interval_seconds,
                timeout_seconds=probe.timeout_seconds,
                retries=probe.retries,
                start_period_seconds=probe.start_period_seconds,
            )
            if probe is not None
            else None
        ),
        prerequisites=list(plan.prerequisites),
        required_grants=list(plan.required_grants),
        operations=[
            PlanOperationView(op=o.op, subject=o.subject) for o in plan.operations
        ],
        limitations=list(plan.limitations),
        plan_sha256=plan.plan_sha256,
    )


async def _submit(
    request: Request,
    *,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    capability_id: str,
    payload: dict[str, Any],
    recipe_credentials: tuple[str, ...],
    body: LifecycleSubmitBody,
) -> Response:
    """Submit one deterministic plan through the EXISTING jobs facade.

    The reviewed ``mai.api.jobs.submit_run`` owns the whole durable path —
    boundary, target binding, idempotency, engine enqueue — so this
    surface composes intent only (AC 1) and re-proves nothing (the facade
    re-checks its own boundary after this route's application checks).
    """
    step = StepBody(
        capability_id=capability_id,
        payload=payload,
        resource=f"{LOCK_RESOURCE_PREFIX}{application_id}",
    )
    facade_body = RunSubmitBody(
        run_id=body.run_id,
        server_id=str(server_id),
        model_profile=LIFECYCLE_MODEL_PROFILE,
        credential_references=list(recipe_credentials),
        steps=[step],
    )
    return await submit_run(request, facade_body)


# ---------------------------------------------------------------------------
# Discovery serialization (safe view only — the producer's own shapes)
# ---------------------------------------------------------------------------


def _container_view(container: Any) -> ContainerView:
    return ContainerView(
        container_id=container.container_id,
        name=container.name,
        image=container.image,
        state=container.state,
        status=container.status,
        ports=[asdict(p) for p in container.ports],
        compose_service=container.compose_service,
    )


def _detail_view(observed: ObservedApplication) -> (
    ComposeDetailView | StandaloneDetailView | SystemdUnitDetailView
):
    detail = observed.detail
    if isinstance(detail, ComposeProjectObservation):
        return ComposeDetailView(
            project_name=detail.project_name,
            working_dir=detail.working_dir,
            config_files=list(detail.config_files),
            environment_files=list(detail.environment_files),
            services=list(detail.services),
            containers=[_container_view(c) for c in detail.containers],
        )
    if isinstance(detail, StandaloneContainerObservation):
        return StandaloneDetailView(container=_container_view(detail.container))
    assert isinstance(detail, SystemdUnitObservation)  # producer vocabulary
    return SystemdUnitDetailView(
        unit=detail.unit,
        unit_file_state=detail.unit_file_state,
        load_state=detail.load_state,
        active_state=detail.active_state,
        sub_state=detail.sub_state,
        description=detail.description,
    )


def _observed_name(view: ObservedApplicationView) -> str:
    """The display name of one observed application (classifier input)."""
    detail = view.detail
    if isinstance(detail, ComposeDetailView):
        return detail.project_name
    if isinstance(detail, StandaloneDetailView):
        return detail.container.name
    return detail.unit


def _inventory_view(
    inventory: DiscoveryInventory,
    *,
    categories: list[ServiceCategoryView],
    mapped_categories: Mapping[str, str],
) -> DiscoveryView:
    applications: list[ObservedApplicationView] = []
    for observed in inventory.applications:
        observed_id = (
            str(observed.application_id)
            if observed.application_id is not None
            else None
        )
        view = ObservedApplicationView(
            origin=observed.origin.value,
            managed=observed.managed,
            application_id=observed_id,
            detail=_detail_view(observed),
        )
        view.suggested_category = classify_service(_observed_name(view))
        view.category = (
            None if observed_id is None else mapped_categories.get(observed_id)
        )
        applications.append(view)
    return DiscoveryView(
        server_id=str(inventory.server_id),
        observed_at=inventory.observed_at,
        applications=applications,
        categories=categories,
        listen_ports=[asdict(p) for p in inventory.listen_ports],
        lanes=[
            LaneView(
                lane=lane.lane.value,
                state=lane.state.value,
                detail=lane.detail,
                truncated=lane.truncated,
                malformed_lines=lane.malformed_lines,
            )
            for lane in inventory.lanes
        ],
    )


# ---------------------------------------------------------------------------
# Routes (frozen design targets; mutation order: session → origin → CSRF)
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/api/v1/servers/{server_id}/applications",
    tags=["applications"],
)


@router.get(
    "",
    status_code=HTTP_200_OK,
    response_model=ApplicationPage,
    summary="List adopted applications (bounded page, C09 rule 1)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Query parameters failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Applications service unavailable",
        },
    },
)
async def list_adopted(
    request: Request,
    server_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> Response:
    """One bounded page of projected adoption records (C01 rule 5/7).

    Safe method: session layer only. Adoption records are the managed
    truth; discovery observations are a SEPARATE resource and never merge
    into this page (C09 rule 1). Service Map entries with no SQL adoption
    row (``origin="manual"`` chat/agent additions) appear seamlessly at
    the tail of the page, and the merged category taxonomy rides the page
    for the picker.
    """
    await _require_session(request)
    adoption = _adoption(request)
    map_store = _map_store(request)
    records = await adoption.list_adopted(server_id)
    entries = {entry.application_id: entry for entry in map_store.list_entries(server_id)}
    views = [
        _application_view(record, entries.get(str(record.application_id)))
        for record in records
    ]
    adopted_ids = {view.application_id for view in views}
    views.extend(
        _map_entry_view(entry)
        for entry in sorted(
            (entry for aid, entry in entries.items() if aid not in adopted_ids),
            key=lambda entry: (entry.name, entry.application_id),
        )
    )
    total = len(views)
    page = views[offset : offset + limit]
    return ApplicationPage(
        server_id=str(server_id),
        applications=page,
        categories=_categories_view(map_store),
        limit=limit,
        offset=offset,
        total=total,
    )


@router.post(
    "",
    status_code=HTTP_201_CREATED,
    response_model=AdoptionCreated,
    summary="Adopt one application (explicit admin action, C09 rule 1)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token mismatch",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile stored for the target server id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale revision, duplicate adoption, selector "
            "overlap or cross-server identity conflict",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Applications service unavailable",
        },
    },
)
async def adopt_application(
    request: Request, server_id: uuid.UUID, payload: AdoptBody
) -> Response:
    """Commit one adoption: canonical manifest + projection + record doc.

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    The target must be an enrolled canonical server (C08 G2 binding); the
    decision composes the reviewed producer request and the journaled
    pipeline runs file + projection (local canonical effects only — the
    external deployment paths are referenced, never touched, C09 rule 6).
    On success the application's identity record is documented through the
    reviewed app-docs producer; a documentation refusal/absence is
    reported honestly on the response and converges on the next adoption
    revision — the committed adoption is never rolled back for it.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _store(request)
    adoption = _adoption(request)
    map_store = _map_store(request)
    application_id = _application_id(payload.application_id)
    if application_id is None:
        return _validation_error(request, "application_id", "must be a canonical UUID string")
    # Target binding: an unknown/unenrolled server is denied before any
    # effect (C01 rule 7; same boundary as the jobs facade).
    _canonical_profile(request, server_id)

    # Custom taxonomy categories ride the adoption request (saving the
    # picker round-trip); a store rejection is the caller's field error.
    for category_body in payload.upsert_categories:
        try:
            map_store.upsert_category(
                category_body.machine_id,
                label=category_body.label,
                description=category_body.description,
            )
        except ServiceMapError as error:
            return _validation_error(
                request, "upsert_categories", error.detail
            )

    if payload.origin == "manual":
        return await _adopt_manual_entry(request, map_store, server_id, application_id, payload)

    try:
        composed = AdoptionRequest(
            server_id=server_id,
            application_id=application_id,
            name=payload.name,
            origin=ApplicationOrigin(payload.origin),
            compose_project=payload.compose_project,
            working_dir=payload.working_dir,
            unit_name=payload.unit_name,
            external_working_dir=payload.external_working_dir,
            external_config_files=tuple(payload.external_config_files),
            external_environment_files=tuple(payload.external_environment_files),
            expected_revision=payload.expected_revision,
        )
    except AdoptionError as error:
        return _adoption_error(request, error)
    try:
        result = await adoption.adopt(store, composed)
    except AdoptionError as error:
        return _adoption_error(request, error)
    # The Service Map sidecar: category/description/instructions persist
    # for the adopted service (the map is the metadata catalogue over the
    # C09 truth; a store rejection here surfaces, never a silent skip).
    entry = map_store.upsert_entry(
        server_id,
        name=payload.name,
        category=payload.category or "system",
        origin=ApplicationOrigin(payload.origin).value,
        application_id=application_id,
        compose_project=payload.compose_project,
        working_dir=payload.working_dir,
        unit_name=payload.unit_name,
        description=payload.description or "",
        instructions=payload.instructions.model_dump(),
    )
    record = await adoption.read(server_id, application_id)
    assert record is not None  # the pipeline just reconciled it
    return AdoptionCreated(
        intent_id=str(result.intent_id),
        application_id=str(result.application_id),
        server_id=str(result.server_id),
        outcome=result.outcome,
        revision=result.revision,
        sha256=result.sha256,
        application=_application_view(record, entry),
        record_doc=await _document_adoption(request, record),
    )


async def _adopt_manual_entry(
    request: Request,
    map_store: ServiceMapStore,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    payload: AdoptBody,
) -> Response:
    """The chat/manual addition: persist directly in the Service Map.

    A manual entry has no live compose/systemd/docker target, so the
    adoption pipeline's target-reference restriction cannot apply — the
    entry is stored in the confined map plus its canonical identity
    record, and answers with the reconciled outcome. The identity doc
    write is an upsert; a documentation refusal/absence is reported
    honestly (``pending``) — the committed map entry is never rolled
    back for it.
    """
    entry = map_store.upsert_entry(
        server_id,
        name=payload.name,
        category=payload.category or "system",
        origin="manual",
        application_id=application_id,
        compose_project=payload.compose_project,
        working_dir=payload.working_dir,
        unit_name=payload.unit_name,
        description=payload.description or "",
        instructions=payload.instructions.model_dump(),
    )
    return AdoptionCreated(
        intent_id=str(uuid.uuid5(application_id, "service-map:manual-adoption")),
        application_id=entry.application_id,
        server_id=entry.server_id,
        outcome="reconciled",
        revision=entry.revision,
        sha256=entry_digest(entry),
        application=_map_entry_view(entry),
        record_doc=_document_manual_entry(request, server_id, application_id, entry),
    )


def _render_manual_record(entry: ServiceMapEntry) -> str:
    """The deterministic identity record for one manual map entry."""
    lines = [
        f"# {entry.name}",
        "",
        "Manual Service Map entry (chat/agent addition).",
        "",
        "## Service",
        "",
        f"- application_id: `{entry.application_id}`",
        f"- server_id: `{entry.server_id}`",
        f"- origin: `{entry.origin}`",
        f"- category: `{entry.category}`",
        f"- map_revision: {entry.revision}",
        "",
    ]
    if entry.description:
        lines += ["## Description", "", entry.description, ""]
    runbook = entry.instructions.to_json()
    if any(runbook.values()):
        lines += ["## Runbook instructions", ""]
        lines += [f"- **{key}**: {runbook[key]}" for key in INSTRUCTION_KEYS if runbook[key]]
        lines.append("")
    return "\n".join(lines) + "\n"


def _document_manual_entry(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    entry: ServiceMapEntry,
) -> RecordDocView:
    """Seed the manual entry's identity record through the canonical store."""
    knowledge = getattr(request.app.state, "knowledge", None)
    doc_store: DocumentStore | None = (
        None if knowledge is None else getattr(knowledge, "store", None)
    )
    if doc_store is None:
        return RecordDocView(status="pending", reason="unavailable")
    doc_id = record_doc_id(server_id, application_id)
    try:
        try:
            current = doc_store.read(
                doc_id, server_id=server_id, app_id=application_id
            )
            expected_revision = current.revision
        except DocumentStoreError as error:
            if error.code is not DocumentStoreErrorCode.NOT_FOUND:
                raise
            expected_revision = 0
        document = doc_store.write(
            doc_id,
            _render_manual_record(entry),
            server_id=server_id,
            app_id=application_id,
            namespace=DocumentNamespace.DOCS,
            expected_revision=expected_revision,
            source_kind=SourceKind.GENERATED,
            origin=f"service-map:{entry.revision}",
            origin_revision=str(entry.revision),
            verification=VerificationStatus.UNVERIFIED,
        )
    except DocumentStoreError as error:
        return RecordDocView(status="pending", reason=error.code.value)
    return RecordDocView(status="documented", doc_id=str(document.doc_id))


async def _document_adoption(
    request: Request, record: AdoptedApplication
) -> RecordDocView:
    """Seed the identity record through the reviewed app-docs producer.

    Failure is honest and non-fatal: the adoption is committed; the record
    documents ``pending`` with a value-free reason and converges on the
    next adoption revision (the doc write is an upsert).
    """
    docs = _docs(request)
    if docs is None:
        return RecordDocView(status="pending", reason="unavailable")
    try:
        outcome = await docs.record_adoption(record)
    except AppDocError as error:
        return RecordDocView(status="pending", reason=error.code.value)
    if outcome.document is None or not outcome.documented:
        return RecordDocView(
            status="pending", reason=outcome.reason or "not_documented"
        )
    return RecordDocView(status="documented", doc_id=str(outcome.document.doc_id))


@router.get(
    "/discoveries",
    status_code=HTTP_200_OK,
    response_model=DiscoveryView,
    summary="Observe one server's application inventory (read-only lanes)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile stored for the target server id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Pinned host key does not match the target "
            "(security boundary; re-enrollment is an explicit admin action)",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Transport prerequisites are unusable",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Credential source or SSH transport unavailable",
        },
    },
)
async def discover_applications(
    request: Request, server_id: uuid.UUID
) -> Response:
    """One live discovery snapshot through the pinned-identity SSH lanes.

    Safe method: session layer only, but the lanes are real bounded
    read-only commands on the TARGET (docker ps / systemctl / ss) — the
    reviewed producer reports per-lane honesty and collects the safe view
    only (no environment values, mounts or command lines; no file
    contents). Managed tagging comes exclusively from the adopted
    records' selectors (C09 rule 1: discovery grants nothing and enables
    no execution). The transport runs off the event loop; the pinned host
    key and identity key resolve server-side only, exactly like the
    enrolled password lane.
    """
    await _require_session(request)
    store = _store(request)
    adoption = _adoption(request)
    stored = _canonical_profile(request, server_id)
    profile = parse_server_profile_text(stored.text)
    resolver = getattr(request.app.state, "credentials", None)
    if resolver is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "credential source is not configured",
        )
    known_hosts_path = (
        store.root / "servers" / str(server_id) / "known_hosts"
    )
    managed_refs = await adoption.managed_references(server_id)
    try:
        key_path = resolve_identity_key(resolver, profile.ssh.credential_ref)
    except (EnvFileError, SshConnectError):
        raise BoundaryError(
            HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_configuration",
            "transport prerequisites are missing; nothing ran",
        ) from None
    try:
        inventory = await asyncio.to_thread(
            discover_server,
            profile.ssh,
            server_id=server_id,
            key_path=str(key_path),
            known_hosts_path=str(known_hosts_path),
            managed_refs=managed_refs,
        )
    except SshConnectError as error:
        if error.code is SshErrorCode.HOST_KEY_MISMATCH:
            return render_error_envelope(
                request,
                HTTP_409_CONFLICT,
                "conflict",
                "pinned host key does not match the target; re-enrollment "
                "is an explicit admin action",
            )
        if error.code is SshErrorCode.INVALID_CONFIGURATION:
            return render_error_envelope(
                request,
                HTTP_422_UNPROCESSABLE_CONTENT,
                "invalid_configuration",
                error.detail,
            )
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "discovery transport is unavailable",
        ) from None
    except OSError as error:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "discovery transport is unavailable",
        ) from error
    map_store = _map_store(request)
    mapped_categories = {
        entry.application_id: entry.category
        for entry in map_store.list_entries(server_id)
    }
    return _inventory_view(
        inventory,
        categories=_categories_view(map_store),
        mapped_categories=mapped_categories,
    )


@router.delete(
    "/{application_id}",
    status_code=HTTP_204_NO_CONTENT,
    summary="Revoke an adoption (non-destructive; C09 rule 6)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token mismatch",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No adoption record for this application id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale revision or unreconciled journal state",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Applications service unavailable",
        },
    },
)
async def revoke_application(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    payload: RevokeBody,
) -> Response:
    """Revoke the adoption: tombstone first, then file, then projection.

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    ONLY the canonical manifest pair is removed — the external deployment
    paths recorded in the manifest are never touched (C09 rule 6), and no
    target-side effect of any kind runs in this route (acceptance 2).
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _store(request)
    adoption = _adoption(request)
    _canonical_profile(request, server_id)
    try:
        await adoption.revoke(
            store,
            server_id,
            application_id,
            expected_revision=payload.expected_revision,
        )
    except AdoptionError as error:
        return _adoption_error(request, error)
    return Response(status_code=HTTP_204_NO_CONTENT)


@router.post(
    "/plan",
    status_code=HTTP_200_OK,
    response_model=PlanView,
    summary="Preview the deterministic plan for one recipe (C09 rule 3)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token mismatch",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile stored for the target server id",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Recipe or target environment failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Applications service unavailable",
        },
    },
)
async def plan_application(
    request: Request, server_id: uuid.UUID, payload: PlanBody
) -> Response:
    """The pure C09 rule 3 preview: actual intended resources/limits.

    POST carries the frozen mutating-request boundary (C08 rule 2), but
    the route is PURE: it validates the recipe against the frozen schema,
    rebuilds the deterministic policy-bound plan for the named target and
    returns it — no store write, no projection, no target contact.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    _store(request)
    _canonical_profile(request, server_id)
    application_id = _application_id(payload.application_id)
    if application_id is None:
        return _validation_error(
            request, "application_id", "must be a canonical UUID string"
        )
    try:
        recipe = parse_recipe_text(payload.recipe)
    except RecipeError as error:
        return _recipe_error(request, error)
    try:
        plan = build_deployment_plan(
            recipe,
            server_id,
            application_id,
            TargetEnvironment(os=payload.target_os, arch=payload.target_arch),
        )
    except RecipeError as error:
        return _recipe_error(request, error)
    return _plan_view(plan)


async def _lifecycle_submit(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    body: LifecycleSubmitBody,
    *,
    capability_id: str,
    current_recipe_text: str | None = None,
    data_intent: RemoveDataIntent | None = None,
) -> Response:
    """Shared lifecycle submit: validate, rebuild the plan, submit intent.

    Mutation order: session → origin → CSRF (the facade re-checks its own
    boundary when the intent is submitted). The route performs its local,
    effect-free validations first (recipe schema, deterministic plan,
    cross-server identity guard), then hands the EXACT plan document to
    the reviewed jobs facade as ONE capability step (acceptance 1/2: HTTP
    persists intent; no Docker/SSH effect exists in this route).
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _store(request)
    _canonical_profile(request, server_id)
    try:
        recipe = parse_recipe_text(body.recipe)
    except RecipeError as error:
        return _recipe_error(request, error)
    current_recipe = None
    if current_recipe_text is not None:
        try:
            current_recipe = parse_recipe_text(current_recipe_text)
        except RecipeError as error:
            return _validation_error(request, "current_recipe", str(error))
        if recipe_sha256(current_recipe) == recipe_sha256(recipe):
            return _validation_error(
                request,
                "current_recipe",
                "the submitted recipe equals the asserted current baseline; "
                "an update must pin a different revision",
            )
    # Cross-server identity guard (C09 rule 1; mirrors the executor's own
    # pre-lock guard): the application id must not belong to another
    # server's canonical tree — an explicit 409, never a cross-server effect.
    if any(other != server_id for other in store.servers_storing(application_id)):
        return render_error_envelope(
            request,
            HTTP_409_CONFLICT,
            "conflict",
            "this application id already exists under a different server; "
            "cross-server relocation is refused",
        )
    try:
        plan = build_deployment_plan(
            recipe,
            server_id,
            application_id,
            TargetEnvironment(os=body.target_os, arch=body.target_arch),
        )
    except RecipeError as error:
        return _recipe_error(request, error)
    payload: dict[str, Any] = json.loads(canonical_plan_json(plan))
    payload["backup_confirmed"] = body.backup_confirmed
    if current_recipe is not None:
        payload["current_recipe"] = render_recipe(current_recipe)
    if data_intent is not None:
        # Explicit C09 rule 5 decision inside the digest-bound payload.
        payload["data_intent"] = data_intent.value
    return await _submit(
        request,
        server_id=server_id,
        application_id=application_id,
        capability_id=capability_id,
        payload=payload,
        recipe_credentials=recipe.credentials,
        body=body,
    )


@router.post(
    "/{application_id}/install",
    status_code=HTTP_201_CREATED,
    response_model=RunCreated,
    summary="Submit an install intent (durable plan via the jobs facade)",
    responses={
        HTTP_200_OK: {
            "model": RunCreated,
            "description": "Duplicate submit attached to the existing run",
        },
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token mismatch",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile stored for the target server id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Run id reused by a different intent, or the "
            "application id belongs to another server",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Jobs domain unavailable",
        },
    },
)
async def install_application(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    payload: LifecycleSubmitBody,
) -> Response:
    """Submit the install plan as one ``app.install`` step (C09 rule 3).

    The plan is rebuilt deterministically from the submitted recipe —
    identical inputs yield the identical plan hash, so the executor's
    approval binding (C03 rule 4) and the submit idempotency digest agree.
    Policy/grants/approval are enforced by the worker's G2 gate on this
    exact intent; a denial surfaces as the executor's structured refusal.
    """
    return await _lifecycle_submit(
        request,
        server_id,
        application_id,
        payload,
        capability_id=INSTALL_CAPABILITY,
    )


@router.post(
    "/{application_id}/update",
    status_code=HTTP_201_CREATED,
    response_model=RunCreated,
    summary="Submit an update intent (revision-aware baseline assertion)",
    responses={
        HTTP_200_OK: {
            "model": RunCreated,
            "description": "Duplicate submit attached to the existing run",
        },
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token mismatch",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile stored for the target server id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Run id reused by a different intent, or the "
            "application id belongs to another server",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation (an update whose "
            "recipe equals the asserted baseline is refused)",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Jobs domain unavailable",
        },
    },
)
async def update_application(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    payload: UpdateSubmitBody,
) -> Response:
    """Submit the update plan with the asserted current baseline (C09 r4)."""
    return await _lifecycle_submit(
        request,
        server_id,
        application_id,
        payload,
        capability_id=UPDATE_CAPABILITY,
        current_recipe_text=payload.current_recipe,
    )


@router.post(
    "/{application_id}/remove",
    status_code=HTTP_201_CREATED,
    response_model=RunCreated,
    summary="Submit a removal intent (explicit keep/delete data decision)",
    responses={
        HTTP_200_OK: {
            "model": RunCreated,
            "description": "Duplicate submit attached to the existing run",
        },
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token mismatch",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile stored for the target server id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Run id reused by a different intent, or the "
            "application id belongs to another server",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation (an absent or "
            "unknown data_intent is a validation error — never inferred)",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Jobs domain unavailable",
        },
    },
)
async def remove_application(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    payload: RemoveSubmitBody,
) -> Response:
    """Submit the removal plan with the EXPLICIT data decision (C09 r5).

    ``data_intent`` has no default and its only values are ``keep`` and
    ``delete``; the decision travels inside the digest-bound step payload
    and never triggers deletion by itself — the executor records it on
    the receipt, and policy may delegate the choice, inference never.
    """
    return await _lifecycle_submit(
        request,
        server_id,
        application_id,
        payload,
        capability_id=REMOVE_CAPABILITY,
        data_intent=payload.data_intent,
    )


def register_applications(app: FastAPI) -> None:
    """Register the applications surface under the shared boundary (C01/C08)."""
    app.include_router(router)
