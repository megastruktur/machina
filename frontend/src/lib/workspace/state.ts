/**
 * Instance-scoped workspace window state — lifecycle, focus and stacking.
 *
 * Producer card `01-window-state`. Implements the workspace state/controller
 * layer of the windowed-workspace ADR (§2 layer table, §5 SSR isolation, §7.2
 * lifecycle, §10.1 PT2 focus/stacking + PT5 close, §10.2 NT5 pinned scope,
 * NT6 backend independence) on top of the frozen descriptor contract in
 * `./types.js`, and contracts.md C08 rule 5 / C11 (explicit pinned target;
 * closing/moving/focusing never starts, cancels, resubmits or retargets
 * backend work; no global "current server" the UI could mutate through).
 *
 * SSR isolation (ADR §5 rule 2) is structural: this module has no
 * module-level mutable state — the only runtime export is the
 * `createWorkspaceState` factory, and every writable store it creates lives
 * inside one factory instance (one per browser session; server-side module
 * evaluation shares nothing between requests). There is no drag/pointer
 * logic here (owned by `01-window-drag`), no persistence (owned by
 * `01-window-layout` via `serializeLayout`), and no presentation transitions
 * (owned by `01-window-presentation`) — only the open/focus/move/close
 * transitions of this card, over validated schema-v1 descriptors.
 *
 * Stacking is bounded by invariant: the internal stack is kept in stacking
 * order (bottom → top) and `z` is renormalized to `index + 1` after every
 * mutation, so z-values never grow beyond the live window count and the
 * same transition sequence always produces the same stack.
 *
 * Targets are pinned by construction: a window's scope is validated once at
 * open and no method of this state (or the module) can rewrite it afterwards;
 * openers always pass the scope explicitly, so a launcher/global selection
 * change can only affect windows opened later.
 */

import { writable } from 'svelte/store';
import {
	createWindowId,
	validateGeometry,
	validateWindowDescriptor,
	type PanelKind,
	type Validated,
	type ValidationIssue,
	type WindowDescriptor,
	type WindowGeometry,
	type WindowId,
	type WindowScope
} from './types.js';

/** Constructor options — the explicit policy inputs of one workspace instance. */
export interface WorkspaceStateOptions {
	/**
	 * Registered panel kinds (windowed ADR §7.1). Until `01-panel-registry`
	 * lands, callers pass the frozen registry set explicitly — the state layer
	 * never resolves panel membership from anywhere else.
	 */
	knownPanelKinds: ReadonlySet<string>;
	/**
	 * Frozen instance policy (windowed ADR §7.1 `singleton`): given the open
	 * request's panel kind and validated scope, returns the identity of the
	 * single live instance this request belongs to, or `null` when the panel
	 * kind allows free multi-instance opening. The registry owner decides the
	 * granularity (per object, per scope, or per kind); this layer only
	 * enforces "equal non-null key ⇒ at most one live instance", focusing the
	 * existing window instead of creating a second one and never rewriting
	 * the existing instance's descriptor.
	 */
	instanceKey(panelKind: PanelKind, scope: WindowScope): string | null;
}

/** Explicit open request — the only way a window enters the workspace. */
export interface OpenWindowRequest {
	panel_kind: PanelKind;
	scope: WindowScope;
	geometry: WindowGeometry;
	/** Optional short display label; forbidden content classes are rejected (ADR §8 rule 1). */
	title_override?: string | null;
}

export type OpenOutcome =
	| {
			ok: true;
			/** Stable descriptor of the (new or reused) live instance. */
			descriptor: WindowDescriptor;
			/** `false` when the instance policy reused an existing live window. */
			created: boolean;
			/**
			 * Registers one close-time release hook (subscription teardown for
			 * the panel mounted in this window; windowed ADR §7.2 "closed:
			 * all subscriptions/resources released explicitly"). Hooks run
			 * exactly once, last-registered-first, on `close`. Returns an
			 * unregister function; registering on an already-closed window is
			 * a no-op.
			 */
			release(hook: () => void): () => void;
	  }
	| { ok: false; issue: ValidationIssue };

