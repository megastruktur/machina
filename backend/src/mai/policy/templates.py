"""Named server-policy templates projected into the grant config (§6, AC2).

``research_only`` is the frozen named server-policy template: a
DETERMINISTIC projection into explicit grant/deny sets of the existing
versioned policy configuration (``<data_root>/policy/config.json`` +
``config.meta.json``), managed by
:class:`mai.policy.config.GrantConfigStore` with ``expected_revision``
CAS and one monotonic policy revision. It is NOT a second policy engine:
the projection produces exactly the frozen document schema the store
already validates (``{"policy", "grants"}``) and hands it to the store's
own CAS write.

Projection discipline (all deterministic, deny wins):

- For every REGISTERED capability, the newest registered version decides
  the effect class — the same descriptor the dispatcher would execute.
  ``read``-effect capabilities are GRANTED on the template servers.
  ``mutate``/``privileged`` capabilities are REVOKED from the template
  servers (grant omission is the per-server deny of the grant-config
  vocabulary). Any other/unknown effect class fails closed
  (``unsupported_effect_class``) — the template never guesses.
- Grants of servers OUTSIDE the template set are preserved verbatim;
  inside the template set exactly the read-effect registered vocabulary
  remains (a grant naming an unregistered capability does not survive
  on template servers — the template set ends up EXACTLY the registered
  read vocabulary; such a grant is inert at dispatch anyway, and the
  projection's outcome stays deterministic).
- ``policy.denied_capabilities`` is preserved verbatim: capability-level
  denies are global (deny wins everywhere) and research-only is a
  PER-SERVER statement — extending the global deny list would silence
  the capability on every server, which the template must never do.
- The document's ``policy.revision`` is ``expected_revision + 1`` (the
  store's CAS contract: the caller states the revision it last
  observed; the document names the next). A stale caller is rejected by
  the store (``stale_revision``) before any filesystem effect, and the
  projection itself refuses a caller whose observation disagrees with
  its ``expected_revision`` (deterministic document construction).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from mai.plugins.registry import CapabilityRegistry
from mai.policy.config import (
    ABSENT_REVISION,
    GrantConfigError,
    GrantConfigErrorCode,
    GrantConfigStore,
    StoredGrantConfig,
)

__all__ = [
    "RESEARCH_ONLY_TEMPLATE_NAME",
    "SERVER_POLICY_TEMPLATE_NAMES",
    "TemplateError",
    "TemplateErrorCode",
    "TemplateProjection",
    "apply_research_only",
    "project_research_only",
]

#: The frozen named template (runtime-v2.md §6; RV2-DENY fixtures).
RESEARCH_ONLY_TEMPLATE_NAME: Final[str] = "research_only"

#: The template-name vocabulary this module projects.
SERVER_POLICY_TEMPLATE_NAMES: Final[tuple[str, ...]] = (RESEARCH_ONLY_TEMPLATE_NAME,)


class TemplateErrorCode(StrEnum):
    """Stable machine-readable template rejections (C01 vocabulary)."""

    INVALID_SERVERS = "invalid_template_servers"
    STALE_REVISION = "stale_revision"
    UNSUPPORTED_EFFECT_CLASS = "unsupported_effect_class"


class TemplateError(ValueError):
    """One structured, value-free template projection rejection."""

    def __init__(self, code: TemplateErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class TemplateProjection:
    """The deterministic result of one template projection (audit view).

    ``document`` is exactly the store-shaped candidate document;
    ``granted``/``revoked`` name the capability-id changes ON THE
    TEMPLATE SERVERS only (references, never server content).
    """

    template: str
    server_ids: tuple[uuid.UUID, ...]
    document: dict[str, object]
    granted: tuple[str, ...]
    revoked: tuple[str, ...]


def _validated_servers(server_ids: Collection[object]) -> tuple[uuid.UUID, ...]:
    if isinstance(server_ids, (str, bytes)) or not isinstance(
        server_ids, Collection
    ):
        raise TemplateError(
            TemplateErrorCode.INVALID_SERVERS,
            "server_ids must be a collection of server UUIDs",
        )
    servers: set[uuid.UUID] = set()
    for item in server_ids:
        if isinstance(item, uuid.UUID):
            servers.add(item)
            continue
        if isinstance(item, str):
            try:
                parsed = uuid.UUID(item)
            except ValueError:
                pass
            else:
                if str(parsed) == item:
                    servers.add(parsed)
                    continue
        raise TemplateError(
            TemplateErrorCode.INVALID_SERVERS,
            "server_ids must be canonical UUID values",
        )
    if not servers:
        raise TemplateError(
            TemplateErrorCode.INVALID_SERVERS,
            "the template projection needs at least one server",
        )
    return tuple(sorted(servers, key=str))


def project_research_only(
    *,
    registry: CapabilityRegistry,
    server_ids: Collection[uuid.UUID],
    current: StoredGrantConfig | None,
    expected_revision: int,
) -> TemplateProjection:
    """Project ``research_only`` for ``server_ids`` (pure; AC2).

    ``current`` is the caller's last observed configuration (``None``
    before the first commit, i.e. ``expected_revision == 0``). The
    result's ``document`` feeds :meth:`GrantConfigStore.write` with the
    same ``expected_revision``; the projection is pure — nothing is
    persisted here.
    """
    if isinstance(expected_revision, bool) or not isinstance(
        expected_revision, int
    ) or expected_revision < 0:
        raise TemplateError(
            TemplateErrorCode.STALE_REVISION,
            "expected_revision must be a non-negative int",
        )
    servers = _validated_servers(server_ids)
    template_servers = frozenset(servers)
    observed = ABSENT_REVISION if current is None else current.revision
    if observed != expected_revision:
        raise TemplateError(
            TemplateErrorCode.STALE_REVISION,
            f"expected_revision {expected_revision} does not match the "
            f"observed configuration revision ({observed})",
        )
    # Newest registered version per identifier (the dispatcher's rule).
    newest: dict[str, object] = {}
    for descriptor in registry.list_capabilities():
        newest[descriptor.identifier] = descriptor
    read_capabilities: set[str] = set()
    for capability_id, descriptor in sorted(newest.items()):
        effect_class = getattr(descriptor, "effect_class", None)
        if effect_class == "read":
            read_capabilities.add(capability_id)
        elif effect_class in ("mutate", "privileged"):
            continue
        else:
            # Unknown effect classes fail closed (§6): the template never
            # guesses whether an exotic class is research-safe.
            raise TemplateError(
                TemplateErrorCode.UNSUPPORTED_EFFECT_CLASS,
                f"capability {capability_id!r} carries effect class "
                f"{effect_class!r}; research_only projects exactly the "
                "read/mutate/privileged vocabulary",
            )
    base: dict[str, set[uuid.UUID]] = {}
    if current is not None:
        for capability_id, scope in current.grants.items():
            remaining = set(scope) - template_servers
            if remaining:
                base[capability_id] = remaining
    granted: list[str] = []
    revoked: list[str] = []
    grants: dict[str, set[uuid.UUID]] = {}
    for capability_id in sorted(set(base) | read_capabilities):
        outside = base.get(capability_id, set())
        before_on_template = (
            set(current.grants.get(capability_id, ())) & template_servers
            if current is not None
            else set()
        )
        if capability_id in read_capabilities:
            after = outside | template_servers
            if not before_on_template:
                granted.append(capability_id)
        else:
            after = outside
            if before_on_template:
                revoked.append(capability_id)
        if after:
            grants[capability_id] = after
    document: dict[str, object] = {
        "policy": {
            "revision": expected_revision + 1,
            "denied_capabilities": (
                sorted(current.policy.denied_capabilities)
                if current is not None
                else []
            ),
        },
        "grants": [
            {
                "capability_id": capability_id,
                "server_ids": sorted(str(s) for s in grants[capability_id]),
            }
            for capability_id in sorted(grants)
        ],
    }
    return TemplateProjection(
        template=RESEARCH_ONLY_TEMPLATE_NAME,
        server_ids=servers,
        document=document,
        granted=tuple(granted),
        revoked=tuple(revoked),
    )


def apply_research_only(
    *,
    store: GrantConfigStore,
    registry: CapabilityRegistry,
    server_ids: Collection[uuid.UUID],
    expected_revision: int,
) -> tuple[StoredGrantConfig, TemplateProjection]:
    """Read-observe → project → CAS-write one ``research_only`` projection.

    The store is the single CAS authority: a concurrent writer between
    the observation and the write surfaces as the store's
    ``stale_revision`` :class:`GrantConfigError` — never a lost update.
    Returns the stored configuration and the projection (the audit view
    of what the template changed on the template servers).
    """
    try:
        current: StoredGrantConfig | None = store.read()
    except GrantConfigError as error:
        if error.code is not GrantConfigErrorCode.NOT_FOUND:
            raise
        current = None
    projection = project_research_only(
        registry=registry,
        server_ids=server_ids,
        current=current,
        expected_revision=expected_revision,
    )
    stored = store.write(projection.document, expected_revision=expected_revision)
    return stored, projection
