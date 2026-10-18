"""Focused whole-instance backup/restore tests (card 12-backup-restore, P12).

Anchors: brief ``mai_12-backup-restore`` acceptances 1–2; contracts.md
C02 rule 3 (traversal/symlink refusal), C03 rule 6 (credential NAMES
travel, VALUES never), C05 guard rules 8–9 (single pg_dump, deliberate
recovery — restore never launches an engine), C07 rule 3 (banks dumped,
never rebuilt; derived index converges from canonical files via the
durable reindex outbox), C10 rules 1–2 (evidence + fixture namespace);
scenario row BACKUP-RESTORE; the 12-cli-ops contract that the script
drives the REAL ``python -m mai.cli backup|restore`` producer and the
reviewed ``mai.credentials.env`` parser, never a second implementation.

Lanes (test policy: focused checks, deterministic fakes disclosed):

- Static lane: ``bash -n``, usage contract and every no-docker refusal
  path (missing roots/files, existing output, missing/junk archive,
  hand-built bundles proving tamper/traversal refusal BEFORE any effect).
- Real-boundary lane (docker): disposable ``pgvector`` fixtures under the
  ``mai-fxt-12-backup-restore`` namespace. Source cluster gets the REAL
  Alembic chain 0001..0007 and the REAL engine schema (``dbos.DBOS.migrate``
  — the reviewed producer, not a hand-drawn stand-in) plus one authored
  PENDING workflow row, one memory-bank table and a canonical root built
  through the real producers. Backup → verify → restore into a FRESH
  container + clean root, then byte/count parity for every user table and
  the untouched-PENDING invariant. The derived-index rebuild runs the REAL
  headless producer (``mai.knowledge.index reconcile``) against a stub
  OpenAI-compatible embeddings endpoint (protocol fake, disclosed).

LIMITS (disclosed): the ``hindsight`` database content here is a
fixture-authored table standing in for hindsight's own startup migrations
(those belong to the hindsight image; this card owns the pg_dump
boundary). The stub embeddings endpoint proves reconcile plumbing and the
C02 W2 convergence, not embedding quality (that is KB-E1/07-verify). No
worker process is ever started here, so "no auto-rerun" is proven as the
PENDING row surviving byte-identical plus ``engine_auto_start: false``,
backed by the engine's own launch-recovery semantics (rule 8, exp7).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import tarfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "backup-restore.sh"
VENV_PY = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"

CARD = "12-backup-restore"
NS = f"mai-fxt-{CARD}"
IMAGE = "pgvector/pgvector:0.8.1-pg18"

SERVER_ID = uuid.UUID("4c2f1b6a-90e3-4d58-8a71-2f6c95d0b4a1")
DOC_ID = uuid.UUID("b7e2d4c6-1538-49af-9d02-63a8f1c4e5b7")

#: fixture-only sentinels: their BYTES must never appear in any archive
#: member (C03 rule 6); their NAMES must appear in env-names.txt.
PG_PASSWORD = "mai-fxt-pg-pw-e8a1c7d3"
EMBED_KEY = "mai-fxt-embed-key-b2c4f9a8"
UNUSED_SECRET = "mai-fxt-unused-secret-z9d4"
ENV_NAMES = [
    "MAI_EMBED_KEY",
    "MAI_POSTGRES_PASSWORD",
    "MAI_SSH_KEY_BUILD_HOST",
    "SEARXNG_SECRET",
]
ENV_FILE_TEXT = (
    f"MAI_POSTGRES_PASSWORD={PG_PASSWORD}\n"
    f"MAI_SSH_KEY_BUILD_HOST=ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfxtKeyFixture\n"
    f"MAI_EMBED_KEY={EMBED_KEY}\n"
    f"SEARXNG_SECRET={UNUSED_SECRET}\n"
)
SENTINELS = (PG_PASSWORD.encode(), EMBED_KEY.encode(), UNUSED_SECRET.encode())

PROFILE_TEXT = f"""\
schema_version: 1
server_id: "{SERVER_ID}"
name: "build-host"
prompt_revision: 3
environment: linux_ssh
ssh:
  host: "198.51.100.10"
  port: 22
  user: "deploy"
  host_key: "SHA256:ZmFxTWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+zS4"
  credential_ref: "MAI_SSH_KEY_BUILD_HOST"
