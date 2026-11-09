"""First-admission plugin tool exposure: the worker registry must be
plugin-correct BEFORE the first ``CapabilityToolBridge`` reads it.

Incident (grant_exposure_receipt.json, 2026-11-09): a fresh, enabled,
globally bound builtin ``onepassword`` (``secret.list``/``secret.get``,
grant_binding.json) produced NO ``secret_*`` tools on the first session
— the bridge skips granted-but-registry-absent descriptors ("nothing to
expose") and the worker only registered plugin descriptors lazily inside
the composite executor on a missing dispatch target, so a fresh grant
could never bootstrap its own registration.

Boundary discipline (test policy): every test drives the REAL
production composition path — ``mai.jobs.worker._configure_default_agent_runtime``
over a disposable data root and a database URL that is never connected
(no production DB, no network, no onepassword execution, no credential
resolution: the data root never even gains a ``.env``). The process-global
session runtime is reset around every test so unrelated suites stay
deterministic.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Iterator

import pytest

from mai.agents import session_runner
from mai.jobs import worker
from mai.plugins.builtin_onepassword import CONFIG_SCHEMA, PLUGIN_NAME
from mai.plugins.config import PluginConfigStore
from mai.policy.evaluate import ExecutionContext, PolicyMode

#: The never-connected database URL (test_worker_web_read pattern): the
#: engine and stores compose lazily, nothing dials.
_DB_URL = "postgresql://mai:mai@127.0.0.1:5999/mai"

GENERAL_CONSULT = uuid.UUID(int=0)

_ENABLED_CONFIG = """\
schema_version: 1
plugin: onepassword
enabled: true
credential_refs: []
config:
  default_vault: PlayerVault
  allowed_vaults:
    - PlayerVault
"""

_DISABLED_CONFIG = """\
schema_version: 1
plugin: onepassword
enabled: false
credential_refs: []
config:
  default_vault: PlayerVault
  allowed_vaults:
    - PlayerVault
"""


def _binding(caps: tuple[str, ...]) -> str:
    rows = "\n".join(f"  - {cap}" for cap in caps)
    return (
        "schema_version: 1\n"
        f"plugin: {PLUGIN_NAME}\n"
        f"server_id: {GENERAL_CONSULT}\n"
        f"capabilities:\n{rows}\n"
    )


def _bind_granted_secrets(store: PluginConfigStore) -> None:
    """The persisted production state (grant_binding.json): enabled
    builtin config plus a GLOBAL (server zero) binding for exactly
    ``secret.list``/``secret.get`` — never ``secret.store``."""
    store.write_config(
        PLUGIN_NAME,
        _ENABLED_CONFIG,
        expected_revision=0,
        config_schema=CONFIG_SCHEMA,
    )
    store.write_binding(
        PLUGIN_NAME,
        GENERAL_CONSULT,
        _binding(("secret.list", "secret.get")),
        expected_revision=0,
    )


@pytest.fixture()
def fresh_runtime() -> Iterator[None]:
    """Isolate the process-global session runtime around each test."""
    session_runner.reset_session_run()
    yield
    session_runner.reset_session_run()


def _compose_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """One fresh production composition on a disposable data root.

    Requires the ``fresh_runtime`` fixture (reset bracket) in the test
    signature.
    """
    monkeypatch.setenv("MAI_DATA_ROOT", str(tmp_path))
    worker._configure_default_agent_runtime(_DB_URL)
    return session_runner._EXECUTOR


@pytest.fixture()
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_runtime):
    """Fresh runtime with the incident's persisted plugin state."""
    _bind_granted_secrets(PluginConfigStore(tmp_path))
    return _compose_runtime(tmp_path, monkeypatch)


def _context(allowed: tuple[str, ...]) -> ExecutionContext:
    return ExecutionContext(
        principal="tester@mai.local",
        server_id=GENERAL_CONSULT,
        run_id=uuid.uuid4(),
        parent_scope=None,
        profile_revision="prof-1",
        prompt_revision="prompt-1",
        credential_references=(),
        allowed_capabilities=allowed,
        mode=PolicyMode.DELEGATED,
    )


