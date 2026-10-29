<script module lang="ts">
	/**
	 * JobsPanel — reusable runs/schedules management panel with the persisted
	 * event stream (card `mai-05-web-jobs`, P05; service row `mai-web`,
	 * contracts C05/C08/C11).
	 *
	 * Layer: feature panel (windowed-workspace ADR-0001 §2 layer table). It owns
	 * its one domain view and nothing else:
	 *
	 *  - Explicit context only: a SERVER management view. Every read names the
	 *    target pinned at open time (`panel.scope.server_id`, C01 rule 2) —
	 *    `list_runs` requires the explicit scope and the schedule create body
	 *    carries it. The module reads no launcher/current-server value
	 *    anywhere, so there is no hidden global mutation target and a launcher
	 *    re-selection can never retarget a watch, a cancel or a schedule
	 *    submission (C08 rule 5, NT5).
	 *  - Explicit services only: the eight-function `JobsApi` enters through
	 *    `panel.services.api` (the typed bag owned by the feature entry
	 *    module). A missing or incomplete bag fails closed with a visible
	 *    state — no service locator, no fallback client.
	 *  - Durable truth, replayed (C05 rule 4): the visible run state always
	 *    comes from `readRun`/`listRuns` — replayed facade state, never an
	 *    engine call, never a client-side guess. The SSE stream is the change
	 *    SIGNAL: each delivered event schedules a debounced durable refresh.
	 *  - Event replay without duplicates (C08 rule 7): the subscription starts
	 *    from the run's beginning (or resumes from the last seen `seq` carried
	 *    as the SSE `Last-Event-ID`), survives the server's bounded stream
	 *    close and API/network restarts with a small fixed backoff, and
	 *    deduplicates by the per-run monotonic `seq` watermark — a frame at or
	 *    below the watermark is never rendered twice. The stream is read-only:
	 *    it never submits, cancels or retargets anything (windowed ADR §7).
	 *  - Honest outcomes (engine guard rule 3, C08 rule 6): a cancel renders
	 *    exactly what the facade answered — `outcome: null` is "requested —
	 *    not yet verified", never "cancelled"; unknown/pending states render
	 *    as recorded. There is no optimistic success anywhere.
	 *  - Schedules management (C05 rule 8): create (definition + explicit
	 *    target), pause/resume (the ONLY update the API allows) and delete.
	 *    The definition is frozen server-side; no edit form exists here.
	 *  - No window mechanics: no drag/geometry/focus/persistence code and no
	 *    workspace-state imports; the window host supplies all of that. The
	 *    stream/refresh controllers are instance-scoped, released on hide and
	 *    unmount (the `$effect` teardown is the release path), and never
	 *    survive into another mount cycle. Closing, hiding or focusing the
	 *    window starts and cancels nothing.
	 *  - No module-level state and no `window`/`document`/storage access
	 *    (ADR §5 rule 4): safe to evaluate per SSR request; instances share
	 *    nothing between requests or windows.
	 *
	 * Tokens come only from ui/tokens.css; no dependency is added (ADR §4.3).
	 */
</script>

