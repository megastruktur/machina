import { test, expect, type BrowserContext, type Page } from '@playwright/test';

/**
 * nbi-acceptance — FULL-STACK LIVE proof of the unified notification inbox
 * (campaign `notification-inbox`, tier-2 gate; contracts
 * docs/architecture/notification-inbox.md). NO synthetic API mocks.
 *
 * Real stack under test, started by this fixture and torn down with proof:
 *  - REAL disposable PostgreSQL 18 (own container `pgvector/pgvector:0.8.1-pg18`,
 *    uniquely named DB), migrated with the repository's alembic chain before
 *    uvicorn starts;
 *  - one REAL enrolled server profile, written through the canonical
 *    production pipeline (`ProfileProjection.apply_upsert` over
 *    `ProfileFileStore` + the projection journal) so minting a SERVER-scoped
 *    ingest key and the «View in Inbox →» hand-off exercise real profiles;
 *  - REAL uvicorn ASGI (`mai.api.app:create_app --factory`) on a loopback
 *    ephemeral port from a fixture-only environment (MAI_DATABASE_URL /
 *    MAI_DATA_ROOT / MAI_ENV_FILE / MAI_PUBLIC_ORIGINS);
 *  - Production-build Svelte UI served by the Playwright webServer (vite
 *    build + vite preview, port 4173) in real Chromium.
 *
 * The ONLY route interception is the same-origin `/api/**` relay to the real
 * uvicorn (`route.fetch` → `route.fulfill({ response })`), exactly the
 * pattern approved for server-enrollment-live.spec.ts: real cookies, real
 * CSRF, real Origin, real SSE streams, real backend bodies. No
 * `route.fulfill` synthetic API response exists in this file.
 *
 * Live journey (the five acceptance outcomes of the frozen acceptance):
 *  1. bootstrap + login through the real boundary and the real UI;
 *  2. Notifications window → Sources tab → GLOBAL key mint → the one-time
 *     plaintext is captured from the modal (`issued-key-value`), copied, and
 *     proven ABSENT from DOM and localStorage/sessionStorage after close;
 *  3. a REAL external `curl` subprocess (cookieless, `Authorization: Bearer`)
 *     publishes through `POST /api/v1/notifications/ingest`; the open Inbox
 *     and the rail unread badge update LIVE (SSE wakeup → durable refetch,
 *     within one coarse tick — never a manual reload); the replayed
 *     `dedup_key` is DEDUPED, never a second row;
 *  4. canonical filters compose (scope=Global, unread, origin=external,
 *     search `q` over the frozen field set), the body expands, read→unread→
 *     read toggles receipt-driven, and mark-all-read reports the route's own
 *     rowcount while the badge honestly drops to hidden;
 *  5. a SERVER-scoped key publishes under its server (source defaults to the
 *     key label); global-key-with-server_id and scoped-key-with-foreign_id
 *     answer the byte-identical 422 `invalid_scope`; the OverviewPanel
 *     «View in Inbox →» hands its pinned scope to the inbox; the key is
 *     revoked and the next real curl fails closed 401 with NO new row.
 *
 * Everything disposable is removed in afterAll with printed proof: uvicorn,
 * the container, the scratch tree (env file, data root, uvicorn log) and the
 * migrated database (inside the removed container).
 *
 * Node-builtin access: this repository deliberately ships no @types/node
 * (tests stay DOM-typed; server-enrollment-live.spec.ts ambient-declares
 * `process` the same way), so the builtins are loaded through the Node >= 22.3
 * runtime API `process.getBuiltinModule` and narrowed to the small typed
 * facades below. Playwright runs on real Node, where they always resolve; the
 * first use in beforeAll would throw immediately if a shape were wrong.
 */

/** Ambient Node surface (same convention as server-enrollment-live.spec.ts). */
declare const process: {
	env: Record<string, string | undefined>;
	getBuiltinModule(id: string): unknown;
};

/** The narrow child-process surface this fixture relies on. */
interface Proc {
	exitCode: number | null;
	kill(signal?: string): boolean;
	once(event: 'exit', cb: () => void): void;
	on(event: 'exit', cb: (code: number | null) => void): void;
	on(event: 'error', cb: (error: Error) => void): void;
	on(event: 'close', cb: (code: number | null) => void): void;
	stdout: { on(event: 'data', cb: (chunk: unknown) => void): void };
	stderr: { on(event: 'data', cb: (chunk: unknown) => void): void };
	stdin: { end(data?: string): void; on(event: 'error', cb: (error: Error) => void): void };
}

interface ServerLike {
	unref(): void;
	on(event: 'error', cb: (error: Error) => void): void;
	listen(port: number, host: string, cb: () => void): void;
	address(): unknown;
	close(cb: () => void): void;
}

interface SocketLike {
	setTimeout(ms: number): void;
	on(event: 'connect', cb: () => void): void;
	on(event: 'error', cb: (error: Error) => void): void;
	on(event: 'timeout', cb: () => void): void;
	destroy(): void;
}

// Unchecked casts of well-known builtin shapes the check environment cannot
// type (no @types/node); the first beforeAll call exercises every entry.
const cp = process.getBuiltinModule('node:child_process') as {
	execFile(
		cmd: string,
		args: string[],
		opts: Record<string, unknown>,
		cb: (error: (Error & { code?: number | string }) | null, stdout: string, stderr: string) => void
	): unknown;
	spawn(cmd: string, args: string[], opts: Record<string, unknown>): Proc;
};
const fs = process.getBuiltinModule('node:fs') as {
	existsSync(path: string): boolean;
	mkdirSync(path: string, opts: { recursive: boolean }): void;
	rmSync(path: string, opts: { recursive: boolean; force: boolean }): void;
	writeFileSync(path: string, data: string, opts?: { mode?: number }): void;
	openSync(path: string, flags: string): number;
	closeSync(fd: number): void;
};
const net = process.getBuiltinModule('node:net') as {
	createServer(): ServerLike;
	connect(opts: { port: number; host: string }): SocketLike;
};
const nodeCrypto = process.getBuiltinModule('node:crypto') as {
	createHash(algorithm: string): { update(data: string): { digest(encoding: string): string } };
	randomUUID(): string;
};
const nodePath = process.getBuiltinModule('node:path') as {
	join(...parts: string[]): string;
	resolve(...parts: string[]): string;
};
const nodeUrl = process.getBuiltinModule('node:url') as {
	fileURLToPath(url: string | URL): string;
};

