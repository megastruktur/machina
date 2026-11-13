<div align="center">

<img src="docs/assets/machina-cog-skull.svg" alt="Machina cog-skull emblem" width="88" />

# Machina — Self-Hosted AI Administration Control Panel

**M.A.I. — Machina Anima Intelligentia**

<img src="docs/assets/machina-hero-banner.png" alt="Machina banner: session-first chat column, categorized Server Service Map, policy-gated OpenSSH, DBOS durable runs" width="100%" />

</div>

Machina is a self-hosted administration control panel for Linux servers. It pairs a Mechanicus-styled desktop windowed web workspace with an approval-gated AI administrator that inspects hosts over OpenSSH, manages Docker Compose applications, investigates incidents, tracks health and security posture, and maintains living operational documentation.

Every action follows an explicit loop:
**Observe → Understand → Plan → Execute under policy → Verify → Document and remember.**

---

## Key Capabilities

- **Session-First Chat Column:** The console's conversation surface is a centered, session-first chat column — the stage of the workspace, not a window to lose. With no active session it renders a compact **hero command plate** (scope strip + composer); submitting the first turn grows the same surface to a full-height transcript. Scope is explicit on a unified **scope strip**: a `NODE` server selector plus an `@service` picker over the server's mapped services, interactive while drafting and read-only once a session is bound (or **General Consult** for server-less questions). Past conversations live in the left drawer's **Chat History** and restore with their recorded scope; the durable transcript, tool-call stack and delegation cards stream over a per-session WebSocket channel (`session.read`).
- **Categorized Server Service Map:** Every adopted application renders as one map row with category chips (8 built-ins — `games`, `ai`, `agent`, `system`, `dev`, `infrastructure`, `media`, `observability` — plus operator-minted custom categories), client-side filter pills, per-server **custom instructions** injected into the agent's `@service` context, and a five-slot operational runbook (install, update, uninstall, backup, verify) recorded through `app.upsert_map_entry`. **Update** and **Remove** queue background runbook jobs — the agent executes the procedure under policy while the chat column stays untouched.
- **Global Runbook Catalog:** A `Global / Fleet` window over the server-agnostic runbook recipes (`/api/v1/applications/catalog`): paginated, searchable, category-filtered, with create/edit/delete. Every server's Applications window lists catalog runbooks that are not installed as **Available** cards with **Install** / **Refresh** (verify) actions and per-server custom-instruction drafts.
- **Desktop-Style Windowed Workspace:** Multi-window SvelteKit interface with draggable, resizable panels for Server Profiles, Capability Policy, Jobs & Schedules, Terminal / Execution Receipts, Agent Chat & Delegation, Knowledge & Documents, Application Inventory, Runbook Catalog, Plugins, MCP & Plugin Forms, Model Settings, Overview Dashboard, and Memory & Local Models. A collapsible left side drawer carries the server tree, chat history and panel launcher; opened windows float over the line-grid canvas and stack above the chat column.
- **Supervised SSH Execution & Policy Gates:** Key-only OpenSSH supervision with pinned host-key fingerprints, explicit capability grants (`none`, `narrow`, `broad`), one-use approval tokens for mutating operations, and secret-redacted command receipts.
- **Durable Job Engine & Scheduling:** PostgreSQL-backed (`dbos`) workflows with lease fencing, crash recovery without blind re-execution of dangerous host steps, and cron/interval scheduling.
- **Application Lifecycle Management:** Discover running services, adopt existing stacks, or install/update/remove curated digest-pinned recipes (`example-http.yaml`, `example-stateful.yaml`, `odysseus.yaml`) with automatic health-gate rollback.
- **Knowledge, Retrieval & Optional Long-Term Memory:** Markdown document store with `pgvector` + full-text hybrid search, optional SearXNG web search (`--profile search`), and optional Hindsight conversational memory (`--profile memory`).
- **Monitoring, Security Posture & Notifications:** Read-only collectors for system resources, network listeners, processes, cron/systemd timers, SSH/firewall/privilege posture, and package/image updates, feeding a deduplicated findings & notification inbox.
- **Plugins, MCP & Skills:** Extend capabilities via Python entry-point plugins (`mai.capabilities`), stdio or streamable-HTTP Model Context Protocol (MCP) servers, and portable skill packages (`docs/plugins/quickstart.md`).
- **Whole-Instance Backup, Restore & Clean Uninstall:** Checksum-verified backups of PostgreSQL state and canonical files (`scripts/backup-restore.sh`), plus consent-gated host deprovisioning (`scripts/uninstall-host.sh`).

