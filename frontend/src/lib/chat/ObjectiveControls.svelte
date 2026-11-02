<script module lang="ts">
	/**
	 * ObjectiveControls — state-specific steer/pause/cancel/approve/resume/
	 * reconcile controls (runtime-v2.md §9; rv_activity-ui AC3).
	 *
	 * Resume here is NEVER a generic replay button. The frozen §9 gate is
	 * rendered verbatim:
	 *  - `waiting_children` → no manual resume: child completion is the only
	 *    producer that wakes the run;
	 *  - `waiting_approval` → the resume is the payload-bound approval
	 *    decision (the panel's approval section renders it);
	 *  - `reconciling` / open unknown effects → classification FIRST; the
	 *    generic resume is disabled and the operator classifies each open
	 *    step (`confirm_applied` / `confirm_not_applied` / `cancel`);
	 *  - `queued`/`running` → the run is executing; steering is the control;
	 *  - settled/paused → the resume IS the next submitted turn (message).
	 *
	 * The classification submit posts through the ONE reconcile route; the
	 * run is never re-executed by it. All controls are native elements
	 * (keyboard operable) with visible focus and honest busy/error states.
	 */
	import type {
		ConversationReconcileView,
		StepClassificationBody
	} from '../api/client.js';
</script>

<script lang="ts">
	let {
		runState = null,
		openUnknowns = [],
		busy = false,
		onresume,
		onreconcile,
		reconcileResult = null,
		problem = null
	}: {
		/** The primary run's replayed state (null = not recorded). */
		runState?: string | null;
		/** Open outcome_unknown steps (run receipts), oldest first. */
		openUnknowns?: Array<{ seq: number; capabilityId: string }>;
		busy?: boolean;
		/** Settled/paused resume: the next submitted turn. */
		onresume: (message: string) => void;
		/** Classification submit (the ONE reconcile route). */
		onreconcile: (classifications: StepClassificationBody[]) => void;
		reconcileResult?: ConversationReconcileView | null;
		problem?: string | null;
	} = $props();

	let resumeMessage = $state('');
	let classifications = $state<Record<number, StepClassificationBody['verdict']>>({});

	const hasOpenUnknowns = $derived(openUnknowns.length > 0);
	const reconciling = $derived(runState === 'reconciling' || hasOpenUnknowns);

	/** The honest resume gate per §9 (null = a resume control may show). */
	const resumeRefusal = $derived.by<string | null>(() => {
		if (reconciling) return 'classification_required';
		if (runState === 'waiting_children') return 'resume_rejected';
		if (runState === 'waiting_approval') return 'approval_decision_required';
		if (runState === 'queued' || runState === 'running') return 'resume_not_applicable';
		return null;
	});

	const resumeRefusalText = $derived.by(() => {
		if (resumeRefusal === 'classification_required')
			return 'Unknown effects NEVER auto-resume: classify each open step first.';
		if (resumeRefusal === 'resume_rejected')
			return 'No manual resume: child completion is the producer that wakes this run.';
		if (resumeRefusal === 'approval_decision_required')
			return 'The ONE resume is the payload-bound approval decision below.';
		if (resumeRefusal === 'resume_not_applicable')
			return 'The run is executing; steering is the control.';
		return '';
	});

	const canResume = $derived(
		!reconciling &&
			(runState === null || runState === 'paused' || runState === 'succeeded' || runState === 'failed' || runState === 'cancelled') &&
			resumeMessage.trim() !== '' &&
			!busy
	);

	function submitResume(): void {
		if (!canResume) return;
		onresume(resumeMessage.trim());
		resumeMessage = '';
	}

	function submitReconcile(): void {
		if (busy) return;
		const entries: StepClassificationBody[] = [];
		for (const step of openUnknowns) {
			const verdict = classifications[step.seq];
			if (verdict === undefined) return; // every open step must be classified
			entries.push({ seq: step.seq, verdict });
		}
		onreconcile(entries);
		classifications = {};
	}

	function setVerdict(seq: number, verdict: StepClassificationBody['verdict']): void {
		classifications = { ...classifications, [seq]: verdict };
	}

	const allClassified = $derived(
		openUnknowns.length > 0 &&
			openUnknowns.every((step) => classifications[step.seq] !== undefined)
	);
</script>

<section
	class="acui-controls"
	aria-label="Objective run controls"
	data-testid="objective-controls"
