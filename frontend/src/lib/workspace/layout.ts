/**
 * Same-browser layout persistence and safe restore — card `01-window-layout`.
 *
 * Implements the selected same-browser layout restore of the windowed-
 * workspace ADR (§1 D7, §5 SSR isolation, §8 serialization rules, §10.1 PT6,
 * §10.2 NT3/NT4, §11 good/bad fixtures; contracts.md C11, C08 rule 5) on top
 * of the integrated producer modules — nothing here duplicates their logic:
 *
 * - `./types.js` owns the frozen schema-v1 document contract:
 *   `parseStoredLayout` (hostile-input validation/drop/dedup) and
 *   `serializeLayout` (whitelist-only emission + forbidden-content refusal);
 * - `./registry.js` owns panel-kind membership, per-scope opening checks and
 *   the `min_geometry` floor (`PanelRegistry.checkScope`/`minGeometry`);
 * - `./drag.js` owns the NT3 title-bar viewport clamp (`clampDraggedGeometry`)
 *   — restore reuses the exact drag clamp instead of a second bound rule;
 * - `./state.js` owns window state; restore only re-opens validated windows
 *   through `WorkspaceState.open` (fresh view identity, pinned scopes);
 * - `./presentation.js` (optional) owns the minimized/maximized dimension;
 *   when provided, persistence uses its effective snapshot and restore
 *   reapplies the stored presentations through it.
 *
 * Contract boundaries (acceptance 1–3):
 * - Only schema-versioned geometry and safe panel/object references are ever
 *   persisted — `serializeLayout` whitelists descriptor fields and refuses
 *   forbidden content classes; there is no field in this path that could
 *   carry credentials, domain payload or a pending action, and restore has no
 *   backend channel at all (no fetch/XHR/WebSocket/EventSource, no dynamic
 *   import, no domain callbacks) — reopening a window can never submit,
 *   cancel or retarget backend work (NT4/NT6, C11).
 * - Hostile stored documents are discarded or per-entry dropped with
 *   notice-safe reasons; whole-document discards also clear the stored
 *   document. Unauthorized scope objects (caller-supplied domain check) and
 *   registry-invalid scope/kind combinations drop only their own entry.
 * - Session handling: the controller binds an explicit caller-supplied
 *   session identity; a changed identity (login/logout) clears the stored
 *   layout, so stored geometry never carries across sessions. The session id
 *   itself is never persisted — only anonymous equality is checkable via the
 *   opaque marker.
 * - SSR isolation (ADR §5): no module-level state, no module-level browser
 *   API access. Storage and viewport are injected or resolved at factory
 *   time behind runtime guards; with no storage (server, blocked storage)
 *   every operation degrades to a no-op. One controller per composition —
 *   never a singleton shared between requests or tabs.
 *
 * Composition contract (thin routes / workspace runtime): create the
 * controller after hydration, call `restore()` once on the empty workspace,
 * then `start()` to persist every effective transition. `restore()` tolerates
 * `start()` being called first (its persistence is suppressed while a restore
 * is in flight; the restored layout is written back once, sanitized).
 *
 * Known limits (documented, not defects):
 * - Multiple tabs are last-writer-wins on the shared document (ADR §5 rule
 *   6); there is no cross-tab live sync and no cross-device sync (§9.2).
 * - `window_id` is view identity (§3.1): restored windows get fresh
 *   client-generated ids; pinned scopes — the reconnect identity (NT6) — are
 *   preserved exactly.
 * - The schema-v1 `presentation` field is a single value: a window stored as
 *   `minimized` restores minimized and returns to `normal` when the user
 *   restores it (the pre-minimize presentation is not representable).
 * - Without an injected `presentation` controller, stored
 *   `minimized`/`maximized` entries reopen `normal` (the dimension's owner
 *   is absent); geometry/scope restore is unaffected.
 */

import { clampDraggedGeometry, type DragViewport } from './drag.js';
import type { PanelRegistry, RegistryIssueCode } from './registry.js';
import type { PresentationController } from './presentation.js';
import type { WorkspaceState } from './state.js';
import {
	parseStoredLayout,
	serializeLayout,
	type LayoutDiscardCode,
	type RejectCode,
	type WindowDescriptor,
	type WindowScope
} from './types.js';

/** Storage key of the schema-v1 layout document (same browser origin). */
export const LAYOUT_STORAGE_KEY = 'mai.workspace.layout.v1';

/** Storage key of the opaque session marker (never carries the id itself). */
export const SESSION_STORAGE_KEY = 'mai.workspace.session.v1';

