# Machina production deployment notes (megaserver)

M-1 packaging (card t_0949b888). M-2 (live bring-up) follows these notes.

## Topology

```
Tailscale serve (svc:machina, TLS)
  → 127.0.0.1:8390 (mai-proxy, Caddy 2.10.2-alpine, plain HTTP :80)
      /api/* → mai-api:8000   (path preserved; FastAPI mounts /api/v1)
      /*     → mai-web:3000   (SvelteKit SSR, same-origin API client)
      /healthz → 200 (proxy-local liveness)
```

- `mai` network pinned to **172.32.0.0/16** (gateway 172.32.0.1);
  `MAI_TRUSTED_PROXIES=172.32.0.0/16` pins the exact CIDR so only the
  mai-proxy hop's `X-Forwarded-*` headers are honored (auth ADR §6.3).
- `mai-api`, `mai-worker`, `hindsight` also join external **net-infra** and
  reach the LiteLLM proxy by container name: `http://litellm:4000/v1`.
- LLM default model: `z-ai-coding/glm-5.3-flash`; embeddings
  `text-embedding-3-small` (1536-dim, verified live through the machina key).

## Secrets (1Password vault "Secrets")

| Item | Feeds |
|---|---|
| Machina Postgres Password | `MAI_POSTGRES_PASSWORD` (compose .env / Komodo var) |
| Machina LiteLLM Key | LiteLLM virtual key `machina` (unrestricted); `MAI_GATEWAY_KEY` in the EnvResolver file; hindsight LLM+embeddings keys |
| Machina Admin Password | `MAI_ADMIN_PASSWORD` (first-admin bootstrap; remove after bootstrap) |
| Machina SearXNG Secret | `SEARXNG_SECRET` (search profile entrypoint guard) |

- Compose `.env` at the repo dir is mode 600, gitignored (Komodo writes its
  own copy in `/etc/komodo/stacks/machina/.env` from Stack env + Variables).
- Application credential file (EnvResolver, C03 rule 6):
  `/home/megastruktur/data/machina/mai-credentials.env` — mode 600, no
  symlink, bind-mounted RO into mai-api/mai-worker at
  `/run/secrets/mai-credentials.env`, referenced via `MAI_ENV_FILE`.
  Contains `MAI_GATEWAY_KEY=<virtual key>`.
- Komodo Variables (secret): `MACHINA_POSTGRES_PASSWORD`,
  `MACHINA_LITELLM_KEY`, `MACHINA_ADMIN_PASSWORD`, `MACHINA_SEARXNG_SECRET`.

## Source of truth / GitOps

- Upstream: `git@github.com:megastruktur/machina.git` (branch `master`).
- Compose carries NO top-level `name:` — the project name is `machina`
  everywhere (Komodo `project_name`, repo dir default, backup-restore.sh
  default), per the verified stack convention.
- **Forgejo mirror**: `http://192.168.3.23:3300/megaserver/machina`
  (private; `komodo-ro` has read). Komodo clones ONLY from Forgejo.
- Refresh mirror after GitHub pushes:
  ```bash
  git clone --mirror git@github.com:megastruktur/machina.git /tmp/mm.git
  cd /tmp/mm.git && git push --mirror http://<token>@127.0.0.1:3300/megaserver/machina.git
  ```
  (mint a temporary `write:repository` token, revoke after; see
  devops skill komodo-git-linked-stack-adoption).
- Komodo stack `machina`: git-linked (not files_on_host), branch `master`,
  `file_paths: [compose.yaml]`, `project_name: machina`, `run_build: true`
  (images `mai-backend`/`mai-web` are built from the repo Dockerfiles),
  `COMPOSE_PROFILES=worker,search,memory` in the Stack environment,
  `ignore_services: [hindsight-db-init]` (one-shot exits 0).

## M-2 bring-up checklist

1. Komodo DeployStack `machina` (builds both images first time — slow).
2. `sudo tailscale serve --service=svc:machina --https=443 http://127.0.0.1:8390`.
3. Bootstrap admin: `POST /api/v1/auth/bootstrap` with `MAI_ADMIN_PASSWORD`
   (then remove `MAI_ADMIN_PASSWORD` from .env/Komodo env and redeploy).
4. Migrations: no auto-migration service exists — run the image's migration
   harness once (`docker compose exec` / `docker run --rm mai-backend …`
   with `MAI_DATABASE_URL`; see backend/migrations + alembic.ini in image at /app).
5. Model connection (UI Model Settings or /api/v1/models/*):
   base_url `http://litellm:4000` (no trailing /v1 — backend appends it for
   embeddings; chat uses the same connection), credential_ref
   `MAI_GATEWAY_KEY` (resolves from the EnvResolver file), profile model
   `z-ai-coding/glm-5.3-flash`, set as default selection.
6. Enroll megaserver (`scripts/install-host.sh`), smoke all 10 panels,
   backup/restore drill (`scripts/backup-restore.sh`).
