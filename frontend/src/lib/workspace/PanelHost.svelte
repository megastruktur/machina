<script lang="ts">
	/**
	 * PanelHost — reusable panel mount point (card `01-panel-registry`, P01).
	 *
	 * Layer: panel registry/host (windowed-workspace ADR-0001 §2 layer table,
	 * §7 "Panel registration and lifecycle", D6; contracts.md C11). Renders the
	 * panel body of ONE live window from its validated descriptor and a frozen
	 * `PanelRegistry`. It owns nothing else:
	 *
	 *  - No window state, no drag/resize/geometry logic, no focus/stacking, no
	 *    persistence: everything enters through props (`descriptor` is already
	 *    validated and pinned by the workspace state layer) and leaves through
	 *    the `onpanelerror` observability callback. Mounting, unmounting,
	 *    hiding or erroring never starts, cancels, resubmits or retargets
	 *    backend work (ADR §7.2, NT6; contracts.md C08 rule 5).
	 *  - No dynamic import from data: the rendered component is resolved ONLY
	 *    through the registry's validated entry (a feature-owned loader
	 *    function). `panel_kind` strings from descriptors/layout/URL can never
	 *    select or construct an import target (ADR §7.1, §10.2 NT4).
	 *  - Registration is not authorization: no code path reads
	 *    `capabilities_note` or derives any permission; unknown AND disabled
	 *    kinds render the same safe "unavailable panel" view (ADR §7.1).
	 *  - Error containment: content renders inside a panel-scoped
	 *    `<svelte:boundary>`; a broken panel shows a generic safe error view
	 *    with Retry (the frame's close control stays with WindowFrame) and
	 *    cannot affect neighboring hosts (ADR §7.2 "Error containment"). Raw
	 *    error text is never echoed into the DOM; details go to
	 *    `onpanelerror` only.
	 *  - Lifecycle isolation: hidden windows (minimized without
	 *    `keep_mounted_when_hidden`) unmount their content and run its
	 *    cleanup hooks exactly once; remounting restores a fresh context
	 *    (ADR §7.2 `hidden`). Panel teardown is the mount-scoped half of the
	 *    release contract (`PanelContext.onCleanup`); window-close-time hooks
	 *    stay with the workspace state layer (`open().release`).
	 *  - No stores, no module-level state, no `window`/`document` access:
	 *    every writable lives inside one component instance, so instances
	 *    share nothing (NT5), no global "current server" target exists, and
	 *    the module is safe to evaluate per SSR request (ADR §5).
	 *
	 * Tokens: colors, spacing and radius come only from ui/tokens.css; no
	 * theme value is redeclared and no dependency is added (ADR §4.3).
	 */
	import { onDestroy } from 'svelte';
	import {
		type PanelComponent,
		type PanelContext,
		type PanelRegistry,
		type PanelServices,
		type RegistryIssueCode
	} from './registry.js';
	import type { WindowDescriptor, WindowId } from './types.js';

	interface Props {
		/** Frozen registry this host resolves components through. */
		registry: PanelRegistry;
		/** Validated live descriptor from the workspace state (schema v1). */
		descriptor: WindowDescriptor;
		/**
		 * Explicit services bag passed through to the panel context. The host
		 * never constructs, filters or falls back to a global — an opener that
		 * passes nothing gets an empty bag, never a locator.
		 */
		services?: PanelServices;
		/** Observability intent for load/render failures (controller-owned). */
		onpanelerror?: (windowId: WindowId, panelKind: string, error: unknown) => void;
		class?: string;
	}

	let { registry, descriptor, services = {}, onpanelerror, class: className = '' }: Props =
		$props();

	// Registry resolution is a pure lookup; unknown kinds render the safe
	// unavailable view below, never an import attempt (ADR §10.2 NT4).
	const entry = $derived(registry.entry(descriptor.panel_kind));

	/** Why this host cannot mount its panel, if it cannot (ADR §7.1/§7.2). */
	const unavailable = $derived.by<{ code: RegistryIssueCode; message: string } | null>(() => {
		if (entry === null) {
			return { code: 'unknown_panel_kind', message: 'This panel is not available.' };
		}
		if (!entry.enabled) {
			return { code: 'panel_disabled', message: 'This panel is not available.' };
		}
		const scopeCheck = registry.checkScope(descriptor.panel_kind, descriptor.scope);
		if (!scopeCheck.ok) {
			return { code: scopeCheck.issue.code, message: 'This panel is not available.' };
		}
		return null;
	});

	/**
	 * ADR §7.2 `hidden`: minimized content unmounts (subscriptions released)
	 * unless the entry declares `keep_mounted_when_hidden`. Unknown kinds
	 * always show the error view — hiding must not swallow the notice.
	 */
	const hidden = $derived(
		unavailable === null &&
			descriptor.presentation === 'minimized' &&
			!entry?.keep_mounted_when_hidden
	);

	// --- Mount-scoped release contract (PanelContext.onCleanup) ---------------

	/** Bumped every time mounted content is torn down; stale ctx turns inert. */
	let generation = $state(0);
	let cleanupHooks: Array<() => void> = [];

	/**
	 * Runs every registered hook exactly once (LIFO), contained: a throwing
	 * teardown can neither skip the remaining hooks nor leak into the next
	 * mount cycle (fresh empty array + generation bump).
	 */
	function runCleanups(): void {
		generation += 1;
		const hooks = cleanupHooks;
		cleanupHooks = [];
		for (let index = hooks.length - 1; index >= 0; index -= 1) {
			try {
				hooks[index]();
			} catch {
				// Contained (ADR §7.2 error containment): teardown failures must
				// not break the host, the frame or sibling windows.
			}
		}
	}

	// Hidden transition releases the content's subscriptions; host removal
	// (close/unmount) releases them too. Both paths clear the array first,
	// so each hook runs exactly once across hide/close combinations.
	$effect(() => {
		if (hidden) runCleanups();
	});
	onDestroy(runCleanups);

	/**
	 * Explicit context handed to the mounted panel. Re-created per teardown
	 * generation so post-unmount registrations (stale async code) are no-ops
	 * and a remount always receives a fresh, live context.
	 */
	const panelContext = $derived.by<PanelContext>(() => {
		const activeGeneration = generation;
		return {
			descriptor,
			scope: descriptor.scope,
			services,
			onCleanup(hook: () => void) {
				if (activeGeneration !== generation) return () => {};
				cleanupHooks.push(hook);
				let active = true;
				return () => {
					if (!active) return;
					active = false;
					const index = cleanupHooks.indexOf(hook);
					if (index !== -1) cleanupHooks.splice(index, 1);
				};
			}
		};
	});

	// --- Lazy component resolution (opening → mounted, ADR §7.2) --------------

	type LoadState =
		| { status: 'opening' }
		| { status: 'ready'; component: PanelComponent }
		| { status: 'failed' };

	let load = $state<LoadState>({ status: 'opening' });
	/** Retry token for load failures; bumping re-runs the resolution effect. */
	let loadAttempt = $state(0);

	/**
	 * Stable resolution key: `null` when nothing may mount, otherwise the
	 * panel kind. Deliberately NOT the descriptor identity — geometry moves
	 * and presentation changes replace the descriptor object, and the resolved
	 * component must not remount (nor re-subscribe) for those; only kind/
	 * availability/hidden-policy changes restart resolution.
	 */
	const resolveKey = $derived.by<string | null>(() => {
		if (hidden || unavailable !== null) return null;
		return descriptor.panel_kind;
	});

	/**
	 * Plain (non-reactive) bookkeeping of the key whose module already
	 * resolved. When the key is unchanged — most importantly when a hidden
	 * window becomes visible again — the effect must NOT re-run the loader
	 * and must NOT reset to `opening`: the template would mount the still-
	 * ready content and immediately tear it down again (a mount cycle with
	 * no release). Reads here are deliberately limited to plain state so the
	 * `load` writes below never re-trigger this effect.
	 */
	let loadedForKey: string | null = null;

	$effect(() => {
		const key = resolveKey;
		void loadAttempt;
		if (key === null || loadedForKey === key) return;
		const loader = entry?.component;
		if (!loader) return;
		let stale = false;
		load = { status: 'opening' };
		loader().then(
			(component) => {
				if (!stale) {
					loadedForKey = key;
					load = { status: 'ready', component };
				}
			},
			(error) => {
				if (!stale) {
					load = { status: 'failed' };
					onpanelerror?.(descriptor.window_id, descriptor.panel_kind, error);
				}
			}
		);
		return () => {
			stale = true;
		};
	});

	function retryLoad(): void {
		loadAttempt += 1;
	}

	/**
	 * Render-failure containment (ADR §7.2): report the raw error to the
	 * controller only, release whatever the partially-mounted content had
	 * registered (the retry is a fresh mount cycle with a fresh context) and
	 * show the generic failed view via the `failed` snippet. The boundary
	 * itself rethrows nothing.
	 */
	function onRenderError(error: unknown): void {
		runCleanups();
		onpanelerror?.(descriptor.window_id, descriptor.panel_kind, error);
	}
