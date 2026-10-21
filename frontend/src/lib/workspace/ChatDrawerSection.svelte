<script lang="ts">
	/**
	 * ChatDrawerSection — Left Side Drawer Chat Controls (`+ New Chat` + `History`).
	 *
	 * Moves `New Chat` and `History` (`Recent Chats`) out of the bottom chat dock
	 * and into the top of the Left Side Drawer so session navigation lives in the
	 * sidebar while the bottom dock stays a clean prompt bar.
	 */
	import AccordionGroup from '$lib/ui/AccordionGroup.svelte';
	import Button from '$lib/ui/Button.svelte';
	import NavItem from '$lib/ui/NavItem.svelte';
	import type { ConversationView } from './entries/06-web-chat.js';

	type Props = {
		/** Recent conversation rows across servers. */
		conversations?: readonly ConversationView[];
		/** Total recorded conversations available across pages. */
		totalConversations?: number;
		/** Whether additional conversation pages can be loaded. */
		hasMoreConversations?: boolean;
		/** Whether a "Load more" request is currently in flight. */
		loadingMoreConversations?: boolean;
		/** Currently active run id in the chat dock, if any. */
		activeRunId?: string | null;
		/** Start a fresh chat session in the bottom dock. */
		onnewchat: () => void;
		/** Open/restore a specific conversation from history in the bottom dock. */
		onselectconversation: (runId: string) => void;
		/** Refresh the conversation list when opening history. */
		onrefreshhistory?: () => void;
		/** Load the next page of conversations. */
		onloadmorehistory?: () => void;
	};

	let {
		conversations = [],
		totalConversations,
		hasMoreConversations = false,
		loadingMoreConversations = false,
		activeRunId = null,
		onnewchat,
		onselectconversation,
		onrefreshhistory,
		onloadmorehistory
	}: Props = $props();

	let historyExpanded = $state(true);

	const historyBadge = $derived(
		totalConversations !== undefined && totalConversations > conversations.length
			? `${conversations.length}/${totalConversations}`
			: String(conversations.length)
	);

	function shortId(id: string): string {
		return id.length > 8 ? `${id.slice(0, 8)}…` : id;
	}

	function formatConversationLabel(row: ConversationView): string {
		const tags =
			row.service_tags && row.service_tags.length > 0
				? row.service_tags.map((t) => `@${t}`).join(' ')
				: `Run ${shortId(row.run_id)}`;
		return tags;
	}
</script>

<div class="mai-chat-drawer" data-testid="drawer-chat-section">
	<Button
		variant="primary"
		size="sm"
		tabindex={-1}
		class="mai-chat-drawer__new-btn"
		data-testid="drawer-new-chat"
		onclick={onnewchat}
	>
		+ New Chat
	</Button>

	<AccordionGroup
		title="Chat History"
		icon="🕓"
		badge={historyBadge}
		bind:open={historyExpanded}
		ontoggle={(open) => {
			if (open) onrefreshhistory?.();
		}}
		toggleTestId="drawer-history-toggle"
	>
		{#if conversations.length === 0}
			<p class="mai-chat-drawer__empty" data-testid="drawer-history-empty">
				No recent conversations.
			</p>
		{:else}
			<div class="mai-chat-drawer__list" data-testid="drawer-history-list">
				{#each conversations as row (row.run_id)}
					<NavItem
						icon="💬"
						label={formatConversationLabel(row)}
						meta={row.state ?? ''}
						active={activeRunId === row.run_id}
						data-testid="drawer-history-item"
						onclick={() => onselectconversation(row.run_id)}
					/>
				{/each}
			</div>
			{#if hasMoreConversations}
				<div class="mai-chat-drawer__more">
					<Button
						variant="secondary"
						size="sm"
						tabindex={-1}
						class="mai-chat-drawer__more-btn"
						loading={loadingMoreConversations}
						data-testid="drawer-history-load-more"
						onclick={() => onloadmorehistory?.()}
					>
						Load more ({conversations.length}{totalConversations !== undefined ? ` of ${totalConversations}` : ''})
					</Button>
				</div>
			{/if}
		{/if}
	</AccordionGroup>
</div>

<style>
	.mai-chat-drawer {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
	}

	:global(.mai-chat-drawer__new-btn) {
		width: 100%;
	}

	.mai-chat-drawer__list {
		display: flex;
		flex-direction: column;
		gap: 2px;
		max-height: 15.5rem;
		overflow-y: auto;
		overscroll-behavior: contain;
		scrollbar-gutter: stable;
	}

	.mai-chat-drawer__more {
		padding-top: var(--mai-space-1);
		border-top: var(--mai-border-width) solid var(--mai-color-border);
	}

	:global(.mai-chat-drawer__more-btn) {
		width: 100%;
	}

	.mai-chat-drawer__empty {
		margin: 0;
		padding: var(--mai-space-2);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}
</style>
