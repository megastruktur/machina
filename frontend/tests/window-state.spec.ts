import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * 01-window-state — real-browser proofs for the instance-scoped window
 * lifecycle, focus and stacking state (windowed-workspace ADR §2
 * state/controller layer, §5 SSR isolation, §7.2 lifecycle, §10.1 PT2/PT5,
 * §10.2 NT5/NT6; contracts.md C08 rule 5, C11).
 *
 * Exercise, against the reviewed 01-web component-host fixture and the frozen
 * descriptor contract from 01-window-types:
 *  - factory isolation: `createWorkspaceState` twice produces fully
 *    independent workspaces — no module-level store, no SSR singleton;
 *  - open validates against the frozen descriptor contract (unknown panel
 *    kind, server scope without server_id, NaN/non-integer geometry,
 *    forbidden title content) and creates nothing on rejection; accepted
 *    opens produce stable instances with pinned scopes;
 *  - frozen instance policy (ADR §7.1 `singleton`): reopening the same
 *    object reuses and focuses the live instance — same window_id, geometry
 *    and scope never rewritten; policy granularity is caller-owned;
 *  - focus/stacking (PT2): deterministic bottom→top order, z renormalized
 *    to 1..n after every transition (bounded stacking), no-op focus changes
 *    nothing;
 *  - move commits geometry only — scope, identity, stacking and focus stay
 *    pinned; invalid geometry is rejected without state change;
 *  - close (PT5): release hooks run exactly once, last-registered-first;
 *    the window leaves the persisted-layout domain; sibling windows and
 *    their targets are untouched; extra fields on an open request (a
 *    would-be domain channel) are dropped and never invoked (NT6);
 *  - server selection cannot retarget windows (NT5): scopes stay pinned
 *    through focus/move/close of siblings; closing a window on one server
 *    leaves the other server's window and target intact.
 *
 * The harness is the same self-contained pattern as the reviewed
 * window-types.spec.ts: an in-memory Vite production build (`write: false`,
 * virtual entry) bundling the real module + the reviewed fixture; Playwright
 * route interception serves it; no backend, no temp files, no committed
 * fixture changes.
 */

const FIXTURE_URL = '/mai-window-state-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const VIRTUAL_ID = 'virtual:mai-window-state-entry';

const STATE = `${FRONTEND_ROOT}src/lib/workspace/state.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;

/**
 * Registry kinds the probes open against — stand-ins for the frozen
 * PanelRegistry set (owned by 01-panel-registry), same set as
 * window-types.spec.ts; "log-viewer" is the ADR §11 unregistered-kind
 * fixture and must stay absent.
 */
const REGISTRY_KINDS = ['chat', 'model-settings', 'server-profile'];

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'd2f6a9c0-3b4e-4f5a-8c1d-2e3f4a5b6c7d';
const RUN_ID_A = '44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a21';
const RUN_ID_B = '99e7c2f0-8d1a-4b2c-9e3f-4a5b6c7d8e9f';

const GEOMETRY_A = { x: 120, y: 80, width: 720, height: 480 };
const GEOMETRY_B = { x: 40, y: 40, width: 640, height: 400 };

/** Server-scoped chat request pinned to a run object (ADR §3.2). */
function chatOn(
	serverId: string,
	runId: string,
	geometry: { x: number; y: number; width: number; height: number } = GEOMETRY_A
) {
	return {
		panel_kind: 'chat',
		scope: { kind: 'server', server_id: serverId, object: { kind: 'run', id: runId } },
		geometry
	};
}

/**
 * Instance-policy fixtures — stand-ins for the frozen PanelRegistry
 * `singleton` field (ADR §7.1); the registry owner decides the granularity,
 * the state layer only enforces "equal non-null key ⇒ one live instance".
 */
const POLICY_FIXTURES = {
	// chat: one live instance per panel kind + full scope (same object).
	'same-object': (panelKind: string, scope: unknown) =>
		panelKind === 'chat' ? `chat:${JSON.stringify(scope)}` : null,
	// model-settings: one live instance across all scopes of the kind.
	'kind-wide': (panelKind: string) => (panelKind === 'model-settings' ? 'model-settings' : null),
	// server-profile: free multi-instance.
	never: () => null
} as const;

type PolicyName = keyof typeof POLICY_FIXTURES;

/**
 * Minimal bundle shape the harness consumes (same structural boundary as
 * window-types.spec.ts — vite 7 keeps Rollup's output types internal).
 */
type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

/** Descriptor shape as it crosses the CDP boundary (schema-v1 whitelist). */
interface ProbeDescriptor {
	window_id: string;
	panel_kind: string;
	scope: { kind: string; server_id?: string; object?: { kind: string; id: string } };
	geometry: { x: number; y: number; width: number; height: number };
	presentation: string;
	z: number;
	title_override: string | null;
}

/** Probe results mirror the module's discriminated results across the CDP boundary. */
interface ProbeIssue {
	code: string;
	message: string;
}
type ProbeOpen =
	| { ok: true; descriptor: ProbeDescriptor; created: boolean }
	| { ok: false; issue: ProbeIssue };
type ProbeMove =
	| { ok: true; value: ProbeDescriptor }
	| { ok: false; issue: ProbeIssue };
interface ProbeSnapshot {
	revision: number;
	windows: ProbeDescriptor[];
	focused_id: string | null;
}

/** Shape the virtual entry exposes on `window` for the probes below. */
interface WindowStateProbeApi {
	surface(): string[];
	create(kinds: string[], policy: PolicyName): number;
	open(index: number, request: unknown, hookLabels?: string[]): ProbeOpen;
	openWithDomainSpy(index: number, request: unknown): ProbeOpen;
	domainSpyReads(index: number): number;
	focus(index: number, windowId: string): boolean;
	move(index: number, windowId: string, geometry: unknown): ProbeMove;
	close(index: number, windowId: string): boolean;
	snapshot(index: number): ProbeSnapshot;
	stateSurface(index: number): string[];
	hookLog(index: number): string[];
	subscribe(index: number): number;
	unsubscribe(subscription: number): void;
	seenRevisions(subscription: number): number[];
}

declare global {
	interface Window {
		__maiStateApi?: WindowStateProbeApi;
	}
}

let harnessJs = '';
let harnessCss = '';

/**
 * Virtual entry mounted in the browser. Exposes thin wrappers over the real
 * module (the policy fixture table lives in-browser — callbacks cannot cross
 * the CDP boundary) and mounts the reviewed component-host fixture as the
 * real-mount proof.
 */
function entrySource(): string {
	return `
