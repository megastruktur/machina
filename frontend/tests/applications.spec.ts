import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-08-web-apps — real-browser proofs for the application inventory panel.
 *
 * The harness compiles the REAL feature panel (`ApplicationInventoryPanel.svelte`),
 * the REAL feature entry (`08-web-apps.ts`), the REAL panel host/registry and the
 * shared UI primitives into a throwaway fixture app served from virtual modules —
 * nothing hits disk and no real backend exists. The fixture API mirrors the
 * reviewed producers 1:1 (the `08-app-api` applications surface: adopted page,
 * read-only discovery, explicit adoption, non-destructive revoke); recorded calls
 * and pre-armed responses are the test-observed boundary (the reviewed `01-web`
 * component-browser fixture convention, not an imaginary backend).
 *
 * Covered acceptance surfaces: catalogue/inventory distinguishing discovered,
 * adopted and pending applications with exact server identity; explicit adoption
 * preserving the external deployment paths + canonical manifest with the record
 * document linked through the docs seam; lifecycle install/update/remove as
 * LINKS to separately implemented surfaces (never reimplemented, fail closed
 * without the seam); scope pinning, stale-response immunity, inert hostile
 * content, revision-checked revoke, fail-closed bags/scopes.
 */

const FIXTURE_URL = '/mai-apps-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-apps-app.svelte';
const ENTRY_ID = 'virtual:mai-apps-entry.js';

const PANEL = `${FRONTEND_ROOT}src/lib/panels/ApplicationInventoryPanel.svelte`;
const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/08-web-apps.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
const APP_ADOPTED = '11111111-1111-4222-8333-444455556666';
const APP_NEW = '22222222-2222-4222-8333-444455556666';
const APP_BETA = '33333333-3333-4222-8333-444455556666';
const INTENT_ID = '44444444-4444-4222-8333-444455556666';
const RECORD_DOC_ID = '55555555-5555-4222-8333-444455556666';
const WINDOW_A = '7a7a7a7a-8c9d-4e8a-8a9b-4c5d6e7f8a9c';
const WINDOW_B = '8b8b8b8b-8c9d-4e8a-8a9b-4c5d6e7f8a9d';
const WINDOW_PINNED = '9c9c9c9c-9999-4222-8333-444455556666';
const WINDOW_GLOBAL = '0d0d0d0d-9999-4222-8333-444455556666';
const WINDOW_BARE = '1e1e1e1e-9999-4222-8333-444455556666';
const WINDOW_NOSEAM = '2f2f2f2f-9999-4222-8333-444455556666';
const WINDOW_SIBLING = '3a3a3a3a-9999-4222-8333-444455556666';

/** Untruncated runtime-failure receipt: a page error is a harness defect. */
const pageErrors: string[] = [];

type Fail = { status: number; code: string; message: string };

interface AppsWindowApi {
	calls: Array<{ op: string; payload?: unknown; args?: unknown[] }>;
	failNextList(failure: Fail | null): void;
	failNextDiscover(failure: Fail | null): void;
	failNextAdopt(failure: Fail | null): void;
	failNextRevoke(failure: Fail | null): void;
	armDeferredDiscover(): void;
	releaseDiscover(view: unknown): void;
	armDeferredList(): void;
	releaseList(pageView: unknown): void;
	armDeferredAdopt(): void;
	releaseAdopt(result: unknown): void;
	armRecordDoc(doc: unknown | null): void;
	setRevision(applicationId: string, revision: number): void;
	toggleHostA(): void;
	registry: {
		kinds: string[];
		scopeChecks: {
			serverScope: string;
			globalScope: string;
			unknownKind: string;
			pinnedObject: string;
			foreignObject: string;
		};
		defaultGeometry: unknown;
		minGeometry: unknown;
		instanceKey: string | null;
		pinnedInstanceKey: string | null;
	};
}

