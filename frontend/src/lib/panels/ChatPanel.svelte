<script module lang="ts">
	/**
	 * ChatPanel — reusable durable-conversation panel (card `mai-06-web-chat`,
	 * P06; service row `mai-web`; contracts C05/C06/C08/C11).
	 *
	 * Layer: feature panel (windowed-workspace ADR-0001 §2 layer table). It owns
	 * its one domain view and nothing else:
	 *
	 *  - Explicit context only: a SERVER-scoped conversation view. Every call
	 *    names the target pinned at open time (`panel.scope.server_id`, C01
	 *    rule 2) — the submit body carries it, the history list is queried with
	 *    it, and attachments reference artifacts of that same target. The module
	 *    reads no launcher/current-server value anywhere, so there is no hidden
	 *    global mutation target and a launcher re-selection can never retarget
	 *    an open conversation, a submit or an approval decision (C08 rule 5,
	 *    NT5).
	 *  - Explicit services only: the eight-function `ChatApi` enters through
	 *    `panel.services.api` (the typed bag owned by the feature entry
	 *    module). A missing or incomplete bag fails closed with a visible
	 *    state — no service locator, no fallback client.
	 *  - Durable truth, replayed (C05 rule 4): everything rendered is
	 *    value-free wire truth — the replayed `ConversationView`, the recorded
	 *    attachment handles, and the run's persisted event stream. The message
	 *    body shown is the text THIS window authored at submit time; the wire
	 *    carries no task/prompt bodies, so a conversation opened from history
	 *    says exactly that instead of guessing. Streamed replies render as
	 *    recorded facts (rounds, requested tool calls with capability and
	 *    payload-hash handles, output sizes, final summary) — never invented
	 *    text (C08 rule 6: never all-green on unknown data).
	 *  - One child-tree implementation: the delegation subtree is the reviewed
	 *    `DelegationTree` view (`06-web-delegation`), composed with this
	 *    panel's bag — deliberately not re-implemented (brief acceptance 2).
	 *    Child task labels are the labels this window submitted, linked through
	 *    the producer's `agent.delegation_planned` child-id order (body order,
	 *    frozen by `plan_children`); a history-opened conversation renders ids
	 *    only. Cancellation is the tree's explicit tree-cancel control; this
	 *    panel adds no second one.
	 *  - Approval path (C03 rule 4): when the record shows a bounded action
	 *    that required a decision (`step.awaiting_approval`, an
	 *    approval-shaped step outcome, or a `waiting_approval` state), the
	 *    panel reads the run's durable step receipts (`05-jobs-api` read) and,
	 *    when one names a bound decision, renders the binding field-for-field
	 *    and records the ONE-USE decision (`03-approval-api`). No bound record
	 *    is an honest "nothing to decide" state; consumed/expired render as
	 *    recorded. The panel never invents an approval and never re-presents a
	 *    spent one.
	 *  - Event replay without duplicates (C08 rule 7): the subscription
	 *    resumes from the last seen `seq` carried as the SSE `Last-Event-ID`,
	 *    survives the server's bounded stream close and API/network restarts
	 *    with a small fixed backoff, and deduplicates by the monotonic `seq`
	 *    watermark. The stream is read-only: it never submits, cancels or
	 *    retargets anything (windowed ADR §7). Closing/hiding the window
	 *    releases the subscription and starts and cancels nothing.
	 *  - No window mechanics: no drag/geometry/focus/persistence code and no
	 *    workspace-state imports; the window host supplies all of that. The
	 *    stream/refresh controllers are instance-scoped, released on unmount
	 *    (the `$effect` teardown is the release path), and never survive into
	 *    another mount cycle.
	 *  - No module-level state and no `window`/`document`/storage access (ADR
	 *    §5 rule 4): safe to evaluate per SSR request; instances share nothing
	 *    between requests or windows.
	 *
	 * Tokens come only from ui/tokens.css; no dependency is added (ADR §4.3).
	 */
</script>

