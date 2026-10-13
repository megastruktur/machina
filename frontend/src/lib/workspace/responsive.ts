/**
 * Viewport-responsive bounds enforcement — card `01-window-responsive`, P01.
 *
 * Keeps desktop windows recoverable across browser resize and zoom (brief
 * acceptance 1–2; windowed-workspace ADR §6 "Controls must remain recoverable
 * under viewport resize and browser zoom", §9.2 desktop-first, §10.2 NT3
 * bounds invariant, §10.1 PT6): one scoped controller watches the viewport
 * `resize` stream and re-clamps every live window's committed geometry so
 * each retains a visible, hit-testable `TITLE_BAR_MIN` (64×32 CSS px)
 * title-bar area inside the viewport. Desktop-first by contract: this is a
 * geometry clamp only — no phone UI, no single-panel mobile mode, no touch
 * requirement is introduced or implied (ADR §9.2).
 *
 * Nothing is duplicated from the sibling producers — the module composes
 * them:
 *
 * - `./drag.js` owns the NT3 position clamp (`clampDraggedGeometry`); the
 *   reflow reuses that exact rule instead of a second bound rule;
 * - `./registry.js` owns the `min_geometry` floors (`registry.minGeometry`)
 *   — the single minimum-size source, identical to the restore path;
 * - `./state.js` owns window state; reflow commits through the single
 *   `move` transition, so scope, identity, stacking z and focus are
 *   structurally untouchable from here (C08 rule 5, C11, NT5).
 *
 * The size half of the clamp (registry floor, viewport cap) mirrors the
 * restore clamp of `01-window-layout`, whose helper is module-private; the
 * floor wins on degenerate tiny viewports — the a11y minimum is never
 * violated (ADR §10.2 NT3, "resizes below `min_geometry` are clamped").
 *
 * Semantics:
 * - Every live window is re-clamped, in stacking order (deterministic),
 *   including minimized/maximized restore targets: a viewport change while
 *   hidden must not make the restore land offscreen (PT6/NT3).
 * - A window whose geometry already satisfies the bounds is left alone:
 *   no-op reflows never churn the layout revision (the sibling
 *   no-op-transition convention), so widening the viewport persists no
 *   spurious layout writes.
 * - Reflow never touches focus, titles, scopes or stacking; the frame DOM
 *   is keyed by `window_id`, so an in-flight keyboard focus survives the
 *   transition (brief acceptance 2).
 *
 * SSR isolation (ADR §5 rule 2, brief acceptance 3) is structural: no
 * module-level mutable state — the only runtime exports are the pure clamp
 * and the `createResponsiveController` factory. The listen target, viewport
 * and desktop origin are injected or resolved at factory/start time behind
 * runtime guards; with no `window` (server, bare Node) `start()` degrades to
 * a no-op. One controller per composition — never a singleton shared between
 * requests or tabs. Browser API access happens only inside these guards, and
 * the module has no domain channel at all (no fetch/WebSocket/EventSource,
 * no dynamic import): a reflow can never start, cancel, resubmit or retarget
 * backend work (NT6, C11).
 *
 * Known limits (documented, not defects):
 * - The default listener is the `window` `resize` event, which desktop
 *   browser zoom (Ctrl +/−) fires alongside real resizes because both change
 *   the CSS-px viewport; pinch zoom on touch devices is not wired (§9.2: no
 *   mandatory touch support). Compositions with a scroll-offset desktop
 *   region inject `origin()`/`viewport()` exactly like the drag adapter's
 *   embedders.
 * - No coordination with an in-flight drag/resize gesture is needed: the
 *   adapters read the live viewport on their own events; the next committed
 *   gesture geometry re-enters through the same `move` transition.
 */

import { clampDraggedGeometry, type DragViewport } from './drag.js';
import type { PanelRegistry } from './registry.js';
import type { WorkspaceState } from './state.js';
import { TITLE_BAR_MIN, type PanelKind, type WindowGeometry, type WindowId } from './types.js';

/**
 * Horizontal/vertical chrome inset per side between the descriptor frame box
 * and the title-bar hit target: the WindowFrame border (`--mai-border-width`,
 * frozen 1px by `01-ui-tokens`). The drag adapter measures the same inset
 * live at gesture start (bar rect − descriptor position, `DragOrigin`);
 * a state-level reflow has no DOM access, so the frozen token value is
 * applied here. The spec's DOM-measured NT3 check cross-checks this constant.
 */
