<script module lang="ts">
	/**
	 * ChatPanel — reusable durable-conversation panel (card `mai-06-web-chat`,
	 * P06; service row `mai-web`; contracts C05/C06/C08/C11).
	 *
	 * Layer: feature panel (windowed-workspace ADR-0001 §2 layer table). It owns
	 * its one domain view and nothing else:
	 *
	 *  - Explicit context only: a SERVER-scoped conversation view. Every call
	 *    names the target pinned at open time (`panel.scope.server_id`, C01
	 *    rule 2) — the submit body carries it, the history list is queried with
	 *    it, and attachments reference artifacts of that same target. The module
	 *    reads no launcher/current-server value anywhere, so there is no hidden
	 *    global mutation target and a launcher re-selection can never retarget
	 *    an open conversation, a submit or an approval decision (C08 rule 5,
	 *    NT5).
	 *  - Explicit services only: the eight-function `ChatApi` enters through
	 *    `panel.services.api` (the typed bag owned by the feature entry
	 *    module). A missing or incomplete bag fails closed with a visible
	 *    state — no service locator, no fallback client.
	 *  - Durable truth, replayed (C05 rule 4): everything rendered is
	 *    value-free wire truth — the replayed `ConversationView`, the recorded
	 *    attachment handles, and the run's persisted event stream. The message
	 *    body shown is the text THIS window authored at submit time; the wire
	 *    carries no task/prompt bodies, so a conversation opened from history
	 *    says exactly that instead of guessing. Streamed replies render as
	 *    recorded facts (rounds, requested tool calls with capability and
	 *    payload-hash handles, output sizes, final summary) — never invented
	 *    text (C08 rule 6: never all-green on unknown data).
	 *  - One child-tree implementation: the delegation subtree is the reviewed
	 *    `DelegationTree` view (`06-web-delegation`), composed with this
	 *    panel's bag — deliberately not re-implemented (brief acceptance 2).
	 *    Child task labels are the labels this window submitted, linked through
	 *    the producer's `agent.delegation_planned` child-id order (body order,
	 *    frozen by `plan_children`); a history-opened conversation renders ids
	 *    only. Cancellation is the tree's explicit tree-cancel control; this
	 *    panel adds no second one.
	 *  - Approval path (C03 rule 4): when the record shows a bounded action
	 *    that required a decision (`step.awaiting_approval`, an
	 *    approval-shaped step outcome, or a `waiting_approval` state), the
	 *    panel reads the run's durable step receipts (`05-jobs-api` read) and,
	 *    when one names a bound decision, renders the binding field-for-field
	 *    and records the ONE-USE decision (`03-approval-api`). No bound record
	 *    is an honest "nothing to decide" state; consumed/expired render as
	 *    recorded. The panel never invents an approval and never re-presents a
	 *    spent one.
	 *  - Event replay without duplicates (C08 rule 7): the subscription
	 *    resumes from the last seen `seq` carried as the SSE `Last-Event-ID`,
	 *    survives the server's bounded stream close and API/network restarts
	 *    with a small fixed backoff, and deduplicates by the monotonic `seq`
	 *    watermark. The stream is read-only: it never submits, cancels or
	 *    retargets anything (windowed ADR §7). Closing/hiding the window
	 *    releases the subscription and starts and cancels nothing.
	 *  - No window mechanics: no drag/geometry/focus/persistence code and no
	 *    workspace-state imports; the window host supplies all of that. The
	 *    stream/refresh controllers are instance-scoped, released on unmount
	 *    (the `$effect` teardown is the release path), and never survive into
	 *    another mount cycle.
	 *  - No module-level state and no `window`/`document`/storage access (ADR
	 *    §5 rule 4): safe to evaluate per SSR request; instances share nothing
	 *    between requests or windows.
	 *
	 * Tokens come only from ui/tokens.css; no dependency is added (ADR §4.3).
	 */
</script>

