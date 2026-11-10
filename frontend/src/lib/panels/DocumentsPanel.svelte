<script module lang="ts">
	/**
	 * DocumentsPanel — canonical docs/runbooks browser for the pinned server
	 * (card `mai-07-web-docs`, P07; contracts C07/C08/C11).
	 *
	 * Layer: reusable feature panel (windowed-workspace ADR §7.2). It owns NO
	 * window mechanics: no geometry, no drag/resize, no focus/stacking, no
	 * stores, no module-level mutable state — everything enters through the
	 * single `panel: PanelContext` prop (validated scope + explicit services
	 * bag) and instances share nothing (NT5; SSR-safe per ADR §5 rule 4).
	 *
	 * Wire honesty (C08 rule 6; AC2): every section renders
	 * loading/empty/unavailable/error/ready states. Retrieval answers
	 * `status: "unavailable"` with the failed seam's stable code — never an
	 * empty-success; pending index work renders as explicit `pending`; a
	 * generate commit with `reindex: null` says the index seam is not wired
	 * instead of claiming a reindex; a failed research run shows its
	 * canonical incident reference, never a fabricated procedure (C07 rule 5).
	 *
	 * Hostile content (acceptance 2): document/excerpt/report text is
	 * Markdown SOURCE and is rendered as inert text via Svelte's escaped
	 * interpolation only — there is deliberately no `{@html}` in this file,
	 * so unsafe markup cannot execute and cannot construct nodes.
	 *
	 * Stale-content guard (acceptance 2): every async section carries an
	 * instance-local epoch; a late response of a superseded request (or of a
	 * disposed host) is dropped before it can paint, so content of another
	 * query/selection can never appear from a stale fetch — there is no
	 * client-side cache to go stale.
	 *
	 * Scope (C01 rule 2, NT5): the descriptor's pinned `server_id` is the
	 * only target of every call; the pinned context of a live window never
	 * follows a global selection.
	 */
</script>

