---
name: readme-refresh
description: Regenerate the Machina README header banner and product screenshots from the app's own design tokens and REAL live surfaces (not mockups). Load when updating README.md artwork, re-shooting screenshots after a UI redesign, or fixing the "crooked header / cropped screenshots" class of problem. Covers the token-derived HTML header, the disposable-backend live-capture harness, the seed recipe, and every pitfall hit while building it.
tags: [machina, readme, screenshots, documentation, playwright, capture, design-tokens]
---

# Machina README Refresh

Reproducible procedure for the README's visual assets. The header and all
screenshots are **generated from the real product**, never hand-drawn:

- **Header** — HTML built on the app's own theme (`mechanicus.css`) + self-hosted
  fonts, rendered headless. Auto-sized CSS frames cannot drift.
- **Screenshots** — a disposable Postgres + real `mai-api` + real SvelteKit shell
  in live mode, seeded through the real API, photographed with Playwright.

Everything lives in `docs/assets/readme-src/`. Output lands in `docs/assets/`.

## When to load this skill

- The README header looks "crooked" / frames drifted / text overflows.
- Screenshots are stale after a UI redesign, look cropped, or show placeholder
  panels / error states instead of the product.
- You need to add a new product screenshot or refresh existing ones.

## The artifacts (source of truth)

| File | Role |
|---|---|
| `docs/assets/readme-src/header.html` | Header source. Imports `mechanicus.css` (copied verbatim from `frontend/src/lib/ui/themes/`) + 5 woff2 fonts. All frames are auto-sized CSS boxes. |
| `docs/assets/readme-src/capture-header.mjs` | Renders `header.html` → `machina-hero-banner.png` @2x. Has an **overflow guard** that fails the capture if any content escapes the plate or the inner 12px frame. |
| `docs/assets/readme-src/capture-readme.mjs` | The live-capture harness (see below). |
| `docs/assets/readme-src/seed_readme.py` | Seeds a realistic transcript + runs into the disposable DB (runs inside `backend/` via `uv run python`). |
| `docs/assets/readme-src/mechanicus.css` | Verbatim copy of the app theme. **Re-copy it if the theme changes.** |
| `docs/assets/readme-src/fonts/*.woff2` | Cinzel 700, Rajdhani 500/600/700, JetBrains Mono 400 — copied from `frontend/node_modules/@fontsource/*/files/`. |

Outputs: `machina-hero-banner.png`, `screenshot-command-deck.png`,
`screenshot-service-map.png`, `screenshot-chat-hero.png`,
`screenshot-login-sanctuary.png`.

## Reproduce (from repo root)

```bash
export PATH="$HOME/.local/bin:$PATH"   # uv, pnpm, node live here

# 1. Header (no backend needed)
node docs/assets/readme-src/capture-header.mjs

# 2. Screenshots — needs a disposable Postgres. The repo's mai-pg-fresh container
#    (pgvector, port 55432) works. Read its password from the container env, NEVER
#    hardcode it:
export MAI_README_PG="$(docker exec mai-pg-fresh printenv POSTGRES_PASSWORD \
  | xargs -I{} echo 'postgresql://mai:{}@127.0.0.1:55432/postgres')"
node docs/assets/readme-src/capture-readme.mjs
```

The capture script drops/recreates `mai_readme` each run — it never touches a
real deployment. It does NOT deploy anything.

## How the live-capture harness works

Mirrors the reviewed D08 live-shell E2E (`frontend/tests/session-virtual-history.spec.ts`):

1. **Disposable DB** — `DROP/CREATE mai_readme` on `MAI_README_PG`, then
   `uv run alembic upgrade head`, then DBOS bootstrap
   (`mai.jobs.engine.launch_worker(url); shutdown()`).
2. **Backend** — spawn `uv run uvicorn mai.api.app:create_app --factory` on
   `127.0.0.1:8917` with `MAI_DATABASE_URL`, `MAI_DATA_ROOT=/tmp/mai-readme-root`,
   and `MAI_PUBLIC_ORIGINS=http://127.0.0.1:4175`.
3. **Frontend** — `pnpm exec vite build`, then `vite preview --port 4175
   --strictPort` with `MAI_PREVIEW_API_PROXY=http://127.0.0.1:8917`. Port 4175 is
   a **non-fixture port**, so `DesktopShell` boots LIVE (its offline-fixture host
   is exactly `127.0.0.1:4173`). The preview proxies same-origin `/api` (REST +
   WebSocket) to the backend.
4. **Seed through the REAL API** — bootstrap + login (capture the `Set-Cookie`
   jar), enroll a server, adopt categorized services (`origin: "manual"` ⇒ no
   live SSH needed), add global catalog runbooks, create a service-scoped
   session. Then `seed_readme.py` inserts the transcript + 2 runs by SQL.
5. **Photograph the real shell** — drive the UI by `data-testid`, screenshot
   @2x (1600×1000 → 3200×2000).

### Seed contract (what makes the transcript render)

`session_entries.payload` must satisfy the D01 `SessionEntryPayload` union
(`backend/src/mai/contracts/sessions.py`, `extra="forbid"`). Key kinds:
`user_message`, `assistant_message` (needs non-empty text OR tool_calls;
`model_profile` required), `tool_result` (needs `call_id`, `status:"executed"`,
`execution_status`, `domain_outcome`, `effect_certainty`, `payload_hash`),
`delegation`, `run_state`. Turn groups form when a `tool_result.call_id` matches
the lead assistant's `tool_calls[].call_id` **in the same run** — so seed real
`session_runs` rows and reference their `run_id`. The channel snapshot serves the
newest ≤30 entries on a cursor-less hello, so a ~13-entry transcript renders
fully. `sessions.status='idle'` + a `title` make the drawer row look real.

