"""Plugin configuration/enablement HTTP API: schemas, configs, bindings.

Card ``09-plugin-api`` against contracts.md **C03** (rules 1–2 identifier
vocabulary and credential references, rule 3 grants as explicit admin
actions, rule 6 credentials resolve by reference only, rule 7 no
installation-time auto-grant), **C01** (envelope, opaque identity,
revision conflicts), **C02** (canonical service over the explicit data
root, rule-4 revision triple) and **C08 rule 2** (session → origin → CSRF
on every mutation). The storage semantics underneath are owned by
:mod:`mai.plugins.config` (``09-plugin-store``); the native availability
vocabulary by :mod:`mai.plugins.native` (``09-native-plugin``); the MCP
server vocabulary by :mod:`mai.plugins.mcp` (``09-mcp``); the skill
package format by :mod:`mai.skills.load` (``09-skills``). This surface
joins them to the authenticated API the way ``mai.api.grants`` joins the
grant configuration store.

Routes (C01 prefix/envelope; C02 rule-4 revision triple; C08 boundary):

- ``GET  /api/v1/plugins`` — the catalog: *installed* native entry points
  (metadata-only names; no plugin code runs on a read), *configured*
  plugins from the canonical store (connection state, kind, credential
  reference names) and their **server grant bindings listed separately**
  (AC2: availability is never conflated with a grant). ``configured`` is
  one bounded page (``limit``/``offset``/``total``); ``installed_plugins``
  adds the rich per-plugin view (presentation metadata, owned
  capabilities, required-vs-configured credential badge, declared config
  schema) over the loaded native set, built-ins included.
- ``GET  /api/v1/plugins/{plugin}/config`` — the stored connection
  document (parsed, revision triple). Credential references travel as
  *names* only; no resolution is attempted and no resolved material can
  exist in any response (C03 rule 6).
- ``PUT  /api/v1/plugins/{plugin}/config`` — one versioned edit; the body
  carries ``expected_revision`` (0 = create) and the document fields. The
  route assembles the frozen-shape YAML document and passes the plugin's
  **declared ``config_schema``** (MCP: ``SERVER_CONFIG_SCHEMA``; native:
  the installed descriptor's declaration; unknown plugin: ``None`` → the
  store rejects any non-empty config) so stored values can never skip
  schema validation (schema-strict).
- ``DELETE /api/v1/plugins/{plugin}/config`` — revision-checked removal;
  the producer refuses while grant bindings still exist (a deleted plugin
  cannot leave active bindings behind it).
- ``GET  /api/v1/plugins/{plugin}/schema`` — the *declared* configuration
  schema of the installed plugin (the schema-generated-forms input, IDEA
  §13). Read-only reflection of installed trusted code (C03 rule 7) via
  the native runtime's fail-closed discovery; per-plugin load rejections
  surface value-free instead of pretending an empty schema.
- ``GET  /api/v1/plugins/{plugin}/bindings`` — the plugin's server grant
  bindings (the explicit grant documents).
- ``PUT  /api/v1/plugins/{plugin}/bindings/{server_id}`` — one versioned
  grant binding write; requires an existing connection config (never
  auto-created — no installation-time auto-grant, C03 rule 7).
- ``DELETE /api/v1/plugins/{plugin}/bindings/{server_id}`` — one
  revision-checked grant revocation.
- ``POST /api/v1/plugins/{plugin}/discovery`` — one bounded, real
  transport MCP discovery run (``09-mcp`` producer): connect once, list
  tools, register exactly the granted subset into a throwaway registry,
  return the value-free report. Grants are read from the stored bindings —
  discovery never grants (C03 rule 7); the report separates registered
  from merely offered tools.
- ``POST /api/v1/plugins/{plugin}/probe`` — one bounded, value-free
  provider readiness classification (mutation boundary: session → origin
  → CSRF, the provider check runs real subprocess work). The built-in
  vault plugin is probed through the reviewed resolver lane; the checked
  vault is the explicit body value, else the stored ``default_vault``;
  installed plugins without a probe report a structured ``unsupported``
  classification.
- ``GET  /api/v1/skills`` — loaded-adjacent inspection of the portable
  skill packages below the data root (``09-skills`` format): summaries
  plus value-free per-package rejections (a corrupt package never hides
  the rest, C02 W4 discipline).
- ``GET  /api/v1/skills/{skill}` — one full package (bounded documents,
  digests) plus the prerequisite binding report: declared
  ``requires_capabilities`` against the capability pool currently granted
  in the plugin store — a pure report, never a mutation (C03 rule 7).
  The worker composes the per-run effective surface (grants ∧ registry);
  this endpoint deliberately does not guess it.

Deliberately absent: any creation/installation endpoint. Plugins arrive
as installed distributions (entry points) or pre-staged skill files; the
HTTP surface exposes configuration and enablement only (AC2: no unsafe
package-installation route exists to leave unguarded).

Value discipline: error bodies carry stable value-free codes only; views
report plugin names, capability slugs, reference *names*, opaque server
UUIDs and revision triples — never credential values, prompt text or
free-form annotations. Route/doc listeners stay off (G1); the schema is
produced in-process via ``app.openapi()`` and recorded as contract
evidence for the central exporter owned by the ``openapi-client``
resource (this card does not hand-edit generated client types).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Mapping
from importlib.metadata import entry_points
from pathlib import Path
from typing import Annotated, Any, Final

import yaml
from fastapi import APIRouter, FastAPI, Query, Request, Response
from pydantic import BaseModel, Field
from starlette.status import (
    HTTP_200_OK,
    HTTP_204_NO_CONTENT,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_502_BAD_GATEWAY,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.api.auth import (
    BoundaryError,
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
    render_error_envelope,
)
from mai.credentials.env import (
    DATA_ROOT_CREDENTIALS_FILENAME,
    EnvFileError,
    EnvResolver,
    build_runtime_env_resolver,
    upsert_env_credentials,
)
from mai.credentials.onepassword import OnePasswordResolver
from mai.plugins.builtin_onepassword import CREDENTIAL_REFERENCES, PLUGIN_NAME
from mai.plugins.config import (
    SCHEMA_VERSION as PLUGIN_SCHEMA_VERSION,
)
from mai.plugins.config import (
    PluginConfigError,
    PluginConfigErrorCode,
    PluginConfigStore,
    parse_plugin_config_text,
)
from mai.plugins.mcp import (
    SERVER_CONFIG_SCHEMA,
    MAX_CREDENTIAL_REFS,
    McpBridgeError,
    McpBridgeErrorCode,
    McpServerConfig,
    McpServerRuntime,
    granted_capability_ids,
)
from mai.plugins.native import (
    CREDENTIAL_REFERENCE_RE,
    ENTRY_POINT_GROUP,
    NativePlugin,
    NativePluginRuntime,
)
from mai.plugins.registry import CapabilityRegistry
from mai.skills.load import (
    SKILL_NAME_RE,
    SKILLS_SUBPATH,
    SkillLoadError,
    SkillLoadErrorCode,
    load_skill,
    missing_prerequisites,
)

__all__ = ["register_plugins"]

#: Plugin (entry-point) name shape — the store's frozen pattern (C03
#: rule 1 slug charset; anchored: the canonical path is traversal-proof
#: by construction).
_PLUGIN_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9._-]{0,127}$"

#: Credential-reference shape — the native runtime's exported frozen
#: pattern (C03 rule 6); the store re-validates authoritatively on write.
_CREDENTIAL_REF_PATTERN: Final[str] = CREDENTIAL_REFERENCE_RE

CapabilitySlug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]
CredentialRef = Annotated[str, Field(pattern=_CREDENTIAL_REF_PATTERN)]
PluginName = Annotated[str, Field(pattern=_PLUGIN_PATTERN)]


# ---------------------------------------------------------------------------
# Wire shapes (transport only; document truth is the canonical store)
# ---------------------------------------------------------------------------


class RevisionTriple(BaseModel):
    """The C02 rule-4 version triple of one committed document."""

    revision: int
    schema_revision: int
    sha256: str


class BindingView(BaseModel):
    """One stored server grant binding: the explicit grant document."""

    server_id: uuid.UUID
    capabilities: list[str]
    revision: int
    schema_revision: int
    sha256: str


class ConfiguredPluginView(BaseModel):
    """One configured plugin: connection state plus its grant bindings.

    ``kind`` classifies the connection: ``mcp`` (a ``transport`` config —
    the store's shared convention), ``native`` (an installed entry point
    of the same name) or ``uninstalled`` (configured but not discoverable
    — surfaced, never silently dropped; its grants stay in the store and
    contribute nothing until backed by a registered descriptor).
    """

    plugin: str
    enabled: bool
    kind: str
    credential_refs: list[str]
    configured_credentials: list[str] = Field(default_factory=list)
    config: dict[str, object]
    revision: int
    schema_revision: int
    sha256: str
    bindings: list[BindingView]


class PluginCatalogView(BaseModel):
    """Availability and grants as deliberately separate sections (AC2).

    ``configured`` is one bounded page over every configured plugin
    (``total`` is the pre-slice count; ``limit``/``offset`` echo the
    request). ``installed`` stays metadata-only names; ``installed_plugins``
    is the rich per-plugin view for the plugins window (presentation
    metadata, owned capabilities, required vs configured credential
    references, declared config schema).
    """

    installed: list[str]
    configured: list[ConfiguredPluginView]
    installed_plugins: list[InstalledPluginView] = Field(default_factory=list)
    limit: int
    offset: int
    total: int


class InstalledPluginView(BaseModel):
    """One installed native plugin's rich catalog view (plugins window).

    ``capabilities`` are the declared capability identifiers in
    declaration order (a plugin may own several); ``required_credentials``
    are the declared reference *names*; ``configured_credentials`` is the
    sorted subset of those names present in the active runtime
    ``EnvResolver.configured_keys()`` — a status badge, never a value
    (C03 rule 6). ``config_schema`` is the shared declared configuration
    schema (every descriptor of the plugin declares the same object).
    """

    plugin: str
    title: str
    description: str
    capabilities: list[str]
    required_credentials: list[str]
    configured_credentials: list[str] = Field(default_factory=list)
    config_schema: dict[str, Any] | None = None


class PluginConfigView(BaseModel):
    """The stored connection document (parsed) with its version triple."""

    plugin: str
    enabled: bool
    credential_refs: list[str]
    configured_credentials: list[str] = Field(default_factory=list)
    config: dict[str, object]
    revision: int
    schema_revision: int
    sha256: str


class PluginSchemaView(BaseModel):
    """The declared configuration schema of one plugin (IDEA §13 input).

    ``config_schema`` is ``null`` when the plugin declares none (the
    write path then rejects any non-empty config — fail closed).
    ``load_rejection`` is the value-free discovery-stage code when the
    installed plugin failed to load; the schema of a rejected plugin is
    unknown, never invented.
    """

    plugin: str
    kind: str
    config_schema: dict[str, object] | None
    load_rejection: str | None = None


class PluginConfigEditBody(BaseModel):
    """One versioned config edit: caller's revision + the new document.

    Deliberately no ``schema_version``/``plugin`` fields: the route
    derives them from the frozen current version and the storage identity
    (a client can neither nominate a schema version nor mismatch the
    embedded plugin name).
    """

    expected_revision: int = Field(ge=0)
    enabled: bool
    credential_refs: list[CredentialRef] = Field(
        default_factory=list, max_length=MAX_CREDENTIAL_REFS
    )
    credential_values: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional write-only map of credential_ref name to secret value "
            "persisted in the controller's 0600 credentials.env store and "
            "never written to plugin config YAML or returned on read."
        ),
    )
    config: dict[str, Any] = Field(default_factory=dict)


class PluginProbeBody(BaseModel):
    """Optional readiness-probe input: one explicit vault name.

    Omitted, the probe falls back to the stored config's
    ``default_vault`` (and then to the provider's unscoped listing).
    """

    vault: str | None = Field(
        default=None,
        description=(
            "Target provider vault name or ID to check; omitted means the "
            "plugin's stored default_vault (or the unscoped provider check)."
        ),
    )


class PluginProbeView(BaseModel):
    """One value-free provider readiness classification (the probe result).

    ``code`` is the provider's stable vocabulary (``ready``, or a
    structured failure code); ``detail`` names references and
    classifications, never credential material. ``checked_vault`` echoes
    the vault the probe actually checked (explicit, stored default or
    ``null`` for the unscoped check).
    """

    plugin: str
    available: bool
    code: str
    detail: str
    checked_vault: str | None = None


class PluginBindingEditBody(BaseModel):
    """One versioned grant binding write (explicit admin action)."""

    expected_revision: int = Field(ge=0)
    capabilities: list[CapabilitySlug] = Field(min_length=1)


class McpToolRejectionView(BaseModel):
    """One value-free per-tool discovery rejection (09-mcp vocabulary)."""

    tool: str
    code: str
    detail: str


class McpDiscoveryReportView(BaseModel):
    """The value-free result of one bounded discovery run (09-mcp)."""

    plugin: str
    transport: str
    tool_count: int
    registered: list[str]
    ungranted: list[str]
    stale_grants: list[str]
    rejections: list[McpToolRejectionView]
    limit_exceeded: bool


class SkillDocumentView(BaseModel):
    """One bounded, digest-stamped skill document (09-skills shape)."""

    name: str
    text: str
    sha256: str
    size_bytes: int


class SkillSummaryView(BaseModel):
    """One loadable skill package: manifest-level summary only."""

    skill: str
    title: str | None
    description: str | None
    requires_capabilities: list[str]
    references: list[str]
    package_sha256: str
    total_bytes: int


class SkillRejectionView(BaseModel):
    """One value-free per-package load rejection (surfaced, not hidden)."""

    skill: str
    code: str


class SkillCatalogView(BaseModel):
    """The skill namespace: loadable summaries plus value-free rejections."""

    skills: list[SkillSummaryView]
    rejections: list[SkillRejectionView]


class SkillPackageView(BaseModel):
    """One full package plus the prerequisite binding report (C03 rule 7).

    ``granted_capability_pool`` is the union of capability ids currently
    granted by any stored binding; ``missing_from_granted_pool`` is the
    declared-minus-granted report. The worker composes the per-run
    effective surface (grants ∧ registry); this view never guesses it.
    """

    skill: str
    title: str | None
    description: str | None
    requires_capabilities: list[str]
    instructions: SkillDocumentView
    documents: list[SkillDocumentView]
    package_sha256: str
    total_bytes: int
    granted_capability_pool: list[str]
    missing_from_granted_pool: list[str]


# ---------------------------------------------------------------------------
# Producer/store rejection mapping (C01 envelope; boundary in mai.api.auth)
# ---------------------------------------------------------------------------

#: Document-validation rejections — the producer refused the content.
#: Identity/revision misuse mostly cannot occur through the typed paths
#: but is classified anyway (servers-router pattern).
_DOCUMENT_REJECTIONS: Final[frozenset[PluginConfigErrorCode]] = frozenset(
    {
        PluginConfigErrorCode.INVALID_PLUGIN_NAME,
        PluginConfigErrorCode.INVALID_SERVER_ID,
        PluginConfigErrorCode.INVALID_REVISION,
        PluginConfigErrorCode.PLUGIN_MISMATCH,
        PluginConfigErrorCode.SERVER_ID_MISMATCH,
        PluginConfigErrorCode.INVALID_DOCUMENT,
        PluginConfigErrorCode.UNPARSABLE_DOCUMENT,
        PluginConfigErrorCode.DOCUMENT_TOO_LARGE,
        PluginConfigErrorCode.DOCUMENT_TOO_DEEP,
        PluginConfigErrorCode.DOCUMENT_ALIAS_FORBIDDEN,
        PluginConfigErrorCode.SCHEMA_VERSION_UNSUPPORTED,
        PluginConfigErrorCode.MISSING_FIELD,
        PluginConfigErrorCode.UNKNOWN_FIELD,
        PluginConfigErrorCode.FIELD_TYPE_INVALID,
        PluginConfigErrorCode.CREDENTIAL_REF_INVALID,
        PluginConfigErrorCode.INLINE_CREDENTIAL_FORBIDDEN,
        PluginConfigErrorCode.CONFIG_SCHEMA_INVALID,
        PluginConfigErrorCode.CONFIG_SCHEMA_UNSUPPORTED,
    }
)

#: Canonical-state rejections needing recovery or an ordering decision —
#: surfaced, never silently repaired (C02 rule 5 discipline).
_STATE_REJECTIONS: Final[frozenset[PluginConfigErrorCode]] = frozenset(
    {
        PluginConfigErrorCode.INVALID_WIRING,
        PluginConfigErrorCode.PATH_ESCAPE,
        PluginConfigErrorCode.BINDINGS_PRESENT,
        PluginConfigErrorCode.METADATA_MISSING,
        PluginConfigErrorCode.METADATA_INVALID,
        PluginConfigErrorCode.ORPHAN_METADATA,
        PluginConfigErrorCode.HASH_MISMATCH,
    }
)


def _plugin_store(request: Request) -> PluginConfigStore:
    """The lifespan-built store; without it fail closed, touch nothing."""
    store = getattr(request.app.state, "plugins", None)
    if store is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "plugin configuration service is unavailable",
        )
    return store


def _skills_root(request: Request) -> Path:
    """The lifespan-resolved data root; without it fail closed."""
    root = getattr(request.app.state, "skills_root", None)
    if root is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "skills service is unavailable",
        )
    return Path(root)


def _config_error(
    request: Request,
    exc: PluginConfigError,
    *,
    not_found_status: int = HTTP_404_NOT_FOUND,
    not_found_code: str = "not_found",
    not_found_fields: dict[str, str] | None = None,
) -> Response:
    """Map a store/producer rejection onto the C01 envelope.

    Exhaustive over :class:`PluginConfigErrorCode`:
    ``not_found`` takes the caller-supplied status/code (a binding
    write's missing *connection config* is an ordering conflict, not a
    missing binding); validation rejections carry the producer's stable
    code in ``fields``; state conflicts are value-free ``409`` bodies;
    anything unmapped takes the app's generic ``internal_error`` path
    rather than masquerade as a denial.
    """
    if exc.code is PluginConfigErrorCode.NOT_FOUND:
        return render_error_envelope(
            request,
            not_found_status,
            not_found_code,
            "no such plugin configuration resource",
            fields=not_found_fields or {},
        )
    if exc.code is PluginConfigErrorCode.STALE_REVISION:
        return render_error_envelope(
            request,
            HTTP_409_CONFLICT,
            "conflict",
            "the plugin configuration changed since the last observed "
            "revision",
        )
    if exc.code is PluginConfigErrorCode.BINDINGS_PRESENT:
        # An ordering rejection for the admin (bindings go first), not
        # corrupted state: the stable code is carried in fields.
        return render_error_envelope(
            request,
            HTTP_409_CONFLICT,
            "conflict",
            "plugin still has server grant bindings; remove them first",
            fields={"binding": exc.code.value},
        )
    if exc.code in _DOCUMENT_REJECTIONS:
        return render_error_envelope(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "plugin configuration failed validation",
            fields={"document": exc.code.value},
        )
    if exc.code in _STATE_REJECTIONS:
        return render_error_envelope(
            request,
            HTTP_409_CONFLICT,
            "conflict",
            "canonical plugin configuration state requires recovery",
        )
    raise exc  # pragma: no cover - the mapping above is exhaustive


# ---------------------------------------------------------------------------
# Kind classification + declared schema resolution (the schema-strict input)
# ---------------------------------------------------------------------------

#: MCP kind marker — the store's shared convention (``mai.plugins.mcp``
#: ``server_plugins``): a stored config carrying ``transport`` is an MCP
#: server connection; native configs never declare one.
_MCP_KIND: Final[str] = "mcp"
_NATIVE_KIND: Final[str] = "native"
_UNINSTALLED_KIND: Final[str] = "uninstalled"


def _installed_plugin_names() -> tuple[str, ...]:
    """Installed native entry-point names, sorted — metadata only.

    ``importlib.metadata`` reads distribution metadata; no plugin module
    is imported and no plugin code runs on a read path.
    """
    try:
        found = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # noqa: BLE001 - metadata scan must never 500 a read
        return ()
    return tuple(sorted({ep.name for ep in found if isinstance(ep.name, str)}))


def _build_runtime_resolver(
    request: Request, store: PluginConfigStore
) -> EnvResolver | None:
    """The active runtime credential source (data-root store over env file).

    Fresh per call, so just-upserted credential values are visible to the
    same request that upserted them.
    """
    env_map = getattr(request.app.state, "env", None) or {}
    return build_runtime_env_resolver(
        env_map.get("MAI_ENV_FILE"), store.root
    )


def _runtime_env_keys(
    request: Request, store: PluginConfigStore
) -> frozenset[str]:
    """The active runtime's configured credential reference names.

    The runtime resolver layers ``<data_root>/credentials.env`` over the
    configured ``MAI_ENV_FILE``; without any configured source the key
    set is empty. Names only — never values (C03 rule 6).
    """
    resolver = _build_runtime_resolver(request, store)
    if resolver is None:
        return frozenset()
    return resolver.configured_keys()


def _plugin_metadata_map(
    plugins: tuple[NativePlugin, ...],
) -> dict[str, dict[str, str]]:
    """``PLUGIN_METADATA`` per loaded plugin, when its module exports it.

    The presentation metadata (title/description) is a module-level
    constant of trusted installed code (C03 rule 7 — the same trust the
    worker's discovery applies; discovery has already imported these
    modules in this process). A plugin without the export falls back to
    the plugin name as title and an empty description.
    """
    metadata: dict[str, dict[str, str]] = {}
    for loaded in plugins:
        module = sys.modules.get(loaded.handler.__module__)
        declared = getattr(module, "PLUGIN_METADATA", None)
        metadata[loaded.plugin] = (
            {str(key): str(value) for key, value in declared.items()}
            if isinstance(declared, dict)
            else {}
        )
    return metadata


def _shared_config_schema(
    loaded: NativePlugin, descriptors: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The plugin's shared declared config schema (its primary declaration).

    Every descriptor of one plugin declares the same schema object (C3);
    the primary (first declared) descriptor's schema is the shared one.
    """
    for name in loaded.descriptor_names:
        descriptor = descriptors.get(name)
        schema = getattr(descriptor, "config_schema", None)
        if schema is not None:
            return dict(schema)
    return None


