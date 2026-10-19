<script module lang="ts">
	/**
	 * DesktopShell — composed desktop workspace (card `01-desktop-shell`, P01).
	 *
	 * Module scope: the interim benign fixture panels and their frozen
	 * registry (windowed-workspace ADR §2 layer table: panel registry/host +
	 * window chrome + workspace state, composed here, owned by nobody else).
	 *
	 * Fixture policy (brief acceptance 2): until auth/domain features exist,
	 * the only openable panels are LABELED benign placeholders — static
	 * self-describing views that fetch nothing, subscribe to nothing and
	 * display no domain or monitoring data. They are built with the public
	 * `createRawSnippet` API because the registry's `PanelComponent` slot
	 * needs a component function and this card may not add `.svelte` fixture
	 * files outside its allowlist; feature cards replace them with compiled
	 * panels + registry entries (ADR §7.1). They never run during SSR:
	 * PanelHost resolves components through a lazy loader inside a
	 * client-only effect, so the server render stays the empty D4 shell.
	 */
	import { createRawSnippet } from 'svelte';
	import {
		createPanelRegistry,
		singletonInstanceKey,
		type PanelComponent,
		type PanelContext,
		type PanelRegistry,
		type PanelRegistryEntry
	} from './registry.js';
	import {
		type PanelKind,
		type ServerId,
		type WindowDescriptor,
		type WindowGeometry
	} from './types.js';
	// The reviewed MAI v1 feature entries (P02–P12): each is the feature's
	// single registration point (ADR §7.1) — the shell only composes them.
	import { applicationsEntry } from './entries/08-web-apps.js';
	import { capabilityPolicyEntry } from './entries/03-web-policy.js';
	import { chatEntry } from './entries/06-web-chat.js';
	import { docsEntry } from './entries/07-web-docs.js';
	import { executionEntry } from './entries/05-web-execution.js';
	import { jobsEntry } from './entries/05-web-jobs.js';
	import { knowledgeModelsEntry } from './entries/11-web-memory-models.js';
	import { modelSettingsEntry } from './entries/06-web-models.js';
	import { overviewEntry } from './entries/10-web-dashboard.js';
	import { pluginFormsEntry } from './entries/09-web-plugin-forms.js';
	import { serverProfileEntry } from './entries/02-web-server.js';

	/**
	 * Fixture server identities (opaque UUIDs per contracts.md C01 rule 1).
	 * Display labels live beside them and are attached to windows only at
	 * open time; nothing here is a mutable "current server" — the launcher
	 * selection below is a visible control consumed exactly once, when a
	 * new open request pins its scope (ADR §3.2, NT5).
	 */
	const FIXTURE_SERVERS: readonly { id: ServerId; label: string }[] = Object.freeze([
		{ id: '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f', label: 'Server Alpha' },
		{ id: 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60', label: 'Server Beta' }
	]);

	/** Human label for a fixture server id; unknown ids read as "Server". */
	function fixtureServerLabel(id: ServerId): string {
		return FIXTURE_SERVERS.find((server) => server.id === id)?.label ?? 'Server';
	}

	/** The pinned target of a live descriptor, as fixture display text. */
	function describeScope(descriptor: WindowDescriptor): string {
		const scope = descriptor.scope;
		if (scope.kind === 'global') return 'global';
		const object = scope.object ? ` · ${scope.object.kind} ${scope.object.id}` : '';
		return `server ${scope.server_id}${object}`;
	}

	/**
	 * One benign fixture panel. Renders its label, the pinned target of ITS
	 * descriptor (real metadata, not live data) and a local interactions
	 * button whose counter is closure state: it survives focus/drag of any
	 * window (content stays mounted) and resets when the content remounts
	 * (close/reopen, hide/restore) — that is the mount lifecycle made
	 * observable without any global test hook.
	 *
	 * Built with the public `createRawSnippet` API (its `Snippet` shape is
	 * assignable to the registry's `PanelComponent` slot). Every value
	 * interpolated into the HTML is a registry constant or a UUID-validated
	 * descriptor field — no free text ever enters this path (ADR §8 rule 1).
	 */
	function fixturePanel(kind: PanelKind, heading: string): PanelComponent {
		// `createRawSnippet` returns a function called `(anchor, props)` at
		// runtime — the same shape as the compiled `<Panel panel={…} />` call —
		// but its `Snippet` type hides the anchor parameter, so the slot
		// assignment needs one explicit cast here. Through the component slot
		// the argument is the plain props object; through an `{@render}` it is
		// a getter, so both forms are resolved.
		return createRawSnippet<[{ panel: PanelContext }]>((getProps) => {
			const props = typeof getProps === 'function' ? getProps() : getProps;
			const { descriptor } = props.panel;
			const banner = `Fixture panel "${heading}" — benign placeholder, no live data.`;
			const target = `Pinned target: ${describeScope(descriptor)}`;
			return {
				render: () =>
					`<div class="mai-fixture-panel" data-testid="fixture-panel" data-fixture-panel="${kind}">` +
					`<p class="mai-fixture-panel__banner">${banner}</p>` +
					`<p class="mai-fixture-panel__target">${target}</p>` +
					`<button type="button" class="mai-fixture-panel__interactions" data-testid="fixture-interactions">Fixture interactions: 0</button>` +
					`</div>`,
				setup: (root) => {
					const button = root.querySelector('button');
					let count = 0;
					const onClick = () => {
						count += 1;
						button!.textContent = `Fixture interactions: ${count}`;
					};
					button?.addEventListener('click', onClick);
					return () => button?.removeEventListener('click', onClick);
				}
			};
		}) as unknown as PanelComponent;
	}

	/**
	 * The interim fixture registry: validated, frozen, one instance per
	 * shell (no module-level singleton — ADR §5 rule 2). `fixture-prefs`
	 * declares an entry default geometry so both the entry-default and the
	 * shell-cascade placement paths are exercised.
	 */
	function createDesktopRegistry(): PanelRegistry {
		const result = createPanelRegistry([
			// --- Interim benign fixtures (fixture policy, brief acceptance 2) ----
			{
				panel_kind: 'fixture-notes',
				title: 'Notes',
				scope_types: ['server'],
				component: () => Promise.resolve(fixturePanel('fixture-notes', 'Notes'))
			},
			{
				panel_kind: 'fixture-prefs',
				title: 'Preferences',
				scope_types: ['global'],
				singleton: true,
				default_geometry: { x: 560, y: 88, width: 360, height: 260 },
				component: () => Promise.resolve(fixturePanel('fixture-prefs', 'Preferences'))
			},
			{
				// Error-lifecycle fixture (brief acceptance 2): a deterministic
				// failing loader. The window still opens; the failure is
				// contained by the panel host's safe error view (Retry offered,
				// frame close intact, siblings unaffected, zero network use).
				// Retry re-invokes this same loader and keeps failing — the
				// honest composed-surface behavior for a broken panel.
				panel_kind: 'fixture-broken',
				title: 'Broken',
				scope_types: ['global'],
				component: () => Promise.reject(new Error('fixture-broken loader always fails'))
			},
			// --- The reviewed MAI v1 feature panels (P02–P12) --------------------
			serverProfileEntry(),
			capabilityPolicyEntry(),
			executionEntry(),
			jobsEntry(),
			chatEntry(),
			modelSettingsEntry(),
			docsEntry(),
			applicationsEntry(),
			overviewEntry(),
			pluginFormsEntry(),
			knowledgeModelsEntry()
		]);
		if (!result.ok) {
			// Constant input; failure here is a programming defect, not a
			// runtime condition — fail loudly instead of rendering a shell
			// that cannot open anything.
			throw new Error(`desktop fixture registry rejected: ${result.issue.code}`);
		}
		return result.value;
	}

	/** Shell default for entries without `default_geometry` (ADR §7.1). */
	const SHELL_DEFAULT_GEOMETRY: WindowGeometry = { x: 24, y: 24, width: 460, height: 320 };
</script>

<script lang="ts">
	/**
	 * Instance scope: the scoped controller of one desktop (brief
	 * acceptance 3). One composition per shell instance — no module-level
	 * store, nothing shared between sessions or SSR requests (ADR §5). The
	 * shell wires the produced adapters together and owns no
	 * geometry/drag/presentation logic of its own:
	 *
	 *  - open: registry scope check → pinned explicit scope → workspace.open
	 *    (the frozen open path proven in panel-host.spec's composed test);
	 *  - focus: frame activation intent → presentation.focus (PT2, raises a
	 *    VISIBLE window only — a minimize control's bubbled activation must
	 *    never resurrect the just-hidden window); taskbar entries →
	 *    presentation.activate (PT4, restore+focus);
	 *  - move/resize: the frame's drag/resize adapters emit clamped geometry
	 *    intents → workspace.move (PT1/PT3) — the shell adds no pointer code;
	 *    the commit is suppressed while the frame is not `normal`, because a
	 *    maximized frame's bounds belong to the presentation overlay;
	 *  - minimize/maximize: frame control intents → the presentation
	 *    controller (PT4). The maximized OVERLAY is CSS drawn here over the
	 *    committed geometry, which the descriptor keeps untouched;
	 *  - persistence: the stored effective layout restores once on mount and
	 *    every transition persists (PT6), re-entering through the same open
	 *    path; same-browser only, cleared on session-identity change;
	 *  - reflow: the responsive controller re-clamps live windows on
	 *    viewport resize/zoom (NT3) through the same move transition;
	 *  - close: frame close intent → workspace.close (PT5); view-only, the
	 *    state layer runs release hooks and nothing here can touch any
	 *    backend (NT6; contracts.md C08 rule 5, C11).
	 */
	import { createWorkspaceState } from './state.js';
	import { createPresentationController } from './presentation.js';
	import { createLayoutPersistence, type RestoredLayoutReport } from './layout.js';
	import { createResponsiveController } from './responsive.js';
	import { clampDraggedGeometry } from './drag.js';
	import type { WindowId, WindowScope } from './types.js';
	import type { PresentationSnapshot } from './presentation.js';
	import type { SwitcherEntry } from './WindowSwitcher.svelte';
	import WindowFrame from './WindowFrame.svelte';
	import PanelHost from './PanelHost.svelte';
	import WindowSwitcher from './WindowSwitcher.svelte';
	import ChatPanel from '$lib/panels/ChatPanel.svelte';
	import Button from '$lib/ui/Button.svelte';
	import Field from '$lib/ui/Field.svelte';
	import Select from '$lib/ui/Select.svelte';
	import GlassSurface from '$lib/ui/GlassSurface.svelte';
	import SideDrawer from '$lib/ui/SideDrawer.svelte';
	import ServerTreeNav from './ServerTreeNav.svelte';
	import ChatDrawerSection from './ChatDrawerSection.svelte';
	// Live transport + the per-panel service slices (each feature entry names
	// its exact contract; the shell maps ONE client onto all of them).
	// Live transport: one client per shell instance (ADR §5 — instances share
	// nothing). The feature entries name the per-panel contracts; the slices
	// below map this ONE client onto each panel's exact interface.
	import { ApiError, createMaiApiClient, CSRF_COOKIE, type ProfileResponse } from '$lib/api/client.js';
	import { APPLICATIONS_PANEL_KIND, applicationsServices, type ApplicationsApi } from './entries/08-web-apps.js';
	import { CAPABILITY_POLICY_PANEL_KIND, capabilityPolicyServices } from './entries/03-web-policy.js';
	import {
		CHAT_PANEL_KIND,
		GLOBAL_CONSULT_SERVER_ID,
		browserChatEventStream,
		chatServices,
		type ChatApi,
		type ChatServiceOption,
		type ConversationView,
		type RunDetail
	} from './entries/06-web-chat.js';
	import { DOCS_PANEL_KIND, docsServices } from './entries/07-web-docs.js';
	import { EXECUTION_PANEL_KIND, executionServices } from './entries/05-web-execution.js';
	import { JOBS_PANEL_KIND, browserRunEventStream, jobsServices, type JobsApi } from './entries/05-web-jobs.js';
	import {
		KNOWLEDGE_MODELS_PANEL_KIND,
		knowledgeModelsServices
	} from './entries/11-web-memory-models.js';
	import {
		MODEL_SETTINGS_PANEL_KIND,
		modelSettingsServices,
		type ModelSettingsApi
	} from './entries/06-web-models.js';
	import {
		OVERVIEW_PANEL_KIND,
		overviewServices,
		type EvidenceAttachPayload,
		type OverviewApi
	} from './entries/10-web-dashboard.js';
	import {
		PLUGIN_FORMS_PANEL_KIND,
		pluginFormsServices,
		type PluginsApi,
		type SkillsApi
	} from './entries/09-web-plugin-forms.js';
	import { SERVER_PROFILE_PANEL_KIND, serverProfileServices } from './entries/02-web-server.js';
	import type { PanelServices } from './registry.js';

	const registry = createDesktopRegistry();

	/** One workspace per shell instance (ADR §5 rule 2). */
	const workspace = createWorkspaceState({
		knownPanelKinds: registry.knownPanelKinds(),
		instanceKey: (kind, scope) => {
			const entry = registry.entry(kind);
			return entry ? singletonInstanceKey(kind, entry, scope) : null;
		}
	});

	/** Effective presentation view: minimize/maximize/hidden projection. */
	const presentation = createPresentationController(workspace);

	/**
	 * Same-browser persistence of the EFFECTIVE layout (PT6). The anonymous
	 * session binding keeps one browser's layout across reloads and lets a
	 * later auth identity (P02) clear it via handleSessionChange instead of
	 * carrying desktop geometry across sessions.
	 */
	const persistence = createLayoutPersistence({
		state: workspace,
		presentation,
		registry,
		session: null
	});

	/** Viewport reflow (NT3): re-clamps live windows on resize/zoom. */
	const responsive = createResponsiveController({
		state: workspace,
		registry,
		origin: () => {
			// The frames' positioned ancestor — the same measured reference the
			// drag adapter uses at gesture start.
			const rect = desktopEl?.getBoundingClientRect();
			return { left: Math.round(rect?.left ?? 0), top: Math.round(rect?.top ?? 0) };
		}
	});

	let snapshot = $state<PresentationSnapshot>(presentation.snapshot());
	$effect(() => presentation.subscribe((next) => (snapshot = next)));

	/**
	 * Stable render order — the open sequence, NOT the stacking order.
	 * Rendering in stacking order would reparent a window's subtree on every
	 * raise, and the raise often originates FROM the focused title bar: the
	 * move removes and reinserts the focused frame, dropping focus to the
	 * document (breaking PT2's keyboard activation) and reparenting subtrees
	 * mid-gesture (the documented window-drag convention). Visual stacking
	 * stays with `descriptor.z`; DOM order stays stable.
	 */
	const openOrder = new Map<WindowId, number>();
	let openSeq = 0;

	const renderOrder = $derived.by(() =>
		[...snapshot.windows]
			// Hidden windows render no frame (this composition uses the default
			// keep_mounted_when_hidden: false policy, ADR §7.1); their taskbar
			// entries remain.
			.filter((win) => win.presentation !== 'minimized')
			.sort((a, b) => (openOrder.get(a.window_id) ?? 0) - (openOrder.get(b.window_id) ?? 0))
	);

	/** Taskbar projection (PT4): one entry per live window, hidden included. */
	const switcherEntries = $derived.by<SwitcherEntry[]>(() =>
		snapshot.windows.map((win) => ({
			window_id: win.window_id,
			title: registry.entry(win.panel_kind)?.title ?? win.panel_kind,
			context_label: switcherContextFor(win),
			minimized: win.presentation === 'minimized',
			focused: snapshot.focused_id === win.window_id
		}))
	);

	/**
	 * The launcher's visible server selection — read once per open request
	 * to PIN the new window's scope (NT5); it can never reach an existing
	 * window, because descriptors are immutable after open.
	 */
	let selectedServerId = $state<ServerId>(FIXTURE_SERVERS[0].id);

	/**
	 * Launcher buttons. Two visible sets, keyed by the live-session state:
	 * the interim fixtures when no backend session is active (the fixture
	 * desktop — zero API requests, Playwright-stable), and the reviewed MAI
	 * v1 feature panels once a live session answered. The registry itself
	 * always holds BOTH sets; only the launcher gates them.
	 */
	const fixtureEntries = registry
		.entries()
		.filter((entry) => entry.enabled && entry.panel_kind.startsWith('fixture-'));
	const liveEntries = registry
		.entries()
		.filter((entry) => entry.enabled && !entry.panel_kind.startsWith('fixture-'));

	/** Last launcher outcome (open result or failure notice) — never data. */
	let status = $state<string | null>(null);

	/** Positioned desktop region the frames are placed within. */
	let desktopEl: HTMLDivElement | null = null;

	/** Cascade counter for entries without a registry default geometry. */
	let cascade = 0;

	// ---------------------------------------------------------------------------
	// Live session (mai-wire-desktop): ONE client, per-panel service slices.
	//
	// The fixture desktop performs zero API requests: the live attach runs
	// only when the readable `mai_csrf` cookie (auth ADR §5, set by a real
	// login) is already in `document.cookie` — fixture-only browsers never
	// trigger a request. The live servers then REPLACE the fixture launcher
	// selection, and the reviewed feature panels open with the exact service
	// slice each entry contract declares.
	// ---------------------------------------------------------------------------

	const api = createMaiApiClient();

	/** Live attach state: fixture desktop until a session cookie answers. */
	let live = $state(false);
	let liveServers = $state<ProfileResponse[]>([]);

	/** Enrollment form visibility/busy/error (live mode only). */
	let enrollOpen = $state(false);
	let enrollBusy = $state(false);
	let enrollError = $state<string | null>(null);
	let enrollName = $state('');
	let enrollPromptRevision = $state('1');
	let enrollHost = $state('');
	let enrollPort = $state('22');
	let enrollUser = $state('');
	let enrollHostKey = $state('');
	let enrollCredentialRef = $state('');
	let enrollPrivilege = $state<'none' | 'sudo' | 'root'>('sudo');
	let enrollCapabilities = $state('');

	/** Launcher options: live profiles replace the fixture pair when live. */
	const serverOptions = $derived.by<{ id: ServerId; label: string }[]>(() =>
		live
			? liveServers.map((profile) => ({ id: String(profile.server_id), label: profile.name }))
			: [...FIXTURE_SERVERS]
	);

	/** Label for a pinned server id: live profile name, else fixture label. */
	function serverLabel(id: ServerId): string {
		return (
			liveServers.find((profile) => String(profile.server_id) === id)?.name ??
			fixtureServerLabel(id)
		);
	}

	/** The readable CSRF cookie is the honest "a session may exist" probe. */
	function hasSessionCookie(): boolean {
		if (typeof document === 'undefined') return false;
		return document.cookie
			.split(';')
			.some((pair) => pair.trim().startsWith(`${CSRF_COOKIE}=`));
	}

	/** Load the enrolled servers; a 401 demotes the shell to fixture mode. */
	async function connectLive(): Promise<void> {
		try {
			const { servers } = await api.listProfiles();
			liveServers = servers;
			live = true;
			status = null;
			if (servers.length > 0) selectedServerId = String(servers[0].server_id);
		} catch (error) {
			live = false;
			liveServers = [];
			if (error instanceof ApiError && error.code === 'unauthenticated') {
				status = 'Live session required — sign in to open the live desktop.';
			} else {
				status = `Live servers unavailable${error instanceof ApiError ? ` (${error.code})` : ''}.`;
			}
		}
	}

	/** Sign out: clear the session, return to the fixture desktop. */
	async function signOut(): Promise<void> {
		try {
			await api.logout();
		} catch {
			// An already-expired session still ends in fixture mode below.
		}
		live = false;
		liveServers = [];
		selectedServerId = FIXTURE_SERVERS[0].id;
		status = 'Signed out — fixture desktop.';
	}

	/** Enroll one server through the reviewed create route, then refresh. */
	async function enrollServer(event: SubmitEvent): Promise<void> {
		event.preventDefault();
		enrollError = null;
		const promptRevision = Number(enrollPromptRevision);
		const port = Number(enrollPort);
		if (!Number.isInteger(promptRevision) || promptRevision < 1) {
			enrollError = 'Prompt revision must be an integer ≥ 1.';
			return;
		}
		if (!Number.isInteger(port) || port < 1 || port > 65535) {
			enrollError = 'SSH port must be an integer between 1 and 65535.';
			return;
		}
		enrollBusy = true;
		try {
			await api.createProfile({
				name: enrollName,
				prompt_revision: promptRevision,
				environment: 'linux_ssh',
				ssh: {
					host: enrollHost,
					port,
					user: enrollUser,
					host_key: enrollHostKey,
					credential_ref: enrollCredentialRef
				},
				privilege: enrollPrivilege,
				capabilities: enrollCapabilities
					.split(',')
					.map((id) => id.trim())
					.filter((id) => id !== '')
			});
			enrollName = '';
			enrollHost = '';
			enrollUser = '';
			enrollHostKey = '';
			enrollCredentialRef = '';
			enrollCapabilities = '';
			enrollOpen = false;
			await connectLive();
			status = 'Server enrolled.';
		} catch (error) {
			enrollError =
				error instanceof ApiError ? `${error.code}: ${error.message}` : 'Enrollment failed.';
		} finally {
			enrollBusy = false;
		}
	}

	// --- Per-panel service slices (each entry's exact contract) ---------------

	const chatApi: ChatApi = {
		submitMessage: api.submitMessage,
		listConversations: api.listConversations,
		readConversation: api.readConversation,
		cancelConversation: api.cancelConversation,
		streamConversationEvents: browserChatEventStream,
		readRun: api.readRun,
		readApproval: api.readApproval,
		decideApproval: api.decideApproval
	};

	const jobsApi: JobsApi = {
		listRuns: api.listRuns,
		readRun: api.readRun,
		cancelRun: api.cancelRun,
		listSchedules: api.listSchedules,
		createSchedule: api.createSchedule,
		updateSchedule: api.updateSchedule,
		deleteSchedule: api.deleteSchedule,
		streamRunEvents: browserRunEventStream
	};

	// ---------------------------------------------------------------------
	// Always-Visible Bottom Chat Dock (mui-03): ChatPanel mounts persistently
	// at the bottom of the viewport — NOT as a registry window (the dock
	// element carries no data-panel-kind and never enters the workspace
	// state). The fixture desktop must perform ZERO API requests, so until a
	// live session answers, the dock receives a safe IN-MEMORY ChatApi: every
	// method resolves from a local store, the event stream parks until its
	// abort signal (no fetch ever leaves the page), and approval routes fail
	// closed as absence. The dock is fully interactive offline; a live
	// session swaps the slice for the real transport-backed one.
	// ---------------------------------------------------------------------

	/** Fixture services offered by the dock's `@service` autocomplete. */
	const DOCK_AVAILABLE_SERVICES: readonly ChatServiceOption[] = Object.freeze([
		{ name: 'enshrouded', category: 'games' },
		{ name: 'nginx', category: 'web' },
		{ name: 'postgres', category: 'databases' },
		{ name: 'backups', category: 'maintenance' }
	]);

	/**
	 * One offline conversation store per shell instance (ADR §5 rule 2 — no
	 * module-level state). Submitted rows replay exactly like wire rows:
	 * value-free view data, `running` state, no events.
	 */
	function createMemoryChatApi(): ChatApi {
		const conversations = new Map<string, ConversationView>();
		const stamp = () => new Date().toISOString();
		const rowOf = (runId: string, serverId: string, profile: string): ConversationView => ({
			run_id: runId,
			server_id: serverId,
			parent_run_id: null,
			depth: 0,
			profile_revision: 1,
			prompt_revision: 1,
			model_profile: profile,
			requested_grants: [],
			state: 'running',
			cancel_requested_at: null,
			submitted_at: stamp(),
			updated_at: stamp()
		});
		return {
			async submitMessage(payload) {
				const base = rowOf(payload.run_id, payload.server_id, payload.profile);
				base.requested_grants = [...(payload.capabilities ?? [])];
				base.children = (payload.children ?? []).map((child, index) => ({
					...rowOf(
						`${payload.run_id}-child-${String(index + 1)}`,
						payload.server_id,
						child.profile ?? payload.profile
					),
					parent_run_id: payload.run_id,
					depth: 1,
					state: 'queued'
				}));
				base.attachments = (payload.attachments ?? []).map((one) => ({
					...one,
					sha256: '',
					bytes: 0
				}));
				if (payload.service_tags !== undefined) base.service_tags = [...payload.service_tags];
				conversations.set(payload.run_id, base);
				return { conversation: structuredClone(base), deduplicated: false };
			},
			async listConversations(serverId, page) {
				const rows = [...conversations.values()]
					.filter((row) => row.server_id === serverId)
					.sort((a, b) => a.submitted_at.localeCompare(b.submitted_at));
				return {
					conversations: structuredClone(rows),
					limit: page?.limit ?? 20,
					offset: page?.offset ?? 0,
					total: rows.length
				};
			},
			async readConversation(runId) {
				const row = conversations.get(runId);
				if (row === undefined) {
					throw new ApiError(404, 'not_found', 'no run stored for this run id');
				}
				return structuredClone(row);
			},
			async cancelConversation(runId) {
				const row = conversations.get(runId);
				if (row === undefined) {
					throw new ApiError(404, 'not_found', 'no run stored for this run id');
				}
				if (row.cancel_requested_at === null) row.cancel_requested_at = stamp();
				return {
					run_id: runId,
					state: row.state,
					outcome: null,
					requested_at: row.cancel_requested_at,
					outcome_at: null,
					requested_now: true,
					unresolved_seqs: []
				};
			},
			/** Offline: park until the panel aborts the subscription — no fetch. */
			async streamConversationEvents(_runId, options) {
				return new Promise<void>((_resolve, reject) => {
					options.signal?.addEventListener(
						'abort',
						() => reject(new DOMException('stream aborted', 'AbortError')),
						{ once: true }
					);
				});
			},
			async readRun(runId) {
				const row = await this.readConversation(runId);
				const detail: RunDetail = { ...row, steps: [] };
				return detail;
			},
			async readApproval() {
				throw new ApiError(404, 'not_found', 'no approval records in offline mode');
			},
			async decideApproval() {
				throw new ApiError(404, 'not_found', 'no approval records in offline mode');
			}
		};
	}

	/** Offline slice: one per shell instance, replaced by the live slice. */
	const memoryChatApi = createMemoryChatApi();

	/** Dock composer visibility: compact bar vs the full transcript dock. */
	let dockCollapsed = $state(false);
	let dockEl: HTMLElement | null = null;

	/** The dock's API slice: live transport once a session answers, else memory. */
	const dockChatApi = $derived<ChatApi>(live ? chatApi : memoryChatApi);

	/** The mounted dock ChatPanel instance + its mirrored history/active state. */
	let dockChatPanel = $state<ChatPanel | null>(null);
	let drawerConversations = $state<readonly ConversationView[]>([]);
	let drawerActiveRunId = $state<string | null>(null);
	let drawerCollapsed = $state(false);

	/**
	 * The dock's explicit panel context — a server scope following the
	 * launcher's visible selection (read at submit time, never a hidden
	 * global), the composed bag (api slice + server options + services for
	 * the autocomplete), and an inert cleanup registry (the dock never
	 * unmounts its panel except with the shell itself).
	 */
	const dockPanel = $derived<PanelContext>({
		descriptor: {
			window_id: 'mai-bottom-chat-dock',
			panel_kind: CHAT_PANEL_KIND,
			scope: { kind: 'server', server_id: selectedServerId },
			geometry: { x: 0, y: 0, width: 0, height: 0 },
			presentation: 'normal',
			z: 0,
			title_override: null
		},
		scope: { kind: 'server', server_id: selectedServerId },
		services: {
			api: dockChatApi,
			serverOptions,
			availableServices: DOCK_AVAILABLE_SERVICES
		},
		onCleanup: () => () => {}
	});

	/** `open-chat` focuses and expands the dock instead of a second window. */
	function focusChatDock(): void {
		dockCollapsed = false;
		status = 'Chat dock focused.';
		const compose = dockEl?.querySelector('textarea');
		if (compose instanceof HTMLTextAreaElement) compose.focus();
	}

	const overviewApi: OverviewApi = {
		listObservations: api.listObservations,
		listFindings: api.listFindings,
		evaluateFindings: api.evaluateFindings,
		listNotifications: api.listNotifications,
		ackNotification: api.ackNotification,
		listAdoptedApplications: api.listAdoptedApplications,
		listRuns: api.listRuns,
		streamRunEvents: browserRunEventStream
	};

	/** Model-settings slice: the client's model methods under panel names. */
	const modelSettingsApi: ModelSettingsApi = {
		listConnections: api.listConnections,
		readConnection: api.readConnection,
		putConnection: api.putConnection,
		deleteConnection: api.deleteConnection,
		listProfiles: api.listModelProfiles,
		readProfile: api.readModelProfile,
		putProfile: api.putModelProfile,
		deleteProfile: api.deleteModelProfile,
		readSelection: api.readSelection,
		putSelection: api.putSelection,
		deleteSelection: api.deleteSelection,
		probe: api.probeModel
	};

	const appsApi: ApplicationsApi = {
		listAdopted: api.listAdoptedApplications,
		discover: api.discoverApplications,
		adopt: api.adoptApplication,
		revoke: api.revokeApplication
	};

	const pluginsApi: PluginsApi = {
		catalog: api.pluginCatalog,
		readConfig: api.readPluginConfig,
		editConfig: api.editPluginConfig,
		removeConfig: api.removePluginConfig,
		readSchema: api.readPluginSchema,
		listBindings: api.listPluginBindings,
		editBinding: api.editPluginBinding,
		removeBinding: api.removePluginBinding,
		runDiscovery: api.runPluginDiscovery
	};

	const skillsApi: SkillsApi = { catalog: api.skillCatalog, readSkill: api.readSkill };

	/** Open one server-scoped panel explicitly pinned to one target. */
	function openServerPanel(kind: PanelKind, serverId: ServerId): void {
		openPanel(kind, { kind: 'server', server_id: serverId });
	}

	/** Open the documents panel pinned to one stored document object. */
	function openDocumentObject(serverId: ServerId, docId: string): void {
		openPanel(DOCS_PANEL_KIND, {
			kind: 'server',
			server_id: serverId,
			object: { kind: 'document', id: docId }
		});
	}

	/**
	 * The registry's services bag is deliberately an open record (ADR §7.2:
	 * explicit context, never globals); the frozen feature bags satisfy it at
	 * runtime — this is the single widening point for all of them.
	 */
	function asBag(services: object): PanelServices {
		return services as PanelServices;
	}

	/**
	 * The services bag of ONE live window: every feature panel receives the
	 * exact slice its entry contract declares. Cross-panel seams are real
	 * openers into this shell; a seam with no windowable surface stays
	 * `null` so the panel renders its honest fail-closed note (never a fake
	 * navigation).
	 */
	function servicesFor(descriptor: WindowDescriptor): PanelServices {
		switch (descriptor.panel_kind) {
			case SERVER_PROFILE_PANEL_KIND:
				return asBag(serverProfileServices(api));
			case CAPABILITY_POLICY_PANEL_KIND:
				return asBag(capabilityPolicyServices(api));
			case EXECUTION_PANEL_KIND:
				return asBag(executionServices(api));
			case JOBS_PANEL_KIND:
				return asBag(jobsServices(jobsApi));
			case CHAT_PANEL_KIND:
				return asBag(chatServices(chatApi));
			case MODEL_SETTINGS_PANEL_KIND:
				return asBag(modelSettingsServices(modelSettingsApi));
			case DOCS_PANEL_KIND:
				return asBag(docsServices(api, { submitMessage: api.submitMessage }));
			case APPLICATIONS_PANEL_KIND: {
				const serverId =
					descriptor.scope.kind === 'server' ? descriptor.scope.server_id : selectedServerId;
				return asBag(
					applicationsServices(appsApi, null, {
						openDocument: (docId) => openDocumentObject(serverId, docId)
					})
				);
			}
			case OVERVIEW_PANEL_KIND:
				return asBag(
					overviewServices(
						overviewApi,
						{
							openApplications: (serverId) =>
								openServerPanel(APPLICATIONS_PANEL_KIND, serverId),
							openJobs: (serverId) => openServerPanel(JOBS_PANEL_KIND, serverId)
						},
						{
							attach: (payload: EvidenceAttachPayload) =>
								openServerPanel(CHAT_PANEL_KIND, payload.server_id)
						}
					)
				);
			case KNOWLEDGE_MODELS_PANEL_KIND:
				return asBag(
					knowledgeModelsServices(api, {
						openApplications: (serverId) => openServerPanel(APPLICATIONS_PANEL_KIND, serverId),
						openJobs: (serverId) => openServerPanel(JOBS_PANEL_KIND, serverId),
						openModelSettings: () => openPanel(MODEL_SETTINGS_PANEL_KIND, { kind: 'global' })
					})
				);
			case PLUGIN_FORMS_PANEL_KIND:
				return asBag(pluginFormsServices(pluginsApi, skillsApi));
			default:
				return {};
		}
	}

	/**
	 * Open geometry: the entry default when declared, else the shell
	 * default with a small cascade. The position is clamped with the
	 * produced NT3 clamp so a new window always keeps a visible title-bar
	 * area inside the viewport (measured against the desktop region's
	 * origin, exactly like a drag gesture). No clamping logic is written
	 * here — only the produced function is reused.
	 */
	function placeGeometry(entry: PanelRegistryEntry): WindowGeometry {
		const base = entry.default_geometry ?? {
			...SHELL_DEFAULT_GEOMETRY,
			x: SHELL_DEFAULT_GEOMETRY.x + (cascade % 6) * 32,
			y: SHELL_DEFAULT_GEOMETRY.y + (cascade % 6) * 24
		};
		cascade += 1;
		const rect = desktopEl?.getBoundingClientRect();
		if (!rect) return base;
		return clampDraggedGeometry(
			base,
			{ width: window.innerWidth, height: window.innerHeight },
			{ left: rect.left, top: rect.top, barWidth: base.width }
		);
	}

	/**
	 * Launcher open: registry scope check first (the composed open path),
	 * then the validated state transition. The scope is built from the
	 * entry's declared scope types and the CURRENT selection — one explicit
	 * target per open, never a shared mutable target.
	 */
	function openPanel(kind: PanelKind, scopeOverride?: WindowScope): void {
		const entry = registry.entry(kind);
		if (!entry) return;
		const scope: WindowScope =
			scopeOverride ??
			(entry.scope_types.has('server')
				? { kind: 'server', server_id: selectedServerId }
				: { kind: 'global' });
		const scopeCheck = registry.checkScope(kind, scope);
		if (!scopeCheck.ok) {
			status = `Cannot open ${entry.title}: ${scopeCheck.issue.message}`;
			return;
		}
		const outcome = workspace.open({ panel_kind: kind, scope, geometry: placeGeometry(entry) });
		if (!outcome.ok) {
			status = `Cannot open ${entry.title}: ${outcome.issue.message}`;
			return;
		}
		if (outcome.created) {
			openOrder.set(outcome.descriptor.window_id, openSeq);
			openSeq += 1;
		}
		const target = scope.kind === 'server' ? serverLabel(scope.server_id) : 'Global';
		status = `Opened ${entry.title} — ${target}.`;
	}

	function closeWindow(windowId: WindowId): void {
		workspace.close(windowId);
		openOrder.delete(windowId);
	}

	function contextLabelFor(descriptor: WindowDescriptor): string {
		return descriptor.scope.kind === 'server' ? serverLabel(descriptor.scope.server_id) : '';
	}

	/**
	 * Taskbar context label: the pinned server's label, or the literal
	 * "Global" for a global scope (ADR §6 taskbar naming).
	 */
	function switcherContextFor(descriptor: WindowDescriptor): string {
		return descriptor.scope.kind === 'server'
			? serverLabel(descriptor.scope.server_id)
			: 'Global';
	}

	/**
	 * Single geometry commit path for the frame's drag/resize adapters. A
	 * maximized frame's bounds belong to the presentation overlay (ADR
	 * §7.2): dragging its title bar is not a restore gesture (no
	 * snapping/tiling exists) and must not silently rewrite the geometry the
	 * frame returns to on restore.
	 */
	function commitGeometry(windowId: WindowId, geometry: WindowGeometry): void {
		const live = snapshot.windows.find((win) => win.window_id === windowId);
		if (!live || live.presentation !== 'normal') return;
		workspace.move(windowId, geometry);
	}

	/** Launcher-safe notice for one restore run — codes only, never data. */
	function restoreNotice(report: RestoredLayoutReport): string | null {
		// A whole-document discard reports BOTH discarded and empty — the
		// discard reason must win over the quiet "nothing stored" case.
		if (report.discarded !== null) {
			return `Stored workspace layout was discarded (${report.discarded}).`;
		}
		if (report.skipped || report.empty) return null;
		const restored = report.restored.length;
		const dropped = report.dropped.length;
		const suffix =
			dropped > 0 ? `; dropped ${dropped} stored window${dropped === 1 ? '' : 's'}` : '';
		return `Restored ${restored} window${restored === 1 ? '' : 's'}${suffix}.`;
	}

	/**
	 * Mount path (browser only — $effect never runs during SSR): restore the
	 * stored layout once, re-key the stable DOM order from the restored
	 * stacking order (windows opened later append after them), surface a
	 * notice when a restore dropped or discarded anything, then persist
	 * every effective transition until the shell unmounts. The effect reads
	 * no reactive state, so it runs exactly once per mount.
	 */
	$effect(() => {
		const report = persistence.restore();
		let seq = 0;
		for (const win of workspace.snapshot().windows) {
			openOrder.set(win.window_id, seq);
			seq += 1;
		}
		openSeq = seq;
		status = restoreNotice(report);
		return persistence.start();
	});

	/** Viewport listener; detached again when the shell unmounts. */
	$effect(() => responsive.start());

	/**
	 * Live attach (browser only): load the enrolled servers ONLY when the
	 * readable `mai_csrf` cookie proves a logged-in browser — a fixture-only
	 * browser (no backend, Playwright) performs zero API requests.
	 */
	$effect(() => {
		if (hasSessionCookie()) void connectLive();
	});
</script>

<div class="mai-desktop-shell">
	<SideDrawer bind:collapsed={drawerCollapsed} data-testid="side-drawer" aria-label="Workspace navigation">
		{#snippet header()}
			<div class="mai-desktop-shell__drawer-brand">
				<span class="mai-desktop-shell__drawer-sigil" aria-hidden="true">⚙</span>
				<h1 class="mai-desktop-shell__drawer-title">Machina</h1>
				<span class="mai-desktop-shell__drawer-subtitle">M.A.I. — Machina Anima Intelligentia</span>
			</div>
		{/snippet}

		<ChatDrawerSection
			conversations={drawerConversations}
			activeRunId={drawerActiveRunId}
			onnewchat={() => {
				focusChatDock();
				dockChatPanel?.triggerNewChat();
			}}
			onselectconversation={(runId) => {
				focusChatDock();
				dockChatPanel?.triggerSelectRun(runId);
			}}
			onrefreshhistory={() => dockChatPanel?.triggerRefreshHistory()}
		/>

		<ServerTreeNav
			servers={serverOptions}
			bind:selectedServerId
			entries={live ? liveEntries : fixtureEntries}
			{live}
			onopenpanel={(kind, serverId) => {
				if (serverId) {
					openPanel(kind, { kind: 'server', server_id: serverId });
				} else {
					openPanel(kind);
				}
			}}
			onfocuschat={(serverId) => {
				selectedServerId = serverId;
				focusChatDock();
			}}
		/>

		{#if live}
			<Button
				size="sm"
				variant="secondary"
				data-testid="desktop-enroll-toggle"
				onclick={() => (enrollOpen = !enrollOpen)}
			>
				{enrollOpen ? 'Close enrollment' : 'Enroll server'}
			</Button>
		{:else}
			<a class="mai-desktop-shell__signin" href="/login" data-testid="desktop-sign-in">
				Connect live session / Sign in
			</a>
		{/if}

		{#snippet footer()}
			<p class="mai-desktop-shell__status" role="status" data-testid="desktop-status">{status}</p>
			{#if live}
				<Button size="sm" variant="secondary" data-testid="desktop-sign-out" onclick={signOut}>
					Sign out
				</Button>
			{/if}
		{/snippet}
	</SideDrawer>

	<div class="mai-desktop-shell__stage">
		{#if live && enrollOpen}
			<!-- Inline enrollment: one reviewed create call; the field set mirrors
			     the frozen ProfileCreate document (C02 rule 1) exactly. -->
			<form class="mai-desktop-shell__enroll" onsubmit={enrollServer} data-testid="enroll-server">
				<Field label="Display name" bind:value={enrollName} required />
				<Field
					label="Prompt revision"
					inputmode="numeric"
					bind:value={enrollPromptRevision}
					required
				/>
				<Field label="SSH host" bind:value={enrollHost} required />
				<Field label="SSH port" inputmode="numeric" bind:value={enrollPort} required />
				<Field label="SSH user" bind:value={enrollUser} required />
				<Field label="Host key (SHA256 fingerprint)" bind:value={enrollHostKey} required />
				<Field label="Credential reference" bind:value={enrollCredentialRef} required />
				<Field label="Capabilities (comma-separated)" bind:value={enrollCapabilities} />
				<Select
					id="mai-enroll-privilege"
					label="Privilege mode"
					bind:value={enrollPrivilege}
				>
					<option value="none">none</option>
					<option value="sudo">sudo</option>
					<option value="root">root</option>
				</Select>
				{#if enrollError}
					<p class="mai-desktop-shell__enroll-error" role="alert" data-testid="enroll-error">
						{enrollError}
					</p>
				{/if}
				<Button type="submit" loading={enrollBusy} data-testid="enroll-submit">Enroll</Button>
			</form>
		{/if}

		<div class="mai-desktop-shell__desktop" bind:this={desktopEl} data-testid="desktop">
		<!-- Frame intents map 1:1 onto the state transitions (focus/close/move).
		     The drag geometry intent ignores an unknown id deliberately: a window
		     closed mid-gesture is gone, the state layer holds the committed base.
		     PanelHost failures are observability only; raw errors never reach the
		     DOM (ADR §7.2). -->
		{#each renderOrder as win (win.window_id)}
			<WindowFrame
				descriptor={win}
				title={registry.entry(win.panel_kind)?.title ?? win.panel_kind}
				contextLabel={contextLabelFor(win)}
				focused={snapshot.focused_id === win.window_id}
				onactivate={(windowId) => presentation.focus(windowId)}
				onminimize={(windowId) => presentation.minimize(windowId)}
				ontogglemaximize={(windowId) => presentation.toggleMaximize(windowId)}
				onclose={closeWindow}
				minGeometry={registry.entry(win.panel_kind)?.min_geometry ?? undefined}
				class={win.presentation === 'maximized' ? 'mai-desktop-shell__frame--maximized' : ''}
				ongeometry={commitGeometry}
			>
				<PanelHost
					registry={registry}
					descriptor={win}
					services={servicesFor(win)}
					onpanelerror={(windowId, panelKind) =>
						console.error(`[mai-desktop] panel failed: kind=${panelKind} window=${windowId}`)}
				/>
			</WindowFrame>
		{/each}
	</div>

	<!-- The Always-Visible Bottom Chat Dock: ChatPanel lives here
	     persistently — never a registry window (no data-panel-kind, no
	     workspace state). Collapsing hides the body visually so the mounted
	     panel (and its draft) survives; the fixture desktop drives it with
	     the in-memory slice and performs zero API requests. -->
	<section
		class="mai-desktop-shell__dock"
		class:mai-desktop-shell__dock--collapsed={dockCollapsed}
		data-testid="bottom-chat-dock"
		aria-label="Chat dock"
		bind:this={dockEl}
	>
		<div class="mai-desktop-shell__dock-bar">
			<span class="mai-desktop-shell__dock-title">⚙ Odyssey Noosphere Chat</span>
			<Button
				size="sm"
				pill
				variant="ghost"
				tabindex={2}
				onclick={() => (dockCollapsed = !dockCollapsed)}
				aria-expanded={!dockCollapsed}
				data-testid="bottom-chat-dock-toggle"
			>
				{dockCollapsed ? 'Expand chat dock' : 'Collapse chat dock'}
			</Button>
		</div>
		<div class="mai-desktop-shell__dock-body">
			<ChatPanel
				bind:this={dockChatPanel}
				panel={dockPanel}
				onhistorychange={(rows) => (drawerConversations = rows)}
				onactivechange={(runId) => (drawerActiveRunId = runId)}
			/>
		</div>
	</section>

	<!-- PT4 taskbar: entries persist for hidden windows and disappear on
	     close; a visible entry focuses, a hidden one restores+focuses. -->
	<WindowSwitcher
		entries={switcherEntries}
		onactivate={(windowId) => {
			dockChatPanel?.scrollComposerIntoView();
			presentation.activate(windowId);
		}}
	/>
	</div>
</div>

<style>
	.mai-desktop-shell {
		position: relative;
		flex: 1 1 0%;
		min-height: 0;
		display: flex;
		flex-direction: row;
		align-items: stretch;
	}

	.mai-desktop-shell__stage {
		position: relative;
		flex: 1 1 0%;
		min-width: 0;
		min-height: 0;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-space-2) var(--mai-space-3) var(--mai-space-3);
	}

	.mai-desktop-shell__drawer-brand {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}

	.mai-desktop-shell__drawer-sigil {
		font-size: var(--mai-text-lg);
		color: var(--mai-color-brass);
		line-height: 1;
	}

	.mai-desktop-shell__drawer-title {
		margin: 0;
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--mai-color-text);
	}

	.mai-desktop-shell__drawer-subtitle {
		font-family: var(--mai-font-mono);
		font-size: 0.68rem;
		color: var(--mai-color-text-muted);
	}

	.mai-desktop-shell__status {
		margin-left: auto;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
		white-space: nowrap;
	}

	/* The live-session hand-off link shown next to the fixture launcher. */
	.mai-desktop-shell__signin {
		padding: var(--mai-space-1) var(--mai-space-3);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		color: var(--mai-color-brass);
		border: var(--mai-border-width) solid var(--mai-color-brass-dim);
		border-radius: var(--mai-radius-pill);
		text-decoration: none;
	}

	.mai-desktop-shell__signin:hover {
		background: var(--mai-color-bg);
		border-color: var(--mai-color-brass);
	}

	/* Inline server enrollment (live mode only): a compact field grid. */
	.mai-desktop-shell__enroll {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
		gap: var(--mai-space-2);
		align-items: end;
		padding: var(--mai-card-pad, var(--mai-space-3));
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-lg);
		z-index: 15;
	}

	.mai-desktop-shell__enroll-error {
		margin: 0;
		grid-column: 1 / -1;
		font-size: var(--mai-text-sm);
		color: var(--mai-status-danger-text);
	}

	/* Transparent spatial line-grid canvas: no solid/overlay fill; only
	   floating windows, the SideDrawer, and the Chat Dock render solid. */
	.mai-desktop-shell__desktop {
		position: relative;
		flex: 1 1 0%;
		min-height: 360px;
		background: transparent;
		border: none;
		box-shadow: none;
	}

	/* Odyssey-Style Floating Center-Bottom Chat Pill: floats above the bottom
	   of the spatial canvas without pushing the document height. */
	.mai-desktop-shell__dock {
		position: absolute;
		bottom: var(--mai-space-2);
		left: calc(100% - var(--mai-space-3) - min(860px, calc(100% - 2 * var(--mai-space-4))));
		transform: none;
		width: min(860px, calc(100% - 2 * var(--mai-space-4)));
		z-index: 30;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		padding: var(--mai-hud-pad-y) var(--mai-hud-pad-x);
		background: var(--mai-surface-glass, var(--mai-color-surface));
		backdrop-filter: var(--mai-blur-glass);
		-webkit-backdrop-filter: var(--mai-blur-glass);
		border: var(--mai-border-width) solid var(--mai-color-border-strong);
		border-radius: var(--mai-radius-2xl, var(--mai-radius-lg));
		box-shadow: var(--mai-shadow-float);
	}

	.mai-desktop-shell__dock-bar {
		position: absolute;
		top: calc(-1 * var(--mai-space-4));
		right: var(--mai-space-2);
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--mai-space-2);
		width: auto;
		padding: 2px var(--mai-space-2);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border-strong);
		border-radius: var(--mai-radius-pill);
		box-shadow: var(--mai-shadow-float);
		z-index: 31;
	}

	.mai-desktop-shell__dock-title {
		font-family: var(--mai-font-display);
		font-size: 0.65rem;
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-brass);
	}

	.mai-desktop-shell__dock-body {
		overflow: visible;
	}

	.mai-desktop-shell__dock--collapsed .mai-desktop-shell__dock-body {
		display: none;
	}

	/* PT4 maximize overlay — owned by this composition (the presentation
	   card's recorded limit): a maximized frame keeps its committed geometry
	   inline in the descriptor and the overlay is CSS drawn over it, so
	   restore cannot lose geometry by construction. `!important` is required
	   to beat the frame's inline geometry styles. */
	:global(.mai-desktop-shell__frame--maximized) {
		left: 0 !important;
		top: 0 !important;
		width: 100% !important;
		height: 100% !important;
		border-radius: 0;
	}

	/* Fixture panels are raw-DOM clients, so their styles are global but
	   namespaced and consumed only inside PanelHost bodies. */
	:global(.mai-fixture-panel) {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		font-family: var(--mai-font-sans);
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text);
	}

	:global(.mai-fixture-panel__banner) {
		font-weight: var(--mai-weight-medium);
	}

	:global(.mai-fixture-panel__target) {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
		overflow-wrap: anywhere;
	}

	:global(.mai-fixture-panel__interactions) {
		align-self: flex-start;
		padding: var(--mai-space-1) var(--mai-space-3);
		font-size: var(--mai-text-sm);
		color: var(--mai-color-primary-contrast);
		cursor: pointer;
		background: var(--mai-color-primary);
		border: var(--mai-border-width) solid var(--mai-color-primary);
		border-radius: var(--mai-radius-sm);
	}

	:global(.mai-fixture-panel__interactions:hover) {
		background: var(--mai-color-primary-hover);
	}

	:global(.mai-fixture-panel__interactions:focus-visible) {
		outline: var(--mai-focus-ring-width) solid var(--mai-focus-ring-color);
		outline-offset: var(--mai-focus-ring-offset);
	}
</style>
