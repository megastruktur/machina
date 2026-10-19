<script module lang="ts">
	/**
	 * ModelSettingsPanel — reusable model connection/profile/selection/probe
	 * panel (card `mai-06-web-models`, P06; service row `mai-web`, contracts
	 * C01/C03/C06/C08/C11).
	 *
	 * Layer: feature panel (windowed-workspace ADR-0001 §2 layer table). It owns
	 * its one domain view and nothing else:
	 *
	 *  - Explicit context only: a GLOBAL settings view (the entry declares
	 *    `scope_types: ['global']`). Every mutation names its record explicitly
	 *    (connection/profile name or the selection pair); this module reads no
	 *    launcher/current-server value anywhere, so there is no hidden global
	 *    mutation target (ADR §3.2, NT5).
	 *  - Explicit services only: the models API enters through
	 *    `panel.services.api` (the typed `ModelSettingsApi` shape owned by the
	 *    feature entry module, satisfied by `createMaiApiClient()` at desktop
	 *    composition). A missing/incomplete bag fails closed with a visible
	 *    state — no service locator, no fallback client.
	 *  - Credential non-disclosure (C03 rule 6, brief acceptance 1): the ONLY
	 *    credential-related field on this surface is `credential_ref` — the
	 *    `.env` variable NAME. No credential value is ever entered, sent,
	 *    stored, displayed or logged here; resolution happens exclusively
	 *    inside the producer's probe round. The base URL validator rejects
	 *    inline userinfo the same way the producer does.
	 *  - Capability distinction (brief acceptance 1): a connection's declared
	 *    protocol capabilities (`tool_calls`, `reasoning`) are shown and
	 *    editable as explicit controls, and every probe outcome renders the
	 *    reviewed classified vocabulary (`ok` / `reachability` / `auth` /
	 *    `rejected_request` / `provider` / `unknown` / `credential`) — a failed
	 *    round is never displayed as a success (C08 rule 6).
	 *  - Explicit binding (brief acceptance 2): the stored selection is ONE
	 *    explicit (connection, profile) record. A probe freezes its named pair
	 *    into the request body at click time; changing the default afterwards
	 *    cannot retarget the in-flight round (the producer pins revisions the
	 *    same way, and chat submissions pin their profile at submit time —
	 *    C06 rule 1). Selection writes never touch connection/profile
	 *    documents, so credentials cannot change through a re-selection.
	 *  - Mount reads, never writes (ADR §7.2): opening/mounting issues only
	 *    the value-free list reads plus the selection read. Writes happen
	 *    exclusively on explicit actions and carry the last observed revision
	 *    (`expected_revision`, C01 rule 6; 0 = create).
	 *  - Conflict discipline: a stale `conflict` response is DISPLAYED (with
	 *    the value-free producer message); the panel never auto-retries with
	 *    a fresh revision, keeps the user's input, and requires an explicit
	 *    Reload before saving again.
	 *  - Client-side field checks mirror the reviewed producer bounds
	 *    (`mai.agents.models`: name slug, credential-reference shape, absolute
	 *    http(s) URL without query/fragment/userinfo, timeout 1–600,
	 *    temperature ≤ 2, top_p ≤ 1, integer token/limit bounds, frozen
	 *    `reasoning_effort` vocabulary). The server re-validates everything —
	 *    these checks only avoid round-trips that cannot succeed.
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
	import { Cable, Cpu, Gauge, Pin } from 'lucide-svelte';
	import Button from '../ui/Button.svelte';
	import Field from '../ui/Field.svelte';
	import Select from '../ui/Select.svelte';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import type {
		ModelConnectionBody,
		ModelProbeBody,
		ModelProbeOutcome,
		ModelProfileBody,
		ModelSettingsApi
	} from '../workspace/entries/06-web-models.js';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	const API_METHODS = [
		'listConnections',
		'readConnection',
		'putConnection',
		'deleteConnection',
		'listProfiles',
		'readProfile',
		'putProfile',
		'deleteProfile',
		'readSelection',
		'putSelection',
		'deleteSelection',
		'probe'
	] as const;

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readApi(services: PanelServices): ModelSettingsApi | null {
		const candidate = services.api as Partial<ModelSettingsApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (const name of API_METHODS) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as ModelSettingsApi;
	}

	const api = $derived(readApi(panel.services));
	/** Global settings view: a server-scoped mount contradicts the entry contract. */
	const globalScope = $derived(panel.scope.kind === 'global');
	const usable = $derived(api !== null && globalScope);

	// --- producer-bound input checks (server re-validates) -------------------

	const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
	const CREDENTIAL_REF_RE = /^[A-Z][A-Z0-9_]{0,127}$/;
	const MODEL_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$/;
	const REASONING_EFFORTS: readonly string[] = ['minimal', 'low', 'medium', 'high'];
	const MAX_BASE_URL_CHARS = 2048;

	/** `null` = empty (→ API null), `'invalid'` = out of bounds, else the number. */
	function parseOptionalNumber(raw: string, low: number, high: number, integer: boolean): number | 'invalid' | null {
		const value = raw.trim();
		if (value === '') return null;
		const parsed = Number(value);
		if (!Number.isFinite(parsed) || parsed < low || parsed > high) return 'invalid';
		if (integer && !Number.isInteger(parsed)) return 'invalid';
		return parsed;
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

	// --- view state (C08 rule 6: never all-green on unavailable data) --------

	type LoadState =
		| { status: 'loading' }
		| { status: 'error'; code: string; message: string }
		| { status: 'ready' };

	/** Editable connection form; `null` = no editor open. */
	interface ConnectionEditor {
		name: string;
		isNew: boolean;
		baseUrl: string;
		credentialRef: string;
		timeout: string;
		toolCalls: boolean;
		reasoning: boolean;
		/** Loaded transport headers, echoed verbatim on save (no silent drop). */
		headers: ReadonlyArray<{ name: string; value: string }>;
		/** The revision this form last observed (0 for a new record). */
		revision: number;
	}

	/** Editable profile form; `null` = no editor open. */
	interface ProfileEditor {
		name: string;
		isNew: boolean;
		connection: string;
		model: string;
		temperature: string;
		topP: string;
		seed: string;
		maxTokens: string;
		reasoningEffort: string;
		contextLimit: string;
		outputLimit: string;
		revision: number;
	}

	let view = $state<LoadState>({ status: 'loading' });
	/** Set once the mount cycle ended (hide/close/error teardown); async
	 * continuations check it before touching state (ADR §7.2 release). */
	let disposed = true;

	/** Value-free record references (identity + version triple). */
	let connections = $state<ReadonlyArray<{ name: string; revision: number; schema_revision: number; sha256: string }>>([]);
	let profiles = $state<ReadonlyArray<{ name: string; revision: number; schema_revision: number; sha256: string }>>([]);
	/** The stored (connection, profile) selection; `null` = none stored (404). */
	let selection = $state<{ connection: string; profile: string; revision: number; schema_revision: number; sha256: string } | null>(null);

	let connEditor = $state<ConnectionEditor | null>(null);
	let profEditor = $state<ProfileEditor | null>(null);

	// Per-section mutation feedback. Conflict banners block further writes
	// until an explicit Reload; problems keep the form open (session ended,
	// unavailable, producer 422 …); saved notices confirm the post-commit
	// revision (C01 rule 6).
	let connSaving = $state(false);
	let connConflict = $state<string | null>(null);
	let connProblem = $state<{ code: string; message: string } | null>(null);
	let connSaved = $state<string | null>(null);

	let profSaving = $state(false);
	let profConflict = $state<string | null>(null);
	let profProblem = $state<{ code: string; message: string } | null>(null);
	let profSaved = $state<string | null>(null);

	/** The selection edit form; prefilled from the stored pair on load. */
	let selectionForm = $state<{ connection: string; profile: string }>({ connection: '', profile: '' });
	let selSaving = $state(false);
	let selConflict = $state<string | null>(null);
	let selProblem = $state<{ code: string; message: string } | null>(null);
	let selSaved = $state<string | null>(null);

	/** Probe form: empty names ⇒ the stored selection picks the pair. */
	let probeForm = $state<{ connection: string; profile: string; tools: boolean }>({
		connection: '',
		profile: '',
		tools: false
	});
	let probeRunning = $state(false);
	let probeOutcome = $state<ModelProbeOutcome | null>(null);
	let probeProblem = $state<{ code: string; message: string } | null>(null);

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
		return fallback;
	}

	/**
	 * Shared mutation-failure routing: a stale-revision (or selection-named
	 * record) 409 DISPLAYS its value-free producer message and blocks until an
	 * explicit Reload; everything else becomes a keep-form-open problem.
	 */
	function handleMutationError(
		error: unknown,
		setConflict: (message: string) => void,
		setProblem: (problem: { code: string; message: string }) => void
	): void {
		const code = codeOf(error);
		if (code === 'conflict') {
			setConflict(
				messageOf(error) ??
					'The record changed since this window last loaded. Reload to review the stored record before editing — your input is kept and nothing was written.'
			);
			return;
		}
		const resolved = code ?? 'unavailable';
		setProblem({
			code: resolved,
			message: messageOf(error) ?? friendlyText(resolved, 'The change could not be saved.')
		});
	}

	// --- derived validation + wire bodies ------------------------------------

	const connErrors = $derived.by<Record<string, string>>(() => {
		const editor = connEditor;
		if (editor === null) return {};
		const errors: Record<string, string> = {};
		if (!NAME_RE.test(editor.name)) {
			errors.name = 'Record name must match [a-z0-9][a-z0-9_-]{0,63}.';
		}
		const urlError = validateBaseUrl(editor.baseUrl);
		if (urlError !== null) errors.baseUrl = urlError;
		const ref = editor.credentialRef.trim();
		if (ref !== '' && !CREDENTIAL_REF_RE.test(ref)) {
			errors.credentialRef = 'Credential reference must be the .env name ([A-Z][A-Z0-9_]{0,127}) — never a value.';
		}
		if (parseOptionalNumber(editor.timeout, 1, 600, false) === 'invalid') {
			errors.timeout = 'Timeout must be a number between 1 and 600 seconds, or empty.';
		}
		return errors;
	});

	const connValid = $derived(connEditor !== null && Object.keys(connErrors).length === 0);

	const profErrors = $derived.by<Record<string, string>>(() => {
		const editor = profEditor;
		if (editor === null) return {};
		const errors: Record<string, string> = {};
		if (!NAME_RE.test(editor.name)) {
			errors.name = 'Record name must match [a-z0-9][a-z0-9_-]{0,63}.';
		}
		if (!NAME_RE.test(editor.connection)) {
			errors.connection = 'Connection must match [a-z0-9][a-z0-9_-]{0,63}.';
		}
		if (!MODEL_ID_RE.test(editor.model.trim())) {
			errors.model = 'Model must be an identifier matching [A-Za-z0-9][A-Za-z0-9._/-]{0,127}.';
		}
		if (parseOptionalNumber(editor.temperature, 0, 2, false) === 'invalid') {
			errors.temperature = 'Temperature must be a number between 0 and 2, or empty.';
		}
		if (parseOptionalNumber(editor.topP, 0, 1, false) === 'invalid') {
			errors.topP = 'Top P must be a number between 0 and 1, or empty.';
		}
		if (parseOptionalNumber(editor.seed, 0, 2147483647, true) === 'invalid') {
			errors.seed = 'Seed must be an integer between 0 and 2147483647, or empty.';
		}
		if (parseOptionalNumber(editor.maxTokens, 1, 1000000, true) === 'invalid') {
			errors.maxTokens = 'Max tokens must be an integer between 1 and 1000000, or empty.';
		}
		if (parseOptionalNumber(editor.contextLimit, 1, 2147483647, true) === 'invalid') {
			errors.contextLimit = 'Context limit must be an integer between 1 and 2147483647, or empty.';
		}
		if (parseOptionalNumber(editor.outputLimit, 1, 2147483647, true) === 'invalid') {
			errors.outputLimit = 'Output limit must be an integer between 1 and 2147483647, or empty.';
		}
		if (editor.reasoningEffort !== '' && !REASONING_EFFORTS.includes(editor.reasoningEffort)) {
			errors.reasoningEffort = 'Reasoning effort must be one of minimal, low, medium, high, or empty.';
		}
		return errors;
	});

	const profValid = $derived(profEditor !== null && Object.keys(profErrors).length === 0);

	const selectionValid = $derived(
		NAME_RE.test(selectionForm.connection) && NAME_RE.test(selectionForm.profile)
	);

	/** Profile→connection options: stored names plus the loaded value (a stored
	 * profile may reference a connection by name even while it is absent). */
	const profConnectionOptions = $derived.by<string[]>(() => {
		const editor = profEditor;
		if (editor === null) return [];
		const names = connections.map((ref) => ref.name);
		if (!names.includes(editor.connection)) names.unshift(editor.connection);
		return names;
	});

	function connectionBody(editor: ConnectionEditor): ModelConnectionBody {
		const ref = editor.credentialRef.trim();
		const timeout = parseOptionalNumber(editor.timeout, 1, 600, false);
		const headers: Record<string, string> | null =
			editor.headers.length > 0
				? Object.fromEntries(editor.headers.map((header) => [header.name, header.value]))
				: null;
		return {
			base_url: editor.baseUrl.trim(),
			credential_ref: ref === '' ? null : ref,
			timeout_seconds: timeout === 'invalid' ? null : timeout,
			headers,
			capabilities: { tool_calls: editor.toolCalls, reasoning: editor.reasoning },
			expected_revision: editor.revision
		};
	}

	function profileBody(editor: ProfileEditor): ModelProfileBody {
		const optional = (raw: string, low: number, high: number, integer: boolean): number | null => {
			const parsed = parseOptionalNumber(raw, low, high, integer);
			return parsed === 'invalid' ? null : parsed;
		};
		return {
			connection: editor.connection,
			model: editor.model.trim(),
			temperature: optional(editor.temperature, 0, 2, false),
			top_p: optional(editor.topP, 0, 1, false),
			seed: optional(editor.seed, 0, 2147483647, true),
			max_tokens: optional(editor.maxTokens, 1, 1000000, true),
			reasoning_effort: editor.reasoningEffort === '' ? null : editor.reasoningEffort,
			context_limit: optional(editor.contextLimit, 1, 2147483647, true),
			output_limit: optional(editor.outputLimit, 1, 2147483647, true),
			expected_revision: editor.revision
		};
	}

	// --- load / refresh ------------------------------------------------------

	async function loadAll(): Promise<void> {
		if (api === null) return;
		view = { status: 'loading' };
		try {
			const [connList, profList] = await Promise.all([api.listConnections(), api.listProfiles()]);
			let storedSelection: typeof selection = null;
			let selectionError: unknown = null;
			try {
				storedSelection = await api.readSelection();
			} catch (error) {
				if (codeOf(error) !== 'not_found') selectionError = error;
			}
			if (selectionError !== null) throw selectionError;
			if (disposed) return;
			connections = connList.connections;
			profiles = profList.profiles;
			selection = storedSelection;
			selectionForm = storedSelection
				? { connection: storedSelection.connection, profile: storedSelection.profile }
				: { connection: '', profile: '' };
			view = { status: 'ready' };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			view = {
				status: 'error',
				code,
				message: friendlyText(
					code,
					messageOf(error) ?? 'The model configuration could not be loaded.'
				)
			};
		}
	}

	async function refreshConnections(): Promise<void> {
		if (api === null || view.status !== 'ready') return;
		try {
			const list = await api.listConnections();
			if (disposed) return;
			connections = list.connections;
		} catch {
			// The loaded editor keeps the post-commit state (C02 rule 4 view);
			// the listing catches up on the next explicit reload.
		}
	}

	async function refreshProfiles(): Promise<void> {
		if (api === null || view.status !== 'ready') return;
		try {
			const list = await api.listProfiles();
			if (disposed) return;
			profiles = list.profiles;
		} catch {
			// Same policy as refreshConnections.
		}
	}

	// --- connection editor ---------------------------------------------------

	function newConnection(): void {
		connConflict = null;
		connProblem = null;
		connSaved = null;
		connEditor = {
			name: '',
			isNew: true,
			baseUrl: '',
			credentialRef: '',
			timeout: '',
			toolCalls: false,
			reasoning: false,
			headers: [],
			revision: 0
		};
	}

	async function openConnection(name: string): Promise<void> {
		if (api === null) return;
		connConflict = null;
		connProblem = null;
		connSaved = null;
		try {
			const stored = await api.readConnection(name);
			if (disposed) return;
			connEditor = {
				name: stored.name,
				isNew: false,
				baseUrl: stored.base_url,
				credentialRef: stored.credential_ref ?? '',
				timeout: stored.timeout_seconds === null ? '' : String(stored.timeout_seconds),
				toolCalls: stored.capabilities.tool_calls,
				reasoning: stored.capabilities.reasoning,
				headers: stored.headers.map((header) => ({ name: header.name, value: header.value })),
				revision: stored.revision
			};
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			connProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? `Connection "${name}" could not be loaded.`)
			};
		}
	}

	function closeConnectionEditor(): void {
		connEditor = null;
		connConflict = null;
		connProblem = null;
		connSaved = null;
	}

	async function saveConnection(): Promise<void> {
		const editor = connEditor;
		if (editor === null || api === null || connSaving || !connValid || connConflict !== null) return;
		connSaving = true;
		connProblem = null;
		connSaved = null;
		try {
			const result = await api.putConnection(editor.name, connectionBody(editor));
			if (disposed) return;
			connEditor = { ...editor, isNew: false, revision: result.revision };
			connSaved = `Saved connection "${editor.name}" — revision ${result.revision}.`;
			void refreshConnections();
		} catch (error) {
			if (disposed) return;
			handleMutationError(error, (message) => (connConflict = message), (problem) => (connProblem = problem));
		} finally {
			if (!disposed) connSaving = false;
		}
	}

	async function deleteConnectionRecord(): Promise<void> {
		const editor = connEditor;
		if (editor === null || editor.isNew || api === null || connSaving || connConflict !== null) return;
		connSaving = true;
		connProblem = null;
		connSaved = null;
		try {
			await api.deleteConnection(editor.name, editor.revision);
			if (disposed) return;
			connEditor = null;
			connSaved = `Deleted connection "${editor.name}".`;
			void refreshConnections();
		} catch (error) {
			if (disposed) return;
			handleMutationError(error, (message) => (connConflict = message), (problem) => (connProblem = problem));
		} finally {
			if (!disposed) connSaving = false;
		}
	}

	/** The only path back to editing after a stale-edit conflict. */
	function reloadConnection(): void {
		const editor = connEditor;
		if (editor === null) return;
		connConflict = null;
		connProblem = null;
		void openConnection(editor.name);
	}

	// --- profile editor ------------------------------------------------------

	function newProfile(): void {
		profConflict = null;
		profProblem = null;
		profSaved = null;
		profEditor = {
			name: '',
			isNew: true,
			connection: connections[0]?.name ?? '',
			model: '',
			temperature: '',
			topP: '',
			seed: '',
			maxTokens: '',
			reasoningEffort: '',
			contextLimit: '',
			outputLimit: '',
			revision: 0
		};
	}

	async function openProfile(name: string): Promise<void> {
		if (api === null) return;
		profConflict = null;
		profProblem = null;
		profSaved = null;
		try {
			const stored = await api.readProfile(name);
			if (disposed) return;
			profEditor = {
				name: stored.name,
				isNew: false,
				connection: stored.connection,
				model: stored.model,
				temperature: stored.temperature === null ? '' : String(stored.temperature),
				topP: stored.top_p === null ? '' : String(stored.top_p),
				seed: stored.seed === null ? '' : String(stored.seed),
				maxTokens: stored.max_tokens === null ? '' : String(stored.max_tokens),
				reasoningEffort: stored.reasoning_effort ?? '',
				contextLimit: stored.context_limit === null ? '' : String(stored.context_limit),
				outputLimit: stored.output_limit === null ? '' : String(stored.output_limit),
				revision: stored.revision
			};
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			profProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? `Profile "${name}" could not be loaded.`)
			};
		}
	}

	function closeProfileEditor(): void {
		profEditor = null;
		profConflict = null;
		profProblem = null;
		profSaved = null;
	}

	async function saveProfile(): Promise<void> {
		const editor = profEditor;
		if (editor === null || api === null || profSaving || !profValid || profConflict !== null) return;
		profSaving = true;
		profProblem = null;
		profSaved = null;
		try {
			const result = await api.putProfile(editor.name, profileBody(editor));
			if (disposed) return;
			profEditor = { ...editor, isNew: false, revision: result.revision };
			profSaved = `Saved profile "${editor.name}" — revision ${result.revision}.`;
			void refreshProfiles();
		} catch (error) {
			if (disposed) return;
			handleMutationError(error, (message) => (profConflict = message), (problem) => (profProblem = problem));
		} finally {
			if (!disposed) profSaving = false;
		}
	}

	async function deleteProfileRecord(): Promise<void> {
		const editor = profEditor;
		if (editor === null || editor.isNew || api === null || profSaving || profConflict !== null) return;
		profSaving = true;
		profProblem = null;
		profSaved = null;
		try {
			await api.deleteProfile(editor.name, editor.revision);
			if (disposed) return;
			profEditor = null;
			profSaved = `Deleted profile "${editor.name}".`;
			void refreshProfiles();
		} catch (error) {
			if (disposed) return;
			handleMutationError(error, (message) => (profConflict = message), (problem) => (profProblem = problem));
		} finally {
			if (!disposed) profSaving = false;
		}
	}

	function reloadProfile(): void {
		const editor = profEditor;
		if (editor === null) return;
		profConflict = null;
		profProblem = null;
		void openProfile(editor.name);
	}

	// --- selection -----------------------------------------------------------

	/** The observed selection revision rides along (C01 rule 6); none stored ⇒ 0. */
	async function applySelection(): Promise<void> {
		if (api === null || selSaving || !selectionValid || selConflict !== null) return;
		selSaving = true;
		selProblem = null;
		selSaved = null;
		try {
			const stored = await api.putSelection({
				connection: selectionForm.connection,
				profile: selectionForm.profile,
				expected_revision: selection?.revision ?? 0
			});
			if (disposed) return;
			selection = stored;
			selSaved = `Selection stored: ${stored.connection} / ${stored.profile} (revision ${stored.revision}).`;
		} catch (error) {
			if (disposed) return;
			handleMutationError(error, (message) => (selConflict = message), (problem) => (selProblem = problem));
		} finally {
			if (!disposed) selSaving = false;
		}
	}

	async function clearSelection(): Promise<void> {
		if (api === null || selSaving || selection === null || selConflict !== null) return;
		selSaving = true;
		selProblem = null;
		selSaved = null;
		try {
			await api.deleteSelection(selection.revision);
			if (disposed) return;
			selection = null;
			selSaved = 'Selection cleared.';
		} catch (error) {
			if (disposed) return;
			handleMutationError(error, (message) => (selConflict = message), (problem) => (selProblem = problem));
		} finally {
			if (!disposed) selSaving = false;
		}
	}

	/** Explicit re-sync: the only path that clears a selection conflict. */
	async function reloadSelection(): Promise<void> {
		if (api === null) return;
		selConflict = null;
		selProblem = null;
		try {
			const stored = await api.readSelection();
			if (disposed) return;
			selection = stored;
			selectionForm = { connection: stored.connection, profile: stored.profile };
		} catch (error) {
			if (disposed) return;
			if (codeOf(error) === 'not_found') {
				selection = null;
				return;
			}
			const code = codeOf(error) ?? 'unavailable';
			selProblem = { code, message: messageOf(error) ?? friendlyText(code, 'The selection could not be reloaded.') };
		}
	}

	// --- bounded probe -------------------------------------------------------

	/**
	 * The probe body is frozen at click time: names ride along (empty ⇒ the
	 * stored selection picks the pair producer-side). Later selection changes
	 * cannot retarget this round — the outcome reports the pinned pair and
	 * revisions (brief acceptance 2).
	 */
	async function runProbe(): Promise<void> {
		if (api === null || probeRunning) return;
		probeRunning = true;
		probeProblem = null;
		probeOutcome = null;
		const body: ModelProbeBody = {
			connection: probeForm.connection === '' ? null : probeForm.connection,
			profile: probeForm.profile === '' ? null : probeForm.profile,
			tools: probeForm.tools
		};
		try {
			const outcome = await api.probe(body);
			if (disposed) return;
			probeOutcome = outcome;
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			probeProblem = {
				code,
				message: messageOf(error) ?? friendlyText(code, 'The probe could not run.')
			};
		} finally {
			if (!disposed) probeRunning = false;
		}
	}

	// Probe outcome → display tone. Every non-ok class is honest trouble:
	// auth/credential are reference-side, reachability transport-side,
	// rejected_request/provider/unknown endpoint-side (C06 rule 1 vocabulary).
	const OUTCOME_LABELS: Record<ModelProbeOutcome['outcome'], string> = {
		ok: 'Round completed — endpoint answered.',
		reachability: 'Unreachable — transport failure or probe deadline exceeded.',
		auth: 'Auth rejected — the endpoint refused the credential (401/403).',
		rejected_request: 'Rejected request — the endpoint refused the payload (capability/payload class).',
		provider: 'Provider failure — the endpoint reported an error.',
		unknown: 'Unclassified failure — see the producer classification.',
		credential: 'Credential unresolved — the reference could not be read locally; the endpoint saw no request.'
	};

	function outcomeTone(outcome: ModelProbeOutcome['outcome']): 'success' | 'warning' | 'danger' {
		if (outcome === 'ok') return 'success';
		if (outcome === 'reachability' || outcome === 'auth' || outcome === 'credential') return 'warning';
		return 'danger';
	}

	// Mount reads once; teardown (hide/unmount/error containment) flips
	// `disposed` so in-flight responses never write afterwards (ADR §7.2).
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

