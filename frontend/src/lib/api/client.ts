/**
 * MAI typed API client boundary (C01/C08) — hand-written transport over the
 * generated schema. Payload types come exclusively from
 * `./schema.generated.ts`; endpoint methods here never restate field shapes.
 *
 * Schema generation (the one documented command; rerun from the worktree
 * root whenever a backend wire model changes, then `git diff` must be empty
 * on a no-drift tree):
 *
 *     export_path=/tmp/mai-fxt-02-api-client/openapi.json && \
 *     mkdir -p "$(dirname "$export_path")" && \
 *     (cd backend && uv run python -c "import json,sys; from mai.api.app import \
 * create_app; json.dump(create_app().openapi(), open(sys.argv[1],'w'), indent=2)" \
 * "$export_path") && \
 *     (cd frontend && pnpm dlx openapi-typescript@7.13.0 "$export_path" \
 * -o src/lib/api/schema.generated.ts)
 *
 * Contract invariants enforced here (contracts.md C01/C08; auth ADR §4–§5):
 * - The principal is the `mai_session` HttpOnly cookie (C08 rule 1); this
 *   client never reads, stores or transmits the session token anywhere but
 *   the browser cookie jar (`credentials: 'include'`), and nothing is
 *   persisted to localStorage/sessionStorage.
 * - Mutating requests carry double-submit `x-csrf-token` == `mai_csrf`
 *   cookie (C08 rule 2) whenever the session-bearing boundary requires it;
 *   a missing token fails closed client-side before any bytes are sent.
 *   The G1 setup-state routes (`bootstrap`, `login`) run before any cookie
 *   exists and are exact-origin-protected instead (auth ADR §3/§8), so the
 *   client attaches their token only when one is already available.
 * - Errors are the C01 structured envelope `{error: {code, message,
 *   request_id, fields}}`; anything unparsable collapses to a generic
 *   status-derived `ApiError` — raw bodies are never surfaced to UI code.
 * - Every server-scoped operation takes its explicit `server_id` argument
 *   (C01 rule 2); there is no module-level or client-level "current server"
 *   anywhere in this module.
 * - Instances are self-contained: no module-level mutable state, so no SSR
 *   singleton can share state between requests (windowed ADR D4; §5 rule 2).
 *   Browser APIs are touched only inside instance methods, never at module
 *   top level.
 *
 * Event cursor transport (C08 rule 7 / C05 rule 4): `subscribeEvents` is a
 * generic SSE read primitive. The concrete jobs endpoint URL and its wire
 * format are owned by `05-jobs-api`/`05-web-jobs` (the one open C08 item);
 * this module commits only to SSE `Last-Event-ID` as the replay-cursor
 * carrier and to the frozen event envelope shape (`MaiRunEvent`). It never
 * invents an endpoint path.
 */

import type { components, operations } from './schema.generated';

/** Frozen wire names (auth ADR §4–§5; mirrors backend mai.api.auth). */
export const SESSION_COOKIE = 'mai_session';
export const CSRF_COOKIE = 'mai_csrf';
export const CSRF_HEADER = 'x-csrf-token';

/** C01 rule 3 stable error codes observed in the integrated P02 surface. */
export type KnownApiErrorCode =
	| 'unauthenticated'
	| 'csrf_rejected'
	| 'origin_rejected'
	| 'request_rejected'
	| 'throttled'
	| 'conflict'
	| 'validation_failed'
	| 'not_found'
	| 'method_not_allowed'
	| 'unavailable'
	| 'internal_error';

export type ApiErrorBody = components['schemas']['ErrorBody'];
export type PasswordBody = components['schemas']['PasswordBody'];
export type ProfileCreate = components['schemas']['ProfileCreate'];
export type ProfileEdit = components['schemas']['ProfileEdit'];
export type ProfileMutationResult = components['schemas']['ProfileMutationResult'];
export type ProfileResponse = components['schemas']['ProfileResponse'];

/**
 * Liveness/readiness bodies. `app.py` returns plain dicts via JSONResponse,
 * so the OpenAPI declarations are `unknown`; these narrow types document the
 * actual observed bodies (backend/src/mai/api/app.py `liveness`/`readiness`).
 */
export interface HealthOk {
	status: string;
}
export interface ReadinessReport {
	status: 'ok' | 'unhealthy';
	database: string;
}

