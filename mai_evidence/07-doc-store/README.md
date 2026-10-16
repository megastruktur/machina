# mai-07-doc-store — evidence (fix round 1, 2026-10-16)

## Target
Card `mai-07-doc-store` (branch `megastruktur/mai-07-doc-store`), fix round
after independent review FAIL on `499fcdb` with two findings:

1. The CLI `write ROOT ... DOC_ID FILE` door in
   `backend/src/mai/knowledge/documents.py` read `Path(args.file)` with a
   bare `read_text()`, bypassing the credential-material name deny-list and
   symlink refusal that `import_document` enforced — while the module
   docstring claimed import was "the only operation that ever reads a
   caller-supplied path outside the data root". An operator (or wrapper)
   could `write` a `.env`/key file, or a symlink that a concurrent swap
   redirects, straight into the canonical store through the supposedly
   guarded boundary.
2. `main()` constructed `DocumentStore(args.root)` outside the `try`
   block, so a root-creation IO error (e.g. root below a non-directory)
   escaped as an uncaught traceback instead of the documented exit-2
   `usage_or_io` contract.

## Implementation (fix delta vs 499fcdb, allowlisted files only)
- `backend/src/mai/knowledge/documents.py`
  - New module helper `_read_outside_source(source) -> bytes`: the single
    guarded outside-read seam — secret-name deny-list
    (`secret_file_forbidden`), `lstat` + symlink refusal
    (`symlink_forbidden`), non-regular refusal (`not_a_file`), unreadable
    refusal (`not_readable`). Never follows a swap-able alias; no content
    scanning (C03 rule 6).
  - `import_document` rewired through `_read_outside_source` (behavior
    unchanged; the guard logic now lives in one place).
  - CLI `write` FILE door now reads via `_checked_document(
    _read_outside_source(Path(args.file)))` — the identical guards as
    import; a refused write materializes nothing.
  - `main()`: `DocumentStore(args.root)` moved inside the `try`, so
    root-creation `OSError` maps to exit 2 (`usage_or_io`), no traceback.
  - Module docstring secret-boundary bullet, `import_document` docstring
    and the `write` FILE `--help` text updated to state both outside-read
    seams honestly ("the two outside-read seams — the explicit import and
    the CLI write FILE argument, the only operations that ever read a
    caller-supplied path outside the data root").
- `backend/tests/test_documents.py`
  - `test_cli_write_refuses_secret_name_without_materializing` — CLI write
    of a `.env` ⇒ exit 1 `secret_file_forbidden`, sentinel value absent
    from stderr, zero materialized files under the data root.
  - `test_cli_write_refuses_symlink_source_without_following` — CLI write
    of a symlink ⇒ exit 1 `symlink_forbidden`, target content absent from
    stderr, nothing materialized.
  - `test_cli_root_creation_io_error_is_exit_two_not_traceback` — root
    below a regular file ⇒ exit 2 `usage_or_io` JSON on stderr, no
    `Traceback`.
  - Removed the `import os` that was unused since the first round (ruff
    F401; surfaced by this round's lint run).

## Acceptance mapping (unchanged criteria, fix delta)
1. *Canonical Markdown/revision/source/verification metadata scoped to
   server/app; outside docs import explicit* — the fix closes the second
   unguarded outside-read door; both seams now share one guard
   (03-negative-controls red/green).
2. *Secret files/path escapes rejected; update/delete emits durable
   reindex intent* — the CLI write seam now refuses `.env` family /
   key-material names and symlinks exactly like import, with no
   materialization on rejection (02-tests, 03-negative-controls,
   04-runtime-checks).

## Commands (verbatim outputs in numbered files)
- Focused: `cd backend && uv run pytest tests/test_documents.py -v`
  → 53 passed (50 first-round + 3 new) — 02-tests.txt.
- `uv run ruff check src/mai/knowledge/documents.py tests/test_documents.py`
  → All checks passed! (first-round tree had one pre-existing F401, the
  unused `os` import, removed in this round).
- Real entrypoint smoke: 04-runtime-checks.txt (write `.env` ⇒ rc 1,
  write symlink ⇒ rc 1, write normal file ⇒ rc 0 + metadata-only read,
  root-creation IO error ⇒ rc 2).

## Red/green proof of the fix (03-negative-controls)
With ONLY the CLI write read line reverted to the first-round
`Path(args.file).read_text(encoding="utf-8")`, the two new controls fail
exactly where they must: both writes succeed (rc 0) and the sentinel
content lands in the canonical store — the review finding verbatim.
Restoring `_read_outside_source`: all 53 green.

## Limitations
- The red run reverts the one guard line on the fixed module rather than
  checking out `499fcdb`: the new tests and helper are part of the fix and
  do not exist at the first-round commit.
- mypy is not part of this programme's backend toolchain (dev group =
  pytest only); type discipline per ruff, as in the first round.
- The deny-list is name-structural by design (C03 rule 6: no host-wide
  content scanning); a credential file under a non-matching name is out of
  scope for this boundary, same posture as the integrated 03-credentials
  review.
- No external fixtures: all runtime evidence used throwaway files under
  `/tmp/mai-fxt-07-doc-store-fix`, removed after the runs
  (05-cleanup-proof).
