"""Focused tests for the Odysseus maintained recipe (card 11-odysseus-recipe).

The maintained catalogue entry ``catalogue/odysseus.yaml`` is the
independently authored installation recipe for the upstream self-hosted AI
workspace (https://github.com/odysseus-dev/odysseus, AGPL-3.0-or-later):
authored from its DOCUMENTED Docker install interface (README quick start +
website/setup.md), never from its implementation (IDEA.md: inspiration only,
no code reuse). This module validates the recipe through the canonical
schema frozen by 08-recipe-schema (``parse_recipe_text`` ->
``render_recipe`` -> ``build_deployment_plan``), pins the digest pin, the
declared resource budget (hw.* tokens evaluated with the producer semantics
imported from the 11-model-recipe test module — one vocabulary, one
evaluator, no second convention), and the standalone single-service
curation: the schema v1 deploys exactly one service, and the upstream
bundled chromadb/searxng/ntfy stack must NOT be re-deployed beside MAI's
own production stack (C09, acceptance 2 — verified degraded-not-fatal on
this digest, provenance in mai_evidence/11-odysseus-recipe/).

Pure module: no Docker, no network, no I/O beyond the catalogue file.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from pathlib import Path

import pytest
import yaml

from mai.applications.recipes import (
    MAX_DOCUMENT_BYTES,
    OP_PULL_IMAGE,
    OP_START,
    OP_VERIFY_PREREQUISITES,
    PlannedVolume,
    PortBinding,
    RecipeError,
    RecipeErrorCode,
    RemovalDataPolicy,
    TargetEnvironment,
    ApplicationRecipe,
    build_deployment_plan,
    canonical_plan_json,
    parse_recipe_text,
    recipe_sha256,
    render_compose,
    render_recipe,
)
from test_inference_recipe import (
    _GIB,
    _HARDWARE_TOKEN_RE,
    APPROVED_TARGET_FACTS,
    evaluate_hardware_checks,
)

#: The catalogue lives at repository root, beside ``backend/``.
REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOGUE_DIR = REPO_ROOT / "catalogue"

ODYSSEUS_FILE = "odysseus.yaml"

#: The safety-critical pin: registry-verified OCI index digest of
#: ghcr.io/odysseus-dev/odysseus tag 1.0.3, which is byte-identical to
#: ``:latest`` at authoring time (curated ``main`` build; upstream docs:
#: ``:latest`` and bare ``:X.Y.Z`` tags move on every push to ``main`` —
#: hence the digest pin, not the tag). Verified 2026-10-18 against the raw
#: registry: linux amd64 + arm64 (both declared below). Any drift of this
#: pin fails here, loudly.
PINNED_IMAGE = (
    "ghcr.io/odysseus-dev/odysseus"
    "@sha256:4aa6e607d2108bd7b5f37ee1a0841dc56e6bbac36e9ccd5e1705aff7ea36fb04"
)

#: Independently documented upstream facts the curation rests on (acceptance
#: 1: license/source pin and maintenance known). Pinned against the curated
#: file's header below so the recipe stays self-describing.
UPSTREAM_SOURCE = "https://github.com/odysseus-dev/odysseus"
UPSTREAM_LICENSE = "AGPL-3.0-or-later"
UPSTREAM_MAINTENANCE_DOCS = "website/setup.md"

#: Declared resource budget (acceptance 2: exact resource limits) as test
#: constants. Measured on the pinned digest, 2026-10-18 (see evidence):
#: unpacked image 2.62 GiB, idle RSS 508.6 MiB. The disk token covers the
#: unpacked image plus a user-data headroom (sqlite DB, uploads, caches);
#: Cookbook model downloads are explicit in-app admin acquisitions with
#: their own budgets, outside this deployment budget (same rule as the
#: sibling local-inference recipe).
IMAGE_UNPACKED_GIB = 3
DATA_HEADROOM_GIB = 5
DISK_BUDGET_TOKEN = "hw.disk.10gb"
RAM_BUDGET_TOKEN = "hw.ram.2gb"
IDLE_RSS_MIB = 509
RAM_HEADROOM_FACTOR = 4

#: The declared prerequisite vocabulary for this recipe: docker compose plus
#: the two budget floors. NO CPU-feature/GPU token: the app is pure Python
#: (upstream image is a multi-arch python:3.14-slim derivative). Order is
#: irrelevant; the set is exact.
PREREQUISITE_VOCABULARY = frozenset(
    {
        "docker",
        RAM_BUDGET_TOKEN,
        DISK_BUDGET_TOKEN,
    }
)

HOST_PORT = 18111
CONTAINER_PORT = 7000
HEALTH_PATH = "/api/health"

#: The five upstream-persisted container paths (upstream compose mounts),
#: name -> container path. Host paths are planner-allocated.
UPSTREAM_PERSISTED_PATHS = {
    "data": "/app/data",
    "logs": "/app/logs",
    "ssh": "/app/.ssh",
    "huggingface": "/app/.cache/huggingface",
    "local": "/app/.local",
}

TARGET_X86_64 = TargetEnvironment(os="linux", arch="x86_64")
TARGET_AARCH64 = TargetEnvironment(os="linux", arch="aarch64")

SERVER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
APP_ID = uuid.UUID("0bbbbbbb-0bbb-4bbb-8bbb-bbbbbbbbbbb4")


def _stream_text() -> str:
    return (CATALOGUE_DIR / ODYSSEUS_FILE).read_text(encoding="utf-8")


def _document_texts() -> list[str]:
    """Split the catalogue file on YAML document boundaries.

    Consumer contract shared with the 08-fixture-recipes test module: a
    catalogue file is a revision stream parsed document-by-document with
    ``parse_recipe_text`` (the whole stream text is rejected by that parser
    by design).
    """
    return [yaml.serialize(node) for node in yaml.compose_all(_stream_text())]


def _recipe() -> ApplicationRecipe:
    documents = _document_texts()
    assert len(documents) == 1, "odysseus.yaml holds exactly one revision"
    return parse_recipe_text(documents[0])


def _mutant(mutate) -> str:
    """One parseable document with one curated field mutated."""
    document = yaml.safe_load(_document_texts()[0])
    mutate(document)
    return yaml.safe_dump(document)


def _plan(target: TargetEnvironment = TARGET_X86_64):
    return build_deployment_plan(_recipe(), SERVER_ID, APP_ID, target)


# ---------------------------------------------------------------------------
# AC1: the independently authored recipe validates through the schema and
# documents its upstream source, license and maintenance interface
# ---------------------------------------------------------------------------


def test_catalogue_file_is_a_single_revision_within_document_bounds() -> None:
    text = _stream_text()
    assert len(_document_texts()) == 1
    assert len(text.encode()) <= MAX_DOCUMENT_BYTES


def test_recipe_pins_the_approved_image_and_target() -> None:
    recipe = _recipe()
    assert recipe.image == PINNED_IMAGE
    assert recipe.name == "odysseus"
    assert recipe.supported_environment.os == ("linux",)
    # Both upstream-published architectures are declared (registry-verified
    # multi-arch index), so planning accepts both targets (asserted below).
    assert recipe.supported_environment.arch == ("aarch64", "x86_64")


def test_recipe_round_trips_through_canonical_rendering() -> None:
    recipe = _recipe()
    rendered = render_recipe(recipe)
    reparsed = parse_recipe_text(rendered)
    assert reparsed == recipe
    assert render_recipe(reparsed) == rendered
    assert recipe_sha256(recipe) == recipe_sha256(reparsed)


def test_catalogue_documents_source_license_and_maintenance() -> None:
    """Acceptance 1: license/source pin and maintenance docs are KNOWN —
    the curated file's own header carries them (self-describing recipe)."""
    text = _stream_text()
    assert UPSTREAM_SOURCE in text
    assert UPSTREAM_LICENSE in text
    assert UPSTREAM_MAINTENANCE_DOCS in text
    # The documented install interface the recipe was authored FROM.
    assert "docker compose logs odysseus" in text