/** One failed API exchange, rendered from the C01 envelope. */
export class ApiError extends Error {
	readonly status: number;
	readonly code: string;
	readonly requestId: string;
	readonly fields: Readonly<Record<string, string>>;

	constructor(
		status: number,
		code: string,
		message: string,
		requestId = '',
		fields: Readonly<Record<string, string>> = {}
	) {
		super(`API ${status} ${code}: ${message}`);
		this.name = 'ApiError';
		this.status = status;
		this.code = code;
		this.requestId = requestId;
		this.fields = fields;
	}
}

/** Client-side misuse or transport failure (no server envelope involved). */
export class MaiClientError extends Error {
	constructor(message: string) {
		super(message);
		this.name = 'MaiClientError';
	}
}

export interface MaiApiClientOptions {
	/**
	 * Base URL prepended to `/api/v1/...` paths. Default '' (same-origin:
	 * the single TLS proxy routes `/api` to mai-api, C08 rule 3). Server-side
	 * callers (SSR load) pass the Compose-internal `http://mai-api:8000`.
	 */
	baseUrl?: string;
	/** Fetch implementation; defaults to global fetch. Inject for SSR/tests. */
	fetch?: typeof fetch;
	/** Extra headers on every request (e.g. SSR cookie forwarding). */
	headers?: Record<string, string>;
	/**
	 * CSRF token source for mutating requests. Default: the `mai_csrf`
	 * cookie (readable by first-party JS by contract). Returns null when
	 * unavailable — mutations then fail closed client-side.
	 */
	getCsrfToken?: () => string | null;
}

type MutatingMethod = 'POST' | 'PUT' | 'PATCH' | 'DELETE';

/** Reads one cookie value from `document.cookie`; browser-only, else null. */
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

interface EnvelopeShape {
	code: string;
	message: string;
	requestId: string;
	fields: Record<string, string>;
}

/** Accepts only a well-formed C01 envelope; anything else is rejected. */
function parseEnvelope(body: unknown): EnvelopeShape | null {
	if (typeof body !== 'object' || body === null || !('error' in body)) return null;
	const error: unknown = body.error;
	if (typeof error !== 'object' || error === null) return null;
	if (!('code' in error) || !('message' in error)) return null;
	if (typeof error.code !== 'string' || typeof error.message !== 'string') return null;
	const fields: Record<string, string> = {};
	if ('fields' in error && typeof error.fields === 'object' && error.fields !== null) {
		for (const [key, value] of Object.entries(error.fields)) {
			if (typeof value === 'string') fields[key] = value;
		}
	}
	return {
		code: error.code,
		message: error.message,
		requestId: 'request_id' in error && typeof error.request_id === 'string' ? error.request_id : '',
		fields
	};
}

/**
 * Render one non-OK response as an ApiError: the C01 envelope when the body
 * parses as one, otherwise a generic status-derived fallback. Shared by the
 * request and event-stream paths so both surface identical error shapes.
 */
async function responseError(response: Response): Promise<ApiError> {
	try {
		const envelope = parseEnvelope(await response.json());
		if (envelope) {
			return new ApiError(
				response.status,
				envelope.code,
				envelope.message,
				envelope.requestId,
				envelope.fields
			);
		}
	} catch {
		// non-JSON error body: status-derived fallback below
	}
	return new ApiError(
		response.status,
		'request_rejected',
		response.status >= 500 ? 'internal server error' : 'request rejected'
	);
}

/** Frozen event envelope (C05 rule 4); `05-jobs-api` produces, UI consumes. */
export interface MaiRunEvent {
	seq: number;
	run_id: string;
	ts: string;
	type: string;
	v: number;
	data: unknown;
}

/** One parsed SSE frame: raw id/event plus decoded data text. */
export interface SseFrame {
	id: string | null;
	event: string | null;
	data: string;
}

