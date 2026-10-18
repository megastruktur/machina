/**
 * Presentation transitions — minimize/restore/maximize and switcher focus
 * (card `01-window-presentation`, P01).
 *
 * Implements the presentation dimension of the windowed-workspace ADR
 * (§7.2 lifecycle `mounted ⇄ hidden (minimized) / maximized`, §10.1 PT4
 * minimize/restore via taskbar, §6 taskbar semantics) on top of the
 * instance-scoped `WorkspaceState` (card `01-window-state`), and contracts.md
 * C08 rule 5 / C11: minimizing, restoring, maximizing, focusing and closing
 * a window never start, cancel, resubmit or retarget backend work — this
 * module has no domain channel at all (no fetch/WebSocket/EventSource, no
 * services bag, no panel context). Job cancellation is a separate explicit
 * domain action that cannot be expressed through this API.
 *
 * View-only by construction (brief acceptance 2): a presentation transition
 * NEVER writes `descriptor.geometry`, scope, identity or the stacking z of
 * the transitioned window. "Maximized" is the ADR §7.2 *presentation
 * overlay*: the descriptor keeps the exact geometry the frame returns to,
 * so restore cannot lose geometry by construction — the renderer reads the
 * effective presentation and draws the overlay; nothing is saved or
 * re-committed through `move`.
 *
 * State ownership (brief acceptance 1/3): the base `WorkspaceState` stays
 * the single owner of identity, geometry, stacking and close; this module is
 * the single owner of the `presentation` dimension and composes the
 * effective view (`windows` with the live presentation merged in). It adds
 * no second copy of base state, no drag/geometry logic and no global
 * "current server": every method addresses one opaque `window_id` and every
 * scope stays pinned in the base descriptor.
 *
 * SSR isolation (ADR §5 rule 2) is structural, exactly like `state.ts`: no
 * module-level mutable state — the only runtime export is the
 * `createPresentationController` factory, and every writable map lives
 * inside one factory instance. Server-side module evaluation shares nothing
 * between requests, and nothing here touches `window`/`document`/
 * `localStorage` at any scope.
 *
 * Focus contract (PT2/PT4):
 *  - minimizing the focused window moves focus to the topmost remaining
 *    visible window (raised to front, PT2), so a visible window is always
 *    the focused one; when every live window is hidden the effective focus
 *    is `null`;
 *  - `restore`/`activate` restore AND focus (PT4: "activating the entry
 *    restores and focuses the window"); a window minimized while maximized
 *    comes back maximized — the pre-minimize presentation is remembered;
 *  - `toggleMaximize` and presentation changes of one window never move
 *    focus (view-only);
 *  - wiring rule for compositions: a rendered frame's activation intent
 *    maps to `focus` (raise visible only — a minimize control click bubbles
 *    an activation that must never resurrect the just-hidden window), and a
 *    taskbar entry's activation maps to `activate` (restore+focus).
 *
 * Transitions are idempotent-safe and report whether the shared state
 * changed, mirroring the base layer: no-op transitions never emit a new
 * snapshot and never churn the revision.
 */

import { writable } from 'svelte/store';
import type { Presentation, WindowDescriptor, WindowId } from './types.js';
import type { WorkspaceState, WorkspaceSnapshot } from './state.js';

/** A `presentation` value of a visible (non-hidden) window. */
export type VisiblePresentation = Exclude<Presentation, 'minimized'>;

/**
 * Read-only presentation view of one workspace instance. `windows` is in
 * the base stacking order (bottom → top) with the effective `presentation`
 * merged into each descriptor; `focused_id` is the focused window or `null`
 * when no live window is visible. `revision` increments once per effective
 * transition of this view (base-driven or presentation-driven) — the
 * persistence input for consumers that persist the effective layout.
 */
export interface PresentationSnapshot {
	revision: number;
	windows: readonly WindowDescriptor[];
	focused_id: WindowId | null;
}

