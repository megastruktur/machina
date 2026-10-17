"""Dependency lane guard for the P09 MCP/plugin-schema set (card 09-dependencies).

Acceptance criteria (card brief):
1. Frozen MCP transport/entrypoint and schema supported library APIs locked;
   no automatic pip install endpoint.
2. Minimal sample external server can initialize from clean runtime;
   compatibility failure explicit.

Selection provenance: IDEA.md §13 selects external MCP servers and
schema-generated configuration forms as extension entry points; contracts.md
C03 rule 7 freezes the trust model — native Python plugins are trusted
installed code loaded via importlib metadata entry points, MCP tools carry
independent grants and server scope, and plugin validation is not sandboxing.
The official modelcontextprotocol python-sdk (distribution ``mcp``) is the
selected protocol implementation, declared DIRECTLY (same convention as the
``openai`` declaration in 07-dependencies) instead of adopting
pydantic-ai-slim's ``[mcp]`` extra, so the agent runtime's frozen extra set
stays exactly ``{openai}`` (06-dependencies) and the plugin layer owns
connection policy. Frozen v1 transports: **stdio** (locally spawned servers,
explicitly scoped subprocess environment) and **streamable HTTP** (separately
hosted servers, IDEA.md §13); the deprecated SSE transports are not part of
the frozen surface. Schema runtime is **pydantic** — declared directly while
it was previously only locked transitively through fastapi/pydantic-ai-slim
even though src/mai already imports it everywhere. No runtime installer is
added: a plugin enters the runtime by being an installed distribution, never
by an in-process pip/uv invocation, so installer distributions are held out
by the negative controls below. The transport and schema API surfaces the
downstream cards (09-mcp, 09-native-plugin, 09-plugin-store, 09-plugin-api)
will call are asserted against the installed distributions — offline
construction plus one real local stdio subprocess fixture, never invented,
never dialed out.
"""

import asyncio
import inspect
import os
import re
import subprocess
import sys
import threading
import tomllib
from importlib.metadata import entry_points, version
from importlib.util import find_spec
from pathlib import Path

import pydantic

BACKEND_DIR = Path(__file__).resolve().parents[1]

# The two selected plugin-runtime requirements: distribution, manifest bounds
# (floor = the version whose API is verified below; cap = deliberate
# major-bump reconciliation, repo convention) and the locked versions the API
# checks run against.
MCP_DISTRIBUTION = "mcp"
MCP_FLOOR = ">=1.30"
MCP_MAJOR_CAP = "<2"
SCHEMA_DISTRIBUTION = "pydantic"
SCHEMA_FLOOR = ">=2.14"
SCHEMA_MAJOR_CAP = "<3"

# Alternate MCP client/server distributions that MAI did not select: the
# official SDK is the one protocol implementation (criterion 1 — no
# parallel MCP stack). The FastMCP v2 framework is a separate distribution;
# the SDK's built-in ``mcp.server.fastmcp`` module used by the fixture below
# lives INSIDE the selected distribution and is unaffected.
UNSELECTED_MCP_DISTRIBUTIONS = ("fastmcp", "fastmcp-slim", "fastmcp-tasks")
UNSELECTED_MCP_IMPORTS = ("fastmcp",)

# Runtime installer distributions: plugin loading is importlib entry-point
# discovery of trusted installed code (C03 rule 7), never an in-process
# package installation — there is no automatic pip/uv install endpoint.
INSTALLER_DISTRIBUTIONS = ("pip", "uv", "pipx", "poetry")
INSTALLER_IMPORTS = ("pip", "uv")

# pydantic-ai-slim's extras stay frozen at the 06-dependencies selection; the
# [mcp] extra must not appear as a hidden second adoption path.
AGENT_RUNTIME_DISTRIBUTION = "pydantic-ai-slim"
AGENT_RUNTIME_FROZEN_EXTRAS = {"openai"}


