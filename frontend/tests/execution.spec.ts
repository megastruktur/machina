import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-05-web-execution — real-browser proofs for the execution panel.
 *
 * Harness convention (observed from `policy.spec.ts` / `panel-host.spec.ts`):
 * a virtual fixture app is built IN MEMORY with the real production toolchain
 * and mounts the REAL `ExecutionPanel` through the REAL `PanelHost` and the
 * REAL panel registry with the REAL feature entry (`executionEntry`) — the
 * lazy `import()` of the panel is exercised for real (`servedChunks`).
 *
 * The fixture API mirrors the reviewed producers (`mai.api.servers`,
 * `mai.api.jobs`, `mai.api.approvals`, `mai.api.password_challenges`):
 * replayed run states (never engine-called), a recorded cancel that answers
 * `outcome: null` (an honest pending — guard rule 3), the approval-store
 * denial precedence, and the challenge lane's one-use consume. Documented
 * emulation: the fixture accepts the panel's presentation at the binding
 * layer and returns the frozen `ChallengeOutcomeView` — the wire's structural
 * argv bound is a lane-side DoS guard (empty argv is rejected by the real
 * `ChallengeOperationBody`); the run-seam that delivers an argv-mode
 * presentation to the browser is the P05-gate assembly finding recorded in
 * the evidence README. The UI contract proven here — exact field-for-field
 * echo of the receipt binding + the submitted operation, honest display of
 * every refusal, secret cleared on every exit path, no fabricated success —
 * is independent of that seam.
 *
 * Secrecy note: the fixture SECRET below is a throwaway fixture string, never
 * a real credential; the tests additionally assert it never appears in the
 * DOM or in any storage.
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
const APPROVAL_ID = 'c1d2e3f4-a5b6-4c7d-8e9f-1a2b3c4d5e6f';
const OP_ID = 'd2e3f4a5-b6c7-4d8e-9f0a-2b3c4d5e6f7a';
const PAYLOAD_HASH = 'ab'.repeat(32);
const RUN_A = '1a1a1a1a-1111-4222-8333-444455556666';
const RUN_B = '2b2b2b2b-1111-4222-8333-444455556666';
const WINDOW_A = '6f6f6a7b-8c9d-4e8a-8a9b-4c5d6e7f8a9c';
const WINDOW_B = '7a7a6a7b-8c9d-4e8a-8a9b-4c5d6e7f8a9d';
const WINDOW_GLOBAL = '3c3c3c3c-1111-4222-8333-444455556666';
const WINDOW_BARE = '4d4d4d4d-1111-4222-8333-444455556666';
const WINDOW_SIBLING = '5e5e5e5e-1111-4222-8333-444455556666';

/** Throwaway fixture secret (never a real credential; hygiene asserted). */
const SECRET = 'fixture-s3cret-05-execution-only';
const SESSION_PRINCIPAL = `sesh-${'aa'.repeat(16)}`;
const OTHER_PRINCIPAL = `sesh-${'bb'.repeat(16)}`;

type Fail = { status: number; code: string; message: string };

