/**
 * Pointer resize adapter for window frames — card `01-window-resize`, P01.
 *
 * Svelte action bound to `WindowFrame` edge/corner resize handles (windowed-
 * workspace ADR §10.1 PT3): `pointerdown` on a handle starts a gesture under
 * pointer capture; `pointermove` emits the resized geometry, clamped to the
 * per-entry minimum (`min_geometry`, global default `DEFAULT_MIN_GEOMETRY`
 * 320×200 CSS px when unset) and to the NT3 viewport bounds, through the
 * owning controller — the frame is a controlled component and the shared
 * state is mutated only by its `move` transition, the exact single geometry
 * path the drag adapter uses. Commit+persist on `pointerup` (PT3): the
 * per-move emissions ARE the live geometry of the shared state; persistence
 * of the layout document is a separate producer (`01-window-layout`) and is
 * deliberately not touched here. The adapter shares no code path with the
 * drag adapter: it binds only the handle elements, which are structurally
 * outside the title bar, so a resize can never initiate a drag and vice
 * versa (brief acceptance 2).
 *
 * Minimum dimensions (PT3): every edge/corner respects `min` (the frame
 * passes the registry entry's `minGeometry` through; the frozen
 * `DEFAULT_MIN_GEOMETRY` applies when an entry omits it). The clamp keeps
 * the fixed edges pinned: an east/south resize stops at the minimum size, a
 * west/north resize stops where the pinned opposite edge minus the minimum
 * puts the moving edge — the bottom-right corner never moves under a
 * west/north gesture and the top-left corner never moves under an
 * east/south gesture.
 *
 * Frozen bounds (NT3): after any resize the window retains a visible,
 * hit-testable title-bar area of at least `TITLE_BAR_MIN` (64×32 CSS px)
 * inside the viewport. Growing east/south can never hide the title bar
 * (left/top are pinned, so the visible strip cannot shrink below the
 * gesture-start invariant). The moving west/north edges are clamped so the
 * bar keeps the frozen visible strip; the caps use the title-bar viewport
 * origin measured at gesture start (same reference the drag adapter
 * measures), floored so the integer result never overshoots the cap.
 * Integer geometry throughout (ADR §8 rule 2 — the state layer validates
 * integers).
 *
 * Interrupted gestures (NT1): `pointercancel`, lost pointer capture or the
 * unmount of the frame release the capture, emit no further geometry and
 * revert to the gesture-start committed geometry — for a resize that
 * committed geometry already satisfies every clamp (it was clamped when it
 * was committed), i.e. the interrupted resize ends clamped, never at an
 * in-flight pointer position. Cleanup is structural, exactly like the drag
 * adapter: every listener lives on the bound handle node, capture is
 * released on the same node, and the only style the action sets
 * (`touch-action`, so touch resizes are not hijacked by scrolling) is
 * restored on destroy — no document/window listeners, no global state, no
 * styles that can outlive the element (§10.2 NT1 "no stuck capture, no
 * stuck pointer styles").
 *
 * Isolation (ADR §5, C08 rule 5, C11): no module-level mutable state, no
 * `window`/`document` access at module top level, no stores. Gesture state
 * lives in the action closure — one per mounted handle — so nothing is
 * shared between windows, tabs or SSR requests, and there is no hidden
 * global mutation target. Handles are display affordances
 * (`aria-hidden`); the keyboard resize mode is the frozen accessibility
 * equivalent (ADR §6) and stays with the phase-gate a11y work, like
 * keyboard Move.
 */

import type { ActionReturn } from 'svelte/action';
import type { DragViewport } from './drag.js';
import { TITLE_BAR_MIN, type WindowGeometry, type WindowId } from './types.js';

/** The eight resize directions — edges first, then corners. */
export const RESIZE_DIRECTIONS = Object.freeze([
	'n',
	's',
	'e',
	'w',
	'ne',
	'nw',
	'se',
	'sw'
] as const);

export type ResizeDirection = (typeof RESIZE_DIRECTIONS)[number];

/**
 * Title-bar viewport position at gesture start, relative to descriptor
 * coordinates: `left`/`top` are the title bar's viewport position when the
 * descriptor x/y are 0 (the positioned ancestor's origin plus the frame
 * border), measured once per gesture from the live bounding rect — the
 * same viewport reference the NT3 drag clamp uses.
 */
export interface ResizeOrigin {
	left: number;
	top: number;
}

