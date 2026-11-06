"""Default capability resolution for session admissions (D09 extraction).

Extracted verbatim from the deleted ``api/chat.py`` so the retained
runbook execution (``api/applications.py``) keeps resolving the same
auto-capability grant set without importing any deleted chat module.

Sources, unioned and deduplicated: the grant configuration store
(entries naming this server), the canonical server profile's
capabilities, enabled plugin bindings (per-server + global), the
internal-API surface and the built-in controller-side defaults. An
unusable source degrades to fewer capabilities — never an error.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import Request

from mai.capabilities.history_tags import TAGS_CAPABILITY_IDS
from mai.capabilities.internal_api import INTERNAL_API_CAPABILITY_IDS
from mai.plugins.config import PluginConfigStore, parse_plugin_config_text
from mai.policy.config import GrantConfigStore

__all__ = [
    "BUILTIN_DEFAULT_CAPABILITIES",
    "SERVER_PROFILE_CAPABILITY_MAP",
    "dedup_preserving_order",
    "plugin_capability_ids",
    "resolve_default_capabilities",
    "server_profile_capability_ids",
]

#: Server-profile capability slugs → the concrete capability ids they
#: enable (C03 rule 3 vocabulary). The worker's grant bootstrap makes
#: exactly the same mapping, so an auto-resolved session context never
#: requests a capability the dispatcher would refuse.
SERVER_PROFILE_CAPABILITY_MAP: Final[dict[str, str]] = {
    "host_shell": "host.shell",
    "host.shell": "host.shell",
    "journald_logs": "host.logs",
    "host.logs": "host.logs",
    "file_read": "host.file_read",
    "host.file_read": "host.file_read",
}

#: Built-in controller-side defaults of the auto-resolved session grant
#: set (alongside ``INTERNAL_API_CAPABILITY_IDS``): document retrieval,
#: web research, the HTTP service probe, the host journal reader and the
#: retained generic ``tags.*`` job-tag vocabulary.
BUILTIN_DEFAULT_CAPABILITIES: Final[tuple[str, ...]] = (
    "docs.retrieve",
    "web.search",
    "web.read",
    "service.http",
    "host.logs",
    *TAGS_CAPABILITY_IDS,
)


def dedup_preserving_order(values: list[str]) -> tuple[str, ...]:
    """Stable de-duplication (first occurrence wins)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


def plugin_capability_ids(
    plugin_store: object, server_id: uuid.UUID
) -> list[str]:
    """Capability ids of every ENABLED plugin binding for this target.

    Both per-server bindings and the global sentinel binding
    (``00000000-0000-0000-0000-000000000000``) count. Any unusable
    plugin store or record degrades to fewer capabilities — the
    admission itself is never blocked by plugin configuration.
    """
    capabilities: list[str] = []
    if not isinstance(plugin_store, PluginConfigStore):
        return capabilities
    try:
        for plugin_name in plugin_store.list_plugins():
            try:
                stored_doc = plugin_store.read_config(plugin_name)
                doc = parse_plugin_config_text(stored_doc.text)
            except Exception:
                continue
            if not doc.enabled:
                continue
            try:
                for binding in plugin_store.list_bindings(plugin_name):
                    if binding.server_id in (server_id, uuid.UUID(int=0)):
                        capabilities.extend(binding.capabilities)
            except Exception:
                continue
    except Exception:
        pass
    return capabilities


def server_profile_capability_ids(
    profile_capabilities: tuple[str, ...],
) -> list[str]:
    """Concrete capability ids enabled by one server profile.

    The worker's grant bootstrap grants both the raw slug and its
    underscore-normalized form plus the explicit ``host_shell`` /
    ``journald_logs`` mappings; this surface resolves exactly the same
    set so auto-resolved sessions never exceed the dispatcher's grants.
    """
    resolved: list[str] = []
    for slug in profile_capabilities:
        resolved.append(slug)
        resolved.append(slug.replace("_", "."))
        mapped = SERVER_PROFILE_CAPABILITY_MAP.get(slug)
        if mapped is not None:
            resolved.append(mapped)
    return resolved


def resolve_default_capabilities(
    request: Request,
    server_id: uuid.UUID,
    profile_capabilities: tuple[str, ...],
) -> list[str]:
    """Every capability granted for the target when the admission omits
    its own ``allowed_capabilities`` (auto-capabilities by default)."""
    resolved: list[str] = []
    grants_store = getattr(request.app.state, "grants", None)
    if isinstance(grants_store, GrantConfigStore):
        try:
            for capability_id, servers in grants_store.read().grants.items():
                if server_id in servers:
                    resolved.append(capability_id)
        except Exception:
            pass
    resolved.extend(server_profile_capability_ids(profile_capabilities))
    resolved.extend(
        plugin_capability_ids(
            getattr(request.app.state, "plugins", None), server_id
        )
    )
    resolved.extend(INTERNAL_API_CAPABILITY_IDS)
    resolved.extend(BUILTIN_DEFAULT_CAPABILITIES)
    return list(dedup_preserving_order(resolved))
