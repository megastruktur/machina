<script lang="ts">
	/**
	 * SessionChatPanel — the session-first virtualized entry view (D08,
	 * restyled S3).
	 *
	 * Owns ONE visible session's bounded history surface:
	 *  - the entry cache lives with the composed shell (≤400 committed
 *    entries — the shell merges channel deliveries + REST older pages
 *    and evicts); this component virtualizes the FILTERED cache with
 *    `virtua/svelte`'s `Virtualizer` (measured, absolutely positioned
 *    rows, windowed DOM) so the DOM never mirrors the whole cache;
	 *  - the transcript is a CHAT, not an audit log: `run_state` entries
	 *    and delegated child-run entries are filtered out of the rendered
	 *    stream (S3) — run state drives the live generating indicator and
	 *    the terminal notice instead of list rows;
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
	 *    badge); message/thinking/delegation/compaction prose and the
	 *    stream drafts render through the XSS-safe `MarkdownRenderer`
	 *    (raw HTML escapes, structure formats); thinking renders as a
	 *    collapsed "Ход мыслей" disclosure and tools/delegations as
	 *    compact disclosures that stay open while the outcome is unproven
	 *    (or the fixture has no run scope);
	 *  - the composer admits turns through the typed session API; a
	 *    resolved submit calls `onturnsubmitted` with the optimistic
	 *    user entry + the response run state (S3) so the message and the
	 *    generating indicator appear immediately on Enter.
	 *
	 * Svelte 5 runes only (AGENTS.md §3); all state is instance-local. No
	 * timers, no polling — the channel pushes, the user scrolls.
	 */

	import type { PanelContext } from '../workspace/registry.js';
	import type { SessionChatApi } from '../workspace/entries/06-session-chat.js';
	import type {
		ChannelEntry,
		SessionStreamDraft,
		SessionChannelConnectionState
	} from '../chat/session_client.js';
	import type {
		SessionRunState,
		SessionTurnSubmitOutcome
	} from '../api/client.js';
	import type { components } from '../api/schema.generated.js';
	import Button from '../ui/Button.svelte';
	import MarkdownRenderer from '../ui/MarkdownRenderer.svelte';
	import { Virtualizer, type VirtualizerHandle } from 'virtua/svelte';
	import {
		buildTurnChildSegments,
		buildTurnUnits,
		type TurnUnit
	} from './turnGroups.js';
	import { tick, untrack } from 'svelte';

	type EntryPayload = components['schemas']['SessionEntryView']['payload'];

	/** The optimistic run-state mirror shape handed up from a submit. */
	type TurnRunState = { run_id: string; state: SessionRunState };

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
		/**
		 * S3 optimistic feedback: called right after `submitAgentSessionTurn`
		 * resolves with the admitted turn's `user_message` entry and the
		 * response's run state, so the shell can merge the entry into the
		 * controller cache and light the generating indicator immediately.
		 */
		onturnsubmitted?: (entry: ChannelEntry, runState: TurnRunState) => void;
		/**
		 * The just-submitted turn's run state until the first committed
		 * `run_state` entry arrives (server truth always wins).
		 */
		optimisticRunState?: TurnRunState | null;
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
		onsessionopened = () => {},
		onturnsubmitted = () => {},
		optimisticRunState = null
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

	/** Build the optimistic committed-view `user_message` entry (S3). */
	function optimisticUserEntry(
		outcome: SessionTurnSubmitOutcome,
		clientRequestId: string,
		text: string,
		sid: string
	): ChannelEntry {
		const payload: EntryPayload = {
			kind: 'user_message',
			text,
			client_request_id: clientRequestId,
			source: 'interactive'
		};
		return {
			schema_version: 1,
			entry_id: outcome.entry_id,
			session_id: sid,
			seq: Number(outcome.seq),
			run_id: outcome.run_id,
			durable_source_id: `turn:${clientRequestId}`,
			created_at: new Date().toISOString(),
			payload
		};
	}

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
				const outcome = await api.submitAgentSessionTurn(
					created.session_id,
					{
						session_id: created.session_id,
						text,
						client_request_id: crypto.randomUUID()
					},
					scope
				);
				composerText = '';
				// The shell attaches the channel SYNCHRONOUSLY inside this
				// callback, so the optimistic merge below lands in the NEW
				// session's controller cache.
				onsessionopened(created.session_id, scope);
				onturnsubmitted?.(
					optimisticUserEntry(
						outcome,
						clientRequestId,
						text,
						created.session_id
					),
					{ run_id: String(outcome.run_id), state: outcome.run_state }
				);
			} else {
				const outcome = await api.submitAgentSessionTurn(
					sessionId,
					{
						session_id: sessionId,
						text,
						client_request_id: clientRequestId
					},
					serverId
				);
				composerText = '';
				onturnsubmitted?.(
					optimisticUserEntry(outcome, clientRequestId, text, sessionId),
					{ run_id: String(outcome.run_id), state: outcome.run_state }
				);
			}
		} catch (error) {
			const apiError = error as { code?: string; message?: string };
			submitProblem = {
				code: apiError.code ?? 'unavailable',
				message: apiError.message ?? 'The turn could not be admitted.'
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
			const apiError = error as { code?: string; message?: string };
			controlProblem = {
				code: apiError.code ?? 'unavailable',
				message: apiError.message ?? 'The control request was refused.'
			};
		} finally {
			controlBusy = null;
		}
	}

	// --- chat view model (S3) --------------------------------------------------
	/**
	 * The delegated child-run ids declared by `delegation` entries: the
	 * child's own entries (its prompts/answers) are the delegation's
	 * content, never top-level transcript rows — otherwise a delegated
	 * subagent's user_message looks like the operator's own message and
	 * its answer appears side by side with the parent's summary.
	 */
	const childRunIds = $derived.by(() => {
		const ids = new Set<string>();
		for (const entry of entries) {
			const payload = entry.payload;
			// (a) The delegation card names the child run.
			if (payload.kind === 'delegation') {
				const childRun = String(payload.child_run_id ?? '');
				if (childRun !== '') ids.add(childRun);
			}
			// (b) The parent's settled `session_delegate` tool_result echoes
			// the child run id inside the bounded summary document — this
			// works even when `output_preview` was truncated at the preview
			// budget (`child_run_id` is the document's FIRST key) and even
			// when the delegation CARD sits on an older history page
			// (`before_seq` byte-budget truncation).
			if (
				payload.kind === 'tool_result' &&
				(payload.capability_id === 'session_delegate' ||
					payload.tool_name === 'session_delegate')
			) {
				const echoed = /"child_run_id"\s*:\s*"([^"]+)"/.exec(
					String(payload.output_preview ?? '')
				);
				if (echoed !== null) ids.add(echoed[1]);
			}
			// (c) The parent's `waiting_child` run_state names the child.
			if (
				payload.kind === 'run_state' &&
				typeof payload.reason_code === 'string' &&
				payload.reason_code.startsWith('child:')
			) {
				const named = payload.reason_code.slice('child:'.length).trim();
				if (named !== '') ids.add(named);
			}
			// (d) The child's OWN entries namespace their durable source
			// ids (`child:turn:…`, `child:round:…`) — the envelope run id
			// is the delegated child run.
			const source = entry.durable_source_id;
			if (
				typeof source === 'string' &&
				source.startsWith('child:') &&
				entry.run_id != null &&
				String(entry.run_id) !== ''
			) {
				ids.add(String(entry.run_id));
			}
		}
		return ids;
	});

	/**
	 * The rendered transcript: run_state entries drive the indicator and
	 * the notice (never list rows), delegated child-run entries fold
	 * into their delegation card, and an EXECUTED `session_delegate`
	 * tool_result folds away while a delegation card exists (M2) — the
	 * card already renders the child's goal + result_summary, so the
	 * tool result would only duplicate them as raw JSON. Without any
	 * delegation card the tool result stays: it is then the only record
	 * of the child's outcome.
	 */
	const visibleEntries = $derived.by(() => {
		// The executed delegate tool_result folds away only while the
		// delegation CARD is in the window: without the card the tool
		// result is the only record of the child's outcome.
		const hasDelegationCard = entries.some(
			(entry) => entry.payload.kind === 'delegation'
		);
		const rows = entries.filter((entry) => {
			if (entry.payload.kind === 'run_state') return false;
			// Child-run entries namespace their durable source ids — they
			// fold into the delegation card even when the card itself sits
			// on an older history page.
			const source = entry.durable_source_id;
			if (typeof source === 'string' && source.startsWith('child:')) {
				return false;
			}
			const runId = entry.run_id ?? null;
			return runId === null || !childRunIds.has(String(runId));
		});
		if (!hasDelegationCard || childRunIds.size === 0) return rows;
		return rows.filter((entry) => {
			const payload = entry.payload;
			return !(
				payload.kind === 'tool_result' &&
				payload.capability_id === 'session_delegate' &&
				payload.status === 'executed'
			);
		});
	});

	/**
	 * CTR-W2: the render units handed to the virtualizer — standalone
	 * rows or TURN GROUPS (an assistant lead + its contiguous
	 * tool_result/delegation children; see ./turnGroups.ts for the
	 * pure grouping rules). The virtualizer's data stays a FLAT list
	 * keyed by each unit's LEADING seq: keys remain unique and stable
	 * while entries append (a group's key is its lead's seq and never
	 * changes), so measured rows, `shift`-compensated prepends and the
	 * load-older flow keep working. Only a prepend can refold the
	 * first visible orphan into a group — one keyed item replaced by
	 * an earlier key, an ordinary keyed-data change for virtua.
	 */
	const turnUnits = $derived(buildTurnUnits(visibleEntries));

	/** The run states that mean "the agent is working right now". */
	const GENERATING_RUN_STATES: ReadonlySet<string> = new Set([
		'queued',
		'running',
		'waiting_child'
	]);
	/** The run states surfaced as a compact terminal inline notice. */
	const NOTICE_RUN_STATES: ReadonlySet<string> = new Set([
		'failed',
		'cancelled',
		'outcome_unknown',
		'waiting_approval'
	]);

	/** The latest PARENT run-state entry's lifecycle value (server truth). */
	const serverRunState = $derived.by<TurnRunState | null>(() => {
		for (let i = entries.length - 1; i >= 0; i -= 1) {
			const entry = entries[i];
			const payload = entry.payload;
			if (payload.kind === 'run_state') {
				// A delegated CHILD settling never drives the parent surface:
				// its `succeeded` transition folds into the delegation card
				// and must not turn off the parent's "Running delegated
				// child…" indicator while the parent is still active.
				const source = entry.durable_source_id;
				if (
					childRunIds.has(String(entry.run_id ?? '')) ||
					(typeof source === 'string' && source.startsWith('child:'))
				) {
					continue;
				}
				// The run identity lives on the ENTRY ENVELOPE (the payload
				// carries only the transition state/reason).
				return {
					run_id: String(entry.run_id ?? ''),
					state: payload.state as SessionRunState
				};
			}
		}
		return null;
	});
	/** Server truth wins; the optimistic mirror fills the submit gap. */
	const effectiveRunState = $derived(serverRunState ?? optimisticRunState);
	/** The live generation indicator: working run, in-flight submit or a
	 * still-streaming draft. */
	const generating = $derived(
		submitting ||
			drafts.length > 0 ||
			(effectiveRunState !== null &&
				GENERATING_RUN_STATES.has(String(effectiveRunState.state)))
	);
	/** The compact terminal notice state (only when NOT generating). */
	const terminalNotice = $derived.by<string | null>(() => {
		if (generating || effectiveRunState === null) return null;
		const state = String(effectiveRunState.state);
		return NOTICE_RUN_STATES.has(state) ? state : null;
	});

	const generatingLabel = $derived.by(() => {
		if (drafts.length > 0 || submitting) return 'Generating…';
		const state = String(effectiveRunState?.state ?? 'running');
		if (state === 'queued') return 'Queued — the engine picked up the turn';
		if (state === 'waiting_child') return 'Running delegated child…';
		return 'Generating…';
	});

	const noticeLabel = $derived.by(() => {
		switch (terminalNotice) {
			case 'failed':
				return 'The run failed.';
			case 'cancelled':
				return 'The run was cancelled.';
			case 'outcome_unknown':
				return 'The run outcome is unknown — reconcile before retrying.';
			case 'waiting_approval':
				return 'Waiting for approval.';
			default:
				return '';
		}
	});

	// --- virtualization (virtua over the filtered cache) ----------------------
	/**
	 * `virtua/svelte`'s `Virtualizer` owns the window: it measures every
	 * mounted row (ResizeObserver), positions rows absolutely and mounts
	 * only the rows near the viewport (+400px buffer), so the DOM never
	 * mirrors the whole cache. `mountedDomCount` — tracked by the tiny
	 * `trackMounted` action on each entry article — surfaces the live
	 * mounted-row count to the E2E assertions through `data-dom-count`.
	 */
	let mountedDomCount = $state(0);
	function trackMounted(_node: HTMLElement) {
		mountedDomCount += 1;
		return {
			destroy() {
				mountedDomCount -= 1;
			}
		};
	}

	/**
	 * The "Load older" trigger occupies the top of the scroll container
	 * before the virtualizer; virtua must know its height to keep its
	 * scroll-offset math exact (`startMargin`).
	 */
	let olderHeaderHeight = $state(0);
	const effectiveStartMargin = $derived(hasOlder ? olderHeaderHeight : 0);

	let virtualizerRef: VirtualizerHandle | null = $state(null);

	/**
	 * Detect an upward `before_seq` prepend — an older page landed at the
	 * TOP of the filtered cache — for virtua's `shift` prop. True ONLY
	 * for the update that prepends, so virtua compensates `scrollTop`
	 * and the reader's position stays put; every other update (tail
	 * append, cache eviction, session swap) keeps it false.
	 */
	let lastSeenFirstSeq: number | null = null;
	let lastSeenSessionId: string | null = null;
	const isPrepending = $derived.by(() => {
		const sid = sessionId ?? null;
		const first = visibleEntries.length > 0 ? Number(visibleEntries[0].seq) : null;
		const prepending =
			sid === lastSeenSessionId &&
			lastSeenFirstSeq !== null &&
			first !== null &&
			first < lastSeenFirstSeq;
		lastSeenSessionId = sid;
		lastSeenFirstSeq = first;
		return prepending;
	});

	const oldestSeq = $derived(
		visibleEntries.length > 0 ? Number(visibleEntries[0].seq) : 0
	);
	const latestSeq = $derived(
		visibleEntries.length > 0
			? Number(visibleEntries[visibleEntries.length - 1].seq)
			: 0
	);

	// --- scroll anchoring -----------------------------------------------------
	let viewportEl: HTMLElement | null = $state(null);
	let prevLastSeq: number | null = null;
	let anchoredToBottom = $state(true);

	/** A session swap re-anchors to the NEW session's tail. */
	$effect(() => {
		void sessionId;
		anchoredToBottom = true;
	});

	function onScroll(): void {
		if (viewportEl === null) return;
		anchoredToBottom =
			viewportEl.scrollTop + viewportEl.clientHeight >=
			viewportEl.scrollHeight - 24;
		// Demand-load older pages near the top (the shell deduplicates).
		// `prevLastSeq !== null` keeps the initial-mount churn from
		// firing a fetch before the first page has settled.
		if (
			viewportEl.scrollTop <= 64 &&
			hasOlder &&
			!loadingOlder &&
			prevLastSeq !== null
		) {
			onloadolder();
		}
	}

	/**
	 * Follow the tail while anchored: a NEW committed tail —
	 * `lastSeq > prevLastSeq` or the first page itself — extends the
	 * transcript below the fold, so scroll to the bottom after the DOM
	 * settles. untrack: scrolling at the bottom toggles
	 * `anchoredToBottom`, which must NEVER re-run this effect.
	 */
	$effect(() => {
		const lastSeq =
			visibleEntries.length > 0
				? Number(visibleEntries[visibleEntries.length - 1].seq)
				: null;
		const appended =
			lastSeq !== null &&
			(prevLastSeq === null || lastSeq > prevLastSeq);
		prevLastSeq = lastSeq;
		if (!appended) return;
		if (!untrack(() => anchoredToBottom)) return;
		void tick().then(() => {
			if (viewportEl !== null && untrack(() => anchoredToBottom)) {
				viewportEl.scrollTop = viewportEl.scrollHeight;
			}
		});
	});

	/** A drafts/generating change while anchored also follows the tail. */
	$effect(() => {
		void drafts.length;
		void generating;
		if (!untrack(() => anchoredToBottom)) return;
		void tick().then(() => {
			if (viewportEl !== null && untrack(() => anchoredToBottom)) {
				viewportEl.scrollTop = viewportEl.scrollHeight;
			}
		});
	});

	function onComposerKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter' && !event.shiftKey) {
			event.preventDefault();
			void submitTurn();
		}
	}

	/**
	 * CTR-W3 disclosure defaults (C08 rule 6 honesty):
	 *  - a tool_result whose outcome is NOT settled (denied /
	 *    approval_required / unknown_effect) renders OPEN — an unproven
	 *    outcome must stay visible without a click;
	 *  - a settled (`executed`) result — and the fixture/out-of-run case
	 *    (runId === null) — renders COLLAPSED: raw output no longer
	 *    dumps into the chat flow, it is one click away;
	 *  - a delegation always renders COLLAPSED behind a one-line
	 *    summary (child run + status WORD) — its goal/result_summary
	 *    live in the body and must never inflate the chat flow.
	 */

	/**
	 * Decode a tool_result `output_preview` for display (M2):
	 *
	 *  - a JSON object with a non-empty string `summary` field (the
	 *    delegation settlement document) renders that summary through
	 *    MarkdownRenderer;
	 *  - anything else decodes ASCII escapes: historical DB rows persist
	 *    `json.dumps(..., ensure_ascii=True)` output, so `\uXXXX` sequences
	 *    and `\n` pairs inside JSON / truncated-JSON strings become real
	 *    UTF-8 text (`\u0427` → `Ч`) instead of raw escape noise.
	 */
	function formatToolOutputPreview(raw: string): { markdown: boolean; text: string } {
		try {
			const parsed: unknown = JSON.parse(raw);
			if (parsed !== null && typeof parsed === 'object' && 'summary' in parsed) {
				const summary = (parsed as { summary: unknown }).summary;
				if (typeof summary === 'string' && summary !== '') {
					return { markdown: true, text: summary };
				}
			}
		} catch {
			// Not JSON — fall through to the escape decode.
		}
		const decoded = raw
			// JSON \uXXXX escapes are BMP code units (surrogate pairs decode
			// unit-by-unit), so per-escape fromCharCode is exact.
			.replace(/\\u([0-9a-fA-F]{4})/g, (_, hex: string) =>
				String.fromCharCode(parseInt(hex, 16))
			)
			.replace(/\\n/g, '\n');
		return { markdown: false, text: decoded };
	}
