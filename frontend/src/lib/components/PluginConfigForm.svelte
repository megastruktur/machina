<script module lang="ts">
	/**
	 * PluginConfigForm — reusable plugin configuration panel (card
	 * `mai-09-web-plugin-forms`, P09; service row `mai-web`, contracts
	 * C01/C03/C08/C11).
	 *
	 * Layer: feature panel (windowed-workspace ADR-0001 §2 layer table). It
	 * owns its one domain view and nothing else:
	 *
	 *  - Explicit context only: a GLOBAL controller-host view (the entry
	 *    declares `scope_types: ['global']`). Every operation names its
	 *    target explicitly — the plugin on every call and, for grant
	 *    bindings, the canonical server UUID typed into the binding editor.
	 *    This module reads no launcher/current-server value anywhere, so
	 *    there is no hidden global mutation target (ADR §3.2, NT5).
	 *  - Explicit services only: the plugins API enters through
	 *    `panel.services.plugins` and the skills read seam through
	 *    `panel.services.skills` (the typed shapes owned by the feature
	 *    entry, satisfied by `createMaiApiClient()` at desktop composition).
	 *    A missing/incomplete bag fails closed with a visible state — no
	 *    service locator, no fallback client.
	 *  - Schema-generated forms (brief acceptance 1): the Configure tab
	 *    renders the plugin's DECLARED `config_schema` (the reviewed
	 *    `09-plugin-api` schema endpoint) as native controls for the
	 *    supported keyword subset — string/number/integer/boolean, `enum`
	 *    selects, bounded arrays, `required`, length/pattern/bounds checks
	 *    (the exact subset `mai.plugins.config` validates). Properties
	 *    outside the subset render as explicitly-labeled raw-JSON editors;
	 *    nothing is silently dropped. `config_schema: null` is fail-closed:
	 *    the producer rejects any non-empty config, so the form offers the
	 *    empty document (or the frozen MCP registration template) and says
	 *    so. A `load_rejection` locks the form — a rejected plugin's schema
	 *    is unknown, never invented.
	 *  - Credential references only (C03 rule 6): the ONLY secret-related
	 *    surface is `credential_refs` — `.env` variable NAMES (`[A-Z]
	 *    [A-Z0-9_]{0,127}`, ≤ 16, unique). No credential value is entered,
	 *    sent, displayed or logged anywhere on this panel; there is no
	 *    password-type input because there is no value. Config keys carrying
	 *    a credential-token substring are refused client-side with the key
	 *    path named, mirroring the producer's `inline_credential_forbidden`.
	 *  - Install/configure/enable stay distinct (brief acceptance 2): the
	 *    catalogue renders `installed` (distribution metadata), `configured`
	 *    (a stored document with its revision triple) and `enabled` (the
	 *    document flag) as separate facts. Enable/disable is an explicit
	 *    full-document edit at the observed revision; grant bindings are a
	 *    separate explicit write and installing/configuring/enabling never
	 *    grants (C03 rule 7). Bounded MCP discovery is a diagnostic link —
	 *    the worker's restart composition stays the execution-path owner.
	 *  - Versioned writes (C01 rule 6): every mutation carries the last
	 *    observed `expected_revision` (0 creates). A stale `409 conflict` is
	 *    DISPLAYED with the producer's value-free message; the panel never
	 *    auto-retries with a fresh revision, keeps the user's input, and
	 *    requires an explicit Reload. 422 producer rejections are surfaced
	 *    verbatim (code + message), never paraphrased into a success.
	 *  - Client-side field checks mirror the reviewed producer bounds
	 *    (`mai.plugins.config` keyword subset + `PLUGIN_NAME_RE`-shaped
	 *    plugin slugs from the wire, `_CREDENTIAL_REF_RE`, `MAX_CREDENTIAL_REFS`;
	 *    MCP registration mirrors the frozen `SERVER_CONFIG_SCHEMA` bounds of
	 *    `mai.plugins.mcp`: transports `stdio`/`streamable_http`,
	 *    command ≤ 512, url ≤ 2048, args ≤ 32 × 512 chars, cwd ≤ 1024,
	 *    timeouts > 0 and ≤ 300, max_tools 1–1024). The server re-validates
	 *    everything — these checks only avoid round-trips that cannot
	 *    succeed.
	 *  - Skills inspection/binding report (09-skills): the catalog renders
	 *    loaded summaries AND the value-free per-package rejections; one
	 *    package renders its required capabilities and the
	 *    declared-minus-granted report against the union of stored bindings.
	 *    The panel states that the worker composes the effective surface —
	 *    this view never guesses it.
	 *  - No window mechanics: no drag/geometry/focus/persistence code and no
	 *    workspace-state imports; the window host supplies all of that.
	 *  - No module-level state and no `window`/`document` access (ADR §5
	 *    rule 4): safe to evaluate per SSR request; instances share nothing.
	 *
	 * Tokens come only from ui/tokens.css; no dependency is added (ADR §4.3).
	 */
</script>

