<script module lang="ts">
	/**
	 * ServerProfilePanel — reusable server-profile/prompt-edition panel
	 * (card `mai-02-web-server`, P02; service row `mai-web`, contracts C01/C08/C11).
	 *
	 * Layer: feature panel (windowed-workspace ADR-0001 §2 layer table). It owns
	 * its one domain view and nothing else:
	 *
	 *  - Explicit context only: the pinned target is `panel.scope.server_id`
	 *    (validated at open time, immutable per ADR §3.2). No global or launcher
	 *    "current server" exists anywhere in this module; two panels bound to
	 *    different servers cannot influence each other (NT5).
	 *  - Explicit services only: the profile API enters through
	 *    `panel.services.api` (the typed `ServerProfileApi` shape, satisfied by
	 *    `createMaiApiClient()`; owned by the feature entry module). A
	 *    missing/incomplete bag fails closed with a visible state — no service
	 *    locator, no fallback client.
	 *  - No window mechanics: no drag/geometry/focus/persistence code and no
	 *    workspace-module imports; the window host supplies all of that.
	 *  - Mount reads, never writes (ADR §7.2): opening/mounting issues only
	 *    `readProfile`. Writes happen exclusively on an explicit Save click and
	 *    carry the last observed revision (`expected_revision`, C01 rule 6).
	 *  - States per C08 rule 6: loading / ready / stale badge (projection flag
	 *    is shown, never hidden, C02 W2) / not-found / unauthenticated /
	 *    unavailable-error, plus save-conflict and save-error banners.
	 *  - Conflict discipline (brief acceptance 2): a stale `conflict` response
	 *    is DISPLAYED; the panel never auto-retries with a fresh revision (no
	 *    silent overwrite), keeps the user's unsaved input, and requires an
	 *    explicit Reload before saving again.
	 *  - Credential non-disclosure (brief acceptance 1): the profile API
	 *    returns no secret values; the panel additionally renders neither
	 *    `ssh.credential_ref` (a reference, not a display field) nor
	 *    `ssh.host_key` — target identity is `server_id` + name + endpoint.
	 *  - No module-level state and no `window`/`document` access (ADR §5
	 *    rule 4): safe to evaluate per SSR request; instances share nothing.
	 *
	 * Tokens come only from ui/tokens.css; no dependency is added (ADR §4.3).
	 */
</script>

