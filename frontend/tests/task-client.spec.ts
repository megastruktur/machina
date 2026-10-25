import { expect, test } from '@playwright/test';
import {
	ApiError,
	createMaiApiClient,
	MaiClientError,
	type MaiApiClient,
	type TaskCollectionQuery,
	type TaskCorrectionBody,
	type TaskDecisionBody,
	type TaskMutationOutcome,
	type TaskStepOperation,
	type TaskStepsPage,
	type TaskView
} from '../src/lib/api/client';
import { taskServices, type TaskApi } from '../src/lib/workspace/entries/07-web-tasks';

/** Node globals (ambient: the project tsconfig targets browser-first code). */
declare const process: { env: Record<string, string | undefined> };

/**
 * 16-task-client — typed task client/service boundary (§8; producer
 * `15-task-api`). One focused file, 4–6 rows, every row through the REAL
 * `createMaiApiClient()` transport and the real `07-web-tasks.ts` service
 * bag (assembled exactly like the desktop composition):
 *
 * - In-process transport + storage honesty (no browser): the real client
 *   module runs every task route against a stubbed fetch while a
 *   recording shim guards `localStorage`/`sessionStorage` — ZERO storage
 *   access, ever (P5: the server is the only source of task truth); the
 *   two mutations carry the double-submit CSRF header, reads carry none.
 * - Auth boundary (live fixture): an unauthenticated read is a value-free
 *   401; a mutation without a CSRF token fails closed client-side before
 *   any byte.
 * - C6: ONE accepted writer — the user correction is 201 created with the
 *   fresh revision (author=user, source-linked), a stale tab's
 *   `base_revision` is the structured 409 `revision_conflict` (never
 *   last-writer-wins; the accepted writer's state stands), and a
 *   retransmitted `idempotency_key` replays onto the SAME revision (200
 *   deduplicated; same key + changed content is the 422 conflict).
 * - C8: every cross-server read AND write fails closed with the
 *   scope-blind 404 `task_not_found` — absent conversation, foreign scope
 *   and absent episode are indistinguishable and leak no identities.
 * - P3: the paged contract — deterministic bounded pages with same-filter
 *   totals and a deduplicated walk, stable item IDs across deltas,
 *   superseded rows recorded and served only when explicitly requested,
 *   completed work + evidence preserved.
 * - P5: two independent sessions share the ONE server revision (reconnect
 *   re-derives truth from the shared append-only ledger); decisions apply
 *   through CAS (stale = 409) and deduplicate on retransmission; cancel is
 *   the terminal, pointer-releasing control.
 *
 * Live rows are skipped unless `MAI_E2E_API_URL` names a live fixture; the
 * coordinates arrive via `MAI_E2E_*` variables set by the evidence launcher
 * reading the fixture state file (the 00-runtime contract):
 * `MAI_E2E_PASSWORD`, `MAI_E2E_SERVER_ID`, `MAI_E2E_TASK_ID`,
 * `MAI_E2E_CONVERSATION_ID`, `MAI_E2E_MESSAGE_ID` (a task seeded through
 * the real task_store producers: steps s1, s2 executed with one message
 * evidence ref, s3 depending on s1; user constraint c1 anchored to the
 * seeded user message).
 */

const BASE_URL = process.env.MAI_E2E_API_URL ?? '';
const PASSWORD = process.env.MAI_E2E_PASSWORD ?? '';
const SERVER_A = process.env.MAI_E2E_SERVER_ID ?? '';
const TASK_ID = process.env.MAI_E2E_TASK_ID ?? '';
const CONVERSATION_ID = process.env.MAI_E2E_CONVERSATION_ID ?? '';
const MESSAGE_ID = process.env.MAI_E2E_MESSAGE_ID ?? '';
const SERVER_B = '11111111-2222-3333-4444-555555555555'; // never seeded anywhere

