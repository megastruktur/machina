# mai-07-deep-research — evidence (fix round 1, 2026-10-16)

## Target
Card `mai-07-deep-research` (branch `megastruktur/mai-07-deep-research`),
fix round after independent review FAIL on `a933359` with one finding:

The Phase-5 logs step in `backend/src/mai/research/workflow.py` computed
the artifact handle list (`refs`) from each `host.logs` outcome and then
discarded it: `SourceEvidence` had no artifact-ref field, the succeeded
logs `StepRecord.evidence_refs` stayed empty, and nothing propagated
into the canonical incident document built from settled steps — while
the module docstring advertises log evidence as "(read-only,
artifact-referenced)". The recorded source therefore could not be traced
back to the persisted evidence artifact it was excerpted from, in the
report views or in a promoted incident.

## Implementation (fix delta vs a933359, allowlisted files only)
- `backend/src/mai/research/workflow.py`
  - `SourceEvidence` gains `artifact_refs: tuple[str, ...] = ()` with a
    comment pinning the handle shape (`artifact:<path>#<sha256>`).
  - New module helper `_log_artifact_refs(document)`: single extraction
    point for the handle from a parsed `host.logs` document (missing /
    non-mapping / path-less artifact → empty tuple).
  - `_record_capability` gains an optional `refs_from_document` seam; on
    success the derived refs become the step record's `evidence_refs`
    (same 16-ref bound via `_add_step`), so `_incident_payload` — built
    verbatim from settled steps — carries the handle into the C07
    rule-5 incident document with no further wiring.
  - Logs phase: passes `refs_from_document=_log_artifact_refs` and
    attaches the same handle to the appended `SourceEvidence`.
  - `report_document` serializes `"artifact_refs"` per source; frozen
    key order preserved (inserted before `"text"`).
  - `render_report` emits `- artifact_refs: <handles>` under a source
    when present (steps already rendered their evidence refs).
- `backend/tests/test_research_workflow.py`
  - `test_log_artifact_handles_preserved_on_source_step_and_report` —
    handle pinned on SourceEvidence, logs StepRecord, JSON report view
    and both Markdown sections.
  - `test_log_artifact_handle_propagates_into_promoted_incident` —
    failing synthesis after a successful logs phase: the promoted
    incident (real DocGenerator + DocumentStore) cites the handle.

No behavior change for non-log phases (`refs_from_document` defaults to
None → empty refs, byte-identical step records). Denied/unavailable
log outcomes keep their empty-ref failed steps.

## Verification
- `uv run pytest tests/test_research_workflow.py -v` — 47 passed
  (45 pre-existing + 2 new). Full listing: `02-focused-tests.txt`.
- `ruff check`: 3 pre-existing F401 in the test module, identical on
  HEAD; untouched. `ruff format --diff`: content-identical to HEAD's
  drift; zero new drift from the fix.
- Receipts: `02-focused-tests.txt`, `06-post-commit-receipt.txt`.

## Limitations
Seam-level proof with injected executor fakes per the card's offline
discipline; incident promotion exercised through the real 07-doc-store
producers. No full-stack rerun (owned by 07-verify).