/**
 * Storage key of the selected-server preference: the bare canonical UUID of
 * the server the operator last targeted (never a profile object, never a
 * credential). It lives OUTSIDE the schema-v1 layout document so the frozen
 * window contract stays untouched; it is cleared by the SAME session
 * reconciliation and `clearStoredLayout` as the layout document.
 */
export const SELECTED_SERVER_STORAGE_KEY = 'mai.workspace.selected-server.v1';

/** Canonical UUID shape (lowercase hex, 8-4-4-4-12) of a stored server id. */
const SERVER_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * Minimal storage boundary (structurally `Storage`). Injected by tests and
 * embedders; defaults to the browser's `localStorage` when available.
 */
export interface LayoutStorage {
	getItem(key: string): string | null;
	setItem(key: string, value: string): void;
	removeItem(key: string): void;
}

/** Codes a restored entry can be dropped for, including registry rejections. */
export type RestoreDropCode = RejectCode | RegistryIssueCode | 'scope_not_authorized';

/** One dropped stored entry — `message` is safe for a visible restore notice. */
export interface RestoreDrop {
	/** Index of the entry in the stored document's `windows` array. */
	index: number;
	code: RestoreDropCode;
	message: string;
}

/** Result of one `restore()` run. */
export interface RestoredLayoutReport {
	/** Live descriptors of the reopened windows (post-presentation, fresh ids). */
	restored: WindowDescriptor[];
	/** Per-entry drops (parse rejects, registry rejections, unauthorized scopes). */
	dropped: RestoreDrop[];
	/** Whole-document discard reason; `null` when a stored document was usable or none existed. */
	discarded: LayoutDiscardCode | null;
	/**
	 * `true` when nothing was restored because nothing usable was stored
	 * (no document, whole-document discard, or session-mismatch clearing) —
	 * the default workspace plus a notice, never a defect.
	 */
	empty: boolean;
	/** `true` when restore did not run (already ran here, or the workspace is non-empty). */
	skipped: boolean;
}

/** Result of one persistence write. */
export type PersistOutcome =
	| { ok: true; /** `false` when persistence is disabled (no storage) or nothing was written. */ persisted: boolean }
	| { ok: false; reason: string };

/** Result of a session-identity change. */
export type SessionChangeOutcome = 'cleared' | 'unchanged';

/** Composition options — every policy input is explicit, nothing is global. */
export interface LayoutPersistenceOptions {
	/** The instance-scoped window state restore re-opens windows into. */
	state: WorkspaceState;
	/**
	 * The presentation controller owning minimized/maximized. When provided,
	 * persistence persists its effective snapshot and restore reapplies the
	 * stored presentations; when omitted the base state is persisted and
	 * stored hidden/maximized entries reopen `normal`.
	 */
	presentation?: PresentationController;
	/** The panel registry supplying membership, scope checks and min sizes. */
	registry: PanelRegistry;
	/**
	 * Storage boundary; defaults to the browser `localStorage` when it exists
	 * at factory time (runtime-guarded — safe under SSR/blocked storage).
	 * `null` disables persistence entirely (all writes/reads no-op).
	 */
	storage?: LayoutStorage | null;
	/**
	 * Viewport provider for the NT3 restore clamp (CSS px). Defaults to the
	 * browser window's live `innerWidth`/`innerHeight`; non-positive
	 * dimensions (no viewport — e.g. bare Node) skip position clamping.
	 */
	viewport?(): DragViewport;
	/**
	 * Session identity this workspace belongs to. When defined, the identity
	 * is bound at factory time and re-reconciled at restore: a different
	 * identity (including logout, `null`) clears the stored layout before it
	 * can restore; the same identity reloads it. The id is never persisted —
	 * the marker only proves "same identity as last writer". Live auth
	 * changes call `handleSessionChange` on the same controller.
	 */
	session?: string | null;
	/**
	 * Domain authorization of a parsed scope (deleted server, revoked object,
	 * …): `false` drops the entry at restore. Defaults to allowing every
	 * shape-valid scope — existence/authorization checks arrive with the
	 * domain/auth producers and are then supplied here by the composition.
	 */
	isScopeAuthorized?(scope: WindowScope): boolean;
}