def test_prerequisites_are_exactly_the_approved_budget_vocabulary() -> None:
    recipe = _recipe()
    assert frozenset(recipe.prerequisites) == PREREQUISITE_VOCABULARY
    assert recipe.required_grants == ("docker.compose",)
    for token in recipe.prerequisites:
        assert _HARDWARE_TOKEN_RE.fullmatch(token) or token == "docker"
    # No CPU-feature/GPU requirement: the app is pure Python.
    assert not any(
        token.startswith(("hw.vram.", "hw.cpu.", "hw.arch."))
        for token in recipe.prerequisites
    )


def test_resource_budget_constants_are_consistent_with_the_tokens() -> None:
    disk_budget = int(DISK_BUDGET_TOKEN.removeprefix("hw.disk.").removesuffix("gb"))
    ram_budget = int(RAM_BUDGET_TOKEN.removeprefix("hw.ram.").removesuffix("gb"))
    assert disk_budget == 10
    assert disk_budget >= IMAGE_UNPACKED_GIB + DATA_HEADROOM_GIB
    assert ram_budget == 2
    # 4x headroom over the measured idle RSS of the pinned digest.
    assert ram_budget * _GIB >= RAM_HEADROOM_FACTOR * IDLE_RSS_MIB * (1 << 20)


def test_approved_host_facts_satisfy_every_declared_check() -> None:
    recipe = _recipe()
    assert (
        evaluate_hardware_checks(
            frozenset(recipe.prerequisites), dict(APPROVED_TARGET_FACTS)
        )
        == frozenset()
    )


