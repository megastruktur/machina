"""Pre-built starter skills shipped with Machina (M.A.I.) and runtime evidence loader.

Each pre-built skill conforms to the frozen ``09-skills`` portable package
format (`skill.yaml` + `SKILL.md` + optional reference files under
`<data_root>/shared/skills/<skill>/`).
"""

from __future__ import annotations

import os
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from mai.agents.context import ContextEvidence
from mai.skills.load import (
    INSTRUCTIONS_FILENAME,
    MANIFEST_FILENAME,
    SKILLS_SUBPATH,
    SkillLoadError,
    load_skill,
    missing_prerequisites,
    skill_evidence,
)

__all__ = [
    "BUILTIN_SKILLS",
    "BuiltinSkillSpec",
    "collect_skills_evidence",
    "seed_builtin_skills",
]


@dataclass(frozen=True, slots=True)
class BuiltinSkillSpec:
    """One pre-built skill package definition."""

    skill: str
    manifest_yaml: str
    instructions_md: str
    references: dict[str, str]


BUILTIN_SKILLS: tuple[BuiltinSkillSpec, ...] = (
    BuiltinSkillSpec(
        skill="service-diagnostics",
        manifest_yaml=(
            "schema_version: 1\n"
            "skill: service-diagnostics\n"
            "title: Service Diagnostics & Triage\n"
            "description: Structured workflow for inspecting systemd units, Docker containers, logs, and HTTP health.\n"
            "requires_capabilities:\n"
            "  - host.logs\n"
            "  - host.shell\n"
            "  - service.http\n"
            "references:\n"
            "  - checklist.md\n"
        ),
        instructions_md=(
            "# Service Diagnostics & Triage\n\n"
            "When diagnosing an unhealthy or degraded service on a managed server:\n"
            "1. **Check adopted inventory & Service Map** first (`app.list_adopted` / `app.discover`) to locate unit names, ports, and compose paths.\n"
            "2. **Inspect recent logs** via `host.logs` before running mutating commands.\n"
            "3. **Verify runtime status** (`systemctl status`, `docker ps`, listening sockets) via `host.shell`.\n"
            "4. **Probe HTTP health** via `service.http` on expected loopback/container endpoints.\n"
            "5. Summarize root cause, affected components, and exact remediation steps.\n"
        ),
        references={
            "checklist.md": (
                "# Triage Checklist\n\n"
                "- Confirm container/unit restart counts and exit codes (`OOMKilled`, `137`, `143`).\n"
                "- Check disk space (`df -h`) and memory pressure (`free -m`) before restarting services.\n"
                "- Verify upstream database/cache reachability and file permissions.\n"
            )
        },
    ),
    BuiltinSkillSpec(
        skill="app-discovery-adoption",
        manifest_yaml=(
            "schema_version: 1\n"
            "skill: app-discovery-adoption\n"
            "title: Application Discovery & Service Map Curation\n"
            "description: Discover running workloads, adopt services into M.A.I., and document their Service Map entries.\n"
            "requires_capabilities:\n"
            "  - app.discover\n"
            "  - app.list_adopted\n"
            "  - app.adopt\n"
            "  - app.upsert_map_entry\n"
            "references: []\n"
        ),
        instructions_md=(
            "# Application Discovery & Service Map Curation\n\n"
            "Use this workflow when onboarding a server or auditing its running workloads:\n"
            "1. Call `app.discover` with pagination (`limit`, `offset`) and optional `kind_filter` / `query`.\n"
            "2. Compare discovered candidates against `app.list_adopted`.\n"
            "3. Adopt confirmed production workloads with `app.adopt` and curate their category, description, and operational runbook via `app.upsert_map_entry`.\n"
        ),
        references={},
    ),
    BuiltinSkillSpec(
        skill="security-posture-audit",
        manifest_yaml=(
            "schema_version: 1\n"
            "skill: security-posture-audit\n"
            "title: Host & Container Security Posture Audit\n"
            "description: Audit exposed listening ports, firewall rules, SSH configuration, and container privileges.\n"
            "requires_capabilities:\n"
            "  - host.shell\n"
            "  - app.discover\n"
            "references: []\n"
        ),
        instructions_md=(
            "# Host & Container Security Posture Audit\n\n"
            "1. Enumerate listening TCP/UDP sockets (`ss -tulnp`) and compare against intended public/tailnet bindings.\n"
            "2. Verify firewall status (`ufw status verbose` or `nft list ruleset`) and ensure container bridge traffic is scoped.\n"
            "3. Check running containers for privileged mode, host network sharing, or sensitive volume mounts.\n"
        ),
        references={},
    ),
    BuiltinSkillSpec(
        skill="web-research-runbook",
        manifest_yaml=(
            "schema_version: 1\n"
            "skill: web-research-runbook\n"
            "title: External Documentation & Advisory Research\n"
            "description: Combine local server documentation retrieval with SearXNG web search and page reading.\n"
            "requires_capabilities:\n"
            "  - web.search\n"
            "  - web.read\n"
            "references: []\n"
        ),
        instructions_md=(
            "# External Documentation & Advisory Research\n\n"
            "1. Query `web.search` with focused technical keywords (software name, version, error code, or CVE).\n"
            "2. Fetch authoritative documentation or release notes using `web.read`.\n"
            "3. Cross-reference external guidance with the target server's actual configuration before recommending changes.\n"
        ),
        references={},
    ),
)


def seed_builtin_skills(data_root: str | os.PathLike[str]) -> tuple[str, ...]:
    """Ensure pre-built skills exist under `<data_root>/shared/skills/` without overwriting custom edits."""
    skills_dir = Path(data_root) / SKILLS_SUBPATH
    skills_dir.mkdir(parents=True, exist_ok=True)
    seeded: list[str] = []
    for spec in BUILTIN_SKILLS:
        pkg_dir = skills_dir / spec.skill
        manifest_path = pkg_dir / MANIFEST_FILENAME
        instructions_path = pkg_dir / INSTRUCTIONS_FILENAME
        if manifest_path.exists() and instructions_path.exists():
            continue
        pkg_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(spec.manifest_yaml, encoding="utf-8")
        instructions_path.write_text(spec.instructions_md, encoding="utf-8")
        for rel_name, content in spec.references.items():
            ref_path = pkg_dir / rel_name
            ref_path.parent.mkdir(parents=True, exist_ok=True)
            ref_path.write_text(content, encoding="utf-8")
        seeded.append(spec.skill)
    return tuple(seeded)


def collect_skills_evidence(
    data_root: object,
    allowed_capabilities: Collection[str] | None = None,
    *,
    max_items: int = 8,
) -> tuple[ContextEvidence, ...]:
    """Load valid skills from `<data_root>/shared/skills/` into bounded `ContextEvidence` items."""
    root = Path(os.fsdecode(data_root))  # type: ignore[arg-type]
    skills_dir = root / SKILLS_SUBPATH
    if not skills_dir.is_dir() or skills_dir.is_symlink():
        return ()
    try:
        entries = sorted(skills_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return ()
    evidence_items: list[ContextEvidence] = []
    for entry in entries:
        if len(evidence_items) >= max_items:
            break
        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
            continue
        try:
            package = load_skill(root, entry.name)
        except SkillLoadError:
            continue
        if allowed_capabilities is not None and package.requires_capabilities:
            missing = missing_prerequisites(package, allowed_capabilities)
            if missing:
                continue
        for item in skill_evidence(package):
            if len(evidence_items) >= max_items:
                break
            evidence_items.append(item)
    return tuple(evidence_items)
