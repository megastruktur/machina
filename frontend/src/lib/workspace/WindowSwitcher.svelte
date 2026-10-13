<script module lang="ts">
	/**
	 * WindowSwitcher — reusable taskbar/window switcher (card
	 * `01-window-presentation`, P01).
	 *
	 * Frozen taskbar contract (windowed-workspace ADR §6): `role="toolbar"`
	 * with `aria-label="Open windows"`; one entry per live window — a native
	 * button whose accessible name identifies panel + server/object
	 * ("«panel title» — «server label»", global scope reads "Global"), with
	 * the minimized state conveyed by `aria-pressed` (PT4). Entries remain
	 * for hidden windows and disappear when a window closes (PT5); a
	 * visible window's entry activates/focuses it, a hidden one's restores
	 * and focuses it — the transition itself belongs to the presentation
	 * controller (`./presentation.ts`), reached through the `onactivate`
	 * intent. All controls are native buttons: keyboard reachable and
	 * operable with the token-layer focus ring, no focus trap (ADR §6).
	 *
	 * Controlled, store-free component (ADR §2 layer table): everything
	 * enters through props and leaves through the `onactivate` callback, so
	 * instances share nothing, there is no hidden global mutation target
	 * (NT5) and the module is safe to evaluate per SSR request (ADR §5).
	 * Labels are display vocabulary supplied by the composition (registry
	 * title / pinned-scope label at open time); this component owns no
	 * domain data, no scope resolution and no presentation state.
	 */

	/** One taskbar entry — the switcher-visible projection of one window. */
	export interface SwitcherEntry {
		/** Opaque view identity of the window (ADR §3.1; never a target id). */
		window_id: string;
		/** Display title (`title_override` already applied by the caller). */
		title: string;
		/**
		 * Display-only context label: the pinned server's label, or "Global"
		 * for a global scope. Empty renders the title alone.
		 */
		context_label: string;
		/** Hidden (minimized) state — conveyed as `aria-pressed` (ADR §6). */
		minimized: boolean;
		/** Whether this window is the focused one (visual state only). */
		focused: boolean;
	}
</script>

<script lang="ts">
	interface Props {
		/** One entry per live window, caller-ordered (registration order). */
		entries: readonly SwitcherEntry[];
		/**
		 * Activation intent for one entry — restore+focus when minimized,
		 * focus when visible (PT4/PT2). The controller performs it.
		 */
		onactivate?: (windowId: string) => void;
		class?: string;
	}

	let { entries, onactivate, class: className = '' }: Props = $props();

	/** Accessible name "«panel title» — «server label»" (ADR §6 taskbar). */
	function accessibleName(entry: SwitcherEntry): string {
		return entry.context_label !== '' ? `${entry.title} — ${entry.context_label}` : entry.title;
	}
</script>

<div
	class="mai-window-switcher {className}"
	role="toolbar"
	aria-label="Open windows"
	data-testid="window-switcher"
>
	{#each entries as entry (entry.window_id)}
		<button
			type="button"
			class="mai-window-switcher__entry"
			class:mai-window-switcher__entry--minimized={entry.minimized}
			class:mai-window-switcher__entry--focused={entry.focused}
			aria-pressed={entry.minimized}
			aria-label={accessibleName(entry)}
			data-window-id={entry.window_id}
			data-minimized={entry.minimized ? 'true' : 'false'}
			data-focused={entry.focused ? 'true' : 'false'}
			onclick={() => onactivate?.(entry.window_id)}
		>
			<span class="mai-window-switcher__entry-title">{entry.title}</span>
			{#if entry.context_label !== ''}
				<span class="mai-window-switcher__entry-sep" aria-hidden="true">—</span>
				<span class="mai-window-switcher__entry-context">{entry.context_label}</span>
			{/if}
		</button>
	{/each}
</div>

<style>
	.mai-window-switcher {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		min-height: var(--mai-target-min);
		padding: var(--mai-space-1) var(--mai-space-2);
		background: var(--mai-color-bg);
		border-top: var(--mai-border-width) solid var(--mai-color-border);
		font-family: var(--mai-font-sans);
	}

	.mai-window-switcher__entry {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2);
		max-width: 220px;
		min-height: var(--mai-target-min);
		padding: 0 var(--mai-space-3);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-medium);
		color: var(--mai-color-text);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		cursor: pointer;
	}

	/* Focused window: primary border + text voice, matching the frame's
	   active styling so the state difference is visible, not color-only. */
	.mai-window-switcher__entry--focused {
		border-color: var(--mai-color-primary);
	}

	/* Minimized: muted voice (aria-pressed carries the state for a11y). */
	.mai-window-switcher__entry--minimized {
		color: var(--mai-color-text-muted);
		background: var(--mai-status-neutral-bg);
	}

	.mai-window-switcher__entry-title {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.mai-window-switcher__entry-sep {
		color: var(--mai-color-border-strong);
	}

	.mai-window-switcher__entry-context {
		flex: none;
		font-size: var(--mai-text-xs);
		color: var(--mai-status-neutral-text);
		white-space: nowrap;
	}
</style>