/** Instance-scoped presentation controller over one `WorkspaceState`. */
export interface PresentationController {
	/**
	 * Minimizes a live visible window (PT4): marks it hidden, remembers its
	 * pre-minimize presentation for `restore`, and keeps geometry, scope,
	 * identity and stacking of the window untouched. When the minimized
	 * window was focused, focus falls to the topmost remaining visible
	 * window. Content unmounting is the renderer's `keep_mounted_when_hidden`
	 * policy (ADR §7.1); this transition only changes the shared state.
	 * Unknown ids and already-hidden windows return `false`.
	 */
	minimize(windowId: WindowId): boolean;
	/**
	 * Restores a hidden window to its pre-minimize presentation (PT4) and
	 * focuses it (raise to front). Strictly from-minimized: visible and
	 * unknown ids return `false`.
	 */
	restore(windowId: WindowId): boolean;
	/**
	 * Toggles the presentation overlay of a live visible window between
	 * `normal` and `maximized` (ADR §7.2). Never touches geometry, scope,
	 * identity or focus. Hidden windows are not toggled from the taskbar —
	 * restore first (`activate`). Unknown ids return `false`.
	 */
	toggleMaximize(windowId: WindowId): boolean;
	/**
	 * The window-frame activation intent (PT2): raises a live VISIBLE window
	 * to front and focuses it. This is the only intent a rendered frame's
	 * activation emits should drive — a click that completes a control
	 * interaction (e.g. minimize) bubbles an activation for a window that is
	 * now hidden, and raising must never resurrect it. Hidden and unknown
	 * ids return `false`; activating the already-focused top window is a
	 * no-op. For the taskbar entry intent see `activate`.
	 */
	focus(windowId: WindowId): boolean;
	/**
	 * The taskbar/switcher activation intent (PT4/PT2): restores and focuses
	 * a hidden window, raises and focuses a visible one. Taskbar entries are
	 * the only surface that can address a hidden window. Unknown ids return
	 * `false`; activating the already-focused top window is a no-op.
	 */
	activate(windowId: WindowId): boolean;
	/** Deep-copied effective snapshot; mutating it never touches live state. */
	snapshot(): PresentationSnapshot;
	/** Svelte store contract over `snapshot()`; fires after every transition. */
	subscribe(run: (snapshot: PresentationSnapshot) => void): () => void;
}

/**
 * Creates the presentation controller for one workspace instance (ADR §5
 * rule 2: one per browser session, created together with its base state).
 * The controller subscribes to the base state, so windows opened, focused,
 * moved or closed directly through the shared base handle stay reflected in
 * this view, and closed windows are pruned from the presentation maps.
 */