import { mount } from 'svelte';
import ComponentHost from ${JSON.stringify(HOST)};
import * as workspaceState from ${JSON.stringify(STATE)};

const POLICY_FIXTURES = {
	'same-object': (panelKind, scope) => (panelKind === 'chat' ? 'chat:' + JSON.stringify(scope) : null),
	'kind-wide': (panelKind) => (panelKind === 'model-settings' ? 'model-settings' : null),
	'never': () => null
};

const states = [];
const hookLogs = [];
const subscriptions = [];
const domainSpyReads = [];

window.__maiStateApi = {
	surface: () => Object.keys(workspaceState).sort(),
	create: (kinds, policy) => {
		const index = states.length;
		states.push(workspaceState.createWorkspaceState({
			knownPanelKinds: new Set(kinds),
			instanceKey: POLICY_FIXTURES[policy]
		}));
		hookLogs.push([]);
		return index;
	},
	open: (index, request, hookLabels) => {
		const outcome = states[index].open(request);
		if (!outcome.ok) return outcome;
		for (const label of hookLabels ?? []) {
			outcome.release(() => hookLogs[index].push('release:' + label));
		}
		return { ok: true, descriptor: outcome.descriptor, created: outcome.created };
	},
	// Opens with a smuggled extra request field backed by a read-counting
	// getter — a would-be domain channel. The count stays 0 iff no lifecycle
	// transition (open validation, close, reuse) ever reads the field.
	openWithDomainSpy: (index, request) => {
		let reads = 0;
		const smuggled = {
			...request,
			get cancelRun() {
				reads += 1;
				return 'domain-cancel-invocation';
			}
		};
		domainSpyReads[index] = 0;
		const outcome = states[index].open(smuggled);
		domainSpyReads[index] = reads;
		if (!outcome.ok) return outcome;
		return { ok: true, descriptor: outcome.descriptor, created: outcome.created };
	},
	domainSpyReads: (index) => domainSpyReads[index],
	focus: (index, windowId) => states[index].focus(windowId),
	move: (index, windowId, geometry) => states[index].move(windowId, geometry),
	close: (index, windowId) => states[index].close(windowId),
	snapshot: (index) => states[index].snapshot(),
	stateSurface: (index) => Object.keys(states[index]).sort(),
	hookLog: (index) => [...hookLogs[index]],
	subscribe: (index) => {
		const seen = [];
		const unsubscribe = states[index].subscribe((snapshot) => seen.push(snapshot.revision));
		const id = subscriptions.length;
		subscriptions.push({ seen, unsubscribe });
		return id;
	},
	unsubscribe: (id) => subscriptions[id].unsubscribe(),
	seenRevisions: (id) => [...subscriptions[id].seen]
};