declare global {
	interface Window {
		__maiAppsApi?: AppsWindowApi;
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
	import { APPLICATIONS_PANEL_KIND, applicationsEntry, applicationsServices } from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const SERVER_B = '${SERVER_B}';
	const APP_ADOPTED = '${APP_ADOPTED}';
	const APP_NEW = '${APP_NEW}';
	const APP_BETA = '${APP_BETA}';
	const INTENT_ID = '${INTENT_ID}';
	const RECORD_DOC_ID = '${RECORD_DOC_ID}';

	let tick = 0;

	// --- adopted records (the managed truth, projected page) -------------------

	function makeRecord(applicationId, serverId, name, fields) {
		return Object.assign(
			{
				application_id: applicationId,
				server_id: serverId,
				name: name,
				origin: 'compose',
				compose_project: null,
				working_dir: null,
				unit_name: null,
				external_working_dir: null,
				external_config_files: [],
				external_environment_files: [],
				revision: 1,
				schema_revision: 1,
				sha256: 'a'.repeat(64),
				adopted_at: '2026-10-16T10:00:00Z',
				reconciled_at: '2026-10-16T10:00:00Z',
				stale: false
			},
			fields
		);
	}

	const adopted = {};

	function seedRecords() {
		adopted[SERVER_A] = {};
		adopted[SERVER_A][APP_ADOPTED] = makeRecord(APP_ADOPTED, SERVER_A, 'metrics-stack', {
			compose_project: 'metrics',
			working_dir: '/srv/metrics',
			external_working_dir: '/srv/metrics',
			external_config_files: ['/srv/metrics/compose.yaml'],
			external_environment_files: ['/srv/metrics/.env'],
			revision: 2
		});
		adopted[SERVER_B] = {};
		adopted[SERVER_B][APP_BETA] = makeRecord(APP_BETA, SERVER_B, 'beta-agent', {
			compose_project: 'agent',
			working_dir: '/opt/agent',
			external_working_dir: '/opt/agent',
			revision: 5
		});
	}

	seedRecords();

	function managedRef(serverId, matcher) {
		const scope = adopted[serverId] || {};
		for (const rec of Object.values(scope)) {
			if (matcher(rec)) {
				return { managed: true, application_id: rec.application_id };
			}
		}
		return { managed: false, application_id: null };
	}

	// --- discovery snapshots (read-only lanes; safe view only) -----------------

	function observedCompose(serverId, project, workingDir, services, containers) {
		const ref = managedRef(serverId, (rec) => rec.compose_project === project);
		return {
			origin: 'compose',
			managed: ref.managed,
			application_id: ref.application_id,
			detail: {
				kind: 'compose_project',
				project_name: project,
				working_dir: workingDir,
				config_files: [workingDir + '/compose.yaml'],
				environment_files: [workingDir + '/.env'],
				services: services,
				containers: containers
			}
		};
	}

	function observedStandalone(serverId, containerId, name, image, state) {
		const ref = managedRef(serverId, (rec) => rec.unit_name === name);
		return {
			origin: 'docker_container',
			managed: ref.managed,
			application_id: ref.application_id,
			detail: {
				kind: 'standalone_container',
				container: {
					container_id: containerId,
					name: name,
					image: image,
					state: state,
					status: 'Up 3 days',
					ports: [],
					compose_service: null
				}
			}
		};
	}

	function observedUnit(serverId, unit, loadState, activeState) {
		const ref = managedRef(serverId, (rec) => rec.unit_name === unit);
		return {
			origin: 'systemd_unit',
			managed: ref.managed,
			application_id: ref.application_id,
			detail: {
				kind: 'systemd_unit',
				unit: unit,
				unit_file_state: 'enabled',
				load_state: loadState,
				active_state: activeState,
				sub_state: activeState === 'active' ? 'running' : null,
				description: 'hostile description <img src=x onerror="window.__maiXssFired = true">'
			}
		};
	}

	function snapshot(serverId) {
		tick += 1;
		const stamp = '2026-10-17T08:00:0' + tick + 'Z';
		if (serverId === SERVER_A) {
			return {
				server_id: serverId,
				observed_at: stamp,
				applications: [
					// The adopted metrics project, tagged managed from the recorded
					// selector (exact reported values, this server's records only).
					observedCompose(SERVER_A, 'metrics', '/srv/metrics', ['prometheus', 'grafana'], [
						{ container_id: 'c0', name: 'metrics-prometheus', image: 'prom/prometheus', state: 'running', status: 'Up', ports: [], compose_service: 'prometheus' }
					]),
					observedCompose(SERVER_A, 'blog', '/srv/blog', ['web', 'db'], [
						{ container_id: 'c1', name: 'blog-web', image: 'nginx:1.27', state: 'running', status: 'Up', ports: [], compose_service: 'web' },
						{ container_id: 'c2', name: 'blog-db', image: 'postgres:17', state: 'running', status: 'Up', ports: [], compose_service: 'db' }
					]),
					observedStandalone(SERVER_A, 'c9c9c9c9', 'legacy-cron', 'busybox:1.36', 'running'),
					observedUnit(SERVER_A, 'nginx.service', 'loaded', 'active')
				],
				listen_ports: [
					{ address: '0.0.0.0', port: 80 },
					{ address: '0.0.0.0', port: 443 }
				],
				lanes: [
					{ lane: 'docker', state: 'observed', detail: 'docker ps parsed; 2 compose labels', truncated: false, malformed_lines: 0 },
					{ lane: 'systemd', state: 'observed', detail: 'unit list read <img src=x onerror="window.__maiXssFired = true">', truncated: false, malformed_lines: 1 },
					{ lane: 'listen_ports', state: 'permission_denied', detail: 'ss -tln refused by policy', truncated: false, malformed_lines: 0 }
				]
			};
		}
		return {
			server_id: serverId,
			observed_at: stamp,
			applications: [
				// The adopted agent project, tagged managed on THIS server's records.
				observedCompose(SERVER_B, 'agent', '/opt/agent', ['agent'], [
					{ container_id: 'd0', name: 'agent-web', image: 'agent:2', state: 'running', status: 'Up', ports: [], compose_service: 'agent' }
				]),
				// Conflicting fixture pair: the SAME compose project name as Alpha's
				// 'blog' at a DIFFERENT path on a DIFFERENT server — managed tagging
				// and every call must stay on the pinned server's side.
				observedCompose(SERVER_B, 'blog', '/opt/blog-beta', ['web'], [
					{ container_id: 'd1', name: 'beta-web', image: 'caddy:2', state: 'running', status: 'Up', ports: [], compose_service: 'web' }
				]),
				observedUnit(SERVER_B, 'fail2ban.service', 'loaded', 'active')
			],
			listen_ports: [{ address: '0.0.0.0', port: 22 }],
			lanes: [
				{ lane: 'docker', state: 'observed', detail: 'docker ps parsed; 2 compose labels', truncated: false, malformed_lines: 0 },
				{ lane: 'systemd', state: 'observed', detail: 'unit list read', truncated: false, malformed_lines: 0 },
				{ lane: 'listen_ports', state: 'observed', detail: 'ss -tln parsed', truncated: false, malformed_lines: 0 }
			]
		};
	}

	// --- recorded calls + armed behavior -------------------------------------

	const calls = [];
	/**
	 * One-shot failure arms, consumed by the next call to their op: arming
	 * the list slot fails exactly ONE listAdopted, then the slot is null
	 * again — the slot itself is cleared at the call site (a helper that
	 * nulled its own parameter would leave failNext armed forever).
	 */
	const arms = { list: null, discover: null, adopt: null, revoke: null };
	// Gate lifecycle: arm* parks the NEXT call; the call moves the gate into
	// the pending* slot where release* finds it — the gate must survive
	// consumption, or the released payload never reaches the panel and the
	// stale-painting assertions turn vacuously green (the F05 test-double trap
	// caught by the docs harness mutation M2).
	let deferredDiscover = null;
	let pendingDiscover = null;
	let deferredList = null;
	let pendingList = null;
	let deferredAdopt = null;
	let pendingAdopt = null;
	let recordDocArm = null;

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

	const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

	const api = {
		async listAdopted(serverId, page) {
			record('listAdopted', page ? deep(page) : null, [serverId]);
			throwArm('list');
			if (deferredList) {
				// Transfer first; the slot stays SET until releaseList consumes it.
				pendingList = deferredList;
				deferredList = null;
				const page = await pendingList.promise;
				return deep(page);
			}
			const all = Object.values(adopted[serverId] || {});
			return {
				server_id: serverId,
				applications: deep(all),
				limit: (page && page.limit) || 50,
				offset: (page && page.offset) || 0,
				total: all.length
			};
		},
		async discover(serverId) {
			record('discover', null, [serverId]);
			throwArm('discover');
			if (deferredDiscover) {
				pendingDiscover = deferredDiscover;
				deferredDiscover = null;
				const view = await pendingDiscover.promise;
				return deep(view);
			}
			return deep(snapshot(serverId));
		},
		async adopt(serverId, payload) {
			record('adopt', deep(payload), [serverId]);
			throwArm('adopt');
			if (deferredAdopt) {
				pendingAdopt = deferredAdopt;
				deferredAdopt = null;
			}
			const resolvedId =
				typeof payload.application_id === 'string' && payload.application_id.trim() !== ''
					? payload.application_id.trim()
					: APP_NEW;
			if (!UUID_RE.test(resolvedId)) {
				throw new ApiError(422, 'validation_failed', 'application_id must be a canonical UUID string');
			}
			// Cross-server identity: a live id under ANOTHER server is refused
			// (adoption never relocates an identity across servers).
			for (const [scopeId, scope] of Object.entries(adopted)) {
				if (scopeId !== serverId && scope[resolvedId]) {
					throw new ApiError(409, 'conflict', 'cross-server identity conflict');
				}
			}
			const live = (adopted[serverId] || {})[resolvedId];
			if (live) {
				// The Service Map re-adoption path: the SAME identity ships the
				// observed revision and the producer reconciles the metadata
				// update (category/description/instructions); anything else is
				// the honest stale/duplicate conflict.
				if (payload.expected_revision !== live.revision) {
					throw new ApiError(409, 'conflict', 'stale revision or canonical state conflict');
				}
				live.name = payload.name;
				live.revision = live.revision + 1;
				live.category = payload.category ?? live.category ?? 'system';
				live.description = payload.description ?? live.description ?? '';
				if (payload.instructions) live.instructions = deep(payload.instructions);
				const updated = {
					intent_id: INTENT_ID,
					application_id: resolvedId,
					server_id: serverId,
					outcome: 'applied',
					revision: live.revision,
					sha256: live.sha256,
					application: deep(live),
					record_doc: { status: 'documented', doc_id: RECORD_DOC_ID }
				};
				if (pendingAdopt) {
					// The slot stays SET until releaseAdopt consumes it.
					const released = await pendingAdopt.promise;
					return deep(released);
				}
				return deep(updated);
			}
			if (payload.expected_revision !== 0) {
				throw new ApiError(409, 'conflict', 'stale revision or canonical state conflict');
			}
			const rec = makeRecord(resolvedId, serverId, payload.name, {
				origin: payload.origin,
				compose_project: payload.compose_project,
				working_dir: payload.working_dir,
				unit_name: payload.unit_name,
				external_working_dir: payload.external_working_dir,
				external_config_files: [...(payload.external_config_files || [])],
				external_environment_files: [...(payload.external_environment_files || [])],
				category: payload.category ?? 'system',
				description: payload.description ?? '',
				instructions: payload.instructions ? deep(payload.instructions) : undefined,
				sha256: 'c'.repeat(64)
			});
			adopted[serverId] = adopted[serverId] || {};
			adopted[serverId][resolvedId] = rec;
			const created = {
				intent_id: INTENT_ID,
				application_id: resolvedId,
				server_id: serverId,
				outcome: 'applied',
				revision: 1,
				sha256: rec.sha256,
				application: rec,
				record_doc: recordDocArm ? deep(recordDocArm) : { status: 'documented', doc_id: RECORD_DOC_ID }
			};
			if (pendingAdopt) {
				// The slot stays SET until releaseAdopt consumes it — nulling it
				// here would strand the parked promise forever (the panel waits,
				// release finds nothing, the button spins silently).
				const released = await pendingAdopt.promise;
				return deep(released);
			}
			return deep(created);
		},
		async revoke(serverId, applicationId, payload) {
			record('revoke', deep(payload), [serverId, applicationId]);
			throwArm('revoke');
			const scope = adopted[serverId] || {};
			const rec = scope[applicationId];
			if (!rec) {
				throw new ApiError(404, 'not_found', 'no adoption record for this application id');
			}
			if (payload.expected_revision !== rec.revision) {
				throw new ApiError(409, 'conflict', 'stale revision or unreconciled journal state');
			}
			delete adopted[serverId][applicationId];
			return null;
		}
	};

	function openLifecycle(op) {
		return function (serverId, applicationId) {
			record(op, null, [serverId, applicationId]);
		};
	}

	function openDocument(docId) {
		record('openDocument', null, [docId]);
	}

	// Fixture state (records, calls, gates, clock) lives in the page's JS
	// realm: every Playwright test gets a fresh page and therefore a fresh
	// module evaluation — no per-test reset hook is needed or wanted.

	const services = applicationsServices(
		api,
		{
			openInstall: openLifecycle('openInstall'),
			openUpdate: openLifecycle('openUpdate'),
			openRemove: openLifecycle('openRemove')
		},
		{ openDocument: openDocument }
	);
	const servicesNoSeams = applicationsServices(api, null, null);

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
		applicationsEntry(),
		{
			panel_kind: 'fixture-notes',
			title: 'Notes',
			scope_types: ['server'],
			component: () => Promise.resolve(sibling)
		}
	]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	const registry = registryResult.value;
	const entryCheck = definePanelEntry(applicationsEntry());
	if (!entryCheck.ok) throw new Error('feature entry rejected: ' + entryCheck.issue.code);

	// --- hosts ----------------------------------------------------------------
	let showA = $state(true);

	function descriptor(windowId, scope, z, kind) {
		return {
			schema_version: 1,
			window_id: windowId,
			panel_kind: kind ?? APPLICATIONS_PANEL_KIND,
			scope,
			geometry: { x: 8, y: 8, width: 880, height: 680 },
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
		failNextList(failure) {
			arms.list = failure;
		},
		failNextDiscover(failure) {
			arms.discover = failure;
		},
		failNextAdopt(failure) {
			arms.adopt = failure;
		},
		failNextRevoke(failure) {
			arms.revoke = failure;
		},
		armDeferredDiscover() {
			deferredDiscover = Promise.withResolvers();
		},
		releaseDiscover(view) {
			const entry = pendingDiscover;
			pendingDiscover = null;
			deferredDiscover = null;
			if (entry) entry.resolve(JSON.parse(JSON.stringify(view)));
		},
		armDeferredList() {
			deferredList = Promise.withResolvers();
		},
		releaseList(pageView) {
			const entry = pendingList;
			pendingList = null;
			deferredList = null;
			if (entry) entry.resolve(JSON.parse(JSON.stringify(pageView)));
		},
		armDeferredAdopt() {
			deferredAdopt = Promise.withResolvers();
		},
		releaseAdopt(result) {
			const entry = pendingAdopt;
			pendingAdopt = null;
			deferredAdopt = null;
			if (entry) entry.resolve(JSON.parse(JSON.stringify(result)));
		},
		armRecordDoc(doc) {
			recordDocArm = doc;
		},
		setRevision(applicationId, revision) {
			for (const scope of Object.values(adopted)) {
				if (scope[applicationId]) scope[applicationId].revision = revision;
			}
		},
		toggleHostA() {
			showA = !showA;
		},
		registry: {
			kinds: Array.from(registry.knownPanelKinds()).sort(),
			scopeChecks: {
				serverScope: checkCode(APPLICATIONS_PANEL_KIND, { kind: 'server', server_id: SERVER_A }),
				globalScope: checkCode(APPLICATIONS_PANEL_KIND, { kind: 'global' }),
				unknownKind: checkCode('not-a-panel', { kind: 'server', server_id: SERVER_A }),
				pinnedObject: checkCode(APPLICATIONS_PANEL_KIND, {
					kind: 'server',
					server_id: SERVER_A,
					object: { kind: 'application', id: APP_ADOPTED }
				}),
				foreignObject: checkCode(APPLICATIONS_PANEL_KIND, {
					kind: 'server',
					server_id: SERVER_A,
					object: { kind: 'run', id: '66666666-6666-4222-8333-444455556666' }
				})
			},
			defaultGeometry: registry.defaultGeometry(APPLICATIONS_PANEL_KIND),
			minGeometry: registry.minGeometry(APPLICATIONS_PANEL_KIND),
			instanceKey: singletonInstanceKey(APPLICATIONS_PANEL_KIND, entryCheck.value, {
				kind: 'server',
				server_id: SERVER_A
			}),
			pinnedInstanceKey: singletonInstanceKey(APPLICATIONS_PANEL_KIND, entryCheck.value, {
				kind: 'server',
				server_id: SERVER_A,
				object: { kind: 'application', id: APP_ADOPTED }
			})
		}
	};
	window.__maiAppsApi = windowApi;
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
<section data-testid="host-pinned" aria-label="Host pinned application">
	<ComponentHost label="Host pinned">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_PINNED}', { kind: 'server', server_id: SERVER_A, object: { kind: 'application', id: '${APP_ADOPTED}' } }, 3)}
			{services}
		/>
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
// Build the harness once per worker (same shape as the reviewed docs fixture)
// ---------------------------------------------------------------------------

/** Serves the virtual modules and the built lazy chunks; nothing hits disk. */
function harnessPlugin(sources: Record<string, string>): Plugin {
	return {
		name: 'mai-apps-harness',
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
			outDir: 'node_modules/.mai-apps-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'apps-[hash].js',
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
<title>MAI apps fixture</title>
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
// Fixture-API boundary (serialized dispatch; same convention as docs spec)
// ---------------------------------------------------------------------------

/** Call one fixture-api method inside the page; args must be serializable. */
async function apiCall(page: Page, method: string, args: unknown[] = []): Promise<void> {
	const pair: [string, unknown[]] = [method, args];
	await page.evaluate(([name, callArgs]) => {
		const value = window.__maiAppsApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		// Dynamic dispatch by serialized name: the fixture's own typed API
		// viewed as a name→method table; the typeof guard still checks the
		// lookup before any call.
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

/** Snapshot the fixture's recorded calls and precomputed registry summary. */
async function apiState(page: Page): Promise<{
	calls: RecordedCall[];
	registry: AppsWindowApi['registry'];
}> {
	return page.evaluate(() => {
		const value = window.__maiAppsApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		return { calls: value.calls, registry: value.registry };
	});
}

/** Test-only recorded-wire boundary: read the LAST recorded op payload. */
function lastCall(calls: RecordedCall[], op: string): RecordedCall {
	const found = calls.filter((call) => call.op === op);
	if (found.length === 0) throw new Error(`no recorded call for op ${op}`);
	return found[found.length - 1];
}

/** Both application windows mounted (lazy chunk fetched through the real loader). */
async function bothWindowsMounted(page: Page): Promise<void> {
	await expect(page.getByTestId('host-a').getByTestId('apps-root')).toBeVisible();
	await expect(page.getByTestId('host-b').getByTestId('apps-root')).toBeVisible();
	await expect(page.getByTestId('host-a').getByTestId('inventory-list')).toBeVisible();
	await expect(page.getByTestId('host-b').getByTestId('inventory-list')).toBeVisible();
	expect(servedChunks.length).toBeGreaterThan(0);
}

// ---------------------------------------------------------------------------

test('inventory lists only the pinned server with exact identity; the pinned object is highlighted beside a sibling', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	const hostB = page.getByTestId('host-b');
	await bothWindowsMounted(page);

	// The desktop holds the applications panel BESIDE a sibling panel.
	await expect(page.getByTestId('host-sibling').getByTestId('sibling-panel')).toBeVisible();

	// Alpha: exactly the pre-adopted record, with its full identity and the
	// referenced external deployment paths.
	const rowsA = hostA.getByTestId('inventory-row');
	await expect(rowsA).toHaveCount(1);
	await expect(rowsA.getByTestId('inventory-row-name')).toHaveText('metrics-stack');
	await expect(rowsA.getByTestId('inventory-row-identity')).toHaveText(APP_ADOPTED);
	await expect(rowsA.getByTestId('inventory-row-server')).toHaveText(SERVER_A);
	await expect(rowsA.getByTestId('inventory-row-working-dir')).toHaveText('/srv/metrics');
	await expect(rowsA.getByTestId('inventory-row-external-dir')).toHaveText('/srv/metrics');
	await expect(rowsA.getByTestId('inventory-row-config-files')).toHaveText('/srv/metrics/compose.yaml');
	await expect(rowsA.getByTestId('inventory-row-revision')).toHaveText('r2');
	await expect(hostA.getByTestId('inventory-count')).toContainText('1 of 1');

	// Beta: its own record only; Alpha's never leaks across the scope.
	const rowsB = hostB.getByTestId('inventory-row');
	await expect(rowsB).toHaveCount(1);
	await expect(rowsB.getByTestId('inventory-row-name')).toHaveText('beta-agent');
	await expect(rowsB.getByTestId('inventory-row-server')).toHaveText(SERVER_B);
	await expect(rowsB.filter({ hasText: APP_ADOPTED })).toHaveCount(0);
	await expect(rowsA.filter({ hasText: APP_BETA })).toHaveCount(0);

	// The application-pinned window highlights exactly that record.
	const pinnedRows = page.getByTestId('host-pinned').getByTestId('inventory-row');
	await expect(pinnedRows).toHaveCount(1);
	await expect(pinnedRows).toHaveAttribute('data-pinned', 'true');
	await expect(
		page
			.getByTestId('host-a')
			.getByTestId('inventory-row')
			.filter({ hasText: 'metrics-stack' })
	).toHaveAttribute('data-pinned', 'false');

	const { calls } = await apiState(page);
	// Every page read named its own window's pinned server, and Alpha's
	// window actually read Alpha (exact server identity end to end).
	const listPayloads = calls
		.filter((call) => call.op === 'listAdopted')
		.map((call) => call.args?.[0]);
	expect(listPayloads).toContain(SERVER_A);
	expect(listPayloads.every((scope) => scope === SERVER_A || scope === SERVER_B)).toBe(true);
});

test('discovery observes read-only lanes; the catalogue distinguishes discovered, adopted and pending', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	const hostB = page.getByTestId('host-b');
	await bothWindowsMounted(page);

	for (const host of [hostA, hostB]) {
		await host.getByTestId('tab-discovery').click();
		await host.getByTestId('discovery-observe').click();
	}

	// Alpha snapshot: honest per-lane states (the refused port lane is
	// REPORTED, not silently absent) + the catalogue summary.
	const lanesA = hostA.getByTestId('lane-row');
	await expect(lanesA).toHaveCount(3);
	await expect(hostA.getByTestId('lane-state').filter({ hasText: 'observed' })).toHaveCount(2);
	await expect(hostA.getByTestId('lane-state').filter({ hasText: 'permission_denied' })).toHaveCount(1);
	await expect(hostA.getByTestId('lane-malformed')).toHaveText('1 malformed lines');
	await expect(hostA.getByTestId('discovery-ports')).toHaveText('2 listen ports');
	await expect(hostA.getByTestId('catalogue-discovered')).toHaveText('3 discovered');
	await expect(hostA.getByTestId('catalogue-adopted')).toHaveText('1 adopted');
	await expect(hostA.getByTestId('catalogue-pending')).toHaveText('0 pending');

	// The pre-adopted metrics project is tagged `adopted` with its id; the
	// blog project is `discovered` — the resources never merge.
	const statusA = hostA.getByTestId('discovery-row-status');
	await expect(statusA.filter({ hasText: 'adopted' })).toHaveCount(1);
	await expect(statusA.filter({ hasText: 'discovered' })).toHaveCount(3);
	await expect(hostA.getByTestId('discovery-row-managed-id')).toHaveText(
		`managed as ${APP_ADOPTED.slice(0, 8)}`
	);
	await expect(hostA.getByTestId('discovery-row').filter({ hasText: 'blog' }).getByTestId('adopt-start')).toBeVisible();

	// Beta's snapshot is Beta's only: same project name, different server —
	// Alpha's adopted tagging never crosses. Beta holds its own adopted
	// project plus the conflicting blog observation.
	const statusB = hostB.getByTestId('discovery-row-status');
	await expect(statusB.filter({ hasText: 'adopted' })).toHaveCount(1);
	await expect(statusB.filter({ hasText: 'discovered' })).toHaveCount(2);
	await expect(
		hostB.getByTestId('discovery-row').filter({ hasText: 'blog-beta' }).getByTestId('discovery-row-working-dir')
	).toHaveText('/opt/blog-beta');
	await expect(
		hostB.getByTestId('discovery-row').filter({ hasText: 'agent' }).getByTestId('discovery-row-status')
	).toHaveText('adopted');
	await expect(hostB.getByTestId('apps-catalogue')).toContainText('1 adopted');

	// Hostile lane/unit text stays inert: rendered as text, no node built.
	const rootA = hostA.getByTestId('apps-root');
	await expect(hostA.getByTestId('lane-detail').filter({ hasText: '<img src=x' })).toHaveCount(1);
	expect(await rootA.locator('script').count()).toBe(0);
	expect(await rootA.locator('img').count()).toBe(0);
	expect(await rootA.locator('iframe').count()).toBe(0);
	const fired = await page.evaluate(() => (window as { __maiXssFired?: boolean }).__maiXssFired);
	expect(fired).toBeUndefined();

	const { calls } = await apiState(page);
	const discoverScopes = calls
		.filter((call) => call.op === 'discover')
		.map((call) => call.args?.[0]);
	expect(discoverScopes).toEqual([SERVER_A, SERVER_B]);
});

test('adoption is explicit, preserves the external deployment paths, and links the record document; discovery then tags it adopted', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await bothWindowsMounted(page);

	// Discover, then start the adoption FROM the observation.
	await hostA.getByTestId('tab-discovery').click();
	await hostA.getByTestId('discovery-observe').click();
	await hostA.getByTestId('discovery-row').filter({ hasText: 'blog' }).getByTestId('adopt-start').click();

	// The form arrives pre-filled from the observation; `application_id` is
	// generated on the backend at submit time (no manual UUID field in the UI).
	await expect(hostA.getByTestId('adopt-origin')).toContainText('Compose project');
	await expect(hostA.getByTestId('adopt-application-id')).toHaveCount(0);
	const name = hostA.getByTestId('adopt-name');
	const workingDir = hostA.getByTestId('adopt-working-dir');
	const externalDir = hostA.getByTestId('adopt-external-dir');
	const configFiles = hostA.getByTestId('adopt-config-files');
	const envFiles = hostA.getByTestId('adopt-env-files');
	await expect(workingDir).toHaveValue('/srv/blog');
	await expect(externalDir).toHaveValue('/srv/blog');
	await expect(configFiles).toHaveValue('/srv/blog/compose.yaml');
	await expect(envFiles).toHaveValue('/srv/blog/.env');
	await expect(name).toHaveValue('blog');

	// Local validation first: an empty display name refuses locally.
	await name.fill('');
	await hostA.getByTestId('adopt-submit').click();
	await expect(name).toHaveAttribute('aria-invalid', 'true');
	const { calls: before } = await apiState(page);
	expect(before.filter((call) => call.op === 'adopt')).toHaveLength(0);

	// Pending state while the decision is in flight (AC 1 pending, honest).
	await apiCall(page, 'armDeferredAdopt');
	await name.fill('blog');
	await hostA.getByTestId('adopt-submit').click();
	await hostA.getByTestId('tab-discovery').click();
	await expect(
		hostA.getByTestId('discovery-row').filter({ hasText: 'blog' }).getByTestId('discovery-row-status')
	).toHaveText('pending');
	await expect(hostA.getByTestId('catalogue-pending')).toHaveText('1 pending');

	// Release the commit: identity + retained paths + record doc link.
	await hostA.getByTestId('tab-adopt').click();
	await apiCall(page, 'releaseAdopt', [
		{
			intent_id: INTENT_ID,
			application_id: APP_NEW,
			server_id: SERVER_A,
			outcome: 'applied',
			revision: 1,
			sha256: 'c'.repeat(64),
			application: {
				application_id: APP_NEW,
				server_id: SERVER_A,
				name: 'blog',
				origin: 'compose',
				compose_project: 'blog',
				working_dir: '/srv/blog',
				unit_name: null,
				external_working_dir: '/srv/blog',
				external_config_files: ['/srv/blog/compose.yaml'],
				external_environment_files: ['/srv/blog/.env'],
				revision: 1,
				schema_revision: 1,
				sha256: 'c'.repeat(64),
				adopted_at: '2026-10-17T09:00:00Z',
				reconciled_at: '2026-10-17T09:00:00Z',
				stale: false
			},
			record_doc: { status: 'documented', doc_id: RECORD_DOC_ID }
		}
	]);
	await expect(hostA.getByTestId('adopt-result')).toBeVisible();
	await expect(hostA.getByTestId('adopt-result-outcome')).toHaveText('applied');
	await expect(hostA.getByTestId('adopt-result-identity')).toHaveText(APP_NEW);
	await expect(hostA.getByTestId('adopt-result-server')).toHaveText(SERVER_A);
	await expect(hostA.getByTestId('adopt-result-revision')).toHaveText('r1');
	await hostA.getByTestId('adopt-result-doc-open').click();

	// The inventory refreshed to the committed truth; the external paths are
	// retained verbatim and the record names the pinned server.
	await hostA.getByTestId('tab-inventory').click();
	const rows = hostA.getByTestId('inventory-row');
	await expect(rows).toHaveCount(2);
	const newRows = rows.filter({ hasText: APP_NEW });
	await expect(newRows).toHaveCount(1);
	await expect(newRows.getByTestId('inventory-row-name')).toHaveText('blog');
	await expect(newRows.getByTestId('inventory-row-working-dir')).toHaveText('/srv/blog');
	await expect(newRows.getByTestId('inventory-row-config-files')).toHaveText('/srv/blog/compose.yaml');
	await expect(newRows.getByTestId('inventory-row-server')).toHaveText(SERVER_A);

	// The stale snapshot says so; a fresh observe tags the blog project
	// adopted with the new id (managed reference matching, server-scoped).
	await expect(hostA.getByTestId('discovery-stale')).toHaveCount(0);
	await hostA.getByTestId('tab-discovery').click();
	await expect(hostA.getByTestId('discovery-stale')).toBeVisible();
	await hostA.getByTestId('discovery-observe').click();
	await expect(hostA.getByTestId('discovery-stale')).toHaveCount(0);
	await expect(hostA.getByTestId('catalogue-adopted')).toHaveText('2 adopted');
	await expect(
		hostA.getByTestId('discovery-row').filter({ hasText: 'blog' }).getByTestId('discovery-row-status')
	).toHaveText('adopted');

	const { calls } = await apiState(page);
	const adopt = lastCall(calls, 'adopt');
	expect(adopt.args).toEqual([SERVER_A]);
	expect(adopt.payload).toEqual({
		name: 'blog',
		origin: 'compose',
		compose_project: 'blog',
		working_dir: '/srv/blog',
		unit_name: null,
		external_working_dir: '/srv/blog',
		external_config_files: ['/srv/blog/compose.yaml'],
		external_environment_files: ['/srv/blog/.env'],
		expected_revision: 0
	});
	expect(lastCall(calls, 'openDocument').args).toEqual([RECORD_DOC_ID]);
	// The page-global A-or-B sweep lives in the cross-server test; this
	// test's own writes are already asserted server-exact above (the mount
	// loads of BOTH hosts legitimately share the recorded-call log).
});

test('lifecycle actions are links to separate surfaces; without the seam they fail closed', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await bothWindowsMounted(page);

	// With the seam: the links call the SEPARATELY implemented openers.
	await hostA.getByTestId('install-link').click();
	await hostA.getByTestId('update-link').click();
	await hostA.getByTestId('remove-link').click();
	let { calls } = await apiState(page);
	expect(lastCall(calls, 'openInstall').args).toEqual([SERVER_A, APP_ADOPTED]);
	expect(lastCall(calls, 'openUpdate').args).toEqual([SERVER_A, APP_ADOPTED]);
	expect(lastCall(calls, 'openRemove').args).toEqual([SERVER_A, APP_ADOPTED]);
	// No lifecycle flow is reimplemented in this panel: no plan/submit-style
	// API call exists here at all — the only API ops are the four reviewed ones.
	const apiOps = new Set(calls.map((call) => call.op));
	expect(apiOps.has('adopt')).toBe(false);

	// Without the seam: disabled links + a visible honest note.
	const noseam = page.getByTestId('host-noseam');
	await expect(noseam.getByTestId('inventory-list')).toBeVisible();
	await expect(noseam.getByTestId('install-link')).toBeDisabled();
	await expect(noseam.getByTestId('update-link')).toBeDisabled();
	await expect(noseam.getByTestId('remove-link')).toBeDisabled();
	await expect(noseam.getByTestId('lifecycle-unavailable')).toContainText('not wired');

	calls = (await apiState(page)).calls;
	const seamCalls = calls.filter(
		(call) => call.op === 'openInstall' || call.op === 'openUpdate' || call.op === 'openRemove'
	);
	expect(seamCalls).toHaveLength(3);
});

test('revocation is revision-checked and non-destructive; a stale revision is refused honestly', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await bothWindowsMounted(page);

	// Hold a snapshot first: the staleness note only means something against
	// a snapshot this window actually observed.
	await hostA.getByTestId('tab-discovery').click();
	await hostA.getByTestId('discovery-observe').click();
	await expect(hostA.getByTestId('discovery-meta')).toBeVisible();
	await hostA.getByTestId('tab-inventory').click();

	// Stale first: the record moves to r9 elsewhere, the confirm still sends
	// the revision this window saw (r2) — the producer refuses, the row stays.
	await hostA.getByTestId('revoke-start').click();
	await expect(hostA.getByTestId('revoke-confirm')).toContainText('metrics-stack');
	await expect(hostA.getByTestId('revoke-confirm')).toContainText('external deployment paths are never touched');
	await apiCall(page, 'setRevision', [APP_ADOPTED, 9]);
	await hostA.getByTestId('revoke-accept').click();
	await expect(hostA.getByTestId('revoke-error')).toBeVisible();
	await expect(hostA.getByTestId('revoke-error')).toHaveAttribute('data-error-code', 'conflict');
	await expect(hostA.getByTestId('inventory-row').filter({ hasText: APP_ADOPTED })).toHaveCount(1);

	// Fresh revision: the revoke commits; only the canonical record is gone.
	await expect(hostA.getByTestId('revoke-confirm')).toBeVisible();
	await apiCall(page, 'setRevision', [APP_ADOPTED, 2]);
	await hostA.getByTestId('revoke-accept').click();
	await expect(hostA.getByTestId('revoke-done')).toContainText('external deployment was never touched');
	await expect(hostA.getByTestId('inventory-empty')).toBeVisible();
	await expect(hostA.getByTestId('revoke-confirm')).toHaveCount(0);

	const { calls } = await apiState(page);
	const revokes = calls.filter((call) => call.op === 'revoke');
	expect(revokes).toHaveLength(2);
	expect(revokes[0].args).toEqual([SERVER_A, APP_ADOPTED]);
	expect(revokes[0].payload).toEqual({ expected_revision: 2 });
	expect(revokes[1].payload).toEqual({ expected_revision: 2 });

	// The held snapshot predates the decision: the panel says observe again.
	await hostA.getByTestId('tab-discovery').click();
	await expect(hostA.getByTestId('discovery-stale')).toBeVisible();
});

test('a discovery outage and a host-key refusal render honest failures, never empty successes', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await bothWindowsMounted(page);

	// Transport refusal mapped by the producer: changed host key is a
	// security boundary (409 conflict), not a retry.
	await apiCall(page, 'failNextDiscover', [
		{ status: 409, code: 'conflict', message: 'pinned host key does not match the target' }
	]);
	await hostA.getByTestId('tab-discovery').click();
	await hostA.getByTestId('discovery-observe').click();
	await expect(hostA.getByTestId('discovery-error')).toBeVisible();
	await expect(hostA.getByTestId('discovery-error')).toHaveAttribute('data-error-code', 'conflict');
	await expect(hostA.getByTestId('discovery-error')).toContainText('pinned host key does not match');
	await expect(hostA.getByTestId('discovery-list')).toHaveCount(0);
	await expect(hostA.getByTestId('apps-catalogue')).toHaveCount(0);

	// Service outage: fail closed with the stable code, retry offered.
	await apiCall(page, 'failNextDiscover', [
		{ status: 503, code: 'unavailable', message: 'credential source or SSH transport unavailable' }
	]);
	await hostA.getByTestId('discovery-retry').click();
	await expect(hostA.getByTestId('discovery-error')).toHaveAttribute('data-error-code', 'unavailable');

	// The inventory half is independent: its own error surface appears when
	// the adopted page fails, without touching the discovery state.
	await hostA.getByTestId('tab-inventory').click();
	await apiCall(page, 'failNextList', [
		{ status: 503, code: 'unavailable', message: 'applications service unavailable' }
	]);
	await hostA.getByTestId('inventory-refresh').click();
	await expect(hostA.getByTestId('inventory-error')).toBeVisible();
	await expect(hostA.getByTestId('inventory-error')).toHaveAttribute('data-error-code', 'unavailable');
	await hostA.getByTestId('inventory-retry').click();
	await expect(hostA.getByTestId('inventory-list')).toBeVisible();

	const { calls } = await apiState(page);
	for (const call of calls.filter((candidate) => candidate.op === 'discover')) {
		expect(call.args?.[0]).toBe(SERVER_A);
	}
});

