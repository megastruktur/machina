<script module lang="ts">
	/**
	 * KnowledgeAndModels — reusable Hindsight memory panel body (card
	 * `mai-11-web-memory-models`, P11; service row `mai-web`, contracts
	 * C01/C03/C06/C07/C08/C11).
	 *
	 * Layer: feature panel body (windowed-workspace ADR-0001 §2 layer table).
	 * It owns its one domain view and nothing else:
	 *
	 *  - Explicit context only: a pinned-scope view. A server window targets
	 *    `panel.scope.server_id`; a global (Global / Fleet) window targets
	 *    the fleet sentinel server id (`MEMORY_GLOBAL_SERVER_ID`) — this
	 *    module reads no launcher/current-server value anywhere, so there is
	 *    no hidden global mutation target (ADR §3.2, NT5). Memory scopes are
	 *    the producer's vocabulary: `"server"` (the target's own bank) or
	 *    `"shared:<name>"` (the configured allowlist); a payload can never
	 *    name a raw bank.
	 *  - Explicit services only: the memory API enters through
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
	 *  - No UI-only memory state: retained/deleted truth is exactly what the
	 *    producer's receipt reports. The panel never infers or remembers
	 *    success.
	 *  - Credential non-disclosure (C03 rule 6): no value field exists on
	 *    this surface — memory payloads never accept or return a credential
	 *    value, and none is entered, sent, stored, displayed or logged.
	 *  - Mount reads, never writes (ADR §7.2): opening issues only the
	 *    status probe. Every write happens on an explicit action;
	 *    destructive bank deletion requires an explicit typed confirmation.
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
		Database,
		FileDown,
		Lightbulb,
		NotebookPen,
		Search,
		Trash2
	} from 'lucide-svelte';
	import Button from '../ui/Button.svelte';
	import Field from '../ui/Field.svelte';
	import Select from '../ui/Select.svelte';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import { MEMORY_GLOBAL_SERVER_ID } from '../workspace/entries/11-web-memory-models.js';
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
		KnowledgeModelsApi
	} from '../workspace/entries/11-web-memory-models.js';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	const API_METHODS = [
		'memoryStatus',
		'recallMemory',
		'retainMemory',
		'reflectMemory',
		'exportMemory',
		'deleteMemory'
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
	type LinkName = 'openModelSettings';

	/** One linkage seam member, narrowed alone (each fails closed alone). */
	function linkFn(
		name: LinkName
	): ((...args: string[]) => void) | null {
		const links = panel.services.links as
			| Partial<{ openModelSettings(): void }>
			| null
			| undefined;
		if (typeof links !== 'object' || links === null) return null;
		const member = links[name];
		return typeof member === 'function' ? member : null;
	}

	const api = $derived(readApi(panel.services));
	/**
	 * The pinned target: the pinned server's id, or the fleet sentinel for a
	 * global window — every call names it explicitly, nothing is remembered.
	 */
	const serverId = $derived(
		panel.scope.kind === 'global' ? MEMORY_GLOBAL_SERVER_ID : panel.scope.server_id
	);
	const usable = $derived(api !== null);
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

	// --- load / refresh -------------------------------------------------------

	async function probeStatus(): Promise<void> {
		if (api === null || statusBusy) return;
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

	async function loadAll(): Promise<void> {
		if (api === null) return;
		view = { status: 'loading' };
		// The status probe ANSWERS 200 with an honest unavailable document when
		// the service is unconfigured/down — it is not an envelope failure, so
		// it must not fail the mount load (C08 rule 6: show the real state).
		try {
			const document = await api.memoryStatus(serverId);
			if (disposed) return;
			statusOutcome =
				document.status === 'ok'
					? { kind: 'ok', document }
					: { kind: 'unavailable', document };
			view = { status: 'ready' };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			if (code === 'unauthenticated' || code === 'csrf_rejected') {
				// Session-boundary failures are load errors (every section would
				// fail the same way); anything else keeps the panel usable with a
				// per-section outage.
				view = {
					status: 'error',
					code,
					message: friendlyText(code, messageOf(error) ?? 'The memory service could not be reached.')
				};
				return;
			}
			statusOutcome = {
				kind: 'problem',
				code,
				message: messageOf(error) ?? memoryCodeText(code, 'The availability probe failed.')
			};
			view = { status: 'ready' };
		}
	}

	// --- memory operations ----------------------------------------------------

	async function runRecall(): Promise<void> {
		const client = api;
		const target = serverId;
		if (client === null || recallBusy || !recallValid) return;
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
		if (client === null || retainBusy || !retainValid) return;
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
		if (client === null || reflectBusy || !reflectValid) return;
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
		if (client === null || exportBusy || exportScopeError !== null) return;
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
		if (client === null || deleteBusy || !deleteValid) return;
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

	// Mount reads once; teardown (hide/unmount/error containment) flips
	// `disposed` (ADR §7.2).
	$effect(() => {
		if (!usable) return;
		disposed = false;
		void loadAll();
		return () => {
			disposed = true;
		};
	});
	// Standalone mounts (outside PanelHost) release through onDestroy; inside
	// PanelHost both paths fire and the guard keeps disposal idempotent.
	onDestroy(() => {
		disposed = true;
	});
</script>

<div class="mai-km" data-testid="knowledge-models-root">
	{#if !usable}
		<div class="mai-km__state mai-km__state--error" role="alert" data-testid="panel-config-error">
			<p>This panel was opened without its memory service bag (panel.services.api).</p>
		</div>
	{:else if view.status === 'loading'}
		<div class="mai-km__state" role="status" data-testid="km-loading">
			<p>Loading memory status…</p>
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