@pytest.mark.parametrize(
    ("facts", "extra_declared", "expected_unsatisfied"),
    [
        # A host with less than the declared RAM floor fails the RAM check.
        (
            {**APPROVED_TARGET_FACTS, "ram_bytes": int(1.5 * _GIB)},
            frozenset(),
            frozenset({RAM_BUDGET_TOKEN}),
        ),
        # Provisioned AT the C10 disposable-envelope cap (2 GiB) the recipe
        # is verifiable in a fixture: the floor is satisfied exactly there.
        (
            {**APPROVED_TARGET_FACTS, "ram_bytes": 2 * _GIB},
            frozenset(),
            frozenset(),
        ),
        # A small disk fails the explicit budget check.
        (
            {**APPROVED_TARGET_FACTS, "disk_free_bytes": 8 * _GIB},
            frozenset(),
            frozenset({DISK_BUDGET_TOKEN}),
        ),
        # Missing fact dimensions fail closed.
        (
            {k: v for k, v in APPROVED_TARGET_FACTS.items() if k != "disk_free_bytes"},
            frozenset(),
            frozenset({DISK_BUDGET_TOKEN}),
        ),
        # Docker unavailable fails the compose prerequisite.
        (
            {**APPROVED_TARGET_FACTS, "docker_compose_ready": False},
            frozenset(),
            frozenset({"docker"}),
        ),
        # Fail-closed: a token outside the frozen families is never silently
        # satisfied (imported producer semantics, one evaluator only).
        (
            {**APPROVED_TARGET_FACTS},
            frozenset({"hw.quantum.qubits64"}),
            frozenset({"hw.quantum.qubits64"}),
        ),
    ],
)
def test_unsupported_hardware_fails_the_matching_checks(
    facts: dict[str, object],
    extra_declared: frozenset[str],
    expected_unsatisfied: frozenset[str],
) -> None:
    recipe = _recipe()
    declared = frozenset(recipe.prerequisites) | extra_declared
    unsatisfied = evaluate_hardware_checks(declared, facts)
    assert unsatisfied == expected_unsatisfied


# ---------------------------------------------------------------------------
# Plan binding: policy-bound, bounded, health-factual (C09 rule 3)
# ---------------------------------------------------------------------------