test('a cross-server response cannot paint; a closed window drops its late adoption answer; focus never retargets', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	const hostB = page.getByTestId('host-b');
	await bothWindowsMounted(page);

	// A parked response naming ANOTHER server is refused by the identity
	// guard (AC 1): the snapshot must name the pinned scope before it may
	// paint — no applications, no catalogue, an honest error instead.
	await apiCall(page, 'armDeferredDiscover');
	await hostA.getByTestId('tab-discovery').click();
	await hostA.getByTestId('discovery-observe').click();
	await apiCall(page, 'releaseDiscover', [
		{
			server_id: SERVER_B,
			observed_at: '2020-01-01T00:00:00Z',
			applications: [
				{
					origin: 'compose',
					managed: false,
					application_id: null,
					detail: {
						kind: 'compose_project',
						project_name: 'CROSS-SERVER BLOG',
						working_dir: '/opt/blog-beta',
						config_files: [],
						environment_files: [],
						services: [],
						containers: []
					}
				}
			],
			listen_ports: [],
			lanes: [{ lane: 'docker', state: 'observed', detail: 'CROSS-SERVER SNAPSHOT', truncated: false, malformed_lines: 0 }]
		}
	]);
	await expect(hostA.getByTestId('discovery-error')).toBeVisible();
	await expect(hostA.getByTestId('discovery-error')).toHaveAttribute('data-error-code', 'scope_mismatch');
	await expect(hostA.getByTestId('discovery-error')).toContainText('names a different server');
	await expect(hostA.getByTestId('discovery-list')).toHaveCount(0);
	await expect(hostA.getByTestId('apps-root')).not.toContainText('CROSS-SERVER');

	// A fresh observe recovers: the refused payload left nothing behind.
	await hostA.getByTestId('discovery-retry').click();
	await expect(hostA.getByTestId('discovery-meta')).toBeVisible();
	await expect(hostA.getByTestId('apps-root')).not.toContainText('2020-01-01');

	// Cross-window isolation: Beta paints its own snapshot; Alpha's held
	// answer never crosses either.
	await hostB.getByTestId('tab-discovery').click();
	await hostB.getByTestId('discovery-observe').click();
	await expect(hostB.getByTestId('discovery-meta')).toBeVisible();
	await expect(page.getByTestId('host-b').getByTestId('apps-root')).not.toContainText('/srv/blog');

	// The SAME identity guard protects the adopted page: a page naming
	// another server is refused before it can paint one row.
	await hostA.getByTestId('tab-inventory').click();
	await apiCall(page, 'armDeferredList');
	await hostA.getByTestId('inventory-refresh').click();
	await apiCall(page, 'releaseList', [
		{
			server_id: SERVER_B,
			applications: [
				{
					application_id: APP_BETA,
					server_id: SERVER_B,
					name: 'beta-agent',
					origin: 'compose',
					compose_project: 'agent',
					working_dir: '/opt/agent',
					unit_name: null,
					external_working_dir: '/opt/agent',
					external_config_files: [],
					external_environment_files: [],
					revision: 5,
					schema_revision: 1,
					sha256: 'a'.repeat(64),
					adopted_at: '2026-10-16T10:00:00Z',
					reconciled_at: '2026-10-16T10:00:00Z',
					stale: false
				}
			],
			limit: 50,
			offset: 0,
			total: 1
		}
	]);
	await expect(hostA.getByTestId('inventory-error')).toBeVisible();
	await expect(hostA.getByTestId('inventory-error')).toHaveAttribute('data-error-code', 'scope_mismatch');
	await expect(hostA.getByTestId('inventory-error')).toContainText('names a different server');
	await expect(hostA.getByTestId('inventory-row')).toHaveCount(0);
	await hostA.getByTestId('inventory-retry').click();
	await expect(hostA.getByTestId('inventory-list')).toBeVisible();
	await expect(hostA.getByTestId('inventory-row')).toHaveCount(1);

	// Adopt with the response parked, then close the window mid-flight: the
	// late answer is dropped (no result card anywhere, no page error), the
	// committed record still exists and a fresh window renders it.
	await hostA.getByTestId('tab-discovery').click();
	await hostA.getByTestId('discovery-row').filter({ hasText: 'blog' }).getByTestId('adopt-start').click();
	await apiCall(page, 'armDeferredAdopt');
	await hostA.getByTestId('adopt-submit').click();
	await apiCall(page, 'toggleHostA');
	await expect(page.getByTestId('host-a').getByTestId('apps-root')).toHaveCount(0);
	await apiCall(page, 'releaseAdopt', [
		{
			intent_id: INTENT_ID,
			application_id: APP_NEW,
			server_id: SERVER_B,
			outcome: 'applied',
			revision: 1,
			sha256: 'c'.repeat(64),
			application: {
				application_id: APP_NEW,
				server_id: SERVER_B,
				name: 'blog',
				origin: 'compose',
				compose_project: 'blog',
				working_dir: '/srv/blog',
				unit_name: null,
				external_working_dir: '/srv/blog',
				external_config_files: [],
				external_environment_files: [],
				revision: 1,
				schema_revision: 1,
				sha256: 'c'.repeat(64),
				adopted_at: '2026-10-17T09:30:00Z',
				reconciled_at: '2026-10-17T09:30:00Z',
				stale: false
			},
			record_doc: { status: 'pending', reason: 'unavailable' }
		}
	]);
	await expect(page.getByTestId('host-b').getByTestId('adopt-result')).toHaveCount(0);

	// Reopen: the panel starts clean and renders the committed truth (the
	// fixture committed at submit time, exactly like the journaled producer).
	await apiCall(page, 'toggleHostA');
	await expect(hostA.getByTestId('apps-root')).toBeVisible();
	const rows = hostA.getByTestId('inventory-row');
	await expect(rows).toHaveCount(2);
	await expect(rows.filter({ hasText: APP_NEW })).toHaveCount(1);

	// Interacting with the sibling/focused panel never retargets any scope.
	await page.getByTestId('host-sibling').getByTestId('sibling-panel').click();
	const { calls } = await apiState(page);
	for (const call of calls) {
		if (call.op === 'listAdopted' || call.op === 'discover') {
			const scope = call.args?.[0];
			expect(scope === SERVER_A || scope === SERVER_B).toBe(true);
		}
		if (call.op === 'adopt' || call.op === 'revoke') {
			expect(call.args?.[0]).toBe(SERVER_A);
		}
	}
	// Zero uncaught page errors across every race and the unmount.
	expect(pageErrors).toEqual([]);
});

