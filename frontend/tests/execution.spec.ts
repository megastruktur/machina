import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-05-web-execution — real-browser proofs for the interactive terminal
 * panel (uir task `u2-terminal`).
 *
 * Harness convention (observed from the reviewed suites): a virtual fixture
 * app is built IN MEMORY with the real production toolchain (vite + the real
 * Svelte compiler, the real registry, the real PanelHost and the real lazy
 * panel chunk), served over one routed URL — zero network. The terminal's
 * WebSocket is a fixture `FakeWebSocket` injected through the entry contract
 * (`openTerminalSocket`), so every wire frame the panel sends and receives is
 * recorded and asserted verbatim; xterm.js itself runs for real (real DOM
 * renderer, real keyboard events).
 */

const FIXTURE_URL = '/mai-execution-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-execution-app.svelte';
const ENTRY_ID = 'virtual:mai-execution-entry.js';

const PANEL = `${FRONTEND_ROOT}src/lib/components/ExecutionPanel.svelte`;
const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/05-web-execution.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
const WINDOW_A = '6f6f6a7b-8c9d-4e8a-8a9b-4c5d6e7f8a9c';
const WINDOW_B = '7a7a6a7b-8c9d-4e8a-8a9b-4c5d6e7f8a9d';
const WINDOW_GLOBAL = '3c3c3c3c-1111-4222-8333-444455556666';
const WINDOW_BARE = '4d4d4d4d-1111-4222-8333-444455556666';
const WINDOW_SIBLING = '5e5e5e5e-1111-4222-8333-444455556666';

type Fail = { status: number; code: string; message: string };

interface SocketSnapshot {
	url: string;
	readyState: number;
	sent: string[];
	clientClosed: { code?: number; reason?: string } | null;
}

interface TerminalWindowApi {
	calls: Array<{ op: string; arg?: unknown }>;
	failNextInfo(failure: Fail | null): void;
	toggleHost(host: 'A' | 'B'): void;
	registry: {
		kinds: string[];
		scopeChecks: { serverScope: string; globalScope: string; unknownKind: string };
		defaultGeometry: unknown;
		minGeometry: unknown;
		instanceKey: string | null;
	};
}

declare global {
	interface Window {
		__maiExecutionApi?: TerminalWindowApi;
		__maiSockets?: { snapshots(): unknown[] };
	}
}

type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; fileName: string; isEntry: boolean; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

let harnessJs = '';
let harnessCss = '';

/**
 * The fixture app. Two server-scoped terminal hosts (A and B), one bare host
 * (no services), one global-scope host (registry scope rejection) and one
 * benign sibling panel.
 */
