import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-07-web-docs — real-browser proofs for the documents panel.
 *
 * The harness compiles the REAL feature panel (`DocumentsPanel.svelte`), the
 * REAL feature entry (`07-web-docs.ts`), the REAL panel host/registry and the
 * shared UI primitives into a throwaway fixture app served from virtual
 * modules — nothing hits disk and no real backend exists. The fixture API
 * mirrors the reviewed producers 1:1 (`07-docs-api` documents surface +
 * the D07 session-first `attachToSession` seam over
 * `/api/v1/agent-sessions`); recorded calls
 * and pre-armed responses are the test-observed boundary (the reviewed
 * `01-web` component-browser fixture convention, not an imaginary backend).
 *
 * Covered acceptance surfaces: scoped browse/search over CONFLICTING server
 * fixtures, citation opening, revision-checked source edits + index
 * reconcile, honest outage/generate/research states, excerpt attach to chat,
 * inert hostile Markdown, stale-response immunity, fail-closed bags/scopes.
 */

const FIXTURE_URL = '/mai-docs-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-docs-app.svelte';
const ENTRY_ID = 'virtual:mai-docs-entry.js';

const PANEL = `${FRONTEND_ROOT}src/lib/panels/DocumentsPanel.svelte`;
const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/07-web-docs.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
const DOC_A1 = '11a11111-1111-4222-8333-444455556666';
const DOC_A2 = '22a22222-2222-4222-8333-444455556666';
const DOC_A3 = '33a33333-3333-4222-8333-444455556666';
const DOC_B1 = '44b44444-4444-4222-8333-444455556666';
const DOC_GEN = '55c55555-5555-4222-8333-444455556666';
const RUN_EVIDENCE = '66d66666-6666-4222-8333-444455556666';
const WINDOW_A = '7a7a7a7a-8c9d-4e8a-8a9b-4c5d6e7f8a9c';
const WINDOW_B = '8b8b8b8b-8c9d-4e8a-8a9b-4c5d6e7f8a9d';
const WINDOW_GLOBAL = '9c9c9c9c-9999-4222-8333-444455556666';
const WINDOW_BARE = '0d0d0d0d-9999-4222-8333-444455556666';
const WINDOW_SIBLING = '1e1e1e1e-9999-4222-8333-444455556666';
const WINDOW_NOCHAT = '2f2f2f2f-9999-4222-8333-444455556666';

/** Untruncated runtime-failure receipt: a page error is a harness defect. */
const pageErrors: string[] = [];

type Fail = { status: number; code: string; message: string };

interface DocsWindowApi {
	calls: Array<{ op: string; payload?: unknown; args?: unknown[] }>;
	failNextList(failure: Fail | null): void;
	failNextRead(failure: Fail | null): void;
	failNextSubmit(failure: Fail | null): void;
	failNextRetrieveUnavailable(code: string | null): void;
	armDeferredRetrieve(): void;
	releaseRetrieve(result: unknown): void;
	armDeferredRead(): void;
	releaseRead(view: unknown): void;
	armReindexOutcomes(outcomes: unknown[] | null): void;
	armResearch(report: unknown | null): void;
	setDocRevision(docId: string, revision: number): void;
	toggleHostA(): void;
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
		__maiDocsApi?: DocsWindowApi;
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
	import { DOCS_PANEL_KIND, docsEntry, docsServices } from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const SERVER_B = '${SERVER_B}';
	const DOC_A1 = '${DOC_A1}';
	const DOC_A2 = '${DOC_A2}';
	const DOC_A3 = '${DOC_A3}';
	const DOC_B1 = '${DOC_B1}';
	const DOC_GEN = '${DOC_GEN}';

	// Conflicting fixture pair: DOC_A1 (Server Alpha) and DOC_B1 (Server Beta)
	// share the import origin 'import:nginx-reload.md' but diverge in revision,
	// hash and content — the storage-boundary scope filter (C07 rule 2) is what
	// keeps each window's answers on its own side.
	const HOSTILE = [
		'# Postmortem (hostile fixture)',
		'',
		'Normal prose before the payloads.',
		'',
		'<script>window.__maiXssFired = true;<\\/script>',
		'<img src=x onerror="window.__maiXssFired = true">',
		'<iframe src="javascript:window.__maiXssFired = true"><\\/iframe>',
		'[clickme](javascript:window.__maiXssFired = true)',
		'',
		'Inline code with tags: \`<b>not bold</b>\` and trailing prose.'
	].join('\\n');

	function docRow(docId, namespace, revision, sha, verification, origin, content) {
		return {
			doc_id: docId,
			namespace: namespace,
			revision: revision,
			sha256: sha,
			source_kind: 'authored',
			origin: origin,
			origin_revision: null,
			verification: verification,
			observed_at: '2026-10-16T08:00:00Z',
			source_path: null,
			content: content
		};
	}

	const docs = {};
	docs[SERVER_A] = {};
	docs[SERVER_A][DOC_A1] = docRow(
		DOC_A1, 'runbooks', 3, 'a'.repeat(64), 'verified', 'import:nginx-reload.md',
		'# Reload nginx (Server Alpha variant)\\n\\nAlpha reloads via systemctl: sudo systemctl reload nginx.\\nAlpha-only note: validate with nginx -t first.'
	);
	docs[SERVER_A][DOC_A2] = docRow(
		DOC_A2, 'docs', 1, 'b'.repeat(64), 'unverified', 'import:deploy.md',
		'# Deploy runbook (Alpha)\\n\\nDeploy steps for the Alpha pipeline; unique deploy marker ALPHA-DEPLOY-9.'
	);
	docs[SERVER_A][DOC_A3] = docRow(
		DOC_A3, 'docs', 2, 'd'.repeat(64), 'hypothesis', 'operator', HOSTILE
	);
	docs[SERVER_B] = {};
	docs[SERVER_B][DOC_B1] = docRow(
		DOC_B1, 'runbooks', 7, 'c'.repeat(64), 'verified', 'import:nginx-reload.md',
		'# Reload nginx (Server Beta variant)\\n\\nBeta uses openresty: openresty -s reload.\\nBeta-only note: the revision path diverges here.'
	);

	const chunkCounts = {};
	chunkCounts[DOC_A1] = 3;
	chunkCounts[DOC_A2] = 2;
	chunkCounts[DOC_A3] = 1;
	chunkCounts[DOC_B1] = 4;

