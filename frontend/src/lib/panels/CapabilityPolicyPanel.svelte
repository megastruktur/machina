<script module lang="ts">
	/**
	 * CapabilityPolicyPanel — reusable grants/approval-decision panel
	 * (card `mai-03-web-policy`, P03; service row `mai-web`, contracts
	 * C03/C08/C11).
	 *
	 * Layer: feature panel (windowed-workspace ADR-0001 §2 layer table). It owns
	 * its one domain view and nothing else:
	 *
	 *  - Explicit context only: a GLOBAL settings view (the entry declares
	 *    `scope_types: ['global']`). The grant configuration is global policy
	 *    with one revision counter (`mai.api.grants`); this module reads no
	 *    launcher/current-server value anywhere, so there is no hidden global
	 *    mutation target (ADR §3.2, NT5).
	 *  - Explicit services only: the policy API enters through
	 *    `panel.services.api` (the typed `CapabilityPolicyApi` shape owned by
	 *    the feature entry module, satisfied by `createMaiApiClient()` at
	 *    composition). A missing/incomplete bag fails closed with a visible
	 *    state — no service locator, no fallback client.
	 *  - Distinct control groups (brief acceptance 1): "Enabled tools (grants)"
	 *    (standing grant/revoke/deny controls over the versioned document) and
	 *    "Pending operation decisions (ask)" (the one-use approval decision)
	 *    are separate visible sections that never merge: a grant never approves
	 *    an operation and a decision never grants a tool. Ask/delegated
	 *    prompting itself is NOT stored here (C03 rule 5: a mode is a property
	 *    of each ExecutionContext, chosen per run by the host — `mai.api.grants`
	 *    explicitly excludes it from the frozen vocabulary), so the panel
	 *    displays the frozen semantics in a visible note instead of inventing a
	 *    mode toggle that could not persist.
	 *  - Safe preview (C03 rule 4): an approval is loaded by explicit id and
	 *    shows the exact target (capability x server), the binding digest
	 *    (payload SHA-256 — a digest, never payload bytes; the API cannot
	 *    return bytes), the bound policy revision, requester, timestamps and
	 *    the one-use status. Approving presents the previewed operation
	 *    binding together with the policy revision THIS panel observes —
	 *    exactly the presenter contract of `ApprovalStore.consume` — so a
	 *    policy change since the approval was requested is denied as
	 *    `revision_mismatch` and displayed, never silently consumed.
	 *  - Honest outcomes (brief acceptance 2, C08 rule 6): a consumed receipt is
	 *    rendered only from the decision response; denials (`already_consumed`,
	 *    `expired`, `*_mismatch`) are DISPLAYED and leave the approval pending —
	 *    no optimistic executed-success. Grant edits use the caller's observed
	 *    revision (`expected_revision`); a stale `conflict` is displayed and
	 *    blocks edits until an explicit Reload (no silent overwrite, no
	 *    auto-retry). Loading/empty (404 before the first commit)/error states
	 *    are always rendered; deny precedence over grants is shown, never
	 *    hidden.
	 *  - No window mechanics: no drag/geometry/focus/persistence code and no
	 *    workspace-state imports; the window host supplies all of that.
	 *  - No module-level state and no `window`/`document` access (ADR §5
	 *    rule 4): safe to evaluate per SSR request; instances share nothing.
	 *
	 * Tokens come only from ui/tokens.css; no dependency is added (ADR §4.3).
	 */
</script>