// ---------------------------------------------------------------------------
// Row 1 — in-process transport + storage honesty (no browser, no network).
// ---------------------------------------------------------------------------

/** One recorded exchange of the stubbed fetch. */
interface StubCall {
	method: string;
	path: string;
	csrf: string | null;
}

test.describe('task client transport + storage honesty (in-process, stubbed fetch)', () => {
	test('every task route is typed end-to-end with ZERO storage access; CSRF on mutations only', async () => {
		const calls: StubCall[] = [];
		const responseFor = (method: string, path: string): { status: number; body: unknown } => {
			const taskId = '6f0a9e56-0000-4000-8000-000000000001';
			if (path === '/api/v1/chat/conversations/conv/task') return { status: 200, body: MOCK_TASK };
			if (path === `/api/v1/chat/tasks/${taskId}/items`) {
				return { status: 200, body: taskPage(taskId, [MOCK_STEP]) };
			}
			if (path === `/api/v1/chat/tasks/${taskId}/constraints`) {
				return { status: 200, body: taskPage(taskId, []) };
			}
			if (path === `/api/v1/chat/tasks/${taskId}/evidence`) {
				return { status: 200, body: taskPage(taskId, []) };
			}
			if (path === `/api/v1/chat/tasks/${taskId}/revisions`) {
				return method === 'POST'
					? { status: 201, body: MOCK_OUTCOME }
					: { status: 200, body: taskPage(taskId, []) };
			}
			if (path === `/api/v1/chat/tasks/${taskId}/decisions`) {
				return { status: 201, body: MOCK_OUTCOME };
			}
			throw new Error(`unexpected task-client call ${method} ${path}`);
		};
		const stubFetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
			// The client passes origin-relative paths when baseUrl is empty;
			// resolve them so Node's Request constructor accepts the URL.
			const resolved =
				input instanceof Request ? input.url : new URL(input, 'http://mai.stub').toString();
			const request = new Request(resolved, init);
			const path = new URL(request.url).pathname;
			calls.push({
				method: request.method,
				path,
				csrf: request.headers.get('x-csrf-token')
			});
			const { status, body } = responseFor(request.method, path);
			return new Response(JSON.stringify(body), {
				status,
				headers: { 'content-type': 'application/json' }
			});
		}) as typeof fetch;

		// The recording storage guard: any get/set by the client module is
		// captured; NOTHING may touch browser storage (P5 server truth).
		const storageAccess: string[] = [];
		const guarded = (kind: 'localStorage' | 'sessionStorage'): PropertyDescriptor => ({
			configurable: true,
			get() {
				storageAccess.push(`get ${kind}`);
				return undefined;
			},
			set() {
				storageAccess.push(`set ${kind}`);
			}
		});
		const previousLocal = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
		const previousSession = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
		Object.defineProperty(globalThis, 'localStorage', guarded('localStorage'));
		Object.defineProperty(globalThis, 'sessionStorage', guarded('sessionStorage'));
		// Node ships no webstorage globals (without the experimental flag), so
		// the restore path deletes through this known-shape alias.
		const globalStorage = globalThis as { localStorage?: unknown; sessionStorage?: unknown };

		const taskId = '6f0a9e56-0000-4000-8000-000000000001';
		const serverId = '6f0a9e56-0000-4000-8000-000000000003';
		let header: TaskView | undefined;
		let steps: TaskStepsPage | undefined;
		try {
			// The panel bag, assembled exactly like the desktop composition.
			// The CSRF supplier stands in for the browser cookie jar the
			// client normally reads (document.cookie does not exist in Node).
			const client = createMaiApiClient({
				fetch: stubFetch,
				getCsrfToken: () => 'token-from-cookie-jar'
			});
			const tasks: TaskApi = taskServices({
				getConversationTask: client.getConversationTask,
				listTaskSteps: client.listTaskSteps,
				listTaskConstraints: client.listTaskConstraints,
				listTaskEvidence: client.listTaskEvidence,
				listTaskRevisions: client.listTaskRevisions,
				submitTaskCorrection: client.submitTaskCorrection,
				submitTaskDecision: client.submitTaskDecision
			}).api;

			header = await tasks.getConversationTask('conv', serverId);
			steps = await tasks.listTaskSteps(taskId, serverId, { limit: 10, offset: 0 });
			await tasks.listTaskConstraints(taskId, serverId, { includeSuperseded: true });
			await tasks.listTaskEvidence(taskId, serverId, { limit: 5 });
			await tasks.listTaskRevisions(taskId, serverId);
			const correction: TaskMutationOutcome = await tasks.submitTaskCorrection(taskId, {
				server_id: serverId,
				patch: {
					schema_version: 1,
					source_message_id: '6f0a9e56-0000-4000-8000-000000000004',
					base_revision: 1,
					idempotency_key: '6f0a9e56-0000-4000-8000-000000000005',
					title: 'corrected'
				}
			});
			const decision = await tasks.submitTaskDecision(taskId, {
				server_id: serverId,
				expected_revision: 1,
				client_request_id: '6f0a9e56-0000-4000-8000-000000000006',
				requested_phase: 'inspect'
			});

			// Typed §8 envelopes end-to-end (the bag IS the client surface).
			expect(header.title).toBe('mocked task');
			expect(header.counts.steps_total).toBe(1);
			expect(steps!.items!.map((row) => row.step_id)).toEqual(['s1']);
			expect(steps!.total).toBe(1);
			expect(correction.disposition).toBe('created');
			expect(correction.task.revision).toBe(1);
			expect(decision.task.title).toBe('mocked task');
		} finally {
			// Restore the pre-test environment exactly.
			delete globalStorage.localStorage;
			delete globalStorage.sessionStorage;
			if (previousLocal) Object.defineProperty(globalThis, 'localStorage', previousLocal);
			if (previousSession) Object.defineProperty(globalThis, 'sessionStorage', previousSession);
		}

		// Storage honesty: the client persisted NOTHING anywhere (P5).
		expect(storageAccess).toEqual([]);

		// Transport honesty: 5 reads + 2 mutations, exact paths, CSRF on the
		// mutations only, GETs carry none (the session boundary is cookies).
		expect(calls.map((one) => one.method)).toEqual([
			'GET',
			'GET',
			'GET',
			'GET',
			'GET',
			'POST',
			'POST'
		]);
		const posts = calls.filter((one) => one.method === 'POST');
		expect(posts.map((one) => one.path).sort()).toEqual([
			`/api/v1/chat/tasks/${taskId}/decisions`,
			`/api/v1/chat/tasks/${taskId}/revisions`
		]);
		for (const one of posts) expect(one.csrf).toBe('token-from-cookie-jar');
		for (const one of calls.filter((row) => row.method === 'GET')) {
			expect(one.csrf, `csrf on ${one.path}`).toBeNull();
		}
	});
});

