# Host command supervision across SSH loss — decision and operating rules (P00)

Status: accepted P00 output for card `mai-00-ssh-spike`. Consumers: P04 (`ssh-exec`,
`supervisor-adapter`, `reconciliation`, `host-key-pinning`) and the `execution`
module row in the service architecture. Input contracts: C04 (SSH execution and
remote outcomes), C01 (opaque operation IDs), C10 (evidence). Sibling decision:
`job-engine.md` (its guard rules 1–4 hold at the target level and are re-measured
here). Evidence: `mai_evidence/00-ssh-spike/` at the campaign root (2026-10-12,
disposable `panubo/sshd` fixture container, probe source
`experiments/ssh_supervision_probe.py`).

## Decision

**Command ownership on a target host is a target-local supervision wrapper plus a
durable receipt directory, established over SSH before the effect, never the SSH
session itself.** On targets with systemd, the same receipt discipline is carried
by a `systemd-run` transient unit (cgroup-exact cancellation, journal exit codes);
on targets without systemd (measured here), a POSIX-sh wrapper with an inner
`setsid` payload session carries it. Both variants share one receipt contract, so
run/jobs/UI code never learns which is active. **No new privileged network daemon
on targets** (C04 constraint): the "supervisor" is a short-lived per-operation
process plus on-disk receipts, not a resident service.

The fixture had no systemd (PID 1 = image entrypoint bash; no `systemctl`, no
`/run/systemd`; documented gap per the dispatch addendum). The wrapper variant is
therefore the measured scheme; the transient-unit variant is design-inference from
documented systemd semantics and MUST be boundary-verified on a systemd target
before it is load-bearing (see Limitations).

## Measured semantics (what the spike proved)

Fixture: `panubo/sshd` (busybox/Alpine) at `127.0.0.1:2222`, user `maiprobe`,
key auth via `/etc/authorized_keys` provisioned with `docker exec` (bind-mount
paths resolve on the Docker daemon host, not the caller — recorded as a fixture
rule). Payload: benign `sh` tree with three `sleep` children.

| # | Experiment | Result | Measured facts |
|---|---|---|---|
| 0 | preflight | **PASS** | key auth works; **no systemd in fixture** (`/proc/1/comm`=bash); busybox `setsid/base64/awk/ps/sleep` present; `date +%N` unsupported → receipts use second-resolution UTC + monotonic `seq` |
| 1a | session-attached tree, client killed mid-run | **measured** | the 3 sleeps **survived as unidentified orphans** — sshd does not SIGHUP-kill non-tty children; and **no receipt exists** for such an operation: attachment is not ownership (uncontrolled death-by-channel or unownable orphan) |
| 1b | wrapper-owned detached tree, client killed mid-run | **PASS** | payload survived SSH loss as its own session (sid = payload pid, detached from sshd sessions); NEW session identified it from receipts: state `running`, pid + `/proc` start ticks match, ppid-closure found all 3 descendants; dispatch intent was persisted before submission |
| 2 | cancellation requested and verified | **PASS** | identity guard (pid + start ticks) ran before any signal; `kill -TERM -<pgid>` killed payload + descendants (verified via fresh ppid closure, TERM→KILL escalation armed); wrapper survived and recorded `exit_code=143`; re-cancel with forged ticks was **refused** (PID-reuse guard) |
| 3 | supervisor (wrapper) `kill -9` mid-operation | **PASS** | payload orphaned but alive (reparented to PID 1); receipt frozen at `running` with **no exit record — permanently**; heartbeats stopped (before=1, after=1) = staleness signal; outcome is `outcome_unknown` until an **explicit** reconciliation killed the orphan and appended its own receipt note |
| 4a | `docker restart` mid-operation (boot analogue) | **PASS** | host keys **unchanged** (pinning held); receipt directory survived (container fs) but state frozen `running` with payload identity **absent from /proc** → classification **derived**: `outcome_unknown`; `boot_id` and `/proc/uptime` **did NOT change** (kernel-shared: not restart detectors on container targets); container `Id`+`StartedAt` did change |
| 4b | full container recreate | **PASS** | reconnect with stale pinning **refused** (`REMOTE HOST IDENTIFICATION HAS CHANGED`, rc=255); receipt directory **gone** (fresh fs); old operation unresolvable — `outcome_unknown` by construction |

## Receipt contract (the frozen part for consumers)

Per operation `<op_id>` (opaque UUID, C01), one directory under a **durable,
service-owned** target path (real hosts: e.g. `/var/lib/mai/receipts/`; the
fixture used `$HOME/mai-receipts/` — container-fs durability only, measured
lost on recreate):

- `state` — atomically replaced (`state.tmp` + `mv`), key=value: `op_id`, `seq`
  (monotonic), `state` ∈ `starting|running|exited`, `ts` (UTC), `boot_id`
  (kernel boot id at dispatch), `wrapper_pid`, `payload_pid`,
  `payload_startticks`, `exit_code` (only on `exited`).
