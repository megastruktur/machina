"""Mai worker entrypoint: durable job execution, independent of the HTTP API.

The worker is one process role of the ``mai`` backend package (SERVICES
``mai-worker`` row): it launches the full DBOS engine against Machina's
Postgres — including launch recovery of pending runs — and claims runs from
the Machina queue (``mai.jobs.engine.QUEUE_NAME``). It imports nothing from
the HTTP server, so it can start, stop and restart independently of the API
(card 05-engine-adapter acceptance 2).

Verified command (placeholder Compose command is replaced by the later
platform wiring card, which owns the Compose service)::

    MAI_DATABASE_URL=postgresql://… python -m mai.jobs.worker

Environment:

- ``MAI_DATABASE_URL`` (required) — the Machina Postgres URL, resolved by
  :func:`mai.database.resolve_database_url` (the single URL contract).
- ``MAI_WORKER_HANDLERS`` (optional) — comma-separated import paths of
  modules that call :func:`mai.jobs.engine.register_job_handler`. Imported
  before launch; a module that fails to import stops the worker with exit 2
  (fail closed: an unregistered handler would fail its runs at execution
  time anyway). Until the owner cards land real handler modules this may be
  unset — the worker still launches, claims and recovers runs.

Signals: ``SIGTERM``/``SIGINT`` stop the engine cleanly (exit 0);
``SIGKILL`` leaves recovery to the next launch (job-engine ADR, exp1).
"""

from __future__ import annotations

import argparse
import importlib
import os
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from mai.database import resolve_database_url
from mai.jobs import engine

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mai.research.pages import WebPageCapability

_STOP = threading.Event()


def _request_stop(signum: int, _frame: object) -> None:
    """Record the stop signal; the main thread performs the shutdown."""
    del signum
    _STOP.set()


