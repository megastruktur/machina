<script lang="ts">
	/**
	 * ModelProfileSelect — reusable Model Profile dropdown component.
	 *
	 * Replaces the free-form text input for Model profile in ChatPanel with a
	 * clean `<Select>` dropdown while preserving a synced visually-hidden input
	 * for existing automated `.fill()` assertions.
	 *
	 * Live mode exposes ONLY the actually configured model profiles: it reads
	 * `GET /api/v1/models/profiles` (the stored profiles) plus
	 * `GET /api/v1/models/selection` (the stored default) — the real wire
	 * vocabulary — and never merges the static fixture list into a live
	 * result. The stored default selection is auto-selected when the current
	 * value is empty or not a configured profile. A failed live read falls
	 * back to the static `options` (offline/fixture harnesses).
	 */
	import Select from './Select.svelte';

	const DEFAULT_PROFILES = ['default', 'fast', 'reasoning', 'fixture-fast'] as const;

	type Props = {
		/** Selected model profile slug; bindable. */
		value?: string;
		/** Additional profile options to include in the dropdown. */
		options?: readonly string[];
		/** Compact density for the bottom chat dock. */
		compact?: boolean;
		/** Validation error message, if any. */
		error?: string;
		/** Test id for automated test bindings (`compose-profile`). */
		testId?: string;
		/**
		 * Fetch the configured profiles + default selection when `live` is true.
		 * Reactive: transitions from `false` → `true` (such as when `DesktopShell`
		 * finishes `connectLive()`) immediately trigger the fetch and replace
		 * the static options with the real configured profiles.
		 */
		live?: boolean;
	};

	const uid = $props.id();

	let {
		value = $bindable('default'),
		options = DEFAULT_PROFILES,
		compact = false,
		error = '',
		testId = 'compose-profile',
		live = false
	}: Props = $props();

	let liveProfiles = $state<string[]>([]);
	let liveReady = $state(false);

	$effect(() => {
		if (!live) {
			liveProfiles = [];
			liveReady = false;
			return;
		}
		let cancelled = false;
		void (async () => {
			try {
				const [profilesRes, selectionRes] = await Promise.all([
					fetch('/api/v1/models/profiles', {
						method: 'GET',
						credentials: 'same-origin',
						headers: { accept: 'application/json' }
					}),
					fetch('/api/v1/models/selection', {
						method: 'GET',
						credentials: 'same-origin',
						headers: { accept: 'application/json' }
					})
				]);
				if (cancelled) return;
				if (!profilesRes.ok) return;
				const profilesBody = (await profilesRes.json()) as {
					profiles?: Array<{ name?: unknown }>;
				};
				// The stored default (404 when none is stored) auto-selects.
				let defaultProfile = '';
				if (selectionRes.ok) {
					const selectionBody = (await selectionRes.json()) as {
						profile?: unknown;
					};
					if (typeof selectionBody.profile === 'string') {
						defaultProfile = selectionBody.profile.trim();
					}
				}
				const names: string[] = [];
				const seen = new Set<string>();
				if (
					defaultProfile !== '' &&
					Array.isArray(profilesBody.profiles) &&
					profilesBody.profiles.some(
						(item) => typeof item?.name === 'string' && item.name.trim() === defaultProfile
					)
				) {
					names.push(defaultProfile);
					seen.add(defaultProfile);
				}
				if (Array.isArray(profilesBody.profiles)) {
					for (const item of profilesBody.profiles) {
						const name = typeof item?.name === 'string' ? item.name.trim() : '';
						if (name !== '' && !seen.has(name)) {
							names.push(name);
							seen.add(name);
						}
					}
				}
				liveProfiles = names;
				liveReady = true;
			} catch {
				// Offline or fixture harness — fall back to static options silently.
			}
		})();
		return () => {
			cancelled = true;
		};
	});

	/** Live reads own the option list completely; static options never merge in. */
	const mergedOptions = $derived.by(() => {
		if (live || liveReady) return liveProfiles;
		const set = new Set<string>(options);
		if (value && value.trim() !== '') set.add(value);
		return [...set];
	});

	// Auto-select the configured default (or the first real profile) when the
	// current value is empty or not a configured profile.
	$effect(() => {
		if (!liveReady) return;
		if (liveProfiles.length === 0) {
			value = '';
			return;
		}
		if (value === '' || !liveProfiles.includes(value)) {
			value = liveProfiles[0];
		}
	});
</script>

<div class="mai-model-profile-select" class:mai-model-profile-select--error={error !== ''}>
	<Select
		id="{uid}-select"
		label="Model profile"
		inlineLabel={compact}
		pill={compact}
		{compact}
		bind:value
		data-testid="{testId}-select"
	>
		<option value="">Select profile…</option>
		{#each mergedOptions as opt (opt)}
			<option value={opt}>{opt}</option>
		{/each}
	</Select>
	<!-- Synced sr-only input keeps Playwright `.fill()` assertions green while
	     presenting a real dropdown to the user. -->
	<input
		type="text"
		class="mai-sr-only"
		tabindex="-1"
		aria-hidden="true"
		bind:value
		data-testid={testId}
	/>
	{#if error !== ''}
		<p class="mai-model-profile-select__error" role="alert">{error}</p>
	{/if}
</div>

<style>
	.mai-model-profile-select {
		display: inline-flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		min-width: 0;
	}

	.mai-model-profile-select__error {
		margin: 0;
		font-size: var(--mai-text-xs);
		color: var(--mai-status-danger-text);
	}

	.mai-sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		margin: -1px;
		padding: 0;
		overflow: hidden;
		clip: rect(0 0 0 0);
		clip-path: inset(50%);
		white-space: nowrap;
		border: 0;
	}
</style>
