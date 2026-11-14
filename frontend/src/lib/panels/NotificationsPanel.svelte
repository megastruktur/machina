<script module lang="ts">
	/**
	 * NotificationsPanel — the fleet-wide unified notification inbox
	 * (campaign `notification-inbox`, `nbi-fe-panel` + `nbi-fe-filters`;
	 * contracts docs/architecture/notification-inbox.md, AGENTS.md pagination
	 * invariant, C08 rule 6, C11).
	 *
	 * Layer: reusable feature panel (windowed-workspace ADR-0001 §2 layer
	 * table). It owns NO window mechanics and NO scope selection:
	 *
	 *  - GLOBAL window, fleet-wide reads: every read is the reviewed global
	 *    route under the ONE canonical filter query this panel owns (R4-F2):
	 *    the server scope Select (All / Global / enrolled via `listProfiles`),
	 *    the read-state Select, the origin chips and the debounced `q` — a
	 *    server filter is an explicit query param inside the panel, never a
	 *    pinned scope (C01 rule 2). List, unread-count and mark-all-read share
	 *    the exact same query snapshot per dispatch; a response may only land
	 *    while its epoch AND filter snapshot still match (the OverviewPanel
	 *    race guard — a stale in-flight page never renders). The panel reads
	 *    `panel.services.api` only; an absent or incomplete bag fails closed
	 *    with a visible state (no locator).
	 *  - Attention controls (R4-F2, C08 rule 6): debounced server-side search
	 *    (`q`, trimmed, omitted when empty — commits reset pagination to
	 *    offset 0), All/Unread/Read toggle (`unread`), origin chips
	 *    (`origin`), and mark-all-read under the EXACT active filters —
	 *    receipt-counted (`{updated}`), never optimistic, with a durable
	 *    list + unread-count refetch afterwards.
	 *  - Honest pagination (AGENTS.md invariant): bounded initial page
	 *    (limit 50), `offset`/`total`/`hasMore`/`loadingMore` state, Load
	 *    more appends rows deduplicated by `alert_id` inside a bounded
	 *    scroll container. The `has_more` flag of the producer's envelope —
	 *    never a local guess — gates the control.
	 *  - Page-bound grouping (R4-F1): rows are grouped by server OVER THE
	 *    LOADED PAGE ONLY; headers read «<name or id> — N loaded» / «Global —
	 *    N loaded». The panel never implies complete per-server totals.
	 *  - Honest rows (C08 rule 6): severity as color AND text label, source
	 *    chip (or origin when `source` is null), receipt-driven read/unread
	 *    toggling — the row flips ONLY to the `AckResult.alert` the route
	 *    answered, never optimistically; failures show the envelope
	 *    `code`/message and leave the row untouched. `body` expands as
	 *    plain text (Svelte escaping; no `{@html}` anywhere).
	 *  - Live is a SIGNAL (the JobsPanel/OverviewPanel contract): the
	 *    wakeup-only SSE stream delivers identity frames; the panel reads
	 *    NOTHING from them — every frame (and a clean stream close) merely
	 *    schedules a debounced refetch of the durable list + unread count.
	 *    Hide/close/unmount releases the stream via the `$effect` teardown.
	 *  - No module-level state and no `window`/`document`/storage access
	 *    (ADR §5 rule 4): safe to evaluate per SSR request; instances share
	 *    nothing between requests or windows.
	 *
	 * Tokens come only from ui/tokens.css; no dependency is added (ADR §4.3).
	 */
</script>