	let intents = [];

	function headOf(doc) {
		return {
			doc_id: doc.doc_id,
			server_id: null,
			app_id: null,
			namespace: doc.namespace,
			revision: doc.revision,
			sha256: doc.sha256,
			citation: doc.namespace + ':' + doc.doc_id + '#r' + doc.revision,
			source_kind: doc.source_kind,
			origin: doc.origin,
			origin_revision: doc.origin_revision,
			verification: doc.verification
		};
	}

	function findDoc(serverId, docId) {
		const scope = docs[serverId];
		return scope ? scope[docId] : null;
	}

	function findDocAnywhere(docId) {
		for (const scope of Object.values(docs)) {
			if (scope[docId]) return scope[docId];
		}
		return null;
	}

	// --- recorded calls + armed behavior -------------------------------------

	const calls = [];
	let failNextList = null;
	let failNextRead = null;
	let failNextSubmit = null;
	let retrieveUnavailableArm = null;
	// Gate lifecycle: arm* parks the NEXT call; the call moves the gate into
	// the pending* slot where release* finds it — the gate must survive
	// consumption, or the released payload never reaches the panel and the
	// stale-painting assertions turn vacuously green (caught by mutation M2).
	let deferredRetrieve = null;
	let pendingRetrieve = null;
	let deferredRead = null;
	let pendingRead = null;
	let reindexArm = null;
	let researchArm = null;

	function record(op, payload, args) {
		calls.push({ op: op, payload: payload, args: args });
	}

	function throwNext(armed) {
		if (armed) {
			const failure = armed;
			armed = null;
			throw new ApiError(failure.status, failure.code, failure.message);
		}
	}

	function excerptOf(doc, query) {
		const clipped = doc.content.length > 80;
		return {
			document_key: 'k-' + doc.doc_id,
			doc_id: doc.doc_id,
			namespace: doc.namespace,
			revision: doc.revision,
			sha256: doc.sha256,
			ord: 0,
			citation: doc.namespace + ':' + doc.doc_id + '#r' + doc.revision + ':0',
			source_kind: doc.source_kind,
			origin: doc.origin,
			origin_revision: doc.origin_revision,
			verification: doc.verification,
			observed_at: doc.observed_at,
			score: 0.5,
			vector_rank: 1,
			fts_rank: 1,
			excerpt: clipped ? doc.content.slice(0, 80) : doc.content,
			truncated: clipped
		};
	}

	function searchScope(payload) {
		const tokens = String(payload.query).toLowerCase().split(/\\s+/).filter(Boolean);
		const scope = docs[payload.server_id] || {};
		const hits = [];
		for (const doc of Object.values(scope)) {
			const haystack = (doc.namespace + ' ' + doc.content).toLowerCase();
			const matched = tokens.every((token) => haystack.includes(token));
			if (matched) hits.push(excerptOf(doc, payload.query));
		}
		return {
			status: 'ok',
			code: null,
			scope: payload.shared ? 'shared' : 'server',
			server_id: payload.server_id,
			excerpts: hits.slice(0, payload.limit),
			error_type: null
		};
	}

