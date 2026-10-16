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
		GenerateView,
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
				'generateDocument',
				'runResearch'
			] as const
		) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as DocsApi;
	}

	/** Narrow the optional conversation seam; incomplete ⇒ null (fail closed). */
	function readChat(services: PanelServices): ChatSubmit | null {
		const candidate = services.chat as Partial<ChatSubmit> | null | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		if (typeof candidate.submitMessage !== 'function') return null;
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
	/** Local textarea guard for the pasted evidence JSON (display-only). */
	const MAX_EVIDENCE_CHARS = 65536;

	/** Frozen request version of the deep-research payload vocabulary. */
	const RESEARCH_REQUEST_VERSION = 1;

	const TABS = Object.freeze([
		{ id: 'library', label: 'Library' },
		{ id: 'search', label: 'Search' },
		{ id: 'index', label: 'Index' },
		{ id: 'generate', label: 'Generate' },
		{ id: 'research', label: 'Research' }
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
	let libraryState = $state<'idle' | 'loading' | 'ready' | 'error'>('idle');
	let libraryProblem = $state<{ code: string; message: string } | null>(null);
	let libraryEpoch = 0;

	async function loadLibrary(): Promise<void> {
		if (api === null || serverScope === null) return;
		const scope = serverScope;
		const epoch = ++libraryEpoch;
		libraryState = 'loading';
		libraryProblem = null;
		try {
			const page: DocumentPage = await api.listDocuments(scope, { limit: PAGE_LIMIT });
			if (disposed || epoch !== libraryEpoch || scope !== serverScope) return;
			documents = page.documents;
			libraryState = 'ready';
		} catch (error) {
			if (disposed || epoch !== libraryEpoch) return;
			const code = codeOf(error) ?? 'unavailable';
			documents = [];
			libraryState = 'error';
			libraryProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The document list could not be read.')
			};
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

	// --- generation (run evidence → runbook/incident; C07 rule 5) --------------

	let evidenceText = $state('');
	let generateBusy = $state(false);
	let generateView = $state<GenerateView | null>(null);
	let generateProblem = $state<{ code: string; message: string } | null>(null);
	let generateFields = $state<Record<string, string>>({});

	async function runGenerate(preview: boolean): Promise<void> {
		if (api === null || generateBusy) return;
		let evidence: Record<string, unknown> | null = null;
		const errors: Record<string, string> = {};
		const raw = evidenceText.trim();
		if (raw === '') {
			errors.evidence = 'Run-evidence JSON is required.';
		} else if (raw.length > MAX_EVIDENCE_CHARS) {
			errors.evidence = `The evidence document is bounded to ${MAX_EVIDENCE_CHARS} characters.`;
		} else {
			try {
				const parsed: unknown = JSON.parse(raw);
				if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
					errors.evidence = 'The evidence must be one JSON object.';
				} else {
					evidence = parsed as Record<string, unknown>;
				}
			} catch {
				errors.evidence = 'The evidence is not valid JSON.';
			}
		}
		generateFields = errors;
		if (evidence === null) return;
		generateBusy = true;
		generateProblem = null;
		try {
			const view = await api.generateDocument({ evidence, preview });
			if (disposed) return;
			generateView = view;
			if (!preview) {
				void loadLibrary();
				void loadIndex();
			}
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			generateProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The generation could not run.')
			};
		} finally {
			if (!disposed) generateBusy = false;
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
	let attachDoneRunId = $state<string | null>(null);
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
			const created = await chat.submitMessage({
				run_id: crypto.randomUUID(),
				server_id: serverScope,
				message,
				profile: attachProfile.trim(),
				role: 'administrator',
				mode: 'ask'
			});
			if (disposed) return;
			attachDoneRunId = created.conversation.run_id;
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
			<h3 class="mai-docs__title">Documents</h3>
			<p class="mai-docs__meta">
				<span data-testid="docs-scope">Server {shortId(serverScope ?? '')}</span>
				<span>Pinned scope — every request below names this target.</span>
			</p>
		</header>

		<nav class="mai-docs__tabs" aria-label="Documents sections" data-testid="docs-tabs">
			{#each TABS as entry (entry.id)}
				<button
					type="button"
					class="mai-docs__tab"
					class:mai-docs__tab--active={tab === entry.id}
					aria-pressed={tab === entry.id}
					onclick={() => (tab = entry.id)}
					data-testid="tab-{entry.id}"
				>
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
							<ul data-testid="index-pending-list">
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
							<ul data-testid="index-projection-list">
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
							<ul data-testid="reconcile-list">
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
		{:else if tab === 'generate'}
			<section class="mai-docs__section" aria-label="Generate from run evidence" data-testid="docs-generate">
				<div class="mai-docs__section-head">
					<h4>Generate</h4>
					<span class="mai-docs__meta">
						One settled run-evidence document (v=1) → preview or commit (C07 rule 5).
					</span>
				</div>
				<div class="mai-docs__form">
					<label class="mai-docs__label" for="{uid}-evidence">Run evidence (JSON)</label>
					<textarea
						id="{uid}-evidence"
						class="mai-docs__textarea"
						bind:value={evidenceText}
						rows={8}
						data-testid="generate-evidence"
					></textarea>
					{#if generateFields.evidence}
						<p class="mai-docs__field-error" role="alert">{generateFields.evidence}</p>
					{/if}
					<div class="mai-docs__actions">
						<Button
							variant="secondary"
							onclick={() => runGenerate(true)}
							loading={generateBusy}
							data-testid="generate-preview"
						>
							Preview
						</Button>
						<Button
							onclick={() => runGenerate(false)}
							loading={generateBusy}
							data-testid="generate-commit"
						>
							Commit as canonical document
						</Button>
					</div>
				</div>
				{#if generateProblem !== null}
					<div
						class="mai-docs__state mai-docs__state--error"
						role="alert"
						data-testid="generate-error"
						data-error-code={generateProblem.code}
					>
						<p>{generateProblem.message}</p>
					</div>
				{/if}
				{#if generateView !== null}
					{#if generateView.preview}
						<div class="mai-docs__subsection" data-testid="generate-preview-view">
							<h5>Preview (nothing was written)</h5>
							<p class="mai-docs__meta" data-testid="generate-preview-classification">
								{generateView.classification} · {generateView.namespace} ·
								{generateView.verification}
							</p>
							<pre class="mai-docs__content" data-testid="generate-preview-content">
{generateView.content}</pre
							>
						</div>
					{:else}
						<div class="mai-docs__subsection" data-testid="generate-commit-view">
							<h5>Committed</h5>
							<p class="mai-docs__meta" data-testid="generate-commit-citation">
								{generateView.doc.citation} · {generateView.classification} ·
								{generateView.verification}
							</p>
							{#if generateView.reindex === null}
								<p class="mai-docs__note" data-testid="generate-reindex-null">
									Reindex outcome: none recorded — the index seam is not wired on this surface;
									the durable intent converges on the next reconcile.
								</p>
							{:else}
								<ul data-testid="generate-reindex-list">
									{#each generateView.reindex as outcome (outcome.doc_id + ':' + outcome.op)}
										<li class="mai-docs__meta" data-testid="generate-reindex-row">
											{outcome.namespace}:{shortId(outcome.doc_id)} · {outcome.status}
											{outcome.code === null ? '' : ` · code ${outcome.code}`}
										</li>
									{/each}
								</ul>
							{/if}
						</div>
					{/if}
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
							<ul data-testid="research-step-list">
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
								<ul data-testid="research-source-list">
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
								<ul data-testid="research-hypothesis-list">
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
								<ul>
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
								Send to conversation
							</Button>
						</div>
					</div>
					{#if attachDoneRunId !== null}
						<p class="mai-docs__note" role="status" data-testid="attach-done">
							Recorded conversation {shortId(attachDoneRunId)} — the excerpt travels inside the
							durable message under its citation.
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
		gap: var(--space-4, 1rem);
		padding: var(--space-4, 1rem);
		color: var(--color-fg, inherit);
		font-size: var(--font-size-sm, 0.875rem);
		min-width: 0;
		min-height: 0;
		overflow: auto;
	}

	.mai-docs__header {
		display: flex;
		flex-direction: column;
		gap: var(--space-1, 0.25rem);
	}

	.mai-docs__title {
		margin: 0;
		font-size: var(--font-size-md, 1rem);
	}

	h4,
	h5 {
		margin: 0;
		font-size: var(--font-size-sm, 0.875rem);
	}

	.mai-docs__meta {
		margin: 0;
		color: var(--color-muted, inherit);
		display: inline-flex;
		gap: var(--space-3, 0.75rem);
		flex-wrap: wrap;
	}

	.mai-docs__tabs {
		display: flex;
		gap: var(--space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-docs__tab {
		border: 1px solid var(--border-subtle, transparent);
		border-radius: var(--radius-sm, 4px);
		background: transparent;
		color: inherit;
		font: inherit;
		padding: var(--space-1, 0.25rem) var(--space-3, 0.75rem);
		cursor: pointer;
	}

	.mai-docs__tab--active {
		border-color: var(--border-strong, currentColor);
		font-weight: 600;
	}

	.mai-docs__section {
		display: flex;
		flex-direction: column;
		gap: var(--space-3, 0.75rem);
		border-top: 1px solid var(--border-subtle, transparent);
		padding-top: var(--space-3, 0.75rem);
	}

	.mai-docs__section-head {
		display: flex;
		align-items: baseline;
		gap: var(--space-3, 0.75rem);
		flex-wrap: wrap;
	}

	.mai-docs__subsection {
		display: flex;
		flex-direction: column;
		gap: var(--space-2, 0.5rem);
	}

	.mai-docs__form {
		display: flex;
		flex-direction: column;
		gap: var(--space-3, 0.75rem);
	}

	.mai-docs__label {
		font-weight: 600;
	}

	.mai-docs__textarea {
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

	.mai-docs__field-error {
		margin: 0;
		color: var(--color-danger, inherit);
	}

	.mai-docs__state {
		padding: var(--space-2, 0.5rem);
		border-radius: var(--radius-sm, 4px);
		border: 1px dashed var(--border-subtle, transparent);
	}

	.mai-docs__state--error {
		border-style: solid;
		border-color: var(--color-danger, currentColor);
		color: var(--color-danger, inherit);
	}

	.mai-docs__rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2, 0.5rem);
	}

	.mai-docs__row {
		border: 1px solid var(--border-subtle, transparent);
		border-radius: var(--radius-sm, 4px);
		padding: var(--space-2, 0.5rem);
		display: flex;
		flex-direction: column;
		gap: var(--space-2, 0.5rem);
	}

	.mai-docs__row-main {
		display: flex;
		align-items: baseline;
		gap: var(--space-3, 0.75rem);
		flex-wrap: wrap;
	}

	.mai-docs__row-id {
		font-weight: 600;
	}

	.mai-docs__row-actions {
		display: flex;
		gap: var(--space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-docs__badge {
		border: 1px solid var(--border-subtle, transparent);
		border-radius: var(--radius-sm, 4px);
		padding: 0 var(--space-2, 0.5rem);
	}

	.mai-docs__badge--ok {
		border-color: var(--color-success, currentColor);
		color: var(--color-success, inherit);
	}

	.mai-docs__badge--danger {
		border-color: var(--color-danger, currentColor);
		color: var(--color-danger, inherit);
	}

	.mai-docs__badge--warn {
		border-color: var(--color-warning, currentColor);
		color: var(--color-warning, inherit);
	}

	.mai-docs__facts {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: var(--space-1, 0.25rem) var(--space-3, 0.75rem);
		margin: 0;
	}

	.mai-docs__facts dt {
		color: var(--color-muted, inherit);
	}

	.mai-docs__facts dd {
		margin: 0;
	}

	.mai-docs__content {
		margin: 0;
		padding: var(--space-2, 0.5rem);
		border: 1px solid var(--border-subtle, transparent);
		border-radius: var(--radius-sm, 4px);
		background: var(--surface-raised, transparent);
		font-family: var(--font-mono, monospace);
		font-size: var(--font-size-sm, 0.875rem);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		max-height: 22rem;
		overflow: auto;
	}

	.mai-docs__actions {
		display: flex;
		gap: var(--space-2, 0.5rem);
		align-items: center;
		flex-wrap: wrap;
	}

	.mai-docs__note {
		margin: 0;
		color: var(--color-muted, inherit);
	}

	.mai-docs__note--error {
		color: var(--color-danger, inherit);
	}
</style>
