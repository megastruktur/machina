"""Seed the terminal-state catalog tags (W1 fix: fresh-install classification).

Claim being fixed: on a production database migrated fresh to 0011 the tag
catalog held only the reserved ``unclassified`` row. Every terminal run
transition (``succeeded`` / ``failed`` / ``cancelled``) then failed inside
``mai.tag_store._aclassify_terminal_job_locked`` with
``TagStoreError(TagErrorCode.TAG_NOT_FOUND)`` because the lifecycle writer
requires a pre-existing tag matching ``terminal_tag_name``.

This revision seeds the three frozen terminal names with stable explicit
UUIDs so fresh installs land in the same state the lifecycle suite always
assumed (``tests/test_lifecycle.py`` seeds the same names per test today;
after this revision the store-created rows and the seeded rows share the
canonical identity). Schema, triggers and the reserved row stay untouched:
no new tables, no trigger DDL, no backfill — existing entities keep their
current classification.

Idempotency + user-data safety (mirror of the 0011 seed discipline):

- ``ON CONFLICT (tag_id) DO NOTHING`` keeps a re-execution (or an upgrade
  whose seed row already exists) a no-op instead of duplicating.
- The ``NOT EXISTS`` canonical-name guard skips names that a user already
  created under any casing. ``ux_tags_name_canonical`` is a UNIQUE INDEX on
  ``lower(name)``, so a same-name user row would otherwise collide on a
  DIFFERENT arbiter than ``tag_id`` and abort the statement; skipping the
  row preserves that user tag (its id and description) verbatim — exactly
  the convention ``acreate_tag`` applies at runtime.

The downgrade deletes ONLY a seeded id that no longer carries an
association (``job_tags`` / ``conversation_tags``). A seeded id that users
adopted stays; user-created rows and every association are never touched.
"""

from __future__ import annotations

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None

# Stable identities of the three terminal catalog rows. Constants (not
# random UUIDs) keep re-upgrades idempotent and let the lifecycle writer
# resolve the tags by name on every install; the focused test suite pins
# these literals against a real database.
TERMINAL_SUCCEEDED_TAG_ID = "7707533f-b618-4f6a-86a4-f3fb22f79ad0"
TERMINAL_FAILED_TAG_ID = "24119527-3bd3-4224-938b-ac407805c886"
TERMINAL_CANCELLED_TAG_ID = "c0d58dcb-378b-4c2c-8d78-4ba45f57b3d0"

# One statement for the whole seed: skip rows whose canonical name already
# exists under ANY id (preserving same-name user tags against the
# lower(name) unique index) and skip rows whose id already exists (re-run).
_SEED_TERMINAL_TAGS_SQL = f"""
INSERT INTO public.tags (tag_id, name, description)
SELECT seed.tag_id, seed.name, seed.description
FROM (VALUES
    ('{TERMINAL_SUCCEEDED_TAG_ID}'::uuid, 'succeeded',
     'Run reached its terminal succeeded state; assigned automatically at '
     'the lifecycle terminal checkpoint.'),
    ('{TERMINAL_FAILED_TAG_ID}'::uuid, 'failed',
     'Run terminated with an error; assigned automatically at the '
     'lifecycle terminal checkpoint.'),
    ('{TERMINAL_CANCELLED_TAG_ID}'::uuid, 'cancelled',
     'Run was cancelled before completion; assigned automatically at the '
     'lifecycle terminal checkpoint.')
) AS seed(tag_id, name, description)
WHERE NOT EXISTS (
    SELECT 1 FROM public.tags t WHERE lower(t.name) = lower(seed.name)
)
ON CONFLICT (tag_id) DO NOTHING
"""

# Only UNUSED seeded ids are removed; a seeded id still referenced by an
# association (job_tags / conversation_tags) survives so user data and
# classifications are never deleted by a downgrade.
_DOWNGRADE_UNUSED_SEED_SQL = f"""
DELETE FROM public.tags t
WHERE t.tag_id IN (
    '{TERMINAL_SUCCEEDED_TAG_ID}'::uuid,
    '{TERMINAL_FAILED_TAG_ID}'::uuid,
    '{TERMINAL_CANCELLED_TAG_ID}'::uuid
)
AND NOT EXISTS (
    SELECT 1 FROM public.job_tags jt WHERE jt.tag_id = t.tag_id
)
AND NOT EXISTS (
    SELECT 1 FROM public.conversation_tags ct WHERE ct.tag_id = t.tag_id
)
"""


def upgrade() -> None:
    op.execute(_SEED_TERMINAL_TAGS_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_UNUSED_SEED_SQL)