	const api = {
		async listDocuments(serverId, page) {
			record('listDocuments', page ? JSON.parse(JSON.stringify(page)) : null, [serverId]);
			throwNext(failNextList);
			const scope = docs[serverId] || {};
			const documents = Object.values(scope).map((doc) => {
				const head = headOf(doc);
				head.server_id = serverId;
				head.observed_at = doc.observed_at;
				head.source_path = doc.source_path;
				return head;
			});
			return {
				server_id: serverId,
				app_id: null,
				namespace: null,
				documents: documents
			};
		},
		async readDocument(serverId, docId, namespace) {
			record('readDocument', null, [serverId, docId, namespace ?? null]);
			throwNext(failNextRead);
			if (deferredRead) {
				pendingRead = deferredRead;
				deferredRead = null;
				const view = await pendingRead.promise;
				return JSON.parse(JSON.stringify(view));
			}
			const doc = findDoc(serverId, docId);
			if (!doc || (namespace && doc.namespace !== namespace)) {
				// Naming another scope finds nothing — scopes never leak (C07 rule 2).
				throw new ApiError(404, 'not_found', 'no canonical document stored for this document id and scope');
			}
			const view = headOf(doc);
			view.content = doc.content;
			return JSON.parse(JSON.stringify(view));
		},
		async editDocument(docId, payload) {
			record('editDocument', JSON.parse(JSON.stringify(payload)), [docId]);
			const doc = findDoc(payload.server_id, docId);
			if (!doc) {
				throw new ApiError(404, 'not_found', 'no canonical document stored for this document id and scope');
			}
			if (payload.expected_revision !== doc.revision) {
				// Stale caller rejected BEFORE any write (C01 rule 6).
				throw new ApiError(409, 'conflict', 'stale revision or canonical state conflict');
			}
			doc.content = payload.content;
			doc.revision = doc.revision + 1;
			doc.sha256 = 'f'.repeat(64);
			intents.push({
				server_id: payload.server_id,
				doc_id: docId,
				namespace: doc.namespace,
				op: 'upsert',
				revision: doc.revision,
				sha256: doc.sha256,
				tombstone: false,
				status: 'pending'
			});
			return JSON.parse(JSON.stringify(headOf(doc)));
		},
		async retrieve(payload) {
			record('retrieve', JSON.parse(JSON.stringify(payload)), null);
			if (retrieveUnavailableArm) {
				const code = retrieveUnavailableArm;
				retrieveUnavailableArm = null;
				return {
					status: 'unavailable',
					code: code,
					scope: 'server',
					server_id: payload.server_id,
					excerpts: [],
					error_type: null
				};
			}
			if (deferredRetrieve) {
				pendingRetrieve = deferredRetrieve;
				deferredRetrieve = null;
				const result = await pendingRetrieve.promise;
				return JSON.parse(JSON.stringify(result));
			}
			return searchScope(payload);
		},
		async readIndex(serverId) {
			record('readIndex', null, [serverId]);
			const scope = docs[serverId] || {};
			const projection = Object.values(scope).map((doc) => ({
				doc_id: doc.doc_id,
				namespace: doc.namespace,
				revision: doc.revision,
				sha256: doc.sha256,
				chunks: chunkCounts[doc.doc_id] ?? 0,
				tombstone: false,
				observed_at: doc.observed_at,
				status: 'indexed'
			}));
			return {
				server_id: serverId,
				app_id: null,
				namespace: null,
				pending: JSON.parse(JSON.stringify(intents)),
				projection: projection
			};
		},
		async reconcileIndex(payload) {
			record('reconcileIndex', JSON.parse(JSON.stringify(payload)), null);
			const outcomes = [];
			const remaining = [];
			for (const intent of intents) {
				// The producer drains exactly the requested scope's intents; other
				// scopes' intents stay pending (C07 rule 2 at the outbox boundary).
				const inScope = payload.shared
					? intent.server_id === null
					: intent.server_id === payload.server_id;
				if (inScope) {
					outcomes.push({
						doc_id: intent.doc_id,
						namespace: intent.namespace,
						op: intent.op,
						status: 'indexed',
						revision: intent.revision,
						chunks: chunkCounts[intent.doc_id] ?? 2,
						code: null
					});
				} else {
					remaining.push(intent);
				}
			}
			intents = remaining;
			return {
				server_id: payload.server_id,
				app_id: payload.app_id,
				outcomes: outcomes
			};
		},
		async generateDocument(payload) {
			record('generateDocument', JSON.parse(JSON.stringify(payload)), null);
			const evidence = payload.evidence;
			const classification = evidence.status === 'failed' ? 'incident' : 'runbook';
			const namespace = classification === 'runbook' ? 'runbooks' : 'docs';
			const verification = evidence.host_verified === true ? 'verified' : 'unverified';
			if (payload.preview) {
				return {
					preview: true,
					namespace: namespace,
					classification: classification,
					verification: verification,
					content:
						'# ' + (classification === 'runbook' ? 'Runbook' : 'Incident') +
						' (generated preview for run ' + evidence.run_id + ')\\n\\n' +
						String(evidence.summary ?? '')
				};
			}
			const doc = docRow(
				DOC_GEN, namespace, 1, 'e'.repeat(64), verification, 'generator', 'GENERATED BODY'
			);
			docs[SERVER_A][DOC_GEN] = doc;
			intents.push({
				server_id: SERVER_A,
				doc_id: DOC_GEN,
				namespace: namespace,
				op: 'upsert',
				revision: 1,
				sha256: doc.sha256,
				tombstone: false,
				status: 'pending'
			});
			return {
				preview: false,
				doc: headOf(doc),
				classification: classification,
				verification: verification,
				reindex: reindexArm === null ? null : JSON.parse(JSON.stringify(reindexArm))
			};
		},
		async runResearch(payload) {
			record('runResearch', JSON.parse(JSON.stringify(payload)), null);
			if (researchArm) {
				const report = researchArm;
				researchArm = null;
				return JSON.parse(JSON.stringify(report));
			}
			return {
				v: 1,
				run_id: '77e77777-7777-4222-8333-444455556666',
				server_id: payload.server_id,
				status: 'succeeded',
				question: payload.payload.question,
				prompt_revision: '3',
				prompt_sha256: '9'.repeat(64),
				profile_revision: '5',
				error: null,
				steps: [
					{
						seq: 1,
						phase: 'retrieve',
						capability_id: 'docs.retrieve',
						status: 'succeeded',
						display: 'searched the cited docs of the pinned server',
						evidence_refs: []
					}
				],
				sources: [
					{
						kind: 'doc',
						citation: 'runbooks:' + DOC_A1 + '#r3:0',
						trust: 'cited-doc',
						status: 'ok',
						truncated: false,
						error_type: null,
						artifact_refs: [],
						text: 'Alpha reloads via systemctl: sudo systemctl reload nginx.'
					}
				],
				hypotheses: [
					{
						claim: 'The Alpha host reloads nginx through systemd.',
						basis: ['runbooks:' + DOC_A1 + '#r3:0']
					}
				],
				dropped_claims: ['An unbacked claim without any citation.'],
				incident: null,
				incident_error: null
			};
		}
	};

	async function attachToSession(payload) {
		record('attachToSession', JSON.parse(JSON.stringify(payload)), null);
		throwNext(failNextSubmit);
		// The session-first admission: create-new (session_id null) then ONE
		// idempotent turn; the fixture returns the durable identities.
		return {
			session_id: payload.session_id ?? 'a1a1a1a1-a1a1-4a11-8a11-a1a1a1a1a1a1',
			run_id: 'b2b2b2b2-b2b2-4b22-8b22-b2b2b2b2b2b2',
			entry_id: 'c3c3c3c3-c3c3-4c33-8c33-c3c3c3c3c3c3',
			seq: payload.session_id ? 61 : 1,
			deduplicated: false
		};
	}

