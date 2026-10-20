"""Grant/mode policy evaluation for scoped execution contexts.

Contracts: ``contracts.md`` **C03 rules 2–5 and 7**, auth ADR ``3e79449``
§8 **G2** (enrollment-before-root, operation-level). This module is the
production decision function the dispatcher (``03-dispatcher``) rechecks
immediately before any privileged effect (C03 rule 3, G2 step 4); it owns
the grant semantics the capability registry deliberately does not have
(see ``mai_evidence/03-capability-registry`` limitation 2).

What it decides
---------------

``evaluate()`` answers exactly one question: *may this execution context
apply this capability on this server right now?* It is a pure function —
no I/O, no clock, no process environment, no logging — so a decision can
be re-run identically at any time (auditability of the "recheck
immediately before the effect" rule). Callers supply the world:

- ``context`` — the :class:`ExecutionContext` (C03 rule 2 frozen
  vocabulary; constructed and validated by the dispatcher);
- ``capability_id`` — the capability being requested;
- ``descriptor`` — the descriptor fetched from the
  :class:`mai.plugins.registry.CapabilityRegistry` for this id, or
  ``None`` when the registry has no such capability (revoked/never
  registered);
- ``grants`` — the current scoped grant set (capability id → the
  explicit set of ``server_id`` values the grant covers);
- ``policy`` — the current :class:`Policy` (its ``revision`` is bound
  into every decision so approval binding can detect policy changes,
  C03 rule 4);
- ``adopted`` — the explicit enrollment/adoption verdict for
  ``context.server_id`` (G2 step 1: discovery alone never enables
  execution).

Fail-closed
-----------

Every ambiguity is a **deny decision**, never an exception, never a
guess, never a partial allow: a malformed request id, a malformed grant
set, a missing descriptor, a non-``True`` adoption verdict — all deny.
The reason precedence is frozen (first match wins):

1. ``invalid_capability_identifier`` — the requested id is not a valid
   capability slug;
2. ``invalid_grants`` — the grant set is structurally invalid (one
   corrupt entry denies everything; an untrusted grant set is never
   partially trusted);
3. ``server_not_adopted`` — the adoption verdict is not exactly true
   (G2 step 1);
4. ``capability_denied_by_policy`` — the policy deny list names the
   capability (deny wins over any grant, registration or mode);
5. ``capability_not_registered`` — no descriptor for this exact id
   (revoked or unknown — a stale grant cannot resurrect it);
6. ``capability_not_granted`` — the id is absent from the context's
   allowed capabilities or from the grant set;
7. ``server_scope_mismatch`` — granted, but the grant's explicit server
   scope does not contain ``context.server_id``;
8. ``allowed`` — with the prompting mode reported
   (``requires_approval``), see below.

Modes (C03 rule 5)
------------------

``ask`` and ``delegated`` change prompting behavior *within grants*; a
mode can never widen the grant set, the server scope, the adoption
requirement or the policy deny list — all of those checks run
identically in both modes (pinned by tests). An allowed decision
reports ``requires_approval``:

- ``privileged`` (raw shell/root class) and ``mutate`` require an
  approval decision in ``ask`` mode (C05 ``waiting_approval``) and not
  in ``delegated`` mode (where granted capabilities run autonomously;
  privileged decisions still record the OS-containment forfeiture
  advisory);
- ``read`` never requires approval (no effect to approve).

OS containment honesty (C03 rule 5)
-----------------------------------

A decision **never claims OS-level isolation** for the raw shell/root
class. An allowed ``privileged`` decision carries exactly one advisory,
``raw_root_forfeits_os_containment`` — the contract's forfeiture note —
and no decision field, key or value asserts isolation, sandboxing or
containment of any kind. Tests pin this adversarially.

Child grants (C03 rule 3)
-------------------------

``effective_child_grants(parent, child)`` computes the delegation
intersection: a capability survives only if **both** the parent and the
child grant it, and its effective server scope is the **intersection**
of both scopes — never a union with child profile defaults, never a
sum. An empty intersection drops the grant entirely. The dispatcher
evaluates children against this effective set, so a narrow child can
only narrow further.

Identity and value discipline
-----------------------------

``server_id``/``run_id``/parent identity are opaque ``uuid.UUID`` values
(C01 rule 1); capability ids use the registry's slug shape (frozen by
the canonical profile schema; kept as a local copy with a review note,
same pattern as the registry). :class:`ExecutionContext` carries
credential **references** (names) only — C03 rule 2: secret values are
never serialized into an execution context; resolvers
(``03-credentials``) hand :class:`~mai.credentials.env.SecretValue` at
effect time, never through the context. Rejection details quote ids,
slugs and field names — never grant contents, never credential
material.

Ownership note: the dispatcher constructs real contexts and registries;
if a later card needs to relocate :class:`ExecutionContext`, that is an
explicit C03 reconciliation, not a local edit.
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from mai.plugins.registry import (
    CapabilityDescriptor,
    CapabilityError,
    parse_descriptor,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "PRIVILEGED_FORFEITS_OS_CONTAINMENT",
    "DecisionReason",
    "ExecutionContext",
    "ParentScope",
    "Policy",
    "PolicyDecision",
    "PolicyErrorCode",
    "PolicyError",
    "PolicyMode",
    "ScopedGrants",
    "effective_child_grants",
    "evaluate",
    "execution_context_from_json",
    "grants_from_json",
    "main",
    "policy_from_json",
]

#: Capability identifier shape — the registry's slug charset (frozen by
#: the canonical profile schema ``mai.contracts.server`` ``_CAPABILITY_RE``:
#: "C03 rule 1 identifiers are lowercase slugs"). Kept local: the shape
#: owner is outside this card's allowlist; a divergence would be a review
#: finding, not a silent import of a private symbol (registry pattern).
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

#: The one advisory an allowed ``privileged`` decision may carry — the
#: C03 rule 5 forfeiture note. It is a *loss* statement, never a
#: containment claim.
PRIVILEGED_FORFEITS_OS_CONTAINMENT = "raw_root_forfeits_os_containment"


class PolicyErrorCode(StrEnum):
    """Stable machine-readable construction-rejection codes (C01 style)."""

    INVALID_CONTEXT = "invalid_context"
    INVALID_POLICY = "invalid_policy"
    INVALID_GRANTS = "invalid_grants"
    INVALID_REQUEST = "invalid_request"


class PolicyError(ValueError):
    """One structured, value-free construction rejection.

    Raised only when a *typed input object* cannot be built (a malformed
    context, policy or grant set). ``evaluate()`` itself never raises on
    runtime ambiguity — it returns a deny decision (fail-closed).
    """

    def __init__(self, code: PolicyErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class PolicyMode(StrEnum):
    """Prompting modes within grants (C03 rule 5).

    ``ask``: non-read effects prompt for an approval decision.
    ``delegated``: effects run without prompting — *within* grants; the
    mode never widens any check (see module docstring).
    """

    ASK = "ask"
    DELEGATED = "delegated"


class DecisionReason(StrEnum):
    """Stable decision reasons (frozen precedence, module docstring)."""

    ALLOWED = "allowed"
    INVALID_CAPABILITY_IDENTIFIER = "invalid_capability_identifier"
    INVALID_GRANTS = "invalid_grants"
    SERVER_NOT_ADOPTED = "server_not_adopted"
    CAPABILITY_DENIED_BY_POLICY = "capability_denied_by_policy"
    CAPABILITY_NOT_REGISTERED = "capability_not_registered"
    CAPABILITY_NOT_GRANTED = "capability_not_granted"
    SERVER_SCOPE_MISMATCH = "server_scope_mismatch"


#: Current scoped grant set: capability id → the explicit ``server_id``
#: values the grant covers. An empty scope means *nowhere* (fail-closed;
#: it denies, it does not widen). No wildcard servers exist.
type ScopedGrants = Mapping[str, Collection[uuid.UUID]]


def _check_capability_id(capability_id: object) -> str:
    """Validated capability slug or :class:`PolicyError`."""
    if not isinstance(capability_id, str) or not _IDENTIFIER_RE.fullmatch(
        capability_id
    ):
        raise PolicyError(
            PolicyErrorCode.INVALID_REQUEST,
            "capability_id must match "
            f"{_IDENTIFIER_RE.pattern} (C03 rule 1 slug shape)",
        )
    return capability_id


def _validated_grants(grants: ScopedGrants) -> dict[str, frozenset[uuid.UUID]]:
    """Structurally validated copy of a grant set.

    Every key must be a capability slug and every scope member a
    ``uuid.UUID`` (C01 rule 1). One corrupt entry rejects the whole set:
    a partially-trusted grant set is not fail-closed.
    """
    if not isinstance(grants, Mapping):
        raise PolicyError(
            PolicyErrorCode.INVALID_GRANTS,
            "grants must be a mapping of capability id -> server scope",
        )
    validated: dict[str, frozenset[uuid.UUID]] = {}
    for capability_id, scope in grants.items():
        try:
            _check_capability_id(capability_id)
        except PolicyError as error:
            raise PolicyError(PolicyErrorCode.INVALID_GRANTS, error.detail) from None
        if isinstance(scope, (str, bytes)) or not isinstance(scope, Collection):
            raise PolicyError(
                PolicyErrorCode.INVALID_GRANTS,
                f"grant {capability_id!r} scope must be a collection of "
                "server UUIDs",
            )
        servers: list[uuid.UUID] = []
        for server_id in scope:
            if not isinstance(server_id, uuid.UUID):
                raise PolicyError(
                    PolicyErrorCode.INVALID_GRANTS,
                    f"grant {capability_id!r} scope member is not a "
                    "uuid.UUID server id",
                )
            servers.append(server_id)
        validated[capability_id] = frozenset(servers)
    return validated


def effective_child_grants(
    parent: ScopedGrants, child: ScopedGrants
) -> dict[str, frozenset[uuid.UUID]]:
    """Delegation intersection (C03 rule 3): child ∩ parent, never union.

    A capability is effective for the child only if both sides grant it;
    its effective server scope is the intersection of both explicit
    scopes; an empty intersection drops the grant entirely. Both inputs
    are validated whole before any result is produced. The result maps
    capability ids in sorted order for deterministic decisions.
    """
    parent_valid = _validated_grants(parent)
    child_valid = _validated_grants(child)
    effective: dict[str, frozenset[uuid.UUID]] = {}
    for capability_id in sorted(parent_valid.keys() & child_valid.keys()):
        intersection = parent_valid[capability_id] & child_valid[capability_id]
        if intersection:
            effective[capability_id] = intersection
    return effective


@dataclass(frozen=True, slots=True)
class ParentScope:
    """The immediate parent binding of a delegated execution context.

    Records the parent run and the server it executed on (C03 rule 2
    "parent scope"; the cross-server tabletop C10 rule 7 wants the
    parent's server binding visible in the child). Recorded, not
    enforced: whether a child may target a different server than its
    parent is a delegation-scope decision owned by the dispatcher
    against the effective (intersected) grants.
    """

    run_id: uuid.UUID
    server_id: uuid.UUID

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, uuid.UUID) or not isinstance(
            self.server_id, uuid.UUID
        ):
            raise PolicyError(
                PolicyErrorCode.INVALID_CONTEXT,
                "parent_scope run_id/server_id must be uuid.UUID (C01 rule 1)",
            )


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Validated execution context (C03 rule 2 frozen vocabulary).

    Fields are exactly the frozen vocabulary: principal, ``server_id``,
    ``run_id``, parent scope, profile/prompt revision, credential
    references, allowed capabilities, mode. Secret values are never part
    of this type: ``credential_references`` are opaque resolver *names*;
    values are resolved at effect time by the credentials card. All
    validation happens at construction — a constructed context is
    unambiguous by definition, so ``evaluate()`` can trust its shape.
    """

    principal: str
    server_id: uuid.UUID
    run_id: uuid.UUID
    parent_scope: ParentScope | None
    profile_revision: str
    prompt_revision: str
    credential_references: tuple[str, ...]
    allowed_capabilities: tuple[str, ...]
    mode: PolicyMode

    def __post_init__(self) -> None:
        if not isinstance(self.principal, str) or not self.principal:
            raise PolicyError(
                PolicyErrorCode.INVALID_CONTEXT,
                "principal must be a non-empty string (authenticated "
                "session identity, C01 rule 2)",
            )
        if not isinstance(self.server_id, uuid.UUID):
            raise PolicyError(
                PolicyErrorCode.INVALID_CONTEXT,
                "server_id must be a uuid.UUID (C01 rule 1)",
            )
        if not isinstance(self.run_id, uuid.UUID):
            raise PolicyError(
                PolicyErrorCode.INVALID_CONTEXT,
                "run_id must be a uuid.UUID (C01 rule 1)",
            )
        if self.parent_scope is not None and not isinstance(
            self.parent_scope, ParentScope
        ):
            raise PolicyError(
                PolicyErrorCode.INVALID_CONTEXT,
                "parent_scope must be a ParentScope or None",
            )
        for field_name in ("profile_revision", "prompt_revision"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise PolicyError(
                    PolicyErrorCode.INVALID_CONTEXT,
                    f"{field_name} must be a non-empty string (C03 rule 2)",
                )
        for reference in self.credential_references:
            if not isinstance(reference, str) or not reference:
                raise PolicyError(
                    PolicyErrorCode.INVALID_CONTEXT,
                    "credential_references must be non-empty resolver "
                    "names (never secret values, C03 rule 2)",
                )
        if len(set(self.credential_references)) != len(
            self.credential_references
        ):
            raise PolicyError(
                PolicyErrorCode.INVALID_CONTEXT,
                "credential_references contains duplicates",
            )
        for capability_id in self.allowed_capabilities:
            try:
                _check_capability_id(capability_id)
            except PolicyError as error:
                raise PolicyError(
                    PolicyErrorCode.INVALID_CONTEXT,
                    f"allowed_capabilities entry invalid ({error.detail})",
                ) from None
        if len(set(self.allowed_capabilities)) != len(self.allowed_capabilities):
            raise PolicyError(
                PolicyErrorCode.INVALID_CONTEXT,
                "allowed_capabilities contains duplicates",
            )
        if not isinstance(self.mode, PolicyMode):
            raise PolicyError(
                PolicyErrorCode.INVALID_CONTEXT,
                "mode must be a PolicyMode (ask | delegated)",
            )

    def to_json(self) -> dict[str, object]:
        """Frozen-shape JSON document (references only, C03 rule 2)."""
        document: dict[str, object] = {
            "principal": self.principal,
            "server_id": str(self.server_id),
            "run_id": str(self.run_id),
            "parent_scope": None
            if self.parent_scope is None
            else {
                "run_id": str(self.parent_scope.run_id),
                "server_id": str(self.parent_scope.server_id),
            },
            "profile_revision": self.profile_revision,
            "prompt_revision": self.prompt_revision,
            "credential_references": list(self.credential_references),
            "allowed_capabilities": list(self.allowed_capabilities),
            "mode": self.mode.value,
        }
        return document

    def __repr__(self) -> str:
        return (
            f"<ExecutionContext run={self.run_id} server={self.server_id} "
            f"principal={self.principal!r} mode={self.mode.value}>"
        )


def execution_context_from_json(document: object) -> ExecutionContext:
    """Build an :class:`ExecutionContext` from its :meth:`to_json` shape.

    Accepts exactly the frozen document shape (unknown fields rejected);
    UUID fields accept their string form. Raises :class:`PolicyError`
    with ``invalid_context`` on any deviation — the CLI maps that to a
    deny decision.
    """
    if not isinstance(document, Mapping):
        raise PolicyError(
            PolicyErrorCode.INVALID_CONTEXT,
            "context must be a JSON object",
        )
    vocabulary = {
        "principal",
        "server_id",
        "run_id",
        "parent_scope",
        "profile_revision",
        "prompt_revision",
        "credential_references",
        "allowed_capabilities",
        "mode",
    }
    unknown = sorted(str(key) for key in document if key not in vocabulary)
    if unknown:
        raise PolicyError(
            PolicyErrorCode.INVALID_CONTEXT,
            f"unknown context field {unknown[0]!r}",
        )
    missing = sorted(vocabulary - set(document))
    if missing:
        raise PolicyError(
            PolicyErrorCode.INVALID_CONTEXT,
            f"missing context field {missing[0]!r}",
        )
    try:
        server_id = (
            document["server_id"]
            if isinstance(document["server_id"], uuid.UUID)
            else uuid.UUID(str(document["server_id"]))
        )
        run_id = (
            document["run_id"]
            if isinstance(document["run_id"], uuid.UUID)
            else uuid.UUID(str(document["run_id"]))
        )
    except (TypeError, ValueError, AttributeError) as error:
        raise PolicyError(
            PolicyErrorCode.INVALID_CONTEXT,
            f"server_id/run_id must be UUID values ({type(error).__name__})",
        ) from None
    raw_parent = document["parent_scope"]
    parent_scope: ParentScope | None = None
    if raw_parent is not None:
        if not isinstance(raw_parent, Mapping) or set(raw_parent) != {
            "run_id",
            "server_id",
        }:
            raise PolicyError(
                PolicyErrorCode.INVALID_CONTEXT,
                "parent_scope must be an object with exactly run_id and "
                "server_id, or null",
            )
        try:
            parent_scope = ParentScope(
                run_id=uuid.UUID(str(raw_parent["run_id"])),
                server_id=uuid.UUID(str(raw_parent["server_id"])),
            )
        except (TypeError, ValueError, AttributeError):
            raise PolicyError(
                PolicyErrorCode.INVALID_CONTEXT,
                "parent_scope run_id/server_id must be UUID values",
            ) from None
    references = document["credential_references"]
    allowed = document["allowed_capabilities"]
    if not isinstance(references, list) or not isinstance(allowed, list):
        raise PolicyError(
            PolicyErrorCode.INVALID_CONTEXT,
            "credential_references/allowed_capabilities must be arrays",
        )
    try:
        mode = PolicyMode(document["mode"])
    except ValueError:
        raise PolicyError(
            PolicyErrorCode.INVALID_CONTEXT,
            "mode must be one of: ask, delegated",
        ) from None
    return ExecutionContext(
        principal=document["principal"],
        server_id=server_id,
        run_id=run_id,
        parent_scope=parent_scope,
        profile_revision=document["profile_revision"],
        prompt_revision=document["prompt_revision"],
        credential_references=tuple(references),
        allowed_capabilities=tuple(allowed),
        mode=mode,
    )


@dataclass(frozen=True, slots=True)
class Policy:
    """The current policy: revision plus explicit deny list (C03 rule 3/4).

    ``revision`` is bound verbatim into every decision; approval binding
    (C03 rule 4) treats a revision change as invalidating prior
    approvals. ``denied_capabilities`` names capabilities that are
    refused outright — deny wins over any grant, registration or mode
    (C03 rule 5). Further policy knobs (per-capability mode ceilings and
    the like) are deliberate reconciliations, not local additions.
    """

    revision: int
    denied_capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise PolicyError(
                PolicyErrorCode.INVALID_POLICY,
                "revision must be an int (bool is not an int here)",
            )
        if self.revision < 1:
            raise PolicyError(
                PolicyErrorCode.INVALID_POLICY,
                "revision must be >= 1",
            )
        for capability_id in self.denied_capabilities:
            try:
                _check_capability_id(capability_id)
            except PolicyError as error:
                raise PolicyError(
                    PolicyErrorCode.INVALID_POLICY,
                    f"denied_capabilities entry invalid ({error.detail})",
                ) from None


def policy_from_json(document: object) -> Policy:
    """Build a :class:`Policy` from ``{"revision": …, "denied_capabilities": […]}``."""
    if not isinstance(document, Mapping):
        raise PolicyError(
            PolicyErrorCode.INVALID_POLICY,
            "policy must be a JSON object",
        )
    known_fields = {"revision", "denied_capabilities"}
    unknown = sorted(str(key) for key in document if key not in known_fields)
    if unknown:
        raise PolicyError(
            PolicyErrorCode.INVALID_POLICY,
            f"unknown policy field {unknown[0]!r}",
        )
    if "revision" not in document:
        raise PolicyError(
            PolicyErrorCode.INVALID_POLICY,
            "missing policy field 'revision'",
        )
    denied = document.get("denied_capabilities", [])
    if not isinstance(denied, list):
        raise PolicyError(
            PolicyErrorCode.INVALID_POLICY,
            "denied_capabilities must be an array of capability ids",
        )
    return Policy(revision=document["revision"], denied_capabilities=frozenset(denied))


def grants_from_json(document: object) -> dict[str, frozenset[uuid.UUID]]:
    """Build a grant set from ``[{"capability_id": …, "server_ids": […]}]``.

    Server UUIDs accept their string form. Raises
    :class:`PolicyError` with ``invalid_grants`` on any deviation.
    """
    if not isinstance(document, list):
        raise PolicyError(
            PolicyErrorCode.INVALID_GRANTS,
            "grants must be an array of {capability_id, server_ids} objects",
        )
    grants: dict[str, frozenset[uuid.UUID]] = {}
    for entry in document:
        if not isinstance(entry, Mapping) or set(entry) != {
            "capability_id",
            "server_ids",
        }:
            raise PolicyError(
                PolicyErrorCode.INVALID_GRANTS,
                "each grant must be an object with exactly capability_id "
                "and server_ids",
            )
        try:
            capability_id = _check_capability_id(entry["capability_id"])
        except PolicyError as error:
            raise PolicyError(PolicyErrorCode.INVALID_GRANTS, error.detail) from None
        raw_ids = entry["server_ids"]
        if not isinstance(raw_ids, list):
            raise PolicyError(
                PolicyErrorCode.INVALID_GRANTS,
                f"grant {capability_id!r} server_ids must be an array",
            )
        try:
            scope = frozenset(
                (
                    server_id
                    if isinstance(server_id, uuid.UUID)
                    else uuid.UUID(str(server_id))
                )
                for server_id in raw_ids
            )
        except (TypeError, ValueError, AttributeError):
            raise PolicyError(
                PolicyErrorCode.INVALID_GRANTS,
                f"grant {capability_id!r} server_ids must be UUID values",
            ) from None
        if capability_id in grants:
            raise PolicyError(
                PolicyErrorCode.INVALID_GRANTS,
                f"grant {capability_id!r} appears more than once",
            )
        grants[capability_id] = scope
    return grants


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """One fail-closed policy decision (C03 rules 3–5).

    ``reason`` is stable (``DecisionReason``); ``policy_revision`` is the
    current policy revision at decision time (C03 rule 4 approval
    binding consumes it). ``effect_class`` mirrors the registry
    descriptor when one exists (``None`` for unregistered capabilities).
    ``requires_approval`` is the prompting verdict *for allowed
    decisions* (always ``False`` on denies — no effect is planned). The
    decision never asserts OS isolation for the raw root class; an
    allowed ``privileged`` decision carries the forfeiture advisory and
    nothing else (``advisories``). The decision embeds no context: no
    principal, no run identity, no credential references.
    """

    allowed: bool
    reason: DecisionReason
    policy_revision: int
    capability_id: str
    server_id: uuid.UUID
    effect_class: str | None
    mode: PolicyMode
    requires_approval: bool
    advisories: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        """Frozen-shape JSON document (stable key order)."""
        document: dict[str, object] = {
            "allowed": self.allowed,
            "reason": self.reason.value,
            "policy_revision": self.policy_revision,
            "capability_id": self.capability_id,
            "server_id": str(self.server_id),
            "effect_class": self.effect_class,
            "mode": self.mode.value,
            "requires_approval": self.requires_approval,
            "advisories": list(self.advisories),
        }
        return document

    def __repr__(self) -> str:
        return (
            f"<PolicyDecision {self.capability_id}@{self.server_id} "
            f"{self.reason.value} rev={self.policy_revision}>"
        )


def _deny(
    reason: DecisionReason,
    *,
    context: ExecutionContext,
    capability_id: str,
    policy: Policy,
    descriptor: CapabilityDescriptor | None,
) -> PolicyDecision:
    """One deny decision: no approval, no advisory, no claim."""
    return PolicyDecision(
        allowed=False,
        reason=reason,
        policy_revision=policy.revision,
        capability_id=capability_id,
        server_id=context.server_id,
        effect_class=None if descriptor is None else descriptor.effect_class,
        mode=context.mode,
        requires_approval=False,
        advisories=(),
    )


def evaluate(
    *,
    context: ExecutionContext,
    capability_id: str,
    descriptor: CapabilityDescriptor | None,
    grants: ScopedGrants,
    policy: Policy,
    adopted: bool,
) -> PolicyDecision:
    """Decide one capability application (module docstring: precedence).

    Pure function; identical inputs produce an identical decision, so a
    dispatcher can recheck immediately before the privileged effect
    (C03 rule 3, G2 step 4) and auditors can replay it. Any ambiguity —
    malformed id, malformed grant set, missing descriptor, adoption not
    exactly true — denies; nothing here can widen a grant.
    """
    if not isinstance(capability_id, str) or not _IDENTIFIER_RE.fullmatch(
        capability_id
    ):
        return _deny(
            DecisionReason.INVALID_CAPABILITY_IDENTIFIER,
            context=context,
            capability_id=capability_id if isinstance(capability_id, str) else "",
            policy=policy,
            descriptor=descriptor,
        )
    try:
        validated_grants = _validated_grants(grants)
    except PolicyError:
        return _deny(
            DecisionReason.INVALID_GRANTS,
            context=context,
            capability_id=capability_id,
            policy=policy,
            descriptor=descriptor,
        )
    if adopted is not True:
        return _deny(
            DecisionReason.SERVER_NOT_ADOPTED,
            context=context,
            capability_id=capability_id,
            policy=policy,
            descriptor=descriptor,
        )
    if capability_id in policy.denied_capabilities:
        return _deny(
            DecisionReason.CAPABILITY_DENIED_BY_POLICY,
            context=context,
            capability_id=capability_id,
            policy=policy,
            descriptor=descriptor,
        )
    if (
        descriptor is None
        or not isinstance(descriptor, CapabilityDescriptor)
        or descriptor.identifier != capability_id
    ):
        return _deny(
            DecisionReason.CAPABILITY_NOT_REGISTERED,
            context=context,
            capability_id=capability_id,
            policy=policy,
            descriptor=descriptor,
        )
    if (
        capability_id not in context.allowed_capabilities
        or capability_id not in validated_grants
    ):
        return _deny(
            DecisionReason.CAPABILITY_NOT_GRANTED,
            context=context,
            capability_id=capability_id,
            policy=policy,
            descriptor=descriptor,
        )
    if context.server_id not in validated_grants[capability_id]:
        return _deny(
            DecisionReason.SERVER_SCOPE_MISMATCH,
            context=context,
            capability_id=capability_id,
            policy=policy,
            descriptor=descriptor,
        )

    # Allowed. Prompting verdict within grants (C03 rule 5, G2 step 3):
    # ask mode requires an approval decision for privileged and mutate;
    # delegated mode runs granted capabilities without prompting (privileged
    # still records the forfeiture advisory); read never needs one. A mode
    # can only add a prompt, never a grant.
    requires_approval = (
        descriptor.effect_class in ("privileged", "mutate")
        and context.mode is PolicyMode.ASK
    )
    advisories: tuple[str, ...] = ()
    if descriptor.effect_class == "privileged":
        advisories = (PRIVILEGED_FORFEITS_OS_CONTAINMENT,)
    return PolicyDecision(
        allowed=True,
        reason=DecisionReason.ALLOWED,
        policy_revision=policy.revision,
        capability_id=capability_id,
        server_id=context.server_id,
        effect_class=descriptor.effect_class,
        mode=context.mode,
        requires_approval=requires_approval,
        advisories=advisories,
    )


# ---------------------------------------------------------------------------
# Production entrypoint: one evaluation request -> one decision line
# ---------------------------------------------------------------------------


def _decide_request(document: object) -> PolicyDecision:
    """Evaluate one request document (CLI shape), fail-closed.

    The request must carry exactly ``policy``, ``context``, ``grants``,
    ``descriptor``, ``capability_id`` and ``adopted``. Any structural
    problem yields a deny decision with ``invalid_request`` — the CLI
    never crashes on malformed input and never answers anything but a
    decision.
    """
    request_fields = {
        "policy",
        "context",
        "grants",
        "descriptor",
        "capability_id",
        "adopted",
    }
    if not isinstance(document, Mapping) or set(document) != request_fields:
        raise PolicyError(
            PolicyErrorCode.INVALID_REQUEST,
            "request must be an object with exactly: "
            + ", ".join(sorted(request_fields)),
        )
    policy = policy_from_json(document["policy"])
    context = execution_context_from_json(document["context"])
    grants = grants_from_json(document["grants"])
    descriptor: CapabilityDescriptor | None = None
    if document["descriptor"] is not None:
        try:
            descriptor = parse_descriptor(document["descriptor"], source="<request>")
        except CapabilityError:
            # A descriptor that fails registry validation cannot establish
            # that the capability exists as declared — fail closed.
            raise PolicyError(
                PolicyErrorCode.INVALID_REQUEST,
                "descriptor does not pass registry validation",
            ) from None
    adopted = document["adopted"]
    if not isinstance(adopted, bool):
        raise PolicyError(
            PolicyErrorCode.INVALID_REQUEST,
            "adopted must be true or false",
        )
    checked_id = _check_capability_id(document["capability_id"])
    return evaluate(
        context=context,
        capability_id=checked_id,
        descriptor=descriptor,
        grants=grants,
        policy=policy,
        adopted=adopted,
    )


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mai.policy.evaluate",
        description=(
            "Evaluate one policy request (JSON document) and print the "
            "decision. Exit 0 allowed, 1 denied (including malformed "
            "requests), 2 usage error."
        ),
    )
    parser.add_argument(
        "request_file",
        help="path to the JSON request document (required)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: 0 allowed, 1 denied/malformed, 2 usage error."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        with open(args.request_file, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError):
        _emit({"allowed": False, "reason": "invalid_request"})
        return 1
    try:
        decision = _decide_request(document)
    except PolicyError:
        _emit({"allowed": False, "reason": "invalid_request"})
        return 1
    _emit(decision.to_json())
    return 0 if decision.allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
