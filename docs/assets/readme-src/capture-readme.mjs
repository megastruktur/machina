#!/usr/bin/env node
/**
 * capture-readme.mjs — README screenshot producer on the REAL product surfaces.
 *
 * Replaces the old offline-fixture capture (which shot placeholder panels and
 * the chat's unwired error state). This boots the REAL backend + the REAL
 * SvelteKit shell in live mode, seeds a realistic session/service-map through
 * the real API + a bounded SQL transcript, and photographs the actual UI:
 *
 *   screenshot-login-sanctuary.png  — the /login route (clean, centered)
 *   screenshot-command-deck.png     — live DesktopShell: side drawer + centered
 *                                     session-first chat column (real transcript,
 *                                     turn-group tool stack) + floating windows
 *   screenshot-service-map.png      — real ApplicationInventoryPanel in the live
 *                                     shell (categorized records + runbooks +
 *                                     Available-from-catalog)
 *   screenshot-chat-hero.png        — the hero command plate (no session yet)
 *
 * Method mirrors the reviewed D08 live-shell E2E (session-virtual-history.spec):
 * disposable Postgres → alembic → DBOS bootstrap → uvicorn → `vite preview` on a
 * NON-fixture port with MAI_PREVIEW_API_PROXY (same-origin REST + WebSocket).
 *
 * Run from the repo root:  node docs/assets/readme-src/capture-readme.mjs
 * Requires: backend/.venv (uv), frontend/node_modules, a disposable Postgres at
 * MAI_README_PG (default postgresql://mai:***@127.0.0.1:55432/postgres).
 * Nothing here touches a real deployment; the DB is dropped/recreated each run.
 */
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../../..');
const FRONTEND = path.join(REPO, 'frontend');
const BACKEND = path.join(REPO, 'backend');
const ASSETS = path.resolve(HERE, '..');
const require = createRequire(path.join(FRONTEND, 'package.json'));
const { chromium } = require('@playwright/test');

const API_PORT = 8917;
const SHELL_PORT = 4175; // non-fixture port ⇒ DesktopShell boots LIVE
const API_BASE = `http://127.0.0.1:${API_PORT}`;
const SHELL_BASE = `http://127.0.0.1:${SHELL_PORT}`;
const PG_ADMIN = process.env.MAI_README_PG ?? 'postgresql://mai:***@127.0.0.1:55432/postgres';
const DB_URL = PG_ADMIN.replace(/\/[^/]*$/, '/mai_readme');
const DATA_ROOT = '/tmp/mai-readme-root';
const PASSWORD = 'readme-capture-password';
const UV = process.env.UV_BIN ?? 'uv';