/** Narrow a parsed SSE frame to the frozen MaiRunEvent shape, else null. */
export function readMaiEvent(frame: SseFrame): MaiRunEvent | null {
	let parsed: unknown;
	try {
		parsed = JSON.parse(frame.data);
	} catch {
		return null;
	}
	if (typeof parsed !== 'object' || parsed === null) return null;
	if (
		!('seq' in parsed) ||
		!('run_id' in parsed) ||
		!('ts' in parsed) ||
		!('type' in parsed) ||
		!('v' in parsed)
	) {
		return null;
	}
	if (
		typeof parsed.seq !== 'number' ||
		typeof parsed.run_id !== 'string' ||
		typeof parsed.ts !== 'string' ||
		typeof parsed.type !== 'string' ||
		typeof parsed.v !== 'number'
	) {
		return null;
	}
	return {
		seq: parsed.seq,
		run_id: parsed.run_id,
		ts: parsed.ts,
		type: parsed.type,
		v: parsed.v,
		data: 'data' in parsed ? parsed.data : undefined
	};
}

export interface SubscribeEventsOptions {
	/** Replay cursor to resume from; travels as the SSE `Last-Event-ID`. */
	cursor?: string | null;
	/** Abort to close the stream; the returned promise then resolves. */
	signal?: AbortSignal;
	/** Called once per complete SSE frame, in stream order. */
	onEvent: (frame: SseFrame) => void;
	/** Fetch implementation; defaults to global fetch. */
	fetch?: typeof fetch;
}

/**
 * Subscribe to an authenticated, origin-checked event stream (C08 rule 7).
 * Replay is read-only: this primitive never submits, cancels or retargets
 * anything (windowed ADR §7 "Backend independence"). Resolves when the
 * server closes the stream or `signal` aborts; rejects on HTTP/network
 * failure with `ApiError`/`TypeError` respectively.
 */
export async function subscribeEvents(
	url: string,
	options: SubscribeEventsOptions
): Promise<void> {
	const doFetch = options.fetch ?? globalThis.fetch;
	const headers: Record<string, string> = { accept: 'text/event-stream' };
	if (options.cursor) headers['last-event-id'] = options.cursor;
	const response = await doFetch(url, {
		method: 'GET',
		credentials: 'include',
		headers,
		signal: options.signal
	});
	if (!response.ok) throw await responseError(response);
	if (!response.body) {
		throw new MaiClientError('event stream closed without a body');
	}
	const reader = response.body.getReader();
	const decoder = new TextDecoder();
	let buffer = '';
	const emitFrame = (raw: string) => {
		const frame = parseSseFrame(raw);
		if (frame) options.onEvent(frame);
	};
	try {
		for (;;) {
			const { done, value } = await reader.read();
			if (done) break;
			buffer += decoder.decode(value, { stream: true });
			let boundary = buffer.indexOf('\n\n');
			while (boundary !== -1) {
				emitFrame(buffer.slice(0, boundary));
				buffer = buffer.slice(boundary + 2);
				boundary = buffer.indexOf('\n\n');
			}
		}
		buffer += decoder.decode();
		if (buffer.trim()) emitFrame(buffer);
	} finally {
		reader.releaseLock();
	}
}

/** Parses one SSE frame (LF or CRLF line endings; comments ignored). */
function parseSseFrame(raw: string): SseFrame | null {
	const dataLines: string[] = [];
	let id: string | null = null;
	let event: string | null = null;
	for (const line of raw.split(/\r?\n/)) {
		if (!line || line.startsWith(':')) continue;
		const colon = line.indexOf(':');
		const field = colon === -1 ? line : line.slice(0, colon);
		const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '');
		if (field === 'data') dataLines.push(value);
		else if (field === 'id') id = value;
		else if (field === 'event') event = value;
		// `retry` and unknown fields are ignored (transport concern only)
	}
	if (dataLines.length === 0) return null;
	return { id, event, data: dataLines.join('\n') };
}

/** Typed boundary over the frozen P02 surface (`operations` of the schema). */
export interface MaiApiClient {
	health(): Promise<HealthOk>;
	readiness(): Promise<ReadinessReport>;
	bootstrap(payload: PasswordBody): Promise<void>;
	login(payload: PasswordBody): Promise<void>;
	logout(): Promise<void>;
	refreshCsrf(): Promise<void>;
	/** POST /api/v1/servers — the service mints `server_id` (C01 rule 1). */
	createProfile(payload: ProfileCreate): Promise<ProfileMutationResult>;
	/** GET /api/v1/servers/{server_id} — explicit target, never a selection. */
	readProfile(serverId: string): Promise<ProfileResponse>;
	/** PUT /api/v1/servers/{server_id} — revision-checked edit (C01 rule 6). */
	editProfile(serverId: string, payload: ProfileEdit): Promise<ProfileMutationResult>;
}

