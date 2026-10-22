<script lang="ts">
	/**
	 * PluginsPanel — the standalone Plugins desktop window (card
	 * `s4-plugins-window`). Native plugin cards (starting with 1Password
	 * Vault), secret readiness, enable/disable with the M6 credential gate,
	 * value-free connection probes, `default_vault`/`allowed_vaults`
	 * configuration with write-only credential values, and one-click agent
	 * grants (global all-servers or the active server).
	 *
	 *  - Explicit services only: the plugins API enters through
	 *    `panel.services.plugins` (typed shape owned by the feature entry,
	 *    satisfied by `pluginsPanelServices` at desktop composition). A
	 *    missing/incomplete bag fails closed with a visible state.
	 *  - Scope honesty (C01 rule 2 / C11): the binding target server is
	 *    `panel.scope.server_id` when server-pinned, otherwise an explicit
	 *    operator pick — never hidden global state.
	 *  - Bounded pagination (AGENTS §1): PAGE_LIMIT-sized initial page,
	 *    tracked `offset`/`total`/`hasMore`/`loadingMore`, "Load more"
	 *    inside a bounded scrollable container.
	 *  - Grants stay explicit (C03 rule 7 / H5): configuring, enabling and
	 *    probing never grant; a binding is its own write, and `secret.store`
	 *    runs through the policy's ask/delegated approval semantics.
	 */
	import { ApiError } from '../api/client.js';
	import {
		readPluginsServices,
		type BindingView,
		type ConfiguredPluginView,
		type InstalledPluginView,
		type PluginCatalogView,
		type PluginConfigEditBody,
		type PluginProbeView,
		type PluginsPanelServices
	} from '../workspace/entries/12-web-plugins.js';
	import type { PanelContext } from '../workspace/registry.js';
	import Button from '../ui/Button.svelte';
	import Select from '../ui/Select.svelte';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	const services = $derived(readPluginsServices(panel));
	const pinnedServerId = $derived(panel.scope.kind === 'server' ? panel.scope.server_id : null);

	/** The global all-servers grant target (`mai` store convention). */
	const GLOBAL_SERVER_ID = '00000000-0000-0000-0000-000000000000';

	/** Bounded initial/read page size (AGENTS §1 mandatory pagination). */
	const PAGE_LIMIT = 10;

	/** One merged catalogue row: installed metadata + configured document. */
	type PluginCard = {
		slug: string;
		installed: InstalledPluginView | null;
		configured: ConfiguredPluginView | null;
	};

	/** Per-plugin draft of the configuration drawer. */
	type PluginDraft = {
		default_vault: string;
		allowed_vaults_text: string;
		/** Write-only credential values; blank means "keep the stored one". */
		secrets: Record<string, string>;
	};

	type LoadState =
		| { status: 'services-missing' }
		| { status: 'loading' }
		| { status: 'error'; message: string }
		| { status: 'ready' };

	type ProbeState =
		| { status: 'idle' }
		| { status: 'running' }
		| { status: 'done'; result: PluginProbeView }
		| { status: 'error'; message: string };

	type SaveState =
		| { status: 'idle' }
		| { status: 'saving' }
		| { status: 'saved'; revision: number }
		| { status: 'error'; message: string };

	let view = $state<LoadState>({ status: 'loading' });
	let cards = $state<PluginCard[]>([]);
	let offset = $state(0);
	let total = $state(0);
	let hasMore = $state(false);
	let loadingMore = $state(false);

	let expandedSlug = $state<string | null>(null);
	let drafts = $state<Record<string, PluginDraft>>({});
	let probes = $state<Record<string, ProbeState>>({});
	let saves = $state<Record<string, SaveState>>({});
	let rowBusy = $state<Record<string, boolean>>({});
	let bindingErrors = $state<Record<string, string>>({});

	/** Enrolled servers (global scope only) for the binding target picker. */
	let servers = $state<{ id: string; label: string }[]>([]);
	let pickedServerId = $state('');

	/** The server a per-server grant targets: pinned scope wins over the pick. */
	const activeServerId = $derived(
		pinnedServerId ?? servers.find((server) => server.id === pickedServerId)?.id ?? null
	);

	/**
	 * Displayed catalogue total: the backend `total` counts configured
	 * documents only while `cards` merges installed-but-unconfigured rows
	 * in — never render "Showing 1 of 0".
	 */
	const displayTotal = $derived(Math.max(total, cards.length));

	// --- derived card facts ---------------------------------------------------

	function requiredCredentials(card: PluginCard): string[] {
		return card.installed?.required_credentials ?? card.configured?.credential_refs ?? [];
	}

	function configuredCredentials(card: PluginCard): Set<string> {
		const names = new Set<string>();
		for (const name of card.installed?.configured_credentials ?? []) names.add(name);
		for (const name of card.configured?.configured_credentials ?? []) names.add(name);
		return names;
	}

	/** Required credentials still missing from the runtime env (badge data). */
	function missingCredentials(card: PluginCard): string[] {
		const configured = configuredCredentials(card);
		return requiredCredentials(card).filter((name) => !configured.has(name));
	}

	/** M6 gate: a plugin with unfilled required credentials cannot enable. */
	function secretSatisfied(card: PluginCard): boolean {
		const missing = missingCredentials(card);
		if (missing.length === 0) return true;
		const draft = drafts[card.slug];
		if (!draft) return false;
		return missing.every((name) => (draft.secrets[name] ?? '').trim() !== '');
	}

	function capabilityAccess(capability: string): 'read' | 'mutate' {
		return capability === 'secret.list' || capability === 'secret.get' ? 'read' : 'mutate';
	}

	function declaredCapabilities(card: PluginCard): string[] {
		if (card.installed !== null && card.installed.capabilities.length > 0) {
			return card.installed.capabilities;
		}
		// Uninstalled-but-configured: only what an existing grant already names.
		const union = new Set<string>();
		for (const binding of card.configured?.bindings ?? []) {
			for (const capability of binding.capabilities) union.add(capability);
		}
		return [...union];
	}

	function bindingFor(card: PluginCard, serverId: string): BindingView | null {
		return card.configured?.bindings.find((binding) => binding.server_id === serverId) ?? null;
	}

	function draftFor(card: PluginCard): PluginDraft {
		return (
			drafts[card.slug] ?? {
				default_vault: 'Secrets',
				allowed_vaults_text: 'Secrets',
				secrets: {}
			}
		);
	}

	function seedDraft(card: PluginCard): PluginDraft {
		const config = card.configured?.config ?? {};
		const storedVault = config['default_vault'];
		const storedVaults = config['allowed_vaults'];
		const secrets: Record<string, string> = {};
		for (const name of requiredCredentials(card)) secrets[name] = '';
		return {
			default_vault: typeof storedVault === 'string' && storedVault !== '' ? storedVault : 'Secrets',
			allowed_vaults_text: Array.isArray(storedVaults)
				? storedVaults.filter((v): v is string => typeof v === 'string').join(', ')
				: 'Secrets',
			secrets
		};
	}

	function serverLabel(serverId: string): string {
		const server = servers.find((candidate) => candidate.id === serverId);
		return server === undefined ? serverId : `${server.label} · ${serverId}`;
	}

	function errorText(err: unknown): string {
		if (err instanceof ApiError) return `${err.status} ${err.code}: ${err.message}`;
		return err instanceof Error ? err.message : String(err);
	}

	// --- catalogue loading (bounded pagination, AGENTS §1) --------------------

	function absorbPage(page: PluginCatalogView): void {
		const seen = new Set(cards.map((card) => card.slug));
		const fresh: PluginCard[] = [];
		for (const installed of page.installed_plugins ?? []) {
			if (seen.has(installed.plugin)) continue;
			seen.add(installed.plugin);
			fresh.push({
				slug: installed.plugin,
				installed,
				configured: page.configured.find((row) => row.plugin === installed.plugin) ?? null
			});
		}
		// Configured-but-uninstalled documents surface too — never dropped.
		for (const configured of page.configured) {
			if (seen.has(configured.plugin)) continue;
			seen.add(configured.plugin);
			fresh.push({ slug: configured.plugin, installed: null, configured });
		}
		cards = [...cards, ...fresh];
	}

	function trackPage(page: PluginCatalogView): void {
		offset = page.offset + page.configured.length;
		total = page.total;
		hasMore = page.configured.length > 0 && offset < page.total;
	}

	async function loadFirstPage(signal: { disposed: boolean }): Promise<void> {
		if (services === null) {
			view = { status: 'services-missing' };
			return;
		}
		view = { status: 'loading' };
		try {
			const page = await services.pluginCatalog({ limit: PAGE_LIMIT, offset: 0 });
			if (signal.disposed) return;
			cards = [];
			absorbPage(page);
			trackPage(page);
			view = { status: 'ready' };
		} catch (err) {
			if (signal.disposed) return;
			view = { status: 'error', message: errorText(err) };
		}
	}

	async function loadMore(): Promise<void> {
		if (services === null || loadingMore || !hasMore) return;
		loadingMore = true;
		try {
			const page = await services.pluginCatalog({ limit: PAGE_LIMIT, offset });
			absorbPage(page);
			trackPage(page);
		} catch (err) {
			view = { status: 'error', message: errorText(err) };
		} finally {
			loadingMore = false;
		}
	}

	$effect(() => {
		// Mount cycle: one initial page (+ the enrolled targets when the
		// window is global-scoped and needs a binding target picker).
		const signal = { disposed: false };
		void loadFirstPage(signal);
		if (services !== null && pinnedServerId === null) {
			services
				.listServers()
				.then((profiles) => {
					if (signal.disposed) return;
					servers = profiles.map((profile) => ({ id: profile.server_id, label: profile.name }));
					pickedServerId = profiles[0]?.server_id ?? '';
				})
				.catch(() => {
					// Honest empty picker; global grants remain available.
					if (!signal.disposed) servers = [];
				});
		}
		return () => {
			signal.disposed = true;
		};
	});

	// --- writes: config, probe, bindings --------------------------------------

	function openConfigure(card: PluginCard): void {
		if (expandedSlug === card.slug) {
			expandedSlug = null;
			return;
		}
		if (drafts[card.slug] === undefined) drafts[card.slug] = seedDraft(card);
		probes[card.slug] = { status: 'idle' };
		saves[card.slug] = { status: 'idle' };
		expandedSlug = card.slug;
	}

	/** Re-read one card's stored document + bindings after a write. */
	async function refreshCard(slug: string, previousKind: string): Promise<void> {
		if (services === null) return;
		const [config, bindings] = await Promise.all([
			services.getPluginConfig(slug),
			services.listPluginBindings(slug)
		]);
		cards = cards.map((card) =>
			card.slug === slug
				? { ...card, configured: { ...config, kind: previousKind, bindings } }
				: card
		);
	}

	/**
	 * Full-document config edit (producer re-validates). Credential values
	 * ride the write-only `credential_values` map; blank stays stored.
	 */
	async function saveConfig(card: PluginCard, nextEnabled: boolean): Promise<void> {
		if (services === null || rowBusy[card.slug] === true) return;
		rowBusy[card.slug] = true;
		saves[card.slug] = { status: 'saving' };
		try {
			const draft = drafts[card.slug] ?? seedDraft(card);
			const credentialValues: Record<string, string> = {};
			for (const [name, value] of Object.entries(draft.secrets)) {
				const trimmed = value.trim();
				if (trimmed !== '') credentialValues[name] = trimmed;
			}
			const configDocument: Record<string, unknown> =
				card.installed === null
					? // No installed metadata: keep the stored document verbatim.
						(card.configured?.config ?? {})
					: card.installed.config_schema == null
						? // Schema-less plugin: the producer rejects any non-empty config.
							{}
						: {
								default_vault:
									draft.default_vault.trim() === '' ? 'Secrets' : draft.default_vault.trim(),
								allowed_vaults: draft.allowed_vaults_text
									.split(',')
									.map((vault) => vault.trim())
									.filter((vault) => vault !== '')
							};
			const payload: PluginConfigEditBody = {
				expected_revision: card.configured?.revision ?? 0,
				enabled: nextEnabled,
				credential_refs: card.configured?.credential_refs ?? requiredCredentials(card),
				config: configDocument
			};
			if (Object.keys(credentialValues).length > 0) payload.credential_values = credentialValues;
			const triple = await services.putPluginConfig(card.slug, payload);
			await refreshCard(card.slug, card.configured?.kind ?? (card.installed ? 'native' : 'uninstalled'));
			drafts[card.slug] = { ...draft, secrets: seedDraft(card).secrets };
			saves[card.slug] = { status: 'saved', revision: triple.revision };
		} catch (err) {
			saves[card.slug] = { status: 'error', message: errorText(err) };
		} finally {
			rowBusy[card.slug] = false;
		}
	}

	function toggleEnabled(card: PluginCard): void {
		void saveConfig(card, !(card.configured?.enabled ?? false));
	}

	async function runProbe(card: PluginCard): Promise<void> {
		if (services === null) return;
		probes[card.slug] = { status: 'running' };
		try {
			const draft = drafts[card.slug];
			const vault = (draft?.default_vault ?? '').trim();
			const result = await services.probePlugin(card.slug, { vault: vault === '' ? null : vault });
			probes[card.slug] = { status: 'done', result };
		} catch (err) {
			probes[card.slug] = { status: 'error', message: errorText(err) };
		}
	}

	async function grantAll(card: PluginCard, serverId: string): Promise<void> {
		if (services === null) return;
		const key = `${card.slug}:${serverId}`;
		bindingErrors[key] = '';
		rowBusy[key] = true;
		try {
			await services.putPluginBinding(card.slug, serverId, {
				expected_revision: bindingFor(card, serverId)?.revision ?? 0,
				capabilities: declaredCapabilities(card)
			});
			await refreshCard(card.slug, card.configured?.kind ?? (card.installed ? 'native' : 'uninstalled'));
		} catch (err) {
			bindingErrors[key] = errorText(err);
		} finally {
			rowBusy[key] = false;
		}
	}

	async function revokeAll(card: PluginCard, serverId: string): Promise<void> {
		if (services === null) return;
		const existing = bindingFor(card, serverId);
		if (existing === null) return;
		const key = `${card.slug}:${serverId}`;
		bindingErrors[key] = '';
		rowBusy[key] = true;
		try {
			await services.deletePluginBinding(card.slug, serverId, existing.revision);
			await refreshCard(card.slug, card.configured?.kind ?? (card.installed ? 'native' : 'uninstalled'));
		} catch (err) {
			bindingErrors[key] = errorText(err);
		} finally {
			rowBusy[key] = false;
		}
	}
