<script lang="ts">
	/**
	 * ObservationDetails — reusable bounded evidence details (card 10-web-evidence).
	 *
	 * The drawer content for one selected observation sample and/or one derived
	 * finding of the monitor surface (`10-monitor-api` wire views). Owns nothing:
	 *
	 * - Presentational + controlled: every byte shown arrives as a wire view prop;
	 *   the component fetches nothing and holds no store (C11: scoped controllers
	 *   only, no duplicated window/server state).
	 * - Explicit scope: `serverId` is a required prop (C01 rule 2); a row whose
	 *   `server_id` differs is refused as a value-free mismatch — never rendered,
	 *   never attached. No hidden global current-server target exists here.
	 * - Chat attachment hands off ONLY the bounded permitted context reference
	 *   (`observation:<uuid>` / `finding:<uuid>`, schema `context_ref`) via the
	 *   `onattach` callback; the embedding panel owns the one reviewed chat
	 *   submission seam (the DocumentsPanel pattern). The button renders only
	 *   when the parent actually wired `onattach` — never a silent no-op.
	 * - Finding re-evaluation is the parent's mutation (`POST …/findings/evaluate`
	 *   via its own API seam): this component emits `onevaluate` and renders the
	 *   parent-owned `evaluateResult` — and only for the pinned server, so a
	 *   cached round from a previously selected server can never be displayed.
	 * - States are truthful (C08 rule 6): empty history, unknown sample
	 *   (`quality='unknown'` — the collector could not observe), permission gap
	 *   (`quality='degraded'` / `*.read_gaps` — partial sample) and the rows
	 *   themselves are visibly different things. Timestamps render as the wire's
	 *   explicit UTC (ISO `…Z`, C01 rule 5); provenance (source/method/quality,
	 *   ids, dedup key) is always exposed.
	 * - Hostile evidence is inert by construction: `finding.detail`, summaries
	 *   and metric values render as escaped text nodes. There is no `{@html}`
	 *   anywhere in this component and no module-level `window`/`document`
	 *   access (windowed ADR §5 rule 4), so server render is safe.
	 *
	 * Wire types come from the reviewed generated client only (C08 rule 8).
	 */
	import type { components } from '../api/schema.generated';
	import Button from '../ui/Button.svelte';

	type ObservationView = components['schemas']['ObservationView'];
	type FindingView = components['schemas']['FindingView'];
	type EvaluateResult = components['schemas']['EvaluateResult'];

	/** Exactly what the chat seam may receive: identity + bounded ref, never a payload. */
	export type EvidenceAttachPayload = {
		server_id: string;
		kind: 'observation' | 'finding';
		context_ref: string;
	};

	/** The explicit target the parent must re-evaluate (C01 rule 2). */
	export type EvidenceEvaluatePayload = { server_id: string };

	interface Props {
		/** The pinned server scope of the surface embedding this component. */
		serverId: string;
		/** The selected observation sample row, or `null` when none is selected. */
		observation: ObservationView | null;
		/** The selected derived finding row, or `null` when none is selected. */
		finding: FindingView | null;
		/** Parent-owned re-evaluation round in flight (disables the trigger). */
		evaluateBusy?: boolean;
		/** Parent-owned last re-evaluation result; shown only for `serverId`. */
		evaluateResult?: EvaluateResult | null;
		/** Wired by the embedding surface; the attach control renders only then. */
		onattach?: (payload: EvidenceAttachPayload) => void;
		/** Wired by the embedding surface; the re-evaluate control renders only then. */
		onevaluate?: (payload: EvidenceEvaluatePayload) => void;
	}

	let {
		serverId,
		observation,
		finding,
		evaluateBusy = false,
		evaluateResult = null,
		onattach,
		onevaluate
	}: Props = $props();

	/** A row from another server scope is refused before any rendering. */
	const scopeMismatch = $derived(
		(observation !== null && observation.server_id !== serverId) ||
			(finding !== null && finding.server_id !== serverId)
	);

	const hasEvidence = $derived(observation !== null || finding !== null);

	/** `quality='unknown'`: no sample exists (metric/value/observed_at are null by CHECK). */
	const sampleUnknown = $derived(observation !== null && observation.quality === 'unknown');

	/**
	 * Permission boundary on the target: the collector caps such samples at
	 * `degraded` (partial /proc or cron visibility) and derives a `*.read_gaps`
	 * finding — the sample is partial, never silently complete (10-process /
	 * 10-cron acceptance 2).
	 */
	const permissionGap = $derived.by(() => {
		if (observation !== null) {
			if (observation.quality === 'degraded') return true;
			if (observation.metric === 'proc.unreadable') return true;
		}
		if (finding !== null && finding.kind.endsWith('.read_gaps')) return true;
		return false;
	});

	const rootState = $derived(
		scopeMismatch ? 'scope-mismatch' : hasEvidence ? 'ready' : 'empty'
	);

	/** Frozen wire vocabularies → badge variants (schema enum values, data-driven). */
	const SEVERITY_BADGE: Readonly<Record<string, string>> = Object.freeze({
		info: 'mai-evidence__badge--info',
		warning: 'mai-evidence__badge--warning',
		critical: 'mai-evidence__badge--danger'
	});
	const QUALITY_BADGE: Readonly<Record<string, string>> = Object.freeze({
		measured: 'mai-evidence__badge--success',
		degraded: 'mai-evidence__badge--warning',
		unknown: 'mai-evidence__badge--warning'
	});
	const FRESHNESS_BADGE: Readonly<Record<string, string>> = Object.freeze({
		fresh: 'mai-evidence__badge--success',
		stale: 'mai-evidence__badge--warning',
		unknown: 'mai-evidence__badge--neutral'
	});
