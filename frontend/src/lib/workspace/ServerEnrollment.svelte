<script lang="ts">
	/**
	 * ServerEnrollment — guided, confirmed server enrollment (so-03).
	 *
	 * Extracted from DesktopShell's inline enrollment form; the shell keeps the
	 * toggle button, the session client and the post-enrollment refresh/select
	 * behavior. This component owns ONLY the guided flow:
	 *
	 *  - Controller public key: loaded through the typed client from
	 *    `/onboarding/controller-key` with honest loading/error/ready states and
	 *    a Copy action that writes exactly the public key (Clipboard API with an
	 *    explicitly supported execCommand fallback). No private material, key
	 *    path or env value is ever rendered or requested.
	 *  - ONE `Test connection` button performs the complete guided test:
	 *    without a selected fingerprint it runs discovery (`host_key: null`);
	 *    a single candidate is selected and pinned-tested IN THE SAME action;
	 *    several candidates require an explicit selection, after which the
	 *    button performs the pinned test. Discovery is never presented as
	 *    authentication success — only `OnboardingTestResult` claims it.
	 *  - Explicit trust: `Enroll server` is enabled only while the CURRENT
	 *    connection tuple {host, port, user, privilege} has a successful pinned
	 *    test AND the user checked `Trust this host key`. Any endpoint,
	 *    privilege, candidate or trust edit resets the dependent result/trust,
	 *    so no stale result can enroll a changed endpoint.
	 *  - Enroll submits `host_key_confirmed: true`, the tested fingerprint and
	 *    NO `credential_ref` (the server stores its configured default).
	 *    Duplicate/racing test and enroll submissions are disabled; failures
	 *    preserve the form.
	 *
	 * Svelte 5 runes only; no module-level state, no `window` access outside
	 * event handlers (SSR-safe). Styling reuses ui/tokens.css variables.
	 */
	import Button from '../ui/Button.svelte';
	import CapabilityPicker from '../ui/CapabilityPicker.svelte';
	import Field from '../ui/Field.svelte';
	import Select from '../ui/Select.svelte';
	import type {
		ControllerKeyResponse,
		OnboardingCandidate,
		OnboardingDiscoveryResponse,
		OnboardingEnrollRequest,
		OnboardingSshTarget,
		OnboardingTestRequest,
		OnboardingTestResult,
		ProfileMutationResult
	} from '../api/client.js';
	import { ApiError } from '../api/client.js';

	/** The exact onboarding slice this flow needs (satisfied by MaiApiClient). */
	interface EnrollmentApi {
		readControllerKey(): Promise<ControllerKeyResponse>;
		testOnboardingConnection(
			payload: OnboardingTestRequest
		): Promise<OnboardingDiscoveryResponse | OnboardingTestResult>;
		enrollViaOnboarding(payload: OnboardingEnrollRequest): Promise<ProfileMutationResult>;
	}

	interface Props {
		api: EnrollmentApi;
		/** Default capability set offered to the new server. */
		capabilities?: string[];
		/** Called after a confirmed enrollment succeeded (form already reset). */
		onenrolled?: (serverId: string) => void;
	}

	let { api, capabilities = [], onenrolled }: Props = $props();

	const uid = $props.id();

	/** Bounded probe budget; mirrors the backend default (so_PLAN "test"). */
	const TEST_TIMEOUT_SECONDS = 10;

	function readApi(candidate: unknown): EnrollmentApi | null {
		if (typeof candidate !== 'object' || candidate === null) return null;
		const bag = candidate as Partial<EnrollmentApi>;
		if (
			typeof bag.readControllerKey !== 'function' ||
			typeof bag.testOnboardingConnection !== 'function' ||
			typeof bag.enrollViaOnboarding !== 'function'
		) {
			return null;
		}
		return bag as EnrollmentApi;
	}

	const usableApi = $derived(readApi(api));

	// --- controller public key ------------------------------------------------

	type KeyState =
		| { status: 'loading' }
		| { status: 'error'; message: string }
		| { status: 'ready'; publicKey: string; keyType: string; fingerprint: string };

	let keyState = $state<KeyState>({ status: 'loading' });
	let keyLoadSeq = 0;
	let copyStatus = $state<'idle' | 'copied' | 'failed'>('idle');

	async function loadKey(): Promise<void> {
		if (usableApi === null) {
			keyState = { status: 'error', message: 'Enrollment service is unavailable.' };
			return;
		}
		const seq = ++keyLoadSeq;
		keyState = { status: 'loading' };
		copyStatus = 'idle';
		try {
			const key = await usableApi.readControllerKey();
			if (seq !== keyLoadSeq) return;
			keyState = {
				status: 'ready',
				publicKey: key.public_key,
				keyType: key.key_type,
				fingerprint: key.fingerprint
			};
		} catch (error) {
			if (seq !== keyLoadSeq) return;
			keyState = {
				status: 'error',
				message:
					error instanceof ApiError
						? `Controller public key unavailable (${error.code}): ${error.message}`
						: 'Controller public key could not be loaded.'
			};
		}
	}

	/**
	 * Clipboard write with the explicit supported fallback: the async
	 * Clipboard API when available/permitted, otherwise the hidden-textarea
	 * `execCommand('copy')` path. Writes EXACTLY the given text.
	 */
	async function writeClipboard(text: string): Promise<boolean> {
		const clipboard = navigator.clipboard;
		if (clipboard && typeof clipboard.writeText === 'function') {
			try {
				await clipboard.writeText(text);
				return true;
			} catch {
				// Permission denied or transient failure — try the fallback.
			}
		}
		const area = document.createElement('textarea');
		area.value = text;
		area.setAttribute('readonly', '');
		area.style.position = 'fixed';
		area.style.opacity = '0';
		document.body.appendChild(area);
		area.select();
		let ok = false;
		try {
			ok = document.execCommand('copy');
		} catch {
			ok = false;
		}
		area.remove();
		return ok;
	}

	async function copyKey(): Promise<void> {
		if (keyState.status !== 'ready') return;
		const ok = await writeClipboard(keyState.publicKey);
		copyStatus = ok ? 'copied' : 'failed';
	}

	// --- guided test / trust state machine ------------------------------------

	let name = $state('');
	let promptText = $state('');
	let host = $state('');
	let port = $state('22');
	let user = $state('');
	let privilege = $state<'none' | 'sudo' | 'root'>('sudo');
	// Intentional initial capture: the shell mounts this component inside an
	// `{#if enrollOpen}` block, so every open remounts fresh and the prop's
	// value at mount IS the offered default set.
	// svelte-ignore state_referenced_locally
	let caps = $state<string[]>([...capabilities]);

	let testing = $state(false);
	let candidates = $state<OnboardingCandidate[] | null>(null);
	let selectedKey = $state<string | null>(null);
	let testResult = $state<OnboardingTestResult | null>(null);
	let testNotice = $state<string | null>(null);
	let testError = $state<string | null>(null);
	let trust = $state(false);
	let enrollBusy = $state(false);
	let enrollError = $state<string | null>(null);

	/** Monotonic run token: invalidations discard in-flight responses. */
	let runSeq = 0;

	function parsePort(raw: string): number | null {
		if (!/^\d+$/.test(raw.trim())) return null;
		const value = Number(raw.trim());
		return value >= 1 && value <= 65535 ? value : null;
	}

	const parsedPort = $derived(parsePort(port));

	function validatedTarget(): OnboardingSshTarget | null {
		const nextHost = host.trim();
		const nextUser = user.trim();
		if (nextHost === '' || nextUser === '' || parsedPort === null) return null;
		return { host: nextHost, port: parsedPort, user: nextUser };
	}

	const targetReady = $derived(validatedTarget() !== null);

	/**
	 * Enroll gate: a CURRENT successful pinned test for exactly this endpoint +
	 * privilege + fingerprint, explicit trust, and a complete form.
	 */
	const canEnroll = $derived(
		targetReady &&
			testResult !== null &&
			trust &&
			name.trim() !== '' &&
			promptText.trim() !== '' &&
			!testing &&
			!enrollBusy
	);

	/** Honest button label: pinned test when a candidate fingerprint is armed. */
	const testButtonLabel = $derived(
		selectedKey !== null ? 'Test selected host key' : 'Test connection'
	);

	/** Any endpoint/privilege edit invalidates scan, selection, result, trust. */
	function invalidateTarget(): void {
		runSeq += 1;
		candidates = null;
		selectedKey = null;
		testResult = null;
		testNotice = null;
		testError = null;
		trust = false;
	}

	/** Candidate (re)selection arms the pinned key and invalidates the prior
	 * result and trust; discovery output itself stays listed. */
	function onCandidateSelect(key: string): void {
		runSeq += 1;
		selectedKey = key;
		testResult = null;
		testNotice = null;
		testError = null;
		trust = false;
	}

	async function runPinnedTest(seq: number, target: OnboardingSshTarget): Promise<void> {
		const pinned = selectedKey;
		if (pinned === null) return;
		const answer = await usableApi!.testOnboardingConnection({
			ssh: target,
			privilege,
			host_key: pinned,
			timeout_seconds: TEST_TIMEOUT_SECONDS
		});
		if (seq !== runSeq) return;
		if (!('authentication' in answer)) {
			testError = 'Pinned test returned an unexpected response.';
			return;
		}
		testResult = answer;
		testNotice = null;
	}

	/**
	 * ONE guided action: discovery when no fingerprint is armed (a single
	 * candidate continues into the pinned test within this same action), the
	 * pinned test itself otherwise.
	 */
	async function runTest(): Promise<void> {
		if (testing || enrollBusy || usableApi === null) return;
		const target = validatedTarget();
		if (target === null) {
			testError = 'SSH host, an integer port (1–65535) and SSH user are required before testing.';
			return;
		}
		const seq = ++runSeq;
		testing = true;
		testError = null;
		testNotice = null;
		try {
			if (selectedKey === null) {
				const answer = await usableApi.testOnboardingConnection({
					ssh: target,
					privilege,
					host_key: null,
					timeout_seconds: TEST_TIMEOUT_SECONDS
				});
				if (seq !== runSeq) return;
				if (!('candidates' in answer)) {
					testError = 'Discovery returned an unexpected response.';
					return;
				}
				candidates = answer.candidates;
				if (answer.candidates.length === 0) {
					testNotice =
						'The scan found no SSH host key for this endpoint. Verify host, port and that sshd is reachable.';
				} else if (answer.candidates.length === 1) {
					selectedKey = answer.candidates[0].host_key;
					await runPinnedTest(seq, target);
				} else {
					testNotice = `Discovery found ${answer.candidates.length} host-key candidates — this is NOT yet an authenticated connection. Select the fingerprint you recognize, then run the pinned test.`;
				}
			} else {
				await runPinnedTest(seq, target);
			}
		} catch (error) {
			if (seq !== runSeq) return;
			testResult = null;
			trust = false;
			testError =
				error instanceof ApiError
					? `${error.code}: ${error.message}`
					: 'Connection test failed.';
		} finally {
			// Unconditional release: `runTest` refuses re-entry while `testing`
			// is true and the button is disabled, so this attempt is ALWAYS the
			// one that set the flag — even when an endpoint/privilege edit
			// invalidated it mid-flight (runSeq bump). The stale answer itself
			// stays discarded by the seq guards above; only the flag release is
			// unconditional, otherwise the button would wedge in "Testing".
			testing = false;
		}
	}

	function resetForm(): void {
		name = '';
		promptText = '';
		host = '';
		port = '22';
		user = '';
		privilege = 'sudo';
		caps = [...capabilities];
		runSeq += 1;
		candidates = null;
		selectedKey = null;
		testResult = null;
		testNotice = null;
		testError = null;
		trust = false;
		enrollError = null;
	}

	async function enrollServer(event: SubmitEvent): Promise<void> {
		event.preventDefault();
		if (!canEnroll || usableApi === null || testResult === null) return;
		const target = validatedTarget();
		if (target === null) return;
		enrollBusy = true;
		enrollError = null;
		try {
			const result = await usableApi.enrollViaOnboarding({
				name: name.trim(),
				prompt_revision: 1,
				prompt_text: promptText,
				environment: 'linux_ssh',
				privilege,
				capabilities: [...caps],
				ssh: target,
				host_key: testResult.host_key,
				host_key_confirmed: true,
				timeout_seconds: TEST_TIMEOUT_SECONDS
			});
			resetForm();
			onenrolled?.(result.server_id);
		} catch (error) {
			enrollError =
				error instanceof ApiError ? `${error.code}: ${error.message}` : 'Enrollment failed.';
		} finally {
			enrollBusy = false;
		}
	}

	// Mount load (release-safe: an unmounted component never writes state).
	let disposed = false;
	$effect(() => {
		disposed = false;
		void loadKey();
		return () => {
			disposed = true;
		};
	});
