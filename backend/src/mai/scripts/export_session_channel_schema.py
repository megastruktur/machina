"""Export the versioned session-channel JSON Schema (D06).

Writes the ``$defs``-complete, ``$id``-versioned JSON Schema for the
discriminated ``ClientChannelFrame`` / ``ServerChannelFrame`` unions to the
checked-in frontend path (``frontend/src/lib/api/session-channel.schema.json``)
so the PINNED generator (``json-schema-to-typescript@16.0.0``) produces the
ONLY TypeScript source of truth for the frames
(``pnpm generate:session-channel``).

Deterministic output: two runs over the same code produce byte-identical
files (the no-drift contract; the schema test asserts it).

Usage (from ``backend/``)::

    uv run python -m mai.scripts.export_session_channel_schema \
        --out ../frontend/src/lib/api/session-channel.schema.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Final

from mai.contracts.session_channel import (
    CHANNEL_PROTOCOL_VERSION,
    MAX_FRAME_BYTES,
    ClientChannelFrame,
    ServerChannelFrame,
)
from pydantic import BaseModel, ConfigDict, Field


class _ClientFrameEnvelope(BaseModel):
    """Export envelope: one client frame (the discriminated union)."""

    model_config = ConfigDict(extra="forbid")

    frame: ClientChannelFrame = Field(
        description="One versioned client channel frame (discriminated on type)"
    )


class _ServerFrameEnvelope(BaseModel):
    """Export envelope: one server frame (the discriminated union)."""

    model_config = ConfigDict(extra="forbid")

    frame: ServerChannelFrame = Field(
        description="One versioned server channel frame (discriminated on type)"
    )


__all__ = ["export_session_channel_schema"]

_SCHEMA_ID_BASE: Final = "https://mai.dev/schemas/session-channel"


def export_session_channel_schema() -> dict[str, object]:
    """Build the deterministic, versioned schema document."""
    _ClientFrameEnvelope.model_rebuild()
    _ServerFrameEnvelope.model_rebuild()
    client = _ClientFrameEnvelope.model_json_schema()
    server = _ServerFrameEnvelope.model_json_schema()

    def _with_id(document: dict[str, object], name: str) -> dict[str, object]:
        document = dict(document)
        document["$id"] = f"{_SCHEMA_ID_BASE}/v{CHANNEL_PROTOCOL_VERSION}/{name}.json"
        document["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        return document


    def _normalize_refs(node: object) -> object:
        """Point every ``#/$defs/...`` ref at the shared ``definitions``
        container (the pinned generator resolves ``#/definitions/``)."""
        if isinstance(node, dict):
            rewritten: dict[str, object] = {}
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    rewritten[key] = value.replace("#/$defs/", "#/definitions/")
                else:
                    rewritten[key] = _normalize_refs(value)
            return rewritten
        if isinstance(node, list):
            return [_normalize_refs(item) for item in node]
        return node

    client_doc = _with_id(client, "client-channel-frame")
    server_doc = _with_id(server, "server-channel-frame")
    client_doc = _normalize_refs(client_doc)  # type: ignore[arg-type]
    server_doc = _normalize_refs(server_doc)  # type: ignore[arg-type]

    # Merge the $defs so every nested model resolves inside ONE document
    # (no external references; the pinned generator inlines definitions).
    defs: dict[str, object] = {}
    defs.update(client_doc.get("$defs", {}))  # type: ignore[arg-type]
    for key, value in server_doc.get("$defs", {}).items():  # type: ignore[union-attr]
        defs.setdefault(key, value)
    client_doc["definitions"] = defs
    client_doc.pop("$defs", None)
    server_doc["definitions"] = defs
    server_doc.pop("$defs", None)
    return {
        "$id": f"{_SCHEMA_ID_BASE}/v{CHANNEL_PROTOCOL_VERSION}/session-channel.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "MAI session channel frames",
        "description": (
            f"Versioned (protocol v{CHANNEL_PROTOCOL_VERSION}) session "
            "channel frames; frame ceiling "
            f"{MAX_FRAME_BYTES} bytes; generated from "
            "mai.contracts.session_channel — never hand-edited."
        ),
        "oneOf": [
            {
                "$id": client_doc["$id"],
                "title": "ClientChannelFrame",
                **{
                    key: value
                    for key, value in client_doc.items()
                    if key not in {"$id", "title"}
                },
            },
            {
                "$id": server_doc["$id"],
                "title": "ServerChannelFrame",
                **{
                    key: value
                    for key, value in server_doc.items()
                    if key not in {"$id", "title"}
                },
            },
        ],
        "definitions": defs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export the session-channel JSON Schema (D06)."
    )
    parser.add_argument(
        "--out",
        default="../frontend/src/lib/api/session-channel.schema.json",
        help="Output path (default: the checked-in frontend path)",
    )
    arguments = parser.parse_args()
    document = export_session_channel_schema()
    out = pathlib.Path(arguments.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(document, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"session-channel schema written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
