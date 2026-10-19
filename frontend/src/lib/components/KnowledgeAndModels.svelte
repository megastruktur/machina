<script module lang="ts">
	/**
	 * KnowledgeAndModels — reusable Hindsight memory + local-models panel body
	 * (card `mai-11-web-memory-models`, P11; service row `mai-web`, contracts
	 * C01/C03/C06/C07/C08/C11).
	 *
	 * Layer: feature panel body (windowed-workspace ADR-0001 §2 layer table).
	 * It owns its one domain view and nothing else:
	 *
	 *  - Explicit context only: a SERVER-scoped view. `panel.scope.server_id`
	 *    is the pinned target of every call — this module reads no launcher/
	 *    current-server value anywhere, so there is no hidden global mutation
	 *    target (ADR §3.2, NT5). Memory scopes are the producer's vocabulary:
	 *    `"server"` (the context's own bank) or `"shared:<name>"` (the
	 *    configured allowlist); a payload can never name a raw bank.
	 *  - Explicit services only: the memory/local-model API enters through
	 *    `panel.services.api` (the typed `KnowledgeModelsApi` shape owned by
	 *    the feature entry module). A missing/incomplete bag fails closed
	 *    with a visible state — no service locator, no fallback client.
	 *  - Honest availability (C08 rule 6, brief acceptance 2): every section
	 *    renders its own loading/empty/offline/error state from the
	 *    producer's documents. A memory outage NEVER reads as an empty
	 *    successful recall: the recall form paints results only when the
	 *    document says `status: "ok"`, and an outage document paints the
	 *    classified outage (`provider_timeout` / `provider_unreachable` /
	 *    `provider_error` / `memory_disabled` / …) with no results list.
	 *  - No UI-only model state (brief acceptance 2): installed/registered
	 *    truth is exactly what the producer reports — the prerequisite
	 *    verdict of a REAL measurement (`verdict` over `facts`/`unknown`),
	 *    the durable run receipts (`RunDetail` steps), and the registered
	 *    connection document (`credential_ref` absent for the unauthenticated
	 *    local endpoint). The panel never infers or remembers success.
	 *  - Credential non-disclosure (C03 rule 6): no value field exists. The
	 *    1Password integration is the REFERENCE vocabulary only: the
	 *    credential-reference checker classifies `.env` names and `op://`
	 *    references (strict producer grammar from
	 *    `mai.credentials.onepassword`) value-free, client-side, with zero
	 *    network use — a value is never entered, sent, stored or displayed.
	 *  - The pull IS the registration path (11-model-install): no separate
	 *    registration control exists; the registered connection surface
	 *    reads the stored C02 record (deterministic per-server name).
	 *  - Mount reads, never writes (ADR §7.2): opening issues only the
	 *    status probe and the connection-list read. Every write happens on
	 *    an explicit action; destructive bank deletion requires an explicit
	 *    typed confirmation. The only poll is a watched submit run (bounded
	 *    1.5 s cadence, stopped at terminal states and torn down on hide/
	 *    close — the reviewed ApplicationActions convention).
	 *  - No window mechanics: no drag/geometry/focus/persistence code and no
	 *    workspace-state imports; the window host supplies all of that.
	 *  - No module-level state and no `window`/`document` access (ADR §5
	 *    rule 4): safe to evaluate per SSR request; instances share nothing.
	 *
	 * Tokens come only from ui/tokens.css; no dependency is added (ADR §4.3).
	 */
</script>