</script>

<form class="mai-enrollment" onsubmit={enrollServer} data-testid="enroll-server">
	<h2 class="mai-enrollment__title">Guided server enrollment</h2>
	<p class="mai-enrollment__lede">
		Copy the controller public key to the target's authorized keys, then let the guided test
		discover, pin and confirm the host key before enrolling.
	</p>

	{#if keyState.status === 'loading'}
		<p class="mai-enrollment__state" role="status" data-testid="enroll-key-loading">
			Loading controller public key…
		</p>
	{:else if keyState.status === 'error'}
		<div class="mai-enrollment__error-box" role="alert" data-testid="enroll-key-error">
			<p>{keyState.message}</p>
			<Button size="sm" variant="secondary" onclick={loadKey} data-testid="enroll-key-retry">
				Retry
			</Button>
		</div>
	{:else}
		<section class="mai-enrollment__key-block" aria-label="Controller public key">
			<span class="mai-enrollment__label">Controller public key ({keyState.keyType})</span>
			<code class="mai-enrollment__key" data-testid="enroll-controller-public-key"
				>{keyState.publicKey}</code
			>
			<div class="mai-enrollment__key-row">
				<Button
					size="sm"
					variant="secondary"
					data-testid="enroll-copy-public-key"
					aria-label="Copy public key"
					disabled={enrollBusy}
					onclick={copyKey}
				>
					Copy public key
				</Button>
				<span class="mai-enrollment__copy-status" role="status" data-testid="enroll-copy-status"
					>{copyStatus === 'copied'
						? 'Copied.'
						: copyStatus === 'failed'
							? 'Copy failed — select the key text and copy manually.'
							: ''}</span
				>
			</div>
			<span class="mai-enrollment__hint">Key fingerprint {keyState.fingerprint}</span>
		</section>
	{/if}

	<div class="mai-enrollment__grid">
		<Field label="Display name" bind:value={name} disabled={enrollBusy} required />
		<Field
			label="SSH host"
			bind:value={host}
			disabled={enrollBusy}
			autocomplete="off"
			oninput={invalidateTarget}
			required
		/>
		<Field
			label="SSH port"
			inputmode="numeric"
			bind:value={port}
			disabled={enrollBusy}
			autocomplete="off"
			oninput={invalidateTarget}
			required
		/>
		<Field
			label="SSH user"
			bind:value={user}
			disabled={enrollBusy}
			autocomplete="off"
			oninput={invalidateTarget}
			required
		/>
		<Select
			id="mai-enroll-privilege-{uid}"
			label="Privilege mode"
			bind:value={privilege}
			disabled={enrollBusy}
			onchange={invalidateTarget}
		>
			<option value="none">none</option>
			<option value="sudo">sudo</option>
			<option value="root">root</option>
		</Select>
	</div>

	<CapabilityPicker
		label="Capabilities"
		bind:value={caps}
		disabled={enrollBusy}
		testId="enroll-capabilities"
	/>

	<label class="mai-enrollment__prompt">
		<span>Server system prompt (required)</span>
		<textarea
			rows="4"
			bind:value={promptText}
			disabled={enrollBusy}
			placeholder="# Server instructions&#10;Describe this server's role, OS, and operational rules..."
			required
			data-testid="enroll-prompt-text"
		></textarea>
	</label>

	<div class="mai-enrollment__actions">
		<Button
			data-testid="enroll-test-connection"
			variant="secondary"
			loading={testing}
			disabled={testing || enrollBusy}
			onclick={runTest}
		>
			{testButtonLabel}
		</Button>
		<span class="mai-enrollment__actions-hint">
			{selectedKey === null
				? 'Scans the endpoint for its host key (no authentication claim).'
				: 'Connects with the selected fingerprint pinned and probes the Linux identity.'}
		</span>
	</div>

	{#if testError}
		<p class="mai-enrollment__state mai-enrollment__state--error" role="alert" data-testid="enroll-test-error">
			{testError}
		</p>
	{/if}
	{#if testNotice}
		<p class="mai-enrollment__state" role="status" data-testid="enroll-test-notice">
			{testNotice}
		</p>
	{/if}

	{#if candidates !== null && candidates.length > 1}
		<fieldset class="mai-enrollment__candidates" data-testid="enroll-host-key-candidates">
			<legend>Host key candidates — select the fingerprint you recognize</legend>
			{#each candidates as candidate (candidate.host_key)}
				<label class="mai-enrollment__candidate">
					<input
						type="radio"
						name="mai-enroll-candidate-{uid}"
						value={candidate.host_key}
						checked={selectedKey === candidate.host_key}
						disabled={testing || enrollBusy}
						onchange={() => onCandidateSelect(candidate.host_key)}
					/>
					<code class="mai-enrollment__key">{candidate.host_key}</code>
					<span class="mai-enrollment__candidate-type">{candidate.key_type}</span>
				</label>
			{/each}
		</fieldset>
	{/if}

	{#if testResult !== null}
		<div class="mai-enrollment__result" role="status" data-testid="enroll-test-result">
			<p class="mai-enrollment__result-headline">
				Authenticated — Linux identity probed with the pinned key.
			</p>
			<dl class="mai-enrollment__result-grid">
				<div>
					<dt>Host key (pinned)</dt>
					<dd><code class="mai-enrollment__key">{testResult.host_key}</code></dd>
				</div>
				<div>
					<dt>Key type</dt>
					<dd>{testResult.key_type}</dd>
				</div>
				<div>
					<dt>Authentication</dt>
					<dd>{testResult.authentication}</dd>
				</div>
				<div>
					<dt>Privilege</dt>
					<dd>{testResult.privilege}</dd>
				</div>
				<div>
					<dt>Hostname</dt>
					<dd>{testResult.probe_environment.hostname}</dd>
				</div>
				<div>
					<dt>OS</dt>
					<dd>
						{testResult.probe_environment.os_id ?? 'unknown'}{testResult.probe_environment
							.os_version_id
							? ` ${testResult.probe_environment.os_version_id}`
							: ''}
					</dd>
				</div>
				<div>
					<dt>Kernel</dt>
					<dd>{testResult.probe_environment.kernel}</dd>
				</div>
			</dl>
		</div>
	{/if}

	<label
		class="mai-enrollment__trust"
		data-testid="enroll-host-key-confirmation"
		aria-disabled={testResult === null || testing || enrollBusy}
	>
		<input
			type="checkbox"
			bind:checked={trust}
			disabled={testResult === null || testing || enrollBusy}
		/>
		<span>
			Trust this host key{testResult !== null
				? ` (${testResult.host_key})`
				: ' — run a successful pinned test first'}
		</span>
	</label>

	{#if enrollError}
		<p class="mai-enrollment__state mai-enrollment__state--error" role="alert" data-testid="enroll-error">
			{enrollError}
		</p>
	{/if}

	<Button type="submit" loading={enrollBusy} disabled={!canEnroll} data-testid="enroll-submit">
		Enroll server
	</Button>
</form>

<style>
	.mai-enrollment {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-card-pad, var(--mai-space-3));
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-lg);
		box-sizing: border-box;
		min-width: 0;
	}

	.mai-enrollment__title {
		margin: 0;
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-text);
	}

	.mai-enrollment__lede {
		margin: 0;
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}

	.mai-enrollment__label,
	.mai-enrollment__prompt > span,
	.mai-enrollment__candidates legend {
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-semibold);
		color: var(--mai-color-text-muted);
	}

	/* Long public keys / fingerprints wrap; no horizontal overflow anywhere. */
	.mai-enrollment__key {
		display: block;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		line-height: 1.5;
		color: var(--mai-color-text);
		word-break: break-all;
		overflow-wrap: anywhere;
		white-space: pre-wrap;
		min-width: 0;
	}

	.mai-enrollment__key-block,
	.mai-enrollment__result,
	.mai-enrollment__candidates {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		padding: var(--mai-space-2);
		background: var(--mai-color-surface-sunken, transparent);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		min-width: 0;
	}

	.mai-enrollment__key-row {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
	}

	.mai-enrollment__copy-status {
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}

	.mai-enrollment__hint {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
		word-break: break-all;
		overflow-wrap: anywhere;
	}

	.mai-enrollment__grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
		gap: var(--mai-space-2);
		align-items: end;
	}

	.mai-enrollment__prompt {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
	}

	.mai-enrollment__prompt textarea {
		width: 100%;
		min-height: 5.5rem;
		padding: var(--mai-space-2) var(--mai-space-3);
		color: var(--mai-color-text);
		background: var(--mai-color-surface-sunken);
		border: var(--mai-border-width) solid var(--mai-color-border-strong);
		border-radius: var(--mai-radius-md);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		line-height: 1.5;
		resize: vertical;
		box-sizing: border-box;
	}

	.mai-enrollment__prompt textarea:focus-visible {
		outline: var(--mai-focus-ring-width) solid var(--mai-focus-ring-color);
		outline-offset: var(--mai-focus-ring-offset);
		border-color: var(--mai-color-accent);
	}

	.mai-enrollment__actions {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
	}

	.mai-enrollment__actions-hint {
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}

	.mai-enrollment__state {
		margin: 0;
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text-muted);
	}

	.mai-enrollment__state--error {
		color: var(--mai-status-danger-text);
	}

	.mai-enrollment__error-box {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
		font-size: var(--mai-text-sm);
		color: var(--mai-status-danger-text);
	}

	.mai-enrollment__error-box p {
		margin: 0;
	}

	.mai-enrollment__candidates {
		margin: 0;
		border: var(--mai-border-width) solid var(--mai-color-border);
	}

	.mai-enrollment__candidate {
		display: flex;
		align-items: baseline;
		gap: var(--mai-space-2);
		min-width: 0;
		cursor: pointer;
	}

	.mai-enrollment__candidate input {
		flex: 0 0 auto;
		accent-color: var(--mai-color-accent);
	}

	.mai-enrollment__candidate-type {
		flex: 0 0 auto;
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}

	.mai-enrollment__result-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr));
		gap: var(--mai-space-2);
		margin: 0;
	}

	.mai-enrollment__result-grid dt {
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-semibold);
		color: var(--mai-color-text-muted);
	}

	.mai-enrollment__result-grid dd {
		margin: 0;
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text);
		word-break: break-word;
		overflow-wrap: anywhere;
	}

	.mai-enrollment__result-headline {
		margin: 0;
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-semibold);
		color: var(--mai-status-success-text, var(--mai-color-text));
	}

	.mai-enrollment__trust {
		display: flex;
		align-items: baseline;
		gap: var(--mai-space-2);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text);
		word-break: break-word;
		overflow-wrap: anywhere;
	}

	.mai-enrollment__trust input {
		flex: 0 0 auto;
		accent-color: var(--mai-color-accent);
	}

	.mai-enrollment__trust[aria-disabled='true'] {
		color: var(--mai-color-text-muted);
	}

	.mai-enrollment__trust input:focus-visible {
		outline: var(--mai-focus-ring-width) solid var(--mai-focus-ring-color);
		outline-offset: var(--mai-focus-ring-offset);
	}
</style>
