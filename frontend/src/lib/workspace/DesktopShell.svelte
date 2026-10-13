<script module lang="ts">
	/**
	 * DesktopShell — composed desktop workspace (card `01-desktop-shell`, P01).
	 *
	 * Module scope: the interim benign fixture panels and their frozen
	 * registry (windowed-workspace ADR §2 layer table: panel registry/host +
	 * window chrome + workspace state, composed here, owned by nobody else).
	 *
	 * Fixture policy (brief acceptance 2): until auth/domain features exist,
	 * the only openable panels are LABELED benign placeholders — static
	 * self-describing views that fetch nothing, subscribe to nothing and
	 * display no domain or monitoring data. They are built with the public
	 * `createRawSnippet` API because the registry's `PanelComponent` slot
	 * needs a component function and this card may not add `.svelte` fixture
	 * files outside its allowlist; feature cards replace them with compiled
	 * panels + registry entries (ADR §7.1). They never run during SSR:
	 * PanelHost resolves components through a lazy loader inside a
	 * client-only effect, so the server render stays the empty D4 shell.
	 */
	import { createRawSnippet } from 'svelte';
	import {
		createPanelRegistry,
		singletonInstanceKey,
		type PanelComponent,
		type PanelContext,
		type PanelRegistry,
		type PanelRegistryEntry
	} from './registry.js';
	import {
		type PanelKind,
		type ServerId,
		type WindowDescriptor,
		type WindowGeometry
	} from './types.js';

	/**
	 * Fixture server identities (opaque UUIDs per contracts.md C01 rule 1).
	 * Display labels live beside them and are attached to windows only at
	 * open time; nothing here is a mutable "current server" — the launcher
	 * selection below is a visible control consumed exactly once, when a
	 * new open request pins its scope (ADR §3.2, NT5).
	 */
	const FIXTURE_SERVERS: readonly { id: ServerId; label: string }[] = Object.freeze([
		{ id: '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f', label: 'Server Alpha' },
		{ id: 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60', label: 'Server Beta' }
	]);

	/** Human label for a fixture server id; unknown ids read as "Server". */
	function serverLabel(id: ServerId): string {
		return FIXTURE_SERVERS.find((server) => server.id === id)?.label ?? 'Server';
	}

	/** The pinned target of a live descriptor, as fixture display text. */
	function describeScope(descriptor: WindowDescriptor): string {
		const scope = descriptor.scope;
		if (scope.kind === 'global') return 'global';
		const object = scope.object ? ` · ${scope.object.kind} ${scope.object.id}` : '';
		return `server ${scope.server_id}${object}`;
	}

	/**
	 * One benign fixture panel. Renders its label, the pinned target of ITS
	 * descriptor (real metadata, not live data) and a local interactions
	 * button whose counter is closure state: it survives focus/drag of any
	 * window (content stays mounted) and resets when the content remounts
	 * (close/reopen, hide/restore) — that is the mount lifecycle made
	 * observable without any global test hook.
	 *
	 * Built with the public `createRawSnippet` API (its `Snippet` shape is
	 * assignable to the registry's `PanelComponent` slot). Every value
	 * interpolated into the HTML is a registry constant or a UUID-validated
	 * descriptor field — no free text ever enters this path (ADR §8 rule 1).
	 */
	function fixturePanel(kind: PanelKind, heading: string): PanelComponent {
		// `createRawSnippet` returns a function called `(anchor, props)` at
		// runtime — the same shape as the compiled `<Panel panel={…} />` call —
		// but its `Snippet` type hides the anchor parameter, so the slot
		// assignment needs one explicit cast here. Through the component slot
		// the argument is the plain props object; through an `{@render}` it is
		// a getter, so both forms are resolved.
		return createRawSnippet<[{ panel: PanelContext }]>((getProps) => {
			const props = typeof getProps === 'function' ? getProps() : getProps;
			const { descriptor } = props.panel;
			const banner = `Fixture panel "${heading}" — benign placeholder, no live data.`;
			const target = `Pinned target: ${describeScope(descriptor)}`;
			return {
				render: () =>
					`<div class="mai-fixture-panel" data-testid="fixture-panel" data-fixture-panel="${kind}">` +
					`<p class="mai-fixture-panel__banner">${banner}</p>` +
					`<p class="mai-fixture-panel__target">${target}</p>` +
					`<button type="button" class="mai-fixture-panel__interactions" data-testid="fixture-interactions">Fixture interactions: 0</button>` +
					`</div>`,
				setup: (root) => {
					const button = root.querySelector('button');
					let count = 0;
					const onClick = () => {
						count += 1;
						button!.textContent = `Fixture interactions: ${count}`;
					};
					button?.addEventListener('click', onClick);
					return () => button?.removeEventListener('click', onClick);
				}
			};
		}) as unknown as PanelComponent;
	}

	/**
	 * The interim fixture registry: validated, frozen, one instance per
	 * shell (no module-level singleton — ADR §5 rule 2). `fixture-prefs`
	 * declares an entry default geometry so both the entry-default and the
	 * shell-cascade placement paths are exercised.
	 */
	function createDesktopRegistry(): PanelRegistry {
		const result = createPanelRegistry([
			{
				panel_kind: 'fixture-notes',
				title: 'Notes',
				scope_types: ['server'],
				component: () => Promise.resolve(fixturePanel('fixture-notes', 'Notes'))
			},
			{
				panel_kind: 'fixture-prefs',
				title: 'Preferences',
				scope_types: ['global'],
				singleton: true,
				default_geometry: { x: 560, y: 88, width: 360, height: 260 },
				component: () => Promise.resolve(fixturePanel('fixture-prefs', 'Preferences'))
			}
		]);
		if (!result.ok) {
			// Constant input; failure here is a programming defect, not a
			// runtime condition — fail loudly instead of rendering a shell
			// that cannot open anything.
			throw new Error(`desktop fixture registry rejected: ${result.issue.code}`);
		}
		return result.value;
	}

	/** Shell default for entries without `default_geometry` (ADR §7.1). */
	const SHELL_DEFAULT_GEOMETRY: WindowGeometry = { x: 24, y: 24, width: 460, height: 320 };
</script>

<script lang="ts">
	/**
	 * Instance scope: the scoped controller of one desktop (brief
	 * acceptance 3). One `createWorkspaceState` per shell instance — no
	 * module-level store, nothing shared between sessions or SSR requests
	 * (ADR §5). The shell wires the produced layers together and owns no
	 * geometry/drag/presentation logic of its own:
	 *
	 *  - open: registry scope check → pinned explicit scope → workspace.open
	 *    (the frozen open path proven in panel-host.spec's composed test);
	 *  - focus: frame activation intent → workspace.focus (PT2);
	 *  - move: the title-bar drag adapter's clamped geometry intents →
	 *    workspace.move (PT1) — the shell adds no pointer code;
	 *  - close: frame close intent → workspace.close (PT5); view-only, the
	 *    state layer runs release hooks and nothing here can touch any
	 *    backend (NT6; contracts.md C08 rule 5, C11).
	 *
	 * Not wired on purpose: minimize/maximize. Their transitions belong to
	 * `01-window-presentation` (the state layer exposes no presentation
	 * transition yet); the frame renders those controls but the shell gives
	 * them no handler — recorded as this slice's known limitation, not a
	 * silent dead feature.
	 */
	import { createWorkspaceState, type WorkspaceSnapshot } from './state.js';
	import { clampDraggedGeometry } from './drag.js';
	import type { WindowId, WindowScope } from './types.js';
	import WindowFrame from './WindowFrame.svelte';
	import PanelHost from './PanelHost.svelte';
	import Button from '$lib/ui/Button.svelte';

	const registry = createDesktopRegistry();

	/** One workspace per shell instance (ADR §5 rule 2). */
	const workspace = createWorkspaceState({
		knownPanelKinds: registry.knownPanelKinds(),
		instanceKey: (kind, scope) => {
			const entry = registry.entry(kind);
			return entry ? singletonInstanceKey(kind, entry, scope) : null;
		}
	});

	let snapshot = $state<WorkspaceSnapshot>(workspace.snapshot());
	$effect(() => workspace.subscribe((next) => (snapshot = next)));

	/**
	 * Stable render order — the open sequence, NOT the stacking order.
	 * Rendering in stacking order would reparent a window's subtree on every
	 * raise, and the raise often originates FROM the focused title bar: the
	 * move removes and reinserts the focused frame, dropping focus to the
	 * document (breaking PT2's keyboard activation) and reparenting subtrees
	 * mid-gesture (the documented window-drag convention). Visual stacking
	 * stays with `descriptor.z`; DOM order stays stable.
	 */
	const openOrder = new Map<WindowId, number>();
	let openSeq = 0;

	const renderOrder = $derived.by(() =>
		[...snapshot.windows].sort(
			(a, b) => (openOrder.get(a.window_id) ?? 0) - (openOrder.get(b.window_id) ?? 0)
		)
	);

	/**
	 * The launcher's visible server selection — read once per open request
	 * to PIN the new window's scope (NT5); it can never reach an existing
	 * window, because descriptors are immutable after open.
	 */
	let selectedServerId = $state<ServerId>(FIXTURE_SERVERS[0].id);

	/** Launcher buttons: enabled registry entries, in registration order. */
	const launcherEntries = registry.entries().filter((entry) => entry.enabled);

	/** Last launcher outcome (open result or failure notice) — never data. */
	let status = $state<string | null>(null);

	/** Positioned desktop region the frames are placed within. */
	let desktopEl: HTMLDivElement | null = null;

	/** Cascade counter for entries without a registry default geometry. */
	let cascade = 0;

	/**
	 * Open geometry: the entry default when declared, else the shell
	 * default with a small cascade. The position is clamped with the
	 * produced NT3 clamp so a new window always keeps a visible title-bar
	 * area inside the viewport (measured against the desktop region's
	 * origin, exactly like a drag gesture). No clamping logic is written
	 * here — only the produced function is reused.
	 */
	function placeGeometry(entry: PanelRegistryEntry): WindowGeometry {
		const base = entry.default_geometry ?? {
			...SHELL_DEFAULT_GEOMETRY,
			x: SHELL_DEFAULT_GEOMETRY.x + (cascade % 6) * 32,
			y: SHELL_DEFAULT_GEOMETRY.y + (cascade % 6) * 24
		};
		cascade += 1;
		const rect = desktopEl?.getBoundingClientRect();
		if (!rect) return base;
		return clampDraggedGeometry(
			base,
			{ width: window.innerWidth, height: window.innerHeight },
			{ left: rect.left, top: rect.top, barWidth: base.width }
		);
	}

	/**
	 * Launcher open: registry scope check first (the composed open path),
	 * then the validated state transition. The scope is built from the
	 * entry's declared scope types and the CURRENT selection — one explicit
	 * target per open, never a shared mutable target.
	 */
	function openPanel(kind: PanelKind): void {
		const entry = registry.entry(kind);
		if (!entry) return;
		const scope: WindowScope = entry.scope_types.has('server')
			? { kind: 'server', server_id: selectedServerId }
			: { kind: 'global' };
		const scopeCheck = registry.checkScope(kind, scope);
		if (!scopeCheck.ok) {
			status = `Cannot open ${entry.title}: ${scopeCheck.issue.message}`;
			return;
		}
		const outcome = workspace.open({ panel_kind: kind, scope, geometry: placeGeometry(entry) });
		if (!outcome.ok) {
			status = `Cannot open ${entry.title}: ${outcome.issue.message}`;
			return;
		}
		if (outcome.created) {
			openOrder.set(outcome.descriptor.window_id, openSeq);
			openSeq += 1;
		}
		const target = scope.kind === 'server' ? serverLabel(scope.server_id) : 'Global';
		status = `Opened ${entry.title} — ${target}.`;
	}

	function closeWindow(windowId: WindowId): void {
		workspace.close(windowId);
		openOrder.delete(windowId);
	}

	function contextLabelFor(descriptor: WindowDescriptor): string {
		return descriptor.scope.kind === 'server' ? serverLabel(descriptor.scope.server_id) : '';
	}
</script>

<div class="mai-desktop-shell">
	<div class="mai-desktop-shell__launcher">
		<h2 class="mai-desktop-shell__launcher-title">Open panels</h2>
		<label class="mai-desktop-shell__server-label" for="mai-desktop-server-select">
			Target server for new windows
		</label>
		<select
			id="mai-desktop-server-select"
			class="mai-desktop-shell__server-select"
			bind:value={selectedServerId}
			data-testid="server-select"
		>
			{#each FIXTURE_SERVERS as server (server.id)}
				<option value={server.id}>{server.label}</option>
			{/each}
		</select>
		{#each launcherEntries as entry (entry.panel_kind)}
			<Button
				data-testid={'open-' + entry.panel_kind}
				onclick={() => openPanel(entry.panel_kind)}
			>
				Open {entry.title} — {entry.scope_types.has('server') ? serverLabel(selectedServerId) : 'Global'}
			</Button>
		{/each}
	</div>

	<p class="mai-desktop-shell__status" role="status" data-testid="desktop-status">{status}</p>

	<div class="mai-desktop-shell__desktop" bind:this={desktopEl} data-testid="desktop">
		<!-- Frame intents map 1:1 onto the state transitions (focus/close/move).
		     The drag geometry intent ignores an unknown id deliberately: a window
		     closed mid-gesture is gone, the state layer holds the committed base.
		     PanelHost failures are observability only; raw errors never reach the
		     DOM (ADR §7.2). -->
		{#each renderOrder as win (win.window_id)}
			<WindowFrame
				descriptor={win}
				title={registry.entry(win.panel_kind)?.title ?? win.panel_kind}
				contextLabel={contextLabelFor(win)}
				focused={snapshot.focused_id === win.window_id}
				onactivate={(windowId) => workspace.focus(windowId)}
				onclose={closeWindow}
				ongeometry={(windowId, geometry) => {
					workspace.move(windowId, geometry);
				}}
			>
				<PanelHost
					registry={registry}
					descriptor={win}
					onpanelerror={(windowId, panelKind) =>
						console.error(`[mai-desktop] panel failed: kind=${panelKind} window=${windowId}`)}
				/>
			</WindowFrame>
		{/each}
	</div>
</div>

<style>
	.mai-desktop-shell {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
	}

	.mai-desktop-shell__launcher {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--mai-space-2);
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
	}

	.mai-desktop-shell__launcher-title {
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-semibold);
		color: var(--mai-color-text);
	}

	.mai-desktop-shell__server-label {
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text-muted);
	}

	.mai-desktop-shell__server-select {
		padding: var(--mai-space-1) var(--mai-space-2);
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text);
		background: var(--mai-color-bg);
		border: var(--mai-border-width) solid var(--mai-color-border-strong);
		border-radius: var(--mai-radius-sm);
	}

	.mai-desktop-shell__status {
		min-height: 1.5em;
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text-muted);
	}

	/* The positioned ancestor of the window frames: geometry coordinates are
	   desktop-relative; the drag adapter and the open clamp both measure this
	   region's viewport origin (NT3). No overflow rule: a clipped window could
	   lose its recoverable title bar (ADR §10.2 NT3). */
	.mai-desktop-shell__desktop {
		position: relative;
		flex: 1 1 auto;
		min-height: 60vh;
		border: var(--mai-border-width) dashed var(--mai-color-border-strong);
		border-radius: var(--mai-radius-md);
	}

	/* Fixture panels are raw-DOM clients, so their styles are global but
	   namespaced and consumed only inside PanelHost bodies. */
	:global(.mai-fixture-panel) {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		font-family: var(--mai-font-sans);
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text);
	}

	:global(.mai-fixture-panel__banner) {
		font-weight: var(--mai-weight-medium);
	}

	:global(.mai-fixture-panel__target) {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
		overflow-wrap: anywhere;
	}

	:global(.mai-fixture-panel__interactions) {
		align-self: flex-start;
		padding: var(--mai-space-1) var(--mai-space-3);
		font-size: var(--mai-text-sm);
		color: var(--mai-color-primary-contrast);
		cursor: pointer;
		background: var(--mai-color-primary);
		border: var(--mai-border-width) solid var(--mai-color-primary);
		border-radius: var(--mai-radius-sm);
	}

	:global(.mai-fixture-panel__interactions:hover) {
		background: var(--mai-color-primary-hover);
	}

	:global(.mai-fixture-panel__interactions:focus-visible) {
		outline: var(--mai-focus-ring-width) solid var(--mai-focus-ring-color);
		outline-offset: var(--mai-focus-ring-offset);
	}
</style>