test('fail-closed bag and global scope; the typed entry validates with singleton scoping', async ({
	page
}) => {
	await bothWindowsMounted(page);

	// No services at all → safe config error, never a crash or a fetch.
	const bare = page.getByTestId('host-bare');
	await expect(bare.getByTestId('panel-config-error')).toBeVisible();
	await expect(bare.getByTestId('panel-config-error')).toContainText('without its applications service');

	// Global scope contradicts the entry contract: the registry's scope check
	// stops the panel BEFORE mount, so the host renders its safe unavailable
	// view (never the panel body, never a fetch).
	const globalHost = page.getByTestId('host-global');
	await expect(globalHost.getByTestId('panel-unavailable')).toBeVisible();
	await expect(globalHost.getByTestId('panel-unavailable')).toHaveAttribute(
		'data-reason',
		'scope_kind_not_allowed'
	);
	await expect(globalHost.getByTestId('panel-config-error')).toHaveCount(0);

	// The registry validated the REAL feature entry: kinds, scope policy
	// (server-only, application pins allowed, foreign pins rejected),
	// geometry and the singleton instance keys.
	const { registry } = await apiState(page);
	expect(registry.kinds).toContain('applications');
	expect(registry.scopeChecks.serverScope).toBe('ok');
	expect(registry.scopeChecks.pinnedObject).toBe('ok');
	expect(registry.scopeChecks.foreignObject).toBe('scope_object_kind_not_allowed');
	expect(registry.scopeChecks.globalScope).toBe('scope_kind_not_allowed');
	expect(registry.scopeChecks.unknownKind).toBe('unknown_panel_kind');
	expect(registry.defaultGeometry).toEqual({ x: 88, y: 72, width: 840, height: 600 });
	expect(registry.minGeometry).toEqual({ x: 0, y: 0, width: 480, height: 360 });
	expect(registry.instanceKey).toBe(`applications::server/${SERVER_A}`);
	expect(registry.pinnedInstanceKey).toBe(`applications::server/${SERVER_A}/application/${APP_ADOPTED}`);
});

