import { expect, test } from '@playwright/test';
import {
	ApiError,
	createMaiApiClient,
	type CanonicalConversationTurn,
	type CanonicalTranscriptPage,
	MaiClientError,
	type MaiApiClient
} from '../src/lib/api/client';
import {
	browserChatEventStream,
	chatServices,
	type ChatApi
} from '../src/lib/workspace/entries/06-web-chat';

/** Node globals (ambient: the project tsconfig targets browser-first code). */
declare const process: { env: Record<string, string | undefined> };

/**
 * Lazy `node:fs`: the project ships no @types/node, so a static `node:fs`
 * import cannot typecheck here; the specifier stays dynamic on purpose (the
 * spec runs under Node, never in the browser bundle).
 */
interface NodeFs {
	writeFileSync(path: string, data: string): void;
	readFileSync(path: string, encoding: string): string;
	rmSync(path: string, options?: { force?: boolean }): void;
}
let fsModule: NodeFs | null = null;
async function fs(): Promise<NodeFs> {
	fsModule ??= (await import('node:fs' as string)) as NodeFs;
	return fsModule;
}

/** Linear wait helper (bounded polling). */
function delay(ms: number): Promise<void> {
	const { promise, resolve } = Promise.withResolvers<void>();
	setTimeout(resolve, ms);
	return promise;
}

/**
 * 09-client-continuity — canonical conversation client/service boundary over
 * the REAL fixture backend (`scripts/agent_runtime_fixture.py`, the
 * 00-runtime harness). One env-gated file proving:
 *
 * - Transport honesty: an unauthenticated read is a value-free 401; a
 *   mutation without a CSRF token fails closed client-side before any byte.
 * - C2: a FRESH session (a second independent cookie jar, zero shared
 *   state) restores the true server transcript through the canonical
 *   bounded reads — role-preserving message pages with honest
 *   limit/offset/total/has_more envelopes, and the cursor-exclusive events
 *   page whose `afterSeq` walk deduplicates by construction and carries
 *   identities only (never transcript text).
 * - C6: retransmission deduplicates onto the original identity (same key +
 *   same content), while changed content under the same key and a stale
 *   `expected_revision` both fail closed with structured 409s — never
 *   last-writer-wins.
 * - C6/C5/C8: while a gated run holds the writer lease, a follow-up is
 *   honestly reported `queued_followup` (the wire's 202), an exclusive new
 *   task is rejected `conversation_busy`, deterministic steering is recorded
 *   durably (201 created; the retransmitted key deduplicates onto the SAME
 *   row), and every cross-server reference fails closed with the scope-blind
 *   `conversation_not_found` 404 — no foreign identity ever leaks.
 *
 * Everything runs through the real `createMaiApiClient()` transport and the
 * real `06-web-chat.ts` service bag (the panel boundary assembled exactly
 * like the desktop composition). Skipped unless `MAI_E2E_API_URL` names a
 * live fixture; the fixture coordinates (password, server id, gate dir,
 * journal) arrive via `MAI_E2E_*` variables set by the evidence launcher
 * reading the fixture state file (the 00-runtime contract).
 */

const BASE_URL = process.env.MAI_E2E_API_URL ?? '';
const PASSWORD = process.env.MAI_E2E_PASSWORD ?? '';
const SERVER_A = process.env.MAI_E2E_SERVER_ID ?? '';
const GATE_DIR = process.env.MAI_E2E_FIXTURE_GATES ?? '';
const JOURNAL = process.env.MAI_E2E_FIXTURE_JOURNAL ?? '';
const GATE_MARKER = 'm1'; // the fixture model script's fixed tool-call marker

/** Synthetic turn bodies; no real host, credential or user content. */
const C2_MESSAGE = 'List the fixture host disk layout; do not change files';

/** Frozen terminal run states (the legacy view's replayed state). */
const TERMINAL_STATES: Record<string, true> = {
	succeeded: true,
	failed: true,
	cancelled: true
};

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

/** The panel bag with the canonical slice guaranteed present. */
type LiveChatApi = Required<ChatApi>;

/**
 * The panel-facing services bag, assembled like the desktop composition and
 * grown by the canonical conversation slice: the real 06-web-chat boundary
 * (`chatServices()`), fully satisfied by ONE client instance — `Required<>`
 * pins the composition contract the 09 wiring claims.
 */