def _declared_config_schema(
    plugin: str, document_config: dict[str, Any] | None
) -> PluginSchemaView:
    """Resolve the declared ``config_schema`` for one plugin (schema-strict).

    - a config carrying ``transport`` is an MCP server connection: the
      frozen ``SERVER_CONFIG_SCHEMA`` applies;
    - otherwise an installed native entry point's descriptors declare the
      schema — resolved through a fresh fail-closed native discovery over
      a throwaway registry (trusted installed code, C03 rule 7; per-plugin
      rejections are reported value-free, never hidden). Built-in plugins
      are included: their entry points live on this very distribution, so
      schema-strict validation must cover them exactly like the worker's
      restart composition does;
    - a plugin owning several capabilities declares one shared schema
      across all of its descriptors (C3) — the primary descriptor's
      declaration is it;
    - an uninstalled plugin declares nothing: ``None`` — the write path
      then rejects any non-empty config instead of storing unvalidated
      values.
    """
    if document_config is not None and "transport" in document_config:
        return PluginSchemaView(
            plugin=plugin,
            kind=_MCP_KIND,
            config_schema=dict(SERVER_CONFIG_SCHEMA),
        )
    if plugin not in _installed_plugin_names():
        return PluginSchemaView(
            plugin=plugin,
            kind=_UNINSTALLED_KIND,
            config_schema=None,
        )
    registry = CapabilityRegistry()
    runtime = NativePluginRuntime(
        registry=registry, disabled=(), include_builtins=True
    )
    runtime.discover()
    rejection = next(
        (
            rejected.code
            for rejected in runtime.rejections
            if rejected.plugin == plugin
        ),
        None,
    )
    if rejection is not None:
        return PluginSchemaView(
            plugin=plugin,
            kind=_NATIVE_KIND,
            config_schema=None,
            load_rejection=rejection,
        )
    loaded = next(
        (entry for entry in runtime.plugins if entry.plugin == plugin),
        None,
    )
    schema = (
        None
        if loaded is None
        else _shared_config_schema(
            loaded,
            {
                candidate.identifier: candidate
                for candidate in registry.list_capabilities()
            },
        )
    )
    return PluginSchemaView(
        plugin=plugin,
        kind=_NATIVE_KIND,
        config_schema=schema,
    )