<script lang="ts">
	import { onDestroy } from 'svelte';
	import {
		Brain,
		Cable,
		Cog,
		Database,
		Download,
		FileDown,
		KeyRound,
		Lightbulb,
		ListChecks,
		NotebookPen,
		Search,
		Trash2,
		Wrench
	} from 'lucide-svelte';
	import Button from '../ui/Button.svelte';
	import Field from '../ui/Field.svelte';
	import Select from '../ui/Select.svelte';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import type {
		MemoryDeleteBody,
		MemoryDeleteDocument,
		MemoryExportDocument,
		MemoryRecallBody,
		MemoryRecallDocument,
		MemoryReflectBody,
		MemoryReflectDocument,
		MemoryRetainBody,
		MemoryRetainDocument,
		MemoryRetainItemBody,
		MemoryStatusDocument,
		ModelConnectionList,
		ModelConnectionView,
		ModelLocalPreviewBody,
		ModelLocalPreviewView,
		ModelLocalPullBody,
		ModelLocalSubmitBody,
		KnowledgeModelsApi,
		RunCreated,
		RunDetail
	} from '../workspace/entries/11-web-memory-models.js';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	const API_METHODS = [
		'memoryStatus',
		'recallMemory',
		'retainMemory',
		'reflectMemory',
		'exportMemory',
		'deleteMemory',
		'previewModelInstall',
		'installModelService',
		'pullModelWeights',
		'readRun',
		'listConnections',
		'readConnection'
	] as const;

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readApi(services: PanelServices): KnowledgeModelsApi | null {
		const candidate = services.api as Partial<KnowledgeModelsApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (const name of API_METHODS) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as KnowledgeModelsApi;
	}

	/** The members of the optional panel-links seam. */
	type LinkName = 'openApplications' | 'openJobs' | 'openModelSettings';

	/** One linkage seam member, narrowed alone (each fails closed alone). */
	function linkFn(
		name: LinkName
	): ((...args: string[]) => void) | null {
		const links = panel.services.links as
			| Partial<{ openApplications(serverId: string): void; openJobs(serverId: string): void; openModelSettings(): void }>
			| null
			| undefined;
		if (typeof links !== 'object' || links === null) return null;
		const member = links[name];
		return typeof member === 'function' ? member : null;
	}

	const api = $derived(readApi(panel.services));
	/** The pinned target: server scope only — a global mount contradicts the entry. */
	const serverId = $derived(panel.scope.kind === 'server' ? panel.scope.server_id : null);
	const usable = $derived(api !== null && serverId !== null);
	const openApplications = $derived(linkFn('openApplications'));
	const openJobs = $derived(linkFn('openJobs'));
	const openModelSettings = $derived(linkFn('openModelSettings'));

	// --- producer-bound input checks (server re-validates) -------------------

	// Memory adapter bounds (frozen by mai.memory.hindsight; mirrored for
	// display only — the producer re-validates everything).
	const MAX_SCOPE_CHARS = 71; // "shared:" + the 64-char shared-bank name
	const SHARED_NAME_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
	const MAX_QUERY_CHARS = 500;
	const MAX_RETAIN_ITEMS = 8;
	const MAX_CONTENT_CHARS = 4000;
	const MAX_CONTEXT_CHARS = 500;
	const DOCUMENT_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
	const FACT_TYPES: readonly string[] = ['world', 'experience', 'observation'];
	const BUDGETS: readonly string[] = ['low', 'mid', 'high'];
	const MIN_TOKEN_BUDGET = 256;
	const MAX_TOKEN_BUDGET = 4096;
	// Local-model wire bounds (mai.api.knowledge_models + local_models).
	const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
	const MAX_TARGET_CHARS = 64;
	const MODEL_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;
	const MAX_LICENSE_CHARS = 256;
	const MAX_BASE_URL_CHARS = 2048;
	// Credential-reference vocabulary (C03 rule 6 + the optional 1Password
	// provider's documented strict subset; value-free shapes only).
	const ENV_NAME_RE = /^[A-Z][A-Z0-9_]{0,127}$/;
	const OP_REFERENCE_RE =
		/^op:\/\/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
	const MAX_OP_REFERENCE_CHARS = 400;
	/** Fixed poll cadence for the watched submit run (bounded; torn down on hide). */
	const POLL_MS = 1500;
	/** Run lifecycle states after which nothing new can be learned (C05 rule 2). */
	const TERMINAL_RUN_STATES: ReadonlySet<string> = new Set(['succeeded', 'failed', 'cancelled']);

	function parseOptionalInt(raw: string, low: number, high: number): number | 'invalid' | null {
		const value = raw.trim();
		if (value === '') return null;
		const parsed = Number(value);
		if (!Number.isInteger(parsed) || parsed < low || parsed > high) return 'invalid';
		return parsed;
	}

	/** Producer scope vocabulary: `"server"` or `"shared:<allowlisted name>"`. */
	function validateScope(raw: string): string | null {
		const value = raw.trim();
		if (value === '') return 'Scope is required ("server" or "shared:<name>").';
		if (value.length > MAX_SCOPE_CHARS) return `Scope must be at most ${MAX_SCOPE_CHARS} characters.`;
		if (value === 'server') return null;
		if (value.startsWith('shared:')) {
			const name = value.slice('shared:'.length);
			if (!SHARED_NAME_RE.test(name)) {
				return 'Shared scope must be shared:<name> with a lowercase name [a-z0-9-].';
			}
			return null;
		}
		return 'Scope must be exactly "server" or "shared:<name>".';
	}

	function validateBaseUrl(raw: string): string | null {
		const value = raw.trim();
		if (value === '') return 'Base URL is required.';
		if (value.length > MAX_BASE_URL_CHARS) return `Base URL must be at most ${MAX_BASE_URL_CHARS} characters.`;
		let parts: URL;
		try {
			parts = new URL(value);
		} catch {
			return 'Base URL must be an absolute http(s) URL with a host.';
		}
		if (parts.protocol !== 'http:' && parts.protocol !== 'https:') {
			return 'Only http and https endpoints are allowed.';
		}
		if (parts.username !== '' || parts.password !== '') {
			return 'Inline URL credentials are not allowed — declare a credential reference instead.';
		}
		if (parts.search !== '' || parts.hash !== '') {
			return 'Query strings and fragments are not part of an endpoint base URL.';
		}
		return null;
	}

	// --- error helpers (frozen C01 vocabulary, value-free display) -----------

	function codeOf(error: unknown): string | null {
		if (typeof error !== 'object' || error === null) return null;
		const code = (error as { code?: unknown }).code;
		return typeof code === 'string' ? code : null;
	}

	function messageOf(error: unknown): string | null {
		return error instanceof Error && error.message ? error.message : null;
	}

	function friendlyText(code: string, fallback: string): string {
		if (code === 'unauthenticated') return 'Your session has ended — sign in again.';
		if (code === 'not_found') return 'This record does not exist.';
		if (code === 'throttled') return 'Too many requests — try again shortly.';
		if (code === 'unavailable') return 'The memory service is not configured or unreachable.';
		return fallback;
	}

	/** Outage code → value-free display text (mai.memory MemoryErrorCode). */
	const MEMORY_CODE_TEXT: Readonly<Record<string, string>> = Object.freeze({
		provider_timeout: 'The memory service did not answer in time.',
		provider_unreachable: 'The memory service is unreachable.',
		provider_error: 'The memory service reported an error.',
		memory_disabled: 'The memory service is not configured.',
		bank_forbidden: 'That scope is not on the shared-bank allowlist.',
		sensitive_content: 'The payload was refused — it looks like secret material.',
		invalid_payload: 'The payload was refused by the memory adapter.'
	});

	function memoryCodeText(code: string | null, fallback: string): string {
		if (code === null) return fallback;
		return MEMORY_CODE_TEXT[code] ?? fallback;
	}

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

	function runStateText(state: string | null): string {
		if (state === null) return 'State not recorded yet';
		return RUN_STATE_TEXT[state] ?? `Recorded state: ${state}`;
	}

	function stepStateText(state: string): string {
		return STEP_STATE_TEXT[state] ?? `Recorded state: ${state}`;
	}

	function shortHash(hash: string): string {
		return hash.length > 16 ? `${hash.slice(0, 12)}…` : hash;
	}

	// --- shared per-operation outcome state -----------------------------------

	/**
	 * One producer-document outcome of a memory operation. A non-ok document
	 * is an OUTCOME (displayed, classified) — never a thrown-away empty
	 * result; an HTTP envelope failure is a `problem`.
	 */
	type MemoryOutcome<T> =
		| { kind: 'idle' }
		| { kind: 'ok'; document: T }
		| { kind: 'unavailable'; document: T }
		| { kind: 'problem'; code: string; message: string };

	// --- view state (C08 rule 6: never all-green on unavailable data) --------

	type LoadState =
		| { status: 'loading' }
		| { status: 'error'; code: string; message: string }
		| { status: 'ready' };

	/** Set once the mount cycle ended (hide/close/error teardown); async
	 * continuations check it before touching state (ADR §7.2 release). */
	let disposed = true;

	let view = $state<LoadState>({ status: 'loading' });

	// Status section.
	let statusOutcome = $state<MemoryOutcome<MemoryStatusDocument>>({ kind: 'idle' });
	let statusBusy = $state(false);

	// Recall section.
	type RecallForm = {
		scope: string;
		query: string;
		world: boolean;
		experience: boolean;
		observation: boolean;
		budget: string;
		maxTokens: string;
	};
	let recallForm = $state<RecallForm>({ scope: 'server', query: '', world: false, experience: false, observation: false, budget: 'low', maxTokens: '' });
	let recallBusy = $state(false);
	let recallOutcome = $state<MemoryOutcome<MemoryRecallDocument>>({ kind: 'idle' });

	// Retain section.
	type RetainItem = { content: string; context: string; documentId: string };
	let retainScope = $state('server');
	let retainItems = $state<RetainItem[]>([{ content: '', context: '', documentId: '' }]);
	let retainBusy = $state(false);
	let retainOutcome = $state<MemoryOutcome<MemoryRetainDocument>>({ kind: 'idle' });

	// Reflect section.
	let reflectForm = $state<{ scope: string; query: string; maxTokens: string }>({ scope: 'server', query: '', maxTokens: '' });
	let reflectBusy = $state(false);
	let reflectOutcome = $state<MemoryOutcome<MemoryReflectDocument>>({ kind: 'idle' });

	// Export section.
	let exportScope = $state('server');
	let exportBusy = $state(false);
	let exportOutcome = $state<MemoryOutcome<MemoryExportDocument>>({ kind: 'idle' });

	// Delete section: an explicit target + typed confirmation for the bank.
	let deleteForm = $state<{ scope: string; target: 'memories' | 'bank'; factType: string; confirm: string }>({ scope: 'server', target: 'memories', factType: '', confirm: '' });
	let deleteBusy = $state(false);
	let deleteOutcome = $state<MemoryOutcome<MemoryDeleteDocument>>({ kind: 'idle' });

	// Local-model forms. Recipe + target bind preview AND submits: a submit
	// without a satisfied preview of the SAME recipe+target is refused
	// client-side (the producer rebuilds and re-verifies the plan anyway).
	let modelAppId = $state('');
	let recipeText = $state('');
	let targetOs = $state('linux');
	let targetArch = $state('x86_64');
	let previewBusy = $state(false);
	let previewView = $state<ModelLocalPreviewView | null>(null);
	/** The exact recipe+target the shown preview was built from. */
	let previewIdentity = $state<{ recipe: string; target: string } | null>(null);
	let previewProblem = $state<{ code: string; message: string } | null>(null);

	let backupConfirmed = $state(false);
	let installBusy = $state(false);
	let installProblem = $state<{ code: string; message: string } | null>(null);

	let pullForm = $state<{ model: string; license: string; baseUrl: string }>({ model: '', license: '', baseUrl: '' });
	let pullBusy = $state(false);
	let pullProblem = $state<{ code: string; message: string } | null>(null);

	/** The currently watched submit run (install or pull); polled to terminal. */
	type WatchedRun = { runId: string; kind: 'install' | 'pull'; deduplicated: boolean };
	let watchedRun = $state<WatchedRun | null>(null);
	let watchedDetail = $state<RunDetail | null>(null);
	let watchProblem = $state<{ code: string; message: string } | null>(null);
	let pollTimer: ReturnType<typeof setTimeout> | null = null;

	// Registered connections (the pull's registration outcome, read-only).
	let connections = $state<ModelConnectionList['connections']>([]);
	let connectionView = $state<ModelConnectionView | null>(null);
	let connectionProblem = $state<{ code: string; message: string } | null>(null);

	// Credential-reference checker (1Password integration): pure client-side
	// classification of a reference SHAPE. Nothing here is transmitted.
	let refCheckInput = $state('');

	// --- derived validation ---------------------------------------------------

	const recallErrors = $derived.by<Record<string, string>>(() => {
		const errors: Record<string, string> = {};
		const scopeError = validateScope(recallForm.scope);
		if (scopeError !== null) errors.scope = scopeError;
		const query = recallForm.query.trim();
		if (query === '') errors.query = 'Query is required.';
		else if (query.length > MAX_QUERY_CHARS) errors.query = `Query must be at most ${MAX_QUERY_CHARS} characters.`;
		if (parseOptionalInt(recallForm.maxTokens, MIN_TOKEN_BUDGET, MAX_TOKEN_BUDGET) === 'invalid') {
			errors.maxTokens = `Max tokens must be an integer between ${MIN_TOKEN_BUDGET} and ${MAX_TOKEN_BUDGET}, or empty.`;
		}
		return errors;
	});
	const recallValid = $derived(Object.keys(recallErrors).length === 0);

	const retainErrors = $derived.by<{ scope: string | null; items: (Record<string, string> | null)[] }>(() => {
		const items: (Record<string, string> | null)[] = retainItems.map((item) => {
			const errors: Record<string, string> = {};
			const content = item.content.trim();
			if (content === '') errors.content = 'Content is required.';
			else if (content.length > MAX_CONTENT_CHARS) errors.content = `Content must be at most ${MAX_CONTENT_CHARS} characters.`;
			if (item.context.trim().length > MAX_CONTEXT_CHARS) errors.context = `Context must be at most ${MAX_CONTEXT_CHARS} characters.`;
			const docId = item.documentId.trim();
			if (docId !== '' && !DOCUMENT_ID_RE.test(docId)) errors.documentId = 'Document id must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127}.';
			return Object.keys(errors).length > 0 ? errors : null;
		});
		return { scope: validateScope(retainScope), items };
	});
	const retainValid = $derived(
		retainErrors.scope === null && retainErrors.items.every((item) => item === null)
	);

	const reflectErrors = $derived.by<Record<string, string>>(() => {
		const errors: Record<string, string> = {};
		const scopeError = validateScope(reflectForm.scope);
		if (scopeError !== null) errors.scope = scopeError;
		const query = reflectForm.query.trim();
		if (query === '') errors.query = 'Query is required.';
		else if (query.length > MAX_QUERY_CHARS) errors.query = `Query must be at most ${MAX_QUERY_CHARS} characters.`;
		if (parseOptionalInt(reflectForm.maxTokens, MIN_TOKEN_BUDGET, MAX_TOKEN_BUDGET) === 'invalid') {
			errors.maxTokens = `Max tokens must be an integer between ${MIN_TOKEN_BUDGET} and ${MAX_TOKEN_BUDGET}, or empty.`;
		}
		return errors;
	});
	const reflectValid = $derived(Object.keys(reflectErrors).length === 0);

	const exportScopeError = $derived(validateScope(exportScope));

	const deleteErrors = $derived.by<Record<string, string>>(() => {
		const errors: Record<string, string> = {};
		const scopeError = validateScope(deleteForm.scope);
		if (scopeError !== null) errors.scope = scopeError;
		if (deleteForm.target === 'memories' && deleteForm.factType !== '' && !FACT_TYPES.includes(deleteForm.factType)) {
			errors.factType = 'Type must be one of world, experience, observation, or empty (all).';
		}
		if (deleteForm.target === 'bank') {
			if (deleteForm.scope.trim() !== 'server') {
				errors.confirm = 'Whole-bank deletion applies to the "server" scope only.';
			} else if (deleteForm.confirm.trim() !== `delete ${deleteForm.scope.trim()}`) {
				errors.confirm = `Type "delete ${deleteForm.scope.trim()}" to confirm the whole-bank deletion.`;
			}
		}
		return errors;
	});
	const deleteValid = $derived(Object.keys(deleteErrors).length === 0);

	const previewFormValid = $derived(
		UUID_RE.test(modelAppId.trim()) && recipeText.trim().length > 0 && targetOs.trim().length > 0 && targetOs.trim().length <= MAX_TARGET_CHARS && targetArch.trim().length > 0 && targetArch.trim().length <= MAX_TARGET_CHARS
	);
	/** A submit is honored only behind a satisfied preview of the SAME recipe+target. */
	const previewIdentityMatches = $derived(
		previewIdentity !== null &&
			previewIdentity.recipe === recipeText &&
			previewIdentity.target === `${targetOs.trim()}/${targetArch.trim()}`
	);
	const installReady = $derived(
		previewIdentityMatches &&
			previewView !== null &&
			previewView.verdict === 'satisfied' &&
			previewView.server_id === serverId &&
			previewView.application_id === modelAppId.trim()
	);
	const pullErrors = $derived.by<Record<string, string>>(() => {
		const errors: Record<string, string> = {};
		if (!MODEL_NAME_RE.test(pullForm.model.trim())) errors.model = 'Model must match [A-Za-z0-9][A-Za-z0-9._:/-]{0,127}.';
		const license = pullForm.license.trim();
		if (license === '') errors.license = 'License metadata is required on every explicit acquisition.';
		else if (license.length > MAX_LICENSE_CHARS) errors.license = `License must be at most ${MAX_LICENSE_CHARS} characters.`;
		const urlError = validateBaseUrl(pullForm.baseUrl);
		if (urlError !== null) errors.baseUrl = urlError;
		return errors;
	});
	const pullReady = $derived(previewIdentityMatches && Object.keys(pullErrors).length === 0);

	/** Honest local verdict line for the preview section header. */
	const previewVerdictText = $derived.by(() => {
		const shown = previewView;
		if (shown === null || !previewIdentityMatches) return null;
		if (shown.verdict === 'satisfied') return 'Prerequisites measured and satisfied.';
		const unknown = shown.unknown.length > 0 ? `${shown.unknown.length} unmeasurable` : 'none unmeasurable';
		return `Unsatisfied: ${shown.hardware_unsatisfied.join(', ') || 'none named'}; ${unknown}.`;
	});

	// Credential-reference classification (pure functions over the SHAPE).
	type RefClassification =
		| { kind: 'empty' }
		| { kind: 'env-name' }
		| { kind: 'op-reference' }
		| { kind: 'invalid'; reason: string };
	const refClassification = $derived.by<RefClassification>(() => {
		const value = refCheckInput.trim();
		if (value === '') return { kind: 'empty' };
		if (ENV_NAME_RE.test(value)) return { kind: 'env-name' };
		if (value.startsWith('op://')) {
			if (value.length > MAX_OP_REFERENCE_CHARS) {
				return { kind: 'invalid', reason: `A 1Password reference must be at most ${MAX_OP_REFERENCE_CHARS} characters.` };
			}
			if (OP_REFERENCE_RE.test(value)) return { kind: 'op-reference' };
			return {
				kind: 'invalid',
				reason: 'A 1Password reference is op://<vault>/<item>/<field> — three non-empty segments of [A-Za-z0-9][A-Za-z0-9._-]{0,127}; no spaces, percent-encoding, "." or ".." segments.'
			};
		}
		return {
			kind: 'invalid',
			reason: 'Not a credential reference shape. A .env name is [A-Z][A-Z0-9_]{0,127}; a 1Password reference is op://<vault>/<item>/<field>. Never paste a secret VALUE.'
		};
	});

	// --- load / refresh -------------------------------------------------------

	async function probeStatus(): Promise<void> {
		if (api === null || serverId === null || statusBusy) return;
		statusBusy = true;
		try {
			const document = await api.memoryStatus(serverId);
			if (disposed) return;
			statusOutcome =
				document.status === 'ok' ? { kind: 'ok', document } : { kind: 'unavailable', document };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			statusOutcome = {
				kind: 'problem',
				code,
				message: messageOf(error) ?? memoryCodeText(code, 'The availability probe failed.')
			};
		} finally {
			if (!disposed) statusBusy = false;
		}
	}

	async function refreshConnections(): Promise<void> {
		if (api === null) return;
		try {
			const list = await api.listConnections();
			if (disposed) return;
			connections = list.connections;
		} catch {
			// Registration visibility catches up on the next explicit reload;
			// the section keeps its last honest state.
		}
	}

	async function loadAll(): Promise<void> {
		if (api === null || serverId === null) return;
		view = { status: 'loading' };
		// The status probe ANSWERS 200 with an honest unavailable document when
		// the service is unconfigured/down — it is not an envelope failure, so
		// it must not fail the mount load (C08 rule 6: show the real state).
		const [status, connectionList] = await Promise.allSettled([
			api.memoryStatus(serverId),
			api.listConnections()
		]);
		if (disposed) return;
		if (status.status === 'fulfilled') {
			statusOutcome =
				status.value.status === 'ok'
					? { kind: 'ok', document: status.value }
					: { kind: 'unavailable', document: status.value };
			view = { status: 'ready' };
		} else if (codeOf(status.reason) === 'unauthenticated' || codeOf(status.reason) === 'csrf_rejected') {
			// Session-boundary failures are load errors (every section would
			// fail the same way); anything else keeps the panel usable with a
			// per-section outage.
			const code = codeOf(status.reason) ?? 'unavailable';
			view = {
				status: 'error',
				code,
				message: friendlyText(code, messageOf(status.reason) ?? 'The memory service could not be reached.')
			};
			return;
		} else {
			const code = codeOf(status.reason) ?? 'unavailable';
			statusOutcome = {
				kind: 'problem',
				code,
				message: messageOf(status.reason) ?? memoryCodeText(code, 'The availability probe failed.')
			};
			view = { status: 'ready' };
		}
		if (connectionList.status === 'fulfilled') {
			connections = connectionList.value.connections;
		} else {
			connectionProblem = {
				code: codeOf(connectionList.reason) ?? 'unavailable',
				message:
					messageOf(connectionList.reason) ??
					'The registered-connection list could not be loaded.'
			};
		}
	}

	// --- memory operations ----------------------------------------------------

	async function runRecall(): Promise<void> {
		const client = api;
		const target = serverId;
		if (client === null || target === null || recallBusy || !recallValid) return;
		recallBusy = true;
		recallOutcome = { kind: 'idle' };
		const types = [recallForm.world && 'world', recallForm.experience && 'experience', recallForm.observation && 'observation'].filter(
			(item): item is string => typeof item === 'string'
		);
		const maxTokens = parseOptionalInt(recallForm.maxTokens, MIN_TOKEN_BUDGET, MAX_TOKEN_BUDGET);
		const body: MemoryRecallBody = {
			scope: recallForm.scope.trim(),
			query: recallForm.query.trim(),
			types: types.length > 0 ? types : null,
			budget: BUDGETS.includes(recallForm.budget) ? recallForm.budget : null,
			max_tokens: maxTokens === 'invalid' ? null : maxTokens
		};
		try {
			const document = await client.recallMemory(target, body);
			if (disposed) return;
			recallOutcome =
				document.status === 'ok' ? { kind: 'ok', document } : { kind: 'unavailable', document };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			recallOutcome = {
				kind: 'problem',
				code,
				message: messageOf(error) ?? memoryCodeText(code, 'The recall could not run.')
			};
		} finally {
			if (!disposed) recallBusy = false;
		}
	}

	function addRetainItem(): void {
		if (retainItems.length >= MAX_RETAIN_ITEMS) return;
		retainItems = [...retainItems, { content: '', context: '', documentId: '' }];
	}

	function removeRetainItem(index: number): void {
		retainItems = retainItems.filter((_, candidate) => candidate !== index);
	}

	async function runRetain(): Promise<void> {
		const client = api;
		const target = serverId;
		if (client === null || target === null || retainBusy || !retainValid) return;
		retainBusy = true;
		retainOutcome = { kind: 'idle' };
		const items: MemoryRetainItemBody[] = retainItems.map((item) => ({
			content: item.content.trim(),
			context: item.context.trim() === '' ? null : item.context.trim(),
			document_id: item.documentId.trim() === '' ? null : item.documentId.trim()
		}));
		const body: MemoryRetainBody = { scope: retainScope.trim(), items };
		try {
			const document = await client.retainMemory(target, body);
			if (disposed) return;
			retainOutcome =
				document.status === 'ok' ? { kind: 'ok', document } : { kind: 'unavailable', document };
			if (document.status === 'ok') retainItems = [{ content: '', context: '', documentId: '' }];
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			retainOutcome = {
				kind: 'problem',
				code,
				message: messageOf(error) ?? memoryCodeText(code, 'The retain could not run.')
			};
		} finally {
			if (!disposed) retainBusy = false;
		}
	}

	async function runReflect(): Promise<void> {
		const client = api;
		const target = serverId;
		if (client === null || target === null || reflectBusy || !reflectValid) return;
		reflectBusy = true;
		reflectOutcome = { kind: 'idle' };
		const maxTokens = parseOptionalInt(reflectForm.maxTokens, MIN_TOKEN_BUDGET, MAX_TOKEN_BUDGET);
		const body: MemoryReflectBody = {
			scope: reflectForm.scope.trim(),
			query: reflectForm.query.trim(),
			max_tokens: maxTokens === 'invalid' ? null : maxTokens
		};
		try {
			const document = await client.reflectMemory(target, body);
			if (disposed) return;
			reflectOutcome =
				document.status === 'ok' ? { kind: 'ok', document } : { kind: 'unavailable', document };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			reflectOutcome = {
				kind: 'problem',
				code,
				message: messageOf(error) ?? memoryCodeText(code, 'The reflect could not run.')
			};
		} finally {
			if (!disposed) reflectBusy = false;
		}
	}

	async function runExport(): Promise<void> {
		const client = api;
		const target = serverId;
		if (client === null || target === null || exportBusy || exportScopeError !== null) return;
		exportBusy = true;
		exportOutcome = { kind: 'idle' };
		try {
			const document = await client.exportMemory(target, exportScope.trim());
			if (disposed) return;
			exportOutcome =
				document.status === 'ok' ? { kind: 'ok', document } : { kind: 'unavailable', document };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			exportOutcome = {
				kind: 'problem',
				code,
				message: messageOf(error) ?? memoryCodeText(code, 'The export could not run.')
			};
		} finally {
			if (!disposed) exportBusy = false;
		}
	}

	async function runDelete(): Promise<void> {
		const client = api;
		const target = serverId;
		if (client === null || target === null || deleteBusy || !deleteValid) return;
		deleteBusy = true;
		deleteOutcome = { kind: 'idle' };
		const body: MemoryDeleteBody = {
			scope: deleteForm.scope.trim(),
			target: deleteForm.target,
			fact_type: deleteForm.target === 'memories' && deleteForm.factType !== '' ? deleteForm.factType : null
		};
		try {
			const document = await client.deleteMemory(target, body);
			if (disposed) return;
			deleteOutcome =
				document.status === 'ok' ? { kind: 'ok', document } : { kind: 'unavailable', document };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			deleteOutcome = {
				kind: 'problem',
				code,
				message: messageOf(error) ?? memoryCodeText(code, 'The delete could not run.')
			};
		} finally {
			if (!disposed) deleteBusy = false;
		}
	}

	// --- local-model operations ----------------------------------------------

	async function runPreview(): Promise<void> {
		const client = api;
		const target = serverId;
		if (client === null || target === null || previewBusy || !previewFormValid) return;
		previewBusy = true;
		previewProblem = null;
		previewView = null;
		previewIdentity = null;
		const body: ModelLocalPreviewBody = {
			application_id: modelAppId.trim(),
			recipe: recipeText,
			target_os: targetOs.trim(),
			target_arch: targetArch.trim()
		};
		try {
			const shown = await client.previewModelInstall(target, body);
			if (disposed) return;
			// Identity guard: only THIS server's preview may paint.
			if (shown.server_id !== target) {
				previewProblem = {
					code: 'scope_mismatch',
					message: 'The preview names a different server — nothing is displayed.'
				};
				return;
			}
			previewView = shown;
			previewIdentity = { recipe: recipeText, target: `${targetOs.trim()}/${targetArch.trim()}` };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			previewProblem = {
				code,
				message: messageOf(error) ?? friendlyText(code, 'The prerequisite preview could not run.')
			};
		} finally {
			if (!disposed) previewBusy = false;
		}
	}

	/** Stop the bounded poll (terminal state or teardown); the watch block
	 * keeps displaying the last honest run view — the terminal receipt is
	 * the operator's proof and is never wiped by the poll stopping. */
	function stopPolling(): void {
		if (pollTimer !== null) {
			clearTimeout(pollTimer);
			pollTimer = null;
		}
	}

	async function pollWatchedRun(): Promise<void> {
		pollTimer = null;
		const client = api;
		const run = watchedRun;
		if (client === null || run === null || disposed) return;
		try {
			const detail = await client.readRun(run.runId);
			if (disposed || watchedRun === null || watchedRun.runId !== run.runId) return;
			watchedDetail = detail;
			const state = detail.state;
			if (state !== null && TERMINAL_RUN_STATES.has(state)) {
				stopPolling();
				// Registration truth (the pull writes the connection) — refresh
				// the registered-connection surface after a terminal pull.
				if (run.kind === 'pull') void refreshConnections();
				return;
			}
		} catch (error) {
			if (disposed || watchedRun === null || watchedRun.runId !== run.runId) return;
			const code = codeOf(error) ?? 'unavailable';
			watchProblem = {
				code,
				message: messageOf(error) ?? friendlyText(code, 'The run view could not be loaded.')
			};
			// A transient poll failure keeps the last honest detail; the loop
			// continues only for non-terminal runs.
		}
		if (!disposed && watchedRun !== null && watchedRun.runId === run.runId) {
			pollTimer = setTimeout(() => void pollWatchedRun(), POLL_MS);
		}
	}

	function beginWatching(run: WatchedRun): void {
		stopPolling();
		watchProblem = null;
		watchedRun = run;
		watchedDetail = null;
		pollTimer = setTimeout(() => void pollWatchedRun(), POLL_MS);
	}

	async function submitInstall(): Promise<void> {
		const client = api;
		const target = serverId;
		if (client === null || target === null || installBusy || !installReady) return;
		installBusy = true;
		installProblem = null;
		const body: ModelLocalSubmitBody = {
			run_id: crypto.randomUUID(),
			recipe: recipeText,
			target_os: targetOs.trim(),
			target_arch: targetArch.trim(),
			backup_confirmed: backupConfirmed
		};
		try {
			const created = await client.installModelService(target, modelAppId.trim(), body);
			if (disposed) return;
			beginWatching({
				runId: created.run.run_id,
				kind: 'install',
				deduplicated: created.deduplicated
			});
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			installProblem = {
				code,
				message: messageOf(error) ?? friendlyText(code, 'The install intent could not be submitted.')
			};
		} finally {
			if (!disposed) installBusy = false;
		}
	}

	async function submitPull(): Promise<void> {
		const client = api;
		const target = serverId;
		if (client === null || target === null || pullBusy || !pullReady) return;
		pullBusy = true;
		pullProblem = null;
		const body: ModelLocalPullBody = {
			run_id: crypto.randomUUID(),
			recipe: recipeText,
			target_os: targetOs.trim(),
			target_arch: targetArch.trim(),
			model: pullForm.model.trim(),
			license: pullForm.license.trim(),
			base_url: pullForm.baseUrl.trim()
		};
		try {
			const created = await client.pullModelWeights(target, modelAppId.trim(), body);
			if (disposed) return;
			beginWatching({ runId: created.run.run_id, kind: 'pull', deduplicated: created.deduplicated });
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			pullProblem = {
				code,
				message: messageOf(error) ?? friendlyText(code, 'The pull intent could not be submitted.')
			};
		} finally {
			if (!disposed) pullBusy = false;
		}
	}

	async function openConnection(name: string): Promise<void> {
		const client = api;
		if (client === null) return;
		connectionProblem = null;
		try {
			const stored = await client.readConnection(name);
			if (disposed) return;
			connectionView = stored;
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			connectionProblem = {
				code,
				message: messageOf(error) ?? friendlyText(code, `Connection "${name}" could not be loaded.`)
			};
		}
	}

	// Mount reads once; teardown (hide/unmount/error containment) flips
	// `disposed` and stops the bounded poll (ADR §7.2).
	$effect(() => {
		if (!usable) return;
		disposed = false;
		void loadAll();
		return () => {
			disposed = true;
			stopPolling();
		};
	});
	// Standalone mounts (outside PanelHost) release through onDestroy; inside
	// PanelHost both paths fire and the guard keeps disposal idempotent.
	onDestroy(() => {
		disposed = true;
		stopPolling();
	});