<script lang="ts">
	import { onDestroy } from 'svelte';
	import Button from '../ui/Button.svelte';
	import Field from '../ui/Field.svelte';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import type { ProfileResponse } from '../api/client.js';
	import type { ServerProfileApi } from '../workspace/entries/02-web-server.js';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readApi(services: PanelServices): ServerProfileApi | null {
		const candidate = services.api as Partial<ServerProfileApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		if (typeof candidate.readProfile !== 'function') return null;
		if (typeof candidate.editProfile !== 'function') return null;
		return candidate as ServerProfileApi;
	}

	// Explicit inputs as derivations: the descriptor/context object is replaced
	// on presentation/geometry changes (PanelHost contract), so reactive reads
	// stay correct by construction; the pinned scope VALUE never changes.
	const api = $derived(readApi(panel.services));
	/** The pinned target captured at open time — never a live selection. */
	const serverId = $derived(panel.scope.kind === 'server' ? panel.scope.server_id : null);
	const usable = $derived(api !== null && serverId !== null);

	// --- view state (C08 rule 6: never all-green on unavailable data) --------

	type LoadState =
		| { status: 'loading' }
		| { status: 'error'; code: string; message: string }
		| { status: 'ready'; profile: ProfileResponse };

	let view = $state<LoadState>({ status: 'loading' });
	/** Editable prompt edition input (string until parsed — numeric input). */
	let revisionInput = $state('');
	let saving = $state(false);
	/** Stale-edit conflict banner: displayed, never auto-overwritten. */
	let conflictMessage = $state<string | null>(null);
	/** Non-conflict save failure (session ended, unavailable, …). */
	let saveProblem = $state<{ code: string; message: string; hint: string } | null>(null);
	/** Post-save confirmation (kept until the next edit attempt or reload). */
	let savedNotice = $state<string | null>(null);

	/** Set once the mount cycle ended (hide/close/error teardown); async
	 * continuations check it before touching state (ADR §7.2 release). */
	let disposed = true;

	const parsedRevision = $derived(parseEdition(revisionInput));
	const revisionError = $derived(
		view.status === 'ready' && parsedRevision === null
			? 'Prompt edition must be an integer ≥ 1.'
			: ''
	);
	const dirty = $derived(
		view.status === 'ready' &&
			parsedRevision !== null &&
			parsedRevision !== view.profile.prompt_revision
	);
	const saveDisabled = $derived(
		saving || !dirty || parsedRevision === null || conflictMessage !== null
	);

	function parseEdition(raw: string): number | null {
		if (!/^\d+$/.test(raw)) return null;
		const value = Number(raw);
		return value >= 1 ? value : null;
	}

	function codeOf(error: unknown): string | null {
		if (typeof error !== 'object' || error === null) return null;
		const code = (error as { code?: unknown }).code;
		return typeof code === 'string' ? code : null;
	}

	function messageOf(error: unknown): string | null {
		return error instanceof Error && error.message ? error.message : null;
	}

	/** Stable, display-safe text for the frozen C01 codes (no raw payloads). */
	function friendlyText(code: string, fallback: string): string {
		if (code === 'unauthenticated') return 'Your session has ended — sign in again.';
		if (code === 'not_found') return 'This server profile does not exist.';
		if (code === 'throttled') return 'Too many requests — try again shortly.';
		return fallback;
	}

	/** Full reload: shows the loading state and resets the editor. */
	async function loadProfile(): Promise<void> {
		if (api === null || serverId === null) return;
		view = { status: 'loading' };
		try {
			const profile = await api.readProfile(serverId);
			if (disposed) return;
			view = { status: 'ready', profile };
			revisionInput = String(profile.prompt_revision);
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			view = {
				status: 'error',
				code,
				message: friendlyText(code, messageOf(error) ?? 'The server profile could not be loaded.')
			};
		}
	}

	/**
	 * Silent re-read after a successful save: refreshes the authoritative
	 * record (projection view included) without flickering the editor and
	 * without resubmitting anything. On failure the optimistic post-commit
	 * state from the mutation result stays visible.
	 */
	async function refreshProfile(): Promise<void> {
		if (api === null || serverId === null || view.status !== 'ready') return;
		try {
			const profile = await api.readProfile(serverId);
			if (disposed) return;
			view = { status: 'ready', profile };
			revisionInput = String(profile.prompt_revision);
		} catch {
			// Current view already reflects the post-commit triple (C02 rule 4).
		}
	}

	async function saveEdition(): Promise<void> {
		if (view.status !== 'ready' || api === null || serverId === null) return;
		if (saving || conflictMessage !== null || parsedRevision === null) return;
		const profile = view.profile;
		saving = true;
		saveProblem = null;
		savedNotice = null;
		try {
			// The observed revision rides along (C01 rule 6); the server rejects
			// a stale caller with a structured `conflict` BEFORE any write.
			const result = await api.editProfile(serverId, {
				name: profile.name,
				prompt_revision: parsedRevision,
				environment: profile.environment,
				ssh: profile.ssh,
				privilege: profile.privilege,
				capabilities: [...profile.capabilities],
				expected_revision: profile.revision
			});
			if (disposed) return;
			view = {
				status: 'ready',
				profile: {
					...profile,
					prompt_revision: parsedRevision,
					revision: result.revision,
					sha256: result.sha256
				}
			};
			revisionInput = String(parsedRevision);
			savedNotice = `Saved — prompt edition ${parsedRevision}, canonical revision ${result.revision}.`;
			void refreshProfile();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error);
			if (code === 'conflict') {
				// Brief acceptance 2: DISPLAY the conflict. No retry with a fresh
				// revision (that would overwrite the other writer's change); the
				// unsaved input is kept; Save stays blocked until an explicit
				// Reload re-syncs the observed revision.
				conflictMessage =
					'The profile changed since this window last loaded. Reload to review the stored profile before editing — your input is kept and nothing was written.';
			} else {
				const resolved = code ?? 'unavailable';
				saveProblem = {
					code: resolved,
					message: messageOf(error) ?? friendlyText(resolved, 'The profile could not be saved.'),
					hint: resolved === 'unauthenticated' ? ' Sign in again, then retry.' : ''
				};
			}
		} finally {
			if (!disposed) saving = false;
		}
	}

	/** Revert the editor to the loaded record (local edits only). */
	function cancelEdit(): void {
		if (view.status !== 'ready') return;
		revisionInput = String(view.profile.prompt_revision);
		saveProblem = null;
	}

	/** Explicit re-sync: the only path that clears a stale-edit conflict. */
	function reloadProfile(): void {
		conflictMessage = null;
		saveProblem = null;
		savedNotice = null;
		void loadProfile();
	}

	// Mount reads once; the teardown of this effect is the panel's release
	// path (hide/unmount/error containment) — in-flight responses observe
	// `disposed` and never write afterwards (ADR §7.2).
	$effect(() => {
		if (!usable) return;
		disposed = false;
		void loadProfile();
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

<div class="mai-server-profile" data-testid="server-profile-root">
	{#if !usable}
		<div class="mai-server-profile__state mai-server-profile__state--error" role="alert" data-testid="panel-config-error">
			<p>This panel was opened without its profile service or a pinned server target.</p>
		</div>
	{:else if view.status === 'loading'}
		<div class="mai-server-profile__state" role="status" data-testid="profile-loading">
			<p>Loading server profile…</p>
		</div>
	{:else if view.status === 'error'}
		<div
			class="mai-server-profile__state mai-server-profile__state--error"
			role="alert"
			data-testid="profile-error"
			data-error-code={view.code}
		>
			<p>{view.message}</p>
			<Button variant="secondary" onclick={loadProfile} data-testid="profile-retry">Retry</Button>
		</div>
	{:else}
		<section class="mai-server-profile__body" aria-label="Server profile">
			{#if view.profile.projection.stale}
				<p
					class="mai-server-profile__badge"
					role="status"
					data-testid="projection-stale"
				>
					Projection stale — the stored index has not reconciled yet (C02 W2)
				</p>
			{/if}

			<dl class="mai-server-profile__rows">
				<div class="mai-server-profile__row">
					<dt>Server</dt>
					<dd><code data-testid="server-id">{serverId}</code></dd>
				</div>
				<div class="mai-server-profile__row">
					<dt>Name</dt>
					<dd data-testid="profile-name">{view.profile.name}</dd>
				</div>
				<div class="mai-server-profile__row">
					<dt>Environment</dt>
					<dd data-testid="profile-environment">{view.profile.environment}</dd>
				</div>
				<div class="mai-server-profile__row">
					<dt>SSH target</dt>
					<dd data-testid="ssh-target">
						{view.profile.ssh.user}@{view.profile.ssh.host}:{view.profile.ssh.port}
					</dd>
				</div>
				<div class="mai-server-profile__row">
					<dt>Privilege</dt>
					<dd data-testid="privilege">{view.profile.privilege}</dd>
				</div>
				<div class="mai-server-profile__row">
					<dt>Capabilities</dt>
					<dd data-testid="capabilities">
						{view.profile.capabilities.length > 0 ? view.profile.capabilities.join(', ') : '—'}
					</dd>
				</div>
				<div class="mai-server-profile__row">
					<dt>Canonical revision</dt>
					<dd><span data-testid="profile-revision">{view.profile.revision}</span></dd>
				</div>
				<div class="mai-server-profile__row">
					<dt>Projection revision</dt>
					<dd data-testid="projection-revision">{view.profile.projection.revision ?? '—'}</dd>
				</div>
				<div class="mai-server-profile__row">
					<dt>Content hash</dt>
					<dd><code data-testid="sha256">{view.profile.sha256}</code></dd>
				</div>
			</dl>

			<div class="mai-server-profile__editor">
				<Field
					label="Prompt edition"
					hint="Versioned prompt edition (prompt_revision) saved with the profile; integer ≥ 1."
					bind:value={revisionInput}
					error={revisionError}
					inputmode="numeric"
					autocomplete="off"
					data-testid="prompt-revision-input"
				/>

				{#if conflictMessage !== null}
					<div class="mai-server-profile__notice mai-server-profile__notice--warning" role="alert" data-testid="conflict-banner">
						<p>{conflictMessage}</p>
					</div>
				{/if}
				{#if saveProblem !== null}
					<div
						class="mai-server-profile__notice mai-server-profile__notice--danger"
						role="alert"
						data-testid="save-error-banner"
						data-error-code={saveProblem.code}
					>
						<p>{saveProblem.message}{saveProblem.hint}</p>
					</div>
				{/if}
				{#if savedNotice !== null}
					<p class="mai-server-profile__notice mai-server-profile__notice--success" role="status" data-testid="saved-notice">
						{savedNotice}
					</p>
				{/if}

				<div class="mai-server-profile__actions">
					<Button onclick={saveEdition} loading={saving} disabled={saveDisabled} data-testid="save-button">
						Save edition
					</Button>
					<Button variant="secondary" onclick={cancelEdit} disabled={saving || !dirty} data-testid="cancel-button">
						Cancel
					</Button>
					<Button variant="secondary" onclick={reloadProfile} disabled={saving} data-testid="reload-button">
						Reload
					</Button>
				</div>
			</div>
		</section>
	{/if}
</div>

<style>
	.mai-server-profile {
		height: 100%;
		overflow: auto;
		color: var(--mai-color-text);
		background: var(--mai-color-bg);
	}

	.mai-server-profile__state {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		align-items: flex-start;
		padding: var(--mai-space-4);
		color: var(--mai-status-neutral-text);
	}

	.mai-server-profile__state--error {
		color: var(--mai-status-danger-text);
	}

	.mai-server-profile__state p {
		margin: 0;
	}

	.mai-server-profile__body {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-4);
		padding: var(--mai-space-4);
	}

	.mai-server-profile__badge {
		margin: 0;
		padding: var(--mai-space-1) var(--mai-space-3);
		font-size: var(--mai-text-sm);
		color: var(--mai-status-warning-text);
		background: var(--mai-status-warning-bg);
		border: var(--mai-border-width) solid var(--mai-status-warning-border);
		border-radius: var(--mai-radius-sm);
	}

	.mai-server-profile__rows {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		margin: 0;
	}

	.mai-server-profile__row {
		display: grid;
		grid-template-columns: minmax(9rem, max-content) 1fr;
		gap: var(--mai-space-3);
	}

	.mai-server-profile__row dt {
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-medium);
		color: var(--mai-color-text-muted);
	}

	.mai-server-profile__row dd {
		margin: 0;
		font-size: var(--mai-text-sm);
		overflow-wrap: anywhere;
	}

	.mai-server-profile__row code {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
	}

	.mai-server-profile__editor {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		padding-top: var(--mai-space-3);
		border-top: var(--mai-border-width) solid var(--mai-color-border);
	}

	.mai-server-profile__notice {
		margin: 0;
		padding: var(--mai-space-2) var(--mai-space-3);
		font-size: var(--mai-text-sm);
		border-radius: var(--mai-radius-sm);
	}

	.mai-server-profile__notice p {
		margin: 0;
	}

	.mai-server-profile__notice--warning {
		color: var(--mai-status-warning-text);
		background: var(--mai-status-warning-bg);
		border: var(--mai-border-width) solid var(--mai-status-warning-border);
	}

	.mai-server-profile__notice--danger {
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
		border: var(--mai-border-width) solid var(--mai-status-danger-border);
	}

	.mai-server-profile__notice--success {
		color: var(--mai-status-success-text);
		background: var(--mai-status-success-bg);
		border: var(--mai-border-width) solid var(--mai-status-success-border);
	}

	.mai-server-profile__actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mai-space-2);
	}
</style>
