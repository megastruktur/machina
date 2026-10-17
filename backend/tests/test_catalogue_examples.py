"""Focused tests for the maintained catalogue fixtures (card 08-fixture-recipes).

Acceptance coverage (card mai-08-fixture-recipes):

- **AC1** — the example recipes are curated, digest-pinned and validated
  through the canonical schema (``parse_recipe_text``): each catalogue file
  is a two-document revision stream (v1 initial install, v2 image-only
  upgrade), every revision round-trips canonically, declares health /
  data / update / removal / recovery / backup semantics and bounded
  resources (one unprivileged port, at most one volume, no credentials,
  documents under the byte cap).
- **AC2** — the stateful fixture carries an identifiable persistent
  sentinel (the single volume ``srv-files`` on the served root ``/srv``,
  planner-allocated below ``TARGET_APPS_ROOT``) with neighboring resource
  controls (``backup.required=true``, ``removal.data_policy=explicit_choice``,
  ``recovery.image_rollback=true``); the upstream is a generic test
  fixture image, never a production application of this product.

Negative controls prove the curation is load-bearing, not decorative:
the raw two-document stream text must NOT parse as one recipe
(explicit-split convention), a tag-mutant image is rejected as unpinned
and a ``delete`` mutant removal policy is rejected outright. Plan-level
controls bind the fixtures to the frozen operation vocabulary on both
declared architectures and refuse undeclared targets.

Pure module, pure tests: no fixtures, no database, no docker, no network —
the catalogue files are read from the repository; everything else goes
through the pure parse -> render -> plan pipeline of
``mai.applications.recipes``.
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pytest
import yaml

from mai.applications.recipes import (
    MAX_DOCUMENT_BYTES,
    TARGET_APPS_ROOT,
    ApplicationRecipe,
    RecipeError,
    RecipeErrorCode,
    RemovalDataPolicy,
    TargetEnvironment,
    UpdateMigrations,
    build_deployment_plan,
    parse_recipe_text,
    recipe_sha256,
    render_recipe,
)

#: The catalogue lives at repository root, beside ``backend/``.
REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOGUE_DIR = REPO_ROOT / "catalogue"

#: Digest pins are the safety-critical fixture content: registry-verified
#: multi-arch manifest-list digests (linux amd64 + arm64) of upstream
#: filebrowser/filebrowser v2.31.2 (v1) and v2.32.0 (v2). Any drift fails
#: here, loudly.
V1_IMAGE = (
    "docker.io/filebrowser/filebrowser"
    "@sha256:a4da9ca8364b0a1fc5dd36f7add92582bf673c0ae0bda8dd9bd13062c41d1715"
)
V2_IMAGE = (
    "docker.io/filebrowser/filebrowser"
    "@sha256:593478e3c24c5ea9f5d7478dc549965b7bc7030707291006ce8d0b6162d3454b"
)

HTTP_FILE = "example-http.yaml"
STATEFUL_FILE = "example-stateful.yaml"
FIXTURE_FILES = (HTTP_FILE, STATEFUL_FILE)

TARGET_X86_64 = TargetEnvironment(os="linux", arch="x86_64")
TARGET_AARCH64 = TargetEnvironment(os="linux", arch="aarch64")

SERVER_ID = uuid.UUID("08080808-0808-4808-8808-080808080808")
HTTP_APP_ID = uuid.UUID("0bbbbbbb-0bbb-4bbb-8bbb-bbbbbbbbbbb1")
STATEFUL_APP_ID = uuid.UUID("0bbbbbbb-0bbb-4bbb-8bbb-bbbbbbbbbbb2")


def _stream_text(filename: str) -> str:
    return (CATALOGUE_DIR / filename).read_text(encoding="utf-8")


def _document_texts(filename: str) -> list[str]:
    """Split one catalogue revision stream into its YAML documents.

    The split is node-level (``compose_all`` + ``serialize``) so the strict
    per-document validation still happens inside ``parse_recipe_text``.
    """
    return [yaml.serialize(node) for node in yaml.compose_all(_stream_text(filename))]


def _revisions(filename: str) -> tuple[ApplicationRecipe, ApplicationRecipe]:
    documents = _document_texts(filename)
    assert len(documents) == 2, f"{filename} must hold exactly two revisions"
    return parse_recipe_text(documents[0]), parse_recipe_text(documents[1])


def _mutant(filename: str, mutate) -> str:
    """One parseable document of the stream with one curated field mutated."""
    document = yaml.safe_load(_document_texts(filename)[0])
    mutate(document)
    return yaml.safe_dump(document)


@pytest.fixture(params=FIXTURE_FILES)
def revisions_pair(request: pytest.FixtureRequest) -> tuple[ApplicationRecipe, ApplicationRecipe]:
    return _revisions(request.param)


# ---------------------------------------------------------------------------
# AC1: curated revision streams validate through the canonical schema
# ---------------------------------------------------------------------------


def test_revision_stream_is_v1_then_v2(revisions_pair) -> None:
    v1, v2 = revisions_pair
    assert v1.name == v2.name
    assert v1.image == V1_IMAGE
    assert v2.image == V2_IMAGE
    assert v1.image != v2.image


def test_revisions_round_trip_through_canonical_rendering(revisions_pair) -> None:
    v1, v2 = revisions_pair
    for recipe in (v1, v2):
        assert parse_recipe_text(render_recipe(recipe)) == recipe
    assert recipe_sha256(v1) == recipe_sha256(parse_recipe_text(render_recipe(v1)))
    assert recipe_sha256(v1) != recipe_sha256(v2)


def test_upgrade_revision_differs_only_in_the_pinned_digest(revisions_pair) -> None:
    v1, v2 = revisions_pair
    assert dataclasses.replace(v1, image=v2.image) == v2


def test_examples_are_bounded_resources(revisions_pair) -> None:
    v1, _ = revisions_pair
    assert len(v1.ports) == 1
    binding = v1.ports[0]
    assert binding.protocol == "tcp"
    assert 1024 <= binding.host <= 65535
    assert len(v1.volumes) <= 1
    assert v1.credentials == ()
    assert len(v1.prerequisites) <= 4
    assert len(v1.required_grants) <= 4
    assert len(_stream_text(HTTP_FILE).encode()) <= MAX_DOCUMENT_BYTES
    assert len(_stream_text(STATEFUL_FILE).encode()) <= MAX_DOCUMENT_BYTES


def test_declared_health_probe_is_image_factual(revisions_pair) -> None:
    v1, v2 = revisions_pair
    for recipe in (v1, v2):
        assert recipe.health is not None
        assert recipe.health.port == 80
        assert recipe.health.path == "/health"
        assert recipe.health.expected_status == 200
        assert recipe.health.interval_seconds == 10
        assert recipe.health.timeout_seconds == 5
        assert recipe.health.retries == 3
        assert recipe.health.start_period_seconds == 10
        assert any(p.container == 80 and p.protocol == "tcp" for p in recipe.ports)


# ---------------------------------------------------------------------------
# AC2: stateless vs stateful semantics, sentinel + neighboring controls
# ---------------------------------------------------------------------------


def test_stateless_fixture_persists_nothing() -> None:
    v1, v2 = _revisions(HTTP_FILE)
    for recipe in (v1, v2):
        assert recipe.name == "example-http"
        assert recipe.volumes == ()
        assert recipe.credentials == ()
        assert recipe.backup.required is False
        assert recipe.removal.data_policy is RemovalDataPolicy.KEEP
        assert recipe.update.migrations is UpdateMigrations.NONE


def test_stateful_fixture_declares_sentinel_volume_and_controls() -> None:
    v1, v2 = _revisions(STATEFUL_FILE)
    for recipe in (v1, v2):
        assert recipe.name == "example-stateful"
        assert len(recipe.volumes) == 1
        sentinel = recipe.volumes[0]
        assert sentinel.name == "srv-files"
        assert sentinel.container_path == "/srv"
        assert recipe.backup.required is True
        assert recipe.removal.data_policy is RemovalDataPolicy.EXPLICIT_CHOICE
        assert recipe.recovery.image_rollback is True
        assert recipe.update.migrations is UpdateMigrations.NONE
        assert recipe.ports[0].host == 18081


# ---------------------------------------------------------------------------
# Negative controls: the curation is load-bearing
# ---------------------------------------------------------------------------


def test_whole_stream_text_is_rejected_as_one_document() -> None:
    for filename in FIXTURE_FILES:
        with pytest.raises(RecipeError) as excinfo:
            parse_recipe_text(_stream_text(filename))
        assert excinfo.value.code is RecipeErrorCode.UNPARSABLE_DOCUMENT


def test_tag_mutant_image_is_rejected_unpinned() -> None:
    def tag_image(document: dict[str, object]) -> None:
        deploy = document["deploy"]
        assert isinstance(deploy, dict)
        deploy["image"] = "docker.io/filebrowser/filebrowser:v2.31.2"

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(HTTP_FILE, tag_image))
    assert excinfo.value.code is RecipeErrorCode.UNPINNED_IMAGE


def test_delete_mutant_removal_policy_is_rejected() -> None:
    def force_delete(document: dict[str, object]) -> None:
        removal = document["removal"]
        assert isinstance(removal, dict)
        removal["data_policy"] = "delete"

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(STATEFUL_FILE, force_delete))
    assert excinfo.value.code is RecipeErrorCode.DATA_DELETE_FORBIDDEN


def test_declared_environment_gates_planning() -> None:
    recipe = _revisions(STATEFUL_FILE)[0]
    with pytest.raises(RecipeError) as os_exc:
        build_deployment_plan(
            recipe, SERVER_ID, STATEFUL_APP_ID, TargetEnvironment(os="windows", arch="x86_64")
        )
    assert os_exc.value.code is RecipeErrorCode.UNSUPPORTED_TARGET_OS
    with pytest.raises(RecipeError) as arch_exc:
        build_deployment_plan(
            recipe, SERVER_ID, STATEFUL_APP_ID, TargetEnvironment(os="linux", arch="arm")
        )
    assert arch_exc.value.code is RecipeErrorCode.UNSUPPORTED_TARGET_ARCH


# ---------------------------------------------------------------------------
# Plan binding: fixtures produce policy-bound install/update plans
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "application_id", "host_port"),
    [(HTTP_FILE, HTTP_APP_ID, 18080), (STATEFUL_FILE, STATEFUL_APP_ID, 18081)],
)
def test_plans_bind_pinned_images_and_operations(
    filename: str, application_id: uuid.UUID, host_port: int
) -> None:
    v1, v2 = _revisions(filename)
    for recipe, image in ((v1, V1_IMAGE), (v2, V2_IMAGE)):
        for target in (TARGET_X86_64, TARGET_AARCH64):
            plan = build_deployment_plan(recipe, SERVER_ID, application_id, target)
            assert plan.image == image
            assert plan.recipe_sha256 == recipe_sha256(recipe)
            assert f"{host_port}:80/tcp" in plan.compose_text
            assert "CMD-SHELL" not in plan.compose_text
            assert "$" not in plan.compose_text
            operations = [op.op for op in plan.operations]
            assert operations[0] == "verify_prerequisites"
            assert "pull_image" in operations
            assert "wait_healthy" in operations
            pull = next(op for op in plan.operations if op.op == "pull_image")
            assert pull.subject == image
            wait = next(op for op in plan.operations if op.op == "wait_healthy")
            assert wait.subject == "http://127.0.0.1:80/health"
            # no credentials declared -> no credentials file rendered
            assert plan.credentials_path is None
            assert "env_file" not in plan.compose_text


def test_stateful_plan_allocates_the_sentinel_host_path() -> None:
    recipe = _revisions(STATEFUL_FILE)[0]
    plan = build_deployment_plan(recipe, SERVER_ID, STATEFUL_APP_ID, TARGET_X86_64)
    sentinel_host = (
        f"{TARGET_APPS_ROOT}/{STATEFUL_APP_ID}/volumes/srv-files"
    )
    assert sentinel_host in plan.directories
    assert plan.volumes[0].host_path == sentinel_host
    assert "require_backup" in [op.op for op in plan.operations]
    assert any("backup of persistent data" in limit for limit in plan.limitations)


def test_stateless_plan_carries_no_backup_or_volume_operations() -> None:
    recipe = _revisions(HTTP_FILE)[0]
    plan = build_deployment_plan(recipe, SERVER_ID, HTTP_APP_ID, TARGET_X86_64)
    assert plan.volumes == ()
    assert "require_backup" not in [op.op for op in plan.operations]
    assert any("keeps all persistent data" in limit for limit in plan.limitations)