<script lang="ts">
	import { onDestroy } from 'svelte';
	import Button from '../ui/Button.svelte';
	import Field from '../ui/Field.svelte';
	import { BookOpen, FolderOpen, ScrollText, Search, Send } from 'lucide-svelte';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import type {
		ChatSubmit,
		DocNamespace,
		DocsApi,
		DocumentEditBody,
		DocumentHeadView,
		DocumentListItem,
		DocumentPage,
		DocumentView,
		IndexStatus,
		RetrievalBody,
		RetrievalExcerpt,
		RetrievalResult,
		ReconcileReport,
		ResearchReport
	} from '../workspace/entries/07-web-docs.js';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readApi(services: PanelServices): DocsApi | null {
		const candidate = services.api as Partial<DocsApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (
			const name of [
				'listDocuments',
				'readDocument',
				'editDocument',
				'retrieve',
				'readIndex',
				'reconcileIndex',
				'runResearch'
			] as const
		) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as DocsApi;
	}

	/**
	 * Narrow the optional chat seam; incomplete ⇒ null (fail closed). The
	 * session-first path is `attachToSession`; the legacy `submitMessage`
	 * fallback died with the D09 coordinated deletion.
	 */
	function readChat(services: PanelServices): ChatSubmit | null {
		const candidate = services.chat as Partial<ChatSubmit> | null | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		if (typeof candidate.attachToSession !== 'function') return null;
		return candidate as ChatSubmit;
	}

	const api = $derived(readApi(panel.services));
	const chat = $derived(readChat(panel.services));
	/** Documents browsing: a global mount contradicts the entry contract. */
	const serverScope = $derived(panel.scope.kind === 'server' ? panel.scope.server_id : null);
	const usable = $derived(api !== null && serverScope !== null);

	/** Stable element-id namespace for this instance (labels ⇄ inputs). */
	const uid = $props.id();

	// --- producer bounds mirrored for display only (server re-validates) -----

	/** Bounded library page size (documents.py DEFAULT_PAGE_LIMIT). */
	const PAGE_LIMIT = 50;
	/** Bounded retrieval page (retrieve.py DEFAULT_LIMIT / MAX_LIMIT). */
	const RETRIEVAL_DEFAULT_LIMIT = 5;
	const RETRIEVAL_MAX_LIMIT = 20;
	/** Bounded query length (retrieve.py MAX_QUERY_CHARS). */
	const MAX_QUERY_CHARS = 2048;
	/**
	 * Attach composition cap: an excerpt handed to chat is clipped to a
	 * bounded quote; the truncation is stated IN the message, never hidden.
	 */
	const MAX_ATTACH_CHARS = 2000;

	/** Frozen request version of the deep-research payload vocabulary. */
	const RESEARCH_REQUEST_VERSION = 1;

	const TABS = Object.freeze([
		{ id: 'library', label: 'Library', icon: FolderOpen },
		{ id: 'search', label: 'Search', icon: Search },
		{ id: 'index', label: 'Index', icon: Send },
		{ id: 'research', label: 'Research', icon: BookOpen }
	] as const);
	type TabId = (typeof TABS)[number]['id'];
	let tab = $state<TabId>('library');

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
		if (code === 'conflict') return 'The canonical state changed; refresh and reapply.';
		if (code === 'not_found') return 'Nothing is stored under this id and scope.';
		if (code === 'unavailable')
			return 'The documents service is unavailable right now; nothing was changed.';
		if (code === 'validation_failed') return 'The request was not accepted; see the details.';
		return fallback;
	}

	/** The retrieval outage vocabulary (retrieve.py RetrieveErrorCode). */
	function retrievalOutageText(code: string | null): string {
		if (code === 'index_unavailable')
			return 'The document index is unavailable; search did not run.';
		if (code === 'embedding_unavailable')
			return 'The embedding seam is unavailable; search did not run.';
		if (code === 'dimension_mismatch')
			return 'The index dimension contract does not match the configured embedding model; this scope refuses to query until a rebuild converges (C07 rule 3).';
		return 'Retrieval is unavailable; search did not run.';
	}

	function verificationBadge(verification: string): 'ok' | 'neutral' | 'warn' {
		if (verification === 'verified') return 'ok';
		if (verification === 'hypothesis') return 'warn';
		return 'neutral';
	}

	// --- library (projection page of the pinned scope; C02 W2) ----------------

	let documents = $state<DocumentListItem[]>([]);
	let libraryTotal = $state<number | null>(null);
	let libraryHasMore = $state(false);
	let libraryLoadingMore = $state(false);
	let libraryState = $state<'idle' | 'loading' | 'ready' | 'error'>('idle');
	let libraryProblem = $state<{ code: string; message: string } | null>(null);
	let libraryEpoch = 0;

	async function loadLibrary(): Promise<void> {
		if (api === null || serverScope === null) return;
		const scope = serverScope;
		const epoch = ++libraryEpoch;
		libraryState = 'loading';
		libraryLoadingMore = false;
		libraryProblem = null;
		try {
			const page: DocumentPage = await api.listDocuments(scope, { limit: PAGE_LIMIT });
			if (disposed || epoch !== libraryEpoch || scope !== serverScope) return;
			documents = page.documents;
			libraryTotal = page.total ?? null;
			libraryHasMore =
				page.total !== undefined
					? page.documents.length < page.total
					: page.documents.length >= PAGE_LIMIT;
			libraryState = 'ready';
		} catch (error) {
			if (disposed || epoch !== libraryEpoch) return;
			const code = codeOf(error) ?? 'unavailable';
			documents = [];
			libraryTotal = null;
			libraryHasMore = false;
			libraryState = 'error';
			libraryProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The document list could not be read.')
			};
		}
	}

	async function loadMoreLibrary(): Promise<void> {
		if (
			api === null ||
			serverScope === null ||
			libraryState !== 'ready' ||
			libraryLoadingMore ||
			!libraryHasMore
		) {
			return;
		}
		const scope = serverScope;
		const epoch = libraryEpoch;
		libraryLoadingMore = true;
		libraryProblem = null;
		try {
			const page: DocumentPage = await api.listDocuments(scope, {
				limit: PAGE_LIMIT,
				offset: documents.length
			});
			if (disposed || epoch !== libraryEpoch || scope !== serverScope) return;
			const seen = new Set(documents.map((d) => `${d.namespace}:${d.doc_id}`));
			const appended = page.documents.filter((d) => !seen.has(`${d.namespace}:${d.doc_id}`));
			documents = [...documents, ...appended];
			libraryTotal = page.total ?? libraryTotal;
			libraryHasMore =
				page.documents.length > 0 &&
				(page.total !== undefined
					? documents.length < page.total
					: page.documents.length >= PAGE_LIMIT);
		} catch (error) {
			if (disposed || epoch !== libraryEpoch) return;
			const code = codeOf(error) ?? 'unavailable';
			libraryProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'More documents could not be loaded.')
			};
		} finally {
			if (!disposed) libraryLoadingMore = false;
		}
	}

	// --- open document (hash-verified read; C02 W2/W4) -------------------------

	let openDoc = $state<DocumentView | null>(null);
	let openState = $state<'idle' | 'loading' | 'ready' | 'error'>('idle');
	let openProblem = $state<{ code: string; message: string } | null>(null);
	let docEpoch = 0;

	async function openDocument(row: Pick<DocumentListItem, 'doc_id' | 'namespace'>): Promise<void> {
		if (api === null || serverScope === null) return;
		const scope = serverScope;
		const docId = row.doc_id;
		const namespace = row.namespace as DocNamespace;
		const epoch = ++docEpoch;
		openState = 'loading';
		openProblem = null;
		editing = false;
		try {
			const stored = await api.readDocument(scope, docId, namespace);
			if (disposed || epoch !== docEpoch) return;
			openDoc = stored;
			openState = 'ready';
		} catch (error) {
			if (disposed || epoch !== docEpoch) return;
			const code = codeOf(error) ?? 'unavailable';
			openDoc = null;
			openState = 'error';
			openProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The document could not be read.')
			};
		}
	}

	function closeDocument(): void {
		docEpoch += 1;
		openDoc = null;
		openState = 'idle';
		openProblem = null;
		editing = false;
		editProblem = null;
	}

	// --- edit source (revision-checked replace; C01 rule 6) --------------------

	let editing = $state(false);
	let editContent = $state('');
	let editBusy = $state(false);
	let editProblem = $state<{ code: string; message: string } | null>(null);
	let editSaved = $state<DocumentHeadView | null>(null);

	function startEdit(): void {
		if (openDoc === null || typeof openDoc.content !== 'string') return;
		editContent = openDoc.content;
		editProblem = null;
		editSaved = null;
		editing = true;
	}

	function cancelEdit(): void {
		editing = false;
		editProblem = null;
		editContent = '';
	}

	async function saveEdit(): Promise<void> {
		const current = openDoc;
		if (api === null || serverScope === null || current === null || editBusy) return;
		if (editContent.trim() === '') {
			editProblem = { code: 'local', message: 'Content must not be empty.' };
			return;
		}
		const body: DocumentEditBody = {
			server_id: serverScope,
			app_id: current.app_id,
			shared: false,
			namespace: current.namespace as DocNamespace,
			content: editContent,
			expected_revision: current.revision
		};
		editBusy = true;
		editProblem = null;
		try {
			const head = await api.editDocument(current.doc_id, body);
			if (disposed) return;
			// Keep THIS window's open view coherent: the edited text is the
			// content this window just wrote; head carries the new revision.
			openDoc = { ...current, ...head, content: editContent };
			editSaved = head;
			editing = false;
			// The durable reindex intent now exists — refresh the visible truth
			// of both halves (list revision, pending outbox row).
			void loadLibrary();
			void loadIndex();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			editProblem = {
				code,
				message:
					code === 'conflict'
						? 'The document changed at another revision; reopen it and reapply the edit.'
						: friendlyText(code, messageOf(error) ?? 'The edit could not be recorded.')
			};
		} finally {
			if (!disposed) editBusy = false;
		}
	}

	// --- hybrid retrieval (cited, provenance-preserving; C07 rule 4) -----------

	let searchQuery = $state('');
	let searchLimit = $state('5');
	let searchBusy = $state(false);
	let searchResult = $state<RetrievalResult | null>(null);
	let searchProblem = $state<{ code: string; message: string } | null>(null);
	let searchFields = $state<Record<string, string>>({});
	let searchEpoch = 0;

	async function runSearch(): Promise<void> {
		if (api === null || serverScope === null || searchBusy) return;
		const scope = serverScope;
		const errors: Record<string, string> = {};
		const query = searchQuery.trim();
		const limit = Number.parseInt(searchLimit, 10);
		if (query === '') errors.query = 'A query is required.';
		else if (query.length > MAX_QUERY_CHARS)
			errors.query = `The query is bounded to ${MAX_QUERY_CHARS} characters.`;
		if (!Number.isInteger(limit) || limit < 1 || limit > RETRIEVAL_MAX_LIMIT)
			errors.limit = `The limit is an integer in 1..${RETRIEVAL_MAX_LIMIT}.`;
		searchFields = errors;
		if (Object.keys(errors).length > 0) return;
		const body: RetrievalBody = {
			server_id: scope,
			app_id: null,
			shared: false,
			namespace: null,
			query,
			limit,
			max_excerpt_chars: 1200
		};
		const epoch = ++searchEpoch;
		searchBusy = true;
		searchProblem = null;
		try {
			const result = await api.retrieve(body);
			if (disposed || epoch !== searchEpoch || scope !== serverScope) return;
			searchResult = result;
		} catch (error) {
			if (disposed || epoch !== searchEpoch) return;
			const code = codeOf(error) ?? 'unavailable';
			searchResult = null;
			searchProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The search could not be answered.')
			};
		} finally {
			if (!disposed) searchBusy = false;
		}
	}

	// --- index state (durable outbox + projection; AC2) ------------------------

	let indexStatus = $state<IndexStatus | null>(null);
	let indexState = $state<'idle' | 'loading' | 'ready' | 'error'>('idle');
	let indexProblem = $state<{ code: string; message: string } | null>(null);
	let indexEpoch = 0;
	let reconcileBusy = $state(false);
	let reconcileReport = $state<ReconcileReport | null>(null);
	let reconcileProblem = $state<{ code: string; message: string } | null>(null);

	async function loadIndex(): Promise<void> {
		if (api === null || serverScope === null) return;
		const scope = serverScope;
		const epoch = ++indexEpoch;
		indexState = 'loading';
		indexProblem = null;
		try {
			const status = await api.readIndex(scope);
			if (disposed || epoch !== indexEpoch || scope !== serverScope) return;
			indexStatus = status;
			indexState = 'ready';
		} catch (error) {
			if (disposed || epoch !== indexEpoch) return;
			const code = codeOf(error) ?? 'unavailable';
			indexStatus = null;
			indexState = 'error';
			indexProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The index state could not be read.')
			};
		}
	}

	async function reconcileNow(): Promise<void> {
		if (api === null || serverScope === null || reconcileBusy) return;
		const body = { server_id: serverScope, app_id: null, shared: false };
		reconcileBusy = true;
		reconcileProblem = null;
		try {
			const report = await api.reconcileIndex(body);
			if (disposed) return;
			reconcileReport = report;
			void loadIndex();
			void loadLibrary();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			reconcileProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The reconcile could not run.')
			};
		} finally {
			if (!disposed) reconcileBusy = false;
		}
	}

	// --- deep research (one bounded server-scoped pass) ------------------------

	let researchQuestion = $state('');
	let researchBusy = $state(false);
	let researchReport = $state<ResearchReport | null>(null);
	let researchProblem = $state<{ code: string; message: string } | null>(null);
	let researchFields = $state<Record<string, string>>({});

	async function runResearch(): Promise<void> {
		if (api === null || serverScope === null || researchBusy) return;
		const scope = serverScope;
		const errors: Record<string, string> = {};
		const question = researchQuestion.trim();
		if (question === '') errors.question = 'A question is required.';
		researchFields = errors;
		if (Object.keys(errors).length > 0) return;
		// The frozen request vocabulary, v=1. The channels this surface cannot
		// truthfully provide (web search/pages, host logs, child runs, model
		// synthesis) stay ABSENT — the producer rejects them pre-flight, so the
		// panel never pretends to offer them (honest UX, C08 rule 6).
		const body = {
			server_id: scope,
			payload: {
				v: RESEARCH_REQUEST_VERSION,
				question,
				search_queries: [],
				page_reads: [],
				logs: [],
				children: [],
				synthesize: false
			}
		};
		researchBusy = true;
		researchProblem = null;
		try {
			const report = await api.runResearch(body);
			if (disposed || scope !== serverScope) return;
			researchReport = report;
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			researchReport = null;
			researchProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The research pass could not run.')
			};
		} finally {
			if (!disposed) researchBusy = false;
		}
	}

	// --- attach excerpt to chat (the one reviewed submitMessage seam) ----------

	let attachCitation = $state('');
	let attachExcerpt = $state('');
	let attachProfile = $state('');
	let attachNote = $state('');
	let attachBusy = $state(false);
	/** The attach attempt's idempotency key: one per attach, reused on retry. */
	let attachRequestId = $state<string | null>(null);
	let attachDoneRunId = $state<string | null>(null);
	/** The session the excerpt was admitted into (session-first path). */
	let attachDoneSessionId = $state<string | null>(null);
	let attachProblem = $state<{ code: string; message: string } | null>(null);
	let attachFields = $state<Record<string, string>>({});

	/** The clipped quote actually composed into the message (bounded). */
	const attachClipped = $derived(
		attachExcerpt.length > MAX_ATTACH_CHARS
			? attachExcerpt.slice(0, MAX_ATTACH_CHARS)
			: attachExcerpt
	);
	const attachTruncated = $derived(attachExcerpt.length > attachClipped.length);

	function startAttach(citation: string, excerpt: string): void {
		attachCitation = citation;
		attachExcerpt = excerpt;
		attachDoneRunId = null;
		attachDoneSessionId = null;
		attachRequestId = null;
		attachProblem = null;
		attachFields = {};
	}

	function attachFromDocument(): void {
		const current = openDoc;
		if (current === null) return;
		startAttach(current.citation, current.content ?? '');
	}

	function attachFromExcerpt(excerpt: RetrievalExcerpt): void {
		startAttach(excerpt.citation, excerpt.excerpt);
	}

	function closeAttach(): void {
		attachCitation = '';
		attachExcerpt = '';
		attachProfile = '';
		attachNote = '';
		attachDoneRunId = null;
		attachDoneSessionId = null;
		attachRequestId = null;
		attachProblem = null;
		attachFields = {};
	}

	async function submitAttach(): Promise<void> {
		if (chat === null || serverScope === null || attachBusy) return;
		const errors: Record<string, string> = {};
		if (attachCitation === '') errors.excerpt = 'An excerpt is required.';
		if (attachProfile.trim() === '') errors.profile = 'A model profile is required.';
		attachFields = errors;
		if (Object.keys(errors).length > 0) return;
		const quoteLines = attachClipped.split('\n').map((line) => `> ${line}`);
		const truncatedNote = attachTruncated
			? `> …(excerpt truncated at ${MAX_ATTACH_CHARS} characters; the full source stays in the document)`
			: null;
		const message = [
			attachNote.trim(),
			`Attached excerpt from \`${attachCitation}\`:`,
			...quoteLines,
			...(truncatedNote !== null ? [truncatedNote] : [])
		]
			.filter((part) => part !== '')
			.join('\n');
		attachBusy = true;
		attachProblem = null;
		try {
			// Session-first attach (D07): the explicit create-new action (no
			// destination session selected) or the explicit destination
			// session, then ONE idempotent turn admission. The SAME
			// client_request_id rides every retry of this attach, so a
			// duplicate click converges (200 deduplicated) instead of
			// creating a second session/turn.
			if (attachRequestId === null) attachRequestId = crypto.randomUUID();
			const outcome = await chat.attachToSession({
				session_id: null, // explicit create-new-session action
				text: message,
				model_profile: attachProfile.trim(),
				client_request_id: attachRequestId
			});
			if (disposed) return;
			attachDoneSessionId = outcome.session_id;
			attachDoneRunId = outcome.run_id;
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			attachProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The excerpt could not be attached.')
			};
		} finally {
			if (!disposed) attachBusy = false;
		}
	}

	// --- mount cycle (the $effect teardown is the release path) ---------------

	$effect(() => {
		if (!usable) return;
		disposed = false;
		void loadLibrary();
		void loadIndex();
		return () => {
			disposed = true;
		};
	});

	onDestroy(() => {
		disposed = true;
	});