const APP_SOURCE = `
<script>
	import PanelHost from '${PANELHOST}';
	import ComponentHost from '${HOST}';
	import { createPanelRegistry, definePanelEntry, singletonInstanceKey } from '${REGISTRY}';
	import { createRawSnippet } from 'svelte';
	import { ApiError } from '${CLIENT}';
	import { executionEntry, executionServices, EXECUTION_PANEL_KIND } from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const SERVER_B = '${SERVER_B}';

	// --- fixture transport: one recorded FakeWebSocket per attach -----------

	class FakeWebSocket {
		constructor(url) {
			this.url = url;
			this.readyState = 0; // CONNECTING
			this.sent = [];
			this.clientClosed = null;
			this.onopen = null;
			this.onmessage = null;
			this.onclose = null;
			this.onerror = null;
			sockets.push(this);
		}
		send(data) {
			if (this.readyState !== 1) throw new Error('send on a non-open socket');
			this.sent.push(data);
		}
		close(code, reason) {
			if (this.readyState >= 2) return;
			this.readyState = 3;
			this.clientClosed = { code: code ?? 1000, reason: reason ?? '' };
		}
		serverOpen() {
			this.readyState = 1;
			this.openedWithoutHandler = !this.onopen;
			this.onopen?.({});
		}
		serverMessage(text) {
			this.onmessage?.({ data: text });
		}
		serverClose(code, reason) {
			this.readyState = 3;
			this.onclose?.({ code, reason });
		}
		serverError() {
			this.onerror?.({});
		}
	}
	const sockets = [];

	// --- fixture backend: terminal attach info -------------------------------

	const terminalViews = {
		[SERVER_A]: {
			server_id: SERVER_A,
			host: '198.51.100.20',
			port: 2222,
			user: 'operator',
			ws_path: '/api/v1/servers/' + SERVER_A + '/terminal',
			tmux_session: 'mai'
		},
		[SERVER_B]: {
			server_id: SERVER_B,
			host: '198.51.100.21',
			port: 22,
			user: 'deploy',
			ws_path: '/api/v1/servers/' + SERVER_B + '/terminal',
			tmux_session: 'mai'
		}
	};
	const calls = [];
	let failNextInfo = null;

	const api = {
		async terminalInfo(serverId) {
			calls.push({ op: 'terminalInfo', arg: serverId });
			if (failNextInfo) {
				const failure = failNextInfo;
				failNextInfo = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			const view = terminalViews[serverId];
			if (!view) throw new ApiError(404, 'not_found', 'no profile stored for this id');
			return { ...view };
		},
		openTerminalSocket(serverId) {
			calls.push({ op: 'openTerminalSocket', arg: serverId });
			return new FakeWebSocket('wss://fixture.test/api/v1/servers/' + serverId + '/terminal');
		}
	};
	const services = executionServices(api);

	// --- registry: the REAL feature entry + one benign sibling ----------------
	const sibling = createRawSnippet(() => {
		return {
			render: () =>
				'<div class="mai-sibling" data-testid="sibling-panel">' +
				'<p>Sibling fixture panel — benign, no domain data.</p>' +
				'</div>',
			setup: () => {}
		};
	});
	const registryResult = createPanelRegistry([
		executionEntry(),
		{
			panel_kind: 'fixture-notes',
			title: 'Notes',
			scope_types: ['server'],
			component: () => Promise.resolve(sibling)
		}
	]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	const registry = registryResult.value;
	const entryCheck = definePanelEntry(executionEntry());
	if (!entryCheck.ok) throw new Error('feature entry rejected: ' + entryCheck.issue.code);

	// --- hosts ----------------------------------------------------------------
	let showA = $state(true);

	function descriptor(windowId, scope, z, kind) {
		return {
			schema_version: 1,
			window_id: windowId,
			panel_kind: kind ?? EXECUTION_PANEL_KIND,
			scope,
			geometry: { x: 8, y: 8, width: 720, height: 560 },
			presentation: 'normal',
			z,
			title_override: null
		};
	}

	function snapshotSocket(socket) {
		return {
			url: socket.url,
			readyState: socket.readyState,
			sent: [...socket.sent],
			clientClosed: socket.clientClosed,
			openedWithoutHandler: socket.openedWithoutHandler ?? null
		};
	}

	const windowApi = {
		calls,
		failNextInfo(failure) {
			failNextInfo = failure;
		},
		toggleHost(host) {
			if (host === 'A') showA = !showA;
		},
		serverOpen(index) {
			sockets[index]?.serverOpen();
		},
		serverOutput(index, text) {
			sockets[index]?.serverMessage(text);
		},
		serverClose(index, code, reason) {
			sockets[index]?.serverClose(code, reason);
		},
		serverError(index) {
			sockets[index]?.serverError();
		},
		registry: (() => {
			function checkCode(kind, scope) {
				const result = registry.checkScope(kind, scope);
				return result.ok ? 'ok' : result.issue.code;
			}
			return {
				kinds: Array.from(registry.knownPanelKinds()).sort(),
				scopeChecks: {
					serverScope: checkCode(EXECUTION_PANEL_KIND, { kind: 'server', server_id: SERVER_A }),
					globalScope: checkCode(EXECUTION_PANEL_KIND, { kind: 'global' }),
					unknownKind: checkCode('not-a-panel', { kind: 'server', server_id: SERVER_A })
				},
				defaultGeometry: registry.defaultGeometry(EXECUTION_PANEL_KIND),
				minGeometry: registry.minGeometry(EXECUTION_PANEL_KIND),
				instanceKey: singletonInstanceKey(EXECUTION_PANEL_KIND, entryCheck.value, {
					kind: 'server',
					server_id: SERVER_A
				})
			};
		})()
	};
	window.__maiExecutionApi = windowApi;
	window.__maiSockets = {
		snapshots() {
			return sockets.map(snapshotSocket);
		}
	};
</script>

<section data-testid="host-a" aria-label="Host A">
	<ComponentHost label="Host A">
		{#if showA}
			<PanelHost
				onpanelerror={(windowId, panelKind, error) => console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error)}
				{registry}
				descriptor={descriptor('${WINDOW_A}', { kind: 'server', server_id: SERVER_A }, 1)}
				{services}
			/>
		{/if}
	</ComponentHost>
</section>
<section data-testid="host-b" aria-label="Host B">
	<ComponentHost label="Host B">
		<PanelHost
			onpanelerror={(windowId, panelKind, error) => console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error)}
			{registry}
			descriptor={descriptor('${WINDOW_B}', { kind: 'server', server_id: SERVER_B }, 2)}
			{services}
		/>
	</ComponentHost>
</section>
<section data-testid="host-sibling" aria-label="Host sibling">
	<ComponentHost label="Host sibling">
		<PanelHost
			onpanelerror={(windowId, panelKind, error) => console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error)}
			{registry}
			descriptor={descriptor('${WINDOW_SIBLING}', { kind: 'server', server_id: SERVER_B }, 3, 'fixture-notes')}
		/>
	</ComponentHost>
</section>
<section data-testid="host-global" aria-label="Host global">
	<ComponentHost label="Host global">
		<PanelHost
			onpanelerror={(windowId, panelKind, error) => console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error)}
			{registry}
			descriptor={descriptor('${WINDOW_GLOBAL}', { kind: 'global' }, 4)}
			{services}
		/>
	</ComponentHost>
</section>
<section data-testid="host-bare" aria-label="Host bare">
	<ComponentHost label="Host bare">
		<PanelHost
			onpanelerror={(windowId, panelKind, error) => console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error)}
			{registry}
			descriptor={descriptor('${WINDOW_BARE}', { kind: 'server', server_id: SERVER_A }, 5)}
		/>
	</ComponentHost>
</section>
`;