<script lang="ts">
	import { onDestroy } from 'svelte';
	import Button from '../ui/Button.svelte';
	import Field from '../ui/Field.svelte';
	import DelegationTree from '../components/DelegationTree.svelte';
	import { readMaiEvent, type MaiRunEvent } from '../api/client.js';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import {
		GLOBAL_CONSULT_SERVER_ID,
		type ApprovalView,
		type CancelView,
		type ChatApi,
		type ChatPage,
		type ChatServerOption,
		type ChatServiceOption,
		type ChatServices,
		type ChatSubmitBody,
		type ConversationView,
		type DecisionBody,
		type RunDetail
	} from '../workspace/entries/06-web-chat.js';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readApi(services: PanelServices): ChatApi | null {
		const candidate = services.api as Partial<ChatApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (
			const name of [
				'submitMessage',
				'listConversations',
				'readConversation',
				'cancelConversation',
				'streamConversationEvents',
				'readRun',
				'readApproval',
				'decideApproval'
			] as const
		) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as ChatApi;
	}

	const api = $derived(readApi(panel.services));
	/** Conversation submission: a global mount contradicts the entry contract. */
	const serverScope = $derived(panel.scope.kind === 'server' ? panel.scope.server_id : null);
	const usable = $derived(api !== null && serverScope !== null);

	// --- draft chat context (Server Chip + @service tags; locked once started)

	/**
	 * Optional bag extras (typed as the frozen ChatServices shape): the server
	 * list for the draft picker and the mapped/discovered services for the
	 * `@service` autocomplete. A host that passes only `api` still works —
	 * every control below degrades to the pinned scope alone.
	 */
	const serverOptions = $derived.by<readonly ChatServerOption[]>(() => {
		const bag = panel.services as Partial<ChatServices> | undefined;
		const list = bag?.serverOptions;
		if (!Array.isArray(list)) return [];
		return list.filter(
			(one): one is ChatServerOption =>
				typeof one === 'object' && one !== null && typeof one.id === 'string'
		);
	});
	const availableServices = $derived.by<readonly ChatServiceOption[]>(() => {
		const bag = panel.services as Partial<ChatServices> | undefined;
		const list = bag?.availableServices;
		if (!Array.isArray(list)) return [];
		return list.filter(
			(one): one is ChatServiceOption =>
				typeof one === 'object' && one !== null && typeof one.name === 'string'
		);
	});

	/**
	 * Draft target override: `undefined` follows the pinned window scope (the
	 * default Server Chip), a server id pins that server, `null` is General
	 * Consult mode. Once a conversation is open the displayed context is the
	 * RECORDED one — immutable for that session.
	 */
	let draftServerOverride = $state<string | null | undefined>(undefined);
	let selectedServiceTags = $state<string[]>([]);

	/** Display label of one server id: the option list first, then the id. */
	function serverLabelFor(id: string): string {
		return serverOptions.find((one) => one.id === id)?.label ?? `Server ${shortId(id)}`;
	}

	// --- @service autocomplete (draft mode, server pinned) --------------------

	let servicePickerOpen = $state(false);
	let serviceQuery = $state('');

	/**
	 * Suggestions: the host's mapped/discovered services filtered by the typed
	 * `@<query>`, plus one explicit custom entry so a user can pin an
	 * arbitrary `@tag` the inventory does not list yet.
	 */
	const serviceSuggestions = $derived.by<ChatServiceOption[]>(() => {
		const query = serviceQuery.trim().toLowerCase();
		const matched = availableServices.filter(
			(one) => query === '' || one.name.toLowerCase().includes(query)
		);
		const seen = new Set(matched.map((one) => one.name));
		const custom = serviceQuery.trim();
		if (custom !== '' && !seen.has(custom)) matched.push({ name: serviceQuery.trim() });
		return matched;
	});

	/** Typing `@<token>` in the message input opens the autocomplete. */
	function handleComposeInput(event: Event): void {
		const el = event.currentTarget as HTMLTextAreaElement;
		const upto = el.value.slice(0, el.selectionStart ?? el.value.length);
		const match = /(?:^|[\s])@([^\s@]*)$/.exec(upto);
		if (match !== null && !chatContextLocked && !generalConsultMode) {
			serviceQuery = match[1];
			servicePickerOpen = true;
		} else if (servicePickerOpen) {
			servicePickerOpen = false;
			serviceQuery = '';
		}
	}

	function pickService(name: string): void {
		const tag = name.trim();
		if (tag !== '' && !selectedServiceTags.includes(tag)) {
			selectedServiceTags = [...selectedServiceTags, tag];
		}
		servicePickerOpen = false;
		serviceQuery = '';
		// The consumed `@<token>` leaves the draft text.
		composeMessage = composeMessage.replace(/@[^\s@]*$/, '');
	}

	function removeServiceTag(tag: string): void {
		selectedServiceTags = selectedServiceTags.filter((one) => one !== tag);
	}

	/** The docked Service Map seam pre-populates a new draft via this event. */
	function handleFocusService(event: Event): void {
		if (chatContextLocked) return;
		const detail = (event as CustomEvent<unknown>).detail;
		if (typeof detail !== 'object' || detail === null) return;
		const record = detail as Record<string, unknown>;
		if (typeof record.serverId === 'string' && record.serverId !== '') {
			draftServerOverride = record.serverId;
		}
		const tag =
			typeof record.serviceName === 'string'
				? record.serviceName
				: typeof record.serviceTag === 'string'
					? record.serviceTag
					: null;
		if (tag !== null && tag.trim() !== '' && !selectedServiceTags.includes(tag)) {
			selectedServiceTags = [...selectedServiceTags, tag];
		}
	}

	// --- floating chat history selector ---------------------------------------

	let historyOpen = $state(false);

	function selectHistoryRow(row: ConversationView): void {
		historyOpen = false;
		openConversation(row);
	}

	/** Stable element-id namespace for this instance (labels ⇄ inputs). */
	const uid = $props.id();

	// --- producer bounds mirrored for display only (server re-validates) -----

	/** Bounded history page size (C01 rule 5); the producer clamps the maximum. */
	const PAGE_LIMIT = 50;
	/** Fixed reconnect delay after a stream close/failure (bounded, visible). */
	const BACKOFF_MS = 750;
	/** Debounce for the durable refresh triggered by delivered events. */
	const REFRESH_DEBOUNCE_MS = 150;
	/** Rendered-transcript cap: a long replay cannot grow the DOM without bound. */
	const MAX_RENDERED_EVENTS = 500;
	/** Explicit children per submission (delegate.MAX_CHILDREN_PER_RUN). */
	const MAX_CHILDREN = 8;
	/** The frozen default role/mode this panel submits (chat.py DEFAULT_*). */
	const SUBMIT_ROLE = 'administrator';
	const SUBMIT_MODE = 'ask' as const;

	/** Run lifecycle states after which nothing new can be learned (C05 rule 2). */
	const TERMINAL_RUN_STATES: ReadonlySet<string> = new Set(['succeeded', 'failed', 'cancelled']);

	const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
	const SHA256_RE = /^[0-9a-f]{64}$/;

	// --- value-free display helpers (C08 rule 6) -----------------------------

	const RUN_STATE_TEXT: Readonly<Record<string, string>> = Object.freeze({
		queued: 'Queued',
		running: 'Running',
		waiting_approval: 'Waiting for approval decision',
		waiting_children: 'Waiting for child runs',
		reconciling: 'Reconciling an unproven outcome',
		succeeded: 'Succeeded',
		failed: 'Failed',
		cancelled: 'Cancelled (verified)'
	});

	function runStateText(state: string | null): string {
		if (state === null) return 'State not recorded yet';
		return RUN_STATE_TEXT[state] ?? `Recorded state: ${state}`;
	}

	function shortId(id: string | null): string {
		return id === null || id === '' ? '—' : id.slice(0, 8);
	}

	function shortHash(hash: unknown): string {
		return typeof hash === 'string' && hash !== '' ? hash.slice(0, 12) : '—';
	}

	function codeOf(error: unknown): string | null {
		if (typeof error !== 'object' || error === null || !('code' in error)) return null;
		return typeof error.code === 'string' ? error.code : null;
	}

	function messageOf(error: unknown): string | null {
		return error instanceof Error && error.message ? error.message : null;
	}

	/** Boundary guard: one unknown event payload as a record, else null. */
	function recordOf(value: unknown): Record<string, unknown> | null {
		if (typeof value !== 'object' || value === null) return null;
		return value as Record<string, unknown>;
	}

	function asStringArray(value: unknown): string[] {
		if (!Array.isArray(value)) return [];
		return value.filter((item): item is string => typeof item === 'string');
	}

	/** Stable, display-safe text for the frozen C01 codes (no raw payloads). */
	function friendlyText(code: string, fallback: string): string {
		if (code === 'unauthenticated') return 'Your session has ended — sign in again.';
		if (code === 'throttled') return 'Too many requests — try again shortly.';
		if (code === 'request_rejected') return 'The request was rejected; nothing was changed.';
		if (code === 'validation_failed') return 'The submission was not accepted; see the details.';
		return fallback;
	}

	/** Comma/whitespace separated free list → trimmed, deduped, order-kept. */
	function parseNameList(raw: string): string[] {
		const seen = new Set<string>();
		const out: string[] = [];
		for (const piece of raw.split(/[\s,]+/)) {
			const name = piece.trim();
			if (name === '' || seen.has(name)) continue;
			seen.add(name);
			out.push(name);
		}
		return out;
	}

	function sleep(ms: number, signal: AbortSignal): Promise<void> {
		return new Promise((resolve) => {
			const timer = setTimeout(resolve, ms);
			signal.addEventListener(
				'abort',
				() => {
					clearTimeout(timer);
					resolve();
				},
				{ once: true }
			);
		});
	}

	// --- compose form (instance-local; the ONLY submit path of this panel) ----

	let composeMessage = $state('');
	let composeProfile = $state('');
	let composeCapabilities = $state('');
	let composeChildren = $state<string[]>(['']);
	let composeAttachments = $state('');
	let composeBusy = $state(false);
	let composeProblem = $state<{ code: string; message: string } | null>(null);
	let composeFields = $state<Record<string, string>>({});

	const childSlots = $derived(composeChildren.length);

	function addChildSlot(): void {
		if (composeChildren.length >= MAX_CHILDREN) return;
		composeChildren = [...composeChildren, ''];
	}

	function removeChildSlot(index: number): void {
		composeChildren = composeChildren.filter((_, i) => i !== index);
	}

	/** Local pre-flight: only the checks with a user-fixable answer here. */
	function composeFieldErrors(): Record<string, string> {
		const errors: Record<string, string> = {};
		if (composeMessage.trim() === '') errors.message = 'A message is required.';
		if (composeProfile.trim() === '') errors.profile = 'A model profile is required.';
		for (const opId of parseNameList(composeAttachments)) {
			if (!UUID_RE.test(opId)) {
				errors.attachments = 'Attachment references must be canonical op ids (UUIDs).';
				break;
			}
		}
		return errors;
	}

	function buildSubmitBody(runId: string): ChatSubmitBody {
		const serverId = generalConsultMode
			? GLOBAL_CONSULT_SERVER_ID
			: (effectiveServerId ?? GLOBAL_CONSULT_SERVER_ID);
		const tasks = composeChildren.map((task) => task.trim()).filter((task) => task !== '');
		const body: ChatSubmitBody = {
			run_id: runId,
			server_id: serverId,
			message: composeMessage.trim(),
			profile: composeProfile.trim(),
			role: SUBMIT_ROLE,
			mode: SUBMIT_MODE,
			capabilities: parseNameList(composeCapabilities),
			children: tasks.map((task) => ({ task })),
			attachments: parseNameList(composeAttachments).map((opId) => ({
				server_id: serverId,
				op_id: opId
			}))
		};
		// Recorded once, immutable thereafter: `@service` chips ride the submit
		// body ONLY when the draft actually carries them — an empty selection
		// omits the field entirely (the producer treats omission as no tags).
		if (selectedServiceTags.length > 0) body.service_tags = [...selectedServiceTags];
		return body;
	}

	async function submitCompose(): Promise<void> {
		if (api === null || serverScope === null || composeBusy) return;
		composeFields = composeFieldErrors();
		composeProblem = null;
		if (Object.keys(composeFields).length > 0) return;
		const body = buildSubmitBody(crypto.randomUUID());
		const childTasks = body.children ?? [];
		composeBusy = true;
		try {
			const created = await api.submitMessage(body);
			if (disposed) return;
			// This window authored the submission: the message and the ordered
			// child tasks are the labels the tree may render (child ids arrive
			// value-free on the planned event, in body order — plan_children).
			authoredLabels.clear();
			authoredLabels.set(created.conversation.run_id, body.message);
			pendingChildTasks = childTasks.map((child) => child.task);
			openConversation(created.conversation);
			composeMessage = '';
			composeChildren = [''];
			composeAttachments = '';
			void loadHistory();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			composeProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The submission could not be recorded.')
			};
			if (error !== null && typeof error === 'object' && 'fields' in error) {
				const fields = (error as { fields: unknown }).fields;
				if (typeof fields === 'object' && fields !== null) {
					const next: Record<string, string> = {};
					for (const [key, value] of Object.entries(fields as Record<string, unknown>)) {
						if (typeof value === 'string') next[key] = value;
					}
					composeFields = next;
				}
			}
		} finally {
			if (!disposed) composeBusy = false;
		}
	}

	// --- history (bounded page of the pinned server; C01 rules 5 and 7) ------

	let historyRows = $state<ConversationView[]>([]);
	let historyTotal = $state(0);
	let historyState = $state<{ status: 'idle' | 'loading' | 'ready' | 'error' }>({ status: 'idle' });
	let historyProblem = $state<{ code: string; message: string } | null>(null);

	async function loadHistory(): Promise<void> {
		if (api === null || serverScope === null) return;
		const scope = serverScope;
		historyState = { status: 'loading' };
		historyProblem = null;
		try {
			const page: ChatPage = await api.listConversations(scope, { limit: PAGE_LIMIT });
			if (disposed || scope !== serverScope) return;
			historyRows = page.conversations;
			historyTotal = page.total;
			historyState = { status: 'ready' };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			historyRows = [];
			historyTotal = 0;
			historyState = { status: 'error' };
			historyProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The conversation list could not be read.')
			};
		}
	}

	// --- open conversation (submit outcome or a history row) ------------------

	let conversation = $state<ConversationView | null>(null);
	/**
	 * The refreshed replayed root row. `null` until this instance's own
	 * refresh lands; the display root is the opener-held row until then.
	 */
	let refreshed = $state<ConversationView | null>(null);
	const display = $derived(refreshed ?? conversation);
	const activeRunId = $derived(display === null ? '' : display.run_id);

	/**
	 * Immutable-once-started: the moment a conversation is open — submitted
	 * here or restored from history — its recorded server and `@service`
	 * context is the ONLY context; the remove/pick controls leave the DOM and
	 * the chips render `data-locked="true"` until `New Chat` resets the draft.
	 */
	const chatContextLocked = $derived(display !== null);
	const effectiveServerId = $derived(
		chatContextLocked
			? (display?.server_id ?? null)
			: draftServerOverride === undefined
				? serverScope
				: draftServerOverride
	);
	const generalConsultMode = $derived(
		effectiveServerId === null || effectiveServerId === GLOBAL_CONSULT_SERVER_ID
	);
	const rootTerminal = $derived(
		display !== null && display.state !== null && TERMINAL_RUN_STATES.has(display.state)
	);

	/**
	 * Labels THIS window authored, keyed by run id: the root message and the
	 * child tasks linked through the planned event's body-ordered ids. A
	 * conversation opened from history has none — the wire carries no bodies.
	 */
	let authoredLabels = $state(new Map<string, string>());
	let pendingChildTasks = $state<string[]>([]);
	let taskLabels = $state<Record<string, string>>({});

	function openConversation(view: ConversationView): void {
		conversation = view;
		refreshed = null;
		taskLabels = Object.fromEntries(authoredLabels);
		needsApproval = false;
		approvalBinding = null;
		approval = null;
		approvalProblem = null;
		approvalBusy = false;
		decisionStatus = null;
		resetFold();
		// The recorded conversation RESTORES its frozen context and locks it:
		// the General Consult sentinel restores as the consult badge, any
		// other id restores as the Server Chip, the recorded `@service` tags
		// become (locked) chips, and the recorded model profile refills the
		// composer. A row without recorded tags keeps the current draft tags
		// (the producer records them at submission; older rows predate them).
		draftServerOverride =
			view.server_id === GLOBAL_CONSULT_SERVER_ID ? null : view.server_id;
		if (Array.isArray(view.service_tags)) selectedServiceTags = asStringArray(view.service_tags);
		composeProfile = view.model_profile;
		servicePickerOpen = false;
		serviceQuery = '';
		void loadConversation();
		startStream();
	}

	/**
	 * `+ New Chat`: drop the open conversation, release its stream and reset
	 * the composer to a fresh UNLOCKED draft — the default Server Chip back
	 * on the pinned scope, no `@service` tags, no profile/capability/child
	 * residue, history untouched.
	 */
	function newChat(): void {
		finishStream();
		conversation = null;
		refreshed = null;
		runDetail = null;
		taskLabels = {};
		needsApproval = false;
		approvalBinding = null;
		approval = null;
		approvalProblem = null;
		approvalBusy = false;
		decisionStatus = null;
		resetFold();
		composeMessage = '';
		composeChildren = [''];
		composeAttachments = '';
		composeCapabilities = '';
		composeFields = {};
		composeProblem = null;
		draftServerOverride = undefined;
		selectedServiceTags = [];
		servicePickerOpen = false;
		serviceQuery = '';
		historyOpen = false;
	}

	// --- transcript fold (value-free stream facts; hostile text stays text) ---

	type TranscriptEntry = {
		seq: number;
		kind: 'submitted' | 'started' | 'context' | 'round' | 'reply' | 'tool' | 'outcome'
			| 'completed' | 'failed' | 'reconciling';
		label: string;
		rows: Array<{ k: string; v: string }>;
	};

	let transcript = $state<TranscriptEntry[]>([]);
	/** Watermark of the applied stream events (C08 rule 7 dedup). */
	let lastSeq = $state(0);
	let appliedEvents = $state(0);
	let streamStatus = $state<'off' | 'connecting' | 'reconnecting' | 'closed'>('off');
	let streamProblem = $state<string | null>(null);
	let refreshProblem = $state<{ code: string; message: string } | null>(null);

	/** Non-reactive controller locals; the reactive mirrors drive the view only. */
	let streamAbort: AbortController | null = null;
	let refreshTimer: ReturnType<typeof setTimeout> | null = null;
	/** Set once the mount cycle ended; async continuations check before writes. */
	let disposed = true;

	function pushEntry(entry: TranscriptEntry): void {
		if (transcript.length >= MAX_RENDERED_EVENTS) transcript = transcript.slice(1);
		transcript = [...transcript, entry];
	}

	function rowOf(data: unknown, key: string): string {
		const value = recordOf(data)?.[key];
		if (typeof value === 'string') return value;
		if (typeof value === 'number') return String(value);
		return '—';
	}

	function foldEvent(event: MaiRunEvent): void {
		const data = event.data;
		switch (event.type) {
			case 'chat.submitted': {
				const record = recordOf(data);
				const attachments = Array.isArray(record?.attachments) ? record.attachments : [];
				pushEntry({
					seq: event.seq,
					kind: 'submitted',
					label: 'Submission recorded',
					rows: [
						{ k: 'handler', v: rowOf(data, 'handler') },
						{ k: 'children', v: rowOf(data, 'children') },
						{ k: 'attachments', v: String(attachments.length) },
						{ k: 'submission_sha256', v: shortHash(rowOf(data, 'submission_sha256')) }
					]
				});
				break;
			}
			case 'agent.started': {
				pushEntry({
					seq: event.seq,
					kind: 'started',
					label: 'Model bound',
					rows: [
						{ k: 'model', v: rowOf(data, 'model') },
						{ k: 'profile', v: rowOf(data, 'profile') },
						{ k: 'connection', v: rowOf(data, 'connection') },
						{ k: 'role', v: rowOf(data, 'role') },
						{ k: 'rounds_budget', v: rowOf(data, 'rounds_budget') }
					]
				});
				break;
			}
			case 'agent.context_assembled': {
				const record = recordOf(data);
				pushEntry({
					seq: event.seq,
					kind: 'context',
					label: 'Context assembled',
					rows: [
						{ k: 'evidence_items', v: rowOf(data, 'evidence_items') },
						{ k: 'truncated', v: record?.truncated === true ? 'yes' : 'no' },
						{ k: 'prompt_revision', v: rowOf(data, 'prompt_revision') }
					]
				});
				break;
			}
			case 'agent.model_round': {
				const record = recordOf(data);
				const round = rowOf(data, 'round');
				if (record?.status === 'tool_calls') {
					const calls = Array.isArray(record.tool_calls) ? record.tool_calls : [];
					pushEntry({
						seq: event.seq,
						kind: 'round',
						label: `Round ${round} — tool calls requested`,
						rows: calls.map((call, index) => {
							const one = recordOf(call);
							return {
								k: typeof one?.capability_id === 'string' ? one.capability_id : `call ${index}`,
								v: `seq ${String(one?.seq ?? '—')} · payload ${shortHash(one?.payload_hash)}`
							};
						})
					});
				} else if (record?.status === 'final') {
					pushEntry({
						seq: event.seq,
						kind: 'reply',
						label: `Round ${round} — final answer recorded`,
						rows: [
							{
								k: 'output_chars',
								v: rowOf(data, 'output_chars')
							},
							{
								k: 'body',
								v: 'held in the durable run history — the wire carries no reply text'
							}
						]
					});
				}
				break;
			}
			case 'step.started': {
				pushEntry({
					seq: event.seq,
					kind: 'tool',
					label: 'Tool attempt started',
					rows: [{ k: 'capability_id', v: rowOf(data, 'capability_id') }]
				});
				break;
			}
			case 'step.completed': {
				const reason = rowOf(data, 'reason');
				if (reason.startsWith('approval')) needsApproval = true;
				pushEntry({
					seq: event.seq,
					kind: 'outcome',
					label: 'Tool outcome recorded',
					rows: [
						{ k: 'capability_id', v: rowOf(data, 'capability_id') },
						{ k: 'reason', v: reason },
						{ k: 'op_id', v: rowOf(data, 'op_id') }
					]
				});
				break;
			}
			case 'step.awaiting_approval': {
				needsApproval = true;
				break;
			}
			case 'agent.awaiting_reconciliation': {
				pushEntry({
					seq: event.seq,
					kind: 'reconciling',
					label: 'Outcome unknown — reconciliation pending',
					rows: [{ k: 'deadline', v: rowOf(data, 'deadline') }]
				});
				break;
			}
			case 'agent.completed': {
				pushEntry({
					seq: event.seq,
					kind: 'completed',
					label: 'Conversation completed',
					rows: [
						{ k: 'rounds', v: rowOf(data, 'rounds') },
						{ k: 'tool_calls', v: rowOf(data, 'tool_calls') },
						{ k: 'output_chars', v: rowOf(data, 'output_chars') }
					]
				});
				break;
			}
			case 'agent.failed': {
				pushEntry({
					seq: event.seq,
					kind: 'failed',
					label: 'Conversation failed',
					rows: [{ k: 'reason', v: rowOf(data, 'reason') }]
				});
				break;
			}
			case 'agent.delegation_planned': {
				// The planned child ids are in submitted-body order (frozen by
				// plan_children) — the ONE truthful link to this window's labels.
				if (pendingChildTasks.length > 0) {
					const ids = asStringArray(recordOf(data)?.children);
					const next = { ...taskLabels };
					const bound = Math.min(ids.length, pendingChildTasks.length);
					for (let index = 0; index < bound; index += 1) next[ids[index]] = pendingChildTasks[index];
					taskLabels = next;
					pendingChildTasks = [];
				}
				break;
			}
			default:
				// Delegation/budget events are the tree's fold; run.transition
				// and run.cancel_requested reflect through the durable refresh.
				break;
		}
	}

	// --- durable refresh (replayed facade state; never an engine call) --------

	function scheduleRefresh(): void {
		if (refreshTimer !== null || disposed) return;
		refreshTimer = setTimeout(() => {
			refreshTimer = null;
			void loadConversation();
		}, REFRESH_DEBOUNCE_MS);
	}

	async function loadConversation(): Promise<void> {
		if (api === null) return;
		const id = activeRunId;
		if (id === '') return;
		try {
			const view = await api.readConversation(id);
			if (disposed || id !== activeRunId) return;
			refreshed = view;
			refreshProblem = null;
			if (view.state === 'waiting_approval') needsApproval = true;
			if (needsApproval && !TERMINAL_RUN_STATES.has(view.state ?? '')) void probeApproval();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			refreshProblem = {
				code,
				message: friendlyText(
					code,
					messageOf(error) ?? 'The conversation state could not be refreshed.'
				)
			};
		}
	}

	// --- approval path (receipts → binding → one-use decision; C03 rule 4) ----

	type ApprovalBinding = {
		approvalId: string;
		capabilityId: string;
		payloadHash: string;
		policyRevision: number;
	};

	let needsApproval = $state(false);
	let runDetail = $state<RunDetail | null>(null);
	let approvalBinding = $state<ApprovalBinding | null>(null);
	let approval = $state<ApprovalView | null>(null);
	let approvalProblem = $state<{ code: string; message: string } | null>(null);
	let approvalBusy = $state(false);
	let decisionStatus = $state<string | null>(null);

	/**
	 * The bound decision of THIS run's most recent receipt. Only well-formed
	 * value-free handles qualify (UUID approval id, SHA-256 payload hash,
	 * positive revision, non-empty capability) — anything else is absence,
	 * never a guess (C08 rule 6).
	 */
	function approvalBindingOf(detail: RunDetail): ApprovalBinding | null {
		let found: ApprovalBinding | null = null;
		for (const step of detail.steps) {
			const receipt = recordOf(step.receipt);
			if (receipt === null) continue;
			const approvalId = receipt.approval_id;
			const payloadHash = receipt.payload_hash;
			const policyRevision = receipt.policy_revision;
			const capabilityId = receipt.capability_id;
			if (
				typeof approvalId !== 'string' ||
				!UUID_RE.test(approvalId) ||
				typeof payloadHash !== 'string' ||
				!SHA256_RE.test(payloadHash) ||
				typeof policyRevision !== 'number' ||
				!Number.isInteger(policyRevision) ||
				policyRevision < 1 ||
				typeof capabilityId !== 'string' ||
				capabilityId === ''
			) {
				continue;
			}
			found = {
				approvalId,
				capabilityId,
				payloadHash,
				policyRevision
			};
		}
		return found;
	}

	async function probeApproval(): Promise<void> {
		if (api === null) return;
		const id = activeRunId;
		if (id === '') return;
		try {
			const detail = await api.readRun(id);
			if (disposed || id !== activeRunId) return;
			runDetail = detail;
			const binding = approvalBindingOf(detail);
			if (binding === null) {
				approvalBinding = null;
				approval = null;
				return;
			}
			const view = await api.readApproval(binding.approvalId);
			if (disposed || id !== activeRunId) return;
			approvalBinding = binding;
			approval = view;
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			approvalBinding = null;
			approval = null;
			approvalProblem = {
				code,
				message:
					code === 'not_found'
						? 'No bound decision record exists for this run.'
						: friendlyText(code, messageOf(error) ?? 'The bound decision could not be loaded.')
			};
		}
	}

	async function recordDecision(): Promise<void> {
		const binding = approvalBinding;
		const view = approval;
		if (api === null || binding === null || view === null || approvalBusy) return;
		if (view.status !== 'pending' || display === null) return;
		const body: DecisionBody = {
			capability_id: binding.capabilityId,
			server_id: display.server_id,
			payload_hash: binding.payloadHash,
			policy_revision: binding.policyRevision
		};
		approvalBusy = true;
		approvalProblem = null;
		decisionStatus = null;
		try {
			const recorded = await api.decideApproval(binding.approvalId, body);
			if (disposed) return;
			approval = recorded;
			decisionStatus =
				'Decision recorded — the one-use approval is consumed; the bound action ' +
				'can run when the conversation next requests it.';
			await loadConversation();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			const message =
				code === 'already_consumed'
					? 'The decision was already consumed; a new approval is required.'
					: code === 'expired'
						? 'The approval expired unconsumed; request the action again.'
						: code === 'payload_mismatch' || code === 'revision_mismatch'
							? 'The bound operation changed since the approval was created; a new approval is required.'
							: friendlyText(code, messageOf(error) ?? 'The decision could not be recorded.');
			approvalProblem = { code, message };
			void probeApproval();
		} finally {
			if (!disposed) approvalBusy = false;
		}
	}

	// --- scoped stream controller (watermark dedup; C08 rule 7) ---------------

	function startStream(): void {
		finishStream();
		if (activeRunId === '') return;
		const controller = new AbortController();
		streamAbort = controller;
		streamStatus = 'connecting';
		void streamLoop(activeRunId, controller);
	}

	function finishStream(): void {
		streamAbort?.abort();
		streamAbort = null;
		streamStatus = 'off';
	}

	function resetFold(): void {
		transcript = [];
		lastSeq = 0;
		appliedEvents = 0;
		streamProblem = null;
		refreshProblem = null;
	}

	async function streamLoop(id: string, controller: AbortController): Promise<void> {
		if (api === null) return;
		while (!disposed && id === activeRunId && !controller.signal.aborted) {
			if (rootTerminal) break;
			try {
				await api.streamConversationEvents(id, {
					cursor: lastSeq > 0 ? String(lastSeq) : null,
					signal: controller.signal,
					onEvent: (frame) => {
						if (disposed || id !== activeRunId) return;
						const event = readMaiEvent(frame);
						// Watermark dedup (C08 rule 7): replayed or re-delivered
						// frames at/below the last seen seq are dropped here, and a
						// foreign run's frame never touches this transcript.
						if (event === null || event.run_id !== id || event.seq <= lastSeq) return;
						lastSeq = event.seq;
						foldEvent(event);
						appliedEvents += 1;
						streamProblem = null;
						scheduleRefresh();
					}
				});
			} catch (error) {
				if (disposed || id !== activeRunId || controller.signal.aborted) return;
				if (codeOf(error) === 'not_found') {
					streamProblem = 'No conversation is stored under this id; the stream stays closed.';
					finishStream();
					streamStatus = 'closed';
					return;
				}
				streamProblem = 'The event stream was interrupted; reconnecting from the last event.';
			}
			if (disposed || id !== activeRunId || controller.signal.aborted) return;
			streamStatus = 'reconnecting';
			await sleep(BACKOFF_MS, controller.signal);
		}
		if (id === activeRunId && !controller.signal.aborted) {
			finishStream();
			streamStatus = 'closed';
		}
	}

	// --- mount cycle (the $effect teardown is the release path) ---------------

	$effect(() => {
		if (!usable) return;
		disposed = false;
		void loadHistory();
		return () => {
			disposed = true;
			finishStream();
			if (refreshTimer !== null) {
				clearTimeout(refreshTimer);
				refreshTimer = null;
			}
		};
	});

	/**
	 * The Service Map seam: `window` dispatches `mai:chat-focus-service` with
	 * `{ serverId, serviceName | serviceTag, category }` so clicking
	 * `[Chat @service]` pre-populates a NEW draft chat. A locked (started)
	 * session never mutates: the event is ignored until `New Chat`.
	 */
	$effect(() => {
		if (typeof window === 'undefined') return;
		window.addEventListener('mai:chat-focus-service', handleFocusService);
		return () => window.removeEventListener('mai:chat-focus-service', handleFocusService);
	});

	onDestroy(() => {
		disposed = true;
		finishStream();
		if (refreshTimer !== null) {
			clearTimeout(refreshTimer);
			refreshTimer = null;
		}
	});

	// --- derived view bits -----------------------------------------------------

	const cancelRequested = $derived(display !== null && display.cancel_requested_at !== null);
	const attachments = $derived(display?.attachments ?? []);
	const messageText = $derived(
		display === null
			? ''
			: (taskLabels[display.run_id] ??
				'Authored outside this window — the wire carries no task bodies.')
	);
	const grantsText = $derived(
		display === null ? '' : display.requested_grants.length === 0 ? 'none' : display.requested_grants.join(', ')
	);
	const showApprovalSection = $derived(needsApproval && display !== null);
	const approvalPending = $derived(
		approvalBinding !== null && approval !== null && approval.status === 'pending'
	);
