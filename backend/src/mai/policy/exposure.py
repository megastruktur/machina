"""Capability exposure projection for objective-scoped runs (§6, AC1).

The ``runtime-v2`` frozen contract (``docs/architecture/runtime-v2.md``
§6) fixes the user's full-list default:

    visible(target) = granted_on_objective_servers - server_denies(target)
                      - objective_denies - parent_denies (for children)

This module is the ONE deterministic projection of that formula. It is a
UI/UX **exposure** vocabulary, never an authorization decision: every
call stays revalidated by the ONE dispatcher
(``mai.capabilities.dispatch.CapabilityDispatcher``) for its exact
target and payload, ``deny`` wins everywhere, and a denied direct call is
still journaled (§5.3). Deliberate boundaries:

- ``granted_on_objective_servers`` — a capability is visible only through
  an explicit grant whose server scope intersects the objective scope.
  Ungranted plugins are NOT advertised (a direct denied call remains
  possible and journaled; exposure merely stops predicting denials).
- ``server_denies(target)`` — expressed by grant omission at that target
  (the grant-config vocabulary has no wildcard; an empty scope means
  *nowhere*). No second deny store exists or is invented here.
- ``objective_denies`` — the policy revision's
  ``denied_capabilities`` (global, capability-level).
- ``parent_denies`` — the delegating parent's explicit subtraction for
  children (v2 delegation narrows; it never adds).

Deterministic: outputs are sorted; identical inputs produce an identical
projection, so descriptors can name valid target sets verbatim and
auditors can replay the projection. Rejections are value-free
(:class:`mai.policy.evaluate.PolicyError`); server identities and
capability slugs are not secret material.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Final

from mai.policy.evaluate import (
    Policy,
    PolicyError,
    PolicyErrorCode,
)

__all__ = [
    "EXPOSURE_PROJECTION_VERSION",
    "ExposedCapability",
    "ObjectiveExposure",
    "project_exposure",
]

#: Projection document version (frozen shape of :meth:`ObjectiveExposure.
#: to_json` / :meth:`ObjectiveExposure.from_json`).
EXPOSURE_PROJECTION_VERSION: Final[int] = 1

#: The frozen capability-slug grammar (same shape as
#: ``mai.policy.evaluate``; a divergent slug rejects the projection).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def _check_slug(capability_id: object) -> str:
    if not isinstance(capability_id, str) or not _SLUG_RE.fullmatch(capability_id):
        raise PolicyError(
            PolicyErrorCode.INVALID_GRANTS,
            "capability ids must match the frozen slug grammar",
        )
    return capability_id


def _coerce_server_id(value: object) -> uuid.UUID:
    """Accept ``uuid.UUID`` or its canonical string form; else reject."""
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            pass
        else:
            if str(parsed) == value:
                return parsed
    raise PolicyError(
        PolicyErrorCode.INVALID_GRANTS,
        "server ids must be canonical UUID values",
    )


def _validated_scope(scope_server_ids: Collection[object]) -> tuple[uuid.UUID, ...]:
    if isinstance(scope_server_ids, (str, bytes)) or not isinstance(
        scope_server_ids, Collection
    ):
        raise PolicyError(
            PolicyErrorCode.INVALID_CONTEXT,
            "scope_server_ids must be a collection of server UUIDs",
        )
    servers = {_coerce_server_id(item) for item in scope_server_ids}
    if not servers:
        raise PolicyError(
            PolicyErrorCode.INVALID_CONTEXT,
            "the objective scope cannot be empty",
        )
    return tuple(sorted(servers, key=str))


@dataclass(frozen=True, slots=True)
class ExposedCapability:
    """One visible capability and its valid target set (§6; AC1)."""

    capability_id: str
    target_server_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class ObjectiveExposure:
    """The exposure projection for one objective scope (immutable).

    ``capabilities`` is the advertised surface, sorted by capability id;
    every member carries its explicit valid target set — no hidden global
    target selection. ``policy_denied``/``parent_denied`` record the
    granted-on-scope capabilities the denies actually subtracted, so the
    projection is auditable without ever widening anything.
    """

    scope_server_ids: tuple[uuid.UUID, ...]
    capabilities: tuple[ExposedCapability, ...]
    policy_denied: tuple[str, ...]
    parent_denied: tuple[str, ...]

    @property
    def capability_ids(self) -> tuple[str, ...]:
        """The advertised capability ids, in projection order."""
        return tuple(item.capability_id for item in self.capabilities)

    def visible(self, capability_id: str) -> bool:
        """Whether the capability is advertised on at least one target."""
        return any(item.capability_id == capability_id for item in self.capabilities)

    def targets(self, capability_id: str) -> tuple[uuid.UUID, ...]:
        """The valid target set for one capability (empty when hidden)."""
        for item in self.capabilities:
            if item.capability_id == capability_id:
                return item.target_server_ids
        return ()

    def to_json(self) -> dict[str, object]:
        """Frozen-shape JSON document (stable key order)."""
        return {
            "v": EXPOSURE_PROJECTION_VERSION,
            "scope_server_ids": [str(s) for s in self.scope_server_ids],
            "capabilities": [
                {
                    "capability_id": item.capability_id,
                    "target_server_ids": [
                        str(s) for s in item.target_server_ids
                    ],
                }
                for item in self.capabilities
            ],
            "policy_denied": list(self.policy_denied),
            "parent_denied": list(self.parent_denied),
        }

    @classmethod
    def from_json(cls, document: object) -> ObjectiveExposure:
        """Rebuild one projection from :meth:`to_json` (strict shape)."""
        if not isinstance(document, Mapping):
            raise PolicyError(
                PolicyErrorCode.INVALID_CONTEXT,
                "exposure document must be a JSON object",
            )
        if document.get("v") != EXPOSURE_PROJECTION_VERSION:
            raise PolicyError(
                PolicyErrorCode.INVALID_CONTEXT,
                f"exposure document v must be {EXPOSURE_PROJECTION_VERSION}",
            )
        allowed = {
            "v",
            "scope_server_ids",
            "capabilities",
            "policy_denied",
            "parent_denied",
        }
        unknown = sorted(str(key) for key in document if key not in allowed)
        if unknown:
            raise PolicyError(
                PolicyErrorCode.INVALID_CONTEXT,
                f"unknown exposure field {unknown[0]!r}",
            )
        capabilities_raw = document["capabilities"]
        if not isinstance(capabilities_raw, list):
            raise PolicyError(
                PolicyErrorCode.INVALID_CONTEXT,
                "capabilities must be an array",
            )
        capabilities: list[ExposedCapability] = []
        for entry in capabilities_raw:
            if not isinstance(entry, Mapping) or set(entry) != {
                "capability_id",
                "target_server_ids",
            }:
                raise PolicyError(
                    PolicyErrorCode.INVALID_CONTEXT,
                    "each exposure entry needs exactly capability_id and "
                    "target_server_ids",
                )
            targets_raw = entry["target_server_ids"]
            if not isinstance(targets_raw, list) or not targets_raw:
                raise PolicyError(
                    PolicyErrorCode.INVALID_CONTEXT,
                    "an exposed capability carries a non-empty target set",
                )
            capabilities.append(
                ExposedCapability(
                    capability_id=_check_slug(entry["capability_id"]),
                    target_server_ids=tuple(
                        _coerce_server_id(item) for item in targets_raw
                    ),
                )
            )
        for field in ("policy_denied", "parent_denied"):
            value = document[field]
            if not isinstance(value, list):
                raise PolicyError(
                    PolicyErrorCode.INVALID_CONTEXT,
                    f"{field} must be an array",
                )
            for item in value:
                _check_slug(item)
        return cls(
            scope_server_ids=_validated_scope(document["scope_server_ids"]),
            capabilities=tuple(capabilities),
            policy_denied=tuple(document["policy_denied"]),
            parent_denied=tuple(document["parent_denied"]),
        )


def project_exposure(
    *,
    grants: Mapping[str, Collection[uuid.UUID]],
    policy: Policy,
    scope_server_ids: Collection[uuid.UUID],
    parent_denies: Collection[str] = (),
) -> ObjectiveExposure:
    """Project one objective scope's visible capability surface (§6).

    Pure and deterministic (sorted outputs; deny wins). ``grants`` is the
    current scoped grant set (capability id → explicit server scope),
    ``policy`` the current revision-bound policy, ``scope_server_ids``
    the objective's explicit in-scope servers and ``parent_denies`` the
    delegating parent's subtraction for children. Any corrupt input
    rejects the whole projection (fail closed); the projection never
    consults registration — a granted-but-uninstalled capability stays
    advertised here and is filtered where tools are built, exactly like
    the single-server surface.
    """
    if not isinstance(policy, Policy):
        raise PolicyError(
            PolicyErrorCode.INVALID_POLICY,
            "policy must be the revision-bound Policy",
        )
    if not isinstance(grants, Mapping):
        raise PolicyError(
            PolicyErrorCode.INVALID_GRANTS,
            "grants must be a mapping of capability id -> server scope",
        )
    scope = _validated_scope(scope_server_ids)
    scope_set = frozenset(scope)
    parent_denied_set: set[str] = set()
    for item in parent_denies:
        parent_denied_set.add(_check_slug(item))
    policy_denied: list[str] = []
    parent_denied: list[str] = []
    capabilities: list[ExposedCapability] = []
    for capability_id in sorted(grants):
        _check_slug(capability_id)
        raw_scope = grants[capability_id]
        if isinstance(raw_scope, (str, bytes)) or not isinstance(raw_scope, Collection):
            raise PolicyError(
                PolicyErrorCode.INVALID_GRANTS,
                f"grant {capability_id!r} scope must be a collection of "
                "server UUIDs",
            )
        granted = {_coerce_server_id(item) for item in raw_scope}
        if not granted & scope_set:
            continue  # not granted anywhere in this scope: never advertised
        if capability_id in policy.denied_capabilities:
            # objective-level deny wins over the grant (C03 rule 5).
            policy_denied.append(capability_id)
            continue
        if capability_id in parent_denied_set:
            # the parent subtracted it for children (narrowing only).
            parent_denied.append(capability_id)
            continue
        targets = tuple(sorted(granted & scope_set, key=str))
        if targets:
            capabilities.append(
                ExposedCapability(
                    capability_id=capability_id, target_server_ids=targets
                )
            )
    return ObjectiveExposure(
        scope_server_ids=scope,
        capabilities=tuple(capabilities),
        policy_denied=tuple(sorted(policy_denied)),
        parent_denied=tuple(sorted(parent_denied)),
    )