const log = (...a) => console.log('[readme-capture]', ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function run(cmd, args, opts = {}) {
  const res = spawnSync(cmd, args, { encoding: 'utf-8', ...opts });
  if (res.status !== 0) {
    throw new Error(`${cmd} ${args.join(' ')} failed (${res.status}):\n${res.stderr}\n${res.stdout}`);
  }
  return res.stdout;
}

let uvicorn = null;
let preview = null;
function cleanup() {
  try { uvicorn?.kill(); } catch {}
  try { preview?.kill(); } catch {}
  // Kill any squatters on BOTH ports (a leftover preview from a prior run
  // serves a STALE build manifest ⇒ ENOENT on the new hashed assets). Match
  // the pnpm wrapper too — `vite preview` survives as a pnpm child otherwise.
  spawnSync('bash', ['-c',
    `pkill -f "uvicorn mai.api.app:create_app --factory --host 127.0.0.1 --port ${API_PORT}" 2>/dev/null || true; ` +
    `pkill -f "vite preview --port ${SHELL_PORT}" 2>/dev/null || true; ` +
    `pkill -f "vite preview --port 4173" 2>/dev/null || true`
  ]);
}
process.on('exit', cleanup);
process.on('SIGINT', () => { cleanup(); process.exit(130); });
process.on('SIGTERM', () => { cleanup(); process.exit(143); });
// Clear any squatters BEFORE we build/spawn, so the fresh preview owns the port.
cleanup();

// ---------------------------------------------------------------------------
// 1. Disposable database + migrations + DBOS bootstrap
// ---------------------------------------------------------------------------
log('resetting disposable database mai_readme');
run(UV, ['run', 'python', '-c',
  `import psycopg; c=psycopg.connect(${JSON.stringify(PG_ADMIN)},autocommit=True); ` +
  `c.execute('DROP DATABASE IF EXISTS mai_readme'); c.execute('CREATE DATABASE mai_readme')`
], { cwd: BACKEND });

log('alembic upgrade head');
run(UV, ['run', 'alembic', 'upgrade', 'head'], {
  cwd: BACKEND, env: { ...process.env, MAI_DATABASE_URL: DB_URL }
});

log('DBOS bootstrap');
run(UV, ['run', 'python', '-c',
  'import os; from mai.jobs import engine as e; e.launch_worker(os.environ["MAI_DATABASE_URL"]); e.shutdown()'
], { cwd: BACKEND, env: { ...process.env, MAI_DATABASE_URL: DB_URL } });

fs.rmSync(DATA_ROOT, { recursive: true, force: true });
fs.mkdirSync(DATA_ROOT, { recursive: true });

// ---------------------------------------------------------------------------
// 2. Spawn uvicorn + vite preview (live mode, same-origin proxy)
// ---------------------------------------------------------------------------
log('starting uvicorn on', API_PORT);
uvicorn = spawn('bash', ['-c',
  `exec ${UV} run uvicorn mai.api.app:create_app --factory --host 127.0.0.1 --port ${API_PORT} >/tmp/mai-readme-uvicorn.log 2>&1`
], {
  cwd: BACKEND,
  env: {
    ...process.env,
    MAI_DATABASE_URL: DB_URL,
    MAI_PUBLIC_ORIGINS: SHELL_BASE,
    MAI_DATA_ROOT: DATA_ROOT
  }
});

log('building frontend (production)');
run('pnpm', ['exec', 'vite', 'build'], { cwd: FRONTEND });

log('starting vite preview on', SHELL_PORT);
preview = spawn('bash', ['-c',
  `exec pnpm exec vite preview --port ${SHELL_PORT} --strictPort >/tmp/mai-readme-preview.log 2>&1`
], { cwd: FRONTEND, env: { ...process.env, MAI_PREVIEW_API_PROXY: API_BASE } });

// Readiness.
const deadline = Date.now() + 60_000;
let up = false;
while (Date.now() < deadline && !up) {
  try { up = (await fetch(`${API_BASE}/api/v1/health`)).status === 200; }
  catch { await sleep(500); }
}
if (!up) throw new Error('uvicorn never became healthy; see /tmp/mai-readme-uvicorn.log');
let shellUp = false;
const shellDeadline = Date.now() + 30_000;
while (Date.now() < shellDeadline && !shellUp) {
  try { shellUp = (await fetch(SHELL_BASE)).status === 200; }
  catch { await sleep(500); }
}
if (!shellUp) throw new Error('vite preview never came up; see /tmp/mai-readme-preview.log');
// Confirm the preview process is still alive (an ENOENT on a stale build
// manifest kills it AFTER it briefly answers) and that the same-origin /api
// proxy actually reaches the backend.
await sleep(1500);
if (preview.exitCode !== null) {
  throw new Error(`vite preview exited early (code ${preview.exitCode}); see /tmp/mai-readme-preview.log`);
}
const proxyProbe = await fetch(`${SHELL_BASE}/api/v1/health`).catch(() => null);
if (!proxyProbe || proxyProbe.status !== 200) {
  throw new Error(`same-origin /api proxy not reaching backend (status ${proxyProbe?.status}); see /tmp/mai-readme-preview.log`);
}
log('backend + shell ready (proxy verified)');

// ---------------------------------------------------------------------------
// 3. Seed through the REAL API (bootstrap, login, server, service map, session)
// ---------------------------------------------------------------------------
const browser = await chromium.launch({
  args: ['--no-sandbox', '--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests']
});

async function api(method, p, { csrf, body, cookie } = {}) {
  const headers = { origin: SHELL_BASE };
  if (csrf) headers['x-csrf-token'] = csrf;
  if (cookie) headers['cookie'] = cookie;
  if (body !== undefined) headers['content-type'] = 'application/json';
  const res = await fetch(`${API_BASE}${p}`, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body)
  });
  return res;
}