- `events.log` — append-only transitions plus per-second `hb payload_pid=…`
  heartbeats (freshness signal) and post-hoc reconciliation notes.
- `stdout.log` / `stderr.log` — bounded output capture.

Identity rule: a receipt names a live process only if `/proc/<payload_pid>/stat`
exists AND field-22 start ticks equal `payload_startticks`. Anything else is
stale. C04 `CommandOutcome` mapping: `exited`+0 → `succeeded`; `exited`+≠0 →
`failed`; `running`+fresh heartbeat+resolving identity → `running`; `running`+stale
heartbeat or unresolved identity → `outcome_unknown`; `cancel_requested` is
recorded by the controller until host-verified `cancelled` (the receipt itself
never says "cancelled" — it records the actual exit, e.g. 143).

## Binding rules for P04/P12 (spike-derived)

1. **Persist dispatch intent before submission; the SSH session is never the
   owner.** Losing the channel must not lose the operation (exp1b) — and an
   operation without receipts is unownable even if its processes live (exp1a).
2. **Verify-then-signal cancellation.** Identity guard first (PID-reuse negative
   control measured), then TERM the payload session group, escalate to KILL,
   verify the ppid-closure is empty, and only then report `cancelled`. The
   receipt's exit code (143), not the cancel request, is the host truth (exp2).
3. **Supervisor death ⇒ `outcome_unknown`, never auto-succeeded, never auto-retry.**
   Exit capture dies with the supervisor (exp3). Repair is an explicit
   reconciliation operation that resolves live state, closes orphans, and appends
   its own receipt note. This is the target-level twin of job-engine rule 2.
4. **Never assume transient ownership artifacts survive a reboot.** Measured:
   receipts frozen stale + identity unresolved after restart; receipts *gone*
   after target replacement; `boot_id`/`uptime` do not move on container restart.
   Classification `outcome_unknown` must be derived from (stale state + unresolved
   identity + observed restart), asserted from measurements, never hardcoded.
   Container targets need control-plane instance identity (`Id`+`StartedAt`) or an
   explicit instance-epoch file; real hosts use `machine-id`+`boot_id`.
5. **Host-key pinning is absolute on reconnect.** A recreated/reattached target
   that fails pinned verification is refused, never auto-trusted (exp4b, C04);
   re-enrollment is an explicit admin action with the new fingerprint recorded.
6. **Scheme selection is a per-target profile capability:** probe `pid1`/`systemctl`
   at profile time. systemd → transient unit carries cgroup-exact cancellation and
   exit capture, receipts still authoritative (units do not survive reboot —
   rule 4 applies unchanged); no systemd → wrapper variant as measured. Both
   variants emit the same receipt contract (§ above).
7. **Descendants are enumerated by ppid closure from the receipt's payload pid**
   for identification and post-cancel verification (exp1b/exp2); cancellation must
   account for the whole closure before reporting success (C04).

## Rejected alternatives

- **Session attachment (plain exec channel):** measured unowned — orphans without
  receipts, death-by-channel without record (exp1a).
- **Target-resident supervisor daemon:** violates C04 "no new privileged network
  daemon"; the per-operation wrapper provides the needed supervision without a
  resident, attackable service.
- **PID-only or channel-liveness ownership:** PID reuse is real (guard negative
  control, exp2); channel liveness ≠ command progress (C04) — measured in exp1a/1b.

## Limitations

- Single disposable fixture, single unprivileged user, no real host reboot: the
  reboot analogue is `docker restart`/recreate on a busybox container; real-host
  reboot semantics (`machine-id`, `boot_id` change, systemd transient loss) are
  documented-consistent but not measured here.
- The systemd transient-unit variant is NOT measured (fixture has no systemd);
  before P04 relies on it, run the same probe roles against a systemd-capable
  disposable target (Arch container or VM fixture) — the probe is scheme-parameter
  ready but currently implements the wrapper variant only.
- Output budgeting/redaction, privilege modes (root/`sudo`), and multi-target
  concurrency are C04 contract concerns not exercised by this spike.
- Heartbeat granularity is 1 s; expiry thresholds for "stale" are adapter policy,
  not frozen here.

## Provenance

Recorded 2026-10-12 on branch `megastruktur/mai-00-ssh-spike`; probe
`experiments/ssh_supervision_probe.py` (roles: `preflight`, `exp1`…`exp4`,
`cleanup`, `all`; verdict JSON per role, exit 0/2). Every command in evidence was
executed by the probe on 2026-10-12 against fixture `mai-fxt-00-ssh-sshd`
(image id in `preflight/image-id.txt`); exact command lines per role in
`<role>/commands.log`, timelines in `<role>/journal.log`, per-experiment verdicts
in `<role>/verdict.txt`. All five roles returned PASS; fixture destroyed and
temp keys removed (see `cleanup-proof.txt`). The coordinator host was never an
SSH target and retains no sshd/systemd changes.
