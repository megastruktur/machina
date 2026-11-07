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
