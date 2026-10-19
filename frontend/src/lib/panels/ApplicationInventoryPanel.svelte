<script module lang="ts">
	/**
	 * ApplicationInventoryPanel — catalogue, adoption and inventory for the
	 * pinned server (card `mai-08-web-apps`, P08; contracts C08/C09/C11).
	 *
	 * Layer: reusable feature panel (windowed-workspace ADR §7.2). It owns NO
	 * window mechanics: no geometry, no drag/resize, no focus/stacking, no
	 * stores, no module-level mutable state — everything enters through the
	 * single `panel: PanelContext` prop (validated scope + explicit services
	 * bag) and instances share nothing (NT5; SSR-safe per ADR §5 rule 4).
	 *
	 * Discovery and adoption stay SEPARATE resources here exactly as the
	 * producer keeps them (C09 rule 1, AC 1): the Discovery tab observes one
	 * read-only snapshot (per-lane honesty — an unavailable lane is REPORTED,
	 * never silently absent), the Inventory tab renders the projected
	 * adoption page, and the two are joined only by explicit admin
	 * decisions. An observation never adopts, grants or enables anything;
	 * adoption is an explicit form submission carrying the observed external
	 * deployment paths as REFERENCED metadata (C09 rule 6).
	 *
	 * Lifecycle actions are LINKS, not reimplementations (AC 2): install/
	 * update/remove are durable `app.*` intent submits owned by their own
	 * feature surfaces and reach this panel only through the typed
	 * `ApplicationsLifecycle` seam; without the seam the links fail closed
	 * to a visible note, never a client-side fake submit. The canonical
	 * record document written by `08-app-docs` is linked through the
	 * `ApplicationsDocs` seam the same way.
	 *
	 * Wire honesty (C08 rule 6; AC 2): every section renders
	 * loading/empty/unavailable/error/ready states; every response's
	 * `server_id` is checked against the pinned scope before it may paint
	 * (exact server identity); an adoption whose record document is still
	 * `pending` says so with the producer's reason — the committed adoption
	 * is never presented as undocumented-or-rolled-back.
	 *
	 * Hostile content (AC 2): names, paths, units and producer reasons are
	 * rendered as inert text via Svelte's escaped interpolation only —
	 * there is deliberately no `{@html}` in this file.
	 *
	 * Stale-content guard (AC 2): every async section carries an
	 * instance-local epoch; a late response of a superseded request (or of
	 * a disposed host) is dropped before it can paint. There is no
	 * client-side cache to go stale.
	 *
	 * Scope (C01 rule 2, NT5): the descriptor's pinned `server_id` is the
	 * only target of every call; the pinned context of a live window never
	 * follows a global selection or another window's focus.
	 */
</script>