def test_plan_is_fully_bound_and_deterministic() -> None:
    plan = _plan()
    assert plan.image == PINNED_IMAGE
    assert plan.recipe_sha256 == recipe_sha256(_recipe())
    assert plan.target_os == "linux" and plan.target_arch == "x86_64"
    assert plan.project_name == f"mai-app-{APP_ID.hex}"
    assert plan.compose_path == f"/var/lib/mai/apps/{APP_ID}/compose.yaml"
    # No credential references: the first admin credential is created by the
    # app itself and printed to its logs (documented upstream flow).
    assert plan.credentials_path is None and plan.credentials == ()
    assert plan.prerequisites == tuple(sorted(PREREQUISITE_VOCABULARY))
    assert plan.ports == (
        PortBinding(host=HOST_PORT, container=CONTAINER_PORT, protocol="tcp"),
    )
    assert plan.volumes == tuple(
        sorted(
            (
                PlannedVolume(
                    name=name,
                    container_path=container_path,
                    host_path=f"/var/lib/mai/apps/{APP_ID}/volumes/{name}",
                )
                for name, container_path in UPSTREAM_PERSISTED_PATHS.items()
            ),
            key=lambda volume: volume.name,
        )
    )
    rebuilt = _plan()
    assert rebuilt == plan
    assert rebuilt.plan_sha256 == plan.plan_sha256
    assert (
        plan.plan_sha256
        == hashlib.sha256(canonical_plan_json(plan).encode("utf-8")).hexdigest()
    )


def test_plan_verifies_prerequisites_and_requires_backup_before_effects() -> None:
    plan = _plan()
    ops = [operation.op for operation in plan.operations]
    # Frozen op order: verify_prerequisites FIRST (unsupported hardware
    # fails before any download or start), then the backup requirement for
    # the stateful data volume, then the bounded install vocabulary.
    assert ops == [
        OP_VERIFY_PREREQUISITES,
        "require_backup",
        "create_directories",
        "write_compose",
        OP_PULL_IMAGE,
        OP_START,
        "wait_healthy",
    ]
    assert plan.operations[0].subject == ",".join(sorted(PREREQUISITE_VOCABULARY))
    assert plan.operations[1].subject == ",".join(
        sorted(
            f"/var/lib/mai/apps/{APP_ID}/volumes/{name}"
            for name in UPSTREAM_PERSISTED_PATHS
        )
    )


def test_plan_accepts_both_declared_architectures() -> None:
    x86_plan = _plan(TARGET_X86_64)
    arm_plan = _plan(TARGET_AARCH64)
    assert arm_plan.target_arch == "aarch64"
    # Same deployed resources; the target record is part of the plan hash.
    assert arm_plan.image == x86_plan.image
    assert arm_plan.plan_sha256 != x86_plan.plan_sha256


def test_plan_refuses_unsupported_targets_before_any_effect() -> None:
    with pytest.raises(RecipeError) as os_exc:
        build_deployment_plan(
            _recipe(),
            SERVER_ID,
            APP_ID,
            TargetEnvironment(os="windows", arch="x86_64"),
        )
    assert os_exc.value.code is RecipeErrorCode.UNSUPPORTED_TARGET_OS


def test_plan_binds_the_documented_liveness_probe() -> None:
    plan = _plan()
    probe = plan.health_probe
    assert probe is not None
    assert (probe.kind, probe.port, probe.path, probe.expected_status) == (
        "http",
        CONTAINER_PORT,
        HEALTH_PATH,
        200,
    )
    assert plan.operations[-1].subject == (
        f"http://127.0.0.1:{CONTAINER_PORT}{HEALTH_PATH}"
    )
    limitations = plan.limitations
    # Health IS declared: the honest-absence limitation must be gone, while
    # the declared data caveats stay visible.
    assert "no declared health verification" not in "".join(limitations)
    assert any(
        "may be irreversible" in limit and "rollback is not possible" in limit
        for limit in limitations
    )
    assert any("previous pinned image digest" in limit for limit in limitations)
    assert any("keeps all persistent data" in limit for limit in limitations)
    assert any(
        "backup of persistent data is required" in limit for limit in limitations
    )


