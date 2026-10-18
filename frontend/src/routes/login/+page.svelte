<script lang="ts">
	/**
	 * Login route (card `02-web-session`, auth ADR §10: login lives outside the
	 * privileged desktop). Thin auth adapter only — windowed-workspace ADR §2:
	 * no feature logic, no window state, no drag logic, no server-scoping, no
	 * module-level browser access. Session truth comes from the real API via
	 * the scoped `createSessionController()` instance created per component
	 * instance (no SSR singleton, ADR §5 rule 2); there is no client-side
	 * authentication flag anywhere.
	 *
	 * States rendered (C08 rule 6 — never all-green on unavailable data):
	 * - `checking` before the first probe answers;
	 * - `unauthenticated` renders the login form with the frozen stable error
	 *   codes of the last failed exchange (wrong password vs throttled);
	 * - `authenticated` renders the signed-in card (continue to the workspace
	 *   or sign out) — navigation stays an explicit user action, never an
	 *   automatic redirect;
	 * - `unavailable` renders the boundary failure next to the form.
	 */
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import Button from '$lib/ui/Button.svelte';
	import Field from '$lib/ui/Field.svelte';
	import { createSessionController } from '$lib/auth/session.js';

	const session = createSessionController();

	/** The submitted secret lives only in this binding; cleared after every attempt. */
	let password = $state('');
	let validationError = $state('');

	onMount(() => {
		void session.probe();
	});

	async function submit(event: SubmitEvent): Promise<void> {
		event.preventDefault();
		validationError = password === '' ? 'Enter the administrator password.' : '';
		if (validationError !== '') return;
		const status = await session.login(password);
		password = ''; // never retained after the exchange, success or not
		validationError = '';
		if (status === 'authenticated') {
			// Route-owned deep-link adapter: the desktop lives at `/`.
			await goto('/', { replaceState: true });
		}
	}

	async function signOut(): Promise<void> {
		await session.logout();
	}
</script>

<svelte:head>
	<title>Sign in — MAI</title>
</svelte:head>

<main class="mai-login">
	<h1>MAI</h1>
	<p class="mai-login__context">Administrator sign-in for the desktop workspace.</p>

	{#if $session.status === 'checking'}
		<p class="mai-login__state" role="status" data-testid="login-checking">
			Checking session…
		</p>
	{:else if $session.status === 'authenticated'}
		<section class="mai-login__card" data-testid="login-signed-in" aria-label="Signed in">
			<p class="mai-login__state" role="status">Signed in.</p>
			<div class="mai-login__actions">
				<a class="mai-login__workspace-link" href="/" data-testid="login-open-workspace">
					Open the workspace
				</a>
				<Button variant="secondary" loading={$session.busy} onclick={signOut} data-testid="login-sign-out">
					Sign out
				</Button>
			</div>
		</section>
	{:else}
		<form class="mai-login__card" novalidate onsubmit={submit} data-testid="login-form">
			<Field
				label="Administrator password"
				type="password"
				autocomplete="current-password"
				bind:value={password}
				error={validationError}
				required
			/>
			{#if $session.error}
				<p
					class="mai-login__api-error"
					role="alert"
					data-testid="login-api-error"
					data-error-code={$session.error.code}
				>
					{$session.error.message}
				</p>
			{/if}
			{#if $session.status === 'unavailable'}
				<p class="mai-login__state" role="status" data-testid="login-unavailable">
					The API boundary is unreachable. Retry once it is back.
				</p>
			{/if}
			<Button type="submit" loading={$session.busy} data-testid="login-submit">Sign in</Button>
		</form>
	{/if}
</main>

<style>
	/* Tokens only (windowed-workspace ADR §2: shared primitives backed by
	 * design tokens); this route owns no theme values. */
	.mai-login {
		min-height: 100vh;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: var(--mai-space-3);
		padding: var(--mai-space-5);
		background: var(--mai-color-bg);
		color: var(--mai-color-text);
		font-family: var(--mai-font-sans);
		box-sizing: border-box;
	}

	.mai-login h1 {
		margin: 0;
		font-size: var(--mai-text-2xl);
		letter-spacing: 0.02em;
	}

	.mai-login__context {
		margin: 0;
		color: var(--mai-color-text-muted);
	}

	.mai-login__card {
		width: min(22rem, 100%);
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		padding: var(--mai-space-5);
		background: var(--mai-color-surface);
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		box-sizing: border-box;
	}

	.mai-login__state {
		margin: 0;
		color: var(--mai-color-text-muted);
	}

	.mai-login__actions {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
	}

	.mai-login__workspace-link {
		color: var(--mai-color-primary);
		text-align: center;
		padding: var(--mai-space-2);
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		text-decoration: none;
	}

	.mai-login__workspace-link:hover {
		background: var(--mai-color-bg);
	}

	.mai-login__api-error {
		margin: 0;
		padding: var(--mai-space-2) var(--mai-space-3);
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
		border: 1px solid var(--mai-status-danger-border);
		border-radius: var(--mai-radius-sm);
	}
</style>