/**
 * Read-only view of one workspace instance. `windows` is in stacking order
 * (bottom → top) with `z` = index + 1; `focused_id` is the focused window or
 * `null` for an empty workspace; `revision` increments once per effective
 * state transition (persistence input for the last-writer-wins layout
 * document, windowed ADR §5 rule 6).
 */
export interface WorkspaceSnapshot {
	revision: number;
	windows: readonly WindowDescriptor[];
	focused_id: WindowId | null;
}

/** Instance-scoped controller handle — one per browser workspace. */
export interface WorkspaceState {
	open(request: OpenWindowRequest): OpenOutcome;
	/**
	 * Brings the window to the top of the stack (next `z`), deterministically
	 * preserving the relative order of all other windows (PT2). Focusing the
	 * already-focused window changes nothing. Unknown ids return `false`.
	 */
	focus(windowId: WindowId): boolean;
	/**
	 * Commits new geometry for an existing window — integers only, positive
	 * sizes (validated like the persisted schema; viewport/min-size clamping
	 * is applied upstream by the drag/resize/restore paths). Scope, identity,
	 * stacking order and focus are untouched. Unknown ids return
	 * `bad_window_id`.
	 */
	move(windowId: WindowId, geometry: WindowGeometry): Validated<WindowDescriptor>;
	/**
	 * Closes a window: runs its release hooks exactly once (LIFO; a throwing
	 * teardown is contained and can neither skip the remaining hooks nor keep
	 * the window alive), removes it from the stack and the persisted-layout
	 * domain. Never performs or triggers any domain/backend action — job
	 * cancellation is a separate explicit domain operation (ADR §7.2, NT6).
	 * Focusing falls to the new top window when the closed window was
	 * focused. Unknown ids return `false`.
	 */
	close(windowId: WindowId): boolean;
	/** Deep-copied snapshot; mutating it never touches the live state. */
	snapshot(): WorkspaceSnapshot;
	/** Svelte store contract over `snapshot()`; fires after every transition. */
	subscribe(run: (snapshot: WorkspaceSnapshot) => void): () => void;
}

/**
 * Creates one instance-scoped workspace state (windowed ADR §5 rule 2: one
 * per browser session; no module-level store, no SSR-shared singleton).
 *
 * Snapshots and open/move results are always `structuredClone`d so callers
 * can never alias (and mutate) live state through a returned descriptor.
 */
