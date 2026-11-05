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
	import type { components } from '$lib/api/schema.generated.js';

	type EntityTagPreviewView = components['schemas']['EntityTagPreviewView'];

	/**
	 * One history row, identity-dual-wired (D07): session-first rows carry
	 * `session_id` (the only conversation/history root per sfc_INTENT §1);
	 * legacy rows carry `conversation_id` (canonical) or fall back to the
	 * bare `run_id` — each identity routes to ITS OWN tag/rename route and
	 * the sets are never conflated (COORD-D02-01; deleted at D09). The
	 * legacy `ConversationView` stays structurally assignable (its required
	 * `tag_preview` satisfies the optional one here).
	 */
	export interface ChatHistoryRow {
		/** Stable row identity: the legacy run id (session rows mirror it). */
		run_id: string;
		/** Session-first identity (sfc session API); absent on legacy rows. */
		session_id?: string | null;
		/** Canonical conversation identity; absent on legacy run rows. */
		conversation_id?: string | null;
		/** Explicit server scope of the row (C01 rule 2). */
		server_id: string;
		/** User-assigned title, when set. */
		title?: string | null;
		/** Service tags for the `@tag` label fallback. */
		service_tags?: readonly string[];
		/** Legacy run state (legacy rows only). */
		state?: string | null;
		/** Coarse session status (session-first rows only). */
		status?: string | null;
		/** Last-change timestamp (session-first rows only). */
		updated_at?: string | null;
		/** Bounded tag preview (present on legacy rows; honest absence else). */
		tag_preview?: EntityTagPreviewView | null;
	}

	/** The row's stable key: the session id when session-first, else run id. */
	function rowKey(row: ChatHistoryRow): string {
		return typeof row.session_id === 'string' && row.session_id !== '' ? row.session_id : row.run_id;
	}

	type Props = {
		/** Recent history rows across servers (session-first + dual-wired). */
		conversations?: readonly ChatHistoryRow[];
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
		 * decides the correct route by the ROW's identity (session_id rows
		 * read the agent-session tag routes, canonical conversation rows the
		 * conversation tag routes, legacy run rows the job tag routes) with
		 * the row's recorded server scope. Returning `null` keeps the row
		 * honest preview-only. Absent rows (`tag_preview` missing) render no
		 * chips.
		 */
		tagLoader?: (row: ChatHistoryRow) => TagPageLoader | null;
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

	function formatConversationLabel(row: ChatHistoryRow): string {
		if (typeof row.title === 'string' && row.title.trim() !== '') {
			return row.title.trim();
		}
		const tags =
			row.service_tags && row.service_tags.length > 0
				? row.service_tags.map((t) => `@${t}`).join(' ')
				: typeof row.session_id === 'string' && row.session_id !== ''
					? `Session ${shortId(row.session_id)}`
					: `Run ${shortId(row.run_id)}`;
		return tags;
	}

	function startRename(row: ChatHistoryRow, event: MouseEvent): void {
		event.stopPropagation();
		editingRunId = rowKey(row);
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
				{#each conversations as row (rowKey(row))}
					{#if editingRunId === rowKey(row)}
						<div class="mai-chat-drawer__edit-row" data-testid="drawer-history-edit-row">
							<input
								type="text"
								class="mai-chat-drawer__edit-input"
								bind:value={editingTitle}
								maxlength={120}
								disabled={renameBusy}
								aria-label="Chat title"
								data-testid="drawer-history-rename-input"
								onkeydown={(e) => handleRenameKeydown(rowKey(row), e)}
							/>
							<button
								type="button"
								class="mai-chat-drawer__icon-btn"
								disabled={renameBusy || editingTitle.trim() === ''}
								aria-label="Save chat title"
								title="Save"
								data-testid="drawer-history-rename-save"
								onclick={() => void commitRename(rowKey(row))}
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
									meta={row.state ?? row.status ?? ''}
									active={activeRunId === rowKey(row)}
									data-testid="drawer-history-item"
									onclick={() => onselectconversation(rowKey(row))}
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
							{#if row.tag_preview !== undefined && row.tag_preview !== null && tagLoader !== undefined && row.tag_preview.total > 0}
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
