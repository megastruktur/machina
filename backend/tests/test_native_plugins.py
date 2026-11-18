"""Focused tests for the native plugin runtime (card ``09-native-plugin``).

Contracts: contracts.md **C03 rules 1/2/6/7** — the frozen descriptor
vocabulary (registry owner), credential *references* in the context with
values resolved only at effect time, and native plugins as trusted
installed code loaded through importlib.metadata entry points (the
frozen surface from ``test_plugin_dependencies.py``) — plus C06 rule 4
(tool exposure follows the dispatcher boundary).

Boundary discipline: plugins are loaded from a **real installed
distribution** (module files plus a real ``.dist-info`` with
``entry_points.txt`` on ``sys.path`` — exactly what an installed plugin
looks like to ``importlib.metadata``, no pip involved, mirroring the
09-dependencies no-installer rule). Policy-mediated execution runs the
**real composed dispatcher** (real ``EnvResolver`` over a real 0600
``.env`` file; the lazily-connecting ``ApprovalStore`` wiring proves the
type boundary without a database because a ``read`` effect never reaches
the approval seam). The fixture handler echoes its credential input so
the dispatcher's redaction is a pinned behavior, not a hope, and appends
to a counter log so zero-unmediated-execution is observable: denied or
disabled dispatches must leave the log absent/empty.

The manifest content is declarative metadata; rejection details are
swept for content sentinels (value-free discipline, the registry's
rule).
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mai.agents.tools import CapabilityToolBridge
from mai.capabilities.dispatch import (
    REDACTED,
    CapabilityCall,
    CapabilityDispatcher,
    DispatchOutcome,
    ExecutorResult,
)
from mai.capabilities.internal_api import assert_agent_ready_descriptor
from mai.credentials.env import EnvResolver
from mai.plugins.builtin_onepassword import (
    CAPABILITIES as ONEPASSWORD_CAPABILITIES,
)
from mai.plugins.native import (
    CREDENTIAL_DECLARATION,
    ENTRY_POINT_GROUP,
    NativePluginError,
    NativePluginErrorCode,
    NativePluginManifest,
    NativePluginRuntime,
    PluginRejectionCode,
)
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import ExecutionContext, Policy, PolicyMode

import test_onepassword as _op_fixtures

BACKEND_DIR = Path(__file__).resolve().parents[1]
SOURCE = BACKEND_DIR / "src" / "mai" / "plugins" / "native.py"

IDENTIFIER = "fxt.compile"
SECRET_NAME = "FXT_TOKEN"
SECRET_VALUE = "s3cr3t-token-value"
SERVER_ID = uuid.uuid4()
RUN_ID = uuid.uuid4()

#: The frozen entry-point contract other plugin authors target (AC1).
ENTRY_POINTS_TEXT = f"[{ENTRY_POINT_GROUP}]\n"

#: Descriptor document the fixture plugins declare (frozen C03 rule 1
#: vocabulary; ``read`` effect keeps the proof off the approval seam).
CAPABILITY_DOC: dict[str, Any] = {
    "identifier": IDENTIFIER,
    "version": "1.0.0",
    "config_schema": None,
    "input_schema": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    },
    "output_schema": {"type": "object"},
    "execution_environments": ["controller"],
    "effect_class": "read",
    "resource_scope": [],
    "required_grants": [IDENTIFIER],
}

_HANDLER_IMPORTS = (
    "import json\n"
    "from pathlib import Path\n"
    "from mai.capabilities.dispatch import ExecutorResult\n"
)

_GOOD_HANDLER = (
    "async def execute(call):\n"
    "    token = call.credentials.get(\"FXT_TOKEN\")\n"
    "    value = token.value if token is not None else \"\"\n"
    "    with _LOG.open(\"a\", encoding=\"utf-8\") as handle:\n"
    "        handle.write(json.dumps({"
    "\"op_id\": str(call.op_id), "
    "\"principal\": call.context.principal, "
    "\"credentials\": sorted(call.credentials)"
    "}) + \"\\n\")\n"
    "    return ExecutorResult(\n"
    "        ok=True,\n"
    "        output=\"compiled name=%s token_len=%d token=%s\"\n"
    "        % (call.payload.get(\"name\"), len(value), value),\n"
    "    )\n"
)

_SYNC_HANDLER = _GOOD_HANDLER.replace("async def execute(", "def execute(")


def _plugin_module(
    *,
    log: Path,
    capability: object = CAPABILITY_DOC,
    credential_references: str = (
        f"{CREDENTIAL_DECLARATION} = [\"{SECRET_NAME}\"]\n"
    ),
    handler: str = _GOOD_HANDLER,
    prologue: str = "",
) -> str:
    """One fixture plugin module; ``_LOG`` is baked as an absolute path."""
    return (
        _HANDLER_IMPORTS
        + f"_LOG = Path({str(log)!r})\n"
        + prologue
        + f"CAPABILITY = {capability!r}\n"
        + credential_references
        + handler
    )


def _install_plugins(
    root: Path,
    monkeypatch: Any,
    *plugins: dict[str, str],
    dist_name: str = "mai-fxt-plugins",
    version: str = "1.0.0",
) -> Path:
    """Write a real installed distribution and put it on ``sys.path``.

    Each entry of ``plugins`` needs ``module`` (file stem) and ``src``;
    optional ``ep_name``/``ep_value`` override the entry point line.
    This is the exact on-disk shape ``importlib.metadata`` discovers for
    an installed plugin distribution — no installer involved. Fixture
    module names are purged from ``sys.modules`` first so every test's
    ``discover()`` imports the module it installed (fresh-process
    semantics), never a same-named module cached by an earlier test.
    """
    root.mkdir(parents=True, exist_ok=True)
    lines = [f"[{ENTRY_POINT_GROUP}]"]
    for plugin in plugins:
        module: str = plugin["module"]
        sys.modules.pop(module, None)
        (root / f"{module}.py").write_text(plugin["src"], encoding="utf-8")
        lines.append(f"{plugin.get('ep_name', module)} = "
                     f"{plugin.get('ep_value', module)}")
    dist_info = root / f"{dist_name}-{version}.dist-info"
    dist_info.mkdir(exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {version}\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    return root


# ---------------------------------------------------------------------------
# persistent test loop (same discipline as tests/test_plugin_dependencies.py:
# no pytest-asyncio in the dev group)
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
# composition helpers (real registry / real dispatcher providers)
# ---------------------------------------------------------------------------


def _context(
    *,
    granted: bool = True,
    credential_references: tuple[str, ...] = (SECRET_NAME,),
) -> ExecutionContext:
    return ExecutionContext(
        principal="fxt-principal",
        server_id=SERVER_ID,
        run_id=RUN_ID,
        parent_scope=None,
        profile_revision="prof-1",
        prompt_revision="prompt-1",
        credential_references=credential_references,
        allowed_capabilities=(IDENTIFIER,) if granted else (),
        mode=PolicyMode.DELEGATED,
    )


def _unused_approvals() -> ApprovalStore:
    """Real ``ApprovalStore`` over a lazily-connecting engine.

    The ``read`` fixture effect never reaches the approval seam, so no
    connection is ever opened; construction still proves the dispatcher's
    wiring type boundary (test_dispatch.py pattern).
    """
    _, sessions = _database_sessions()
    return ApprovalStore(sessions)


def _database_sessions() -> tuple[Any, Any]:
    from mai.database import create_app_engine

    return create_app_engine("postgresql://unused:unused@127.0.0.1:1/unused")


def _env_file(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.env"
    path.write_text(f"{SECRET_NAME}={SECRET_VALUE}\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _compose(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    disabled: tuple[str, ...] = (),
) -> tuple[CapabilityRegistry, NativePluginRuntime, CapabilityDispatcher]:
    """The real composition: native runtime wired into the dispatcher."""
    registry = CapabilityRegistry()
    runtime = NativePluginRuntime(registry=registry, disabled=disabled)
    runtime.discover()
    grants = {IDENTIFIER: {SERVER_ID}}
    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=_unused_approvals(),
        grants=lambda: grants,
        policy=lambda: Policy(revision=1),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(_env_file(tmp_path)),
        executor=runtime.executor,
    )
    return registry, runtime, dispatcher


def _sweep(value: object, *forbidden: str) -> None:
    text = json.dumps(value, default=repr)
    for needle in forbidden:
        assert needle not in text, f"leaked {needle!r} in receipt"


# ---------------------------------------------------------------------------
# AC1: the frozen entry-point/manifest contract loads installed plugins
# ---------------------------------------------------------------------------


class TestDiscoveryAndRegistration:
    def test_installed_plugin_discovered_and_registered(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        log = tmp_path / "good.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            {"module": "mai_fx_good", "src": _plugin_module(log=log)},
        )
        registry = CapabilityRegistry()
        runtime = NativePluginRuntime(registry=registry)
        runtime.discover()

        assert len(runtime.plugins) == 1
        plugin = runtime.plugins[0]
        assert plugin.plugin == "mai_fx_good"
        assert plugin.descriptor_name == IDENTIFIER
        assert plugin.version == "1.0.0"
        assert plugin.effect_class == "read"
        assert plugin.credential_references == (SECRET_NAME,)
        assert callable(plugin.handler)

        descriptor = registry.get(IDENTIFIER, "1.0.0")
        assert descriptor is not None
        assert descriptor.to_json() == CAPABILITY_DOC
        assert runtime.credential_references == (SECRET_NAME,)
        assert runtime.rejections == ()
        assert runtime.skipped_disabled == ()

    def test_manifest_envelope_is_frozen(self) -> None:
        """The envelope vocabulary is exactly {capability, capabilities,
        references}; anything else is the explicit compatibility failure."""
        schema = NativePluginManifest.model_json_schema()
        assert sorted(schema["properties"]) == [
            "capabilities",
            "capability",
            "credential_references",
        ]
        assert schema["additionalProperties"] is False

        valid = {"capability": CAPABILITY_DOC, "credential_references": []}
        manifest = NativePluginManifest.model_validate(valid)
        assert manifest.capability == CAPABILITY_DOC
        # The singular declaration normalizes into the one-element
        # plural tuple the runtime consumes.
        assert manifest.capabilities == (CAPABILITY_DOC,)
        plural = NativePluginManifest.model_validate(
            {
                "capabilities": [CAPABILITY_DOC, CAPABILITY_DOC],
                "credential_references": [],
            }
        )
        assert plural.capability is None
        assert plural.capabilities == (CAPABILITY_DOC, CAPABILITY_DOC)
        try:
            NativePluginManifest.model_validate({**valid, "handler": "os.system"})
        except ValidationError:
            pass
        else:  # pragma: no cover
            raise AssertionError("envelope accepted an undeclared field")

    def test_inspect_cli_lists_plugin_from_fresh_process(
        self, tmp_path: Path
    ) -> None:
        """The minimal actual entrypoint: real OS process, real discovery."""
        root = tmp_path / "dist"
        root.mkdir()
        (root / "mai_fx_cli.py").write_text(
            _plugin_module(log=tmp_path / "cli.log"), encoding="utf-8"
        )
        dist_info = root / "mai-fxt-cli-1.0.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: mai-fxt-cli\nVersion: 1.0.0\n",
            encoding="utf-8",
        )
        (dist_info / "entry_points.txt").write_text(
            f"[{ENTRY_POINT_GROUP}]\nmai_fx_cli = mai_fx_cli\n", encoding="utf-8"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(root), env.get("PYTHONPATH", "")]
        ).removesuffix(os.pathsep)
        proc = subprocess.run(
            [sys.executable, "-m", "mai.plugins.native", "inspect", "--no-builtins"],
            capture_output=True,
            text=True,
            env=env,
            cwd=BACKEND_DIR,
            check=False,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["group"] == ENTRY_POINT_GROUP
        assert payload["plugins"] == [
            {
                "plugin": "mai_fx_cli",
                "identifier": IDENTIFIER,
                "descriptor_names": [IDENTIFIER],
                "version": "1.0.0",
                "effect_class": "read",
                "credential_references": [SECRET_NAME],
            }
        ]
        assert payload["rejections"] == []
        assert payload["skipped_disabled"] == []

    def test_inspect_cli_reports_rejection_from_fresh_process(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "dist"
        root.mkdir()
        # No CAPABILITY declaration: the structured rejection path.
        (root / "mai_fx_broken.py").write_text(
            _HANDLER_IMPORTS + "_LOG = Path('.')\n" + _GOOD_HANDLER,
            encoding="utf-8",
        )
        dist_info = root / "mai-fxt-broken-1.0.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: mai-fxt-broken\nVersion: 1.0.0\n",
            encoding="utf-8",
        )
        (dist_info / "entry_points.txt").write_text(
            f"[{ENTRY_POINT_GROUP}]\nmai_fx_broken = mai_fx_broken\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root)
        proc = subprocess.run(
            [sys.executable, "-m", "mai.plugins.native", "inspect", "--no-builtins"],
            capture_output=True,
            text=True,
            env=env,
            cwd=BACKEND_DIR,
            check=False,
            timeout=120,
        )
        assert proc.returncode == 1
        payload = json.loads(proc.stdout)
        assert payload["plugins"] == []
        assert payload["rejections"][0]["code"] == "capability_declaration_missing"


# ---------------------------------------------------------------------------
# AC2: disabled / invalid plugins cannot register enabled tools
# ---------------------------------------------------------------------------


class TestDisable:
    def test_disabled_plugin_never_imports_and_never_registers(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        log = tmp_path / "disabled.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            {
                "module": "mai_fx_off",
                "src": _plugin_module(
                    log=log,
                    prologue="_LOG.write_text('imported')\n",
                ),
            },
        )
        registry = CapabilityRegistry()
        runtime = NativePluginRuntime(registry=registry, disabled={"mai_fx_off"})
        runtime.discover()

        assert runtime.plugins == ()
        assert runtime.rejections == ()
        assert runtime.skipped_disabled == ("mai_fx_off",)
        assert len(registry) == 0
        # Load-time disable skips the import: the plugin's code never ran.
        assert not log.exists()

    def test_unknown_disabled_name_is_tolerated(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        log = tmp_path / "good.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            {"module": "mai_fx_good", "src": _plugin_module(log=log)},
        )
        registry = CapabilityRegistry()
        runtime = NativePluginRuntime(registry=registry, disabled={"not-installed"})
        runtime.discover()
        assert len(registry) == 1
        assert runtime.skipped_disabled == ()
        assert "not-installed" not in {p.plugin for p in runtime.plugins}


class TestInvalidPlugins:
    def test_invalid_plugins_rejected_individually_and_isolated(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        log = tmp_path / "good.log"
        broken_log = tmp_path / "broken.log"
        cases = [
            # (module name, expected rejection code, module source)
            (
                "mai_fx_nodecl",
                "capability_declaration_missing",
                _HANDLER_IMPORTS + f"_LOG = Path({str(broken_log)!r})\n" + _GOOD_HANDLER,
            ),
            (
                "mai_fx_notmap",
                "manifest_invalid",
                _plugin_module(log=broken_log, capability=["not", "a", "mapping"]),
            ),
            (
                "mai_fx_badver",
                "invalid_version",
                _plugin_module(
                    log=broken_log,
                    capability={**CAPABILITY_DOC, "version": "1.0"},
                ),
            ),
            (
                "mai_fx_smuggler",
                "unknown_field",
                _plugin_module(
                    log=broken_log,
                    capability={**CAPABILITY_DOC, "handler": "os.system"},
                ),
            ),
            (
                "mai_fx_badref",
                "manifest_invalid",
                _plugin_module(
                    log=broken_log,
                    credential_references=f"{CREDENTIAL_DECLARATION} = "
                    "[\"fxt_lowercase\"]\n",
                ),
            ),
            (
                "mai_fx_refnotlist",
                "manifest_invalid",
                _plugin_module(
                    log=broken_log,
                    credential_references=f"{CREDENTIAL_DECLARATION} = "
                    f"\"{SECRET_NAME}\"\n",
                ),
            ),
            (
                "mai_fx_refdup",
                "manifest_invalid",
                _plugin_module(
                    log=broken_log,
                    credential_references=f"{CREDENTIAL_DECLARATION} = "
                    f"[\"{SECRET_NAME}\", \"{SECRET_NAME}\"]\n",
                ),
            ),
            (
                "mai_fx_nohandler",
                "capability_handler_missing",
                _plugin_module(log=broken_log, handler=""),
            ),
            (
                "mai_fx_notcallable",
                "capability_handler_missing",
                _plugin_module(log=broken_log, handler="execute = 42\n"),
            ),
            (
                "mai_fx_boom",
                "import_failed",
                "raise RuntimeError(\"boom with secret payload details\")\n"
                + _plugin_module(log=broken_log),
            ),
            (
                "mai_fx_attr_ep",
                "plugin_module_invalid",
                _plugin_module(log=broken_log),
            ),
        ]
        plugins = []
        for module, _code, src in cases:
            entry = {"module": module, "src": src}
            if module == "mai_fx_attr_ep":
                entry["ep_value"] = "mai_fx_attr_ep:SOMETHING"
                src += "\nSOMETHING = {'not': 'a module'}\n"
                entry["src"] = src
            plugins.append(entry)
        # The good plugin shares the distribution: one bad installation
        # must not take the others down.
        plugins.append(
            {"module": "mai_fx_good", "src": _plugin_module(log=log)}
        )
        _install_plugins(tmp_path, monkeypatch, *plugins)

        registry = CapabilityRegistry()
        runtime = NativePluginRuntime(registry=registry)
        runtime.discover()

        by_name = {r.plugin: r for r in runtime.rejections}
        assert set(by_name) == {case[0] for case in cases}
        for module, code, _src in cases:
            assert by_name[module].code == code, (
                f"{module}: {by_name[module].code} != {code} "
                f"({by_name[module].detail})"
            )
        # Only the good plugin registered and bound; no rejection leaked a
        # registration (atomic per-plugin, AC2).
        assert [p.plugin for p in runtime.plugins] == ["mai_fx_good"]
        assert len(registry) == 1
        assert registry.versions(IDENTIFIER) == ("1.0.0",)
        assert registry.get(IDENTIFIER, "1.0.0") is not None
        assert runtime.credential_references == (SECRET_NAME,)

    def test_import_failure_detail_is_type_name_only(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Trusted-code exceptions never leak their message (C03 rule 7)."""
        _install_plugins(
            tmp_path,
            monkeypatch,
            {
                "module": "mai_fx_boom",
                "src": 'raise RuntimeError("secret boom payload")\n',
            },
        )
        runtime = NativePluginRuntime(registry=CapabilityRegistry())
        runtime.discover()
        (rejection,) = runtime.rejections
        assert rejection.code == PluginRejectionCode.IMPORT_FAILED.value
        assert rejection.detail == (
            "mai_fx_boom: import raised RuntimeError"
        )
        assert "secret" not in rejection.detail

    def test_identifier_conflict_rejects_second_plugin(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        first_log = tmp_path / "first.log"
        second_log = tmp_path / "second.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            {"module": "mai_fx_first", "src": _plugin_module(log=first_log)},
            {
                "module": "mai_fx_second",
                "src": _plugin_module(
                    log=second_log,
                    capability={**CAPABILITY_DOC, "version": "2.0.0"},
                ),
            },
        )
        registry = CapabilityRegistry()
        runtime = NativePluginRuntime(registry=registry)
        runtime.discover()
        assert [p.plugin for p in runtime.plugins] == ["mai_fx_first"]
        (rejection,) = runtime.rejections
        assert rejection.plugin == "mai_fx_second"
        assert rejection.code == PluginRejectionCode.IDENTIFIER_CONFLICT.value
        # The first owner stays bound; no shadowing, no duplicate row.
        assert len(registry) == 1
        assert registry.versions(IDENTIFIER) == ("1.0.0",)

    def test_duplicate_plugin_name_follows_platform_precedence(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Two distributions declaring one plugin name: importlib.metadata
        exposes exactly one (first on ``sys.path``, module-shadowing
        precedence). The runtime loads what the platform exposes — never
        a second selection rule."""
        from importlib.metadata import entry_points

        _install_plugins(
            tmp_path,
            monkeypatch,
            {
                "module": "mai_fx_one",
                "src": _plugin_module(
                    log=tmp_path / "one.log",
                    capability={**CAPABILITY_DOC, "identifier": "fxt.one"},
                ),
                "ep_name": "fxt.dup",
            },
            dist_name="mai-fxt-one",
        )
        two = tmp_path / "two"
        _install_plugins(
            two,
            monkeypatch,
            {
                "module": "mai_fx_two",
                "src": _plugin_module(
                    log=tmp_path / "two.log",
                    capability={**CAPABILITY_DOC, "identifier": "fxt.two"},
                ),
                "ep_name": "fxt.dup",
            },
            dist_name="mai-fxt-two",
        )
        exposed = {ep.name: ep.value for ep in entry_points(group=ENTRY_POINT_GROUP)}
        runtime = NativePluginRuntime(registry=CapabilityRegistry())
        runtime.discover()
        # Exactly the platform's selection, nothing more.
        assert exposed["fxt.dup"] in {"mai_fx_one", "mai_fx_two"}
        assert len(runtime.plugins) == 1
        assert runtime.plugins[0].plugin == "fxt.dup"
        assert runtime.plugins[0].handler.__module__ == exposed["fxt.dup"]
        assert runtime.rejections == ()
        assert {p.descriptor_name for p in runtime.plugins} == (
            {"fxt.one"} if exposed["fxt.dup"] == "mai_fx_one" else {"fxt.two"}
        )

    def test_invalid_plugin_name_never_imports(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        log = tmp_path / "sideeffect.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            {
                "module": "mai_fx_named",
                "src": _plugin_module(
                    log=log, prologue="_LOG.write_text('imported')\n"
                ),
                "ep_name": "Bad Name!",
            },
        )
        registry = CapabilityRegistry()
        runtime = NativePluginRuntime(registry=registry)
        runtime.discover()
        (rejection,) = runtime.rejections
        assert rejection.code == PluginRejectionCode.INVALID_PLUGIN_NAME.value
        assert runtime.plugins == ()
        assert len(registry) == 0
        assert not log.exists()

    def test_rejection_details_never_quote_manifest_content(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Value-free discipline: schema/manifest content stays out of
        rejection text (a schema may describe credential-shaped config)."""
        sentinel = "SENTINEL-CREDENTIAL-SHAPE-9f3a"
        # The document content carries the sentinel; the rejection
        # (invalid version, checked before schema parsing) must not.
        leaking_capability = {
            **CAPABILITY_DOC,
            "version": "not.a.version",
            "config_schema": {"type": "object", "value": sentinel},
        }
        _install_plugins(
            tmp_path,
            monkeypatch,
            {
                "module": "mai_fx_leak1",
                "src": _plugin_module(
                    log=tmp_path / "l1.log", capability=leaking_capability
                ),
            },
            {
                "module": "mai_fx_leak2",
                "src": _plugin_module(
                    log=tmp_path / "l2.log",
                    credential_references=f"{CREDENTIAL_DECLARATION} = "
                    f"[\"{sentinel}\"]\n",
                ),
            },
        )
        runtime = NativePluginRuntime(registry=CapabilityRegistry())
        runtime.discover()
        assert [r.code for r in runtime.rejections] == [
            "invalid_version",
            "manifest_invalid",
        ]
        for rejection in runtime.rejections:
            blob = f"{rejection} {rejection.detail} {rejection.code}"
            assert sentinel not in blob

    def test_composition_wiring_is_constructor_checked(self) -> None:
        for kwargs in (
            {"registry": "not-a-registry"},
            {"registry": CapabilityRegistry(), "disabled": "mai_fx"},
            {"registry": CapabilityRegistry(), "disabled": [42]},
            {"registry": CapabilityRegistry(), "disabled": ("a", "a")},
        ):
            try:
                NativePluginRuntime(**kwargs)
            except NativePluginError as error:
                assert error.code == NativePluginErrorCode.INVALID_WIRING
            else:  # pragma: no cover
                raise AssertionError(f"wiring accepted: {kwargs}")

    def test_accessors_fail_closed_before_discover(self) -> None:
        runtime = NativePluginRuntime(registry=CapabilityRegistry())
        for probe in (
            lambda: runtime.plugins,
            lambda: runtime.rejections,
            lambda: runtime.skipped_disabled,
            lambda: runtime.credential_references,
            lambda: runtime.executor,
        ):
            try:
                probe()
            except NativePluginError as error:
                assert error.code == NativePluginErrorCode.NOT_DISCOVERED
            else:  # pragma: no cover
                raise AssertionError("undiscovered runtime answered")
        runtime.discover()
        try:
            runtime.discover()
        except NativePluginError as error:
            assert error.code == NativePluginErrorCode.ALREADY_DISCOVERED
        else:  # pragma: no cover
            raise AssertionError("double discover accepted")

    def test_executor_refuses_unknown_capability_structurally(self) -> None:
        runtime = NativePluginRuntime(registry=CapabilityRegistry())
        runtime.discover()
        call = CapabilityCall(
            descriptor=parse_descriptor(CAPABILITY_DOC),
            payload={},
            context=_context(),
            credentials={},
            op_id=uuid.uuid4(),
        )
        result = _run(runtime.executor(call))
        assert isinstance(result, ExecutorResult)
        assert result.ok is False
        assert IDENTIFIER in result.output
        assert "no enabled native plugin" in result.output


# ---------------------------------------------------------------------------
# no hot install: the import surface stays installer-free (AC2, source pin;
# runtime absence of installers is pinned by test_plugin_dependencies.py)
# ---------------------------------------------------------------------------


class TestNoHotInstall:
    def test_import_surface_stays_installer_and_network_free(self) -> None:
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert node.module is not None
                roots.add(node.module.split(".")[0])
        allowed = {
            "__future__", "argparse", "collections", "dataclasses", "enum",
            "importlib", "json", "mai", "pydantic", "re", "types", "typing",
        }
        assert roots <= allowed, f"unexpected imports in native.py: {roots - allowed}"
        forbidden = {"pip", "uv", "pipx", "poetry", "subprocess", "socket",
                     "http", "urllib", "requests"}
        assert roots.isdisjoint(forbidden)


# ---------------------------------------------------------------------------
# policy-mediated execution: zero unmediated execution (real dispatcher)
# ---------------------------------------------------------------------------


class TestPolicyMediatedExecution:
    def test_allowed_dispatch_executes_plugin_with_redacted_credentials(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        log = tmp_path / "good.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            {"module": "mai_fx_good", "src": _plugin_module(log=log)},
        )
        _registry, _runtime, dispatcher = _compose(tmp_path, monkeypatch)
        outcome: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=_context(),
                capability_id=IDENTIFIER,
                payload={"name": "kernel"},
            )
        )
        receipt = outcome.to_json()
        assert outcome.allowed is True
        assert outcome.reason == "allowed"
        assert outcome.effect == "executed"
        assert outcome.executor_ok is True
        assert outcome.credential_references == (SECRET_NAME,)
        assert outcome.output is not None
        # The handler received the resolved SecretValue (C03 rule 6: at
        # effect time) — token_len proves real resolution — and the
        # dispatcher redacted the echoed value (defense-in-depth).
        assert f"token_len={len(SECRET_VALUE)}" in outcome.output.text
        assert REDACTED in outcome.output.text
        _sweep(receipt, SECRET_VALUE)
        assert SECRET_VALUE not in outcome.output.text

        # Exactly one handler invocation, through the dispatcher only.
        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["principal"] == "fxt-principal"
        assert record["credentials"] == [SECRET_NAME]
        assert record["op_id"] == str(outcome.op_id)

    def test_denied_dispatch_never_reaches_handler(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        log = tmp_path / "good.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            {"module": "mai_fx_good", "src": _plugin_module(log=log)},
        )
        _registry, _runtime, dispatcher = _compose(tmp_path, monkeypatch)
        outcome: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=_context(granted=False),
                capability_id=IDENTIFIER,
                payload={"name": "kernel"},
            )
        )
        assert outcome.allowed is False
        assert outcome.reason == "capability_not_granted"
        assert outcome.effect == "not_attempted"
        assert outcome.output is None
        assert not log.exists()

    def test_disabled_plugin_denies_as_unregistered(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        log = tmp_path / "good.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            {"module": "mai_fx_good", "src": _plugin_module(log=log)},
        )
        registry = CapabilityRegistry()
        runtime = NativePluginRuntime(registry=registry, disabled=("mai_fx_good",))
        runtime.discover()
        dispatcher = CapabilityDispatcher(
            registry=registry,
            approvals=_unused_approvals(),
            grants=lambda: {IDENTIFIER: {SERVER_ID}},
            policy=lambda: Policy(revision=1),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(_env_file(tmp_path)),
            executor=runtime.executor,
        )
        outcome: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=_context(),
                capability_id=IDENTIFIER,
                payload={"name": "kernel"},
            )
        )
        assert outcome.allowed is False
        assert outcome.reason == "capability_not_registered"
        assert outcome.effect == "not_attempted"
        assert not log.exists()

    def test_sync_handler_supported_through_dispatcher(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        log = tmp_path / "sync.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            {
                "module": "mai_fx_sync",
                "src": _plugin_module(log=log, handler=_SYNC_HANDLER),
            },
        )
        registry, runtime, dispatcher = _compose(tmp_path, monkeypatch)
        assert registry.get(IDENTIFIER, "1.0.0") is not None
        outcome: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=_context(),
                capability_id=IDENTIFIER,
                payload={"name": "sync"},
            )
        )
        assert outcome.allowed is True
        assert outcome.effect == "executed"
        assert outcome.output is not None
        assert "name=sync" in outcome.output.text
        assert log.exists()


