"""Frozen jobs vocabulary: run lifecycle, engine map, events, schedules.

Frozen schema producer for contracts.md **C05** (runs, steps, schedules and
shared locks) and the job-engine ADR (`bc8d264`, DBOS `dbos==3.2.0`), against:

- **C05 rule 2** — the frozen run lifecycle state machine. ``RunState`` is
  exactly the eight diagram states; ``ENGINE_STATE_MAP`` is the frozen
  engine mapping (``queued``=ENQUEUED, ``running``=PENDING,
  ``succeeded``=SUCCESS, ``failed``=ERROR, ``cancelled``=CANCELLED).
  ``waiting_approval``/``waiting_children``/``reconciling`` are
  application-level states layered on top of the engine; ``cancel_requested``
  is NOT a run state — the facade records it as a timestamp (``C05 rule 2``,
  requested ≠ verified) until the engine reports CANCELLED.
- **C05 rule 3 / rule 2** — a run records stable target/parent/revision
  identity (``server_id``, ``parent_run_id``, ``depth``, profile/prompt
  revision, model profile, requested grants, root budget, error/evidence
  refs). The SQL home for these fields is migration ``0004_jobs``
  (``job_runs``); that table deliberately carries NO engine status column —
  DBOS system tables are the canonical run-state owner (job-engine ADR,
  "run-state ownership"), so there is no duplicate run-state authority.
- **C04 rule 4** — the step outcome vocabulary is the ``CommandStatus``
  vocabulary plus one pre-dispatch state: ``StepState`` = ``pending`` +
  exactly the six frozen outcome values. :func:`step_state_from_command_status`
  is the frozen identity mapping ("CommandOutcome status enum + mapping"
  fixture); ``outcome_unknown`` is never auto-finalized (job-engine guard
  rule 2: missing receipt ⇒ ``outcome_unknown``, reconciliation decides).
- **C05 rule 4** — the frozen event envelope:
  ``{"seq": 42, "run_id": "…", "ts": "…Z", "type": "step.completed",
  "v": 1, "data": {…}}``. Unknown envelope fields are rejected (windowed ADR
  §8 rule 2 applied to durable data); ``v`` >= 1 is mandatory (bad:
  non-versioned payloads); seq >= 1 and the storage-level UNIQUE
  ``(run_id, seq)`` (migration ``0004_jobs``) keep the per-run cursor
  monotonic and replayable. The ``data`` payload guard is shallow
  defense-in-depth: top-level keys shaped like credential material are
  rejected, but the guard is NOT proof — producers must never place secret
  values into ``data`` (C05 rule 4 "bad" list).
- **C05 rule 8 / engine rules 6–7** — schedules carry an explicit timezone
  (``cron_timezone``, default UTC) and exactly one legal misfire policy:
  skip missed fires, resume from now, no catch-up. Dispatch dedup is the
  storage-level UNIQUE ``(schedule_id, planned_fire_time)`` in
  ``job_schedule_fires``; the engine dedups per fire time by deterministic
  workflow ID (spike exp5). Cron *syntax* is validated by the engine when
  the schedule is created; this module validates only the frozen field
  contract (non-blank expression + IANA timezone), not cron semantics.
- **C01 rule 1** — opaque canonical lowercase hyphenated UUID strings are
  the only identities; labels are never identity.

Consumers: ``05-run-lifecycle`` (legal transitions over :class:`RunState`),
``05-engine-adapter`` (:func:`run_state_from_engine`), ``05-steps``
(:class:`StepState`, command-status mapping), ``05-jobs-api``/``05-web-jobs``
/``06-web-chat`` (frozen event envelope over HTTP/SSE), ``05-schedules``/
``05-schedules-api`` (schedule field contract), ``05-job-schema``
(migration ``0004_jobs`` CHECK vocabularies mirror these values).

The module is pure: it reads no process environment, filesystem or network
state, so validation is SSR/worker/replay safe and evidence runs cannot leak
ambient secret material.

Production entrypoint (verified command; exit contract 0 all valid / 1 at
least one envelope rejected / 2 usage or IO error)::

    uv run python -m mai.contracts.jobs EVENTS.ndjson [EVENTS.ndjson ...]

Each input file holds one JSON event envelope per line (NDJSON).
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final
from zoneinfo import ZoneInfo

from mai.execution.commands import CommandStatus

#: Envelope payload version produced by this freeze (C05 rule 4 ``"v"``).
EVENT_PAYLOAD_VERSION: Final[int] = 1

#: Frozen misfire policy value (C05 rule 8; engine rule 6): schedules skip
#: missed fires, resume from now, no catch-up. Single legal value — a second
#: policy is a contract reconciliation, not a column value.
MISFIRE_POLICY_SKIP_MISSED: Final[str] = "skip_missed"

#: Default schedule timezone (engine rule 7: explicit ``cron_timezone``,
#: default UTC; C01 rule 5: stored UTC).
DEFAULT_CRON_TIMEZONE: Final[str] = "UTC"

_MAX_EVENT_TYPE_CHARS = 100
_MAX_CRON_CHARS = 200


class RunState(StrEnum):
    """Frozen run lifecycle states (C05 rule 2 diagram, exactly eight)."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_CHILDREN = "waiting_children"
    RECONCILING = "reconciling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: States that end a run; stale results must never overwrite them
