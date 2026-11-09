"""SCOPE-1: typed nullable session service scope column.

Adds one nullable ``sessions.service_id`` (PostgreSQL UUID) to the session
header (``chat-recovery`` plan §B "durable session service scope"):

- The identity is a FIRST-CLASS typed column, but deliberately carries NO
  foreign key and NO index: it points into the per-server JSON Service Map
  (a confined-store identity, not a relational row), admission validates the
  identity against the SAME-server map at create time, and this feature
  performs no service-filtered queries. Legacy rows stay NULL — no guessed
  backfill (generic ``session_tags`` are NOT authority and are untouched).
- ``downgrade`` drops exactly this column and nothing else; the rest of the
  0014/0015 surface survives unchanged.

Chain position: sixteenth revision of the single linear migration chain
(exclusive ``migration-chain`` resource); ``down_revision`` is ``0015``.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-09

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the nullable service-scope column; no FK, no index, no backfill."""
    op.add_column(
        "sessions",
        sa.Column(
            "service_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Optional adopted-service scope (Service Map identity; "
            "validated against the SAME-server map at admission — never "
            "a relational FK).",
        ),
    )


def downgrade() -> None:
    """Drop ONLY the added column (legacy NULL rows carry no data to keep)."""
    op.drop_column("sessions", "service_id")