	const services = docsServices(api, { attachToSession: attachToSession });
	const servicesNoChat = docsServices(api, null);

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
		docsEntry(),
		{
			panel_kind: 'fixture-notes',
			title: 'Notes',
			scope_types: ['server'],
			component: () => Promise.resolve(sibling)
		}
	]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	const registry = registryResult.value;
	const entryCheck = definePanelEntry(docsEntry());
	if (!entryCheck.ok) throw new Error('feature entry rejected: ' + entryCheck.issue.code);

	// --- hosts ----------------------------------------------------------------
	let showA = $state(true);

	function descriptor(windowId, scope, z, kind) {
		return {
			schema_version: 1,
			window_id: windowId,
			panel_kind: kind ?? DOCS_PANEL_KIND,
			scope,
			geometry: { x: 8, y: 8, width: 860, height: 660 },
			presentation: 'normal',
			z,
			title_override: null
		};
	}

	function onPanelError(windowId, panelKind, error) {
		console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error);
	}

	const windowApi = {
		calls,
		failNextList(failure) {
			failNextList = failure;
		},
		failNextRead(failure) {
			failNextRead = failure;
		},
		failNextSubmit(failure) {
			failNextSubmit = failure;
		},
		failNextRetrieveUnavailable(code) {
			retrieveUnavailableArm = code;
		},
		armDeferredRetrieve() {
			deferredRetrieve = Promise.withResolvers();
		},
		releaseRetrieve(result) {
			const entry = pendingRetrieve;
			pendingRetrieve = null;
			deferredRetrieve = null;
			if (entry) entry.resolve(JSON.parse(JSON.stringify(result)));
		},
		armDeferredRead() {
			deferredRead = Promise.withResolvers();
		},
		releaseRead(view) {
			const entry = pendingRead;
			pendingRead = null;
			deferredRead = null;
			if (entry) entry.resolve(JSON.parse(JSON.stringify(view)));
		},
		armReindexOutcomes(outcomes) {
			reindexArm = outcomes;
		},
		armResearch(report) {
			researchArm = report;
		},
		setDocRevision(docId, revision) {
			const doc = findDocAnywhere(docId);
			if (doc) doc.revision = revision;
		},
		toggleHostA() {
			showA = !showA;
		},
		registry: (() => {
			function checkCode(kind, scope) {
				const result = registry.checkScope(kind, scope);
				return result.ok ? 'ok' : result.issue.code;
			}
			return {
				kinds: Array.from(registry.knownPanelKinds()).sort(),
				scopeChecks: {
					serverScope: checkCode(DOCS_PANEL_KIND, { kind: 'server', server_id: SERVER_A }),
					globalScope: checkCode(DOCS_PANEL_KIND, { kind: 'global' }),
					unknownKind: checkCode('not-a-panel', { kind: 'server', server_id: SERVER_A })
				},
				defaultGeometry: registry.defaultGeometry(DOCS_PANEL_KIND),
				minGeometry: registry.minGeometry(DOCS_PANEL_KIND),
				instanceKey: singletonInstanceKey(DOCS_PANEL_KIND, entryCheck.value, {
					kind: 'server',
					server_id: SERVER_A
				})
			};
		})()
	};
	window.__maiDocsApi = windowApi;
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
<section data-testid="host-sibling" aria-label="Host sibling">
	<ComponentHost label="Host sibling">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_SIBLING}', { kind: 'server', server_id: SERVER_B }, 3, 'fixture-notes')}
		/>
	</ComponentHost>
</section>
<section data-testid="host-global" aria-label="Host global">
	<ComponentHost label="Host global">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_GLOBAL}', { kind: 'global' }, 4)}
			{services}
		/>
	</ComponentHost>
</section>
<section data-testid="host-bare" aria-label="Host bare">
	<ComponentHost label="Host bare">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_BARE}', { kind: 'server', server_id: SERVER_A }, 5)}
		/>
	</ComponentHost>
</section>
<section data-testid="host-nochat" aria-label="Host without chat seam">
	<ComponentHost label="Host nochat">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_NOCHAT}', { kind: 'server', server_id: SERVER_A }, 6)}
			services={servicesNoChat}
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
// Build the harness once per worker (same shape as the reviewed jobs fixture)
// ---------------------------------------------------------------------------