/**
 * Build one self-contained API client. Create a fresh instance wherever a
 * request scope needs one (SSR load, browser session controller); instances
 * share nothing, so no state can leak across SSR requests or windows.
 */
export function createMaiApiClient(options: MaiApiClientOptions = {}): MaiApiClient {
	const baseUrl = options.baseUrl ?? '';
	const doFetch = options.fetch ?? globalThis.fetch.bind(globalThis);
	const extraHeaders = options.headers ?? {};
	const getCsrfToken = options.getCsrfToken ?? (() => readCookie(CSRF_COOKIE));

	/** One HTTP exchange: CSRF guard, credential cookies, C01 error mapping.
	 * CSRF class: session-bearing mutations REQUIRE the double-submit token
	 * (auth ADR §5); the pre-session setup routes `bootstrap`/`login` are
	 * exact-origin-protected instead (ADR §3/§8 — no cookie exists yet), so
	 * they attach the token only when one is already available. */
	async function send(
		method: 'GET' | MutatingMethod,
		path: string,
		body?: unknown,
		csrf: 'required' | 'optional' = 'required'
	): Promise<Response> {
		const headers: Record<string, string> = { accept: 'application/json', ...extraHeaders };
		if (body !== undefined) headers['content-type'] = 'application/json';
		if (method !== 'GET') {
			const token = getCsrfToken();
			if (token) headers[CSRF_HEADER] = token;
			else if (csrf === 'required') {
				throw new MaiClientError(
					`mutation to ${path} blocked: no ${CSRF_COOKIE} token available`
				);
			}
		}
		const response = await doFetch(baseUrl + path, {
			method,
			credentials: 'include',
			headers,
			body: body === undefined ? undefined : JSON.stringify(body)
		});
		if (!response.ok) throw await responseError(response);
		return response;
	}

	/** Success body boundary cast: the shape is owned by the generated schema
	 * (schema.generated.ts); no runtime re-validation happens client-side. */
	async function requestJson<Success>(
		method: 'GET' | MutatingMethod,
		path: string,
		body?: unknown,
		csrf: 'required' | 'optional' = 'required'
	): Promise<Success> {
		const response = await send(method, path, body, csrf);
		try {
			return (await response.json()) as Success;
		} catch {
			throw new MaiClientError(`${method} ${path}: success body was not valid JSON`);
		}
	}

	/** 204-style success: no body is read; unexpected statuses fail loudly. */
	async function requestVoid(
		method: 'GET' | MutatingMethod,
		path: string,
		body?: unknown,
		expectedStatus?: number,
		csrf: 'required' | 'optional' = 'required'
	): Promise<void> {
		const response = await send(method, path, body, csrf);
		if (expectedStatus !== undefined && response.status !== expectedStatus) {
			throw new MaiClientError(`${method} ${path}: unexpected status ${response.status}`);
		}
	}

	return {
		health: () => requestJson<HealthOk>('GET', '/api/v1/health'),
		readiness: () => requestJson<ReadinessReport>('GET', '/api/v1/health/ready'),
		// G1 setup-state routes: exact-origin layer, no session/CSRF yet (ADR §3).
		bootstrap: (payload) => requestVoid('POST', '/api/v1/auth/bootstrap', payload, 201, 'optional'),
		login: (payload) => requestVoid('POST', '/api/v1/auth/login', payload, 204, 'optional'),
		logout: () => requestVoid('POST', '/api/v1/auth/logout', undefined, 204),
		refreshCsrf: () => requestVoid('GET', '/api/v1/auth/csrf', undefined, 204),
		createProfile: (payload) => requestJson<ProfileMutationResult>('POST', '/api/v1/servers', payload),
		readProfile: (serverId) =>
			requestJson<ProfileResponse>('GET', `/api/v1/servers/${encodeURIComponent(serverId)}`),
		editProfile: (serverId, payload) =>
			requestJson<ProfileMutationResult>(
				'PUT',
				`/api/v1/servers/${encodeURIComponent(serverId)}`,
				payload
			)
	};
}

/** Compile-time pin: the client surface is typed by generated operations. */
export type MaiOperationName = keyof operations;
