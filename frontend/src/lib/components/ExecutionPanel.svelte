<script module lang="ts">
	/**
	 * ExecutionPanel — the interactive SSH terminal window (uir task
	 * `u2-terminal`; replaces the old run-approval/password-challenge/receipt
	 * form for card `mai-05-web-execution`).
	 *
	 * Layer: feature panel (windowed-workspace ADR-0001 §2 layer table). It owns
	 * its one domain view and nothing else:
	 *
	 *  - Explicit context only: a SERVER terminal view pinned at open time
	 *    (`panel.scope.server_id`, C01 rule 2). No launcher/current-server read
	 *    anywhere, so a launcher re-selection can never retarget a live
	 *    terminal (C08 rule 5).
	 *  - Explicit services only: the `ExecutionApi` slice enters through
	 *    `panel.services.api` (the typed bag owned by the feature entry
	 *    module). A missing or incomplete bag fails closed with a visible
	 *    state — no service locator, no fallback client.
	 *  - The panel reads the terminal attach info (pinned SSH target) and
	 *    opens ONE authenticated WebSocket (injected `openTerminalSocket`, or
	 *    the browser constructor against the info's `ws_path` — the
	 *    `mai_session` cookie rides along same-origin). All privilege policy,
	 *    grants and host-key pinning stay server-side; the panel transports
	 *    terminal bytes only: JSON `{"type":"input"}` / `{"type":"resize"}`
	 *    frames out, raw text frames in.
	 *  - Honest wire states (C08 rule 6): connecting / connected /
	 *    disconnected are surfaced with the close code or failure detail;
	 *    a dropped socket never pretends to be connected and `Reconnect`
	 *    re-runs the whole attach (fresh info read + fresh socket) while
	 *    preserving the scrollback. `Clear` wipes the local view only — it
	 *    sends nothing to the target.
	 *  - No module-level state and no `window`/`document`/storage access at
	 *    module top level (ADR §5 rule 4): xterm, the ResizeObserver and the
	 *    socket exist only inside instance effects, are torn down by the same
	 *    effects, and nothing survives into another mount cycle.
	 */
</script>

