/**
 * Session client — the typed `/api/v1/agent-sessions/**` transport plus the
 * ONE cursor WebSocket channel controller (BRIEF_D07 §2.2, sfc_PLAN D07).
 *
 * Layer boundaries:
 *  - REST calls delegate to the composed `MaiApiClient` session methods
 *    (client.ts); this module adds NO second HTTP path.
 *  - The channel controller owns the versioned frame protocol over ONE
 *    WebSocket per visible session (`06-session-chat` opens it; switching
 *    sessions drains/closes the old stream — plan acceptance 2).
 *  - Payload/frame types come exclusively from `schema.generated.ts` and
 *    `session-channel.generated.ts`; no hand-written frame interfaces.
 *
 * Committed-cursor discipline (the load-bearing invariant):
 *  - `highestCommittedSeq` advances ONLY on committed entries delivered by
 *    `snapshot` / `entries` / `committed` frames — NEVER on a volatile
 *    `delta` (C08 rule 6: volatile text is never mistaken for durable
 *    continuity; a reconnect replays strictly from the durable cursor).
 *  - Committed entries are deduplicated by `(session_id, seq)` / `entry_id`
 *    and kept sorted by `seq`, so a replay after a lost ack converges on
 *    exactly one copy per seq (D06 proof 4, mirrored client-side).
 *  - Volatile streaming drafts are keyed by `stream_id` with per-`delta_id`
 *    dedup. When the committed entry of that run arrives (`committed`), the
 *    draft is reconciled away and replaced by the single committed entry —
 *    duplicate/out-of-order deltas can never create phantom assistant
 *    messages. `resync_required` and every reconnect discard all volatile
 *    drafts first.
 *
 * Lifecycle honesty: connection state is surfaced
 * (`connecting`/`open`/`reconnecting`/`resync`/`closed`) so the panel shows
 * reconnecting/resync instead of fabricating completion (plan acceptance 6).
 *
 * No module-level mutable state (AGENTS.md §3): every controller is factory
 * created per instance. No background polling: the only timers ever armed
 * are one-shot reconnect backoff timeouts, all cleared by `dispose()`.
 * The WebSocket factory is injectable for deterministic tests.
 */

import type {
	ClientAckFrame,
	ClientHelloFrame,
	ClientPingFrame,
	ClientSubscribeFrame,
	ServerChannelErrorFrame,
	ServerCommittedFrame,
	ServerDeltaFrame,
	ServerEntriesFrame,
	ServerHelloOkFrame,
	ServerPongFrame,
	ServerResyncRequiredFrame,
	ServerRunStateFrame,
	ServerSnapshotFrame
} from '../api/session-channel.generated.js';
import { CSRF_HEADER } from '../api/client.js';

/**
 * One committed entry AS THE CHANNEL DELIVERS IT (the channel-generated
 * SessionEntryView — snapshot/entries/committed all carry this shape).
 * Distinct from the REST envelope type on purpose: the frames are the
 * generated wire truth this controller consumes.
 */
type ChannelEntry = ServerSnapshotFrame['entries'][number];

/** Channel protocol version (D06 contract; hello carries it verbatim). */
const CHANNEL_PROTOCOL_VERSION = 1;

/** Connection lifecycle surfaced to the panel (plan acceptance 2/6). */
export type SessionChannelConnectionState =
	| 'connecting'
	| 'open'
	| 'reconnecting'
	| 'resync'
	| 'closed';

/** Structural WebSocket subset the controller needs (browser-compatible). */
export interface ChannelSocket {
	send(data: string): void;
	close(code?: number, reason?: string): void;
	onopen: (() => void) | null;
	onclose: ((event: { code?: number; reason?: string }) => void) | null;
	onerror: ((event: unknown) => void) | null;
	onmessage: ((event: { data: unknown }) => void) | null;
}

/** One volatile streaming draft: the run's uncommitted assistant text. */
export interface SessionStreamDraft {
	/** The run's volatile text stream identity (D06 StreamIdentity rule). */
	stream_id: string;
	/** The run the stream belongs to (reconcile key for `committed`). */
	run_id: string | null;
	/** Deltas applied in arrival order, deduplicated by `delta_id`. */
	text: string;
	/** `delta_id`s already applied (out-of-order/duplicate protection). */
	seen_delta_ids: Set<string>;
}