#: (05-run-lifecycle acceptance 1; C05 rule 2 terminal edges).
TERMINAL_RUN_STATES: Final[frozenset[RunState]] = frozenset(
    {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
)

#: Application-level states layered on top of the engine (C05 rule 2); they
#: are facade-owned and never reported by the engine itself.
APPLICATION_RUN_STATES: Final[frozenset[RunState]] = frozenset(
    {
        RunState.WAITING_APPROVAL,
        RunState.WAITING_CHILDREN,
        RunState.RECONCILING,
    }
)


#: Frozen engine→domain map (C05 rule 2 "Engine map"). DBOS 3.2.0 workflow
#: status vocabulary → :class:`RunState`. Any other engine value (including
#: an upgrade that adds states) must fail closed here, never guess.
ENGINE_STATE_MAP: Final[Mapping[str, RunState]] = MappingProxyType(
    {
        "ENQUEUED": RunState.QUEUED,
        "PENDING": RunState.RUNNING,
        "SUCCESS": RunState.SUCCEEDED,
        "ERROR": RunState.FAILED,
        "CANCELLED": RunState.CANCELLED,
    }
)


def run_state_from_engine(engine_status: str) -> RunState:
    """Map a DBOS workflow status onto the frozen run state (C05 rule 2).

    Raises :class:`EngineStateError` for anything outside
    :data:`ENGINE_STATE_MAP` — fail closed, never guess.
    """
    if not isinstance(engine_status, str) or not engine_status:
        raise EngineStateError("engine status must be a non-empty string")
    mapped = ENGINE_STATE_MAP.get(engine_status)
    if mapped is None:
        raise EngineStateError(
            f"unmapped engine status (known: {', '.join(sorted(ENGINE_STATE_MAP))})"
        )
    return mapped


class StepState(StrEnum):
    """Durable step states (C04 rule 4 vocabulary + pre-dispatch ``pending``).

    ``succeeded``/``failed``/``cancelled`` are terminal; ``outcome_unknown``
    is deliberately NOT terminal — reconciliation (C04, job-engine guard
    rules 1–2) decides; ``cancel_requested`` is facade-recorded until the
    engine lands the cancellation at a step boundary (guard rule 3:
    requested ≠ verified).
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


#: Step states that come from a ``CommandOutcome`` — exactly the C04 rule 4
#: vocabulary, passed through unchanged by the frozen identity mapping.
STEP_OUTCOME_STATES: Final[frozenset[StepState]] = frozenset(
    {
        StepState.RUNNING,
        StepState.SUCCEEDED,
        StepState.FAILED,
        StepState.OUTCOME_UNKNOWN,
        StepState.CANCEL_REQUESTED,
        StepState.CANCELLED,
    }
)

#: Terminal step states; ``outcome_unknown`` stays open for reconciliation.
TERMINAL_STEP_STATES: Final[frozenset[StepState]] = frozenset(
    {StepState.SUCCEEDED, StepState.FAILED, StepState.CANCELLED}
)


def step_state_from_command_status(status: CommandStatus) -> StepState:
    """Frozen ``CommandOutcome`` → step-state mapping (C04 rule 4).

    Identity by construction: the step outcome vocabulary IS the command
    outcome vocabulary; the mapping exists so a drift between the two frozen
    sets fails loudly here instead of silently at the storage layer.
    """
    mapped = StepState(status.value)
    if mapped not in STEP_OUTCOME_STATES:
        raise EngineStateError(
            f"command status {status.value} is not a step outcome state"
        )
    return mapped


class JobErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_ENVELOPE = "invalid_envelope"
    UNKNOWN_FIELD = "unknown_field"
    MISSING_FIELD = "missing_field"
    INVALID_SEQ = "invalid_seq"
    INVALID_RUN_ID = "invalid_run_id"
    INVALID_TIMESTAMP = "invalid_timestamp"
    INVALID_EVENT_TYPE = "invalid_event_type"
    INVALID_VERSION = "invalid_version"
    INVALID_DATA = "invalid_data"
    SECRET_SHAPED_FIELD = "secret_shaped_field"
    INVALID_ENGINE_STATE = "invalid_engine_state"
    INVALID_TIMEZONE = "invalid_timezone"
    INVALID_CRON = "invalid_cron"


class EngineStateError(ValueError):
    """One value-free rejection of an unmapped engine status."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"{JobErrorCode.INVALID_ENGINE_STATE.value}: {detail}")
        self.code = JobErrorCode.INVALID_ENGINE_STATE
        self.detail = detail


@dataclass(frozen=True, slots=True)
class JobFieldError:
    """One rejection: field path + stable code + value-free reason."""

    code: JobErrorCode
    field: str
    detail: str


class JobsContractError(ValueError):
    """All rejections for one event envelope or schedule field set.

    ``message``/``str`` contain only field paths, codes and fixed reasons —
    never payload values — so the exception is safe to log, store and return
    to a browser (C01 rule 3).
    """

    def __init__(self, errors: Sequence[JobFieldError]) -> None:
        self.errors: tuple[JobFieldError, ...] = tuple(errors)
        summary = ", ".join(f"{e.field}: {e.code}" for e in self.errors)
        super().__init__(f"rejected ({summary})")

    @property
    def fields(self) -> dict[str, str]:
        """Field path → stable code, ready for the C01 envelope ``fields``."""
        return {e.field: e.code for e in self.errors}


#: Frozen event envelope field set (C05 rule 4). Nothing else is accepted.
EVENT_ENVELOPE_FIELDS: Final[frozenset[str]] = frozenset(
    {"seq", "run_id", "ts", "type", "v", "data"}
)

# Event types are lowercase dotted slugs ("step.completed"); the TYPE
# vocabulary stays open — the envelope SHAPE is what is frozen here.
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]+)*$")

