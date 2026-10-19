<script lang="ts">
	/**
	 * WindowFrame — reusable non-modal window chrome (card 01-window-frame, P01).
	 *
	 * Layer: window chrome (windowed-workspace ADR-0001 §2). Renders one live
	 * window from its validated schema-v1 descriptor: contextual title bar,
	 * control region and body region. It owns nothing else:
	 *
	 *  - No drag/resize pointer logic. The pointer adapters are owned by
	 *    `01-window-drag` / `01-window-resize`; this component contains no
	 *    pointermove/setPointerCapture code and no geometry clamping (NT1–NT3
	 *    belong to those producers) — it only BINDS the drag action to the
	 *    title bar and the resize action to the edge/corner handles. The only
	 *    pointer handling here is
	 *    activation + deterministic keyboard-focus placement on the title bar
	 *    (ADR §6, PT2), and it never prevents default or stops propagation,
	 *    so controls, text selection and future adapters keep working (NT2).
	 *  - No presentation transitions (01-window-presentation applies them via
	 *    the workspace state); the frame renders `descriptor.presentation` as
	 *    data and emits minimize/maximize intents only.
	 *  - No panel/chat/SSH/domain logic and no domain channel (no fetch/XHR/
	 *    WebSocket/EventSource): close is view-only intent emission — the
	 *    owning controller decides whether, and when, the window leaves the
	 *    workspace. Closing/focusing never starts, cancels or retargets
	 *    backend work (ADR §7.2, NT6; contracts.md C08 rule 5, C11).
	 *  - No stores and no module-level state: everything enters through props
	 *    and leaves through callbacks, so instances share nothing, no hidden
	 *    global "current server" mutation target can exist (ADR §2, NT5), and
	 *    the module is safe to evaluate per SSR request (ADR §5).
	 *
	 * Controlled-component contract: `descriptor` and `focused` come from the
	 * owning workspace controller (already validated by the state layer);
	 * mutations are requested through the `on*` intent callbacks, each
	 * receiving the stable opaque `window_id` (view identity only, ADR §3.1).
	 * Emission is idempotent-safe: focusing the already-focused window is a
	 * no-op at the controller, so click and focusin may both emit for one
	 * interaction. Activation emits on click COMPLETION, never on
	 * pointerdown — a synchronous raise reparents the frame in the stacking
	 * order, which cancels an in-flight pointer gesture (see onPointerDown).
	 *
	 * Accessibility (ADR §6): `role="dialog"`, `aria-modal="false"`,
	 * `aria-labelledby` pointing at the title heading, which carries the
	 * accessible name "«panel title» — «server/context label»"; a global
	 * scope always reads "Global", never a caller-supplied server name (ADR
	 * §3.2). The title bar is focusable, every chrome control is a labeled
	 * native button (native Enter/Space activation and the token-layer
	 * `:focus-visible` ring), and there is no focus trap: tab order follows
	 * DOM order.
	 *
	 * Positioning: absolutely positioned inside the owning desktop region (a
	 * positioned ancestor) from `descriptor.geometry`/`descriptor.z`, rendered
	 * as given — viewport/min-size clamping is upstream (drag/resize/restore
	 * paths hold the viewport). The `min-width`/`min-height` floors keep the
	 * chrome above the frozen NT3 visible title-bar area in CSS.
	 *
	 * Tokens: colors, spacing, type and motion come only from ui/tokens.css
	 * (including the frozen `--mai-titlebar-min-*` floors); no theme value is
	 * redeclared here and no dependency is added (ADR §4.3).
	 */
	import type { Snippet } from 'svelte';
	import { windowDrag } from './drag.js';
	import { RESIZE_DIRECTIONS, windowResize, type MinGeometry } from './resize.js';
	import {
		DEFAULT_MIN_GEOMETRY,
		type WindowDescriptor,
		type WindowGeometry,
		type WindowId
	} from './types.js';

	interface Props {
		/** Validated live descriptor from the workspace state (schema v1). */
		descriptor: WindowDescriptor;
		/** Display title (registry vocabulary is a later card; display-only). */
		title: string;
		/**
		 * Display-only context label for server-scoped windows. Ignored for
		 * global scope, which always reads "Global" (ADR §3.2).
		 */
		contextLabel?: string;
		/** Whether this window is the focused one (drives active styling). */
		focused?: boolean;
		/** Activation intent: bring this window to front (click/keyboard). */
		onactivate?: (windowId: WindowId) => void;
		/** Minimize intent (the transition itself stays with the controller). */
		onminimize?: (windowId: WindowId) => void;
		/** Maximize/restore toggle intent. */
		ontogglemaximize?: (windowId: WindowId) => void;
		/** Close intent — view-only; the controller performs the close. */
		onclose?: (windowId: WindowId) => void;
		/**
		 * Drag geometry intent from the title-bar pointer adapter
		 * (`01-window-drag`, PT1/NT1/NT3): live clamped moves and interrupt
		 * reverts. The controller applies them through the workspace `move`
		 * transition — the frame never mutates the shared state itself.
		 * The resize adapter (`01-window-resize`, PT3/NT1/NT3) emits through
		 * the same single geometry intent channel.
		 */
		ongeometry?: (windowId: WindowId, geometry: WindowGeometry) => void;
		/**
		 * Per-entry minimum size clamp for the resize handles (ADR §10.1
		 * PT3: "per-entry `min_geometry` clamp, global default 320×200 when
		 * unset"). Registry wiring passes the entry's `min_geometry`; the
		 * frozen `DEFAULT_MIN_GEOMETRY` applies when omitted.
		 */
		minGeometry?: MinGeometry;
		/** Extra chrome controls, rendered before the default window controls. */
		controls?: Snippet;
		/**
		 * Automatically size the window height to match its intrinsic content
		 * height, capped only when the content exceeds the available screen/canvas
		 * height. Resets to manual height if the user drags a vertical resize handle.
		 */
		autoFitHeight?: boolean;
		/** Panel content (the window body). */
		children?: Snippet;
		class?: string;
	}

	let {
		descriptor,
		title,
		contextLabel = '',
		focused = false,
		onactivate,
		onminimize,
		ontogglemaximize,
		onclose,
		ongeometry,
		minGeometry = DEFAULT_MIN_GEOMETRY,
		controls,
		autoFitHeight = false,
		children,
		class: className = ''
	}: Props = $props();

	const titleId = $props.id();

	let frameElement = $state<HTMLElement | null>(null);
	let titleHeading = $state<HTMLHeadingElement | null>(null);
	/** Measured by the resize adapter at gesture start (NT3 viewport origin). */
	let titlebarElement = $state<HTMLElement | null>(null);
	let bodyElement = $state<HTMLElement | null>(null);
	let contentElement = $state<HTMLElement | null>(null);

	/** Set to true when the operator manually drags a vertical resize handle. */
	let userResized = $state(false);
	let fitHeightPx = $state<number | null>(null);
	let maxHeightPx = $state<number | null>(null);
	let contentCapped = $state(false);

	const activeAutoFit = $derived(
		autoFitHeight && !userResized && descriptor.presentation === 'normal'
	);

	function recomputeAutoHeight(): void {
		if (!activeAutoFit || !frameElement || !bodyElement || !contentElement) return;
		const titleH = titlebarElement?.offsetHeight ?? 40;
		const bodyStyle = getComputedStyle(bodyElement);
		const padY =
			(parseFloat(bodyStyle.paddingTop) || 0) + (parseFloat(bodyStyle.paddingBottom) || 0);
		const borderH = Math.max(2, frameElement.offsetHeight - frameElement.clientHeight);
		const contentH = contentElement.scrollHeight;
		// +2px buffer prevents sub-pixel rounding from triggering a 1px scrollbar.
		const desiredHeight = Math.ceil(titleH + padY + contentH + borderH + 2);

		const parent = frameElement.offsetParent as HTMLElement | null;
		const parentRect = parent?.getBoundingClientRect();
		const bottomMargin = 16;
		const frameTopInViewport = (parentRect?.top ?? 0) + descriptor.geometry.y;
		const maxByViewport = Math.floor(window.innerHeight - frameTopInViewport - bottomMargin);
		const maxByCanvas = parentRect
			? Math.floor(parentRect.height - descriptor.geometry.y - bottomMargin)
			: maxByViewport;
		const maxAvailable = Math.max(
			minGeometry.height,
			Math.min(maxByViewport, maxByCanvas)
		);

		contentCapped = desiredHeight > maxAvailable;
		fitHeightPx = Math.max(minGeometry.height, Math.min(desiredHeight, maxAvailable));
		maxHeightPx = maxAvailable;
	}

	$effect(() => {
		if (!activeAutoFit || !contentElement || !frameElement) return;
		// Track descriptor.geometry.y / width changes so dragging or width-resizing
		// immediately updates the available vertical room and wrapped text height.
		void descriptor.geometry.y;
		void descriptor.geometry.width;
		recomputeAutoHeight();

		const ro = new ResizeObserver(() => recomputeAutoHeight());
		ro.observe(contentElement);
		if (bodyElement) ro.observe(bodyElement);

		const mo = new MutationObserver(() => recomputeAutoHeight());
		mo.observe(contentElement, { childList: true, subtree: true, attributes: true });

		const onWinResize = () => recomputeAutoHeight();
		window.addEventListener('resize', onWinResize);

		return () => {
			ro.disconnect();
			mo.disconnect();
			window.removeEventListener('resize', onWinResize);
		};
	});

	/** A `title_override` replaces the registry display title (ADR §8). */
	const displayTitle = $derived(descriptor.title_override ?? title);
	/** Global scope always reads Global — never a last-selected server name. */
	const contextText = $derived(
		descriptor.scope.kind === 'global' ? 'Global' : contextLabel !== '' ? contextLabel : 'Server'
	);
	const maximizeLabel = $derived(descriptor.presentation === 'maximized' ? 'Restore' : 'Maximize');

	/**
	 * Interactive descendants: a pointerdown on one of them is a control/body
	 * interaction, so keyboard focus must stay with the control the user
	 * clicked (native focus), not be moved to the title bar over their head.
	 */
	const INTERACTIVE_SELECTOR =
		'button, input, select, textarea, a[href], [contenteditable], [tabindex]';

	/**
	 * True between pointerdown and pointerup. Raising a window reparents it
	 * in the stacking order; doing that MID-GESTURE makes Chromium cancel the
	 * in-flight click (observed 2026-10-13: mousedown retargeted, click never
	 * dispatched). So activation emits on click completion, and focusin
	 * caused by the pointer gesture stays silent (its click emits instead).
	 */
	let pointerGesture = false;

	function onPointerDown(event: PointerEvent): void {
		pointerGesture = true;
		const target = event.target;
		if (!(target instanceof Element) || !target.closest(INTERACTIVE_SELECTOR)) {
			titleHeading?.focus();
		}
	}

	function onPointerUp(): void {
		pointerGesture = false;
	}

	function onClick(): void {
		// PT2: activating an inactive window (click anywhere) raises it.
		if (!focused) onactivate?.(descriptor.window_id);
	}

	function onFocusIn(): void {
		// PT2 keyboard half: tabbing into an inactive window raises it.
		if (!pointerGesture && !focused) onactivate?.(descriptor.window_id);
	}