mount(ComponentHost, { target: document.getElementById('app'), props: { label: 'MAI window state probe' } });
`;
}

/** Serves the virtual entry from the in-memory bundle; nothing hits disk. */
function harnessPlugin(code: string): Plugin {
	return {
		name: 'mai-window-state-harness',
		resolveId(id) {
			if (id === VIRTUAL_ID) return `\0${VIRTUAL_ID}`;
			return undefined;
		},
		load(id) {
			if (id === `\0${VIRTUAL_ID}`) return code;
			return undefined;
		}
	};
}

test.beforeAll(async () => {
	const result = await build({
		configFile: false,
		root: FRONTEND_ROOT,
		logLevel: 'error',
		plugins: [svelte(), harnessPlugin(entrySource())],
		build: {
			write: false,
			emptyOutDir: true,
			outDir: 'node_modules/.mai-window-state-fixture',
			rollupOptions: {
				input: VIRTUAL_ID,
				output: { entryFileNames: 'demo.js', assetFileNames: 'demo[extname]' }
			}
		}
	});

	const built = result as BuiltBundle | BuiltBundle[];
	const outputs = Array.isArray(built) ? built : [built];
	for (const { output } of outputs) {
		for (const item of output) {
			if (item.type === 'chunk') harnessJs += item.code;
			else if (item.fileName.endsWith('.css')) harnessCss += String(item.source);
		}
	}
	if (!harnessJs) {
		throw new Error('harness bundle incomplete: missing JS chunk');
	}
});

test.beforeEach(async ({ page }) => {
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI window-state fixture</title>
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
		if (pathname.endsWith('demo.js')) {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: harnessJs });
		}
		if (pathname.endsWith('demo.css')) {
			return route.fulfill({ contentType: 'text/css; charset=utf-8', body: harnessCss });
		}
		return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
	});
});

/** Navigates and waits until the entry module has exposed the probe API. */
async function gotoProbe(page: Page): Promise<void> {
	await page.goto(FIXTURE_URL);

	// The entry is a deferred `<script type="module">`: under full-suite CPU
	// contention it can execute after `goto` resolves, so a bare `evaluate`
	// races it. waitForFunction retries — same readiness contract as the
	// reviewed window-types.spec.ts harness.
	await page.waitForFunction(() => window.__maiStateApi !== undefined, undefined, {
		timeout: 15_000
	});
}

/** Creates a fresh workspace state in the browser and returns its index. */
async function create(page: Page, policy: PolicyName): Promise<number> {
	return page.evaluate(([kinds, policyName]) => window.__maiStateApi!.create(kinds, policyName), [
		REGISTRY_KINDS,
		policy
	] as const);
}

/** Opens a window in the indexed workspace (registering labeled release hooks). */
async function openWindow(
	page: Page,
	index: number,
	request: unknown,
	hookLabels?: string[]
): Promise<ProbeOpen> {
	return page.evaluate(([idx, req, labels]) => window.__maiStateApi!.open(idx, req, labels), [
		index,
		request,
		hookLabels
	] as const);
}

/** Narrows an open outcome to the accepted shape (boundary type guard). */
function requireOpen(outcome: ProbeOpen): { descriptor: ProbeDescriptor; created: boolean } {
	if (!outcome.ok) {
		throw new Error(`open was rejected: ${outcome.issue.code} (${outcome.issue.message})`);
	}
	return outcome;
}

/** Focuses a window by id in the indexed workspace. */
async function focusWindow(page: Page, index: number, windowId: string): Promise<boolean> {
	return page.evaluate(([idx, id]) => window.__maiStateApi!.focus(idx, id), [index, windowId] as const);
}

/** Moves a window in the indexed workspace. */
async function moveWindow(
	page: Page,
	index: number,
	windowId: string,
	geometry: unknown
): Promise<ProbeMove> {
	return page.evaluate(([idx, id, geom]) => window.__maiStateApi!.move(idx, id, geom), [
		index,
		windowId,
		geometry
	] as const);
}

/** Closes a window by id in the indexed workspace. */
async function closeWindow(page: Page, index: number, windowId: string): Promise<boolean> {
	return page.evaluate(([idx, id]) => window.__maiStateApi!.close(idx, id), [index, windowId] as const);
}

/** Reads the full snapshot of the indexed workspace. */
async function snapshotOf(page: Page, index: number): Promise<ProbeSnapshot> {
	return page.evaluate((idx) => window.__maiStateApi!.snapshot(idx), index);
}

test('probe surface mounts in the reviewed component-host and exports exactly the state factory API', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await gotoProbe(page);

	// Real client mount in the reviewed fixture (not static HTML).
	await expect(page.getByTestId('component-host')).toHaveAttribute('data-mounted', 'true');

	// Module surface: only the factory — no module-level store, no SSR
	// singleton, no hidden "current server" mutation target.
	const surface = await page.evaluate(() => window.__maiStateApi!.surface());
	expect(surface).toEqual(['createWorkspaceState']);

	// Controller surface: lifecycle transitions + reads only. No retarget,
	// no scope mutation, no domain/cancellation channel of any kind (NT5/NT6
	// are impossible by construction, not by convention).
	const index = await create(page, 'never');
	const stateSurface = await page.evaluate((idx) => window.__maiStateApi!.stateSurface(idx), index);
	expect(stateSurface).toEqual(['close', 'focus', 'move', 'open', 'snapshot', 'subscribe']);

	// A fresh workspace is empty and starts at revision 0.
	expect(await snapshotOf(page, index)).toEqual({
		revision: 0,
		windows: [],
		focused_id: null
	});

	expect(errors).toEqual([]);
});

test('factory twice: two workspaces are fully isolated (no shared module state)', async ({
	page
}) => {
	await gotoProbe(page);

	const first = await create(page, 'never');
	const second = await create(page, 'never');

	// Open on server A in the first workspace, server B in the second.
	const firstOpen = requireOpen(await openWindow(page, first, chatOn(SERVER_A, RUN_ID_A)));
	const secondOpen = requireOpen(await openWindow(page, second, chatOn(SERVER_B, RUN_ID_B)));
	expect(firstOpen.descriptor.window_id).not.toEqual(secondOpen.descriptor.window_id);
	const firstId = firstOpen.descriptor.window_id;
	const secondId = secondOpen.descriptor.window_id;

	// Transitions in the first workspace leave the second untouched…
	expect(await focusWindow(page, first, firstId)).toBe(true);
	const moved = await moveWindow(page, first, firstId, { x: 300, y: 200, width: 500, height: 400 });
	expect(moved.ok).toBe(true);
	expect(await snapshotOf(page, second)).toEqual({
		revision: 1,
		windows: [
			{
				window_id: secondId,
				panel_kind: 'chat',
				scope: { kind: 'server', server_id: SERVER_B, object: { kind: 'run', id: RUN_ID_B } },
				geometry: GEOMETRY_A,
				presentation: 'normal',
				z: 1,
				title_override: null
			}
		],
		focused_id: secondId
	});

	// …and closing in the second leaves the first untouched.
	expect(await closeWindow(page, second, secondId)).toBe(true);
	expect(await snapshotOf(page, second)).toEqual({ revision: 2, windows: [], focused_id: null });
	const firstAfter = await snapshotOf(page, first);
	expect(firstAfter.windows).toHaveLength(1);
	expect(firstAfter.focused_id).toEqual(firstId);
	expect(firstAfter.windows[0].geometry).toEqual({ x: 300, y: 200, width: 500, height: 400 });
});

test('open creates stable validated instances; every accepted open focuses and stacks on top', async ({
	page
}) => {
	await gotoProbe(page);

	const index = await create(page, 'never');
	const first = requireOpen(await openWindow(page, index, chatOn(SERVER_A, RUN_ID_A)));
	expect(first.created).toBe(true);
	expect(first.descriptor).toEqual({
		window_id: first.descriptor.window_id,
		panel_kind: 'chat',
		scope: { kind: 'server', server_id: SERVER_A, object: { kind: 'run', id: RUN_ID_A } },
		geometry: GEOMETRY_A,
		presentation: 'normal',
		z: 1,
		title_override: null
	});
	// window_id is an opaque client UUID (C01) — stable view identity.
	expect(first.descriptor.window_id).toMatch(
		/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
	);
	const firstId = first.descriptor.window_id;

	let snapshot = await snapshotOf(page, index);
	expect(snapshot.revision).toEqual(1);
	expect(snapshot.focused_id).toEqual(firstId);

	// A second open on server B stacks above and takes focus; the first
	// instance is stable — same id, scope, geometry and z.
	const second = requireOpen(
		await openWindow(page, index, chatOn(SERVER_B, RUN_ID_B, GEOMETRY_B))
	);
	const secondId = second.descriptor.window_id;
	expect(second.created).toBe(true);
	expect(secondId).not.toEqual(firstId);

	snapshot = await snapshotOf(page, index);
	expect(snapshot.revision).toEqual(2);
	expect(snapshot.focused_id).toEqual(secondId);
	expect(snapshot.windows.map((window) => window.window_id)).toEqual([firstId, secondId]);
	expect(snapshot.windows.map((window) => window.z)).toEqual([1, 2]);
	expect(snapshot.windows[0]).toEqual({
		window_id: firstId,
		panel_kind: 'chat',
		scope: { kind: 'server', server_id: SERVER_A, object: { kind: 'run', id: RUN_ID_A } },
		geometry: GEOMETRY_A,
		presentation: 'normal',
		z: 1,
		title_override: null
	});
});

test('open rejects the frozen bad set with stable codes and creates nothing', async ({ page }) => {
	await gotoProbe(page);

	const index = await create(page, 'never');

	const rejects: Array<[unknown, string]> = [
		[
			{ panel_kind: 'log-viewer', scope: { kind: 'global' }, geometry: GEOMETRY_A },
			'unknown_panel_kind'
		],
		[
			{ panel_kind: 'chat', scope: { kind: 'server' }, geometry: GEOMETRY_A },
			'scope_server_without_server_id'
		],
		[
			{
				panel_kind: 'chat',
				scope: { kind: 'server', server_id: SERVER_A },
				geometry: { x: Number.NaN, y: 0, width: 100, height: 100 }
			},
			'bad_geometry'
		],
		[
			{
				panel_kind: 'chat',
				scope: { kind: 'server', server_id: SERVER_A },
				geometry: { x: 0, y: 0, width: 100.5, height: 100 }
			},
			'bad_geometry'
		],
		[
			{
				panel_kind: 'chat',
				scope: { kind: 'server', server_id: SERVER_A },
				geometry: GEOMETRY_A,
				title_override: 'token=sk-fxt-abc12345'
			},
			'forbidden_content'
		]
	];

	for (const [request, code] of rejects) {
		const outcome = await openWindow(page, index, request);
		expect(outcome).toEqual({ ok: false, issue: { code, message: expect.any(String) } });
	}

	// No rejected request left any trace: the workspace is still empty.
	expect(await snapshotOf(page, index)).toEqual({
		revision: 0,
		windows: [],
		focused_id: null
	});
});

test('frozen instance policy: same object reuses the live instance; granularity is policy-owned', async ({
	page
}) => {
	await gotoProbe(page);

	// chat is registered with the "same object" policy.
	const index = await create(page, 'same-object');
	const first = requireOpen(await openWindow(page, index, chatOn(SERVER_A, RUN_ID_A)));
	const firstId = first.descriptor.window_id;

	// Reopening the same object reuses the live instance — same window_id,
	// same pinned scope and geometry; the new request's geometry/title are
	// never written onto the existing window. Already on top: no revision churn.
	const reopen = requireOpen(
		await openWindow(page, index, {
			...chatOn(SERVER_A, RUN_ID_A, { x: 999, y: 999, width: 100, height: 100 }),
			title_override: 'renamed'
		})
	);
	expect(reopen.created).toBe(false);
	expect(reopen.descriptor.window_id).toEqual(firstId);
	expect(reopen.descriptor).toEqual(first.descriptor);
	expect((await snapshotOf(page, index)).revision).toEqual(1);
	expect((await snapshotOf(page, index)).windows).toHaveLength(1);

	// A different object on the same server is a different instance key:
	// it opens a second window.
	const otherObject = requireOpen(await openWindow(page, index, chatOn(SERVER_A, RUN_ID_B)));
	const otherId = otherObject.descriptor.window_id;
	expect(otherObject.created).toBe(true);
	expect(otherId).not.toEqual(firstId);

	// Reopening the first object now REUSES and RAISES it: same id, still its
	// own geometry/scope, back on top with focus.
	const raise = requireOpen(await openWindow(page, index, chatOn(SERVER_A, RUN_ID_A)));
	expect(raise.created).toBe(false);
	expect(raise.descriptor.window_id).toEqual(firstId);
	const snapshot = await snapshotOf(page, index);
	expect(snapshot.windows.map((window) => window.window_id)).toEqual([otherId, firstId]);
	expect(snapshot.focused_id).toEqual(firstId);
	expect(snapshot.windows.map((window) => window.z)).toEqual([1, 2]);
	expect(snapshot.windows[1].geometry).toEqual(GEOMETRY_A);

	// model-settings with the kind-wide policy: a global instance exists and
	// a server-scoped open of the same kind reuses it (granularity is owned
	// by the frozen registry policy, not by scope equality).
	const kindWide = await create(page, 'kind-wide');
	const globalSettings = requireOpen(
		await openWindow(page, kindWide, {
			panel_kind: 'model-settings',
			scope: { kind: 'global' },
			geometry: GEOMETRY_B
		})
	);
	const scopedSettings = await openWindow(page, kindWide, {
		panel_kind: 'model-settings',
		scope: { kind: 'server', server_id: SERVER_B },
		geometry: GEOMETRY_A
	});
	// The server-scoped request reuses the live global instance unchanged.
	expect(scopedSettings).toEqual({
		ok: true,
		created: false,
		descriptor: globalSettings.descriptor
	});
	expect((await snapshotOf(page, kindWide)).windows).toHaveLength(1);

	// server-profile with the "never" policy: the same scope twice yields two
	// independent live instances with distinct stable ids.
	const multi = await create(page, 'never');
	const profileRequest = {
		panel_kind: 'server-profile',
		scope: { kind: 'server', server_id: SERVER_A },
		geometry: GEOMETRY_A
	};
	const profileA = requireOpen(await openWindow(page, multi, profileRequest));
	const profileB = requireOpen(await openWindow(page, multi, profileRequest));
	expect(profileA.created).toBe(true);
	expect(profileB.created).toBe(true);
	expect(profileA.descriptor.window_id).not.toEqual(profileB.descriptor.window_id);
});

test('focus/stacking (PT2): deterministic bottom→top order, z bounded to 1..n, no-op focus is free', async ({
	page
}) => {
	await gotoProbe(page);

	const index = await create(page, 'never');
	const ids: string[] = [];
	for (const [serverId, runId] of [
		[SERVER_A, RUN_ID_A],
		[SERVER_B, RUN_ID_B],
		[SERVER_A, RUN_ID_B]
	] as const) {
		const opened = requireOpen(await openWindow(page, index, chatOn(serverId, runId)));
		ids.push(opened.descriptor.window_id);
	}
	const [idA, idB, idC] = ids;

	const zOrder = async (): Promise<Array<[string, number]>> => {
		const snapshot = await snapshotOf(page, index);
		return snapshot.windows.map((window) => [window.window_id, window.z]);
	};
	const revisionOf = async (): Promise<number> => (await snapshotOf(page, index)).revision;

	// Deterministic initial stacking: open order ⇒ z 1,2,3; last open focused.
	expect(await zOrder()).toEqual([
		[idA, 1],
		[idB, 2],
		[idC, 3]
	]);
	expect((await snapshotOf(page, index)).focused_id).toEqual(idC);

	// Focusing the bottom window raises it above all others, preserving the
	// relative order of every other window, and takes focus.
	expect(await focusWindow(page, index, idA)).toBe(true);
	expect(await zOrder()).toEqual([
		[idB, 1],
		[idC, 2],
		[idA, 3]
	]);
	expect((await snapshotOf(page, index)).focused_id).toEqual(idA);

	const revisionBefore = await revisionOf();

	// Focusing the already-focused window is a no-op: no reorder, no revision.
	expect(await focusWindow(page, index, idA)).toBe(true);
	expect(await zOrder()).toEqual([
		[idB, 1],
		[idC, 2],
		[idA, 3]
	]);
	expect(await revisionOf()).toEqual(revisionBefore);

	// Bounded stacking: 40 alternations never grow any z beyond the live
	// window count — z is renormalized to 1..n after every transition.
	for (let round = 0; round < 40; round += 1) {
		await focusWindow(page, index, round % 2 === 0 ? idC : idA);
		const order = await zOrder();
		expect(order.map(([, z]) => z)).toEqual([1, 2, 3]);
	}

	// Determinism: the exact same transition sequence on a fresh factory
	// produces the exact same final stacking, focus and revision.
	const replay = await create(page, 'never');
	const replayIds: string[] = [];
	for (const [serverId, runId] of [
		[SERVER_A, RUN_ID_A],
		[SERVER_B, RUN_ID_B],
		[SERVER_A, RUN_ID_B]
	] as const) {
		const opened = requireOpen(await openWindow(page, replay, chatOn(serverId, runId)));
		replayIds.push(opened.descriptor.window_id);
	}
	// The main run focused idA once (real change) and once more (no-op)
	// before the alternation; replay repeats that exact prefix.
	await focusWindow(page, replay, replayIds[0]);
	const replayBeforeNoop = (await snapshotOf(page, replay)).revision;
	await focusWindow(page, replay, replayIds[0]);
	expect((await snapshotOf(page, replay)).revision).toEqual(replayBeforeNoop);
	for (let round = 0; round < 40; round += 1) {
		await focusWindow(page, replay, round % 2 === 0 ? replayIds[2] : replayIds[0]);
	}
	const finalFirst = await snapshotOf(page, index);
	const finalReplay = await snapshotOf(page, replay);
	// Final order after the alternation (last focus = A): B, C, A — identical
	// on both factories modulo the fresh UUIDs.
	expect(finalFirst.windows.map((window) => window.window_id)).toEqual([idB, idC, idA]);
	expect(finalFirst.focused_id).toEqual(idA);
	expect(finalFirst.windows.map((window) => window.z)).toEqual(
		finalReplay.windows.map((window) => window.z)
	);
	expect(finalReplay.focused_id).not.toEqual(null);
	expect(finalFirst.revision).toEqual(finalReplay.revision);

	// Unknown ids report false and change nothing.
	expect(
		await focusWindow(page, index, '00000000-0000-4000-8000-000000000000')
	).toBe(false);
	expect(await revisionOf()).toEqual(finalFirst.revision);
});

test('move commits geometry only — scope, identity, stacking and focus stay pinned', async ({
	page
}) => {
	await gotoProbe(page);

	const index = await create(page, 'never');
	const firstId = requireOpen(await openWindow(page, index, chatOn(SERVER_A, RUN_ID_A)))
		.descriptor.window_id;
	const secondOpen = requireOpen(
		await openWindow(page, index, chatOn(SERVER_B, RUN_ID_B, GEOMETRY_B))
	);
	const secondId = secondOpen.descriptor.window_id;

	// Focus the first window so we can prove move() does not steal focus.
	expect(await focusWindow(page, index, firstId)).toBe(true);
	const before = await snapshotOf(page, index);

	const movedGeometry = { x: 250, y: 160, width: 800, height: 600 };
	const moved = await moveWindow(page, index, secondId, movedGeometry);
	expect(moved.ok).toBe(true);
	if (moved.ok) {
		expect(moved.value).toEqual({ ...secondOpen.descriptor, geometry: movedGeometry });
	}

	const after = await snapshotOf(page, index);
	expect(after.revision).toEqual(before.revision + 1);
	expect(after.focused_id).toEqual(firstId);
	expect(after.windows.map((window) => window.window_id)).toEqual(
		before.windows.map((window) => window.window_id)
	);
	expect(after.windows.map((window) => window.z)).toEqual([1, 2]);
	expect(after.windows[0].geometry).toEqual(movedGeometry);
	expect(after.windows[1].geometry).toEqual(GEOMETRY_A);

	// Invalid geometry is rejected with the frozen code and changes nothing.
	const afterValid = await snapshotOf(page, index);
	for (const geometry of [
		{ x: Number.NaN, y: 0, width: 10, height: 10 },
		{ x: 0, y: 0, width: 10.5, height: 10 },
		{ x: 0, y: 0, width: 10, height: 0 }
	]) {
		const rejected = await moveWindow(page, index, secondId, geometry);
		expect(rejected).toEqual({ ok: false, issue: { code: 'bad_geometry', message: expect.any(String) } });
	}
	const unknownWindow = await moveWindow(
		page,
		index,
		'00000000-0000-4000-8000-000000000000',
		movedGeometry
	);
	expect(unknownWindow).toEqual({
		ok: false,
		issue: { code: 'bad_window_id', message: expect.any(String) }
	});
	expect(await snapshotOf(page, index)).toEqual(afterValid);
});

test('close (PT5): release hooks run once, last-registered-first; siblings untouched; no domain channel', async ({
	page
}) => {
	await gotoProbe(page);

	const index = await create(page, 'never');

	// The would-be domain channel: an extra `cancelRun` field on an open
	// request, backed in-browser by a read-counting getter. The whitelist
	// construction drops it and no lifecycle transition ever reads it.
	const smuggled = requireOpen(
		await page.evaluate(
			([idx, request]) => window.__maiStateApi!.openWithDomainSpy(idx, request),
			[index, chatOn(SERVER_A, RUN_ID_A)] as const
		)
	);
	const firstId = smuggled.descriptor.window_id;

	// Subscription teardown hooks (fake event-source subscriptions) on a
	// second window on server B.
	const withHooks = await openWindow(
		page,
		index,
		chatOn(SERVER_B, RUN_ID_B, GEOMETRY_B),
		['sub-a', 'sub-b', 'sub-c']
	);
	const secondId = requireOpen(withHooks).descriptor.window_id;
	const snapshotWithTwo = await snapshotOf(page, index);

	// Close the first window: exactly its lifecycle ends; the sibling window,
	// its subscription hooks and its pinned target are untouched.
	expect(await closeWindow(page, index, firstId)).toBe(true);
	expect(await page.evaluate((idx) => window.__maiStateApi!.hookLog(idx), index)).toEqual([]);
	expect(await page.evaluate((idx) => window.__maiStateApi!.domainSpyReads(idx), index)).toEqual(0);

	// The smuggled field never entered the descriptor (whitelist-only shape).
	const descriptorFields = snapshotWithTwo.windows.map((window) =>
		Object.keys(window).sort().join(',')
	);
	expect(descriptorFields).toEqual([
		'geometry,panel_kind,presentation,scope,title_override,window_id,z',
		'geometry,panel_kind,presentation,scope,title_override,window_id,z'
	]);

	// Focus falls to the new top window when the focused window closes.
	expect(snapshotWithTwo.focused_id).toEqual(secondId);

	// Closing the remaining window runs its hooks exactly once, LIFO.
	expect(await closeWindow(page, index, secondId)).toBe(true);
	expect(await page.evaluate((idx) => window.__maiStateApi!.hookLog(idx), index)).toEqual([
		'release:sub-c',
		'release:sub-b',
		'release:sub-a'
	]);

	// Closing again is false and re-runs nothing; unknown ids are false.
	expect(await closeWindow(page, index, secondId)).toBe(false);
	expect(await page.evaluate((idx) => window.__maiStateApi!.hookLog(idx), index)).toEqual([
		'release:sub-c',
		'release:sub-b',
		'release:sub-a'
	]);
	expect(await closeWindow(page, index, '00000000-0000-4000-8000-000000000000')).toBe(false);

	// Scoped teardown: closing one window never fires a sibling's hooks.
	const scoped = await create(page, 'never');
	const scopedFirst = requireOpen(
		await openWindow(page, scoped, chatOn(SERVER_A, RUN_ID_A), ['A-subscription'])
	);
	const scopedSecond = requireOpen(
		await openWindow(page, scoped, chatOn(SERVER_B, RUN_ID_B, GEOMETRY_B), ['B-subscription'])
	);
	expect(await closeWindow(page, scoped, scopedFirst.descriptor.window_id)).toBe(true);
	expect(await page.evaluate((idx) => window.__maiStateApi!.hookLog(idx), scoped)).toEqual([
		'release:A-subscription'
	]);
	const scopedSnapshot = await snapshotOf(page, scoped);
	expect(scopedSnapshot.windows).toEqual([{ ...scopedSecond.descriptor, z: 1 }]);
	expect(scopedSnapshot.focused_id).toEqual(scopedSecond.descriptor.window_id);
});

test('pinned scopes (NT5): no transition rewrites a window target; server selection cannot exist', async ({
	page
}) => {
	await gotoProbe(page);

	const index = await create(page, 'same-object');
	const scopeA = { kind: 'server', server_id: SERVER_A, object: { kind: 'run', id: RUN_ID_A } };
	const scopeB = { kind: 'server', server_id: SERVER_B, object: { kind: 'run', id: RUN_ID_B } };

	const firstId = requireOpen(
		await openWindow(page, index, { panel_kind: 'chat', scope: scopeA, geometry: GEOMETRY_A })
	).descriptor.window_id;
	const second = requireOpen(
		await openWindow(page, index, { panel_kind: 'chat', scope: scopeB, geometry: GEOMETRY_B })
	);
	const secondId = second.descriptor.window_id;

	// Exercise every transition this card owns, interleaved across the two
	// servers: focus A three times, move B, reopen A's object (reuse/focus).
	for (let round = 0; round < 3; round += 1) {
		expect(await focusWindow(page, index, firstId)).toBe(true);
	}
	expect(
		await moveWindow(page, index, secondId, { x: 500, y: 300, width: 640, height: 480 })
	).toMatchObject({ ok: true, value: { geometry: { x: 500, y: 300, width: 640, height: 480 } } });
	const reopen = await openWindow(page, index, {
		panel_kind: 'chat',
		scope: scopeA,
		geometry: GEOMETRY_A
	});
	expect(reopen).toMatchObject({ ok: true, created: false, descriptor: { window_id: firstId } });

	const snapshot = await snapshotOf(page, index);
	expect(snapshot.windows.map((window) => window.scope)).toEqual([scopeB, scopeA]);
	expect(snapshot.windows.map((window) => window.window_id)).toEqual([secondId, firstId]);
	expect(snapshot.windows[0].geometry).toEqual({ x: 500, y: 300, width: 640, height: 480 });

	// Closing a window on server A leaves server B's window and target intact
	// (including the geometry committed by the move above).
	expect(await closeWindow(page, index, firstId)).toBe(true);
	const afterClose = await snapshotOf(page, index);
	expect(afterClose.windows).toEqual([
		{ ...second.descriptor, geometry: { x: 500, y: 300, width: 640, height: 480 }, z: 1 }
	]);
	expect(afterClose.focused_id).toEqual(secondId);
});

test('snapshots and open outcomes are deep copies — callers cannot alias live state', async ({
	page
}) => {
	await gotoProbe(page);

	const index = await create(page, 'never');
	const opened = requireOpen(await openWindow(page, index, chatOn(SERVER_A, RUN_ID_A)));
	expect(opened.created).toBe(true);

	// Mutate the returned outcome descriptor…
	opened.descriptor.geometry.x = 9999;
	if (opened.descriptor.scope.kind === 'server') {
		opened.descriptor.scope.server_id = SERVER_B;
	}

	// …and a snapshot copy, including a smuggled extra window.
	const snapshot = await snapshotOf(page, index);
	snapshot.windows[0].z = 42;
	snapshot.windows.push({
		window_id: 'fake',
		panel_kind: 'chat',
		scope: { kind: 'global' },
		geometry: GEOMETRY_A,
		presentation: 'normal',
		z: 2,
		title_override: null
	});
	snapshot.focused_id = 'fake';

	// Live state is untouched by all of it.
	expect(await snapshotOf(page, index)).toEqual({
		revision: 1,
		windows: [
			{
				window_id: opened.descriptor.window_id,
				panel_kind: 'chat',
				scope: { kind: 'server', server_id: SERVER_A, object: { kind: 'run', id: RUN_ID_A } },
				geometry: GEOMETRY_A,
				presentation: 'normal',
				z: 1,
				title_override: null
			}
		],
		focused_id: opened.descriptor.window_id
	});
});

test('store contract: subscribe fires with the current snapshot and after every transition', async ({
	page
}) => {
	await gotoProbe(page);

	const index = await create(page, 'never');
	const subscription = await page.evaluate((idx) => window.__maiStateApi!.subscribe(idx), index);
	const seenRevisions = async (): Promise<number[]> =>
		page.evaluate((id) => window.__maiStateApi!.seenRevisions(id), subscription);
	// Immediate current-value delivery (Svelte store contract).
	expect(await seenRevisions()).toEqual([0]);

	const firstId = requireOpen(await openWindow(page, index, chatOn(SERVER_A, RUN_ID_A)))
		.descriptor.window_id;
	const secondId = requireOpen(
		await openWindow(page, index, chatOn(SERVER_B, RUN_ID_B, GEOMETRY_B))
	).descriptor.window_id;

	expect(await focusWindow(page, index, firstId)).toBe(true);
	expect(await closeWindow(page, index, secondId)).toBe(true);
	expect(await seenRevisions()).toEqual([0, 1, 2, 3, 4]);

	// Rejected transitions do not notify.
	const rejected = await openWindow(page, index, {
		panel_kind: 'log-viewer',
		scope: { kind: 'global' },
		geometry: GEOMETRY_A
	});
	expect(rejected).toMatchObject({ ok: false });

	// After unsubscribe, no further delivery.
	await page.evaluate((id) => window.__maiStateApi!.unsubscribe(id), subscription);
	await openWindow(page, index, chatOn(SERVER_B, RUN_ID_A));
	expect(await seenRevisions()).toEqual([0, 1, 2, 3, 4]);
});