# Frozen credential key names for the shallow top-of-``data`` guard, same
# discipline as the canonical profile schema (contracts/server.py). Exact
# key-name matching (case-insensitive) — NOT substring matching, so benign
# telemetry keys like ``token_count`` stay valid. Defense-in-depth only —
# never proof (C05 rule 4 "bad" list).
_SECRET_DATA_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "auth_token",
        "api_key",
        "apikey",
        "access_key",
        "private_key",
        "passphrase",
        "credential",
        "credentials",
    }
)


@dataclass(frozen=True, slots=True)
class JobEvent:
    """One validated durable event (C05 rule 4 frozen envelope, typed)."""

    seq: int
    run_id: uuid.UUID
    ts: datetime
    type: str
    v: int
    data: Mapping[str, object]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_utc_timestamp(raw: object) -> datetime | None:
    """RFC3339 timestamp → timezone-aware UTC datetime, else ``None``."""
    if not isinstance(raw, str) or not raw:
        return None
    text_value = raw.strip()
    if text_value.endswith(("Z", "z")):
        text_value = text_value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def parse_job_event(payload: Mapping[str, object]) -> JobEvent:
    """Validate one event envelope (C05 rule 4) and return it typed.

    Rejects (all value-free): missing/unknown fields (a present-but-``null``
    field is a VALUE error for that field, not "missing"), non-integer or
    ``< 1`` ``seq``/``v`` (bad: non-versioned payloads), non-canonical
    ``run_id`` UUIDs, naive or unparsable timestamps, empty/oversized or
    malformed ``type`` slugs, non-object ``data``, and frozen credential
    key names at the top of ``data`` (shallow guard, defense-in-depth only).
    """
    errors: list[JobFieldError] = []

    if not isinstance(payload, Mapping):
        raise JobsContractError(
            [
                JobFieldError(
                    JobErrorCode.INVALID_ENVELOPE,
                    "envelope",
                    "not a JSON object",
                )
            ]
        )

    unknown = sorted(
        str(key) for key in payload if str(key) not in EVENT_ENVELOPE_FIELDS
    )
    if unknown:
        errors.append(
            JobFieldError(
                JobErrorCode.UNKNOWN_FIELD, ",".join(unknown), "unknown envelope field"
            )
        )

    seq: int | None = None
    if "seq" not in payload:
        errors.append(
            JobFieldError(JobErrorCode.MISSING_FIELD, "seq", "required field")
        )
    else:
        raw_seq = payload["seq"]
        if not _is_int(raw_seq) or raw_seq < 1:
            errors.append(
                JobFieldError(
                    JobErrorCode.INVALID_SEQ, "seq", "must be an integer >= 1"
                )
            )
        else:
            seq = raw_seq

    run_id: uuid.UUID | None = None
    if "run_id" not in payload:
        errors.append(
            JobFieldError(JobErrorCode.MISSING_FIELD, "run_id", "required field")
        )
    else:
        raw_run_id = payload["run_id"]
        parsed_id: uuid.UUID | None = None
        if isinstance(raw_run_id, str):
            try:
                parsed_id = uuid.UUID(raw_run_id)
            except ValueError:
                parsed_id = None
        if (
            not isinstance(raw_run_id, str)
            or parsed_id is None
            or str(parsed_id) != raw_run_id
        ):
            errors.append(
                JobFieldError(
                    JobErrorCode.INVALID_RUN_ID,
                    "run_id",
                    "must be a canonical lowercase hyphenated UUID string (C01 rule 1)",
                )
            )
        else:
            run_id = parsed_id

    ts: datetime | None = None
    if "ts" not in payload:
        errors.append(JobFieldError(JobErrorCode.MISSING_FIELD, "ts", "required field"))
    else:
        parsed_ts = _parse_utc_timestamp(payload["ts"])
        if parsed_ts is None:
            errors.append(
                JobFieldError(
                    JobErrorCode.INVALID_TIMESTAMP,
                    "ts",
                    "must be an RFC3339 timestamp with explicit UTC offset",
                )
            )
        else:
            ts = parsed_ts

    event_type: str | None = None
    if "type" not in payload:
        errors.append(
            JobFieldError(JobErrorCode.MISSING_FIELD, "type", "required field")
        )
    else:
        raw_type = payload["type"]
        if (
            not isinstance(raw_type, str)
            or not raw_type
            or len(raw_type) > _MAX_EVENT_TYPE_CHARS
            or not _EVENT_TYPE_RE.match(raw_type)
        ):
            errors.append(
                JobFieldError(
                    JobErrorCode.INVALID_EVENT_TYPE,
                    "type",
                    "must be a lowercase dotted slug (e.g. step.completed)",
                )
            )
        else:
            event_type = raw_type

    version: int | None = None
    if "v" not in payload:
        errors.append(JobFieldError(JobErrorCode.MISSING_FIELD, "v", "required field"))
    else:
        raw_version = payload["v"]
        if not _is_int(raw_version) or raw_version < 1:
            # "bad: non-versioned payloads" (C05 rule 4) — v is mandatory.
            errors.append(
                JobFieldError(
                    JobErrorCode.INVALID_VERSION, "v", "must be an integer >= 1"
                )
            )
        else:
            version = raw_version

    data: Mapping[str, object] | None = None
    if "data" not in payload:
        errors.append(
            JobFieldError(JobErrorCode.MISSING_FIELD, "data", "required field")
        )
    else:
        raw_data = payload["data"]
        if not isinstance(raw_data, Mapping):
            errors.append(
                JobFieldError(
                    JobErrorCode.INVALID_DATA, "data", "must be a JSON object"
                )
            )
        else:
            secret_keys = sorted(
                str(key) for key in raw_data if str(key).lower() in _SECRET_DATA_KEYS
            )
            if secret_keys:
                # Value-free rejection: report the key NAMES only.
                errors.append(
                    JobFieldError(
                        JobErrorCode.SECRET_SHAPED_FIELD,
                        ",".join(secret_keys),
                        "credential data key (C05 rule 4: payloads never "
                        "carry secret values)",
                    )
                )
            else:
                data = dict(raw_data)

    if (
        errors
        or seq is None
        or run_id is None
        or ts is None
        or event_type is None
        or version is None
        or data is None
    ):
        raise JobsContractError(errors)

    return JobEvent(
        seq=seq, run_id=run_id, ts=ts, type=event_type, v=version, data=data
    )