privilege: none
capabilities:
  - shell.exec
  - files.read
"""

DOC_TEXT = (
    "# fixture runbook\n\ncanonical body of the boundary-gap document "
    "written after the database snapshot window.\n"
)
PENDING_WF = "wf-fixture-pending-0001"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _script_env(container: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("MAI_BACKUP_CLI", None)
    env.pop("MAI_BACKUP_PG_CONTAINER", None)
    if container is not None:
        env["MAI_BACKUP_PG_CONTAINER"] = container
    return env


def run_script(
    args: list[str], container: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=600,
        env=_script_env(container),
    )


def venv_python(
    *args: str, cwd: Path | None = None, **kw: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(VENV_PY), *args],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(cwd) if cwd else None,
        **kw,
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=300, check=check
    )


def psql(container: str, database: str, sql: str) -> str:
    proc = docker(
        "exec",
        container,
        "psql",
        "--username=mai",
        "--set=ON_ERROR_STOP=1",
        "--tuples-only",
        "--no-align",
        "--dbname",
        database,
        "--command",
        sql,
    )
    return proc.stdout.strip()


def pg_url(port: int, database: str = "mai") -> str:
    return f"postgresql://mai:{PG_PASSWORD}@127.0.0.1:{port}/{database}"


def root_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def bundle_members(archive: Path) -> dict[str, bytes]:
    """Decompressed member bytes of a backup bundle, keyed by member path
    relative to the bundle root (``postgres/…``, ``files/…``)."""
    out: dict[str, bytes] = {}
    with tarfile.open(archive, "r:gz") as outer:
        for member in outer.getmembers():
            if not member.isfile():
                continue
            handle = outer.extractfile(member)
            assert handle is not None
            data = handle.read()
            if member.name.endswith(".gz"):
                data = gzip.decompress(data)
            rel = member.name.split("/", 1)[1]
            out[rel] = data
    return out


def make_bundle(
    tmp: Path,
    members: dict[str, bytes],
    *,
    sheet_lines: list[str],
    manifest_extra: dict | None = None,
    member_corruption: dict[str, int] | None = None,
) -> Path:
    """Hand-build a mai-backup bundle for verify-path negatives (no docker)."""
    staging = tmp / "hand-bundle"
    bundle = staging / "mai-backup"
    bundle.mkdir(parents=True, exist_ok=True)
    for name, payload in members.items():
        target = bundle / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    sheet = bundle / "SHA256SUMS"
    sheet.write_text("".join(f"{line}\n" for line in sheet_lines), encoding="utf-8")
    sums_hash = hashlib.sha256(sheet.read_bytes()).hexdigest()
    manifest = {
        "format": 1,
        "created_at": "2026-10-18T00:00:00Z",
        "bundle": "mai-backup",
        "producer": "fixture",
        "sha256sums": sums_hash,
        "components": [],
        **(manifest_extra or {}),
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    for name, offset in (member_corruption or {}).items():
        data = bytearray((bundle / name).read_bytes())
        data[offset] ^= 0x5A
        (bundle / name).write_bytes(bytes(data))
    archive = tmp / "hand.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(staging, arcname=".")
    return archive


def _expected_sheet(members: dict[str, bytes]) -> list[str]:
    return [
        f"{hashlib.sha256(payload).hexdigest()}  {name}"
        for name, payload in sorted(members.items())
    ]


GOOD_MEMBERS = {
    "postgres/mai.sql.gz": gzip.compress(b"-- fixture dump\nSELECT 1;\n"),
    "postgres/hindsight.sql.gz": gzip.compress(b"-- fixture memory dump\n"),
    "files/data-root.tar.gz": b"not-a-real-inner-archive",
    "config/env-names.txt": "\n".join(ENV_NAMES).encode() + b"\n",
    "config/stack.json": b'{"project": "fixture"}\n',
}


# ---------------------------------------------------------------------------
# static lane — usage contract and docker-free refusals
# ---------------------------------------------------------------------------


def test_script_parses_and_prints_usage() -> None:
    assert (
        subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0
    )
    proc = run_script([])
    assert proc.returncode == 2
    for word in ("backup", "verify", "restore"):
        assert word in proc.stderr


def test_unknown_subcommand_is_usage_error() -> None:
    proc = run_script(["frobnicate"])
    assert proc.returncode == 2
    assert proc.stdout == ""


@pytest.mark.parametrize("missing", ["--data-root", "--env-file", "--output"])
def test_backup_requires_every_argument(missing: str) -> None:
    args = [
        "backup",
        "--data-root",
        "/tmp/x",
        "--env-file",
        "/tmp/e",
        "--output",
        "/tmp/o.tar.gz",
    ]
    index = args.index(missing)
    del args[index : index + 2]
    proc = run_script(args)
    assert proc.returncode == 2


def test_backup_refuses_missing_data_root(tmp_path: Path) -> None:
    out = tmp_path / "a.tar.gz"
    proc = run_script(
        [
            "backup",
            "--data-root",
            str(tmp_path / "nope"),
            "--env-file",
            str(tmp_path / "e.env"),
            "--output",
            str(out),
        ]
    )
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"]["code"] == "data_root_missing"
    assert not out.exists()


def test_backup_refuses_missing_env_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    proc = run_script(
        [
            "backup",
            "--data-root",
            str(root),
            "--env-file",
            str(tmp_path / "nope.env"),
            "--output",
            str(tmp_path / "a.tar.gz"),
        ]
    )
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"]["code"] == "env_file_missing"


def test_backup_refuses_existing_output(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    env_file = tmp_path / "e.env"
    env_file.write_text(ENV_FILE_TEXT)
    out = tmp_path / "a.tar.gz"
    out.write_bytes(b"x")
    proc = run_script(
        [
            "backup",
            "--data-root",
            str(root),
            "--env-file",
            str(env_file),
            "--output",
            str(out),
        ]
    )
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"]["code"] == "output_exists"


def test_verify_refuses_missing_archive(tmp_path: Path) -> None:
    proc = run_script(["verify", str(tmp_path / "nope.tar.gz")])
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"]["code"] == "archive_missing"


def test_verify_refuses_non_gzip_archive(tmp_path: Path) -> None:
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"definitely not gzip" * 10)
    proc = run_script(["verify", str(junk)])
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"]["code"] == "archive_invalid"


def test_verify_accepts_a_wellformed_hand_built_bundle(tmp_path: Path) -> None:
    archive = make_bundle(
        tmp_path, GOOD_MEMBERS, sheet_lines=_expected_sheet(GOOD_MEMBERS)
    )
    proc = run_script(["verify", str(archive)])
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["verify"] is True
    assert report["members"] == len(GOOD_MEMBERS)


def test_verify_refuses_traversal_member_path(tmp_path: Path) -> None:
    sheet = _expected_sheet(GOOD_MEMBERS) + ["0" * 64 + "  ../escape"]
    archive = make_bundle(tmp_path, GOOD_MEMBERS, sheet_lines=sheet)
    proc = run_script(["verify", str(archive)])
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"]["code"] == "unsafe_path"
    assert not (tmp_path / "escape").exists()


def test_verify_refuses_tampered_member(tmp_path: Path) -> None:
    archive = make_bundle(
        tmp_path,
        GOOD_MEMBERS,
        sheet_lines=_expected_sheet(GOOD_MEMBERS),
        member_corruption={"postgres/mai.sql.gz": 3},
    )
    proc = run_script(["verify", str(archive)])
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"]["code"] == "checksum_mismatch"


def test_verify_refuses_tampered_sheet(tmp_path: Path) -> None:
    lines = _expected_sheet(GOOD_MEMBERS)
    lines[0] = "f" * 64 + lines[0][64:]
    archive = make_bundle(tmp_path, GOOD_MEMBERS, sheet_lines=lines)
    proc = run_script(["verify", str(archive)])
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"]["code"] == "checksum_mismatch"


def test_verify_refuses_unsupported_manifest_format(tmp_path: Path) -> None:
    archive = make_bundle(
        tmp_path,
        GOOD_MEMBERS,
        sheet_lines=_expected_sheet(GOOD_MEMBERS),
        manifest_extra={"format": 999},
    )
    proc = run_script(["verify", str(archive)])
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"]["code"] == "archive_invalid"


def test_restore_usage_requires_archive_and_root(tmp_path: Path) -> None:
    proc = run_script(["restore"])
    assert proc.returncode == 2
    proc = run_script(["restore", str(tmp_path / "a.tar.gz")])
    assert proc.returncode == 2


# ---------------------------------------------------------------------------
# real-boundary lane — disposable pgvector fixtures (mai-fxt namespace)
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _await_postgres(name: str) -> None:
    """Wait until a REAL query succeeds twice, one second apart.

    The postgres image starts a temporary server for initdb and restarts;
    ``pg_isready`` alone can green-light inside that window (observed as
    alembic dying with "server closed the connection unexpectedly"), so the
    probe needs two consecutive ``SELECT 1`` hits with a gap between them.
    """
    hits = 0
    for _ in range(120):
        probe = docker(
            "exec",
            name,
            "psql",
            "--username=mai",
            "--dbname=mai",
            "--tuples-only",
            "--no-align",
            "--command",
            "SELECT 1",
            check=False,
        )
        hits = hits + 1 if probe.returncode == 0 else 0
        if hits >= 2:
            return
        time.sleep(0.5)
    docker("rm", "-f", name, check=False)
    pytest.fail(f"fixture postgres never became ready: {name}")


@pytest.fixture(scope="module")
def pg_source() -> dict[str, object]:
    if not _docker_available():
        pytest.skip("docker CLI not available")
    name = f"{NS}-src"
    port = free_port()
    docker("rm", "-f", name, check=False)
    docker(
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-e",
        f"POSTGRES_PASSWORD={PG_PASSWORD}",
        "-e",
        "POSTGRES_USER=mai",
        "-e",
        "POSTGRES_DB=mai",
        "-p",
        f"127.0.0.1:{port}:5432",
        IMAGE,
    )
    _await_postgres(name)
    yield {"name": name, "port": port}
    docker("rm", "-f", name, check=False)


@pytest.fixture(scope="module")
def pg_target() -> dict[str, object]:
    if not _docker_available():
        pytest.skip("docker CLI not available")
    name = f"{NS}-dst"
    port = free_port()
    docker("rm", "-f", name, check=False)
    docker(
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-e",
        f"POSTGRES_PASSWORD={PG_PASSWORD}",
        "-e",
        "POSTGRES_USER=mai",
        "-e",
        "POSTGRES_DB=mai",
        "-p",
        f"127.0.0.1:{port}:5432",
        IMAGE,
    )
    _await_postgres(name)
    yield {"name": name, "port": port}
    docker("rm", "-f", name, check=False)


def _seed_source_state(pg: dict[str, object], data_root: Path, env_file: Path) -> None:
    name, port = str(pg["name"]), int(pg["port"])  # type: ignore[arg-type]
    # Real Alembic chain 0001..0007 against the fixture cluster.
    env = dict(os.environ)
    env["MAI_DATABASE_URL"] = pg_url(port)
    mig = subprocess.run(
        [str(VENV_PY), "-m", "alembic", "-c", str(ALEMBIC_INI), "upgrade", "head"],
        cwd=REPO_ROOT / "backend",
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    assert mig.returncode == 0, mig.stderr[-2000:]
    # Real engine-owned dbos schema via the reviewed producer entrypoint.
    migrate = venv_python(
        "-c",
        "import sys; from dbos import DBOS; DBOS.migrate(sys.argv[1])",
        pg_url(port),
    )
    assert migrate.returncode == 0, migrate.stderr[-2000:]
    # One authored PENDING workflow row: restore must keep it exactly as-is.
    psql(
        name,
        "mai",
        f"INSERT INTO dbos.workflow_status (workflow_uuid, status, name, "
        f"application_name, application_version, queue_name, created_at, "
        f"updated_at, recovery_attempts, inputs, serialization) VALUES "
        f"('{PENDING_WF}', 'PENDING', 'fixture.dangerous_step', 'mai', "
        f"'mai-1', 'mai-jobs', 1, 1, 0, '{{}}', 'json')",
    )
    assert (
        psql(
            name,
            "mai",
            f"SELECT status FROM dbos.workflow_status "
            f"WHERE workflow_uuid='{PENDING_WF}'",
        )
        == "PENDING"
    )
    # Dedicated memory database (C07 rule 3): fixture-authored stand-in for
    # hindsight's own startup migrations (disclosed; the pg_dump boundary is
    # what this card proves).
    docker("exec", name, "createdb", "--username=mai", "--owner=mai", "hindsight")
    psql(name, "hindsight", "CREATE TABLE banks (id int primary key, label text)")
    psql(name, "hindsight", "INSERT INTO banks VALUES (1, 'mai-smoke'), (2, 'shared')")
    # Canonical root through the REAL producer (12-cli-ops); the source
    # profile document lives OUTSIDE the root (the store owns the root).
    profile = data_root.parent / "controller-profile.yaml"
    profile.write_text(PROFILE_TEXT, encoding="utf-8")
    boot = venv_python(
        "-m",
        "mai.cli",
        "bootstrap",
        "--data-root",
        str(data_root),
        "--server-id",
        str(SERVER_ID),
        "--profile",
        str(profile),
    )
    assert boot.returncode == 0, boot.stderr
    # One canonical document whose file+intent exist but whose derived
    # chunks do NOT exist yet (the documented files-vs-DB boundary gap:
    # written after any DB snapshot, intent journaled, reconcile pending).
    sys_path = str(REPO_ROOT / "backend" / "src")
    code = (
        "import sys, uuid; sys.path.insert(0, sys.argv[4])\n"
        "from mai.knowledge.documents import DocumentStore, DocumentNamespace\n"
        "store = DocumentStore(sys.argv[1])\n"
        "stored = store.write(uuid.UUID(sys.argv[2]), sys.argv[3],\n"
        "                     server_id=uuid.UUID(sys.argv[2]),\n"
        "                     namespace=DocumentNamespace.DOCS, expected_revision=0)\n"
        "print('written', stored.revision)\n"
    )
    wrote = subprocess.run(
        [str(VENV_PY), "-c", code, str(data_root), str(SERVER_ID), DOC_TEXT, sys_path],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert wrote.returncode == 0, wrote.stderr
    env_file.write_text(ENV_FILE_TEXT, encoding="utf-8")
    env_file.chmod(0o600)


@pytest.fixture(scope="module")
def fixture_state(
    pg_source: dict[str, object], tmp_path_factory: pytest.TempPathFactory
) -> dict[str, object]:
    tmp = tmp_path_factory.mktemp(f"{NS}")
    data_root = tmp / "source-root"
    env_file = tmp / "controller.env"
    _seed_source_state(pg_source, data_root, env_file)
    return {
        "tmp": tmp,
        "pg": pg_source,
        "data_root": data_root,
        "env_file": env_file,
        "hashes": root_hashes(data_root),
    }


@pytest.fixture(scope="module")
def backup_archive(fixture_state: dict[str, object]) -> dict[str, object]:
    state = fixture_state
    out = Path(str(state["tmp"])) / "backup-1.tar.gz"
    proc = run_script(
        [
            "backup",
            "--data-root",
            str(state["data_root"]),
            "--env-file",
            str(state["env_file"]),
            "--output",
            str(out),
        ],
        container=str(state["pg"]["name"]),
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["backup"] is True
    return {"archive": out, "report": report}


def test_backup_report_is_checksum_pinned(backup_archive: dict[str, object]) -> None:
    archive = Path(str(backup_archive["archive"]))
    report = dict(backup_archive["report"])  # type: ignore[arg-type]
    assert report["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert report["databases"] == ["mai", "hindsight"]
    assert archive.stat().st_size == report["bytes"] > 0


def test_backup_dumps_are_deterministic(
    fixture_state: dict[str, object], backup_archive: dict[str, object]
) -> None:
    state = fixture_state
    second = Path(str(state["tmp"])) / "backup-2.tar.gz"
    proc = run_script(
        [
            "backup",
            "--data-root",
            str(state["data_root"]),
            "--env-file",
            str(state["env_file"]),
            "--output",
            str(second),
        ],
        container=str(state["pg"]["name"]),
    )
    assert proc.returncode == 0, proc.stderr
    first_members = bundle_members(Path(str(backup_archive["archive"])))
    second_members = bundle_members(second)
    for member in (
        "postgres/mai.sql.gz",
        "postgres/hindsight.sql.gz",
        "config/env-names.txt",
    ):
        assert first_members[member] == second_members[member], member


def test_archive_carries_names_never_values(backup_archive: dict[str, object]) -> None:
    archive = Path(str(backup_archive["archive"]))
    members = bundle_members(archive)
    names = members["config/env-names.txt"].decode().splitlines()
    assert names == ENV_NAMES
    blobs: list[bytes] = list(members.values())
    with tarfile.open(
        fileobj=io.BytesIO(members["files/data-root.tar.gz"]), mode="r:"
    ) as inner:
        for item in inner.getmembers():
            if item.isfile():
                handle = inner.extractfile(item)
                assert handle is not None
                blobs.append(handle.read())
    for sentinel in SENTINELS:
        for blob in blobs:
            assert sentinel not in blob, f"secret material leaked: {sentinel[:12]}…"


def test_restore_into_fresh_instance_preserves_everything(
    fixture_state: dict[str, object],
    backup_archive: dict[str, object],
    pg_target: dict[str, object],
) -> None:
    state = fixture_state
    archive = Path(str(backup_archive["archive"]))
    assert archive.exists()
    target_root = Path(str(state["tmp"])) / "restored-root"
    proc = run_script(
        ["restore", str(archive), "--data-root", str(target_root)],
        container=str(pg_target["name"]),
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["restore"] is True
    assert report["engine_auto_start"] is False
    # File half: identical file set and identical bytes (profiles, prompts,
    # canonical docs, durable reindex intents).
    assert root_hashes(target_root) == dict(state["hashes"])  # type: ignore[arg-type]
    # Database half: row-count parity for every user table of every schema.
    src, dst = str(state["pg"]["name"]), str(pg_target["name"])
    for schema in ("public", "dbos"):
        tables = psql(
            src,
            "mai",
            f"SELECT tablename FROM pg_catalog.pg_tables "
            f"WHERE schemaname='{schema}' ORDER BY 1",
        ).splitlines()
        assert tables, f"no {schema} tables captured by the dump"
        for table in tables:
            left = psql(src, "mai", f"SELECT count(*) FROM {schema}.{table}")
            right = psql(dst, "mai", f"SELECT count(*) FROM {schema}.{table}")
            assert left == right, f"{schema}.{table}: {left} != {right}"
    assert psql(dst, "hindsight", "SELECT count(*) FROM banks") == "2"
    # Engine rows: identity + untouched status (nothing auto-reran).
    row = psql(
        dst,
        "mai",
        f"SELECT status || '|' || recovery_attempts || '|' || "
        f"application_version FROM dbos.workflow_status "
        f"WHERE workflow_uuid='{PENDING_WF}'",
    )
    assert row == "PENDING|0|mai-1"
    # Source instance untouched (acceptance: source never disturbed).
    assert root_hashes(Path(str(state["data_root"]))) == dict(state["hashes"])  # type: ignore[arg-type]
    assert psql(src, "hindsight", "SELECT count(*) FROM banks") == "2"


def test_restore_refuses_non_fresh_target(
    backup_archive: dict[str, object],
    fixture_state: dict[str, object],
    pg_target: dict[str, object],
) -> None:
    """Order-independent: the guard must fire whatever ran before it."""
    archive = Path(str(backup_archive["archive"]))
    used_root = Path(str(fixture_state["tmp"])) / "used-target-root"
    first = run_script(
        ["restore", str(archive), "--data-root", str(used_root)],
        container=str(pg_target["name"]),
    )
    if first.returncode != 0:
        # A earlier test already used the target: the refusal IS the proof.
        assert json.loads(first.stderr)["error"]["code"] == "database_not_fresh"
        assert not used_root.exists()
        return
    refused_root = Path(str(fixture_state["tmp"])) / "refused-target-root"
    proc = run_script(
        ["restore", str(archive), "--data-root", str(refused_root)],
        container=str(pg_target["name"]),
    )
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"]["code"] == "database_not_fresh"
    assert not refused_root.exists()


def test_restore_refuses_tampered_archive_before_any_effect(
    backup_archive: dict[str, object],
    tmp_path: Path,
) -> None:
    """A bundle byte-flip must stop at verify: fresh target stays empty."""
    fresh = f"{NS}-tam"
    docker("rm", "-f", fresh, check=False)
    docker(
        "run",
        "-d",
        "--rm",
        "--name",
        fresh,
        "-e",
        f"POSTGRES_PASSWORD={PG_PASSWORD}",
        "-e",
        "POSTGRES_USER=mai",
        "-e",
        "POSTGRES_DB=mai",
        IMAGE,
    )
    try:
        _await_postgres(fresh)
        staging = tmp_path / "tamper-extract"
        with tarfile.open(str(Path(str(backup_archive["archive"]))), "r:gz") as tar:
            tar.extractall(staging)
        member = staging / "mai-backup" / "postgres" / "mai.sql.gz"
        data = bytearray(member.read_bytes())
        data[len(data) // 2] ^= 0x5A
        member.write_bytes(bytes(data))
        tampered = tmp_path / "tampered.tar.gz"
        with tarfile.open(tampered, "w:gz") as tar:
            tar.add(staging, arcname=".")
        root = tmp_path / "never-created"
        proc = run_script(
            ["restore", str(tampered), "--data-root", str(root)], container=fresh
        )
        assert proc.returncode == 1
        assert json.loads(proc.stderr)["error"]["code"] == "checksum_mismatch"
        assert not root.exists()
        assert (
            psql(
                fresh,
                "mai",
                "SELECT count(*) FROM pg_catalog.pg_tables WHERE "
                "schemaname NOT IN ('pg_catalog','information_schema')",
            )
            == "0"
        )
    finally:
        docker("rm", "-f", fresh, check=False)


def test_restore_refuses_tampered_archive_even_without_postgres(tmp_path: Path) -> None:
    """No docker/compose at all: the tamper refusal still fires first."""
    lines = _expected_sheet(GOOD_MEMBERS)
    archive = make_bundle(
        tmp_path,
        GOOD_MEMBERS,
        sheet_lines=lines,
        member_corruption={"config/env-names.txt": 0},
    )
    proc = run_script(["restore", str(archive), "--data-root", str(tmp_path / "r")])
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"]["code"] == "checksum_mismatch"


@pytest.fixture(scope="module")
def embeddings_endpoint() -> dict[str, object]:
    """Stub OpenAI-compatible /v1/embeddings: deterministic 1536-dim vectors."""
    import hashlib as _hashlib

    def vector_for(text: str) -> list[float]:
        digest = _hashlib.sha256(text.encode()).digest()
        return [digest[i % 32] / 255.0 for i in range(1536)]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib API
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            inputs = (
                body["input"] if isinstance(body["input"], list) else [body["input"]]
            )
            payload = {
                "data": [
                    {
                        "object": "embedding",
                        "index": i,
                        "embedding": vector_for(str(text)),
                    }
                    for i, text in enumerate(inputs)
                ],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args: object) -> None:  # silence
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"port": server.server_address[1]}
    server.shutdown()


def test_reconcile_rebuilds_derived_index_from_restored_files(
    backup_archive: dict[str, object],
    fixture_state: dict[str, object],
    tmp_path: Path,
    embeddings_endpoint: dict[str, object],
) -> None:
    """Restore again into a fresh target; reconcile converges the index."""
    fresh = f"{NS}-idx"
    port = free_port()
    docker("rm", "-f", fresh, check=False)
    docker(
        "run",
        "-d",
        "--rm",
        "--name",
        fresh,
        "-e",
        f"POSTGRES_PASSWORD={PG_PASSWORD}",
        "-e",
        "POSTGRES_USER=mai",
        "-e",
        "POSTGRES_DB=mai",
        "-p",
        f"127.0.0.1:{port}:5432",
        IMAGE,
    )
    try:
        _await_postgres(fresh)
        archive = Path(str(backup_archive["archive"]))
        root = tmp_path / "reconciled-root"
        proc = run_script(
            ["restore", str(archive), "--data-root", str(root)], container=fresh
        )
        assert proc.returncode == 0, proc.stderr
        store_module = "mai.knowledge.index"
        env = _script_env()
        env["MAI_DATABASE_URL"] = pg_url(port)
        rec = subprocess.run(
            [
                str(VENV_PY),
                "-m",
                store_module,
                "reconcile",
                str(root),
                "--server",
                str(SERVER_ID),
                "--base-url",
                f"http://127.0.0.1:{embeddings_endpoint['port']}",
                "--credential-ref",
                "MAI_EMBED_KEY",
                "--env-file",
                str(fixture_state["env_file"]),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=600,
            cwd=str(REPO_ROOT),
        )
        assert rec.returncode == 0, rec.stderr[-2000:]
        intents = [
            json.loads(line) for line in rec.stdout.splitlines() if line.startswith("{")
        ]
        kinds = [item.get("kind") for item in intents]
        assert "intent" in kinds
        assert any(
            item.get("status") == "indexed"
            for item in intents
            if item.get("kind") == "intent"
        )
        # The boundary-gap document now has real chunk rows in the restored
        # cluster, and its durable intent is purged (C02 W4 ack-after).
        chunks = psql(
            fresh,
            "mai",
            "SELECT count(*) FROM knowledge_documents d JOIN "
            "knowledge_doc_chunks c ON c.document_key = d.document_key "
            "WHERE d.source_path LIKE 'servers/%'",
        )
        assert int(chunks) >= 1
        leftover = subprocess.run(
            [
                str(VENV_PY),
                "-c",
                "import sys, uuid; sys.path.insert(0, sys.argv[3]); "
                "from mai.knowledge.documents import DocumentStore; "
                "print(len(DocumentStore(sys.argv[1]).read_intents("
                "server_id=uuid.UUID(sys.argv[2]))))",
                str(root),
                str(SERVER_ID),
                str(REPO_ROOT / "backend" / "src"),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert leftover.returncode == 0, leftover.stderr
        assert leftover.stdout.strip() == "0"
    finally:
        docker("rm", "-f", fresh, check=False)
