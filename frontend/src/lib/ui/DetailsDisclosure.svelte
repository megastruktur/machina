<script lang="ts">
	/**
	 * DetailsDisclosure — compact in-flow disclosure primitive (chat-clean 1).
	 *
	 * Layer: shared UI primitive. Owns nothing: no window geometry, no server
	 * scoping, no global state, no module-level `window`/`document` access.
	 * Safe to render on the server.
	 *
	 * Accessibility contract:
	 *   - native <button> trigger: Enter/Space toggle via browser
	 *   - aria-expanded + aria-controls wired to panel id
	 *   - details region has aria-labelledby pointing at trigger label
	 *   - hidden panel uses the `hidden` attribute (display:none) so its
	 *     descendants are never keyboard-focusable
	 *   - Escape while open: close + focus returns to trigger
	 *   - reopening preserves child state (display:none vs unmount preserves state)
	 */
	import type { Snippet } from 'svelte';
	import { ChevronRight } from 'lucide-svelte';

	type Props = {
		/** Bound open state; defaults to false. */
		open?: boolean;
		/** Disclosure label text; default "Details". */
		label?: string;
		/** Stable id for this instance's panel; auto-generated if omitted. */
		id?: string;
		/** Details content via Snippet. */
		children?: Snippet;
		/** Optional alert / count indicator rendered after the label text. */
		alert?: Snippet;
		/** data-testid prefix for internal elements. */
		testId?: string;
	};

	/** Stable unique id per Svelte 5 instance — SSR-safe, no Math.random. */
	const generatedId = $props.id();

	let {
		open = $bindable(false),
		label = 'Details',
		id = generatedId,
		children,
		alert,
		testId = 'details-disclosure',
	}: Props = $props();

	const panelId = $derived(id);
	const triggerId = $derived(`mai-details-trigger-${id}`);
	const labelId = $derived(`mai-details-label-${id}`);

	/**
	 * Escape key handler on the wrapper div.
	 * Fires when focus is inside the panel (e.g. on a child input) and the user
	 * presses Escape — the event bubbles up to the nearest ancestor with a
	 * keydown listener. Capturing on the wrapper ensures we intercept it before
	 * it reaches the trigger or any outer shell.
	 */
	function handleWrapperKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			e.stopPropagation();
			open = false;
			const triggerEl = document.getElementById(triggerId);
			triggerEl?.focus();
		}
	}
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions:
  role="group" correctly identifies a disclosure widget group. The onkeydown
  handler catches Escape bubbles from focusable panel children (e.g. inputs);
  this is intentional and the correct a11y pattern for disclosure widgets. -->
<!-- svelte-ignore a11y_no_static_element_interactions:
  Same justification as above — group is the correct role for a disclosure. -->
<div
	class="mai-details"
	class:mai-details--open={open}
	role="group"
	onkeydown={handleWrapperKeydown}
>
	<!-- Trigger: compact icon+text button with native button semantics -->
	<button
		type="button"
		id={triggerId}
		class="mai-details__trigger"
		aria-expanded={open}
		aria-controls={panelId}
		onclick={() => (open = !open)}
		data-testid="{testId}-trigger"
	>
		<!-- Mechanicus sharp-geometry chevron: rotates 90° when open -->
		<span class="mai-details__icon" aria-hidden="true">
			<ChevronRight size={14} strokeWidth={2} />
		</span>
		<span class="mai-details__label" id={labelId}>{label}</span>
		{#if alert}
			<span class="mai-details__alert" aria-live="polite">
				{@render alert()}
			</span>
		{/if}
	</button>

	<!-- Details panel: hidden attribute = display:none; descendants never focusable -->
	<div
		id={panelId}
		class="mai-details__panel"
		role="region"
		aria-labelledby={labelId}
		aria-hidden={!open}
		hidden={!open}
		data-testid="{testId}-panel"
	>
		{#if children}
			{@render children()}
		{/if}
	</div>
</div>

<style>
	.mai-details {
		display: flex;
		flex-direction: column;
		width: 100%;
		font-family: var(--mai-font-sans, inherit);
	}

	/* ---- Trigger button ---- */
	.mai-details__trigger {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2, 8px);
		padding: var(--mai-space-1, 4px) var(--mai-space-3, 12px);
		min-height: var(--mai-target-min, 32px);
		background: transparent;
		border: var(--mai-border-width, 1px) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md, 2px);
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-sm, 0.875rem);
		font-weight: var(--mai-weight-medium, 500);
		cursor: pointer;
		text-align: left;
		white-space: nowrap;
		transition:
			background-color var(--mai-motion-fast) ease,
			border-color var(--mai-motion-fast) ease,
			color var(--mai-motion-fast) ease;
	}

	.mai-details__trigger:hover {
		background: var(--mai-color-surface-raised);
		border-color: var(--mai-color-brass-dim);
		color: var(--mai-color-text);
	}

	.mai-details__trigger:focus-visible {
		outline: var(--mai-focus-ring-width, 2px) solid var(--mai-focus-ring-color);
		outline-offset: var(--mai-focus-ring-offset, 2px);
	}

	/* ---- Chevron icon ---- */
	.mai-details__icon {
		display: inline-flex;
		align-items: center;
		flex-shrink: 0;
		color: currentColor;
		transition: transform var(--mai-motion-fast) ease;
	}

	:global(.mai-details--open) .mai-details__icon {
		transform: rotate(90deg);
	}

	/* ---- Label ---- */
	.mai-details__label {
		flex: 1;
		min-width: 0;
		overflow-wrap: break-word;
		word-break: break-word;
	}

	/* ---- Alert / count indicator slot ---- */
	.mai-details__alert {
		display: inline-flex;
		align-items: center;
		flex-shrink: 0;
	}

	/* ---- Panel ---- */
	.mai-details__panel {
		width: 100%;
		padding: var(--mai-space-3, 12px) var(--mai-space-4, 16px);
		border: var(--mai-border-width, 1px) solid var(--mai-color-border);
		border-top: none;
		border-radius: 0 0 var(--mai-radius-md, 2px) var(--mai-radius-md, 2px);
		background: var(--mai-color-surface);
		color: var(--mai-color-text);
		font-size: var(--mai-text-sm, 0.875rem);
		/* No overflow clipping — long content wraps naturally; parent controls overflow */
		overflow: visible;
		min-width: 0;
	}

	/* Show panel when not hidden */
	.mai-details__panel:not([hidden]) {
		display: block;
	}

	/* Mechanicus sharp join: trigger bottom border merges with panel top border when open */
	.mai-details--open .mai-details__trigger {
		border-bottom-left-radius: 0;
		border-bottom-right-radius: 0;
		border-bottom-color: transparent;
	}
</style>
