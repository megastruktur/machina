"""Neutral tag and pagination vocabulary shared by every retained consumer.

Campaign ``machina-session-first-chat`` (D01 extraction): the server-agnostic
tag projection types (``EntityTagPreview`` / ``EntityTagPreviewView``) and the
product-wide pagination bounds previously frozen inside the conversation
contract move here so retained Jobs/tags surfaces (``api/jobs.py``,
``api/history_tags.py``, ``tag_store.py``, ``history_store.py``) and the new
session-first vocabulary depend on this neutral module instead of the deleted
chat contracts. ``contracts/conversations.py`` keeps re-exporting these names
until D09 deletes that module; the re-exports are the same objects, so every
existing caller keeps working unchanged.

Governing rules:

- **Pagination (C01 rule 5; AGENTS.md mandatory-pagination invariant)** —
  every collection view is a bounded page envelope: ``DEFAULT_PAGE_LIMIT =
  50``, ``MAX_PAGE_LIMIT = 200`` and ``limit|offset|total|has_more`` on every
  page. ``compute_has_more`` is the single canonical ``has_more`` derivation;
  producers must not reinvent it.
- **Tag previews** — a listing projection carries at most the first three
  deterministic tag names plus the honest total (``total >= 1``: an entity
  with no tags is represented by ``None``, never by a fabricated empty
  projection).
- **Envelope honesty** — a page that claims more results cannot start at or
  past the collection end; the consistency validator fails closed on any
  dishonest continuation metadata.

The module is pure: no process environment, filesystem, network or clock
access — validation is SSR/worker/replay safe and never leaks ambient secret
material.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "EntityTagPreview",
    "EntityTagPreviewView",
    "PageEnvelope",
    "compute_has_more",
]

# --- pagination bounds (C01 rule 5; AGENTS.md mandatory-pagination invariant) ---

#: Default list page size for every collection endpoint/tool in the product.
DEFAULT_PAGE_LIMIT: Final[int] = 50

#: Hard upper bound any single page may request.
MAX_PAGE_LIMIT: Final[int] = 200


# --- canonical has_more derivation --------------------------------------------


def compute_has_more(offset: int, returned: int, total: int) -> bool:
    """Canonical ``has_more`` derivation shared by every paginated producer."""
    return offset + returned < total


# --- tag previews ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityTagPreview:
    """Bounded listing projection: first names plus the honest total."""

    preview: tuple[str, ...]
    total: int


class EntityTagPreviewView(BaseModel):
    """Bounded listing projection of tag names plus honest total count."""

    model_config = ConfigDict(extra="forbid")

    preview: list[str] = Field(
        max_length=3,
        description="First ≤3 tag names (deterministic name order)",
    )
    total: int = Field(
        ge=1,
        description="Honest total number of tags attached (including beyond "
        "preview); at least one tag is mandatory, so 0 is never valid",
    )


# --- bounded page envelope ------------------------------------------------------


class PageEnvelope(BaseModel):
    """Bounded page metadata every collection view carries (C01 rule 5;
    AGENTS.md mandatory-pagination invariant)."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Page size used for this slice (1..200)",
    )
    offset: int = Field(ge=0, description="Slice start within the collection")
    total: int = Field(ge=0, description="Pre-slice matching count")
    has_more: bool = Field(
        description="True when another page exists after this slice",
    )

    @model_validator(mode="after")
    def _envelope_is_consistent(self) -> "PageEnvelope":
        if self.has_more and self.offset >= self.total:
            raise ValueError(
                "has_more must be false when the slice starts at or past the "
                "collection end"
            )
        return self
