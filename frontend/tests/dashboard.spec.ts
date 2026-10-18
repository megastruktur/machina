import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-10-web-dashboard — real-browser proofs for the overview/attention-inbox
 * panel (`OverviewPanel.svelte` + the feature-owned entry).
 *
 * Harness convention (reviewed `01-web` component-browser fixture): the fixture
 * compiles the REAL feature panel, the REAL feature entry, the REAL panel
 * host/registry and the shared UI primitives into a throwaway fixture app
 * served from virtual modules — nothing hits disk and no real backend exists.
 * The fixture API mirrors the reviewed producers 1:1 (the `10-monitor-api`
 * monitoring surface, the `08-app-api` adopted page, the `05-jobs-api` run
 * page and the reviewed run-event stream seam); recorded calls and pre-armed
 * responses are the test-observed boundary, not an imaginary backend. The
 * reviewed `10-web-evidence` `ObservationDetails` drawer is exercised as the
 * real detail surface inside the panel.
 *
 * Covered acceptance (brief 10-web-dashboard): the overview summarizes the
 * pinned server's actual monitor freshness, open findings, notification
 * attention and application/job state with links to the tested detail
 * surfaces; loading/empty/error/offline states are truthful and the overview
 * duplicates no collector or detail-panel logic; the panel registers through
 * the typed feature-owned entry and owns no window/drag/global-server logic;
 * acknowledgment and re-evaluation render exactly what the reviewed routes
 * answer, never optimistic success.
 */

const FIXTURE_URL = '/mai-dashboard-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-dashboard-app.svelte';
const ENTRY_ID = 'virtual:mai-dashboard-entry.js';

const PANEL = `${FRONTEND_ROOT}src/lib/panels/OverviewPanel.svelte`;
const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/10-web-dashboard.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
const OBS_1 = 'aa111111-1111-4222-8333-444455556666';
const OBS_2 = 'aa222222-2222-4333-8444-555566667777';
const OBS_B = 'aa333333-3333-4444-8555-666677778888';
const FINDING_1 = 'bb111111-1111-4333-8444-555566667777';
const ALERT_1 = 'cc111111-1111-4555-8666-777788889999';
const ALERT_2 = 'cc222222-2222-4555-8666-777788889999';
const APP_ADOPTED = 'dd111111-1111-4666-8777-888899990000';
const RUN_ID = 'ee111111-1111-4777-8888-999900000000';
const RUN_2 = 'ee222222-2222-4777-8888-999900000000';
const WINDOW_A = '7a7a7a7a-8c9d-4e8a-8a9b-4c5d6e7f8a9c';
const WINDOW_B = '8b8b8b8b-8c9d-4e8a-8a9b-4c5d6e7f8a9d';
const WINDOW_RUN = '9c9c9c9c-9999-4222-8333-444455556666';
const WINDOW_GLOBAL = '0d0d0d0d-9999-4222-8333-444455556666';
const WINDOW_BARE = '1e1e1e1e-9999-4222-8333-444455556666';
const WINDOW_NOSEAM = '2f2f2f2f-9999-4222-8333-444455556666';
const WINDOW_SIBLING = '3a3a3a3a-9999-4222-8333-444455556666';

const ACK_STAMP = '2026-10-17T12:00:00Z';
const EVENT_TS = '2026-10-17T11:59:00Z';

/** Untruncated runtime-failure receipt: a page error is a harness defect. */
const pageErrors: string[] = [];

type Fail = { status: number; code: string; message: string };

interface StreamRecord {
	runId: string;
	cursor: string | null;
	aborted: boolean;
	open: boolean;
}

interface DashboardWindowApi {
	calls: Array<{ op: string; payload?: unknown; args?: unknown[] }>;
	streams: StreamRecord[];
	failNextObservations(failure: Fail | null): void;
	failNextFindings(failure: Fail | null): void;
	failNextInbox(failure: Fail | null): void;
	failNextApps(failure: Fail | null): void;
	failNextRuns(failure: Fail | null): void;
	failNextAck(failure: Fail | null): void;
	failNextEvaluate(failure: Fail | null): void;
	armWrongScopeObservations(): void;
	armForeignEvaluate(): void;
	armDeferredObservations(): void;
	releaseObservations(pageView: unknown): void;
	setRunState(runId: string, state: string): void;
	deliverEvent(seq: number): void;
	closeStream(): void;
	toggleHostA(): void;
	toggleHostRun(): void;
	registry: {
		kinds: string[];
		scopeChecks: {
			serverScope: string;
			globalScope: string;
			pinnedRun: string;
			foreignObject: string;
			unknownKind: string;
		};
		defaultGeometry: unknown;
		minGeometry: unknown;
		instanceKey: string | null;
		pinnedInstanceKey: string | null;
	};
}

declare global {
	interface Window {
		__maiDashboardApi?: DashboardWindowApi;
		/** Set only if hostile fixture text ever executed — must stay undefined. */
		__maiXssFired?: boolean;
	}
}

type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; isEntry: boolean; fileName: string; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

// ---------------------------------------------------------------------------
// The fixture app (compiled once per worker; mounted fresh per test)
// ---------------------------------------------------------------------------