---

## The Machina Workspace

### Command Deck — the session-first chat column at center stage

![Machina command deck: left side drawer with server tree and chat history, centered session-first chat column with a live transcript and tool-call stack over the line-grid canvas](docs/assets/screenshot-command-deck.png)

The workspace opens on the chat column: a centered, session-first surface over the line-grid canvas, with the collapsible side drawer (server tree, **Chat History**, panel launcher) on the left. A live session shows its transcript as turn groups — assistant prose with a vertical, one-click **tool-call stack** and delegation cards on a telemetry rail — while the composer stays anchored at the bottom. Operational surfaces open as draggable, resizable windows that float above the column; the chat steps back a depth echelon while a window is focused and returns when you interact with it again.

### Chat Hero — the command plate before a session starts

![Machina chat hero: compact centered command plate with the NODE and @service scope strip and the composer](docs/assets/screenshot-chat-hero.png)

With no active session the column hugs a compact **hero command plate**: the `COGITATOR DIRECTIVE // READY` sigil, the interactive scope strip (`NODE` server selector + `@service` picker), and the composer. Submitting the first turn grows the same card to the full-height transcript — focus and draft survive the transition.

### Server Service Map — every adopted application, categorized

![Machina Server Service Map: category chips, filter pills, operational runbooks and background runbook actions](docs/assets/screenshot-service-map.png)

The Application Inventory panel doubles as the Server Service Map: adopt a discovered stack, categorize it (built-in taxonomy or mint a custom category), describe it, and record its five-slot runbook plus server-specific **Custom instructions** — all multi-line, all injected into the agent's `@service` context. Every installed row offers **✎ Edit**, background **Update**/**Remove** runbook jobs (the conversation never opens), and an **Available from Runbook Catalog** section below lists every global recipe not yet installed, with **Install**, **Refresh** (verify), and per-server instruction drafts.

### Machina Login Sanctuary

![Machina Login Sanctuary with the cog-skull emblem](docs/assets/screenshot-login-sanctuary.png)

Single-administrator sign-in under the cog-skull sigil: the setup bootstrap creates the one administrator credential, after which the `/login` sanctuary is the only gate to the workspace.

### Architecture Flow

![Machina architecture flow: operator browser to reverse proxy, mai-web workspace, mai-api policy engine, OpenSSH targets, DBOS durable jobs, Postgres, optional search and memory profiles](docs/assets/machina-architecture-flow.svg)

---

## Requirements

| Component | Requirement |
|---|---|
| **Controller / Target Host** | Linux x86_64 with OpenSSH server, `git`, and root access (`sudo`) for initial host provisioning |
| **Containers** | Docker Engine + Docker Compose v2 (`docker compose version`) |
| **Python Toolchain** | `uv` on `PATH` (manages Python ≥ 3.14 and locked dependencies in `backend/uv.lock`) |
| **Firewall (Optional)** | `ufw` or `firewalld` (when active, the installer adds a single rule scoped to `--controller-address`) |
| **Browser Access** | Desktop Chrome or Firefox via the bundled `mai-proxy` behind an operator TLS layer on LAN or Tailscale |

---

## Quick Deployment (Single-Host Setup)

For the complete runbook and troubleshooting table, see [`docs/operations/quickstart.md`](docs/operations/quickstart.md).

### 1. Clone the repository & generate a controller SSH key

```bash
git clone git@github.com:megastruktur/machina.git
cd machina

ssh-keygen -t ed25519 -f ~/.ssh/mai_controller -N "" -C mai-controller
sudo cp ~/.ssh/mai_controller.pub /root/mai_controller.pub
```

### 2. Preview the installation plan (read-only)

Generate a permanent server UUID and run `scripts/install-host.sh` with `--print-plan` to inspect every account, sudoers file, firewall rule, and container profile before anything is touched:

```bash
SERVER_ID="$(uuidgen)"
echo "Server ID: $SERVER_ID"

sudo bash scripts/install-host.sh \
  --server-id "$SERVER_ID" \
  --data-root /var/lib/mai \
  --public-key /root/mai_controller.pub \
  --grant-scope narrow \
  --controller-address 192.168.1.10/32 \
  --stack-profiles core,worker \
  --print-plan
```

- `--controller-address` must be your admin workstation/controller IP or subnet (`/8` or narrower; blanket rules like `0.0.0.0/0` are refused).
- `--grant-scope` chooses sudo privileges for the `mai` service account: `none` (no sudo), `narrow` (`systemctl start/stop/restart/status docker.service`), or `broad` (`NOPASSWD: ALL`).
- `--stack-profiles` selects which Compose profiles to start: `core` (`postgres`, `mai-api`, `mai-web`, `mai-proxy`), `worker` (`mai-worker` durable job runner), `search` (`searxng` + `valkey`), `memory` (`hindsight`).

### 3. Apply the installation

Re-run with `--yes` (or answer the interactive prompt) to provision the `mai` SSH user, scoped sudoers/firewall rules, mode-`0600` `.env` secrets, Docker Compose services, and canonical server profile under `/var/lib/mai`:

```bash
sudo bash scripts/install-host.sh \
  --server-id "$SERVER_ID" \
  --data-root /var/lib/mai \
  --public-key /root/mai_controller.pub \
  --grant-scope narrow \
  --controller-address 192.168.1.10/32 \
  --stack-profiles core,worker \
  --yes
```

### 4. Verify stack health

```bash
cd backend && uv sync --frozen
.venv/bin/python -m mai.cli status --data-root /var/lib/mai
.venv/bin/python -m mai.cli doctor --data-root /var/lib/mai --env-file ../.env
docker compose --env-file ../.env ps
```

### 5. Reach the browser (bundled proxy + operator TLS)

`compose.yaml` ships a single bundled reverse proxy, **`mai-proxy`** (Caddy), the only component that publishes a host port — bound to loopback at `127.0.0.1:${MAI_PROXY_HTTP_PORT:-8390}`. It routes `/api/*` → `mai-api:8000` (path preserved) and everything else → `mai-web:3000`, so the browser talks to one same-origin endpoint (`deploy/proxy/Caddyfile`).

Terminate TLS in front of it with your own layer (Tailscale Serve, or Caddy/Nginx/Traefik on the `mai` network), then make the API trust that origin and proxy — via a Compose override that adds to the `mai-api` service environment:

- `MAI_PUBLIC_ORIGINS` — the exact browser origin allowed to mutate (e.g. `https://mai.lan.example`); no wildcards.
- `MAI_TRUSTED_PROXIES` — the CIDR/name peers allowed to set forwarded headers (the pinned `mai` subnet for the bundled proxy); any other peer's forwarded headers are ignored.

See [`docs/operations/quickstart.md`](docs/operations/quickstart.md) §6 for the origin/CSRF/forwarded-header rules.

### 6. Bootstrap the administrator password (one-shot)

On first boot, MAI starts in setup state. Initialize the single administrator credential via the web UI or `POST /api/v1/auth/bootstrap`:

```bash
curl -fsS -X POST https://mai.lan.example/api/v1/auth/bootstrap \
  -H 'Content-Type: application/json' \
  -d '{"password": "<your-strong-admin-password>"}'
```

Once created, `POST /api/v1/auth/bootstrap` is permanently locked (`409 Conflict`) and you can sign in at your configured origin.

### 7. Preparing & Onboarding a Target Server (SSH User & Permissions)

Machina inspects and manages target servers **exclusively over OpenSSH** (`mai-api` / `mai-worker` → `ssh <ssh_user>@<target_host>`). Even when onboarding the local host (`megaserver`) or an external Linux server, the remote SSH service account (default: `mai`) executes `docker ps`, `systemctl`, and `ss -tln` on the target host after SSH login.

Before enrolling a server in the Machina UI (**Enroll server** in the left drawer) or via `scripts/install-host.sh`, prepare the target server as `root`:

1. **Create the dedicated `mai` SSH user and grant `docker` socket group access:**
   ```bash
   # Create the passwordless SSH service account
   sudo useradd --create-home --shell /bin/bash --comment "mai-controller" mai

   # Add 'mai' to the 'docker' group so `docker ps` / `docker compose` work over SSH
   # without "permission denied while trying to connect to unix:///var/run/docker.sock"
   sudo usermod -aG docker mai
   ```
2. **Authorize the Machina controller's SSH public key (`~mai/.ssh/authorized_keys`):**
   ```bash
   sudo install -d -m 700 -o mai -g mai /home/mai/.ssh
   sudo sh -c 'cat /path/to/mai_controller.pub >> /home/mai/.ssh/authorized_keys'
   sudo chown mai:mai /home/mai/.ssh/authorized_keys
   sudo chmod 600 /home/mai/.ssh/authorized_keys
   ```
3. **Configure `sudo` permissions (`/etc/sudoers.d/90-mai-mai`) matching your chosen privilege mode:**
   - **Narrow privilege (`privilege: sudo`):**
     ```bash
     echo 'mai ALL=(root) NOPASSWD: /usr/bin/systemctl start docker.service, /usr/bin/systemctl stop docker.service, /usr/bin/systemctl restart docker.service, /usr/bin/systemctl status docker.service' \
       | sudo EDITOR='tee' visudo -f /etc/sudoers.d/90-mai-mai
     sudo chmod 440 /etc/sudoers.d/90-mai-mai
     ```
   - **Broad privilege (`privilege: root`, trusted admin only):**
     ```bash
     echo 'mai ALL=(ALL) NOPASSWD: ALL' | sudo EDITOR='tee' visudo -f /etc/sudoers.d/90-mai-mai
     sudo chmod 440 /etc/sudoers.d/90-mai-mai
     ```
4. **Allow SSH from the Machina controller subnet (`ufw` / `firewalld`):**
   - If Machina runs inside Docker on the same host, allow the `mai` Docker bridge subnet (e.g. `172.32.0.0/16`) to reach port `22/tcp`:
     ```bash
     sudo ufw allow from 172.32.0.0/16 to any port 22 proto tcp comment "machina -> host ssh"
     ```
   - If Machina connects from another host, allow that controller's IP/CIDR (`/32` or subnet).
5. **Optionally read the target server's SSH host-key fingerprint (for independent comparison during enrollment):**
   ```bash
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```
   The guided enrollment flow (see [`docs/operations/quickstart.md`](docs/operations/quickstart.md) §2.1) discovers the host keys the target actually presents and you select the matching candidate in the UI — there is no raw host-key field to paste a fingerprint into.

---

## Day-2 Operations & CLI Reference

All operator commands run through `python -m mai.cli` (or the scripts in `scripts/`) and never print secret values to stdout/stderr:

### Operator CLI (`mai.cli`)

```bash
# Inspect global or per-server canonical state
python -m mai.cli status --data-root /var/lib/mai
python -m mai.cli status --data-root /var/lib/mai --server-id "$SERVER_ID"

# Run integrity and permission diagnostics
python -m mai.cli doctor --data-root /var/lib/mai --env-file .env
```

Subcommands: `bootstrap`, `doctor`, `status`, `backup`, `restore`, `uninstall`, `admin write`, `admin delete`.

### Managing Optional Compose Profiles

```bash
# Start background job & schedule worker
docker compose --profile worker up -d

# Start SearXNG + Valkey web search stack
docker compose --profile search up -d

# Start Hindsight conversational memory stack
docker compose --profile memory up -d
```

### Backup, Verify & Restore ([`docs/operations/recovery.md`](docs/operations/recovery.md))

```bash
# Create a checksum-pinned backup bundle (Postgres + canonical data root, zero plaintext secrets)
bash scripts/backup-restore.sh backup \
  --data-root /var/lib/mai \
  --env-file .env \
  --output /var/backups/mai-backup.tar.gz

# Verify bundle checksums and manifest integrity
bash scripts/backup-restore.sh verify /var/backups/mai-backup.tar.gz

# Restore into a fresh instance
bash scripts/backup-restore.sh restore /var/backups/mai-backup.tar.gz \
  --data-root /var/lib/mai
```

### Key Rotation & Uninstall ([`docs/operations/uninstall.md`](docs/operations/uninstall.md))