<script lang="ts">
	import Button from '../ui/Button.svelte';
	import Field from '../ui/Field.svelte';
	import Select from '../ui/Select.svelte';
	import { Cog, KeyRound, Package, Puzzle, Radar, Wrench } from 'lucide-svelte';
	import { ApiError } from '../api/client.js';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import type {
		BindingView,
		McpDiscoveryReportView,
		PluginCatalogView,
		PluginConfigView,
		PluginSchemaView,
		PluginsApi,
		SkillCatalogView,
		SkillPackageView,
		SkillsApi,
		PluginFormsServices
	} from '../workspace/entries/09-web-plugin-forms.js';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	const PLUGIN_METHODS = [
		'catalog',
		'readConfig',
		'editConfig',
		'removeConfig',
		'readSchema',
		'listBindings',
		'editBinding',
		'removeBinding',
		'runDiscovery'
	] as const;
	const SKILL_METHODS = ['catalog', 'readSkill'] as const;

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readBag<T extends object>(services: PanelServices, key: string, methods: readonly string[]): T | null {
		const candidate = (services as Record<string, unknown>)[key] as Partial<T> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (const name of methods) {
			if (typeof (candidate as Record<string, unknown>)[name] !== 'function') return null;
		}
		return candidate as T;
	}

	const plugins = $derived(readBag<PluginsApi>(panel.services, 'plugins', PLUGIN_METHODS));
	const skills = $derived(readBag<SkillsApi>(panel.services, 'skills', SKILL_METHODS));
	/** Global controller view: a server-scoped mount contradicts the entry contract. */
	const globalScope = $derived(panel.scope.kind === 'global');
	const usable = $derived(plugins !== null && globalScope);

	// --- producer-bound constants (server re-validates everything) ----------

	/** `mai.plugins.config` `_CREDENTIAL_REF_RE` + `MAX_CREDENTIAL_REFS`. */
	const CREDENTIAL_REF_RE = /^[A-Z][A-Z0-9_]{0,127}$/;
	const MAX_CREDENTIAL_REFS = 16;
	/** `mai.plugins.config` `_CREDENTIAL_TOKENS`: inline credential keys are refused. */
	const CREDENTIAL_TOKENS: readonly string[] = [
		'password',
		'passwd',
		'secret',
		'token',
		'private_key',
		'passphrase',
		'api_key',
		'access_key'
	];
	/** `mai.plugins.config` `PLUGIN_NAME_RE` (route path parameter shape). */
	const PLUGIN_NAME_RE = /^[a-z0-9][a-z0-9._-]{0,127}$/;
	/** Canonical UUID (C01 rule 1): binding targets are typed explicitly. */
	const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
	/** `mai.plugins.mcp` frozen registration vocabulary + bounds. */
	const MCP_TRANSPORTS: readonly string[] = ['stdio', 'streamable_http'];
	const MCP_MAX_TIMEOUT_S = 300.0;
	const MCP_DEFAULT_CONNECT_TIMEOUT_S = 10.0;
	const MCP_DEFAULT_CALL_TIMEOUT_S = 120.0;
	const MCP_MAX_MAX_TOOLS = 1024;
	const MCP_DEFAULT_MAX_TOOLS = 64;
	const MCP_MAX_COMMAND_CHARS = 512;
	const MCP_MAX_URL_CHARS = 2048;
	const MCP_MAX_ARGS = 32;
	const MCP_MAX_ARG_CHARS = 512;
	const MCP_MAX_CWD_CHARS = 1024;
	/** The supported fail-closed keyword subset (`mai.plugins.config`). */
	const SUPPORTED_KEYWORDS: readonly string[] = [
		'type',
		'enum',
		'const',
		'required',
		'properties',
		'additionalProperties',
		'items',
		'minItems',
		'maxItems',
		'minLength',
		'maxLength',
		'pattern',
		'minimum',
		'maximum',
		'exclusiveMinimum',
		'exclusiveMaximum'
	];
	const ANNOTATION_KEYWORDS: readonly string[] = [
		'title',
		'description',
		'default',
		'examples',
		'format',
		'$schema',
		'$id'
	];
	const JSON_TYPES: readonly string[] = [
		'object',
		'array',
		'string',
		'number',
		'integer',
		'boolean',
		'null'
	];

	// --- shared view state (all instance-local; no module state) -------------

	type TabId = 'catalog' | 'configure' | 'bindings' | 'discovery' | 'skills';
	type Fail = { status: number; code: string; message: string };

	let tab = $state<TabId>('catalog');
	let catalogState = $state<{
		view: PluginCatalogView | null;
		phase: 'idle' | 'loading' | 'ready' | 'error';
		error: Fail | null;
	}>({ view: null, phase: 'idle', error: null });
	/** Row-scoped in-flight flags so one spinner never blanks the whole list. */
	let rowBusy = $state<Record<string, boolean>>({});
	let catalogNotice = $state<{ kind: 'ok' | 'error'; text: string } | null>(null);

	let selected = $state<string | null>(null);
	let schemaState = $state<{
		view: PluginSchemaView | null;
		phase: 'idle' | 'loading' | 'ready' | 'error';
		error: Fail | null;
	}>({ view: null, phase: 'idle', error: null });
	let configState = $state<{
		view: PluginConfigView | null;
		phase: 'idle' | 'loading' | 'ready' | 'error';
		error: Fail | null;
	}>({ view: null, phase: 'idle', error: null });

	// The generated form's draft: raw control values per declared property.
	let draft = $state<Record<string, string | boolean>>({});
	let draftEnabled = $state(false);
	let draftRefs = $state<string[]>([]);
	let fieldErrors = $state<Record<string, string>>({});
	let formError = $state<string | null>(null);
	let formNotice = $state<string | null>(null);
	let conflict = $state(false);
	let saving = $state(false);
	let mcpRegisterOpen = $state(false);
	/** The configure-new form's typed plugin name (opens the schema form). */
	let newPluginName = $state('');

	let bindingsState = $state<{
		view: BindingView[] | null;
		phase: 'idle' | 'loading' | 'ready' | 'error';
		error: Fail | null;
	}>({ view: null, phase: 'idle', error: null });
	let bindingEditor = $state<{
		open: boolean;
		serverId: string;
		capabilities: string;
		expectedRevision: number;
	}>({ open: false, serverId: '', capabilities: '', expectedRevision: 0 });
	let bindingError = $state<string | null>(null);
	let bindingNotice = $state<string | null>(null);
	let bindingBusy = $state(false);

	let discoveryState = $state<{
		view: McpDiscoveryReportView | null;
		phase: 'idle' | 'loading' | 'ready' | 'error';
		error: Fail | null;
	}>({ view: null, phase: 'idle', error: null });

	let skillsState = $state<{
		view: SkillCatalogView | null;
		phase: 'idle' | 'loading' | 'ready' | 'error';
		error: Fail | null;
	}>({ view: null, phase: 'idle', error: null });
	let skillPackageState = $state<{
		view: SkillPackageView | null;
		phase: 'idle' | 'loading' | 'ready' | 'error';
		error: Fail | null;
	}>({ view: null, phase: 'idle', error: null });

	function failOf(error: unknown): Fail {
		if (error instanceof ApiError) {
			return { status: error.status, code: error.code, message: error.message };
		}
		return {
			status: 0,
			code: 'unexpected',
			message: error instanceof Error ? error.message : String(error)
		};
	}

	// --- catalogue -----------------------------------------------------------

	async function loadCatalog(): Promise<void> {
		if (plugins === null) return;
		catalogState = { view: null, phase: 'loading', error: null };
		try {
			const view = await plugins.catalog();
			catalogState = { view, phase: 'ready', error: null };
		} catch (error) {
			catalogState = { view: null, phase: 'error', error: failOf(error) };
		}
	}

	// Mount reads, never writes: the catalog is the panel's one mount read.
	$effect(() => {
		if (usable && catalogState.phase === 'idle') void loadCatalog();
	});

	function configuredOf(name: string): ConfiguredRow | null {
		const view = catalogState.view;
		if (view === null) return null;
		return view.configured.find((entry) => entry.plugin === name) ?? null;
	}

	type ConfiguredRow = PluginCatalogView['configured'][number];

	/** Enable/disable: an explicit full-document edit at the observed revision. */
	async function toggleEnabled(row: ConfiguredRow): Promise<void> {
		if (plugins === null) return;
		rowBusy = { ...rowBusy, [`enable:${row.plugin}`]: true };
		catalogNotice = null;
		try {
			await plugins.editConfig(row.plugin, {
				expected_revision: row.revision,
				enabled: !row.enabled,
				credential_refs: [...row.credential_refs],
				config: { ...row.config }
			});
			await loadCatalog();
			catalogNotice = {
				kind: 'ok',
				text: row.enabled ? `${row.plugin} disabled` : `${row.plugin} enabled`
			};
			// A disabled MCP server cannot be discovered; the stale report goes.
			if (row.enabled) discoveryState = { view: null, phase: 'idle', error: null };
		} catch (error) {
			const fail = failOf(error);
			catalogNotice = { kind: 'error', text: `${fail.status} ${fail.code}: ${fail.message}` };
		} finally {
			rowBusy = { ...rowBusy, [`enable:${row.plugin}`]: false };
		}
	}

	/**
	 * Destructive actions confirm in-panel (no `window.confirm`): arming
	 * names the exact target, the second explicit click executes. Reset by
	 * Cancel, catalog refresh or selection change.
	 */
	let deleteArmed = $state<string | null>(null);
	let revokeArmed = $state<string | null>(null);

	function armDelete(row: ConfiguredRow): void {
		catalogNotice = null;
		deleteArmed = row.plugin;
	}

	async function confirmDelete(row: ConfiguredRow): Promise<void> {
		if (plugins === null) return;
		rowBusy = { ...rowBusy, [`delete:${row.plugin}`]: true };
		catalogNotice = null;
		try {
			await plugins.removeConfig(row.plugin, row.revision);
			deleteArmed = null;
			await loadCatalog();
			catalogNotice = { kind: 'ok', text: `${row.plugin} configuration deleted` };
			if (selected === row.plugin) closeConfigure();
		} catch (error) {
			const fail = failOf(error);
			// Bindings referencing the plugin refuse the delete (409) — surfaced,
			// never silently retried.
			catalogNotice = { kind: 'error', text: `${fail.status} ${fail.code}: ${fail.message}` };
		} finally {
			rowBusy = { ...rowBusy, [`delete:${row.plugin}`]: false };
		}
	}

	// --- configure (the schema-generated form) -------------------------------

	const TABS: ReadonlyArray<{ id: TabId; label: string }> = [
		{ id: 'catalog', label: 'Catalog' },
		{ id: 'configure', label: 'Configure' },
		{ id: 'bindings', label: 'Bindings' },
		{ id: 'discovery', label: 'MCP discovery' },
		{ id: 'skills', label: 'Skills' }
	];

	function closeConfigure(): void {
		selected = null;
		schemaState = { view: null, phase: 'idle', error: null };
		configState = { view: null, phase: 'idle', error: null };
		resetDraft();
	}

	function resetDraft(): void {
		draft = {};
		draftEnabled = false;
		draftRefs = [];
		fieldErrors = {};
		formError = null;
		formNotice = null;
		conflict = false;
		saving = false;
		mcpRegisterOpen = false;
	}

	async function openConfigure(name: string): Promise<void> {
		if (plugins === null) return;
		selected = name;
		tab = 'configure';
		deleteArmed = null;
		revokeArmed = null;
		resetDraft();
		schemaState = { view: null, phase: 'loading', error: null };
		configState = { view: null, phase: 'loading', error: null };
		const stored = configuredOf(name);
		const configPromise =
			stored === null
				? Promise.resolve(null)
				: plugins.readConfig(name).catch((error: unknown) => {
						// An unconfigured row raced a configure open: treat 404 as
						// "creates", surface anything else.
						const fail = failOf(error);
						if (fail.status === 404) return null;
						throw error;
					});
		let schema: PluginSchemaView;
		try {
			schema = await plugins.readSchema(name);
			schemaState = { view: schema, phase: 'ready', error: null };
		} catch (error) {
			const fail = failOf(error);
			if (fail.status === 404) {
				// The producer declares nothing for a name it neither stores nor
				// ships (404): the registration path stays open — its write path
				// still validates an MCP transport document against the frozen
				// schema. Nothing is invented: the form shows exactly "declares
				// nothing".
				schema = { plugin: name, kind: 'uninstalled', config_schema: null, load_rejection: null };
				schemaState = { view: schema, phase: 'ready', error: null };
			} else {
				schemaState = { view: null, phase: 'error', error: fail };
				return;
			}
		}
		try {
			const config = await configPromise;
			if (config === null) {
				configState = { view: null, phase: 'ready', error: null };
			} else {
				configState = { view: config, phase: 'ready', error: null };
				draftFromConfig(schema, config);
			}
		} catch (error) {
			configState = { view: null, phase: 'error', error: failOf(error) };
		}
	}

	/** Seed the draft from the stored document (documented defaults win first). */
	function draftFromConfig(schema: PluginSchemaView, config: PluginConfigView): void {
		const values: Record<string, string | boolean> = {};
		const properties = objectSchemaProperties(schema.config_schema);
		for (const [key, property] of properties) {
			const present = config.config[key];
			if (present !== undefined) {
				values[key] = controlValue(property, present);
			} else if (property !== null && 'default' in property) {
				values[key] = controlValue(property, property.default);
			}
		}
		// Keys the declared schema does not declare (hostile/legacy stores):
		// shown in the raw-JSON editor so a full-document replace cannot
		// silently drop them.
		const declared = new Set(properties.keys());
		const extra: Record<string, unknown> = {};
		for (const [key, value] of Object.entries(config.config)) {
			if (!declared.has(key)) extra[key] = value;
		}
		if (Object.keys(extra).length > 0) values['__extra_json'] = JSON.stringify(extra, null, 2);
		draft = values;
		draftEnabled = config.enabled;
		draftRefs = [...config.credential_refs];
	}

	function controlValue(property: Record<string, unknown> | null, value: unknown): string | boolean {
		if (property !== null && property['type'] === 'boolean') return value === true;
		if (typeof value === 'boolean') return value;
		if (property !== null && property['type'] === 'array') {
			return Array.isArray(value) ? value.map((item) => String(item)).join('\n') : '';
		}
		if (property !== null && isJsonFallback(property)) {
			return JSON.stringify(value, null, 2);
		}
		return typeof value === 'string' ? value : JSON.stringify(value);
	}

	// --- declared-schema accessors -------------------------------------------

	function isRecord(value: unknown): value is Record<string, unknown> {
		return typeof value === 'object' && value !== null && !Array.isArray(value);
	}

	function objectSchemaProperties(
		schema: Record<string, unknown> | null
	): Map<string, Record<string, unknown> | null> {
		const result = new Map<string, Record<string, unknown> | null>();
		if (schema === null) return result;
		const properties = schema['properties'];
		if (!isRecord(properties)) return result;
		for (const [key, value] of Object.entries(properties)) {
			result.set(key, isRecord(value) ? value : null);
		}
		return result;
	}

	function requiredNames(schema: Record<string, unknown> | null): ReadonlySet<string> {
		if (schema === null) return new Set();
		const raw = schema['required'];
		if (!Array.isArray(raw)) return new Set();
		return new Set(raw.filter((name): name is string => typeof name === 'string'));
	}

	/**
	 * True when a property must render as a raw-JSON editor: object or
	 * undeclared/multi types, arrays of non-scalars, or a keyword outside
	 * the supported subset anywhere in its subtree. The producer rejects
	 * unsupported keywords outright — the form surfaces that explicitly
	 * instead of silently degrading the control.
	 */
	function isJsonFallback(property: Record<string, unknown>): boolean {
		const unsupported = firstUnsupportedKeyword(property);
		if (unsupported !== null) return true;
		const type = property['type'];
		if (typeof type === 'string') {
			if (type === 'object') return true;
			if (type === 'array') return !isScalarArray(property);
			return !['string', 'number', 'integer', 'boolean'].includes(type);
		}
		if (Array.isArray(type)) return true;
		// No declared type: enum of strings still renders as a select.
		return !Array.isArray(property['enum']);
	}

	function isScalarArray(property: Record<string, unknown>): boolean {
		const items = property['items'];
		if (!isRecord(items)) return false;
		const itemType = items['type'];
		if (typeof itemType !== 'string') return false;
		if (itemType === 'string') return true;
		return itemType === 'integer' || itemType === 'number';
	}

	function firstUnsupportedKeyword(schema: unknown, depth = 0): string | null {
		if (depth > 24 || !isRecord(schema)) return null;
		for (const keyword of Object.keys(schema)) {
			if (!SUPPORTED_KEYWORDS.includes(keyword) && !ANNOTATION_KEYWORDS.includes(keyword)) {
				return keyword;
			}
		}
		const type = schema['type'];
		if (typeof type === 'string' && !JSON_TYPES.includes(type)) return `type:${type}`;
		if (Array.isArray(type) && type.some((name) => typeof name !== 'string' || !JSON_TYPES.includes(name))) {
			return 'type:list';
		}
		for (const nested of ['properties', 'items', 'additionalProperties']) {
			const value = schema[nested];
			if (isRecord(value)) {
				for (const child of Object.values(value)) {
					const found = firstUnsupportedKeyword(child, depth + 1);
					if (found !== null) return found;
				}
			} else if (value !== undefined && value !== null && nested === 'items') {
				const found = firstUnsupportedKeyword(value, depth + 1);
				if (found !== null) return found;
			}
		}
		return null;
	}

	/** Field kind per declared property; JSON fallback is the honest default. */
	function controlKind(property: Record<string, unknown> | null): 'select' | 'text' | 'number' | 'checkbox' | 'list' | 'json' {
		if (property === null) return 'json';
		if (isJsonFallback(property)) return 'json';
		if (Array.isArray(property['enum'])) return 'select';
		const type = property['type'];
		if (type === 'boolean') return 'checkbox';
		if (type === 'integer' || type === 'number') return 'number';
		if (type === 'array') return 'list';
		return 'text';
	}

	function describeProperty(property: Record<string, unknown> | null, required: boolean): string {
		if (property === null) return required ? 'required, undeclared shape (raw JSON)' : 'undeclared shape (raw JSON)';
		const parts: string[] = [];
		if (required) parts.push('required');
		const type = property['type'];
		if (typeof type === 'string') parts.push(type);
		if (Array.isArray(property['enum'])) parts.push(`one of ${property['enum'].length} values`);
		if (typeof property['minLength'] === 'number') parts.push(`min length ${property['minLength']}`);
		if (typeof property['maxLength'] === 'number') parts.push(`max length ${property['maxLength']}`);
		if (typeof property['minimum'] === 'number') parts.push(`≥ ${property['minimum']}`);
		if (typeof property['maximum'] === 'number') parts.push(`≤ ${property['maximum']}`);
		if (typeof property['description'] === 'string' && property['description'] !== '') {
			parts.push(String(property['description']));
		}
		return parts.join(' · ');
	}

	// --- draft validation (mirrors the producer subset; server re-validates) --

	function validateNumberRaw(raw: string, property: Record<string, unknown>, integer: boolean): number | string {
		const text = raw.trim();
		if (text === '') return 'required';
		const parsed = Number(text);
		if (!Number.isFinite(parsed)) return 'must be a number';
		if (integer && !Number.isInteger(parsed)) return 'must be an integer';
		if (typeof property['minimum'] === 'number' && parsed < property['minimum']) {
			return `must be ≥ ${property['minimum']}`;
		}
		if (typeof property['maximum'] === 'number' && parsed > property['maximum']) {
			return `≤ ${property['maximum']}`;
		}
		if (typeof property['exclusiveMinimum'] === 'number' && parsed <= property['exclusiveMinimum']) {
			return `must be > ${property['exclusiveMinimum']}`;
		}
		if (typeof property['exclusiveMaximum'] === 'number' && parsed >= property['exclusiveMaximum']) {
			return `must be < ${property['exclusiveMaximum']}`;
		}
		return parsed;
	}

	function validateStringRaw(raw: string, property: Record<string, unknown>): string | null {
		if (typeof property['minLength'] === 'number' && raw.length < property['minLength']) {
			return `must be at least ${property['minLength']} characters`;
		}
		if (typeof property['maxLength'] === 'number' && raw.length > property['maxLength']) {
			return `must be at most ${property['maxLength']} characters`;
		}
		const pattern = property['pattern'];
		if (typeof pattern === 'string') {
			try {
				if (new RegExp(pattern).test(raw) === false) return 'does not match the declared pattern';
			} catch {
				return 'declared pattern is not a valid regular expression';
			}
		}
		return null;
	}

	function validateDraftValue(
		name: string,
		property: Record<string, unknown> | null,
		required: boolean
	): string | null {
		const raw = draft[name];
		const kind = controlKind(property);
		if (kind === 'checkbox') return null;
		const text = typeof raw === 'string' ? raw : String(raw ?? '');
		if (text.trim() === '') {
			return required ? 'required' : null;
		}
		if (property === null || kind === 'json') {
			try {
				JSON.parse(text);
				return null;
			} catch {
				return 'must be valid JSON';
			}
		}
		if (kind === 'select') {
			const choices = property['enum'];
			if (Array.isArray(choices) && !choices.some((choice) => String(choice) === text)) {
				return 'must be one of the declared values';
			}
			return null;
		}
		if (kind === 'number') {
			const parsed = validateNumberRaw(text, property, property['type'] === 'integer');
			return typeof parsed === 'string' ? parsed : null;
		}
		if (kind === 'list') {
			const lines = text.split('\n').filter((line) => line.trim() !== '');
			if (typeof property['minItems'] === 'number' && lines.length < property['minItems']) {
				return `needs at least ${property['minItems']} items`;
			}
			if (typeof property['maxItems'] === 'number' && lines.length > property['maxItems']) {
				return `allows at most ${property['maxItems']} items`;
			}
			const items = isRecord(property['items']) ? property['items'] : null;
			const itemType = items !== null ? items['type'] : null;
			if (itemType === 'string') {
				for (const line of lines) {
					const itemError = validateStringRaw(line, items ?? {});
					if (itemError !== null) return `items: ${itemError}`;
				}
			}
			if (itemType === 'integer' || itemType === 'number') {
				for (const line of lines) {
					const parsed = validateNumberRaw(line, items ?? {}, itemType === 'integer');
					if (typeof parsed === 'string') return `items: ${parsed}`;
				}
			}
			return null;
		}
		// text (string type)
		return validateStringRaw(text, property);
	}

	function validateAllFields(): boolean {
		const schema = schemaState.view?.config_schema ?? null;
		const properties = objectSchemaProperties(schema);
		const required = requiredNames(schema);
		const errors: Record<string, string> = {};
		for (const [name, property] of properties) {
			const error = validateDraftValue(name, property, required.has(name));
			if (error !== null) errors[name] = error;
		}
		// MCP registration template fields (schema may still be null on create).
		if (mcpTemplateActive) {
			for (const [name, error] of Object.entries(validateMcpDraft())) {
				if (!(name in errors)) errors[name] = error;
			}
		}
		const refsError = validateRefs();
		if (refsError !== null) errors['__refs'] = refsError;
		const extraRaw = draft['__extra_json'];
		if (typeof extraRaw === 'string' && extraRaw.trim() !== '') {
			try {
				JSON.parse(extraRaw);
			} catch {
				errors['__extra_json'] = 'must be valid JSON';
			}
		}
		const inline = firstInlineCredentialKey(buildConfigCandidate());
		if (inline !== null) errors['__inline'] = `inline credential fields are forbidden: ${inline}`;
		fieldErrors = errors;
		return Object.keys(errors).length === 0;
	}

	function validateRefs(): string | null {
		if (draftRefs.length > MAX_CREDENTIAL_REFS) {
			return `at most ${MAX_CREDENTIAL_REFS} credential references`;
		}
		const seen = new Set<string>();
		for (const ref of draftRefs) {
			if (!CREDENTIAL_REF_RE.test(ref)) {
				return `credential references must be .env names ([A-Z][A-Z0-9_]*): ${ref === '' ? '(empty)' : ref}`;
			}
			if (seen.has(ref)) return `duplicate credential reference: ${ref}`;
			seen.add(ref);
		}
		return null;
	}

	/** Mirrors the producer's `_check_config_keys` token scan; names the path. */
	function firstInlineCredentialKey(value: unknown, path = 'config', depth = 0): string | null {
		if (depth > 24) return null;
		if (Array.isArray(value)) {
			for (let index = 0; index < value.length; index += 1) {
				const found = firstInlineCredentialKey(value[index], `${path}[${index}]`, depth + 1);
				if (found !== null) return found;
			}
			return null;
		}
		if (isRecord(value)) {
			for (const [key, item] of Object.entries(value)) {
				const lower = key.toLowerCase();
				if (CREDENTIAL_TOKENS.some((token) => lower.includes(token))) return `${path}.${key}`;
				const found = firstInlineCredentialKey(item, `${path}.${key}`, depth + 1);
				if (found !== null) return found;
			}
		}
		return null;
	}

	// --- MCP registration template (frozen producer vocabulary) --------------

	const MCP_FIELDS: ReadonlyArray<{ name: string; label: string; kind: 'select' | 'text' | 'number' | 'list' }> = [
		{ name: 'transport', label: 'Transport', kind: 'select' },
		{ name: 'command', label: 'Command', kind: 'text' },
		{ name: 'args', label: 'Arguments (one per line)', kind: 'list' },
		{ name: 'cwd', label: 'Working directory', kind: 'text' },
		{ name: 'url', label: 'Server URL', kind: 'text' },
		{ name: 'connect_timeout_s', label: 'Connect timeout (s)', kind: 'number' },
		{ name: 'call_timeout_s', label: 'Call timeout (s)', kind: 'number' },
		{ name: 'max_tools', label: 'Max tools', kind: 'number' }
	];

	/** The frozen MCP registration template is offered exactly when the
	 *  producer declares no schema and did not report a load rejection. */
	const mcpTemplateActive = $derived(
		selected !== null &&
			schemaState.view !== null &&
			schemaState.view.config_schema === null &&
			!schemaState.view.load_rejection
	);

	function validateMcpDraft(): Record<string, string> {
		const errors: Record<string, string> = {};
		const transport = String(draft['__mcp_transport'] ?? '');
		if (!MCP_TRANSPORTS.includes(transport)) errors['__mcp_transport'] = 'required';
		if (transport === 'stdio') {
			const command = String(draft['__mcp_command'] ?? '').trim();
			if (command === '') errors['__mcp_command'] = 'required for stdio transport';
			else if (command.length > MCP_MAX_COMMAND_CHARS) errors['__mcp_command'] = `at most ${MCP_MAX_COMMAND_CHARS} characters`;
			const args = String(draft['__mcp_args'] ?? '')
				.split('\n')
				.filter((line) => line.trim() !== '');
			if (args.length > MCP_MAX_ARGS) errors['__mcp_args'] = `at most ${MCP_MAX_ARGS} arguments`;
			else {
				for (const arg of args) {
					if (arg.length > MCP_MAX_ARG_CHARS) {
						errors['__mcp_args'] = `each argument is at most ${MCP_MAX_ARG_CHARS} characters`;
						break;
					}
				}
			}
			const cwd = String(draft['__mcp_cwd'] ?? '').trim();
			if (cwd.length > MCP_MAX_CWD_CHARS) errors['__mcp_cwd'] = `at most ${MCP_MAX_CWD_CHARS} characters`;
		}
		if (transport === 'streamable_http') {
			const url = String(draft['__mcp_url'] ?? '').trim();
			if (url === '') errors['__mcp_url'] = 'required for streamable_http transport';
			else if (url.length > MCP_MAX_URL_CHARS) errors['__mcp_url'] = `at most ${MCP_MAX_URL_CHARS} characters`;
		}
		for (const [name, label] of [
			['__mcp_connect_timeout_s', 'Connect timeout'],
			['__mcp_call_timeout_s', 'Call timeout']
		] as const) {
			const raw = String(draft[name] ?? '').trim();
			if (raw === '') continue;
			const parsed = Number(raw);
			if (!Number.isFinite(parsed) || parsed <= 0 || parsed > MCP_MAX_TIMEOUT_S) {
				errors[name] = `${label} must be > 0 and ≤ ${MCP_MAX_TIMEOUT_S}`;
			}
		}
		const tools = String(draft['__mcp_max_tools'] ?? '').trim();
		if (tools !== '') {
			const parsed = Number(tools);
			if (!Number.isInteger(parsed) || parsed < 1 || parsed > MCP_MAX_MAX_TOOLS) {
				errors['__mcp_max_tools'] = `must be an integer 1–${MCP_MAX_MAX_TOOLS}`;
			}
		}
		return errors;
	}

	function mcpConfigFromDraft(): Record<string, unknown> {
		const transport = String(draft['__mcp_transport'] ?? '');
		const config: Record<string, unknown> = { transport };
		if (transport === 'stdio') {
			const command = String(draft['__mcp_command'] ?? '').trim();
			if (command !== '') config['command'] = command;
			const args = String(draft['__mcp_args'] ?? '')
				.split('\n')
				.map((line) => line.trim())
				.filter((line) => line !== '');
			if (args.length > 0) config['args'] = args;
			const cwd = String(draft['__mcp_cwd'] ?? '').trim();
			if (cwd !== '') config['cwd'] = cwd;
		}
		if (transport === 'streamable_http') {
			const url = String(draft['__mcp_url'] ?? '').trim();
			if (url !== '') config['url'] = url;
		}
		const connect = Number(String(draft['__mcp_connect_timeout_s'] ?? '').trim());
		if (Number.isFinite(connect) && String(draft['__mcp_connect_timeout_s'] ?? '').trim() !== '') {
			config['connect_timeout_s'] = connect;
		}
		const call = Number(String(draft['__mcp_call_timeout_s'] ?? '').trim());
		if (Number.isFinite(call) && String(draft['__mcp_call_timeout_s'] ?? '').trim() !== '') {
			config['call_timeout_s'] = call;
		}
		const tools = Number(String(draft['__mcp_max_tools'] ?? '').trim());
		if (Number.isInteger(tools) && String(draft['__mcp_max_tools'] ?? '').trim() !== '') {
			config['max_tools'] = tools;
		}
		return config;
	}

	/** Assemble the full config document candidate from the current draft. */
	function buildConfigCandidate(): Record<string, unknown> {
		if (mcpTemplateActive) return mcpConfigFromDraft();
		const schema = schemaState.view?.config_schema ?? null;
		const properties = objectSchemaProperties(schema);
		const config: Record<string, unknown> = {};
		for (const [name, property] of properties) {
			const raw = draft[name];
			const kind = controlKind(property);
			if (kind === 'checkbox') {
				config[name] = raw === true;
				continue;
			}
			const text = typeof raw === 'string' ? raw : '';
			if (text.trim() === '') continue;
			if (kind === 'select' || kind === 'text') {
				config[name] = text;
			} else if (kind === 'number') {
				config[name] = Number(text.trim());
			} else if (kind === 'list') {
				config[name] = text
					.split('\n')
					.map((line) => line.trim())
					.filter((line) => line !== '');
			} else {
				try {
					config[name] = JSON.parse(text);
				} catch {
					// Validation blocks the save before this can ship.
				}
			}
		}
		const extraRaw = draft['__extra_json'];
		if (typeof extraRaw === 'string' && extraRaw.trim() !== '') {
			try {
				Object.assign(config, JSON.parse(extraRaw) as Record<string, unknown>);
			} catch {
				// Validation blocks the save before this can ship.
			}
		}
		return config;
	}

	async function saveConfig(): Promise<void> {
		if (plugins === null || selected === null) return;
		formError = null;
		formNotice = null;
		if (!validateAllFields()) {
			formError = 'Fix the marked fields — the document was not sent.';
			return;
		}
		const stored = configState.view;
		const expectedRevision = stored === null ? 0 : stored.revision;
		saving = true;
		try {
			const triple = await plugins.editConfig(selected, {
				expected_revision: expectedRevision,
				enabled: draftEnabled,
				credential_refs: [...draftRefs],
				config: buildConfigCandidate()
			});
			conflict = false;
			formNotice = `Saved revision r${triple.revision}`;
			// Re-read what the producer now declares (a transport key flips the
			// schema to the frozen MCP one) and refresh the catalogue row.
			const [schema, config] = await Promise.all([
				plugins.readSchema(selected),
				plugins.readConfig(selected)
			]);
			schemaState = { view: schema, phase: 'ready', error: null };
			configState = { view: config, phase: 'ready', error: null };
			draftFromConfig(schema, config);
			await loadCatalog();
		} catch (error) {
			const fail = failOf(error);
			formError = `${fail.status} ${fail.code}: ${fail.message}`;
			// A stale revision is displayed and stays displayed until an explicit
			// Reload — never auto-retried with a fresh revision.
			conflict = fail.status === 409;
		} finally {
			saving = false;
		}
	}

	/** Explicit conflict recovery: re-read the stored document, keep nothing. */
	async function reloadAfterConflict(): Promise<void> {
		if (plugins === null || selected === null) return;
		try {
			const [schema, config] = await Promise.all([
				plugins.readSchema(selected),
				plugins.readConfig(selected)
			]);
			schemaState = { view: schema, phase: 'ready', error: null };
			configState = { view: config, phase: 'ready', error: null };
			draftFromConfig(schema, config);
			conflict = false;
			formError = null;
			formNotice = 'Reloaded the stored revision — your unsaved edits were discarded.';
		} catch (error) {
			const fail = failOf(error);
			formError = `${fail.status} ${fail.code}: ${fail.message}`;
		}
	}

	// --- bindings -------------------------------------------------------------

	async function openBindings(name: string): Promise<void> {
		selected = name;
		tab = 'bindings';
		bindingEditor = { open: false, serverId: '', capabilities: '', expectedRevision: 0 };
		bindingError = null;
		bindingNotice = null;
		await loadBindings(name);
	}

	async function loadBindings(name: string): Promise<void> {
		if (plugins === null) return;
		bindingsState = { view: null, phase: 'loading', error: null };
		try {
			const view = await plugins.listBindings(name);
			bindingsState = { view, phase: 'ready', error: null };
		} catch (error) {
			bindingsState = { view: null, phase: 'error', error: failOf(error) };
		}
	}

	function editBinding(binding: BindingView): void {
		bindingEditor = {
			open: true,
			serverId: binding.server_id,
			capabilities: binding.capabilities.join('\n'),
			expectedRevision: binding.revision
		};
		bindingError = null;
		bindingNotice = null;
	}

	function newBinding(): void {
		bindingEditor = { open: true, serverId: '', capabilities: '', expectedRevision: 0 };
		bindingError = null;
		bindingNotice = null;
	}

	async function saveBinding(): Promise<void> {
		if (plugins === null || selected === null) return;
		const serverId = bindingEditor.serverId.trim();
		const capabilities = bindingEditor.capabilities
			.split('\n')
			.map((line) => line.trim())
			.filter((line) => line !== '');
		if (!UUID_RE.test(serverId)) {
			bindingError = 'Server id must be a canonical UUID string.';
			return;
		}
		if (capabilities.length === 0) {
			bindingError = 'Name at least one capability to grant.';
			return;
		}
		bindingError = null;
		bindingBusy = true;
		try {
			const triple = await plugins.editBinding(selected, serverId, {
				expected_revision: bindingEditor.expectedRevision,
				capabilities
			});
			bindingNotice = `Binding saved at r${triple.revision} — grant is explicit, nothing else grants.`;
			bindingEditor = { open: false, serverId: '', capabilities: '', expectedRevision: 0 };
			await loadBindings(selected);
			await loadCatalog();
		} catch (error) {
			const fail = failOf(error);
			bindingError = `${fail.status} ${fail.code}: ${fail.message}`;
		} finally {
			bindingBusy = false;
		}
	}

	function armRevoke(binding: BindingView): void {
		bindingError = null;
		bindingNotice = null;
		revokeArmed = binding.server_id;
	}

	async function confirmRevoke(binding: BindingView): Promise<void> {
		if (plugins === null || selected === null) return;
		bindingError = null;
		bindingNotice = null;
		bindingBusy = true;
		try {
			await plugins.removeBinding(selected, binding.server_id, binding.revision);
			revokeArmed = null;
			bindingNotice = `Binding on ${binding.server_id} revoked — the configuration document stays.`;
			await loadBindings(selected);
			await loadCatalog();
		} catch (error) {
			const fail = failOf(error);
			bindingError = `${fail.status} ${fail.code}: ${fail.message}`;
		} finally {
			bindingBusy = false;
		}
	}

	// --- discovery ------------------------------------------------------------

	async function openDiscovery(name: string): Promise<void> {
		selected = name;
		tab = 'discovery';
		discoveryState = { view: null, phase: 'idle', error: null };
	}

	async function runDiscovery(): Promise<void> {
		if (plugins === null || selected === null) return;
		discoveryState = { view: null, phase: 'loading', error: null };
		try {
			const view = await plugins.runDiscovery(selected);
			discoveryState = { view, phase: 'ready', error: null };
		} catch (error) {
			discoveryState = { view: null, phase: 'error', error: failOf(error) };
		}
	}

	// --- skills ---------------------------------------------------------------

	async function openSkills(): Promise<void> {
		tab = 'skills';
		if (skills === null || skillsState.phase !== 'idle') return;
		skillsState = { view: null, phase: 'loading', error: null };
		try {
			const view = await skills.catalog();
			skillsState = { view, phase: 'ready', error: null };
		} catch (error) {
			skillsState = { view: null, phase: 'error', error: failOf(error) };
		}
	}

	async function inspectSkill(name: string): Promise<void> {
		if (skills === null) return;
		skillPackageState = { view: null, phase: 'loading', error: null };
		try {
			const view = await skills.readSkill(name);
			skillPackageState = { view, phase: 'ready', error: null };
		} catch (error) {
			skillPackageState = { view: null, phase: 'error', error: failOf(error) };
		}
	}

	function shortSha(sha: string): string {
		return sha.length > 12 ? sha.slice(0, 12) : sha;
	}

	// --- render helpers ---------------------------------------------------------

	const schemaReady = $derived(schemaState.view !== null && schemaState.phase === 'ready');
	const declaredSchema = $derived(schemaState.view?.config_schema ?? null);
	const declaredProperties = $derived(objectSchemaProperties(declaredSchema));
	const declaredRequired = $derived(requiredNames(declaredSchema));
	const selectedRow = $derived(selected === null ? null : configuredOf(selected));
	const isMcpKind = $derived(selectedRow !== null && selectedRow.kind === 'mcp');
	const unconfigured = $derived(configState.view === null && configState.phase === 'ready');
