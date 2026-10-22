"""Native plugin runtime — installed trusted Python capability plugins.

Cards ``09-dependencies`` (selected distributions) and ``09-native-plugin``
(this module) against contracts.md **C03 rule 7**: "Native Python plugins
are trusted installed code (v1) ... Plugin validation is not sandboxing."
and IDEA.md §13 ("Python plugins: native tools, integrations, and
credential providers" without extending the core agent loop).

The frozen plugin contract (AC1)
--------------------------------

One installed distribution exposes one or more plugins through the
importlib.metadata entry-point group :data:`ENTRY_POINT_GROUP`
(``mai.capabilities``). One entry point is one plugin:

- entry point **name** = plugin name (configuration/disable key);
- entry point **value** = a dotted module path (no ``:attr``) — the
  plugin module;
- the module declares a module-level ``CAPABILITY`` mapping (singular,
  the original 1:1 contract) **or** a non-empty module-level
  ``CAPABILITIES`` sequence of mappings (one plugin owning several
  capability identifiers) — each in exactly the frozen C03 rule 1
  descriptor vocabulary (same declaration name and validation path as
  ``mai.plugins.registry``, the vocabulary owner). A multi-capability
  plugin must declare one identical ``config_schema`` across all its
  descriptors (the plugin has one configuration form) and, unlike the
  legacy singular declaration, every descriptor must pass the
  agent-readiness gate (:func:`mai.capabilities.internal_api.assert_agent_ready_descriptor`
  — LLM-facing tool and parameter descriptions);
- the module declares a callable ``execute`` — the trusted handler
  receiving a :class:`~mai.capabilities.dispatch.CapabilityCall` and
  returning an :class:`~mai.capabilities.dispatch.ExecutorResult`
  (sync return or awaitable);
- the module MAY declare ``CREDENTIAL_REFERENCES``: the credential
  reference *names* the handler needs (C03 rule 2 shape). Names only —
  resolution happens at effect time through the dispatcher's credential
  seam (C03 rule 6), never at load, and the runtime reads no process
  environment.

The manifest envelope (capability document + credential references) is
validated by :class:`NativePluginManifest` (pydantic, ``extra="forbid"``)
— the selected schema runtime (09-dependencies); a ``ValidationError`` is
the explicit compatibility failure. The embedded capability document then
goes through :func:`mai.plugins.registry.parse_descriptor`, the frozen
C03 rule 1 vocabulary owner. No plugin can add fields the descriptor
vocabulary does not define: the strict envelope structurally excludes
code references (``handler``, ``entry_point``) and credential values.

What this runtime does — and deliberately does not
---------------------------------------------------

- **Discovery is load, not install.** Plugins are discovered from
  already-installed distributions via ``importlib.metadata`` entry
  points. There is no install/uninstall endpoint here and no runtime
  installer dependency (negative controls: ``test_plugin_dependencies.py``):
  adding a plugin is an operator out-of-process installation, then a
  reload of this runtime at composition time. **No hot arbitrary pip
  install exists** (AC2).
- **Loading executes trusted code by design** (C03 rule 7). Importing a
  plugin module runs it; validation is metadata validation, **not**
  sandboxing. Import failures report the exception *type name* only —
  never its message.
- **Registration ≠ authorization.** Validated descriptors are registered
  into the caller-supplied
  :class:`mai.plugins.registry.CapabilityRegistry` — the declarative
  store every consumer (dispatcher, tool bridge, jobs facade) selects
  from. The runtime grants nothing, caches no policy state and performs
  no execution of its own.
- **Disable is a load-time configuration.** A plugin name listed in
  ``disabled`` is skipped *before* registration: a disabled (or invalid)
  plugin cannot register enabled tools (AC2). Capability-level denial is
  the policy deny list / grants (C03, owner: policy cards); package-level
  disable is this knob. Changing the disabled set means composing a new
  runtime with a fresh registry — the registry itself is append-only by
  contract (registration is a declared fact, not an upsert).
- **Built-ins are opt-in per composition.** The host distribution itself
  (``BUILTIN_DISTRIBUTION``) may ship its own plugins (today
  ``onepassword``). By default a runtime composes *third-party installed*
  plugins only — entry points backed by the host distribution are skipped
  before import — so fixture/diagnostic compositions see exactly the
  installed third-party set. Hosts that execute built-ins (the worker,
  the inspector CLI) pass ``include_builtins=True`` explicitly.
- **Zero unmediated execution.** The only invocation surface is the
  :attr:`NativePluginRuntime.executor` async callable, built to be wired
  into :class:`mai.capabilities.dispatch.CapabilityDispatcher` as its
  executor (C03 rule 7: the host's trusted capability implementation).
  Handlers are unreachable except through a dispatch that passed every
  gate; there is no CLI that runs a capability (the dispatcher ships none
  for the same reason — a generic run-this backdoor). A dispatch for a
  capability without an enabled plugin returns a structured ``ok=False``
  answer instead of executing (defense-in-depth behind the registry).
- **One owner per capability identifier.** A second plugin claiming an
  already-bound identifier is rejected (``capability_identifier_conflict``)
  rather than shadowing: the executor routes by identifier, so ambiguity
  is an installation error, never a silent last-writer-wins.

Value-free discipline: rejection details carry plugin names, identifiers,
versions, source paths and field names — never manifest document content
(a schema may describe credential-shaped configuration), never resolved
credential values, never import-error messages. One rejected plugin is
recorded and skipped; it cannot prevent the other plugins from loading.

Thread safety: ``discover`` runs once and populates immutable state;
accessors before discovery raise (fail-closed — an undiscovered runtime
that answered "no plugins" would look like a silent empty success).
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import EntryPoint, entry_points
from types import ModuleType
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    model_validator,
)

from mai.capabilities.dispatch import CapabilityCall, ExecutorResult
from mai.capabilities.internal_api import assert_agent_ready_descriptor
from mai.plugins.registry import (
    CAPABILITY_DECLARATION,
    CapabilityDescriptor,
    CapabilityErrorCode,
    CapabilityError,
    CapabilityRegistry,
    parse_descriptor,
)

__all__ = [
    "BUILTIN_DISTRIBUTION",
    "CAPABILITIES_DECLARATION",
    "CREDENTIAL_DECLARATION",
    "CREDENTIAL_REFERENCE_RE",
    "ENTRY_POINT_GROUP",
    "HANDLER_DECLARATION",
    "NativePlugin",
    "NativePluginError",
    "NativePluginErrorCode",
    "NativePluginManifest",
    "NativePluginRuntime",
    "PluginRejection",
    "PluginRejectionCode",
    "main",
]

#: Frozen discovery contract (AC1): plugins are entry points in this
#: group of installed distributions. The group name is the API other
#: plugin authors target; changing it is a contract reconciliation.
ENTRY_POINT_GROUP = "mai.capabilities"

#: Module-level attribute holding the trusted handler callable.
HANDLER_DECLARATION = "execute"

#: Module-level attribute holding the plural capability-declaration
#: sequence: one plugin owning several capability identifiers. Exactly
#: one of ``CAPABILITY`` (singular, 1:1) / ``CAPABILITIES`` (plural,
#: non-empty) may be declared.
CAPABILITIES_DECLARATION = "CAPABILITIES"

#: The distribution shipping the built-in plugins (this package). A
#: runtime composed with ``include_builtins=False`` (the default:
#: fixtures, store diagnostics) skips entry points backed by this
#: distribution before any import; hosts that execute built-ins opt in
#: explicitly.
BUILTIN_DISTRIBUTION = "mai"

#: Module-level attribute holding the credential reference names the
#: handler needs (names only, C03 rule 2/6 — values resolve at effect
#: time through the dispatcher).
CREDENTIAL_DECLARATION = "CREDENTIAL_REFERENCES"

#: Credential-reference shape, frozen by the canonical profile schema
#: (``mai.contracts.server`` ``_CREDENTIAL_REF_RE``; contracts.md C03
#: rule 6: credentials resolve by reference). Kept local: the schema
#: module is the shape owner and outside this card's allowlist; a
#: divergence would be a review finding, not a silent import of a
#: private symbol (registry/dispatcher pattern). Anchored explicitly:
#: pydantic ``pattern`` constraints are search-based, while the schema
#: owner matches with ``fullmatch`` — without ``^…$`` a valid *prefix*
#: would pass (e.g. ``BAD-ref``).
CREDENTIAL_REFERENCE_RE = r"^[A-Z][A-Z0-9_]{0,127}$"

#: Plugin (entry-point) name shape: the capability slug charset (C03
#: rule 1 identifiers are lowercase slugs). Plugin names are
#: configuration keys; the same frozen shape keeps them dispatchable and
#: sortable. Local copy of the registry pattern, same review note.
_PLUGIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def _json_documents_equal(left: object, right: object) -> bool:
    """Strict JSON-document equality: bools are their own type.

    Used for the multi-capability ``config_schema`` identity rule; JSON
    round-trip keeps the comparison key-order independent, and the bool
    guard keeps ``True`` distinct from ``1`` (the plugin store's rule).
    """
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    try:
        return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
    except (TypeError, ValueError):
        return left == right


def _is_builtin_entry_point(ep: EntryPoint) -> bool:
    """Whether one entry point is backed by the host distribution itself.

    Dist-less entry points (never the platform's shape for a discovered
    plugin) are treated as third-party and always load.
    """
    dist = getattr(ep, "dist", None)
    return dist is not None and getattr(dist, "name", None) == BUILTIN_DISTRIBUTION


class NativePluginManifest(BaseModel):
    """Validated manifest envelope of one native plugin module.

    The pydantic layer owns only the native envelope: the capability
    documents are carried opaquely (``dict[str, object]``) and validated
    by :func:`mai.plugins.registry.parse_descriptor` — the C03 rule 1
    vocabulary owner. ``extra="forbid"`` mirrors the descriptor's strict
    vocabulary: an envelope cannot smuggle undeclared fields.

    Exactly one declaration is required: ``capability`` (singular,
    normalized to a one-element ``capabilities`` tuple) or
    ``capabilities`` (plural, non-empty). Every descriptor of a
    multi-capability plugin must declare an identical ``config_schema``
    (one plugin, one configuration form; C3) — divergence is the
    explicit compatibility failure.
    """

    model_config = ConfigDict(extra="forbid")

    capability: dict[str, object] | None = None
    capabilities: tuple[dict[str, object], ...] = ()
    credential_references: tuple[
        Annotated[str, StringConstraints(pattern=CREDENTIAL_REFERENCE_RE)], ...
    ] = ()

    @model_validator(mode="after")
    def _exactly_one_declaration(self) -> "NativePluginManifest":
        if self.capability is not None and self.capabilities:
            raise ValueError(
                "declare exactly one of capability / capabilities "
                "(a plugin owns one declaration form)"
            )
        if self.capability is not None:
            self.capabilities = (self.capability,)
        if not self.capabilities:
            raise ValueError(
                "at least one capability descriptor must be declared "
                "(capability or non-empty capabilities)"
            )
        schemas = [doc.get("config_schema") for doc in self.capabilities]
        first = schemas[0]
        for other in schemas[1:]:
            if not _json_documents_equal(first, other):
                raise ValueError(
                    "all descriptors of a multi-capability plugin must "
                    "declare an identical config_schema"
                )
        return self


class NativePluginErrorCode(StrEnum):
    """Stable machine-readable runtime-construction rejection codes."""

    NOT_DISCOVERED = "not_discovered"
    ALREADY_DISCOVERED = "already_discovered"
    INVALID_WIRING = "invalid_wiring"


class NativePluginError(ValueError):
    """One structured runtime-level rejection (composition errors).

    Raised for wiring mistakes and discovery-state misuse — errors of
    the *host process*, not of one plugin. A broken plugin is never
    raised: it becomes a :class:`PluginRejection` record so one bad
    installation cannot take down the runtime (per-plugin fail-closed).
    """

    def __init__(self, code: NativePluginErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class PluginRejectionCode(StrEnum):
    """Native-owned per-plugin rejection codes.

    Descriptor-stage rejections carry the registry's stable
    :class:`mai.plugins.registry.CapabilityErrorCode` value instead
    (passthrough pattern of the dispatcher's decision reasons).
    """

    INVALID_PLUGIN_NAME = "invalid_plugin_name"
    PLUGIN_MODULE_INVALID = "plugin_module_invalid"
    IMPORT_FAILED = "import_failed"
    MANIFEST_INVALID = "manifest_invalid"
    HANDLER_MISSING = "capability_handler_missing"
    NOT_AGENT_READY = "capability_not_agent_ready"
    IDENTIFIER_CONFLICT = "capability_identifier_conflict"


@dataclass(frozen=True, slots=True)
class PluginRejection:
    """One structured, value-free per-plugin load rejection.

    ``code`` is a :class:`PluginRejectionCode` value or (for the
    descriptor stage) a registry ``CapabilityErrorCode`` value.
    """

    plugin: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class NativePlugin:
    """One loaded, enabled plugin: descriptor(s) plus trusted handler.

    ``handler`` is the module's ``execute`` callable — trusted installed
    code (C03 rule 7). It is reachable only through
    :attr:`NativePluginRuntime.executor`, the dispatcher's executor seam;
    keeping the handler here (unlike the registry's declaration-only
    records) is this runtime's one deliberate code reference.

    ``descriptor_names`` lists every capability identifier the plugin
    owns in declaration order; ``version``/``effect_class`` are the
    primary (first) descriptor's values. ``descriptor_name`` remains the
    primary identifier for backward compatibility with single-capability
    callers.
    """

    plugin: str
    descriptor_names: tuple[str, ...]
    version: str
    effect_class: str
    credential_references: tuple[str, ...]
    handler: Callable[[CapabilityCall], ExecutorResult | Awaitable[ExecutorResult]]

    @property
    def descriptor_name(self) -> str:
        """The primary (first declared) capability identifier."""
        return self.descriptor_names[0]


class NativePluginRuntime:
    """Discovers installed plugins, registers descriptors, binds handlers.

    Composition is constructor-checked (fail-closed wiring, the sibling
    pattern); :meth:`discover` runs once, then all state is immutable.
    The runtime owns no policy, no approvals and no credentials: those
    are the dispatcher's seams (C03 rules 3–6), which is exactly why the
    executor below can be the host's one trusted implementation without
    weakening the boundary.
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        disabled: Collection[str] = (),
        include_builtins: bool = False,
    ) -> None:
        if not isinstance(registry, CapabilityRegistry):
            raise NativePluginError(
                NativePluginErrorCode.INVALID_WIRING,
                "registry must be a CapabilityRegistry",
            )
        if isinstance(disabled, str) or not isinstance(disabled, Collection):
            raise NativePluginError(
                NativePluginErrorCode.INVALID_WIRING,
                "disabled must be a collection of plugin names",
            )
        disabled_names = tuple(disabled)
        if any(not isinstance(name, str) for name in disabled_names):
            raise NativePluginError(
                NativePluginErrorCode.INVALID_WIRING,
                "disabled entries must be plugin-name strings",
            )
        if len(set(disabled_names)) != len(disabled_names):
            raise NativePluginError(
                NativePluginErrorCode.INVALID_WIRING,
                "disabled contains duplicates",
            )
        if not isinstance(include_builtins, bool):
            raise NativePluginError(
                NativePluginErrorCode.INVALID_WIRING,
                "include_builtins must be a boolean",
            )
        self._registry = registry
        self._disabled = frozenset(disabled_names)
        self._include_builtins = include_builtins
        self._discovered = False
        self._plugins: dict[str, NativePlugin] = {}
        self._handlers: dict[str, NativePlugin] = {}
        self._rejections: list[PluginRejection] = []
        self._skipped_disabled: list[str] = []

    def __repr__(self) -> str:
        return (
            f"<NativePluginRuntime discovered={self._discovered} "
            f"plugins={len(self._plugins)} rejections={len(self._rejections)}>"
        )

    # -- discovery -----------------------------------------------------------

    def discover(self) -> None:
        """Load every installed plugin once; register enabled descriptors.

        Per plugin, fail-closed stages: name shape → import → declaration
        → manifest envelope → descriptor vocabulary → handler → identifier
        ownership. A failure at any stage records a
        :class:`PluginRejection` and skips that plugin; the runtime and
        the other plugins are unaffected. Configured-disabled names are
        skipped before any import (a disabled plugin's code never runs).

        Entry-point names are unique per discovered set by platform
        contract: when two installed distributions declare the same
        plugin name, ``importlib.metadata`` exposes only the first on
        ``sys.path`` — the same precedence as Python module shadowing.
        The runtime loads exactly what the platform exposes and never
        re-implements that selection.
        """
        if self._discovered:
            raise NativePluginError(
                NativePluginErrorCode.ALREADY_DISCOVERED,
                "runtime already discovered; compose a new runtime to "
                "change the disabled set or pick up an installation",
            )
        self._discovered = True
        found = sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda ep: ep.name)
        for ep in found:
            name = ep.name
            if not self._include_builtins and _is_builtin_entry_point(ep):
                continue
            if not isinstance(name, str) or not _PLUGIN_NAME_RE.fullmatch(name):
                self._rejections.append(
                    PluginRejection(
                        plugin=name,
                        code=PluginRejectionCode.INVALID_PLUGIN_NAME.value,
                        detail=f"{name!r} must match {_PLUGIN_NAME_RE.pattern}",
                    )
                )
                continue
            if name in self._disabled:
                self._skipped_disabled.append(name)
                continue
            self._load_one(name, ep)

    def _reject(self, plugin: str, code: str, detail: str) -> None:
        self._rejections.append(PluginRejection(plugin=plugin, code=code, detail=detail))

    def _load_one(self, name: str, ep: EntryPoint) -> None:
        try:
            module = ep.load()
        except Exception as error:  # trusted code: type name only
            self._reject(
                name,
                PluginRejectionCode.IMPORT_FAILED.value,
                f"{name}: import raised {type(error).__name__}",
            )
            return
        if not isinstance(module, ModuleType):
            self._reject(
                name,
                PluginRejectionCode.PLUGIN_MODULE_INVALID.value,
                f"{name}: entry point value must be a module path, "
                f"resolved to {type(module).__name__}",
            )
            return
        declaration = getattr(module, CAPABILITY_DECLARATION, None)
        plural_declaration = getattr(module, CAPABILITIES_DECLARATION, None)
        if declaration is None and plural_declaration is None:
            self._reject(
                name,
                CapabilityErrorCode.DECLARATION_MISSING.value,
                f"{name}: module does not declare {CAPABILITY_DECLARATION} "
                f"or {CAPABILITIES_DECLARATION}",
            )
            return
        document: dict[str, object] = {
            "credential_references": list(
                getattr(module, CREDENTIAL_DECLARATION, ()) or ()
            ),
        }
        if declaration is not None:
            document["capability"] = declaration
        if plural_declaration is not None:
            document["capabilities"] = plural_declaration
        try:
            manifest = NativePluginManifest.model_validate(document)
        except ValidationError as error:
            # Value-free: pydantic messages quote input content; report
            # only the model title and error count.
            self._reject(
                name,
                PluginRejectionCode.MANIFEST_INVALID.value,
                f"{name}: manifest rejected ({error.title}, "
                f"{error.error_count()} validation error(s))",
            )
            return
        if len(set(manifest.credential_references)) != len(
            manifest.credential_references
        ):
            self._reject(
                name,
                PluginRejectionCode.MANIFEST_INVALID.value,
                f"{name}: {CREDENTIAL_DECLARATION} contains duplicates",
            )
            return
        # Multi-capability plugins are the agent-facing surface (C1):
        # every descriptor must carry LLM-ready tool and parameter
        # descriptions. The legacy singular declaration keeps its
        # historical leniency (synthetic fixtures, in-repo experiments).
        enforce_agent_ready = declaration is None
        descriptors: list[CapabilityDescriptor] = []
        for capability_document in manifest.capabilities:
            try:
                descriptor = parse_descriptor(capability_document, source=name)
            except CapabilityError as error:
                self._reject(name, error.code.value, error.detail)
                return
            if enforce_agent_ready:
                try:
                    assert_agent_ready_descriptor(descriptor)
                except ValueError as error:
                    self._reject(
                        name,
                        PluginRejectionCode.NOT_AGENT_READY.value,
                        f"{name}: {error}",
                    )
                    return
            descriptors.append(descriptor)
        handler = getattr(module, HANDLER_DECLARATION, None)
        if not callable(handler):
            self._reject(
                name,
                PluginRejectionCode.HANDLER_MISSING.value,
                f"{name}: module does not declare a callable "
                f"{HANDLER_DECLARATION}",
            )
            return
        # One owner per capability identifier — within the plugin and
        # across the discovered set. Checked for every descriptor before
        # the first registration, so a rejected plugin never leaves a
        # partial registration behind (atomic per plugin, AC2).
        claimed: set[str] = set()
        for descriptor in descriptors:
            if descriptor.identifier in claimed or descriptor.identifier in self._handlers:
                self._reject(
                    name,
                    PluginRejectionCode.IDENTIFIER_CONFLICT.value,
                    f"{name}: identifier {descriptor.identifier!r} is already "
                    "bound to another plugin; one owner per capability "
                    "identifier",
                )
                return
            claimed.add(descriptor.identifier)
        for descriptor in descriptors:
            self._registry.register(descriptor)
        primary = descriptors[0]
        plugin = NativePlugin(
            plugin=name,
            descriptor_names=tuple(d.identifier for d in descriptors),
            version=primary.version,
            effect_class=primary.effect_class,
            credential_references=manifest.credential_references,
            handler=handler,
        )
        self._plugins[name] = plugin
        for descriptor in descriptors:
            self._handlers[descriptor.identifier] = plugin

    # -- state ----------------------------------------------------------------

    def _require_discovered(self) -> None:
        if not self._discovered:
            raise NativePluginError(
                NativePluginErrorCode.NOT_DISCOVERED,
                "runtime has not discovered plugins yet; call discover() "
                "first (an undiscovered runtime must not look like an "
                "empty success)",
            )

    @property
    def plugins(self) -> tuple[NativePlugin, ...]:
        """Enabled plugins, sorted by plugin name."""
        self._require_discovered()
        return tuple(self._plugins[name] for name in sorted(self._plugins))

    @property
    def rejections(self) -> tuple[PluginRejection, ...]:
        """Structured per-plugin load rejections (order of discovery)."""
        self._require_discovered()
        return tuple(self._rejections)

    @property
    def skipped_disabled(self) -> tuple[str, ...]:
        """Plugin names found installed but configured off (load-time)."""
        self._require_discovered()
        return tuple(self._skipped_disabled)

    @property
    def credential_references(self) -> tuple[str, ...]:
        """Union of enabled plugins' credential reference names, sorted.

        The host passes these names into the
        :class:`~mai.policy.evaluate.ExecutionContext` — references only
        (C03 rule 2); values resolve at effect time in the dispatcher's
        credential seam (C03 rule 6).
        """
        self._require_discovered()
        names: set[str] = set()
        for plugin in self._plugins.values():
            names.update(plugin.credential_references)
        return tuple(sorted(names))

    # -- the one execution seam -----------------------------------------------

    @property
    def executor(self) -> Callable[[CapabilityCall], Awaitable[ExecutorResult]]:
        """The dispatcher-facing executor (C03 rule 7).

        Wire into ``CapabilityDispatcher(executor=...)``. Routes an
        allowed, rechecked dispatch to the owning plugin's handler; a
        capability without an enabled plugin (disabled after a registry
        projection, or registered by another composition) answers a
        structured ``ok=False`` instead of executing. Handler exceptions
        and contract violations are classified honestly by the dispatcher
        (``executor_failed``/``executor_contract_violation``,
        ``effect="unknown"``) — this wrapper adds nothing to hide.
        """
        self._require_discovered()
        handlers = self._handlers

        async def execute_native(call: CapabilityCall) -> ExecutorResult:
            plugin = handlers.get(call.descriptor.identifier)
            if plugin is None:
                return ExecutorResult(
                    ok=False,
                    output=(
                        f"capability {call.descriptor.identifier} has no "
                        "enabled native plugin"
                    ),
                )
            result = plugin.handler(call)
            if isinstance(result, ExecutorResult):
                return result
            return await result

        return execute_native


