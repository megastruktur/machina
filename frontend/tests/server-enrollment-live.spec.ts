import { test, expect, type BrowserContext, type Page } from '@playwright/test';

/**
 * so-04 full-stack LIVE proof — guided server onboarding with NO mocks.
 *
 * Real stack under test, started by this fixture and torn down with proof:
 *  - REAL uvicorn ASGI (`mai.api.app:create_app --factory`) on a loopback
 *    ephemeral port, constructed from a fixture-only environment:
 *    MAI_DATABASE_URL / MAI_DATA_ROOT / MAI_ENV_FILE / MAI_PUBLIC_ORIGINS.
 *    No MAI_SSH_CREDENTIAL_REF is set, so the installer fallback
 *    MAI_SSH_KEY_MAI is the genuinely exercised credential reference.
 *  - REAL disposable PostgreSQL (own container, uniquely named DB), migrated
 *    with the repository's alembic chain before uvicorn starts.
 *  - REAL disposable Arch OpenSSH container (openssh + sudo image), started
 *    with root's authorized_keys EMPTY; the controller's public key is
 *    installed only through the documented external step (docker exec),
 *    never by weakening anything in the product.
 *  - Production-build Svelte UI served by the Playwright webServer
 *    (vite build + vite preview, port 4173) in real Chromium.
 *
 * The ONLY route interception is the same-origin `/api/**` relay to the real
 * uvicorn (`route.fetch`), exactly the pattern approved for login.spec.ts's
 * live journey: real cookies, real CSRF, real Origin, real backend bodies.
 * No `route.fulfill` synthetic API responses exist in this file.
 *
 * Live journey (mirrors the documented administrator flow):
 *  1. one-shot bootstrap + login through the real boundary and the real UI;
 *  2. controller public key exported by the real backend, copied EXACTLY in
 *     the browser, cross-checked against `ssh-keygen -y`; no credential-ref
 *     or host-key input anywhere;
 *  3. EMPTY authorized_keys: real discovery returns real candidates, the
 *     pinned test honestly fails authentication, nothing is written;
 *  4. negative enroll controls through the real API: unconfirmed trust and
 *     a changed (never-presented) fingerprint both leave zero durable state;
 *  5. the EXACT copied public key is authorized for root (fixture-only
 *     docker exec), then the full guided flow: discovery → select the real
 *     ed25519 fingerprint → authenticated Linux identity → explicit Trust →
 *     Enroll → the shell selects the new server;
 *  6. read-back: canonical `servers/<id>/known_hosts` pin matches the
 *     selected fingerprint, profile API returns the pin + fallback
 *     credential_ref, the profile panel shows the pinned key, and the
 *     EXISTING pinned `probe_environment` helper (an SSH helper in
 *     mai.execution.ssh — NOT an HTTP route) runs against the persisted
 *     identity/store with no second trust step;
 *  7. privilege failure (password-required sudo for a limited user) writes
 *     nothing.
 *
 * Everything disposable is removed in afterAll with printed proof:
 * uvicorn, both containers, the scratch tree (keys, env file, data root)
 * and the migrated database (inside the removed container).
 *
 * Node-builtin access: this repository deliberately ships no @types/node
 * (tests stay DOM-typed; login.spec.ts ambient-declares `process` the same
 * way), so the builtins are loaded through the Node >= 22.3 runtime API
 * `process.getBuiltinModule` and narrowed to the small typed facades below.
 * Playwright runs on real Node, where they always resolve; the first use in
 * beforeAll would throw immediately if a shape were wrong.
 */