<script lang="ts">
	import { onDestroy } from 'svelte';
	import Button from '../ui/Button.svelte';
	import Field from '../ui/Field.svelte';
	import Chip from '../ui/Chip.svelte';
	import Select from '../ui/Select.svelte';
	import ModelProfileSelect from '../ui/ModelProfileSelect.svelte';
	import MarkdownRenderer from '../ui/MarkdownRenderer.svelte';
	import DetailsDisclosure from '../ui/DetailsDisclosure.svelte';
	import DelegationTree from '../components/DelegationTree.svelte';
	import TaskEvidence from './chat/TaskEvidence.svelte';
	import type { TaskOutcomeCriterion, TaskOutcomeDocument } from './chat/TaskEvidence.svelte';
	import TaskPlan from './chat/TaskPlan.svelte';
	import TaskUnderstanding from './chat/TaskUnderstanding.svelte';
	import { taskServices, type TaskApi, type TaskServices } from '../workspace/entries/07-web-tasks.js';
	import { readMaiEvent, type MaiRunEvent } from '../api/client.js';
	import type { components } from '../api/schema.generated.js';
	import type { PanelContext, PanelServices } from '../workspace/registry.js';
	import {
		GLOBAL_CONSULT_SERVER_ID,
		type ApprovalView,
		type CancelView,
		type ChatApi,
		type ChatPage,
		type ChatServerOption,
		type ChatServiceOption,
		type ChatServices,
		type ChatSubmitBody,
		type CanonicalConversationDetail,
		type CanonicalConversationPage,
		type CanonicalConversationSummary,
		type CanonicalTranscriptMessage,
		type CanonicalTranscriptPage,
		type CanonicalTurnSubmitted,
		type ConversationView,
		type DecisionBody,
		type RunDetail,
		type TurnDisposition
	} from '../workspace/entries/06-web-chat.js';

	export type ChatHistoryMeta = {
		total: number;
		hasMore: boolean;
		loadingMore: boolean;
	};

	let {
		panel,
		dockCollapsed = $bindable(false),
		onhistorychange,
		onactivechange
	}: {
		panel: PanelContext;
		dockCollapsed?: boolean;
		onhistorychange?: (rows: readonly ConversationView[], meta?: ChatHistoryMeta) => void;
		onactivechange?: (runId: string | null) => void;
	} = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	/** Runtime narrowing of the opaque services bag; absent api ⇒ safe state. */
	function readApi(services: PanelServices): ChatApi | null {
		const candidate = services.api as Partial<ChatApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (
			const name of [
				'submitMessage',
				'listConversations',
				'readConversation',
				'cancelConversation',
				'streamConversationEvents',
				'readRun',
				'readApproval',
				'decideApproval'
			] as const
		) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as ChatApi;
	}

	const api = $derived(readApi(panel.services));
	/** Conversation submission: a global mount contradicts the entry contract. */
	const serverScope = $derived(panel.scope.kind === 'server' ? panel.scope.server_id : null);
	const usable = $derived(api !== null && serverScope !== null);

	/**
	 * Whether ModelProfileSelect is allowed to fetch live profiles on mount.
	 * Only true when the host is live: an explicit `live` bag flag, or the
	 * api slice carrying the real HTTP transport markers (listProfiles or
	 * browser streamConversationEvents). The offline in-memory harness never
	 * triggers a real `/api/v1/models` fetch (fixture acceptance).
	 */
	const profilesLive = $derived.by(() => {
		const bag = panel.services as (PanelServices & { live?: unknown }) | undefined;
		if (bag?.live === true) return true;
		const candidate = bag?.api as Partial<ChatApi> & { listProfiles?: unknown } | undefined;
		if (typeof candidate !== 'object' || candidate === null) return false;
		return typeof candidate.listProfiles === 'function';
	});

	// --- draft chat context (Server Chip + @service tags; locked once started)

	/**
	 * Optional bag extras (typed as the frozen ChatServices shape): the server
	 * list for the draft picker and the mapped/discovered services for the
	 * `@service` autocomplete. A host that passes only `api` still works —
	 * every control below degrades to the pinned scope alone.
	 */
	const serverOptions = $derived.by<readonly ChatServerOption[]>(() => {
		const bag = panel.services as Partial<ChatServices> | undefined;
		const list = bag?.serverOptions;
		if (!Array.isArray(list)) return [];
		return list.filter(
			(one): one is ChatServerOption =>
				typeof one === 'object' && one !== null && typeof one.id === 'string'
		);
	});
	const availableServices = $derived.by<readonly ChatServiceOption[]>(() => {
		const bag = panel.services as Partial<ChatServices> | undefined;
		const list = bag?.availableServices;
		if (!Array.isArray(list)) return [];
		return list.filter(
			(one): one is ChatServiceOption =>
				typeof one === 'object' && one !== null && typeof one.name === 'string'
		);
	});

	/**
	 * Draft target override: `undefined` follows the pinned window scope (the
	 * default Server Chip), a server id pins that server, `null` is General
	 * Consult mode. Once a conversation is open the displayed context is the
	 * RECORDED one — immutable for that session.
	 */
	let draftServerOverride = $state<string | null | undefined>(undefined);
	let selectedServiceTags = $state<string[]>([]);

	/** Display label of one server id: the option list first, then the id. */
	function serverLabelFor(id: string): string {
		return serverOptions.find((one) => one.id === id)?.label ?? `Server ${shortId(id)}`;
	}

	// --- @service autocomplete (draft mode, server pinned) --------------------

	let servicePickerOpen = $state(false);
	let serviceQuery = $state('');

	/**
	 * Suggestions: the host's mapped/discovered services filtered by the typed
	 * `@<query>`, plus one explicit custom entry so a user can pin an
	 * arbitrary `@tag` the inventory does not list yet.
	 */
	const serviceSuggestions = $derived.by<ChatServiceOption[]>(() => {
		const query = serviceQuery.trim().toLowerCase();
		const matched = availableServices.filter(
			(one) => query === '' || one.name.toLowerCase().includes(query)
		);
		const seen = new Set(matched.map((one) => one.name));
		const custom = serviceQuery.trim();
		if (custom !== '' && !seen.has(custom)) matched.push({ name: serviceQuery.trim() });
		return matched;
	});

	/** Typing `@<token>` in the message input opens the autocomplete. */
	function handleComposeInput(event: Event): void {
		const el = event.currentTarget as HTMLTextAreaElement;
		autoGrowComposer();
		const upto = el.value.slice(0, el.selectionStart ?? el.value.length);
		const match = /(?:^|[\s])@([^\s@]*)$/.exec(upto);
		if (match !== null && !chatContextLocked && !generalConsultMode) {
			serviceQuery = match[1];
			servicePickerOpen = true;
		} else if (servicePickerOpen) {
			servicePickerOpen = false;
			serviceQuery = '';
		}
	}

	function pickService(name: string): void {
		const tag = name.trim();
		if (tag !== '' && !selectedServiceTags.includes(tag)) {
			selectedServiceTags = [...selectedServiceTags, tag];
		}
		servicePickerOpen = false;
		serviceQuery = '';
		// The consumed `@<token>` leaves the draft text.
		composeMessage = composeMessage.replace(/@[^\s@]*$/, '');
	}

	function removeServiceTag(tag: string): void {
		selectedServiceTags = selectedServiceTags.filter((one) => one !== tag);
	}

	/** The docked Service Map seam pre-populates a new draft via this event. */
	function handleFocusService(event: Event): void {
		if (chatContextLocked) return;
		const detail = (event as CustomEvent<unknown>).detail;
		if (typeof detail !== 'object' || detail === null) return;
		const record = detail as Record<string, unknown>;
		if (typeof record.serverId === 'string' && record.serverId !== '') {
			draftServerOverride = record.serverId;
		}
		const tag =
			typeof record.serviceName === 'string'
				? record.serviceName
				: typeof record.serviceTag === 'string'
					? record.serviceTag
					: null;
		if (tag !== null && tag.trim() !== '' && !selectedServiceTags.includes(tag)) {
			selectedServiceTags = [...selectedServiceTags, tag];
		}
	}

	// --- floating chat history selector ---------------------------------------

	let historyOpen = $state(false);
	let dockComposerEl = $state<HTMLTextAreaElement | null>(null);

	export function scrollComposerIntoView(): void {
		dockComposerEl?.scrollIntoView({ block: 'nearest' });
	}

	function selectHistoryRow(row: ChatHistoryRow): void {
		historyOpen = false;
		if (row.canonical === true && canonicalApi !== null) {
			void openCanonicalConversation(row);
			return;
		}
		openConversation(row);
	}

	/** Stable element-id namespace for this instance (labels ⇄ inputs). */
	const uid = $props.id();

	// --- producer bounds mirrored for display only (server re-validates) -----

	/** Bounded history page size (10 most recent chats per page; C01 rule 5). */
	const PAGE_LIMIT = 10;
	/** Fixed reconnect delay after a stream close/failure (bounded, visible). */
	const BACKOFF_MS = 750;
	/** Debounce for the durable refresh triggered by delivered events. */
	const REFRESH_DEBOUNCE_MS = 150;
	/** Rendered-transcript cap: a long replay cannot grow the DOM without bound. */
	const MAX_RENDERED_EVENTS = 500;
	/** The frozen default role this panel submits (chat.py DEFAULT_ROLE). */
	const SUBMIT_ROLE = 'administrator';
	/** Auto-growing composer cap (7.5rem): a compact pill that never eats the dock. */
	const COMPOSER_MAX_HEIGHT_PX = 120;


	/** Run lifecycle states after which nothing new can be learned (C05 rule 2). */
	const TERMINAL_RUN_STATES: ReadonlySet<string> = new Set(['succeeded', 'failed', 'cancelled']);

	const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
	const SHA256_RE = /^[0-9a-f]{64}$/;

	// --- value-free display helpers (C08 rule 6) -----------------------------

	const RUN_STATE_TEXT: Readonly<Record<string, string>> = Object.freeze({
		queued: 'Queued',
		running: 'Running',
		waiting_approval: 'Waiting for approval decision',
		waiting_children: 'Waiting for child runs',
		reconciling: 'Reconciling an unproven outcome',
		succeeded: 'Succeeded',
		failed: 'Failed',
		cancelled: 'Cancelled (verified)'
	});

	function runStateText(state: string | null): string {
		if (state === null) return 'State not recorded yet';
		return RUN_STATE_TEXT[state] ?? `Recorded state: ${state}`;
	}

	function shortId(id: string | null): string {
		return id === null || id === '' ? '—' : id.slice(0, 8);
	}

	function shortHash(hash: unknown): string {
		return typeof hash === 'string' && hash !== '' ? hash.slice(0, 12) : '—';
	}

	/** Concise `HH:MM` clock for bubble headers; the full value rides `title`. */
	function formatShortTime(iso: string): string {
		const parsed = Date.parse(iso);
		if (Number.isNaN(parsed)) return iso;
		const stamp = new Date(parsed);
		const pad = (value: number) => String(value).padStart(2, '0');
		return `${pad(stamp.getHours())}:${pad(stamp.getMinutes())}`;
	}

	function codeOf(error: unknown): string | null {
		if (typeof error !== 'object' || error === null || !('code' in error)) return null;
		return typeof error.code === 'string' ? error.code : null;
	}

	function messageOf(error: unknown): string | null {
		return error instanceof Error && error.message ? error.message : null;
	}

	/** Boundary guard: one unknown event payload as a record, else null. */
	function recordOf(value: unknown): Record<string, unknown> | null {
		if (typeof value !== 'object' || value === null) return null;
		return value as Record<string, unknown>;
	}

	/** Frozen outcome vocabulary (`mai.contracts.tasks.TaskOutcome`). */
	const TASK_OUTCOME_VALUES: ReadonlySet<string> = new Set([
		'in_progress',
		'partially_verified',
		'verified',
		'needs_input',
		'failed',
		'cancelled'
	]);
	/** Frozen per-criterion verification vocabulary (`VerificationStatus`). */
	const CRITERION_STATE_VALUES: ReadonlySet<string> = new Set(['passed', 'failed', 'unavailable']);

	/**
	 * Fail-closed narrow of the additive `task_outcome` run-summary document
	 * (`OutcomeObservation.document()`): identifiers and frozen-vocabulary
	 * states only. Any drift — unknown outcome word, non-array/incomplete
	 * criterion states, counts that disagree with the explicit states — is a
	 * rejection (`null`), never a guessed block (C08 rule 6).
	 */
	function readTaskOutcome(data: unknown): TaskOutcomeDocument | null {
		const document = recordOf(recordOf(data)?.['task_outcome']);
		if (document === null) return null;
		const rawOutcome = document['outcome'];
		const outcome =
			typeof rawOutcome === 'string' && TASK_OUTCOME_VALUES.has(rawOutcome) ? rawOutcome : null;
		const criteria = recordOf(document['criteria']);
		if (criteria === null) return null;
		const boundedCount = (value: unknown): number | null =>
			typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
		const total = boundedCount(criteria['total']);
		const supported = boundedCount(criteria['supported']);
		const failed = boundedCount(criteria['failed']);
		const unavailable = boundedCount(criteria['unavailable']);
		if (total === null || supported === null || failed === null || unavailable === null) return null;
		if (supported + failed + unavailable !== total) return null;
		const rawStates = document['criterion_states'];
		if (!Array.isArray(rawStates) || rawStates.length !== total) return null;
		const criterionStates: TaskOutcomeCriterion[] = [];
		const seen = new Set<string>();
		for (const entry of rawStates) {
			const one = recordOf(entry);
			const id = one?.['criterion_id'];
			const state = one?.['state'];
			if (typeof id !== 'string' || id === '') return null;
			if (typeof state !== 'string' || !CRITERION_STATE_VALUES.has(state)) return null;
			if (seen.has(id)) return null;
			seen.add(id);
			criterionStates.push({ criterion_id: id, state });
		}
		const rawRejectionFields = document['rejection_fields'];
		let rejectionFields: Record<string, string> | undefined;
		if (rawRejectionFields !== undefined) {
			if (recordOf(rawRejectionFields) === null) return null;
			rejectionFields = {};
			for (const [field, code] of Object.entries(rawRejectionFields as Record<string, unknown>)) {
				if (typeof code !== 'string') return null;
				rejectionFields[field] = code;
			}
		}
		return {
			task_id: typeof document['task_id'] === 'string' ? (document['task_id'] as string) : null,
			outcome,
			verified: document['verified'] === true,
			recorded: document['recorded'] === true,
			criteria: { total, supported, failed, unavailable },
			criterion_states: criterionStates,
			proposal: typeof document['proposal'] === 'string' ? (document['proposal'] as string) : 'none',
			disposition:
				typeof document['disposition'] === 'string' ? (document['disposition'] as string) : undefined,
			revision: boundedCount(document['revision']) ?? undefined,
			rejection_code:
				typeof document['rejection_code'] === 'string'
					? (document['rejection_code'] as string)
					: undefined,
			rejection_fields: rejectionFields
		};
	}

	function asStringArray(value: unknown): string[] {
		if (!Array.isArray(value)) return [];
		return value.filter((item): item is string => typeof item === 'string');
	}

	/** Stable, display-safe text for the frozen C01 codes (no raw payloads). */
	function friendlyText(code: string, fallback: string): string {
		if (code === 'unauthenticated') return 'Your session has ended — sign in again.';
		if (code === 'throttled') return 'Too many requests — try again shortly.';
		if (code === 'request_rejected') return 'The request was rejected; nothing was changed.';
		if (code === 'validation_failed') return 'The submission was not accepted; see the details.';
		return fallback;
	}

	/** Comma/whitespace separated free list → trimmed, deduped, order-kept. */
	function parseNameList(raw: string): string[] {
		const seen = new Set<string>();
		const out: string[] = [];
		for (const piece of raw.split(/[\s,]+/)) {
			const name = piece.trim();
			if (name === '' || seen.has(name)) continue;
			seen.add(name);
			out.push(name);
		}
		return out;
	}

	function sleep(ms: number, signal: AbortSignal): Promise<void> {
		return new Promise((resolve) => {
			const timer = setTimeout(resolve, ms);
			signal.addEventListener(
				'abort',
				() => {
					clearTimeout(timer);
					resolve();
				},
				{ once: true }
			);
		});
	}

	// --- compose form (instance-local; the ONLY submit path of this panel) ----

	let composeMessage = $state('');
	let composeProfile = $state('default');
	let readOnlyMode = $state(false);
	let composeAttachments = $state('');
	let composeBusy = $state(false);
	let composeProblem = $state<{ code: string; message: string } | null>(null);
	let composeFields = $state<Record<string, string>>({});

	/** On-demand attachments drawer: collapsed to a 1px sr-only shell until used. */
	let attachmentsOpen = $state(false);
	const attachmentsRevealed = $derived(
		attachmentsOpen || composeAttachments.trim() !== '' || Boolean(composeFields.attachments)
	);

	/** Local pre-flight: only the checks with a user-fixable answer here. */
	function composeFieldErrors(): Record<string, string> {
		const errors: Record<string, string> = {};
		if (composeMessage.trim() === '') errors.message = 'A message is required.';
		if (composeProfile.trim() === '') errors.profile = 'A model profile is required.';
		for (const opId of parseNameList(composeAttachments)) {
			if (!UUID_RE.test(opId)) {
				errors.attachments = 'Attachment references must be canonical op ids (UUIDs).';
				break;
			}
		}
		return errors;
	}

	function buildSubmitBody(runId: string): ChatSubmitBody {
		const serverId = generalConsultMode
			? GLOBAL_CONSULT_SERVER_ID
			: (effectiveServerId ?? GLOBAL_CONSULT_SERVER_ID);
		// The browser NEVER stitches prior turns into the prompt (C1/C6):
		// the legacy seam carries ONLY the actual new user text, and the
		// canonical conversation surface assembles history server-side.
		const fullMessage = composeMessage.trim();
		const body: ChatSubmitBody = {
			run_id: runId,
			server_id: serverId,
			message: fullMessage,
			profile: composeProfile.trim(),
			role: SUBMIT_ROLE,
			// Auto-capabilities: the backend resolves every capability granted
			// for the target when `allowed_capabilities` is omitted; the
			// delegated mode runs them autonomously. Read-only strips the
			// mutation surface server-side (the instruction block rides too).
			mode: 'delegated',
			read_only: readOnlyMode,
			attachments: parseNameList(composeAttachments).map((opId) => ({
				server_id: serverId,
				op_id: opId
			}))
		};
		if (display !== null) {
			body.session_run_id = display.run_id;
		}
		// Recorded once, immutable thereafter: `@service` chips ride the submit
		// body ONLY when the draft actually carries them — an empty selection
		// omits the field entirely (the producer treats omission as no tags).
		if (selectedServiceTags.length > 0) body.service_tags = [...selectedServiceTags];
		return body;
	}

	async function submitCompose(): Promise<void> {
		if (api === null || serverScope === null || composeBusy) return;
		composeFields = composeFieldErrors();
		composeProblem = null;
		if (Object.keys(composeFields).length > 0) return;
		const rawUserPrompt = composeMessage.trim();
		if (canonicalApi !== null) {
			await submitCanonicalTurn(rawUserPrompt);
			return;
		}
		const body = buildSubmitBody(crypto.randomUUID());
		composeBusy = true;
		try {
			const created = await api.submitMessage(body);
			if (disposed) return;
			const isFollowUp = display !== null;
			const steeredInPlace =
				display !== null && created.conversation.run_id === display.run_id;
			if (isFollowUp && display !== null) {
				const nextPriorTurns: PriorTurn[] = [
					...priorTurns,
					{
						runId: steeredInPlace
							? `${display.run_id}#turn-${priorTurns.length + 1}`
							: display.run_id,
						submittedAt: display.submitted_at,
						userMessage: messageText,
						modelLabel: resolvedModel ?? display.model_profile,
						thinkingSteps: [...thinkingSteps],
						toolCalls: [...visibleToolCalls],
						reply: latestAssistantReply || streamedReply
					}
				];
				priorTurns = nextPriorTurns;
				storedTurnsByRun.set(created.conversation.run_id, nextPriorTurns);
				saveStoredTurns(storedTurnsByRun);
				if (!steeredInPlace) {
					supersededRunIds.add(display.run_id);
					saveStoredSuperseded(supersededRunIds);
					historyRows = historyRows.filter((row) => row.run_id !== display.run_id);
				}
			} else {
				authoredLabels.clear();
			}
			// This window authored the submission: the message is the root
			// label the tree may render.
			authoredLabels.set(created.conversation.run_id, rawUserPrompt);
			saveStoredAuthored(authoredLabels);
			if (steeredInPlace && display !== null) {
				taskLabels = { ...taskLabels, [display.run_id]: rawUserPrompt };
				stopStreamReveal();
				latestAssistantReply = '';
				streamedReply = '';
				visibleToolCalls = [];
				thinkingSteps = ['Steering active cogitator session…'];
				queueMicrotask(scrollToConversationBottom);
			} else {
				openConversation(created.conversation, isFollowUp);
			}
			composeMessage = '';
			composeAttachments = '';
			void loadHistory();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			composeProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The submission could not be recorded.')
			};
			if (error !== null && typeof error === 'object' && 'fields' in error) {
				const fields = (error as { fields: unknown }).fields;
				if (typeof fields === 'object' && fields !== null) {
					const next: Record<string, string> = {};
					for (const [key, value] of Object.entries(fields as Record<string, unknown>)) {
						if (typeof value === 'string') next[key] = value;
					}
					composeFields = next;
				}
			}
		} finally {
			if (!disposed) composeBusy = false;
		}
	}

	// --- canonical conversation surface (§3; 09-client-continuity; task 10) ----

	/**
	 * Honest legacy-history vocabulary (generated schema, §4): `complete`
	 * needs no banner; `partial`/`unavailable` render the gap banner and
	 * the backend blocks new mutations until scope confirmation — the UI
	 * never fabricates turns to fill a gap (C7).
	 */
	type HistoryCompleteness = components['schemas']['HistoryCompleteness'];
	type LegacyGap = components['schemas']['LegacyHistoryGap'];

	/**
	 * History-row union: legacy run rows unchanged, canonical conversation
	 * rows pinning `run_id` to the conversation id (the stable list
	 * identity the drawer seam selects by) plus canonical-only display
	 * fields. Legacy consumers keep reading the ConversationView part.
	 */
	type ChatHistoryRow = ConversationView & {
		conversation_id?: string;
		message_count?: number;
		canonical?: boolean;
	};

	/** The optional canonical slice, present only when EVERY method exists. */
	type CanonicalChatApi = ChatApi &
		Required<
			Pick<
				ChatApi,
				| 'createConversation'
				| 'submitConversationMessage'
				| 'steerConversation'
				| 'listCanonicalConversations'
				| 'getConversation'
				| 'listConversationMessages'
				| 'listConversationEvents'
			>
		>;

	const canonicalApi = $derived.by<CanonicalChatApi | null>(() => {
		if (api === null) return null;
		for (
			const name of [
				'createConversation',
				'submitConversationMessage',
				'steerConversation',
				'listCanonicalConversations',
				'getConversation',
				'listConversationMessages',
				'listConversationEvents'
			] as const
		) {
			if (typeof api[name] !== 'function') return null;
		}
		return api as CanonicalChatApi;
	});

	/**
	 * §8 Plan section services (act `18-plan-ui`): narrow the task slice off
	 * the SAME composed client the panel already carries — a bag without the
	 * task methods keeps the plan section honestly unbound (fail closed, no
	 * fallback client). Like every panel seam, it consumes `panel.services`
	 * only; no hidden global state.
	 */
	function readTaskServices(services: PanelServices): TaskServices | null {
		const candidate = services.api as Partial<TaskApi> | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (
			const name of [
				'getConversationTask',
				'listTaskSteps',
				'listTaskEvidence',
				'listTaskRevisions',
				'submitTaskCorrection'
			] as const
		) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return taskServices(candidate as TaskApi);
	}
	const taskServicesBag = $derived(readTaskServices(panel.services));

	// Canonical conversation state (single-writer instance locals; no
	// module-level singletons, ADR §5 rule 4).
	let canonicalId = $state<string | null>(null);
	/**
	 * The explicit scope a canonical conversation was opened under: the
	 * draft target at create time, or the history row's recorded `server_id`
	 * at restore time. It rules every canonical read until the server's own
	 * `canonicalDetail.server_id` lands and keeps ruling it if that read is
	 * still in flight (24a D-1: the draft scope, never the stale panel pin).
	 */
	let canonicalActiveScope = $state<string | null>(null);
	let canonicalDetail = $state<CanonicalConversationDetail | null>(null);
	let canonicalMessages = $state<CanonicalTranscriptMessage[]>([]);
	let canonicalTotal = $state(0);
	/** Slice start of the loaded transcript window (ascending sequences). */
	let canonicalOffset = $state(0);
	let canonicalHasMore = $state(false);
	let canonicalLoadingMore = $state(false);
	let canonicalBusy = $state(false);
	let canonicalProblem = $state<{ code: string; message: string } | null>(null);
	let canonicalCompleteness = $state<HistoryCompleteness>('complete');
	let canonicalGaps = $state<LegacyGap[]>([]);
	/** The replay cursor: strictly newer events only (dedup by construction). */
	let canonicalLatestSeq = $state(0);
	let canonicalPendingSteering = $state(0);
	let lastRevision = $state<number | null>(null);
	let lastDisposition = $state<TurnDisposition | null>(null);
	let controlBusy = $state(false);
	let controlNote = $state<string | null>(null);

	const canonicalMode = $derived(canonicalId !== null);

	/**
	 * The newest recorded USER message of the canonical transcript: the
	 * source anchor every plan revision proposal must carry (§8 "always
	 * source-linked"). `null` honestly disables the proposal form.
	 */
	const lastCanonicalUserMessageId = $derived.by<string | null>(() => {
		for (let index = canonicalMessages.length - 1; index >= 0; index -= 1) {
			const message = canonicalMessages[index];
			if (message.role === 'user') return message.message_id;
		}
		return null;
	});

	/**
	 * Honest status text for a canonical conversation: the lease-holding
	 * run's recorded state when bound, otherwise the restore/queued truth —
	 * never a fabricated success (C08 rule 6).
	 */
	const canonicalStateText = $derived.by(() => {
		if (display !== null) return runStateText(display.state);
		if (canonicalBusy) return 'Restoring conversation…';
		if (lastDisposition === 'queued_followup') return 'Follow-up queued';
		return 'Conversation';
	});

	function resetCanonicalView(): void {
		canonicalId = null;
		canonicalActiveScope = null;
		canonicalDetail = null;
		canonicalMessages = [];
		canonicalTotal = 0;
		canonicalOffset = 0;
		canonicalHasMore = false;
		canonicalLoadingMore = false;
		canonicalProblem = null;
		canonicalCompleteness = 'complete';
		canonicalGaps = [];
		canonicalLatestSeq = 0;
		canonicalPendingSteering = 0;
		lastRevision = null;
		lastDisposition = null;
		controlBusy = false;
		// The outcome block belongs to exactly one conversation: a switch
		// clears it (the fold re-derives from the replayed run record, or the
		// block stays honestly absent — never a stale verdict).
		taskOutcome = null;
		controlNote = null;
	}

	/** Canonical row → the drawer-compatible history row (identity pinned). */
	function canonicalHistoryRow(row: CanonicalConversationSummary): ChatHistoryRow {
		return {
			run_id: row.conversation_id,
			server_id: row.server_id,
			parent_run_id: null,
			depth: 0,
			profile_revision: 0,
			prompt_revision: 0,
			model_profile: '',
			requested_grants: [],
			state: null,
			cancel_requested_at: null,
			submitted_at: row.created_at,
			updated_at: row.updated_at,
			title: row.title ?? null,
			conversation_id: row.conversation_id,
			message_count: row.message_count,
			canonical: true
		};
	}

	/**
	 * Structured turn/control failures keep their honest, specific text:
	 * the user learns exactly what was NOT applied (C6/C7 wire honesty).
	 */
	function canonicalTurnErrorText(code: string, fallback: string): string {
		if (code === 'revision_conflict')
			return 'This conversation changed elsewhere; your message was NOT applied. Reopen the conversation and try again.';
		if (code === 'client_request_conflict')
			return 'That idempotency key already carried a different message; nothing was duplicated or overwritten.';
		if (code === 'conversation_busy')
			return 'Another run holds this conversation; steering, pause or cancel are accepted while it executes.';
		if (code === 'history_reconstruction_incomplete')
			return 'Earlier history is incomplete; explicit scope confirmation is required before new mutations. The message was not applied.';
		if (code === 'no_active_run')
			return 'No active run holds this conversation; there is nothing to steer.';
		return friendlyText(code, fallback);
	}

	/**
	 * Bounded history page of the PINNED server from the canonical store
	 * (C01 rules 2/5): safe metadata only — no transcript content ever
	 * rides a list page.
	 */
	async function loadCanonicalHistory(loadMore: boolean): Promise<void> {
		const canonicalRef = canonicalApi;
		// The one canonical scope: Global while the draft is unpinned, the
		// pinned server while no override is set, the recorded scope while a
		// conversation is open (24a D-1).
		const scope = canonicalScope;
		if (canonicalRef === null || scope === null) return;
		if (loadMore) {
			if (historyState.status !== 'ready' || historyLoadingMore || !historyHasMore) return;
		}
		historyState = { status: 'loading' };
		if (!loadMore) {
			historyRows = [];
			historyLoadingMore = false;
		}
		historyProblem = null;
		try {
			const page: CanonicalConversationPage = await canonicalRef.listCanonicalConversations(
				scope,
				{ limit: PAGE_LIMIT, offset: loadMore ? historyOffset : 0 }
			);
			// Scope guard: a slow response scoped to a PREVIOUS draft/open
			// conversation is discarded — it can never overwrite another
			// scope's page (no cross-server leakage).
			if (disposed || scope !== canonicalScope) return;
			const mapped = (page.items ?? []).map(canonicalHistoryRow);
			if (loadMore) {
				const seen = new Set(historyRows.map((row) => row.run_id));
				historyRows = [...historyRows, ...mapped.filter((row) => !seen.has(row.run_id))];
			} else {
				historyRows = mapped;
			}
			historyOffset = page.offset + (page.items ?? []).length;
			historyRawTotal = page.total;
			historyTotal = page.total;
			historyState = { status: 'ready' };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			if (!loadMore) {
				historyRows = [];
				historyTotal = 0;
				historyOffset = 0;
				historyRawTotal = 0;
				historyState = { status: 'error' };
			}
			historyProblem = {
				code,
				message: friendlyText(
					code,
					messageOf(error) ??
						(loadMore
							? 'Additional conversations could not be loaded.'
							: 'The conversation list could not be read.')
				)
			};
		}
	}

	/**
	 * Apply one transcript page as the visible window (ascending order).
	 * The window is the bounded LAST page on restore; the server truth
	 * replaces everything local, so no optimistic residue survives.
	 */
	function applyCanonicalPage(page: CanonicalTranscriptPage, start: number): void {
		canonicalMessages = page.items ?? [];
		canonicalTotal = page.total;
		canonicalOffset = start;
		canonicalHasMore = start > 0;
		canonicalCompleteness = page.history_completeness;
		queueMicrotask(scrollToConversationBottom);
	}

	/** Restore the bounded transcript window (latest PAGE_LIMIT rows). */
	async function loadCanonicalMessages(): Promise<void> {
		const canonicalRef = canonicalApi;
		const id = canonicalId;
		const scope = canonicalScope;
		if (canonicalRef === null || id === null || scope === null) return;
		const head = await canonicalRef.listConversationMessages(id, scope, {
			limit: PAGE_LIMIT,
			offset: 0
		});
		if (disposed || canonicalId !== id || scope !== canonicalScope) return;
		const start = Math.max(0, head.total - PAGE_LIMIT);
		if (start === 0) {
			applyCanonicalPage(head, 0);
			return;
		}
		const tail = await canonicalRef.listConversationMessages(id, scope, {
			limit: PAGE_LIMIT,
			offset: start
		});
		if (disposed || canonicalId !== id || scope !== canonicalScope) return;
		applyCanonicalPage(tail, start);
	}

	/** `Load earlier`: prepend the preceding deduplicated page. */
	async function loadEarlierCanonicalMessages(): Promise<void> {
		const canonicalRef = canonicalApi;
		const id = canonicalId;
		const scope = canonicalScope;
		if (canonicalRef === null || id === null || scope === null) return;
		if (canonicalLoadingMore || !canonicalHasMore) return;
		canonicalLoadingMore = true;
		canonicalProblem = null;
		try {
			const start = Math.max(0, canonicalOffset - PAGE_LIMIT);
			const page = await canonicalRef.listConversationMessages(id, scope, {
				limit: canonicalOffset - start,
				offset: start
			});
			if (disposed || canonicalId !== id || scope !== canonicalScope) return;
			const seen = new Set(canonicalMessages.map((one) => one.message_id));
			const earlier = (page.items ?? []).filter((one) => !seen.has(one.message_id));
			canonicalMessages = [...earlier, ...canonicalMessages];
			canonicalOffset = start;
			canonicalTotal = page.total;
			canonicalCompleteness = page.history_completeness;
			canonicalHasMore = start > 0;
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			canonicalProblem = {
				code,
				message: friendlyText(
					code,
					messageOf(error) ?? 'Earlier messages could not be loaded.'
				)
			};
		} finally {
			if (!disposed) canonicalLoadingMore = false;
		}
	}

	/**
	 * Cursor catch-up (C2/C5/C8 honesty): read the bounded event page
	 * strictly after the last seen conversation sequence. `current` is the
	 * honest state block — completeness, lease-holding run, pending
	 * steering count — and new appends trigger a tail re-read so the
	 * transcript stays one server truth. A newly bound lease starts its
	 * stream; every read carries the pinned server scope.
	 */
	async function refreshCanonicalTail(): Promise<void> {
		const canonicalRef = canonicalApi;
		const id = canonicalId;
		const scope = canonicalScope;
		if (canonicalRef === null || id === null || scope === null) return;
		try {
			const page = await canonicalRef.listConversationEvents(id, scope, {
				afterSeq: canonicalLatestSeq,
				limit: 50
			});
			if (disposed || canonicalId !== id || scope !== canonicalScope) return;
			canonicalLatestSeq = page.current.latest_seq;
			canonicalCompleteness = page.current.history_completeness;
			canonicalPendingSteering = page.current.pending_steering;
			// A bound run's facade state moves independently of its stream
			// (lease released, run finished): the debounced refresh keeps the
			// recorded badge honest without waiting for stream frames.
			if (conversation !== null) scheduleRefresh();
			if (canonicalDetail !== null) {
				canonicalDetail = {
					...canonicalDetail,
					history_completeness: page.current.history_completeness,
					active_run_id: page.current.active_run_id
				};
			}
			if ((page.events ?? []).length > 0 || canonicalMessages.length === 0) {
				await loadCanonicalMessages();
				if (disposed || canonicalId !== id || scope !== canonicalScope) return;
			}
			// Bind the live fold to the lease-holding run (a real chat run:
			// the run facade and the SSE route both serve it).
			const activeRun = page.current.active_run_id;
			if (
				activeRun !== null &&
				activeRun !== '' &&
				(conversation === null || conversation.run_id !== activeRun)
			) {
				try {
					const view = await api?.readConversation(activeRun);
					if (disposed || canonicalId !== id || scope !== canonicalScope) return;
					if (view !== undefined && conversation?.run_id !== activeRun) {
						conversation = view;
						refreshed = view;
						startStream();
					}
				} catch {
					// The run row read failing must not kill the transcript
					// view; the structured problem stays on the next poll.
				}
			}
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			refreshProblem = {
				code,
				message: friendlyText(
					code,
					messageOf(error) ?? 'The conversation state could not be refreshed.'
				)
			};
		}
	}

	/**
	 * The canonical submit (§3; C1/C6): ONLY the raw user message rides the
	 * wire — never a stitched history wrapper. Identity is the client-minted
	 * idempotency key; continuation is the conversation id; `expected_revision`
	 * pins the task revision so a stale writer fails closed (409), and an
	 * active lease surfaces `queued_followup` instead of a second executor.
	 */
	async function submitCanonicalTurn(rawUserPrompt: string): Promise<void> {
		const canonicalRef = canonicalApi;
		const openId = canonicalId;
		if (canonicalRef === null || canonicalScope === null) return;
		const clientRequestId = crypto.randomUUID();
		// A NEW conversation targets the explicit DRAFT choice (the sentinel
		// when the server chip was removed); a FOLLOW-UP targets the open
		// conversation's RECORDED scope (`canonicalScope`) — a draft change
		// never retargets an open conversation (C01 rule 2, 24a D-1).
		const serverId =
			openId === null ? (effectiveServerId ?? GLOBAL_CONSULT_SERVER_ID) : canonicalScope;
		composeBusy = true;
		try {
			const submitted: CanonicalTurnSubmitted =
				openId === null
					? await canonicalRef.createConversation({
							server_id: serverId,
							client_request_id: clientRequestId,
							message: rawUserPrompt,
							profile: composeProfile.trim(),
							requested_phase: readOnlyMode ? 'plan' : null,
							task_action: 'continue'
						})
					: await canonicalRef.submitConversationMessage(openId, {
							server_id: serverId,
							client_request_id: clientRequestId,
							message: rawUserPrompt,
							expected_revision: lastRevision,
							requested_phase: readOnlyMode ? 'plan' : null,
							task_action: 'continue'
						});
			if (disposed) return;
			canonicalId = submitted.conversation_id;
			// Pin the read scope to the target the request actually named
			// BEFORE the first tail/messages/history refresh fires: a Global
			// create polls Global, never the previous panel pin.
			canonicalActiveScope = serverId;
			lastRevision = submitted.revision;
			lastDisposition = submitted.disposition;
			composeMessage = '';
			// The admitted turn is durable before the response: refresh the
			// tail immediately, then the history list (message_count moved).
			await refreshCanonicalTail();
			if (!disposed) void loadHistory();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			composeProblem = {
				code,
				message: canonicalTurnErrorText(
					code,
					messageOf(error) ?? 'The turn could not be recorded.'
				)
			};
			if (error !== null && typeof error === 'object' && 'fields' in error) {
				const fields = (error as { fields: unknown }).fields;
				if (typeof fields === 'object' && fields !== null) {
					const next: Record<string, string> = {};
					for (const [key, value] of Object.entries(fields as Record<string, unknown>)) {
						if (typeof value === 'string') next[key] = value;
					}
					composeFields = next;
				}
			}
		} finally {
			if (!disposed) composeBusy = false;
		}
	}

	/**
	 * Deterministic control rows (§3; C5): steer/pause/cancel are recorded
	 * durably before the caller learns anything and consumed at the run's
	 * next safe effect boundary — never retroactive; in-flight effects are
	 * not claimed stopped until their receipts say so.
	 */
	async function recordControl(action: 'steer' | 'pause' | 'cancel'): Promise<void> {
		const canonicalRef = canonicalApi;
		const id = canonicalId;
		const scope = canonicalScope;
		if (canonicalRef === null || id === null || scope === null || controlBusy) return;
		controlBusy = true;
		controlNote = null;
		try {
			const view = await canonicalRef.steerConversation(id, {
				conversation_id: id,
				server_id: scope,
				action,
				message: null,
				client_request_id: crypto.randomUUID()
			});
			if (disposed) return;
			controlNote =
				view.disposition === 'deduplicated'
					? `The ${view.action} control was already recorded (deduplicated); the run consumes it at its next safe boundary.`
					: `${view.action === 'steer' ? 'Steering' : view.action === 'pause' ? 'Pause' : 'Cancellation'} recorded — the run consumes it at its next safe boundary; in-flight effects are not claimed stopped until their receipts say so.`;
			scheduleRefresh();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			controlNote = canonicalTurnErrorText(
				code,
				messageOf(error) ?? 'The control could not be recorded.'
			);
		} finally {
			if (!disposed) controlBusy = false;
		}
	}

	/** Restore one canonical conversation from a history row (no local cache). */
	async function openCanonicalConversation(row: ChatHistoryRow): Promise<void> {
		const canonicalRef = canonicalApi;
		if (canonicalRef === null) return;
		const conversationId = row.conversation_id ?? row.run_id;
		// 24a D-1: the restore targets the ROW's RECORDED server (the Global
		// sentinel for consult conversations) — never the panel pin, which
		// would 404 a scope-blind read before the recorded scope is known.
		const rowScope = row.server_id;
		if (conversationId === '' || rowScope === '') return;
		finishStream();
		historyOpen = false;
		// A canonical restore shares NO state with the legacy fold.
		priorTurns = [];
		conversation = null;
		refreshed = null;
		needsApproval = false;
		approvalBinding = null;
		approval = null;
		approvalProblem = null;
		approvalBusy = false;
		decisionStatus = null;
		resetFold();
		detailsOpen = false;
		resetCanonicalView();
		canonicalId = conversationId;
		// The row's recorded scope rules the detail read and every read
		// after it until (and after) the server's own detail lands.
		canonicalActiveScope = rowScope;
		// Reopen continuity: the conversation's last server-delivered outcome
		// document (a lease-holding run re-folds it from the SSE replay).
		taskOutcome = taskOutcomeByConversation.get(conversationId) ?? null;
		canonicalBusy = true;
		try {
			const detail = await canonicalRef.getConversation(conversationId, rowScope);
			if (disposed || canonicalId !== conversationId) return;
			canonicalDetail = detail;
			canonicalCompleteness = detail.history_completeness;
			canonicalGaps = detail.legacy_gaps ?? [];
			// The recorded scope locks the context exactly like the legacy
			// restore: an open conversation never retargets (C01 rule 2).
			draftServerOverride =
				detail.server_id === GLOBAL_CONSULT_SERVER_ID ? null : detail.server_id;
			selectedServiceTags = [];
			composeProfile = 'default';
			await loadCanonicalMessages();
			if (disposed || canonicalId !== conversationId) return;
			// A live writer lease resumes its stream; otherwise the bounded
			// events read settles the honest state block.
			await refreshCanonicalTail();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			// 24c D-3: the identity stays OPEN with the structured problem —
			// exactly the messages-load failure path. Wiping it here collapsed
			// the dock to the blank draft and silently hid the fail-closed
			// restore error (wrong scope → 404) from the user.
			canonicalProblem = {
				code,
				message: friendlyText(
					code,
					messageOf(error) ?? 'The conversation could not be restored.'
				)
			};
		} finally {
			if (!disposed) canonicalBusy = false;
		}
	}

	// --- history (bounded page of the pinned server; C01 rules 5 and 7) ------

	let historyRows = $state<ChatHistoryRow[]>([]);
	let historyTotal = $state(0);
	let historyOffset = $state(0);
	let historyRawTotal = $state(0);
	let historyLoadingMore = $state(false);
	let historyState = $state<{ status: 'idle' | 'loading' | 'ready' | 'error' }>({ status: 'idle' });
	let historyProblem = $state<{ code: string; message: string } | null>(null);
	const historyHasMore = $derived(
		historyState.status === 'ready' &&
			historyOffset < historyRawTotal &&
			historyRows.length < historyTotal
	);

	async function loadHistory(): Promise<void> {
		if (api === null || serverScope === null) return;
		if (canonicalApi !== null) {
			await loadCanonicalHistory(false);
			return;
		}
		const scope = serverScope;
		historyState = { status: 'loading' };
		historyLoadingMore = false;
		historyProblem = null;
		try {
			const page: ChatPage = await api.listConversations(scope, { limit: PAGE_LIMIT });
			if (disposed || scope !== serverScope) return;
			for (const row of page.conversations) {
				if (typeof row.session_run_id === 'string' && row.session_run_id !== '') {
					supersededRunIds.add(row.session_run_id);
				}
			}
			const filtered = page.conversations.filter((row) => !supersededRunIds.has(row.run_id));
			const removedCount = page.conversations.length - filtered.length;
			historyRows = filtered;
			historyOffset = page.conversations.length;
			historyRawTotal = page.total;
			historyTotal = Math.max(filtered.length, page.total - removedCount);
			historyState = { status: 'ready' };
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			historyRows = [];
			historyTotal = 0;
			historyOffset = 0;
			historyRawTotal = 0;
			historyState = { status: 'error' };
			historyProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The conversation list could not be read.')
			};
		}
	}

	async function loadMoreHistory(): Promise<void> {
		if (
			api === null ||
			serverScope === null ||
			historyState.status !== 'ready' ||
			historyLoadingMore ||
			!historyHasMore
		) {
			return;
		}
		if (canonicalApi !== null) {
			await loadCanonicalHistory(true);
			return;
		}
		const scope = serverScope;
		const nextOffset = historyOffset;
		historyLoadingMore = true;
		historyProblem = null;
		try {
			const page: ChatPage = await api.listConversations(scope, {
				limit: PAGE_LIMIT,
				offset: nextOffset
			});
			if (disposed || scope !== serverScope) return;
			for (const row of page.conversations) {
				if (typeof row.session_run_id === 'string' && row.session_run_id !== '') {
					supersededRunIds.add(row.session_run_id);
				}
			}
			const seen = new Set(historyRows.map((row) => row.run_id));
			const appended: ConversationView[] = [];
			let skippedSuperseded = 0;
			for (const row of page.conversations) {
				if (supersededRunIds.has(row.run_id)) {
					skippedSuperseded += 1;
					continue;
				}
				if (!seen.has(row.run_id)) {
					seen.add(row.run_id);
					appended.push(row);
				}
			}
			historyRows = [...historyRows, ...appended];
			historyOffset = nextOffset + page.conversations.length;
			if (page.conversations.length === 0) {
				historyRawTotal = historyOffset;
				historyTotal = historyRows.length;
			} else {
				historyRawTotal = page.total;
				historyTotal = Math.max(historyRows.length, page.total - skippedSuperseded);
			}
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			historyProblem = {
				code,
				message: friendlyText(
					code,
					messageOf(error) ?? 'Additional conversations could not be loaded.'
				)
			};
		} finally {
			if (!disposed) historyLoadingMore = false;
		}
	}

	// --- open conversation (submit outcome or a history row) ------------------

	let conversation = $state<ConversationView | null>(null);
	/**
	 * The refreshed replayed root row. `null` until this instance's own
	 * refresh lands; the display root is the opener-held row until then.
	 */
	let refreshed = $state<ConversationView | null>(null);
	const display = $derived(refreshed ?? conversation);
	const activeRunId = $derived(display === null ? '' : display.run_id);

	/**
	 * Immutable-once-started: the moment a conversation is open — submitted
	 * here or restored from history — its recorded server and `@service`
	 * context is the ONLY context; the remove/pick controls leave the DOM and
	 * the chips render `data-locked="true"` until `New Chat` resets the draft.
	 */
	const chatContextLocked = $derived(display !== null || canonicalMode);
	const effectiveServerId = $derived(
		chatContextLocked
			? (canonicalDetail?.server_id ?? display?.server_id ?? null)
			: draftServerOverride === undefined
				? serverScope
				: draftServerOverride
	);
	const generalConsultMode = $derived(
		effectiveServerId === null || effectiveServerId === GLOBAL_CONSULT_SERVER_ID
	);
	/**
	 * THE one canonical scope (24a D-1 repair). Every canonical call —
	 * history list/load-more, detail, transcript pages, event tail, steering
	 * controls and the follow-up turn's `server_id` — reads THIS value, so
	 * the read scope can never drift from the target the conversation
	 * actually belongs to. Derivation, in priority order:
	 *
	 *  1. OPEN conversation (recorded truth): `canonicalDetail.server_id`.
	 *     An opened conversation never retargets (C01 rule 2), and reads
	 *     stay on the server the backend recorded — not on the panel pin
	 *     and not on a draft chip.
	 *  2. CREATED here, detail read still in flight: the scope the create
	 *     request itself named (`canonicalActiveScope`) — for a Global draft
	 *     that is the sentinel, so the first tail/events/messages reads
	 *     already hit Global instead of the previous panel pin.
	 *  3. FRESH DRAFT: the explicit draft choice — the draft override when
	 *     set (`null` ⇒ Global sentinel, id ⇒ that server), else the pinned
	 *     panel scope. Removing the server chip therefore re-scopes the
	 *     history list to Global and a later submit to the same target.
	 */
	const canonicalScope = $derived.by<string | null>(() => {
		if (canonicalDetail !== null) return canonicalDetail.server_id;
		if (canonicalId !== null) return canonicalActiveScope;
		return effectiveServerId ?? GLOBAL_CONSULT_SERVER_ID;
	});
	const rootTerminal = $derived(
		display !== null && display.state !== null && TERMINAL_RUN_STATES.has(display.state)
	);

	/**
	 * Labels THIS window authored, keyed by run id: the root message and the
	 * child tasks linked through the planned event's body-ordered ids. A
	 * conversation opened from history has none — the wire carries no bodies.
	 */
	const AUTHORED_STORAGE_KEY = 'mai.chat.authored.v1';
	const PRIOR_TURNS_STORAGE_KEY = 'mai.chat.prior_turns.v1';
	const SUPERSEDED_STORAGE_KEY = 'mai.chat.superseded.v1';

	function loadStoredAuthored(): Map<string, string> {
		if (typeof window === 'undefined') return new Map();
		try {
			const raw = window.localStorage.getItem(AUTHORED_STORAGE_KEY);
			if (!raw) return new Map();
			const parsed = JSON.parse(raw) as Record<string, unknown>;
			const entries: Array<[string, string]> = [];
			for (const [k, v] of Object.entries(parsed)) {
				if (typeof v === 'string' && v.trim() !== '') entries.push([k, v]);
			}
			return new Map(entries);
		} catch {
			return new Map();
		}
	}

	function saveStoredAuthored(map: Map<string, string>): void {
		if (typeof window === 'undefined') return;
		try {
			const obj = Object.fromEntries(map);
			window.localStorage.setItem(AUTHORED_STORAGE_KEY, JSON.stringify(obj));
		} catch {
			// Ignore storage quota errors.
		}
	}

	function loadStoredTurns(): Map<string, PriorTurn[]> {
		if (typeof window === 'undefined') return new Map();
		try {
			const raw = window.localStorage.getItem(PRIOR_TURNS_STORAGE_KEY);
			if (!raw) return new Map();
			const parsed = JSON.parse(raw) as Record<string, unknown>;
			const entries: Array<[string, PriorTurn[]]> = [];
			for (const [k, v] of Object.entries(parsed)) {
				if (Array.isArray(v)) entries.push([k, v as PriorTurn[]]);
			}
			return new Map(entries);
		} catch {
			return new Map();
		}
	}

	function saveStoredTurns(map: Map<string, PriorTurn[]>): void {
		if (typeof window === 'undefined') return;
		try {
			const obj = Object.fromEntries(map);
			window.localStorage.setItem(PRIOR_TURNS_STORAGE_KEY, JSON.stringify(obj));
		} catch {
			// Ignore storage quota errors.
		}
	}

	function loadStoredSuperseded(): Set<string> {
		if (typeof window === 'undefined') return new Set();
		try {
			const raw = window.localStorage.getItem(SUPERSEDED_STORAGE_KEY);
			if (!raw) return new Set();
			const parsed = JSON.parse(raw);
			if (!Array.isArray(parsed)) return new Set();
			return new Set(parsed.filter((item): item is string => typeof item === 'string'));
		} catch {
			return new Set();
		}
	}

	function saveStoredSuperseded(set: Set<string>): void {
		if (typeof window === 'undefined') return;
		try {
			window.localStorage.setItem(SUPERSEDED_STORAGE_KEY, JSON.stringify([...set]));
		} catch {
			// Ignore storage quota errors.
		}
	}

	let authoredLabels = $state(loadStoredAuthored());
	let storedTurnsByRun = $state(loadStoredTurns());
	let supersededRunIds = $state(loadStoredSuperseded());
	let taskLabels = $state<Record<string, string>>({});

	function openConversation(view: ConversationView, keepPriorTurns = false): void {
		if (!keepPriorTurns) {
			priorTurns = storedTurnsByRun.get(view.run_id) ?? [];
		}
		// A legacy open replaces any canonical view entirely.
		resetCanonicalView();
		// Reopen continuity: the conversation's last server-delivered outcome
		// document (the SSE replay re-folds it when the run record replays).
		taskOutcome = taskOutcomeByConversation.get(view.run_id) ?? null;
		conversation = view;
		refreshed = null;
		taskLabels = Object.fromEntries(authoredLabels);
		needsApproval = false;
		approvalBinding = null;
		approval = null;
		approvalProblem = null;
		approvalBusy = false;
		decisionStatus = null;
		resetFold();
		detailsOpen = false;
		// The recorded conversation RESTORES its frozen context and locks it:
		// the General Consult sentinel restores as the consult badge, any
		// other id restores as the Server Chip, the recorded `@service` tags
		// become (locked) chips, and the recorded model profile refills the
		// composer. A row without recorded tags keeps the current draft tags
		// (the producer records them at submission; older rows predate them).
		draftServerOverride =
			view.server_id === GLOBAL_CONSULT_SERVER_ID ? null : view.server_id;
		if (Array.isArray(view.service_tags)) selectedServiceTags = asStringArray(view.service_tags);
		composeProfile = view.model_profile;
		servicePickerOpen = false;
		serviceQuery = '';
		void loadConversation();
		startStream();
	}

	/**
	 * `+ New Chat`: drop the open conversation, release its stream and reset
	 * the composer to a fresh UNLOCKED draft — the default Server Chip back
	 * on the pinned scope, no `@service` tags, no profile/read-only residue,
	 * history untouched.
	 */
	function newChat(): void {
		finishStream();
		priorTurns = [];
		conversation = null;
		refreshed = null;
		runDetail = null;
		taskLabels = {};
		resetCanonicalView();
		needsApproval = false;
		approvalBinding = null;
		approval = null;
		approvalProblem = null;
		approvalBusy = false;
		decisionStatus = null;
		resetFold();
		detailsOpen = false;
		composeMessage = '';
		composeProfile = 'default';
		readOnlyMode = false;
		composeAttachments = '';
		composeFields = {};
		composeProblem = null;
		draftServerOverride = undefined;
		selectedServiceTags = [];
		servicePickerOpen = false;
		serviceQuery = '';
		historyOpen = false;
	}

	// --- transcript fold (value-free stream facts; hostile text stays text) ---

	type TranscriptEntry = {
		seq: number;
		kind: 'submitted' | 'started' | 'context' | 'round' | 'reply' | 'tool' | 'outcome'
			| 'completed' | 'failed' | 'reconciling';
		label: string;
		rows: Array<{ k: string; v: string }>;
	};

	type VisibleToolCall = {
		id: string;
		runId: string;
		capabilityId: string;
		stepSeq: string;
		payloadHash: string;
		status: 'requested' | 'running' | 'succeeded' | 'failed';
		reason: string;
		opId: string;
	};

	type ToolStepDetail = {
		loading: boolean;
		error: string | null;
		params: unknown;
		response: unknown;
		receipt: Record<string, unknown> | null;
	};

	let expandedToolDetails = $state<Record<string, ToolStepDetail>>({});

	async function loadToolStepDetail(tc: VisibleToolCall): Promise<void> {
		const key = `${tc.runId}:${tc.stepSeq}`;
		const existing = expandedToolDetails[key];
		if (existing && (existing.loading || (existing.error === null && existing.response !== null))) {
			return;
		}
		expandedToolDetails = {
			...expandedToolDetails,
			[key]: {
				loading: true,
				error: null,
				params: existing?.params ?? null,
				response: existing?.response ?? null,
				receipt: existing?.receipt ?? null
			}
		};
		try {
			const res = await fetch(
				`/api/v1/chat/runs/${encodeURIComponent(tc.runId)}/steps/${encodeURIComponent(tc.stepSeq)}`,
				{ credentials: 'same-origin' }
			);
			if (!res.ok) {
				throw new Error(`HTTP ${res.status}`);
			}
			const data = (await res.json()) as Record<string, unknown>;
			expandedToolDetails = {
				...expandedToolDetails,
				[key]: {
					loading: false,
					error: null,
					params: data.params ?? null,
					response: data.response ?? null,
					receipt:
						typeof data.receipt === 'object' && data.receipt !== null
							? (data.receipt as Record<string, unknown>)
							: null
				}
			};
		} catch (err) {
			expandedToolDetails = {
				...expandedToolDetails,
				[key]: {
					loading: false,
					error: err instanceof Error ? err.message : 'Unable to load step details',
					params: null,
					response: null,
					receipt: null
				}
			};
		}
	}

	function formatStepJson(value: unknown): string {
		if (value === null || value === undefined) return '—';
		if (typeof value === 'string') return value;
		try {
			return JSON.stringify(value, null, 2);
		} catch {
			return String(value);
		}
	}

	type PriorTurn = {
		runId: string;
		submittedAt: string;
		userMessage: string;
		modelLabel: string;
		thinkingSteps: string[];
		toolCalls: VisibleToolCall[];
		reply: string;
	};

	let priorTurns = $state<PriorTurn[]>([]);
	let transcript = $state<TranscriptEntry[]>([]);
	let latestAssistantReply = $state<string>('');
	let streamedReply = $state<string>('');
	let isStreamingReply = $state(false);
	let thinkingSteps = $state<string[]>([]);
	let visibleToolCalls = $state<VisibleToolCall[]>([]);
	let conversationScrollEl = $state<HTMLElement | null>(null);
	let streamRevealTimer: ReturnType<typeof setInterval> | null = null;

	function stopStreamReveal(): void {
		if (streamRevealTimer !== null) {
			clearInterval(streamRevealTimer);
			streamRevealTimer = null;
		}
		isStreamingReply = false;
	}

	function startStreamReveal(fullText: string): void {
		stopStreamReveal();
		if (fullText === '') {
			streamedReply = '';
			return;
		}
		// Stream chunks progressively for a responsive cogitator readout feel
		const totalLen = fullText.length;
		const stepChars = Math.max(12, Math.ceil(totalLen / 18));
		let pos = Math.min(stepChars, totalLen);
		streamedReply = fullText.slice(0, pos);
		if (pos >= totalLen) {
			scrollToConversationBottom();
			return;
		}
		isStreamingReply = true;
		streamRevealTimer = setInterval(() => {
			pos = Math.min(pos + stepChars, totalLen);
			streamedReply = fullText.slice(0, pos);
			scrollToConversationBottom();
			if (pos >= totalLen) {
				stopStreamReveal();
			}
		}, 28);
	}

	function addThinkingStep(text: string): void {
		const cleaned = text.trim();
		if (!cleaned) return;
		for (const line of cleaned.split('\n')) {
			const trimmed = line.trim();
			if (trimmed && !thinkingSteps.includes(trimmed)) {
				thinkingSteps = [...thinkingSteps, trimmed];
			}
		}
		queueMicrotask(scrollToConversationBottom);
	}
	/** Watermark of the applied stream events (C08 rule 7 dedup). */
	let lastSeq = $state(0);
	let appliedEvents = $state(0);
	let streamStatus = $state<'off' | 'connecting' | 'reconnecting' | 'closed'>('off');
	let streamProblem = $state<string | null>(null);
	let refreshProblem = $state<{ code: string; message: string } | null>(null);

	/** Non-reactive controller locals; the reactive mirrors drive the view only. */
	let streamAbort: AbortController | null = null;
	let refreshTimer: ReturnType<typeof setTimeout> | null = null;
	/** Set once the mount cycle ended; async continuations check before writes. */
	let disposed = true;

	function scrollToConversationBottom(): void {
		if (conversationScrollEl) {
			conversationScrollEl.scrollTop = conversationScrollEl.scrollHeight;
		}
	}

	function pushEntry(entry: TranscriptEntry): void {
		if (transcript.length >= MAX_RENDERED_EVENTS) transcript = transcript.slice(1);
		transcript = [...transcript, entry];
		queueMicrotask(scrollToConversationBottom);
	}

	function rowOf(data: unknown, key: string): string {
		const value = recordOf(data)?.[key];
		if (typeof value === 'string') return value;
		if (typeof value === 'number') return String(value);
		return '—';
	}

	function foldEvent(event: MaiRunEvent): void {
		const data = event.data;
		switch (event.type) {
			case 'chat.submitted': {
				const record = recordOf(data);
				const attachments = Array.isArray(record?.attachments) ? record.attachments : [];
				pushEntry({
					seq: event.seq,
					kind: 'submitted',
					label: 'Submission recorded',
					rows: [
						{ k: 'handler', v: rowOf(data, 'handler') },
						{ k: 'children', v: rowOf(data, 'children') },
						{ k: 'attachments', v: String(attachments.length) },
						{ k: 'submission_sha256', v: shortHash(rowOf(data, 'submission_sha256')) }
					]
				});
				break;
			}
			case 'chat.steered': {
				addThinkingStep(
					`Steering directive queued for next cogitator round (${rowOf(data, 'message_chars')} chars)`
				);
				pushEntry({
					seq: event.seq,
					kind: 'submitted',
					label: 'Steering directive recorded',
					rows: [
						{ k: 'steer_id', v: shortId(rowOf(data, 'steer_id')) },
						{ k: 'message_chars', v: rowOf(data, 'message_chars') },
						{ k: 'submission_sha256', v: shortHash(rowOf(data, 'submission_sha256')) }
					]
				});
				break;
			}
			case 'agent.started': {
				addThinkingStep(
					`Binding model ${rowOf(data, 'model')} (profile: ${rowOf(data, 'profile')}, connection: ${rowOf(data, 'connection')})`
				);
				pushEntry({
					seq: event.seq,
					kind: 'started',
					label: 'Model bound',
					rows: [
						{ k: 'model', v: rowOf(data, 'model') },
						{ k: 'profile', v: rowOf(data, 'profile') },
						{ k: 'connection', v: rowOf(data, 'connection') },
						{ k: 'role', v: rowOf(data, 'role') },
						{ k: 'rounds_budget', v: rowOf(data, 'rounds_budget') }
					]
				});
				break;
			}
			case 'agent.context_assembled': {
				const record = recordOf(data);
				addThinkingStep(
					`Assembled server context (prompt edition ${rowOf(data, 'prompt_revision')}, evidence items: ${rowOf(data, 'evidence_items')})`
				);
				pushEntry({
					seq: event.seq,
					kind: 'context',
					label: 'Context assembled',
					rows: [
						{ k: 'evidence_items', v: rowOf(data, 'evidence_items') },
						{ k: 'truncated', v: record?.truncated === true ? 'yes' : 'no' },
						{ k: 'prompt_revision', v: rowOf(data, 'prompt_revision') }
					]
				});
				break;
			}
			case 'agent.model_round': {
				const record = recordOf(data);
				const round = rowOf(data, 'round');
				if (typeof record?.thinking === 'string' && record.thinking.trim() !== '') {
					addThinkingStep(record.thinking);
				}
				if (record?.status === 'tool_calls') {
					const calls = Array.isArray(record.tool_calls) ? record.tool_calls : [];
					for (let idx = 0; idx < calls.length; idx++) {
						const one = recordOf(calls[idx]);
						const capId =
							typeof one?.capability_id === 'string' ? one.capability_id : `call_${idx}`;
						const seqStr = String(one?.seq ?? event.seq);
						const hashStr = shortHash(one?.payload_hash);
						addThinkingStep(`Round ${round}: requesting tool ${capId} (step #${seqStr})`);
						visibleToolCalls = [
							...visibleToolCalls,
							{
								id: `${event.seq}-${idx}`,
								runId: event.run_id ?? activeRunId ?? '',
								capabilityId: capId,
								stepSeq: seqStr,
								payloadHash: hashStr,
								status: 'requested',
								reason: 'dispatched',
								opId: '—'
							}
						];
					}
					pushEntry({
						seq: event.seq,
						kind: 'round',
						label: `Round ${round} — tool calls requested`,
						rows: calls.map((call, index) => {
							const one = recordOf(call);
							return {
								k: typeof one?.capability_id === 'string' ? one.capability_id : `call ${index}`,
								v: `seq ${String(one?.seq ?? '—')} · payload ${shortHash(one?.payload_hash)}`
							};
						})
					});
				} else if (record?.status === 'final') {
					const replyOutput = typeof record.output === 'string' ? record.output.trim() : '';
					addThinkingStep(`Round ${round}: synthesized final response (${rowOf(data, 'output_chars')} chars)`);
					if (replyOutput !== '') {
						latestAssistantReply = replyOutput;
						startStreamReveal(replyOutput);
					}
					pushEntry({
						seq: event.seq,
						kind: 'reply',
						label: `Round ${round} — final answer recorded`,
						rows: [
							{
								k: 'output_chars',
								v: rowOf(data, 'output_chars')
							},
							{
								k: 'body',
								v:
									replyOutput !== ''
										? replyOutput
										: 'held in the durable run history — the wire carries no reply text'
							}
						]
					});
				}
				break;
			}
			case 'step.started': {
				const capId = rowOf(data, 'capability_id');
				addThinkingStep(`Executing tool capability ${capId}…`);
				const nextCalls = [...visibleToolCalls];
				const matchIdx = nextCalls.findIndex(
					(c) => c.capabilityId === capId && c.status === 'requested'
				);
				if (matchIdx >= 0) {
					nextCalls[matchIdx] = { ...nextCalls[matchIdx], status: 'running' };
					visibleToolCalls = nextCalls;
				} else if (
					priorTurns.length > 0 &&
					priorTurns[priorTurns.length - 1].toolCalls.some(
						(c) => c.capabilityId === capId && c.status === 'requested'
					)
				) {
					const lastIdx = priorTurns.length - 1;
					const updatedTools = [...priorTurns[lastIdx].toolCalls];
					const priorMatch = updatedTools.findIndex(
						(c) => c.capabilityId === capId && c.status === 'requested'
					);
					if (priorMatch >= 0) {
						updatedTools[priorMatch] = { ...updatedTools[priorMatch], status: 'running' };
						const nextPrior = [...priorTurns];
						nextPrior[lastIdx] = { ...nextPrior[lastIdx], toolCalls: updatedTools };
						priorTurns = nextPrior;
					}
				} else {
					visibleToolCalls = [
						...visibleToolCalls,
						{
							id: `step-${event.seq}`,
							runId: event.run_id ?? activeRunId ?? '',
							capabilityId: capId,
							stepSeq: String(event.seq),
							payloadHash: '—',
							status: 'running',
							reason: 'running',
							opId: '—'
						}
					];
				}
				pushEntry({
					seq: event.seq,
					kind: 'tool',
					label: 'Tool attempt started',
					rows: [{ k: 'capability_id', v: capId }]
				});
				break;
			}
			case 'step.completed': {
				const capId = rowOf(data, 'capability_id');
				const reason = rowOf(data, 'reason');
				const opId = rowOf(data, 'op_id');
				if (reason.startsWith('approval')) needsApproval = true;
				const isOk =
					reason === '—' ||
					reason === '' ||
					reason === 'ok' ||
					reason === 'succeeded' ||
					reason === 'completed';
				const displayReason = reason === '—' || reason === '' ? 'succeeded' : reason;
				addThinkingStep(`Tool ${capId} finished (${displayReason})`);
				const nextCalls = [...visibleToolCalls];
				const matchIdx = nextCalls.findIndex(
					(c) =>
						c.capabilityId === capId &&
						(c.status === 'running' || c.status === 'requested')
				);
				if (matchIdx >= 0) {
					nextCalls[matchIdx] = {
						...nextCalls[matchIdx],
						status: isOk ? 'succeeded' : 'failed',
						reason: displayReason,
						opId: shortId(opId)
					};
					visibleToolCalls = nextCalls;
				} else if (
					priorTurns.length > 0 &&
					priorTurns[priorTurns.length - 1].toolCalls.some(
						(c) =>
							c.capabilityId === capId &&
							(c.status === 'running' || c.status === 'requested')
					)
				) {
					const lastIdx = priorTurns.length - 1;
					const updatedTools = [...priorTurns[lastIdx].toolCalls];
					const priorMatch = updatedTools.findIndex(
						(c) =>
							c.capabilityId === capId &&
							(c.status === 'running' || c.status === 'requested')
					);
					if (priorMatch >= 0) {
						updatedTools[priorMatch] = {
							...updatedTools[priorMatch],
							status: isOk ? 'succeeded' : 'failed',
							reason: displayReason,
							opId: shortId(opId)
						};
						const nextPrior = [...priorTurns];
						nextPrior[lastIdx] = { ...nextPrior[lastIdx], toolCalls: updatedTools };
						priorTurns = nextPrior;
					}
				} else {
					visibleToolCalls = [
						...visibleToolCalls,
						{
							id: `outcome-${event.seq}`,
							runId: event.run_id ?? activeRunId ?? '',
							capabilityId: capId,
							stepSeq: String(event.seq),
							payloadHash: '—',
							status: isOk ? 'succeeded' : 'failed',
							reason: displayReason,
							opId: shortId(opId)
						}
					];
				}
				pushEntry({
					seq: event.seq,
					kind: 'outcome',
					label: 'Tool outcome recorded',
					rows: [
						{ k: 'capability_id', v: capId },
						{ k: 'reason', v: reason },
						{ k: 'op_id', v: opId }
					]
				});
				break;
			}
			case 'step.awaiting_approval': {
				needsApproval = true;
				break;
			}
			case 'agent.awaiting_reconciliation': {
				pushEntry({
					seq: event.seq,
					kind: 'reconciling',
					label: 'Outcome unknown — reconciliation pending',
					rows: [{ k: 'deadline', v: rowOf(data, 'deadline') }]
				});
				break;
			}
			case 'agent.completed': {
				pushEntry({
					seq: event.seq,
					kind: 'completed',
					label: 'Conversation completed',
					rows: [
						{ k: 'rounds', v: rowOf(data, 'rounds') },
						{ k: 'tool_calls', v: rowOf(data, 'tool_calls') },
						{ k: 'output_chars', v: rowOf(data, 'output_chars') }
					]
				});
				// 21-outcome-ui: the run summary's additive `task_outcome`
				// document (absent for taskless runs). Fail-closed narrow —
				// the outcome block is driven by THIS document, never by the
				// run's technical success state (E1).
				const outcomeDocument = readTaskOutcome(data);
				if (outcomeDocument !== null) {
					taskOutcome = outcomeDocument;
					taskOutcomeByConversation.set(event.run_id, outcomeDocument);
					if (canonicalId !== null) {
						taskOutcomeByConversation.set(canonicalId, outcomeDocument);
					}
				}
				break;
			}
			case 'agent.failed': {
				const failReason = rowOf(data, 'reason');
				if (visibleToolCalls.some((c) => c.status === 'requested' || c.status === 'running')) {
					visibleToolCalls = visibleToolCalls.map((c) =>
						c.status === 'requested' || c.status === 'running'
							? {
									...c,
									status: 'failed',
									reason: `aborted (${failReason !== '—' ? failReason : 'run failed'})`
								}
							: c
					);
				}
				pushEntry({
					seq: event.seq,
					kind: 'failed',
					label: 'Conversation failed',
					rows: [{ k: 'reason', v: failReason }]
				});
				break;
			}
			case 'agent.delegation_planned': {
				// The manual child-task form is gone: this panel no longer
				// submits children, so there are no opener-authored labels
				// to bind (a history-opened conversation renders ids only).
				break;
			}
			default:
				// Delegation/budget events are the tree's fold; run.transition
				// and run.cancel_requested reflect through the durable refresh.
				break;
		}
	}

	// --- durable refresh (replayed facade state; never an engine call) --------

	function scheduleRefresh(): void {
		if (refreshTimer !== null || disposed) return;
		refreshTimer = setTimeout(() => {
			refreshTimer = null;
			void loadConversation();
		}, REFRESH_DEBOUNCE_MS);
	}

	async function loadConversation(): Promise<void> {
		if (api === null) return;
		const id = activeRunId;
		if (id === '') return;
		try {
			const view = await api.readConversation(id);
			if (disposed || id !== activeRunId) return;
			refreshed = view;
			refreshProblem = null;
			if (
				TERMINAL_RUN_STATES.has(view.state ?? '') &&
				view.state !== 'succeeded' &&
				visibleToolCalls.some((c) => c.status === 'requested' || c.status === 'running')
			) {
				visibleToolCalls = visibleToolCalls.map((c) =>
					c.status === 'requested' || c.status === 'running'
						? {
								...c,
								status: 'failed',
								reason: view.state === 'cancelled' ? 'cancelled' : 'aborted'
							}
						: c
				);
			}
			if (view.state === 'waiting_approval') needsApproval = true;
			if (needsApproval && !TERMINAL_RUN_STATES.has(view.state ?? '')) void probeApproval();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			refreshProblem = {
				code,
				message: friendlyText(
					code,
					messageOf(error) ?? 'The conversation state could not be refreshed.'
				)
			};
		}
	}

	// --- approval path (receipts → binding → one-use decision; C03 rule 4) ----

	type ApprovalBinding = {
		approvalId: string;
		capabilityId: string;
		payloadHash: string;
		policyRevision: number;
	};

	let needsApproval = $state(false);
	let runDetail = $state<RunDetail | null>(null);
	/**
	 * The value-free task outcome document (act `21-outcome-ui`), folded from
	 * the run summary's additive `task_outcome` key (`TASK_OUTCOME_SUMMARY_KEY`,
	 * `mai.agents.task_completion`) as projected onto the run's event stream.
	 * `null` = absent or malformed ⇒ NO outcome block — never an invented
	 * verdict, and never the run's technical success state (C08 rule 6; E1).
	 */
	let taskOutcome = $state<TaskOutcomeDocument | null>(null);
	/**
	 * Presentation continuity for the outcome block: the SERVER-DELIVERED
	 * documents (folded from the run summary) keyed by conversation identity.
	 * Instance-scoped memory only — never web storage, never invented: a
	 * conversation whose document was never delivered renders no block.
	 */
	const taskOutcomeByConversation = new Map<string, TaskOutcomeDocument>();
	let approvalBinding = $state<ApprovalBinding | null>(null);
	let approval = $state<ApprovalView | null>(null);
	let approvalProblem = $state<{ code: string; message: string } | null>(null);
	let approvalBusy = $state(false);
	let decisionStatus = $state<string | null>(null);

	/**
	 * The bound decision of THIS run's most recent receipt. Only well-formed
	 * value-free handles qualify (UUID approval id, SHA-256 payload hash,
	 * positive revision, non-empty capability) — anything else is absence,
	 * never a guess (C08 rule 6).
	 */
	function approvalBindingOf(detail: RunDetail): ApprovalBinding | null {
		let found: ApprovalBinding | null = null;
		for (const step of detail.steps) {
			const receipt = recordOf(step.receipt);
			if (receipt === null) continue;
			const approvalId = receipt.approval_id;
			const payloadHash = receipt.payload_hash;
			const policyRevision = receipt.policy_revision;
			const capabilityId = receipt.capability_id;
			if (
				typeof approvalId !== 'string' ||
				!UUID_RE.test(approvalId) ||
				typeof payloadHash !== 'string' ||
				!SHA256_RE.test(payloadHash) ||
				typeof policyRevision !== 'number' ||
				!Number.isInteger(policyRevision) ||
				policyRevision < 1 ||
				typeof capabilityId !== 'string' ||
				capabilityId === ''
			) {
				continue;
			}
			found = {
				approvalId,
				capabilityId,
				payloadHash,
				policyRevision
			};
		}
		return found;
	}

	async function probeApproval(): Promise<void> {
		if (api === null) return;
		const id = activeRunId;
		if (id === '') return;
		try {
			const detail = await api.readRun(id);
			if (disposed || id !== activeRunId) return;
			runDetail = detail;
			const binding = approvalBindingOf(detail);
			if (binding === null) {
				approvalBinding = null;
				approval = null;
				return;
			}
			const view = await api.readApproval(binding.approvalId);
			if (disposed || id !== activeRunId) return;
			if (binding.approvalId !== approvalBinding?.approvalId) approvalProblem = null;
			approvalBinding = binding;
			approval = view;
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			approvalBinding = null;
			approval = null;
			approvalProblem = {
				code,
				message:
					code === 'not_found'
						? 'No bound decision record exists for this run.'
						: friendlyText(code, messageOf(error) ?? 'The bound decision could not be loaded.')
			};
		}
	}

	async function recordDecision(): Promise<void> {
		const binding = approvalBinding;
		const view = approval;
		if (api === null || binding === null || view === null || approvalBusy) return;
		if (!approvalPending || display === null || Date.parse(view.expires_at) <= Date.now()) return;
		const body: DecisionBody = {
			capability_id: binding.capabilityId,
			server_id: display.server_id,
			payload_hash: binding.payloadHash,
			policy_revision: binding.policyRevision
		};
		approvalBusy = true;
		approvalProblem = null;
		decisionStatus = null;
		try {
			const recorded = await api.decideApproval(binding.approvalId, body);
			if (disposed) return;
			approval = recorded;
			decisionStatus =
				'Decision recorded — the one-use approval is consumed; the bound action ' +
				'can run when the conversation next requests it.';
			await loadConversation();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			const message =
				code === 'already_consumed'
					? 'The decision was already consumed; a new approval is required.'
					: code === 'expired'
						? 'The approval expired unconsumed; request the action again.'
						: code === 'payload_mismatch' || code === 'revision_mismatch'
							? 'The bound operation changed since the approval was created; a new approval is required.'
							: friendlyText(code, messageOf(error) ?? 'The decision could not be recorded.');
			approvalProblem = { code, message };
			void probeApproval();
		} finally {
			if (!disposed) approvalBusy = false;
		}
	}

	// --- scoped stream controller (watermark dedup; C08 rule 7) ---------------

	function startStream(): void {
		finishStream();
		if (activeRunId === '') return;
		const controller = new AbortController();
		streamAbort = controller;
		streamStatus = 'connecting';
		void streamLoop(activeRunId, controller);
	}

	function finishStream(): void {
		streamAbort?.abort();
		streamAbort = null;
		streamStatus = 'off';
	}

	function resetFold(): void {
		stopStreamReveal();
		transcript = [];
		latestAssistantReply = '';
		streamedReply = '';
		thinkingSteps = [];
		visibleToolCalls = [];
		lastSeq = 0;
		appliedEvents = 0;
		streamProblem = null;
		refreshProblem = null;
	}

	function onComposerKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
			event.preventDefault();
			if (!composeBusy && composeMessage.trim() !== '') {
				void submitCompose();
			}
		}
	}

	/** Compact auto-grow: one row tall, capped at 7.5rem, reset when cleared. */
	function autoGrowComposer(): void {
		const el = dockComposerEl;
		if (el === null) return;
		if (el.value === '') {
			el.style.height = '';
			return;
		}
		el.style.height = 'auto';
		el.style.height = `${Math.min(el.scrollHeight, COMPOSER_MAX_HEIGHT_PX)}px`;
	}

	/* Programmatic clears (submit, New Chat) fire no input event: reset here. */
	$effect(() => {
		if (composeMessage === '') dockComposerEl?.style.setProperty('height', '');
	});

	async function streamLoop(id: string, controller: AbortController): Promise<void> {
		if (api === null) return;
		let passCount = 0;
		while (!disposed && id === activeRunId && !controller.signal.aborted) {
			if (rootTerminal && passCount > 0) break;
			passCount += 1;
			try {
				await api.streamConversationEvents(id, {
					cursor: lastSeq > 0 ? String(lastSeq) : null,
					signal: controller.signal,
					onEvent: (frame) => {
						if (disposed || id !== activeRunId) return;
						const event = readMaiEvent(frame);
						// Watermark dedup (C08 rule 7): replayed or re-delivered
						// frames at/below the last seen seq are dropped here, and a
						// foreign run's frame never touches this transcript.
						if (event === null || event.run_id !== id || event.seq <= lastSeq) return;
						lastSeq = event.seq;
						foldEvent(event);
						appliedEvents += 1;
						streamProblem = null;
						scheduleRefresh();
					}
				});
			} catch (error) {
				if (disposed || id !== activeRunId || controller.signal.aborted) return;
				if (codeOf(error) === 'not_found') {
					streamProblem = 'No conversation is stored under this id; the stream stays closed.';
					finishStream();
					streamStatus = 'closed';
					return;
				}
				streamProblem = 'The event stream was interrupted; reconnecting from the last event.';
			}
			if (disposed || id !== activeRunId || controller.signal.aborted) return;
			streamStatus = 'reconnecting';
			await sleep(BACKOFF_MS, controller.signal);
		}
		if (id === activeRunId && !controller.signal.aborted) {
			finishStream();
			streamStatus = 'closed';
		}
	}

	// --- mount cycle (the $effect teardown is the release path) ---------------

	$effect(() => {
		if (!usable) return;
		disposed = false;
		void loadHistory();
		return () => {
			disposed = true;
			stopStreamReveal();
			finishStream();
			if (refreshTimer !== null) {
				clearTimeout(refreshTimer);
				refreshTimer = null;
			}
		};
	});

	/**
	 * The Service Map seam: `window` dispatches `mai:chat-focus-service` with
	 * `{ serverId, serviceName | serviceTag, category }` so clicking
	 * `[Chat @service]` pre-populates a NEW draft chat. A locked (started)
	 * session never mutates: the event is ignored until `New Chat`.
	 */
	$effect(() => {
		if (typeof window === 'undefined') return;
		window.addEventListener('mai:chat-focus-service', handleFocusService);
		return () => window.removeEventListener('mai:chat-focus-service', handleFocusService);
	});

	onDestroy(() => {
		disposed = true;
		finishStream();
		if (refreshTimer !== null) {
			clearTimeout(refreshTimer);
			refreshTimer = null;
		}
	});

	// --- derived view bits -----------------------------------------------------

	const cancelRequested = $derived(display !== null && display.cancel_requested_at !== null);
	const attachments = $derived(display?.attachments ?? []);
	const messageText = $derived(
		display === null
			? ''
			: (taskLabels[display.run_id] ??
				'Authored outside this window — the wire carries no task bodies.')
	);
	const grantsText = $derived(
		display === null ? '' : display.requested_grants.length === 0 ? 'none' : display.requested_grants.join(', ')
	);
	const showApprovalSection = $derived(needsApproval && display !== null);
	let approvalExpired = $state(true);
	$effect(() => {
		const expiry = Date.parse(approval?.expires_at ?? '');
		let timer: ReturnType<typeof setTimeout> | undefined;
		function checkExpiry(): void {
			const remaining = expiry - Date.now();
			approvalExpired = !Number.isFinite(remaining) || remaining <= 0;
			if (remaining > 0) timer = setTimeout(checkExpiry, Math.min(remaining, 2_147_483_647));
		}
		checkExpiry();
		return () => clearTimeout(timer);
	});
	// The receipt and loaded decision must name exactly the same operation.
	const approvalPending = $derived(
		!rootTerminal && !approvalExpired &&
		!['already_consumed', 'expired', 'payload_mismatch', 'revision_mismatch'].includes(approvalProblem?.code ?? '') &&
		display !== null && approvalBinding !== null && approval !== null &&
		approval.status === 'pending' &&
		approval.approval_id === approvalBinding.approvalId &&
		approval.capability_id === approvalBinding.capabilityId &&
		approval.server_id === display.server_id &&
		approval.payload_hash === approvalBinding.payloadHash &&
		approval.policy_revision === approvalBinding.policyRevision
	);
	const approvalWarning = $derived(
		!rootTerminal && (approvalProblem !== null ||
			(display?.state === 'waiting_approval' && !approvalPending && approval?.status !== 'consumed'))
	);
	const resolvedModel = $derived(
		transcript.find((entry) => entry.kind === 'started')?.rows.find((row) => row.k === 'model')?.v
	);
	const jobInProgress = $derived(display !== null && !rootTerminal);
	let elapsedSeconds = $state(0);
	$effect(() => {
		if (!jobInProgress || display === null) {
			elapsedSeconds = 0;
			return;
		}
		const submittedMs = Date.parse(display.submitted_at);
		const origin = Number.isFinite(submittedMs) ? submittedMs : Date.now();
		function tickElapsed(): void {
			elapsedSeconds = Math.max(0, Math.floor((Date.now() - origin) / 1000));
		}
		tickElapsed();
		const interval = setInterval(tickElapsed, 1000);
		return () => clearInterval(interval);
	});
	const activeStepText = $derived.by(() => {
		if (!jobInProgress) return '';
		const activeTool = [...visibleToolCalls]
			.reverse()
			.find((c) => c.status === 'running' || c.status === 'requested');
		if (activeTool) {
			return activeTool.status === 'running'
				? `Executing tool capability ${activeTool.capabilityId}…`
				: `Dispatching tool capability ${activeTool.capabilityId}…`;
		}
		if (thinkingSteps.length > 0) {
			const lastStep = thinkingSteps[thinkingSteps.length - 1] ?? '';
			if (lastStep.includes('finished')) return 'Synthesizing next cogitator round…';
			return lastStep;
		}
		return 'Initializing cogitator & assembling server context…';
	});
	const isDocked = $derived(panel.descriptor?.window_id === 'mai-bottom-chat-dock');
	let detailsOpen = $state(false);
	let stoppingRun = $state(false);
	const canStopRun = $derived(
		api !== null && display !== null && jobInProgress && !cancelRequested && !stoppingRun
	);

	async function stopActiveConversation(): Promise<void> {
		if (api === null || display === null || !jobInProgress || cancelRequested || stoppingRun) return;
		if (canonicalMode) {
			// The canonical control plane: a deterministic cancel row recorded
			// durably (steerConversation), never the legacy tree-cancel body.
			stoppingRun = true;
			try {
				await recordControl('cancel');
			} finally {
				if (!disposed) stoppingRun = false;
			}
			return;
		}
		stoppingRun = true;
		try {
			await api.cancelConversation(display.run_id, { cancel_children: true });
			if (disposed) return;
			await loadConversation();
		} catch (error) {
			if (disposed) return;
			const code = codeOf(error) ?? 'unavailable';
			refreshProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The cancellation could not be recorded.')
			};
		} finally {
			if (!disposed) stoppingRun = false;
		}
	}

	$effect(() => {
		onhistorychange?.(historyRows, {
			total: historyTotal,
			hasMore: historyHasMore,
			loadingMore: historyLoadingMore
		});
	});

	$effect(() => {
		// The §3 turn body carries no @service tags and no attachments: a
		// panel running the canonical surface never shows those draft
		// controls, so any residue would only mislead — cleared on sight
		// (wire honesty, C08 rule 6).
		if (canonicalApi !== null) {
			selectedServiceTags = [];
			composeAttachments = '';
			attachmentsOpen = false;
		}
	});

	$effect(() => {
		// The drawer's active marker: the canonical conversation identity
		// when one is open (history rows pin run_id to it), else the run id.
		onactivechange?.(canonicalId ?? (activeRunId || null));
	});

	/** Bounded canonical catch-up cadence while a conversation is open. */
	const CANONICAL_POLL_MS = 2000;

	$effect(() => {
		if (canonicalApi === null || canonicalId === null) return;
		const timer = setInterval(() => {
			if (disposed) return;
			void refreshCanonicalTail();
		}, CANONICAL_POLL_MS);
		return () => clearInterval(timer);
	});

	$effect(() => {
		// Canonical terminal hand-over: the assistant reply becomes a
		// transcript row (server truth), so the live fold releases its text
		// and one catch-up read pulls the produced rows in — no duplicated
		// reply, no invented text (C08 rule 6).
		if (!canonicalMode || !rootTerminal) return;
		stopStreamReveal();
		streamedReply = '';
		thinkingSteps = [];
		visibleToolCalls = [];
		void refreshCanonicalTail();
	});


	export function triggerNewChat(): void {
		newChat();
	}

	export function triggerSelectRun(runId: string): void {
		const row = historyRows.find((one) => one.run_id === runId);
		if (row) selectHistoryRow(row);
	}

	export function triggerRefreshHistory(): void {
		void loadHistory();
	}

	export function triggerLoadMoreHistory(): void {
		void loadMoreHistory();
	}

	export async function triggerRenameRun(runId: string, title: string): Promise<void> {
		const trimmed = title.trim();
		if (!trimmed) return;
		let updated: ConversationView | null = null;
		if (typeof api?.renameConversation === 'function') {
			updated = await api.renameConversation(runId, { title: trimmed });
		}
		historyRows = historyRows.map((row) =>
			row.run_id === runId ? (updated ?? { ...row, title: trimmed }) : row
		);
		if (conversation !== null && conversation.run_id === runId) {
			conversation = updated ?? { ...conversation, title: trimmed };
		}
		if (refreshed !== null && refreshed.run_id === runId) {
			refreshed = updated ?? { ...refreshed, title: trimmed };
		}
	}
