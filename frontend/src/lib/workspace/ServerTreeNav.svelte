<script lang="ts">
	/**
	 * ServerTreeNav — Option B Multi-Server Accordion Tree component.
	 *
	 * Renders every enrolled/fixture server as an expandable `<AccordionGroup>`
	 * so the operator can expand multiple servers simultaneously and launch
	 * server-scoped floating windows for different servers side-by-side.
	 *
	 * Inside each server accordion, panels are organized by logical category
	 * (`Operations`, `AI & Knowledge`, `Governance`) using `<NavItem>` without
	 * repeating the server name on every button. Global / Fleet panel kinds
	 * (`knowledge-models`, `model-settings`, `plugins`) plus every global-only
	 * entry render ONCE under the `Global / Fleet` section and pin the global
	 * scope on open.
	 */
	import AccordionGroup from '$lib/ui/AccordionGroup.svelte';
	import NavItem from '$lib/ui/NavItem.svelte';
	import Select from '$lib/ui/Select.svelte';
	import type { PanelRegistryEntry } from './registry.js';
	import type { PanelKind, ServerId } from './types.js';

	type ServerOption = { id: ServerId; label: string };

	type Props = {
		/** Available servers (live enrolled profiles or fixture servers). */
		servers: readonly ServerOption[];
		/** Default server id used by the chat dock & keyboard/test selector; bindable. */
		selectedServerId: ServerId;
		/** Active panel registry entries for the current mode (live or fixture). */
		entries: readonly PanelRegistryEntry[];
		/** Whether the shell is connected to a live backend session. */
		live?: boolean;
		/** Callback to open a panel pinned to a specific server (or global scope). */
		onopenpanel: (kind: PanelKind, serverId?: ServerId) => void;
		/** Callback to focus the bottom chat dock (pinned to a specific server). */
		onfocuschat?: (serverId: ServerId) => void;
	};

	let {
		servers,
		selectedServerId = $bindable(),
		entries,
		live = false,
		onopenpanel,
		onfocuschat
	}: Props = $props();

	/** Per-server accordion expansion map (all servers default to expanded so
	 *  multi-server windows can be opened with a single click). */
	let expandedByServer = $state<Record<string, boolean>>({});

	function isServerExpanded(serverId: string): boolean {
		return expandedByServer[serverId] ?? true;
	}

	function setServerExpanded(serverId: string, open: boolean): void {
		expandedByServer = { ...expandedByServer, [serverId]: open };
	}

	/**
	 * Panel kinds rendered ONCE in the Global / Fleet section. They never
	 * repeat inside a server accordion, and a global-section open pins the
	 * `{ kind: 'global' }` scope.
	 */
	const GLOBAL_KINDS: readonly string[] = ['knowledge-models', 'model-settings', 'plugins'];

	const serverScopedEntries = $derived(
		entries.filter(
			(entry) =>
				entry.scope_types.has('server') &&
				!GLOBAL_KINDS.includes(entry.panel_kind) &&
				entry.panel_kind !== 'chat'
		)
	);

	const globalEntries = $derived(
		entries.filter(
			(entry) => !entry.scope_types.has('server') || GLOBAL_KINDS.includes(entry.panel_kind)
		)
	);

	const CATEGORY_ORDER: readonly { id: string; title: string; kinds: readonly string[] }[] = [
		{
			id: 'ops',
			title: 'Operations',
			kinds: ['applications', 'server-profile', 'jobs', 'execution']
		},
		{
			id: 'ai',
			title: 'AI & Knowledge',
			kinds: ['docs']
		},
		{
			id: 'gov',
			title: 'Governance & Extensions',
			kinds: ['capability-policy', 'plugin-forms']
		}
	];

	const ICON_BY_KIND: Readonly<Record<string, string>> = {
		applications: '◈',
		'server-profile': '🖥',
		jobs: '⏱',
		execution: '⌨',
		docs: '📄',
		'knowledge-models': '🧠',
		'model-settings': '⚙',
		'capability-policy': '🛡',
		'plugin-forms': '🧩',
		plugins: '🔌',
		overview: '🌐',
		'fixture-notes': '📝',
		'fixture-prefs': '⚙',
		'fixture-broken': '⚠'
	};

	function iconFor(kind: string): string {
		return ICON_BY_KIND[kind] ?? '•';
	}

	function categorizedFor(list: readonly PanelRegistryEntry[]): {
		title: string;
		items: PanelRegistryEntry[];
	}[] {
		const assigned = new Set<string>();
		const groups: { title: string; items: PanelRegistryEntry[] }[] = [];
		for (const cat of CATEGORY_ORDER) {
			const items = list.filter((entry) => cat.kinds.includes(entry.panel_kind));
			if (items.length > 0) {
				for (const item of items) assigned.add(item.panel_kind);
				groups.push({ title: cat.title, items });
			}
		}
		const remaining = list.filter((entry) => !assigned.has(entry.panel_kind));
		if (remaining.length > 0) {
			groups.push({ title: groups.length > 0 ? 'Other' : '', items: remaining });
		}
		return groups;
	}

	const groupedServerPanels = $derived(categorizedFor(serverScopedEntries));

	function onServerExpanded(serverId: string, next: boolean): void {
		setServerExpanded(serverId, next);
		if (next) {
			selectedServerId = serverId;
			// Collapse the other accordion sections so the active selection is
			// always the single expanded tree node in the drawer.
			for (const server of servers) {
				if (server.id !== serverId) expandedByServer[server.id] = false;
			}
		}
	}

	$effect(() => {
		// Keyboard / select change ⇒ the accordion of the new active server
		// expands while all other servers collapse (Option B multi-server tree).
		expandedByServer[selectedServerId] = true;
		for (const server of servers) {
			if (server.id !== selectedServerId) expandedByServer[server.id] = false;
		}
	});