# ---------------------------------------------------------------------------
# Declarative CLI (no execution surface)
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mai.plugins.native",
        description=(
            "Inspect installed native capability plugins (entry-point "
            f"group {ENTRY_POINT_GROUP}). Structured JSON only; this CLI "
            "validates and lists declarations and never executes a "
            "capability — execution exists only through the dispatcher."
        ),
    )
    parser.add_argument("command", choices=["inspect"])
    parser.add_argument(
        "--no-builtins",
        action="store_true",
        help=(
            "skip the host distribution's own built-in plugins and list "
            "only third-party installed plugins"
        ),
    )
    return parser


def main(argv: Collection[str] | None = None) -> int:
    """CLI entrypoint: 0 when every discovered plugin loaded, else 1."""
    args = _build_parser().parse_args(argv)
    registry = CapabilityRegistry()
    runtime = NativePluginRuntime(
        registry=registry,
        include_builtins=not getattr(args, "no_builtins", False),
    )
    runtime.discover()
    payload = {
        "group": ENTRY_POINT_GROUP,
        "plugins": [
            {
                "plugin": plugin.plugin,
                "identifier": plugin.descriptor_name,
                "descriptor_names": list(plugin.descriptor_names),
                "version": plugin.version,
                "effect_class": plugin.effect_class,
                "credential_references": list(plugin.credential_references),
            }
            for plugin in runtime.plugins
        ],
        "rejections": [
            {
                "plugin": rejection.plugin,
                "code": rejection.code,
                "detail": rejection.detail,
            }
            for rejection in runtime.rejections
        ],
        "skipped_disabled": list(runtime.skipped_disabled),
    }
    print(json.dumps(payload))
    return 1 if runtime.rejections else 0


if __name__ == "__main__":
    raise SystemExit(main())
