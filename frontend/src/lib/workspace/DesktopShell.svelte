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
	import { createRawSnippet, untrack } from 'svelte';
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
	import { docsEntry } from './entries/07-web-docs.js';
	import { executionEntry } from './entries/05-web-execution.js';
	import { jobsEntry } from './entries/05-web-jobs.js';
	import { knowledgeModelsEntry } from './entries/11-web-memory-models.js';
	import { modelSettingsEntry } from './entries/06-web-models.js';
	import { overviewEntry } from './entries/10-web-dashboard.js';
	import { pluginFormsEntry } from './entries/09-web-plugin-forms.js';
	import { pluginsEntry } from './entries/12-web-plugins.js';
	import { runbooksEntry } from './entries/13-web-runbooks.js';
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
			sessionChatEntry(),
			modelSettingsEntry(),
			docsEntry(),
			applicationsEntry(),
			overviewEntry(),
			pluginFormsEntry(),
			pluginsEntry(),
			runbooksEntry(),
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
	import { goto } from '$app/navigation';
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
	import SessionChatPanel from '$lib/panels/SessionChatPanel.svelte';
	import Button from '../ui/Button.svelte';
	import GlassSurface from '$lib/ui/GlassSurface.svelte';
	import SideDrawer from '$lib/ui/SideDrawer.svelte';
	// Guided enrollment (so-03): the focused extraction of the old inline form.
	import ServerEnrollment from './ServerEnrollment.svelte';
	import ServerTreeNav from './ServerTreeNav.svelte';
	import ChatDrawerSection, { type ChatHistoryRow } from './ChatDrawerSection.svelte';
	// Live transport + the per-panel service slices (each feature entry names
	// its exact contract; the shell maps ONE client onto all of them).
	// Live transport: one client per shell instance (ADR §5 — instances share
	// nothing). The feature entries name the per-panel contracts; the slices
	// below map this ONE client onto each panel's exact interface.
	import { ApiError, createMaiApiClient, CSRF_COOKIE, type ProfileResponse, type TagPage } from '$lib/api/client.js';
	import type { TagPageLoader } from '$lib/ui/HistoryTags.svelte';
	import { APPLICATIONS_PANEL_KIND, applicationsServices, type ApplicationsApi } from './entries/08-web-apps.js';
	import { CAPABILITY_POLICY_PANEL_KIND, capabilityPolicyServices } from './entries/03-web-policy.js';
	import {
		SESSION_CHAT_PANEL_KIND as CHAT_PANEL_KIND,
		sessionChatEntry,
		sessionChatServices,
		type SessionChatApi,
		type SessionChatServiceOption,
		type SessionChatTagApi
	} from './entries/06-session-chat.js';
	import {
		createSessionChannelController,
		type SessionChannelConnectionState,
		type SessionChannelController,
		type SessionStreamDraft
	} from '../chat/session_client.js';
	import type { ChannelEntry } from '../chat/session_client.js';
	import type { SessionRunState } from '$lib/api/client.js';
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
	import {
		PLUGINS_PANEL_KIND,
		pluginsPanelServices
	} from './entries/12-web-plugins.js';
	import {
		RUNBOOKS_PANEL_KIND,
		runbookCatalogServices
	} from './entries/13-web-runbooks.js';
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
	 * Selection persistence (W2): every live selection change — tree nav,
	 * dropdown, chat-dock focus, post-enrollment hand-off — persists through
	 * the same controller that owns the window layout (one lifecycle). The
	 * guard never persists fixture ids and never writes during initial
	 * defaults or while the live list is absent; `connectLive`'s restore
	 * lands on the same value the storage already holds, so the write-back
	 * is an idempotent repair, not a clobber.
	 */
	$effect(() => {
		if (
			live &&
			liveServers.some((profile) => String(profile.server_id) === selectedServerId)
		) {
			persistence.setSelectedServerId(selectedServerId);
		}
	});

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

	/** Enrollment drawer visibility (live mode only); the guided flow itself
	 * lives in ServerEnrollment and remounts fresh on every open. */
	let enrollOpen = $state(false);
	/** True when opening enrollment collapsed a pocket-overlay drawer, so
	 * closing can restore the user's own drawer state. */
	let enrollCollapsedDrawer = $state(false);

	/**
	 * Pocket viewports (≤640px) render an EXPANDED SideDrawer as an overlay
	 * above the stage (SideDrawer R-24c), which would hide the guided
	 * enrollment card entirely. Opening enrollment collapses it; closing
	 * restores it. Desktop widths never overlay, so nothing changes there.
	 */
	function pocketViewport(): boolean {
		return (
			typeof window !== 'undefined' &&
			window.matchMedia('(max-width: 640px)').matches
		);
	}

	function toggleEnroll(): void {
		if (!enrollOpen && pocketViewport() && !drawerCollapsed) {
			drawerCollapsed = true;
			enrollCollapsedDrawer = true;
		} else if (enrollOpen && enrollCollapsedDrawer) {
			drawerCollapsed = false;
			enrollCollapsedDrawer = false;
		}
		enrollOpen = !enrollOpen;
	}

	/** Close enrollment, restoring a pocket drawer this flow collapsed. */
	function closeEnrollment(): void {
		if (enrollCollapsedDrawer) {
			drawerCollapsed = false;
			enrollCollapsedDrawer = false;
		}
		enrollOpen = false;
	}

	/** Default capability set offered to a newly enrolled server. */
	const ENROLL_CAPABILITIES: string[] = [
		'app.adopt',
		'app.delete_map_entry',
		'app.discover',
		'app.install',
		'app.list_adopted',
		'app.remove',
		'app.unadopt',
		'app.update',
		'app.upsert_map_entry',
		'session.read',
		'docs.retrieve',
		'host.logs',
		'host.shell',
		'memory.recall',
		'memory.reflect',
		'memory.retain',
		'service.http',
		'web.read',
		'web.search'
	];

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

	/**
	 * Offline fixture harnesses run against `vite preview` on `127.0.0.1:4173` /
	 * `localhost:4173` with no backend. Any other host/port (or when a
	 * `mai_csrf` cookie is already present) is a live deployment and must
	 * authenticate against the API and redirect to `/login` when unauthorized.
	 */
	function isOfflineFixtureHost(): boolean {
		if (typeof window === 'undefined') return true;
		return (
			(window.location.hostname === '127.0.0.1' || window.location.hostname === 'localhost') &&
			window.location.port === '4173'
		);
	}

	/** Load the enrolled servers; a 401 redirects to `/login` on live hosts. */
	async function connectLive(): Promise<void> {
		try {
			const { servers } = await api.listProfiles();
			liveServers = servers;
			live = true;
			status = null;
			if (servers.length > 0) {
				// Restore the stored choice ONLY when it still names an enrolled
				// server; anything else (absent, deleted, stale, invalid) falls
				// back to the first enrolled server and repairs the stored value.
				const stored = persistence.getSelectedServerId();
				const exists =
					stored !== null &&
					servers.some((profile) => String(profile.server_id) === stored);
				selectedServerId = exists
					? stored
					: String(servers[0].server_id);
				if (!exists) persistence.setSelectedServerId(selectedServerId);
			}
			// The session history loads under the RESOLVED server scope (an
			// earlier call here filtered against the fixture default server).
			void loadSessionHistory();
			// Refresh continuity (R2-3): restore the active session pointer.
			try {
				const raw = sessionStorage.getItem(SESSION_POINTER_KEY);
				if (raw !== null) {
					const pointer = JSON.parse(raw) as {
						session_id: string;
						server_id: string | null;
					};
					if (
						typeof pointer.session_id === 'string' &&
						pointer.session_id !== ''
					) {
						openActiveSession(pointer.session_id, pointer.server_id);
					}
				}
			} catch {
				/* corrupted pointer: ignored, honesty intact */
			}
			// An empty enrolled list keeps the selection honest: no fabrication.
		} catch (error) {
			live = false;
			liveServers = [];
			if (error instanceof ApiError && (error.status === 401 || error.code === 'unauthenticated')) {
				status = 'Live session required — sign in to open the live desktop.';
				if (!isOfflineFixtureHost()) {
					await goto('/login', { replaceState: true });
				}
			} else {
				status = `Live servers unavailable${error instanceof ApiError ? ` (${error.code})` : ''}.`;
			}
		}
	}

	/** Sign out: clear the session, redirect to `/login` on live hosts. */
	async function signOut(): Promise<void> {
		try {
			await api.logout();
		} catch {
			// An already-expired session still ends in signed-out state below.
		}
		live = false;
		liveServers = [];
		selectedServerId = FIXTURE_SERVERS[0].id;
		status = 'Signed out — fixture desktop.';
		if (!isOfflineFixtureHost()) {
			await goto('/login', { replaceState: true });
		}
	}

	/**
	 * Post-enrollment hand-off from the guided flow (so-03): close the drawer,
	 * refresh the live server list and SELECT the new server — the same
	 * load/select behavior the shell already owns.
	 */
	async function handleEnrolled(serverId: string): Promise<void> {
		closeEnrollment();
		await connectLive();
		selectedServerId = serverId;
		const label = liveServers.find((profile) => String(profile.server_id) === serverId)?.name;
		status = `Server enrolled: ${label ?? serverId}.`;
	}

	// --- Per-panel service slices (each entry's exact contract) ---------------

	/**
	 * The dock's session-first API slice (D09): the typed
	 * /api/v1/agent-sessions transport ONLY — the legacy chat/task/
	 * objective vocabulary died with the coordinated deletion. Offline the
	 * bag carries no methods: the panel's structural narrowing fails
	 * closed and renders the honest not-wired state (zero requests).
	 */
	const dockSessionApi = $derived<SessionChatApi>(
		live
			? {
					createAgentSession: api.createAgentSession,
					listAgentSessions: api.listAgentSessions,
					readAgentSession: api.readAgentSession,
					renameAgentSession: api.renameAgentSession,
					listAgentSessionEntries: api.listAgentSessionEntries,
					submitAgentSessionTurn: api.submitAgentSessionTurn,
					submitAgentSessionControl: api.submitAgentSessionControl
				}
			: ({} as SessionChatApi)
	);

	/**
	 * The OPTIONAL history-tag slice (history-tags W6B/W7A, session-first
	 * routing D07): the REAL client's tag methods under the bag's `tags`
	 * field — session_id rows read the agent-session tag routes, canonical
	 * conversation rows the conversation routes, legacy run rows the job
	 * routes (the catalog is global; every assignment carries its explicit
	 * server scope). The OFFLINE memory dock omits it and fails closed.
	 */
	const chatTagApi: SessionChatTagApi = {
		listTags: api.listTags,
		createTag: api.createTag,
		listRunTags: api.listRunTags,
		assignRunTag: api.assignRunTag,
		removeRunTag: api.removeRunTag,
		listAgentSessionTags: api.listAgentSessionTags,
		assignAgentSessionTag: api.assignAgentSessionTag,
		removeAgentSessionTag: api.removeAgentSessionTag
	};

	/**
	 * One drawer row's bounded tag-page adapter (history-tags W7A; D09
	 * session-first): session_id rows read the agent-session tag route;
	 * legacy run rows read the retained generic job tag route. The scope is
	 * always the ROW's recorded `server_id` — never a hidden global. A row
	 * without an adapter method stays honestly preview-only.
	 */
	function chatRowTagsLoader(row: ChatHistoryRow): TagPageLoader | null {
		const rowSessionId = row.session_id;
		if (typeof rowSessionId === 'string' && rowSessionId !== '') {
			const listAgentSessionTags = api.listAgentSessionTags;
			if (typeof listAgentSessionTags !== 'function') return null;
			return async (offset, limit) => {
				const page = await listAgentSessionTags(rowSessionId, row.server_id, { limit, offset });
				return {
					limit: page.limit,
					offset: page.offset,
					total: page.total,
					has_more: page.has_more,
					items: (page.items ?? []).map((one) => ({ tag_id: one.tag_id, name: one.name }))
				} as TagPage;
			};
		}
		const listRunTags = api.listRunTags;
		if (typeof listRunTags !== 'function') return null;
		return async (offset, limit) => {
			const page = await listRunTags(row.run_id, row.server_id, { limit, offset });
			return {
				limit: page.limit,
				offset: page.offset,
				total: page.total,
				has_more: page.has_more,
				items: (page.items ?? []).map((one) => ({ tag_id: one.tag_id, name: one.name }))
			} as TagPage;
		};
	}

	const jobsApi: JobsApi = {
		listRuns: api.listRuns,
		readRun: api.readRun,
		cancelRun: api.cancelRun,
		listSchedules: api.listSchedules,
		createSchedule: api.createSchedule,
		updateSchedule: api.updateSchedule,
		deleteSchedule: api.deleteSchedule,
		streamRunEvents: browserRunEventStream,
		// W3 narrow name seam: resolve ONLY the pinned panel scope's enrolled
		// profile name. Failure keeps the panel on the UUID (nonfatal there).
		readServerName: async (serverId) => (await api.readProfile(serverId)).name
	};

	// ---------------------------------------------------------------------
	// Always-Visible Bottom Chat Dock (mui-03; D09): SessionChatPanel mounts
	// persistently at the bottom of the viewport — NOT as a registry window
	// (the dock element carries no data-panel-kind and never enters the
	// workspace state). The fixture desktop must perform ZERO API requests:
	// offline the bag carries no session methods, so the panel's structural
	// narrowing fails closed and renders the honest not-wired state.
	// ---------------------------------------------------------------------

	// SCOPE-3: the dock's @service options are the REAL same-server Service
	// Map page (ApplicationsApi.listAdopted, bounded envelope). No fixtures:
	// loading/empty/error are honest states; "Load more" continues the page.
	let dockServices = $state<readonly SessionChatServiceOption[]>([]);
	let dockServicesLoading = $state(false);
	let dockServicesError = $state<string | null>(null);
	let dockServicesHasMore = $state(false);
	let dockServicesOffset = $state(0);
	const DOCK_SERVICES_PAGE_LIMIT = 10;

	async function loadDockServices(reset: boolean): Promise<void> {
		if (!live || dockServicesLoading) return;
		// General Consult has no per-server Service Map: an empty honest
		// state instead of a doomed request against the zero UUID.
		if (selectedServerId === '00000000-0000-0000-0000-000000000000') {
			dockServices = [];
			dockServicesError = null;
			dockServicesHasMore = false;
			dockServicesOffset = 0;
			return;
		}
		dockServicesLoading = true;
		if (reset) {
			dockServicesError = null;
			dockServicesOffset = 0;
		}
		try {
			const offset = reset ? 0 : dockServicesOffset;
			const page = await api.listAdoptedApplications(selectedServerId, {
				limit: DOCK_SERVICES_PAGE_LIMIT,
				offset
			});
			const options: SessionChatServiceOption[] = (page.applications ?? []).map(
				(application) => ({
					application_id: application.application_id,
					name: application.name,
					...(application.category ? { category: application.category } : {})
				})
			);
			dockServices = reset
				? options
				: [
						...dockServices.filter(
							(existing) =>
								!options.some(
									(next) => next.application_id === existing.application_id
								)
						),
						...options
					];
			dockServicesOffset = offset + options.length;
			dockServicesHasMore = dockServicesOffset < page.total;
		} catch (error) {
			const apiError = error as { code?: string; message?: string };
			dockServicesError =
				apiError.message ?? 'The adopted services could not be loaded.';
			if (reset) dockServices = [];
		} finally {
			dockServicesLoading = false;
		}
	}

	// The draft scope is the active/new-chat scope: reload the bounded
	// first page whenever it changes (mount path is browser-only).
	$effect(() => {
		const scope = selectedServerId;
		const isLive = live;
		untrack(() => {
			if (isLive) void loadDockServices(true);
		});
		// scope/isLive are the tracked dependencies.
		void scope;
	});


	/** The centered chat column root (focus target for chat intents). */
	let dockEl: HTMLElement | null = null;
	/**
	 * CHC H1: the column's blended CARD (`.mai-desktop-shell__dock-body`)
	 * — the surface that hugs the composer in hero and grows to full
	 * column height in active.
	 */
	let dockBodyEl: HTMLElement | null = null;
	let drawerCollapsed = $state(false);

	// --- Session-first drawer history (D07; F4 pagination, F3 channel) -------
	/** The bounded first page of session-first rows (newest-change first). */
	let drawerSessions = $state<readonly ChatHistoryRow[]>([]);
	let drawerSessionsTotal = $state<number | undefined>(undefined);
	let drawerSessionsHasMore = $state(false);
	let drawerSessionsLoading = $state(false);
	let drawerSessionsLoadingMore = $state(false);
	/** The offset the NEXT session page starts at (F4 pagination cursor). */
	let drawerSessionsOffset = $state(0);
	/** Drawer page size (the reviewed bounded initial page). */
	const SESSION_HISTORY_PAGE_LIMIT = 10;
	/** The refresh-continuity pointer (opaque ids ONLY — never transcript). */
	const SESSION_POINTER_KEY = 'mai-active-session';

	/** One bounded agent-session page for the drawer (honest failure keeps
	 * the previous rows and surfaces the status line — never a fake list). */
	async function loadSessionHistory(): Promise<void> {
		if (!live || drawerSessionsLoading) return;
		drawerSessionsLoading = true;
		try {
			const page = await api.listAgentSessions({
				server_id: selectedServerId,
				limit: SESSION_HISTORY_PAGE_LIMIT,
				offset: 0
			});
			drawerSessions = (page.items ?? []).map((session) => ({
				run_id: session.session_id,
				session_id: session.session_id,
				server_id: session.server_id,
				title: session.title ?? null,
				service_tags: [],
				status: session.status,
				updated_at: session.updated_at,
				tag_preview: session.tag_preview ?? null
			}));
			drawerSessionsTotal = page.total;
			drawerSessionsHasMore = page.has_more;
			drawerSessionsOffset = drawerSessions.length;
			// Success renders in the drawer itself (rows + counts); no global
			// status write — a background refresh must never clobber a
			// concurrent action's status line. Failures still surface below.
		} catch (error) {
			status = `Session history unavailable${error instanceof ApiError ? ` (${error.code})` : ''}.`;
		} finally {
			drawerSessionsLoading = false;
		}
	}

	/** The NEXT bounded session page: deduplicated append (F4). */
	async function loadMoreSessionHistory(): Promise<void> {
		if (!live || drawerSessionsLoadingMore || !drawerSessionsHasMore) return;
		drawerSessionsLoadingMore = true;
		try {
			const page = await api.listAgentSessions({
				server_id: selectedServerId,
				limit: SESSION_HISTORY_PAGE_LIMIT,
				offset: drawerSessionsOffset
			});
			const known = new Set(
				drawerSessions.map((row) => row.session_id ?? row.run_id)
			);
			const fresh = (page.items ?? [])
				.filter((session) => !known.has(session.session_id))
				.map((session) => ({
					run_id: session.session_id,
					session_id: session.session_id,
					server_id: session.server_id,
					title: session.title ?? null,
					service_tags: [],
					status: session.status,
					updated_at: session.updated_at,
					tag_preview: session.tag_preview ?? null
				}));
			drawerSessions = [...drawerSessions, ...fresh];
			drawerSessionsOffset += (page.items ?? []).length;
			drawerSessionsTotal = page.total;
			drawerSessionsHasMore = page.has_more;
		} catch (error) {
			status = `More sessions unavailable${error instanceof ApiError ? ` (${error.code})` : ''}.`;
		} finally {
			drawerSessionsLoadingMore = false;
		}
	}

	/** The single-flight `before_seq` older-page fetch (D08 F-acceptance 1). */
	async function loadOlderEntries(): Promise<void> {
		if (!live || loadingOlderEntries || olderEntriesExhausted) return;
		if (activeSessionId === null || sessionEntries.length === 0) return;
		loadingOlderEntries = true;
		try {
			const oldestSeq = Number(sessionEntries[0].seq);
			// R08 F5: pin the ACTIVE SESSION's own server scope — not the
			// current selector — so a selector change after a pointer restore
			// degrades nothing (the fetch follows the session's server).
			const page = await api.listAgentSessionEntries(activeSessionId, {
				server_id: activeSessionServerId ?? selectedServerId,
				before_seq: oldestSeq,
				limit: 50
			});
			const known = new Set(sessionEntries.map((entry) => entry.entry_id));
			// The REST history page and the channel frames carry the SAME wire
			// JSON; the two generated views differ only cosmetically, so the
			// merge is a structural cast (verified by the seq/entry_id dedup).
			const fresh = ((page.items ?? []) as unknown as ChannelEntry[]).filter(
				(entry) => !known.has(entry.entry_id)
			);
			if (fresh.length === 0) {
				olderEntriesExhausted = true;
				return;
			}
			// R08 F1: the REST page feeds the SAME controller cache the
			// channel frames feed — the next commit MERGES with (rather than
			// replaces) the loaded older pages, mid-scroll reading holds.
			if (activeSessionChannel !== null) {
				activeSessionChannel.mergeExternalEntries(fresh);
			} else {
				const merged = [...sessionEntries, ...fresh]
					.sort((a, b) => a.seq - b.seq)
					.slice(-400);
				sessionEntries = merged;
			}
		} catch (error) {
			status = `Older entries unavailable${error instanceof ApiError ? ` (${error.code})` : ''}.`;
		} finally {
			loadingOlderEntries = false;
		}
	}

	/** Rename one session through the typed PATCH route, then refresh. */
	async function renameSession(sessionId: string, title: string): Promise<void> {
		await api.renameAgentSession(sessionId, { title });
		status = 'Session renamed.';
		await loadSessionHistory();
	}

	// --- The ONE visible session's channel (F3; plan acceptance 1/2/6) -------
	/** The active session's id — only THIS one has an open channel. */
	let activeSessionId = $state<string | null>(null);
	/** The active session's pinned server scope (R08 F5; set at open time). */
	let activeSessionServerId = $state<string | null>(null);
	/** The active session's channel controller (null = none open). */
	let activeSessionChannel = $state<SessionChannelController | null>(null);
	/** Honest connection state mirror for the dock surface. */
	let channelState = $state<SessionChannelConnectionState>('closed');
	/**
	 * The visible session's bounded entry cache (D08): the channel mirror
	 * (≤400, controller-evicted) MERGED with REST before_seq older pages —
	 * always sorted by seq, deduplicated, evicted at the same cap.
	 */
	let sessionEntries = $state<ChannelEntry[]>([]);
	/** The visible session's volatile streaming drafts (channel mirror). */
	let sessionDrafts = $state<SessionStreamDraft[]>([]);
	/**
	 * The optimistic run state of the JUST-submitted turn (S3): between
	 * the submit resolution and the first committed ``run_state`` entry,
	 * the generating indicator would otherwise have nothing to show. The
	 * derived server truth always wins once a run_state entry lands.
	 */
	let optimisticRunState = $state<{
		run_id: string;
		state: SessionRunState;
	} | null>(null);

	/**
	 * S3 optimistic turn feedback: the panel calls this right after
	 * ``submitAgentSessionTurn`` resolves with the admitted turn's
	 * ``user_message`` entry + the response's run state, so the user's
	 * message and the generating indicator appear immediately on Enter.
	 * The entry merges into the SAME bounded controller cache the channel
	 * feeds (the committed frame later reconciles by seq/entry_id).
	 */
	function handleTurnSubmitted(
		entry: ChannelEntry,
		runState: { run_id: string; state: SessionRunState }
	): void {
		if (!live) return;
		if (activeSessionChannel !== null) {
			activeSessionChannel.mergeExternalEntries([entry]);
		} else {
			const merged = [...sessionEntries, entry]
				.sort((one, two) => Number(one.seq) - Number(two.seq))
				.slice(-400);
			sessionEntries = merged;
		}
		optimisticRunState = runState;
	}
	/** Single-flight guard + exhaustion flag for older-page loads. */
	let loadingOlderEntries = $state(false);
	let olderEntriesExhausted = $state(false);

	/**
	 * Open (or switch to) the visible session's channel: the previous
	 * controller is disposed FIRST — switching sessions drains/closes the
	 * old stream and aborts nothing durable (plan acceptance 2).
	 */
	function openActiveSession(sessionId: string, serverId: string | null): void {
		if (!live) return;
		closeActiveSession();
		activeSessionId = sessionId;
		activeSessionServerId = serverId;
		channelState = 'connecting';
		optimisticRunState = null;
		// The refresh-continuity pointer (D08, R2-3): ONLY the opaque
		// session/server ids — never transcript content.
		try {
			sessionStorage.setItem(
				SESSION_POINTER_KEY,
				JSON.stringify({ session_id: sessionId, server_id: serverId })
			);
		} catch {
			/* storage unavailable: continuity degrades, honesty intact */
		}
		activeSessionChannel = createSessionChannelController({
			sessionId,
			serverId,
			handlers: {
				onConnectionState: (next) => (channelState = next),
				onCommittedEntriesChanged: (entries) => {
					// The bounded cache mirror (D08): the controller already
					// deduped + evicted to ≤400.
					sessionEntries = [...entries];
					// S3: committed server truth replaces the optimistic
					// mirror the moment any run_state entry lands.
					if (entries.some((entry) => entry.payload.kind === 'run_state')) {
						optimisticRunState = null;
					}
				},
				onStreamDraftChanged: () => {
					sessionDrafts = [...(activeSessionChannel?.streamDrafts() ?? [])];
				}
			}
		});
		activeSessionChannel.connect();
	}

	/** Switching away / New Chat: the channel closes; nothing lingers. */
	function closeActiveSession(): void {
		if (activeSessionChannel !== null) {
			activeSessionChannel.dispose();
			activeSessionChannel = null;
		}
		activeSessionId = null;
		activeSessionServerId = null;
		channelState = 'closed';
		sessionEntries = [];
		sessionDrafts = [];
		optimisticRunState = null;
		olderEntriesExhausted = false;
		try {
			sessionStorage.removeItem(SESSION_POINTER_KEY);
		} catch {
			/* storage unavailable */
		}
	}

	/** Drawer rows: the session-first history only (D09). */
	const drawerRows = $derived<readonly ChatHistoryRow[]>([...drawerSessions]);
	const drawerRowsTotal = $derived<number | undefined>(drawerSessionsTotal);
	const drawerRowsHasMore = $derived(drawerSessionsHasMore);
	/** The visible session has older server-side entries (D08 Load older). */
	const sessionHasOlder = $derived(
		!olderEntriesExhausted &&
			sessionEntries.length > 0 &&
			Number(sessionEntries[0].seq) > 1
	);

	// --- CHC H1: hero compact card ↔ active full-height growth ---------------
	/**
	 * Honest motion budget: reduced-motion users get the INSTANT layout
	 * switch — the WAAPI card growth is skipped entirely (tokens.css
	 * already compiles CSS transitions out under reduce; Web Animations
	 * are not covered by that rule, so the gate is explicit here).
	 */
	let shellMotionReduced = $state(false);
	$effect(() => {
		const query = window.matchMedia('(prefers-reduced-motion: reduce)');
		const apply = () => (shellMotionReduced = query.matches);
		apply();
		query.addEventListener('change', apply);
		return () => query.removeEventListener('change', apply);
	});

	/** The hero→active card growth budget (CHC H1). */
	const DOCK_CARD_GROWTH_MS = 240;
	/**
	 * The card's rect captured in `$effect.pre` — BEFORE the hero→active
	 * DOM flip applies, i.e. while the card still hugs the composer — so
	 * the growth can start from the exact compact geometry. `null` unless
	 * the previous rendered state was hero (a session mounted straight to
	 * active renders full-height directly: no flash, no animation).
	 */
	let dockCardFlipFrom: DOMRect | null = null;
	let dockWasHero: boolean | null = null;
	$effect.pre(() => {
		const hero = activeSessionId === null;
		if (dockWasHero === true && !hero && dockBodyEl !== null) {
			dockCardFlipFrom = dockBodyEl.getBoundingClientRect();
		}
		dockWasHero = hero;
	});

	/**
	 * After the flip settles: animate the card from the captured hero
	 * rect to its new resting rect (~240ms ease, symmetric — the card's
	 * auto block margins keep it centered while it grows). Pure-CSS
	 * `auto ↔ 100%` heights are not interpolable, hence WAAPI; the panel
	 * inside reflows per frame (its viewport is the flex:1 scroll
	 * container), so the transcript unfolds above the bottom-pinned
	 * composer as the surface grows.
	 */
	let dockCardAnimation: Animation | null = null;
	$effect(() => {
		void activeSessionId;
		const card = dockBodyEl;
		const from = dockCardFlipFrom;
		dockCardFlipFrom = null;
		if (card === null || from === null || shellMotionReduced) return;
		const to = card.getBoundingClientRect();
		if (Math.abs(to.height - from.height) < 2) return;
		dockCardAnimation?.cancel();
		dockCardAnimation = card.animate(
			[
				{ height: `${from.height}px` },
				{ height: `${to.height}px` }
			],
			{ duration: DOCK_CARD_GROWTH_MS, easing: 'ease' }
		);
	});
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
			api: dockSessionApi,
			// The optional history-tag slice rides ONLY the live transport (the
			// offline memory dock has no tag methods and fails closed).
			...(live ? { tags: chatTagApi } : {}),
			// The visible session's channel rides the bag additively (F3):
			// exactly ONE controller exists, and only while a session is open.
			...(live ? { channel: activeSessionChannel } : {}),
			serverOptions,
			// SCOPE-3: the REAL same-server Service Map page with its honest
			// bounded continuation (no fixture services anywhere).
			availableServices: dockServices,
			servicesLoading: dockServicesLoading,
			servicesError: dockServicesError,
			servicesHasMore: dockServicesHasMore,
			loadMoreServices: () => void loadDockServices(false)
		},
		onCleanup: () => () => {}
	});

	/** `open-chat` focuses the column's composer instead of a second window. */
	function focusChatDock(): void {
		status = 'Chat column focused.';
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
		revoke: api.revokeApplication,
		executeRunbook: api.executeRunbook
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
				return asBag(sessionChatServices(dockSessionApi, chatTagApi));
			case MODEL_SETTINGS_PANEL_KIND:
				return asBag(modelSettingsServices(modelSettingsApi));
			case DOCS_PANEL_KIND:
				return asBag(
					docsServices(api, {
						// Session-first attach (D07; N1): the explicit create-new-
						// session action (session_id null) or the explicit
						// destination session, then ONE idempotent turn admission
						// — and the returned session BECOMES the visible session
						// (its channel opens; the drawer refreshes). The legacy
						// submitMessage stays on the bag until D09 (COORD-D02-01).
						attachToSession: async ({ session_id, text, model_profile, client_request_id }) => {
							const scope = descriptor.scope;
							const serverId = scope.kind === 'server' ? scope.server_id : null;
							if (serverId === null) {
								throw new ApiError(400, 'bad_request', 'documents attach needs a server scope');
							}
							const target =
								session_id ??
								(
									await api.createAgentSession({
										server_id: serverId,
										source: 'document_attach',
										model_profile: model_profile ?? null,
										client_request_id
									})
								).session_id;
							const outcome = await api.submitAgentSessionTurn(
								target,
								{
									session_id: target,
									text,
									client_request_id,
									model_profile: model_profile ?? null
								},
								serverId
							);
							openActiveSession(target, serverId);
							void loadSessionHistory();
							return outcome;
						}
					})
				);
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
						openModelSettings: () => openPanel(MODEL_SETTINGS_PANEL_KIND, { kind: 'global' })
					})
				);
			case PLUGIN_FORMS_PANEL_KIND:
				return asBag(pluginFormsServices(pluginsApi, skillsApi));
			case PLUGINS_PANEL_KIND:
				return asBag(pluginsPanelServices(api));
			case RUNBOOKS_PANEL_KIND:
				return asBag(runbookCatalogServices(api));
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
	 * target per open, never a shared mutable target. Entries declaring a
	 * global scope (the Global / Fleet kinds) pin `{ kind: 'global' }`;
	 * server-only entries pin the current selection.
	 */
	function openPanel(kind: PanelKind, scopeOverride?: WindowScope): void {
		const entry = registry.entry(kind);
		if (!entry) return;
		const scope: WindowScope =
			scopeOverride ??
			(entry.scope_types.has('global')
				? { kind: 'global' }
				: { kind: 'server', server_id: selectedServerId });
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
	 * Live attach (browser only): on live hosts (or whenever a `mai_csrf`
	 * cookie is present), probe `/api/v1/servers` — a 401 redirects to
	 * `/login`. Offline fixture harnesses (`127.0.0.1:4173` without cookie)
	 * stay in fixture mode and perform zero API requests.
	 */
	$effect(() => {
		if (hasSessionCookie() || !isOfflineFixtureHost()) {
			void connectLive();
		}
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
			conversations={drawerRows}
			totalConversations={drawerRowsTotal}
			hasMoreConversations={drawerRowsHasMore}
			loadingMoreConversations={drawerSessionsLoadingMore}
			onnewchat={() => {
				// A fresh chat abandons the visible session: its channel closes
				// now, not when the next session opens (plan acceptance 2).
				closeActiveSession();
				focusChatDock();
			}}
			onselectconversation={(rowKey) => {
				// Session-first rows only (D09): open the visible session's
				// channel — the ONLY open channel (F3; never a fabricated
				// "attached" note: the dock surface mirrors the real
				// connection state).
				const row = drawerRows.find(
					(one) => (one.session_id ?? one.run_id) === rowKey
				);
				if (row !== undefined && typeof row.session_id === 'string' && row.session_id !== '') {
					openActiveSession(row.session_id, row.server_id);
				}
				focusChatDock();
			}}
			onrefreshhistory={() => {
				void loadSessionHistory();
			}}
			onloadmorehistory={() => {
				if (drawerSessionsHasMore) void loadMoreSessionHistory();
			}}
			onrenameconversation={async (rowKey, title) => {
				const row = drawerRows.find(
					(one) => (one.session_id ?? one.run_id) === rowKey
				);
				if (row !== undefined && typeof row.session_id === 'string' && row.session_id !== '') {
					await renameSession(row.session_id, title);
				}
			}}
			tagLoader={chatRowTagsLoader}
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
				onclick={toggleEnroll}
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

	<div
		class="mai-desktop-shell__stage"
		class:mai-desktop-shell__stage--enrolling={live && enrollOpen}
	>
		{#if live && enrollOpen}
			<!-- Guided enrollment (so-03): extracted focused component; remounts
			     fresh on every open, so reopening always starts cleanly. -->
			<div class="mai-desktop-shell__enroll-slot">
				<ServerEnrollment api={api} capabilities={ENROLL_CAPABILITIES} onenrolled={handleEnrolled} />
			</div>
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
				autoFitHeight={true}
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

	<!-- The Centered Chat Column (CCC S1 / CHC H1): SessionChatPanel lives
	     here persistently — never a registry window (no data-panel-kind,
	     no workspace state). The column is centered in the stage and
	     width-bounded so the spatial canvas stays visible on both sides;
	     opened windows keep stacking above it. CHC H1: the column box is
	     the transparent positioning host — the blended CARD (dock-body)
	     hugs ONLY the composer in hero (sessionId null, centered by its
	     auto block margins) and grows to the full column height when a
	     chat starts or loads; the hero→active growth is the shell's WAAPI
	     effect above (reduced motion: instant). The fixture desktop
	     drives it with the unwired bag and performs zero API requests. -->
	<section
		class="mai-desktop-shell__dock"
		class:mai-desktop-shell__dock--hero={activeSessionId === null}
		class:mai-desktop-shell__dock--reduced-motion={shellMotionReduced}
		data-testid="bottom-chat-dock"
		aria-label="Chat column"
		bind:this={dockEl}
	>
		<div class="mai-desktop-shell__dock-body" bind:this={dockBodyEl}>
			<!-- D09: the session-first virtualized surface is THE column; the
			     legacy ChatPanel fallback is deleted (no toggle). Without a
			     live session the panel renders its honest empty/create state;
			     offline it fails closed on the unwired bag. -->
			<!-- SCOPE-3: the read scope is the ACTIVE session's OWN stored
			     server (restored reloads included) — never the drawer's
			     selection; the draft/create scope stays the selection. -->
			<SessionChatPanel
				panel={dockPanel}
				sessionId={activeSessionId}
				serverId={activeSessionId === null
					? selectedServerId
					: activeSessionServerId ?? selectedServerId}
				entries={sessionEntries}
				drafts={sessionDrafts}
				channelState={channelState}
				loadingOlder={loadingOlderEntries}
				hasOlder={sessionHasOlder}
				optimisticRunState={optimisticRunState}
				onloadolder={() => void loadOlderEntries()}
				onsessionopened={(sessionId, serverId) =>
					openActiveSession(sessionId, serverId)}
				onturnsubmitted={handleTurnSubmitted}
				selectedServerId={selectedServerId}
				onserverchange={(serverId) => (selectedServerId = serverId)}
			/>
		</div>
	</section>

	<!-- PT4 taskbar: entries persist for hidden windows and disappear on
	     close; a visible entry focuses, a hidden one restores+focuses. -->
	<div class="mai-desktop-shell__taskbar">
		<WindowSwitcher
			entries={switcherEntries}
			onactivate={(windowId) => {
				focusChatDock();
				presentation.activate(windowId);
			}}
		/>
	</div>
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

	/* Guided enrollment slot (so-03): while the card is open the stage
	   switches to `--enrolling` — the desktop canvas yields a strip and the
	   card takes the stage height, scrolling internally, so every control
	   stays reachable at short viewports (360x640) without page scroll. */
	.mai-desktop-shell__enroll-slot {
		position: relative;
		z-index: 15;
		display: flex;
		flex-direction: column;
		flex: 1 1 auto;
		min-height: 0;
		min-width: 0;
		overflow-y: auto;
	}

	.mai-desktop-shell__stage--enrolling .mai-desktop-shell__desktop {
		min-height: 0;
		flex: 0 1 7rem;
	}

	/* Transparent spatial line-grid canvas: no solid/overlay fill; only
	   floating windows, the SideDrawer, and the Chat Column render solid.
	   `z-index: 20` places the window canvas ABOVE the centered chat
	   column (`z-index: 5`) so opened windows always render on top of it,
	   while `pointer-events: none` on the empty canvas lets clicks on
	   empty space pass through to the column underneath. */
	.mai-desktop-shell__desktop {
		position: relative;
		z-index: 20;
		pointer-events: none;
		flex: 1 1 0%;
		min-height: 360px;
		background: transparent;
		border: none;
		box-shadow: none;
	}

	.mai-desktop-shell__desktop > :global(.mai-window-frame) {
		pointer-events: auto;
	}

	/* The Centered Chat Column (CCC S1): absolutely centered in the stage
	   and stretched between its paddings — out of flow, so the spatial
	   canvas keeps its geometry and opened windows keep stacking ABOVE
	   the column (`z-index: 5` under the canvas's `z-index: 20`).
	   CHC H1: this box is now the TRANSPARENT positioning host — the
	   blended surface lives on the card (dock-body) below so it can hug
	   the composer in hero. `pointer-events: none` keeps empty-stage
	   clicks honest: only the card (auto) is interactive. The width is
	   DETERMINISTIC, never content-driven, and bounded so the canvas
	   stays visible on both sides; the page-padding floor keeps the
	   column inside the stage at every width. */
	.mai-desktop-shell__dock {
		position: absolute;
		top: var(--mai-space-2);
		bottom: var(--mai-space-3);
		left: 0;
		right: 0;
		margin-inline: auto;
		width: min(880px, 72%, calc(100% - 2 * var(--mai-space-3)));
		min-height: 0;
		z-index: 5;
		display: flex;
		flex-direction: column;
		padding: var(--mai-hud-pad-y) var(--mai-hud-pad-x);
		pointer-events: none;
	}

	/* Pocket viewports (CCC S1): the bounded column yields its side
	   margins and becomes the stage minus the page padding — the chat
	   owns the stage instead of a crushed sliver beside it. */
	@media (max-width: 720px) {
		.mai-desktop-shell__dock {
			width: calc(100% - 2 * var(--mai-space-3));
		}
	}

	/* CHC H1 — the blended CARD. CSP P2 surface: near-canvas solid
	   (--mai-color-surface), one quiet steel hairline, NO float shadow
	   and NO brass on the frame — no glass, no backdrop blur; the
	   Mechanicus character (typography, sigils, brass chips/buttons)
	   lives INSIDE the card. Active: `height: 100%` fills the column
	   (NOT flex-grow — the explicit height is what the shell's WAAPI
	   growth animates). Hero: `height: auto` hugs ONLY the composer and
	   the auto block margins center it in the column, so the visible
	   surface is a compact centered card — never a full-height slab. */
	.mai-desktop-shell__dock-body {
		pointer-events: auto;
		flex: 0 0 auto;
		margin-block: auto;
		height: 100%;
		min-height: 0;
		min-width: 0;
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-2xl, var(--mai-radius-lg));
		box-shadow: none;
	}
	.mai-desktop-shell__dock--hero .mai-desktop-shell__dock-body {
		height: auto;
	}

	.mai-desktop-shell__taskbar {
		position: relative;
		z-index: 35;
		align-self: flex-start;
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
