"""MAI API application: ASGI entrypoint, ``/api/v1`` prefix, truthful health.

Ownership map (contracts.md C01/C08; auth ADR ``3e79449`` §8 G1; SERVICES
``mai-api`` row: "unhealthy dependencies reported accurately"):

- :func:`create_app` is the single ASGI application factory (uvicorn factory
  form: ``uvicorn mai.api.app:create_app --factory``). The lifespan starts
  the one app-side engine owned by :mod:`mai.database` and disposes it at
  shutdown. Database unavailability never blocks startup or liveness: the
  app serves, readiness reports the truth. The durable DBOS engine is
  launched later by its owning adapter card (C05), never here.
- ``GET /api/v1/health`` is liveness: it never touches a dependency, so it
  answers while PostgreSQL is down. G1 (auth ADR §8) serves it
  unauthenticated in setup state; it grants no privilege.
- ``GET /api/v1/health/ready`` is readiness: one classified probe through
  :func:`mai.database.check_database` → ``200`` when dependencies are ok,
  ``503`` carrying the stable health class otherwise. The response body
  never contains the URL, credentials, or server error text (C01: no secret
  material in any error body).
- Every error response uses the C01 structured envelope
  ``{"error": {"code", "message", "request_id", "fields"}}``; unhandled
  exceptions collapse to a generic ``internal_error`` with no traceback.

The health GET routes stay unauthenticated in setup state (auth ADR §8 G1).
The request security boundary (``mai.api.auth``: bootstrap/login/logout/csrf,
cookie/origin/CSRF layers, §5.3 throttling) is registered by
:func:`create_app` via :func:`mai.api.auth.register_auth`; later privileged
routers join under the same ``/api/v1`` prefix through the same boundary —
no route may bypass that prefix or the envelope. Public docs/OpenAPI
listeners stay off (G1 minimal unauthenticated surface); the schema is
produced in-process via ``app.openapi()`` for the central exporter owned by
the ``openapi-client`` resource.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import State
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.agents.model_store import ModelConfigStore
from mai.agents.models import (
    SCHEMA_VERSION as CONNECTION_SCHEMA_VERSION,
    ModelConfigError,
    parse_connection,
)
from mai.api.approvals import register_approvals
from mai.api.applications import register_applications
from mai.api.agent_sessions import register_agent_sessions
from mai.session_channel import SessionChannelHub
from mai.api.auth import (
    bootstrap_password_from_env,
    effective_client_ip,
    forwarded_host_spoofed,
    register_auth,
    render_error_envelope,
    resolve_public_origins,
    resolve_trusted_proxies,
)
from mai.api.chat import register_chat
from mai.api.conversations import register_conversations
from mai.api.documents import (
    EMBEDDINGS_BASE_URL_VAR,
    EMBEDDINGS_CREDENTIAL_REF_VAR,
    KnowledgeServices,
    register_documents,
)
from mai.api.grants import register_grants
from mai.api.history_tags import register_history_tags
from mai.api.jobs import register_jobs
from mai.api.knowledge_models import (
    MEMORY_BASE_URL_VAR,
    MEMORY_SHARED_BANKS_VAR,
    MemoryServices,
    register_knowledge_models,
)
from mai.api.models import register_models
from mai.api.monitoring import register_monitoring
from mai.api.objectives import register_objectives
from mai.api.password_challenges import (
    lane_approval_store,
    register_password_challenges,
)
from mai.api.plugins import register_plugins
from mai.jobs.lifecycle import RunLifecycle
from mai.jobs.recovery import RecoveryService
from mai.jobs.schedules import ScheduleManager
from mai.api.servers import register_servers, resolve_profiles_root
from mai.api.schedules import register_schedules
from mai.api.tasks import register_tasks
from mai.api.terminal import register_terminal
from mai.memory.hindsight import (
    HindsightProvider,
    MemoryAdapterError,
    parse_shared_banks,
)
from mai.capabilities.internal_api import (
    InternalApiComposition,
    build_internal_api_capabilities,
)
from mai.capabilities.history_tags import build_history_tag_capabilities
from mai.capabilities.task_state import build_task_state_capabilities
from mai.applications.adopt import (
    ApplicationAdoption,
    ApplicationManifestStore,
)
from mai.applications.document import ApplicationDocService
from mai.applications.service_map import ServiceMapStore
from mai.skills.builtin import seed_builtin_skills
from mai.auth.sessions import AuthError, SessionStore
from mai.capabilities.dispatch import CapabilityDispatcher
from mai.credentials.env import (
    EnvFileError,
    build_runtime_env_resolver,
)
from mai.database import (
    DatabaseConfigError,
    DatabaseHealth,
    DatabaseStatus,
    check_database,
    create_app_engine,
    resolve_database_url,
)
from mai.knowledge.documents import DocumentStore
from mai.knowledge.generate import DocGenerator
from mai.knowledge.index import GatewayEmbedder, ScopeIndexer
from mai.knowledge.retrieve import (
    DocumentRetrievalCapability,
    ScopeRetriever,
    descriptor as retrieval_descriptor,
)
from mai.plugins.config import PluginConfigStore
from mai.plugins.registry import CapabilityRegistry
from mai.policy.approvals import ApprovalStore
from mai.policy.config import GrantConfigStore
from mai.profiles.files import ProfileFileError, ProfileFileStore
from mai.profiles.projection import ProfileProjection
from mai.research.workflow import ResearchWorkflow

API_PREFIX = "/api/v1"
HEALTH_PATH = f"{API_PREFIX}/health"
READINESS_PATH = f"{API_PREFIX}/health/ready"


async def _database_state(state: State) -> DatabaseStatus:
    """Classified dependency truth for readiness; never raises."""
    config_error = getattr(state, "database_config_error", None)
    if isinstance(config_error, DatabaseHealth):
        return DatabaseStatus(
            config_error, "configured database URL was rejected at startup"
        )
    return await check_database(getattr(state, "database_url", None))


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start/stop the app-side database engine (the P01 dependency set).

    A missing or rejected ``MAI_DATABASE_URL`` is recorded and reported by
    readiness (``not_configured``/``invalid_config``); it does not prevent
    the app from serving liveness and truthful readiness.
    """
    # Server-side credential resolution for the passworded transport lane
    # (C03 rule 6): the configured ``.env`` source (``MAI_ENV_FILE``) is
    # loaded ONCE here — the challenge surface (mai.api.password_challenges)
    # resolves transport paths itself, and the wire never names one. An
    # absent or unusable source fails closed: the surface answers 503
    # unavailable and touches no state.
    app.state.credentials = None
    source = (app.state.env or os.environ).get("MAI_ENV_FILE")
    try:
        data_root_for_creds = resolve_profiles_root(app.state.env)
    except OSError:
        data_root_for_creds = None
    if source or data_root_for_creds is not None:
        try:
            app.state.credentials = build_runtime_env_resolver(
                source, data_root_for_creds
            )
        except EnvFileError:
            app.state.credentials = None
    try:
        url = resolve_database_url(app.state.env)
    except DatabaseConfigError as exc:
        app.state.database_url = None
        app.state.database_config_error = exc.health
    else:
        app.state.database_url = url
        app.state.database_config_error = None
        app.state.engine, app.state.session_factory = create_app_engine(url)
        app.state.sessions = SessionStore(app.state.session_factory)
        # Approval decisions (C03 rule 4): the payload-bound one-use store
        # over the one app engine; the HTTP surface (mai.api.approvals)
        # fails closed with 503 when this is absent.
        app.state.approvals = ApprovalStore(app.state.session_factory)
        # Passworded transport lane (mai.api.password_challenges): the
        # bounded blocking ssh lane must run OFF the request event loop
        # (the password channel's composition duty), so its approval gate
        # gets a dedicated no-pool store — a NullPool connection lives on
        # exactly one event loop, and nothing pools across loops.
        app.state.password_approvals, app.state.password_lane_engine = (
            lane_approval_store(url)
        )
        # Canonical profile service (C02): the confined file store over the
        # explicitly configured data root plus the journal worker over the
        # one app engine. A store that cannot be constructed (unusable
        # root) fails closed: routes answer 503 unavailable and touch no
        # state; the value-free marker is all readiness needs.
        app.state.profiles = None
        app.state.profiles_error = None
        app.state.projection = ProfileProjection(app.state.session_factory)
        try:
            app.state.profiles = ProfileFileStore(resolve_profiles_root(app.state.env))
        except OSError, ProfileFileError:
            app.state.profiles_error = "profiles_root_unusable"
        # Capability-grant configuration (C03): the confined store over the
        # same explicitly configured data root (module-fixed policy/
        # namespace); the HTTP surface (mai.api.grants) fails closed with
        # 503 when this is absent. An unusable root is recorded and
        # reported, never a startup crash.
        app.state.grants = None
        app.state.grants_error = None
        try:
            app.state.grants = GrantConfigStore(resolve_profiles_root(app.state.env))
        except OSError:
            app.state.grants_error = "policy_root_unusable"
        # Documents surface (C02/C07): the canonical doc store over the
        # same explicit data root; the retrieval/reconcile engine over the
        # one app engine plus the explicitly configured embeddings
        # endpoint (never discovered); the deep-research composition over
        # the docs.retrieve channel through the ONE dispatcher boundary
        # with current grants/policy/adoption from app state. Every part
        # fails closed: routes answer 503 and touch no state.
        app.state.knowledge = None
        doc_store: DocumentStore | None = None
        try:
            doc_store = DocumentStore(resolve_profiles_root(app.state.env))
        except OSError:
            doc_store = None
        if doc_store is not None:
            env = app.state.env or os.environ
            retriever = None
            indexer = None
            workflow = None
            base_url = env.get(EMBEDDINGS_BASE_URL_VAR)
            if base_url and app.state.session_factory is not None:
                try:
                    connection = parse_connection(
                        {
                            "schema_version": CONNECTION_SCHEMA_VERSION,
                            "name": "docs-api-embeddings",
                            "base_url": base_url,
                            "credential_ref": env.get(
                                EMBEDDINGS_CREDENTIAL_REF_VAR
                            )
                            or None,
                        }
                    )
                    embedder = GatewayEmbedder(connection, app.state.credentials)
                except ModelConfigError:
                    embedder = None
                if embedder is not None:
                    retriever = ScopeRetriever(app.state.session_factory, embedder)
                    indexer = ScopeIndexer(
                        app.state.session_factory, doc_store, embedder
                    )
            profiles_store = app.state.profiles
            grants_store = app.state.grants
            if (
                retriever is not None
                and app.state.approvals is not None
                and grants_store is not None
                and profiles_store is not None
            ):
                registry = CapabilityRegistry()
                registry.register(retrieval_descriptor())

                def adopted_probe(server_id: object) -> bool:
                    try:
                        profiles_store.read(server_id)
                    except Exception:  # noqa: BLE001 - boolean probe only
                        return False
                    return True

                dispatcher = CapabilityDispatcher(
                    registry=registry,
                    approvals=app.state.approvals,
                    grants=lambda: grants_store.read().grants,
                    policy=lambda: grants_store.read().policy,
                    adopted=adopted_probe,
                    credentials=lambda: app.state.credentials,
                    executor=DocumentRetrievalCapability(retriever),
                )
                workflow = ResearchWorkflow(
                    dispatcher,
                    data_root=resolve_profiles_root(app.state.env),
                    generator=DocGenerator(doc_store),
                )
            app.state.knowledge = KnowledgeServices(
                store=doc_store,
                retriever=retriever,
                indexer=indexer,
                workflow=workflow,
            )
        # Applications surface (C09): the confined canonical manifest store
        # and the journaled adoption worker over the SAME explicit data
        # root and the ONE app engine; the app-docs maintainer over the
        # canonical doc store (present whenever it is). An unusable root
        # fails closed: routes answer 503 unavailable and touch no state.
        app.state.applications = None
        app.state.app_adoption = None
        app.state.app_docs = None
        try:
            app.state.applications = ApplicationManifestStore(
                resolve_profiles_root(app.state.env)
            )
        except OSError:
            app.state.applications = None
        if app.state.session_factory is not None and (
            app.state.applications is not None
        ):
            app.state.app_adoption = ApplicationAdoption(
                app.state.session_factory
            )
        if doc_store is not None:
            app.state.app_docs = ApplicationDocService(doc_store)
        # Service Map surface (mechanicus-ui 02): the confined category
        # taxonomy + per-service map store (category/description/runbook)
        # over the SAME explicit data root (``<root>/service_map/``); the
        # applications + chat surfaces fail closed with 503 when absent.
        app.state.service_map = None
        try:
            app.state.service_map = ServiceMapStore(
                os.path.join(resolve_profiles_root(app.state.env), "service_map")
            )
        except OSError:
            app.state.service_map = None
        # Internal API → Agent Tool surface (C09/C06): every declared
        # internal API operation (service adoption/unadoption, discovery,
        # inventory listing, Service Map CRUD) becomes a chat-agent tool
        # automatically through the ONE shared builder. Fails closed with
        # 503 when any service is absent.
        app.state.internal_api = build_internal_api_capabilities(
            applications=app.state.applications,
            adoption=app.state.app_adoption,
            docs=app.state.app_docs,
            service_map=app.state.service_map,
            session_factory=app.state.session_factory,
            data_root=resolve_profiles_root(app.state.env),
            env_resolver=lambda: app.state.credentials,
        )
        # Task-state tools (act 14-task-tools, act_CONTRACTS §7): the
        # bounded read/proposal/item-update surface over the ONE task
        # service, registered through the SAME composition seam so every
        # consumer of ``app.state.internal_api`` sees one
        # descriptor→executor surface. Descriptors are validated
        # fail-closed at composition time; a missing database composition
        # (``session_factory=None``) yields executors that answer
        # unavailable.
        _task_state = build_task_state_capabilities(
            session_factory=app.state.session_factory,
        )
        # History-tag tools (W4D): the SAME composition seam — the
        # ``tags.*`` surface joins the one ``app.state.internal_api``
        # descriptor→executor mapping alongside the internal-API and
        # task-state tools, so every consumer sees one surface.
        _history_tags = build_history_tag_capabilities(
            session_factory=app.state.session_factory,
        )
        app.state.internal_api = InternalApiComposition(
            descriptors=(
                *app.state.internal_api.descriptors,
                *_task_state.descriptors,
                *_history_tags.descriptors,
            ),
            executors={
                **app.state.internal_api.executors,
                **_task_state.executors,
                **_history_tags.executors,
            },
        )
        # Model configuration store (C03 rule 6/C06 rule 1): the confined
        # atomic store over the same explicitly configured data root (the
        # store appends its own ``models/`` namespace). Credential material
        # is never held here — the resolver below is the only path that
        # resolves references, at call time. An unusable root fails closed:
        # routes answer 503 unavailable and touch no state.
        app.state.models = None
        app.state.models_error = None
        try:
            app.state.models = ModelConfigStore(resolve_profiles_root(app.state.env))
        except OSError:
            app.state.models_error = "models_root_unusable"
        # Plugin configuration store (C02/C03): the confined atomic store
        # over the same explicitly configured data root (the store appends
        # its own ``plugins/`` namespace) plus the skills namespace root
        # (``shared/skills``, loader-owned format). Credential material is
        # never held here — the wire carries reference names only (C03
        # rule 6). Unusable roots fail closed: routes answer 503
        # unavailable and touch no state.
        app.state.plugins = None
        app.state.plugins_error = None
        try:
            app.state.plugins = PluginConfigStore(
                resolve_profiles_root(app.state.env)
            )
        except OSError:
            app.state.plugins_error = "plugins_root_unusable"
        app.state.skills_root = None
        app.state.skills_error = None
        try:
            app.state.skills_root = resolve_profiles_root(app.state.env)
            if (app.state.env or os.environ).get("MAI_SEED_BUILTIN_SKILLS") == "1":
                seed_builtin_skills(app.state.skills_root)
        except OSError:
            app.state.skills_error = "skills_root_unusable"
        # Memory surface (C07 rule 6): the bank-scoped Hindsight provider
        # over the explicitly configured compose-internal service URL —
        # never discovered (the embeddings-endpoint precedent). An absent
        # or unusable configuration fails closed: routes answer 503 and
        # touch no state. Shared banks are the explicit validated
        # allowlist; empty means the deployment exposes none.
        app.state.memory = None
        memory_env = app.state.env or os.environ
        memory_url = memory_env.get(MEMORY_BASE_URL_VAR)
        if memory_url:
            try:
                app.state.memory = MemoryServices(
                    provider=HindsightProvider(memory_url),
                    shared_banks=parse_shared_banks(
                        [
                            name.strip()
                            for name in memory_env.get(
                                MEMORY_SHARED_BANKS_VAR, ""
                            ).split(",")
                            if name.strip()
                        ]
                    ),
                )
            except MemoryAdapterError:
                app.state.memory = None
        # Jobs domain surfaces (C05/C08): the facade lifecycle replay
        # producer plus the safe-cancel facade over the ONE engine client.
        # The durable engine itself is launched by the worker (never here):
        # the EngineClient inside RecoveryService opens lazily on first
        # use, so an API process that never submits never touches the
        # engine-owned ``dbos`` schema. Both fail closed with 503 when
        # absent (mai.api.jobs).
        app.state.job_lifecycle = RunLifecycle(app.state.session_factory)
        app.state.job_recovery = RecoveryService(url, app.state.session_factory)
        # Session channel hub (D06): the ONE shared LISTEN/NOTIFY listener
        # + bounded per-session fanout over the ONE app engine. The reader
        # is the D02 bounded history reader (after_seq catch-up pages).
        from functools import partial

        from mai.session_store import aread_session_history

        _factory = app.state.session_factory
        _tick_raw = (app.state.env or os.environ).get(
            "MAI_SESSION_CHANNEL_TICK_SECONDS"
        )
        try:
            _tick = float(_tick_raw) if _tick_raw is not None else 5.0
        except ValueError:
            _tick = 5.0
        _tick = min(max(_tick, 0.05), 60.0)
        app.state.session_channel = SessionChannelHub(
            entry_reader=partial(aread_session_history, _factory),
            coarse_tick_seconds=_tick,
        )
        app.state.session_channel.start(app.state.engine)
        # Schedules surface (C05 rule 8): the control-plane facade over
        # the ONE engine URL — a bare DBOSClient, never a launched engine
        # (launching here would steal pending workflows via launch
        # recovery). Fails closed with 503 when absent (mai.api.schedules).
        app.state.schedules = ScheduleManager(url, app.state.session_factory)
        # One-shot env bootstrap (auth ADR §3): ``MAI_ADMIN_PASSWORD_FILE``
        # first, then ``MAI_ADMIN_PASSWORD``; ignored once a credential
        # record exists (bootstrap-only, never a runtime override). The
        # plaintext is dropped right after the store's hashing call and is
        # never logged. An unusable source simply leaves setup state, where
        # ``POST /api/v1/auth/bootstrap`` is the frozen recovery path.
        password = bootstrap_password_from_env(app.state.env)
        if password is not None:
            try:
                if not await app.state.sessions.credential_exists():
                    await app.state.sessions.bootstrap_credential(password)
            except AuthError:
                pass  # a concurrent startup won the one-shot insert
            finally:
                del password  # not retained beyond the hashing call
    try:
        yield
    finally:
        # The schedules facade owns a DBOSClient (its own system-DB pool)
        # and the cancel facade owns an EngineClient: release both before
        # the app engine goes away. The session-channel hub closes its
        # dedicated LISTEN connection first.
        hub = getattr(app.state, "session_channel", None)
        if hub is not None:
            await hub.stop()
        schedules = getattr(app.state, "schedules", None)
        if schedules is not None:
            schedules.close()
        recovery = getattr(app.state, "job_recovery", None)
        if recovery is not None:
            recovery.close()
        engine = getattr(app.state, "engine", None)
        if engine is not None:
            await engine.dispose()
        lane_engine = getattr(app.state, "password_lane_engine", None)
        if lane_engine is not None:
            await lane_engine.dispose()