interface ExecutionWindowApi {
	calls: Array<{ op: string; payload?: unknown; arg?: unknown }>;
	approvals: Record<string, Record<string, unknown>>;
	armRunScript(host: 'A' | 'B', snapshots: unknown[]): void;
	failNextSubmitRun(failure: Fail | null): void;
	failNextReadProfile(failure: Fail | null): void;
	failNextReadRun(failure: Fail | null): void;
	failNextCancel(failure: Fail | null): void;
	failNextReadApproval(failure: Fail | null): void;
	failNextChallenge(code: string | null): void;
	consumeApprovalExternally(approvalId: string): void;
	resetApproval(approvalId: string): void;
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
		__maiExecutionApi?: ExecutionWindowApi;
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
 * The fixture app. Two server-scoped execution hosts (A: sudo target; B:
 * passwordless target), one bare host (no services), one global-scope host
 * (registry scope rejection) and one benign sibling panel.
 */
const APP_SOURCE = `
<script>
	import PanelHost from '${PANELHOST}';
	import ComponentHost from '${HOST}';
	import { createPanelRegistry, definePanelEntry, singletonInstanceKey } from '${REGISTRY}';
	import { createRawSnippet } from 'svelte';
	import { ApiError } from '${CLIENT}';
	import {
		executionEntry,
		executionServices,
		EXECUTION_PANEL_KIND,
		SHELL_CAPABILITY_ID
	} from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const SERVER_B = '${SERVER_B}';
	const APPROVAL_ID = '${APPROVAL_ID}';
	const OP_ID = '${OP_ID}';
	const PAYLOAD_HASH = '${PAYLOAD_HASH}';
	const RUN_A = '${RUN_A}';
	const RUN_B = '${RUN_B}';
	const SESSION_PRINCIPAL = '${SESSION_PRINCIPAL}';
	const OTHER_PRINCIPAL = '${OTHER_PRINCIPAL}';

	// --- fixture backend: mirrors the reviewed producers ---------------------

	function profile(serverId, overrides) {
		return Object.assign(
			{
				schema_version: 1,
				server_id: serverId,
				name: serverId === SERVER_A ? 'Exec Target Alpha' : 'Exec Target Beta',
				prompt_revision: 2,
				environment: 'linux_ssh',
				ssh:
					serverId === SERVER_A
						? {
								host: '198.51.100.20',
								port: 2222,
								user: 'operator',
								host_key: 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
								credential_ref: 'MAI_SSH_KEY_FIXTURE'
							}
						: {
								host: '198.51.100.21',
								port: 22,
								user: 'deploy',
								host_key: 'SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB',
								credential_ref: 'MAI_SSH_KEY_OTHER'
							},
				privilege: serverId === SERVER_A ? 'sudo' : 'none',
				capabilities: ['host.shell', 'host.logs'],
				revision: 4,
				sha256: 'cd'.repeat(32),
				projection: { revision: 4, stale: false }
			},
			overrides ?? {}
		);
	}
	const profiles = { [SERVER_A]: profile(SERVER_A), [SERVER_B]: profile(SERVER_B) };

	const approvals = {
		[APPROVAL_ID]: {
			approval_id: APPROVAL_ID,
			capability_id: SHELL_CAPABILITY_ID,
			server_id: SERVER_A,
			payload_hash: PAYLOAD_HASH,
			policy_revision: 3,
			status: 'pending',
			created_by: SESSION_PRINCIPAL,
			created_at: '2026-10-15T09:00:00+00:00',
			expires_at: '2026-10-15T09:10:00+00:00',
			consumed_by: null,
			consumed_at: null
		}
	};

	function step(seq, state, receipt) {
		return { seq, capability_id: SHELL_CAPABILITY_ID, state, receipt: receipt ?? null };
	}
	function receiptWaiting() {
		return {
			v: 1,
			op_id: OP_ID,
			capability_id: SHELL_CAPABILITY_ID,
			allowed: false,
			reason: 'approval_required_but_absent',
			effect: 'not_attempted',
			policy_revision: 3,
			approval_id: APPROVAL_ID,
			approval_spent: false,
			approval_code: null,
			executor_ok: null,
			payload_hash: PAYLOAD_HASH,
			error_type: null,
			output_truncated: null,
			omitted_bytes: null
		};
	}
	function runDetail(runId, serverId, overrides) {
		return Object.assign(
			{
				run_id: runId,
				server_id: serverId,
				parent_run_id: null,
				depth: 0,
				profile_revision: 4,
				prompt_revision: 2,
				model_profile: 'direct',
				requested_grants: [SHELL_CAPABILITY_ID],
				state: 'queued',
				cancel_requested_at: null,
				submitted_at: '2026-10-15T09:00:00Z',
				updated_at: '2026-10-15T09:00:01Z',
				steps: []
			},
			overrides ?? {}
		);
	}

	// Per-host replayed-run scripts: readRun shifts the next snapshot and then
	// repeats the last one (the facade replays persisted state, never guesses).
	const runs = {
		A: { id: RUN_A, script: [], last: runDetail(RUN_A, SERVER_A) },
		B: { id: RUN_B, server_id: SERVER_B, script: [], last: runDetail(RUN_B, SERVER_B) }
	};
	const calls = [];
	let failNextReadProfile = null;
	let failNextSubmitRun = null;
	let failNextReadRun = null;
	let failNextCancel = null;
	let failNextReadApproval = null;
	let failNextChallengeCode = null;

	const api = {
		async readProfile(serverId) {
			calls.push({ op: 'readProfile', arg: serverId });
			if (failNextReadProfile) {
				const failure = failNextReadProfile;
				failNextReadProfile = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			const row = profiles[serverId];
			if (!row) throw new ApiError(404, 'not_found', 'no enrolled profile for this id');
			return structuredClone(row);
		},
		async submitRun(payload) {
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'submitRun', payload: wire });
			if (failNextSubmitRun) {
				const failure = failNextSubmitRun;
				failNextSubmitRun = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			const host = payload.server_id === SERVER_A ? 'A' : 'B';
			runs[host].id = payload.run_id;
			runs[host].last = runDetail(payload.run_id, payload.server_id, { state: 'queued' });
			return { run: structuredClone(runs[host].last), deduplicated: false };
		},
		async readRun(runId) {
			calls.push({ op: 'readRun', arg: runId });
			if (failNextReadRun) {
				const failure = failNextReadRun;
				failNextReadRun = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			const run = runId === runs.B.id ? runs.B : runs.A;
			if (run.script.length > 0) {
				// The facade replays full persisted rows: a scripted snapshot
				// updates the recorded fields of the SAME durable run, it never
				// replaces the row's identity binding.
				run.last = Object.assign({}, run.last, run.script.shift());
			}
			return structuredClone(run.last);
		},
		async cancelRun(runId, payload) {
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'cancelRun', arg: runId, payload: wire });
			if (failNextCancel) {
				const failure = failNextCancel;
				failNextCancel = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			const run = runId === runs.B.id ? runs.B : runs.A;
			// Real boundary behavior (jobs.py): the request is recorded ONCE and
			// the observation budget expires into an honest pending — 'outcome'
			// null never claims "cancelled" (engine guard rule 3).
			return {
				run_id: runId,
				state: run.last.state,
				outcome: null,
				requested_at: '2026-10-15T09:05:00Z',
				outcome_at: null,
				requested_now: true,
				unresolved_seqs: []
			};
		},
		async readApproval(approvalId) {
			calls.push({ op: 'readApproval', arg: approvalId });
			if (failNextReadApproval) {
				const failure = failNextReadApproval;
				failNextReadApproval = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			const row = approvals[approvalId];
			if (!row) throw new ApiError(404, 'not_found', 'no approval record for this id');
			return { ...row };
		},
		async submitChallenge(payload) {
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'submitChallenge', payload: wire });
			if (failNextChallengeCode) {
				const code = failNextChallengeCode;
				failNextChallengeCode = null;
				const spent = code === 'password_rejected' || code === 'account_locked';
				const status = spent ? 403 : code === 'not_found' ? 404 : 409;
				throw new ApiError(status, code, 'fixture refusal (' + code + ')');
			}
			// Binding precedence mirrors the approval store + channel seams; a
			// refusal never mutates the row and never reaches any transport.
			const row = approvals[payload.approval_id];
			if (!row) throw new ApiError(404, 'not_found', 'no approval record for this id');
			if (row.status !== 'pending') {
				throw new ApiError(409, 'already_consumed', 'decision already consumed (one-use)');
			}
			if (payload.capability_id !== row.capability_id) {
				throw new ApiError(409, 'capability_mismatch', 'presented capability does not match');
			}
			if (payload.server_id !== row.server_id) {
				throw new ApiError(409, 'target_mismatch', 'presented target does not match');
			}
			if (payload.payload_hash !== row.payload_hash) {
				throw new ApiError(409, 'payload_mismatch', 'presented payload hash does not match');
			}
			if (payload.policy_revision !== row.policy_revision) {
				throw new ApiError(409, 'revision_mismatch', 'presented policy revision does not match');
			}
			const enrolled = profiles[payload.server_id].ssh;
			const identity = payload.identity;
			if (
				payload.account !== enrolled.user ||
				identity.host !== enrolled.host ||
				identity.port !== enrolled.port ||
				identity.user !== enrolled.user ||
				identity.host_key !== enrolled.host_key ||
				identity.credential_ref !== enrolled.credential_ref
			) {
				throw new ApiError(
					422,
					'validation_failed',
					'presented identity does not match the enrolled profile'
				);
			}
			row.status = 'consumed';
			row.consumed_by = OTHER_PRINCIPAL;
			row.consumed_at = '2026-10-15T09:06:00+00:00';
			// The frozen ChallengeOutcomeView (C04 rule 4): no submission echo.
			return {
				op_id: payload.operation.op_id,
				server_id: payload.server_id,
				display: payload.operation.display,
				status: 'succeeded',
				exit_code: 0,
				stdout: 'uid=1000(operator)\\n',
				stdout_bytes: 19,
				stdout_truncated: false,
				stderr: '',
				stderr_bytes: 0,
				stderr_truncated: false,
				timed_out: false,
				cwd_change_failed: false,
				duration_seconds: 0.42
			};
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

	const windowApi = {
		calls,
		approvals,
		armRunScript(host, snapshots) {
			runs[host].script = structuredClone(snapshots);
		},
		failNextSubmitRun(failure) {
			failNextSubmitRun = failure;
		},
		failNextReadProfile(failure) {
			failNextReadProfile = failure;
		},
		failNextReadRun(failure) {
			failNextReadRun = failure;
		},
		failNextCancel(failure) {
			failNextCancel = failure;
		},
		failNextReadApproval(failure) {
			failNextReadApproval = failure;
		},
		failNextChallenge(code) {
			failNextChallengeCode = code;
		},
		consumeApprovalExternally(approvalId) {
			const row = approvals[approvalId];
			row.status = 'consumed';
			row.consumed_by = OTHER_PRINCIPAL;
			row.consumed_at = '2026-10-15T09:07:00+00:00';
		},
		resetApproval(approvalId) {
			const row = approvals[approvalId];
			row.status = 'pending';
			row.consumed_by = null;
			row.consumed_at = null;
		},
		toggleHost(host) {
			if (host === 'A') showA = !showA;
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
		// Dynamic dispatch by serialized name: the fixture's own typed API
		// viewed as a name→method table (a static per-method switch would
		// duplicate every signature); the typeof guard below still checks
		// the lookup before any call.
		const table = value as unknown as Record<string, (...a: never[]) => void>;
		const fn = table[name];
		if (typeof fn !== 'function') throw new Error(`unknown fixture method ${name}`);
		fn.apply(value, callArgs as never[]);
	}, pair);
}

interface RecordedCall {
	op: string;
	payload?: unknown;
	arg?: unknown;
}

interface RegistrySummary {
	kinds: string[];
	scopeChecks: { serverScope: string; globalScope: string; unknownKind: string };
	defaultGeometry: unknown;
	minGeometry: unknown;
	instanceKey: string | null;
}

/** Snapshot the fixture's recorded calls and precomputed registry summary. */
async function apiState(
	page: Page
): Promise<{ calls: RecordedCall[]; registry: RegistrySummary }> {
	return page.evaluate(() => {
		const value = window.__maiExecutionApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		return { calls: value.calls, registry: value.registry };
	});
}

/**
 * Test-only recorded-wire boundary: the fixture recorded these payloads
 * itself (structuredClone of the exact bodies the panel sent), so the shape
 * is pinned by the assertion block that follows; the named transfer keeps
 * that single trust point explicit instead of inline member casts.
 */
function recorded<T>(value: unknown, what: string): T {
	if (typeof value !== 'object' || value === null) {
		throw new Error(`${what} was not recorded as an object`);
	}
	return value as T;
}

interface RecordedSubmitBody {
	run_id: string;
	server_id: string;
	model_profile: string;
	mode: string;
	credential_references: string[];
	steps: Array<{ capability_id: string; payload: Record<string, unknown> }>;
}

const hostA = 'section[data-testid="host-a"]';
const hostB = 'section[data-testid="host-b"]';

test('target, privilege request and transport render honestly; load failure shows an error, not all-green', async ({
	page
}) => {
	const root = page.locator(`${hostA} [data-testid="execution-root"]`);
	await expect(root.getByTestId('target-name')).toHaveText('Exec Target Alpha');
	await expect(root.getByTestId('target-privilege')).toContainText('sudo');
	await expect(root.getByTestId('target-transport')).toHaveText('operator@198.51.100.20:2222');
	await expect(root.getByTestId('run-none')).toHaveText('No run is being tracked.');
	// The passwordless target names its privilege request just as honestly.
	const rootB = page.locator(`${hostB} [data-testid="execution-root"]`);
	await expect(rootB.getByTestId('target-privilege')).toContainText('none');

	// Profile read failure path: an explicit error state (data-error-code on
	// the visible alert), then Retry recovers — never a faked view.
	await apiCall(page, 'failNextReadProfile', [
		{ status: 503, code: 'unavailable', message: 'api unavailable' }
	]);
	await apiCall(page, 'toggleHost', ['A']);
	await apiCall(page, 'toggleHost', ['A']);
	const failed = root.getByTestId('target-error');
	await expect(failed).toBeVisible({ timeout: 8000 });
	await expect(failed).toHaveAttribute('data-error-code', 'unavailable');
	await root.getByTestId('target-retry').click();
	await expect(root.getByTestId('target-name')).toHaveText('Exec Target Alpha', {
		timeout: 8000
	});
});

test('submit posts the client-minted idempotent intent and tracks replayed state', async ({
	page
}) => {
	await apiCall(page, 'armRunScript', [
		'A',
		[
			{ state: 'queued', steps: [] },
			{
				state: 'running',
				steps: [{ seq: 1, capability_id: 'host.shell', state: 'running', receipt: null }]
			}
		]
	]);
	const root = page.locator(`${hostA} [data-testid="execution-root"]`);
	// Client-side form guard: no command ⇒ no call.
	await root.getByTestId('submit-run').click();
	await expect(root.getByTestId('form-error')).toBeVisible();
	await root.getByTestId('command-input').fill('systemctl status sshd');
	await root.getByTestId('timeout-input').fill('30');
	await root.getByTestId('budget-input').fill('1024');
	await root.getByTestId('cwd-input').fill('/var/log');
	await root.getByTestId('submit-run').click();

	await expect(root.getByTestId('run-state')).toHaveText('Queued', { timeout: 5000 });
	await expect(root.getByTestId('run-state')).toHaveText('Running', { timeout: 8000 });

	const { calls } = await apiState(page);
	const submit = calls.find((call) => call.op === 'submitRun');
	const body = recorded<RecordedSubmitBody>(submit?.payload, 'submitRun payload');
	expect(body.run_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
	expect(body.server_id).toBe(SERVER_A);
	expect(body.model_profile).toBe('direct');
	expect(body.mode).toBe('ask');
	expect(body.credential_references).toEqual([]);
	expect(body.steps).toHaveLength(1);
	expect(body.steps[0].capability_id).toBe('host.shell');
	expect(body.steps[0].payload).toEqual({
		command: 'systemctl status sshd',
		timeout: 30,
		output_budget: 1024,
		working_directory: '/var/log'
	});
	await expect(root.getByTestId('step-list').getByTestId('step-state')).toHaveText('Running');
});

test('terminal, truncated and unproven states are honest; cancel is never claimed verified', async ({
	page
}) => {
	await apiCall(page, 'armRunScript', [
		'A',
		[
			{
				state: 'succeeded',
				steps: [
					{
						seq: 1,
						capability_id: 'host.shell',
						state: 'succeeded',
						receipt: {
							v: 1,
							op_id: OP_ID,
							capability_id: 'host.shell',
							status: 'succeeded',
							exit_code: 0,
							output_truncated: true,
							omitted_bytes: 18753,
							allowed: true,
							effect: 'executed'
						}
					}
				]
			}
		]
	]);
	const root = page.locator(`${hostA} [data-testid="execution-root"]`);
	await root.getByTestId('command-input').fill('dmesg');
	await root.getByTestId('submit-run').click();
	await expect(root.getByTestId('step-truncation')).toHaveText(
		'Truncated — 18753 bytes omitted',
		{ timeout: 8000 }
	);
	await expect(root.getByTestId('run-state')).toHaveText('Succeeded');
	await expect(root.getByTestId('cancel-run')).toHaveCount(0);

	// Unproven outcome: reconciliation, never a guessed status.
	await apiCall(page, 'armRunScript', [
		'A',
		[
			{
				state: 'reconciling',
				steps: [
					{
						seq: 1,
						capability_id: 'host.shell',
						state: 'outcome_unknown',
						receipt: {
							v: 1,
							op_id: OP_ID,
							capability_id: 'host.shell',
							status: 'running',
							reason: 'status_unresolved',
							output_truncated: false,
							omitted_bytes: null
						}
					}
				]
			}
		]
	]);
	await apiCall(page, 'failNextReadRun', [
		{ status: 503, code: 'unavailable', message: 'jobs engine unavailable' }
	]);
	await root.getByTestId('submit-run').click();
	await expect(root.getByTestId('poll-error')).toBeVisible({ timeout: 8000 });
	await root.getByTestId('poll-retry').click();
	await expect(root.getByTestId('run-state')).toHaveText('Reconciling an unproven outcome', {
		timeout: 8000
	});
	await expect(root.getByTestId('step-state').first()).toHaveText(
		'Outcome unknown — reconciliation required'
	);
	await expect(root).not.toContainText('Succeeded');

	// Cancel: recorded request answers an honest pending; verification comes
	// only from the replayed run state.
	await apiCall(page, 'armRunScript', [
		'A',
		[
			{
				state: 'running',
				cancel_requested_at: '2026-10-15T09:05:00Z',
				steps: [{ seq: 1, capability_id: 'host.shell', state: 'cancel_requested', receipt: null }]
			},
			{
				state: 'cancelled',
				cancel_requested_at: '2026-10-15T09:05:00Z',
				steps: [{ seq: 1, capability_id: 'host.shell', state: 'cancelled', receipt: null }]
			}
		]
	]);
	await root.getByTestId('cancel-run').click();
	const cancelStatus = root.getByTestId('cancel-status');
	await expect(cancelStatus).toHaveAttribute('data-outcome', 'pending', { timeout: 8000 });
	await expect(cancelStatus).toContainText('not yet verified');
	await expect(cancelStatus).not.toContainText('verified: the engine reports');
	await expect(root.getByTestId('run-cancel-flag')).toBeVisible();
	// The replayed state is the only verification path.
	await expect(root.getByTestId('run-state')).toHaveText('Cancelled (verified)', {
		timeout: 8000
	});
	const { calls } = await apiState(page);
	const cancel = calls.find((call) => call.op === 'cancelRun');
	expect(cancel?.payload).toEqual({ cancel_children: false });
});

test('password exists only for the bound challenge; echo is field-for-field; secret cleared everywhere', async ({
	page
}) => {
	await apiCall(page, 'armRunScript', [
		'A',
		[
			{
				state: 'waiting_approval',
				steps: [
					{
						seq: 1,
						capability_id: 'host.shell',
						state: 'pending',
						receipt: {
							v: 1,
							op_id: OP_ID,
							capability_id: 'host.shell',
							allowed: false,
							reason: 'approval_required_but_absent',
							effect: 'not_attempted',
							policy_revision: 3,
							approval_id: APPROVAL_ID,
							approval_spent: false,
							approval_code: null,
							executor_ok: null,
							payload_hash: PAYLOAD_HASH,
							error_type: null,
							output_truncated: null,
							omitted_bytes: null
						}
					}
				]
			},
			{
				state: 'running',
				steps: [
					{
						seq: 1,
						capability_id: 'host.shell',
						state: 'running',
						receipt: null
					}
				]
			}
		]
	]);
	const root = page.locator(`${hostA} [data-testid="execution-root"]`);
	await root.getByTestId('command-input').fill('apt upgrade -y');
	await root.getByTestId('timeout-input').fill('900');
	await root.getByTestId('budget-input').fill('2048');
	await root.getByTestId('submit-run').click();

	const challenge = root.getByTestId('challenge-section');
	await expect(challenge).toBeVisible({ timeout: 8000 });
	// The safe binding preview — a digest, never payload bytes.
	await expect(challenge.getByTestId('challenge-binding')).toContainText(
		`${PAYLOAD_HASH.slice(0, 12)}…`
	);
	await expect(challenge.getByTestId('challenge-approval-status')).toHaveText('pending');

	// The secret goes exactly once to the bound challenge and is then cleared.
	const password = root.getByTestId('challenge-password');
	await expect(password).toHaveValue('');
	await password.fill(SECRET);
	expect(await password.inputValue()).toBe(SECRET);
	await root.getByTestId('challenge-submit').click();

	const outcome = root.getByTestId('challenge-outcome');
	await expect(outcome).toBeVisible({ timeout: 8000 });
	await expect(outcome.getByTestId('challenge-outcome-status')).toHaveText('Succeeded');
	await expect(outcome.getByTestId('challenge-outcome-exit')).toHaveText('exit 0');
	// Cleared from the form whatever the outcome (acceptance 2): the resolved
	// challenge unmounts the field entirely — the value-clearing while the
	// form persists is proven on the denial paths below.
	await expect(password).toHaveCount(0);
	// Never rendered, never stored (acceptance 2).
	const bodyText = await page.locator('body').innerText();
	expect(bodyText).not.toContain(SECRET);
	const storage = await page.evaluate(() => ({
		local: Object.keys(localStorage),
		session: Object.keys(sessionStorage)
	}));
	expect(storage.local).toEqual([]);
	expect(storage.session).toEqual([]);
	// The run keeps its server-recorded state; the challenge consumed nothing
	// of the run's own progress proof.
	await expect(root.getByTestId('run-state')).toHaveText('Running', { timeout: 8000 });

	// The recorded presentation echoes the binding + the submitted operation.
	const { calls } = await apiState(page);
	const challengeCall = calls.find((call) => call.op === 'submitChallenge');
	expect(challengeCall, 'submitChallenge recorded').toBeTruthy();
	expect(challengeCall?.payload).toEqual({
		approval_id: APPROVAL_ID,
		capability_id: 'host.shell',
		server_id: SERVER_A,
		payload_hash: PAYLOAD_HASH,
		policy_revision: 3,
		account: 'operator',
		operation: {
			op_id: OP_ID,
			server_id: SERVER_A,
			display: 'host.shell',
			timeout: 900,
			output_budget: 2048,
			argv: [],
			shell: 'apt upgrade -y',
			working_directory: null
		},
		identity: {
			host: '198.51.100.20',
			port: 2222,
			user: 'operator',
			host_key: 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
			credential_ref: 'MAI_SSH_KEY_FIXTURE'
		},
		secret: SECRET
	});
});

test('challenge denials display value-free; a spent decision says so; nothing fakes success', async ({
	page
}) => {
	await apiCall(page, 'armRunScript', [
		'A',
		[
			{
				state: 'waiting_approval',
				steps: [
					{
						seq: 1,
						capability_id: 'host.shell',
						state: 'pending',
						receipt: {
							v: 1,
							op_id: OP_ID,
							capability_id: 'host.shell',
							allowed: false,
							reason: 'approval_required_but_absent',
							effect: 'not_attempted',
							policy_revision: 3,
							approval_id: APPROVAL_ID,
							approval_spent: false,
							approval_code: null,
							executor_ok: null,
							payload_hash: PAYLOAD_HASH,
							error_type: null,
							output_truncated: null,
							omitted_bytes: null
						}
					}
				]
			}
		]
	]);
	const root = page.locator(`${hostA} [data-testid="execution-root"]`);
	await root.getByTestId('command-input').fill('apt upgrade -y');
	await root.getByTestId('submit-run').click();
	await expect(root.getByTestId('challenge-section')).toBeVisible({ timeout: 8000 });

	// Replay after another session consumed the decision: denial displayed,
	// the secret cleared, no outcome fabricated. (The panel keeps its last
	// loaded binding view; the refusal text is the honest signal.)
	await apiCall(page, 'consumeApprovalExternally', [APPROVAL_ID]);
	await root.getByTestId('challenge-password').fill(SECRET);
	await root.getByTestId('challenge-submit').click();
	await expect(root.getByTestId('challenge-denial')).toContainText('already consumed', {
		timeout: 8000
	});
	await expect(root.getByTestId('challenge-password')).toHaveValue('');
	await expect(root.getByTestId('challenge-outcome')).toHaveCount(0);
	await expect(root.getByTestId('challenge-spent')).toHaveCount(0);

	// A refused password spends the one-use decision — said honestly, with a
	// fresh approval required, and still no fabricated success.
	await apiCall(page, 'resetApproval', [APPROVAL_ID]);
	await apiCall(page, 'failNextChallenge', ['password_rejected']);
	await expect(root.getByTestId('challenge-password')).toHaveValue('');
	await root.getByTestId('challenge-password').fill(SECRET);
	await root.getByTestId('challenge-submit').click();
	await expect(root.getByTestId('challenge-spent')).toContainText('decision was spent', {
		timeout: 8000
	});
	await expect(root.getByTestId('challenge-password')).toHaveValue('');
	await expect(root.getByTestId('challenge-outcome')).toHaveCount(0);
	const bodyText = await page.locator('body').innerText();
	expect(bodyText).not.toContain(SECRET);
});

test('a passwordless target never shows the secret form; decide stays with the policy panel', async ({
	page
}) => {
	await apiCall(page, 'armRunScript', [
		'B',
		[
			{
				state: 'waiting_approval',
				steps: [
					{
						seq: 1,
						capability_id: 'host.shell',
						state: 'pending',
						receipt: {
							v: 1,
							op_id: OP_ID,
							capability_id: 'host.shell',
							allowed: false,
							reason: 'approval_required_but_absent',
							effect: 'not_attempted',
							policy_revision: 3,
							approval_id: APPROVAL_ID,
							approval_spent: false,
							approval_code: null,
							executor_ok: null,
							payload_hash: PAYLOAD_HASH,
							error_type: null,
							output_truncated: null,
							omitted_bytes: null
						}
					}
				]
			}
		]
	]);
	const root = page.locator(`${hostB} [data-testid="execution-root"]`);
	await root.getByTestId('command-input').fill('uptime');
	await root.getByTestId('submit-run').click();
	await expect(root.getByTestId('challenge-section')).toBeVisible({ timeout: 8000 });
	await expect(root.getByTestId('challenge-not-sudo')).toBeVisible();
	await expect(root.getByTestId('challenge-password')).toHaveCount(0);
});

test('scope and services fail closed; the real registry validates the feature entry; hosts stay isolated', async ({
	page
}) => {
	// Bare host (no services bag): fail closed, no traffic.
	await expect(page.locator(`${'section[data-testid="host-bare"]'} [data-testid="panel-config-error"]`)).toBeVisible();
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

	// Instance isolation: submitting in A leaves B's fresh mount untouched.
	await apiCall(page, 'armRunScript', ['A', [{ state: 'queued', steps: [] }]]);
	const rootA = page.locator(`${hostA} [data-testid="execution-root"]`);
	await rootA.getByTestId('command-input').fill('id');
	await rootA.getByTestId('submit-run').click();
	await expect(rootA.getByTestId('run-state')).toHaveText('Queued', { timeout: 8000 });
	await expect(
		page.locator(`${hostB} [data-testid="execution-root"] [data-testid="run-none"]`)
	).toHaveText('No run is being tracked.');
	const { calls } = await apiState(page);
	const submits = calls.filter((call) => call.op === 'submitRun');
	expect(submits).toHaveLength(1);
	const onlySubmit = recorded<RecordedSubmitBody>(submits[0]?.payload, 'isolation submitRun');
	expect(onlySubmit.server_id).toBe(SERVER_A);
});

test('hide/unmount releases the scoped poll controller; remount starts fresh', async ({
	page
}) => {
	await apiCall(page, 'armRunScript', [
		'A',
		[
			{ state: 'running', steps: [] },
			{ state: 'running', steps: [] },
			{ state: 'running', steps: [] },
			{ state: 'running', steps: [] },
			{ state: 'running', steps: [] },
			{ state: 'running', steps: [] },
			{ state: 'running', steps: [] },
			{ state: 'running', steps: [] },
			{ state: 'running', steps: [] },
			{ state: 'running', steps: [] },
			{ state: 'running', steps: [] },
			{ state: 'running', steps: [] }
		]
	]);
	const root = page.locator(`${hostA} [data-testid="execution-root"]`);
	await root.getByTestId('command-input').fill('sleep 60');
	await root.getByTestId('submit-run').click();
	await expect(root.getByTestId('run-state')).toHaveText('Running', { timeout: 8000 });

	// While mounted, the scoped controller keeps polling: the read count grows.
	async function readRunCount(): Promise<number> {
		const { calls } = await apiState(page);
		return calls.filter((call) => call.op === 'readRun').length;
	}
	const first = await readRunCount();
	await page.waitForTimeout(3200);
	const second = await readRunCount();
	expect(second).toBeGreaterThan(first);

	// Unmount (hide): the poll timer must be released — the count freezes and
	// stays frozen past one full would-be interval.
	await apiCall(page, 'toggleHost', ['A']);
	const frozen = await readRunCount();
	await page.waitForTimeout(3200);
	await expect
		.poll(async () => readRunCount(), { timeout: 8000 })
		.toBe(frozen);

	// Remount is a fresh context: honest loading → ready, no tracked run.
	await apiCall(page, 'toggleHost', ['A']);
	const freshRoot = page.locator(`${hostA} [data-testid="execution-root"]`);
	await expect(freshRoot.getByTestId('target-name')).toHaveText('Exec Target Alpha', {
		timeout: 8000
	});
	await expect(freshRoot.getByTestId('run-none')).toHaveText('No run is being tracked.');
});
