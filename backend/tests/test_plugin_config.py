"""Focused tests for the plugin/MCP configuration store (card 09-plugin-store).

Acceptance (BRIEF):
1. Versioned canonical configuration validates schema and stores credential
   references, with separate server grant bindings.
2. Restart preserves disable/config; deleted plugin cannot leave active tool
   binding silently usable.

Boundary discipline (test policy, risk-relevant slice: canonical data +
secret non-disclosure): schema/revision/confinement claims run on a real
temporary filesystem, including the crash-window proofs — the pre-replace
interruption is forced deterministically in-process (``os.replace`` made to
fail) and the divergence window (W2) is proven as honest ``hash_mismatch``
detection, never silent repair. The restart/deleted-plugin journey uses a
**real installed distribution** (module + ``.dist-info`` + entry points —
the same shape ``test_native_plugins.py`` froze) composed through the real
``NativePluginRuntime``, and a **fresh OS process** CLI journey: configure →
``compose`` (registered/exposed) → uninstall → re-``compose`` over the same
data root (not registered, exposure gone, binding surfaced as unbacked).
The store resolves no credentials and dispatches nothing: handlers in
fixtures raise if ever invoked, and all receipts/errors are swept for
secret sentinels (value-free discipline).
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from mai.plugins.config import (
    MAX_DOCUMENT_BYTES,
    PLUGIN_CONFIG_FILENAME,
    PluginConfigError,
    PluginConfigErrorCode,
    PluginConfigStore,
    compose_runtime,
    main,
    parse_grant_binding_text,
    parse_plugin_config_text,
    server_exposure,
    unbacked_bindings,
)
from mai.plugins.registry import CapabilityRegistry

BACKEND_DIR = Path(__file__).resolve().parents[1]

PLUGIN = "fxt.compile"
OTHER_PLUGIN = "fxt.other"
SECRET_NAME = "FXT_TOKEN"
SECRET_VALUE = "s3cr3t-config-value-9f2a"
CAPABILITY = "fxt.compile"
OTHER_CAPABILITY = "fxt.other.compile"
SERVER_A = uuid.UUID("7e6c1a34-9a2b-4c3d-8e5f-1029384756af")
SERVER_B = uuid.UUID("3f2e9a10-5b6c-4d7e-8f90-1a2b3c4d5e6f")

#: Declared config schema for the fixture plugin (JSON-Schema subset).
CONFIG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "endpoint": {"type": "string", "minLength": 1},
        "retries": {"type": "integer", "minimum": 0, "maximum": 10},
        "mode": {"enum": ["fast", "safe"]},
    },
    "additionalProperties": False,
    "required": ["endpoint"],
}


def _config_text(
    *,
    plugin: str = PLUGIN,
    enabled: bool = True,
    refs: tuple[str, ...] = (SECRET_NAME,),
    config_yaml: str = "{}",
) -> str:
    refs_yaml = "\n".join(f'  - "{ref}"' for ref in refs) or "  []"
    return f"""\
schema_version: 1
plugin: "{plugin}"
enabled: {'true' if enabled else 'false'}
credential_refs:
{refs_yaml}
config: {config_yaml}
"""


def _binding_text(
    plugin: str = PLUGIN,
    server_id: uuid.UUID = SERVER_A,
    capabilities: tuple[str, ...] = (CAPABILITY,),
) -> str:
    caps_yaml = "\n".join(f'  - "{cap}"' for cap in capabilities)
    return f"""\