<script lang="ts">
	import { Terminal } from '@xterm/xterm';
	import { FitAddon } from '@xterm/addon-fit';
	import '@xterm/xterm/css/xterm.css';
	import Button from '../ui/Button.svelte';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import type {
		ExecutionApi,
		TerminalSessionInfoView
	} from '../workspace/entries/05-web-execution.js';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readApi(services: PanelServices): ExecutionApi | null {
		const candidate = services.api as Partial<ExecutionApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		if (typeof candidate.terminalInfo !== 'function') return null;
		if (
			candidate.openTerminalSocket !== undefined &&
			typeof candidate.openTerminalSocket !== 'function'
		) {
			return null;
		}
		return candidate as ExecutionApi;
	}

	const api = $derived(readApi(panel.services));
	/** Terminal view: a global mount contradicts the entry contract. */
	const serverScope = $derived(panel.scope.kind === 'server' ? panel.scope.server_id : null);
	const usable = $derived(api !== null && serverScope !== null);

	// --- link state (C08 rule 6: never all-green while disconnected) ---------

	type LinkState =
		| { kind: 'idle' }
		| { kind: 'connecting' }
		| { kind: 'open' }
		| { kind: 'closed'; detail: string };

	let link = $state<LinkState>({ kind: 'idle' });
	let info = $state<TerminalSessionInfoView | null>(null);
	let infoError = $state('');

	const STATUS_TEXT: Record<LinkState['kind'], string> = Object.freeze({
		idle: 'Idle',
		connecting: 'Connecting…',
		open: 'Connected',
		closed: 'Disconnected'
	});

	const linkText = $derived(
		link.kind === 'closed' ? `${STATUS_TEXT.closed} · ${link.detail}` : STATUS_TEXT[link.kind]
	);
	const badgeTone = $derived(
		link.kind === 'open'
			? 'success'
			: link.kind === 'connecting'
				? 'warning'
				: link.kind === 'closed'
					? 'danger'
					: 'neutral'
	);
	const targetText = $derived(info === null ? null : `${info.user}@${info.host}:${info.port}`);

	// --- instance-scoped terminal/socket plumbing ----------------------------

	let viewportEl = $state<HTMLDivElement | null>(null);
	/** Bumped by Reconnect; the connection effect re-runs attach end to end. */
	let attempt = $state(0);

	let term: Terminal | null = null;
	let fitAddon: FitAddon | null = null;
	let socket: WebSocket | null = null;

	function errorMessageOf(error: unknown): string {
		if (typeof error === 'object' && error !== null && 'code' in error) {
			const code = String((error as { code?: unknown }).code);
			if (code === 'unauthenticated') return 'your session has ended — sign in again';
			if (code === 'unavailable') return 'the terminal service is unavailable';
			return `request rejected (${code})`;
		}
		return error instanceof Error && error.message ? error.message : 'attach failed';
	}

	function sendFrame(payload: Record<string, unknown>): void {
		if (socket !== null && socket.readyState === WebSocket.OPEN) {
			socket.send(JSON.stringify(payload));
		}
	}

	function sendInput(data: string): void {
		sendFrame({ type: 'input', data });
	}

	function fitAndResize(): void {
		if (term === null || fitAddon === null) return;
		try {
			fitAddon.fit();
		} catch {
			return; // zero-size layout: retry on the next observer tick
		}
		sendFrame({ type: 'resize', cols: term.cols, rows: term.rows });
	}

	/** Mount the xterm instance once per usable host; dispose on teardown. */
	$effect(() => {
		if (!usable || viewportEl === null) return;
		const mounted = new Terminal({
			fontFamily: "ui-monospace, 'JetBrains Mono', 'Cascadia Code', Menlo, Consolas, monospace",
			fontSize: 13,
			cursorBlink: true,
			allowTransparency: true,
			theme: { background: 'rgba(0, 0, 0, 0)' },
			scrollback: 5000
		});
		const fit = new FitAddon();
		mounted.loadAddon(fit);
		mounted.open(viewportEl);
		fit.fit();
		term = mounted;
		fitAddon = fit;
		const dataHook = mounted.onData((data) => sendInput(data));
		const observer = new ResizeObserver(() => fitAndResize());
		observer.observe(viewportEl);
		return () => {
			observer.disconnect();
			dataHook.dispose();
			mounted.dispose();
			term = null;
			fitAddon = null;
		};
	});

	/** Attach: fresh info read + fresh socket per attempt; cleanup closes. */
	$effect(() => {
		if (!usable) return;
		const client = api;
		const serverId = serverScope;
		if (client === null || serverId === null) return;
		void attempt; // reactive dependency: Reconnect re-runs this effect

		let cancelled = false;
		let local: WebSocket | null = null;
		infoError = '';
		info = null;
		link = { kind: 'connecting' };

		void (async () => {
			try {
				const view = await client.terminalInfo(serverId);
				if (cancelled) return;
				info = view;
				local = client.openTerminalSocket
					? client.openTerminalSocket(serverId)
					: new WebSocket(
							`${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}${view.ws_path}`
						);
				socket = local;
				local.onopen = () => {
					if (cancelled || local !== socket) return;
					link = { kind: 'open' };
					fitAndResize();
					requestAnimationFrame(() => fitAndResize());
				};
				local.onmessage = (event) => {
					const live = term;
					if (local !== socket || live === null) return;
					if (typeof event.data === 'string') live.write(event.data);
				};
				local.onclose = (event) => {
					if (cancelled || local !== socket) return;
					socket = null;
					const reason = event.reason ? event.reason : `code ${event.code}`;
					link = { kind: 'closed', detail: reason };
				};
				local.onerror = () => {
					if (cancelled || local !== socket) return;
					socket = null;
					link = { kind: 'closed', detail: 'connection error' };
				};
			} catch (error) {
				if (cancelled) return;
				infoError = errorMessageOf(error);
				link = { kind: 'closed', detail: 'unavailable' };
			}
		})();

		return () => {
			cancelled = true;
			const closing = local;
			if (closing !== null) {
				closing.onopen = null;
				closing.onmessage = null;
				closing.onclose = null;
				closing.onerror = null;
				try {
					closing.close(1000, 'panel detached');
				} catch {
					// already closed: nothing to release
				}
			}
			if (socket === closing) socket = null;
		};
	});

	function reconnect(): void {
		attempt += 1;
	}

	function clearView(): void {
		term?.clear();
		term?.focus();
	}
</script>

