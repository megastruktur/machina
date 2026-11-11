# Decision: op-daemon.sock must not live in MAI_DATA_ROOT (t_e8178c8f, amended by t_8168ffeb)

Date: 2026-11-09 · Owner: devops · Status: LANDED on master (1b3df47, mirror synced) — activates at the next Komodo DeployStack of `machina`
Amended: 2026-11-11 (t_8168ffeb) — the compose env alone was INSUFFICIENT; see "Second finding" below.

## Problem

`scripts/backup-restore.sh backup` (recovery.md P12, canonical whole-instance
backup) refuses with `unexpected_entry` when the production data root contains
`op-daemon.sock`. The producer `python -m mai.cli backup` deliberately refuses
non-regular entries (C02 rule 3) — that refusal is correct and stays.

## Root cause (evidence, 2026-11-09)

- Both `machina-mai-api-1` and `machina-mai-worker-1` run with
  `HOME=/var/lib/mai`, which IS the `machina_mai-data` volume = MAI_DATA_ROOT.
- The `op` CLI (2.42.0, present in the backend image for the secret tools
  enabled in d5e83c5) writes `$HOME/.config/op/` (config + `op-daemon.sock`)
  whenever the agent API spawns it. When the daemon exits, a dead socket file
  remains in the data root.
- Not a one-off Oct-23 leftover: the socket re-appeared 2026-11-09 14:43 UTC,
  minutes after the d5e83c5 deploy + secret-tool exposure test. Every
  secret-tool session re-creates it.
- Liveness probes at removal time: socket absent from `/proc/net/unix` in both
  containers → dead file, safe to remove. Removed 2026-11-09 17:49 local
  (14:49Z); full audit of the data root found no other socket/fifo/symlink.
- Controlled probe in the api container: with `XDG_CONFIG_HOME` set, `op`
  wrote only to `$XDG_CONFIG_HOME/op` and left `$HOME` untouched. `op`
  honors XDG_CONFIG_HOME.

## Decision

`.config/op/` shall not live in MAI_DATA_ROOT. Redirect it with compose env,
NOT by changing HOME (the app may use HOME for other paths):

    # machina compose.yaml, api + worker services:
    XDG_CONFIG_HOME: /tmp/op-xdg

- Ephemeral per container: correct for a daemon socket; op's 84-byte CLI
  config file auto-regenerates (no secret values in it).
- Effect: op daemon writes to /tmp/op-xdg/op/ (container-local), data root
  stays all-regular, canonical backup never trips on it again.
- Apply together with the next compose change that triggers a Komodo
  DeployStack — no dedicated deploy for this alone.
- Landed 2026-11-09: master 1b3df47 (compose.yaml env for mai-api +
  mai-worker), GitHub pushed, Forgejo mirror synced (token minted/pushed/
  provably revoked, 0 residual rows). The next DeployStack activates it —
  post-deploy verification per t_ca7abe1f.
- After the redirect is deployed, optionally remove the leftover regular
  file `.config/op/config` from the data root (harmless either way).

## Guard in the meantime

SUPERSEDED by the t_8168ffeb fix (below): `mai.cli backup` now skips dead
unix sockets itself, so the manual rm dance is no longer needed. Kept for
history: pre-deploy backup operators were told to verify no listener
(`docker exec <api|worker> grep op-daemon /proc/net/unix` — empty = dead)
and `sudo rm` the socket, then re-run. Removal was safe only with an empty
grep result in BOTH containers.

## Second finding (t_8168ffeb, 2026-11-11): the compose redirect alone was insufficient

**Symptom recurrence.** The socket reappeared at
`.config/op/op-daemon.sock` (mtime 2026-11-10T10:02:21Z) despite
`XDG_CONFIG_HOME=/tmp/op-xdg` confirmed present in both containers, and
`/tmp/op-xdg` was never even created inside them — the redirect never
absorbed anything. It blocked the 3a42472 release backup (third
occurrence).

**True root cause.** The backend does not inherit the compose env when it
spawns `op`: `OnePasswordClient._child_env`
(backend/src/mai/credentials/onepassword.py) deliberately scrubbed the
child environment to exactly `PATH`, `HOME` and the token key — a
documented security boundary (C03 rule 6) — and `XDG_CONFIG_HOME` was
dropped with it. Every backend-driven `op` invocation (secret tools, op
plugin lane) therefore ran with the DEFAULT config path
`$HOME/.config/op/`, and since `HOME=/var/lib/mai` IS the `machina_mai-data`
volume, op re-created its daemon socket there on every session. The
2026-11-09 "op honors XDG" probe passed because `docker exec` inherits the
compose environment — it probed a different execution path than the
backend's own spawn.

**Fix A (creator honors the redirect).** `_child_env` now passes
`XDG_CONFIG_HOME` through when the controller process itself has it set
(compose sets it; unset stays unset — no invented values). It is a
path-only value, never secret material, so the scrub boundary is intact.
After the next backend image deploy, backend-spawned `op` writes its
socket under `/tmp/op-xdg/op/` (container-local, wiped on restart) and the
data root never sees it again.

**Fix B (producer tolerance, active immediately on the host).**
`python -m mai.cli backup` liveness-probes unix-socket entries in the
data root: a DEAD socket (connect → ECONNREFUSED/ENOENT — nothing
listening, the steady state after a daemon/container exit) is SKIPPED and
recorded in the archive manifest (`skipped_dead_sockets`) and the summary
line; a LIVE socket (connect succeeds) is still refused
(`unexpected_entry`) — an active daemon mid-backup is an operational fact
the operator must see. Symlinks, FIFOs, devices: refused unchanged
(C02 rule 3). Proven live 2026-11-11: canonical
`scripts/backup-restore.sh backup` + `verify` both exit 0 with a dead
socket planted at the exact original volume path
(`machina-sockfix-proof-20261111T184017Z.tar.gz`, members=5, manifest
records the skip).

**Deployment note.** Fix B is host-side and effective now (the script's
venv is an editable checkout). Fix A rides the next backend image build +
Komodo DeployStack — no dedicated deploy for this alone (same policy as
the original decision). Until that deploy, the socket may still
occasionally appear in the volume; backups proceed regardless (Fix B).

## Verification of the canonical path (2026-11-09, post-removal)

- backup: exit 0, 6,492,615 bytes,
  sha256 10f80ecbca14525ecbd55e7ea3a5789f6c10cc5f410ed46ed126899338b96efc,
  archive /mnt/synology/backups/machina/machina-canonical-20261109T145126Z.tar.gz
- verify: exit 0, members=5
- stack after: api/worker/web healthy, local healthz 200, data root 0
  non-regular entries.