/** Per-entry minimum size clamp (registry `min_geometry` vocabulary). */
export interface MinGeometry {
	width: number;
	height: number;
}

/**
 * Action parameters, bound per handle. `geometry()` reads the controller's
 * committed descriptor at gesture start (the revert base); `titleBar()`
 * measures the frame's title bar (`getBoundingClientRect()`-shaped);
 * `onGeometry` receives clamped integer geometry for live resizes and
 * interrupt reverts — the controller applies it through the workspace
 * `move` transition.
 */
export interface WindowResizeParams {
	windowId: WindowId;
	direction: ResizeDirection;
	geometry(): WindowGeometry;
	min: MinGeometry;
	titleBar(): { left: number; top: number } | null;
	onGeometry?(windowId: WindowId, geometry: WindowGeometry): void;
	/**
	 * Viewport provider (CSS pixels). Defaults to the browser window;
	 * injectable so embedders with scroll-offset frames stay honest.
	 */
	viewport?(): DragViewport;
}

/**
 * PT3/NT3 clamp: applies the pointer delta for one direction and returns
 * the clamped integer geometry. Fixed edges stay pinned (`e`/`w` keep the
 * left/right edge respectively, `n`/`s` the top/bottom); sizes never go
 * below `min`; west/north edge moves never push the title bar below the
 * frozen `TITLE_BAR_MIN` visible strip inside the viewport. Result fields
 * are integers and mutually exact: `x + width` / `y + height` reproduce
 * the pinned edge without rounding drift (west/north derive the size from
 * the pinned edge minus the clamped moving edge).
 */
export function clampResizedGeometry(
	base: WindowGeometry,
	direction: ResizeDirection,
	dx: number,
	dy: number,
	min: MinGeometry,
	viewport: DragViewport,
	origin: ResizeOrigin
): WindowGeometry {
	const right = base.x + base.width;
	const bottom = base.y + base.height;
	let { x, y } = base;
	let width = base.width;
	let height = base.height;

	// Edge components of the direction (`ne` = north + east, …).
	if (direction.includes('e')) {
		width = Math.max(min.width, Math.round(base.width + dx));
	}
	if (direction.includes('w')) {
		x = Math.round(base.x + dx);
		// Minimum width: the pinned right edge minus the minimum size.
		const maxLeftForMin = right - min.width;
		if (x > maxLeftForMin) x = maxLeftForMin;
		// NT3: pushing the left edge right must keep a 64px visible strip of
		// the title bar inside the viewport. Floor the fractional cap (the
		// measured origin is fractional) so the integer result stays ≤ cap.
		const maxLeftForBounds = Math.floor(
			viewport.width - TITLE_BAR_MIN.width - origin.left
		);
		if (x > maxLeftForBounds) x = maxLeftForBounds;
		width = right - x;
	}
	if (direction.includes('s')) {
		height = Math.max(min.height, Math.round(base.height + dy));
	}
	if (direction.includes('n')) {
		y = Math.round(base.y + dy);
		// Minimum height: the pinned bottom edge minus the minimum size.
		const maxTopForMin = bottom - min.height;
		if (y > maxTopForMin) y = maxTopForMin;
		// NT3: same rule for the moving top edge (32px visible strip).
		const maxTopForBounds = Math.floor(
			viewport.height - TITLE_BAR_MIN.height - origin.top
		);
		if (y > maxTopForBounds) y = maxTopForBounds;
		height = bottom - y;
	}
	return { x, y, width, height };
}

/** Live gesture record — exists only between pointerdown and release. */
interface ResizeGesture {
	pointerId: number;
	/** Pointer position at gesture start (client coordinates). */
	startX: number;
	startY: number;
	/** Title-bar viewport origin relative to descriptor coordinates. */
	origin: ResizeOrigin;
	/** Committed geometry at gesture start — the NT1 revert base. */
	base: WindowGeometry;
	/** Last emitted geometry, so pinned moves don't churn the revision. */
	emitted: WindowGeometry;
}

/**
 * The resize action. Bind on one edge/corner handle element:
 * `use:windowResize={{ windowId, direction, geometry, min, titleBar, onGeometry }}`.
 */