<script lang="ts">
	import { onDestroy } from 'svelte';
	import { Ban, FingerprintPattern, Scale, ShieldCheck } from 'lucide-svelte';
	import Button from '../ui/Button.svelte';
	import Field from '../ui/Field.svelte';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import type {
		ApprovalView,
		CapabilityPolicyApi,
		DecisionBody,
		GrantsConfigView,
		GrantsEditBody
	} from '../workspace/entries/03-web-policy.js';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readApi(services: PanelServices): CapabilityPolicyApi | null {
		const candidate = services.api as Partial<CapabilityPolicyApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (const name of ['readGrants', 'editGrants', 'readApproval', 'decideApproval'] as const) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as CapabilityPolicyApi;
	}

	const api = $derived(readApi(panel.services));
	/** Global policy view: a server-scoped mount contradicts the entry contract. */
	const globalScope = $derived(panel.scope.kind === 'global');
	const usable = $derived(api !== null && globalScope);

	// --- shapes mirroring the reviewed transport bounds (server re-validates) --

	const CAPABILITY_SLUG_RE = /^[a-z0-9][a-z0-9._-]{0,127}$/;
	const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

	// --- policy view state (C08 rule 6: never all-green on unavailable data) --

	type LoadState =
		| { status: 'loading' }
		/** 404 before the first commit: an honest empty state, never faked green. */
		| { status: 'empty' }
		| { status: 'error'; code: string; message: string }
		| { status: 'ready'; config: GrantsConfigView };

	let view = $state<LoadState>({ status: 'loading' });
	/** Set once the mount cycle ended (hide/close/error teardown); async
	 * continuations check it before touching state (ADR §7.2 release). */
	let disposed = true;
	/** True while any policy mutation is in flight (grants or deny list). */
	let editing = $state(false);
	/** Stale-edit conflict banner: displayed, never auto-overwritten. */
	let conflictMessage = $state<string | null>(null);
	/** Non-conflict save failure (session ended, unavailable, …). */
	let editProblem = $state<{ code: string; message: string; hint: string } | null>(null);
	/** Post-commit confirmation (kept until the next edit attempt or reload). */
	let savedNotice = $state<string | null>(null);

	/** The edit base: the committed document, or revision-0 emptiness (404). */
	const EMPTY_BASE: GrantsConfigView = {
		revision: 0,
		schema_revision: 1,
		sha256: '',
		policy: { revision: 0, denied_capabilities: [] },
		grants: []
	};
	const editBase = $derived(
		view.status === 'ready' ? view.config : view.status === 'empty' ? EMPTY_BASE : null
	);

	const grants = $derived(editBase?.grants ?? []);
	const deniedList = $derived(
		editBase ? [...editBase.policy.denied_capabilities].sort() : []
	);
	const deniedSet = $derived(new Set(deniedList));
	const revisionShown = $derived(view.status === 'ready' ? view.config.revision : null);
	const shaShown = $derived(
		view.status === 'ready' ? `${view.config.sha256.slice(0, 12)}…` : null
	);
	/** Any mutation control is blocked by an in-flight edit or a conflict hold. */
	const editsBlocked = $derived(editing || conflictMessage !== null);

	// --- grant/deny input state ----------------------------------------------

	let grantCapabilityInput = $state('');
	let grantServerInput = $state('');
	let grantInputError = $state('');
	let denyInput = $state('');
	let denyInputError = $state('');

	// --- approval decision state ---------------------------------------------

	type ApprovalState =
		| { status: 'idle' }
		| { status: 'loading' }
		| { status: 'error'; code: string; message: string }
		| { status: 'ready'; approval: ApprovalView };

	let approvalState = $state<ApprovalState>({ status: 'idle' });
	let approvalIdInput = $state('');
	let approvalIdError = $state('');
	/** True while the one-use decision POST is in flight. */
	let deciding = $state(false);
	/** A stable-code denial of the decision — displayed, approval unchanged. */
	let decisionDenied = $state<{ code: string; message: string } | null>(null);
	/** Non-denial decision failure (session ended, unavailable, …). */
	let approvalProblem = $state<{ code: string; message: string; hint: string } | null>(null);

	const approval = $derived(approvalState.status === 'ready' ? approvalState.approval : null);

	// --- error rendering helpers (value-free, C01 rule 3) ----------------------

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
		if (code === 'throttled') return 'Too many requests — try again shortly.';
		return fallback;
	}

	/** The frozen approval denial language (mirrors `mai.api.approvals`). */
	function denialText(code: string): string {
		if (code === 'already_consumed') {
			return 'This decision was already consumed — one approval authorizes exactly one operation.';
		}
		if (code === 'expired') return 'This approval expired before a decision was recorded.';
		if (code === 'capability_mismatch') {
			return 'The presented capability does not match the approval binding.';
		}
		if (code === 'target_mismatch') return 'The presented target does not match the approval binding.';
		if (code === 'payload_mismatch') {
			return 'The operation changed since this approval was requested — a new approval is required.';
		}
		if (code === 'revision_mismatch') {
			return 'The policy changed since this approval was requested — a new approval is required.';
		}
		return 'The decision was rejected; the approval is unchanged.';
	}

	// --- policy load / edit ---------------------------------------------------

	/** Full load: shows the loading state and resyncs every control. */
	async function loadPolicy(): Promise<void> {
		if (api === null) return;
		view = { status: 'loading' };
		try {
			const config = await api.readGrants();
			if (disposed) return;
			view = { status: 'ready', config };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			if (code === 'not_found') {
				// Before the first commit the API reports 404 (C08 rule 6): an
				// explicit empty state — revision-0 edits below can create it.
				view = { status: 'empty' };
				return;
			}
			view = {
				status: 'error',
				code,
				message: friendlyText(
					code,
					messageOf(error) ?? 'The capability policy could not be loaded.'
				)
			};
		}
	}

	/**
	 * Silent re-read after a successful commit: refreshes the authoritative
	 * document without flickering the controls. On failure the optimistic
	 * post-commit revision from the mutation result stays visible.
	 */
	async function refreshPolicy(): Promise<void> {
		if (api === null) return;
		try {
			const config = await api.readGrants();
			if (disposed) return;
			view = { status: 'ready', config };
		} catch {
			// The commit already succeeded; the current view reflects it.
		}
	}

	/** The edit body always carries the caller's observed revision (C01 rule 6). */
	function bodyFrom(base: GrantsConfigView): GrantsEditBody {
		return {
			expected_revision: base.revision,
			denied_capabilities: [...base.policy.denied_capabilities],
			grants: base.grants.map((entry) => ({
				capability_id: entry.capability_id,
				server_ids: [...entry.server_ids]
			}))
		};
	}

	async function commitEdit(next: GrantsEditBody, notice: string): Promise<void> {
		if (api === null || editBase === null || editsBlocked) return;
		editing = true;
		editProblem = null;
		savedNotice = null;
		try {
			await api.editGrants(next);
			if (disposed) return;
			savedNotice = notice;
			await refreshPolicy();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error);
			if (code === 'conflict') {
				// Brief acceptance 2: DISPLAY the conflict. No retry with a fresh
				// revision (that would overwrite the other writer's change); the
				// controls stay blocked until an explicit Reload re-syncs.
				conflictMessage =
					'The policy changed since this window last loaded — your change was not applied. Reload to review the committed policy; nothing was written.';
			} else {
				const resolved = code ?? 'unavailable';
				editProblem = {
					code: resolved,
					message:
						messageOf(error) ?? friendlyText(resolved, 'The policy could not be saved.'),
					hint: resolved === 'unauthenticated' ? ' Sign in again, then retry.' : ''
				};
			}
		} finally {
			if (!disposed) editing = false;
		}
	}

	/** Revoke one server from one grant (drop the entry when the last goes). */
	function revokeGrant(capabilityId: string, serverId: string): void {
		if (editBase === null) return;
		const next = bodyFrom(editBase);
		next.grants = (next.grants ?? [])
			.map((entry) =>
				entry.capability_id === capabilityId
					? { ...entry, server_ids: entry.server_ids.filter((id) => id !== serverId) }
					: entry
			)
			.filter((entry) => entry.server_ids.length > 0);
		void commitEdit(next, `Revoked ${capabilityId} on ${serverId}.`);
	}

	/** Grant one capability on one server (new entry or scope extension). */
	function addGrant(): void {
		if (editBase === null) return;
		const capability = grantCapabilityInput.trim();
		const server = grantServerInput.trim();
		grantInputError = '';
		if (!CAPABILITY_SLUG_RE.test(capability)) {
			grantInputError = 'Capability must be a lowercase slug (letters, digits, . _ -).';
			return;
		}
		if (!UUID_RE.test(server)) {
			grantInputError = 'A server id is an opaque UUID.';
			return;
		}
		const next = bodyFrom(editBase);
		const existing = (next.grants ?? []).find((entry) => entry.capability_id === capability);
		if (existing) {
			if (existing.server_ids.includes(server)) {
				grantInputError = 'That server is already granted for this capability.';
				return;
			}
			existing.server_ids.push(server);
		} else {
			(next.grants ??= []).push({ capability_id: capability, server_ids: [server] });
		}
		void commitEdit(next, `Granted ${capability} on ${server}.`);
	}

	/** Add one capability to the policy deny list (deny wins everywhere). */
	function denyCapability(): void {
		if (editBase === null) return;
		const capability = denyInput.trim();
		denyInputError = '';
		if (!CAPABILITY_SLUG_RE.test(capability)) {
			denyInputError = 'Capability must be a lowercase slug (letters, digits, . _ -).';
			return;
		}
		if (editBase.policy.denied_capabilities.includes(capability)) {
			denyInputError = 'That capability is already denied.';
			return;
		}
		const next = bodyFrom(editBase);
		next.denied_capabilities = [...(next.denied_capabilities ?? []), capability];
		void commitEdit(next, `${capability} is now denied everywhere.`);
	}

	/** Remove one capability from the deny list (a grant still gates execution). */
	function allowCapability(capabilityId: string): void {
		if (editBase === null) return;
		const next = bodyFrom(editBase);
		next.denied_capabilities = (next.denied_capabilities ?? []).filter(
			(entry) => entry !== capabilityId
		);
		void commitEdit(next, `Removed ${capabilityId} from the deny list.`);
	}

	/** Explicit re-sync: the only path that clears a stale-edit conflict. */
	function reloadPolicy(): void {
		conflictMessage = null;
		editProblem = null;
		savedNotice = null;
		void loadPolicy();
	}

	// --- approval lookup + one-use decision (C03 rule 4) ----------------------

	/** Load the safe preview for one explicitly entered approval id. */
	async function loadApproval(): Promise<void> {
		if (api === null) return;
		const id = approvalIdInput.trim();
		approvalIdError = '';
		if (!UUID_RE.test(id)) {
			approvalIdError = 'An approval id is an opaque UUID.';
			return;
		}
		approvalState = { status: 'loading' };
		decisionDenied = null;
		approvalProblem = null;
		try {
			const loaded = await api.readApproval(id);
			if (disposed) return;
			approvalState = { status: 'ready', approval: loaded };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			approvalState = {
				status: 'error',
				code,
				message:
					code === 'not_found'
						? 'No approval record exists for this id.'
						: friendlyText(
								code,
								messageOf(error) ?? 'The approval could not be loaded.'
							)
			};
		}
	}

	/**
	 * Spend the one-use decision by echoing the previewed binding
	 * field-for-field. The consumed receipt is rendered only from the
	 * response — never optimistically (brief acceptance 2).
	 */
	async function approveOperation(): Promise<void> {
		if (approval === null || api === null || deciding) return;
		if (approval.status !== 'pending') return;
		// The presented binding (C03 rule 4, `ApprovalStore.consume`): the
		// operation fields come from the previewed approval; the policy
		// revision is the one THIS panel currently observes — so any policy
		// change since the approval was requested is denied as
		// `revision_mismatch` and displayed instead of silently consumed.
		const currentRevision = editBase?.revision ?? 0;
		if (currentRevision < 1) {
			approvalProblem = {
				code: 'request_rejected',
				message: 'The committed policy revision is unknown — reload before deciding.',
				hint: ''
			};
			return;
		}
		const binding: DecisionBody = {
			capability_id: approval.capability_id,
			server_id: approval.server_id,
			payload_hash: approval.payload_hash,
			policy_revision: currentRevision
		};
		deciding = true;
		decisionDenied = null;
		approvalProblem = null;
		try {
			const receipt = await api.decideApproval(approval.approval_id, binding);
			if (disposed) return;
			approvalState = { status: 'ready', approval: receipt };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error);
			const resolved = code ?? 'unavailable';
			if (resolved === 'unauthenticated' || resolved === 'unavailable' || resolved === 'throttled') {
				approvalProblem = {
					code: resolved,
					message:
						messageOf(error) ??
						friendlyText(resolved, 'The decision could not be submitted.'),
					hint: resolved === 'unauthenticated' ? ' Sign in again, then retry.' : ''
				};
			} else {
				decisionDenied = { code: resolved, message: denialText(resolved) };
			}
		} finally {
			if (!disposed) deciding = false;
		}
	}

	/** Re-run the last lookup (recovers from transport/session failures). */
	function retryApproval(): void {
		void loadApproval();
	}

	// Mount reads once; the teardown of this effect is the panel's release
	// path (hide/unmount/error containment) — in-flight responses observe
	// `disposed` and never write afterwards (ADR §7.2).
	$effect(() => {
		if (!usable) return;
		disposed = false;
		void loadPolicy();
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

<div class="mai-policy" data-testid="policy-root">
	{#if !usable}
		<div
			class="mai-policy__state mai-policy__state--error"
			role="alert"
			data-testid="panel-config-error"
		>
			<p>This panel was opened without its policy service or outside a global scope.</p>
		</div>
	{:else if view.status === 'loading'}
		<div class="mai-policy__state" role="status" data-testid="policy-loading">
			<p>Loading capability policy…</p>
		</div>
	{:else if view.status === 'error'}
		<div
			class="mai-policy__state mai-policy__state--error"
			role="alert"
			data-testid="policy-error"
			data-error-code={view.code}
		>
			<p>{view.message}</p>
			<Button variant="secondary" onclick={loadPolicy} data-testid="policy-retry">
				Retry
			</Button>
		</div>
	{:else}
		<header class="mai-policy__header">
			{#if revisionShown !== null}
				<p class="mai-policy__revision" data-testid="policy-revision">
					Policy revision
					<code class="mai-policy__chip mai-policy__chip--rev">{revisionShown}</code>
					{#if shaShown}
						<code class="mai-policy__chip">sha256 {shaShown}</code>
					{/if}
				</p>
			{:else}
				<p
					class="mai-policy__revision mai-policy__revision--empty"
					data-testid="policy-empty"
					role="status"
				>
					No capability policy has been committed yet — the controls below create revision 1.
				</p>
			{/if}
			{#if savedNotice}
				<p class="mai-policy__notice" role="status" data-testid="policy-saved-notice">
					{savedNotice}
				</p>
			{/if}
			{#if conflictMessage}
				<div class="mai-policy__banner" role="alert" data-testid="policy-conflict-banner">
					<p>{conflictMessage}</p>
					<Button variant="secondary" onclick={reloadPolicy} data-testid="policy-reload">
						Reload
					</Button>
				</div>
			{/if}
			{#if editProblem}
				<div class="mai-policy__banner" role="alert" data-testid="policy-edit-error" data-error-code={editProblem.code}>
					<p>{editProblem.message}{editProblem.hint}</p>
				</div>
			{/if}
		</header>

		<!-- Control group 1: standing permission (enabled tools). -->
		<section class="mai-policy__section" aria-label="Enabled tools (grants)" data-testid="grants-section">
			<div class="mai-policy__section-head">
				<ShieldCheck size={15} aria-hidden="true" />
				<h3 class="mai-policy__heading">Enabled tools (grants)</h3>
				{#if revisionShown !== null}
					<code class="mai-policy__chip mai-policy__chip--rev mai-policy__section-rev">
						rev {revisionShown}
					</code>
				{/if}
			</div>
			<p class="mai-policy__note" data-testid="mode-note">
				Ask/delegated prompting is chosen per run by the agent host (C03 rule 5) and is not
				stored in this policy: privileged operations always ask for a decision, read-only
				operations never ask, and other operations ask only in ask mode. A grant below is a
				standing permission — it never approves an operation, and an approval decision never
				grants a tool.
			</p>

			{#if grants.length === 0}
				<p class="mai-policy__muted" data-testid="grants-empty">No tools are enabled.</p>
			{:else}
				<ul class="mai-policy__grants" data-testid="grant-list">
					{#each grants as grant (grant.capability_id)}
						<li class="mai-policy__grant" data-testid="grant-row">
							<div class="mai-policy__grant-head">
								<span class="mai-policy__capability" data-testid="grant-capability">
									{grant.capability_id}
								</span>
								{#if deniedSet.has(grant.capability_id)}
									<span class="mai-policy__badge" data-testid="grant-denied-badge" role="status">
										denied by policy (takes precedence)
									</span>
								{/if}
							</div>
							<ul class="mai-policy__servers">
								{#each grant.server_ids as serverId (serverId)}
									<li class="mai-policy__server" data-testid="grant-server">
										<code class="mai-policy__chip">{serverId}</code>
										<Button
											variant="danger"
											disabled={editsBlocked}
											aria-label="Revoke server {serverId} from {grant.capability_id}"
											onclick={() => revokeGrant(grant.capability_id, serverId)}
										>
											Revoke
										</Button>
									</li>
								{/each}
							</ul>
						</li>
					{/each}
				</ul>
			{/if}

			<form
				class="mai-policy__form"
				aria-label="Grant a tool"
				onsubmit={(event) => {
					event.preventDefault();
					addGrant();
				}}
			>
				<Field
					label="Capability"
					bind:value={grantCapabilityInput}
					error={grantInputError}
					data-testid="grant-capability-input"
				/>
				<Field
					label="Server id"
					bind:value={grantServerInput}
					hint="Opaque UUID of the adopted target server."
					data-testid="grant-server-input"
				/>
				<Button type="submit" disabled={editsBlocked} data-testid="grant-add">
					Grant tool
				</Button>
			</form>
		</section>

		<section class="mai-policy__section" aria-label="Disabled tools (deny list)" data-testid="deny-section">
			<div class="mai-policy__section-head">
				<Ban size={15} aria-hidden="true" />
				<h3 class="mai-policy__heading">Disabled tools (deny list)</h3>
			</div>
			<p class="mai-policy__note">
				A denied capability is refused everywhere — deny takes precedence over any grant,
				registration or mode (C03 rule 5).
			</p>
			{#if deniedList.length === 0}
				<p class="mai-policy__muted" data-testid="deny-empty">No tools are disabled.</p>
			{:else}
				<ul class="mai-policy__denied" data-testid="deny-list">
					{#each deniedList as capability (capability)}
						<li class="mai-policy__deny-row" data-testid="deny-row">
							<span class="mai-policy__capability" data-testid="deny-capability">
								{capability}
							</span>
							<Button
								variant="secondary"
								disabled={editsBlocked}
								aria-label="Remove {capability} from the deny list"
								onclick={() => allowCapability(capability)}
							>
								Allow
							</Button>
						</li>
					{/each}
				</ul>
			{/if}
			<form
				class="mai-policy__form"
				aria-label="Deny a tool"
				onsubmit={(event) => {
					event.preventDefault();
					denyCapability();
				}}
			>
				<Field
					label="Capability to deny"
					bind:value={denyInput}
					error={denyInputError}
					data-testid="deny-input"
				/>
				<Button variant="danger" type="submit" disabled={editsBlocked} data-testid="deny-add">
					Deny tool
				</Button>
			</form>
		</section>

		<!-- Control group 2: the ask path (one bound operation at a time). -->
		<section
			class="mai-policy__section"
			aria-label="Pending operation decisions (ask)"
			data-testid="approvals-section"
		>
			<div class="mai-policy__section-head">
				<Scale size={15} aria-hidden="true" />
				<h3 class="mai-policy__heading">Pending operation decisions (ask)</h3>
			</div>
			<p class="mai-policy__note">
				An approval binds one operation's payload hash, exact server target, scope and policy
				revision with an expiry (C03 rule 4). Approving presents that operation binding under
				the policy revision this window currently observes (shown in the header): it spends
				the decision once for exactly that binding, and any policy change, expiry or replay
				is denied and displayed.
			</p>
			<form
				class="mai-policy__form"
				aria-label="Look up an approval"
				onsubmit={(event) => {
					event.preventDefault();
					void loadApproval();
				}}
			>
				<Field
					label="Approval id"
					bind:value={approvalIdInput}
					error={approvalIdError}
					hint="The opaque id of the pending operation awaiting a decision."
					data-testid="approval-id-input"
				/>
				<Button type="submit" disabled={deciding} data-testid="approval-load">
					Load approval
				</Button>
			</form>

			{#if approvalState.status === 'loading'}
				<div class="mai-policy__state" role="status" data-testid="approval-loading">
					<p>Loading approval…</p>
				</div>
			{:else if approvalState.status === 'error'}
				<div
					class="mai-policy__state mai-policy__state--error"
					role="alert"
					data-testid="approval-error"
					data-error-code={approvalState.code}
				>
					<p>{approvalState.message}</p>
					<Button variant="secondary" onclick={retryApproval} data-testid="approval-retry">
						Retry
					</Button>
				</div>
			{:else if approval}
				<div class="mai-policy__preview" data-testid="approval-preview">
					<div class="mai-policy__preview-head">
						<FingerprintPattern size={14} aria-hidden="true" />
						<p class="mai-policy__target" data-testid="approval-target">
							Operation: <strong>{approval.capability_id}</strong> on server
							<code>{approval.server_id}</code>
						</p>
					</div>
					<dl class="mai-policy__rows">
						<div class="mai-policy__row">
							<dt>Payload hash</dt>
							<dd><code data-testid="approval-payload-hash">{approval.payload_hash}</code></dd>
						</div>
						<div class="mai-policy__row">
							<dt>Bound policy revision</dt>
							<dd class="mai-policy__chip" data-testid="approval-policy-revision">
								{approval.policy_revision}
							</dd>
						</div>
						<div class="mai-policy__row">
							<dt>Status</dt>
							<dd data-testid="approval-status">{approval.status}</dd>
						</div>
						<div class="mai-policy__row">
							<dt>Requested by</dt>
							<dd data-testid="approval-created-by">{approval.created_by}</dd>
						</div>
						<div class="mai-policy__row">
							<dt>Created</dt>
							<dd data-testid="approval-created-at">{approval.created_at}</dd>
						</div>
						<div class="mai-policy__row">
							<dt>Expires</dt>
							<dd data-testid="approval-expires-at">{approval.expires_at}</dd>
						</div>
						{#if approval.consumed_by !== null}
							<div class="mai-policy__row">
								<dt>Consumed by</dt>
								<dd data-testid="approval-consumed-by">{approval.consumed_by}</dd>
							</div>
							<div class="mai-policy__row">
								<dt>Consumed at</dt>
								<dd data-testid="approval-consumed-at">{approval.consumed_at}</dd>
							</div>
						{/if}
					</dl>
					{#if approval.status === 'pending'}
						<Button
							disabled={deciding}
							loading={deciding}
							onclick={approveOperation}
							data-testid="approval-decide"
						>
							Approve this operation
						</Button>
					{/if}
				</div>
			{/if}

			{#if decisionDenied}
				<div class="mai-policy__banner" role="alert" data-testid="approval-denial" data-error-code={decisionDenied.code}>
					<p>{decisionDenied.message}</p>
				</div>
			{/if}
			{#if approvalProblem}
				<div class="mai-policy__banner" role="alert" data-testid="approval-problem" data-error-code={approvalProblem.code}>
					<p>{approvalProblem.message}{approvalProblem.hint}</p>
				</div>
			{/if}
		</section>
	{/if}
</div>

<style>
	.mai-policy {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-4);
		height: 100%;
		padding: var(--mai-space-4);
		background: var(--mai-color-bg);
		color: var(--mai-color-text);
		font-family: var(--mai-font-sans);
		font-size: var(--mai-text-sm);
		line-height: var(--mai-leading-normal);
		overflow-y: auto;
	}

	/* Status plates: loading / error / fail-closed mounts. */
	.mai-policy__state {
		padding: var(--mai-space-4);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface-raised);
		color: var(--mai-color-text-muted);
	}

	.mai-policy__state--error {
		border-color: var(--mai-status-danger-border);
		background: var(--mai-status-danger-bg);
		color: var(--mai-status-danger-text);
	}

	/* Doctrine header band: observed policy revision under a brass rule. */
	.mai-policy__header {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-space-3) var(--mai-space-4);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface-header);
	}

	.mai-policy__revision {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--mai-space-2);
		margin: 0;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}

	.mai-policy__revision--empty {
		display: block;
		font-family: var(--mai-font-sans);
		font-size: var(--mai-text-sm);
	}

	/* Revision/digest chips: rev, sha256, server ids, binding revision. */
	.mai-policy__chip {
		display: inline-flex;
		align-items: center;
		padding: 0 var(--mai-space-2);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-surface);
		color: var(--mai-color-text);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		line-height: var(--mai-leading-tight);
		transition:
			border-color var(--mai-motion-fast) ease,
			color var(--mai-motion-fast) ease;
	}

	.mai-policy__chip--rev {
		border-color: var(--mai-color-brass-dim);
		color: var(--mai-color-brass);
	}

	.mai-policy__notice {
		margin: 0;
		padding: var(--mai-space-2) var(--mai-space-3);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-status-success-bg);
		color: var(--mai-status-success-text);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
	}

	.mai-policy__banner {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-space-3);
		border: var(--mai-border-width) solid var(--mai-status-warning-border);
		border-left: 3px solid var(--mai-status-warning-solid);
		border-radius: var(--mai-radius-md);
		background: var(--mai-status-warning-bg);
		color: var(--mai-status-warning-text);
	}

	.mai-policy__banner p {
		margin: 0;
	}

	/* Doctrine section cards. */
	.mai-policy__section {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		padding: var(--mai-space-4);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-lg);
		background: var(--mai-color-surface-raised);
		box-shadow: var(--mai-shadow-window);
	}

	.mai-policy__section-head {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		margin: calc(-1 * var(--mai-space-4)) calc(-1 * var(--mai-space-4)) 0;
		padding: var(--mai-space-2) var(--mai-space-4) var(--mai-space-2) var(--mai-space-3);
		border-bottom: var(--mai-border-width) solid var(--mai-color-border);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-lg) var(--mai-radius-lg) 0 0;
		background: var(--mai-color-surface-header);
		color: var(--mai-color-brass);
	}

	.mai-policy__section-head :global(svg) {
		flex: 0 0 auto;
	}

	.mai-policy__heading {
		margin: 0;
		color: var(--mai-color-brass);
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-base);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		line-height: var(--mai-leading-tight);
		text-transform: uppercase;
	}

	.mai-policy__section-rev {
		margin-left: auto;
	}

	.mai-policy__note {
		margin: 0;
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-sm);
	}

	.mai-policy__muted {
		margin: 0;
		color: var(--mai-color-text-muted);
	}

	.mai-policy__badge {
		padding: 0 var(--mai-space-2);
		border: var(--mai-border-width) solid var(--mai-status-warning-border);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-status-warning-bg);
		color: var(--mai-status-warning-text);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
	}

	/* Grants matrix: one raised-edge row per capability, servers beneath. */
	.mai-policy__grants,
	.mai-policy__servers,
	.mai-policy__denied {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.mai-policy__grant {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-space-3);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface);
	}

	.mai-policy__grant-head {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--mai-space-2);
	}

	.mai-policy__capability {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-sm);
	}

	.mai-policy__servers {
		padding-left: var(--mai-space-3);
		border-left: 3px solid var(--mai-color-brass-dim);
	}

	.mai-policy__server {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
	}

	.mai-policy__deny-row {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		padding: var(--mai-space-2) var(--mai-space-3);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-left: 3px solid var(--mai-status-danger-border);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface);
	}

	.mai-policy__form {
		display: flex;
		flex-wrap: wrap;
		align-items: end;
		gap: var(--mai-space-3);
		padding-top: var(--mai-space-3);
		border-top: var(--mai-border-width) solid var(--mai-color-border);
	}

	.mai-policy__form :global(.mai-field) {
		flex: 1 1 14rem;
	}

	/* Approval binding card, presented under the observed revision. */
	.mai-policy__preview {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		padding: var(--mai-space-3) var(--mai-space-4);
		border: var(--mai-border-width) solid var(--mai-color-border-strong);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface);
	}

	.mai-policy__preview-head {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		color: var(--mai-color-brass);
	}

	.mai-policy__preview-head :global(svg) {
		flex: 0 0 auto;
	}

	.mai-policy__target {
		margin: 0;
		color: var(--mai-color-text);
		font-size: var(--mai-text-base);
		font-weight: var(--mai-weight-medium);
	}

	.mai-policy__target code {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-sm);
	}

	.mai-policy__rows {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		margin: 0;
	}

	.mai-policy__row {
		display: flex;
		align-items: baseline;
		gap: var(--mai-space-3);
	}

	.mai-policy__row dt {
		flex: 0 0 11rem;
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-sm);
	}

	.mai-policy__row dd {
		margin: 0;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-sm);
		overflow-wrap: anywhere;
	}

	.mai-policy__row dd.mai-policy__chip {
		align-self: center;
		font-size: var(--mai-text-xs);
	}
</style>
