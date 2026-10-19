"""Mai worker entrypoint: durable job execution, independent of the HTTP API.

The worker is one process role of the ``mai`` backend package (SERVICES
``mai-worker`` row): it launches the full DBOS engine against Machina's
Postgres — including launch recovery of pending runs — and claims runs from
the Machina queue (``mai.jobs.engine.QUEUE_NAME``). It imports nothing from
the HTTP server, so it can start, stop and restart independently of the API
(card 05-engine-adapter acceptance 2).

Verified command (placeholder Compose command is replaced by the later
platform wiring card, which owns the Compose service)::

    MAI_DATABASE_URL=postgresql://… python -m mai.jobs.worker

Environment:

- ``MAI_DATABASE_URL`` (required) — the Machina Postgres URL, resolved by
  :func:`mai.database.resolve_database_url` (the single URL contract).
- ``MAI_WORKER_HANDLERS`` (optional) — comma-separated import paths of
  modules that call :func:`mai.jobs.engine.register_job_handler`. Imported
  before launch; a module that fails to import stops the worker with exit 2
  (fail closed: an unregistered handler would fail its runs at execution
  time anyway). Until the owner cards land real handler modules this may be
  unset — the worker still launches, claims and recovers runs.

Signals: ``SIGTERM``/``SIGINT`` stop the engine cleanly (exit 0);
``SIGKILL`` leaves recovery to the next launch (job-engine ADR, exp1).
"""

from __future__ import annotations

import argparse
import importlib
import os
import signal
import sys
import threading
from collections.abc import Sequence

from mai.database import resolve_database_url
from mai.jobs import engine

_STOP = threading.Event()


def _request_stop(signum: int, _frame: object) -> None:
    """Record the stop signal; the main thread performs the shutdown."""
    del signum
    _STOP.set()


