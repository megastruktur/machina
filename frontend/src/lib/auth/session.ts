/**
 * MAI browser session controller (card `02-web-session`, contracts.md C08,
 * auth ADR §4–§5 ownership table: this module owns the login contract and the
 * "no credential persistence in localStorage" rule).
 *
 * Design (windowed-workspace ADR §5):
 * - No module-level state: every `createSessionController()` call builds a
 *   self-contained instance (own store, own client), so no UI state is shared
 *   between SSR requests, tabs or windows. Only factories are exported.
 * - No credential persistence: the password exists exactly for the duration of
 *   one `login()` argument. The session credential is the `HttpOnly`
 *   `mai_session` cookie owned by the browser jar; this controller never sees
 *   it and never touches `localStorage`/`sessionStorage`/cookies. The only
 *   retained value is the C01 error view of the last failed exchange.
 * - Server truth only: the status is derived from real API exchanges, never
 *   from a client-side flag ("no fake UI-only authentication"). The probe is
 *   the authenticated CSRF re-mint (`GET /api/v1/auth/csrf`, auth ADR §5):
 *   `204` ⇒ a valid session answers, `401` ⇒ none. No session-check endpoint
 *   is invented (C08 rule 8).
 * - Errors surface as their frozen stable codes (`unauthenticated`,
 *   `throttled`, `origin_rejected`, …) so the route can render appropriate
 *   session errors; payloads stay value-free per C01 rule 3.
 */

import { writable } from 'svelte/store';
import { ApiError, createMaiApiClient, type MaiApiClient } from '$lib/api/client';

/**
 * Session states rendered by the route (C08 rule 6: loading/unknown/error are
 * explicit, never all-green on unavailable data).
 * - `checking`  — no answer yet (initial and during a probe).
 * - `authenticated` / `unauthenticated` — the two answers the API can give.
 * - `unavailable` — the boundary could not be reached (transport failure or
 *   5xx); the previous authentication belief is NOT silently kept.
 */
export type SessionStatus = 'checking' | 'authenticated' | 'unauthenticated' | 'unavailable';

/** Value-free error view for the route to render (C01 envelope fields). */
export interface SessionErrorView {
	code: string;
	message: string;
	requestId: string;
}

/** Immutable status snapshot delivered to subscribers. */
export interface SessionSnapshot {
	status: SessionStatus;
	/** Last boundary denial; `null` after a successful exchange or a 401 probe. */
	error: SessionErrorView | null;
	/** A login/logout exchange is in flight (honest pending state). */
	busy: boolean;
}

export interface SessionControllerOptions {
	/** API boundary; defaults to a fresh same-origin `createMaiApiClient()`. */
	client?: MaiApiClient;
}

export interface SessionController {
	subscribe: (run: (snapshot: SessionSnapshot) => void) => () => void;
	/**
	 * Ask the real boundary whether a session exists (authenticated CSRF
	 * re-mint). A `401` here is the normal unauthenticated answer, not an
	 * error worth rendering — it clears any stale error view.
	 */
	probe(): Promise<SessionStatus>;
	/**
	 * Exchange the password for a fresh session (ADR §4: a new token is minted
	 * at every login). The argument is never retained; a failed login surfaces
	 * its stable code on `error` and keeps/returns `unauthenticated`.
	 */
	login(password: string): Promise<SessionStatus>;
	/**
	 * Revoke the presenting session. `401` means the session was already gone:
	 * the goal state is reached either way (revocation is effective on the
	 * next request, no grace window). A `403` boundary rejection (origin/CSRF)
	 * did NOT revoke anything — the status is kept and the error surfaced.
	 */
	logout(): Promise<SessionStatus>;
}

/** C01 transport/5xx failures render as one unavailable state. */
function unavailableError(error: unknown): SessionErrorView {
	if (error instanceof ApiError) {
		return { code: error.code, message: error.message, requestId: error.requestId };
	}
	const reason = error instanceof Error ? error.name : 'unknown';
	return { code: 'unavailable', message: `the API boundary is unreachable (${reason})`, requestId: '' };
}

export function createSessionController(options: SessionControllerOptions = {}): SessionController {
	const client = options.client ?? createMaiApiClient();

	const store = writable<SessionSnapshot>({ status: 'checking', error: null, busy: false });

	/** Mirrors the store's status so denied exchanges can keep it. */
	let status: SessionStatus = 'checking';

	function patch(change: Partial<SessionSnapshot>): void {
		if (change.status !== undefined) status = change.status;
		store.update((current) => ({ ...current, ...change }));
	}

	async function probe(): Promise<SessionStatus> {
		patch({ status: 'checking' });
		try {
			await client.refreshCsrf();
			patch({ status: 'authenticated', error: null });
			return 'authenticated';
		} catch (error) {
			if (error instanceof ApiError && error.status === 401) {
				// The normal signed-out answer: not a failure worth rendering.
				patch({ status: 'unauthenticated', error: null });
				return 'unauthenticated';
			}
			patch({ status: 'unavailable', error: unavailableError(error) });
			return 'unavailable';
		}
	}

	async function login(password: string): Promise<SessionStatus> {
		patch({ busy: true, error: null });
		try {
			await client.login({ password });
			patch({ status: 'authenticated', error: null });
			return 'authenticated';
		} catch (error) {
			const view = unavailableError(error);
			// A rejected credential (and any other boundary denial) leaves the
			// caller unauthenticated; transport/5xx leaves the state unknown.
			patch({
				status: error instanceof ApiError ? 'unauthenticated' : 'unavailable',
				error: view
			});
			return error instanceof ApiError ? 'unauthenticated' : 'unavailable';
		} finally {
			patch({ busy: false });
		}
	}

	async function logout(): Promise<SessionStatus> {
		patch({ busy: true, error: null });
		try {
			await client.logout();
			patch({ status: 'unauthenticated', error: null });
			return 'unauthenticated';
		} catch (error) {
			if (error instanceof ApiError && error.status === 401) {
				// Already revoked/expired: the signed-out goal state is reached.
				patch({ status: 'unauthenticated', error: null });
				return 'unauthenticated';
			}
			// A denied logout revoked nothing: keep the current belief (the
			// status field is untouched) and surface why.
			patch({ error: unavailableError(error) });
			return status;
		} finally {
			patch({ busy: false });
		}
	}

	return {
		subscribe: store.subscribe,
		probe,
		login,
		logout
	};
}
