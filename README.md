# MAI — AI Administration Control Panel

MAI is a self-hosted, AI-powered administration control panel that understands
the servers it manages, installs and maintains applications, investigates
problems, and keeps authoritative operational documentation. It combines a
windowed desktop-style web workspace with a trusted, delegated administrator
that performs real operations over SSH under configured policy: approve where
required, verify outcomes, document and remember.

Core workflow: **Observe → Understand → Plan → Execute under configured policy
→ Verify → Document and remember.**

## Status (read before deploying)

This is the integrated v1 tree of the MAI campaign (phases P00–P12). What is
claimed here is what the tree actually tests:

- Durable job engine, SSH supervision, approvals/grants/policy, knowledge
  retrieval, monitoring collection, backup/restore and uninstall are covered
  by per-slice producer tests under `backend/tests/` and end-to-end journeys
  in `tests/runtime/test_journeys.py`
  (see `docs/operations/acceptance.md` for the journey → outcome map).
- Every referenced CLI command/flag, HTTP endpoint, Compose profile, catalogue
  recipe and script name in the documentation is verified against the real
  tree by `tests/docs/test_docs_consistency.py`.

Explicit limitations of this release (do not assume otherwise):

- **No published ports.** `compose.yaml` defines no `ports:` — `mai-api`
  (8000) and `mai-web` (3000) listen on the Compose-internal `mai` network
  only. Browser access requires an **operator-supplied TLS reverse proxy**
  attached to that network (see `docs/operations/quickstart.md`). No bundled
  proxy service ships in this release.
- **Single admin, single controller.** One administrator session model, one
  controller host per stack; LAN/Tailscale exposure only — public-internet
  exposure is out of scope.
- **Monitoring is collection, not armed alerting.** Collectors, findings and
  the notification inbox exist and are tested; no paging/messenger delivery
  is wired in v1.
- **Linux only.** Controller and managed targets are Linux over OpenSSH.
  Windows targets, fleets and messenger integrations are non-goals of v1.

## Supported platforms (tested set)

| Role | Requirement |
|---|---|
| Controller host | Linux x86_64; Docker Engine + Docker Compose v2; `uv` on PATH; OpenSSH client; git |
| Backend runtime | Python ≥ 3.14 (installed into `backend/.venv` by `uv sync --frozen`; `backend/uv.lock` is authoritative) |
| Managed target | Fresh Linux host with a running OpenSSH server; root access for the installer; `ufw` or `firewalld` used when present and active |
| Browser | Current desktop Chrome/Firefox (desktop-first; mobile/touch parity deferred per the windowed-workspace contract) |

Exact component versions captured at integration are recorded in
`mai_evidence/12-end-to-end/01-commands.txt` (engine `dbos==3.2.0`,
Postgres fixture `pgvector/pgvector:0.8.1-pg18`, model stack
`pydantic-ai-slim[openai]` over `openai 3.x`).

## Repository map

| Path | Contents |
|---|---|
| `backend/` | Controller backend package (`mai`): API, auth, capabilities, execution, jobs, knowledge, memory, monitoring, plugins, profiles |
| `frontend/` | SvelteKit windowed workspace (adapter-node SSR, container `mai-web`) |
| `compose.yaml` | Core topology: `postgres`, `mai-api`, `mai-web` (default set) + profile-gated `mai-worker`, `searxng`+`valkey`, `hindsight` |
| `catalogue/` | Application recipes (`example-http.yaml`, `example-stateful.yaml`, `local-inference.yaml`, `odysseus.yaml`) |
| `scripts/` | Operator scripts: `install-host.sh`, `backup-restore.sh`, `uninstall-host.sh`, `runtime_check.py` |
| `deploy/` | Service-specific deployment notes (`deploy/hindsight/README.md`) and settings |
| `docs/architecture/` | Frozen contracts and ADRs (index below) |
| `docs/operations/` | Operator runbooks (index below) |
| `docs/plugins/` | Plugin contributor/operator quickstart |
| `tests/` | Cross-cutting suites: `tests/runtime/` (compose/bootstrap/restore/uninstall/journeys), `tests/docs/` (documentation consistency) |
| `experiments/` | P00 probes (`durable_job_probe.py`, `ssh_supervision_probe.py`) — regression gates, not operator tooling |
| `mai_evidence/` | Campaign evidence root (outside disposable worktrees) |