export function windowResize(
	node: HTMLElement,
	params: WindowResizeParams
): ActionReturn<WindowResizeParams> {
	let current = params;
	let gesture: ResizeGesture | null = null;

	function begin(event: PointerEvent): void {
		if (gesture) return; // one live gesture per handle
		if (!event.isPrimary || event.button !== 0) return;
		const base = current.geometry();
		if (!base) return;
		const bar = current.titleBar();
		if (!bar) return;
		const active: ResizeGesture = {
			pointerId: event.pointerId,
			startX: event.clientX,
			startY: event.clientY,
			origin: { left: bar.left - base.x, top: bar.top - base.y },
			base: { ...base },
			emitted: { ...base }
		};
		try {
			// Capture on the bound handle: pointer events keep targeting the
			// handle (and bubble to the frame) even when the pointer crosses
			// sibling frames or leaves the window — PT3 keeps tracking
			// without any document-level listeners.
			node.setPointerCapture(event.pointerId);
		} catch {
			return; // stale/inactive pointer id — nothing to resize with
		}
		gesture = active;
	}

	/**
	 * Emits one geometry through the controller unless it equals the last
	 * emitted geometry. Live resizes and interrupt reverts share this single
	 * path, so both dedupe against the same record — a pinned pointer or a
	 * redundant revert never churns the layout revision.
	 */
	function emit(active: ResizeGesture, next: WindowGeometry): void {
		const last = active.emitted;
		if (
			next.x === last.x &&
			next.y === last.y &&
			next.width === last.width &&
			next.height === last.height
		) {
			return;
		}
		active.emitted = next;
		current.onGeometry?.(current.windowId, next);
	}

	function resize(event: PointerEvent): void {
		const active = gesture;
		if (!active || event.pointerId !== active.pointerId) return;
		event.preventDefault(); // no text selection / native drag mid-gesture
		emit(
			active,
			clampResizedGeometry(
				active.base,
				current.direction,
				event.clientX - active.startX,
				event.clientY - active.startY,
				current.min,
				current.viewport?.() ?? { width: window.innerWidth, height: window.innerHeight },
				active.origin
			)
		);
	}

	/**
	 * Ends the gesture; `revert` restores the committed base (NT1) — for a
	 * resize that is the already-clamped committed geometry, re-clamped
	 * against the live viewport exactly once.
	 */
	function finish(revert: boolean): void {
		const active = gesture;
		gesture = null;
		if (!active || !revert) return;
		emit(
			active,
			clampResizedGeometry(
				active.base,
				current.direction,
				0,
				0,
				current.min,
				current.viewport?.() ?? { width: window.innerWidth, height: window.innerHeight },
				active.origin
			)
		);
	}

	function release(pointerId: number): void {
		try {
			if (node.hasPointerCapture(pointerId)) node.releasePointerCapture(pointerId);
		} catch {
			// The pointer is already gone (e.g. after pointercancel); implicit
			// release has happened or will — nothing to recover.
		}
	}

	function end(event: PointerEvent): void {
		if (!gesture || event.pointerId !== gesture.pointerId) return;
		release(event.pointerId);
		finish(false); // committed live already; nothing to do but stop
	}

	function cancelled(event: PointerEvent): void {
		if (!gesture || event.pointerId !== gesture.pointerId) return;
		release(event.pointerId);
		finish(true);
	}

	function lostCapture(event: PointerEvent): void {
		// Capture lost without pointerup/pointercancel (e.g. the frame was
		// reparented or removed mid-gesture): treat as an interruption.
		if (!gesture || event.pointerId !== gesture.pointerId) return;
		finish(true);
	}

	node.addEventListener('pointerdown', begin);
	node.addEventListener('pointermove', resize);
	node.addEventListener('pointerup', end);
	node.addEventListener('pointercancel', cancelled);
	node.addEventListener('lostpointercapture', lostCapture);

	// Touch resizes must not be hijacked by scrolling (the pointerdown would
	// otherwise never reach `move` — the browser fires pointercancel).
	const previousTouchAction = node.style.touchAction;
	node.style.touchAction = 'none';

	return {
		update(next: WindowResizeParams): void {
			current = next;
		},
		destroy(): void {
			// Unmount mid-gesture (NT1): the window is leaving the workspace,
			// so no geometry is emitted; element-bound listeners die with the
			// node, capture is implicitly released, and the scoped style is
			// restored. Explicit removal covers hosts that detach the node
			// without running destroy.
			gesture = null;
			node.removeEventListener('pointerdown', begin);
			node.removeEventListener('pointermove', resize);
			node.removeEventListener('pointerup', end);
			node.removeEventListener('pointercancel', cancelled);
			node.removeEventListener('lostpointercapture', lostCapture);
			node.style.touchAction = previousTouchAction;
		}
	};
}
