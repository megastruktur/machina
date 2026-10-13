/**
 * Typed panel registry — card `01-panel-registry`, P01.
 *
 * Producer of the registry/host layer (windowed-workspace ADR-0001 §2 layer
 * table, §7 "Panel registration and lifecycle", D6; contracts.md C11):
 * validates typed feature-owned panel entries and freezes them into a
 * centrally lookup-able map. `PanelHost.svelte` renders one live window's
 * panel body through a resolved entry of this registry.
 *
 * Registration model (ADR §7.1, binding):
 *  - Entries are explicit, feature-owned code (a fixed, trusted component
 *    map). The shell never imports not-yet-existing feature modules and no
 *    per-feature shell edit is needed: a feature ships its entry, the app
 *    composes entries into one registry.
 *  - `component` is a LOADER FUNCTION — a statically analyzable lazy import
 *    written by the owning feature (`() => import('…/FeaturePanel.svelte')`).
 *    It is never a path string, URL or persisted-state value: entries carrying
 *    anything but a function are rejected (`bad_component`), and the host
 *    resolves components exclusively through this registry — never from
 *    descriptor/layout/URL data (ADR §10.2 NT4 "no dynamic import from data").
 *  - `capabilities_note` is descriptive metadata only. Registration is NOT an
 *    authorization grant: the entry vocabulary has no permission/grant field
 *    (closed set — `unknown_field`), and nothing in this module or the host
 *    branches on capabilities.
 *  - A `panel_kind` may be registered but disabled (`enabled: false`, e.g. a
 *    feature flag). Unknown AND disabled kinds are the same safe "unavailable
 *    panel" error downstream (ADR §7.1), never an import attempt.
 *
 * Validation results use a registry-local issue vocabulary. The descriptor
 * `RejectCode` set in `./types.js` governs the persisted-window path and is
 * intentionally not stretched here; `validateGeometry` is reused for the
 * geometry fields so the integer/positivity rules stay single-sourced.
 *
 * SSR isolation (ADR §5 rule 2): this module has no module-level mutable
 * state — the only exports are types, pure validators and the
 * `createPanelRegistry` factory. Every registry instance is frozen and
 * per-call; server-side module evaluation shares nothing between requests.
 */

import type { Component } from 'svelte';
import {
	DEFAULT_MIN_GEOMETRY,
	SCOPE_OBJECT_KINDS,
	containsForbiddenContent,
	validateGeometry,
	type PanelKind,
	type ScopeObjectKind,
	type WindowDescriptor,
	type WindowGeometry,
	type WindowScope
} from './types.js';

/** Maximum stored length for registry display text (same bound as overrides). */
const MAX_REGISTRY_TEXT_LENGTH = 200;

/** Same slug shape as the descriptor-level `PANEL_KIND_RE` (ADR §7.1 slugs). */
const PANEL_KIND_RE = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;

// ---------------------------------------------------------------------------
// Validation results (registry-local vocabulary)
// ---------------------------------------------------------------------------

/** Stable reject codes for panel entry/registry/scope validation. */
export type RegistryIssueCode =
	| 'not_an_object'
	| 'bad_panel_kind'
	| 'bad_title'
	| 'bad_scope_types'
	| 'bad_scope_object_kinds'
	| 'bad_component'
	| 'bad_geometry'
	| 'bad_singleton'
	| 'bad_keep_mounted_when_hidden'
	| 'bad_enabled'
	| 'bad_capabilities_note'
	| 'unknown_field'
	| 'duplicate_panel_kind'
	| 'empty_entries'
	| 'unknown_panel_kind'
	| 'panel_disabled'
	| 'scope_kind_not_allowed'
	| 'scope_object_kind_not_allowed';

export interface RegistryIssue {
	code: RegistryIssueCode;
	/** Safe for a visible error view; never echoes rejected values. */
	message: string;
}

export type RegistryValidated<T> = { ok: true; value: T } | { ok: false; issue: RegistryIssue };