class TestFirstAdmissionExposure:
    """AC1: real ``secret_*`` tools on the FIRST tool bridge, with no
    prior dispatch. Red on the lazy-only base: the registry is empty for
    ``secret.*`` before any dispatch, so the bridge exposes nothing."""

    def test_registry_holds_granted_secrets_before_any_bridge_or_dispatch(
        self, runtime
    ) -> None:
        registry = runtime._registry
        assert registry.versions("secret.list") != ()
        assert registry.versions("secret.get") != ()

    def test_first_bridge_exposes_real_secret_tools(self, runtime) -> None:
        bridge = runtime.bridge_of(_context(("secret.list", "secret.get")))
        # FIRST bridge construction: exposure follows the now-correct
        # registry; nothing was dispatched beforehand in this runtime.
        assert bridge.enabled_capabilities == ("secret.list", "secret.get")
        names = {tool.name for tool in bridge.tools}
        assert names == {"secret_list", "secret_get"}
        assert bridge.descriptor_for("secret.list") is not None
        assert bridge.descriptor_for("secret.get") is not None

    def test_global_binding_grants_exactly_the_persisted_pair(
        self, runtime
    ) -> None:
        grants = runtime.steps._dispatcher._grants()
        assert GENERAL_CONSULT in grants.get("secret.list", frozenset())
        assert GENERAL_CONSULT in grants.get("secret.get", frozenset())
        # The binding never granted the write surface.
        assert GENERAL_CONSULT not in grants.get("secret.store", frozenset())

    def test_non_granted_secret_store_absent_from_bridge(self, runtime) -> None:
        bridge = runtime.bridge_of(_context(("secret.list", "secret.get")))
        assert "secret.store" not in bridge.enabled_capabilities
        assert "secret_store" not in {tool.name for tool in bridge.tools}


class TestFailClosedSurfaces:
    """AC2: disabled plugins and missing configuration must not expose
    secret tools; the built-in host surface is unchanged; no credential
    resolution happens during composition."""

    def test_disabled_plugin_is_absent_everywhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_runtime
    ) -> None:
        store = PluginConfigStore(tmp_path)
        store.write_config(
            PLUGIN_NAME,
            _DISABLED_CONFIG,
            expected_revision=0,
            config_schema=CONFIG_SCHEMA,
        )
        store.write_binding(
            PLUGIN_NAME,
            GENERAL_CONSULT,
            _binding(("secret.list",)),
            expected_revision=0,
        )
        runtime = _compose_runtime(tmp_path, monkeypatch)
        registry = runtime._registry
        # Load-time disable skips the plugin before any import: its
        # descriptors are never registered.
        assert registry.versions("secret.list") == ()
        # The grants lane also skips disabled plugins' bindings.
        grants = runtime.steps._dispatcher._grants()
        assert GENERAL_CONSULT not in grants.get("secret.list", frozenset())
        bridge = runtime.bridge_of(_context(("secret.list",)))
        assert bridge.enabled_capabilities == ()

    def test_vanished_config_fails_closed_on_grants(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_runtime
    ) -> None:
        """Bound but with no readable stored config, the grant lane
        refuses (fail-closed): no exposure, composition still succeeds,
        host tools unaffected."""
        store = PluginConfigStore(tmp_path)
        _bind_granted_secrets(store)
        # Simulate the vanished config file pair. The store API refuses
        # this while bindings exist (by design), so the raw deletion is
        # exactly the out-of-band corruption the grants lane must survive.
        plugin_dir = tmp_path / "plugins" / PLUGIN_NAME
        (plugin_dir / "config.yaml").unlink()
        (plugin_dir / "config.meta.json").unlink()
        runtime = _compose_runtime(tmp_path, monkeypatch)
        grants = runtime.steps._dispatcher._grants()
        assert GENERAL_CONSULT not in grants.get("secret.list", frozenset())
        assert GENERAL_CONSULT not in grants.get("secret.get", frozenset())
        # Host surface unaffected by the corrupt plugin store.
        assert runtime._registry.versions("host.shell") != ()

    def test_host_surface_unchanged_and_no_eager_credential_resolution(
        self, runtime, tmp_path: Path
    ) -> None:
        registry = runtime._registry
        for capability in (
            "host.shell",
            "host.logs",
            "host.file_read",
            "session.read",
            "docs.retrieve",
            "service.http",
            "web.search",
            "web.read",
        ):
            assert registry.versions(capability) != (), capability
        # A resolver construction materializes data_root/.env; its
        # absence proves composition performed zero credential
        # resolution (secrets still resolve per actual plugin call).
        assert not (tmp_path / ".env").exists()


class TestBrokenPluginIsolation:
    """AC5: a broken optional plugin cannot block unrelated host tools
    or sibling plugins. Discovery records a per-plugin rejection and
    moves on; host descriptors were registered before the plugin lane."""

    def test_broken_third_party_plugin_does_not_block_host_or_builtin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_runtime
    ) -> None:
        broken = tmp_path / "broken_dist"
        dist_info = broken / "broken_plugin-1.0.dist-info"
        dist_info.mkdir(parents=True)
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: broken_plugin\nVersion: 1.0\n",
            encoding="utf-8",
        )
        (dist_info / "entry_points.txt").write_text(
            "[mai.capabilities]\n"
            "broken_thing = mai_nonexistent_plugin_module_xyz\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(broken))
        _bind_granted_secrets(PluginConfigStore(tmp_path))
        runtime = _compose_runtime(tmp_path, monkeypatch)
        registry = runtime._registry
        # Host tools intact, builtin sibling intact and exposed.
        assert registry.versions("host.shell") != ()
        assert registry.versions("secret.list") != ()
        bridge = runtime.bridge_of(_context(("secret.list", "secret.get")))
        assert bridge.enabled_capabilities == ("secret.list", "secret.get")