</script>

<section
	class="mai-evidence"
	data-testid="evidence-details"
	data-state={rootState}
	data-quality={scopeMismatch || observation === null ? '' : observation.quality}
	data-permission-gap={permissionGap}
	aria-label="Observation evidence details"
>
	{#if scopeMismatch}
		<p class="mai-evidence__state mai-evidence__state--error" role="alert" data-testid="evidence-scope-mismatch">
			Evidence does not belong to the pinned server scope — refusing to render it here.
		</p>
	{:else}
		{#if !hasEvidence}
			<p class="mai-evidence__state" role="status" data-testid="evidence-empty">
				No evidence is recorded for this server scope yet. Collected observations and
				derived findings appear here once the monitor collects them — an empty history
				is not an unknown reading.
			</p>
		{:else}
			{#if sampleUnknown}
				<p class="mai-evidence__state mai-evidence__state--unknown" role="status" data-testid="evidence-unknown">
					No sample was taken — the collector could not observe this target
					(permission-refused or unreachable). This row is an unknown reading,
					not a zero value and not an empty history.
				</p>
			{/if}
			{#if permissionGap}
				<p class="mai-evidence__state mai-evidence__state--gap" role="status" data-testid="evidence-permission-gap">
					Permission boundary on the target: part of this sample was unreadable to
					the collector identity. The sample is partial (quality degraded) — it is
					never presented as complete.
				</p>
			{/if}

			{#if finding !== null}
				<section class="mai-evidence__row" aria-label="Finding" data-testid="evidence-finding">
					<header class="mai-evidence__rowhead">
						<span
							class="mai-evidence__badge {SEVERITY_BADGE[finding.severity] ?? 'mai-evidence__badge--info'}"
							data-testid="finding-severity">{finding.severity}</span>
						<span
							class="mai-evidence__badge {finding.open
								? 'mai-evidence__badge--warning'
								: 'mai-evidence__badge--neutral'}"
							data-testid="finding-state">{finding.open ? 'open' : 'resolved'}</span>
						<h4 class="mai-evidence__kind" data-testid="finding-kind">{finding.kind}</h4>
					</header>
					<p class="mai-evidence__summary" data-testid="finding-summary">
						{finding.summary === '' ? 'The collector recorded no summary for this condition.' : finding.summary}
					</p>
					{#if finding.detail !== null}
						<!-- Inert by construction: escaped text node, never {@html}. -->
						<div class="mai-evidence__detail" data-testid="finding-detail">
							<h5>Guidance</h5>
							<p>{finding.detail}</p>
						</div>
					{/if}
					<dl class="mai-evidence__facts" data-testid="finding-provenance">
						<div class="mai-evidence__fact">
							<dt>Finding</dt>
							<dd data-testid="finding-id">{finding.finding_id}</dd>
						</div>
						<div class="mai-evidence__fact">
							<dt>Dedup key</dt>
							<dd data-testid="finding-dedup-key">{finding.dedup_key}</dd>
						</div>
						<div class="mai-evidence__fact">
							<dt>Producing observation</dt>
							<dd data-testid="finding-observation-id">
								{finding.observation_id ?? 'No producing observation reference'}
							</dd>
						</div>
						{#if finding.application_id !== null}
							<div class="mai-evidence__fact">
								<dt>Application</dt>
								<dd data-testid="finding-application-id">{finding.application_id}</dd>
							</div>
						{/if}
						<div class="mai-evidence__fact">
							<dt>Opened (UTC)</dt>
							<dd data-testid="finding-opened-at">{finding.opened_at}</dd>
						</div>
						<div class="mai-evidence__fact">
							<dt>Updated (UTC)</dt>
							<dd data-testid="finding-updated-at">{finding.updated_at}</dd>
						</div>
						{#if !finding.open}
							<div class="mai-evidence__fact">
								<dt>Resolved (UTC)</dt>
								<dd data-testid="finding-resolved-at">{finding.resolved_at}</dd>
							</div>
						{/if}
					</dl>
					{#if onattach}
						<div class="mai-evidence__attach">
							<Button
								variant="secondary"
								onclick={() =>
									onattach?.({
										server_id: serverId,
										kind: 'finding',
										context_ref: finding.context_ref
									})}
								data-testid="finding-attach">Attach to chat</Button>
							<p class="mai-evidence__attachnote" data-testid="finding-attach-note">
								Attaches the bounded context reference {finding.context_ref} — never a payload.
							</p>
						</div>
					{/if}
				</section>
			{/if}

			{#if observation !== null}
				<section class="mai-evidence__row" aria-label="Observation sample" data-testid="evidence-observation">
					<header class="mai-evidence__rowhead">
						<span class="mai-evidence__badge mai-evidence__badge--neutral" data-testid="observation-source">
							source: {observation.source}
						</span>
						<span class="mai-evidence__badge mai-evidence__badge--neutral" data-testid="observation-method">
							method: {observation.method}
						</span>
						<span
							class="mai-evidence__badge {QUALITY_BADGE[observation.quality] ?? 'mai-evidence__badge--neutral'}"
							data-testid="observation-quality">quality: {observation.quality}</span>
						<span
							class="mai-evidence__badge {FRESHNESS_BADGE[observation.freshness] ?? 'mai-evidence__badge--neutral'}"
							data-testid="observation-freshness">freshness: {observation.freshness}</span>
					</header>
					{#if sampleUnknown}
						<p class="mai-evidence__muted" data-testid="observation-no-measurement">
							No measurement exists for this row (unknown quality).
						</p>
					{:else}
						<div class="mai-evidence__measurement" data-testid="observation-measurement">
							<span class="mai-evidence__metric" data-testid="observation-metric">{observation.metric}</span>
							<span class="mai-evidence__value" data-testid="observation-value">{observation.value}</span>
						</div>
					{/if}
					<dl class="mai-evidence__facts" data-testid="observation-provenance">
						<div class="mai-evidence__fact">
							<dt>Observation</dt>
							<dd data-testid="observation-id">{observation.observation_id}</dd>
						</div>
						{#if observation.application_id !== null}
							<div class="mai-evidence__fact">
								<dt>Application</dt>
								<dd data-testid="observation-application-id">{observation.application_id}</dd>
							</div>
						{/if}
						{#if observation.observed_at !== null}
							<div class="mai-evidence__fact">
								<dt>Observed (UTC)</dt>
								<dd data-testid="observation-observed-at">{observation.observed_at}</dd>
							</div>
						{/if}
						<div class="mai-evidence__fact">
							<dt>Collected (UTC)</dt>
							<dd data-testid="observation-collected-at">{observation.collected_at}</dd>
						</div>
						{#if observation.fresh_until !== null}
							<div class="mai-evidence__fact">
								<dt>Fresh until (UTC)</dt>
								<dd data-testid="observation-fresh-until">{observation.fresh_until}</dd>
							</div>
						{/if}
						<div class="mai-evidence__fact">
							<dt>Seq</dt>
							<dd data-testid="observation-seq">{observation.seq}</dd>
						</div>
					</dl>
					{#if onattach}
						<div class="mai-evidence__attach">
							<Button
								variant="secondary"
								onclick={() =>
									onattach?.({
										server_id: serverId,
										kind: 'observation',
										context_ref: observation.context_ref
									})}
								data-testid="observation-attach">Attach to chat</Button>
							<p class="mai-evidence__attachnote" data-testid="observation-attach-note">
								Attaches the bounded context reference {observation.context_ref} — never a payload.
							</p>
						</div>
					{/if}
				</section>
			{/if}
		{/if}

		{#if onevaluate}
			<section class="mai-evidence__evaluate" aria-label="Finding re-evaluation" data-testid="evidence-evaluate">
				<Button
					loading={evaluateBusy}
					onclick={() => onevaluate({ server_id: serverId })}
					data-testid="evaluate-button">Re-evaluate findings</Button>
				{#if evaluateResult !== null && evaluateResult.server_id === serverId}
					<p class="mai-evidence__evaluatesummary" data-testid="evaluate-summary">
						Evaluated {evaluateResult.evaluated} open finding(s) for this server.
					</p>
					<ul class="mai-evidence__outcomes" data-testid="evaluate-outcomes">
						{#each evaluateResult.outcomes as outcome (outcome.alert_id)}
							<li class="mai-evidence__outcome" data-testid="evaluate-outcome">
								<span data-testid="evaluate-outcome-dedup">{outcome.dedup_key}</span>
								<span data-testid="evaluate-outcome-disposition">{outcome.disposition}</span>
								<span data-testid="evaluate-outcome-delivery">{outcome.delivery}</span>
								<span data-testid="evaluate-outcome-channels">{outcome.channels_attempted.join(', ')}</span>
							</li>
						{/each}
					</ul>
				{/if}
			</section>
		{/if}
	{/if}
</section>

<style>
	/*
	 * Tokens only (ADR §2): colors, spacing, type come from ui/tokens.css
	 * custom properties; this component redeclares no theme values.
	 */
	.mai-evidence {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
	}

	.mai-evidence__state {
		padding: var(--mai-space-3);
		border: 1px solid var(--mai-status-neutral-border);
		border-radius: var(--mai-radius-md);
		background: var(--mai-status-neutral-bg);
		color: var(--mai-status-neutral-text);
		font-size: var(--mai-text-sm);
	}

	.mai-evidence__state--error {
		border-color: var(--mai-status-danger-border);
		background: var(--mai-status-danger-bg);
		color: var(--mai-status-danger-text);
	}

	.mai-evidence__state--unknown,
	.mai-evidence__state--gap {
		border-color: var(--mai-status-warning-border);
		background: var(--mai-status-warning-bg);
		color: var(--mai-status-warning-text);
	}

	.mai-evidence__row {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-space-3);
		border: 1px solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		background: var(--mai-color-surface);
	}

	.mai-evidence__rowhead {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--mai-space-2);
	}

	.mai-evidence__kind {
		font-size: var(--mai-text-base);
		font-weight: var(--mai-weight-semibold);
		color: var(--mai-color-text);
	}

	.mai-evidence__badge {
		display: inline-block;
		padding: 0.1rem var(--mai-space-2);
		border: 1px solid var(--mai-status-neutral-border);
		border-radius: 999px;
		background: var(--mai-status-neutral-bg);
		color: var(--mai-status-neutral-text);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
	}

	.mai-evidence__badge--info {
		border-color: var(--mai-status-info-border);
		background: var(--mai-status-info-bg);
		color: var(--mai-status-info-text);
	}

	.mai-evidence__badge--success {
		border-color: var(--mai-status-success-border);
		background: var(--mai-status-success-bg);
		color: var(--mai-status-success-text);
	}

	.mai-evidence__badge--warning {
		border-color: var(--mai-status-warning-border);
		background: var(--mai-status-warning-bg);
		color: var(--mai-status-warning-text);
	}

	.mai-evidence__badge--danger {
		border-color: var(--mai-status-danger-border);
		background: var(--mai-status-danger-bg);
		color: var(--mai-status-danger-text);
	}

	.mai-evidence__summary {
		color: var(--mai-color-text);
		font-size: var(--mai-text-sm);
	}

	.mai-evidence__detail {
		border-left: 3px solid var(--mai-color-border-strong);
		padding-left: var(--mai-space-3);
	}

	.mai-evidence__detail h5 {
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-semibold);
		color: var(--mai-color-text-muted);
		text-transform: uppercase;
	}

	.mai-evidence__detail p,
	.mai-evidence__muted {
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-sm);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}

	.mai-evidence__facts {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(14rem, 1fr));
		gap: var(--mai-space-2);
		margin: 0;
	}

	.mai-evidence__fact dt {
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}

	.mai-evidence__fact dd {
		margin: 0;
		color: var(--mai-color-text);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		overflow-wrap: anywhere;
	}

	.mai-evidence__measurement {
		display: flex;
		align-items: baseline;
		gap: var(--mai-space-2);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-sm);
	}

	.mai-evidence__metric {
		color: var(--mai-color-text-muted);
	}

	.mai-evidence__value {
		color: var(--mai-color-text);
		font-weight: var(--mai-weight-medium);
		overflow-wrap: anywhere;
	}

	.mai-evidence__attach {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
	}

	.mai-evidence__attachnote,
	.mai-evidence__evaluatesummary {
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-xs);
		overflow-wrap: anywhere;
	}

	.mai-evidence__evaluate {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
	}

	.mai-evidence__outcomes {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.mai-evidence__outcome {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mai-space-2);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text);
	}
</style>
