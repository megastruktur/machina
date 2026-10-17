# mai-08-recipe-schema — evidence index (fix round 1)

**Card:** P08 · service `applications` · risk `medium` · paired review `r-08-recipe-schema`
**Slice:** `backend/src/mai/applications/recipes.py`,
`backend/tests/test_recipes.py` — allowlist-complete
**Worktree/branch:** `/home/megastruktur/orca/workspaces/machina/mai-08-recipe-schema`
· `megastruktur/mai-08-recipe-schema`
**Base HEAD:** `39fc79b` (reviewed snapshot; reviewer FAIL: '$' interpolation)
**Status:** FIX ROUND 1 COMPLETE — reviewer finding fixed at the parse
boundary, red-first negative controls recorded (4 failed at base → green),
focused suite 178 passed, ruff clean, benign rendering byte-identical to base.

## Review finding addressed

'$'-bearing strings (`${VAR}`, `$VAR`, any '$') passed `container_path` and
`health.path` validation into the rendered Compose YAML, where Docker
Compose interpolates them at apply time — contradicting the module's
no-interpolation invariant and C09 rule 3 plan honesty; the T-11 render
test only asserted '$' absent on the benign fixture.

**Fix (parse/validation time, no escaping — '$' is rejected outright):**
- `_validated_container_path`: adds `"$" in value` to the
  `invalid_volume_path` rejection; docstring states the no-interpolation
  rationale (paths render into the deployment Compose document).
- `_parse_health` path check: adds `"$" in path` to the
  `invalid_health_check` rejection.
- `container_path` and `health.path` were the only free-form strings in
  `render_compose` output; all other rendered strings are regex-bound,
  vocabulary-bound, int, or frozen constants (enumeration in
  `03-negative-controls.txt`), so '$' is now structurally impossible in
  the rendered artifact for any parsed recipe.

## Index

| File | Content |
|---|---|
| `README.md` | This index: finding, fix, acceptance mapping, provenance/limitations |
| `02-focused-tests.txt` | Red-first receipt (4 '$' cases FAILED at base, `DID NOT RAISE`), green full-suite run `pytest tests/test_recipes.py -v` — **178 passed**, ruff clean, parse-boundary probe transcript, mypy not-configured note |
| `03-negative-controls.txt` | The four '$' negative controls (exact inputs, codes), full `-k unsafe` selection — **17 passed**, and the render_compose free-string field enumeration proving closure |
| `06-post-commit-receipt.txt` | Post-commit SHA, parent, diffstat, clean-tree proof (written after commit) |

## Acceptance criteria → evidence

| AC | Evidence |
|---|---|
| 2. Policy-bound plan from validated inputs; no arbitrary hidden shell in declarative field | '$' rejection at parse (this round, `02`/`03`); existing argv-only healthcheck, digest-only image pin, unknown-field rejection of command/script fields (base suite, unchanged) |
| 1. Recipe declares prerequisites/data/health/update/removal/recovery semantics and supported environment | Base suite (unchanged this round): strict-shape section tests, vocabulary refusals, golden plan fixture — 178 passed |

## Provenance and limitations

- All commands ran from `backend/` in the feature worktree above
  (branch `megastruktur/mai-08-recipe-schema`, base `39fc79b`) on
  2026-10-16 ~23:40–23:58Z; red run produced by stashing only
  `src/mai/applications/recipes.py` (tests present, fix absent), restored
  and re-verified before the green run.
- Pure module, pure tests: no fixtures, no database, no docker, no network
  (unchanged posture from the base slice); the parse-boundary probe is a
  throwaway heredoc, not persisted.
- `mypy` is not configured for this backend (no dependency in
  `backend/pyproject.toml`) — not run; `ruff check` on both changed files
  is clean.
- Benign golden fixtures (`test_minimal_compose_exact_fixture`,
  `test_compose_parse_back_matches_fixture`) are untouched and pass, so
  the fix changes no accepted rendering — byte-identical to base.