schema_version: 1
plugin: "{plugin}"
server_id: "{server_id}"
capabilities:
{caps_yaml}
"""


def _write_plugin(
    store: PluginConfigStore,
    *,
    plugin: str = PLUGIN,
    enabled: bool = True,
    refs: tuple[str, ...] = (SECRET_NAME,),
    config_yaml: str = "{}",
    config_schema: Any = None,
    expected_revision: int = 0,
) -> Any:
    return store.write_config(
        plugin,
        _config_text(
            plugin=plugin, enabled=enabled, refs=refs, config_yaml=config_yaml
        ),
        expected_revision=expected_revision,
        config_schema=config_schema,
    )


def _installed_dist(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    *,
    plugin: str,
    identifier: str,
    version: str = "1.0.0",
) -> Path:
    """Install one real plugin distribution (module + dist-info) on sys.path.

    Same installed shape ``test_native_plugins.py`` froze for discovery:
    a module file plus a real ``.dist-info`` directory with ``METADATA``
    and ``entry_points.txt`` — no pip, no installer. The fixture handler
    raises if ever invoked: this card persists configuration and composes
    registries, it never executes capabilities.
    """
    stem = "fxtcfg_" + identifier.replace(".", "_").replace("-", "_")
    dist = tmp_path / f"dist-{identifier.replace('.', '-')}"
    dist.mkdir()
    (dist / f"{stem}.py").write_text(
        "CAPABILITY = {\n"
        f"    'identifier': {identifier!r},\n"
        f"    'version': {version!r},\n"
        "    'config_schema': None,\n"
        "    'input_schema': {'type': 'object'},\n"
        "    'output_schema': {'type': 'object'},\n"
        "    'execution_environments': ['controller'],\n"
        "    'effect_class': 'read',\n"
        "    'resource_scope': [],\n"
        f"    'required_grants': [{identifier!r}],\n"
        "}\n"
        f"CREDENTIAL_REFERENCES = [{SECRET_NAME!r}]\n"
        "async def execute(call):\n"
        "    raise AssertionError('capability executed outside dispatch')\n",
        encoding="utf-8",
    )
    dist_info = dist / f"{stem.replace('_', '-')}-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {stem.replace('_', '-')}\n"
        f"Version: {version}\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        f"[mai.capabilities]\n{plugin} = {stem}\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(dist))

    def _purge() -> None:
        sys.modules.pop(stem, None)
        importlib.invalidate_caches()

    request.addfinalizer(_purge)
    return dist


def _code_of(exc: PluginConfigError) -> PluginConfigErrorCode:
    return exc.code


def _compose_cli(
    *args: str,
    pythonpath: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if pythonpath is None:
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = os.pathsep.join(
            [pythonpath, env.get("PYTHONPATH", "")]
        ).removesuffix(os.pathsep)
    return subprocess.run(
        [sys.executable, "-m", "mai.plugins.config", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=BACKEND_DIR,
        check=False,
        timeout=120,
    )


# ---------------------------------------------------------------------------
# AC1 — versioned canonical configuration, schema-validated, reference-only
# ---------------------------------------------------------------------------


class TestVersionedCanonicalConfig:
    def test_create_roundtrip_revision_one(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        stored = _write_plugin(store)
        assert stored.revision == 1
        assert stored.schema_revision == 1
        assert store.read_config(PLUGIN) == stored
        meta = json.loads(
            (store.root / "plugins" / PLUGIN / "config.meta.json").read_bytes()
        )
        assert meta["format"] == 1
        assert meta["kind"] == "config"
        assert meta["plugin"] == PLUGIN
        assert meta["revision"] == 1
        assert meta["schema_revision"] == 1
        assert meta["sha256"] == stored.sha256

    def test_update_increments_revision_and_replaces_content(
        self, tmp_path: Path
    ) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        updated = store.write_config(
            PLUGIN,
            _config_text(enabled=False, config_yaml='{"endpoint": "x"}'),
            expected_revision=1,
            config_schema=CONFIG_SCHEMA,
        )
        assert updated.revision == 2
        reread = store.read_config(PLUGIN)
        assert reread.revision == 2
        assert parse_plugin_config_text(reread.text).enabled is False

    def test_stale_revision_conflict(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        with pytest.raises(PluginConfigError) as exc:
            _write_plugin(store)
        assert _code_of(exc.value) is PluginConfigErrorCode.STALE_REVISION
        assert "expected revision 0" in exc.value.detail
        assert "current revision is 1" in exc.value.detail

    def test_stale_binding_revision_conflict(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        store.write_binding(
            PLUGIN, SERVER_A, _binding_text(), expected_revision=0
        )
        with pytest.raises(PluginConfigError) as exc:
            store.write_binding(
                PLUGIN, SERVER_A, _binding_text(), expected_revision=0
            )
        assert _code_of(exc.value) is PluginConfigErrorCode.STALE_REVISION

    def test_embedded_plugin_mismatch_rejected(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        with pytest.raises(PluginConfigError) as exc:
            store.write_config(
                PLUGIN,
                _config_text(plugin=OTHER_PLUGIN),
                expected_revision=0,
                config_schema=None,
            )
        assert _code_of(exc.value) is PluginConfigErrorCode.PLUGIN_MISMATCH

    def test_embedded_server_mismatch_rejected(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        with pytest.raises(PluginConfigError) as exc:
            store.write_binding(
                PLUGIN, SERVER_A, _binding_text(server_id=SERVER_B),
                expected_revision=0,
            )
        assert _code_of(exc.value) is PluginConfigErrorCode.SERVER_ID_MISMATCH

    def test_invalid_identity_arguments(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        for bad in ("../escape", "Upper", "", "a/b", ".hidden", "x" * 129):
            with pytest.raises(PluginConfigError) as exc:
                _write_plugin(store, plugin=bad)
            assert _code_of(exc.value) is PluginConfigErrorCode.INVALID_PLUGIN_NAME
        with pytest.raises(PluginConfigError) as exc:
            store.write_binding(
                PLUGIN,
                "7e6c1a34-9a2b-4c3d-8e5f-1029384756zz",  # type: ignore[arg-type]
                _binding_text(),
                expected_revision=0,
            )
        assert _code_of(exc.value) is PluginConfigErrorCode.INVALID_SERVER_ID
        for bad_revision in (-1, True, "1", 1.0):
            with pytest.raises(PluginConfigError) as exc:
                store.write_binding(
                    PLUGIN,
                    SERVER_A,
                    _binding_text(),
                    expected_revision=bad_revision,  # type: ignore[arg-type]
                )
            assert _code_of(exc.value) is PluginConfigErrorCode.INVALID_REVISION

    def test_document_too_large_rejected(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        filler = "a" * (MAX_DOCUMENT_BYTES + 1)
        with pytest.raises(PluginConfigError) as exc:
            _write_plugin(store, config_yaml='{"pad": "%s"}' % filler)
        assert _code_of(exc.value) is PluginConfigErrorCode.DOCUMENT_TOO_LARGE

    def test_deep_config_nesting_rejected(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        deep = "x"
        for _ in range(40):
            deep = f"{{\"n\": {deep}}}"
        with pytest.raises(PluginConfigError) as exc:
            _write_plugin(store, config_yaml=deep)
        assert _code_of(exc.value) is PluginConfigErrorCode.DOCUMENT_TOO_DEEP

    def test_alias_documents_rejected(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        text = (
            "schema_version: 1\n"
            f'plugin: "{PLUGIN}"\n'
            "enabled: true\n"
            "credential_refs: []\n"
            "base: &base {\"endpoint\": \"e\"}\n"
            "config: *base\n"
        )
        with pytest.raises(PluginConfigError) as exc:
            store.write_config(
                PLUGIN, text, expected_revision=0, config_schema=CONFIG_SCHEMA
            )
        assert (
            _code_of(exc.value) is PluginConfigErrorCode.DOCUMENT_ALIAS_FORBIDDEN
        )

    def test_unparsable_and_duplicate_keys_rejected(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        with pytest.raises(PluginConfigError) as exc:
            _write_plugin(store, config_yaml="{unbalanced")
        assert (
            _code_of(exc.value) is PluginConfigErrorCode.UNPARSABLE_DOCUMENT
        )
        duplicate = _config_text().replace(
            'enabled: true', 'enabled: true\nenabled: false'
        )
        with pytest.raises(PluginConfigError) as exc:
            store.write_config(
                PLUGIN, duplicate, expected_revision=0, config_schema=None
            )
        assert (
            _code_of(exc.value) is PluginConfigErrorCode.UNPARSABLE_DOCUMENT
        )

    def test_schema_vocabulary_enforced(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        with pytest.raises(PluginConfigError) as exc:
            _write_plugin(
                store,
                config_yaml='{"endpoint": "e", "extra": 1}',
                config_schema=CONFIG_SCHEMA,
            )
        assert _code_of(exc.value) is PluginConfigErrorCode.CONFIG_SCHEMA_INVALID
        with pytest.raises(PluginConfigError) as exc:
            _write_plugin(
                store, config_yaml='{"retries": 1}', config_schema=CONFIG_SCHEMA
            )
        assert _code_of(exc.value) is PluginConfigErrorCode.CONFIG_SCHEMA_INVALID
        with pytest.raises(PluginConfigError) as exc:
            _write_plugin(
                store, config_yaml='{"endpoint": 5}', config_schema=CONFIG_SCHEMA
            )
        assert _code_of(exc.value) is PluginConfigErrorCode.CONFIG_SCHEMA_INVALID
        with pytest.raises(PluginConfigError) as exc:
            _write_plugin(
                store,
                config_yaml='{"endpoint": "e", "mode": "turbo"}',
                config_schema=CONFIG_SCHEMA,
            )
        assert _code_of(exc.value) is PluginConfigErrorCode.CONFIG_SCHEMA_INVALID

    def test_config_schema_unsupported_keyword_fail_closed(
        self, tmp_path: Path
    ) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        with pytest.raises(PluginConfigError) as exc:
            store.write_config(
                PLUGIN,
                _config_text(),
                expected_revision=0,
                config_schema={"allOf": [{"type": "object"}]},
            )
        assert _code_of(exc.value) is PluginConfigErrorCode.CONFIG_SCHEMA_INVALID
        assert "allOf" in exc.value.detail

    def test_config_without_declared_schema_must_be_empty(
        self, tmp_path: Path
    ) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        with pytest.raises(PluginConfigError) as exc:
            _write_plugin(store, config_yaml='{"endpoint": "e"}')
        assert _code_of(exc.value) is PluginConfigErrorCode.CONFIG_SCHEMA_INVALID
        stored = _write_plugin(store, config_yaml="{}")
        assert stored.revision == 1

    def test_document_schema_fields(self) -> None:
        with pytest.raises(PluginConfigError) as exc:
            parse_plugin_config_text('schema_version: 1\nplugin: "p"\n')
        assert _code_of(exc.value) is PluginConfigErrorCode.MISSING_FIELD
        with pytest.raises(PluginConfigError) as exc:
            parse_plugin_config_text(
                _config_text().replace("schema_version: 1", "schema_version: 2")
            )
        assert (
            _code_of(exc.value) is PluginConfigErrorCode.SCHEMA_VERSION_UNSUPPORTED
        )
        with pytest.raises(PluginConfigError) as exc:
            parse_plugin_config_text(_config_text().replace(
                "enabled: true", 'enabled: "true"'
            ))
        assert _code_of(exc.value) is PluginConfigErrorCode.FIELD_TYPE_INVALID
        with pytest.raises(PluginConfigError) as exc:
            parse_plugin_config_text(_config_text().replace(
                "config: {}", "unknown_extra: 1"
            ))
        assert _code_of(exc.value) is PluginConfigErrorCode.UNKNOWN_FIELD

    def test_credential_references_validated(self) -> None:
        with pytest.raises(PluginConfigError) as exc:
            parse_plugin_config_text(_config_text(refs=("lower_case",)))
        assert (
            _code_of(exc.value) is PluginConfigErrorCode.CREDENTIAL_REF_INVALID
        )
        assert "lower_case" in exc.value.detail  # names are not secret values
        with pytest.raises(PluginConfigError) as exc:
            parse_plugin_config_text(_config_text(refs=(SECRET_NAME, SECRET_NAME)))
        assert _code_of(exc.value) is PluginConfigErrorCode.FIELD_TYPE_INVALID
        document = parse_plugin_config_text(_config_text())
        assert document.credential_refs == (SECRET_NAME,)
        assert document.config == {}
        assert document.enabled is True
        assert document.plugin == PLUGIN

    def test_inline_credential_keys_rejected_value_free(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        for key in ("api_key", "password", "secret_token", "ACCESS_KEY"):
            nested = '{"endpoint": "e", "%s": "%s"}' % (key, SECRET_VALUE)
            with pytest.raises(PluginConfigError) as exc:
                _write_plugin(store, config_yaml=nested)
            assert (
                _code_of(exc.value)
                is PluginConfigErrorCode.INLINE_CREDENTIAL_FORBIDDEN
            )
            assert SECRET_VALUE not in exc.value.detail
            assert SECRET_VALUE not in str(exc.value)


# ---------------------------------------------------------------------------
# AC1 — separate server grant bindings
# ---------------------------------------------------------------------------


class TestGrantBindings:
    def test_binding_roundtrip_listing_and_aggregation(
        self, tmp_path: Path
    ) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store, plugin=PLUGIN)
        _write_plugin(store, plugin=OTHER_PLUGIN)
        store.write_binding(
            PLUGIN, SERVER_A, _binding_text(), expected_revision=0
        )
        store.write_binding(
            PLUGIN, SERVER_B, _binding_text(PLUGIN, SERVER_B), expected_revision=0
        )
        store.write_binding(
            OTHER_PLUGIN,
            SERVER_A,
            _binding_text(OTHER_PLUGIN, SERVER_A, (OTHER_CAPABILITY,)),
            expected_revision=0,
        )
        listing = store.list_bindings(PLUGIN)
        assert [document.server_id for document in listing] == sorted(
            [SERVER_A, SERVER_B]
        )
        assert store.server_grants(SERVER_A) == (CAPABILITY, OTHER_CAPABILITY)
        assert store.server_grants(SERVER_B) == (CAPABILITY,)
        other = uuid.uuid4()
        assert store.server_grants(other) == ()

    def test_binding_requires_plugin_config(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        with pytest.raises(PluginConfigError) as exc:
            store.write_binding(
                PLUGIN, SERVER_A, _binding_text(), expected_revision=0
            )
        assert _code_of(exc.value) is PluginConfigErrorCode.NOT_FOUND

    def test_config_write_never_creates_bindings(self, tmp_path: Path) -> None:
        """No installation-time auto-grant (SERVICES decision-dependency row)."""
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        assert store.list_bindings(PLUGIN) == ()
        assert store.server_grants(SERVER_A) == ()

    def test_delete_config_refused_while_bindings_present(
        self, tmp_path: Path
    ) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        store.write_binding(
            PLUGIN, SERVER_A, _binding_text(), expected_revision=0
        )
        with pytest.raises(PluginConfigError) as exc:
            store.delete_config(PLUGIN, expected_revision=1)
        assert _code_of(exc.value) is PluginConfigErrorCode.BINDINGS_PRESENT
        store.delete_binding(PLUGIN, SERVER_A, expected_revision=1)
        store.delete_config(PLUGIN, expected_revision=1)
        with pytest.raises(PluginConfigError) as exc:
            store.read_config(PLUGIN)
        assert _code_of(exc.value) is PluginConfigErrorCode.NOT_FOUND

    def test_binding_capabilities_validated(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        for caps in ((), (CAPABILITY, CAPABILITY), ("FXT.UPPER",)):
            with pytest.raises(PluginConfigError) as exc:
                store.write_binding(
                    PLUGIN,
                    SERVER_A,
                    _binding_text(capabilities=caps),
                    expected_revision=0,
                )
            assert _code_of(exc.value) is PluginConfigErrorCode.FIELD_TYPE_INVALID

    def test_binding_schema_fields(self) -> None:
        with pytest.raises(PluginConfigError) as exc:
            parse_grant_binding_text(
                f'schema_version: 1\nplugin: "{PLUGIN}"\n'
                f'server_id: "{SERVER_A}"\n'
            )
        assert _code_of(exc.value) is PluginConfigErrorCode.MISSING_FIELD
        with pytest.raises(PluginConfigError) as exc:
            parse_grant_binding_text(
                _binding_text().replace(f'"{SERVER_A}"', '"not-a-uuid"')
            )
        assert _code_of(exc.value) is PluginConfigErrorCode.INVALID_SERVER_ID
        with pytest.raises(PluginConfigError) as exc:
            parse_grant_binding_text(
                _binding_text().replace(
                    "capabilities:", "extra_junk: 1\ncapabilities:"
                )
            )
        assert _code_of(exc.value) is PluginConfigErrorCode.UNKNOWN_FIELD


# ---------------------------------------------------------------------------
# Confinement (C02 rule 3)
# ---------------------------------------------------------------------------


class TestConfinement:
    def test_symlinked_component_rejected(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        outside = tmp_path / "outside"
        outside.mkdir()
        link = store.root / "plugins" / OTHER_PLUGIN
        link.symlink_to(outside)
        with pytest.raises(PluginConfigError) as exc:
            _write_plugin(store, plugin=OTHER_PLUGIN)
        assert _code_of(exc.value) is PluginConfigErrorCode.PATH_ESCAPE
        with pytest.raises(PluginConfigError) as exc:
            store.read_config(OTHER_PLUGIN)
        assert _code_of(exc.value) is PluginConfigErrorCode.PATH_ESCAPE

    def test_restrictive_permissions(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        assert (store.root.stat().st_mode & 0o077) == 0
        document = store.root / "plugins" / PLUGIN / PLUGIN_CONFIG_FILENAME
        assert (document.stat().st_mode & 0o077) == 0

    def test_listing_skips_non_namespace_entries(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        (store.root / "plugins" / "Not_A_Plugin").mkdir()
        bindings = store.root / "plugins" / PLUGIN / "bindings"
        bindings.mkdir()
        (bindings / "not-a-uuid.yaml").write_text("junk", encoding="utf-8")
        assert store.list_plugins() == (PLUGIN,)
        assert store.list_bindings(PLUGIN) == ()


# ---------------------------------------------------------------------------
# AC2 — restart preservation and deleted-plugin safety
# ---------------------------------------------------------------------------


class TestRestartPreservation:
    def test_new_store_instance_preserves_disable_config_and_bindings(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "data-root"
        first = PluginConfigStore(root)
        _write_plugin(first, enabled=False)
        first.write_binding(
            PLUGIN, SERVER_A, _binding_text(), expected_revision=0
        )
        # Restart: a fresh store over the same root (fresh worker process).
        second = PluginConfigStore(root)
        assert second.disabled_plugins() == (PLUGIN,)
        stored = second.read_config(PLUGIN)
        assert stored.revision == 1
        document = parse_plugin_config_text(stored.text)
        assert document.enabled is False
        assert document.credential_refs == (SECRET_NAME,)
        assert second.server_grants(SERVER_A) == (CAPABILITY,)

    def test_disabled_plugin_skipped_at_load_then_enabled_by_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        request: pytest.FixtureRequest,
        tmp_path: Path,
    ) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store, enabled=False)
        _installed_dist(
            monkeypatch, request, tmp_path, plugin=PLUGIN, identifier=CAPABILITY
        )
        registry = CapabilityRegistry()
        runtime = compose_runtime(store, registry=registry)
        runtime.discover()
        assert runtime.skipped_disabled == (PLUGIN,)
        assert len(registry) == 0
        assert server_exposure(store, SERVER_A, registry) == ()
        # Re-enable through the store (revision-pinned), recompose: exposed.
        store.write_config(
            PLUGIN,
            _config_text(enabled=True),
            expected_revision=1,
            config_schema=None,
        )
        fresh_registry = CapabilityRegistry()
        fresh_runtime = compose_runtime(store, registry=fresh_registry)
        fresh_runtime.discover()
        assert [p.plugin for p in fresh_runtime.plugins] == [PLUGIN]
        assert CAPABILITY in {
            descriptor.identifier for descriptor in fresh_registry.list_capabilities()
        }

    def test_exposure_is_grant_inter_registered_never_union(
        self,
        monkeypatch: pytest.MonkeyPatch,
        request: pytest.FixtureRequest,
        tmp_path: Path,
    ) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        store.write_binding(
            PLUGIN, SERVER_A, _binding_text(), expected_revision=0
        )
        # Installed but no binding for SERVER_B, and a granted capability
        # no descriptor provides.
        _installed_dist(
            monkeypatch, request, tmp_path, plugin=PLUGIN, identifier=CAPABILITY
        )
        store.write_binding(
            PLUGIN,
            SERVER_B,
            _binding_text(PLUGIN, SERVER_B, (OTHER_CAPABILITY,)),
            expected_revision=0,
        )
        registry = CapabilityRegistry()
        compose_runtime(store, registry=registry).discover()
        assert server_exposure(store, SERVER_A, registry) == (CAPABILITY,)
        assert server_exposure(store, SERVER_B, registry) == ()
        unbacked = unbacked_bindings(store, registry)
        assert [(u.plugin, u.server_id, u.missing_capabilities) for u in unbacked] == [
            (PLUGIN, SERVER_B, (OTHER_CAPABILITY,))
        ]

    def test_deleted_plugin_cannot_leave_binding_usable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Binding present, plugin absent from discovery: unusable + surfaced."""
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        store.write_binding(
            PLUGIN, SERVER_A, _binding_text(), expected_revision=0
        )
        # No distribution installed for PLUGIN: a fresh worker composition
        # must expose nothing for SERVER_A and must surface the stale
        # binding instead of silently honoring or silently dropping it.
        registry = CapabilityRegistry()
        compose_runtime(store, registry=registry).discover()
        assert len(registry) == 0
        assert CAPABILITY not in {
            descriptor.identifier for descriptor in registry.list_capabilities()
        }
        assert server_exposure(store, SERVER_A, registry) == ()
        unbacked = unbacked_bindings(store, registry)
        assert [(u.plugin, u.server_id) for u in unbacked] == [
            (PLUGIN, SERVER_A)
        ]
        assert unbacked[0].missing_capabilities == (CAPABILITY,)