<script lang="ts">
	import { onDestroy } from 'svelte';
	import Button from '../ui/Button.svelte';
	import Field from '../ui/Field.svelte';
	import SearchFilterInput from '../ui/SearchFilterInput.svelte';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import {
		CATEGORY_KEYWORD_HINTS,
		DEFAULT_SERVICE_CATEGORIES
	} from '../workspace/entries/08-web-apps.js';
	import type {
		AdoptBody,
		ApplicationOrigin,
		ApplicationPage,
		ApplicationView,
		ApplicationsApi,
		ApplicationsDocs,
		ApplicationsLifecycle,
		AdoptionCreated,
		ComposeDetailView,
		DiscoveryView,
		ObservedApplicationView,
		RecordDocView,
		ServiceCategoryView,
		ServiceInstructionsView,
		StandaloneDetailView,
		SystemdUnitDetailView
	} from '../workspace/entries/08-web-apps.js';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readApi(services: PanelServices): ApplicationsApi | null {
		const candidate = services.api as Partial<ApplicationsApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (
			const name of ['listAdopted', 'discover', 'adopt', 'revoke'] as const
		) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as ApplicationsApi;
	}

	/** Narrow the optional lifecycle seam; incomplete ⇒ null (fail closed). */
	function readLifecycle(services: PanelServices): ApplicationsLifecycle | null {
		const candidate = services.lifecycle as Partial<ApplicationsLifecycle> | null | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (
			const name of ['openInstall', 'openUpdate', 'openRemove'] as const
		) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as ApplicationsLifecycle;
	}

	/** Narrow the optional documents seam; incomplete ⇒ null (fail closed). */
	function readDocs(services: PanelServices): ApplicationsDocs | null {
		const candidate = services.docs as Partial<ApplicationsDocs> | null | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		if (typeof candidate.openDocument !== 'function') return null;
		return candidate as ApplicationsDocs;
	}

	const api = $derived(readApi(panel.services));
	const lifecycle = $derived(readLifecycle(panel.services));
	const docs = $derived(readDocs(panel.services));
	/** Applications management: a global mount contradicts the entry contract. */
	const serverScope = $derived(panel.scope.kind === 'server' ? panel.scope.server_id : null);
	/** Optional pinned application object (registry-restricted to kind 'application'). */
	const pinnedApplicationId = $derived(
		panel.scope.kind === 'server' && panel.scope.object?.kind === 'application'
			? panel.scope.object.id
			: null
	);
	const usable = $derived(api !== null && serverScope !== null);

	// --- producer bounds mirrored for display only (server re-validates) -----

	/** Bounded adopted page size (applications.py DEFAULT_PAGE_LIMIT). */
	const PAGE_LIMIT = 50;
	/** Bounded adoption name (adopt.py _NAME_MAX). */
	const MAX_NAME_CHARS = 200;
	/** Bounded selector values: compose project/working dir/unit name (adopt.py). */
	const MAX_SELECTOR_CHARS = 512;
	/** Bounded external path entries (adopt.py _PATH_MAX). */
	const MAX_PATH_CHARS = 1024;

	const TABS = Object.freeze([
		{ id: 'inventory', label: 'Inventory' },
		{ id: 'discovery', label: 'Discovery' },
		{ id: 'adopt', label: 'Adopt' },
		{ id: 'categories', label: 'Categories' }
	] as const);
	type TabId = (typeof TABS)[number]['id'];
	let tab = $state<TabId>('inventory');

	// --- category taxonomy (the Service Map chips vocabulary) ----------------

	/** Producer taxonomy carried on the latest adopted page (may be omitted). */
	let pageCategories = $state<ServiceCategoryView[]>([]);
	/** Producer taxonomy carried on the latest discovery snapshot. */
	let snapshotCategories = $state<ServiceCategoryView[]>([]);
	/** Custom categories minted in this window; they ride the next adoption. */
	let customCategories = $state<ServiceCategoryView[]>([]);

	/**
	 * The rendered taxonomy: the producer's merged page (8 built-ins +
	 * customs) when the wire carries it, else the client-side built-ins
	 * (old fixtures); session-minted customs merge on top by machine id.
	 */
	const taxonomy = $derived.by<ServiceCategoryView[]>(() => {
		const base: ServiceCategoryView[] =
			pageCategories.length > 0
				? pageCategories
				: snapshotCategories.length > 0
					? snapshotCategories
					: DEFAULT_SERVICE_CATEGORIES.map((seed) => ({
							machine_id: seed.machine_id,
							label: seed.label,
							description: seed.description,
							built_in: true
						}));
		const merged = [...base];
		for (const custom of customCategories) {
			const index = merged.findIndex((entry) => entry.machine_id === custom.machine_id);
			if (index >= 0) merged[index] = custom;
			else merged.push(custom);
		}
		merged.sort((a, b) =>
			a.machine_id.localeCompare(b.machine_id, undefined, { numeric: true, sensitivity: 'base' })
		);
		return merged;
	});

	/** Human label for one machine id; unknown ids render as-is (honest). */
	function categoryLabel(machineId: string): string {
		return taxonomy.find((entry) => entry.machine_id === machineId)?.label ?? machineId;
	}

	/** The category recorded on one mapped record (old records → "system"). */
	function mappedCategory(row: ApplicationView): string {
		// The generated type declares the field; old fixtures omit it at
		// runtime — the `in` guard reads only what is actually present.
		if ('category' in row && typeof row.category === 'string' && row.category !== '') {
			return row.category;
		}
		return 'system';
	}

	/** The stored installation description (old records → empty). */
	function mappedDescription(row: ApplicationView): string {
		if ('description' in row && typeof row.description === 'string') return row.description;
		return '';
	}

	/** The runbook recorded on one mapped record (old records → none). */
	function mappedInstructions(row: ApplicationView): ServiceInstructionsView | null {
		if ('instructions' in row && typeof row.instructions === 'object' && row.instructions !== null) {
			return row.instructions;
		}
		return null;
	}

	const RUNBOOK_STEPS = Object.freeze(['install', 'update', 'uninstall', 'backup', 'verify'] as const);
	type RunbookStep = (typeof RUNBOOK_STEPS)[number];

	/** Non-empty runbook steps of one record, in display order. */
	function recordedSteps(instructions: ServiceInstructionsView | null): Array<[RunbookStep, string]> {
		if (instructions === null) return [];
		const steps: Array<[RunbookStep, string]> = [];
		for (const step of RUNBOOK_STEPS) {
			const text = instructions[step];
			if (typeof text === 'string' && text.trim() !== '') steps.push([step, text]);
		}
		return steps;
	}

	/**
	 * The deterministic suggested category for one observation: the
	 * producer's hint when present, else the client-side keyword heuristic
	 * over the suggested name and unit path (old fixtures omit the hint).
	 */
	function suggestedCategory(observed: ObservedApplicationView): string {
		// The producer hint wins when the snapshot carries it; the generated
		// type declares the field, old fixtures omit it at runtime.
		if (
			'suggested_category' in observed &&
			typeof observed.suggested_category === 'string' &&
			observed.suggested_category !== ''
		) {
			return observed.suggested_category;
		}
		const haystack = suggestedName(observed).toLowerCase();
		for (const [machineId, keywords] of CATEGORY_KEYWORD_HINTS) {
			for (const keyword of keywords) {
				if (haystack.includes(keyword)) return machineId;
			}
		}
		return 'system';
	}

	// --- service map filter (category pills + search over the adopted page) --

	let categoryFilter = $state<string>('all');
	let inventorySearchQuery = $state<string>('');
	let discoverySearchQuery = $state<string>('');

	/** Primary machine/unit sort key for an adopted inventory application. */
	function inventorySortKey(row: ApplicationView): string {
		return row.unit_name ?? row.compose_project ?? row.name ?? row.application_id;
	}

	/** Categories that actually hold at least one mapped record, in alphabetical taxonomy order. */
	const presentCategories = $derived.by<string[]>(() => {
		const present = new Set(applications.map((row) => mappedCategory(row)));
		return taxonomy.map((entry) => entry.machine_id).filter((id) => present.has(id));
	});

	/** The adopted page after the active category pill + search filter, sorted alphabetically by unit/machine name. */
	const filteredApplications = $derived.by<ApplicationView[]>(() => {
		const byCategory =
			categoryFilter === 'all'
				? applications
				: applications.filter((row) => mappedCategory(row) === categoryFilter);
		const q = inventorySearchQuery.trim().toLowerCase();
		const byQuery =
			q === ''
				? byCategory
				: byCategory.filter((row) => {
						const catId = mappedCategory(row);
						const haystacks = [
							row.name,
							row.application_id,
							row.origin,
							originLabel(row.origin),
							catId,
							categoryLabel(catId),
							row.compose_project ?? '',
							row.working_dir ?? '',
							row.unit_name ?? '',
							row.external_working_dir ?? '',
							mappedDescription(row)
						];
						return haystacks.some((part) => part.toLowerCase().includes(q));
					});
		return [...byQuery].sort((a, b) => {
			const cmp = inventorySortKey(a).localeCompare(inventorySortKey(b), undefined, {
				numeric: true,
				sensitivity: 'base'
			});
			if (cmp !== 0) return cmp;
			return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
		});
	});

	// --- value-free display helpers (C08 rule 6) -----------------------------

	let disposed = false;

	function shortId(id: string | null): string {
		return id === null || id === '' ? '—' : id.slice(0, 8);
	}

	function shortHash(hash: string | null): string {
		return hash === null || hash === '' ? '—' : hash.slice(0, 12);
	}

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
		if (code === 'conflict')
			return 'The canonical state changed or conflicts with an existing record; refresh and reapply.';
		if (code === 'not_found') return 'Nothing is stored under this id and scope.';
		if (code === 'unavailable')
			return 'The applications service is unavailable right now; nothing was changed.';
		if (code === 'validation_failed') return 'The request was not accepted; see the details.';
		return fallback;
	}

	function originLabel(origin: string): string {
		if (origin === 'compose') return 'Compose project';
		if (origin === 'docker_container') return 'Standalone container';
		if (origin === 'systemd_unit') return 'systemd unit';
		return origin;
	}

	function composeDetail(observed: ObservedApplicationView): ComposeDetailView | null {
		return observed.detail.kind === 'compose_project' ? observed.detail : null;
	}

	function standaloneDetail(observed: ObservedApplicationView): StandaloneDetailView | null {
		return observed.detail.kind === 'standalone_container' ? observed.detail : null;
	}

	function systemdDetail(observed: ObservedApplicationView): SystemdUnitDetailView | null {
		return observed.detail.kind === 'systemd_unit' ? observed.detail : null;
	}

	/** A suggested display name from the observation (the admin may edit it). */
	function suggestedName(observed: ObservedApplicationView): string {
		const compose = composeDetail(observed);
		if (compose !== null) return compose.project_name;
		const standalone = standaloneDetail(observed);
		if (standalone !== null) return standalone.container.name;
		const systemd = systemdDetail(observed);
		if (systemd !== null) return systemd.unit;
		return '';
	}

	/** The discovery row key: a managed id, else the stable detail identity. */
	function observedKey(observed: ObservedApplicationView): string {
		if (observed.application_id !== null) return `id:${observed.application_id}`;
		const detail = observed.detail;
		if (detail.kind === 'compose_project') return `compose:${detail.project_name}`;
		if (detail.kind === 'standalone_container') return `container:${detail.container.container_id}`;
		return `unit:${detail.unit}`;
	}

	function laneBadge(state: string): 'ok' | 'warn' | 'danger' {
		if (state === 'observed') return 'ok';
		if (state === 'outcome_unknown' || state === 'failed') return 'danger';
		return 'warn';
	}

	// --- inventory (the projected adoption page; C09 rule 1) -------------------

	let applications = $state<ApplicationView[]>([]);
	let inventoryTotal = $state(0);
	let inventoryState = $state<'idle' | 'loading' | 'ready' | 'error'>('idle');
	let inventoryProblem = $state<{ code: string; message: string } | null>(null);
	let inventoryEpoch = 0;

	async function loadInventory(): Promise<void> {
		if (api === null || serverScope === null) return;
		const scope = serverScope;
		const epoch = ++inventoryEpoch;
		inventoryState = 'loading';
		inventoryProblem = null;
		try {
			const page: ApplicationPage = await api.listAdopted(scope, { limit: PAGE_LIMIT });
			if (disposed || epoch !== inventoryEpoch || scope !== serverScope) return;
			// Exact server identity (AC 1): the page must name the pinned scope.
			if (page.server_id !== scope) {
				applications = [];
				inventoryTotal = 0;
				inventoryState = 'error';
				inventoryProblem = {
					code: 'scope_mismatch',
					message: 'The answer names a different server than this window\'s pinned scope.'
				};
				return;
			}
			applications = page.applications;
			inventoryTotal = page.total;
			// The merged taxonomy rides the page when the producer carries it;
			// an omitted field (old fixtures) keeps the client-side built-ins.
			pageCategories = page.categories ?? [];
			inventoryState = 'ready';
		} catch (error) {
			if (disposed || epoch !== inventoryEpoch) return;
			const code = codeOf(error) ?? 'unavailable';
			applications = [];
			inventoryTotal = 0;
			inventoryState = 'error';
			inventoryProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The adopted applications could not be read.')
			};
		}
	}

	// --- record documents seen from adoption responses (by application id) -----

	let recordDocs = $state<Record<string, RecordDocView>>({});

	/**
	 * Set when an adoption/revoke happened after the held snapshot was
	 * taken; cleared by the next observation. The snapshot is observation
	 * data, never silently rewritten by a decision.
	 */
	let snapshotStale = $state(false);

	// --- revoke (non-destructive; C09 rule 6) ----------------------------------

	let revokeTarget = $state<ApplicationView | null>(null);
	let revokeBusy = $state(false);
	let revokeProblem = $state<{ code: string; message: string } | null>(null);
	let revokeDoneId = $state<string | null>(null);

	function startRevoke(row: ApplicationView): void {
		revokeTarget = row;
		revokeProblem = null;
		revokeDoneId = null;
	}

	function cancelRevoke(): void {
		revokeTarget = null;
		revokeProblem = null;
	}

	async function confirmRevoke(): Promise<void> {
		const target = revokeTarget;
		if (api === null || serverScope === null || target === null || revokeBusy) return;
		const scope = serverScope;
		revokeBusy = true;
		revokeProblem = null;
		try {
			await api.revoke(scope, target.application_id, { expected_revision: target.revision });
			if (disposed || scope !== serverScope) return;
			revokeDoneId = target.application_id;
			revokeTarget = null;
			// The managed truth changed: refresh the page and mark any held
			// snapshot older than this decision.
			snapshotStale = true;
			void loadInventory();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			revokeProblem = {
				code,
				message:
					code === 'conflict'
						? 'The record changed at another revision; refresh the inventory and reapply.'
						: friendlyText(code, messageOf(error) ?? 'The revocation could not be recorded.')
			};
		} finally {
			if (!disposed) revokeBusy = false;
		}
	}

	// --- discovery (read-only lanes; C09 rule 1) -------------------------------

	let discovery = $state<DiscoveryView | null>(null);
	let discoveryState = $state<'idle' | 'loading' | 'ready' | 'error'>('idle');
	let discoveryProblem = $state<{ code: string; message: string } | null>(null);
	let discoveryEpoch = 0;

	async function observeInventory(): Promise<void> {
		if (api === null || serverScope === null) return;
		const scope = serverScope;
		const epoch = ++discoveryEpoch;
		discoveryState = 'loading';
		discoveryProblem = null;
		try {
			const view = await api.discover(scope);
			if (disposed || epoch !== discoveryEpoch || scope !== serverScope) return;
			// Exact server identity (AC 1): the snapshot must name the pinned scope.
			if (view.server_id !== scope) {
				discovery = null;
				discoveryState = 'error';
				discoveryProblem = {
					code: 'scope_mismatch',
					message: 'The snapshot names a different server than this window\'s pinned scope.'
				};
				return;
			}
			discovery = view;
			snapshotCategories = view.categories ?? [];
			snapshotStale = false;
			discoveryState = 'ready';
		} catch (error) {
			if (disposed || epoch !== discoveryEpoch) return;
			const code = codeOf(error) ?? 'unavailable';
			discovery = null;
			discoveryState = 'error';
			discoveryProblem = {
				code,
				message:
					code === 'conflict'
						? 'The target\'s pinned host key does not match — re-enrollment is an explicit admin action; nothing was observed.'
						: friendlyText(code, messageOf(error) ?? 'The inventory could not be observed.')
			};
		}
	}

	// --- adoption (the explicit admin decision; C09 rule 1/6) ------------------

	let adoptOrigin = $state<ApplicationOrigin | null>(null);
	let adoptName = $state('');
	let adoptApplicationId = $state('');
	let adoptComposeProject = $state('');
	let adoptWorkingDir = $state('');
	let adoptUnitName = $state('');
	let adoptExternalWorkingDir = $state('');
	let adoptExternalConfigFiles = $state('');
	let adoptExternalEnvironmentFiles = $state('');
	let adoptFields = $state<Record<string, string>>({});
	let adoptBusy = $state(false);
	let adoptProblem = $state<{ code: string; message: string } | null>(null);
	let adoptResult = $state<AdoptionCreated | null>(null);
	/** Discovery key of the observation whose adoption is in flight (AC 1 pending). */
	let adoptPendingKey = $state<string | null>(null);

	// --- service map metadata on the adoption decision (optional fields) -----

	/** Selected category machine id; '' = keep the taxonomy default ("system"). */
	let adoptCategory = $state('');
	/** The pre-filled (suggested) category: selecting it is NOT an explicit change. */
	let adoptCategoryInitial = $state('');
	let adoptDescription = $state('');
	let adoptInstructions = $state<Record<RunbookStep, string>>({
		install: '',
		update: '',
		uninstall: '',
		backup: '',
		verify: ''
	});

	/** Catalogue summary of the held snapshot (AC 1: the three states). */
	const catalogue = $derived.by<{ discovered: number; adopted: number; pending: number } | null>(
		() => {
			if (discovery === null) return null;
			let discovered = 0;
			let adopted = 0;
			for (const observed of discovery.applications) {
				if (observed.managed) adopted += 1;
				else discovered += 1;
			}
			// "pending" is the explicit adoption decision currently in flight —
			// an honest in-between state, never a fabricated third resource.
			return { discovered, adopted, pending: adoptBusy ? 1 : 0 };
		}
	);

	/** Discovery lanes sorted alphabetically by lane machine name. */
	const sortedDiscoveryLanes = $derived.by(() => {
		const lanes = discovery?.lanes ?? [];
		return [...lanes].sort((a, b) =>
			a.lane.localeCompare(b.lane, undefined, { numeric: true, sensitivity: 'base' })
		);
	});

	/** Listening TCP ports sorted numerically by port, then alphabetically by address. */
	const sortedListenPorts = $derived.by<{ address: string; port: number }[]>(() => {
		const raw = discovery?.listen_ports ?? [];
		const ports: { address: string; port: number }[] = [];
		for (const item of raw) {
			if (
				typeof item === 'object' &&
				item !== null &&
				typeof item.address === 'string' &&
				typeof item.port === 'number'
			) {
				ports.push({ address: item.address, port: item.port });
			}
		}
		return ports.sort((a, b) => {
			if (a.port !== b.port) return a.port - b.port;
			return a.address.localeCompare(b.address, undefined, { numeric: true, sensitivity: 'base' });
		});
	});

	/** Human-readable lane detail: replaces the producer's `<none>` placeholder
	 *  when a lane succeeded with the actual observed count/summary. */
	function formatLaneDetail(lane: { lane: string; state: string; detail: string }): string {
		if (lane.detail && lane.detail !== '<none>') return lane.detail;
		if (lane.state !== 'observed') return lane.detail || 'No additional detail reported.';
		const apps = discovery?.applications ?? [];
		if (lane.lane === 'docker') {
			const composeCount = apps.filter((a) => a.origin === 'compose').length;
			const containerCount = apps.filter((a) => a.origin === 'docker_container').length;
			return `${composeCount} compose project(s), ${containerCount} standalone container(s) observed`;
		}
		if (lane.lane === 'systemd') {
			const unitCount = apps.filter((a) => a.origin === 'systemd_unit').length;
			return `${unitCount} systemd service unit(s) observed`;
		}
		if (lane.lane === 'listen_ports') {
			const ports = sortedListenPorts;
			if (ports.length === 0) return '0 listening TCP sockets observed';
			const sample = ports
				.slice(0, 8)
				.map((p) => `${p.address}:${p.port}`)
				.join(', ');
			const more = ports.length > 8 ? ` (+${ports.length - 8} more)` : '';
			return `${ports.length} listening TCP socket(s): ${sample}${more}`;
		}
		return 'Observed';
	}

	/** Observed applications filtered by `discoverySearchQuery` and sorted alphabetically by unit/machine name. */
	const filteredDiscoveryApplications = $derived.by<ObservedApplicationView[]>(() => {
		const all = discovery?.applications ?? [];
		const q = discoverySearchQuery.trim().toLowerCase();
		const filtered =
			q === ''
				? all
				: all.filter((observed) => {
						const compose = composeDetail(observed);
						const standalone = standaloneDetail(observed);
						const systemd = systemdDetail(observed);
						const catId = suggestedCategory(observed);
						const statusText = observed.managed ? 'adopted' : 'discovered';
						const haystacks = [
							suggestedName(observed),
							observed.origin,
							originLabel(observed.origin),
							catId,
							categoryLabel(catId),
							statusText,
							observed.application_id ?? '',
							compose?.project_name ?? '',
							compose?.working_dir ?? '',
							...(compose?.services ?? []),
							...(compose?.containers.map((c) => c.name) ?? []),
							standalone?.container.name ?? '',
							standalone?.container.image ?? '',
							standalone?.container.state ?? '',
							systemd?.unit ?? '',
							systemd?.load_state ?? '',
							systemd?.active_state ?? '',
							systemd?.sub_state ?? ''
						];
						return haystacks.some((part) => part.toLowerCase().includes(q));
					});
		return [...filtered].sort((a, b) => {
			const cmp = suggestedName(a).localeCompare(suggestedName(b), undefined, {
				numeric: true,
				sensitivity: 'base'
			});
			if (cmp !== 0) return cmp;
			return observedKey(a).localeCompare(observedKey(b), undefined, {
				numeric: true,
				sensitivity: 'base'
			});
		});
	});

	/** Pre-fill the adoption form FROM the observation (paths stay referenced). */
	function startAdoption(observed: ObservedApplicationView): void {
		// The wire vocabulary is the generated enum; an unknown origin string
		// cannot be adopted (fail closed, server re-validates regardless).
		const origin = ORIGINS.includes(observed.origin as ApplicationOrigin)
			? (observed.origin as ApplicationOrigin)
			: null;
		if (origin === null) return;
		const compose = composeDetail(observed);
		const systemd = systemdDetail(observed);
		adoptOrigin = origin;
		adoptName = suggestedName(observed);
		adoptApplicationId = '';
		adoptComposeProject = compose?.project_name ?? '';
		adoptWorkingDir = compose?.working_dir ?? '';
		adoptUnitName = systemd?.unit ?? '';
		// External deployment paths arrive as the observation reported them
		// (labels/`ss` only); an absent path stays absent — never a guess.
		adoptExternalWorkingDir = compose?.working_dir ?? '';
		adoptExternalConfigFiles = (compose?.config_files ?? []).join(', ');
		adoptExternalEnvironmentFiles = (compose?.environment_files ?? []).join(', ');
		// Service Map metadata starts empty: the suggested category is offered
		// as the pre-selection, but only an EXPLICIT change ships in the body.
		adoptCategory = suggestedCategory(observed);
		adoptCategoryInitial = adoptCategory;
		adoptDescription = '';
		adoptInstructions = { install: '', update: '', uninstall: '', backup: '', verify: '' };
		adoptFields = {};
		adoptProblem = null;
		adoptResult = null;
		adoptPendingKey = observedKey(observed);
		tab = 'adopt';
	}

	function cancelAdoption(): void {
		adoptOrigin = null;
		adoptFields = {};
		adoptProblem = null;
		adoptResult = null;
		adoptPendingKey = null;
	}

	function clearAdoptResult(): void {
		adoptResult = null;
	}

	/** Comma/newline separated path list → bounded non-empty entries. */
	function parsePathList(value: string): string[] {
		return value
			.split(/[\n,]/)
			.map((entry) => entry.trim())
			.filter((entry) => entry !== '');
	}

	function orNull(value: string): string | null {
		const trimmed = value.trim();
		return trimmed === '' ? null : trimmed;
	}

	const UUID_RE =
		/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

	/** The generated adoption origin vocabulary (adopt producer enum). */
	const ORIGINS: readonly string[] = Object.freeze([
		'compose',
		'docker_container',
		'systemd_unit'
	]);

	function validateAdopt(): Record<string, string> {
		const errors: Record<string, string> = {};
		const id = adoptApplicationId.trim();
		if (!UUID_RE.test(id)) errors.application_id = 'An application id (canonical UUID) is required.';
		const name = adoptName.trim();
		if (name === '') errors.name = 'A display name is required.';
		else if (name.length > MAX_NAME_CHARS)
			errors.name = `The name is bounded to ${MAX_NAME_CHARS} characters.`;
		const selectors: Array<[string, string]> = [
			['compose_project', adoptComposeProject],
			['working_dir', adoptWorkingDir],
			['unit_name', adoptUnitName]
		];
		for (const [field, value] of selectors) {
			if (value.trim().length > MAX_SELECTOR_CHARS)
				errors[field] = `Bounded to ${MAX_SELECTOR_CHARS} characters.`;
		}
		if (adoptDescription.length > CATEGORY_DESC_MAX)
			errors.description = `Bounded to ${CATEGORY_DESC_MAX} characters.`;
		if (adoptExternalWorkingDir.trim().length > MAX_PATH_CHARS)
			errors.external_working_dir = `Bounded to ${MAX_PATH_CHARS} characters.`;
		for (const entry of parsePathList(adoptExternalConfigFiles)) {
			if (entry.length > MAX_PATH_CHARS) {
				errors.external_config_files = `Every entry is bounded to ${MAX_PATH_CHARS} characters.`;
				break;
			}
		}
		for (const entry of parsePathList(adoptExternalEnvironmentFiles)) {
			if (entry.length > MAX_PATH_CHARS) {
				errors.external_environment_files = `Every entry is bounded to ${MAX_PATH_CHARS} characters.`;
				break;
			}
		}
		return errors;
	}

	async function submitAdoption(): Promise<void> {
		if (api === null || serverScope === null || adoptOrigin === null || adoptBusy) return;
		const scope = serverScope;
		const errors = validateAdopt();
		adoptFields = errors;
		if (Object.keys(errors).length > 0) return;
		const body: AdoptBody = {
			application_id: adoptApplicationId.trim(),
			name: adoptName.trim(),
			origin: adoptOrigin,
			compose_project: orNull(adoptComposeProject),
			working_dir: orNull(adoptWorkingDir),
			unit_name: orNull(adoptUnitName),
			external_working_dir: orNull(adoptExternalWorkingDir),
			external_config_files: parsePathList(adoptExternalConfigFiles),
			external_environment_files: parsePathList(adoptExternalEnvironmentFiles),
			expected_revision: 0
		};
		// The Service Map enrichments ride the request ONLY when explicitly
		// set, so an untouched form keeps the reviewed create payload exact
		// (the strict wire assertion in applications.spec.ts pins this).
		if (adoptCategory !== '' && adoptCategory !== adoptCategoryInitial) {
			body.category = adoptCategory;
		}
		if (adoptDescription.trim() !== '') body.description = adoptDescription.trim();
		const steps = Object.fromEntries(
			RUNBOOK_STEPS.map((step) => [step, adoptInstructions[step].trim()])
		) as Record<RunbookStep, string>;
		if (RUNBOOK_STEPS.some((step) => steps[step] !== '')) body.instructions = steps;
		if (customCategories.length > 0) {
			body.upsert_categories = customCategories.map((entry) => ({
				machine_id: entry.machine_id,
				label: entry.label,
				description: entry.description
			}));
		}
		adoptBusy = true;
		adoptProblem = null;
		try {
			const created = await api.adopt(scope, body);
			if (disposed || scope !== serverScope) return;
			// Exact server identity (AC 1): the commit must name the pinned scope.
			if (created.server_id !== scope) {
				adoptProblem = {
					code: 'scope_mismatch',
					message: 'The adoption answer names a different server than this window\'s pinned scope.'
				};
				return;
			}
			adoptResult = created;
			recordDocs = { ...recordDocs, [created.application.application_id]: created.record_doc };
			// The managed truth changed: refresh the page; the held snapshot is
			// now older than this decision (observe again for fresh tagging).
			snapshotStale = true;
			void loadInventory();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			adoptProblem = {
				code,
				message:
					code === 'conflict'
						? 'No adoption was committed: an adoption already exists for this reference, or the identity/selector conflicts with an existing record.'
						: friendlyText(code, messageOf(error) ?? 'The adoption could not be committed.')
			};
		} finally {
			if (!disposed) {
				adoptBusy = false;
				adoptPendingKey = null;
			}
		}
	}

	// --- lifecycle + documents seams (links, never reimplementations) ---------

	function openInstall(row: ApplicationView): void {
		if (lifecycle === null || serverScope === null) return;
		lifecycle.openInstall(serverScope, row.application_id);
	}

	function openUpdate(row: ApplicationView): void {
		if (lifecycle === null || serverScope === null) return;
		lifecycle.openUpdate(serverScope, row.application_id);
	}

	function openRemove(row: ApplicationView): void {
		if (lifecycle === null || serverScope === null) return;
		lifecycle.openRemove(serverScope, row.application_id);
	}

	function openRecordDoc(docId: string): void {
		if (docs === null) return;
		docs.openDocument(docId);
	}

	// --- one-click chat tag (the bottom dock listens on the window) ----------

	/**
	 * Tags this server + `@service` in the Bottom Chat Dock: a window-level
	 * CustomEvent, nothing else — the dock owns the focus mechanics (the
	 * panel never reaches into another surface's state).
	 */
	function chatAboutService(row: ApplicationView): void {
		if (serverScope === null) return;
		window.dispatchEvent(
			new CustomEvent('mai:chat-focus-service', {
				detail: {
					serverId: serverScope,
					serviceName: row.name,
					applicationId: row.application_id,
					category: mappedCategory(row)
				}
			})
		);
	}

	// --- service map editing (revision-checked re-adoption of metadata) ------

	let editTarget = $state<ApplicationView | null>(null);
	let editCategory = $state('');
	let editDescription = $state('');
	let editInstructions = $state<Record<RunbookStep, string>>({
		install: '',
		update: '',
		uninstall: '',
		backup: '',
		verify: ''
	});
	let editBusy = $state(false);
	let editProblem = $state<{ code: string; message: string } | null>(null);

	function emptyInstructions(): Record<RunbookStep, string> {
		return { install: '', update: '', uninstall: '', backup: '', verify: '' };
	}

	/** Open the inline editor pre-filled with the record's stored map metadata. */
	function startEdit(row: ApplicationView): void {
		editTarget = row;
		editCategory = mappedCategory(row);
		editDescription = mappedDescription(row);
		const recorded = mappedInstructions(row);
		editInstructions = emptyInstructions();
		if (recorded !== null) {
			for (const step of RUNBOOK_STEPS) {
				const text = recorded[step];
				if (typeof text === 'string') editInstructions[step] = text;
			}
		}
		editProblem = null;
	}

	function cancelEdit(): void {
		editTarget = null;
		editProblem = null;
	}

	/**
	 * Persist the edited map metadata through the ONE reviewed mutation: a
	 * revision-checked re-adoption of the same identity (expected_revision =
	 * the observed revision), carrying only metadata differences. A stale
	 * revision is the producer's conflict, surfaced verbatim.
	 */
	async function saveEdit(): Promise<void> {
		const target = editTarget;
		if (api === null || serverScope === null || target === null || editBusy) return;
		const scope = serverScope;
		// The wire vocabulary is the generated enum plus the manual map entry;
		// a foreign origin string cannot be re-adopted (fail closed).
		const origin = ORIGINS.includes(target.origin) ? (target.origin as AdoptBody['origin']) : null;
		if (origin === null) {
			editProblem = {
				code: 'validation_failed',
				message: 'This record\'s origin cannot be updated through the map.'
			};
			return;
		}
		const steps = emptyInstructions();
		for (const step of RUNBOOK_STEPS) steps[step] = editInstructions[step].trim();
		const body: AdoptBody = {
			application_id: target.application_id,
			name: target.name,
			origin,
			compose_project: target.compose_project,
			working_dir: target.working_dir,
			unit_name: target.unit_name,
			external_working_dir: target.external_working_dir,
			external_config_files: [...target.external_config_files],
			external_environment_files: [...target.external_environment_files],
			expected_revision: target.revision,
			category: editCategory,
			description: editDescription.trim(),
			instructions: steps
		};
		editBusy = true;
		editProblem = null;
		try {
			const updated = await api.adopt(scope, body);
			if (disposed || scope !== serverScope) return;
			if (updated.server_id !== scope) {
				editProblem = {
					code: 'scope_mismatch',
					message: 'The map update names a different server than this window\'s pinned scope.'
				};
				return;
			}
			applications = applications.map((row) =>
				row.application_id === updated.application.application_id ? updated.application : row
			);
			editTarget = null;
			snapshotStale = true;
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			editProblem = {
				code,
				message:
					code === 'conflict'
						? 'The record changed at another revision; refresh the inventory and reapply.'
						: friendlyText(code, messageOf(error) ?? 'The map details could not be saved.')
			};
		} finally {
			if (!disposed) editBusy = false;
		}
	}

	// --- custom taxonomy minting (rides the next adoption) -------------------

	let categoryIdInput = $state('');
	let categoryLabelInput = $state('');
	let categoryDescInput = $state('');
	let categoryFields = $state<Record<string, string>>({});

	const CATEGORY_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
	const CATEGORY_DESC_MAX = 2000;

	function validateCategory(): Record<string, string> {
		const errors: Record<string, string> = {};
		const machineId = categoryIdInput.trim();
		if (!CATEGORY_ID_RE.test(machineId))
			errors.machine_id = 'A machine id: lowercase letters, digits, - and _ (1–64 characters).';
		else if (taxonomy.some((entry) => entry.machine_id === machineId))
			errors.machine_id = 'This machine id already exists in the taxonomy.';
		if (categoryLabelInput.trim() === '') errors.label = 'A human-readable label is required.';
		if (categoryDescInput.length > CATEGORY_DESC_MAX)
			errors.description = `Bounded to ${CATEGORY_DESC_MAX} characters.`;
		return errors;
	}

	/**
	 * Mint one custom category in this window: it joins every picker and
	 * chip immediately and persists through the next adoption's
	 * `upsert_categories` (the one reviewed mutation that carries taxonomy).
	 */
	function saveCategory(): void {
		const errors = validateCategory();
		categoryFields = errors;
		if (Object.keys(errors).length > 0) return;
		customCategories = [
			...customCategories,
			{
				machine_id: categoryIdInput.trim(),
				label: categoryLabelInput.trim(),
				description: categoryDescInput.trim(),
				built_in: false
			}
		];
		categoryIdInput = '';
		categoryLabelInput = '';
		categoryDescInput = '';
		categoryFields = {};
	}

	// --- mount cycle (the $effect teardown is the release path) ---------------

	$effect(() => {
		if (!usable) return;
		disposed = false;
		void loadInventory();
		return () => {
			disposed = true;
		};
	});

	onDestroy(() => {
		disposed = true;
	});
