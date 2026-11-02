import { expect, test, type BrowserContext, type Page } from '@playwright/test';

/**
 * activity-ui v2 LIVE proof — the runtime-v2 operations surface in real
 * Chromium against the REAL backend (rv_activity-ui.md; runtime-v2.md §5-§11).
 *
 * Real stack, started by this fixture and torn down with proof:
 *  - REAL uvicorn ASGI (`mai.api.app:create_app --factory`) on a loopback
 *    ephemeral port from a fixture-only environment (MAI_DATABASE_URL /
 *    MAI_DATA_ROOT / MAI_ENV_FILE / MAI_PUBLIC_ORIGINS).
 *  - The coordinator-assigned disposable PostgreSQL 18 container
 *    (`mai-rv-pg18`, 127.0.0.1:35076): this fixture creates its OWN database,
 *    migrates it with the repository Alembic chain, and drops it in afterAll.
 *  - REAL worker subprocesses (`python -m mai.jobs.worker`) composing the
 *    real agent-run/v2 executors, the real dispatcher/policy/approval
 *    machinery and the ONE delegation engine (`agent.delegate` with the v2
 *    payload), with a deterministic mission-branched FunctionModel fake
 *    (C06 rule 6) and a journaled disposable target whose effect markers
 *    make exactly-once observable (the proven rv conversation-api fixture
 *    pattern; nothing under test is mocked).
 *  - Production-build Svelte UI served by the Playwright webServer
 *    (vite build + vite preview, port 4173) in real Chromium.
 *
 * The ONLY route interception is the same-origin `/api/**` relay to the real
 * uvicorn (`route.fetch`) — real cookies, real CSRF, real Origin, real
 * backend bodies. No `route.fulfill` synthetic API responses exist here.
 *
 * Scenarios (rv_activity-ui.md "Real runtime testing"):
 *  1. live v2 submission via New Chat with NO objective control (the
 *     direct-agent default: the admission starts the episode) → header
 *     metrics + expandable timeline details + receipts/verification view;
 *     redaction honesty;
 *  2. bounded event pagination ("Load more" appends the next page without
 *     duplicates) + replay/reconnect dedup by the sequence watermark;
 *  3. multi-server delegation: real v2 delegate parent over two scoped
 *     servers → tree `server_ids`, aggregated child RECEIPTS, target/
 *     resource filter;
 *  4. approval-bound ask-mode run (payload-bound decision wakes it) and the
 *     crash window: `outcome_unknown` + `reconciling` where blind resume is
 *     disabled and operator classification (`confirm_applied`) resolves and
 *     wakes the run;
 *  5. legacy history renders truthfully (server-derived `is_legacy` marker;
 *     no fake v2 journal);
 *  6. 360×640 + `prefers-reduced-motion: reduce`: keyboard expand/collapse
 *     of timeline details and controls, no horizontal overflow, streamed
 *     events never steal focus; desktop + mobile screenshots preserved as
 *     campaign evidence.
 */

/** Ambient Node surface (same convention as server-enrollment-live.spec.ts). */
declare const process: {
	env: Record<string, string | undefined>;
	getBuiltinModule(id: string): unknown;
};

