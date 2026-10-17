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
from mai.credentials.env import EnvResolver
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
        """The envelope vocabulary is exactly {capability, references};
        anything else is the explicit compatibility failure."""
        schema = NativePluginManifest.model_json_schema()
        assert sorted(schema["properties"]) == ["capability", "credential_references"]
        assert schema["additionalProperties"] is False

        valid = {"capability": CAPABILITY_DOC, "credential_references": []}
        manifest = NativePluginManifest.model_validate(valid)
        assert manifest.capability == CAPABILITY_DOC
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
            [sys.executable, "-m", "mai.plugins.native", "inspect"],
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
            [sys.executable, "-m", "mai.plugins.native", "inspect"],
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
                    prologue=f"_LOG.write_text('imported')\n",
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