def _declared_requirements() -> list[str]:
    with (BACKEND_DIR / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return list(data["project"]["dependencies"])


def _distribution_names(requirements: list[str]) -> list[str]:
    """PEP 508 entry -> lowercase distribution name (markers/versions stripped)."""
    return [
        re.split(r"[<>=!~\[\s]", entry.split(";")[0].strip(), maxsplit=1)[0].lower()
        for entry in requirements
    ]


def _requirements_for(requirements: list[str], distribution: str) -> list[str]:
    """Raw PEP 508 entries whose distribution name matches (extras preserved)."""
    return [
        entry
        for entry in requirements
        if re.split(r"[<>=!~\[\s]", entry.split(";")[0].strip(), maxsplit=1)[0].lower()
        == distribution
    ]


def _locked_packages() -> dict[str, str]:
    with (BACKEND_DIR / "uv.lock").open("rb") as handle:
        return {
            package["name"].lower(): package["version"]
            for package in tomllib.load(handle)["package"]
        }


def _extras_of(requirement: str) -> list[str]:
    match = re.search(r"\[([^\]]*)\]", requirement)
    return (
        [extra.strip() for extra in match[1].split(",") if extra.strip()]
        if match
        else []
    )


def _importable(name: str) -> bool:
    try:
        return find_spec(name) is not None
    except ModuleNotFoundError, ValueError:
        # Dotted name with a missing parent, or unparsable — not installed.
        return False


def _version_tuple(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version(name).split(".")[:3])


# ---------------------------------------------------------------------------
# persistent test loop (same discipline as tests/test_agent_run.py: no
# pytest-asyncio in the dev group)
# ---------------------------------------------------------------------------

_TEST_LOOP: asyncio.AbstractEventLoop | None = None
_TEST_LOOP_LOCK = threading.Lock()


def _run(coro: object) -> object:
    """Run one coroutine on the persistent test loop; block for the result."""
    global _TEST_LOOP
    with _TEST_LOOP_LOCK:
        if _TEST_LOOP is None:
            _TEST_LOOP = asyncio.new_event_loop()
            threading.Thread(target=_TEST_LOOP.run_forever, daemon=True).start()
    assert _TEST_LOOP is not None
    return asyncio.run_coroutine_threadsafe(coro, _TEST_LOOP).result()


# ---------------------------------------------------------------------------
# manifest / lock / environment shape
# ---------------------------------------------------------------------------


def test_manifest_declares_exactly_the_selected_plugin_set() -> None:
    declared = _declared_requirements()
    for distribution, floor, cap in (
        (MCP_DISTRIBUTION, MCP_FLOOR, MCP_MAJOR_CAP),
        (SCHEMA_DISTRIBUTION, SCHEMA_FLOOR, SCHEMA_MAJOR_CAP),
    ):
        entries = _requirements_for(declared, distribution)
        assert len(entries) == 1, (
            f"expected exactly one {distribution} entry: {entries}"
        )
        assert floor in entries[0], (
            f"{distribution} floor {floor} missing: {entries[0]}"
        )
        assert cap in entries[0], f"{distribution} cap {cap} missing: {entries[0]}"
        # Direct declarations only — no extra adoption path (the mcp extra on
        # pydantic-ai-slim stays unselected; pydantic needs no extras).
        assert _extras_of(entries[0]) == [], (
            f"{distribution} must be declared without extras: {entries[0]}"
        )


def test_agent_runtime_extras_stay_frozen() -> None:
    declared = _declared_requirements()
    entries = _requirements_for(declared, AGENT_RUNTIME_DISTRIBUTION)
    assert len(entries) == 1, (
        f"expected exactly one {AGENT_RUNTIME_DISTRIBUTION} entry: {entries}"
    )
    assert set(_extras_of(entries[0])) == AGENT_RUNTIME_FROZEN_EXTRAS, (
        f"pydantic-ai-slim extras must stay exactly {sorted(AGENT_RUNTIME_FROZEN_EXTRAS)}: {entries[0]}"
    )


def test_lock_resolves_the_selected_plugin_set() -> None:
    locked = _locked_packages()
    for distribution, floor in (
        (MCP_DISTRIBUTION, (1, 30)),
        (SCHEMA_DISTRIBUTION, (2, 14)),
    ):
        resolved = locked.get(distribution)
        assert resolved is not None, f"{distribution} missing from uv.lock"
        assert tuple(int(part) for part in resolved.split(".")[:2]) >= floor, (
            f"{distribution} {resolved} below the API-verified floor {floor}"
        )
    # The MCP SDK's runtime requirements resolve in lockstep: anyio (both
    # transports), httpx (streamable HTTP transport), pydantic (wire models —
    # also the selected schema runtime), jsonschema (SDK-internal tool-schema
    # validation). Each enters as an mcp requirement, not a separate MAI
    # selection.
    for requirement in ("anyio", "httpx", "pydantic-settings", "jsonschema"):
        assert locked.get(requirement) is not None, (
            f"mcp requirement {requirement} unresolved"
        )


def test_lock_is_consistent_with_manifest() -> None:
    proc = subprocess.run(
        ["uv", "lock", "--check"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"uv.lock out of sync with pyproject: {proc.stdout}{proc.stderr}"
    )


def test_installed_versions_match_lock() -> None:
    locked = _locked_packages()
    for distribution in (MCP_DISTRIBUTION, SCHEMA_DISTRIBUTION):
        installed = version(distribution)
        assert installed == locked[distribution], (
            f"{distribution} {installed} installed != locked {locked[distribution]} "
            "(clean runtime proof: uv sync --frozen)"
        )


# ---------------------------------------------------------------------------
# negative controls: no parallel MCP stack, no runtime installer
# ---------------------------------------------------------------------------


def test_manifest_declares_no_alternate_mcp_distribution() -> None:
    declared = _distribution_names(_declared_requirements())
    offenders = sorted(set(declared) & set(UNSELECTED_MCP_DISTRIBUTIONS))
    assert offenders == [], f"unselected MCP distributions in pyproject: {offenders}"


def test_lock_contains_no_alternate_mcp_distribution() -> None:
    locked = _locked_packages()
    offenders = sorted(locked.keys() & set(UNSELECTED_MCP_DISTRIBUTIONS))
    assert offenders == [], f"unselected MCP distributions in uv.lock: {offenders}"


def test_locked_environment_has_no_alternate_mcp_importable() -> None:
    present = sorted(name for name in UNSELECTED_MCP_IMPORTS if _importable(name))
    assert present == [], f"unselected MCP SDKs importable in locked env: {present}"


def test_manifest_declares_no_runtime_installer() -> None:
    declared = _distribution_names(_declared_requirements())
    offenders = sorted(set(declared) & set(INSTALLER_DISTRIBUTIONS))
    assert offenders == [], f"installer distributions in pyproject: {offenders}"


def test_lock_contains_no_runtime_installer() -> None:
    locked = _locked_packages()
    offenders = sorted(locked.keys() & set(INSTALLER_DISTRIBUTIONS))
    assert offenders == [], f"installer distributions in uv.lock: {offenders}"


def test_locked_environment_has_no_installer_importable() -> None:
    # The uv-managed runtime venv ships no pip: an in-process "automatic pip
    # install endpoint" has no interpreter-level target either.
    present = sorted(name for name in INSTALLER_IMPORTS if _importable(name))
    assert present == [], f"installer packages importable in locked env: {present}"


# ---------------------------------------------------------------------------
# frozen MCP transport/entrypoint and schema API surfaces (criterion 1)
# ---------------------------------------------------------------------------


def test_selected_mcp_transport_entrypoints_exist() -> None:
    # The frozen v1 transports and the session entrypoint the plugin layer
    # wraps: stdio for locally spawned servers, streamable HTTP for
    # separately hosted ones (IDEA.md §13). The deprecated SSE transports
    # are deliberately absent from this surface.
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client

    params = list(StdioServerParameters.model_fields)
    for field in ("command", "args", "env", "cwd"):
        assert field in params, f"StdioServerParameters.{field} missing: {params}"
    # Scoped-environment surface: ``env`` is a first-class field so the
    # 09-mcp card can pass an explicitly scoped subprocess environment
    # (acceptance there: subprocess credential env scoped).
    assert (
        StdioServerParameters(command=sys.executable, args=[], env={"PATH": "/usr/bin"})
        is not None
    )
    assert "server" in str(list(inspect.signature(stdio_client).parameters))
    assert "url" in str(list(inspect.signature(streamablehttp_client).parameters))
    assert ClientSession is not None


def test_pydantic_schema_surface_for_plugin_config() -> None:
    # The exact schema surface the plugin cards will call: model_json_schema()
    # generates the configuration form (IDEA.md §13), TypeAdapter validates
    # stored config against it, ValidationError is the explicit compatibility
    # failure — never a silent defaulting.
    class PluginConfig(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="forbid")

        endpoint: str
        port: int = 8080

    schema = PluginConfig.model_json_schema()
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"endpoint"}
    assert schema["properties"]["port"]["type"] == "integer"

    adapter = pydantic.TypeAdapter(PluginConfig)
    parsed = adapter.validate_python({"endpoint": "http://127.0.0.1:9", "port": 9})
    assert parsed == PluginConfig(endpoint="http://127.0.0.1:9", port=9)
    assert adapter.validate_json('{"endpoint": "http://127.0.0.1:9"}') == PluginConfig(
        endpoint="http://127.0.0.1:9"
    )

    for bad in (
        {"port": 1},
        {"endpoint": "http://x", "unknown": True},
        {"endpoint": "http://x", "port": "nine"},
    ):
        try:
            adapter.validate_python(bad)
        except pydantic.ValidationError as exc:
            assert exc.title == PluginConfig.__name__
        else:  # pragma: no cover - guard
            raise AssertionError(f"incompatible config silently accepted: {bad}")


def test_entry_point_discovery_api_available() -> None:
    # Native plugins load as trusted installed code through importlib
    # metadata entry points (C03 rule 7) — stdlib surface the
    # 09-native-plugin card freezes its group contract onto.
    assert entry_points().select(group="console_scripts") is not None


# ---------------------------------------------------------------------------
# criterion 2: minimal sample external server from the clean runtime,
# explicit compatibility failure — real local stdio subprocesses
# ---------------------------------------------------------------------------


_FIXTURE_SERVER = """\
import logging

logging.disable(logging.CRITICAL)

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mai-p09-fixture")


@mcp.tool()
def echo(text: str) -> str:
    \"\"\"Return the input unchanged.\"\"\"
    return text


mcp.run()
"""

# A process that speaks non-JSON-RPC on stdout: the SDK's wire validation
# must fail the session explicitly.
_GARBAGE_SERVER = """\
import sys

sys.stdout.write("this is not json-rpc\\n")
sys.stdout.flush()
"""

# A process that exits before the handshake: no silent empty success.
_EXITING_SERVER = """\
import sys

sys.exit(0)
"""


def _scoped_env() -> dict[str, str]:
    # Deliberately minimal: no HOME, no inherited provider/credential
    # variables — the fixture proves a scoped environment initializes the
    # server (09-mcp consumes this discipline).
    return {"PATH": os.environ["PATH"]}


def _exception_leaves(exc: BaseException) -> list[BaseException]:
    leaves: list[BaseException] = []
    for sub in getattr(exc, "exceptions", ()):
        leaves.extend(_exception_leaves(sub))
    return leaves or [exc]


def test_minimal_external_mcp_server_initializes_from_clean_runtime() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def scenario() -> None:
        server = Path(BACKEND_DIR) / ".pytest-mcp-fixture-server.py"
        server.write_text(_FIXTURE_SERVER, encoding="utf-8")
        with open(os.devnull, "w", encoding="utf-8") as sink:  # server stderr sink
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(server)],
                env=_scoped_env(),
            )
            async with stdio_client(params, errlog=sink) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await asyncio.wait_for(session.initialize(), timeout=15)
                    assert init.serverInfo.name == "mai-p09-fixture"
                    assert init.protocolVersion, "no protocol version negotiated"

                    tools = await asyncio.wait_for(session.list_tools(), timeout=15)
                    assert [tool.name for tool in tools.tools] == ["echo"]
                    schema = tools.tools[0].inputSchema
                    assert schema["type"] == "object"
                    assert schema["properties"]["text"]["type"] == "string"

                    result = await asyncio.wait_for(
                        session.call_tool("echo", {"text": "mai-p09"}), timeout=15
                    )
                    assert not result.isError
                    assert result.content[0].text == "mai-p09"
        server.unlink()

    _run(scenario())