</script>

<div
	class="mai-chat"
	class:mai-chat--docked={isDocked}
	class:mai-chat--dock-collapsed={isDocked && dockCollapsed}
>
	{#if !usable}
		<div class="mai-chat__state mai-chat__state--error" role="alert" data-testid="panel-config-error">
			<p>This panel was opened without its conversation service or outside a pinned server scope.</p>
		</div>
	{:else}
		{#snippet historyList()}
		<section class="mai-chat__section" aria-label="Conversations" data-testid="chat-history">
			<div class="mai-chat__section-head">
				<h4>Conversations</h4>
				<Button variant="secondary" onclick={loadHistory} data-testid="history-refresh">
					Refresh
				</Button>
			</div>
			{#if historyState.status === 'loading'}
				<div class="mai-chat__state" role="status" data-testid="history-loading">
					<p>Loading conversations…</p>
				</div>
			{:else if historyState.status === 'error'}
				<div
					class="mai-chat__state mai-chat__state--error"
					role="alert"
					data-testid="history-error"
					data-error-code={historyProblem?.code}
				>
					<p>{historyProblem?.message}</p>
					<Button variant="secondary" onclick={loadHistory} data-testid="history-retry">
						Retry
					</Button>
				</div>
			{:else if historyRows.length === 0}
				<div class="mai-chat__state" role="status" data-testid="history-empty">
					<p>No conversations are recorded for this server.</p>
				</div>
			{:else}
				<ul class="mai-chat__rows" data-testid="history-list">
					{#each historyRows as row (row.run_id)}
						<li class="mai-chat__row" data-testid="history-row" data-run-id={row.run_id}>
							<button type="button" class="mai-chat__history-item" data-testid="chat-history-item" onclick={() => selectHistoryRow(row)}>
							<div class="mai-chat__row-main">
								<span class="mai-chat__row-id" data-testid="history-row-id">
									{row.title && row.title.trim() !== '' ? row.title.trim() : shortId(row.run_id)}
								</span>
								<span
									class="mai-chat__badge"
									class:mai-chat__badge--ok={row.state === 'succeeded'}
									class:mai-chat__badge--danger={row.state === 'failed'}
									class:mai-chat__badge--neutral={row.state === 'cancelled'}
									data-testid="history-row-state"
								>
									{row.canonical === true
										? `${row.message_count ?? 0} messages`
										: runStateText(row.state)}
								</span>
								{#if row.canonical === true}
									<span class="mai-chat__meta">{formatShortTime(row.updated_at)}</span>
								{:else}
									<span class="mai-chat__meta">{row.model_profile}</span>
									<span class="mai-chat__meta">{row.submitted_at}</span>
								{/if}
								<span class="mai-chat__badge">{row.server_id === GLOBAL_CONSULT_SERVER_ID ? 'General Consult' : serverLabelFor(row.server_id)}</span>
								{#each row.service_tags ?? [] as tag (tag)}<span class="mai-chat__badge">@{tag}</span>{/each}
							</div>
							<span data-testid="history-open">Open</span>
							</button>
						</li>
					{/each}
				</ul>
				<p class="mai-chat__meta" data-testid="history-total">
					Showing {historyRows.length} of {historyTotal} recorded conversations.
				</p>
				{#if historyHasMore}
					<div class="mai-chat__history-more">
						<Button
							size="sm"
							variant="secondary"
							loading={historyLoadingMore}
							onclick={loadMoreHistory}
							data-testid="history-load-more"
						>
							Load more ({historyRows.length} of {historyTotal})
						</Button>
					</div>
				{/if}
			{/if}
		</section>
		{/snippet}
		<header class="mai-chat__header">
			<div class="mai-chat__header-row">
				<!-- Left side of the header: Target server selector, active server/consult
				     chip, @service chips, and the + @service selector. -->
				<div class="mai-chat__header-context">
					<div class="mai-chat__chips" data-testid="chat-context-chips">
						{#if generalConsultMode}
							<Chip variant="neutral" data-testid="chat-general-consult-badge">
								General Consult — No Server Pinned
							</Chip>
						{:else}
							<Chip
								variant="brass"
								locked={chatContextLocked}
								data-testid="chat-server-chip"
								removeLabel="Remove pinned server"
								removeTestId="chat-server-chip-remove"
								onremove={!chatContextLocked ? () => (draftServerOverride = null) : undefined}
							>
								🖥 {serverLabelFor(effectiveServerId ?? '')}
							</Chip>
						{/if}
						{#each selectedServiceTags as tag (tag)}
							<Chip
								variant="info"
								locked={chatContextLocked}
								data-testid="chat-service-chip"
								removeLabel="Remove service tag"
								removeTestId="chat-service-chip-remove"
								onremove={!chatContextLocked ? () => removeServiceTag(tag) : undefined}
							>
								@{tag}
							</Chip>
						{/each}
					</div>

					{#if !chatContextLocked}
						<div class="mai-chat__compose-context">
							<div class="mai-chat__context-controls">
								<Select
									id="{uid}-server"
									label="Target server"
									hideLabel
									inlineLabel
									pill
									compact
									class="mai-chat__server-picker"
									data-testid="chat-server-picker"
									value={generalConsultMode ? GLOBAL_CONSULT_SERVER_ID : (effectiveServerId ?? '')}
									onchange={(event) => {
										const next = (event.currentTarget as HTMLSelectElement).value;
										draftServerOverride = next === GLOBAL_CONSULT_SERVER_ID ? null : next;
									}}
								>
									<option value={GLOBAL_CONSULT_SERVER_ID}>
										General Consult — No Server Pinned
									</option>
									{#each serverOptions as server (server.id)}
										<option value={server.id}>{server.label}</option>
									{/each}
									{#if !generalConsultMode && effectiveServerId !== null && !serverOptions.some((one) => one.id === effectiveServerId)}
										<option value={effectiveServerId}>Server {shortId(effectiveServerId)}</option>
									{/if}
								</Select>
								{#if !generalConsultMode && canonicalApi === null}
									<Button
										size="sm"
										pill
										variant="secondary"
										onclick={() => (servicePickerOpen = !servicePickerOpen)}
										data-testid="chat-service-picker-toggle"
									>
										+ @service
									</Button>
								{/if}
							</div>
							{#if servicePickerOpen && !generalConsultMode && canonicalApi === null}
								<div class="mai-chat__service-autocomplete" data-testid="chat-service-autocomplete">
									{#each serviceSuggestions as service (service.name)}
										<button
											type="button"
											class="mai-chat__service-option"
											data-testid="chat-service-option"
											onclick={() => pickService(service.name)}
										>
											@{service.name}
											{#if service.category}
												<span class="mai-chat__badge">[{service.category}]</span>
											{/if}
										</button>
									{/each}
									{#if serviceSuggestions.length === 0}
										<p class="mai-chat__meta">No services match that filter.</p>
									{/if}
								</div>
							{/if}
						</div>
					{/if}
				</div>

				<!-- Right side of the header: working badge (when docked) and the dock
				     collapse button, plus sr-only / standalone New Chat & History controls. -->
				<div class="mai-chat__header-right">
					{#if isDocked}
						{#if dockCollapsed && jobInProgress}
							<span class="mai-chat__badge mai-chat__badge--active" data-testid="chat-header-working">
								WORKING · {elapsedSeconds}s
							</span>
						{/if}
					{:else}
						<h3 class="mai-chat__title">Conversation</h3>
					{/if}
					{#if isDocked && dockCollapsed}
						<Button
							size="sm"
							pill
							variant="secondary"
							tabindex={2}
							onclick={() => (dockCollapsed = false)}
						>
							Ask M.A.I.…
						</Button>
					{/if}
					{#if isDocked}
						<Button
							size="sm"
							pill
							variant="ghost"
							tabindex={2}
							onclick={() => (dockCollapsed = !dockCollapsed)}
							aria-expanded={!dockCollapsed}
							aria-label={dockCollapsed ? 'Expand chat dock' : 'Collapse chat dock'}
							data-testid="bottom-chat-dock-toggle"
						>
							{dockCollapsed ? '▴ Expand' : '▾'}
						</Button>
					{/if}
					<div class="mai-chat__header-actions" class:mai-sr-only={isDocked}>
						<Button
							size="sm"
							pill
							variant="secondary"
							tabindex={isDocked ? -1 : 0}
							onclick={newChat}
							data-testid="chat-new-session"
						>
							New Chat
						</Button>
						<Button
							size="sm"
							pill
							variant="secondary"
							tabindex={isDocked ? -1 : 0}
							onclick={() => (historyOpen = !historyOpen)}
							aria-expanded={historyOpen}
							data-testid="chat-history-toggle"
						>
							History
						</Button>
					</div>
				</div>
			</div>
			{#if historyOpen}
				<!-- One bounded history picker; selection restores transcript and
				     frozen server/service context without displacing the chat. -->
				<div class="mai-chat__history-popover" data-testid="chat-history-popover">
					{@render historyList()}
				</div>
			{/if}
		</header>

		<div class="mai-chat__body" hidden={isDocked && dockCollapsed} data-testid="chat-root">
		<!-- 24c D-3: idle means "no conversation open at all". An open or
		     restoring canonical identity (canonicalId nonempty) is NOT idle
		     even while the legacy fold is null (completed conversation,
		     active_run_id null) — the docked idle-collapse rule must never
		     hide a reopened transcript, Understanding or Plan. -->
		<section
			class="mai-chat__section mai-chat__section--conversation"
			class:mai-chat__section--idle={display === null && !canonicalMode}
			aria-label="Open conversation"
			data-testid="chat-conversation"
		>
			{#if display === null && !canonicalMode}
				<div class="mai-chat__state" role="status" data-testid="chat-none">
					<p>No conversation is open. Send a message or open one from the list.</p>
				</div>
			{:else}
				<p class="mai-chat__line mai-chat__conversation-status">
					{#if canonicalMode}
						<span
							class="mai-chat__badge"
							class:mai-chat__badge--active={jobInProgress}
							class:mai-chat__badge--ok={display?.state === 'succeeded'}
							class:mai-chat__badge--danger={display?.state === 'failed'}
							class:mai-chat__badge--neutral={display?.state === 'cancelled'}
							data-testid="conversation-state"
						>
							{canonicalStateText}
						</span>
						{#if canonicalPendingSteering > 0}
							<span class="mai-chat__note" data-testid="pending-steering-note" role="status">
								Pending steering rows: {canonicalPendingSteering}
							</span>
						{/if}
						{#if lastDisposition === 'queued_followup'}
							<span class="mai-chat__note" data-testid="turn-queued-note" role="status">
								Follow-up recorded — queued behind the active run; the run consumes it
								at its next safe boundary.
							</span>
						{/if}
						{#if controlNote !== null}
							<span class="mai-chat__note" data-testid="control-note" role="status">
								{controlNote}
							</span>
						{/if}
					{:else}
						{#if display !== null}
							<span
								class="mai-chat__badge"
								class:mai-chat__badge--active={jobInProgress}
								class:mai-chat__badge--ok={display.state === 'succeeded'}
								class:mai-chat__badge--danger={display.state === 'failed'}
								class:mai-chat__badge--neutral={display.state === 'cancelled'}
								data-testid="conversation-state"
							>
								{runStateText(display.state)}
							</span>
							{#if jobInProgress}
								<span class="mai-chat__live-status" data-testid="conversation-live-progress" role="status">
									<span class="mai-chat__cog-spin" aria-hidden="true">⚙</span>
									<span class="mai-chat__live-status-text">{activeStepText}</span>
									<span class="mai-chat__live-status-timer">{elapsedSeconds}s</span>
								</span>
							{/if}
							{#if cancelRequested && !rootTerminal}
								<span class="mai-chat__note" data-testid="conversation-cancel-note">
									Cancellation requested
								</span>
							{/if}
						{/if}
					{/if}
				</p>

				<!-- 24d grid usability: the outcome/§8/gap content lives INSIDE
				     the bounded conversation scroll region. As direct children of
				     the conversation grid they auto-placed into an implicit
				     ~6300px row, starved the minmax(0,1fr) column to 0px (zero-
				     width task text) and the transcript row to 0 height
				     (R-24c FAIL_REPAIRABLE). Inside the scroll container every
				     block spans the full section width and long task content
				     stays reachable through the working vertical scroll path
				     instead of starving the transcript. -->
				<div class="mai-chat__conversation-scroll" bind:this={conversationScrollEl}>
					{#if taskOutcome !== null}
						<TaskEvidence document={taskOutcome} />
					{/if}

					{#if canonicalMode}
						<!-- §8 Understanding section (act 17-understanding-ui): the ONE
						     active task episode's scope/summary/constraints/plan, read
						     only through the same bound task services. Fails closed
						     (honest unbound/unavailable states) without task services or
						     without an active episode; never fabricates content. -->
						<TaskUnderstanding
							services={taskServicesBag}
							conversationId={canonicalId}
							serverId={canonicalScope}
						/>

						<!-- §8 Plan section (act 18-plan-ui): persistent todos, dependencies,
						     progress, evidence links and revision proposals. Fails closed
						     (honest unbound/unavailable states) without task services or
						     without an active task episode; never disturbs the outcome block. -->
						<TaskPlan
							services={taskServicesBag}
							conversationId={canonicalId}
							serverId={canonicalScope}
							sourceMessageId={lastCanonicalUserMessageId}
						/>
					{/if}

					{#if canonicalMode && canonicalCompleteness !== 'complete'}
						<div
							class="mai-chat__note mai-chat__note--error mai-chat__gap-banner"
							role="alert"
							data-testid="legacy-gaps-banner"
						>
							<span>
								Recorded history is {canonicalCompleteness}: earlier messages may be
								missing, and new mutations require explicit scope confirmation
								(§4 — nothing is reconstructed or invented).
							</span>
							{#each canonicalGaps as gap, gapIndex (gapIndex)}
								<span class="mai-chat__gap" data-testid="legacy-gap">{gap.description}</span>
							{/each}
						</div>
					{/if}

					{#if canonicalMode}
						{#if canonicalBusy}
							<div class="mai-chat__state" role="status" data-testid="canonical-restoring">
								<p>Restoring the recorded conversation…</p>
							</div>
						{:else if canonicalProblem !== null}
							<div
								class="mai-chat__state mai-chat__state--error"
								role="alert"
								data-testid="canonical-problem"
								data-error-code={canonicalProblem.code}
							>
								<p>{canonicalProblem.message}</p>
							</div>
						{:else}
							{#if canonicalHasMore}
								<div class="mai-chat__history-more">
									<Button
										size="sm"
										variant="secondary"
										loading={canonicalLoadingMore}
										onclick={loadEarlierCanonicalMessages}
										data-testid="transcript-load-earlier"
									>
										Load earlier messages ({canonicalMessages.length} of {canonicalTotal})
									</Button>
								</div>
							{/if}
							{#each canonicalMessages as message (message.message_id)}
								{#if message.role === 'user'}
									<div class="mai-chat__bubble mai-chat__bubble--user" data-testid="canonical-user-message">
										<div class="mai-chat__bubble-header">
											<span class="mai-chat__bubble-author">You</span>
											<span class="mai-chat__bubble-time" title={message.created_at}>
												{formatShortTime(message.created_at)}
											</span>
										</div>
										<div class="mai-chat__bubble-body">{message.content}</div>
									</div>
								{:else if message.role === 'assistant'}
									<div class="mai-chat__bubble mai-chat__bubble--assistant" data-testid="canonical-assistant-message">
										<div class="mai-chat__bubble-header">
											<span class="mai-chat__bubble-author">⚙ M.A.I.</span>
										</div>
										<div class="mai-chat__bubble-body">
											{#if message.content !== ''}
												<div class="mai-chat__reply-markdown">
													<MarkdownRenderer content={message.content} />
												</div>
											{:else if message.content_ref !== null && message.content_ref !== ''}
												<p class="mai-chat__meta">
													Recorded behind a safe content reference:
													{message.content_ref}{message.redacted ? ' (redacted)' : ''}
												</p>
											{:else}
												<p class="mai-chat__meta">No inline content recorded for this message.</p>
											{/if}
										</div>
									</div>
								{:else}
									<p class="mai-chat__meta" data-testid="canonical-other-message">
										[{message.role}]{message.content !== ''
											? ` ${message.content}`
											: message.content_ref !== null && message.content_ref !== ''
												? ` ${message.content_ref}`
												: ' (no inline content)'}
									</p>
								{/if}
							{/each}
							<p class="mai-chat__meta" data-testid="transcript-window-meta">
								Showing {canonicalMessages.length} of {canonicalTotal} recorded messages.
							</p>
						{/if}
					{:else}
					{#each priorTurns as turn (turn.runId)}
						<div class="mai-chat__bubble mai-chat__bubble--user" data-testid="prior-turn-user">
							<div class="mai-chat__bubble-header">
								<span class="mai-chat__bubble-author">You</span>
								<span class="mai-chat__bubble-time" title={turn.submittedAt}>
									{formatShortTime(turn.submittedAt)}
								</span>
							</div>
							<div class="mai-chat__bubble-body">
								{turn.userMessage}
							</div>
						</div>

						<div class="mai-chat__bubble mai-chat__bubble--assistant" data-testid="prior-turn-assistant">
							<div class="mai-chat__bubble-header">
								<span class="mai-chat__bubble-author">⚙ M.A.I.</span>
								<span class="mai-chat__bubble-model">{turn.modelLabel}</span>
							</div>
							<div class="mai-chat__bubble-body">
								{#if turn.thinkingSteps.length > 0}
									<details class="mai-chat__thinking-details">
										<summary class="mai-chat__thinking-summary">
											<span>&gt; Thinking ({turn.thinkingSteps.length} steps)</span>
										</summary>
										<blockquote class="mai-chat__thinking-quote">
											{#each turn.thinkingSteps as stepLine, idx (idx)}
												<div class="mai-chat__thinking-line">
													<span class="mai-chat__thinking-prefix">&gt;</span>
													<span>{stepLine}</span>
												</div>
											{/each}
										</blockquote>
									</details>
								{/if}

								{#if turn.toolCalls.length > 0}
									<div class="mai-chat__tool-calls">
										{#each turn.toolCalls as tc (tc.id)}
											{@const detailKey = `${tc.runId}:${tc.stepSeq}`}
											{@const stepDetail = expandedToolDetails[detailKey]}
											<details
												class="mai-chat__tool-card"
												ontoggle={(event) => {
													if ((event.currentTarget as HTMLDetailsElement).open) {
														void loadToolStepDetail(tc);
													}
												}}
											>
												<summary class="mai-chat__tool-card-summary">
													<span class="mai-chat__tool-card-title">
														▸ ⚙ {tc.capabilityId}
														<span class="mai-chat__tool-card-seq">#{tc.stepSeq}</span>
														{#if tc.reason !== ''}
															<span class="mai-chat__tool-card-reason">· {tc.reason}</span>
														{/if}
													</span>
													<span
														class="mai-chat__badge"
														class:mai-chat__badge--ok={tc.status === 'succeeded'}
														class:mai-chat__badge--danger={tc.status === 'failed'}
													>
														{tc.status.toUpperCase()}
													</span>
												</summary>
												<div class="mai-chat__tool-card-body">
													{#if stepDetail?.loading}
														<div class="mai-chat__tool-card-loading">Loading params &amp; response…</div>
													{:else if stepDetail?.error}
														<div class="mai-chat__tool-card-error">{stepDetail.error}</div>
													{:else if stepDetail}
														<div class="mai-chat__tool-card-section">
															<span class="mai-chat__tool-card-label">PARAMS</span>
															<pre class="mai-chat__tool-card-pre">{formatStepJson(stepDetail.params)}</pre>
														</div>
														<div class="mai-chat__tool-card-section">
															<span class="mai-chat__tool-card-label">RESPONSE</span>
															<pre class="mai-chat__tool-card-pre">{formatStepJson(stepDetail.response ?? stepDetail.receipt)}</pre>
														</div>
													{/if}
												</div>
											</details>
										{/each}
									</div>
								{/if}

								{#if turn.reply !== ''}
									<div class="mai-chat__reply-markdown">
										<MarkdownRenderer content={turn.reply} />
									</div>
								{/if}
							</div>
						</div>
					{/each}

					{#if display !== null}
						<div class="mai-chat__bubble mai-chat__bubble--user">
							<div class="mai-chat__bubble-header">
								<span class="mai-chat__bubble-author">You</span>
								<span class="mai-chat__bubble-time" title={display.submitted_at}>
									{formatShortTime(display.submitted_at)}
								</span>
							</div>
							<div class="mai-chat__bubble-body" data-testid="conversation-message">
								{messageText}
							</div>
						</div>
					{/if}
					{/if}

					{#if !canonicalMode || jobInProgress}
					<div class="mai-chat__bubble mai-chat__bubble--assistant">
						<div class="mai-chat__bubble-header">
							<span class="mai-chat__bubble-author">⚙ M.A.I.</span>
							{#if display !== null}
								<span class="mai-chat__bubble-model">{resolvedModel ?? display.model_profile}</span>
							{/if}
						</div>
						<div class="mai-chat__bubble-body" data-testid="assistant-reply">
							{#if thinkingSteps.length > 0 || !rootTerminal}
								<details
									class="mai-chat__thinking-details"
									data-testid="assistant-thinking-block"
									open={!rootTerminal && streamedReply === ''}
								>
									<summary class="mai-chat__thinking-summary">
										{#if !rootTerminal}
											<span class="mai-chat__thinking-pulse"></span>
										{/if}
										<span>
											&gt; Thinking {thinkingSteps.length > 0
												? `(${thinkingSteps.length} steps)`
												: '(active…)'}
										</span>
									</summary>
									<blockquote class="mai-chat__thinking-quote" data-testid="assistant-thinking">
										{#if thinkingSteps.length === 0}
											<div class="mai-chat__thinking-line">
												<span class="mai-chat__thinking-prefix">&gt;</span>
												<span>Initializing cogitator &amp; assembling server context…</span>
											</div>
										{:else}
											{#each thinkingSteps as stepLine, idx (idx)}
												<div class="mai-chat__thinking-line">
													<span class="mai-chat__thinking-prefix">&gt;</span>
													<span>{stepLine}</span>
												</div>
											{/each}
										{/if}
									</blockquote>
								</details>
							{/if}

							{#if visibleToolCalls.length > 0}
								<div class="mai-chat__tool-calls" data-testid="assistant-tool-calls">
									{#each visibleToolCalls as tc (tc.id)}
										{@const detailKey = `${tc.runId}:${tc.stepSeq}`}
										{@const stepDetail = expandedToolDetails[detailKey]}
										<details
											class="mai-chat__tool-card"
											data-testid="assistant-tool-card"
											ontoggle={(event) => {
												if ((event.currentTarget as HTMLDetailsElement).open) {
													void loadToolStepDetail(tc);
												}
											}}
										>
										<summary class="mai-chat__tool-card-summary">
											<span class="mai-chat__tool-card-title">
												▸ <span class:mai-chat__cog-spin={tc.status === 'running' || tc.status === 'requested'} aria-hidden="true">⚙</span> {tc.capabilityId}
												<span class="mai-chat__tool-card-seq">#{tc.stepSeq}</span>
												{#if tc.reason !== ''}
													<span class="mai-chat__tool-card-reason">· {tc.reason}</span>
												{/if}
											</span>
											<span
												class="mai-chat__badge"
												class:mai-chat__badge--active={tc.status === 'running' || tc.status === 'requested'}
												class:mai-chat__badge--ok={tc.status === 'succeeded'}
												class:mai-chat__badge--danger={tc.status === 'failed'}
											>
												{tc.status.toUpperCase()}
											</span>
										</summary>
											<div class="mai-chat__tool-card-body">
												{#if stepDetail?.loading}
													<div class="mai-chat__tool-card-loading">Loading params &amp; response…</div>
												{:else if stepDetail?.error}
													<div class="mai-chat__tool-card-error">{stepDetail.error}</div>
												{:else if stepDetail}
													<div class="mai-chat__tool-card-section">
														<span class="mai-chat__tool-card-label">PARAMS</span>
														<pre class="mai-chat__tool-card-pre">{formatStepJson(stepDetail.params)}</pre>
													</div>
													<div class="mai-chat__tool-card-section">
														<span class="mai-chat__tool-card-label">RESPONSE</span>
														<pre class="mai-chat__tool-card-pre">{formatStepJson(stepDetail.response ?? stepDetail.receipt)}</pre>
													</div>
												{/if}
											</div>
										</details>
									{/each}
								</div>
							{/if}

							{#if streamedReply !== ''}
								<div class="mai-chat__reply-markdown">
									<MarkdownRenderer content={streamedReply} testId="assistant-reply-text" />
									{#if isStreamingReply}
										<span class="mai-chat__stream-cursor" aria-hidden="true">▍</span>
									{/if}
								</div>
						{:else if jobInProgress && thinkingSteps.length === 0 && visibleToolCalls.length === 0 && streamedReply === ''}
								<div
									class="mai-chat__work-indicator"
									role="status"
									aria-live="polite"
									data-testid="assistant-working-indicator"
								>
									<div class="mai-chat__work-indicator-row">
										<span class="mai-chat__cog-spin" aria-hidden="true">⚙</span>
										<span class="mai-chat__work-indicator-label">{activeStepText}</span>
										<span class="mai-chat__work-indicator-elapsed">{elapsedSeconds}s</span>
									</div>
									<div class="mai-chat__work-bar" aria-hidden="true">
										<div class="mai-chat__work-bar-fill"></div>
									</div>
								</div>
							{:else if rootTerminal}
								<div class="mai-chat__reply-placeholder">
									(Assistant reply is recorded in Details → Agent activity & events)
								</div>
							{/if}
						</div>
					</div>
					{/if}

				</div>

				<div class="mai-chat__inline-alerts">
					{#if approvalPending}
						<Button onclick={recordDecision} loading={approvalBusy} data-testid="approval-decide">
							Approve bounded action
						</Button>
					{/if}
					{#if approvalWarning}
						<p class="mai-chat__note mai-chat__note--error" role="alert" data-testid="approval-warning">
							{approvalProblem?.message ?? 'Waiting for approval; no actionable bound decision is available.'}
							<Button variant="ghost" size="sm" onclick={() => (detailsOpen = true)}>Details</Button>
						</p>
					{/if}
					{#if streamProblem !== null}
						<p class="mai-chat__note mai-chat__note--error" role="status" data-testid="stream-problem">
							{streamProblem}
							<Button variant="ghost" size="sm" onclick={() => (detailsOpen = true)}>Details</Button>
						</p>
					{/if}
					{#if refreshProblem !== null}
						<p class="mai-chat__note mai-chat__note--error" role="status" data-testid="refresh-problem" data-error-code={refreshProblem.code}>
							{refreshProblem.message}
							<Button variant="ghost" size="sm" onclick={() => (detailsOpen = true)}>Details</Button>
						</p>
					{/if}
				</div>

				<DetailsDisclosure bind:open={detailsOpen} id="{uid}-details" testId="chat-details">
					<div class="mai-chat__drawer-head">
						<span class="mai-chat__drawer-title">⚙ Conversation Details</span>
						<Button
							size="sm"
							variant="ghost"
							onclick={() => (detailsOpen = false)}
							aria-label="Close details"
							data-testid="chat-details-close"
						>
							✕
						</Button>
					</div>
					<div class="mai-chat__section-head">
						{#if display !== null}
						<span class="mai-chat__meta" data-testid="conversation-id">{shortId(display.run_id)}</span>
						<Button variant="secondary" onclick={loadConversation} data-testid="conversation-refresh">Refresh</Button>
						{/if}
					</div>
					<p class="mai-chat__meta" data-testid="chat-scope">
						{canonicalScope === GLOBAL_CONSULT_SERVER_ID
							? 'General Consult · global scope'
							: `Server ${shortId(canonicalScope ?? '')} · Pinned scope`}
					</p>
					{#if display !== null}
					<p class="mai-chat__meta" data-testid="conversation-target">
						Server {shortId(display.server_id)} · profile {display.model_profile} · grants {grantsText}
					</p>
					{/if}
					{#if cancelRequested && rootTerminal}
						<p class="mai-chat__meta" data-testid="conversation-cancel-note">Cancellation requested</p>
					{/if}
					{#if transcript.length > 0}
						<details class="mai-chat__transcript" data-testid="conversation-transcript">
							<summary class="mai-chat__transcript-summary">
								Agent activity &amp; events ({transcript.length} recorded)
							</summary>
							<ul data-testid="transcript-list">
								{#each transcript as entry (entry.seq)}
									<li
										class="mai-chat__entry"
										data-testid="transcript-entry"
										data-kind={entry.kind}
										data-seq={entry.seq}
									>
										<span class="mai-chat__entry-seq">#{entry.seq}</span>
										<span class="mai-chat__entry-label" data-testid="transcript-label">
											{entry.label}
										</span>
										{#if entry.rows.length > 0}
											<dl class="mai-chat__entry-rows" data-testid="transcript-rows">
												{#each entry.rows as row, rowIndex (rowIndex + row.k)}
													<dt>{row.k}</dt>
													<dd data-testid="transcript-value">{row.v}</dd>
												{/each}
											</dl>
										{/if}
									</li>
								{/each}
							</ul>
						</details>
					{:else}
						<p class="mai-chat__meta" data-testid="transcript-empty">
							No events are recorded yet.
						</p>
					{/if}

				<div class="mai-chat__attachments" data-testid="conversation-attachments">
					<h5>Permitted context attachments</h5>
					{#if attachments.length === 0}
						<p class="mai-chat__meta" data-testid="attachments-empty">
							No artifact references are recorded for this conversation.
						</p>
					{:else}
						<ul data-testid="attachment-list">
							{#each attachments as attachment (attachment.op_id)}
								<li class="mai-chat__attachment" data-testid="attachment-row">
									<span data-testid="attachment-op">{shortId(attachment.op_id)}</span>
									<span data-testid="attachment-sha">sha256 {shortHash(attachment.sha256)}</span>
									<span data-testid="attachment-bytes">{attachment.bytes} bytes</span>
								</li>
							{/each}
						</ul>
					{/if}
				</div>

				<p class="mai-chat__meta" data-testid="stream-status">
					{#if streamStatus === 'connecting'}
						Event stream connecting…
					{:else if streamStatus === 'reconnecting'}
						Event stream reconnecting from the last recorded event…
					{:else if streamStatus === 'closed'}
						Event stream closed ({appliedEvents} applied).
					{:else if streamStatus === 'off'}
						Event stream idle.
					{:else}
						Event stream live ({appliedEvents} applied).
					{/if}
				</p>
				{#if showApprovalSection}
					<div class="mai-chat__approval" data-testid="approval-section">
						<h5>Bounded action</h5>
						{#if approvalProblem !== null}
							<p
								class="mai-chat__note mai-chat__note--error"
								role="alert"
								data-testid="approval-error"
								data-error-code={approvalProblem.code}
							>
								{approvalProblem.message}
							</p>
						{:else if approvalBinding === null && approval === null}
							<p class="mai-chat__meta" data-testid="approval-absent">
								A bounded action requires an approval decision; no bound decision record
								exists for this run yet.
							</p>
						{/if}
						{#if approvalBinding !== null && approval !== null && display !== null}
							<dl class="mai-chat__entry-rows" data-testid="approval-binding">
								<dt>approval_id</dt>
								<dd data-testid="approval-id">{approval.approval_id}</dd>
								<dt>capability_id</dt>
								<dd data-testid="approval-capability">{approvalBinding.capabilityId}</dd>
								<dt>server_id</dt>
								<dd data-testid="approval-server">{display.server_id}</dd>
								<dt>payload_hash</dt>
								<dd data-testid="approval-hash">{approvalBinding.payloadHash}</dd>
								<dt>policy_revision</dt>
								<dd data-testid="approval-revision">{approvalBinding.policyRevision}</dd>
								<dt>status</dt>
								<dd data-testid="approval-status">{approval.status}</dd>
								<dt>expires_at</dt>
								<dd data-testid="approval-expires">{approval.expires_at}</dd>
							</dl>
							{#if approvalPending}
								<p class="mai-chat__meta">
									One-use decision (C03 rule 4): recording it consumes the approval for
									exactly this binding.
								</p>
							{:else if approval.status === 'consumed'}
								<p class="mai-chat__meta" data-testid="approval-spent">
									Decision consumed{approval.consumed_at === null
										? ''
										: ` at ${approval.consumed_at}`} — the bound action can run when the
									conversation next requests it.
								</p>
							{:else}
								<p class="mai-chat__meta" data-testid="approval-expired">
									Approval status: {approval.status}. No decision can be recorded.
								</p>
							{/if}
						{/if}
						{#if decisionStatus !== null}
							<p class="mai-chat__note" role="status" data-testid="approval-recorded">
								{decisionStatus}
							</p>
						{/if}
					</div>
				{/if}

				{#if display !== null}
					<div class="mai-chat__children" data-testid="conversation-children">
						<h5>Delegated children</h5>
						<DelegationTree
							conversation={display}
							api={api}
							taskLabels={taskLabels}
							showCancel={false}
						/>
					</div>
				{/if}
				</DetailsDisclosure>
			{/if}
		</section>

		<section class="mai-chat__section mai-chat__section--compose" aria-label="Compose" data-testid="chat-compose">
			<div class="mai-chat__form">
				<div class="mai-chat__composer-box">
					<label class="mai-chat__label mai-chat__label--sr" for="{uid}-message">Message</label>
					<textarea
						id="{uid}-message"
						class="mai-chat__textarea"
						tabindex={isDocked ? 1 : undefined}
						bind:this={dockComposerEl}
						placeholder="Ask M.A.I. or type @service to pin context…"
						bind:value={composeMessage}
						oninput={handleComposeInput}
						onkeydown={onComposerKeydown}
						rows={1}
						data-testid="compose-message"
					></textarea>
					<!-- On-demand attachments: the field stays in the DOM as a 1px
					     sr-only shell when collapsed so `.fill()`/focus reveal it. -->
					<div
						class="mai-chat__composer-attachments"
						class:mai-chat__composer-attachments--collapsed={!attachmentsRevealed}
					>
						<Field
							label="Attachments"
							compact
							bind:value={composeAttachments}
							data-testid="compose-attachments"
							error={composeFields.attachments ?? ''}
							hint=""
							oninput={() => (attachmentsOpen = true)}
							onfocus={() => (attachmentsOpen = true)}
						/>
					</div>
					<div class="mai-chat__composer-toolbar">
						<div class="mai-chat__composer-tools">
							<ModelProfileSelect
								compact
								bind:value={composeProfile}
								error={composeFields.profile ?? ''}
								testId="compose-profile"
								live={profilesLive}
							/>
							<Button
								pill
								size="sm"
								variant={readOnlyMode ? 'primary' : 'secondary'}
								aria-pressed={readOnlyMode}
								onclick={() => (readOnlyMode = !readOnlyMode)}
								data-testid="compose-read-only-toggle"
							>
								{readOnlyMode ? 'Read-only ON' : 'Read-only'}
							</Button>
							{#if canonicalApi === null}
								<Button
									pill
									size="sm"
									variant={attachmentsOpen ? 'primary' : 'secondary'}
									aria-pressed={attachmentsOpen}
									onclick={() => (attachmentsOpen = !attachmentsOpen)}
								>
									📎 Attach
								</Button>
							{/if}
						</div>
						<div class="mai-chat__actions">
							{#if canonicalMode && jobInProgress}
								<Button
									pill
									size="sm"
									variant="secondary"
									disabled={controlBusy}
									loading={controlBusy}
									onclick={() => recordControl('pause')}
									data-testid="chat-pause"
								>
									Pause
								</Button>
							{/if}
							<Button
								pill
								size="sm"
								variant={canStopRun ? 'danger' : 'ghost'}
								class={canStopRun ? 'mai-chat__stop' : 'mai-chat__stop mai-chat__stop--idle'}
								disabled={!canStopRun}
								loading={stoppingRun}
								onclick={stopActiveConversation}
								data-testid="chat-stop"
							>
								Stop
							</Button>
							<Button pill onclick={submitCompose} loading={composeBusy} data-testid="chat-submit">
								{jobInProgress ? 'Steer' : 'Send'}
							</Button>
						</div>
					</div>
				</div>
				{#if composeFields.message}
					<p class="mai-chat__field-error" role="alert">{composeFields.message}</p>
				{/if}
				{#if composeProblem !== null}
					<div
						class="mai-chat__state mai-chat__state--error"
						role="alert"
						data-testid="compose-error"
						data-error-code={composeProblem.code}
					>
						<p>{composeProblem.message}</p>
					</div>
				{/if}
			</div>
		</section>
		</div>
	{/if}
</div>

<style>
	.mai-chat {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3, 0.75rem);
		padding: 0;
		color: var(--mai-color-text, inherit);
		font-size: var(--mai-text-sm, 0.875rem);
		min-width: 0;
		min-height: 0;
		overflow: visible;
		container-type: inline-size;
		overflow-wrap: anywhere;
	}

	/* Compact layout when mounted inside the Bottom Chat Dock. */
	.mai-chat--docked {
		gap: var(--mai-space-2);
		padding: 0;
		overflow: visible;
	}

	.mai-chat__body {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		min-width: 0;
		min-height: 0;
		overflow: hidden;
		/* Bound the whole workspace, never individual message length. Auto-height
		   windows still measure this intrinsic body and own their outer cap. */
		height: min(32rem, calc(100dvh - 10rem));
	}

	.mai-chat--dock-collapsed { height: auto; }
	.mai-chat--docked .mai-chat__body:has(.mai-chat__section--idle) { height: auto; }
	/* Minimized spotlight pill: the collapsed bar keeps only the pinned
	 * context chip, the live WORKING badge and the expand controls. */
	.mai-chat--dock-collapsed .mai-chat__compose-context { display: none; }

	.mai-chat__body[hidden] {
		display: none;
	}

	.mai-chat--docked .mai-chat__body {
		gap: var(--mai-space-2);
	}

	.mai-chat--docked .mai-chat__title,
	.mai-chat--docked .mai-chat__section-head > h4,
	.mai-chat--docked .mai-chat__section--idle {
		display: none;
	}

	.mai-chat--docked .mai-chat__section {
		border-top: var(--mai-border-width) solid var(--mai-color-border);
		padding-top: var(--mai-space-2);
		gap: var(--mai-space-2);
	}

	/* Place the reviewed disclosure trigger beside status; its mounted panel
	 * slides in from the right edge of .mai-chat__body, so toggling never
	 * steals vertical height from the conversation scroll area. */
	.mai-chat__section--conversation:not(.mai-chat__section--idle) {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		grid-template-rows: auto auto minmax(0, 1fr);
		flex: 1 1 0;
		min-height: 0;
		min-width: 0;
		border: 0;
		padding: 0;
	}

	.mai-chat__conversation-status {
		grid-area: 1 / 1;
		min-height: 28px;
		align-items: center;
	}
	.mai-chat__inline-alerts { grid-area: 2 / 1 / auto / -1; }
	.mai-chat__inline-alerts:empty { display: none; }
	.mai-chat__section--conversation > .mai-chat__conversation-scroll { grid-area: 3 / 1 / auto / -1; }
	.mai-chat__section--conversation > :global(.mai-details) { display: contents; }
	.mai-chat__section--conversation > :global(.mai-details > .mai-details__trigger) {
		grid-area: 1 / 2;
		min-height: 28px;
	}
	.mai-chat__section--conversation > :global(.mai-details > .mai-details__panel) {
		position: absolute;
		top: 0;
		right: 0;
		bottom: 0;
		width: min(26rem, 92%);
		max-height: none;
		z-index: 25;
		overflow-x: hidden;
		overflow-y: auto;
		overscroll-behavior: contain;
		box-sizing: border-box;
		background: var(--mai-surface-glass-raised, var(--mai-color-surface-raised));
		border: var(--mai-border-width) solid var(--mai-color-border-strong);
		border-left: var(--mai-chip-accent-width, 3px) solid var(--mai-color-brass);
		border-radius: var(--mai-radius-md);
		box-shadow: var(--mai-shadow-float);
		padding: var(--mai-space-3);
		animation: mai-chat-slide-from-right var(--mai-motion-base, 180ms) ease-out;
	}

	.mai-chat__drawer-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--mai-space-2);
		padding-bottom: var(--mai-space-2);
		margin-bottom: var(--mai-space-2);
		border-bottom: var(--mai-border-width) solid var(--mai-color-border);
	}

	.mai-chat__drawer-title {
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--mai-color-brass);
	}

	@keyframes mai-chat-slide-from-right {
		from {
			transform: translateX(100%);
			opacity: 0.4;
		}
		to {
			transform: translateX(0);
			opacity: 1;
		}
	}

	.mai-chat__conversation-scroll {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
		min-height: 0;
		min-width: 0;
		max-width: 100%;
		overflow-x: hidden;
		overflow-y: auto;
		overscroll-behavior: contain;
		padding: 0;
		scrollbar-gutter: stable;
	}

	.mai-chat__bubble {
		display: flex;
		flex-direction: column;
		flex: 0 0 auto;
		gap: var(--mai-space-1);
		width: 100%;
		min-width: 0;
		padding: var(--mai-space-1) 0;
		border: 0;
		background: transparent;
		box-shadow: none;
	}

	.mai-chat__bubble-header {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: var(--mai-space-2);
	}

	.mai-chat__bubble-author {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-brass);
	}

	.mai-chat__bubble-time {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-subtle);
	}

	.mai-chat__bubble-model {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: 999px;
		padding: 0 var(--mai-space-2);
		overflow-wrap: anywhere;
	}

	.mai-chat__bubble-body {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		font-size: var(--mai-text-sm);
		line-height: 1.55;
		color: var(--mai-color-text);
		overflow-wrap: anywhere;
	}

	/* User turns: the OUTER bubble stays full width (geometry invariant); the
	 * header and body align right and the body becomes a brass-tinted pill. */
	.mai-chat__bubble--user .mai-chat__bubble-header,
	.mai-chat__bubble--user .mai-chat__bubble-body {
		align-self: flex-end;
		max-width: min(88%, 42rem);
	}

	.mai-chat__bubble--user .mai-chat__bubble-body {
		padding: var(--mai-space-2) var(--mai-space-3);
		border-radius: var(--mai-radius-lg) var(--mai-radius-lg) var(--mai-radius-sm) var(--mai-radius-lg);
		background: color-mix(in srgb, var(--mai-color-brass) 12%, var(--mai-color-surface-raised));
		border: 1px solid color-mix(in srgb, var(--mai-color-brass) 28%, transparent);
	}

	.mai-chat__thinking-details {
		min-width: 0;
		max-width: 100%;
		border: 0;
		border-left: var(--mai-chip-accent-width, 3px) solid var(--mai-color-brass-dim);
		background: transparent;
		padding: var(--mai-space-1) var(--mai-space-2);
		overflow-wrap: anywhere;
		word-break: break-word;
	}

	.mai-chat__thinking-summary {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2);
		cursor: pointer;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.04em;
		color: var(--mai-color-text-muted);
		user-select: none;
		padding: 1px var(--mai-space-2);
		min-height: 24px;
		border-radius: 999px;
		background: color-mix(in srgb, var(--mai-color-brass) 5%, transparent);
	}

	.mai-chat__thinking-quote {
		margin: var(--mai-space-2) 0 0;
		padding: var(--mai-space-1) var(--mai-space-2);
		min-width: 0;
		max-width: 100%;
		border: 0;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
		display: flex;
		flex-direction: column;
		gap: 3px;
		overflow-wrap: anywhere;
		word-break: break-word;
	}

	.mai-chat__thinking-line {
		display: flex;
		align-items: flex-start;
		gap: var(--mai-space-2);
		min-width: 0;
		line-height: 1.45;
		overflow-wrap: anywhere;
		word-break: break-word;
	}

	.mai-chat__thinking-prefix {
		color: var(--mai-color-brass);
		font-weight: var(--mai-weight-semibold);
		user-select: none;
		flex: 0 0 auto;
	}

	.mai-chat__tool-calls {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	/* Thinking trace and tool chips form one tight activity group (4px). */
	.mai-chat__thinking-details + .mai-chat__tool-calls {
		margin-top: calc(4px - var(--mai-space-2));
	}

	.mai-chat__tool-card {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 0;
		border: 0;
		background: transparent;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
	}

	.mai-chat__tool-card-summary {
		cursor: pointer;
		list-style: none;
		display: flex;
		flex-direction: row;
		align-items: center;
		justify-content: space-between;
		gap: var(--mai-space-2);
		padding: 3px 8px;
		border-radius: var(--mai-radius-sm);
		background: color-mix(in srgb, var(--mai-color-brass) 6%, transparent);
		user-select: none;
		min-width: 0;
	}

	.mai-chat__tool-card-summary::-webkit-details-marker {
		display: none;
	}

	.mai-chat__tool-card-body {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding-top: var(--mai-space-2);
		margin-top: var(--mai-space-1);
		border-top: 1px dashed var(--mai-color-border);
	}

	.mai-chat__tool-card-section {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.mai-chat__tool-card-label {
		color: var(--mai-color-brass);
		font-weight: var(--mai-weight-semibold);
		font-size: 0.7rem;
		letter-spacing: 0.06em;
	}

	.mai-chat__tool-card-pre {
		margin: 0;
		padding: var(--mai-space-1) var(--mai-space-2);
		background: var(--mai-color-surface-sunken, rgba(0, 0, 0, 0.25));
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm, 3px);
		max-height: 16rem;
		overflow: auto;
		white-space: pre-wrap;
		word-break: break-word;
		color: var(--mai-color-text);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
	}

	.mai-chat__tool-card-loading,
	.mai-chat__tool-card-error {
		color: var(--mai-color-text-muted);
		font-style: italic;
	}

	.mai-chat__tool-card-title {
		display: inline-flex;
		align-items: baseline;
		gap: var(--mai-space-1);
		flex: 1 1 auto;
		min-width: 0;
		color: var(--mai-color-brass);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.04em;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.mai-chat__tool-card-summary .mai-chat__badge {
		flex: 0 0 auto;
	}

	.mai-chat__tool-card-seq,
	.mai-chat__tool-card-reason {
		color: var(--mai-color-text-muted);
		font-weight: var(--mai-weight-medium);
	}

	.mai-chat__reply-markdown {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		min-width: 0;
		max-width: 100%;
		overflow-wrap: anywhere;
		word-break: break-word;
	}

	.mai-chat__stream-cursor {
		display: inline-block;
		color: var(--mai-color-brass);
		font-family: var(--mai-font-mono);
		animation: mai-chat-pulse 0.8s ease-in-out infinite;
	}

	.mai-chat__thinking-pulse {
		display: inline-block;
		width: 8px;
		height: 8px;
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-brass);
		animation: mai-chat-pulse 1.2s ease-in-out infinite;
	}

	@keyframes mai-chat-pulse {
		0%,
		100% {
			opacity: 0.35;
			transform: scale(0.9);
		}
		50% {
			opacity: 1;
			transform: scale(1.15);
		}
	}

	.mai-chat__reply-placeholder {
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text-subtle);
		font-style: italic;
	}

	.mai-chat__transcript {
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		background-color: var(--mai-color-surface-sunken);
		padding: var(--mai-space-2) var(--mai-space-3);
	}

	.mai-chat__transcript-summary {
		cursor: pointer;
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-text-muted);
		user-select: none;
	}

	/* Unified single-box composer surface: textarea + on-demand attachments +
	 * toolbar live in one cohesive bordered pill container. */
	.mai-chat__composer-box {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		min-width: 0;
		border: var(--mai-border-width) solid var(--mai-color-border-strong);
		border-radius: var(--mai-radius-lg);
		background: var(--mai-color-surface-sunken, rgba(0, 0, 0, 0.2));
		padding: var(--mai-space-2);
	}

	.mai-chat__composer-toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
		min-width: 0;
	}

	.mai-chat__composer-tools {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
		min-width: 0;
	}

	/* On-demand attachments: collapsed to a 1px sr-only shell (the input stays
	 * fillable/focusable and reveals itself on first use), full row when open. */
	.mai-chat__composer-attachments {
		min-width: 0;
	}

	.mai-chat__composer-attachments--collapsed {
		position: absolute;
		width: 1px;
		height: 1px;
		margin: -1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		clip-path: inset(50%);
		white-space: nowrap;
	}

	/* Rides the Button class prop (out of this template's static scope). */
	:global(.mai-chat__stop--idle) {
		opacity: 0.45;
	}

	.mai-chat__header-context {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
		flex: 1 1 auto;
		min-width: 0;
	}

	.mai-chat__header-right {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2);
		flex: 0 0 auto;
	}

	.mai-chat__label--sr,
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


	/* Floating history selector & @service autocomplete: open ABOVE the dock when docked. */
	.mai-chat--docked .mai-chat__history-popover,
	.mai-chat--docked .mai-chat__service-autocomplete {
		top: auto;
		bottom: calc(100% + var(--mai-space-2));
		background: var(--mai-surface-glass-raised, var(--mai-color-surface-raised));
		backdrop-filter: var(--mai-blur-glass);
		-webkit-backdrop-filter: var(--mai-blur-glass);
		border-color: var(--mai-color-border-strong);
		border-radius: var(--mai-radius-lg);
		box-shadow: var(--mai-shadow-float);
	}

	.mai-chat__header {
		display: flex;
		flex-direction: column;
		gap: var(--space-1, 0.25rem);
		position: relative;
	}

	.mai-chat__header-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	/* Floating history selector: anchored to the header row, above the page.
	 * Stacks ABOVE the details sheet (z-index 25): both are transient, but
	 * the popover must stay clickable while the sheet is open. */
	.mai-chat__history-popover {
		position: absolute;
		top: 100%;
		right: 0;
		z-index: 30;
		width: min(24rem, 100%);
		max-height: 18rem;
		overflow: auto;
		padding: var(--mai-space-2, 0.5rem);
		background: var(--surface-raised, var(--mai-color-surface, inherit));
		border: 1px solid var(--border-strong, currentColor);
		border-radius: var(--mai-radius-sm, 4px);
		box-shadow: var(--mai-shadow-window, none);
	}

	.mai-chat__chips {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-chat__compose-context {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
	}

	.mai-chat__context-controls {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	/* `@service` autocomplete: opens under the composer controls; stacks
	 * above the details sheet (z-index 25) like the history popover. */
	.mai-chat__service-autocomplete {
		position: absolute;
		top: 100%;
		left: 0;
		z-index: 30;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 0.25rem);
		width: min(20rem, 100%);
		max-height: 14rem;
		overflow: auto;
		padding: var(--mai-space-2, 0.5rem);
		background: var(--surface-raised, var(--mai-color-surface, inherit));
		border: 1px solid var(--border-strong, currentColor);
		border-radius: var(--mai-radius-sm, 4px);
		box-shadow: var(--mai-shadow-window, none);
	}

	.mai-chat__service-option {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
		padding: var(--mai-space-1, 0.25rem) var(--mai-space-2, 0.5rem);
		font: inherit;
		color: inherit;
		text-align: start;
		cursor: pointer;
		background: transparent;
		border: 1px solid var(--border-subtle, transparent);
		border-radius: var(--mai-radius-sm, 4px);
	}

	.mai-chat__service-option:hover {
		background: var(--mai-color-bg, transparent);
	}

	.mai-chat__title {
		margin: 0;
		font-size: var(--font-size-md, 1rem);
	}

	h4,
	h5 {
		margin: 0;
		font-size: var(--font-size-sm, 0.875rem);
	}

	.mai-chat__meta {
		margin: 0;
		color: var(--color-muted, inherit);
		display: inline-flex;
		gap: var(--space-3, 0.75rem);
		flex-wrap: wrap;
	}

	.mai-chat__section {
		display: flex;
		flex-direction: column;
		gap: var(--space-3, 0.75rem);
		border-top: 1px solid var(--border-subtle, transparent);
		padding-top: var(--space-3, 0.75rem);
	}

	.mai-chat__section-head {
		display: flex;
		align-items: baseline;
		gap: var(--space-3, 0.75rem);
		flex-wrap: wrap;
	}

	.mai-chat__form {
		display: flex;
		flex-direction: column;
		gap: var(--space-3, 0.75rem);
	}

	.mai-chat__label {
		font-weight: 600;
	}

	.mai-chat__textarea {
		width: 100%;
		resize: none;
		height: auto;
		min-height: 2.25rem;
		max-height: 7.5rem;
		box-sizing: border-box;
		padding: var(--mai-space-1) var(--mai-space-2);
		border: 0;
		border-radius: var(--mai-radius-sm, 4px);
		background: transparent;
		color: inherit;
		font: inherit;
		overflow-y: auto;
	}

	.mai-chat__textarea:focus-visible {
		outline: var(--mai-focus-ring-width) solid var(--mai-focus-ring-color);
		outline-offset: calc(-1 * var(--mai-focus-ring-width));
	}

	.mai-chat__actions {
		display: flex;
		gap: var(--space-2, 0.5rem);
		align-items: center;
	}

	.mai-chat__field-error {
		margin: 0;
		color: var(--color-danger, inherit);
	}

	.mai-chat__state {
		padding: var(--space-2, 0.5rem);
		border-radius: var(--radius-sm, 4px);
		border: 1px dashed var(--border-subtle, transparent);
	}

	.mai-chat__state--error {
		border-style: solid;
		border-color: var(--color-danger, currentColor);
		color: var(--color-danger, inherit);
	}

	.mai-chat__rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2, 0.5rem);
	}

	.mai-chat__row {
		display: flex;
		justify-content: space-between;
		gap: var(--space-2, 0.5rem);
		align-items: center;
		border: 1px solid var(--border-subtle, transparent);
		border-radius: var(--radius-sm, 4px);
		padding: var(--space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-chat__row-main {
		display: inline-flex;
		gap: var(--space-2, 0.5rem);
		align-items: center;
		flex-wrap: wrap;
	}

	.mai-chat__row-id {
		font-family: var(--font-mono, monospace);
	}

	.mai-chat__badge {
		border: 1px solid var(--border-subtle, currentColor);
		border-radius: 999px;
		padding: 0 var(--space-2, 0.5rem);
	}

	.mai-chat__badge--active {
		color: var(--mai-color-brass);
		border-color: var(--mai-color-brass);
		background: color-mix(in srgb, var(--mai-color-brass) 12%, transparent);
	}

	.mai-chat__cog-spin {
		display: inline-block;
		color: var(--mai-color-brass);
		animation: mai-chat-spin 2.2s linear infinite;
	}

	@keyframes mai-chat-spin {
		from {
			transform: rotate(0deg);
		}
		to {
			transform: rotate(360deg);
		}
	}

	.mai-chat__live-status {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2, 0.5rem);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs, 0.75rem);
		color: var(--mai-color-text-muted);
		min-width: 0;
	}

	.mai-chat__live-status-text {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 36ch;
	}

	.mai-chat__live-status-timer {
		color: var(--mai-color-brass);
		font-weight: var(--mai-weight-semibold);
		flex: 0 0 auto;
	}

	.mai-chat__work-indicator {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: var(--mai-space-2) var(--mai-space-3);
		border: var(--mai-border-width) solid var(--mai-color-brass-dim);
		border-left: var(--mai-chip-accent-width, 3px) solid var(--mai-color-brass);
		background: color-mix(in srgb, var(--mai-color-brass) 6%, var(--mai-color-surface));
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		min-width: 0;
		max-width: 100%;
	}

	.mai-chat__work-indicator-row {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		min-width: 0;
	}

	.mai-chat__work-indicator-label {
		flex: 1 1 auto;
		min-width: 0;
		color: var(--mai-color-text);
		overflow-wrap: anywhere;
		word-break: break-word;
	}

	.mai-chat__work-indicator-elapsed {
		flex: 0 0 auto;
		color: var(--mai-color-brass);
		font-weight: var(--mai-weight-semibold);
	}

	.mai-chat__work-bar {
		position: relative;
		width: 100%;
		height: 2px;
		background: var(--mai-color-border);
		overflow: hidden;
	}

	.mai-chat__work-bar-fill {
		position: absolute;
		top: 0;
		left: 0;
		width: 35%;
		height: 100%;
		background: var(--mai-color-brass);
		animation: mai-chat-shimmer 1.4s ease-in-out infinite;
	}

	@keyframes mai-chat-shimmer {
		0% {
			transform: translateX(-100%);
		}
		100% {
			transform: translateX(300%);
		}
	}

	.mai-chat__badge--ok {
		color: var(--color-success, inherit);
		border-color: var(--color-success, currentColor);
	}

	.mai-chat__badge--danger {
		color: var(--color-danger, inherit);
		border-color: var(--color-danger, currentColor);
	}

	.mai-chat__badge--neutral {
		color: var(--color-muted, inherit);
	}

	.mai-chat__line {
		margin: 0;
		display: inline-flex;
		gap: var(--space-2, 0.5rem);
		align-items: center;
		flex-wrap: wrap;
	}

	.mai-chat__note {
		margin: 0;
		color: var(--color-warning, inherit);
	}

	.mai-chat__note--error {
		color: var(--color-danger, inherit);
	}

	.mai-chat__attachments,
	.mai-chat__transcript,
	.mai-chat__approval,
	.mai-chat__children {
		display: flex;
		flex-direction: column;
		gap: var(--space-2, 0.5rem);
	}

	.mai-chat__attachments ul,
	.mai-chat__transcript ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-1, 0.25rem);
	}

	.mai-chat__attachment {
		display: inline-flex;
		gap: var(--space-3, 0.75rem);
		font-family: var(--font-mono, monospace);
		font-size: var(--font-size-xs, 0.75rem);
		flex-wrap: wrap;
	}

	.mai-chat__entry {
		display: flex;
		flex-direction: column;
		gap: var(--space-1, 0.25rem);
		border: 1px solid var(--border-subtle, transparent);
		border-radius: var(--radius-sm, 4px);
		padding: var(--space-2, 0.5rem);
	}

	.mai-chat__entry-seq {
		color: var(--color-muted, inherit);
		font-family: var(--font-mono, monospace);
		font-size: var(--font-size-xs, 0.75rem);
	}

	.mai-chat__entry-rows {
		margin: 0;
		display: grid;
		grid-template-columns: minmax(0, max-content) minmax(0, 1fr);
		gap: 0 var(--space-3, 0.75rem);
		font-family: var(--font-mono, monospace);
		font-size: var(--font-size-xs, 0.75rem);
		overflow-wrap: anywhere;
	}

	.mai-chat__entry-rows dt {
		color: var(--color-muted, inherit);
	}

	.mai-chat__entry-rows dd {
		margin: 0;
	}

	.mai-chat__section--compose { flex: 0 0 auto; min-width: 0; }
	.mai-chat__section--idle { flex: 1 1 0; min-height: 0; overflow: auto; }
	.mai-chat__header { flex: 0 0 auto; }
	.mai-chat__header-right { flex-wrap: wrap; min-width: 0; }
	.mai-chat__context-controls, .mai-chat__compose-context, .mai-chat__chips { min-width: 0; max-width: 100%; }
	.mai-chat__context-controls :global(.mai-select), .mai-chat__context-controls :global(select) { min-width: 0; max-width: 100%; }
	.mai-chat__inline-alerts { max-height: 18dvh; overflow: auto; }
	.mai-chat__reply-markdown, .mai-chat__bubble-body { min-width: 0; }
	@container (max-width: 440px) {
		/* The composer toolbar wraps inside the single composer box; actions
		 * stay right-aligned and the conversation keeps the taller share. */
		.mai-chat__composer-toolbar { align-items: stretch; flex-direction: column; }
		.mai-chat__composer-tools { flex-wrap: wrap; }
		.mai-chat__actions { justify-content: flex-end; }
		.mai-chat__header-right { gap: var(--mai-space-1); }
		.mai-chat__title { display: none; }
		.mai-chat__body { height: calc(100dvh - 12rem); }
	}
	.mai-chat__history-item {
		display: flex; width: 100%; min-width: 0; align-items: center;
		justify-content: space-between; gap: var(--mai-space-2);
		border: 0; padding: var(--mai-space-2); color: inherit;
		background: transparent; font: inherit; text-align: left; cursor: pointer;
	}
	.mai-chat__history-item:hover { background: var(--mai-color-surface-raised); }
	.mai-chat__history-item:focus-visible {
		outline: var(--mai-focus-ring-width) solid var(--mai-focus-ring-color);
		outline-offset: calc(-1 * var(--mai-focus-ring-width));
	}
</style>
