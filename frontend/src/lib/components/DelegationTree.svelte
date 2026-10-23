<script module lang="ts">
	/**
	 * DelegationTree — reusable parent→child delegation-tree inspection view
	 * (card `mai-06-web-delegation`, P06; service row `mai-web`; contracts
	 * C05/C06/C08/C11).
	 *
	 * Layer: feature component (windowed-workspace ADR-0001 §2), NOT a registry
	 * panel: a feature panel (the conversation panel owns composition per its
	 * brief) embeds this view and passes its already-held conversation plus the
	 * typed services bag. There is deliberately exactly one child-tree
	 * implementation in the app (`mai-06-web-chat` composes this one; its brief
	 * forbids a duplicate).
	 *
	 * What this component owns, and nothing else:
	 *
	 *  - Explicit inputs only: the `conversation` prop is the replayed root
	 *    row the opener already holds; the typed `DelegationApi` bag mirrors
	 *    the reviewed `06-chat-api` routes 1:1 (read/cancel/stream on
	 *    `/api/v1/chat/runs/{run_id}`). A missing or incomplete bag fails
	 *    closed with a visible state — no service locator, no fallback client.
	 *    Every call names THIS conversation's `run_id`; the module reads no
	 *    launcher/current-server value anywhere, so there is no hidden global
	 *    mutation target (C08 rule 5, NT5) and a launcher re-selection can
	 *    never retarget a tracked tree.
	 *  - Actual state only (brief acceptance 1, C08 rule 6): statuses, model
	 *    profile, target server, grant chips and provenance (`parent_run_id` +
	 *    `depth`) are rendered from the replayed rows and the parent's
	 *    persisted event stream — never guessed, never all-green on unknown
	 *    data. The tree never renders task/context bodies beyond the
	 *    opener-authored `taskLabels` (the labels the opener itself submitted
	 *    in its ChatSubmitBody children — the only truthful source; the
	 *    producer's `agent.delegation_planned` event is value-free by
	 *    contract), so no secret context is copied into the tree (C05 rule 4).
	 *  - Bounded outcomes are distinguishable (brief acceptance 2): a nested
	 *    child `failed`, a cancelled run (`cancelled` / `cancel_requested_at`)
	 *    and a grant denial (the effective grants intersected to EMPTY —
	 *    GRANT-INTERSECT: a child request can never add to the parent) render
	 *    as three visibly distinct states; a parent `agent.failed` reason is
	 *    shown verbatim as the recorded bounded-failure reason.
	 *  - Budget consumption (C05 rule 7): the root stream's ledger events
	 *    (`agent.budget_reserved`/`released`/`charged`) are folded client-side
	 *    exactly as the reviewed `mai.agents.budgets.ledger_state` fold —
	 *    releases refund outstanding reservations, charged spend is never
	 *    un-spent. Caps live server-side; the view shows consumption, never
	 *    invented remaining-quota numbers.
	 *  - Event replay without duplicates (C08 rule 7): the SSE subscription
	 *    resumes from the last seen `seq` carried as `Last-Event-ID`, survives
	 *    the server's bounded stream close with a small fixed backoff, and
	 *    deduplicates by the monotonic `seq` watermark; child identities are
	 *    discovered once from `agent.delegation_planned` (a set union keyed by
	 *    `run_id`), so a reconnect never duplicates nodes. The stream is
	 *    read-only: it never submits, cancels or retargets anything (windowed
	 *    ADR §7).
	 *  - Honest cancel (engine guard rule 3): the cancel control posts the
	 *    explicit tree-cancel body (`cancel_children: true` — a cancelled
	 *    conversation never orphans delegated work) and renders exactly the
	 *    facade's answer; `outcome: null` is "requested — not yet verified",
	 *    `cancelled` only from the facade's KNOWN outcome.
	 *  - No window mechanics: no drag/geometry/focus/persistence code and no
	 *    workspace-state imports; the embedding panel/window host supplies all
	 *    of that. The stream/refresh controllers are instance-scoped, released
	 *    on unmount (the `$effect` teardown is the release path), and never
	 *    survive into another mount cycle.
	 *  - No module-level mutable state and no `window`/`document`/storage
	 *    access (ADR §5 rule 4): safe to evaluate per SSR request; instances
	 *    share nothing between requests or windows.
	 *
	 * Tokens come only from ui/tokens.css; no dependency is added (ADR §4.3).
	 */
	import { onDestroy, untrack } from 'svelte';
	import Button from '../ui/Button.svelte';
	import type { MaiRunEvent, SubscribeEventsOptions } from '../api/client.js';
	import { readMaiEvent } from '../api/client.js';
	import type { components } from '../api/schema.generated.js';
