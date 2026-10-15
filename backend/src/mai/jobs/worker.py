"""Mai worker entrypoint: durable job execution, independent of the HTTP API.

The worker is one process role of the ``mai`` backend package (SERVICES
``mai-worker`` row): it launches the full DBOS engine against Machina's
Postgres — including launch recovery of pending runs — and claims runs from
the Machina queue (``mai.jobs.engine.QUEUE_NAME``). It imports nothing from
the HTTP server, so it can start, stop and restart independently of the API
(card 05-engine-adapter acceptance 2).

Verified command (placeholder Compose command is replaced by the later
platform wiring card, which owns the Compose service)::

    MAI_DATABASE_URL=postgresql://… python -m mai.jobs.worker

Environment:

- ``MAI_DATABASE_URL`` (required) — the Machina Postgres URL, resolved by
  :func:`mai.database.resolve_database_url` (the single URL contract).
- ``MAI_WORKER_HANDLERS`` (optional) — comma-separated import paths of
  modules that call :func:`mai.jobs.engine.register_job_handler`. Imported
  before launch; a module that fails to import stops the worker with exit 2
  (fail closed: an unregistered handler would fail its runs at execution
  time anyway). Until the owner cards land real handler modules this may be
  unset — the worker still launches, claims and recovers runs.

Signals: ``SIGTERM``/``SIGINT`` stop the engine cleanly (exit 0);
``SIGKILL`` leaves recovery to the next launch (job-engine ADR, exp1).
"""

from __future__ import annotations

import argparse
import importlib
import os
import signal
import sys
import threading
from collections.abc import Sequence

from mai.database import resolve_database_url
from mai.jobs import engine

_STOP = threading.Event()


def _request_stop(signum: int, _frame: object) -> None:
    """Record the stop signal; the main thread performs the shutdown."""
    del signum
    _STOP.set()


def _import_handlers(spec: str) -> int:
    """Import handler-registration modules; return the process exit code."""
    for name in (part.strip() for part in spec.split(",")):
        if not name:
            continue
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 - worker must fail closed
            print(
                f"mai worker: handler module '{name}' failed to import: "
                f"{type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return 2
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one worker process until ``SIGTERM``/``SIGINT``."""
    parser = argparse.ArgumentParser(
        prog="python -m mai.jobs.worker",
        description="Mai durable-job worker (DBOS engine, C05).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=engine.DEFAULT_QUEUE_CONCURRENCY,
        help=f"per-worker queue concurrency (default {engine.DEFAULT_QUEUE_CONCURRENCY})",
    )
    ns = parser.parse_args(argv)

    try:
        database_url = resolve_database_url()
    except Exception as exc:  # noqa: BLE001 - value-free config failure
        print(
            f"mai worker: MAI_DATABASE_URL is not usable: {type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    handler_spec = os.environ.get("MAI_WORKER_HANDLERS", "")
    rc = _import_handlers(handler_spec)
    if rc != 0:
        return rc

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    try:
        engine.launch_worker(database_url, queue_concurrency=ns.concurrency)
    except Exception as exc:  # noqa: BLE001 - value-free startup failure
        print(
            f"mai worker: engine launch failed: {type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    print("mai worker: ready", file=sys.stderr, flush=True)
    _STOP.wait()
    engine.shutdown()
    print("mai worker: stopped", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