/** Serves the virtual modules and the built lazy chunks; nothing hits disk. */
function harnessPlugin(sources: Record<string, string>): Plugin {
	return {
		name: 'mai-docs-harness',
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
			outDir: 'node_modules/.mai-docs-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'docs-[hash].js',
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
<title>MAI docs fixture</title>
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
// Fixture-API boundary (serialized dispatch; same convention as jobs spec)
// ---------------------------------------------------------------------------

/** Call one fixture-api method inside the page; args must be serializable. */
async function apiCall(page: Page, method: string, args: unknown[] = []): Promise<void> {
	const pair: [string, unknown[]] = [method, args];
	await page.evaluate(([name, callArgs]) => {
		const value = window.__maiDocsApi;
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
	registry: DocsWindowApi['registry'];
}> {
	return page.evaluate(() => {
		const value = window.__maiDocsApi;
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

/** Both docs windows mounted (lazy chunk fetched through the real loader). */
async function bothWindowsMounted(page: Page): Promise<void> {
	await expect(page.getByTestId('host-a').getByTestId('docs-root')).toBeVisible();
	await expect(page.getByTestId('host-b').getByTestId('docs-root')).toBeVisible();
	await expect(page.getByTestId('host-a').getByTestId('library-list')).toBeVisible();
	await expect(page.getByTestId('host-b').getByTestId('library-list')).toBeVisible();
	expect(servedChunks.length).toBeGreaterThan(0);
}

// ---------------------------------------------------------------------------

test('library lists only the pinned scope; open renders the cited document beside a sibling panel', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	const hostB = page.getByTestId('host-b');
	await bothWindowsMounted(page);

	// The desktop holds the documents panel BESIDE a sibling panel.
	await expect(page.getByTestId('host-sibling').getByTestId('sibling-panel')).toBeVisible();

	// Server Alpha's window: Alpha rows only — the conflicting Beta document
	// (same import origin, different revision) never appears.
	const rowsA = hostA.getByTestId('library-row');
	await expect(rowsA).toHaveCount(3);
	await expect(rowsA.filter({ hasText: DOC_A1 })).toHaveCount(1);
	await expect(rowsA.filter({ hasText: 'runbooks:' + DOC_A1 + '#r3' })).toHaveCount(1);
	await expect(rowsA.filter({ hasText: DOC_B1 })).toHaveCount(0);
	await expect(hostA.getByTestId('docs-scope')).toContainText(SERVER_A.slice(0, 8));

	// Server Beta's window: the Beta variant only.
	const rowsB = hostB.getByTestId('library-row');
	await expect(rowsB).toHaveCount(1);
	await expect(rowsB.filter({ hasText: 'runbooks:' + DOC_B1 + '#r7' })).toHaveCount(1);
	await expect(rowsB.filter({ hasText: DOC_A1 })).toHaveCount(0);

	// Open the cited document in Alpha's window.
	await rowsA.filter({ hasText: DOC_A1 }).getByTestId('library-open').click();
	const content = hostA.getByTestId('document-content');
	await expect(content).toContainText('Server Alpha variant');
	await expect(hostA.getByTestId('document-citation')).toHaveText('runbooks:' + DOC_A1 + '#r3');
	await expect(hostA.getByTestId('document-revision')).toHaveText('r3');
	await expect(hostA.getByTestId('document-verification')).toHaveText('verified');

	const { calls } = await apiState(page);
	const read = lastCall(calls, 'readDocument');
	expect(read.args).toEqual([SERVER_A, DOC_A1, 'runbooks']);
});

test('hybrid retrieval answers with cited excerpts of the pinned scope only (conflicting fixtures)', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	const hostB = page.getByTestId('host-b');
	await bothWindowsMounted(page);

	// Same query in both windows; the fixture store holds CONFLICTING
	// same-origin documents per server — each window must see only its own.
	for (const host of [hostA, hostB]) {
		await host.getByTestId('tab-search').click();
		await host.getByTestId('search-query').fill('reload nginx');
		await host.getByTestId('search-submit').click();
	}

	const resultA = hostA.getByTestId('search-result');
	await expect(resultA).toHaveCount(1);
	await expect(hostA.getByTestId('excerpt-citation')).toHaveText('runbooks:' + DOC_A1 + '#r3:0');
	await expect(hostA.getByTestId('excerpt-meta')).toContainText('r3');
	await expect(hostA.getByTestId('excerpt-text')).toContainText('Server Alpha variant');
	await expect(hostA.getByTestId('excerpt-verification')).toHaveText('verified');

	const resultB = hostB.getByTestId('search-result');
	await expect(resultB).toHaveCount(1);
	await expect(hostB.getByTestId('excerpt-citation')).toHaveText('runbooks:' + DOC_B1 + '#r7:0');
	await expect(hostB.getByTestId('excerpt-text')).toContainText('Server Beta variant');
	await expect(hostB.getByTestId('excerpt-text')).not.toContainText('Server Alpha variant');

	// Open the citation: the read carries the excerpt's doc id AND namespace.
	await hostA.getByTestId('excerpt-open').click();
	await expect(hostA.getByTestId('document-content')).toContainText('Server Alpha variant');
	const { calls } = await apiState(page);
	const retrieveA = calls.filter((call) => call.op === 'retrieve')[0];
	expect(retrieveA.payload).toMatchObject({ server_id: SERVER_A, query: 'reload nginx' });
	const retrieveB = calls.filter((call) => call.op === 'retrieve')[1];
	expect(retrieveB.payload).toMatchObject({ server_id: SERVER_B, query: 'reload nginx' });
	const read = lastCall(calls, 'readDocument');
	expect(read.args).toEqual([SERVER_A, DOC_A1, 'runbooks']);
});

test('a retrieval outage names the failed seam and never renders an empty success', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await bothWindowsMounted(page);

	await apiCall(page, 'failNextRetrieveUnavailable', ['embedding_unavailable']);
	await hostA.getByTestId('tab-search').click();
	await hostA.getByTestId('search-query').fill('anything');
	await hostA.getByTestId('search-submit').click();

	await expect(hostA.getByTestId('search-unavailable')).toBeVisible();
	await expect(hostA.getByTestId('search-unavailable-code')).toHaveText('embedding_unavailable');
	await expect(hostA.getByTestId('search-unavailable')).toContainText('embedding seam is unavailable');
	await expect(hostA.getByTestId('search-result')).toHaveCount(0);
	await expect(hostA.getByTestId('search-empty')).toHaveCount(0);
});

test('edit source is revision-checked; a stale revision is refused with an honest conflict', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await bothWindowsMounted(page);

	// Open, edit, save at the current revision.
	await hostA
		.getByTestId('library-row')
		.filter({ hasText: DOC_A1 })
		.getByTestId('library-open')
		.click();
	await expect(hostA.getByTestId('document-content')).toBeVisible();
	await hostA.getByTestId('document-edit').click();
	const textarea = hostA.getByTestId('document-edit-textarea');
	await expect(textarea).toHaveValue(/systemctl reload nginx/);
	await textarea.fill('# Reload nginx (Alpha, edited)\n\nEdited by the browser proof.');
	await hostA.getByTestId('document-edit-save').click();

	await expect(hostA.getByTestId('document-edit-saved')).toContainText('r4');
	await expect(hostA.getByTestId('document-citation')).toHaveText('runbooks:' + DOC_A1 + '#r4');
	await expect(hostA.getByTestId('document-content')).toContainText('Edited by the browser proof');
	// The list refreshed to the new revision.
	await expect(
		hostA.getByTestId('library-row').filter({ hasText: 'runbooks:' + DOC_A1 + '#r4' })
	).toHaveCount(1);

	const { calls } = await apiState(page);
	const edit = lastCall(calls, 'editDocument');
	// Test-authored wire boundary: the fixture structuredClone-d the exact
	// body the panel sent; the recorded payload IS this shape by construction.
	const editBody = edit.payload as {
		server_id: string;
		namespace: string;
		expected_revision: number;
		content: string;
	};
	expect(editBody).toMatchObject({
		server_id: SERVER_A,
		namespace: 'runbooks',
		expected_revision: 3
	});
	expect(editBody.content).toContain('Edited by the browser proof');

	// A stale caller is rejected BEFORE any write (C01 rule 6): bump the
	// canonical revision out-of-band, replay the edit from the stale view.
	await apiCall(page, 'setDocRevision', [DOC_A1, 9]);
	await hostA.getByTestId('document-edit').click();
	await hostA.getByTestId('document-edit-textarea').fill('Stale attempt.');
	await hostA.getByTestId('document-edit-save').click();
	await expect(hostA.getByTestId('document-edit-error')).toBeVisible();
	await expect(hostA.getByTestId('document-edit-error')).toHaveAttribute('data-error-code', 'conflict');
	await expect(hostA.getByTestId('document-edit-error')).toContainText(
		'changed at another revision'
	);
	// The failed session cleared the earlier saved note (fresh edit session).
	await expect(hostA.getByTestId('document-edit-saved')).toHaveCount(0);
	await expect(hostA.getByTestId('document-citation')).toHaveText('runbooks:' + DOC_A1 + '#r4');
});

test('index state shows pending intents and projection; reconcile reports producer outcomes', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await bothWindowsMounted(page);

	// Produce a durable intent by editing a source document.
	await hostA
		.getByTestId('library-row')
		.filter({ hasText: DOC_A2 })
		.getByTestId('library-open')
		.click();
	await expect(hostA.getByTestId('document-content')).toBeVisible();
	await hostA.getByTestId('document-edit').click();
	await hostA.getByTestId('document-edit-textarea').fill('# Deploy (Alpha)\n\nRefreshed body.');
	await hostA.getByTestId('document-edit-save').click();
	await expect(hostA.getByTestId('document-edit-saved')).toContainText('r2');

	// Index tab: the pending intent is explicit (never "unknown").
	await hostA.getByTestId('tab-index').click();
	await expect(hostA.getByTestId('index-pending-list')).toBeVisible();
	const pendingRow = hostA.getByTestId('index-pending-row').filter({ hasText: DOC_A2.slice(0, 8) });
	await expect(pendingRow).toContainText('upsert');
	await expect(pendingRow).toContainText('pending');
	await expect(hostA.getByTestId('index-projection-row').filter({ hasText: DOC_A1.slice(0, 8) }))
		.toContainText('3 chunks');

	// Reindex now: per-intent producer outcomes, verbatim.
	await hostA.getByTestId('index-reconcile').click();
	const reconcileRow = hostA.getByTestId('reconcile-row').filter({ hasText: DOC_A2.slice(0, 8) });
	await expect(reconcileRow).toBeVisible();
	await expect(reconcileRow.getByTestId('reconcile-status')).toHaveText('indexed');
	await expect(reconcileRow).toContainText('chunks');
	// The outbox drains; the projection keeps its indexed truth.
	await expect(hostA.getByTestId('index-pending-empty')).toBeVisible();

	const { calls } = await apiState(page);
	const reconcile = lastCall(calls, 'reconcileIndex');
	expect(reconcile.payload).toMatchObject({ server_id: SERVER_A, shared: false });
});

test('generation previews without writing; commit reports the reindex outcome honestly', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await bothWindowsMounted(page);

	const evidence = JSON.stringify({
		v: 1,
		run_id: RUN_EVIDENCE,
		server_id: SERVER_A,
		status: 'succeeded',
		host_verified: true,
		summary: 'Reloaded nginx after certificate renewal; health check verified.',
		error: null,
		steps: [
			{
				seq: 1,
				capability_id: 'exec.run',
				status: 'succeeded',
				verified: true,
				display: 'systemctl reload nginx',
				exit_code: 0,
				evidence_refs: []
			}
		]
	});

	const listCallsBefore = (await apiState(page)).calls.filter(
		(call) => call.op === 'listDocuments'
	).length;
	await hostA.getByTestId('tab-generate').click();
	await hostA.getByTestId('generate-evidence').fill(evidence);
	await hostA.getByTestId('generate-preview').click();

	// Preview: classification from run truth; NOTHING was written — the panel
	// issued no store effect and did not even refresh the list.
	await expect(hostA.getByTestId('generate-preview-view')).toContainText('nothing was written');
	await expect(hostA.getByTestId('generate-preview-classification')).toHaveText(
		'runbook · runbooks · verified'
	);
	await expect(hostA.getByTestId('generate-preview-content')).toContainText('generated preview');
	const afterPreview = await apiState(page);
	expect(
		afterPreview.calls.filter((call) => call.op === 'listDocuments')
	).toHaveLength(listCallsBefore);

	// Commit: the canonical document is created; the reindex outcome is the
	// producer's honest null (no indexer wired on this surface).
	await hostA.getByTestId('generate-commit').click();
	await expect(hostA.getByTestId('generate-commit-view')).toBeVisible();
	await expect(hostA.getByTestId('generate-commit-citation')).toContainText('runbooks:' + DOC_GEN);
	await expect(hostA.getByTestId('generate-reindex-null')).toContainText('not wired');
	await expect(hostA.getByTestId('generate-reindex-null')).toContainText('next reconcile');
	// The library refreshed with the committed document (one extra list read).
	const afterCommit = await apiState(page);
	expect(
		afterCommit.calls.filter((call) => call.op === 'listDocuments')
	).toHaveLength(listCallsBefore + 1);
	// DOM proof: back on the library tab the committed document is listed.
	await hostA.getByTestId('tab-library').click();
	await expect(
		hostA.getByTestId('library-row').filter({ hasText: 'runbooks:' + DOC_GEN + '#r1' })
	).toHaveCount(1);

	const { calls } = await apiState(page);
	const preview = calls.filter((call) => call.op === 'generateDocument')[0];
	expect(preview.payload).toMatchObject({ preview: true });
	const commit = calls.filter((call) => call.op === 'generateDocument')[1];
	expect(commit.payload).toMatchObject({ preview: false });
});

test('research renders the settled report; a failed run shows its canonical incident, never a procedure', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await bothWindowsMounted(page);

	await hostA.getByTestId('tab-research').click();
	await hostA.getByTestId('research-question').fill('how do we reload nginx safely?');
	await hostA.getByTestId('research-submit').click();

	// Settled pass: sources, hypotheses and dropped claims stay distinct.
	await expect(hostA.getByTestId('research-report')).toBeVisible();
	await expect(hostA.getByTestId('research-status')).toHaveText('succeeded');
	await expect(hostA.getByTestId('research-question-echo')).toContainText('reload nginx safely');
	await expect(hostA.getByTestId('research-step')).toHaveCount(1);
	await expect(hostA.getByTestId('research-step')).toContainText('docs.retrieve');
	const source = hostA.getByTestId('research-source');
	await expect(source).toHaveCount(1);
	await expect(source.getByTestId('research-source-citation')).toContainText('runbooks:' + DOC_A1);
	await expect(source).toHaveAttribute('data-status', 'ok');
	await expect(hostA.getByTestId('research-source-text')).toContainText('systemctl reload nginx');
	await expect(hostA.getByTestId('research-hypothesis')).toContainText('systemd');
	await expect(hostA.getByTestId('research-dropped-claim')).toContainText('unbacked claim');
	await expect(hostA.getByTestId('research-incident')).toHaveCount(0);

	// A failed run promotes exactly one canonical incident (C07 rule 5).
	await apiCall(page, 'armResearch', [
		{
			v: 1,
			run_id: '88f88888-8888-4222-8333-444455556666',
			server_id: SERVER_A,
			status: 'failed',
			question: 'how do we reload nginx safely?',
			prompt_revision: '3',
			prompt_sha256: '9'.repeat(64),
			profile_revision: '5',
			error: 'the docs channel denied the retrieval call',
			steps: [
				{
					seq: 1,
					phase: 'retrieve',
					capability_id: 'docs.retrieve',
					status: 'failed',
					display: 'denied by policy',
					evidence_refs: []
				}
			],
			sources: [],
			hypotheses: [],
			dropped_claims: [],
			incident: { doc_id: DOC_A2, namespace: 'docs' },
			incident_error: null
		}
	]);
	await hostA.getByTestId('research-question').fill('and now a failing pass');
	await hostA.getByTestId('research-submit').click();

	await expect(hostA.getByTestId('research-status')).toHaveText('failed');
	await expect(hostA.getByTestId('research-run-error')).toContainText('denied the retrieval');
	await expect(hostA.getByTestId('research-incident')).toContainText('incident was recorded');
	await expect(hostA.getByTestId('research-incident')).toContainText('no runbook success was fabricated');
	await expect(hostA.getByTestId('research-source')).toHaveCount(0);

	const { calls } = await apiState(page);
	const research = lastCall(calls, 'runResearch');
	expect(research.payload).toMatchObject({
		server_id: SERVER_A,
		payload: { v: 1, synthesize: false }
	});
});