<script lang="ts">
	import { onDestroy } from 'svelte';
	import { CalendarClock, ListTree, RadioTower, ScrollText, ServerCog } from 'lucide-svelte';
	import Button from '../ui/Button.svelte';
	import Field from '../ui/Field.svelte';
	import { readMaiEvent, type MaiRunEvent } from '../api/client.js';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import type {
		CancelView,
		JobsApi,
		RunDetail,
		RunView,
		ScheduleView
	} from '../workspace/entries/05-web-jobs.js';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readApi(services: PanelServices): JobsApi | null {
		const candidate = services.api as Partial<JobsApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (
			const name of [
				'listRuns',
				'readRun',
				'cancelRun',
				'listSchedules',
				'createSchedule',
				'updateSchedule',
				'deleteSchedule',
				'streamRunEvents'
			] as const
		) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as JobsApi;
	}

	const api = $derived(readApi(panel.services));
	/** Jobs management: a global mount contradicts the entry contract. */
	const serverScope = $derived(panel.scope.kind === 'server' ? panel.scope.server_id : null);
	const usable = $derived(api !== null && serverScope !== null);

	// --- producer bounds mirrored for display only (server re-validates) -----

	/** Bounded page size (C01 rule 5); the producer clamps the maximum. */
	const PAGE_LIMIT = 10;
	/** Fixed reconnect delay after a stream close/failure (bounded, visible). */
	const BACKOFF_MS = 750;
	/** Debounce for the durable refresh triggered by delivered events. */
	const REFRESH_DEBOUNCE_MS = 150;
	/** Rendered-event cap: a long replay cannot grow the DOM without bound. */
	const MAX_RENDERED_EVENTS = 500;

	/** Run lifecycle states after which nothing new can be learned (C05 rule 2). */
	const TERMINAL_RUN_STATES: ReadonlySet<string> = new Set(['succeeded', 'failed', 'cancelled']);

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

	const STEP_STATE_TEXT: Readonly<Record<string, string>> = Object.freeze({
		pending: 'Pending',
		running: 'Running',
		succeeded: 'Succeeded',
		failed: 'Failed',
		outcome_unknown: 'Outcome unknown — reconciliation required',
		cancel_requested: 'Cancellation requested — not yet verified',
		cancelled: 'Cancelled (host-verified)'
	});

	const EVENT_TYPE_TEXT: Readonly<Record<string, string>> = Object.freeze({
		'run.submitted': 'Run submitted',
		'run.transition': 'Run state changed',
		'run.cancel_requested': 'Cancellation requested',
		'run.recovery_parked': 'Run parked for recovery',
		'step.started': 'Step started',
		'step.completed': 'Step completed',
		'step.awaiting_approval': 'Step waiting for approval decision',
		'step.reconciling': 'Step reconciling an unproven outcome',
		// Agent-run event vocabulary: deltas are SKIPPED from the rendered
		// collection entirely (no rows, no renaming); thinking is never shown.
		'agent.model_round': 'Model round',
		'agent.tool_result': 'Tool result',
		'agent.failed': 'Run failed'
	});

	function isTerminal(state: string | null): boolean {
		return TERMINAL_RUN_STATES.has(state ?? '');
	}

	function runStateText(state: string | null): string {
		if (state === null) return 'State not recorded yet';
		return RUN_STATE_TEXT[state] ?? `Recorded state: ${state}`;
	}

	function stepStateText(state: string): string {
		return STEP_STATE_TEXT[state] ?? `Recorded state: ${state}`;
	}

	function eventTypeText(type: string): string {
		return EVENT_TYPE_TEXT[type] ?? `Recorded event: ${type}`;
	}

	function codeOf(error: unknown): string | null {
		if (typeof error !== 'object' || error === null) return null;
		const code = (error as { code?: unknown }).code;
		return typeof code === 'string' ? code : null;
	}

	function messageOf(error: unknown): string | null {
		return error instanceof Error && error.message ? error.message : null;
	}

	/** Stable, display-safe text for the frozen C01 codes (no raw payloads). */
	function friendlyText(code: string, fallback: string): string {
		if (code === 'unauthenticated') return 'Your session has ended — sign in again.';
		if (code === 'throttled') return 'Too many requests — try again shortly.';
		if (code === 'request_rejected') return 'The request was rejected; nothing was changed.';
		return fallback;
	}

	function shortId(id: string): string {
		return id === '' ? '—' : id.slice(0, 8);
	}

	/** Full, useful timestamp text — timestamps are shown verbatim (ISO). */
	function stampText(iso: string | null): string {
		return iso === null || iso === '' ? '—' : iso;
	}

	/**
	 * Honest completion text for one run row: there is NO completion_at wire
	 * field — `updated_at` is labeled as a completion ONLY when the row's
	 * state is terminal, never presented as a separate claim.
	 */
	function completionText(state: string | null, updatedAt: string): string | null {
		if (state === 'succeeded') return `Completed ${stampText(updatedAt)}`;
		if (state === 'failed' || state === 'cancelled') return `Ended ${stampText(updatedAt)}`;
		return null;
	}

	// --- W3 watch report (durable final answer, tool results, failure) -------

	/** Per-item display cap: one huge result cannot flood the report. */
	const MAX_ITEM_CHARS = 2_000;
	/** The final answer's display cap (the durable text stays in storage). */
	const MAX_FINAL_CHARS = 20_000;

	function bounded(text: string, cap: number): string {
		return text.length <= cap
			? text
			: `${text.slice(0, cap)}… [+${text.length - cap} characters truncated]`;
	}

	function eventData(event: MaiRunEvent): Record<string, unknown> {
		const data = event.data;
		return typeof data === 'object' && data !== null ? (data as Record<string, unknown>) : {};
	}

	/**
	 * The current answer: the LATEST `agent.model_round` event carrying
	 * `status: "final"` and a string `data.output`. Deltas are never
	 * concatenated into it and thinking is never read.
	 */
	const finalReport = $derived.by<{ text: string; at: string; truncated: boolean } | null>(
		() => {
			let chosen: MaiRunEvent | null = null;
			for (const event of events) {
				if (event.type !== 'agent.model_round') continue;
				const data = eventData(event);
				if (data.status !== 'final' || typeof data.output !== 'string') continue;
				chosen = event;
			}
			if (chosen === null) return null;
			const output = eventData(chosen).output as string;
			return {
				text: bounded(output, MAX_FINAL_CHARS),
				at: chosen.ts,
				truncated: output.length > MAX_FINAL_CHARS
			};
		}
	);

	/** Bounded tool-result projection: capability + useful text/JSON each. */
	const toolResults = $derived.by<Array<{ seq: number; capability: string; text: string }>>(
		() => {
			const rows: Array<{ seq: number; capability: string; text: string }> = [];
			for (const event of events) {
				if (event.type !== 'agent.tool_result') continue;
				const data = eventData(event);
				const capability =
					typeof data.capability_id === 'string' && data.capability_id !== ''
						? data.capability_id
						: 'unknown capability';
				let text: string;
				if (typeof data.result === 'string') text = data.result;
				else if (data.result !== undefined) {
					try {
						text = JSON.stringify(data.result) ?? 'unrenderable result value';
					} catch {
						text = 'unrenderable result value';
					}
				} else {
					text = '(no result payload recorded)';
				}
				rows.push({ seq: event.seq, capability, text: bounded(text, MAX_ITEM_CHARS) });
			}
			return rows;
		}
	);

	/** The useful failure reason of the latest `agent.failed` event. */
	const failedReason = $derived.by<string | null>(() => {
		let reason: string | null = null;
		for (const event of events) {
			if (event.type !== 'agent.failed') continue;
			const data = eventData(event);
			const candidate = data.reason ?? data.error;
			if (typeof candidate === 'string' && candidate.trim() !== '') reason = candidate;
		}
		return reason;
	});

	/** Abortable sleep: the reconnect backoff releases early on teardown. */
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

	// --- shared async-list state shape (C08 rule 6: never all-green) ----------

	type ListState =
		| { status: 'loading' }
		| { status: 'error'; code: string; message: string }
		| { status: 'ready' };

	/** Set once the mount cycle ended; async continuations check before writes. */
	let disposed = true;

	// --- runs list (explicit server scope; C01 rule 7) ------------------------

	let runs = $state<RunView[]>([]);
	let runsTotal = $state(0);
	let runsLoadingMore = $state(false);
	let runsState = $state<ListState>({ status: 'loading' });
	const runsHasMore = $derived(runsState.status === 'ready' && runs.length < runsTotal);

	// --- W1 run filters (applied BEFORE pagination by the reviewed API) -------

	/** Draft inputs: editing them alone never refetches. */
	let qInput = $state('');
	let dateFromInput = $state('');
	let dateToInput = $state('');
	/** The applied filter set the list currently renders. */
	let runsFilter = $state<{ q?: string; date_from?: string; date_to?: string }>({});

	/**
	 * The Today preset translates BROWSER-LOCAL today into explicit ISO
	 * instant bounds: local midnight inclusive (date_from) and the next
	 * local midnight exclusive-exclusive upper edge (date_to). Ambiguous
	 * local date text is never sent; the instant pair is timezone-safe.
	 */
	function localTodayBounds(): { date_from: string; date_to: string } {
		const now = new Date();
		const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
		const end = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
		return { date_from: start.toISOString(), date_to: end.toISOString() };
	}

	/** Applies the draft filters: offset resets to 0 and rows are replaced. */
	function applyRunFilters(): void {
		const q = qInput.trim();
		const dateFrom = dateFromInput.trim();
		const dateTo = dateToInput.trim();
		runsFilter = {
			...(q !== '' ? { q } : {}),
			...(dateFrom !== '' ? { date_from: dateFrom } : {}),
			...(dateTo !== '' ? { date_to: dateTo } : {})
		};
		void loadRuns();
	}

	/** The Today preset: explicit local-midnight bounds, then load page 0. */
	function applyTodayFilter(): void {
		const bounds = localTodayBounds();
		dateFromInput = bounds.date_from;
		dateToInput = bounds.date_to;
		runsFilter = { ...bounds };
		void loadRuns();
	}

	/** Clears every filter back to the unfiltered first page. */
	function clearRunFilters(): void {
		qInput = '';
		dateFromInput = '';
		dateToInput = '';
		runsFilter = {};
		void loadRuns();
	}

	async function loadRuns(): Promise<void> {
		if (api === null || serverScope === null) return;
		runsState = { status: 'loading' };
		runsLoadingMore = false;
		try {
			const page = await api.listRuns(serverScope, { limit: PAGE_LIMIT, ...runsFilter });
			if (disposed) return;
			runs = page.runs;
			runsTotal = page.total;
			runsState = { status: 'ready' };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			runsState = {
				status: 'error',
				code,
				message: friendlyText(code, messageOf(error) ?? 'The run list could not be loaded.')
			};
		}
	}

	async function loadMoreRuns(): Promise<void> {
		if (api === null || serverScope === null || runsLoadingMore || !runsHasMore) return;
		runsLoadingMore = true;
		try {
			const page = await api.listRuns(serverScope, {
				limit: PAGE_LIMIT,
				offset: runs.length,
				...runsFilter
			});
			if (disposed) return;
			const seen = new Set(runs.map((r) => r.run_id));
			const appended = page.runs.filter((r) => !seen.has(r.run_id));
			runs = [...runs, ...appended];
			runsTotal = page.total;
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			runsState = {
				status: 'error',
				code,
				message: friendlyText(code, messageOf(error) ?? 'More runs could not be loaded.')
			};
		} finally {
			if (!disposed) runsLoadingMore = false;
		}
	}

	// --- pinned server name (W3: one narrow lookup, nonfatal) -----------------

	let serverName = $state<string | null>(null);
	let serverNameFailed = $state(false);

	$effect(() => {
		if (api === null || serverScope === null || api.readServerName === undefined) return;
		const scope = serverScope;
		api.readServerName(scope).then(
			(name) => {
				if (disposed || serverScope !== scope) return;
				serverName = name;
				serverNameFailed = false;
			},
			() => {
				if (disposed || serverScope !== scope) return;
				// Nonfatal: the UUID stays; jobs data keeps working.
				serverName = null;
				serverNameFailed = true;
			}
		);
	});

	// --- watched run: durable detail + persisted event stream -----------------

	let watchedRunId = $state('');
	let watchedRun = $state<RunDetail | null>(null);
	let watchedState = $state<ListState>({ status: 'loading' });
	/** Non-fatal durable-refresh failure; the next event re-arms the refresh. */
	let refreshProblem = $state<string | null>(null);
	let cancelStatus = $state<CancelView | null>(null);
	let cancelError = $state<{ code: string; message: string } | null>(null);
	let cancelBusy = $state(false);
	/** Delivered stream events, deduplicated by the monotonic seq watermark. */
	let events = $state<MaiRunEvent[]>([]);
	let streamStatus = $state<'off' | 'connecting' | 'live' | 'reconnecting' | 'closed'>('off');
	let streamProblem = $state<string | null>(null);

	/** Non-reactive controller locals: the async loop and frame callbacks read
	 * these directly; the reactive mirrors above drive the view only. */
	let streamAbort: AbortController | null = null;
	let lastSeq = 0;
	let refreshTimer: ReturnType<typeof setTimeout> | null = null;

	const watchedTerminal = $derived(isTerminal(watchedRun?.state ?? null));

	function finishStream(): void {
		streamAbort?.abort();
		streamAbort = null;
		streamStatus = 'closed';
	}

	function noteRowState(detail: RunDetail): void {
		let changed = false;
		const next = runs.map((row) => {
			if (row.run_id !== detail.run_id) return row;
			if (
				row.state === detail.state &&
				row.cancel_requested_at === detail.cancel_requested_at
			) {
				return row;
			}
			changed = true;
			return { ...row, state: detail.state, cancel_requested_at: detail.cancel_requested_at };
		});
		if (changed) runs = next;
	}

	/** One replayed durable read; keeps the displayed state server-recorded. */
	async function loadWatched(): Promise<void> {
		if (api === null || watchedRunId === '') return;
		try {
			const detail = await api.readRun(watchedRunId);
			if (disposed || watchedRunId !== detail.run_id) return;
			watchedRun = detail;
			watchedState = { status: 'ready' };
			noteRowState(detail);
			if (isTerminal(detail.state)) finishStream();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			watchedState = {
				status: 'error',
				code,
				message: friendlyText(code, messageOf(error) ?? 'The run could not be read.')
			};
		}
	}

	/** Debounced durable refresh; events are the signal, `readRun` the truth. */
	function scheduleWatchedRefresh(): void {
		if (disposed || refreshTimer !== null || watchedRunId === '') return;
		refreshTimer = setTimeout(() => {
			refreshTimer = null;
			void refreshWatched();
		}, REFRESH_DEBOUNCE_MS);
	}

	async function refreshWatched(): Promise<void> {
		if (api === null || watchedRunId === '') return;
		try {
			const detail = await api.readRun(watchedRunId);
			if (disposed || watchedRunId !== detail.run_id) return;
			watchedRun = detail;
			watchedState = { status: 'ready' };
			refreshProblem = null;
			noteRowState(detail);
			if (isTerminal(detail.state)) finishStream();
		} catch (error) {
			if (disposed) return;
			refreshProblem =
				messageOf(error) ?? 'The durable state could not be refreshed; retrying on the next event.';
		}
	}

	/**
	 * The scoped stream controller. Each session is one SSE subscription from
	 * the watermark cursor; the server's bounded close and any network/API
	 * failure end the session, and the loop re-subscribes from the last seen
	 * `seq` (carried as `Last-Event-ID`) after the fixed backoff — replay is
	 * deduplicated by the watermark, so no event renders twice. Terminal
	 * runs stop the loop: nothing new can arrive (C05 rule 2).
	 */
	async function streamLoop(runId: string, controller: AbortController): Promise<void> {
		if (api === null) return;
		while (!disposed && watchedRunId === runId && !controller.signal.aborted) {
			if (watchedTerminal) break;
			try {
				await api.streamRunEvents(runId, {
					cursor: lastSeq > 0 ? String(lastSeq) : null,
					signal: controller.signal,
					onEvent: (frame) => {
						if (disposed || watchedRunId !== runId) return;
						const event = readMaiEvent(frame);
						// Watermark dedup (C08 rule 7): replayed or re-delivered
						// frames at/below the last seen seq are dropped here.
						if (event === null || event.run_id !== runId || event.seq <= lastSeq) return;
						lastSeq = event.seq;
						// Transient deltas never enter the rendered collection at
						// all — no rows, no relabeling, no content — but they DO
						// advance the watermark and arm the durable refresh.
						if (event.type !== 'agent.output_delta') {
							events = [...events, event].slice(-MAX_RENDERED_EVENTS);
						}
						streamStatus = 'live';
						streamProblem = null;
						scheduleWatchedRefresh();
					}
				});
			} catch (error) {
				if (disposed || watchedRunId !== runId || controller.signal.aborted) return;
				if (codeOf(error) === 'not_found') {
					streamProblem = 'No run is stored under this id; the stream stays closed.';
					finishStream();
					return;
				}
				// A fixed sentence: the actionable content is the reconnect itself;
				// raw network messages add nothing the status line does not say.
				streamProblem = 'The event stream was interrupted; reconnecting from the last event.';
			}
			if (disposed || watchedRunId !== runId || controller.signal.aborted) return;
			streamStatus = 'reconnecting';
			await sleep(BACKOFF_MS, controller.signal);
		}
		if (watchedRunId === runId && !controller.signal.aborted) finishStream();
	}

	/** Watch one run: durable detail now, full event replay, then the tail. */
	function selectRun(runId: string): void {
		if (api === null || watchedRunId === runId) return;
		finishStream();
		if (refreshTimer !== null) {
			clearTimeout(refreshTimer);
			refreshTimer = null;
		}
		watchedRunId = runId;
		lastSeq = 0;
		events = [];
		watchedRun = null;
		watchedState = { status: 'loading' };
		refreshProblem = null;
		cancelStatus = null;
		cancelError = null;
		streamProblem = null;
		streamStatus = 'connecting';
		const controller = new AbortController();
		streamAbort = controller;
		void loadWatched();
		void streamLoop(runId, controller);
	}

	// --- cancel (requested ≠ verified; engine guard rule 3) -------------------

	async function cancelWatched(): Promise<void> {
		if (api === null || watchedRunId === '' || cancelBusy || watchedTerminal) return;
		cancelBusy = true;
		cancelError = null;
		try {
			const view = await api.cancelRun(watchedRunId, { cancel_children: false });
			if (disposed) return;
			cancelStatus = view;
			await refreshWatched();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			cancelError = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The cancellation could not be recorded.')
			};
		} finally {
			if (!disposed) cancelBusy = false;
		}
	}

	// --- schedules (explicit server scope; pause/resume is the only update) ---

	let schedules = $state<ScheduleView[]>([]);
	let schedulesTotal = $state(0);
	let schedulesLoadingMore = $state(false);
	let schedulesState = $state<ListState>({ status: 'loading' });
	let schedulesActionError = $state<{ code: string; message: string } | null>(null);
	let busyScheduleId = $state('');
	const schedulesHasMore = $derived(
		schedulesState.status === 'ready' && schedules.length < schedulesTotal
	);

	async function loadSchedules(): Promise<void> {
		if (api === null || serverScope === null) return;
		schedulesState = { status: 'loading' };
		schedulesLoadingMore = false;
		try {
			const page = await api.listSchedules(serverScope, { limit: PAGE_LIMIT });
			if (disposed) return;
			schedules = page.schedules;
			schedulesTotal = page.total;
			schedulesState = { status: 'ready' };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			schedulesState = {
				status: 'error',
				code,
				message: friendlyText(code, messageOf(error) ?? 'The schedule list could not be loaded.')
			};
		}
	}

	async function loadMoreSchedules(): Promise<void> {
		if (api === null || serverScope === null || schedulesLoadingMore || !schedulesHasMore) return;
		schedulesLoadingMore = true;
		try {
			const page = await api.listSchedules(serverScope, {
				limit: PAGE_LIMIT,
				offset: schedules.length
			});
			if (disposed) return;
			const seen = new Set(schedules.map((s) => s.schedule_id));
			const appended = page.schedules.filter((s) => !seen.has(s.schedule_id));
			schedules = [...schedules, ...appended];
			schedulesTotal = page.schedules.length === 0 ? schedules.length : page.total;
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			schedulesActionError = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'More schedules could not be loaded.')
			};
		} finally {
			if (!disposed) schedulesLoadingMore = false;
		}
	}

	async function toggleSchedule(row: ScheduleView): Promise<void> {
		if (api === null || busyScheduleId !== '') return;
		busyScheduleId = row.schedule_id;
		schedulesActionError = null;
		try {
			const updated = await api.updateSchedule(row.schedule_id, { enabled: !row.enabled });
			if (disposed) return;
			schedules = schedules.map((candidate) =>
				candidate.schedule_id === updated.schedule_id ? updated : candidate
			);
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			schedulesActionError = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The schedule could not be updated.')
			};
		} finally {
			if (!disposed) busyScheduleId = '';
		}
	}

	async function deleteScheduleRow(row: ScheduleView): Promise<void> {
		if (api === null || busyScheduleId !== '') return;
		busyScheduleId = row.schedule_id;
		schedulesActionError = null;
		try {
			await api.deleteSchedule(row.schedule_id);
			if (disposed) return;
			schedules = schedules.filter((candidate) => candidate.schedule_id !== row.schedule_id);
			schedulesTotal = Math.max(0, schedulesTotal - 1);
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			schedulesActionError = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The schedule could not be deleted.')
			};
		} finally {
			if (!disposed) busyScheduleId = '';
		}
	}

	// --- schedule creation (the explicit-server submission door) --------------

	let cronInput = $state('');
	let timezoneInput = $state('');
	let handlerInput = $state('');
	let payloadInput = $state('');
	let formError = $state('');
	let createBusy = $state(false);

	async function createSchedule(): Promise<void> {
		if (api === null || serverScope === null || createBusy) return;
		formError = '';
		const cron = cronInput.trim();
		if (cron === '') {
			formError = 'Enter the cron expression.';
			return;
		}
		const handler = handlerInput.trim();
		if (handler === '') {
			formError = 'Enter the handler name the schedule dispatches.';
			return;
		}
		let payload: Record<string, unknown>;
		try {
			const parsed: unknown = JSON.parse(payloadInput);
			if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
				throw new Error('not an object');
			}
			payload = parsed as Record<string, unknown>;
		} catch {
			formError = 'Payload must be a valid JSON object.';
			return;
		}
		const timezone = timezoneInput.trim();
		createBusy = true;
		try {
			const created = await api.createSchedule({
				server_id: serverScope,
				cron,
				run_spec: { handler_name: handler, payload },
				...(timezone === '' ? {} : { cron_timezone: timezone })
			});
			if (disposed) return;
			schedules = [...schedules, created];
			schedulesTotal += 1;
			cronInput = '';
			timezoneInput = '';
			handlerInput = '';
			payloadInput = '';
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			formError = friendlyText(
				code,
				messageOf(error) ?? 'The schedule could not be created; nothing was changed.'
			);
		} finally {
			if (!disposed) createBusy = false;
		}
	}

	// --- mount + scoped controller release (hide/unmount is the release path) -

	$effect(() => {
		if (!usable) return;
		disposed = false;
		void loadRuns();
		void loadSchedules();
		return () => {
			disposed = true;
			finishStream();
			if (refreshTimer !== null) {
				clearTimeout(refreshTimer);
				refreshTimer = null;
			}
		};
	});

	onDestroy(() => {
		disposed = true;
		finishStream();
		if (refreshTimer !== null) {
			clearTimeout(refreshTimer);
			refreshTimer = null;
		}
	});
