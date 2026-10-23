/**
 * Feature-owned panel entry — card `mai-08-web-apps` (P08, service `web`).
 *
 * This module is the ONE registration point of the application-inventory
 * panel (windowed-workspace ADR §7.1: a fixed trusted component map;
 * registration grants no permissions). It owns:
 *
 *  - the stable panel slug and the typed wire shapes the panel consumes
 *    (aliases of the generated `schema.generated.ts` — never restated
 *    fields, never invented endpoints);
 *  - the typed services bag the panel requires, mirroring the reviewed
 *    adoption surface (`08-app-api`, `backend/src/mai/api/applications.py`)
 *    1:1: the projected adopted page, the read-only discovery snapshot, the
 *    explicit adoption and the non-destructive revoke (C09 rules 1/6);
 *  - the composition seams for the SEPARATELY implemented lifecycle
 *    surfaces (the `app.install`/`app.update`/`app.remove` intent submits
 *    are jobs-facade features of their own cards) and for the canonical
 *    record document the `08-app-docs` producer writes (browsable in the
 *    documents panel). This panel LINKS to those actions; it never
 *    reimplements a plan/submit flow or a document store client-side
 *    (acceptance 2; C09 rule 3–5 stay producer-owned).
 *
 * The discovery and adoption resources stay SEPARATE here exactly as the
 * producer keeps them (C09 rule 1): a discovered observation is never
 * merged into the adopted page, carries no grant and enables no execution
 * — adoption is an explicit admin decision made in this panel.
 *
 * No module-level mutable state; safe to evaluate per SSR request (ADR §5
 * rule 4). Composition passes
 * `applicationsServices({ …clientApplicationsMethods }, lifecycle?, docs?)`
 * — the composed client grows the applications transport methods;
 * instances still share nothing.
 */

import type { components } from '../../api/schema.generated.js';
import type { RunbookExecutionView } from './13-web-runbooks.js';
import type { PanelEntryInput } from '../registry.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const APPLICATIONS_PANEL_KIND = 'applications';

// ---------------------------------------------------------------------------
// Generated wire shapes (schema.generated.ts; never restated fields)
// ---------------------------------------------------------------------------

/** Generated wire shape: which target source an observed application came from. */
export type ApplicationOrigin = components['schemas']['ApplicationOrigin'];
/** Generated wire shape: one explicit adoption decision (expected_revision 0 creates). */
export type AdoptBody = components['schemas']['AdoptBody'];
/** Generated wire shape: one committed adoption/revoke-shaped result + doc outcome. */
export type AdoptionCreated = components['schemas']['AdoptionCreated'];
/** Generated wire shape: one bounded adoption list page for one explicit server. */
export type ApplicationPage = components['schemas']['ApplicationPage'];
/** Generated wire shape: one projected adoption record (the managed truth). */
export type ApplicationView = components['schemas']['ApplicationView'];
/** Generated wire shape: one live discovery snapshot of one server. */
export type DiscoveryView = components['schemas']['DiscoveryView'];
/** Generated wire shape: one observed inventory record (safe view). */
export type ObservedApplicationView = components['schemas']['ObservedApplicationView'];
/** Generated wire shape: one per-lane discovery outcome (honest absence). */
export type LaneView = components['schemas']['LaneView'];
/** Generated wire shape: one observed Compose project (referenced paths only). */
export type ComposeDetailView = components['schemas']['ComposeDetailView'];
/** Generated wire shape: one observed standalone container. */
export type StandaloneDetailView = components['schemas']['StandaloneDetailView'];
/** Generated wire shape: one observed systemd unit. */
export type SystemdUnitDetailView = components['schemas']['SystemdUnitDetailView'];
/** Generated wire shape: one observed container (no env/mounts/command lines). */
export type ContainerView = components['schemas']['ContainerView'];
/** Generated wire shape: the identity-record documentation outcome of one adoption. */
export type RecordDocView = components['schemas']['RecordDocView'];
/** Generated wire shape: one revision-checked, non-destructive revoke request. */
export type RevokeBody = components['schemas']['RevokeBody'];
/** Generated wire shape: one taxonomy category (built-in or operator-defined). */
export type ServiceCategoryView = components['schemas']['ServiceCategoryView'];
/** Generated wire shape: the runbook instructions persisted on a map entry. */
export type ServiceInstructionsView = components['schemas']['ServiceInstructionsView'];