/** Controller handle — one per composition (never a module singleton). */
export interface LayoutPersistence {
	/**
	 * Restores the stored layout once (PT6): validates the stored document,
	 * re-opens survivors with clamped geometry and pinned scopes, reapplies
	 * stored presentations and writes the sanitized layout back. A second
	 * call (or a call on a non-empty workspace) is a no-op (`skipped`).
	 */
	restore(): RestoredLayoutReport;
	/**
	 * Persists every effective layout transition until the returned stop
	 * function runs. The store's initial current-value emission is not
	 * persisted (calling `start` before `restore` must not clobber the stored
	 * document with an empty workspace); persistence during `restore()` is
	 * suppressed; write failures are contained (they must never break the
	 * desktop) and are reported by `persistNow()`.
	 */
	start(): () => void;
	/** Writes the current effective layout once (the restore sanitize write). */
	persistNow(): PersistOutcome;
	/** Reconciles the stored session marker with `sessionId`: a changed
	 * identity (including logout, `null`) clears the stored layout AND the
	 * stored selected-server preference together. Call once
	 * at composition with the bound session and on every auth change.
	 */
	handleSessionChange(sessionId: string | null): SessionChangeOutcome;
	/** Removes the stored layout document, selected-server preference and session marker immediately. */
	clearStoredLayout(): void;
	/**
	 * The stored selected server: the canonical UUID last accepted by
	 * `setSelectedServerId`, or `null` when absent/stale/invalid (an invalid
	 * stored value is removed on read — stale input cannot survive).
	 * Restoring is the CALLER's policy: existence against the loaded
	 * enrolled profiles is checked there, never here.
	 */
	getSelectedServerId(): string | null;
	/**
	 * Persists the selected server id (canonical UUID only — a profile
	 * object or non-UUID value is rejected, never stored). No-op-without-
	 * persistence (`persisted: false`) when storage is unavailable.
	 */
	setSelectedServerId(id: string): PersistOutcome;
}

/** Browser storage resolution at factory time — never at module scope. */
function defaultStorage(): LayoutStorage | null {
	try {
		return typeof globalThis.localStorage === 'undefined' ? null : globalThis.localStorage;
	} catch {
		// Storage access can throw (e.g. blocked cookies): persistence degrades
		// to a no-op instead of breaking the workspace.
		return null;
	}
}

/** Browser viewport at call time — never at module scope; 0×0 when absent. */
function defaultViewport(): DragViewport {
	return {
		width: Math.round(globalThis.window?.innerWidth ?? 0),
		height: Math.round(globalThis.window?.innerHeight ?? 0)
	};
}

function safeGet(storage: LayoutStorage, key: string): string | null {
	try {
		return storage.getItem(key);
	} catch {
		return null;
	}
}

function safeSet(storage: LayoutStorage, key: string, value: string): boolean {
	try {
		storage.setItem(key, value);
		return true;
	} catch {
		return false;
	}
}

function safeRemove(storage: LayoutStorage, key: string): void {
	try {
		storage.removeItem(key);
	} catch {
		// Removal failure cannot be repaired here; the next successful write
		// overwrites the document anyway.
	}
}

/**
 * PT6/NT3 restore clamp: sizes are floored at the registry's `min_geometry`
 * and capped at the viewport (the min floor wins on degenerate tiny
 * viewports — the a11y minimum is never violated); positions reuse the exact
 * NT3 drag clamp so the stored window keeps a `TITLE_BAR_MIN` visible,
 * hit-testable title-bar area inside the viewport. Offscreen stored
 * coordinates therefore clamp at restore (ADR §10.1 PT6, §10.2 NT3).
 */
function clampRestoredGeometry(
	geometry: WindowDescriptor['geometry'],
	min: Readonly<WindowDescriptor['geometry']>,
	viewport: DragViewport
): WindowDescriptor['geometry'] {
	if (viewport.width <= 0 || viewport.height <= 0) {
		// No viewport to clamp against (bare Node probe, headless caller):
		// keep the stored position, apply only the minimum-size floor.
		return {
			x: geometry.x,
			y: geometry.y,
			width: Math.max(min.width, geometry.width),
			height: Math.max(min.height, geometry.height)
		};
	}
	const width = Math.max(min.width, Math.min(geometry.width, viewport.width));
	const height = Math.max(min.height, Math.min(geometry.height, viewport.height));
	// The frame origin is the positioned ancestor (0/0 at restore time) and
	// the title bar spans the full stored frame width — the same viewport
	// reference the drag clamp documents. `+ 0` normalizes the clamp's
	// possible −0 to canonical 0 (the persisted integer shape).
	const clamped = clampDraggedGeometry(
		{ x: geometry.x, y: geometry.y, width, height },
		viewport,
		{ left: 0, top: 0, barWidth: width }
	);
	return { x: clamped.x + 0, y: clamped.y + 0, width: clamped.width, height: clamped.height };
}