def event_to_wire(event: JobEvent) -> dict[str, object]:
    """Render the exact frozen wire envelope (C05 rule 4 shape, ``ts`` as ``Z``)."""
    return {
        "seq": event.seq,
        "run_id": str(event.run_id),
        "ts": event.ts.isoformat().replace("+00:00", "Z"),
        "type": event.type,
        "v": event.v,
        "data": dict(event.data),
    }


# --- schedule field contract (C05 rule 8, engine rules 6–7) -----------------


def validate_cron_expression(expression: object) -> str:
    """Validate the frozen schedule field contract for the cron expression.

    The expression's *syntax* is validated by the engine when the schedule is
    registered (DBOS cron); here only the durable field contract is checked:
    a non-blank string without control characters, at most
    ``_MAX_CRON_CHARS`` long. Recurrence is the only thing the expression may
    encode — time-of-day logic lives inside the scheduled workflow, which
    receives its fire time as an argument (engine rule 7).
    """
    if not isinstance(expression, str) or not expression.strip():
        raise JobsContractError(
            [
                JobFieldError(
                    JobErrorCode.INVALID_CRON, "cron", "must be a non-empty string"
                )
            ]
        )
    if len(expression) > _MAX_CRON_CHARS or any(
        ord(ch) < 0x20 or ord(ch) == 0x7F for ch in expression
    ):
        raise JobsContractError(
            [
                JobFieldError(
                    JobErrorCode.INVALID_CRON,
                    "cron",
                    f"must be 1..{_MAX_CRON_CHARS} printable characters",
                )
            ]
        )
    return expression


