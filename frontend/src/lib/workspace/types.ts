/**
 * Window descriptor and panel-scope contracts — persisted schema version 1.
 *
 * Producer card `01-window-types`. Implements the frozen descriptor schema and
 * validation rules of the windowed-workspace ADR (§3 identity/scope, §8
 * serialization rules, §10.2 NT3/NT4 bounds + hostile restore, §11 good/bad
 * fixtures) and contracts.md C01 (opaque UUID identity, §C01 rule 1) / C08
 * rule 5 + C11 (explicit pinned scope, never inferred from a global
 * selection).
 *
 * This module is pure and context-free by contract (ADR §5 rule 4):
 * - no browser API at module top level (safe to import in any SvelteKit
 *   environment; workspace runtime state is owned by the later
 *   window-state/window-layout cards, not here),
 * - no module-level mutable state, so no SSR-shared singleton and no hidden
 *   "current server" mutation target (ADR §5 rule 2, NT5),
 * - no dynamic import of any kind — a hostile `panel_kind` can never cause an
 *   import attempt (ADR §7.1, §11).
 *
 * Whether a panel kind is registered, and whether a scope object exists and is
 * authorized, is decided at open/restore time by the panel registry and domain
 * state (ADR §7.2 `opening`); this module takes the known panel kinds as an
 * explicit parameter on every call and never resolves a target itself.
 *
 * Validation issues carry stable machine codes plus human messages that are
 * safe for restore notices (ADR §10.2 NT4: dropped entries get a visible,
 * understandable notice). Messages never echo rejected free-text content.
 */

/** Opaque client-generated view identity (ADR §3.1). Never a target id. */
export type WindowId = string;

/** Server identity as carried in a validated scope (contracts.md C01 rule 1). */
export type ServerId = string;

/**
 * Stable panel slug (`"chat"`, `"server-profile"`, …), validated against the
 * frozen registry set on every open/restore (ADR §7.1). The registry itself is
 * owned by `01-panel-registry`; this module only validates membership.
 */
export type PanelKind = string;

/** Registry-declared scope object kinds a server-scoped window may pin (ADR §3.2). */
export const SCOPE_OBJECT_KINDS = Object.freeze([
	'application',
	'run',
	'document',
	'conversation'
] as const);
export type ScopeObjectKind = (typeof SCOPE_OBJECT_KINDS)[number];

/** Presentation states of the window frame (ADR §7.2 lifecycle, §8 example). */
export const PRESENTATIONS = Object.freeze(['normal', 'minimized', 'maximized'] as const);
export type Presentation = (typeof PRESENTATIONS)[number];

/** Typed object reference pinned inside a server scope (ADR §3.2). */
export interface ScopeObject {
	kind: ScopeObjectKind;
	id: string;
}

/** Explicit server scope: a validated `server_id` plus optional pinned object. */
export interface ServerWindowScope {
	kind: 'server';
	server_id: ServerId;
	object?: ScopeObject;
}

/** Explicit global scope: a declared global settings view, no server target. */
export interface GlobalWindowScope {
	kind: 'global';
}

export type WindowScope = ServerWindowScope | GlobalWindowScope;

/**
 * Safe geometry: integers only (ADR §8 rule 2 — non-integers/NaN are rejected
 * at restore). Sizes must be positive; sizes below a registry entry's
 * `min_geometry` and offscreen positions are clamped by the restore path
 * (ADR §10.1 PT6, §10.2 NT3), not rejected here.
 */
export interface WindowGeometry {
	x: number;
	y: number;
	width: number;
	height: number;
}

/**
 * Persisted/serializable window descriptor, schema version 1 (ADR §8). The
 * normalized shape below is exactly what parse/serialize emit — unknown fields
 * within the same major version are ignored on load and never propagated
 * (ADR §8 rule 2).
 */
export interface WindowDescriptor {
	window_id: WindowId;
	panel_kind: PanelKind;
	scope: WindowScope;
	geometry: WindowGeometry;
	presentation: Presentation;
	z: number;
	title_override: string | null;
}