/** Handler surface: every server frame plus derived state notifications. */
export interface SessionChannelHandlers {
	onHelloOk?(frame: ServerHelloOkFrame): void;
	onSnapshot?(frame: ServerSnapshotFrame): void;
	onEntries?(frame: ServerEntriesFrame): void;
	onCommitted?(frame: ServerCommittedFrame): void;
	onDelta?(frame: ServerDeltaFrame): void;
	onRunState?(frame: ServerRunStateFrame): void;
	onResyncRequired?(frame: ServerResyncRequiredFrame): void;
	onError?(frame: ServerChannelErrorFrame): void;
	onPong?(frame: ServerPongFrame): void;
	/** Connection lifecycle transitions (reconnecting/resync honesty). */
	onConnectionState?(state: SessionChannelConnectionState): void;
	/** The merged, deduplicated committed log after every merge. */
	onCommittedEntriesChanged?(entries: readonly ChannelEntry[]): void;
	/** The run's volatile draft after every delta merge. */
	onStreamDraftChanged?(draft: SessionStreamDraft | null): void;
}

/** Controller options (all instance-scoped; injectable for tests). */
export interface SessionChannelOptions {
	/** Session identity; the channel URL carries ONLY this (+ scope pin). */
	sessionId: string;
	/** Non-secret scope pin riding the URL (D06: no query-string secrets). */
	serverId?: string | null;
	/**
	 * CSRF token for the `hello` proof. Defaults to the `mai_csrf` cookie;
	 * a null token fails closed client-side (no socket is opened).
	 */
	getCsrfToken?: () => string | null;
	/** Socket factory; defaults to the browser WebSocket. Injectable. */
	socketFactory?: (url: string) => ChannelSocket;
	/** One-shot reconnect backoff ladder in ms (default 0.5s→30s, capped). */
	reconnectDelaysMs?: readonly number[];
	/** Frame/state handlers (all optional). */
	handlers?: SessionChannelHandlers;
	/** `crypto.randomUUID` seam for deterministic tests. */
	newRequestId?: () => string;
}

/** Default reconnect ladder: bounded, exponential, 30s ceiling. */
const DEFAULT_RECONNECT_DELAYS_MS: readonly number[] = [
	500,
	1_000,
	2_000,
	4_000,
	8_000,
	15_000,
	30_000
];

/** Reads one cookie value; browser-only (SSR: null → fail closed). */
function readCookie(name: string): string | null {
	if (typeof document === 'undefined') return null;
	for (const pair of document.cookie.split(';')) {
		const eq = pair.indexOf('=');
		if (eq === -1) continue;
		if (pair.slice(0, eq).trim() === name) {
			try {
				return decodeURIComponent(pair.slice(eq + 1).trim());
			} catch {
				return pair.slice(eq + 1).trim();
			}
		}
	}
	return null;
}

/** The channel URL: session id + non-secret scope pin, no secrets ever. */
export function sessionChannelUrl(sessionId: string, serverId?: string | null): string {
	const params = new URLSearchParams();
	if (serverId !== undefined && serverId !== null) params.set('server_id', serverId);
	const query = params.toString();
	return (
		`/api/v1/agent-sessions/${encodeURIComponent(sessionId)}/channel` +
		(query === '' ? '' : `?${query}`)
	);
}

/** The public controller surface (named contract; panels hold this type). */
export interface SessionChannelController {
	/** Open the channel (hello handshake; safe to call once). */
	connect(): void;
	/** Send the application heartbeat probe (server answers `pong`). */
	ping(): void;
	/** Advance THIS connection's committed-cursor acknowledgement. */
	ack(): void;
	/** User-initiated close: no reconnect is ever scheduled afterwards. */
	close(): void;
	/** Final teardown: timers cleared, volatile state dropped. */
	dispose(): void;
	/** The merged committed log, sorted by `seq`. */
	entries(): readonly ChannelEntry[];
	/** The durable cursor (null before any committed entry). */
	highestCommittedSeq(): number | null;
	/** One run's volatile draft, or null. */
	streamDraft(streamId: string): SessionStreamDraft | null;
	/** All live volatile drafts. */
	streamDrafts(): readonly SessionStreamDraft[];
	/** Current connection lifecycle state. */
	connectionState(): SessionChannelConnectionState;
}

