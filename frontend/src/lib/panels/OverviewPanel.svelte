<script module lang="ts">
	/**
	 * OverviewPanel — server attention dashboard (card `mai-10-web-dashboard`,
	 * P10; contracts C10 rules 3/4, C08 rule 6, C11).
	 *
	 * Layer: reusable feature panel (windowed-workspace ADR-0001 §2 layer
	 * table). It owns NO window mechanics and NO collector/detail logic:
	 *
	 *  - Explicit context only: a SERVER-scoped attention view. Every read
	 *    names the target pinned at open time (`panel.scope.server_id`, C01
	 *    rule 2); the module reads no launcher/current-server value anywhere,
	 *    so there is no hidden global mutation target and a launcher
	 *    re-selection can never retarget an ack, an evaluation or a refresh
	 *    (C08 rule 5, NT5). An optional pinned `run` object selects the ONE
	 *    reviewed event stream to watch (C08 rule 7).
	 *  - Explicit services only: the eight-function `OverviewApi` plus the
	 *    optional links/chat seams enter through `panel.services` (the typed
	 *    bag owned by the feature entry module). Missing/incomplete bags fail
	 *    closed with a visible state — no service locator, no fallback.
	 *  - Summary, not duplication (acceptance 2): freshness comes from the
	 *    producer's request-time derivation (`ObservationView.freshness`);
	 *    finding/inbox/app/run pages are the reviewed producers' own bounded
	 *    queries; the only detail rendering is the reviewed `10-web-evidence`
	 *    `ObservationDetails` drawer, which stays the single detail surface.
	 *    Counts computed here are labeled page-scoped ("latest page"), never
	 *    presented as global collector truth.
	 *  - Honest mutations: acknowledging a notice renders exactly the
	 *    idempotent stamp the API answered (`AckResult.alert` — the row
	 *    flips to acknowledged, source evidence untouched); re-evaluation
	 *    renders the evaluator's own `EvaluateResult` ONLY for the pinned
	 *    server (a cached round of another scope can never display). No
	 *    optimistic success anywhere (C08 rule 6).
	 *  - Live updates are a change SIGNAL, never a data source (the
	 *    JobsPanel contract): delivered frames deduplicate by the per-run
	 *    monotonic `seq` watermark and schedule a debounced refresh of the
	 *    durable reads. The stream is read-only; hide/close/unmount releases
	 *    it (the `$effect` teardown is the release path) and never cancels
	 *    or retargets backend work (windowed ADR §7.2).
	 *  - No module-level state and no `window`/`document`/storage access
	 *    (ADR §5 rule 4): safe to evaluate per SSR request; instances share
	 *    nothing between requests or windows. No `{@html}` anywhere — every
	 *    producer string renders as escaped text.
	 *
	 * Tokens come only from ui/tokens.css; no dependency is added (ADR §4.3).
	 */
	import { untrack } from 'svelte';
	import ObservationDetails from '../components/ObservationDetails.svelte';
	import Button from '../ui/Button.svelte';
	import { readMaiEvent } from '../api/client.js';
	import { DEFAULT_SERVICE_CATEGORIES } from '../workspace/entries/08-web-apps.js';
	import type { ServiceCategoryView } from '../workspace/entries/08-web-apps.js';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import type {
		ApplicationPage,
		AlertView,
		EvidenceAttachPayload,
		FindingPage,
		OverviewApi,
		OverviewChat,
		OverviewLinks,
		RunPage,
		RunView
	} from '../workspace/entries/10-web-dashboard.js';
</script>

