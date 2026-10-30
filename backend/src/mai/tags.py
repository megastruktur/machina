"""History-tag schema constants shared by the runtime writers (W1).

The mandatory-tag schema (migration ``0011``) requires every ``job_runs`` /
``conversations`` row to carry at least one ``job_tags`` /
``conversation_tags`` association inside the SAME transaction that creates
the entity. Entity-creating writers therefore attach the reserved default
classification here — the store/API/UI slices later replace it with
meaningful tags.

``RESERVED_UNCLASSIFIED_TAG_ID`` is the single source of truth for runtime
code; ``migrations/versions/0011_history_tags.py`` and the focused schema
tests mirror the same fixed value (the chain stays import-standalone), and
``tests/test_history_tag_schema.py`` asserts the mirror equality so a drift
fails loudly.
"""

from __future__ import annotations

from typing import Final

#: Fixed identity of the reserved ``unclassified`` catalog row (seeded by
#: migration 0011 and protected against deletion there).
RESERVED_UNCLASSIFIED_TAG_ID: Final[str] = "6a1e9d3c-5b4f-4c2a-9e8d-7f0a1b2c3d4e"

#: ``assignment_source`` value written by migration/backfill and system
#: writers (agent/user assignments arrive with the tag tools in W3+).
SYSTEM_ASSIGNMENT_SOURCE: Final[str] = "system"