def _import_handlers(spec: str) -> int:
    """Import handler-registration modules; return the process exit code."""
    for name in (part.strip() for part in spec.split(",")):
        if not name:
            continue
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 - worker must fail closed
            print(
                f"mai worker: handler module '{name}' failed to import: "
                f"{type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return 2
    return 0


def _build_web_read_capability(
    *,
    transport: object | None = None,
    resolver: Callable[[str], Sequence[str]] | None = None,
) -> "WebPageCapability":
    """Construct the production ``web.read`` capability.

    The provider's byte ceiling is the schema's advertised maximum
    (:data:`mai.research.pages.MAX_MAX_BYTES`), so a schema-valid
    ``max_bytes`` request is never refused by the wiring itself — the
    advertised range and the enforced ceiling agree. The capability keeps
    its own defaults (default byte budget, timeout, Markdown budget)
    unchanged; only the provider ceiling moves.

    ``transport``/``resolver`` are the provider's own constructor seams
    (httpx2 transport and DNS resolver); the production call site passes
    neither, so production keeps real DNS and a real dial.
    """
    from mai.research.pages import (
        MAX_MAX_BYTES,
        HttpPageProvider,
        WebPageCapability,
    )

    provider = HttpPageProvider(
        max_bytes=MAX_MAX_BYTES, transport=transport, resolver=resolver
    )
    return WebPageCapability(provider)


def _configure_default_agent_runtime(database_url: str) -> None:
    """Wire the session-first runtime handlers when ``MAI_DATA_ROOT`` is set."""
    data_root_raw = os.environ.get("MAI_DATA_ROOT", "").strip()
    if not data_root_raw:
        return
    from pathlib import Path
    import uuid

    from mai.agents.model_store import ModelConfigStore
    from mai.applications.adopt import (
        ApplicationAdoption,
        ApplicationManifestStore,
    )
    from mai.applications.document import ApplicationDocService
    from mai.applications.service_map import (
        CAPABILITY_ID as UPSERT_MAP_CAPABILITY,
        ServiceMapStore,
        ServiceMapUpsertCapability,
        upsert_map_descriptor,
    )
    from mai.capabilities.internal_api import (
        INTERNAL_API_CAPABILITY_IDS,
        build_internal_api_capabilities,
    )
    from mai.capabilities.history_tags import (
        TAGS_CAPABILITY_IDS,
        build_history_tag_capabilities,
    )
    from mai.capabilities.chat_read import (
        ChatReadSessionCapability,
        chat_read_descriptor,
    )
    from mai.capabilities.dispatch import (
        CapabilityCall,
        CapabilityDispatcher,
        ExecutorResult,
    )
    from mai.capabilities.host_shell import (
        HostShellCapability,
        descriptor as host_shell_descriptor,
    )
    from mai.capabilities.logs import (
        HostLogsCapability,
        descriptor as host_logs_descriptor,
    )
    from mai.capabilities.host_file_read import (
        HostFileReadCapability,
        descriptor as host_file_read_descriptor,
    )
    from mai.capabilities.service_http import (
        HttpServiceProvider,
        ServiceHttpCapability,
        descriptor as service_http_descriptor,
    )
    from mai.contracts.server import parse_server_profile_text
    from mai.credentials.env import EnvResolver, build_runtime_env_resolver
    from mai.database import create_app_engine
    from mai.execution.ssh import SshIdentity, pin_host_key
    from mai.jobs.resource_locks import ResourceLockService
    from mai.knowledge.index import GatewayEmbedder
    from mai.knowledge.documents import DocumentStore
    from mai.knowledge.retrieve import (
        DocumentRetrievalCapability,
        ScopeRetriever,
        descriptor as docs_retrieve_descriptor,
    )
    from mai.plugins.config import (
        PluginConfigStore,
        parse_plugin_config_text,
    )
    from mai.plugins.mcp import (
        McpServerRuntime,
        granted_capability_ids,
        server_plugins,
    )
    from mai.plugins.native import NativePluginRuntime
    from mai.plugins.registry import CapabilityRegistry
    from mai.policy.approvals import ApprovalStore
    from mai.policy.config import GrantConfigStore
    from mai.policy.evaluate import Policy
    from mai.profiles.files import ProfileFileStore
    from mai.research.pages import descriptor as web_read_descriptor
    from mai.research.search import (
        SearXNGProvider,
        WebSearchCapability,
        descriptor as web_search_descriptor,
    )
    from mai.skills.builtin import seed_builtin_skills

    data_root = Path(data_root_raw).expanduser()
    models_root = data_root / "models"
    intents_dir = data_root / "intents"
    known_hosts_dir = data_root / "known_hosts"
    artifacts_dir = data_root / "artifacts"
    for folder in (models_root, intents_dir, known_hosts_dir, artifacts_dir):
        folder.mkdir(parents=True, exist_ok=True)

    _, sessions = create_app_engine(database_url)
    profile_store = ProfileFileStore(data_root)
    grant_store = GrantConfigStore(data_root)
    plugin_store = PluginConfigStore(data_root)
    env_file = os.environ.get("MAI_ENV_FILE", "").strip()
    try:
        seed_builtin_skills(data_root)
    except Exception:
        pass

    def _load_profile(server_id: uuid.UUID):
        stored = profile_store.read(server_id)
        return parse_server_profile_text(stored.text)

    def _known_hosts_for(server_id: uuid.UUID) -> Path:
        prof = _load_profile(server_id)
        target = known_hosts_dir / f"{server_id}.known_hosts"
        pin_host_key(
            SshIdentity(
                host=prof.ssh.host,
                port=prof.ssh.port,
                user=prof.ssh.user,
                host_key=prof.ssh.host_key,
                credential_ref=prof.ssh.credential_ref,
            ),
            target,
        )
        return target

    host_shell_cap = HostShellCapability(
        profiles=_load_profile,
        known_hosts=_known_hosts_for,
        receipt_root="/tmp/mai-receipts",
        intents_dir=intents_dir,
    )
    host_logs_cap = HostLogsCapability(
        profiles=_load_profile,
        known_hosts=_known_hosts_for,
        artifacts_root=artifacts_dir,
    )
    host_file_read_cap = HostFileReadCapability(
        profiles=_load_profile,
        known_hosts=_known_hosts_for,
    )
    chat_read_cap = ChatReadSessionCapability(sessions)

    class _DynamicEmbedder:
        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            binding = model_store.bind()
            return await GatewayEmbedder(
                binding.connection, resolver=_resolve_env()
            ).embed(texts)

    docs_retrieve_cap = DocumentRetrievalCapability(
        ScopeRetriever(sessions, _DynamicEmbedder())  # type: ignore[arg-type]
    )
    service_http_provider = HttpServiceProvider()

    async def _service_http_cap(call: CapabilityCall) -> ExecutorResult:
        allowlist: dict[str, str] = {}
        raw_allow = os.environ.get("MAI_SERVICE_HTTP_ALLOW", "").strip()
        if raw_allow:
            for item in raw_allow.split(","):
                if "=" in item:
                    k, v = item.split("=", 1)
                    if k.strip() and v.strip():
                        allowlist[k.strip()] = v.strip()
        if not allowlist:
            req_payload = call.payload if isinstance(call.payload, dict) else {}
            req_service = str(req_payload.get("service", ""))
            req_method = str(req_payload.get("method", "GET"))
            req_path = str(req_payload.get("path", "/"))
            import json as _json

            return ExecutorResult(
                ok=False,
                output=_json.dumps(
                    {
                        "status": "refused",
                        "code": "unknown_service",
                        "trust": "untrusted_external",
                        "service": req_service,
                        "method": req_method,
                        "endpoint": req_path,
                        "http_status": None,
                        "json": None,
                        "truncated": False,
                        "redirects": 0,
                        "error_type": None,
                    },
                    sort_keys=True,
                ),
            )
        cap = ServiceHttpCapability(service_http_provider, services=allowlist)
        return await cap(call)

    registry = CapabilityRegistry()
    for desc in (
        host_shell_descriptor(),
        host_logs_descriptor(),
        host_file_read_descriptor(),
        chat_read_descriptor(),
        docs_retrieve_descriptor(),
        service_http_descriptor(),
        upsert_map_descriptor(),
        web_search_descriptor(),
        web_read_descriptor(),
    ):
        registry.register(desc)

    app_manifest_store: ApplicationManifestStore | None = None
    app_adoption_service: ApplicationAdoption | None = None
    app_doc_service: ApplicationDocService | None = None
    service_map_store: ServiceMapStore | None = None
    try:
        app_manifest_store = ApplicationManifestStore(data_root)
        app_adoption_service = ApplicationAdoption(sessions)
    except Exception:
        app_manifest_store = None
        app_adoption_service = None
    try:
        app_doc_service = ApplicationDocService(DocumentStore(data_root))
    except Exception:
        app_doc_service = None
    try:
        service_map_store = ServiceMapStore(data_root / "service_map")
    except Exception:
        service_map_store = None

    # Internal API → Agent Tool surface (C09/C06): every declared internal
    # API operation (service adoption/unadoption, discovery, inventory
    # listing, Service Map CRUD) becomes a worker-registered executor
    # automatically through the ONE shared builder. Fails closed with 503
    # when any service is absent.
    internal_api = build_internal_api_capabilities(
        applications=app_manifest_store,
        adoption=app_adoption_service,
        docs=app_doc_service,
        service_map=service_map_store,
        session_factory=sessions,
        data_root=data_root,
        env_resolver=lambda: _resolve_env(),
    )
    for desc in internal_api.descriptors:
        registry.register(desc)

    # History-tag agent tools (W4D): the SAME composition seam the API
    # process uses (``app.state.internal_api``) — descriptors registered
    # fail-closed at build time, executors merged into the one dispatch
    # map. ``sessions`` is always composed here (the worker owns the
    # database), so the unavailable fallback never applies.
    history_tags = build_history_tag_capabilities(session_factory=sessions)
    for desc in history_tags.descriptors:
        registry.register(desc)

    async def _upsert_map_cap(call: CapabilityCall) -> ExecutorResult:
        if service_map_store is None:
            return ExecutorResult(
                ok=False,
                output='{"status":"unavailable","code":"unavailable","message":"service map store unavailable"}',
            )
        return await ServiceMapUpsertCapability(service_map_store)(call)

    searxng_url = (
        os.environ.get("MAI_SEARXNG_URL") or "http://searxng:8080"
    ).strip()
    web_search_cap = WebSearchCapability(SearXNGProvider(searxng_url))
    web_read_cap = _build_web_read_capability()

    executors = {
        "host.shell": host_shell_cap,
        "host.logs": host_logs_cap,
        "host.file_read": host_file_read_cap,
        "session.read": chat_read_cap,
        "docs.retrieve": docs_retrieve_cap,
        "service.http": _service_http_cap,
        "web.search": web_search_cap,
        "web.read": web_read_cap,
        UPSERT_MAP_CAPABILITY: _upsert_map_cap,
        **internal_api.executors,
        **history_tags.executors,
    }
    plugin_executors: dict[str, object] = {}
    # Capability identifier → owning loaded NativePlugin (built-ins are
    # composed here: the worker executes the host distribution's own
    # plugins). _composite_executor uses it for the per-plugin scoped
    # credential seam (C4) and the dispatch-time enabled recheck.
    native_plugin_owners: dict[str, object] = {}
    mcp_cache: dict[tuple[str, str, tuple[str, ...]], McpServerRuntime] = {}

    def _sync_native_plugin_capabilities() -> None:
        """Register enabled native plugins' descriptors at composition
        time — BEFORE any ``CapabilityToolBridge`` reads this registry.

        Why eager (first-admission exposure): the bridge exposes exactly
        granted ∩ registered and deliberately skips granted-but-absent
        ids ("nothing to expose"); the lazy fallback only ran inside the
        composite executor AFTER a dispatch named the missing capability
        — which a freshly granted, never-exposed plugin can never
        produce. Registering before ``SessionRunExecutor`` construction
        makes a fresh grant visible on the FIRST tool bridge.

        Scope: the native lane only, fully synchronous (entry-point
        scan + module imports, no event loop, no network). MCP servers
        keep their lazy discovery in :func:`_sync_plugin_capabilities`:
        no broad network dialing at worker startup.

        Why a broken optional plugin cannot block unrelated host tools:
        host capabilities are registered BEFORE this lane runs, so a
        plugin failure can only omit that plugin's descriptors, never
        remove or block already-registered host tools; inside discovery
        each plugin loads fail-closed per plugin (a bad installation
        records a rejection and skips itself, not the runtime or its
        siblings); and the lane-level guard below preserves the existing
        best-effort semantics — a store-level composition error degrades
        to the unchanged lazy dispatch fallback instead of aborting
        worker startup. No credential resolution happens here: secret
        values still resolve per actual plugin call in the composite
        executor.
        """
        try:
            native_reg = CapabilityRegistry()
            native_rt = NativePluginRuntime(
                registry=native_reg,
                disabled=plugin_store.disabled_plugins(),
                include_builtins=True,
            )
            native_rt.discover()
            for loaded in native_rt.plugins:
                for desc in native_reg.list_capabilities():
                    if desc.identifier not in loaded.descriptor_names:
                        continue
                    if registry.get(desc.identifier, desc.version) is None:
                        registry.register(desc)
                    plugin_executors[desc.identifier] = native_rt.executor
                    native_plugin_owners[desc.identifier] = loaded
        except Exception:
            pass

    async def _sync_plugin_capabilities() -> None:
        _sync_native_plugin_capabilities()
        try:
            for mcp_cfg in server_plugins(plugin_store):
                granted = granted_capability_ids(plugin_store, mcp_cfg.plugin)
                if not granted:
                    continue
                cache_key = (
                    mcp_cfg.plugin,
                    repr(mcp_cfg),
                    tuple(sorted(granted)),
                )
                cached = mcp_cache.get(cache_key)
                if cached is not None:
                    for cid in cached.tools:
                        plugin_executors[cid] = cached.executor
                    continue
                mcp_reg = CapabilityRegistry()
                mcp_rt = McpServerRuntime(
                    config=mcp_cfg,
                    registry=mcp_reg,
                    credentials=_resolve_env,
                )
                try:
                    await mcp_rt.discover(granted=granted)
                except Exception:
                    continue
                for desc in mcp_reg.list_capabilities():
                    if registry.get(desc.identifier, desc.version) is None:
                        registry.register(desc)
                    plugin_executors[desc.identifier] = mcp_rt.executor
                mcp_cache[cache_key] = mcp_rt
        except Exception:
            pass

    async def _composite_executor(call: CapabilityCall) -> ExecutorResult:
        import dataclasses

        cid = call.descriptor.identifier
        target = executors.get(cid) or plugin_executors.get(cid)
        if target is None:
            await _sync_plugin_capabilities()
            target = executors.get(cid) or plugin_executors.get(cid)
        if target is None or not callable(target):
            return ExecutorResult(
                ok=False,
                output=f"capability {cid!r} has no runtime executor",
            )
        if call.context.server_id != uuid.UUID(int=0):
            try:
                prof = _load_profile(call.context.server_id)
                ref = prof.ssh.credential_ref
                if ref and ref not in call.credentials:
                    secret = _resolve_env().resolve(ref)
                    call = dataclasses.replace(
                        call,
                        credentials={**call.credentials, ref: secret},
                    )
            except Exception:
                pass
        if cid.startswith("mcp."):
            parts = cid.split(".")
            if len(parts) >= 3:
                plugin_slug = ".".join(parts[1:-1])
                try:
                    stored_doc = plugin_store.read_config(plugin_slug)
                    doc = parse_plugin_config_text(stored_doc.text)
                    extra_creds = dict(call.credentials)
                    env_res = _resolve_env()
                    for ref in doc.credential_refs:
                        if ref not in extra_creds:
                            extra_creds[ref] = env_res.resolve(ref)
                    if len(extra_creds) != len(call.credentials):
                        call = dataclasses.replace(
                            call, credentials=extra_creds
                        )
                except Exception:
                    pass
        owner = native_plugin_owners.get(cid)
        if owner is not None:
            # Scoped per-plugin credential seam (C4/H2/M7): the plugin's
            # declared CREDENTIAL_REFERENCES plus its configured
            # credential_refs resolve here, on demand, ONLY when one of
            # the plugin's own capabilities dispatches — a missing plugin
            # secret can never break host.shell/web.search/etc. Unlike
            # the MCP lane above, a missing required secret fails THIS
            # call with a structured answer instead of degrading to the
            # handler without credentials.
            import json as _json

            try:
                stored_doc = plugin_store.read_config(owner.plugin)
                doc = parse_plugin_config_text(stored_doc.text)
            except Exception:
                doc = None
            if doc is not None and not doc.enabled:
                return ExecutorResult(
                    ok=False,
                    error_code="plugin_disabled",
                    output=_json.dumps(
                        {
                            "status": "error",
                            "code": "plugin_disabled",
                            "detail": (
                                f"native plugin {owner.plugin!r} is "
                                "disabled in the stored configuration"
                            ),
                        }
                    ),
                )
            refs: list[str] = list(owner.credential_references)
            if doc is not None:
                for ref in doc.credential_refs:
                    if ref not in refs:
                        refs.append(ref)
            unresolved = [ref for ref in refs if ref not in call.credentials]
            if unresolved:
                try:
                    env_res = _resolve_env()
                    resolved = {ref: env_res.resolve(ref) for ref in unresolved}
                except Exception as error:
                    return ExecutorResult(
                        ok=False,
                        error_code="credential_resolution_failed",
                        output=_json.dumps(
                            {
                                "status": "error",
                                "code": "credential_resolution_failed",
                                "detail": (
                                    f"native plugin {owner.plugin!r} "
                                    f"credential {unresolved[0]!r} did "
                                    f"not resolve ({type(error).__name__})"
                                ),
                            }
                        ),
                    )
                call = dataclasses.replace(
                    call,
                    credentials={**call.credentials, **resolved},
                )
        return await target(call)  # type: ignore[misc]

    def _read_grants():
        grants: dict[str, set[uuid.UUID]] = {}
        enrolled_servers: set[uuid.UUID] = {uuid.UUID(int=0)}
        try:
            stored_cfg = grant_store.read()
            for cid, sids in stored_cfg.grants.items():
                grants.setdefault(cid, set()).update(sids)
        except Exception:
            pass
        # Also include capabilities declared directly on each server's profile.yaml
        try:
            servers_dir = data_root / "servers"
            if servers_dir.is_dir():
                for child in servers_dir.iterdir():
                    if not child.is_dir():
                        continue
                    try:
                        sid = uuid.UUID(child.name)
                        enrolled_servers.add(sid)
                        prof = _load_profile(sid)
                        for cap_slug in prof.capabilities:
                            normalized = cap_slug.replace("_", ".")
                            grants.setdefault(cap_slug, set()).add(sid)
                            grants.setdefault(normalized, set()).add(sid)
                        # Always allow host.shell and host.logs if host_shell / journald_logs present
                        if "host_shell" in prof.capabilities or "host.shell" in prof.capabilities:
                            grants.setdefault("host.shell", set()).add(sid)
                        if "journald_logs" in prof.capabilities or "host.logs" in prof.capabilities:
                            grants.setdefault("host.logs", set()).add(sid)
                        if "file_read" in prof.capabilities or "host.file_read" in prof.capabilities:
                            grants.setdefault("host.file_read", set()).add(sid)
                    except Exception:
                        continue
        except Exception:
            pass
        # Include plugin bindings (both per-server and global 00000000-0000-0000-0000-000000000000).
        try:
            for plugin_name in plugin_store.list_plugins():
                try:
                    stored_doc = plugin_store.read_config(plugin_name)
                    doc = parse_plugin_config_text(stored_doc.text)
                except Exception:
                    continue
                if not doc.enabled:
                    continue
                for binding in plugin_store.list_bindings(plugin_name):
                    target_servers = (
                        enrolled_servers
                        if binding.server_id == uuid.UUID(int=0)
                        else {binding.server_id}
                    )
                    for cap_id in binding.capabilities:
                        grants.setdefault(cap_id, set()).update(target_servers)
        except Exception:
            pass
        # Automatically grant internal API capabilities (app.discover,
        # app.list_adopted, app.adopt, app.unadopt, app.upsert_map_entry,
        # app.delete_map_entry, session.read), the history-tag
        # tools (tags.*: run-identity scoped) and controller-side web
        # research capabilities (web.search, web.read) to every enrolled
        # server and General Consult so they work out of the box.
        for cap_id in (
            *INTERNAL_API_CAPABILITY_IDS,
            *TAGS_CAPABILITY_IDS,
            "web.search",
            "web.read",
        ):
            grants.setdefault(cap_id, set()).update(enrolled_servers)
        return {cid: frozenset(sids) for cid, sids in grants.items()}

    def _read_policy() -> Policy:
        try:
            stored_cfg = grant_store.read()
            return stored_cfg.policy
        except Exception:
            return Policy(revision=1, denied_capabilities=frozenset())

    def _is_adopted(server_id: uuid.UUID) -> bool:
        if server_id == uuid.UUID(int=0):
            return True
        try:
            profile_store.read(server_id)
            return True
        except Exception:
            return False

    def _resolve_env() -> EnvResolver:
        primary = env_file if env_file and Path(env_file).is_file() else None
        resolved = build_runtime_env_resolver(primary, data_root)
        if resolved is not None:
            return resolved
        fallback = data_root / ".env"
        if not fallback.is_file():
            fallback.write_text("", encoding="utf-8")
            fallback.chmod(0o600)
        return EnvResolver(fallback)

    # First-admission plugin exposure: the registry MUST already hold
    # the enabled native plugins' descriptors when the dispatcher /
    # SessionRunExecutor (and thus the first CapabilityToolBridge) is
    # composed. Synchronous, no event loop, no network — only the
    # native lane runs here; MCP discovery stays lazy inside
    # _composite_executor, which keeps its dispatch-time fallback.
    _sync_native_plugin_capabilities()
    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(sessions),
        grants=_read_grants,
        policy=_read_policy,
        adopted=_is_adopted,
        credentials=_resolve_env,
        executor=_composite_executor,
    )
    model_store = ModelConfigStore(data_root)
    # The single-loop session runner (D04/D05, COORD-D02-01): the ONLY
    # agent runtime after the D09 deletion — ``agent.run``/``agent.run_v2``/
    # ``agent.delegate`` are gone. It composes the production dispatcher,
    # registry, model store and resource locks exactly as before.
    try:
        from mai.agents import session_runner as agent_session_runner

        agent_session_runner.configure_session_run(
            agent_session_runner.SessionRunExecutor(
                sessions=sessions,
                dispatcher=dispatcher,
                registry=registry,
                model_store=model_store,
                data_root=data_root,
                locks=ResourceLockService(database_url),
                resolver=_resolve_env,
                # The production default model builder (models.build_model)
                # applies; protocol fixtures inject their own builder via
                # the handler-module composition.
                model_builder=None,
            )
        )
    except agent_session_runner.SessionRunError as exc:
        # Idempotent wiring: the handler module may already have composed
        # the session runner in this process.
        if exc.reason != "already_configured":
            raise


