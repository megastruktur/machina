<script lang="ts">
	/**
	 * IngestKeysSection — the Sources tab surface (campaign `notification-inbox`,
	 * task `nbi-fe-sources`; contracts docs/architecture/notification-inbox.md
	 * §6/§7, AGENTS.md pagination invariant, C08 rule 6).
	 *
	 * Mounted by the notifications panel's Inbox/Sources tab seam: the panel
	 * passes the reviewed API bag (`panel.services.api`) and its loaded
	 * enrolled profiles. The component owns nothing else — no window
	 * mechanics, no stores, no module-level mutable state, no `{@html}`.
	 *
	 *  - Bounded key list (AGENTS.md invariant): initial page (limit 20),
	 *    `offset`/`total`/`hasMore` taken from the producer envelope only,
	 *    Load more appends rows deduplicated by `key_id` inside a bounded
	 *    scroll container. Columns carry metadata ONLY — label, scope
	 *    (Global or the enrolled display name with a raw-id fallback),
	 *    created, last_used, expires, revoked — never `key_hash` or any key
	 *    material (the server never returns it; this surface never shows one).
	 *  - One-time secret (auth ADR §13): a mint receipt's plaintext renders
	 *    EXACTLY ONCE inside a modal — copy button with explicit feedback,
	 *    the «will not be shown again» warning, focus moved into the dialog
	 *    on open and restored on close, and the §7 canonical curl example
	 *    with the `$MAI_INGEST_KEY` placeholder (never the minted secret).
	 *    Closing clears the plaintext from component state immediately;
	 *    nothing ever writes it to localStorage/sessionStorage/logs
	 *    (C03 rule 6).
	 *  - Revoke is receipt-driven and never optimistic: an inline confirm
	 *    gates the destructive call; the row flips ONLY when the durable
	 *    refetch that follows the receipt reports the server's `revoked_at`.
	 *  - Honest states (C08 rule 6): loading/empty/error (+ Retry) for the
	 *    list, envelope-coded problem banners for load-more/revoke/mint
	 *    failures, busy states on the mint/revoke controls.
	 */
	import { untrack } from 'svelte';
	import Button from '../ui/Button.svelte';
	import Select from '../ui/Select.svelte';
	import type { IngestKeyView } from '../api/client.js';
	import type {
		CreateIngestKeyBody,
		IssuedIngestKeyView,
		NotificationsApi,
		ProfileResponse
	} from '../workspace/entries/14-web-notifications.js';

	interface Props {
		/** The reviewed notifications surface; an absent bag fails closed. */
		api: NotificationsApi | null;
		/** Loaded enrolled profiles (scope display names); may be empty. */
		profiles: ProfileResponse[];
	}

	let { api, profiles }: Props = $props();

	/** Bounded initial/load-more page size (AGENTS.md pagination invariant). */
	const PAGE_LIMIT = 20;

	/**
	 * The §7 canonical publish example. `$MAI_INGEST_KEY` stays a PLACEHOLDER
	 * forever — the minted secret is never substituted into rendered text
	 * beyond the one-time value display.
	 */
	const CURL_EXAMPLE = [
		'curl -i -X POST https://$ORIGIN/api/v1/notifications/ingest \\',
		'    -H "Authorization: Bearer $MAI_INGEST_KEY" \\',
		'    -H "Content-Type: application/json" \\',
		'    -d \'{"title":"disk almost full","body":"/var usage 94% on db-1","severity":"warning","source":"host-monitor","dedup_key":"disk-/var-db-1","context_ref":"https://monitoring.example/host/db-1"}\''
	].join('\n');

	type KeyListState =
		| { status: 'loading' }
		| { status: 'error'; code: string; message: string }
		| { status: 'ready' };

	/** Instance-scoped unmount guard: an in-flight page never lands dead. */
	let disposed = true;

	function codeOf(error: unknown): string | null {
		if (typeof error !== 'object' || error === null || !('code' in error)) return null;
		return typeof error.code === 'string' ? error.code : null;
	}

	function messageOf(error: unknown): string | null {
		return error instanceof Error && error.message ? error.message : null;
	}

	/** Stable, display-safe text for the frozen C01 codes (no raw payloads). */
	function friendlyText(code: string, fallback: string): string {
		if (code === 'unauthenticated') return 'Your session has ended — sign in again.';
		if (code === 'throttled') return 'Too many requests — try again shortly.';
		if (code === 'unavailable')
			return 'The key service is unavailable right now; nothing was changed.';
		return fallback;
	}

	// --- scope display names (the panel's loaded profiles; raw-id fallback) --

	const serverNames = $derived.by(() => {
		const names = new Map<string, string>();
		for (const profile of profiles) names.set(profile.server_id, profile.name);
		return names;
	});

	/** Enrolled servers for the mint Scope select — deterministic (name, id) order. */
	const enrolledServers = $derived(
		[...profiles].sort((a, b) => {
			if (a.name !== b.name) return a.name < b.name ? -1 : 1;
			if (a.server_id !== b.server_id) return a.server_id < b.server_id ? -1 : 1;
			return 0;
		})
	);

	function scopeName(serverId: string | null): string {
		if (serverId === null) return 'Global';
		return serverNames.get(serverId) ?? serverId;
	}

	// --- the bounded key list --------------------------------------------------

	let keys = $state<IngestKeyView[]>([]);
	let total = $state(0);
	let hasMore = $state(false);
	let loadingMore = $state(false);
	let listState = $state<KeyListState>({ status: 'loading' });
	/** The last non-initial failure (load more / revoke / refresh). */
	let actionProblem = $state<{ code: string; message: string } | null>(null);

	async function loadKeys(): Promise<void> {
		if (api === null) return;
		listState = { status: 'loading' };
		loadingMore = false;
		try {
			const page = await api.listIngestKeys({ limit: PAGE_LIMIT, offset: 0 });
			if (disposed) return;
			keys = page.keys;
			total = page.total;
			// The producer's envelope flag is the only has-more truth.
			hasMore = page.has_more;
			listState = { status: 'ready' };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			keys = [];
			total = 0;
			hasMore = false;
			listState = {
				status: 'error',
				code,
				message: friendlyText(code, messageOf(error) ?? 'The ingest keys could not be read.')
			};
		}
	}

	async function loadMore(): Promise<void> {
		if (api === null || listState.status !== 'ready' || loadingMore || !hasMore) return;
		loadingMore = true;
		actionProblem = null;
		try {
			const page = await api.listIngestKeys({ limit: PAGE_LIMIT, offset: keys.length });
			if (disposed) return;
			// Deduplicate by key_id: an overlapping page never duplicates rows.
			const seen = new Set(keys.map((row) => row.key_id));
			keys = [...keys, ...page.keys.filter((row) => !seen.has(row.key_id))];
			total = page.total;
			hasMore = page.has_more;
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			actionProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'More keys could not be read.')
			};
		} finally {
			if (!disposed) loadingMore = false;
		}
	}

	/**
	 * Durable page-0 refresh after a mint/revoke receipt: the list renders
	 * the server's truth, never a locally invented row state. Keeps the
	 * loaded page visible (no loading flash); a failure surfaces the
	 * envelope while the previous page stays.
	 */
	async function refreshKeys(): Promise<void> {
		if (api === null) return;
		try {
			const page = await api.listIngestKeys({ limit: PAGE_LIMIT, offset: 0 });
			if (disposed) return;
			keys = page.keys;
			total = page.total;
			hasMore = page.has_more;
			listState = { status: 'ready' };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			actionProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The key list could not be refreshed.')
			};
		}
	}

	// --- mint flow: one-time secret modal ---------------------------------------

	let newLabel = $state('');
	let newScope = $state('global');
	let newExpires = $state('');
	let createBusy = $state(false);
	let createProblem = $state<{ code: string; message: string } | null>(null);

	/** Producer bound mirrored for the guard (the server re-validates). */
	const labelValid = $derived(newLabel.trim().length >= 1 && newLabel.trim().length <= 64);

	async function submitCreate(): Promise<void> {
		if (api === null || createBusy || !labelValid) return;
		const label = newLabel.trim();
		const payload: CreateIngestKeyBody = { label };
		// Omitting `server_id` mints a GLOBAL key (§6); a scoped key carries
		// the explicit enrolled server id. The optional expiry rides only
		// when set, normalized to ISO UTC for the wire.
		if (newScope !== 'global') payload.server_id = newScope;
		if (newExpires !== '') payload.expires_at = new Date(newExpires).toISOString();
		// Capture the trigger focus BEFORE the busy gap: a loading (disabled)
		// button loses browser focus mid-flight, so the restore target must
		// be pinned before the await.
		captureTriggerFocus();
		createBusy = true;
		createProblem = null;
		try {
			const minted = await api.createIngestKey(payload);
			if (disposed) return;
			// The plaintext lives ONLY in this one state slot until close.
			openIssued(minted);
			newLabel = '';
			newScope = 'global';
			newExpires = '';
			await refreshKeys();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			createProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The key could not be minted.')
			};
		} finally {
			if (!disposed) createBusy = false;
		}
	}

	// --- the one-time secret modal ----------------------------------------------

	let issued = $state<IssuedIngestKeyView | null>(null);
	let copied = $state(false);
	let copyProblem = $state<string | null>(null);
	let issuedDialog = $state<HTMLDivElement | null>(null);
	/** Non-reactive local: the trigger focus is restored to on close. */
	let lastFocused: HTMLElement | null = null;

	// Focus moves INTO the dialog when it opens (the dialog is the first
	// stop; Tab reaches Copy, then Done).
	$effect(() => {
		if (issued !== null && issuedDialog !== null) issuedDialog.focus();
	});

	/** Pin the trigger focus for the close-path restore; runs pre-flight. */
	function captureTriggerFocus(): void {
		lastFocused =
			typeof document !== 'undefined' && document.activeElement instanceof HTMLElement
				? document.activeElement
				: null;
	}

	function openIssued(view: IssuedIngestKeyView): void {
		issued = view;
		copied = false;
		copyProblem = null;
	}

	function closeIssued(): void {
		// The plaintext leaves component state HERE — after this returns it
		// is absent from the DOM, and it was never persisted anywhere.
		issued = null;
		copied = false;
		copyProblem = null;
		lastFocused?.focus();
		lastFocused = null;
	}

	async function copyIssued(): Promise<void> {
		if (issued === null) return;
		try {
			await navigator.clipboard.writeText(issued.key);
			copied = true;
			copyProblem = null;
		} catch {
			copied = false;
			copyProblem = 'The clipboard refused the write — copy the highlighted text manually.';
		}
	}

	// --- revoke flow: inline confirm, receipt-driven flip -----------------------

	let revokeAskId = $state<string | null>(null);
	let revokeBusyId = $state<string | null>(null);

	function askRevoke(keyId: string): void {
		revokeAskId = keyId;
		actionProblem = null;
	}

	function cancelRevoke(): void {
		revokeAskId = null;
	}

	async function confirmRevoke(row: IngestKeyView): Promise<void> {
		if (api === null || revokeBusyId !== null) return;
		revokeBusyId = row.key_id;
		actionProblem = null;
		try {
			// The receipt only GATES the refetch; the row flips to the
			// server's own `revoked_at` when the durable page lands. The
			// idempotent `revoked: false` receipt covers unknown/already-
			// revoked keys — the refetch renders that truth equally.
			await api.revokeIngestKey(row.key_id);
			if (disposed) return;
			revokeAskId = null;
			await refreshKeys();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			actionProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The key could not be revoked.')
			};
		} finally {
			if (!disposed) revokeBusyId = null;
		}
	}

	// --- mount cycle (the $effect teardown is the release path) -----------------

	$effect(() => {
		if (api === null) return;
		disposed = false;
		// Started untracked so the effect depends only on the api bag and
		// never re-runs from its own refreshes.
		untrack(() => void loadKeys());
		return () => {
			disposed = true;
		};
	});