const ENTRY_SOURCE = `
import { mount } from 'svelte';
import App from '${APP_ID}';
import '${TOKENS}';
mount(App, { target: document.getElementById('app') });
`;

/** Serves the virtual modules and the built lazy chunks; nothing hits disk. */
function harnessPlugin(sources: Record<string, string>): Plugin {
	return {
		name: 'mai-execution-harness',
		resolveId(id) {
			// .svelte virtual modules must NOT get the conventional `\0` prefix:
			// vite-plugin-svelte excludes `\0` ids from Svelte compilation
			// (panel-host harness convention). The JS entry keeps the `\0`.
			if (id === APP_ID) return id;
			if (id === ENTRY_ID) return `\\0${id}`;
			return undefined;
		},
		load(id) {
			if (id === APP_ID) return sources[APP_ID];
			if (id === `\\0${ENTRY_ID}`) return sources[ENTRY_ID];
			return undefined;
		}
	};
}

/** Built lazy chunks (the entry's real dynamic panel import), served by name. */
let chunks: Array<{ fileName: string; code: string }> = [];
/** Chunk names actually fetched by the browser (lazy-loader proof). */
let servedChunks: string[] = [];

test.beforeAll(async () => {
	const sources: Record<string, string> = {
		[APP_ID]: APP_SOURCE,
		[ENTRY_ID]: ENTRY_SOURCE
	};
	const result = await build({
		configFile: false,
		root: FRONTEND_ROOT,
		logLevel: 'error',
		plugins: [svelte(), harnessPlugin(sources)],
		build: {
			write: false,
			emptyOutDir: true,
			cssCodeSplit: false,
			minify: false,
			outDir: 'node_modules/.mai-execution-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'execution-[hash].js',
					assetFileNames: 'demo[extname]'
				}
			}
		}
	});

	// `build()` is one-shot (no watcher configured); structural boundary type.
	const built = result as BuiltBundle | BuiltBundle[];
	const outputs = Array.isArray(built) ? built : [built];
	chunks = [];
	harnessJs = '';
	harnessCss = '';
	for (const { output } of outputs) {
		for (const item of output) {
			if (item.type === 'chunk') {
				if (item.isEntry) harnessJs += item.code;
				else chunks.push({ fileName: item.fileName, code: item.code });
			} else if (item.fileName.endsWith('.css')) {
				harnessCss += String(item.source);
			}
		}
	}
	if (!harnessJs || !harnessCss) {
		throw new Error('harness bundle incomplete: missing JS entry chunk or CSS asset');
	}
	if (chunks.length === 0) {
		throw new Error('harness bundle incomplete: the panel must build as a lazy chunk');
	}
});

