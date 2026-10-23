"""Dependency lane guard for the P06 agent/provider set (card 06-dependencies).

Acceptance criteria (card brief):
1. Selected supported PydanticAI/provider versions locked with minimal needed
   extras, exact APIs verified.
2. No automatic proprietary-provider expansion or embedded alternate scheduler.

Selection provenance: IDEA.md §2/§8 selects PydanticAI over configurable
OpenAI-compatible endpoints (contracts.md C06; AGT-PROVIDER scenario in
docs/architecture/runtime-scenarios.md — the live provider reaches MAI through
the configured gateway, credentials via .env). The manifest therefore declares
the slim distribution with ONLY the ``openai`` extra; every other provider SDK
and every scheduler extra of pydantic-ai is held out by the negative controls
below — the durable engine stays the frozen direct pin ``dbos==3.2.0``
(contracts.md C05), so pydantic-ai's dbos/temporal/prefect extras are
alternate-engine vectors. The API surface downstream adapters will call is
asserted offline against the installed distribution (constructor + deferred
tool + fake-LLM protocol surface, C06 rules 4/6), never invented.
"""

import re
import subprocess
import tomllib
from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path

import pydantic_ai

BACKEND_DIR = Path(__file__).resolve().parents[1]

# The one selected agent-runtime requirement: distribution, its single enabled
# extra, and the exact pin. The version is pinned EXACTLY (not floored) in
# lockstep with pydantic-ai-harness, whose own hard pin
# pydantic-ai-slim==2.60.0 forces the pair to move together deliberately.
AGENT_RUNTIME_DISTRIBUTION = "pydantic-ai-slim"
AGENT_RUNTIME_EXTRA = "openai"
AGENT_RUNTIME_PIN = "==2.60.0"
# The conversation-compaction capability library (TieredCompaction and its
# tier strategies attach to the per-round Agent); pinned exactly because the
# harness itself hard-pins pydantic-ai-slim.
HARNESS_DISTRIBUTION = "pydantic-ai-harness"
HARNESS_PIN = "==0.60.0"

# Provider SDKs pydantic-ai supports but MAI did not select: the configured
# gateway is OpenAI-compatible (criterion 2 — no automatic proprietary-provider
# expansion). Distribution names for manifest/lock checks; import names for the
# installed-environment check.
UNSELECTED_PROVIDER_DISTRIBUTIONS = (
    "anthropic",
    "google-genai",
    "groq",
    "cohere",
    "mistralai",
    "boto3",
    "botocore",
    "xai-sdk",
    "voyageai",
    "tavily-python",
    "exa-py",
    "fastmcp-slim",
    "fastmcp-tasks",
    "sentence-transformers",
    "ddgs",
)
UNSELECTED_PROVIDER_IMPORTS = (
    "anthropic",
    "google.genai",
    "groq",
    "cohere",
    "mistralai",
    "boto3",
    "botocore",
    "xai_sdk",
    "voyageai",
    "tavily",
    "exa_py",
    "fastmcp",
    "sentence_transformers",
    "ddgs",
)

# Alternate schedulers/task queues: the durable engine is DBOS exactly as
# pinned (contracts.md C05 rule 1 — no second scheduler, no hidden queue).
UNSELECTED_SCHEDULER_DISTRIBUTIONS = (
    "celery",
    "rq",
    "dramatiq",
    "huey",
    "arq",
    "apscheduler",
    "prefect",
    "temporalio",
    "dagster",
    "procrastinate",
    "taskiq",
    "kuyruk",
)

# Frozen engine pin (contracts.md C05: "dbos==3.2.0 is the durable engine").
FROZEN_ENGINE_REQUIREMENT = "dbos==3.2.0"
FROZEN_ENGINE_VERSION = "3.2.0"

# Floor the `openai` extra declares for the provider SDK (pydantic-ai-slim
# 2.56 metadata: openai>=3.26.0, tiktoken>=0.12.0).
PROVIDER_SDK_FLOOR = (3, 26)


