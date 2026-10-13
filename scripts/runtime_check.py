#!/usr/bin/env python3
"""MAI runtime scenario runner and evidence envelope (card 01-harness, P01).

Contract anchors: ``docs/architecture/contracts.md`` C10 (evidence ties to the
exact commit/tree/build, is sanitized, uses the ``mai-fxt-<card>-<purpose>``
fixture namespace and records a cleanup proof) and
``docs/architecture/runtime-scenarios.md`` (status legend; a scenario that is
not implemented by its producer is never recorded as PASS).

Commands:
  list                     print the registered scenario catalog and exit 0.
  run SCENARIO             execute SCENARIO, write one JSON evidence envelope
                           into the evidence directory, exit 0 on PASS.

Exit codes: ``0`` scenario passed; ``1`` scenario ran and failed; ``2`` unknown
scenario or missing --evidence-dir; ``3`` known-but-unimplemented scenario.

The runner is independent of the workspace/desktop completion (test policy):
the only implemented scenario exercises the real ``mai.api.app`` ASGI callable
in-process — no server process, no extra dependencies (backend tests already
drive the app the same way; httpx/uvicorn are not in the locked lane).
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
NAMESPACE = "mai-fxt-01-harness"
TMP_ROOT = Path("/tmp")
ENVELOPE_SCHEMA = "mai-evidence-envelope/1"
REDACTED = "[REDACTED]"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_UNKNOWN = 2
EXIT_UNIMPLEMENTED = 3

# C10: "secrets never enter repo/transcripts/evidence". Key names that carry
# credential material are dropped entirely; URL userinfo (database URLs) is
# scrubbed in place; both rules apply recursively to everything recorded.
_SECRET_KEY = re.compile(
    r"(token|secret|passwo?rd|passwd|api_?key|credential|authorization|cookie|private)",
    re.IGNORECASE,
)
# Credential material also hides under bare stems: a var named *_KEY/*_TOKEN/
# *_SECRET/*_PASSWORD (any provider prefix, e.g. LITELLM_KEY) is treated as
# secret regardless of what the name contains.
_ENV_SECRET_SUFFIX = re.compile(r"(?:^|_)(key|token|secret|password)$", re.IGNORECASE)
_URL_USERINFO = re.compile(r"(://[^/@\s:]+:)[^@\s]+(@)")


@dataclass(frozen=True)
class Scenario:
    """One registry row: identity, ownership, expectation, status."""

    id: str
    title: str
    producer: str
    source: str
    expected: str
    implemented: bool


SCENARIOS: dict[str, Scenario] = {
    "health": Scenario(
        id="health",
        title="API liveness over the real ASGI app (lifespan + request)",
        producer="01-harness",
        source="brief mai_01-harness.md AC3; backend/tests/test_health.py",
        expected=(
            "lifespan starts and stops cleanly; GET /api/v1/health returns "
            '200 {"status": "ok"} with an X-Request-ID header; scratch dir '
            "removed; namespace left clear"
        ),
        implemented=True,
    ),
    "workspace-smoke": Scenario(
        id="workspace-smoke",
        title="Assembled workspace runtime scenarios (registry WS-*)",
        producer="01-workspace-runtime",
        source="docs/architecture/runtime-scenarios.md section WS (design-frozen)",
        expected="real-browser workspace scenario owned by 01-workspace-runtime at the P01 gate",
        implemented=False,
    ),
}


def redact(value: Any) -> Any:
    """Recursive sanitization for anything that enters an evidence envelope."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            hit = _SECRET_KEY.search(key_str) or _ENV_SECRET_SUFFIX.search(key_str)
            out[key_str] = REDACTED if hit else redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _URL_USERINFO.sub(rf"\g<1>{REDACTED}\g<2>", value)
    return value


def source_identity() -> dict[str, Any]:
    """Exact commit/tree of the tree this run executed (C10 rule 1)."""

    def git(*args: str) -> str:
        try:
            done = subprocess.run(
                ["git", "-C", str(REPO_ROOT), *args],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return done.stdout.strip() if done.returncode == 0 else ""

    commit = git("rev-parse", "HEAD") or "(git unavailable)"
    branch = git("rev-parse", "--abbrev-ref", "HEAD") or "(git unavailable)"
    status = git("status", "--porcelain")
    dirty_paths = sorted(
        line[3:].strip().strip('"') for line in status.splitlines() if line.strip()
    )
    return {
        "commit": commit,
        "branch": branch,
        "dirty": bool(dirty_paths),
        "dirty_paths": dirty_paths,
    }


def build_identity() -> dict[str, Any]:
    """Build/environment identity of the interpreter that ran the scenario."""
    versions: dict[str, str] = {}
    for dist in ("mai", "fastapi", "starlette", "dbos", "alembic"):
        try:
            versions[dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            versions[dist] = "absent"
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "packages": versions,
    }


def namespace_scratch_dir(purpose: str, tmp_root: Path) -> Path:
    """Create a namespace-scoped scratch dir (C10 fixture-namespace rule)."""
    return Path(tempfile.mkdtemp(prefix=f"{NAMESPACE}-{purpose}-", dir=tmp_root))


def namespace_leftovers(tmp_root: Path) -> list[str]:
    """Everything still present under the card's fixture namespace."""
    return sorted(glob.glob(str(tmp_root / f"{NAMESPACE}-*")))


def _ensure_mai_importable() -> None:
    """Make ``mai`` importable when running from a source tree checkout."""
    try:
        import mai  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))


