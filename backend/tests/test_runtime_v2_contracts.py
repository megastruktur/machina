"""Executable documentation-consistency tests for the Runtime v2 contract freeze.

Campaign ``runtime-v2``, task ``contracts`` (branch ``megastruktur/rv-contracts``).
The contract lives in ``docs/architecture/runtime-v2.md``; this suite executes
its machine-verified pins so a producer slice cannot drift a pinned name
without a failing test (contracts.md §Change control discipline, applied to the
rv freeze; runtime-v2.md §13/§15).

Verified here:

1. **Tree pins** — every path in the runtime-v2.md "Verified tree pins" table
   exists in this repository (no fabricated file references).
2. **Symbol pins** — every module/symbol pair in the "Symbol pins" table
   resolves against the real backend tree, and every pinned frozen value
   matches (no fabricated symbols, no silent value drift).
3. **Legacy handler identity** — the frozen ``agent.run`` /
   ``agent.delegate`` handler identities remain registered in the ONE engine
   handler map (runtime-v2.md §10: legacy replay/recovery stays executable
   through the whole drain window).
4. **Anti-fabrication sweep** — every backticked reference in runtime-v2.md
   that looks like a repo path or an importable ``mai.*`` module either exists
   or sits inside the explicit design-targets fence (§14).
5. **Supersession completeness** — the §1.1 supersession table carries all
   frozen rows S1–S5 with real resolutions.
6. **Scenario ownership** — every RV2 row in runtime-scenarios.md names a
   producer, gate, fixtures and expected evidence, all required acceptance
   kinds are covered, and every registered producer slice is one of the
   campaign's later slices.

Documentation-only: no implementation handler, no migration, no network.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
V2_DOC = REPO_ROOT / "docs" / "architecture" / "runtime-v2.md"
SCENARIOS_DOC = REPO_ROOT / "docs" / "architecture" / "runtime-scenarios.md"
CONTINUITY_DOC = REPO_ROOT / "docs" / "architecture" / "agent-task-continuity.md"
CONTRACTS_DOC = REPO_ROOT / "docs" / "architecture" / "contracts.md"

DESIGN_TARGETS_BEGIN = "<!-- design-targets:begin -->"
DESIGN_TARGETS_END = "<!-- design-targets:end -->"

#: Backticked tokens that LOOK like repository files or importable backend
#: modules. Anything matching outside the design-targets fence must be real.
_REPO_PATH_RE = re.compile(r"(?:backend|frontend)/src/[A-Za-z0-9_./-]+")
_MAI_MODULE_RE = re.compile(r"mai\.[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+")


def _mai_reference_resolves(reference: str) -> bool:
    """True if `reference` resolves as module path or module+symbol chain.

    ``mai.jobs.engine.register_job_handler`` resolves via the importable
    prefix ``mai.jobs.engine`` plus attribute access; a fabricated
    ``mai.agents.run_v2`` does not resolve at any split point.
    """
    parts = reference.split(".")
    for size in range(len(parts), 0, -1):
        try:
            target = importlib.import_module(".".join(parts[:size]))
        except ImportError:
            continue
        try:
            for attribute in parts[size:]:
                target = getattr(target, attribute)
        except AttributeError:
            return False
        return True
    return False

#: The campaign's later slices are the only legitimate RV2 producers/owners.
CAMPAIGN_SLICES = frozenset(
    {
        "contracts",
        "journal-store",
        "action-runtime",
        "policy-delegation",
        "conversation-api",
        "activity-ui",
        "acceptance",
        "deploy",
    }
)

#: Producers named in RV2 rows are module paths or slice names; every
#: `mai.*`-shaped producer reference must be a real module.
_RV2_REQUIRED_ROWS = (
    "RV2-JOURNEY",
    "RV2-DENY",
    "RV2-DELEGATE",
    "RV2-APPROVAL-RESTART",
    "RV2-REPLAY",
    "RV2-UNKNOWN",
    "RV2-UI-A11Y",
    "RV2-LEGACY",
    "RV2-DEPLOY-DRAIN",
)

#: Acceptance kinds the campaign plan requires as live-runtime scenarios,
#: mapped to the row that must cover them and a keyword its definition or
#: fixtures must mention.
_RV2_REQUIRED_KINDS = (
    ("real provider", "RV2-JOURNEY", "real provider"),
    ("disposable targets", "RV2-JOURNEY", "disposable"),
    ("disposable targets (denial)", "RV2-DENY", "disposable"),
    ("disposable targets (delegation)", "RV2-DELEGATE", "disposable"),
    ("browser/a11y", "RV2-UI-A11Y", "360×640"),
    ("restart", "RV2-APPROVAL-RESTART", "restart"),
    ("restart/replay", "RV2-REPLAY", "replay"),
    ("unknown effects", "RV2-UNKNOWN", "unknown"),
    ("deployment/drain", "RV2-DEPLOY-DRAIN", "drain"),
    ("legacy compatibility", "RV2-LEGACY", "legacy"),
)

_SUPERSESSION_IDS = ("S1", "S2", "S3", "S4", "S5")


def _doc_text() -> str:
    return V2_DOC.read_text(encoding="utf-8")


def _outside_design_targets(text: str) -> str:
    """Drop the §14 design-targets fence (declared future names live there)."""
    begin = text.find(DESIGN_TARGETS_BEGIN)
    end = text.find(DESIGN_TARGETS_END)
    assert begin != -1 and end != -1 and begin < end, (
        "runtime-v2.md must keep its design-targets fence markers intact"
    )
    return text[:begin] + text[end + len(DESIGN_TARGETS_END) :]


def _table_rows_after(text: str, heading: str) -> list[list[str]]:
    """Parse the markdown table that directly follows `heading` (post-header)."""
    index = text.find(heading)
    assert index != -1, f"missing heading in runtime-v2.md: {heading!r}"
    lines = text[index:].splitlines()[1:]
    rows: list[list[str]] = []
    seen_separator = False
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            seen_separator = True
            continue
        if not seen_separator:
            continue  # the header row
        rows.append(cells)
    assert rows, f"no table rows under {heading!r}"
    return rows


def _pin_modules_and_symbols() -> list[tuple[str, str, str]]:
    """(module, symbol, note) triples from the Symbol pins table."""
    rows = _table_rows_after(_doc_text(), "### Symbol pins (machine-verified)")
    pinned = []
    for cells in rows:
        assert len(cells) == 3, f"symbol pin row must have 3 cells: {cells}"
        module = cells[0].strip("`")
        symbol = cells[1].strip("`")
        assert module.startswith("mai."), f"pin module must be a backend module: {module}"
        assert symbol, f"empty symbol for module {module}"
        pinned.append((module, symbol, cells[2]))
    assert len(pinned) >= 30, "symbol pin table looks truncated"
    return pinned


def _resolve(module_name: str, symbol: str) -> object:
    module = importlib.import_module(module_name)
    target: object = module
    for attribute in symbol.split("."):
        target = getattr(target, attribute)
    return target


def _expected_literal(note: str) -> object | None:
    """A pinned frozen value if the note leads with a quoted/numeric literal."""
    match = re.search(r"`([^`]+)`", note)
    if match is None:
        return None
    token = match.group(1).strip()
    if not (token.startswith(('"', "'")) or token[:1].isdigit()):
        return None
    return ast.literal_eval(token)


# ---------------------------------------------------------------------------
# Contract document integrity
# ---------------------------------------------------------------------------


def test_contract_documents_exist() -> None:
    for path in (V2_DOC, SCENARIOS_DOC, CONTINUITY_DOC, CONTRACTS_DOC):
        assert path.is_file(), f"missing contract document: {path}"


def test_runtime_v2_doc_declares_frozen_status_and_provenance() -> None:
    text = _doc_text()
    assert text.startswith("# Runtime v2")
    assert "CONVERGED" in text
    assert "d7245204ed1b4b8c8123344493bf981fb2ac75e48886507a48939ee65d00cf59" in text
    assert "328f364538ca1d9825b825fb2be4d5504fde25e4582d9afe700b2eb1f9fdd22b" in text
    assert "agent.run_v2" in text
    assert "design-targets:begin" in text and "design-targets:end" in text


def test_supersession_table_is_complete() -> None:
    rows = _table_rows_after(_doc_text(), "### 1.1 Superseded rules (complete list)")
    by_id = {cells[0]: cells for cells in rows if cells and cells[0] in _SUPERSESSION_IDS}
    missing = [sid for sid in _SUPERSESSION_IDS if sid not in by_id]
    assert not missing, f"supersession rows missing: {missing}"
    for sid in _SUPERSESSION_IDS:
        cells = by_id[sid]
        assert len(cells) == 4, f"S-row must have 4 cells: {cells}"
        # Every supersession names what it replaces and the v2 replacement.
        assert len(cells[2]) > 20, f"{sid} 'where it lived' too thin: {cells[2]!r}"
        assert len(cells[3]) > 20, f"{sid} v2 rule too thin: {cells[3]!r}"


# ---------------------------------------------------------------------------
# Machine-verified pins (runtime-v2.md §13)
# ---------------------------------------------------------------------------


def test_tree_pins_exist() -> None:
    rows = _table_rows_after(_doc_text(), "## 13. Verified tree pins (machine-verified)")
    paths = []
    for cells in rows:
        assert len(cells) == 2, f"tree pin row must have 2 cells: {cells}"
        path = cells[0].strip("`")
        assert path.startswith(("backend/", "frontend/", "docs/")), (
            f"tree pin must be a repo path: {path}"
        )
        paths.append(path)
    assert len(paths) >= 20, "tree pin table looks truncated"
    missing = [p for p in paths if not (REPO_ROOT / p).exists()]
    assert not missing, f"fabricated tree pins (paths do not exist): {missing}"


def test_symbol_pins_resolve() -> None:
    mismatches: list[str] = []
    for module, symbol, note in _pin_modules_and_symbols():
        try:
            resolved = _resolve(module, symbol)
        except (ImportError, AttributeError) as exc:
            mismatches.append(f"{module}:{symbol} does not resolve ({exc!r})")
            continue
        expected = _expected_literal(note)
        if expected is not None and resolved != expected:
            mismatches.append(
                f"{module}:{symbol} == {resolved!r}, doc pins {expected!r}"
            )
    assert not mismatches, "symbol pins drifted from the frozen contract:\n" + "\n".join(
        mismatches
    )


def test_pagination_bounds_pin_the_agents_invariant() -> None:
    from mai.contracts.conversations import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT

    assert (DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT) == (50, 200), (
        "AGENTS.md pagination invariant bounds changed; reconcile runtime-v2.md §5.2"
    )


def test_stream_redaction_bounds_are_the_frozen_ones() -> None:
    from mai.agents import run as agent_run

    assert agent_run.STREAM_DELTA_MAX_CHARS == 4096
    assert agent_run.STREAM_TOOL_DETAIL_MAX_CHARS == 32768


def test_delegation_caps_and_payload_freeze() -> None:
    from mai.agents import delegate

    assert delegate.MAX_CHILDREN_PER_RUN == 8
    assert delegate.MAX_DELEGATION_DEPTH == 4
    # The single-server v1 payload stays frozen for legacy replay (S2).
    assert delegate.DELEGATION_PAYLOAD_VERSION == 1
    server_fields = {f for f in delegate.ChildSpec.__dataclass_fields__}
    assert "server_ids" not in server_fields, (
        "v1 ChildSpec gained a server field; that is the S2 supersession, "
        "which must ship as delegation payload version 2, not in place"
    )


# ---------------------------------------------------------------------------
# Legacy handler identity stays registered (runtime-v2.md §10)
# ---------------------------------------------------------------------------


def test_legacy_handler_identity_remains_registered() -> None:
    engine = importlib.import_module("mai.jobs.engine")
    agent_run = importlib.import_module("mai.agents.run")
    agent_delegate = importlib.import_module("mai.agents.delegate")

    assert agent_run.HANDLER_NAME == "agent.run"
    assert agent_delegate.HANDLER_NAME == "agent.delegate"
    assert agent_run.RUN_PAYLOAD_VERSION == 1

    registered = engine._RUN_HANDLERS
    assert isinstance(registered, dict), "engine handler registry shape changed"
    assert "agent.run" in registered, (
        "legacy agent.run handler lost its engine registration; legacy "
        "replay/drain (runtime-v2.md §10) requires it registered"
    )
    assert "agent.delegate" in registered, (
        "the ONE delegation handler lost its engine registration"
    )
    assert registered["agent.run"] is agent_run.run_agent_conversation
    assert registered["agent.delegate"] is agent_delegate.run_delegating_parent


def test_v2_handler_name_fits_the_frozen_grammar() -> None:
    engine = importlib.import_module("mai.jobs.engine")
    assert engine._HANDLER_NAME_RE.fullmatch("agent.run_v2"), (
        "frozen v2 handler name violates the engine dotted-slug grammar"
    )
    assert engine.RUN_WORKFLOW_NAME == "run_workflow"
    assert engine.APPLICATION_VERSION == "mai-1"
    # The action-runtime slice: importing the v2 runtime module registers
    # the handler through the ONE registration seam, while the legacy
    # handlers stay registered beside it (drain window, §10).
    run_v2 = importlib.import_module("mai.agents.run_v2")
    assert run_v2.HANDLER_NAME == "agent.run_v2"
    assert run_v2.RUN_PAYLOAD_VERSION_V2 == 2
    registered = engine._RUN_HANDLERS
    assert isinstance(registered, dict), "engine handler registry shape changed"
    assert "agent.run_v2" in registered, (
        "importing mai.agents.run_v2 must register agent.run_v2 via "
        "mai.jobs.engine.register_job_handler (runtime-v2.md §2)"
    )
    assert registered["agent.run_v2"] is run_v2.run_agent_conversation_v2
    # Legacy handlers remain registered for frozen replay/drain (§10).
    assert "agent.run" in registered and "agent.delegate" in registered


# ---------------------------------------------------------------------------
# Anti-fabrication sweep over the contract prose
# ---------------------------------------------------------------------------


def test_referenced_paths_and_modules_are_not_fabricated() -> None:
    body = _outside_design_targets(_doc_text())
    backticked = set(re.findall(r"`([^`]+)`", body))

    path_failures: list[str] = []
    module_failures: list[str] = []
    for token in backticked:
        for path_match in _REPO_PATH_RE.finditer(token):
            candidate = path_match.group(0).rstrip(".,;:")
            if not (REPO_ROOT / candidate).exists():
                path_failures.append(candidate)
        for module_match in _MAI_MODULE_RE.finditer(token):
            candidate = module_match.group(0).rstrip(".,;:")
            if not _mai_reference_resolves(candidate):
                module_failures.append(candidate)
    assert not path_failures, f"fabricated repo paths in runtime-v2.md: {path_failures}"
    assert not module_failures, (
        f"non-importable mai.* references in runtime-v2.md (move design "
        f"targets into §14): {module_failures}"
    )


def test_design_targets_stay_declared_future_names() -> None:
    text = _doc_text()
    begin = text.find(DESIGN_TARGETS_BEGIN)
    end = text.find(DESIGN_TARGETS_END)
    fence = text[begin:end]
    for declared in ("agent.run_v2", "ChildSpecV2", "payload_version", "objective."):
        assert declared in fence, f"declared design target missing from §14: {declared}"


# ---------------------------------------------------------------------------
# Scenario ownership (runtime-scenarios.md RV2 section)
# ---------------------------------------------------------------------------


def _rv2_rows() -> dict[str, list[str]]:
    text = SCENARIOS_DOC.read_text(encoding="utf-8")
    heading = "## RV2 — runtime v2 live scenarios"
    index = text.find(heading)
    assert index != -1, "runtime-scenarios.md lost the RV2 section"
    section = text[index:]
    end = section.find("\n### ")
    if end != -1:
        section = section[:end]
    rows: dict[str, list[str]] = {}
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0].startswith("RV2-"):
            rows[cells[0]] = cells
    return rows


def test_all_rv2_scenarios_have_producers_gates_and_fixtures() -> None:
    rows = _rv2_rows()
    missing = [rid for rid in _RV2_REQUIRED_ROWS if rid not in rows]
    assert not missing, f"RV2 rows missing from the registry: {missing}"

    failures: list[str] = []
    for rid in _RV2_REQUIRED_ROWS:
        cells = rows[rid]
        # ID | scenario | producer | gate | fixtures | evidence | status
        if len(cells) != 7:
            failures.append(f"{rid}: expected 7 columns, got {len(cells)}")
            continue
        scenario, producer, gate, fixtures, evidence, status = cells[1:]
        if not producer:
            failures.append(f"{rid}: no producer")
        if not gate:
            failures.append(f"{rid}: no verification gate")
        if len(fixtures) < 20:
            failures.append(f"{rid}: fixtures too thin: {fixtures!r}")
        if len(evidence) < 10:
            failures.append(f"{rid}: expected evidence too thin: {evidence!r}")
        if status not in {"design-frozen", "measured"}:
            failures.append(
                f"{rid}: status must be design-frozen or measured, got {status!r}"
            )
        if not scenario:
            failures.append(f"{rid}: empty definition")
    assert not failures, "RV2 registry ownership gaps:\n" + "\n".join(failures)


def test_rv2_producers_are_campaign_slices_or_real_modules() -> None:
    rows = _rv2_rows()
    failures: list[str] = []
    for rid, cells in rows.items():
        for token in re.findall(r"`([^`]+)`", cells[2]):
            for module_match in _MAI_MODULE_RE.finditer(token):
                candidate = module_match.group(0)
                if not _mai_reference_resolves(candidate):
                    failures.append(f"{rid}: fabricated producer module {candidate}")
    assert not failures, "RV2 producer pins:\n" + "\n".join(failures)


def test_rv2_required_acceptance_kinds_are_covered() -> None:
    rows = _rv2_rows()
    failures = []
    for kind, rid, keyword in _RV2_REQUIRED_KINDS:
        cells = rows.get(rid)
        if cells is None:
            failures.append(f"{kind}: row {rid} missing")
            continue
        searchable = " ".join(cells[1:6])
        if keyword not in searchable:
            failures.append(f"{kind}: {rid} does not mention {keyword!r}")
    assert not failures, "required live-runtime kinds uncovered:\n" + "\n".join(failures)


def test_rv2_coverage_statement_names_all_rows() -> None:
    text = SCENARIOS_DOC.read_text(encoding="utf-8")
    index = text.find("### RV2 coverage statement")
    assert index != -1, "RV2 coverage statement missing"
    section = text[index:]
    for rid in _RV2_REQUIRED_ROWS:
        assert rid in section, f"coverage statement does not map {rid}"


def test_campaign_slices_are_the_declared_owners() -> None:
    text = _doc_text()
    index = text.find("## 11. API route ownership for later slices")
    assert index != -1
    section = text[index : text.find("## 12.")]
    for slice_name in sorted(CAMPAIGN_SLICES - {"contracts"}):
        assert slice_name in section, (
            f"route-ownership table lost the {slice_name} slice"
        )


def test_downstream_contract_docs_point_at_the_v2_contract() -> None:
    contracts = CONTRACTS_DOC.read_text(encoding="utf-8")
    continuity = CONTINUITY_DOC.read_text(encoding="utf-8")
    assert "runtime-v2.md" in contracts, "contracts.md addendum must cite runtime-v2.md"
    assert "runtime-v2.md" in continuity, "continuity doc must cite runtime-v2.md"
    assert "legacy-only" in continuity or "legacy handler" in continuity
    scenarios = SCENARIOS_DOC.read_text(encoding="utf-8")
    assert "runtime-v2.md" in scenarios, "RV2 section must cite its contract source"