const ORIGIN = 'http://127.0.0.1:4173';
const RUN_ID = nodeCrypto.randomUUID().slice(0, 8);
const SCRATCH = `/tmp/mai-fxt-nbi-e2e/${RUN_ID}`;
const PG_CONTAINER = `mai-fxt-nbi-e2e-pg-${RUN_ID}`;
const PG_DB = 'mai_nbi_e2e';
const PG_PASSWORD = `nbi-e2e-${nodeCrypto.randomUUID()}`; // synthetic, disposable, never printed
const BACKEND_ROOT = nodePath.resolve(nodeUrl.fileURLToPath(new URL('.', import.meta.url)), '..', '..', 'backend');
const ADMIN_PASSWORD = `nbi-e2e-${nodeCrypto.randomUUID()}`; // synthetic, disposable, never printed

// Fixture-scoped subjects of the journey (synthetic values; never secrets).
const SERVER_NAME = 'nbi-e2e-core';
const SERVER_ID = nodeCrypto.randomUUID();
const GLOBAL_KEY_LABEL = 'ci-pipeline';
const SCOPED_KEY_LABEL = 'host-monitor';

/**
 * The REAL backend project interpreter, resolved without assuming a
 * checked-in venv (isolated worktrees have no backend/.venv):
 * MAI_BACKEND_PYTHON override, then the uv project environment
 * ($UV_PROJECT_ENVIRONMENT), then uv's default in-project venv.
 * Fails closed naming every candidate tried.
 */
function backendPython(): string {
	const candidates: Array<string | undefined> = [
		process.env.MAI_BACKEND_PYTHON,
		process.env.UV_PROJECT_ENVIRONMENT
			? nodePath.join(process.env.UV_PROJECT_ENVIRONMENT, 'bin', 'python')
			: undefined,
		nodePath.join(BACKEND_ROOT, '.venv', 'bin', 'python')
	];
	for (const candidate of candidates) {
		if (candidate && fs.existsSync(candidate)) return candidate;
	}
	throw new Error(
		`no backend interpreter for the live fixture; tried: ${candidates
			.map((candidate) => candidate ?? '<unset>')
			.join(', ')}`
	);
}

// Fixture-name refusal guard: every docker command in this file only ever
// addresses containers matching this pattern, and the database URL handed to
// uvicorn/alembic is constructed here — never read from the ambient
// environment (ambient MAI_* is excluded from child env by construction).
const FIXTURE_NAME_RE = /^mai-fxt-nbi-e2e-pg-[0-9a-f]{8}$/;

interface ExecResult {
	code: number;
	stdout: string;
	stderr: string;
}

function delay(ms: number): Promise<void> {
	const { promise, resolve } = Promise.withResolvers<void>();
	setTimeout(resolve, ms);
	return promise;
}

function run(cmd: string, args: string[], opts: { timeout?: number; cwd?: string; env?: Record<string, string | undefined> } = {}): Promise<ExecResult> {
	const { promise, resolve } = Promise.withResolvers<ExecResult>();
	cp.execFile(cmd, args, { timeout: opts.timeout ?? 60_000, cwd: opts.cwd, env: opts.env ?? process.env }, (error, stdout, stderr) => {
		const code = error ? (typeof error.code === 'number' ? error.code : 1) : 0;
		resolve({ code, stdout: String(stdout), stderr: String(stderr) });
	});
	return promise;
}

function runWithInput(cmd: string, args: string[], input: string, timeoutMs = 30_000): Promise<ExecResult> {
	const { promise, resolve } = Promise.withResolvers<ExecResult>();
	const child = cp.spawn(cmd, args, { stdio: ['pipe', 'pipe', 'pipe'] });
	let stdout = '';
	let stderr = '';
	const timer = setTimeout(() => child.kill('SIGKILL'), timeoutMs);
	child.stdout.on('data', (chunk) => (stdout += String(chunk)));
	child.stderr.on('data', (chunk) => (stderr += String(chunk)));
	child.on('error', (error) => {
		clearTimeout(timer);
		resolve({ code: 1, stdout, stderr: `${stderr}${error.message}` });
	});
	child.on('close', (code) => {
		clearTimeout(timer);
		resolve({ code: code ?? 1, stdout, stderr });
	});
	child.stdin.on('error', () => undefined); // EPIPE if curl exits early
	child.stdin.end(input);
	return promise;
}

async function freePort(): Promise<number> {
	const { promise, resolve, reject } = Promise.withResolvers<number>();
	const server = net.createServer();
	server.unref();
	server.on('error', reject);
	server.listen(0, '127.0.0.1', () => {
		const address = server.address();
		if (typeof address === 'object' && address !== null && 'port' in address && typeof address.port === 'number') {
			resolve(address.port);
		} else {
			reject(new Error('no ephemeral port from the listener'));
		}
	});
	const port = await promise;
	const { promise: closed, resolve: resolveClose } = Promise.withResolvers<void>();
	server.close(() => resolveClose());
	await closed;
	return port;
}