def test_incompatible_mcp_server_fails_explicitly() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.shared.exceptions import McpError

    async def assert_failure(script: str) -> None:
        server = Path(BACKEND_DIR) / ".pytest-mcp-incompatible-server.py"
        server.write_text(script, encoding="utf-8")
        with open(os.devnull, "w", encoding="utf-8") as sink:  # server stderr sink
            params = StdioServerParameters(
                command=sys.executable, args=[str(server)], env=_scoped_env()
            )
            try:
                async with stdio_client(params, errlog=sink) as (read, write):
                    async with ClientSession(read, write) as session:
                        await asyncio.wait_for(session.initialize(), timeout=15)
            except BaseException as exc:  # noqa: BLE001 — the SDK surfaces anyio task groups
                assert not isinstance(exc, asyncio.CancelledError)
                leaves = _exception_leaves(exc)
                assert any(isinstance(leaf, McpError) for leaf in leaves), (
                    f"expected explicit McpError boundary, got {[type(leaf).__name__ for leaf in leaves]}"
                )
                assert any("closed" in str(leaf).lower() for leaf in leaves), (
                    f"unexpected failure reason: {[str(leaf) for leaf in leaves]}"
                )
            else:  # pragma: no cover - guard
                raise AssertionError(
                    f"incompatible server succeeded silently: {script!r}"
                )
        server.unlink()

    _run(assert_failure(_GARBAGE_SERVER))
    _run(assert_failure(_EXITING_SERVER))
