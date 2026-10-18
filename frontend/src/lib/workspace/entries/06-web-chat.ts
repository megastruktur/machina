/**
 * Feature-owned panel entry — card `mai-06-web-chat` (P06, service `web`).
 *
 * This module is the ONE registration point of the conversation panel
 * (windowed-workspace ADR §7.1: a fixed trusted component map; registration
 * grants no permissions). It owns:
 *
 *  - the stable panel slug and the typed wire shapes the panel consumes
 *    (generated schema only — no invented endpoints, C08 rule 8);
 *  - the typed services bag the panel requires, mirroring the reviewed
 *    producers 1:1: the chat surface (`06-chat-api`,
 *    `backend/src/mai/api/chat.py`), the shared run-record read (`05-jobs-api`
 *    — the durable step receipts are where a bound approval decision is
 *    discoverable) and the approvals decision surface (`03-approval-api`);
 *  - the concrete SSE binding of the one conversation event-stream route
 *    (C08 rule 7: transport = SSE, replay cursor = `Last-Event-ID`, frozen by
 *    `05-jobs-api` and reused by `06-chat-api`). The URL builder and the
 *    stream call are composed here once; the panel stays transport-shaped and
 *    never hardcodes the path.
 *
 * The child delegation tree is NOT re-implemented here: the panel composes
 * the one reviewed `DelegationTree` view (`06-web-delegation`), whose bag is
 * a structural subset of `ChatApi`.
 *
 * No module-level mutable state; safe to evaluate per SSR request (ADR §5
 * rule 4). Composition passes `chatServices({ …clientChatMethods,
 * streamConversationEvents: browserChatEventStream })` — the composed client
 * grows the chat transport methods; instances still share nothing.
 */

import type { components } from '../../api/schema.generated.js';
import type { SubscribeEventsOptions } from '../../api/client.js';
import { subscribeEvents } from '../../api/client.js';
import type { PanelEntryInput } from '../registry.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const CHAT_PANEL_KIND = 'chat';

/** Generated wire shape: one submit body (client-minted idempotency key, C05 rule 5). */
export type ChatSubmitBody = components['schemas']['ChatSubmitBody'];
/** Generated wire shape: one explicitly scoped child task (no target field, C06 rule 5). */
export type ChildBody = components['schemas']['ChildBody'];
/** Generated wire shape: one permitted-artifact context reference (identity only). */
export type AttachmentBody = components['schemas']['AttachmentBody'];
/** Generated wire shape: submit outcome (conversation view + dedup verdict). */
export type ChatCreated = components['schemas']['ChatCreated'];
/** Generated wire shape: one bounded, explicitly server-scoped conversation page. */
export type ChatPage = components['schemas']['ChatPage'];
/** Generated wire shape: one conversation plus recorded references and children. */
export type ConversationView = components['schemas']['ConversationView'];
/** Generated wire shape: explicit tree cancellation body (guard rule 4). */
export type ChatCancelBody = components['schemas']['ChatCancelBody'];
/** Generated wire shape: KNOWN engine outcome or an honest pending. */
export type CancelView = components['schemas']['CancelView'];
/** Generated wire shape: one run plus its durable step rows (shared run record). */
export type RunDetail = components['schemas']['RunDetail'];
/** Generated wire shape: the safe preview of one bound decision. */
export type ApprovalView = components['schemas']['ApprovalView'];
/** Generated wire shape: the binding the admin confirms, field-for-field (C03 rule 4). */
export type DecisionBody = components['schemas']['DecisionBody'];

/** Optional bounded-page query shared by the list routes (C01 rule 5). */
export interface PageQuery {
	/** Page size; server default and maximum are producer-owned bounds. */
	limit?: number;
	/** Page offset. */
	offset?: number;
}

/**
 * Options of the one SSE subscription (the generic `SubscribeEventsOptions`
 * shape of the reviewed client primitive): `cursor` is the last seen event
 * `seq` and travels as the standard SSE `Last-Event-ID` header; `signal`
 * aborts the stream (hide/close/unmount release path, ADR §7.2); `onEvent`
 * receives each raw frame in stream order for the panel to narrow and
 * deduplicate.
 */
export type ConversationEventStreamOptions = SubscribeEventsOptions;