<script lang="ts">
	import { untrack } from 'svelte';
	import IngestKeysSection from '../components/IngestKeysSection.svelte';
	import Button from '../ui/Button.svelte';
	import Chip from '../ui/Chip.svelte';
	import SearchFilterInput from '../ui/SearchFilterInput.svelte';
	import Select from '../ui/Select.svelte';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import type {
		AlertOrigin,
		AlertView,
		MarkAllReadResult,
		NotificationFilterQuery,
		NotificationsApi,
		ProfileResponse
	} from '../workspace/entries/14-web-notifications.js';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readApi(services: PanelServices): NotificationsApi | null {
		const candidate = services.api as Partial<NotificationsApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (
			const name of [
				'listInboxNotifications',
				'getUnreadNotificationCount',
				'acknowledgeInboxNotification',
				'unacknowledgeInboxNotification',
				'markAllNotificationsRead',
				'subscribeNotificationEvents',
				'listProfiles',
				'listIngestKeys',
				'createIngestKey',
				'revokeIngestKey'
			] as const
		) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as NotificationsApi;
	}

	const api = $derived(readApi(panel.services));
	const usable = $derived(api !== null);

	// --- producer bounds mirrored for display only (server re-validates) -----

	/** Bounded initial/load-more page size (AGENTS.md invariant; route default 50). */
	const PAGE_LIMIT = 50;
	/** Debounce for the durable refresh triggered by wakeups. */
	const REFRESH_DEBOUNCE_MS = 150;
	/** Debounce for the committed server-side search term (`q`). */
	const SEARCH_DEBOUNCE_MS = 250;
	/** Fixed reconnect delay after a stream failure (bounded, visible). */
	const BACKOFF_MS = 750;

	// --- value-free display helpers (C08 rule 6) -----------------------------

	type ListState =
		| { status: 'loading' }
		| { status: 'error'; code: string; message: string }
		| { status: 'ready' };

	let disposed = true;

	function codeOf(error: unknown): string | null {
		if (typeof error !== 'object' || error === null || !('code' in error)) return null;
		return typeof error.code === 'string' ? error.code : null;
	}

	function messageOf(error: unknown): string | null {
		return error instanceof Error && error.message ? error.message : null;
	}

	/** Stable, display-safe text for the frozen C01 codes (no raw payloads). */
	function friendlyText(code: string, fallback: string): string {
		if (code === 'unauthenticated') return 'Your session has ended — sign in again.';
		if (code === 'throttled') return 'Too many requests — try again shortly.';
		if (code === 'not_found') return 'Nothing is stored under this id.';
		if (code === 'unavailable')
			return 'The notification service is unavailable right now; nothing was changed.';
		return fallback;
	}

	/** Severity dot class: color is always paired with the text label. */
	function severityClass(severity: string): string {
		if (severity === 'critical') return 'mai-notifications__dot--critical';
		if (severity === 'warning') return 'mai-notifications__dot--warning';
		if (severity === 'info') return 'mai-notifications__dot--info';
		return 'mai-notifications__dot--unknown';
	}

	// --- canonical inbox filters (R4-F2; ONE query for list/count/mark-all) --

	/**
	 * The filter toolbar state. `server_id` and `scope` are mutually
	 * exclusive BY CONSTRUCTION (the branches below never both fire) and
	 * `include_global` travels only beside an explicit `server_id` (R4-F2);
	 * `unread`, `origin` and `q` are omitted — never null — when their
	 * control sits at «All»/empty. The derived object's IDENTITY is the
	 * race guard's filter snapshot: it changes only when a control changes.
	 */
	/** The tab seam (nbi-fe-sources): Inbox is the default so the inbox
	 * loads exactly as before; Sources mounts the ingest-key section. */
	let activeTab = $state<'inbox' | 'sources'>('inbox');

	type ReadChoice = 'all' | 'unread' | 'read';
	type OriginChoice = 'all' | AlertOrigin;

	const READ_OPTIONS: ReadonlyArray<{ value: ReadChoice; label: string }> = [
		{ value: 'all', label: 'All' },
		{ value: 'unread', label: 'Unread' },
		{ value: 'read', label: 'Read' }
	];

	const ORIGIN_OPTIONS: ReadonlyArray<{ value: OriginChoice; label: string }> = [
		{ value: 'all', label: 'All' },
		{ value: 'external', label: 'External' },
		{ value: 'finding', label: 'Finding' },
		{ value: 'job_result', label: 'Job result' }
	];

	/** 'all' | 'global' | enrolled server_id (the Select's option values). */
	let scopeChoice = $state<string>('all');
	let includeGlobal = $state(true);
	let readChoice = $state<ReadChoice>('all');
	let originChoice = $state<OriginChoice>('all');
	/** Raw input text, bound to the SearchFilterInput primitive. */
	let searchInput = $state('');
	/** The committed (debounced, trimmed) `q` — the only value the wire sees. */
	let searchQuery = $state('');

	const filterQuery = $derived.by<NotificationFilterQuery>(() => {
		const query: NotificationFilterQuery = {};
		if (scopeChoice === 'global') {
			query.scope = 'global';
		} else if (scopeChoice !== 'all') {
			query.server_id = scopeChoice;
			query.include_global = includeGlobal;
		}
		if (readChoice === 'unread') query.unread = true;
		else if (readChoice === 'read') query.unread = false;
		if (originChoice !== 'all') query.origin = originChoice;
		if (searchQuery !== '') query.q = searchQuery;
		return query;
	});

	/**
	 * Debounced search commit: every keystroke restarts the timer, only the
	 * settled text becomes `q`. Whitespace never commits (trimmed), so a
	 * no-op edit never triggers a server refetch. The effect teardown clears
	 * the pending timer on unmount.
	 */
	$effect(() => {
		const text = searchInput;
		if (text === searchQuery) return;
		const timer = setTimeout(() => {
			const committed = text.trim();
			if (committed !== searchQuery) {
				searchQuery = committed;
				applyFilters();
			}
		}, SEARCH_DEBOUNCE_MS);
		return () => clearTimeout(timer);
	});

	/**
	 * Any filter change: drop the previous mark-all receipt (it described a
	 * previous query's effect) and refetch the durable page + count from
	 * offset 0. `total`/`has_more` are taken from the fresh envelopes only.
	 */
	function applyFilters(): void {
		markAllReceipt = null;
		void refreshAll();
	}

	function setScope(value: string): void {
		if (value === scopeChoice) return;
		scopeChoice = value;
		// The toggle is meaningless outside an explicit server scope; the
		// default (true) is restored so a stale false never leaks into a
		// later server selection.
		if (value === 'all' || value === 'global') includeGlobal = true;
		applyFilters();
	}

	function setReadState(value: string): void {
		const next = READ_OPTIONS.find((option) => option.value === value)?.value ?? 'all';
		if (next === readChoice) return;
		readChoice = next;
		applyFilters();
	}

	function setOrigin(value: string): void {
		const next = ORIGIN_OPTIONS.find((option) => option.value === value)?.value ?? 'all';
		if (next === originChoice) return;
		originChoice = next;
		applyFilters();
	}

	function setIncludeGlobal(checked: boolean): void {
		if (checked === includeGlobal) return;
		includeGlobal = checked;
		applyFilters();
	}

	// --- the inbox page (bounded, deduplicated, honest) -----------------------

	let alerts = $state<AlertView[]>([]);
	let total = $state(0);
	let hasMore = $state(false);
	let loadingMore = $state(false);
	let listState = $state<ListState>({ status: 'loading' });
	let listEpoch = 0;
	/** The last non-initial failure (load more / toggles); the list itself stays. */
	let actionProblem = $state<{ code: string; message: string } | null>(null);

	async function loadList(): Promise<void> {
		if (api === null) return;
		// Snapshot of the filters riding THIS request: the response may only
		// land while both the epoch and the filter snapshot still match (the
		// OverviewPanel race guard) — a stale in-flight page never renders.
		const filters = filterQuery;
		const epoch = ++listEpoch;
		listState = { status: 'loading' };
		loadingMore = false;
		try {
			const page = await api.listInboxNotifications({ ...filters, limit: PAGE_LIMIT, offset: 0 });
			if (disposed || epoch !== listEpoch || filters !== filterQuery) return;
			alerts = page.alerts;
			total = page.total;
			// The producer's envelope flag is the only has-more truth.
			hasMore = page.has_more;
			listState = { status: 'ready' };
		} catch (error) {
			if (disposed || epoch !== listEpoch || filters !== filterQuery) return;
			const code = codeOf(error) ?? 'unavailable';
			alerts = [];
			total = 0;
			hasMore = false;
			listState = {
				status: 'error',
				code,
				message: friendlyText(code, messageOf(error) ?? 'The notification inbox could not be read.')
			};
		}
	}

	async function loadMore(): Promise<void> {
		if (api === null || listState.status !== 'ready' || loadingMore || !hasMore) return;
		// The active filters ride every page fetch; the epoch is NOT bumped —
		// a filter change during the flight invalidates this page instead.
		const filters = filterQuery;
		const epoch = listEpoch;
		loadingMore = true;
		actionProblem = null;
		try {
			const page = await api.listInboxNotifications({
				...filters,
				limit: PAGE_LIMIT,
				offset: alerts.length
			});
			if (disposed || epoch !== listEpoch || filters !== filterQuery) return;
			// Deduplicate by alert_id: an overlapping page never duplicates rows.
			const seen = new Set(alerts.map((row) => row.alert_id));
			const appended = page.alerts.filter((row) => !seen.has(row.alert_id));
			alerts = [...alerts, ...appended];
			total = page.total;
			hasMore = page.has_more;
		} catch (error) {
			if (disposed || epoch !== listEpoch) return;
			const code = codeOf(error) ?? 'unavailable';
			actionProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'More notices could not be read.')
			};
		} finally {
			if (!disposed) loadingMore = false;
		}
	}

	// --- unread count (badge/drawer truth under the same canonical filters) --

	let unreadTotal = $state<number | null>(null);
	let unreadUnavailable = $state(false);
	/** Separate epoch: list and count refreshes never invalidate each other. */
	let countEpoch = 0;

	async function loadUnread(): Promise<void> {
		if (api === null) return;
		const filters = filterQuery;
		const epoch = ++countEpoch;
		try {
			const view = await api.getUnreadNotificationCount(filters);
			if (disposed || epoch !== countEpoch || filters !== filterQuery) return;
			unreadTotal = view.total;
			unreadUnavailable = false;
		} catch {
			if (disposed || epoch !== countEpoch || filters !== filterQuery) return;
			// Honest: the count is hidden as unavailable, never fabricated stale.
			unreadUnavailable = true;
		}
	}

	// --- server display names (listProfiles; clean fallback to the raw id) ----

	let profiles = $state<ProfileResponse[]>([]);

	async function loadProfiles(): Promise<void> {
		if (api === null) return;
		try {
			const listing = await api.listProfiles();
			if (disposed) return;
			profiles = listing.servers;
		} catch {
			if (disposed) return;
			// Fallback stays clean: unknown names render as the raw server_id.
			profiles = [];
		}
	}

	const serverNames = $derived.by(() => {
		const names = new Map<string, string>();
		for (const profile of profiles) names.set(profile.server_id, profile.name);
		return names;
	});

	/** Enrolled servers for the scope Select — deterministic (name, id) order. */
	const enrolledServers = $derived(
		[...profiles].sort((a, b) => {
			if (a.name !== b.name) return a.name < b.name ? -1 : 1;
			if (a.server_id !== b.server_id) return a.server_id < b.server_id ? -1 : 1;
			return 0;
		})
	);

	function displayName(serverId: string | null): string {
		if (serverId === null) return 'Global';
		return serverNames.get(serverId) ?? serverId;
	}

	// --- page-bound grouping (R4-F1: LOADED page only, first-appearance order)

	type NoticeGroup = { serverId: string | null; rows: AlertView[] };

	const groups = $derived.by<NoticeGroup[]>(() => {
		const order: NoticeGroup[] = [];
		const byServer = new Map<string, NoticeGroup>();
		for (const row of alerts) {
			const key = row.server_id ?? '';
			let group = byServer.get(key);
			if (group === undefined) {
				group = { serverId: row.server_id, rows: [] };
				byServer.set(key, group);
				order.push(group);
			}
			group.rows.push(row);
		}
		return order;
	});

	// --- receipt-driven read/unread toggling (never optimistic) ---------------

	let ackBusyId = $state<string | null>(null);
	let expandedId = $state<string | null>(null);

	async function toggleRead(row: AlertView): Promise<void> {
		if (api === null || ackBusyId !== null) return;
		ackBusyId = row.alert_id;
		actionProblem = null;
		try {
			// One idempotent stamp; the ROW updates ONLY to the receipt.
			const receipt = row.acked
				? await api.unacknowledgeInboxNotification(row.alert_id)
				: await api.acknowledgeInboxNotification(row.alert_id);
			if (disposed) return;
			if (receipt.alert.alert_id !== row.alert_id) {
				actionProblem = {
					code: 'scope_mismatch',
					message: 'The stamp answer names a different notice; the row was left unchanged.'
				};
				return;
			}
			alerts = alerts.map((candidate) =>
				candidate.alert_id === receipt.alert.alert_id ? receipt.alert : candidate
			);
			void loadUnread();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			actionProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The notice state could not be changed.')
			};
		} finally {
			if (!disposed) ackBusyId = null;
		}
	}

	function toggleBody(alertId: string): void {
		expandedId = expandedId === alertId ? null : alertId;
	}

	// --- mark-all-read under the EXACT current filters (receipt-driven) ------

	let markAllBusy = $state(false);
	/** The route's own rowcount receipt; cleared by any filter change. */
	let markAllReceipt = $state<MarkAllReadResult | null>(null);

	async function markAllRead(): Promise<void> {
		if (api === null || markAllBusy) return;
		// The SAME canonical query snapshot the list/count reads use.
		const filters = filterQuery;
		markAllBusy = true;
		actionProblem = null;
		try {
			const receipt = await api.markAllNotificationsRead(filters);
			if (disposed) return;
			// Never optimistic: the receipt renders, then the list + unread
			// count refetch from the durable store (under the CURRENT filters).
			markAllReceipt = receipt;
			await Promise.allSettled([loadList(), loadUnread()]);
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			actionProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The notices could not be marked read.')
			};
		} finally {
			if (!disposed) markAllBusy = false;
		}
	}

	// --- live wakeup signal (identity frames; refetch is the only reaction) --

	let live = $state<'connecting' | 'live' | 'offline'>('connecting');

	/** Non-reactive controller locals: the async loop reads these directly. */
	let streamAbort: AbortController | null = null;
	let refreshTimer: ReturnType<typeof setTimeout> | null = null;

	/** Debounced durable refresh; wakeups are the signal, the pages the truth. */
	function scheduleRefresh(): void {
		if (disposed || refreshTimer !== null) return;
		refreshTimer = setTimeout(() => {
			refreshTimer = null;
			void refreshAll();
		}, REFRESH_DEBOUNCE_MS);
	}

	async function refreshAll(): Promise<void> {
		await Promise.allSettled([loadList(), loadUnread()]);
	}

	/** Abortable sleep: the reconnect backoff releases early on teardown. */
	function sleep(ms: number, signal: AbortSignal): Promise<void> {
		const { promise, resolve } = Promise.withResolvers<void>();
		const timer = setTimeout(resolve, ms);
		signal.addEventListener(
			'abort',
			() => {
				clearTimeout(timer);
				resolve();
			},
			{ once: true }
		);
		return promise;
	}

	/**
	 * The wakeup stream loop (instance-scoped): subscribes, treats every
	 * identity frame as a refetch signal — no frame field is ever read —
	 * and survives failures with a small fixed backoff. The stream never
	 * submits anything.
	 */
	async function liveLoop(): Promise<void> {
		if (api === null) return;
		const controller = new AbortController();
		streamAbort = controller;
		while (!disposed && !controller.signal.aborted) {
			live = 'connecting';
			try {
				await api.subscribeNotificationEvents({
					signal: controller.signal,
					// The frame carries identity only; the panel reads NOTHING
					// from it — the mere arrival schedules a durable refetch.
					onWakeup: () => {
						if (disposed || controller.signal.aborted) return;
						live = 'live';
						scheduleRefresh();
					},
					onStreamEnd: () => {
						if (disposed || controller.signal.aborted) return;
						// The bounded stream self-closed: refetch now, the loop
						// resubscribes after its backoff.
						scheduleRefresh();
					}
				});
			} catch {
				if (disposed || controller.signal.aborted) return;
				live = 'offline';
			}
			if (disposed || controller.signal.aborted) return;
			await sleep(BACKOFF_MS, controller.signal);
		}
	}

	// --- mount cycle (the $effect teardown is the release path) ---------------

	$effect(() => {
		if (!usable) return;
		disposed = false;
		// The reads start untracked: loadList/loadUnread read the canonical
		// filter query, and this effect must depend ONLY on `usable` — never
		// re-run (teardown + refetch + resubscribe) from a filter change.
		untrack(() => {
			void refreshAll();
			void loadProfiles();
		});
		// The loop reads no reactive state; started untracked so the effect
		// depends only on `usable` and never re-runs from its own refreshes.
		untrack(() => void liveLoop());
		return () => {
			disposed = true;
			streamAbort?.abort();
			streamAbort = null;
			if (refreshTimer !== null) {
				clearTimeout(refreshTimer);
				refreshTimer = null;
			}
		};
	});

	const liveText = $derived(
		live === 'live'
			? 'live updates on — new notices trigger a refetch'
			: live === 'connecting'
				? 'connecting to the live stream…'
				: 'live updates offline — showing the last loaded page; reconnecting automatically'
	);