</script>

<div class="mai-apps" data-testid="apps-root">
	{#if !usable}
		<div class="mai-apps__state mai-apps__state--error" role="alert" data-testid="panel-config-error">
			<p>This panel was opened without its applications service or outside a pinned server scope.</p>
		</div>
	{:else}
		<header class="mai-apps__header">
			<h3 class="mai-apps__title">Applications</h3>
			<p class="mai-apps__meta">
				<span data-testid="apps-scope">Server {shortId(serverScope ?? '')}</span>
				<span title={serverScope ?? ''}>Pinned scope — every request below names this target.</span>
			</p>
		</header>

		<nav class="mai-apps__tabs" aria-label="Applications sections" data-testid="apps-tabs">
			{#each TABS as entry (entry.id)}
				<button
					type="button"
					class="mai-apps__tab"
					class:mai-apps__tab--active={tab === entry.id}
					aria-pressed={tab === entry.id}
					onclick={() => (tab = entry.id)}
					data-testid="tab-{entry.id}"
				>
					{entry.label}
				</button>
			{/each}
		</nav>

		{#if tab === 'inventory'}
			<section class="mai-apps__section" aria-label="Adopted applications" data-testid="apps-inventory">
				<div class="mai-apps__section-head">
					<h4>Inventory</h4>
					<span class="mai-apps__meta">Adopted records of the pinned server — the managed truth.</span>
					<Button variant="secondary" onclick={loadInventory} data-testid="inventory-refresh">
						Refresh
					</Button>
				</div>
				{#if inventoryState === 'loading'}
					<div class="mai-apps__state" role="status" data-testid="inventory-loading">
						<p>Loading adopted applications…</p>
					</div>
				{:else if inventoryState === 'error'}
					<div
						class="mai-apps__state mai-apps__state--error"
						role="alert"
						data-testid="inventory-error"
						data-error-code={inventoryProblem?.code}
					>
						<p>{inventoryProblem?.message}</p>
						<Button variant="secondary" onclick={loadInventory} data-testid="inventory-retry">
							Retry
						</Button>
					</div>
				{:else if applications.length === 0}
					<div class="mai-apps__state" role="status" data-testid="inventory-empty">
						<p>No adopted applications are recorded for this server. Discovery never adopts —
							use the Discovery tab and adopt explicitly.</p>
					</div>
				{:else}
					<div class="mai-apps__toolbar">
						<p class="mai-apps__meta" data-testid="inventory-count">
							Showing {applications.length} of {inventoryTotal} adopted.
						</p>
						<SearchFilterInput
							bind:value={inventorySearchQuery}
							label="Search adopted applications"
							placeholder="Filter inventory by unit, name, category, path, or id…"
							resultCount={filteredApplications.length}
							totalCount={applications.length}
							testId="inventory-search"
						/>
					</div>
					{#if presentCategories.length > 0}
						<div
							class="mai-apps__filterbar"
							role="group"
							aria-label="Filter mapped services by category"
							data-testid="category-filter-bar"
						>
							<button
								type="button"
								class="mai-apps__pill"
								class:mai-apps__pill--active={categoryFilter === 'all'}
								aria-pressed={categoryFilter === 'all'}
								onclick={() => (categoryFilter = 'all')}
								data-testid="category-filter-pill"
								data-category="all"
							>
								All
							</button>
							{#each presentCategories as machineId (machineId)}
								<button
									type="button"
									class="mai-apps__pill"
									class:mai-apps__pill--active={categoryFilter === machineId}
									aria-pressed={categoryFilter === machineId}
									onclick={() => (categoryFilter = machineId)}
									data-testid="category-filter-pill"
									data-category={machineId}
								>
									{categoryLabel(machineId)}
								</button>
							{/each}
						</div>
					{/if}
					{#if filteredApplications.length === 0}
						<div class="mai-apps__state" role="status" data-testid="inventory-filtered-empty">
							<p>No mapped services in this category.</p>
						</div>
					{/if}
					<ul class="mai-apps__rows" data-testid="inventory-list">
						{#each filteredApplications as row (row.application_id)}
							<li
								class="mai-apps__row"
								class:mai-apps__row--pinned={row.application_id === pinnedApplicationId}
								data-testid="inventory-row"
								data-app-id={row.application_id}
								data-pinned={row.application_id === pinnedApplicationId ? 'true' : 'false'}
							>
								<div class="mai-apps__row-main">
									<span class="mai-apps__row-id" data-testid="inventory-row-id" title={row.application_id}>
										{shortId(row.application_id)}
									</span>
									<span class="mai-apps__label" data-testid="inventory-row-name">{row.name}</span>
									<span class="mai-apps__badge" data-testid="inventory-row-origin">
										{originLabel(row.origin)}
									</span>
									<span
										class="mai-apps__chip"
										data-testid="service-category-chip"
										data-category={mappedCategory(row)}
									>
										{categoryLabel(mappedCategory(row))}
									</span>
									{#if row.stale}
										<span
											class="mai-apps__badge mai-apps__badge--warn"
											data-testid="inventory-row-stale"
										>
											drift observed — reconcile pending
										</span>
									{/if}
									{#if recordDocs[row.application_id]?.status === 'pending'}
										<span
											class="mai-apps__badge mai-apps__badge--warn"
											data-testid="inventory-row-doc-pending"
										>
											record doc pending ({recordDocs[row.application_id]?.reason ?? 'unknown'})
										</span>
									{/if}
								</div>
								<dl class="mai-apps__facts" data-testid="inventory-row-facts">
									<dt>Identity</dt>
									<dd data-testid="inventory-row-identity">{row.application_id}</dd>
									<dt>Server</dt>
									<dd data-testid="inventory-row-server">{row.server_id}</dd>
									{#if row.compose_project !== null}
										<dt>Compose project</dt>
										<dd data-testid="inventory-row-compose">{row.compose_project}</dd>
									{/if}
									{#if row.working_dir !== null}
										<dt>Working dir</dt>
										<dd data-testid="inventory-row-working-dir">{row.working_dir}</dd>
									{/if}
									{#if row.unit_name !== null}
										<dt>Unit</dt>
										<dd data-testid="inventory-row-unit">{row.unit_name}</dd>
									{/if}
									{#if row.external_working_dir !== null}
										<dt>External working dir</dt>
										<dd data-testid="inventory-row-external-dir">{row.external_working_dir}</dd>
									{/if}
									{#if row.external_config_files.length > 0}
										<dt>External config files</dt>
										<dd data-testid="inventory-row-config-files">
											{row.external_config_files.join(', ')}
										</dd>
									{/if}
									{#if row.external_environment_files.length > 0}
										<dt>External env files</dt>
										<dd data-testid="inventory-row-env-files">
											{row.external_environment_files.join(', ')}
										</dd>
									{/if}
									<dt>Canonical record</dt>
									<dd>
										<span data-testid="inventory-row-revision">r{row.revision}</span>
										·
										<span data-testid="inventory-row-sha">{shortHash(row.sha256)}</span>
										· schema v{row.schema_revision}
									</dd>
									<dt>Adopted</dt>
									<dd data-testid="inventory-row-adopted-at">{row.adopted_at}</dd>
								</dl>
								{#if mappedDescription(row) !== ''}
									<p class="mai-apps__description" data-testid="service-description">
										{mappedDescription(row)}
									</p>
								{/if}
								<details class="mai-apps__runbook" data-testid="service-instructions">
									<summary>Operational runbook</summary>
									{#if recordedSteps(mappedInstructions(row)).length === 0}
										<p class="mai-apps__note">No runbook instructions recorded for this service.</p>
									{:else}
										<dl class="mai-apps__facts">
											{#each recordedSteps(mappedInstructions(row)) as [step, text] (step)}
												<dt>{step}</dt>
												<dd data-testid="runbook-step" data-step={step}>{text}</dd>
											{/each}
										</dl>
									{/if}
								</details>
								{#if editTarget?.application_id === row.application_id}
									<div class="mai-apps__editor" data-testid="service-edit-form">
										<label class="mai-apps__select-label">
											Category
											<select
												class="mai-apps__select"
												bind:value={editCategory}
												data-testid="service-edit-category"
											>
												{#each taxonomy as entry (entry.machine_id)}
													<option value={entry.machine_id}>{entry.label}</option>
												{/each}
											</select>
										</label>
										<Field
											label="Description"
											bind:value={editDescription}
											hint="What this service is for (bounded to 2000 characters)."
											data-testid="service-edit-description"
										/>
										<fieldset class="mai-apps__fieldset">
											<legend>Runbook instructions</legend>
											{#each RUNBOOK_STEPS as step (step)}
												<Field
													label={step}
													bind:value={editInstructions[step]}
													hint="Optional command or note for the {step} procedure."
													data-testid="service-edit-instructions-{step}"
												/>
											{/each}
										</fieldset>
										<div class="mai-apps__actions">
											<Button onclick={saveEdit} loading={editBusy} data-testid="service-edit-save">
												Save map details
											</Button>
											<Button variant="secondary" onclick={cancelEdit} data-testid="service-edit-cancel">
												Cancel
											</Button>
										</div>
										{#if editProblem !== null}
											<div
												class="mai-apps__state mai-apps__state--error"
												role="alert"
												data-testid="service-edit-error"
												data-error-code={editProblem.code}
											>
												<p>{editProblem.message}</p>
											</div>
										{/if}
									</div>
								{/if}
								<div class="mai-apps__row-actions">
									<Button
										variant="secondary"
										onclick={() => chatAboutService(row)}
										data-testid="service-chat-trigger"
									>
										Chat @{row.name}
									</Button>
									<Button
										variant="secondary"
										onclick={() => startEdit(row)}
										data-testid="service-edit-start"
									>
										Map details…
									</Button>
									<Button
										variant="secondary"
										onclick={() => openInstall(row)}
										disabled={lifecycle === null}
										data-testid="install-link"
									>
										Install…
									</Button>
									<Button
										variant="secondary"
										onclick={() => openUpdate(row)}
										disabled={lifecycle === null}
										data-testid="update-link"
									>
										Update…
									</Button>
									<Button
										variant="secondary"
										onclick={() => openRemove(row)}
										disabled={lifecycle === null}
										data-testid="remove-link"
									>
										Remove…
									</Button>
									{#if recordDocs[row.application_id]?.status === 'documented' &&
										recordDocs[row.application_id]?.doc_id}
										<Button
											variant="secondary"
											onclick={() => openRecordDoc(recordDocs[row.application_id]?.doc_id ?? '')}
											disabled={docs === null}
											data-testid="doc-open"
										>
											Record document…
										</Button>
									{/if}
									<Button variant="danger" onclick={() => startRevoke(row)} data-testid="revoke-start">
										Revoke adoption…
									</Button>
								</div>
							</li>
						{/each}
					</ul>
					{#if lifecycle === null}
						<p class="mai-apps__note" data-testid="lifecycle-unavailable">
							The install/update/remove surfaces are not wired into this composition; the links
							above stay inert instead of faking a submission.
						</p>
					{/if}
				{/if}

				{#if revokeTarget !== null}
					<div class="mai-apps__confirm" data-testid="revoke-confirm">
						<p>
							Revoke the adoption of
							<strong data-testid="revoke-confirm-name">{revokeTarget?.name}</strong>
							(<span data-testid="revoke-confirm-id">{shortId(revokeTarget?.application_id ?? null)}</span>)?
							Only the canonical manifest pair is removed at revision r{revokeTarget?.revision};
							the external deployment paths are never touched.
						</p>
						<div class="mai-apps__actions">
							<Button variant="danger" onclick={confirmRevoke} loading={revokeBusy} data-testid="revoke-accept">
								Confirm revoke
							</Button>
							<Button variant="secondary" onclick={cancelRevoke} data-testid="revoke-cancel">
								Cancel
							</Button>
						</div>
						{#if revokeProblem !== null}
							<div
								class="mai-apps__state mai-apps__state--error"
								role="alert"
								data-testid="revoke-error"
								data-error-code={revokeProblem.code}
							>
								<p>{revokeProblem.message}</p>
							</div>
						{/if}
					</div>
				{/if}
				{#if revokeDoneId !== null}
					<p class="mai-apps__note" role="status" data-testid="revoke-done">
						Adoption {shortId(revokeDoneId)} revoked — the canonical record is gone, the external
						deployment was never touched.
					</p>
				{/if}
			</section>

		{:else if tab === 'discovery'}
			<section class="mai-apps__section" aria-label="Observed inventory" data-testid="apps-discovery">
				<div class="mai-apps__section-head">
					<h4>Discovery</h4>
					<span class="mai-apps__meta">Read-only observation; it never adopts or enables anything.</span>
					<Button
						variant="secondary"
						onclick={observeInventory}
						loading={discoveryState === 'loading'}
						data-testid="discovery-observe"
					>
						Observe inventory
					</Button>
				</div>
				{#if discoveryState === 'idle'}
					<div class="mai-apps__state" role="status" data-testid="discovery-idle">
						<p>No snapshot held for this window yet.</p>
					</div>
				{:else if discoveryState === 'loading'}
					<div class="mai-apps__state" role="status" data-testid="discovery-loading">
						<p>Observing the pinned server through the read-only lanes…</p>
					</div>
				{:else if discoveryState === 'error'}
					<div
						class="mai-apps__state mai-apps__state--error"
						role="alert"
						data-testid="discovery-error"
						data-error-code={discoveryProblem?.code}
					>
						<p>{discoveryProblem?.message}</p>
						<Button variant="secondary" onclick={observeInventory} data-testid="discovery-retry">
							Retry
						</Button>
					</div>
				{:else if discovery !== null}
					<p class="mai-apps__meta" data-testid="discovery-meta">
						<span>Observed {discovery?.observed_at}</span>
						<span data-testid="discovery-ports">{discovery?.listen_ports.length ?? 0} listen ports</span>
					</p>
					{#if snapshotStale}
						<p class="mai-apps__note" role="status" data-testid="discovery-stale">
							The latest adoption change happened after this snapshot — observe again for fresh
							managed tagging.
						</p>
					{/if}
					<div class="mai-apps__catalogue" data-testid="apps-catalogue">
						<span class="mai-apps__badge" data-testid="catalogue-discovered">
							{catalogue?.discovered ?? 0} discovered
						</span>
						<span class="mai-apps__badge mai-apps__badge--ok" data-testid="catalogue-adopted">
							{catalogue?.adopted ?? 0} adopted
						</span>
						<span class="mai-apps__badge mai-apps__badge--warn" data-testid="catalogue-pending">
							{catalogue?.pending ?? 0} pending
						</span>
					</div>
					<div class="mai-apps__subsection">
						<h5>Lanes</h5>
						<ul class="mai-apps__rows" data-testid="discovery-lanes">
							{#each sortedDiscoveryLanes as lane (lane.lane)}
								<li class="mai-apps__row" data-testid="lane-row">
									<div class="mai-apps__row-main">
										<span class="mai-apps__label" data-testid="lane-name">{lane.lane}</span>
										<span
											class="mai-apps__badge"
											class:mai-apps__badge--ok={laneBadge(lane.state) === 'ok'}
											class:mai-apps__badge--warn={laneBadge(lane.state) === 'warn'}
											class:mai-apps__badge--danger={laneBadge(lane.state) === 'danger'}
											data-testid="lane-state"
										>
											{lane.state}
										</span>
										{#if lane.truncated}
											<span class="mai-apps__badge mai-apps__badge--warn" data-testid="lane-truncated">
												output truncated
											</span>
										{/if}
										{#if lane.malformed_lines > 0}
											<span class="mai-apps__badge mai-apps__badge--warn" data-testid="lane-malformed">
												{lane.malformed_lines} malformed lines
											</span>
										{/if}
									</div>
									<p class="mai-apps__note" data-testid="lane-detail">{formatLaneDetail(lane)}</p>
								</li>
							{/each}
						</ul>
					</div>
					{#if sortedListenPorts.length > 0}
						<div class="mai-apps__subsection" data-testid="discovery-ports-section">
							<h5>Listening TCP ports ({sortedListenPorts.length})</h5>
							<div class="mai-apps__port-chips">
								{#each sortedListenPorts as entry (`${entry.address}:${entry.port}`)}
									<span class="mai-apps__badge mai-apps__mono" data-testid="discovery-port-chip">
										{entry.address}:{entry.port}
									</span>
								{/each}
							</div>
						</div>
					{/if}
					<div class="mai-apps__subsection">
						<div class="mai-apps__toolbar">
							<h5>Observed applications</h5>
							{#if (discovery?.applications.length ?? 0) > 0}
								<SearchFilterInput
									bind:value={discoverySearchQuery}
									label="Search discovered applications"
									placeholder="Filter discovery by unit, project, container, image, or category…"
									resultCount={filteredDiscoveryApplications.length}
									totalCount={discovery?.applications.length ?? 0}
									testId="discovery-search"
								/>
							{/if}
						</div>
						{#if (discovery?.applications.length ?? 0) === 0}
							<div class="mai-apps__state" role="status" data-testid="discovery-empty">
								<p>No applications were observed in this snapshot. Unavailable lanes contributed
									nothing — their states are reported above, never inferred.</p>
							</div>
						{:else if filteredDiscoveryApplications.length === 0}
							<div class="mai-apps__state" role="status" data-testid="discovery-filtered-empty">
								<p>No observed applications match "{discoverySearchQuery}".</p>
							</div>
						{:else}
							<ul class="mai-apps__rows" data-testid="discovery-list">
								{#each filteredDiscoveryApplications as observed (observedKey(observed))}
									<li class="mai-apps__row" data-testid="discovery-row">
										<div class="mai-apps__row-main">
											{#if observed.managed}
												<span class="mai-apps__badge mai-apps__badge--ok" data-testid="discovery-row-status">
													adopted
												</span>
											{:else if adoptBusy && adoptPendingKey === observedKey(observed)}
												<span class="mai-apps__badge mai-apps__badge--warn" data-testid="discovery-row-status">
													pending
												</span>
											{:else}
												<span class="mai-apps__badge" data-testid="discovery-row-status">
													discovered
												</span>
											{/if}
											<span class="mai-apps__badge" data-testid="discovery-row-origin">
												{originLabel(observed.origin)}
											</span>
											<span
												class="mai-apps__chip mai-apps__chip--suggested"
												data-testid="discovery-category-chip"
												data-category={suggestedCategory(observed)}
												title="Suggested category — the adoption decision may override it."
											>
												{categoryLabel(suggestedCategory(observed))}
											</span>
											{#if observed.managed && observed.application_id !== null}
												<span
													class="mai-apps__meta"
													data-testid="discovery-row-managed-id"
													title={observed.application_id}
												>
													managed as {shortId(observed.application_id)}
												</span>
											{/if}
										</div>
										<dl class="mai-apps__facts" data-testid="discovery-row-facts">
											{#if composeDetail(observed) !== null}
												<dt>Project</dt>
												<dd data-testid="discovery-row-project">{composeDetail(observed)?.project_name}</dd>
												<dt>Working dir</dt>
												<dd data-testid="discovery-row-working-dir">
													{composeDetail(observed)?.working_dir ?? 'not reported'}
												</dd>
												<dt>Services</dt>
												<dd>{composeDetail(observed)?.services.join(', ')}</dd>
												<dt>Containers</dt>
												<dd>
													{composeDetail(observed)?.containers.length ?? 0}
													({composeDetail(observed)?.containers.map((c) => c.name).join(', ')})
												</dd>
											{/if}
											{#if standaloneDetail(observed) !== null}
												<dt>Container</dt>
												<dd data-testid="discovery-row-container">
													{standaloneDetail(observed)?.container.name}
													({standaloneDetail(observed)?.container.image},
													{standaloneDetail(observed)?.container.state})
												</dd>
											{/if}
											{#if systemdDetail(observed) !== null}
												<dt>Unit</dt>
												<dd data-testid="discovery-row-unit">{systemdDetail(observed)?.unit}</dd>
												<dt>States</dt>
												<dd>
													{systemdDetail(observed)?.load_state ?? 'not loaded'} /
													{systemdDetail(observed)?.active_state ?? '—'} /
													{systemdDetail(observed)?.sub_state ?? '—'}
												</dd>
											{/if}
										</dl>
										<div class="mai-apps__row-actions">
											{#if observed.managed}
												<Button
													variant="secondary"
													onclick={() => (tab = 'inventory')}
													data-testid="inventory-goto"
												>
													Open in inventory
												</Button>
											{:else}
												<Button
													variant="secondary"
													onclick={() => startAdoption(observed)}
													data-testid="adopt-start"
												>
													Adopt…
												</Button>
											{/if}
										</div>
									</li>
								{/each}
							</ul>
						{/if}
					</div>
				{/if}
			</section>

		{:else if tab === 'adopt'}
			<section class="mai-apps__section" aria-label="Explicit adoption" data-testid="apps-adopt">
				<div class="mai-apps__section-head">
					<h4>Adopt</h4>
					<span class="mai-apps__meta">
						An explicit admin decision (C09 rule 1): external deployment paths are recorded as
						references, never moved or touched.
					</span>
				</div>
				{#if adoptOrigin === null}
					<div class="mai-apps__state" role="status" data-testid="adopt-no-selection">
						<p>Start from the Discovery tab: adopt one observed application explicitly.</p>
					</div>
				{:else}
					<div class="mai-apps__form">
						<p class="mai-apps__meta" data-testid="adopt-origin">
							Origin: {adoptOrigin === null ? '' : originLabel(adoptOrigin)} — carried verbatim from
							the observation.
						</p>
						<Field
							label="Application id (admin-minted UUID)"
							bind:value={adoptApplicationId}
							error={adoptFields.application_id ?? ''}
							hint="Opaque identity you mint for this application (C01 rule 1); reusing it on another server conflicts."
							data-testid="adopt-application-id"
						/>
						<Field
							label="Name"
							bind:value={adoptName}
							error={adoptFields.name ?? ''}
							hint="Display name, 1–{MAX_NAME_CHARS} printable characters."
							data-testid="adopt-name"
						/>
						<label class="mai-apps__select-label">
							Category
							<select
								class="mai-apps__select"
								bind:value={adoptCategory}
								data-testid="adopt-category-select"
							>
								{#each taxonomy as entry (entry.machine_id)}
									<option value={entry.machine_id}>{entry.label}</option>
								{/each}
							</select>
							<span class="mai-apps__note">
								Pre-selected from the observation's suggestion; only an explicit change is sent
								with the adoption.
							</span>
						</label>
						<Field
							label="Description (optional)"
							bind:value={adoptDescription}
							error={adoptFields.description ?? ''}
							hint="What this service is for; bounded to 2000 characters."
							data-testid="adopt-description-input"
						/>
						<fieldset class="mai-apps__fieldset">
							<legend>Runbook instructions (optional)</legend>
							{#each RUNBOOK_STEPS as step (step)}
								<Field
									label={step}
									bind:value={adoptInstructions[step]}
									hint="Optional command or note for the {step} procedure."
									data-testid="adopt-instructions-{step}"
								/>
							{/each}
						</fieldset>
						{#if adoptOrigin === 'compose'}
							<Field
								label="Compose project"
								bind:value={adoptComposeProject}
								error={adoptFields.compose_project ?? ''}
								hint="Selector recorded from the observation; bounded to {MAX_SELECTOR_CHARS} characters."
								data-testid="adopt-compose-project"
							/>
							<Field
								label="Working dir"
								bind:value={adoptWorkingDir}
								error={adoptFields.working_dir ?? ''}
								hint="The project stays at its original path (C09 rule 6); bounded to {MAX_SELECTOR_CHARS} characters."
								data-testid="adopt-working-dir"
							/>
						{/if}
						{#if adoptOrigin === 'systemd_unit'}
							<Field
								label="Unit name"
								bind:value={adoptUnitName}
								error={adoptFields.unit_name ?? ''}
								hint="Selector recorded from the observation; bounded to {MAX_SELECTOR_CHARS} characters."
								data-testid="adopt-unit-name"
							/>
						{/if}
						<Field
							label="External working dir (optional)"
							bind:value={adoptExternalWorkingDir}
							error={adoptFields.external_working_dir ?? ''}
							hint="Referenced deployment path outside MAI's store; bounded to {MAX_PATH_CHARS} characters."
							data-testid="adopt-external-dir"
						/>
						<Field
							label="External config files (optional, comma/newline separated)"
							bind:value={adoptExternalConfigFiles}
							error={adoptFields.external_config_files ?? ''}
							hint="Referenced compose/config files; every entry bounded to {MAX_PATH_CHARS} characters."
							data-testid="adopt-config-files"
						/>
						<Field
							label="External environment files (optional, comma/newline separated)"
							bind:value={adoptExternalEnvironmentFiles}
							error={adoptFields.external_environment_files ?? ''}
							hint="Referenced env files (paths only — contents are never read here); every entry bounded to {MAX_PATH_CHARS} characters."
							data-testid="adopt-env-files"
						/>
						<div class="mai-apps__actions">
							<Button onclick={submitAdoption} loading={adoptBusy} data-testid="adopt-submit">
								Commit adoption
							</Button>
							<Button variant="secondary" onclick={cancelAdoption} data-testid="adopt-cancel">
								Cancel
							</Button>
						</div>
					</div>
					{#if adoptProblem !== null}
						<div
							class="mai-apps__state mai-apps__state--error"
							role="alert"
							data-testid="adopt-error"
							data-error-code={adoptProblem.code}
						>
							<p>{adoptProblem.message}</p>
						</div>
					{/if}
					{#if adoptResult !== null}
						<div class="mai-apps__confirm" data-testid="adopt-result">
							<p>
								Adoption committed — outcome
								<span class="mai-apps__badge" data-testid="adopt-result-outcome"
									>{adoptResult?.outcome}</span
								>, intent {shortId(adoptResult?.intent_id ?? null)}.
							</p>
							<dl class="mai-apps__facts">
								<dt>Application</dt>
								<dd data-testid="adopt-result-identity">{adoptResult?.application.application_id}</dd>
								<dt>Server</dt>
								<dd data-testid="adopt-result-server">{adoptResult?.server_id}</dd>
								<dt>Record</dt>
								<dd>
									<span data-testid="adopt-result-revision">r{adoptResult?.revision ?? '—'}</span>
									·
									<span data-testid="adopt-result-sha">{shortHash(adoptResult?.sha256 ?? null)}</span>
								</dd>
							</dl>
							<p class="mai-apps__note" data-testid="adopt-result-paths">
								The external deployment paths travel as references inside the canonical manifest;
								nothing on the target was read, moved or restarted.
							</p>
							{#if adoptResult?.record_doc.status === 'documented'}
								<p class="mai-apps__note" data-testid="adopt-result-doc">
									Canonical record document written (doc
									{shortId(adoptResult?.record_doc.doc_id ?? null)}).
								</p>
								<Button
									variant="secondary"
									onclick={() => openRecordDoc(adoptResult?.record_doc.doc_id ?? '')}
									disabled={docs === null}
									data-testid="adopt-result-doc-open"
								>
									Open record document…
								</Button>
							{:else if adoptResult !== null}
								<p class="mai-apps__note mai-apps__note--error" data-testid="adopt-result-doc-pending">
									Record documentation is pending ({adoptResult.record_doc.reason ?? 'unknown'}) — the
									adoption stands; it converges on the next adoption revision.
								</p>
							{/if}
							<div class="mai-apps__actions">
								<Button variant="secondary" onclick={clearAdoptResult} data-testid="adopt-result-close">
									Close
								</Button>
							</div>
						</div>
					{/if}
				{/if}
			</section>

		{:else if tab === 'categories'}
			<section class="mai-apps__section" aria-label="Category taxonomy" data-testid="apps-categories">
				<div class="mai-apps__section-head">
					<h4>Categories</h4>
					<span class="mai-apps__meta">
						The Service Map taxonomy: 8 built-ins plus operator-defined categories.
					</span>
				</div>
				<div class="mai-apps__taxonomy" data-testid="category-taxonomy-menu">
					{#each taxonomy as entry (entry.machine_id)}
						<div
							class="mai-apps__taxonomy-row"
							data-testid="category-taxonomy-row"
							data-category={entry.machine_id}
						>
							<span class="mai-apps__chip">{entry.label}</span>
							<code class="mai-apps__mono" data-testid="category-taxonomy-id">{entry.machine_id}</code>
							<span class="mai-apps__taxonomy-desc" data-testid="category-taxonomy-description">
								{entry.description}
							</span>
							{#if entry.built_in}
								<span class="mai-apps__badge">built-in</span>
							{:else}
								<span class="mai-apps__badge mai-apps__badge--ok">custom</span>
							{/if}
						</div>
					{/each}
				</div>
				<form
					class="mai-apps__form"
					data-testid="category-create-form"
					onsubmit={(event) => {
						event.preventDefault();
						saveCategory();
					}}
				>
					<Field
						label="Machine id"
						bind:value={categoryIdInput}
						error={categoryFields.machine_id ?? ''}
						hint="Lowercase letters, digits, - and _ (1–64 characters); the wire vocabulary value."
						data-testid="category-id-input"
					/>
					<Field
						label="Label"
						bind:value={categoryLabelInput}
						error={categoryFields.label ?? ''}
						hint="Human-readable label shown on chips and pickers."
						data-testid="category-label-input"
					/>
					<Field
						label="Description"
						bind:value={categoryDescInput}
						error={categoryFields.description ?? ''}
						hint="What belongs in this category."
						data-testid="category-desc-input"
					/>
					<div class="mai-apps__actions">
						<Button type="submit" data-testid="category-save-btn">Save category</Button>
					</div>
				</form>
				<p class="mai-apps__note">
					A custom category joins every picker and chip immediately and persists with the next
					adoption or map save (the reviewed upsert rides that one mutation).
				</p>
			</section>
		{/if}
	{/if}
</div>

<style>
	.mai-apps {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3, 0.75rem);
		padding: var(--mai-panel-pad, 1rem);
		color: var(--mai-color-text, inherit);
		font-size: var(--mai-text-sm, 0.875rem);
		min-width: 0;
		min-height: 0;
		overflow: auto;
	}

	.mai-apps__header {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
	}

	.mai-apps__title {
		margin: 0;
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-base, 1rem);
		letter-spacing: 0.04em;
	}

	h4,
	h5 {
		margin: 0;
		font-size: var(--mai-text-sm, 0.875rem);
	}

	.mai-apps__meta {
		margin: 0;
		color: var(--mai-color-text-muted, inherit);
		display: inline-flex;
		gap: var(--mai-space-3, 0.75rem);
		flex-wrap: wrap;
	}

	.mai-apps__tabs {
		display: flex;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-apps__tab {
		border: 1px solid var(--mai-color-border, transparent);
		border-radius: var(--mai-radius-pill, 9999px);
		background: var(--mai-surface-glass-raised, transparent);
		color: var(--mai-color-text-muted, inherit);
		font: inherit;
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		padding: var(--mai-space-1, 0.25rem) var(--mai-space-3, 0.75rem);
		cursor: pointer;
	}

	.mai-apps__tab--active {
		border-color: var(--mai-color-brass, currentColor);
		color: var(--mai-color-text);
		background: var(--mai-color-surface-raised);
		font-weight: 600;
	}

	.mai-apps__section {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3, 0.75rem);
		border-top: 1px solid var(--mai-color-border, transparent);
		padding-top: var(--mai-space-3, 0.75rem);
	}

	.mai-apps__section-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-apps__toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--mai-space-3, 0.75rem);
		flex-wrap: wrap;
	}

	.mai-apps__port-chips {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mai-space-1, 0.25rem);
	}

	.mai-apps__subsection {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
	}

	.mai-apps__form {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3, 0.75rem);
	}

	.mai-apps__label {
		font-weight: 600;
	}

	.mai-apps__state {
		padding: var(--mai-card-pad, 0.75rem);
		border-radius: var(--mai-radius-md, 8px);
		border: 1px dashed var(--mai-color-border, transparent);
	}

	.mai-apps__state--error {
		border-style: solid;
		border-color: var(--mai-status-danger-border, currentColor);
		color: var(--mai-status-danger-text, inherit);
	}

	.mai-apps__catalogue {
		display: flex;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-apps__rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(22rem, 1fr));
		gap: var(--mai-space-3, 0.75rem);
	}

	.mai-apps__row {
		background: var(--mai-surface-glass-raised, var(--mai-color-surface-raised));
		border: 1px solid var(--mai-color-border, transparent);
		border-radius: var(--mai-radius-lg, 12px);
		padding: var(--mai-card-pad, 0.875rem);
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
	}

	.mai-apps__row--pinned {
		border-color: var(--mai-color-brass, currentColor);
	}

	.mai-apps__row-main {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-apps__row-id {
		font-weight: 600;
		font-size: var(--mai-text-base);
	}

	.mai-apps__row-actions {
		display: flex;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-apps__badge {
		border: 1px solid var(--mai-color-border, transparent);
		border-radius: var(--mai-radius-pill, 9999px);
		padding: 1px var(--mai-space-2, 0.5rem);
		font-size: var(--mai-text-xs);
	}

	.mai-apps__badge--ok {
		border-color: var(--mai-status-success-border, currentColor);
		background: var(--mai-status-success-bg);
		color: var(--mai-status-success-text, inherit);
	}

	.mai-apps__badge--danger {
		border-color: var(--mai-status-danger-border, currentColor);
		background: var(--mai-status-danger-bg);
		color: var(--mai-status-danger-text, inherit);
	}

	.mai-apps__badge--warn {
		border-color: var(--mai-status-warning-border, currentColor);
		background: var(--mai-status-warning-bg);
		color: var(--mai-status-warning-text, inherit);
	}

	.mai-apps__chip {
		display: inline-block;
		border: 1px solid var(--mai-color-brass, currentColor);
		border-radius: var(--mai-radius-pill, 9999px);
		padding: 1px var(--mai-space-2, 0.5rem);
		font-size: var(--mai-text-xs, 0.75rem);
		color: var(--mai-color-brass, inherit);
		white-space: nowrap;
	}

	.mai-apps__chip--suggested {
		border-style: dashed;
		color: var(--mai-color-brass-dim, inherit);
		border-color: var(--mai-color-brass-dim, currentColor);
	}

	.mai-apps__filterbar {
		display: flex;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-apps__pill {
		border: 1px solid var(--mai-color-border, transparent);
		border-radius: var(--mai-radius-pill, 9999px);
		background: var(--mai-surface-glass-raised, transparent);
		color: var(--mai-color-text-muted);
		font: inherit;
		font-size: var(--mai-text-xs, 0.75rem);
		padding: 2px var(--mai-space-3, 0.75rem);
		cursor: pointer;
	}

	.mai-apps__pill--active {
		border-color: var(--mai-color-brass, currentColor);
		color: var(--mai-color-brass, inherit);
		background: var(--mai-color-surface-raised);
		font-weight: 600;
	}

	.mai-apps__description {
		margin: 0;
		color: var(--color-muted, inherit);
	}

	.mai-apps__runbook {
		border: 1px solid var(--mai-color-border, transparent);
		border-radius: var(--mai-radius-sm, 4px);
		padding: var(--space-2, 0.5rem);
	}

	.mai-apps__runbook summary {
		cursor: pointer;
		font-weight: 600;
		font-size: var(--mai-text-xs, 0.75rem);
	}

	.mai-apps__editor {
		display: flex;
		flex-direction: column;
		gap: var(--space-3, 0.75rem);
		border: 1px solid var(--mai-color-brass, currentColor);
		border-radius: var(--mai-radius-sm, 4px);
		padding: var(--space-3, 0.75rem);
	}

	.mai-apps__select-label {
		display: flex;
		flex-direction: column;
		gap: var(--space-1, 0.25rem);
		font-weight: 600;
		font-size: var(--mai-text-sm, 0.875rem);
	}

	.mai-apps__select {
		font: inherit;
		padding: var(--space-1, 0.25rem) var(--space-2, 0.5rem);
		border: 1px solid var(--mai-color-border, currentColor);
		border-radius: var(--mai-radius-sm, 4px);
		background: var(--mai-color-surface-raised, transparent);
		color: inherit;
	}

	.mai-apps__fieldset {
		border: 1px solid var(--mai-color-border, transparent);
		border-radius: var(--mai-radius-sm, 4px);
		padding: var(--space-2, 0.5rem);
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2, 0.5rem);
	}

	.mai-apps__fieldset legend {
		padding: 0 var(--space-1, 0.25rem);
		font-weight: 600;
		font-size: var(--mai-text-xs, 0.75rem);
	}

	.mai-apps__taxonomy {
		display: flex;
		flex-direction: column;
		gap: var(--space-2, 0.5rem);
	}

	.mai-apps__taxonomy-row {
		display: flex;
		align-items: baseline;
		gap: var(--space-3, 0.75rem);
		flex-wrap: wrap;
		border: 1px solid var(--mai-color-border, transparent);
		border-radius: var(--mai-radius-sm, 4px);
		padding: var(--space-2, 0.5rem);
	}

	.mai-apps__taxonomy-desc {
		color: var(--color-muted, inherit);
		flex: 1 1 16rem;
		min-width: 0;
	}

	.mai-apps__mono {
		font-family: var(--mai-font-mono, monospace);
		font-size: var(--mai-text-xs, 0.75rem);
	}

	.mai-apps__facts {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: var(--space-1, 0.25rem) var(--space-3, 0.75rem);
		margin: 0;
	}

	.mai-apps__facts dt {
		color: var(--color-muted, inherit);
	}

	.mai-apps__facts dd {
		margin: 0;
		overflow-wrap: anywhere;
	}

	.mai-apps__confirm {
		display: flex;
		flex-direction: column;
		gap: var(--space-2, 0.5rem);
		padding: var(--space-2, 0.5rem);
		border: 1px solid var(--border-strong, currentColor);
		border-radius: var(--radius-sm, 4px);
	}

	.mai-apps__confirm p {
		margin: 0;
	}

	.mai-apps__actions {
		display: flex;
		gap: var(--space-2, 0.5rem);
		align-items: center;
		flex-wrap: wrap;
	}

	.mai-apps__note {
		margin: 0;
		color: var(--color-muted, inherit);
	}

	.mai-apps__note--error {
		color: var(--color-warning, inherit);
	}
</style>