# ---------------------------------------------------------------------------
# C06 rule 4: agent/tool exposure follows grant, not package presence
# ---------------------------------------------------------------------------


class TestExposureFollowsGrant:
    def _bridge_tools(
        self,
        tmp_path: Path,
        monkeypatch: Any,
        *,
        granted: bool,
        disabled: tuple[str, ...] = (),
    ) -> tuple[tuple[str, ...], int]:
        log = tmp_path / "good.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            {"module": "mai_fx_good", "src": _plugin_module(log=log)},
        )
        registry, _runtime, dispatcher = _compose(
            tmp_path, monkeypatch, disabled=disabled
        )
        bridge = CapabilityToolBridge(
            registry=registry,
            dispatcher=dispatcher,
            context=_context(granted=granted),
        )
        return bridge.enabled_capabilities, len(registry)

    def test_granted_and_installed_exposes_tool(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        enabled, registered = self._bridge_tools(
            tmp_path, monkeypatch, granted=True
        )
        assert enabled == (IDENTIFIER,)
        assert registered == 1

    def test_present_but_not_granted_exposes_nothing(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        enabled, registered = self._bridge_tools(
            tmp_path, monkeypatch, granted=False
        )
        assert enabled == ()
        assert registered == 1  # package presence alone is not exposure

    def test_granted_but_disabled_exposes_nothing(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        enabled, registered = self._bridge_tools(
            tmp_path, monkeypatch, granted=True, disabled=("mai_fx_good",)
        )
        assert enabled == ()
        assert registered == 0

    def test_granted_but_absent_exposes_nothing(self) -> None:
        registry = CapabilityRegistry()
        runtime = NativePluginRuntime(registry=registry)
        runtime.discover()
        _, _sessions = _database_sessions()
        dispatcher = CapabilityDispatcher(
            registry=registry,
            approvals=ApprovalStore(_sessions),
            grants=lambda: {IDENTIFIER: {SERVER_ID}},
            policy=lambda: Policy(revision=1),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(Path("/nonexistent")),
            executor=runtime.executor,
        )
        bridge = CapabilityToolBridge(
            registry=registry,
            dispatcher=dispatcher,
            context=_context(granted=True),
        )
        assert bridge.enabled_capabilities == ()
        assert len(registry) == 0


# ---------------------------------------------------------------------------
# s2: multi-capability native plugins (CAPABILITIES, plan contract C3)
# ---------------------------------------------------------------------------

_MULTI_A: dict[str, Any] = {**CAPABILITY_DOC, "identifier": "fxt.multi.a"}
_MULTI_B: dict[str, Any] = {**CAPABILITY_DOC, "identifier": "fxt.multi.b"}

# Agent-ready variant: schema-level description (>= 30 chars) plus a
# >= 12-char description on every property (assert_agent_ready_descriptor).
_AGENT_READY_A: dict[str, Any] = {
    **CAPABILITY_DOC,
    "identifier": "fxt.ready.a",
    "input_schema": {
        "type": "object",
        "description": "Compile the given name through the fixture handler.",
        "properties": {"name": {"type": "string", "description": "The name to compile."}},
        "required": ["name"],
        "additionalProperties": False,
    },
}

_AGENT_READY_B: dict[str, Any] = {
    **_AGENT_READY_A,
    "identifier": "fxt.ready.b",
}


def _multi_plugin_module(
    log: Path,
    *docs: dict[str, Any],
    credential_references: str = (
        f"{CREDENTIAL_DECLARATION} = [\"{SECRET_NAME}\"]\n"
    ),
    extra_declarations: str = "",
    handler: str = _GOOD_HANDLER,
) -> str:
    """One fixture plugin module declaring the plural CAPABILITIES."""
    if docs:
        docs_source = ", ".join(repr(doc) for doc in docs)
        declaration = f"CAPABILITIES = ({docs_source},)\n"
    else:
        declaration = "CAPABILITIES = ()\n"
    return (
        _HANDLER_IMPORTS
        + f"_LOG = Path({str(log)!r})\n"
        + declaration
        + extra_declarations
        + credential_references
        + handler
    )


def _executor_call(identifier: str, document: dict[str, Any]) -> CapabilityCall:
    return CapabilityCall(
        descriptor=parse_descriptor(document),
        payload={"name": "kernel"},
        context=_context(),
        credentials={},
        op_id=uuid.uuid4(),
    )


class TestMultiCapability:
    def test_multi_capability_module_loads_all_descriptors(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        log = tmp_path / "multi.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            {
                "module": "mai_fx_multi",
                "src": _multi_plugin_module(log, _AGENT_READY_A, _AGENT_READY_B),
            },
        )
        registry = CapabilityRegistry()
        runtime = NativePluginRuntime(registry=registry)
        runtime.discover()

        assert len(runtime.plugins) == 1
        plugin = runtime.plugins[0]
        assert plugin.descriptor_names == ("fxt.ready.a", "fxt.ready.b")
        # Backward-compatible primary-descriptor property.
        assert plugin.descriptor_name == "fxt.ready.a"
        assert plugin.version == "1.0.0"
        assert plugin.effect_class == "read"
        assert plugin.credential_references == (SECRET_NAME,)
        assert len(registry) == 2
        assert runtime.rejections == ()
        assert runtime.credential_references == (SECRET_NAME,)

        # Every descriptor routes to the plugin's one execute handler.
        for identifier, document in (
            ("fxt.ready.a", _AGENT_READY_A),
            ("fxt.ready.b", _AGENT_READY_B),
        ):
            result = _run(runtime.executor(_executor_call(identifier, document)))
            assert isinstance(result, ExecutorResult)
            assert result.ok is True

    def test_multi_capability_requires_identical_config_schema(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        log = tmp_path / "divergent.log"
        divergent = (
            {**CAPABILITY_DOC, "identifier": "fxt.multi.a",
             "config_schema": {"type": "object"}},
            {**CAPABILITY_DOC, "identifier": "fxt.multi.b",
             "config_schema": {"type": "object", "additionalProperties": False}},
        )
        _install_plugins(
            tmp_path,
            monkeypatch,
            {"module": "mai_fx_multi", "src": _multi_plugin_module(log, *divergent)},
        )
        registry = CapabilityRegistry()
        runtime = NativePluginRuntime(registry=registry)
        runtime.discover()
        (rejection,) = runtime.rejections
        assert rejection.code == PluginRejectionCode.MANIFEST_INVALID.value
        assert runtime.plugins == ()
        assert len(registry) == 0

    def test_bool_is_never_an_int_in_config_schema_identity(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        log = tmp_path / "bool.log"
        docs = (
            {**CAPABILITY_DOC, "identifier": "fxt.multi.a",
             "config_schema": True},
            {**CAPABILITY_DOC, "identifier": "fxt.multi.b",
             "config_schema": 1},
        )
        _install_plugins(
            tmp_path,
            monkeypatch,
            {"module": "mai_fx_multi", "src": _multi_plugin_module(log, *docs)},
        )
        runtime = NativePluginRuntime(registry=CapabilityRegistry())
        runtime.discover()
        (rejection,) = runtime.rejections
        assert rejection.code == PluginRejectionCode.MANIFEST_INVALID.value

    def test_multi_capability_rejects_both_declarations(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        log = tmp_path / "both.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            {
                "module": "mai_fx_multi",
                "src": _multi_plugin_module(
                    log,
                    _MULTI_A,
                    extra_declarations=f"CAPABILITY = {CAPABILITY_DOC!r}\n",
                ),
            },
        )
        runtime = NativePluginRuntime(registry=CapabilityRegistry())
        runtime.discover()
        (rejection,) = runtime.rejections
        assert rejection.code == PluginRejectionCode.MANIFEST_INVALID.value
        assert runtime.plugins == ()

    def test_multi_capability_rejects_empty_sequence(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        log = tmp_path / "empty.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            {
                "module": "mai_fx_multi",
                "src": _multi_plugin_module(log),
            },
        )
        runtime = NativePluginRuntime(registry=CapabilityRegistry())
        runtime.discover()
        (rejection,) = runtime.rejections
        assert rejection.code == PluginRejectionCode.MANIFEST_INVALID.value
        assert runtime.plugins == ()

    def test_multi_capability_enforces_agent_ready_descriptions(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Plural declarations are the agent-facing surface (C1): every
        descriptor must pass assert_agent_ready_descriptor; the legacy
        singular declaration keeps its leniency (all other fixtures)."""
        log = tmp_path / "bare.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            # _MULTI_A has no descriptions: rejected under CAPABILITIES.
            {"module": "mai_fx_bare", "src": _multi_plugin_module(log, _MULTI_A)},
        )
        registry = CapabilityRegistry()
        runtime = NativePluginRuntime(registry=registry)
        runtime.discover()
        (rejection,) = runtime.rejections
        assert rejection.code == PluginRejectionCode.NOT_AGENT_READY.value
        assert rejection.plugin == "mai_fx_bare"
        assert runtime.plugins == ()
        # Atomic per plugin: nothing leaked a registration.
        assert len(registry) == 0

        ready_log = tmp_path / "ready.log"
        _install_plugins(
            tmp_path,
            monkeypatch,
            {
                "module": "mai_fx_ready",
                "src": _multi_plugin_module(
                    ready_log,
                    _AGENT_READY_A,
                    credential_references=f"{CREDENTIAL_DECLARATION} = []\n",
                ),
            },
            dist_name="ready-fxt",
        )
        ready_registry = CapabilityRegistry()
        ready_runtime = NativePluginRuntime(registry=ready_registry)
        ready_runtime.discover()
        assert [p.descriptor_names for p in ready_runtime.plugins] == [
            ("fxt.ready.a",)
        ]
        # The bare fixture from the first half is still installed and
        # keeps failing the same readiness gate; the ready plugin loads.
        assert [r.plugin for r in ready_runtime.rejections] == ["mai_fx_bare"]


# ---------------------------------------------------------------------------
# s2: the built-in onepassword plugin (C1/C2, dispatcher-level)
# ---------------------------------------------------------------------------

OP_VAULT = "mai-dev"
OP_TOKEN = "op-token-sentinel-not-a-secret"
_OP_LISTING = [
    {
        "id": "it-a",
        "title": "alpha",
        "category": "LOGIN",
        "vault": OP_VAULT,
        "updated_at": "2026-01-01T00:00:00Z",
    }
]

#: C2 regression material: a value whose JSON serialization escapes it
#: (``"``, ``\``, newline, non-ASCII) so a byte-form distinct from the
#: raw secret reaches ``json.dumps`` output. Raw-substring scrubbing
#: used to miss exactly this form.
ESCAPING_SECRET = 'INQ-GEN-"q7\\Wx2#mK\n€'


def _op_env_file(tmp_path: Path) -> Path:
    path = tmp_path / "op.env"
    path.write_text(f"OP_SERVICE_ACCOUNT_TOKEN={OP_TOKEN}\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _assert_secret_absent(outcome: DispatchOutcome, secret: str) -> None:
    """C2: neither the raw value nor either JSON-escaped form survives.

    ``outcome.output`` is the durable dispatcher answer; ``to_json()``
    is the receipt serialization the audit surface persists.
    """
    assert outcome.output is not None
    for form in (
        secret,
        json.dumps(secret, ensure_ascii=True)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
    ):
        assert form not in outcome.output.text
        _sweep(outcome.to_json(), form)


class TestBuiltinOnepassword:
    """The host distribution's own plugin: opt-in composition (worker
    runtime semantics) exercised through the real dispatcher."""

    def _compose(
        self,
        tmp_path: Path,
        monkeypatch: Any,
        *,
        bin_dir: Path | None = None,
    ) -> tuple[CapabilityRegistry, NativePluginRuntime, CapabilityDispatcher]:
        if bin_dir is not None:
            monkeypatch.setenv(
                "PATH",
                os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]),
            )
        registry = CapabilityRegistry()
        runtime = NativePluginRuntime(registry=registry, include_builtins=True)
        runtime.discover()
        dispatcher = CapabilityDispatcher(
            registry=registry,
            approvals=_unused_approvals(),
            grants=lambda: {
                cid: {SERVER_ID}
                for cid in ("secret.list", "secret.get", "secret.store")
            },
            policy=lambda: Policy(revision=1),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(_op_env_file(tmp_path)),
            executor=runtime.executor,
        )
        return registry, runtime, dispatcher

    def _context(
        self,
        *,
        principal: str = "fxt-principal",
        credential_references: tuple[str, ...] = ("OP_SERVICE_ACCOUNT_TOKEN",),
    ) -> ExecutionContext:
        return ExecutionContext(
            principal=principal,
            server_id=SERVER_ID,
            run_id=RUN_ID,
            parent_scope=None,
            profile_revision="prof-1",
            prompt_revision="prompt-1",
            credential_references=credential_references,
            allowed_capabilities=("secret.list", "secret.get", "secret.store"),
            mode=PolicyMode.DELEGATED,
        )

    def test_builtin_excluded_by_default_and_opted_in(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Default composition (store diagnostics, fixtures) sees no
        host-distribution entry points; opt-in (worker) composes them."""
        default_registry = CapabilityRegistry()
        default_runtime = NativePluginRuntime(registry=default_registry)
        default_runtime.discover()
        assert default_registry.list_capabilities() == ()
        assert default_runtime.plugins == ()
        assert default_runtime.rejections == ()

        registry = CapabilityRegistry()
        runtime = NativePluginRuntime(registry=registry, include_builtins=True)
        runtime.discover()
        assert [p.plugin for p in runtime.plugins] == ["onepassword"]
        plugin = runtime.plugins[0]
        assert plugin.descriptor_names == (
            "secret.list",
            "secret.get",
            "secret.store",
        )
        assert plugin.descriptor_name == "secret.list"
        assert plugin.version == "1.0.0"
        assert plugin.effect_class == "read"
        assert plugin.credential_references == ("OP_SERVICE_ACCOUNT_TOKEN",)
        assert sorted(d.identifier for d in registry.list_capabilities()) == [
            "secret.get",
            "secret.list",
            "secret.store",
        ]
        # Plural declarations are agent-ready (C1): the real plugin passes.
        for document in ONEPASSWORD_CAPABILITIES:
            assert_agent_ready_descriptor(parse_descriptor(document))

    def test_secret_list_through_dispatcher(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        bin_dir = _op_fixtures._install_fake_op(tmp_path, items=_OP_LISTING)
        _registry, _runtime, dispatcher = self._compose(
            tmp_path, monkeypatch, bin_dir=bin_dir
        )
        outcome: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=self._context(),
                capability_id="secret.list",
                payload={"vault": OP_VAULT},
            )
        )
        assert outcome.allowed is True
        assert outcome.effect == "executed"
        assert outcome.executor_ok is True
        assert outcome.output is not None
        document = json.loads(outcome.output.text)
        assert document["status"] == "ok"
        assert document["vault"] == OP_VAULT
        assert [item["title"] for item in document["items"]] == ["alpha"]
        assert document["total"] == 1
        assert document["has_more"] is False
        # The service-account token never echoes into the answer.
        assert OP_TOKEN not in outcome.output.text
        _sweep(outcome.to_json(), OP_TOKEN)

    def test_secret_store_redacts_value_in_outcome(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        bin_dir = _op_fixtures._install_fake_op(tmp_path, items=[])
        _registry, _runtime, dispatcher = self._compose(
            tmp_path, monkeypatch, bin_dir=bin_dir
        )
        outcome: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=self._context(),
                capability_id="secret.store",
                payload={
                    "vault": OP_VAULT,
                    "title": "deploy-key",
                    "field": "password",
                    "value": _op_fixtures.SENTINEL,
                },
            )
        )
        assert outcome.allowed is True
        assert outcome.executor_ok is True
        assert outcome.output is not None
        document = json.loads(outcome.output.text)
        assert document["status"] == "ok"
        assert document["reference"] == f"op://{OP_VAULT}/deploy-key/password"
        # C2 pinned: the store value scrubs out of the durable outcome.
        assert REDACTED in outcome.output.text
        assert _op_fixtures.SENTINEL not in outcome.output.text
        # L3: no digest oracle for human-chosen secrets.
        assert "sha256" not in document
        _sweep(outcome.to_json(), _op_fixtures.SENTINEL, OP_TOKEN)

    def test_secret_get_json_escaped_secret_fully_redacted(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """C2: a value ``json.dumps`` escapes (`", ``\\``, newline,
        non-ASCII) must not survive in its escaped byte form — raw
        substring scrubbing used to miss exactly that copy."""
        bin_dir = _op_fixtures._install_fake_op(
            tmp_path, value=ESCAPING_SECRET
        )
        _registry, _runtime, dispatcher = self._compose(
            tmp_path, monkeypatch, bin_dir=bin_dir
        )
        outcome: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=self._context(),
                capability_id="secret.get",
                payload={
                    "vault": OP_VAULT,
                    "item": "test-item",
                    "field": "api-key",
                },
            )
        )
        assert outcome.allowed is True
        assert outcome.executor_ok is True
        assert outcome.output is not None
        document = json.loads(outcome.output.text)
        assert document["status"] == "ok"
        # The escaped copy in the JSON document scrubs to the marker:
        # the parsed answer carries no readable material.
        assert document["value"] == REDACTED
        _assert_secret_absent(outcome, ESCAPING_SECRET)
        _sweep(outcome.to_json(), OP_TOKEN)

    def test_secret_store_json_escaped_secret_fully_redacted(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """C2: the store surface with an escaping value — the value the
        model supplied never survives the dispatcher output, raw or
        JSON-escaped."""
        bin_dir = _op_fixtures._install_fake_op(tmp_path, items=[])
        _registry, _runtime, dispatcher = self._compose(
            tmp_path, monkeypatch, bin_dir=bin_dir
        )
        outcome: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=self._context(),
                capability_id="secret.store",
                payload={
                    "vault": OP_VAULT,
                    "title": "nasty-key",
                    "field": "password",
                    "value": ESCAPING_SECRET,
                },
            )
        )
        assert outcome.allowed is True
        assert outcome.executor_ok is True
        assert outcome.output is not None
        document = json.loads(outcome.output.text)
        assert document["status"] == "ok"
        assert document["reference"] == f"op://{OP_VAULT}/nasty-key/password"
        assert document["value"] == REDACTED
        # L3: no digest oracle for human-chosen secrets.
        assert "sha256" not in document
        _assert_secret_absent(outcome, ESCAPING_SECRET)
        _sweep(outcome.to_json(), OP_TOKEN)

    def test_secret_get_export_credential_ref_persists_env(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from mai.credentials.env import parse_env_text

        data_root = tmp_path / "data"
        monkeypatch.setenv("MAI_DATA_ROOT", str(data_root))
        bin_dir = _op_fixtures._install_fake_op(tmp_path)
        _registry, _runtime, dispatcher = self._compose(
            tmp_path, monkeypatch, bin_dir=bin_dir
        )
        outcome: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=self._context(),
                capability_id="secret.get",
                payload={
                    "vault": OP_VAULT,
                    "item": "test-item",
                    "field": "api-key",
                    "export_credential_ref": "EXPORTED_TOKEN",
                },
            )
        )
        assert outcome.allowed is True
        assert outcome.executor_ok is True
        assert outcome.output is not None
        document = json.loads(outcome.output.text)
        assert document["exported_credential_ref"] == "EXPORTED_TOKEN"
        assert REDACTED in outcome.output.text
        assert _op_fixtures.SENTINEL not in outcome.output.text
        credentials_path = data_root / "credentials.env"
        assert credentials_path.is_file()
        assert (credentials_path.stat().st_mode & 0o777) == 0o600
        assert parse_env_text(credentials_path.read_text(encoding="utf-8")) == {
            "EXPORTED_TOKEN": _op_fixtures.SENTINEL
        }

        # A malformed reference is a structured refusal, never a write.
        bad: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=self._context(),
                capability_id="secret.get",
                payload={
                    "vault": OP_VAULT,
                    "item": "test-item",
                    "field": "api-key",
                    "export_credential_ref": "bad-ref",
                },
            )
        )
        assert bad.executor_ok is False
        assert bad.output is not None
        assert "invalid_credential_ref" in bad.output.text

    # -- ask_08-secret-export: protected agent-created exports (D6/C6) --

    def _write_operator_refs(self, data_root: Path) -> tuple[str, str, str]:
        """Seed SSH / model / plugin operator credential refs under the
        data root (the operator surfaces an agent export must never
        overwrite)."""
        server_dir = data_root / "servers" / str(uuid.uuid4())
        server_dir.mkdir(parents=True)
        (server_dir / "profile.yaml").write_text(
            'schema_version: 1\n'
            f'server_id: "{uuid.uuid4()}"\n'
            "privilege: none\n"
            "ssh:\n"
            '  credential_ref: "MAI_SSH_KEY_PROD"\n',
            encoding="utf-8",
        )
        connections = data_root / "models" / "connections"
        connections.mkdir(parents=True)
        (connections / "primary.json").write_text(
            json.dumps({"name": "primary", "credential_ref": "MODEL_DB_PASS"}),
            encoding="utf-8",
        )
        plugins_dir = data_root / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "onepassword.yaml").write_text(
            "schema_version: 1\n"
            "plugin: onepassword\n"
            "enabled: true\n"
            "credential_refs:\n"
            "  - PLUGIN_TOKEN\n",
            encoding="utf-8",
        )
        return "MAI_SSH_KEY_PROD", "MODEL_DB_PASS", "PLUGIN_TOKEN"

    def test_export_bootstrap_refs_refused_without_side_effects(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from mai.credentials.env import parse_env_text

        data_root = tmp_path / "data"
        monkeypatch.setenv("MAI_DATA_ROOT", str(data_root))
        bin_dir = _op_fixtures._install_fake_op(tmp_path)
        _registry, _runtime, dispatcher = self._compose(
            tmp_path, monkeypatch, bin_dir=bin_dir
        )
        scenarios = (
            (
                "secret.get",
                {"vault": OP_VAULT, "item": "test-item", "field": "api-key"},
            ),
            (
                "secret.store",
                {"vault": OP_VAULT, "title": "operator-item",
                 "value": _op_fixtures.SENTINEL},
            ),
        )
        for capability, base in scenarios:
            for ref in ("OP_SERVICE_ACCOUNT_TOKEN", "MAI_DATABASE_URL", "PATH"):
                outcome: DispatchOutcome = _run(
                    dispatcher.dispatch(
                        context=self._context(),
                        capability_id=capability,
                        payload={**base, "export_credential_ref": ref},
                    )
                )
                assert outcome.executor_ok is False
                assert outcome.output is not None
                assert "credential_ref_protected" in outcome.output.text
                assert _op_fixtures.SENTINEL not in outcome.output.text
        # Zero side effects: no op invocation, no credentials.env write.
        assert _op_fixtures._invocations(bin_dir) == []
        assert not (data_root / "credentials.env").exists()
        parse_env_text  # noqa: B018 - asserted via read-back in sibling tests

    def test_export_operator_refs_refused(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        data_root = tmp_path / "data"
        monkeypatch.setenv("MAI_DATA_ROOT", str(data_root))
        ssh_ref, model_ref, plugin_ref = self._write_operator_refs(data_root)
        bin_dir = _op_fixtures._install_fake_op(tmp_path)
        _registry, _runtime, dispatcher = self._compose(
            tmp_path, monkeypatch, bin_dir=bin_dir
        )
        for ref in (ssh_ref, model_ref, plugin_ref):
            outcome: DispatchOutcome = _run(
                dispatcher.dispatch(
                    context=self._context(),
                    capability_id="secret.get",
                    payload={
                        "vault": OP_VAULT,
                        "item": "test-item",
                        "field": "api-key",
                        "export_credential_ref": ref,
                    },
                )
            )
            assert outcome.executor_ok is False
            assert outcome.output is not None
            assert "credential_ref_protected" in outcome.output.text
            assert _op_fixtures.SENTINEL not in outcome.output.text
        assert _op_fixtures._invocations(bin_dir) == []
        assert not (data_root / "credentials.env").exists()

    def test_agent_export_namespace_rules(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from mai.credentials.env import parse_env_text

        data_root = tmp_path / "data"
        monkeypatch.setenv("MAI_DATA_ROOT", str(data_root))
        bin_dir = _op_fixtures._install_fake_op(tmp_path)
        _registry, _runtime, dispatcher = self._compose(
            tmp_path, monkeypatch, bin_dir=bin_dir
        )
        server8 = SERVER_ID.hex[:8].upper()
        run8 = RUN_ID.hex[:8].upper()
        good_ref = f"MAI_AGENT_{server8}_{run8}_DB_PASS"

        # Agent caller, valid server/run-owned ref: succeeds and reads back.
        good: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=self._context(principal="agent"),
                capability_id="secret.get",
                payload={
                    "vault": OP_VAULT,
                    "item": "test-item",
                    "field": "api-key",
                    "export_credential_ref": good_ref,
                },
            )
        )
        assert good.executor_ok is True
        assert good.output is not None
        assert json.loads(good.output.text)["exported_credential_ref"] == good_ref
        credentials_path = data_root / "credentials.env"
        assert credentials_path.is_file()
        assert (credentials_path.stat().st_mode & 0o777) == 0o600
        assert parse_env_text(credentials_path.read_text(encoding="utf-8")) == {
            good_ref: _op_fixtures.SENTINEL
        }
        assert EnvResolver(credentials_path).resolve(good_ref).value == (
            _op_fixtures.SENTINEL
        )

        # Unscoped operator ref, another server's and another run's
        # namespace: refused BEFORE any op call or env write.
        refusals = (
            ("OPERATOR_DB_PASS", "credential_ref_scope_mismatch"),
            (
                f"MAI_AGENT_{uuid.uuid4().hex[:8].upper()}_{run8}_DB_PASS",
                "credential_ref_scope_mismatch",
            ),
            (
                f"MAI_AGENT_{server8}_{uuid.uuid4().hex[:8].upper()}_DB_PASS",
                "credential_ref_scope_mismatch",
            ),
            (good_ref, "credential_ref_collision"),
        )
        for ref, code in refusals:
            outcome: DispatchOutcome = _run(
                dispatcher.dispatch(
                    context=self._context(principal="agent"),
                    capability_id="secret.get",
                    payload={
                        "vault": OP_VAULT,
                        "item": "test-item",
                        "field": "api-key",
                        "export_credential_ref": ref,
                    },
                )
            )
            assert outcome.executor_ok is False, ref
            assert outcome.output is not None
            assert code in outcome.output.text, (ref, outcome.output.text)
            assert _op_fixtures.SENTINEL not in outcome.output.text
        assert parse_env_text(credentials_path.read_text(encoding="utf-8")) == {
            good_ref: _op_fixtures.SENTINEL
        }

        # Store-side refusals happen BEFORE `op item create`.
        store_bad: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=self._context(principal="agent"),
                capability_id="secret.store",
                payload={
                    "vault": OP_VAULT,
                    "title": "agent-item",
                    "generate": True,
                    "export_credential_ref": "OPERATOR_DB_PASS",
                },
            )
        )
        assert store_bad.executor_ok is False
        assert store_bad.output is not None
        assert "credential_ref_scope_mismatch" in store_bad.output.text
        assert not any(
            record["argv"][:2] == ["item", "create"]
            for record in _op_fixtures._invocations(bin_dir)
        )

    def test_agent_store_generation_export_persists_and_redacts(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from mai.credentials.env import parse_env_text

        data_root = tmp_path / "data"
        monkeypatch.setenv("MAI_DATA_ROOT", str(data_root))
        bin_dir = _op_fixtures._install_fake_op(tmp_path, items=[])
        _registry, _runtime, dispatcher = self._compose(
            tmp_path, monkeypatch, bin_dir=bin_dir
        )
        good_ref = (
            f"MAI_AGENT_{SERVER_ID.hex[:8].upper()}_"
            f"{RUN_ID.hex[:8].upper()}_DB_PASS"
        )
        outcome: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=self._context(principal="agent"),
                capability_id="secret.store",
                payload={
                    "vault": OP_VAULT,
                    "title": "agent-db",
                    "generate": True,
                    "export_credential_ref": good_ref,
                },
            )
        )
        assert outcome.executor_ok is True
        assert outcome.output is not None
        document = json.loads(outcome.output.text)
        assert document["status"] == "ok"
        assert document["exported_credential_ref"] == good_ref
        assert document["generated"] is True
        credentials_path = data_root / "credentials.env"
        stored = parse_env_text(credentials_path.read_text(encoding="utf-8"))
        assert stored[good_ref]
        assert stored[good_ref] != REDACTED
        # The generated value itself never reaches the durable outcome.
        assert REDACTED in outcome.output.text
        assert stored[good_ref] not in outcome.output.text
        _sweep(outcome.to_json(), stored[good_ref])

    def test_vault_allowlist_default_and_disabled_config(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from mai.plugins.builtin_onepassword import CONFIG_SCHEMA
        from mai.plugins.config import PluginConfigStore

        data_root = tmp_path / "data"
        monkeypatch.setenv("MAI_DATA_ROOT", str(data_root))
        bin_dir = _op_fixtures._install_fake_op(tmp_path, items=_OP_LISTING)
        _registry, _runtime, dispatcher = self._compose(
            tmp_path, monkeypatch, bin_dir=bin_dir
        )
        store = PluginConfigStore(data_root)
        store.write_config(
            "onepassword",
            (
                "schema_version: 1\n"
                "plugin: onepassword\n"
                "enabled: true\n"
                "credential_refs: []\n"
                "config:\n"
                "  default_vault: mai-dev\n"
                "  allowed_vaults:\n"
                "    - mai-dev\n"
            ),
            expected_revision=0,
            config_schema=CONFIG_SCHEMA,
        )

        # No vault in the payload: the configured default applies.
        default_outcome: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=self._context(),
                capability_id="secret.list",
                payload={},
            )
        )
        assert default_outcome.executor_ok is True
        assert default_outcome.output is not None
        assert json.loads(default_outcome.output.text)["vault"] == OP_VAULT

        # Strict allowlist enforcement on the stored config.
        denied: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=self._context(),
                capability_id="secret.list",
                payload={"vault": "other-vault"},
            )
        )
        assert denied.executor_ok is False
        assert denied.output is not None
        assert "vault_not_allowed" in denied.output.text

        # Dispatch-time enabled recheck (worker refuses upstream, too).
        store.write_config(
            "onepassword",
            (
                "schema_version: 1\n"
                "plugin: onepassword\n"
                "enabled: false\n"
                "credential_refs: []\n"
                "config:\n"
                "  default_vault: mai-dev\n"
                "  allowed_vaults:\n"
                "    - mai-dev\n"
            ),
            expected_revision=1,
            config_schema=CONFIG_SCHEMA,
        )
        disabled: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=self._context(),
                capability_id="secret.list",
                payload={"vault": OP_VAULT},
            )
        )
        assert disabled.executor_ok is False
        assert disabled.output is not None
        assert "plugin_disabled" in disabled.output.text

    def test_missing_token_fails_structurally(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        bin_dir = _op_fixtures._install_fake_op(tmp_path, items=_OP_LISTING)
        _registry, _runtime, dispatcher = self._compose(
            tmp_path, monkeypatch, bin_dir=bin_dir
        )
        outcome: DispatchOutcome = _run(
            dispatcher.dispatch(
                context=self._context(credential_references=()),
                capability_id="secret.list",
                payload={"vault": OP_VAULT},
            )
        )
        assert outcome.allowed is True
        assert outcome.executor_ok is False
        assert outcome.output is not None
        assert "token_missing" in outcome.output.text
        assert OP_TOKEN not in outcome.output.text