</script>

<!-- role="dialog" on a plain div (APG dialog pattern): a <section> would
     trip a11y_no_noninteractive_element_to_interactive_role while the dialog
     role is exactly the frozen contract (ADR §6). -->
<!-- Click-to-activate on the window surface has a complete keyboard
     equivalent (title bar and every control are tab-reachable; focusin
     raises the window), so no extra key handler belongs on the container
     (ADR §6: no focus traps, no shortcut suite). -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div
	bind:this={frameElement}
	class="mai-window-frame {className}"
	class:mai-window-frame--focused={focused}
	class:mai-window-frame--auto-height={activeAutoFit && fitHeightPx !== null}
	class:mai-window-frame--capped={activeAutoFit && contentCapped}
	role="dialog"
	aria-modal="false"
	aria-labelledby={titleId}
	tabindex="-1"
	data-window-id={descriptor.window_id}
	data-panel-kind={descriptor.panel_kind}
	data-presentation={descriptor.presentation}
	data-focused={focused ? 'true' : 'false'}
	style:left="{descriptor.geometry.x}px"
	style:top="{descriptor.geometry.y}px"
	style:width="{descriptor.geometry.width}px"
	style:height="{descriptor.geometry.height}px"
	style:--mai-window-fit-h={activeAutoFit && fitHeightPx !== null ? `${fitHeightPx}px` : undefined}
	style:--mai-window-max-h={activeAutoFit && maxHeightPx !== null ? `${maxHeightPx}px` : undefined}
	style:z-index={descriptor.z}
	onpointerdown={onPointerDown}
	onpointerup={onPointerUp}
	onclick={onClick}
	onfocusin={onFocusIn}
