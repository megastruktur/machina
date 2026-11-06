"""Focused tests for explicit application adoption (card ``08-adoption``).

Acceptance coverage:

- **AC1** — an explicit authorized adoption creates the canonical manifest
  at ``servers/<server_id>/applications/<application_id>/manifest.yaml``
  plus its C02 version-triple sidecar, reconciles the SQL projection, and
  never moves deployment files: the external paths are recorded as
  references while real sentinel files at a real directory outside the data
  root stay byte-identical and the store tree contains exactly the canonical
  pair.
- **AC2** — duplicate/stale adoption is handled with structured, value-free
  conflicts (``already_adopted`` / ``stale_revision`` /
  ``reference_conflict`` — the ambiguity resolution deferred by discovery to
  this card — plus ``cross_server_conflict`` for a live identity under a
  different server), and discovery never implies mutation authority: the
  only adoption↔discovery bridge is ``managed_references()`` FEEDING
  :func:`mai.applications.discover.classify_managed`; no API accepts an
  inventory and mutates.

Boundary discipline (test policy, risk-critical slice): the projection, the
journal phases, the duplicate/conflict refusals and the crash-window
recovery claims run against a real disposable PostgreSQL fixture
(``MAI_TEST_DATABASE_URL``) with the real Alembic chain applied. The W1/W2
windows are produced at the exact seams where a real crash severs the
pipeline (file write / journal advance / reconcile raise before the next
step), then healed by the real ``recover()`` sweep against the real file
state — the negative controls stay mock-free. Store confinement (symlink
escape at every component, identity mismatch, interrupted pair, alias
bombs) runs pure against the filesystem. Documents carry TEST-NET paths and
a synthetic NOT-A-SECRET sentinel that must never appear in any error
string (value-free discipline).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
import yaml
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from mai.applications.adopt import (
    MANIFEST_FILENAME,
    MANIFEST_META_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    MAX_DOCUMENT_BYTES,
    AdoptionError,
    AdoptionErrorCode,
    AdoptionRequest,
    ApplicationAdoption,
    ApplicationManifestStore,
    canonical_manifest_relative_path,
    canonical_meta_relative_path,
    parse_manifest_text,
    render_manifest,
)
from mai.applications.discover import (
    ApplicationOrigin,
    DockerContainerObservation,
    ObservedApplication,
    PortMapping,
    StandaloneContainerObservation,
    SystemdUnitObservation,
    classify_managed,
    parse_docker_ps,
    group_docker_entries,
)
from mai.database import create_app_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Linear migration chain (exclusive migration-chain resource). Chain-tip
# reconciliation (card 10-observation-schema owns 0007): the tip constants
# are advanced here; later chain appenders advance them again in their own
# files.
REVISION_IDS = (
    "0001",
    "0002",
    "0003",
    "0004",
    "0005",
    "0006",
    "0007",
    "0008",
    "0009",
    "0010",
    "0011",
    "0012",
    "0013",
    "0014",
    "0015",
)

# The exact public application schema at the 0015 head: the 0007 set plus
# the 0011 tag catalog/job-tags and the D02 session-first tables; the
# obsolete chat/task/objective tables are dropped by 0015 — nothing else,
# in particular no engine-shadowing run-state table (C05 rule 1).
EXPECTED_APP_TABLES = frozenset(
    {
        "admin_credential",
        "admin_sessions",
        "alembic_version",
        "approvals",
        "profile_projection",
        "profile_projection_journal",
        "job_runs",
        "job_steps",
        "job_events",
        "job_schedules",
        "job_schedule_fires",
        "job_tags",
        "tags",
        "knowledge_documents",
        "knowledge_doc_chunks",
        "application_projection",
        "application_projection_journal",
        "server_observations",
        "observation_findings",
        "notification_alerts",
        "sessions",
        "session_entries",
        "session_runs",
        "session_admissions",
        "session_provider_artifacts",
        "session_tags",
    }
)

# Synthetic non-disclosure marker (NOT a secret): planted as manifest field
# values in the value-free discipline tests; no error string may carry it.
SENTINEL = "SENTINEL-mai-08-adopt-not-a-secret"

_JOURNAL_CHECKS = {
    "ck_application_projection_journal_op",
    "ck_application_projection_journal_phase",
    "ck_application_projection_journal_expected_revision",
    "ck_application_projection_journal_target_revision",
    "ck_application_projection_journal_upsert_targets",
    "ck_application_projection_journal_delete_targets",
    "ck_application_projection_journal_upsert_payload_prepared",
    "ck_application_projection_journal_payload_only_prepared",
}


# ---------------------------------------------------------------------------
# Fixtures: real disposable Postgres + real linear Alembic chain
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(ALEMBIC_INI))


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through head."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the adoption claims need a real "
            "disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            # Whole-schema reset (the sibling suites' approach): the
            # enumerated-drop list went stale across migrations; the
            # schema drop is FK-safe and immune to future revisions.
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
            conn.commit()
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture
async def sessions(migrated_url: str):
    engine, factory = create_app_engine(migrated_url)
    yield factory
    await engine.dispose()


@pytest.fixture
def store(tmp_path: Path) -> ApplicationManifestStore:
    return ApplicationManifestStore(tmp_path / "data-root")


@pytest.fixture
def adoption(sessions) -> ApplicationAdoption:
    return ApplicationAdoption(sessions)


@pytest.fixture
def server_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def application_id() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Request fixtures
# ---------------------------------------------------------------------------


def _compose_request(
    server_id: uuid.UUID | None = None,
    application_id: uuid.UUID | None = None,
    **overrides: object,
) -> AdoptionRequest:
    values: dict[str, object] = {
        "server_id": server_id or uuid.uuid4(),
        "application_id": application_id or uuid.uuid4(),
        "name": "build-website",
        "origin": ApplicationOrigin.COMPOSE,
        "compose_project": "build-website",
        "working_dir": "/srv/build-website",
        "unit_name": None,
        "external_working_dir": "/srv/build-website",
        "external_config_files": ("/srv/build-website/compose.yaml",),
        "external_environment_files": ("/srv/build-website/.env",),
        "expected_revision": 0,
    }
    values.update(overrides)
    return AdoptionRequest(**values)  # type: ignore[arg-type]


def _unit_request(
    server_id: uuid.UUID | None = None,
    application_id: uuid.UUID | None = None,
    **overrides: object,
) -> AdoptionRequest:
    values: dict[str, object] = {
        "server_id": server_id or uuid.uuid4(),
        "application_id": application_id or uuid.uuid4(),
        "name": "nginx-proxy",
        "origin": ApplicationOrigin.SYSTEMD_UNIT,
        "compose_project": None,
        "working_dir": None,
        "unit_name": "nginx.service",
        "external_working_dir": None,
        "external_config_files": (),
        "external_environment_files": (),
        "expected_revision": 0,
    }
    values.update(overrides)
    return AdoptionRequest(**values)  # type: ignore[arg-type]


async def _raw_scalar(sessions, sql: str, **params: object) -> object:
    async with sessions() as session:
        return (await session.execute(text(sql), params)).scalar()


# ---------------------------------------------------------------------------
# Migration chain + public schema at the 0006 tip
# ---------------------------------------------------------------------------


def test_migration_chain_is_linear_with_single_head(migrated_url: str) -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    assert script.get_heads() == [REVISION_IDS[-1]]
    revisions = {r.revision: r for r in script.walk_revisions()}
    assert sorted(revisions) == sorted(REVISION_IDS)
    assert revisions[REVISION_IDS[0]].down_revision is None
    for previous, following in zip(REVISION_IDS, REVISION_IDS[1:]):
        assert revisions[following].down_revision == previous


def test_migration_reupgrade_is_a_noop_and_records_the_revision(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAI_DATABASE_URL", migrated_url)
    command.upgrade(_alembic_config(), "head")  # second run on the same DB
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row == (REVISION_IDS[-1],)


def test_public_schema_at_tip_is_exactly_the_application_set(
    migrated_url: str,
) -> None:
    with psycopg.connect(migrated_url) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        ).fetchall()
    assert {row[0] for row in rows} == EXPECTED_APP_TABLES


def test_downgrade_round_trip_restores_the_head(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAI_DATABASE_URL", migrated_url)
    try:
        # D09: 0015's honest no-op downgrade means the oldest reachable
        # revision is 0014 (its downgrade only drops the session-first
        # tables; the dropped obsolete tables are never fabricated back).
        command.downgrade(_alembic_config(), "0014")
        with psycopg.connect(migrated_url) as conn:
            # The obsolete chat/task/objective tables stay absent — the
            # 0015 no-op downgrade fabricates nothing.
            assert not conn.execute(
                "SELECT to_regclass('public.conversations') IS NOT NULL"
            ).fetchone()[0]
        command.upgrade(_alembic_config(), "head")
    finally:
        command.upgrade(_alembic_config(), "head")
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row == (REVISION_IDS[-1],)


@pytest.mark.anyio
async def test_journal_carries_the_frozen_check_constraints(sessions) -> None:
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'application_projection_journal'::regclass "
                    "AND contype = 'c'"
                )
            )
        ).scalars()
    assert _JOURNAL_CHECKS <= set(rows)


# ---------------------------------------------------------------------------
# AC1 — explicit adoption: canonical manifest + projection, files untouched
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_adopt_creates_canonical_manifest_and_projection(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    sessions,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    request = _compose_request(server_id, application_id)
    result = await adoption.adopt(store, request)
    assert (result.outcome, result.op, result.revision) == ("applied", "upsert", 1)

    # Canonical pair exists at the C02 scheme, content matches the request.
    stored = store.read(server_id, application_id)
    assert stored.revision == 1
    assert stored.schema_revision == MANIFEST_SCHEMA_VERSION
    assert stored.sha256 == result.sha256
    manifest = parse_manifest_text(stored.text)
    assert manifest.name == "build-website"
    assert manifest.origin is ApplicationOrigin.COMPOSE
    assert manifest.compose_project == "build-website"
    assert manifest.working_dir == "/srv/build-website"
    assert manifest.external_config_files == ("/srv/build-website/compose.yaml",)

    # Projection row: same triple plus the manifest fields, never the text.
    record = await adoption.read(server_id, application_id)
    assert record is not None
    assert record.revision == stored.revision
    assert record.sha256 == stored.sha256
    assert record.schema_revision == MANIFEST_SCHEMA_VERSION
    assert record.name == "build-website"
    assert record.stale is False
    assert record.external_environment_files == ("/srv/build-website/.env",)

    # Journal: exactly one reconciled intent, payload NULLed (no second
    # document copy at rest).
    intents = await adoption.list_live_intents(server_id=server_id)
    assert intents == []
    assert (
        await _raw_scalar(
            sessions,
            "SELECT count(*) FROM application_projection_journal "
            "WHERE application_id = :a AND phase = 'reconciled' "
            "AND payload IS NULL",
            a=application_id,
        )
    ) == 1


@pytest.mark.anyio
async def test_adopt_never_touches_external_deployment_files(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    tmp_path: Path,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    # Real deployment directory OUTSIDE the data root with real files.
    deployment = tmp_path / "deployment"
    (deployment / "sub").mkdir(parents=True)
    files = [
        deployment / "compose.yaml",
        deployment / ".env",
        deployment / "sub" / "extra.conf",
    ]
    for index, path in enumerate(files):
        path.write_bytes(f"deployment fixture bytes {index}\n".encode())
    before = {path: path.read_bytes() for path in files}

    request = _compose_request(
        server_id,
        application_id,
        external_working_dir=str(deployment),
        external_config_files=(str(files[0]), str(files[2])),
        external_environment_files=(str(files[1]),),
    )
    await adoption.adopt(store, request)
    # Revocation exercises the only deletion path this card owns.
    await adoption.revoke(store, server_id, application_id, expected_revision=1)

    assert {path: path.read_bytes() for path in files} == before
    assert sorted(str(p.relative_to(deployment)) for p in deployment.rglob("*")) == [
        ".env",
        "compose.yaml",
        "sub",
        "sub/extra.conf",
    ]
    # The store holds exactly the canonical pair — no copies of deployment
    # data anywhere under the data root.
    found = {
        str(p.relative_to(store.root)) for p in store.root.rglob("*") if p.is_file()
    }
    assert found == set()  # revoked: pair removed, nothing else ever written


@pytest.mark.anyio
async def test_store_tree_contains_exactly_the_canonical_pair_after_adopt(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    tmp_path: Path,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (deployment / "compose.yaml").write_text("services: {}\n")
    request = _compose_request(
        server_id,
        application_id,
        external_working_dir=str(deployment),
        external_config_files=(str(deployment / "compose.yaml"),),
    )
    await adoption.adopt(store, request)
    found = {
        str(p.relative_to(store.root)) for p in store.root.rglob("*") if p.is_file()
    }
    assert found == {
        canonical_manifest_relative_path(server_id, application_id),
        canonical_meta_relative_path(server_id, application_id),
    }


@pytest.mark.anyio
async def test_managed_references_feed_discovery_not_the_other_way(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    # A discovery inventory over a docker ps payload (pure parse) containing
    # the adopted compose project and an unrelated systemd unit.
    ps_output = (
        '{"ID":"abc123","Names":"web-1","Image":"nginx:1.27","State":"running",'
        '"Status":"Up 1 min","Ports":"80/tcp","Labels":"'
        "com.docker.compose.project=build-website,"
        'com.docker.compose.project.working_dir=/srv/build-website,'
        'com.docker.compose.service=web"}\n'
    )
    parsed = parse_docker_ps(ps_output)
    inventory = [
        ObservedApplication(
            origin=ApplicationOrigin.COMPOSE, detail=group_docker_entries(parsed.entries)[0]
        ),
        ObservedApplication(
            origin=ApplicationOrigin.SYSTEMD_UNIT,
            detail=SystemdUnitObservation(unit="nginx.service", unit_file_state="enabled"),
        ),
    ]

    # Before adoption: nothing is managed.
    assert await adoption.managed_references(server_id) == ()
    assert all(not app.managed for app in classify_managed(tuple(inventory), ()))

    request = _compose_request(server_id, application_id)
    await adoption.adopt(store, request)
    references = await adoption.managed_references(server_id)
    assert len(references) == 1
    tagged = classify_managed(tuple(inventory), references)
    assert tagged[0].managed is True
    assert tagged[0].application_id == application_id
    assert tagged[1].managed is False  # the unrelated unit stays discovered

    # After revocation the record is gone: maintenance has no adopted record
    # and discovery tags nothing as managed.
    await adoption.revoke(store, server_id, application_id, expected_revision=1)
    assert await adoption.managed_references(server_id) == ()
    assert all(
        not app.managed for app in classify_managed(tuple(inventory), ())
    )


@pytest.mark.anyio
async def test_no_module_surface_accepts_a_discovery_inventory_for_mutation(
    adoption: ApplicationAdoption,
) -> None:
    # The mutation entrypoints take explicit decisions only; passing an
    # observed record where a request belongs is a structured rejection,
    # never an adoption.
    observed = ObservedApplication(
        origin=ApplicationOrigin.DOCKER_CONTAINER,
        detail=StandaloneContainerObservation(
            container=DockerContainerObservation(
                container_id="abc", name="n", image="i", state="running",
                status="", ports=(PortMapping(container_port=80, protocol="tcp"),),
            )
        ),
    )
    with pytest.raises(AdoptionError) as excinfo:
        await adoption.adopt(object(), observed)  # type: ignore[arg-type]
    assert excinfo.value.code is AdoptionErrorCode.INVALID_FIELD


# ---------------------------------------------------------------------------
# AC2 — duplicate / stale / ambiguous adoption
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_duplicate_create_refused_structurally(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    sessions,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    request = _compose_request(server_id, application_id)
    first = await adoption.adopt(store, request)
    with pytest.raises(AdoptionError) as excinfo:
        await adoption.adopt(store, request)  # expected_revision still 0
    assert excinfo.value.code is AdoptionErrorCode.ALREADY_ADOPTED
    # No second intent, no revision bump, no manifest rewrite.
    assert (
        await _raw_scalar(
            sessions,
            "SELECT count(*) FROM application_projection_journal "
            "WHERE application_id = :a",
            a=application_id,
        )
    ) == 1
    assert store.read(server_id, application_id).sha256 == first.sha256


@pytest.mark.anyio
async def test_cross_server_duplicate_adoption_refused(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    sessions,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    # Server A holds the live identity: projection row + canonical pair.
    first = await adoption.adopt(store, _compose_request(server_id, application_id))
    other_server = uuid.uuid4()
    # Server B adopting the SAME application id is a cross-server duplicate:
    # refused with a structured, value-free conflict — never a silent
    # relocation of the projection row (which would reset the revision chain
    # and orphan server A's manifest pair and managed references).
    with pytest.raises(AdoptionError) as excinfo:
        await adoption.adopt(store, _compose_request(other_server, application_id))
    assert excinfo.value.code is AdoptionErrorCode.CROSS_SERVER_CONFLICT
    assert str(other_server) not in str(excinfo.value)
    assert str(server_id) not in str(excinfo.value)
    # The row kept its owner: still server A's, revision chain unbroken.
    assert (
        await _raw_scalar(
            sessions,
            "SELECT server_id FROM application_projection "
            "WHERE application_id = :a",
            a=application_id,
        )
        == server_id
    )
    stored = store.read(server_id, application_id)
    assert (stored.revision, stored.sha256) == (1, first.sha256)
    # Exactly one canonical tree for the identity — server A's.
    assert store.servers_storing(application_id) == (server_id,)
    assert not (store.root / "servers" / str(other_server)).exists()
    # The refusal happened before any effect: no live intent for server B.
    assert (
        await _raw_scalar(
            sessions,
            "SELECT count(*) FROM application_projection_journal "
            "WHERE application_id = :a AND server_id = :s "
            "AND phase <> :reconciled",
            a=application_id,
            s=other_server,
            reconciled="reconciled",
        )
    ) == 0
    # Server A's discovery bridge survives; server B gained no references.
    assert len(await adoption.managed_references(server_id)) == 1
    assert await adoption.managed_references(other_server) == ()
    # Storage backstop: even with the projection row purged out of band
    # (crash-window shape — server A's canonical pair survives alone), the
    # canonical-tree half of the guard still refuses the relocation and the
    # surviving pair stays byte-identical at revision 1.
    async with sessions() as session, session.begin():
        await session.execute(
            text("DELETE FROM application_projection WHERE application_id = :a"),
            {"a": application_id},
        )
    with pytest.raises(AdoptionError) as excinfo:
        await adoption.adopt(store, _compose_request(other_server, application_id))
    assert excinfo.value.code is AdoptionErrorCode.CROSS_SERVER_CONFLICT
    assert store.read(server_id, application_id).revision == 1
    assert store.servers_storing(application_id) == (server_id,)


@pytest.mark.anyio
async def test_stale_caller_revision_conflicts(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    await adoption.adopt(store, _compose_request(server_id, application_id))
    with pytest.raises(AdoptionError) as excinfo:
        await adoption.adopt(
            store,
            _compose_request(server_id, application_id, expected_revision=99),
        )
    assert excinfo.value.code is AdoptionErrorCode.STALE_REVISION
    assert store.read(server_id, application_id).revision == 1


@pytest.mark.anyio
async def test_adopt_unknown_revision_with_no_record_conflicts(
    adoption: ApplicationAdoption,
    store: ApplicationManifestStore,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    with pytest.raises(AdoptionError) as excinfo:
        await adoption.adopt(
            store,
            _compose_request(server_id, application_id, expected_revision=3),
        )
    assert excinfo.value.code is AdoptionErrorCode.STALE_REVISION


@pytest.mark.anyio
async def test_ambiguous_compose_project_reference_refused(
    adoption: ApplicationAdoption,
    store: ApplicationManifestStore,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    await adoption.adopt(store, _compose_request(server_id, application_id))
    other = uuid.uuid4()
    with pytest.raises(AdoptionError) as excinfo:
        await adoption.adopt(
            store,
            _compose_request(server_id, other, compose_project="build-website"),
        )
    assert excinfo.value.code is AdoptionErrorCode.REFERENCE_CONFLICT
    assert await adoption.read(server_id, other) is None


@pytest.mark.anyio
async def test_ambiguous_working_dir_is_normalized_before_refusal(
    adoption: ApplicationAdoption,
    store: ApplicationManifestStore,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    # Trailing-slash-insensitive matching — the same normalization discovery
    # applies when tagging managed records.
    await adoption.adopt(store, _compose_request(server_id, application_id))
    with pytest.raises(AdoptionError) as excinfo:
        await adoption.adopt(
            store,
            _compose_request(
                server_id, uuid.uuid4(), working_dir="/srv/build-website///"
            ),
        )
    assert excinfo.value.code is AdoptionErrorCode.REFERENCE_CONFLICT


@pytest.mark.anyio
async def test_ambiguous_unit_name_reference_refused(
    adoption: ApplicationAdoption,
    store: ApplicationManifestStore,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    await adoption.adopt(store, _unit_request(server_id, application_id))
    with pytest.raises(AdoptionError) as excinfo:
        await adoption.adopt(
            store, _unit_request(server_id, uuid.uuid4(), unit_name="nginx.service")
        )
    assert excinfo.value.code is AdoptionErrorCode.REFERENCE_CONFLICT


@pytest.mark.anyio
async def test_disjoint_selectors_adopt_independently_and_list_stably(
    adoption: ApplicationAdoption,
    store: ApplicationManifestStore,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    second = uuid.uuid4()
    await adoption.adopt(store, _compose_request(server_id, application_id))
    await adoption.adopt(
        store, _unit_request(server_id, second, unit_name="nginx.service")
    )
    records = await adoption.list_adopted(server_id)
    assert [r.application_id for r in records] == sorted([application_id, second])
    assert {r.origin for r in records} == {
        ApplicationOrigin.COMPOSE,
        ApplicationOrigin.SYSTEMD_UNIT,
    }


@pytest.mark.anyio
async def test_update_with_observed_revision_bumps_and_preserves_adopted_at(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    sessions,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    await adoption.adopt(store, _compose_request(server_id, application_id))
    first = await adoption.read(server_id, application_id)
    assert first is not None
    renamed = _compose_request(
        server_id, application_id, name="renamed-app", expected_revision=1
    )
    result = await adoption.adopt(store, renamed)
    assert result.revision == 2
    record = await adoption.read(server_id, application_id)
    assert record is not None
    assert record.name == "renamed-app"
    assert record.revision == 2
    assert record.adopted_at == first.adopted_at  # first reconcile preserved
    assert record.reconciled_at >= first.reconciled_at
    assert store.read(server_id, application_id).revision == 2
    assert (
        await _raw_scalar(
            sessions,
            "SELECT count(*) FROM application_projection_journal "
            "WHERE application_id = :a AND phase = 'reconciled'",
            a=application_id,
        )
    ) == 2


# ---------------------------------------------------------------------------
# Revocation: tombstone first, purge after, nothing else touched
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_revoke_removes_record_and_manifest_but_keeps_journal_tombstone(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    sessions,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    await adoption.adopt(store, _compose_request(server_id, application_id))
    result = await adoption.revoke(
        store, server_id, application_id, expected_revision=1
    )
    assert (result.outcome, result.op) == ("applied", "delete")
    with pytest.raises(AdoptionError) as excinfo:
        store.read(server_id, application_id)
    assert excinfo.value.code is AdoptionErrorCode.NOT_FOUND
    assert await adoption.read(server_id, application_id) is None
    # The W4 tombstone row stays journaled (reconciled), proving the revoke
    # happened through the durable path.
    assert (
        await _raw_scalar(
            sessions,
            "SELECT count(*) FROM application_projection_journal "
            "WHERE application_id = :a AND op = 'delete' AND phase = 'reconciled'",
            a=application_id,
        )
    ) == 1


@pytest.mark.anyio
async def test_revoke_of_unknown_or_stale_record_conflicts(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    with pytest.raises(AdoptionError) as excinfo:
        await adoption.revoke(
            store, server_id, application_id, expected_revision=1
        )
    assert excinfo.value.code is AdoptionErrorCode.NOT_ADOPTED
    await adoption.adopt(store, _compose_request(server_id, application_id))
    with pytest.raises(AdoptionError) as excinfo:
        await adoption.revoke(
            store, server_id, application_id, expected_revision=42
        )
    assert excinfo.value.code is AdoptionErrorCode.STALE_REVISION


@pytest.mark.anyio
async def test_readopt_after_revocation_starts_a_fresh_record(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    await adoption.adopt(store, _compose_request(server_id, application_id))
    await adoption.revoke(store, server_id, application_id, expected_revision=1)
    result = await adoption.adopt(store, _compose_request(server_id, application_id))
    assert result.revision == 1
    record = await adoption.read(server_id, application_id)
    assert record is not None and record.revision == 1


# ---------------------------------------------------------------------------
# Crash windows: W1/W2 recovery at the real seams (real DB, real files)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_w2_crash_after_file_commit_recovers_from_file_truth(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    sessions,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _compose_request(server_id, application_id)

    async def crash_reconcile(self, intent, store):  # pragma: no cover - seam
        raise RuntimeError("simulated crash after file commit")

    monkeypatch.setattr(ApplicationAdoption, "_reconcile_upsert_from_file", crash_reconcile)
    with pytest.raises(RuntimeError):
        await adoption.adopt(store, request)
    monkeypatch.undo()

    # The manifest is authoritative (W2): file committed, journal advanced
    # past the payload, projection not yet written and the record is stale.
    stored = store.read(server_id, application_id)
    assert stored.revision == 1
    live = await adoption.list_live_intents(server_id=server_id)
    assert len(live) == 1
    assert live[0].phase == "file_committed"
    assert live[0].payload is None
    assert (
        await _raw_scalar(
            sessions,
            "SELECT count(*) FROM application_projection WHERE application_id = :a",
            a=application_id,
        )
    ) == 0

    outcomes = await adoption.recover(store, server_id=server_id)
    assert len(outcomes) == 1
    assert outcomes[0].status == "advanced_reconciled"
    record = await adoption.read(server_id, application_id)
    assert record is not None
    assert record.revision == 1
    assert record.sha256 == stored.sha256
    assert record.stale is False
    assert await adoption.list_live_intents(server_id=server_id) == []


@pytest.mark.anyio
async def test_w1_crash_before_file_commit_reexecutes_from_journal_payload(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _compose_request(server_id, application_id)

    def crash_write(self, *args: object, **kwargs: object):  # pragma: no cover
        raise RuntimeError("simulated crash before file commit")

    monkeypatch.setattr(ApplicationManifestStore, "write", crash_write)
    with pytest.raises(RuntimeError):
        await adoption.adopt(store, request)
    monkeypatch.undo()

    # W1: intent prepared WITH its payload (re-execution source), no file.
    live = await adoption.list_live_intents(server_id=server_id)
    assert len(live) == 1
    assert live[0].phase == "prepared"
    assert live[0].payload == render_manifest(request)
    with pytest.raises(AdoptionError):
        store.read(server_id, application_id)

    outcomes = await adoption.recover(store, server_id=server_id)
    assert len(outcomes) == 1
    assert outcomes[0].status == "reexecuted_reconciled"
    stored = store.read(server_id, application_id)
    assert stored.text == render_manifest(request)
    assert stored.sha256 == live[0].target_sha256
    record = await adoption.read(server_id, application_id)
    assert record is not None and record.revision == 1


@pytest.mark.anyio
async def test_w1_crash_after_replace_is_recognized_never_rewritten(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _compose_request(server_id, application_id)

    async def crash_advance(self, intent_id):  # pragma: no cover - seam
        raise RuntimeError("simulated crash after replace")

    monkeypatch.setattr(ApplicationAdoption, "_mark_file_committed", crash_advance)
    with pytest.raises(RuntimeError):
        await adoption.adopt(store, request)
    monkeypatch.undo()

    manifest_path = store.root / canonical_manifest_relative_path(server_id, application_id)
    bytes_before = manifest_path.read_bytes()

    outcomes = await adoption.recover(store, server_id=server_id)
    assert outcomes[0].status == "advanced_reconciled"
    assert outcomes[0].detail == "file already at intent target"
    assert manifest_path.read_bytes() == bytes_before  # never rewritten
    record = await adoption.read(server_id, application_id)
    assert record is not None and record.stale is False


@pytest.mark.anyio
async def test_w1_divergence_is_surfaced_never_overwritten(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _compose_request(server_id, application_id)

    def crash_write(self, *args: object, **kwargs: object):  # pragma: no cover
        raise RuntimeError("simulated crash before file commit")

    monkeypatch.setattr(ApplicationManifestStore, "write", crash_write)
    with pytest.raises(RuntimeError):
        await adoption.adopt(store, request)
    monkeypatch.undo()
    # Someone else writes a DIFFERENT manifest at the target revision while
    # the intent is live: neither at the intent target nor at the baseline.
    divergent = _compose_request(
        server_id, application_id, name="hostile-drift"
    )
    store.write(server_id, application_id, render_manifest(divergent), expected_revision=0)

    outcomes = await adoption.recover(store, server_id=server_id)
    assert outcomes[0].status == "conflict"
    assert outcomes[0].code == AdoptionErrorCode.FILE_DIVERGED.value
    assert store.read(server_id, application_id).revision == 1  # untouched


@pytest.mark.anyio
async def test_w4_resurrected_manifest_under_live_tombstone_conflicts(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _compose_request(server_id, application_id)
    await adoption.adopt(store, request)

    async def crash_purge(self, intent, store):  # pragma: no cover - seam
        raise RuntimeError("simulated crash during revoke")

    monkeypatch.setattr(ApplicationAdoption, "_reconcile_delete", crash_purge)
    with pytest.raises(RuntimeError):
        await adoption.revoke(store, server_id, application_id, expected_revision=1)
    monkeypatch.undo()
    # File half landed, projection purge did not; a live tombstone exists.
    # A record still exists (purge never ran), so the duplicate-create rule
    # fires before any tombstone interaction: structured, no second intent.
    with pytest.raises(AdoptionError) as excinfo:
        await adoption.adopt(store, request)
    assert excinfo.value.code is AdoptionErrorCode.ALREADY_ADOPTED

    # The manifest reappears while the tombstone is live: surfaced to the
    # admin, never silently overwritten (W4).
    store.write(server_id, application_id, render_manifest(request), expected_revision=0)
    outcomes = await adoption.recover(store, server_id=server_id)
    assert outcomes[0].status == "conflict"
    assert outcomes[0].code == AdoptionErrorCode.TOMBSTONE_CONFLICT.value


@pytest.mark.anyio
async def test_w4_file_deleted_projection_surviving_is_purged_on_recovery(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    sessions,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _compose_request(server_id, application_id)
    await adoption.adopt(store, request)

    def crash_delete(self, *args: object, **kwargs: object):  # pragma: no cover
        raise RuntimeError("simulated crash after tombstone")

    monkeypatch.setattr(ApplicationManifestStore, "delete", crash_delete)
    with pytest.raises(RuntimeError):
        await adoption.revoke(store, server_id, application_id, expected_revision=1)
    monkeypatch.undo()
    # Tombstone live, file still present, delete never executed.
    live = await adoption.list_live_intents(server_id=server_id)
    assert live[0].phase == "prepared"
    outcomes = await adoption.recover(store, server_id=server_id)
    assert outcomes[0].status == "reexecuted_reconciled"
    assert await adoption.read(server_id, application_id) is None
    with pytest.raises(AdoptionError):
        store.read(server_id, application_id)


@pytest.mark.anyio
async def test_intent_retry_with_same_id_is_idempotent(
    store: ApplicationManifestStore,
    adoption: ApplicationAdoption,
    sessions,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    request = _compose_request(server_id, application_id)
    intent_id = uuid.uuid4()
    first = await adoption.adopt(store, request, intent_id=intent_id)
    assert first.outcome == "applied"
    # Same id + same document = a completed retry: no new intent, no effect.
    retry = await adoption.adopt(store, request, intent_id=intent_id)
    assert retry.outcome == "already_reconciled"
    assert retry.revision == 1
    assert (
        await _raw_scalar(
            sessions,
            "SELECT count(*) FROM application_projection_journal "
            "WHERE application_id = :a",
            a=application_id,
        )
    ) == 1
    assert store.read(server_id, application_id).revision == 1


@pytest.mark.anyio
async def test_partial_unique_index_blocks_a_second_live_intent(
    adoption: ApplicationAdoption,
    sessions,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO application_projection_journal "
                "(intent_id, server_id, application_id, op, phase, expected_revision) "
                "VALUES (:i, :s, :a, 'delete', 'prepared', 0)"
            ),
            {"i": uuid.uuid4(), "s": server_id, "a": application_id},
        )
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO application_projection_journal "
                    "(intent_id, server_id, application_id, op, phase, "
                    "expected_revision, target_revision, target_sha256, "
                    "schema_revision, payload) VALUES "
                    "(:i, :s, :a, 'upsert', 'prepared', 0, 1, :sha, 1, :doc)"
                ),
                {
                    "i": uuid.uuid4(),
                    "s": server_id,
                    "a": application_id,
                    "sha": "0" * 64,
                    "doc": "payload",
                },
            )


@pytest.mark.anyio
async def test_projection_storage_rejects_non_adoptable_origin_and_reference(
    sessions,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> None:
    base = (
        "INSERT INTO application_projection (application_id, server_id, name, "
        "origin, compose_project, working_dir, unit_name, revision, "
        "schema_revision, sha256) VALUES (:a, :s, 'n', :o, :c, :w, :u, 1, 1, "
        "'0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef')"
    )
    async with sessions() as session, session.begin():
        with pytest.raises(IntegrityError):
            await session.execute(
                text(base),
                {"a": application_id, "s": server_id, "o": "docker_container",
                 "c": None, "w": None, "u": "nginx.service"},
            )
    async with sessions() as session, session.begin():
        with pytest.raises(IntegrityError):
            await session.execute(
                text(base),
                {"a": application_id, "s": server_id, "o": "compose",
                 "c": None, "w": None, "u": None},
            )


# ---------------------------------------------------------------------------
# Canonical manifest store: confinement, pairing, hardening (pure)
# ---------------------------------------------------------------------------


def _codes_of(excinfo: pytest.ExceptionInfo[AdoptionError]) -> AdoptionErrorCode:
    assert isinstance(excinfo.value, AdoptionError)
    return excinfo.value.code


def test_manifest_render_is_deterministic_and_round_trips() -> None:
    request = _compose_request()
    first = render_manifest(request)
    assert render_manifest(request) == first  # byte-stable
    manifest = parse_manifest_text(first)
    assert manifest.schema_version == MANIFEST_SCHEMA_VERSION
    assert manifest.server_id == request.server_id
    assert manifest.application_id == request.application_id
    assert manifest.name == "build-website"
    assert manifest.origin is ApplicationOrigin.COMPOSE
    assert manifest.unit_name is None
    assert manifest.external_working_dir == "/srv/build-website"
    assert manifest.external_config_files == ("/srv/build-website/compose.yaml",)


def test_manifest_preserves_unicode_and_verbatim_path_order() -> None:
    request = _compose_request(
        name="build wéb",
        external_config_files=(
            "/srv/app/compose.yaml",
            "/srv/app/compose.override.yaml",
            "/opt/späte Datei.yaml",
        ),
    )
    manifest = parse_manifest_text(render_manifest(request))
    assert manifest.name == "build wéb"
    assert manifest.external_config_files == request.external_config_files


def _manifest_text(**overrides: object) -> str:
    request = _compose_request(**overrides)
    return render_manifest(request)


def _mutated_document(mutation: object) -> str:
    document = yaml.safe_load(_manifest_text())
    if callable(mutation):
        mutation(document)
    return yaml.safe_dump(document)


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("not a mapping: [", AdoptionErrorCode.UNPARSABLE_DOCUMENT),
        ("- just\n- a\n- list\n", AdoptionErrorCode.INVALID_FIELD),
        (
            _mutated_document(lambda d: d.update(extra_field=1)),
            AdoptionErrorCode.UNKNOWN_FIELD,
        ),
        (
            _mutated_document(lambda d: d.pop("name")),
            AdoptionErrorCode.MISSING_FIELD,
        ),
        (
            _mutated_document(lambda d: d.update(schema_version=2)),
            AdoptionErrorCode.SCHEMA_VERSION_UNSUPPORTED,
        ),
        (
            _mutated_document(lambda d: d.update(server_id="not-a-uuid")),
            AdoptionErrorCode.INVALID_FIELD,
        ),
        (
            _mutated_document(lambda d: d.update(origin="docker_container")),
            AdoptionErrorCode.UNSUPPORTED_ORIGIN,
        ),
        (
            _mutated_document(lambda d: d.update(reference={})),
            AdoptionErrorCode.INVALID_REFERENCE,
        ),
        (
            _mutated_document(lambda d: d["reference"].update(unknown_selector="x")),
            AdoptionErrorCode.UNKNOWN_FIELD,
        ),
        (
            _mutated_document(lambda d: d["external_paths"].update(files=["/x"])),
            AdoptionErrorCode.UNKNOWN_FIELD,
        ),
        (
            _mutated_document(
                lambda d: d["external_paths"].update(config_files="not-a-list")
            ),
            AdoptionErrorCode.INVALID_FIELD,
        ),
        (
            _mutated_document(lambda d: d.update(name="")),
            AdoptionErrorCode.INVALID_NAME,
        ),
    ],
)
def test_manifest_parse_rejections_are_structural_and_value_free(
    document: str, expected: AdoptionErrorCode
) -> None:
    with pytest.raises(AdoptionError) as excinfo:
        parse_manifest_text(document)
    assert _codes_of(excinfo) is expected
    assert SENTINEL not in str(excinfo.value)


def test_parse_rejects_duplicate_mapping_keys() -> None:
    text = _manifest_text()
    duplicated = text.replace(
        "name: build-website", "name: build-website\nname: other"
    )
    with pytest.raises(AdoptionError) as excinfo:
        parse_manifest_text(duplicated)
    assert _codes_of(excinfo) is AdoptionErrorCode.UNPARSABLE_DOCUMENT


@pytest.mark.parametrize(
    "overrides",
    [
        {"server_id": "not-a-uuid"},
        {"application_id": 42},
        {"name": ""},
        {"name": "line\nbreak"},
        {"name": "x" * 201},
        {"origin": "compose"},  # raw string is not the frozen enum
        {"compose_project": None, "working_dir": None, "unit_name": None},
        {"external_config_files": ["has\nnewline"]},
        {"external_config_files": "not-a-tuple"},
        {"external_config_files": ("/p",) * 65},
        {"external_working_dir": "x" * 1025},
        {"expected_revision": -1},
        {"expected_revision": True},
    ],
)
def test_request_validation_rejects_before_any_effect(overrides: dict) -> None:
    with pytest.raises(AdoptionError):
        _compose_request(**overrides)


def test_request_refuses_standalone_container_origin() -> None:
    # Standalone containers have no frozen selector (discovery keeps them
    # discovered): adopting one would create an unmatchable record.
    with pytest.raises(AdoptionError) as excinfo:
        _compose_request(origin=ApplicationOrigin.DOCKER_CONTAINER)
    assert _codes_of(excinfo) is AdoptionErrorCode.UNSUPPORTED_ORIGIN


def test_request_accepts_systemd_unit_with_single_selector() -> None:
    request = _unit_request()
    assert request.unit_name == "nginx.service"
    manifest = parse_manifest_text(render_manifest(request))
    assert manifest.unit_name == "nginx.service"
    assert manifest.compose_project is None


def test_value_free_errors_never_carry_manifest_values(tmp_path: Path) -> None:
    secretish_name = SENTINEL
    secretish_dir = f"/srv/{SENTINEL}/deploy"
    request = _compose_request(
        server_id=uuid.uuid4(),
        application_id=uuid.uuid4(),
        name=secretish_name,
        working_dir=secretish_dir,
        compose_project=None,
        external_working_dir=None,
        external_config_files=(),
        external_environment_files=(),
        expected_revision=0,
    )
    document = render_manifest(request)
    assert secretish_name in document  # canonical DATA carries it, by design
    store = ApplicationManifestStore(tmp_path / "data-root")
    store.write(request.server_id, request.application_id, document, expected_revision=0)
    # A stale caller gets a structured conflict whose message names no
    # manifest field value (C01 rule 3 discipline at the storage boundary).
    with pytest.raises(AdoptionError) as excinfo:
        store.write(request.server_id, request.application_id, document, expected_revision=0)
    assert excinfo.value.code is AdoptionErrorCode.STALE_REVISION
    assert SENTINEL not in str(excinfo.value)
    assert SENTINEL not in excinfo.value.detail


# ---------------------------------------------------------------------------
# Store: revisioning, pairing, confinement
# ---------------------------------------------------------------------------


def test_store_write_read_round_trip_and_revision_bump(tmp_path: Path) -> None:
    store = ApplicationManifestStore(tmp_path / "data-root")
    server, application = uuid.uuid4(), uuid.uuid4()
    text = _manifest_text(server_id=server, application_id=application)
    stored = store.write(server, application, text, expected_revision=0)
    assert (stored.revision, stored.schema_revision) == (1, MANIFEST_SCHEMA_VERSION)
    updated = store.write(server, application, text, expected_revision=1)
    assert updated.revision == 2
    assert store.read(server, application).revision == 2


def test_store_meta_carries_frozen_version_triple(tmp_path: Path) -> None:
    store = ApplicationManifestStore(tmp_path / "data-root")
    server, application = uuid.uuid4(), uuid.uuid4()
    stored = store.write(
        server, application, _manifest_text(server_id=server, application_id=application),
        expected_revision=0,
    )
    meta = json.loads(
        (store.root / canonical_meta_relative_path(server, application)).read_bytes()
    )
    assert meta == {
        "format": 1,
        "server_id": str(server),
        "application_id": str(application),
        "revision": stored.revision,
        "schema_revision": MANIFEST_SCHEMA_VERSION,
        "sha256": stored.sha256,
    }


def test_store_permissions_are_restrictive(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    store = ApplicationManifestStore(root)
    server, application = uuid.uuid4(), uuid.uuid4()
    store.write(
        server, application, _manifest_text(server_id=server, application_id=application),
        expected_revision=0,
    )
    assert (root.stat().st_mode & 0o777) == 0o700
    app_dir = root / "servers" / str(server) / "applications" / str(application)
    assert (app_dir.stat().st_mode & 0o777) == 0o700
    assert ((app_dir.parent).stat().st_mode & 0o777) == 0o700
    for name in (MANIFEST_FILENAME, MANIFEST_META_FILENAME):
        assert ((app_dir / name).stat().st_mode & 0o777) == 0o600


def test_store_rejects_identity_mismatch_before_any_effect(tmp_path: Path) -> None:
    store = ApplicationManifestStore(tmp_path / "data-root")
    server, application = uuid.uuid4(), uuid.uuid4()
    foreign_application = uuid.uuid4()
    with pytest.raises(AdoptionError) as excinfo:
        store.write(
            server,
            application,
            _manifest_text(server_id=server, application_id=foreign_application),
            expected_revision=0,
        )
    assert _codes_of(excinfo) is AdoptionErrorCode.APPLICATION_ID_MISMATCH
    foreign_server = uuid.uuid4()
    with pytest.raises(AdoptionError) as excinfo:
        store.write(
            server,
            application,
            _manifest_text(server_id=foreign_server, application_id=application),
            expected_revision=0,
        )
    assert _codes_of(excinfo) is AdoptionErrorCode.SERVER_ID_MISMATCH
    found = {p.name for p in store.root.rglob("*")}
    assert found == set()  # nothing was written


def test_store_rejects_non_uuid_identities(tmp_path: Path) -> None:
    store = ApplicationManifestStore(tmp_path / "data-root")
    with pytest.raises(AdoptionError) as excinfo:
        store.write("server", uuid.uuid4(), _manifest_text(), expected_revision=0)  # type: ignore[arg-type]
    assert _codes_of(excinfo) is AdoptionErrorCode.INVALID_SERVER_ID
    with pytest.raises(AdoptionError) as excinfo:
        store.write(uuid.uuid4(), None, _manifest_text(), expected_revision=0)  # type: ignore[arg-type]
    assert _codes_of(excinfo) is AdoptionErrorCode.INVALID_APPLICATION_ID


def test_store_rejects_stale_and_orphan_states(tmp_path: Path) -> None:
    store = ApplicationManifestStore(tmp_path / "data-root")
    server, application = uuid.uuid4(), uuid.uuid4()
    text = _manifest_text(server_id=server, application_id=application)
    store.write(server, application, text, expected_revision=0)
    with pytest.raises(AdoptionError) as excinfo:
        store.write(server, application, text, expected_revision=0)
    assert _codes_of(excinfo) is AdoptionErrorCode.STALE_REVISION
    # Orphan metadata (metadata without manifest) blocks writes, allows the
    # completing delete.
    (store.root / canonical_manifest_relative_path(server, application)).unlink()
    with pytest.raises(AdoptionError) as excinfo:
        store.write(server, application, text, expected_revision=1)
    assert _codes_of(excinfo) is AdoptionErrorCode.ORPHAN_MANIFEST
    store.delete(server, application, expected_revision=1)
    with pytest.raises(AdoptionError) as excinfo:
        store.read(server, application)
    assert _codes_of(excinfo) is AdoptionErrorCode.NOT_FOUND


def test_store_reports_interrupted_pair_as_metadata_missing(tmp_path: Path) -> None:
    store = ApplicationManifestStore(tmp_path / "data-root")
    server, application = uuid.uuid4(), uuid.uuid4()
    store.write(
        server, application, _manifest_text(server_id=server, application_id=application),
        expected_revision=0,
    )
    (store.root / canonical_meta_relative_path(server, application)).unlink()
    with pytest.raises(AdoptionError) as excinfo:
        store.read(server, application)
    assert _codes_of(excinfo) is AdoptionErrorCode.METADATA_MISSING


def test_store_detects_external_tampering_via_hash(tmp_path: Path) -> None:
    store = ApplicationManifestStore(tmp_path / "data-root")
    server, application = uuid.uuid4(), uuid.uuid4()
    store.write(
        server, application, _manifest_text(server_id=server, application_id=application),
        expected_revision=0,
    )
    target = store.root / canonical_manifest_relative_path(server, application)
    target.write_text(
        _manifest_text(server_id=server, application_id=application, name="tampered")
    )
    with pytest.raises(AdoptionError) as excinfo:
        store.read(server, application)
    assert _codes_of(excinfo) is AdoptionErrorCode.HASH_MISMATCH


def test_store_rejects_stale_delete(tmp_path: Path) -> None:
    store = ApplicationManifestStore(tmp_path / "data-root")
    server, application = uuid.uuid4(), uuid.uuid4()
    store.write(
        server, application, _manifest_text(server_id=server, application_id=application),
        expected_revision=0,
    )
    with pytest.raises(AdoptionError) as excinfo:
        store.delete(server, application, expected_revision=7)
    assert _codes_of(excinfo) is AdoptionErrorCode.STALE_REVISION


def test_two_applications_and_two_servers_are_isolated(tmp_path: Path) -> None:
    # CROSS-SCOPE fixture obligation: canonical content never leaks across
    # applications or servers at the storage boundary.
    store = ApplicationManifestStore(tmp_path / "data-root")
    server, other_server = uuid.uuid4(), uuid.uuid4()
    application, other_application = uuid.uuid4(), uuid.uuid4()
    store.write(
        server, application, _manifest_text(server_id=server, application_id=application),
        expected_revision=0,
    )
    store.write(
        server,
        other_application,
        _manifest_text(server_id=server, application_id=other_application, name="other"),
        expected_revision=0,
    )
    store.write(
        other_server,
        application,
        _manifest_text(server_id=other_server, application_id=application, name="elsewhere"),
        expected_revision=0,
    )
    assert parse_manifest_text(store.read(server, application).text).name == "build-website"
    assert parse_manifest_text(store.read(server, other_application).text).name == "other"
    assert parse_manifest_text(store.read(other_server, application).text).name == "elsewhere"


def test_symlink_escape_rejected_at_every_component(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    store = ApplicationManifestStore(tmp_path / "data-root")
    server, application = uuid.uuid4(), uuid.uuid4()
    text = _manifest_text(server_id=server, application_id=application)

    # servers dir itself is a link
    (store.root / "servers").mkdir()
    (store.root / "servers").rmdir()
    (store.root / "servers").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AdoptionError) as excinfo:
        store.write(server, application, text, expected_revision=0)
    assert _codes_of(excinfo) is AdoptionErrorCode.PATH_ESCAPE
    (store.root / "servers").unlink()
    (store.root / "servers").mkdir()

    # application directory is a link
    app_dir = store.root / "servers" / str(server) / "applications"
    app_dir.mkdir(parents=True)
    app_dir.rmdir()
    app_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AdoptionError) as excinfo:
        store.write(server, application, text, expected_revision=0)
    assert _codes_of(excinfo) is AdoptionErrorCode.PATH_ESCAPE
    app_dir.unlink()
    app_dir.mkdir()

    # the canonical manifest file is a link pointing outside
    (app_dir / str(application)).mkdir()
    manifest = app_dir / str(application) / MANIFEST_FILENAME
    manifest.symlink_to(outside / "elsewhere.yaml")
    with pytest.raises(AdoptionError) as excinfo:
        store.write(server, application, text, expected_revision=0)
    assert _codes_of(excinfo) is AdoptionErrorCode.PATH_ESCAPE

    # the metadata sidecar is a link too
    meta = app_dir / str(application) / MANIFEST_META_FILENAME
    manifest.unlink()
    meta.symlink_to(outside / "elsewhere.json")
    with pytest.raises(AdoptionError) as excinfo:
        store.write(server, application, text, expected_revision=0)
    assert _codes_of(excinfo) is AdoptionErrorCode.PATH_ESCAPE


def test_symlinked_data_root_is_resolved_and_confined(tmp_path: Path) -> None:
    # C02 rule 1: the root is configured explicitly; a symlinked root path
    # resolves once at construction and stays confined afterwards.
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    link_root = tmp_path / "link-root"
    link_root.symlink_to(real_root, target_is_directory=True)
    store = ApplicationManifestStore(link_root)
    server, application = uuid.uuid4(), uuid.uuid4()
    store.write(
        server, application, _manifest_text(server_id=server, application_id=application),
        expected_revision=0,
    )
    assert store.root == real_root.resolve()
    assert (real_root / canonical_manifest_relative_path(server, application)).exists()


# ---------------------------------------------------------------------------
# Resource-exhaustion boundary (alias bombs, size, depth)
# ---------------------------------------------------------------------------


def test_alias_bomb_rejected_before_expansion(tmp_path: Path) -> None:
    store = ApplicationManifestStore(tmp_path / "data-root")
    server, application = uuid.uuid4(), uuid.uuid4()
    bomb = "a0: &a0 [" + ", ".join(["x"] * 10) + "]\n"
    for level in range(1, 6):
        bomb += (
            f"a{level}: &a{level} ["
            + ", ".join([f"*a{level - 1}"] * 10)
            + "]\n"
        )
    with pytest.raises(AdoptionError) as excinfo:
        store.write(server, application, bomb, expected_revision=0)
    assert _codes_of(excinfo) is AdoptionErrorCode.DOCUMENT_ALIAS_FORBIDDEN
    with pytest.raises(AdoptionError):
        store.read(server, application)


def test_oversized_manifest_rejected_before_parse(tmp_path: Path) -> None:
    store = ApplicationManifestStore(tmp_path / "data-root")
    server, application = uuid.uuid4(), uuid.uuid4()
    oversized = "name: " + "x" * MAX_DOCUMENT_BYTES
    with pytest.raises(AdoptionError) as excinfo:
        store.write(server, application, oversized, expected_revision=0)
    assert _codes_of(excinfo) is AdoptionErrorCode.DOCUMENT_TOO_LARGE


def test_deeply_nested_manifest_rejected_not_crashing(tmp_path: Path) -> None:
    store = ApplicationManifestStore(tmp_path / "data-root")
    server, application = uuid.uuid4(), uuid.uuid4()
    deep = "name: " + "[" * 30_000 + "]" * 30_000 + "\n"
    with pytest.raises(AdoptionError) as excinfo:
        store.write(server, application, deep, expected_revision=0)
    assert _codes_of(excinfo) is AdoptionErrorCode.DOCUMENT_TOO_DEEP


def test_quoted_anchor_characters_are_not_false_positives(tmp_path: Path) -> None:
    store = ApplicationManifestStore(tmp_path / "data-root")
    server, application = uuid.uuid4(), uuid.uuid4()
    text = _manifest_text(
        server_id=server, application_id=application, name="R&D * research [v1]"
    )
    stored = store.write(server, application, text, expected_revision=0)
    assert store.read(server, application).text == stored.text


# ---------------------------------------------------------------------------
# Reads: unknown identities are absent values, not errors
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_and_list_of_unknown_identities_are_empty(
    adoption: ApplicationAdoption, server_id: uuid.UUID
) -> None:
    assert await adoption.read(server_id, uuid.uuid4()) is None
    assert await adoption.list_adopted(server_id) == ()
    assert await adoption.managed_references(server_id) == ()


def test_canonical_path_helpers_accept_only_uuid_identities() -> None:
    with pytest.raises(TypeError):
        canonical_manifest_relative_path("server", uuid.uuid4())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        canonical_meta_relative_path(uuid.uuid4(), "app")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# uir-u4: per-server custom_instructions/installed + global Runbook Catalog
# ---------------------------------------------------------------------------


def test_service_map_entry_round_trips_custom_instructions_and_installed(
    tmp_path: Path,
) -> None:
    from mai.applications.service_map import ServiceMapStore

    store = ServiceMapStore(tmp_path / "service_map")
    server = uuid.uuid4()
    entry = store.upsert_entry(
        server,
        name="nginx",
        custom_instructions="This host proxies through Tailscale; reload with systemctl.",
        installed=True,
    )
    assert entry.custom_instructions.startswith("This host proxies")
    assert entry.installed is True
    reread = store.get_entry(server, uuid.UUID(entry.application_id))
    assert reread is not None
    assert reread.custom_instructions == entry.custom_instructions
    assert reread.installed is True
    # The staged (installed=False) state persists with the record.
    staged = store.upsert_entry(
        server,
        name="staged-service",
        installed=False,
    )
    reread_staged = store.get_entry(server, uuid.UUID(staged.application_id))
    assert reread_staged is not None and reread_staged.installed is False


def test_entry_without_new_fields_reads_with_backward_compatible_defaults(
    tmp_path: Path,
) -> None:
    import json as _json

    from mai.applications.service_map import ServiceMapStore

    store = ServiceMapStore(tmp_path / "service_map")
    server = uuid.uuid4()
    entry = store.upsert_entry(server, name="legacy")
    # Rewrite the record exactly as a pre-catalog store left it: no
    # custom_instructions/installed keys at all.
    path = store.root / "servers" / f"{server}.json"
    document = _json.loads(path.read_text(encoding="utf-8"))
    for record in document:
        record.pop("custom_instructions", None)
        record.pop("installed", None)
    path.write_text(_json.dumps(document), encoding="utf-8")
    reread = store.get_entry(server, uuid.UUID(entry.application_id))
    assert reread is not None
    assert reread.custom_instructions == ""
    assert reread.installed is True


def test_upsert_entry_preserves_server_local_fields_when_unnamed(
    tmp_path: Path,
) -> None:
    from mai.applications.service_map import ServiceMapStore

    store = ServiceMapStore(tmp_path / "service_map")
    server = uuid.uuid4()
    first = store.upsert_entry(
        server,
        name="jellyfin",
        description="media server",
        custom_instructions="host-specific note",
        installed=False,
    )
    # A later metadata-only upsert (no custom_instructions/installed named)
    # must never wipe the server-local state.
    second = store.upsert_entry(
        server,
        name="jellyfin",
        description="media server, updated",
        application_id=uuid.UUID(first.application_id),
    )
    assert second.custom_instructions == "host-specific note"
    assert second.installed is False
    assert second.revision == 2


def test_catalog_runbooks_seed_from_server_entries_and_persist(tmp_path: Path) -> None:
    from mai.applications.service_map import ServiceMapStore

    store = ServiceMapStore(tmp_path / "service_map")
    server = uuid.uuid4()
    entry = store.upsert_entry(server, name="pre-catalog service", category="media")
    runbooks = store.list_catalog_runbooks()
    assert [r.name for r in runbooks] == ["pre-catalog service"]
    synthesized = runbooks[0]
    assert synthesized.runbook_id == entry.application_id
    assert synthesized.category == "media"

    # A persisted catalog runbook of the same name shadows the synthesized
    # projection (one catalog record per name) and bumps revisions on re-save.
    created = store.upsert_catalog_runbook(
        name="Pre-Catalog Service", category="media", description="the recipe"
    )
    runbooks = store.list_catalog_runbooks()
    assert len(runbooks) == 1
    assert runbooks[0].runbook_id == created.runbook_id
    assert created.revision == 1
    updated = store.upsert_catalog_runbook(
        name="Pre-Catalog Service", description="the recipe, v2"
    )
    assert updated.runbook_id == created.runbook_id  # deterministic name id
    assert updated.revision == 2
    assert store.get_catalog_runbook(created.runbook_id) is not None

    # Deleting the catalog record is catalog-scoped: the server entry stays,
    # so the synthesized projection reappears (under the ENTRY id); the
    # deleted persisted id is gone and deleting again is a no-op.
    assert store.delete_catalog_runbook(created.runbook_id) is True
    assert store.get_catalog_runbook(created.runbook_id) is None
    assert [
        r.runbook_id for r in store.list_catalog_runbooks()
    ] == [entry.application_id]
    assert store.delete_catalog_runbook(created.runbook_id) is False
    assert store.get_entry(server, uuid.UUID(entry.application_id)) is not None


def test_catalog_upsert_validates_and_rejects_bad_records(tmp_path: Path) -> None:
    from mai.applications.service_map import ServiceMapError, ServiceMapStore

    store = ServiceMapStore(tmp_path / "service_map")
    with pytest.raises(ServiceMapError):
        store.upsert_catalog_runbook(name="")
    with pytest.raises(ServiceMapError):
        store.upsert_catalog_runbook(name="x", category="Not A Slug")
    with pytest.raises(ServiceMapError):
        store.upsert_catalog_runbook(name="x", instructions={"nope": "1"})