</script>

<div class="mai-docs" data-testid="docs-root">
	{#if !usable}
		<div class="mai-docs__state mai-docs__state--error" role="alert" data-testid="panel-config-error">
			<p>This panel was opened without its documents service or outside a pinned server scope.</p>
		</div>
	{:else}
		<header class="mai-docs__header">
			<h3 class="mai-docs__title">
				<ScrollText size={16} aria-hidden="true" />
				Documents
			</h3>
			<p class="mai-docs__meta">
				<span data-testid="docs-scope">Server {shortId(serverScope ?? '')}</span>
				<span>Pinned scope — every request below names this target.</span>
			</p>
		</header>

		<nav class="mai-docs__tabs" aria-label="Documents sections" data-testid="docs-tabs">
			{#each TABS as entry (entry.id)}
				{@const Icon = entry.icon}
				<button
					type="button"
					class="mai-docs__tab"
					class:mai-docs__tab--active={tab === entry.id}
					aria-pressed={tab === entry.id}
					onclick={() => (tab = entry.id)}
					data-testid="tab-{entry.id}"
				>
					<Icon size={14} aria-hidden="true" />
					{entry.label}
				</button>
			{/each}
		</nav>

		{#if tab === 'library'}
			<section class="mai-docs__section" aria-label="Document library" data-testid="docs-library">
				<div class="mai-docs__section-head">
					<h4>Library</h4>
					<Button variant="secondary" onclick={loadLibrary} data-testid="library-refresh">
						Refresh
					</Button>
				</div>
				{#if libraryState === 'loading'}
					<div class="mai-docs__state" role="status" data-testid="library-loading">
						<p>Loading documents…</p>
					</div>
				{:else if libraryState === 'error'}
					<div
						class="mai-docs__state mai-docs__state--error"
						role="alert"
						data-testid="library-error"
						data-error-code={libraryProblem?.code}
					>
						<p>{libraryProblem?.message}</p>
						<Button variant="secondary" onclick={loadLibrary} data-testid="library-retry">
							Retry
						</Button>
					</div>
				{:else if documents.length === 0}
					<div class="mai-docs__state" role="status" data-testid="library-empty">
						<p>No live documents are recorded for this scope.</p>
					</div>
				{:else}
					<ul class="mai-docs__rows" data-testid="library-list">
						{#each documents as row (row.doc_id)}
							<li class="mai-docs__row" data-testid="library-row" data-doc-id={row.doc_id}>
								<div class="mai-docs__row-main">
									<span class="mai-docs__row-id" data-testid="library-row-citation">
										{row.citation}
									</span>
									<span
										class="mai-docs__badge"
										class:mai-docs__badge--ok={verificationBadge(row.verification) === 'ok'}
										class:mai-docs__badge--warn={verificationBadge(row.verification) === 'warn'}
										data-testid="library-row-verification"
									>
										{row.verification}
									</span>
									<span class="mai-docs__meta">
										{row.namespace} · r{row.revision} · {row.source_kind} · observed
										{row.observed_at}
									</span>
								</div>
								<div class="mai-docs__row-actions">
									<Button
										variant="secondary"
										onclick={() => openDocument(row)}
										data-testid="library-open"
									>
										Open
									</Button>
								</div>
							</li>
						{/each}
					</ul>
					{#if libraryHasMore}
						<div class="mai-docs__row-actions">
							<Button
								variant="secondary"
								size="sm"
								loading={libraryLoadingMore}
								onclick={loadMoreLibrary}
								data-testid="library-load-more"
							>
								Load more ({documents.length}{libraryTotal !== null ? ` of ${libraryTotal}` : ''})
							</Button>
						</div>
					{/if}
				{/if}
			</section>

		{:else if tab === 'search'}
			<section class="mai-docs__section" aria-label="Hybrid retrieval" data-testid="docs-search">
				<div class="mai-docs__section-head">
					<h4>Search</h4>
					<span class="mai-docs__meta">Cited hybrid retrieval over the pinned scope.</span>
				</div>
				<div class="mai-docs__form">
					<Field
						label="Query"
						bind:value={searchQuery}
						error={searchFields.query ?? ''}
						data-testid="search-query"
					/>
					<Field
						label="Limit (1–{RETRIEVAL_MAX_LIMIT})"
						bind:value={searchLimit}
						error={searchFields.limit ?? ''}
						data-testid="search-limit"
					/>
					<div class="mai-docs__actions">
						<Button onclick={runSearch} loading={searchBusy} data-testid="search-submit">
							Search
						</Button>
					</div>
				</div>
				{#if searchProblem !== null}
					<div
						class="mai-docs__state mai-docs__state--error"
						role="alert"
						data-testid="search-error"
						data-error-code={searchProblem.code}
					>
						<p>{searchProblem.message}</p>
					</div>
				{/if}
				{#if searchResult !== null}
					{#if searchResult.status === 'unavailable'}
						<div
							class="mai-docs__state mai-docs__state--error"
							role="alert"
							data-testid="search-unavailable"
							data-error-code={searchResult.code ?? 'unavailable'}
						>
							<p>{retrievalOutageText(searchResult.code)}</p>
							<p class="mai-docs__meta">
								Failed seam: <span data-testid="search-unavailable-code"
									>{searchResult.code ?? 'unknown'}</span
								>
							</p>
						</div>
					{:else if searchResult.excerpts.length === 0}
						<div class="mai-docs__state" role="status" data-testid="search-empty">
							<p>The search ran and no excerpt matched this query in this scope.</p>
						</div>
					{:else}
						<ul class="mai-docs__rows" data-testid="search-results">
							{#each searchResult.excerpts as excerpt (excerpt.document_key + ':' + excerpt.ord)}
								<li
									class="mai-docs__row"
									data-testid="search-result"
									data-citation={excerpt.citation}
								>
									<div class="mai-docs__row-main">
										<span class="mai-docs__row-id" data-testid="excerpt-citation">
											{excerpt.citation}
										</span>
										<span
											class="mai-docs__badge"
											class:mai-docs__badge--ok={verificationBadge(excerpt.verification) === 'ok'}
											class:mai-docs__badge--warn={verificationBadge(excerpt.verification) ===
												'warn'}
											data-testid="excerpt-verification"
										>
											{excerpt.verification}
										</span>
										<span class="mai-docs__meta" data-testid="excerpt-meta">
											r{excerpt.revision} · sha {shortHash(excerpt.sha256)} · chunk
											{excerpt.ord} · score {excerpt.score} · vector
											{excerpt.vector_rank ?? '—'} / fts {excerpt.fts_rank ?? '—'}
											{excerpt.truncated ? ' · truncated' : ''}
										</span>
									</div>
									<pre class="mai-docs__content" data-testid="excerpt-text">{excerpt.excerpt}</pre>
									<div class="mai-docs__row-actions">
										<Button
											variant="secondary"
											onclick={() => openDocument(excerpt)}
											data-testid="excerpt-open"
										>
											Open cited document
										</Button>
										<Button
											variant="secondary"
											onclick={() => attachFromExcerpt(excerpt)}
											data-testid="excerpt-attach"
										>
											Attach excerpt to chat
										</Button>
									</div>
								</li>
							{/each}
						</ul>
					{/if}
				{/if}
			</section>
		{:else if tab === 'index'}
			<section class="mai-docs__section" aria-label="Index state" data-testid="docs-index">
				<div class="mai-docs__section-head">
					<h4>Index</h4>
					<Button variant="secondary" onclick={loadIndex} data-testid="index-refresh">
						Refresh
					</Button>
					<Button onclick={reconcileNow} loading={reconcileBusy} data-testid="index-reconcile">
						Reindex now
					</Button>
				</div>
				{#if indexState === 'loading'}
					<div class="mai-docs__state" role="status" data-testid="index-loading">
						<p>Reading the index state…</p>
					</div>
				{:else if indexState === 'error'}
					<div
						class="mai-docs__state mai-docs__state--error"
						role="alert"
						data-testid="index-error"
						data-error-code={indexProblem?.code}
					>
						<p>{indexProblem?.message}</p>
						<Button variant="secondary" onclick={loadIndex} data-testid="index-retry">
							Retry
						</Button>
					</div>
				{:else if indexStatus !== null}
					<div class="mai-docs__subsection" data-testid="index-pending">
						<h5>Pending reindex intents</h5>
						{#if indexStatus.pending.length === 0}
							<p class="mai-docs__meta" data-testid="index-pending-empty">
								No pending intents — the outbox of this scope is drained.
							</p>
						{:else}
							<ul class="mai-docs__rows" data-testid="index-pending-list">
								{#each indexStatus.pending as intent (intent.doc_id + ':' + intent.op)}
									<li class="mai-docs__meta" data-testid="index-pending-row">
										{intent.namespace}:{shortId(intent.doc_id)} · {intent.op} · r{intent.revision ??
											'—'} · <strong>pending</strong>
									</li>
								{/each}
							</ul>
						{/if}
					</div>
					<div class="mai-docs__subsection" data-testid="index-projection">
						<h5>Projection</h5>
						{#if indexStatus.projection === 'unavailable'}
							<div
								class="mai-docs__state mai-docs__state--error"
								role="alert"
								data-testid="index-projection-unavailable"
							>
								<p>The projection is unavailable (database not reachable) — no guess is shown.</p>
							</div>
						{:else if indexStatus.projection.length === 0}
							<p class="mai-docs__meta" data-testid="index-projection-empty">
								Nothing is projected for this scope yet.
							</p>
						{:else}
							<ul class="mai-docs__rows" data-testid="index-projection-list">
								{#each indexStatus.projection as row (row.doc_id)}
									<li class="mai-docs__meta" data-testid="index-projection-row">
										{row.namespace}:{shortId(row.doc_id)} · r{row.revision} ·
										{row.chunks}
										chunks · <span
											class="mai-docs__badge"
											class:mai-docs__badge--warn={row.status === 'tombstoned'}
											data-testid="index-projection-status"
										>
											{row.status}
										</span>
									</li>
								{/each}
							</ul>
						{/if}
					</div>
				{/if}
				{#if reconcileReport !== null}
					<div class="mai-docs__subsection" data-testid="reconcile-report">
						<h5>Last reconcile outcomes</h5>
						{#if reconcileReport.outcomes.length === 0}
							<p class="mai-docs__meta" data-testid="reconcile-empty">
								No live intents existed; nothing was reindexed.
							</p>
						{:else}
							<ul class="mai-docs__rows" data-testid="reconcile-list">
								{#each reconcileReport.outcomes as outcome (outcome.doc_id + ':' + outcome.op)}
									<li class="mai-docs__meta" data-testid="reconcile-row">
										{outcome.namespace}:{shortId(outcome.doc_id)} · {outcome.op} ·
										<span data-testid="reconcile-status">{outcome.status}</span>
										{outcome.chunks === null ? '' : ` · ${outcome.chunks} chunks`}
										{outcome.code === null ? '' : ` · code ${outcome.code}`}
									</li>
								{/each}
							</ul>
						{/if}
					</div>
				{/if}
				{#if reconcileProblem !== null}
					<div
						class="mai-docs__state mai-docs__state--error"
						role="alert"
						data-testid="reconcile-error"
						data-error-code={reconcileProblem.code}
					>
						<p>{reconcileProblem.message}</p>
					</div>
				{/if}
			</section>
		{:else if tab === 'research'}
			<section class="mai-docs__section" aria-label="Deep research" data-testid="docs-research">
				<div class="mai-docs__section-head">
					<h4>Research</h4>
					<span class="mai-docs__meta">
						One bounded pass over the pinned server's cited docs. Web search, page/log evidence,
						child runs and model synthesis are worker-owned channels — this surface honestly
						declines them instead of silently degrading.
					</span>
				</div>
				<div class="mai-docs__form">
					<Field
						label="Question"
						bind:value={researchQuestion}
						error={researchFields.question ?? ''}
						data-testid="research-question"
					/>
					<div class="mai-docs__actions">
						<Button onclick={runResearch} loading={researchBusy} data-testid="research-submit">
							Run research
						</Button>
					</div>
				</div>
				{#if researchProblem !== null}
					<div
						class="mai-docs__state mai-docs__state--error"
						role="alert"
						data-testid="research-error"
						data-error-code={researchProblem.code}
					>
						<p>{researchProblem.message}</p>
					</div>
				{/if}
				{#if researchReport !== null}
					<div class="mai-docs__subsection" data-testid="research-report">
						<div class="mai-docs__section-head">
							<h5>Report</h5>
							<span
								class="mai-docs__badge"
								class:mai-docs__badge--ok={researchReport.status === 'succeeded'}
								class:mai-docs__badge--danger={researchReport.status === 'failed'}
								data-testid="research-status"
							>
								{researchReport.status}
							</span>
							<span class="mai-docs__meta" data-testid="research-run-id">
								run {shortId(researchReport.run_id)}
							</span>
						</div>
						<p class="mai-docs__meta" data-testid="research-question-echo">
							{researchReport.question}
						</p>
						{#if researchReport.error !== null}
							<p class="mai-docs__note mai-docs__note--error" data-testid="research-run-error">
								{researchReport.error}
							</p>
						{/if}
						<div class="mai-docs__subsection" data-testid="research-steps">
							<h5>Steps</h5>
							<ul class="mai-docs__rows" data-testid="research-step-list">
								{#each researchReport.steps as step (step.seq)}
									<li class="mai-docs__meta" data-testid="research-step">
										{step.seq} · {step.phase} · {step.capability_id} · {step.status} ·
										{step.display}
									</li>
								{/each}
							</ul>
						</div>
						<div class="mai-docs__subsection" data-testid="research-sources">
							<h5>Sources</h5>
							{#if researchReport.sources.length === 0}
								<p class="mai-docs__meta" data-testid="research-sources-empty">
									No sources were recorded.
								</p>
							{:else}
								<ul class="mai-docs__rows" data-testid="research-source-list">
									{#each researchReport.sources as source, index (index)}
										<li
											class="mai-docs__row"
											data-testid="research-source"
											data-status={source.status}
										>
											<span class="mai-docs__row-id" data-testid="research-source-citation">
												[{source.kind}|{source.trust}] {source.citation === '' ? '(no citation)' : source.citation}
											</span>
											<span class="mai-docs__meta" data-testid="research-source-meta">
												{source.status}{source.truncated ? ' · truncated' : ''}{source.error_type ===
												null
													? ''
													: ` · error ${source.error_type}`}
											</span>
											<pre class="mai-docs__content" data-testid="research-source-text">
{source.text}</pre
											>
										</li>
									{/each}
								</ul>
							{/if}
						</div>
						<div class="mai-docs__subsection" data-testid="research-hypotheses">
							<h5>Hypotheses (citation-backed claims only)</h5>
							{#if researchReport.hypotheses.length === 0}
								<p class="mai-docs__meta" data-testid="research-hypotheses-empty">
									No verifiable hypothesis was produced.
								</p>
							{:else}
								<ul class="mai-docs__rows" data-testid="research-hypothesis-list">
									{#each researchReport.hypotheses as hypothesis, index (index)}
										<li class="mai-docs__meta" data-testid="research-hypothesis">
											{hypothesis.claim} — basis: {hypothesis.basis.join(', ')}
										</li>
									{/each}
								</ul>
							{/if}
						</div>
						{#if researchReport.dropped_claims.length > 0}
							<div class="mai-docs__subsection" data-testid="research-dropped">
								<h5>Dropped (unbacked) claims</h5>
								<ul class="mai-docs__rows">
									{#each researchReport.dropped_claims as claim, index (index)}
										<li class="mai-docs__meta" data-testid="research-dropped-claim">{claim}</li>
									{/each}
								</ul>
							</div>
						{/if}
						{#if researchReport.incident !== null}
							<p class="mai-docs__note mai-docs__note--error" data-testid="research-incident">
								A canonical incident was recorded for this failed run (C07 rule 5); no runbook
								success was fabricated.
							</p>
						{/if}
						{#if researchReport.incident_error !== null}
							<p
								class="mai-docs__note mai-docs__note--error"
								data-testid="research-incident-error"
							>
								The incident record itself failed to persist: {researchReport.incident_error}
							</p>
						{/if}
					</div>
				{/if}
			</section>
		{/if}

		{#if openState !== 'idle' || openDoc !== null}
			<section class="mai-docs__section" aria-label="Open document" data-testid="docs-document">
				{#if openState === 'loading'}
					<div class="mai-docs__state" role="status" data-testid="document-loading">
						<p>Reading the canonical document…</p>
					</div>
				{:else if openState === 'error'}
					<div
						class="mai-docs__state mai-docs__state--error"
						role="alert"
						data-testid="document-error"
						data-error-code={openProblem?.code}
					>
						<p>{openProblem?.message}</p>
					</div>
				{:else if openDoc !== null}
					<div class="mai-docs__section-head">
						<h4>Document</h4>
						<span class="mai-docs__meta" data-testid="document-citation">{openDoc.citation}</span>
						<Button variant="secondary" onclick={closeDocument} data-testid="document-close">
							Close
						</Button>
					</div>
					<dl class="mai-docs__facts" data-testid="document-facts">
						<dt>Revision</dt>
						<dd data-testid="document-revision">r{openDoc.revision}</dd>
						<dt>sha256</dt>
						<dd data-testid="document-sha">{shortHash(openDoc.sha256)}</dd>
						<dt>Verification</dt>
						<dd data-testid="document-verification">{openDoc.verification}</dd>
						<dt>Source</dt>
						<dd data-testid="document-source">
							{openDoc.source_kind} · {openDoc.origin ?? 'unknown origin'}
							{openDoc.origin_revision === null ? '' : ` · ${openDoc.origin_revision}`}
						</dd>
						<dt>Namespace</dt>
						<dd data-testid="document-namespace">{openDoc.namespace}</dd>
					</dl>
					{#if !editing}
						<pre class="mai-docs__content" data-testid="document-content">{openDoc.content ?? ''}</pre>
						<div class="mai-docs__actions">
							<Button variant="secondary" onclick={startEdit} data-testid="document-edit">
								Edit source
							</Button>
							<Button onclick={attachFromDocument} data-testid="document-attach">
								Attach excerpt to chat
							</Button>
						</div>
					{:else}
						<div class="mai-docs__form" data-testid="document-edit">
							<label class="mai-docs__label" for="{uid}-edit">Source (Markdown)</label>
							<textarea
								id="{uid}-edit"
								class="mai-docs__textarea"
								bind:value={editContent}
								rows={10}
								data-testid="document-edit-textarea"
							></textarea>
							<div class="mai-docs__actions">
								<Button onclick={saveEdit} loading={editBusy} data-testid="document-edit-save">
									Save at r{openDoc.revision}
								</Button>
								<Button
									variant="secondary"
									onclick={cancelEdit}
									disabled={editBusy}
									data-testid="document-edit-cancel"
								>
									Cancel
								</Button>
							</div>
						</div>
					{/if}
					{#if editSaved !== null}
						<p class="mai-docs__note" role="status" data-testid="document-edit-saved">
							Saved — the canonical revision is now r{editSaved.revision}; the reindex intent is
							visible under Index.
						</p>
					{/if}
					{#if editProblem !== null}
						<div
							class="mai-docs__state mai-docs__state--error"
							role="alert"
							data-testid="document-edit-error"
							data-error-code={editProblem.code}
						>
							<p>{editProblem.message}</p>
						</div>
					{/if}
				{/if}
			</section>
		{/if}

		{#if attachCitation !== ''}
			<section class="mai-docs__section" aria-label="Attach excerpt to chat" data-testid="docs-attach">
				<div class="mai-docs__section-head">
					<h4>Attach excerpt to chat</h4>
					<span class="mai-docs__meta" data-testid="attach-citation">{attachCitation}</span>
					<Button variant="secondary" onclick={closeAttach} data-testid="attach-close">
						Close
					</Button>
				</div>
				{#if chat === null}
					<div class="mai-docs__state" role="status" data-testid="attach-unavailable">
						<p>
							The conversation surface is not wired into this window — attaching is unavailable
							here. Nothing is submitted.
						</p>
					</div>
				{:else}
					<pre class="mai-docs__content" data-testid="attach-excerpt">{attachClipped}</pre>
					{#if attachTruncated}
						<p class="mai-docs__meta" data-testid="attach-truncated">
							The composed message quotes the first {MAX_ATTACH_CHARS} characters; the full source
							stays in the document and is named by the citation.
						</p>
					{/if}
					<div class="mai-docs__form">
						<Field
							label="Model profile"
							bind:value={attachProfile}
							error={attachFields.profile ?? ''}
							hint="Explicit selection (C06 rule 1); the stored pairing of the pinned server."
							data-testid="attach-profile"
						/>
						<Field
							label="Cover note (optional)"
							bind:value={attachNote}
							data-testid="attach-note"
						/>
						<div class="mai-docs__actions">
							<Button onclick={submitAttach} loading={attachBusy} data-testid="attach-send">
								Send to session
							</Button>
						</div>
					</div>
					{#if attachDoneRunId !== null}
						<p class="mai-docs__note" role="status" data-testid="attach-done">
							{attachDoneSessionId !== null
								? `Recorded session ${shortId(attachDoneSessionId)} · run ${shortId(attachDoneRunId)}`
								: `Recorded conversation ${shortId(attachDoneRunId)}`} —
							the excerpt travels inside the durable message under its citation.
						</p>
					{/if}
					{#if attachProblem !== null}
						<div
							class="mai-docs__state mai-docs__state--error"
							role="alert"
							data-testid="attach-error"
							data-error-code={attachProblem.code}
						>
							<p>{attachProblem.message}</p>
						</div>
					{/if}
				{/if}
			</section>
		{/if}
	{/if}
</div>

<style>
	.mai-docs {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-4);
		padding: var(--mai-space-4);
		color: var(--mai-color-text);
		font-family: var(--mai-font-sans);
		font-size: var(--mai-text-sm);
		line-height: var(--mai-leading-normal);
		min-width: 0;
		min-height: 0;
		overflow: auto;
	}

	/* Header band: brass data-scroll title on the cogitator header surface. */
	.mai-docs__header {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		padding: var(--mai-space-3) var(--mai-space-4);
		background: var(--mai-color-surface-header);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-md);
	}

	.mai-docs__title {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2);
		margin: 0;
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-lg);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-brass);
	}

	h4,
	h5 {
		margin: 0;
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-brass);
	}

	/* System annotations and telemetry (ids, revisions, digests, run steps). */
	.mai-docs__meta {
		margin: 0;
		color: var(--mai-color-text-muted);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		display: inline-flex;
		gap: var(--mai-space-3);
		flex-wrap: wrap;
	}

	/* Tab strip: display-type tabs; the active tab carries a crimson underline. */
	.mai-docs__tabs {
		display: flex;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
		border-bottom: var(--mai-border-width) solid var(--mai-color-border);
	}

	.mai-docs__tab {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2);
		border: var(--mai-border-width) solid transparent;
		border-bottom: 2px solid transparent;
		margin-bottom: calc(-1 * var(--mai-border-width));
		border-radius: var(--mai-radius-sm) var(--mai-radius-sm) 0 0;
		background: transparent;
		color: var(--mai-color-text-muted);
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		padding: var(--mai-space-2) var(--mai-space-3);
		cursor: pointer;
		transition:
			color var(--mai-motion-fast) ease,
			background-color var(--mai-motion-fast) ease,
			border-color var(--mai-motion-fast) ease;
	}

	.mai-docs__tab:hover {
		color: var(--mai-color-text);
	}

	.mai-docs__tab--active {
		background: var(--mai-color-surface-header);
		border-color: var(--mai-color-border);
		border-bottom-color: var(--mai-color-primary);
		color: var(--mai-color-brass);
		font-weight: var(--mai-weight-semibold);
	}

	/* Sections are raised cards; sub-groups are inset strips with a dim spine. */
	.mai-docs__section {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		padding: var(--mai-space-4);
		background: var(--mai-color-surface-raised);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-lg);
	}

	.mai-docs__section-head {
		display: flex;
		align-items: center;
		gap: var(--mai-space-3);
		flex-wrap: wrap;
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface-header);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-sm);
	}

	.mai-docs__subsection {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-space-3);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-left: 3px solid var(--mai-color-brass-dim);
		border-radius: var(--mai-radius-md);
	}

	.mai-docs__form {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
	}

	.mai-docs__label {
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.mai-docs__textarea {
		width: 100%;
		resize: vertical;
		min-height: 3.5rem;
		padding: var(--mai-space-2);
		border: var(--mai-border-width) solid var(--mai-color-border-strong);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface);
		color: var(--mai-color-text);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-sm);
		transition: border-color var(--mai-motion-fast) ease;
	}

	/* Honest states: dashed neutral for pending/empty, solid status for errors. */
	.mai-docs__state {
		padding: var(--mai-space-3);
		border-radius: var(--mai-radius-md);
		border: var(--mai-border-width) dashed var(--mai-color-border);
		color: var(--mai-color-text-muted);
	}

	.mai-docs__state--error {
		border-style: solid;
		border-color: var(--mai-status-danger-border);
		background: var(--mai-status-danger-bg);
		color: var(--mai-status-danger-text);
	}

	.mai-docs__rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
	}

	.mai-docs__row {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-space-3);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
	}

	.mai-docs__row-main {
		display: flex;
		align-items: baseline;
		gap: var(--mai-space-3);
		flex-wrap: wrap;
	}

	/* Citation chip: the stable scroll address of a document or excerpt. */
	.mai-docs__row-id {
		display: inline-flex;
		align-items: baseline;
		max-width: 100%;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-brass);
		background: var(--mai-color-surface-header);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		padding: 0 var(--mai-space-2);
	}

	.mai-docs__row-actions {
		display: flex;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
	}

	/* Status badges keep the frozen --mai-status-* triples. */
	.mai-docs__badge {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		letter-spacing: 0.04em;
		border: var(--mai-border-width) solid var(--mai-status-neutral-border);
		border-radius: var(--mai-radius-sm);
		padding: 0 var(--mai-space-2);
		background: var(--mai-status-neutral-bg);
		color: var(--mai-status-neutral-text);
	}

	.mai-docs__badge--ok {
		border-color: var(--mai-status-success-border);
		background: var(--mai-status-success-bg);
		color: var(--mai-status-success-text);
	}

	.mai-docs__badge--danger {
		border-color: var(--mai-status-danger-border);
		background: var(--mai-status-danger-bg);
		color: var(--mai-status-danger-text);
	}

	.mai-docs__badge--warn {
		border-color: var(--mai-status-warning-border);
		background: var(--mai-status-warning-bg);
		color: var(--mai-status-warning-text);
	}

	/* Canonical document facts: muted labels, mono digests and revisions. */
	.mai-docs__facts {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: var(--mai-space-1) var(--mai-space-3);
		margin: 0;
		padding: var(--mai-space-3);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
	}

	.mai-docs__facts dt {
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-xs);
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	.mai-docs__facts dd {
		margin: 0;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
	}

	/* Quoted source text: mono on an inset strip. */
	.mai-docs__content {
		margin: 0;
		padding: var(--mai-space-3);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface);
		color: var(--mai-color-text);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-sm);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		max-height: 22rem;
		overflow: auto;
	}

	.mai-docs__actions {
		display: flex;
		gap: var(--mai-space-2);
		align-items: center;
		flex-wrap: wrap;
	}

	.mai-docs__note {
		margin: 0;
		color: var(--mai-color-text-muted);
	}

	.mai-docs__note--error {
		color: var(--mai-status-danger-text);
	}
</style>
