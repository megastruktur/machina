/**
 * Feature-owned panel entry — session-first chat (D07; sfc_PLAN D07).
 *
 * This module is the session-first composition unit of the chat surface:
 *
 *  - the stable panel slug (`SESSION_CHAT_PANEL_KIND = 'chat'`) and the
 *    typed session-first wire shapes (`SessionChatApi` bag);
 *  - the channel-URL builder and channel-controller re-exports from
 *    `../../chat/session_client.js` (the ONE cursor WebSocket controller);
 *  - the typed services bag the chat panel and the history drawer consume,
 *    including the history-tag slice routed per row identity.
 *
 * Expand/Contract (COORD-D02-01): the legacy `ChatApi` vocabulary stays the
 * base of the bag until D09 deletes it — `SessionChatApi` EXTENDS it, so
 * `ChatPanel.svelte` keeps every reviewed method while the session-first
 * methods ride the same bag additively. There is no legacy/canonical
 * toggle: one composition, one wire.
 *
 * No module-level mutable state (AGENTS.md §3); safe to evaluate per SSR
 * request. Channel controllers are factory created per visible session —
 * switching sessions drains/closes the old stream (plan acceptance 2).
 */

import type { PanelEntryInput } from '../registry.js';
import type {
	AgentSessionEntriesQuery,
	AgentSessionListQuery,
	AgentSessionPage,
	AgentSessionView,
	SessionControlOutcome,
	SessionControlRequest,
	SessionCreateRequest,
	SessionEntriesPage,
	SessionTagsPage,
	SessionTurnSubmitOutcome,
	SessionTurnSubmitRequest,
	TagAssignBody,
	TagEntityTagsQuery,
	TagRemovalOptions
} from '../../api/client.js';
import {
	sessionChannelUrl,
	type SessionChannelController
} from '../../chat/session_client.js';
import type {
	ChatApi,
	ChatServerOption,
	ChatServiceOption,
	ChatTagApi
} from './06-web-chat.js';

/** Stable panel slug — the session-first chat IS the chat panel (D07). */
export const SESSION_CHAT_PANEL_KIND = 'chat';

/**
 * The wire's sentinel server id for a General Consult session (the same
 * reviewed sentinel the legacy surface uses; a draft with no pinned server
 * creates its session under this id and restores as the General Consult
 * badge instead of a Server Chip).
 */
export const GLOBAL_CONSULT_SERVER_ID = '00000000-0000-0000-0000-000000000000';

/** The ONE cursor channel URL builder (re-exported for openers). */
export const sessionChatChannelUrl = sessionChannelUrl;
export type { SessionChannelController };

/**
 * The session-first API slice this surface consumes: the reviewed legacy
 * `ChatApi` vocabulary (kept until D09, COORD-D02-01) plus the typed
 * `/api/v1/agent-sessions/**` transport. Every collection read is an
 * explicitly-scoped bounded page (AGENTS.md §1); mutations carry the CSRF
 * double submit inside the client boundary.
 */
export interface SessionChatApi extends ChatApi {
	/** POST /api/v1/agent-sessions — idempotent session create. */
	createAgentSession(payload: SessionCreateRequest): Promise<AgentSessionView>;
	/** GET /api/v1/agent-sessions — bounded deterministic page. */
	listAgentSessions(query?: AgentSessionListQuery): Promise<AgentSessionPage>;
	/** GET /api/v1/agent-sessions/{id}?server_id=… — safe header detail. */
	readAgentSession(sessionId: string, serverId?: string | null): Promise<AgentSessionView>;
	/** PATCH /api/v1/agent-sessions/{id} — durable user-assigned title. */
	renameAgentSession(sessionId: string, payload: { title: string }): Promise<AgentSessionView>;
	/**
	 * GET /api/v1/agent-sessions/{id}/history?server_id=… — bounded
	 * cursor-exclusive entries page (the entries route of the D06 backend).
	 */
	listAgentSessionEntries(
		sessionId: string,
		query?: AgentSessionEntriesQuery
	): Promise<SessionEntriesPage>;
	/** POST /api/v1/agent-sessions/{id}/turns — durable intent admission. */
	submitAgentSessionTurn(
		sessionId: string,
		payload: SessionTurnSubmitRequest
	): Promise<SessionTurnSubmitOutcome>;
	/** POST /api/v1/agent-sessions/{id}/control — steer/pause/cancel/… */
	submitAgentSessionControl(
		sessionId: string,
		payload: SessionControlRequest
	): Promise<SessionControlOutcome>;
}