export function createPresentationController(windows: WorkspaceState): PresentationController {
	/** Effective presentation per live window (single owner of the dimension). */
	const presentationBy = new Map<WindowId, Presentation>();
	/** Remembered visible presentation of hidden windows (restore target). */
	const preMinimize = new Map<WindowId, VisiblePresentation>();

	let revision = 0;
	/** Latest base snapshot, kept current by the subscription below. */
	let base: WorkspaceSnapshot = windows.snapshot();

	const store = writable<PresentationSnapshot>(compose());

	function effectivePresentation(window: WindowDescriptor): Presentation {
		return presentationBy.get(window.window_id) ?? 'normal';
	}

	function compose(): PresentationSnapshot {
		const merged = base.windows.map((window) => {
			const presentation = effectivePresentation(window);
			return presentation === window.presentation
				? window
				: { ...window, presentation };
		});
		const focused = base.focused_id;
		const focusedVisible =
			focused !== null &&
			merged.some(
				(window) => window.window_id === focused && window.presentation !== 'minimized'
			);
		return {
			revision,
			windows: structuredClone(merged),
			focused_id: focusedVisible ? focused : null
		};
	}

	function emit(): void {
		revision += 1;
		store.set(compose());
	}

	/** Topmost live window that is not hidden, or `null`. */
	function topmostVisible(): WindowId | null {
		for (let index = base.windows.length - 1; index >= 0; index -= 1) {
			if (effectivePresentation(base.windows[index]) !== 'minimized') {
				return base.windows[index].window_id;
			}
		}
		return null;
	}

	function findLive(windowId: WindowId): WindowDescriptor | undefined {
		return base.windows.find((window) => window.window_id === windowId);
	}

	/**
	 * Base transitions emit synchronously through the subscription below; a
	 * controller transition coalesces that with its own map changes into one
	 * emission so one user action bumps the revision exactly once.
	 */
	let inTransition = false;
	let dirty = false;

	function markBaseChange(next: WorkspaceSnapshot): void {
		const removed = new Set(presentationBy.keys());
		for (const window of next.windows) removed.delete(window.window_id);
		for (const windowId of removed) {
			presentationBy.delete(windowId);
			preMinimize.delete(windowId);
		}
		base = next;
		dirty = true;
	}

	windows.subscribe((next) => {
		if (inTransition) {
			markBaseChange(next);
			return;
		}
		const changed = next.revision !== base.revision;
		markBaseChange(next);
		if (changed) emit();
	});

	function runTransition(transition: () => boolean): boolean {
		inTransition = true;
		dirty = false;
		let changed = false;
		try {
			changed = transition();
		} finally {
			inTransition = false;
		}
		if (dirty || changed) emit();
		return changed;
	}

	function minimize(windowId: WindowId): boolean {
		return runTransition(() => {
			const window = findLive(windowId);
			const current = window ? effectivePresentation(window) : undefined;
			if (!window || current === undefined || current === 'minimized') return false;
			preMinimize.set(windowId, current);
			presentationBy.set(windowId, 'minimized');
			if (base.focused_id === windowId) {
				// Focus falls to the topmost remaining visible window (raised to
				// front, PT2); the subscription records it via `dirty`.
				const next = topmostVisible();
				if (next !== null) windows.focus(next);
			}
			return true;
		});
	}

	function restoreFromHidden(windowId: WindowId): boolean {
		const window = findLive(windowId);
		if (!window || effectivePresentation(window) !== 'minimized') return false;
		const previous = preMinimize.get(windowId) ?? 'normal';
		preMinimize.delete(windowId);
		presentationBy.set(windowId, previous);
		// PT4: restoring focuses — raise the restored window to front.
		windows.focus(windowId);
		return true;
	}

	function restore(windowId: WindowId): boolean {
		return runTransition(() => restoreFromHidden(windowId));
	}

	function toggleMaximize(windowId: WindowId): boolean {
		return runTransition(() => {
			const window = findLive(windowId);
			if (!window) return false;
			const current = effectivePresentation(window);
			if (current === 'minimized') return false;
			presentationBy.set(windowId, current === 'normal' ? 'maximized' : 'normal');
			return true;
		});
	}

	/** Raises a live visible window to front (shared by focus/activate). */
	function raiseVisible(windowId: WindowId): boolean {
		// The base focus() reports `true` for a valid but already-focused
		// window (a no-op that commits nothing); a raise is a transition only
		// when it actually commits (the subscription records it via `dirty`).
		const alreadyTopFocused =
			base.focused_id === windowId &&
			base.windows[base.windows.length - 1]?.window_id === windowId;
		windows.focus(windowId);
		return !alreadyTopFocused;
	}

	function focus(windowId: WindowId): boolean {
		return runTransition(() => {
			const window = findLive(windowId);
			if (!window || effectivePresentation(window) === 'minimized') return false;
			return raiseVisible(windowId);
		});
	}

	function activate(windowId: WindowId): boolean {
		return runTransition(() => {
			const window = findLive(windowId);
			if (!window) return false;
			if (effectivePresentation(window) === 'minimized') return restoreFromHidden(windowId);
			return raiseVisible(windowId);
		});
	}

	return {
		minimize,
		restore,
		toggleMaximize,
		focus,
		activate,
		snapshot: compose,
		subscribe: store.subscribe
	};
}