test('an adoption conflict is refused with the honest vocabulary and nothing changes', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await bothWindowsMounted(page);

	await hostA.getByTestId('tab-discovery').click();
	await hostA.getByTestId('discovery-observe').click();
	await hostA.getByTestId('discovery-row').filter({ hasText: 'blog' }).getByTestId('adopt-start').click();
	await apiCall(page, 'failNextAdopt', [
		{ status: 409, code: 'conflict', message: 'duplicate adoption or selector overlap' }
	]);
	await hostA.getByTestId('adopt-submit').click();

	await expect(hostA.getByTestId('adopt-error')).toBeVisible();
	await expect(hostA.getByTestId('adopt-error')).toHaveAttribute('data-error-code', 'conflict');
	await expect(hostA.getByTestId('adopt-error')).toContainText('No adoption was committed');
	await expect(hostA.getByTestId('adopt-result')).toHaveCount(0);
	await expect(hostA.getByTestId('discovery-stale')).toHaveCount(0);

	// The inventory still holds exactly the pre-existing record.
	await hostA.getByTestId('tab-inventory').click();
	await expect(hostA.getByTestId('inventory-row')).toHaveCount(1);
});

// ---------------------------------------------------------------------------
// Service Map & Category Taxonomy (campaign mechanicus-ui, mui_04)
// ---------------------------------------------------------------------------

