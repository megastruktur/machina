# Decision: op-daemon.sock must not live in MAI_DATA_ROOT (t_e8178c8f)

Date: 2026-11-09 · Owner: devops · Status: LANDED on master (1b3df47, mirror synced) — activates at the next Komodo DeployStack of `machina`

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

Pre-deploy backup operators: if `backup` refuses with `unexpected_entry` on
`.config/op/op-daemon.sock`, verify no listener
(`docker exec <api|worker> grep op-daemon /proc/net/unix` — empty = dead)
and `sudo rm` the socket, then re-run. Removal is safe only with an empty
grep result in BOTH containers.

## Verification of the canonical path (2026-11-09, post-removal)

- backup: exit 0, 6,492,615 bytes,
  sha256 10f80ecbca14525ecbd55e7ea3a5789f6c10cc5f410ed46ed126899338b96efc,
  archive /mnt/synology/backups/machina/machina-canonical-20261109T145126Z.tar.gz
- verify: exit 0, members=5
- stack after: api/worker/web healthy, local healthz 200, data root 0
  non-regular entries.
