<script module lang="ts">
	/**
	 * NotificationsPanel — the fleet-wide unified notification inbox
	 * (campaign `notification-inbox`, `nbi-fe-panel`; contracts
	 * docs/architecture/notification-inbox.md, AGENTS.md pagination invariant,
	 * C08 rule 6, C11).
	 *
	 * Layer: reusable feature panel (windowed-workspace ADR-0001 §2 layer
	 * table). It owns NO window mechanics and NO scope selection:
	 *
	 *  - GLOBAL window, fleet-wide reads: every read is the reviewed global
	 *    route under the canonical filters (initially ALL notices — the
	 *    filter toolbar is the sibling task's surface; a server filter is an
	 *    explicit query param inside the panel, never a pinned scope, C01
	 *    rule 2). The panel reads `panel.services.api` only; an absent or
	 *    incomplete bag fails closed with a visible state (no locator).
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
	import Button from '../ui/Button.svelte';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import type { AlertView, NotificationsApi, ProfileResponse } from '../workspace/entries/14-web-notifications.js';

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
		const epoch = ++listEpoch;
		listState = { status: 'loading' };
		loadingMore = false;
		try {
			const page = await api.listInboxNotifications({ limit: PAGE_LIMIT, offset: 0 });
			if (disposed || epoch !== listEpoch) return;
			alerts = page.alerts;
			total = page.total;
			// The producer's envelope flag is the only has-more truth.
			hasMore = page.has_more;
			listState = { status: 'ready' };
		} catch (error) {
			if (disposed || epoch !== listEpoch) return;
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
		const epoch = listEpoch;
		loadingMore = true;
		actionProblem = null;
		try {
			const page = await api.listInboxNotifications({ limit: PAGE_LIMIT, offset: alerts.length });
			if (disposed || epoch !== listEpoch) return;
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

	async function loadUnread(): Promise<void> {
		if (api === null) return;
		try {
			const view = await api.getUnreadNotificationCount({});
			if (disposed) return;
			unreadTotal = view.total;
			unreadUnavailable = false;
		} catch {
			if (disposed) return;
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
		void refreshAll();
		void loadProfiles();
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