</script>

<div class="mai-chat" data-testid="chat-root">
	{#if !usable}
		<div class="mai-chat__state mai-chat__state--error" role="alert" data-testid="panel-config-error">
			<p>This panel was opened without its conversation service or outside a pinned server scope.</p>
		</div>
	{:else}
		<header class="mai-chat__header">
			<div class="mai-chat__header-row">
				<h3 class="mai-chat__title">Conversation</h3>
				<div class="mai-chat__header-actions">
					<Button variant="secondary" onclick={newChat} data-testid="chat-new-session">
						New Chat
					</Button>
					<Button
						variant="secondary"
						onclick={() => (historyOpen = !historyOpen)}
						aria-expanded={historyOpen}
						data-testid="chat-history-toggle"
					>
						History
					</Button>
				</div>
			</div>
			{#if historyOpen}
				<!-- Floating history selector: the same bounded page the inline
				     list renders, as a header popover that restores the selected
				     session's transcript AND its frozen server/service context. -->
				<div class="mai-chat__history-popover" data-testid="chat-history-popover">
					{#if historyState.status === 'loading'}
						<p class="mai-chat__meta" role="status">Loading conversations…</p>
					{:else if historyState.status === 'error'}
						<p class="mai-chat__note mai-chat__note--error" role="alert">
							{historyProblem?.message}
						</p>
					{:else if historyRows.length === 0}
						<p class="mai-chat__meta">No conversations are recorded for this server.</p>
					{:else}
						<ul class="mai-chat__history-popover-list">
							{#each historyRows as row (row.run_id)}
								<li>
									<button
										type="button"
										class="mai-chat__history-item"
										data-testid="chat-history-item"
										onclick={() => selectHistoryRow(row)}
									>
										<span class="mai-chat__row-id">{shortId(row.run_id)}</span>
										{#if row.server_id === GLOBAL_CONSULT_SERVER_ID}
											<span class="mai-chat__badge">General Consult</span>
										{:else}
											<span class="mai-chat__badge">{serverLabelFor(row.server_id)}</span>
										{/if}
										{#each row.service_tags ?? [] as tag (tag)}
											<span class="mai-chat__badge">@{tag}</span>
										{/each}
										<span
											class="mai-chat__badge"
											class:mai-chat__badge--ok={row.state === 'succeeded'}
											class:mai-chat__badge--danger={row.state === 'failed'}
											class:mai-chat__badge--neutral={row.state === 'cancelled'}
										>
											{runStateText(row.state)}
										</span>
										<span class="mai-chat__meta">{row.submitted_at}</span>
									</button>
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			{/if}
			<p class="mai-chat__meta">
				<span data-testid="chat-scope">Server {shortId(serverScope ?? '')}</span>
				<span>Pinned scope — every action below names this target.</span>
			</p>
		</header>

		<section class="mai-chat__section" aria-label="Compose" data-testid="chat-compose">
			<div class="mai-chat__section-head">
				<h4>New message</h4>
			</div>

			<!-- Draft chat context: the Server Chip (or the General Consult
			     badge when no server is pinned) plus the removable `@service`
			     chips. Once the chat starts everything here is recorded and
			     immutable: chips render data-locked="true" and the remove /
			     pick controls leave the DOM. -->
			<div class="mai-chat__chips" data-testid="chat-context-chips">
				{#if generalConsultMode}
					<span class="mai-chat__badge" data-testid="chat-general-consult-badge">
						General Consult — No Server Pinned
					</span>
				{:else}
					<span
						class="mai-chat__chip"
						data-testid="chat-server-chip"
						data-locked={chatContextLocked ? 'true' : null}
					>
						🖥 {serverLabelFor(effectiveServerId ?? '')}
						{#if !chatContextLocked}
							<button
								type="button"
								class="mai-chat__chip-remove"
								data-testid="chat-server-chip-remove"
								aria-label="Remove pinned server"
								onclick={() => (draftServerOverride = null)}
							>
								×
							</button>
						{/if}
					</span>
				{/if}
				{#each selectedServiceTags as tag (tag)}
					<span
						class="mai-chat__chip"
						data-testid="chat-service-chip"
						data-locked={chatContextLocked ? 'true' : null}
					>
						@{tag}
						{#if !chatContextLocked}
							<button
								type="button"
								class="mai-chat__chip-remove"
								data-testid="chat-service-chip-remove"
								aria-label="Remove service tag"
								onclick={() => removeServiceTag(tag)}
							>
								×
							</button>
						{/if}
					</span>
				{/each}
				{#if chatContextLocked}
					<span class="mai-chat__lock" data-testid="chat-context-locked">
						🔒 Server &amp; @service context locked for this session
					</span>
				{/if}
			</div>

			{#if !chatContextLocked}
				<div class="mai-chat__compose-context">
					<div class="mai-chat__context-controls">
						<label class="mai-chat__label" for="{uid}-server">Target server</label>
						<select
							id="{uid}-server"
							class="mai-chat__server-picker"
							data-testid="chat-server-picker"
							value={generalConsultMode ? GLOBAL_CONSULT_SERVER_ID : effectiveServerId}
							onchange={(event) => {
								const next = (event.currentTarget as HTMLSelectElement).value;
								draftServerOverride = next === GLOBAL_CONSULT_SERVER_ID ? null : next;
							}}
						>
							<option value={GLOBAL_CONSULT_SERVER_ID}>
								General Consult — No Server Pinned
							</option>
							{#each serverOptions as server (server.id)}
								<option value={server.id}>{server.label}</option>
							{/each}
							{#if !generalConsultMode && effectiveServerId !== null && !serverOptions.some((one) => one.id === effectiveServerId)}
								<option value={effectiveServerId}>Server {shortId(effectiveServerId)}</option>
							{/if}
						</select>
						{#if !generalConsultMode}
							<Button
								variant="secondary"
								onclick={() => (servicePickerOpen = !servicePickerOpen)}
								data-testid="chat-service-picker-toggle"
							>
								+ @service
							</Button>
						{/if}
					</div>
					{#if servicePickerOpen && !generalConsultMode}
						<div class="mai-chat__service-autocomplete" data-testid="chat-service-autocomplete">
							{#each serviceSuggestions as service (service.name)}
								<button
									type="button"
									class="mai-chat__service-option"
									data-testid="chat-service-option"
									onclick={() => pickService(service.name)}
								>
									@{service.name}
									{#if service.category}
										<span class="mai-chat__badge">[{service.category}]</span>
									{/if}
								</button>
							{/each}
							{#if serviceSuggestions.length === 0}
								<p class="mai-chat__meta">No services match that filter.</p>
							{/if}
						</div>
					{/if}
				</div>
			{/if}

			<div class="mai-chat__form">
				<label class="mai-chat__label" for="{uid}-message">Message</label>
				<textarea
					id="{uid}-message"
					class="mai-chat__textarea"
					bind:value={composeMessage}
					oninput={handleComposeInput}
					rows={3}
					data-testid="compose-message"
				></textarea>
				{#if composeFields.message}
					<p class="mai-chat__field-error" role="alert">{composeFields.message}</p>
				{/if}
				<Field
					label="Model profile"
					bind:value={composeProfile}
					data-testid="compose-profile"
					error={composeFields.profile ?? ''}
					hint="Explicit selection (C06 rule 1); the stored pairing of the pinned server."
				/>
				<Field
					label="Capabilities"
					bind:value={composeCapabilities}
					data-testid="compose-capabilities"
					error={composeFields.capabilities ?? ''}
					hint="Comma-separated grant request; empty means NO capability."
				/>
				<fieldset class="mai-chat__children">
					<legend class="mai-chat__label">Child tasks (optional, runs on this server)</legend>
					{#each composeChildren as _, index (index)}
						<div class="mai-chat__child" data-testid="compose-child">
							<Field
								label="Child {index + 1} task"
								bind:value={composeChildren[index]}
								data-testid="compose-child-task"
							/>
							<Button
								variant="secondary"
								onclick={() => removeChildSlot(index)}
								disabled={childSlots <= 1}
								data-testid="child-remove"
							>
								Remove
							</Button>
						</div>
					{/each}
					<Button
						variant="secondary"
						onclick={addChildSlot}
						disabled={childSlots >= MAX_CHILDREN}
						data-testid="child-add"
					>
						Add child task
					</Button>
					{#if composeFields.children}
						<p class="mai-chat__field-error" role="alert">{composeFields.children}</p>
					{/if}
				</fieldset>
				<Field
					label="Context attachments"
					bind:value={composeAttachments}
					data-testid="compose-attachments"
					error={composeFields.attachments ?? ''}
					hint="Op ids of stored artifacts of this server (comma-separated); references only."
				/>
				<div class="mai-chat__actions">
					<Button onclick={submitCompose} loading={composeBusy} data-testid="chat-submit">
						Send
					</Button>
				</div>
				{#if composeProblem !== null}
					<div
						class="mai-chat__state mai-chat__state--error"
						role="alert"
						data-testid="compose-error"
						data-error-code={composeProblem.code}
					>
						<p>{composeProblem.message}</p>
					</div>
				{/if}
			</div>
		</section>

		<section class="mai-chat__section" aria-label="Conversations" data-testid="chat-history">
			<div class="mai-chat__section-head">
				<h4>Conversations</h4>
				<Button variant="secondary" onclick={loadHistory} data-testid="history-refresh">
					Refresh
				</Button>
			</div>
			{#if historyState.status === 'loading'}
				<div class="mai-chat__state" role="status" data-testid="history-loading">
					<p>Loading conversations…</p>
				</div>
			{:else if historyState.status === 'error'}
				<div
					class="mai-chat__state mai-chat__state--error"
					role="alert"
					data-testid="history-error"
					data-error-code={historyProblem?.code}
				>
					<p>{historyProblem?.message}</p>
					<Button variant="secondary" onclick={loadHistory} data-testid="history-retry">
						Retry
					</Button>
				</div>
			{:else if historyRows.length === 0}
				<div class="mai-chat__state" role="status" data-testid="history-empty">
					<p>No conversations are recorded for this server.</p>
				</div>
			{:else}
				<ul class="mai-chat__rows" data-testid="history-list">
					{#each historyRows as row (row.run_id)}
						<li class="mai-chat__row" data-testid="history-row" data-run-id={row.run_id}>
							<div class="mai-chat__row-main">
								<span class="mai-chat__row-id" data-testid="history-row-id">
									{shortId(row.run_id)}
								</span>
								<span
									class="mai-chat__badge"
									class:mai-chat__badge--ok={row.state === 'succeeded'}
									class:mai-chat__badge--danger={row.state === 'failed'}
									class:mai-chat__badge--neutral={row.state === 'cancelled'}
									data-testid="history-row-state"
								>
									{runStateText(row.state)}
								</span>
								<span class="mai-chat__meta">{row.model_profile}</span>
							</div>
							<div class="mai-chat__row-actions">
								<Button
									variant="secondary"
									onclick={() => openConversation(row)}
									data-testid="history-open"
								>
									Open
								</Button>
							</div>
						</li>
					{/each}
				</ul>
				<p class="mai-chat__meta" data-testid="history-total">
					Showing {historyRows.length} of {historyTotal} recorded conversations.
				</p>
			{/if}
		</section>

		<section class="mai-chat__section" aria-label="Open conversation" data-testid="chat-conversation">
			{#if display === null}
				<div class="mai-chat__state" role="status" data-testid="chat-none">
					<p>No conversation is open. Send a message or open one from the list.</p>
				</div>
			{:else}
				<div class="mai-chat__section-head">
					<h4>Conversation</h4>
					<span class="mai-chat__meta" data-testid="conversation-id">{shortId(display.run_id)}</span>
					<Button variant="secondary" onclick={loadConversation} data-testid="conversation-refresh">
						Refresh
					</Button>
				</div>

				<p class="mai-chat__line">
					<span
						class="mai-chat__badge"
						class:mai-chat__badge--ok={display.state === 'succeeded'}
						class:mai-chat__badge--danger={display.state === 'failed'}
						class:mai-chat__badge--neutral={display.state === 'cancelled'}
						data-testid="conversation-state"
					>
						{runStateText(display.state)}
					</span>
					{#if cancelRequested}
						<span class="mai-chat__note" data-testid="conversation-cancel-note">
							Cancellation requested
						</span>
					{/if}
				</p>
				<p class="mai-chat__meta" data-testid="conversation-target">
					Server {shortId(display.server_id)} · profile {display.model_profile} · grants
					{grantsText}
				</p>
				<p class="mai-chat__message" data-testid="conversation-message">{messageText}</p>

				<div class="mai-chat__attachments" data-testid="conversation-attachments">
					<h5>Permitted context attachments</h5>
					{#if attachments.length === 0}
						<p class="mai-chat__meta" data-testid="attachments-empty">
							No artifact references are recorded for this conversation.
						</p>
					{:else}
						<ul data-testid="attachment-list">
							{#each attachments as attachment (attachment.op_id)}
								<li class="mai-chat__attachment" data-testid="attachment-row">
									<span data-testid="attachment-op">{shortId(attachment.op_id)}</span>
									<span data-testid="attachment-sha">sha256 {shortHash(attachment.sha256)}</span>
									<span data-testid="attachment-bytes">{attachment.bytes} bytes</span>
								</li>
							{/each}
						</ul>
					{/if}
				</div>

				<p class="mai-chat__meta" data-testid="stream-status">
					{#if streamStatus === 'connecting'}
						Event stream connecting…
					{:else if streamStatus === 'reconnecting'}
						Event stream reconnecting from the last recorded event…
					{:else if streamStatus === 'closed'}
						Event stream closed ({appliedEvents} applied).
					{:else if streamStatus === 'off'}
						Event stream idle.
					{:else}
						Event stream live ({appliedEvents} applied).
					{/if}
				</p>
				{#if streamProblem !== null}
					<p class="mai-chat__note mai-chat__note--error" role="status" data-testid="stream-problem">
						{streamProblem}
					</p>
				{/if}
				{#if refreshProblem !== null}
					<p
						class="mai-chat__note mai-chat__note--error"
						role="status"
						data-testid="refresh-problem"
						data-error-code={refreshProblem.code}
					>
						{refreshProblem.message}
					</p>
				{/if}

				<div class="mai-chat__transcript" data-testid="conversation-transcript">
					<h5>Recorded transcript</h5>
					{#if transcript.length === 0}
						<p class="mai-chat__meta" data-testid="transcript-empty">
							No events are recorded yet.
						</p>
					{:else}
						<ul data-testid="transcript-list">
							{#each transcript as entry (entry.seq)}
								<li
									class="mai-chat__entry"
									data-testid="transcript-entry"
									data-kind={entry.kind}
									data-seq={entry.seq}
								>
									<span class="mai-chat__entry-seq">#{entry.seq}</span>
									<span class="mai-chat__entry-label" data-testid="transcript-label">
										{entry.label}
									</span>
									{#if entry.rows.length > 0}
										<dl class="mai-chat__entry-rows" data-testid="transcript-rows">
											{#each entry.rows as row, rowIndex (rowIndex + row.k)}
												<dt>{row.k}</dt>
												<dd data-testid="transcript-value">{row.v}</dd>
											{/each}
										</dl>
									{/if}
								</li>
							{/each}
						</ul>
					{/if}
				</div>

				{#if showApprovalSection}
					<div class="mai-chat__approval" data-testid="approval-section">
						<h5>Bounded action</h5>
						{#if approvalProblem !== null}
							<p
								class="mai-chat__note mai-chat__note--error"
								role="alert"
								data-testid="approval-error"
								data-error-code={approvalProblem.code}
							>
								{approvalProblem.message}
							</p>
						{:else if approvalBinding === null && approval === null}
							<p class="mai-chat__meta" data-testid="approval-absent">
								A bounded action requires an approval decision; no bound decision record
								exists for this run yet.
							</p>
						{/if}
						{#if approvalBinding !== null && approval !== null}
							<dl class="mai-chat__entry-rows" data-testid="approval-binding">
								<dt>approval_id</dt>
								<dd data-testid="approval-id">{approval.approval_id}</dd>
								<dt>capability_id</dt>
								<dd data-testid="approval-capability">{approvalBinding.capabilityId}</dd>
								<dt>server_id</dt>
								<dd data-testid="approval-server">{shortId(display.server_id)}</dd>
								<dt>payload_hash</dt>
								<dd data-testid="approval-hash">{approvalBinding.payloadHash}</dd>
								<dt>policy_revision</dt>
								<dd data-testid="approval-revision">{approvalBinding.policyRevision}</dd>
								<dt>status</dt>
								<dd data-testid="approval-status">{approval.status}</dd>
								<dt>expires_at</dt>
								<dd data-testid="approval-expires">{approval.expires_at}</dd>
							</dl>
							{#if approvalPending}
								<div class="mai-chat__actions">
									<Button
										onclick={recordDecision}
										loading={approvalBusy}
										data-testid="approval-decide"
									>
										Approve bounded action
									</Button>
								</div>
								<p class="mai-chat__meta">
									One-use decision (C03 rule 4): recording it consumes the approval for
									exactly this binding.
								</p>
							{:else if approval.status === 'consumed'}
								<p class="mai-chat__meta" data-testid="approval-spent">
									Decision consumed{approval.consumed_at === null
										? ''
										: ` at ${approval.consumed_at}`} — the bound action can run when the
									conversation next requests it.
								</p>
							{:else}
								<p class="mai-chat__meta" data-testid="approval-expired">
									Approval status: {approval.status}. No decision can be recorded.
								</p>
							{/if}
						{/if}
						{#if decisionStatus !== null}
							<p class="mai-chat__note" role="status" data-testid="approval-recorded">
								{decisionStatus}
							</p>
						{/if}
					</div>
				{/if}

				<div class="mai-chat__children" data-testid="conversation-children">
					<h5>Delegated children</h5>
					<DelegationTree conversation={display} api={api} taskLabels={taskLabels} />
				</div>
			{/if}
		</section>
	{/if}
</div>

<style>
	.mai-chat {
		display: flex;
		flex-direction: column;
		gap: var(--space-4, 1rem);
		padding: var(--space-4, 1rem);
		color: var(--color-fg, inherit);
		font-size: var(--font-size-sm, 0.875rem);
		min-width: 0;
		min-height: 0;
		overflow: auto;
	}

	.mai-chat__header {
		display: flex;
		flex-direction: column;
		gap: var(--space-1, 0.25rem);
		position: relative;
	}

	.mai-chat__header-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-chat__header-actions {
		display: inline-flex;
		gap: var(--mai-space-2, 0.5rem);
	}

	/* Floating history selector: anchored to the header row, above the page. */
	.mai-chat__history-popover {
		position: absolute;
		top: 100%;
		right: 0;
		z-index: 20;
		width: min(24rem, 100%);
		max-height: 18rem;
		overflow: auto;
		padding: var(--mai-space-2, 0.5rem);
		background: var(--surface-raised, var(--mai-color-surface, inherit));
		border: 1px solid var(--border-strong, currentColor);
		border-radius: var(--mai-radius-sm, 4px);
		box-shadow: var(--mai-shadow-window, none);
	}

	.mai-chat__history-popover-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 0.25rem);
	}

	.mai-chat__history-item {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
		width: 100%;
		padding: var(--mai-space-2, 0.5rem);
		font: inherit;
		color: inherit;
		text-align: start;
		cursor: pointer;
		background: transparent;
		border: 1px solid var(--border-subtle, transparent);
		border-radius: var(--mai-radius-sm, 4px);
	}

	.mai-chat__history-item:hover {
		background: var(--mai-color-bg, transparent);
	}

	.mai-chat__chips {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-chat__chip {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-1, 0.25rem);
		padding: 0 var(--mai-space-2, 0.5rem);
		border: 1px solid var(--border-strong, currentColor);
		border-radius: 999px;
		font-size: var(--mai-text-xs, 0.75rem);
	}

	.mai-chat__chip-remove {
		padding: 0 var(--mai-space-1, 0.25rem);
		font: inherit;
		color: inherit;
		cursor: pointer;
		background: transparent;
		border: none;
		border-radius: 999px;
	}

	.mai-chat__chip-remove:hover {
		background: var(--mai-status-danger-bg, transparent);
		color: var(--mai-status-danger-text, inherit);
	}

	.mai-chat__lock {
		font-size: var(--mai-text-xs, 0.75rem);
		color: var(--color-warning, inherit);
	}

	.mai-chat__compose-context {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
	}

	.mai-chat__context-controls {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-chat__server-picker {
		padding: var(--mai-space-1, 0.25rem) var(--mai-space-2, 0.5rem);
		font: inherit;
		color: inherit;
		background: var(--surface-raised, transparent);
		border: 1px solid var(--border-strong, currentColor);
		border-radius: var(--mai-radius-sm, 4px);
	}

	/* `@service` autocomplete: opens under the composer controls. */
	.mai-chat__service-autocomplete {
		position: absolute;
		top: 100%;
		left: 0;
		z-index: 20;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 0.25rem);
		width: min(20rem, 100%);
		max-height: 14rem;
		overflow: auto;
		padding: var(--mai-space-2, 0.5rem);
		background: var(--surface-raised, var(--mai-color-surface, inherit));
		border: 1px solid var(--border-strong, currentColor);
		border-radius: var(--mai-radius-sm, 4px);
		box-shadow: var(--mai-shadow-window, none);
	}

	.mai-chat__service-option {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
		padding: var(--mai-space-1, 0.25rem) var(--mai-space-2, 0.5rem);
		font: inherit;
		color: inherit;
		text-align: start;
		cursor: pointer;
		background: transparent;
		border: 1px solid var(--border-subtle, transparent);
		border-radius: var(--mai-radius-sm, 4px);
	}

	.mai-chat__service-option:hover {
		background: var(--mai-color-bg, transparent);
	}

	.mai-chat__title {
		margin: 0;
		font-size: var(--font-size-md, 1rem);
	}

	h4,
	h5 {
		margin: 0;
		font-size: var(--font-size-sm, 0.875rem);
	}

	.mai-chat__meta {
		margin: 0;
		color: var(--color-muted, inherit);
		display: inline-flex;
		gap: var(--space-3, 0.75rem);
		flex-wrap: wrap;
	}

	.mai-chat__section {
		display: flex;
		flex-direction: column;
		gap: var(--space-3, 0.75rem);
		border-top: 1px solid var(--border-subtle, transparent);
		padding-top: var(--space-3, 0.75rem);
	}

	.mai-chat__section-head {
		display: flex;
		align-items: baseline;
		gap: var(--space-3, 0.75rem);
		flex-wrap: wrap;
	}

	.mai-chat__form {
		display: flex;
		flex-direction: column;
		gap: var(--space-3, 0.75rem);
	}

	.mai-chat__label {
		font-weight: 600;
	}

	.mai-chat__textarea {
		width: 100%;
		resize: vertical;
		min-height: 3.5rem;
		padding: var(--space-2, 0.5rem);
		border: 1px solid var(--border-strong, currentColor);
		border-radius: var(--radius-sm, 4px);
		background: var(--surface-raised, transparent);
		color: inherit;
		font: inherit;
	}

	.mai-chat__children {
		border: 1px solid var(--border-subtle, transparent);
		border-radius: var(--radius-sm, 4px);
		padding: var(--space-2, 0.5rem);
		display: flex;
		flex-direction: column;
		gap: var(--space-2, 0.5rem);
		margin: 0;
	}

	.mai-chat__child {
		display: flex;
		align-items: end;
		gap: var(--space-2, 0.5rem);
	}

	.mai-chat__child > :global(.mai-field) {
		flex: 1;
	}

	.mai-chat__actions {
		display: flex;
		gap: var(--space-2, 0.5rem);
		align-items: center;
	}

	.mai-chat__field-error {
		margin: 0;
		color: var(--color-danger, inherit);
	}

	.mai-chat__state {
		padding: var(--space-2, 0.5rem);
		border-radius: var(--radius-sm, 4px);
		border: 1px dashed var(--border-subtle, transparent);
	}

	.mai-chat__state--error {
		border-style: solid;
		border-color: var(--color-danger, currentColor);
		color: var(--color-danger, inherit);
	}

	.mai-chat__rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2, 0.5rem);
	}

	.mai-chat__row {
		display: flex;
		justify-content: space-between;
		gap: var(--space-2, 0.5rem);
		align-items: center;
		border: 1px solid var(--border-subtle, transparent);
		border-radius: var(--radius-sm, 4px);
		padding: var(--space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-chat__row-main {
		display: inline-flex;
		gap: var(--space-2, 0.5rem);
		align-items: center;
		flex-wrap: wrap;
	}

	.mai-chat__row-id {
		font-family: var(--font-mono, monospace);
	}

	.mai-chat__badge {
		border: 1px solid var(--border-subtle, currentColor);
		border-radius: 999px;
		padding: 0 var(--space-2, 0.5rem);
	}

	.mai-chat__badge--ok {
		color: var(--color-success, inherit);
		border-color: var(--color-success, currentColor);
	}

	.mai-chat__badge--danger {
		color: var(--color-danger, inherit);
		border-color: var(--color-danger, currentColor);
	}

	.mai-chat__badge--neutral {
		color: var(--color-muted, inherit);
	}

	.mai-chat__line {
		margin: 0;
		display: inline-flex;
		gap: var(--space-2, 0.5rem);
		align-items: center;
		flex-wrap: wrap;
	}

	.mai-chat__note {
		margin: 0;
		color: var(--color-warning, inherit);
	}

	.mai-chat__note--error {
		color: var(--color-danger, inherit);
	}

	.mai-chat__message {
		margin: 0;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		border-left: 3px solid var(--border-subtle, currentColor);
		padding-left: var(--space-2, 0.5rem);
	}

	.mai-chat__attachments,
	.mai-chat__transcript,
	.mai-chat__approval,
	.mai-chat__children {
		display: flex;
		flex-direction: column;
		gap: var(--space-2, 0.5rem);
	}

	.mai-chat__attachments ul,
	.mai-chat__transcript ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-1, 0.25rem);
	}

	.mai-chat__attachment {
		display: inline-flex;
		gap: var(--space-3, 0.75rem);
		font-family: var(--font-mono, monospace);
		font-size: var(--font-size-xs, 0.75rem);
		flex-wrap: wrap;
	}

	.mai-chat__entry {
		display: flex;
		flex-direction: column;
		gap: var(--space-1, 0.25rem);
		border: 1px solid var(--border-subtle, transparent);
		border-radius: var(--radius-sm, 4px);
		padding: var(--space-2, 0.5rem);
	}

	.mai-chat__entry-seq {
		color: var(--color-muted, inherit);
		font-family: var(--font-mono, monospace);
		font-size: var(--font-size-xs, 0.75rem);
	}

	.mai-chat__entry-rows {
		margin: 0;
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0 var(--space-3, 0.75rem);
		font-family: var(--font-mono, monospace);
		font-size: var(--font-size-xs, 0.75rem);
		overflow-wrap: anywhere;
	}

	.mai-chat__entry-rows dt {
		color: var(--color-muted, inherit);
	}

	.mai-chat__entry-rows dd {
		margin: 0;
	}
</style>