def test_compose_is_digest_pinned_shell_free_and_volume_bound() -> None:
    plan = _plan()
    text = plan.compose_text
    document = yaml.safe_load(text)
    service = document["services"]["odysseus"]
    assert service["image"] == PINNED_IMAGE
    assert service["ports"] == [f"{HOST_PORT}:{CONTAINER_PORT}/tcp"]
    assert service["volumes"] == [
        f"/var/lib/mai/apps/{APP_ID}/volumes/{name}:{container_path}"
        for name, container_path in sorted(UPSTREAM_PERSISTED_PATHS.items())
    ]
    assert service["restart"] == "unless-stopped"
    # No credentials -> no env_file pointer at all.
    assert "env_file" not in text
    healthcheck = service["healthcheck"]
    assert healthcheck["test"] == [
        "CMD",
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--output",
        "/dev/null",
        f"http://127.0.0.1:{CONTAINER_PORT}{HEALTH_PATH}",
    ]
    assert healthcheck["interval"] == "10s"
    assert healthcheck["timeout"] == "5s"
    assert healthcheck["retries"] == 6
    assert healthcheck["start_period"] == "30s"
    for forbidden in ("$", "CMD-SHELL", "/bin/sh", "sh -c"):
        assert forbidden not in text
    assert document["name"] == f"mai-app-{APP_ID.hex}"


def test_removal_keeps_user_data_and_updates_declare_irreversible() -> None:
    recipe = _recipe()
    assert recipe.removal.data_policy is RemovalDataPolicy.KEEP
    assert recipe.backup.required is True
    assert recipe.update.migrations.value == "irreversible"
    assert any(operation.op == "require_backup" for operation in _plan().operations)


def test_plan_mutation_is_bound_to_the_recipe_revision() -> None:
    plan = _plan()
    other_digest = PINNED_IMAGE.replace("4aa6e607", "0aa6e607")
    mutated = dataclasses.replace(_recipe(), image=other_digest)
    other_plan = build_deployment_plan(mutated, SERVER_ID, APP_ID, TARGET_X86_64)
    assert recipe_sha256(mutated) != recipe_sha256(_recipe())
    assert other_plan.plan_sha256 != plan.plan_sha256


# ---------------------------------------------------------------------------
# Negative controls: the curation is load-bearing
# ---------------------------------------------------------------------------


def test_tag_mutant_image_is_rejected_unpinned() -> None:
    def tag_image(document: dict[str, object]) -> None:
        document["deploy"]["image"] = "ghcr.io/odysseus-dev/odysseus:1.0.3"

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(tag_image))
    assert excinfo.value.code is RecipeErrorCode.UNPINNED_IMAGE


def test_corrupted_digest_mutant_image_is_rejected() -> None:
    def corrupt_digest(document: dict[str, object]) -> None:
        document["deploy"]["image"] = PINNED_IMAGE[:-1]  # 63 hex digits

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(corrupt_digest))
    assert excinfo.value.code is RecipeErrorCode.INVALID_IMAGE


@pytest.mark.parametrize(
    "field",
    ["services", "include", "env", "entrypoint", "command"],
)
def test_bundle_stack_smuggling_fields_are_unknown(field: str) -> None:
    """The v1 surface cannot re-deploy the upstream bundled chromadb/
    searxng/ntfy stack or configure the container environment: every such
    field is outside the frozen schema and must be rejected."""

    def inject(document: dict[str, object]) -> None:
        if field == "services":
            document[field] = {"searxng": {"image": "docker.io/searxng/searxng:latest"}}
        else:
            document[field] = ["SEARXNG_INSTANCE=http://searxng:8080"]

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(inject))
    assert excinfo.value.code is RecipeErrorCode.UNKNOWN_FIELD