const FRAME_BORDER_PX = 1;

/**
 * Viewport origin of the frames' positioned ancestor at reflow time, in CSS
 * pixels (the drag adapter measures the same reference at gesture start).
 * The composition owning the desktop region supplies its measured rect;
 * the default `{ left: 0, top: 0 }` is the restore clamp's reference frame
 * (ancestor at the viewport origin).
 */
export interface ResponsiveOrigin {
	left: number;
	top: number;
}

/** Structural listener target of the viewport stream (injected in tests). */
export interface ViewportEventTarget {
	addEventListener(type: 'resize', listener: () => void): void;
	removeEventListener(type: 'resize', listener: () => void): void;
}

/** One applied re-clamp — `from`/`to` are committed integer geometries. */
export interface ResponsiveAdjustment {
	window_id: WindowId;
	panel_kind: PanelKind;
	from: WindowGeometry;
	to: WindowGeometry;
}

/** Result of one reflow pass over the live workspace. */
export interface ResponsiveReport {
	/** Live windows examined (all presentations, stacking order). */
	inspected: number;
	/** Windows whose committed geometry changed; empty ⇒ nothing moved. */
	adjustments: ResponsiveAdjustment[];
}

/** Composition options — every policy input is explicit, nothing is global. */
export interface ResponsiveControllerOptions {
	/** The instance-scoped window state the reflow commits into. */
	state: WorkspaceState;
	/** The panel registry supplying the per-entry `min_geometry` floors. */
	registry: PanelRegistry;
	/**
	 * Viewport provider (CSS pixels), read at every reflow — never cached.
	 * Defaults to the browser window's live `innerWidth`/`innerHeight`
	 * rounded to integers; non-positive dimensions (no viewport — bare Node,
	 * detached caller) degrade to the minimum-size floor without position
	 * clamping, exactly like the restore clamp.
	 */
	viewport?(): DragViewport;
	/**
	 * Positioned-ancestor origin at reflow time. Defaults to the restore
	 * clamp's reference frame; a scroll-offset desktop region passes its
	 * measured rect (rounded to CSS-px integers) like the drag embedders.
	 */
	origin?(): ResponsiveOrigin;
	/**
	 * Viewport event target; defaults to the browser `window` when it exists
	 * at factory time (runtime-guarded — `null` under SSR/bare Node, which
	 * makes `start()` a no-op). `null` disables the listener entirely.
	 */
	target?: ViewportEventTarget | null;
}

/** Instance-scoped controller handle — one per composition, never a singleton. */
export interface ResponsiveController {
	/**
	 * Re-clamps every live window once against the current viewport and
	 * commits the changes through the workspace `move` transition. Safe to
	 * call any time; a fully in-bounds workspace produces an empty report
	 * and no state transition.
	 */
	reflow(): ResponsiveReport;
	/**
	 * Attaches the viewport listener (one per controller); the returned stop
	 * function detaches it. Idempotent: a second `start()` returns the
	 * pending stop instead of attaching twice. With no listen target
	 * (server render) this is a no-op and never throws.
	 */
	start(): () => void;
}

/** Browser viewport at call time — never at module scope; 0×0 when absent. */
function defaultViewport(): DragViewport {
	return {
		width: Math.round(globalThis.window?.innerWidth ?? 0),
		height: Math.round(globalThis.window?.innerHeight ?? 0)
	};
}

/** Browser listen target at factory time — never at module scope. */
function defaultTarget(): ViewportEventTarget | null {
	const candidate: unknown = globalThis.window;
	if (
		candidate !== null &&
		typeof candidate === 'object' &&
		typeof (candidate as ViewportEventTarget).addEventListener === 'function'
	) {
		return candidate as ViewportEventTarget;
	}
	return null;
}

/**
 * NT3 reflow clamp (ADR §10.2 NT3, §6 zoom/bounds, PT6): sizes are floored
 * at the registry's `min_geometry` and capped at the viewport — the floor
 * wins on degenerate tiny viewports, so the a11y minimum is never violated.
 * Positions reuse the exact NT3 drag clamp so the window keeps a
 * `TITLE_BAR_MIN` visible, hit-testable title-bar area inside the viewport,
 * measured from the positioned ancestor origin like a drag gesture.
 *
 * Browser zoom yields fractional CSS-px viewports; the state layer validates
 * integers only (ADR §8 rule 2), so the effective viewport rounds once here
 * and the drag clamp rounds the position. Result geometry is always integer.
 */