def _declared_requirements() -> list[str]:
    with (BACKEND_DIR / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return list(data["project"]["dependencies"])


def _distribution_names(requirements: list[str]) -> list[str]:
    """PEP 508 entry -> lowercase distribution name (markers/versions stripped)."""
    return [re.split(r"[<>=!~\[\s]", entry.split(";")[0].strip(), maxsplit=1)[0].lower()
            for entry in requirements]


def _requirements_for(requirements: list[str], distribution: str) -> list[str]:
    """Raw PEP 508 entries whose distribution name matches (extras preserved)."""
    return [entry for entry in requirements
            if re.split(r"[<>=!~\[\s]", entry.split(";")[0].strip(), maxsplit=1)[0].lower() == distribution]


def _locked_packages() -> dict[str, str]:
    with (BACKEND_DIR / "uv.lock").open("rb") as handle:
        return {package["name"].lower(): package["version"] for package in tomllib.load(handle)["package"]}


def _extras_of(requirement: str) -> list[str]:
    match = re.search(r"\[([^\]]*)\]", requirement)
    return [extra.strip() for extra in match[1].split(",") if extra.strip()] if match else []


def _importable(name: str) -> bool:
    try:
        return find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        # Dotted name with a missing parent, or unparsable — not installed.
        return False


def test_manifest_declares_exactly_the_selected_agent_runtime() -> None:
    entries = _requirements_for(_declared_requirements(), AGENT_RUNTIME_DISTRIBUTION)
    assert len(entries) == 1, f"expected exactly one {AGENT_RUNTIME_DISTRIBUTION} entry: {entries}"
    entry = entries[0]
    assert _extras_of(entry) == [AGENT_RUNTIME_EXTRA], (
        f"minimal extras violated — expected only [{AGENT_RUNTIME_EXTRA}]: {entry}"
    )
    assert AGENT_RUNTIME_PIN in entry, f"exact pin {AGENT_RUNTIME_PIN} missing: {entry}"
    harness_entries = _requirements_for(_declared_requirements(), HARNESS_DISTRIBUTION)
    assert harness_entries == [HARNESS_DISTRIBUTION + HARNESS_PIN], (
        f"harness must be declared exactly once as {HARNESS_DISTRIBUTION}{HARNESS_PIN}: {harness_entries}"
    )
    # The meta-package would silently pull additional provider SDKs: never
    # declare it alongside the slim distribution.
    assert "pydantic-ai" not in _distribution_names(_declared_requirements()), (
        "meta-package 'pydantic-ai' must not be declared (pulls unselected provider extras)"
    )


def test_lock_resolves_agent_runtime_in_lockstep() -> None:
    locked = _locked_packages()
    slim = locked.get(AGENT_RUNTIME_DISTRIBUTION)
    assert slim is not None, "agent runtime missing from uv.lock"
    graph = locked.get("pydantic-graph")
    assert graph == slim, (
        f"pydantic-graph {graph} out of lockstep with {AGENT_RUNTIME_DISTRIBUTION} {slim}"
    )
    openai_version = locked.get("openai")
    assert openai_version is not None, "provider SDK missing from uv.lock"
    assert tuple(int(part) for part in openai_version.split(".")[:2]) >= PROVIDER_SDK_FLOOR, (
        f"openai {openai_version} below the extra's documented floor {PROVIDER_SDK_FLOOR}"
    )
    assert locked.get("tiktoken") is not None, "openai-extra tiktoken requirement unresolved"


def test_manifest_declares_no_proprietary_provider_sdk() -> None:
    declared = _distribution_names(_declared_requirements())
    offenders = sorted(set(declared) & set(UNSELECTED_PROVIDER_DISTRIBUTIONS))
    assert offenders == [], f"unselected provider SDKs in pyproject: {offenders}"


def test_lock_contains_no_proprietary_provider_distribution() -> None:
    locked = _locked_packages()
    offenders = sorted(locked.keys() & set(UNSELECTED_PROVIDER_DISTRIBUTIONS))
    assert offenders == [], f"unselected provider distributions in uv.lock: {offenders}"


def test_locked_environment_has_no_proprietary_provider_importable() -> None:
    present = sorted(name for name in UNSELECTED_PROVIDER_IMPORTS if _importable(name))
    assert present == [], f"unselected provider SDKs importable in locked env: {present}"


def test_manifest_declares_no_alternate_scheduler() -> None:
    declared = _distribution_names(_declared_requirements())
    offenders = sorted(set(declared) & set(UNSELECTED_SCHEDULER_DISTRIBUTIONS))
    assert offenders == [], f"alternate schedulers in pyproject: {offenders}"
    engine = _requirements_for(_declared_requirements(), "dbos")
    assert engine == [FROZEN_ENGINE_REQUIREMENT], (
        f"frozen engine pin changed (contracts.md C05): {engine}"
    )


def test_lock_contains_no_alternate_scheduler() -> None:
    locked = _locked_packages()
    offenders = sorted(locked.keys() & set(UNSELECTED_SCHEDULER_DISTRIBUTIONS))
    assert offenders == [], f"alternate schedulers in uv.lock: {offenders}"


def test_lock_is_consistent_with_manifest() -> None:
    proc = subprocess.run(
        ["uv", "lock", "--check"], cwd=BACKEND_DIR, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"uv.lock out of sync with pyproject: {proc.stdout}{proc.stderr}"


def test_locked_engine_version_matches_frozen_pin() -> None:
    locked = _locked_packages()
    assert locked.get("dbos") == FROZEN_ENGINE_VERSION, (
        f"dbos {locked.get('dbos')} != frozen {FROZEN_ENGINE_VERSION} (contracts.md C05)"
    )


def test_installed_agent_runtime_version_matches_lock() -> None:
    locked = _locked_packages()
    installed = version(AGENT_RUNTIME_DISTRIBUTION)
    assert installed == locked[AGENT_RUNTIME_DISTRIBUTION] == pydantic_ai.__version__, (
        f"installed {installed} / lock {locked[AGENT_RUNTIME_DISTRIBUTION]} / "
        f"module {pydantic_ai.__version__} disagree"
    )
    assert tuple(int(part) for part in version("openai").split(".")[:2]) >= PROVIDER_SDK_FLOOR


def test_verified_model_adapter_api_surface() -> None:
    # Offline construction only — never dials. Proves the exact classes and
    # constructor arguments downstream P06 adapters (06-agent-run,
    # 06-tool-bridge) will use exist on the pinned version: not invented.
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    from pydantic_ai.tools import Tool

    provider = OpenAIProvider(base_url="http://127.0.0.1:4000/v1", api_key="guard-offline")
    model = OpenAIChatModel("mai-guard-probe", provider=provider)

    def echo(ctx, x: int) -> int:  # RunContext-typed tool, the C06 tool-bridge shape
        return x

    agent = Agent(model=model, instructions="guard probe", tools=[Tool(echo, name="echo")])
    assert type(agent).__name__ == "Agent"
    assert str(provider.client.base_url).rstrip("/") == "http://127.0.0.1:4000/v1"


def test_deferred_tool_and_fake_llm_protocol_surface() -> None:
    # C06 rule 4 names pydanticAI deferred tools; rule 6 demands deterministic
    # fake-LLM protocol tests. Both surfaces must exist on the pinned version.
    from pydantic_ai import capture_run_messages
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
    )
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

    deferred = DeferredToolRequests()
    assert deferred.calls == [] and deferred.approvals == []
    assert DeferredToolResults is not None
    assert callable(capture_run_messages)
    assert ModelRequest is not None and ModelSettings is not None
    response = ModelResponse(parts=[TextPart("ok"), ToolCallPart(tool_name="echo", args={"x": 1})])
    assert isinstance(response.parts[0], TextPart)
    assert ToolReturnPart(tool_name="echo", content=1) is not None
