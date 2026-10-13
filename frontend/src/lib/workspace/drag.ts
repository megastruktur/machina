/**
 * Pointer drag adapter for window frames — card `01-window-drag`, P01.
 *
 * Svelte action bound to a `WindowFrame` title bar (windowed-workspace ADR
 * §10.1 PT1): `pointerdown` on the title bar excluding interactive
 * descendants starts a gesture under pointer capture; `pointermove` emits
 * clamped geometry through the owning controller (the frame is a controlled
 * component — the shared state is mutated only by its `move` transition);
 * `pointerup` releases capture. Per PT1 the per-move emissions ARE the live
 * geometry updates of the shared state; persistence of the layout document
 * is a separate producer (`01-window-layout`) and is deliberately not
 * touched here, and neither is resize (`01-window-resize`).
 *
 * Frozen bounds (NT3): after any drag the window retains a visible,
 * hit-testable title-bar area of at least `TITLE_BAR_MIN` (64×32 CSS px)
 * inside the viewport. Geometry coordinates are relative to the frame's
 * positioned ancestor, so the clamp runs against that ancestor's viewport
 * origin (measured once per gesture from the live bounding rect) — not
 * against raw descriptor coordinates.
 *
 * Interrupted gestures (NT1): `pointercancel`, lost pointer capture or the
 * unmount of the frame release the capture, emit no further geometry and
 * revert to the gesture-start (last committed) geometry. Cleanup is
 * structural: every listener lives on the bound node itself, capture is
 * released on the same node, and the only style the action sets
 * (`touch-action`, so touch drags are not hijacked by scrolling) is
 * restored on destroy — no document/window listeners, no global state, no
 * styles that can outlive the element (§10.2 NT1 "no stuck capture, no
 * stuck pointer styles").
 *
 * Wrong origins (NT2): `pointerdown` on buttons, inputs, selects,
 * textareas, links or contenteditable descendants never starts a drag, so
 * chrome controls and body interactions keep their native behavior. The
 * title heading itself is a valid drag origin — it is a heading, not a
 * control; ADR §6's [tabindex] title focusability is a keyboard path, not
 * interactivity. Body scrolling/text selection cannot initiate a move
 * structurally: the action only listens on the title bar, and it cancels
 * `pointermove` during a gesture, which suppresses the compatibility mouse
 * events that text selection and native drag would need (native focus from
 * `mousedown` is deliberately NOT suppressed, keeping PT2 intact).
 *
 * Isolation (ADR §5, C08 rule 5, C11): no module-level mutable state, no
 * `window`/`document` access at module top level, no stores. Gesture state
 * lives in the action closure — one per mounted title bar — so nothing is
 * shared between windows, tabs or SSR requests, and there is no hidden
 * global mutation target.
 */

import type { ActionReturn } from 'svelte/action';
import { TITLE_BAR_MIN, type WindowGeometry, type WindowId } from './types.js';

/** Viewport size in CSS pixels — the NT3 clamp boundary. */
export interface DragViewport {
	width: number;
	height: number;
}

/**
 * Viewport geometry of the frame's title bar, measured at gesture start.
 * `left`/`top` are the title bar's viewport position when the descriptor
 * x/y are 0 (i.e. the positioned ancestor's origin plus the frame border);
 * `barWidth` is the measured title-bar width — the NT3 hit target is the
 * bar itself, which is narrower than the descriptor width by the frame's
 * two borders, so the clamp must not assume they are equal.
 */
export interface DragOrigin {
	left: number;
	top: number;
	barWidth: number;
}

/**
 * Action parameters. `geometry()` reads the controller's committed
 * descriptor at gesture start (the revert base); `onGeometry` receives
 * clamped integer geometry for live moves and interrupt reverts — the
 * controller applies it through the workspace `move` transition.
 */
export interface WindowDragParams {
	windowId: WindowId;
	geometry(): WindowGeometry;
	onGeometry?(windowId: WindowId, geometry: WindowGeometry): void;
	/**
	 * Viewport provider (CSS pixels). Defaults to the browser window;
	 * injectable so embedders with scroll-offset frames stay honest.
	 */
	viewport?(): DragViewport;
}

/**
 * Interactive descendants of the title bar: a `pointerdown` on one of them
 * is a control interaction, never a drag start (NT2). Body scrollbars,
 * body text and body controls cannot reach this handler at all — the
 * action is bound to the title bar only.
 */
const INTERACTIVE_SELECTOR =
	'button, input, select, textarea, a[href], [contenteditable]';

function defaultViewport(): DragViewport {
	return { width: window.innerWidth, height: window.innerHeight };
}