export function clampResponsiveGeometry(
	geometry: WindowGeometry,
	min: Readonly<WindowGeometry>,
	viewport: DragViewport,
	origin: ResponsiveOrigin
): WindowGeometry {
	const width = Math.round(viewport.width);
	const height = Math.round(viewport.height);
	if (width <= 0 || height <= 0) {
		// No viewport to clamp against (bare Node probe, headless caller):
		// keep the stored position, apply only the minimum-size floor.
		return {
			x: geometry.x,
			y: geometry.y,
			width: Math.max(min.width, geometry.width),
			height: Math.max(min.height, geometry.height)
		};
	}
	const clampedWidth = Math.max(min.width, Math.min(geometry.width, width));
	const clampedHeight = Math.max(min.height, Math.min(geometry.height, height));
	// The NT3 hit target is the title-bar ELEMENT, not the descriptor box: it
	// sits one frame border inside the frame on every side and is narrower by
	// the two horizontal borders — the same measurement the drag adapter
	// takes live at gesture start. Clamping on the raw descriptor width would
	// leave only 62..63 visible bar pixels at the viewport edge (observed in
	// the spec's DOM measure), short of the frozen 64×32. `+ 0` normalizes
	// the clamp's possible −0 to canonical 0 (the persisted integer shape).
	const positioned = clampDraggedGeometry(
		{ x: geometry.x, y: geometry.y, width: clampedWidth, height: clampedHeight },
		{ width, height },
		{
			left: origin.left + FRAME_BORDER_PX,
			top: origin.top + FRAME_BORDER_PX,
			barWidth: Math.max(TITLE_BAR_MIN.width, clampedWidth - 2 * FRAME_BORDER_PX)
		}
	);
	return {
		x: positioned.x + 0,
		y: positioned.y + 0,
		width: positioned.width,
		height: positioned.height
	};
}

/**
 * Creates the responsive controller for one composition (ADR §5 rule 2: one
 * per browser session; no module-level store, no SSR-shared singleton).
 */
export function createResponsiveController(
	options: ResponsiveControllerOptions
): ResponsiveController {
	const state = options.state;
	const registry = options.registry;
	const viewport = options.viewport ?? defaultViewport;
	const origin = options.origin ?? (() => ({ left: 0, top: 0 }));
	const target = options.target === undefined ? defaultTarget() : options.target;

	/** Pending stop of the single allowed listener — `null` when detached. */
	let stopListening: (() => void) | null = null;

	function reflow(): ResponsiveReport {
		const snapshot = state.snapshot();
		const frame = viewport();
		const ancestor = origin();
		const adjustments: ResponsiveAdjustment[] = [];
		// Stacking order (bottom → top): deterministic commit order, so the
		// same viewport change over the same state always produces the same
		// transition sequence.
		for (const window of snapshot.windows) {
			const clamped = clampResponsiveGeometry(
				window.geometry,
				registry.minGeometry(window.panel_kind),
				frame,
				ancestor
			);
			if (
				clamped.x === window.geometry.x &&
				clamped.y === window.geometry.y &&
				clamped.width === window.geometry.width &&
				clamped.height === window.geometry.height
			) {
				continue;
			}
			const outcome = state.move(window.window_id, clamped);
			if (!outcome.ok) {
				// Unreachable: the clamp emits integers from validated state and
				// the window id comes from the same snapshot — contained, never
				// thrown, exactly like the restore path's containment.
				continue;
			}
			adjustments.push({
				window_id: window.window_id,
				panel_kind: window.panel_kind,
				from: window.geometry,
				to: outcome.value.geometry
			});
		}
		return { inspected: snapshot.windows.length, adjustments };
	}

	function start(): () => void {
		if (stopListening) return stopListening;
		if (!target) return () => {};
		const listener = () => {
			reflow();
		};
		target.addEventListener('resize', listener);
		stopListening = () => {
			target.removeEventListener('resize', listener);
			stopListening = null;
		};
		return stopListening;
	}

	return { reflow, start };
}