function liveChatServices(sessionJar: CookieJar): { client: MaiApiClient; chat: LiveChatApi } {
	const client = createMaiApiClient({
		baseUrl: BASE_URL,
		fetch: sessionJar.fetch,
		getCsrfToken: sessionJar.getCsrfToken,
		// The fixture allowlists exactly its own origin (MAI_PUBLIC_ORIGINS).
		headers: { origin: BASE_URL }
	});
	const chat: Required<ChatApi> = {
		submitMessage: client.submitMessage,
		listConversations: client.listConversations,
		readConversation: client.readConversation,
		renameConversation: client.renameConversation,
		cancelConversation: client.cancelConversation,
		streamConversationEvents: browserChatEventStream,
		readRun: client.readRun,
		readApproval: client.readApproval,
		decideApproval: client.decideApproval,
		/** Live-transport marker read by ChatPanel.profilesLive (not the wire). */
		listProfiles: async () => [],
		listCanonicalConversations: client.listCanonicalConversations,
		getConversation: client.getConversation,
		listConversationMessages: client.listConversationMessages,
		listConversationEvents: client.listConversationEvents,
		createConversation: client.createConversation,
		submitConversationMessage: client.submitConversationMessage,
		steerConversation: client.steerConversation
	};
	return { client, chat: chatServices(chat).api as Required<ChatApi> };
}

/** One independent fresh session: its own login, cookies and CSRF token. */
async function freshSession(): Promise<{ client: MaiApiClient; chat: LiveChatApi }> {
	const session = liveChatServices(jar());
	await session.client.login({ password: PASSWORD });
	return session;
}

/** Bounded poll for a terminal run state through the panel bag. */
async function waitTerminal(
	chat: LiveChatApi,
	runId: string,
	timeoutMs = 120_000
): Promise<string> {
	const deadline = Date.now() + timeoutMs;
	let last: string | null = null;
	while (Date.now() < deadline) {
		const view = await chat.readConversation(runId);
		last = view.state;
		if (last !== null && TERMINAL_STATES[last]) return last;
		await delay(250);
	}
	throw new Error(`run ${runId} never turned terminal (last=${last})`);
}

/** Bounded poll for a transcript page carrying at least `total` rows. */
async function waitMessageTotal(
	chat: LiveChatApi,
	conversationId: string,
	serverId: string,
	total: number,
	timeoutMs = 120_000
): Promise<CanonicalTranscriptPage> {
	const deadline = Date.now() + timeoutMs;
	let page: CanonicalTranscriptPage | null = null;
	while (Date.now() < deadline) {
		page = await chat.listConversationMessages(conversationId, serverId, { limit: 50 });
		if (page.total >= total) return page;
		await delay(250);
	}
	throw new Error(`transcript never reached ${total} rows (last=${page?.total})`);
}

/** One canonical user turn body with a fresh idempotency key. */
function turn(
	message: string,
	extra?: Partial<CanonicalConversationTurn>
): CanonicalConversationTurn {
	return {
		server_id: SERVER_A,
		client_request_id: crypto.randomUUID(),
		message,
		profile: 'fast',
		...extra
	};
}

/** Journal line poll (the fixture executor/model journal). */
async function waitJournalLine(needle: string, timeoutMs = 60_000): Promise<void> {
	const nodeFs = await fs();
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (nodeFs.readFileSync(JOURNAL, 'utf8').includes(needle)) return;
		await delay(200);
	}
	throw new Error(`journal never showed ${needle}`);
}

/** Deterministic active-lease window: block the run's fixture effect. */
async function holdGate(): Promise<void> {
	(await fs()).writeFileSync(`${GATE_DIR}/${GATE_MARKER}.gate`, '');
}
async function releaseGate(): Promise<void> {
	(await fs()).writeFileSync(`${GATE_DIR}/${GATE_MARKER}.release`, '');
}
async function clearGate(): Promise<void> {
	const nodeFs = await fs();
	nodeFs.rmSync(`${GATE_DIR}/${GATE_MARKER}.gate`, { force: true });
	nodeFs.rmSync(`${GATE_DIR}/${GATE_MARKER}.release`, { force: true });
}