</script>

<script lang="ts">
	/** Generated wire shapes (C08 rule 8: generated schema only). */
	type ConversationView = components['schemas']['ConversationView'];
	type RunView = components['schemas']['RunView'];
	type ChatCancelBody = components['schemas']['ChatCancelBody'];
	type CancelView = components['schemas']['CancelView'];

	/**
	 * The delegation API slice this view consumes, injected by the embedding
	 * panel (satisfied by the composed client once it grows the chat
	 * transport methods; mirrors the reviewed `06-chat-api` routes 1:1).
	 * The concrete stream URL stays the opener's exclusive resource: this
	 * view never names a path (the sibling convention of `05-web-jobs`).
	 */
	export interface DelegationApi {
		/** GET /api/v1/chat/runs/{run_id} — replayed state + recorded references. */
		readConversation(runId: string): Promise<ConversationView>;
		/** POST /api/v1/chat/runs/{run_id}/cancel — explicit tree cancellation. */
		cancelConversation(runId: string, payload: ChatCancelBody): Promise<CancelView>;
		/**
		 * GET /api/v1/chat/runs/{run_id}/events — authenticated, origin-checked
		 * SSE subscription with `Last-Event-ID` replay (C08 rule 7). Resolves
		 * when the server closes its bounded stream or `signal` aborts; rejects
		 * on HTTP/network failure. Read-only: a disconnected browser cancels
		 * nothing (windowed ADR §7).
		 */
		streamConversationEvents(runId: string, options: SubscribeEventsOptions): Promise<void>;
	}

	let {
		conversation,
		api,
		taskLabels,
		showCancel = true
	}: {
		/** The replayed root conversation row the opener already holds. */
		conversation: ConversationView;
		/** The typed services bag (fail-closed when missing/incomplete). */
		api: unknown;
		/** Opener-authored task labels keyed by run id (never fetched, never invented). */
		taskLabels?: Readonly<Record<string, string>>;
		/**
		 * Render the "Stop this conversation" section. Default `true` — the
		 * standalone tree keeps its explicit cancel control; the Chat panel's
		 * embedded instance passes `false` (the conversation panel owns the
		 * one composer Stop control and adds no second one).
		 */
		showCancel?: boolean;
	} = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readApi(services: unknown): DelegationApi | null {
		const candidate = services as Partial<DelegationApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (const name of ['readConversation', 'cancelConversation', 'streamConversationEvents'] as const) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as DelegationApi;
	}

	const apiBag = $derived(readApi(api));
	/** Inspection view: the conversation prop must carry its identity. */
	const runId = $derived(conversation.run_id);
	const usable = $derived(apiBag !== null && runId !== '');

	// --- frozen vocabularies (C05 rule 2; producer event types C06) ----------

	/** Run lifecycle states after which nothing new can be learned. */
	const TERMINAL_RUN_STATES: ReadonlySet<string> = new Set(['succeeded', 'failed', 'cancelled']);

	/** Event types this view consumes from the ROOT run's stream (value-free). */
	const PLANNED_EVENT = 'agent.delegation_planned';
	const WAIT_EVENT = 'agent.delegation_wait';
	const RESOLVED_EVENT = 'agent.children_resolved';
	const COMPLETED_EVENT = 'agent.delegation_completed';
	const FAILED_EVENT = 'agent.failed';
	const RESERVED_EVENT = 'agent.budget_reserved';
	const RELEASED_EVENT = 'agent.budget_released';
	const CHARGED_EVENT = 'agent.budget_charged';

	/** Fixed reconnect delay after a stream close/failure (bounded, visible). */
	const BACKOFF_MS = 750;
	/** Debounce for the durable refresh triggered by delivered events. */
	const REFRESH_DEBOUNCE_MS = 150;

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

	/** Stable, display-safe text for the frozen C01 codes (no raw payloads). */
	function friendlyText(code: string, fallback: string): string {
		if (code === 'unauthenticated') return 'Your session has ended — sign in again.';
		if (code === 'throttled') return 'Too many requests — try again shortly.';
		if (code === 'request_rejected') return 'The request was rejected; nothing was changed.';
		return fallback;
	}

	// --- tree state -----------------------------------------------------------

	/**
	 * The refreshed replayed root row. `null` until this instance's own
	 * refresh lands; the display root is the opener's prop until then, so a
	 * prop update for the SAME run never resets the live fold.
	 */
	let refreshedRoot = $state<ConversationView | null>(null);
	/** Child rows/identities discovered from the stream but not yet in rows. */
	let extraIds = $state<string[]>([]);
	let eventChildren = $state<Record<string, { state: string | null; depth: number | null }>>({});
	/** Recorded bounded-failure reason of the root (`agent.failed`, value-free). */
	let failureReason = $state<string | null>(null);
	/** Persisted children-wait deadline of the parked parent (value-free). */
	let waitDeadline = $state<string | null>(null);

	// --- budget ledger fold (client mirror of mai.agents.budgets.ledger_state)

	let ledger = $state({
		seen: 0,
		reservations: 0,
		releases: 0,
		activeSlots: 0,
		consumedSteps: 0,
		consumedTokens: 0
	});
	/** Non-reactive mirror; the reactive `activeSlots` count is updated alongside. */
	let outstanding = new Set<string>();

	// --- stream controller state ----------------------------------------------

	let streamStatus = $state<'off' | 'connecting' | 'reconnecting' | 'closed'>('off');
	let streamProblem = $state<string | null>(null);
	let refreshProblem = $state<{ code: string; message: string } | null>(null);
	/** Applied (deduplicated) event count + watermark, for the visible cursor. */
	let appliedEvents = $state(0);
	let lastSeq = $state(0);

	/** Non-reactive controller locals; the reactive mirrors drive the view only. */
	let streamAbort: AbortController | null = null;
	let refreshTimer: ReturnType<typeof setTimeout> | null = null;
	/** Set once the mount cycle ended; async continuations check before writes. */
	let disposed = true;

	// --- merged tree projection -----------------------------------------------

	const displayRoot = $derived(refreshedRoot ?? conversation);

	/** One rendered node: a replayed child row, or a stream-discovered identity. */
	type ChildNode = {
		run_id: string;
		state: string | null;
		source: 'row' | 'event';
		model_profile: string | null;
		server_id: string | null;
		parent_run_id: string | null;
		depth: number | null;
		grants: string[];
		cancel_requested_at: string | null;
	};

	const childNodes = $derived.by<ChildNode[]>(() => {
		const rows = displayRoot.children ?? [];
		const nodes: ChildNode[] = rows.map((row) => ({
			run_id: row.run_id,
			state: row.state,
			source: 'row' as const,
			model_profile: row.model_profile,
			server_id: row.server_id,
			parent_run_id: row.parent_run_id,
			depth: row.depth,
			grants: row.requested_grants,
			cancel_requested_at: row.cancel_requested_at
		}));
		const seen = new Set(rows.map((row) => row.run_id));
		for (const id of extraIds) {
			if (seen.has(id)) continue;
			const observed = eventChildren[id] ?? null;
			nodes.push({
				run_id: id,
				state: observed?.state ?? null,
				source: 'event',
				model_profile: null,
				server_id: displayRoot.server_id,
				parent_run_id: displayRoot.run_id,
				depth: observed?.depth ?? null,
				grants: [],
				cancel_requested_at: null
			});
		}
		return nodes;
	});

	const parentTerminal = $derived(
		displayRoot.state !== null && TERMINAL_RUN_STATES.has(displayRoot.state)
	);
	/** Quiet tree: nothing (root or any known child) can still change (C05 r2). */
	const treeQuiet = $derived(
		parentTerminal &&
			childNodes.every(
				(node) => node.state !== null && TERMINAL_RUN_STATES.has(node.state)
			)
	);
	const cancellable = $derived(displayRoot.state !== null && !parentTerminal);

	// --- event fold (defensive; a malformed payload is ignored, never thrown) --

	function asStringArray(value: unknown): string[] {
		if (!Array.isArray(value)) return [];
		return value.filter((item): item is string => typeof item === 'string');
	}

	function plannedIds(data: unknown): { ids: string[]; depth: number | null } {
		const record = recordOf(data);
		if (record === null) return { ids: [], depth: null };
		const depth = typeof record.depth === 'number' ? record.depth : null;
		return { ids: asStringArray(record.children), depth };
	}

	function resolvedStates(data: unknown): Array<{ run_id: string; state: string | null }> {
		if (!Array.isArray(data)) return [];
		const out: Array<{ run_id: string; state: string | null }> = [];
		for (const item of data) {
			const record = recordOf(item);
			if (record === null || typeof record.run_id !== 'string') continue;
			out.push({
				run_id: record.run_id,
				state: typeof record.state === 'string' ? record.state : null
			});
		}
		return out;
	}

	function foldEvent(event: MaiRunEvent): void {
		const data = event.data;
		switch (event.type) {
			case PLANNED_EVENT: {
				const { ids, depth } = plannedIds(data);
				const fresh: string[] = [];
				const known = new Set(extraIds);
				for (const id of ids) {
					if (known.has(id)) continue;
					known.add(id);
					fresh.push(id);
				}
				if (fresh.length > 0) extraIds = [...extraIds, ...fresh];
				if (depth !== null) {
					const next = { ...eventChildren };
					for (const id of ids) next[id] = { state: next[id]?.state ?? null, depth };
					eventChildren = next;
				}
				break;
			}
			case WAIT_EVENT: {
				const deadline = recordOf(data)?.deadline;
				if (typeof deadline === 'string') waitDeadline = deadline;
				break;
			}
			case RESOLVED_EVENT:
			case COMPLETED_EVENT: {
				const record = recordOf(data);
				const next = { ...eventChildren };
				for (const resolved of resolvedStates(record?.children)) {
					next[resolved.run_id] = {
						state: resolved.state,
						depth: next[resolved.run_id]?.depth ?? null
					};
				}
				eventChildren = next;
				break;
			}
			case FAILED_EVENT: {
				const reason = recordOf(data)?.reason;
				if (typeof reason === 'string') failureReason = reason;
				break;
			}
			case RESERVED_EVENT: {
				const ids = plannedIds(data).ids;
				let added = false;
				for (const id of ids) {
					if (outstanding.has(id)) continue;
					outstanding.add(id);
					added = true;
				}
				if (added || ids.length > 0) {
					ledger = {
						...ledger,
						seen: ledger.seen + 1,
						reservations: ledger.reservations + 1,
						activeSlots: outstanding.size
					};
				}
				break;
			}
			case RELEASED_EVENT: {
				const ids = plannedIds(data).ids;
				for (const id of ids) outstanding.delete(id);
				ledger = {
					...ledger,
					seen: ledger.seen + 1,
					releases: ledger.releases + 1,
					activeSlots: outstanding.size
				};
				break;
			}
			case CHARGED_EVENT: {
				const record = recordOf(data);
				const steps = typeof record?.steps === 'number' ? record.steps : 0;
				const tokens = typeof record?.tokens === 'number' ? record.tokens : 0;
				ledger = {
					...ledger,
					seen: ledger.seen + 1,
					consumedSteps: ledger.consumedSteps + steps,
					consumedTokens: ledger.consumedTokens + tokens
				};
				break;
			}
			default:
				// Other stream events (run.transition, agent.model_round, …) are
				// reflected through the debounced durable refresh, not folded here.
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
		if (apiBag === null || runId === '') return;
		try {
			const view = await apiBag.readConversation(runId);
			if (disposed || view.run_id !== runId) return;
			refreshedRoot = view;
			refreshProblem = null;
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

	// --- scoped stream controller (watermark dedup; C08 rule 7) ---------------

	function finishStream(): void {
		streamAbort?.abort();
		streamAbort = null;
		streamStatus = 'closed';
	}

	function resetFold(): void {
		refreshedRoot = null;
		extraIds = [];
		eventChildren = {};
		failureReason = null;
		waitDeadline = null;
		outstanding = new Set<string>();
		ledger = { seen: 0, reservations: 0, releases: 0, activeSlots: 0, consumedSteps: 0, consumedTokens: 0 };
		appliedEvents = 0;
		lastSeq = 0;
		streamProblem = null;
		refreshProblem = null;
		streamStatus = 'connecting';
	}

	async function streamLoop(id: string, controller: AbortController): Promise<void> {
		if (apiBag === null) return;
		while (!disposed && runId === id && !controller.signal.aborted) {
			if (treeQuiet) break;
			try {
				await apiBag.streamConversationEvents(id, {
					cursor: lastSeq > 0 ? String(lastSeq) : null,
					signal: controller.signal,
					onEvent: (frame) => {
						if (disposed || runId !== id) return;
						const event = readMaiEvent(frame);
						// Watermark dedup (C08 rule 7): replayed or re-delivered
						// frames at/below the last seen seq are dropped here, and a
						// foreign run's frame never touches this tree's fold.
						if (event === null || event.run_id !== id || event.seq <= lastSeq) return;
						lastSeq = event.seq;
						foldEvent(event);
						appliedEvents += 1;
						streamProblem = null;
						scheduleRefresh();
					}
				});
			} catch (error) {
				if (disposed || runId !== id || controller.signal.aborted) return;
				if (codeOf(error) === 'not_found') {
					streamProblem = 'No conversation is stored under this id; the stream stays closed.';
					finishStream();
					return;
				}
				streamProblem = 'The event stream was interrupted; reconnecting from the last event.';
			}
			if (disposed || runId !== id || controller.signal.aborted) return;
			streamStatus = 'reconnecting';
			await sleep(BACKOFF_MS, controller.signal);
		}
		if (runId === id && !controller.signal.aborted) finishStream();
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

	// --- cancel (explicit tree cancel; guard rule 3: requested ≠ verified) ----

	let cancelling = $state(false);
	let cancelStatus = $state<CancelView | null>(null);
	let cancelProblem = $state<{ code: string; message: string } | null>(null);

	async function cancelConversation(): Promise<void> {
		if (apiBag === null || runId === '' || cancelling || !cancellable) return;
		cancelling = true;
		cancelProblem = null;
		try {
			// cancel_children: true — a cancelled conversation never orphans its
			// delegated children (the reviewed ChatCancelBody default, explicit).
			const view = await apiBag.cancelConversation(runId, { cancel_children: true });
			if (disposed) return;
			cancelStatus = view;
			await loadConversation();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			cancelProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The cancellation could not be recorded.')
			};
		} finally {
			if (!disposed) cancelling = false;
		}
	}

	// --- mount cycle (the $effect teardown is the release path) ---------------

	$effect(() => {
		if (!usable) return;
		const id = runId;
		disposed = false;
		cancelStatus = null;
		cancelProblem = null;
		// The controllers run UNTRACKED: their synchronous prologue reads tree
		// state (apiBag, treeQuiet), which must never become an effect
		// dependency — a fold update would otherwise re-run this effect,
		// abort the live stream and wipe the fold it was delivering into.
		const controller = new AbortController();
		untrack(() => {
			resetFold();
			streamAbort = controller;
			void loadConversation();
			void streamLoop(id, controller);
		});
		return () => {
			disposed = true;
			if (refreshTimer !== null) {
				clearTimeout(refreshTimer);
				refreshTimer = null;
			}
			controller.abort();
			streamAbort = null;
		};
	});

	onDestroy(() => {
		disposed = true;
		if (refreshTimer !== null) {
			clearTimeout(refreshTimer);
			refreshTimer = null;
		}
		streamAbort?.abort();
		streamAbort = null;
	});

	function taskLabelOf(id: string): string | null {
		const label = taskLabels?.[id];
		return typeof label === 'string' && label !== '' ? label : null;
	}

	function stateKind(state: string | null): string {
		if (state === null) return 'unknown';
		if (TERMINAL_RUN_STATES.has(state)) return state;
		if (state === 'reconciling' || state === 'waiting_approval') return 'warning';
		return 'active';
	}
</script>

<div class="mai-delg" data-testid="delegation-root">
	{#if !usable}
		<div class="mai-delg__state mai-delg__state--error" role="alert" data-testid="tree-config-error">
			<p>This tree view was mounted without its delegation service or a conversation identity.</p>
		</div>
	{:else}
		<header class="mai-delg__header">
			<h3 class="mai-delg__title" data-testid="tree-title">Delegation tree</h3>
			<p class="mai-delg__meta">
				<span data-testid="tree-cursor">Events applied: {appliedEvents} (cursor #{lastSeq})</span>
				<span data-testid="stream-status">
					{#if streamStatus === 'connecting'}
						Event stream connecting…
					{:else if streamStatus === 'reconnecting'}
						Event stream reconnecting from the last recorded event…
					{:else if streamStatus === 'closed'}
						Event stream closed.
					{/if}
				</span>
			</p>
		</header>

		{#if streamProblem !== null}
			<p class="mai-delg__note mai-delg__note--error" role="status" data-testid="stream-problem">
				{streamProblem}
			</p>
		{/if}
		{#if refreshProblem !== null}
			<div
				class="mai-delg__banner mai-delg__banner--error"
				role="alert"
				data-testid="refresh-error"
				data-error-code={refreshProblem.code}
			>
				<p>{refreshProblem.message}</p>
				<Button variant="secondary" onclick={loadConversation} data-testid="refresh-retry">
					Retry
				</Button>
			</div>
		{/if}

		<ul class="mai-delg__tree" aria-label="Delegation tree">
			<li class="mai-delg__node mai-delg__node--root" data-testid="root-node">
				<p class="mai-delg__line">
					<span class="mai-delg__badge" data-state={stateKind(displayRoot.state)} data-testid="root-state">
						{runStateText(displayRoot.state)}
					</span>
					{#if taskLabelOf(displayRoot.run_id) !== null}
						<span class="mai-delg__task" data-testid="root-task">{taskLabelOf(displayRoot.run_id)}</span>
					{/if}
					<span class="mai-delg__meta" data-testid="root-model">Model {displayRoot.model_profile}</span>
					<span class="mai-delg__meta" data-testid="root-target">Target {shortId(displayRoot.server_id)}</span>
					<span class="mai-delg__meta" data-testid="root-id">Run {shortId(displayRoot.run_id)}</span>
					<span class="mai-delg__meta" data-testid="root-depth">depth {displayRoot.depth}</span>
				</p>
				<p class="mai-delg__line" data-testid="root-grants">
					Grants:
					{#if displayRoot.requested_grants.length === 0}
						<span class="mai-delg__grant" data-grants="none">none</span>
					{:else}
						{#each displayRoot.requested_grants as grant (grant)}
							<span class="mai-delg__grant" data-testid="root-grant">{grant}</span>
						{/each}
					{/if}
				</p>
				{#if displayRoot.cancel_requested_at !== null}
					<p class="mai-delg__note" data-testid="root-cancel-flag">
						Cancellation requested {displayRoot.cancel_requested_at}
					</p>
				{/if}
				{#if waitDeadline !== null && displayRoot.state === 'waiting_children'}
					<p class="mai-delg__note" data-testid="root-wait-deadline">
						Waiting for children until {waitDeadline}
					</p>
				{/if}
				{#if displayRoot.state === 'failed' && failureReason !== null}
					<p class="mai-delg__note mai-delg__note--error" data-testid="root-failure-reason">
						Recorded failure reason: {failureReason}
					</p>
				{/if}
			</li>
			{#if childNodes.length === 0}
				<li class="mai-delg__node" data-testid="children-none">
					<p class="mai-delg__muted">No child runs recorded for this conversation.</p>
				</li>
			{:else}
			{#each childNodes as child (child.run_id)}
				<li
					class="mai-delg__node mai-delg__node--child"
					data-testid="child-row"
					data-run-id={child.run_id}
					data-source={child.source}
				>
					<p class="mai-delg__line">
						<span class="mai-delg__badge" data-state={stateKind(child.state)} data-testid="child-state">
							{runStateText(child.state)}
						</span>
						{#if child.cancel_requested_at !== null}
							<span class="mai-delg__note mai-delg__note--inline" data-testid="child-cancel-flag">
								cancellation requested — not yet verified
							</span>
						{/if}
						{#if taskLabelOf(child.run_id) !== null}
							<span class="mai-delg__task" data-testid="child-task">{taskLabelOf(child.run_id)}</span>
						{/if}
						{#if child.model_profile !== null}
							<span class="mai-delg__meta" data-testid="child-model">Model {child.model_profile}</span>
						{/if}
						<span class="mai-delg__meta" data-testid="child-target">Target {shortId(child.server_id)}</span>
						<span class="mai-delg__meta" data-testid="child-parent">
							child of {shortId(child.parent_run_id)} · depth {child.depth ?? '?'}
						</span>
						<span class="mai-delg__meta" data-testid="child-id">Run {shortId(child.run_id)}</span>
					</p>
					<p class="mai-delg__line" data-testid="child-grants" data-grants={child.grants.length === 0 ? 'none' : 'some'}>
						Grants:
						{#if child.grants.length === 0}
							<span class="mai-delg__grant mai-delg__grant--denied" data-grants="none" data-testid="child-grants-none">
								no effective grants (clamped at the delegation boundary)
							</span>
						{:else}
							{#each child.grants as grant (grant)}
								<span class="mai-delg__grant" data-testid="child-grant">{grant}</span>
							{/each}
						{/if}
					</p>
				</li>
			{/each}
			{/if}
		</ul>

		<section class="mai-delg__section" aria-label="Budget consumption" data-testid="budget-section">
			<h4 class="mai-delg__heading">Budget consumption</h4>
			{#if ledger.seen === 0}
				<p class="mai-delg__muted" data-testid="budget-none">
					No budget ledger events recorded for this tree yet — an unbudgeted root writes none.
				</p>
			{:else}
				<p class="mai-delg__line" data-testid="budget-summary">
					<span data-testid="budget-active">Active reservations: {ledger.activeSlots}</span>
					<span data-testid="budget-steps">Steps charged: {ledger.consumedSteps}</span>
					<span data-testid="budget-tokens">Tokens charged: {ledger.consumedTokens}</span>
					<span data-testid="budget-ledger">Reservations: {ledger.reservations} · Releases: {ledger.releases}</span>
				</p>
				<p class="mai-delg__note" data-testid="budget-note">
					Charged spend is never un-spent; a release refunds only outstanding reservations.
				</p>
			{/if}
		</section>

		{#if showCancel}
			<section
				class="mai-delg__section"
				aria-label="Cancel conversation"
				data-testid="cancel-section"
			>
				<h4 class="mai-delg__heading">Stop this conversation</h4>
				{#if cancellable}
					<div class="mai-delg__actions">
						<Button
							variant="danger"
							loading={cancelling}
							onclick={cancelConversation}
							data-testid="cancel-conversation"
						>
							Cancel conversation and children
						</Button>
					</div>
				{:else}
					<p class="mai-delg__muted" data-testid="cancel-unavailable">
						{#if displayRoot.state === null}
							The run state is not recorded yet; there is nothing to cancel here.
						{:else}
							This conversation already reached a terminal state; nothing left to cancel.
						{/if}
					</p>
				{/if}
				{#if cancelStatus !== null}
					<p
						class="mai-delg__note"
						role="status"
						data-testid="cancel-status"
						data-outcome={cancelStatus.outcome ?? 'pending'}
					>
						{cancelStatus.outcome === null
							? 'Cancellation requested — not yet verified.'
							: `Cancelled (verified).`}
					</p>
				{/if}
				{#if cancelProblem !== null}
					<div
						class="mai-delg__banner mai-delg__banner--error"
						role="alert"
						data-testid="cancel-error"
						data-error-code={cancelProblem.code}
					>
						<p>{cancelProblem.message}</p>
					</div>
				{/if}
			</section>
		{/if}
	{/if}
</div>

<style>
	.mai-delg {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		color: var(--mai-color-text);
		font-size: var(--mai-text-sm);
	}

	.mai-delg__header {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
	}

	.mai-delg__title {
		margin: 0;
		font-size: var(--mai-text-lg);
		font-weight: 600;
	}

	.mai-delg__meta {
		color: var(--mai-color-text-muted);
	}

	.mai-delg__tree {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
	}

	.mai-delg__node {
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface);
		padding: var(--mai-space-2) var(--mai-space-3);
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
	}

	.mai-delg__node--child {
		margin-left: var(--mai-space-6);
	}

	.mai-delg__line {
		margin: 0;
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: var(--mai-space-2);
	}

	.mai-delg__task {
		font-weight: 600;
	}

	.mai-delg__badge {
		display: inline-block;
		padding: 0 var(--mai-space-2);
		border-radius: var(--mai-radius-sm);
		border: 1px solid var(--mai-status-neutral-border);
		background: var(--mai-status-neutral-bg);
		color: var(--mai-status-neutral-text);
	}

	.mai-delg__badge[data-state='succeeded'] {
		background: var(--mai-status-success-bg);
		border-color: var(--mai-status-success-border);
		color: var(--mai-status-success-text);
	}

	.mai-delg__badge[data-state='failed'] {
		background: var(--mai-status-danger-bg);
		border-color: var(--mai-status-danger-border);
		color: var(--mai-status-danger-text);
	}

	.mai-delg__badge[data-state='warning'] {
		background: var(--mai-status-warning-bg);
		border-color: var(--mai-status-warning-border);
		color: var(--mai-status-warning-text);
	}

	.mai-delg__badge[data-state='active'] {
		background: var(--mai-status-info-bg);
		border-color: var(--mai-status-info-border);
		color: var(--mai-status-info-text);
	}

	.mai-delg__grant {
		display: inline-block;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		padding: 0 var(--mai-space-1);
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
	}

	.mai-delg__grant--denied {
		border-color: var(--mai-status-warning-border);
		background: var(--mai-status-warning-bg);
		color: var(--mai-status-warning-text);
	}

	.mai-delg__note {
		margin: 0;
		color: var(--mai-color-text-muted);
	}

	.mai-delg__note--inline {
		font-size: var(--mai-text-xs);
	}

	.mai-delg__note--error {
		color: var(--mai-status-danger-text);
	}

	.mai-delg__section {
		border-top: 1px solid var(--mai-color-border);
		padding-top: var(--mai-space-2);
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
	}

	.mai-delg__heading {
		margin: 0;
		font-size: var(--mai-text-base);
		font-weight: 600;
	}

	.mai-delg__muted {
		margin: 0;
		color: var(--mai-color-text-muted);
	}

	.mai-delg__actions {
		display: flex;
		gap: var(--mai-space-2);
	}

	.mai-delg__banner {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--mai-space-2);
		padding: var(--mai-space-2) var(--mai-space-3);
		border-radius: var(--mai-radius-md);
		border: 1px solid var(--mai-status-danger-border);
		background: var(--mai-status-danger-bg);
	}

	.mai-delg__banner p {
		margin: 0;
		color: var(--mai-status-danger-text);
	}

	.mai-delg__state {
		padding: var(--mai-space-3);
		border-radius: var(--mai-radius-md);
		border: 1px solid var(--mai-color-border);
	}

	.mai-delg__state--error {
		border-color: var(--mai-status-danger-border);
		background: var(--mai-status-danger-bg);
	}

	.mai-delg__state p {
		margin: 0;
	}
</style>