/** One adopted page record WITH Service Map metadata (released over the list gate). */
const METRICS_MAPPED = {
	application_id: APP_ADOPTED,
	server_id: SERVER_A,
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
	stale: false,
	category: 'observability',
	description: 'Metrics and alerting stack.',
	instructions: {
		install: 'docker compose up -d',
		update: 'docker compose pull && docker compose up -d',
		uninstall: 'docker compose down',
		backup: 'restic backup /srv/metrics',
		verify: 'curl -fsS localhost:9090/-/healthy'
	}
};

/** A second mapped record in another category (the filter-pill target). */
const GAMES_MAPPED = {
	application_id: APP_BETA,
	server_id: SERVER_A,
	name: 'game-server',
	origin: 'compose',
	compose_project: 'games',
	working_dir: '/opt/games',
	unit_name: null,
	external_working_dir: null,
	external_config_files: [],
	external_environment_files: [],
	revision: 1,
	schema_revision: 1,
	sha256: 'b'.repeat(64),
	adopted_at: '2026-10-16T11:00:00Z',
	reconciled_at: '2026-10-16T11:00:00Z',
	stale: false,
	category: 'games',
	description: ''
};

/** Release a Service Map page (with metadata) over the armed list gate. */
async function releaseMappedPage(page: Page): Promise<void> {
	await apiCall(page, 'armDeferredList');
	await page.getByTestId('host-a').getByTestId('inventory-refresh').click();
	await apiCall(page, 'releaseList', [
		{
			server_id: SERVER_A,
			applications: [METRICS_MAPPED, GAMES_MAPPED],
			limit: 50,
			offset: 0,
			total: 2
		}
	]);
}

