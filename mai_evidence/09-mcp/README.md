# mai-09-mcp — evidence index (fix round 1)

**Card:** P09 · service `plugins` · risk `high` · paired code review `r-09-mcp`
**Slice:** `backend/src/mai/plugins/mcp.py`, `backend/tests/test_mcp.py` —
allowlist-complete
**Worktree/branch:** `/home/megastruktur/orca/workspaces/machina/mai-09-mcp`
· `megastruktur/mai-09-mcp`
**Base HEAD:** `c3ecffd` (reviewed snapshot; reviewer FAIL: unbounded
tools/list pagination)
**Status:** FIX ROUND 1 COMPLETE — reviewer finding fixed with a page-count
bound + wall-clock deadline, red-first negative controls recorded
(endless-cursor discovery hung at base, killed at 120s → green bounded
rejection), focused suite 48 passed, ruff clean (incl. 2 pre-existing F821
fixed).

## Review finding addressed

`McpServerRuntime.discover` (backend/src/mai/plugins/mcp.py, the
`tools/list` pagination loop around lines 855-865 at base) followed
`result.nextCursor` in a bare `while True` with only the `max_tools`
tool-count bound. An untrusted MCP server returning **empty pages with a
perpetual `nextCursor`** never adds tools, so the count bound cannot trip
and discovery hangs indefinitely — violating the card's bounded-failure
acceptance (AC2).

**Fix (two independent bounds inside the existing listing `try`, so the
failure stays the established structured CONNECT_FAILED-class rejection):**
- `MAX_LIST_PAGES = 128` (new module constant, exported): at most 128
  `tools/list` page requests per discovery; exceeding raises the new
  internal `DiscoveryLimitExceeded` (defined; the name was already in
  `__all__` but never defined at base — a latent broken export this fix
  completes), normalized to `McpBridgeError(CONNECT_FAILED, "stdio tool
  listing failed (tools/list exceeded the 128-page bound)")`.
- `LIST_DEADLINE_SECONDS = 60.0` (new module constant, exported): the
  whole paginated listing phase runs under a `time.monotonic()`-anchored
  wall-clock budget; tripping yields
  `"... (tools/list exceeded the 60.0s listing deadline)"`.
- Rationale for constants over config fields: the frozen
  `SERVER_CONFIG_SCHEMA` is an approved contract surface; controller-side
  robustness bounds are not per-server tunables (a host that widens them
  per server would re-open the hang). 128 pages at the SDK's page size is
  orders of magnitude past any legitimate surface (`max_tools` caps at
  1024); 60s is half the default per-request call timeout.
- Value-free discipline preserved: rejection details carry only
  controller-side bound names/numbers, never server-supplied text; the
  session stack is closed by the existing `finally` on both bound trips.
- Drive-by (required by the verify gate): missing
  `CapabilityDescriptor` import added — `__future__.annotations` made the
  two F821s annotation-only latent at base (ruff at c3ecffd reports them;
  suite stayed green because nothing evaluated the annotations). No
  behavior change.

## Index

| File | Content |
|---|---|
| `README.md` | This index: finding, fix, acceptance mapping, provenance/limitations |
| `02-focused-tests.txt` | Red receipt (endless-cursor hangs at base, `timeout` kill rc=124), green run `pytest tests/test_mcp.py -v` — **48 passed**, ruff clean + pre-existing F821 note |
| `03-negative-controls.txt` | The two new bounded-listing negative controls (endless cursor, slow pager under tightened deadline), mutation-sensitivity rationale, four-axis bound completeness |
| `06-post-commit-receipt.txt` | Post-commit SHA, parent, diffstat, clean-tree proof (written after the fix commit) |

## Acceptance criteria → evidence

| AC | Evidence |
|---|---|
| 1. Actual transport initializes/discovers valid schemas, only explicitly granted tools exposed | Base suite (unchanged this round): real stdio + streamable-HTTP round trips, grant-boundary tests — green in the 48-passed run |
| 2. Connection/reconnect failure bounded, malformed tools and prompt-like content do not bypass policy; subprocess credential env scoped | **This fix**: endless-cursor and slow-pager listing now bounded structured CONNECT_FAILED rejections (`02`/`03`); base bounds re-run green: spawn failure, initialize timeout, reconnect journey (denied tool never executes), credential env scoping |

## Provenance and limitations

- All commands ran from `backend/` in the feature worktree above
  (branch `megastruktur/mai-09-mcp`, base `c3ecffd`) on 2026-10-17
  ~13:05–13:25Z; red run executed before `src/mai/plugins/mcp.py` was
  touched (no stash needed — hang proof captured, then fix applied, then
  green).
- Real-boundary proofs: both new controls drive a real fixture MCP
  server subprocess over real stdio (mcp SDK 1.30.0), no mocks; the
  deadline control temporarily tightens the module constant
  (`LIST_DEADLINE_SECONDS = 0.5`) and restores it in a `finally`.
- The deadline bound is exercised at 0.5s in-test, not at its production
  value (60s); production-value behavior is the same code path with a
  larger budget (limitation accepted: waiting 60s per CI run is not
  meaningful added proof).
- `mypy` is not configured for this backend (no mypy dependency in
  `backend/pyproject.toml`) — not run (same posture as the base slice and
  08-recipe-schema R1).
- The two pre-existing F821 ruff errors at base are fixed in this commit
  (import-only, no behavior change); no other drive-by changes.