/**
 * The conversation API slice this panel consumes, injected via the panel
 * services bag (satisfied by the composed client once it grows the P06
 * transport methods). Mirrors the reviewed routes 1:1; read-only routes are
 * session-checked server-side, mutations additionally origin+CSRF-checked
 * (auth ADR §5) — the bag never weakens that. The three delegation-tree
 * methods are a structural subset of the reviewed `DelegationApi` bag.
 */
export interface ChatApi {
	/** POST /api/v1/chat/messages — durable intent; idempotent on `run_id`. */
	submitMessage(payload: ChatSubmitBody): Promise<ChatCreated>;
	/** GET /api/v1/chat/runs?server_id=… — explicit scope, never unfiltered. */
	listConversations(serverId: string, page?: PageQuery): Promise<ChatPage>;
	/** GET /api/v1/chat/runs/{run_id} — replayed state + recorded references. */
	readConversation(runId: string): Promise<ConversationView>;
	/** POST /api/v1/chat/runs/{run_id}/cancel — explicit tree cancellation. */
	cancelConversation(runId: string, payload: ChatCancelBody): Promise<CancelView>;
	/**
	 * GET /api/v1/chat/runs/{run_id}/events — authenticated, origin-checked
	 * SSE subscription with `Last-Event-ID` replay (C08 rule 7). Resolves
	 * when the server closes its bounded stream or `signal` aborts; rejects
	 * on HTTP/network failure. Read-only: a disconnected browser cancels
	 * nothing (windowed ADR §7).
	 */
	streamConversationEvents(runId: string, options: ConversationEventStreamOptions): Promise<void>;
	/**
	 * GET /api/v1/jobs/runs/{run_id} — the shared durable run record. The
	 * conversation panel reads it ONLY to discover a bound approval decision
	 * from this run's step receipts (value-free); the delegation tree needs
	 * none of it.
	 */
	readRun(runId: string): Promise<RunDetail>;
	/** GET /api/v1/approvals/{approval_id} — safe preview of one decision. */
	readApproval(approvalId: string): Promise<ApprovalView>;
	/** POST /api/v1/approvals/{approval_id}/decision — one-use consume. */
	decideApproval(approvalId: string, payload: DecisionBody): Promise<ApprovalView>;
}

/** Typed services bag the conversation panel requires from its opener. */
export interface ChatServices {
	readonly api: ChatApi;
}

/**
 * The reviewed producer path of the conversation event stream
 * (`06-chat-api`); the one place the concrete URL exists client-side. The
 * path is pinned by the generated schema operation
 * `stream_conversation_events_api_v1_chat_runs__run_id__events_get`.
 */
export function chatEventsUrl(runId: string): string {
	return `/api/v1/chat/runs/${encodeURIComponent(runId)}/events`;
}

/**
 * Browser composition of the bag's `streamConversationEvents`: the reviewed
 * generic SSE primitive over the concrete producer URL. Authenticated via
 * cookies, replay cursor carried as `Last-Event-ID` (C08 rule 7).
 */
export function browserChatEventStream(
	runId: string,
	options: ConversationEventStreamOptions
): Promise<void> {
	return subscribeEvents(chatEventsUrl(runId), options);
}

/** The feature entry for composition into the app's one panel registry. */
export function chatEntry(): PanelEntryInput {
	return {
		panel_kind: CHAT_PANEL_KIND,
		title: 'Chat',
		scope_types: ['server'],
		// The panel composes conversations of the pinned server; a window may
		// additionally pin ONE conversation object. Runs/documents/applications
		// are other panels' pins.
		scope_object_kinds: ['conversation'],
		component: () => import('../../panels/ChatPanel.svelte').then((module) => module.default),
		default_geometry: { x: 48, y: 72, width: 780, height: 620 },
		min_geometry: { x: 0, y: 0, width: 480, height: 360 },
		// Free-instance: two windows may hold two conversations of one server
		// side by side; each instance is scoped by its own descriptor.
		singleton: false,
		capabilities_note:
			'Submits conversations for the pinned target, replays the recorded ' +
			'transcript and stream, records one-use approval decisions, composes ' +
			'the shared delegation tree; registration grants no permissions.'
	};
}

/**
 * Typed services bag factory for openers (the composed desktop passes
 * `chatServices({ …baseClientChatMethods, streamConversationEvents:
 * browserChatEventStream })` into its panel hosts). Freezing keeps the bag
 * read-only from the panel's side; the panel still fails closed if the bag
 * is missing or incomplete.
 */
export function chatServices(api: ChatApi): ChatServices {
	return Object.freeze({ api });
}
