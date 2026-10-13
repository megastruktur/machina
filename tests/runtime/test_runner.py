"""Focused tests for the runtime scenario runner and evidence envelope (01-harness).

Anchors: brief `mai_01-harness.md` acceptance 1–3; contracts.md C10 (sanitized
evidence, fixture namespace, cleanup proof); runtime-scenarios.md status legend
(unknown/unimplemented scenario never records PASS).
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "runtime_check", REPO_ROOT / "scripts" / "runtime_check.py"
)
assert _SPEC is not None and _SPEC.loader is not None
runtime_check = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runtime_check
_SPEC.loader.exec_module(runtime_check)


def _run(tmp_path: Path, scenario: str) -> int:
    return runtime_check.main(
        [
            "run",
            scenario,
            "--evidence-dir",
            str(tmp_path / "evidence"),
            "--tmp-root",
            str(tmp_path),
        ]
    )


def _load_envelope(tmp_path: Path) -> dict:
    files = list((tmp_path / "evidence").glob("health-*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text())


def test_help_names_commands_scenarios_and_exit_contract(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        runtime_check.main(["--help"])
    assert excinfo.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    for fragment in (
        "list",
        "run",
        "health (implemented)",
        "workspace-smoke (unimplemented, owner 01-workspace-runtime)",
        "exit nonzero",
    ):
        assert fragment in out, fragment

    # The `run` subcommand documents the evidence envelope and scratch controls.
    with pytest.raises(SystemExit) as excinfo:
        runtime_check.main(["run", "--help"])
    assert excinfo.value.code == 0
    run_help = " ".join(capsys.readouterr().out.split())
    for fragment in ("--evidence-dir", "--tmp-root", "scenario", "MAI_EVIDENCE_DIR"):
        assert fragment in run_help, fragment


def test_list_reports_full_catalog(capsys: pytest.CaptureFixture[str]) -> None:
    assert runtime_check.main(["list"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"^health\s+implemented\s+01-harness", out, re.MULTILINE)
    assert re.search(
        r"^workspace-smoke\s+unimplemented\s+01-workspace-runtime", out, re.MULTILINE
    )


def test_health_scenario_passes_with_complete_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAI_DATABASE_URL", "postgresql://runner:scrub-me@localhost:5432/mai")
    assert _run(tmp_path, "health") == 0
    envelope = _load_envelope(tmp_path)

    assert envelope["schema"] == "mai-evidence-envelope/1"
    assert envelope["verdict"] == "pass"
    assert envelope["scenario"]["id"] == "health"
    assert envelope["scenario"]["producer"] == "01-harness"

    source = envelope["source_identity"]
    assert re.fullmatch(r"[0-9a-f]{40}", source["commit"])
    assert source["branch"]
    assert isinstance(source["dirty"], bool)
    assert isinstance(source["dirty_paths"], list)

    build = envelope["build_identity"]
    assert build["python"]
    assert build["packages"]["fastapi"] != "absent"
    assert build["packages"]["mai"] != "absent"

    assert envelope["command"][0] == "run"
    assert envelope["command"][1] == "health"
    assert envelope["namespace"] == "mai-fxt-01-harness"
    timestamps = envelope["timestamps"]
    assert timestamps["started_utc"] <= timestamps["finished_utc"]
    assert timestamps["duration_s"] >= 0
    assert envelope["expected"]

    by_name = {check["name"]: check for check in envelope["checks"]}
    assert by_name["liveness_status"] == {
        "name": "liveness_status",
        "expected": 200,
        "observed": 200,
        "ok": True,
    }
    assert by_name["liveness_body"]["observed"] == {"status": "ok"}
    assert by_name["request_id_header"]["ok"] is True
    assert by_name["lifespan_startup_and_shutdown"]["observed"] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert by_name["scratch_removed"]["ok"] is True
    assert by_name["namespace_clear"]["observed"] == []
    assert all(check["ok"] for check in envelope["checks"])


def test_environment_secrets_never_reach_the_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAI_RUNNER_TOKEN", "super-secret-token-value")
    monkeypatch.setenv("MAI_DATABASE_URL", "postgresql://runner:scrub-me@localhost:5432/mai")
    monkeypatch.setenv("MAI_PLAIN_VALUE", "not-a-secret")
    assert _run(tmp_path, "health") == 0
    envelope = _load_envelope(tmp_path)

    assert "super-secret-token-value" not in json.dumps(envelope)
    assert "scrub-me" not in json.dumps(envelope)
    environment = envelope["environment"]
    assert environment["MAI_RUNNER_TOKEN"] == "[REDACTED]"
    assert environment["MAI_DATABASE_URL"] == "postgresql://runner:[REDACTED]@localhost:5432/mai"
    assert environment["MAI_PLAIN_VALUE"] == "not-a-secret"


def test_sk_api_key_env_value_never_reaches_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """C10 negative: a live sk- key under a bare *_KEY name is redacted."""
    monkeypatch.setenv("LITELLM_KEY", "sk-live-0123456789abcdef")  # no api_key/token stem
    monkeypatch.setenv("MAI_PLAIN_VALUE", "not-a-secret")
    assert _run(tmp_path, "health") == 0

    # Raw file proof: the serialized envelope must not contain the key value.
    envelope_file = next(iter((tmp_path / "evidence").glob("health-*.json")))
    raw = envelope_file.read_text()
    assert "sk-live-0123456789abcdef" not in raw
    assert "sk-" not in raw

    envelope = json.loads(raw)
    environment = envelope["environment"]
    assert environment["LITELLM_KEY"] == "[REDACTED]"
    assert environment["MAI_PLAIN_VALUE"] == "not-a-secret"


def test_unknown_scenario_exits_nonzero_and_writes_no_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, "definitely-not-a-scenario") == 2
    assert not (tmp_path / "evidence").exists()
    err = capsys.readouterr().err
    assert "unknown scenario" in err
    assert "health" in err  # names the known alternatives


def test_unimplemented_scenario_exits_nonzero_without_stub_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, "workspace-smoke") == 3
    assert not (tmp_path / "evidence").exists()
    err = capsys.readouterr().err
    assert "not implemented" in err
    assert "01-workspace-runtime" in err
    assert "no PASS is recorded" in err


def test_namespace_leftover_fails_the_run(tmp_path: Path) -> None:
    stray = tmp_path / f"{runtime_check.NAMESPACE}-stray-leftover"
    stray.mkdir()
    assert _run(tmp_path, "health") == 1
    envelope = _load_envelope(tmp_path)

    assert envelope["verdict"] == "fail"
    by_name = {check["name"]: check for check in envelope["checks"]}
    assert by_name["namespace_clear"]["ok"] is False
    assert by_name["namespace_clear"]["observed"] == [str(stray)]
    # the scenario's own scratch dir was still removed; only the stray remains
    assert by_name["scratch_removed"]["ok"] is True
    assert [p.name for p in tmp_path.glob(f"{runtime_check.NAMESPACE}-*")] == [
        stray.name
    ]