<div class="mai-term" data-testid="execution-root">
	{#if !usable}
		<section class="mai-term__closed" data-testid="terminal-unusable">
			<p class="mai-term__closed-title">Terminal unavailable</p>
			<p class="mai-term__closed-text">
				This view needs a server scope and the terminal service bag. Open the
				Terminal from a server in the navigation tree.
			</p>
		</section>
	{:else}
		<header class="mai-term__bar">
			<span class="mai-term__title">Terminal</span>
			<span class="mai-term__scope" data-testid="execution-scope" title={serverScope ?? ''}>
				{serverScope}
			</span>
			{#if targetText !== null}
				<code class="mai-term__target" data-testid="terminal-target">{targetText}</code>
			{:else}
				<code class="mai-term__target mai-term__target--pending" data-testid="terminal-target">
					resolving target…
				</code>
			{/if}
			<span
				class="mai-term__badge mai-term__badge--{badgeTone}"
				data-testid="terminal-status"
				data-state={link.kind}
			>
				{linkText}
			</span>
			<span class="mai-term__actions">
				<Button
					variant="secondary"
					size="sm"
					onclick={reconnect}
					disabled={link.kind === 'connecting'}
					data-testid="terminal-reconnect"
				>
					Reconnect
				</Button>
				<Button variant="secondary" size="sm" onclick={clearView} data-testid="terminal-clear">
					Clear
				</Button>
			</span>
		</header>
		{#if infoError}
			<p class="mai-term__error" data-testid="terminal-info-error" role="alert">
				Cannot open the terminal: {infoError}.
			</p>
		{/if}
		<div class="mai-term__viewport" data-testid="terminal-viewport" bind:this={viewportEl}></div>
	{/if}
</div>

<style>
	.mai-term {
		display: flex;
		flex-direction: column;
		height: 100%;
		/* Floor for content-sized hosts (test fixtures): the terminal must
		 * never size its own container, or fit() feeds back through the
		 * ResizeObserver and the rows grow unbounded. */
		min-height: 360px;
		min-width: 0;
		color: var(--mai-color-text);
		background: var(--mai-color-surface-sunken);
	}

	.mai-term__closed {
		display: grid;
		gap: var(--mai-space-2);
		padding: var(--mai-space-5);
	}

	.mai-term__closed-title {
		margin: 0;
		color: var(--mai-color-brass);
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-base);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
	}

	.mai-term__closed-text {
		margin: 0;
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-sm);
	}

	.mai-term__bar {
		display: flex;
		align-items: center;
		gap: var(--mai-space-3);
		flex: 0 0 auto;
		padding: var(--mai-space-2) var(--mai-space-3);
		border-bottom: var(--mai-border-width) solid var(--mai-color-border);
		background: var(--mai-color-surface-header);
	}

	.mai-term__title {
		color: var(--mai-color-brass);
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.mai-term__scope {
		max-width: 16ch;
		overflow: hidden;
		padding: 1px var(--mai-space-2);
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--mai-color-text-muted);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		border: var(--mai-border-width) solid var(--mai-color-border);
	}

	.mai-term__target {
		min-width: 0;
		overflow: hidden;
		color: var(--mai-color-text);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.mai-term__target--pending {
		color: var(--mai-color-text-muted);
	}

	.mai-term__badge {
		flex: 0 0 auto;
		padding: 1px var(--mai-space-2);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		letter-spacing: 0.04em;
		border: var(--mai-border-width) solid var(--mai-status-neutral-border);
		color: var(--mai-status-neutral-text);
	}

	.mai-term__badge--success {
		color: var(--mai-status-success-text);
		border-color: var(--mai-status-success-border);
	}

	.mai-term__badge--warning {
		color: var(--mai-status-warning-text);
		border-color: var(--mai-status-warning-border);
	}

	.mai-term__badge--danger {
		color: var(--mai-status-danger-text);
		border-color: var(--mai-status-danger-border);
	}

	.mai-term__actions {
		display: flex;
		gap: var(--mai-space-2);
		margin-left: auto;
	}

	.mai-term__error {
		margin: 0;
		flex: 0 0 auto;
		padding: var(--mai-space-2) var(--mai-space-3);
		color: var(--mai-status-danger-text);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
	}

	.mai-term__viewport {
		flex: 1 1 auto;
		position: relative;
		min-height: 0;
		padding: var(--mai-space-2);
		overflow: hidden;
	}

	/* The xterm element is taken out of flow: its rendered rows can never
	 * grow the viewport box, so the fit ⇄ ResizeObserver loop terminates
	 * after one stable measurement. */
	.mai-term__viewport :global(.xterm) {
		position: absolute;
		inset: var(--mai-space-2);
	}
</style>