/** Frozen persisted-layout schema version (ADR §8; unknown major ⇒ discard). */
export const SCHEMA_VERSION = 1 as const;

/**
 * Stored layout envelope. `revision` is the layout document's version for the
 * last-writer-wins multi-tab persistence model (ADR §5 rule 6); contract
 * records carry a revision (contracts.md C01 rule 6).
 */
export interface StoredLayout {
	schema_version: typeof SCHEMA_VERSION;
	revision: number;
	windows: WindowDescriptor[];
}

/** NT3 frozen constant: minimum visible, hit-testable title-bar area. */
export const TITLE_BAR_MIN = Object.freeze({ width: 64, height: 32 });

/**
 * Global default minimum window size when a registry entry omits
 * `min_geometry` (ADR §10.1 PT3, §10.2 NT3).
 */
export const DEFAULT_MIN_GEOMETRY = Object.freeze({ width: 320, height: 200 });

/**
 * Free text in layout state is a short display label only (ADR §8 rule 1:
 * log bodies, prompts and conversation text are domain payload and forbidden).
 */
export const MAX_TITLE_OVERRIDE_LENGTH = 200;

// ---------------------------------------------------------------------------
// Validation results
// ---------------------------------------------------------------------------

/**
 * Stable reject codes. `scope_server_without_server_id` mirrors the §11 bad
 * fixture; `unknown_panel_kind` the "unavailable panel" row; `forbidden_content`
 * the severity-1 secret/payload row.
 */
export type RejectCode =
	| 'not_an_object'
	| 'bad_window_id'
	| 'bad_panel_kind'
	| 'unknown_panel_kind'
	| 'bad_scope'
	| 'scope_server_without_server_id'
	| 'bad_server_id'
	| 'bad_object_ref'
	| 'bad_geometry'
	| 'bad_presentation'
	| 'bad_z'
	| 'bad_title_override'
	| 'forbidden_content'
	| 'bad_revision'
	| 'duplicate_window_id';

export interface ValidationIssue {
	code: RejectCode;
	/** Safe for a visible restore notice; never echoes rejected text content. */
	message: string;
}

export type Validated<T> = { ok: true; value: T } | { ok: false; issue: ValidationIssue };