</script>

<div class="mai-jobs" data-testid="jobs-root">
	{#if !usable}
		<div class="mai-jobs__state mai-jobs__state--error" role="alert" data-testid="panel-config-error">
			<p>This panel was opened without its jobs service or outside a pinned server scope.</p>
		</div>
	{:else}
		<header class="mai-jobs__header">
			<div class="mai-jobs__title-row">
				<span class="mai-jobs__title-icon"><ServerCog size={16} aria-hidden="true" /></span>
				<h3 class="mai-jobs__title">Jobs &amp; schedules</h3>
			</div>
			<p class="mai-jobs__meta">
				<span class="mai-jobs__mono" data-testid="jobs-scope">Server {shortId(serverScope ?? '')}</span>
				{#if serverName !== null}
					<span data-testid="jobs-scope-name">{serverName}</span>
				{:else if serverNameFailed}
					<span class="mai-jobs__row-note" data-testid="jobs-scope-name-unavailable" role="status">
						Server name unavailable — showing the pinned id.
					</span>
				{/if}
				<span>Pinned scope — every action below names this target.</span>
			</p>
		</header>

		<div class="mai-jobs__columns">
			<div class="mai-jobs__col">
				<section class="mai-jobs__section" aria-label="Runs" data-testid="runs-section">
					<div class="mai-jobs__section-head">
						<h4 class="mai-jobs__section-title">
							<span class="mai-jobs__section-icon"><ListTree size={14} aria-hidden="true" /></span>
							Runs
						</h4>
						<Button variant="secondary" onclick={loadRuns} data-testid="runs-refresh">Refresh</Button>
					</div>
					<div class="mai-jobs__section-body">
						<div class="mai-jobs__form" role="search" aria-label="Filter runs" data-testid="runs-filters">
							<div class="mai-jobs__grid">
								<Field
									label="Search (id, label, model profile)"
									bind:value={qInput}
									placeholder="deploy"
									data-testid="runs-q-input"
								/>
								<Field
									label="From (ISO instant, inclusive)"
									bind:value={dateFromInput}
									placeholder="2026-10-29T00:00:00Z"
									data-testid="runs-date-from-input"
								/>
								<Field
									label="To (ISO instant, inclusive)"
									bind:value={dateToInput}
									placeholder="2026-10-29T23:59:59Z"
									data-testid="runs-date-to-input"
								/>
							</div>
							<div class="mai-jobs__form-actions">
								<Button variant="secondary" size="sm" onclick={applyRunFilters} data-testid="runs-apply-filters">
									Apply filters
								</Button>
								<Button variant="secondary" size="sm" onclick={applyTodayFilter} data-testid="runs-today">
									Today
								</Button>
								<Button variant="secondary" size="sm" onclick={clearRunFilters} data-testid="runs-clear-filters">
									Clear
								</Button>
							</div>
						</div>
						{#if runsState.status === 'loading'}
							<div class="mai-jobs__state" role="status" data-testid="runs-loading">
								<p>Loading runs…</p>
							</div>
						{:else if runsState.status === 'error'}
							<div
								class="mai-jobs__state mai-jobs__state--error"
								role="alert"
								data-testid="runs-error"
								data-error-code={runsState.code}
							>
								<p>{runsState.message}</p>
								<Button variant="secondary" onclick={loadRuns} data-testid="runs-retry">Retry</Button>
							</div>
						{:else if runs.length === 0}
							<div class="mai-jobs__state" role="status" data-testid="runs-empty">
								{#if Object.keys(runsFilter).length > 0}
									<p>No runs match the current filters.</p>
								{:else}
									<p>No runs are recorded for this server.</p>
								{/if}
							</div>
						{:else}
							<ul class="mai-jobs__rows" data-testid="runs-list">
								{#each runs as row (row.run_id)}
									<li class="mai-jobs__row" data-testid="run-row" data-run-id={row.run_id}>
										<div class="mai-jobs__row-main">
											<span class="mai-jobs__row-id" data-testid="run-row-label">
												{row.label !== '' ? row.label : shortId(row.run_id)}
											</span>
											<span class="mai-jobs__meta mai-jobs__mono" data-testid="run-row-id">{row.run_id}</span>
											<span
												class="mai-jobs__badge"
												class:mai-jobs__badge--ok={row.state === 'succeeded'}
												class:mai-jobs__badge--danger={row.state === 'failed'}
												class:mai-jobs__badge--neutral={row.state === 'cancelled'}
												data-testid="run-row-state"
											>
												{runStateText(row.state)}
											</span>
											<span class="mai-jobs__meta" data-testid="run-row-submitted">
												Submitted {stampText(row.submitted_at)}
											</span>
											{#if completionText(row.state, row.updated_at) !== null}
												<span class="mai-jobs__meta" data-testid="run-row-completed">
													{completionText(row.state, row.updated_at)}
												</span>
											{/if}
											{#if row.cancel_requested_at !== null}
												<span class="mai-jobs__row-note" data-testid="run-row-cancel-note">
													Cancellation requested
												</span>
											{/if}
										</div>
										<div class="mai-jobs__row-actions">
											<Button
												variant="secondary"
												onclick={() => selectRun(row.run_id)}
												data-testid="run-watch"
											>
												{watchedRunId === row.run_id ? 'Watching' : 'Watch'}
											</Button>
										</div>
									</li>
								{/each}
							</ul>
							<p class="mai-jobs__meta" data-testid="runs-total">
								Showing {runs.length} of {runsTotal} recorded runs.
							</p>
							{#if runsHasMore}
								<div class="mai-jobs__row-actions">
									<Button
										variant="secondary"
										size="sm"
										loading={runsLoadingMore}
										onclick={loadMoreRuns}
										data-testid="runs-load-more"
									>
										Load more ({runs.length} of {runsTotal})
									</Button>
								</div>
							{/if}
						{/if}
					</div>
				</section>

				<section class="mai-jobs__section" aria-label="Watched run" data-testid="watched-section">
					<div class="mai-jobs__section-head">
						<h4 class="mai-jobs__section-title">
							<span class="mai-jobs__section-icon"><RadioTower size={14} aria-hidden="true" /></span>
							Watched run
						</h4>
						{#if watchedRunId !== ''}
							<span class="mai-jobs__meta mai-jobs__mono" data-testid="watched-id">{shortId(watchedRunId)}</span>
						{/if}
					</div>
					<div class="mai-jobs__section-body">
						{#if watchedRunId === ''}
							<div class="mai-jobs__state" role="status" data-testid="run-none">
								<p>No run is being watched. Select one from the run list.</p>
							</div>
						{:else if watchedState.status === 'loading'}
							<div class="mai-jobs__state" role="status" data-testid="watched-loading">
								<p>Loading run…</p>
							</div>
						{:else if watchedState.status === 'error'}
							<div
								class="mai-jobs__state mai-jobs__state--error"
								role="alert"
								data-testid="watched-error"
								data-error-code={watchedState.code}
							>
								<p>{watchedState.message}</p>
								<Button variant="secondary" onclick={loadWatched} data-testid="watched-retry">
									Retry
								</Button>
							</div>
						{:else if watchedRun !== null}
							<p class="mai-jobs__line">
								<span data-testid="watched-state">{runStateText(watchedRun.state)}</span>
								{#if watchedRun.cancel_requested_at !== null}
									<span class="mai-jobs__row-note" data-testid="watched-cancel-note">
										Cancellation requested
									</span>
								{/if}
							</p>
							<p class="mai-jobs__meta" data-testid="watched-binding">
								Profile revision <span class="mai-jobs__mono">{watchedRun.profile_revision}</span> ·
								prompt revision <span class="mai-jobs__mono">{watchedRun.prompt_revision}</span> ·
								grants {watchedRun.requested_grants.join(', ')}
							</p>

							<div class="mai-jobs__cancel">
								{#if watchedTerminal}
									<span class="mai-jobs__meta" data-testid="cancel-unavailable">
										This run is terminal; cancellation no longer applies.
									</span>
								{:else}
									<Button
										variant="danger"
										loading={cancelBusy}
										onclick={cancelWatched}
										data-testid="watched-cancel"
									>
										Cancel run
									</Button>
								{/if}
								{#if cancelStatus !== null}
									{#if cancelStatus.outcome === null}
										<span class="mai-jobs__row-note" data-testid="cancel-pending" role="status">
											Cancellation requested — not yet verified.
										</span>
									{:else if cancelStatus.outcome === 'cancelled'}
										<span class="mai-jobs__badge mai-jobs__badge--neutral" data-testid="cancel-verified">
											Cancelled (verified)
										</span>
									{:else}
										<span class="mai-jobs__meta" data-testid="cancel-other">
											Cancel observed a finished run: {cancelStatus.outcome}
										</span>
									{/if}
								{/if}
								{#if cancelError !== null}
									<span
										class="mai-jobs__row-note mai-jobs__row-note--error"
										role="alert"
										data-testid="cancel-error"
										data-error-code={cancelError.code}
									>
										{cancelError.message}
									</span>
								{/if}
							</div>

							<p class="mai-jobs__meta" data-testid="stream-status">
								{#if streamStatus === 'connecting'}
									Event stream connecting…
								{:else if streamStatus === 'live'}
									Event stream live.
								{:else if streamStatus === 'reconnecting'}
									Event stream reconnecting from the last recorded event…
								{:else if streamStatus === 'closed'}
									Event stream closed.
								{/if}
							</p>
							{#if streamProblem !== null}
								<p class="mai-jobs__row-note mai-jobs__row-note--error" role="status" data-testid="stream-problem">
									{streamProblem}
								</p>
							{/if}
							{#if refreshProblem !== null}
								<p class="mai-jobs__row-note mai-jobs__row-note--error" role="status" data-testid="refresh-problem">
									{refreshProblem}
								</p>
							{/if}

							<!-- W3 watch report: the durable final answer + tool results +
							     failure reason. Deltas are transient and never rendered;
							     thinking is never read; both lists stay bounded. -->
							<div class="mai-jobs__report" data-testid="watch-report">
								<p class="mai-jobs__workbench-title">
									<span class="mai-jobs__section-icon"><ScrollText size={13} aria-hidden="true" /></span>
									Report
								</p>
								{#if finalReport === null}
									<p class="mai-jobs__meta" data-testid="watch-report-empty" role="status">
										No final agent response is recorded for this run yet.
									</p>
								{:else}
									<p class="mai-jobs__meta" data-testid="watch-report-meta">
										Final response · {finalReport.at}{finalReport.truncated ? ' · truncated for display' : ''}
									</p>
									<pre class="mai-jobs__report-text" data-testid="watch-report-final">{finalReport.text}</pre>
								{/if}
								{#if failedReason !== null}
									<p
										class="mai-jobs__row-note mai-jobs__row-note--error"
										role="alert"
										data-testid="watch-failed"
									>
										Failure reason: {failedReason}
									</p>
								{/if}
								{#if toolResults.length > 0}
									<ul class="mai-jobs__rows" data-testid="tool-result-list">
										{#each toolResults as row (row.seq)}
											<li class="mai-jobs__event" data-testid="tool-result-row" data-tool-seq={row.seq}>
												<span class="mai-jobs__event-seq">#{row.seq}</span>
												<span class="mai-jobs__event-kind" data-testid="tool-result-capability">
													{row.capability}
												</span>
												<pre class="mai-jobs__report-text" data-testid="tool-result-text">{row.text}</pre>
											</li>
										{/each}
									</ul>
								{/if}
							</div>

							<ul class="mai-jobs__events" data-testid="event-list">
								{#each events as event (event.seq)}
									<li class="mai-jobs__event" data-testid="event-row" data-event-seq={event.seq}>
										<span class="mai-jobs__event-seq" data-testid="event-seq">#{event.seq}</span>
										<span class="mai-jobs__event-ts">{event.ts}</span>
										<span class="mai-jobs__event-kind" data-testid="event-type">{eventTypeText(event.type)}</span>
									</li>
								{/each}
							</ul>

							{#if watchedRun.steps.length === 0}
								<p class="mai-jobs__meta" data-testid="step-none">No durable steps are recorded.</p>
							{:else}
								<ul class="mai-jobs__rows" data-testid="step-list">
									{#each watchedRun.steps as step (step.seq)}
										<li class="mai-jobs__row" data-testid="step-row">
											<span class="mai-jobs__row-id">#{step.seq} {step.capability_id}</span>
											<span class="mai-jobs__badge mai-jobs__badge--neutral" data-testid="step-state">
												{stepStateText(step.state)}
											</span>
										</li>
									{/each}
								</ul>
							{/if}
						{/if}
					</div>
				</section>
			</div>

			<div class="mai-jobs__col">

				<section class="mai-jobs__section" aria-label="Schedules" data-testid="schedules-section">
					<div class="mai-jobs__section-head">
						<h4 class="mai-jobs__section-title">
							<span class="mai-jobs__section-icon"><CalendarClock size={14} aria-hidden="true" /></span>
							Schedules
						</h4>
						<Button variant="secondary" onclick={loadSchedules} data-testid="schedules-refresh">
							Refresh
						</Button>
					</div>
					<div class="mai-jobs__section-body">
						{#if schedulesState.status === 'loading'}
							<div class="mai-jobs__state" role="status" data-testid="schedules-loading">
								<p>Loading schedules…</p>
							</div>
						{:else if schedulesState.status === 'error'}
							<div
								class="mai-jobs__state mai-jobs__state--error"
								role="alert"
								data-testid="schedules-error"
								data-error-code={schedulesState.code}
							>
								<p>{schedulesState.message}</p>
								<Button variant="secondary" onclick={loadSchedules} data-testid="schedules-retry">
									Retry
								</Button>
							</div>
						{:else if schedules.length === 0}
							<div class="mai-jobs__state" role="status" data-testid="schedules-empty">
								<p>No schedules are recorded for this server.</p>
							</div>
						{:else}
							<ul class="mai-jobs__rows" data-testid="schedules-list">
								{#each schedules as row (row.schedule_id)}
									<li class="mai-jobs__row" data-testid="schedule-row" data-schedule-id={row.schedule_id}>
										<div class="mai-jobs__row-main">
											<span class="mai-jobs__row-id" data-testid="schedule-cron">{row.cron}</span>
											<span class="mai-jobs__meta mai-jobs__mono" data-testid="schedule-timezone"
												>{row.cron_timezone}</span
											>
											<span class="mai-jobs__meta mai-jobs__mono" data-testid="schedule-handler">
												{row.run_spec.handler_name}
											</span>
											<span
												class="mai-jobs__badge"
												class:mai-jobs__badge--ok={row.enabled}
												data-testid="schedule-enabled"
											>
												{row.enabled ? 'Enabled' : 'Paused'}
											</span>
										</div>
										<div class="mai-jobs__row-actions">
											<Button
												variant="secondary"
												loading={busyScheduleId === row.schedule_id}
												onclick={() => toggleSchedule(row)}
												data-testid="schedule-toggle"
											>
												{row.enabled ? 'Pause' : 'Resume'}
											</Button>
											<Button
												variant="danger"
												loading={busyScheduleId === row.schedule_id}
												onclick={() => deleteScheduleRow(row)}
												data-testid="schedule-delete"
											>
												Delete
											</Button>
										</div>
									</li>
								{/each}
							</ul>
							{#if schedulesHasMore}
								<div class="mai-jobs__row-actions">
									<Button
										variant="secondary"
										size="sm"
										loading={schedulesLoadingMore}
										onclick={loadMoreSchedules}
										data-testid="schedules-load-more"
									>
										Load more ({schedules.length} of {schedulesTotal})
									</Button>
								</div>
							{/if}
						{/if}
						{#if schedulesActionError !== null}
							<p
								class="mai-jobs__row-note mai-jobs__row-note--error"
								role="alert"
								data-testid="schedule-action-error"
								data-error-code={schedulesActionError.code}
							>
								{schedulesActionError.message}
							</p>
						{/if}

						<form
							class="mai-jobs__form"
							aria-label="Create a schedule"
							onsubmit={(event) => {
								event.preventDefault();
								void createSchedule();
							}}
							data-testid="schedule-form"
						>
							<p class="mai-jobs__workbench-title">
								<span class="mai-jobs__section-icon"><ScrollText size={13} aria-hidden="true" /></span>
								Creation workbench
							</p>
							<p class="mai-jobs__meta" data-testid="schedule-form-scope">
								New schedule for server {shortId(serverScope ?? '')} (pinned scope).
							</p>
							<div class="mai-jobs__grid">
								<Field
									label="Cron expression"
									bind:value={cronInput}
									placeholder="30 2 * * *"
									data-testid="schedule-cron-input"
								/>
								<Field
									label="Timezone (optional — defaults to UTC)"
									bind:value={timezoneInput}
									placeholder="UTC"
									data-testid="schedule-tz-input"
								/>
								<Field
									label="Handler name"
									bind:value={handlerInput}
									placeholder="collect.logs"
									data-testid="schedule-handler-input"
								/>
								<div class="mai-jobs__field">
									<!-- svelte-ignore a11y_label_has_associated_control -->
									<label class="mai-jobs__label" for="mai-jobs-payload">Payload (JSON object)</label>
									<textarea
										id="mai-jobs-payload"
										class="mai-jobs__textarea"
										rows="3"
										spellcheck="false"
										bind:value={payloadInput}
										data-testid="schedule-payload-input"
									></textarea>
								</div>
							</div>
							{#if formError !== ''}
								<p class="mai-jobs__row-note mai-jobs__row-note--error" role="alert" data-testid="schedule-form-error">
									{formError}
								</p>
							{/if}
							<div class="mai-jobs__form-actions">
								<Button type="submit" loading={createBusy} data-testid="schedule-create">
									Create schedule
								</Button>
							</div>
						</form>
					</div>
				</section>
			</div>
		</div>
	{/if}
</div>

<style>
	.mai-jobs {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-4);
		padding: var(--mai-space-4);
		background: var(--mai-color-surface);
		color: var(--mai-color-text);
		height: 100%;
		overflow-y: auto;
	}

	/* Cogitator masthead: display serif title on a header band with the
	 * brass left rule; pinned scope named in mono telemetry. */
	.mai-jobs__header {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-space-3);
		background: var(--mai-color-surface-header);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-sm);
	}

	.mai-jobs__title-row {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
	}

	.mai-jobs__title-icon {
		display: inline-flex;
		flex-shrink: 0;
		color: var(--mai-color-brass);
	}

	.mai-jobs__title {
		margin: 0;
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-lg);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-brass);
	}

	.mai-jobs__meta {
		margin: 0;
		display: flex;
		flex-wrap: wrap;
		gap: var(--mai-space-3);
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text-muted);
	}

	.mai-jobs__mono {
		font-family: var(--mai-font-mono);
	}

	/* Two-column cogitator grid: runs + live stream left, schedules right;
	 * collapses to a single column on narrow hosts. */
	.mai-jobs__columns {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(min(420px, 100%), 1fr));
		gap: var(--mai-space-4);
		align-items: start;
	}

	.mai-jobs__col {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-4);
		min-width: 0;
	}

	/* Rite card: raised plate, full-width header band, brass left rule. */
	.mai-jobs__section {
		display: flex;
		flex-direction: column;
		background: var(--mai-color-surface-raised);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-lg);
		overflow: hidden;
	}

	.mai-jobs__section-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--mai-space-3);
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface-header);
		border-left: 3px solid var(--mai-color-brass);
		border-bottom: var(--mai-border-width) solid var(--mai-color-border);
	}

	.mai-jobs__section-title {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2);
		margin: 0;
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-brass);
	}

	.mai-jobs__section-icon {
		display: inline-flex;
		flex-shrink: 0;
		color: var(--mai-color-brass-dim);
	}

	.mai-jobs__section-body {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		padding: var(--mai-space-3);
	}

	.mai-jobs__state {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		align-items: flex-start;
		padding: var(--mai-space-3);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-status-neutral-bg);
		color: var(--mai-status-neutral-text);
		font-size: var(--mai-text-sm);
	}

	.mai-jobs__state--error {
		background: var(--mai-status-danger-bg);
		color: var(--mai-status-danger-text);
		border: var(--mai-border-width) solid var(--mai-status-danger-border);
	}

	.mai-jobs__state p {
		margin: 0;
	}

	.mai-jobs__rows,
	.mai-jobs__events {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
	}

	/* W3: the runs list scrolls within a bounded card (10-row pages must not
	 * stretch the window); the runs list keeps its own scroll strip. */
	section[aria-label='Runs'] .mai-jobs__rows {
		max-height: calc(var(--mai-space-8) * 8);
		overflow-y: auto;
	}

	/* W3 watch report: whitespace-preserving bounded text (final answer and
	 * tool results keep their line structure; long text wraps, never scrolls
	 * the card). */
	.mai-jobs__report {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-sm);
		max-height: calc(var(--mai-space-8) * 10);
		overflow-y: auto;
	}

	.mai-jobs__report-text {
		margin: 0;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		white-space: pre-wrap;
		word-break: break-word;
		color: var(--mai-color-text);
	}

	/* Recorded rows read as inset strips engraved into the raised plate. */
	.mai-jobs__row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--mai-space-3);
		padding: var(--mai-space-2) var(--mai-space-3);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface);
	}

	.mai-jobs__row-main {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: var(--mai-space-3);
		min-width: 0;
	}

	/* Run ids, cron expressions, step counters: mono telemetry. */
	.mai-jobs__row-id {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-sm);
	}

	.mai-jobs__row-actions {
		display: flex;
		gap: var(--mai-space-2);
		flex-shrink: 0;
	}

	.mai-jobs__row-note {
		font-size: var(--mai-text-sm);
		color: var(--mai-status-warning-text);
	}

	.mai-jobs__row-note--error {
		color: var(--mai-status-danger-text);
	}

	.mai-jobs__badge {
		display: inline-flex;
		align-items: center;
		padding: 0 var(--mai-space-2);
		min-height: calc(var(--mai-space-4) + var(--mai-space-1));
		border-radius: var(--mai-radius-sm);
		border: var(--mai-border-width) solid var(--mai-status-neutral-border);
		background: var(--mai-status-neutral-bg);
		color: var(--mai-status-neutral-text);
		font-size: var(--mai-text-sm);
	}

	.mai-jobs__badge--ok {
		background: var(--mai-status-success-bg);
		border-color: var(--mai-status-success-border);
		color: var(--mai-status-success-text);
	}

	.mai-jobs__badge--danger {
		background: var(--mai-status-danger-bg);
		border-color: var(--mai-status-danger-border);
		color: var(--mai-status-danger-text);
	}

	.mai-jobs__badge--neutral {
		background: var(--mai-status-info-bg);
		border-color: var(--mai-status-info-border);
		color: var(--mai-status-info-text);
	}

	.mai-jobs__line {
		margin: 0;
		display: flex;
		align-items: center;
		gap: var(--mai-space-3);
	}

	.mai-jobs__cancel {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: var(--mai-space-3);
	}

	/* Live SSE telemetry log: mono log strip under a brass rule; the bounded
	 * replay scrolls inside the strip instead of stretching the card. */
	.mai-jobs__events {
		gap: var(--mai-space-1);
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-sm);
		max-height: calc(var(--mai-space-8) * 7);
		overflow-y: auto;
	}

	.mai-jobs__event {
		display: flex;
		align-items: baseline;
		gap: var(--mai-space-2);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
	}

	.mai-jobs__event-seq {
		flex-shrink: 0;
		color: var(--mai-color-brass);
	}

	.mai-jobs__event-ts {
		flex-shrink: 0;
		color: var(--mai-color-text-muted);
	}

	.mai-jobs__event-kind {
		min-width: 0;
		color: var(--mai-color-text);
	}

	/* Creation workbench: inset console plate beneath the matrix. */
	.mai-jobs__form {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		padding: var(--mai-space-3);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
	}

	.mai-jobs__workbench-title {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		margin: 0;
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-brass);
	}

	.mai-jobs__grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
		gap: var(--mai-space-3);
	}

	.mai-jobs__field {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
	}

	.mai-jobs__label {
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-text-muted);
	}

	.mai-jobs__textarea {
		width: 100%;
		min-width: 0;
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface-raised);
		color: var(--mai-color-text);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		font-size: var(--mai-text-base);
		font-family: var(--mai-font-mono);
		resize: vertical;
	}

	.mai-jobs__textarea:focus-visible {
		outline: var(--mai-border-width) solid var(--mai-focus-ring-color);
		outline-offset: 1px;
	}

	.mai-jobs__form-actions {
		display: flex;
		justify-content: flex-start;
	}
</style>
