<script module lang="ts">
	/**
	 * ObjectiveActivity — the live v2 operations view for one objective
	 * episode (runtime-v2.md §11 activity-ui row; rv_activity-ui AC1–AC4).
	 *
	 * One canonical ledger: the header, timeline and controls are
	 * PROJECTIONS of `/api/v1/objectives/{id}` truth (detail, journal,
	 * bounded event/run pages) plus the SSE ledger stream with
	 * `Last-Event-ID` replay (C08 rule 7 — reconnects deduplicate by the
	 * monotonic sequence watermark). No second execution engine: the view
	 * never submits effects; controls cross the frozen §9 vocabulary only.
	 *
	 * Fail-closed wiring: the api bag is narrowed structurally (every used
	 * method must be a function, else the section renders its honest
	 * unbound state) — a legacy host that lacks the v2 slice never reaches
	 * the network through this component (AGENTS.md wire honesty).
	 */
	import ObjectiveHeader from './ObjectiveHeader.svelte';
	import ObjectiveTimeline from './ObjectiveTimeline.svelte';
	import ObjectiveControls from './ObjectiveControls.svelte';
	import DelegationTree from '../components/DelegationTree.svelte';
	import {
		objectiveEventsUrl,
		readObjectiveEvent,
		type ObjectiveDetail,
		type ObjectiveEventFrame,
		type ObjectiveRunLink,
		type StepClassificationBody,
		type StepResolutionView,
		type SubscribeEventsOptions,
		type WorkJournal
	} from '../api/client.js';
	import { subscribeEvents } from '../api/client.js';
	import type { components } from '../api/schema.generated.js';
</script>