interface Proc {
	exitCode: number | null;
	kill(signal?: string): boolean;
	once(event: 'exit', cb: () => void): void;
	on(event: 'exit', cb: (code: number | null) => void): void;
	on(event: 'error', cb: (error: Error) => void): void;
	stdout: { on(event: 'data', cb: (chunk: unknown) => void): void };
	stderr: { on(event: 'data', cb: (chunk: unknown) => void): void };
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
	on(event: 'connect', cb: (error?: unknown) => void): void;
	on(event: 'error', cb: (error: Error) => void): void;
	on(event: 'timeout', cb: () => void): void;
	destroy(): void;
}

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
	writeFileSync(path: string, data: string, opts?: { mode?: number }): void;
	rmSync(path: string, opts: { recursive: boolean; force: boolean }): void;
	openSync(path: string, flags: string): number;
	closeSync(fd: number): void;
};
const net = process.getBuiltinModule('node:net') as {
	createServer(): ServerLike;
	connect(opts: { port: number; host: string }): SocketLike;
};
const nodeCrypto = process.getBuiltinModule('node:crypto') as {
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
const SCRATCH = `/tmp/mai-fxt-acui-live/${RUN_ID}`;
const PG_CONTAINER = 'mai-rv-pg18';
const PG_HOST_PORT = '35076';
const PG_SUPER_USER = 'mai_test';
const DB_NAME = `mai_acui_${RUN_ID}`;
const PG_URL = `postgresql://${PG_SUPER_USER}:mai_test_pw@127.0.0.1:${PG_HOST_PORT}/${DB_NAME}`;
const BACKEND_ROOT = nodePath.resolve(
	nodeUrl.fileURLToPath(new URL('.', import.meta.url)),
	'..',
	'..',
	'backend'
);
const EVIDENCE_DIR =
	'/home/megastruktur/projects/dev/machina/plans/runtime-v2/rv_evidence/activity-ui';
const ADMIN_PASSWORD = `acui-live-${nodeCrypto.randomUUID()}`; // synthetic, disposable, never printed

/**
 * Secret-SHAPED fixture canary (synthetic, NOT a secret): the fixture target
 * puts it inside every tool output so the REDACTION honesty of the rendered
 * timeline is observable — it must NEVER appear in the DOM (§5.3).
 */
const FIXTURE_SECRET_TOKEN = 'sk-rvfixtureredaction0123456789abcdef';

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

function delay(ms: number): Promise<void> {
	const { promise, resolve } = Promise.withResolvers<void>();
	setTimeout(resolve, ms);
	return promise;
}

function run(
	cmd: string,
	args: string[],
	opts: { timeout?: number; cwd?: string; env?: Record<string, string | undefined> } = {}
): Promise<{ code: number; stdout: string; stderr: string }> {
	const { promise, resolve } = Promise.withResolvers<{
		code: number;
		stdout: string;
		stderr: string;
	}>();
	cp.execFile(
		cmd,
		args,
		{ timeout: opts.timeout ?? 60_000, cwd: opts.cwd, env: opts.env ?? process.env },
		(error, stdout, stderr) => {
			const code = error ? (typeof error.code === 'number' ? error.code : 1) : 0;
			resolve({ code, stdout: String(stdout), stderr: String(stderr) });
		}
	);
	return promise;
}

async function freePort(): Promise<number> {
	const { promise, resolve, reject } = Promise.withResolvers<number>();
	const server = net.createServer();
	server.unref();
	server.on('error', reject);
	server.listen(0, '127.0.0.1', () => {
		const address = server.address();
		if (typeof address === 'object' && address !== null && 'port' in address) {
			resolve(Number(address.port));
		} else {
			reject(new Error('no ephemeral port'));
		}
	});
	const port = await promise;
	const { promise: closed, resolve: resolveClosed } = Promise.withResolvers<void>();
	server.close(() => resolveClosed());
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

// ---------------------------------------------------------------------------
// the worker-side composition (real executors; mission-branched fake model)
// ---------------------------------------------------------------------------

const HANDLER_MODULE_SOURCE = `
"""Worker-side composition for the activity-ui live fixture: real
dispatcher/policy/approval machinery, the ONE delegation engine (v2 payload),
journaled disposable target effects (gate behavior for the crash window) and
a deterministic mission-branched FunctionModel fake. Registers agent.run_v2
and agent.delegate through the ONE engine seams."""

import asyncio
import json
import os
import re
import time
import uuid

from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import mai.agents.run as agent_run
from mai.agents.budgets import BudgetLedger
from mai.agents.model_store import ModelConfigStore
from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
from mai.credentials.env import EnvResolver
from mai.jobs.engine import EngineClient
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import Policy

READ_CAP = "fixture.read"
WRITE_CAP = "fixture.write"


def _descriptor(capability_id, effect_class):
    return parse_descriptor(
        {
            "identifier": capability_id,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["marker"],
                "properties": {"marker": {"type": "string", "minLength": 1}},
            },
            "output_schema": {
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
            "execution_environments": ["linux_ssh"],
            "effect_class": effect_class,
            "resource_scope": [] if effect_class == "read" else ["target.exec"],
            "required_grants": [],
        },
        source="<acui-descriptor>",
    )


class FixtureExecutor:
    """Journaled disposable target; behaviors keyed 'capability:marker' then
    'capability': ok | gate. Every output carries a fake secret-shaped token
    plus oversized padding so redaction/truncation is observable."""

    def __init__(self):
        self.journal = os.environ["MAI_FX_JOURNAL"]
        self.gate_dir = os.environ["MAI_FX_GATE_DIR"]
        self.behaviors = json.loads(os.environ.get("MAI_FX_BEHAVIOR", "{}"))

    def _mark(self, marker):
        with open(self.journal, "a", encoding="utf-8") as handle:
            handle.write(marker + "\\n")

    def _behavior(self, capability_id, marker):
        keyed = self.behaviors.get(f"{capability_id}:{marker}")
        if keyed is not None:
            return keyed
        return self.behaviors.get(capability_id, "ok")

    async def __call__(self, call):
        capability_id = call.descriptor.identifier
        marker = call.payload.get("marker", "?")
        behavior = self._behavior(capability_id, str(marker))
        self._mark(f"begin:{capability_id}:{marker}")
        if behavior == "gate":
            gate = os.path.join(self.gate_dir, f"gate-{capability_id}-{marker}")
            deadline = time.time() + 300
            while not os.path.exists(gate):
                if time.time() > deadline:
                    raise RuntimeError("gate timeout")
                time.sleep(0.1)
        self._mark(f"effect:{capability_id}:{marker}")
        document = {
            "status": "succeeded",
            "exit_code": 0,
            "stdout": (
                "leak token " + os.environ.get("MAI_FX_CANARY", "sk-none") + " end "
                + "x" * 5000
            ),
            "stderr": "",
        }
        return ExecutorResult(ok=True, output=json.dumps(document, sort_keys=True))


def _script_model():
    scripts = json.loads(os.environ["MAI_FX_MODEL_SCRIPT"])
    journal = os.environ["MAI_FX_JOURNAL"]

    def _mark(line):
        with open(journal, "a", encoding="utf-8") as handle:
            handle.write(line + "\\n")

    def _mission_of(messages):
        for message in reversed(messages):
            if message.kind == "request":
                for part in message.parts:
                    if getattr(part, "part_kind", "") == "user-prompt":
                        text = str(getattr(part, "content", ""))
                        found = re.search(r"mission=([a-z-]+)", text)
                        if found:
                            return found.group(1)
        return "journey"

    def _entry_payload(entry):
        if "tool_call" in entry:
            call = entry["tool_call"]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=call["name"],
                        args=call.get("args", {"marker": "m1"}),
                        tool_call_id=call.get("id", "c1"),
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content=entry.get("text", "done"))])

    def _model_fn(messages, info):
        _mark("model_call")
        script = scripts[_mission_of(messages)]
        # Replay-safe position: derived from the DURABLE conversation length.
        index = (len(messages) - 1) // 2
        if index >= len(script):
            raise AssertionError("model script exhausted for the mission")
        return _entry_payload(script[index])

    async def _stream_fn(messages, info):
        response = await asyncio.to_thread(_model_fn, messages, info)
        for index, part in enumerate(response.parts):
            if isinstance(part, ToolCallPart):
                yield {
                    index: DeltaToolCall(
                        name=part.tool_name,
                        json_args=part.args_as_json_str(),
                        tool_call_id=part.tool_call_id,
                    )
                }
            elif isinstance(part, TextPart):
                yield part.content

    return FunctionModel(_model_fn, stream_function=_stream_fn)


def _build():
    url = os.environ["MAI_DATABASE_URL"]
    app_engine = create_async_engine(url, poolclass=NullPool)
    sessions = async_sessionmaker(app_engine, expire_on_commit=False)
    with open(os.environ["MAI_FX_GRANTS"], encoding="utf-8") as handle:
        grants_doc = json.load(handle)
    with open(os.environ["MAI_FX_POLICY"], encoding="utf-8") as handle:
        policy_doc = json.load(handle)
    grants = {
        entry["capability_id"]: frozenset(uuid.UUID(v) for v in entry["server_ids"])
        for entry in grants_doc
    }
    registry = CapabilityRegistry()
    registry.register(_descriptor(READ_CAP, "read"))
    registry.register(_descriptor(WRITE_CAP, "mutate"))
    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(sessions),
        grants=lambda: grants,
        policy=lambda: Policy(
            revision=int(policy_doc["revision"]),
            denied_capabilities=frozenset(policy_doc.get("denied", [])),
        ),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(os.environ["MAI_FX_ENV"]),
        executor=FixtureExecutor(),
    )
    model_store = ModelConfigStore(os.environ["MAI_FX_MODEL_ROOT"])
    fake_model = _script_model()
    agent_executor = agent_run.AgentRunExecutor(
        sessions=sessions,
        dispatcher=dispatcher,
        registry=registry,
        model_store=model_store,
        data_root=os.environ["MAI_FX_DATA_ROOT"],
        locks=ResourceLockService(url),
        resolver=EnvResolver(os.environ["MAI_FX_ENV"]),
        model_builder=lambda **kwargs: fake_model,
    )
    agent_run.configure_agent_run(agent_executor)
    from mai.agents import delegate as agent_delegate

    agent_delegate.configure_delegation(
        agent_delegate.DelegationExecutor(
            sessions=sessions,
            executor=agent_executor,
            client=EngineClient(url),
            ledger=BudgetLedger(sessions),
        )
    )
    # The v2 handler and the delegation handler register through the ONE
    # engine seam at import and drive the SAME composed executor.
    import mai.agents.run_v2  # noqa: F401
    import mai.agents.delegate  # noqa: F401


_build()
`;

/** Seeds the API process's own model store (connection + `default` profile
 * + selection) — the deterministic choice for the live profile picker. */
const SEED_API_MODEL_STORE_SOURCE = `
"""Seed the API data root's model store only (no server profiles here:
enrollment already wrote them)."""

import json
import sys

from mai.agents.model_store import ModelConfigStore

model_root = sys.argv[1]
store = ModelConfigStore(model_root)
connection = {
    "schema_version": 1,
    "name": "gateway",
    "base_url": "http://127.0.0.1:9/v1",
    "timeout_seconds": 5,
    "capabilities": {"tool_calls": True},
}
store.write_connection("gateway", json.dumps(connection) + "\\n", expected_revision=0)
profile = {
    "schema_version": 1,
    "name": "default",
    "connection": "gateway",
    "model": "fixture-model",
    "max_tokens": 256,
}
store.write_profile("default", json.dumps(profile) + "\\n", expected_revision=0)
store.set_selection("gateway", "default", expected_revision=0)
print("api model store seeded")
`;

const SEED_LEGACY_SOURCE = `
"""Seed ONE historical legacy conversation through the internal store
seams (pre-default runtime) and enqueue it for the frozen agent.run
worker. The direct-agent default means the canonical create route no
longer produces legacy material — fixtures seed history the honest way."""

import asyncio
import hashlib
import sys
import uuid


async def main() -> int:
    url, run_id, server_id, message = sys.argv[1:5]
    from datetime import datetime, timezone

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from mai.agents import run as agent_run
    from mai.agents.conversation_store import (
        MessageSourceKind,
        aattach_run,
        acreate_conversation,
        asubmit_turn,
    )
    from mai.api.chat import (
        _canonical,
        _modern_turn_document,
        _record_chat_submission,
    )
    from mai.api.jobs import _record_submit_intent
    from mai.jobs.engine import EngineClient
    from mai.policy.evaluate import ExecutionContext, PolicyMode

    key = uuid.UUID(run_id)
    server = uuid.UUID(server_id)
    engine = create_async_engine(url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await acreate_conversation(sessions, server_id=server, conversation_id=key)
        turn = await asubmit_turn(
            sessions,
            conversation_id=key,
            server_id=server,
            client_request_id=key,
            message=message,
            source_kind=MessageSourceKind.API,
        )
        now = datetime.now(timezone.utc)
        context = ExecutionContext(
            principal="hash:acui-legacy-fixture",
            server_id=server,
            run_id=key,
            parent_scope=None,
            profile_revision="1",
            prompt_revision="1",
            credential_references=(),
            allowed_capabilities=["fixture.read"],
            mode=PolicyMode("delegated"),
        )
        document = _modern_turn_document(
            run_id=run_id,
            server_id=server_id,
            context=context,
            profile="default",
            task=turn.message.content,
        )
        agent_run.parse_agent_run_intent(_canonical(document))
        payload_json = _canonical(document)
        digest = hashlib.sha256(payload_json.encode("ascii")).hexdigest()
        async with sessions() as session, session.begin():
            await _record_submit_intent(
                session,
                run_id=run_id,
                server_id=server_id,
                profile_revision=1,
                prompt_revision=1,
                model_profile="default",
                requested_grants=["fixture.read"],
                digest=digest,
                label="default",
                now=now,
            )
            await _record_chat_submission(
                session,
                run_id=run_id,
                digest=digest,
                handler="agent.run",
                attachments=[],
                child_count=0,
                service_tags=[],
                now=now,
                conversation_id=key,
                message_id=turn.message.message_id,
            )
        await aattach_run(
            sessions, conversation_id=key, server_id=server, run_id=key
        )
    finally:
        await engine.dispose()
    client = EngineClient(url)
    try:
        await asyncio.to_thread(client.enqueue_run, run_id, "agent.run", payload_json)
    finally:
        client.close()
    print(run_id)
    return 0


raise SystemExit(asyncio.run(main()))
`;

/** Seeds one v2 delegating parent onto an existing objective through the
 * real store seams (job_runs row + objective binding + ONE engine enqueue). */
const SEED_DELEGATE_SOURCE = `
"""Seed one v2 delegating parent (payload version 2) onto an objective."""

import asyncio
import json
import sys
import uuid


async def main() -> int:
    url, objective_id, parent_run_id, server_a, server_b = sys.argv[1:6]
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    import mai.agents.run as agent_run
    from mai.agents import delegate as agent_delegate
    from mai.agents.objective_store import abind_run
    from mai.contracts.objectives import ActorKind
    from mai.jobs.engine import EngineClient
    from mai.policy.evaluate import ExecutionContext, PolicyMode

    engine = create_async_engine(url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    context = ExecutionContext(
        principal="hash:acui-fixture-principal",
        server_id=uuid.UUID(server_a),
        run_id=uuid.UUID(parent_run_id),
        parent_scope=None,
        profile_revision="profile-1",
        prompt_revision="prompt-1",
        credential_references=(),
        allowed_capabilities=("fixture.read",),
        mode=PolicyMode("delegated"),
    )
    run_doc = {
        "v": agent_run.RUN_PAYLOAD_VERSION,
        "run_id": parent_run_id,
        "server_id": server_a,
        "context": context.to_json(),
        "role": "operator assistant",
        "task": "Delegate the scoped reads across the objective servers.",
    }
    document = {
        "v": agent_delegate.DELEGATION_PAYLOAD_VERSION_V2,
        "run": run_doc,
        "children": [
            {
                "task": "mission=child aspect-alpha scoped read",
                "server_ids": [server_b],
                "capabilities": ["fixture.read"],
            },
            {
                "task": "mission=child aspect-beta scoped read",
                "server_ids": [server_a],
                "capabilities": ["fixture.read"],
            },
        ],
        "children_wait_seconds": 120,
        "children_poll_seconds": 0.5,
    }
    agent_delegate.parse_delegation_intent_v2(
        json.dumps(document, sort_keys=True, separators=(",", ":"))
    )
    from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID, SYSTEM_ASSIGNMENT_SOURCE

    async with sessions() as session:
        await session.execute(
            text(
                """
                INSERT INTO job_runs
                    (run_id, server_id, depth, profile_revision,
                     prompt_revision, model_profile, requested_grants,
                     submitted_at, updated_at)
                VALUES
                    (:run_id, :server_id, 0, 3, 7, 'default',
                     CAST(:requested_grants AS jsonb), now(), now())
                """
            ),
            {
                "run_id": uuid.UUID(parent_run_id),
                "server_id": uuid.UUID(server_a),
                "requested_grants": json.dumps(["fixture.read"]),
            },
        )
        # The 0011 mandatory-classification invariant: every job run commits
        # with at least one tag; the seeded parent takes the reserved default.
        await session.execute(
            text(
                """
                INSERT INTO job_tags
                    (job_id, tag_id, assigned_by, assignment_source)
                VALUES (CAST(:rid AS uuid), CAST(:tag AS uuid), '', :source)
                """
            ),
            {
                "rid": parent_run_id,
                "tag": RESERVED_UNCLASSIFIED_TAG_ID,
                "source": SYSTEM_ASSIGNMENT_SOURCE,
            },
        )
        await session.commit()
    await abind_run(
        sessions,
        uuid.UUID(objective_id),
        server_id=uuid.UUID(server_a),
        run_id=uuid.UUID(parent_run_id),
        initiating_server_id=uuid.UUID(server_a),
        actor_kind=ActorKind.AGENT,
        handler="agent.run_v2",
    )
    await engine.dispose()
    client = EngineClient(url)
    client.enqueue_run(
        parent_run_id,
        "agent.delegate",
        json.dumps(document, sort_keys=True, separators=(",", ":")),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
`;

/** Seeds the controller-side model store (profile 'default' resolves the
 * canonical submit path's C06 gate; never dialed — the worker fakes it) and
 * the WORKER-side data root (canonical profile files + prompts — the v2
 * context assembly reads them through ProfileFileStore). */
const SEED_ROOTS_SOURCE = `
"""Seed the model store and the worker data root for the fixture servers."""

import json
import os
import sys
import uuid

from mai.agents.model_store import ModelConfigStore
from mai.profiles.files import ProfileFileStore

model_root = sys.argv[1]
worker_root = sys.argv[2]
server_a = uuid.UUID(sys.argv[3])
server_b = uuid.UUID(sys.argv[4])

store = ModelConfigStore(model_root)
connection = {
    "schema_version": 1,
    "name": "gateway",
    "base_url": "http://127.0.0.1:9/v1",
    "timeout_seconds": 5,
    "capabilities": {"tool_calls": True},
}
store.write_connection("gateway", json.dumps(connection) + "\\n", expected_revision=0)
profile = {
    "schema_version": 1,
    "name": "default",
    "connection": "gateway",
    "model": "fixture-model",
    "max_tokens": 256,
}
store.write_profile("default", json.dumps(profile) + "\\n", expected_revision=0)
store.set_selection("gateway", "default", expected_revision=0)

profile_store = ProfileFileStore(worker_root)
for server in (server_a, server_b):
    profile_text = (
        'schema_version: 1\\n'
        f'server_id: "{server}"\\n'
        'name: "acui-fixture"\\n'
        'prompt_revision: 3\\n'
        'environment: linux_ssh\\n'
        'ssh:\\n'
        '  host: "198.51.100.10"\\n'
        '  port: 22\\n'
        '  user: "deploy"\\n'
        '  host_key: "SHA256:6VhOyUC6fvXW0aQ9WNe7Kx3nZZf+zxgupEWLoZXoTMU"\\n'
        '  credential_ref: "MAI_SSH_KEY_FIXTURE"\\n'
        'privilege: none\\n'
        'capabilities:\\n'
        '  - fixture.read\\n'
    )
    profile_store.write(server, profile_text, expected_revision=0)
    prompt_dir = os.path.join(worker_root, "servers", str(server))
    os.makedirs(prompt_dir, exist_ok=True)
    os.chmod(prompt_dir, 0o700)
    prompt_path = os.path.join(prompt_dir, "prompt.md")
    with open(prompt_path, "w", encoding="utf-8") as handle:
        handle.write("Answer briefly and act only on the fixture host.\\n")
    os.chmod(prompt_path, 0o600)
print("roots seeded")
`;

const completeBlock = (evidence: string[]): string =>
	[
		'Journey complete: change applied and verified.',
		'```machina-complete',
		JSON.stringify({
			task_outcome: 'verified',
			domain_outcome: 'success',
			effect_certainty: 'certain',
			criteria: [
				{ criterion_id: 'applied', result: 'passed', evidence: [evidence[1]] },
				{ criterion_id: 'verified', result: 'passed', evidence: [evidence[0]] }
			],
			evidence
		}),
		'```',
		''
	].join('\n');

const MODEL_SCRIPT: Record<string, Array<Record<string, unknown>>> = {
	journey: [
		{ tool_call: { name: 'fixture_read', args: { marker: 'm1' }, id: 'c1' } },
		{ tool_call: { name: 'fixture_write', args: { marker: 'm1' }, id: 'c2' } },
		{ text: completeBlock(['step:1', 'step:9']) }
	],
	ask: [
		{ tool_call: { name: 'fixture_read', args: { marker: 'm2' }, id: 'c1' } },
		{ tool_call: { name: 'fixture_write', args: { marker: 'm3' }, id: 'c2' } },
		{ text: completeBlock(['step:1', 'step:9']) }
	],
	crash: [
		{ tool_call: { name: 'fixture_write', args: { marker: 'mcrash' }, id: 'c1' } },
		{ tool_call: { name: 'fixture_read', args: { marker: 'm4' }, id: 'c2' } },
		{ text: completeBlock(['step:1']) }
	],
	child: [
		{ tool_call: { name: 'fixture_read', args: { marker: 'mc' }, id: 'c1' } },
		{ text: 'Scoped read complete on the delegated target.' }
	],
	// The legacy intake planner receives a plain reply on purpose: the frozen
	// legacy handler settles `failed` with `intake_rejected` — the truthful
	// legacy behavior this scenario renders (never reinterpreted as v2).
	legacy: [{ text: 'Plain conversational reply from the scripted legacy model.' }]
};

interface LiveStack {
	scratch: string;
	workerRoot: string;
	dbName: string;
	pgUrl: string;
	backendOrigin: string;
	uviproc: Proc | null;
	workerRef: { worker: Proc | null };
	browserCtx: BrowserContext | null;
	page: Page | null;
	journalPath: string;
	/** Node-side admin session (bootstrap+login via the real boundary). */
	adminCookieHeader: string;
	adminCsrf: string;
	/** The MINTED server profile ids (the service mints server_id). */
	serverA: string;
	serverB: string;
	/** The browser session logged in through the real UI (once). */
	loggedIn: boolean;
	/** The journey conversation's run id (T1 records it for T6's reopen). */
	journeyRunId: string;
}

const live = {} as LiveStack & { workerRef: { worker: Proc | null } };

/** The same-origin `/api/**` relay to the real uvicorn (no mocks). */
async function attachRelay(ctx: BrowserContext): Promise<void> {
	await ctx.route('**/api/**', async (route) => {
		try {
			const request = route.request();
			const target = new URL(request.url());
			const response = await route.fetch({
				url: live.backendOrigin + target.pathname + target.search
			});
			await route.fulfill({ response });
		} catch {
			// The page/context went away mid-flight (read-only SSE aborts on
			// unmount are expected); nothing is left to fulfill.
			await route.abort().catch(() => undefined);
		}
	});
}

async function apiFetch(method: string, apiPath: string, body?: unknown): Promise<Response> {
	if (!live.adminCookieHeader) throw new Error('admin session not started');
	return fetch(live.backendOrigin + apiPath, {
		method,
		headers: {
			'content-type': 'application/json',
			origin: ORIGIN,
			cookie: live.adminCookieHeader,
			...(body !== undefined && live.adminCsrf ? { 'x-csrf-token': live.adminCsrf } : {})
		},
		body: body === undefined ? undefined : JSON.stringify(body)
	});
}

async function createObjective(
	title: string,
	scope: string[],
	request: string
): Promise<string> {
	const response = await apiFetch('POST', '/api/v1/objectives', {
		server_id: live.serverA,
		original_request: request,
		constraints: ['Stay bounded; fixture targets only.'],
		scope_server_ids: scope,
		title
	});
	const bodyText = await response.text();
	expect(response.status, `objective create ${title}: ${bodyText}`).toBe(201);
	const body = JSON.parse(bodyText) as { objective_id: string };
	return body.objective_id;
}

function journalCount(marker: string): number {
	try {
		return fs
			.readFileSync(live.journalPath, 'utf-8')
			.split('\n')
			.filter((line) => line === marker).length;
	} catch {
		return 0;
	}
}

async function waitUntil(predicate: () => boolean, timeoutMs: number, what: string): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (predicate()) return;
		await delay(300);
	}
	throw new Error(`timed out after ${timeoutMs}ms waiting for ${what}`);
}