test('the service map renders category chips, filter pills, descriptions and the operational runbook', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await expect(hostA.getByTestId('apps-root')).toBeVisible();
	await releaseMappedPage(page);

	// Category chips on both mapped rows (label via the built-in taxonomy).
	const rows = hostA.getByTestId('inventory-row');
	await expect(rows).toHaveCount(2);
	const metricsRow = rows.filter({ hasText: 'metrics-stack' });
	await expect(metricsRow.getByTestId('service-category-chip')).toHaveAttribute(
		'data-category',
		'observability'
	);
	await expect(metricsRow.getByTestId('service-category-chip')).toHaveText('Observability & Backup');
	const gamesRow = rows.filter({ hasText: 'game-server' });
	await expect(gamesRow.getByTestId('service-category-chip')).toHaveAttribute(
		'data-category',
		'games'
	);

	// Category filter pills: All + the two present categories (taxonomy order).
	const pills = hostA.getByTestId('category-filter-pill');
	await expect(pills).toHaveCount(3);
	await expect(pills.nth(0)).toHaveAttribute('data-category', 'all');
	await expect(pills.nth(1)).toHaveAttribute('data-category', 'games');
	await expect(pills.nth(2)).toHaveAttribute('data-category', 'observability');
	await expect(pills.nth(1)).toHaveText('Games');

	// Filtering is a client-side view over the adopted page: pill clicks add
	// ZERO wire calls (the four panel mounts + the one refresh above are all).
	const listCallsBefore = (await apiState(page)).calls.filter(
		(call) => call.op === 'listAdopted'
	).length;
	await pills.nth(1).click();
	await expect(hostA.getByTestId('inventory-row')).toHaveCount(1);
	await expect(hostA.getByTestId('inventory-row-name')).toHaveText('game-server');
	await pills.nth(0).click();
	await expect(hostA.getByTestId('inventory-row')).toHaveCount(2);
	const listCallsAfter = (await apiState(page)).calls.filter(
		(call) => call.op === 'listAdopted'
	).length;
	expect(listCallsAfter).toBe(listCallsBefore);

	// Installation description + the five-slot runbook on the mapped row.
	await expect(metricsRow.getByTestId('service-description')).toHaveText(
		'Metrics and alerting stack.'
	);
	await expect(gamesRow.getByTestId('service-description')).toHaveCount(0);
	await expect(metricsRow.getByTestId('service-instructions')).toBeVisible();
	const steps = metricsRow.getByTestId('runbook-step');
	await expect(steps).toHaveCount(5);
	await expect(metricsRow.locator('[data-step="install"]')).toHaveText('docker compose up -d');
	await expect(metricsRow.locator('[data-step="verify"]')).toHaveText(
		'curl -fsS localhost:9090/-/healthy'
	);
	// A record without runbook renders the honest empty state, never fakes one.
	await expect(gamesRow.getByTestId('runbook-step')).toHaveCount(0);
	await expect(gamesRow.getByTestId('service-instructions')).toContainText(
		'No runbook instructions recorded'
	);
});

test('one-click Chat @service tags the pinned server and service on the window for the dock', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await expect(hostA.getByTestId('apps-root')).toBeVisible();
	await releaseMappedPage(page);

	await page.evaluate(() => {
		const target = window as unknown as { __chatTag?: unknown };
		target.__chatTag = null;
		window.addEventListener('mai:chat-focus-service', (event) => {
			target.__chatTag = (event as CustomEvent).detail;
		});
	});

	const metricsRow = hostA.getByTestId('inventory-row').filter({ hasText: 'metrics-stack' });
	await metricsRow.getByTestId('service-chat-trigger').click();

	const detail = await page.evaluate(() => {
		return (window as unknown as { __chatTag?: unknown }).__chatTag;
	});
	expect(detail).toEqual({
		serverId: SERVER_A,
		serviceName: 'metrics-stack',
		applicationId: APP_ADOPTED,
		category: 'observability'
	});
});

test('discovery rows render suggested category chips from the hint or the heuristic fallback', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await expect(hostA.getByTestId('apps-root')).toBeVisible();

	// The fixture snapshot omits suggested_category: the deterministic
	// client-side keyword heuristic classifies each observation.
	await hostA.getByTestId('tab-discovery').click();
	await hostA.getByTestId('discovery-observe').click();
	const rows = hostA.getByTestId('discovery-row');
	await expect(rows).toHaveCount(4);
	await expect(
		rows.filter({ hasText: 'metrics' }).getByTestId('discovery-category-chip')
	).toHaveAttribute('data-category', 'observability');
	await expect(
		rows.filter({ hasText: 'blog' }).getByTestId('discovery-category-chip')
	).toHaveAttribute('data-category', 'media');
	await expect(
		rows.filter({ hasText: 'legacy-cron' }).getByTestId('discovery-category-chip')
	).toHaveAttribute('data-category', 'system');
	await expect(
		rows.filter({ hasText: 'nginx.service' }).getByTestId('discovery-category-chip')
	).toHaveAttribute('data-category', 'infrastructure');
	await expect(
		rows.filter({ hasText: 'blog' }).getByTestId('discovery-category-chip')
	).toHaveText('Media & Content');

	// The producer hint wins when the snapshot carries it.
	await apiCall(page, 'armDeferredDiscover');
	await hostA.getByTestId('discovery-observe').click();
	await apiCall(page, 'releaseDiscover', [
		{
			server_id: SERVER_A,
			observed_at: '2026-10-17T10:00:00Z',
			applications: [
				{
					origin: 'compose',
					managed: false,
					application_id: null,
					suggested_category: 'ai',
					detail: {
						kind: 'compose_project',
						project_name: 'ollama',
						working_dir: '/srv/ollama',
						config_files: [],
						environment_files: [],
						services: ['ollama'],
						containers: []
					}
				}
			],
			listen_ports: [],
			lanes: [
				{
					lane: 'docker',
					state: 'observed',
					detail: 'docker ps parsed',
					truncated: false,
					malformed_lines: 0
				}
			]
		}
	]);
	const hintRow = hostA.getByTestId('discovery-row').filter({ hasText: 'ollama' });
	await expect(hintRow.getByTestId('discovery-category-chip')).toHaveAttribute(
		'data-category',
		'ai'
	);
	await expect(hintRow.getByTestId('discovery-category-chip')).toHaveText('AI & Models');
});

