"""Dependency lane guard for the P07 knowledge embedding/fetch set (card 07-dependencies).

Acceptance criteria (card brief):
1. Only frozen chunk/embed/vector/fetch library choices added with supported
   APIs and service license notes.
2. No hidden vector service or unbounded browser install required without
   contract decision.

Selection provenance: docs/architecture/knowledge-packaging.md (§1 decision
summary, §2.1 packaging table, §4 embedding/vector store decision) frozen as
contracts.md C07. The doc index embeds via ``POST {base_url}/v1/embeddings``
on the SAME OpenAI-compatible connection as the chat path (C07 rule 7 — the
embeddings endpoint is a different API from chat completions; pydantic-ai
exposes no embeddings client, so the direct ``openai`` SDK surface below is
the selected client). Vectors live in the controller's own PostgreSQL+pgvector
(C07 rule 3: ``vector(1536)``, HNSW ``vector_cosine_ops`` — a dedicated
vector-database service is explicitly rejected, ADR §4.4), accessed through
the ``pgvector`` distribution's supported SQLAlchemy/psycopg adapters on the
locked psycopg3 application driver (src/mai/database.py). Page fetching is
MAI's own bounded HTTP capability (ADR §3.2) on ``httpx2`` — the client family
already locked as the openai SDK's transport — never browser automation
(criterion 2). Hindsight/SearXNG/Valkey enter as pinned Compose images owned
by platform cards (ADR §2.1: the bare-metal ``hindsight-api`` pip package is
rejected — it drags the whole memory engine into the FastAPI dependency set),
so every in-process Hindsight package is held out by the negative controls.
The exact API surface downstream cards (07-vector-schema, 07-chunk-index,
07-retrieval, 07-search-capability, 07-page-reader) will call is asserted
offline against the installed distributions (DDL compile, distance operator
compile, mock-transport request capture) — never invented, never dialed.
"""

import asyncio
import json
import re
import subprocess
import threading
import tomllib
from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]

# The three selected knowledge-lane requirements: distribution, manifest
# bounds (floor = the version whose API is verified below; cap = deliberate
# bump reconciliation, repo convention) and the locked version the API checks
# run against.
VECTOR_ACCESS_DISTRIBUTION = "pgvector"
VECTOR_ACCESS_FLOOR = ">=0.5"
VECTOR_ACCESS_CAP = "<0.6"
EMBEDDINGS_SDK_DISTRIBUTION = "openai"
EMBEDDINGS_SDK_FLOOR = ">=3.26"  # == pydantic-ai-slim[openai]'s own requirement
EMBEDDINGS_SDK_CAP = "<4"
FETCH_CLIENT_DISTRIBUTION = "httpx2"
FETCH_CLIENT_FLOOR = ">=2.13"
FETCH_CLIENT_CAP = "<3"

# C07 rule 3 dimension contract tuple, v1 (binding on the schema/retrieval
# producers; pinned here so the dependency lane records the contract the
# verified API surface below must serve).
EMBEDDING_PROVIDER = "openai-compatible"
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
DISTANCE_METRIC = "cosine"

# Dedicated vector-database clients (ADR §4.4: a separate vector service is
# an undocumented dependency addition contradicting the Postgres-default
# decision; distributions for manifest/lock checks, import names for the
# installed-environment check).
UNSELECTED_VECTOR_STORE_DISTRIBUTIONS = (
    "qdrant-client",
    "pymilvus",
    "weaviate-client",
    "chromadb",
    "lancedb",
    "faiss-cpu",
    "annoy",
    "hnswlib",
)
UNSELECTED_VECTOR_STORE_IMPORTS = (
    "qdrant_client",
    "pymilvus",
    "weaviate",
    "chromadb",
    "lancedb",
    "faiss",
    "annoy",
    "hnswlib",
)

# In-process Hindsight packages (ADR §2.1 packaging table: the Docker image
# ghcr.io/vectorize-io/hindsight is the selected packaging run as a Compose
# service; pip-installing the server into the controller drags Postgres
# drivers and ONNX extras into the FastAPI dependency set and duplicates a
# process the stack isolates).
UNSELECTED_HINDSIGHT_DISTRIBUTIONS = (
    "hindsight-api",
    "hindsight-all",
    "hindsight-client",
    "hindsight-litellm",
)
UNSELECTED_HINDSIGHT_IMPORTS = (
    "hindsight_api",
    "hindsight_client",
    "hindsight_litellm",
)

# Browser automation (criterion 2: page fetch is bounded HTTP — no hidden
# browser install without a contract decision).
UNSELECTED_BROWSER_AUTOMATION_DISTRIBUTIONS = ("playwright", "selenium", "pyppeteer")
UNSELECTED_BROWSER_AUTOMATION_IMPORTS = ("playwright", "selenium", "pyppeteer")

