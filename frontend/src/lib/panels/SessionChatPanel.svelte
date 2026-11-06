<script lang="ts">
	/**
	 * SessionChatPanel — the session-first virtualized entry view (D08).
	 *
	 * Owns ONE visible session's bounded history surface:
	 *  - the entry cache lives with the composed shell (≤400 committed
	 *    entries — the shell merges channel deliveries + REST older pages
	 *    and evicts); this component renders a WINDOWED slice of that cache
	 *    (≤80 DOM rows + measured spacers) so the DOM never mirrors the
	 *    whole cache;
	 *  - older pages load on demand (scroll-top or the keyboard-accessible
	 *    "Load older" trigger) through the shell's single-flight
	 *    `before_seq` fetcher — this component never issues duplicate
	 *    concurrent page loads;
	 *  - volatile streaming drafts render at the tail and are reconciled
	 *    away by the committed truth (the shell/controller own that merge —
	 *    duplicate/out-of-order deltas can never fabricate a second
	 *    assistant response here);
	 *  - every entry kind renders from its typed payload — nothing is
	 *    inferred as success (`unknown_effect` carries an explicit warning
	 *    badge), delegations render as session/run-edge cards, compaction
	 *    as an explicit marker banner;
	 *  - the composer admits turns through the typed session API; control
	 *    actions (steer/pause/cancel) are typed requests, never engine
	 *    writes from the client.
	 *
	 * Svelte 5 runes only (AGENTS.md §3); all state is instance-local. No
	 * timers, no polling — the channel pushes, the user scrolls.
	 */

	import type { PanelContext } from '../workspace/registry.js';
	import type { SessionChatApi } from '../workspace/entries/06-session-chat.js';
	import type { ChannelEntry } from '../chat/session_client.js';
	import type { SessionStreamDraft } from '../chat/session_client.js';
	import type { SessionChannelConnectionState } from '../chat/session_client.js';
	import type { components } from '../api/schema.generated.js';
	import Button from '../ui/Button.svelte';

	type EntryPayload = components['schemas']['SessionEntryView']['payload'];
	type ToolCallItem = components['schemas']['ToolCallItem'];

	type Props = {
		panel: PanelContext;
		/** The visible session (null/absent ⇒ create-on-submit composer). */
		sessionId?: string | null;
		serverId?: string | null;
		/** The shell's bounded merged cache (≤400 committed entries). */
		entries?: readonly ChannelEntry[];
		/** Live volatile streaming drafts keyed by stream identity. */
		drafts?: readonly SessionStreamDraft[];
		channelState?: SessionChannelConnectionState;
		/** True while the shell's single-flight older-page load is running. */
		loadingOlder?: boolean;
		/** True when the server still holds entries older than the cache. */
		hasOlder?: boolean;
		/** Run the shell's single-flight `before_seq` older-page fetch. */
		onloadolder?: () => void;
		/** A composer-created session: the shell attaches its channel. */
		onsessionopened?: (sessionId: string, serverId: string | null) => void;
	};

	let {
		panel,
		sessionId = null,
		serverId = null,
		entries = [],
		drafts = [],
		channelState = 'closed',
		loadingOlder = false,
		hasOlder = false,
		onloadolder = () => {},
		onsessionopened = () => {}
	}: Props = $props();

	/** Narrow the session API slice (fail closed like every panel). */
	const api: SessionChatApi | null = $derived.by(() => {
		const candidate = panel.services.api as Partial<SessionChatApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		if (typeof candidate.submitAgentSessionTurn !== 'function') return null;
		if (typeof candidate.submitAgentSessionControl !== 'function') return null;
		return candidate as SessionChatApi;
	});
	const usable = $derived(api !== null);

	// --- composer + controls -------------------------------------------------
	let composerText = $state('');
	let submitting = $state(false);
	let submitProblem = $state<{ code: string; message: string } | null>(null);
	let controlBusy = $state<string | null>(null);
	let controlProblem = $state<{ code: string; message: string } | null>(null);

	async function submitTurn(): Promise<void> {
		const text = composerText.trim();
		if (!api || submitting || text === '') return;
		submitting = true;
		submitProblem = null;
		const clientRequestId = crypto.randomUUID();
		try {
			if (sessionId === null) {
				// Create-on-submit: the explicit first turn mints the session
				// and the shell attaches its channel.
				const scope = serverId ?? '00000000-0000-0000-0000-000000000000';
				const created = await api.createAgentSession({
					server_id: scope,
					source: 'interactive',
					client_request_id: clientRequestId
				});
				await api.submitAgentSessionTurn(
					created.session_id,
					{
						session_id: created.session_id,
						text,
						client_request_id: crypto.randomUUID()
					},
					scope
				);
				onsessionopened(created.session_id, scope);
			} else {
				await api.submitAgentSessionTurn(
					sessionId,
					{
						session_id: sessionId,
						text,
						client_request_id: clientRequestId
					},
					serverId
				);
			}
			composerText = '';
		} catch (error) {
			const api2 = error as { code?: string; message?: string };
			submitProblem = {
				code: api2.code ?? 'unavailable',
				message: api2.message ?? 'The turn could not be admitted.'
			};
		} finally {
			submitting = false;
		}
	}

	async function runControl(action: 'steer' | 'pause' | 'cancel'): Promise<void> {
		if (!api || sessionId === null || controlBusy !== null) return;
		if (action === 'steer' && composerText.trim() === '') return;
		controlBusy = action;
		controlProblem = null;
		try {
			await api.submitAgentSessionControl(
				sessionId,
				{
					session_id: sessionId,
					action,
					client_request_id: crypto.randomUUID(),
					note: action === 'steer' ? composerText.trim() : null
				},
				serverId
			);
			if (action === 'steer') composerText = '';
		} catch (error) {
			const api2 = error as { code?: string; message?: string };
			controlProblem = {
				code: api2.code ?? 'unavailable',
				message: api2.message ?? 'The control request was refused.'
			};
		} finally {
			controlBusy = null;
		}
	}

	// --- derived view model ---------------------------------------------------
	/** The latest run-state entry's lifecycle value (server truth). */
	const runState = $derived.by(() => {
		for (let i = entries.length - 1; i >= 0; i -= 1) {
			const payload = entries[i].payload;
			if (payload.kind === 'run_state') return String(payload.state);
		}
		return null;
	});

	const oldestSeq = $derived(entries.length > 0 ? Number(entries[0].seq) : 0);
	const latestSeq = $derived(
		entries.length > 0 ? Number(entries[entries.length - 1].seq) : 0
	);

	// --- virtualization (windowed DOM over the bounded cache) -----------------
	const ENTRY_HEIGHT_ESTIMATE = 72;
	const OVERSCAN_PX = 480;
	const MAX_DOM_ROWS = 80;

	let viewportHeight = $state(0);
	let scrollTop = $state(0);
	/** Measured row heights by seq (unmeasured rows use the estimate). */
	let heights = new Map<number, number>();
	/** Bumped whenever a measurement lands so offsets recompute. */
	let measureVersion = $state(0);

	// R08 F4: prune measured heights evicted from the bounded cache — the
	// map must not grow with browsed history (tens of KB per 1k entries).
	// Plain-map mutation is safe here: offsets recompute through
	// `measureVersion`, never through the map itself.
	$effect(() => {
		const live = new Set(entries.map((entry) => Number(entry.seq)));
		for (const seq of [...heights.keys()]) {
			if (!live.has(seq)) heights.delete(seq);
		}
	});

	function measure(node: HTMLElement, entry: ChannelEntry) {
		const height = node.offsetHeight;
		if (height > 0 && heights.get(Number(entry.seq)) !== height) {
			heights.set(Number(entry.seq), height);
			measureVersion += 1;
		}
	}

	function heightOf(entry: ChannelEntry): number {
		return heights.get(Number(entry.seq)) ?? ENTRY_HEIGHT_ESTIMATE;
	}

	function offsetOf(index: number): number {
		let total = 0;
		for (let i = 0; i < index && i < entries.length; i += 1) {
			total += heightOf(entries[i]);
		}
		return total;
	}

	const totalHeight = $derived.by(() => {
		void measureVersion;
		return offsetOf(entries.length);
	});

	/** The rendered window: first index whose offset enters the viewport. */
	const windowStart = $derived.by(() => {
		void measureVersion;
		let cumulative = 0;
		let start = 0;
		const limit = Math.max(scrollTop - OVERSCAN_PX, 0);
		for (let i = 0; i < entries.length; i += 1) {
			if (cumulative >= limit) {
				start = i;
				break;
			}
			cumulative += heightOf(entries[i]);
			start = i + 1;
		}
		return Math.min(start, entries.length);
	});

	const windowEnd = $derived.by(() => {
		void measureVersion;
		const startOffset = offsetOf(windowStart);
		let cumulative = startOffset;
		let count = 0;
		for (let i = windowStart; i < entries.length; i += 1) {
			if (cumulative >= scrollTop + viewportHeight + OVERSCAN_PX && count > 0) {
				break;
			}
			cumulative += heightOf(entries[i]);
			count += 1;
			if (count >= MAX_DOM_ROWS) break;
		}
		return windowStart + count;
	});

	const renderedSlice = $derived(entries.slice(windowStart, windowEnd));
	const topSpacerHeight = $derived.by(() => {
		void measureVersion;
		return offsetOf(windowStart);
	});
	const bottomSpacerHeight = $derived.by(() => {
		void measureVersion;
		return Math.max(totalHeight - offsetOf(windowEnd), 0);
	});

	// --- scroll anchoring -----------------------------------------------------
	let viewportEl: HTMLElement | null = $state(null);
	let prevFirstSeq = $state<number | null>(null);
	let prevScrollHeight = $state(0);
	let anchoredToBottom = $state(true);

	function onScroll(): void {
		if (viewportEl === null) return;
		scrollTop = viewportEl.scrollTop;
		anchoredToBottom =
			scrollTop + viewportEl.clientHeight >= viewportEl.scrollHeight - 4;
		// Demand-load older pages near the top (the shell deduplicates).
		if (scrollTop <= 64 && hasOlder && !loadingOlder && entries.length > 0) {
			onloadolder();
		}
	}

	/**
	 * Scroll anchoring (post-DOM): prepending older entries adjusts
	 * `scrollTop` by the prepended height delta; following the tail keeps
	 * the newest row visible. Runs after every entries change.
	 */
	$effect(() => {
		const firstSeq = entries.length > 0 ? Number(entries[0].seq) : null;
		if (viewportEl === null || entries.length === 0 || firstSeq === null) {
			prevFirstSeq = firstSeq;
			return;
		}
		const before = prevFirstSeq;
		const previousHeight = prevScrollHeight;
		if (
			before !== null &&
			firstSeq < before &&
			prevScrollHeight > 0
		) {
			// Older entries PREPENDED: keep the reader's visual position.
			viewportEl.scrollTop += viewportEl.scrollHeight - previousHeight;
		} else if (before === null || firstSeq > before) {
			// Newest entries appended: follow the tail when already at it.
			if (anchoredToBottom) viewportEl.scrollTop = viewportEl.scrollHeight;
		}
		prevFirstSeq = firstSeq;
		prevScrollHeight = viewportEl.scrollHeight;
	});

	/** Initial focus + bottom anchor once the first page rendered. */
	$effect(() => {
		if (entries.length > 0 && prevScrollHeight === 0 && viewportEl !== null) {
			viewportEl.scrollTop = viewportEl.scrollHeight;
			prevScrollHeight = viewportEl.scrollHeight;
			prevFirstSeq = Number(entries[0].seq);
		}
	});

	function onComposerKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter' && !event.shiftKey) {
			event.preventDefault();
			void submitTurn();
		}
	}
