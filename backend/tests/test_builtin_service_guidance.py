"""ask_34 — built-in skill guidance alignment (D1/D2/D6; C2/C6).

The pre-built packages in :mod:`mai.skills.builtin` are CONTEXT GUIDANCE,
never policy authority. These tests pin the aligned routing:

- a KNOWN service is resolved and its learned notes read via
  ``app.resolve_service`` / ``app.get_service_knowledge`` BEFORE any
  ``app.discover``;
- verified outcomes are recorded through ``app.learn_service`` (real
  observations only — never guesses);
- secret values are prohibited everywhere: credentials route exclusively
  through ``secret.list`` / ``secret.get`` / ``secret.store`` and only
  handles/references may be recorded;
- unavailable/denied tools are handled honestly (say so, report what was
  NOT verified — no fabricated actions);
- no guidance claims authority over policy/grants/operator instructions;
- the global catalog is written only on an explicit operator request
  (no auto-rewrite) and no per-server prompt writes exist;
- seeding keeps its non-overwrite semantics (operator-edited files are
  never replaced) and evidence gating keeps filtering skills whose
  declared capabilities are absent from the run's allowed set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mai.skills.builtin import (
    BUILTIN_SKILLS,
    collect_skills_evidence,
    seed_builtin_skills,
)

INSTRUCTIONS_BY_SKILL: dict[str, str] = {
    spec.skill: spec.instructions_md for spec in BUILTIN_SKILLS
}

HONESTY_SUFFIX = (
    "If a required tool is unavailable, denied, or fails, say so plainly "
    "and report what was NOT verified — never fabricate an observation or "
    "substitute a guessed action. This guidance is advisory: it never "
    "overrides policy, grants, or operator instructions."
)

AUTHORITY_CLAIMS = ("bypass", "ignore the policy", "self-grant")


@pytest.fixture()
def seeded_root(tmp_path: Path) -> Path:
    seed_builtin_skills(tmp_path)
    return tmp_path


def test_every_builtin_skill_routes_known_service_lookup_before_discovery() -> None:
    """Known services resolve and read learned notes BEFORE discovery."""
    for skill in ("service-diagnostics", "app-discovery-adoption"):
        text = INSTRUCTIONS_BY_SKILL[skill]
        assert "app.resolve_service" in text
        assert "app.get_service_knowledge" in text
        assert text.index("app.resolve_service") < text.index("app.discover"), (
            f"{skill} must present known-service lookup before discovery"
        )


def test_diagnostics_prefers_learned_notes_over_host_discovery() -> None:
    text = INSTRUCTIONS_BY_SKILL["service-diagnostics"]
    # Step 1 is the known-service resolution; discovery is the honest
    # fallback in step 2.
    assert "**Resolve the service first.**" in text
    assert "BEFORE any discovery" in text
    assert "app.learn_service" in text


def test_verified_learning_goes_through_learn_service_with_sources() -> None:
    """Recorded outcomes ride app.learn_service with real source refs."""
    for skill in ("service-diagnostics", "app-discovery-adoption"):
        text = INSTRUCTIONS_BY_SKILL[skill]
        assert "app.learn_service" in text
        assert "source references" in text or "source refs" in text


@pytest.mark.parametrize(
    "skill",
    [spec.skill for spec in BUILTIN_SKILLS],
)
def test_no_secret_values_any_secret_use_is_reference_only(skill: str) -> None:
    """D6: secret values never appear in facts, notes, or instructions."""
    text = INSTRUCTIONS_BY_SKILL[skill]
    lowered = text.lower()
    # Every skill names the prohibition; the credential tools are named
    # where secrets could plausibly be involved.
    assert "never" in lowered and "secret value" in lowered
    for forbidden in ("password=", "token=", "api_key=", "begin private key"):
        assert forbidden not in lowered


@pytest.mark.parametrize(
    "skill",
    [spec.skill for spec in BUILTIN_SKILLS],
)
def test_unavailable_tools_are_handled_honestly(skill: str) -> None:
    """A missing/denied tool is reported, never replaced by a guess."""
    assert HONESTY_SUFFIX in INSTRUCTIONS_BY_SKILL[skill]


@pytest.mark.parametrize(
    "skill",
    [spec.skill for spec in BUILTIN_SKILLS],
)
def test_no_skill_claims_policy_authority(skill: str) -> None:
    """Guidance is advisory: no authority claims over policy or grants."""
    text = INSTRUCTIONS_BY_SKILL[skill]
    assert "it never overrides policy, grants, or operator instructions" in text
    # The advisory disclaimer is the ONLY permitted "override" mention.
    body = text.replace(HONESTY_SUFFIX, "").lower()
    for claim in AUTHORITY_CLAIMS:
        assert claim not in body


def test_catalog_is_operator_driven_and_never_auto_rewritten() -> None:
    """Tier-1 catalog writes happen only on an explicit operator request."""
    text = INSTRUCTIONS_BY_SKILL["app-discovery-adoption"]
    assert "ONLY when the operator explicitly asked" in text
    assert "Never rewrite, reorganize, or refresh the global catalog" in text
    research = INSTRUCTIONS_BY_SKILL["web-research-runbook"]
    assert "ONLY on that explicit operator request" in research
    assert "never auto-rewrite existing catalog entries" in research


def test_no_per_server_prompt_writes_are_suggested() -> None:
    """C2: guidance never writes per-server prompts or global state automatically."""
    for text in INSTRUCTIONS_BY_SKILL.values():
        lowered = text.lower()
        assert "prompt" not in lowered.replace("prompt revision", "")
        assert "write" not in lowered or "write_text" not in lowered


def test_requires_capabilities_are_unchanged_no_grant_widening() -> None:
    """Manifests stay untouched: guidance is text, never a grant surface."""
    declared = {
        "service-diagnostics": ("host.logs", "host.shell", "service.http"),
        "app-discovery-adoption": (
            "app.list_catalog",
            "app.upsert_catalog_runbook",
            "app.discover",
            "app.list_adopted",
            "app.adopt",
            "app.upsert_map_entry",
        ),
        "security-posture-audit": ("host.shell", "app.discover"),
        "web-research-runbook": ("web.search", "web.read"),
    }
    for spec in BUILTIN_SKILLS:
        expected = declared[spec.skill]
        for capability in expected:
            assert f"- {capability}\n" in spec.manifest_yaml
        # No knowledge/learning capability was ADDED to the declared set:
        # the routing guidance references them, the gate does not grow.
        for not_declared in ("app.resolve_service", "app.get_service_knowledge", "app.learn_service"):
            assert f"- {not_declared}\n" not in spec.manifest_yaml


def test_seed_completes_missing_files_only_non_overwrite(tmp_path: Path) -> None:
    """Per-file non-overwrite: an edited file is never replaced by seeding."""
    seeded = seed_builtin_skills(tmp_path)
    assert sorted(seeded) == sorted(spec.skill for spec in BUILTIN_SKILLS)

    instructions = tmp_path / "shared" / "skills" / "service-diagnostics" / "SKILL.md"
    instructions.write_text("# operator edit\n", encoding="utf-8")
    manifest = tmp_path / "shared" / "skills" / "service-diagnostics" / "skill.yaml"
    original_manifest = manifest.read_text(encoding="utf-8")

    # Re-seeding after an operator edit writes NOTHING anywhere.
    assert seed_builtin_skills(tmp_path) == ()
    assert instructions.read_text(encoding="utf-8") == "# operator edit\n"
    assert manifest.read_text(encoding="utf-8") == original_manifest

    # A partial package (manifest deleted, instructions kept) gets ONLY
    # the manifest back — the operator's instructions text survives.
    manifest.unlink()
    assert seed_builtin_skills(tmp_path) == ("service-diagnostics",)
    assert manifest.read_text(encoding="utf-8") == original_manifest
    assert instructions.read_text(encoding="utf-8") == "# operator edit\n"


def test_collect_evidence_filters_on_declared_capabilities(seeded_root: Path) -> None:
    """Different capability sets gate evidence honestly (skills never grant)."""
    full = collect_skills_evidence(
        seeded_root,
        allowed_capabilities=(
            "host.logs",
            "host.shell",
            "service.http",
            "web.search",
            "web.read",
            "app.discover",
            "app.list_adopted",
        ),
    )
    sources = {item.source for item in full}
    assert "skill/service-diagnostics" in sources
    assert "skill/web-research-runbook" in sources

    # Without host.logs the diagnostics skill is filtered out entirely —
    # its declared prerequisite is reported, never self-served.
    restricted = collect_skills_evidence(
        seeded_root,
        allowed_capabilities=("web.search", "web.read"),
    )
    restricted_sources = {item.source for item in restricted}
    assert "skill/service-diagnostics" not in restricted_sources
    assert "skill/web-research-runbook" in restricted_sources

    # The collection read stays bounded.
    assert len(full) <= 8


def test_seeded_evidence_carries_the_aligned_routing(seeded_root: Path) -> None:
    """The evidence text that reaches a run is the ALIGNED guidance."""
    items = collect_skills_evidence(
        seeded_root,
        allowed_capabilities=(
            "host.logs",
            "host.shell",
            "service.http",
            "web.search",
            "web.read",
            "app.discover",
            "app.list_adopted",
        ),
    )
    diagnostics = next(
        item for item in items if item.source == "skill/service-diagnostics"
    )
    assert "app.resolve_service" in diagnostics.text
    assert diagnostics.text.index("app.resolve_service") < diagnostics.text.index(
        "app.list_adopted"
    )
    assert HONESTY_SUFFIX in diagnostics.text