>
	<!-- Title bar = the only drag origin (PT1): the reviewed pointer adapter
	     owns the gesture (capture, NT1 cleanup, NT3 clamp) and emits
	     geometry intents; interactive descendants are excluded inside the
	     action, so chrome controls keep their native behavior (NT2). -->
	<header
		bind:this={titlebarElement}
		class="mai-window-frame__titlebar"
		use:windowDrag={{
			windowId: descriptor.window_id,
			geometry: () => descriptor.geometry,
			onGeometry: ongeometry
		}}
	>
		<!-- The title bar is keyboard-focusable by frozen contract (ADR §6:
		     "Title bar is focusable") even though a heading is not itself an
		     interactive widget — tabbing to it is the keyboard activation
		     path (focusin raises the window), so a positive tabindex on the
		     heading is required, not a typo. -->
		<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
		<h2 class="mai-window-frame__title" id={titleId} tabindex="0" bind:this={titleHeading}>
			<span class="mai-window-frame__title-text">{displayTitle}</span>
			<span class="mai-window-frame__title-sep">—</span>
			<span class="mai-window-frame__title-context">{contextText}</span>
		</h2>
		<div class="mai-window-frame__window-controls">
			{@render controls?.()}
			<button
				type="button"
				class="mai-window-frame__control"
				aria-label="Minimize"
				onclick={() => onminimize?.(descriptor.window_id)}
			>
				<svg viewBox="0 0 10 10" aria-hidden="true">
					<path d="M1.5 8.5h7" />
				</svg>
			</button>
			<button
				type="button"
				class="mai-window-frame__control"
				aria-label={maximizeLabel}
				onclick={() => ontogglemaximize?.(descriptor.window_id)}
			>
				<svg viewBox="0 0 10 10" aria-hidden="true">
					<rect x="1.5" y="1.5" width="7" height="7" />
				</svg>
			</button>
			<button
				type="button"
				class="mai-window-frame__control mai-window-frame__control--close"
				aria-label="Close"
				onclick={() => onclose?.(descriptor.window_id)}
			>
				<svg viewBox="0 0 10 10" aria-hidden="true">
					<path d="M1.5 1.5l7 7M8.5 1.5l-7 7" />
				</svg>
			</button>
		</div>
	</header>
	<div class="mai-window-frame__body" bind:this={bodyElement}>
		<div class="mai-window-frame__content" bind:this={contentElement}>
			{@render children?.()}
		</div>
	</div>
	<!-- Resize handles (PT3): one reusable pointer adapter per direction,
	     bound to edge/corner hit strips. They exist only in `normal`
	     presentation — a maximized frame's geometry belongs to the
	     presentation owner (01-window-presentation), not to a pointer
	     gesture. Handles are display affordances, hidden from the a11y
	     tree; the keyboard Resize mode (ADR §6) is the frozen accessibility
	     equivalent and stays with the phase-gate a11y work, like keyboard
	     Move. Being outside the title bar they structurally cannot initiate
	     a drag, and chrome controls stack above the strips (NT2). -->
	{#if descriptor.presentation === 'normal'}
		{#each RESIZE_DIRECTIONS as direction (direction)}
			<div
				class="mai-window-frame__resize-handle"
				data-resize-direction={direction}
				aria-hidden="true"
				onpointerdown={() => {
					if (direction.includes('n') || direction.includes('s')) {
						userResized = true;
					}
				}}
				use:windowResize={{
					windowId: descriptor.window_id,
					direction,
					geometry: () => descriptor.geometry,
					min: minGeometry,
					titleBar: () => titlebarElement?.getBoundingClientRect() ?? null,
					onGeometry: ongeometry
				}}
			></div>
		{/each}
	{/if}
</div>

<style>
	.mai-window-frame {
		position: absolute;
		display: flex;
		flex-direction: column;
		/* NT3 floor: chrome never renders below the frozen visible title-bar
		   area; viewport clamping itself stays with drag/resize/restore. */
		min-width: var(--mai-titlebar-min-w);
		min-height: var(--mai-titlebar-min-h);
		background: var(--mai-surface-glass, var(--mai-color-surface));
		backdrop-filter: var(--mai-blur-glass);
		-webkit-backdrop-filter: var(--mai-blur-glass);
		color: var(--mai-color-text);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-xl, var(--mai-radius-lg));
		box-shadow: var(--mai-shadow-window);
		font-family: var(--mai-font-sans);
		overflow: hidden;
		transition:
			border-color var(--mai-motion-fast) ease,
			box-shadow var(--mai-motion-fast) ease;
	}

	/* Active focus styling: border, title voice and context badge all come
	   from token pairs, so the state difference is visible, not color-only. */
	.mai-window-frame--focused {
		border-color: var(--mai-color-primary);
		box-shadow: var(--mai-shadow-window-focused, var(--mai-shadow-window));
	}

	/* Brass auspex corner reticles on focused Mechanicus windows */
	.mai-window-frame--focused::after {
		content: '';
		position: absolute;
		inset: 0;
		pointer-events: none;
		z-index: 4;
		background:
			linear-gradient(to right, var(--mai-color-brass) 10px, transparent 10px) 0 0 / 100% 2px
				no-repeat,
			linear-gradient(to bottom, var(--mai-color-brass) 10px, transparent 10px) 0 0 / 2px 100%
				no-repeat,
			linear-gradient(to left, var(--mai-color-brass) 10px, transparent 10px) 100% 0 / 100% 2px
				no-repeat,
			linear-gradient(to bottom, var(--mai-color-brass) 10px, transparent 10px) 100% 0 / 2px 100%
				no-repeat,
			linear-gradient(to right, var(--mai-color-brass) 10px, transparent 10px) 0 100% / 100% 2px
				no-repeat,
			linear-gradient(to top, var(--mai-color-brass) 10px, transparent 10px) 0 100% / 2px 100%
				no-repeat,
			linear-gradient(to left, var(--mai-color-brass) 10px, transparent 10px) 100% 100% / 100% 2px
				no-repeat,
			linear-gradient(to top, var(--mai-color-brass) 10px, transparent 10px) 100% 100% / 2px 100%
				no-repeat;
	}

	.mai-window-frame__titlebar {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		flex: none;
		min-height: calc(var(--mai-titlebar-min-h) + 4px);
		padding: var(--mai-space-1) var(--mai-space-2) var(--mai-space-1) var(--mai-space-3);
		background: var(--mai-surface-glass-header, var(--mai-color-bg));
		border-bottom: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: calc(var(--mai-radius-xl, var(--mai-radius-lg)) - var(--mai-border-width))
			calc(var(--mai-radius-xl, var(--mai-radius-lg)) - var(--mai-border-width)) 0 0;
		cursor: grab;
	}

	.mai-window-frame--focused .mai-window-frame__titlebar {
		border-bottom-color: var(--mai-color-brass);
		box-shadow: inset 0 -1px 0 var(--mai-color-brass-dim);
	}

	.mai-window-frame__title {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2);
		min-width: 0;
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.04em;
		text-transform: uppercase;
		line-height: var(--mai-leading-tight);
		color: var(--mai-color-text-muted);
	}

	.mai-window-frame--focused .mai-window-frame__title {
		color: var(--mai-color-text);
	}

	.mai-window-frame__title-text {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.mai-window-frame__title-sep {
		color: var(--mai-color-border-strong);
	}

	.mai-window-frame__title-context {
		flex: none;
		padding: 1px var(--mai-space-2);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		letter-spacing: 0.03em;
		text-transform: none;
		color: var(--mai-status-neutral-text);
		background: var(--mai-status-neutral-bg);
		border: var(--mai-border-width) solid var(--mai-status-neutral-border);
		border-left: var(--mai-chip-accent-width, 3px) solid var(--mai-color-brass-dim);
		border-radius: var(--mai-radius-sm);
		white-space: nowrap;
	}

	.mai-window-frame--focused .mai-window-frame__title-context {
		color: var(--mai-status-info-text);
		background: var(--mai-status-info-bg);
		border-color: var(--mai-status-info-border);
		border-left-color: var(--mai-color-brass);
	}

	.mai-window-frame__window-controls {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-1);
		margin-left: auto;
	}

	.mai-window-frame__control {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: var(--mai-target-min);
		height: var(--mai-target-min);
		padding: 0;
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface);
		color: var(--mai-color-text-muted);
		cursor: pointer;
		/* Chrome controls stack above the edge/corner resize strips, so a
		   press anywhere on a control is always a control interaction (NT2). */
		position: relative;
		z-index: 3;
	}

	.mai-window-frame--focused .mai-window-frame__control {
		color: var(--mai-color-text);
		border-color: var(--mai-color-brass-dim);
	}

	.mai-window-frame__control:hover {
		background: var(--mai-status-neutral-bg);
		border-color: var(--mai-color-brass);
		color: var(--mai-color-text);
	}

	.mai-window-frame__control--close:hover {
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
		border-color: var(--mai-status-danger-border);
	}

	.mai-window-frame__control svg {
		width: 10px;
		height: 10px;
		fill: none;
		stroke: currentColor;
		stroke-width: 1.5;
		stroke-linecap: round;
	}

	.mai-window-frame__body {
		flex: 1 1 auto;
		min-height: 0;
		overflow: auto;
		padding: var(--mai-panel-pad, var(--mai-space-4));
	}

	/* Auto-fit content height mode: the frame sizes to the exact intrinsic
	   height of its content (`--mai-window-fit-h`) up to the screen/canvas
	   limit (`--mai-window-max-h`). While content fits within the screen limit,
	   vertical overflow is hidden so no redundant scrollbar appears; when the
	   content exceeds the screen limit (`--capped`), `.mai-window-frame__body`
	   scrolls cleanly. */
	.mai-window-frame--auto-height:not(.mai-desktop-shell__frame--maximized) {
		height: var(--mai-window-fit-h) !important;
		max-height: var(--mai-window-max-h) !important;
	}

	.mai-window-frame--auto-height:not(.mai-window-frame--capped) .mai-window-frame__body {
		overflow-y: hidden;
	}

	.mai-window-frame__content {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}

	.mai-window-frame--auto-height .mai-window-frame__content :global(.mai-panel-host),
	.mai-window-frame--auto-height .mai-window-frame__content :global(.mai-panel-host__body),
	.mai-window-frame--auto-height .mai-window-frame__content :global(.mai-panel-host__body > *) {
		height: auto !important;
		max-height: none !important;
		overflow: visible !important;
	}

	/* Resize hit strips (PT3). Inside the frame box: the frame's
	   `overflow: hidden` clips anything past the border, and strips outside
	   it would be dead area. Corners (12×12) sit above the edge strips'
	   ends, so the corner gesture always wins at a corner. The 6px
	   east/south strips overlay the body edge incl. its scrollbar rim —
	   the standard overlay-handle tradeoff; the scrollbar thumb keeps a
	   usable grab area beside the strip. Keyboard resize is the ADR §6
	   accessibility path and does not live on these pointer strips. */
	.mai-window-frame__resize-handle {
		position: absolute;
		z-index: 2;
	}

	.mai-window-frame__resize-handle[data-resize-direction='n'] {
		top: 0;
		right: 10px;
		left: 10px;
		height: 6px;
		cursor: ns-resize;
	}

	.mai-window-frame__resize-handle[data-resize-direction='s'] {
		right: 10px;
		bottom: 0;
		left: 10px;
		height: 6px;
		cursor: ns-resize;
	}

	.mai-window-frame__resize-handle[data-resize-direction='e'] {
		top: 10px;
		right: 0;
		bottom: 10px;
		width: 6px;
		cursor: ew-resize;
	}

	.mai-window-frame__resize-handle[data-resize-direction='w'] {
		top: 10px;
		bottom: 10px;
		left: 0;
		width: 6px;
		cursor: ew-resize;
	}

	.mai-window-frame__resize-handle[data-resize-direction='ne'] {
		top: 0;
		right: 0;
		width: 12px;
		height: 12px;
		cursor: nesw-resize;
	}

	.mai-window-frame__resize-handle[data-resize-direction='se'] {
		right: 0;
		bottom: 0;
		width: 12px;
		height: 12px;
		cursor: nwse-resize;
	}

	.mai-window-frame__resize-handle[data-resize-direction='sw'] {
		bottom: 0;
		left: 0;
		width: 12px;
		height: 12px;
		cursor: nesw-resize;
	}

	.mai-window-frame__resize-handle[data-resize-direction='nw'] {
		top: 0;
		left: 0;
		width: 12px;
		height: 12px;
		cursor: nwse-resize;
	}
</style>