def main(argv: Sequence[str] | None = None) -> int:
    """Run one worker process until ``SIGTERM``/``SIGINT``."""
    parser = argparse.ArgumentParser(
        prog="python -m mai.jobs.worker",
        description="Mai durable-job worker (DBOS engine, C05).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=engine.DEFAULT_QUEUE_CONCURRENCY,
        help=f"per-worker queue concurrency (default {engine.DEFAULT_QUEUE_CONCURRENCY})",
    )
    ns = parser.parse_args(argv)

    try:
        database_url = resolve_database_url()
    except Exception as exc:  # noqa: BLE001 - value-free config failure
        print(
            f"mai worker: MAI_DATABASE_URL is not usable: {type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    handler_spec = os.environ.get("MAI_WORKER_HANDLERS", "")
    rc = _import_handlers(handler_spec)
    if rc != 0:
        return rc

    try:
        _configure_default_agent_runtime(database_url)
    except Exception as exc:  # noqa: BLE001 - log non-fatal runtime init warning
        print(
            f"mai worker: agent runtime init warning: {type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    try:
        engine.launch_worker(database_url, queue_concurrency=ns.concurrency)
    except Exception as exc:  # noqa: BLE001 - value-free startup failure
        print(
            f"mai worker: engine launch failed: {type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    print("mai worker: ready", file=sys.stderr, flush=True)
    _STOP.wait()
    engine.shutdown()
    print("mai worker: stopped", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
