<script module lang="ts">
	/**
	 * ExecutionPanel — reusable run-submission/preview panel with the ephemeral
	 * sudo password prompt (card `mai-05-web-execution`, P05; service row
	 * `mai-web`, contracts C04/C05/C08/C11).
	 *
	 * Layer: feature panel (windowed-workspace ADR-0001 §2 layer table). It owns
	 * its one domain view and nothing else:
	 *
	 *  - Explicit context only: a SERVER execution view. Every operation this
	 *    panel submits names the target pinned at open time
	 *    (`panel.scope.server_id`, C01 rule 2); the module reads no
	 *    launcher/current-server value anywhere, so there is no hidden global
	 *    mutation target and a launcher re-selection can never retarget a
	 *    tracked run or a bound password challenge (C08 rule 5, NT5).
	 *  - Explicit services only: the six-function `ExecutionApi` enters through
	 *    `panel.services.api` (the typed bag owned by the feature entry module,
	 *    satisfied by `createMaiApiClient()` at composition). A missing or
	 *    incomplete bag fails closed with a visible state — no service locator,
	 *    no fallback client.
	 *  - HTTP never executes (C05): the panel submits ONE durable run intent
	 *    (client-minted `run_id` idempotency key, C05 rule 5) for the
	 *    `host.shell` capability and then renders the REPLAYED facade state and
	 *    durable step receipts. It starts, cancels and retargets nothing else;
	 *    the concrete SSE consumer stays `05-web-jobs`' exclusive resource.
	 *  - Honest outcomes (brief acceptance 1, C04 rule 4, host rule 3): the
	 *    target, the privilege request, step receipts (including output
	 *    truncation with omitted-byte counts) and every unproven state are
	 *    rendered as recorded. `outcome_unknown` says "outcome unknown —
	 *    reconciliation required"; a cancellation is "requested — not yet
	 *    verified" until the replayed run state reports `cancelled` (guard
	 *    rule 3: requested ≠ verified). There is no optimistic success, ever.
	 *  - Ephemeral secret hygiene (brief acceptance 2): the password field
	 *    exists ONLY while a bound challenge is present (the tracked run waits
	 *    on an approval whose receipt names it, and the pinned target's
	 *    privilege request is `sudo`). The typed secret goes once to
	 *    `/api/v1/password-challenges` bound field-for-field to that approval
	 *    (C03 rule 4), is cleared from the form state whatever the outcome
	 *    (denial, spend, success, teardown), is never rendered as text, never
	 *    written to any storage, and never included in a displayed transcript
	 *    (the route's response shape carries no submission echo by contract).
	 *    A deny/decide control is NOT duplicated here: approving or denying the
	 *    operation belongs to the capability-policy panel; this panel only
	 *    transports the sudo secret for the already-bound challenge.
	 *  - No window mechanics: no drag/geometry/focus/persistence code and no
	 *    workspace-state imports; the window host supplies all of that. The
	 *    poll timer is instance-scoped controller state, released on unmount
	 *    and on hide (the `$effect` teardown is the release path), and never
	 *    survives into another mount cycle.
	 *  - No module-level state and no `window`/`document`/storage access
	 *    (ADR §5 rule 4): safe to evaluate per SSR request; instances share
	 *    nothing between requests or windows.
	 *
	 * Tokens come only from ui/tokens.css; no dependency is added (ADR §4.3).
	 */
</script>