## Pitfalls (all hit while building this — do not rediscover)

1. **Node `fetch` keeps no cookies.** After login, collect `Set-Cookie` into a
   manual `cookie` header and pass it to every mutating API call, AND mirror the
   cookies into the Playwright context (`ctx.addCookies`) so the browser shell
   authenticates. Missing this ⇒ `401 unauthenticated` on the first server POST.
2. **Orphaned `vite preview` ⇒ ENOENT.** A preview left over from a prior run
   holds the port with a STALE build manifest; the new `vite build` regenerates
   hashed asset names and the old preview 500s/ENOENTs. Cleanup must `pkill -f`
   BOTH the uvicorn and the `vite preview --port …` (and the pnpm wrapper) BEFORE
   building/spawning, and verify `preview.exitCode === null` + a same-origin
   `/api/v1/health` probe AFTER startup. A preview can answer briefly then die.
3. **Floating windows overlap the chat column.** The centered chat column is
   `min(880px, 72%)`; panel windows have `min_geometry` 480–520px wide, so at
   1600px they CANNOT sit beside it without overlapping — and overlap triggers
   the auto-recede dimming (`opacity .42`). The honest hero surface is the
   **chat-centered workspace with NO floating windows**. Shoot the service map
   as a **maximized** Applications window instead. Do not try to tile windows
   beside the column at this width.
4. **PG password is masked in tool output.** Never hardcode it; read it from the
   container env (`docker exec mai-pg-fresh printenv POSTGRES_PASSWORD`) into
   `MAI_README_PG`. The literal `postgresql://mai:***@…` default in the script is
   a placeholder and will fail auth.
5. **`uv`/`pnpm`/`node` are in `~/.local/bin`**, not on the default PATH in this
   environment. Export it once per shell.
6. **The old capture spec was a regression trap.** The previous
   `frontend/tests/capture-readme-screenshots.spec.ts` was NOT gated and
   re-overwrote the good screenshots with offline-fixture placeholder shots on
   every `pnpm exec playwright test`. It has been **deleted**; the harness now
   lives in `docs/assets/readme-src/`. Do not reintroduce an ungated spec that
   writes `docs/assets/*.png`.
7. **Stale-write protection.** `write_file` refuses to overwrite a file you
   haven't fully read this session. `read_file` the whole target first.
8. **`psql`/`pgrep` heuristics.** A terminal command containing `pgrep -af
   "vite preview|uvicorn"` can be misflagged as "starting a server". Read logs
   with `read_file` (`/tmp/mai-readme-uvicorn.log`, `/tmp/mai-readme-preview.log`)
   instead of grepping process lists.

## Header design rules (why it can't drift again)

- Palette/type come from `mechanicus.css` tokens (`--mai-color-*`, `--mai-font-*`)
  — the same values the app uses. Re-copy the CSS if the theme changes.
- Every frame is a CSS box that **auto-sizes to its content** (flex/inline-flex),
  never a fixed-width rect sized for one font. The old SVG used fixed rects +
  fonts unavailable to a standalone SVG ⇒ fallback fonts changed every width ⇒
  text overran frames and ran off the canvas.
- `capture-header.mjs` asserts no descendant escapes the plate OR the inner 12px
  frame; any overflow fails the capture loudly. Keep that guard.
- Check contrast: all text should meet WCAG AA (≥4.5:1). The doctrine-loop footer
  must use `--mai-color-brass` (not `brass-dim`, which is ~3.9:1).

## Verification before committing

```bash
# Docs-consistency gate — README must be clean. NOTE: there is a PRE-EXISTING
# failure in docs/architecture/contracts.md (the WebSocket /channel route is not
# matched by the HTTP-route checker). It is OUT OF SCOPE for a README refresh;
# confirm README itself is clean, don't "fix" contracts.md as a side effect.
cd backend && uv run pytest ../tests/docs/test_docs_consistency.py -q

# Frontend typecheck (deleting/adding specs must not break it)
cd ../frontend && pnpm run check
```

To prove README alone is clean across every checker, load the module and run
`_check_endpoints`, `_check_cli`, `_check_scripts`+`_check_compose_profiles`,
`_check_repo_paths`+`_check_doc_links` over `README.md` (the endpoint test
iterates ALL docs and fails on contracts.md first, masking README's status).

Quantitative screenshot check (no vision needed): a Pillow edge-density + content
-bbox script confirms screenshots aren't cropped and the transcript actually
rendered (non-bg density in the dock region should be >5%). The login shot should
be centered with large margins; the service map should fill the frame.

## After a UI redesign

1. Re-copy `mechanicus.css` + fonts if the theme changed.
2. Re-run both capture scripts.
3. If panel testids changed, update the selectors in `capture-readme.mjs`
   (they are all `data-testid`-based; grep the panel `.svelte` for the current
   ids). Key ids: `side-drawer`, `drawer-history-item`, `drawer-new-chat`,
   `session-chat-panel`, `session-history-viewport`, `turn-group`,
   `entry-tool-result`, `open-applications`, `apps-root`, `inventory-row`,
   `available-runbooks-section`, `service-instructions`, `chat-hero-directive`,
   `chat-scope-strip`, `login-form`, `login-emblem`. Window maximize/restore/close
   are `getByRole('button', { name: 'Maximize' | 'Restore' | 'Close' })`.
4. Update README prose to match the new layout (the chat went from a "bottom
   dock" to a centered session-first column; capability `chat.read_session` was
   renamed `session.read`; `compose.yaml` now ships a bundled `mai-proxy` on
   `127.0.0.1:8390`, so "no bundled proxy / no published ports" is stale).
5. Verify, then commit. **Do not deploy** — this is documentation only.