</script>

<div class="mai-server-tree" data-testid="server-tree-nav">
	<div class="mai-server-tree__section-head">
		<span class="mai-server-tree__section-title">Servers</span>
		<span class="mai-server-tree__section-count">{servers.length}</span>
	</div>

	<!-- Compact active-server selector (keeps keyboard & Playwright `server-select`
	     contract intact and sets the default server for the Chat Dock). -->
	<div class="mai-server-tree__default-picker">
		<Select
			id="mai-desktop-server-select"
			label="Target server for new windows"
			hideLabel
			compact
			bind:value={selectedServerId}
			data-testid="server-select"
		>
			{#each servers as server (server.id)}
				<option value={server.id}>Active: {server.label}</option>
			{/each}
		</Select>
	</div>

	<!-- Option B: Multi-Server Expandable Accordion Tree -->
	<div class="mai-server-tree__list">
		{#each servers as server (server.id)}
			{@const isSelected = server.id === selectedServerId}
			<AccordionGroup
				title={server.label}
				icon="🖥"
				badge={isSelected ? 'active' : ''}
				open={isServerExpanded(server.id)}
				ontoggle={(next) => onServerExpanded(server.id, next)}
				toggleTestId={'server-group-toggle-' + server.id}
			>
				{#each groupedServerPanels as group (group.title || 'default')}
					{#if group.title}
						<div class="mai-server-tree__subhead">{group.title}</div>
					{/if}
					{#each group.items as entry (entry.panel_kind)}
						<NavItem
							icon={iconFor(entry.panel_kind)}
							label={entry.title}
							tabindex={isSelected ? 0 : -1}
							srPrefix="Open "
							srSuffix={' — ' + server.label}
							data-testid={isSelected ? 'open-' + entry.panel_kind : 'open-' + entry.panel_kind + '-' + server.id}
							onclick={() => {
								selectedServerId = server.id;
								onopenpanel(entry.panel_kind, server.id);
							}}
						/>
					{/each}
				{/each}
				{#if live && onfocuschat}
					<NavItem
						icon="💬"
						label="Chat on this server"
						srPrefix="Open "
						srSuffix={' — ' + server.label}
						data-testid={isSelected ? 'open-chat' : 'open-chat-' + server.id}
						onclick={() => {
							selectedServerId = server.id;
							onfocuschat(server.id);
						}}
					/>
				{/if}
			</AccordionGroup>
		{/each}
	</div>

	{#if globalEntries.length > 0}
		<div class="mai-server-tree__section-head">
			<span class="mai-server-tree__section-title">Global / Fleet</span>
		</div>
		<AccordionGroup title="Workspace & Fleet" icon="🌐" open={true}>
			{#each globalEntries as entry (entry.panel_kind)}
				<NavItem
					icon={iconFor(entry.panel_kind)}
					label={entry.title}
					srPrefix="Open "
					srSuffix=" — Global"
					data-testid={'open-' + entry.panel_kind}
					onclick={() => onopenpanel(entry.panel_kind)}
				/>
			{/each}
		</AccordionGroup>
	{/if}
</div>

<style>
	.mai-server-tree {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
	}

	.mai-server-tree__section-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0 var(--mai-space-1);
	}

	.mai-server-tree__section-title {
		font-family: var(--mai-font-display);
		font-size: 0.7rem;
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--mai-color-brass);
	}

	.mai-server-tree__section-count {
		font-family: var(--mai-font-mono);
		font-size: 0.7rem;
		color: var(--mai-color-text-muted);
	}

	.mai-server-tree__default-picker {
		display: flex;
		flex-direction: column;
	}

	.mai-server-tree__list {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
	}

	.mai-server-tree__subhead {
		padding: var(--mai-space-1) var(--mai-space-2) 2px;
		font-family: var(--mai-font-mono);
		font-size: 0.65rem;
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-text-muted);
	}
</style>