def _canonical_config_text(
    plugin: str, payload: PluginConfigEditBody
) -> str:
    """Assemble the frozen-shape YAML document (single producer).

    Only the typed fields travel; unknown body fields are structurally
    excluded (pydantic surface), so annotations can never enter the
    canonical namespace (C02 rule 6). The route derives
    ``schema_version`` and the embedded ``plugin`` identity.
    """
    document = {
        "schema_version": PLUGIN_SCHEMA_VERSION,
        "plugin": plugin,
        "enabled": payload.enabled,
        "credential_refs": list(payload.credential_refs),
        "config": dict(payload.config),
    }
    return yaml.safe_dump(document, sort_keys=True, default_flow_style=False)


def _canonical_binding_text(
    plugin: str, server_id: uuid.UUID, payload: PluginBindingEditBody
) -> str:
    """Assemble the frozen-shape grant binding document."""
    document = {
        "schema_version": PLUGIN_SCHEMA_VERSION,
        "plugin": plugin,
        "server_id": str(server_id),
        "capabilities": list(payload.capabilities),
    }
    return yaml.safe_dump(document, sort_keys=True, default_flow_style=False)


def _parse_stored_config(store: PluginConfigStore, plugin: str):
    """Read and parse the stored connection document (producer-verified)."""
    stored = store.read_config(plugin)
    return stored, parse_plugin_config_text(stored.text)