/**
 * The OPTIONAL history-tag slice, session-first routing included: the
 * catalog methods stay the reviewed global ones; per-entity assignment
 * reads/writes route by the ROW's identity (session_id rows read the
 * agent-session tag routes; conversation_id rows the conversation routes;
 * legacy run rows the job routes — never conflated, COORD-D02-01).
 */
export interface SessionChatTagApi extends ChatTagApi {
	/** GET /api/v1/agent-sessions/{id}/tags?server_id=… — bounded page. */
	listAgentSessionTags(
		sessionId: string,
		serverId: string,
		page?: TagEntityTagsQuery
	): Promise<SessionTagsPage>;
	/** POST /api/v1/agent-sessions/{id}/tags — idempotent assignment. */
	assignAgentSessionTag(sessionId: string, payload: TagAssignBody): Promise<void>;
	/** DELETE /api/v1/agent-sessions/{id}/tags/{tag_id}?server_id=… */
	removeAgentSessionTag(sessionId: string, tagId: string, options: TagRemovalOptions): Promise<void>;
}

/**
 * Typed services bag for the session-first chat surface. `channel` is the
 * ONE channel controller of the currently visible session — `null` when no
 * session is open (the panel renders the honest not-connected state; it
 * never fabricates a stream). The bag owns no controller lifecycle: openers
 * create/dispose controllers per session switch.
 */
export interface SessionChatServices {
	readonly api: SessionChatApi;
	/** Optional history-tag slice (session-first routing included). */
	readonly tags?: SessionChatTagApi;
	/** The visible session's channel controller, when one is open. */
	readonly channel?: SessionChannelController | null;
	/** Available servers for the draft-mode Server Chip / picker. */
	readonly serverOptions?: readonly ChatServerOption[];
	/** Mapped/discovered services for the draft-mode `@service` autocomplete. */
	readonly availableServices?: readonly ChatServiceOption[];
}

/** The feature entry for composition into the app's one panel registry. */
export function sessionChatEntry(): PanelEntryInput {
	return {
		panel_kind: SESSION_CHAT_PANEL_KIND,
		title: 'Chat',
		scope_types: ['server'],
		// The panel composes sessions of the pinned server; a window may
		// additionally pin ONE session object. Runs/documents/applications
		// are other panels' pins.
		scope_object_kinds: ['conversation'],
		component: () => import('../../panels/ChatPanel.svelte').then((module) => module.default),
		default_geometry: { x: 48, y: 72, width: 780, height: 620 },
		min_geometry: { x: 0, y: 0, width: 480, height: 360 },
		// Free-instance: two windows may hold two sessions of one server
		// side by side; each instance is scoped by its own descriptor.
		singleton: false,
		capabilities_note:
			'Creates and selects sessions for the pinned target, admits turns ' +
			'through the typed session API and streams the visible session ' +
			'channel; registration grants no permissions.'
	};
}

/**
 * Typed services bag factory for openers. Freezing keeps the bag read-only
 * from the panel's side; panels still fail closed on absent slices.
 */
export function sessionChatServices(
	api: SessionChatApi,
	tags?: SessionChatTagApi,
	channel?: SessionChannelController | null
): SessionChatServices {
	return Object.freeze({
		api,
		...(tags === undefined ? {} : { tags }),
		...(channel === undefined ? {} : { channel })
	});
}
