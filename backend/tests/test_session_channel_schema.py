"""D06 schema/no-drift tests — channel schema export, frame validation and
generated-TypeScript discipline (BRIEF_D06 §D/§E.9).

1. The exported JSON Schema is DETERMINISTIC: two exports over the same
   code produce byte-identical documents (no drift).
2. The checked-in schema file matches the live export (no drift between
   the committed artifact and the contracts).
3. Malformed CLIENT example frames fail ``validate_client_frame``; a
   malformed SERVER example frame fails the envelope validation (both
   directions of the wire contract are closed).
4. The generated TypeScript (``session-channel.generated.ts``) exists,
   carries the discriminated unions, and contains NO ``any``/``unknown``
   escape hatch.
5. The pinned generator (``json-schema-to-typescript@16.0.0``) and the
   ``generate:session-channel`` script are declared in ``package.json``.
6. The OpenAPI surface: the new ``/api/v1/agent-sessions/**`` routes exist;
   ``/api/v1/chat/**`` still exists at this stage (D09 removes it).
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI

from mai.api.app import create_app
from mai.contracts.session_channel import (
    CHANNEL_PROTOCOL_VERSION,
    ClientChannelFrame,
    ServerChannelFrame,
    validate_client_frame,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"
SCHEMA_PATH = FRONTEND_ROOT / "src" / "lib" / "api" / "session-channel.schema.json"
GENERATED_PATH = FRONTEND_ROOT / "src" / "lib" / "api" / "session-channel.generated.ts"

pytestmark = pytest.mark.anyio


def test_export_is_deterministic(tmp_path: Path) -> None:
    from mai.scripts.export_session_channel_schema import (
        export_session_channel_schema,
    )

    first = json.dumps(export_session_channel_schema(), indent=2)
    second = json.dumps(export_session_channel_schema(), indent=2)
    assert first == second
    document = json.loads(first)
    assert document["$id"].endswith(f"/v{CHANNEL_PROTOCOL_VERSION}/session-channel.json")
    # Every nested model resolves inside ONE document (no external refs).
    assert "$defs" not in document
    assert len(document["definitions"]) >= 20
    assert len(document["oneOf"]) == 2


def test_checked_in_schema_matches_live_export() -> None:
    from mai.scripts.export_session_channel_schema import (
        export_session_channel_schema,
    )

    committed = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    live = export_session_channel_schema()
    assert committed == live


def test_malformed_client_frames_fail_validation() -> None:
    # Wrong protocol version.
    with pytest.raises(Exception):
        validate_client_frame(
            json.dumps(
                {
                    "type": "hello",
                    "protocol_version": 99,
                    "after_seq": None,
                    "csrf_token": "x",
                    "client_request_id": str(__import__("uuid").uuid4()),
                }
            )
        )
    # Unknown frame type (an engine-state write attempt has NO frame).
    with pytest.raises(Exception):
        validate_client_frame(
            json.dumps({"type": "engine_cancel", "run_id": str(uuid.uuid4())})
        )
    # Oversized frame.
    with pytest.raises(Exception):
        validate_client_frame(json.dumps({"type": "ping"}) + "x" * (64 * 1024))
    # Malformed JSON.
    with pytest.raises(Exception):
        validate_client_frame("{nope")
    # Extra fields are forbidden (the double-submit proof only).
    with pytest.raises(Exception):
        validate_client_frame(
            json.dumps({"type": "ping", "run_id": str(uuid.uuid4())})
        )


def test_malformed_server_frame_fails_envelope_validation() -> None:
    from mai.scripts.export_session_channel_schema import _ServerFrameEnvelope
    from pydantic import ValidationError

    # A server frame with an unknown discriminator is rejected by the
    # envelope (the closed server vocabulary).
    with pytest.raises(ValidationError):
        _ServerFrameEnvelope.model_validate(
            {"frame": {"type": "nonsense", "code": "x", "reason": "y"}}
        )
    # A committed frame without its entry payload is rejected.
    with pytest.raises(ValidationError):
        _ServerFrameEnvelope.model_validate(
            {"frame": {"type": "committed", "stream_id": "s"}}
        )
    # A delta whose text exceeds the bound is rejected.
    with pytest.raises(ValidationError):
        _ServerFrameEnvelope.model_validate(
            {
                "frame": {
                    "type": "delta",
                    "stream_id": "s",
                    "delta_id": "d",
                    "text": "x" * 5000,
                    "run_id": str(uuid.uuid4()),
                }
            }
        )


def test_generated_ts_exists_without_escape_hatches() -> None:
    source = GENERATED_PATH.read_text(encoding="utf-8")
    # The pinned generator's banner: the file is GENERATED, not hand-written.
    assert "automatically generated by json-schema-to-typescript" in source
    # The discriminated unions exist and narrow on `type`.
    assert "export type Frame =" in source
    assert "export type Frame1 =" in source
    assert '"hello"' in source and '"committed"' in source
    # NO escape hatch IN THE FRAME UNIONS: every discriminator is a string
    # literal and no frame field types `any`/`unknown`. (The one
    # `[k: string]: unknown` in the file is `ToolCallItem.args` — the D01
    # open JSON-object payload of tool-call arguments, not a frame field.)
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("type:") and not stripped.startswith(
            "type: Type"
        ):
            assert "any" not in stripped and "unknown" not in stripped
    assert ": any;" not in source
    assert "export type Frame = any" not in source


def test_package_json_declares_pinned_generator() -> None:
    package = json.loads(
        (FRONTEND_ROOT / "package.json").read_text(encoding="utf-8")
    )
    assert (
        package["devDependencies"]["json-schema-to-typescript"] == "16.0.0"
    )
    assert "generate:session-channel" in package["scripts"]
    assert "json2ts" in package["scripts"]["generate:session-channel"]


async def test_openapi_route_presence() -> None:
    url = os.environ.get("MAI_TEST_DATABASE_URL")
    app: FastAPI = create_app(
        env={
            "MAI_DATABASE_URL": url or "",
            "MAI_PUBLIC_ORIGINS": "http://testserver",
            "MAI_DATA_ROOT": str(tmp_dir()),
        }
    )
    openapi = app.openapi()
    paths = set(openapi["paths"])
    assert "/api/v1/agent-sessions" in paths
    assert "/api/v1/agent-sessions/{session_id}/turns" in paths
    assert "/api/v1/agent-sessions/{session_id}/control" in paths
    # ADDITIVE (COORD-D02-01): the old chat surface stays until D09.
    assert any(path.startswith("/api/v1/chat") for path in paths)


def tmp_dir() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="mai-fxt-06-schema-"))
