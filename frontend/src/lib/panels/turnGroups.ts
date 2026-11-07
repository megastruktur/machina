import type { ChannelEntry } from '../chat/session_client.js';

/**
 * CTR-W2: the turn-group view model for the session transcript.
 *
 * The flat `visibleEntries` stream (seq-ordered, already filtered by the
 * panel) is projected into RENDER UNITS: either a standalone row or one
 * turn group — an assistant message (the lead) plus the `tool_result` /
 * `delegation` rows that belong to it, rendered under a left rail and
 * indented deeper than the lead's prose.
 *
 * Grouping rules (pure projection — no reordering, no dropping):
 *  - A `tool_result` belongs to the lead assistant message of the SAME
 *    run whose `tool_calls[].call_id` equals the result's `call_id`.
 *  - A `delegation` belongs to the lead assistant message of the same
 *    run (the delegation card is the assistant's own action).
 *  - Rows with a missing/empty `run_id` never group — synthetic
 *    fixtures and unscoped history stay flat exactly as before W2.
 *  - Children are only ever the CONTIGUOUS rows following the lead: the
 *    first non-qualifying row closes the group and starts a new unit.
 *    This keeps the rendered DOM order IDENTICAL to seq order — a
 *    result separated from its requesting message by unrelated rows
 *    renders top-level instead of reordering the transcript. (The
 *    backend admission contract settles each result immediately after
 *    the round that requested it, so contiguity holds for well-formed
 *    streams; the orphan branch is the safety net for edge windows.)
 *  - `user_message` rows always stand alone (they never qualify as
 *    children and never open a group).
 */

export type TurnUnit =
	| { kind: 'row'; key: number; entry: ChannelEntry }
	| {
			kind: 'group';
			key: number;
			lead: ChannelEntry;
			runId: string;
			children: ChannelEntry[];
	  };

function declaredCallIds(payload: ChannelEntry['payload']): Set<string> {
	if (payload.kind !== 'assistant_message') return new Set();
	const calls = payload.tool_calls ?? [];
	const ids = new Set<string>();
	for (const call of calls) {
		if (typeof call.call_id === 'string' && call.call_id !== '') {
			ids.add(call.call_id);
		}
	}
	return ids;
}

function isTurnChild(
	candidate: ChannelEntry,
	runId: string,
	declared: ReadonlySet<string>
): boolean {
	// A row without run scope can never be attached to a run's turn.
	if (candidate.run_id == null || String(candidate.run_id) !== runId) return false;
	const payload = candidate.payload;
	if (payload.kind === 'delegation') return true;
	if (payload.kind !== 'tool_result') return false;
	// Only the results the lead actually requested (call_id pairing).
	return typeof payload.call_id === 'string' && declared.has(payload.call_id);
}

export function buildTurnUnits(rows: readonly ChannelEntry[]): TurnUnit[] {
	const units: TurnUnit[] = [];
	let index = 0;
	while (index < rows.length) {
		const lead = rows[index]!;
		const leadRun = lead.run_id;
		if (
			lead.payload.kind !== 'assistant_message' ||
			leadRun == null ||
			String(leadRun) === ''
		) {
			units.push({ kind: 'row', key: lead.seq, entry: lead });
			index += 1;
			continue;
		}
		const runId = String(leadRun);
		const declared = declaredCallIds(lead.payload);
		const children: ChannelEntry[] = [];
		let cursor = index + 1;
		while (cursor < rows.length && isTurnChild(rows[cursor]!, runId, declared)) {
			children.push(rows[cursor]!);
			cursor += 1;
		}
		if (children.length === 0) {
			units.push({ kind: 'row', key: lead.seq, entry: lead });
		} else {
			units.push({ kind: 'group', key: lead.seq, lead, runId, children });
		}
		index = cursor;
	}
	return units;
}

/**
 * CTR-W3: one capability/status word pair in a tool group's summary.
 * `unsettled` marks an unproven outcome (warn styling + open body).
 */
export type ToolSummaryItem = {
	capabilityId: string;
	status: string;
	unsettled: boolean;
};

/**
 * CTR-W3: a group's children split into RENDER SEGMENTS, in seq order:
 * each maximal run of consecutive `tool_result` rows becomes ONE
 * `tools` segment — rendered as a single collapsed disclosure whose
 * summary carries every capability and its status WORD — while a
 * `delegation` stays its own one-line row. Segment order always equals
 * seq order, so a delegation interleaved between two results of the
 * same round splits the disclosure rather than reordering the DOM.
 */
export type TurnChildSegment =
	| { kind: 'tools'; entries: ChannelEntry[]; summary: ToolSummaryItem[] }
	| { kind: 'row'; entry: ChannelEntry };

export function buildTurnChildSegments(
	lead: ChannelEntry,
	children: readonly ChannelEntry[]
): TurnChildSegment[] {
	const segments: TurnChildSegment[] = [];
	const runs: ChannelEntry[][] = [];
	for (const child of children) {
		if (child.payload.kind !== 'tool_result') {
			segments.push({ kind: 'row', entry: child });
			continue;
		}
		const last = segments[segments.length - 1];
		if (last !== undefined && last.kind === 'tools') {
			last.entries.push(child);
		} else {
			const entries: ChannelEntry[] = [child];
			segments.push({ kind: 'tools', entries, summary: [] });
			runs.push(entries);
		}
	}
	if (runs.length === 0) return segments;
	// The summary joins the lead's DECLARED calls (ordered) with the
	// results: a settled call shows its result's status; a declared
	// call with no result anywhere is honestly `pending` and attaches
	// to the first tools run (the round is still settling there).
	const settledCalls = new Set<string>();
	for (const run of runs) {
		for (const result of run) {
			const callId = result.payload.kind === 'tool_result' ? result.payload.call_id : null;
			if (typeof callId === 'string') settledCalls.add(callId);
		}
	}
	const declared =
		lead.payload.kind === 'assistant_message' ? (lead.payload.tool_calls ?? []) : [];
	for (const segment of segments) {
		if (segment.kind !== 'tools') continue;
		const items: ToolSummaryItem[] = [];
		for (const call of declared) {
			const result = segment.entries.find(
				(entry) =>
					entry.payload.kind === 'tool_result' && entry.payload.call_id === call.call_id
			);
			if (result === undefined && (settledCalls.has(call.call_id) || runs[0] !== segment.entries)) {
				// Its result lives in another run, or the pending slot
				// belongs to the first run only.
				continue;
			}
			const status =
				result !== undefined && result.payload.kind === 'tool_result'
					? String(result.payload.status)
					: 'pending';
			items.push({ capabilityId: call.capability_id, status, unsettled: status !== 'executed' });
		}
		segment.summary = items;
	}
	return segments;
}
