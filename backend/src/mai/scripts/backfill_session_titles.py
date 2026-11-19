"""Backfill LLM-generated titles for existing untitled sessions (§4.4).

Campaign ``machina-session-title`` ST-W4. The worker titles every NEW
first turn (the ``agents.session.title`` step in the session runner);
this script covers the sessions that predate that path: bounded pages of
``title IS NULL`` sessions oldest-first, each one — first user message →
ONE bounded generation → the guarded store seam.

Governing rules:

- **Bounded pages, oldest first** — 50 rows per page (AGENTS.md §1),
  keyset-ordered ``(created_at, session_id) ASC``. The cursor moves past
  every EXAMINED row, so titling a row (which removes it from the
  ``title IS NULL`` set) can never skip later rows or loop, and a failed
  generation is simply left behind by the cursor.
- **The generator is an injected seam** — :func:`main` takes
  ``generate``: ONE async callable ``first_message -> title | None``.
  The production seam (:func:`_default_generator`) resolves the model
  exactly as the worker wires the session runner:
  ``ModelConfigStore(MAI_DATA_ROOT).bind()`` (the stored selection) →
  ``build_model(binding.selection(), resolver)`` with the credential
  resolver layered from ``MAI_ENV_FILE`` over ``<data_root>/credentials.env``
  (the ``_resolve_env`` pattern of ``mai/jobs/worker.py``), then wraps
  :func:`mai.agents.session_title.generate_session_title`. Tests inject a
  scripted callable — no live provider, no endpoint, no key, no new
  configuration anywhere in this module.
- **The model's output is untrusted DATA** — it passes through the
  redaction + normalization of :func:`generate_session_title` and the
  never-overwrite / no-``updated_at`` store seam
  :func:`aset_generated_session_title` before anything is stored; the
  prompt source is the user's raw first message with any appended Service
  Map section cut (the runner's ``_title_source_text`` behavior).
- **Value-free output** — the summary and every log line carry counters
  only: no session ids, no message texts, no generated titles, no
  secrets (``--verbose`` prints per-page progress counters).
- **Idempotent** — a titled session leaves the predicate set, so a re-run
  scans only what is still ``NULL``. ``--dry-run`` generates and reports
  would-be outcomes but NEVER writes anything.
- **Kill-switch (D7)** — ``MAI_SESSION_TITLE=0`` disables the script
  entirely (the same env var as the runtime path).

Usage (worker container, which already has ``MAI_DATABASE_URL`` +
``MAI_DATA_ROOT`` + ``MAI_ENV_FILE``)::

    python -m mai.scripts.backfill_session_titles --dry-run
    python -m mai.scripts.backfill_session_titles [--limit N] [--verbose]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

from sqlalchemy import text as sql_text

from mai.agents.models import build_model
from mai.agents.model_store import ModelConfigStore
from mai.agents.session_title import generate_session_title
from mai.contracts.sessions import SERVICE_MAP_SECTION_HEADER
from mai.credentials.env import EnvResolver, build_runtime_env_resolver
from mai.database import create_app_engine

__all__ = ["main"]

#: Bounded page size (AGENTS.md §1 pagination invariant).
PAGE_SIZE: Final[int] = 50

#: The kill-switch env var (D7), shared with the runtime title path.
KILL_SWITCH_ENV: Final[str] = "MAI_SESSION_TITLE"

#: The generator seam: ONE async callable from the first user message to
#: a normalized title (or ``None`` — a failed generation skips the
#: session and the run continues).
TitleGenerator = Callable[[str], Awaitable[str | None]]


def _default_generator() -> TitleGenerator:
    """The production seam: the worker's own model resolution.

    ``ModelConfigStore(MAI_DATA_ROOT).bind()`` pins the STORED selection
    (no hardcoded connection/profile/model anywhere), ``build_model``
    resolves the PydanticAI model with call-time credentials through the
    ``MAI_ENV_FILE``-layered resolver (the worker's ``_resolve_env``
    pattern), and the round runs through the ONE bounded title wrapper
    (:func:`mai.agents.session_title.generate_session_title` — hard
    deadline, redaction, normalization, degrade-to-``None``).
    """
    data_root = Path(os.environ["MAI_DATA_ROOT"])
    env_file = os.environ.get("MAI_ENV_FILE") or None
    primary = env_file if env_file and Path(env_file).is_file() else None
    resolver: EnvResolver | None = build_runtime_env_resolver(primary, data_root)
    if resolver is None:
        fallback = data_root / ".env"
        if not fallback.is_file():
            fallback.write_text("", encoding="utf-8")
            fallback.chmod(0o600)
        resolver = EnvResolver(fallback)
    store = ModelConfigStore(data_root)
    binding = store.bind()
    resolved = build_model(binding.selection(), resolver=resolver)
    model = resolved.model if hasattr(resolved, "model") else resolved

    async def _generate(first_message: str) -> str | None:
        return await generate_session_title(model, first_message)

    return _generate


async def _next_page(
    sessions: object,
    *,
    cursor_ts: object,
    cursor_id: object,
    page_size: int,
) -> list[Mapping[str, object]]:
    """The next bounded page of ``title IS NULL`` sessions with at least
    one user entry, keyset-ordered oldest-first."""
    keyset = ""
    params: dict[str, object] = {"page_size": page_size}
    if cursor_ts is not None:
        keyset = "AND (s.created_at, s.session_id) > (:cursor_ts, :cursor_id) "
        params["cursor_ts"] = cursor_ts
        params["cursor_id"] = cursor_id
    query = (
        """
        SELECT s.session_id AS session_id, s.server_id AS server_id,
               s.created_at AS created_at
        FROM sessions s
        WHERE s.title IS NULL
          AND EXISTS (
                SELECT 1 FROM session_entries e
                WHERE e.session_id = s.session_id AND e.kind = 'user_message'
          )
        """
        + keyset
        + """
        ORDER BY s.created_at ASC, s.session_id ASC
        LIMIT :page_size
        """
    )
    async with sessions() as session:  # type: ignore[union-attr]
        return (await session.execute(sql_text(query), params)).mappings().all()


async def _backfill_one(
    sessions: object,
    generate: TitleGenerator,
    row: Mapping[str, object],
    *,
    dry_run: bool,
) -> str:
    """One session → a value-free outcome: ``titled`` | ``skipped`` |
    ``failed``. ONE failed session never aborts the run."""
    from mai.session_store import aset_generated_session_title

    try:
        async with sessions() as session:  # type: ignore[union-attr]
            first = (
                await session.execute(
                    sql_text(
                        """
                        SELECT payload->>'text' AS text
                        FROM session_entries
                        WHERE session_id = :sid AND kind = 'user_message'
                        ORDER BY seq ASC
                        LIMIT 1
                        """
                    ),
                    {"sid": row["session_id"]},
                )
            ).scalar()
        if not first:
            return "skipped"
        # The raw first message only: an appended Service Map section
        # never enters the title prompt (the runner's source rule).
        cut = str(first).find(SERVICE_MAP_SECTION_HEADER)
        source = str(first)[:cut].rstrip() if cut >= 0 else str(first)
        title = await generate(source)
        if not title:
            # Provider error / deadline / garbage / echoed secret: the
            # session keeps ``title IS NULL`` and the run continues.
            return "failed"
        if dry_run:
            return "titled"  # would-be outcome; NOTHING is written
        written = await aset_generated_session_title(
            sessions,  # type: ignore[arg-type]
            session_id=row["session_id"],  # type: ignore[arg-type]
            server_id=row["server_id"],  # type: ignore[arg-type]
            title=title,
        )
        # ``None`` means a title landed concurrently (a manual rename won).
        return "titled" if written is not None else "skipped"
    except Exception:  # noqa: BLE001 - one session's failure is value-free
        return "failed"


async def _run_backfill(
    url: str,
    generate: TitleGenerator,
    *,
    dry_run: bool,
    limit: int | None,
    verbose: bool,
) -> dict[str, object]:
    """The bounded page walk; returns the value-free counters summary."""
    eng, sessions = create_app_engine(url)
    scanned = titled = skipped = failed = 0
    page = 0
    cursor_ts: object = None
    cursor_id: object = None
    try:
        while True:
            remaining = None if limit is None else limit - scanned
            if remaining is not None and remaining <= 0:
                break
            page_size = PAGE_SIZE if remaining is None else min(PAGE_SIZE, remaining)
            rows = await _next_page(
                sessions,
                cursor_ts=cursor_ts,
                cursor_id=cursor_id,
                page_size=page_size,
            )
            if not rows:
                break
            page += 1
            for row in rows:
                scanned += 1
                outcome = await _backfill_one(
                    sessions, generate, row, dry_run=dry_run
                )
                if outcome == "titled":
                    titled += 1
                elif outcome == "skipped":
                    skipped += 1
                else:
                    failed += 1
            last = rows[-1]
            cursor_ts = last["created_at"]
            cursor_id = last["session_id"]
            if verbose:
                print(
                    f"page {page}: scanned={scanned} titled={titled} "
                    f"skipped={skipped} failed={failed}",
                    flush=True,
                )
            if len(rows) < page_size:
                break
    finally:
        await eng.dispose()
    return {
        "scanned": scanned,
        "titled": titled,
        "skipped": skipped,
        "failed": failed,
        "dry_run": dry_run,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    generate: TitleGenerator | None = None,
) -> int:
    """CLI entry: backfill generated titles over the untitled sessions.

    ``generate`` is the documented seam: when supplied (tests, harnesses)
    it replaces :func:`_default_generator` wholesale — no provider
    configuration is touched. Returns 0 on a completed run (individual
    failures are tallied in the summary, never fatal); setup errors
    (missing env, unusable model resolution) print a value-free line to
    stderr and return 2.
    """
    parser = argparse.ArgumentParser(
        prog="python -m mai.scripts.backfill_session_titles",
        description=(
            "Backfill LLM-generated titles for existing sessions with "
            "title IS NULL and at least one user message."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="generate and report would-be outcomes; write NOTHING",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="process at most N sessions this run",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print per-page value-free progress counters",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be >= 0")

    # D7: the kill-switch disables the script entirely — before any
    # database or model work.
    if os.environ.get(KILL_SWITCH_ENV) == "0":
        print(
            json.dumps(
                {
                    "scanned": 0,
                    "titled": 0,
                    "skipped": 0,
                    "failed": 0,
                    "dry_run": args.dry_run,
                    "disabled": True,
                },
                sort_keys=True,
            )
        )
        return 0

    url = os.environ.get("MAI_DATABASE_URL", "").strip()
    if not url:
        print("backfill: MAI_DATABASE_URL is not set", file=sys.stderr)
        return 2
    if generate is None:
        try:
            generate = _default_generator()
        except Exception as error:  # noqa: BLE001 - value-free setup failure
            print(
                f"backfill: model resolution failed: {type(error).__name__}",
                file=sys.stderr,
            )
            return 2
    summary = asyncio.run(
        _run_backfill(
            url,
            generate,
            dry_run=args.dry_run,
            limit=args.limit,
            verbose=args.verbose,
        )
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