/**
 * ONE session's channel controller. Factory created; instances share no
 * state. See the module docstring for the committed-cursor discipline.
 */
export function createSessionChannelController(options: SessionChannelOptions): SessionChannelController {
	const handlers = options.handlers ?? {};
	const delays = options.reconnectDelaysMs ?? DEFAULT_RECONNECT_DELAYS_MS;
	const newRequestId =
		options.newRequestId ??
		(() =>
			typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
				? crypto.randomUUID()
				: `req-${Date.now()}-${Math.random().toString(16).slice(2)}`);

	// --- Instance state (all controller-local; no module singletons) --------
	let socket: ChannelSocket | null = null;
	let state: SessionChannelConnectionState = 'closed';
	let disposed = false;
	let userClosed = false;
	let openAttempts = 0;
	let helloSeen = false;
	/** True once ANY hello_ok succeeded (a re-open is a RE-connection). */
	let everConnected = false;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	/** Committed log: deduplicated by (session_id, seq), sorted by seq. */
	const committedBySeq = new Map<number, ChannelEntry>();
	/** Volatile drafts keyed by stream_id. */
	const drafts = new Map<string, SessionStreamDraft>();
	/** stream_ids already reconciled away by a committed entry. */
	const reconciledStreams = new Set<string>();

	function setState(next: SessionChannelConnectionState): void {
		if (state === next) return;
		state = next;
		handlers.onConnectionState?.(next);
	}

	function committedEntriesSorted(): ChannelEntry[] {
		return [...committedBySeq.values()].sort((a, b) => a.seq - b.seq);
	}

	function notifyEntries(): void {
		handlers.onCommittedEntriesChanged?.(committedEntriesSorted());
	}

	/** Merge committed entries; returns the NEW highest seq in the batch. */
	function mergeCommittedEntries(entries: readonly ChannelEntry[]): number | null {
		let highest: number | null = null;
		for (const entry of entries) {
			if (entry === null || typeof entry !== 'object') continue;
			const seq = Number(entry.seq);
			const entryId = String(entry.entry_id ?? '');
			if (!Number.isFinite(seq) || seq < 1 || entryId === '') continue;
			// Exactly one copy per (session-scoped) seq: replay after a lost
			// ack overwrites in place instead of duplicating.
			committedBySeq.set(seq, entry);
			if (highest === null || seq > highest) highest = seq;
			// Reconcile: the committed truth replaces the run's volatile draft
			// so duplicate/out-of-order deltas never leave phantom text.
			const runId = entry.run_id ?? null;
			if (runId !== null) {
				for (const [streamId, draft] of drafts) {
					if (draft.run_id === runId) {
						drafts.delete(streamId);
						reconciledStreams.add(streamId);
						handlers.onStreamDraftChanged?.(null);
					}
				}
			}
		}
		return highest;
	}

	function sendFrame(frame: ClientHelloFrame | ClientSubscribeFrame | ClientAckFrame | ClientPingFrame): void {
		if (socket === null) return;
		socket.send(JSON.stringify(frame));
	}

	function sendHello(): void {
		const token = options.getCsrfToken ? options.getCsrfToken() : readCookie('mai_csrf');
		if (token === null || token === '') {
			// Fail closed: the CSRF proof is part of the hello; without it the
			// server would close 1008 csrf_rejected — never open the socket.
			setState('closed');
			return;
		}
		const hello: ClientHelloFrame = {
			type: 'hello',
			protocol_version: CHANNEL_PROTOCOL_VERSION,
			after_seq: committedBySeq.size > 0 ? highestSeq() : null,
			csrf_token: token,
			client_request_id: newRequestId()
		};
		sendFrame(hello);
	}

	function highestSeq(): number | null {
		if (committedBySeq.size === 0) return null;
		let highest: number | null = null;
		for (const seq of committedBySeq.keys()) {
			if (highest === null || seq > highest) highest = seq;
		}
		return highest;
	}

	function handleFrame(frame: Record<string, unknown>): void {
		const type = frame['type'];
		switch (type) {
			case 'hello_ok': {
				helloSeen = true;
				everConnected = true;
				openAttempts = 0;
				setState('open');
				handlers.onHelloOk?.(frame as unknown as ServerHelloOkFrame);
				notifyEntries();
				return;
			}
			case 'snapshot': {
				const snapshot = frame as unknown as ServerSnapshotFrame;
				mergeCommittedEntries(snapshot.entries ?? []);
				// A fresh snapshot is the server's bounded truth; adopt its
				// latest_seq only through the merged committed entries (which
				// include every snapshot entry) — never speculatively.
				handlers.onSnapshot?.(snapshot);
				notifyEntries();
				return;
			}
			case 'entries': {
				const entries = frame as unknown as ServerEntriesFrame;
				mergeCommittedEntries(entries.entries ?? []);
				handlers.onEntries?.(entries);
				notifyEntries();
				return;
			}
			case 'delta': {
				const delta = frame as unknown as ServerDeltaFrame;
				applyDelta(delta);
				handlers.onDelta?.(delta);
				return;
			}
			case 'committed': {
				const committed = frame as unknown as ServerCommittedFrame;
				if (committed.entry !== undefined && committed.entry !== null) {
					mergeCommittedEntries([committed.entry]);
					handlers.onCommitted?.(committed);
					notifyEntries();
				}
				return;
			}
			case 'run_state': {
				handlers.onRunState?.(frame as unknown as ServerRunStateFrame);
				return;
			}
			case 'resync_required': {
				const resync = frame as unknown as ServerResyncRequiredFrame;
				setState('resync');
				resubscribeAfterGap();
				handlers.onResyncRequired?.(resync);
				return;
			}
			case 'error': {
				handlers.onError?.(frame as unknown as ServerChannelErrorFrame);
				return;
			}
			case 'pong': {
				handlers.onPong?.(frame as unknown as ServerPongFrame);
				return;
			}
			default:
				// Unknown frame types are ignored (forward compatibility); the
				// durable store stays the only continuity authority.
				return;
		}
	}

	function applyDelta(delta: ServerDeltaFrame): void {
		const streamId = String(delta.stream_id ?? '');
		if (streamId === '') return;
		let draft = drafts.get(streamId);
		if (draft === undefined) {
			draft = { stream_id: streamId, run_id: delta.run_id ?? null, text: '', seen_delta_ids: new Set() };
			drafts.set(streamId, draft);
		}
		const deltaId = String(delta.delta_id ?? '');
		if (deltaId !== '') {
			if (draft.seen_delta_ids.has(deltaId)) return; // duplicate delta
			draft.seen_delta_ids.add(deltaId);
		}
		draft.text += String(delta.text ?? '');
		handlers.onStreamDraftChanged?.(draft);
	}

	/** Gap honesty: discard volatile deltas, re-subscribe from the cursor. */
	function resubscribeAfterGap(): void {
		// Volatile drafts are disposable by contract; the durable log is not.
		const hadDrafts = drafts.size > 0;
		drafts.clear();
		reconciledStreams.clear();
		if (hadDrafts) handlers.onStreamDraftChanged?.(null);
		const cursor = highestSeq();
		const subscribe: ClientSubscribeFrame = {
			type: 'subscribe',
			after_seq: cursor,
			client_request_id: newRequestId()
		};
		sendFrame(subscribe);
		// Advance THIS connection's cursor acknowledgement (no-op at null).
		if (cursor !== null) {
			const ack: ClientAckFrame = { type: 'ack', after_seq: cursor };
			sendFrame(ack);
		}
	}

	function clearReconnectTimer(): void {
		if (reconnectTimer !== null) {
			clearTimeout(reconnectTimer);
			reconnectTimer = null;
		}
	}

	function scheduleReconnect(): void {
		if (disposed || userClosed) return;
		const delay = delays[Math.min(openAttempts, delays.length - 1)];
		openAttempts += 1;
		setState('reconnecting');
		clearReconnectTimer();
		// ONE one-shot timeout — never an interval, never a polling loop.
		reconnectTimer = setTimeout(() => {
			reconnectTimer = null;
			openSocket();
		}, delay);
	}

	function openSocket(): void {
		if (disposed || userClosed) return;
		clearReconnectTimer();
		const url = sessionChannelUrl(options.sessionId, options.serverId);
		const factory =
			options.socketFactory ?? ((wsUrl: string) => new WebSocket(wsUrl) as unknown as ChannelSocket);
		const token = options.getCsrfToken ? options.getCsrfToken() : readCookie('mai_csrf');
		if (token === null || token === '') {
			setState('closed');
			return;
		}
		// A RE-open attempt stays honest: the panel shows reconnecting (not a
		// fresh connection) until the server re-acknowledges the handshake.
		setState(everConnected ? 'reconnecting' : 'connecting');
		const originPrefix = typeof location !== 'undefined' ? location.origin : '';
		const next = factory(url.startsWith('/') ? `${originPrefix}${url}` : url);
		socket = next;
		helloSeen = false;
		next.onopen = () => {
			if (socket !== next) return;
			sendHello();
		};
		next.onmessage = (event) => {
			if (socket !== next) return;
			const raw = typeof event.data === 'string' ? event.data : '';
			let parsed: unknown;
			try {
				parsed = JSON.parse(raw);
			} catch {
				return; // malformed transport frame: ignored, cursor untouched
			}
			if (parsed === null || typeof parsed !== 'object') return;
			handleFrame(parsed as Record<string, unknown>);
		};
		next.onerror = () => {
			if (socket !== next) return;
		};
		next.onclose = () => {
			if (socket !== next) return;
			socket = null;
			helloSeen = false;
			if (disposed || userClosed) {
				setState('closed');
				return;
			}
			scheduleReconnect();
		};
	}

	return {
		connect() {
			if (disposed || userClosed || socket !== null || reconnectTimer !== null) return;
			openSocket();
		},
		ping() {
			const ping: ClientPingFrame = { type: 'ping' };
			sendFrame(ping);
		},
		ack() {
			const cursor = highestSeq();
			if (cursor === null) return;
			const ack: ClientAckFrame = { type: 'ack', after_seq: cursor };
			sendFrame(ack);
		},
		close() {
			userClosed = true;
			clearReconnectTimer();
			if (socket !== null) {
				const closing = socket;
				socket = null;
				closing.onclose = null;
				closing.onmessage = null;
				closing.onerror = null;
				closing.onopen = null;
				closing.close(1000, 'client-close');
			}
			setState('closed');
		},
		dispose() {
			disposed = true;
			clearReconnectTimer();
			if (socket !== null) {
				const closing = socket;
				socket = null;
				closing.onclose = null;
				closing.onmessage = null;
				closing.onerror = null;
				closing.onopen = null;
				closing.close(1000, 'dispose');
			}
			committedBySeq.clear();
			drafts.clear();
			reconciledStreams.clear();
			setState('closed');
		},
		entries() {
			return committedEntriesSorted();
		},
		highestCommittedSeq() {
			return highestSeq();
		},
		streamDraft(streamId: string) {
			return drafts.get(streamId) ?? null;
		},
		streamDrafts() {
			return [...drafts.values()];
		},
		connectionState() {
			return state;
		}
	};
}

/**
 * Typed REST slice the session-first panel consumes (satisfied by the
 * composed client's session methods; see client.ts). Re-declared here as a
 * structural subset so the chat panel and the channel controller share one
 * bag without importing the whole client type into the entry contract.
 */
export interface SessionRestApi {
	createAgentSession(payload: {
		server_id: string;
		source: 'interactive' | 'runbook' | 'document_attach' | 'api';
		title?: string | null;
		model_profile?: string | null;
		client_request_id: string;
	}): Promise<{ session_id: string } & Record<string, unknown>>;
	submitAgentSessionTurn(
		sessionId: string,
		payload: {
			session_id: string;
			text: string;
			client_request_id: string;
			model_profile?: string | null;
		}
	): Promise<{
		session_id: string;
		run_id: string;
		entry_id: string;
		seq: number;
		run_state: string;
		deduplicated: boolean;
	}>;
	submitAgentSessionControl(
		sessionId: string,
		payload: { action: string; run_id?: string | null; payload?: Record<string, unknown> | null }
	): Promise<Record<string, unknown>>;
}

/** Re-exported so panels import the header constant from one place. */
export { CSRF_HEADER };
