# Operations — backup, restore and index rebuild (card 12-backup-restore)

Authoritative recovery procedure for one MAI instance. Anchors:
`docs/architecture/contracts.md` C02 (canonical files, W1–W4), C03 rules
2/6 (secret boundary), C05 guard rules 8–9 (single `pg_dump`, deliberate
recovery), C07 rules 2–3 (reindex outbox, banks backed up never rebuilt),
C10 rule 2 (disposable-fixture discipline); scenario registry row
`BACKUP-RESTORE` in `docs/architecture/runtime-scenarios.md` (P12 gate).

Tool: `scripts/backup-restore.sh` (one script, three subcommands:

```bash
scripts/backup-restore.sh backup  --data-root DIR --env-file FILE --output ARCHIVE.tar.gz \
                                  [--compose-file FILE] [--project NAME]
scripts/backup-restore.sh verify  ARCHIVE.tar.gz
scripts/backup-restore.sh restore ARCHIVE.tar.gz --data-root DIR \
                                  [--compose-file FILE] [--project NAME]
```

Every command prints exactly one JSON line on stdout and exits 0, or one
`{"error":{"code","message"}}` line on stderr and exits 1 (2 = usage).

## 1. What a backup contains — and the secret boundary

The bundle is a sorted, fixed-mtime/owner `.tar.gz` (gzip `-n`) of one
`mai-backup/` directory:

| member | content | producer |
|---|---|---|
| `postgres/mai.sql.gz` | one plain `pg_dump` of the `mai` database — the Alembic schema chain 0001..0007 **plus** the engine-owned `dbos` schema (runs/steps/schedules state) | `pg_dump --format=plain --no-owner --no-privileges` |
| `postgres/hindsight.sql.gz` | one plain `pg_dump` of the dedicated `hindsight` memory database | same |
| `files/data-root.tar.gz` | the canonical data root: `servers/<id>/profile.yaml`, prompts, `applications/` manifests, canonical `docs/`, and the durable `reindex/` intent outbox | `python -m mai.cli backup` (12-cli-ops) |
| `config/env-names.txt` | sorted credential NAMES parsed from the configured `.env` | `mai.credentials.env.parse_env_text` |
| `config/stack.json` | value-free provenance: compose project/file name, pg user, database list | script |
| `SHA256SUMS` + `manifest.json` | per-member digests; the manifest pins the sheet's own digest | script |

**C03 boundary (binding):** credential VALUES never enter the archive.
The configured `.env` is reduced to NAMES through the reviewed parser; the
database dumps carry credential *references* only (no role passwords are
dumped, `--no-privileges`); the file half is canonical profile/document
content, which holds no secret material by construction (C03 rule 2:
values are never serialized into profiles/context). Re-provisioning `.env`
values after a restore is a deliberate operator act — the archive gives
you the name list to check against, not the values.

Determinism: member ordering, `gzip -n`, `tar --mtime=@0 --owner=0
--group=0 --numeric-owner` make the script's own outputs byte-stable for a
byte-stable state; the `mai.cli` file archive embeds file mtimes by design
(producer-owned format), so the whole bundle is checksum-verified rather
than claimed byte-identical across captures.

## 2. Consistency boundary (what one backup does NOT promise)

The two dumps and the file snapshot are **sequential, not atomic**. The
script therefore captures the DATABASE first and the FILES second. Any
file-side delta against the DB snapshot then carries its durable `reindex`
intent in the (younger) file snapshot — exactly the C02 W2/W4 recovery
postulate (file authoritative; projection reconciles from file
hash/revision; intents are journaled before effects). The restore step §3.5
closes the gap: reconciling every scope converges the derived index from
canonical files. Applications that mutate BOTH Postgres and files outside
this journal (none today) would violate the boundary.

## 3. Restore into a fresh instance

Prerequisite: the target Postgres is **freshly provisioned** (empty `mai`
database, no `dbos` schema, `hindsight` absent or empty) and the target
data root does not exist. The script refuses anything else
(`database_not_fresh` / producer's `target_not_clean`) BEFORE the first
restored byte. The script never creates/starts/stops containers: provision
the fresh Postgres yourself (e.g. the stack image `pgvector/pgvector`,
then `docker compose up postgres` with a fresh volume, or a disposable
fixture container).

What `restore` does, in order:

1. **Verify everything** — manifest↔SHA256SUMS digest, sheet format, every
   member digest, member path safety (absolute/`..` refused; GNU tar
   additionally refuses such member names at extraction). Any tamper ⇒
   exit 1 before any effect.
2. **Freshness guard** on the target Postgres (see above).
3. **File half** via the reviewed producer `python -m mai.cli restore`
   (it re-verifies internally, refuses symlink/traversal members and a
   non-clean target, lands 0600/0700). Runs before the DB load so a
   rejected file half leaves the fresh database untouched.
4. **Database load** — `gunzip | psql --set=ON_ERROR_STOP=1` per database;
   `hindsight` is created first when absent.
5. **Summary + operator checklist** (below). `engine_auto_start` is
   always `false`.

### 3.5 Derived index rebuild (required, not optional)

The chunk/vector projection is DERIVED data (C07 rule 3). Bring it to a
consistent state from the restored canonical files by reconciling every
scope's reindex outbox:

- through the API: `POST /api/v1/docs/index/reconcile` per scope
  (per-intent outcomes; an embeddings outage leaves intents `pending`,
  never a fabricated `indexed`), or
- through the reviewed `ScopeIndexer.reconcile_scope(...)` seam for
  headless operation.

This is idempotent and converges regardless of how long the outage was.

### 3.6 Stale dangerous jobs (what restore deliberately does NOT do)

Restored `dbos.workflow_status` rows ride the dump **untouched**: PENDING
stays PENDING, ERROR stays ERROR. The script starts no worker, no engine
and never calls resume. Before the FIRST worker relaunch on restored
state:

1. inspect pending/error runs through the reviewed jobs surface (C05;
   the engine module owns all `dbos` reads);
2. cancel stale/dangerous runs deliberately via that surface — they are
   never resumed implicitly;
3. only then relaunch `mai-worker`. DBOS launch recovery then resumes
   PENDING workflows exactly once, filtered by the pinned
   `application_version` (`mai-1`; engine rule 8 + job-engine exp7).
   ERROR runs stay ERROR until an explicit `EngineClient.resume_run`.

## 4. When to back up / how often

- before every stack upgrade (`git pull` + image change) and before any
  `uninstall --delete-data`;
- on a schedule you own (this card ships no scheduler — the engine's own
  schedule loops are for MAI jobs, not operator backups);
- store archives outside the instance host when feasible; `verify` any
  archive before trusting it.

## 5. Limits (disclosed)

- The script targets the documented compose topology (service `postgres`,
  user `mai`, databases `mai` + `hindsight`). Other topologies are out of
  scope; `MAI_BACKUP_PG_CONTAINER` is a documented seam for disposable
  fixtures, not a second production path.
- Non-regular entries in the data root (symlink escape attempts) are
  refused by the producer; a refusal aborts the backup before the bundle
  is written.
- `pg_dump` of `mai` includes the `dbos` schema — engine-owned state
  travels with the single dump (guard rule 9). Engine-SCHEMA MIGRATIONS
  across version upgrades are the worker's launch duty, not the
  restore's.
- Atomicity across files/DB is bounded as §2; nothing here invents a
  two-phase snapshot.