```bash
# Preview teardown plan
sudo bash scripts/uninstall-host.sh \
  --data-root /var/lib/mai \
  --keep-data \
  --print-plan

# Deprovision MAI-owned host access/containers while preserving /var/lib/mai
sudo bash scripts/uninstall-host.sh \
  --data-root /var/lib/mai \
  --keep-data \
  --yes
```

---

## Repository Structure

| Path | Description |
|---|---|
| `backend/` | FastAPI + DBOS controller package (`mai`): API routes, auth, capability policy, SSH supervision, durable jobs, knowledge/retrieval, memory, monitoring, notifications, plugins |
| `frontend/` | SvelteKit windowed desktop workspace (`mai-web`) and Playwright test suites (`frontend/tests/`) |
| `compose.yaml` | Container topology: `postgres`, `mai-api`, `mai-web`, `mai-proxy` + profile-gated `mai-worker`, `searxng` + `valkey`, `hindsight` |
| `catalogue/` | Curated application & model recipes (`example-http.yaml`, `example-stateful.yaml`, `odysseus.yaml`) |
| `scripts/` | Operator tooling: `install-host.sh`, `backup-restore.sh`, `uninstall-host.sh`, `runtime_check.py` |
| `deploy/` | Service deployment notes (`deploy/hindsight/README.md`), SearXNG configuration (`deploy/searxng/settings.yml`), and the bundled proxy Caddyfile (`deploy/proxy/Caddyfile`) |
| `docs/operations/` | Operator runbooks: [`quickstart.md`](docs/operations/quickstart.md), [`recovery.md`](docs/operations/recovery.md), [`uninstall.md`](docs/operations/uninstall.md), [`acceptance.md`](docs/operations/acceptance.md) |
| `docs/plugins/` | Plugin, MCP & skill author guide: [`quickstart.md`](docs/plugins/quickstart.md) |
| `docs/architecture/` | Architecture contracts (`docs/architecture/contracts.md` C01–C11) and subsystem ADRs |
| `docs/assets/` | Mechanicus artwork and README screenshots; the header and screenshots are reproducible from `docs/assets/readme-src/` (`capture-header.mjs`, `capture-readme.mjs`) |
| `tests/` | Cross-cutting runtime (`tests/runtime/`) and documentation-consistency (`tests/docs/`) suites |

---

## Development & Verification

```bash
# Backend unit & integration tests
cd backend && uv sync --frozen
.venv/bin/python -m pytest ../tests/docs/test_docs_consistency.py -v

# Frontend typecheck & Playwright component/workspace tests
cd ../frontend
pnpm run check
pnpm exec playwright test
```

`tests/docs/test_docs_consistency.py` automatically verifies that every CLI subcommand/flag, script flag, Compose profile, recipe, repo path, relative link, and `/api/v1/` endpoint in `README.md`, `docs/operations/`, and `docs/plugins/` matches the live codebase.

### Regenerating the README artwork

The header and screenshots are produced from the app's own design tokens and the real product surfaces, not hand-drawn mockups:

```bash
# Header banner (HTML built on frontend/src/lib/ui/themes/mechanicus.css + fonts)
node docs/assets/readme-src/capture-header.mjs

# Screenshots (disposable Postgres + real backend + real shell; see the script header)
MAI_README_PG=postgresql://mai:***@127.0.0.1:55432/postgres \
  node docs/assets/readme-src/capture-readme.mjs
```

---

## Scope & v1 Limitations

- **Bundled loopback proxy, operator TLS in front:** `compose.yaml` ships `mai-proxy` (Caddy) bound to `127.0.0.1:8390` as the single same-origin entry point; terminate TLS with your own layer (Tailscale Serve or a reverse proxy) to expose the UI on LAN or Tailscale. No public-internet exposure is configured by default.
- **Single administrator, Linux-only:** Designed for one administrator managing Linux hosts over OpenSSH (Windows/macOS targets and multi-tenant RBAC are out of scope for v1).
- **In-app notifications:** Monitoring collectors and the notification inbox are active; external pager/messenger webhooks are not armed by default.

---

## License

MIT License — see [`LICENSE`](LICENSE).
Third-party container images (`pgvector`, `searxng`, `valkey`, `hindsight`, `ollama`) retain their respective upstream licenses.