## Architecture & contract index

The frozen cross-module contracts are `docs/architecture/contracts.md`
(sections C01–C11). Specialized decisions and their owners:

| Document | Owns |
|---|---|
| `docs/architecture/contracts.md` | C01 identities/API envelope · C02 canonical files/projections · C03 capabilities/credentials/policy · C04 SSH execution · C05 runs/schedules/locks · C06 agent/context/models · C07 documents/retrieval/memory · C08 UI/API/auth · C09 catalogue/adoption · C10 evidence/testing · C11 windowed UI summary; services ownership map |
| `docs/architecture/auth-and-origin.md` | Single-admin auth, Argon2id bootstrap, session/CSRF/origin model, TLS proxy and exposure rules |
| `docs/architecture/host-supervision.md` | SSH execution, wrapper-owned supervision, verified outcomes (`succeeded`/`failed`/`unknown`) |
| `docs/architecture/job-engine.md` | Durable DBOS-backed engine, guard rules, recovery windows |
| `docs/architecture/knowledge-packaging.md` | Documents/chunking/pgvector retrieval, SearXNG search, Hindsight memory — with per-dependency license verification |
| `docs/architecture/runtime-environment.md` | Disposable fixture discipline (`mai-fxt-*` namespaces, cleanup proofs) |
| `docs/architecture/runtime-scenarios.md` | Scenario registry: producer, gate, fixtures, evidence per scenario |
| `docs/architecture/windowed-workspace.md` | Reusable OS-style windowed UI contract (drag/resize/focus/restore) |

## Operations docs

| Document | Covers |
|---|---|
| `docs/operations/quickstart.md` | Fresh-operator install: plan → apply → first authenticated browser session |
| `docs/operations/recovery.md` | Backup/verify/restore (`scripts/backup-restore.sh`), consistency boundary, index rebuild |
| `docs/operations/uninstall.md` | Key rotation and full uninstall (`scripts/uninstall-host.sh`), exit codes, retention semantics |
| `docs/operations/acceptance.md` | End-to-end journey coverage and fault matrix (what is proven, on which fixtures) |

## Operator surface (summary)

CLI (`python -m mai.cli`, via `backend/.venv` or `uv run --frozen`):
`bootstrap`, `doctor`, `status`, `backup`, `restore`, `uninstall`,
`admin write`, `admin delete` — all require an explicit `--data-root`;
output is value-free; exit `0` success / `1` structured rejection / `2`
usage-IO. See `docs/operations/quickstart.md` for the worked flow.

Compose profiles: the default start set is `postgres` + `mai-api` + `mai-web`;
`--profile worker` adds the durable engine, `--profile search` the SearXNG +
Valkey pair, `--profile memory` the Hindsight service.

## Plugins

Two plugin kinds and one skill format are documented in
`docs/plugins/quickstart.md`: native Python capability plugins (entry-point
group `mai.capabilities`), external MCP servers (stdio/streamable-HTTP
connections with explicit per-server grant bindings), and portable skill
packages under `<data-root>/shared/skills/`. Registration is never
authorization; there is no in-app installer in v1.

## Development

```bash
cd backend && uv sync --frozen            # locked venv (uv.lock)
.venv/bin/python -m pytest ../tests/docs/test_docs_consistency.py -v
ruff format <changed files> && ruff check <changed files>
```

`scripts/runtime_check.py list|run SCENARIO` runs the registered runtime
scenario harness (evidence envelopes under an explicit `--evidence-dir`).
Runtime fixtures use disposable Docker namespaces (`mai-fxt-…`) and print a
cleanup proof; a stray leftover fails the run.

## License

MAI is MIT-licensed — see `LICENSE` (third-party services retain their own
licenses, also summarized there). Credential material never appears in
documentation, logs or CLI output by contract (C03 rule 6); reports must keep
it that way.
