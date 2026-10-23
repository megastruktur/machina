# Runbook Catalog vs. Application Adoption Architecture

Machina (M.A.I.) separates **server-agnostic software recipes** from **per-server service deployments** using a two-tier model:

1. **Tier 1 — Global Runbook Catalog (`catalog.json`):** What an application is and how it is operated in general (`install`, `update`, `uninstall`, `backup`, `verify`).
2. **Tier 2 — Per-Server Adoption & Service Map (`servers/<server_id>.json` + `adopted_applications`):** How that application is deployed on a specific server (`origin`, selectors, `installed` verification status, and server-specific `custom_instructions`).

---

## 1. Tier 1: Global Runbook Catalog

### Storage & Identity
- **On-disk path:** `<data_root>/service_map/catalog.json` (managed by `ServiceMapStore` in `backend/src/mai/applications/service_map.py`).
- **Scope:** Fleet-wide (`global`). Does not require a `server_id` and can be read or mutated from General Consult (`00000000-0000-0000-0000-000000000000`) or any server-scoped conversation.
- **Identity (`runbook_id`):** Deterministic UUID5 derived from the normalized service name (`uuid5(SERVICE_ID_NAMESPACE, f"catalog:{name.lower()}")`), or an explicit UUID when updating an existing record.

### Schema (`CatalogRunbook`)
| Field | Type | Description |
|---|---|---|
| `runbook_id` | `UUID` | Canonical catalog recipe identity. |
| `name` | `str` (1–200 chars) | Human-readable application name (e.g., `Immich`, `Forgejo`, `Prometheus`). |
| `category` | `str` | Taxonomy category `machine_id` (`games`, `ai`, `agent`, `system`, `dev`, `infrastructure`, `media`, `observability`, or custom slug). |
| `description` | `str` (0–2000 chars) | Server-agnostic summary of the software and its architecture. |
| `instructions` | `ServiceInstructions` | Five operational procedure slots (0–8000 chars each): `install`, `update`, `uninstall`, `backup`, `verify`. |
| `revision` | `int` ($\ge 1$) | Monotonic revision counter bumped on each upsert. |
| `updated_at` | `str` (ISO-8601 UTC) | Timestamp of last modification. |

> **Separation rule:** The global catalog never stores host paths, ports, `custom_instructions`, or `installed` state. Deleting a catalog recipe removes it from `catalog.json` only and never touches any server's adopted services.

### Surfaces
- **Agent Tools (`@internal_api_tool` in `backend/src/mai/capabilities/internal_api.py`):**
  - `app.list_catalog` (`read`): Paginated (`limit`, `offset`) search/filter (`query`, `category`) over the global Runbook Catalog and taxonomy categories.
  - `app.upsert_catalog_runbook` (`mutate`): Create or update a global catalog recipe (`name`, `category`, `description`, `instructions`, optional `runbook_id`, optional `upsert_categories`).
  - `app.delete_catalog_runbook` (`mutate`): Delete a recipe from `catalog.json` by `runbook_id`.
- **HTTP Endpoints (`backend/src/mai/api/applications.py`):**
  - `GET /api/v1/applications/catalog`
  - `POST /api/v1/applications/catalog`
  - `DELETE /api/v1/applications/catalog/{runbook_id}`
- **Desktop UI:** Standalone **Runbook Catalog** window (`RunbookCatalogPanel.svelte`) and the **"Available from Catalog"** section (`available_runbooks`) inside each server's **Applications** window (`ApplicationInventoryPanel.svelte`).

---

## 2. Tier 2: Per-Server Adoption & Service Map

### Storage & Identity
When an application is assigned to a specific server (`server_id`), Machina persists:
1. **Canonical Adoption Manifest & SQL Projection** (for `origin="compose"` and `origin="systemd_unit"`):
   - `<data_root>/servers/<server_id>/applications/<application_id>/manifest.yaml` (+ `.meta.json`)
   - Postgres `adopted_applications` row (`AdoptionService` in `backend/src/mai/applications/adopt.py`)
   - Marks the workload as `managed=true` in `app.discover`.
2. **Per-Server Service Map Entry (`<data_root>/service_map/servers/<server_id>.json`):**
   - Stores the server's operational metadata (`ServiceMapEntry`), including **`custom_instructions`** and **`installed`**.
