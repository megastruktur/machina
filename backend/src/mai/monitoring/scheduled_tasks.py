"""Bounded scheduler inventory over SSH (card 10-cron, C04 + C10).

Frozen-shape producer for the deterministic ``cron`` collector: one fixed,
read-only, self-bounding probe command inventories the TARGET's own
schedulers — systemd timers (identity + next/last elapse where the manager
answers), system crontabs (``/etc/crontab``, ``/etc/cron.d/*``), user
crontabs (``/var/spool/cron/crontabs/*``, ``/var/spool/cron/*``) and
anacron (``/etc/anacrontab`` + ``/var/spool/anacron/*``) — from a
pinned-identity SSH target (the ``04-ssh-connect`` transport) and persists
bounded rows of the ``0007_observations`` schema. The product's own DBOS
schedules (``job_schedules``, §C05 rule 8) are a DIFFERENT domain: this
collector never reads or writes them (acceptance: target cron/timers are
observed SEPARATELY from MAI schedules).

- **Acceptance 1 (read-only inventory with identity + next/last where
  supported)** — systemd timers report unit identity, next elapse and last
  trigger as epoch microseconds (``systemctl list-timers --all
  --no-pager --output=json``; field names and µs typing verified against
  the systemd ``table_to_json`` serializer: columns ``next left last
  passed unit activates``, ``TABLE_TIMESTAMP`` → unsigned µs, ``null``
  when unset). cron has NO next/last authority (vixie cron records
  nothing), so cron/anacron entries carry identity + schedule + a SHA-256
  command fingerprint only — the honest "where supported" boundary.
- **Acceptance 2 (unsupported/missing distinguished; no edits)** — every
  scheduler source is THREE-valued: measured (possibly empty — a real
  zero), absent (marker says the scheduler/layout does not exist), or
  unknown (present but the query failed, e.g. systemd binaries installed
  while the manager is not PID 1 → ``tm_rc≠0`` ⇒ timers UNKNOWN, never a
  zero timer count; unreadable spool/file ⇒ explicit ``read_gaps`` count,
  quality capped at ``degraded``). The probe command contains ONLY read
  primitives (``printf/command -v/test -d -f -r/cat-redirects-from-glob/
  awk/sha256sum/cut/tr/head``): no ``crontab`` invocation (the binary can
  edit; presence is checked with ``command -v`` only), no systemctl
  mutation verb, no redirect into a target path — negative controls audit
  the token surface, and the real-target fixture hashes every planted
  schedule file before/after collection to prove NOTHING changed.

**Secret redaction (strict, structural):** cron command lines and
crontab environment values routinely carry credentials (API keys in
headers/tokens, basic-auth URLs). The probe NEVER emits that text: awk
splits each line and pipes ONLY the command/value suffix into
``sha256sum`` on the target — the controller sees ``<16 hex chars>`` or
``-`` (hash tool unavailable), and the raw suffix is discarded with the
pipeline. There is no code path that could carry command text into the
sample, the storage dump or an evidence file. The fingerprint exists for
identity and change detection across polls; it is not confidentiality
against offline brute-force of low-entropy values (documented limitation).
Schedule fields, usernames, variable NAMES and file paths are inventory
identity and are kept verbatim (validated, bounded).

Bounds mirror the sibling collectors: listing caps enforced target-side
BEFORE prefixes (honest totals survive the trim), controller hard-caps
captured output, timer JSON truncated at 16 KiB target-side with an
explicit ``tm_truncated`` flag that forces the timers source UNKNOWN
(a truncated answer is never a lying partial count). Sample quality
covers all three states; an unavailable target records ONE
``quality='unknown'`` row with NO metric/value/observed_at (the schema's
``ck_server_observations_unknown_has_no_measurement`` structurally forbids
a zero reading) and consumers derive staleness via the shared
:func:`mai.contracts.observations.derive_freshness`. Missing data never
closes a finding (``no_data`` drafts do nothing).

The probe runs synchronously (same shape as the P04 ``probe_environment``
and the sibling ``system``/``network``/``process`` collectors: system
``ssh`` client via :func:`subprocess.run`), persistence is async on the
single application engine (:func:`mai.database.create_app_engine`). A
collection attempt never raises for target unavailability — it returns
the unknown path — but controller misconfiguration (unresolvable key
reference, missing enrollment) still raises, failing closed.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.observations import (
    SUMMARY_MAX_CHARS,
    ObservationMethod,
    ObservationQuality,
)
from mai.contracts.server import SshIdentity
from mai.execution.ssh import (
    SshConnectError,
    SshErrorCode,
    ssh_arguments,
)

__all__ = [
    "FRESH_HORIZON",
    "MAX_ENV_LISTED",
    "MAX_ANACRON_LISTED",
    "MAX_JOBS_LISTED",
    "MAX_PROBE_OUTPUT_BYTES",
    "MAX_TIMERS_LISTED",
    "PROBE_TIMEOUT_SECONDS",
    "RETENTION_HORIZON",
    "SOURCE",
    "AnacronEntry",
    "AnacronSpoolEntry",
    "CronEnvEntry",
    "CronJobEntry",
    "CollectionResult",
    "FindingDraft",
    "ScheduledTaskSample",
    "TimerEntry",
    "collect",
    "findings_for_sample",
    "parse_probe_output",
    "probe_scheduled_tasks",
    "record_sample",
    "record_unavailable",
]

#: Collector identity persisted on every row of this collector (the 0007
#: schema's own docstring names 'cron' as the canonical example source).
SOURCE = "cron"

#: Wall-clock budget for one probe round trip (connect timeout is the frozen
#: P04 :data:`CONNECT_TIMEOUT_SECONDS`).
PROBE_TIMEOUT_SECONDS = 30.0

#: Hard cap on captured probe output. The probe emits bounded lines by
#: construction (counts + ≤16 timer/job/anacron rows + ≤16 env rows +
#: ≤8 gap paths); anything larger is a hostile/broken target.
MAX_PROBE_OUTPUT_BYTES = 32 * 1024

#: Collector-declared freshness horizon (C10 rule 4: consumers derive
#: staleness against their own clock via ``derive_freshness``).
FRESH_HORIZON = timedelta(minutes=15)

#: Retention bound stamped on every row this collector writes.
RETENTION_HORIZON = timedelta(days=7)

#: Bounded per-sample listings (target-side caps; the controller re-applies
#: them defensively). Full counts are honest because the target counts
#: BEFORE trimming.
MAX_TIMERS_LISTED = 16
MAX_JOBS_LISTED = 32
MAX_ENV_LISTED = 16
MAX_ANACRON_LISTED = 16
_MAX_GAP_FILES_LISTED = 8

#: Target-side cap on the flattened ``list-timers --output=json`` blob.
#: ~140 bytes per timer row ⇒ >100 timers before this bites; a target that
#: exceeds it reports ``tm_truncated=yes`` and the timers source becomes
#: UNKNOWN (never an understated count).
_MAX_TIMER_JSON_CHARS = 16384

#: Length of the SHA-256 fingerprint kept for command/value identity.
_CMD_HASH_CHARS = 16

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

_UNIT_RE = re.compile(r"^[A-Za-z0-9@._-]{1,64}$")
_LOC_RE = re.compile(r"^[A-Za-z0-9/._-]{1,128}:[0-9]{1,6}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9/._-]{1,128}$")
_SCHED_RE = re.compile(r"^[A-Za-z0-9*,\-/@ ]{1,64}$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_HASH_RE = re.compile(r"^[0-9a-f]{16}$")
_ANACRON_NUM_RE = re.compile(r"^[0-9]{1,8}$")
_ANACRON_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ANSPOOL_TS_RE = re.compile(r"^[0-9]{1,32}$")

# ---------------------------------------------------------------------------
# The single remote command (fixed, read-only, self-bounding, POSIX-sh)
# ---------------------------------------------------------------------------

#: Structure: target clock; systemd timer JSON (rc-guarded, newline-stripped,
#: size-capped with an explicit truncation flag); hash-tool capability;
#: scheduler presence markers (crontab binary, anacron binary, system
#: crontab, /etc/cron.d, spool dirs — each three-valued); then per-file
#: parsing (awk splits, the command/value suffix is piped straight into
#: ``sha256sum`` and never printed) with honest pre-trim counters.
#: ``read`` with a tab IFS folds the raw command suffix (including internal
#: tabs) into the last variable; every awk record is padded to six tab
#: fields so one ``read`` shape serves job/env/anacron/malformed records.
PROBE_COMMAND = (
    "printf 'ts=%s\\n' \"$(date -u '+%Y-%m-%dT%H:%M:%S' 2>/dev/null)\"; "
    "if command -v systemctl >/dev/null 2>&1; then "
    "printf 'systemd=yes\\n'; "
    "tmraw=$(systemctl list-timers --all --no-pager --output=json 2>/dev/null); "
    "tmrc=$?; printf 'tm_rc=%s\\n' \"$tmrc\"; "
    'if [ "$tmrc" = "0" ]; then '
    "tmflat=$(printf '%s' \"$tmraw\" | tr -d '\\n\\r'); "
    'if [ "${#tmflat}" -gt ' + str(_MAX_TIMER_JSON_CHARS) + " ]; then "
    "tmflat=$(printf '%s' \"$tmflat\" | head -c " + str(_MAX_TIMER_JSON_CHARS) + "); "
    "printf 'tm_truncated=yes\\n'; "
    "else printf 'tm_truncated=no\\n'; fi; "
    "printf 'tmj=%s\\n' \"$tmflat\"; fi; "
    "else printf 'systemd=no\\n'; fi; "
    "hashok=no; "
    "if command -v sha256sum >/dev/null 2>&1; then hashok=yes; fi; "
    "printf 'hash=%s\\n' \"$hashok\"; "
    "if command -v crontab >/dev/null 2>&1; then printf 'cron_cmd=yes\\n'; "
    "else printf 'cron_cmd=no\\n'; fi; "
    "if command -v anacron >/dev/null 2>&1; then printf 'anacron_cmd=yes\\n'; "
    "else printf 'anacron_cmd=no\\n'; fi; "
    "if [ -f /etc/crontab ]; then printf 'syscrontab=yes\\n'; "
    "else printf 'syscrontab=no\\n'; fi; "
    "if [ -f /etc/anacrontab ]; then printf 'anacron_tab=yes\\n'; "
    "else printf 'anacron_tab=no\\n'; fi; "
    "if [ -d /etc/cron.d ]; then "
    "if [ -r /etc/cron.d ] && [ -x /etc/cron.d ]; then printf 'cron_d=yes\\n'; "
    "else printf 'cron_d=unreadable\\n'; fi; "
    "else printf 'cron_d=missing\\n'; fi; "
    "for d in /var/spool/cron/crontabs /var/spool/cron; do "
    'if [ -d "$d" ]; then '
    'if [ -r "$d" ] && [ -x "$d" ]; then printf \'spool=%s\\n\' "$d"; '
    "else printf 'spool_unreadable=%s\\n' \"$d\"; fi; fi; done; "
    "unreadable=0; files=0; "
    "hashof() { "
    'if [ "$hashok" = yes ]; then printf \'%s\' "$1" | sha256sum 2>/dev/null '
    "| cut -c1-16; else printf -- '-'; fi; }; "
    "parsecron() { "
    'if [ ! -r "$1" ]; then '
    "unreadable=$((unreadable+1)); "
    '[ "$unreadable" -le ' + str(_MAX_GAP_FILES_LISTED) + " ] && "
    "printf 'gap_file=%s\\n' \"$1\"; return 0; fi; "
    "files=$((files+1)); "
    'awk -v kind="$2" \''
    "function fld(i) { p=1; c=0; "
    'while (c < i) { while (p <= length(line) && substr(line,p,1) == " ") p++; '
    'while (p <= length(line) && substr(line,p,1) != " " && '
    'substr(line,p,1) != "\\t") p++; c++ }; '
    'while (p <= length(line) && (substr(line,p,1) == " " || '
    'substr(line,p,1) == "\\t")) p++; return p } '
    '{ line=$0; sub(/^[ \\t\\r]+/, "", line); '
    'if (line == "" || line ~ /^#/) next; sub(/[ \\t\\r]+$/, "", line); '
    "n = split(line, a, /[ \\t]+/); "
    "if (a[1] ~ /^[A-Za-z_][A-Za-z0-9_]*=/ || (n >= 2 && "
    "a[1] ~ /^[A-Za-z_][A-Za-z0-9_]*$/ && a[2] ~ /^=/)) { "
    'name = a[1]; sub(/=.*/, "", name); val = line; '
    'sub(/^[^=]*=[ \\t]*/, "", val); '
    'printf "E\\t%s:%d\\t%s\\t%s\\t-\\t-\\n", FILENAME, FNR, name, val; next } '
    'if (kind == "anacron") { '
    "if (n >= 4) { cmd = substr(line, fld(3)); "
    'printf "A\\t%s:%d\\t%s\\t%s\\t%s\\t%s\\n", FILENAME, FNR, a[1], a[2], a[3], cmd } '
    'else printf "M\\t%s:%d\\t-\\t-\\t-\\t-\\n", FILENAME, FNR; next } '
    "if (a[1] ~ /^@/) { "
    'if (kind == "sys") { if (n >= 3) { cmd = substr(line, fld(2)); '
    'printf "J\\t%s:%d\\t%s\\t%s\\t%s\\t-\\n", FILENAME, FNR, a[1], a[2], cmd } '
    'else printf "M\\t%s:%d\\t-\\t-\\t-\\t-\\n", FILENAME, FNR } '
    "else { if (n >= 2) { cmd = substr(line, fld(1)); "
    'printf "J\\t%s:%d\\t%s\\t-\\t%s\\t-\\n", FILENAME, FNR, a[1], cmd } '
    'else printf "M\\t%s:%d\\t-\\t-\\t-\\t-\\n", FILENAME, FNR } next } '
    'if (kind == "sys") { if (n >= 7) { sched = a[1] " " a[2] " " a[3] " " '
    'a[4] " " a[5]; cmd = substr(line, fld(6)); '
    'printf "J\\t%s:%d\\t%s\\t%s\\t%s\\t-\\n", FILENAME, FNR, sched, a[6], cmd } '
    'else printf "M\\t%s:%d\\t-\\t-\\t-\\t-\\n", FILENAME, FNR } '
    'else { if (n >= 6) { sched = a[1] " " a[2] " " a[3] " " a[4] " " a[5]; '
    "cmd = substr(line, fld(5)); "
    'printf "J\\t%s:%d\\t%s\\t-\\t%s\\t-\\n", FILENAME, FNR, sched, cmd } '
    'else printf "M\\t%s:%d\\t-\\t-\\t-\\t-\\n", FILENAME, FNR } }'
    '\' "$1" | { '
    "TAB=$(printf '\\t'); jb=$3; eb=$4; ab=$5; "
    "j_t=0; j_l=0; e_t=0; e_l=0; a_t=0; a_l=0; m_t=0; "
    'while IFS="$TAB" read -r tag f2 f3 f4 f5 f6; do '
    'case "$tag" in '
    "J) j_t=$((j_t+1)); "
    'if [ "$j_l" -lt "$jb" ]; then '
    'printf \'job=%s user=%s cmd=%s %s\\n\' "$f2" "$f4" "$(hashof "$f5")" "$f3"; '
    "j_l=$((j_l+1)); fi;; "
    "E) e_t=$((e_t+1)); "
    'if [ "$e_l" -lt "$eb" ]; then '
    'printf \'env=%s %s=%s\\n\' "$f2" "$f3" "$(hashof "$f4")"; '
    "e_l=$((e_l+1)); fi;; "
    "A) a_t=$((a_t+1)); "
    'if [ "$a_l" -lt "$ab" ]; then '
    'printf \'an=%s %s %s %s cmd=%s\\n\' "$f2" "$f3" "$f4" "$f5" "$(hashof "$f6")"; '
    "a_l=$((a_l+1)); fi;; "
    "M) m_t=$((m_t+1));; "
    "esac; done; "
    "printf 'j_total=%s\\ne_total=%s\\na_total=%s\\nmalformed=%s\\n' "
    '"$j_t" "$e_t" "$a_t" "$m_t"; }; '
    "}; "
    "JB="
    + str(MAX_JOBS_LISTED)
    + "; EB="
    + str(MAX_ENV_LISTED)
    + "; AB="
    + str(MAX_ANACRON_LISTED)
    + "; "
    # Base-zero delta counters: emitted even when parsecron never runs (no
    # file exists) so the controller sums an honest explicit zero instead of
    # an unknown.
    "printf 'j_total=0\\ne_total=0\\na_total=0\\nmalformed=0\\n'; "
    'if [ -f /etc/crontab ]; then parsecron /etc/crontab sys "$JB" "$EB" "$AB"; fi; '
    "if [ -d /etc/cron.d ] && [ -r /etc/cron.d ] && [ -x /etc/cron.d ]; then "
    "for f in /etc/cron.d/*; do "
    'if [ -f "$f" ]; then parsecron "$f" sys "$JB" "$EB" "$AB"; fi; done; fi; '
    "for d in /var/spool/cron/crontabs /var/spool/cron; do "
    'if [ -d "$d" ] && [ -r "$d" ] && [ -x "$d" ]; then '
    'for f in "$d"/*; do '
    'if [ -f "$f" ]; then parsecron "$f" user "$JB" "$EB" "$AB"; fi; done; fi; done; '
    'if [ -f /etc/anacrontab ]; then parsecron /etc/anacrontab anacron "$JB" "$EB" "$AB"; fi; '
    "if [ -d /var/spool/anacron ] && [ -r /var/spool/anacron ] && "
    "[ -x /var/spool/anacron ]; then "
    "for f in /var/spool/anacron/*; do "
    'if [ -f "$f" ]; then '
    'if [ -r "$f" ]; then '
    "printf 'anspool=%s %s\\n' \"${f##*/}\" "
    '"$(head -n 1 "$f" 2>/dev/null | tr -d \' \\t\\r\')"; '
    "else unreadable=$((unreadable+1)); "
    '[ "$unreadable" -le ' + str(_MAX_GAP_FILES_LISTED) + " ] && "
    "printf 'gap_file=%s\\n' \"$f\"; fi; fi; done; fi; "
    'printf \'unreadable=%s\\nfiles=%s\\n\' "$unreadable" "$files"'
)

#: Listing bounds enforced by the probe itself; mirrored controller-side.
_MAX_TIMERS_BOUND_TARGET = MAX_TIMERS_LISTED
_MAX_JOBS_BOUND_TARGET = MAX_JOBS_LISTED
_MAX_ENV_BOUND_TARGET = MAX_ENV_LISTED
_MAX_ANACRON_BOUND_TARGET = MAX_ANACRON_LISTED

#: Scalar keys whose values are per-file DELTAS the controller sums
#: (honest full counts survive the per-file listing budgets).
_SUMMED_KEYS = frozenset({"j_total", "e_total", "a_total", "malformed"})


# ---------------------------------------------------------------------------
# Sample shape (pure; what one probe round trip observed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimerEntry:
    """One systemd timer as ``list-timers --output=json`` reported it.

    ``next_us``/``last_us`` are epoch microseconds (systemd serializes
    ``TABLE_TIMESTAMP`` cells as unsigned µs; JSON ``null`` — a timer with
    no next elapse/last trigger, e.g. an inactive ``OnUnitInactiveActive``
    — stays ``None``: never a fabricated zero). ``activates`` flattens the
    single-string or string-array ``activates`` cell.
    """

    unit: str
    next_us: int | None
    last_us: int | None
    activates: tuple[str, ...] = ()

    @property
    def next_elapse(self) -> datetime | None:
        if self.next_us is None:
            return None
        return datetime.fromtimestamp(self.next_us / 1_000_000, tz=UTC)

    @property
    def last_trigger(self) -> datetime | None:
        if self.last_us is None:
            return None
        return datetime.fromtimestamp(self.last_us / 1_000_000, tz=UTC)


@dataclass(frozen=True, slots=True)
class CronJobEntry:
    """One cron line: location, schedule, user, command fingerprint.

    ``cmd_sha256`` is the 16-hex-char SHA-256 prefix of the RAW command
    suffix (computed ON THE TARGET; the command text never leaves it) or
    ``None`` when the target lacks ``sha256sum`` (a ``hash`` gap that caps
    sample quality at ``degraded`` — a fingerprint-less inventory is
    honest, an emitted command line is forbidden).
    """

    location: str
    schedule: str
    user: str | None
    cmd_sha256: str | None


@dataclass(frozen=True, slots=True)
class CronEnvEntry:
    """One crontab environment assignment: location, name, value fingerprint.

    Same redaction rule as :class:`CronJobEntry`: only the NAME is
    verbatim, the VALUE is hashed target-side (values are where
    ``https://user:pass@host``-style secrets live).
    """

    location: str
    name: str
    value_sha256: str | None


@dataclass(frozen=True, slots=True)
class AnacronEntry:
    """One ``/etc/anacrontab`` line: period/delay (days/minutes), job id,
    command fingerprint."""

    location: str
    period_days: str
    delay_minutes: str
    job_id: str
    cmd_sha256: str | None


@dataclass(frozen=True, slots=True)
class AnacronSpoolEntry:
    """One ``/var/spool/anacron/<id>`` timestamp file: the id and the raw
    first token (an anacron date number like ``20261017``; a plain date,
    not secret material)."""

    job_id: str
    raw_stamp: str | None


@dataclass(frozen=True, slots=True)
class ScheduledTaskSample:
    """One bounded scheduler inventory snapshot.

    Three-valued sources (acceptance 2): ``None`` = unknown (present but
    the query failed / answer unusable), ``()`` = measured and empty,
    non-empty = measured. ``systemd_present`` is ``None`` when the probe
    output carried no marker at all (hostile/broken target), ``False`` is
    the explicit ``systemd=no`` answer.

    ``timers_query`` names HOW the timer source resolved: ``ok``,
    ``absent`` (no systemctl), ``rc=<n>`` (query failed),
    ``truncated`` (JSON cap exceeded) or ``unparseable`` (rc=0 but the
    JSON could not be parsed) — missing vs unsupported vs failing is
    distinguishable from the sample alone.

    ``cron_present`` derives from the presence markers: ``True`` = at
    least one cron/anacron layout exists, ``False`` = every marker
    explicitly negative (the "missing scheduler" answer), ``None`` =
    markers unusable.
    """

    observed_at: datetime
    observed_at_source: Literal["target", "controller"]
    quality: ObservationQuality
    systemd_present: bool | None = None
    timers: tuple[TimerEntry, ...] | None = None
    timers_total: int | None = None
    timers_query: str | None = None
    timer_json_truncated: bool | None = None
    cron_cmd_present: bool | None = None
    anacron_cmd_present: bool | None = None
    system_crontab: bool | None = None
    anacrontab: bool | None = None
    cron_d: str | None = None
    spool_dirs: tuple[str, ...] = ()
    spool_unreadable: tuple[str, ...] = ()
    cron_present: bool | None = None
    jobs: tuple[CronJobEntry, ...] | None = None
    jobs_total: int | None = None
    envs: tuple[CronEnvEntry, ...] | None = None
    envs_total: int | None = None
    anacron_jobs: tuple[AnacronEntry, ...] | None = None
    anacron_total: int | None = None
    anacron_spool: tuple[AnacronSpoolEntry, ...] = ()
    unreadable: int | None = None
    gap_files: tuple[str, ...] = ()
    malformed: int | None = None
    hash_supported: bool | None = None


# ---------------------------------------------------------------------------
# Pure parsing: bounded target output → honest sample
# ---------------------------------------------------------------------------


def _int_field(raw: str | None) -> int | None:
    if raw is None or not raw.isdigit():
        return None
    return int(raw)


def _bool_field(raw: str | None) -> bool | None:
    if raw == "yes":
        return True
    if raw == "no":
        return False
    return None


def _hash_field(raw: str) -> str | None:
    value = raw.strip()
    if _HASH_RE.fullmatch(value) is None:
        return None
    return value


def _loc_field(raw: str) -> str | None:
    value = raw.strip()
    if _LOC_RE.fullmatch(value) is None:
        return None
    return value


def _sched_field(raw: str) -> str | None:
    value = raw.strip()
    if _SCHED_RE.fullmatch(value) is None:
        return None
    return value


def _user_field(raw: str) -> str | None:
    value = raw.strip()
    if value == "-":
        return None
    if _USER_RE.fullmatch(value) is None:
        return None
    return value


def _path_field(raw: str) -> str | None:
    value = raw.strip()
    if _PATH_RE.fullmatch(value) is None:
        return None
    return value


def _fingerprint(raw: str) -> str | None:
    """``-`` is the legitimate no-fingerprint marker (hash tool absent);
    any other non-fingerprint value is a MALFORMED marker that must drop
    the row (a hostile target cannot smuggle junk identity through)."""
    if raw == "-":
        return None
    return _hash_field(raw)


def _parse_job_line(value: str) -> CronJobEntry | None:
    """Parse one ``job=<loc> user=<u> cmd=<h> <schedule>`` listing line.

    The schedule (the ONLY multi-token field) is deliberately LAST so the
    first three fields split unambiguously; the tail is kept verbatim
    (normalized single-space field joins from the target-side awk) and
    validated as a whole. ``-`` marks the honest no-user/no-fingerprint
    cases. Malformed input drops the entry — a hostile target cannot
    inject inventory rows.
    """
    fields = value.split(maxsplit=3)
    if len(fields) != 4:
        return None
    loc = _loc_field(fields[0])
    user_token = fields[1]
    cmd_token = fields[2]
    sched = _sched_field(fields[3])
    if (
        loc is None
        or sched is None
        or not user_token.startswith("user=")
        or not cmd_token.startswith("cmd=")
    ):
        return None
    user = _user_field(user_token[len("user=") :])
    cmd_hash = _fingerprint(cmd_token[len("cmd=") :])
    if cmd_hash is None and cmd_token[len("cmd=") :] != "-":
        return None
    return CronJobEntry(location=loc, schedule=sched, user=user, cmd_sha256=cmd_hash)


def _parse_env_line(value: str) -> CronEnvEntry | None:
    """Parse one ``env=<loc> <name>=<h>`` listing line (``-`` = no hash)."""
    fields = value.split(maxsplit=1)
    if len(fields) != 2:
        return None
    loc = _loc_field(fields[0])
    name_token = fields[1]
    if loc is None or "=" not in name_token:
        return None
    name, _, hash_raw = name_token.partition("=")
    if _ENV_NAME_RE.fullmatch(name) is None:
        return None
    value_hash = _fingerprint(hash_raw)
    if value_hash is None and hash_raw != "-":
        return None
    return CronEnvEntry(location=loc, name=name, value_sha256=value_hash)


def _parse_anacron_line(value: str) -> AnacronEntry | None:
    """Parse one ``an=<loc> <period> <delay> <id> cmd=<h>`` listing line."""
    fields = value.split(maxsplit=4)
    if len(fields) != 5:
        return None
    loc = _loc_field(fields[0])
    period, delay, job_id = fields[1], fields[2], fields[3]
    cmd_token = fields[4]
    if (
        loc is None
        or _ANACRON_NUM_RE.fullmatch(period) is None
        or _ANACRON_NUM_RE.fullmatch(delay) is None
        or _ANACRON_ID_RE.fullmatch(job_id) is None
        or not cmd_token.startswith("cmd=")
    ):
        return None
    cmd_hash = _fingerprint(cmd_token[len("cmd=") :])
    if cmd_hash is None and cmd_token[len("cmd=") :] != "-":
        return None
    return AnacronEntry(
        location=loc,
        period_days=period,
        delay_minutes=delay,
        job_id=job_id,
        cmd_sha256=cmd_hash,
    )


def _parse_timer_json(raw: str) -> tuple[TimerEntry, ...] | None:
    """Parse the flattened ``list-timers`` JSON blob (pure).

    Returns ``None`` when the blob is not a JSON array of objects (a
    hostile/broken answer is UNKNOWN, never zero). Every field is
    type-checked: ``unit`` must be a bounded unit-name string,
    ``next``/``last`` unsigned µs integers or ``null``, ``activates`` a
    string or array of strings (systemd's ``table_add_triggered`` emits
    one OR the other). Unparsable entries drop; hostile types never
    fabricate inventory.
    """
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(doc, list):
        return None
    entries: list[TimerEntry] = []
    for row in doc[: _MAX_TIMERS_BOUND_TARGET * 4]:
        if not isinstance(row, dict):
            continue
        unit = row.get("unit")
        if not isinstance(unit, str) or _UNIT_RE.fullmatch(unit) is None:
            continue
        times: list[int | None] = []
        for key in ("next", "last"):
            value = row.get(key)
            if value is None:
                times.append(None)
            elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                times.append(value)
            else:
                times = []
                break
        if len(times) != 2:
            continue
        raw_activates = row.get("activates")
        activates: list[str] = []
        if isinstance(raw_activates, str):
            candidates: tuple[str, ...] = (raw_activates,)
        elif isinstance(raw_activates, list):
            candidates = tuple(raw_activates)
        else:
            candidates = ()
        for act in candidates:
            if isinstance(act, str) and _UNIT_RE.fullmatch(act) is not None:
                activates.append(act)
        entries.append(
            TimerEntry(
                unit=unit,
                next_us=times[0],
                last_us=times[1],
                activates=tuple(activates),
            )
        )
        if len(entries) >= _MAX_TIMERS_BOUND_TARGET:
            break
    return tuple(entries)


def parse_probe_output(output: str, *, controller_now: datetime) -> ScheduledTaskSample:
    """Parse fixed probe output into a :class:`ScheduledTaskSample` (pure).

    Order- and first-occurrence-sensitive like the sibling parsers:
    recognized scalar keys are captured on their first appearance, unknown
    keys ignored, malformed values leave the field ``None`` (never a
    fabricated zero). Listing rows keep target order and are capped at the
    frozen bounds. Quality: the two section markers (``systemd=``,
    ``hash=``/cron markers) must be present, else the sample is
    ``unknown``; explicit gaps — unreadable schedule files, missing hash
    tool, malformed cron lines, a failed/truncated/unparseable timer query
    on a systemd target — cap quality at ``degraded``; clean answers
    (including the honest "absent scheduler" ones) measure.
    """
    if controller_now.tzinfo is None:
        raise ValueError("controller_now must be timezone-aware")
    values: dict[str, str] = {}
    job_lines: list[str] = []
    env_lines: list[str] = []
    anacron_lines: list[str] = []
    anspool_lines: list[str] = []
    spool_lines: list[str] = []
    spool_unreadable_lines: list[str] = []
    gap_lines: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "job":
            job_lines.append(value)
        elif key == "env":
            env_lines.append(value)
        elif key == "an":
            anacron_lines.append(value)
        elif key == "anspool":
            anspool_lines.append(value)
        elif key == "spool":
            spool_lines.append(value)
        elif key == "spool_unreadable":
            spool_unreadable_lines.append(value)
        elif key == "gap_file":
            gap_lines.append(value)
        elif key in _SUMMED_KEYS:
            # Per-file delta counters: the target emits one line per parsed
            # file; the sample totals are the SUM (a truncated/broken line
            # is ignored — the honest count can only grow by real answers).
            if value.isdigit():
                values[key] = str(int(values.get(key, "0")) + int(value))
        elif key not in values:
            values[key] = value

    observed_at = controller_now
    observed_at_source: Literal["target", "controller"] = "controller"
    raw_ts = values.get("ts")
    if raw_ts:
        try:
            parsed = datetime.strptime(raw_ts, _TS_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            parsed = None
        if parsed is not None:
            observed_at = parsed
            observed_at_source = "target"

    systemd_present = _bool_field(values.get("systemd"))
    hash_supported = _bool_field(values.get("hash"))
    cron_cmd_present = _bool_field(values.get("cron_cmd"))
    anacron_cmd_present = _bool_field(values.get("anacron_cmd"))
    system_crontab = _bool_field(values.get("syscrontab"))
    anacrontab = _bool_field(values.get("anacron_tab"))
    cron_d_raw = values.get("cron_d")
    cron_d = cron_d_raw if cron_d_raw in ("yes", "unreadable", "missing") else None

    spool_dirs: list[str] = []
    for raw in spool_lines[: _MAX_GAP_FILES_LISTED * 2]:
        path = _path_field(raw)
        if path is not None and path not in spool_dirs:
            spool_dirs.append(path)
    spool_unreadable: list[str] = []
    for raw in spool_unreadable_lines[:_MAX_GAP_FILES_LISTED]:
        path = _path_field(raw)
        if path is not None and path not in spool_unreadable:
            spool_unreadable.append(path)

    # The cron-section markers must all be answerable for cron_present;
    # every marker explicitly negative is the honest "absent" answer.
    cron_d_present = {"yes": True, "unreadable": True, "missing": False}.get(cron_d)
    cron_markers = (cron_cmd_present, system_crontab, anacrontab, cron_d_present)
    if (
        all(marker is False for marker in cron_markers)
        and not spool_dirs
        and not spool_unreadable
    ):
        cron_present: bool | None = False
    elif (
        any(marker is not None for marker in cron_markers)
        or spool_dirs
        or spool_unreadable
    ):
        cron_present = True
    else:
        cron_present = None

    timers: tuple[TimerEntry, ...] | None = None
    timers_total: int | None = None
    timers_query: str | None = None
    timer_json_truncated = _bool_field(values.get("tm_truncated"))
    if systemd_present is True:
        tm_rc = _int_field(values.get("tm_rc"))
        truncated = timer_json_truncated is True
        if truncated:
            timers_query = "truncated"
        elif tm_rc is None:
            timers_query = "unparseable"
        elif tm_rc != 0:
            timers_query = f"rc={tm_rc}"
        elif "tmj" not in values:
            timers_query = "unparseable"
        else:
            parsed_timers = _parse_timer_json(values["tmj"])
            full_count: int | None = None
            try:
                full = json.loads(values["tmj"])
                if isinstance(full, list):
                    full_count = len(full)
            except ValueError:
                full_count = None
            if parsed_timers is None or (
                full_count is not None and full_count > 0 and not parsed_timers
            ):
                # A broken answer or one whose EVERY row failed validation is
                # an unusable timer source — unknown, never an empty listing.
                timers_query = "unparseable"
            else:
                # Honest total: the untruncated blob is the full answer.
                timers_total = full_count
                timers = parsed_timers
                timers_query = "ok"
    elif systemd_present is False:
        timers_query = "absent"

    # Fingerprint gap: the hash tool answered "no" — every listed entry
    # carries the "-" no-fingerprint marker and the sample caps at degraded.
    hash_gap = hash_supported is False

    # The cron section ANSWERED when any of its markers is present; once it
    # answered, zero listed rows are a real empty inventory (not unknown).
    cron_section_answered = bool(
        cron_cmd_present is not None
        or anacron_cmd_present is not None
        or system_crontab is not None
        or anacrontab is not None
        or cron_d is not None
        or spool_dirs
        or spool_unreadable
    )

    jobs: list[CronJobEntry] = []
    for raw in job_lines[:_MAX_JOBS_BOUND_TARGET]:
        entry = _parse_job_line(raw)
        if entry is not None:
            jobs.append(entry)
    envs: list[CronEnvEntry] = []
    for raw in env_lines[:_MAX_ENV_BOUND_TARGET]:
        entry = _parse_env_line(raw)
        if entry is not None:
            envs.append(entry)
    anacron_jobs: list[AnacronEntry] = []
    for raw in anacron_lines[:_MAX_ANACRON_BOUND_TARGET]:
        entry = _parse_anacron_line(raw)
        if entry is not None:
            anacron_jobs.append(entry)

    anacron_spool: list[AnacronSpoolEntry] = []
    for raw in anspool_lines[: _MAX_GAP_FILES_LISTED * 2]:
        job_id, _, stamp = raw.strip().partition(" ")
        if _ANACRON_ID_RE.fullmatch(job_id) is None:
            continue
        stamp = stamp.strip()
        raw_stamp: str | None = stamp if _ANSPOOL_TS_RE.fullmatch(stamp) else None
        anacron_spool.append(AnacronSpoolEntry(job_id=job_id, raw_stamp=raw_stamp))

    gap_files: list[str] = []
    for raw in gap_lines[:_MAX_GAP_FILES_LISTED]:
        path = _path_field(raw)
        if path is not None and path not in gap_files:
            gap_files.append(path)

    unreadable = _int_field(values.get("unreadable"))
    malformed = _int_field(values.get("malformed"))
    jobs_total = _int_field(values.get("j_total"))
    envs_total = _int_field(values.get("e_total"))
    anacron_total = _int_field(values.get("a_total"))

    # Quality: core sources = the systemd marker + the cron presence
    # markers + the per-file section. Any marker entirely missing ⇒ the
    # probe output is unusable ⇒ unknown. Explicit gaps cap at degraded.
    answered = systemd_present is not None and hash_supported is not None
    cron_answered = cron_cmd_present is not None or cron_present is not None
    gaps = bool(
        (unreadable is not None and unreadable > 0)
        or (malformed is not None and malformed > 0)
        or hash_gap
        or gap_files
        or spool_unreadable
        or cron_d == "unreadable"
        or (systemd_present is True and timers is None)
    )
    if answered and cron_answered:
        quality = ObservationQuality.DEGRADED if gaps else ObservationQuality.MEASURED
    else:
        quality = ObservationQuality.UNKNOWN

    return ScheduledTaskSample(
        observed_at=observed_at,
        observed_at_source=observed_at_source,
        quality=quality,
        systemd_present=systemd_present,
        timers=timers,
        timers_total=timers_total,
        timers_query=timers_query,
        timer_json_truncated=timer_json_truncated,
        cron_cmd_present=cron_cmd_present,
        anacron_cmd_present=anacron_cmd_present,
        system_crontab=system_crontab,
        anacrontab=anacrontab,
        cron_d=cron_d,
        spool_dirs=tuple(spool_dirs),
        spool_unreadable=tuple(spool_unreadable),
        cron_present=cron_present,
        jobs=tuple(jobs) if (jobs or job_lines or cron_section_answered) else None,
        jobs_total=jobs_total,
        envs=tuple(envs) if (envs or env_lines or cron_section_answered) else None,
        envs_total=envs_total,
        anacron_jobs=(
            tuple(anacron_jobs)
            if (anacron_jobs or anacron_lines or cron_section_answered)
            else None
        ),
        anacron_total=anacron_total,
        anacron_spool=tuple(anacron_spool),
        unreadable=unreadable,
        gap_files=tuple(gap_files),
        malformed=malformed,
        hash_supported=hash_supported,
    )


# ---------------------------------------------------------------------------
# Probe execution (P04 transport: frozen option surface, pinned host key)
# ---------------------------------------------------------------------------


def _sanitize_stderr(stderr: str) -> str:
    """Bounded single-line stderr tail for error details (value-free)."""
    flat = " ".join(stderr.split())
    return flat[-200:] if flat else "<no stderr>"


def _map_probe_failure(rc: int, stderr: str, endpoint: str) -> SshConnectError:
    """Map a non-zero ssh exit to the stable P04 rejection vocabulary.

    Local mirror of the measured-probe classification (the transport owns
    the canonical table): only the coarse class matters here — the
    collector maps any of these to the unknown path, the codes exist so
    evidence and logs name the same rejection reason the transport suite
    pins.
    """
    flat = " ".join(stderr.split())
    if (
        "Host key verification failed" in flat
        or "HOST IDENTIFICATION HAS CHANGED" in flat
    ):
        return SshConnectError(
            SshErrorCode.HOST_KEY_MISMATCH,
            f"{endpoint} refused by host key pinning (stderr: {_sanitize_stderr(stderr)})",
        )
    if "Permission denied" in flat or "bad permissions" in flat.lower():
        return SshConnectError(
            SshErrorCode.AUTH_FAILED,
            f"{endpoint} rejected the pinned identity key (stderr: {_sanitize_stderr(stderr)})",
        )
    if any(
        marker in flat
        for marker in (
            "Could not resolve hostname",
            "Connection refused",
            "Connection timed out",
            "Connection reset",
            "Network is unreachable",
        )
    ):
        return SshConnectError(
            SshErrorCode.UNREACHABLE,
            f"{endpoint} unreachable (stderr: {_sanitize_stderr(stderr)})",
        )
    return SshConnectError(
        SshErrorCode.SSH_FAILED,
        f"{endpoint} ssh exited rc={rc} (stderr: {_sanitize_stderr(stderr)})",
    )


def probe_scheduled_tasks(
    identity: SshIdentity,
    *,
    key_path: Path,
    known_hosts_path: Path,
    controller_now: datetime,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ScheduledTaskSample:
    """Run the fixed scheduler probe over the pinned-identity transport.

    Preconditions mirror :func:`mai.execution.ssh.probe_environment`: the
    key path comes from ``resolve_identity_key`` and the managed
    known-hosts file from ``pin_host_key`` (enrollment). Raises
    :class:`SshConnectError` (stable codes) for any transport failure; a
    target that answers is parsed into a :class:`ScheduledTaskSample`
    under the hard output budget.
    """
    if not known_hosts_path.is_file():
        raise SshConnectError(
            SshErrorCode.INVALID_CONFIGURATION,
            f"managed known-hosts file {known_hosts_path} does not exist; "
            "enroll the pinned host key explicitly before collecting",
        )
    command = ssh_arguments(
        identity,
        key_path=key_path,
        known_hosts_path=known_hosts_path,
        remote_command=PROBE_COMMAND,
    )
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise SshConnectError(
            SshErrorCode.TIMEOUT, f"scheduler probe timed out after {timeout}s"
        ) from None
    if len(proc.stdout.encode("utf-8")) > MAX_PROBE_OUTPUT_BYTES:
        raise SshConnectError(
            SshErrorCode.BOUNDED_OUTPUT_EXCEEDED,
            f"scheduler probe output exceeded the {MAX_PROBE_OUTPUT_BYTES}-byte budget",
        )
    if proc.returncode != 0:
        raise _map_probe_failure(
            proc.returncode, proc.stderr, f"{identity.host}:{identity.port}"
        )
    return parse_probe_output(proc.stdout, controller_now=controller_now)


# ---------------------------------------------------------------------------
# Findings (collector-owned inventory conditions; dedup by
# (server_id, kind, dedup_key))
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FindingDraft:
    """One collector-owned finding evaluation for a sample.

    ``state`` is three-valued: ``active`` upserts the row, ``clear`` closes
    an open row, ``no_data`` leaves the row untouched (missing data is never
    evidence that a condition ended).
    """

    kind: str
    dedup_key: str
    severity: Literal["info", "warning", "critical"]
    summary: str
    state: Literal["active", "clear", "no_data"]


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def findings_for_sample(sample: ScheduledTaskSample) -> tuple[FindingDraft, ...]:
    """Evaluate the collector's inventory policy against one sample (pure).

    Three conditions, all inventory-honest (no invented health thresholds):

    - ``cron.read_gaps`` — schedule files/dirs exist but were unreadable to
      the collector identity (permission gap; the inventory is partial).
    - ``cron.timer_query`` — systemd is present but the timer inventory
      could not be read (rc≠0 / truncated / unparseable). A target WITHOUT
      systemd is absent, not failing: no_data.
    - ``cron.no_scheduler`` — every scheduler marker is explicitly negative
      (no systemd, no cron/anacron layout): an info-level statement that
      the target has nothing to schedule with, distinct from unknown.
    """
    drafts: list[FindingDraft] = []

    if sample.unreadable is None:
        drafts.append(
            FindingDraft(
                kind="cron.read_gaps",
                dedup_key="unreadable",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    else:
        active = sample.unreadable > 0
        drafts.append(
            FindingDraft(
                kind="cron.read_gaps",
                dedup_key="unreadable",
                severity="warning" if active else "info",
                summary=_truncate(
                    f"{sample.unreadable} scheduler files unreadable to the "
                    "collector identity (permission gap; inventory is partial)",
                    SUMMARY_MAX_CHARS,
                ),
                state="active" if active else "clear",
            )
        )

    if sample.systemd_present is True and sample.timers is None:
        drafts.append(
            FindingDraft(
                kind="cron.timer_query",
                dedup_key="query",
                severity="warning",
                summary=_truncate(
                    "systemd present but the timer inventory is unreadable "
                    f"(query: {sample.timers_query})",
                    SUMMARY_MAX_CHARS,
                ),
                state="active",
            )
        )
    elif sample.systemd_present is not None:
        drafts.append(
            FindingDraft(
                kind="cron.timer_query",
                dedup_key="query",
                severity="info",
                summary="",
                state="clear",
            )
        )
    else:
        drafts.append(
            FindingDraft(
                kind="cron.timer_query",
                dedup_key="query",
                severity="warning",
                summary="",
                state="no_data",
            )
        )

    if sample.cron_present is False and sample.systemd_present is False:
        drafts.append(
            FindingDraft(
                kind="cron.no_scheduler",
                dedup_key="absent",
                severity="info",
                summary=_truncate(
                    "no scheduler found: systemd absent and no cron/anacron "
                    "layout (explicit absence, not a query failure)",
                    SUMMARY_MAX_CHARS,
                ),
                state="active",
            )
        )
    elif sample.cron_present is not None and sample.systemd_present is not None:
        drafts.append(
            FindingDraft(
                kind="cron.no_scheduler",
                dedup_key="absent",
                severity="info",
                summary="",
                state="clear",
            )
        )
    else:
        drafts.append(
            FindingDraft(
                kind="cron.no_scheduler",
                dedup_key="absent",
                severity="info",
                summary="",
                state="no_data",
            )
        )

    return tuple(drafts)


# ---------------------------------------------------------------------------
# Persistence into the 0007 observation schema
# ---------------------------------------------------------------------------

_INSERT_OBSERVATION = sa.text(
    """
    INSERT INTO server_observations
        (observation_id, server_id, source, method, quality, metric, value,
         observed_at, collected_at, fresh_until, retain_until)
    VALUES
        (:observation_id, :server_id, :source, :method, :quality, :metric,
         :value, :observed_at, :collected_at, :fresh_until, :retain_until)
    """
)

_UPSERT_FINDING = sa.text(
    """
    INSERT INTO observation_findings
        (finding_id, server_id, kind, dedup_key, severity, summary, detail,
         observation_id, opened_at, updated_at, resolved_at, retain_until)
    VALUES
        (:finding_id, :server_id, :kind, :dedup_key, :severity, :summary,
         :detail, :observation_id, :collected_at, :collected_at, NULL,
         :retain_until)
    ON CONFLICT (server_id, kind, dedup_key) DO UPDATE SET
        severity = EXCLUDED.severity,
        summary = EXCLUDED.summary,
        detail = EXCLUDED.detail,
        observation_id = EXCLUDED.observation_id,
        updated_at = EXCLUDED.updated_at,
        resolved_at = NULL
    """
)

_CLOSE_FINDING = sa.text(
    """
    UPDATE observation_findings
       SET resolved_at = :resolved_at, updated_at = :resolved_at
     WHERE server_id = :server_id AND kind = :kind AND resolved_at IS NULL
    """
)


def _metric_rows(
    server_id: uuid.UUID,
    sample: ScheduledTaskSample,
    collected_at: datetime,
    fresh_until: datetime,
    retain_until: datetime,
) -> list[dict[str, object]]:
    """Bounded per-metric rows; the rendering stays a short normalized text.

    Identity (unit names, locations, schedules, usernames, fingerprints)
    is inventory data and bounded per the 0007 column CHECKs; command
    lines and environment VALUES exist here ONLY as fingerprints (they
    never left the target as text).
    """

    def row(metric: str, value: str) -> dict[str, object]:
        return {
            "observation_id": uuid.uuid4(),
            "server_id": server_id,
            "source": SOURCE,
            "method": ObservationMethod.COMMAND.value,
            "quality": sample.quality.value,
            "metric": metric,
            "value": value,
            "observed_at": sample.observed_at,
            "collected_at": collected_at,
            "fresh_until": fresh_until,
            "retain_until": retain_until,
        }

    rows: list[dict[str, object]] = []
    if sample.systemd_present is not None:
        rows.append(row("cron.systemd", "yes" if sample.systemd_present else "no"))
    if sample.timers_query is not None:
        rows.append(row("cron.timers_query", sample.timers_query))
    if sample.timers_total is not None:
        rows.append(row("cron.timers_total", str(sample.timers_total)))
    if sample.timers is not None:
        for timer in sample.timers:
            value = "next={} last={} activates={}".format(
                timer.next_us if timer.next_us is not None else "-",
                timer.last_us if timer.last_us is not None else "-",
                ",".join(timer.activates) if timer.activates else "-",
            )
            rows.append(row("cron.timer", f"{timer.unit} {value}"))
    if sample.cron_cmd_present is not None:
        rows.append(row("cron.cmd_present", "yes" if sample.cron_cmd_present else "no"))
    if sample.anacron_cmd_present is not None:
        rows.append(
            row("cron.anacron_cmd", "yes" if sample.anacron_cmd_present else "no")
        )
    if sample.system_crontab is not None:
        rows.append(
            row("cron.system_crontab", "yes" if sample.system_crontab else "no")
        )
    if sample.anacrontab is not None:
        rows.append(row("cron.anacrontab", "yes" if sample.anacrontab else "no"))
    if sample.cron_d is not None:
        rows.append(row("cron.cron_d", sample.cron_d))
    for path in sample.spool_dirs:
        rows.append(row("cron.spool", path))
    for path in sample.spool_unreadable:
        rows.append(row("cron.spool_unreadable", path))
    if sample.cron_present is not None:
        rows.append(row("cron.present", "yes" if sample.cron_present else "no"))
    if sample.hash_supported is not None:
        rows.append(row("cron.hash", "ok" if sample.hash_supported else "unavailable"))
    if sample.unreadable is not None:
        rows.append(row("cron.unreadable", str(sample.unreadable)))
    if sample.malformed is not None:
        rows.append(row("cron.malformed", str(sample.malformed)))
    if sample.jobs_total is not None:
        rows.append(row("cron.jobs_total", str(sample.jobs_total)))
    if sample.jobs is not None:
        rows.append(row("cron.jobs_listed", str(len(sample.jobs))))
    for job in sample.jobs or ():
        rows.append(
            row(
                "cron.job",
                "{} user={} cmd={} {}".format(
                    job.location,
                    job.user if job.user is not None else "-",
                    job.cmd_sha256 if job.cmd_sha256 is not None else "-",
                    job.schedule,
                ),
            )
        )
    if sample.envs_total is not None:
        rows.append(row("cron.env_total", str(sample.envs_total)))
    for env in sample.envs or ():
        rows.append(
            row(
                "cron.env",
                "{} {}={}".format(
                    env.location,
                    env.name,
                    env.value_sha256 if env.value_sha256 is not None else "-",
                ),
            )
        )
    if sample.anacron_total is not None:
        rows.append(row("cron.anacron_total", str(sample.anacron_total)))
    for entry in sample.anacron_jobs or ():
        rows.append(
            row(
                "cron.anacron",
                "{} {} {} {} cmd={}".format(
                    entry.location,
                    entry.period_days,
                    entry.delay_minutes,
                    entry.job_id,
                    entry.cmd_sha256 if entry.cmd_sha256 is not None else "-",
                ),
            )
        )
    for spool in sample.anacron_spool:
        rows.append(
            row(
                "cron.anacron_spool",
                "{} {}".format(
                    spool.job_id,
                    spool.raw_stamp if spool.raw_stamp is not None else "-",
                ),
            )
        )
    for path in sample.gap_files:
        rows.append(row("cron.gap_file", path))
    return rows


async def record_sample(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    sample: ScheduledTaskSample,
    *,
    controller_now: datetime,
) -> int:
    """Persist one sample as bounded observation rows + inventory findings.

    One row per observed metric (source/method/quality/timestamps per C10
    rule 4), then the collector's finding set: active drafts upsert onto
    ``(server_id, kind, dedup_key)``, cleared drafts close open rows,
    ``no_data`` drafts do nothing. The ONLY tables touched are
    ``server_observations`` and ``observation_findings`` — MAI schedules
    (``job_schedules``/``job_schedule_fires``) are never read or written
    (target schedulers are observed separately from MAI schedules).
    Returns the number of observation rows written (findings excluded).
    """
    if controller_now.tzinfo is None:
        raise ValueError("controller_now must be timezone-aware")
    fresh_until = sample.observed_at + FRESH_HORIZON
    retain_until = controller_now + RETENTION_HORIZON
    rows = _metric_rows(server_id, sample, controller_now, fresh_until, retain_until)
    # Active findings anchor to a REAL inserted sample row (FK to
    # server_observations); with no metric rows no finding can be active.
    observation_id = rows[0]["observation_id"] if rows else uuid.uuid4()
    async with sessions() as session:
        for params in rows:
            await session.execute(_INSERT_OBSERVATION, params)
        for draft in findings_for_sample(sample):
            if draft.state == "active":
                await session.execute(
                    _UPSERT_FINDING,
                    {
                        "finding_id": uuid.uuid4(),
                        "server_id": server_id,
                        "kind": draft.kind,
                        "dedup_key": draft.dedup_key,
                        "severity": draft.severity,
                        "summary": draft.summary,
                        "detail": None,  # bounded text column stays unused here
                        "observation_id": observation_id,
                        "collected_at": controller_now,
                        "retain_until": retain_until,
                    },
                )
            elif draft.state == "clear":
                await session.execute(
                    _CLOSE_FINDING,
                    {
                        "resolved_at": controller_now,
                        "server_id": server_id,
                        "kind": draft.kind,
                    },
                )
        await session.commit()
    return len(rows)


async def record_unavailable(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    *,
    controller_now: datetime,
) -> int:
    """Persist the unknown path for a permission-refused/unreachable target.

    Exactly ONE ``quality='unknown'`` row with NO metric/value/observed_at
    (the schema's ``ck_server_observations_unknown_has_no_measurement``
    forbids anything else) — never a zero scheduler count. Existing
    findings are deliberately left untouched: missing data neither
    re-evaluates nor closes a condition. Returns 1.
    """
    if controller_now.tzinfo is None:
        raise ValueError("controller_now must be timezone-aware")
    async with sessions() as session:
        await session.execute(
            _INSERT_OBSERVATION,
            {
                "observation_id": uuid.uuid4(),
                "server_id": server_id,
                "source": SOURCE,
                "method": ObservationMethod.COMMAND.value,
                "quality": ObservationQuality.UNKNOWN.value,
                "metric": None,
                "value": None,
                "observed_at": None,
                "collected_at": controller_now,
                "fresh_until": None,
                "retain_until": controller_now + RETENTION_HORIZON,
            },
        )
        await session.commit()
    return 1


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Outcome of one bounded collection attempt.

    ``quality`` is the sample's quality, or ``unknown`` when the target was
    unavailable — the caller-facing shape of acceptance 2. ``error`` carries
    only the stable rejection code.
    """

    quality: ObservationQuality
    observations: int
    server_id: uuid.UUID
    error: str | None = None


async def collect(
    sessions: async_sessionmaker[AsyncSession],
    identity: SshIdentity,
    *,
    key_path: Path,
    known_hosts_path: Path,
    server_id: uuid.UUID,
    controller_now: datetime,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> CollectionResult:
    """Probe a target and persist the sample, or persist the unknown path.

    Never raises for target-side failures (any :class:`SshConnectError`
    becomes a persisted unknown observation); controller misconfiguration
    (unresolvable key reference, missing enrollment) still raises — failing
    closed is the configuration boundary's job, not a collection sample.
    """
    try:
        sample = probe_scheduled_tasks(
            identity,
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            controller_now=controller_now,
            timeout=timeout,
        )
    except SshConnectError as error:
        written = await record_unavailable(
            sessions, server_id, controller_now=controller_now
        )
        return CollectionResult(
            quality=ObservationQuality.UNKNOWN,
            observations=written,
            server_id=server_id,
            error=error.code.value,
        )
    written = await record_sample(
        sessions, server_id, sample, controller_now=controller_now
    )
    return CollectionResult(
        quality=sample.quality, observations=written, server_id=server_id
    )