def create_app(env: Mapping[str, str] | None = None) -> FastAPI:
    """Build the MAI ASGI application.

    ``env`` overrides the environment for database URL resolution (test
    seam, mirroring :func:`mai.database.resolve_database_url`); ``None``
    reads the process environment.
    """
    app = FastAPI(
        title="MAI controller API",
        version="0.1.0",
        lifespan=_lifespan,
        # G1 minimal unauthenticated surface: no public docs/OpenAPI listeners.
        # The schema is still generated in-process (app.openapi()).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.env = env

    # Request-boundary configuration (auth ADR §6.3/§7): exact origin
    # allowlist and trusted proxies. Unusable configuration fails closed —
    # an empty allowlist rejects every mutation, an empty proxy list ignores
    # forwarded metadata everywhere.
    try:
        app.state.public_origins = resolve_public_origins(env)
    except ValueError:
        app.state.public_origins = frozenset()
    try:
        app.state.trusted_proxies = resolve_trusted_proxies(env)
    except ValueError:
        app.state.trusted_proxies = ()
    register_auth(app)
    # Privileged routers join the same /api/v1 prefix and boundary (C01/C08):
    # the canonical server-profile surface (C02), the approval decision
    # surface (C03 rule 4), the capability-grant configuration surface
    # (C03 rules 3–5) and the jobs submit/status/cancel/event surface
    # (C05/C08 — HTTP persists intent only; effects run in the worker).
    register_servers(app)
    register_approvals(app)
    # The password challenge surface (C03 rule 4 + C04 rule 2): the
    # ephemeral submit-only secret transport — no read endpoint exists.
    register_password_challenges(app)
    register_grants(app)
    register_jobs(app)
    # The scheduled-job configuration surface (C05 rule 8): durable
    # schedule records over the reviewed facade — HTTP persists
    # configuration only, firing stays engine/worker-owned.
    register_schedules(app)
    # The chat conversation surface (C06): durable agent-conversation
    # submit/detail/cancel/event routes over the reviewed agent-run and
    # delegation facade — HTTP persists intent only; model rounds, tool
    # calls and child workflows run in the worker.
    register_chat(app)
    # The canonical conversation history surface (act_CONTRACTS §3/§4):
    # bounded, explicitly server-scoped reads over the app-owned
    # conversation store — the paginated list/detail/transcript pages and
    # the cursor-deduplicated metadata event page, with honest legacy-gap
    # labels (partial/unavailable) instead of invented turns. Read-only:
    # writes stay on the reviewed chat submit/steer seam.
    register_conversations(app)
    # The runtime-v2 objective surface (runtime-v2.md §3/§8/§10/§11):
    # explicit operator-owned objective episodes over the ONE v2 stores —
    # creation, scope CAS (operator authority only), journal/checklist
    # projections, the bounded ``objective.*`` ledger pages, the
    # cursor-exclusive activity SSE and the legacy drain inventory.
    register_objectives(app)
    # The history-tag management surface (history-tags HT-W6B): the
    # global tag catalog CRUD plus same-server job-run and canonical
    # conversation tag assignments over the ONE tag store — idempotent
    # user-sourced writes, the used/unused deletion policy and the
    # reserved unclassified semantics preserved verbatim; the legacy
    # chat list stays unfiltered (owned by the chat surface's own slice).
    register_history_tags(app)
    # The task read/correction/decision surface (act_CONTRACTS §6/§8): the
    # compact header view for the conversation's ONE active episode, the
    # explicitly server-scoped paginated items/constraints/evidence/
    # revisions pages, the authenticated user correction route (ONE shared
    # revision service call: CAS + idempotency + source-linked,
    # author=user) and the distinct decision route (advisory phase
    # selection, pause/cancel) — operation approvals stay on the C03
    # approvals surface; metadata decisions never authorize or terminate
    # target operations.
    register_tasks(app)
    # The model configuration/selection/probe surface (C03 rule 6/C06
    # rule 1): connection/profile/selection records over the confined
    # atomic store — credential references only, never resolved material
    # — plus the bounded classified probe. The durable model rounds of
    # conversations stay worker-owned; the probe is a single bounded
    # diagnostic round owned by this surface.
    register_models(app)
    # The memory surface (C07): the session-derived, grant-gated
    # Hindsight memory operations (status/recall/retain/reflect/
    # export/delete — bank scope derived from the context, never a
    # client-supplied name). No memory content or target effect ever
    # bypasses the producer.
    register_knowledge_models(app)
    # The canonical documents surface (C02/C07): docs/runbook CRUD with
    # provenance and revision checks, hybrid cited retrieval, explicit
    # index status/reconcile, run-evidence generation and the bounded
    # server-scoped deep-research pass — all over the reviewed knowledge
    # producers; HTTP composes services, effects stay in the producers.
    register_documents(app)
    # The applications surface (C09): discovery/adopted listings kept
    # separate (C09 rule 1), the explicit adoption/revoke writes over the
    # journaled canonical producers with the app-docs identity record, the
    # pure C09 rule 3 plan preview, and the install/update/remove intent
    # submits through the EXISTING jobs facade — no Docker/SSH effect ever
    # runs in a route; the data-removal decision is an explicit field.
    register_applications(app)
    # Internal API → Agent Tool surface (C09/C06): every declared internal
    # API operation (service adoption/unadoption, discovery, inventory
    # listing, Service Map CRUD) becomes a chat-agent tool automatically
    # through the ONE shared builder. The descriptors and executors are
    # composed at lifespan time (app.state.internal_api) and consumed by
    # both the HTTP dispatcher and the DBOS worker without drift.
    # The plugin configuration/enablement surface (C03): plugin catalogs
    # with availability separate from grants, versioned connection-config
    # edits validated against the declared schemas (credential references
    # only, never values), explicit server grant bindings, the bounded
    # MCP discovery diagnostic, and read-only skill inspection — all over
    # the reviewed plugin-store/native/mcp/skills producers. No
    # installation endpoint exists by design (C03 rule 7).
    register_plugins(app)
    # The monitoring surface (C10 rules 3/4): bounded server-scoped
    # observation/finding queries with derived freshness, the
    # evidence-preserving notification ack, the findings re-evaluation
    # through the reviewed notifications producer and the effective
    # routing-policy view — all over the 0007 schema; no collection or
    # delivery effect ever runs in a route.
    register_monitoring(app)
    # The interactive terminal surface (u2-terminal): session+origin
    # authenticated WebSocket attaching one system-OpenSSH PTY (``ssh -tt``,
    # remote tmux/login shell) plus its session-authenticated info view. No
    # third-party SSH library — the pinned transport is mai.execution.ssh.
    register_terminal(app)
    # The session-first surface (D06): REST collection/detail/admission/
    # history/control + the versioned cursor WebSocket channel (ADDITIVE —
    # /api/v1/chat/** stays until the D09 coordinated deletion).
    register_agent_sessions(app)

    @app.middleware("http")
    async def attach_request_boundary(request: Request, call_next):  # noqa: ANN202
        request.state.request_id = uuid.uuid4().hex
        if forwarded_host_spoofed(request):
            # Auth ADR §6.3/S4: a trusted peer's forwarded metadata must
            # agree with Host; disagreement is an origin-spoof marker.
            response = render_error_envelope(
                request,
                HTTP_400_BAD_REQUEST,
                "request_rejected",
                "forwarded host does not match request host",
            )
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        request.state.client_ip = effective_client_ip(request)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.get(HEALTH_PATH, tags=["health"], summary="Liveness")
    async def liveness() -> dict[str, str]:
        """Process liveness; never checks dependencies (auth ADR §8 G1)."""
        return {"status": "ok"}

    @app.get(
        READINESS_PATH,
        tags=["health"],
        summary="Readiness",
        responses={
            HTTP_503_SERVICE_UNAVAILABLE: {
                "description": "At least one dependency is unhealthy"
            }
        },
    )
    async def readiness(request: Request) -> JSONResponse:
        """Classified, secret-free dependency readiness; never raises."""
        status = await _database_state(request.app.state)
        body = {
            "status": "ok" if status.ok else "unhealthy",
            "database": str(status.health),
        }
        return JSONResponse(
            status_code=HTTP_200_OK if status.ok else HTTP_503_SERVICE_UNAVAILABLE,
            content=body,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # C01: field paths only — the submitted values are never echoed.
        fields = {
            ".".join(str(part) for part in error.get("loc", ())) or "request": "invalid"
            for error in exc.errors()[:20]
        }
        return render_error_envelope(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "request body failed validation",
            fields=fields,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # Stable, browser-safe messages only; exception payloads are never
        # echoed.
        messages = {
            404: ("not_found", "resource not found"),
            405: ("method_not_allowed", "method not allowed"),
        }
        if exc.status_code >= 500:
            code, message = "internal_error", "internal server error"
        else:
            code, message = messages.get(
                exc.status_code, ("request_rejected", "request rejected")
            )
        return render_error_envelope(request, exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        # C01: no traceback, stack frame, or secret material in any body.
        return render_error_envelope(
            request, 500, "internal_error", "internal server error"
        )

    return app