<div class="mai-model-settings" data-testid="model-settings-root">
	{#if !usable}
		<div class="mai-model-settings__state mai-model-settings__state--error" role="alert" data-testid="panel-config-error">
			<p>This panel was opened without its model-settings service or outside a global scope.</p>
		</div>
	{:else if view.status === 'loading'}
		<div class="mai-model-settings__state" role="status" data-testid="models-loading">
			<p>Loading model configuration…</p>
		</div>
	{:else if view.status === 'error'}
		<div
			class="mai-model-settings__state mai-model-settings__state--error"
			role="alert"
			data-testid="models-error"
			data-error-code={view.code}
		>
			<p>{view.message}</p>
			<Button variant="secondary" onclick={loadAll} data-testid="models-retry">Retry</Button>
		</div>
	{:else}
		<!-- ===================== Connections ===================== -->
		<section class="mai-model-settings__section" aria-label="Connections" data-testid="connections-section">
			<header class="mai-model-settings__section-header">
				<span class="mai-model-settings__section-icon"><Cable size={16} aria-hidden="true" /></span>
				<h3 class="mai-model-settings__heading">Connections</h3>
			</header>
			<p class="mai-model-settings__hint">
				Provider endpoints. The credential field takes the .env variable NAME only — the key value
				is never entered, stored or displayed here.
			</p>

			<ul class="mai-model-settings__list" data-testid="connection-list">
				{#each connections as ref (ref.name)}
					<li class="mai-model-settings__list-row">
						<code data-testid="connection-ref-{ref.name}">{ref.name}</code>
						<span class="mai-model-settings__chip">revision {ref.revision}</span>
						<span class="mai-model-settings__chip">schema {ref.schema_revision}</span>
						<span class="mai-model-settings__chip mai-model-settings__chip--sha">sha256 {ref.sha256}</span>
						<Button
							variant="secondary"
							onclick={() => openConnection(ref.name)}
							disabled={connSaving}
							data-testid="connection-open-{ref.name}"
						>
							Edit
						</Button>
					</li>
				{/each}
			</ul>
			{#if connections.length === 0}
				<p class="mai-model-settings__empty" role="status" data-testid="connections-empty">
					No connections stored yet.
				</p>
			{/if}
			<Button onclick={newConnection} disabled={connSaving} data-testid="connection-new">
				New connection
			</Button>

			{#if connSaved !== null}
				<p class="mai-model-settings__notice mai-model-settings__notice--success" role="status" data-testid="connection-saved">
					{connSaved}
				</p>
			{/if}
			{#if connProblem !== null}
				<div
					class="mai-model-settings__notice mai-model-settings__notice--danger"
					role="alert"
					data-testid="connection-problem"
					data-error-code={connProblem.code}
				>
					<p>{connProblem.message}</p>
				</div>
			{/if}

			{#if connEditor !== null}
				<div class="mai-model-settings__editor" data-testid="connection-editor" aria-label={connEditor.isNew ? 'New connection' : `Connection ${connEditor.name}`}>
					{#if connEditor.isNew}
						<Field
							label="Record name"
							bind:value={connEditor.name}
							error={connErrors.name ?? ''}
							hint="Lowercase slug, the record identity (C01 rule 1)."
							autocomplete="off"
							spellcheck="false"
							data-testid="connection-name-input"
						/>
					{:else}
						<dl class="mai-model-settings__rows">
							<div class="mai-model-settings__row">
								<dt>Record</dt>
								<dd><code data-testid="connection-name">{connEditor.name}</code></dd>
							</div>
							<div class="mai-model-settings__row">
								<dt>Observed revision</dt>
								<dd data-testid="connection-revision">{connEditor.revision}</dd>
							</div>
						</dl>
					{/if}

					<Field
						label="Base URL"
						bind:value={connEditor.baseUrl}
						error={connErrors.baseUrl ?? ''}
						hint="Absolute http(s) endpoint base — no query string, fragment or inline credentials."
						autocomplete="off"
						spellcheck="false"
						data-testid="connection-base-url-input"
					/>
					<div class="mai-model-settings__form-grid">
						<Field
							label="Credential reference"
							bind:value={connEditor.credentialRef}
							error={connErrors.credentialRef ?? ''}
							hint="The .env variable NAME holding the API key (e.g. MAI_GATEWAY_KEY). Empty = unauthenticated endpoint. The value itself is never read back."
							autocomplete="off"
							spellcheck="false"
							data-testid="connection-credential-ref-input"
						/>
						<Field
							label="Timeout seconds"
							bind:value={connEditor.timeout}
							error={connErrors.timeout ?? ''}
							hint="Optional round timeout, 1–600."
							inputmode="decimal"
							autocomplete="off"
							data-testid="connection-timeout-input"
						/>
					</div>

					<fieldset class="mai-model-settings__caps">
						<legend>Declared capabilities</legend>
						<label class="mai-model-settings__cap">
							<input type="checkbox" bind:checked={connEditor.toolCalls} data-testid="connection-tool-calls" />
							Tool calls
						</label>
						<label class="mai-model-settings__cap">
							<input type="checkbox" bind:checked={connEditor.reasoning} data-testid="connection-reasoning" />
							Reasoning
						</label>
					</fieldset>
					<p class="mai-model-settings__hint">
						Undeclared capabilities gate selection and tool probes producer-side
						(422 unsupported_option) — declared here, enforced there.
					</p>
					{#if connEditor.headers.length > 0}
						<p class="mai-model-settings__hint mai-model-settings__telemetry" data-testid="connection-headers">
							Declared transport headers (kept as stored):
							{connEditor.headers.map((header) => header.name).join(', ')}
						</p>
					{/if}

					{#if connConflict !== null}
						<div class="mai-model-settings__notice mai-model-settings__notice--warning" role="alert" data-testid="connection-conflict">
							<p>{connConflict}</p>
							<Button variant="secondary" onclick={reloadConnection} data-testid="connection-reload">
								Reload
							</Button>
						</div>
					{/if}

					<div class="mai-model-settings__actions">
						<Button
							onclick={saveConnection}
							loading={connSaving}
							disabled={!connValid || connConflict !== null}
							data-testid="connection-save"
						>
							Save connection
						</Button>
						<Button
							variant="secondary"
							onclick={closeConnectionEditor}
							disabled={connSaving}
							data-testid="connection-cancel"
						>
							Close
						</Button>
						{#if !connEditor.isNew}
							<Button
								variant="danger"
								onclick={deleteConnectionRecord}
								loading={connSaving}
								disabled={connConflict !== null}
								data-testid="connection-delete"
							>
								Delete
							</Button>
						{/if}
					</div>
				</div>
			{/if}
		</section>

		<!-- ===================== Model profiles ===================== -->
		<section class="mai-model-settings__section" aria-label="Model profiles" data-testid="profiles-section">
			<header class="mai-model-settings__section-header">
				<span class="mai-model-settings__section-icon"><Cpu size={16} aria-hidden="true" /></span>
				<h3 class="mai-model-settings__heading">Model profiles</h3>
			</header>
			<p class="mai-model-settings__hint">
				Allowlisted settings only (C06 rule 1); endpoint capability gates are enforced at selection
				and probe time.
			</p>

			<ul class="mai-model-settings__list" data-testid="profile-list">
				{#each profiles as ref (ref.name)}
					<li class="mai-model-settings__list-row">
						<code data-testid="profile-ref-{ref.name}">{ref.name}</code>
						<span class="mai-model-settings__chip">revision {ref.revision}</span>
						<span class="mai-model-settings__chip">schema {ref.schema_revision}</span>
						<span class="mai-model-settings__chip mai-model-settings__chip--sha">sha256 {ref.sha256}</span>
						<Button
							variant="secondary"
							onclick={() => openProfile(ref.name)}
							disabled={profSaving}
							data-testid="profile-open-{ref.name}"
						>
							Edit
						</Button>
					</li>
				{/each}
			</ul>
			{#if profiles.length === 0}
				<p class="mai-model-settings__empty" role="status" data-testid="profiles-empty">
					No profiles stored yet.
				</p>
			{/if}
			<Button onclick={newProfile} disabled={profSaving} data-testid="profile-new">
				New profile
			</Button>

			{#if profSaved !== null}
				<p class="mai-model-settings__notice mai-model-settings__notice--success" role="status" data-testid="profile-saved">
					{profSaved}
				</p>
			{/if}
			{#if profProblem !== null}
				<div
					class="mai-model-settings__notice mai-model-settings__notice--danger"
					role="alert"
					data-testid="profile-problem"
					data-error-code={profProblem.code}
				>
					<p>{profProblem.message}</p>
				</div>
			{/if}

			{#if profEditor !== null}
				<div class="mai-model-settings__editor" data-testid="profile-editor" aria-label={profEditor.isNew ? 'New model profile' : `Model profile ${profEditor.name}`}>
					{#if profEditor.isNew}
						<Field
							label="Record name"
							bind:value={profEditor.name}
							error={profErrors.name ?? ''}
							hint="Lowercase slug, the record identity (C01 rule 1)."
							autocomplete="off"
							spellcheck="false"
							data-testid="profile-name-input"
						/>
					{:else}
						<dl class="mai-model-settings__rows">
							<div class="mai-model-settings__row">
								<dt>Record</dt>
								<dd><code data-testid="profile-name">{profEditor.name}</code></dd>
							</div>
							<div class="mai-model-settings__row">
								<dt>Observed revision</dt>
								<dd data-testid="profile-revision">{profEditor.revision}</dd>
							</div>
						</dl>
					{/if}

					<Select
						label="Connection"
						bind:value={profEditor.connection}
						error={profErrors.connection ?? ''}
						data-testid="profile-connection-select"
					>
						{#each profConnectionOptions as name (name)}
							<option value={name}>{name}</option>
						{/each}
					</Select>

					<Field
						label="Model"
						bind:value={profEditor.model}
						error={profErrors.model ?? ''}
						hint="OpenAI-style, gateway-prefixed (openai/gpt-4o-mini) or local name (qwen3-8b)."
						autocomplete="off"
						spellcheck="false"
						data-testid="profile-model-input"
					/>
					<div class="mai-model-settings__form-grid">
						<Field
							label="Temperature"
							bind:value={profEditor.temperature}
							error={profErrors.temperature ?? ''}
							hint="Optional, 0–2."
							inputmode="decimal"
							autocomplete="off"
							data-testid="profile-temperature-input"
						/>
						<Field
							label="Top P"
							bind:value={profEditor.topP}
							error={profErrors.topP ?? ''}
							hint="Optional, 0–1."
							inputmode="decimal"
							autocomplete="off"
							data-testid="profile-top-p-input"
						/>
						<Field
							label="Seed"
							bind:value={profEditor.seed}
							error={profErrors.seed ?? ''}
							hint="Optional integer, 0–2147483647."
							inputmode="numeric"
							autocomplete="off"
							data-testid="profile-seed-input"
						/>
						<Field
							label="Max tokens"
							bind:value={profEditor.maxTokens}
							error={profErrors.maxTokens ?? ''}
							hint="Optional integer, 1–1000000."
							inputmode="numeric"
							autocomplete="off"
							data-testid="profile-max-tokens-input"
						/>
					</div>

					<Select
						label="Reasoning effort"
						bind:value={profEditor.reasoningEffort}
						error={profErrors.reasoningEffort ?? ''}
						data-testid="profile-reasoning-select"
					>
						<option value="">(unset)</option>
						{#each REASONING_EFFORTS as effort (effort)}
							<option value={effort}>{effort}</option>
						{/each}
					</Select>

					<div class="mai-model-settings__form-grid">
						<Field
							label="Context limit"
							bind:value={profEditor.contextLimit}
							error={profErrors.contextLimit ?? ''}
							hint="Optional integer, 1–2147483647."
							inputmode="numeric"
							autocomplete="off"
							data-testid="profile-context-limit-input"
						/>
						<Field
							label="Output limit"
							bind:value={profEditor.outputLimit}
							error={profErrors.outputLimit ?? ''}
							hint="Optional integer, 1–2147483647."
							inputmode="numeric"
							autocomplete="off"
							data-testid="profile-output-limit-input"
						/>
					</div>

					{#if profConflict !== null}
						<div class="mai-model-settings__notice mai-model-settings__notice--warning" role="alert" data-testid="profile-conflict">
							<p>{profConflict}</p>
							<Button variant="secondary" onclick={reloadProfile} data-testid="profile-reload">
								Reload
							</Button>
						</div>
					{/if}

					<div class="mai-model-settings__actions">
						<Button
							onclick={saveProfile}
							loading={profSaving}
							disabled={!profValid || profConflict !== null}
							data-testid="profile-save"
						>
							Save profile
						</Button>
						<Button
							variant="secondary"
							onclick={closeProfileEditor}
							disabled={profSaving}
							data-testid="profile-cancel"
						>
							Close
						</Button>
						{#if !profEditor.isNew}
							<Button
								variant="danger"
								onclick={deleteProfileRecord}
								loading={profSaving}
								disabled={profConflict !== null}
								data-testid="profile-delete"
							>
								Delete
							</Button>
						{/if}
					</div>
				</div>
			{/if}
		</section>

		<!-- ===================== Selection ===================== -->
		<section class="mai-model-settings__section" aria-label="Selection" data-testid="selection-section">
			<header class="mai-model-settings__section-header">
				<span class="mai-model-settings__section-icon"><Pin size={16} aria-hidden="true" /></span>
				<h3 class="mai-model-settings__heading">Selection</h3>
			</header>
			<p class="mai-model-settings__hint">
				The default (connection, profile) pair. Changing it never retargets a running conversation:
				chat submissions pin their profile at submit time, and probes pin the pair at click time.
			</p>

			{#if selection === null}
				<p class="mai-model-settings__empty" role="status" data-testid="selection-empty">
					No pair is selected — probes without explicit names will fail until one is stored.
				</p>
			{:else}
				<dl class="mai-model-settings__rows">
					<div class="mai-model-settings__row">
						<dt>Stored pair</dt>
						<dd data-testid="selection-view">
							<code>{selection.connection}</code> / <code>{selection.profile}</code>
						</dd>
					</div>
					<div class="mai-model-settings__row">
						<dt>Observed revision</dt>
						<dd data-testid="selection-revision">{selection.revision}</dd>
					</div>
				</dl>
			{/if}

			{#if selSaved !== null}
				<p class="mai-model-settings__notice mai-model-settings__notice--success" role="status" data-testid="selection-saved">
					{selSaved}
				</p>
			{/if}
			{#if selProblem !== null}
				<div
					class="mai-model-settings__notice mai-model-settings__notice--danger"
					role="alert"
					data-testid="selection-problem"
					data-error-code={selProblem.code}
				>
					<p>{selProblem.message}</p>
				</div>
			{/if}
			{#if selConflict !== null}
				<div class="mai-model-settings__notice mai-model-settings__notice--warning" role="alert" data-testid="selection-conflict">
					<p>{selConflict}</p>
					<Button variant="secondary" onclick={reloadSelection} data-testid="selection-reload">
						Reload
					</Button>
				</div>
			{/if}

			<div class="mai-model-settings__editor" data-testid="selection-editor">
				<Select
					label="Connection"
					bind:value={selectionForm.connection}
					data-testid="selection-connection-select"
				>
					<option value="">(none)</option>
					{#each connections as ref (ref.name)}
						<option value={ref.name}>{ref.name}</option>
					{/each}
				</Select>
				<Select
					label="Profile"
					bind:value={selectionForm.profile}
					data-testid="selection-profile-select"
				>
					<option value="">(none)</option>
					{#each profiles as ref (ref.name)}
						<option value={ref.name}>{ref.name}</option>
					{/each}
				</Select>
				<div class="mai-model-settings__actions">
					<Button
						onclick={applySelection}
						loading={selSaving}
						disabled={!selectionValid || selConflict !== null}
						data-testid="selection-apply"
					>
						Store selection
					</Button>
					{#if selection !== null}
						<Button
							variant="secondary"
							onclick={clearSelection}
							loading={selSaving}
							disabled={selConflict !== null}
							data-testid="selection-clear"
						>
							Clear
						</Button>
					{/if}
				</div>
			</div>
		</section>

		<!-- ===================== Probe ===================== -->
		<section class="mai-model-settings__section" aria-label="Connection test" data-testid="probe-section">
			<header class="mai-model-settings__section-header">
				<span class="mai-model-settings__section-icon"><Gauge size={16} aria-hidden="true" /></span>
				<h3 class="mai-model-settings__heading">Test connection</h3>
			</header>
			<p class="mai-model-settings__hint">
				Runs ONE bounded classified round. Leave both names empty to test the stored selection;
				the pair and revisions are pinned when the round starts.
			</p>

			<Select
				label="Connection"
				bind:value={probeForm.connection}
				data-testid="probe-connection-select"
			>
				<option value="">(stored selection)</option>
				{#each connections as ref (ref.name)}
					<option value={ref.name}>{ref.name}</option>
				{/each}
			</Select>
			<Select
				label="Profile"
				bind:value={probeForm.profile}
				data-testid="probe-profile-select"
			>
				<option value="">(stored selection)</option>
				{#each profiles as ref (ref.name)}
					<option value={ref.name}>{ref.name}</option>
				{/each}
			</Select>
			<label class="mai-model-settings__cap">
				<input type="checkbox" bind:checked={probeForm.tools} data-testid="probe-tools" />
				Include tool round-trip (connection must declare Tool calls)
			</label>

			<div class="mai-model-settings__actions">
				<Button onclick={runProbe} loading={probeRunning} data-testid="probe-run">
					Run test
				</Button>
			</div>

			{#if probeProblem !== null}
				<div
					class="mai-model-settings__notice mai-model-settings__notice--danger"
					role="alert"
					data-testid="probe-error"
					data-error-code={probeProblem.code}
				>
					<p>{probeProblem.message}</p>
				</div>
			{/if}

			{#if probeOutcome !== null}
				<div
					class="mai-model-settings__probe mai-model-settings__probe--{outcomeTone(probeOutcome.outcome)}"
					role="status"
					data-testid="probe-outcome"
					data-outcome={probeOutcome.outcome}
				>
					<p class="mai-model-settings__probe-label" data-testid="probe-outcome-label">
						{OUTCOME_LABELS[probeOutcome.outcome]}
					</p>
					<dl class="mai-model-settings__rows">
						<div class="mai-model-settings__row">
							<dt>Pinned pair</dt>
							<dd data-testid="probe-pair">
								<code>{probeOutcome.connection}</code> / <code>{probeOutcome.profile}</code>
							</dd>
						</div>
						<div class="mai-model-settings__row">
							<dt>Model</dt>
							<dd data-testid="probe-model"><code>{probeOutcome.model}</code></dd>
						</div>
						<div class="mai-model-settings__row">
							<dt>Pinned revisions</dt>
							<dd data-testid="probe-revisions">
								connection {probeOutcome.connection_revision}, profile {probeOutcome.profile_revision}
							</dd>
						</div>
						<div class="mai-model-settings__row">
							<dt>Tool round</dt>
							<dd data-testid="probe-tools-echo">{probeOutcome.tools ? 'requested' : 'not requested'}</dd>
						</div>
						<div class="mai-model-settings__row">
							<dt>Latency</dt>
							<dd data-testid="probe-latency">{probeOutcome.latency_ms} ms</dd>
						</div>
					</dl>
					{#if probeOutcome.detail !== undefined}
						<dl class="mai-model-settings__rows" data-testid="probe-detail">
							{#each Object.entries(probeOutcome.detail) as [key, value] (key)}
								<div class="mai-model-settings__row">
									<dt>{key}</dt>
									<dd>{value}</dd>
								</div>
							{/each}
						</dl>
					{/if}
				</div>
			{/if}
		</section>
	{/if}
</div>

<style>
	.mai-model-settings {
		height: 100%;
		overflow: auto;
		color: var(--mai-color-text);
		background: var(--mai-color-bg);
		font-family: var(--mai-font-sans);
	}

	.mai-model-settings__state {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		align-items: flex-start;
		padding: var(--mai-space-4);
		color: var(--mai-status-neutral-text);
	}

	.mai-model-settings__state--error {
		color: var(--mai-status-danger-text);
	}

	.mai-model-settings__state p {
		margin: 0;
	}

	.mai-model-settings__section {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		padding: var(--mai-space-4);
		border-bottom: var(--mai-border-width) solid var(--mai-color-border);
	}

	.mai-model-settings__section-header {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		margin: calc(var(--mai-space-4) * -1) calc(var(--mai-space-4) * -1) 0;
		padding: var(--mai-space-3) var(--mai-space-4);
		background: var(--mai-color-surface-header);
		border-left: 3px solid var(--mai-color-brass);
		border-bottom: var(--mai-border-width) solid var(--mai-color-border);
	}

	.mai-model-settings__section-icon {
		display: inline-flex;
		flex: none;
		color: var(--mai-color-brass);
	}

	.mai-model-settings__heading {
		margin: 0;
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-base);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-brass);
	}

	.mai-model-settings__hint {
		margin: 0;
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text-muted);
	}

	.mai-model-settings__telemetry {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
	}

	.mai-model-settings__list {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.mai-model-settings__list-row {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: var(--mai-space-2);
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface-raised);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-lg);
	}

	.mai-model-settings__list-row code {
		margin-right: auto;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-medium);
	}

	.mai-model-settings__chip {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		padding: calc(var(--mai-space-1) / 2) var(--mai-space-1);
		white-space: nowrap;
	}

	.mai-model-settings__chip--sha {
		max-width: 18ch;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.mai-model-settings__empty {
		margin: 0;
		padding-left: var(--mai-space-3);
		border-left: 3px solid var(--mai-color-border);
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text-muted);
	}

	.mai-model-settings__rows {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		margin: 0;
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
	}

	.mai-model-settings__row {
		display: grid;
		grid-template-columns: minmax(9rem, max-content) 1fr;
		gap: var(--mai-space-3);
	}

	.mai-model-settings__row dt {
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-text-muted);
	}

	.mai-model-settings__row dd {
		margin: 0;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-sm);
		overflow-wrap: anywhere;
	}

	.mai-model-settings__row code {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
	}

	.mai-model-settings__editor {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		padding: var(--mai-space-4);
		background: var(--mai-color-surface-raised);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-lg);
	}

	.mai-model-settings__form-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
		gap: var(--mai-space-3);
	}

	.mai-model-settings__caps {
		display: flex;
		gap: var(--mai-space-4);
		margin: 0;
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
	}

	.mai-model-settings__caps legend {
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-brass-dim);
		padding: 0 var(--mai-space-1);
	}

	.mai-model-settings__cap {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2);
		font-size: var(--mai-text-sm);
	}

	.mai-model-settings__field-error {
		margin: 0;
		font-size: var(--mai-text-sm);
		color: var(--mai-status-danger-text);
	}

	.mai-model-settings__notice {
		margin: 0;
		padding: var(--mai-space-2) var(--mai-space-3);
		font-size: var(--mai-text-sm);
		border-radius: var(--mai-radius-sm);
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		align-items: flex-start;
	}

	.mai-model-settings__notice p {
		margin: 0;
	}

	.mai-model-settings__notice--success {
		color: var(--mai-status-success-text);
		background: var(--mai-status-success-bg);
		border: var(--mai-border-width) solid var(--mai-status-success-border);
		border-left-width: 3px;
	}

	.mai-model-settings__notice--warning {
		color: var(--mai-status-warning-text);
		background: var(--mai-status-warning-bg);
		border: var(--mai-border-width) solid var(--mai-status-warning-border);
		border-left-width: 3px;
	}

	.mai-model-settings__notice--danger {
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
		border: var(--mai-border-width) solid var(--mai-status-danger-border);
		border-left-width: 3px;
	}

	.mai-model-settings__actions {
		display: flex;
		gap: var(--mai-space-3);
		flex-wrap: wrap;
	}

	.mai-model-settings__probe {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		padding: var(--mai-space-4);
		border: var(--mai-border-width) solid var(--mai-status-neutral-border);
		border-left-width: 3px;
		border-radius: var(--mai-radius-lg);
	}

	.mai-model-settings__probe--success {
		color: var(--mai-status-success-text);
		background: var(--mai-status-success-bg);
		border-color: var(--mai-status-success-border);
	}

	.mai-model-settings__probe--warning {
		color: var(--mai-status-warning-text);
		background: var(--mai-status-warning-bg);
		border-color: var(--mai-status-warning-border);
	}

	.mai-model-settings__probe--danger {
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
		border-color: var(--mai-status-danger-border);
	}

	.mai-model-settings__probe-label {
		margin: 0;
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-medium);
	}
</style>