try {
  const ctx = await browser.newContext({
    viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2
  });
  const page = await ctx.newPage();

  // Bootstrap + login (origin must match the allowlist).
  let r = await api('POST', '/api/v1/auth/bootstrap', { body: { password: PASSWORD } });
  if (![201, 409].includes(r.status)) throw new Error(`bootstrap: ${r.status} ${await r.text()}`);
  r = await api('POST', '/api/v1/auth/login', { body: { password: PASSWORD } });
  if (r.status !== 204) throw new Error(`login: ${r.status} ${await r.text()}`);
  const cookies = r.headers.getSetCookie();
  let csrf = '';
  const jar = [];
  for (const c of cookies) {
    const [pair] = c.split(';');
    const name = pair.slice(0, pair.indexOf('='));
    const value = pair.slice(pair.indexOf('=') + 1);
    if (name === 'mai_csrf') csrf = value;
    jar.push(`${name}=${value}`);
    await ctx.addCookies([{ name, value, domain: '127.0.0.1', path: '/' }]);
  }
  const cookie = jar.join('; ');
  log('authenticated; csrf acquired');

  // Enroll one server.
  r = await api('POST', '/api/v1/servers', {
    csrf, cookie,
    body: {
      name: 'forge-prime',
      prompt_revision: 1,
      environment: 'linux_ssh',
      ssh: {
        host: 'forge-prime.internal', port: 22, user: 'ops',
        host_key: 'SHA256:6VhOyUC6fvXW0aQ9WNe7Kx3nZZf+zxgupEWLoZXoTMU',
        credential_ref: 'MAI_SSH_KEY_FORGE'
      },
      privilege: 'sudo',
      capabilities: ['app.adopt', 'app.list_adopted', 'app.discover', 'host.shell', 'host.logs', 'session.read', 'session.delegate', 'host.file_read']
    }
  });
  if (r.status !== 201) throw new Error(`servers POST: ${r.status} ${await r.text()}`);
  const serverId = (await r.json()).server_id;
  log('enrolled server', serverId);

  // Adopt a few categorized services (manual origin ⇒ no live SSH needed).
  const services = [
    { name: 'backup-verify', category: 'system', description: 'Nightly backup + integrity verification (restic + checksum sweep).',
      instructions: { install: 'systemctl enable --now backup-verify.timer', update: 'restic self-update && systemctl restart backup-verify', uninstall: 'systemctl disable --now backup-verify.timer', backup: 'restic backup /srv/data', verify: 'restic check --read-data' },
      custom: 'Repo: /mnt/nas/restic · timer: 02:00 UTC · alerts → #ops' },
    { name: 'litellm-gateway', category: 'ai', description: 'LiteLLM proxy routing model profiles to local + upstream providers.',
      instructions: { install: 'docker compose up -d', update: 'docker compose pull && docker compose up -d', uninstall: 'docker compose down', backup: 'pg_dump litellm > /var/backups/litellm.sql', verify: 'curl -fsS localhost:4000/health' } },
    { name: 'forgejo', category: 'dev', description: 'Self-hosted Git forge + CI runners.',
      instructions: { install: 'docker compose up -d', update: 'docker compose pull && docker compose up -d', uninstall: 'docker compose down', backup: 'forgejo dump', verify: 'curl -fsS localhost:3000/api/healthz' } },
    { name: 'caddy-proxy', category: 'infrastructure', description: 'TLS-terminating reverse proxy for the LAN origins.',
      instructions: { install: 'systemctl enable --now caddy', update: 'apt upgrade caddy && systemctl reload caddy', uninstall: 'systemctl disable --now caddy', backup: 'cp -a /etc/caddy /var/backups/caddy', verify: 'caddy validate --config /etc/caddy/Caddyfile' } }
  ];
  const adopted = [];
  for (const s of services) {
    r = await api('POST', `/api/v1/servers/${serverId}/applications`, {
      csrf, cookie,
      body: { name: s.name, origin: 'manual', category: s.category, description: s.description,
              instructions: s.instructions, custom_instructions: s.custom ?? null, installed: true }
    });
    if (r.status !== 201) throw new Error(`adopt ${s.name}: ${r.status} ${await r.text()}`);
    adopted.push(await r.json());
  }
  const backupService = adopted.find((a) => a.application.name === 'backup-verify');
  const serviceId = backupService.application.application_id;
  log('adopted', adopted.length, 'services; backup-verify =', serviceId);

  // A couple of global catalog runbooks (Available-from-catalog section).
  for (const rb of [
    { name: 'valheim-server', category: 'games', description: 'Valheim dedicated server (SteamCMD).',
      instructions: { install: 'steamcmd +app_update 896660 validate', update: 'steamcmd +app_update 896660 validate', verify: 'curl -fsS localhost:2457' } },
    { name: 'searxng', category: 'infrastructure', description: 'Privacy-respecting metasearch engine.',
      instructions: { install: 'docker compose up -d', update: 'docker compose pull && docker compose up -d', verify: 'curl -fsS localhost:8080/healthz' } }
  ]) {
    r = await api('POST', '/api/v1/applications/catalog', { csrf, cookie, body: rb });
    if (r.status !== 200) throw new Error(`catalog ${rb.name}: ${r.status} ${await r.text()}`);
  }
  log('seeded global catalog runbooks');

  // Create the session (service-scoped) through the real API, then seed its
  // transcript + runs with the bounded SQL script.
  r = await api('POST', '/api/v1/agent-sessions', {
    csrf, cookie,
    body: { server_id: serverId, source: 'interactive', service_id: serviceId,
            client_request_id: crypto.randomUUID(), title: 'backup-verify backup verification' }
  });
  if (r.status !== 201) throw new Error(`session create: ${r.status} ${await r.text()}`);
  const sessionId = (await r.json()).session_id;
  log('created session', sessionId);

  const seedOut = run(UV, ['run', 'python', path.join(HERE, 'seed_readme.py'),
    serverId, sessionId, serviceId, 'backup-verify'
  ], { cwd: BACKEND, env: { ...process.env, MAI_DATABASE_URL: DB_URL } });
  log(seedOut.trim());

  // ---------------------------------------------------------------------
  // 4. Capture the REAL surfaces
  // ---------------------------------------------------------------------
  const shot = async (name) => {
    await page.screenshot({ path: path.join(ASSETS, name) });
    log('wrote', name);
  };

  // 4a. Command deck — the live shell, chat as the center stage (the current
  //     CCC/MCR design): side drawer + centered session-first chat column with
  //     the real seeded transcript + line-grid canvas + taskbar. No floating
  //     windows: at this width they would overlap the 880px column and trigger
  //     the auto-recede dimming, so the default chat-centered workspace IS the
  //     honest hero surface.
  await page.goto(`${SHELL_BASE}/`, { waitUntil: 'networkidle' });
  await page.getByTestId('side-drawer').waitFor({ state: 'visible', timeout: 20_000 });
  try {
    await page.getByTestId('drawer-history-item').first().waitFor({ state: 'visible', timeout: 30_000 });
  } catch {
    const diag = await page.evaluate(async () => ({
      url: location.href,
      status: document.querySelector('[data-testid="desktop-status"]')?.textContent ?? null,
      cookie: document.cookie,
      testids: [...document.querySelectorAll('[data-testid]')].slice(0, 40).map((n) => n.getAttribute('data-testid')),
      serversProbe: await fetch('/api/v1/servers', { credentials: 'include' }).then(async (r) => `${r.status} ${(await r.text()).slice(0, 200)}`).catch(String),
      sessionsProbe: await fetch('/api/v1/agent-sessions?limit=10', { credentials: 'include' }).then(async (r) => `${r.status} ${(await r.text()).slice(0, 300)}`).catch(String)
    }));
    log('drawer-history not visible; diag:', JSON.stringify(diag, null, 1));
    await page.screenshot({ path: '/tmp/mai-readme-shell-diag.png' });
    throw new Error('drawer-history-item did not render');
  }
  await page.getByTestId('drawer-history-item').first().click();
  await page.getByTestId('session-chat-panel').waitFor({ state: 'visible', timeout: 20_000 });
  await page.getByTestId('session-history-viewport').waitFor({ state: 'visible' });
  await page.getByTestId('turn-group').first().waitFor({ state: 'visible', timeout: 20_000 });
  // Expand one tool-call disclosure so the telemetry rail shows real output.
  const firstTool = page.getByTestId('entry-tool-result').first();
  if (await firstTool.count()) { await firstTool.locator('summary').click(); }
  await page.waitForTimeout(700);
  const deckGeom = await page.evaluate(() => {
    const r = (sel) => { const e = document.querySelector(sel); if (!e) return null; const b = e.getBoundingClientRect(); return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) }; };
    return { viewport: { w: innerWidth, h: innerHeight }, drawer: r('[data-testid="side-drawer"]'), stage: r('.mai-desktop-shell__stage'), desktop: r('[data-testid="desktop"]'), dock: r('[data-testid="bottom-chat-dock"]'), chat: r('[data-testid="session-chat-panel"]') };
  });
  log('command-deck geometry:', JSON.stringify(deckGeom));
  await shot('screenshot-command-deck.png');

  // 4b. Service map — open Applications and MAXIMIZE it for a full, uncropped
  //     view of the categorized inventory + runbooks + Available-from-catalog.
  await page.getByTestId('open-applications').click();
  const appsFrame = page.locator('.mai-window-frame[data-panel-kind="applications"]');
  await appsFrame.waitFor({ state: 'visible', timeout: 20_000 });
  await page.getByTestId('apps-root').waitFor({ state: 'visible' });
  await page.getByTestId('inventory-row').first().waitFor({ state: 'visible', timeout: 20_000 });
  await appsFrame.getByRole('button', { name: 'Maximize' }).click();
  await page.waitForTimeout(600);
  await page.getByTestId('available-runbooks-section').waitFor({ state: 'visible' });
  // Open one operational runbook so the five instruction slots are visible.
  const runbook = page.getByTestId('service-instructions').first();
  if (await runbook.count()) { await runbook.locator('summary').click(); }
  await page.waitForTimeout(500);
  await shot('screenshot-service-map.png');
  await appsFrame.getByRole('button', { name: 'Restore' }).click();
  await page.waitForTimeout(400);
  // Close the window so the hero shot is the clean chat-centered workspace.
  await appsFrame.getByRole('button', { name: 'Close' }).click();
  await page.waitForTimeout(400);

  // 4c. Chat hero — start a new chat (no session) to show the command plate.
  await page.getByTestId('drawer-new-chat').click();
  await page.getByTestId('chat-hero-directive').waitFor({ state: 'visible', timeout: 20_000 });
  await page.getByTestId('chat-scope-strip').waitFor({ state: 'visible' });
  await page.waitForTimeout(600);
  await shot('screenshot-chat-hero.png');

  // 4e. Login sanctuary (clean signed-out form). Fresh context ⇒ no session
  //     cookie. Diagnose the rendered state if the form does not appear.
  const loginCtx = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 });
  const loginPage = await loginCtx.newPage();
  await loginPage.goto(`${SHELL_BASE}/login`, { waitUntil: 'networkidle' });
  try {
    await loginPage.getByTestId('login-form').waitFor({ state: 'visible', timeout: 15_000 });
  } catch {
    const diag = await loginPage.evaluate(() => ({
      url: location.href,
      testids: [...document.querySelectorAll('[data-testid]')].map((n) => n.getAttribute('data-testid')),
      bodyText: document.body.innerText.slice(0, 400)
    }));
    log('login-form not visible; diag:', JSON.stringify(diag));
    await loginPage.screenshot({ path: '/tmp/mai-readme-login-diag.png' });
    throw new Error('login-form did not render');
  }
  await loginPage.getByTestId('login-emblem').waitFor({ state: 'visible' });
  await loginPage.waitForTimeout(400);
  await loginPage.screenshot({ path: path.join(ASSETS, 'screenshot-login-sanctuary.png') });
  log('wrote screenshot-login-sanctuary.png');
  await loginCtx.close();

  log('all captures complete');
  await ctx.close();
} finally {
  await browser.close();
  cleanup();
}