3. **Canonical Application Identity Document:**
   - Generated in the `docs` namespace (`record_doc_id(server_id, application_id)`) via `ApplicationDocService.record_adoption` (or `_render_manual_record` for `origin="manual"`).

### Schema (`ServiceMapEntry` + Adoption Selectors)
| Field | Type | Description |
|---|---|---|
| `application_id` | `UUID` | Per-server application UUID (auto-resolved from `(server_id, name)` when omitted in `app.adopt`). |
| `server_id` | `UUID` | Target server UUID. |
| `name` | `str` | Service display name on this server. |
| `origin` | `"compose" \| "systemd_unit" \| "manual"` | Runtime supervisor type. Standalone containers are refused by C09 rule 1. |
| `compose_project` / `working_dir` | `str \| None` | Required when `origin="compose"`. |
| `unit_name` | `str \| None` | Required when `origin="systemd_unit"` (must end in `.service`, `.socket`, or `.timer`). |
| `installed` | `bool` | `true` when verified installed/present on the server; `false` when staged from the catalog or uninstalled. |
| `custom_instructions` | `str` (0–8000 chars) | **Server-specific operational instructions:** host paths, ports, bind addresses, `.env` locations, reverse-proxy domains, systemd overrides, or backup destinations on this server. |
| `category` | `str` | Inherited from the matching global Catalog Runbook when omitted, or set explicitly. |
| `description` | `str` | Inherited from the matching global Catalog Runbook when omitted, or set explicitly. |
| `instructions` | `ServiceInstructions` | Inherited slot-by-slot (`install`, `update`, `uninstall`, `backup`, `verify`) from the matching global Catalog Runbook when omitted, or overridden per server. |

### Automatic Catalog Inheritance on Adopt
When `app.adopt` (or `POST /api/v1/servers/{server_id}/applications`) is called for a service whose `name` (case-insensitive) or `application_id` matches a global `CatalogRunbook`:
- Any omitted `category`, `description`, or `instructions` (`install`, `update`, `uninstall`, `backup`, `verify`) are **automatically inherited** from the global catalog recipe.
- If the service is already adopted on `server_id`, calling `app.adopt` again without `expected_revision` or `application_id` automatically resolves the live revision and updates `custom_instructions`, `installed`, or selectors in place without wiping inherited catalog instructions.

### How `@service` Chat Tagging Uses Both Tiers
When an operator tags `@service` in Chat (`service_tags`), `format_service_map_section()` (`backend/src/mai/agents/context.py`) injects both tiers together into the agent prompt:
1. Identity (`name`, `category`, `origin`, `application_id`)
2. Global `description`
3. **`Server custom instructions:`** (`custom_instructions` for this specific host)
4. Operational runbook slots (`install`, `update`, `uninstall`, `backup`, `verify`)

---

## 3. End-to-End Agent Workflow

### Step 1: Add Application to Global Runbook Catalog
> *"Add <App> to the catalog (research docs + fill in the schema)."*
1. The agent searches upstream documentation via `web.search` and reads authoritative install/upgrade/backup docs via `web.read`.
2. The agent calls `app.upsert_catalog_runbook` with:
   - `name`: Canonical software name
   - `category`: Taxonomy category slug
   - `description`: General architecture and purpose
   - `instructions`: `{install, update, uninstall, backup, verify}`

### Step 2: Adopt Application onto a Target Server
> *"Adopt <App> on this server — check if installed and fill in server-specific instructions."*
1. The agent inspects the target server via `app.discover` (paginated) and/or `host.shell` / `service.http` to verify whether the service is installed and locate its compose directory, systemd unit, ports, config files, and data paths.
2. The agent calls `app.adopt` with:
   - `name`: `<App>`
   - `origin`: `"compose"`, `"systemd_unit"`, or `"manual"`
   - `compose_project` + `working_dir` (or `unit_name`)
   - `installed`: `true` (verified present on host) or `false` (staged from catalog, not yet installed)
   - `custom_instructions`: Host-specific ports, paths, `.env` files, domains, and local operational notes
   - (`category`, `description`, and `instructions` are inherited automatically from the catalog recipe created in Step 1).