test.beforeEach(async ({ page }) => {
	servedChunks = [];
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI execution fixture</title>
<link rel="stylesheet" href="${FIXTURE_URL}demo.css">
</head>
<body>
<div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"></script>
</body>
</html>`;

	await page.route('**/*', (route) => {
		const { pathname } = new URL(route.request().url());
		if (!pathname.startsWith(FIXTURE_URL)) return route.abort();
		const rest = pathname.slice(FIXTURE_URL.length);
		if (rest === '' || rest.endsWith('/')) {
			return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
		}
		if (rest === 'demo.js') {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: harnessJs });
		}
		if (rest === 'demo.css') {
			return route.fulfill({ contentType: 'text/css; charset=utf-8', body: harnessCss });
		}
		const chunk = chunks.find((candidate) => candidate.fileName === rest);
		if (chunk) {
			servedChunks.push(rest);
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: chunk.code });
		}
		return route.abort();
	});
	// Surface runtime failures in the receipt: an uncaught page error is a
	// harness/panel defect, never a silent pass.
	page.on('pageerror', (error) => {
		console.log(`[pageerror] ${error.stack ?? error.message}`);
	});
	page.on('console', (message) => {
		if (message.type() === 'error') console.log(`[console.error] ${message.text()}`);
	});
	await page.goto(FIXTURE_URL);
});

/**
 * Fixture-API boundary. Playwright serializes the evaluated function source
 * (closures over test scope do not transfer — observed: "fn is not defined"),
 * so every helper below is self-contained and passes only serializable
 * primitive/array arguments.
 */

/** Call one fixture-api method inside the page; args must be serializable. */
async function apiCall(page: Page, method: string, args: unknown[] = []): Promise<void> {
	const pair: [string, unknown[]] = [method, args];
	await page.evaluate(([name, callArgs]) => {
		const value = window.__maiExecutionApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		const table = value as unknown as Record<string, (...a: never[]) => void>;
		const fn = table[name];
		if (typeof fn !== 'function') throw new Error(`unknown fixture method ${name}`);
		fn.apply(value, callArgs as never[]);
	}, pair);
}

interface RecordedCall {
	op: string;
	arg?: unknown;
}

interface RegistrySummary {
	kinds: string[];
	scopeChecks: { serverScope: string; globalScope: string; unknownKind: string };
	defaultGeometry: unknown;
	minGeometry: unknown;
	instanceKey: string | null;
}

/** Snapshot the fixture's recorded calls, registry summary and sockets. */
async function apiState(
	page: Page
): Promise<{ calls: RecordedCall[]; registry: RegistrySummary; sockets: SocketSnapshot[] }> {
	return page.evaluate(() => {
		const value = window.__maiExecutionApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		const socketSource = window.__maiSockets;
		if (!socketSource) throw new Error('fixture sockets missing — app did not mount');
		return {
			calls: value.calls,
			registry: value.registry,
			sockets: socketSource.snapshots() as SocketSnapshot[]
		};
	});
}

/** One input-frame projection: every typed character the panel sent. */
async function typedInput(page: Page, urlPart: string): Promise<string> {
	return page.evaluate((part) => {
		const source = window.__maiSockets;
		if (!source) throw new Error('fixture sockets missing');
		const socket = (source.snapshots() as SocketSnapshot[]).find((row) =>
			row.url.includes(part)
		);
		if (!socket) throw new Error(`no socket for ${part}`);
		return socket.sent
			.map((frame) => {
				try {
					const parsed = JSON.parse(frame) as { type?: string; data?: unknown };
					return parsed.type === 'input' && typeof parsed.data === 'string' ? parsed.data : '';
				} catch {
					return frame; // raw text frame: transported verbatim
				}
			})
			.join('');
	}, urlPart);
}

const hostA = 'section[data-testid="host-a"]';
const hostB = 'section[data-testid="host-b"]';

test('terminal attaches honestly: pinned scope, SSH target, live connect state, resize frames', async ({
	page
}) => {
	const root = page.locator(`${hostA} [data-testid="execution-root"]`);
	await expect(root).toBeVisible();
	await expect(root.getByTestId('execution-scope')).toHaveText(SERVER_A);
	await expect(root.getByTestId('terminal-target')).toHaveText('operator@198.51.100.20:2222');
	// The xterm viewport mounts for real.
	await expect(root.getByTestId('terminal-viewport').locator('.xterm')).toHaveCount(1);

	// Honest pre-open state, then the injected transport opens. Both hosts
	// attach on mount: two sockets, each pinned to its own target.
	await expect
		.poll(async () => (await apiState(page)).sockets.length, { timeout: 8000 })
		.toBe(2);
	let state = await apiState(page);
	const socketA = state.sockets.find((row) => row.url.includes(SERVER_A));
	const socketB = state.sockets.find((row) => row.url.includes(SERVER_B));
	expect(socketA?.url).toContain(`/api/v1/servers/${SERVER_A}/terminal`);
	expect(socketB?.url).toContain(`/api/v1/servers/${SERVER_B}/terminal`);
	await expect(root.getByTestId('terminal-status')).toHaveAttribute('data-state', 'connecting');

	await apiCall(page, 'serverOpen', [state.sockets.indexOf(socketA!)]);
	await expect(root.getByTestId('terminal-status')).toHaveText('Connected');
	await expect(root.getByTestId('terminal-status')).toHaveAttribute('data-state', 'open');

	// Opening the session ships the current geometry as a resize frame.
	state = await apiState(page);
	const freshSocketA = state.sockets.find((row) => row.url.includes(SERVER_A));
	const frames = (freshSocketA?.sent ?? []).map(
		(frame) => JSON.parse(frame) as Record<string, unknown>
	);
	const resize = frames.find((frame) => frame['type'] === 'resize');
	expect(resize).toBeTruthy();
	expect(resize?.['cols']).toBeGreaterThan(0);
	expect(resize?.['rows']).toBeGreaterThan(0);

	// Info-read failure is an explicit error state, then Retry recovers —
	// never a faked view.
	await apiCall(page, 'failNextInfo', [
		{ status: 503, code: 'unavailable', message: 'api unavailable' }
	]);
	await root.getByTestId('terminal-reconnect').click();
	await expect(root.getByTestId('terminal-info-error')).toContainText(
		'Cannot open the terminal: the terminal service is unavailable.'
	);
	await expect(root.getByTestId('terminal-status')).toHaveAttribute('data-state', 'closed');
	await expect(root.getByTestId('terminal-status')).toContainText('Disconnected');

	await root.getByTestId('terminal-reconnect').click();
	await expect(root.getByTestId('terminal-info-error')).toHaveCount(0);
	state = await apiState(page);
	await apiCall(page, 'serverOpen', [state.sockets.length - 1]);
	await expect(root.getByTestId('terminal-status')).toHaveText('Connected');
});

test('terminal I/O: server bytes render, keyboard ships input frames, Clear wipes the view, Reconnect replaces the socket', async ({
	page
}) => {
	const root = page.locator(`${hostA} [data-testid="execution-root"]`);
	await expect
		.poll(async () => (await apiState(page)).sockets.length, { timeout: 8000 })
		.toBe(2);
	let state = await apiState(page);
	const firstIndex = state.sockets.findIndex((row) => row.url.includes(SERVER_A));
	await apiCall(page, 'serverOpen', [firstIndex]);
	await expect(root.getByTestId('terminal-status')).toHaveText('Connected');

	// Server → panel: raw text frames are written into the real xterm buffer.
	await apiCall(page, 'serverOutput', [firstIndex, 'welcome to fixture-01\r\n']);
	const viewport = root.getByTestId('terminal-viewport');
	await expect(viewport).toContainText('welcome to fixture-01');

	// Panel → server: real keyboard events become JSON input frames.
	await viewport.locator('textarea').focus();
	await page.keyboard.type('ls');
	await page.keyboard.press('Enter');
	await expect
		.poll(async () => typedInput(page, SERVER_A), { timeout: 8000 })
		.toBe('ls\r');

	// Clear wipes the local view only — nothing is sent to the target.
	await apiCall(page, 'serverOutput', [firstIndex, 'marker-gone-42\r\n\r\n']);
	await expect(viewport).toContainText('marker-gone-42');
	const beforeClear = (await apiState(page)).sockets.find((row) => row.url.includes(SERVER_A))!
		.sent.length;
	await root.getByTestId('terminal-clear').click();
	await expect(viewport).not.toContainText('marker-gone-42');
	const afterClear = (await apiState(page)).sockets.find((row) => row.url.includes(SERVER_A))!
		.sent.length;
	expect(afterClear).toBe(beforeClear);

	// Reconnect: the panel closes the old socket (client side), opens a new
	// one through the injected factory, and the stream resumes. Scrollback
	// emitted after the Clear and before the drop survives the reconnect —
	// only the transport is replaced, never the view.
	await apiCall(page, 'serverOutput', [firstIndex, 'before-drop\r\n']);
	await expect(viewport).toContainText('before-drop');
	await root.getByTestId('terminal-reconnect').click();
	state = await apiState(page);
	expect(state.sockets).toHaveLength(3);
	const oldSocket = state.sockets[firstIndex];
	expect(oldSocket.clientClosed).toEqual({ code: 1000, reason: 'panel detached' });
	const nextIndex = state.sockets.length - 1;
	expect(state.sockets[nextIndex].url).toContain(SERVER_A);
	await expect(root.getByTestId('terminal-status')).toHaveAttribute('data-state', 'connecting');
	await apiCall(page, 'serverOpen', [nextIndex]);
	await expect(root.getByTestId('terminal-status')).toHaveText('Connected');
	await apiCall(page, 'serverOutput', [nextIndex, 'resumed\r\n']);
	await expect(viewport).toContainText('resumed');
	await expect(viewport).toContainText('before-drop');
});

test('server-side close and transport errors are reported honestly, and reconnect recovers', async ({
	page
}) => {
	const root = page.locator(`${hostA} [data-testid="execution-root"]`);
	await expect
		.poll(async () => (await apiState(page)).sockets.length, { timeout: 8000 })
		.toBe(2);
	let state = await apiState(page);
	const firstIndex = state.sockets.findIndex((row) => row.url.includes(SERVER_A));
	await apiCall(page, 'serverOpen', [firstIndex]);
	await expect(root.getByTestId('terminal-status')).toHaveText('Connected');

	// A server-side close carries its reason into the badge — no fake green.
	await apiCall(page, 'serverClose', [firstIndex, 1011, 'remote shutdown']);
	await expect(root.getByTestId('terminal-status')).toHaveText('Disconnected · remote shutdown');
	await expect(root.getByTestId('terminal-status')).toHaveAttribute('data-state', 'closed');

	// Reconnect after a transport error says so without inventing a cause.
	await root.getByTestId('terminal-reconnect').click();
	state = await apiState(page);
	const nextIndex = state.sockets.length - 1;
	await apiCall(page, 'serverError', [nextIndex]);
	await expect(root.getByTestId('terminal-status')).toHaveText('Disconnected · connection error');

	// And a clean re-attach still works after both failure shapes.
	await root.getByTestId('terminal-reconnect').click();
	state = await apiState(page);
	await apiCall(page, 'serverOpen', [state.sockets.length - 1]);
	await expect(root.getByTestId('terminal-status')).toHaveText('Connected');
});

test('scope and services fail closed; the real registry validates the feature entry; hosts stay isolated', async ({
	page
}) => {
	// Bare host (no services bag): the panel fails closed, no traffic.
	await expect(
		page.locator(`section[data-testid="host-bare"] [data-testid="terminal-unusable"]`)
	).toBeVisible();
	// Global scope contradicts the server-only entry: the registry rejects it.
	await expect(
		page.locator('section[data-testid="host-global"] [data-testid="panel-unavailable"]')
	).toBeVisible();
	// The sibling is unaffected by every neighbor.
	await expect(page.locator('[data-testid="sibling-panel"]')).toBeVisible();

	// The REAL registry validates the REAL entry: scope policy, singleton key,
	// geometry; the lazy panel chunk was genuinely fetched through the entry's
	// loader function.
	const { registry: summary } = await apiState(page);
	expect(summary.kinds).toContain('execution');
	expect(summary.scopeChecks.serverScope).toBe('ok');
	expect(summary.scopeChecks.globalScope).toBe('scope_kind_not_allowed');
	expect(summary.scopeChecks.unknownKind).toBe('unknown_panel_kind');
	expect(summary.defaultGeometry).toEqual({ x: 72, y: 72, width: 640, height: 520 });
	expect(summary.minGeometry).toEqual({ x: 0, y: 0, width: 460, height: 360 });
	expect(summary.instanceKey).toBe(`execution::server/${SERVER_A}`);
	expect(servedChunks.length).toBeGreaterThan(0);

	// Instance isolation: two server-scoped hosts, two separately-pinned
	// sockets; neither transport addresses the other target.
	await expect
		.poll(async () => (await apiState(page)).sockets.length, { timeout: 8000 })
		.toBe(2);
	const { sockets } = await apiState(page);
	expect(sockets.map((row) => row.url).filter((url) => url.includes(SERVER_A))).toHaveLength(1);
	expect(sockets.map((row) => row.url).filter((url) => url.includes(SERVER_B))).toHaveLength(1);
	// Exactly one info read per host, each naming its own pinned target.
	const { calls } = await apiState(page);
	const reads = calls.filter((call) => call.op === 'terminalInfo');
	expect(reads.map((call) => call.arg).sort()).toEqual([SERVER_A, SERVER_B]);
});

test('hide/unmount closes the attached socket; remount attaches fresh', async ({
	page
}) => {
	const root = page.locator(`${hostA} [data-testid="execution-root"]`);
	await expect(root.getByTestId('terminal-target')).toHaveText('operator@198.51.100.20:2222');
	await expect
		.poll(async () => (await apiState(page)).sockets.length, { timeout: 8000 })
		.toBe(2);
	let state = await apiState(page);
	const attached = state.sockets.find((row) => row.url.includes(SERVER_A));
	expect(attached?.clientClosed).toBeNull();

	// Hide (unmount): the panel's teardown closes its socket with the
	// documented client code.
	await apiCall(page, 'toggleHost', ['A']);
	await expect
		.poll(async () => (await apiState(page)).sockets.find((row) => row.url.includes(SERVER_A))?.clientClosed, {
			timeout: 8000
		})
		.toEqual({ code: 1000, reason: 'panel detached' });
	const closedCount = (await apiState(page)).sockets.length;

	// Remount is a fresh attach: a new pinned socket, honest connecting state.
	await apiCall(page, 'toggleHost', ['A']);
	const freshRoot = page.locator(`${hostA} [data-testid="execution-root"]`);
	await expect(freshRoot.getByTestId('terminal-target')).toHaveText('operator@198.51.100.20:2222', {
		timeout: 8000
	});
	state = await apiState(page);
	expect(state.sockets.length).toBeGreaterThan(closedCount);
	const fresh = state.sockets[state.sockets.length - 1];
	expect(fresh.url).toContain(SERVER_A);
	await expect(freshRoot.getByTestId('terminal-status')).toHaveAttribute('data-state', 'connecting');
});