def validate_cron_timezone(name: object) -> str:
    """Validate ``cron_timezone`` as an IANA zone (engine rule 7).

    Blank/``None`` is NOT defaulted here — callers that want the frozen
    default apply :data:`DEFAULT_CRON_TIMEZONE` explicitly, so an absent
    timezone can never silently differ between API and storage layers.
    """
    if not isinstance(name, str) or not name:
        raise JobsContractError(
            [
                JobFieldError(
                    JobErrorCode.INVALID_TIMEZONE,
                    "cron_timezone",
                    "must be an explicit IANA timezone name (default: UTC)",
                )
            ]
        )
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001 - ZoneInfo raises several types per platform
        raise JobsContractError(
            [
                JobFieldError(
                    JobErrorCode.INVALID_TIMEZONE,
                    "cron_timezone",
                    "must be an explicit IANA timezone name (default: UTC)",
                )
            ]
        ) from None
    return name


# --- entrypoint --------------------------------------------------------------


_USAGE = (
    "usage: python -m mai.contracts.jobs EVENTS.ndjson [EVENTS.ndjson ...]\n"
    "\n"
    "Validate frozen durable event envelopes (contracts.md C05 rule 4,\n"
    "schema frozen by 05-job-schema). Each input file holds one JSON\n"
    "envelope per line (NDJSON).\n"
    "\n"
    "Exit codes:\n"
    "  0  every envelope is valid\n"
    "  1  at least one line was rejected (per-line structured output;\n"
    "     payload values are never printed)\n"
    "  2  usage or IO error (bad arguments, missing/unreadable file)\n"
)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: validate each NDJSON line, print one structured line each."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--help" in args or "-h" in args:
        print(_USAGE, end="")
        return 0
    if not args:
        print(_USAGE, file=sys.stderr, end="")
        return 2

    any_invalid = False
    for raw_path in args:
        try:
            handle = Path(raw_path).open(encoding="utf-8")  # noqa: SIM115 - closed below
        except OSError:
            print(f"IO-ERROR {raw_path}", file=sys.stderr)
            return 2
        with handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    document = json.loads(line)
                except json.JSONDecodeError:
                    any_invalid = True
                    print(f"INVALID {raw_path}:{line_number} :invalid_envelope")
                    continue
                try:
                    event = parse_job_event(document)
                except JobsContractError as exc:
                    any_invalid = True
                    if exc.fields:
                        fields = ",".join(
                            f"{field}={code}" for field, code in exc.fields.items()
                        )
                    else:
                        fields = ":invalid_envelope"
                    print(f"INVALID {raw_path}:{line_number} {fields}")
                else:
                    print(
                        f"OK {raw_path}:{line_number} seq={event.seq} type={event.type}"
                    )
    return 1 if any_invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