test('attach composes ONE durable session admission carrying the excerpt under its citation', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await bothWindowsMounted(page);

	// Produce an excerpt, then attach it.
	await hostA.getByTestId('tab-search').click();
	await hostA.getByTestId('search-query').fill('reload nginx');
	await hostA.getByTestId('search-submit').click();
	await expect(hostA.getByTestId('search-result')).toHaveCount(1);
	await hostA.getByTestId('excerpt-attach').click();

	const attach = hostA.getByTestId('docs-attach');
	await expect(attach.getByTestId('attach-citation')).toHaveText('runbooks:' + DOC_A1 + '#r3:0');
	await expect(attach.getByTestId('attach-excerpt')).toContainText('Server Alpha variant');

	// Local pre-flight: profile is required; nothing is submitted without it.
	await attach.getByTestId('attach-send').click();
	await expect(attach.getByText('A model profile is required.')).toBeVisible();
	const before = (await apiState(page)).calls.filter((call) => call.op === 'attachToSession');
	expect(before).toHaveLength(0);

	await attach.getByTestId('attach-profile').fill('mai-primary');
	await attach.getByTestId('attach-note').fill('Please apply on the pinned server.');
	await attach.getByTestId('attach-send').click();

	await expect(attach.getByTestId('attach-done')).toContainText('Recorded session');
	const { calls } = await apiState(page);
	const attachCall = lastCall(calls, 'attachToSession');
	// Test-authored wire boundary: the fixture recorded the exact body the
	// panel sent; the recorded payload IS this shape by construction.
	const body = attachCall.payload as {
		session_id: string | null;
		text: string;
		model_profile: string;
		client_request_id: string;
	};
	const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
	expect(body.session_id).toBeNull(); // the explicit create-new-session action
	expect(body.client_request_id).toMatch(UUID_RE); // idempotency key
	expect(body.model_profile).toBe('mai-primary');
	expect(body.text).toContain('runbooks:' + DOC_A1 + '#r3:0');
	expect(body.text).toContain('> Alpha reloads via systemctl');
	expect(body.text).toContain('Please apply on the pinned server.');

	// A refused submission stays honest (no fabricated success).
	await hostA.getByTestId('excerpt-attach').click();
	await hostA.getByTestId('attach-profile').fill('mai-primary');
	await apiCall(page, 'failNextSubmit', [
		{ status: 503, code: 'unavailable', message: 'chat surface unavailable' }
	]);
	await hostA.getByTestId('attach-send').click();
	await expect(hostA.getByTestId('attach-error')).toBeVisible();
	await expect(hostA.getByTestId('attach-error')).toHaveAttribute('data-error-code', 'unavailable');
});

