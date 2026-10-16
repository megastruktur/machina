# 03 — negative controls for the CLI outside-read fix (red/green, 2026-10-16)

Three new controls in tests/test_documents.py (all through the real CLI in
a fresh OS process per call):

1. test_cli_write_refuses_secret_name_without_materializing
   CLI `write` with FILE = a planted `.env` containing
   `CLI_WRITE_SENTINEL=xyz`.
   ⇒ exit 1 `secret_file_forbidden`; the sentinel key and value appear
   nowhere on stderr; `servers/` was never created under the data root —
   nothing indexable materialized.
2. test_cli_write_refuses_symlink_source_without_following
   CLI `write` with FILE = symlink to a real file containing
   `LINK_SENTINEL_CONTENT`.
   ⇒ exit 1 `symlink_forbidden`; target content absent from stderr; no
   canonical file, metadata or intent materialized. The guard refuses
   before following, so a concurrent swap of the link target is moot.
3. test_cli_root_creation_io_error_is_exit_two_not_traceback
   ROOT path requires `mkdir` through an existing regular file
   (`<file>/data`).
   ⇒ exit 2 with value-free `{"error": "usage_or_io", ...}` JSON on
   stderr and no `Traceback` — root construction now happens inside the
   `try` in `main()`.

## RED run (guard reverted): only the CLI write read line replaced

    text = _checked_document(_read_outside_source(Path(args.file)))
    →
    text = Path(args.file).read_text(encoding="utf-8")

$ cd backend && uv run pytest tests/test_documents.py -v -k cli_write_refuses
...
FAILED tests/test_documents.py::test_cli_write_refuses_secret_name_without_materializing - assert 0 == 1
 +  where 0 = CompletedProcess(args=[... 'write', .../data, '--server', ..., .../.env, '--expect-revision', '0'], returncode=0, stdout='{"app_id": null, "doc_id": ..., "revision": 1, ...}')
FAILED tests/test_documents.py::test_cli_write_refuses_symlink_source_without_following - assert 0 == 1
 +  where 0 = CompletedProcess(args=[... 'write', .../data, '--server', ..., .../link.md, '--expect-revision', '0'], returncode=0, stdout='{"app_id": null, ..., "revision": 1, ...}')
======================= 2 failed, 51 deselected in 0.12s =======================

Reading: with the guard removed, both writes SUCCEED (rc 0, revision 1
committed) and the sentinel bytes land in the canonical store — exactly
the first-round behavior the review flagged: the CLI write FILE door was
an unguarded second outside-read seam while the docstring claimed import
was the only one. The other 51 tests (including every import-side refusal)
stay green: the reverted line is the ONLY thing standing between a
caller-supplied `.env`/symlink and the canonical store.

## GREEN run (guard restored, final code):
$ cd backend && uv run pytest tests/test_documents.py -v
...
tests/test_documents.py::test_cli_write_refuses_secret_name_without_materializing PASSED [ 94%]
tests/test_documents.py::test_cli_write_refuses_symlink_source_without_following PASSED [ 96%]
tests/test_documents.py::test_cli_root_creation_io_error_is_exit_two_not_traceback PASSED [ 98%]
============================== 53 passed in 0.58s ==============================