/**
 * NT3 clamp: returns x/y so the title bar keeps a `TITLE_BAR_MIN` visible,
 * hit-testable area inside the viewport, preserving descriptor width and
 * height (sizes belong to open/resize/restore, not to dragging). The frame
 * sits at `origin + {x, y}` in viewport coordinates; its title bar spans
 * the full frame width from the frame top with a `--mai-titlebar-min-h`
 * (≥ 32 px) floor. Result x/y are integers (ADR §8 rule 2 — the state
 * layer validates integers). For degenerate inputs (window narrower than
 * 2×64 px) the upper bound wins, which cannot hide the title bar further
 * than the viewport's own edge.
 */
export function clampDraggedGeometry(
	geometry: WindowGeometry,
	viewport: DragViewport,
	origin: DragOrigin
): WindowGeometry {
	const minX = TITLE_BAR_MIN.width - origin.barWidth - origin.left;
	const maxX = viewport.width - TITLE_BAR_MIN.width - origin.left;
	const minY = -origin.top;
	const maxY = viewport.height - TITLE_BAR_MIN.height - origin.top;
	return {
		x: Math.round(Math.min(maxX, Math.max(minX, geometry.x))),
		y: Math.round(Math.min(maxY, Math.max(minY, geometry.y))),
		width: geometry.width,
		height: geometry.height
	};
}

/** Live gesture record — exists only between pointerdown and release. */
interface DragGesture {
	pointerId: number;
	/** Pointer position at gesture start (client coordinates). */
	startX: number;
	startY: number;
	/** Positioned-ancestor viewport origin + measured bar width, at start. */
	origin: DragOrigin;
	/** Committed geometry at gesture start — the NT1 revert base. */
	base: WindowGeometry;
	/** Last emitted position, so pinned moves don't churn the revision. */
	emittedX: number;
	emittedY: number;
}

/**
 * The drag action. Bind on the title bar element:
 * `use:windowDrag={{ windowId, geometry, onGeometry }}`.
 */
export function windowDrag(
	node: HTMLElement,
	params: WindowDragParams
): ActionReturn<WindowDragParams> {
	let current = params;
	let gesture: DragGesture | null = null;

	function begin(event: PointerEvent): void {
		if (gesture) return; // one live gesture per title bar
		if (!event.isPrimary || event.button !== 0) return;
		const target = event.target;
		if (target instanceof Element && target.closest(INTERACTIVE_SELECTOR)) return;
		const base = current.geometry();
		if (!base) return;
		const rect = node.getBoundingClientRect();
		const active: DragGesture = {
			pointerId: event.pointerId,
			startX: event.clientX,
			startY: event.clientY,
			origin: {
				left: rect.left - base.x,
				top: rect.top - base.y,
				barWidth: rect.width
			},
			base: { ...base },
			emittedX: base.x,
			emittedY: base.y
		};
		try {
			// Capture on the bound title bar: pointer events keep targeting the
			// frame (and bubble to it) even when the pointer leaves the window
			// or crosses sibling frames — PT1's "release outside the frame"
			// keeps tracking without any document-level listeners.
			node.setPointerCapture(event.pointerId);
		} catch {
			return; // stale/inactive pointer id — nothing to drag with
		}
		gesture = active;
	}

	function drag(event: PointerEvent): void {
		const active = gesture;
		if (!active || event.pointerId !== active.pointerId) return;
		event.preventDefault(); // no text selection / native drag mid-gesture
		const x = active.base.x + (event.clientX - active.startX);
		const y = active.base.y + (event.clientY - active.startY);
		if (x === active.emittedX && y === active.emittedY) return;
		const next = clampDraggedGeometry(
			{ x, y, width: active.base.width, height: active.base.height },
			current.viewport?.() ?? defaultViewport(),
			active.origin
		);
		if (next.x === active.emittedX && next.y === active.emittedY) return;
		active.emittedX = next.x;
		active.emittedY = next.y;
		current.onGeometry?.(current.windowId, next);
	}

	/** Ends the gesture; `revert` restores the committed base (NT1). */
	function finish(revert: boolean): void {
		const active = gesture;
		gesture = null;
		if (!active || !revert) return;
		const restored = clampDraggedGeometry(
			active.base,
			current.viewport?.() ?? defaultViewport(),
			active.origin
		);
		if (restored.x === active.emittedX && restored.y === active.emittedY) return;
		active.emittedX = restored.x;
		active.emittedY = restored.y;
		current.onGeometry?.(current.windowId, restored);
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
	node.addEventListener('pointermove', drag);
	node.addEventListener('pointerup', end);
	node.addEventListener('pointercancel', cancelled);
	node.addEventListener('lostpointercapture', lostCapture);

	// Touch drags must not be hijacked by scrolling (the pointerdown would
	// otherwise never reach `move` — the browser fires pointercancel).
	const previousTouchAction = node.style.touchAction;
	node.style.touchAction = 'none';

	return {
		update(next: WindowDragParams): void {
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
			node.removeEventListener('pointermove', drag);
			node.removeEventListener('pointerup', end);
			node.removeEventListener('pointercancel', cancelled);
			node.removeEventListener('lostpointercapture', lostCapture);
			node.style.touchAction = previousTouchAction;
		}
	};
}
