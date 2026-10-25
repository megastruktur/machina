#!/usr/bin/env python3
"""Disclosed scorecard for the 23-evals acceptance (act card 23-evals).

Reads the scenario evidence produced by
``tests/runtime/test_agent_campaign_acceptance.py`` (default: the campaign
evidence directory, overridable via ``--evidence-dir`` / ``MAI_ACT_EVIDENCE_DIR``)
and prints the scorecard with the THREE MANDATORY DISCLOSURE BUCKETS:

1. ``deterministic``  — outcome/constraint graders run through the real
   app/worker/store with the harness's OWN scripted-model fixtures
   (labelled scripted, never live);
2. ``provider-quality`` — legs actually measured against a real configured
   provider through the app path, each with its request count, wall clock
   and reserved spend;
3. ``not-measured``  — every registry row or leg that did not run, with a
   machine-readable reason. A baseline that was not actually measured is
   reported as ``not measured`` — never as a number (no fabricated
   baselines).

Budget instrumentation (act_DIRECTIVES.md ratified cap): 60 provider
requests / 30 provider wall-clock minutes / USD 5 conservatively reserved
token spend — first bound wins, and the harness stops opening new live legs
at the bound. Scripted model calls are fixture-local (they never reach a
provider) and are disclosed separately from provider requests.

Build identity comes from ``fixture-start.json`` (commit/tree/dirty).
Exit code 0 iff every deterministic grader passed AND every unmeasured
leg is disclosed with a reason.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = REPO_ROOT / "tests" / "runtime" / "agent_campaign_cases.json"
DEFAULT_EVIDENCE_DIR = Path(
    "/home/megastruktur/projects/dev/machina/plans/agent-continuity/"
    "act_evidence/23-evals"
)

#: Ratified live-provider budget (hard cap, first bound wins).
BUDGET = {
    "requests_cap": 60,
    "wall_clock_minutes_cap": 30,
    "reserved_spend_usd_cap": 5.0,
}


def _load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def _case_limitations(case: dict[str, Any]) -> list[str]:
    """Every disclosed limitation of one registry case, de-duplicated.

    Limitations may live at the CASE level (``case["disclosed_limitations"]``)
    or nested under the GRADER (``case["grader"]["disclosed_limitations"]``).
    The 23-evals registry carried them only under ``grader``, which this
    reader silently dropped (defect found on act 23d) — read BOTH places,
    de-duplicated per (case, text), preserving first-seen order.
    """
    seen: dict[str, None] = {}
    for source in (
        case.get("disclosed_limitations") or [],
        (case.get("grader") or {}).get("disclosed_limitations") or [],
    ):
        for text in source:
            if isinstance(text, str) and text.strip():
                seen.setdefault(text, None)
    return list(seen)


def build_scorecard(evidence_dir: Path) -> dict[str, Any]:
    cases_doc = _load(CASES_PATH) or {"cases": []}
    registry: dict[str, dict[str, Any]] = {
        case["id"]: case for case in cases_doc.get("cases", [])
    }
    budget_doc = cases_doc.get("budget") or BUDGET
    scenarios = _load(evidence_dir / "scenarios.json") or {}
    fixture_start = _load(evidence_dir / "fixture-start.json")
    cleanup = _load(evidence_dir / "cleanup.json")

    records: dict[str, dict[str, Any]] = scenarios.get("cases", {})
    deterministic: dict[str, Any] = {}
    provider_quality: list[dict[str, Any]] = []
    not_measured: list[dict[str, Any]] = []

    provider_requests = 0
    provider_wall_minutes = 0.0
    reserved_spend_usd = 0.0
    for leg in scenarios.get("provider_legs", []):
        provider_requests += int(leg.get("requests", 0))
        provider_wall_minutes += float(leg.get("wall_clock_minutes", 0.0))
        reserved_spend_usd += float(leg.get("reserved_spend_usd", 0.0))
        provider_quality.append(leg)

    for case_id, case in registry.items():
        record = records.get(case_id)
        if record is None:
            not_measured.append(
                {
                    "case": case_id,
                    "leg": "whole case",
                    "reason": "no runtime evidence produced by the "
                    "acceptance suite for this row",
                }
            )
            continue
        deterministic[case_id] = {
            "domain": case.get("domain"),
            "verdict": record.get("verdict", "unknown"),
            "checks": record.get("checks", []),
            "mode": "scripted",
            "scripted_model_calls": record.get("model_calls"),
        }
        for limitation in _case_limitations(case):
            not_measured.append(
                {
                    "case": case_id,
                    "leg": "disclosed limitation",
                    "reason": limitation,
                }
            )
        if not record.get("live_measured", False):
            not_measured.append(
                {
                    "case": case_id,
                    "leg": "live-provider trial",
                    "reason": record.get("live_skip_reason")
                    or "no authorized isolated provider endpoint is composed "
                    "in this evaluation environment; the scripted-model leg "
                    "proves plumbing and the deterministic grader proves "
                    "app behaviour, but no provider-quality claim is made",
                }
            )

    deterministic_failures = sorted(
        case_id
        for case_id, row in deterministic.items()
        if row.get("verdict") != "pass"
    )
    return {
        "schema": "act-evals-scorecard/1",
        "build_identity": (fixture_start or {}).get(
            "source", scenarios.get("source")
        ),
        "disclosure_buckets": {
            "deterministic": deterministic,
            "provider_quality": provider_quality,
            "not_measured": not_measured,
        },
        "budget": {
            **{key: budget_doc.get(key, BUDGET[key]) for key in BUDGET},
            "provider_requests_used": provider_requests,
            "provider_wall_clock_minutes_used": round(provider_wall_minutes, 2),
            "reserved_spend_usd_used": round(reserved_spend_usd, 2),
            "stop_policy": "first bound reached stops all live legs; "
            "scripted model calls never reach a provider and are not "
            "counted against the provider caps",
            "within_cap": (
                provider_requests <= BUDGET["requests_cap"]
                and provider_wall_minutes <= BUDGET["wall_clock_minutes_cap"]
                and reserved_spend_usd <= BUDGET["reserved_spend_usd_cap"]
            ),
        },
        "baselines": {
            "sanitized_baseline_cases": "carried as qualitative registry "
            "expectations in agent_campaign_cases.json",
            "baseline_metrics": "not measured",
            "note": "no baseline number is reported because no baseline "
            "was measured in this evaluation run; fabricating one is "
            "forbidden by the acceptance criteria",
        },
        "cleanup": cleanup,
        "deterministic_failures": deterministic_failures,
        "summary": {
            "registry_rows": len(registry),
            "deterministic_pass": len(deterministic) - len(deterministic_failures),
            "deterministic_fail": len(deterministic_failures),
            "provider_quality_legs": len(provider_quality),
            "not_measured_disclosures": len(not_measured),
        },
    }


def _render(scorecard: dict[str, Any]) -> str:
    lines: list[str] = []
    summary = scorecard["summary"]
    budget = scorecard["budget"]
    identity = scorecard.get("build_identity") or {}
    lines.append("ACT 23-evals scorecard")
    lines.append("======================")
    lines.append(
        "build: commit {commit} tree {tree} dirty={dirty} branch={branch}".format(
            commit=str(identity.get("commit", "unknown"))[:12],
            tree=str(identity.get("tree", "unknown"))[:12],
            dirty=identity.get("dirty", "unknown"),
            branch=identity.get("branch", "unknown"),
        )
    )
    lines.append("")

    lines.append(
        f"Bucket 1 — deterministic graders (scripted fixtures, real "
        f"app/worker/store): {summary['deterministic_pass']} pass / "
        f"{summary['deterministic_fail']} fail"
    )
    buckets = scorecard["disclosure_buckets"]
    for case_id, row in sorted(buckets["deterministic"].items()):
        marker = "PASS" if row["verdict"] == "pass" else "FAIL"
        lines.append(
            f"  [{marker}] {case_id} ({row['domain']}) "
            f"checks={len(row.get('checks', []))}"
        )
    lines.append("")

    lines.append(
        f"Bucket 2 — provider quality (real provider, budgeted): "
        f"{summary['provider_quality_legs']} legs"
    )
    if buckets["provider_quality"]:
        for leg in buckets["provider_quality"]:
            lines.append(
                f"  {leg.get('case', '?')}: requests={leg.get('requests')} "
                f"wall={leg.get('wall_clock_minutes')}min "
                f"reserved=${leg.get('reserved_spend_usd')}"
                + (
                    f" satisfied={leg.get('satisfied')}"
                    if isinstance(leg.get("satisfied"), bool)
                    else ""
                )
            )
            if leg.get("satisfied") is False and leg.get("detail"):
                lines.append(f"    PROVIDER-QUALITY FAILURE: {leg['detail']}")
    else:
        lines.append(
            "  (none measured — scripted legs are reported in bucket 1 and "
            "never claimed as provider evidence)"
        )
    lines.append("")

    lines.append(
        f"Bucket 3 — not measured (every skip is disclosed): "
        f"{summary['not_measured_disclosures']} disclosures"
    )
    for row in buckets["not_measured"]:
        lines.append(f"  - {row['case']} [{row['leg']}]: {row['reason']}")
    lines.append("")

    lines.append(
        "Budget (hard cap, first bound wins): "
        f"{budget['provider_requests_used']}/{budget['requests_cap']} requests, "
        f"{budget['provider_wall_clock_minutes_used']}/"
        f"{budget['wall_clock_minutes_cap']} provider minutes, "
        f"${budget['reserved_spend_usd_used']}/"
        f"${budget['reserved_spend_usd_cap']} reserved — "
        f"within_cap={budget['within_cap']}"
    )
    lines.append(
        "Baselines: " + scorecard["baselines"]["baseline_metrics"]
        + " (no fabricated baseline numbers)"
    )
    if scorecard["deterministic_failures"]:
        lines.append(
            "DETERMINISTIC FAILURES: "
            + ", ".join(scorecard["deterministic_failures"])
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--evidence-dir",
        default=os.environ.get("MAI_ACT_EVIDENCE_DIR", str(DEFAULT_EVIDENCE_DIR)),
        help="evidence directory produced by the acceptance suite",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="also write the full scorecard document to this path",
    )
    args = parser.parse_args(argv)
    evidence_dir = Path(args.evidence_dir)
    scorecard = build_scorecard(evidence_dir)
    print(_render(scorecard))
    target = evidence_dir / "scorecard.json"
    target.write_text(
        json.dumps(scorecard, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(scorecard, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    healthy = (
        not scorecard["deterministic_failures"]
        and scorecard["budget"]["within_cap"]
    )
    return 0 if healthy else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