/**
 * The 8 default taxonomy categories (the confined-store grammar's built-ins).
 * Client-side fallback when a producer page/snapshot omits ``categories``
 * (old fixtures): the picker, chips and the taxonomy menu still render the
 * full vocabulary, and unmapped records fall back to ``"system"``.
 */
export interface ServiceCategorySeed {
	/** Stable machine id (the wire vocabulary value). */
	readonly machine_id: string;
	/** Human-readable label for chips and pickers. */
	readonly label: string;
	/** What belongs in this category (shown in the taxonomy menu). */
	readonly description: string;
}

/** The 8 built-in categories, in display order (frozen shared contract). */
export const DEFAULT_SERVICE_CATEGORIES: readonly ServiceCategorySeed[] = Object.freeze([
	{ machine_id: 'games', label: 'Games', description: 'Game servers, launchers and play workloads.' },
	{ machine_id: 'ai', label: 'AI & Models', description: 'LLM runtimes, model services and inference workloads.' },
	{ machine_id: 'agent', label: 'Autonomous Agents', description: 'Agent loops, bots and automated workers.' },
	{ machine_id: 'system', label: 'System & OS', description: 'OS services, daemons and host-level units.' },
	{ machine_id: 'dev', label: 'Development & CI', description: 'Build, test and continuous-integration services.' },
	{ machine_id: 'infrastructure', label: 'Infrastructure & Proxy', description: 'Proxies, databases and platform plumbing.' },
	{ machine_id: 'media', label: 'Media & Content', description: 'Content sites, streaming and media libraries.' },
	{ machine_id: 'observability', label: 'Observability & Backup', description: 'Monitoring, metrics, logs and backups.' }
]);

/** The deterministic client-side classification hints for one observed
 * service name/path/image, first match wins (fallback when the producer
 * omits ``suggested_category`` in old fixtures). */
export const CATEGORY_KEYWORD_HINTS: ReadonlyArray<readonly [string, readonly string[]]> =
	Object.freeze([
		['games', ['game', 'steam', 'lutris', 'retro', 'minecraft']],
		['ai', ['ai', 'llm', 'ollama', 'model', 'whisper', 'diffusion', 'embedding']],
		['agent', ['agent', 'bot', 'worker']],
		['system', ['system', 'cron', 'systemd', 'kernel', 'fail2ban', 'ssh', 'ntp']],
		['dev', ['dev', 'git', 'ci', 'build', 'npm', 'cargo', 'jenkins']],
		['infrastructure', ['nginx', 'proxy', 'caddy', 'traefik', 'ingress', 'postgres', 'mysql', 'redis', 'database', 'db']],
		['media', ['media', 'blog', 'video', 'stream', 'jellyfin', 'plex', 'photo', 'wiki']],
		['observability', ['metric', 'prometheus', 'grafana', 'monitor', 'observ', 'backup', 'loki', 'alert']]
	]);

/** Optional bounded-page query of the adopted list route (C01 rule 5). */
export interface ApplicationsPageQuery {
	/** Page size; server default 50, maximum 200 (applications.py bounds). */
	limit?: number;
	/** Page offset. */
	offset?: number;
}

// ---------------------------------------------------------------------------
// Composition seams (separately implemented surfaces; types only)
// ---------------------------------------------------------------------------

/**
 * The lifecycle seam this panel LINKS to instead of reimplementing
 * (acceptance 2): install/update/remove are durable `app.*` intent submits
 * through the jobs facade, owned by their own feature cards. The opener
 * injects real openers; `null` (or an incomplete object) fails closed to a
 * visible "not wired" note — never a client-side fake submit.
 */
export interface ApplicationsLifecycle {
	/** Open the install surface for one adopted application of one server. */
	openInstall(serverId: string, applicationId: string): void;
	/** Open the revision-aware update surface for one adopted application. */
	openUpdate(serverId: string, applicationId: string): void;
	/** Open the explicit keep/delete-data removal surface for one record. */
	openRemove(serverId: string, applicationId: string): void;
}

/**
 * The documents seam for the canonical identity record the `08-app-docs`
 * producer writes at adoption time: the panel renders the recorded doc
 * reference and hands opening to the separately implemented documents
 * panel. `null`/incomplete fails closed to a displayed reference only.
 */
