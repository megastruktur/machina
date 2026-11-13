"""Seed the disposable README-capture database with a realistic session.

Runs inside backend/ via `uv run python`. Reads MAI_DATABASE_URL + the ids the
capture script minted through the REAL API (server, session, service). Inserts
a bounded, well-formed transcript (user turns, assistant rounds with tool
calls, executed tool results, a delegation, run-state transitions) so the
session-first chat column renders its turn-group telemetry rail exactly as it
does in production. Every payload satisfies the D01 SessionEntryPayload union
(extra=forbid), so the channel snapshot validates and renders it.

This is SHOWCASE data for documentation screenshots only — it never touches a
real deployment; the capture script drops/recreates the database each run.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import psycopg

DB = os.environ["MAI_DATABASE_URL"]
SERVER_ID = sys.argv[1]
SESSION_ID = sys.argv[2]
SERVICE_ID = sys.argv[3]
SERVICE_NAME = sys.argv[4]

HASH = "9" * 64


def uid() -> str:
    return str(uuid.uuid4())


# Two runs: the parent (interactive) and a delegated child.
RUN_PARENT = uid()
RUN_CHILD = uid()

conn = psycopg.connect(DB, autocommit=True)

# --- session_runs: parent + delegated child (both succeeded) ---------------
for run_id, parent in ((RUN_PARENT, None), (RUN_CHILD, RUN_PARENT)):
    conn.execute(
        """
        INSERT INTO session_runs
            (run_id, session_id, server_id, parent_run_id, state,
             model_profile, client_request_id, completed_at)
        VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'succeeded',
                'default', %s::uuid, now())
        """,
        (run_id, SESSION_ID, SERVER_ID, parent, uid()),
    )

# --- the transcript ---------------------------------------------------------
# Each row: (kind, payload, run_id). seq is allocated in order.
rows: list[tuple[str, dict, str | None]] = []


def user(text: str, service_ref: bool = False) -> None:
    payload: dict = {
        "kind": "user_message",
        "text": text,
        "client_request_id": uid(),
        "source": "interactive",
    }
    if service_ref:
        payload["service_ref"] = {"service_id": SERVICE_ID, "name": SERVICE_NAME}
    rows.append(("user_message", payload, RUN_PARENT))


def assistant(text: str, calls: list[dict] | None = None, group: str = "none") -> None:
    payload = {
        "kind": "assistant_message",
        "text": text,
        "model_profile": "default",
        "tool_group_state": group,
        "tool_calls": calls or [],
    }
    rows.append(("assistant_message", payload, RUN_PARENT))


def tool_result(call_id: str, cap: str, preview: str) -> None:
    rows.append(
        (
            "tool_result",
            {
                "kind": "tool_result",
                "call_id": call_id,
                "tool_name": cap,
                "capability_id": cap,
                "status": "executed",
                "execution_status": "succeeded",
                "domain_outcome": "success",
                "effect_certainty": "certain",
                "payload_hash": HASH,
                "output_preview": preview,
            },
            RUN_PARENT,
        )
    )


def run_state(state: str, detail: str) -> None:
    rows.append(
        ("run_state", {"kind": "run_state", "state": state, "detail": detail}, RUN_PARENT)
    )


# Turn 1 — service-scoped directive (SCOPE-2 provenance on the first entry).
user(
    f"@{SERVICE_NAME} the nightly backup job finished but the verify step looks "
    "off. Check the service health and the last run, then summarize.",
    service_ref=True,
)
run_state("queued", "turn admitted")
run_state("running", "model round started")
c1, c2 = uid(), uid()
assistant(
    "I'll check the service health and pull the last run's events before "
    "drawing any conclusion.",
    calls=[
        {"call_id": c1, "tool_name": "host.shell", "capability_id": "host.shell",
         "args": {"command": "systemctl status backup-verify --no-pager"}},
        {"call_id": c2, "tool_name": "session.read", "capability_id": "session.read",
         "args": {"session_id": SESSION_ID}},
    ],
    group="complete",
)
tool_result(c1, "host.shell",
            "backup-verify.service — active (exited)\n"
            "Main PID: 4121 (code=exited, status=0/SUCCESS)\n"
            "Last run: completed 02:14 UTC, duration 3m 41s")
tool_result(c2, "session.read",
            "session idle · 1 run succeeded · verify step: 2 warnings, 0 errors")

# Turn 2 — delegate the deep check to a child run.
user("Good. Delegate a deeper integrity check to a child run and report back.")
c3 = uid()
assistant(
    "Delegating the integrity check to a bounded child run with read-only "
    "capabilities.",
    calls=[
        {"call_id": c3, "tool_name": "session.delegate",
         "capability_id": "session.delegate",
         "args": {"goal": "verify backup checksums", "allowed_capabilities": ["host.file_read"]}},
    ],
    group="complete",
)
rows.append(
    (
        "delegation",
        {
            "kind": "delegation",
            "child_run_id": RUN_CHILD,
            "goal": "Verify backup checksums and report any mismatch.",
            "status": "succeeded",
            "allowed_capabilities": ["host.file_read", "host.logs"],
            "result_summary": "All 14 backup artifacts verified; 0 checksum mismatches.",
        },
        RUN_PARENT,
    )
)
tool_result(c3, "session.delegate",
            "child run succeeded · 14 artifacts verified · 0 mismatches")

# Turn 3 — the wrap-up.
user("Perfect. Summarize the state and what I should watch next.")
assistant(
    "**Backup is healthy.** The verify step's warnings were informational "
    "(retry-once on a transient mount), not failures — all 14 artifacts "
    "checksum-verified by the delegated child run.\n\n"
    "Watch next:\n"
    "- the mount retry counter (2 this week, was 0)\n"
    "- nightly run duration (trending +8%)\n\n"
    "No action required tonight; I'll flag it if the retry counter climbs."
)
run_state("succeeded", "turn complete")

# --- insert entries with monotonic seq -------------------------------------
for seq, (kind, payload, run_id) in enumerate(rows, start=1):
    conn.execute(
        """
        INSERT INTO session_entries
            (entry_id, session_id, run_id, seq, kind, payload, durable_source_id)
        VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s::jsonb, %s)
        """,
        (uid(), SESSION_ID, run_id, seq, kind, json.dumps(payload), f"readme-{seq}"),
    )

# Mark the session idle (projected from its settled runs) and titled.
conn.execute(
    "UPDATE sessions SET status='idle', title=%s, updated_at=now() WHERE session_id=%s::uuid",
    (f"{SERVICE_NAME} backup verification", SESSION_ID),
)

count = conn.execute(
    "SELECT COUNT(*) FROM session_entries WHERE session_id=%s::uuid", (SESSION_ID,)
).fetchone()[0]
print(f"seeded {count} entries, 2 runs, session titled")
conn.close()