</script>

<div class="mai-session-chat" data-testid="session-chat-panel">
	{#if !usable}
		<div class="mai-session-chat__state" role="alert" data-testid="session-config-error">
			<p>This panel was opened without its session services.</p>
		</div>
	{:else}
		<header class="mai-session-chat__header">
			<span class="mai-session-chat__id" data-testid="session-header-id">
				{sessionId === null ? 'new session' : `session ${sessionId.slice(0, 8)}…`}
			</span>
			<span
				class="mai-session-chat__channel"
				data-testid="session-channel-badge"
				data-channel-state={channelState}
			>
				{channelState}
			</span>
			{#if runState !== null}
				<span class="mai-session-chat__run" data-testid="session-run-state" data-run-state={runState}>
					run: {runState}
				</span>
			{/if}
			{#if channelState === 'resync'}
				<span class="mai-session-chat__resync" role="status" data-testid="session-resync-note">
					resync — reconciling with the server
				</span>
			{/if}
		</header>

		<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
		<!-- A focusable scroll region is DELIBERATE (D08 acceptance 6:
		     keyboard users scroll the log with the keyboard). -->
		<div
			class="mai-session-chat__viewport"
			data-testid="session-history-viewport"
			data-cached-count={entries.length}
			data-dom-count={renderedSlice.length}
			data-oldest-seq={oldestSeq}
			data-latest-seq={latestSeq}
			data-has-older={hasOlder}
			bind:this={viewportEl}
			bind:clientHeight={viewportHeight}
			onscroll={onScroll}
			tabindex="0"
			role="log"
			aria-label="Session entry history"
		>
			{#if hasOlder || loadingOlder}
				<div class="mai-session-chat__older">
					<Button
						variant="secondary"
						size="sm"
						tabindex={0}
						loading={loadingOlder}
						disabled={!hasOlder}
						data-testid="session-load-older"
						onclick={() => onloadolder()}
					>
						Load older
					</Button>
				</div>
			{/if}
			<div style="height:{topSpacerHeight}px" aria-hidden="true"></div>
			{#if entries.length === 0 && channelState !== 'connecting' && channelState !== 'reconnecting'}
				<p class="mai-session-chat__state" data-testid="session-history-empty">
					No committed entries yet — say something first.
				</p>
			{/if}
			{#each renderedSlice as entry (entry.entry_id)}
				<article
					class="mai-session-entry mai-session-entry--{entry.payload.kind}"
					data-testid="session-entry"
					data-entry-kind={entry.payload.kind}
					data-seq={entry.seq}
					use:measure={entry}
				>
					<span class="mai-session-entry__seq">#{entry.seq}</span>
					{#if entry.payload.kind === 'user_message'}
						<div class="mai-session-entry__body" data-testid="entry-user-message">
							<span class="mai-session-entry__badge mai-session-entry__badge--user">user</span>
							<p class="mai-session-entry__text">{entry.payload.text}</p>
						</div>
					{:else if entry.payload.kind === 'assistant_message'}
						<div class="mai-session-entry__body" data-testid="entry-assistant-message">
							<span class="mai-session-entry__badge mai-session-entry__badge--assistant">assistant</span>
							<span class="mai-session-entry__meta">{entry.payload.model_profile}</span>
							<p class="mai-session-entry__text">{entry.payload.text}</p>
							{#if entry.payload.provider_artifacts !== undefined && entry.payload.provider_artifacts.length > 0}
								<details class="mai-session-entry__reasoning" data-testid="entry-reasoning">
									<summary>reasoning artifacts ({entry.payload.provider_artifacts.length})</summary>
									{#each entry.payload.provider_artifacts as artifact}
										<p class="mai-session-entry__meta">
											{artifact.artifact_kind}
											{#if typeof artifact.content === 'string'}
												— {artifact.content.slice(0, 200)}
											{/if}
										</p>
									{/each}
								</details>
							{/if}
							{#if entry.payload.tool_calls !== undefined && entry.payload.tool_calls.length > 0}
								<ul class="mai-session-entry__tools" data-testid="entry-tool-calls">
									{#each entry.payload.tool_calls as call (call.call_id)}
										<li data-testid="entry-tool-call" data-tool-status="pending">
											<span class="mai-session-entry__badge mai-session-entry__badge--pending">pending</span>
											<code>{call.capability_id}</code>
											<span class="mai-session-entry__meta">{call.call_id}</span>
										</li>
									{/each}
								</ul>
							{/if}
						</div>
					{:else if entry.payload.kind === 'tool_result'}
						<div
							class="mai-session-entry__body"
							data-testid="entry-tool-result"
							data-tool-status={entry.payload.status}
							data-domain-outcome={entry.payload.domain_outcome}
						>
							<span
								class="mai-session-entry__badge mai-session-entry__badge--{entry.payload.status}"
								data-testid="tool-status-badge"
							>
								{#if entry.payload.status === 'unknown_effect'}
									Outcome unknown — effect not proven
								{:else}
									{entry.payload.status}
								{/if}
							</span>
							<code>{entry.payload.capability_id}</code>
							<span class="mai-session-entry__meta">{entry.payload.call_id}</span>
							{#if entry.payload.error_code !== null && entry.payload.error_code !== undefined}
								<p class="mai-session-entry__error" data-testid="tool-error">
									{entry.payload.error_code}: {entry.payload.error_detail ?? ''}
								</p>
							{/if}
						</div>
					{:else if entry.payload.kind === 'delegation'}
						<div
							class="mai-session-entry__body mai-session-entry__delegation"
							data-testid="entry-delegation"
							data-delegation-status={entry.payload.status}
						>
							<span class="mai-session-entry__badge mai-session-entry__badge--delegation">
								delegation · {entry.payload.status}
							</span>
							<dl>
								<dt>child session</dt>
								<dd>{entry.payload.child_session_id ?? '—'}</dd>
								<dt>child run</dt>
								<dd>{entry.payload.child_run_id}</dd>
							</dl>
							{#if entry.payload.goal !== ''}
								<p class="mai-session-entry__text">{entry.payload.goal}</p>
							{/if}
							{#if entry.payload.allowed_capabilities.length > 0}
								<p class="mai-session-entry__meta">
									capabilities: {entry.payload.allowed_capabilities.join(', ')}
								</p>
							{/if}
							{#if entry.payload.result_summary !== null && entry.payload.result_summary !== undefined}
								<p class="mai-session-entry__meta" data-testid="delegation-summary">
									{entry.payload.result_summary.slice(0, 240)}
								</p>
							{/if}
							{#if entry.payload.error_code !== null && entry.payload.error_code !== undefined}
								<p class="mai-session-entry__error">{entry.payload.error_code}</p>
							{/if}
						</div>
					{:else if entry.payload.kind === 'compaction'}
						<div class="mai-session-entry__compaction" data-testid="compaction-marker">
							<span>
								Compacted entries seq {entry.payload.from_seq}..{entry.payload.to_seq}
							</span>
							<details data-testid="compaction-summary">
								<summary>summary</summary>
								<p class="mai-session-entry__text">{entry.payload.summary_text}</p>
							</details>
						</div>
					{:else if entry.payload.kind === 'run_state'}
						<div class="mai-session-entry__runstate" data-testid="entry-run-state" data-run-state={entry.payload.state}>
							<span class="mai-session-entry__badge">{entry.payload.state}</span>
							{#if entry.payload.reason_code !== null && entry.payload.reason_code !== undefined}
								<span class="mai-session-entry__meta">{entry.payload.reason_code}</span>
							{/if}
						</div>
					{:else if entry.payload.kind === 'control'}
						<div class="mai-session-entry__control" data-testid="entry-control" data-control-action={entry.payload.action}>
							<span class="mai-session-entry__badge">{entry.payload.action}</span>
							{#if entry.payload.note !== null && entry.payload.note !== undefined}
								<span class="mai-session-entry__meta">{entry.payload.note}</span>
							{/if}
						</div>
					{/if}
				</article>
			{/each}
			<div style="height:{bottomSpacerHeight}px" aria-hidden="true"></div>
		</div>

		{#if drafts.length > 0}
			<div class="mai-session-chat__drafts" data-testid="session-stream-drafts">
				{#each drafts as draft (draft.stream_id)}
					<p class="mai-session-chat__draft" data-testid="session-stream-draft" data-stream-id={draft.stream_id}>
						streaming… {draft.text.slice(-240)}
					</p>
				{/each}
			</div>
		{/if}

		<footer class="mai-session-chat__composer">
			{#if submitProblem !== null}
				<p class="mai-session-chat__problem" role="alert" data-testid="session-submit-error">
					{submitProblem.code}: {submitProblem.message}
				</p>
			{/if}
			{#if controlProblem !== null}
				<p class="mai-session-chat__problem" role="alert" data-testid="session-control-error">
					{controlProblem.code}: {controlProblem.message}
				</p>
			{/if}
			<div class="mai-session-chat__composer-row">
				<textarea
					class="mai-session-chat__input"
					data-testid="session-composer-input"
					placeholder={sessionId === null
						? 'Describe the task — a session is created on submit'
						: 'Admit a turn to the visible session'}
					bind:value={composerText}
					onkeydown={onComposerKeydown}
					rows="2"
					disabled={submitting}
				></textarea>
				<Button
					variant="primary"
					size="sm"
					loading={submitting}
					disabled={composerText.trim() === ''}
					data-testid="session-composer-submit"
					onclick={() => void submitTurn()}
				>
					Send
				</Button>
			</div>
			<div class="mai-session-chat__controls">
				<Button
					variant="secondary"
					size="sm"
					tabindex={0}
					disabled={sessionId === null || composerText.trim() === '' || controlBusy !== null}
					loading={controlBusy === 'steer'}
					data-testid="session-control-steer"
					onclick={() => void runControl('steer')}
				>
					Steer
				</Button>
				<Button
					variant="secondary"
					size="sm"
					tabindex={0}
					disabled={sessionId === null || controlBusy !== null}
					loading={controlBusy === 'pause'}
					data-testid="session-control-pause"
					onclick={() => void runControl('pause')}
				>
					Pause
				</Button>
				<Button
					variant="secondary"
					size="sm"
					tabindex={0}
					disabled={sessionId === null || controlBusy !== null}
					loading={controlBusy === 'cancel'}
					data-testid="session-control-cancel"
					onclick={() => void runControl('cancel')}
				>
					Cancel
				</Button>
			</div>
		</footer>
	{/if}
</div>

<style>
	.mai-session-chat {
		display: flex;
		flex-direction: column;
		/* Same docked envelope as ChatPanel's `.mai-chat__body` bound: the
		   dock host is position:absolute with auto height, so `height:100%`
		   would grow to intrinsic content height and the history viewport
		   would never overflow (no scroll events, no demand-load). */
		height: min(32rem, calc(100dvh - 10rem));
		min-height: 0;
		overflow: hidden;
	}
	.mai-session-chat__header {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		align-items: center;
		padding: 4px 8px;
		font-size: 0.75rem;
		border-bottom: var(--mai-border-width, 1px) solid
			var(--mai-color-border, transparent);
	}
	.mai-session-chat__channel {
		font-weight: 600;
	}
	.mai-session-chat__resync {
		color: var(--mai-color-warning, #b45309);
	}
	.mai-session-chat__viewport {
		flex: 1 1 auto;
		min-height: 0;
		overflow-y: auto;
		overflow-x: hidden;
		outline: none;
	}
	.mai-session-chat__older {
		display: flex;
		justify-content: center;
		padding: 4px 0;
	}
	.mai-session-entry {
		display: flex;
		gap: 6px;
		padding: 4px 8px;
		border-bottom: var(--mai-border-width, 1px) solid
			var(--mai-color-border, transparent);
	}
	.mai-session-entry__seq {
		flex: none;
		min-width: 3.2em;
		font-size: 0.72rem;
		color: var(--mai-color-muted, inherit);
	}
	.mai-session-entry__body {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}
	.mai-session-entry__text {
		margin: 0;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.mai-session-entry__meta {
		font-size: 0.72rem;
		color: var(--mai-color-muted, inherit);
		overflow-wrap: anywhere;
	}
	.mai-session-entry__badge {
		display: inline-block;
		padding: 0 6px;
		border-radius: 8px;
		font-size: 0.7rem;
		background: var(--mai-color-surface-raised, rgba(0, 0, 0, 0.06));
	}
	.mai-session-entry__badge--user {
		background: var(--mai-color-brass, rgba(180, 83, 9, 0.18));
	}
	.mai-session-entry__badge--unknown_effect {
		background: var(--mai-color-warning, #b45309);
		color: #fff;
	}
	.mai-session-entry__badge--failed {
		background: var(--mai-color-danger, #b3261e);
		color: #fff;
	}
	.mai-session-entry__badge--denied {
		background: var(--mai-color-muted, #666);
		color: #fff;
	}
	.mai-session-entry__error {
		margin: 0;
		color: var(--mai-color-danger, #b3261e);
		font-size: 0.75rem;
	}
	.mai-session-entry__tools {
		margin: 2px 0 0;
		padding: 0;
		list-style: none;
	}
	.mai-session-entry__delegation dl {
		display: grid;
		grid-template-columns: auto auto;
		gap: 0 10px;
		margin: 2px 0;
		font-size: 0.72rem;
	}
	.mai-session-entry__delegation dt {
		color: var(--mai-color-muted, inherit);
	}
	.mai-session-entry__delegation dd {
		margin: 0;
		overflow-wrap: anywhere;
	}
	.mai-session-entry__compaction {
		padding: 4px 8px;
		background: var(--mai-color-surface-raised, rgba(0, 0, 0, 0.05));
		font-size: 0.75rem;
	}
	.mai-session-chat__drafts {
		padding: 2px 8px;
		border-top: 1px dashed var(--mai-color-border, transparent);
	}
	.mai-session-chat__draft {
		margin: 0;
		font-size: 0.78rem;
		color: var(--mai-color-muted, inherit);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.mai-session-chat__composer {
		border-top: var(--mai-border-width, 1px) solid
			var(--mai-color-border, transparent);
		padding: 6px 8px;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.mai-session-chat__composer-row {
		display: flex;
		gap: 6px;
		align-items: flex-end;
	}
	.mai-session-chat__input {
		flex: 1 1 auto;
		min-width: 0;
		resize: vertical;
		font: inherit;
	}
	.mai-session-chat__controls {
		display: flex;
		gap: 6px;
	}
	.mai-session-chat__problem {
		margin: 0;
		color: var(--mai-color-danger, #b3261e);
		font-size: 0.75rem;
	}
	.mai-session-chat__state {
		padding: 8px;
		color: var(--mai-color-muted, inherit);
	}
</style>
