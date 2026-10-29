"""Jobs: human-readable run label on ``job_runs`` (W1).

The label is frozen at submit: the first step's ``label`` payload field
when present, else the first step's ``capability_id``. It feeds the
run list text search (``q``) and wire views.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_runs",
        sa.Column(
            "label",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
            comment="Human-readable run label frozen at submit: the first "
            "step's ``label`` payload field when present, else its "
            "capability_id (W1); feeds list text search.",
        ),
    )
    op.create_check_constraint(
        "ck_job_runs_label_len", "job_runs", "char_length(label) <= 200"
    )


def downgrade() -> None:
    op.drop_constraint("ck_job_runs_label_len", "job_runs", type_="check")
    op.drop_column("job_runs", "label")
