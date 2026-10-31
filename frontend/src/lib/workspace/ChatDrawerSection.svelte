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
	import HistoryTags, { type TagPageLoader } from '$lib/ui/HistoryTags.svelte';
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
		/** Rename a specific conversation in history. */
		onrenameconversation?: (runId: string, title: string) => Promise<void> | void;
		/**
		 * One row's bounded tag-page adapter (history-tags W7A): the composer
		 * decides the correct route (legacy run rows read the job tag routes,
		 * canonical conversation rows the conversation tag routes) with the
		 * row's recorded server scope. Returning `null` keeps the row honest
		 * preview-only. Absent rows (`tag_preview` missing) render no chips.
		 */
		tagLoader?: (row: ConversationView) => TagPageLoader | null;
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
		onloadmorehistory,
		onrenameconversation,
		tagLoader
	}: Props = $props();

	let historyExpanded = $state(true);
	let editingRunId = $state<string | null>(null);
	let editingTitle = $state('');
	let renameBusy = $state(false);

	const historyBadge = $derived(
		totalConversations !== undefined && totalConversations > conversations.length
			? `${conversations.length}/${totalConversations}`
			: String(conversations.length)
	);

	function shortId(id: string): string {
		return id.length > 8 ? `${id.slice(0, 8)}…` : id;
	}

	function formatConversationLabel(row: ConversationView): string {
		if (typeof row.title === 'string' && row.title.trim() !== '') {
			return row.title.trim();
		}
		const tags =
			row.service_tags && row.service_tags.length > 0
				? row.service_tags.map((t) => `@${t}`).join(' ')
				: `Run ${shortId(row.run_id)}`;
		return tags;
	}

	function startRename(row: ConversationView, event: MouseEvent): void {
		event.stopPropagation();
		editingRunId = row.run_id;
		editingTitle = formatConversationLabel(row);
	}

	function cancelRename(): void {
		if (renameBusy) return;
		editingRunId = null;
		editingTitle = '';
	}

	async function commitRename(runId: string): Promise<void> {
		const trimmed = editingTitle.trim();
		if (!trimmed || renameBusy) return;
		renameBusy = true;
		try {
			await onrenameconversation?.(runId, trimmed);
			editingRunId = null;
			editingTitle = '';
		} finally {
			renameBusy = false;
		}
	}

	function handleRenameKeydown(runId: string, event: KeyboardEvent): void {
		if (event.key === 'Enter') {
			event.preventDefault();
			void commitRename(runId);
		} else if (event.key === 'Escape') {
			event.preventDefault();
			cancelRename();
		}
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
					{#if editingRunId === row.run_id}
						<div class="mai-chat-drawer__edit-row" data-testid="drawer-history-edit-row">
							<input
								type="text"
								class="mai-chat-drawer__edit-input"
								bind:value={editingTitle}
								maxlength={120}
								disabled={renameBusy}
								aria-label="Chat title"
								data-testid="drawer-history-rename-input"
								onkeydown={(e) => handleRenameKeydown(row.run_id, e)}
							/>
							<button
								type="button"
								class="mai-chat-drawer__icon-btn"
								disabled={renameBusy || editingTitle.trim() === ''}
								aria-label="Save chat title"
								title="Save"
								data-testid="drawer-history-rename-save"
								onclick={() => void commitRename(row.run_id)}
							>
								✓
							</button>
							<button
								type="button"
								class="mai-chat-drawer__icon-btn"
								disabled={renameBusy}
								aria-label="Cancel rename"
								title="Cancel"
								data-testid="drawer-history-rename-cancel"
								onclick={cancelRename}
							>
								✕
							</button>
						</div>
					{:else}
						<div class="mai-chat-drawer__item-row">
							<div class="mai-chat-drawer__item-head">
								<NavItem
									icon="💬"
									label={formatConversationLabel(row)}
									meta={row.state ?? ''}
									active={activeRunId === row.run_id}
									data-testid="drawer-history-item"
									onclick={() => onselectconversation(row.run_id)}
								/>
								{#if onrenameconversation}
									<button
										type="button"
										class="mai-chat-drawer__rename-btn"
										aria-label="Rename chat {formatConversationLabel(row)}"
										title="Rename chat"
										data-testid="drawer-history-rename"
										onclick={(e) => startRename(row, e)}
									>
										✎
									</button>
								{/if}
							</div>
							{#if row.tag_preview !== undefined && tagLoader !== undefined && row.tag_preview.total > 0}
								<span class="mai-chat-drawer__row-tags" data-testid="drawer-history-row-tags">
									<HistoryTags
										tag_preview={row.tag_preview}
										loadMore={tagLoader(row)}
										label={'Tags of ' + formatConversationLabel(row)}
										testId="drawer-history-row-tags"
									/>
								</span>
							{/if}
						</div>
					{/if}
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
		max-height: min(22rem, 42dvh);
		overflow-x: hidden;
		overflow-y: auto;
		overscroll-behavior: contain;
		scrollbar-gutter: stable;
	}

	/* Two-line row: head (nav + rename) on top, tag preview underneath.
	 * Column layout with min-width 0 keeps long titles wrapping/ellipsizing
	 * inside the drawer — the row never forces the list to scroll sideways. */
	.mai-chat-drawer__item-row {
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: stretch;
		gap: 2px;
		min-width: 0;
		max-width: 100%;
	}

	.mai-chat-drawer__item-head {
		display: flex;
		align-items: center;
		gap: 2px;
		min-width: 0;
		max-width: 100%;
	}

	.mai-chat-drawer__item-head :global(.mai-nav-item) {
		flex: 1 1 auto;
		min-width: 0;
	}

	.mai-chat-drawer__row-tags {
		display: inline-flex;
		min-width: 0;
		max-width: 100%;
		overflow: hidden;
		/* Indented under the head row's icon so tags read as row metadata. */
		padding-left: 1.5rem;
	}

	.mai-chat-drawer__rename-btn,
	.mai-chat-drawer__icon-btn {
		flex: 0 0 auto;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 1.5rem;
		height: 1.5rem;
		padding: 0;
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
		background: transparent;
		border: var(--mai-border-width) solid transparent;
		border-radius: var(--mai-radius-sm);
		cursor: pointer;
		transition:
			color var(--mai-motion-fast) ease,
			background-color var(--mai-motion-fast) ease,
			border-color var(--mai-motion-fast) ease,
			opacity var(--mai-motion-fast) ease;
	}

	.mai-chat-drawer__rename-btn {
		opacity: 0;
	}

	.mai-chat-drawer__item-row:hover .mai-chat-drawer__rename-btn,
	.mai-chat-drawer__item-row:focus-within .mai-chat-drawer__rename-btn {
		opacity: 1;
	}

	.mai-chat-drawer__rename-btn:hover:not(:disabled),
	.mai-chat-drawer__icon-btn:hover:not(:disabled) {
		color: var(--mai-color-text);
		background: var(--mai-color-surface-raised);
		border-color: var(--mai-color-border);
	}

	.mai-chat-drawer__edit-row {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 2px var(--mai-space-1);
		background: var(--mai-color-surface-raised);
		border: var(--mai-border-width) solid var(--mai-color-brass);
		border-radius: var(--mai-radius-sm);
	}

	.mai-chat-drawer__edit-input {
		flex: 1 1 auto;
		min-width: 0;
		padding: 2px var(--mai-space-1);
		font-family: var(--mai-font-sans);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text);
		background: var(--mai-color-bg);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
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