def _granted_pool(store: PluginConfigStore) -> tuple[str, ...]:
    """Union of capability ids granted by any stored binding (sorted)."""
    granted: set[str] = set()
    for plugin in store.list_plugins():
        for document in store.list_bindings(plugin):
            granted.update(document.capabilities)
    return tuple(sorted(granted))


# ---------------------------------------------------------------------------
# Routes — plugins (C01 prefix/envelope/identity; C02/C03 producers; C08)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/plugins", tags=["plugins"])

_SESSION_401: Final[dict[str, object]] = {
    "model": ErrorEnvelope,
    "description": "No valid session",
}
_MUTATION_403: Final[dict[str, object]] = {
    "model": ErrorEnvelope,
    "description": "Origin not allowed or CSRF token rejected",
}
_UNAVAILABLE_503: Final[dict[str, object]] = {
    "model": ErrorEnvelope,
    "description": "Plugin configuration service unavailable",
}


@router.get(
    "",
    status_code=HTTP_200_OK,
    response_model=PluginCatalogView,
    summary="List installed, configured and granted plugins",
    responses={
        HTTP_401_UNAUTHORIZED: _SESSION_401,  # type: ignore[arg-type]
        HTTP_503_SERVICE_UNAVAILABLE: _UNAVAILABLE_503,  # type: ignore[arg-type]
    },
)
async def list_plugins(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    """Availability (installed/configured/enabled) and grants, separately.

    Safe method: session layer only. Installed names come from
    distribution metadata (no plugin code runs for ``installed``);
    configured documents and bindings come from the canonical store,
    served as one deterministic bounded page (``total`` is the pre-slice
    count). ``installed_plugins`` enriches the loaded native set (built-ins
    included, the worker's own composition trust) with presentation
    metadata, owned capabilities and the required-vs-configured credential
    badge — names only, never values (C03 rule 6). A configured plugin
    whose binding names a capability nothing registers is reported through
    the store's own views by the worker; this catalog never unions the two.
    """
    await _require_session(request)
    store = _plugin_store(request)
    installed = list(_installed_plugin_names())
    env_keys = _runtime_env_keys(request, store)
    configured: list[ConfiguredPluginView] = []
    for plugin in store.list_plugins():
        try:
            stored, document = _parse_stored_config(store, plugin)
            bindings: list[BindingView] = []
            for bound in store.list_bindings(plugin):
                bound_stored = store.read_binding(plugin, bound.server_id)
                bindings.append(
                    BindingView(
                        server_id=bound.server_id,
                        capabilities=list(bound.capabilities),
                        revision=bound_stored.revision,
                        schema_revision=bound_stored.schema_revision,
                        sha256=bound_stored.sha256,
                    )
                )
        except PluginConfigError as exc:
            return _config_error(request, exc)
        if "transport" in document.config:
            kind = _MCP_KIND
        elif plugin in installed:
            kind = _NATIVE_KIND
        else:
            kind = _UNINSTALLED_KIND
        configured.append(
            ConfiguredPluginView(
                plugin=plugin,
                enabled=document.enabled,
                kind=kind,
                credential_refs=list(document.credential_refs),
                configured_credentials=sorted(
                    set(document.credential_refs) & env_keys
                ),
                config=dict(document.config),
                revision=stored.revision,
                schema_revision=stored.schema_revision,
                sha256=stored.sha256,
                bindings=bindings,
            )
        )
    total = len(configured)
    page = configured[offset : offset + limit]
    registry = CapabilityRegistry()
    runtime = NativePluginRuntime(
        registry=registry, disabled=(), include_builtins=True
    )
    runtime.discover()
    descriptors = {
        candidate.identifier: candidate
        for candidate in registry.list_capabilities()
    }
    metadata = _plugin_metadata_map(runtime.plugins)
    installed_plugins = [
        InstalledPluginView(
            plugin=loaded.plugin,
            title=metadata.get(loaded.plugin, {}).get("title") or loaded.plugin,
            description=metadata.get(loaded.plugin, {}).get("description", ""),
            capabilities=list(loaded.descriptor_names),
            required_credentials=list(loaded.credential_references),
            configured_credentials=sorted(
                set(loaded.credential_references) & env_keys
            ),
            config_schema=_shared_config_schema(loaded, descriptors),
        )
        for loaded in runtime.plugins
    ]
    return PluginCatalogView(
        installed=installed,
        configured=page,
        installed_plugins=installed_plugins,
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get(
    "/{plugin}/config",
    status_code=HTTP_200_OK,
    response_model=PluginConfigView,
    summary="Read one stored plugin connection document",
    responses={
        HTTP_401_UNAUTHORIZED: _SESSION_401,  # type: ignore[arg-type]
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No configuration stored for this plugin",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Stored state requires recovery",
        },
        HTTP_503_SERVICE_UNAVAILABLE: _UNAVAILABLE_503,  # type: ignore[arg-type]
    },
)
async def read_config(request: Request, plugin: PluginName) -> Response:
    """The stored document: credential reference *names*, never values."""
    await _require_session(request)
    store = _plugin_store(request)
    try:
        stored, document = _parse_stored_config(store, plugin)
    except PluginConfigError as exc:
        return _config_error(request, exc)
    return PluginConfigView(
        plugin=document.plugin,
        enabled=document.enabled,
        credential_refs=list(document.credential_refs),
        configured_credentials=sorted(
            set(document.credential_refs) & _runtime_env_keys(request, store)
        ),
        config=dict(document.config),
        revision=stored.revision,
        schema_revision=stored.schema_revision,
        sha256=stored.sha256,
    )


@router.put(
    "/{plugin}/config",
    status_code=HTTP_200_OK,
    response_model=RevisionTriple,
    summary="Commit one versioned plugin configuration edit",
    responses={
        HTTP_401_UNAUTHORIZED: _SESSION_401,  # type: ignore[arg-type]
        HTTP_403_FORBIDDEN: _MUTATION_403,  # type: ignore[arg-type]
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale expected revision or canonical state "
            "requires recovery",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Document failed producer validation "
            "(schema/credential discipline)",
        },
        HTTP_503_SERVICE_UNAVAILABLE: _UNAVAILABLE_503,  # type: ignore[arg-type]
    },
)
async def edit_config(
    request: Request, plugin: PluginName, payload: PluginConfigEditBody
) -> Response:
    """Validate against the declared schema, then atomically commit.

    Full boundary order (session → origin → CSRF) runs before any state
    is touched. Credential values go first: ``credential_values`` are
    validated and upserted into the controller's 0600
    ``credentials.env`` and the runtime credential source rebuilt
    *before* the canonical config write, so a rejected value never
    leaves a half-applied edit behind (config.yaml untouched, 422).
    The built-in vault plugin additionally refuses ``enabled: true``
    while its service-account reference has no configured value (the
    gate answers from the updated key set, before ``enabled: true`` can
    land in config.yaml). The store re-validates authoritatively: frozen
    envelope vocabulary, credential-token keys rejected outright
    (``inline_credential_forbidden``) and ``config`` values checked
    against the plugin's *declared* ``config_schema`` — resolved here so
    stored values can never skip validation (schema-strict). A stale
    caller is rejected with ``409 conflict`` before any filesystem
    effect.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _plugin_store(request)
    if payload.credential_values:
        declared_refs = set(payload.credential_refs)
        undeclared = [
            key for key in payload.credential_values if key not in declared_refs
        ]
        if undeclared:
            return render_error_envelope(
                request,
                HTTP_422_UNPROCESSABLE_CONTENT,
                "validation_error",
                "credential_values keys must appear in credential_refs",
                fields={"credential_values": "undeclared_credential_ref"},
            )
        # Credential-first ordering: the values land in the 0600
        # credentials.env store (and the runtime source is rebuilt) before
        # the canonical document is written — an invalid value fails the
        # whole request with config.yaml untouched.
        try:
            upsert_env_credentials(
                store.root / DATA_ROOT_CREDENTIALS_FILENAME,
                payload.credential_values,
            )
        except EnvFileError as exc:
            return render_error_envelope(
                request,
                HTTP_422_UNPROCESSABLE_CONTENT,
                "validation_error",
                "credential value failed validation",
                fields={"credential_values": exc.code.value},
            )
        request.app.state.credentials = _build_runtime_resolver(request, store)
    if plugin == PLUGIN_NAME and payload.enabled:
        configured = _runtime_env_keys(request, store)
        missing = [
            ref for ref in CREDENTIAL_REFERENCES if ref not in configured
        ]
        if missing:
            return render_error_envelope(
                request,
                HTTP_422_UNPROCESSABLE_CONTENT,
                "validation_error",
                "enabling the plugin requires its credential references to "
                "have configured values",
                fields={"credential_refs": "missing_credential_value"},
            )
    declared = _declared_config_schema(plugin, payload.config)
    try:
        stored = store.write_config(
            plugin,
            _canonical_config_text(plugin, payload),
            expected_revision=payload.expected_revision,
            config_schema=declared.config_schema,
        )
    except PluginConfigError as exc:
        return _config_error(request, exc)
    return RevisionTriple(
        revision=stored.revision,
        schema_revision=stored.schema_revision,
        sha256=stored.sha256,
    )


@router.delete(
    "/{plugin}/config",
    status_code=HTTP_204_NO_CONTENT,
    summary="Delete one plugin connection document (revision-checked)",
    responses={
        HTTP_401_UNAUTHORIZED: _SESSION_401,  # type: ignore[arg-type]
        HTTP_403_FORBIDDEN: _MUTATION_403,  # type: ignore[arg-type]
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Nothing stored for this plugin",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Grant bindings still exist, stale revision, or "
            "canonical state requires recovery",
        },
        HTTP_503_SERVICE_UNAVAILABLE: _UNAVAILABLE_503,  # type: ignore[arg-type]
    },
)
async def remove_config(
    request: Request,
    plugin: PluginName,
    expected_revision: Annotated[int, Query(ge=0)],
) -> Response:
    """Refused while grant bindings reference the plugin (409).

    Bindings are removed first, so no binding can outlive its config
    through this API (a deleted plugin cannot leave active grants).
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _plugin_store(request)
    try:
        store.delete_config(plugin, expected_revision=expected_revision)
    except PluginConfigError as exc:
        return _config_error(request, exc)
    return Response(status_code=HTTP_204_NO_CONTENT)