# Frozen lane pins owned by earlier dependency cards, asserted unchanged
# (contracts.md C05 engine freeze; P06 agent-runtime entry). The compaction
# harness was REMOVED at D09 — asserted ABSENT instead of frozen.
FROZEN_ENGINE_REQUIREMENT = "dbos==3.2.0"
FROZEN_ENGINE_VERSION = "3.2.0"
FROZEN_AGENT_RUNTIME_REQUIREMENT = "pydantic-ai-slim[openai]==2.60.0"


def _declared_requirements() -> list[str]:
    with (BACKEND_DIR / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return list(data["project"]["dependencies"])


def _distribution_names(requirements: list[str]) -> list[str]:
    """PEP 508 entry -> lowercase distribution name (markers/versions stripped)."""
    return [re.split(r"[<>=!~\[\s]", entry.split(";")[0].strip(), maxsplit=1)[0].lower()
            for entry in requirements]


def _requirements_for(requirements: list[str], distribution: str) -> list[str]:
    """Raw PEP 508 entries whose distribution name matches (extras preserved)."""
    return [entry for entry in requirements
            if re.split(r"[<>=!~\[\s]", entry.split(";")[0].strip(), maxsplit=1)[0].lower() == distribution]


def _locked_packages() -> dict[str, str]:
    with (BACKEND_DIR / "uv.lock").open("rb") as handle:
        return {package["name"].lower(): package["version"] for package in tomllib.load(handle)["package"]}


def _importable(name: str) -> bool:
    try:
        return find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        # Dotted name with a missing parent, or unparsable — not installed.
        return False


# ---------------------------------------------------------------------------
# persistent test loop (same discipline as tests/test_agent_run.py: no
# pytest-asyncio in the dev group)
# ---------------------------------------------------------------------------

_TEST_LOOP: asyncio.AbstractEventLoop | None = None
_TEST_LOOP_LOCK = threading.Lock()


def _run(coro: object) -> object:
    """Run one coroutine on the persistent test loop; block for the result."""
    global _TEST_LOOP
    with _TEST_LOOP_LOCK:
        if _TEST_LOOP is None:
            _TEST_LOOP = asyncio.new_event_loop()
            threading.Thread(target=_TEST_LOOP.run_forever, daemon=True).start()
    assert _TEST_LOOP is not None
    return asyncio.run_coroutine_threadsafe(coro, _TEST_LOOP).result()


# ---------------------------------------------------------------------------
# manifest / lock / environment shape
# ---------------------------------------------------------------------------


def test_manifest_declares_exactly_the_selected_knowledge_set() -> None:
    declared = _declared_requirements()
    for distribution, floor, cap in (
        (VECTOR_ACCESS_DISTRIBUTION, VECTOR_ACCESS_FLOOR, VECTOR_ACCESS_CAP),
        (EMBEDDINGS_SDK_DISTRIBUTION, EMBEDDINGS_SDK_FLOOR, EMBEDDINGS_SDK_CAP),
        (FETCH_CLIENT_DISTRIBUTION, FETCH_CLIENT_FLOOR, FETCH_CLIENT_CAP),
    ):
        entries = _requirements_for(declared, distribution)
        assert len(entries) == 1, f"expected exactly one {distribution} entry: {entries}"
        assert floor in entries[0], f"{distribution} floor {floor} missing: {entries[0]}"
        assert cap in entries[0], f"{distribution} cap {cap} missing: {entries[0]}"
        # No extras on the knowledge lane: the fetch client ships plain async
        # HTTP (cli/http2/socks/zstd/brotli extras unselected), the embedding
        # client needs no extras of its own.
        match = re.search(r"\[([^\]]*)\]", entries[0])
        assert match is None, f"{distribution} must be declared without extras: {entries[0]}"


def test_lock_resolves_the_selected_knowledge_set() -> None:
    locked = _locked_packages()
    for distribution in (
        VECTOR_ACCESS_DISTRIBUTION,
        EMBEDDINGS_SDK_DISTRIBUTION,
        FETCH_CLIENT_DISTRIBUTION,
    ):
        assert locked.get(distribution) is not None, f"{distribution} missing from uv.lock"
    # The fetch client and its HTTP/1.1 core resolve in lockstep (httpx2
    # declares httpcore2==2.13.1; a mismatched pair is a broken transport).
    assert locked["httpcore2"] == locked[FETCH_CLIENT_DISTRIBUTION], (
        f"httpcore2 {locked.get('httpcore2')} out of lockstep with "
        f"{FETCH_CLIENT_DISTRIBUTION} {locked[FETCH_CLIENT_DISTRIBUTION]}"
    )
    # The embeddings SDK stays at/above the floor the agent-runtime extra
    # documents (pydantic-ai-slim 2.56 metadata: openai>=3.26.0).
    openai_version = tuple(int(part) for part in locked[EMBEDDINGS_SDK_DISTRIBUTION].split(".")[:2])
    assert openai_version >= (3, 26), f"openai {openai_version} below the documented floor (3, 26)"


def test_lock_is_consistent_with_manifest() -> None:
    proc = subprocess.run(
        ["uv", "lock", "--check"], cwd=BACKEND_DIR, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"uv.lock out of sync with pyproject: {proc.stdout}{proc.stderr}"


def test_installed_knowledge_versions_match_lock() -> None:
    locked = _locked_packages()
    for distribution, module_version in (
        (VECTOR_ACCESS_DISTRIBUTION, version(VECTOR_ACCESS_DISTRIBUTION)),
        (EMBEDDINGS_SDK_DISTRIBUTION, version(EMBEDDINGS_SDK_DISTRIBUTION)),
        (FETCH_CLIENT_DISTRIBUTION, version(FETCH_CLIENT_DISTRIBUTION)),
    ):
        assert module_version == locked[distribution], (
            f"installed {distribution} {module_version} != locked {locked[distribution]}"
        )


def test_frozen_lane_pins_unchanged() -> None:
    # The knowledge lane must not silently retune earlier frozen lanes
    # (contracts.md C05; 04-dependencies / 06-dependencies entries).
    declared = _declared_requirements()
    engine = _requirements_for(declared, "dbos")
    assert engine == [FROZEN_ENGINE_REQUIREMENT], f"frozen engine pin changed: {engine}"
    agent_runtime = _requirements_for(declared, "pydantic-ai-slim")
    assert agent_runtime == [FROZEN_AGENT_RUNTIME_REQUIREMENT], (
        f"frozen agent-runtime entry changed: {agent_runtime}"
    )
    harness = _requirements_for(declared, "pydantic-ai-harness")
    assert harness == [], f"the removed compaction harness reappeared: {harness}"
    locked = _locked_packages()
    assert locked.get("dbos") == FROZEN_ENGINE_VERSION, (
        f"dbos {locked.get('dbos')} != frozen {FROZEN_ENGINE_VERSION}"
    )


# ---------------------------------------------------------------------------
# negative controls (criterion 2)
# ---------------------------------------------------------------------------


def test_manifest_declares_no_dedicated_vector_store_client() -> None:
    declared = _distribution_names(_declared_requirements())
    offenders = sorted(set(declared) & set(UNSELECTED_VECTOR_STORE_DISTRIBUTIONS))
    assert offenders == [], f"dedicated vector-store clients in pyproject: {offenders}"


def test_lock_contains_no_dedicated_vector_store_distribution() -> None:
    locked = _locked_packages()
    offenders = sorted(locked.keys() & set(UNSELECTED_VECTOR_STORE_DISTRIBUTIONS))
    assert offenders == [], f"dedicated vector-store clients in uv.lock: {offenders}"


def test_locked_environment_has_no_vector_store_client_importable() -> None:
    present = sorted(name for name in UNSELECTED_VECTOR_STORE_IMPORTS if _importable(name))
    assert present == [], f"vector-store clients importable in locked env: {present}"


def test_manifest_declares_no_inprocess_hindsight_package() -> None:
    declared = _distribution_names(_declared_requirements())
    offenders = sorted(set(declared) & set(UNSELECTED_HINDSIGHT_DISTRIBUTIONS))
    assert offenders == [], f"in-process Hindsight packages in pyproject: {offenders}"


def test_lock_contains_no_inprocess_hindsight_distribution() -> None:
    locked = _locked_packages()
    offenders = sorted(locked.keys() & set(UNSELECTED_HINDSIGHT_DISTRIBUTIONS))
    assert offenders == [], f"in-process Hindsight packages in uv.lock: {offenders}"


def test_locked_environment_has_no_inprocess_hindsight_importable() -> None:
    present = sorted(name for name in UNSELECTED_HINDSIGHT_IMPORTS if _importable(name))
    assert present == [], f"in-process Hindsight packages importable in locked env: {present}"


def test_manifest_and_lock_declare_no_browser_automation() -> None:
    declared = _distribution_names(_declared_requirements())
    offenders = sorted(set(declared) & set(UNSELECTED_BROWSER_AUTOMATION_DISTRIBUTIONS))
    assert offenders == [], f"browser automation in pyproject: {offenders}"
    locked = _locked_packages()
    offenders = sorted(locked.keys() & set(UNSELECTED_BROWSER_AUTOMATION_DISTRIBUTIONS))
    assert offenders == [], f"browser automation in uv.lock: {offenders}"
    present = sorted(name for name in UNSELECTED_BROWSER_AUTOMATION_IMPORTS if _importable(name))
    assert present == [], f"browser automation importable in locked env: {present}"


# ---------------------------------------------------------------------------
# verified API surface (criterion 1 — offline, never dialed)
# ---------------------------------------------------------------------------


def test_pgvector_sqlalchemy_and_psycopg_surface() -> None:
    # The exact producers 07-vector-schema (migration DDL) and
    # 07-chunk-index/07-retrieval (distance ordering) will call, compiled
    # offline against the locked psycopg dialect: not invented.
    from pgvector.psycopg import register_vector
    from pgvector.sqlalchemy import Vector
    from sqlalchemy import MetaData, Table, Column, Integer, select
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    assert callable(register_vector), "pgvector.psycopg adapter missing"

    metadata = MetaData()
    table = Table(
        "guard_chunks",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("embedding", Vector(EMBEDDING_DIMENSIONS)),
    )
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert re.search(rf"VECTOR\({EMBEDDING_DIMENSIONS}\)", ddl, re.IGNORECASE) is not None, (
        f"pinned column type vector({EMBEDDING_DIMENSIONS}) not emitted: {ddl}"
    )

    from pgvector import Vector as PgVectorLiteral

    distance = table.c.embedding.comparator.cosine_distance(
        PgVectorLiteral([0.1] * EMBEDDING_DIMENSIONS)
    )
    compiled = str(select(distance).compile(dialect=postgresql.dialect()))
    assert "<=>" in compiled, f"cosine distance operator missing from compiled SQL: {compiled}"


def test_embeddings_sdk_targets_the_embeddings_endpoint() -> None:
    # C07 rule 7: embeddings use POST {base_url}/v1/embeddings — never
    # /chat/completions. Captured with a mock transport: the request the
    # 07-chunk-index producer will make, and the response it must parse,
    # on the pinned SDK version. Offline — no endpoint is dialed.
    import httpx2
    import openai

    captured: dict[str, object] = {}

    async def handler(request: httpx2.Request) -> httpx2.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "object": "list",
            "data": [{
                "object": "embedding",
                "index": 0,
                "embedding": [0.05] * EMBEDDING_DIMENSIONS,
            }],
            "model": EMBEDDING_MODEL,
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        })

    async def embed() -> object:
        client = openai.AsyncOpenAI(
            base_url="http://127.0.0.1:9991/v1",
            api_key="guard-offline",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        )
        try:
            return await client.embeddings.create(
                model=EMBEDDING_MODEL, input=["dimension contract probe"]
            )
        finally:
            await client.close()

    response = _run(embed())

    assert captured["method"] == "POST", f"embeddings must POST: {captured['method']}"
    assert str(captured["url"]).endswith("/v1/embeddings"), (
        f"embeddings must target /v1/embeddings (C07 rule 7): {captured['url']}"
    )
    assert "/chat/completions" not in str(captured["url"]), (
        "embedding request must never target the chat-completions API"
    )
    body = captured["body"]
    assert body["model"] == EMBEDDING_MODEL, f"embedding model not carried: {body}"
    assert body["input"] == ["dimension contract probe"], f"input not carried: {body}"

    embedding = response.data[0].embedding
    assert len(embedding) == EMBEDDING_DIMENSIONS, (
        f"parsed embedding must carry the pinned {EMBEDDING_DIMENSIONS} dims: {len(embedding)}"
    )


def test_fetch_client_bounded_http_surface() -> None:
    # The primitive 07-search-capability (SearXNG JSON), 07-page-reader and
    # the Hindsight HTTP adapter build on: one async client, one request,
    # parsed JSON — no browser, no driver session. Offline via mock transport.
    import httpx2

    captured: dict[str, object] = {}

    async def handler(request: httpx2.Request) -> httpx2.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx2.Response(200, json={"number_of_results": 1, "results": [{"title": "ok"}]})

    async def fetch() -> object:
        async with httpx2.AsyncClient(
            base_url="http://127.0.0.1:9992", transport=httpx2.MockTransport(handler)
        ) as client:
            return await client.get("/search", params={"q": "guard", "format": "json"})

    response = _run(fetch())
    assert captured["method"] == "GET"
    assert str(captured["url"]) == "http://127.0.0.1:9992/search?q=guard&format=json"
    assert response.status_code == 200
    assert response.json()["results"][0]["title"] == "ok"