test.describe('canonical conversation client (live fixture backend)', () => {
	test.beforeAll(async () => {
		test.skip(!BASE_URL, 'real-boundary run only: set MAI_E2E_API_URL to a live fixture');
		if (!PASSWORD || !SERVER_A || !GATE_DIR || !JOURNAL) {
			throw new Error(
				'fixture coordinates missing: set MAI_E2E_PASSWORD, MAI_E2E_SERVER_ID, ' +
					'MAI_E2E_FIXTURE_GATES and MAI_E2E_FIXTURE_JOURNAL from the fixture state file'
			);
		}
	});

	test('auth boundary: unauthenticated read is a value-free 401; CSRF-less mutation fails closed', async () => {
		const anonymous = liveChatServices(jar());
		await expect(anonymous.chat.listCanonicalConversations(SERVER_A)).rejects.toMatchObject({
			status: 401,
			code: 'unauthenticated'
		});
		const noCsrf = createMaiApiClient({
			baseUrl: BASE_URL,
			fetch: jar().fetch,
			getCsrfToken: () => null,
			headers: { origin: BASE_URL }
		});
		await expect(noCsrf.createConversation(turn('should never leave the client'))).rejects.toThrow(
			MaiClientError
		);
	});

	test('C2: a fresh session restores the true transcript; pages and the events cursor dedup', async () => {
		const a = await freshSession();
		const submitted = await a.chat.createConversation(turn(C2_MESSAGE));
		expect(submitted.disposition).toBe('created');
		expect(submitted.task_id).toBeNull();
		expect(submitted.revision).toBe(1);
		expect(await waitTerminal(a.chat, submitted.run_id!)).toBe('succeeded');

		// Fresh session: an independent login, zero shared state with `a`.
		const b = await freshSession();
		const restored = await waitMessageTotal(b.chat, submitted.conversation_id, SERVER_A, 2);
		expect(restored.history_completeness).toBe('complete');
		expect(restored.conversation_id).toBe(submitted.conversation_id);
		expect(restored.items!.map((row) => row.role)).toEqual(['user', 'assistant']);
		expect(restored.items![0].content).toBe(C2_MESSAGE);
		expect(restored.items![0].source_kind).toBe('api');
		expect(restored.items![1].content.length).toBeGreaterThan(0);

		// Bounded pagination over the same collection, deterministic order.
		const page1 = await b.chat.listConversationMessages(submitted.conversation_id, SERVER_A, {
			limit: 1,
			offset: 0
		});
		expect(page1.total).toBe(2);
		expect(page1.has_more).toBe(true);
		expect(page1.items!.map((row) => row.sequence)).toEqual([restored.items![0].sequence]);
		const page2 = await b.chat.listConversationMessages(submitted.conversation_id, SERVER_A, {
			limit: 1,
			offset: 1
		});
		expect(page2.has_more).toBe(false);
		expect(page2.items!.map((row) => row.sequence)).toEqual([restored.items![1].sequence]);
		expect(page2.items![0].sequence).toBeGreaterThan(page1.items![0].sequence);

		// The header carries the honest completeness marker and count.
		const detail = await b.chat.getConversation(submitted.conversation_id, SERVER_A);
		expect(detail.conversation_id).toBe(submitted.conversation_id);
		expect(detail.server_id).toBe(SERVER_A);
		expect(detail.message_count).toBe(2);
		expect(detail.history_completeness).toBe('complete');
		expect(detail.legacy_gaps).toHaveLength(0);

		// Scoped list page: the fresh conversation appears, safe metadata only.
		const list = await b.chat.listCanonicalConversations(SERVER_A, { limit: 50 });
		expect(list.total).toBeGreaterThanOrEqual(1);
		const row = list.items!.find((one) => one.conversation_id === submitted.conversation_id);
		expect(row).toBeTruthy();
		expect(JSON.stringify(row)).not.toContain(C2_MESSAGE);

		// Events cursor walk: strictly cursor-exclusive, gap-free, dedup by
		// construction; identities only, never transcript text.
		const first = await b.chat.listConversationEvents(submitted.conversation_id, SERVER_A, {
			afterSeq: 0,
			limit: 1
		});
		expect(first.total).toBe(2);
		expect(first.after_seq).toBe(0);
		expect(first.offset).toBe(0);
		expect(first.events).toHaveLength(1);
		expect(first.has_more).toBe(true);
		expect(first.events![0].kind).toBe('transcript.appended');
		expect(first.events![0].role).toBe('user');
		expect(first.current.latest_seq).toBe(2);
		expect(JSON.stringify(first)).not.toContain(C2_MESSAGE);
		const tip = first.events![0].seq;
		const rest = await b.chat.listConversationEvents(submitted.conversation_id, SERVER_A, {
			afterSeq: tip
		});
		expect(rest.events).toHaveLength(1);
		expect(rest.events![0].seq).toBe(tip + 1);
		expect(rest.events![0].role).toBe('assistant');
		expect(rest.has_more).toBe(false);
		// Reconnect at the tip: nothing replays.
		const reconnect = await b.chat.listConversationEvents(submitted.conversation_id, SERVER_A, {
			afterSeq: tip + 1
		});
		expect(reconnect.events).toHaveLength(0);
	});

	test('C6: retransmission deduplicates; changed content and stale revision conflict', async () => {
		const a = await freshSession();
		const body = turn('Check the fixture service unit name');
		const first = await a.chat.createConversation(body);
		expect(first.disposition).toBe('created');
		const retry = await a.chat.createConversation(body);
		expect(retry.disposition).toBe('deduplicated');
		expect(retry.conversation_id).toBe(first.conversation_id);
		expect(retry.message_id).toBe(first.message_id);
		expect(retry.run_id).toBe(first.run_id);
		expect(retry.revision).toBe(first.revision);
		expect(await waitTerminal(a.chat, first.run_id!)).toBe('succeeded');

		// Same key, changed content: the stable 409, never an overwrite.
		const changed = a.chat.createConversation({ ...body, message: 'changed body' });
		await expect(changed).rejects.toMatchObject({ status: 409, code: 'client_request_conflict' });

		// A stale task-revision pin on a follow-up: structured 409, value-free.
		await expect(
			a.chat.submitConversationMessage(first.conversation_id, turn('continue', { expected_revision: 999 }))
		).rejects.toMatchObject({ status: 409, code: 'revision_conflict' });

		// The 409 is a typed ApiError carrying the C01 envelope identity.
		const caught = (await changed.catch((error: unknown) => error)) as ApiError;
		expect(caught).toBeInstanceOf(ApiError);
		expect(caught.status).toBe(409);
		expect(caught.code).toBe('client_request_conflict');
		expect(caught.requestId).toBeTruthy();
	});

	test('C6/C5: gated lease admits a queued follow-up (202) and durable steering; busy new_task conflicts', async () => {
		test.setTimeout(180_000);
		await clearGate();
		try {
			const a = await freshSession();
			await holdGate();
			const first = await a.chat.createConversation(turn('Inspect the fixture host state'));
			expect(first.disposition).toBe('created');
			// The gate blocks the run's fixture effect: the lease stays held.
			await waitJournalLine(`gate_wait:fixture.read:${GATE_MARKER}`);

			// Follow-up under the active lease: recorded, honestly queued (202).
			const followUp = await a.chat.submitConversationMessage(
				first.conversation_id,
				turn('continue with the same scope')
			);
			expect(followUp.disposition).toBe('queued_followup');
			expect(followUp.conversation_id).toBe(first.conversation_id);
			expect(followUp.run_id).toBeNull();

			// An exclusive new episode cannot start under the live lease.
			await expect(
				a.chat.submitConversationMessage(first.conversation_id, turn('start over', { task_action: 'new_task' }))
			).rejects.toMatchObject({ status: 409, code: 'conversation_busy' });

			// Deterministic steering onto the lease-holding run: recorded
			// durably; the retransmitted key deduplicates onto the SAME row.
			const steeringId = crypto.randomUUID();
			const steer = await a.chat.steerConversation(first.conversation_id, {
				conversation_id: first.conversation_id,
				server_id: SERVER_A,
				action: 'steer',
				message: 'Prefer read-only output',
				client_request_id: steeringId
			});
			expect(steer.disposition).toBe('created');
			expect(steer.run_id).toBe(first.run_id);
			expect(steer.delivery_status).toBe('pending');
			const steerRetry = await a.chat.steerConversation(first.conversation_id, {
				conversation_id: first.conversation_id,
				server_id: SERVER_A,
				action: 'steer',
				message: 'Prefer read-only output',
				client_request_id: steeringId
			});
			expect(steerRetry.disposition).toBe('deduplicated');
			expect(steerRetry.steering_id).toBe(steer.steering_id);

			// Reconnect at the pre-followup cursor replays EXACTLY the new
			// event — one user row, no gaps, no duplicates (C2/C6).
			const firstEvent = await a.chat.listConversationEvents(first.conversation_id, SERVER_A, {
				afterSeq: 0,
				limit: 1
			});
			expect(firstEvent.total).toBe(2); // user row + the queued follow-up's row
			expect(firstEvent.current.latest_seq).toBe(2);
			const reconnected = await a.chat.listConversationEvents(first.conversation_id, SERVER_A, {
				afterSeq: firstEvent.events![0].seq
			});
			expect(reconnected.events).toHaveLength(1);
			expect(reconnected.events![0].role).toBe('user');
			expect(reconnected.events![0].seq).toBe(2);
			// The durable control rows sit pending at the run's next boundary.
			expect(reconnected.current.pending_steering).toBeGreaterThanOrEqual(1);

			// Release: the run reaches a terminal state; both user turns are
			// preserved in order regardless of the scripted run's verdict.
			await releaseGate();
			expect(TERMINAL_STATES[await waitTerminal(a.chat, first.run_id!, 150_000)]).toBe(true);
			const after = await waitMessageTotal(a.chat, first.conversation_id, SERVER_A, 2);
			expect(after.items!.filter((row) => row.role === 'user').map((row) => row.content)).toEqual([
				'Inspect the fixture host state',
				'continue with the same scope'
			]);
			const header = await a.chat.getConversation(first.conversation_id, SERVER_A);
			expect(header.active_run_id).toBeNull();
		} finally {
			await clearGate();
		}
	});

	test('C8: cross-server references fail closed with zero identity leakage', async () => {
		const a = await freshSession();
		const other = await freshSession();

		// A real second server scope through the typed client.
		const created = await a.client.createProfile({
			name: `act-09-other-${crypto.randomUUID().slice(0, 8)}`,
			prompt_revision: 1,
			environment: 'linux_ssh',
			ssh: {
				host: 'build.example.internal',
				port: 22,
				user: 'deploy',
				host_key: `SHA256:${'A'.repeat(43)}`,
				credential_ref: 'FIXTURE_SSH_KEY'
			},
			privilege: 'none',
			capabilities: []
		});
		const SERVER_B = created.server_id;

		// One conversation per scope.
		const onB = await a.chat.createConversation(turn('other server conversation body', { server_id: SERVER_B }));
		expect(onB.disposition).toBe('created');
		const onA = await a.chat.createConversation(turn('primary scope conversation body'));
		await waitTerminal(a.chat, onA.run_id!);

		// Every cross-scope reference (read AND mutation) fails closed with
		// the scope-blind 404; no foreign identity appears in any body.
		const attempts: Array<() => Promise<unknown>> = [
			() => other.chat.getConversation(onA.conversation_id, SERVER_B),
			() => other.chat.listConversationMessages(onA.conversation_id, SERVER_B),
			() => other.chat.listConversationEvents(onA.conversation_id, SERVER_B, { afterSeq: 0 }),
			() =>
				other.chat.submitConversationMessage(onA.conversation_id, {
					server_id: SERVER_B,
					client_request_id: crypto.randomUUID(),
					message: 'cross-server turn',
					profile: 'fast'
				}),
			() =>
				other.chat.steerConversation(onA.conversation_id, {
					conversation_id: onA.conversation_id,
					server_id: SERVER_B,
					action: 'pause'
				})
		];
		for (const attempt of attempts) {
			const error = (await attempt().then(
				() => null,
				(e: unknown) => e
			)) as ApiError | null;
			expect(error, 'cross-server call must fail').toBeInstanceOf(ApiError);
			expect(error!.status).toBe(404);
			expect(error!.code).toBe('conversation_not_found');
			// The rendered error (envelope fields + message text) carries none
			// of the other scope's identities or content.
			const rendered = `${JSON.stringify(error)} ${error!.message}`;
			for (const fragment of [
				onA.conversation_id,
				SERVER_A,
				onA.run_id ?? '',
				'primary scope conversation body'
			]) {
				if (fragment) expect(rendered).not.toContain(fragment);
			}
		}

		// The scoped list never shows the foreign conversation, either way.
		const listA = await a.chat.listCanonicalConversations(SERVER_A, { limit: 200 });
		expect(listA.items!.some((one) => one.conversation_id === onB.conversation_id)).toBe(false);
		const listB = await a.chat.listCanonicalConversations(SERVER_B, { limit: 200 });
		expect(listB.items!.map((one) => one.conversation_id)).toEqual([onB.conversation_id]);
		expect(listB.total).toBe(1);
	});
});