@router.get(
    "/{plugin}/schema",
    status_code=HTTP_200_OK,
    response_model=PluginSchemaView,
    summary="Read the declared configuration schema of one plugin",
    responses={
        HTTP_401_UNAUTHORIZED: _SESSION_401,  # type: ignore[arg-type]
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Plugin neither installed nor configured",
        },
        HTTP_503_SERVICE_UNAVAILABLE: _UNAVAILABLE_503,  # type: ignore[arg-type]
    },
)
async def read_schema(request: Request, plugin: PluginName) -> Response:
    """The schema-generated-forms input (IDEA §13), value-free.

    MCP servers declare the frozen ``SERVER_CONFIG_SCHEMA``; native
    plugins are resolved through a fresh fail-closed discovery (trusted
    installed code — the same trust the worker applies at restart, here
    over a throwaway registry with no executor wiring). A configured
    plugin that is neither (``uninstalled``) reports ``null``.
    """
    await _require_session(request)
    store = _plugin_store(request)
    stored_names = store.list_plugins()
    installed = _installed_plugin_names()
    if plugin not in stored_names and plugin not in installed:
        return render_error_envelope(
            request,
            HTTP_404_NOT_FOUND,
            "not_found",
            "plugin is neither configured nor installed",
        )
    document_config: dict[str, Any] | None = None
    if plugin in stored_names:
        try:
            _, document = _parse_stored_config(store, plugin)
        except PluginConfigError as exc:
            return _config_error(request, exc)
        document_config = dict(document.config)
    return _declared_config_schema(plugin, document_config)