<script lang="ts">
	import { onDestroy } from 'svelte';
	import Button from '../ui/Button.svelte';
	import Field from '../ui/Field.svelte';
	import {
		Cog,
		KeyRound,
		ScrollText,
		Server,
		ShieldCheck,
		Terminal,
		TriangleAlert
	} from 'lucide-svelte';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import { SHELL_CAPABILITY_ID } from '../workspace/entries/05-web-execution.js';
	import type {
		ApprovalView,
		CancelView,
		ChallengeBody,
		ChallengeOutcomeView,
		ExecutionApi,
		RunDetail,
		RunSubmitBody
	} from '../workspace/entries/05-web-execution.js';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readApi(services: PanelServices): ExecutionApi | null {
		const candidate = services.api as Partial<ExecutionApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (
			const name of [
				'readProfile',
				'submitRun',
				'readRun',
				'cancelRun',
				'readApproval',
				'submitChallenge'
			] as const
		) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as ExecutionApi;
	}

	const api = $derived(readApi(panel.services));
	/** Execution view: a global mount contradicts the entry contract. */
	const serverScope = $derived(panel.scope.kind === 'server' ? panel.scope.server_id : null);
	const usable = $derived(api !== null && serverScope !== null);

	// --- shapes mirroring the reviewed producer bounds (server re-validates) --

	const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
	const PAYLOAD_HASH_RE = /^[0-9a-f]{64}$/;
	/** CommandRequest bounds (mai.execution.commands); the server re-validates. */
	const MAX_TIMEOUT_SECONDS = 3600;
	const MAX_OUTPUT_BUDGET_BYTES = 8 * 1024 * 1024;
	/** host.shell defaults (payload values are submitted explicitly so the
	 * bound-challenge echo never depends on an assumed default). */
	const DEFAULT_TIMEOUT_SECONDS = '120';
	const DEFAULT_OUTPUT_BUDGET = '65536';
	/** Facade run metadata: a direct UI execution selects no model profile. */
	const DIRECT_MODEL_PROFILE = 'direct';
	/** Poll cadence for the replayed run state (bounded, instance-scoped). */
	const POLL_MS = 1500;

	/** Run lifecycle states after which nothing new can be learned (C05 rule 2). */
	const TERMINAL_RUN_STATES: ReadonlySet<string> = new Set(['succeeded', 'failed', 'cancelled']);

	// --- value-free display helpers (C08 rule 6) -----------------------------

	const RUN_STATE_TEXT: Readonly<Record<string, string>> = Object.freeze({
		queued: 'Queued',
		running: 'Running',
		waiting_approval: 'Waiting for approval decision',
		waiting_children: 'Waiting for child runs',
		reconciling: 'Reconciling an unproven outcome',
		succeeded: 'Succeeded',
		failed: 'Failed',
		cancelled: 'Cancelled (verified)'
	});

	const STEP_STATE_TEXT: Readonly<Record<string, string>> = Object.freeze({
		pending: 'Pending',
		running: 'Running',
		succeeded: 'Succeeded',
		failed: 'Failed',
		outcome_unknown: 'Outcome unknown — reconciliation required',
		cancel_requested: 'Cancellation requested — not yet verified',
		cancelled: 'Cancelled (host-verified)'
	});

	const OUTCOME_STATUS_TEXT: Readonly<Record<string, string>> = Object.freeze({
		succeeded: 'Succeeded',
		failed: 'Failed',
		running: 'Still running on the target',
		outcome_unknown: 'Outcome unknown — reconciliation required',
		cancel_requested: 'Cancellation requested — not yet verified',
		cancelled: 'Cancelled (host-verified)'
	});

	function runStateText(state: string | null): string {
		if (state === null) return 'State not recorded yet';
		return RUN_STATE_TEXT[state] ?? `Recorded state: ${state}`;
	}

	function stepStateText(state: string): string {
		return STEP_STATE_TEXT[state] ?? `Recorded state: ${state}`;
	}

	function outcomeStatusText(status: string): string {
		return OUTCOME_STATUS_TEXT[status] ?? `Recorded status: ${status}`;
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
		if (code === 'throttled') return 'Too many requests — try again shortly.';
		if (code === 'request_rejected') return 'The request was rejected; nothing was changed.';
		return fallback;
	}

	// --- target view state (C08 rule 6: never all-green on unavailable data) --

	type TargetState =
		| { status: 'loading' }
		| { status: 'error'; code: string; message: string }
		| { status: 'ready'; profile: ProfileResponseView };

	/** Narrow generated profile type re-export (kept local to the instance). */
	type ProfileResponseView = Awaited<ReturnType<ExecutionApi['readProfile']>>;

	let target = $state<TargetState>({ status: 'loading' });
	/** Set once the mount cycle ended; async continuations check before writes. */
	let disposed = true;

	async function loadTarget(): Promise<void> {
		if (api === null || serverScope === null) return;
		target = { status: 'loading' };
		try {
			const profile = await api.readProfile(serverScope);
			if (disposed) return;
			target = { status: 'ready', profile };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			target = {
				status: 'error',
				code,
				message: friendlyText(code, messageOf(error) ?? 'The target profile could not be loaded.')
			};
		}
	}

	// --- command form state ---------------------------------------------------

	let commandInput = $state('');
	let timeoutInput = $state(DEFAULT_TIMEOUT_SECONDS);
	let budgetInput = $state(DEFAULT_OUTPUT_BUDGET);
	let workingDirectoryInput = $state('');
	let formError = $state('');

	/** The exact operation this instance submitted for the tracked run — the
	 * only operation the bound challenge echo may truthfully name. */
	type SubmittedOperation = {
		command: string;
		timeout: number;
		output_budget: number;
		working_directory: string | null;
	};
	let submittedOperation = $state<SubmittedOperation | null>(null);

	// --- run tracking state ---------------------------------------------------

	let run = $state<RunDetail | null>(null);
	/** Non-reactive id of the tracked run (poll continuations read it). */
	let trackedRunId = '';
	let submitProblem = $state<{ code: string; message: string } | null>(null);
	let submitNotice = $state<string | null>(null);
	let pollProblem = $state<{ code: string; message: string } | null>(null);
	let pollBusy = $state(false);
	let cancelling = $state(false);
	let cancelProblem = $state<{ code: string; message: string } | null>(null);
	/** The last cancel response: a KNOWN outcome or an honest pending. */
	let cancelStatus = $state<CancelView | null>(null);

	const runTerminal = $derived(run !== null && TERMINAL_RUN_STATES.has(run.state ?? ''));

	/** The durable receipt of the step this run waits a decision on. */
	type AwaitingReceipt = { approvalId: string; opId: string; payloadHash: string; policyRevision: number };

	const awaitingReceipt = $derived.by<AwaitingReceipt | null>(() => {
		const current = run;
		if (current === null || current.state !== 'waiting_approval') return null;
		for (let index = current.steps.length - 1; index >= 0; index -= 1) {
			const step = current.steps[index];
			const receipt = step?.receipt;
			if (receipt === null || receipt === undefined) continue;
			const approvalId = receipt['approval_id'];
			const opId = receipt['op_id'];
			const payloadHash = receipt['payload_hash'];
			const policyRevision = receipt['policy_revision'];
			if (
				typeof approvalId === 'string' &&
				UUID_RE.test(approvalId) &&
				typeof opId === 'string' &&
				UUID_RE.test(opId) &&
				typeof payloadHash === 'string' &&
				PAYLOAD_HASH_RE.test(payloadHash) &&
				typeof policyRevision === 'number' &&
				Number.isInteger(policyRevision) &&
				policyRevision >= 1
			) {
				return { approvalId, opId, payloadHash, policyRevision };
			}
		}
		return null;
	});

	/** One replayed read; keeps the displayed state server-recorded only. */
	async function pollOnce(): Promise<void> {
		if (api === null || trackedRunId === '' || pollBusy) return;
		if (!UUID_RE.test(trackedRunId)) return;
		pollBusy = true;
		try {
			const detail = await api.readRun(trackedRunId);
			if (disposed) return;
			run = detail;
			pollProblem = null;
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			// Polling stops on failure; the Retry control re-arms it explicitly.
			pollProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The run state could not be read.')
			};
		} finally {
			if (!disposed) pollBusy = false;
		}
	}

	function retryPolling(): void {
		pollProblem = null;
		void pollOnce();
	}

	// --- submit (the one intent door; C05 rule 5) -----------------------------

	async function submitRun(): Promise<void> {
		if (api === null || serverScope === null || target.status !== 'ready') return;
		formError = '';
		submitProblem = null;
		submitNotice = null;
		const command = commandInput.trim();
		if (command === '') {
			formError = 'Enter the command to run.';
			return;
		}
		const timeout = Number(timeoutInput);
		if (!Number.isFinite(timeout) || timeout <= 0 || timeout > MAX_TIMEOUT_SECONDS) {
			formError = `Timeout must be a number in (0, ${MAX_TIMEOUT_SECONDS}] seconds.`;
			return;
		}
		const budget = Number(budgetInput);
		if (
			!Number.isInteger(budget) ||
			budget <= 0 ||
			budget > MAX_OUTPUT_BUDGET_BYTES
		) {
			formError = `Output budget must be an integer in (0, ${MAX_OUTPUT_BUDGET_BYTES}] bytes.`;
			return;
		}
		const workingDirectory = workingDirectoryInput.trim();
		const payload: RunSubmitBody = {
			run_id: crypto.randomUUID(),
			server_id: serverScope,
			model_profile: DIRECT_MODEL_PROFILE,
			mode: 'ask',
			credential_references: [],
			steps: [
				{
					capability_id: SHELL_CAPABILITY_ID,
					payload:
						workingDirectory === ''
							? { command, timeout, output_budget: budget }
							: { command, timeout, output_budget: budget, working_directory: workingDirectory }
				}
			]
		};
		// The echo record is exactly what was submitted — nothing re-derived.
		submittedOperation = {
			command,
			timeout,
			output_budget: budget,
			working_directory: workingDirectory === '' ? null : workingDirectory
		};
		try {
			const created = await api.submitRun(payload);
			if (disposed) return;
			trackedRunId = created.run.run_id;
			run = { ...created.run, steps: [] };
			cancelStatus = null;
			cancelProblem = null;
			pollProblem = null;
			if (created.deduplicated) {
				submitNotice = 'This run id was already submitted — tracking the recorded run.';
			}
			await pollOnce();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			submitProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The run could not be submitted.')
			};
		}
	}

	// --- cancel (requested ≠ verified; guard rule 3) --------------------------

	async function cancelTrackedRun(): Promise<void> {
		if (api === null || trackedRunId === '' || cancelling || runTerminal) return;
		cancelling = true;
		cancelProblem = null;
		try {
			const view = await api.cancelRun(trackedRunId, { cancel_children: false });
			if (disposed) return;
			cancelStatus = view;
			await pollOnce();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			cancelProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The cancellation could not be recorded.')
			};
		} finally {
			if (!disposed) cancelling = false;
		}
	}

	// --- bound challenge (ephemeral secret; brief acceptance 2) ---------------

	let approval = $state<ApprovalView | null>(null);
	let approvalProblem = $state<{ code: string; message: string } | null>(null);
	let approvalBusy = $state(false);
	let passwordInput = $state('');
	let challengeBusy = $state(false);
	let challengeOutcome = $state<ChallengeOutcomeView | null>(null);
	/** A stable-code denial: the decision is unchanged; the secret is cleared. */
	let challengeDenial = $state<{ code: string; message: string } | null>(null);
	/** A post-consume refusal: the decision was honestly spent on a wrong secret. */
	let challengeSpent = $state<{ code: string; message: string } | null>(null);
	/** The approval id the (cleared) secret was bound to; drives re-binding. */
	let boundApprovalId = $state('');

	/** The challenge form exists only for THIS exact bound challenge. */
	const challengeReady = $derived(
		awaitingReceipt !== null &&
			approval !== null &&
			approval.approval_id === awaitingReceipt.approvalId &&
			approval.status === 'pending' &&
			target.status === 'ready' &&
			target.profile.privilege === 'sudo'
	);

	/** Clear the secret from form memory. JS strings cannot be zero-filled;
	 * dropping the binding is the reachable UI half of the wipe contract. */
	function clearSecret(): void {
		passwordInput = '';
	}

	async function loadApproval(approvalId: string): Promise<void> {
		if (api === null) return;
		approvalBusy = true;
		approvalProblem = null;
		try {
			const loaded = await api.readApproval(approvalId);
			if (disposed || boundApprovalId !== approvalId) return;
			approval = loaded;
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			approvalProblem = {
				code,
				message:
					code === 'not_found'
						? 'No approval record exists for the bound decision.'
						: friendlyText(code, messageOf(error) ?? 'The bound approval could not be loaded.')
			};
		} finally {
			if (!disposed) approvalBusy = false;
		}
	}

	/**
	 * Present the secret to the ONE bound challenge. The body echoes the
	 * binding field-for-field from the durable receipt, the operation exactly
	 * as this panel submitted it, and the enrolled identity of the pinned
	 * profile — the channel recomputes and refuses any mismatch BEFORE the
	 * decision is consumed. The secret is cleared whatever the outcome.
	 */
	async function submitChallenge(): Promise<void> {
		const receipt = awaitingReceipt;
		const profile = target.status === 'ready' ? target.profile : null;
		const operation = submittedOperation;
		if (api === null || serverScope === null || receipt === null || profile === null) return;
		if (approval === null || approval.status !== 'pending') return;
		if (challengeBusy || passwordInput === '') return;
		challengeDenial = null;
		challengeSpent = null;
		challengeOutcome = null;
		const body: ChallengeBody = {
			approval_id: receipt.approvalId,
			capability_id: SHELL_CAPABILITY_ID,
			server_id: serverScope,
			payload_hash: receipt.payloadHash,
			policy_revision: receipt.policyRevision,
			account: profile.ssh.user,
			operation: {
				op_id: receipt.opId,
				server_id: serverScope,
				display: SHELL_CAPABILITY_ID,
				timeout: operation?.timeout ?? Number(DEFAULT_TIMEOUT_SECONDS),
				output_budget: operation?.output_budget ?? Number(DEFAULT_OUTPUT_BUDGET),
				argv: [],
				shell: operation?.command ?? '',
				working_directory: operation?.working_directory ?? null
			},
			identity: {
				host: profile.ssh.host,
				port: profile.ssh.port,
				user: profile.ssh.user,
				host_key: profile.ssh.host_key,
				credential_ref: profile.ssh.credential_ref
			},
			secret: passwordInput
		};
		challengeBusy = true;
		try {
			const outcome = await api.submitChallenge(body);
			if (disposed) return;
			challengeOutcome = outcome;
			await pollOnce();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			if (code === 'password_rejected' || code === 'account_locked') {
				// The decision was consumed by the refused attempt (one-use):
				// say so — a retry needs a fresh approval, never a second guess.
				challengeSpent = {
					code,
					message:
						code === 'password_rejected'
							? 'The target refused this password. The one-use decision was spent; a new approval is required before another attempt.'
							: 'The target account is locked or expired. The one-use decision was spent; resolve the account state first.'
				};
			} else if (
				code === 'unauthenticated' ||
				code === 'unavailable' ||
				code === 'throttled'
			) {
				approvalProblem = {
					code,
					message:
						messageOf(error) ??
						friendlyText(code, 'The challenge could not be submitted; nothing was consumed.')
				};
			} else {
				challengeDenial = {
					code,
					message:
						code === 'already_consumed'
							? 'This decision was already consumed — one approval authorizes exactly one operation.'
							: code === 'expired'
								? 'This approval expired before the challenge was presented.'
								: code === 'payload_mismatch'
									? 'The bound operation changed since the approval was requested — the challenge was refused before anything was consumed.'
									: friendlyText(
											code,
											messageOf(error) ??
												'The challenge was refused; the approval is unchanged.'
										)
				};
			}
		} finally {
			// Brief acceptance 2: cleared from form memory on EVERY exit path.
			clearSecret();
			if (!disposed) challengeBusy = false;
		}
	}

	// --- mount + scoped poll controller (released on teardown) ----------------

	$effect(() => {
		if (!usable) return;
		disposed = false;
		void loadTarget();
		return () => {
			disposed = true;
			clearSecret();
		};
	});

	// Re-binding effect: a new bound challenge clears the old secret and any
	// stale challenge display before the binding preview is (re)loaded. The
	// run RESUMING (bound id becomes '') keeps the last outcome visible — it
	// is the only receipt of the executed challenge.
	$effect(() => {
		const boundId = awaitingReceipt?.approvalId ?? '';
		if (boundId === boundApprovalId) return;
		const hadChallenge = boundApprovalId !== '';
		boundApprovalId = boundId;
		clearSecret();
		approval = null;
		approvalProblem = null;
		challengeDenial = null;
		challengeSpent = null;
		if (boundId === '') return;
		if (hadChallenge || challengeOutcome !== null) challengeOutcome = null;
		void loadApproval(boundId);
	});

	// Instance-scoped poll controller: one timer while a non-terminal run is
	// tracked and no poll failure is displayed. Teardown (hide/unmount/error
	// containment) is the release path; Retry re-arms after a failure.
	$effect(() => {
		if (!usable || run === null || runTerminal || pollProblem !== null) return;
		const timer = setInterval(() => {
			void pollOnce();
		}, POLL_MS);
		return () => clearInterval(timer);
	});

	onDestroy(() => {
		disposed = true;
		clearSecret();
	});

	function shortId(id: string): string {
		return id === '' ? '—' : id.slice(0, 8);
	}

	function receiptText(receipt: Record<string, unknown>, key: string): string | null {
		const value = receipt[key];
		if (typeof value !== 'string' || value === '') return null;
		return value;
	}

	function receiptNumber(receipt: Record<string, unknown>, key: string): number | null {
		const value = receipt[key];
		return typeof value === 'number' && Number.isFinite(value) ? value : null;
	}

	function receiptBoolean(receipt: Record<string, unknown>, key: string): boolean | null {
		const value = receipt[key];
		return typeof value === 'boolean' ? value : null;
	}