async def _asgi_request(app: Any, method: str, path: str) -> dict[str, Any]:
    """One HTTP request through the real ASGI callable (no test-client dep)."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"runtime-check")],
        "client": ("127.0.0.1", 0),
        "server": ("runtime-check", 0),
    }
    response: dict[str, Any] = {"status": None, "headers": [], "body": b""}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response["status"] = message["status"]
            response["headers"] = [
                (k.decode("ascii"), v.decode("ascii"))
                for k, v in message.get("headers", [])
            ]
        elif message["type"] == "http.response.body":
            response["body"] += message.get("body", b"")

    # ServerErrorMiddleware re-raises after the handler response; the recorded
    # response messages are the observable contract (same as backend tests).
    try:
        await app(scope, receive, send)
    except Exception:  # noqa: BLE001
        pass
    response["body_text"] = response["body"].decode("utf-8", "replace")
    return response


def _health_transcript() -> dict[str, Any]:
    """Run the minimal real health path: lifespan up, request, lifespan down."""
    _ensure_mai_importable()
    from mai.api.app import HEALTH_PATH, create_app

    app = create_app(env={})  # no database configured: liveness must still serve

    async def flow() -> dict[str, Any]:
        sent: list[str] = []
        startup_complete = asyncio.Event()
        request_finished = asyncio.Event()
        startup_requested = False

        async def receive() -> dict[str, str]:
            nonlocal startup_requested
            if not startup_requested:
                startup_requested = True
                return {"type": "lifespan.startup"}
            await request_finished.wait()
            return {"type": "lifespan.shutdown"}

        async def send(message: Mapping[str, Any]) -> None:
            sent.append(str(message.get("type")))
            if message.get("type") == "lifespan.startup.complete":
                startup_complete.set()

        lifespan_task = asyncio.create_task(
            app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
        )
        await asyncio.wait_for(startup_complete.wait(), timeout=10.0)
        response = await _asgi_request(app, "GET", HEALTH_PATH)
        request_finished.set()
        await asyncio.wait_for(lifespan_task, timeout=10.0)
        return {"lifespan_messages": sent, "response": response}

    return asyncio.run(flow())


def run_scenario(scenario: Scenario, tmp_root: Path) -> dict[str, Any]:
    """Execute one implemented scenario and return its check results."""
    started = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    transcript: dict[str, Any] = {"lifespan_messages": [], "response": {}}
    scratch: Path | None = None
    scratch_removed = False
    try:
        scratch = namespace_scratch_dir(scenario.id, tmp_root)
        transcript = _health_transcript()
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
            scratch_removed = not scratch.exists()

    response = transcript.get("response", {})
    body_text = response.get("body_text", "")
    try:
        body_json = json.loads(body_text) if body_text else None
    except json.JSONDecodeError:
        body_json = None
    headers = dict(response.get("headers", []))
    leftovers = namespace_leftovers(tmp_root)

    checks = [
        {
            "name": "lifespan_startup_and_shutdown",
            "expected": ["lifespan.startup.complete", "lifespan.shutdown.complete"],
            "observed": transcript.get("lifespan_messages", []),
            "ok": transcript.get("lifespan_messages")
            == ["lifespan.startup.complete", "lifespan.shutdown.complete"],
        },
        {
            "name": "liveness_status",
            "expected": 200,
            "observed": response.get("status"),
            "ok": response.get("status") == 200,
        },
        {
            "name": "liveness_body",
            "expected": {"status": "ok"},
            "observed": body_json,
            "ok": body_json == {"status": "ok"},
        },
        {
            "name": "request_id_header",
            "expected": "X-Request-ID present",
            "observed": bool(headers.get("x-request-id")),
            "ok": bool(headers.get("x-request-id")),
        },
        {
            "name": "scratch_removed",
            "expected": True,
            "observed": scratch_removed,
            "ok": scratch_removed,
        },
        {
            "name": "namespace_clear",
            "expected": [],
            "observed": leftovers,
            "ok": leftovers == [],
        },
    ]
    finished = datetime.now(timezone.utc)
    return {
        "started_utc": started.isoformat(timespec="milliseconds"),
        "finished_utc": finished.isoformat(timespec="milliseconds"),
        "duration_s": round(time.monotonic() - started_monotonic, 3),
        "checks": checks,
        "passed": all(check["ok"] for check in checks),
    }


def write_envelope(
    scenario: Scenario, result: dict[str, Any], evidence_dir: Path, command: list[str]
) -> Path:
    """Build and atomically write the sanitized evidence envelope (C10)."""
    identity = source_identity()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    commit_part = identity["commit"][:12].replace("(git unavailable)", "nogit")
    envelope = {
        "schema": ENVELOPE_SCHEMA,
        "scenario": {
            "id": scenario.id,
            "title": scenario.title,
            "producer": scenario.producer,
            "source": scenario.source,
        },
        "verdict": "pass" if result["passed"] else "fail",
        "source_identity": identity,
        "build_identity": build_identity(),
        "command": list(command),
        "namespace": NAMESPACE,
        "timestamps": {
            "started_utc": result["started_utc"],
            "finished_utc": result["finished_utc"],
            "duration_s": result["duration_s"],
        },
        "expected": scenario.expected,
        "checks": redact(result["checks"]),
        "environment": redact(dict(os.environ)),
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target = evidence_dir / f"{scenario.id}-{stamp}-{commit_part}.json"
    tmp_file = target.with_suffix(".json.tmp")
    tmp_file.write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n")
    os.replace(tmp_file, target)
    return target


def cmd_list() -> int:
    """Print the scenario catalog; implemented rows first."""
    width = max(len(sid) for sid in SCENARIOS)
    for scenario in sorted(SCENARIOS.values(), key=lambda s: (not s.implemented, s.id)):
        status = "implemented" if scenario.implemented else "unimplemented"
        print(f"{scenario.id:<{width}}  {status:<14}  {scenario.producer:<22}  {scenario.title}")
    return EXIT_OK


def cmd_run(
    scenario_id: str,
    evidence_dir: Path | None,
    tmp_root: Path,
    command: list[str],
) -> int:
    """Run one scenario; unknown/unimplemented never record a PASS."""
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        print(
            f"unknown scenario: {scenario_id!r}; known: {', '.join(sorted(SCENARIOS))}",
            file=sys.stderr,
        )
        return EXIT_UNKNOWN
    if not scenario.implemented:
        print(
            f"scenario {scenario_id!r} is registered but not implemented by its "
            f"producer ({scenario.producer}); no PASS is recorded",
            file=sys.stderr,
        )
        return EXIT_UNIMPLEMENTED
    if evidence_dir is None:
        evidence_dir = Path(os.environ["MAI_EVIDENCE_DIR"]) if os.environ.get("MAI_EVIDENCE_DIR") else None
        if evidence_dir is None:
            print(
                "no evidence directory: pass --evidence-dir or set MAI_EVIDENCE_DIR",
                file=sys.stderr,
            )
            return EXIT_UNKNOWN

    result = run_scenario(scenario, tmp_root)
    envelope_path = write_envelope(scenario, result, evidence_dir, command)
    for check in result["checks"]:
        mark = "ok" if check["ok"] else "FAIL"
        print(f"  [{mark}] {check['name']}: expected={check['expected']!r} observed={check['observed']!r}")
    print(f"verdict: {'pass' if result['passed'] else 'fail'}; envelope: {envelope_path}")
    return EXIT_OK if result["passed"] else EXIT_FAIL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runtime_check.py",
        description="MAI runtime scenario runner and evidence envelope (card 01-harness).",
        epilog=(
            "Registered scenarios: "
            + ", ".join(f"{sid} (implemented)" if sc.implemented else f"{sid} (unimplemented, owner {sc.producer})" for sid, sc in sorted(SCENARIOS.items()))
            + ". Unknown or unimplemented scenarios exit nonzero and never record a PASS."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="print the registered scenario catalog")
    run_parser = sub.add_parser("run", help="run one scenario and write an evidence envelope")
    run_parser.add_argument("scenario", help="scenario id (see 'list')")
    run_parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        help="directory for the JSON evidence envelope (default: $MAI_EVIDENCE_DIR)",
    )
    run_parser.add_argument(
        "--tmp-root",
        type=Path,
        default=TMP_ROOT,
        help="root for %s-* scratch dirs (default: /tmp)" % NAMESPACE,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = list(argv) if argv is not None else sys.argv[1:]
    if args.command == "list":
        return cmd_list()
    return cmd_run(args.scenario, args.evidence_dir, args.tmp_root, command)


if __name__ == "__main__":
    sys.exit(main())
