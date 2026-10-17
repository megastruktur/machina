"""Focused contract tests for lifecycle-to-documentation wiring (card 08-app-docs).

Acceptance criteria (card brief):
1. Verified lifecycle writes version/paths/health/recovery result to
   server/app docs and queues reindex.
2. Failed/uncertain lifecycle records incident, never overwrites old
   verified state as new success.

Contract anchors: contracts.md C02 (rule 2 docs/runbooks layout, rule 3
confined identity paths, W4 tombstone recreate), C07 (rule 1 provenance,
rule 2 durable reindex intents, rule 5 verified-success/incident
boundary), C09 (rules 2/4/5 declared semantics recorded, never guessed).
All canonical-store claims run on a REAL temporary filesystem (atomicity,
confinement, tombstone semantics are real-boundary claims); receipts are
the settled result dataclasses the services return, bound to the REAL
deterministic planner by hash. No mocks: the only substituted seam is the
explicitly injected ``reconcile`` hook (its absence must leave the
durable intent pending — that honesty is itself asserted).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from mai.applications.adopt import AdoptedApplication
from mai.applications.discover import ApplicationOrigin
from mai.applications.document import (
    AppDocError,
    AppDocErrorCode,
    ApplicationDocService,
    DocEvent,
    LifecyclePhase,
    incident_doc_id,
    record_doc_id,
    render_runbook,
    state_doc_id,
    RunbookFacts,
)
from mai.applications.install import InstallResult
from mai.applications.recipes import (
    ApplicationRecipe,
    DeploymentPlan,
    TargetEnvironment,
    build_deployment_plan,
    parse_recipe_text,
)
from mai.applications.remove import RemoveResult
from mai.applications.update import UpdateResult
from mai.knowledge.documents import (
    DocumentNamespace,
    DocumentStore,
    DocumentStoreError,
    DocumentStoreErrorCode,
    VerificationStatus,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_DIR = BACKEND_ROOT.parent / "catalogue"

HTTP_FILE = "example-http.yaml"
STATEFUL_FILE = "example-stateful.yaml"

TARGET = TargetEnvironment(os="linux", arch="x86_64")

SERVER_ID = uuid.UUID("0a0d0a0d-0a0d-4a0d-8a0d-0a0d0a0d0a01")
SERVER_2_ID = uuid.UUID("0a0d0a0d-0a0d-4a0d-8a0d-0a0d0a0d0a02")
APP_ID = uuid.UUID("0b0d0b0d-0b0d-4b0d-8b0d-0b0d0b0d0b01")

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures: frozen catalogue recipes, real planner, settled receipts
# ---------------------------------------------------------------------------


def _document_texts(filename: str) -> list[str]:
    stream = (CATALOGUE_DIR / filename).read_text(encoding="utf-8")
    return [yaml.serialize(node) for node in yaml.compose_all(stream)]


def _recipe(filename: str = HTTP_FILE, revision: int = 0) -> ApplicationRecipe:
    return parse_recipe_text(_document_texts(filename)[revision])


def _plan(
    recipe: ApplicationRecipe,
    server_id: uuid.UUID = SERVER_ID,
    app_id: uuid.UUID = APP_ID,
    target: TargetEnvironment = TARGET,
) -> DeploymentPlan:
    return build_deployment_plan(recipe, server_id, app_id, target)


def _install_result(
    plan: DeploymentPlan,
    *,
    status: str = "succeeded",
    effect: str = "executed",
    reason: str | None = None,
    health_verified: bool = True,
    manifest_revision: int | None = 3,
    manifest_sha256: str | None = "a" * 64,
    published_ports: tuple[int, ...] = (18080,),
    rollback_attempted: bool = False,
    rollback_verified: bool = False,
) -> InstallResult:
    return InstallResult(
        server_id=plan.server_id,
        application_id=plan.application_id,
        plan_sha256=plan.plan_sha256,
        status=status,
        effect=effect,
        reason=reason,
        manifest_revision=manifest_revision,
        manifest_sha256=manifest_sha256,
        health_verified=health_verified,
        published_ports=published_ports,
        rollback_attempted=rollback_attempted,
        rollback_verified=rollback_verified,
    )


def _update_result(
    plan: DeploymentPlan,
    *,
    new_image: str,
    prior_image: str = "docker.io/example/prior@sha256:" + "9" * 64,
    status: str = "succeeded",
    effect: str = "executed",
    reason: str | None = None,
    health_verified: bool = True,
    migrations: str = "none",
    image_rollback_supported: bool = True,
    snapshot_paths: tuple[str, ...] = (),
    rollback_attempted: bool = False,
    rollback_verified: bool = False,
) -> UpdateResult:
    return UpdateResult(
        server_id=plan.server_id,
        application_id=plan.application_id,
        plan_sha256=plan.plan_sha256,
        status=status,
        effect=effect,
        reason=reason,
        new_image=new_image,
        migrations=migrations,
        image_rollback_supported=image_rollback_supported,
        manifest_revision=4,
        manifest_sha256="b" * 64,
        health_verified=health_verified,
        prior_image=prior_image,
        snapshot_paths=snapshot_paths,
        rollback_attempted=rollback_attempted,
        rollback_verified=rollback_verified,
    )


def _remove_result(
    plan: DeploymentPlan,
    *,
    status: str = "succeeded",
    effect: str = "executed",
    reason: str | None = None,
    manifest_removed: bool = True,
) -> RemoveResult:
    return RemoveResult(
        server_id=plan.server_id,
        application_id=plan.application_id,
        plan_sha256=plan.plan_sha256,
        status=status,
        effect=effect,
        reason=reason,
        data_intent="keep",
        manifest_removed=manifest_removed,
        manifest_revision=None if manifest_removed else 3,
    )


def _record(
    *,
    server_id: uuid.UUID = SERVER_ID,
    app_id: uuid.UUID = APP_ID,
    stale: bool = False,
    revision: int = 2,
) -> AdoptedApplication:
    return AdoptedApplication(
        application_id=app_id,
        server_id=server_id,
        name="example-http",
        origin=ApplicationOrigin.COMPOSE,
        compose_project="mai-app-0b0d0b0d",
        working_dir="/var/lib/mai/apps/0b0d0b0d",
        unit_name=None,
        external_working_dir=None,
        external_config_files=("/etc/example/nginx.conf",),
        external_environment_files=(),
        revision=revision,
        schema_revision=1,
        sha256="c" * 64,
        adopted_at=NOW,
        reconciled_at=NOW + timedelta(seconds=1),
        stale=stale,
    )


def _service(store: DocumentStore, **kwargs: object) -> ApplicationDocService:
    return ApplicationDocService(store, **kwargs)


def _runbook(store: DocumentStore, *, server_id: uuid.UUID = SERVER_ID):
    return store.read(
        state_doc_id(server_id, APP_ID),
        server_id=server_id,
        app_id=APP_ID,
        namespace=DocumentNamespace.RUNBOOKS,
    )


def _record_doc(store: DocumentStore, *, server_id: uuid.UUID = SERVER_ID):
    return store.read(
        record_doc_id(server_id, APP_ID),
        server_id=server_id,
        app_id=APP_ID,
        namespace=DocumentNamespace.DOCS,
    )


def _upsert_intents(store: DocumentStore, server_id: uuid.UUID = SERVER_ID):
    return [
        intent
        for intent in store.read_intents(server_id=server_id, app_id=APP_ID)
        if intent.op == "upsert"
    ]


# ---------------------------------------------------------------------------
# AC1: verified lifecycle -> runbook/record docs + durable reindex queue
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_verified_install_writes_verified_runbook_with_facts_and_intent(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    plan = _plan(recipe)
    store = DocumentStore(tmp_path)
    outcome = await _service(store).record_install(
        recipe=recipe, target=TARGET, result=_install_result(plan)
    )
    assert outcome.event is DocEvent.RUNBOOK
    assert outcome.documented and outcome.reindex_queued
    assert outcome.verification is VerificationStatus.VERIFIED
    doc = _runbook(store)
    assert doc == outcome.document
    assert doc.namespace is DocumentNamespace.RUNBOOKS
    assert doc.source_kind.value == "generated"
    assert doc.origin == f"install:{plan.plan_sha256}"
    assert doc.verification is VerificationStatus.VERIFIED
    # version / paths / health / recovery facts, from the exact verified plan
    assert f"image: `{plan.image}`" in doc.text
    assert f"compose: `{plan.compose_path}`" in doc.text
    assert f"plan_sha256: `{plan.plan_sha256}`" in doc.text
    assert "probe: http port 80 path /health expecting status 200" in doc.text
    assert "image rollback: available" in doc.text
    assert "migrations: none" in doc.text
    assert "manifest revision: 3" in doc.text
    # reindex queued: one durable upsert intent bound to the committed source
    intents = _upsert_intents(store)
    assert len(intents) == 1
    assert intents[0].doc_id == state_doc_id(SERVER_ID, APP_ID)
    assert intents[0].sha256 == doc.sha256
    assert intents[0].revision == doc.revision == 1
    assert intents[0].tombstone is False


@pytest.mark.anyio
async def test_verified_update_refreshes_version_and_records_recovery(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    recipe_v2 = _recipe(revision=1)
    store = DocumentStore(tmp_path)
    service = _service(store)
    await service.record_install(
        recipe=recipe, target=TARGET, result=_install_result(_plan(recipe))
    )
    plan_v2 = _plan(recipe_v2)
    snapshot = _plan(recipe).compose_path.replace("compose.yaml", "snapshots/pre-update-abc.tar")
    outcome = await service.record_update(
        recipe=recipe_v2,
        target=TARGET,
        result=_update_result(
            plan_v2,
            new_image=recipe_v2.image,
            snapshot_paths=(snapshot,),
        ),
    )
    assert outcome.event is DocEvent.RUNBOOK
    doc = _runbook(store)
    assert doc.revision == 2  # SAME canonical document, refreshed in place
    assert f"image: `{recipe_v2.image}`" in doc.text
    assert f"prior image: `{_update_result(plan_v2, new_image='x').prior_image}`" in doc.text
    assert f"- pre-update snapshots: `{snapshot}`" in doc.text
    assert json.loads(_meta_text(store))["origin"] == f"update:{plan_v2.plan_sha256}"


@pytest.mark.anyio
async def test_adoption_writes_unverified_record_and_refuses_stale_record(
    tmp_path: Path,
) -> None:
    store = DocumentStore(tmp_path)
    outcome = await _service(store).record_adoption(_record())
    assert outcome.event is DocEvent.RECORD
    assert outcome.verification is VerificationStatus.UNVERIFIED
    doc = _record_doc(store)
    assert doc.namespace is DocumentNamespace.DOCS
    assert doc.verification is VerificationStatus.UNVERIFIED
    assert "# Application record: example-http" in doc.text
    assert "origin: compose" in doc.text
    assert "`/etc/example/nginx.conf`" in doc.text
    assert "Deployment state is NOT recorded here" in doc.text
    assert len(_upsert_intents(store)) == 1
    # a record with a live journal intent is never documented (C02 W2)
    with pytest.raises(AppDocError) as excinfo:
        await _service(store).record_adoption(_record(stale=True))
    assert excinfo.value.code is AppDocErrorCode.STALE_RECORD
    assert len(_upsert_intents(store)) == 1


@pytest.mark.anyio
async def test_verified_removal_with_tombstone_retires_docs_and_queues_deletes(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    store = DocumentStore(tmp_path)
    service = _service(store)
    await service.record_adoption(_record())
    await service.record_install(
        recipe=recipe, target=TARGET, result=_install_result(_plan(recipe))
    )
    outcome = await service.record_remove(
        _remove_result(_plan(recipe), manifest_removed=True)
    )
    assert outcome.event is DocEvent.RETIRED
    assert outcome.documented
    retired_ids = {item.doc_id for item in outcome.retired}
    assert retired_ids == {
        state_doc_id(SERVER_ID, APP_ID),
        record_doc_id(SERVER_ID, APP_ID),
    }
    assert all(item.intent.tombstone for item in outcome.retired)
    for item in outcome.retired:
        assert item.intent.op == "delete"
    # canonical content gone; durable tombstone intents remain for the indexer
    with pytest.raises(DocumentStoreError) as runbook_gone:
        _runbook(store)
    assert runbook_gone.value.code is DocumentStoreErrorCode.NOT_FOUND
    with pytest.raises(DocumentStoreError) as record_gone:
        _record_doc(store)
    assert record_gone.value.code is DocumentStoreErrorCode.NOT_FOUND
    tombstones = [
        intent
        for intent in store.read_intents(server_id=SERVER_ID, app_id=APP_ID)
        if intent.tombstone
    ]
    assert {intent.doc_id for intent in tombstones} == retired_ids


@pytest.mark.anyio
async def test_reinstall_after_retirement_recreates_from_exact_tombstone(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    store = DocumentStore(tmp_path)
    service = _service(store)
    await service.record_install(
        recipe=recipe, target=TARGET, result=_install_result(_plan(recipe))
    )
    await service.record_remove(_remove_result(_plan(recipe)))
    outcome = await service.record_install(
        recipe=recipe, target=TARGET, result=_install_result(_plan(recipe))
    )
    assert outcome.event is DocEvent.RUNBOOK
    doc = _runbook(store)
    assert doc.revision == 2  # tombstone revision (1) + 1, never a silent reset


@pytest.mark.anyio
async def test_reconcile_hook_runs_after_writes_and_is_absent_honestly(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    store = DocumentStore(tmp_path)
    calls: list[int] = []

    async def reconcile() -> list[str]:
        calls.append(1)
        return ["reconciled"]

    hooked = ApplicationDocService(store, reconcile=reconcile)
    outcome = await hooked.record_install(
        recipe=recipe, target=TARGET, result=_install_result(_plan(recipe))
    )
    assert outcome.reindex == ("reconciled",)
    assert len(calls) == 1
    # without the hook the durable intent still exists; only the immediate
    # indexing is absent (never a fabricated "reindexed" claim)
    plain = await _service(store).record_install(
        recipe=recipe,
        target=TARGET,
        result=_install_result(_plan(recipe)),
    )
    assert plain.reindex is None
    assert plain.reindex_queued


# ---------------------------------------------------------------------------
# AC2: failed/uncertain records incidents, never overwrites verified state
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_failed_update_records_incident_and_preserves_verified_runbook(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    recipe_v2 = _recipe(revision=1)
    store = DocumentStore(tmp_path)
    service = _service(store)
    await service.record_install(
        recipe=recipe, target=TARGET, result=_install_result(_plan(recipe))
    )
    verified_before = _runbook(store)
    intents_before = {intent.doc_id for intent in _upsert_intents(store)}
    outcome = await service.record_update(
        recipe=recipe_v2,
        target=TARGET,
        result=_update_result(
            _plan(recipe_v2),
            new_image=recipe_v2.image,
            status="failed",
            effect="executed",
            reason="apply_failed:compose.up",
            rollback_attempted=True,
            rollback_verified=True,
        ),
    )
    assert outcome.event is DocEvent.INCIDENT
    assert outcome.verification is VerificationStatus.VERIFIED
    incident = outcome.document
    assert incident is not None
    assert incident.namespace is DocumentNamespace.DOCS
    assert "# Lifecycle incident: update failed" in incident.text
    assert "reason: apply_failed:compose.up" in incident.text
    assert "rollback attempted yes; verified yes" in incident.text
    # the old verified state is NOT overwritten as a new success
    verified_after = _runbook(store)
    assert verified_after == verified_before
    assert verified_after.revision == 1
    assert f"image: `{recipe.image}`" in verified_after.text
    # exactly one new durable intent: the incident upsert (runbook untouched)
    upserts = _upsert_intents(store)
    new_intents = [intent for intent in upserts if intent.doc_id not in intents_before]
    assert [intent.doc_id for intent in new_intents] == [incident.doc_id]
    assert incident.doc_id == incident_doc_id(
        SERVER_ID, APP_ID, LifecyclePhase.UPDATE, _plan(recipe_v2).plan_sha256, "failed"
    )


@pytest.mark.anyio
async def test_outcome_unknown_records_hypothesis_incident(tmp_path: Path) -> None:
    recipe = _recipe()
    store = DocumentStore(tmp_path)
    service = _service(store)
    await service.record_install(
        recipe=recipe, target=TARGET, result=_install_result(_plan(recipe))
    )
    outcome = await service.record_update(
        recipe=recipe,
        target=TARGET,
        result=_update_result(
            _plan(recipe),
            new_image=recipe.image,
            status="outcome_unknown",
            effect="unknown",
            reason="outcome_unknown:transport_lost",
        ),
    )
    assert outcome.event is DocEvent.INCIDENT
    assert outcome.verification is VerificationStatus.HYPOTHESIS
    incident = outcome.document
    assert incident is not None
    assert "verification: hypothesis" in incident.text
    assert "effect: unknown" in incident.text
    assert _runbook(store).revision == 1  # verified runbook preserved


@pytest.mark.anyio
async def test_failed_install_without_prior_success_writes_no_runbook(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    store = DocumentStore(tmp_path)
    outcome = await _service(store).record_install(
        recipe=recipe,
        target=TARGET,
        result=_install_result(
            _plan(recipe),
            status="failed",
            effect="executed",
            reason="apply_failed:compose.up",
        ),
    )
    assert outcome.event is DocEvent.INCIDENT
    with pytest.raises(DocumentStoreError) as excinfo:
        _runbook(store)
    assert excinfo.value.code is DocumentStoreErrorCode.NOT_FOUND
    assert outcome.document is not None  # the incident exists
    assert len(store.read_intents(server_id=SERVER_ID, app_id=APP_ID)) == 1


@pytest.mark.anyio
async def test_succeeded_without_verified_health_is_incident_not_runbook(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    store = DocumentStore(tmp_path)
    outcome = await _service(store).record_install(
        recipe=recipe,
        target=TARGET,
        result=_install_result(
            _plan(recipe),
            status="succeeded",
            effect="executed",
            health_verified=False,
        ),
    )
    # C07 rule 5 belt and braces: only the full verified-success
    # combination may write the authoritative runbook.
    assert outcome.event is DocEvent.INCIDENT
    with pytest.raises(DocumentStoreError) as excinfo:
        _runbook(store)
    assert excinfo.value.code is DocumentStoreErrorCode.NOT_FOUND


@pytest.mark.anyio
async def test_refusal_is_not_documented(tmp_path: Path) -> None:
    recipe = _recipe()
    store = DocumentStore(tmp_path)
    service = _service(store)
    for outcome in (
        await service.record_install(
            recipe=recipe,
            target=TARGET,
            result=_install_result(
                _plan(recipe), status="refused", effect="not_attempted",
                reason="policy_denied:install",
            ),
        ),
        await service.record_remove(
            _remove_result(
                _plan(recipe), status="refused", effect="not_attempted",
                reason="not_installed:absent",
            )
        ),
    ):
        assert outcome.event is DocEvent.NOT_DOCUMENTED
        assert not outcome.documented
        assert not outcome.reindex_queued
        assert outcome.document is None
    assert store.read_intents(server_id=SERVER_ID, app_id=APP_ID) == []


@pytest.mark.anyio
async def test_repeated_same_plan_failures_converge_other_statuses_split(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    store = DocumentStore(tmp_path)
    service = _service(store)
    first = await service.record_install(
        recipe=recipe,
        target=TARGET,
        result=_install_result(
            _plan(recipe), status="failed", effect="executed",
            reason="apply_failed:compose.up",
        ),
    )
    second = await service.record_install(
        recipe=recipe,
        target=TARGET,
        result=_install_result(
            _plan(recipe), status="failed", effect="executed",
            reason="apply_failed:compose.up",
        ),
    )
    assert second.document is not None and first.document is not None
    assert second.document.doc_id == first.document.doc_id
    assert second.document.revision == 2  # same incident record, bumped
    unknown = await service.record_install(
        recipe=recipe,
        target=TARGET,
        result=_install_result(
            _plan(recipe), status="outcome_unknown", effect="unknown",
            reason="outcome_unknown:transport_lost",
        ),
    )
    assert unknown.document is not None
    assert unknown.document.doc_id != first.document.doc_id


@pytest.mark.anyio
async def test_failed_removal_records_incident_and_preserves_docs(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    store = DocumentStore(tmp_path)
    service = _service(store)
    await service.record_adoption(_record())
    await service.record_install(
        recipe=recipe, target=TARGET, result=_install_result(_plan(recipe))
    )
    runbook_before = _runbook(store)
    outcome = await service.record_remove(
        _remove_result(
            _plan(recipe),
            status="failed",
            effect="executed",
            reason="apply_failed:compose.down",
            manifest_removed=False,
        )
    )
    assert outcome.event is DocEvent.INCIDENT
    assert "declared data intent: keep" in (outcome.document.text if outcome.document else "")
    assert _runbook(store) == runbook_before
    assert _record_doc(store).revision == 1


@pytest.mark.anyio
async def test_verified_removal_with_conflicted_tombstone_rewrites_runbook(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    store = DocumentStore(tmp_path)
    service = _service(store)
    await service.record_install(
        recipe=recipe, target=TARGET, result=_install_result(_plan(recipe))
    )
    outcome = await service.record_remove(
        _remove_result(_plan(recipe), manifest_removed=False)
    )
    assert outcome.event is DocEvent.RUNBOOK
    assert outcome.verification is VerificationStatus.VERIFIED
    doc = _runbook(store)
    assert doc.revision == 2
    assert "# Application operational record: REMOVED" in doc.text
    assert "canonical tombstone: NOT reconciled" in doc.text
    assert "declared data intent: keep" in doc.text


# ---------------------------------------------------------------------------
# Identity binding, isolation, constructor checks, determinism
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_receipt_hash_binds_the_documented_plan(tmp_path: Path) -> None:
    recipe = _recipe()
    recipe_v2 = _recipe(revision=1)
    store = DocumentStore(tmp_path)
    # receipt produced from v1 but presented with the v2 recipe: the
    # rebuilt plan hash differs -> altered-payload refusal, no writes
    stale_receipt = _install_result(_plan(recipe))
    with pytest.raises(AppDocError) as excinfo:
        await _service(store).record_install(
            recipe=recipe_v2, target=TARGET, result=stale_receipt
        )
    assert excinfo.value.code is AppDocErrorCode.PLAN_MISMATCH
    assert store.read_intents(server_id=SERVER_ID, app_id=APP_ID) == []


@pytest.mark.anyio
async def test_same_app_on_two_servers_never_shares_docs(tmp_path: Path) -> None:
    recipe = _recipe()
    store = DocumentStore(tmp_path)
    service = _service(store)
    await service.record_install(
        recipe=recipe, target=TARGET, result=_install_result(_plan(recipe))
    )
    await service.record_install(
        recipe=recipe,
        target=TARGET,
        result=_install_result(_plan(recipe, server_id=SERVER_2_ID)),
    )
    first = _runbook(store)
    second = _runbook(store, server_id=SERVER_2_ID)
    assert first.doc_id != second.doc_id
    assert first.server_id == SERVER_ID and second.server_id == SERVER_2_ID
    with pytest.raises(DocumentStoreError) as cross:
        store.read(
            state_doc_id(SERVER_2_ID, APP_ID),
            server_id=SERVER_ID,
            app_id=APP_ID,
            namespace=DocumentNamespace.RUNBOOKS,
        )
    assert cross.value.code is DocumentStoreErrorCode.NOT_FOUND


def test_runbook_render_is_deterministic() -> None:
    recipe = _recipe(STATEFUL_FILE)
    plan = _plan(recipe)
    facts = RunbookFacts(
        phase=LifecyclePhase.INSTALL,
        name=recipe.name,
        plan=plan,
        image=plan.image,
        prior_image=None,
        migrations=recipe.update.migrations.value,
        image_rollback=recipe.recovery.image_rollback,
        manifest_revision=3,
        manifest_sha256="a" * 64,
        published_ports=(18080,),
        rollback_attempted=False,
        rollback_verified=False,
        snapshot_paths=(),
        notes=("rollback after failed operation: wait_healthy",),
    )
    assert render_runbook(facts) == render_runbook(facts)
    text = render_runbook(facts)
    # the stateful fixture proves data-location rendering (it declares no
    # credentials, so the honest "none declared" line must appear)
    assert "srv-files: container /srv host" in text
    assert "credentials: none declared" in text


@pytest.mark.anyio
async def test_constructor_and_receipt_type_checks(tmp_path: Path) -> None:
    store = DocumentStore(tmp_path)
    with pytest.raises(AppDocError) as store_check:
        ApplicationDocService("not-a-store")  # type: ignore[arg-type]
    assert store_check.value.code is AppDocErrorCode.INVALID_REQUEST
    with pytest.raises(AppDocError) as reconcile_check:
        ApplicationDocService(store, reconcile="not-callable")  # type: ignore[arg-type]
    assert reconcile_check.value.code is AppDocErrorCode.INVALID_REQUEST
    service = _service(store)
    recipe = _recipe()
    with pytest.raises(AppDocError) as receipt_check:
        await service.record_install(
            recipe=recipe,
            target=TARGET,
            result="not-a-receipt",  # type: ignore[arg-type]
        )
    assert receipt_check.value.code is AppDocErrorCode.INVALID_RECEIPT
    with pytest.raises(AppDocError) as adoption_check:
        await service.record_adoption("not-a-record")  # type: ignore[arg-type]
    assert adoption_check.value.code is AppDocErrorCode.INVALID_RECEIPT


@pytest.mark.anyio
async def test_foreign_identity_receipt_is_refused(tmp_path: Path) -> None:
    recipe = _recipe()
    store = DocumentStore(tmp_path)
    service = _service(store)
    result = _install_result(_plan(recipe))
    forged = InstallResult(
        server_id="not-a-uuid",  # type: ignore[arg-type]
        application_id=result.application_id,
        plan_sha256=result.plan_sha256,
        status=result.status,
        effect=result.effect,
        reason=None,
        health_verified=True,
    )
    with pytest.raises(AppDocError) as excinfo:
        await service.record_install(recipe=recipe, target=TARGET, result=forged)
    assert excinfo.value.code is AppDocErrorCode.INVALID_RECEIPT


def _meta_text(store: DocumentStore) -> str:
    """Read the canonical metadata sidecar through the store's layout."""
    meta = (
        store.root
        / "servers"
        / str(SERVER_ID)
        / "applications"
        / str(APP_ID)
        / "runbooks"
        / f"{state_doc_id(SERVER_ID, APP_ID)}.doc.meta.json"
    )
    return meta.read_text(encoding="utf-8")