// ---------------------------------------------------------------------------
// Rows 2–6 — live fixture legs (env-gated; the 00-runtime contract).
// ---------------------------------------------------------------------------

/** Cookie-jar fetch shim: the browser jar this client normally relies on. */
interface CookieJar {
	fetch: typeof fetch;
	getCsrfToken: () => string | null;
}
function jar(): CookieJar {
	const cookies = new Map<string, string>();
	const impl = (async (input: RequestInfo | URL, init?: RequestInit) => {
		const headers = new Headers(init?.headers);
		const cookie = [...cookies.entries()].map(([k, v]) => `${k}=${v}`).join('; ');
		if (cookie) headers.set('cookie', cookie);
		const response = await fetch(new Request(input, { ...init, headers }));
		for (const setCookie of response.headers.getSetCookie()) {
			const [pair] = setCookie.split(';');
			const eq = pair.indexOf('=');
			cookies.set(pair.slice(0, eq), pair.slice(eq + 1));
		}
		return response;
	}) as typeof fetch;
	return { fetch: impl, getCsrfToken: () => cookies.get('mai_csrf') ?? null };
}

/**
 * The panel-facing services bag, assembled like the desktop composition and
 * grown by the task slice: the real 07-web-tasks boundary (`taskServices()`),
 * fully satisfied by ONE client instance.
 */