def _import_handlers(spec: str) -> int:
    """Import handler-registration modules; return the process exit code."""
    for name in (part.strip() for part in spec.split(",")):
        if not name:
            continue
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 - worker must fail closed
            print(
                f"mai worker: handler module '{name}' failed to import: "
                f"{type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return 2
    return 0


def _configure_default_agent_runtime(database_url: str) -> None:
    """Wire ``agent.run`` and ``agent.delegate`` when ``MAI_DATA_ROOT`` is set."""
    data_root_raw = os.environ.get("MAI_DATA_ROOT", "").strip()
    if not data_root_raw:
        return
    from pathlib import Path
    import uuid

    from mai.agents import delegate as agent_delegate
    from mai.agents import run as agent_run

    if getattr(agent_run, "_EXECUTOR", None) is not None:
        return

    from mai.agents.budgets import BudgetLedger
    from mai.agents.model_store import ModelConfigStore
    from mai.capabilities.chat_read import (
        ChatReadSessionCapability,
        chat_read_descriptor,
    )
    from mai.capabilities.dispatch import (
        CapabilityCall,
        CapabilityDispatcher,
        ExecutorResult,
    )
    from mai.capabilities.host_shell import (
        HostShellCapability,
        descriptor as host_shell_descriptor,
    )
    from mai.capabilities.logs import (
        HostLogsCapability,
        descriptor as host_logs_descriptor,
    )
    from mai.contracts.server import parse_server_profile_text
    from mai.credentials.env import EnvResolver
    from mai.database import create_app_engine
    from mai.execution.ssh import SshIdentity, pin_host_key
    from mai.jobs.engine import EngineClient
    from mai.jobs.resource_locks import ResourceLockService
    from mai.plugins.registry import CapabilityRegistry
    from mai.policy.approvals import ApprovalStore
    from mai.policy.config import GrantConfigStore
    from mai.policy.evaluate import Policy
    from mai.profiles.files import ProfileFileStore

    data_root = Path(data_root_raw).expanduser()
    models_root = data_root / "models"
    intents_dir = data_root / "intents"
    known_hosts_dir = data_root / "known_hosts"
    artifacts_dir = data_root / "artifacts"
    for folder in (models_root, intents_dir, known_hosts_dir, artifacts_dir):
        folder.mkdir(parents=True, exist_ok=True)

    _, sessions = create_app_engine(database_url)
    profile_store = ProfileFileStore(data_root)
    grant_store = GrantConfigStore(data_root)
    env_file = os.environ.get("MAI_ENV_FILE", "").strip()

    def _load_profile(server_id: uuid.UUID):
        stored = profile_store.read(server_id)
        return parse_server_profile_text(stored.text)

    def _known_hosts_for(server_id: uuid.UUID) -> Path:
        prof = _load_profile(server_id)
        target = known_hosts_dir / f"{server_id}.known_hosts"
        pin_host_key(
            SshIdentity(
                host=prof.ssh.host,
                port=prof.ssh.port,
                user=prof.ssh.user,
                host_key=prof.ssh.host_key,
                credential_ref=prof.ssh.credential_ref,
            ),
            target,
        )
        return target

    host_shell_cap = HostShellCapability(
        profiles=_load_profile,
        known_hosts=_known_hosts_for,
        receipt_root="/tmp/mai-receipts",
        intents_dir=intents_dir,
    )
    host_logs_cap = HostLogsCapability(
        profiles=_load_profile,
        known_hosts=_known_hosts_for,
        artifacts_root=artifacts_dir,
    )
    chat_read_cap = ChatReadSessionCapability(sessions)

    registry = CapabilityRegistry()
    for desc in (
        host_shell_descriptor(),
        host_logs_descriptor(),
        chat_read_descriptor(),
    ):
        registry.register(desc)

    executors = {
        "host.shell": host_shell_cap,
        "host.logs": host_logs_cap,
        "chat.read_session": chat_read_cap,
    }

    async def _composite_executor(call: CapabilityCall) -> ExecutorResult:
        cid = call.descriptor.identifier
        target = executors.get(cid)
        if target is None:
            return ExecutorResult(
                ok=False,
                output=f"capability {cid!r} has no runtime executor",
            )
        return await target(call)

    def _read_grants():
        grants: dict[str, set[uuid.UUID]] = {}
        try:
            stored_cfg = grant_store.read()
            for cid, sids in stored_cfg.grants.items():
                grants.setdefault(cid, set()).update(sids)
        except Exception:
            pass
        # Also include capabilities declared directly on each server's profile.yaml
        try:
            servers_dir = data_root / "servers"
            if servers_dir.is_dir():
                for child in servers_dir.iterdir():
                    if not child.is_dir():
                        continue
                    try:
                        sid = uuid.UUID(child.name)
                        prof = _load_profile(sid)
                        for cap_slug in prof.capabilities:
                            normalized = cap_slug.replace("_", ".")
                            grants.setdefault(cap_slug, set()).add(sid)
                            grants.setdefault(normalized, set()).add(sid)
                        # Always allow host.shell and host.logs if host_shell / journald_logs present
                        if "host_shell" in prof.capabilities or "host.shell" in prof.capabilities:
                            grants.setdefault("host.shell", set()).add(sid)
                        if "journald_logs" in prof.capabilities or "host.logs" in prof.capabilities:
                            grants.setdefault("host.logs", set()).add(sid)
                    except Exception:
                        continue
        except Exception:
            pass
        return {cid: frozenset(sids) for cid, sids in grants.items()}

    def _read_policy() -> Policy:
        try:
            stored_cfg = grant_store.read()
            return stored_cfg.policy
        except Exception:
            return Policy(revision=1, denied_capabilities=frozenset())

    def _is_adopted(server_id: uuid.UUID) -> bool:
        if server_id == uuid.UUID(int=0):
            return True
        try:
            profile_store.read(server_id)
            return True
        except Exception:
            return False

    def _resolve_env() -> EnvResolver:
        if env_file and Path(env_file).is_file():
            return EnvResolver(env_file)
        fallback = data_root / ".env"
        if not fallback.is_file():
            fallback.write_text("", encoding="utf-8")
            fallback.chmod(0o600)
        return EnvResolver(fallback)

    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(sessions),
        grants=_read_grants,
        policy=_read_policy,
        adopted=_is_adopted,
        credentials=_resolve_env,
        executor=_composite_executor,
    )
    model_store = ModelConfigStore(
        models_root if models_root.is_dir() else data_root
    )
    resolver = _resolve_env()
    agent_executor = agent_run.AgentRunExecutor(
        sessions=sessions,
        dispatcher=dispatcher,
        registry=registry,
        model_store=model_store,
        data_root=data_root,
        locks=ResourceLockService(database_url),
        resolver=resolver,
    )
    agent_run.configure_agent_run(agent_executor)
    agent_delegate.configure_delegation(
        agent_delegate.DelegationExecutor(
            sessions=sessions,
            executor=agent_executor,
            client=EngineClient(database_url),
            ledger=BudgetLedger(sessions),
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one worker process until ``SIGTERM``/``SIGINT``."""
    parser = argparse.ArgumentParser(
        prog="python -m mai.jobs.worker",
        description="Mai durable-job worker (DBOS engine, C05).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=engine.DEFAULT_QUEUE_CONCURRENCY,
        help=f"per-worker queue concurrency (default {engine.DEFAULT_QUEUE_CONCURRENCY})",
    )
    ns = parser.parse_args(argv)

    try:
        database_url = resolve_database_url()
    except Exception as exc:  # noqa: BLE001 - value-free config failure
        print(
            f"mai worker: MAI_DATABASE_URL is not usable: {type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    handler_spec = os.environ.get("MAI_WORKER_HANDLERS", "")
    rc = _import_handlers(handler_spec)
    if rc != 0:
        return rc

    try:
        _configure_default_agent_runtime(database_url)
    except Exception as exc:  # noqa: BLE001 - log non-fatal runtime init warning
        print(
            f"mai worker: agent runtime init warning: {type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    try:
        engine.launch_worker(database_url, queue_concurrency=ns.concurrency)
    except Exception as exc:  # noqa: BLE001 - value-free startup failure
        print(
            f"mai worker: engine launch failed: {type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    print("mai worker: ready", file=sys.stderr, flush=True)
    _STOP.wait()
    engine.shutdown()
    print("mai worker: stopped", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
