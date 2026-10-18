# Uninstall and host deprovisioning (P12)

Operator runbook for `scripts/uninstall-host.sh` — the exact inverse of
`scripts/install-host.sh` (host bootstrap). It removes ONLY the artifacts that
installer created and never touches pre-existing host state. Run it AS ROOT on
the target host, by the host's administrator, from the same repository
checkout that produced the installed stack.

Related: `docs/operations/recovery.md` (backup before teardown — the backup
runbook tells you to take one `pg_dump` + data-root archive BEFORE any
`uninstall --delete-data`), and the canonical CLI (`python -m mai.cli`) whose
`uninstall` command owns the controller data root.

## What the installer owns (and therefore what uninstall may remove)

| Artifact | Created by | Removed by uninstall |
|---|---|---|
| Service account `mai` (key-only) | `useradd` (only if absent) | only with `--remove-user`, and only when the home holds nothing beyond `.ssh` |
| Controller key line in `~mai/.ssh/authorized_keys` | exact line append | the exact line only; every other line survives byte-for-byte |
| `/etc/sudoers.d/90-mai-<user>` (mode 440, visudo-validated) | managed file, first line `# managed by scripts/install-host.sh` | only when that marker is present; a foreign file at that path refuses the whole run before anything is applied |
| Firewall rule scoped to the controller address + SSH port | `ufw allow from <addr> to any port <port> proto tcp` or the equivalent firewalld rich rule | the exact rule only; neighboring rules are never listed, matched or removed |
| Compose stack (core + optional worker/search/memory profiles) | `docker compose up -d --wait` | `docker compose down`; volumes are purged ONLY with `--delete-data` |
| Controller data root (`servers/` canonical pair) | `mai.cli bootstrap` | only via `mai.cli uninstall` with your explicit intent (below) |

Never touched: sshd configuration, host SSH keys (the pinned `host_key`
fingerprint stays valid), the compose `.env`, unrelated accounts/keys/rules,
and any data-root entry MAI does not own.

## Mode 1 — rotate the controller key (no teardown)

Replace a controller key without touching anything else. Verifiable host
trust is maintained: the host fingerprint is printed unchanged, the canonical
profile (`host_key`, `credential_ref`) needs no edit, and unrelated
authorized_keys lines survive byte-for-byte.

```console
# as root, on the target host
scripts/uninstall-host.sh --print-plan \
  --rotate-key /secure/new_controller.pub --public-key /secure/old_controller.pub
scripts/uninstall-host.sh --yes \
  --rotate-key /secure/new_controller.pub --public-key /secure/old_controller.pub
```

- `--public-key` is the OLD controller key currently authorized; rotation
  refuses to run when that exact line is absent — it never adds new authority
  it cannot tie to the controller.
- The new key is appended once (rerun-safe); the old line is removed by exact
  match. Chaining rotations works: rotate old→new, then new→next.
- Verify afterwards: `ssh -i new_controller_key mai@host` works; the old key
  is refused; the fingerprint printed by the run matches your enrollment
  record.

## Mode 2 — full uninstall (consent-gated teardown)

Exactly one data intent is required — an absent flag is never inferred as
delete (C09 rule 5):

- `--keep-data` retains the controller data root and `.env` (recommended when
  the teardown might be temporary or you want the archive trail).
- `--delete-data` deletes the controller data root through
  `python -m mai.cli uninstall --delete-data` and adds `-v --remove-orphans`
  to the compose down (purging MAI-owned named volumes). The CLI refuses when
  the data root holds top-level entries MAI does not own — move them first;
  managed application data is never removed implicitly.

```console
# inspect the exact inventory first (read-only, exit 0)
scripts/uninstall-host.sh --print-plan \
  --data-root /var/lib/mai --public-key /secure/old_controller.pub \
  --controller-address 203.0.113.10 --ssh-port 22 \
  --stack-profiles core --keep-data

# apply (a tty asks you to type exactly "uninstall"; scripts use --yes)
scripts/uninstall-host.sh --yes \
  --data-root /var/lib/mai --public-key /secure/old_controller.pub \
  --controller-address 203.0.113.10 --ssh-port 22 \
  --stack-profiles core --keep-data
```

Flags mirror the install: `--ssh-user` (default `mai`), `--repo`,
`--env-file`, `--stack-profiles` (same `core,worker,search,memory` tokens —
pass the profiles you installed so `compose down` sees those services).
`--controller-address` must be the exact address/CIDR the rule was scoped to;
blanket targets (`0.0.0.0/0`, `any`) and broader-than-/8 prefixes are refused.

Order of operations: access is revoked first (stack → firewall → grants →
key), the data decision runs last. Every step is idempotent: rerunning a
partial uninstall completes it and reports `absent`/`not-present` instead of
failing or duplicating effects.

### Retention explained (what "keep" means)

- `--keep-data` keeps the canonical data root (profiles, receipts of
  enrollment) and the `.env` with its generated credentials. Keep both when
  you may re-install: `scripts/install-host.sh` preserves them.
- The `.env` is NEVER deleted by the script — the installer never overwrites
  or merges credentials, and the uninstaller cannot distinguish an
  installer-generated `.env` from an older one. Deleting it (`rm -- .env`)
  is a deliberate operator act, recommended only after `--delete-data` and a
  final backup.
- `--remove-user` deletes the service account and its home ONLY when the home
  contains nothing beyond `.ssh` (the key-only account as provisioned). A
  pre-existing account with other content refuses the run in its preflight —
  revoke the key/grants instead and decide about the home yourself.

## Exit codes and refusals

`0` success or plan printed · `1` refusal or step failure (foreign sudoers
file, foreign home under `--remove-user`, CLI rejection, old key absent in
rotation) · `2` usage error (missing/contradictory arguments, blanket
addresses, missing consent for non-interactive runs).

A refusal BEFORE any effect is the contract: the preflight inspects the
current state read-only, the plan prints it, and nothing mutates before
consent. If a later step fails, rerun after fixing — completed steps report
as already done.

## Verification seams (tests only)

`MAI_UNINSTALL_SKIP_ROOT_CHECK=1`, `MAI_UNINSTALL_SANDBOX=DIR` and
`MAI_UNINSTALL_CLI="CMD..."` exist for the disposable-fixture test lane
(`tests/runtime/test_uninstall.py`); never set them in production. The
sandbox suite round-trips the REAL installer and the REAL `mai.cli` producer
per test; real sshd/ufw/firewalld/docker effects are covered separately by
the P12 runtime-target scenario (rotate → old key denied/new key works,
uninstall with keep-data, unrelated app/SSH still functional).