test('the category taxonomy menu lists the 8 built-ins and mints custom categories into every picker', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await expect(hostA.getByTestId('apps-root')).toBeVisible();

	await hostA.getByTestId('tab-categories').click();
	const menu = hostA.getByTestId('category-taxonomy-menu');
	await expect(menu).toBeVisible();
	const rows = hostA.getByTestId('category-taxonomy-row');
	await expect(rows).toHaveCount(8);
	for (const [machineId, label] of [
		['games', 'Games'],
		['ai', 'AI & Models'],
		['agent', 'Autonomous Agents'],
		['system', 'System & OS'],
		['dev', 'Development & CI'],
		['infrastructure', 'Infrastructure & Proxy'],
		['media', 'Media & Content'],
		['observability', 'Observability & Backup']
	] as const) {
		const row = hostA.locator(
			'[data-testid="category-taxonomy-row"][data-category="' + machineId + '"]'
		);
		await expect(row).toContainText(label);
		await expect(row.getByTestId('category-taxonomy-id')).toHaveText(machineId);
		await expect(row.getByTestId('category-taxonomy-description')).not.toHaveText('');
		await expect(row).toContainText('built-in');
	}

	// Validation: a non-slug machine id refuses locally, nothing is minted.
	await hostA.getByTestId('category-id-input').fill('Bad ID');
	await hostA.getByTestId('category-label-input').fill('Broken');
	await hostA.getByTestId('category-save-btn').click();
	await expect(hostA.getByTestId('category-id-input')).toHaveAttribute('aria-invalid', 'true');
	await expect(hostA.getByTestId('category-taxonomy-row')).toHaveCount(8);

	// Mint a custom category: it joins the taxonomy immediately.
	await hostA.getByTestId('category-id-input').fill('homelab');
	await hostA.getByTestId('category-label-input').fill('Homelab');
	await hostA.getByTestId('category-desc-input').fill('Personal homelab experiments.');
	await hostA.getByTestId('category-save-btn').click();
	const customRow = hostA.locator(
		'[data-testid="category-taxonomy-row"][data-category="homelab"]'
	);
	await expect(customRow).toContainText('Homelab');
	await expect(customRow).toContainText('homelab');
	await expect(customRow).toContainText('Personal homelab experiments.');
	await expect(customRow).toContainText('custom');
	await expect(hostA.getByTestId('category-taxonomy-row')).toHaveCount(9);
	// The form resets for the next mint.
	await expect(hostA.getByTestId('category-id-input')).toHaveValue('');

	// The minted category is already in the adoption picker.
	await hostA.getByTestId('tab-discovery').click();
	await hostA.getByTestId('discovery-observe').click();
	await hostA.getByTestId('discovery-row').filter({ hasText: 'blog' }).getByTestId('adopt-start').click();
	await expect(hostA.getByTestId('adopt-category-select')).toContainText('Homelab');
	// The suggested classification pre-selects the media category.
	await expect(hostA.getByTestId('adopt-category-select')).toHaveValue('media');
});

test('adoption ships category, description and instructions only when explicitly set', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await expect(hostA.getByTestId('apps-root')).toBeVisible();

	await hostA.getByTestId('tab-discovery').click();
	await hostA.getByTestId('discovery-observe').click();
	await hostA.getByTestId('discovery-row').filter({ hasText: 'blog' }).getByTestId('adopt-start').click();

	// Explicit metadata: category changed away from the suggestion, a
	// description, and two runbook slots — all three ride the request.
	await hostA.getByTestId('adopt-category-select').selectOption('games');
	await hostA.getByTestId('adopt-description-input').fill('Blog site.');
	await hostA.getByTestId('adopt-instructions-install').fill('docker compose up -d');
	await hostA.getByTestId('adopt-instructions-verify').fill('curl -fsS localhost');
	await hostA.getByTestId('adopt-submit').click();
	await expect(hostA.getByTestId('adopt-result')).toBeVisible();

	const { calls } = await apiState(page);
	expect(lastCall(calls, 'adopt').payload).toEqual({
		name: 'blog',
		origin: 'compose',
		compose_project: 'blog',
		working_dir: '/srv/blog',
		unit_name: null,
		external_working_dir: '/srv/blog',
		external_config_files: ['/srv/blog/compose.yaml'],
		external_environment_files: ['/srv/blog/.env'],
		expected_revision: 0,
		category: 'games',
		description: 'Blog site.',
		instructions: {
			install: 'docker compose up -d',
			update: '',
			uninstall: '',
			backup: '',
			verify: 'curl -fsS localhost'
		}
	});

	// The committed record carries the map metadata into the inventory.
	await hostA.getByTestId('tab-inventory').click();
	const blogRow = hostA.getByTestId('inventory-row').filter({ hasText: 'blog' });
	await expect(blogRow.getByTestId('service-category-chip')).toHaveAttribute(
		'data-category',
		'games'
	);
	await expect(blogRow.getByTestId('service-description')).toHaveText('Blog site.');
	await expect(blogRow.locator('[data-step="install"]')).toHaveText('docker compose up -d');
});

test('a minted custom category rides the next adoption via upsert_categories', async ({ page }) => {
	const hostA = page.getByTestId('host-a');
	await expect(hostA.getByTestId('apps-root')).toBeVisible();

	await hostA.getByTestId('tab-categories').click();
	await hostA.getByTestId('category-id-input').fill('homelab');
	await hostA.getByTestId('category-label-input').fill('Homelab');
	await hostA.getByTestId('category-desc-input').fill('Personal homelab experiments.');
	await hostA.getByTestId('category-save-btn').click();
	await expect(hostA.getByTestId('category-taxonomy-row')).toHaveCount(9);

	// Adopt WITHOUT touching any optional field: the taxonomy upsert still
	// rides the same reviewed mutation; the create payload otherwise stays
	// exactly the ten frozen keys.
	await hostA.getByTestId('tab-discovery').click();
	await hostA.getByTestId('discovery-observe').click();
	await hostA
		.getByTestId('discovery-row')
		.filter({ hasText: 'blog' })
		.getByTestId('adopt-start')
		.click();
	await hostA.getByTestId('adopt-submit').click();
	await expect(hostA.getByTestId('adopt-result')).toBeVisible();

	const { calls } = await apiState(page);
	expect(lastCall(calls, 'adopt').payload).toEqual({
		name: 'blog',
		origin: 'compose',
		compose_project: 'blog',
		working_dir: '/srv/blog',
		unit_name: null,
		external_working_dir: '/srv/blog',
		external_config_files: ['/srv/blog/compose.yaml'],
		external_environment_files: ['/srv/blog/.env'],
		expected_revision: 0,
		upsert_categories: [
			{
				machine_id: 'homelab',
				label: 'Homelab',
				description: 'Personal homelab experiments.'
			}
		]
	});
});

test('map-details editing re-adopts with the observed revision and a stale revision is refused honestly', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await expect(hostA.getByTestId('apps-root')).toBeVisible();
	await releaseMappedPage(page);

	const metricsRow = hostA.getByTestId('inventory-row').filter({ hasText: 'metrics-stack' });

	// The editor arrives pre-filled with the stored map metadata.
	await metricsRow.getByTestId('service-edit-start').click();
	await expect(hostA.getByTestId('service-edit-form')).toBeVisible();
	await expect(hostA.getByTestId('service-edit-category')).toHaveValue('observability');
	await expect(hostA.getByTestId('service-edit-description')).toHaveValue(
		'Metrics and alerting stack.'
	);
	await expect(hostA.getByTestId('service-edit-instructions-install')).toHaveValue(
		'docker compose up -d'
	);

	// Change the category, the description and one runbook slot; save.
	await hostA.getByTestId('service-edit-category').selectOption('games');
	await hostA.getByTestId('service-edit-description').fill('Relocated to games.');
	await hostA.getByTestId('service-edit-instructions-install').fill('make deploy');
	await hostA.getByTestId('service-edit-save').click();
	await expect(hostA.getByTestId('service-edit-form')).toHaveCount(0);

	// The re-adoption names the same identity at the observed revision and
	// carries the metadata; the external deployment paths are retained.
	const { calls } = await apiState(page);
	expect(lastCall(calls, 'adopt').payload).toEqual({
		application_id: APP_ADOPTED,
		name: 'metrics-stack',
		origin: 'compose',
		compose_project: 'metrics',
		working_dir: '/srv/metrics',
		unit_name: null,
		external_working_dir: '/srv/metrics',
		external_config_files: ['/srv/metrics/compose.yaml'],
		external_environment_files: ['/srv/metrics/.env'],
		expected_revision: 2,
		category: 'games',
		description: 'Relocated to games.',
		instructions: {
			install: 'make deploy',
			update: 'docker compose pull && docker compose up -d',
			uninstall: 'docker compose down',
			backup: 'restic backup /srv/metrics',
			verify: 'curl -fsS localhost:9090/-/healthy'
		}
	});

	// The row paints the reconciled metadata without a refetch.
	await expect(metricsRow.getByTestId('service-category-chip')).toHaveAttribute(
		'data-category',
		'games'
	);
	await expect(metricsRow.getByTestId('service-description')).toHaveText('Relocated to games.');
	await expect(metricsRow.locator('[data-step="install"]')).toHaveText('make deploy');

	// Stale: the record moved elsewhere; the editor still ships the observed
	// revision, the producer refuses, the row stays and the error is honest.
	await apiCall(page, 'setRevision', [APP_ADOPTED, 9]);
	await metricsRow.getByTestId('service-edit-start').click();
	await hostA.getByTestId('service-edit-save').click();
	const editError = hostA.getByTestId('service-edit-error');
	await expect(editError).toBeVisible();
	await expect(editError).toHaveAttribute('data-error-code', 'conflict');
	await expect(hostA.getByTestId('service-edit-form')).toBeVisible();
	await expect(metricsRow.getByTestId('service-category-chip')).toHaveAttribute(
		'data-category',
		'games'
	);
});