def test_deploy_smuggling_field_is_unknown() -> None:
    def inject(document: dict[str, object]) -> None:
        document["deploy"]["extra_hosts"] = ["host.docker.internal:host-gateway"]

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(inject))
    assert excinfo.value.code is RecipeErrorCode.UNKNOWN_FIELD


def test_auth_gated_readiness_mutant_would_render_a_dead_probe() -> None:
    """GET /api/ready answers 401 unauthenticated (verified curation fact),
    so an argv curl --fail probe of it can never pass. The curated recipe
    keeps the documented liveness route /api/health; the mutant documents
    what the wrong path would silently break."""

    def readiness_path(document: dict[str, object]) -> None:
        document["health"]["path"] = "/api/ready"

    recipe = parse_recipe_text(_mutant(readiness_path))
    compose_text = render_compose(recipe, APP_ID)
    healthcheck = yaml.safe_load(compose_text)["services"]["odysseus"]["healthcheck"]
    assert healthcheck["test"][-1] == f"http://127.0.0.1:{CONTAINER_PORT}/api/ready"
    # The curated file declares the passing probe only.
    curated = yaml.safe_load(render_compose(_recipe(), APP_ID))
    assert curated["services"]["odysseus"]["healthcheck"]["test"][-1] == (
        f"http://127.0.0.1:{CONTAINER_PORT}{HEALTH_PATH}"
    )


def test_delete_removal_policy_is_rejected() -> None:
    def force_delete(document: dict[str, object]) -> None:
        document["removal"]["data_policy"] = "delete"

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(force_delete))
    assert excinfo.value.code is RecipeErrorCode.DATA_DELETE_FORBIDDEN


def test_privileged_host_port_mutant_is_rejected() -> None:
    def privileged_port(document: dict[str, object]) -> None:
        document["deploy"]["ports"][0]["host"] = 443

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(privileged_port))
    assert excinfo.value.code is RecipeErrorCode.INVALID_PORT


def test_unbound_health_port_mutant_is_rejected() -> None:
    def unbound_port(document: dict[str, object]) -> None:
        document["health"]["port"] = 8080

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(unbound_port))
    assert excinfo.value.code is RecipeErrorCode.HEALTH_PORT_UNBOUND


@pytest.mark.parametrize(
    "token", ["rm -rf", "hw.ram.2GB", "-hw.disk", "hw/ram/2gb", "HW.ram.2gb"]
)
def test_out_of_grammar_prerequisite_tokens_are_rejected(token: str) -> None:
    def add_token(document: dict[str, object]) -> None:
        document["prerequisites"].append(token)

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(add_token))
    assert excinfo.value.code is RecipeErrorCode.INVALID_PREREQUISITE


def test_out_of_vocabulary_prerequisite_is_caught_by_the_vocabulary_pin() -> None:
    """A well-formed but unapproved check token parses at schema level and
    MUST be caught by the curated-vocabulary pin, not by the schema."""

    def add_gpu_requirement(document: dict[str, object]) -> None:
        document["prerequisites"].append("hw.vram.8gb")

    recipe = parse_recipe_text(_mutant(add_gpu_requirement))
    assert frozenset(recipe.prerequisites) != PREREQUISITE_VOCABULARY


def test_credentials_cannot_carry_values() -> None:
    def value_map(document: dict[str, object]) -> None:
        document["deploy"]["credentials"] = {"ODYSSEUS_ADMIN_PASSWORD": "hunter2"}

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(value_map))
    assert excinfo.value.code is RecipeErrorCode.INVALID_CREDENTIAL_REFERENCE

    def lowercase_reference(document: dict[str, object]) -> None:
        document["deploy"]["credentials"] = ["admin_password"]

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(lowercase_reference))
    assert excinfo.value.code is RecipeErrorCode.INVALID_CREDENTIAL_REFERENCE