</script>

<div class="mai-keys" data-testid="keys-root">
	<header class="mai-keys__head">
		<h4 class="mai-keys__title">Ingest keys</h4>
		<p class="mai-keys__meta">
			Machine keys that publish into this inbox — label, scope and lifecycle metadata only.
			Key material is shown exactly once at mint time and never stored.
		</p>
		<div class="mai-keys__actions">
			<Button variant="secondary" size="sm" onclick={loadKeys} data-testid="keys-refresh">
				Refresh
			</Button>
		</div>
	</header>

	<section class="mai-keys__create" aria-label="Mint a new ingest key">
		<h5 class="mai-keys__subtitle">New ingest key</h5>
		<div class="mai-keys__create-row">
			<label class="mai-keys__field">
				Label
				<input
					type="text"
					bind:value={newLabel}
					maxlength={64}
					placeholder="e.g. host-monitor"
					data-testid="keys-new-label"
				/>
			</label>
			<Select
				label="Scope"
				hideLabel
				compact
				value={newScope}
				onchange={(event) => (newScope = event.currentTarget.value)}
				data-testid="keys-new-scope"
			>
				<option value="global">Global</option>
				{#each enrolledServers as profile (profile.server_id)}
					<option value={profile.server_id}>{profile.name}</option>
				{/each}
			</Select>
			<label class="mai-keys__field">
				Expires (optional)
				<input type="datetime-local" bind:value={newExpires} data-testid="keys-new-expires" />
			</label>
			<Button onclick={submitCreate} loading={createBusy} data-testid="keys-create">
				Create key
			</Button>
		</div>
		{#if createProblem !== null}
			<div
				class="mai-keys__state mai-keys__state--error"
				role="alert"
				data-testid="keys-create-error"
				data-error-code={createProblem.code}
			>
				<p>{createProblem.message}</p>
			</div>
		{/if}
	</section>

	{#if actionProblem !== null}
		<div
			class="mai-keys__state mai-keys__state--error"
			role="alert"
			data-testid="keys-problem"
			data-error-code={actionProblem.code}
		>
			<p>{actionProblem.message}</p>
		</div>
	{/if}

	{#if listState.status === 'loading' && keys.length === 0}
		<div class="mai-keys__state" role="status" data-testid="keys-loading">
			<p>Loading ingest keys…</p>
		</div>
	{:else if listState.status === 'error'}
		<div
			class="mai-keys__state mai-keys__state--error"
			role="alert"
			data-testid="keys-error"
			data-error-code={listState.code}
		>
			<p>{listState.message}</p>
			<Button variant="secondary" onclick={loadKeys} data-testid="keys-retry">Retry</Button>
		</div>
	{:else if keys.length === 0}
		<div class="mai-keys__state" role="status" data-testid="keys-empty">
			<p>
				No ingest keys yet. Mint one, copy the secret once, and hand it to the service that
				publishes into this inbox.
			</p>
		</div>
	{:else}
		<!-- Bounded scroll container: the loaded page grows INSIDE this box. -->
		<div class="mai-keys__scroll" data-testid="keys-scroll">
			<table class="mai-keys__table" data-testid="keys-table">
				<thead>
					<tr>
						<th scope="col">Label</th>
						<th scope="col">Scope</th>
						<th scope="col">Created</th>
						<th scope="col">Last used</th>
						<th scope="col">Expires</th>
						<th scope="col">Revoked</th>
						<th scope="col"><span class="mai-keys__sr">Actions</span></th>
					</tr>
				</thead>
				<tbody>
					{#each keys as row (row.key_id)}
						<tr
							data-testid="key-row"
							data-key-id={row.key_id}
							data-revoked={row.revoked_at !== null ? 'true' : 'false'}
						>
							<td data-testid="key-label">{row.label}</td>
							<td data-testid="key-scope">{scopeName(row.server_id)}</td>
							<td data-testid="key-created">{row.created_at}</td>
							<td data-testid="key-last-used">{row.last_used_at ?? '—'}</td>
							<td data-testid="key-expires">{row.expires_at ?? '—'}</td>
							<td data-testid="key-revoked">
								<span
									data-testid="key-status"
									data-revoked={row.revoked_at !== null ? 'true' : 'false'}
								>
									{row.revoked_at === null ? 'active' : 'revoked'}
								</span>
								<span data-testid="key-revoked-at">{row.revoked_at ?? '—'}</span>
							</td>
							<td class="mai-keys__row-actions">
								{#if row.revoked_at === null}
									{#if revokeAskId === row.key_id}
										<Button
											variant="danger"
											size="sm"
											loading={revokeBusyId === row.key_id}
											onclick={() => confirmRevoke(row)}
											data-testid="key-revoke-confirm"
										>
											Confirm revoke
										</Button>
										<Button
											variant="ghost"
											size="sm"
											onclick={cancelRevoke}
											data-testid="key-revoke-cancel"
										>
											Cancel
										</Button>
									{:else}
										<Button
											variant="secondary"
											size="sm"
											onclick={() => askRevoke(row.key_id)}
											data-testid="key-revoke"
										>
											Revoke
										</Button>
									{/if}
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
		{#if hasMore}
			<div class="mai-keys__actions">
				<Button
					variant="secondary"
					size="sm"
					loading={loadingMore}
					onclick={loadMore}
					data-testid="keys-load-more"
				>
					Load more ({keys.length} of {total})
				</Button>
			</div>
		{:else}
			<p class="mai-keys__meta" data-testid="keys-page-count">
				{keys.length} of {total} ingest keys loaded.
			</p>
		{/if}
	{/if}
</div>

<svelte:window
	onkeydown={(event) => {
		if (issued !== null && event.key === 'Escape') closeIssued();
	}}
/>

{#if issued !== null}
	<!-- One-time secret display: the ONLY place the plaintext ever renders. -->
	<div class="mai-keys__overlay">
		<div
			class="mai-keys__dialog"
			role="dialog"
			aria-modal="true"
			aria-labelledby="mai-keys-issued-title"
			tabindex="-1"
			bind:this={issuedDialog}
			data-testid="issued-key-modal"
		>
			<h5 id="mai-keys-issued-title" class="mai-keys__subtitle">
				Ingest key issued — copy it now
			</h5>
			<p class="mai-keys__warning" data-testid="issued-key-warning">
				This secret is shown ONLY ONCE and will not be shown again — store it in your password
				manager now. If it is lost, revoke this key and mint a new one.
			</p>
			<code class="mai-keys__secret" data-testid="issued-key-value">{issued.key}</code>
			<div class="mai-keys__actions">
				<Button
					variant="secondary"
					size="sm"
					onclick={copyIssued}
					data-testid="issued-key-copy"
				>
					Copy key
				</Button>
				{#if copied}
					<span class="mai-keys__copied" role="status" data-testid="issued-key-copied">
						Copied to clipboard.
					</span>
				{/if}
			</div>
			{#if copyProblem !== null}
				<p
					class="mai-keys__state mai-keys__state--error"
					role="alert"
					data-testid="issued-key-copy-error"
				>
					{copyProblem}
				</p>
			{/if}
			<p class="mai-keys__meta">Publish example — the placeholder stands for the key:</p>
			<pre class="mai-keys__curl" data-testid="issued-key-curl">{CURL_EXAMPLE}</pre>
			<div class="mai-keys__actions">
				<Button onclick={closeIssued} data-testid="issued-key-close">Done</Button>
			</div>
		</div>
	</div>
{/if}

<style>
	.mai-keys {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
		min-width: 0;
		font-size: var(--mai-font-size-sm, 0.875rem);
	}

	.mai-keys__head {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 0.25rem);
		margin: 0;
	}

	.mai-keys__title {
		margin: 0;
		font-size: var(--mai-font-size-md, 1rem);
	}

	.mai-keys__subtitle {
		margin: 0;
		font-size: var(--mai-font-size-sm, 0.875rem);
	}

	.mai-keys__meta {
		margin: 0;
		color: var(--mai-color-text-muted);
		overflow-wrap: anywhere;
	}

	.mai-keys__actions {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	/* Mint form: one row that wraps on narrow windows. */
	.mai-keys__create {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 0.25rem);
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm, 0.25rem);
		background: var(--mai-color-surface);
		padding: var(--mai-space-2, 0.5rem);
	}

	.mai-keys__create-row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
	}

	.mai-keys__field {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-1, 0.25rem);
		color: var(--mai-color-text-muted);
	}

	.mai-keys__field input {
		font: inherit;
		color: var(--mai-color-text);
		background: var(--mai-color-bg);
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm, 0.25rem);
		padding: 0 var(--mai-space-2, 0.5rem);
		height: 2rem;
	}

	.mai-keys__field input:focus-visible {
		outline: 2px solid var(--mai-color-brass, var(--mai-color-text));
		outline-offset: 1px;
	}

	/* Bounded scroll container (AGENTS.md pagination invariant). */
	.mai-keys__scroll {
		overflow-y: auto;
		max-height: 22rem;
		min-height: 0;
	}

	.mai-keys__table {
		width: 100%;
		border-collapse: collapse;
		font-size: var(--mai-font-size-xs, 0.75rem);
	}

	.mai-keys__table th {
		text-align: left;
		color: var(--mai-color-text-muted);
		font-weight: 600;
		border-bottom: 1px solid var(--mai-color-border);
		padding: var(--mai-space-1, 0.25rem) var(--mai-space-2, 0.5rem);
		white-space: nowrap;
	}

	.mai-keys__table td {
		border-bottom: 1px solid var(--mai-color-border);
		padding: var(--mai-space-1, 0.25rem) var(--mai-space-2, 0.5rem);
		vertical-align: top;
		overflow-wrap: anywhere;
	}

	.mai-keys__row-actions {
		white-space: nowrap;
		text-align: right;
	}

	.mai-keys__row-actions :global(.mai-button + .mai-button) {
		margin-left: var(--mai-space-1, 0.25rem);
	}

	.mai-keys__sr {
		position: absolute;
		width: 1px;
		height: 1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
	}

	.mai-keys__state {
		padding: var(--mai-space-2, 0.5rem);
		border-radius: var(--mai-radius-sm, 0.25rem);
		background: var(--mai-color-bg);
		color: var(--mai-color-text-muted);
	}

	.mai-keys__state p {
		margin: 0 0 var(--mai-space-1, 0.25rem);
	}

	.mai-keys__state--error {
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
		border: 1px solid var(--mai-status-danger-border);
	}

	/* One-time secret modal. */
	.mai-keys__overlay {
		position: fixed;
		inset: 0;
		z-index: 999;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgb(0 0 0 / 0.45);
		padding: var(--mai-space-4, 1rem);
	}

	.mai-keys__dialog {
		background: var(--mai-color-surface);
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-md, 0.5rem);
		max-width: 38rem;
		width: 100%;
		max-height: calc(100vh - 2rem);
		overflow-y: auto;
		padding: var(--mai-space-4, 1rem);
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
	}

	.mai-keys__dialog:focus {
		outline: none;
	}

	.mai-keys__warning {
		margin: 0;
		color: var(--mai-status-warning-text);
	}

	.mai-keys__secret {
		display: block;
		font-family: var(--mai-font-mono, ui-monospace, monospace);
		font-size: var(--mai-font-size-xs, 0.75rem);
		background: var(--mai-color-bg);
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm, 0.25rem);
		padding: var(--mai-space-2, 0.5rem);
		overflow-wrap: anywhere;
		user-select: all;
	}

	.mai-keys__copied {
		color: var(--mai-status-success-text);
	}

	.mai-keys__curl {
		margin: 0;
		font-family: var(--mai-font-mono, ui-monospace, monospace);
		font-size: var(--mai-font-size-xs, 0.75rem);
		background: var(--mai-color-bg);
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm, 0.25rem);
		padding: var(--mai-space-2, 0.5rem);
		overflow-x: auto;
		white-space: pre;
	}
</style>