export function createWorkspaceState(options: WorkspaceStateOptions): WorkspaceState {
	const knownPanelKinds = options.knownPanelKinds;
	const instanceKeyOf = options.instanceKey;

	/** Live windows in stacking order, bottom → top; `z` is always index + 1. */
	let stack: WindowDescriptor[] = [];
	let revision = 0;
	let focusedId: WindowId | null = null;

	/** Close-time release hooks per live window (LIFO on close). */
	const releaseHooks = new Map<WindowId, Array<() => void>>();
	/** Live single-instance keys → their window (instance policy enforcement). */
	const instanceIndex = new Map<string, WindowId>();

	const store = writable<WorkspaceSnapshot>(currentSnapshot());

	function currentSnapshot(): WorkspaceSnapshot {
		return {
			revision,
			windows: stack.map((window) => structuredClone(window)),
			focused_id: focusedId
		};
	}

	/** Restores the bounded-stacking invariant: z = index + 1, bottom → top. */
	function renumber(): void {
		stack = stack.map((window, index) =>
			window.z === index + 1 ? window : { ...window, z: index + 1 }
		);
	}

	function commit(): void {
		revision += 1;
		store.set(currentSnapshot());
	}

	function makeRelease(windowId: WindowId): (hook: () => void) => () => void {
		return (hook: () => void) => {
			let hooks = releaseHooks.get(windowId);
			if (!hooks) {
				// Nothing registers on a closed window: the map entry is removed
				// at close, so a missing entry can only mean "not live".
				if (!stack.some((window) => window.window_id === windowId)) return () => {};
				hooks = [];
				releaseHooks.set(windowId, hooks);
			}
			hooks.push(hook);
			let active = true;
			return () => {
				if (!active) return;
				active = false;
				const index = hooks.indexOf(hook);
				if (index !== -1) hooks.splice(index, 1);
			};
		};
	}

	/**
	 * Shared focus path (public focus + instance-policy reuse); returns true
	 * only when the stack actually changed, so no-op transitions never churn
	 * the layout revision.
	 */
	function raiseToTop(windowId: WindowId): boolean {
		const index = stack.findIndex((window) => window.window_id === windowId);
		if (index === -1) return false;
		if (focusedId === windowId && index === stack.length - 1) return false;
		const [window] = stack.splice(index, 1);
		stack.push(window);
		renumber();
		focusedId = windowId;
		return true;
	}

	function open(request: OpenWindowRequest): OpenOutcome {
		// Total validation against the frozen schema: unknown panel kinds,
		// scope without explicit server_id, NaN/non-integer geometry and
		// forbidden title content are rejected before any state exists.
		const candidate = validateWindowDescriptor(
			{
				window_id: createWindowId(),
				panel_kind: request?.panel_kind,
				scope: request?.scope,
				geometry: request?.geometry,
				presentation: 'normal',
				z: 1,
				title_override: request?.title_override ?? null
			},
			knownPanelKinds
		);
		if (!candidate.ok) return candidate;

		const key = instanceKeyOf(candidate.value.panel_kind, candidate.value.scope);
		if (key !== null) {
			const existingId = instanceIndex.get(key);
			const existing =
				existingId !== undefined
					? stack.find((window) => window.window_id === existingId)
					: undefined;
			if (existing) {
				// Frozen instance policy: reuse the live instance — same stable
				// window_id, same pinned scope/geometry; the open only raises it.
				if (raiseToTop(existing.window_id)) commit();
				return {
					ok: true,
					descriptor: structuredClone(existing),
					created: false,
					release: makeRelease(existing.window_id)
				};
			}
			instanceIndex.set(key, candidate.value.window_id);
		}

		stack.push(candidate.value);
		renumber();
		focusedId = candidate.value.window_id;
		commit();
		return {
			ok: true,
			descriptor: structuredClone(candidate.value),
			created: true,
			release: makeRelease(candidate.value.window_id)
		};
	}

	function focus(windowId: WindowId): boolean {
		if (!raiseToTop(windowId)) {
			// Either unknown or already top-focused — unknown ids report false.
			return stack.some((window) => window.window_id === windowId);
		}
		commit();
		return true;
	}

	function move(windowId: WindowId, geometry: WindowGeometry): Validated<WindowDescriptor> {
		const window = stack.find((entry) => entry.window_id === windowId);
		if (!window) {
			return {
				ok: false,
				issue: { code: 'bad_window_id', message: 'cannot move an unknown window' }
			};
		}
		const geometryResult = validateGeometry(geometry);
		if (!geometryResult.ok) return geometryResult;
		const moved: WindowDescriptor = { ...window, geometry: geometryResult.value };
		stack[stack.indexOf(window)] = moved;
		commit();
		return { ok: true, value: structuredClone(moved) };
	}

	function close(windowId: WindowId): boolean {
		const index = stack.findIndex((window) => window.window_id === windowId);
		if (index === -1) return false;

		// Release this window's subscriptions only (ADR §7.2). The state layer
		// has no domain channel at all — nothing here can start, cancel,
		// resubmit or retarget backend work (NT6).
		const hooks = releaseHooks.get(windowId) ?? [];
		releaseHooks.delete(windowId);
		while (hooks.length > 0) {
			const hook = hooks.pop();
			try {
				hook?.();
			} catch {
				// Contained: a broken panel teardown can neither skip the
				// remaining hooks nor keep the window alive (ADR §7.2 error
				// containment); hooks run exactly once either way.
			}
		}

		stack.splice(index, 1);
		renumber();
		for (const [key, id] of instanceIndex) {
			if (id === windowId) instanceIndex.delete(key);
		}
		if (focusedId === windowId) {
			const top = stack[stack.length - 1];
			focusedId = top ? top.window_id : null;
		}
		commit();
		return true;
	}

	return {
		open,
		focus,
		move,
		close,
		snapshot: currentSnapshot,
		subscribe: store.subscribe
	};
}