function spawnWorker(): Proc {
	const env: Record<string, string | undefined> = {
		PATH: process.env.PATH,
		HOME: process.env.HOME,
		MAI_DATABASE_URL: live.pgUrl,
		MAI_WORKER_HANDLERS: 'acui_fixture',
		MAI_FX_JOURNAL: live.journalPath,
		MAI_FX_GATE_DIR: nodePath.join(live.scratch, 'gates'),
		MAI_FX_GRANTS: nodePath.join(live.scratch, 'grants.json'),
		MAI_FX_POLICY: nodePath.join(live.scratch, 'policy.json'),
		MAI_FX_ENV: nodePath.join(live.workerRoot, 'fixture.env'),
		MAI_FX_MODEL_ROOT: nodePath.join(live.scratch, 'model-root'),
		MAI_FX_DATA_ROOT: live.workerRoot,
		MAI_FX_BEHAVIOR: JSON.stringify({ 'fixture.write:mcrash': 'gate' }),
		MAI_FX_MODEL_SCRIPT: JSON.stringify(MODEL_SCRIPT),
		MAI_FX_CANARY: FIXTURE_SECRET_TOKEN,
		PYTHONPATH: nodePath.join(live.scratch, 'handlers')
	};
	const logFd = fs.openSync(nodePath.join(live.scratch, `worker-${nodeCrypto.randomUUID().slice(0, 6)}.log`), 'a');
	const proc = cp.spawn(
		backendPython(),
		['-m', 'mai.jobs.worker', '--concurrency', '2'],
		{ cwd: BACKEND_ROOT, env, stdio: ['ignore', logFd, logFd] }
	);
	fs.closeSync(logFd);
	return proc;
}