</script>

<div class="mai-exec" data-testid="execution-root">
	{#if !usable}
		<div class="mai-exec__state mai-exec__state--error" role="alert" data-testid="panel-config-error">
			<p>This panel was opened without its execution service or outside a pinned server scope.</p>
		</div>
	{:else if target.status === 'loading'}
		<div class="mai-exec__state" role="status" data-testid="target-loading">
			<p>Loading target…</p>
		</div>
	{:else if target.status === 'error'}
		<div
			class="mai-exec__state mai-exec__state--error"
			role="alert"
			data-testid="target-error"
			data-error-code={target.code}
		>
			<p>{target.message}</p>
			<Button variant="secondary" onclick={loadTarget} data-testid="target-retry">Retry</Button>
		</div>
	{:else}
		<header class="mai-exec__header">
			<div class="mai-exec__titlebar">
				<Server size={16} aria-hidden="true" />
				<h3 class="mai-exec__title" data-testid="target-name">{target.profile.name}</h3>
			</div>
			<p class="mai-exec__meta">
				<span data-testid="target-server-id">Target {shortId(serverScope ?? '')}</span>
				<span data-testid="target-privilege">
					Privilege request: {target.profile.privilege === 'sudo'
						? 'sudo (target account password may be required)'
						: target.profile.privilege === 'root'
							? 'root'
							: 'none'}
				</span>
				<span data-testid="target-transport">
					{target.profile.ssh.user}@{target.profile.ssh.host}:{target.profile.ssh.port}
				</span>
			</p>
		</header>

		<section class="mai-exec__section" aria-label="Run a command" data-testid="run-form">
			<div class="mai-exec__sectionhead">
				<Cog size={14} aria-hidden="true" />
				<h4 class="mai-exec__heading">Command uplink</h4>
			</div>
			<div class="mai-exec__grid">
				<div class="mai-exec__field mai-exec__field--wide">
					<div class="mai-exec__cmdline">
						<Terminal size={14} aria-hidden="true" />
						<Field
							label="Command"
							bind:value={commandInput}
							data-testid="command-input"
							placeholder="shell command to run on the target"
						/>
					</div>
				</div>
				<Field
					label="Timeout (seconds)"
					bind:value={timeoutInput}
					inputmode="decimal"
					data-testid="timeout-input"
				/>
				<Field
					label="Output budget (bytes)"
					bind:value={budgetInput}
					inputmode="numeric"
					data-testid="budget-input"
				/>
				<Field
					label="Working directory (optional)"
					bind:value={workingDirectoryInput}
					data-testid="cwd-input"
				/>
			</div>
			{#if formError !== ''}
				<p class="mai-exec__error" role="alert" data-testid="form-error">{formError}</p>
			{/if}
			{#if submitProblem}
				<div
					class="mai-exec__banner mai-exec__banner--error"
					role="alert"
					data-testid="submit-error"
					data-error-code={submitProblem.code}
				>
					<p>{submitProblem.message}</p>
				</div>
			{/if}
			{#if submitNotice}
				<p class="mai-exec__notice" role="status" data-testid="submit-notice">{submitNotice}</p>
			{/if}
			<div class="mai-exec__actions">
				<Button onclick={submitRun} data-testid="submit-run">Run</Button>
			</div>
		</section>

		<section class="mai-exec__section" aria-label="Tracked run" data-testid="run-view">
			<div class="mai-exec__sectionhead">
				<ScrollText size={14} aria-hidden="true" />
				<h4 class="mai-exec__heading">Tracked run</h4>
			</div>
			{#if run === null}
				<p class="mai-exec__muted" data-testid="run-none">No run is being tracked.</p>
			{:else}
				<p class="mai-exec__runline">
					<span class="mai-exec__badge" data-state={run.state ?? 'unknown'} data-testid="run-state">
						{runStateText(run.state)}
					</span>
					<span class="mai-exec__mono" data-testid="run-id">Run {shortId(run.run_id)}</span>
					{#if run.cancel_requested_at !== null}
						<span class="mai-exec__mono mai-exec__cancelflag" data-testid="run-cancel-flag">
							<TriangleAlert size={12} aria-hidden="true" />
							Cancellation requested {run.cancel_requested_at}
						</span>
					{/if}
				</p>
				{#if pollProblem}
					<div
						class="mai-exec__banner mai-exec__banner--error"
						role="alert"
						data-testid="poll-error"
						data-error-code={pollProblem.code}
					>
						<p>{pollProblem.message}</p>
						<Button variant="secondary" onclick={retryPolling} data-testid="poll-retry">
							Retry
						</Button>
					</div>
				{/if}
				{#if !runTerminal}
					<div class="mai-exec__actions">
						<Button variant="danger" loading={cancelling} onclick={cancelTrackedRun} data-testid="cancel-run">
							Cancel run
						</Button>
					</div>
				{/if}
				{#if cancelStatus}
					<p
						class="mai-exec__notice"
						role="status"
						data-testid="cancel-status"
						data-outcome={cancelStatus.outcome ?? 'pending'}
					>
						{cancelStatus.outcome === null
							? 'Cancellation recorded — not yet verified. The run state above is the only proof.'
							: cancelStatus.outcome === 'cancelled'
								? 'Cancellation verified: the engine reports the run cancelled.'
								: `Cancellation outcome: ${cancelStatus.outcome}`}
					</p>
				{/if}
				{#if cancelProblem}
					<div
						class="mai-exec__banner mai-exec__banner--error"
						role="alert"
						data-testid="cancel-error"
						data-error-code={cancelProblem.code}
					>
						<p>{cancelProblem.message}</p>
					</div>
				{/if}
				{#if run.steps.length === 0}
					<p class="mai-exec__muted" data-testid="steps-none">No durable steps recorded yet.</p>
				{:else}
					<ul class="mai-exec__steps" data-testid="step-list">
						{#each run.steps as step (step.seq)}
							<li class="mai-exec__step" data-testid="step-row" data-step-state={step.state}>
								<span class="mai-exec__step-node">step {step.seq}</span>
								<p class="mai-exec__stepline">
									<span class="mai-exec__badge" data-testid="step-state">
										{stepStateText(step.state)}
									</span>
									<span class="mai-exec__mono" data-testid="step-capability">{step.capability_id}</span>
								</p>
								{#if step.receipt !== null}
									<dl class="mai-exec__receipt" data-testid="step-receipt">
										{#if receiptText(step.receipt, 'status') !== null}
											<div>
												<dt>Outcome</dt>
												<dd data-testid="step-outcome">
													{outcomeStatusText(receiptText(step.receipt, 'status') ?? '')}
												</dd>
											</div>
										{/if}
										{#if receiptNumber(step.receipt, 'exit_code') !== null}
											<div>
												<dt>Exit code</dt>
												<dd
													class="mai-exec__exitbadge"
													data-exit-ok={receiptNumber(step.receipt, 'exit_code') === 0}
													data-testid="step-exit-code"
												>
													{receiptNumber(step.receipt, 'exit_code')}
												</dd>
											</div>
										{/if}
										{#if receiptBoolean(step.receipt, 'output_truncated') === true}
											<div>
												<dt>Output</dt>
												<dd data-testid="step-truncation">
													Truncated —
													{#if receiptNumber(step.receipt, 'omitted_bytes') !== null}
														{receiptNumber(step.receipt, 'omitted_bytes')} bytes omitted
													{:else}
														budget exceeded
													{/if}
												</dd>
											</div>
										{/if}
										{#if receiptText(step.receipt, 'reason') !== null}
											<div>
												<dt>Reason</dt>
												<dd data-testid="step-reason">{receiptText(step.receipt, 'reason')}</dd>
											</div>
										{/if}
										{#if receiptText(step.receipt, 'approval_id') !== null}
											<div>
												<dt>Approval</dt>
												<dd data-testid="step-approval">{shortId(receiptText(step.receipt, 'approval_id') ?? '')}</dd>
											</div>
										{/if}
									</dl>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			{/if}
		</section>

		{#if awaitingReceipt !== null}
			<section
				class="mai-exec__section mai-exec__seal"
				aria-label="Bound password challenge"
				data-testid="challenge-section"
			>
				<div class="mai-exec__sealhead">
					<ShieldCheck size={16} aria-hidden="true" />
					<h4 class="mai-exec__heading">Bound password challenge</h4>
				</div>
				<p class="mai-exec__note" data-testid="challenge-note">
					This run waits on one bound approval ({shortId(awaitingReceipt.approvalId)}). Approving or
					denying the operation itself belongs to the capability-policy panel; if the target account
					needs a sudo password, enter it here — it travels once to the bound challenge and is then
					cleared from this form.
				</p>
				{#if approvalBusy}
					<p class="mai-exec__muted" role="status" data-testid="challenge-loading">
						Loading the bound approval…
					</p>
				{:else if approvalProblem}
					<div
						class="mai-exec__banner mai-exec__banner--error"
						role="alert"
						data-testid="challenge-error"
						data-error-code={approvalProblem.code}
					>
						<p>{approvalProblem.message}</p>
					</div>
				{:else if approval !== null}
					<dl class="mai-exec__receipt" data-testid="challenge-binding">
						<div>
							<dt>Capability</dt>
							<dd>{approval.capability_id}</dd>
						</div>
						<div>
							<dt>Target</dt>
							<dd>{shortId(approval.server_id)}</dd>
						</div>
						<div>
							<dt>Payload binding</dt>
							<dd><code>{approval.payload_hash.slice(0, 12)}…</code></dd>
						</div>
						<div>
							<dt>Policy revision</dt>
							<dd>{approval.policy_revision}</dd>
						</div>
						<div>
							<dt>Status</dt>
							<dd data-testid="challenge-approval-status">{approval.status}</dd>
						</div>
						<div>
							<dt>Expires</dt>
							<dd>{approval.expires_at}</dd>
						</div>
					</dl>
					{#if challengeReady}
						<div class="mai-exec__keyline">
							<KeyRound size={14} aria-hidden="true" />
							<div class="mai-exec__field mai-exec__field--wide">
								<Field
									label="Sudo password for {target.profile.ssh.user}"
									type="password"
									autocomplete="off"
									bind:value={passwordInput}
									data-testid="challenge-password"
								/>
							</div>
						</div>
						<div class="mai-exec__actions">
							<Button
								loading={challengeBusy}
								disabled={passwordInput === ''}
								onclick={submitChallenge}
								data-testid="challenge-submit"
							>
								Submit password
							</Button>
						</div>
					{:else if approval.status !== 'pending'}
						<p class="mai-exec__muted" role="status" data-testid="challenge-consumed">
							The bound decision is {approval.status}; the run state above is the only progress proof.
						</p>
					{:else}
						<p class="mai-exec__muted" data-testid="challenge-not-sudo">
							This target's privilege request does not use a sudo password; decide the operation in
							the capability-policy panel.
						</p>
					{/if}
					{#if challengeDenial}
						<div
							class="mai-exec__banner mai-exec__banner--error"
							role="alert"
							data-testid="challenge-denial"
							data-error-code={challengeDenial.code}
						>
							<p>{challengeDenial.message}</p>
						</div>
					{/if}
					{#if challengeSpent}
						<div
							class="mai-exec__banner mai-exec__banner--warn"
							role="alert"
							data-testid="challenge-spent"
							data-error-code={challengeSpent.code}
						>
							<p>{challengeSpent.message}</p>
						</div>
					{/if}
				{/if}
			</section>
		{/if}

		{#if challengeOutcome !== null}
			<section
				class="mai-exec__section"
				aria-label="Last password challenge outcome"
				data-testid="challenge-outcome-section"
			>
				<div class="mai-exec__sectionhead">
					<ShieldCheck size={14} aria-hidden="true" />
					<h4 class="mai-exec__heading">Last password challenge outcome</h4>
				</div>
				<div class="mai-exec__outcome" data-status={challengeOutcome.status} data-testid="challenge-outcome">
					<p class="mai-exec__runline">
						<span class="mai-exec__badge" data-testid="challenge-outcome-status">
							{outcomeStatusText(challengeOutcome.status)}
						</span>
						{#if challengeOutcome.exit_code !== null}
							<span class="mai-exec__exitbadge" data-exit-ok={challengeOutcome.exit_code === 0} data-testid="challenge-outcome-exit">exit {challengeOutcome.exit_code}</span>
						{/if}
						<span class="mai-exec__mono">duration {challengeOutcome.duration_seconds}s</span>
					</p>
					{#if challengeOutcome.stdout_truncated || challengeOutcome.stderr_truncated}
						<p class="mai-exec__muted" data-testid="challenge-outcome-truncation">
							Output truncated — stdout {challengeOutcome.stdout_bytes} bytes
							({challengeOutcome.stdout_truncated ? 'cut' : 'complete'}), stderr
							{challengeOutcome.stderr_bytes} bytes
							({challengeOutcome.stderr_truncated ? 'cut' : 'complete'}).
						</p>
					{/if}
					{#if challengeOutcome.stdout !== ''}
						<pre class="mai-exec__stream" data-testid="challenge-stdout">{challengeOutcome.stdout}</pre>
					{/if}
					{#if challengeOutcome.stderr !== ''}
						<pre class="mai-exec__stream mai-exec__stream--err" data-testid="challenge-stderr">{challengeOutcome.stderr}</pre>
					{/if}
				</div>
			</section>
		{/if}
	{/if}
</div>

<style>
	.mai-exec {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-4);
		padding: var(--mai-space-4);
		color: var(--mai-color-text);
		font-family: var(--mai-font-sans);
		background: var(--mai-color-bg);
	}

	/* --- fail-closed states ---------------------------------------------- */

	.mai-exec__state {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		align-items: flex-start;
		color: var(--mai-status-neutral-text);
	}

	.mai-exec__state--error {
		color: var(--mai-status-danger-text);
	}

	.mai-exec__state p,
	.mai-exec__banner p {
		margin: 0;
	}

	/* --- cogitator header band ------------------------------------------- */

	.mai-exec__header {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-space-3) var(--mai-space-4);
		background: var(--mai-color-surface-header);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-md);
	}

	.mai-exec__titlebar {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		color: var(--mai-color-brass);
	}

	.mai-exec__title {
		margin: 0;
		color: var(--mai-color-brass);
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-xl);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	.mai-exec__meta {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mai-space-1) var(--mai-space-4);
		margin: 0;
		color: var(--mai-color-text-muted);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
	}

	/* --- section chrome --------------------------------------------------- */

	.mai-exec__section {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		padding-top: var(--mai-space-3);
		border-top: var(--mai-border-width) solid var(--mai-color-border);
	}

	.mai-exec__sectionhead {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface-header);
		border-left: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-sm);
		color: var(--mai-color-brass);
	}

	.mai-exec__heading {
		margin: 0;
		color: var(--mai-color-brass);
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-base);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	/* --- command uplink: monospace terminal line -------------------------- */

	.mai-exec__grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: var(--mai-space-3);
	}

	.mai-exec__field--wide {
		grid-column: 1 / -1;
	}

	.mai-exec__grid :global(.mai-field__input),
	.mai-exec__keyline :global(.mai-field__input) {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-sm);
		letter-spacing: 0.02em;
	}

	.mai-exec__cmdline {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		padding: 0 var(--mai-space-3);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border-strong);
		border-radius: var(--mai-radius-md);
		color: var(--mai-color-brass);
	}

	.mai-exec__cmdline :global(.mai-field) {
		flex: 1;
		min-width: 0;
	}

	.mai-exec__actions {
		display: flex;
		gap: var(--mai-space-3);
	}

	/* --- telemetry helpers ------------------------------------------------- */

	.mai-exec__mono {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		letter-spacing: 0.02em;
	}

	.mai-exec__runline {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--mai-space-2) var(--mai-space-3);
		margin: 0;
	}

	.mai-exec__cancelflag {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-1);
		color: var(--mai-status-warning-text);
	}

	.mai-exec__badge {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-1);
		padding: var(--mai-space-1) var(--mai-space-2);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		letter-spacing: 0.04em;
	}

	.mai-exec__badge[data-state='queued'],
	.mai-exec__badge[data-state='running'],
	.mai-exec__badge[data-state='waiting_approval'],
	.mai-exec__badge[data-state='waiting_children'],
	.mai-exec__badge[data-state='reconciling'],
	.mai-exec__badge[data-state='cancel_requested'] {
		color: var(--mai-status-info-text);
		background: var(--mai-status-info-bg);
		border-color: var(--mai-status-info-border);
	}

	.mai-exec__badge[data-state='succeeded'],
	.mai-exec__badge[data-state='cancelled'] {
		color: var(--mai-status-success-text);
		background: var(--mai-status-success-bg);
		border-color: var(--mai-status-success-border);
	}

	.mai-exec__badge[data-state='failed'],
	.mai-exec__badge[data-state='outcome_unknown'] {
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
		border-color: var(--mai-status-danger-border);
	}

	/* --- step receipts timeline -------------------------------------------- */

	.mai-exec__steps {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.mai-exec__step {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr);
		gap: var(--mai-space-2) var(--mai-space-3);
		align-items: start;
		padding: var(--mai-space-3);
		background: var(--mai-color-surface-raised);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-left: 3px solid var(--mai-color-brass-dim);
		border-radius: var(--mai-radius-lg);
	}

	.mai-exec__step-node {
		grid-column: 1;
		grid-row: 1 / span 2;
		padding: 2px var(--mai-space-2);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-brass-dim);
		border-radius: var(--mai-radius-sm);
		color: var(--mai-color-brass);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		white-space: nowrap;
	}

	.mai-exec__stepline {
		grid-column: 2;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--mai-space-2) var(--mai-space-3);
		margin: 0;
	}

	.mai-exec__receipt {
		grid-column: 2;
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: var(--mai-space-2) var(--mai-space-4);
		margin: 0;
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		font-size: var(--mai-text-xs);
	}

	.mai-exec__receipt dt {
		color: var(--mai-color-text-muted);
		font-family: var(--mai-font-sans);
		font-size: var(--mai-text-xs);
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	.mai-exec__receipt dd {
		margin: 0;
		overflow-wrap: anywhere;
		color: var(--mai-color-text);
		font-family: var(--mai-font-mono);
	}

	.mai-exec__receipt code {
		color: var(--mai-color-brass);
		font-family: var(--mai-font-mono);
	}

	.mai-exec__exitbadge {
		display: inline-block;
		padding: 0 var(--mai-space-2);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		color: var(--mai-status-neutral-text);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
	}

	.mai-exec__exitbadge[data-exit-ok='true'] {
		color: var(--mai-status-success-text);
		border-color: var(--mai-status-success-border);
	}

	.mai-exec__exitbadge[data-exit-ok='false'] {
		color: var(--mai-status-danger-text);
		border-color: var(--mai-status-danger-border);
	}

	/* --- alert banners ------------------------------------------------------ */

	.mai-exec__banner {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		align-items: flex-start;
		padding: var(--mai-space-3);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
	}

	.mai-exec__banner--error {
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
		border-color: var(--mai-status-danger-border);
	}

	.mai-exec__banner--warn {
		color: var(--mai-status-warning-text);
		background: var(--mai-status-warning-bg);
		border-color: var(--mai-status-warning-border);
	}

	.mai-exec__error {
		margin: 0;
		color: var(--mai-status-danger-text);
	}

	.mai-exec__notice {
		margin: 0;
		color: var(--mai-status-success-text);
	}

	.mai-exec__muted {
		margin: 0;
		color: var(--mai-color-text-muted);
	}

	.mai-exec__note {
		margin: 0;
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-sm);
	}

	/* --- sudo / password challenge seal ------------------------------------- */

	.mai-exec__seal {
		gap: var(--mai-space-3);
		padding: var(--mai-space-4);
		background: var(--mai-color-surface-raised);
		border: 3px solid var(--mai-color-brass);
		border-radius: var(--mai-radius-lg);
		box-shadow: 0 0 0 1px var(--mai-color-brass-dim);
	}

	.mai-exec__sealhead {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		margin: calc(-1 * var(--mai-space-4)) calc(-1 * var(--mai-space-4)) 0;
		padding: var(--mai-space-2) var(--mai-space-4);
		background: var(--mai-color-surface-header);
		border-bottom: var(--mai-border-width) solid var(--mai-color-brass);
		border-radius: var(--mai-radius-lg) var(--mai-radius-lg) 0 0;
		color: var(--mai-color-brass);
	}

	.mai-exec__keyline {
		display: flex;
		align-items: flex-end;
		gap: var(--mai-space-2);
		padding: var(--mai-space-2) var(--mai-space-3) var(--mai-space-3);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) dashed var(--mai-color-brass-dim);
		border-radius: var(--mai-radius-md);
		color: var(--mai-color-brass);
	}

	.mai-exec__keyline .mai-exec__field {
		flex: 1;
		min-width: 0;
	}

	/* --- challenge outcome --------------------------------------------------- */

	.mai-exec__outcome {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-space-3);
		background: var(--mai-color-surface-raised);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-lg);
	}

	.mai-exec__outcome[data-status='succeeded'] .mai-exec__badge,
	.mai-exec__outcome[data-status='cancelled'] .mai-exec__badge {
		color: var(--mai-status-success-text);
		background: var(--mai-status-success-bg);
		border-color: var(--mai-status-success-border);
	}

	.mai-exec__outcome[data-status='failed'] .mai-exec__badge,
	.mai-exec__outcome[data-status='outcome_unknown'] .mai-exec__badge {
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
		border-color: var(--mai-status-danger-border);
	}

	.mai-exec__outcome[data-status='running'] .mai-exec__badge,
	.mai-exec__outcome[data-status='cancel_requested'] .mai-exec__badge {
		color: var(--mai-status-info-text);
		background: var(--mai-status-info-bg);
		border-color: var(--mai-status-info-border);
	}

	.mai-exec__stream {
		margin: 0;
		padding: var(--mai-space-3);
		overflow: auto;
		max-height: 12rem;
		background: var(--mai-color-bg);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		color: var(--mai-color-text);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}

	.mai-exec__stream--err {
		color: var(--mai-status-danger-text);
	}
</style>