/**
 * Creates the layout persistence for one composition (ADR §5 rule 2: one per
 * browser session; no module-level store, no SSR-shared singleton).
 */
export function createLayoutPersistence(options: LayoutPersistenceOptions): LayoutPersistence {
	const state = options.state;
	const presentation = options.presentation ?? null;
	const registry = options.registry;
	const storage = options.storage === undefined ? defaultStorage() : options.storage;
	const viewport = options.viewport ?? defaultViewport;
	const session = options.session;
	const isScopeAuthorized = options.isScopeAuthorized ?? (() => true);

	/** Snapshot source: the effective (presentation-merged) view when available. */
	const source = presentation ?? state;

	let restoreRan = false;
	/** True while `restore()` is reopening windows — persistence is suppressed. */
	let restoring = false;
	/** True once the subscription's current-value echo has been swallowed. */
	let primed = false;
	let stopPersistence: (() => void) | null = null;

	// Factory-time session binding: the marker is reconciled once here, so a
	// reload with the same bound identity keeps its stored layout while a
	// changed identity (including logout) has already cleared it before any
	// restore can read the document.
	if (session !== undefined) handleSessionChange(session);

	function writeNow(): PersistOutcome {
		if (!storage) return { ok: true, persisted: false };
		const snap = source.snapshot();
		try {
			const json = serializeLayout(snap.windows, snap.revision, registry.knownPanelKinds());
			// Direct write so the real storage failure (quota, privacy mode)
			// surfaces in the outcome instead of a generic message.
			storage.setItem(LAYOUT_STORAGE_KEY, json);
			return { ok: true, persisted: true };
		} catch (error) {
			// LayoutSerializationError: an in-memory state defect must never
			// take the desktop down; it is contained and reported.
			return {
				ok: false,
				reason: error instanceof Error ? error.message : 'layout serialization failed'
			};
		}
	}

	function handleSessionChange(sessionId: string | null): SessionChangeOutcome {
		if (!storage) return 'unchanged';
		const stored = safeGet(storage, SESSION_STORAGE_KEY);
		if (stored === sessionId || (stored === null && sessionId === null)) {
			return 'unchanged';
		}
		safeRemove(storage, LAYOUT_STORAGE_KEY);
		safeRemove(storage, SELECTED_SERVER_STORAGE_KEY);
		if (sessionId === null) {
			safeRemove(storage, SESSION_STORAGE_KEY);
		} else {
			safeSet(storage, SESSION_STORAGE_KEY, sessionId);
		}
		return 'cleared';
	}

	function restore(): RestoredLayoutReport {
		const skippedReport: RestoredLayoutReport = {
			restored: [],
			dropped: [],
			discarded: null,
			empty: true,
			skipped: true
		};
		if (restoreRan || source.snapshot().windows.length > 0) return skippedReport;
		restoreRan = true;

		const dropped: RestoreDrop[] = [];
		let discarded: LayoutDiscardCode | null = null;

		restoring = true;
		try {
			// Session reconciliation runs before any read: a changed identity
			// has already cleared the stored document here (logout/session
			// changes are handled before anything restores).
			if (session !== undefined) handleSessionChange(session);

			if (!storage) {
				return { restored: [], dropped, discarded: null, empty: true, skipped: false };
			}

			const text = safeGet(storage, LAYOUT_STORAGE_KEY);
			if (text === null) {
				return { restored: [], dropped, discarded: null, empty: true, skipped: false };
			}

			const parsed = parseStoredLayout(text, registry.knownPanelKinds());
			if (parsed.status === 'discarded') {
				// NT4: whole-document discard — default workspace + notice, and
				// the unusable document is removed (discard means discard).
				safeRemove(storage, LAYOUT_STORAGE_KEY);
				return {
					restored: [],
					dropped,
					discarded: parsed.code,
					empty: true,
					skipped: false
				};
			}
			dropped.push(
				...parsed.dropped.map((entry) => ({ index: entry.index, code: entry.code, message: entry.message }))
			);

			// Re-open survivors in stored stacking order (stable z sort) so the
			// reconstructed stack matches the persisted one; each open generates
			// a fresh view identity and validates the full schema again.
			const order = parsed.windows
				.map((descriptor, index) => ({ descriptor, index }))
				.sort((a, b) => a.descriptor.z - b.descriptor.z || a.index - b.index);

			const opened: Array<{ id: string; descriptor: WindowDescriptor }> = [];
			for (const { descriptor, index } of order) {
				// Registry scope check at `opening` (ADR §7.2): disabled kinds,
				// scope kinds the entry does not declare and undeclared object
				// pins drop their entry only.
				const scopeCheck = registry.checkScope(descriptor.panel_kind, descriptor.scope);
				if (!scopeCheck.ok) {
					dropped.push({
						index,
						code: scopeCheck.issue.code,
						message: scopeCheck.issue.message
					});
					continue;
				}
				// Domain authorization of the pinned target (deleted/revoked
				// scope objects drop the entry — NT4); shape-valid parse drops
				// never reach this callback.
				if (!isScopeAuthorized(descriptor.scope)) {
					dropped.push({
						index,
						code: 'scope_not_authorized',
						message: 'the pinned server or object is no longer available; this window was not restored'
					});
					continue;
				}
				const outcome = state.open({
					panel_kind: descriptor.panel_kind,
					scope: descriptor.scope,
					geometry: clampRestoredGeometry(
						descriptor.geometry,
						registry.minGeometry(descriptor.panel_kind),
						viewport()
					),
					title_override: descriptor.title_override
				});
				if (!outcome.ok) {
					// Unreachable for parse-validated input (same schema, same
					// registry set); contained as a drop, never thrown.
					dropped.push({ index, code: outcome.issue.code, message: outcome.issue.message });
					continue;
				}
				opened.push({ id: outcome.descriptor.window_id, descriptor });
			}

			// Reapply the presentation dimension through its owner: maximized
			// first (visible overlay toggle), then minimized (focus falls to
			// the topmost remaining visible window), both in stacking order —
			// so the end state is deterministic: focus on the topmost visible.
			if (presentation) {
				for (const { id, descriptor } of opened) {
					if (descriptor.presentation === 'maximized') presentation.toggleMaximize(id);
				}
				for (const { id, descriptor } of opened) {
					if (descriptor.presentation === 'minimized') presentation.minimize(id);
				}
			}

			// Deterministic notice order: drops sorted by stored position.
			dropped.sort((a, b) => a.index - b.index);

			// Sanitize write: the stored document becomes exactly the validated
			// live layout, so dropped/duplicate/offscreen entries cannot
			// re-notify on the next reload. The earlier returns (no storage, no
			// document, whole-document discard) deliberately write nothing.
			writeNow();

			return {
				restored: source.snapshot().windows.map((entry) => entry),
				dropped,
				discarded: null,
				empty: opened.length === 0,
				skipped: false
			};
		} finally {
			restoring = false;
		}
	}

	function start(): () => void {
		if (stopPersistence) return stopPersistence;
		primed = false;
		const unsubscribe = source.subscribe(() => {
			// The Svelte store's initial emission is the current-value echo —
			// persisting it would clobber the stored document with an empty
			// workspace when `start()` runs before `restore()`.
			if (!primed) {
				primed = true;
				return;
			}
			if (restoring) return;
			writeNow();
		});
		stopPersistence = () => {
			unsubscribe();
			stopPersistence = null;
		};
		return stopPersistence;
	}

	return {
		restore,
		start,
		persistNow: writeNow,
		handleSessionChange,
		clearStoredLayout(): void {
			if (!storage) return;
			safeRemove(storage, LAYOUT_STORAGE_KEY);
			safeRemove(storage, SELECTED_SERVER_STORAGE_KEY);
			safeRemove(storage, SESSION_STORAGE_KEY);
		},
		getSelectedServerId(): string | null {
			if (!storage) return null;
			const raw = safeGet(storage, SELECTED_SERVER_STORAGE_KEY);
			if (raw === null) return null;
			if (!SERVER_ID_PATTERN.test(raw)) {
				// Stale/hostile stored value: remove it, read as absent. Only the
				// exact canonical (lowercase) UUID shape survives; anything else
				// is repaired by the caller's next fallback write.
				safeRemove(storage, SELECTED_SERVER_STORAGE_KEY);
				return null;
			}
			return raw;
		},
		setSelectedServerId(id: string): PersistOutcome {
			if (!storage) return { ok: true, persisted: false };
			if (!SERVER_ID_PATTERN.test(id)) {
				return { ok: false, reason: 'selected server id must be a canonical UUID string' };
			}
			return safeSet(storage, SELECTED_SERVER_STORAGE_KEY, id)
				? { ok: true, persisted: true }
				: { ok: false, reason: 'selected server persistence failed' };
		}
	};
}