async function stopProc(proc: Proc | null, kill: boolean): Promise<void> {
	if (!proc || proc.exitCode !== null) return;
	const { promise, resolve } = Promise.withResolvers<void>();
	const timer = setTimeout(() => proc.kill('SIGKILL'), 10_000);
	proc.once('exit', () => {
		clearTimeout(timer);
		resolve();
	});
	proc.kill(kill ? 'SIGKILL' : 'SIGTERM');
	await promise;
}

async function killAndRespawnWorker(): Promise<void> {
	await stopProc(live.workerRef.worker, true);
	live.workerRef.worker = spawnWorker();
}

test.describe('activity-ui v2 live (real uvicorn + pg18 + workers + production UI)', () => {
	test.describe.configure({ mode: 'serial', timeout: 300_000 });

	/** The legacy conversation id found in the canonical history (T5). */
	let legacyConversationId: string | null = null;

	test.beforeAll(async ({ browser }) => {
		test.setTimeout(420_000);
		const dockerCheck = await run('docker', ['info', '--format', '{{.ServerVersion}}']);
		test.skip(
			dockerCheck.code !== 0,
			'docker unavailable: the live stack needs the disposable postgres container'
		);
		const pgCheck = await run('docker', ['exec', PG_CONTAINER, 'pg_isready', '-U', PG_SUPER_USER]);
		test.skip(pgCheck.code !== 0, `coordinator postgres container ${PG_CONTAINER} not reachable`);

		fs.mkdirSync(SCRATCH, { recursive: true });
		live.scratch = SCRATCH;
		fs.mkdirSync(nodePath.join(SCRATCH, 'handlers'), { recursive: true });
		fs.mkdirSync(nodePath.join(SCRATCH, 'gates'), { recursive: true });
		fs.writeFileSync(
			nodePath.join(SCRATCH, 'handlers', 'acui_fixture.py'),
			HANDLER_MODULE_SOURCE,
			{ mode: 0o600 }
		);
		fs.writeFileSync(nodePath.join(SCRATCH, 'acui_seed_delegate.py'), SEED_DELEGATE_SOURCE, {
			mode: 0o600
		});
		fs.writeFileSync(nodePath.join(SCRATCH, 'acui_seed_legacy.py'), SEED_LEGACY_SOURCE, {
			mode: 0o600
		});
		live.journalPath = nodePath.join(SCRATCH, 'journal.log');
		// Worker-side root (env file now; grants/policy/prompts after the
		// real profiles exist and their minted ids are known).
		live.workerRoot = nodePath.join(SCRATCH, 'worker-root');
		fs.mkdirSync(live.workerRoot, { recursive: true });
		fs.writeFileSync(
			nodePath.join(live.workerRoot, 'fixture.env'),
			'MAI_SSH_KEY_FIXTURE=fixture-key-value-not-a-secret\n',
			{ mode: 0o600 }
		);

		// -- own disposable database in the coordinator container -------------
		live.dbName = DB_NAME;
		live.pgUrl = PG_URL;
		const created = await run('docker', [
			'exec', PG_CONTAINER, 'psql', '-U', PG_SUPER_USER, '-d', 'postgres',
			'-c', `CREATE DATABASE ${DB_NAME} OWNER ${PG_SUPER_USER}`
		]);
		expect(created.code, `create database failed: ${created.stderr}`).toBe(0);
		const migrated = await run(
			backendPython(),
			['-m', 'alembic', '-c', 'alembic.ini', 'upgrade', 'head'],
			{
				cwd: BACKEND_ROOT,
				env: { PATH: process.env.PATH!, HOME: process.env.HOME!, MAI_DATABASE_URL: live.pgUrl },
				timeout: 240_000
			}
		);
		expect(migrated.code, `alembic upgrade head failed: ${migrated.stderr.slice(-800)}`).toBe(0);
		// The DBOS system schema boots at FIRST engine launch (not alembic):
		// one launch+shutdown initializes dbos.workflow_status so the API's
		// enqueue path works (the proven conversation-api fixture order).
		const dbosBoot = await run(
			backendPython(),
			['-c', 'import os; from mai.jobs import engine; engine.launch_worker(os.environ["MAI_DATABASE_URL"]); engine.shutdown()'],
			{
				cwd: BACKEND_ROOT,
				env: { PATH: process.env.PATH!, HOME: process.env.HOME!, MAI_DATABASE_URL: live.pgUrl },
				timeout: 120_000
			}
		);
		expect(dbosBoot.code, `dbos bootstrap failed: ${dbosBoot.stderr.slice(-600)}`).toBe(0);

		fs.mkdirSync(nodePath.join(SCRATCH, 'model-root'), { recursive: true });

		// -- real uvicorn on a loopback ephemeral port -------------------------
		const backendPort = await freePort();
		live.backendOrigin = `http://127.0.0.1:${backendPort}`;
		const dataRoot = nodePath.join(SCRATCH, 'api-data-root');
		fs.mkdirSync(dataRoot, { recursive: true });
		const envFile = nodePath.join(SCRATCH, 'resolver.env');
		fs.writeFileSync(envFile, 'MAI_SSH_KEY_FIXTURE=fixture-key-value-not-a-secret\n', { mode: 0o600 });
		const uvicornLog = nodePath.join(SCRATCH, 'uvicorn.log');
		const logFd = fs.openSync(uvicornLog, 'a');
		live.uviproc = cp.spawn(
			backendPython(),
			['-m', 'uvicorn', 'mai.api.app:create_app', '--factory', '--host', '127.0.0.1', '--port', String(backendPort)],
			{
				cwd: BACKEND_ROOT,
				env: {
					PATH: process.env.PATH,
					HOME: process.env.HOME,
					MAI_DATABASE_URL: live.pgUrl,
					MAI_DATA_ROOT: dataRoot,
					MAI_ENV_FILE: envFile,
					MAI_PUBLIC_ORIGINS: ORIGIN
				},
				stdio: ['ignore', logFd, logFd]
			}
		);
		fs.closeSync(logFd);
		await waitTcp(backendPort, 90_000);

		// One-shot bootstrap + admin login through the real boundary (the
		// Node-side session seeds profiles/grants/objectives; the browser
		// signs in separately through the real UI).
		const bootstrap = await fetch(`${live.backendOrigin}/api/v1/auth/bootstrap`, {
			method: 'POST',
			headers: { 'content-type': 'application/json', origin: ORIGIN },
			body: JSON.stringify({ password: ADMIN_PASSWORD })
		});
		expect([201, 409]).toContain(bootstrap.status);
		const login = await fetch(`${live.backendOrigin}/api/v1/auth/login`, {
			method: 'POST',
			headers: { 'content-type': 'application/json', origin: ORIGIN },
			body: JSON.stringify({ password: ADMIN_PASSWORD })
		});
		expect(login.status, 'admin login at the real backend').toBe(204);
		const setCookies = login.headers.getSetCookie();
		live.adminCookieHeader = setCookies
			.map((cookie) => cookie.split(';', 1)[0])
			.join('; ');
		live.adminCsrf =
			setCookies
				.find((cookie) => cookie.startsWith('mai_csrf='))
				?.split(';', 1)[0]
				.split('=')[1] ?? '';

		// -- REAL server profiles (production surface; the SERVICE mints ids) --
		live.serverA = '';
		live.serverB = '';
		for (const [name, slot] of [
			['acui-server-a', 'serverA'],
			['acui-server-b', 'serverB']
		] as const) {
			const createdProfile = await apiFetch('POST', '/api/v1/servers', {
				name,
				prompt_revision: 7,
				environment: 'linux_ssh',
				ssh: {
					host: 'fixture.internal',
					port: 22,
					user: 'fixture',
					host_key: 'SHA256:6VhOyUC6fvXW0aQ9WNe7Kx3nZZf+zxgupEWLoZXoTMU',
					credential_ref: 'MAI_SSH_KEY_FIXTURE'
				},
				privilege: 'none',
				capabilities: []
			});
			const profileText = await createdProfile.text();
			expect(createdProfile.status, `profile ${name}: ${profileText}`).toBe(201);
			live[slot] = (JSON.parse(profileText) as { server_id: string }).server_id;
		}
		// Grants ride the REAL surface too (the canonical submit resolves the
		// turn's allowed capabilities from this store).
		const granted = await apiFetch('PUT', '/api/v1/grants', {
			expected_revision: 0,
			denied_capabilities: [],
			grants: [
				{ capability_id: 'fixture.read', server_ids: [live.serverA, live.serverB] },
				{ capability_id: 'fixture.write', server_ids: [live.serverA] }
			]
		});
		expect(granted.status, `grants put: ${await granted.text()}`).toBe(200);

		// -- worker-side roots (need the minted ids) ---------------------------
		fs.writeFileSync(
			nodePath.join(SCRATCH, 'grants.json'),
			JSON.stringify([
				{ capability_id: 'fixture.read', server_ids: [live.serverA, live.serverB] },
				{ capability_id: 'fixture.write', server_ids: [live.serverA] }
			])
		);
		fs.writeFileSync(
			nodePath.join(SCRATCH, 'policy.json'),
			JSON.stringify({ revision: 1, denied: [] })
		);
		fs.writeFileSync(nodePath.join(SCRATCH, 'seed_model_store.py'), SEED_ROOTS_SOURCE, {
			mode: 0o600
		});
		const seeded = await run(
			backendPython(),
			[
				nodePath.join(SCRATCH, 'seed_model_store.py'),
				nodePath.join(SCRATCH, 'model-root'),
				live.workerRoot,
				live.serverA,
				live.serverB
			],
			{
				cwd: BACKEND_ROOT,
				env: { PATH: process.env.PATH!, HOME: process.env.HOME! },
				timeout: 60_000
			}
		);
		expect(seeded.code, `roots seed failed: ${seeded.stderr}`).toBe(0);
		// The API process owns ITS OWN confined data root (MAI_DATA_ROOT):
		// seed the SAME deterministic model store (gateway connection +
		// `default` profile + selection) there, so the live profile picker
		// has a real choice and the canonical submit pins a real canonical
		// profile — never a failed-fetch fallback. Server profiles are NOT
		// rewritten: enrollment already recorded them at their real CAS
		// revisions.
		fs.writeFileSync(
			nodePath.join(SCRATCH, 'acui_seed_api_models.py'),
			SEED_API_MODEL_STORE_SOURCE,
			{ mode: 0o600 }
		);
		const seededApi = await run(
			backendPython(),
			[nodePath.join(SCRATCH, 'acui_seed_api_models.py'), dataRoot],
			{
				cwd: BACKEND_ROOT,
				env: { PATH: process.env.PATH!, HOME: process.env.HOME! },
				timeout: 60_000
			}
		);
		expect(seededApi.code, `api model store seed failed: ${seededApi.stderr}`).toBe(0);

		// -- browser context: production UI + the /api relay (no mocks) -------
		const ctx = await browser.newContext({ baseURL: ORIGIN, viewport: { width: 1280, height: 800 } });
		live.browserCtx = ctx;
		await attachRelay(ctx);
		live.page = await ctx.newPage();

		// -- seed the worker composition ---------------------------------------
		live.workerRef = { worker: spawnWorker() };
	});

	test.afterAll(async () => {
		try {
			await live.page?.close().catch(() => undefined);
			await live.browserCtx?.close().catch(() => undefined);
			await stopProc(live.workerRef?.worker ?? null, false);
			await stopProc(live.uviproc, false);
			if (live.dbName) {
				const dropped = await run('docker', [
					'exec', PG_CONTAINER, 'psql', '-U', PG_SUPER_USER, '-d', 'postgres',
					'-c', `DROP DATABASE IF EXISTS ${live.dbName} WITH (FORCE)`
				]);
				console.log(`[acui-live] drop ${live.dbName}: code ${dropped.code}`);
			}
			if (live.scratch && process.env.MAI_ACUI_KEEP !== '1') {
				fs.rmSync(live.scratch, { recursive: true, force: true });
			}
			console.log(`[acui-live] cleanup done (scratch kept: ${process.env.MAI_ACUI_KEEP === '1' ? live.scratch : 'no'})`);
		} catch (error) {
			console.log(`[acui-live] CLEANUP ERROR: ${String(error)}`);
		}
	});

	const page = (): Page => {
		if (!live.page) throw new Error('live page not initialized');
		return live.page;
	};

	async function loginThroughUi(): Promise<void> {
		await page().goto('/login');
		await expect(page().getByTestId('login-form')).toBeVisible();
		await page().locator('input[type="password"]').fill(ADMIN_PASSWORD);
		await page().getByTestId('login-submit').click();
		await expect(page().getByTestId('component-host')).toBeVisible({ timeout: 30_000 });
	}

	/**
	 * Submits one New Chat turn with NO objective control: the runtime/
	 * objective dropdown is gone, the direct-agent default starts the v2
	 * episode server-side and binds the conversation automatically
	 * (runtime-v2 §3.3; the backend admission owns the episode).
	 */
	async function submitV2Turn(mode: 'delegated' | 'ask', message: string): Promise<void> {
		if (!live.loggedIn) {
			await loginThroughUi();
			live.loggedIn = true;
		}
		const dock = page().getByTestId('bottom-chat-dock');
		await expect(dock).toBeVisible();
		// A started context locks the draft: every NEW submission starts
		// from a fresh unlocked draft.
		await page().getByTestId('drawer-new-chat').click();
		await page().getByTestId('server-select').selectOption(live.serverA);
		// The runtime/objective selector is REMOVED (no picker, no load-more,
		// no list error slot); the Ask/Delegated mode picker stays.
		await expect(dock.getByTestId('chat-objective-picker')).toHaveCount(0);
		await expect(dock.getByTestId('chat-objectives-load-more')).toHaveCount(0);
		await expect(dock.getByTestId('chat-objectives-error')).toHaveCount(0);
		await expect(dock.getByTestId('chat-mode-picker')).toBeVisible();
		await dock.getByTestId('chat-mode-picker').selectOption(mode);
		await dock.getByTestId('compose-message').fill(message);
		await dock.getByTestId('chat-submit').click();
		// Loud failure: a rejected submission names its structured error
		// instead of leaving the probe/assertions to time out vaguely.
		try {
			await expect(dock.getByTestId('conversation-state')).toBeVisible({
				timeout: 20_000
			});
		} catch (error) {
			console.log(
				'[SUBMIT-ERR]',
				await dock
					.getByTestId('compose-error')
					.textContent()
					.catch(() => 'no compose-error rendered')
			);
			throw error;
		}
	}

	/**
	 * The server's most recently updated objective episode: for a fresh
	 * New-Chat submission that IS the episode the admission just created
	 * (objectives list order: `updated_at DESC`).
	 */
	async function latestObjectiveId(): Promise<string> {
		const response = await apiFetch(
			'GET',
			`/api/v1/objectives?server_id=${live.serverA}&limit=1`
		);
		expect(response.status).toBe(200);
		const body = (await response.json()) as { items?: Array<{ objective_id: string }> };
		const id = body.items?.[0]?.objective_id ?? '';
		expect(id, 'the automatic episode exists').not.toBe('');
		return id;
	}

	async function openObjectiveSections(): Promise<void> {
		const activity = page().getByTestId('objective-activity');
		await expect(activity).toBeVisible({ timeout: 30_000 });
	}

	test('live v2 submission: header metrics, expandable timeline, receipts and verification', async () => {
		if (!live.loggedIn) {
			await loginThroughUi();
			live.loggedIn = true;
		}
		// New Chat with NO objective control: the direct-agent default starts
		// the episode inside the admission (first submit = automatic v2).
		await submitV2Turn(
			'delegated',
			'mission=journey Do the read, apply the bounded change, verify.'
		);
		// The accepted turn is durable: the activity section mounts from the
		// server's own v2 markers (handler_name + objective_ids) — the
		// AUTOMATIC episode the backend created for this conversation.
		await openObjectiveSections();
		const objectiveId = await latestObjectiveId();
		const header = page().getByTestId('objective-header');
		await expect(header).toBeVisible();
		// An automatic episode carries no operator title: the header renders
		// the honest short-id fallback (never a fabricated title).
		await expect(header.getByTestId('objective-title')).toContainText(
			/^Objective [0-9a-f]{6}/
		);
		await expect(header.getByTestId('objective-target')).toContainText('Target');
		await expect(header.getByTestId('objective-model')).toContainText('Model default', {
			timeout: 30_000
		});
		// LIVE: the run is working — the header names the current state and
		// the elapsed timer ticks from real run data.
		await expect(header.getByTestId('objective-status')).toContainText(/Running|Queued|Succeeded/);
		// The v2 pending phase never presents the legacy intake ladder (AC5).
		await expect(page().getByTestId('chat-root')).not.toContainText(
			'Understanding request & preparing task plan…'
		);
		// The journey settles: 2 effects, receipts and an evidence-bound verdict.
		const journal = page().getByTestId('objective-journal');
		await expect(journal.locator('summary').first()).toContainText(
			/Work journal/, { timeout: 90_000 }
		);
		await journal.locator('summary').first().click();
		await journal.getByTestId('journal-verification').locator('summary').click();
		await expect(
			journal.getByTestId('journal-verification-verdict'),
			'evidence-bound verification lands'
		).toBeVisible({ timeout: 90_000 });
		await expect(page().getByTestId('objective-status')).toContainText('Succeeded', {
			timeout: 120_000
		});
		// Expandable complete timeline: ledger + event payloads.
		await page().locator('[data-testid="objective-ledger"] > summary').click();
		await expect(page().getByTestId('objective-ledger-scroll')).toBeVisible();
		const eventCount = await page().getByTestId('objective-event').count();
		expect(eventCount).toBeGreaterThan(4);
		const firstEvent = page().getByTestId('objective-event').first();
		await firstEvent.locator('summary').click();
		await expect(firstEvent.getByTestId('objective-event-payload')).toBeVisible();
		// Work journal sections expand independently (the verification section
		// is already open from the verdict assertion above).
		await journal.getByTestId('journal-actions').locator('summary').click();
		await expect(journal.getByTestId('journal-action').first()).toBeVisible();
		await journal.getByTestId('journal-receipts').locator('summary').click();
		await expect(journal.getByTestId('journal-receipt')).toHaveCount(2);
		await expect(journal.getByTestId('journal-criterion').first()).toContainText('passed');
		// Redaction honesty: the secret-shaped fixture token never renders.
		const bodyText = await page().getByTestId('chat-root').textContent();
		expect(bodyText).not.toContain(FIXTURE_SECRET_TOKEN);
		// Record the journey conversation's run id (T6 reopens it by the
		// drawer label the row actually renders: `Run <short id>`).
		const journeyRuns = await apiFetch(
			'GET',
			`/api/v1/objectives/${objectiveId}/runs?server_id=${live.serverA}&limit=5`
		);
		const journeyRunBody = (await journeyRuns.json()) as {
			items?: Array<{ run_id: string; parent_run_id: string | null }>;
		};
		live.journeyRunId =
			(journeyRunBody.items ?? []).find((one) => one.parent_run_id === null)?.run_id ?? '';
		expect(live.journeyRunId).not.toBe('');
		// Desktop evidence screenshot of the settled journey view.
		fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
		await page().screenshot({
			path: nodePath.join(EVIDENCE_DIR, 'desktop-v2-journey.png'),
			fullPage: false
		});
	});

	test('bounded ledger pagination appends without duplicates; replay deduplicates', async () => {
		// A second v2 turn on the SAME objective pushes the ledger past one
		// page: the "Load more" control appears and appends OLDER rows.
		const dock = page().getByTestId('bottom-chat-dock');
		await dock.getByTestId('compose-message').fill('mission=journey Second pass: read, change, verify again.');
		await dock.getByTestId('chat-submit').click();
		await expect(page().getByTestId('objective-status')).toContainText('Succeeded', { timeout: 120_000 });
		// Idempotent expand: T1 may have left the ledger open (serial flow).
		const ledgerDetails = page().locator('[data-testid="objective-ledger"]');
		if ((await ledgerDetails.getAttribute('open')) === null) {
			await ledgerDetails.locator('summary').click();
		}
		const loadMore = page().getByTestId('objective-events-load-more');
		await expect(loadMore).toBeVisible({ timeout: 30_000 });
		const beforeSeqs = await page().getByTestId('objective-event').evaluateAll((rows) =>
			rows.map((row) => row.getAttribute('data-seq'))
		);
		await loadMore.click();
		await expect
			.poll(async () => (await page().getByTestId('objective-event').count()))
			.toBeGreaterThan(beforeSeqs.length);
		const afterSeqs = await page().getByTestId('objective-event').evaluateAll((rows) =>
			rows.map((row) => row.getAttribute('data-seq'))
		);
		expect(new Set(afterSeqs).size, 'no duplicate ledger rows after append').toBe(afterSeqs.length);
		// Replay/reconnect dedup: close and reopen the conversation from the
		// server list — the section re-subscribes and replays from the
		// watermark; every sequence still renders exactly once.
		await page().getByTestId('drawer-new-chat').click();
		await expect(page().getByTestId('objective-activity')).toHaveCount(0);
		await page().getByTestId('drawer-history-item').first().click();
		await expect(page().getByTestId('objective-activity')).toBeVisible({ timeout: 30_000 });
		await page().locator('[data-testid="objective-ledger"] > summary').click();
		// The fresh section legitimately loads its FIRST page; the replay
		// property under test is DEDUPLICATION (watermark + page merge), so
		// every rendered sequence must appear exactly once. Under the
		// direct-agent default the follow-up above may have rolled the
		// conversation into a NEW episode, and the reopened section mounts
		// one of the conversation's episodes — the dedup invariant holds
		// for whichever episode rendered.
		await expect
			.poll(
				async () => (await page().getByTestId('objective-event').count()),
				{ timeout: 30_000 }
			)
			.toBeGreaterThanOrEqual(1);
		const replaySeqs = await page().getByTestId('objective-event').evaluateAll((rows) =>
			rows.map((row) => row.getAttribute('data-seq'))
		);
		expect(replaySeqs.length, 'the reopened ledger rendered rows').toBeGreaterThanOrEqual(1);
		expect(new Set(replaySeqs).size).toBe(replaySeqs.length);
	});

	test('multi-server delegation: v2 tree server_ids, child receipts, target/resource filter', async () => {
		// The delegation FIXTURE needs an episode scoped to BOTH servers up
		// front (each child binds with its own initiating server in scope),
		// while the automatic episode is scoped exactly to the initiating
		// server. Meaningful scenario setup only: pre-create the operator
		// objective and bind it at submit through the documented explicit
		// wire path (the UI no longer carries an objective control).
		const objectiveId = await createObjective(
			'Delegation objective',
			[live.serverA, live.serverB],
			'Fan the scoped reads out across both fixture servers.'
		);
		const conversationKey = nodeCrypto.randomUUID();
		const bound = await apiFetch('POST', '/api/v1/chat/conversations', {
			server_id: live.serverA,
			client_request_id: conversationKey,
			message: 'mission=child Prepare the delegation episode.',
			profile: 'default',
			objective_id: objectiveId
		});
		expect(
			[200, 201],
			`bound submit: ${await bound.text()}`
		).toContain(bound.status);
		// Open the bound conversation from the drawer (its run id IS the
		// conversation id, so the history row labels it `Run <short id>`).
		const dock = page().getByTestId('bottom-chat-dock');
		await expect(dock).toBeVisible();
		await page().getByTestId('server-select').selectOption(live.serverA);
		await page().getByTestId('drawer-new-chat').click();
		const historyToggle = page().getByTestId('drawer-history-toggle');
		const expanded = await historyToggle.getAttribute('aria-expanded');
		if (expanded !== 'true') await historyToggle.click();
		else {
			await historyToggle.click();
			await historyToggle.click();
		}
		await page()
			.getByTestId('drawer-history-item')
			.filter({ hasText: conversationKey.slice(0, 8) })
			.first()
			.click();
		await expect(page().getByTestId('objective-activity')).toBeVisible({
			timeout: 30_000
		});
		await expect(page().getByTestId('objective-status')).toContainText('Succeeded', { timeout: 90_000 });
		const parentRunId = nodeCrypto.randomUUID();
		const seeded = await run(
			backendPython(),
			[
				nodePath.join(SCRATCH, 'acui_seed_delegate.py'),
				live.pgUrl,
				objectiveId,
				parentRunId,
				live.serverA,
				live.serverB
			],
			{ cwd: BACKEND_ROOT, env: { PATH: process.env.PATH!, HOME: process.env.HOME! }, timeout: 120_000 }
		);
		expect(seeded.code, `delegate seed failed: ${seeded.stderr.slice(-600)}`).toBe(0);
		// Children settle (each runs a real scripted read on its target).
		const runsResponse = await apiFetch(
			'GET',
			`/api/v1/objectives/${objectiveId}/runs?server_id=${live.serverA}&limit=10`
		);
		expect(runsResponse.status).toBe(200);
		await expect
			.poll(
				async () => {
					const response = await apiFetch(
						'GET',
						`/api/v1/objectives/${objectiveId}/runs?server_id=${live.serverA}&limit=10`
					);
					const body = (await response.json()) as { total: number };
					return body.total;
				},
				{ timeout: 90_000, intervals: [1_000, 2_500, 5_000] }
			)
			.toBe(4);
		// Wire probe (D2 remediated): the children's persisted parent_run_id
		// rows exist, every row carries its mandatory classification tag,
		// and the facade previews each row under its OWN server scope, so
		// the read is clean — never a 500.
		const parentView = await apiFetch('GET', `/api/v1/chat/runs/${parentRunId}`);
		expect(parentView.status).toBe(200);
		const parentBody = (await parentView.json()) as { children?: unknown[] };
		expect(parentBody.children?.length ?? 0).toBe(2);
		// The tree: expand the runs section, then the parent's tree (the
		// conversation's own run + the parent + 2 children).
		await page().getByTestId('objective-runs').locator('summary').click();
		await expect(page().getByTestId('objective-run-row')).toHaveCount(4, { timeout: 60_000 });
		const parentRow = page().locator(`[data-testid="objective-run-row"][data-run-id="${parentRunId}"]`);
		await expect(parentRow).toHaveCount(1);
		await parentRow.getByTestId('objective-run-toggle').click();
		const tree = page().getByTestId('objective-run-tree');
		await expect(tree).toBeVisible({ timeout: 30_000 });
		// v2 children carry their delegated server subsets (§7.2).
		await expect(tree.getByTestId('child-row')).toHaveCount(2);
		await expect(tree.getByTestId('child-servers').first()).toBeVisible();
		// Children settle on their own durable runs (D1 remediated: the v2
		// child dispatch resolves the §7.2 envelope through the real
		// handler); the tree's state chips replay from the read runs and
		// the parent stream's recorded `agent.children_resolved` facts, so
		// poll to a RECORDED terminal: both delegated children ran their
		// scripted read and Succeeded.
		let observedChildStates: string[] = [];
		await expect
			.poll(
				async () => {
					observedChildStates = await tree.getByTestId('child-state').allTextContents();
					return observedChildStates.filter((state) =>
						/Succeeded/.test(state)
					).length;
				},
				{ timeout: 90_000, intervals: [1_000, 2_500, 5_000] }
			)
			.toBe(2);
		console.log(`[acui-live] observed child states: ${JSON.stringify(observedChildStates)}`);
		// Aggregated RECEIPTS of the objective: the conversation run's and
		// both children's real effects journal here (each child ran one
		// scripted read on its delegated target).
		await expect(tree.getByTestId('receipts-section')).toBeVisible();
		await expect(tree.getByTestId('child-receipt').first()).toBeVisible({ timeout: 60_000 });
		// Target filter: pinning server B leaves only its child.
		const receiptsBefore = await tree.getByTestId('child-receipt').count();
		expect(receiptsBefore).toBeGreaterThanOrEqual(1);
		await tree.getByTestId('tree-filter-server').selectOption(live.serverB);
		await expect(tree.getByTestId('child-row')).toHaveCount(1);
		// The B-pinned child's own read receipt targets server B, so the
		// filtered receipts list shows it — never an empty-success.
		await expect(tree.getByTestId('receipts-none')).toHaveCount(0);
		await expect(tree.getByTestId('child-receipt').first()).toBeVisible();
		await tree.getByTestId('tree-filter-server').selectOption('');
		await expect(tree.getByTestId('child-row')).toHaveCount(2);
		// Resource filter: a non-matching resource empties the tree honestly.
		await tree.getByTestId('tree-filter-resource').fill('fixture.write');
		await expect(tree.getByTestId('children-none')).toBeVisible();
		await tree.getByTestId('tree-filter-resource').fill('');
		await expect(tree.getByTestId('child-row')).toHaveCount(2);
	});

	test('approval-bound resume; crash window parks reconciling where blind resume is refused', async () => {
		// Two real park/wake journeys in one scenario: the payload-bound
		// approval wake plus the crash-window recovery (kill → reconcile →
		// wake). Each involves real worker restarts — bounded, but long.
		test.setTimeout(600_000);
		// (a) ask mode via New Chat (no objective control): the non-read
		// effect parks waiting_approval; the ONE payload-bound decision
		// resumes it.
		await submitV2Turn('ask', 'mission=ask Apply the gated change.');
		await expect(page().getByTestId('objective-wait')).toContainText(
			'Waiting for approval decision',
			{ timeout: 90_000 }
		);
		const dock = page().getByTestId('bottom-chat-dock');
		await expect(dock.getByTestId('approval-decide')).toBeVisible({ timeout: 30_000 });
		await dock.getByTestId('approval-decide').click();
		await expect(page().getByTestId('objective-status')).toContainText('Succeeded', {
			timeout: 120_000
		});
		// Expand the fresh objective's journal + verification sections.
		const approvalJournal = page().getByTestId('objective-journal');
		await approvalJournal.locator('summary').first().click();
		await approvalJournal.getByTestId('journal-verification').locator('summary').click();
		await expect(page().getByTestId('journal-verification-verdict')).toBeVisible();

		// (b) crash window via New Chat: the gate behavior holds the effect;
		// SIGKILL parks outcome_unknown + reconciling; blind resume is
		// refused.
		await submitV2Turn('delegated', 'mission=crash Apply the crash-window change.');
		await waitUntil(
			() => journalCount('begin:fixture.write:mcrash') >= 1,
			90_000,
			'the gated effect to begin'
		);
		console.log('[T4-T]', 'gate observed, killing worker', Math.round(performance.now() / 1000));
		await killAndRespawnWorker();
		await expect(page().getByTestId('objective-wait')).toContainText(
			'Unknown outcome — reconciliation required',
			{ timeout: 180_000 }
		);
		// Blind resume cannot run: the §9 refusal is rendered and the generic
		// resume control is replaced by the classification form.
		await expect(page().getByTestId('reconcile-refusal')).toContainText('blind resume is disabled');
		await expect(page().getByTestId('resume-section')).toHaveCount(0);
		await expect(page().getByTestId('reconcile-step').first()).toBeVisible();
		await expect(page().getByTestId('reconcile-apply')).toBeDisabled();
		// Operator classification resolves and wakes the run.
		await page().getByTestId('reconcile-verdict-applied').first().check();
		await expect(page().getByTestId('reconcile-apply')).toBeEnabled();
		await page().getByTestId('reconcile-apply').click();
		console.log('[T4-T]', 'classified', Math.round(performance.now() / 1000));
		await expect(page().getByTestId('reconcile-result')).toBeVisible({ timeout: 30_000 });
		console.log('[T4-T]', 'wake sent', Math.round(performance.now() / 1000));
		await expect(page().getByTestId('objective-status')).toContainText('Succeeded', {
			timeout: 180_000
		});
		console.log('[T4-T]', 'succeeded', Math.round(performance.now() / 1000));
		// The unknown row closed honestly: it STAYS in the journal, marked
		// reconciled (never erased, never claimed never-to-have-happened).
		const crashJournal = page().getByTestId('objective-journal');
		await crashJournal.locator('summary').first().click();
		await crashJournal.getByTestId('journal-unknowns').locator('summary').click();
		await expect(page().getByTestId('journal-unknown').first()).toContainText('reconciled');
	});

	test('legacy history renders truthfully with the legacy marker and no fake v2 journal', async () => {
		test.setTimeout(420_000);
		if (!live.loggedIn) {
			await loginThroughUi();
			live.loggedIn = true;
		}
		// A LEGACY conversation: the direct-agent default means the canonical
		// create route NO LONGER produces legacy material, so this fixture
		// seeds the historical row through the INTERNAL store seams (the
		// discipline the backend slice pins) and enqueues it for the REAL
		// frozen agent.run worker (§10.3 read compatibility forever).
		const runId = nodeCrypto.randomUUID();
		const seededLegacy = await run(
			backendPython(),
			[
				nodePath.join(SCRATCH, 'acui_seed_legacy.py'),
				live.pgUrl,
				runId,
				live.serverA,
				'mission=legacy Plain legacy question.'
			],
			{ cwd: BACKEND_ROOT, env: { PATH: process.env.PATH!, HOME: process.env.HOME! }, timeout: 120_000 }
		);
		expect(seededLegacy.code, `legacy seed failed: ${seededLegacy.stderr.slice(-600)}`).toBe(0);
		// The frozen legacy handler settles the scripted plain reply through
		// its frozen checkpoint sequence: read-compatible history, never
		// reinterpreted as v2. Wait for the RECORDED terminal run state AND
		// the canonical history row (the drawer's entry point).
		await expect
			.poll(
				async () => {
					const runRead = await apiFetch('GET', `/api/v1/chat/runs/${runId}`);
					if (runRead.status !== 200) return null;
					const runBody = (await runRead.json().catch(() => ({}))) as {
						state?: string | null;
					};
					const runState = runBody.state ?? '';
					if (!['succeeded', 'failed', 'cancelled'].includes(runState)) {
						return null;
					}
					const list = await apiFetch(
						'GET',
						`/api/v1/chat/conversations?server_id=${live.serverA}&limit=10`
					);
					if (list.status !== 200) return null;
					const listBody = (await list.json().catch(() => ({}))) as {
						items?: Array<{ conversation_id?: string; is_legacy?: boolean }>;
						total?: number;
					};
					legacyConversationId =
						(listBody.items ?? []).find((one) => one.conversation_id === runId)
							?.conversation_id ?? null;
					return legacyConversationId !== null ? true : null;
				},
				{ timeout: 90_000, intervals: [1_000, 2_500, 5_000] }
			)
			.toBe(true);
		// The drawer reads the SELECTED server's scope: pin the fixture
		// server (the legacy conversation's recorded scope) explicitly.
		await page().getByTestId('server-select').selectOption(live.serverA);
		await page().getByTestId('drawer-new-chat').click();
		// The drawer's history page loaded at panel mount (before the legacy
		// conversation was seeded): opening the accordion refreshes it.
		const historyToggle = page().getByTestId('drawer-history-toggle');
		const expanded = await historyToggle.getAttribute('aria-expanded');
		if (expanded !== 'true') await historyToggle.click();
		else {
			await historyToggle.click();
			await historyToggle.click();
		}
		const historyRows = page().getByTestId('drawer-history-item');
		await expect(historyRows.first()).toBeVisible({ timeout: 30_000 });
		// The legacy conversation is the most recent activity on this server.
		await historyRows.first().click();
		await expect(page().getByTestId('legacy-runtime-marker')).toBeVisible({ timeout: 30_000 });
		await expect(page().getByTestId('legacy-runtime-marker')).toContainText('agent.run');
		// NO fabricated v2 surface on a legacy conversation.
		await expect(page().getByTestId('objective-activity')).toHaveCount(0);
		await expect(page().getByTestId('objective-timeline')).toHaveCount(0);
		// The submission is truthfully rendered (value-free wire fact).
		await expect(page().getByTestId('chat-root')).toContainText('Plain legacy question');
	});

	test('360x640 reduced motion: keyboard-operable activity, no overflow, no focus theft', async () => {
		// The mobile context + fresh login + the keyboard pass are bounded
		// but slow; the default 300s is not enough.
		test.setTimeout(600_000);
		// Self-contained: the mobile pass runs on ITS OWN quick v2 episode
		// (one scripted read), so the scenario works standalone too.
		if (!live.loggedIn) {
			await loginThroughUi();
			live.loggedIn = true;
		}
		await submitV2Turn(
			'delegated',
			'mission=child Prepare the mobile accessibility pass.'
		);
		await expect(page().getByTestId('objective-status')).toContainText('Succeeded', {
			timeout: 120_000
		});
		// The mobile episode's conversation run id (the drawer row label).
		const mobileObjective = await latestObjectiveId();
		const mobileRuns = await apiFetch(
			'GET',
			`/api/v1/objectives/${mobileObjective}/runs?server_id=${live.serverA}&limit=5`
		);
		const mobileRunBody = (await mobileRuns.json()) as {
			items?: Array<{ run_id: string; parent_run_id: string | null }>;
		};
		const mobileRunId =
			(mobileRunBody.items ?? []).find((one) => one.parent_run_id === null)?.run_id ?? '';
		expect(mobileRunId).not.toBe('');

		// A FRESH pre-sized context: resizing the live 1280px workspace
		// crashed the headless renderer, so the 360x640 pass runs in its own
		// context created at the mobile viewport with reduced motion.
		const browserRef = live.browserCtx;
		if (browserRef === null) throw new Error('live browser context not started');
		const mobileCtx = await browserRef.browser()!.newContext({
			baseURL: ORIGIN,
			viewport: { width: 360, height: 640 },
			reducedMotion: 'reduce'
		});
		await attachRelay(mobileCtx);
		const mobilePage = await mobileCtx.newPage();
		const priorPage = live.page;
		live.page = mobilePage;
		try {
			await loginThroughUi();
			// At 360px the navigation drawer (server tree + history) starts
			// COLLAPSED: the shell's own toggle opens it (the reviewed
			// desktop-shell.spec 360px convention).
			// The drawer may START open at 360px: toggle only to OPEN it.
			const drawerToggle = mobilePage.getByTestId('side-drawer-toggle');
			const drawerExpanded = await drawerToggle.getAttribute('aria-expanded');
			if (drawerExpanded !== 'true') await drawerToggle.click();
			await expect(mobilePage.getByTestId('server-select')).toBeVisible({
				timeout: 20_000
			});
			await mobilePage.getByTestId('server-select').selectOption(live.serverA);
			await mobilePage.getByTestId('drawer-new-chat').click();
			const mobileHistoryToggle = mobilePage.getByTestId('drawer-history-toggle');
			const mobileExpanded = await mobileHistoryToggle.getAttribute('aria-expanded');
			if (mobileExpanded !== 'true') await mobileHistoryToggle.click();
			else {
				await mobileHistoryToggle.click();
				await mobileHistoryToggle.click();
			}
			await mobilePage
				.getByTestId('drawer-history-item')
				.filter({ hasText: mobileRunId.slice(0, 8) })
				.first()
				.click();
			await expect(mobilePage.getByTestId('objective-activity')).toBeVisible({
				timeout: 30_000
			});
			await runMobilePass(mobilePage);
		} finally {
			live.page = priorPage;
		}
		// The mobile context closes with the browser context in afterAll.
		await mobileCtx.close();
	});

	/** The 360x640 keyboard/overflow/focus assertions over one open page. */
	async function runMobilePass(mobilePage: Page): Promise<void> {
		const page = (): Page => mobilePage;
		// Honest reduced motion: the global baseline (tokens.css) collapses
		// every animation's timing under `prefers-reduced-motion: reduce`.
		const animationDuration = await page().evaluate(
			() => getComputedStyle(document.body).animationDuration
		);
		// `0.01ms` serializes as `1e-05s`; `0s` when no animation applies.
		expect(['1e-05s', '0.01ms', '0s']).toContain(animationDuration);
		// No horizontal overflow at 360px (AGENTS.md mobile invariant).
		await expect
			.poll(async () => page().evaluate(() => document.documentElement.scrollWidth))
			.toBeLessThanOrEqual(360);
		expect(await page().evaluate(() => window.innerWidth)).toBe(360);
		// Keyboard: every timeline detail expands/collapses with Enter/Space.
		await page().getByTestId('compose-message').focus();
		const ledgerSummary = page().locator('[data-testid="objective-ledger"] > summary');
		await ledgerSummary.focus();
		await expect(ledgerSummary).toBeFocused();
		await ledgerSummary.press('Enter');
		await expect(page().getByTestId('objective-ledger-scroll')).toBeVisible();
		// A ledger event expands via keyboard too.
		const firstEvent = page().getByTestId('objective-event').first();
		await firstEvent.locator('summary').focus();
		await firstEvent.locator('summary').press('Enter');
		await expect(firstEvent.getByTestId('objective-event-payload')).toBeVisible();
		await firstEvent.locator('summary').press('Enter');
		await expect(firstEvent.getByTestId('objective-event-payload')).toBeHidden();
		await ledgerSummary.press('Enter');
		await expect(page().getByTestId('objective-ledger-scroll')).toBeHidden();
		// Controls operate by keyboard: the runs disclosure and its tree toggle.
		await page().getByTestId('objective-runs').locator('summary').focus();
		await page().getByTestId('objective-runs').locator('summary').press('Enter');
		const runToggle = page().getByTestId('objective-run-toggle').first();
		await runToggle.focus();
		await runToggle.press('Enter');
		await expect(page().getByTestId('objective-run-tree')).toBeVisible({ timeout: 30_000 });
		await runToggle.press('Enter');
		await expect(page().getByTestId('objective-run-tree')).toBeHidden();
		// Streaming/poll updates never steal focus from a focused control.
		await page().getByTestId('compose-message').focus();
		await expect(page().getByTestId('compose-message')).toBeFocused();
		await page().waitForTimeout(4_000);
		await expect(page().getByTestId('compose-message')).toBeFocused();
		// Mobile evidence screenshot (360x640, reduced motion).
		fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
		await page().screenshot({
			path: nodePath.join(EVIDENCE_DIR, 'mobile-360-v2-journey.png'),
			fullPage: false
		});
	}
});