/** Ambient Node surface (same convention as login.spec.ts, plus the
 * runtime builtin loader this fixture needs). */
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
	readFileSync(path: string, encoding: string): string;
	readdirSync(path: string, opts: { withFileTypes: true }): Array<{ name: string; isDirectory(): boolean }>;
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
const SCRATCH = `/tmp/mai-fxt-so04-live/${RUN_ID}`;
const PG_CONTAINER = `mai-fxt-so04-live-pg-${RUN_ID}`;
const SSHD_CONTAINER = `mai-fxt-so04-live-sshd-${RUN_ID}`;
const PG_DB = 'mai_so04_live';
const PG_PASSWORD = `so04-live-${nodeCrypto.randomUUID()}`; // synthetic, disposable, never printed
const SSHD_IMAGE = 'mai-fxt-so-01-ssh-arch:sshd'; // task-owned image (arch + openssh + sudo)
const BACKEND_ROOT = nodePath.resolve(nodeUrl.fileURLToPath(new URL('.', import.meta.url)), '..', '..', 'backend');
const ADMIN_PASSWORD = `so04-live-${nodeCrypto.randomUUID()}`; // synthetic, disposable, never printed
const FALLBACK_REF = 'MAI_SSH_KEY_MAI';

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
const FIXTURE_NAME_RE = /^mai-fxt-so04-live-(pg|sshd)-[0-9a-f]{8}$/;

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
	child.stdin.on('error', () => undefined); // EPIPE if the shell exits early
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

function walk(root: string): string[] {
	if (!fs.existsSync(root)) return [];
	const out: string[] = [];
	const visit = (dir: string, prefix: string) => {
		for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
			const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
			out.push(rel);
			if (entry.isDirectory()) visit(nodePath.join(dir, entry.name), rel);
		}
	};
	visit(root, '');
	return out.sort();
}

/** Real `ssh-keygen -lf` fingerprint of one public-key line (never guesses). */
async function fingerprintOf(publicLine: string): Promise<string> {
	const file = nodePath.join(SCRATCH, `fp-${nodeCrypto.createHash('sha1').update(publicLine).digest('hex').slice(0, 10)}.pub`);
	fs.writeFileSync(file, `${publicLine}\n`, { mode: 0o600 });
	const keygen = await run('ssh-keygen', ['-lf', file]);
	if (keygen.code !== 0) throw new Error(`ssh-keygen -lf failed: ${keygen.stderr}`);
	const fingerprint = keygen.stdout.trim().split(/\s+/)[1];
	if (!/^SHA256:/.test(fingerprint)) throw new Error(`unexpected fingerprint output: ${keygen.stdout}`);
	return fingerprint;
}

interface LiveStack {
	scratch: string;
	dataRoot: string;
	envFile: string;
	controllerKey: string;
	controllerPublicKey: string;
	pgUrl: string;
	sshdPort: number;
	backendOrigin: string;
	uviproc: Proc | null;
	browserCtx: BrowserContext | null;
	page: Page | null;
	ed25519Fingerprint: string;
	enrolledServerId: string;
	enrolledServerName: string;
	baselineTree: string[];
}

const live = {} as LiveStack;