</script>

<div class="mai-km" data-testid="knowledge-models-root">
	{#if !usable}
		<div class="mai-km__state mai-km__state--error" role="alert" data-testid="panel-config-error">
			<p>This panel was opened without its memory/local-model service or outside a server scope.</p>
		</div>
	{:else if view.status === 'loading'}
		<div class="mai-km__state" role="status" data-testid="km-loading">
			<p>Loading memory and local-model status…</p>
		</div>
	{:else if view.status === 'error'}
		<div
			class="mai-km__state mai-km__state--error"
			role="alert"
			data-testid="km-error"
			data-error-code={view.code}
		>
			<p>{view.message}</p>
			<Button variant="secondary" onclick={loadAll} data-testid="km-retry">Retry</Button>
		</div>
	{:else}
		<!-- ===================== Memory availability ===================== -->
		<section class="mai-km__section" aria-label="Memory availability" data-testid="memory-status-section">
			<header class="mai-km__header">
				<Brain size={16} aria-hidden="true" />
				<h3 class="mai-km__heading">Hindsight memory</h3>
			</header>
			{#if statusOutcome.kind === 'ok'}
				<p class="mai-km__notice mai-km__notice--success" role="status" data-testid="memory-status-ok">
					Available — API version <code>{statusOutcome.document.api_version ?? 'unknown'}</code>
				</p>
			{:else if statusOutcome.kind === 'unavailable'}
				<div
					class="mai-km__notice mai-km__notice--warning"
					role="alert"
					data-testid="memory-status-unavailable"
					data-memory-code={statusOutcome.document.code ?? ''}
				>
					<p>
						Unavailable — {memoryCodeText(statusOutcome.document.code, 'the memory service reported an outage.')}
						{#if statusOutcome.document.error_type !== null}<span>(classified: {statusOutcome.document.error_type})</span>{/if}
					</p>
				</div>
			{:else if statusOutcome.kind === 'problem'}
				<div
					class="mai-km__notice mai-km__notice--danger"
					role="alert"
					data-testid="memory-status-problem"
					data-error-code={statusOutcome.code}
				>
					<p>{statusOutcome.message}</p>
				</div>
			{:else}
				<p class="mai-km__muted" role="status" data-testid="memory-status-idle">Availability not probed yet.</p>
			{/if}
			<Button variant="secondary" onclick={probeStatus} loading={statusBusy} data-testid="memory-status-refresh">
				Check availability
			</Button>
			{#if openModelSettings !== null}
				<Button variant="secondary" onclick={() => openModelSettings()} data-testid="memory-open-model-settings">
					Model settings
				</Button>
			{/if}
		</section>

		<!-- ===================== Recall ===================== -->
		<section class="mai-km__section" aria-label="Recall" data-testid="recall-section">
			<header class="mai-km__header">
				<Search size={16} aria-hidden="true" />
				<h3 class="mai-km__heading">Recall</h3>
			</header>
			<p class="mai-km__hint">
				One gated query through the memory.recall grant. Scope is "server" (this server's bank)
				or "shared:&lt;name&gt;" (the configured allowlist).
			</p>
			<div class="mai-km__form">
				<Field
					label="Scope"
					bind:value={recallForm.scope}
					error={recallErrors.scope ?? ''}
					hint='"server" or "shared:<name>".'
					autocomplete="off"
					spellcheck="false"
					data-testid="recall-scope-input"
				/>
				<Field
					label="Query"
					bind:value={recallForm.query}
					error={recallErrors.query ?? ''}
					hint={`What to look up, at most ${MAX_QUERY_CHARS} characters.`}
					autocomplete="off"
					data-testid="recall-query-input"
				/>
				<fieldset class="mai-km__caps">
					<legend>Fact types (empty = all)</legend>
					<label class="mai-km__cap"><input type="checkbox" bind:checked={recallForm.world} data-testid="recall-type-world" /> world</label>
					<label class="mai-km__cap"><input type="checkbox" bind:checked={recallForm.experience} data-testid="recall-type-experience" /> experience</label>
					<label class="mai-km__cap"><input type="checkbox" bind:checked={recallForm.observation} data-testid="recall-type-observation" /> observation</label>
				</fieldset>
				<Select
					label="Budget"
					bind:value={recallForm.budget}
					data-testid="recall-budget-select"
				>
					{#each BUDGETS as budget (budget)}
						<option value={budget}>{budget}</option>
					{/each}
				</Select>
				<Field
					label="Max tokens"
					bind:value={recallForm.maxTokens}
					error={recallErrors.maxTokens ?? ''}
					hint={`Optional integer, ${MIN_TOKEN_BUDGET}–${MAX_TOKEN_BUDGET} (producer default 1024).`}
					inputmode="numeric"
					autocomplete="off"
					data-testid="recall-max-tokens-input"
				/>
				<Button onclick={runRecall} loading={recallBusy} disabled={!recallValid} data-testid="recall-run">
					Recall
				</Button>
			</div>

			{#if recallOutcome.kind === 'ok'}
				<div class="mai-km__card" data-testid="recall-result">
					<div class="mai-km__cardhead">
						<Database size={14} aria-hidden="true" />
						<p class="mai-km__cardhead-meta" data-testid="recall-meta">
							{recallOutcome.document.results.length} hit(s) from <code>{recallOutcome.document.bank}</code>
							· trust {recallOutcome.document.trust} · role {recallOutcome.document.role}
						</p>
					</div>
					{#if recallOutcome.document.results_truncated}
						<p class="mai-km__notice mai-km__notice--warning" role="status" data-testid="recall-truncated">
							Results were truncated by the producer — the page is bounded, not complete.
						</p>
					{/if}
					{#if recallOutcome.document.results.length === 0}
						<p class="mai-km__muted" role="status" data-testid="recall-empty">
							The bank answered successfully with no matching memories.
						</p>
					{:else}
						<ul class="mai-km__list" data-testid="recall-hits">
							{#each recallOutcome.document.results as hit (hit.id)}
								<li class="mai-km__hit" data-testid="recall-hit">
									<p class="mai-km__hit-meta" data-testid="recall-hit-meta">
										#{hit.rank} · {hit.type} · score {hit.score}
										{#if hit.occurred_start !== null}· occurred {hit.occurred_start}{/if}
										{#if hit.mentioned_at !== null}· mentioned {hit.mentioned_at}{/if}
									</p>
									<p class="mai-km__hit-text" data-testid="recall-hit-text">{hit.text}</p>
									{#if hit.truncated}
										<p class="mai-km__muted" data-testid="recall-hit-cut">Excerpt cut by the per-hit budget.</p>
									{/if}
									<p class="mai-km__hit-cite" data-testid="recall-hit-cite">
										citation <code>{hit.citation}</code>{#if hit.document_id !== null} · document <code>{hit.document_id}</code>{/if}
									</p>
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			{:else if recallOutcome.kind === 'unavailable'}
				<div
					class="mai-km__notice mai-km__notice--warning"
					role="alert"
					data-testid="recall-unavailable"
					data-memory-code={recallOutcome.document.code ?? ''}
				>
					<p>
						Recall could not run — {memoryCodeText(recallOutcome.document.code, 'the memory service reported an outage.')}
						No results are shown because none were retrieved.
					</p>
				</div>
			{:else if recallOutcome.kind === 'problem'}
				<div class="mai-km__notice mai-km__notice--danger" role="alert" data-testid="recall-problem" data-error-code={recallOutcome.code}>
					<p>{recallOutcome.message}</p>
				</div>
			{/if}
		</section>

		<!-- ===================== Retain ===================== -->
		<section class="mai-km__section" aria-label="Retain" data-testid="retain-section">
			<header class="mai-km__header">
				<NotebookPen size={16} aria-hidden="true" />
				<h3 class="mai-km__heading">Retain</h3>
			</header>
			<p class="mai-km__hint">
				Synchronous write with the producer's real receipt — a failed retain is shown as an
				outage, never as a success.
			</p>
			<div class="mai-km__form">
				<Field
					label="Scope"
					bind:value={retainScope}
					error={retainErrors.scope ?? ''}
					hint='"server" or "shared:<name>".'
					autocomplete="off"
					spellcheck="false"
					data-testid="retain-scope-input"
				/>
				{#each retainItems as item, index (index)}
					<div class="mai-km__item" data-testid="retain-item">
						<div class="mai-km__itemhead">
							<span class="mai-km__ord">#{index + 1}</span>
						</div>
						<Field
							label={`Content ${index + 1}`}
							bind:value={item.content}
							error={retainErrors.items[index]?.content ?? ''}
							hint={`At most ${MAX_CONTENT_CHARS} characters.`}
							autocomplete="off"
							data-testid="retain-content-input"
						/>
						<Field
							label="Context (optional)"
							bind:value={item.context}
							error={retainErrors.items[index]?.context ?? ''}
							hint={`At most ${MAX_CONTEXT_CHARS} characters.`}
							autocomplete="off"
							data-testid="retain-context-input"
						/>
						<Field
							label="Document id (optional)"
							bind:value={item.documentId}
							error={retainErrors.items[index]?.documentId ?? ''}
							hint={'Document id must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127}.'}
							autocomplete="off"
							spellcheck="false"
							data-testid="retain-document-id-input"
						/>
						{#if retainItems.length > 1}
							<Button variant="secondary" onclick={() => removeRetainItem(index)} data-testid="retain-item-remove">
								Remove item {index + 1}
							</Button>
						{/if}
					</div>
				{/each}
				<Button variant="secondary" onclick={addRetainItem} disabled={retainItems.length >= MAX_RETAIN_ITEMS} data-testid="retain-item-add">
					Add item (at most {MAX_RETAIN_ITEMS})
				</Button>
				<Button onclick={runRetain} loading={retainBusy} disabled={!retainValid} data-testid="retain-run">
					Retain {retainItems.length} item(s)
				</Button>
			</div>

			{#if retainOutcome.kind === 'ok'}
				<div class="mai-km__notice mai-km__notice--success" role="status" data-testid="retain-result">
					<p>
						Retained {retainOutcome.document.receipt?.items_count ?? 0} item(s) into
						<code>{retainOutcome.document.bank}</code> — operation <code>{retainOutcome.document.receipt?.operation_id}</code>
						({retainOutcome.document.receipt?.usage.total_tokens} tokens).
					</p>
				</div>
			{:else if retainOutcome.kind === 'unavailable'}
				<div class="mai-km__notice mai-km__notice--warning" role="alert" data-testid="retain-unavailable" data-memory-code={retainOutcome.document.code ?? ''}>
					<p>
						Retain failed — {memoryCodeText(retainOutcome.document.code, 'the memory service reported an outage.')}
						Nothing was retained.
					</p>
				</div>
			{:else if retainOutcome.kind === 'problem'}
				<div class="mai-km__notice mai-km__notice--danger" role="alert" data-testid="retain-problem" data-error-code={retainOutcome.code}>
					<p>{retainOutcome.message}</p>
				</div>
			{/if}
		</section>

		<!-- ===================== Reflect ===================== -->
		<section class="mai-km__section" aria-label="Reflect" data-testid="reflect-section">
			<header class="mai-km__header">
				<Lightbulb size={16} aria-hidden="true" />
				<h3 class="mai-km__heading">Reflect</h3>
			</header>
			<p class="mai-km__hint">One bounded generated answer grounded in the scoped bank.</p>
			<div class="mai-km__form">
				<Field
					label="Scope"
					bind:value={reflectForm.scope}
					error={reflectErrors.scope ?? ''}
					hint='"server" or "shared:<name>".'
					autocomplete="off"
					spellcheck="false"
					data-testid="reflect-scope-input"
				/>
				<Field
					label="Query"
					bind:value={reflectForm.query}
					error={reflectErrors.query ?? ''}
					hint={`Question to answer from memory, at most ${MAX_QUERY_CHARS} characters.`}
					autocomplete="off"
					data-testid="reflect-query-input"
				/>
				<Field
					label="Max tokens"
					bind:value={reflectForm.maxTokens}
					error={reflectErrors.maxTokens ?? ''}
					hint={`Optional integer, ${MIN_TOKEN_BUDGET}–${MAX_TOKEN_BUDGET}.`}
					inputmode="numeric"
					autocomplete="off"
					data-testid="reflect-max-tokens-input"
				/>
				<Button onclick={runReflect} loading={reflectBusy} disabled={!reflectValid} data-testid="reflect-run">
					Reflect
				</Button>
			</div>

			{#if reflectOutcome.kind === 'ok'}
				<div class="mai-km__card" data-testid="reflect-result">
					<p class="mai-km__answer" data-testid="reflect-answer">{reflectOutcome.document.text}</p>
					{#if reflectOutcome.document.truncated}
						<p class="mai-km__notice mai-km__notice--warning" role="status" data-testid="reflect-truncated">
							The answer was cut at the producer's character budget.
						</p>
					{/if}
					<p class="mai-km__muted" data-testid="reflect-basis">
						{#if reflectOutcome.document.based_on.length > 0}
							Based on {reflectOutcome.document.based_on.length} memory id(s).
						{:else}
							No basis ids were reported.
						{/if}
					</p>
				</div>
			{:else if reflectOutcome.kind === 'unavailable'}
				<div class="mai-km__notice mai-km__notice--warning" role="alert" data-testid="reflect-unavailable" data-memory-code={reflectOutcome.document.code ?? ''}>
					<p>Reflect could not run — {memoryCodeText(reflectOutcome.document.code, 'the memory service reported an outage.')}</p>
				</div>
			{:else if reflectOutcome.kind === 'problem'}
				<div class="mai-km__notice mai-km__notice--danger" role="alert" data-testid="reflect-problem" data-error-code={reflectOutcome.code}>
					<p>{reflectOutcome.message}</p>
				</div>
			{/if}
		</section>

		<!-- ===================== Export ===================== -->
		<section class="mai-km__section" aria-label="Export" data-testid="export-section">
			<header class="mai-km__header">
				<FileDown size={16} aria-hidden="true" />
				<h3 class="mai-km__heading">Export</h3>
			</header>
			<p class="mai-km__hint">
				Backup-grade bank read (the memory.recall grant class): the manifest IS the data and
				passes through unmodified — banks are backed up, never silently rebuilt.
			</p>
			<div class="mai-km__form">
				<Field
					label="Scope"
					bind:value={exportScope}
					error={exportScopeError ?? ''}
					hint='"server" or "shared:<name>".'
					autocomplete="off"
					spellcheck="false"
					data-testid="export-scope-input"
				/>
				<Button onclick={runExport} loading={exportBusy} disabled={exportScopeError !== null} data-testid="export-run">
					Export manifest
				</Button>
			</div>

			{#if exportOutcome.kind === 'ok'}
				<div class="mai-km__card" data-testid="export-result">
					<p class="mai-km__telemetry" data-testid="export-meta">
						Manifest received ({JSON.stringify(exportOutcome.document.manifest ?? null).length} bytes serialized).
					</p>
					<pre class="mai-km__pre" data-testid="export-manifest">{JSON.stringify(exportOutcome.document.manifest ?? null, null, 2)}</pre>
				</div>
			{:else if exportOutcome.kind === 'unavailable'}
				<div class="mai-km__notice mai-km__notice--warning" role="alert" data-testid="export-unavailable" data-memory-code={exportOutcome.document.code ?? ''}>
					<p>Export could not run — {memoryCodeText(exportOutcome.document.code, 'the memory service reported an outage.')}</p>
				</div>
			{:else if exportOutcome.kind === 'problem'}
				<div class="mai-km__notice mai-km__notice--danger" role="alert" data-testid="export-problem" data-error-code={exportOutcome.code}>
					<p>{exportOutcome.message}</p>
				</div>
			{/if}
		</section>

		<!-- ===================== Delete ===================== -->
		<section class="mai-km__section" aria-label="Delete" data-testid="delete-section">
			<header class="mai-km__header">
				<Trash2 size={16} aria-hidden="true" />
				<h3 class="mai-km__heading">Delete</h3>
			</header>
			<p class="mai-km__hint">
				Explicit and receipted (the memory.retain grant class): the target decides
				memories-vs-bank — nothing is ever inferred.
			</p>
			<div class="mai-km__form">
				<Field
					label="Scope"
					bind:value={deleteForm.scope}
					error={deleteErrors.scope ?? ''}
					hint='"server" or "shared:<name>".'
					autocomplete="off"
					spellcheck="false"
					data-testid="delete-scope-input"
				/>
				<fieldset class="mai-km__caps">
					<legend>Target</legend>
					<label class="mai-km__cap">
						<input type="radio" name="mai-km-delete-target" value="memories" bind:group={deleteForm.target} data-testid="delete-target-memories" />
						Memories
					</label>
					<label class="mai-km__cap">
						<input type="radio" name="mai-km-delete-target" value="bank" bind:group={deleteForm.target} data-testid="delete-target-bank" />
						Whole bank
					</label>
				</fieldset>
				{#if deleteForm.target === 'memories'}
					<Select
						label="Fact type"
						bind:value={deleteForm.factType}
						error={deleteErrors.factType ?? ''}
						data-testid="delete-fact-type-select"
					>
						<option value="">(all types)</option>
						{#each FACT_TYPES as factType (factType)}
							<option value={factType}>{factType}</option>
						{/each}
					</Select>
				{:else}
					<Field
						label="Confirmation"
						bind:value={deleteForm.confirm}
						error={deleteErrors.confirm ?? ''}
						hint={`Type "delete ${deleteForm.scope.trim()}" to confirm the whole-bank deletion.`}
						autocomplete="off"
						spellcheck="false"
						data-testid="delete-confirm-input"
					/>
				{/if}
				<Button variant="danger" onclick={runDelete} loading={deleteBusy} disabled={!deleteValid} data-testid="delete-run">
					Delete
				</Button>
			</div>

			{#if deleteOutcome.kind === 'ok'}
				<div class="mai-km__notice mai-km__notice--success" role="status" data-testid="delete-result">
					<p>
						{deleteOutcome.document.operation} on <code>{deleteOutcome.document.bank}</code> —
						{deleteOutcome.document.deleted_count === null ? 'receipt without a count' : `${deleteOutcome.document.deleted_count} memory record(s) deleted`}.
					</p>
				</div>
			{:else if deleteOutcome.kind === 'unavailable'}
				<div class="mai-km__notice mai-km__notice--warning" role="alert" data-testid="delete-unavailable" data-memory-code={deleteOutcome.document.code ?? ''}>
					<p>Delete failed — {memoryCodeText(deleteOutcome.document.code, 'the memory service reported an outage.')} Nothing was deleted.</p>
				</div>
			{:else if deleteOutcome.kind === 'problem'}
				<div class="mai-km__notice mai-km__notice--danger" role="alert" data-testid="delete-problem" data-error-code={deleteOutcome.code}>
					<p>{deleteOutcome.message}</p>
				</div>
			{/if}
		</section>

		<!-- ===================== Local models ===================== -->
		<section class="mai-km__section" aria-label="Local models" data-testid="local-models-section">
			<header class="mai-km__header">
				<Cog size={16} aria-hidden="true" />
				<h3 class="mai-km__heading">Local models</h3>
			</header>
			<p class="mai-km__hint">
				Prerequisite preview measures REAL target facts; the executor re-verifies fresh facts
				behind the full gate chain before any effect. The pull IS the registration path —
				there is no separate registration control.
			</p>

			<div class="mai-km__form">
				<Field
					label="Application id"
					bind:value={modelAppId}
					error={modelAppId.trim() !== '' && !UUID_RE.test(modelAppId.trim()) ? 'Application id must be a canonical UUID.' : ''}
					hint={openApplications !== null ? 'The adopted local-inference application (Applications panel).' : 'The adopted local-inference application id.'}
					autocomplete="off"
					spellcheck="false"
					data-testid="model-app-id-input"
				/>
				{#if openApplications !== null}
					<Button variant="secondary" onclick={() => openApplications(serverId ?? '')} data-testid="model-open-applications">
						Open Applications
					</Button>
				{/if}
				<label class="mai-km__select-label">
					<span>Recipe document</span>
					<textarea
						class="mai-km__textarea"
						bind:value={recipeText}
						rows={8}
						spellcheck="false"
						autocomplete="off"
						aria-label="Recipe document"
						data-testid="model-recipe-input"
					></textarea>
				</label>
				<p class="mai-km__hint">ONE canonical recipe document (catalogue revision, e.g. local-inference.yaml) — split at document boundaries.</p>
				<Field
					label="Target OS"
					bind:value={targetOs}
					error={targetOs.trim().length > MAX_TARGET_CHARS ? `Target OS must be at most ${MAX_TARGET_CHARS} characters.` : ''}
					hint="The explicit observed target the plan binds."
					autocomplete="off"
					spellcheck="false"
					data-testid="model-target-os-input"
				/>
				<Field
					label="Target arch"
					bind:value={targetArch}
					error={targetArch.trim().length > MAX_TARGET_CHARS ? `Target arch must be at most ${MAX_TARGET_CHARS} characters.` : ''}
					hint="The explicit observed target the plan binds."
					autocomplete="off"
					spellcheck="false"
					data-testid="model-target-arch-input"
				/>
				<Button onclick={runPreview} loading={previewBusy} disabled={!previewFormValid} data-testid="model-preview-run">
					Preview prerequisites
				</Button>
			</div>

			{#if previewProblem !== null}
				<div class="mai-km__notice mai-km__notice--danger" role="alert" data-testid="model-preview-problem" data-error-code={previewProblem.code}>
					<p>{previewProblem.message}</p>
				</div>
			{/if}

			{#if previewView !== null && previewIdentityMatches}
				<div
					class="mai-km__preview"
					data-testid="model-preview"
					data-verdict={previewView.verdict}
				>
					<p
						class="mai-km__notice {previewView.verdict === 'satisfied' ? 'mai-km__notice--success' : 'mai-km__notice--warning'}"
						role="status"
						data-testid="model-preview-verdict"
					>
						{previewVerdictText}
					</p>
					<dl class="mai-km__rows">
						<div class="mai-km__row"><dt>Plan</dt><dd><code>{shortHash(previewView.plan.plan_sha256)}</code> · recipe {shortHash(previewView.plan.recipe_sha256)}</dd></div>
						<div class="mai-km__row"><dt>Image</dt><dd><code>{previewView.plan.image}</code></dd></div>
						<div class="mai-km__row"><dt>Project</dt><dd>{previewView.plan.project_name} · {previewView.plan.target_os}/{previewView.plan.target_arch}</dd></div>
						<div class="mai-km__row"><dt>Prerequisites</dt><dd data-testid="model-preview-prereqs">{previewView.plan.prerequisites.join(', ')}</dd></div>
						<div class="mai-km__row"><dt>Required grants</dt><dd>{previewView.plan.required_grants.join(', ')}</dd></div>
						<div class="mai-km__row"><dt>Measured facts</dt><dd data-testid="model-preview-facts">{Object.keys(previewView.facts).length} fact(s)</dd></div>
						<div class="mai-km__row"><dt>Limitations</dt><dd>{previewView.plan.limitations.join('; ')}</dd></div>
					</dl>
					{#if previewView.hardware_unsatisfied.length > 0}
						<p class="mai-km__muted" data-testid="model-preview-unsatisfied">
							Unsatisfied: {previewView.hardware_unsatisfied.join(', ')}
						</p>
					{/if}
					{#if previewView.unknown.length > 0}
						<p class="mai-km__muted" data-testid="model-preview-unknown">
							Unmeasurable (counts as NOT satisfied): {previewView.unknown.join(', ')}
						</p>
					{/if}
					{#if previewView.measurement_notes.length > 0}
						<p class="mai-km__muted" data-testid="model-preview-notes">
							Notes: {previewView.measurement_notes.join('; ')}
						</p>
					{/if}
				</div>
			{/if}

			<div class="mai-km__form">
				<div class="mai-km__subhead">
					<Wrench size={14} aria-hidden="true" />
					<h4 class="mai-km__subheading">Install service</h4>
				</div>
				<label class="mai-km__cap">
					<input type="checkbox" bind:checked={backupConfirmed} data-testid="model-backup-confirmed" />
					Backup confirmed (explicit operator decision for pre-existing volume data)
				</label>
				<Button onclick={submitInstall} loading={installBusy} disabled={!installReady} data-testid="model-install-run">
					Submit install intent
				</Button>
				{#if !installReady && previewView !== null && !previewIdentityMatches}
					<p class="mai-km__muted" role="status" data-testid="model-install-stale">
						The recipe or target changed since the preview — run a new preview first.
					</p>
				{/if}
				{#if installProblem !== null}
					<div class="mai-km__notice mai-km__notice--danger" role="alert" data-testid="model-install-problem" data-error-code={installProblem.code}>
						<p>{installProblem.message}</p>
					</div>
				{/if}

				<div class="mai-km__subhead">
					<Download size={14} aria-hidden="true" />
					<h4 class="mai-km__subheading">Pull weights (the pull IS the registration)</h4>
				</div>
				<Field
					label="Model"
					bind:value={pullForm.model}
					error={pullErrors.model ?? ''}
					hint="Upstream reference, e.g. qwen2.5:7b-instruct-q4_K_M."
					autocomplete="off"
					spellcheck="false"
					data-testid="model-pull-model-input"
				/>
				<Field
					label="License"
					bind:value={pullForm.license}
					error={pullErrors.license ?? ''}
					hint={`REQUIRED metadata naming the license you acquire the weights under (at most ${MAX_LICENSE_CHARS} characters).`}
					autocomplete="off"
					spellcheck="false"
					data-testid="model-pull-license-input"
				/>
				<Field
					label="Endpoint base URL"
					bind:value={pullForm.baseUrl}
					error={pullErrors.baseUrl ?? ''}
					hint="Controller-reachable OpenAI-compatible endpoint of the installed service."
					autocomplete="off"
					spellcheck="false"
					data-testid="model-pull-base-url-input"
				/>
				<Button onclick={submitPull} loading={pullBusy} disabled={!pullReady} data-testid="model-pull-run">
					Submit pull intent
				</Button>
				{#if pullProblem !== null}
					<div class="mai-km__notice mai-km__notice--danger" role="alert" data-testid="model-pull-problem" data-error-code={pullProblem.code}>
						<p>{pullProblem.message}</p>
					</div>
				{/if}
			</div>

			{#if watchedRun !== null}
				<div class="mai-km__watch" data-testid="model-run-watch" data-run-kind={watchedRun.kind}>
					<div class="mai-km__cardhead">
						<ListChecks size={14} aria-hidden="true" />
						<p class="mai-km__cardhead-meta" data-testid="model-run-id">
							{watchedRun.kind} run <code>{watchedRun.runId}</code>
							{#if watchedRun.deduplicated}(deduplicated: an identical intent was already running){/if}
						</p>
					</div>
					{#if watchProblem !== null}
						<div class="mai-km__notice mai-km__notice--warning" role="alert" data-testid="model-run-problem" data-error-code={watchProblem.code}>
							<p>{watchProblem.message}</p>
						</div>
					{/if}
					{#if watchedDetail !== null}
						<p class="mai-km__runstate" data-testid="model-run-state">{runStateText(watchedDetail.state)}</p>
						{#if watchedDetail.state !== null && TERMINAL_RUN_STATES.has(watchedDetail.state)}
							<p class="mai-km__muted" data-testid="model-run-terminal">
								{watchedDetail.state === 'succeeded'
									? 'The producer verified this run as succeeded.'
									: 'This run did NOT succeed — nothing is claimed as installed.'}
							</p>
						{/if}
						<ul class="mai-km__list" data-testid="model-run-steps">
							{#each watchedDetail.steps as step (step.seq)}
								<li class="mai-km__list-row mai-km__step" data-testid="model-run-step">
									<span class="mai-km__step-seq">{step.seq}</span>
									<code>{step.capability_id}</code>
									<span class="mai-km__step-state">— {stepStateText(step.state)}</span>
								</li>
							{/each}
						</ul>
					{:else}
						<p class="mai-km__muted" role="status" data-testid="model-run-pending">Waiting for the first run view…</p>
					{/if}
					{#if openJobs !== null}
						<Button variant="secondary" onclick={() => openJobs(serverId ?? '')} data-testid="model-run-open-jobs">
							Open Jobs
						</Button>
					{/if}
				</div>
			{/if}

			<div class="mai-km__form">
				<div class="mai-km__subhead">
					<Cable size={14} aria-hidden="true" />
					<h4 class="mai-km__subheading">Registered connections</h4>
				</div>
				<ul class="mai-km__list" data-testid="connection-list">
					{#each connections as ref (ref.name)}
						<li class="mai-km__list-row">
							<code data-testid="connection-ref-{ref.name}">{ref.name}</code>
							<span class="mai-km__refmeta">revision {ref.revision}</span>
							<Button variant="secondary" onclick={() => openConnection(ref.name)} data-testid="connection-open-{ref.name}">
								View
							</Button>
						</li>
					{/each}
				</ul>
				{#if connections.length === 0}
					<p class="mai-km__muted" role="status" data-testid="connections-empty">
						No model connections stored yet — a pull registers one only after the endpoint
						really answers.
					</p>
				{/if}
				<Button variant="secondary" onclick={refreshConnections} data-testid="connection-refresh">
					Refresh connections
				</Button>
				{#if connectionProblem !== null}
					<div class="mai-km__notice mai-km__notice--danger" role="alert" data-testid="connection-problem" data-error-code={connectionProblem.code}>
						<p>{connectionProblem.message}</p>
					</div>
				{/if}
				{#if connectionView !== null}
					<div class="mai-km__preview" data-testid="connection-view">
						<dl class="mai-km__rows">
							<div class="mai-km__row"><dt>Record</dt><dd><code data-testid="connection-view-name">{connectionView.name}</code></dd></div>
							<div class="mai-km__row"><dt>Base URL</dt><dd><code data-testid="connection-view-url">{connectionView.base_url}</code></dd></div>
							<div class="mai-km__row">
								<dt>Credential reference</dt>
								<dd data-testid="connection-view-credential-ref">
									{connectionView.credential_ref === null
										? 'none — unauthenticated endpoint'
										: connectionView.credential_ref}
								</dd>
							</div>
							<div class="mai-km__row"><dt>Tool calls</dt><dd data-testid="connection-view-tool-calls">{connectionView.capabilities.tool_calls ? 'declared (observed by the producer probe)' : 'not observed'}</dd></div>
							<div class="mai-km__row"><dt>Reasoning</dt><dd data-testid="connection-view-reasoning">{connectionView.capabilities.reasoning ? 'declared' : 'not claimed'}</dd></div>
							<div class="mai-km__row"><dt>Revision</dt><dd>{connectionView.revision}</dd></div>
						</dl>
					</div>
				{/if}
			</div>

			<div class="mai-km__form">
				<div class="mai-km__subhead">
					<KeyRound size={14} aria-hidden="true" />
					<h4 class="mai-km__subheading">Credential references (1Password-aware)</h4>
				</div>
				<p class="mai-km__hint">
					Credentials resolve by REFERENCE (C03 rule 6): a connection stores the .env variable
					NAME; the optional 1Password provider reads op://&lt;vault&gt;/&lt;item&gt;/&lt;field&gt;
					references host-side. Never paste a secret value — nothing typed here is sent anywhere.
				</p>
				<Field
					label="Check a reference shape"
					bind:value={refCheckInput}
					hint="Paste a .env name or an op:// reference — never a value."
					autocomplete="off"
					spellcheck="false"
					data-testid="credential-ref-check-input"
				/>
				{#if refClassification.kind === 'env-name'}
					<p class="mai-km__notice mai-km__notice--success" role="status" data-testid="credential-ref-classified">
						Valid .env reference name — this is what a connection's credential-reference field
						stores (edit it in Model settings).
					</p>
				{:else if refClassification.kind === 'op-reference'}
					<p class="mai-km__notice mai-km__notice--success" role="status" data-testid="credential-ref-classified">
						Valid 1Password reference — it belongs as the .env VALUE of a credential reference;
						the optional provider resolves it host-side and the secret never enters MAI.
					</p>
				{:else if refClassification.kind === 'invalid'}
					<p class="mai-km__notice mai-km__notice--warning" role="alert" data-testid="credential-ref-invalid">
						{refClassification.reason}
					</p>
				{/if}
			</div>
		</section>
	{/if}
</div>

<style>
	.mai-km {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-4);
		padding: var(--mai-space-3);
		color: var(--mai-color-text);
		font: var(--mai-text-base) var(--mai-font-sans);
	}

	.mai-km__state {
		padding: var(--mai-space-3);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface);
	}

	.mai-km__state--error {
		color: var(--mai-status-danger-text);
	}

	.mai-km__section {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		padding: var(--mai-space-4);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-lg);
		background: var(--mai-color-surface-raised);
	}

	/* Section header band: brass mark + Cinzel title on the header surface. */
	.mai-km__header {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		padding: var(--mai-space-2) var(--mai-space-3);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface-header);
	}

	.mai-km__header :global(svg),
	.mai-km__cardhead :global(svg) {
		flex: none;
		color: var(--mai-color-brass);
	}

	.mai-km__heading {
		margin: 0;
		color: var(--mai-color-brass);
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-base);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	/* Card-level band: mono telemetry line under a brass mark. */
	.mai-km__cardhead {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		padding: var(--mai-space-1) var(--mai-space-2);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface-header);
	}

	.mai-km__cardhead-meta {
		margin: 0;
		color: var(--mai-color-text);
		font: var(--mai-text-xs) var(--mai-font-mono);
	}

	/* Raised result card (recall / reflect / export receipts). */
	.mai-km__card {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		width: 100%;
		padding: var(--mai-space-3);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface);
	}

	.mai-km__subhead {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		margin-top: var(--mai-space-2);
	}

	.mai-km__subhead :global(svg) {
		flex: none;
		color: var(--mai-color-brass-dim);
	}

	.mai-km__subheading {
		margin: 0;
		color: var(--mai-color-brass-dim);
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	.mai-km__hint,
	.mai-km__muted {
		margin: 0;
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-sm);
	}

	.mai-km__form {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		align-items: flex-start;
	}

	.mai-km__form > :global(.mai-field) {
		width: 100%;
	}

	.mai-km__item {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		width: 100%;
		padding: var(--mai-space-2);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface);
	}

	/* Retain-record band: mono ordinal on a full-bleed header strip. */
	.mai-km__itemhead {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		padding: var(--mai-space-1) var(--mai-space-2);
		margin: calc(-1 * var(--mai-space-2)) calc(-1 * var(--mai-space-2)) 0;
		border-left: 3px solid var(--mai-color-brass-dim);
		border-radius: var(--mai-radius-md) var(--mai-radius-md) 0 0;
		background: var(--mai-color-surface-header);
	}

	.mai-km__ord {
		color: var(--mai-color-brass-dim);
		font: var(--mai-text-xs) var(--mai-font-mono);
		letter-spacing: 0.06em;
	}

	.mai-km__caps {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mai-space-3);
		margin: 0;
		padding: var(--mai-space-3);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface);
	}

	.mai-km__caps legend {
		padding: 0 var(--mai-space-2);
		color: var(--mai-color-brass-dim);
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	.mai-km__cap {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-1);
		font-size: var(--mai-text-sm);
	}

	.mai-km__select-label {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		width: 100%;
		font-size: var(--mai-text-sm);
	}

	.mai-km__textarea {
		width: 100%;
		padding: var(--mai-space-1) var(--mai-space-2);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-bg);
		color: var(--mai-color-text);
		font: var(--mai-text-sm) var(--mai-font-mono);
	}

	.mai-km__notice {
		margin: 0;
		padding: var(--mai-space-2);
		border: var(--mai-border-width) solid var(--mai-status-neutral-border);
		border-radius: var(--mai-radius-md);
		font-size: var(--mai-text-sm);
	}

	.mai-km__notice--success {
		color: var(--mai-status-success-text);
		background: var(--mai-status-success-bg);
		border-color: var(--mai-status-success-border);
	}

	.mai-km__notice--warning {
		color: var(--mai-status-warning-text);
		background: var(--mai-status-warning-bg);
		border-color: var(--mai-status-warning-border);
	}

	.mai-km__notice--danger {
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
		border-color: var(--mai-status-danger-border);
	}

	.mai-km__list {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		width: 100%;
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.mai-km__list-row {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		padding: var(--mai-space-1) 0;
	}

	.mai-km__refmeta {
		color: var(--mai-color-text-muted);
		font: var(--mai-text-xs) var(--mai-font-mono);
	}

	.mai-km__hit {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-space-3);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface);
	}

	/* Hit band: rank/type/score/ordinals as mono telemetry on the header surface. */
	.mai-km__hit-meta {
		margin: 0;
		padding: var(--mai-space-1) var(--mai-space-2);
		border-left: 2px solid var(--mai-color-brass-dim);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface-header);
		color: var(--mai-color-text-muted);
		font: var(--mai-text-xs) var(--mai-font-mono);
	}

	.mai-km__hit-cite {
		margin: 0;
		padding: var(--mai-space-1) var(--mai-space-2);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface-raised);
		color: var(--mai-color-text-muted);
		font: var(--mai-text-xs) var(--mai-font-mono);
	}

	.mai-km__hit-text {
		margin: 0;
		font-size: var(--mai-text-sm);
		white-space: pre-wrap;
	}

	.mai-km__answer {
		margin: 0;
		padding: var(--mai-space-3);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface);
		white-space: pre-wrap;
	}

	.mai-km__pre {
		max-height: 320px;
		margin: 0;
		padding: var(--mai-space-2);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		overflow: auto;
		background: var(--mai-color-bg);
		font: var(--mai-text-xs) var(--mai-font-mono);
	}

	/* Mono telemetry strip (serialized manifest size, receipts). */
	.mai-km__telemetry {
		margin: 0;
		padding: var(--mai-space-1) var(--mai-space-2);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface-raised);
		color: var(--mai-color-text-muted);
		font: var(--mai-text-xs) var(--mai-font-mono);
		overflow-wrap: anywhere;
	}

	.mai-km__rows {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		width: 100%;
		margin: 0;
	}

	.mai-km__row {
		display: grid;
		grid-template-columns: 160px 1fr;
		gap: var(--mai-space-2);
	}

	.mai-km__row > dt {
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-xs);
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	.mai-km__row > dd {
		margin: 0;
		font-size: var(--mai-text-sm);
		overflow-wrap: anywhere;
	}

	.mai-km__preview,
	.mai-km__watch {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		width: 100%;
		padding: var(--mai-space-3);
		border: var(--mai-border-width) solid var(--mai-color-border-strong);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface);
	}

	.mai-km__preview[data-verdict='satisfied'] {
		border-color: var(--mai-status-success-border);
	}

	.mai-km__preview[data-verdict='unsatisfied'] {
		border-color: var(--mai-status-warning-border);
	}

	.mai-km__watch[data-run-kind='pull'] {
		border-color: var(--mai-color-brass-dim);
	}

	/* Watched-run state line and step rows. */
	.mai-km__runstate {
		margin: 0;
		font-size: var(--mai-text-base);
		font-weight: var(--mai-weight-semibold);
	}

	.mai-km__step {
		padding: var(--mai-space-1) var(--mai-space-2);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface-raised);
	}

	.mai-km__step-seq {
		flex: none;
		min-width: 3ch;
		padding: 0 var(--mai-space-1);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface-header);
		color: var(--mai-color-brass);
		font: var(--mai-text-xs) var(--mai-font-mono);
		text-align: center;
	}

	.mai-km__step-state {
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-sm);
	}

	code {
		padding: 0 var(--mai-space-1);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface-raised);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		overflow-wrap: anywhere;
	}
</style>