function issue<T = never>(code: RegistryIssueCode, message: string): RegistryValidated<T> {
	return { ok: false, issue: { code, message } };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Registry-safe free text: bounded, no forbidden content classes (ADR §8 rule 1). */
function isValidText(value: unknown): value is string {
	return (
		typeof value === 'string' &&
		value.trim().length > 0 &&
		value.length <= MAX_REGISTRY_TEXT_LENGTH &&
		!containsForbiddenContent(value)
	);
}

// ---------------------------------------------------------------------------
// Panel-facing contracts (explicit context — never globals)
// ---------------------------------------------------------------------------

/**
 * Explicit services bag handed to a panel by its host caller. Deliberately an
 * opaque typed record: the registry/host never constructs, fetches or falls
 * back to a global service locator — every panel receives exactly what its
 * opener passes (ADR §2: panels must not read an unqualified global).
 */
export type PanelServices = { readonly [name: string]: unknown };

/**
 * The complete explicit context of one mounted panel body (ADR §7.2 "Panel
 * receives explicit scope/services, isolated error and lifecycle").
 * `descriptor` is the validated live schema-v1 descriptor; its `scope` is the
 * pinned target — view identity only, never a mutable "current server".
 */
export interface PanelContext {
	readonly descriptor: WindowDescriptor;
	readonly scope: WindowScope;
	readonly services: PanelServices;
	/**
	 * Registers one teardown hook run exactly once when this panel CONTENT
	 * unmounts — on hide (minimized, default `keep_mounted_when_hidden`),
	 * close, or host removal (ADR §7.2: hidden/closed release subscriptions).
	 * Registration after the content unmounted is a no-op (returns an inert
	 * unregister). LIFO order; a throwing teardown is contained and can never
	 * skip the remaining hooks. This is the mount-scoped half of the release
	 * contract; window-close-time hooks stay with the workspace state layer
	 * (`open().release`) — no duplicated window state.
	 */
	onCleanup(hook: () => void): () => void;
}

/**
 * A mounted panel component. Every panel takes the same single `panel` prop —
 * the explicit context above — and nothing else implicit.
 */
export type PanelComponent = Component<{ panel: PanelContext }>;

/**
 * Lazy component loader: a feature-owned, statically analyzable dynamic
 * import (`() => import('…/Panel.svelte')`). The registry validates only that
 * it is a function; the host calls it exclusively from a validated entry.
 */
export type PanelComponentLoader = () => Promise<PanelComponent>;

/** Scope kinds a panel kind may be opened with (ADR §7.1 `scope_types`). */
export type PanelScopeType = 'server' | 'global';

const SCOPE_TYPES = Object.freeze(['server', 'global'] as const);

// ---------------------------------------------------------------------------
// Entry + registry
// ---------------------------------------------------------------------------

/** Validated registry entry — the frozen §7.1 field set, nothing else. */
export interface PanelRegistryEntry {
	readonly panel_kind: PanelKind;
	/** Display title key (vocabulary resolved by callers for display). */
	readonly title: string;
	readonly scope_types: ReadonlySet<PanelScopeType>;
	/**
	 * Allowed pinned object kinds for server scope. Empty/omitted ⇒ server
	 * windows of this kind open unpinned; a descriptor pinning an object is
	 * rejected at the open check.
	 */
	readonly scope_object_kinds: ReadonlySet<ScopeObjectKind>;
	readonly component: PanelComponentLoader;
	readonly default_geometry: WindowGeometry | null;
	readonly min_geometry: WindowGeometry | null;
	readonly singleton: boolean;
	readonly keep_mounted_when_hidden: boolean;
	/** Registered-but-disabled kinds produce the safe unavailable error. */
	readonly enabled: boolean;
	/** Descriptive metadata only — read by humans, never by code paths. */
	readonly capabilities_note: string | null;
}

/** Raw entry shape accepted by `definePanelEntry` (sets may be arrays). */
export interface PanelEntryInput {
	panel_kind: PanelKind;
	title: string;
	scope_types: Iterable<PanelScopeType>;
	scope_object_kinds?: Iterable<ScopeObjectKind>;
	component: PanelComponentLoader;
	default_geometry?: WindowGeometry;
	min_geometry?: WindowGeometry;
	singleton?: boolean;
	keep_mounted_when_hidden?: boolean;
	enabled?: boolean;
	capabilities_note?: string;
}

const PANEL_ENTRY_FIELDS = Object.freeze([
	'panel_kind',
	'title',
	'scope_types',
	'scope_object_kinds',
	'component',
	'default_geometry',
	'min_geometry',
	'singleton',
	'keep_mounted_when_hidden',
	'enabled',
	'capabilities_note'
] as const);

function toFrozenSet<T extends string>(values: Iterable<T>): ReadonlySet<T> {
	return Object.freeze(new Set(values));
}

/**
 * Validates one raw panel entry against the frozen closed vocabulary and
 * returns the normalized entry. Unknown fields are rejected (`unknown_field`)
 * — the entry shape cannot smuggle grants/permissions or alternate component
 * references (registration grants no permissions, ADR §7.1).
 */
export function definePanelEntry(raw: unknown): RegistryValidated<PanelRegistryEntry> {
	if (!isPlainObject(raw)) return issue('not_an_object', 'panel entry must be an object');

	for (const key of Object.keys(raw)) {
		if (!PANEL_ENTRY_FIELDS.includes(key as (typeof PANEL_ENTRY_FIELDS)[number])) {
			return issue('unknown_field', `unknown panel entry field: ${key}`);
		}
	}

	if (typeof raw.panel_kind !== 'string' || !PANEL_KIND_RE.test(raw.panel_kind)) {
		return issue('bad_panel_kind', 'panel_kind must be a stable lowercase slug');
	}
	if (!isValidText(raw.title)) {
		return issue('bad_title', 'title must be a non-empty bounded display key');
	}

	if (typeof raw.scope_types !== 'object' || raw.scope_types === null) {
		return issue('bad_scope_types', 'scope_types must list "server" and/or "global"');
	}
	const scopeTypes = toFrozenSet([...(raw.scope_types as Iterable<PanelScopeType>)]);
	if (scopeTypes.size === 0) {
		return issue('bad_scope_types', 'scope_types must not be empty');
	}
	for (const scopeType of scopeTypes) {
		if (!SCOPE_TYPES.includes(scopeType)) {
			return issue('bad_scope_types', `scope_types entries must be "server" or "global"`);
		}
	}

	let scopeObjectKinds = toFrozenSet([] as ScopeObjectKind[]);
	if (raw.scope_object_kinds !== undefined) {
		if (typeof raw.scope_object_kinds !== 'object' || raw.scope_object_kinds === null) {
			return issue('bad_scope_object_kinds', 'scope_object_kinds must be a list when present');
		}
		scopeObjectKinds = toFrozenSet([...(raw.scope_object_kinds as Iterable<ScopeObjectKind>)]);
		for (const objectKind of scopeObjectKinds) {
			if (!SCOPE_OBJECT_KINDS.includes(objectKind)) {
				return issue('bad_scope_object_kinds', 'scope_object_kinds entry is not a known kind');
			}
		}
		if (scopeObjectKinds.size > 0 && !scopeTypes.has('server')) {
			return issue(
				'bad_scope_object_kinds',
				'scope_object_kinds require the "server" scope type'
			);
		}
	}

	// The load-bearing boundary: the component slot accepts a loader FUNCTION
	// only. Strings/URLs/paths are rejected — a persisted or URL-borne import
	// target can never enter the trusted map (ADR §7.1, §10.2 NT4).
	if (typeof raw.component !== 'function') {
		return issue('bad_component', 'component must be a lazy loader function');
	}
	const component = raw.component as PanelComponentLoader;

	let defaultGeometry: WindowGeometry | null = null;
	if (raw.default_geometry !== undefined) {
		const geometry = validateGeometry(raw.default_geometry);
		if (!geometry.ok) return issue('bad_geometry', 'default_geometry fails integer/positivity');
		defaultGeometry = geometry.value;
	}
	let minGeometry: WindowGeometry | null = null;
	if (raw.min_geometry !== undefined) {
		const geometry = validateGeometry(raw.min_geometry);
		if (!geometry.ok) return issue('bad_geometry', 'min_geometry fails integer/positivity');
		minGeometry = geometry.value;
	}
	if (defaultGeometry !== null && minGeometry !== null) {
		if (
			defaultGeometry.width < minGeometry.width ||
			defaultGeometry.height < minGeometry.height
		) {
			return issue('bad_geometry', 'default_geometry must not be smaller than min_geometry');
		}
	}

	if (raw.singleton !== undefined && typeof raw.singleton !== 'boolean') {
		return issue('bad_singleton', 'singleton must be a boolean when present');
	}
	if (
		raw.keep_mounted_when_hidden !== undefined &&
		typeof raw.keep_mounted_when_hidden !== 'boolean'
	) {
		return issue('bad_keep_mounted_when_hidden', 'keep_mounted_when_hidden must be a boolean');
	}
	if (raw.enabled !== undefined && typeof raw.enabled !== 'boolean') {
		return issue('bad_enabled', 'enabled must be a boolean when present');
	}
	if (raw.capabilities_note !== undefined && !isValidText(raw.capabilities_note)) {
		return issue('bad_capabilities_note', 'capabilities_note must be bounded safe text');
	}

	return {
		ok: true,
		value: {
			panel_kind: raw.panel_kind,
			title: raw.title,
			scope_types: scopeTypes,
			scope_object_kinds: scopeObjectKinds,
			component,
			default_geometry: defaultGeometry,
			min_geometry: minGeometry,
			singleton: raw.singleton ?? false,
			keep_mounted_when_hidden: raw.keep_mounted_when_hidden ?? false,
			enabled: raw.enabled ?? true,
			capabilities_note: raw.capabilities_note ?? null
		}
	};
}

/**
 * Read-only panel registry: a frozen, centrally lookup-able map of validated
 * entries (ADR §7.1 "fixed, trusted component map"). One instance per
 * composition; no module-level singleton exists (ADR §5 rule 2).
 */
export interface PanelRegistry {
	/** Validated entries in registration order. */
	entries(): readonly PanelRegistryEntry[];
	/** The entry for `kind`, or `null` when unknown. Disabled entries return. */
	entry(kind: PanelKind): PanelRegistryEntry | null;
	/** Every registered kind — the `knownPanelKinds` input of the workspace state. */
	knownPanelKinds(): ReadonlySet<PanelKind>;
	/**
	 * Frozen per-open scope check (ADR §7.1/§7.2 "opening"): kind must be
	 * known and enabled; the scope kind must be declared; server scope objects
	 * must be allowed by the entry's `scope_object_kinds`. Returns the scope
	 * unchanged (targets are pinned by the state layer, never rewritten here).
	 */
	checkScope(kind: PanelKind, scope: WindowScope): RegistryValidated<WindowScope>;
	/** Entry default geometry, or `null` when the entry omits it (openers supply). */
	defaultGeometry(kind: PanelKind): WindowGeometry | null;
	/** Entry minimum geometry, or the frozen global default (ADR §10.2 NT3). */
	minGeometry(kind: PanelKind): Readonly<WindowGeometry>;
}

/**
 * Free-instance policy key: non-singleton kinds never deduplicate (`null`);
 * singleton kinds deduplicate per full pinned scope (global, or server +
 * object identity), so "at most one live instance" never conflates two
 * targets (windowed ADR §7.1 `singleton`, §3.2 pinned scope). Consumed as the
 * `instanceKey` of `createWorkspaceState`.
 */
export function singletonInstanceKey(
	kind: PanelKind,
	entry: PanelRegistryEntry,
	scope: WindowScope
): string | null {
	if (!entry.singleton) return null;
	if (scope.kind === 'global') return `${kind}::global`;
	const object = scope.object ? `/${scope.object.kind}/${scope.object.id}` : '';
	return `${kind}::server/${scope.server_id}${object}`;
}

/**
 * Validates the entries and freezes them into one registry. Duplicate panel
 * kinds are rejected — the map is trusted precisely because each kind has
 * exactly one feature-owned entry.
 */
export function createPanelRegistry(
	entries: readonly unknown[]
): RegistryValidated<PanelRegistry> {
	if (!Array.isArray(entries) || entries.length === 0) {
		return issue('empty_entries', 'a panel registry needs at least one entry');
	}
	const validated: PanelRegistryEntry[] = [];
	const byKind = new Map<PanelKind, PanelRegistryEntry>();
	for (const raw of entries) {
		const entry = definePanelEntry(raw);
		if (!entry.ok) return entry;
		if (byKind.has(entry.value.panel_kind)) {
			return issue(
				'duplicate_panel_kind',
				`panel_kind already registered: ${entry.value.panel_kind}`
			);
		}
		byKind.set(entry.value.panel_kind, entry.value);
		validated.push(entry.value);
	}

	const registry: PanelRegistry = {
		entries: () => validated,
		entry(kind) {
			return byKind.get(kind) ?? null;
		},
		knownPanelKinds: () => toFrozenSet(byKind.keys()),
		checkScope(kind, scope) {
			const entry = byKind.get(kind);
			if (!entry) {
				return issue('unknown_panel_kind', 'this panel is not available');
			}
			if (!entry.enabled) {
				return issue('panel_disabled', 'this panel is not available');
			}
			if (!entry.scope_types.has(scope.kind as PanelScopeType)) {
				return issue('scope_kind_not_allowed', 'this panel cannot open with this scope');
			}
			if (scope.kind === 'server' && scope.object) {
				if (!entry.scope_object_kinds.has(scope.object.kind)) {
					return issue('scope_object_kind_not_allowed', 'this panel cannot pin this object');
				}
			}
			return { ok: true, value: scope };
		},
		defaultGeometry(kind) {
			return byKind.get(kind)?.default_geometry ?? null;
		},
		minGeometry(kind) {
			const entryMin = byKind.get(kind)?.min_geometry;
			// The frozen constant's declared type carries literal members; the
			// contract only promises the readonly shape.
			return entryMin ?? (DEFAULT_MIN_GEOMETRY as Readonly<WindowGeometry>);
		}
	};
	return { ok: true, value: registry };
}