>
	{#if problem !== null}
		<p class="acui-controls__problem" role="alert" data-testid="objective-controls-problem">
			{problem}
		</p>
	{/if}

	{#if reconciling}
		<div class="acui-controls__reconcile" data-testid="reconcile-section">
			<p class="acui-controls__refusal" role="status" data-testid="reconcile-refusal">
				Outcome unknown — blind resume is disabled. Classify every open
				effect; the run is never re-executed by this route.
			</p>
			<ul class="acui-controls__steps" data-testid="reconcile-steps">
				{#each openUnknowns as step (step.seq)}
					<li class="acui-controls__step" data-testid="reconcile-step" data-seq={step.seq}>
						<span class="acui-controls__step-label">
							Step #{step.seq} · {step.capabilityId}
						</span>
						<fieldset class="acui-controls__verdicts">
							<legend class="acui-sr-only">Classification of step {step.seq}</legend>
							<label class="acui-controls__verdict">
								<input
									type="radio"
									name="reconcile-{step.seq}"
									value="confirm_applied"
									checked={classifications[step.seq] === 'confirm_applied'}
									onchange={() => setVerdict(step.seq, 'confirm_applied')}
									data-testid="reconcile-verdict-applied"
								/>
								Confirm applied
							</label>
							<label class="acui-controls__verdict">
								<input
									type="radio"
									name="reconcile-{step.seq}"
									value="confirm_not_applied"
									checked={classifications[step.seq] === 'confirm_not_applied'}
									onchange={() => setVerdict(step.seq, 'confirm_not_applied')}
									data-testid="reconcile-verdict-not-applied"
								/>
								Confirm not applied
							</label>
							<label class="acui-controls__verdict">
								<input
									type="radio"
									name="reconcile-{step.seq}"
									value="cancel"
									checked={classifications[step.seq] === 'cancel'}
									onchange={() => setVerdict(step.seq, 'cancel')}
									data-testid="reconcile-verdict-cancel"
								/>
								Cancel
							</label>
						</fieldset>
					</li>
				{/each}
			</ul>
			<button
				type="button"
				class="acui-controls__btn acui-controls__btn--primary"
				disabled={!allClassified || busy}
				onclick={submitReconcile}
				data-testid="reconcile-apply"
			>
				{busy ? 'Recording classification…' : 'Record classification and wake the run'}
			</button>
		</div>
	{:else if resumeRefusal !== null}
		<p class="acui-controls__refusal" role="status" data-testid="resume-refusal">
			{resumeRefusalText}
		</p>
	{:else}
		<div class="acui-controls__resume" data-testid="resume-section">
			<label class="acui-sr-only" for="acui-resume-message">Resume message</label>
			<input
				id="acui-resume-message"
				type="text"
				class="acui-controls__input"
				placeholder="Continuation instruction for the resumed run"
				bind:value={resumeMessage}
				data-testid="resume-message"
				onkeydown={(event) => {
					if (event.key === 'Enter') {
						event.preventDefault();
						submitResume();
					}
				}}
			/>
			<button
				type="button"
				class="acui-controls__btn acui-controls__btn--primary"
				disabled={!canResume}
				onclick={submitResume}
				data-testid="resume-submit"
			>
				{busy ? 'Recording…' : 'Resume (next submitted turn)'}
			</button>
		</div>
		<p class="acui-controls__hint">
			The resume starts FROM the last settled checkpoint with your message —
			never a replay of effects.
		</p>
	{/if}

	{#if reconcileResult !== null}
		<div class="acui-controls__result" data-testid="reconcile-result" role="status">
			{#each reconcileResult.results as result (result.seq)}
				<span class="acui-controls__meta">
					Step #{result.seq}: {result.verdict} → {result.state}
					{result.wake_sent ? '· run woken' : '· no wake sent'}
				</span>
			{/each}
		</div>
	{/if}
</section>

<style>
	.acui-controls {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		padding: 0.45rem 0.6rem;
		border: 1px solid var(--border, #4444);
		border-radius: 0.5rem;
	}
	.acui-controls__problem {
		margin: 0;
		font-size: 0.75rem;
		color: var(--error, #c44);
	}
	.acui-controls__refusal {
		margin: 0;
		font-size: 0.75rem;
		color: var(--warning, #c90);
	}
	.acui-controls__reconcile {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}
	.acui-controls__steps {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}
	.acui-controls__step {
		border: 1px solid var(--border, #4444);
		border-radius: 0.4rem;
		padding: 0.3rem 0.45rem;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	.acui-controls__step-label {
		font-size: 0.72rem;
		font-weight: 600;
	}
	.acui-controls__verdicts {
		border: 0;
		padding: 0;
		margin: 0;
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem 0.8rem;
	}
	.acui-controls__verdict {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		font-size: 0.72rem;
	}
	.acui-controls__resume {
		display: flex;
		flex-wrap: wrap;
		gap: 0.35rem;
	}
	.acui-controls__input {
		flex: 1 1 12rem;
		min-width: 0;
		font: inherit;
		font-size: 0.75rem;
		padding: 0.25rem 0.45rem;
		border-radius: 0.4rem;
		border: 1px solid var(--border, #4444);
		background: transparent;
		color: inherit;
	}
	.acui-controls__input:focus-visible {
		outline: 2px solid var(--accent, #888);
		outline-offset: 1px;
	}
	.acui-controls__btn {
		font: inherit;
		font-size: 0.75rem;
		padding: 0.25rem 0.7rem;
		border-radius: 999px;
		border: 1px solid var(--border, #4444);
		background: transparent;
		color: inherit;
		cursor: pointer;
	}
	.acui-controls__btn--primary {
		border-color: var(--accent, #888);
		color: var(--accent, inherit);
	}
	.acui-controls__btn:disabled {
		cursor: default;
		color: var(--muted, inherit);
		border-color: var(--border, #4444);
	}
	.acui-controls__btn:focus-visible {
		outline: 2px solid var(--accent, #888);
		outline-offset: 1px;
	}
	.acui-controls__hint {
		margin: 0;
		font-size: 0.68rem;
		color: var(--muted, inherit);
	}
	.acui-controls__result {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
	.acui-controls__meta {
		font-size: 0.72rem;
		color: var(--muted, inherit);
	}
	.acui-sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
		border: 0;
	}
</style>
