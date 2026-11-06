"""Shared runtime-scenario harness (D09 extraction).

The persistent test loop, the kill/stop protocol, the journal readers and
the frozen fixture identities previously lived in the deleted
``test_agent_run.py``; the retained session-runner/delegation suites reuse
them verbatim (the BRIEF_D04 harness-reuse mandate). Copy, never import,
across suites: each scenario module owns its own composition.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

__all__ = [
    "HOST_KEY",
    "PRIV_CAP",
    "READ_CAP",
    "READY_TIMEOUT",
    "RUN_TIMEOUT",
    "SECRET_NAME",
    "SECRET_VALUE",
    "SERVER_ID",
    "_journal_count",
    "_journal_lines",
    "_kill9",
    "_run",
    "_stop",
    "_wait_until",
]

READY_TIMEOUT = 90.0
RUN_TIMEOUT = 120.0

# Frozen fixture identities (canonical lowercase UUID strings, C01 rule 1).
SERVER_ID = "6a91c2e4-1b3d-4f5a-8c7e-2d3b4a5f6e70"

READ_CAP = "fixture.read"
PRIV_CAP = "fixture.priv"

SECRET_NAME = "MAI_FXT_TOKEN"
SECRET_VALUE = "fxt-synthetic-token-value-06agent"

HOST_KEY = "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"


# ---------------------------------------------------------------------------
# persistent test loop (same discipline the production executor applies)
# ---------------------------------------------------------------------------

_TEST_LOOP: asyncio.AbstractEventLoop | None = None
_TEST_LOOP_LOCK = threading.Lock()


def _run(coro: object) -> object:
    """Run one coroutine on the persistent test loop; block for the result."""
    global _TEST_LOOP
    with _TEST_LOOP_LOCK:
        if _TEST_LOOP is None:
            _TEST_LOOP = asyncio.new_event_loop()
            threading.Thread(target=_TEST_LOOP.run_forever, daemon=True).start()
    assert _TEST_LOOP is not None
    return asyncio.run_coroutine_threadsafe(coro, _TEST_LOOP).result()


# ---------------------------------------------------------------------------
# journal readers + wait helper
# ---------------------------------------------------------------------------


def _journal_lines(scenario: Path) -> list[str]:
    try:
        return (scenario / "journal.log").read_text().splitlines()
    except FileNotFoundError:
        return []


def _journal_count(scenario: Path, marker: str) -> int:
    return sum(1 for line in _journal_lines(scenario) if line == marker)


def _wait_until(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


# ---------------------------------------------------------------------------
# kill / stop protocol
# ---------------------------------------------------------------------------


def _kill9(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)


def _stop(proc: subprocess.Popen) -> int:
    """SIGTERM, then SIGKILL if the engine's graceful shutdown stalls."""
    proc.send_signal(signal.SIGTERM)
    try:
        return proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=30)