</script>

<div class="mai-notifications" data-testid="notifications-root" data-live={live}>
	{#if !usable}
		<div
			class="mai-notifications__state mai-notifications__state--error"
			role="alert"
			data-testid="notifications-config-error"
		>
			<p>This panel was opened without its notifications service.</p>
		</div>
	{:else}
		<header class="mai-notifications__header">
			<h3 class="mai-notifications__title">Notifications</h3>
			<p class="mai-notifications__meta">
				Fleet-wide inbox — grouped by server over the loaded page only.
				<span data-testid="unread-count">
					{#if unreadUnavailable}
						Unread: unavailable
					{:else}
						Unread: {unreadTotal ?? '…'}
					{/if}
				</span>
			</p>
			<p class="mai-notifications__live" data-testid="live-status" data-live={live}>
				Live: {liveText}
			</p>
			<div class="mai-notifications__head-actions">
				<Button variant="secondary" size="sm" onclick={refreshAll} data-testid="notifications-refresh">
					Refresh
				</Button>
			</div>
		</header>

		<!-- Tab seam (nbi-fe-sources): Inbox is the default tab, Sources mounts
			the ingest-key section (api bag + loaded profiles). The inbox markup
			below is the {:else} arm of this strip and stays tab-content-agnostic;
			its state survives tab switches and re-renders without a refetch. -->
		<div class="mai-notifications__tabs" role="tablist" aria-label="Notification surfaces">
			<button
				type="button"
				role="tab"
				aria-selected={activeTab === 'inbox'}
				class="mai-notifications__tab"
				class:mai-notifications__tab--active={activeTab === 'inbox'}
				onclick={() => (activeTab = 'inbox')}
				data-testid="tab-inbox"
			>
				Inbox
			</button>
			<button
				type="button"
				role="tab"
				aria-selected={activeTab === 'sources'}
				class="mai-notifications__tab"
				class:mai-notifications__tab--active={activeTab === 'sources'}
				onclick={() => (activeTab = 'sources')}
				data-testid="tab-sources"
			>
				Sources
			</button>
		</div>
		{#if activeTab === 'sources'}
			<IngestKeysSection {api} profiles={profiles} />
		{:else}
		<div class="mai-notifications__toolbar" role="group" aria-label="Inbox filters">
			<SearchFilterInput
				bind:value={searchInput}
				label="Search notices"
				placeholder="Search title, summary, body, source…"
				resultCount={listState.status === 'ready' ? alerts.length : undefined}
				totalCount={listState.status === 'ready' ? total : undefined}
				testId="notifications-search"
			/>
			<Select
				label="Server"
				hideLabel
				compact
				value={scopeChoice}
				onchange={(event) => setScope(event.currentTarget.value)}
				data-testid="filter-scope"
			>
				<option value="all">All servers</option>
				<option value="global">Global only</option>
				{#each enrolledServers as profile (profile.server_id)}
					<option value={profile.server_id}>{profile.name}</option>
				{/each}
			</Select>
			{#if scopeChoice !== 'all' && scopeChoice !== 'global'}
				<label class="mai-notifications__toggle">
					<input
						type="checkbox"
						checked={includeGlobal}
						onchange={(event) => setIncludeGlobal(event.currentTarget.checked)}
						data-testid="filter-include-global"
					/>
					include global
				</label>
			{/if}
			<Select
				label="Read state"
				hideLabel
				compact
				value={readChoice}
				onchange={(event) => setReadState(event.currentTarget.value)}
				data-testid="filter-read"
			>
				{#each READ_OPTIONS as option (option.value)}
					<option value={option.value}>{option.label}</option>
				{/each}
			</Select>
			<div class="mai-notifications__chips" role="group" aria-label="Origin filter">
				{#each ORIGIN_OPTIONS as option (option.value)}
					<Chip
						variant={originChoice === option.value ? 'brass' : 'default'}
						role="button"
						tabindex={0}
						aria-pressed={originChoice === option.value ? 'true' : 'false'}
						onclick={() => setOrigin(option.value)}
						onkeydown={(event) => {
							if (event.key === 'Enter' || event.key === ' ') {
								event.preventDefault();
								setOrigin(option.value);
							}
						}}
						data-testid={'filter-origin-' + option.value}
					>
						{option.label}
					</Chip>
				{/each}
			</div>
			<Button
				variant="secondary"
				size="sm"
				loading={markAllBusy}
				onclick={markAllRead}
				data-testid="notifications-mark-all-read"
			>
				Mark all read
			</Button>
		</div>

		{#if markAllReceipt !== null}
			<p class="mai-notifications__receipt" role="status" data-testid="mark-all-receipt">
				Marked {markAllReceipt.updated} read under the current filters.
			</p>
		{/if}

		{#if actionProblem !== null}
			<div
				class="mai-notifications__state mai-notifications__state--error"
				role="alert"
				data-testid="notifications-problem"
				data-error-code={actionProblem.code}
			>
				<p>{actionProblem.message}</p>
			</div>
		{/if}

		{#if listState.status === 'loading' && alerts.length === 0}
			<div class="mai-notifications__state" role="status" data-testid="notifications-loading">
				<p>Loading the inbox…</p>
			</div>
		{:else if listState.status === 'error'}
			<div
				class="mai-notifications__state mai-notifications__state--error"
				role="alert"
				data-testid="notifications-error"
				data-error-code={listState.code}
			>
				<p>{listState.message}</p>
				<Button variant="secondary" onclick={loadList} data-testid="notifications-retry">
					Retry
				</Button>
			</div>
		{:else if alerts.length === 0}
			<div class="mai-notifications__state" role="status" data-testid="notifications-empty">
				<p>
					No notices yet. External services publish into this inbox with a machine ingest
					key — <code>POST /api/v1/notifications/ingest</code> with a Bearer key (the
					canonical example lives in docs/architecture/notification-inbox.md; keys are
					minted in the Sources tab).
				</p>
			</div>
		{:else}
			<div class="mai-notifications__scroll">
				{#each groups as group (group.serverId ?? '')}
					<section
						class="mai-notifications__group"
						aria-label="Notices from {displayName(group.serverId)}"
						data-testid="notice-group"
						data-server={group.serverId ?? ''}
					>
						<h4 class="mai-notifications__group-head" data-testid="group-heading">
							{displayName(group.serverId)} — {group.rows.length} loaded
						</h4>
						<ul class="mai-notifications__rows">
							{#each group.rows as row (row.alert_id)}
								<li
									class="mai-notifications__row"
									class:mai-notifications__row--read={row.acked}
									data-testid="notice-row"
									data-alert-id={row.alert_id}
									data-acked={row.acked ? 'true' : 'false'}
								>
									<div class="mai-notifications__row-main">
										<span
											class="mai-notifications__dot {severityClass(row.severity)}"
											aria-hidden="true"
											data-testid="row-severity"
											data-severity={row.severity}
										></span>
										<span class="mai-notifications__sevlabel" data-testid="row-severity-label">
											{row.severity}
										</span>
										<span class="mai-notifications__label" data-testid="row-title">{row.title}</span>
										<span class="mai-notifications__chip" data-testid="row-source">
											{row.source ?? row.origin}
										</span>
										<span class="mai-notifications__fact" data-testid="row-time">{row.created_at}</span>
										<span class="mai-notifications__fact" data-testid="row-readstate">
											{row.acked ? 'read' : 'unread'}
										</span>
										<Button
											variant="secondary"
											size="sm"
											loading={ackBusyId === row.alert_id}
											onclick={() => toggleRead(row)}
											data-testid="row-toggle"
										>
											{row.acked ? 'Mark unread' : 'Mark read'}
										</Button>
										{#if row.body !== null}
											<Button
												variant="ghost"
												size="sm"
												onclick={() => toggleBody(row.alert_id)}
												aria-expanded={expandedId === row.alert_id}
												data-testid="row-expand"
											>
												{expandedId === row.alert_id ? 'Hide details' : 'Details'}
											</Button>
										{/if}
									</div>
									{#if row.summary !== null && expandedId !== row.alert_id}
										<p class="mai-notifications__summary" data-testid="row-summary">{row.summary}</p>
									{/if}
									{#if row.body !== null && expandedId === row.alert_id}
										<!-- Plain text only: Svelte escapes every producer string. -->
										<p class="mai-notifications__body" data-testid="row-body">{row.body}</p>
									{/if}
								</li>
							{/each}
						</ul>
					</section>
				{/each}
			</div>
			{#if hasMore}
				<div class="mai-notifications__head-actions">
					<Button
						variant="secondary"
						size="sm"
						loading={loadingMore}
						onclick={loadMore}
						data-testid="notifications-load-more"
					>
						Load more ({alerts.length} of {total})
					</Button>
				</div>
			{:else}
				<p class="mai-notifications__meta" data-testid="notifications-page-count">
					{alerts.length} of {total} notices loaded.
				</p>
			{/if}
		{/if}
		{/if}
	{/if}
</div>

<style>
	.mai-notifications {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
		min-width: 0;
		font-size: var(--mai-font-size-sm, 0.875rem);
	}

	.mai-notifications__header {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 0.25rem);
		margin: 0;
	}

	.mai-notifications__title {
		margin: 0;
		font-size: var(--mai-font-size-md, 1rem);
	}

	.mai-notifications__meta {
		margin: 0;
		color: var(--mai-color-text-muted);
	}

	.mai-notifications__live {
		margin: 0;
		color: var(--mai-color-text-muted);
	}

	.mai-notifications__live[data-live='offline'] {
		color: var(--mai-status-warning-text);
	}

	.mai-notifications__head-actions {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	/* Tab seam (nbi-fe-sources): Inbox / Sources strip under the header. */
	.mai-notifications__tabs {
		display: inline-flex;
		gap: var(--mai-space-1, 0.25rem);
		border-bottom: 1px solid var(--mai-color-border);
	}

	.mai-notifications__tab {
		appearance: none;
		background: none;
		border: none;
		border-bottom: 2px solid transparent;
		cursor: pointer;
		font: inherit;
		color: var(--mai-color-text-muted);
		padding: var(--mai-space-1, 0.25rem) var(--mai-space-2, 0.5rem);
	}

	.mai-notifications__tab--active {
		color: var(--mai-color-text);
		border-bottom-color: var(--mai-color-brass, var(--mai-color-text));
	}

	/* Filter toolbar: search grows, the controls wrap on narrow windows. */
	.mai-notifications__toolbar {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
	}

	.mai-notifications__toolbar :global(.mai-search-filter) {
		flex: 1 1 12rem;
		min-width: 10rem;
	}

	.mai-notifications__chips {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-1, 0.25rem);
		flex-wrap: wrap;
	}

	/* Origin chips are buttons for every input modality. */
	.mai-notifications__chips :global(.mai-chip[role='button']) {
		cursor: pointer;
		user-select: none;
	}

	.mai-notifications__toggle {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-1, 0.25rem);
		color: var(--mai-color-text-muted);
	}

	.mai-notifications__receipt {
		margin: 0;
		color: var(--mai-status-success-text);
	}

	/* Bounded scroll container: the loaded page grows INSIDE this box. */
	.mai-notifications__scroll {
		overflow-y: auto;
		max-height: 26rem;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
		padding-right: 2px;
		min-height: 0;
	}

	.mai-notifications__group {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 0.25rem);
	}

	.mai-notifications__group-head {
		margin: 0;
		font-size: var(--mai-font-size-sm, 0.875rem);
		color: var(--mai-color-text-muted);
	}

	.mai-notifications__rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 0.25rem);
	}

	.mai-notifications__row {
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm, 0.25rem);
		background: var(--mai-color-surface);
		min-width: 0;
		padding: var(--mai-space-1, 0.25rem) var(--mai-space-2, 0.5rem);
	}

	.mai-notifications__row--read {
		opacity: 0.72;
	}

	.mai-notifications__row-main {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
	}

	.mai-notifications__dot {
		width: 0.6em;
		height: 0.6em;
		border-radius: 50%;
		flex: none;
	}

	.mai-notifications__dot--critical {
		background: var(--mai-status-danger-border);
	}

	.mai-notifications__dot--warning {
		background: var(--mai-status-warning-border);
	}

	.mai-notifications__dot--info {
		background: var(--mai-status-info-border);
	}

	.mai-notifications__dot--unknown {
		background: var(--mai-color-text-muted);
	}

	.mai-notifications__sevlabel {
		font-size: var(--mai-font-size-xs, 0.75rem);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.mai-notifications__label {
		font-weight: 600;
		overflow-wrap: anywhere;
	}

	.mai-notifications__row--read .mai-notifications__label {
		font-weight: 400;
	}

	.mai-notifications__chip {
		display: inline-block;
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm, 0.25rem);
		padding: 0 var(--mai-space-2, 0.5rem);
		font-size: var(--mai-font-size-xs, 0.75rem);
		white-space: nowrap;
		color: var(--mai-color-brass, var(--mai-color-text));
	}

	.mai-notifications__fact {
		color: var(--mai-color-text-muted);
		overflow-wrap: anywhere;
	}

	.mai-notifications__summary {
		margin: var(--mai-space-1, 0.25rem) 0 0;
		color: var(--mai-color-text-muted);
		overflow-wrap: anywhere;
	}

	.mai-notifications__body {
		margin: var(--mai-space-1, 0.25rem) 0 0;
		border-top: 1px solid var(--mai-color-border);
		padding-top: var(--mai-space-1, 0.25rem);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}

	.mai-notifications__state {
		padding: var(--mai-space-2, 0.5rem);
		border-radius: var(--mai-radius-sm, 0.25rem);
		background: var(--mai-color-bg);
		color: var(--mai-color-text-muted);
	}

	.mai-notifications__state p {
		margin: 0 0 var(--mai-space-1, 0.25rem);
	}

	.mai-notifications__state--error {
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
		border: 1px solid var(--mai-status-danger-border);
	}
</style>