const APP_SOURCE = `
<script>
	import PanelHost from '${PANELHOST}';
	import ComponentHost from '${HOST}';
	import { createPanelRegistry, definePanelEntry, singletonInstanceKey } from '${REGISTRY}';
	import { createRawSnippet } from 'svelte';
	import { ApiError } from '${CLIENT}';
	import { OVERVIEW_PANEL_KIND, overviewEntry, overviewServices } from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const SERVER_B = '${SERVER_B}';
	const OBS_1 = '${OBS_1}';
	const OBS_2 = '${OBS_2}';
	const OBS_B = '${OBS_B}';
	const FINDING_1 = '${FINDING_1}';
	const ALERT_1 = '${ALERT_1}';
	const ALERT_2 = '${ALERT_2}';
	const APP_ADOPTED = '${APP_ADOPTED}';
	const RUN_ID = '${RUN_ID}';
	const RUN_2 = '${RUN_2}';
	const ACK_STAMP = '${ACK_STAMP}';

	// --- wire-view builders (the generated schema's exact field sets) --------

	function observationRow(observationId, serverId, fields) {
		return Object.assign(
			{
				observation_id: observationId,
				server_id: serverId,
				application_id: null,
				source: 'process',
				method: 'command',
				quality: 'measured',
				metric: 'load.avg1',
				value: '0.42',
				observed_at: '2026-10-17T09:15:04Z',
				collected_at: '2026-10-17T09:15:05Z',
				fresh_until: '2026-10-17T09:45:04Z',
				freshness: 'fresh',
				seq: 7,
				context_ref: 'observation:' + observationId
			},
			fields
		);
	}

	function findingRow(findingId, serverId, fields) {
		return Object.assign(
			{
				finding_id: findingId,
				server_id: serverId,
				application_id: null,
				observation_id: OBS_1,
				kind: 'process.zombies',
				dedup_key: 'count',
				severity: 'warning',
				summary: '3 zombie processes counted by the collector',
				detail: 'Reap or restart the parent of the listed zombies.',
				opened_at: '2026-10-17T09:15:06Z',
				updated_at: '2026-10-17T09:30:06Z',
				resolved_at: null,
				open: true,
				context_ref: 'finding:' + findingId
			},
			fields
		);
	}

	function alertRow(alertId, serverId, fields) {
		return Object.assign(
			{
				alert_id: alertId,
				server_id: serverId,
				origin: 'security.firewall',
				dedup_key: 'firewall',
				severity: 'critical',
				title: 'Firewall rule count changed',
				summary: 'nftables ruleset differs from the previous sample',
				context_ref: 'finding:' + FINDING_1,
				delivery_status: 'delivered',
				created_at: '2026-10-17T09:31:00Z',
				delivered_at: '2026-10-17T09:31:01Z',
				acknowledged_at: null,
				acked: false,
				finding_id: FINDING_1,
				run_id: null
			},
			fields
		);
	}

	function applicationRow(applicationId, serverId, fields) {
		return Object.assign(
			{
				application_id: applicationId,
				server_id: serverId,
				name: 'metrics-stack',
				origin: 'compose',
				compose_project: 'metrics',
				working_dir: '/srv/metrics',
				unit_name: null,
				external_working_dir: '/srv/metrics',
				external_config_files: ['/srv/metrics/compose.yaml'],
				external_environment_files: ['/srv/metrics/.env'],
				revision: 2,
				schema_revision: 1,
				sha256: 'a'.repeat(64),
				adopted_at: '2026-10-16T10:00:00Z',
				reconciled_at: '2026-10-16T10:00:00Z',
				stale: false
			},
			fields
		);
	}

	function runRow(runId, serverId, fields) {
		return Object.assign(
			{
				run_id: runId,
				server_id: serverId,
				parent_run_id: null,
				depth: 0,
				profile_revision: 1,
				prompt_revision: 1,
				model_profile: 'default',
				requested_grants: [],
				state: 'running',
				cancel_requested_at: null,
				submitted_at: '2026-10-17T09:00:00Z',
				updated_at: '2026-10-17T09:10:00Z'
			},
			fields
		);
	}

	function observationPage(serverId, rows) {
		return { server_id: serverId, observations: rows, limit: 10, offset: 0, total: rows.length };
	}
	function findingPage(serverId, rows) {
		return { server_id: serverId, findings: rows, limit: 10, offset: 0, total: rows.length };
	}
	function alertPage(serverId, rows) {
		return { server_id: serverId, alerts: rows, limit: 10, offset: 0, total: rows.length };
	}
	function applicationPage(serverId, rows) {
		return { server_id: serverId, applications: rows, limit: 50, offset: 0, total: rows.length };
	}
	function runPage(serverId, rows) {
		return { runs: rows, limit: 50, offset: 0, total: rows.length };
	}

	// --- the per-server durable truth (fresh module evaluation per test) ------

	const data = {};
	data[SERVER_A] = {
		observations: [
			observationRow(OBS_1, SERVER_A, {}),
			// The collector could not observe this target: an unknown reading,
			// never a zero and never an empty history.
			observationRow(OBS_2, SERVER_A, {
				source: 'security',
				method: 'command',
				quality: 'unknown',
				metric: null,
				value: null,
				observed_at: null,
				fresh_until: null,
				freshness: 'unknown',
				collected_at: '2026-10-17T09:20:05Z',
				seq: 8
			})
		],
		findings: [findingRow(FINDING_1, SERVER_A, {})],
		alerts: [
			alertRow(ALERT_1, SERVER_A, {}),
			alertRow(ALERT_2, SERVER_A, {
				severity: 'warning',
				title: 'Cron timers drifted <img src=x onerror="window.__maiXssFired = true">',
				summary: 'Two timers missed their window',
				context_ref: null,
				finding_id: null,
				created_at: '2026-10-17T09:40:00Z'
			})
		],
		applications: [applicationRow(APP_ADOPTED, SERVER_A, {})],
		runs: [runRow(RUN_ID, SERVER_A, {}), runRow(RUN_2, SERVER_A, { state: 'succeeded' })]
	};
	// Beta holds its OWN stale observation and nothing else: counts, alerts
	// and findings must never leak across the pinned scopes.
	data[SERVER_B] = {
		observations: [
			observationRow(OBS_B, SERVER_B, { freshness: 'stale', value: '0.90', seq: 3 })
		],
		findings: [],
		alerts: [],
		applications: [],
		runs: []
	};

	function evaluateRound(serverId) {
		return {
			server_id: serverId,
			evaluated: 2,
			outcomes: [
				{
					alert_id: ALERT_1,
					dedup_key: 'firewall',
					disposition: 'created',
					delivery: 'delivered',
					channels_attempted: ['web_inbox']
				},
				{
					alert_id: ALERT_2,
					dedup_key: 'timers',
					disposition: 'unchanged',
					delivery: 'skipped',
					channels_attempted: []
				}
			]
		};
	}

	// --- recorded calls + armed behavior -------------------------------------

	const calls = [];
	/**
	 * One-shot failure arms, consumed by the next call to their op: the slot
	 * is cleared at the call site (a helper that nulled its own parameter
	 * would leave the arm set forever).
	 */
	const arms = {
		observations: null,
		findings: null,
		inbox: null,
		apps: null,
		runs: null,
		ack: null,
		evaluate: null
	};
	let wrongScopeObservations = false;
	let foreignEvaluate = false;
	// Deferred gates survive consumption until release* consumes them —
	// nulling at arm time would strand the parked promise forever.
	let deferredObservations = null;
	let pendingObservations = null;

	function record(op, payload, args) {
		calls.push({ op: op, payload: payload, args: args });
	}

	function throwArm(name) {
		const failure = arms[name];
		if (failure) {
			arms[name] = null;
			throw new ApiError(failure.status, failure.code, failure.message);
		}
	}

	function deep(value) {
		return JSON.parse(JSON.stringify(value));
	}

	// Live stream sessions; the fake api closes over this list.
	const streams = [];

	const api = {
		async listObservations(serverId, page) {
			record('listObservations', page ? deep(page) : null, [serverId]);
			throwArm('observations');
			if (wrongScopeObservations) {
				// One hostile answer: a page naming the OTHER server. The panel
				// must refuse it before anything paints.
				wrongScopeObservations = false;
				return deep(observationPage(SERVER_B, data[SERVER_B].observations));
			}
			if (deferredObservations) {
				pendingObservations = deferredObservations;
				deferredObservations = null;
				const released = await pendingObservations.promise;
				return deep(released);
			}
			return deep(observationPage(serverId, data[serverId].observations));
		},
		async listFindings(serverId, query) {
			record('listFindings', query ? deep(query) : null, [serverId]);
			throwArm('findings');
			const rows = data[serverId].findings.filter((row) => query && query.open ? row.open : true);
			return deep(findingPage(serverId, rows));
		},
		async evaluateFindings(serverId) {
			record('evaluateFindings', null, [serverId]);
			throwArm('evaluate');
			if (foreignEvaluate) {
				foreignEvaluate = false;
				return deep(evaluateRound(SERVER_B));
			}
			return deep(evaluateRound(serverId));
		},
		async listNotifications(serverId, query) {
			record('listNotifications', query ? deep(query) : null, [serverId]);
			throwArm('inbox');
			const rows = data[serverId].alerts.filter(
				(row) => !(query && query.unacknowledged ? row.acked : false)
			);
			return deep(alertPage(serverId, rows));
		},
		async ackNotification(serverId, alertId) {
			record('ackNotification', null, [serverId, alertId]);
			throwArm('ack');
			const row = (data[serverId].alerts || []).find((candidate) => candidate.alert_id === alertId);
			if (!row) {
				throw new ApiError(404, 'not_found', 'no notice for this id in this server scope');
			}
			// The reviewed route: one idempotent stamp, nothing deleted.
			row.acked = true;
			row.acknowledged_at = ACK_STAMP;
			return { alert: deep(row) };
		},
		async listAdoptedApplications(serverId, page) {
			record('listAdoptedApplications', page ? deep(page) : null, [serverId]);
			throwArm('apps');
			return deep(applicationPage(serverId, data[serverId].applications));
		},
		async listRuns(serverId, page) {
			record('listRuns', page ? deep(page) : null, [serverId]);
			throwArm('runs');
			return deep(runPage(serverId, data[serverId].runs));
		},
		async streamRunEvents(runId, options) {
			const session = {
				runId: runId,
				cursor: options.cursor ?? null,
				aborted: false,
				open: true,
				onEvent: options.onEvent,
				resolve: null
			};
			streams.push(session);
			record('streamRunEvents', null, [runId, { cursor: session.cursor }]);
			const { promise, resolve } = Promise.withResolvers();
			session.resolve = resolve;
			if (options.signal.aborted) {
				session.aborted = true;
				session.open = false;
				resolve();
			} else {
				options.signal.addEventListener(
					'abort',
					() => {
						session.aborted = true;
						session.open = false;
						resolve();
					},
					{ once: true }
				);
			}
			await promise;
			session.open = false;
		}
	};

	// --- fail-closed seams (recorded openers; null fails closed) --------------

	const links = {
		openApplications: (serverId) => record('openApplications', null, [serverId]),
		openJobs: (serverId) => record('openJobs', null, [serverId])
	};
	const chat = {
		attach: (payload) => record('attach', deep(payload), [])
	};
	const services = overviewServices(api, links, chat);
	const servicesNoSeams = overviewServices(api, null, null);

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
		overviewEntry(),
		{
			panel_kind: 'fixture-notes',
			title: 'Notes',
			scope_types: ['server'],
			component: () => Promise.resolve(sibling)
		}
	]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	const registry = registryResult.value;
	const entryCheck = definePanelEntry(overviewEntry());
	if (!entryCheck.ok) throw new Error('feature entry rejected: ' + entryCheck.issue.code);

	// --- hosts ----------------------------------------------------------------
	let showA = $state(true);
	let showRun = $state(true);

	function descriptor(windowId, scope, z, kind) {
		return {
			schema_version: 1,
			window_id: windowId,
			panel_kind: kind ?? OVERVIEW_PANEL_KIND,
			scope,
			geometry: { x: 8, y: 8, width: 920, height: 640 },
			presentation: 'normal',
			z,
			title_override: null
		};
	}

	function onPanelError(windowId, panelKind, error) {
		console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error);
	}

	function checkCode(kind, scope) {
		const result = registry.checkScope(kind, scope);
		return result.ok ? 'ok' : result.issue.code;
	}

	const windowApi = {
		calls,
		streams,
		failNextObservations(failure) {
			arms.observations = failure;
		},
		failNextFindings(failure) {
			arms.findings = failure;
		},
		failNextInbox(failure) {
			arms.inbox = failure;
		},
		failNextApps(failure) {
			arms.apps = failure;
		},
		failNextRuns(failure) {
			arms.runs = failure;
		},
		failNextAck(failure) {
			arms.ack = failure;
		},
		failNextEvaluate(failure) {
			arms.evaluate = failure;
		},
		armWrongScopeObservations() {
			wrongScopeObservations = true;
		},
		armForeignEvaluate() {
			foreignEvaluate = true;
		},
		armDeferredObservations() {
			deferredObservations = Promise.withResolvers();
		},
		releaseObservations(pageView) {
			const entry = pendingObservations;
			pendingObservations = null;
			deferredObservations = null;
			if (entry) entry.resolve(JSON.parse(JSON.stringify(pageView)));
		},
		setRunState(runId, state) {
			for (const row of data[SERVER_A].runs) {
				if (row.run_id === runId) row.state = state;
			}
		},
		deliverEvent(seq) {
			for (let index = streams.length - 1; index >= 0; index -= 1) {
				const session = streams[index];
				if (session.open && session.onEvent) {
					session.onEvent({
						id: String(seq),
						event: null,
						data: JSON.stringify({
							seq: seq,
							run_id: session.runId,
							ts: '${EVENT_TS}',
							type: 'run.transition',
							v: 1,
							data: { state: 'running' }
						})
					});
					return;
				}
			}
			throw new Error('no open stream session to deliver to');
		},
		closeStream() {
			for (let index = streams.length - 1; index >= 0; index -= 1) {
				const session = streams[index];
				if (session.open && session.resolve) {
					session.resolve();
					return;
				}
			}
			throw new Error('no open stream session to close');
		},
		toggleHostA() {
			showA = !showA;
		},
		toggleHostRun() {
			showRun = !showRun;
		},
		registry: {
			kinds: Array.from(registry.knownPanelKinds()).sort(),
			scopeChecks: {
				serverScope: checkCode(OVERVIEW_PANEL_KIND, { kind: 'server', server_id: SERVER_A }),
				globalScope: checkCode(OVERVIEW_PANEL_KIND, { kind: 'global' }),
				pinnedRun: checkCode(OVERVIEW_PANEL_KIND, {
					kind: 'server',
					server_id: SERVER_A,
					object: { kind: 'run', id: RUN_ID }
				}),
				foreignObject: checkCode(OVERVIEW_PANEL_KIND, {
					kind: 'server',
					server_id: SERVER_A,
					object: { kind: 'document', id: '${RUN_2}' }
				}),
				unknownKind: checkCode('not-a-panel', { kind: 'server', server_id: SERVER_A })
			},
			defaultGeometry: registry.defaultGeometry(OVERVIEW_PANEL_KIND),
			minGeometry: registry.minGeometry(OVERVIEW_PANEL_KIND),
			instanceKey: singletonInstanceKey(OVERVIEW_PANEL_KIND, entryCheck.value, {
				kind: 'server',
				server_id: SERVER_A
			}),
			pinnedInstanceKey: singletonInstanceKey(OVERVIEW_PANEL_KIND, entryCheck.value, {
				kind: 'server',
				server_id: SERVER_A,
				object: { kind: 'run', id: RUN_ID }
			})
		}
	};
	// Expose the live session handles for teardown/close assertions.
	windowApi.streams = streams;

	window.__maiDashboardApi = windowApi;
</script>

<section data-testid="host-a" aria-label="Host A">
	<ComponentHost label="Host A">
		{#if showA}
			<PanelHost
				onpanelerror={onPanelError}
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
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_B}', { kind: 'server', server_id: SERVER_B }, 2)}
			{services}
		/>
	</ComponentHost>
</section>
<section data-testid="host-run" aria-label="Host pinned run">
	<ComponentHost label="Host pinned run">
		{#if showRun}
			<PanelHost
				onpanelerror={onPanelError}
				{registry}
				descriptor={descriptor('${WINDOW_RUN}', { kind: 'server', server_id: SERVER_A, object: { kind: 'run', id: '${RUN_ID}' } }, 3)}
				{services}
			/>
		{/if}
	</ComponentHost>
</section>
<section data-testid="host-sibling" aria-label="Host sibling">
	<ComponentHost label="Host sibling">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_SIBLING}', { kind: 'server', server_id: SERVER_B }, 4, 'fixture-notes')}
		/>
	</ComponentHost>
</section>
<section data-testid="host-global" aria-label="Host global">
	<ComponentHost label="Host global">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_GLOBAL}', { kind: 'global' }, 5)}
			{services}
		/>
	</ComponentHost>
</section>
<section data-testid="host-bare" aria-label="Host bare">
	<ComponentHost label="Host bare">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_BARE}', { kind: 'server', server_id: SERVER_A }, 6)}
		/>
	</ComponentHost>
</section>
<section data-testid="host-noseam" aria-label="Host without seams">
	<ComponentHost label="Host noseam">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_NOSEAM}', { kind: 'server', server_id: SERVER_A }, 7)}
			services={servicesNoSeams}
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

// ---------------------------------------------------------------------------
// Build the harness once per worker (same shape as the reviewed apps fixture)
// ---------------------------------------------------------------------------

/** Serves the virtual modules and the built lazy chunks; nothing hits disk. */
function harnessPlugin(sources: Record<string, string>): Plugin {
	return {
		name: 'mai-dashboard-harness',
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
let harnessJs = '';
let harnessCss = '';

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
			outDir: 'node_modules/.mai-dashboard-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'dashboard-[hash].js',
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
	pageErrors.length = 0;
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI dashboard fixture</title>
<link rel="stylesheet" href="${FIXTURE_URL}demo.css">
</head>
<body>
<div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"><\/script>
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
		pageErrors.push(error.stack ?? error.message);
		console.log(`[pageerror] ${error.stack ?? error.message}`);
	});
	page.on('console', (message) => {
		if (message.type() === 'error') console.log(`[console.error] ${message.text()}`);
	});
	await page.goto(FIXTURE_URL);
});

// ---------------------------------------------------------------------------
// Fixture-API boundary (serialized dispatch; same convention as apps spec)
// ---------------------------------------------------------------------------

/** Call one fixture-api method inside the page; args must be serializable. */
async function apiCall(page: Page, method: string, args: unknown[] = []): Promise<void> {
	const pair: [string, unknown[]] = [method, args];
	await page.evaluate(([name, callArgs]) => {
		const value = window.__maiDashboardApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		const table = value as unknown as Record<string, (...a: never[]) => void>;
		const fn = table[name];
		if (typeof fn !== 'function') throw new Error(`unknown fixture method ${name}`);
		fn.apply(value, callArgs as never[]);
	}, pair);
}

interface RecordedCall {
	op: string;
	payload?: unknown;
	args?: unknown[];
}

/** Snapshot the fixture's recorded calls, streams and precomputed registry. */
async function apiState(page: Page): Promise<{
	calls: RecordedCall[];
	streams: Array<{ runId: string; cursor: string | null; aborted: boolean }>;
	registry: DashboardWindowApi['registry'];
}> {
	return page.evaluate(() => {
		const value = window.__maiDashboardApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		return {
			calls: value.calls,
			streams: value.streams.map((session) => ({
				runId: session.runId,
				cursor: session.cursor,
				aborted: session.aborted
			})),
			registry: value.registry
		};
	});
}

/** Test-only recorded-wire boundary: every recorded call for one op. */
function callsOf(calls: RecordedCall[], op: string): RecordedCall[] {
	return calls.filter((call) => call.op === op);
}

/** Both server windows mounted (lazy chunk fetched through the real loader). */
async function serverWindowsMounted(page: Page): Promise<void> {
	await expect(page.getByTestId('host-a').getByTestId('overview-root')).toBeVisible();
	await expect(page.getByTestId('host-b').getByTestId('overview-root')).toBeVisible();
	await expect(page.getByTestId('host-a').getByTestId('freshness-counts')).toBeVisible();
	await expect(page.getByTestId('host-b').getByTestId('freshness-counts')).toBeVisible();
	expect(servedChunks.length).toBeGreaterThan(0);
}

// ---------------------------------------------------------------------------

test('overview summarizes the pinned server real pages beside a sibling and never crosses scopes', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	const hostB = page.getByTestId('host-b');
	await serverWindowsMounted(page);

	// The desktop holds the overview BESIDE another panel.
	await expect(page.getByTestId('host-sibling').getByTestId('sibling-panel')).toBeVisible();

	// Alpha: the producer-derived freshness tally over the latest page —
	// one fresh measured sample, one unknown reading (not a zero).
	await expect(hostA.getByTestId('freshness-fresh')).toHaveText('1 fresh');
	await expect(hostA.getByTestId('freshness-stale')).toHaveText('0 stale');
	await expect(hostA.getByTestId('freshness-unknown')).toHaveText('1 unknown');
	await expect(hostA.getByTestId('freshness-newest')).toHaveText('2026-10-17T09:20:05Z');
	const obsRows = hostA.getByTestId('observation-row');
	await expect(obsRows).toHaveCount(2);
	await expect(obsRows.first().getByTestId('row-observation-freshness')).toHaveText('fresh');
	await expect(obsRows.last().getByTestId('row-observation-quality')).toHaveText('unknown');
	await expect(obsRows.last().getByTestId('row-observation-metric')).toHaveText('no sample taken');

	// Findings: the producer's attention order; page-scoped severity tally.
	await expect(hostA.getByTestId('findings-critical')).toHaveText('0 critical');
	await expect(hostA.getByTestId('findings-warning')).toHaveText('1 warning');
	await expect(hostA.getByTestId('findings-info')).toHaveText('0 info');
	await expect(hostA.getByTestId('finding-row')).toHaveCount(1);
	await expect(hostA.getByTestId('row-finding-kind')).toHaveText('process.zombies');

	// Inbox: the unacknowledged attention set only.
	await expect(hostA.getByTestId('inbox-count')).toHaveText('2 of 2 unacknowledged (latest page).');
	await expect(hostA.getByTestId('notice-row')).toHaveCount(2);

	// Application/job state summaries + the live-off honesty (no pinned run).
	await expect(hostA.getByTestId('apps-total')).toHaveText('1 adopted applications');
	await expect(hostA.getByTestId('apps-stale')).toHaveText('0 with observed drift');
	await expect(hostA.getByTestId('runs-active')).toHaveText('1 active');
	await expect(hostA.getByTestId('runs-terminal')).toHaveText('1 finished');
	await expect(hostA.getByTestId('live-off')).toBeVisible();

	// Beta: its own stale observation, zero findings, zero notices —
	// nothing of Alpha's leaks across the pinned scopes.
	await expect(hostB.getByTestId('freshness-stale')).toHaveText('1 stale');
	await expect(hostB.getByTestId('freshness-fresh')).toHaveText('0 fresh');
	await expect(hostB.getByTestId('observation-row')).toHaveCount(1);
	await expect(hostB.getByTestId('findings-empty')).toBeVisible();
	await expect(hostB.getByTestId('inbox-empty')).toBeVisible();
	await expect(hostB.getByTestId('apps-total')).toHaveText('0 adopted applications');
	await expect(
		hostA.getByTestId('observation-row').filter({ hasText: SERVER_B })
	).toHaveCount(0);

	const { calls } = await apiState(page);
	// Every page read named its own window's pinned server.
	for (const op of [
		'listObservations',
		'listFindings',
		'listNotifications',
		'listAdoptedApplications',
		'listRuns'
	]) {
		const scopes = callsOf(calls, op).map((call) => call.args?.[0]);
		expect(scopes).toContain(SERVER_A);
		expect(scopes).toContain(SERVER_B);
		expect(scopes.every((scope) => scope === SERVER_A || scope === SERVER_B)).toBe(true);
	}
	// The inbox query is the attention query: unacknowledged only.
	const inboxQuery = callsOf(calls, 'listNotifications').find(
		(call) => call.args?.[0] === SERVER_A
	)?.payload as { unacknowledged?: boolean };
	expect(inboxQuery.unacknowledged).toBe(true);
	// Exactly the run-pinned window subscribed: the plain server windows
	// never open a stream (there is no pinned run to watch).
	expect(callsOf(calls, 'streamRunEvents')).toHaveLength(1);
	expect(callsOf(calls, 'streamRunEvents')[0].args?.[0]).toBe(RUN_ID);
});

test('the feature entry composes into the typed registry with scope rules, geometry and singleton keys', async ({
	page
}) => {
	await serverWindowsMounted(page);
	const { registry } = await apiState(page);

	// Registry membership: exactly the feature entry plus the benign sibling.
	expect(registry.kinds).toEqual(['fixture-notes', 'overview']);

	// Scope rules (ADR §3.2): server-scoped only; a pinned run is allowed,
	// a foreign object kind and a global mount are refused with stable codes.
	expect(registry.scopeChecks.serverScope).toBe('ok');
	expect(registry.scopeChecks.pinnedRun).toBe('ok');
	expect(registry.scopeChecks.globalScope).toBe('scope_kind_not_allowed');
	expect(registry.scopeChecks.foreignObject).toBe('scope_object_kind_not_allowed');
	expect(registry.scopeChecks.unknownKind).toBe('unknown_panel_kind');

	// Geometry: the entry defaults and clamps are the registry's values.
	expect(registry.defaultGeometry).toEqual({ x: 40, y: 40, width: 920, height: 640 });
	expect(registry.minGeometry).toEqual({ x: 0, y: 0, width: 520, height: 380 });

	// Singleton per full pinned scope: a plain and a run-pinned overview of
	// the same server are distinct scope keys.
	expect(registry.instanceKey).not.toBeNull();
	expect(registry.pinnedInstanceKey).not.toBeNull();
	expect(registry.instanceKey).not.toBe(registry.pinnedInstanceKey);
});

test('acknowledge stamps the notice in place and honest failures stay visible', async ({ page }) => {
	const hostA = page.getByTestId('host-a');
	await serverWindowsMounted(page);

	// Ack the first notice: the idempotent stamp the route answers lands,
	// the attention page reloads and the acked notice drops out of the
	// unacknowledged set (the source evidence stays — the route deletes
	// nothing; the inbox here is the unacknowledged attention page).
	await hostA
		.getByTestId('notice-row')
		.filter({ hasText: 'Firewall rule count changed' })
		.getByTestId('ack-button')
		.click();
	await expect(hostA.getByTestId('notice-row')).toHaveCount(1);
	await expect(hostA.getByTestId('notice-row').filter({ hasText: 'Cron timers drifted' })).toHaveCount(1);
	await expect(hostA.getByTestId('notice-row').filter({ hasText: 'Firewall rule count changed' })).toHaveCount(0);
	await expect(hostA.getByTestId('inbox-count')).toHaveText('1 of 1 unacknowledged (latest page).');
	// No fake acknowledged state is ever rendered for an unacked notice.
	await expect(hostA.getByTestId('notice-acked')).toHaveCount(0);

	const { calls } = await apiState(page);
	const ackCalls = callsOf(calls, 'ackNotification');
	expect(ackCalls).toHaveLength(1);
	expect(ackCalls[0].args).toEqual([SERVER_A, ALERT_1]);
	// The stamp reloads the inbox (the acked row drops from the page).
	const inboxCalls = callsOf(calls, 'listNotifications').filter(
		(call) => call.args?.[0] === SERVER_A
	);
	expect(inboxCalls.length).toBeGreaterThanOrEqual(2);

	// A refused ack renders the envelope's answer — never a fake stamp.
	await apiCall(page, 'failNextAck', [{ status: 404, code: 'not_found', message: 'no notice for this id in this server scope' }]);
	const secondRow = hostA.getByTestId('notice-row').filter({ hasText: 'Cron timers drifted' });
	await secondRow.getByTestId('ack-button').click();
	await expect(hostA.getByTestId('ack-problem')).toBeVisible();
	await expect(hostA.getByTestId('ack-problem')).toContainText('Nothing is stored under this id and scope.');
	await expect(secondRow).toHaveAttribute('data-acked', 'false');
});

test('re-evaluation renders the evaluator own round for the pinned server through the evidence drawer', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await serverWindowsMounted(page);

	// Select the finding: the reviewed drawer is the one detail surface.
	await hostA.getByTestId('finding-open').click();
	const drawer = hostA.getByTestId('overview-drawer');
	await expect(drawer.getByTestId('evidence-finding')).toBeVisible();
	await expect(drawer.getByTestId('finding-id')).toHaveText(FINDING_1);

	// Re-evaluate from the section head: the REAL evaluator's round comes
	// back and renders through the drawer contract (server-checked).
	await hostA.getByTestId('evaluate-run-button').click();
	await expect(drawer.getByTestId('evaluate-summary')).toHaveText(
		'Evaluated 2 open finding(s) for this server.'
	);
	await expect(drawer.getByTestId('evaluate-outcome')).toHaveCount(2);
	await expect(drawer.getByTestId('evaluate-outcome-dedup').first()).toHaveText('firewall');
	await expect(drawer.getByTestId('evaluate-outcome-disposition').first()).toHaveText('created');

	// The evidence attach hand-off: only the bounded context reference.
	await drawer.getByTestId('finding-attach').click();
	const { calls } = await apiState(page);
	const evaluateCalls = callsOf(calls, 'evaluateFindings');
	expect(evaluateCalls).toEqual([{ op: 'evaluateFindings', payload: null, args: [SERVER_A] }]);
	const attachCalls = callsOf(calls, 'attach');
	expect(attachCalls).toHaveLength(1);
	expect(attachCalls[0].payload).toEqual({
		server_id: SERVER_A,
		kind: 'finding',
		context_ref: `finding:${FINDING_1}`
	});

	// A round naming ANOTHER server is refused by the panel guard — the
	// pinned server's previous round stays the only rendered one.
	await apiCall(page, 'armForeignEvaluate');
	await hostA.getByTestId('evaluate-run-button').click();
	await expect(hostA.getByTestId('evaluate-problem')).toContainText(
		'names a different server than this window'
	);
	await expect(drawer.getByTestId('evaluate-summary')).toHaveText(
		'Evaluated 2 open finding(s) for this server.'
	);
});

test('the pinned run stream drives durable refreshes, dedupes replay and releases on teardown', async ({
	page
}) => {
	const hostRun = page.getByTestId('host-run');
	await serverWindowsMounted(page);
	await expect(hostRun.getByTestId('overview-root')).toBeVisible();

	const observedCalls = async (): Promise<number> => {
		const { calls } = await apiState(page);
		return callsOf(calls, 'listObservations').filter((call) => call.args?.[0] === SERVER_A)
			.length;
	};
	const baseline = await observedCalls();
	let state = await apiState(page);

	// The one reviewed stream, subscribed once with an empty cursor.
	expect(state.streams).toHaveLength(1);
	expect(state.streams[0]).toMatchObject({ runId: RUN_ID, cursor: null, aborted: false });
	await expect(hostRun.getByTestId('overview-root')).toHaveAttribute('data-live', 'connecting');

	// Frame 5: accepted → one debounced durable refresh round, status live.
	await apiCall(page, 'deliverEvent', [5]);
	await expect
		.poll(observedCalls, { timeout: 5_000 })
		.toBe(baseline + 1);
	await expect(hostRun.getByTestId('overview-root')).toHaveAttribute('data-live', 'live');
	await expect(hostRun.getByTestId('live-status')).toHaveText('stream live');
	await expect(hostRun.getByTestId('live-delivered')).toHaveText('· 1 change frames');

	// Frame 5 replayed: the watermark dedupes — no second refresh round.
	await apiCall(page, 'deliverEvent', [5]);
	await page.waitForTimeout(400);
	expect(await observedCalls()).toBe(baseline + 1);
	await expect(hostRun.getByTestId('live-delivered')).toHaveText('· 1 change frames');

	// Frame 6: accepted → exactly one more refresh round.
	await apiCall(page, 'deliverEvent', [6]);
	await expect.poll(observedCalls, { timeout: 5_000 }).toBe(baseline + 2);
	await expect(hostRun.getByTestId('live-delivered')).toHaveText('· 2 change frames');

	// The server's bounded close: the loop reconnects FROM the watermark.
	await apiCall(page, 'closeStream');
	await expect(hostRun.getByTestId('overview-root')).toHaveAttribute('data-live', 'reconnecting');
	await expect.poll(async () => (await apiState(page)).streams.length, { timeout: 5_000 }).toBe(2);
	state = await apiState(page);
	expect(state.streams[1]).toMatchObject({ runId: RUN_ID, cursor: '6', aborted: false });

	// The stream is read-only: frames never acknowledge or evaluate anything.
	const { calls } = await apiState(page);
	expect(callsOf(calls, 'ackNotification')).toHaveLength(0);
	expect(callsOf(calls, 'evaluateFindings')).toHaveLength(0);

	// Hide/close releases the subscription (the $effect teardown path).
	await apiCall(page, 'toggleHostRun');
	await expect(hostRun.getByTestId('overview-root')).toHaveCount(0);
	await expect.poll(async () => (await apiState(page)).streams, { timeout: 5_000 }).toEqual([
		{ runId: RUN_ID, cursor: null, aborted: true },
		{ runId: RUN_ID, cursor: '6', aborted: true }
	]);
});

test('a terminal watched run closes the stream for good instead of reconnecting forever', async ({
	page
}) => {
	const hostRun = page.getByTestId('host-run');
	await serverWindowsMounted(page);
	await expect(hostRun.getByTestId('overview-root')).toBeVisible();

	// The durable read reports the run terminal; the next stream close then
	// ends the loop — nothing new can arrive (C05 rule 2).
	await apiCall(page, 'setRunState', [RUN_ID, 'succeeded']);
	await apiCall(page, 'deliverEvent', [5]);
	await expect.poll(async () => {
		const { calls } = await apiState(page);
		return callsOf(calls, 'listRuns').filter((call) => call.args?.[0] === SERVER_A).length;
	}, { timeout: 5_000 }).toBeGreaterThanOrEqual(2);
	await expect(hostRun.getByTestId('pinned-run-state')).toHaveText('succeeded');

	await apiCall(page, 'closeStream');
	await expect(hostRun.getByTestId('overview-root')).toHaveAttribute('data-live', 'closed');
	await expect(hostRun.getByTestId('live-status')).toHaveText('stream closed (run finished)');
	await page.waitForTimeout(1_200);
	const { streams } = await apiState(page);
	expect(streams).toHaveLength(1);
	expect(streams[0]).toMatchObject({ runId: RUN_ID, aborted: true });
});

test('loading, empty and offline states are truthful and never all-green', async ({ page }) => {
	const hostA = page.getByTestId('host-a');
	const hostB = page.getByTestId('host-b');
	await serverWindowsMounted(page);

	// Loading: the parked observation page shows an honest loading state —
	// no counts, no rows, no fabricated zeros.
	await apiCall(page, 'armDeferredObservations');
	await hostA.getByTestId('freshness-refresh').click();
	await expect(hostA.getByTestId('freshness-loading')).toBeVisible();
	await expect(hostA.getByTestId('freshness-counts')).toHaveCount(0);
	await expect(hostA.getByTestId('observation-row')).toHaveCount(0);

	// Empty: an empty page is an empty history — visibly not an error and
	// not an unknown reading.
	await apiCall(page, 'releaseObservations', [
		{ server_id: SERVER_A, observations: [], limit: 10, offset: 0, total: 0 }
	]);
	await expect(hostA.getByTestId('freshness-empty')).toBeVisible();
	await expect(hostA.getByTestId('freshness-counts')).toHaveCount(0);

	// Offline: a 503 from the monitoring surface renders the unavailable
	// state with its stable code — never all-green. The manual refresh is
	// the failing call (the next listRuns once, then the arm is consumed).
	await apiCall(page, 'failNextRuns', [{ status: 503, code: 'unavailable', message: 'worker pool exhausted' }]);
	await hostA.getByTestId('state-refresh').click();
	await expect(hostA.getByTestId('state-error')).toHaveAttribute('data-error-code', 'unavailable');
	await expect(hostA.getByTestId('state-error')).toContainText(
		'The monitoring service is unavailable right now; nothing was changed.'
	);
	// The retry reloads the durable state: the section recovers.
	await hostA.getByTestId('state-retry').click();
	await expect(hostA.getByTestId('state-error')).toHaveCount(0);
	await expect(hostA.getByTestId('apps-total')).toHaveText('1 adopted applications');

	// Beta still shows its own ready summaries: one broken section must not
	// blank the sibling panels (error containment, ADR §7.2).
	await expect(hostB.getByTestId('freshness-stale')).toHaveText('1 stale');
	expect(pageErrors).toEqual([]);
});

test('negative control: an answer naming another server is refused value-free before it can paint', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await serverWindowsMounted(page);

	// Arm exactly one hostile answer: an observation page naming SERVER_B.
	await apiCall(page, 'armWrongScopeObservations');
	await hostA.getByTestId('freshness-refresh').click();

	await expect(hostA.getByTestId('freshness-error')).toBeVisible();
	await expect(hostA.getByTestId('freshness-error')).toHaveAttribute(
		'data-error-code',
		'scope_mismatch'
	);
	await expect(hostA.getByTestId('freshness-error')).toContainText(
		'names a different server than this window'
	);
	// Nothing from the foreign page painted: Beta's rows/counts stay absent.
	await expect(hostA.getByTestId('observation-row')).toHaveCount(0);
	await expect(hostA.getByTestId('freshness-counts')).toHaveCount(0);

	// The next refresh is honest again (the arm was consumed once).
	await hostA.getByTestId('freshness-retry').click();
	await expect(hostA.getByTestId('freshness-counts')).toBeVisible();
	await expect(hostA.getByTestId('observation-row')).toHaveCount(2);
	expect(pageErrors).toEqual([]);
});

test('negative control: a stale response of a released host never paints and remount starts clean', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await serverWindowsMounted(page);

	// Park a response, then release the host content before it arrives.
	await apiCall(page, 'armDeferredObservations');
	await hostA.getByTestId('freshness-refresh').click();
	await expect(hostA.getByTestId('freshness-loading')).toBeVisible();
	await apiCall(page, 'toggleHostA');
	await expect(hostA.getByTestId('overview-root')).toHaveCount(0);
	await apiCall(page, 'releaseObservations', [
		{
			server_id: SERVER_A,
			observations: [
				{
					observation_id: '${OBS_2}',
					server_id: '${SERVER_A}',
					application_id: null,
					source: 'process',
					method: 'command',
					quality: 'unknown',
					metric: null,
					value: null,
					observed_at: null,
					collected_at: '2026-10-17T09:20:05Z',
					fresh_until: null,
					freshness: 'unknown',
					seq: 99,
					context_ref: 'observation:${OBS_2}'
				}
			],
			limit: 10,
			offset: 0,
			total: 1
		}
	]);
	await page.waitForTimeout(300);

	// The released payload resolved into a disposed instance: nothing
	// painted, no error escaped. A fresh mount starts from its own loads.
	await apiCall(page, 'toggleHostA');
	await expect(hostA.getByTestId('overview-root')).toBeVisible();
	await expect(hostA.getByTestId('freshness-counts')).toBeVisible({ timeout: 5_000 });
	await expect(hostA.getByTestId('observation-row')).toHaveCount(2);
	// The stale unknown row (seq 99) of the released round never appeared.
	await expect(hostA.getByTestId('observation-row').filter({ hasText: '99' })).toHaveCount(0);
	expect(pageErrors).toEqual([]);
});

test('negative control: hostile producer text stays inert in rows, inbox and drawer', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await serverWindowsMounted(page);

	// The hostile strings are visible as literal text…
	await expect(
		hostA.getByTestId('notice-row').filter({ hasText: '<img src=x' })
	).toHaveCount(1);
	// …and as escaped text nodes only: no element was ever built.
	const root = hostA.getByTestId('overview-root');
	expect(await root.locator('script').count()).toBe(0);
	expect(await root.locator('img').count()).toBe(0);
	expect(await root.locator('iframe').count()).toBe(0);

	// The drawer inherits the same inertness for finding guidance.
	await hostA.getByTestId('finding-open').click();
	const drawer = hostA.getByTestId('overview-drawer');
	await expect(drawer.getByTestId('evidence-finding')).toBeVisible();
	expect(await drawer.locator('script').count()).toBe(0);
	expect(await drawer.locator('img').count()).toBe(0);

	const fired = await page.evaluate(() => window.__maiXssFired);
	expect(fired).toBeUndefined();
	expect(pageErrors).toEqual([]);
});

test('negative control: unwired seams fail closed and links stay recorded hand-offs', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	const hostNoseam = page.getByTestId('host-noseam');
	await serverWindowsMounted(page);
	await expect(hostNoseam.getByTestId('overview-root')).toBeVisible();

	// Without the links seam: a visible note, no fake navigation buttons.
	await expect(hostNoseam.getByTestId('links-note')).toContainText('not wired');
	await expect(hostNoseam.getByTestId('apps-link')).toHaveCount(0);
	await expect(hostNoseam.getByTestId('jobs-link')).toHaveCount(0);

	// Without the chat seam the attach control never renders (never a
	// silent no-op), and nothing records a hand-off.
	await hostNoseam.getByTestId('observation-open').first().click();
	await expect(hostNoseam.getByTestId('overview-drawer').getByTestId('evidence-observation')).toBeVisible();
	await expect(hostNoseam.getByTestId('overview-drawer').getByTestId('observation-attach')).toHaveCount(0);

	// With the seams: the openers are recorded hand-offs with the pinned
	// scope, and the drawer hands over exactly the bounded reference.
	await hostA.getByTestId('apps-link').click();
	await hostA.getByTestId('jobs-link').click();
	await hostA.getByTestId('observation-open').first().click();
	await hostA
		.getByTestId('overview-drawer')
		.getByTestId('observation-attach')
		.click();

	const { calls } = await apiState(page);
	expect(callsOf(calls, 'openApplications')).toEqual([
		{ op: 'openApplications', payload: null, args: [SERVER_A] }
	]);
	expect(callsOf(calls, 'openJobs')).toEqual([
		{ op: 'openJobs', payload: null, args: [SERVER_A] }
	]);
	expect(callsOf(calls, 'attach')).toEqual([
		{
			op: 'attach',
			payload: { server_id: SERVER_A, kind: 'observation', context_ref: `observation:${OBS_1}` },
			args: []
		}
	]);
	expect(pageErrors).toEqual([]);
});

test('negative control: global scope or missing services fail closed with no unscoped fetch', async ({
	page
}) => {
	await serverWindowsMounted(page);

	// A global mount contradicts the entry contract: the REGISTRY refuses it
	// at opening (ADR §7.2), so the host shows the host's unavailable view
	// with the stable scope reason — the panel body never mounts.
	const hostGlobal = page.getByTestId('host-global');
	await expect(hostGlobal.getByTestId('panel-unavailable')).toBeVisible();
	await expect(hostGlobal.getByTestId('panel-unavailable')).toHaveAttribute(
		'data-reason',
		'scope_kind_not_allowed'
	);

	// A bare host (no services bag) passes the scope check and the panel
	// itself fails closed on the missing services.
	const hostBare = page.getByTestId('host-bare');
	await expect(hostBare.getByTestId('overview-config-error')).toBeVisible();

	// No panel instance ever fetched without naming a server scope: every
	// recorded list call carries an explicit UUID target.
	const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
	const { calls } = await apiState(page);
	for (const op of [
		'listObservations',
		'listFindings',
		'listNotifications',
		'listAdoptedApplications',
		'listRuns'
	]) {
		for (const call of callsOf(calls, op)) {
			expect(UUID_RE.test(String(call.args?.[0]))).toBe(true);
		}
	}
	expect(pageErrors).toEqual([]);
});