</script>

<div class="mai-plugins" data-testid="plugins-root">
	<header class="mai-plugins__header">
		<h2><span class="mai-plugins__icon"><Cog size={16} aria-hidden="true" /></span> Plugins &amp; skills</h2>
		<p class="mai-plugins__scope">
			{#if globalScope}
				Controller-host catalogue. Every operation names its plugin — and, for grant
				bindings, its server — explicitly.
			{:else}
				Server-scoped mount: this panel is a global controller view; the entry contract
				is contradicted.
			{/if}
		</p>
	</header>

	{#if plugins === null}
		<div class="mai-plugins__state mai-plugins__state--error" role="alert" data-testid="plugins-unusable">
			The plugins API seam is missing or incomplete — the panel fails closed and performs
			nothing. No fallback client is substituted.
		</div>
	{:else if !globalScope}
		<div class="mai-plugins__state mai-plugins__state--error" role="alert" data-testid="plugins-scope-error">
			This panel registers under the global scope only.
		</div>
	{:else}
		<nav class="mai-plugins__tabs" aria-label="Plugin panel sections">
			{#each TABS as entry (entry.id)}
				<button
					type="button"
					class="mai-plugins__tab"
					class:mai-plugins__tab--active={tab === entry.id}
					aria-pressed={tab === entry.id}
					onclick={() => (entry.id === 'skills' ? openSkills() : (tab = entry.id))}
					data-testid="tab-{entry.id}"
				>
					{entry.label}
				</button>
			{/each}
		</nav>

		{#if tab === 'catalog'}
			<section aria-label="Plugin catalogue" data-testid="catalog-section">
				<div class="mai-plugins__toolbar">
					<Button variant="secondary" loading={catalogState.phase === 'loading'} onclick={loadCatalog}>
						Refresh catalogue
					</Button>
				</div>
				{#if catalogState.phase === 'error'}
					<div class="mai-plugins__state mai-plugins__state--error" role="alert" data-testid="catalog-error">
						Catalogue unavailable — {catalogState.error?.status}
						{catalogState.error?.code}: {catalogState.error?.message}
					</div>
				{:else if catalogState.view === null}
					<div class="mai-plugins__state" role="status" data-testid="catalog-loading">
						Reading the catalogue…
					</div>
				{:else}
					{#if catalogNotice !== null}
						<div
							class="mai-plugins__state"
							class:mai-plugins__state--ok={catalogNotice.kind === 'ok'}
							class:mai-plugins__state--error={catalogNotice.kind === 'error'}
							role={catalogNotice.kind === 'error' ? 'alert' : 'status'}
							data-testid="catalog-notice"
						>
							{catalogNotice.text}
						</div>
					{/if}

					<h3>
						Installed (distribution metadata)
						<span class="mai-plugins__meta" data-testid="catalog-installed-count">
							{catalogState.view.installed.length} installed
						</span>
					</h3>
					{#if catalogState.view.installed.length === 0}
						<p class="mai-plugins__empty" data-testid="installed-empty">
							No native plugins installed — only MCP server connections can be configured.
						</p>
					{:else}
						<ul class="mai-plugins__chips" data-testid="installed-list">
							{#each catalogState.view.installed as name (name)}
								<li class="mai-plugins__chip" data-testid="installed-name">{name}</li>
							{/each}
						</ul>
					{/if}

					<h3>Configured documents</h3>
					<p class="mai-plugins__hint">
						Installed, configured and enabled are separate facts. Configuring or enabling
						never grants a capability — a binding is its own explicit write.
					</p>
					{#if catalogState.view.configured.length === 0}
						<p class="mai-plugins__empty" data-testid="configured-empty">
							Nothing configured yet.
						</p>
					{:else}
						<div class="mai-plugins__rows" data-testid="configured-list">
							{#each catalogState.view.configured as row (row.plugin)}
								<article class="mai-plugins__row" data-testid="configured-row" data-plugin={row.plugin}>
									<div class="mai-plugins__row-main mai-plugins__band">
										{#if row.kind === 'mcp'}
											<span class="mai-plugins__icon"><Wrench size={14} aria-hidden="true" /></span>
										{:else}
											<span class="mai-plugins__icon"><Puzzle size={14} aria-hidden="true" /></span>
										{/if}
										<span class="mai-plugins__row-name" data-testid="configured-plugin">{row.plugin}</span>
										<span class="mai-plugins__badge" data-testid="configured-kind">{row.kind}</span>
										<span
											class="mai-plugins__badge"
											class:mai-plugins__badge--on={row.enabled}
											data-testid="configured-enabled"
										>
											{row.enabled ? 'enabled' : 'disabled'}
										</span>
										<span class="mai-plugins__meta" data-testid="configured-revision">r{row.revision}</span>
									</div>
									<dl class="mai-plugins__facts">
										<div>
											<dt>Credential references</dt>
											<dd data-testid="configured-refs">
												{row.credential_refs.length === 0
													? 'none'
													: row.credential_refs.join(', ')}
											</dd>
										</div>
										<div>
											<dt>Server bindings</dt>
											<dd data-testid="configured-bindings">{row.bindings.length}</dd>
										</div>
									</dl>
									<div class="mai-plugins__row-actions">
										<Button variant="secondary" onclick={() => openConfigure(row.plugin)} data-testid="configure-start">
											Configure
										</Button>
										<Button variant="secondary" onclick={() => openBindings(row.plugin)} data-testid="bindings-start">
											Bindings
										</Button>
										{#if row.kind === 'mcp'}
											<Button variant="secondary" onclick={() => openDiscovery(row.plugin)} data-testid="discovery-start">
												Discovery
											</Button>
										{/if}
										<Button
											variant="secondary"
											loading={rowBusy[`enable:${row.plugin}`] === true}
											onclick={() => toggleEnabled(row)}
											data-testid="enable-toggle"
										>
											{row.enabled ? 'Disable' : 'Enable'}
										</Button>
										{#if deleteArmed === row.plugin}
											<div class="mai-plugins__confirm" data-testid="delete-confirm">
												<p>
													Delete the stored configuration of
													<strong>{row.plugin}</strong> at r{row.revision}? Refused while
													server bindings reference it — revoke those first.
												</p>
												<div class="mai-plugins__row-actions">
													<Button
														variant="danger"
														loading={rowBusy[`delete:${row.plugin}`] === true}
														onclick={() => confirmDelete(row)}
														data-testid="delete-confirm-yes"
													>
														Delete config (confirmed)
													</Button>
													<Button variant="secondary" onclick={() => (deleteArmed = null)} data-testid="delete-confirm-no">
														Cancel
													</Button>
												</div>
											</div>
										{:else}
											<Button
												variant="danger"
												loading={rowBusy[`delete:${row.plugin}`] === true}
												onclick={() => armDelete(row)}
												data-testid="delete-config"
											>
												Delete config
											</Button>
										{/if}
									</div>
								</article>
							{/each}
						</div>
					{/if}
				{/if}
			</section>
		{:else if tab === 'configure'}
			<section aria-label="Plugin configuration" data-testid="configure-section">
				{#if selected === null}
					<p class="mai-plugins__empty" data-testid="configure-empty">
						Pick a configured plugin in the catalogue — or configure a new connection below.
					</p>
					<form
						class="mai-plugins__new"
						data-testid="configure-new"
						onsubmit={(event) => {
							event.preventDefault();
							void openConfigure(newPluginName.trim());
						}}
					>
						<Field
							label="Plugin name"
							hint="Lowercase slug ([a-z0-9][a-z0-9._-]*); the producer re-validates."
							data-testid="configure-new-input"
							bind:value={newPluginName}
						/>
						<Button type="submit">Open form</Button>
					</form>
				{:else}
					<div class="mai-plugins__toolbar">
						<span class="mai-plugins__row-name" data-testid="configure-plugin">{selected}</span>
						{#if selectedRow !== null}
							<span class="mai-plugins__badge" data-testid="configure-kind">{selectedRow.kind}</span>
						{/if}
						<Button variant="secondary" onclick={closeConfigure} data-testid="configure-close">
							Close
						</Button>
					</div>

					{#if schemaState.phase === 'loading' || configState.phase === 'loading'}
						<div class="mai-plugins__state" role="status" data-testid="configure-loading">
							Reading the declared schema and stored document…
						</div>
					{:else if schemaState.phase === 'error'}
						<div class="mai-plugins__state mai-plugins__state--error" role="alert" data-testid="schema-error">
							Schema unavailable — {schemaState.error?.status} {schemaState.error?.code}:
							{schemaState.error?.message}
						</div>
					{:else if configState.phase === 'error'}
						<div class="mai-plugins__state mai-plugins__state--error" role="alert" data-testid="config-error">
							Stored document unavailable — {configState.error?.status}
							{configState.error?.code}: {configState.error?.message}
						</div>
					{:else if schemaReady && schemaState.view !== null}
						{#if schemaState.view.load_rejection}
							<div class="mai-plugins__state mai-plugins__state--error" role="alert" data-testid="schema-rejection">
								The installed plugin failed to load ({schemaState.view.load_rejection}) — its
								configuration schema is unknown and is never invented. The form stays locked.
							</div>
						{:else if declaredSchema === null}
							{#if mcpTemplateActive}
								<div class="mai-plugins__note" data-testid="mcp-register-note">
									No declared schema: register an MCP server connection with the frozen
									transport vocabulary (stdio / streamable_http). The producer validates the
									stored document against its frozen server schema.
								</div>
								<form
									class="mai-plugins__form"
									aria-label="MCP server registration"
									data-testid="mcp-register-form"
								>
									<Select
										label="Transport"
										value={String(draft['__mcp_transport'] ?? '')}
										onchange={(event) => (draft['__mcp_transport'] = event.currentTarget.value)}
										data-testid="mcp-transport"
									>
										<option value="">— choose —</option>
										{#each MCP_TRANSPORTS as transport (transport)}
											<option value={transport}>{transport}</option>
										{/each}
									</Select>
									{#if String(draft['__mcp_transport'] ?? '') === 'stdio'}
										<Field
											label="Command"
											hint="≤ {MCP_MAX_COMMAND_CHARS} characters; spawned controller-local."
											bind:value={() => String(draft['__mcp_command'] ?? ''), (v) => (draft['__mcp_command'] = String(v))}
											error={fieldErrors['__mcp_command'] ?? ''}
											data-testid="mcp-command"
										/>
										<label class="mai-plugins__field mai-plugins__field--wide">
											<span>Arguments (one per line, ≤ {MCP_MAX_ARGS})</span>
											<textarea
												rows="3"
												bind:value={() => String(draft['__mcp_args'] ?? ''), (v) => (draft['__mcp_args'] = String(v))}
												data-testid="mcp-args"
											></textarea>
											{#if fieldErrors['__mcp_args']}
												<strong class="mai-plugins__field-error" role="alert">{fieldErrors['__mcp_args']}</strong>
											{/if}
										</label>
										<Field
											label="Working directory"
											hint="Optional; ≤ {MCP_MAX_CWD_CHARS} characters."
											bind:value={() => String(draft['__mcp_cwd'] ?? ''), (v) => (draft['__mcp_cwd'] = String(v))}
											error={fieldErrors['__mcp_cwd'] ?? ''}
											data-testid="mcp-cwd"
										/>
									{/if}
									{#if String(draft['__mcp_transport'] ?? '') === 'streamable_http'}
										<Field
											label="Server URL"
											hint="≤ {MCP_MAX_URL_CHARS} characters."
											id="mcp-url-field"
											bind:value={() => String(draft['__mcp_url'] ?? ''), (v) => (draft['__mcp_url'] = String(v))}
											error={fieldErrors['__mcp_url'] ?? ''}
											data-testid="mcp-url"
										/>
									{/if}
									<Field
										label="Connect timeout (s)"
										hint="> 0 and ≤ {MCP_MAX_TIMEOUT_S}; default {MCP_DEFAULT_CONNECT_TIMEOUT_S}."
										bind:value={() => String(draft['__mcp_connect_timeout_s'] ?? ''), (v) => (draft['__mcp_connect_timeout_s'] = String(v))}
										error={fieldErrors['__mcp_connect_timeout_s'] ?? ''}
										data-testid="mcp-connect-timeout"
									/>
									<Field
										label="Call timeout (s)"
										hint="> 0 and ≤ {MCP_MAX_TIMEOUT_S}; default {MCP_DEFAULT_CALL_TIMEOUT_S}."
										bind:value={() => String(draft['__mcp_call_timeout_s'] ?? ''), (v) => (draft['__mcp_call_timeout_s'] = String(v))}
										error={fieldErrors['__mcp_call_timeout_s'] ?? ''}
										data-testid="mcp-call-timeout"
									/>
									<Field
										label="Max tools"
										hint="1–{MCP_MAX_MAX_TOOLS}; default {MCP_DEFAULT_MAX_TOOLS}."
										bind:value={() => String(draft['__mcp_max_tools'] ?? ''), (v) => (draft['__mcp_max_tools'] = String(v))}
										error={fieldErrors['__mcp_max_tools'] ?? ''}
										data-testid="mcp-max-tools"
									/>
									{@render refsEditor()}
									{@render enabledToggle()}
									{@render saveRow()}
								</form>
							{:else}
								<div class="mai-plugins__note" data-testid="schema-absent">
									This plugin declares no configuration schema: the producer rejects any
									non-empty config (fail closed). You can still store the enabled flag and
									credential references with an empty document.
								</div>
								{@render refsEditor()}
								{@render enabledToggle()}
								{@render saveRow()}
							{/if}
						{:else}
							<p class="mai-plugins__hint" data-testid="schema-summary">
								Generated from the declared configuration schema — {declaredProperties.size}
								{declaredProperties.size === 1 ? 'property' : 'properties'}. Secret material
								never appears here: credentials are referenced by .env name only.
							</p>
							<form class="mai-plugins__form" aria-label="Generated plugin configuration" data-testid="generated-form">
								{#each [...declaredProperties.entries()] as [name, property] (name)}
									{@render schemaField(name, property, declaredRequired.has(name))}
								{/each}
								{#if typeof draft['__extra_json'] === 'string'}
									<label class="mai-plugins__field mai-plugins__field--wide">
										<span>Stored keys outside the declared schema</span>
										<textarea
											rows="4"
											spellcheck="false"
											bind:value={() => draft['__extra_json'], (v) => (draft['__extra_json'] = v)}
											data-testid="field-__extra_json"
										></textarea>
										{#if fieldErrors['__extra_json']}
											<strong class="mai-plugins__field-error" role="alert">{fieldErrors['__extra_json']}</strong>
										{/if}
									</label>
								{/if}
								{@render refsEditor()}
								{@render enabledToggle()}
								{@render saveRow()}
							</form>
						{/if}
					{/if}
				{/if}
			</section>
		{:else if tab === 'bindings'}
			<section aria-label="Server grant bindings" data-testid="bindings-section">
				{#if selected === null}
					<p class="mai-plugins__empty" data-testid="bindings-empty">
						Pick a configured plugin in the catalogue first.
					</p>
				{:else}
					<div class="mai-plugins__toolbar">
						<span class="mai-plugins__row-name" data-testid="bindings-plugin">{selected}</span>
						<Button variant="secondary" onclick={() => loadBindings(selected ?? '')} data-testid="bindings-refresh">
							Refresh
						</Button>
						<Button onclick={newBinding} data-testid="binding-new">New binding</Button>
					</div>
					<p class="mai-plugins__hint">
						A binding grants the listed capabilities to ONE canonical server. Writing a
						binding never creates the configuration and configuring never grants (C03
						rule 7). The server id is typed here — nothing is inferred.
					</p>
					{#if bindingNotice !== null}
						<div class="mai-plugins__state mai-plugins__state--ok" role="status" data-testid="binding-notice">{bindingNotice}</div>
					{/if}
					{#if bindingError !== null}
						<div class="mai-plugins__state mai-plugins__state--error" role="alert" data-testid="binding-error">
							{bindingError}
						</div>
					{/if}
					{#if bindingsState.phase === 'loading'}
						<div class="mai-plugins__state" role="status" data-testid="bindings-loading">Reading bindings…</div>
					{:else if bindingsState.phase === 'error'}
						<div class="mai-plugins__state mai-plugins__state--error" role="alert" data-testid="bindings-error">
							Bindings unavailable — {bindingsState.error?.status} {bindingsState.error?.code}:
							{bindingsState.error?.message}
						</div>
					{:else if bindingsState.view !== null}
						{#if bindingsState.view.length === 0}
							<p class="mai-plugins__empty" data-testid="bindings-none">No server grants stored.</p>
						{:else}
							<div class="mai-plugins__rows" data-testid="bindings-list">
								{#each bindingsState.view as binding (binding.server_id)}
									<article class="mai-plugins__row" data-testid="binding-row">
										<div class="mai-plugins__row-main mai-plugins__band">
											<span class="mai-plugins__mono" data-testid="binding-server">{binding.server_id}</span>
											<span class="mai-plugins__meta" data-testid="binding-revision">r{binding.revision}</span>
										</div>
										<p class="mai-plugins__mono" data-testid="binding-caps">{binding.capabilities.join(', ')}</p>
										<div class="mai-plugins__row-actions">
											<Button variant="secondary" onclick={() => editBinding(binding)} data-testid="binding-edit">
												Edit
											</Button>
											{#if revokeArmed === binding.server_id}
												<div class="mai-plugins__confirm" data-testid="revoke-confirm">
													<p>
														Revoke the grant of <strong>{selected}</strong> on server
														<span class="mai-plugins__mono">{binding.server_id}</span>
														at r{binding.revision}? The configuration document stays.
													</p>
													<div class="mai-plugins__row-actions">
														<Button
															variant="danger"
															loading={bindingBusy}
															onclick={() => confirmRevoke(binding)}
															data-testid="revoke-confirm-yes"
														>
															Revoke (confirmed)
														</Button>
														<Button
															variant="secondary"
															onclick={() => (revokeArmed = null)}
															data-testid="revoke-confirm-no"
														>
															Cancel
														</Button>
													</div>
												</div>
											{:else}
												<Button
													variant="danger"
													loading={bindingBusy}
													onclick={() => armRevoke(binding)}
													data-testid="binding-revoke"
												>
													Revoke
												</Button>
											{/if}
										</div>
									</article>
								{/each}
							</div>
						{/if}
					{/if}
					{#if bindingEditor.open}
						<form class="mai-plugins__form" aria-label="Binding editor" data-testid="binding-form">
							<Field
								label="Server id"
								hint="Canonical UUID of the enrolled server — typed explicitly."
								bind:value={() => bindingEditor.serverId, (v) => (bindingEditor.serverId = v)}
								data-testid="binding-server-input"
							/>
							<label class="mai-plugins__field mai-plugins__field--wide">
								<span>Granted capabilities (one per line)</span>
								<textarea
									rows="4"
									spellcheck="false"
									bind:value={() => bindingEditor.capabilities, (v) => (bindingEditor.capabilities = v)}
									data-testid="binding-caps-input"
								></textarea>
							</label>
							<p class="mai-plugins__hint" data-testid="binding-revision-note">
								expected_revision {bindingEditor.expectedRevision}
								{bindingEditor.expectedRevision === 0 ? '(creates the binding)' : '(stale writes are refused)'}
							</p>
							<div class="mai-plugins__row-actions">
								<Button loading={bindingBusy} onclick={saveBinding} data-testid="binding-save">Save binding</Button>
								<Button
									variant="secondary"
									onclick={() => (bindingEditor = { ...bindingEditor, open: false })}
									data-testid="binding-cancel"
								>
									Cancel
								</Button>
							</div>
						</form>
					{/if}
				{/if}
			</section>
		{:else if tab === 'discovery'}
			<section aria-label="MCP discovery" data-testid="discovery-section">
				{#if selected === null}
					<p class="mai-plugins__empty" data-testid="discovery-empty">
						Pick an MCP-configured plugin in the catalogue first.
					</p>
				{:else}
					<div class="mai-plugins__toolbar">
						<span class="mai-plugins__row-name" data-testid="discovery-plugin">{selected}</span>
						{#if isMcpKind}
							<Button loading={discoveryState.phase === 'loading'} onclick={runDiscovery} data-testid="discovery-run">
								Run bounded discovery
							</Button>
						{:else}
							<span class="mai-plugins__badge" data-testid="discovery-not-mcp">not an MCP connection</span>
						{/if}
					</div>
					<p class="mai-plugins__hint" data-testid="discovery-note">
						One bounded diagnostic: connect once, list tools, register exactly the granted
						subset. Discovery never grants; offered-but-ungranted tools are reported by
						name. A disabled plugin refuses discovery.
					</p>
					{#if discoveryState.phase === 'loading'}
						<div class="mai-plugins__state" role="status" data-testid="discovery-loading">Discovering…</div>
					{:else if discoveryState.phase === 'error'}
						<div class="mai-plugins__state mai-plugins__state--error" role="alert" data-testid="discovery-error">
							Discovery refused — {discoveryState.error?.status} {discoveryState.error?.code}:
							{discoveryState.error?.message}
						</div>
					{:else if discoveryState.view !== null}
						<article class="mai-plugins__row" data-testid="discovery-report">
							<div class="mai-plugins__row-main mai-plugins__band">
								<span class="mai-plugins__icon"><Radar size={14} aria-hidden="true" /></span>
								<span class="mai-plugins__badge" data-testid="discovery-transport">{discoveryState.view.transport}</span>
								<span class="mai-plugins__meta" data-testid="discovery-tool-count">
									{discoveryState.view.tool_count} tools offered
								</span>
								{#if discoveryState.view.limit_exceeded}
									<span class="mai-plugins__badge mai-plugins__badge--warn" data-testid="discovery-limit">
										listing bound exceeded
									</span>
								{/if}
							</div>
							<h4>Registered (granted subset)</h4>
							{#if discoveryState.view.registered.length === 0}
								<p class="mai-plugins__empty" data-testid="discovery-registered-empty">
									Nothing granted — no tool registers from discovery alone.
								</p>
							{:else}
								<ul class="mai-plugins__chips" data-testid="discovery-registered">
									{#each discoveryState.view.registered as capability (capability)}
										<li class="mai-plugins__chip mai-plugins__chip--ok">{capability}</li>
									{/each}
								</ul>
							{/if}
							<h4>Offered but not granted</h4>
							{#if discoveryState.view.ungranted.length === 0}
								<p class="mai-plugins__empty" data-testid="discovery-ungranted-empty">None.</p>
							{:else}
								<ul class="mai-plugins__chips" data-testid="discovery-ungranted">
									{#each discoveryState.view.ungranted as capability (capability)}
										<li class="mai-plugins__chip mai-plugins__chip--info">{capability}</li>
									{/each}
								</ul>
								<p class="mai-plugins__hint">Grant explicitly under Bindings — discovery never widens access.</p>
							{/if}
							{#if discoveryState.view.stale_grants.length > 0}
								<h4>Stale grants (no longer offered)</h4>
								<ul class="mai-plugins__chips" data-testid="discovery-stale">
									{#each discoveryState.view.stale_grants as capability (capability)}
										<li class="mai-plugins__chip mai-plugins__chip--warn">{capability}</li>
									{/each}
								</ul>
							{/if}
							{#if discoveryState.view.rejections.length > 0}
								<h4>Rejections (surfaced, not hidden)</h4>
								<div class="mai-plugins__rows" data-testid="discovery-rejections">
									{#each discoveryState.view.rejections as rejection (rejection.tool)}
										<article class="mai-plugins__row" data-testid="discovery-rejection">
											<span class="mai-plugins__mono">{rejection.tool}</span>
											<span class="mai-plugins__badge mai-plugins__badge--danger">{rejection.code}</span>
											<p>{rejection.detail}</p>
										</article>
									{/each}
								</div>
							{/if}
						</article>
					{/if}
				{/if}
			</section>
		{:else if tab === 'skills'}
			<section aria-label="Skills" data-testid="skills-section">
				{#if skills === null}
					<div class="mai-plugins__state mai-plugins__state--error" role="alert" data-testid="skills-unavailable">
						The skills read seam is not wired into this window — inspection stays unavailable
						instead of guessing. The plugin surfaces above remain usable.
					</div>
				{:else if skillsState.phase === 'loading'}
					<div class="mai-plugins__state" role="status" data-testid="skills-loading">Reading the skills catalog…</div>
				{:else if skillsState.phase === 'error'}
					<div class="mai-plugins__state mai-plugins__state--error" role="alert" data-testid="skills-error">
						Skills unavailable — {skillsState.error?.status} {skillsState.error?.code}:
						{skillsState.error?.message}
					</div>
				{:else if skillsState.view !== null}
					{#if skillsState.view.rejections.length > 0}
						<h4>Load rejections (surfaced, not hidden)</h4>
						<div class="mai-plugins__rows" data-testid="skill-rejections">
							{#each skillsState.view.rejections as rejection (rejection.skill)}
								<article class="mai-plugins__row" data-testid="skill-rejection-row">
									<span class="mai-plugins__row-name">{rejection.skill}</span>
									<span class="mai-plugins__badge mai-plugins__badge--danger">{rejection.code}</span>
								</article>
							{/each}
						</div>
					{/if}
					<h4>Loaded packages</h4>
					{#if skillsState.view.skills.length === 0}
						<p class="mai-plugins__empty" data-testid="skills-empty">No skills loaded.</p>
					{:else}
						<div class="mai-plugins__rows" data-testid="skills-list">
							{#each skillsState.view.skills as skill (skill.skill)}
								<article class="mai-plugins__row" data-testid="skill-row">
									<div class="mai-plugins__row-main mai-plugins__band">
										<span class="mai-plugins__row-name" data-testid="skill-name">{skill.skill}</span>
										{#if skill.title !== null}
											<span data-testid="skill-title">{skill.title}</span>
										{/if}
									</div>
									{#if skill.description !== null}
										<p data-testid="skill-description">{skill.description}</p>
									{/if}
									<p class="mai-plugins__hint" data-testid="skill-requires">
										requires: {skill.requires_capabilities.length === 0 ? 'nothing declared' : skill.requires_capabilities.join(', ')}
									</p>
									<p class="mai-plugins__meta" data-testid="skill-digest">
										sha256 {shortSha(skill.package_sha256)} · {skill.total_bytes} bytes
									</p>
									<div class="mai-plugins__row-actions">
										<Button variant="secondary" loading={skillPackageState.phase === 'loading'} onclick={() => inspectSkill(skill.skill)} data-testid="skill-inspect">
											Inspect
										</Button>
									</div>
								</article>
							{/each}
						</div>
					{/if}
					{#if skillPackageState.phase === 'error'}
						<div class="mai-plugins__state mai-plugins__state--error" role="alert" data-testid="skill-package-error">
							Package unavailable — {skillPackageState.error?.status} {skillPackageState.error?.code}:
							{skillPackageState.error?.message}
						</div>
					{:else if skillPackageState.view !== null}
						<article class="mai-plugins__row" data-testid="skill-package">
							<h4 class="mai-plugins__band" data-testid="skill-package-title">
								<span class="mai-plugins__icon"><Package size={14} aria-hidden="true" /></span>
								{skillPackageState.view.skill}
								{#if skillPackageState.view.title !== null}
									— {skillPackageState.view.title}
								{/if}
							</h4>
							{#if skillPackageState.view.description !== null}
								<p>{skillPackageState.view.description}</p>
							{/if}
							<h5>Required capabilities</h5>
							{#if skillPackageState.view.requires_capabilities.length === 0}
								<p class="mai-plugins__empty">Nothing declared.</p>
							{:else}
								<ul class="mai-plugins__chips" data-testid="skill-package-requires">
									{#each skillPackageState.view.requires_capabilities as capability (capability)}
										<li class="mai-plugins__chip mai-plugins__chip--info">{capability}</li>
									{/each}
								</ul>
							{/if}
							<h5>Binding report (declared minus granted pool)</h5>
							<p class="mai-plugins__hint">
								The granted pool is the union of all stored binding capabilities; the worker
								composes the per-run effective surface (grants ∧ registry) — this report never
								guesses it.
							</p>
							<p data-testid="skill-granted-pool">
								granted pool: {skillPackageState.view.granted_capability_pool.length === 0
									? 'empty'
									: skillPackageState.view.granted_capability_pool.join(', ')}
							</p>
							{#if skillPackageState.view.missing_from_granted_pool.length === 0}
								<p data-testid="skill-missing-none">Nothing missing from the granted pool.</p>
							{:else}
								<ul class="mai-plugins__chips" data-testid="skill-missing">
									{#each skillPackageState.view.missing_from_granted_pool as capability (capability)}
										<li class="mai-plugins__chip mai-plugins__chip--warn">{capability}</li>
									{/each}
								</ul>
								<p class="mai-plugins__hint">
									Missing capabilities never bind implicitly — grant them under Bindings.
								</p>
							{/if}
							<h5>Instructions</h5>
							<p class="mai-plugins__meta" data-testid="skill-instructions">
								{skillPackageState.view.instructions.name} · sha256
								{shortSha(skillPackageState.view.instructions.sha256)} ·
								{skillPackageState.view.instructions.size_bytes} bytes
							</p>
							{#if skillPackageState.view.documents.length > 0}
								<h5>Documents</h5>
								<ul data-testid="skill-documents">
									{#each skillPackageState.view.documents as document (document.name)}
										<li data-testid="skill-document">
											{document.name} · sha256 {shortSha(document.sha256)} · {document.size_bytes} bytes
										</li>
									{/each}
								</ul>
							{/if}
							<p class="mai-plugins__meta" data-testid="skill-package-digest">
								package sha256 {shortSha(skillPackageState.view.package_sha256)} ·
								{skillPackageState.view.total_bytes} bytes
							</p>
						</article>
					{/if}
				{/if}
			</section>
		{/if}
	{/if}
</div>

{#snippet schemaField(name: string, property: Record<string, unknown> | null, required: boolean)}
	{@const kind = controlKind(property)}
	{@const label = String(property?.['title'] ?? name)}
	{@const hint = describeProperty(property, required)}
	{#if kind === 'select'}
		<div class="mai-plugins__field" data-testid="field-{name}">
			<Select
				{label}
				{hint}
				onchange={(event) => (draft[name] = event.currentTarget.value)}
				value={typeof draft[name] === 'string' ? draft[name] : ''}
			>
				<option value="">— choose —</option>
				{#each (property?.['enum'] as unknown[]) ?? [] as choice, index (index)}
					<option value={String(choice)}>{String(choice)}</option>
				{/each}
			</Select>
			{#if fieldErrors[name]}
				<strong class="mai-plugins__field-error" role="alert" data-testid="field-error-{name}">{fieldErrors[name]}</strong>
			{/if}
		</div>
	{:else if kind === 'checkbox'}
		<label class="mai-plugins__field mai-plugins__field--check" data-testid="field-{name}">
			<input
				type="checkbox"
				checked={draft[name] === true}
				onchange={(event) => (draft[name] = event.currentTarget.checked)}
			/>
			<span>{label}</span>
			<small>{hint}</small>
		</label>
	{:else if kind === 'number'}
		<label class="mai-plugins__field" data-testid="field-{name}">
			<span>{label}</span>
			<input
				type="number"
				step={property?.['type'] === 'integer' ? '1' : 'any'}
				min={typeof property?.['minimum'] === 'number' ? property['minimum'] : undefined}
				max={typeof property?.['maximum'] === 'number' ? property['maximum'] : undefined}
				value={typeof draft[name] === 'string' ? draft[name] : ''}
				oninput={(event) => (draft[name] = event.currentTarget.value)}
			/>
			<small>{hint}</small>
			{#if fieldErrors[name]}
				<strong class="mai-plugins__field-error" role="alert" data-testid="field-error-{name}">{fieldErrors[name]}</strong>
			{/if}
		</label>
	{:else if kind === 'list'}
		<label class="mai-plugins__field mai-plugins__field--wide" data-testid="field-{name}">
			<span>{label}</span>
			<textarea
				rows="3"
				spellcheck="false"
				value={typeof draft[name] === 'string' ? draft[name] : ''}
				oninput={(event) => (draft[name] = event.currentTarget.value)}
			></textarea>
			<small>{hint} — one item per line</small>
			{#if fieldErrors[name]}
				<strong class="mai-plugins__field-error" role="alert" data-testid="field-error-{name}">{fieldErrors[name]}</strong>
			{/if}
		</label>
	{:else if kind === 'json'}
		<label class="mai-plugins__field mai-plugins__field--wide" data-testid="field-{name}">
			<span>{label} (raw JSON)</span>
			<textarea
				rows="4"
				spellcheck="false"
				value={typeof draft[name] === 'string' ? draft[name] : ''}
				oninput={(event) => (draft[name] = event.currentTarget.value)}
			></textarea>
			<small>
				{hint}
				{#if property !== null && firstUnsupportedKeyword(property) !== null}
					· declares unsupported keyword “{firstUnsupportedKeyword(property)}” — the producer
					rejects the document; shown unmodified
				{/if}
			</small>
			{#if fieldErrors[name]}
				<strong class="mai-plugins__field-error" role="alert" data-testid="field-error-{name}">{fieldErrors[name]}</strong>
			{/if}
		</label>
	{:else}
		<Field
			label={label}
			{hint}
			id={'field-' + name}
			value={typeof draft[name] === 'string' ? draft[name] : ''}
			oninput={(event) => (draft[name] = event.currentTarget.value)}
			error={fieldErrors[name] ?? ''}
			data-testid="field-{name}"
		/>
	{/if}
{/snippet}

{#snippet refsEditor()}
	<fieldset class="mai-plugins__refs" data-testid="refs-editor">
		<legend>
			<span class="mai-plugins__icon"><KeyRound size={12} aria-hidden="true" /></span>
			Credential references (C03 rule 6) — .env NAMES only, never values
		</legend>
		<p class="mai-plugins__hint">
			References resolve server-side from the configured source at call time. There is no
			credential-value field on this panel because there is no value.
		</p>
		{#each draftRefs as ref, index (index)}
			<div class="mai-plugins__ref-row" data-testid="cred-ref-row">
				<input
					type="text"
					aria-label="Credential reference {index + 1}"
					value={ref}
					oninput={(event) => {
						const next = [...draftRefs];
						next[index] = event.currentTarget.value;
						draftRefs = next;
					}}
					data-testid="cred-ref-input"
				/>
				<Button
					variant="danger"
					onclick={() => {
						draftRefs = draftRefs.filter((_, at) => at !== index);
					}}
					data-testid="cred-ref-remove-{index}"
					aria-label="Remove credential reference {index + 1}"
				>
					✕
				</Button>
			</div>
		{/each}
		<Button
			variant="secondary"
			disabled={draftRefs.length >= MAX_CREDENTIAL_REFS}
			onclick={() => {
				draftRefs = [...draftRefs, ''];
			}}
			data-testid="cred-ref-add"
		>
			Add reference ({draftRefs.length}/{MAX_CREDENTIAL_REFS})
		</Button>
		{#if fieldErrors['__refs']}
			<strong class="mai-plugins__field-error" role="alert" data-testid="refs-error">{fieldErrors['__refs']}</strong>
		{/if}
	</fieldset>
{/snippet}

{#snippet enabledToggle()}
	<label class="mai-plugins__field mai-plugins__field--check" data-testid="enabled-toggle-field">
		<input type="checkbox" bind:checked={draftEnabled} data-testid="enabled-toggle" />
		<span>Enabled</span>
		<small>
			The stored document's own flag — distinct from installed and configured. A disabled
			plugin's tools stop composing and discovery refuses.
		</small>
	</label>
{/snippet}

{#snippet saveRow()}
	<div class="mai-plugins__row-actions" data-testid="save-row">
		<Button loading={saving} onclick={saveConfig} data-testid="config-save">Save configuration</Button>
		{#if conflict}
			<Button variant="secondary" onclick={reloadAfterConflict} data-testid="config-conflict-reload">
				Reload stored revision
			</Button>
		{/if}
	</div>
	{#if formNotice !== null}
		<div class="mai-plugins__state mai-plugins__state--ok" role="status" data-testid="config-status">{formNotice}</div>
	{/if}
	{#if formError !== null}
		<div
			class="mai-plugins__state mai-plugins__state--error"
			role="alert"
			data-testid={conflict ? 'config-conflict' : 'config-error'}
		>
			{formError}
		</div>
	{/if}
	{#if fieldErrors['__inline']}
		<div class="mai-plugins__state mai-plugins__state--error" role="alert" data-testid="config-inline-error">
			{fieldErrors['__inline']} — credentials resolve by reference (C03 rule 6).
		</div>
	{/if}
{/snippet}

<style>
	.mai-plugins {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-4);
		padding: var(--mai-space-4);
		height: 100%;
		overflow: auto;
		background: var(--mai-color-bg);
		color: var(--mai-color-text);
		font-family: var(--mai-font-sans);
		font-size: var(--mai-text-sm);
		line-height: var(--mai-leading-normal);
	}

	/* --- panel header: cogitator title band --- */

	.mai-plugins__header {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		background: var(--mai-color-surface-header);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-md);
		padding: var(--mai-space-2) var(--mai-space-3);
	}

	.mai-plugins__header h2 {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		margin: 0;
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-base);
		font-weight: var(--mai-weight-semibold);
		line-height: var(--mai-leading-tight);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-brass);
	}

	.mai-plugins__icon {
		display: inline-flex;
		flex: none;
		color: var(--mai-color-brass);
	}

	.mai-plugins__scope {
		margin: 0;
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-xs);
	}

	/* --- tab strip: brass labels, crimson active underline --- */

	.mai-plugins__tabs {
		display: flex;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
		border-bottom: var(--mai-border-width) solid var(--mai-color-border);
	}

	.mai-plugins__tab {
		appearance: none;
		border: none;
		border-bottom: 2px solid transparent;
		background: transparent;
		color: var(--mai-color-text-muted);
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-medium);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		padding: var(--mai-space-1) var(--mai-space-3);
		cursor: pointer;
		transition:
			color var(--mai-motion-fast) linear,
			border-color var(--mai-motion-fast) linear;
	}

	.mai-plugins__tab:hover {
		color: var(--mai-color-brass-dim);
	}

	.mai-plugins__tab--active,
	.mai-plugins__tab--active:hover {
		color: var(--mai-color-brass);
		border-bottom-color: var(--mai-color-primary);
	}

	.mai-plugins__tab:focus-visible {
		outline: 2px solid var(--mai-focus-ring-color);
		outline-offset: 2px;
	}

	/* --- state strips --- */

	.mai-plugins__state {
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface);
		font-size: var(--mai-text-sm);
	}

	.mai-plugins__state--ok {
		border-color: var(--mai-status-success-border);
		background: var(--mai-status-success-bg);
		color: var(--mai-status-success-text);
	}

	.mai-plugins__state--error {
		border-color: var(--mai-status-danger-border);
		background: var(--mai-status-danger-bg);
		color: var(--mai-status-danger-text);
	}

	.mai-plugins__note {
		border: var(--mai-border-width) solid var(--mai-status-info-border);
		border-left: 3px solid var(--mai-status-info-solid);
		border-radius: var(--mai-radius-md);
		background: var(--mai-status-info-bg);
		color: var(--mai-status-info-text);
		padding: var(--mai-space-2) var(--mai-space-3);
	}

	.mai-plugins__toolbar {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
	}

	/* --- raised record cards --- */

	.mai-plugins__rows {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
	}

	.mai-plugins__row {
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-lg);
		background: var(--mai-color-surface-raised);
		padding: var(--mai-space-3);
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
	}

	.mai-plugins__row p {
		margin: 0;
	}

	.mai-plugins__row-main {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
	}

	/* Card header band: a header strip with a brass edging opens each card. */
	.mai-plugins__band {
		margin: calc(-1 * var(--mai-space-3)) calc(-1 * var(--mai-space-3)) var(--mai-space-2);
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface-header);
		border-left: 3px solid var(--mai-color-brass);
		border-bottom: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-lg) var(--mai-radius-lg) 0 0;
	}

	.mai-plugins__band .mai-plugins__mono,
	.mai-plugins__band .mai-plugins__meta {
		color: var(--mai-color-text);
	}

	.mai-plugins__row-name {
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-brass);
		overflow-wrap: anywhere;
	}

	.mai-plugins__row-actions {
		display: flex;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
	}

	/* --- badges and telemetry chips --- */

	.mai-plugins__badge {
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		padding: 0 var(--mai-space-2);
		color: var(--mai-color-text-muted);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
	}

	.mai-plugins__badge--on {
		border-color: var(--mai-status-success-border);
		background: var(--mai-status-success-bg);
		color: var(--mai-status-success-text);
	}

	.mai-plugins__badge--warn {
		border-color: var(--mai-status-warning-border);
		background: var(--mai-status-warning-bg);
		color: var(--mai-status-warning-text);
	}

	.mai-plugins__badge--danger {
		border-color: var(--mai-status-danger-border);
		background: var(--mai-status-danger-bg);
		color: var(--mai-status-danger-text);
	}

	/* Revision triples, SHAs, byte counts: mono chips on inset strips. */
	.mai-plugins__meta {
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface);
		padding: 0 var(--mai-space-2);
		color: var(--mai-color-text-muted);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		overflow-wrap: anywhere;
	}

	.mai-plugins__mono {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		overflow-wrap: anywhere;
	}

	/* --- capability / skill id lists --- */

	.mai-plugins__chips {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
	}

	.mai-plugins__chip {
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface);
		padding: 0 var(--mai-space-2);
		color: var(--mai-color-text-muted);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		overflow-wrap: anywhere;
	}

	.mai-plugins__chip--ok {
		border-color: var(--mai-status-success-border);
		background: var(--mai-status-success-bg);
		color: var(--mai-status-success-text);
	}

	.mai-plugins__chip--info {
		border-color: var(--mai-status-info-border);
		background: var(--mai-status-info-bg);
		color: var(--mai-status-info-text);
	}

	.mai-plugins__chip--warn {
		border-color: var(--mai-status-warning-border);
		background: var(--mai-status-warning-bg);
		color: var(--mai-status-warning-text);
	}

	.mai-plugins__confirm {
		border: var(--mai-border-width) solid var(--mai-status-danger-border);
		border-left: 3px solid var(--mai-status-danger-solid);
		border-radius: var(--mai-radius-md);
		background: var(--mai-status-danger-bg);
		color: var(--mai-status-danger-text);
		padding: var(--mai-space-2) var(--mai-space-3);
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		width: 100%;
	}

	.mai-plugins__confirm p {
		margin: 0;
	}

	.mai-plugins__facts {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mai-space-2) var(--mai-space-5);
		margin: 0;
	}

	.mai-plugins__facts dt {
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-xs);
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	.mai-plugins__facts dd {
		margin: 0;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		overflow-wrap: anywhere;
	}

	.mai-plugins__hint {
		margin: 0;
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-xs);
	}

	.mai-plugins__empty {
		margin: 0;
		color: var(--mai-color-text-muted);
		font-style: italic;
	}

	/* --- schema-generated forms: fields in a clean grid --- */

	.mai-plugins__form {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr));
		gap: var(--mai-space-3) var(--mai-space-4);
		align-items: start;
		max-width: 52rem;
		background: var(--mai-color-surface-raised);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-lg);
		padding: var(--mai-space-3);
	}

	.mai-plugins__form > .mai-plugins__refs,
	.mai-plugins__form > .mai-plugins__field--check,
	.mai-plugins__form > .mai-plugins__field--wide,
	.mai-plugins__form > .mai-plugins__row-actions,
	.mai-plugins__form > .mai-plugins__state,
	.mai-plugins__form > .mai-plugins__hint {
		grid-column: 1 / -1;
	}

	.mai-plugins__new {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		max-width: 24rem;
		background: var(--mai-color-surface-raised);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-lg);
		padding: var(--mai-space-3);
	}

	.mai-plugins__field {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		min-width: 0;
	}

	.mai-plugins__field--check {
		flex-direction: row;
		align-items: flex-start;
		gap: var(--mai-space-2);
	}

	.mai-plugins__field > span {
		font-weight: var(--mai-weight-medium);
	}

	.mai-plugins__field small {
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-xs);
	}

	.mai-plugins__field-error {
		color: var(--mai-status-danger-text);
		font-size: var(--mai-text-xs);
	}

	.mai-plugins__field input,
	.mai-plugins__field textarea {
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-bg);
		color: var(--mai-color-text);
		padding: var(--mai-space-1) var(--mai-space-2);
		font: inherit;
		transition: border-color var(--mai-motion-fast) linear;
	}

	.mai-plugins__field input:focus-visible,
	.mai-plugins__field textarea:focus-visible {
		border-color: var(--mai-color-primary);
		outline: 2px solid var(--mai-focus-ring-color);
		outline-offset: 1px;
	}

	.mai-plugins__field textarea {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
	}

	/* --- credential reference editor --- */

	.mai-plugins__refs {
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface);
		padding: var(--mai-space-3);
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
	}

	.mai-plugins__refs legend {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		padding: 0 var(--mai-space-1);
		font-weight: var(--mai-weight-semibold);
		font-size: var(--mai-text-sm);
	}

	.mai-plugins__refs legend .mai-plugins__icon {
		vertical-align: -0.125em;
	}

	.mai-plugins__ref-row {
		display: flex;
		gap: var(--mai-space-2);
		align-items: center;
	}

	.mai-plugins__ref-row input {
		flex: 1;
		min-width: 0;
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-bg);
		color: var(--mai-color-text);
		padding: var(--mai-space-1) var(--mai-space-2);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
	}

	h3,
	h4,
	h5 {
		margin: var(--mai-space-2) 0 0;
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-semibold);
		line-height: var(--mai-leading-tight);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-brass);
	}
</style>