test('hostile Markdown stays inert: source text renders, no node it names is ever constructed', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await bothWindowsMounted(page);

	// Open the hostile fixture document.
	await hostA
		.getByTestId('library-row')
		.filter({ hasText: DOC_A3 })
		.getByTestId('library-open')
		.click();
	const content = hostA.getByTestId('document-content');
	await expect(content).toContainText('Normal prose before the payloads');
	await expect(content).toContainText('<img src=x onerror=');

	// Nothing the payload names exists as a node inside the panel; the canary
	// never fires (the fixture would set window.__maiXssFired on execution).
	const root = hostA.getByTestId('docs-root');
	await expect(root.locator('script')).toHaveCount(0);
	await expect(root.locator('img')).toHaveCount(0);
	await expect(root.locator('iframe')).toHaveCount(0);
	await expect(root.locator('a[href^="javascript:"]')).toHaveCount(0);
	const fired = await page.evaluate(() => (window as { __maiXssFired?: boolean }).__maiXssFired);
	expect(fired).toBeUndefined();

	// The retrieval excerpt of the same hostile document renders inert too.
	await hostA.getByTestId('tab-search').click();
	await hostA.getByTestId('search-query').fill('hostile payloads');
	await hostA.getByTestId('search-submit').click();
	const excerptText = hostA.getByTestId('excerpt-text');
	await expect(excerptText).toContainText('<script>');
	expect(await hostA.getByTestId('docs-root').locator('script').count()).toBe(0);
	expect(await hostA.getByTestId('docs-root').locator('img').count()).toBe(0);
	expect(await hostA.getByTestId('docs-root').locator('iframe').count()).toBe(0);
});