@router.get(
    "/{plugin}/bindings",
    status_code=HTTP_200_OK,
    response_model=list[BindingView],
    summary="List one plugin's server grant bindings",
    responses={
        HTTP_401_UNAUTHORIZED: _SESSION_401,  # type: ignore[arg-type]
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Stored state requires recovery",
        },
        HTTP_503_SERVICE_UNAVAILABLE: _UNAVAILABLE_503,  # type: ignore[arg-type]
    },
)
async def list_bindings(request: Request, plugin: PluginName) -> Response:
    """The explicit grant documents of one plugin (never implied)."""
    await _require_session(request)
    store = _plugin_store(request)
    bindings: list[BindingView] = []
    try:
        for bound in store.list_bindings(plugin):
            bound_stored = store.read_binding(plugin, bound.server_id)
            bindings.append(
                BindingView(
                    server_id=bound.server_id,
                    capabilities=list(bound.capabilities),
                    revision=bound_stored.revision,
                    schema_revision=bound_stored.schema_revision,
                    sha256=bound_stored.sha256,
                )
            )
    except PluginConfigError as exc:
        return _config_error(request, exc)
    return bindings


@router.put(
    "/{plugin}/bindings/{server_id}",
    status_code=HTTP_200_OK,
    response_model=RevisionTriple,
    summary="Commit one versioned server grant binding",
    responses={
        HTTP_401_UNAUTHORIZED: _SESSION_401,  # type: ignore[arg-type]
        HTTP_403_FORBIDDEN: _MUTATION_403,  # type: ignore[arg-type]
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No binding stored under this identity",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "No connection config stored for the plugin, "
            "stale revision, or canonical state requires recovery",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Binding failed producer validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: _UNAVAILABLE_503,  # type: ignore[arg-type]
    },
)
async def edit_binding(
    request: Request,
    plugin: PluginName,
    server_id: uuid.UUID,
    payload: PluginBindingEditBody,
) -> Response:
    """Grant explicitly; requires an existing connection config (409).

    Writing a binding never creates the config and installing/connecting
    never grants (C03 rule 7): the canonical store enforces both, this
    route only maps the rejections.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _plugin_store(request)
    try:
        stored = store.write_binding(
            plugin,
            server_id,
            _canonical_binding_text(plugin, server_id, payload),
            expected_revision=payload.expected_revision,
        )
    except PluginConfigError as exc:
        # A binding write's NOT_FOUND is the missing *config* prerequisite
        # (the store refuses to invent one) — an ordering conflict, not a
        # missing binding.
        return _config_error(
            request,
            exc,
            not_found_status=HTTP_409_CONFLICT,
            not_found_code="conflict",
            not_found_fields={"binding": "connection_config_missing"},
        )
    return RevisionTriple(
        revision=stored.revision,
        schema_revision=stored.schema_revision,
        sha256=stored.sha256,
    )


@router.delete(
    "/{plugin}/bindings/{server_id}",
    status_code=HTTP_204_NO_CONTENT,
    summary="Revoke one server grant binding (revision-checked)",
    responses={
        HTTP_401_UNAUTHORIZED: _SESSION_401,  # type: ignore[arg-type]
        HTTP_403_FORBIDDEN: _MUTATION_403,  # type: ignore[arg-type]
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No binding stored under this identity",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale expected revision or canonical state "
            "requires recovery",
        },
        HTTP_503_SERVICE_UNAVAILABLE: _UNAVAILABLE_503,  # type: ignore[arg-type]
    },
)
async def remove_binding(
    request: Request,
    plugin: PluginName,
    server_id: uuid.UUID,
    expected_revision: Annotated[int, Query(ge=0)],
) -> Response:
    """Revoke one explicit grant; the config document stays."""
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _plugin_store(request)
    try:
        store.delete_binding(plugin, server_id, expected_revision=expected_revision)
    except PluginConfigError as exc:
        return _config_error(request, exc)
    return Response(status_code=HTTP_204_NO_CONTENT)


@router.post(
    "/{plugin}/discovery",
    status_code=HTTP_200_OK,
    response_model=McpDiscoveryReportView,
    summary="Run one bounded MCP discovery against the configured server",
    responses={
        HTTP_401_UNAUTHORIZED: _SESSION_401,  # type: ignore[arg-type]
        HTTP_403_FORBIDDEN: _MUTATION_403,  # type: ignore[arg-type]
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No configuration stored for this plugin",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Not an MCP server configuration, disabled, or "
            "configuration fails composition",
        },
        HTTP_502_BAD_GATEWAY: {
            "model": ErrorEnvelope,
            "description": "Bounded discovery failure against the server",
        },
        HTTP_503_SERVICE_UNAVAILABLE: _UNAVAILABLE_503,  # type: ignore[arg-type]
    },
)
async def run_discovery(request: Request, plugin: PluginName) -> Response:
    """Connect once, list tools, register exactly the granted subset.

    The granted set is read from the stored bindings — discovery never
    grants (C03 rule 7); offered-but-ungranted tools are reported by
    name. Descriptors register into a throwaway registry: this endpoint
    is a bounded diagnostic, the worker's restart composition stays the
    execution-path owner. All transport work is the reviewed ``09-mcp``
    producer's (bounded timeouts, page/deadline bounds, value-free
    failures).
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _plugin_store(request)
    try:
        _, document = _parse_stored_config(store, plugin)
    except PluginConfigError as exc:
        return _config_error(request, exc)
    if "transport" not in document.config:
        return render_error_envelope(
            request,
            HTTP_409_CONFLICT,
            "conflict",
            "configured plugin is not an MCP server connection",
        )
    try:
        config = McpServerConfig.from_document(document)
    except McpBridgeError as exc:
        if exc.code is McpBridgeErrorCode.INVALID_CONFIG:
            return render_error_envelope(
                request,
                HTTP_409_CONFLICT,
                "conflict",
                "stored MCP configuration fails composition validation",
                fields={"document": exc.code.value},
            )
        raise
    credentials = getattr(request.app.state, "credentials", None)
    if config.transport == "stdio" and config.credential_refs:
        if credentials is None:
            raise BoundaryError(
                HTTP_503_SERVICE_UNAVAILABLE,
                "unavailable",
                "credential source is unavailable; a stdio server with "
                "credential references cannot be discovered",
            )
        runtime = McpServerRuntime(
            config=config,
            registry=CapabilityRegistry(),
            credentials=lambda: credentials,
        )
    else:
        runtime = McpServerRuntime(config=config, registry=CapabilityRegistry())
    try:
        report = await runtime.discover(
            granted=granted_capability_ids(store, plugin)
        )
    except McpBridgeError as exc:
        if exc.code is McpBridgeErrorCode.CONNECT_FAILED:
            return render_error_envelope(
                request,
                HTTP_502_BAD_GATEWAY,
                "discovery_failed",
                "bounded MCP discovery failed against the configured server",
            )
        raise
    payload = report.to_json()
    return McpDiscoveryReportView(
        plugin=str(payload["plugin"]),
        transport=str(payload["transport"]),
        tool_count=int(payload["tool_count"]),  # type: ignore[arg-type]
        registered=list(payload["registered"]),  # type: ignore[arg-type]
        ungranted=list(payload["ungranted"]),  # type: ignore[arg-type]
        stale_grants=list(payload["stale_grants"]),  # type: ignore[arg-type]
        rejections=[
            McpToolRejectionView(
                tool=str(item["tool"]),
                code=str(item["code"]),
                detail=str(item["detail"]),
            )
            for item in payload["rejections"]  # type: ignore[union-attr]
        ],
        limit_exceeded=bool(payload["limit_exceeded"]),
    )