<script lang="ts">
	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readApi(services: PanelServices): OverviewApi | null {
		const candidate = services.api as Partial<OverviewApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (
			const name of [
				'listObservations',
				'listFindings',
				'evaluateFindings',
				'listNotifications',
				'ackNotification',
				'listAdoptedApplications',
				'listRuns',
				'streamRunEvents'
			] as const
		) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as OverviewApi;
	}

	/** Narrow the optional links seam; incomplete ⇒ null (fail closed). */
	function readLinks(services: PanelServices): OverviewLinks | null {
		const candidate = services.links as Partial<OverviewLinks> | null | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (const name of ['openApplications', 'openJobs'] as const) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as OverviewLinks;
	}

	/** Narrow the optional chat hand-off seam; incomplete ⇒ null (fail closed). */
	function readChat(services: PanelServices): OverviewChat | null {
		const candidate = services.chat as Partial<OverviewChat> | null | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		if (typeof candidate.attach !== 'function') return null;
		return candidate as OverviewChat;
	}

	const api = $derived(readApi(panel.services));
	const links = $derived(readLinks(panel.services));
	const chat = $derived(readChat(panel.services));
	/** Monitoring is a server attention view; a global mount is a misopen. */
	const serverScope = $derived(panel.scope.kind === 'server' ? panel.scope.server_id : null);
	/**
	 * Optional pinned run: ITS persisted event stream is the live signal.
	 * The stream carries no rendered state — frames only trigger refreshes
	 * of the durable reads below (C08 rule 7; JobsPanel contract).
	 */
	const pinnedRunId = $derived(
		panel.scope.kind === 'server' && panel.scope.object?.kind === 'run'
			? panel.scope.object.id
			: null
	);
	const usable = $derived(api !== null && serverScope !== null);

	// --- producer bounds mirrored for display only (server re-validates) -----

	/** Bounded attention page size for the three monitor pages (C01 rule 5). */
	const MONITOR_PAGE_LIMIT = 10;
	/** Bounded summary page size for applications/runs. */
	const SUMMARY_PAGE_LIMIT = 50;
	/** Debounce for the durable refresh triggered by delivered events. */
	const REFRESH_DEBOUNCE_MS = 150;
	/** Fixed reconnect delay after a stream close/failure (bounded, visible). */
	const BACKOFF_MS = 750;

	/** Run lifecycle states after which nothing new can be learned (C05 rule 2). */
	const TERMINAL_RUN_STATES: ReadonlySet<string> = new Set(['succeeded', 'failed', 'cancelled']);

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
		if (code === 'not_found') return 'Nothing is stored under this id and scope.';
		if (code === 'unavailable')
			return 'The monitoring service is unavailable right now; nothing was changed.';
		if (code === 'request_rejected') return 'The request was rejected; nothing was changed.';
		return fallback;
	}

	function shortId(id: string | null): string {
		return id === null || id === '' ? '—' : id.slice(0, 8);
	}

	function severityBadge(severity: string): string {
		if (severity === 'critical') return 'mai-overview__badge--danger';
		if (severity === 'warning') return 'mai-overview__badge--warn';
		return 'mai-overview__badge--neutral';
	}

	function freshnessBadge(freshness: string): string {
		if (freshness === 'fresh') return 'mai-overview__badge--ok';
		if (freshness === 'stale') return 'mai-overview__badge--warn';
		return 'mai-overview__badge--neutral';
	}

	function stateBadge(state: string | null): string {
		if (state === null) return 'mai-overview__badge--neutral';
		if (state === 'succeeded') return 'mai-overview__badge--ok';
		if (state === 'failed') return 'mai-overview__badge--danger';
		if (TERMINAL_RUN_STATES.has(state)) return 'mai-overview__badge--neutral';
		return 'mai-overview__badge--info';
	}

	function runStateText(state: string | null): string {
		return state ?? 'state not recorded';
	}

	// --- monitor freshness (the producer derives freshness at request time) --

	let observations = $state<ImportedObservation[]>([]);
	let observationsTotal = $state(0);
	let observationsState = $state<ListState>({ status: 'loading' });
	let observationsEpoch = 0;

	/** Local alias so the template stays short; the wire shape is unchanged. */
	type ImportedObservation = Awaited<ReturnType<OverviewApi['listObservations']>>['observations'][number];

	async function loadObservations(): Promise<void> {
		if (api === null || serverScope === null) return;
		const scope = serverScope;
		const epoch = ++observationsEpoch;
		observationsState = { status: 'loading' };
		try {
			const page = await api.listObservations(scope, { limit: MONITOR_PAGE_LIMIT });
			if (disposed || epoch !== observationsEpoch || scope !== serverScope) return;
			if (page.server_id !== scope) {
				observations = [];
				observationsTotal = 0;
				observationsState = {
					status: 'error',
					code: 'scope_mismatch',
					message: 'The answer names a different server than this window\'s pinned scope.'
				};
				return;
			}
			observations = page.observations;
			observationsTotal = page.total;
			observationsState = { status: 'ready' };
		} catch (error) {
			if (disposed || epoch !== observationsEpoch) return;
			const code = codeOf(error) ?? 'unavailable';
			observations = [];
			observationsTotal = 0;
			observationsState = {
				status: 'error',
				code,
				message: friendlyText(code, messageOf(error) ?? 'The observation page could not be read.')
			};
		}
	}

	/** Page-scoped freshness tally — the producer derived every value. */
	const freshnessCounts = $derived.by(() => {
		const counts = { fresh: 0, stale: 0, unknown: 0 };
		for (const row of observations) {
			if (row.freshness === 'fresh') counts.fresh += 1;
			else if (row.freshness === 'stale') counts.stale += 1;
			else counts.unknown += 1;
		}
		return counts;
	});

	/** Newest sample actually on the page (empty page ⇒ null, not a guess). */
	const newestObservedAt = $derived.by(() => {
		let newest: string | null = null;
		for (const row of observations) {
			if (row.collected_at !== null && (newest === null || row.collected_at > newest)) {
				newest = row.collected_at;
			}
		}
		return newest;
	});

	// --- findings attention + the re-evaluation action ------------------------

	let findings = $state<ImportedFinding[]>([]);
	let findingsTotal = $state(0);
	let findingsState = $state<ListState>({ status: 'loading' });
	let findingsEpoch = 0;

	type ImportedFinding = Awaited<ReturnType<OverviewApi['listFindings']>>['findings'][number];

	async function loadFindings(): Promise<void> {
		if (api === null || serverScope === null) return;
		const scope = serverScope;
		const epoch = ++findingsEpoch;
		findingsState = { status: 'loading' };
		try {
			const page: FindingPage = await api.listFindings(scope, {
				open: true,
				limit: MONITOR_PAGE_LIMIT
			});
			if (disposed || epoch !== findingsEpoch || scope !== serverScope) return;
			if (page.server_id !== scope) {
				findings = [];
				findingsTotal = 0;
				findingsState = {
					status: 'error',
					code: 'scope_mismatch',
					message: 'The answer names a different server than this window\'s pinned scope.'
				};
				return;
			}
			findings = page.findings;
			findingsTotal = page.total;
			findingsState = { status: 'ready' };
		} catch (error) {
			if (disposed || epoch !== findingsEpoch) return;
			const code = codeOf(error) ?? 'unavailable';
			findings = [];
			findingsTotal = 0;
			findingsState = {
				status: 'error',
				code,
				message: friendlyText(code, messageOf(error) ?? 'The finding page could not be read.')
			};
		}
	}

	/** Page-scoped severity tally over the open conditions on the page. */
	const severityCounts = $derived.by(() => {
		const counts = { critical: 0, warning: 0, info: 0, other: 0 };
		for (const row of findings) {
			if (row.severity === 'critical') counts.critical += 1;
			else if (row.severity === 'warning') counts.warning += 1;
			else if (row.severity === 'info') counts.info += 1;
			else counts.other += 1;
		}
		return counts;
	});

	let evaluateBusy = $state(false);
	/** The evaluator's own round — stored only for THIS server scope. */
	let evaluateResult = $state<ImportedEvaluateResult | null>(null);
	let evaluateProblem = $state<{ code: string; message: string } | null>(null);

	type ImportedEvaluateResult = Awaited<ReturnType<OverviewApi['evaluateFindings']>>;

	async function evaluate(): Promise<void> {
		if (api === null || serverScope === null || evaluateBusy) return;
		const scope = serverScope;
		evaluateBusy = true;
		evaluateProblem = null;
		try {
			const result = await api.evaluateFindings(scope);
			if (disposed || scope !== serverScope) return;
			// Only the pinned server's round may ever render (the drawer
			// re-checks; this guard keeps stale foreign rounds out of state).
			if (result.server_id !== scope) {
				evaluateProblem = {
					code: 'scope_mismatch',
					message: 'The evaluation answer names a different server than this window\'s pinned scope.'
				};
				return;
			}
			evaluateResult = result;
			void loadFindings();
			void loadInbox();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			evaluateProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The re-evaluation could not run.')
			};
		} finally {
			if (!disposed) evaluateBusy = false;
		}
	}

	// --- notification attention + the evidence-preserving ack ----------------

	let notices = $state<AlertView[]>([]);
	let noticesTotal = $state(0);
	let noticesState = $state<ListState>({ status: 'loading' });
	let noticesEpoch = 0;
	/** The one notice whose ack round trip is in flight. */
	let ackBusyId = $state<string | null>(null);
	let ackProblem = $state<{ code: string; message: string } | null>(null);

	async function loadInbox(): Promise<void> {
		if (api === null || serverScope === null) return;
		const scope = serverScope;
		const epoch = ++noticesEpoch;
		noticesState = { status: 'loading' };
		try {
			const page = await api.listNotifications(scope, {
				unacknowledged: true,
				limit: MONITOR_PAGE_LIMIT
			});
			if (disposed || epoch !== noticesEpoch || scope !== serverScope) return;
			if (page.server_id !== scope) {
				notices = [];
				noticesTotal = 0;
				noticesState = {
					status: 'error',
					code: 'scope_mismatch',
					message: 'The answer names a different server than this window\'s pinned scope.'
				};
				return;
			}
			notices = page.alerts;
			noticesTotal = page.total;
			noticesState = { status: 'ready' };
		} catch (error) {
			if (disposed || epoch !== noticesEpoch) return;
			const code = codeOf(error) ?? 'unavailable';
			notices = [];
			noticesTotal = 0;
			noticesState = {
				status: 'error',
				code,
				message: friendlyText(code, messageOf(error) ?? 'The notification inbox could not be read.')
			};
		}
	}

	async function acknowledge(notice: AlertView): Promise<void> {
		if (api === null || serverScope === null || ackBusyId !== null) return;
		const scope = serverScope;
		ackBusyId = notice.alert_id;
		ackProblem = null;
		try {
			const receipt = await api.ackNotification(scope, notice.alert_id);
			if (disposed || scope !== serverScope) return;
			if (receipt.alert.server_id !== scope || receipt.alert.alert_id !== notice.alert_id) {
				ackProblem = {
					code: 'scope_mismatch',
					message: 'The acknowledgment answer names a different server or notice.'
				};
				return;
			}
			// The stamp is the truth the API answered: the row flips to
			// acknowledged in place, then the attention page reloads (the
			// acked notice drops out of the unacknowledged set).
			notices = notices.map((row) => (row.alert_id === notice.alert_id ? receipt.alert : row));
			void loadInbox();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			ackProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The notice could not be acknowledged.')
			};
		} finally {
			if (!disposed) ackBusyId = null;
		}
	}

	// --- application / job state summaries (links, not duplication) ----------

	let applications = $state<ImportedApplication[]>([]);
	let applicationsTotal = $state(0);
	let applicationsState = $state<ListState>({ status: 'loading' });
	let applicationsEpoch = 0;
	/** The merged taxonomy riding the adopted page (omitted in old fixtures). */
	let applicationsCategories = $state<ServiceCategoryView[]>([]);

	/**
	 * The rendered taxonomy for the Service Map summary: the producer's page
	 * when carried, else the 8 client-side built-ins (shared contract).
	 */
	const serviceTaxonomy = $derived.by<ServiceCategoryView[]>(() =>
		applicationsCategories.length > 0
			? applicationsCategories
			: DEFAULT_SERVICE_CATEGORIES.map((seed) => ({
					machine_id: seed.machine_id,
					label: seed.label,
					description: seed.description,
					built_in: true
				}))
	);

	function serviceCategoryLabel(machineId: string): string {
		return serviceTaxonomy.find((entry) => entry.machine_id === machineId)?.label ?? machineId;
	}

	type ImportedApplication = Awaited<
		ReturnType<OverviewApi['listAdoptedApplications']>
	>['applications'][number];

	async function loadApplications(): Promise<void> {
		if (api === null || serverScope === null) return;
		const scope = serverScope;
		const epoch = ++applicationsEpoch;
		applicationsState = { status: 'loading' };
		try {
			const page: ApplicationPage = await api.listAdoptedApplications(scope, {
				limit: SUMMARY_PAGE_LIMIT
			});
			if (disposed || epoch !== applicationsEpoch || scope !== serverScope) return;
			if (page.server_id !== scope) {
				applications = [];
				applicationsTotal = 0;
				applicationsState = {
					status: 'error',
					code: 'scope_mismatch',
					message: 'The answer names a different server than this window\'s pinned scope.'
				};
				return;
			}
			applications = page.applications;
			applicationsTotal = page.total;
			applicationsCategories = page.categories ?? [];
			applicationsState = { status: 'ready' };
		} catch (error) {
			if (disposed || epoch !== applicationsEpoch) return;
			const code = codeOf(error) ?? 'unavailable';
			applications = [];
			applicationsTotal = 0;
			applicationsState = {
				status: 'error',
				code,
				message: friendlyText(code, messageOf(error) ?? 'The adopted applications could not be read.')
			};
		}
	}

	const staleApplications = $derived(applications.filter((row) => row.stale).length);

	/** Mapped service counts per category chip, in taxonomy order (AC strip). */
	const serviceMapCounts = $derived.by<Array<{ machineId: string; label: string; count: number }>>(
		() => {
			const counts = new Map<string, number>();
			for (const row of applications) {
				// The generated type declares the field; old fixtures omit it
				// at runtime — the `in` guard reads only what is present.
				const machineId =
					'category' in row && typeof row.category === 'string' && row.category !== ''
						? row.category
						: 'system';
				counts.set(machineId, (counts.get(machineId) ?? 0) + 1);
			}
			const ordered: Array<{ machineId: string; label: string; count: number }> = [];
			for (const entry of serviceTaxonomy) {
				const count = counts.get(entry.machine_id);
				if (count !== undefined) {
					ordered.push({ machineId: entry.machine_id, label: entry.label, count });
					counts.delete(entry.machine_id);
				}
			}
			for (const [machineId, count] of counts) {
				ordered.push({ machineId, label: machineId, count });
			}
			return ordered;
		}
	);

	let runs = $state<RunView[]>([]);
	let runsTotal = $state(0);
	let runsState = $state<ListState>({ status: 'loading' });
	let runsEpoch = 0;

	async function loadRuns(): Promise<void> {
		if (api === null || serverScope === null) return;
		const scope = serverScope;
		const epoch = ++runsEpoch;
		runsState = { status: 'loading' };
		try {
			const page: RunPage = await api.listRuns(scope, { limit: SUMMARY_PAGE_LIMIT });
			if (disposed || epoch !== runsEpoch || scope !== serverScope) return;
			// The runs route answers the explicit scope; an alien page is refused.
			for (const row of page.runs) {
				if (row.server_id !== scope) {
					runs = [];
					runsTotal = 0;
					runsState = {
						status: 'error',
						code: 'scope_mismatch',
						message: 'The answer names a different server than this window\'s pinned scope.'
					};
					return;
				}
			}
			runs = page.runs;
			runsTotal = page.total;
			runsState = { status: 'ready' };
		} catch (error) {
			if (disposed || epoch !== runsEpoch) return;
			const code = codeOf(error) ?? 'unavailable';
			runs = [];
			runsTotal = 0;
			runsState = {
				status: 'error',
				code,
				message: friendlyText(code, messageOf(error) ?? 'The run list could not be loaded.')
			};
		}
	}

	const runCounts = $derived.by(() => {
		let active = 0;
		let terminal = 0;
		for (const row of runs) {
			if (row.state !== null && TERMINAL_RUN_STATES.has(row.state)) terminal += 1;
			else active += 1;
		}
		return { active, terminal };
	});

	/** The pinned run's replayed state, refreshed by the durable reads. */
	const pinnedRun = $derived(runs.find((row) => row.run_id === pinnedRunId) ?? null);

	// --- detail drawer: the reviewed evidence component, nothing re-derived --

	let selectedObservationId = $state<string | null>(null);
	let selectedFindingId = $state<string | null>(null);

	const drawerObservation = $derived(
		observations.find((row) => row.observation_id === selectedObservationId) ?? null
	);
	const drawerFinding = $derived(
		findings.find((row) => row.finding_id === selectedFindingId) ?? null
	);

	function toggleObservation(observationId: string): void {
		selectedObservationId = selectedObservationId === observationId ? null : observationId;
	}

	function toggleFinding(findingId: string): void {
		selectedFindingId = selectedFindingId === findingId ? null : findingId;
	}

	/** The drawer's attach hand-off — only wired when the seam exists. */
	function attachEvidence(payload: EvidenceAttachPayload): void {
		if (chat === null) return;
		chat.attach(payload);
	}

	// --- live signal: the ONE reviewed stream, deduped, released on teardown --

	let streamStatus = $state<'off' | 'connecting' | 'live' | 'reconnecting' | 'closed'>('off');
	let streamProblem = $state<string | null>(null);
	/** Frames delivered and accepted (past the watermark) — observability only. */
	let streamDelivered = $state(0);

	/** Non-reactive controller locals: the async loop reads these directly. */
	let streamAbort: AbortController | null = null;
	let lastSeq = 0;
	let refreshTimer: ReturnType<typeof setTimeout> | null = null;

	/** Debounced durable refresh; frames are the signal, the pages the truth. */
	function scheduleRefresh(): void {
		if (disposed || refreshTimer !== null) return;
		refreshTimer = setTimeout(() => {
			refreshTimer = null;
			void refreshAll();
		}, REFRESH_DEBOUNCE_MS);
	}

	async function refreshAll(): Promise<void> {
		await Promise.allSettled([
			loadObservations(),
			loadFindings(),
			loadInbox(),
			loadApplications(),
			loadRuns()
		]);
	}

	function finishStream(): void {
		streamAbort?.abort();
		streamAbort = null;
		streamStatus = 'closed';
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
	 * The stream loop (the JobsPanel contract, instance-scoped): subscribes
	 * from the last seen `seq`, deduplicates by the monotonic watermark,
	 * survives the server's bounded stream close with a small fixed backoff,
	 * and stops for good once the pinned run reaches a terminal state (C05
	 * rule 2). The stream never submits, cancels or retargets anything.
	 */
	async function streamLoop(): Promise<void> {
		if (api === null) return;
		const runId = pinnedRunId;
		if (runId === null) return;
		const controller = new AbortController();
		streamAbort = controller;
		streamStatus = 'connecting';
		while (!disposed && pinnedRunId === runId && !controller.signal.aborted) {
			if (
				pinnedRun !== null &&
				pinnedRun.state !== null &&
				TERMINAL_RUN_STATES.has(pinnedRun.state)
			) {
				break;
			}
			try {
				await api.streamRunEvents(runId, {
					cursor: lastSeq > 0 ? String(lastSeq) : null,
					signal: controller.signal,
					onEvent: (frame) => {
						if (disposed || pinnedRunId !== runId) return;
						const event = readMaiEvent(frame);
						// Watermark dedupe (C08 rule 7): a replayed or duplicate
						// frame never schedules a second refresh.
						if (event === null || event.run_id !== runId || event.seq <= lastSeq) return;
						lastSeq = event.seq;
						streamDelivered += 1;
						streamStatus = 'live';
						streamProblem = null;
						scheduleRefresh();
					}
				});
			} catch (error) {
				if (disposed || pinnedRunId !== runId || controller.signal.aborted) return;
				// A fixed sentence: the actionable content is the reconnect
				// itself; raw network messages add nothing the status line
				// does not say.
				streamProblem = 'The event stream was interrupted; reconnecting from the last event.';
			}
			if (disposed || pinnedRunId !== runId || controller.signal.aborted) return;
			streamStatus = 'reconnecting';
			await sleep(BACKOFF_MS, controller.signal);
		}
		if (pinnedRunId === runId && !controller.signal.aborted) finishStream();
	}

	// --- panel links (fail-closed seams; never fake navigation) --------------

	function openApplications(): void {
		if (links === null || serverScope === null) return;
		links.openApplications(serverScope);
	}

	function openJobs(): void {
		if (links === null || serverScope === null) return;
		links.openJobs(serverScope);
	}

	// --- mount cycle (the $effect teardown is the release path) ---------------

	$effect(() => {
		if (!usable) return;
		disposed = false;
		void refreshAll();
		// The stream loop reads `pinnedRun` (the runs page) before its first
		// await. Started inside this effect's tracking scope it would make
		// the effect depend on `runs` — and every durable refresh re-runs
		// the effect forever. The loop's deps are pinned at open time (the
		// pinned scope never changes); starting it untracked keeps the effect
		// dependent only on `usable` (the JobsPanel contract: the stream is
		// a change signal, never a re-trigger of its own watcher).
		if (pinnedRunId !== null) untrack(() => void streamLoop());
		return () => {
			disposed = true;
			finishStream();
			if (refreshTimer !== null) {
				clearTimeout(refreshTimer);
				refreshTimer = null;
			}
		};
	});
</script>

<div
	class="mai-overview"
	data-testid="overview-root"
	data-scope={serverScope ?? ''}
	data-live={streamStatus}
>
	{#if !usable}
		<div class="mai-overview__state mai-overview__state--error" role="alert" data-testid="overview-config-error">
			<p>This panel was opened without its overview service or outside a pinned server scope.</p>
		</div>
	{:else}
		<header class="mai-overview__header">
			<h3 class="mai-overview__title">Overview</h3>
			<p class="mai-overview__meta">
				<span data-testid="overview-scope">Server {shortId(serverScope ?? '')}</span>
				<span title={serverScope ?? ''}>Pinned scope — every request below names this target.</span>
			</p>
			<p class="mai-overview__live" data-testid="overview-live" data-live={streamStatus}>
				{#if pinnedRunId !== null}
					Live: watching run {shortId(pinnedRunId)} —
					<span data-testid="live-status">
						{streamStatus === 'live'
							? 'stream live'
							: streamStatus === 'connecting'
								? 'connecting'
								: streamStatus === 'reconnecting'
									? 'reconnecting'
									: 'stream closed (run finished)'}
					</span>
					{#if streamDelivered > 0}
						<span data-testid="live-delivered">· {streamDelivered} change frames</span>
					{/if}
					{#if streamProblem !== null}
						<span class="mai-overview__live-problem" data-testid="live-problem">
							last stream problem: {streamProblem} — reconnecting
						</span>
					{/if}
				{:else}
					<span data-testid="live-off">
						Live updates off — no run is pinned to this window, so there is no event stream
						to watch. Refresh reads the durable state.
					</span>
				{/if}
			</p>
		</header>

		<div class="mai-overview__grid">
			<!-- Monitor freshness: the producer derives freshness at request time.
			     The tally below is page-scoped and labeled as such. -->
			<section class="mai-overview__section" aria-label="Monitor freshness" data-testid="overview-freshness">
				<div class="mai-overview__section-head">
					<h4>Monitor freshness</h4>
					<Button variant="secondary" onclick={loadObservations} data-testid="freshness-refresh">
						Refresh
					</Button>
				</div>
				{#if observationsState.status === 'loading'}
					<div class="mai-overview__state" role="status" data-testid="freshness-loading">
						<p>Loading observation page…</p>
					</div>
				{:else if observationsState.status === 'error'}
					<div
						class="mai-overview__state mai-overview__state--error"
						role="alert"
						data-testid="freshness-error"
						data-error-code={observationsState.code}
					>
						<p>{observationsState.message}</p>
						<Button variant="secondary" onclick={loadObservations} data-testid="freshness-retry">
							Retry
						</Button>
					</div>
				{:else if observations.length === 0}
					<div class="mai-overview__state" role="status" data-testid="freshness-empty">
						<p>No observations are recorded for this server yet — an empty history is not an
							unknown reading. Collectors create samples when they run.</p>
					</div>
				{:else}
					<p class="mai-overview__meta" data-testid="freshness-counts">
						Latest page: <span data-testid="freshness-fresh">{freshnessCounts.fresh} fresh</span> ·
						<span data-testid="freshness-stale">{freshnessCounts.stale} stale</span> ·
						<span data-testid="freshness-unknown">{freshnessCounts.unknown} unknown</span>
						— of {observationsTotal} recorded.
						{#if newestObservedAt !== null}
							Newest sample collected <span data-testid="freshness-newest">{newestObservedAt}</span>.
						{/if}
					</p>
					<ul class="mai-overview__rows" data-testid="observation-list">
						{#each observations as row (row.observation_id)}
							<li class="mai-overview__row" data-testid="observation-row" data-obs-id={row.observation_id}>
								<button
									type="button"
									class="mai-overview__row-open"
									aria-pressed={selectedObservationId === row.observation_id}
									onclick={() => toggleObservation(row.observation_id)}
									data-testid="observation-open"
								>
									<span class="mai-overview__label" data-testid="row-observation-metric">
										{row.metric ?? 'no sample taken'}
									</span>
									<span
										class="mai-overview__badge {freshnessBadge(row.freshness)}"
										data-testid="row-observation-freshness"
									>
										{row.freshness}
									</span>
									<span class="mai-overview__badge {row.quality === 'measured' ? 'mai-overview__badge--ok' : 'mai-overview__badge--warn'}" data-testid="row-observation-quality">
										{row.quality}
									</span>
									<span class="mai-overview__fact">{row.source} · {row.method}</span>
									<span class="mai-overview__fact" data-testid="row-observation-collected">
										{row.collected_at}
									</span>
								</button>
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<!-- Findings + re-evaluation: attention-ordered by the producer. -->
			<section class="mai-overview__section" aria-label="Findings" data-testid="overview-findings">
				<div class="mai-overview__section-head">
					<h4>Open findings</h4>
					<Button
						variant="secondary"
						loading={evaluateBusy}
						onclick={evaluate}
						data-testid="evaluate-run-button"
					>
						Re-evaluate
					</Button>
				</div>
				{#if findingsState.status === 'loading'}
					<div class="mai-overview__state" role="status" data-testid="findings-loading">
						<p>Loading finding page…</p>
					</div>
				{:else if findingsState.status === 'error'}
					<div
						class="mai-overview__state mai-overview__state--error"
						role="alert"
						data-testid="findings-error"
						data-error-code={findingsState.code}
					>
						<p>{findingsState.message}</p>
						<Button variant="secondary" onclick={loadFindings} data-testid="findings-retry">
							Retry
						</Button>
					</div>
				{:else if findings.length === 0}
					<div class="mai-overview__state" role="status" data-testid="findings-empty">
						<p>No open findings for this server. Re-evaluation re-runs the real evaluator
							under the frozen policy — it never invents or closes conditions.</p>
					</div>
				{:else}
					<p class="mai-overview__meta" data-testid="findings-counts">
						Latest page:
						<span data-testid="findings-critical">{severityCounts.critical} critical</span> ·
						<span data-testid="findings-warning">{severityCounts.warning} warning</span> ·
						<span data-testid="findings-info">{severityCounts.info} info</span>
						— {findingsTotal} open in total.
					</p>
					<ul class="mai-overview__rows" data-testid="finding-list">
						{#each findings as row (row.finding_id)}
							<li class="mai-overview__row" data-testid="finding-row" data-finding-id={row.finding_id}>
								<button
									type="button"
									class="mai-overview__row-open"
									aria-pressed={selectedFindingId === row.finding_id}
									onclick={() => toggleFinding(row.finding_id)}
									data-testid="finding-open"
								>
									<span class="mai-overview__badge {severityBadge(row.severity)}" data-testid="row-finding-severity">
										{row.severity}
									</span>
									<span class="mai-overview__label" data-testid="row-finding-kind">{row.kind}</span>
									<span class="mai-overview__fact" data-testid="row-finding-summary">{row.summary}</span>
									<span class="mai-overview__fact">opened {row.opened_at}</span>
								</button>
							</li>
						{/each}
					</ul>
				{/if}
				{#if evaluateProblem !== null}
					<div
						class="mai-overview__state mai-overview__state--error"
						role="alert"
						data-testid="evaluate-problem"
						data-error-code={evaluateProblem.code}
					>
						<p>{evaluateProblem.message}</p>
					</div>
				{/if}
			</section>

			<!-- Notification attention inbox + the idempotent ack. -->
			<section class="mai-overview__section" aria-label="Notification inbox" data-testid="overview-inbox">
				<div class="mai-overview__section-head">
					<h4>Notification inbox</h4>
					<Button variant="secondary" onclick={loadInbox} data-testid="inbox-refresh">
						Refresh
					</Button>
				</div>
				{#if noticesState.status === 'loading'}
					<div class="mai-overview__state" role="status" data-testid="inbox-loading">
						<p>Loading inbox…</p>
					</div>
				{:else if noticesState.status === 'error'}
					<div
						class="mai-overview__state mai-overview__state--error"
						role="alert"
						data-testid="inbox-error"
						data-error-code={noticesState.code}
					>
						<p>{noticesState.message}</p>
						<Button variant="secondary" onclick={loadInbox} data-testid="inbox-retry">
							Retry
						</Button>
					</div>
				{:else if notices.length === 0}
					<div class="mai-overview__state" role="status" data-testid="inbox-empty">
						<p>No unacknowledged notices for this server. The inbox is the durable record —
							acknowledgment stamps a notice and deletes nothing.</p>
					</div>
				{:else}
					<p class="mai-overview__meta" data-testid="inbox-count">
						{notices.length} of {noticesTotal} unacknowledged (latest page).
					</p>
					<ul class="mai-overview__rows" data-testid="notice-list">
						{#each notices as row (row.alert_id)}
							<li
								class="mai-overview__row"
								data-testid="notice-row"
								data-alert-id={row.alert_id}
								data-acked={row.acked ? 'true' : 'false'}
							>
								<div class="mai-overview__row-main">
									<span class="mai-overview__badge {severityBadge(row.severity)}" data-testid="notice-severity">
										{row.severity}
									</span>
									<span class="mai-overview__label" data-testid="notice-title">{row.title}</span>
									{#if row.summary !== null}
										<span class="mai-overview__fact" data-testid="notice-summary">{row.summary}</span>
									{/if}
									<span class="mai-overview__fact" data-testid="notice-created">{row.created_at}</span>
									{#if row.acked}
										<span class="mai-overview__badge mai-overview__badge--ok" data-testid="notice-acked">
											acknowledged {row.acknowledged_at ?? ''}
										</span>
									{:else}
										<Button
											variant="secondary"
											loading={ackBusyId === row.alert_id}
											onclick={() => acknowledge(row)}
											data-testid="ack-button"
										>
											Acknowledge
										</Button>
									{/if}
								</div>
							</li>
						{/each}
					</ul>
				{/if}
				{#if ackProblem !== null}
					<div
						class="mai-overview__state mai-overview__state--error"
						role="alert"
						data-testid="ack-problem"
						data-error-code={ackProblem.code}
					>
						<p>{ackProblem.message}</p>
					</div>
				{/if}
			</section>

			<!-- Application / job state: summaries + links, no duplicated logic. -->
			<section class="mai-overview__section" aria-label="Applications and runs" data-testid="overview-state">
				<div class="mai-overview__section-head">
					<h4>Applications &amp; runs</h4>
					<span class="mai-overview__head-actions">
						<Button
							variant="secondary"
							onclick={() => {
								void loadApplications();
								void loadRuns();
							}}
							data-testid="state-refresh"
						>
							Refresh
						</Button>
						{#if links === null}
							<span class="mai-overview__meta" data-testid="links-note">
								Panel links are not wired in this host — the summaries below stay read-only.
							</span>
						{:else}
							<Button variant="secondary" onclick={openApplications} data-testid="apps-link">
								Open Applications
							</Button>
							<Button variant="secondary" onclick={openJobs} data-testid="jobs-link">
								Open Jobs
							</Button>
						{/if}
					</span>
				</div>
				{#if applicationsState.status === 'loading' || runsState.status === 'loading'}
					<div class="mai-overview__state" role="status" data-testid="state-loading">
						<p>Loading application and run state…</p>
					</div>
				{:else if applicationsState.status === 'error' || runsState.status === 'error'}
					<div
						class="mai-overview__state mai-overview__state--error"
						role="alert"
						data-testid="state-error"
						data-error-code={applicationsState.status === 'error'
							? applicationsState.code
							: runsState.status === 'error'
								? runsState.code
								: ''}
					>
						<p>
							{applicationsState.status === 'error'
								? applicationsState.message
								: runsState.status === 'error'
									? runsState.message
									: ''}
						</p>
						<Button
							variant="secondary"
							onclick={() => {
								void loadApplications();
								void loadRuns();
							}}
							data-testid="state-retry"
						>
							Retry
						</Button>
					</div>
				{:else}
					<p class="mai-overview__meta" data-testid="apps-counts">
						<span data-testid="apps-total">{applicationsTotal} adopted applications</span>
						{#if applications.length > 0}
							· <span data-testid="apps-stale">{staleApplications} with observed drift</span>
						{/if}
					</p>
					{#if serviceMapCounts.length > 0}
						<div
							class="mai-overview__servicemap"
							role="list"
							aria-label="Server service map summary"
							data-testid="service-map-summary"
						>
							{#each serviceMapCounts as entry (entry.machineId)}
								<span
									class="mai-overview__servicemap-chip"
									role="listitem"
									data-testid="service-map-chip"
									data-category={entry.machineId}
								>
									{serviceCategoryLabel(entry.machineId)} · {entry.count}
								</span>
							{/each}
						</div>
					{/if}
					<p class="mai-overview__meta" data-testid="runs-counts">
						<span data-testid="runs-active">{runCounts.active} active</span> ·
						<span data-testid="runs-terminal">{runCounts.terminal} finished</span>
						— of {runsTotal} on the latest page window.
					</p>
					{#if pinnedRun !== null}
						<p class="mai-overview__meta" data-testid="pinned-run">
							Watched run: <span data-testid="pinned-run-state" class="mai-overview__badge {stateBadge(pinnedRun.state)}">
								{runStateText(pinnedRun.state)}
							</span>
							— updated {pinnedRun.updated_at}
						</p>
					{/if}
				{/if}
			</section>
		</div>

		<!-- The ONE detail surface: the reviewed evidence drawer component.
		     Selecting rows is view state only; evaluation renders through the
		     drawer contract (server-checked) so no detail logic is duplicated. -->
		<section class="mai-overview__drawer" aria-label="Evidence details" data-testid="overview-drawer">
			{#if drawerObservation === null && drawerFinding === null}
				<p class="mai-overview__meta" data-testid="drawer-empty">
					Select an observation or a finding to inspect its recorded evidence.
				</p>
			{:else}
				<ObservationDetails
					serverId={serverScope ?? ''}
					observation={drawerObservation}
					finding={drawerFinding}
					evaluateBusy={evaluateBusy}
					evaluateResult={evaluateResult}
					onattach={chat === null ? undefined : attachEvidence}
					onevaluate={evaluate}
				/>
			{/if}
		</section>
	{/if}
</div>

<style>
	.mai-overview {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3, 0.75rem);
		padding: var(--mai-space-3, 0.75rem);
		color: var(--mai-color-text);
		background: var(--mai-color-surface);
		font-size: var(--mai-font-size-sm, 0.875rem);
		min-width: 0;
		min-height: 0;
		overflow: auto;
	}

	.mai-overview__header {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 0.25rem);
	}

	.mai-overview__title {
		margin: 0;
		font-size: var(--mai-font-size-md, 1rem);
	}

	.mai-overview__meta {
		margin: 0;
		color: var(--mai-color-text-muted);
	}

	.mai-overview__live {
		margin: 0;
		color: var(--mai-color-text-muted);
	}

	.mai-overview__live-problem {
		color: var(--mai-status-warning-text);
	}

	.mai-overview__grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
		gap: var(--mai-space-3, 0.75rem);
		align-items: start;
	}

	.mai-overview__section {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
		padding: var(--mai-space-2, 0.5rem);
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-md, 0.375rem);
		background: var(--mai-color-surface-raised);
		min-width: 0;
	}

	.mai-overview__section-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-overview__head-actions {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-overview__section-head h4 {
		margin: 0;
		font-size: var(--mai-font-size-sm, 0.875rem);
	}

	.mai-overview__rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 0.25rem);
	}

	.mai-overview__row {
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm, 0.25rem);
		background: var(--mai-color-surface);
		min-width: 0;
	}

	.mai-overview__row-open {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
		width: 100%;
		padding: var(--mai-space-1, 0.25rem) var(--mai-space-2, 0.5rem);
		border: none;
		background: transparent;
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}

	.mai-overview__row-open:focus-visible {
		outline: var(--mai-focus-ring-width, 2px) solid var(--mai-color-primary);
		outline-offset: 1px;
	}

	.mai-overview__row-main {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
		padding: var(--mai-space-1, 0.25rem) var(--mai-space-2, 0.5rem);
	}

	.mai-overview__label {
		font-weight: 600;
		overflow-wrap: anywhere;
	}

	.mai-overview__fact {
		color: var(--mai-color-text-muted);
		overflow-wrap: anywhere;
	}

	.mai-overview__badge {
		display: inline-block;
		padding: 0 0.35em;
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm, 0.25rem);
		font-size: var(--mai-font-size-xs, 0.75rem);
	}

	.mai-overview__badge--ok {
		color: var(--mai-status-success-text);
		background: var(--mai-status-success-bg);
		border-color: var(--mai-status-success-border);
	}

	.mai-overview__badge--warn {
		color: var(--mai-status-warning-text);
		background: var(--mai-status-warning-bg);
		border-color: var(--mai-status-warning-border);
	}

	.mai-overview__badge--danger {
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
		border-color: var(--mai-status-danger-border);
	}

	.mai-overview__badge--info {
		color: var(--mai-status-info-text);
		background: var(--mai-status-info-bg);
		border-color: var(--mai-status-info-border);
	}

	.mai-overview__badge--neutral {
		color: var(--mai-color-text-muted);
		background: var(--mai-color-bg);
	}

	.mai-overview__servicemap {
		display: flex;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
		align-items: baseline;
	}

	.mai-overview__servicemap-chip {
		display: inline-block;
		border: 1px solid var(--mai-color-brass, var(--mai-color-border));
		border-radius: var(--mai-radius-sm, 0.25rem);
		padding: 0 var(--mai-space-2, 0.5rem);
		font-size: var(--mai-font-size-xs, 0.75rem);
		white-space: nowrap;
		color: var(--mai-color-brass, var(--mai-color-text));
	}

	.mai-overview__state {
		padding: var(--mai-space-2, 0.5rem);
		border-radius: var(--mai-radius-sm, 0.25rem);
		background: var(--mai-color-bg);
		color: var(--mai-color-text-muted);
	}

	.mai-overview__state p {
		margin: 0 0 var(--mai-space-1, 0.25rem);
	}

	.mai-overview__state--error {
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
		border: 1px solid var(--mai-status-danger-border);
	}

	.mai-overview__drawer {
		border-top: 1px solid var(--mai-color-border);
		padding-top: var(--mai-space-2, 0.5rem);
	}
</style>