class TestCrashWindows:
    def test_interrupt_before_replace_preserves_previous_revision(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        real_replace = os.replace

        def failing_replace(src: object, dst: object, **kw: object) -> None:
            if str(dst).endswith(PLUGIN_CONFIG_FILENAME):
                raise OSError("forced interruption before replace")
            real_replace(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(
            "mai.plugins.config.os.replace", failing_replace
        )
        with pytest.raises(OSError):
            store.write_config(
                PLUGIN,
                _config_text(enabled=False),
                expected_revision=1,
                config_schema=None,
            )
        monkeypatch.undo()
        # W1: the previous committed pair is intact and readable.
        stored = store.read_config(PLUGIN)
        assert stored.revision == 1
        assert parse_plugin_config_text(stored.text).enabled is True
        # Recovery: the write can be re-executed (revision still pinned).
        recovered = store.write_config(
            PLUGIN,
            _config_text(enabled=False),
            expected_revision=1,
            config_schema=None,
        )
        assert recovered.revision == 2

    def test_divergence_detected_not_repaired(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        document = store.root / "plugins" / PLUGIN / PLUGIN_CONFIG_FILENAME
        document.write_text(
            _config_text(enabled=False), encoding="utf-8"
        )
        with pytest.raises(PluginConfigError) as exc:
            store.read_config(PLUGIN)
        assert _code_of(exc.value) is PluginConfigErrorCode.HASH_MISMATCH

    def test_half_state_detected(self, tmp_path: Path) -> None:
        store = PluginConfigStore(tmp_path / "data-root")
        _write_plugin(store)
        document = store.root / "plugins" / PLUGIN / PLUGIN_CONFIG_FILENAME
        # W4 half-deleted state (metadata without canonical document):
        # repeating the delete completes it — never silent resurrection.
        document.unlink()
        store.delete_config(PLUGIN, expected_revision=1)
        with pytest.raises(PluginConfigError) as exc:
            store.read_config(PLUGIN)
        assert _code_of(exc.value) is PluginConfigErrorCode.NOT_FOUND
        # Reverse half-state: canonical document without metadata blocks
        # reads and writes until reconciliation.
        document.write_text(_config_text(), encoding="utf-8")
        with pytest.raises(PluginConfigError) as exc:
            store.read_config(PLUGIN)
        assert _code_of(exc.value) is PluginConfigErrorCode.METADATA_MISSING
        with pytest.raises(PluginConfigError) as exc:
            _write_plugin(store)
        assert _code_of(exc.value) is PluginConfigErrorCode.METADATA_MISSING


# ---------------------------------------------------------------------------
# Fresh-process CLI journey (restart + uninstall over one data root)
# ---------------------------------------------------------------------------


class TestCliFreshProcess:
    def test_configure_compose_uninstall_recompose(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "data-root"
        store = PluginConfigStore(root)
        _write_plugin(store, enabled=True)
        store.write_binding(
            PLUGIN, SERVER_A, _binding_text(), expected_revision=0
        )
        dist = _build_dist_dir(tmp_path, plugin=PLUGIN, identifier=CAPABILITY)

        # Restart 1: plugin installed and enabled — registered and exposed.
        installed = _compose_cli("compose", str(root), pythonpath=str(dist))
        assert installed.returncode == 0, installed.stderr
        payload = json.loads(installed.stdout)
        assert [p["identifier"] for p in payload["plugins"]] == [CAPABILITY]
        assert payload["skipped_disabled"] == []
        assert payload["exposure"] == {str(SERVER_A): [CAPABILITY]}
        assert payload["unbacked"] == []
        assert payload["disabled"] == []

        # Restart 2: the plugin distribution is gone (deleted/uninstalled).
        # Same data root, fresh process: config and binding are preserved,
        # but nothing may be silently usable and the stale binding must be
        # surfaced.
        uninstalled = _compose_cli("compose", str(root))
        assert uninstalled.returncode == 0, uninstalled.stderr
        gone = json.loads(uninstalled.stdout)
        assert gone["plugins"] == []
        assert gone["exposure"] == {str(SERVER_A): []}
        assert gone["unbacked"] == [
            {
                "plugin": PLUGIN,
                "server_id": str(SERVER_A),
                "missing_capabilities": [CAPABILITY],
            }
        ]
        assert gone["disabled"] == []
        # Restart 3: the stored configuration itself survived both restarts.
        inspected = _compose_cli("inspect", str(root))
        assert inspected.returncode == 0, inspected.stderr
        state = json.loads(inspected.stdout)
        assert state["plugins"] == [
            {
                "plugin": PLUGIN,
                "enabled": True,
                "credential_refs": [SECRET_NAME],
                "revision": 1,
                "schema_revision": 1,
            }
        ]
        assert state["bindings"] == [
            {
                "plugin": PLUGIN,
                "server_id": str(SERVER_A),
                "capabilities": [CAPABILITY],
                "revision": 1,
                "schema_revision": 1,
            }
        ]
        for sentinel in (SECRET_VALUE,):
            assert sentinel not in installed.stdout
            assert sentinel not in uninstalled.stdout
            assert sentinel not in inspected.stdout

    def test_cli_disable_is_visible_after_restart(self, tmp_path: Path) -> None:
        root = tmp_path / "data-root"
        store = PluginConfigStore(root)
        _write_plugin(store, enabled=False)
        dist = _build_dist_dir(tmp_path, plugin=PLUGIN, identifier=CAPABILITY)
        proc = _compose_cli("compose", str(root), pythonpath=str(dist))
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["plugins"] == []
        assert payload["skipped_disabled"] == [PLUGIN]
        assert payload["disabled"] == [PLUGIN]
        assert payload["exposure"] == {}

    def test_cli_rejects_corrupt_store_and_bad_usage(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "data-root"
        store = PluginConfigStore(root)
        _write_plugin(store)
        document = store.root / "plugins" / PLUGIN / PLUGIN_CONFIG_FILENAME
        document.write_text("tampered", encoding="utf-8")
        proc = _compose_cli("inspect", str(root))
        assert proc.returncode == 1
        error = json.loads(proc.stdout)["error"]
        assert error["code"] == PluginConfigErrorCode.HASH_MISMATCH.value
        missing = _compose_cli("inspect", str(tmp_path / "absent"))
        assert missing.returncode == 2
        usage = _compose_cli()
        assert usage.returncode == 2


def _build_dist_dir(
    tmp_path: Path, *, plugin: str, identifier: str, version: str = "1.0.0"
) -> Path:
    """Distribution directory for subprocess runs (never on this sys.path)."""
    stem = "fxtcfg_" + identifier.replace(".", "_").replace("-", "_")
    dist = tmp_path / f"dist-{identifier.replace('.', '-')}"
    dist.mkdir(exist_ok=True)
    (dist / f"{stem}.py").write_text(
        "CAPABILITY = {\n"
        f"    'identifier': {identifier!r},\n"
        f"    'version': {version!r},\n"
        "    'config_schema': None,\n"
        "    'input_schema': {'type': 'object'},\n"
        "    'output_schema': {'type': 'object'},\n"
        "    'execution_environments': ['controller'],\n"
        "    'effect_class': 'read',\n"
        "    'resource_scope': [],\n"
        f"    'required_grants': [{identifier!r}],\n"
        "}\n"
        f"CREDENTIAL_REFERENCES = [{SECRET_NAME!r}]\n"
        "async def execute(call):\n"
        "    raise AssertionError('capability executed outside dispatch')\n",
        encoding="utf-8",
    )
    dist_info = dist / f"{stem.replace('_', '-')}-{version}.dist-info"
    dist_info.mkdir(exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {stem.replace('_', '-')}\n"
        f"Version: {version}\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        f"[mai.capabilities]\n{plugin} = {stem}\n", encoding="utf-8"
    )
    return dist


# ---------------------------------------------------------------------------
# Import surface — persistence only: no environment, no execution
# ---------------------------------------------------------------------------


class TestImportSurface:
    def test_module_never_touches_environment_or_execution(self) -> None:
        source = (BACKEND_DIR / "src" / "mai" / "plugins" / "config.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        banned_modules = {
            "subprocess",
            "socket",
            "importlib",
            "httpx",
            "requests",
            "urllib",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name.split(".")[0] not in banned_modules
                    for alias in node.names
                ), ast.dump(node)
            elif isinstance(node, ast.ImportFrom):
                assert node.module is None or (
                    node.module.split(".")[0] not in banned_modules
                ), ast.dump(node)
            elif isinstance(node, ast.Attribute):
                assert node.attr not in {"environ", "getenv"}, ast.dump(node)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "compile"}, ast.dump(
                    node
                )


# ---------------------------------------------------------------------------
# Entrypoint wiring (main(argv) contract, in-process)
# ---------------------------------------------------------------------------


def test_main_reports_structured_error_and_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "data-root"
    store = PluginConfigStore(root)
    _write_plugin(store)
    assert main(["inspect", str(root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plugins"][0]["plugin"] == PLUGIN
    assert SECRET_VALUE not in json.dumps(payload)
    document = store.root / "plugins" / PLUGIN / PLUGIN_CONFIG_FILENAME
    document.unlink()
    assert main(["inspect", str(root)]) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == PluginConfigErrorCode.NOT_FOUND.value