</script>

<div class="mai-session-chat" data-testid="session-chat-panel">
	{#if !usable}
		<div class="mai-session-chat__state" role="alert" data-testid="session-config-error">
			<p>This panel was opened without its session services.</p>
		</div>
	{:else}
		<header class="mai-session-chat__header">
			<!-- Refresh continuity (R2-3): the id stays in the DOM for the E2E
			     assertions, visually silent — the chat is the surface now. -->
			<span class="mai-session-chat__id" data-testid="session-header-id">
				{sessionId === null ? 'new session' : `session ${sessionId.slice(0, 8)}…`}
			</span>
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
			data-dom-count={mountedDomCount}
			data-oldest-seq={oldestSeq}
			data-latest-seq={latestSeq}
			data-has-older={hasOlder ? 'true' : 'false'}
			bind:this={viewportEl}
			onscroll={onScroll}
			tabindex="0"
			role="log"
			aria-label="Session entry history"
		>
			{#if hasOlder || loadingOlder}
				<div class="mai-session-chat__older" bind:offsetHeight={olderHeaderHeight}>
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
			{#if visibleEntries.length === 0 && channelState !== 'connecting' && channelState !== 'reconnecting'}
				<p class="mai-session-chat__state" data-testid="session-history-empty">
					No committed entries yet — say something first.
				</p>
			{/if}
			{#if visibleEntries.length > 0}
				<!-- scrollRef stays optional-typed: while viewportEl is still
				     null (first mount) virtua observes the parent element,
				     which IS this viewport. -->
				<Virtualizer
					bind:this={virtualizerRef}
					data={turnUnits}
					getKey={(unit) => unit.key}
					scrollRef={viewportEl ?? undefined}
					shift={isPrepending}
					startMargin={effectiveStartMargin}
					bufferSize={400}
					itemProps={() => ({
						// HORIZONTAL padding ONLY. virtua measures this
						// wrapper via a ResizeObserver reading the CONTENT
						// box (contentRect), never the border box: vertical
						// padding here is invisible to the layout — offsets
						// advance by the content height while the painted
						// box stays taller, so rows overlapped by exactly
						// the wrapper's vertical padding (measured 0px gaps
						// on the live deployment). The vertical rhythm
						// therefore lives on the article (.mai-session-entry
						// padding-bottom), INSIDE the measured content box.
						// box-sizing stays explicit so a future padding edit
						// here cannot silently reintroduce the overlap.
						style: { padding: '0 10px', 'box-sizing': 'border-box' }
					})}
					onresize={() => {
						// Measurements change the total height: while the
						// reader is anchored to the tail, re-pin the scroll.
						if (viewportEl !== null && anchoredToBottom && !loadingOlder) {
							viewportEl.scrollTop = viewportEl.scrollHeight;
						}
					}}
				>
					{#snippet children(unit: TurnUnit)}
						{#snippet entryArticle(entry: ChannelEntry, suppressToolCalls = false)}
						<article
							class="mai-session-entry"
							data-testid="session-entry"
							data-entry-seq={entry.seq}
							data-entry-kind={entry.payload.kind}
							use:trackMounted
						>
						{#if entry.payload.kind === 'user_message'}
							<div class="mai-user-bubble" data-testid="entry-user-message">
								<!-- The wrapper keeps the parent-scoped text styles: Svelte
							     does NOT carry them through a class prop onto a child
							     component's root (pruned as unused by the compiler). -->
								<div class="mai-session-entry__text">
									<MarkdownRenderer content={entry.payload.text} />
								</div>
							</div>
						{:else if entry.payload.kind === 'assistant_message'}
							<div class="mai-assistant-flow" data-testid="entry-assistant-message">
								{#if entry.payload.text !== ''}
									<div class="mai-session-entry__text">
										<MarkdownRenderer content={entry.payload.text ?? ''} />
									</div>
								{/if}
								{#if entry.payload.provider_artifacts !== undefined && entry.payload.provider_artifacts.length > 0}
									<details class="mai-thinking-disclosure" data-testid="entry-thinking">
										<summary>Ход мыслей</summary>
										<div class="mai-thinking-disclosure__body">
											{#each entry.payload.provider_artifacts as artifact}
												<div class="mai-thinking-disclosure__line">
													<MarkdownRenderer content={artifact.content} />
												</div>
											{/each}
										</div>
									</details>
								{/if}
								{#if !suppressToolCalls && entry.payload.tool_calls !== undefined && entry.payload.tool_calls.length > 0}
									<!-- CTR-W3: the standalone (ungrouped) declared-calls
									     disclosure — summary lists the capabilities, no
									     fabricated status (no result is in this window). In a
									     turn group this is suppressed: the group's ONE
									     tool-group disclosure carries capability + result
									     status. No hardcoded `open`: collapsed by default. -->
									<details
										class="mai-tool-disclosure mai-tool-group"
										data-testid="entry-tool-calls"
									>
										<summary class="mai-tool-group__summary">
											<span class="mai-tool-group__label">
												tool calls ({entry.payload.tool_calls.length})
											</span>
											{#each entry.payload.tool_calls as call (call.call_id)}
												<span class="mai-tool-group__item">
													<code>{call.capability_id}</code>
												</span>
											{/each}
										</summary>
										<ul class="mai-tool-disclosure__list">
											{#each entry.payload.tool_calls as call (call.call_id)}
												<li class="mai-tool-disclosure__item" data-testid="entry-tool-call">
													<code>{call.capability_id}</code>
												</li>
											{/each}
										</ul>
									</details>
								{/if}
							</div>
						{:else if entry.payload.kind === 'tool_result'}
							<details
								class="mai-tool-disclosure"
								data-testid="entry-tool-result"
								open={entry.payload.status !== 'executed'}
							>
								<summary class="mai-tool-disclosure__summary">
									<code>{entry.payload.capability_id}</code>
									<span
										class="mai-tool-status"
										class:mai-tool-status--warn={entry.payload.status === 'unknown_effect' || entry.payload.status === 'approval_required'}
										class:mai-tool-status--bad={entry.payload.status === 'denied'}
										data-testid="tool-status-badge"
									>
										{#if entry.payload.status === 'unknown_effect'}
											Outcome unknown — effect not proven
										{:else}
											{entry.payload.status}
										{/if}
									</span>
								</summary>
								<div class="mai-tool-disclosure__body">
									{#if entry.payload.output_preview !== null && entry.payload.output_preview !== undefined && entry.payload.output_preview !== ''}
										{@const preview = formatToolOutputPreview(entry.payload.output_preview)}
										{#if preview.markdown}
											<div class="mai-tool-disclosure__output">
												<MarkdownRenderer content={preview.text} />
											</div>
										{:else}
											<p class="mai-tool-disclosure__output">{preview.text}</p>
										{/if}
									{/if}
									{#if entry.payload.error_code !== null && entry.payload.error_code !== undefined}
										<p class="mai-session-entry__error" data-testid="tool-error">
											{entry.payload.error_code}: {entry.payload.error_detail ?? ''}
										</p>
									{/if}
								</div>
							</details>
						{:else if entry.payload.kind === 'delegation'}
							<!-- CTR-W3: no `open` binding — ALWAYS collapsed by default.
							     The one-line summary (child run + status WORD) is the
							     whole visible card; goal/result_summary/error stay in
							     the body behind one click (a live delegation card was
							     measured at 1616px tall — it must never inflate the
							     chat flow). Native <details> toggling still works. -->
							<details
								class="mai-tool-disclosure mai-tool-disclosure--delegation"
								data-testid="entry-delegation"
							>
								<summary class="mai-tool-disclosure__summary">
									<span class="mai-tool-disclosure__title">delegation</span>
									<code class="mai-tool-disclosure__child" data-testid="delegation-child-id">
										{entry.payload.child_run_id.slice(0, 8)}
									</code>
									<span
										class="mai-tool-status"
										class:mai-tool-status--warn={entry.payload.status === 'queued' || entry.payload.status === 'running'}
										class:mai-tool-status--bad={entry.payload.status === 'failed'}
										data-testid="tool-status-badge"
									>
										{entry.payload.status}
									</span>
								</summary>
								<div class="mai-tool-disclosure__body">
									<p class="mai-tool-disclosure__meta">
										child run <code>{entry.payload.child_run_id}</code>
									</p>
									{#if entry.payload.goal !== ''}
										<div class="mai-session-entry__text">
											<MarkdownRenderer content={entry.payload.goal} />
										</div>
									{/if}
									{#if entry.payload.result_summary !== null && entry.payload.result_summary !== undefined}
										<div class="mai-tool-disclosure__output" data-testid="delegation-summary">
											<MarkdownRenderer content={entry.payload.result_summary} />
										</div>
									{/if}
									{#if entry.payload.error_code !== null && entry.payload.error_code !== undefined}
										<p class="mai-session-entry__error">{entry.payload.error_code}</p>
									{/if}
								</div>
							</details>
						{:else if entry.payload.kind === 'compaction'}
							<div class="mai-compaction" data-testid="compaction-marker">
								<span class="mai-compaction__label">
									Compacted entries seq {entry.payload.from_seq}..{entry.payload.to_seq}
								</span>
								<details class="mai-tool-disclosure" data-testid="compaction-summary">
									<summary>summary</summary>
									<div class="mai-session-entry__text">
										<MarkdownRenderer content={entry.payload.summary_text} />
									</div>
								</details>
							</div>
						{:else if entry.payload.kind === 'control'}
							<p
								class="mai-control-note"
								data-testid="entry-control"
								data-control-action={entry.payload.action}
							>
								{entry.payload.action}{#if entry.payload.note !== null && entry.payload.note !== undefined}: {entry.payload.note}{/if}
							</p>
						{/if}
						</article>
						{/snippet}
						{#if unit.kind === 'group'}
							<!-- CTR-W2/W3: one turn group = the assistant lead's prose at
							     the base indent + its tool activity and delegations 16px
							     deeper, with the 1px rail spanning the group's full height
							     on its left edge. A childless lead renders as a plain row —
							     no rail, no group container. CTR-W3: each run of tool
							     results is ONE collapsed disclosure whose summary carries
							     every capability + status WORD; raw outputs live inside. -->
							{@const segments = buildTurnChildSegments(unit.lead, unit.children)}
							{@const hasToolChildren = segments.some(
								(segment) => segment.kind === 'tools'
							)}
							<div
								class="mai-turn-group"
								data-testid="turn-group"
								data-turn-run-id={unit.runId}
							>
								{@render entryArticle(unit.lead, hasToolChildren)}
								<div class="mai-turn-group__children">
									{#each segments as segment, segmentIndex (segmentIndex)}
										{#if segment.kind === 'tools'}
											<!-- Open ONLY while some outcome is unproven (C08
							     rule 6): a denied/unknown/approval-pending result must
							     stay visible without a click; settled rounds collapse. -->
											<details
												class="mai-tool-disclosure mai-tool-group"
												data-testid="turn-tool-group"
												open={segment.entries.some(
													(child) =>
														child.payload.kind === 'tool_result' &&
														child.payload.status !== 'executed'
												)}
											>
												<summary class="mai-tool-group__summary">
													<span class="mai-tool-group__label">tools</span>
													{#each segment.summary as item, itemIndex (itemIndex)}
														<span class="mai-tool-group__item">
															<code>{item.capabilityId}</code>
															<span
																class="mai-tool-status"
																class:mai-tool-status--warn={item.unsettled &&
																	item.status !== 'denied'}
																class:mai-tool-status--bad={item.status === 'denied'}
																data-testid="tool-status-badge"
															>
																{item.status}
															</span>
														</span>
													{/each}
												</summary>
												<div class="mai-tool-group__body">
													{#each segment.entries as child (child.seq)}
														{@render entryArticle(child)}
													{/each}
												</div>
											</details>
										{:else}
											<div class="mai-turn-group__child" data-testid="turn-group-child">
												{@render entryArticle(segment.entry)}
											</div>
										{/if}
									{/each}
								</div>
							</div>
						{:else}
							{@render entryArticle(unit.entry)}
						{/if}
					{/snippet}
				</Virtualizer>
			{/if}
		</div>

		{#if drafts.length > 0}
			<div class="mai-session-chat__drafts" data-testid="session-stream-drafts">
				{#each drafts as draft (draft.stream_id)}
					<!-- div, not p: the renderer's block-level root could not
					     nest inside a p (the parser would hoist it out and the
					     testid would lose its content). -->
					<div class="mai-session-chat__draft" data-testid="session-stream-draft" data-stream-id={draft.stream_id}>
						<MarkdownRenderer content={draft.text.slice(-240)} />
					</div>
				{/each}
			</div>
		{/if}

		{#if generating}
			<div class="mai-generating" data-testid="session-generating-indicator" data-run-state={String(effectiveRunState?.state ?? 'running')} role="status">
				<span class="mai-generating__dots" aria-hidden="true">
					<i></i><i></i><i></i>
				</span>
				<span class="mai-generating__bar" aria-hidden="true"></span>
				<span class="mai-generating__label">{generatingLabel}</span>
			</div>
		{:else if terminalNotice !== null}
			<p
				class="mai-terminal-notice"
				data-testid="session-terminal-notice"
				data-run-state={terminalNotice}
				role="status"
			>
				{noticeLabel}
			</p>
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
	/*
	 * CTR rhythm scale (8px grid, PLAN §3.1) — the ONE place that owns
	 * every vertical gap in the transcript:
	 *   --mai-turn-gap        16px  user ↔ assistant turn boundary
	 *   --mai-tool-gap         8px  assistant prose ↔ its own tool group
	 *   --mai-call-gap         4px  inside a tool group (capability ↔
	 *                                its result; tool group ↔ delegation)
	 *   --mai-session-rhythm  12px  base gap for rows outside a turn
	 *                                relation (orphan results/delegations,
	 *                                compaction, control notes)
	 * Plus the CTR-W3 group label size (13px / 600 — PLAN §3.2).
	 */
	.mai-session-chat {
		--mai-turn-gap: 16px;
		--mai-tool-gap: 8px;
		--mai-call-gap: 4px;
		--mai-session-rhythm: 12px;
		--mai-tool-label-size: 0.8125rem;
	}
	.mai-session-chat__header {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		align-items: center;
		padding: 2px 8px;
		font-size: 0.75rem;
	}
	/* Refresh continuity (R2-3): present for the E2E assertions, visually
	   silent — assistive technology still reads the active session id. */
	.mai-session-chat__id {
		position: absolute;
		width: 1px;
		height: 1px;
		margin: -1px;
		padding: 0;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
		border: 0;
	}
	.mai-session-chat__resync {
		color: var(--mai-color-warning, #b45309);
	}
	.mai-session-chat__viewport {
		flex: 1 1 auto;
		min-height: 0;
		/* virtua requires a BLOCK scroll container: its rows are absolutely
		   positioned against the virtualizer root. */
		display: block;
		overflow-y: auto;
		overflow-x: hidden;
		overflow-anchor: none;
		outline: none;
	}
	.mai-session-chat__older {
		display: flex;
		justify-content: center;
		padding: 4px 0;
		flex-shrink: 0;
	}
	/* ---- entries: a chat stream, not an audit table (S3) ---------------- */
	.mai-session-entry {
		display: flex;
		flex-direction: column;
		/* The vertical rhythm lives HERE, as padding INSIDE virtua's
		   measured content box (the wrapper's ResizeObserver reads its
		   contentRect): padding-bottom advances the measured height
		   together with the painted box, so rows never overlap. A
		   margin would fall OUTSIDE the measured height and
		   reintroduce the overlap; the wrapper itself must stay
		   vertical-padding-free (see itemProps). box-sizing explicit:
		   the padding must stay outside the declared content box.
		   CTR-W3: the VALUES live once on .mai-session-chat (the
		   rhythm-scale token block above); the base gap applies to
		   rows outside a turn relation, the kind rules below refine
		   it per relation. */
		padding-bottom: var(--mai-session-rhythm);
		margin: 0;
		box-sizing: content-box;
	}
	/* CTR-W3 per-relation gaps: a user or standalone-assistant row ends
	   at a TURN boundary; a group lead's prose meets its own tool
	   activity tighter. Specificity: the group override below beats
	   these; the zero rules after it beat the base. */
	.mai-session-entry[data-entry-kind='user_message'],
	.mai-session-entry[data-entry-kind='assistant_message'] {
		padding-bottom: var(--mai-turn-gap);
	}
	.mai-turn-group {
		padding-bottom: var(--mai-turn-gap);
	}
	.mai-turn-group > .mai-session-entry[data-entry-kind='assistant_message'] {
		padding-bottom: var(--mai-tool-gap);
	}
	.mai-turn-group__child .mai-session-entry,
	.mai-tool-group__body .mai-session-entry {
		padding-bottom: 0;
	}
	/* ---- turn groups (CTR-W2): an assistant turn + its children ---- */
	.mai-turn-group {
		position: relative;
	}
	/* The left rail: one 1px muted line on the group's left edge,
	   spanning the group's FULL height (the lead prose + every child).
	   It exists only for real groups — a childless lead renders as a
	   plain row, so no rail appears without children. */
	.mai-turn-group::before {
		content: '';
		position: absolute;
		left: 0;
		top: 0;
		bottom: 0;
		width: 1px;
		background: var(--mai-color-border, rgba(0, 0, 0, 0.25));
	}
	/* Children (tool groups, delegations) sit --mai-space-4 (16px)
	   deeper than the group's content edge, so the parent/child
	   relation reads at a glance. CTR-W3: the 4px gap between sibling
	   children IS the "inside a tool group" rhythm step. */
	.mai-turn-group__children {
		display: flex;
		flex-direction: column;
		gap: var(--mai-call-gap);
		padding-left: var(--mai-space-4, 16px);
	}
	/* ---- CTR-W3: the collapsed tool group + typed summary --------------- */
	.mai-tool-group__summary {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mai-space-2, 8px);
		align-items: baseline;
	}
	.mai-tool-group__label {
		font-size: var(--mai-tool-label-size, 0.8125rem);
		font-weight: 600;
	}
	.mai-tool-group__item {
		display: inline-flex;
		flex-wrap: wrap;
		gap: var(--mai-space-1, 4px);
		align-items: baseline;
	}
	.mai-tool-group__body {
		display: flex;
		flex-direction: column;
		gap: var(--mai-call-gap);
		padding: 2px 0 2px 10px;
	}
	/* Delegation one-line summary: the short child-run id sits next to
	   the title (the FULL id stays in the collapsed body). */
	.mai-tool-disclosure__child {
		font-size: var(--mai-text-xs, 0.75rem);
	}
	.mai-session-entry__text {
		margin: 0;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.mai-session-entry__error {
		margin: 0;
		color: var(--mai-color-danger, #b3261e);
		font-size: 0.75rem;
	}
	/* User messages: right-aligned accent-tinted bubbles (S3). */
	.mai-user-bubble {
		align-self: flex-end;
		max-width: 82%;
		margin-left: auto;
		padding: 6px 10px;
		border-radius: 14px;
		background: color-mix(
			in oklch,
			var(--mai-color-accent, #b45309) 16%,
			var(--mai-color-bg-elevated, rgba(0, 0, 0, 0.04))
		);
	}
	/* Assistant messages: borderless prose straight on the canvas. */
	.mai-assistant-flow {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	/* Thinking: collapsed "Ход мыслей", italic muted body (S3). */
	.mai-thinking-disclosure {
		font-size: 0.78rem;
	}
	.mai-thinking-disclosure summary {
		cursor: pointer;
		color: var(--mai-color-text-muted, var(--mai-color-muted, inherit));
	}
	.mai-thinking-disclosure__body {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 2px 0 2px 10px;
	}
	.mai-thinking-disclosure__line {
		margin: 0;
		font-style: italic;
		color: var(--mai-color-text-muted, var(--mai-color-muted, inherit));
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	/* Tools + delegations: compact collapsed disclosures (S3). Open while
	   the outcome is unproven (or the row has no run scope — the synthetic
	   fixtures) so errors/refusals stay immediately visible. */
	.mai-tool-disclosure {
		font-size: 0.78rem;
		border: none;
	}
	.mai-tool-disclosure summary {
		display: flex;
		gap: 8px;
		align-items: baseline;
		cursor: pointer;
		color: var(--mai-color-text-muted, var(--mai-color-muted, inherit));
		list-style: revert;
	}
	.mai-tool-disclosure__summary {
		flex-wrap: wrap;
	}
	.mai-tool-disclosure__body {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 2px 0 2px 10px;
	}
	.mai-tool-disclosure__list {
		margin: 0;
		padding: 0;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.mai-tool-disclosure__item code,
	.mai-tool-disclosure__summary code {
		font-size: 0.74rem;
	}
	.mai-tool-disclosure__meta {
		margin: 0;
		color: var(--mai-color-text-muted, var(--mai-color-muted, inherit));
		overflow-wrap: anywhere;
	}
	.mai-tool-disclosure__output {
		margin: 0;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		color: var(--mai-color-text-muted, var(--mai-color-muted, inherit));
		font-size: 0.76rem;
	}
	.mai-tool-status {
		font-size: 0.7rem;
		padding: 0 6px;
		border-radius: 8px;
		background: var(--mai-color-surface-raised, rgba(0, 0, 0, 0.06));
	}
	.mai-tool-status--warn {
		background: var(--mai-color-warning, #b45309);
		color: #fff;
	}
	.mai-tool-status--bad {
		background: var(--mai-color-danger, #b3261e);
		color: #fff;
	}
	.mai-compaction {
		display: flex;
		gap: 10px;
		align-items: baseline;
		flex-wrap: wrap;
		padding: 4px 10px;
		font-size: 0.75rem;
		color: var(--mai-color-text-muted, var(--mai-color-muted, inherit));
	}
	.mai-compaction__label {
		font-weight: 600;
	}
	.mai-control-note {
		margin: 0;
		padding: 0 10px;
		font-size: 0.75rem;
		font-style: italic;
		color: var(--mai-color-text-muted, var(--mai-color-muted, inherit));
	}
	/* ---- live generation indicator (S3) ---------------------------------- */
	.mai-generating {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 6px 12px 2px;
	}
	.mai-generating__dots {
		display: inline-flex;
		gap: 4px;
	}
	.mai-generating__dots i {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--mai-color-accent, #b45309);
		animation: mai-generating-dot 1.1s ease-in-out infinite;
	}
	.mai-generating__dots i:nth-child(2) {
		animation-delay: 0.18s;
	}
	.mai-generating__dots i:nth-child(3) {
		animation-delay: 0.36s;
	}
	@keyframes mai-generating-dot {
		0%,
		100% {
			opacity: 0.25;
			transform: translateY(0);
		}
		50% {
			opacity: 1;
			transform: translateY(-2px);
		}
	}
	.mai-generating__bar {
		display: block;
		height: 3px;
		border-radius: 2px;
		background: linear-gradient(
			90deg,
			color-mix(in oklch, var(--mai-color-accent, #b45309) 12%, transparent),
			color-mix(in oklch, var(--mai-color-accent, #b45309) 55%, transparent) 45%,
			color-mix(in oklch, var(--mai-color-accent, #b45309) 12%, transparent)
		);
		background-size: 220% 100%;
		animation: mai-generating-shimmer 1.6s linear infinite;
	}
	@keyframes mai-generating-shimmer {
		0% {
			background-position: 120% 0;
		}
		100% {
			background-position: -120% 0;
		}
	}
	.mai-generating__label {
		font-size: 0.72rem;
		color: var(--mai-color-text-muted, var(--mai-color-muted, inherit));
	}
	@media (prefers-reduced-motion: reduce) {
		.mai-generating__dots i,
		.mai-generating__bar {
			animation: none;
		}
	}
	.mai-terminal-notice {
		margin: 0;
		padding: 6px 12px 2px;
		font-size: 0.75rem;
		color: var(--mai-color-warning, #b45309);
	}
	/* ---- drafts + composer ----------------------------------------------- */
	.mai-session-chat__drafts {
		padding: 2px 12px;
		border-top: 1px dashed var(--mai-color-border, transparent);
	}
	.mai-session-chat__draft {
		margin: 0;
		font-size: 0.78rem;
		color: var(--mai-color-text-muted, var(--mai-color-muted, inherit));
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
		color: var(--mai-color-text-muted, var(--mai-color-muted, inherit));
	}
</style>