</script>

<div
	class="mai-panel-host {className}"
	data-window-id={descriptor.window_id}
	data-panel-kind={descriptor.panel_kind}
	data-hidden={hidden ? 'true' : 'false'}
>
	{#if hidden}
		<!-- ADR §7.2 hidden: content unmounted, subscriptions released above. -->
	{:else if unavailable !== null}
		<div class="mai-panel-host__state" role="status" data-testid="panel-unavailable" data-reason={unavailable.code}>
			<p>This panel is not available.</p>
		</div>
	{:else if load.status === 'opening'}
		<div class="mai-panel-host__state" role="status" data-testid="panel-opening">
			<p>Loading panel…</p>
		</div>
	{:else if load.status === 'failed'}
		<div class="mai-panel-host__state mai-panel-host__state--error" role="alert" data-testid="panel-load-failed">
			<p>This panel could not be loaded.</p>
			<button type="button" data-testid="panel-load-retry" onclick={retryLoad}>Retry</button>
		</div>
	{:else}
		<svelte:boundary onerror={onRenderError}>
			{#if load.status === 'ready'}
				{@const Panel = load.component}
				<div class="mai-panel-host__body" data-testid="panel-body">
					<Panel panel={panelContext} />
				</div>
			{/if}
			{#snippet failed(_error, reset)}
				<div
					class="mai-panel-host__state mai-panel-host__state--error"
					role="alert"
					data-testid="panel-render-failed"
				>
					<p>This panel could not be rendered.</p>
					<button type="button" data-testid="panel-render-retry" onclick={reset}>Retry</button>
				</div>
			{/snippet}
		</svelte:boundary>
	{/if}
</div>

<style>
	.mai-panel-host {
		height: 100%;
		overflow: auto;
		color: var(--mai-color-text);
		background: var(--mai-color-bg);
	}

	.mai-panel-host__state {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		align-items: flex-start;
		padding: var(--mai-space-4);
		color: var(--mai-status-neutral-text);
	}

	.mai-panel-host__state--error {
		color: var(--mai-status-danger-text);
	}

	.mai-panel-host__state p {
		margin: 0;
	}

	.mai-panel-host__state button {
		padding: var(--mai-space-1) var(--mai-space-3);
		color: var(--mai-color-primary-contrast);
		cursor: pointer;
		background: var(--mai-color-primary);
		border: var(--mai-border-width) solid var(--mai-color-primary);
		border-radius: var(--mai-radius-sm);
	}

	.mai-panel-host__state button:hover {
		background: var(--mai-color-primary-hover);
	}

	.mai-panel-host__state button:focus-visible {
		outline: var(--mai-focus-ring-width) solid var(--mai-focus-ring-color);
		outline-offset: var(--mai-focus-ring-offset);
	}

	.mai-panel-host__body {
		height: 100%;
	}
</style>