<script lang="ts">
	/** The structural api slice this view consumes (panel bag narrowed). */
	export interface ObjectiveActivityApi {
		readObjective(objectiveId: string, serverId: string): Promise<ObjectiveDetail>;
		readObjectiveJournal(objectiveId: string, serverId: string): Promise<WorkJournal>;
		listObjectiveEvents(
			objectiveId: string,
			serverId: string,
			query?: { limit?: number; offset?: number; afterSeq?: number }
		): Promise<{
			limit: number;
			offset: number;
			total: number;
			has_more: boolean;
			items?: ObjectiveEventFrame[];
		}>;
		listObjectiveRuns(
			objectiveId: string,
			serverId: string,
			page?: { limit?: number; offset?: number }
		): Promise<{
			limit: number;
			offset: number;
			total: number;
			has_more: boolean;
			items?: ObjectiveRunLink[];
		}>;
		readRun(runId: string): Promise<{
			run_id: string;
			state: string | null;
			model_profile: string;
			steps: Array<{ seq: number; capability_id: string; state: string; receipt: Record<string, unknown> | null }>;
		}>;
		readConversation(runId: string): Promise<Record<string, unknown>>;
		resumeConversation(
			conversationId: string,
			payload: { server_id: string; message?: string | null }
		): Promise<{ conversation_id: string; run_id: string | null; disposition: string }>;
		reconcileConversation(
			conversationId: string,
			payload: { server_id: string; classifications: StepClassificationBody[] }
		): Promise<{ conversation_id: string; run_id: string; run_state: string | null; results: StepResolutionView[] }>;
	}

	/** Bounded pages (AGENTS.md §1; the ledger is a projection, not a dump).
	 * The ledger page stays deliberately small: the narrative layer above
	 * carries the live tail, and "Load more" is the explicit older-pages
	 * control — never an unbounded dump. */
	const EVENT_PAGE_LIMIT = 5;
	const RUN_PAGE_LIMIT = 10;
	const BACKOFF_MS = 750;
	const REFRESH_DEBOUNCE_MS = 300;

	function readApi(services: unknown): ObjectiveActivityApi | null {
		if (typeof services !== 'object' || services === null) return null;
		const candidate = services as Partial<ObjectiveActivityApi>;
		for (const name of [
			'readObjective',
			'readObjectiveJournal',
			'listObjectiveEvents',
			'listObjectiveRuns',
			'readRun',
			'readConversation',
			'resumeConversation',
			'reconcileConversation'
		] as const) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as ObjectiveActivityApi;
	}

	function shortId(id: string | null): string {
		return id === null || id === '' ? '—' : id.slice(0, 8);
	}

	function messageOf(error: unknown): string {
		return error instanceof Error && error.message ? error.message : 'The request failed.';
	}

	let {
		objectiveId,
		serverId,
		conversationId,
		api,
		serverLabels = {},
		elapsedSeconds = null,
		pendingSteering = 0,
		liveRunId = null
	}: {
		objectiveId: string;
		/** The explicit read scope (the open conversation's recorded server). */
		serverId: string;
		/** The open conversation identity (resume/reconcile target). */
		conversationId: string;
		/** The panel's services bag (narrowed structurally; null ⇒ unbound). */
		api: unknown;
		serverLabels?: Readonly<Record<string, string>>;
		/** Live elapsed seconds of the panel's active run. */
		elapsedSeconds?: number | null;
		/** Pending steering rows of the open conversation. */
		pendingSteering?: number;
		/** The conversation's live run id, when one holds the lease. */
		liveRunId?: string | null;
	} = $props();

	const bag = $derived(readApi(api));
	const usable = $derived(bag !== null && objectiveId !== '' && serverId !== '');

	// --- honest per-collection states (loading/empty/error; C08 rule 6) ------

	let detail = $state<ObjectiveDetail | null>(null);
	let journal = $state<WorkJournal | null>(null);
	let events = $state<ObjectiveEventFrame[]>([]);
	let eventsTotal = $state(0);
	let eventsHasMore = $state(false);
	let eventsLoadingMore = $state(false);
	let eventsError = $state<string | null>(null);
	let runs = $state<ObjectiveRunLink[]>([]);
	let runsTotal = $state(0);
	let runsHasMore = $state(false);
	let runsLoadingMore = $state(false);
	let runStates = $state<Record<string, string | null>>({});
	let runModels = $state<Record<string, string>>({});
	let openUnknowns = $state<Array<{ seq: number; capabilityId: string }>>([]);
	/** The run whose delegation tree is expanded (one at a time). */
	let expandedRunId = $state<string | null>(null);
	/** The fetched conversation view of the expanded run (honest states). */
	let expandedRunView = $state<Record<string, unknown> | null>(null);
	let expandedRunLoading = $state(false);
	let expandedRunError = $state<string | null>(null);
	let detailError = $state<string | null>(null);
	let loading = $state(true);
	let controlBusy = $state(false);
	let controlProblem = $state<string | null>(null);
	let reconcileResult = $state<Awaited<
		ReturnType<ObjectiveActivityApi['reconcileConversation']>
	> | null>(null);

	/** The root (non-child) run binding — the header's primary run. */
	const rootRun = $derived(runs.find((one) => one.parent_run_id === null) ?? runs[0] ?? null);
	const rootRunState = $derived(rootRun === null ? null : (runStates[rootRun.run_id] ?? null));
	const rootModel = $derived(
		rootRun === null ? null : (runModels[rootRun.run_id] ?? null)
	);
	/** The concise current action: the latest ledger fact while the root
	 * run is live; the frozen §9 wait overrides win when parked. */
	const currentAction = $derived.by<string | null>(() => {
		if (openUnknowns.length > 0) return 'Reconciling an unproven outcome';
		if (rootRunState !== 'running' && rootRunState !== 'queued') return null;
		const latest = events.length > 0 ? events[events.length - 1] : null;
		if (latest === null) return null;
		if (latest.type.startsWith('objective.journal_updated')) return 'Receipts/verification journaling';
		if (latest.type.startsWith('objective.checklist')) return 'Advisory checklist update';
		if (latest.type.startsWith('agent.tool')) return `Tool activity (${latest.type})`;
		if (latest.type.startsWith('agent.model_round')) return 'Model round';
		if (latest.type.startsWith('agent.delegation')) return 'Delegation activity';
		if (latest.type.startsWith('agent.budget')) return 'Budget ledger update';
		return latest.type;
	});
	/** Elapsed shows only while THIS view's root run is the live one. */
	const headerElapsed = $derived(
		liveRunId !== null && rootRun !== null && liveRunId === rootRun.run_id
			? elapsedSeconds
			: null
	);

	/** Ledger page: bounded, ascending, deduplicated by sequence. */
	function mergeEvents(page: { items?: ObjectiveEventFrame[] }): void {
		const seen = new Set(events.map((one) => one.sequence));
		const fresh = (page.items ?? []).filter((one) => !seen.has(one.sequence));
		events = [...events, ...fresh].sort((a, b) => a.sequence - b.sequence);
		eventsTotal = Math.max(eventsTotal, events.length);
	}

	async function loadHead(): Promise<void> {
		if (bag === null) return;
		loading = true;
		detailError = null;
		try {
			const [objective, eventPage, runPage] = await Promise.all([
				bag.readObjective(objectiveId, serverId),
				bag.listObjectiveEvents(objectiveId, serverId, {
					limit: EVENT_PAGE_LIMIT,
					offset: 0
				}),
				bag.listObjectiveRuns(objectiveId, serverId, { limit: RUN_PAGE_LIMIT, offset: 0 })
			]);
			if (disposed) return;
			detail = objective;
			events = [];
			eventsTotal = eventPage.total;
			eventsHasMore = eventPage.has_more;
			mergeEvents(eventPage);
			runs = runPage.items ?? [];
			runsTotal = runPage.total;
			runsHasMore = runPage.has_more;
			eventsError = null;
			await refreshProjections();
		} catch (error) {
			if (disposed) return;
			detailError = messageOf(error);
		} finally {
			if (!disposed) loading = false;
		}
	}

	async function loadMoreEvents(): Promise<void> {
		if (bag === null || eventsLoadingMore || !eventsHasMore) return;
		eventsLoadingMore = true;
		try {
			const page = await bag.listObjectiveEvents(objectiveId, serverId, {
				limit: EVENT_PAGE_LIMIT,
				offset: events.length
			});
			if (disposed) return;
			eventsHasMore = page.has_more;
			eventsTotal = page.total;
			mergeEvents(page);
			eventsError = null;
		} catch (error) {
			if (disposed) return;
			eventsError = messageOf(error);
		} finally {
			if (!disposed) eventsLoadingMore = false;
		}
	}

	async function loadMoreRuns(): Promise<void> {
		if (bag === null || runsLoadingMore || !runsHasMore) return;
		runsLoadingMore = true;
		try {
			const page = await bag.listObjectiveRuns(objectiveId, serverId, {
				limit: RUN_PAGE_LIMIT,
				offset: runs.length
			});
			if (disposed) return;
			const seen = new Set(runs.map((one) => one.run_id));
			runs = [...runs, ...(page.items ?? []).filter((one) => !seen.has(one.run_id))];
			runsTotal = page.total;
			runsHasMore = page.has_more;
			await readRunStates();
		} catch (error) {
			if (disposed) return;
			controlProblem = messageOf(error);
		} finally {
			if (!disposed) runsLoadingMore = false;
		}
	}

	/** Journal + run-state catch-up after ledger movement (§8.4: the
	 * projection catches up honestly; effects stay authoritative). New run
	 * bindings (delegation children, resumed runs) merge in deduplicated. */
	async function refreshProjections(): Promise<void> {
		if (bag === null) return;
		try {
			const derived = await bag.readObjectiveJournal(objectiveId, serverId);
			if (disposed) return;
			journal = derived;
		} catch {
			if (disposed) return;
			journal = null; // honest absence; never a fabricated document
		}
		try {
			const page = await bag.listObjectiveRuns(objectiveId, serverId, {
				limit: RUN_PAGE_LIMIT,
				offset: 0
			});
			if (disposed) return;
			const seen = new Set(runs.map((one) => one.run_id));
			const fresh = (page.items ?? []).filter((one) => !seen.has(one.run_id));
			if (fresh.length > 0) {
				runs = [...runs, ...fresh];
				runsTotal = Math.max(runsTotal, page.total, runs.length);
				runsHasMore = runs.length < runsTotal;
			}
		} catch {
			// the runs refresh failing never kills the section
		}
		await readRunStates();
	}

	async function readRunStates(): Promise<void> {
		if (bag === null) return;
		const nextStates: Record<string, string | null> = { ...runStates };
		const nextModels: Record<string, string> = { ...runModels };
		for (const link of runs) {
			try {
				const runDetail = await bag.readRun(link.run_id);
				if (disposed) return;
				nextStates[link.run_id] = runDetail.state;
				nextModels[link.run_id] = runDetail.model_profile;
				// Open unknown effects of the LIVE run feed the §9 gate.
				if (liveRunId === link.run_id || openUnknowns.length === 0) {
					const open = runDetail.steps
						.filter((step) => step.state === 'outcome_unknown')
						.map((step) => ({ seq: step.seq, capabilityId: step.capability_id }));
					if (liveRunId === link.run_id) openUnknowns = open;
				}
			} catch {
				// one unreadable run row never kills the section
			}
		}
		if (disposed) return;
		runStates = nextStates;
		runModels = nextModels;
	}

	/**
	 * The tree view of the expanded run. The facade read is the first
	 * source; when it fails (or serves no children while the objective's
	 * run bindings have children for this parent), the tree is synthesized
	 * from the RECORDED objective_runs bindings (parent_run_id linkage +
	 * initiating server pins) + the runs' replayed states — never invented
	 * states (§3.3: every run binds one initiating server).
	 */
	function synthesizedTreeView(runId: string): Record<string, unknown> | null {
		const childBindings = runs.filter((one) => one.parent_run_id === runId);
		if (childBindings.length === 0) return null;
		const self = runs.find((one) => one.run_id === runId);
		return {
			run_id: runId,
			server_id: self?.initiating_server_id ?? serverId,
			parent_run_id: null,
			depth: 0,
			model_profile: runModels[runId] ?? '—',
			requested_grants: [],
			label: '',
			state: runStates[runId] ?? null,
			cancel_requested_at: null,
			submitted_at: '',
			updated_at: '',
			tag_preview: { preview: [], total: 0 },
			children: childBindings.map((link) => ({
				run_id: link.run_id,
				server_id: link.initiating_server_id,
				parent_run_id: link.parent_run_id,
				depth: 1,
				model_profile: runModels[link.run_id] ?? '—',
				requested_grants: [],
				label: '',
				state: runStates[link.run_id] ?? null,
				cancel_requested_at: null
			}))
		};
	}

	/** Expand one run's delegation tree: the ONE readConversation view plus
	 * the objective journal (aggregated receipts). A facade read failure
	 * falls back to the run-bindings projection — named honestly in the
	 * tree, never silently. */
	async function toggleRunTree(runId: string): Promise<void> {
		const slice = bag;
		if (slice === null) return;
		if (expandedRunId === runId) {
			expandedRunId = null;
			expandedRunView = null;
			expandedRunError = null;
			return;
		}
		expandedRunId = runId;
		expandedRunView = null;
		expandedRunError = null;
		expandedRunLoading = true;
		try {
			const childBindings = runs.filter((one) => one.parent_run_id === runId);
			let view: Record<string, unknown> | null = null;
			try {
				const fetched = await slice.readConversation(runId);
				if (disposed || expandedRunId !== runId) return;
				const fetchedChildren = (fetched.children ?? []) as unknown[];
				if (fetchedChildren.length > 0 || childBindings.length === 0) {
					view = fetched;
				} else {
					// The facade served no children while the objective holds
					// child bindings: project the recorded bindings.
					view = synthesizedTreeView(runId);
				}
			} catch {
				if (disposed || expandedRunId !== runId) return;
				// Facade read failed: the run-bindings projection is the
				// honest fallback (the tree names it via the cursor note).
				view = synthesizedTreeView(runId);
			}
			if (view === null) {
				expandedRunError =
					'No delegation records are available for this run (facade read failed and no child bindings exist).';
			} else {
				expandedRunView = view;
			}
		} catch (error) {
			if (disposed) return;
			expandedRunError = messageOf(error);
		} finally {
			if (!disposed) expandedRunLoading = false;
		}
	}

	// --- controls (state-specific; §9; AC3) ----------------------------------

	async function resume(message: string): Promise<void> {
		if (bag === null || controlBusy) return;
		controlBusy = true;
		controlProblem = null;
		try {
			const submitted = await bag.resumeConversation(conversationId, {
				server_id: serverId,
				message
			});
			if (disposed) return;
			reconcileResult = null;
			controlProblem = null;
			// A resumed run re-anchors the live fold through the panel's own
			// tail refresh; the section re-reads its projections.
			await refreshProjections();
			if (submitted.run_id !== null && submitted.run_id !== '') {
				const link = {
					objective_id: objectiveId,
					run_id: submitted.run_id,
					parent_run_id: null,
					initiating_server_id: serverId,
					handler: 'agent.run_v2' as const,
					actor_kind: 'operator' as const,
					created_at: new Date().toISOString()
				};
				if (!runs.some((one) => one.run_id === submitted.run_id)) {
					runs = [...runs, link];
				}
			}
		} catch (error) {
			if (disposed) return;
			controlProblem = messageOf(error);
		} finally {
			if (!disposed) controlBusy = false;
		}
	}

	async function reconcile(entries: StepClassificationBody[]): Promise<void> {
		if (bag === null || controlBusy || entries.length === 0) return;
		controlBusy = true;
		controlProblem = null;
		try {
			const view = await bag.reconcileConversation(conversationId, {
				server_id: serverId,
				classifications: entries
			});
			if (disposed) return;
			reconcileResult = view;
			openUnknowns = [];
			await refreshProjections();
		} catch (error) {
			if (disposed) return;
			controlProblem = messageOf(error);
		} finally {
			if (!disposed) controlBusy = false;
		}
	}

	// --- SSE ledger subscription (Last-Event-ID replay; C08 rule 7) ----------

	let lastSequence = $state(0);
	let disposed = true;
	let streamAbort: AbortController | null = null;
	let projectionTimer: ReturnType<typeof setTimeout> | null = null;

	function scheduleProjectionRefresh(): void {
		if (projectionTimer !== null || disposed) return;
		projectionTimer = setTimeout(() => {
			projectionTimer = null;
			void refreshProjections();
		}, REFRESH_DEBOUNCE_MS);
	}

	function foldObjectiveEvent(event: ObjectiveEventFrame): void {
		if (event.sequence <= lastSequence) return;
		lastSequence = event.sequence;
		// The REAL frame fields (value-free wire truth) — never fabricated
		// timestamps or actor kinds. Dedup is the sequence watermark.
		mergeEvents({ items: [event] });
		scheduleProjectionRefresh();
	}

	async function streamLoop(controller: AbortController): Promise<void> {
		if (bag === null) return;
		while (!disposed && !controller.signal.aborted) {
			try {
				await subscribeEvents(objectiveEventsUrl(objectiveId, serverId), {
					cursor: lastSequence > 0 ? String(lastSequence) : null,
					signal: controller.signal,
					onEvent: (frame) => {
						if (disposed) return;
						const event = readObjectiveEvent(frame);
						if (event === null || event.objective_id !== objectiveId) return;
						foldObjectiveEvent(event);
					}
				} satisfies SubscribeEventsOptions);
			} catch (error) {
				if (disposed || controller.signal.aborted) return;
				const status =
					typeof error === 'object' && error !== null && 'status' in error
						? (error as { status?: number }).status
						: undefined;
				if (status === 404) {
					// The objective is gone or out of scope: the stream stays
					// closed and the section names it honestly.
					detailError = 'The objective is not readable in this server scope.';
					return;
				}
				// Network/HTTP blip: bounded backoff, then replay from the
				// watermark (deduplicated by construction).
			}
			if (disposed || controller.signal.aborted) return;
			await new Promise<void>((resolve) => {
				const timer = setTimeout(resolve, BACKOFF_MS);
				controller.signal.addEventListener(
					'abort',
					() => {
						clearTimeout(timer);
						resolve();
					},
					{ once: true }
				);
			});
		}
	}

	$effect(() => {
		if (!usable) return;
		disposed = false;
		const controller = new AbortController();
		streamAbort = controller;
		void loadHead();
		void streamLoop(controller);
		// Bounded catch-up cadence: the SSE stream is the low-latency path;
		// this poll guarantees the journal/runs projection catches up even
		// across stream closes/reconnects (§8.4 — the projection catches up
		// honestly; the effects' durable records stay authoritative).
		const timer = setInterval(() => {
			if (disposed) return;
			void refreshProjections();
		}, 2_000);
		return () => {
			disposed = true;
			controller.abort();
			streamAbort = null;
			clearInterval(timer);
			if (projectionTimer !== null) {
				clearTimeout(projectionTimer);
				projectionTimer = null;
			}
		};
	});

	/** A live-run change (follow-up turns) re-reads the open-unknown set. */
	$effect(() => {
		if (!usable || liveRunId === null) return;
		void readRunStates();
	});