</script>

<div class="mai-pluginswin" data-testid="plugins-panel">
	<header class="mai-pluginswin__header">
		<h2>Plugins</h2>
		<p class="mai-pluginswin__scope">
			{#if pinnedServerId !== null}
				Native plugins for server
				<span class="mai-pluginswin__mono">{pinnedServerId}</span>. Grants below target this
				server or all servers globally.
			{:else}
				Controller-host native plugins. Configure secrets and vaults, then grant agent
				access globally or per server.
			{/if}
		</p>
	</header>

	{#if services === null}
		<div class="mai-pluginswin__state mai-pluginswin__state--error" role="alert" data-testid="plugins-unusable">
			The plugins API seam is missing or incomplete — the panel fails closed and performs
			nothing. No fallback client is substituted.
		</div>
	{:else if view.status === 'loading'}
		<div class="mai-pluginswin__state" role="status" data-testid="plugins-loading">
			Reading the plugin catalogue…
		</div>
	{:else if view.status === 'error'}
		<div class="mai-pluginswin__state mai-pluginswin__state--error" role="alert" data-testid="plugins-error">
			Catalogue unavailable — {view.message}
		</div>
	{:else if cards.length === 0}
		<div class="mai-pluginswin__state" role="status" data-testid="plugins-empty">
			No plugins installed or configured yet.
		</div>
	{:else}
		<p class="mai-pluginswin__meta" data-testid="plugins-count">
			Showing {cards.length} of {displayTotal} plugin{displayTotal === 1 ? '' : 's'}
		</p>
		<div class="mai-pluginswin__list" data-testid="plugins-list">
			{#each cards as card (card.slug)}
				{@const missing = missingCredentials(card)}
				{@const enabled = card.configured?.enabled ?? false}
				{@const enableBlocked = !enabled && !secretSatisfied(card)}
				{@const probe = probes[card.slug] ?? { status: 'idle' }}
				{@const save = saves[card.slug] ?? { status: 'idle' }}
				<article class="mai-pluginswin__card" data-testid="plugin-card" data-plugin={card.slug}>
					<div class="mai-pluginswin__card-head">
						<span class="mai-pluginswin__card-title">{card.installed?.title ?? card.slug}</span>
						<span class="mai-pluginswin__mono mai-pluginswin__slug">{card.slug}</span>
						<span class="mai-pluginswin__badge" data-testid="plugin-kind">
							{card.installed === null ? (card.configured?.kind ?? 'uninstalled') : 'native'}
						</span>
						<span
							class="mai-pluginswin__badge"
							class:mai-pluginswin__badge--on={enabled}
							data-testid="plugin-enabled"
						>
							{enabled ? 'enabled' : 'disabled'}
						</span>
					</div>
					{#if card.installed?.description}
						<p class="mai-pluginswin__description">{card.installed.description}</p>
					{/if}

					<ul class="mai-pluginswin__chips" data-testid="plugin-capabilities">
						{#each declaredCapabilities(card) as capability (capability)}
							<li class="mai-pluginswin__chip">
								{capability}
								<small>({capabilityAccess(capability)})</small>
							</li>
						{/each}
					</ul>

					<ul class="mai-pluginswin__secrets" data-testid="plugin-credentials">
						{#each requiredCredentials(card) as name (name)}
							<li
								class="mai-pluginswin__secret"
								class:mai-pluginswin__secret--ok={!missing.includes(name)}
								data-testid="plugin-credential"
							>
								{missing.includes(name) ? '○' : '●'}
								{missing.includes(name) ? `${name} Required` : `${name} Configured in credentials.env`}
							</li>
						{/each}
					</ul>

					<div class="mai-pluginswin__actions">
						<Button
							variant={enabled ? 'secondary' : 'primary'}
							loading={rowBusy[card.slug] === true}
							disabled={enableBlocked}
							onclick={() => toggleEnabled(card)}
							data-testid="plugin-toggle"
						>
							{enabled ? 'Disable' : 'Enable'}
						</Button>
						<Button
							variant="secondary"
							loading={probe.status === 'running'}
							onclick={() => runProbe(card)}
							data-testid="plugin-probe-run"
						>
							Test Connection
						</Button>
						<Button
							variant="secondary"
							onclick={() => openConfigure(card)}
							data-testid="plugin-configure"
						>
							{expandedSlug === card.slug ? 'Close configuration' : 'Configure'}
						</Button>
					</div>
					{#if enableBlocked}
						<p class="mai-pluginswin__hint" data-testid="plugin-toggle-blocked">
							Enable needs {missing.join(', ')} — paste it under Configure (stored write-only in
							credentials.env, never in the plugin config).
						</p>
					{/if}

					{#if probe.status === 'done'}
						<div
							class="mai-pluginswin__state"
							class:mai-pluginswin__state--ok={probe.result.available}
							class:mai-pluginswin__state--error={!probe.result.available}
							role="status"
							data-testid="plugin-probe"
						>
							{#if probe.result.available}
								Ready — provider check passed
							{:else}
								Not ready — {probe.result.code}: {probe.result.detail}
							{/if}
							{#if probe.result.checked_vault !== null && probe.result.checked_vault !== undefined}
								(vault: <span class="mai-pluginswin__mono">{probe.result.checked_vault}</span>)
							{/if}
						</div>
					{:else if probe.status === 'error'}
						<div class="mai-pluginswin__state mai-pluginswin__state--error" role="alert" data-testid="plugin-probe-error">
							Probe refused — {probe.message}
						</div>
					{/if}

					{#if expandedSlug === card.slug && drafts[card.slug] !== undefined}
						{@const draft = drafts[card.slug]}
						<section class="mai-pluginswin__drawer" data-testid="plugin-drawer">
							<h3>Configuration, Secret &amp; Agent Access</h3>

							{#each requiredCredentials(card) as name (name)}
								<label class="mai-pluginswin__field">
									<span>{name} (write-only)</span>
									<input
										type="password"
										autocomplete="off"
										spellcheck="false"
										placeholder={name === 'OP_SERVICE_ACCOUNT_TOKEN'
											? 'Paste ops_... token (leave blank to keep existing)'
											: `Paste ${name} value (leave blank to keep existing)`}
										bind:value={draft.secrets[name]}
										data-testid="plugin-secret-input"
									/>
								</label>
							{/each}

							<label class="mai-pluginswin__field">
								<span>Default vault</span>
								<input
									type="text"
									placeholder="Secrets"
									bind:value={draft.default_vault}
									data-testid="plugin-default-vault"
								/>
							</label>
							<label class="mai-pluginswin__field">
								<span>Allowed vaults (comma-separated)</span>
								<input
									type="text"
									placeholder="Secrets"
									bind:value={draft.allowed_vaults_text}
									data-testid="plugin-allowed-vaults"
								/>
							</label>

							<div class="mai-pluginswin__actions">
								<Button
									loading={save.status === 'saving'}
									onclick={() => saveConfig(card, card.configured?.enabled ?? false)}
									data-testid="plugin-config-save"
								>
									Save configuration
								</Button>
							</div>
							{#if save.status === 'saved'}
								<div class="mai-pluginswin__state mai-pluginswin__state--ok" role="status" data-testid="plugin-config-saved">
									Stored at revision {save.revision}.
								</div>
							{:else if save.status === 'error'}
								<div class="mai-pluginswin__state mai-pluginswin__state--error" role="alert" data-testid="plugin-config-error">
									Save refused — {save.message}
								</div>
							{/if}

							<h3>Agent access (bindings)</h3>
							<p class="mai-pluginswin__hint">
								One click grants every declared capability
								({declaredCapabilities(card).join(', ') || 'none declared'}) to one target.
								Configuring, enabling and probing never grant — a binding is its own
								explicit write.
							</p>
							<p class="mai-pluginswin__hint" data-testid="plugin-store-notice">
								<strong>secret.store</strong> semantics: in <em>ask</em> policy mode each call
								creates a 1-hour pending approval card in Chat; in <em>delegated</em> mode it
								executes immediately (within the grant).
							</p>

							<div class="mai-pluginswin__binding" data-testid="binding-global">
								<div class="mai-pluginswin__binding-main">
									<span class="mai-pluginswin__mono">{GLOBAL_SERVER_ID}</span>
									<span class="mai-pluginswin__binding-label">Global — all servers &amp; general consult</span>
								</div>
								{#if bindingFor(card, GLOBAL_SERVER_ID) !== null}
									{@const binding = bindingFor(card, GLOBAL_SERVER_ID)!}
									<span class="mai-pluginswin__meta">
										granted: {binding.capabilities.join(', ')} · r{binding.revision}
									</span>
									<Button
										variant="danger"
										loading={rowBusy[`${card.slug}:${GLOBAL_SERVER_ID}`] === true}
										onclick={() => revokeAll(card, GLOBAL_SERVER_ID)}
										data-testid="binding-revoke-global"
									>
										Revoke global access
									</Button>
								{:else}
									<Button
										variant="secondary"
										loading={rowBusy[`${card.slug}:${GLOBAL_SERVER_ID}`] === true}
										disabled={declaredCapabilities(card).length === 0}
										onclick={() => grantAll(card, GLOBAL_SERVER_ID)}
										data-testid="binding-grant-global"
									>
										Grant all — global
									</Button>
								{/if}
							</div>

							{#if activeServerId !== null}
								{@const key = `${card.slug}:${activeServerId}`}
								<div class="mai-pluginswin__binding" data-testid="binding-server">
									<div class="mai-pluginswin__binding-main">
										<span class="mai-pluginswin__mono">{activeServerId}</span>
										<span class="mai-pluginswin__binding-label">
											{pinnedServerId !== null ? 'This server' : serverLabel(activeServerId)}
										</span>
									</div>
									{#if bindingFor(card, activeServerId) !== null}
										{@const binding = bindingFor(card, activeServerId)!}
										<span class="mai-pluginswin__meta">
											granted: {binding.capabilities.join(', ')} · r{binding.revision}
										</span>
										<Button
											variant="danger"
											loading={rowBusy[key] === true}
											onclick={() => revokeAll(card, activeServerId)}
											data-testid="binding-revoke-server"
										>
											Revoke server access
										</Button>
									{:else}
										<Button
											variant="secondary"
											loading={rowBusy[key] === true}
											disabled={declaredCapabilities(card).length === 0}
											onclick={() => grantAll(card, activeServerId)}
											data-testid="binding-grant-server"
										>
											Grant all — this server
										</Button>
									{/if}
								</div>
								{#if bindingErrors[key]}
									<div class="mai-pluginswin__state mai-pluginswin__state--error" role="alert" data-testid="binding-error">
										Binding write refused — {bindingErrors[key]}
									</div>
								{/if}
							{:else if pinnedServerId === null}
								<div class="mai-pluginswin__picker">
									<Select
										label="Grant target server"
										bind:value={pickedServerId}
										hint="No servers enrolled yet — global grants remain available."
										data-testid="binding-server-picker"
									>
										{#each servers as server (server.id)}
											<option value={server.id}>{server.label}</option>
										{/each}
									</Select>
								</div>
							{/if}
						</section>
					{/if}
				</article>
			{/each}

			{#if hasMore}
				<div class="mai-pluginswin__more">
					<Button
						variant="secondary"
						loading={loadingMore}
						onclick={loadMore}
						data-testid="plugins-load-more"
					>
						Load more
					</Button>
				</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.mai-pluginswin {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		height: 100%;
		min-height: 0;
	}

	.mai-pluginswin__header h2 {
		margin: 0;
		font-family: var(--mai-font-display);
		font-size: 1rem;
	}

	.mai-pluginswin__scope {
		margin: 2px 0 0;
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}

	.mai-pluginswin__meta {
		margin: 0;
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}

	.mai-pluginswin__list {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		flex: 1;
		min-height: 0;
		max-height: 480px;
		overflow-y: auto;
		padding-right: 2px;
	}

	.mai-pluginswin__card {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-space-2);
		border: 1px solid var(--mai-color-border);
		border-radius: 8px;
		background: var(--mai-color-surface);
	}

	.mai-pluginswin__card-head {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
	}

	.mai-pluginswin__card-title {
		font-weight: var(--mai-weight-semibold);
	}

	.mai-pluginswin__slug {
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}

	.mai-pluginswin__mono {
		font-family: var(--mai-font-mono);
		font-size: 0.72rem;
	}

	.mai-pluginswin__badge {
		font-size: 0.66rem;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		padding: 1px 6px;
		border: 1px solid var(--mai-color-border);
		border-radius: 999px;
		color: var(--mai-color-text-muted);
	}

	.mai-pluginswin__badge--on {
		border-color: var(--mai-status-ok-border, var(--mai-color-border));
		color: var(--mai-status-ok-text, var(--mai-color-text));
	}

	.mai-pluginswin__description {
		margin: 0;
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text-muted);
	}

	.mai-pluginswin__chips,
	.mai-pluginswin__secrets {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mai-space-1);
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.mai-pluginswin__chip {
		font-family: var(--mai-font-mono);
		font-size: 0.7rem;
		padding: 1px 8px;
		border: 1px solid var(--mai-color-border);
		border-radius: 999px;
	}

	.mai-pluginswin__chip small {
		color: var(--mai-color-text-muted);
	}

	.mai-pluginswin__secret {
		font-family: var(--mai-font-mono);
		font-size: 0.7rem;
		color: var(--mai-status-warn-text, var(--mai-color-text-muted));
	}

	.mai-pluginswin__secret--ok {
		color: var(--mai-status-ok-text, var(--mai-color-text));
	}

	.mai-pluginswin__actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mai-space-2);
	}

	.mai-pluginswin__hint {
		margin: 0;
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}

	.mai-pluginswin__state {
		padding: var(--mai-space-2);
		border: 1px solid var(--mai-color-border);
		border-radius: 6px;
		font-size: var(--mai-text-sm);
	}

	.mai-pluginswin__state--ok {
		border-color: var(--mai-status-ok-border, var(--mai-color-border));
	}

	.mai-pluginswin__state--error {
		border-color: var(--mai-status-danger-border);
		color: var(--mai-status-danger-text);
	}

	.mai-pluginswin__drawer {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding-top: var(--mai-space-2);
		border-top: 1px dashed var(--mai-color-border);
	}

	.mai-pluginswin__drawer h3 {
		margin: 0;
		font-size: 0.82rem;
		font-family: var(--mai-font-display);
	}

	.mai-pluginswin__field {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.mai-pluginswin__field span {
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}

	.mai-pluginswin__field input {
		padding: 6px 8px;
		border: 1px solid var(--mai-color-border);
		border-radius: 6px;
		background: var(--mai-color-surface);
		color: var(--mai-color-text);
		font-size: var(--mai-text-sm);
	}

	.mai-pluginswin__binding {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		padding: var(--mai-space-2);
		border: 1px solid var(--mai-color-border);
		border-radius: 6px;
	}

	.mai-pluginswin__binding-main {
		display: flex;
		align-items: baseline;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
	}

	.mai-pluginswin__binding-label {
		font-size: var(--mai-text-sm);
	}

	.mai-pluginswin__picker {
		max-width: 360px;
	}
</style>
