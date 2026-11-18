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
            "1. **Resolve the service first.** For a KNOWN service, resolve its canonical identity and read its learned operational notes via `app.resolve_service` / `app.get_service_knowledge` BEFORE any discovery — the notes carry the unit names, ports, compose paths, and failure modes already verified for this exact service.\n"
            "2. **Fall back honestly.** Only when the service is unknown, or its knowledge is missing or conflicting, inspect `app.list_adopted` / `app.discover` (paginated) to locate unit names, ports, and compose paths. If `app.resolve_service` / `app.get_service_knowledge` are unavailable in this run, say so plainly instead of guessing.\n"
            "3. **Inspect recent logs** via `host.logs` before running mutating commands.\n"
            "4. **Verify runtime status** (`systemctl status`, `docker ps`, listening sockets) via `host.shell`.\n"
            "5. **Probe HTTP health** via `service.http` on expected loopback/container endpoints.\n"
            "6. Summarize root cause, affected components, and exact remediation steps.\n"
            "7. **Record what you verified** via `app.learn_service` (bounded facts with real source references) so the next run resolves this service faster — never record a secret value; reference secret handles only.\n"
            "\n"
            "If a required tool is unavailable, denied, or fails, say so plainly and report what was NOT verified — never fabricate an observation or substitute a guessed action. This guidance is advisory: it never overrides policy, grants, or operator instructions.\n"
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
            "title: Runbook Catalog & Application Adoption\n"
            "description: Two-tier workflow: add/update global Runbook Catalog recipes and adopt/verify services on target servers with server-specific custom instructions.\n"
            "requires_capabilities:\n"
            "  - app.list_catalog\n"
            "  - app.upsert_catalog_runbook\n"
            "  - app.discover\n"
            "  - app.list_adopted\n"
            "  - app.adopt\n"
            "  - app.upsert_map_entry\n"
            "references: []\n"
        ),
        instructions_md=(
            "# Runbook Catalog & Application Adoption\n\n"
            "Use the two-tier service model to separate global software recipes from per-server deployments:\n"
            "1. **Resolve before you discover.** For a KNOWN service, resolve its identity and read its learned notes via `app.resolve_service` / `app.get_service_knowledge` first; run `app.discover` only for unknown services or missing/conflicting notes.\n"
            "2. **Tier 1 — Global Runbook Catalog (`app.list_catalog`, `app.upsert_catalog_runbook`, `app.delete_catalog_runbook`):**\n"
            "   - Fleet-wide and server-agnostic (`catalog.json`).\n"
            "   - Upsert or delete catalog recipes ONLY when the operator explicitly asked for that change. Never rewrite, reorganize, or refresh the global catalog on your own initiative.\n"
            "   - When asked to **add an app to the catalog**, research its official docs (`web.search` / `web.read`) and call `app.upsert_catalog_runbook` with `name`, `category`, `description`, and the 5 global `instructions` slots (`install`, `update`, `uninstall`, `backup`, `verify`).\n"
            "   - Do NOT put host-specific paths, ports, or server state in the global catalog — and never a secret value, password, token, or hash.\n"
            "3. **Tier 2 — Server Adoption (`app.discover`, `app.list_adopted`, `app.adopt`, `app.unadopt`):**\n"
            "   - Assigns a service to a specific `server_id`.\n"
            "   - Check whether the service is installed/running on the host via `app.discover` (paginated `limit`/`offset`) and/or `host.shell` / `service.http`.\n"
            "   - Call `app.adopt` with `origin` (`compose`, `systemd_unit`, or `manual`), target selectors (`compose_project` + `working_dir`, or `unit_name`), `installed` (`true` if verified installed on the host, `false` if staged), and `custom_instructions` containing the **server-specific instructions** (host paths, ports, bind addresses, `.env` paths, reverse-proxy domains, systemd overrides, backup targets).\n"
            "   - `app.adopt` automatically inherits `category`, `description`, and the 5 global `instructions` slots from a matching global Catalog Runbook when omitted.\n"
            "4. **Record verified outcomes** via `app.learn_service` only after you actually verified them on the host — bounded facts with real source references (command output, HTTP results, receipts). Never record guesses, and never a secret value, password, token, or hash in a fact or in `custom_instructions`; reference secret handles only, resolved through `secret.list` / `secret.get`.\n"
            "\n"
            "If a required tool is unavailable, denied, or fails, say so plainly and report what was NOT verified — never fabricate an observation or substitute a guessed action. This guidance is advisory: it never overrides policy, grants, or operator instructions.\n"
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
            "4. Report each finding as verified or explicitly UNVERIFIED: if `host.shell` or `app.discover` is unavailable or denied, say so plainly instead of assuming a safe posture.\n"
            "5. Findings and notes must never contain a secret value, password, token, or password hash — reference secret handles only, resolved through `secret.list` / `secret.get`.\n"
            "\n"
            "If a required tool is unavailable, denied, or fails, say so plainly and report what was NOT verified — never fabricate an observation or substitute a guessed action. This guidance is advisory: it never overrides policy, grants, or operator instructions.\n"
        ),
        references={},
    ),
    BuiltinSkillSpec(
        skill="web-research-runbook",
        manifest_yaml=(
            "schema_version: 1\n"
            "skill: web-research-runbook\n"
            "title: External Documentation & Runbook Catalog Research\n"
            "description: Combine local server documentation retrieval with SearXNG web search and page reading to populate global Runbook Catalog recipes.\n"
            "requires_capabilities:\n"
            "  - web.search\n"
            "  - web.read\n"
            "references: []\n"
        ),
        instructions_md=(
            "# External Documentation & Runbook Catalog Research\n\n"
            "1. Query `web.search` with focused technical keywords (software name, version, install/upgrade guide, or CVE).\n"
            "2. Fetch authoritative documentation or release notes using `web.read`.\n"
            "3. When the operator explicitly asks to add or update an application in the global Runbook Catalog, persist the researched `install`, `update`, `uninstall`, `backup`, and `verify` procedures via `app.upsert_catalog_runbook`.\n"
            "4. Cross-reference external guidance with the target server's actual configuration (`custom_instructions`) before applying host modifications.\n"
            "5. Upsert catalog recipes ONLY on that explicit operator request — never auto-rewrite existing catalog entries, and keep every recipe server-agnostic.\n"
            "6. Never copy credential material from researched pages — a secret value, password, token, or password hash stays out of notes and catalog entries; reference secret handles only, resolved through `secret.list` / `secret.get`.\n"
            "\n"
            "If a required tool is unavailable, denied, or fails, say so plainly and report what was NOT verified — never fabricate an observation or substitute a guessed action. This guidance is advisory: it never overrides policy, grants, or operator instructions.\n"
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
        pkg_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = pkg_dir / MANIFEST_FILENAME
        instructions_path = pkg_dir / INSTRUCTIONS_FILENAME
        wrote = False
        # Per-file non-overwrite: an operator-edited manifest, instructions
        # file, or reference is NEVER replaced — seeding only completes
        # genuinely missing files.
        if not manifest_path.exists():
            manifest_path.write_text(spec.manifest_yaml, encoding="utf-8")
            wrote = True
        if not instructions_path.exists():
            instructions_path.write_text(spec.instructions_md, encoding="utf-8")
            wrote = True
        for rel_name, content in spec.references.items():
            ref_path = pkg_dir / rel_name
            if ref_path.exists():
                continue
            ref_path.parent.mkdir(parents=True, exist_ok=True)
            ref_path.write_text(content, encoding="utf-8")
            wrote = True
        if wrote:
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