</script>

<section
	class="acui-activity"
	aria-label="Objective activity"
	data-testid="objective-activity"
	data-objective={objectiveId}
>
	<div class="acui-activity__head">
		<h4 class="acui-activity__title" data-testid="objective-activity-title">
			Objective activity (runtime v2)
		</h4>
		<span class="acui-activity__meta">{shortId(objectiveId)}</span>
	</div>

	{#if bag === null}
		<p class="acui-activity__state" role="status" data-testid="objective-unbound">
			The host did not provide the runtime v2 objective services; the live
			objective view is unavailable (fail closed).
		</p>
	{:else if loading && detail === null}
		<p class="acui-activity__state" role="status" data-testid="objective-loading">
			Loading objective activity…
		</p>
	{:else if detailError !== null && detail === null}
		<p class="acui-activity__state acui-activity__state--error" role="alert" data-testid="objective-error">
			{detailError}
		</p>
	{:else if detail !== null}
		<ObjectiveHeader
			{detail}
			{journal}
			{runs}
			{runStates}
			runState={rootRunState}
			model={rootModel}
			elapsedSeconds={headerElapsed}
			{currentAction}
			{pendingSteering}
			{serverLabels}
		/>
		<ObjectiveControls
			runState={rootRunState}
			{openUnknowns}
			busy={controlBusy}
			onresume={resume}
			onreconcile={reconcile}
			{reconcileResult}
			problem={controlProblem}
		/>
		<ObjectiveTimeline
			{events}
			total={eventsTotal}
			hasMore={eventsHasMore}
			loadingMore={eventsLoadingMore}
			onloadmore={loadMoreEvents}
			{journal}
			error={eventsError}
		/>
		<details class="acui-activity__runs" data-testid="objective-runs">
			<summary>Objective runs ({runs.length} of {runsTotal})</summary>
			<ul class="acui-activity__run-list">
				{#each runs as run (run.run_id)}
					<li class="acui-activity__run" data-testid="objective-run-row" data-run-id={run.run_id}>
						<button
							type="button"
							class="acui-activity__run-toggle"
							aria-expanded={expandedRunId === run.run_id}
							onclick={() => void toggleRunTree(run.run_id)}
							data-testid="objective-run-toggle"
						>
							<span class="acui-activity__run-state" data-state={runStates[run.run_id] ?? 'unknown'}>
								{runStates[run.run_id] ?? 'State not recorded yet'}
							</span>
							<span class="acui-activity__meta">
								run {shortId(run.run_id)} · handler {run.handler}
								· target {serverLabels[run.initiating_server_id] ?? shortId(run.initiating_server_id)}
								· {run.actor_kind}
							</span>
						</button>
						{#if expandedRunId === run.run_id}
							<div class="acui-activity__tree" data-testid="objective-run-tree">
								{#if expandedRunLoading}
									<p class="acui-activity__meta" role="status" data-testid="objective-run-tree-loading">
										Loading the run's delegation tree…
									</p>
								{:else if expandedRunError !== null}
									<p class="acui-activity__meta" role="alert" data-testid="objective-run-tree-error">
										{expandedRunError}
									</p>
								{:else if expandedRunView !== null}
									<DelegationTree
										conversation={expandedRunView as components['schemas']['ConversationView']}
										api={api}
										taskLabels={{}}
										showCancel={false}
										objectiveJournal={journal}
										v2Runs={runs}
										{serverLabels}
									/>
								{/if}
							</div>
						{/if}
					</li>
				{/each}
			</ul>
			<p class="acui-activity__meta" data-testid="objective-runs-total">
				Showing {runs.length} of {runsTotal} bound runs.
			</p>
			{#if runsHasMore}
				<button
					type="button"
					class="acui-activity__more"
					disabled={runsLoadingMore}
					onclick={loadMoreRuns}
					data-testid="objective-runs-load-more"
				>
					{runsLoadingMore ? 'Loading…' : `Load more runs (${runs.length} of ${runsTotal})`}
				</button>
			{/if}
		</details>
	{/if}
</section>

<style>
	.acui-activity {
		display: flex;
		flex-direction: column;
		gap: 0.45rem;
		margin: 0.25rem 0 0.5rem;
		padding: 0.5rem;
		border: 1px solid var(--border, #4444);
		border-radius: 0.5rem;
		min-width: 0;
	}
	.acui-activity__head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.acui-activity__title {
		margin: 0;
		font-size: 0.78rem;
		letter-spacing: 0.03em;
		text-transform: uppercase;
	}
	.acui-activity__state {
		margin: 0;
		font-size: 0.75rem;
		color: var(--muted, inherit);
	}
	.acui-activity__state--error {
		color: var(--error, #c44);
	}
	.acui-activity__meta {
		font-size: 0.68rem;
		color: var(--muted, inherit);
		overflow-wrap: anywhere;
	}
	.acui-activity__runs {
		border: 1px solid var(--border, #4444);
		border-radius: 0.4rem;
	}
	.acui-activity__runs summary {
		cursor: pointer;
		padding: 0.25rem 0.5rem;
		font-size: 0.75rem;
	}
	.acui-activity__run-list {
		list-style: none;
		margin: 0;
		padding: 0.2rem 0.5rem;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		max-height: 10rem;
		overflow-y: auto;
	}
	.acui-activity__run {
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		border-bottom: 1px dashed var(--border, #3333);
		padding-bottom: 0.2rem;
	}
	.acui-activity__run-toggle {
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		font: inherit;
		text-align: left;
		background: transparent;
		border: 0;
		color: inherit;
		cursor: pointer;
		padding: 0;
	}
	.acui-activity__run-toggle:focus-visible {
		outline: 2px solid var(--accent, #888);
		outline-offset: 2px;
	}
	.acui-activity__tree {
		margin-top: 0.25rem;
		padding: 0.25rem;
		border: 1px dashed var(--border, #3333);
		border-radius: 0.3rem;
		font-size: 0.72rem;
	}
	.acui-activity__run-state {
		font-size: 0.72rem;
		font-weight: 600;
	}
	.acui-activity__run-state[data-state='succeeded'] {
		color: var(--success, #4a4);
	}
	.acui-activity__run-state[data-state='failed'],
	.acui-activity__run-state[data-state='outcome_unknown'] {
		color: var(--error, #c44);
	}
	.acui-activity__more {
		align-self: center;
		font: inherit;
		font-size: 0.72rem;
		padding: 0.15rem 0.6rem;
		margin: 0.25rem 0 0.4rem;
		border-radius: 999px;
		border: 1px solid var(--border, #4444);
		background: transparent;
		color: inherit;
		cursor: pointer;
	}
	.acui-activity__more:disabled {
		cursor: default;
		color: var(--muted, inherit);
	}
	.acui-activity__more:focus-visible {
		outline: 2px solid var(--accent, #888);
		outline-offset: 1px;
	}
	.acui-activity__runs summary:focus-visible {
		outline: 2px solid var(--accent, #888);
		outline-offset: 1px;
	}
</style>