function docker(...args: string[]): Promise<ExecResult> {
	if (args.some((arg) => /^mai-/.test(arg) && !FIXTURE_NAME_RE.test(arg) && arg !== SSHD_IMAGE)) {
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

test.describe('live full-stack guided onboarding (real uvicorn + postgres + openssh)', () => {
	test.describe.configure({ mode: 'serial', timeout: 240_000 });

	test.beforeAll(async ({ browser }) => {
		// Hook-scoped timeout (the fn+timeout hook signature no longer exists).
		test.setTimeout(300_000);
		const dockerCheck = await run('docker', ['info', '--format', '{{.ServerVersion}}']);
		test.skip(dockerCheck.code !== 0, 'docker unavailable: the live stack needs disposable postgres + openssh containers');

		// -- disposable scratch, controller key, resolver env file -------------
		fs.mkdirSync(SCRATCH, { recursive: true });
		live.scratch = SCRATCH;
		live.dataRoot = nodePath.join(SCRATCH, 'data-root');
		live.envFile = nodePath.join(SCRATCH, 'resolver.env');
		live.controllerKey = nodePath.join(SCRATCH, 'controller_key');
		fs.mkdirSync(live.dataRoot, { recursive: true });
		const keygen = await run('ssh-keygen', [
			'-q', '-t', 'ed25519', '-N', '', '-f', live.controllerKey, '-C', 'mai-so04-live-controller'
		]);
		// The empty passphrase is passed via argv (synthetic, disposable, 0600,
		// removed with the scratch tree); key material is never printed.
		expect(keygen.code, `controller keygen failed: ${keygen.stderr}`).toBe(0);
		const exported = await run('ssh-keygen', ['-y', '-f', live.controllerKey]);
		expect(exported.code, `ssh-keygen -y failed: ${exported.stderr}`).toBe(0);
		live.controllerPublicKey = exported.stdout.trim();
		// The env resolver file contains ONLY the fallback reference so the
		// default path is genuinely exercised end to end.
		fs.writeFileSync(live.envFile, `${FALLBACK_REF}='${live.controllerKey}'\n`, { mode: 0o600 });

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
		let pgReady = false;
		for (let attempt = 0; attempt < 60; attempt += 1) {
			const ready = await docker('exec', PG_CONTAINER, 'pg_isready', '-U', 'postgres', '-d', PG_DB);
			if (ready.code === 0 && ready.stdout.includes('accepting connections')) {
				pgReady = true;
				break;
			}
			await delay(500);
		}
		expect(pgReady, 'disposable postgres never became ready').toBe(true);
		// Migrate the disposable DB with the repository chain. The fixture-only
		// child env carries no ambient MAI_* values.
		const migrateEnv = { PATH: process.env.PATH!, HOME: process.env.HOME!, MAI_DATABASE_URL: live.pgUrl };
		const migrated = await run(
			backendPython(),
			['-m', 'alembic', '-c', 'alembic.ini', 'upgrade', 'head'],
			{ cwd: BACKEND_ROOT, env: migrateEnv, timeout: 180_000 }
		);
		expect(migrated.code, `alembic upgrade head failed: ${migrated.stderr.slice(-800)}`).toBe(0);

		// -- disposable Arch sshd with EMPTY root authorized_keys --------------
		const imageCheck = await docker('image', 'inspect', SSHD_IMAGE);
		if (imageCheck.code !== 0) {
			const context = nodePath.join(SCRATCH, 'build-context');
			fs.mkdirSync(context, { recursive: true });
			fs.writeFileSync(
				nodePath.join(context, 'Dockerfile'),
				'FROM archlinux:latest\nRUN pacman-key --init && pacman -Sy --noconfirm openssh sudo && ssh-keygen -A\n'
			);
			const build = await docker('build', '-q', '-t', SSHD_IMAGE, context);
			expect(build.code, `sshd image build failed: ${build.stderr.slice(-800)}`).toBe(0);
		}
		const sshdRun = await docker(
			'run', '--rm', '-d', '--name', SSHD_CONTAINER, '-p', '127.0.0.1::22', SSHD_IMAGE,
			'sh', '-c',
			// PerSourcePenalties (OpenSSH >= 9.8, default on) rate-limits the
			// CONTROLLER after the live negative controls' real auth failures —
			// enroll's immediate re-probe was dropped at kex ("Connection
			// closed by remote host", observed). This disposable loopback
			// fixture legitimately disables the internet-facing DoS guard;
			// production sshd configuration is untouched.
			'rm -f /etc/ssh/ssh_host_* && ssh-keygen -A && mkdir -p /var/empty /run/sshd '
			+ '&& exec /usr/bin/sshd -D -o PerSourcePenalties=no'
		);
		expect(sshdRun.code, `disposable sshd failed to start: ${sshdRun.stderr}`).toBe(0);
		const portInspect = await docker(
			'inspect', '--format',
			'{{(index (index .NetworkSettings.Ports "22/tcp") 0).HostPort}}', SSHD_CONTAINER
		);
		expect(portInspect.code, 'sshd port inspect failed').toBe(0);
		live.sshdPort = Number(portInspect.stdout.trim());
		expect(Number.isInteger(live.sshdPort) && live.sshdPort > 0).toBe(true);
		// STRONGEST FIXTURE: root's authorized_keys starts EMPTY. The limited
		// user (password-required sudo) is created without any authorized key.
		const emptyRoot = await docker(
			'exec', SSHD_CONTAINER, 'sh', '-c',
			'mkdir -p /root/.ssh && chmod 700 /root/.ssh && : > /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys'
		);
		expect(emptyRoot.code, `empty authorized_keys provisioning failed: ${emptyRoot.stderr}`).toBe(0);
		const limitedUser = await docker(
			'exec', SSHD_CONTAINER, 'sh', '-c',
			'useradd -m -s /bin/bash limited && echo \'limited ALL=(ALL) ALL\' >> /etc/sudoers'
		);
		expect(limitedUser.code, `limited user provisioning failed: ${limitedUser.stderr}`).toBe(0);
		// Wait until real ssh-keyscan sees host keys.
		let scannable = false;
		for (let attempt = 0; attempt < 60; attempt += 1) {
			const scan = await run('ssh-keyscan', ['-p', String(live.sshdPort), '-t', 'ed25519,rsa', '127.0.0.1'], { timeout: 30_000 });
			if (scan.code === 0 && scan.stdout.includes(' ssh-ed25519 ')) {
				scannable = true;
				break;
			}
			await delay(500);
		}
		expect(scannable, 'disposable sshd never became scannable').toBe(true);
		const scanNow = await run('ssh-keyscan', ['-p', String(live.sshdPort), '-t', 'ed25519', '127.0.0.1'], { timeout: 30_000 });
		const edLine = scanNow.stdout.split('\n').find((line: string) => line.includes(' ssh-ed25519 '));
		if (!edLine) throw new Error('keyscan produced no ed25519 host key');
		live.ed25519Fingerprint = await fingerprintOf(edLine.trim());

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
			// Deliberately NO MAI_SSH_CREDENTIAL_REF: the fallback reference
			// MAI_SSH_KEY_MAI must carry the whole flow.
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
				console.log(`[so04-live] uvicorn exited early with code ${code}; see ${uvicornLog}`);
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
		const ctx = await browser.newContext({ baseURL: ORIGIN, viewport: { width: 1280, height: 800 } });
		live.browserCtx = ctx;
		await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: ORIGIN });
		await ctx.route('**/api/**', async (route) => {
			const request = route.request();
			const target = new URL(request.url());
			const response = await route.fetch({ url: live.backendOrigin + target.pathname + target.search });
			await route.fulfill({ response });
		});
		live.page = await ctx.newPage();
	});

	test.afterAll(async () => {
		try {
			await live.page?.close().catch(() => undefined);
			await live.browserCtx?.close().catch(() => undefined);
			await killUvicorn();
			await removeContainer(SSHD_CONTAINER, 'sshd');
			await removeContainer(PG_CONTAINER, 'postgres');
			if (live.scratch) {
				fs.rmSync(live.scratch, { recursive: true, force: true });
				cleanupProof.push(`scratch ${live.scratch} exists after rm: ${fs.existsSync(live.scratch)}`);
			}
			const leftovers = await run('docker', ['ps', '-a', '--filter', 'name=mai-fxt-so04-live', '--format', '{{.Names}}']);
			cleanupProof.push(`remaining mai-fxt-so04-live containers: '${leftovers.stdout.trim() || '<none>'}'`);
			console.log(`\nSO04 LIVE CLEANUP PROOF:\n${cleanupProof.map((line) => `  - ${line}`).join('\n')}`);
		} catch (error) {
			console.log(`SO04 LIVE CLEANUP ERROR: ${String(error)}`);
			console.log(cleanupProof.join('\n'));
		}
	});

	const page = (): Page => {
		if (!live.page) throw new Error('live page not initialized');
		return live.page;
	};

	async function openEnrollment(): Promise<void> {
		await page().goto('/');
		await expect(page().getByTestId('desktop-enroll-toggle')).toBeVisible();
		await page().getByTestId('desktop-enroll-toggle').click();
		await expect(page().getByTestId('enroll-controller-public-key')).toBeVisible({ timeout: 30_000 });
	}

	async function fillTarget(user: string, privilege: 'none' | 'sudo' | 'root'): Promise<void> {
		// Scoped to the enrollment card: other open panels (e.g. the server
		// profile window) carry their own SSH host inputs.
		const form = page().getByTestId('enroll-server');
		await form.getByLabel('SSH host').fill('127.0.0.1');
		await form.getByLabel('SSH port').fill(String(live.sshdPort));
		await form.getByLabel('SSH user').fill(user);
		await form.getByLabel('Privilege mode').selectOption(privilege);
	}

	/** Runs discovery; if several candidates appear, selects the ed25519 one
	 * and runs the pinned test; if exactly one candidate appeared the
	 * component already continued into the pinned test itself. */
	async function guidedTestUntilAuthenticated(user: string, privilege: 'none' | 'sudo' | 'root'): Promise<void> {
		await fillTarget(user, privilege);
		await page().getByTestId('enroll-test-connection').click();
		const fieldset = page().getByTestId('enroll-host-key-candidates');
		// Either several candidates (explicit selection required) or the
		// single-candidate auto-continue already produced the result/error.
		await expect
			.poll(
				async () => {
					const resultVisible = await page().getByTestId('enroll-test-result').isVisible().catch(() => false);
					const errorVisible = await page().getByTestId('enroll-test-error').isVisible().catch(() => false);
					const candidatesVisible = await fieldset.isVisible().catch(() => false);
					return resultVisible || errorVisible || candidatesVisible;
				},
				{ timeout: 60_000 }
			)
			.toBe(true);
		if (await fieldset.isVisible().catch(() => false)) {
			await expect(page().getByTestId('enroll-test-notice')).toContainText('NOT yet an authenticated connection');
			const edRadio = fieldset.locator('label', { hasText: 'ssh-ed25519' }).getByRole('radio');
			await expect(edRadio).toHaveCount(1);
			await edRadio.check();
			await expect(page().getByTestId('enroll-test-connection')).toHaveText('Test selected host key');
			await page().getByTestId('enroll-test-connection').click();
		}
	}

	test('real login through the production UI and boundary', async () => {
		await page().goto('/login');
		await expect(page().getByTestId('login-form')).toBeVisible();
		await page().locator('input[type="password"]').fill(ADMIN_PASSWORD);
		await page().getByTestId('login-submit').click();
		await expect(page()).toHaveURL(/\/$/);
		await expect(page().getByTestId('component-host')).toBeVisible();
		await expect(page().getByTestId('desktop-enroll-toggle')).toBeVisible();
		// Baseline durable state right after login (before any onboarding).
		live.baselineTree = walk(live.dataRoot);
		const servers = await apiFetch('GET', '/api/v1/servers');
		expect(servers.status).toBe(200);
		expect((await servers.json()).servers).toEqual([]);
	});

	test('controller key is exported by the real backend, copied EXACTLY, and no credential/host-key input exists', async () => {
		await openEnrollment();
		const keyElement = page().getByTestId('enroll-controller-public-key');
		await expect(keyElement).toContainText('ssh-ed25519');
		const shownKey = (await keyElement.textContent())?.trim() ?? '';
		// Cross-check 1: identical to the real ssh-keygen -y of the fixture key.
		expect(shownKey).toBe(live.controllerPublicKey);
		// Cross-check 2: the backend response body itself (relayed read).
		const response = await apiFetch('GET', '/api/v1/servers/onboarding/controller-key');
		expect(response.status).toBe(200);
		const body = await response.json();
		expect(body.public_key).toBe(shownKey);
		expect(body.key_type).toBe('ssh-ed25519');
		expect(String(body.fingerprint)).toMatch(/^SHA256:/);
		expect(body.private_key).toBeUndefined();
		// Copy action writes EXACTLY the public key into the real clipboard.
		await page().getByTestId('enroll-copy-public-key').click();
		await expect(page().getByTestId('enroll-copy-status')).toHaveText('Copied.');
		const clipboard: string = await page().evaluate(() => navigator.clipboard.readText());
		expect(clipboard.trim()).toBe(shownKey);
		live.controllerPublicKey = clipboard.trim(); // the EXACT copied key is what gets authorized later
		// The guided flow shows no credential reference and no host-key input.
		const form = page().getByTestId('enroll-server');
		// No legacy inputs: the removed raw-form labels are gone entirely.
		await expect(form.getByText('Credential reference')).toHaveCount(0);
		await expect(form.getByText('Host key (SHA256 fingerprint)')).toHaveCount(0);
		await expect(form.getByLabel(/credential/i)).toHaveCount(0);
		// The ONLY control whose label mentions a host key is the explicit
		// trust checkbox — nothing asks the user to type a fingerprint.
		const hostKeyLabeled = form.getByLabel(/host key/i);
		await expect(hostKeyLabeled).toHaveCount(1);
		await expect(hostKeyLabeled).toHaveRole('checkbox');
		expect(await form.textContent()).not.toContain(FALLBACK_REF);
	});

	test('EMPTY authorized_keys: real discovery succeeds, pinned test honestly fails authentication, nothing is written', async () => {
		await guidedTestUntilAuthenticated('root', 'sudo');
		// The pinned test must fail AUTH (empty authorized_keys) — a real
		// BatchMode refusal from the real sshd, mapped to auth_failed.
		const error = page().getByTestId('enroll-test-error');
		await expect(error).toBeVisible({ timeout: 60_000 });
		await expect(error).toContainText('auth_failed');
		await expect(page().getByTestId('enroll-test-result')).toHaveCount(0);
		await expect(page().getByTestId('enroll-host-key-confirmation').getByRole('checkbox')).toBeDisabled();
		// Discovered candidates are the target's REAL host keys: the ed25519
		// fingerprint the UI offered equals the independently scanned one.
		const shownFingerprints = await page()
			.getByTestId('enroll-host-key-candidates')
			.locator('code')
			.allTextContents();
		expect(shownFingerprints.map((fp) => fp.trim())).toContain(live.ed25519Fingerprint);
		// Zero durable writes: canonical data root byte-tree and server list.
		expect(walk(live.dataRoot)).toEqual(live.baselineTree);
		const servers = await apiFetch('GET', '/api/v1/servers');
		expect((await servers.json()).servers).toEqual([]);
	});

	test('negative enroll controls at the real boundary write nothing (unconfirmed trust, changed fingerprint)', async () => {
		const enrollBody = (overrides: Record<string, unknown>) => ({
			name: 'so04-negative',
			prompt_revision: 1,
			prompt_text: 'negative-control prompt',
			environment: 'linux_ssh',
			privilege: 'sudo',
			capabilities: [],
			ssh: { host: '127.0.0.1', port: live.sshdPort, user: 'root' },
			host_key: live.ed25519Fingerprint,
			host_key_confirmed: true,
			timeout_seconds: 15,
			...overrides
		});
		// (a) Unconfirmed trust: rejected before any effect.
		const unconfirmed = await apiFetch('POST', '/api/v1/servers/onboarding/enroll', enrollBody({ host_key_confirmed: false }));
		expect(unconfirmed.status).toBe(422);
		expect((await unconfirmed.json()).error.code).toBe('host_key_confirmation_required');
		// (b) Changed fingerprint between test and enroll: the presented key at
		// enroll time is not the submitted one -> refused, no writes.
		const changed = nodeCrypto.createHash('sha256').update('so04-never-presented-host-key').digest('base64').replace(/=+$/, '');
		const stale = await apiFetch('POST', '/api/v1/servers/onboarding/enroll', enrollBody({ host_key: `SHA256:${changed}` }));
		expect(stale.status).toBe(409);
		expect((await stale.json()).error.code).toBe('host_key_mismatch');
		expect(walk(live.dataRoot)).toEqual(live.baselineTree);
		const servers = await apiFetch('GET', '/api/v1/servers');
		expect((await servers.json()).servers).toEqual([]);
	});

	test('authorizing the EXACT copied public key enables the full guided flow: test, trust, enroll', async () => {
		// Fixture-only external step (the documented administrator action):
		// add the EXACT copied public key to the target user's authorized_keys.
		const install = await runWithInput(
			'docker', ['exec', '-i', SSHD_CONTAINER, 'sh', '-c',
				'mkdir -p /root/.ssh && chmod 700 /root/.ssh && cat > /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys'],
			`${live.controllerPublicKey}\n`
		);
		expect(install.code, `authorizing the copied key failed: ${install.stderr}`).toBe(0);

		live.enrolledServerName = 'so04-live-target';
		await guidedTestUntilAuthenticated('root', 'sudo');
		const result = page().getByTestId('enroll-test-result');
		await expect(result).toBeVisible({ timeout: 60_000 });
		await expect(result).toContainText('Authenticated');
		await expect(result).toContainText(live.ed25519Fingerprint);
		await expect(result).toContainText('ok');
		await expect(result).toContainText('arch');
		await expect(result).toContainText(/Linux.*arch/);

		const trust = page().getByTestId('enroll-host-key-confirmation').getByRole('checkbox');
		await expect(page().getByTestId('enroll-submit')).toBeDisabled();
		await trust.check();
		await page().getByLabel('Display name').fill(live.enrolledServerName);
		await page().getByTestId('enroll-prompt-text').fill('so04 live enrollment prompt');
		await expect(page().getByTestId('enroll-submit')).toBeEnabled();
		await page().getByTestId('enroll-submit').click();

		await expect(page().getByTestId('enroll-server')).toHaveCount(0);
		await expect(page().getByTestId('desktop-status')).toContainText(`Server enrolled: ${live.enrolledServerName}.`);
		const servers = await apiFetch('GET', '/api/v1/servers');
		const list = (await servers.json()).servers as Array<Record<string, unknown>>;
		expect(list).toHaveLength(1);
		expect(list[0].name).toBe(live.enrolledServerName);
		live.enrolledServerId = String(list[0].server_id);
	});

	test('read-back: canonical pin matches the selected fingerprint, profile carries it, panel shows it, pinned probe works', async () => {
		// Canonical single pin at servers/<id>/known_hosts, one managed line
		// whose fingerprint equals the selected one.
		const pinPath = nodePath.join(live.dataRoot, 'servers', live.enrolledServerId, 'known_hosts');
		expect(fs.existsSync(pinPath), `canonical pin missing at ${pinPath}`).toBe(true);
		const pinLines = fs.readFileSync(pinPath, 'utf-8').split('\n').filter((line: string) => line.trim() !== '');
		expect(pinLines).toHaveLength(1);
		expect(pinLines[0].startsWith(`[127.0.0.1]:${live.sshdPort} `)).toBe(true);
		expect(await fingerprintOf(pinLines[0].split(/\s+/).slice(1).join(' '))).toBe(live.ed25519Fingerprint);
		expect(fs.existsSync(nodePath.join(live.dataRoot, 'known_hosts'))).toBe(false);

		// Profile read-back through the real API: pinned fingerprint, fallback
		// credential reference (server-side default), no credential VALUE.
		const profileResponse = await apiFetch('GET', `/api/v1/servers/${live.enrolledServerId}`);
		expect(profileResponse.status).toBe(200);
		const profile = await profileResponse.json();
		expect(profile.ssh.host_key).toBe(live.ed25519Fingerprint);
		expect(profile.ssh.credential_ref).toBe(FALLBACK_REF);
		expect(profile.ssh.host).toBe('127.0.0.1');
		expect(profile.ssh.port).toBe(live.sshdPort);
		expect(profile.privilege).toBe('sudo');
		expect(JSON.stringify(profile)).not.toContain('PRIVATE');
		expect(JSON.stringify(profile)).not.toContain(live.controllerKey);

		// UI read-back: the server profile panel renders the stored pin.
		await expect(page().getByTestId('server-select')).toContainText(live.enrolledServerName, { ignoreCase: true });
		await page().getByTestId('open-server-profile').click();
		const panel = page().getByTestId('server-profile-root');
		await expect(panel).toBeVisible({ timeout: 30_000 });
		await expect(panel.getByTestId('profile-name')).toHaveText(live.enrolledServerName);
		// Read-only input showing the stored pin (rotation deferred by design).
		await expect(panel.getByTestId('pinned-host-key')).toHaveValue(live.ed25519Fingerprint);

		// The EXISTING pinned probe (SSH helper in mai.execution.ssh — NOT an
		// HTTP route) runs against the PERSISTED identity/store with no second
		// trust step: same data root, same env file, canonical consumer path.
		const probeScript = [
			'import json, os, uuid',
			'import yaml',
			'from mai.api.terminal import _known_hosts_path',
			'from mai.contracts.server import SshIdentity',
			'from mai.credentials.env import EnvResolver',
			'from mai.execution.ssh import probe_environment',
			'from mai.profiles.files import ProfileFileStore',
			'store = ProfileFileStore(os.environ["SO04_DATA_ROOT"])',
			'sid = uuid.UUID(os.environ["SO04_SERVER_ID"])',
			'ssh = yaml.safe_load(store.read(sid).text)["ssh"]',
			'key_path = EnvResolver(os.environ["SO04_ENV_FILE"]).resolve(ssh["credential_ref"]).value',
			'identity = SshIdentity(host=ssh["host"], port=ssh["port"], user=ssh["user"], host_key=ssh["host_key"], credential_ref=ssh["credential_ref"])',
			'observed = probe_environment(identity, key_path=key_path, known_hosts_path=_known_hosts_path(store, sid))',
			'print(json.dumps({"kernel_name": observed.kernel_name, "os_id": observed.os_id, "hostname": observed.hostname, "boot_id_present": bool(observed.boot_id)}))'
		].join('\n');
		const probe = await run(
			backendPython(),
			['-c', probeScript],
			{
				cwd: BACKEND_ROOT,
				timeout: 120_000,
				env: {
					PATH: process.env.PATH!,
					HOME: process.env.HOME!,
					SO04_DATA_ROOT: live.dataRoot,
					SO04_ENV_FILE: live.envFile,
					SO04_SERVER_ID: live.enrolledServerId
				}
			}
		);
		expect(probe.code, `pinned probe failed: ${probe.stderr.slice(-800)}`).toBe(0);
		const observed = JSON.parse(probe.stdout.trim().split('\n').pop() ?? '{}');
		expect(observed.kernel_name).toBe('Linux');
		expect(observed.os_id).toBe('arch');
		expect(observed.boot_id_present).toBe(true);
		expect(observed.hostname).toBeTruthy();
	});

	test('privilege failure (password-required sudo) writes nothing', async () => {
		// Fixture-only: authorize the same controller key for the limited user
		// whose sudoers entry requires a password (no NOPASSWD).
		const install = await runWithInput(
			'docker', ['exec', '-i', SSHD_CONTAINER, 'sh', '-c',
				'mkdir -p /home/limited/.ssh && chmod 700 /home/limited/.ssh && cat > /home/limited/.ssh/authorized_keys && chmod 600 /home/limited/.ssh/authorized_keys && chown -R limited:limited /home/limited/.ssh'],
			`${live.controllerPublicKey}\n`
		);
		expect(install.code, `authorizing the limited user key failed: ${install.stderr}`).toBe(0);

		const treeAfterEnroll = walk(live.dataRoot);
		await openEnrollment();
		await guidedTestUntilAuthenticated('limited', 'sudo');
		const error = page().getByTestId('enroll-test-error');
		await expect(error).toBeVisible({ timeout: 60_000 });
		await expect(error).toContainText('privilege_unavailable');
		await expect(page().getByTestId('enroll-test-result')).toHaveCount(0);
		await expect(page().getByTestId('enroll-host-key-confirmation').getByRole('checkbox')).toBeDisabled();
		// No durable writes beyond the already-enrolled server.
		expect(walk(live.dataRoot)).toEqual(treeAfterEnroll);
		const servers = await apiFetch('GET', '/api/v1/servers');
		const list = (await servers.json()).servers as unknown[];
		expect(list).toHaveLength(1);
	});
});