@router.post(
    "/{plugin}/probe",
    status_code=HTTP_200_OK,
    response_model=PluginProbeView,
    summary="Probe one plugin's provider readiness (value-free)",
    responses={
        HTTP_401_UNAUTHORIZED: _SESSION_401,  # type: ignore[arg-type]
        HTTP_403_FORBIDDEN: _MUTATION_403,  # type: ignore[arg-type]
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Plugin neither installed nor configured",
        },
        HTTP_503_SERVICE_UNAVAILABLE: _UNAVAILABLE_503,  # type: ignore[arg-type]
    },
)
async def probe_plugin(
    request: Request,
    plugin: PluginName,
    payload: PluginProbeBody = PluginProbeBody(),
) -> Response:
    """One bounded, non-destructive readiness classification.

    POST under the full mutation boundary (session → origin → CSRF)
    because the provider check runs real subprocess work. The built-in
    vault plugin is probed through the reviewed resolver lane (its token
    resolves by reference from the active runtime ``.env`` source — never
    a process-environment scan); the checked vault is the explicit body
    value, else the stored config's ``default_vault``, else the
    unscoped provider check. The report is value-free by construction.
    A plugin that is neither installed nor configured is ``404``; an
    installed plugin without a probe reports a structured
    ``unsupported`` classification instead of pretending readiness.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _plugin_store(request)
    stored_names = store.list_plugins()
    installed = _installed_plugin_names()
    if plugin not in stored_names and plugin not in installed:
        return render_error_envelope(
            request,
            HTTP_404_NOT_FOUND,
            "not_found",
            "plugin is neither configured nor installed",
        )
    if plugin != PLUGIN_NAME:
        return PluginProbeView(
            plugin=plugin,
            available=False,
            code="unsupported",
            detail="plugin exposes no readiness probe",
        )
    target_vault = payload.vault
    if target_vault is None and plugin in stored_names:
        try:
            _, document = _parse_stored_config(store, plugin)
        except PluginConfigError as exc:
            return _config_error(request, exc)
        configured_vault = document.config.get("default_vault")
        if isinstance(configured_vault, str) and configured_vault:
            target_vault = configured_vault
    runtime_resolver = _build_runtime_resolver(request, store)
    if runtime_resolver is None:
        return PluginProbeView(
            plugin=plugin,
            available=False,
            code="token_missing",
            detail=(
                "credential source is not configured; the service-account "
                "token reference cannot resolve (no fallback scan)"
            ),
            checked_vault=target_vault,
        )
    resolver = OnePasswordResolver(
        credential_ref=CREDENTIAL_REFERENCES[0],
        resolver=runtime_resolver,
        token_env=None,
    )
    report = await asyncio.to_thread(resolver.probe, target_vault)
    return PluginProbeView(
        plugin=plugin,
        available=report.available,
        code=report.code,
        detail=report.detail,
        checked_vault=report.checked_vault,
    )


# ---------------------------------------------------------------------------
# Routes — skills (inspection only; no write route exists by design, AC2)
# ---------------------------------------------------------------------------

skills_router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


def _skill_error(request: Request, exc: SkillLoadError) -> Response:
    """Map a loader rejection onto the C01 envelope (value-free codes)."""
    if exc.code is SkillLoadErrorCode.SKILL_NOT_FOUND:
        return render_error_envelope(
            request,
            HTTP_404_NOT_FOUND,
            "not_found",
            "no skill package stored under this name",
        )
    if exc.code is SkillLoadErrorCode.SKILL_INVALID:
        return render_error_envelope(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "skill name failed validation",
            fields={"skill": exc.code.value},
        )
    return render_error_envelope(
        request,
        HTTP_409_CONFLICT,
        "conflict",
        "stored skill package requires recovery",
        fields={"package": exc.code.value},
    )


def _skill_view(package, granted: tuple[str, ...]) -> SkillPackageView:
    """Materialize the wire view from one loaded package."""
    manifest = package.manifest
    return SkillPackageView(
        skill=manifest.skill,
        title=manifest.title,
        description=manifest.description,
        requires_capabilities=list(manifest.requires_capabilities),
        instructions=SkillDocumentView(
            name=package.instructions.name,
            text=package.instructions.text,
            sha256=package.instructions.sha256,
            size_bytes=package.instructions.size_bytes,
        ),
        documents=[
            SkillDocumentView(
                name=document.name,
                text=document.text,
                sha256=document.sha256,
                size_bytes=document.size_bytes,
            )
            for document in package.references
        ],
        package_sha256=package.package_sha256,
        total_bytes=package.total_bytes,
        granted_capability_pool=list(granted),
        missing_from_granted_pool=list(
            missing_prerequisites(package, granted)
        ),
    )


@skills_router.get(
    "",
    status_code=HTTP_200_OK,
    response_model=SkillCatalogView,
    summary="List portable skill packages below the data root",
    responses={
        HTTP_401_UNAUTHORIZED: _SESSION_401,  # type: ignore[arg-type]
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Skills service unavailable",
        },
    },
)
async def list_skills(request: Request) -> Response:
    """Manifest-level summaries; corrupt packages surface by code.

    Only name-shaped, non-symlink directories are candidates; each is
    loaded authoritatively by the reviewed loader (all-or-nothing per
    package). A rejection records the stable code and never hides the
    remaining packages (C02 W4 discipline). Installing or modifying
    packages is a filesystem-admin action — deliberately no HTTP write
    route exists (AC2).
    """
    await _require_session(request)
    root = _skills_root(request)
    skills_dir = root / SKILLS_SUBPATH
    summaries: list[SkillSummaryView] = []
    rejections: list[SkillRejectionView] = []
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir()):
            if not entry.is_dir() or entry.is_symlink():
                continue
            if not SKILL_NAME_RE.fullmatch(entry.name):
                continue
            try:
                package = load_skill(root, entry.name)
            except SkillLoadError as exc:
                rejections.append(
                    SkillRejectionView(skill=entry.name, code=exc.code.value)
                )
                continue
            summaries.append(
                SkillSummaryView(
                    skill=package.manifest.skill,
                    title=package.manifest.title,
                    description=package.manifest.description,
                    requires_capabilities=list(
                        package.manifest.requires_capabilities
                    ),
                    references=list(package.manifest.references),
                    package_sha256=package.package_sha256,
                    total_bytes=package.total_bytes,
                )
            )
    return SkillCatalogView(skills=summaries, rejections=rejections)


@skills_router.get(
    "/{skill}",
    status_code=HTTP_200_OK,
    response_model=SkillPackageView,
    summary="Read one skill package and its prerequisite binding report",
    responses={
        HTTP_401_UNAUTHORIZED: _SESSION_401,  # type: ignore[arg-type]
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No skill package stored under this name",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stored package requires recovery",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Skill name failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Skills service unavailable",
        },
    },
)
async def read_skill(request: Request, skill: PluginName) -> Response:
    """The bounded package (texts, digests, sizes) — inert by design.

    ``missing_from_granted_pool`` is the declared-prerequisites report
    against the capability pool granted by stored bindings — a pure
    report (C03 rule 7); loading a skill never grants a tool.
    """
    await _require_session(request)
    root = _skills_root(request)
    try:
        package = load_skill(root, skill)
    except SkillLoadError as exc:
        return _skill_error(request, exc)
    return _skill_view(package, _granted_pool(_plugin_store(request)))


def register_plugins(app: FastAPI) -> None:
    """Register the plugin + skill surfaces under the shared boundary
    (api-wiring)."""
    app.include_router(router)
    app.include_router(skills_router)