function liveTaskServices(sessionJar: CookieJar): { client: MaiApiClient; tasks: TaskApi } {
	const client = createMaiApiClient({
		baseUrl: BASE_URL,
		fetch: sessionJar.fetch,
		getCsrfToken: sessionJar.getCsrfToken,
		// The fixture allowlists exactly its own origin (MAI_PUBLIC_ORIGINS).
		headers: { origin: BASE_URL }
	});
	const tasks: TaskApi = taskServices({
		getConversationTask: client.getConversationTask,
		listTaskSteps: client.listTaskSteps,
		listTaskConstraints: client.listTaskConstraints,
		listTaskEvidence: client.listTaskEvidence,
		listTaskRevisions: client.listTaskRevisions,
		submitTaskCorrection: client.submitTaskCorrection,
		submitTaskDecision: client.submitTaskDecision
	}).api;
	return { client, tasks };
}

/** One independent fresh session: its own login, cookies and CSRF token. */
async function freshSession(): Promise<{ client: MaiApiClient; tasks: TaskApi }> {
	const session = liveTaskServices(jar());
	await session.client.login({ password: PASSWORD });
	return session;
}

/** One user correction body against the seeded task with a fresh key. */
function correction(
	baseRevision: number,
	overrides?: Partial<TaskCorrectionBody['patch']>
): TaskCorrectionBody {
	return {
		server_id: SERVER_A,
		patch: {
			schema_version: 1,
			source_message_id: MESSAGE_ID,
			base_revision: baseRevision,
			idempotency_key: crypto.randomUUID(),
			...overrides
		}
	};
}

/** One user decision body with a fresh key. */
function decision(
	expectedRevision: number,
	overrides?: Partial<TaskDecisionBody>
): TaskDecisionBody {
	return {
		server_id: SERVER_A,
		expected_revision: expectedRevision,
		client_request_id: crypto.randomUUID(),
		...overrides
	};
}

/** The current shared header through one session (the CAS base). */
async function currentRevision(tasks: TaskApi): Promise<TaskView> {
	return tasks.getConversationTask(CONVERSATION_ID, SERVER_A);
}

/** Linear wait helper (bounded polling). */
function delay(ms: number): Promise<void> {
	const { promise, resolve } = Promise.withResolvers<void>();
	setTimeout(resolve, ms);
	return promise;
}

/** Bounded retry until the header reports at least `revision`. */
async function waitRevision(tasks: TaskApi, revision: number, timeoutMs = 15_000): Promise<TaskView> {
	const deadline = Date.now() + timeoutMs;
	let header = await currentRevision(tasks);
	while (header.revision < revision && Date.now() < deadline) {
		await delay(200);
		header = await currentRevision(tasks);
	}
	return header;
}

/** A full add-operation step literal (the generated type requires every field). */
function step(stepId: string, title: string): TaskStepOperation {
	return {
		op: 'add',
		step: {
			step_id: stepId,
			title,
			status: 'pending',
			verification: 'not_checked',
			risk: 'read_only',
			prerequisite_unresolved: false
		}
	};
}