async function waitTcp(port: number, timeoutMs: number): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		const { promise, resolve } = Promise.withResolvers<boolean>();
		const socket = net.connect({ port, host: '127.0.0.1' });
		socket.setTimeout(2_000);
		socket.on('connect', () => {
			socket.destroy();
			resolve(true);
		});
		socket.on('error', () => resolve(false));
		socket.on('timeout', () => {
			socket.destroy();
			resolve(false);
		});
		if (await promise) return;
		await delay(300);
	}
	throw new Error(`port ${port} never opened within ${timeoutMs}ms`);
}

interface LiveStack {
	scratch: string;
	dataRoot: string;
	envFile: string;
	pgUrl: string;
	backendOrigin: string;
	uviproc: Proc | null;
	browserCtx: BrowserContext | null;
	page: Page | null;
	/** The GLOBAL key's one-time plaintext (used, then revoked). */
	globalKey: string;
	globalKeyId: string;
	/** The SERVER-scoped key's one-time plaintext. */
	scopedKey: string;
	/** Durable row identities the journey asserts on. */
	globalAlertId: string;
	scopedAlertId: string;
}

const live = {} as LiveStack;

function docker(...args: string[]): Promise<ExecResult> {
	if (args.some((arg) => /^mai-/.test(arg) && !FIXTURE_NAME_RE.test(arg))) {
		throw new Error(`refusing docker command against non-fixture name: ${args.join(' ')}`);
	}
	return run('docker', args, { timeout: 120_000 });
}

/** Same-origin API access from Node with the browser's REAL session cookies. */
async function apiFetch(method: string, apiPath: string, body?: unknown): Promise<Response> {
	const ctx = live.browserCtx;
	if (!ctx) throw new Error('live context not started');
	const cookies = await ctx.cookies(ORIGIN);
	const cookieHeader = cookies.map((cookie) => `${cookie.name}=${cookie.value}`).join('; ');
	const csrf = cookies.find((cookie) => cookie.name === 'mai_csrf')?.value;
	return fetch(live.backendOrigin + apiPath, {
		method,
		headers: {
			'content-type': 'application/json',
			origin: ORIGIN,
			cookie: cookieHeader,
			...(body !== undefined && csrf ? { 'x-csrf-token': csrf } : {})
		},
		body: body === undefined ? undefined : JSON.stringify(body)
	});
}

interface IngestReply {
	exit: number;
	status: number;
	body: string;
}

/**
 * A REAL external publisher: the `curl` SUBPROCESS (no cookies, no browser
 * state — the machine lane is cookieless by contract) POSTs to the real
 * uvicorn with `Authorization: Bearer <key>`.
 */
async function curlIngest(key: string, payload: Record<string, unknown>): Promise<IngestReply> {
	const result = await runWithInput(
		'curl',
		[
			'-sS',
			'-X', 'POST', `${live.backendOrigin}/api/v1/notifications/ingest`,
			'-H', `Authorization: Bearer ${key}`,
			'-H', 'Content-Type: application/json',
			'-w', '\n%{http_code}',
			'--data-binary', '@-'
		],
		JSON.stringify(payload),
		30_000
	);
	const lastNewline = result.stdout.lastIndexOf('\n');
	const status = Number(result.stdout.slice(lastNewline + 1).trim());
	const body = result.stdout.slice(0, lastNewline);
	return { exit: result.code, status, body };
}

/** Direct durable-store read inside the fixture container (psql, -tA). */
async function dbCount(where: string): Promise<number> {
	const result = await docker(
		'exec', PG_CONTAINER, 'psql', '-U', 'postgres', '-d', PG_DB, '-tAc',
		`SELECT COUNT(*) FROM notification_alerts WHERE ${where}`
	);
	if (result.code !== 0) throw new Error(`psql count failed: ${result.stderr}`);
	return Number(result.stdout.trim());
}

const cleanupProof: string[] = [];

async function killUvicorn(): Promise<void> {
	const proc = live.uviproc;
	live.uviproc = null;
	if (!proc || proc.exitCode !== null) {
		cleanupProof.push(`uvicorn: already exited (code ${proc?.exitCode ?? 'n/a'})`);
		return;
	}
	const { promise, resolve } = Promise.withResolvers<void>();
	const timer = setTimeout(() => proc.kill('SIGKILL'), 5_000);
	proc.once('exit', () => {
		clearTimeout(timer);
		resolve();
	});
	proc.kill('SIGTERM');
	await promise;
	cleanupProof.push(`uvicorn: terminated (exit code ${proc.exitCode})`);
}

async function removeContainer(name: string, what: string): Promise<void> {
	if (!FIXTURE_NAME_RE.test(name)) throw new Error(`refusing to remove non-fixture container ${name}`);
	await docker('rm', '-f', name);
	const listing = await docker('ps', '-a', '--filter', `name=^${name}$`, '--format', '{{.Names}}');
	cleanupProof.push(`${what} container ${name} after rm: '${listing.stdout.trim() || '<none>'}'`);
}