function fail<T = never>(code: RejectCode, message: string): Validated<T> {
	return { ok: false, issue: { code, message } };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

// ---------------------------------------------------------------------------
// Identity (ADR §3.1, contracts.md C01 rule 1)
// ---------------------------------------------------------------------------

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Opaque stable UUIDs are the only identity; labels, names and IPs are not. */
function isUuid(value: unknown): value is string {
	return typeof value === 'string' && UUID_RE.test(value);
}

/**
 * The single auditable producer of view identity: a client-generated opaque
 * UUID (ADR §3.1), namespace-distinct from `server_id`/`run_id`/
 * `application_id`/`conversation_id` by construction — it identifies a view,
 * never a target or a job.
 */
export function createWindowId(): WindowId {
	const uuid = globalThis.crypto?.randomUUID?.();
	if (uuid === undefined) {
		throw new Error(
			'window_id requires crypto.randomUUID (secure browser context or Node 19+)'
		);
	}
	return uuid;
}

// ---------------------------------------------------------------------------
// Forbidden content classes (ADR §8 rule 1, §11 severity-1 row)
// ---------------------------------------------------------------------------

/**
 * Content classes that must never enter workspace/layout state: credentials,
 * tokens, password values, key material, and by length cap any domain payload
 * (log bodies, prompts, conversation text). Applied to `title_override` — the
 * only free-text field of the descriptor — and, as defense in depth, to the
 * serialized document as a whole.
 */
const FORBIDDEN_CONTENT_PATTERNS: ReadonlyArray<RegExp> = [
	// credential-style assignments: token=…, password: …, api_key = …
	/\b(?:token|password|passwd|pwd|secret|api[-_]?key|apikey|access[-_]?key|private[-_]?key|client[-_]?secret|authorization)\b\s*[=:]\s*\S/i,
	// provider/credential token shapes
	/\bsk-[A-Za-z0-9_-]{8,}/,
	/\bgh[pousr]_[A-Za-z0-9]{8,}/,
	/\bgithub_pat_[A-Za-z0-9_]{8,}/,
	/\bxox[baprs]-[A-Za-z0-9-]{8,}/,
	/\bAKIA[0-9A-Z]{16}\b/,
	// bearer/authorization values and JWT-shaped blobs
	/\bbearer\s+[A-Za-z0-9._~+/=-]{8,}/i,
	/\beyJ[A-Za-z0-9_-]{20,}\b/,
	// PEM key material
	/-----BEGIN [A-Z ]*PRIVATE KEY-----/
];

/** True when the text matches a forbidden content class (ADR §8 rule 1). */
export function containsForbiddenContent(text: string): boolean {
	return FORBIDDEN_CONTENT_PATTERNS.some((pattern) => pattern.test(text));
}

// ---------------------------------------------------------------------------
// Field validators
// ---------------------------------------------------------------------------

/** Validates one scope, requiring an explicit target (ADR §3.2, §11 bad set). */
export function validateScope(raw: unknown): Validated<WindowScope> {
	if (!isPlainObject(raw)) {
		return fail('bad_scope', 'window scope must be an object with kind "server" or "global"');
	}
	if (raw.kind === 'server') {
		if (raw.server_id === undefined) {
			return fail(
				'scope_server_without_server_id',
				'server-scoped windows require an explicit server_id; the target is never inferred from a global selection'
			);
		}
		if (!isUuid(raw.server_id)) {
			return fail('bad_server_id', 'server_id must be an opaque UUID (labels, names and IPs are never identity)');
		}
		let object: ScopeObject | undefined;
		if (raw.object !== undefined) {
			const ref = validateObjectRef(raw.object);
			if (!ref.ok) return ref;
			object = ref.value;
		}
		return { ok: true, value: object ? { kind: 'server', server_id: raw.server_id, object } : { kind: 'server', server_id: raw.server_id } };
	}
	if (raw.kind === 'global') {
		return { ok: true, value: { kind: 'global' } };
	}
	return fail('bad_scope', 'window scope kind must be "server" or "global"');
}

/** Validates a typed scope object reference (ADR §3.2 scope object kinds). */
export function validateObjectRef(raw: unknown): Validated<ScopeObject> {
	if (!isPlainObject(raw)) {
		return fail('bad_object_ref', 'scope object reference must be an object');
	}
	const kind = SCOPE_OBJECT_KINDS.find((candidate) => candidate === raw.kind);
	if (kind === undefined) {
		return fail(
			'bad_object_ref',
			`scope object kind must be one of: ${SCOPE_OBJECT_KINDS.join(', ')}`
		);
	}
	if (!isUuid(raw.id)) {
		return fail('bad_object_ref', 'scope object reference requires an opaque UUID id');
	}
	return { ok: true, value: { kind, id: raw.id } };
}

/** Type guard: true integers only — NaN, ±Infinity, floats, numeric strings fail. */
function isIntegerValue(value: unknown): value is number {
	return typeof value === 'number' && Number.isInteger(value);
}

/** Validates geometry: integers only, positive sizes (ADR §8 rule 2, §11). */
export function validateGeometry(raw: unknown): Validated<WindowGeometry> {
	if (!isPlainObject(raw)) {
		return fail('bad_geometry', 'window geometry must be an object with integer x, y, width, height');
	}
	const { x, y, width, height } = raw;
	if (!isIntegerValue(x)) return fail('bad_geometry', 'geometry x must be an integer');
	if (!isIntegerValue(y)) return fail('bad_geometry', 'geometry y must be an integer');
	// §11 row: "geometry": {"x": NaN, …} / non-integers.
	if (!isIntegerValue(width)) return fail('bad_geometry', 'geometry width must be an integer');
	if (!isIntegerValue(height)) return fail('bad_geometry', 'geometry height must be an integer');
	if (width < 1 || height < 1) {
		return fail('bad_geometry', 'geometry width and height must be positive');
	}
	return { ok: true, value: { x, y, width, height } };
}

/** Validates the free-text `title_override` field (ADR §8 rule 1). */
export function validateTitleOverride(raw: unknown): Validated<string | null> {
	if (raw === null) return { ok: true, value: null };
	if (typeof raw !== 'string') {
		return fail('bad_title_override', 'title_override must be a string or null');
	}
	if (raw.length > MAX_TITLE_OVERRIDE_LENGTH) {
		return fail(
			'bad_title_override',
			'title_override is a short display label, never log bodies, prompts or conversation text'
		);
	}
	if (containsForbiddenContent(raw)) {
		return fail(
			'forbidden_content',
			'title_override must never carry credentials, tokens or domain payload content'
		);
	}
	return { ok: true, value: raw };
}

const PANEL_KIND_RE = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;

/**
 * Validates a full window descriptor and returns the normalized schema-v1
 * shape. Unknown fields are ignored and never propagated (ADR §8 rule 2);
 * unknown panel kinds are rejected against the caller-supplied registry set
 * (ADR §7.1/§11: unavailable panel, no import attempt).
 */
export function validateWindowDescriptor(
	raw: unknown,
	knownPanelKinds: ReadonlySet<string>
): Validated<WindowDescriptor> {
	if (!isPlainObject(raw)) {
		return fail('not_an_object', 'window descriptor must be an object');
	}
	if (!isUuid(raw.window_id)) {
		return fail('bad_window_id', 'window_id must be an opaque client-generated UUID');
	}
	if (typeof raw.panel_kind !== 'string' || !PANEL_KIND_RE.test(raw.panel_kind)) {
		return fail('bad_panel_kind', 'panel_kind must be a stable lowercase slug');
	}
	if (!knownPanelKinds.has(raw.panel_kind)) {
		return fail(
			'unknown_panel_kind',
			`panel kind "${raw.panel_kind}" is not registered; this window is unavailable`
		);
	}
	const scope = validateScope(raw.scope);
	if (!scope.ok) return scope;
	const geometry = validateGeometry(raw.geometry);
	if (!geometry.ok) return geometry;
	const presentation = PRESENTATIONS.find((candidate) => candidate === raw.presentation);
	if (presentation === undefined) {
		return fail('bad_presentation', `presentation must be one of: ${PRESENTATIONS.join(', ')}`);
	}
	if (!isIntegerValue(raw.z) || raw.z < 0) {
		return fail('bad_z', 'z must be a non-negative integer');
	}
	const title = validateTitleOverride(raw.title_override);
	if (!title.ok) return title;
	return {
		ok: true,
		value: {
			window_id: raw.window_id,
			panel_kind: raw.panel_kind,
			scope: scope.value,
			geometry: geometry.value,
			presentation,
			z: raw.z,
			title_override: title.value
		}
	};
}

// ---------------------------------------------------------------------------
// Hostile-layout restore (ADR §10.2 NT4, §11) and serialization
// ---------------------------------------------------------------------------

/** Whole-document discard codes for the restore path (ADR §10.2 NT4). */
export type LayoutDiscardCode =
	| 'unparsable_json'
	| 'bad_layout_shape'
	| 'unknown_schema_version'
	| 'bad_revision';

export interface DroppedWindow {
	/** Index of the rejected entry in the stored `windows` array. */
	index: number;
	code: RejectCode;
	message: string;
}

export type StoredLayoutParse =
	| { status: 'discarded'; code: LayoutDiscardCode; message: string }
	| {
			status: 'ok';
			revision: number;
			windows: WindowDescriptor[];
			dropped: DroppedWindow[];
	  };

/**
 * Parses and validates a stored layout document (e.g. from localStorage).
 * Unknown schema major, unparsable JSON and bad envelope shape discard the
 * whole stored layout (caller shows the default workspace + notice); per-entry
 * defects drop only the affected entries with a reason; duplicate
 * `window_id`s deduplicate deterministically, keeping the first (ADR §10.2
 * NT4, §11). No entry ever causes an import or an action replay.
 */
export function parseStoredLayout(
	text: string,
	knownPanelKinds: ReadonlySet<string>
): StoredLayoutParse {
	let doc: unknown;
	try {
		doc = JSON.parse(text);
	} catch {
		return {
			status: 'discarded',
			code: 'unparsable_json',
			message: 'stored layout is not valid JSON; the stored layout was discarded'
		};
	}
	if (!isPlainObject(doc) || !Array.isArray(doc.windows)) {
		return {
			status: 'discarded',
			code: 'bad_layout_shape',
			message: 'stored layout has an unexpected shape; the stored layout was discarded'
		};
	}
	// Strict major check: 2, "1", true, null all invalidate the whole document
	// (ADR §8 rule 2: unknown major version invalidates the stored layout).
	if (doc.schema_version !== SCHEMA_VERSION) {
		return {
			status: 'discarded',
			code: 'unknown_schema_version',
			message: 'stored layout schema version is unknown; the stored layout was discarded'
		};
	}
	const { revision } = doc;
	if (typeof revision !== 'number' || !Number.isInteger(revision) || revision < 0) {
		return {
			status: 'discarded',
			code: 'bad_revision',
			message: 'stored layout revision is invalid; the stored layout was discarded'
		};
	}
	const windows: WindowDescriptor[] = [];
	const dropped: DroppedWindow[] = [];
	const seen = new Set<string>();
	doc.windows.forEach((entry: unknown, index: number) => {
		const result = validateWindowDescriptor(entry, knownPanelKinds);
		if (!result.ok) {
			dropped.push({ index, code: result.issue.code, message: result.issue.message });
			return;
		}
		if (seen.has(result.value.window_id)) {
			dropped.push({
				index,
				code: 'duplicate_window_id',
				message: 'duplicate window_id; keeping the first occurrence'
			});
			return;
		}
		seen.add(result.value.window_id);
		windows.push(result.value);
	});
	return { status: 'ok', revision, windows, dropped };
}

/**
 * Thrown only when in-memory state fails serialization — a programming defect
 * (state must already be validated), never a hostile-input path. Hostile
 * persisted input goes through `parseStoredLayout`, which drops instead of
 * throwing (ADR §10.2 NT4).
 */
export class LayoutSerializationError extends Error {
	readonly issue: ValidationIssue;

	constructor(issue: ValidationIssue) {
		super(issue.message);
		this.name = 'LayoutSerializationError';
		this.issue = issue;
	}
}

/**
 * Serializes the persisted layout from whitelisted fields only, so extra
 * properties on in-memory state can never leak into storage (ADR §8). Reuses
 * the full descriptor validation, refuses duplicates, and scans the emitted
 * document for forbidden content classes as defense in depth (§11 severity-1
 * row: a violation is impossible by construction).
 */
export function serializeLayout(
	windows: readonly WindowDescriptor[],
	revision: number,
	knownPanelKinds: ReadonlySet<string>
): string {
	if (!Array.isArray(windows)) {
		throw new LayoutSerializationError({
			code: 'not_an_object',
			message: 'layout windows must be an array'
		});
	}
	if (typeof revision !== 'number' || !Number.isInteger(revision) || revision < 0) {
		throw new LayoutSerializationError({
			code: 'bad_revision',
			message: 'layout revision must be a non-negative integer'
		});
	}
	const seen = new Set<string>();
	const safeWindows: WindowDescriptor[] = windows.map((entry) => {
		const result = validateWindowDescriptor(entry, knownPanelKinds);
		if (!result.ok) throw new LayoutSerializationError(result.issue);
		if (seen.has(result.value.window_id)) {
			throw new LayoutSerializationError({
				code: 'duplicate_window_id',
				message: 'duplicate window_id in layout state'
			});
		}
		seen.add(result.value.window_id);
		return result.value;
	});
	const doc: StoredLayout = { schema_version: SCHEMA_VERSION, revision, windows: safeWindows };
	const json = JSON.stringify(doc);
	if (containsForbiddenContent(json)) {
		throw new LayoutSerializationError({
			code: 'forbidden_content',
			message: 'serialized layout would carry forbidden content; refusing to persist'
		});
	}
	return json;
}