test.describe('task client (live fixture backend)', () => {
	test.describe.configure({ mode: 'serial' });
	test.beforeAll(async () => {
		test.skip(!BASE_URL, 'real-boundary run only: set MAI_E2E_API_URL to a live fixture');
		if (!PASSWORD || !SERVER_A || !TASK_ID || !CONVERSATION_ID || !MESSAGE_ID) {
			throw new Error(
				'fixture coordinates missing: set MAI_E2E_PASSWORD, MAI_E2E_SERVER_ID, ' +
					'MAI_E2E_TASK_ID, MAI_E2E_CONVERSATION_ID and MAI_E2E_MESSAGE_ID from the ' +
					'evidence launcher (real task_store seed)'
			);
		}
	});

	test('auth boundary: unauthenticated read is a value-free 401; CSRF-less mutation fails closed', async () => {
		const anonymous = liveTaskServices(jar());
		await expect(
			anonymous.tasks.getConversationTask(CONVERSATION_ID, SERVER_A)
		).rejects.toMatchObject({ status: 401, code: 'unauthenticated' });
		const noCsrf = createMaiApiClient({
			baseUrl: BASE_URL,
			fetch: jar().fetch,
			getCsrfToken: () => null,
			headers: { origin: BASE_URL }
		});
		await expect(noCsrf.submitTaskCorrection(TASK_ID, correction(1))).rejects.toThrow(
			MaiClientError
		);
	});

	test('C6: accepted correction (201), stale tab conflict (409), idempotent replay (200 dedup)', async () => {
		const writer = await freshSession();
		const staleTab = await freshSession();

		// The compact §8 header: the seeded episode, counts only.
		const header = await currentRevision(writer.tasks);
		expect(header.task_id).toBe(TASK_ID);
		expect(header.conversation_id).toBe(CONVERSATION_ID);
		expect(header.server_id).toBe(SERVER_A);
		expect(header.status).toBe('in_progress');
		expect(header.counts.steps_total).toBe(3);
		expect(header.counts.constraints_total).toBe(1);
		expect(header.counts.evidence_total).toBe(1);

		// Tab B (the fresh writer) submits the user correction: 201 created,
		// author=user, source-linked to the seeded user message.
		const body = correction(header.revision, {
			title: 'user-corrected objective',
			operations: [step('s9', 'step s9')]
		});
		const accepted: TaskMutationOutcome = await writer.tasks.submitTaskCorrection(TASK_ID, body);
		expect(accepted.disposition).toBe('created');
		expect(accepted.task.revision).toBe(header.revision + 1);
		expect(accepted.revision.author).toBe('user');
		expect(accepted.revision.kind).toBe('plan_patch');
		expect(accepted.revision.source_message_id).toBe(MESSAGE_ID);

		// Tab A still holds the stale base revision: its own delta is the
		// structured 409 — never last-writer-wins; the writer's state stands.
		const stale = staleTab.tasks.submitTaskCorrection(TASK_ID, {
			server_id: SERVER_A,
			patch: {
				schema_version: 1,
				source_message_id: MESSAGE_ID,
				base_revision: header.revision,
				idempotency_key: crypto.randomUUID(),
				operations: [step('s7', 'step s7')]
			}
		});
		const caught = (await stale.then(() => null, (error: unknown) => error)) as ApiError;
		expect(caught).toBeInstanceOf(ApiError);
		expect(caught.status).toBe(409);
		expect(caught.code).toBe('revision_conflict');
		expect(caught.fields).toEqual({ base_revision: 'revision_conflict' });
		const afterConflict = await writer.tasks.listTaskSteps(TASK_ID, SERVER_A);
		const ids = afterConflict.items!.map((row) => row.step_id);
		expect(ids).toContain('s9');
		expect(ids).not.toContain('s7');

		// Retransmission of the SAME body replays onto the SAME revision
		// (200 deduplicated); the ledger records ONE correction, not two.
		const replay: TaskMutationOutcome = await writer.tasks.submitTaskCorrection(TASK_ID, body);
		expect(replay.disposition).toBe('deduplicated');
		expect(replay.task.revision).toBe(accepted.task.revision);
		expect(replay.revision.revision).toBe(accepted.revision.revision);
		const ledger = await writer.tasks.listTaskRevisions(TASK_ID, SERVER_A);
		expect(ledger.total).toBe(2); // create + ONE correction

		// Same key, changed content: the stable contract conflict (422).
		await expect(
			writer.tasks.submitTaskCorrection(TASK_ID, {
				...body,
				patch: { ...body.patch, title: 'something else' }
			})
		).rejects.toMatchObject({ status: 422, code: 'invalid_task_patch' });
	});

	test('C8: foreign and absent scopes fail closed, scope-blind, zero identity leakage', async () => {
		const a = await freshSession();

		// Every cross-scope read fails with the SAME scope-blind 404.
		const reads: Array<() => Promise<unknown>> = [
			() => a.tasks.getConversationTask(CONVERSATION_ID, SERVER_B),
			() => a.tasks.listTaskSteps(TASK_ID, SERVER_B),
			() => a.tasks.listTaskConstraints(TASK_ID, SERVER_B),
			() => a.tasks.listTaskEvidence(TASK_ID, SERVER_B),
			() => a.tasks.listTaskRevisions(TASK_ID, SERVER_B),
			// A conversation without an episode and a foreign conversation id
			// collapse into the SAME response (existence never leaks).
			() => a.tasks.getConversationTask(crypto.randomUUID(), SERVER_A),
			() => a.tasks.getConversationTask(CONVERSATION_ID, crypto.randomUUID())
		];
		for (const read of reads) {
			const error = (await read().then(() => null, (e: unknown) => e)) as ApiError | null;
			expect(error, 'cross-scope read must fail').toBeInstanceOf(ApiError);
			expect(error!.status).toBe(404);
			expect(error!.code).toBe('task_not_found');
			// No foreign identity or task content in the rendered envelope.
			const rendered = `${JSON.stringify(error)} ${error!.message}`;
			for (const fragment of [TASK_ID, CONVERSATION_ID, SERVER_A, MESSAGE_ID]) {
				expect(rendered).not.toContain(fragment);
			}
		}

		// Cross-scope WRITES fail closed identically.
		await expect(
			a.tasks.submitTaskCorrection(TASK_ID, {
				...correction(1, { title: 'foreign scope rename' }),
				server_id: SERVER_B
			})
		).rejects.toMatchObject({ status: 404, code: 'task_not_found' });
		await expect(
			a.tasks.submitTaskDecision(
				TASK_ID,
				decision(1, { server_id: SERVER_B, requested_phase: 'act' })
			)
		).rejects.toMatchObject({ status: 404, code: 'task_not_found' });
	});

	test('P3: bounded pages with dedup walk; superseded history only on explicit request', async () => {
		const a = await freshSession();

		// Deterministic items page: same-filter total + has_more, dedup walk.
		const first: TaskStepsPage = await a.tasks.listTaskSteps(TASK_ID, SERVER_A, { limit: 2 });
		expect(first.task_id).toBe(TASK_ID);
		expect(first.limit).toBe(2);
		expect(first.offset).toBe(0);
		expect(first.total).toBe(4); // s1, s2, s3, s9
		expect(first.has_more).toBe(true);
		const second = await a.tasks.listTaskSteps(TASK_ID, SERVER_A, { limit: 2, offset: 2 });
		expect(second.has_more).toBe(false);
		const walked = [...first.items!, ...second.items!];
		expect(walked.map((row) => row.step_id)).toHaveLength(
			new Set(walked.map((row) => row.step_id)).size
		);
		const positions = walked.map((row) => row.position);
		expect([...positions].sort((x, y) => x - y)).toEqual(positions);

		// User correction supersedes s3 -> s3b: the recorded P3 history.
		const header = await currentRevision(a.tasks);
		const supersede = await a.tasks.submitTaskCorrection(TASK_ID, {
			server_id: SERVER_A,
			patch: {
				schema_version: 1,
				source_message_id: MESSAGE_ID,
				base_revision: header.revision,
				idempotency_key: crypto.randomUUID(),
				operations: [
					{
						op: 'supersede',
						step: {
							step_id: 's3b',
							title: 'step s3b',
							status: 'pending',
							verification: 'not_checked',
							risk: 'read_only',
							prerequisite_unresolved: false
						},
						supersedes_step_id: 's3'
					}
				]
			}
		});
		expect(supersede.disposition).toBe('created');
		await waitRevision(a.tasks, supersede.task.revision);

		// Default page: the active compacted set only (s3 gone, s3b in).
		const active = await a.tasks.listTaskSteps(TASK_ID, SERVER_A);
		const activeIds = active.items!.map((row) => row.step_id);
		expect(activeIds).toContain('s3b');
		expect(activeIds).not.toContain('s3');
		expect(active.total).toBe(4);

		// History is RECORDED, never deleted — served only when asked by name.
		const history = await a.tasks.listTaskSteps(TASK_ID, SERVER_A, { includeSuperseded: true });
		const s3 = history.items!.find((row) => row.step_id === 's3');
		expect(s3).toBeTruthy();
		expect(s3!.superseded_by).toBe('s3b');
		expect(s3!.superseded_at).toBeTruthy();
		expect(history.total).toBe(5);

		// Completed work survives the delta: s2 stays executed WITH its
		// evidence reference (stable IDs, P3).
		const s2 = active.items!.find((row) => row.step_id === 's2');
		expect(s2!.status).toBe('executed');
		expect(s2!.evidence_refs).toHaveLength(1);

		// Constraints page: origin + source anchors, bounded envelope.
		const constraints = await a.tasks.listTaskConstraints(TASK_ID, SERVER_A, { limit: 1 });
		expect(constraints.total).toBe(1);
		expect(constraints.items![0].constraint_id).toBe('c1');
		expect(constraints.items![0].origin).toBe('user');
		expect(constraints.items![0].source_message_refs).toContain(MESSAGE_ID);

		// Evidence page: provenance identities only (kind + record id + note).
		const evidence = await a.tasks.listTaskEvidence(TASK_ID, SERVER_A);
		expect(evidence.total).toBe(1);
		expect(evidence.items![0].kind).toBe('message');
		expect(evidence.items![0].origin_step_id).toBe('s2');
		expect(JSON.stringify(evidence)).not.toContain('correct the objective');
	});

	test('P5: two sessions share ONE server revision; decisions CAS + replay; cancel is terminal', async () => {
		const a = await freshSession();
		const b = await freshSession();

		// Reconnect truth: both independent sessions derive the SAME
		// revision from the server (never from browser state).
		const headerA = await currentRevision(a.tasks);
		const headerB = await currentRevision(b.tasks);
		expect(headerA.revision).toBe(headerB.revision);

		// Phase decision through CAS: 201 created, metadata-only (the task
		// stays in_progress — a phase is advisory, never permission).
		const phaseDecision = decision(headerA.revision, { requested_phase: 'inspect' });
		const decided = await a.tasks.submitTaskDecision(TASK_ID, phaseDecision);
		expect(decided.disposition).toBe('created');
		expect(decided.task.requested_phase).toBe('inspect');
		expect(decided.task.status).toBe('in_progress');
		expect(decided.revision.kind).toBe('metadata');
		expect(decided.revision.author).toBe('user');
		const revisionAfterDecision = decided.task.revision;

		// Retransmitted decision key deduplicates onto the SAME revision.
		const replayDecision = await a.tasks.submitTaskDecision(TASK_ID, phaseDecision);
		expect(replayDecision.disposition).toBe('deduplicated');
		expect(replayDecision.task.revision).toBe(revisionAfterDecision);

		// Session B sees the applied phase; session A's next CAS must use
		// the NEW revision — the stale named revision is the 409.
		const headerBAfter = await waitRevision(b.tasks, revisionAfterDecision);
		expect(headerBAfter.requested_phase).toBe('inspect');
		await expect(
			b.tasks.submitTaskDecision(TASK_ID, decision(headerA.revision, { requested_phase: 'act' }))
		).rejects.toMatchObject({ status: 409, code: 'revision_conflict' });

		// The shared append-only ledger, newest first: create + the user
		// corrections + the metadata decision — ONE history for UI and tools.
		const ledger = await b.tasks.listTaskRevisions(TASK_ID, SERVER_A);
		expect(ledger.total).toBe(4);
		const kinds = ledger.items!.map((row) => [row.revision, row.kind, row.author]);
		expect(kinds).toEqual([
			[revisionAfterDecision, 'metadata', 'user'],
			[revisionAfterDecision - 1, 'plan_patch', 'user'],
			[revisionAfterDecision - 2, 'plan_patch', 'user'],
			[1, 'create', 'system']
		]);
		const revisions = ledger.items!.map((row) => row.revision);
		expect([...revisions].sort((x, y) => y - x)).toEqual(revisions);

		// No duplicated steps across sessions (P5 shared truth, not copies).
		const stepsB = await b.tasks.listTaskSteps(TASK_ID, SERVER_A, { includeSuperseded: true });
		const stepIds = stepsB.items!.map((row) => row.step_id);
		expect(stepIds).toHaveLength(new Set(stepIds).size);

		// Cancel: the explicit terminal control closes the episode and
		// releases the conversation's active-task pointer — the header
		// becomes the SAME scope-blind 404 (the LAST live action).
		const cancelled = await a.tasks.submitTaskDecision(
			TASK_ID,
			decision(revisionAfterDecision, { task_action: 'cancel' })
		);
		expect(cancelled.disposition).toBe('created');
		expect(cancelled.task.status).toBe('cancelled');
		await expect(currentRevision(a.tasks)).rejects.toMatchObject({
			status: 404,
			code: 'task_not_found'
		});
	});
});