test.describe('live full-stack notification inbox (real uvicorn + postgres + curl)', () => {
	test.describe.configure({ mode: 'serial', timeout: 240_000 });

	test.beforeAll(async ({ browser }) => {
		// Hook-scoped timeout (the fn+timeout hook signature no longer exists).
		test.setTimeout(300_000);
		const dockerCheck = await run('docker', ['info', '--format', '{{.ServerVersion}}']);
		test.skip(dockerCheck.code !== 0, 'docker unavailable: the live stack needs disposable postgres');

		// -- disposable scratch + minimal resolver env file --------------------
		fs.mkdirSync(SCRATCH, { recursive: true });
		live.scratch = SCRATCH;
		live.dataRoot = nodePath.join(SCRATCH, 'data-root');
		live.envFile = nodePath.join(SCRATCH, 'resolver.env');
		fs.mkdirSync(live.dataRoot, { recursive: true });
		fs.writeFileSync(live.envFile, '', { mode: 0o600 });

		// -- disposable PostgreSQL (own container, uniquely named DB) ----------
		const pgPort = await freePort();
		live.pgUrl = `postgresql://postgres:${PG_PASSWORD}@127.0.0.1:${pgPort}/${PG_DB}`;
		const pgRun = await docker(
			'run', '--rm', '-d', '--name', PG_CONTAINER,
			'-p', `127.0.0.1:${pgPort}:5432`,
			'-e', `POSTGRES_PASSWORD=${PG_PASSWORD}`,
			'-e', `POSTGRES_DB=${PG_DB}`,
			'pgvector/pgvector:0.8.1-pg18'
		);
		expect(pgRun.code, `disposable postgres failed to start: ${pgRun.stderr}`).toBe(0);
		// Readiness is the real migration command itself at the published host
		// endpoint (the container-local unix socket answers BEFORE the final
		// TCP listener exists — the bounded retry covers only the classified
		// startup failure while the fresh DB initializes; never a second
		// successful migration, no sleeps).
		const migrateEnv = { PATH: process.env.PATH!, HOME: process.env.HOME!, MAI_DATABASE_URL: live.pgUrl };
		let migrated: ExecResult;
		for (let attempt = 0; ; attempt += 1) {
			migrated = await run(
				backendPython(),
				['-m', 'alembic', '-c', 'alembic.ini', 'upgrade', 'head'],
				{ cwd: BACKEND_ROOT, env: migrateEnv, timeout: 180_000 }
			);
			if (migrated.code === 0 || attempt >= 59 || !migrated.stderr.startsWith('migration aborted: health=unreachable')) break;
		}
		expect(migrated.code, `alembic upgrade head failed: ${migrated.stderr.slice(-800)}`).toBe(0);

		// -- one REAL enrolled server profile through the canonical pipeline ---
		// The machine lane's scope gate and the panel's scope/group names read
		// the same profile store enrollment writes; the fixture goes through
		// ProfileProjection.apply_upsert (journal + hash-verified file pair),
		// never a handcrafted file.
		const hostKeyDigest = nodeCrypto.createHash('sha256').update(`nbi-e2e-host-key-${RUN_ID}`).digest('base64').replace(/=+$/, '');
		const profileScript = [
			'import asyncio, os, uuid',
			'import yaml',
			'from mai.contracts.server import SCHEMA_VERSION',
			'from mai.database import create_app_engine',
			'from mai.profiles.files import ProfileFileStore',
			'from mai.profiles.projection import ProfileProjection',
			'sid = uuid.UUID(os.environ["NBI_SERVER_ID"])',
			'document = yaml.safe_dump({',
			'    "schema_version": SCHEMA_VERSION,',
			'    "server_id": str(sid),',
			'    "name": os.environ["NBI_SERVER_NAME"],',
			'    "prompt_revision": 1,',
			'    "environment": "linux_ssh",',
			'    "ssh": {"host": "127.0.0.1", "port": 22, "user": "root",',
			'            "host_key": os.environ["NBI_HOST_KEY"],',
			'            "credential_ref": "MAI_SSH_KEY_MAI"},',
			'    "privilege": "none",',
			'    "capabilities": [],',
			'}, sort_keys=False, default_flow_style=False)',
			'async def main():',
			'    engine, factory = create_app_engine(os.environ["NBI_DATABASE_URL"])',
			'    try:',
			'        store = ProfileFileStore(os.environ["NBI_DATA_ROOT"])',
			'        result = await ProfileProjection(factory).apply_upsert(store, sid, document, expected_revision=0)',
			'        print(result.outcome)',
			'    finally:',
			'        await engine.dispose()',
			'asyncio.run(main())'
		].join('\n');
		const enrolled = await run(
			backendPython(),
			['-c', profileScript],
			{
				cwd: BACKEND_ROOT,
				timeout: 120_000,
				env: {
					PATH: process.env.PATH!,
					HOME: process.env.HOME!,
					NBI_DATA_ROOT: live.dataRoot,
					NBI_DATABASE_URL: live.pgUrl,
					NBI_SERVER_ID: SERVER_ID,
					NBI_SERVER_NAME: SERVER_NAME,
					NBI_HOST_KEY: `SHA256:${hostKeyDigest}`
				}
			}
		);
		expect(enrolled.code, `canonical profile write failed: ${enrolled.stderr.slice(-800)}`).toBe(0);
		expect(enrolled.stdout.trim()).toBe('applied');

		// -- real uvicorn ASGI on a loopback ephemeral port --------------------
		const backendPort = await freePort();
		live.backendOrigin = `http://127.0.0.1:${backendPort}`;
		const uvicornEnv: Record<string, string | undefined> = {
			PATH: process.env.PATH!,
			HOME: process.env.HOME!,
			MAI_DATABASE_URL: live.pgUrl,
			MAI_DATA_ROOT: live.dataRoot,
			MAI_ENV_FILE: live.envFile,
			MAI_PUBLIC_ORIGINS: ORIGIN
		};
		const uvicornLog = nodePath.join(SCRATCH, 'uvicorn.log');
		const logFd = fs.openSync(uvicornLog, 'a');
		const uviproc = cp.spawn(
			backendPython(),
			['-m', 'uvicorn', 'mai.api.app:create_app', '--factory', '--host', '127.0.0.1', '--port', String(backendPort)],
			{ cwd: BACKEND_ROOT, env: uvicornEnv, stdio: ['ignore', logFd, logFd] }
		);
		live.uviproc = uviproc;
		fs.closeSync(logFd);
		uviproc.on('exit', (code) => {
			if (code !== null && code !== 0) {
				// surfaced for diagnosis; the log contains no credential material
				console.log(`[nbi-e2e] uvicorn exited early with code ${code}; see ${uvicornLog}`);
			}
		});
		await waitTcp(backendPort, 60_000);

		// One-shot bootstrap through the real boundary (same-origin origin
		// header, exactly like an administrator's non-browser setup client).
		const bootstrap = await fetch(`${live.backendOrigin}/api/v1/auth/bootstrap`, {
			method: 'POST',
			headers: { 'content-type': 'application/json', origin: ORIGIN },
			body: JSON.stringify({ password: ADMIN_PASSWORD })
		});
		expect(bootstrap.status, 'one-shot bootstrap at the real backend').toBe(201);

		// -- browser context: production UI + the /api relay (no mocks) --------
		const ctx = await browser.newContext({ baseURL: ORIGIN, viewport: { width: 1280, height: 900 } });
		live.browserCtx = ctx;
		await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: ORIGIN });
		await ctx.route('**/api/**', async (route) => {
			const request = route.request();
			const target = new URL(request.url());
			// continue_ with a URL override = the same-origin relay as a REAL
			// reverse proxy: headers/cookies/body untouched, and the SSE event
			// stream's chunks flow through natively (no response buffering).
			await route.continue({ url: live.backendOrigin + target.pathname + target.search });
		});
		live.page = await ctx.newPage();
	});

	test.afterAll(async () => {
		try {
			await live.page?.close().catch(() => undefined);
			await live.browserCtx?.close().catch(() => undefined);
			await killUvicorn();
			await removeContainer(PG_CONTAINER, 'postgres');
			if (live.scratch) {
				fs.rmSync(live.scratch, { recursive: true, force: true });
				cleanupProof.push(`scratch ${live.scratch} exists after rm: ${fs.existsSync(live.scratch)}`);
			}
			const leftovers = await run('docker', ['ps', '-a', '--filter', 'name=mai-fxt-nbi-e2e', '--format', '{{.Names}}']);
			cleanupProof.push(`remaining mai-fxt-nbi-e2e containers: '${leftovers.stdout.trim() || '<none>'}'`);
			console.log(`\nNBI E2E CLEANUP PROOF:\n${cleanupProof.map((line) => `  - ${line}`).join('\n')}`);
		} catch (error) {
			console.log(`NBI E2E CLEANUP ERROR: ${String(error)}`);
			console.log(cleanupProof.join('\n'));
		}
	});

	const page = (): Page => {
		if (!live.page) throw new Error('live page not initialized');
		return live.page;
	};

	/** The singleton notifications window's frame (windowed ADR naming). */
	function notificationsFrame() {
		return page().locator('.mai-window-frame[data-panel-kind="notifications"]');
	}

	/** The OVERVIEW window's frame (one per pinned server scope). */
	function overviewFrame() {
		return page().locator('.mai-window-frame[data-panel-kind="overview"]');
	}

	/**
	 * The rail (and its unread badge) exists ONLY in the collapsed drawer
	 * (MCR S4: expanded drawer renders no rail — desktop-shell.spec contract).
	 * The reviewed interaction: `side-drawer-toggle` collapses the drawer,
	 * `side-drawer-rail` appears, the badge reads server truth; the second
	 * toggle restores the expanded tree.
	 */
	async function collapseDrawerToRail(): Promise<void> {
		await page().getByTestId('side-drawer-toggle').click();
		await expect(page().getByTestId('side-drawer-rail')).toBeVisible();
	}

	async function expandDrawer(): Promise<void> {
		await page().getByTestId('side-drawer-toggle').click();
		await expect(page().getByTestId('side-drawer-rail')).toHaveCount(0);
	}

	async function openNotificationsWindow(): Promise<void> {
		// The expanded tree's Global / Fleet launcher opens the ONE singleton
		// window (the same reviewed route dashboard.spec's shell harness uses).
		await page().getByTestId('open-notifications').click();
		await expect(notificationsFrame().getByTestId('notifications-root')).toBeVisible({ timeout: 30_000 });
	}

	async function openSourcesTab(): Promise<void> {
		await notificationsFrame().getByTestId('tab-sources').click();
		await expect(notificationsFrame().getByTestId('keys-root')).toBeVisible();
	}

	/**
	 * The one-time secret modal's plaintext: read from `issued-key-value`,
	 * cross-checked against the real clipboard after the Copy action, and
	 * proven absent from the curl example (placeholder only). Closing clears
	 * the plaintext from component state — then it must be absent from the
	 * whole DOM AND from both web-storage areas (C03 rule 6 hygiene).
	 */
	async function captureIssuedSecret(): Promise<string> {
		const modal = page().getByTestId('issued-key-modal');
		await expect(modal).toBeVisible();
		await expect(modal.getByTestId('issued-key-warning')).toContainText('ONLY ONCE');
		const plaintext = ((await modal.getByTestId('issued-key-value').textContent()) ?? '').trim();
		expect(plaintext.length, 'minted key is an opaque 256-bit token').toBeGreaterThanOrEqual(40);
		expect(plaintext).toMatch(/^[A-Za-z0-9_-]+$/);
		await modal.getByTestId('issued-key-curl').innerText();
		expect(await modal.getByTestId('issued-key-curl').innerText()).not.toContain(plaintext);
		await modal.getByTestId('issued-key-copy').click();
		await expect(modal.getByTestId('issued-key-copied')).toHaveText('Copied to clipboard.');
		const clipboard: string = await page().evaluate(() => navigator.clipboard.readText());
		expect(clipboard).toBe(plaintext);
		await modal.getByTestId('issued-key-close').click();
		await expect(page().getByTestId('issued-key-modal')).toHaveCount(0);
		const dom = await page().content();
		expect(dom, 'plaintext left the DOM with the modal').not.toContain(plaintext);
		const storage: string = await page().evaluate(
			() =>
				JSON.stringify({
					local: { ...window.localStorage },
					session: { ...window.sessionStorage }
				})
		);
		expect(storage, 'plaintext is never persisted').not.toContain(plaintext);
		return plaintext;
	}

	test('real login through the production UI and boundary', async () => {
		await page().goto('/login');
		await expect(page().getByTestId('login-form')).toBeVisible();
		await page().locator('input[type="password"]').fill(ADMIN_PASSWORD);
		await page().getByTestId('login-submit').click();
		await expect(page()).toHaveURL(/\/$/);
		await expect(page().getByTestId('component-host')).toBeVisible();
		// The expanded tree renders the Global / Fleet launcher for the entry.
		await expect(page().getByTestId('open-notifications')).toContainText('Notifications');
		// Fresh inbox: the fleet-wide unread badge is honestly absent at zero.
		await collapseDrawerToRail();
		await expect(page().getByTestId('rail-notifications-badge')).toHaveCount(0);
		await expandDrawer();
		// The fixture server is enrolled for real (canonical profile store).
		const servers = await apiFetch('GET', '/api/v1/servers');
		expect(servers.status).toBe(200);
		const serverList: { servers: Array<{ server_id: string; name: string }> } = await servers.json();
		expect(serverList.servers).toHaveLength(1);
		expect(serverList.servers[0].server_id).toBe(SERVER_ID);
		expect(serverList.servers[0].name).toBe(SERVER_NAME);
	});

	test('global key mint: one-time plaintext captured, copied, then absent everywhere', async () => {
		await openNotificationsWindow();
		// Fresh durable inbox: the honest empty state (wakeup-only stream is
		// 'connecting' until the FIRST identity frame flips it live — proven
		// after the real publish in the next test).
		await expect(page().getByTestId('notifications-empty')).toBeVisible();
		await openSourcesTab();
		await expect(page().getByTestId('keys-empty')).toBeVisible();
		await page().getByTestId('keys-new-label').fill(GLOBAL_KEY_LABEL);
		// The Scope select defaults to Global — the mint omits server_id.
		await expect(page().getByTestId('keys-new-scope')).toHaveValue('global');
		await page().getByTestId('keys-create').click();

		live.globalKey = await captureIssuedSecret();

		// The key table shows METADATA ONLY: label + Global scope + lifecycle.
		const row = page().getByTestId('key-row').filter({ hasText: GLOBAL_KEY_LABEL });
		await expect(row).toHaveCount(1);
		live.globalKeyId = (await row.getAttribute('data-key-id')) ?? '';
		expect(live.globalKeyId).toMatch(/^[0-9a-f-]{36}$/);
		await expect(row.getByTestId('key-scope')).toHaveText('Global');
		await expect(row.getByTestId('key-status')).toHaveAttribute('data-revoked', 'false');
		const tableText = await page().getByTestId('keys-table').innerText();
		expect(tableText, 'no key material in the metadata table').not.toContain(live.globalKey);
	});

	test('real curl ingest lands LIVE in the open inbox and rail badge (SSE); replay is deduped', async () => {
		await notificationsFrame().getByTestId('tab-inbox').click();
		await expect(notificationsFrame().getByTestId('notifications-root')).toBeVisible();

		const reply = await curlIngest(live.globalKey, {
			title: 'nbi-e2e build 42 finished',
			body: 'pipeline stage deploy finished with warnings on runner 7',
			severity: 'warning',
			source: GLOBAL_KEY_LABEL,
			dedup_key: 'build-42'
		});
		expect(reply.exit, 'curl subprocess succeeded').toBe(0);
		expect(reply.status, 'ingest answers 200 created').toBe(200);
		const created: { alert_id: string; disposition: string } = JSON.parse(reply.body);
		expect(created.disposition).toBe('created');
		live.globalAlertId = created.alert_id;

		// LIVE within one coarse tick (5 s) + refetch debounce — the open
		// panel and the rail badge update WITHOUT any manual reload. The
		// first identity frame also flips the panel's honest stream state.
		const tickBudget = { timeout: 7_000 };
		await expect(page().getByTestId('live-status')).toHaveAttribute('data-live', 'live', tickBudget);
		const row = page().getByTestId('notice-row').filter({ hasText: 'nbi-e2e build 42 finished' });
		await expect(row, 'notice appears live in the open inbox').toHaveCount(1, tickBudget);
		await expect(row).toHaveAttribute('data-alert-id', live.globalAlertId, tickBudget);
		await expect(row).toHaveAttribute('data-acked', 'false', tickBudget);
		await expect(row.getByTestId('row-severity-label')).toHaveText('warning');
		await expect(row.getByTestId('row-source')).toHaveText(GLOBAL_KEY_LABEL);
		await expect(page().getByTestId('group-heading')).toHaveText('Global — 1 loaded', tickBudget);
		await expect(page().getByTestId('unread-count')).toHaveText('Unread: 1', tickBudget);
		// The rail badge lives only in the collapsed drawer (MCR S4).
		await collapseDrawerToRail();
		await expect(page().getByTestId('rail-notifications-badge')).toHaveText('1', tickBudget);
		await expandDrawer();
		await expect(page().getByTestId('notifications-page-count')).toHaveText('1 of 1 notices loaded.');

		// Idempotent replay of the same dedup_key: deduped, same row identity.
		const replay = await curlIngest(live.globalKey, {
			title: 'nbi-e2e build 42 finished',
			body: 'pipeline stage deploy finished with warnings on runner 7',
			severity: 'warning',
			source: GLOBAL_KEY_LABEL,
			dedup_key: 'build-42'
		});
		expect(replay.status, 'replay answers 200 deduped').toBe(200);
		const deduped: { alert_id: string; disposition: string } = JSON.parse(replay.body);
		expect(deduped.disposition).toBe('deduped');
		expect(deduped.alert_id).toBe(live.globalAlertId);
		// No duplicate row: the loaded page stays 1 of 1.
		await expect(page().getByTestId('notifications-page-count')).toHaveText('1 of 1 notices loaded.');
		await expect(page().getByTestId('notice-row')).toHaveCount(1);
		expect(await dbCount("dedup_key LIKE 'external:%'")).toBe(1);
	});

	test('canonical filters compose; body expands; read state toggles both ways', async () => {
		const row = () => page().getByTestId('notice-row').filter({ hasText: 'nbi-e2e build 42 finished' });

		// scope=global matches the NULL-server notice through the real route.
		await page().getByTestId('filter-scope').selectOption('global');
		await expect(page().getByTestId('group-heading')).toHaveText('Global — 1 loaded');
		await page().getByTestId('filter-scope').selectOption('all');

		// unread + origin=external still match; origin=finding matches nothing.
		await page().getByTestId('filter-read').selectOption('unread');
		await expect(row()).toHaveCount(1);
		await page().getByTestId('filter-origin-external').click();
		await expect(row()).toHaveCount(1);
		await page().getByTestId('filter-origin-finding').click();
		await expect(page().getByTestId('notifications-empty')).toBeVisible();
		await page().getByTestId('filter-origin-external').click();

		// Server-side q over the frozen field set (here: the body text).
		await page().getByTestId('notifications-search').fill('runner 7');
		await expect(page().getByTestId('notifications-search-count')).toHaveText('1 / 1');
		await page().getByTestId('notifications-search').fill('no-such-token-anywhere');
		await expect(page().getByTestId('notifications-empty')).toBeVisible();
		await page().getByTestId('notifications-search-clear').click();
		await expect(row()).toHaveCount(1);
		await page().getByTestId('filter-read').selectOption('all');

		// Body expansion: plain text details toggle open and closed.
		await expect(row().getByTestId('row-summary')).toHaveCount(0); // external rows carry no summary
		await row().getByTestId('row-expand').click();
		await expect(row().getByTestId('row-body')).toHaveText(
			'pipeline stage deploy finished with warnings on runner 7'
		);
		await row().getByTestId('row-expand').click();
		await expect(row().getByTestId('row-body')).toHaveCount(0);

		// Receipt-driven read toggle: the row flips ONLY on the ack receipt.
		await row().getByTestId('row-toggle').click();
		await expect(row()).toHaveAttribute('data-acked', 'true');
		await expect(row().getByTestId('row-toggle')).toHaveText('Mark unread');
		await expect(row().getByTestId('row-readstate')).toHaveText('read');
		await expect(page().getByTestId('unread-count')).toHaveText('Unread: 0');
		await collapseDrawerToRail();
		await expect(page().getByTestId('rail-notifications-badge')).toHaveCount(0);
		await expandDrawer();

		// Back to unread: the badge honestly returns.
		await row().getByTestId('row-toggle').click();
		await expect(row()).toHaveAttribute('data-acked', 'false');
		await expect(row().getByTestId('row-toggle')).toHaveText('Mark read');
		await collapseDrawerToRail();
		await expect(page().getByTestId('rail-notifications-badge')).toHaveText('1');
		await expandDrawer();
	});

	test('mark-all-read under the unread filter: receipt rowcount, badge drops to hidden', async () => {
		await page().getByTestId('filter-read').selectOption('unread');
		await expect(page().getByTestId('notice-row')).toHaveCount(1);

		await page().getByTestId('notifications-mark-all-read').click();
		await expect(page().getByTestId('mark-all-receipt')).toHaveText(
			'Marked 1 read under the current filters.'
		);
		// The unread-filtered page refetches honestly: nothing unread remains.
		await expect(page().getByTestId('notifications-empty')).toBeVisible();
		await expect(page().getByTestId('unread-count')).toHaveText('Unread: 0');
		await collapseDrawerToRail();
		await expect(page().getByTestId('rail-notifications-badge')).toHaveCount(0);
		await expandDrawer();

		// The durable row flipped (evidence preserved, never deleted).
		await page().getByTestId('filter-read').selectOption('all');
		const row = page().getByTestId('notice-row').filter({ hasText: 'nbi-e2e build 42 finished' });
		await expect(row).toHaveAttribute('data-acked', 'true');
	});

	test('server-scoped key + scoped notice + scope violations + Overview «View in Inbox →» hand-off', async () => {
		// Mint a SERVER-scoped key through the real Sources surface.
		await openSourcesTab();
		await page().getByTestId('keys-new-label').fill(SCOPED_KEY_LABEL);
		await page().getByTestId('keys-new-scope').selectOption({ label: SERVER_NAME });
		await page().getByTestId('keys-create').click();
		live.scopedKey = await captureIssuedSecret();
		const scopedRow = page().getByTestId('key-row').filter({ hasText: SCOPED_KEY_LABEL });
		await expect(scopedRow).toHaveCount(1);
		await expect(scopedRow.getByTestId('key-scope')).toHaveText(SERVER_NAME);

		// Publish with server_id; omit `source` — the route defaults it to the
		// key's label, which the row chip must show.
		const reply = await curlIngest(live.scopedKey, {
			title: 'nbi-e2e replication lag',
			body: 'replica lag 38s on db-1 exceeds the alert threshold',
			severity: 'critical',
			server_id: SERVER_ID,
			dedup_key: 'repl-lag-1'
		});
		expect(reply.exit).toBe(0);
		expect(reply.status, 'scoped ingest answers 200 created').toBe(200);
		const created: { alert_id: string; disposition: string } = JSON.parse(reply.body);
		expect(created.disposition).toBe('created');
		live.scopedAlertId = created.alert_id;

		// Back to the fleet view: the Sources tab was active for the mint, so
		// the inbox tab renders again before the live assertions.
		await notificationsFrame().getByTestId('tab-inbox').click();
		await expect(page().getByTestId('notifications-root')).toBeVisible();

		// LIVE under scope=all: a SECOND group appears under the server name.
		const tickBudget = { timeout: 7_000 };
		const scopedNotice = page().getByTestId('notice-row').filter({ hasText: 'nbi-e2e replication lag' });
		await expect(scopedNotice).toHaveCount(1, tickBudget);
		await expect(scopedNotice).toHaveAttribute('data-alert-id', live.scopedAlertId);
		await expect(scopedNotice.getByTestId('row-severity-label')).toHaveText('critical');
		await expect(scopedNotice.getByTestId('row-source')).toHaveText(SCOPED_KEY_LABEL);
		await collapseDrawerToRail();
		await expect(page().getByTestId('rail-notifications-badge')).toHaveText('1', tickBudget);
		await expandDrawer();

		// Scope enforcement (R3-F3): ANY server_id on a global key and a
		// foreign id on the scoped key answer the byte-identical 422.
		const foreignId = nodeCrypto.randomUUID();
		const globalViolation = await curlIngest(live.globalKey, {
			title: 'nbi-e2e must not land',
			server_id: foreignId
		});
		expect(globalViolation.status).toBe(422);
		const scopedViolation = await curlIngest(live.scopedKey, {
			title: 'nbi-e2e must not land',
			server_id: foreignId
		});
		expect(scopedViolation.status).toBe(422);
		const stripRequestId = (body: string): string =>
			body.replace(/"request_id":"[^"]*"/, '"request_id":"<per-request>"');
		// Byte-identical invalid_scope modulo the per-request audit UUID (the
		// frozen no-existence-oracle contract: code/message/fields never
		// distinguish unknown from foreign from global-key-with-server).
		expect(stripRequestId(scopedViolation.body), 'byte-identical invalid_scope, no existence oracle').toBe(
			stripRequestId(globalViolation.body)
		);
		const scopedViolationBody: { error: { code: string } } = JSON.parse(scopedViolation.body);
		expect(scopedViolationBody).toMatchObject({ error: { code: 'invalid_scope' } });
		expect(await dbCount("title = 'nbi-e2e must not land'")).toBe(0);

		// Overview hand-off: the overview window is pinned to the (only)
		// enrolled server; its inbox shows the unacknowledged notice and the
		// «View in Inbox →» opener hands the scope to the global window.
		await expect(page().getByTestId('server-select')).toHaveValue(SERVER_ID);
		await page().getByTestId('open-overview').click();
		const overview = overviewFrame();
		await expect(overview.getByTestId('overview-root')).toBeVisible({ timeout: 30_000 });
		await expect(overview.getByTestId('overview-root')).toHaveAttribute('data-scope', SERVER_ID);
		await expect(overview.getByTestId('inbox-count')).toHaveText('1 of 1 unacknowledged (latest page).');
		const overviewRow = overview.getByTestId('notice-row').filter({ hasText: 'nbi-e2e replication lag' });
		await expect(overviewRow).toHaveCount(1);
		await expect(overviewRow).toHaveAttribute('data-alert-id', live.scopedAlertId);

		await overview.getByTestId('inbox-view-link').click();
		// The ONE notifications window re-focuses ALREADY SCOPED (the
		// sequenced hand-off applied through the same setScope path).
		await expect(page().getByTestId('filter-scope')).toHaveValue(SERVER_ID, tickBudget);
		await expect(page().getByTestId('group-heading').filter({ hasText: SERVER_NAME })).toHaveText(
			`${SERVER_NAME} — 1 loaded`
		);
		// include_global composes (R4-F2): the read global notice rides along.
		await expect(page().getByTestId('filter-include-global')).toBeChecked();
		await expect(page().getByTestId('group-heading').filter({ hasText: 'Global —' })).toHaveCount(1);
		// Strictly the server when include_global=false (the refetch lands
		// asynchronously — poll the Global group's disappearance FIRST).
		await page().getByTestId('filter-include-global').uncheck();
		await expect(page().getByTestId('group-heading').filter({ hasText: 'Global —' })).toHaveCount(0);
		await expect(page().getByTestId('group-heading')).toHaveText(`${SERVER_NAME} — 1 loaded`);
	});

	test('revoked key: next real curl fails closed 401 and writes no row', async () => {
		await openSourcesTab();

		// Revoke the GLOBAL key: inline confirm, then receipt-driven flip.
		const globalRow = page().getByTestId('key-row').filter({ hasText: GLOBAL_KEY_LABEL });
		await globalRow.getByTestId('key-revoke').click();
		await globalRow.getByTestId('key-revoke-confirm').click();
		await expect(globalRow.getByTestId('key-status')).toHaveAttribute('data-revoked', 'true');
		await expect(globalRow.getByTestId('key-status')).toHaveText('revoked');
		await expect(globalRow.getByTestId('key-revoked-at')).not.toHaveText('—');
		// The scoped key stays untouched.
		await expect(
			page().getByTestId('key-row').filter({ hasText: SCOPED_KEY_LABEL }).getByTestId('key-status')
		).toHaveAttribute('data-revoked', 'false');

		// The next publish with the revoked key fails closed, value-free.
		const sentinelTitle = 'nbi-e2e post-revoke must not persist';
		const reply = await curlIngest(live.globalKey, {
			title: sentinelTitle,
			body: 'no row may exist for this title',
			severity: 'info',
			dedup_key: 'post-revoke-1'
		});
		expect(reply.status, 'revoked key answers 401').toBe(401);
		const rejected: { error: { code: string } } = JSON.parse(reply.body);
		expect(rejected).toMatchObject({ error: { code: 'unauthenticated' } });

		// NO new row: canonical API AND the durable table agree.
		const search = await apiFetch('GET', `/api/v1/notifications?q=${encodeURIComponent(sentinelTitle)}`);
		expect(search.status).toBe(200);
		const searchPage: { total: number } = await search.json();
		expect(searchPage.total).toBe(0);
		expect(await dbCount(`title = '${sentinelTitle}'`)).toBe(0);

		// The unread truth is untouched: the scoped notice stays counted.
		await collapseDrawerToRail();
		await expect(page().getByTestId('rail-notifications-badge')).toHaveText('1');
		await expandDrawer();
	});
});