test('a superseded response cannot paint; closing a window submits nothing further', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	const hostB = page.getByTestId('host-b');
	await bothWindowsMounted(page);

	// Same-window stale selector: park the read of DOC_A2, open DOC_A1
	// instead, then release the stale A2 view — it must never paint.
	await apiCall(page, 'armDeferredRead');
	await hostA
		.getByTestId('library-row')
		.filter({ hasText: DOC_A2 })
		.getByTestId('library-open')
		.click();
	await hostA
		.getByTestId('library-row')
		.filter({ hasText: DOC_A1 })
		.getByTestId('library-open')
		.click();
	await expect(hostA.getByTestId('document-citation')).toHaveText('runbooks:' + DOC_A1 + '#r3');

	const staleView = {
		doc_id: DOC_A2,
		server_id: SERVER_A,
		app_id: null,
		namespace: 'docs',
		revision: 1,
		sha256: 'b'.repeat(64),
		citation: 'docs:' + DOC_A2 + '#r1',
		source_kind: 'authored',
		origin: 'import:deploy.md',
		origin_revision: null,
		verification: 'unverified',
		content: 'STALE DOCUMENT BODY THAT MUST NEVER APPEAR'
	};
	await apiCall(page, 'releaseRead', [staleView]);
	await expect(hostA.getByTestId('document-citation')).toHaveText('runbooks:' + DOC_A1 + '#r3');
	await expect(hostA.getByTestId('document-content')).not.toContainText('STALE DOCUMENT BODY');

	// Cross-window stale content: park a slow Alpha search, let Beta answer,
	// then release an Alpha response that carries Beta's citation — scope and
	// recency both hold.
	await apiCall(page, 'armDeferredRetrieve');
	await hostA.getByTestId('tab-search').click();
	await hostA.getByTestId('search-query').fill('slow alpha query');
	await apiCall(page, 'armDeferredRetrieve');
	await hostA.getByTestId('search-submit').click();
	await hostB.getByTestId('tab-search').click();
	await hostB.getByTestId('search-query').fill('reload nginx');
	await hostB.getByTestId('search-submit').click();
	await expect(hostB.getByTestId('excerpt-citation')).toHaveText('runbooks:' + DOC_B1 + '#r7:0');

	// Close Alpha's window while its search is still parked, then release a
	// late Alpha response whose payload names Beta's citation: the disposed
	// window must drop it silently — it may paint in NEITHER window and no
	// late response may throw. (A LIVE window applying its own correctly
	// scoped response is correct behavior; excerpts' doc scope is the
	// storage-boundary contract of the producer — C07 rule 2 — not something
	// a client can or should re-derive.)
	await apiCall(page, 'toggleHostA');
	await expect(page.getByTestId('host-a').getByTestId('docs-root')).toHaveCount(0);
	await apiCall(page, 'releaseRetrieve', [
		{
			status: 'ok',
			code: null,
			scope: 'server',
			server_id: SERVER_A,
			excerpts: [
				{
					document_key: 'k-' + DOC_B1,
					doc_id: DOC_B1,
					namespace: 'runbooks',
					revision: 7,
					sha256: 'c'.repeat(64),
					ord: 0,
					citation: 'runbooks:' + DOC_B1 + '#r7:0',
					source_kind: 'authored',
					origin: 'import:nginx-reload.md',
					origin_revision: null,
					verification: 'verified',
					observed_at: '2026-10-16T08:00:00Z',
					score: 0.9,
					vector_rank: 1,
					fts_rank: 1,
					excerpt: 'LATE CROSS-WINDOW EXCERPT',
					truncated: false
				}
			],
			error_type: null
		}
	]);
	await expect(hostB.getByTestId('docs-root')).not.toContainText('LATE CROSS-WINDOW EXCERPT');
	await expect(hostB.getByTestId('excerpt-citation')).toHaveText('runbooks:' + DOC_B1 + '#r7:0');

	// Close-release race on the read path: a read parked at close time is
	// dropped by the disposed guard; no error escapes. (Reopen a fresh Alpha
	// window first — its per-instance state starts clean.)
	await apiCall(page, 'toggleHostA');
	await expect(page.getByTestId('host-a').getByTestId('docs-root')).toBeVisible();
	await expect(page.getByTestId('host-a').getByTestId('document-citation')).toHaveCount(0);
	await hostA.getByTestId('tab-library').click();
	await apiCall(page, 'armDeferredRead');
	await hostA
		.getByTestId('library-row')
		.filter({ hasText: DOC_A2 })
		.getByTestId('library-open')
		.click();
	await apiCall(page, 'toggleHostA');
	await expect(hostA.getByTestId('docs-root')).toHaveCount(0);
	await apiCall(page, 'releaseRead', [staleView]);
	await expect(page.getByTestId('host-b').getByTestId('docs-root')).toBeVisible();

	// Interacting with the sibling/focused panel never retargets Alpha's scope.
	await page.getByTestId('host-sibling').getByTestId('sibling-panel').click();
	const { calls } = await apiState(page);
	for (const call of calls) {
		if (call.op === 'listDocuments' || call.op === 'readIndex') {
			const scope = (call.args as unknown[])[0];
			expect(scope === SERVER_A || scope === SERVER_B).toBe(true);
		}
		if (call.op === 'retrieve') {
			const payload = call.payload as { server_id: string };
			expect([SERVER_A, SERVER_B]).toContain(payload.server_id);
		}
		if (call.op === 'submitMessage') {
			const payload = call.payload as { server_id: string };
			expect(payload.server_id).toBe(SERVER_A);
		}
	}
	// Zero uncaught page errors across every race and the unmount.
	expect(pageErrors).toEqual([]);
});

test('fail-closed bag and global scope; typed entry validates; attach without the chat seam refuses', async ({
	page
}) => {
	await bothWindowsMounted(page);

	// No services at all → safe config error, never a crash or a fetch.
	const bare = page.getByTestId('host-bare');
	await expect(bare.getByTestId('panel-config-error')).toBeVisible();
	await expect(bare.getByTestId('panel-config-error')).toContainText('without its documents service');

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

	// The registry validated the REAL feature entry.
	const { registry } = await apiState(page);
	expect(registry.kinds).toContain('documents');
	expect(registry.scopeChecks.serverScope).toBe('ok');
	expect(registry.scopeChecks.globalScope).toBe('scope_kind_not_allowed');
	expect(registry.scopeChecks.unknownKind).toBe('unknown_panel_kind');
	expect(registry.defaultGeometry).toEqual({ x: 64, y: 64, width: 820, height: 620 });
	expect(registry.minGeometry).toEqual({ x: 0, y: 0, width: 480, height: 360 });
	expect(registry.instanceKey).toBeNull(); // free-instance panel

	// Without the chat seam the attach path fails closed with a visible note.
	const nochat = page.getByTestId('host-nochat');
	await nochat.getByTestId('tab-search').click();
	await nochat.getByTestId('search-query').fill('reload nginx');
	await nochat.getByTestId('search-submit').click();
	await expect(nochat.getByTestId('search-result')).toHaveCount(1);
	await nochat.getByTestId('excerpt-attach').click();
	await expect(nochat.getByTestId('attach-unavailable')).toContainText('not wired');
	await expect(nochat.getByTestId('attach-send')).toHaveCount(0);
});