// ---------------------------------------------------------------------------
// Minimal §8 bodies for the stubbed-fetch row (shapes, not content).
// ---------------------------------------------------------------------------

const MOCK_TASK: TaskView = {
	task_id: '6f0a9e56-0000-4000-8000-000000000001',
	conversation_id: '6f0a9e56-0000-4000-8000-000000000002',
	server_id: '6f0a9e56-0000-4000-8000-000000000003',
	revision: 1,
	title: 'mocked task',
	summary: null,
	requested_phase: 'plan',
	status: 'in_progress',
	active_step_id: null,
	counts: {
		steps_total: 1,
		steps_executed: 0,
		constraints_total: 0,
		evidence_total: 0,
		criteria_total: 0,
		criteria_verified: 0
	},
	created_at: '2026-01-01T00:00:00Z',
	updated_at: '2026-01-01T00:00:00Z'
};
const MOCK_STEP = {
	task_id: MOCK_TASK.task_id,
	step_id: 's1',
	title: 'step s1',
	status: 'pending',
	verification: 'not_checked',
	position: 0,
	risk: 'read_only',
	prerequisite_unresolved: false,
	expected_check: null,
	skip_reason: null,
	result_summary: null,
	evidence_refs: [],
	depends_on: [],
	superseded_at: null,
	superseded_by: null,
	created_at: '2026-01-01T00:00:00Z',
	updated_at: '2026-01-01T00:00:00Z'
};

function taskPage(taskId: string, items: unknown[]) {
	return {
		task_id: taskId,
		limit: 10,
		offset: 0,
		total: items.length,
		has_more: false,
		items
	};
}

const MOCK_OUTCOME: TaskMutationOutcome = {
	task: MOCK_TASK,
	revision: {
		task_id: MOCK_TASK.task_id,
		revision: 2,
		kind: 'plan_patch',
		author: 'user',
		source_message_id: null,
		item_id: null,
		payload: null,
		applied_at: '2026-01-01T00:00:00Z'
	},
	disposition: 'created'
};