export interface ApplicationsDocs {
	/** Open one stored document (the adoption record doc) in the docs panel. */
	openDocument(docId: string): void;
}

// ---------------------------------------------------------------------------
// Services bag (mirrors the reviewed routes 1:1; opener supplies it)
// ---------------------------------------------------------------------------

/**
 * The applications API slice this panel consumes, injected via the panel
 * services bag (satisfied by the composed client once it grows the P08
 * transport methods). Mirrors the reviewed routes 1:1; reads are
 * session-checked server-side, mutations additionally origin+CSRF checked
 * (auth ADR §5) — the bag never weakens that. Every method names its scope
 * explicitly; there is no module-level or client-level "current server"
 * anywhere (C01 rule 2, NT5).
 */
export interface ApplicationsApi {
	/** GET /api/v1/servers/{id}/applications — one scope's adopted page. */
	listAdopted(serverId: string, page?: ApplicationsPageQuery): Promise<ApplicationPage>;
	/** GET /api/v1/servers/{id}/applications/discoveries — read-only snapshot. */
	discover(serverId: string): Promise<DiscoveryView>;
	/** POST /api/v1/servers/{id}/applications — explicit adoption (C09 rule 1). */
	adopt(serverId: string, payload: AdoptBody): Promise<AdoptionCreated>;
	/** DELETE /api/v1/servers/{id}/applications/{app_id} — non-destructive revoke. */
	revoke(serverId: string, applicationId: string, payload: RevokeBody): Promise<void>;
	/**
	 * POST /api/v1/servers/{id}/applications/{app_id}/execute — queue one
	 * BACKGROUND runbook job (install/update/uninstall/verify). The Chat
	 * window is never opened or pre-filled; the returned view carries the
	 * queued run's identity + replayed state. Optional: without it the
	 * panel falls back to the lifecycle openers (or fails closed).
	 */
	executeRunbook?(
		serverId: string,
		applicationId: string,
		payload: {
			action: 'install' | 'update' | 'uninstall' | 'verify';
			custom_instructions?: string | null;
		}
	): Promise<RunbookExecutionView>;
}

/** Typed services bag the applications panel requires from its opener. */
export interface ApplicationsServices {
	readonly api: ApplicationsApi;
	/**
	 * The lifecycle-links seam (install/update/remove openers). `null`
	 * fails closed: the panel renders the honest unavailable note on those
	 * links and never fakes a lifecycle submission.
	 */
	readonly lifecycle: ApplicationsLifecycle | null;
	/**
	 * The documents seam for the canonical record doc. `null` fails closed:
	 * the recorded doc reference stays displayed text, not a fake link.
	 */
	readonly docs: ApplicationsDocs | null;
}

/** The feature entry for composition into the app's one panel registry. */
export function applicationsEntry(): PanelEntryInput {
	return {
		panel_kind: APPLICATIONS_PANEL_KIND,
		title: 'Applications',
		scope_types: ['server'],
		// The panel browses the pinned server's catalogue; a window may
		// additionally pin ONE application object (focus highlight).
		scope_object_kinds: ['application'],
		component: () =>
			import('../../panels/ApplicationInventoryPanel.svelte').then((module) => module.default),
		default_geometry: { x: 88, y: 72, width: 840, height: 600 },
		min_geometry: { x: 0, y: 0, width: 480, height: 360 },
		// Singleton per full pinned scope: one desktop holds one inventory per
		// server (an application-pinned variant is its own scope key).
		singleton: true,
		capabilities_note:
			'Observes the pinned server\'s applications via read-only lanes, adopts records ' +
			'explicitly with referenced external paths, links lifecycle surfaces; ' +
			'registration grants no permissions.'
	};
}

/**
 * Typed services bag factory for openers (the composed desktop passes
 * `applicationsServices({ …baseClientApplicationsMethods }, lifecycle, docs)`
 * into its panel hosts). Freezing keeps the bag read-only from the panel's
 * side; the panel still fails closed if the bag is missing or incomplete.
 */
export function applicationsServices(
	api: ApplicationsApi,
	lifecycle: ApplicationsLifecycle | null = null,
	docs: ApplicationsDocs | null = null
): ApplicationsServices {
	return Object.freeze({ api, lifecycle, docs });
}
